import { useEffect, useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { api } from "../api";
import { getDeviceInfo, isLicenseActivated, markLicenseActivated } from "../device";

export default function ActivatePage() {
  const { user, ready, login } = useAuth();
  const nav = useNavigate();
  const [licenseKey, setLicenseKey] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [installPassword, setInstallPassword] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [hostname, setHostname] = useState("");
  const [show, setShow] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // Always read the real device identity from this machine — never let the
    // user type it in, or a copied/shared device_id could activate on any PC.
    getDeviceInfo().then((info) => {
      setDeviceId(info.device_id);
      setHostname(info.hostname);
    });
  }, []);

  if (!ready) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }

  if (isLicenseActivated() && user) {
    if (user.must_change_password) return <Navigate to="/change-password" replace />;
    return <Navigate to={user.role === "super_admin" ? "/admin" : "/app"} replace />;
  }

  if (isLicenseActivated() && !user) {
    return <Navigate to="/login" replace />;
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const res = await api.activate({
        license_key: licenseKey.trim(),
        username: username.trim(),
        password,
        device_id: deviceId.trim().toUpperCase(),
        hostname: hostname.trim(),
        install_password: installPassword,
      });
      localStorage.setItem("smorx_device_id", deviceId.trim().toUpperCase());
      localStorage.setItem("smorx_hostname", hostname.trim());
      markLicenseActivated({
        license_key: res.customer.license_key,
        customer_id: res.customer.customer_id,
        installation_id: res.installation.installation_id,
      });
      localStorage.setItem("smorx_token", res.token);
      await login(username.trim(), password, {
        device_id: deviceId.trim().toUpperCase(),
        hostname: hostname.trim(),
      });
      const me = await api.me();
      if (me.must_change_password) {
        nav("/change-password");
        return;
      }
      nav(me.role === "super_admin" ? "/admin" : "/app");
    } catch (ex: unknown) {
      const msg = ex instanceof Error ? ex.message : "Activation failed";
      setErr(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page-center">
      <div className="card">
        <div className="logo-box">
          <img src="/smorx-logo-full.png" alt="SmorX.ai" />
        </div>
        <h1>Activate AutoBalloon</h1>
        <p className="muted">
          Enter the license, install password, account, and device details provided by SmorX.
        </p>
        <form onSubmit={onSubmit}>
          <div className="field">
            <label>Install password</label>
            <input
              type={show ? "text" : "password"}
              value={installPassword}
              onChange={(e) => setInstallPassword(e.target.value)}
              placeholder="Provided by SmorX admin"
              autoComplete="off"
            />
          </div>
          <div className="field">
            <label>License key</label>
            <input
              value={licenseKey}
              onChange={(e) => setLicenseKey(e.target.value)}
              placeholder="AB-2026-001"
              autoComplete="off"
              required
            />
          </div>
          <div className="field">
            <label>Email or username</label>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              required
            />
          </div>
          <div className="field">
            <label>Password (temporary)</label>
            <div className="password-wrap">
              <input
                type={show ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
              <button type="button" className="show" onClick={() => setShow((s) => !s)}>
                {show ? "Hide" : "Show"}
              </button>
            </div>
          </div>
          <div className="field">
            <label>Device ID (auto-detected, cannot be edited)</label>
            <input value={deviceId || "Detecting…"} readOnly disabled />
          </div>
          <div className="field">
            <label>Host name (auto-detected, cannot be edited)</label>
            <input value={hostname || "Detecting…"} readOnly disabled />
          </div>
          {err && <div className="error">{err}</div>}
          <button className="btn btn-primary" disabled={busy || !deviceId || !hostname}>
            {busy ? "Activating…" : "Activate"}
          </button>
        </form>
        <p className="muted mt" style={{ fontSize: "0.8rem" }}>
          Already activated?{" "}
          <a href="#/login" style={{ color: "var(--blue)" }}>
            Sign in
          </a>
        </p>
      </div>
    </div>
  );
}
