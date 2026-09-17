import React, { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";
import BoxOverlay from "./BoxOverlay.jsx";

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// Image + zoom/pan viewport + scrubber. The native frame is several MB, so
// stepping through frames pulls a downscaled proxy by default; zooming past
// ~1x or drawing a box needs real pixels, and so does not yet knowing a
// folder's true frame size at all (using a downscaled load to "discover" the
// native size would record the DOWNSCALED size as native and misplace every
// box for the rest of the session) -- so `nativeSizeUnknown`, fixed for the
// whole time a folder is open, forces native-frame loads throughout it.
const FrameViewport = forwardRef(function FrameViewport({
  site, folder, frame, frameIdx, imgRef,
  nativeW, nativeH, nativeSizeUnknown,
  pencil, moveMode, selectedTrack, selected, followTrack,
  frameBoxes, manualBoxesForFrame,
  onScrub, onSelectModel, onSelectManual, onBoxMoved,
  onPencilBoxDrawn, onPencilPreview, onNativeSizeDetected, onZoomChange,
}, ref) {
  const viewportRef = useRef(null);
  const stageRef = useRef(null);
  const [scale, setScale] = useState(1);
  const scaleRef = useRef(1);
  useEffect(() => { scaleRef.current = scale; }, [scale]);

  const wantsNative = pencil || nativeSizeUnknown || scale > 1.05;

  function applyScale(next, anchorX, anchorY) {
    const viewport = viewportRef.current, stage = stageRef.current;
    if (!nativeW || !viewport || !stage) return;
    const rect = viewport.getBoundingClientRect();
    const ax = anchorX != null ? anchorX - rect.left : rect.width / 2;
    const ay = anchorY != null ? anchorY - rect.top : rect.height / 2;
    const beforeX = viewport.scrollLeft + ax, beforeY = viewport.scrollTop + ay;
    const clamped = clamp(next, 0.05, 24);
    const ratio = clamped / (scaleRef.current || 1);
    scaleRef.current = clamped;
    setScale(clamped);
    stage.style.width = `${nativeW * clamped}px`;
    stage.style.height = `${nativeH * clamped}px`;
    viewport.scrollLeft = beforeX * ratio - ax;
    viewport.scrollTop = beforeY * ratio - ay;
    onZoomChange(Math.round(clamped * 100));
  }

  function zoomBy(f, x, y) { applyScale(scaleRef.current * f, x, y); }
  function fitView() {
    const viewport = viewportRef.current;
    if (!nativeW || !viewport) return;
    applyScale(Math.min(viewport.clientWidth / nativeW, viewport.clientHeight / nativeH, 1));
    viewport.scrollLeft = 0;
    viewport.scrollTop = 0;
  }

  useImperativeHandle(ref, () => ({
    fit: fitView,
    zoomIn: () => zoomBy(1.25),
    zoomOut: () => zoomBy(0.8),
  }));

  // Whenever a folder's native size becomes known (or a new folder with a
  // different size opens), re-fit if the reviewer never zoomed away from the
  // default, or re-apply the current zoom to the (possibly new) dimensions.
  useEffect(() => {
    if (!nativeW) return;
    if (scaleRef.current === 1 && !wantsNative) fitView(); else applyScale(scaleRef.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nativeW, nativeH]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return undefined;
    function onWheel(e) {
      if (!nativeW) return;
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX, e.clientY);
    }
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", onWheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nativeW]);

  function handleImgLoad(e) {
    if (!nativeW) onNativeSizeDetected(e.target.naturalWidth, e.target.naturalHeight);
  }

  const framePath = frame ? `${folder.day}/${folder.window}/${frame.file}` : null;
  const src = framePath ? `${wantsNative ? "/frame/" : "/proxy/"}${site}/${framePath}` : null;
  const totalFrames = folder ? folder.frames.length : 0;

  return (
    <div className="review-viewport-wrap">
      <div ref={viewportRef} className="review-viewport">
        <div ref={stageRef} className="review-stage">
          {src && (
            <img ref={imgRef} src={src} draggable={false} alt="" onLoad={handleImgLoad} />
          )}
          <BoxOverlay
            nativeW={nativeW} nativeH={nativeH}
            frameBoxes={frameBoxes} manualBoxes={manualBoxesForFrame}
            selectedTrack={selectedTrack} selected={selected} followTrack={followTrack}
            pencil={pencil} moveMode={moveMode}
            onSelectModel={onSelectModel} onSelectManual={onSelectManual}
            onBoxMoved={onBoxMoved} onPencilBoxDrawn={onPencilBoxDrawn} onPencilPreview={onPencilPreview}
          />
        </div>
      </div>

      <div className="review-scrubber">
        <button type="button" onClick={() => onScrub(frameIdx - 1, true)} title="previous frame">&larr;</button>
        <input
          type="range" min={0} max={Math.max(0, totalFrames - 1)} value={frameIdx}
          onChange={(e) => onScrub(Number(e.target.value), true)}
        />
        <button type="button" onClick={() => onScrub(frameIdx + 1, true)} title="next frame">&rarr;</button>
        <span className="review-framelabel">
          {folder && totalFrames ? `frame ${frameIdx + 1} / ${totalFrames}` : "—"}
        </span>
      </div>
      <div className="review-ticks" title="frames with detections">
        {folder && totalFrames > 0 && folder.frames.map((f, i) => f.boxes ? (
          <i key={i} style={{ left: `${(i / Math.max(1, totalFrames - 1)) * 100}%` }} />
        ) : null)}
      </div>
    </div>
  );
});

export default FrameViewport;
