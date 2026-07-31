import { useCallback, useEffect, useState } from "react";
import { DashboardStatus, dashboard } from "../lib/api";
import { useAuth } from "../hooks/useAuth";
import { TokenUsageChart } from "../components/TokenUsageChart";

function AccountCard({ account, kind }: { account: any; kind: string }) {
  if (!account) return null;
  return (
    <div className="card">
      <div className="row between">
        <div>
          <strong>{kind}</strong> <span className="muted">{account.account}</span>
        </div>
        {account.progress && (
          <span className="badge warn">
            {account.progress.phase}: {account.progress.processed}/{account.progress.total || "?"}
          </span>
        )}
      </div>
      {account.counts && (
        <div className="row" style={{ marginTop: 8, fontSize: 13 }}>
          <span>Total: {account.counts.total}</span>
          <span>L2: {account.counts.level_2}</span>
          <span>L1: {account.counts.level_1}</span>
          <span>L0: {account.counts.level_0}</span>
          <span>Pending: {account.counts.pending_triage}</span>
        </div>
      )}
      {account.summary?.errors?.length > 0 && (
        <ul style={{ marginTop: 8 }}>
          {account.summary.errors.map((e: string, i: number) => (
            <li key={i} className="error-text">
              {e}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ProfilePanel({ name, data, onAction }: { name: string; data: any; onAction: (action: string, profile: string) => void }) {
  const accounts = data.accounts as any[] | undefined;

  return (
    <div className="card">
      <div className="row between">
        <h3 style={{ margin: 0 }}>{name}</h3>
        <div className="row">
          <button onClick={() => onAction("sync", name)} disabled={data.running}>
            Sync
          </button>
          <button onClick={() => onAction("download_all", name)} disabled={data.running}>
            Download All
          </button>
          <button onClick={() => onAction("stop", name)} disabled={!data.running}>
            Stop
          </button>
        </div>
      </div>
      {data.running && <span className="badge warn">running</span>}
      {data.stop_requested && <span className="badge error">stop requested</span>}

      {accounts && accounts.length > 0 ? (
        accounts.map((a) => <AccountCard key={a.account} account={a} kind={a.provider} />)
      ) : (
        <>
          <AccountCard account={data.gmail} kind="gmail" />
          <AccountCard account={data.imap} kind="imap" />
        </>
      )}
    </div>
  );
}

export function DashboardPage() {
  const { user } = useAuth();
  const [status, setStatus] = useState<DashboardStatus | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await dashboard.status(showAll);
      setStatus(data);
    } catch {
      setError("Failed to load status");
    }
  }, [showAll]);

  useEffect(() => {
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, [load]);

  async function handleAction(action: string, profile: string) {
    if (action === "sync") await dashboard.syncStart(profile);
    else if (action === "stop") await dashboard.syncStop(profile);
    else if (action === "download_all") await dashboard.downloadAllStart(profile);
    await load();
  }

  if (error) return <p className="error-text">{error}</p>;
  if (!status) return <p className="muted">Loading…</p>;

  const daily = status.token_stats?.daily ?? [];
  const totalIn = daily.reduce((sum, d) => sum + d.input_tokens, 0);
  const totalOut = daily.reduce((sum, d) => sum + d.output_tokens, 0);
  const totalSaved = daily.reduce((sum, d) => sum + d.tei_saved_tokens, 0);

  return (
    <div>
      <h1>Dashboard</h1>
      <p className="muted">
        Sync scheduler: {status.scheduler.enabled ? `every ${status.scheduler.interval}` : "disabled"} · Full-mailbox
        download: {status.download_all_scheduler.enabled ? `every ${status.download_all_scheduler.interval}` : "disabled"}
      </p>

      {user?.is_admin && (
        <label style={{ marginBottom: 12, display: "block" }}>
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> Show all users
        </label>
      )}

      <div className="card">
        <h3>Token usage</h3>
        <div className="stat-row">
          <div className="stat-tile">
            <div className="value">{totalIn.toLocaleString()}</div>
            <div className="label">input tokens</div>
          </div>
          <div className="stat-tile">
            <div className="value">{totalOut.toLocaleString()}</div>
            <div className="label">output tokens</div>
          </div>
          {status.token_stats?.tei_enabled && (
            <div className="stat-tile">
              <div className="value">{totalSaved.toLocaleString()}</div>
              <div className="label">saved by router</div>
            </div>
          )}
        </div>
        <TokenUsageChart daily={daily} />
      </div>

      {Object.entries(status.profiles).map(([name, data]) => (
        <ProfilePanel key={name} name={name} data={data} onAction={handleAction} />
      ))}
    </div>
  );
}
