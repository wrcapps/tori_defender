import React from "react";

function Group({ title, items, currentFolderId, onOpen, onToggleReviewed }) {
  if (!items.length) return null;
  return (
    <div className="review-group">
      <div className="review-grouphead">{title} &middot; {items.length}</div>
      {items.map((f) => (
        <div
          key={f.id}
          className={`review-folder ${currentFolderId === f.id ? "is-current" : ""}`}
          onClick={() => onOpen(f.id)}
        >
          <div className="review-folder-name">
            <span>{f.camera ? `${f.camera} ` : ""}{f.window}</span>
            {f.reviewed && <span className="review-tick" title="reviewed manually">&#10003;</span>}
            {f.recording && <span className="review-rec" title="still being written">&#9679; REC</span>}
            <button
              type="button"
              className="review-tickbtn"
              disabled={f.recording}
              title={f.reviewed ? "move back to the review queue" : "mark this session reviewed"}
              onClick={(e) => { e.stopPropagation(); onToggleReviewed(f.id); }}
            >
              {f.reviewed ? "undo" : "done"}
            </button>
          </div>
          <div className="review-folder-meta">
            {f.day} &middot; {f.frames} frames &middot; {f.tracks} detections
            {f.undecided ? ` · ${f.undecided} undecided` : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function SessionList({ folders, currentFolderId, onOpen, onToggleReviewed, onRefresh }) {
  const todo = folders.filter((f) => !f.reviewed);
  const done = folders.filter((f) => f.reviewed);

  return (
    <nav className="review-sessions">
      <div className="review-navhead">
        <span>Sessions</span>
        <button type="button" onClick={onRefresh} title="reload the session list">&#8635;</button>
      </div>
      <div className="review-folderlist">
        {folders.length === 0 ? (
          <div className="review-empty">
            No capture sessions yet.<br />Run live_capture.py, or point it at existing frames.
          </div>
        ) : (
          <>
            <Group title="To review" items={todo} currentFolderId={currentFolderId}
                   onOpen={onOpen} onToggleReviewed={onToggleReviewed} />
            <Group title="Reviewed manually" items={done} currentFolderId={currentFolderId}
                   onOpen={onOpen} onToggleReviewed={onToggleReviewed} />
          </>
        )}
      </div>
    </nav>
  );
}
