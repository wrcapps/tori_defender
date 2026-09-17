import React from "react";

export default function VerdictPanel({ box, verdict, canAct, canUndo, onMark, onUndo }) {
  return (
    <section className="review-section">
      <h2>Detection</h2>
      <div className="review-info">
        {!box ? (
          "no detection on this frame"
        ) : (
          <dl>
            <dt>detection</dt>
            <dd>t{String(box.track).padStart(4, "0")}</dd>
            <dt>confidence</dt>
            <dd>{box.conf.toFixed(2)}{box.carried ? " (carried)" : ""}</dd>
            <dt>size</dt>
            <dd>{Math.round(box.bbox[2] - box.bbox[0])}&times;{Math.round(box.bbox[3] - box.bbox[1])} px</dd>
            <dt>verdict</dt>
            <dd className={`review-verdicttag ${verdict || ""}`}>{verdict || "undecided"}</dd>
          </dl>
        )}
      </div>

      <h2>Verdict</h2>
      <div className="review-verdict-buttons">
        <button type="button" disabled={!canAct} onClick={() => onMark("keep")}>Y &mdash; real bird</button>
        <button type="button" disabled={!canAct} onClick={() => onMark("drop")}>N &mdash; false positive</button>
        <button type="button" disabled={!canAct} onClick={() => onMark("unsure")}>space &mdash; unsure</button>
        <button type="button" disabled={!canUndo} onClick={onUndo}>&#8630; undo</button>
      </div>
    </section>
  );
}
