import React from "react";
import { useDataDirCheck } from "../hooks/useSettings.js";
import "./SettingsSections.css";

// The three choices, shared by the Settings page (all at once) and the first-run setup (one per step).

export function ModelSection({ state, value, onChange }) {
  const { models, models_dir: dir, config_model: configModel, model_missing: missing } = state;
  return (
    <section className="set-section" aria-labelledby="set-model">
      <h2 id="set-model" className="set-title">Model</h2>
      <p className="set-help">Detection models found in <code>{dir}</code>. Drop a <code>.pt</code> (YOLO) or <code>.pth</code> (RF-DETR) file there and reload this page.</p>
      {missing && (
        <p className="set-note warn" role="alert">
          The model chosen earlier ({state.settings.model}) is no longer in that folder; the configured one is used instead.
        </p>
      )}
      <div className="set-options" role="radiogroup" aria-labelledby="set-model">
        {models.map((m) => (
          <Option key={m.file} checked={value === m.file} onSelect={() => onChange(m.file)}
                  title={m.label}
                  meta={`${m.type === "rfdetr" ? `RF-DETR ${m.variant}` : "YOLO"} · ${m.size_mb} MB${m.label !== m.file ? ` · ${m.file}` : ""}`} />
        ))}
        {configModel && (
          <Option checked={value === ""} onSelect={() => onChange("")}
                  title="As configured (config.yaml)" meta={configModel} />
        )}
      </div>
      {models.length === 0 && (
        <p className="set-note" role="status">
          No model files yet. {configModel ? "Detection keeps using the configured model above." : "Until you add one, cameras run video-only with no detection."}
        </p>
      )}
    </section>
  );
}

const SITE_STATE = {
  present: "found",
  will_create: "will be created",
  wrong_case: "wrong capitalisation",
};

export function DataFolderSection({ state, value, onChange }) {
  const { result, checking, failed } = useDataDirCheck(value);
  const info = state.data_dir;
  const win = state.platform === "windows";
  return (
    <section className="set-section" aria-labelledby="set-data">
      <h2 id="set-data" className="set-title">Data folder (NAS)</h2>
      <p className="set-help">
        Where captured footage, detections and your review decisions are read from and saved to.
        Point this at the NAS share{win ? " (a mapped drive such as Z:\\Dataset, or a \\\\server\\share\\Dataset path)" : " once it is mounted"}. Leave empty for the app's own folder.
      </p>
      <label className="set-field">
        <span className="visually-hidden">Data folder path</span>
        <input type="text" className="set-input" value={value} spellCheck={false}
               placeholder={win ? "Z:\\Dataset   or   \\\\nas\\share\\Dataset" : "/mnt/Tori/Dataset"}
               onChange={(e) => onChange(e.target.value)} aria-describedby="set-data-status" />
      </label>
      <div id="set-data-status" className="set-status" aria-live="polite">
        {checking && <span className="set-note">Checking folder…</span>}
        {!checking && failed && <span className="set-note warn">Can't reach the server to check this folder.</span>}
        {!checking && result && !result.ok && <span className="set-note bad" role="alert">{result.error}</span>}
        {!checking && result?.ok && (
          <>
            <span className="set-note ok">Folder found{result.writable ? " and writable" : " but read-only"}.</span>
            {result.sites.length > 0 && (
              <ul className="set-sites">
                {result.sites.map((s) => (
                  <li key={s.name} className={s.state === "wrong_case" ? "bad" : ""}>
                    <strong>{s.name}</strong> — {SITE_STATE[s.state]}
                  </li>
                ))}
              </ul>
            )}
            {result.warnings.map((w) => <span key={w} className="set-note warn" role="alert">{w}</span>)}
          </>
        )}
        {!value.trim() && <span className="set-note">Currently using <code>{info.active}</code> ({info.source}).</span>}
      </div>
      {info.overridden && (
        <p className="set-note warn" role="status">
          The app was started with an explicit <code>--sites-root</code>, which takes priority over this setting.
        </p>
      )}
      {info.restart_needed && (
        <p className="set-note warn" role="status">
          Saved, but the app is still using <code>{info.active}</code> — restart it to switch.
        </p>
      )}
    </section>
  );
}

function Option({ checked, onSelect, title, meta, disabled }) {
  return (
    <button type="button" role="radio" aria-checked={checked} disabled={disabled}
            className={`set-option ${checked ? "is-checked" : ""}`} onClick={onSelect}>
      <span className="set-radio" aria-hidden="true" />
      <span className="set-option-text">
        <span className="set-option-title">{title}</span>
        <span className="set-option-meta">{meta}</span>
      </span>
    </button>
  );
}
