import React from "react";
import "./StorageNotice.css";

// NAS pill in the header, and a banner when it is unreachable. The app itself never depends on the NAS:
// capture and review use the local disk, and a background process moves finished data over (archive.py).
// Nothing is rendered when no NAS folder was chosen ("local") or before the first answer.
export function StoragePill({ storage }) {
  if (storage.loading || storage.state === "local") return null;
  if (storage.unknown || storage.state === "unknown") {
    return <span className="storage-pill tone-faint" title="The NAS mover has not reported recently">NAS ?</span>;
  }
  const up = storage.state === "up";
  const waiting = (storage.pending_windows || 0) + (storage.pending_negatives || 0);
  const title = up
    ? `NAS reachable: ${storage.archive}${waiting ? ` (${waiting} item(s) being moved)` : ""}`
    : `NAS unreachable: ${storage.archive} (${storage.reason})`;
  return (
    <span className={`storage-pill ${up ? "tone-ok" : "tone-bad"}`} title={title}>
      <span className="storage-dot" aria-hidden="true" />
      NAS {up ? "up" : "down"}
    </span>
  );
}

export default function StorageNotice({ storage }) {
  if (storage.loading || storage.unknown || storage.state !== "down") return null;
  const w = storage.pending_windows || 0;
  const n = storage.pending_negatives || 0;
  return (
    <div className="storage-banner banner-warn" role="status">
      NAS unreachable. Capture and review keep working on this machine;
      {" "}{w} confirmed window(s) and {n} older negative frame(s) are waiting to move to the NAS when it is back.
      Windows already moved to the NAS cannot be opened until then.
    </div>
  );
}
