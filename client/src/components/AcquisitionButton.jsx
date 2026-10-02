import React, { useState } from "react";
import { useAuth } from "../AuthContext.jsx";
import "./AcquisitionButton.css";

function fmt(v, unit = "") {
  return v == null ? "—" : `${v}${unit}`;
}

// Header switch for live acquisition: ONE background service that watches every camera,
// detects and raises notifications. It is deliberately not the Record button -- recording
// (saving frames) is per camera and stays exactly where it was.
export default function AcquisitionButton({ status, error, reconnecting, busy, onToggle }) {
  const { role } = useAuth();
  const [open, setOpen] = useState(false);
  const canToggle = role === "operator";

  let tone = "off", label = "Acquisition off";
  if (status === null) { tone = "wait"; label = "Acquisition…"; }
  else if (reconnecting) { tone = "warn"; label = "Reconnecting…"; }
  else if (status.error) { tone = "bad"; label = "Acquisition failed"; }
  else if (status.enabled && status.starting) { tone = "wait"; label = "Starting…"; }
  else if (status.enabled && status.running) { tone = "on"; label = "Acquisition on"; }
  else if (status.enabled) { tone = "wait"; label = "Starting…"; }
  else if (status.running) { tone = "partial"; label = "Acquisition off"; }

  const s = status || {};
  const sub = s.enabled && s.running && s.cameras_active != null
    ? `${s.cameras_active}/${s.cameras_configured} cameras`
    : (s.running ? `${s.cameras_active ?? 0} watched/recording` : null);

  return (
    <div className="acq">
      <button
        type="button"
        className={`acq-toggle acq-${tone}`}
        disabled={!canToggle || busy || status === null}
        aria-pressed={!!s.enabled}
        onClick={() => onToggle(!s.enabled)}
        title={canToggle
          ? (s.enabled ? "Stop acquisition on all cameras (recording is not changed)"
                       : `Start acquisition on all ${s.cameras_configured ?? ""} cameras: detection + notifications (recording is not changed)`)
          : "Operator only"}
      >
        <span className="acq-dot" aria-hidden="true" />
        <span className="acq-label">{busy ? "Working…" : label}</span>
        {sub && <span className="acq-sub">{sub}</span>}
      </button>
      <button type="button" className="acq-more" aria-expanded={open} aria-label="acquisition details"
              onClick={() => setOpen((v) => !v)}>&#9662;</button>

      {open && (
        <div className="acq-panel" role="dialog" aria-label="Acquisition details">
          {error && <div className="acq-msg bad">{error}</div>}
          {s.error && <div className="acq-msg bad">{s.error}</div>}
          {s.warning && <div className="acq-msg warn">{s.warning}</div>}
          {s.detection_note && <div className="acq-msg warn">{s.detection_note}</div>}
          {!s.running ? (
            <div className="acq-note">
              Not running. Enabling it connects every camera (about 6 Mbit/s each) and runs
              detection with one shared model. Recording stays a per-camera choice.
            </div>
          ) : (
            <dl className="acq-stats">
              <dt>cameras</dt><dd>{fmt(s.cameras_active)} of {fmt(s.cameras_configured)} (~{fmt(s.est_mbit_s)} Mbit/s)</dd>
              <dt>sources</dt>
              <dd>
                live {fmt(s.sources?.live)}
                {s.sources?.segments > 0 && <> &middot; <span className="acq-warn-text">NVR segments {s.sources.segments}</span></>}
                {s.sources?.none > 0 && <> &middot; <span className="acq-bad-text">no data (black) {s.sources.none}</span></>}
              </dd>
              {s.segment_hub && (
                <>
                  <dt>NVR</dt>
                  <dd>{s.segment_hub.reachable === false ? "unreachable" : "reachable"}
                    {" "}&middot; {fmt(s.segment_hub.downloaded_mib, " MiB")} pulled
                    {s.segment_hub.error ? ` · ${s.segment_hub.error.slice(0, 60)}` : ""}</dd>
                </>
              )}
              <dt>per round</dt><dd>{fmt(s.cameras_per_round)} cameras, {fmt(s.frames_per_round)} inferred</dd>
              <dt>round time</dt><dd>p50 {fmt(s.round_ms?.p50, " ms")} · p95 {fmt(s.round_ms?.p95, " ms")}</dd>
              <dt>capture sync</dt><dd>p50 {fmt(s.capture_skew_ms?.p50, " ms")} · p95 {fmt(s.capture_skew_ms?.p95, " ms")}</dd>
              <dt>frame age</dt><dd>p50 {fmt(s.frame_age_ms?.p50, " ms")} · p95 {fmt(s.frame_age_ms?.p95, " ms")}</dd>
              <dt>GPU memory</dt>
              <dd>{s.vram?.torch_peak_mib != null ? `${s.vram.torch_peak_mib} MiB peak` : "—"}
                {s.vram?.gpu_free_mib != null ? ` · ${s.vram.gpu_free_mib} MiB free` : ""}</dd>
              <dt>model</dt><dd>{s.model?.type || "—"}{s.detecting ? "" : " (detection off)"}</dd>
            </dl>
          )}
          {s.auto_stopped_at && !s.enabled && (
            <div className="acq-msg warn">
              Switched off automatically at {new Date(s.auto_stopped_at * 1000).toLocaleTimeString()}:
              no page was open for {s.idle_stop_minutes} min.
            </div>
          )}
          {s.enabled && s.idle_stop_minutes > 0 && (
            <div className="acq-note">Stops by itself {s.idle_stop_minutes} min after the last page is closed.</div>
          )}
          {s.recording?.length > 0 && (
            <div className="acq-note">Recording: {s.recording.join(", ")}</div>
          )}
        </div>
      )}
    </div>
  );
}
