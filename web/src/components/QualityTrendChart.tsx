import { useRef, useState } from "react";
import type { QualityTrendDay } from "../lib/api";

const WIDTH = 640;
const HEIGHT = 160;
const PAD = 24;

const F1_COLOR = "var(--accent)";
const QUALITY_COLOR = "#d97706";

function xFor(i: number, n: number): number {
  return PAD + (i / Math.max(1, n - 1)) * (WIDTH - PAD * 2);
}

function yFor(v: number): number {
  return HEIGHT - PAD - v * (HEIGHT - PAD * 2);
}

function linePoints(days: QualityTrendDay[], value: (d: QualityTrendDay) => number | null): string {
  return days
    .map((d, i) => {
      const v = value(d);
      return v === null ? null : `${xFor(i, days.length)},${yFor(v)}`;
    })
    .filter((p): p is string => p !== null)
    .join(" ");
}

function formatDay(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
}

export function QualityTrendChart({ days }: { days: QualityTrendDay[] }) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const hasData = days.some((d) => d.sample_size > 0);
  const f1Points = linePoints(days, (d) => d.f1);
  const qualityPoints = linePoints(days, (d) => (d.summary_quality_avg !== null ? d.summary_quality_avg / 10 : null));

  const lastF1Index = [...days].reverse().findIndex((d) => d.f1 !== null);
  const f1EndIndex = lastF1Index === -1 ? null : days.length - 1 - lastF1Index;
  const lastQualityIndex = [...days].reverse().findIndex((d) => d.summary_quality_avg !== null);
  const qualityEndIndex = lastQualityIndex === -1 ? null : days.length - 1 - lastQualityIndex;

  function indexFromClientX(clientX: number): number {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return 0;
    const relX = (clientX - rect.left) / rect.width;
    const virtualX = relX * WIDTH;
    const idx = Math.round(((virtualX - PAD) / (WIDTH - PAD * 2)) * (days.length - 1));
    return Math.max(0, Math.min(days.length - 1, idx));
  }

  const hovered = hoverIndex !== null ? days[hoverIndex] : null;
  const tooltipLeftPct = hoverIndex !== null ? Math.max(8, Math.min(92, (xFor(hoverIndex, days.length) / WIDTH) * 100)) : 0;

  return (
    <div>
      {!hasData ? (
        <p className="muted">No quality-check runs yet in this window.</p>
      ) : (
        <div style={{ position: "relative" }}>
          {hovered && (
            <div
              style={{
                position: "absolute",
                left: `${tooltipLeftPct}%`,
                top: 0,
                transform: "translate(-50%, -100%)",
                background: "var(--panel)",
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)",
                boxShadow: "var(--shadow-md)",
                padding: "8px 10px",
                fontSize: 12,
                lineHeight: 1.5,
                whiteSpace: "nowrap",
                pointerEvents: "none",
                zIndex: 1,
              }}
            >
              <div style={{ color: "var(--text)", fontWeight: 600, marginBottom: 3 }}>{formatDay(hovered.day)}</div>
              <div style={{ color: "var(--text-secondary)" }}>
                <span style={{ display: "inline-block", width: 10, height: 2, background: F1_COLOR, marginRight: 5 }} />
                F1{" "}
                <strong style={{ color: "var(--text)" }}>{hovered.f1 !== null ? hovered.f1.toFixed(2) : "—"}</strong>
                {hovered.f1 !== null && (
                  <span>
                    {" "}
                    (P {hovered.precision?.toFixed(2) ?? "—"}, R {hovered.recall?.toFixed(2) ?? "—"})
                  </span>
                )}
              </div>
              <div style={{ color: "var(--text-secondary)" }}>
                <span
                  style={{
                    display: "inline-block", width: 10, height: 2, background: QUALITY_COLOR, marginRight: 5,
                  }}
                />
                Summary quality{" "}
                <strong style={{ color: "var(--text)" }}>
                  {hovered.summary_quality_avg !== null ? `${hovered.summary_quality_avg.toFixed(1)}/10` : "—"}
                </strong>
              </div>
              <div style={{ color: "var(--text-secondary)", marginTop: 2 }}>{hovered.sample_size} email(s) sampled</div>
            </div>
          )}

          <svg
            ref={svgRef}
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            width="100%"
            height={HEIGHT}
            preserveAspectRatio="none"
            role="img"
            aria-label="7-day quality trend"
            onMouseMove={(e) => setHoverIndex(indexFromClientX(e.clientX))}
            onMouseLeave={() => setHoverIndex(null)}
            style={{ cursor: "crosshair" }}
          >
            <line x1={PAD} y1={HEIGHT - PAD} x2={WIDTH - PAD} y2={HEIGHT - PAD} stroke="var(--border)" />
            <line x1={PAD} y1={PAD} x2={WIDTH - PAD} y2={PAD} stroke="var(--border)" strokeDasharray="2 3" />

            {hoverIndex !== null && (
              <line
                x1={xFor(hoverIndex, days.length)} x2={xFor(hoverIndex, days.length)}
                y1={PAD} y2={HEIGHT - PAD}
                stroke="var(--text-secondary)" strokeDasharray="2 2" opacity={0.5}
              />
            )}

            <polyline points={f1Points} fill="none" stroke={F1_COLOR} strokeWidth={2} />
            <polyline points={qualityPoints} fill="none" stroke={QUALITY_COLOR} strokeWidth={2} strokeDasharray="4 3" />

            {f1EndIndex !== null && (
              <text
                x={xFor(f1EndIndex, days.length)} y={yFor(days[f1EndIndex].f1 as number) - 8}
                textAnchor="end" fontSize={11} fill="var(--text-secondary)"
              >
                {(days[f1EndIndex].f1 as number).toFixed(2)}
              </text>
            )}
            {qualityEndIndex !== null && (
              <text
                x={xFor(qualityEndIndex, days.length)}
                y={yFor((days[qualityEndIndex].summary_quality_avg as number) / 10) + 16}
                textAnchor="end" fontSize={11} fill="var(--text-secondary)"
              >
                {(days[qualityEndIndex].summary_quality_avg as number).toFixed(1)}/10
              </text>
            )}

            {days.map((d, i) => {
              const isHovered = i === hoverIndex;
              return (
                <g key={d.day}>
                  {d.f1 !== null && <circle cx={xFor(i, days.length)} cy={yFor(d.f1)} r={isHovered ? 5 : 3} fill={F1_COLOR} stroke="var(--panel)" strokeWidth={isHovered ? 2 : 0} />}
                  {d.summary_quality_avg !== null && (
                    <circle
                      cx={xFor(i, days.length)} cy={yFor(d.summary_quality_avg / 10)}
                      r={isHovered ? 5 : 3} fill={QUALITY_COLOR} stroke="var(--panel)" strokeWidth={isHovered ? 2 : 0}
                    />
                  )}
                </g>
              );
            })}
          </svg>
        </div>
      )}
      <div className="row" style={{ fontSize: 12, marginTop: 6, gap: 16 }}>
        <span className="row" style={{ gap: 4 }}>
          <span style={{ width: 9, height: 9, background: F1_COLOR, display: "inline-block", borderRadius: 2 }} />
          Level F1 (vs. judge)
        </span>
        <span className="row" style={{ gap: 4 }}>
          <span style={{ width: 9, height: 9, background: QUALITY_COLOR, display: "inline-block", borderRadius: 2 }} />
          Summary quality (÷10)
        </span>
      </div>
    </div>
  );
}
