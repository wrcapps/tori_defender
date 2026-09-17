import React, { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useCameras } from "../hooks/useCameras.js";
import { useAuth } from "../AuthContext.jsx";
import { api } from "../api.js";
import { deriveKind } from "../lib/grouping.js";
import StatusPill from "../components/StatusPill.jsx";
import "./CameraDetail.css";

function ageText(age) {
  if (age == null) return "—";
  if (age < 1) return "just now";
  if (age < 60) return `${age.toFixed(0)}s ago`;
  return `${Math.round(age / 60)}m ago`;
}

export default function CameraDetail() {
  const { site, name } = useParams();
  const { role } = useAuth();
  const { rows, error } = useCameras();
  const [busy, setBusy] = useState(false);
  const [imgFailed, setImgFailed] = useState(false);

  if (rows === null && !error) {
    return <div className="state-page skeleton-block" style={{ height: 420 }} />;
  }

  const camera = (rows || []).find((c) => c.site === site && c.name === name);
  if (!camera) {
    return (
      <div className="state-page">
        <h2>Camera not found</h2>
        <p>"{name}" isn't in "{site}" right now.</p>
        <Link to={`/app/live/${encodeURIComponent(site)}`}>Back to {site}</Link>
      </div>
    );
  }

  const kind = deriveKind(camera);

  async function toggleRecording() {
    setBusy(true);
    try {
      await api.post("/api/recording", { name: camera.name, enabled: !camera.recording });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="camera-detail">
      <div className="camera-detail-head">
        <Link to={`/app/live/${encodeURIComponent(site)}`} className="camera-detail-back">
          ← {site}
        </Link>
        <h2 className="camera-detail-title">{camera.name}</h2>
        <StatusPill kind={kind} detail={role === "operator" ? (camera.error || camera.warning) : null} />
      </div>

      <div className="camera-detail-layout">
        <div className="camera-detail-media">
          {!imgFailed ? (
            <img
              key={kind === "error" ? "err" : "ok"}
              src={`/stream/${encodeURIComponent(camera.name)}.mjpg`}
              alt={`Live view from ${camera.name}`}
              onError={() => setImgFailed(true)}
            />
          ) : (
            <div className="camera-detail-fallback">Stream unavailable</div>
          )}
          {camera.recording && (
            <span className="camera-detail-rec"><span className="camera-detail-rec-dot" /> REC</span>
          )}
        </div>

        <aside className="camera-detail-info">
          <span className="tick-label">Camera info</span>
          <dl className="info-list">
            <div><dt>Site</dt><dd>{camera.site}</dd></div>
            <div><dt>Mast</dt><dd>{camera.pair != null ? camera.pair : "—"}</dd></div>
            <div><dt>Updated</dt><dd>{ageText(camera.age)}</dd></div>
            {camera.running && (
              <div><dt>Detection</dt><dd>{camera.detecting ? "Running" : "Off"}</dd></div>
            )}
          </dl>
          {role === "operator" && camera.running && !camera.detecting && camera.detection_note && (
            <div className="info-issue">
              <span className="tick-label">Why no detection</span>
              <p>{camera.detection_note}</p>
            </div>
          )}
          {/* Diagnostic detail (often a server file path or an
              operator-facing instruction) is operator-only -- see the same
              reasoning in CameraCard.jsx. Its own block, not another
              <dl> row: this text runs to several lines and a label/value
              flex row (right for the short rows above) clips or overflows
              it instead of wrapping. */}
          {role === "operator" && camera.error && (
            <div className="info-issue">
              <span className="tick-label">Issue</span>
              <p>{camera.error}</p>
            </div>
          )}

          {role === "operator" && (
            <button
              type="button"
              className={`camera-detail-record ${camera.recording ? "is-on" : ""}`}
              onClick={toggleRecording}
              disabled={busy}
            >
              {camera.recording ? "Stop recording" : "Record"}
            </button>
          )}
          {role === "operator" && camera.recording && camera.running && !camera.detecting && (
            <p className="info-hint">
              Recording is on, but this camera is video-only right now —
              nothing will be saved until detection is available.
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}
