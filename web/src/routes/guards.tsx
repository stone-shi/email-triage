import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";

export function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();

  if (status === "loading") {
    return <div className="page-loading">Loading…</div>;
  }
  if (status === "anonymous") {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <Outlet />;
}

export function RequirePasswordChanged() {
  const { mustChangePassword } = useAuth();
  if (mustChangePassword) {
    return <Navigate to="/change-password" replace />;
  }
  return <Outlet />;
}

export function RequireAdmin() {
  const { user } = useAuth();
  if (!user?.is_admin) {
    return (
      <div className="card">
        <h2>Administrators only</h2>
        <p>You don&apos;t have permission to view this page.</p>
      </div>
    );
  }
  return <Outlet />;
}
