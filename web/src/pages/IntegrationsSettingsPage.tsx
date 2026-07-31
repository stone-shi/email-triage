import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Integration, ProviderSpec, integrations as integrationsApi } from "../lib/api";
import { IntegrationCard } from "../components/IntegrationCard";
import { AddIntegrationForm } from "../components/AddIntegrationForm";

export function IntegrationsSettingsPage() {
  const [items, setItems] = useState<Integration[] | null>(null);
  const [providers, setProviders] = useState<ProviderSpec[]>([]);
  const [params, setParams] = useSearchParams();

  const load = useCallback(async () => {
    const [{ integrations: list }, { providers: providerList }] = await Promise.all([
      integrationsApi.list(),
      integrationsApi.providers(),
    ]);
    setItems(list);
    setProviders(providerList);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const connected = params.get("connected");
  const oauthError = params.get("error");

  useEffect(() => {
    if (connected || oauthError) {
      load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function dismissBanner() {
    const next = new URLSearchParams(params);
    next.delete("connected");
    next.delete("error");
    setParams(next, { replace: true });
  }

  return (
    <div>
      {connected && (
        <div className="card" style={{ borderColor: "var(--success)" }}>
          <p className="success-text">Connected {connected} successfully.</p>
          <button onClick={dismissBanner}>Dismiss</button>
        </div>
      )}
      {oauthError && (
        <div className="card" style={{ borderColor: "var(--danger)" }}>
          <p className="error-text">Connection failed: {oauthError}</p>
          <button onClick={dismissBanner}>Dismiss</button>
        </div>
      )}

      {items === null && <p className="muted">Loading…</p>}
      {items !== null && items.length === 0 && <p className="muted">No accounts connected yet.</p>}
      {items?.map((integration) => (
        <IntegrationCard key={integration.id} integration={integration} onChanged={load} />
      ))}

      <AddIntegrationForm providers={providers} onAdded={load} />
    </div>
  );
}
