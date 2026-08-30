import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { Balloon, PageGeometry } from "../api";
import { useAuthImage } from "../hooks/useAuthImage";

export type Mode = "view" | "create" | "edit" | "delete" | "area";

/** True when left-drag should pan instead of edit/create/delete. */
function shouldPan(e: ReactPointerEvent, mode: Mode, spaceDown: boolean) {
  return (
    e.button === 1 ||
    e.button === 2 ||
    spaceDown ||
    e.altKey ||
    e.shiftKey ||
    mode === "view"
  );
}

type Box = { x0: number; y0: number; x1: number; y1: number };

type Props = {
  imageUrl: string;
  balloons: Balloon[];
  mode: Mode;
  selected: number | null;
  busy?: boolean;
  /** Draw balloons/arrows live on the image (moves instantly while editing). */
  liveDraw?: boolean;
  /** Maps unrotated PDF balloon coords ↔ preview image pixels (handles page rotation). */
  pageGeometry?: PageGeometry | null;
  onSelect: (no: number | null) => void;
  onCreate: (box: { x: number; y: number; width: number; height: number }) => void;
  /** Auto-detect dimensions inside drawn rectangle (area mode). */
  onArea?: (box: { x: number; y: number; width: number; height: number }) => void;
  /** Move balloon circle (bx/by) and keep dim tip (x/y) relative. */
  onMove: (no: number, patch: Partial<Balloon>) => void;
  onMoveStart?: () => void;
  onMoveEnd: () => void;
  onDelete: (no: number) => void;
  /** Balloons marked for batch delete (delete mode). */
  deleteSelected?: number[];
  onToggleDeleteSelect?: (no: number) => void;
  /** Persistent work area (PDF coords) — shown as green overlay. */
  workArea?: { x: number; y: number; width: number; height: number } | null;
};

function applyMat(m: number[] | undefined, x: number, y: number) {
  if (!m || m.length < 6) return { x, y };
  const [a, b, c, d, e, f] = m;
  return { x: a * x + c * y + e, y: b * x + d * y + f };
}

/** Circle centre in PDF space (leader balloon), falling back to offset from dim tip. */
function balloonCenterPdf(b: Balloon) {
  if (b.bx != null && b.by != null && Number.isFinite(b.bx) && Number.isFinite(b.by)) {
    return { x: b.bx, y: b.by };
  }
  const w = b.width || 20;
  const h = b.height || 12;
  const tipX = b.x + w / 2;
  const tipY = b.y + h / 2;
  // Match typical auto-balloon placement: short leader to the left of the dim
  return { x: tipX - Math.max(28, h * 3.2), y: tipY };
}

function tipCenterPdf(b: Balloon) {
  return { x: b.x + (b.width || 20) / 2, y: b.y + (b.height || 12) / 2 };
}

/** Leader arrow tip — custom tx/ty when set, else joined at dim centre. */
function arrowTipPdf(b: Balloon) {
  if (b.tx != null && b.ty != null && Number.isFinite(b.tx) && Number.isFinite(b.ty)) {
    return { x: b.tx, y: b.ty };
  }
  return tipCenterPdf(b);
}

function arrowScale(b: Balloon) {
  const s = b.arrow_size;
  return s != null && Number.isFinite(s) ? Math.min(2.5, Math.max(0.35, s)) : 1;
}

/** Edit mode: arrow tip is independent from dim box when tx/ty stored. */
function isDetachedEdit(b: Balloon, mode: Mode, selected: number | null) {
  return mode === "edit" && selected === b.balloon;
}

/**
 * PDF-reader style zoom/pan viewport + balloon create/edit/delete on canvas.
 * Balloon x/y are dim tip boxes; bx/by are circle centres (unrotated mediabox).
 * Preview PNG already has drawn balloons — overlay is selection/hit only (no duplicate numbers).
 */
export default function BalloonCanvas({
  imageUrl,
  balloons,
  mode,
  selected,
  busy,
  liveDraw,
  pageGeometry,
  onSelect,
  onCreate,
  onArea,
  onMove,
  onMoveStart,
  onMoveEnd,
  onDelete,
  deleteSelected = [],
  onToggleDeleteSelect,
  workArea = null,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [ready, setReady] = useState(0);
  const [zoomPct, setZoomPct] = useState(100);
  const [savingHint, setSavingHint] = useState(false);

  const zoomRef = useRef(1);
  const panRef = useRef({ x: 0, y: 0 });
  const spaceRef = useRef(false);
  const panningRef = useRef(false);
  const panStartRef = useRef({ x: 0, y: 0, panX: 0, panY: 0 });

  const drawingRef = useRef(false);
  const boxRef = useRef<Box | null>(null);
  const [liveBox, setLiveBox] = useState<Box | null>(null);
  const dragNoRef = useRef<number | null>(null);
  /** circle | arrow | dim — edit mode only; parts move independently */
  const dragPartRef = useRef<"circle" | "arrow" | "dim">("circle");
  const dragOriginRef = useRef<{
    ix: number;
    iy: number;
    bx?: number;
    by?: number;
    x?: number;
    y?: number;
    tx?: number;
    ty?: number;
  } | null>(null);
  /** Instant drag coords (avoids waiting on React setState to move circle/arrow). */
  const dragLiveRef = useRef<{
    no: number;
    bx?: number;
    by?: number;
    x?: number;
    y?: number;
    tx?: number;
    ty?: number;
    arrow_size?: number;
  } | null>(null);
  const movedRef = useRef(false);
  const fittedUrlRef = useRef("");

  const balloonsRef = useRef(balloons);
  balloonsRef.current = balloons;
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const deleteSelectedRef = useRef(deleteSelected);
  deleteSelectedRef.current = deleteSelected;
  const geomRef = useRef(pageGeometry);
  geomRef.current = pageGeometry;
  const workAreaRef = useRef(workArea);
  workAreaRef.current = workArea;
  const busyRef = useRef(!!busy);
  busyRef.current = !!busy;
  const { src, failed, loading } = useAuthImage(imageUrl);

  useEffect(() => {
    setSavingHint(!!busy);
  }, [busy]);

  const previewScale = () => {
    const img = imgRef.current;
    const g = geomRef.current;
    if (g && g.page_width > 0 && img?.naturalWidth) {
      return img.naturalWidth / g.page_width;
    }
    return g?.preview_scale || 1.35;
  };

  /** PDF unrotated point → image pixel (preview space). */
  const pdfPtToImg = (x: number, y: number) => {
    const scale = previewScale();
    const p = applyMat(geomRef.current?.rot, x, y);
    return { x: p.x * scale, y: p.y * scale };
  };

  const pdfBoxToImg = (x: number, y: number, w: number, h: number) => {
    const p0 = pdfPtToImg(x, y);
    const p1 = pdfPtToImg(x + w, y + h);
    const ix0 = Math.min(p0.x, p1.x);
    const iy0 = Math.min(p0.y, p1.y);
    const ix1 = Math.max(p0.x, p1.x);
    const iy1 = Math.max(p0.y, p1.y);
    return { x: ix0, y: iy0, w: Math.max(2, ix1 - ix0), h: Math.max(2, iy1 - iy0) };
  };

  /** Image pixel → PDF unrotated point. */
  const imgPxToPdf = (ix: number, iy: number) => {
    const scale = previewScale();
    const dx = ix / scale;
    const dy = iy / scale;
    return applyMat(geomRef.current?.derot, dx, dy);
  };

  useEffect(() => {
    if (!src) {
      if (failed) setReady((n) => n + 1);
      return;
    }
    const img = new Image();
    img.onload = () => {
      const prev = imgRef.current;
      imgRef.current = img;
      const sameSize =
        prev &&
        prev.naturalWidth === img.naturalWidth &&
        prev.naturalHeight === img.naturalHeight;
      const baseUrl = imageUrl.split("&v=")[0];
      // Fit only on first image / new drawing — keep zoom/pan across preview refreshes
      if (!prev || fittedUrlRef.current !== baseUrl || !sameSize) {
        if (!prev || fittedUrlRef.current !== baseUrl) {
          fitToView();
        } else {
          redraw();
        }
        fittedUrlRef.current = baseUrl;
      } else {
        redraw();
      }
      setReady((n) => n + 1);
    };
    img.onerror = () => {
      setReady((n) => n + 1);
    };
    img.src = src;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);

  useEffect(() => {
    const isTypingTarget = (t: EventTarget | null) => {
      if (!(t instanceof HTMLElement)) return false;
      const tag = t.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable;
    };
    const down = (e: KeyboardEvent) => {
      if (e.code === "Space") {
        if (isTypingTarget(e.target)) return;
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

    const m = modeRef.current;
    const showHandles = m === "edit" || m === "delete" || m === "create";

    const drawArrowHead = (
      tipX: number,
      tipY: number,
      fromX: number,
      fromY: number,
      size: number,
    ) => {
      const dx = tipX - fromX;
      const dy = tipY - fromY;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len;
      const uy = dy / len;
      const bx = tipX - ux * size;
      const by = tipY - uy * size;
      const px = -uy;
      const py = ux;
      ctx.beginPath();
      ctx.moveTo(tipX, tipY);
      ctx.lineTo(bx + px * size * 0.45, by + py * size * 0.45);
      ctx.lineTo(bx - px * size * 0.45, by - py * size * 0.45);
      ctx.closePath();
      ctx.fill();
    };

    // Live balloons on the clean input drawing — circle + leader move with drag.
    for (const raw of balloonsRef.current) {
      const live = dragLiveRef.current;
      const b =
        live && live.no === raw.balloon
          ? {
              ...raw,
              ...(live.bx != null ? { bx: live.bx } : {}),
              ...(live.by != null ? { by: live.by } : {}),
              ...(live.x != null ? { x: live.x } : {}),
              ...(live.y != null ? { y: live.y } : {}),
              ...(live.tx != null ? { tx: live.tx } : {}),
              ...(live.ty != null ? { ty: live.ty } : {}),
              ...(live.arrow_size != null ? { arrow_size: live.arrow_size } : {}),
            }
          : raw;
      const cPdf = balloonCenterPdf(b);
      const aPdf = arrowTipPdf(b);
      const cImg = pdfPtToImg(cPdf.x, cPdf.y);
      const aImg = pdfPtToImg(aPdf.x, aPdf.y);
      const cx = pan.x + cImg.x * z;
      const cy = pan.y + cImg.y * z;
      const ax = pan.x + aImg.x * z;
      const ay = pan.y + aImg.y * z;
      const active = selectedRef.current === b.balloon;
      const markedDelete =
        m === "delete" && deleteSelectedRef.current.includes(b.balloon);
      const detached = isDetachedEdit(b, m, selectedRef.current);
      const rad = Math.max(10, 11 * Math.min(Math.max(z, 0.55), 2.4));
      const stroke = active ? "#3d7eff" : markedDelete ? "#ff7043" : "#c62828";
      const fill = active
        ? "rgba(61,126,255,0.95)"
        : markedDelete
          ? "rgba(255,112,67,0.92)"
          : "rgba(255,247,191,0.95)";
      const aScale = arrowScale(b);

      if (liveDraw) {
        const dx = ax - cx;
        const dy = ay - cy;
        const dist = Math.hypot(dx, dy) || 1;
        const ux = dx / dist;
        const uy = dy / dist;
        const startX = cx + ux * (rad + 0.5);
        const startY = cy + uy * (rad + 0.5);
        const tipGap = Math.max(3, 4 * Math.min(z, 2));
        const tipX = ax - ux * tipGap;
        const tipY = ay - uy * tipGap;

        ctx.strokeStyle = stroke;
        ctx.fillStyle = stroke;
        ctx.lineWidth = Math.max(1.2, 1.4 * Math.min(z, 2));
        ctx.beginPath();
        ctx.moveTo(startX, startY);
        ctx.lineTo(tipX, tipY);
        ctx.stroke();
        drawArrowHead(tipX, tipY, startX, startY, Math.max(3, rad * 0.38 * aScale));

        ctx.beginPath();
        ctx.arc(cx, cy, rad, 0, Math.PI * 2);
        ctx.fillStyle = fill;
        ctx.fill();
        ctx.lineWidth = active ? 2.5 : 1.6;
        ctx.strokeStyle = stroke;
        ctx.stroke();

        ctx.fillStyle = active ? "#fff" : "#b71c1c";
        ctx.font = `bold ${Math.max(9, 10 * Math.min(z, 1.9))}px Segoe UI, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(String(b.balloon), cx, cy + 0.5);

        if (detached) {
          const tip = pdfBoxToImg(b.x, b.y, b.width || 20, b.height || 12);
          ctx.save();
          ctx.strokeStyle = "rgba(61,126,255,0.85)";
          ctx.lineWidth = 1.5;
          ctx.setLineDash([5, 4]);
          ctx.strokeRect(
            pan.x + tip.x * z,
            pan.y + tip.y * z,
            Math.max(4, tip.w * z),
            Math.max(4, tip.h * z),
          );
          ctx.restore();
          const hs = Math.max(5, 6 * Math.min(z, 1.8));
          ctx.fillStyle = "#ff9800";
          ctx.fillRect(ax - hs, ay - hs, hs * 2, hs * 2);
          ctx.strokeStyle = "#fff";
          ctx.lineWidth = 1;
          ctx.strokeRect(ax - hs, ay - hs, hs * 2, hs * 2);
        }
      } else if (active || showHandles) {
        ctx.beginPath();
        ctx.arc(cx, cy, rad, 0, Math.PI * 2);
        if (active) {
          ctx.fillStyle = "rgba(61,126,255,0.22)";
          ctx.fill();
          ctx.lineWidth = 2.5;
          ctx.strokeStyle = "#3d7eff";
        } else if (m === "delete") {
          ctx.strokeStyle = markedDelete ? "rgba(255,112,67,0.85)" : "rgba(229,57,53,0.55)";
          ctx.lineWidth = markedDelete ? 2.2 : 1.5;
        } else {
          ctx.strokeStyle = "rgba(61,126,255,0.35)";
          ctx.lineWidth = 1.25;
        }
        ctx.stroke();
      }

      if (active && showHandles && !liveDraw) {
        const tip = pdfBoxToImg(b.x, b.y, b.width || 20, b.height || 12);
        ctx.save();
        ctx.strokeStyle = "rgba(61,126,255,0.7)";
        ctx.lineWidth = 1.25;
        ctx.setLineDash([4, 3]);
        ctx.strokeRect(
          pan.x + tip.x * z,
          pan.y + tip.y * z,
          Math.max(4, tip.w * z),
          Math.max(4, tip.h * z),
        );
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
      ctx.strokeStyle = "#b71c1c";
      ctx.fillStyle = "rgba(183, 28, 28, 0.22)";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
    }

    const wa = workAreaRef.current;
    if (wa && wa.width > 0 && wa.height > 0) {
      const r = pdfBoxToImg(wa.x, wa.y, wa.width, wa.height);
      const x = pan.x + r.x * z;
      const y = pan.y + r.y * z;
      const w = r.w * z;
      const h = r.h * z;
      ctx.save();
      ctx.strokeStyle = "#2e7d32";
      ctx.fillStyle = "rgba(46, 125, 50, 0.22)";
      ctx.lineWidth = 2.5;
      ctx.setLineDash([8, 4]);
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      ctx.font = `600 ${Math.max(10, 11 * Math.min(z, 1.8))}px Segoe UI, sans-serif`;
      ctx.fillStyle = "#1b5e20";
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.fillText("Work area — Create balloons inside", x + 6, y + 6);
      ctx.restore();
    }
  };

  useEffect(() => {
    redraw();
    const onResize = () => redraw();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, balloons, selected, deleteSelected, liveBox, mode, imageUrl, zoomPct, pageGeometry, workArea]);

  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const onWheelNative = (e: WheelEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const rect = el.getBoundingClientRect();
      // Edit + selected: wheel resizes arrowhead; otherwise zoom viewport
      if (modeRef.current === "edit" && selectedRef.current != null && !spaceRef.current) {
        const no = selectedRef.current;
        const b = balloonsRef.current.find((x) => x.balloon === no);
        if (b) {
          const cur = arrowScale(b);
          const next = Math.min(2.5, Math.max(0.35, cur * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
          onMove(no, { arrow_size: next });
          dragLiveRef.current = { no, arrow_size: next };
          redraw();
          return;
        }
      }
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      setZoomAround(zoomRef.current * factor, e.clientX - rect.left, e.clientY - rect.top);
    };
    el.addEventListener("wheel", onWheelNative, { passive: false });
    return () => el.removeEventListener("wheel", onWheelNative);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, imageUrl]);

  const hitBalloon = (ix: number, iy: number): { b: Balloon; part: "circle" | "arrow" | "dim" } | null => {
    const z = zoomRef.current;
    const hitRImg = 22 / Math.max(z, 0.25);
    const arrowHitR = 18 / Math.max(z, 0.25);
    let best: { b: Balloon; part: "circle" | "arrow" | "dim"; d: number } | null = null;
    const m = modeRef.current;
    for (const b of balloonsRef.current) {
      const cPdf = balloonCenterPdf(b);
      const c = pdfPtToImg(cPdf.x, cPdf.y);
      const dCircle = Math.hypot(c.x - ix, c.y - iy);
      const aPdf = arrowTipPdf(b);
      const a = pdfPtToImg(aPdf.x, aPdf.y);
      const dArrow = Math.hypot(a.x - ix, a.y - iy);
      const tip = pdfBoxToImg(b.x, b.y, b.width || 20, b.height || 12);
      const insideTip =
        ix >= tip.x - 6 &&
        iy >= tip.y - 6 &&
        ix <= tip.x + tip.w + 6 &&
        iy <= tip.y + tip.h + 6;

      if (m === "edit" && dArrow <= arrowHitR) {
        const cand = { b, part: "arrow" as const, d: dArrow };
        if (!best || cand.d < best.d) best = cand;
      } else if (dCircle <= hitRImg) {
        const cand = { b, part: "circle" as const, d: dCircle };
        if (!best || cand.d < best.d) best = cand;
      } else if (insideTip) {
        const cx = tip.x + tip.w / 2;
        const cy = tip.y + tip.h / 2;
        const cand = { b, part: "dim" as const, d: Math.hypot(cx - ix, cy - iy) };
        if (!best || cand.d < best.d) best = cand;
      }
    }
    return best ? { b: best.b, part: best.part } : null;
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    // Allow pan even while a background save runs; only block create/edit/delete writes
    const pt = clientToImg(e.clientX, e.clientY);
    if (!pt) return;
    canvasRef.current?.setPointerCapture(e.pointerId);

    const m = modeRef.current;
    if (shouldPan(e, m, spaceRef.current)) {
      panningRef.current = true;
      panStartRef.current = { x: e.clientX, y: e.clientY, panX: panRef.current.x, panY: panRef.current.y };
      return;
    }
    if (e.button !== 0) return;
    if (busyRef.current && (m === "create" || m === "area")) return;

    if (m === "create" || m === "area") {
      drawingRef.current = true;
      const box = { x0: pt.ix, y0: pt.iy, x1: pt.ix, y1: pt.iy };
      boxRef.current = box;
      setLiveBox(box);
      return;
    }
    const hit = hitBalloon(pt.ix, pt.iy);
    if (m === "delete") {
      if (hit) {
        if (e.ctrlKey || e.metaKey || e.shiftKey) {
          onToggleDeleteSelect?.(hit.b.balloon);
        } else {
          onDelete(hit.b.balloon);
        }
      }
      return;
    }
    if (m === "edit") {
      if (hit) {
        onSelect(hit.b.balloon);
        dragNoRef.current = hit.b.balloon;
        dragPartRef.current = hit.part;
        movedRef.current = false;
        const cPdf = balloonCenterPdf(hit.b);
        const aPdf = arrowTipPdf(hit.b);
        const init: Partial<Balloon> = {};
        if (hit.b.bx == null || hit.b.by == null) {
          init.bx = cPdf.x;
          init.by = cPdf.y;
        }
        if (hit.b.tx == null || hit.b.ty == null) {
          init.tx = aPdf.x;
          init.ty = aPdf.y;
        }
        if (Object.keys(init).length) {
          onMove(hit.b.balloon, init);
        }
        dragOriginRef.current = {
          ix: pt.ix,
          iy: pt.iy,
          bx: hit.b.bx ?? cPdf.x,
          by: hit.b.by ?? cPdf.y,
          x: hit.b.x,
          y: hit.b.y,
          tx: hit.b.tx ?? aPdf.x,
          ty: hit.b.ty ?? aPdf.y,
        };
        onMoveStart?.();
      } else {
        onSelect(null);
        dragNoRef.current = null;
        dragOriginRef.current = null;
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
    const pt = clientToImg(e.clientX, e.clientY);
    if (!pt) return;

    if ((modeRef.current === "create" || modeRef.current === "area") && drawingRef.current && boxRef.current) {
      const box = { ...boxRef.current, x1: pt.ix, y1: pt.iy };
      boxRef.current = box;
      setLiveBox(box);
      return;
    }

    if (modeRef.current === "edit" && dragNoRef.current != null && dragOriginRef.current && (e.buttons & 1) === 1) {
      const origin = dragOriginRef.current;
      const p0 = imgPxToPdf(origin.ix, origin.iy);
      const p1 = imgPxToPdf(pt.ix, pt.iy);
      const ddx = p1.x - p0.x;
      const ddy = p1.y - p0.y;
      movedRef.current = true;
      const part = dragPartRef.current;
      let patch: Partial<Balloon>;
      if (part === "dim") {
        patch = { x: (origin.x ?? 0) + ddx, y: (origin.y ?? 0) + ddy };
      } else if (part === "arrow") {
        patch = { tx: (origin.tx ?? 0) + ddx, ty: (origin.ty ?? 0) + ddy };
      } else {
        patch = { bx: (origin.bx ?? 0) + ddx, by: (origin.by ?? 0) + ddy };
      }
      dragLiveRef.current = { no: dragNoRef.current, ...patch };
      redraw();
      onMove(dragNoRef.current, patch);
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

    if (modeRef.current === "area" && drawingRef.current) {
      drawingRef.current = false;
      const box = boxRef.current;
      boxRef.current = null;
      setLiveBox(null);
      if (!box || !onArea) return;
      let x0 = Math.min(box.x0, box.x1);
      let y0 = Math.min(box.y0, box.y1);
      let x1 = Math.max(box.x0, box.x1);
      let y1 = Math.max(box.y0, box.y1);
      if (x1 - x0 < 8 && y1 - y0 < 8) return;
      const p0 = imgPxToPdf(x0, y0);
      const p1 = imgPxToPdf(x1, y1);
      onArea({
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
      dragOriginRef.current = null;
      dragLiveRef.current = null;
      onMoveEnd();
    } else {
      dragNoRef.current = null;
      dragOriginRef.current = null;
      dragLiveRef.current = null;
    }
  };

  const cursor =
    mode === "create" || mode === "area"
      ? "crosshair"
      : panningRef.current || mode === "view"
        ? "grab"
        : "pointer";

  const hasImage = !!imgRef.current;

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
        {savingHint ? <span className="muted viewer-hint saving-hint">Saving…</span> : null}
      </div>
      <div ref={wrapRef} className={`panel-body canvas-wrap viewer-body ${mode === "create" || mode === "area" ? "drawing" : ""}`}>
        {!hasImage && (loading || ready === 0) && !failed ? (
          <div className="placeholder">Loading preview…</div>
        ) : !hasImage || failed ? (
          <div className="placeholder">Preview unavailable.</div>
        ) : (
          <>
            {loading ? (
              <div className="placeholder viewer-loading-overlay">Loading page…</div>
            ) : null}
            <canvas
            ref={canvasRef}
            className="balloon-canvas viewer-canvas"
            style={{ cursor, touchAction: "none" }}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onContextMenu={(e) => e.preventDefault()}
          />
          </>
        )}
      </div>
    </div>
  );
}
