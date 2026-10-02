import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { useSettings } from "../hooks/useSettings.js";
import { DataFolderSection, ModelSection } from "../components/SettingsSections.jsx";
import "./Settings.css";

const STEPS = ["Model", "Data folder"];

// First run: the same three choices as Settings, one per step. "Skip" keeps whatever config.yaml
// already says, and still records that setup was seen, so it is never shown again by surprise.
export default function Setup() {
  const s = useSettings();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);

  if (!s.state) {
    return s.error ? (
      <div className="state-page"><h2>Can't load setup</h2><p>{s.error}</p></div>
    ) : (
      <div className="setup-page"><div className="skeleton-block settings-skeleton" aria-hidden="true" /></div>
    );
  }

  const finish = async () => {
    const next = await s.save({ setup_done: true });
    if (next) navigate("/app/live", { replace: true });
  };
  const skip = async () => {
    // Only mark it seen: nothing the person did not choose is written.
    try {
      await api.saveSettings({ setup_done: true });
      navigate("/app/live", { replace: true });
    } catch {
      /* stay put; the buttons are still usable */
    }
  };

  const last = step === STEPS.length - 1;
  return (
    <div className="setup-page">
      <div className="setup-head">
        <span className="tick-label">First-time setup</span>
        <h1>Set up Bird Monitoring</h1>
        <p>Two choices. You can change any of them later under Settings.</p>
      </div>
      <ol className="setup-steps" aria-label="Progress">
        {STEPS.map((label, i) => (
          <li key={label} className={i === step ? "is-current" : i < step ? "is-done" : ""}
              aria-current={i === step ? "step" : undefined}>
            {i + 1}. {label}
          </li>
        ))}
      </ol>

      {step === 0 && <ModelSection state={s.state} value={s.draft.model} onChange={(v) => s.edit("model", v)} />}
      {step === 1 && <DataFolderSection state={s.state} value={s.draft.data_dir} onChange={(v) => s.edit("data_dir", v)} />}

      <div className="setup-nav">
        {step > 0 && <button type="button" className="setup-secondary" onClick={() => setStep(step - 1)}>Back</button>}
        <button type="button" className="setup-secondary" onClick={skip} disabled={s.saving}>
          Skip, keep current configuration
        </button>
        <span className="spacer" />
        {s.saveError && <span className="setup-error" role="alert">{s.saveError}</span>}
        {last
          ? <button type="button" className="setup-primary" onClick={finish} disabled={s.saving}>{s.saving ? "Saving…" : "Finish"}</button>
          : <button type="button" className="setup-primary" onClick={() => setStep(step + 1)}>Next</button>}
      </div>
    </div>
  );
}
