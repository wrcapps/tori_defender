import React from "react";
import { Link } from "react-router-dom";
import { useInbox } from "../hooks/useInbox.js";
import { formatWhen } from "../lib/format.js";
import "./Inbox.css";

function expiry(ms) {
  if (ms == null) return "kept until you decide";
  const left = ms * 1000 - Date.now();   // the server sends epoch seconds
  if (left <= 0) return "expires on the next sweep";
  const h = Math.round(left / 3.6e6);
  return h >= 48 ? `expires in ${Math.round(h / 24)} days` : `expires in ${Math.max(1, h)} h`;
}

// Detections waiting for a decision. Anything here is temporary: confirmed ones are kept, rejected or unreviewed
// ones are removed after the retention period (docs/DATA_LAYOUT.md).
export default function Inbox() {
  const { windows, error, reconnecting } = useInbox();

  if (windows === null && !error) return <div className="state-page skeleton-block" style={{ height: 240 }} />;
  if (error) {
    return (
      <div className="state-page">
        <h2>Can't load the inbox</h2>
        <p>{error}</p>
      </div>
    );
  }

  const needing = windows.filter((w) => w.undecided > 0 || w.tracks.length === 0);
  return (
    <div className="inbox-page">
      <div className="inbox-head">
        <h2>Inbox</h2>
        <span className="inbox-count">{windows.length} pending window{windows.length === 1 ? "" : "s"}</span>
        {reconnecting && <span className="inbox-stale">Reconnecting… showing the last list</span>}
      </div>
      {windows.length === 0 ? (
        <p className="inbox-empty">Nothing waiting for review.</p>
      ) : (
        <ul className="inbox-list">
          {windows.map((w) => {
            const first = w.tracks.find((t) => !t.verdict) || w.tracks[0];
            return (
              <li key={`${w.site}/${w.id}`} className={`inbox-item ${w.undecided ? "needs" : ""}`}>
                <div className="inbox-thumbs">
                  {w.tracks.slice(0, 4).map((t) => (
                    t.crop ? <img key={t.id} src={`/img/${encodeURIComponent(w.site)}/${t.crop}`} alt="" loading="lazy" /> : null
                  ))}
                </div>
                <div className="inbox-main">
                  <div className="inbox-title">{w.camera} · {formatWhen(w.day, w.window)}{!w.closed && " · recording"}</div>
                  <div className="inbox-sub">
                    {w.tracks.length} detection{w.tracks.length === 1 ? "" : "s"}
                    {w.undecided > 0 && <> · <b>{w.undecided} to review</b></>}
                    {" · "}{expiry(w.expires_at)}
                    {w.protected && " · has your edits"}
                  </div>
                </div>
                {first ? (
                  <Link className="inbox-open" to={`/app/live/${encodeURIComponent(w.site)}/track/${encodeURIComponent(first.id)}`}>
                    Review
                  </Link>
                ) : <span className="inbox-sub">no detections yet</span>}
              </li>
            );
          })}
        </ul>
      )}
      {needing.length === 0 && windows.length > 0 && (
        <p className="inbox-empty">Everything here has a verdict; windows leave the inbox once decided.</p>
      )}
    </div>
  );
}
