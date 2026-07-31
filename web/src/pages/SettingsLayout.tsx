import { NavLink, Outlet } from "react-router-dom";

export function SettingsLayout() {
  return (
    <div>
      <h1>Settings</h1>
      <div className="tab-rail">
        <NavLink to="/settings/integrations">Integrations</NavLink>
        <NavLink to="/settings/mcp">MCP access</NavLink>
        <NavLink to="/settings/password">Password</NavLink>
      </div>
      <Outlet />
    </div>
  );
}
