import { HashRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useEffect, useState } from "react";
import { AuthProvider, useAuth } from "./auth";
import LoginPage from "./pages/LoginPage";
import ActivatePage from "./pages/ActivatePage";
import ChangePasswordPage from "./pages/ChangePasswordPage";
import AdminPage from "./pages/AdminPage";
import InspectionPage from "./pages/InspectionPage";
import { isDesktopApp, isLicenseActivated } from "./device";

const LAST_ROUTE_KEY = "smorx_last_route";

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

function Private({ children }: { children: React.ReactNode }) {
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
    return <Navigate to="/activate" replace />;
  }
  if (!user) return <Navigate to="/login" replace />;
  if (user.must_change_password) return <Navigate to="/change-password" replace />;
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
        <AuthBoot>
          <RememberRoute />
          <Routes>
            <Route path="/activate" element={<ActivatePage />} />
            <Route path="/login" element={<LoginPage />} />
            <Route path="/change-password" element={<ChangePasswordPage />} />
            <Route
              path="/admin"
              element={
                <Private>
                  <AdminPage />
                </Private>
              }
            />
            <Route
              path="/app"
              element={
                <Private>
                  <InspectionPage />
                </Private>
              }
            />
            <Route path="/" element={<HomeRedirect />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </AuthBoot>
      </HashRouter>
    </AuthProvider>
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
  if (user.must_change_password) return <Navigate to="/change-password" replace />;
  const last = sessionStorage.getItem(LAST_ROUTE_KEY);
  if (last === "/app" || last === "/admin") return <Navigate to={last} replace />;
  if (user.role === "super_admin") return <Navigate to="/admin" replace />;
  return <Navigate to="/app" replace />;
}
