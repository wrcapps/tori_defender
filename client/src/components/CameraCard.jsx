import React, { useMemo } from "react";
import LiveCanvas from "./LiveCanvas.jsx";
import StatusPill from "./StatusPill.jsx";
import { deriveKind } from "../lib/grouping.js";
import "./CameraCard.css";

function ageText(age) {
  if (age == null) return "—";
  if (age < 1) return "just now";
  if (age < 60) return `${age.toFixed(0)}s ago`;
  return `${Math.round(age / 60)}m ago`;
}

// A tile defaults to a still thumbnail (/thumb/, no capture side-effect) and
// only opens a live connection (/stream/) once someone explicitly asks for
// it here, or it's swept into "watch all" (SiteDetail). Rendering a live
// <img> for every camera on a site the moment its page loads -- the earlier
// version of this component did exactly that -- silently started real
// capture for every camera nobody asked to watch, which is exactly the
// behavior this app's own README says the whole design exists to avoid.
export default function CameraCard({
  camera, canRecord, onToggleRecording, recordingBusy, showDiagnostics,
  previewing, onTogglePreview, pollTick,
}) {
  const kind = useMemo(() => deriveKind(camera), [camera]);

  function togglePreview(e) {
    // This card sits inside a <Link> (the site detail page's mast grid) that
    // navigates to the camera's own page -- without stopping propagation,
    // this click would also follow that link.
    e.preventDefault();
    e.stopPropagation();
    onTogglePreview(camera.name);
  }

  return (
    <div className={`cam-card kind-${kind}`}>
      <div className="cam-card-media">
        {previewing ? (
          <LiveCanvas camera={camera.name} alt={`Live view from ${camera.name}`} />
        ) : (
          // A still frame, cache-busted by the shared poll tick so it visibly
          // advances -- but it is only ever what some *other* already-running
          // process last wrote; this request itself starts nothing.
          <img
            className="cam-card-thumb"
            src={`/thumb/${encodeURIComponent(camera.name)}.jpg?t=${pollTick}`}
            alt={`Last known frame from ${camera.name}`}
          />
        )}

        {camera.recording && (
          <span className="cam-card-rec" title="Recording">
            <span className="cam-card-rec-dot" aria-hidden="true" /> REC
          </span>
        )}

        <button type="button" className="cam-card-preview-toggle" onClick={togglePreview}>
          {previewing ? "Stop preview" : "Preview"}
        </button>
      </div>

      <div className="cam-card-meta">
        <div className="cam-card-heading">
          <span className="cam-card-name">{camera.name}</span>
          <span className="cam-card-site">{camera.site}</span>
        </div>
        <div className="cam-card-row">
          <StatusPill kind={kind} detail={showDiagnostics ? (camera.error || camera.warning) : null} />
          <span className="cam-card-age">updated {ageText(camera.age)}</span>
        </div>
        {/* Raw backend error text (sometimes a config file path, an
            operator-facing instruction like "pass --weights to app.py") is
            diagnostic detail for whoever can act on it -- an operator -- and
            has no business reaching a client's screen, which just needs to
            know the feed isn't available right now. */}
        {showDiagnostics && camera.error && <div className="cam-card-error">{camera.error}</div>}

        {canRecord && (
          <button
            type="button"
            className={`cam-card-record ${camera.recording ? "is-on" : ""}`}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onToggleRecording(camera.name, !camera.recording);
            }}
            disabled={recordingBusy}
          >
            {camera.recording ? "Stop recording" : "Record"}
          </button>
        )}
        {/* Only shown once the camera has actually started and confirmed it
            has no model loaded -- not pre-emptively disabling Record before
            that's known, since a camera that hasn't started yet may well get
            detection once it does (this just reflects reality, it doesn't
            predict it). */}
        {camera.recording && camera.running && !camera.detecting && (
          <div className="cam-card-hint">
            Recording is on, but this camera is video-only right now — nothing
            will be saved until detection is available.
          </div>
        )}
      </div>
    </div>
  );
}
