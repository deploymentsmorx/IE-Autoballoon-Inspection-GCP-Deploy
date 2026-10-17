import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { COSTSHEET_ADMIN_PORTAL } from "../constants/branding";

export default function ChangePasswordPage() {
  const { user, ready, logout, refresh } = useAuth();
  const nav = useNavigate();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
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

  if (!user) return <Navigate to="/login" replace />;
  if (COSTSHEET_ADMIN_PORTAL && user.role !== "super_admin") {
    return <Navigate to="/access-denied" replace />;
  }

  if (!user.must_change_password) {
    return <Navigate to={user.role === "super_admin" ? "/admin" : "/app"} replace />;
  }

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (next.length < 8) {
      setErr("New password must be at least 8 characters");
      return;
    }
    if (next !== confirm) {
      setErr("New passwords do not match");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      await api.changePassword(current, next);
      await refresh();
      nav(COSTSHEET_ADMIN_PORTAL || user.role === "super_admin" ? "/admin" : "/app", { replace: true });
    } catch (ex: unknown) {
      setErr(ex instanceof Error ? ex.message : "Could not change password");
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
        <h1>Change password</h1>
        <p className="muted">
          Your administrator provided a temporary password. Set a new password before continuing.
        </p>
        <form onSubmit={onSubmit}>
          <div className="field">
            <label>Current (temporary) password</label>
            <input
              type={show ? "text" : "password"}
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              required
            />
          </div>
          <div className="field">
            <label>New password</label>
            <input
              type={show ? "text" : "password"}
              value={next}
              onChange={(e) => setNext(e.target.value)}
              minLength={8}
              required
            />
          </div>
          <div className="field">
            <label>Confirm new password</label>
            <div className="password-wrap">
              <input
                type={show ? "text" : "password"}
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                minLength={8}
                required
              />
              <button type="button" className="show" onClick={() => setShow((s) => !s)}>
                {show ? "Hide" : "Show"}
              </button>
            </div>
          </div>
          {err && <div className="error">{err}</div>}
          <button className="btn btn-primary" disabled={busy}>
            {busy ? "Saving…" : "Update password"}
          </button>
        </form>
        <p className="muted mt" style={{ fontSize: "0.8rem" }}>
          <button type="button" className="show" onClick={logout}>
            Sign out
          </button>
        </p>
      </div>
    </div>
  );
}
