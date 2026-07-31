import { NavLink, Outlet } from "react-router-dom";

export function AdminLayout() {
  return (
    <div>
      <h1>Admin</h1>
      <div className="tab-rail">
        <NavLink to="/admin/users">Users</NavLink>
        <NavLink to="/admin/settings">System settings</NavLink>
        <NavLink to="/admin/prompts">Prompts</NavLink>
      </div>
      <Outlet />
    </div>
  );
}
