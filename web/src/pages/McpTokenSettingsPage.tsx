import { useCallback, useEffect, useState } from "react";
import { McpToken, mcpTokens, ApiError } from "../lib/api";

export function McpTokenSettingsPage() {
  const [tokens, setTokens] = useState<McpToken[] | null>(null);
  const [newToken, setNewToken] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const { tokens: list } = await mcpTokens.list();
    setTokens(list);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleCreate() {
    setBusy(true);
    setError(null);
    try {
      const created = await mcpTokens.create(label || undefined);
      setNewToken(created.token);
      setLabel("");
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to create token");
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke(id: number) {
    if (!window.confirm("Revoke this token? Any MCP client using it will stop working immediately.")) return;
    setBusy(true);
    try {
      await mcpTokens.revoke(id);
      await load();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="card">
        <h3>MCP access tokens</h3>
        <p className="muted">
          Used by MCP clients (Claude Desktop, editors, scripts) to access your mailbox data — separate from your
          dashboard login.
        </p>
        <div className="row">
          <input placeholder="Label (e.g. laptop)" value={label} onChange={(e) => setLabel(e.target.value)} />
          <button className="primary" onClick={handleCreate} disabled={busy}>
            Generate new token
          </button>
        </div>
        {error && <p className="error-text">{error}</p>}
        {newToken && (
          <div style={{ marginTop: 12 }}>
            <p className="success-text">Copy this now — it won&apos;t be shown again:</p>
            <pre className="token-value">{newToken}</pre>
            <pre className="token-value">
              Authorization: Bearer {newToken}
              {"\n"}URL: {window.location.origin}/sse
            </pre>
          </div>
        )}
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Label</th>
              <th>Prefix</th>
              <th>Created</th>
              <th>Last used</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {tokens?.map((t) => (
              <tr key={t.id}>
                <td>{t.label || "—"}</td>
                <td>{t.token_prefix}…</td>
                <td>{new Date(t.created_at).toLocaleString()}</td>
                <td>{t.last_used_at ? new Date(t.last_used_at).toLocaleString() : "never"}</td>
                <td>
                  <button className="danger" onClick={() => handleRevoke(t.id)} disabled={busy}>
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
            {tokens?.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No active tokens.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
