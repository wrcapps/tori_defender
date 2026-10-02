import React from "react";
import { useSettings } from "../hooks/useSettings.js";
import { DataFolderSection, ModelSection } from "../components/SettingsSections.jsx";
import "./Settings.css";

export default function Settings() {
  const s = useSettings();

  if (!s.state) {
    return s.error ? (
      <div className="state-page"><h2>Can't load settings</h2><p>{s.error}</p></div>
    ) : (
      <div className="settings-page"><div className="skeleton-block settings-skeleton" aria-hidden="true" /></div>
    );
  }
  const restartsAcquisition = "model" in s.changes;

  return (
    <div className="settings-page">
      {s.error && <div className="live-banner" role="status">Reconnecting to the server…</div>}
      <span className="tick-label">Settings</span>
      <ModelSection state={s.state} value={s.draft.model} onChange={(v) => s.edit("model", v)} />
      <DataFolderSection state={s.state} value={s.draft.data_dir} onChange={(v) => s.edit("data_dir", v)} />

      <div className="settings-actions">
        <button type="button" className="settings-save" disabled={!s.dirty || s.saving} onClick={() => s.save()}>
          {s.saving ? "Saving…" : "Save changes"}
        </button>
        <div className="settings-feedback" aria-live="polite">
          {s.saveError && <span className="bad" role="alert">{s.saveError}</span>}
          {!s.saveError && s.savedNote && !s.dirty && <span className="ok">{s.savedNote}</span>}
          {!s.saveError && s.dirty && restartsAcquisition && (
            <span>Saving restarts acquisition if it is running (detection pauses for a few seconds while the model loads).</span>
          )}
        </div>
      </div>
    </div>
  );
}
