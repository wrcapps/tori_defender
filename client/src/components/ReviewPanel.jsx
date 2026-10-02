import React, { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api.js";
import "./ReviewPanel.css";

const REASONS = [
  ["not_a_bird", "Not a bird (bush, clock, lens flare…)"],
  ["wrong_box", "Bird, but the box is wrong"],
  ["duplicate", "Duplicate of another detection"],
  ["other", "Other"],
];

function expiresText(ms) {
  if (ms == null) return null;
  const left = ms * 1000 - Date.now();   // the server sends epoch seconds
  if (left <= 0) return "expires on the next sweep";
  const h = Math.round(left / 3.6e6);
  return h >= 48 ? `expires in ${Math.round(h / 24)} days` : `expires in ${h} h`;
}

const OUTCOME = {
  confirmed: "Confirmed. This window is now kept permanently.",
  trash: "Rejected. The window moved to trash and is deleted after the retention period.",
};

// The operator's decision on one detection: confirm, reject (with why), or unsure. Lifecycle of the
// window (inbox -> kept / trash) follows from the verdicts -- see docs/DATA_LAYOUT.md.
export default function ReviewPanel({ site, track, onChanged }) {
  const navigate = useNavigate();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState(REASONS[0][0]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const [next, setNext] = useState(null);
  const [trashed, setTrashed] = useState(false);

  const life = track.lifecycle || {};
  useEffect(() => { setOutcome(null); setRejecting(false); setError(null); setTrashed(false); setNote(""); }, [track.id]);

  useEffect(() => {
    let cancelled = false;          // an old answer must not overwrite a newer one
    api.inbox(site).then((inbox) => {
      if (cancelled) return;
      for (const w of inbox.windows) {
        const t = w.tracks.find((x) => !x.verdict && x.id !== track.id);
        if (t) { setNext(t.id); return; }
      }
      setNext(null);
    }).catch(() => !cancelled && setNext(null));
    return () => { cancelled = true; };
  }, [site, track.id, outcome]);

  async function undo() {
    setBusy(true); setError(null);
    try {
      const res = await api.undoReview(site, track.day, track.window);
      setTrashed(false);
      setOutcome("Rejection undone. The detection is back in the inbox.");
      onChanged({ verdict: null, lifecycle: res.lifecycle });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the server. Nothing was changed; try again.");
    } finally { setBusy(false); }
  }

  async function decide(decision) {
    setBusy(true); setError(null);
    try {
      const res = await api.review(site, track.id, decision, decision === "drop" ? reason : "", note);
      const moved = res.moved && res.moved[0];
      setOutcome(moved && !moved.error ? (OUTCOME[moved.to] || "Saved.")
        : decision === "unsure" ? "Marked unsure. It stays in the inbox for a later decision."
        : "Saved. Other detections in this window still need a decision.");
      setTrashed(Boolean(moved && !moved.error && moved.to === "trash"));
      setNote("");
      onChanged({ verdict: decision, lifecycle: res.lifecycle });
      setRejecting(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the server. Nothing was saved; try again.");
    } finally { setBusy(false); }
  }

  const expiry = life.state === "pending" ? expiresText(life.expires_at) : null;

  return (
    <section className="review-panel" aria-label="Review this detection">
      <div className="review-panel-head">
        <span className="tick-label">Review</span>
        <span className={`review-state state-${life.state || "unknown"}`}>
          {life.state === "pending" ? "pending review" : life.state === "confirmed" ? "kept" : life.state || ""}
        </span>
        {expiry && <span className="review-expiry">{expiry}</span>}
        {track.verdict && <span className="review-verdict">your verdict: <b>{track.verdict}</b></span>}
      </div>

      {error && <div className="review-error" role="alert">{error}</div>}
      {outcome && (
        <div className="review-outcome" role="status">
          {outcome}
          {trashed && <> <button type="button" className="rv-link rv-undo" disabled={busy} onClick={undo}>Undo</button></>}
        </div>
      )}

      {!rejecting ? (
        <div className="review-actions">
          <button type="button" className="rv-btn rv-keep" disabled={busy} onClick={() => decide("keep")}>
            Confirm bird
          </button>
          <button type="button" className="rv-btn rv-drop" disabled={busy} onClick={() => setRejecting(true)}>
            Something is wrong…
          </button>
          <button type="button" className="rv-btn" disabled={busy} onClick={() => decide("unsure")}>
            Unsure
          </button>
          <Link className="rv-link"
                to={`/app/review?${new URLSearchParams({ site, folder: `${track.day}/${track.window}`,
                  file: track.boxes[0]?.file || "", track: String(track.track) })}`}>
            Fix boxes in Review
          </Link>
        </div>
      ) : (
        <div className="review-reject">
          <label>What is wrong?
            <select value={reason} onChange={(e) => setReason(e.target.value)}>
              {REASONS.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select>
          </label>
          <label>Note (optional)
            <input type="text" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="review-actions">
            <button type="button" className="rv-btn rv-drop" disabled={busy} onClick={() => decide("drop")}>
              Reject detection
            </button>
            <button type="button" className="rv-btn" disabled={busy} onClick={() => setRejecting(false)}>Cancel</button>
          </div>
          <p className="review-hint">
            A rejected window is moved to trash and deleted after the retention period; it is only removed once
            every detection in it is rejected and nobody drew boxes on it.
          </p>
        </div>
      )}

      {next && (
        <button type="button" className="rv-next"
                onClick={() => navigate(`/app/live/${encodeURIComponent(site)}/track/${encodeURIComponent(next)}`)}>
          Next pending detection →
        </button>
      )}
    </section>
  );
}
