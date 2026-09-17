import React from "react";

export default function Toolbar({
  zoomPct, onFit, onZoomOut, onZoomIn,
  pencil, onTogglePencil, moveMode, onToggleMove,
  following, onToggleFollow, onStopFollow,
  canDelete, onDelete, onJumpUncertain,
  pencilHint, status, folderLabel,
}) {
  return (
    <div className="review-toolbar">
      <button type="button" onClick={onFit}>Fit</button>
      <button type="button" onClick={onZoomOut}>&minus;</button>
      <span className="review-zoomlabel">{zoomPct}%</span>
      <button type="button" onClick={onZoomIn}>+</button>
      <button
        type="button" className={pencil ? "is-active" : ""} onClick={onTogglePencil}
        title="draw a box the model missed (P)"
      >
        &#9998; add
      </button>
      <button
        type="button" className={moveMode ? "is-active" : ""} onClick={onToggleMove}
        title="drag a box to reposition it (M)"
      >
        &#8596; move
      </button>
      <button
        type="button" className={following ? "is-active" : ""}
        onClick={following ? onStopFollow : onToggleFollow}
        title="carry this box onto the next frames (F)"
      >
        &#8677; follow
      </button>
      <button type="button" disabled={!canDelete} onClick={onDelete} title="remove the selected box (Del)">
        &#128465; delete
      </button>
      <button
        type="button" disabled={!following} onClick={onStopFollow}
        title="the bird has left the scene: stop following (X)"
      >
        &#9632; out of scene
      </button>
      <button type="button" onClick={onJumpUncertain} title="jump to the lowest-confidence undecided detection">
        next uncertain
      </button>
      <span className="review-hint">{pencilHint}</span>
      <span className="review-hint">{status}</span>
      <span className="review-hint review-hint-right">{folderLabel}</span>
    </div>
  );
}
