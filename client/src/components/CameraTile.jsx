import React, { useState } from "react";
import { Link } from "react-router-dom";
import StatusPill from "./StatusPill.jsx";
import { deriveKind } from "../lib/grouping.js";
import "./CameraTile.css";

// The default "security wall" tile: always a still thumbnail (/thumb/, no
// capture side-effect -- same discipline CameraCard.jsx documents), never a
// live stream. Clicking anywhere on the tile "focuses" it by navigating to
// its own page (CameraDetail), which is where an actual live connection and
// the rest of that camera's metadata live. That keeps this grid safe to
// render for every camera on a site at once, exactly like today's mast grid.
export default function CameraTile({ camera, activity, pollTick }) {
  const kind = deriveKind(camera);
  const [imgFailed, setImgFailed] = useState(false);

  return (
    <Link
      to={`/app/live/${encodeURIComponent(camera.site)}/camera/${encodeURIComponent(camera.name)}`}
      className={`cwall-tile kind-${kind}`}
    >
      {!imgFailed ? (
        <img
          key={kind === "error" ? "err" : "ok"}
          src={`/thumb/${encodeURIComponent(camera.name)}.jpg?t=${pollTick}`}
          alt={`Last known frame from ${camera.name}`}
          onError={() => setImgFailed(true)}
        />
      ) : (
        <div className="cwall-tile-fallback"><span>No frame yet</span></div>
      )}

      <div className="cwall-tile-top">
        {camera.recording && (
          <span className="cwall-tile-rec"><span className="cwall-tile-rec-dot" /> REC</span>
        )}
        {activity && (
          <span className="cwall-tile-activity" title={activity.title}>{activity.label}</span>
        )}
      </div>

      <div className="cwall-tile-bottom">
        <span className="cwall-tile-name">{camera.name}</span>
        <StatusPill kind={kind} />
      </div>
    </Link>
  );
}
