import { useEffect, useRef, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import {
  api,
  measureStatus,
  type Balloon,
  type Job,
  type MeasureCol,
  type MissingDim,
  type ReportRow,
} from "../api";
import { getJobSession, setJobSession, useAuth } from "../auth";
import BalloonCanvas, { type Mode } from "../components/BalloonCanvas";
import ZoomImageViewer from "../components/ZoomImageViewer";
import {
  INSTRUMENT_OPTIONS,
  addInstrumentId,
  allInstrumentIds,
  defaultUnitForInstrument,
  instrumentIdOptions,
  renameInstrumentId,
  unitOptionsForInstrument,
} from "../constants/instruments";
import { APP_TITLE } from "../constants/branding";

export default function InspectionPage() {
  const { user, logout, clearJob } = useAuth();
  // Session-only: closing EXE / browser clears previous drawing + report
  const [jobId, setJobId] = useState<string | null>(() => getJobSession());
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [ballooning, setBallooning] = useState(false);
  const [err, setErr] = useState("");
  const [mode, setMode] = useState<Mode>("view");
  const [selected, setSelected] = useState<number | null>(null);
  const [deleteSelected, setDeleteSelected] = useState<number[]>([]);
  const [previewRev, setPreviewRev] = useState(0);
  const [undoCount, setUndoCount] = useState(0);
  const [showReport, setShowReport] = useState(false);
  const [missing, setMissing] = useState<MissingDim[]>([]);
  const [auditMsg, setAuditMsg] = useState("");
  const [viewPage, setViewPage] = useState(0); // 0-based page in multi-page PDF
  /** After ballooning, hide input panel so output can use full width for edit/zoom. */
  const [showInput, setShowInput] = useState(true);
  const balloonsRef = useRef<Balloon[]>([]);
  const undoStackRef = useRef<Balloon[][]>([]);
  const moveUndoPushedRef = useRef(false);
  const balloonsDirtyRef = useRef(false);
  const syncQueueRef = useRef(Promise.resolve());
  const createQueueRef = useRef(Promise.resolve());
  const pageGeomCacheRef = useRef<Map<number, import("../api").PageGeometry>>(new Map());
  const undoHandlerRef = useRef<() => void>(() => {});
  const modeRef = useRef(mode);
  modeRef.current = mode;
  const deleteSelectedRef = useRef(deleteSelected);
  deleteSelectedRef.current = deleteSelected;
  const doDeleteManyRef = useRef<(nos: number[]) => void>(() => {});
  const [unsavedEdits, setUnsavedEdits] = useState(false);

  const pushUndo = () => {
    const snap = balloonsRef.current.map((b) => ({ ...b }));
    undoStackRef.current = [...undoStackRef.current.slice(-24), snap];
    setUndoCount(undoStackRef.current.length);
  };

  const bumpPreview = () => setPreviewRev((r) => r + 1);

  const markBalloonsDirty = () => {
    balloonsDirtyRef.current = true;
    setUnsavedEdits(true);
  };

  const clearBalloonsDirty = () => {
    balloonsDirtyRef.current = false;
    setUnsavedEdits(false);
  };

  const renumberBalloonsCompact = (balloons: Balloon[]): Balloon[] =>
    [...balloons]
      .sort((a, b) => a.balloon - b.balloon)
      .map((b, i) => ({ ...b, balloon: i + 1 }));

  const queueBalloonSync = () => {
    if (!jobId) return;
    syncQueueRef.current = syncQueueRef.current.then(async () => {
      try {
        const res = await api.saveBalloons(jobId, balloonsRef.current, { redrawPdf: false });
        if (res?.balloons?.length != null) {
          balloonsRef.current = res.balloons;
          setJob((j) =>
            j
              ? {
                  ...j,
                  balloons: res.balloons,
                  report_rows: res.report_rows?.length ? res.report_rows : j.report_rows,
                }
              : j,
          );
        } else if (res?.report_rows?.length) {
          setJob((j) => (j ? { ...j, report_rows: res.report_rows } : j));
        }
        clearBalloonsDirty();
        if (showReport && !res?.report_rows?.length) {
          const j = await api.getJob(jobId, viewPage);
          setJob((prev) => (prev ? { ...prev, report_rows: j.report_rows } : prev));
        }
      } catch (ex: any) {
        setErr(ex.message || "Failed to sync balloon changes");
      }
    });
  };

  const toggleDeleteSelect = (no: number) => {
    setDeleteSelected((prev) =>
      prev.includes(no) ? prev.filter((n) => n !== no) : [...prev, no],
    );
  };

  const selectAllPageForDelete = () => {
    const page = viewPage + 1;
    setDeleteSelected(
      balloonsRef.current.filter((b) => (b.page || 1) === page).map((b) => b.balloon),
    );
  };

  /** Join arrow tip to dim centre (Save). Skip join when flushing before other actions. */
  const balloonsPayload = (join: boolean): Balloon[] => {
    const src = balloonsRef.current;
    if (!join) return src.map((b) => ({ ...b }));
    return src.map((b) => ({
      ...b,
      tx: b.x + (b.width || 20) / 2,
      ty: b.y + (b.height || 12) / 2,
    }));
  };

  const persistBalloons = async (opts?: {
    join?: boolean;
    bumpPreview?: boolean;
    reportHint?: boolean;
    /** When true (default), skip network call if nothing changed. */
    skipIfClean?: boolean;
    /** When false, skip slow PDF overlay (use before create / drag flush). */
    redrawPdf?: boolean;
  }) => {
    if (!jobId) return;
    if (opts?.skipIfClean !== false && !balloonsDirtyRef.current) return;
    setErr("");
    const payload = balloonsPayload(!!opts?.join);
    balloonsRef.current = payload;
    setJob((j) => (j ? { ...j, balloons: payload } : j));
    const res = await api.saveBalloons(jobId, payload, {
      redrawPdf: opts?.redrawPdf,
    });
    if (res?.balloons?.length) {
      balloonsRef.current = res.balloons;
      setJob((j) =>
        j
          ? {
              ...j,
              balloons: res.balloons,
              report_rows: res.report_rows?.length ? res.report_rows : j.report_rows,
            }
          : j,
      );
    } else if (res?.report_rows?.length) {
      setJob((j) => (j ? { ...j, report_rows: res.report_rows } : j));
    }
    clearBalloonsDirty();
    if (opts?.bumpPreview) bumpPreview();
    if (opts?.reportHint && showReport) {
      const n = res?.report_rows?.length ?? 0;
      if (n) setAuditMsg(`Inspection report updated (${n} rows).`);
    }
  };

  /** Save pending balloon edits before another action (no arrow rejoin). */
  const flushUnsavedEdits = async () => {
    await persistBalloons({ join: false, skipIfClean: true, redrawPdf: false });
  };

  const refresh = async (id = jobId, opts?: { bumpPreview?: boolean; reportHint?: boolean; page?: number }) => {
    if (!id) return;
    const j = await api.getJob(id, opts?.page ?? viewPage);
    setJob(j);
    balloonsRef.current = j.balloons || [];
    if (j.missing_dimensions) setMissing(j.missing_dimensions);
    if (opts?.bumpPreview) bumpPreview();
    if (opts?.reportHint && showReport) {
      setAuditMsg(`Inspection report updated (${(j.report_rows || []).length} rows).`);
    }
  };

  const resetLocalJob = async () => {
    if (jobId && balloonsDirtyRef.current) {
      try {
        await flushUnsavedEdits();
      } catch (e: any) {
        setErr(e.message || "Could not save balloon edits");
        return;
      }
    }
    clearJob();
    setJobId(null);
    setJob(null);
    setMissing([]);
    setAuditMsg("");
    setShowReport(false);
    setSelected(null);
    setMode("view");
    setErr("");
    setViewPage(0);
    setPreviewRev(0);
    setShowInput(true);
    undoStackRef.current = [];
    setUndoCount(0);
    clearBalloonsDirty();
  };

  useEffect(() => {
    if (jobId) refresh(jobId).catch((e) => setErr(e.message));
  }, [jobId]);

  useEffect(() => {
    if (!jobId) return;
    const pc = Math.max(1, Number(job?.page_count) || 1);
    if (pc <= 1) return;
    const neighbors = [viewPage - 1, viewPage + 1].filter((p) => p >= 0 && p < pc);
    for (const p of neighbors) {
      const url = api.previewInputUrl(jobId, p, previewRev);
      void api.authFetch(url).catch(() => {});
    }
  }, [jobId, viewPage, job?.page_count, previewRev]);

  useEffect(() => {
    const geom = job?.page_geometry;
    if (geom) pageGeomCacheRef.current.set(viewPage, geom);
  }, [job?.page_geometry, viewPage]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "z" && !e.shiftKey) {
        e.preventDefault();
        undoHandlerRef.current();
        return;
      }
      if (modeRef.current !== "delete") return;
      if (e.key !== "Delete" && e.key !== "Backspace") return;
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      const sel = deleteSelectedRef.current;
      if (sel.length === 0) return;
      e.preventDefault();
      doDeleteManyRef.current([...sel]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!user) return <Navigate to="/login" replace />;

  const balloons = job?.balloons || [];
  balloonsRef.current = balloons;
  const pageCount = Math.max(1, Number(job?.page_count) || 1);
  const pageBalloons = balloons.filter((b) => (b.page || 1) === viewPage + 1);
  const hasBalloonOutput = job?.status === "ballooned" || balloons.length > 0;
  const focusOutput = hasBalloonOutput && !showInput;
  const inputUrl = jobId ? api.previewInputUrl(jobId, viewPage, previewRev) : "";

  const changePage = (next: number) => {
    const p = Math.max(0, Math.min(pageCount - 1, next));
    setViewPage(p);
    setSelected(null);
    setDeleteSelected([]);
    const cached = pageGeomCacheRef.current.get(p);
    if (cached) {
      setJob((j) => (j ? { ...j, page_geometry: cached } : j));
    }
    if (!jobId) return;
    void (async () => {
      try {
        const geom = await api.getPageGeometry(jobId, p);
        pageGeomCacheRef.current.set(p, geom);
        setJob((j) => (j ? { ...j, page_geometry: geom } : j));
      } catch {
        /* keep previous geometry until retry */
      }
    })();
    void flushUnsavedEdits().catch((e: any) => setErr(e.message));
  };
  const onUpload = async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    setErr("");
    setAuditMsg("Uploading drawing…");
    // New drawing → wipe previous balloons / report / audit UI first
    clearJob();
    setJobId(null);
    setJob(null);
    balloonsRef.current = [];
    setMissing([]);
    setShowReport(false);
    setSelected(null);
    setMode("view");
    setViewPage(0);
    setPreviewRev(0);
    setShowInput(true);
    undoStackRef.current = [];
    setUndoCount(0);
    try {
      const res = await api.uploadPdfs(files);
      setJobSession(res.job_id);
      setJobId(res.job_id);
      await refresh(res.job_id, { bumpPreview: true });
      const n = files.length;
      const pages = res.page_count || 1;
      setAuditMsg(
        res.message ||
          (n > 1
            ? `Combined ${n} files (${pages} pages). Click “Run auto ballooning”.`
            : "Upload done. Click “Run auto ballooning” to process the drawing."),
      );
    } catch (e: any) {
      setErr(e.message);
      setAuditMsg("");
    } finally {
      setUploading(false);
    }
  };

  const onBalloon = async () => {
    if (!jobId) return;
    setBusy(true);
    setBallooning(true);
    setErr("");
    setAuditMsg("Auto ballooning running......");
    try {
      await flushUnsavedEdits();
      const res = await api.runBalloon(jobId, 0);
      await refresh(jobId, { bumpPreview: true, reportHint: showReport });
      setMode("edit");
      setShowInput(false);
      setAuditMsg(`Ballooning done: ${res.count} balloons${res.page_count && res.page_count > 1 ? ` across ${res.page_count} pages` : ""}. Use full-width output to edit — “Show input” brings the original back.`);
    } catch (e: any) {
      setErr(e.message || "Auto ballooning failed — try again");
      setAuditMsg("");
    } finally {
      setBallooning(false);
      setBusy(false);
    }
  };

  const onAudit = async () => {
    if (!jobId) {
      setErr("Upload and run auto ballooning first.");
      return;
    }
    setBusy(true);
    setErr("");
    setAuditMsg("Cross-checking missing dimensions…");
    try {
      await flushUnsavedEdits();
      const res = await api.auditCrosscheck(jobId, 0);
      setMissing(res.missing || []);
      await refresh(jobId, { bumpPreview: true, reportHint: showReport });
      setAuditMsg(
        `Cross-check done: +${res.added} recovered · ${res.count} total balloons · ${res.missing_count} still missing`,
      );
      setMode("view");
    } catch (e: any) {
      setErr(e.message || "Cross-check failed");
      setAuditMsg("");
    } finally {
      setBusy(false);
    }
  };

  const addOneMissing = async (item: MissingDim) => {
    if (!jobId) return;
    setBusy(true);
    setErr("");
    try {
      await flushUnsavedEdits();
      const res = await api.addMissingDims(jobId, [item]);
      setMissing(res.missing || []);
      await refresh(jobId, { bumpPreview: true, reportHint: showReport });
      setAuditMsg(`Added “${item.text}”. ${res.missing_count} missing left.`);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const addAllMissing = async () => {
    if (!jobId || !missing.length) return;
    if (!window.confirm(`Add all ${missing.length} missing dimensions as balloons?`)) return;
    setBusy(true);
    setErr("");
    try {
      await flushUnsavedEdits();
      const res = await api.addMissingDims(jobId, missing);
      setMissing(res.missing || []);
      await refresh(jobId, { bumpPreview: true, reportHint: showReport });
      setAuditMsg(`Added missing dimensions. ${res.count} balloons total.`);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const download = async (kind: "ballooned" | "report" | "excel" | "source"): Promise<string> => {
    if (!jobId) {
      const msg = "No job loaded — upload a drawing first.";
      setErr(msg);
      throw new Error(msg);
    }
    setErr("");
    setBusy(true);
    setAuditMsg("Downloading…");
    try {
      await flushUnsavedEdits();
      if (kind === "report" && (!job?.report_rows || job.report_rows.length === 0)) {
        if (job?.balloons?.length) {
          await api.rebuildReport(jobId);
          await refresh(jobId);
        }
      }
      const url =
        kind === "ballooned"
          ? api.downloadBallooned(jobId)
          : kind === "source"
            ? api.downloadSourceUrl(jobId)
            : kind === "report"
              ? api.downloadReportPdf(jobId)
              : api.downloadExcel(jobId);
      const srcName = job?.files?.[0]?.name || "";
      const stem = (srcName.replace(/\.[^.]+$/, "") || jobId).replace(/[<>:"|?*\\/]/g, "_");
      const filename =
        kind === "ballooned"
          ? `${stem}_ballooned.pdf`
          : kind === "source"
            ? srcName || `${jobId}_input.pdf`
            : kind === "report"
              ? `${stem}_inspection_report.pdf`
              : `${stem}_inspection_report.xlsx`;
      const savedPath = await api.downloadFile(url, filename);
      const label =
        kind === "ballooned"
          ? "Ballooned PDF"
          : kind === "source"
            ? "Input file"
            : kind === "report"
              ? "Inspection report PDF"
              : "Excel report";
      setAuditMsg(
        savedPath.includes("\\") || savedPath.includes("/")
          ? `${label} saved to: ${savedPath}`
          : `${label} downloaded — check your Downloads folder.`,
      );
      return savedPath;
    } catch (e: any) {
      setAuditMsg("");
      const msg = e.message || "Download failed";
      setErr(msg);
      throw e;
    } finally {
      setBusy(false);
    }
  };

  const openReport = async () => {
    if (showReport) {
      setShowReport(false);
      setErr("");
      return;
    }
    if (!jobId) {
      setErr("Upload a drawing and run ballooning before opening the report.");
      return;
    }
    setErr("");
    if (!showReport) {
      try {
        await flushUnsavedEdits();
      } catch (e: any) {
        setErr(e.message);
        return;
      }
    }
    // Open report immediately; refresh grid references in background (do not block UI).
    setShowReport(true);
    if (job?.balloons?.length) {
      void (async () => {
        try {
          const res = await api.rebuildReport(jobId);
          if (res?.rows?.length) {
            setJob((j) => (j ? { ...j, report_rows: res.rows } : j));
          }
        } catch (e: any) {
          if (!job?.report_rows?.length) {
            setJob((j) => {
              if (!j) return j;
              const rows = (j.balloons || []).map((b, i) => ({
                sno: i + 1,
                balloon: b.balloon,
                reference: `P${b.page || 1}`,
                nominal: b.text || "",
                tol_low: "",
                tol_high: "",
                instrument: "",
                unit: "mm",
                instr_id: "",
                measured: ["", "", ""],
                remarks: "",
              }));
              return { ...j, report_rows: rows };
            });
          }
          console.warn("Report rebuild failed:", e?.message || e);
        }
      })();
    } else {
      setErr("No balloons yet — run auto ballooning or create balloons first.");
    }
  };

  const handleLogout = () => {
    void resetLocalJob().then(() => logout());
  };

  const onCreateBox = (box: { x: number; y: number; width: number; height: number }) => {
    if (!jobId) return;
    const nextNo =
      Math.max(0, ...(balloonsRef.current.map((b) => Number(b.balloon) || 0))) + 1;
    const w = box.width || 24;
    const h = box.height || 14;
    const tipX = box.x + w / 2;
    const tipY = box.y + h / 2;
    const optimistic: Balloon = {
      balloon: nextNo,
      page: viewPage + 1,
      text: "",
      x: box.x,
      y: box.y,
      width: w,
      height: h,
      bx: tipX - Math.max(28, h * 3.2),
      by: tipY,
      category: "manual",
      pattern: "manual",
    };
    setErr("");
    pushUndo();
    const next = [...balloonsRef.current, optimistic];
    balloonsRef.current = next;
    setJob((j) => (j ? { ...j, balloons: next, status: "ballooned" } : j));
    setMode("edit");
    setSelected(nextNo);
    setAuditMsg(`Balloon #${nextNo} created — click Save to update PDF.`);

    createQueueRef.current = createQueueRef.current.then(async () => {
      try {
        const withoutPending = balloonsRef.current.filter((b) => b.balloon !== nextNo);
        if (balloonsDirtyRef.current) {
          balloonsRef.current = withoutPending;
          await persistBalloons({ join: false, skipIfClean: true, redrawPdf: false });
          balloonsRef.current = [...balloonsRef.current, optimistic];
          setJob((j) => (j ? { ...j, balloons: balloonsRef.current } : j));
        }
        const res = await api.createBalloon(jobId, {
          page: viewPage + 1,
          text: "",
          ...box,
          width: w,
          height: h,
        });
        if (res?.balloons?.length) {
          balloonsRef.current = res.balloons;
          setJob((j) =>
            j
              ? {
                  ...j,
                  balloons: res.balloons,
                  status: "ballooned",
                  report_rows: res.report_rows?.length ? res.report_rows : j.report_rows,
                }
              : j,
          );
          clearBalloonsDirty();
          if (showReport && res.report_rows?.length) {
            setAuditMsg(`Balloon #${nextNo} created — inspection report updated.`);
          }
        }
      } catch (ex: any) {
        balloonsRef.current = balloonsRef.current.filter((b) => b.balloon !== nextNo);
        setJob((j) => (j ? { ...j, balloons: balloonsRef.current } : j));
        setSelected(null);
        setErr(ex.message);
      }
    });
  };

  const onMoveBalloon = (no: number, patch: Partial<Balloon>) => {
    markBalloonsDirty();
    setJob((j) => {
      if (!j) return j;
      const next = j.balloons.map((b) => (b.balloon === no ? { ...b, ...patch } : b));
      balloonsRef.current = next;
      return { ...j, balloons: next };
    });
  };

  const onMoveStart = () => {
    if (!moveUndoPushedRef.current) {
      pushUndo();
      moveUndoPushedRef.current = true;
    }
  };

  const onMoveEnd = () => {
    moveUndoPushedRef.current = false;
  };

  const doDeleteMany = (nos: number[]) => {
    if (!jobId || nos.length === 0) return;
    pushUndo();
    const remove = new Set(nos);
    const next = renumberBalloonsCompact(balloonsRef.current.filter((b) => !remove.has(b.balloon)));
    balloonsRef.current = next;
    setJob((j) => (j ? { ...j, balloons: next } : j));
    setSelected(null);
    setDeleteSelected([]);
    setAuditMsg(
      nos.length === 1 ? `Balloon #${nos[0]} deleted.` : `${nos.length} balloons deleted.`,
    );
    queueBalloonSync();
  };

  const doDelete = (balloonNo: number) => doDeleteMany([balloonNo]);

  const doDeleteSelected = () => doDeleteMany([...deleteSelected]);
  doDeleteManyRef.current = doDeleteMany;

  const saveEdits = async () => {
    if (!jobId) return;
    setBusy(true);
    try {
      pushUndo();
      await persistBalloons({
        join: true,
        reportHint: showReport,
        skipIfClean: false,
        redrawPdf: false,
      });
      setMode("view");
      setSelected(null);
      setAuditMsg("Balloons saved — inspection report updated.");
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const doUndo = async () => {
    if (!jobId || busy || undoStackRef.current.length === 0) return;
    const prev = undoStackRef.current.pop();
    if (!prev) return;
    setUndoCount(undoStackRef.current.length);
    setErr("");
    balloonsRef.current = prev;
    setJob((j) => (j ? { ...j, balloons: prev } : j));
    setMode("view");
    setSelected(null);
    clearBalloonsDirty();
    void (async () => {
      try {
        const res = await api.saveBalloons(jobId, prev, { redrawPdf: false });
        if (res?.balloons?.length) {
          balloonsRef.current = res.balloons;
          setJob((j) =>
            j
              ? {
                  ...j,
                  balloons: res.balloons,
                  report_rows: res.report_rows?.length ? res.report_rows : j.report_rows,
                }
              : j,
          );
        }
        setAuditMsg("Undo — restored previous balloons and report.");
      } catch (e: any) {
        setErr(e.message);
      }
    })();
  };

  undoHandlerRef.current = () => {
    void doUndo();
  };

  return (
    <div className="app-shell">
      <div className="topbar">
        <div className="brand">
          <img src="/smorx-logo-full.png" alt="SmorX.ai" />
        </div>
        <div className="title">{APP_TITLE}</div>
        <div className="actions">
          {user.role === "super_admin" && (
            <Link className="btn btn-ghost btn-sm" to="/admin">
              Admin
            </Link>
          )}
          <button className="btn btn-ghost btn-sm" onClick={handleLogout}>
            Log out
          </button>
          <button className="btn btn-gold btn-sm" onClick={() => void openReport()}>
            {showReport ? "Close report" : "Inspection report"}
          </button>
          <Link className="btn btn-ghost btn-sm" to="/app">
            Go to Dashboard
          </Link>
        </div>
      </div>

      <div className="toolbar">
        <div className="field" style={{ margin: 0, minWidth: 280 }}>
          <label>Upload drawing(s) — multiple PDFs are combined</label>
          <input
            type="file"
            accept="application/pdf,.pdf,image/png,image/jpeg,image/webp,image/tiff,image/bmp,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp"
            multiple
            disabled={uploading || busy}
            onChange={(e) => {
              void onUpload(e.target.files);
              e.target.value = "";
            }}
          />
          {uploading && <span className="muted" style={{ display: "block", marginTop: 6 }}>Uploading…</span>}
        </div>
        <button
          className="btn btn-primary"
          style={{ width: "auto" }}
          disabled={busy || uploading || !jobId}
          onClick={onBalloon}
        >
          {busy ? (ballooning ? "Ballooning…" : "Please wait…") : "Run auto ballooning"}
        </button>
        <button
          className="btn btn-outline"
          style={{ width: "auto" }}
          disabled={busy || uploading || !jobId || !hasBalloonOutput}
          onClick={() => void onAudit()}
          title="Recover missed dims and list anything still missing"
        >
          Audit & Add Balloon
        </button>
        <button className="btn btn-ghost" disabled={busy || uploading} onClick={() => void resetLocalJob()}>
          New drawing
        </button>
        <button className="btn btn-ghost" disabled={!jobId || uploading || busy} onClick={() => download("excel")}>
          Download Excel
        </button>
      </div>

      {err && <div className="error mb">{err}</div>}
      {auditMsg && <div className="ok mb">{auditMsg}</div>}

      {missing.length > 0 && (
        <div className="card wide mb" style={{ width: "100%" }}>
          <div className="row-between mb">
            <div>
              <h3 style={{ margin: 0 }}>Missing dimensions ({missing.length})</h3>
              <p className="muted" style={{ margin: "4px 0 0" }}>
                Found by cross-check — not ballooned yet. Add one or all, then Save / Download.
              </p>
            </div>
            <div className="row">
              <button className="btn btn-primary btn-sm" style={{ width: "auto" }} disabled={busy} onClick={() => void addAllMissing()}>
                Add all missing
              </button>
              <button className="btn btn-ghost btn-sm" onClick={() => setMissing([])}>
                Hide list
              </button>
            </div>
          </div>
          <div className="missing-list">
            {missing.slice(0, 200).map((m) => (
              <div key={`${m.page}-${m.id}-${m.x}-${m.y}`} className="missing-item">
                <div className="missing-text">
                  <strong>{m.text}</strong>
                  <span className="muted">
                    {" "}
                    · P{m.page} · {m.zone || "drawing"} · {m.pattern || m.category || "dim"}
                  </span>
                </div>
                <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void addOneMissing(m)}>
                  Add balloon
                </button>
              </div>
            ))}
            {missing.length > 200 && (
              <p className="muted">Showing first 200 of {missing.length}. Use “Add all missing” for the rest.</p>
            )}
          </div>
        </div>
      )}

      <div className={`panels ${focusOutput ? "panels-focus" : ""}`}>
        {(showInput || !hasBalloonOutput) && (
        <div className="panel">
          <div className="panel-head">
            <h3>1 · Input file{pageCount > 1 ? ` · page ${viewPage + 1}/${pageCount}` : ""}</h3>
            <div className="row">
              {pageCount > 1 && (
                <>
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={viewPage <= 0}
                    onClick={() => changePage(viewPage - 1)}
                  >
                    Prev
                  </button>
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={viewPage >= pageCount - 1}
                    onClick={() => changePage(viewPage + 1)}
                  >
                    Next
                  </button>
                </>
              )}
              <button
                className="btn btn-ghost btn-sm"
                disabled={!jobId}
                onClick={() => void download("source")}
              >
                Download
              </button>
            </div>
          </div>
          {jobId ? (
            <ZoomImageViewer imageUrl={inputUrl} />
          ) : (
            <div className="panel-body">
              <div className="placeholder">No file yet.</div>
            </div>
          )}
        </div>
        )}

        <div className={`panel ${focusOutput ? "panel-focus" : ""}`}>
          <div className="panel-head">
            <h3>
              {focusOutput ? "Auto Ballooning" : "2 · Auto Ballooning"}
              {pageCount > 1 ? ` · page ${viewPage + 1}/${pageCount}` : ""}
              {hasBalloonOutput ? ` · ${pageBalloons.length} on page` : ""}
            </h3>
            <div className="row">
              {hasBalloonOutput && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => setShowInput((v) => !v)}
                  title={showInput ? "Hide input drawing — more room to edit balloons" : "Show original input drawing beside output"}
                >
                  {showInput ? "Hide input" : "Show input"}
                </button>
              )}
              {pageCount > 1 && (
                <>
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={viewPage <= 0}
                    onClick={() => changePage(viewPage - 1)}
                  >
                    Prev
                  </button>
                  <button
                    className="btn btn-ghost btn-sm"
                    disabled={viewPage >= pageCount - 1}
                    onClick={() => changePage(viewPage + 1)}
                  >
                    Next
                  </button>
                </>
              )}
              <ModeBtn
                active={mode === "view"}
                disabled={!hasBalloonOutput || busy}
                onClick={() => {
                  setMode("view");
                  setSelected(null);
                  setErr("");
                }}
                label="Pan"
              />
              <ModeBtn
                active={mode === "create"}
                disabled={!hasBalloonOutput || busy}
                onClick={() => {
                  setMode("create");
                  setSelected(null);
                  setErr("");
                }}
                label="Create"
              />
              <ModeBtn
                active={mode === "edit"}
                disabled={!hasBalloonOutput || busy}
                onClick={() => {
                  setMode("edit");
                  setErr("");
                }}
                label="Edit"
              />
              <ModeBtn
                active={mode === "delete"}
                disabled={!hasBalloonOutput || busy}
                onClick={() => {
                  setMode("delete");
                  setSelected(null);
                  setDeleteSelected([]);
                  setErr("");
                }}
                label="Delete"
              />
              {mode === "delete" && deleteSelected.length > 0 && (
                <>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    style={{ width: "auto" }}
                    onClick={() => doDeleteSelected()}
                  >
                    Delete selected ({deleteSelected.length})
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={() => setDeleteSelected([])}
                  >
                    Clear selection
                  </button>
                </>
              )}
              {mode === "delete" && pageBalloons.length > 0 && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => selectAllPageForDelete()}
                >
                  Select all on page
                </button>
              )}
              <button
                className="btn btn-ghost btn-sm"
                disabled={busy || !hasBalloonOutput || undoCount === 0}
                onClick={() => void doUndo()}
                title="Undo last balloon change (Ctrl+Z)"
              >
                Undo
              </button>
              <button
                className="btn btn-primary btn-sm"
                style={{ width: "auto" }}
                onClick={() => void saveEdits()}
                disabled={busy || !hasBalloonOutput}
                title={unsavedEdits ? "You have unsaved balloon edits" : "Save balloon positions to PDF and report"}
              >
                {unsavedEdits ? "Save *" : "Save"}
              </button>
              <button
                className="btn btn-ghost btn-sm"
                disabled={busy || !hasBalloonOutput}
                onClick={() => void download("ballooned")}
              >
                {busy ? "Downloading…" : "Download"}
              </button>
            </div>
          </div>

          {jobId && hasBalloonOutput ? (
            <BalloonCanvas
              imageUrl={inputUrl}
              balloons={pageBalloons}
              mode={mode}
              selected={selected}
              busy={busy && mode !== "delete" && mode !== "create" && !unsavedEdits}
              liveDraw
              pageGeometry={job?.page_geometry}
              onSelect={setSelected}
              onCreate={onCreateBox}
              onMove={onMoveBalloon}
              onMoveStart={onMoveStart}
              onMoveEnd={onMoveEnd}
              onDelete={(n) => doDelete(n)}
              deleteSelected={deleteSelected}
              onToggleDeleteSelect={toggleDeleteSelect}
            />
          ) : (
            <div className="panel-body">
              <div className="placeholder">
                {jobId
                  ? pageCount > 1
                    ? `Multi-page PDF (${pageCount} pages). Click “Run auto ballooning” — every page is processed and the download stays one combined file.`
                    : "Input is on the left. Click “Run auto ballooning” to generate the ballooned output here."
                  : "Upload a PDF or image to start."}
              </div>
            </div>
          )}

          {jobId && pageBalloons.length > 0 && (mode === "edit" || mode === "delete") && (
            <div className="balloon-list">
              {mode === "delete" && (
                <div className="balloon-list-hint">
                  Click balloon to delete · Ctrl+click to multi-select · Del key removes selection
                </div>
              )}
              {pageBalloons.map((b) => (
                <div
                  key={b.balloon}
                  className={`balloon-list-item ${
                    mode === "edit" && selected === b.balloon
                      ? "selected"
                      : mode === "delete" && deleteSelected.includes(b.balloon)
                        ? "marked-delete"
                        : ""
                  }`}
                >
                  {mode === "delete" && (
                    <input
                      type="checkbox"
                      className="balloon-list-check"
                      checked={deleteSelected.includes(b.balloon)}
                      onChange={() => toggleDeleteSelect(b.balloon)}
                      aria-label={`Select balloon ${b.balloon} for delete`}
                    />
                  )}
                  <button
                    type="button"
                    className="balloon-list-pick"
                    onClick={() => {
                      if (mode === "delete") toggleDeleteSelect(b.balloon);
                      else setSelected(b.balloon);
                    }}
                  >
                    #{b.balloon} · {b.text}
                  </button>
                  {mode === "delete" && (
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => doDelete(b.balloon)}
                    >
                      Del
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {showReport && jobId && job && (
        <ReportPanel
          job={job}
          jobId={jobId}
          onClose={() => {
            setShowReport(false);
            setErr("");
          }}
          onSaved={async () => {
            await refresh(jobId);
          }}
          onError={(msg) => setErr(msg)}
        />
      )}
    </div>
  );
}

function ModeBtn({
  active,
  onClick,
  label,
  disabled,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className={`btn btn-sm ${active ? "btn-primary" : "btn-ghost"}`}
      style={{ width: "auto" }}
      onClick={onClick}
      disabled={disabled}
    >
      {label}
    </button>
  );
}

function defaultMeasureCols(): MeasureCol[] {
  return [
    { id: "m1", label: "M1", values: {} },
    { id: "m2", label: "M2", values: {} },
  ];
}

function nowReportDate() {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function ReportPanel({
  job,
  jobId,
  onSaved,
  onClose,
  onError,
}: {
  job: Job;
  jobId: string;
  onSaved: () => Promise<void>;
  onClose: () => void;
  onError: (msg: string) => void;
}) {
  const [partNumber, setPartNumber] = useState(job.part_number || "");
  const [partName, setPartName] = useState(job.part_name || "");
  const [revision, setRevision] = useState(job.revision || "");
  const [material, setMaterial] = useState(job.material || "");
  const [mass, setMass] = useState(job.mass || "");
  const [finish, setFinish] = useState(job.finish || "");
  const [serialNumber, setSerialNumber] = useState(job.serial_number || "");
  const [reportDate, setReportDate] = useState(job.report_date || nowReportDate());
  const [companyName, setCompanyName] = useState(job.company_name || "");
  const [supplierName, setSupplierName] = useState(job.supplier_name || "");
  const [customerName, setCustomerName] = useState(job.customer_name || "");
  const [authorizedSignatory, setAuthorizedSignatory] = useState(job.authorized_signatory || "");
  const [rows, setRows] = useState<ReportRow[]>(() =>
    (job.report_rows || []).length
      ? job.report_rows || []
      : (job.balloons || []).map((b, i) => ({
          sno: i + 1,
          balloon: b.balloon,
          reference: `P${b.page || 1}`,
          nominal: b.text || "",
          tol_low: "",
          tol_high: "",
          instrument: "",
          unit: "mm",
          instr_id: "",
          measured: ["", ""],
          remarks: "",
        })),
  );
  const [cols, setCols] = useState<MeasureCol[]>(() =>
    job.measure_columns?.length ? job.measure_columns : defaultMeasureCols(),
  );
  const [idRev, setIdRev] = useState(0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [page, setPage] = useState(0);
  const [rowsDirty, setRowsDirty] = useState(false);
  const [reportDirty, setReportDirty] = useState(false);
  const pageSize = 40;

  const markReportDirty = () => setReportDirty(true);
  const markRowsDirty = () => {
    setRowsDirty(true);
    setReportDirty(true);
  };
  const setHeader =
    (setter: (v: string) => void) =>
    (v: string) => {
      markReportDirty();
      setter(v);
    };

  useEffect(() => {
    const nextRows =
      (job.report_rows || []).length > 0
        ? job.report_rows || []
        : (job.balloons || []).map((b, i) => ({
            sno: i + 1,
            balloon: b.balloon,
            reference: `P${b.page || 1}`,
            nominal: b.text || "",
            tol_low: "",
            tol_high: "",
            instrument: "",
            unit: "mm",
            instr_id: "",
            measured: ["", ""],
            remarks: "",
          }));
    setRows(nextRows);
    setRowsDirty(false);
    setReportDirty(false);
    setCols(job.measure_columns?.length ? job.measure_columns : defaultMeasureCols());
    setPartNumber(job.part_number || "");
    setPartName(job.part_name || "");
    setRevision(job.revision || "");
    setMaterial(job.material || "");
    setMass(job.mass || "");
    setFinish(job.finish || "");
    setSerialNumber(job.serial_number || "");
    setReportDate(job.report_date || nowReportDate());
    setCompanyName(job.company_name || "");
    setSupplierName(job.supplier_name || "");
    setCustomerName(job.customer_name || "");
    setAuthorizedSignatory(job.authorized_signatory || "");
    setPage(0);
  }, [job]);

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const pageRows = rows.slice(page * pageSize, page * pageSize + pageSize);

  const addColumn = () => {
    markRowsDirty();
    setCols((c) => {
      const n = c.length + 1;
      return [...c, { id: `m${Date.now()}`, label: `M${n}`, values: {} }];
    });
  };

  const removeColumn = () => {
    markRowsDirty();
    setCols((c) => (c.length <= 2 ? c : c.slice(0, -1)));
  };

  const renameColumn = (id: string, label: string) => {
    markRowsDirty();
    setCols((cs) => cs.map((col) => (col.id === id ? { ...col, label } : col)));
  };

  const addInstrId = () => {
    const instrument =
      window.prompt(
        "Instrument type for this ID (e.g. Vernier Caliper)",
        rows.find((r) => r.instrument)?.instrument || "Vernier Caliper",
      ) || "";
    if (!instrument.trim()) return;
    const id = window.prompt("New instrument ID (e.g. VC-004)", "") || "";
    if (!id.trim()) return;
    addInstrumentId(instrument.trim(), id.trim());
    setIdRev((n) => n + 1);
    setMsg(`Instrument ID “${id.trim()}” added.`);
  };

  const editInstrId = () => {
    const known = Array.from(
      new Set([...allInstrumentIds(), ...rows.map((r) => r.instr_id).filter(Boolean)]),
    );
    const current = rows.find((r) => r.instr_id)?.instr_id || known[0] || "";
    const oldId = window.prompt(`Instrument ID to rename\nKnown: ${known.slice(0, 12).join(", ")}`, current) || "";
    if (!oldId.trim()) return;
    const newId = window.prompt("New name / ID", oldId.trim()) || "";
    if (!newId.trim() || newId.trim() === oldId.trim()) return;
    renameInstrumentId(oldId.trim(), newId.trim());
    markRowsDirty();
    setRows((rs) => rs.map((r) => (r.instr_id === oldId.trim() ? { ...r, instr_id: newId.trim() } : r)));
    setIdRev((n) => n + 1);
    setMsg(`Instrument ID renamed to “${newId.trim()}”.`);
  };

  const extractTitle = async () => {
    setBusy(true);
    setMsg("");
    try {
      const res = await api.extractTitle(jobId);
      if (res.part_number) setPartNumber(res.part_number);
      if (res.part_name) setPartName(res.part_name);
      if (res.revision) setRevision(res.revision);
      if (res.material) setMaterial(res.material);
      if (res.mass) setMass(res.mass);
      if (res.finish) setFinish(res.finish);
      if (res.serial_number) setSerialNumber(res.serial_number);
      if (res.report_date) setReportDate(res.report_date);
      if (res.company_name) setCompanyName(res.company_name);
      if (res.supplier_name) setSupplierName(res.supplier_name);
      if (res.customer_name) setCustomerName(res.customer_name);
      if (res.authorized_signatory) setAuthorizedSignatory(res.authorized_signatory);
      markReportDirty();
      await onSaved();
      const filled = Object.entries(res).filter(([, v]) => String(v || "").trim()).length;
      setMsg(`Title block read from drawing (${filled} fields). Empty report fields were filled.`);
    } catch (e: any) {
      onError(e.message || "Title-block extract failed");
    } finally {
      setBusy(false);
    }
  };

  const updateRowByBalloon = (balloon: number, patch: Partial<ReportRow>) => {
    markRowsDirty();
    setRows((rs) => rs.map((x) => (x.balloon === balloon ? { ...x, ...patch } : x)));
  };

  const setColValue = (colId: string, balloon: number, val: string) => {
    markRowsDirty();
    setCols((cs) =>
      cs.map((col) =>
        col.id === colId ? { ...col, values: { ...col.values, [String(balloon)]: val } } : col,
      ),
    );
  };

  const payload = () => ({
    part_number: partNumber,
    part_name: partName,
    revision,
    material,
    mass,
    finish,
    serial_number: serialNumber,
    report_date: reportDate,
    company_name: companyName,
    supplier_name: supplierName,
    customer_name: customerName,
    authorized_signatory: authorizedSignatory,
    rows,
    measure_columns: cols,
  });

  const save = async () => {
    setBusy(true);
    setMsg("");
    try {
      if (rows.length > 500) {
        await api.saveReportChunked(jobId, payload(), (m) => setMsg(m));
      } else {
        setMsg(`Saving report data (${rows.length} rows)…`);
        await api.saveReport(jobId, payload(), { localOnly: rows.length > 200 });
      }
      setRowsDirty(false);
      setReportDirty(false);
      await onSaved();
      setMsg(`Report saved (${rows.length} rows).`);
    } catch (e: any) {
      onError(e.message || "Failed to save report");
    } finally {
      setBusy(false);
    }
  };

  const downloadPdf = async () => {
    setBusy(true);
    setMsg(
      rowsDirty
        ? `Preparing download (${rows.length} rows with edits)…`
        : `Preparing PDF (${rows.length} rows)…`,
    );
    await new Promise((r) => setTimeout(r, 80));
    try {
      const srcName = job.files?.[0]?.name || "";
      const stem = (srcName.replace(/\.[^.]+$/, "") || jobId).replace(/[<>:"|?*\\/]/g, "_");
      const body = payload();
      const needSave = reportDirty || rowsDirty;
      const savedPath = await api.downloadReportPdfFile(
        jobId,
        body,
        `${stem}_inspection_report.pdf`,
        {
          rowsDirty: needSave && rowsDirty,
          skipSave: !needSave,
          onProgress: (m) => setMsg(m),
        },
      );
      setRowsDirty(false);
      setReportDirty(false);
      setMsg(
        savedPath.includes("\\") || savedPath.includes("/")
          ? `Inspection report PDF saved to:\n${savedPath}`
          : "Inspection report PDF saved — check your Downloads folder (not the dist-desktop folder).",
      );
    } catch (e: any) {
      setMsg("");
      const detail =
        typeof e === "string"
          ? e
          : e?.message || e?.error || (e != null ? String(e) : "") || "Report PDF download failed";
      onError(detail);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="report-modal-backdrop" onClick={onClose}>
      <div className="report-modal report-modal-desktop" onClick={(e) => e.stopPropagation()}>
        <div className="row-between mb">
          <div>
            <h2 style={{ margin: 0 }}>Inspection Report</h2>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              {APP_TITLE} · {rows.length} characteristics
            </p>
          </div>
          <div className="row">
            <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void extractTitle()} title="Read title block / drawing labels into empty report fields">
              Extract from drawing
            </button>
            <button className="btn btn-primary btn-sm" style={{ width: "auto" }} disabled={busy} onClick={() => void save()}>
              Save report data
            </button>
            <button className="btn btn-outline btn-sm" disabled={busy} onClick={() => void downloadPdf()}>
              {busy ? "Working…" : "Download report PDF"}
            </button>
            <button className="btn btn-ghost btn-sm" onClick={onClose}>
              Close
            </button>
          </div>
        </div>

        {msg && <div className="ok mb">{msg}</div>}
        {!rows.length && (
          <div className="error mb">No report rows yet. Run auto ballooning or create balloons first.</div>
        )}

        <div className="report-title-block mb">
          <TitleField label="Part Number" value={partNumber} onChange={setHeader(setPartNumber)} />
          <TitleField label="Part Name" value={partName} onChange={setHeader(setPartName)} wide />
          <TitleField label="Revision" value={revision} onChange={setHeader(setRevision)} />
          <TitleField label="Material" value={material} onChange={setHeader(setMaterial)} wide />
          <TitleField label="Mass" value={mass} onChange={setHeader(setMass)} />
          <TitleField label="Finish Treatment" value={finish} onChange={setHeader(setFinish)} wide />
          <TitleField label="Date" value={reportDate} onChange={setHeader(setReportDate)} />
          <TitleField label="Serial Number" value={serialNumber} onChange={setHeader(setSerialNumber)} wide />
          <TitleField label="Company Name" value={companyName} onChange={setHeader(setCompanyName)} />
          <TitleField label="Supplier Name" value={supplierName} onChange={setHeader(setSupplierName)} wide />
          <TitleField label="Customer Name" value={customerName} onChange={setHeader(setCustomerName)} />
          <TitleField label="Authorized Signatory" value={authorizedSignatory} onChange={setHeader(setAuthorizedSignatory)} wide />
        </div>

        <div className="row-between mb">
          <div className="row report-col-tools">
            <div className="report-col-btns" title="Screen only — not printed on PDF">
              <button type="button" className="btn btn-sm report-col-btn" onClick={addColumn}>
                + column
              </button>
              <button
                type="button"
                className="btn btn-sm report-col-btn"
                onClick={removeColumn}
                disabled={cols.length <= 2}
              >
                − column
              </button>
            </div>
            <button className="btn btn-ghost btn-sm" disabled={page <= 0} onClick={() => setPage((p) => p - 1)}>
              Prev
            </button>
            <span className="muted">
              Page {page + 1} / {pageCount}
            </span>
            <button
              className="btn btn-ghost btn-sm"
              disabled={page >= pageCount - 1}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </button>
          </div>
        </div>

        <div className="report-wrap">
          <table className="table report-table">
            <thead>
              <tr>
                <th>S.No</th>
                <th>Balloon</th>
                <th>Reference</th>
                <th>Nominal</th>
                <th>Tol -</th>
                <th>Tol +</th>
                <th>Instrument</th>
                <th>Unit</th>
                <th className="instr-id-head">
                  <div className="instr-id-head-inner">
                    <div className="report-col-btns instr-id-btns">
                      <button type="button" className="btn btn-sm report-col-btn" onClick={addInstrId} title="Add a new Instr. ID">
                        + Instr. ID
                      </button>
                      <button type="button" className="btn btn-sm report-col-btn" onClick={editInstrId} title="Rename / edit an existing Instr. ID">
                        Rename Instr. ID
                      </button>
                    </div>
                    <span>Instr. ID</span>
                  </div>
                </th>
                {cols.map((c) => (
                  <th key={c.id}>
                    <input
                      className="report-col-name"
                      value={c.label}
                      onChange={(e) => renameColumn(c.id, e.target.value)}
                      title="Edit column name"
                    />
                  </th>
                ))}
                <th>Remarks</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((r) => (
                <tr key={r.balloon}>
                  <td>{r.sno}</td>
                  <td>{r.balloon}</td>
                  <td>
                    <input
                      value={r.reference}
                      onChange={(e) => updateRowByBalloon(r.balloon, { reference: e.target.value })}
                      title="Border grid zone (editable)"
                    />
                  </td>
                  <td>{r.nominal}</td>
                  <td>
                    <input value={r.tol_low} onChange={(e) => updateRowByBalloon(r.balloon, { tol_low: e.target.value })} />
                  </td>
                  <td>
                    <input value={r.tol_high} onChange={(e) => updateRowByBalloon(r.balloon, { tol_high: e.target.value })} />
                  </td>
                  <td>
                    <select
                      className="report-select"
                      value={r.instrument || ""}
                        onChange={(e) => {
                          const instrument = e.target.value;
                          const ids = instrumentIdOptions(instrument);
                          const instr_id = ids.includes(r.instr_id) ? r.instr_id : "";
                          const units = unitOptionsForInstrument(instrument);
                          const unit = instrument
                            ? units.includes(r.unit)
                              ? r.unit
                              : defaultUnitForInstrument(instrument)
                            : r.unit || "mm";
                          updateRowByBalloon(r.balloon, { instrument, instr_id, unit });
                        }}
                    >
                      <option value=""></option>
                      {INSTRUMENT_OPTIONS.map((name) => (
                        <option key={name} value={name}>
                          {name}
                        </option>
                      ))}
                      {r.instrument && !INSTRUMENT_OPTIONS.includes(r.instrument as (typeof INSTRUMENT_OPTIONS)[number]) && (
                        <option value={r.instrument}>{r.instrument}</option>
                      )}
                    </select>
                  </td>
                    <td>
                      <select
                        className="report-select"
                        value={r.unit || ""}
                        onChange={(e) => updateRowByBalloon(r.balloon, { unit: e.target.value })}
                      >
                        <option value=""></option>
                        {unitOptionsForInstrument(r.instrument, r.unit).map((u) => (
                          <option key={u} value={u}>
                            {u}
                          </option>
                        ))}
                      </select>
                    </td>
                  <td>
                    <select
                      className="report-select"
                      value={r.instr_id || ""}
                      onChange={(e) => updateRowByBalloon(r.balloon, { instr_id: e.target.value })}
                    >
                      <option value=""></option>
                      {instrumentIdOptions(r.instrument, r.instr_id).map((id) => (
                        <option key={`${idRev}-${id}`} value={id}>
                          {id}
                        </option>
                      ))}
                    </select>
                  </td>
                  {cols.map((c) => {
                    const legacy = (r.measured || [])[cols.indexOf(c)] || "";
                    const val = c.values[String(r.balloon)] ?? legacy;
                    const st = measureStatus(r.nominal, r.tol_low, r.tol_high, val);
                    return (
                      <td key={c.id} className={st !== "empty" ? `cell-${st}` : undefined}>
                        <input value={val} onChange={(e) => setColValue(c.id, r.balloon, e.target.value)} />
                      </td>
                    );
                  })}
                  <td>
                    <input value={r.remarks} onChange={(e) => updateRowByBalloon(r.balloon, { remarks: e.target.value })} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function TitleField({
  label,
  value,
  onChange,
  wide,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  wide?: boolean;
}) {
  return (
    <div className={`report-title-cell ${wide ? "wide" : ""}`}>
      <span className="report-title-label">{label}</span>
      <input className="report-title-value" value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
