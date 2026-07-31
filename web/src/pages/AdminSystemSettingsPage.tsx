import { useCallback, useEffect, useState } from "react";
import { SettingEntry, settings as settingsApi, ApiError } from "../lib/api";

const SECTIONS: { title: string; keys: string[] }[] = [
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
    title: "OAuth / deployment",
    keys: [
      "public_base_url",
      "google_client_id", "google_client_secret",
      "zoho_client_id", "zoho_client_secret", "zoho_dc",
      "log_level",
    ],
  },
];

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
      onChange={(e) => onChange(entry.type === "int" || entry.type === "float" ? Number(e.target.value) : e.target.value)}
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
      if (key in dirty) values[key] = dirty[key];
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
          {section.keys.map((key) => {
            const entry = all[key];
            if (!entry) return null;
            const currentValue = key in dirty ? dirty[key] : entry.value;
            return (
              <div className="field" key={key}>
                <label>{key}</label>
                <SettingField
                  entry={entry}
                  value={currentValue}
                  onChange={(v) => setDirty((prev) => ({ ...prev, [key]: v }))}
                />
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
