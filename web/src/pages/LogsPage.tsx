import { useEffect, useMemo, useRef, useState } from "react";
import { logsApi } from "../lib/api";

const MAX_LINES = 500;
const LEVELS = ["ALL", "INFO", "WARNING", "ERROR", "CRITICAL"] as const;

// Matches the backend's formatter: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
const LINE_PATTERN = /^(\S+ \S+) \[(\w+)\] ([\w.]+): (.*)$/s;

interface LogRow {
  key: number;
  raw: string;
  timestamp: string | null;
  level: string | null;
  logger: string | null;
  message: string;
}

function parseLine(raw: string, key: number): LogRow {
  const match = raw.match(LINE_PATTERN);
  if (!match) {
    return { key, raw, timestamp: null, level: null, logger: null, message: raw };
  }
  const [, timestamp, level, logger, message] = match;
  return { key, raw, timestamp, level, logger, message };
}

function levelBadgeClass(level: string | null): string {
  switch (level) {
    case "ERROR":
    case "CRITICAL":
      return "error";
    case "WARNING":
      return "warn";
    default:
      return "ok";
  }
}

export function LogsPage() {
  const [rows, setRows] = useState<LogRow[]>([]);
  const [live, setLive] = useState(true);
  const [levelFilter, setLevelFilter] = useState<string>("ALL");
  const [search, setSearch] = useState("");
  const consoleRef = useRef<HTMLDivElement>(null);
  const lastSeqRef = useRef(0);
  const keyRef = useRef(0);

  function appendLine(line: string) {
    setRows((prev) => {
      const next = [...prev, parseLine(line, keyRef.current++)];
      return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
    });
  }

  useEffect(() => {
    const source = new EventSource("/api/logs/stream");
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
    source.onmessage = (event) => appendLine(event.data);
    return () => source.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (live) return;
    const poll = setInterval(async () => {
      try {
        const { logs, last_seq } = await logsApi.since(lastSeqRef.current);
        logs.forEach((entry) => appendLine(entry.line));
        lastSeqRef.current = last_seq;
      } catch {
        // keep retrying silently
      }
    }, 2000);
    return () => clearInterval(poll);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  useEffect(() => {
    const el = consoleRef.current;
    if (el && el.scrollTop + el.clientHeight >= el.scrollHeight - 40) {
      el.scrollTop = el.scrollHeight;
    }
  }, [rows]);

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    return rows.filter((row) => {
      if (levelFilter !== "ALL" && row.level !== levelFilter) return false;
      if (term && !row.raw.toLowerCase().includes(term)) return false;
      return true;
    });
  }, [rows, levelFilter, search]);

  return (
    <div>
      <div className="row between" style={{ marginBottom: 4 }}>
        <h1>Logs</h1>
        <span className={`badge ${live ? "ok" : "warn"}`}>{live ? "live" : "polling"}</span>
      </div>
      <p className="muted" style={{ marginTop: 0, marginBottom: 16 }}>
        Live application log stream — last {MAX_LINES} lines, in-memory only.
      </p>

      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <div className="log-toolbar">
          <div className="row" style={{ gap: 8 }}>
            {LEVELS.map((lvl) => (
              <button
                key={lvl}
                className={levelFilter === lvl ? "primary" : ""}
                style={{ padding: "5px 11px", fontSize: 12.5 }}
                onClick={() => setLevelFilter(lvl)}
              >
                {lvl}
              </button>
            ))}
          </div>
          <div className="row" style={{ gap: 8 }}>
            <input
              placeholder="Filter by text…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ width: 220 }}
            />
            <button onClick={() => setRows([])}>Clear</button>
          </div>
        </div>

        <div className="log-console" ref={consoleRef}>
          {filtered.length === 0 && <div className="log-empty">No log lines match.</div>}
          {filtered.map((row) => (
            <div className="log-row" key={row.key}>
              {row.level ? (
                <>
                  <span className={`badge log-level ${levelBadgeClass(row.level)}`}>{row.level}</span>
                  <span className="log-timestamp">{row.timestamp}</span>
                  <span className="log-logger">{row.logger}</span>
                  <span className="log-message">{row.message}</span>
                </>
              ) : (
                <span className="log-message log-continuation">{row.raw}</span>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
