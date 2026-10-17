import { HashRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { AuthProvider, useAuth } from "./auth";
import LoginPage from "./pages/LoginPage";
import ActivatePage from "./pages/ActivatePage";
import ChangePasswordPage from "./pages/ChangePasswordPage";
import AdminPage from "./pages/AdminPage";
import InspectionPage from "./pages/InspectionPage";
import { isDesktopApp, isLicenseActivated, verifyActivationWithServer } from "./device";
import { APP_TITLE, COSTSHEET_ADMIN_PORTAL } from "./constants/branding";

const LAST_ROUTE_KEY = "smorx_last_route";

/** Runs once at startup: if desktop and not locally marked activated, ask the
 *  backend whether this machine is already an approved installation before any
 *  page's synchronous isLicenseActivated() check runs. */
function ActivationBoot({ children }: { children: React.ReactNode }) {
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    (async () => {
      if (await isDesktopApp()) {
        await verifyActivationWithServer();
      }
      setChecked(true);
    })();
  }, []);

  if (!checked) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }
  return <>{children}</>;
}

function AuthBoot({ children }: { children: React.ReactNode }) {
  const { ready } = useAuth();
  if (!ready) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }
  return <>{children}</>;
}

function Private({
  children,
  adminOnly = false,
}: {
  children: React.ReactNode;
  adminOnly?: boolean;
}) {
  const { user, ready } = useAuth();
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
    return <Navigate to={COSTSHEET_ADMIN_PORTAL ? "/access-denied" : "/activate"} replace />;
  }
  if (!user) return <Navigate to="/login" replace />;
  if (COSTSHEET_ADMIN_PORTAL && user.role !== "super_admin") {
    return <Navigate to="/access-denied" replace />;
  }
  if (user.must_change_password) return <Navigate to="/change-password" replace />;
  // Packaged desktop EXE is for customer field use only — the admin console
  // is web-only, even if the activated account happens to carry that role.
  if (adminOnly && desktop) {
    return <Navigate to={COSTSHEET_ADMIN_PORTAL ? "/access-denied" : "/app"} replace />;
  }
  return <>{children}</>;
}

/** Remember /admin vs /app so refresh / root does not yank you away mid-work. */
function RememberRoute() {
  const loc = useLocation();
  useEffect(() => {
    if (loc.pathname === "/admin" || loc.pathname === "/app") {
      sessionStorage.setItem(LAST_ROUTE_KEY, loc.pathname);
    }
  }, [loc.pathname]);
  return null;
}

export default function App() {
  return (
    <AuthProvider>
      <HashRouter>
        <ActivationBoot>
          <AuthBoot>
            <RememberRoute />
            <Routes>
              <Route path="/activate" element={COSTSHEET_ADMIN_PORTAL ? <Navigate to="/login" replace /> : <ActivatePage />} />
              <Route path="/login" element={<LoginPage />} />
              <Route path="/change-password" element={<ChangePasswordPage />} />
              <Route path="/access-denied" element={<AccessDeniedPage />} />
              <Route
                path="/admin"
                element={
                  <Private adminOnly>
                    <AdminPage />
                  </Private>
                }
              />
              <Route
                path="/app"
                element={COSTSHEET_ADMIN_PORTAL ? <Navigate to="/" replace /> : (
                  <Private>
                    <InspectionPage />
                  </Private>
                )}
              />
              <Route path="/" element={<HomeRedirect />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </AuthBoot>
        </ActivationBoot>
      </HashRouter>
    </AuthProvider>
  );
}

function AccessDeniedPage() {
  const { logout } = useAuth();
  const navigate = useNavigate();
  return (
    <div className="page-center">
      <div className="card">
        <h1>{APP_TITLE}</h1>
        <p className="muted">This portal is for Cost Sheet super administrators.</p>
        <button
          className="btn btn-primary"
          onClick={() => {
            logout();
            navigate("/login", { replace: true });
          }}
        >
          Sign out
        </button>
      </div>
    </div>
  );
}

/** Prefer last workspace page; otherwise admins → admin, others → app. */
function HomeRedirect() {
  const { user } = useAuth();
  const [desktop, setDesktop] = useState(false);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    isDesktopApp().then((v) => {
      setDesktop(v);
      setChecked(true);
    });
  }, []);

  if (!checked) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }

  if (desktop && !isLicenseActivated()) {
    return <Navigate to="/activate" replace />;
  }
  if (!user) return <Navigate to="/login" replace />;
  if (COSTSHEET_ADMIN_PORTAL && user.role !== "super_admin") {
    return <Navigate to="/access-denied" replace />;
  }
  if (user.must_change_password) return <Navigate to="/change-password" replace />;
  // The Cost Sheet deployment is a dedicated licensing portal. Never restore
  // a stale /app route saved by the former combined application.
  if (COSTSHEET_ADMIN_PORTAL) return <Navigate to="/admin" replace />;
  // The packaged desktop app is for customer field use only — never land on
  // the super-admin console there, regardless of role or remembered route.
  if (desktop) return <Navigate to="/app" replace />;
  const last = sessionStorage.getItem(LAST_ROUTE_KEY);
  if (last === "/app" || last === "/admin") return <Navigate to={last} replace />;
  if (user.role === "super_admin") return <Navigate to="/admin" replace />;
  return <Navigate to="/app" replace />;
}
