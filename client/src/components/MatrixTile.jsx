import React from "react";
import LiveCanvas from "./LiveCanvas.jsx";
import StatusPill from "./StatusPill.jsx";
import { deriveKind } from "../lib/grouping.js";
import "./MatrixTile.css";

// Deliberately minimal: this tile exists only on the Matrix (360°) page,
// where every camera is already, explicitly, being watched -- no preview
// toggle (there's nothing to opt into, that decision was the click that got
// here) and no Record button (recording is a per-camera decision made from
// its own page or the site grid, not a wall-of-video action).
export default function MatrixTile({ camera }) {
  const kind = deriveKind(camera);

  return (
    <div className={`matrix-tile kind-${kind}`}>
      <LiveCanvas camera={camera.name} alt={`Live view from ${camera.name}`} />
      {camera.recording && (
        <span className="matrix-tile-rec"><span className="matrix-tile-rec-dot" /> REC</span>
      )}
      <div className="matrix-tile-label">
        <span className="matrix-tile-name">{camera.name}</span>
        <StatusPill kind={kind} />
      </div>
    </div>
  );
}
