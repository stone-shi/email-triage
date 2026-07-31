import { useCallback, useEffect, useState } from "react";
import { PromptEntry, prompts as promptsApi, ApiError } from "../lib/api";

const SOURCE_LABEL: Record<PromptEntry["source"], string> = {
  database: "Customized",
  "prompts.yml": "From prompts.yml",
  default: "Built-in default",
};

function PromptCard({ promptKey, entry, onChanged }: { promptKey: string; entry: PromptEntry; onChanged: () => void }) {
  const [value, setValue] = useState(entry.value);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    setValue(entry.value);
  }, [entry.value]);

  const dirty = value !== entry.value;

  async function handleSave() {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await promptsApi.update(promptKey, value);
      setSaved(true);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to save prompt");
    } finally {
      setBusy(false);
    }
  }

  async function handleReset() {
    if (!window.confirm(`Reset "${entry.label}" to its default (or prompts.yml) text? This discards any customization.`)) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await promptsApi.reset(promptKey);
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to reset prompt");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <div className="row between" style={{ alignItems: "flex-start" }}>
        <div>
          <h3 style={{ marginBottom: 2 }}>{entry.label}</h3>
          <p className="muted" style={{ marginTop: 0, fontSize: 12.5 }}>
            {entry.description}
          </p>
        </div>
        <span className={`badge ${entry.source === "database" ? "admin" : "warn"}`}>{SOURCE_LABEL[entry.source]}</span>
      </div>
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        rows={8}
        style={{ width: "100%", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 12.5 }}
      />
      {error && <p className="error-text">{error}</p>}
      {saved && !dirty && <p className="success-text">Saved.</p>}
      <div className="row" style={{ marginTop: 10 }}>
        <button className="primary" onClick={handleSave} disabled={busy || !dirty}>
          Save
        </button>
        <button onClick={handleReset} disabled={busy || entry.source !== "database"}>
          Reset to default
        </button>
      </div>
    </div>
  );
}

export function AdminPromptsPage() {
  const [entries, setEntries] = useState<Record<string, PromptEntry> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const { prompts: list } = await promptsApi.list();
      setEntries(list);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to load prompts");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (error) return <p className="error-text">{error}</p>;
  if (!entries) return <p className="muted">Loading…</p>;

  return (
    <div>
      <p className="muted" style={{ marginTop: 0, marginBottom: 16 }}>
        System prompts sent to the LLM at each triage stage. Changes apply immediately to every user's pipeline —
        there is one shared set of prompts, not one per user.
      </p>
      {Object.entries(entries).map(([key, entry]) => (
        <PromptCard key={key} promptKey={key} entry={entry} onChanged={load} />
      ))}
    </div>
  );
}
