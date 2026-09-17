// Review's domain state: sessions, the open folder, boxes, verdicts,
// annotations, undo history, and the follow/pencil/move modes. A reducer
// (the first in this codebase) rather than a scatter of useState, because
// several of these fields have cross-cutting invariants a handful of
// sibling components would otherwise have to re-derive independently:
//   - `history` (undo) must move atomically with `verdicts` -- one push,
//     one pop, never out of sync.
//   - `pencil`/`moveMode`/`follow` are mutually exclusive or interacting
//     modes, enforced once here rather than in every component that toggles
//     one of them.
//   - `frameBoxes`/`manualBoxes`/`selectedTrack` reset together on a site or
//     folder change.
//
// Deliberately NOT in here (kept local to the components that own them):
// `generation` (a race guard, not domain state -- see Review.jsx),
// transient status text, and in-progress drag/pencil-point state (high
// frequency, would repaint every sibling on every mouse-move pixel).

export const initialReviewState = {
  folders: [],
  folder: null,          // {id, day, window, width, height, frames[], uncertain[]}
  frameIdx: 0,
  frameBoxes: [],        // current frame's model boxes: {bbox, conf, track, carried, crop, edited?}
  selectedTrack: null,
  verdicts: {},          // {track_id: keep|drop|unsure}
  trackMeta: {},         // {track_id: {species, distance, size}}
  manualBoxes: [],
  speciesSeen: [],
  history: [],           // [{id, prev}] undo stack
  selected: null,        // {kind:"model", track} | {kind:"manual", id}
  moveMode: false,
  pencil: false,
  follow: null,          // {track, history: [[x0,y0,x1,y1], ...]}
  nativeW: 0,
  nativeH: 0,
  // Fixed for as long as a folder stays open (set once, at FOLDER_OPENED,
  // from the server's cached size if any) -- never flips back to false on a
  // later poll refresh, matching legacy app.js's own module-level flag.
  // Forces every frame load to use the native (not downscaled) image for the
  // whole folder, so `nativeW`/`nativeH` can never be contaminated by a
  // downscaled image's own naturalWidth/Height.
  nativeSizeUnknown: false,
};

function trackId(day, windowName, track) {
  return `${day}/${windowName}/t${String(track).padStart(4, "0")}`;
}

// Mirrors legacy showFrame()'s selection rule: keep the current selection if
// it still exists on the new frame and the caller asked to keep it;
// otherwise select the lowest-confidence box (what a reviewer most needs to
// look at next), or nothing if the frame has none.
function nextSelectedTrack(frameBoxes, keepSelection, currentSelectedTrack) {
  if (keepSelection && frameBoxes.some((b) => b.track === currentSelectedTrack)) {
    return currentSelectedTrack;
  }
  if (!frameBoxes.length) return null;
  return frameBoxes.reduce((a, b) => (b.conf < a.conf ? b : a)).track;
}

export function reviewReducer(state, action) {
  switch (action.type) {
    case "SITE_RESET":
      return {
        ...initialReviewState,
        folders: state.folders, // repopulated by FOLDERS_LOADED right after
      };

    case "SITE_DATA_LOADED":
      return { ...state, verdicts: action.verdicts, trackMeta: action.trackMeta,
               manualBoxes: action.manualBoxes };

    case "FOLDERS_LOADED":
      return { ...state, folders: action.folders, speciesSeen: action.species };

    case "FOLDER_OPENED":
      return {
        ...state,
        folder: action.folder,
        frameIdx: 0,
        frameBoxes: [],
        selectedTrack: null,
        selected: null,
        follow: null, // opening a different session always stops any follow
        nativeW: action.folder.width || 0,
        nativeH: action.folder.height || 0,
        nativeSizeUnknown: !action.folder.width,
      };

    case "FOLDER_GREW":
      return { ...state, folder: action.folder };

    // Only accepted once, from the first native-frame load of a folder whose
    // size wasn't already known -- see `nativeSizeUnknown` above.
    case "NATIVE_SIZE_DETECTED":
      return state.nativeW ? state : { ...state, nativeW: action.width, nativeH: action.height };

    case "FRAME_INDEX_SET":
      return { ...state, frameIdx: action.frameIdx };

    case "FRAME_BOXES_LOADED": {
      const selectedTrack = nextSelectedTrack(action.boxes, action.keepSelection, state.selectedTrack);
      return { ...state, frameBoxes: action.boxes, selectedTrack };
    }

    case "TRACK_SELECTED":
      return { ...state, selectedTrack: action.track, selected: { kind: "model", track: action.track } };

    case "MANUAL_SELECTED":
      return { ...state, selected: { kind: "manual", id: action.id } };

    case "SELECTION_CLEARED":
      return { ...state, selected: null };

    case "VERDICT_MARKED": {
      const id = action.id;
      const prev = state.verdicts[id];
      const verdicts = { ...state.verdicts, [id]: action.verdict };
      return { ...state, verdicts, history: [...state.history, { id, prev }] };
    }

    // Restore the top of the undo stack without popping it (a failed save
    // rolls back to the last real server-confirmed value).
    case "VERDICT_ROLLED_BACK": {
      const last = state.history[state.history.length - 1];
      if (!last) return state;
      const verdicts = { ...state.verdicts };
      if (last.prev === undefined) delete verdicts[last.id]; else verdicts[last.id] = last.prev;
      return { ...state, verdicts, history: state.history.slice(0, -1) };
    }

    case "UNDO_APPLIED": {
      const last = state.history[state.history.length - 1];
      if (!last) return state;
      const verdicts = { ...state.verdicts };
      if (last.prev === undefined) delete verdicts[last.id]; else verdicts[last.id] = last.prev;
      return { ...state, verdicts, history: state.history.slice(0, -1) };
    }

    case "MODEL_BOX_MOVED":
      return {
        ...state,
        frameBoxes: state.frameBoxes.map((b) =>
          b.track === action.track ? { ...b, bbox: action.bbox, edited: true } : b),
      };

    case "MODEL_BOX_REMOVED":
      return {
        ...state,
        frameBoxes: state.frameBoxes.filter((b) => b.track !== action.track),
        selected: null,
      };

    case "MANUAL_BOX_ADDED":
      return {
        ...state,
        manualBoxes: [...state.manualBoxes, action.row],
        selected: { kind: "manual", id: action.row.id },
      };

    case "MANUAL_BOX_UPDATED":
      return {
        ...state,
        manualBoxes: state.manualBoxes.map((m) => (m.id === action.row.id ? action.row : m)),
      };

    case "MANUAL_BOX_REMOVED":
      return {
        ...state,
        manualBoxes: state.manualBoxes.filter((m) => m.id !== action.id),
        selected: null,
      };

    case "META_SAVED": {
      const trackMeta = { ...state.trackMeta };
      if (action.meta && Object.keys(action.meta).length) trackMeta[action.id] = action.meta;
      else delete trackMeta[action.id];
      return { ...state, trackMeta, speciesSeen: action.species || state.speciesSeen };
    }

    case "FOLLOW_STARTED":
      return { ...state, follow: { track: action.track, history: [action.bbox] }, moveMode: true };

    case "FOLLOW_STOPPED":
      return { ...state, follow: null };

    case "FOLLOW_HISTORY_PUSHED":
      return state.follow
        ? { ...state, follow: { ...state.follow, history: [...state.follow.history, action.bbox] } }
        : state;

    case "FOLLOW_HISTORY_CORRECTED":
      return state.follow
        ? {
            ...state,
            follow: {
              ...state.follow,
              history: [...state.follow.history.slice(0, -1), action.bbox],
            },
          }
        : state;

    case "PENCIL_TOGGLED":
      return { ...state, pencil: !state.pencil };

    case "MOVE_MODE_TOGGLED": {
      const moveMode = !state.moveMode;
      // Entering move mode always drops pencil mode -- the two are mutually
      // exclusive (legacy: `if (moveMode && pencil) togglePencil()`).
      return { ...state, moveMode, pencil: moveMode ? false : state.pencil };
    }

    case "FOLDER_REVIEWED_SET":
      return {
        ...state,
        folders: state.folders.map((f) => (f.id === action.id ? { ...f, reviewed: action.reviewed } : f)),
      };

    case "UNDECIDED_BUMPED":
      return {
        ...state,
        folders: state.folders.map((f) =>
          f.id === action.folderId ? { ...f, undecided: Math.max(0, f.undecided + action.delta) } : f),
      };

    default:
      return state;
  }
}

export { trackId };
