import React, { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../api.js";
import { useAuth } from "../AuthContext.jsx";
import ReviewPanel from "../components/ReviewPanel.jsx";
import { formatWhen } from "../lib/format.js";
import "./TrackView.css";

const DIRECTION_LABEL = { up: "climbing", down: "descending" };

// Read-only "follow this bird" view opened from a detection notification --
// steps through the frames one track was seen on, drawing the model's own
// box on each. No pencil/move/follow tools and no verdict buttons: those
// belong to Review, where an operator edits the record. This just shows
// what's already there.
export default function TrackView() {
  const { site, trackId } = useParams();
  const { role } = useAuth();
  const [track, setTrack] = useState(null);
  const [error, setError] = useState(null);
  const [frameIdx, setFrameIdx] = useState(0);
  const [natural, setNatural] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setTrack(null);
    setError(null);
    setFrameIdx(0);
    setNatural(null);
    api.track(site, trackId)
      .then((data) => {
        if (cancelled) return;
        setTrack(data);
        const first = (data.files || []).indexOf(data.boxes[0]?.file);
        if (first > 0) setFrameIdx(first);
      })
      .catch((err) => !cancelled && setError(err instanceof ApiError ? err.message : "network-error"));
    return () => { cancelled = true; };
  }, [site, trackId]);

  if (error) {
    return (
      <div className="state-page">
        <h2>Can't load this detection</h2>
        <p>{error === "unknown track" || /unknown track/.test(error)
          ? "It was rejected or has expired, so it is no longer available."
          : error}</p>
        <Link to={`/app/live/${encodeURIComponent(site)}`}>Back to {site}</Link>
      </div>
    );
  }
  if (!track) return <div className="state-page skeleton-block" style={{ height: 360 }} />;

  // Every frame of the window (the seconds before and after the bird too); the model's box is drawn only on
  // frames that have one. Older servers send no `files`: fall back to the track's own frames.
  const boxByFile = new Map(track.boxes.map((b) => [b.file, b]));
  const files = track.files && track.files.length ? track.files : track.boxes.map((b) => b.file);
  const clamped = Math.max(0, Math.min(frameIdx, files.length - 1));
  const file = files[clamped];
  const box = boxByFile.get(file);
  const src = `/frame/${encodeURIComponent(site)}/${track.day}/${track.window}/${file}`;

  const boxStyle = natural && box ? {
    left: `${(box.bbox[0] / natural.w) * 100}%`,
    top: `${(box.bbox[1] / natural.h) * 100}%`,
    width: `${((box.bbox[2] - box.bbox[0]) / natural.w) * 100}%`,
    height: `${((box.bbox[3] - box.bbox[1]) / natural.h) * 100}%`,
  } : null;

  return (
    <div className="track-page">
      <div className="track-head">
        <Link to={`/app/live/${encodeURIComponent(site)}/camera/${encodeURIComponent(track.camera)}`}
              className="track-back">
          ← {track.camera}
        </Link>
        <h2 className="track-title">
          Tracking a bird on {track.camera}
          {track.verdict === "keep" && <span className="track-confirmed"> · confirmed</span>}
        </h2>
      </div>

      <div className="track-layout">
        <div className="track-media">
          <img
            key={file}
            src={src}
            alt={`Frame ${clamped + 1} of ${files.length}`}
            onLoad={(e) => setNatural({ w: e.target.naturalWidth, h: e.target.naturalHeight })}
          />
          {boxStyle && <div className="track-box" style={boxStyle} />}
        </div>

        <aside className="track-info">
          <span className="tick-label">Detection</span>
          <dl className="info-list">
            <div><dt>Site</dt><dd>{site}</dd></div>
            <div><dt>Camera</dt><dd>{track.camera}</dd></div>
            <div><dt>When</dt><dd>{formatWhen(track.day, track.window)}</dd></div>
            <div><dt>Confidence</dt><dd>{Math.round(track.confidence * 100)}%</dd></div>
            <div><dt>Movement</dt><dd>{DIRECTION_LABEL[track.direction] || "unclear"}</dd></div>
            {track.species && <div><dt>Species</dt><dd>{track.species}</dd></div>}
          </dl>
        </aside>
      </div>

      {role === "operator" && (
        <ReviewPanel site={site} track={track} onChanged={(next) => setTrack((t) => ({ ...t, ...next }))} />
      )}

      <div className="track-scrubber">
        <button type="button" onClick={() => setFrameIdx((i) => Math.max(0, i - 1))} disabled={clamped === 0}>
          &larr;
        </button>
        <input
          type="range" min={0} max={Math.max(0, files.length - 1)} value={clamped}
          onChange={(e) => setFrameIdx(Number(e.target.value))}
        />
        <button
          type="button" onClick={() => setFrameIdx((i) => Math.min(files.length - 1, i + 1))}
          disabled={clamped === files.length - 1}
        >
          &rarr;
        </button>
        <span className="track-framelabel">frame {clamped + 1} / {files.length}{!box && " · before/after the bird"}</span>
      </div>
    </div>
  );
}
