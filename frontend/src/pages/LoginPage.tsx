import { FormEvent, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";
import { api } from "../api";

export default function LoginPage() {
  const { user, ready, login } = useAuth();
  const nav = useNavigate();
  const [username, setUsername] = useState("admin@smorx.ai");
  const [password, setPassword] = useState("admin123");
  const [show, setShow] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  if (!ready) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }

  if (user) {
    return <Navigate to={user.role === "super_admin" ? "/admin" : "/app"} replace />;
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      await login(username, password);
      const me = await api.me();
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
        <h1>Inspection Report</h1>
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
        </p>
      </div>
    </div>
  );
}
