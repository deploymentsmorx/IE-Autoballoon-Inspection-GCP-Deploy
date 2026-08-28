import { useEffect, useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { api } from "../api";
import { getDeviceInfo, isDesktopApp, isLicenseActivated } from "../device";
import { APP_TITLE } from "../constants/branding";

export default function LoginPage() {
  const { user, ready, login } = useAuth();
  const nav = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [desktop, setDesktop] = useState(false);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    isDesktopApp().then((v) => {
      setDesktop(v);
      setChecked(true);
    });
  }, []);

  if (!ready || !checked) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }

  if (desktop && !isLicenseActivated()) {
    return <Navigate to="/activate" replace />;
  }

  if (user) {
    if (user.must_change_password) return <Navigate to="/change-password" replace />;
    return <Navigate to={user.role === "super_admin" ? "/admin" : "/app"} replace />;
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const device = desktop ? await getDeviceInfo() : undefined;
      await login(username, password, device);
      const me = await api.me();
      if (me.must_change_password) {
        nav("/change-password");
        return;
      }
      nav(me.role === "super_admin" ? "/admin" : "/app");
    } catch (ex: any) {
      setErr(ex.message || "Login failed");
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
        <h1>{APP_TITLE}</h1>
        <p className="muted">Sign in with the email and password provided by your administrator.</p>
        <form onSubmit={onSubmit}>
          <div className="field">
            <label>Email or username</label>
            <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          </div>
          <div className="field">
            <label>Password</label>
            <div className="password-wrap">
              <input
                type={show ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
              <button type="button" className="show" onClick={() => setShow((s) => !s)}>
                {show ? "Hide" : "Show"}
              </button>
            </div>
          </div>
          {err && <div className="error">{err}</div>}
          <button className="btn btn-primary" disabled={busy}>
            {busy ? "Signing in…" : "Log in"}
          </button>
        </form>
        <p className="muted mt" style={{ fontSize: "0.8rem" }}>
          Your super admin must grant access before you can use the app.
          {desktop && (
            <>
              {" "}
              Need first-time setup?{" "}
              <a href="#/activate" style={{ color: "var(--blue)" }}>
                Activate license
              </a>
            </>
          )}
        </p>
      </div>
    </div>
  );
}
