import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useEffect } from "react";
import { AuthProvider, useAuth } from "./auth";
import LoginPage from "./pages/LoginPage";
import AdminPage from "./pages/AdminPage";
import InspectionPage from "./pages/InspectionPage";

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
  if (!ready) {
    return (
      <div className="page-center">
        <div className="muted">Loading…</div>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;
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
      <BrowserRouter>
        <AuthBoot>
          <RememberRoute />
          <Routes>
            <Route path="/login" element={<LoginPage />} />
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
      </BrowserRouter>
    </AuthProvider>
  );
}

/** Prefer last workspace page; otherwise admins → admin, others → app. */
function HomeRedirect() {
  const { user } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  const last = sessionStorage.getItem(LAST_ROUTE_KEY);
  if (last === "/app" || last === "/admin") return <Navigate to={last} replace />;
  if (user.role === "super_admin") return <Navigate to="/admin" replace />;
  return <Navigate to="/app" replace />;
}
