import { NavLink, Outlet } from "react-router-dom";
import { EnvelopeSimple, Gauge, GearSix, ShieldCheck, SignOut, TerminalWindow } from "@phosphor-icons/react";
import { useAuth } from "../hooks/useAuth";

export function AppShell() {
  const { user, logout } = useAuth();

  return (
    <div className="app-shell">
      <nav className="app-nav">
        <h1 className="row" style={{ gap: 8 }}>
          <EnvelopeSimple size={18} weight="bold" color="var(--accent)" />
          Email Triage
        </h1>
        <NavLink to="/" end className="row">
          <Gauge size={18} />
          Dashboard
        </NavLink>
        <NavLink to="/logs" className="row">
          <TerminalWindow size={18} />
          Logs
        </NavLink>
        <NavLink to="/settings/integrations" className="row">
          <GearSix size={18} />
          Settings
        </NavLink>
        {user?.is_admin && (
          <NavLink to="/admin/users" className="row">
            <ShieldCheck size={18} />
            Admin
          </NavLink>
        )}
        <div className="user-box">
          <div className="row between">
            <span>{user?.display_name || user?.username}</span>
            {user?.is_admin && <span className="badge admin">admin</span>}
          </div>
          <div style={{ marginTop: 10 }}>
            <button className="row" onClick={() => logout()} style={{ width: "100%", justifyContent: "center" }}>
              <SignOut size={16} />
              Sign out
            </button>
          </div>
        </div>
      </nav>
      <main className="app-content">
        <Outlet />
      </main>
    </div>
  );
}
