import { useEffect, useRef } from "react";
import { api } from "../api.js";

// A third hook shape alongside useCameras (interval + fail-counter, for "must
// self-heal") and useSiteNames (one-shot): interval + a caller-owned
// staleness guard, for "re-fetch one open thing while it's still being
// written." Deliberately dispatch-agnostic (an onUpdate callback, not owned
// state) so a future push-based replacement (once M4a's SSE channel exists)
// can swap this hook's internals for a subscription without Review.jsx
// changing at all -- the contract stays "call onUpdate when the folder grew."
//
// Mirrors legacy app.js's poll(): re-fetches the open folder and the current
// frame's boxes every 5s, skipped entirely while pencil mode is active (never
// move the ground under a box being drawn), and only while a folder is open.
export function useFolderPolling(site, folder, frameFile, pencil, generationRef, onUpdate) {
  const savedCallback = useRef(onUpdate);
  savedCallback.current = onUpdate;

  useEffect(() => {
    if (!site || !folder || pencil) return undefined;
    const folderId = folder.id;
    const myGeneration = generationRef.current;

    const id = setInterval(async () => {
      if (generationRef.current !== myGeneration) return;
      let updated;
      try {
        updated = await api.folder(site, folderId);
      } catch {
        return;
      }
      if (generationRef.current !== myGeneration) return;
      const grew = updated.frames.length !== folder.frames.length;
      let boxes = null;
      if (frameFile) {
        try {
          boxes = await api.boxes(site, updated.day, updated.window, frameFile);
        } catch {
          boxes = null;
        }
        if (generationRef.current !== myGeneration) return;
      }
      savedCallback.current(updated, grew, boxes);
    }, 5000);

    return () => clearInterval(id);
  }, [site, folder, frameFile, pencil, generationRef]);
}
