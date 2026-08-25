import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { useAuthImage } from "../hooks/useAuthImage";

type Props = {
  imageUrl: string;
};

/** PDF-reader style zoom/pan viewer for the input drawing (view only). */
export default function ZoomImageViewer({ imageUrl }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const [ready, setReady] = useState(0);
  const [zoomPct, setZoomPct] = useState(100);
  const zoomRef = useRef(1);
  const panRef = useRef({ x: 0, y: 0 });
  const panningRef = useRef(false);
  const panStartRef = useRef({ x: 0, y: 0, panX: 0, panY: 0 });
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

  useEffect(() => {
    redraw();
    const onResize = () => redraw();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, imageUrl, zoomPct]);

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

  const onPointerDown = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    canvasRef.current?.setPointerCapture(e.pointerId);
    panningRef.current = true;
    panStartRef.current = { x: e.clientX, y: e.clientY, panX: panRef.current.x, panY: panRef.current.y };
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!panningRef.current) return;
    panRef.current = {
      x: panStartRef.current.panX + (e.clientX - panStartRef.current.x),
      y: panStartRef.current.panY + (e.clientY - panStartRef.current.y),
    };
    redraw();
  };
  const onPointerUp = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    panningRef.current = false;
    try {
      canvasRef.current?.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
  };

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
      </div>
      <div ref={wrapRef} className="panel-body canvas-wrap viewer-body">
        {loading || (!imgRef.current && ready === 0 && !failed) ? (
          <div className="placeholder">Loading…</div>
        ) : !imgRef.current || failed ? (
          <div className="placeholder">Preview unavailable.</div>
        ) : (
          <canvas
            ref={canvasRef}
            className="balloon-canvas viewer-canvas"
            style={{ cursor: "grab", touchAction: "none" }}
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
