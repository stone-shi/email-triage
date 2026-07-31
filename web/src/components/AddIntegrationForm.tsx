import { FormEvent, useState } from "react";
import { ApiError, ProviderSpec, integrations as integrationsApi } from "../lib/api";

export function AddIntegrationForm({ providers, onAdded }: { providers: ProviderSpec[]; onAdded: () => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const [accountKey, setAccountKey] = useState("");
  const [label, setLabel] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("993");
  const [password, setPassword] = useState("");

  async function connectOAuth(providerId: string) {
    setError(null);
    setSubmitting(true);
    try {
      const { authorize_url } = await integrationsApi.startOAuth(providerId);
      window.location.href = authorize_url;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to start connection");
      setSubmitting(false);
    }
  }

  async function submitImap(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await integrationsApi.createImap({
        account_key: accountKey,
        account_label: label || undefined,
        config: { host, port: Number(port) },
        secret: { password },
      });
      setAccountKey("");
      setLabel("");
      setHost("");
      setPort("993");
      setPassword("");
      setSelected(null);
      onAdded();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to add account");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="card">
      <h3>Connect an account</h3>
      {!selected && (
        <div className="row wrap">
          {providers.map((p) => (
            <button
              key={p.id}
              disabled={!p.available}
              title={p.unavailable_reason ?? undefined}
              onClick={() => (p.auth_type === "oauth" ? connectOAuth(p.id) : setSelected(p.id))}
            >
              {p.label}
              {!p.available && " (unavailable)"}
            </button>
          ))}
        </div>
      )}
      {selected === "imap" && (
        <form onSubmit={submitImap} style={{ marginTop: 12 }}>
          <div className="field">
            <label>Email address</label>
            <input value={accountKey} onChange={(e) => setAccountKey(e.target.value)} required style={{ width: "100%" }} />
          </div>
          <div className="field">
            <label>Label (optional)</label>
            <input value={label} onChange={(e) => setLabel(e.target.value)} style={{ width: "100%" }} />
          </div>
          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label>IMAP host</label>
              <input value={host} onChange={(e) => setHost(e.target.value)} required style={{ width: "100%" }} />
            </div>
            <div className="field" style={{ width: 100 }}>
              <label>Port</label>
              <input value={port} onChange={(e) => setPort(e.target.value)} required style={{ width: "100%" }} />
            </div>
          </div>
          <div className="field">
            <label>Password</label>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required style={{ width: "100%" }} />
          </div>
          <div className="row">
            <button type="submit" className="primary" disabled={submitting}>
              {submitting ? "Adding…" : "Add account"}
            </button>
            <button type="button" onClick={() => setSelected(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}
