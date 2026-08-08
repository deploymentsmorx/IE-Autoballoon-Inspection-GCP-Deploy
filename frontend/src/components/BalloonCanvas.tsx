import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { Balloon } from "../api";
import { useAuthImage } from "../hooks/useAuthImage";

export type Mode = "view" | "create" | "edit" | "delete";

type Box = { x0: number; y0: number; x1: number; y1: number };

const PREVIEW_SCALE = 1.35;

type Props = {
  imageUrl: string;
  balloons: Balloon[];
  mode: Mode;
  selected: number | null;
  busy?: boolean;
  onSelect: (no: number | null) => void;
  onCreate: (box: { x: number; y: number; width: number; height: number }) => void;
  onMove: (no: number, x: number, y: number) => void;
  onMoveEnd: () => void;
  onDelete: (no: number) => void;
};

/**
 * PDF-reader style zoom/pan viewport + balloon create/edit/delete on canvas.
 * Coordinates are in image-pixel space, mapped to PDF points via PREVIEW_SCALE.
 */
export default function BalloonCanvas({
  imageUrl,
  balloons,
  mode,
  selected,
  busy,
  onSelect,
  onCreate,
  onMove,
  onMoveEnd,
  onDelete,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [ready, setReady] = useState(0);
  const [zoomPct, setZoomPct] = useState(100);

  const zoomRef = useRef(1);
  const panRef = useRef({ x: 0, y: 0 });
  const spaceRef = useRef(false);
  const panningRef = useRef(false);
  const panStartRef = useRef({ x: 0, y: 0, panX: 0, panY: 0 });

  const drawingRef = useRef(false);
  const boxRef = useRef<Box | null>(null);
  const [liveBox, setLiveBox] = useState<Box | null>(null);
  const dragNoRef = useRef<number | null>(null);
  const movedRef = useRef(false);

  const balloonsRef = useRef(balloons);
  balloonsRef.current = balloons;
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const { src, failed, loading } = useAuthImage(imageUrl);

  useEffect(() => {
    if (!src) {
      imgRef.current = null;
      if (failed) setReady((n) => n + 1);
      return;
    }
    const img = new Image();
    img.onload = () => {
      imgRef.current = img;
      fitToView();
      setReady((n) => n + 1);
    };
    img.onerror = () => {
      imgRef.current = null;
      setReady((n) => n + 1);
    };
    img.src = src;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.code === "Space") {
        spaceRef.current = true;
        e.preventDefault();
      }
    };
    const up = (e: KeyboardEvent) => {
      if (e.code === "Space") spaceRef.current = false;
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, []);

  const sizeCanvas = () => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return null;
    const cssW = Math.max(100, wrap.clientWidth);
    const cssH = Math.max(220, wrap.clientHeight);
    const dpr = window.devicePixelRatio || 1;
    const bw = Math.floor(cssW * dpr);
    const bh = Math.floor(cssH * dpr);
    if (canvas.width !== bw || canvas.height !== bh) {
      canvas.width = bw;
      canvas.height = bh;
      canvas.style.width = `${cssW}px`;
      canvas.style.height = `${cssH}px`;
    }
    return { canvas, cssW, cssH, dpr };
  };

  const fitToView = () => {
    const wrap = wrapRef.current;
    const img = imgRef.current;
    if (!wrap || !img || !img.naturalWidth) return;
    const pad = 16;
    const z = Math.min(
      (wrap.clientWidth - pad) / img.naturalWidth,
      (wrap.clientHeight - pad) / img.naturalHeight,
      2,
    );
    zoomRef.current = Math.max(0.1, z);
    panRef.current = {
      x: (wrap.clientWidth - img.naturalWidth * zoomRef.current) / 2,
      y: (wrap.clientHeight - img.naturalHeight * zoomRef.current) / 2,
    };
    setZoomPct(Math.round(zoomRef.current * 100));
    redraw();
  };

  const setZoomAround = (nextZoom: number, cx?: number, cy?: number) => {
    const wrap = wrapRef.current;
    const img = imgRef.current;
    if (!wrap || !img) return;
    const z0 = zoomRef.current;
    const z1 = Math.min(8, Math.max(0.15, nextZoom));
    const ax = cx ?? wrap.clientWidth / 2;
    const ay = cy ?? wrap.clientHeight / 2;
    const ix = (ax - panRef.current.x) / z0;
    const iy = (ay - panRef.current.y) / z0;
    zoomRef.current = z1;
    panRef.current = { x: ax - ix * z1, y: ay - iy * z1 };
    setZoomPct(Math.round(z1 * 100));
    redraw();
  };

  const imgPxToPdf = (ix: number, iy: number) => ({
    x: ix / PREVIEW_SCALE,
    y: iy / PREVIEW_SCALE,
  });
  const pdfToImgPx = (x: number, y: number, w: number, h: number) => ({
    x: x * PREVIEW_SCALE,
    y: y * PREVIEW_SCALE,
    w: w * PREVIEW_SCALE,
    h: h * PREVIEW_SCALE,
  });

  const clientToImg = (clientX: number, clientY: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const vx = clientX - rect.left;
    const vy = clientY - rect.top;
    const z = zoomRef.current;
    return {
      vx,
      vy,
      ix: (vx - panRef.current.x) / z,
      iy: (vy - panRef.current.y) / z,
    };
  };

  const redraw = () => {
    const sized = sizeCanvas();
    const img = imgRef.current;
    if (!sized || !img) return;
    const { canvas, cssW, cssH, dpr } = sized;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.fillStyle = "#0a0e13";
    ctx.fillRect(0, 0, cssW, cssH);

    const z = zoomRef.current;
    const pan = panRef.current;
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(img, pan.x, pan.y, img.naturalWidth * z, img.naturalHeight * z);

    // Balloon markers (small numbered chips at dim centers — not full yellow sheets)
    for (const b of balloonsRef.current) {
      const r = pdfToImgPx(b.x, b.y, b.width || 20, b.height || 12);
      const cx = pan.x + (r.x + r.w / 2) * z;
      const cy = pan.y + (r.y + r.h / 2) * z;
      const active = selectedRef.current === b.balloon;
      const rad = Math.max(9, 11 * Math.min(z, 2));
      ctx.beginPath();
      ctx.arc(cx, cy, rad, 0, Math.PI * 2);
      ctx.fillStyle = active ? "rgba(61,126,255,0.95)" : "rgba(255,247,191,0.95)";
      ctx.fill();
      ctx.lineWidth = active ? 2.5 : 1.5;
      ctx.strokeStyle = active ? "#3d7eff" : "#c62828";
      ctx.stroke();
      ctx.fillStyle = active ? "#fff" : "#b71c1c";
      ctx.font = `bold ${Math.max(9, 10 * Math.min(z, 1.8))}px Segoe UI, sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(b.balloon), cx, cy + 0.5);

      // In edit/create show thin dim box for selected only
      if (active) {
        ctx.save();
        ctx.strokeStyle = "#3d7eff";
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.strokeRect(pan.x + r.x * z, pan.y + r.y * z, Math.max(4, r.w * z), Math.max(4, r.h * z));
        ctx.restore();
      }
    }

    const box = boxRef.current;
    if (box) {
      const x = pan.x + Math.min(box.x0, box.x1) * z;
      const y = pan.y + Math.min(box.y0, box.y1) * z;
      const w = Math.abs(box.x1 - box.x0) * z;
      const h = Math.abs(box.y1 - box.y0) * z;
      ctx.save();
      ctx.strokeStyle = "#3d7eff";
      ctx.fillStyle = "rgba(61,126,255,0.2)";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
    }
  };

  useEffect(() => {
    redraw();
    const onResize = () => redraw();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, balloons, selected, liveBox, mode, imageUrl, zoomPct]);

  // Non-passive wheel so we can prevent browser/page zoom
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const onWheelNative = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const rect = el.getBoundingClientRect();
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      setZoomAround(zoomRef.current * factor, e.clientX - rect.left, e.clientY - rect.top);
    };
    el.addEventListener("wheel", onWheelNative, { passive: false });
    return () => el.removeEventListener("wheel", onWheelNative);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, imageUrl]);

  const hitBalloon = (ix: number, iy: number) => {
    let best: Balloon | null = null;
    let bestD = Infinity;
    for (const b of balloonsRef.current) {
      const r = pdfToImgPx(b.x, b.y, b.width || 20, b.height || 12);
      const cx = r.x + r.w / 2;
      const cy = r.y + r.h / 2;
      const hitR = Math.max(14, Math.hypot(r.w, r.h) * 0.35) / Math.max(zoomRef.current, 0.5);
      const d = Math.hypot(cx - ix, cy - iy);
      // also hit inside dim rect
      const inside =
        ix >= r.x - 4 && iy >= r.y - 4 && ix <= r.x + r.w + 4 && iy <= r.y + r.h + 4;
      if ((inside || d < hitR * 2) && d < bestD) {
        bestD = d;
        best = b;
      }
    }
    return best;
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    if (busy) return;
    const pt = clientToImg(e.clientX, e.clientY);
    if (!pt) return;
    canvasRef.current?.setPointerCapture(e.pointerId);

    // Pan: middle button, or Space+left, or Alt+left
    if (e.button === 1 || spaceRef.current || e.altKey) {
      panningRef.current = true;
      panStartRef.current = { x: e.clientX, y: e.clientY, panX: panRef.current.x, panY: panRef.current.y };
      return;
    }
    if (e.button !== 0) return;

    const m = modeRef.current;
    if (m === "create") {
      drawingRef.current = true;
      const box = { x0: pt.ix, y0: pt.iy, x1: pt.ix, y1: pt.iy };
      boxRef.current = box;
      setLiveBox(box);
      return;
    }
    if (m === "view") {
      panningRef.current = true;
      panStartRef.current = { x: e.clientX, y: e.clientY, panX: panRef.current.x, panY: panRef.current.y };
      return;
    }

    const hit = hitBalloon(pt.ix, pt.iy);
    if (m === "delete") {
      if (hit) onDelete(hit.balloon);
      return;
    }
    if (m === "edit") {
      if (hit) {
        onSelect(hit.balloon);
        dragNoRef.current = hit.balloon;
        movedRef.current = false;
      } else {
        onSelect(null);
        dragNoRef.current = null;
        panningRef.current = true;
        panStartRef.current = { x: e.clientX, y: e.clientY, panX: panRef.current.x, panY: panRef.current.y };
      }
    }
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    if (panningRef.current) {
      const dx = e.clientX - panStartRef.current.x;
      const dy = e.clientY - panStartRef.current.y;
      panRef.current = { x: panStartRef.current.panX + dx, y: panStartRef.current.panY + dy };
      redraw();
      return;
    }
    if (busy) return;
    const pt = clientToImg(e.clientX, e.clientY);
    if (!pt) return;

    if (modeRef.current === "create" && drawingRef.current && boxRef.current) {
      const box = { ...boxRef.current, x1: pt.ix, y1: pt.iy };
      boxRef.current = box;
      setLiveBox(box);
      return;
    }

    if (modeRef.current === "edit" && dragNoRef.current != null && (e.buttons & 1) === 1) {
      const b = balloonsRef.current.find((x) => x.balloon === dragNoRef.current);
      if (!b) return;
      const pdf = imgPxToPdf(pt.ix, pt.iy);
      movedRef.current = true;
      onMove(dragNoRef.current, pdf.x - (b.width || 20) / 2, pdf.y - (b.height || 12) / 2);
    }
  };

  const onPointerUp = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    try {
      canvasRef.current?.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    if (panningRef.current) {
      panningRef.current = false;
      return;
    }

    if (modeRef.current === "create" && drawingRef.current) {
      drawingRef.current = false;
      const box = boxRef.current;
      boxRef.current = null;
      setLiveBox(null);
      if (!box) return;
      let x0 = Math.min(box.x0, box.x1);
      let y0 = Math.min(box.y0, box.y1);
      let x1 = Math.max(box.x0, box.x1);
      let y1 = Math.max(box.y0, box.y1);
      if (x1 - x0 < 8 && y1 - y0 < 8) {
        const cx = (x0 + x1) / 2;
        const cy = (y0 + y1) / 2;
        x0 = cx - 24;
        y0 = cy - 14;
        x1 = cx + 24;
        y1 = cy + 14;
      }
      const p0 = imgPxToPdf(x0, y0);
      const p1 = imgPxToPdf(x1, y1);
      onCreate({
        x: Math.min(p0.x, p1.x),
        y: Math.min(p0.y, p1.y),
        width: Math.max(4, Math.abs(p1.x - p0.x)),
        height: Math.max(4, Math.abs(p1.y - p0.y)),
      });
      return;
    }

    if (modeRef.current === "edit" && movedRef.current) {
      movedRef.current = false;
      dragNoRef.current = null;
      onMoveEnd();
    } else {
      dragNoRef.current = null;
    }
  };

  const cursor =
    mode === "create"
      ? "crosshair"
      : panningRef.current || mode === "view"
        ? "grab"
        : "pointer";

  return (
    <div className="viewer-shell">
      <div className="viewer-toolbar">
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setZoomAround(zoomRef.current / 1.2)}>
          −
        </button>
        <span className="viewer-zoom-label">{zoomPct}%</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setZoomAround(zoomRef.current * 1.2)}>
          +
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={fitToView}>
          Fit
        </button>
        <button type="button" className="btn btn-ghost btn-sm" onClick={() => setZoomAround(1)}>
          100%
        </button>
        <span className="muted viewer-hint">
          Wheel = zoom · Drag / Space+drag = pan · Create = draw box
        </span>
      </div>
      <div ref={wrapRef} className={`panel-body canvas-wrap viewer-body ${mode === "create" ? "drawing" : ""}`}>
        {loading || (!imgRef.current && ready === 0 && !failed) ? (
          <div className="placeholder">Loading preview…</div>
        ) : !imgRef.current || failed ? (
          <div className="placeholder">Preview unavailable.</div>
        ) : (
          <canvas
            ref={canvasRef}
            className="balloon-canvas viewer-canvas"
            style={{ cursor, touchAction: "none" }}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
          />
        )}
      </div>
    </div>
  );
}
