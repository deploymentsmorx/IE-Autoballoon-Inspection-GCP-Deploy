import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, getApiBaseOverride, setApiBaseOverride, type TabletRecord } from "../api";
import { clearTabletClient, getDeviceInfo, markTabletClient } from "../device";
import { APP_TITLE } from "../constants/branding";

export default function TabletPage() {
  const [deviceId, setDeviceId] = useState("");
  const [name, setName] = useState("");
  const [server, setServer] = useState(getApiBaseOverride());
  const [record, setRecord] = useState<TabletRecord | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
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
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      setApiBaseOverride(server);
      const healthUrl = `${getApiBaseOverride()}/api/health`;
      const health = await fetch(healthUrl);
      if (!health.ok) throw new Error(`Auto Balloon PC did not answer (${health.status})`);
      markTabletClient();
      localStorage.setItem("smorx_tablet_name", name.trim());
      localStorage.setItem("smorx_hostname", name.trim());
      const row = await api.checkInTablet(deviceId, name.trim(), name.trim());
      setRecord(row);
      setMsg(
        row.status === "approved"
          ? "This tablet is approved. Sign in to fill inspection reports. Reports are saved on the Auto Balloon PC."
          : "Checked in. Ask Super Admin to approve this tablet, then sign in.",
      );
    } catch (ex: unknown) {
      setErr(ex instanceof Error ? ex.message : "Could not reach the Auto Balloon PC");
    } finally {
      setBusy(false);
    }
  };

  const approved = record?.status === "approved";

  return (
    <div className="page-center">
      <div className="card" style={{ maxWidth: 560 }}>
        <h1>Inspection tablet</h1>
        <p className="muted">
          {APP_TITLE} stays on the Windows PC. This tablet connects to that PC, and every report you save is stored there.
        </p>
        <p className="muted">
          On the tablet, open this page in Chrome and choose Add to Home screen. Android cannot install the Windows .exe.
          A Windows tablet uses this same page.
        </p>

        {err && <div className="error mb">{err}</div>}
        {msg && <div className="ok mb">{msg}</div>}

        <div className="field">
          <label>Auto Balloon PC address</label>
          <input
            value={server}
            placeholder="Leave blank when this page was opened from that PC"
            onChange={(e) => setServer(e.target.value)}
            autoCapitalize="off"
            autoCorrect="off"
          />
          <span className="muted">Example: http://192.168.1.20:9686</span>
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
