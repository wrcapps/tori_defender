import React from "react";
import CameraTile from "./CameraTile.jsx";
import { formatWhen } from "../lib/format.js";
import "./CameraWall.css";

// The default "security wall" view of a site: every camera as one uniform
// grid (no mast grouping), each tile a still thumbnail. The physical-layout
// view (MastMap) stays one click away for whoever needs it -- this is the
// at-a-glance one for "what's happening right now, on any camera".
export default function CameraWall({ cameras, recentByCamera, pollTick }) {
  return (
    <div className="cwall-grid">
      {cameras.map((camera) => {
        const recent = recentByCamera?.[camera.name];
        const activity = recent
          ? {
              label: recent.species ? recent.species : "bird detected",
              title: `${recent.species || "unconfirmed detection"} — ${formatWhen(recent.day, recent.window)}`,
            }
          : null;
        return (
          <CameraTile key={camera.name} camera={camera} activity={activity} pollTick={pollTick} />
        );
      })}
    </div>
  );
}
