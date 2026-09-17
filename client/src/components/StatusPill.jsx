import React from "react";
import "./StatusPill.css";

// One vocabulary for connection health, used everywhere a camera's state is
// shown (DESIGN.md W2/W6): live, starting, stale (link is up but behind --
// distinct from no-signal, which is "nothing is even trying"), no-signal,
// and error (the capture process itself failed to start, with a reason).
const KIND = {
  live: { label: "Live", tone: "accent", pulse: true },
  starting: { label: "Starting…", tone: "info", pulse: true },
  // Video is fine; there is no detection model running on it right now (no
  // weights configured for this site, or the GPU had no room for one more
  // when this camera started) -- a different fact from "stale"/"no-signal",
  // and shown as one, not folded into "Live" or a warning color that would
  // wrongly suggest the video itself is degraded.
  "live-no-detect": { label: "Video only", tone: "info", pulse: true },
  stale: { label: "Stale", tone: "warn", pulse: false },
  "no-signal": { label: "No signal", tone: "faint", pulse: false },
  error: { label: "Unavailable", tone: "danger", pulse: false },
};

export default function StatusPill({ kind, detail }) {
  const spec = KIND[kind] || KIND["no-signal"];
  return (
    <span className={`status-pill tone-${spec.tone}`} title={detail || spec.label}>
      <span className={`status-dot ${spec.pulse ? "pulse" : ""}`} aria-hidden="true" />
      {spec.label}
    </span>
  );
}
