import React, { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, ApiError } from "../../api.js";
import { useSiteNames } from "../../hooks/useSiteNames.js";
import { useFolderPolling } from "../../hooks/useFolderPolling.js";
import SiteSwitcher from "../../components/SiteSwitcher.jsx";
import SessionList from "./SessionList.jsx";
import FrameViewport from "./FrameViewport.jsx";
import Toolbar from "./Toolbar.jsx";
import Magnifier from "./Magnifier.jsx";
import VerdictPanel from "./VerdictPanel.jsx";
import AnnotationForm from "./AnnotationForm.jsx";
import ManualBoxStrip from "./ManualBoxStrip.jsx";
import { initialReviewState, reviewReducer, trackId } from "./reviewReducer.js";
import "./Review.css";

export default function Review() {
  const sites = useSiteNames();
  const [site, setSite] = useState(null);
  const [state, dispatch] = useReducer(reviewReducer, initialReviewState);
  const [status, setStatusText] = useState("");
  const [zoomPct, setZoomPct] = useState(100);
  const [hoverPreview, setHoverPreview] = useState(null); // pencil-mode magnifier override
  const [error, setError] = useState(null);

  const generationRef = useRef(0);
  const imgRef = useRef(null);
  const viewportRef = useRef(null);
  const statusTimer = useRef(null);
  const fieldsRef = useRef({ species: "", distance: "", size: "" }); // live pencil-mode form values

  function setStatus(text) {
    setStatusText(text);
    clearTimeout(statusTimer.current);
    if (text) statusTimer.current = setTimeout(() => setStatusText(""), 4000);
  }

  useEffect(() => {
    if (sites && sites.length > 0 && !site) setSite(sites[0]);
  }, [sites, site]);

  // ---------------------------------------------------------------- site
  const loadSite = useCallback(async (name) => {
    dispatch({ type: "SITE_RESET" });
    setError(null);
    try {
      const [v, m, mb] = await Promise.all([
        api.verdicts(name), api.trackMeta(name), api.manualBoxes(name),
      ]);
      dispatch({ type: "SITE_DATA_LOADED", verdicts: v, trackMeta: m, manualBoxes: mb });
      const data = await api.folders(name);
      dispatch({ type: "FOLDERS_LOADED", folders: data.folders || [], species: data.species || [] });
      const first = (data.folders || []).find((f) => !f.reviewed) || (data.folders || [])[0];
      if (first) openFolder(name, first.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "network-error");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { if (site) loadSite(site); }, [site, loadSite]);

  const loadFolders = useCallback(async (keepOpen = true) => {
    if (!site) return;
    try {
      const data = await api.folders(site);
      dispatch({ type: "FOLDERS_LOADED", folders: data.folders || [], species: data.species || [] });
      if (!keepOpen || !state.folder) {
        const first = (data.folders || []).find((f) => !f.reviewed) || (data.folders || [])[0];
        if (first) openFolder(site, first.id);
      }
    } catch {
      setStatus("could not refresh the session list");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [site, state.folder]);

  // ---------------------------------------------------------------- folder
  async function openFolder(forSite, id) {
    const gen = ++generationRef.current;
    let opened;
    try {
      opened = await api.folder(forSite, id);
    } catch {
      return;
    }
    if (gen !== generationRef.current) return;
    dispatch({ type: "FOLDER_OPENED", folder: opened });
    showFrame(forSite, opened, 0, false);
  }

  async function showFrame(forSite, folder, index, keepSelection) {
    if (!folder || !folder.frames.length) {
      dispatch({ type: "FRAME_INDEX_SET", frameIdx: 0 });
      return;
    }
    const clamped = Math.max(0, Math.min(index, folder.frames.length - 1));
    dispatch({ type: "FRAME_INDEX_SET", frameIdx: clamped });
    const gen = generationRef.current;
    const file = folder.frames[clamped].file;
    let boxes = [];
    try {
      boxes = await api.boxes(forSite, folder.day, folder.window, file);
    } catch {
      boxes = [];
    }
    if (gen !== generationRef.current) return;
    dispatch({ type: "FRAME_BOXES_LOADED", boxes, keepSelection });
  }

  function handleScrub(index, keepSelection) {
    if (!state.folder) return;
    showFrame(site, state.folder, index, keepSelection);
  }

  // ---------------------------------------------------------------- polling
  const currentFrame = state.folder && state.folder.frames.length
    ? state.folder.frames[Math.min(state.frameIdx, state.folder.frames.length - 1)]
    : null;

  useFolderPolling(site, state.folder, currentFrame?.file, state.pencil, generationRef,
    (updatedFolder, grew, boxes) => {
      dispatch({ type: "FOLDER_GREW", folder: updatedFolder });
      if (boxes !== null) dispatch({ type: "FRAME_BOXES_LOADED", boxes, keepSelection: true });
      const entry = state.folders.find((f) => f.id === updatedFolder.id);
      if (entry && entry.recording) loadFolders();
    });

  // -------------------------------------------------------------- sessions
  async function handleToggleReviewed(id) {
    const entry = state.folders.find((f) => f.id === id);
    if (!entry) return;
    try {
      await api.setFolderStatus(site, id, !entry.reviewed);
      dispatch({ type: "FOLDER_REVIEWED_SET", id, reviewed: !entry.reviewed });
    } catch (err) {
      setStatus(err instanceof ApiError ? err.message : "could not update that session");
    }
  }

  // -------------------------------------------------------------- selection
  const manualBoxesForFrame = currentFrame && state.folder
    ? state.manualBoxes.filter((b) => b.day === state.folder.day && b.window === state.folder.window &&
                                       b.file === currentFrame.file)
    : [];

  function selectedBox() {
    if (!state.selected) return null;
    if (state.selected.kind === "model") {
      const b = state.frameBoxes.find((x) => x.track === state.selected.track);
      return b ? { kind: "model", bbox: b.bbox, model: b } : null;
    }
    const m = manualBoxesForFrame.find((x) => x.id === state.selected.id) ||
      state.manualBoxes.find((x) => x.id === state.selected.id);
    return m ? { kind: "manual", bbox: m.bbox, manual: m } : null;
  }

  // -------------------------------------------------------------- verdicts
  function bumpUndecided(now, before) {
    if (!state.folder) return;
    if (before === undefined && now !== undefined) {
      dispatch({ type: "UNDECIDED_BUMPED", folderId: state.folder.id, delta: -1 });
    }
    if (before !== undefined && now === undefined) {
      dispatch({ type: "UNDECIDED_BUMPED", folderId: state.folder.id, delta: 1 });
    }
  }

  async function mark(verdict) {
    if (state.selectedTrack == null || !state.folder) return;
    const id = trackId(state.folder.day, state.folder.window, state.selectedTrack);
    const prev = state.verdicts[id];
    dispatch({ type: "VERDICT_MARKED", id, verdict });
    bumpUndecided(verdict, prev);
    try {
      await api.setVerdict(site, id, verdict);
    } catch {
      dispatch({ type: "VERDICT_ROLLED_BACK" });
      bumpUndecided(prev, verdict);
      setStatus("could not save that verdict — is the review server still running?");
    }
  }

  async function undoLast() {
    const last = state.history[state.history.length - 1];
    if (!last) return;
    const current = state.verdicts[last.id];
    dispatch({ type: "UNDO_APPLIED" });
    bumpUndecided(last.prev, current);
    try {
      await api.setVerdict(site, last.id, last.prev === undefined ? "clear" : last.prev);
    } catch {
      setStatus("could not save that undo — is the review server still running?");
    }
  }

  function jumpUncertain() {
    if (!state.folder) return;
    const next = (state.folder.uncertain || []).find((u) => !state.verdicts[u.id]);
    if (!next) { setStatus("nothing undecided left in this session"); return; }
    dispatch({ type: "TRACK_SELECTED", track: next.track });
    setStatus(`jumped to the lowest-confidence undecided detection (${next.conf.toFixed(2)})`);
    showFrame(site, state.folder, next.index, true);
  }

  // ------------------------------------------------------------ annotation
  async function saveMeta(id, values) {
    try {
      const res = await api.saveTrackMeta(site, id, values);
      dispatch({ type: "META_SAVED", id, meta: res.meta, species: res.species });
    } catch {
      setStatus("could not save those notes");
    }
  }

  // ------------------------------------------------------------ box edits
  async function handleBoxMoved(kind, key, bbox) {
    if (!state.folder) return;
    if (kind === "manual") {
      try {
        const row = await api.updateManualBox(site, key, bbox);
        dispatch({ type: "MANUAL_BOX_UPDATED", row });
        if (state.follow && state.selected?.kind === "manual" && row.track === state.follow.track) {
          dispatch({ type: "FOLLOW_HISTORY_CORRECTED", bbox: bbox.slice() });
        }
      } catch {
        setStatus("could not save that move");
      }
    } else {
      try {
        await api.boxEdit(site, state.folder.day, state.folder.window, currentFrame.file, key, { bbox });
        dispatch({ type: "MODEL_BOX_MOVED", track: key, bbox });
      } catch {
        setStatus("could not save that move");
      }
    }
  }

  async function handleDelete() {
    const sel = selectedBox();
    if (!sel || !state.folder) return;
    if (sel.kind === "manual") {
      try {
        await api.deleteManualBox(site, sel.manual.id);
        dispatch({ type: "MANUAL_BOX_REMOVED", id: sel.manual.id });
        setStatus("box deleted");
      } catch {
        setStatus("could not delete that box");
      }
    } else {
      try {
        await api.boxEdit(site, state.folder.day, state.folder.window, currentFrame.file,
                          sel.model.track, { deleted: true });
        dispatch({ type: "MODEL_BOX_REMOVED", track: sel.model.track });
        setStatus("detection removed from this frame (the track's verdict is separate)");
      } catch {
        setStatus("could not remove that detection");
      }
    }
  }

  // ------------------------------------------------------------ pencil
  async function handlePencilBoxDrawn(bbox) {
    if (!state.folder || !currentFrame) return;
    try {
      const row = await api.addManualBox(site, state.folder.day, state.folder.window,
                                          currentFrame.file, bbox, fieldsRef.current);
      dispatch({ type: "MANUAL_BOX_ADDED", row });
    } catch {
      setStatus("could not save that box");
    }
  }

  // ------------------------------------------------------------ follow
  async function startFollow() {
    const sel = selectedBox();
    if (!sel) { setStatus("select a box first, then follow it"); return; }
    try {
      const res = await api.startManualTrack(site);
      dispatch({ type: "FOLLOW_STARTED", track: res.track, bbox: sel.bbox.slice() });
      setStatus(`following as ${res.track} — step frames with →, X when it leaves the scene`);
    } catch {
      setStatus("could not start a track");
    }
  }

  function stopFollow(reason) {
    if (!state.follow) return;
    const track = state.follow.track;
    dispatch({ type: "FOLLOW_STOPPED" });
    setStatus(reason || `stopped following ${track}`);
  }

  function predictNext(history, nativeW, nativeH) {
    const last = history[history.length - 1];
    if (history.length < 2) return last.slice();
    const prev = history[history.length - 2];
    const dx = last[0] - prev[0], dy = last[1] - prev[1];
    const [x0, y0, x1, y1] = last;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    return [clamp(x0 + dx, 0, nativeW), clamp(y0 + dy, 0, nativeH),
            clamp(x1 + dx, 0, nativeW), clamp(y1 + dy, 0, nativeH)];
  }

  const placeFollowBoxRef = useRef();
  placeFollowBoxRef.current = async function placeFollowBox() {
    if (!state.follow || !currentFrame || !state.folder) return;
    const bbox = predictNext(state.follow.history, state.nativeW, state.nativeH);
    try {
      const row = await api.addManualBox(site, state.folder.day, state.folder.window, currentFrame.file,
                                          bbox, { ...fieldsRef.current, track: state.follow.track });
      dispatch({ type: "MANUAL_BOX_ADDED", row });
      dispatch({ type: "FOLLOW_HISTORY_PUSHED", bbox });
    } catch {
      setStatus("could not place the box on this frame");
    }
  };

  // Auto-place a follow box on any frame stepped to that doesn't have one yet.
  useEffect(() => {
    if (!state.follow || !currentFrame) return;
    const here = manualBoxesForFrame.some((b) => b.track === state.follow.track);
    if (!here) placeFollowBoxRef.current();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.follow, currentFrame?.file]);

  // ------------------------------------------------------------- keyboard
  useEffect(() => {
    function onKeyDown(e) {
      if (e.key === "Escape" && state.pencil) { dispatch({ type: "PENCIL_TOGGLED" }); return; }
      if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
      const step = e.shiftKey ? 10 : 1;
      if (e.key === "ArrowLeft") handleScrub(state.frameIdx - step, true);
      else if (e.key === "ArrowRight") handleScrub(state.frameIdx + step, true);
      else if (e.key === "y" || e.key === "Y") mark("keep");
      else if (e.key === "n" || e.key === "N") mark("drop");
      else if (e.key === " ") { e.preventDefault(); mark("unsure"); }
      else if (e.key === "u" || e.key === "U") jumpUncertain();
      else if (e.key === "p" || e.key === "P") dispatch({ type: "PENCIL_TOGGLED" });
      else if (e.key === "m" || e.key === "M") dispatch({ type: "MOVE_MODE_TOGGLED" });
      else if (e.key === "f" || e.key === "F") { state.follow ? stopFollow() : startFollow(); }
      else if (e.key === "x" || e.key === "X") { if (state.follow) stopFollow("that bird is out of the scene"); }
      else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); handleDelete(); }
      else if (e.key === "z" || e.key === "Z") undoLast();
      else if (e.key === "+" || e.key === "=") viewportRef.current?.zoomIn();
      else if (e.key === "-" || e.key === "_") viewportRef.current?.zoomOut();
      else if (e.key === "0") viewportRef.current?.fit();
    }
    addEventListener("keydown", onKeyDown);
    return () => removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  // --------------------------------------------------------------- render
  if (sites === null) return <div className="state-page skeleton-block" style={{ height: 300 }} />;
  if (sites.length === 0) {
    return (
      <div className="state-page">
        <h2>No cameras configured</h2>
        <p>Review needs at least one configured site.</p>
      </div>
    );
  }
  if (error) {
    return (
      <div className="state-page">
        <h2>Can't load Review</h2>
        <p>{error}</p>
      </div>
    );
  }

  const box = state.frameBoxes.find((b) => b.track === state.selectedTrack) || null;
  const verdictId = box ? trackId(state.folder.day, state.folder.window, box.track) : null;
  const metaId = state.pencil ? null : verdictId;
  const disabledForm = !verdictId && !state.pencil;
  const selectedNow = selectedBox();
  const magnifierPoint = state.pencil
    ? hoverPreview?.point || null
    : (box ? { cx: (box.bbox[0] + box.bbox[2]) / 2, cy: (box.bbox[1] + box.bbox[3]) / 2 } : null);
  const magnifierBoxes = state.pencil
    ? hoverPreview?.boxes || []
    : (box ? [{ bbox: box.bbox, color: "#f0c14b" }] : []);
  const magnifierHint = state.pencil
    ? (hoverPreview?.hint || "click corner 1")
    : (box ? "selected detection, zoomed" : "no detection selected");

  return (
    <div className="review-page">
      <div className="review-head">
        <span className="tick-label">Review</span>
        <SiteSwitcher sites={sites} value={site} onChange={setSite} />
      </div>

      <div className="review-layout">
        <SessionList
          folders={state.folders} currentFolderId={state.folder?.id}
          onOpen={(id) => openFolder(site, id)} onToggleReviewed={handleToggleReviewed}
          onRefresh={() => loadFolders()}
        />

        <div className="review-viewer">
          <Toolbar
            zoomPct={zoomPct}
            onFit={() => viewportRef.current?.fit()}
            onZoomOut={() => viewportRef.current?.zoomOut()}
            onZoomIn={() => viewportRef.current?.zoomIn()}
            pencil={state.pencil} onTogglePencil={() => dispatch({ type: "PENCIL_TOGGLED" })}
            moveMode={state.moveMode} onToggleMove={() => dispatch({ type: "MOVE_MODE_TOGGLED" })}
            following={!!state.follow} onToggleFollow={startFollow} onStopFollow={() => stopFollow()}
            canDelete={!!selectedNow} onDelete={handleDelete} onJumpUncertain={jumpUncertain}
            pencilHint={state.pencil ? (hoverPreview?.hint || "move to the bird, watch the magnifier, "
              + "click two opposite corners") : ""}
            status={status}
            folderLabel={state.folder ? `${state.folder.day} / ${state.folder.window}` : ""}
          />

          <FrameViewport
            ref={viewportRef} site={site} folder={state.folder} frame={currentFrame}
            frameIdx={state.frameIdx} imgRef={imgRef}
            nativeW={state.nativeW} nativeH={state.nativeH} nativeSizeUnknown={state.nativeSizeUnknown}
            pencil={state.pencil} moveMode={state.moveMode}
            selectedTrack={state.selectedTrack} selected={state.selected}
            followTrack={state.follow?.track ?? null}
            frameBoxes={state.frameBoxes} manualBoxesForFrame={manualBoxesForFrame}
            onScrub={handleScrub}
            onSelectModel={(track) => dispatch({ type: "TRACK_SELECTED", track })}
            onSelectManual={(id) => dispatch({ type: "MANUAL_SELECTED", id })}
            onBoxMoved={handleBoxMoved} onPencilBoxDrawn={handlePencilBoxDrawn}
            onPencilPreview={setHoverPreview}
            onNativeSizeDetected={(w, h) => dispatch({ type: "NATIVE_SIZE_DETECTED", width: w, height: h })}
            onZoomChange={setZoomPct}
          />
        </div>

        <aside className="review-sidebar">
          <section className="review-section">
            <h2>Legend</h2>
            <ul className="review-legend">
              <li><i className="review-sw followed" /> box you are following</li>
              <li><i className="review-sw mine" /> selected detection</li>
              <li><i className="review-sw other" /> other detection on this frame</li>
              <li><i className="review-sw carried" /> carried from the last inferred frame</li>
              <li><i className="review-sw manual" /> box you drew by hand</li>
              <li><i className="review-sw keep" /> judged a real bird</li>
              <li><i className="review-sw drop" /> judged a false positive</li>
              <li><i className="review-sw unsure" /> marked unsure</li>
            </ul>
          </section>

          <Magnifier imgRef={imgRef} nativeW={state.nativeW} point={magnifierPoint}
                     boxes={magnifierBoxes} hint={magnifierHint} />

          <VerdictPanel
            box={box} verdict={verdictId ? state.verdicts[verdictId] : null}
            canAct={!!verdictId} canUndo={state.history.length > 0}
            onMark={mark} onUndo={undoLast}
          />

          <AnnotationForm
            key={state.pencil ? "pencil" : (metaId ?? "none")}
            id={metaId} pencil={state.pencil} meta={metaId ? state.trackMeta[metaId] : null}
            speciesSeen={state.speciesSeen} disabled={disabledForm}
            onSave={saveMeta} onFieldsChange={(values) => { fieldsRef.current = values; }}
          />

          <ManualBoxStrip site={site} boxes={manualBoxesForFrame} />
        </aside>
      </div>
    </div>
  );
}
