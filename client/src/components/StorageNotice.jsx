import React from "react";
import "./StorageNotice.css";

// NAS pill in the header + a banner when something needs the operator's attention.
// Nothing is rendered when no NAS folder was chosen ("local") or before the first answer.
export function StoragePill({ storage }) {
  if (storage.loading || storage.state === "local") return null;
  if (storage.unknown) {
    return <span className="storage-pill tone-faint" title="Storage status unavailable">NAS ?</span>;
  }
  const up = storage.state === "up";
  const title = up ? `NAS reachable: ${storage.path}` : `NAS unreachable: ${storage.path} (${storage.reason})`;
  return (
    <span className={`storage-pill ${up ? "tone-ok" : "tone-bad"}`} title={title}>
      <span className="storage-dot" aria-hidden="true" />
      NAS {up ? "up" : "down"}
    </span>
  );
}

export default function StorageNotice({ storage }) {
  if (storage.loading || storage.unknown || storage.state !== "down" && !storage.on_fallback) return null;
  let tone = "warn", text;
  if (storage.state === "down" && storage.on_fallback) {
    text = `NAS unavailable. New data is being saved to a temporary folder on this machine and moves to the NAS the next time the app starts with it back. Earlier data on the NAS is not visible until then.`;
  } else if (storage.state === "down") {
    tone = "bad";
    text = `NAS connection lost. Saving new data and opening older data may fail until it is back.`;
  } else {
    text = `NAS is back. ${storage.pending_files} file(s) saved while it was down are still in the temporary folder. Restart the app to move them to the NAS.`;
  }
  return <div className={`storage-banner banner-${tone}`} role="status">{text}</div>;
}
