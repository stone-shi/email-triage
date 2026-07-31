import { useState } from "react";
import { Integration, integrations as integrationsApi, ApiError } from "../lib/api";

const STATUS_BADGE: Record<Integration["status"], string> = {
  ok: "ok",
  unverified: "warn",
  reauth_required: "error",
  error: "error",
};

export function IntegrationCard({ integration, onChanged }: { integration: Integration; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; error: string | null } | null>(null);
  const [label, setLabel] = useState(integration.account_label ?? "");
  const [editingLabel, setEditingLabel] = useState(false);

  async function handleTest() {
    setBusy(true);
    setTestResult(null);
    try {
      const result = await integrationsApi.test(integration.id);
      setTestResult(result);
    } catch (e) {
      setTestResult({ ok: false, error: e instanceof ApiError ? e.message : "Test failed" });
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  async function handleToggleEnabled() {
    setBusy(true);
    try {
      await integrationsApi.update(integration.id, { enabled: !integration.enabled });
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  async function handleSaveLabel() {
    setBusy(true);
    try {
      await integrationsApi.update(integration.id, { account_label: label });
      setEditingLabel(false);
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  async function handleDisconnect() {
    if (!window.confirm(`Disconnect ${integration.account_key}? This removes the stored credentials.`)) return;
    setBusy(true);
    try {
      await integrationsApi.remove(integration.id);
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="row between">
        <div>
          <div className="row">
            <strong>{integration.provider}</strong>
            <span className={`badge ${STATUS_BADGE[integration.status]}`}>{integration.status}</span>
            {!integration.enabled && <span className="badge warn">disabled</span>}
          </div>
          {editingLabel ? (
            <div className="row" style={{ marginTop: 6 }}>
              <input value={label} onChange={(e) => setLabel(e.target.value)} />
              <button onClick={handleSaveLabel} disabled={busy}>
                Save
              </button>
              <button onClick={() => setEditingLabel(false)}>Cancel</button>
            </div>
          ) : (
            <div className="muted" onClick={() => setEditingLabel(true)} style={{ cursor: "pointer" }} title="Click to rename">
              {integration.account_label || integration.account_key} ({integration.account_key})
            </div>
          )}
        </div>
        <div className="row">
          <button onClick={handleTest} disabled={busy}>
            Test
          </button>
          <button onClick={handleToggleEnabled} disabled={busy}>
            {integration.enabled ? "Disable" : "Enable"}
          </button>
          <button className="danger" onClick={handleDisconnect} disabled={busy}>
            Disconnect
          </button>
        </div>
      </div>
      {testResult && (
        <p className={testResult.ok ? "success-text" : "error-text"} style={{ marginTop: 8 }}>
          {testResult.ok ? "Connection OK" : testResult.error}
        </p>
      )}
      {integration.last_test_error && !testResult && (
        <p className="error-text" style={{ marginTop: 8 }}>
          Last check: {integration.last_test_error}
        </p>
      )}
    </div>
  );
}
