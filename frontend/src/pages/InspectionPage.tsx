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
import { useAuth } from "../auth";
import BalloonCanvas, { type Mode } from "../components/BalloonCanvas";
import ZoomImageViewer from "../components/ZoomImageViewer";

export default function InspectionPage() {
  const { user, logout, clearJob } = useAuth();
  const [jobId, setJobId] = useState<string | null>(localStorage.getItem("smorx_job"));
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [mode, setMode] = useState<Mode>("view");
  const [selected, setSelected] = useState<number | null>(null);
  const [tick, setTick] = useState(0);
  const [showReport, setShowReport] = useState(false);
  const [missing, setMissing] = useState<MissingDim[]>([]);
  const [auditMsg, setAuditMsg] = useState("");
  const [viewPage, setViewPage] = useState(0); // 0-based page in multi-page PDF
  const balloonsRef = useRef<Balloon[]>([]);

  const resetLocalJob = () => {
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
    setTick((t) => t + 1);
  };
  const refresh = async (id = jobId) => {
    if (!id) return;
    const j = await api.getJob(id);
    setJob(j);
    balloonsRef.current = j.balloons || [];
    if (j.missing_dimensions) setMissing(j.missing_dimensions);
    setTick((t) => t + 1);
  };

  useEffect(() => {
    if (jobId) refresh(jobId).catch((e) => setErr(e.message));
  }, [jobId]);

  if (!user) return <Navigate to="/login" replace />;

  const balloons = job?.balloons || [];
  balloonsRef.current = balloons;
  const pageCount = Math.max(1, Number(job?.page_count) || 1);
  const pageBalloons = balloons.filter((b) => (b.page || 1) === viewPage + 1);
  const hasBalloonOutput = job?.status === "ballooned" || balloons.length > 0;
  const canvasUrl =
    jobId && hasBalloonOutput ? api.previewBalloonUrl(jobId, viewPage) : "";
  const inputUrl = jobId ? api.previewInputUrl(jobId, viewPage) : "";

  const changePage = (next: number) => {
    const p = Math.max(0, Math.min(pageCount - 1, next));
    setViewPage(p);
    setSelected(null);
    setTick((t) => t + 1);
  };
  const onUpload = async (files: FileList | null) => {
    if (!files?.length) return;
    setBusy(true);
    setErr("");
    try {
      const res = await api.uploadPdfs(files);
      localStorage.setItem("smorx_job", res.job_id);
      setJobId(res.job_id);
      setViewPage(0);
      setMode("view");
      setSelected(null);
      setShowReport(false);
      await refresh(res.job_id);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const onBalloon = async () => {
    if (!jobId) return;
    setBusy(true);
    setErr("");
    setAuditMsg("Auto ballooning running — large drawings can take 1–3 minutes. Please wait…");
    try {
      const res = await api.runBalloon(jobId, 0);
      await refresh(jobId);
      setMode("view");
      setTick((t) => t + 1);
      setAuditMsg(`Ballooning done: ${res.count} balloons${res.page_count && res.page_count > 1 ? ` across ${res.page_count} pages` : ""}.`);
    } catch (e: any) {
      setErr(e.message || "Auto ballooning failed — try again");
      setAuditMsg("");
    } finally {
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
    setAuditMsg("Auditing / cross-checking missing dimensions…");
    try {
      const res = await api.auditCrosscheck(jobId, 0);
      setMissing(res.missing || []);
      await refresh(jobId);
      setAuditMsg(
        `Audit done: +${res.added} recovered · ${res.count} total balloons · ${res.missing_count} still missing`,
      );
      setMode("view");
    } catch (e: any) {
      setErr(e.message || "Audit failed");
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
      const res = await api.addMissingDims(jobId, [item]);
      setMissing(res.missing || []);
      await refresh(jobId);
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
      const res = await api.addMissingDims(jobId, missing);
      setMissing(res.missing || []);
      await refresh(jobId);
      setAuditMsg(`Added missing dimensions. ${res.count} balloons total.`);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const download = async (kind: "ballooned" | "report" | "excel" | "source") => {
    if (!jobId) {
      setErr("No job loaded — upload a drawing first.");
      return;
    }
    setErr("");
    try {
      if (kind === "report" && (!job?.report_rows || job.report_rows.length === 0)) {
        if (job?.balloons?.length) {
          await api.rebuildReport(jobId);
          await refresh(jobId);
        }
      }
      const url =
        kind === "ballooned"
          ? `${api.apiBase}/api/jobs/${jobId}/download/ballooned`
          : kind === "source"
            ? api.downloadSourceUrl(jobId)
            : kind === "report"
              ? api.downloadReportPdf(jobId)
              : api.downloadExcel(jobId);
      const blob = await api.authFetch(url);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download =
        kind === "ballooned"
          ? `${jobId}_ballooned.pdf`
          : kind === "source"
            ? `${jobId}_input.pdf`
            : kind === "report"
              ? `${jobId}_inspection_report.pdf`
              : `${jobId}_inspection_report.xlsx`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e: any) {
      setErr(e.message || "Download failed");
    }
  };

  const openReport = async () => {
    if (showReport) {
      setShowReport(false);
      return;
    }
    if (!jobId) {
      setErr("Upload a drawing and run ballooning before opening the report.");
      return;
    }
    setErr("");
    // Open immediately (modal). Rebuild rows in background if missing.
    setShowReport(true);
    if (!job?.report_rows?.length) {
      if (job?.balloons?.length) {
        try {
          await api.rebuildReport(jobId);
          await refresh(jobId);
        } catch (e: any) {
          // Client-side fallback so the report still opens
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
          setErr(e.message || "Report opened from balloons (server rebuild failed).");
        }
      } else {
        setErr("No balloons yet — run auto ballooning or create balloons first.");
      }
    }
  };

  const handleLogout = () => {
    resetLocalJob();
    logout();
  };

  const onCreateBox = async (box: { x: number; y: number; width: number; height: number }) => {
    if (!jobId) return;
    const raw = window.prompt("Dimension text for new balloon", "AutoBallon");
    if (raw === null) return; // Cancel
    // "AutoBallon" is a placeholder only — do not put it on the drawing / report nominal
    const trimmed = raw.trim();
    const text =
      !trimmed || /^auto\s*ballon$/i.test(trimmed) || /^auto\s*balloon$/i.test(trimmed)
        ? ""
        : trimmed;
    setBusy(true);
    setErr("");
    try {
      await api.createBalloon(jobId, {
        page: viewPage + 1,
        text,
        ...box,
      });
      await refresh(jobId);
      setMode("view");
    } catch (ex: any) {
      setErr(ex.message);
    } finally {
      setBusy(false);
    }
  };

  const onMoveBalloon = (no: number, x: number, y: number) => {
    setJob((j) => {
      if (!j) return j;
      const next = j.balloons.map((b) => (b.balloon === no ? { ...b, x, y } : b));
      balloonsRef.current = next;
      return { ...j, balloons: next };
    });
  };

  const onMoveEnd = async () => {
    if (!jobId) return;
    setBusy(true);
    setErr("");
    try {
      await api.saveBalloons(jobId, balloonsRef.current);
      await refresh(jobId);
    } catch (ex: any) {
      setErr(ex.message);
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async (balloonNo: number) => {
    if (!jobId) return;
    if (!window.confirm(`Delete balloon #${balloonNo}? Sequence will renumber.`)) return;
    setBusy(true);
    setErr("");
    try {
      await api.deleteBalloon(jobId, balloonNo);
      setSelected(null);
      await refresh(jobId);
      setMode("view");
    } catch (ex: any) {
      setErr(ex.message);
    } finally {
      setBusy(false);
    }
  };

  const saveEdits = async () => {
    if (!jobId) return;
    setBusy(true);
    try {
      await api.saveBalloons(jobId, balloonsRef.current);
      await refresh(jobId);
      setMode("view");
      setSelected(null);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="app-shell">
      <div className="topbar">
        <div className="brand">
          <img src="/smorx-logo-full.png" alt="SmorX.ai" />
        </div>
        <div className="title">Inspection Software system</div>
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
          <label>Upload drawing (PDF or image)</label>
          <input
            type="file"
            accept="application/pdf,.pdf,image/png,image/jpeg,image/webp,image/tiff,image/bmp,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp"
            multiple
            onChange={(e) => onUpload(e.target.files)}
          />
        </div>
        <button className="btn btn-primary" style={{ width: "auto" }} disabled={busy || !jobId} onClick={onBalloon}>
          {busy ? "Working…" : "Run auto ballooning"}
        </button>
        <button
          className="btn btn-outline"
          style={{ width: "auto" }}
          disabled={busy || !jobId || !hasBalloonOutput}
          onClick={() => void onAudit()}
          title="Recover missed dims and list anything still missing"
        >
          Audit / Cross-check
        </button>
        <button className="btn btn-ghost" disabled={busy} onClick={resetLocalJob}>
          New drawing
        </button>
        <button className="btn btn-ghost" disabled={!jobId} onClick={() => download("excel")}>
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
                Found by audit / cross-check — not ballooned yet. Add one or all, then Save / Download.
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

      <div className="panels">
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
            <ZoomImageViewer key={`in-${tick}-p${viewPage}`} imageUrl={inputUrl} />
          ) : (
            <div className="panel-body">
              <div className="placeholder">No file yet.</div>
            </div>
          )}
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>
              2 · Auto Ballooning
              {pageCount > 1 ? ` · page ${viewPage + 1}/${pageCount}` : ""}
              {hasBalloonOutput ? ` · ${pageBalloons.length} on page` : ""}
            </h3>
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
                  setErr("");
                }}
                label="Delete"
              />
              <button className="btn btn-primary btn-sm" style={{ width: "auto" }} onClick={saveEdits} disabled={busy || !hasBalloonOutput}>
                Save
              </button>
              <button className="btn btn-ghost btn-sm" disabled={!hasBalloonOutput} onClick={() => download("ballooned")}>
                Download
              </button>
            </div>
          </div>

          {jobId && hasBalloonOutput ? (
            <BalloonCanvas
              key={`cv-${tick}-p${viewPage}`}
              imageUrl={canvasUrl}
              balloons={pageBalloons}
              mode={mode}
              selected={selected}
              busy={busy}
              onSelect={setSelected}
              onCreate={onCreateBox}
              onMove={onMoveBalloon}
              onMoveEnd={onMoveEnd}
              onDelete={(n) => void doDelete(n)}
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
              {pageBalloons.map((b) => (
                <div key={b.balloon} className={`balloon-list-item ${selected === b.balloon ? "selected" : ""}`}>
                  <button
                    type="button"
                    className="balloon-list-pick"
                    onClick={() => {
                      if (mode === "delete") void doDelete(b.balloon);
                      else setSelected(b.balloon);
                    }}
                  >
                    #{b.balloon} · {b.text}
                  </button>
                  {mode === "delete" && (
                    <button type="button" className="btn btn-ghost btn-sm" onClick={() => void doDelete(b.balloon)}>
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
          onClose={() => setShowReport(false)}
          onSaved={async () => {
            await refresh(jobId);
          }}
          onDownloadPdf={async () => {
            await download("report");
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

function ReportPanel({
  job,
  jobId,
  onSaved,
  onDownloadPdf,
  onClose,
  onError,
}: {
  job: Job;
  jobId: string;
  onSaved: () => Promise<void>;
  onDownloadPdf: () => Promise<void> | void;
  onClose: () => void;
  onError: (msg: string) => void;
}) {
  const [partNumber, setPartNumber] = useState(job.part_number || "");
  const [partName, setPartName] = useState(job.part_name || "");
  const [revision, setRevision] = useState(job.revision || "");
  const [material, setMaterial] = useState(job.material || "");
  const [mass, setMass] = useState(job.mass || "");
  const [finish, setFinish] = useState(job.finish || "");
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
          measured: ["", "", ""],
          remarks: "",
        })),
  );
  const [cols, setCols] = useState<MeasureCol[]>(job.measure_columns || []);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [page, setPage] = useState(0);
  const pageSize = 40;

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
            measured: ["", "", ""],
            remarks: "",
          }));
    setRows(nextRows);
    setCols(job.measure_columns || []);
    setPartNumber(job.part_number || "");
    setPartName(job.part_name || "");
    setRevision(job.revision || "");
    setMaterial(job.material || "");
    setMass(job.mass || "");
    setFinish(job.finish || "");
    setPage(0);
  }, [job]);

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const pageRows = rows.slice(page * pageSize, page * pageSize + pageSize);

  const addColumn = () => {
    const id = `m${Date.now()}`;
    setCols((c) => [...c, { id, label: `Measured ${c.length + 4}`, values: {} }]);
  };

  const removeColumn = (id: string) => setCols((c) => c.filter((x) => x.id !== id));

  const updateRowByBalloon = (balloon: number, patch: Partial<ReportRow>) => {
    setRows((rs) => rs.map((x) => (x.balloon === balloon ? { ...x, ...patch } : x)));
  };

  const save = async () => {
    setBusy(true);
    setMsg("");
    try {
      const nextRows = rows.map((r) => {
        const measured = [...(r.measured || ["", "", ""])];
        while (measured.length < 3) measured.push("");
        return { ...r, measured };
      });
      await api.saveReport(jobId, {
        part_number: partNumber,
        part_name: partName,
        revision,
        material,
        mass,
        finish,
        rows: nextRows,
        measure_columns: cols,
      });
      await onSaved();
      setMsg(`Report saved (${nextRows.length} rows).`);
    } catch (e: any) {
      onError(e.message || "Failed to save report");
    } finally {
      setBusy(false);
    }
  };

  const downloadPdf = async () => {
    setBusy(true);
    setMsg("");
    try {
      const nextRows = rows.map((r) => {
        const measured = [...(r.measured || ["", "", ""])];
        while (measured.length < 3) measured.push("");
        return { ...r, measured };
      });
      await api.saveReport(jobId, {
        part_number: partNumber,
        part_name: partName,
        revision,
        material,
        mass,
        finish,
        rows: nextRows,
        measure_columns: cols,
      });
      await onSaved();
      await onDownloadPdf();
      setMsg("Inspection report PDF downloaded (NP1213M layout).");
    } catch (e: any) {
      onError(e.message || "Report PDF download failed");
    } finally {
      setBusy(false);
    }
  };

  const setMeasured = (balloon: number, mi: number, val: string) => {
    setRows((rs) =>
      rs.map((r) => {
        if (r.balloon !== balloon) return r;
        const measured = [...(r.measured || ["", "", ""])];
        while (measured.length < 3) measured.push("");
        measured[mi] = val;
        return { ...r, measured };
      }),
    );
  };

  return (
    <div className="report-modal-backdrop" onClick={onClose}>
      <div className="report-modal card wide" onClick={(e) => e.stopPropagation()}>
        <div className="row-between mb">
          <div>
            <h2 style={{ margin: 0 }}>Inspection Report</h2>
            <p className="muted" style={{ margin: "4px 0 0" }}>
              {rows.length} characteristics · same layout as InspectionReport_NP1213M.pdf
            </p>
          </div>
          <div className="row">
            <button className="btn btn-ghost btn-sm" onClick={addColumn}>
              Add measured column
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

        <div className="row mb">
          <Field label="Part number" value={partNumber} onChange={setPartNumber} />
          <Field label="Part name" value={partName} onChange={setPartName} />
          <Field label="Revision" value={revision} onChange={setRevision} />
          <Field label="Material" value={material} onChange={setMaterial} />
          <Field label="Mass" value={mass} onChange={setMass} />
          <Field label="Finish" value={finish} onChange={setFinish} />
        </div>

        <div className="row-between mb">
          <p className="muted" style={{ margin: 0 }}>
            Green = exact · Yellow = in tolerance · Red = out · Click Download report PDF for the full file
          </p>
          <div className="row">
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
          <table className="table">
            <thead>
              <tr>
                <th>S.No</th>
                <th>Balloon</th>
                <th>Reference</th>
                <th>Nominal</th>
                <th>Tol -</th>
                <th>Tol +</th>
                <th>Instrument</th>
                <th>Instr. ID</th>
                <th>M1</th>
                <th>M2</th>
                <th>M3</th>
                {cols.map((c) => (
                  <th key={c.id}>
                    {c.label}{" "}
                    <button className="btn btn-ghost btn-sm" onClick={() => removeColumn(c.id)}>
                      ×
                    </button>
                  </th>
                ))}
                <th>Remarks</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((r) => {
                const m = r.measured || ["", "", ""];
                return (
                  <tr key={r.balloon}>
                    <td>{r.sno}</td>
                    <td>{r.balloon}</td>
                    <td>{r.reference}</td>
                    <td>{r.nominal}</td>
                    <td>
                      <input value={r.tol_low} onChange={(e) => updateRowByBalloon(r.balloon, { tol_low: e.target.value })} />
                    </td>
                    <td>
                      <input value={r.tol_high} onChange={(e) => updateRowByBalloon(r.balloon, { tol_high: e.target.value })} />
                    </td>
                    <td>
                      <input
                        value={r.instrument}
                        onChange={(e) => updateRowByBalloon(r.balloon, { instrument: e.target.value })}
                      />
                    </td>
                    <td>
                      <input value={r.instr_id} onChange={(e) => updateRowByBalloon(r.balloon, { instr_id: e.target.value })} />
                    </td>
                    {[0, 1, 2].map((mi) => {
                      const st = measureStatus(r.nominal, r.tol_low, r.tol_high, m[mi] || "");
                      return (
                        <td key={mi} className={st !== "empty" ? `cell-${st}` : undefined}>
                          <input value={m[mi] || ""} onChange={(e) => setMeasured(r.balloon, mi, e.target.value)} />
                        </td>
                      );
                    })}
                    {cols.map((c) => {
                      const val = c.values[String(r.balloon)] || "";
                      const st = measureStatus(r.nominal, r.tol_low, r.tol_high, val);
                      return (
                        <td key={c.id} className={st !== "empty" ? `cell-${st}` : undefined}>
                          <input
                            value={val}
                            onChange={(e) =>
                              setCols((cs) =>
                                cs.map((col) =>
                                  col.id === c.id
                                    ? { ...col, values: { ...col.values, [String(r.balloon)]: e.target.value } }
                                    : col,
                                ),
                              )
                            }
                          />
                        </td>
                      );
                    })}
                    <td>
                      <input value={r.remarks} onChange={(e) => updateRowByBalloon(r.balloon, { remarks: e.target.value })} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="field" style={{ margin: 0, minWidth: 140 }}>
      <label>{label}</label>
      <input value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}
