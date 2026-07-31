interface DailyStat {
  day: string;
  input_tokens: number;
  output_tokens: number;
  tei_saved_tokens: number;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

export function TokenUsageChart({ daily }: { daily: DailyStat[] }) {
  const max = Math.max(1, ...daily.map((d) => d.input_tokens + d.output_tokens));

  return (
    <div>
      <div className="chart">
        {daily.map((d) => {
          const total = d.input_tokens + d.output_tokens;
          const heightPct = Math.max(1, (total / max) * 100);
          const inPct = total > 0 ? (d.input_tokens / total) * 100 : 0;
          return (
            <div
              key={d.day}
              className="bar"
              style={{ height: `${heightPct}%` }}
              title={`${d.day}: ${formatTokens(d.input_tokens)} in / ${formatTokens(d.output_tokens)} out`}
            >
              <div className="in" style={{ height: `${inPct}%` }} />
            </div>
          );
        })}
      </div>
      <div className="row" style={{ fontSize: 12 }}>
        <span className="muted">last {daily.length} days</span>
        <span className="row" style={{ gap: 4 }}>
          <span style={{ width: 9, height: 9, background: "var(--accent)", display: "inline-block", borderRadius: 2 }} />
          input
        </span>
        <span className="row" style={{ gap: 4 }}>
          <span style={{ width: 9, height: 9, background: "#93c5fd", display: "inline-block", borderRadius: 2 }} />
          output
        </span>
      </div>
    </div>
  );
}
