import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";

export function AppShell() {
  const { user, logout } = useAuth();

  return (
    <div className="app-shell">
      <nav className="app-nav">
        <h1>Email Triage</h1>
        <NavLink to="/" end>
          Dashboard
        </NavLink>
        <NavLink to="/logs">Logs</NavLink>
        <NavLink to="/settings/integrations">Settings</NavLink>
        {user?.is_admin && <NavLink to="/admin/users">Admin</NavLink>}
        <div className="user-box">
          <div>{user?.display_name || user?.username}</div>
          {user?.is_admin && <span className="badge admin">admin</span>}
          <div style={{ marginTop: 8 }}>
            <button onClick={() => logout()}>Sign out</button>
          </div>
        </div>
      </nav>
      <main className="app-content">
        <Outlet />
      </main>
    </div>
  );
}
