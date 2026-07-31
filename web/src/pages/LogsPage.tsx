import { useEffect, useRef, useState } from "react";
import { logsApi } from "../lib/api";

const MAX_LINES = 500;

export function LogsPage() {
  const [lines, setLines] = useState<string[]>([]);
  const [live, setLive] = useState(true);
  const consoleRef = useRef<HTMLDivElement>(null);
  const lastSeqRef = useRef(0);

  useEffect(() => {
    const source = new EventSource("/api/logs/stream");
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
    source.onmessage = (event) => {
      appendLine(event.data);
    };
    return () => source.close();
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
  }, [live]);

  function appendLine(line: string) {
    setLines((prev) => {
      const next = [...prev, line];
      return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
    });
  }

  useEffect(() => {
    const el = consoleRef.current;
    if (el && el.scrollTop + el.clientHeight >= el.scrollHeight - 20) {
      el.scrollTop = el.scrollHeight;
    }
  }, [lines]);

  return (
    <div>
      <div className="row between">
        <h1>Logs</h1>
        <div className="row">
          <span className={`badge ${live ? "ok" : "warn"}`}>{live ? "live" : "polling"}</span>
          <button onClick={() => setLines([])}>Clear</button>
        </div>
      </div>
      <div className="log-console" ref={consoleRef}>
        {lines.join("\n")}
      </div>
    </div>
  );
}
