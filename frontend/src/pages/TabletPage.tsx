import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, getApiBaseOverride, setApiBaseOverride, type TabletRecord } from "../api";
import { TABLET_EXE } from "../constants/branding";
import { clearTabletClient, getDeviceInfo, markTabletClient } from "../device";

const BAKED_API = ((import.meta.env.VITE_API_URL as string | undefined) || "").replace(/\/$/, "");

export default function TabletPage() {
  const [deviceId, setDeviceId] = useState("");
  const [name, setName] = useState("");
  const [server, setServer] = useState(getApiBaseOverride() || BAKED_API);
  const [record, setRecord] = useState<TabletRecord | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (TABLET_EXE) markTabletClient();
    getDeviceInfo().then((info) => {
      setDeviceId(info.device_id);
      setName((current) => current || localStorage.getItem("smorx_tablet_name") || info.hostname || "");
    });
  }, []);

  useEffect(() => {
    if (!deviceId) return;
    let stop = false;
    const tick = async () => {
      try {
        const status = await api.tabletStatus(deviceId);
        if (!stop && status.status !== "unknown") setRecord(status);
      } catch {
        /* server address may not be set yet */
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), 5000);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [deviceId, server]);

  const checkIn = async () => {
    if (!deviceId) return;
    if (!name.trim()) {
      setErr("Enter a name for this tablet, such as Line 2.");
      return;
    }
    const base = (server || BAKED_API).trim().replace(/\/$/, "");
    if (!base) {
      setErr("Enter the Auto Balloon server address, such as http://100.51.165.76:19686");
      return;
    }
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      setApiBaseOverride(base);
      const health = await fetch(`${base}/api/health`);
      if (!health.ok) throw new Error(`Auto Balloon server did not answer (${health.status})`);
      markTabletClient();
      localStorage.setItem("smorx_tablet_name", name.trim());
      localStorage.setItem("smorx_hostname", name.trim());
      const row = await api.checkInTablet(deviceId, name.trim(), name.trim());
      setRecord(row);
      setMsg(
        row.status === "approved"
          ? "This tablet is approved. Sign in to fill inspection reports."
          : "Checked in. Ask Super Admin to approve this tablet, then sign in.",
      );
    } catch (ex: unknown) {
      setErr(ex instanceof Error ? ex.message : "Could not reach the Auto Balloon server");
    } finally {
      setBusy(false);
    }
  };

  const approved = record?.status === "approved";

  if (!TABLET_EXE) {
    return (
      <div className="page-center">
        <div className="card" style={{ maxWidth: 560 }}>
          <h1>Inspection tablet</h1>
          <p className="muted">
            Factory tablets use InspectionTablet.exe (Windows) or InspectionTablet.apk (Android / Samsung). Do not register a web browser as a tablet.
          </p>
          <p className="muted">
            Install the tablet app, type a name such as Line 2, then Register. Super Admin approves it under Factory tablets.
          </p>
          <p className="muted mt">
            <Link to="/login">Back to sign in</Link>
            {" · "}
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => {
                clearTabletClient();
                window.location.hash = "#/login";
              }}
            >
              This browser is the PC, not a tablet
            </button>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="page-center">
      <div className="card" style={{ maxWidth: 560 }}>
        <h1>Inspection tablet</h1>
        <p className="muted">
          This tablet app connects to the Auto Balloon server. Every report you save is stored there.
        </p>

        {err && <div className="error mb">{err}</div>}
        {msg && <div className="ok mb">{msg}</div>}

        <div className="field">
          <label>Auto Balloon server address</label>
          <input
            value={server}
            placeholder="http://100.51.165.76:19686"
            onChange={(e) => setServer(e.target.value)}
            autoCapitalize="off"
            autoCorrect="off"
          />
        </div>
        <div className="field">
          <label>Tablet name — type this</label>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Bay 3 tablet" />
        </div>
        <div className="field">
          <label>Device ID — filled automatically, do not type it</label>
          <input value={deviceId || "Preparing…"} readOnly />
        </div>

        <button className="btn btn-primary" disabled={busy || !deviceId} onClick={() => void checkIn()}>
          {busy ? "Connecting…" : "Register this tablet"}
        </button>

        {record && (
          <p className="mt">
            Status: <strong>{record.status}</strong>
            {record.status === "pending" ? " — waiting for Super Admin" : ""}
          </p>
        )}

        {approved && (
          <p className="mt">
            <Link className="btn btn-gold" to="/login">
              Sign in and open inspection
            </Link>
          </p>
        )}
      </div>
    </div>
  );
}
