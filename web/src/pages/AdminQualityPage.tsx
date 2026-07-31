import { useCallback, useEffect, useState } from "react";
import { ApiError, QualityTrendDay, quality as qualityApi } from "../lib/api";
import { QualityTrendChart } from "../components/QualityTrendChart";

function formatDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
}

export function AdminQualityPage() {
  const [trend, setTrend] = useState<QualityTrendDay[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const t = await qualityApi.trend(7);
      setTrend(t.days);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load quality-check data");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleRunNow() {
    setRunning(true);
    setMessage(null);
    try {
      const res = await qualityApi.runNow();
      setMessage(res.status === "started" ? "Quality check started in the background." : res.reason ?? res.status);
      setTimeout(load, 4000);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to start quality check");
    } finally {
      setRunning(false);
    }
  }

  if (error) return <p className="error-text">{error}</p>;
  if (!trend) return <p className="muted">Loading…</p>;

  const hasAnyRuns = trend.some((d) => d.run_count > 0);
  const sortedByRecent = [...trend].reverse();

  return (
    <div>
      <div className="card">
        <div className="row between" style={{ alignItems: "flex-start" }}>
          <div>
            <h3 style={{ marginBottom: 2 }}>Last 7 days</h3>
            <p className="muted" style={{ marginTop: 0, fontSize: 12.5, maxWidth: 560 }}>
              A stratified sample of triaged emails — drawn per triage level across all of a user's
              accounts combined, so no level goes unchecked — is re-evaluated nightly by a separate
              judge model. Precision/recall/F1 compare production's cached triage level against the
              judge's independent re-classification; summary quality is the judge's 1-10 grade of the
              production summary. Hover the chart for exact values. Configure the judge model,
              sample rate, and schedule under System Settings → Quality check.
            </p>
          </div>
          <button className="primary" onClick={handleRunNow} disabled={running}>
            {running ? "Starting…" : "Run now"}
          </button>
        </div>
        {message && <p className="success-text">{message}</p>}
        <QualityTrendChart days={trend} />
      </div>

      <div className="card">
        <h3>Recent runs, by day</h3>
        {!hasAnyRuns ? (
          <p className="muted">
            No runs yet. Configure a judge model in System Settings, then click "Run now" or wait for the
            nightly schedule.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Day</th>
                <th>Runs</th>
                <th>Sample</th>
                <th>Precision</th>
                <th>Recall</th>
                <th>F1</th>
                <th>Summary quality</th>
              </tr>
            </thead>
            <tbody>
              {sortedByRecent.map((d) => (
                <tr key={d.day}>
                  <td>{formatDay(d.day)}</td>
                  <td>
                    {d.run_count === 0 ? (
                      <span className="muted">—</span>
                    ) : d.error_count > 0 ? (
                      <span className="badge error" title={`${d.error_count} of ${d.run_count} run(s) failed`}>
                        {d.run_count} ({d.error_count} failed)
                      </span>
                    ) : (
                      <span className="badge ok">{d.run_count}</span>
                    )}
                  </td>
                  <td>{d.sample_size}</td>
                  <td>{d.precision !== null ? d.precision.toFixed(2) : "—"}</td>
                  <td>{d.recall !== null ? d.recall.toFixed(2) : "—"}</td>
                  <td>{d.f1 !== null ? d.f1.toFixed(2) : "—"}</td>
                  <td>{d.summary_quality_avg !== null ? `${d.summary_quality_avg.toFixed(1)}/10` : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
