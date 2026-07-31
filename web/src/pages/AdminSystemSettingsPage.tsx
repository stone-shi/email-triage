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
    title: "Triage router",
    keys: [
      "triage.confidence_threshold", "triage.triage_type",
      "tei_url", "tei_model", "tei_api_key",
      "triage.tei_router_enabled", "triage.tei_noise_enabled", "triage.tei_signal_enabled",
      "triage.tei_noise_threshold", "triage.tei_signal_threshold",
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
          <button className="primary" onClick={() => saveSection(section.title, section.keys)}>
            Save
          </button>
        </div>
      ))}
    </div>
  );
}
