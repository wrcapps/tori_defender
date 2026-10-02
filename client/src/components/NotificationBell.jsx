import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../AuthContext.jsx";
import "./NotificationBell.css";

const DIRECTION_LABEL = { up: "climbing", down: "descending" };

function directionText(direction) {
  return DIRECTION_LABEL[direction] || "position unclear";
}

// Raw-detection feed, not confirmed sightings (see useDetectionEvents.js) --
// copy says "detected", never "confirmed", so nobody reads a false positive
// (a bush, the on-screen clock, a lens flare) as a verified bird.
const DECISION_NOTE = {
  low: "no action",
  medium: "deterrent activated",
  high: "turbine stopped",
};

export default function NotificationBell({ events, onDismiss, onClearAll }) {
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const { role } = useAuth();

  function openTrack(event) {
    setOpen(false);
    navigate(`/app/live/${encodeURIComponent(event.site)}/track/${encodeURIComponent(event.id)}`);
  }

  // Straight to the interval the detection happened in: its session, at the first frame of
  // the track, with the track selected. Review is operator-only, so a client falls back to
  // the read-only track page.
  function openInterval(event) {
    // Operators land on the detection's own page, where it can be confirmed or rejected.
    // A live event reaches it once its detections have been saved (the server then adds track_id).
    if (event.kind === "live") {
      if (!event.track_id) return;
      setOpen(false);
      navigate(`/app/live/${encodeURIComponent(event.site)}/track/${encodeURIComponent(event.track_id)}`);
      return;
    }
    if (role === "operator" || !(event.first_file || event.file)) { openTrack(event); return; }
    const file = event.first_file || event.file;
    setOpen(false);
    const q = new URLSearchParams({ site: event.site, folder: `${event.day}/${event.window}`, file });
    if (event.kind === "model" || event.kind === "live") q.set("track", String(event.track));
    navigate(`/app/review?${q.toString()}`);
  }

  return (
    <div className="notify">
      <button
        type="button"
        className={`notify-bell ${events.length > 0 ? "has-events" : ""}`}
        aria-expanded={open}
        aria-label={`${events.length} recent detections`}
        onClick={() => setOpen((v) => !v)}
      >
        &#128276;
        {events.length > 0 && <span className="notify-badge">{events.length}</span>}
      </button>

      {open && (
        <div className="notify-panel" role="menu">
          <div className="notify-panel-head">
            <span>Detections &amp; alerts</span>
            {events.length > 0 && (
              <button type="button" className="notify-clear" onClick={onClearAll}>clear</button>
            )}
          </div>
          {events.length === 0 ? (
            <div className="notify-empty">Nothing new right now.</div>
          ) : (
            <ul className="notify-list">
              {events.map((e) => (
                <li key={e.id} className={`notify-item ${e.risk ? `risk-${e.risk}` : ""}`}>
                  <div className="notify-item-body">
                    <button type="button" className="notify-item-main" onClick={() => openInterval(e)}
                            title={e.kind === "live" && !e.track_id ? "nothing to open (not recorded, or not saved yet)"
                                                                 : "open this detection to review it"}>
                      {e.kind === "live" ? (
                        <>
                          <span className="notify-item-title">
                            Live detection on {e.site} &middot; {e.camera}
                          </span>
                          <span className="notify-item-sub">
                            {e.count} object{e.count === 1 ? "" : "s"} &middot; confidence {Number(e.conf).toFixed(2)}
                            {" "}&middot; {e.track_id ? "saved -- open to review"
                              : e.recording ? (Date.now() / 1000 - e.ts > 180 ? "no longer available (rejected, expired or not saved)" : "saving…")
                              : "not recorded (acquisition and recording are off)"}
                          </span>
                        </>
                      ) : e.risk ? (
                        <>
                          <span className="notify-item-title">{e.message}</span>
                          <span className="notify-item-sub">
                            <span className={`risk-chip ${e.risk}`}>{e.risk_label.toUpperCase()}</span>
                            {" "}decision: <b>{e.decision_label || DECISION_NOTE[e.risk]}</b>
                            {" "}<span className="notify-item-dim">(logged, not actuated)</span>
                            {e.policy_configured === false && (
                              <span className="notify-item-dim"> · placeholder zones</span>
                            )}
                          </span>
                        </>
                      ) : (
                        <>
                          <span className="notify-item-title">
                            Bird detected on {e.site} &middot; {e.camera}
                          </span>
                          <span className="notify-item-sub">
                            {directionText(e.direction)} &middot; confidence {e.confidence.toFixed(2)}
                            {" "}&middot; no distance, no risk yet
                          </span>
                        </>
                      )}
                    </button>
                    <div className="notify-item-actions">
                      {!(e.kind === "live" && !e.track_id) && (
                        <button type="button" className="notify-item-link" onClick={() => openInterval(e)}>
                          {role === "operator" ? "Review" : "Open track"}
                        </button>
                      )}
                      {role === "operator" && e.kind !== "live" && (e.first_file || e.file) && (
                        <button type="button" className="notify-item-link" onClick={() => {
                          const file = e.first_file || e.file;
                          const q = new URLSearchParams({ site: e.site, folder: `${e.day}/${e.window}`, file, track: String(e.track) });
                          setOpen(false);
                          navigate(`/app/review?${q.toString()}`);
                        }}>
                          fix boxes
                        </button>
                      )}
                    </div>
                  </div>
                  <button
                    type="button" className="notify-item-dismiss" aria-label="dismiss"
                    onClick={() => onDismiss(e.id)}
                  >
                    &times;
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
