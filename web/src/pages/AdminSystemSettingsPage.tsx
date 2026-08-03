import { useCallback, useEffect, useState } from "react";
import { SettingEntry, settings as settingsApi, ApiError } from "../lib/api";

const SECTIONS: { title: string; description?: string; keys: string[] }[] = [
  {
    title: "LLM endpoints",
    keys: [
      "triage_base_url", "triage_model", "triage_api_key",
      "summary_base_url", "summary_model", "summary_api_key",
    ],
  },
  {
    title: "Triage & rerank filter",
    description:
      "The rerank filter is a cheap, high-precision noise gate that runs before the Level 1 LLM call -- " +
      "it only ever short-circuits obvious noise straight to Level 0; anything else always falls through " +
      "to a real Level 1 classification. Off by default.",
    keys: [
      "triage.confidence_threshold", "triage.triage_type",
      "tei_url", "tei_model", "tei_api_key",
      "triage.tei_router_enabled", "triage.tei_noise_enabled", "triage.tei_noise_threshold",
      "triage.tei_score_normalize",
    ],
  },
  {
    title: "Scheduler",
    keys: [
      "scheduler.enabled", "scheduler.interval", "scheduler.max_per_account", "scheduler.days",
      "download_all_scheduler.enabled", "download_all_scheduler.interval",
    ],
  },
  {
    title: "Auto mark-read",
    keys: [
      "auto_mark_read.level_0.enabled", "auto_mark_read.level_0.after_displays",
      "auto_mark_read.level_1.enabled", "auto_mark_read.level_1.after_displays",
      "auto_mark_read.level_2.enabled", "auto_mark_read.level_2.after_displays",
    ],
  },
  {
    title: "Quality check",
    description:
      "Nightly \"no-look\" audit: a random sample of already-triaged emails is independently re-classified " +
      "(and, for summarized emails, quality-graded) by a separate judge model, so you can track triage " +
      "accuracy over time without reading every email yourself. Off by default -- it costs judge-model " +
      "tokens, and needs a judge endpoint/model configured below before it does anything.",
    keys: [
      "quality_check.enabled", "quality_check.hour", "quality_check.minute", "quality_check.sample_rate",
      "quality_check.judge_base_url", "quality_check.judge_model", "quality_check.judge_api_key",
    ],
  },
  {
    title: "OAuth / deployment",
    keys: [
      "public_base_url",
      "google_client_id", "google_client_secret",
      "zoho_client_id", "zoho_client_secret", "zoho_dc",
      "log_level",
    ],
  },
];

// Friendlier labels/help text for keys that are easy to misread as raw config
// names (dotted paths, ambiguous units). Keys not listed here just show their
// raw name with no help line, as before.
const FIELD_LABELS: Record<string, string> = {
  "quality_check.enabled": "Enable nightly quality check",
  "quality_check.sample_rate": "Sample rate",
  "quality_check.judge_base_url": "Judge LLM base URL",
  "quality_check.judge_model": "Judge LLM model",
  "quality_check.judge_api_key": "Judge LLM API key",
  "triage.tei_router_enabled": "Enable rerank noise filter",
  "triage.tei_noise_enabled": "Noise filter active",
  "triage.tei_noise_threshold": "Noise score threshold",
  "triage.tei_score_normalize": "Normalize raw reranker scores (sigmoid)",
  "tei_url": "Reranker URL",
  "tei_model": "Reranker model",
  "tei_api_key": "Reranker API key",
};

// Per-section "Test connection" buttons: each fires a trivial live call against
// the endpoint built from the field group's keys (falling back to the saved
// value for any key not currently edited) and reports ok/error inline.
const TEST_GROUPS: Record<
  string,
  { label: string; kind: "triage" | "summary" | "tei" | "quality_judge"; keys: string[] }[]
> = {
  "LLM endpoints": [
    { label: "Test triage connection", kind: "triage", keys: ["triage_base_url", "triage_model", "triage_api_key"] },
    { label: "Test summary connection", kind: "summary", keys: ["summary_base_url", "summary_model", "summary_api_key"] },
  ],
  "Triage & rerank filter": [
    { label: "Test reranker connection", kind: "tei", keys: ["tei_url", "tei_model", "tei_api_key"] },
  ],
  "Quality check": [
    {
      label: "Test judge connection",
      kind: "quality_judge",
      keys: ["quality_check.judge_base_url", "quality_check.judge_model", "quality_check.judge_api_key"],
    },
  ],
};

const FIELD_HELP: Record<string, string> = {
  "quality_check.enabled":
    "When on, the nightly audit runs automatically at the time below. Requires a judge base URL and model to be set, or it will fail on each run.",
  "quality_check.sample_rate":
    "A fraction between 0 and 1, not a percent or a count -- 0.1 means 10%. Entering 10 would mean 1000% " +
    "(sample everything). Applied per triage level (0/1/2) against all of a user's accounts combined, with " +
    "at least 1 message sampled from each level that has any -- e.g. 20/50/30 emails at 0.1 becomes a " +
    "2/5/3 sample, not just 10 random emails that could miss a whole level.",
  "quality_check.judge_base_url":
    "OpenAI-compatible /chat/completions endpoint the judge model is called on. Use a different -- ideally stronger -- model/provider than your triage or summary models, so it's a genuine second opinion rather than the same model grading itself.",
  "quality_check.judge_model":
    "Model name sent to the judge endpoint above.",
  "quality_check.judge_api_key":
    "API key for the judge endpoint. Leave blank when saving to keep the currently stored key unchanged.",
  "triage.tei_router_enabled":
    "When on, every email is scored against a single \"noise\" anchor via the reranker below before " +
    "the Level 1 LLM call. Only ever short-circuits confidently-noise mail to Level 0 for free -- it " +
    "never escalates, so anything not confidently noise still gets a real Level 1 classification.",
  "triage.tei_noise_threshold":
    "Relevance score (from the reranker's /rerank endpoint) an email's noise score must meet or exceed " +
    "to be filtered as Level 0 without an LLM call. Reranker scores aren't calibrated the same way as an " +
    "LLM's confidence -- keep this high and bias toward precision, since a false positive here silently " +
    "drops a real email.",
  "triage.tei_score_normalize":
    "Turn this on if your reranker returns a raw, unbounded score (e.g. a plain cross-encoder logit like " +
    "4.7 or -5.6) instead of a calibrated 0-1 relevance_score the way Cohere/Jina-hosted rerank APIs do -- " +
    "this applies a sigmoid so the threshold above is comparable across requests. Leave off if your reranker " +
    "already returns scores in [0,1]; turning it on for an already-calibrated backend distorts the scores.",
  "tei_url": "OpenAI/Cohere-style /rerank endpoint (model + query + documents in, relevance scores out).",
  "tei_model": "Model name sent to the reranker endpoint above.",
  "tei_api_key": "API key for the reranker endpoint. Leave blank when saving to keep the currently stored key unchanged.",
};

function SettingField({
  entry,
  value,
  onChange,
}: {
  entry: SettingEntry;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  if (entry.type === "bool") {
    return <input type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} />;
  }
  if (entry.is_secret) {
    return (
      <input
        type="password"
        placeholder={entry.set ? "•••• (unchanged)" : "not set"}
        onChange={(e) => onChange(e.target.value)}
        style={{ width: "100%" }}
      />
    );
  }
  if (entry.type === "json") {
    return (
      <textarea
        value={Array.isArray(value) ? value.join("\n") : ""}
        onChange={(e) => onChange(e.target.value.split("\n").filter(Boolean))}
        rows={3}
        style={{ width: "100%" }}
      />
    );
  }
  return (
    <input
      value={value === null || value === undefined ? "" : String(value)}
      onChange={(e) => onChange(e.target.value)}
      inputMode={entry.type === "int" || entry.type === "float" ? "decimal" : undefined}
      style={{ width: "100%" }}
    />
  );
}

export function AdminSystemSettingsPage() {
  const [all, setAll] = useState<Record<string, SettingEntry> | null>(null);
  const [dirty, setDirty] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  const [savedSection, setSavedSection] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<
    Record<string, { status: "testing" | "ok" | "error"; message?: string }>
  >({});

  const load = useCallback(async () => {
    const { settings: entries } = await settingsApi.get();
    setAll(entries);
    setDirty({});
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function saveSection(title: string, keys: string[]) {
    const values: Record<string, unknown> = {};
    for (const key of keys) {
      if (!(key in dirty)) continue;
      const entry = all?.[key];
      let value = dirty[key];
      if (entry && (entry.type === "int" || entry.type === "float") && typeof value === "string") {
        const parsed = Number(value);
        if (value.trim() === "" || Number.isNaN(parsed)) {
          setError(`"${key}" must be a number`);
          return;
        }
        value = parsed;
      }
      values[key] = value;
    }
    if (Object.keys(values).length === 0) return;
    setError(null);
    try {
      await settingsApi.put(values);
      setSavedSection(title);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to save settings");
    }
  }

  async function runTest(kind: "triage" | "summary" | "tei" | "quality_judge", keys: string[]) {
    setTestResults((prev) => ({ ...prev, [kind]: { status: "testing" } }));
    // Use whatever's currently on-screen (edited or saved) so an admin can test
    // before hitting Save; secret fields are only sent if the admin just typed a
    // new one -- otherwise the backend falls back to the already-stored key.
    const values: Record<string, unknown> = {};
    for (const key of keys) {
      if (key in dirty) {
        values[key] = dirty[key];
        continue;
      }
      const entry = all?.[key];
      if (entry && !entry.is_secret) {
        values[key] = entry.value;
      }
    }
    try {
      const result = await settingsApi.test(kind, values);
      setTestResults((prev) => ({
        ...prev,
        [kind]: {
          status: result.ok ? "ok" : "error",
          message: result.ok ? result.detail ?? "Connection OK" : result.error ?? "Test failed",
        },
      }));
    } catch (e) {
      setTestResults((prev) => ({
        ...prev,
        [kind]: { status: "error", message: e instanceof ApiError ? e.message : "Test failed" },
      }));
    }
  }

  if (!all) return <p className="muted">Loading…</p>;

  return (
    <div>
      {error && <p className="error-text">{error}</p>}
      {savedSection && <p className="success-text">{savedSection} saved.</p>}
      {SECTIONS.map((section) => (
        <div className="card" key={section.title}>
          <h3>{section.title}</h3>
          {section.description && (
            <p className="muted" style={{ marginTop: 0, fontSize: 12.5, maxWidth: 620 }}>
              {section.description}
            </p>
          )}
          {section.keys.map((key) => {
            // Rendered as part of "quality_check.hour"'s row below, as a single time picker.
            if (key === "quality_check.minute") return null;

            const entry = all[key];
            if (!entry) return null;
            const currentValue = key in dirty ? dirty[key] : entry.value;

            if (key === "quality_check.hour") {
              const minuteEntry = all["quality_check.minute"];
              const minuteValue = "quality_check.minute" in dirty ? dirty["quality_check.minute"] : minuteEntry?.value;
              const hh = String(currentValue ?? 0).padStart(2, "0");
              const mm = String(minuteValue ?? 0).padStart(2, "0");
              return (
                <div className="field" key={key}>
                  <label>Run time (UTC)</label>
                  <input
                    type="time"
                    value={`${hh}:${mm}`}
                    onChange={(e) => {
                      const [h, m] = e.target.value.split(":");
                      setDirty((prev) => ({ ...prev, "quality_check.hour": h, "quality_check.minute": m }));
                    }}
                  />
                  <p className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                    This is UTC, not your local timezone -- convert your desired local time to UTC first. The
                    audit runs once a day at this time.
                  </p>
                </div>
              );
            }

            return (
              <div className="field" key={key}>
                <label>{FIELD_LABELS[key] ?? key}</label>
                <SettingField
                  entry={entry}
                  value={currentValue}
                  onChange={(v) => setDirty((prev) => ({ ...prev, [key]: v }))}
                />
                {FIELD_HELP[key] && (
                  <p className="muted" style={{ fontSize: 11, marginTop: 4 }}>
                    {FIELD_HELP[key]}
                  </p>
                )}
              </div>
            );
          })}
          {TEST_GROUPS[section.title]?.map((group) => {
            const result = testResults[group.kind];
            return (
              <div key={group.kind} style={{ marginBottom: 8 }}>
                <button
                  type="button"
                  onClick={() => runTest(group.kind, group.keys)}
                  disabled={result?.status === "testing"}
                >
                  {result?.status === "testing" ? "Testing…" : group.label}
                </button>
                {result && result.status !== "testing" && (
                  <span
                    className={result.status === "ok" ? "success-text" : "error-text"}
                    style={{ marginLeft: 8, fontSize: 12.5 }}
                  >
                    {result.message}
                  </span>
                )}
              </div>
            );
          })}
          <button className="primary" onClick={() => saveSection(section.title, section.keys)}>
            Save
          </button>
        </div>
      ))}
    </div>
  );
}
