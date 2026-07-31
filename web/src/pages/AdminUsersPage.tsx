import { FormEvent, useCallback, useEffect, useState } from "react";
import { User, users as usersApi, ApiError } from "../lib/api";
import { useAuth } from "../hooks/useAuth";

export function AdminUsersPage() {
  const { user: me } = useAuth();
  const [items, setItems] = useState<User[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [tempPassword, setTempPassword] = useState<{ username: string; password: string } | null>(null);

  const activeAdmins = (items ?? []).filter((u) => u.is_admin && u.is_active).length;

  const load = useCallback(async () => {
    const page = await usersApi.list({ includeInactive: true });
    setItems(page.items);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await usersApi.create({ username, password });
      setUsername("");
      setPassword("");
      setCreating(false);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to create user");
    }
  }

  async function toggleAdmin(u: User) {
    try {
      await usersApi.update(u.id, { is_admin: !u.is_admin });
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to update user");
    }
  }

  async function toggleActive(u: User) {
    try {
      await usersApi.update(u.id, { is_active: !u.is_active });
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to update user");
    }
  }

  async function resetPassword(u: User) {
    if (!window.confirm(`Reset ${u.username}'s password? They'll be forced to change it at next login.`)) return;
    try {
      const result = await usersApi.resetPassword(u.id);
      setTempPassword({ username: u.username, password: result.temporary_password });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to reset password");
    }
  }

  async function deleteUser(u: User) {
    if (!window.confirm(`Deactivate ${u.username}? This does not delete their data.`)) return;
    try {
      await usersApi.remove(u.id);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to deactivate user");
    }
  }

  return (
    <div>
      <div className="row between">
        <h2>Users</h2>
        <button className="primary" onClick={() => setCreating((v) => !v)}>
          {creating ? "Cancel" : "New user"}
        </button>
      </div>

      {creating && (
        <form className="card" onSubmit={handleCreate}>
          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label>Username</label>
              <input value={username} onChange={(e) => setUsername(e.target.value)} required style={{ width: "100%" }} />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>Temporary password</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={10}
                style={{ width: "100%" }}
              />
            </div>
          </div>
          <button type="submit" className="primary">
            Create
          </button>
        </form>
      )}

      {error && <p className="error-text">{error}</p>}
      {tempPassword && (
        <div className="card">
          <p className="success-text">
            New password for {tempPassword.username}: <code>{tempPassword.password}</code>
          </p>
          <button onClick={() => setTempPassword(null)}>Dismiss</button>
        </div>
      )}

      <table>
        <thead>
          <tr>
            <th>Username</th>
            <th>Admin</th>
            <th>Active</th>
            <th>Last login</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items?.map((u) => {
            const isLastAdmin = u.is_admin && u.is_active && activeAdmins <= 1;
            return (
              <tr key={u.id}>
                <td>{u.username}</td>
                <td>
                  <input
                    type="checkbox"
                    checked={u.is_admin}
                    disabled={isLastAdmin}
                    onChange={() => toggleAdmin(u)}
                  />
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={u.is_active}
                    disabled={isLastAdmin}
                    onChange={() => toggleActive(u)}
                  />
                </td>
                <td>{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}</td>
                <td className="row">
                  <button onClick={() => resetPassword(u)}>Reset password</button>
                  <button className="danger" disabled={u.id === me?.id} onClick={() => deleteUser(u)}>
                    Deactivate
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
