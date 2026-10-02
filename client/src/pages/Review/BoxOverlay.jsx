import React, { useEffect, useRef, useState } from "react";

// The SVG box layer: rendering, click-to-select, drag-to-move, and the
// pencil tool's two-click box placement. Drag/pencil-point state is kept
// local (not in the reducer) -- a mouse-move fires far more often than
// anything that should re-render the rest of Review's sidebar.
export default function BoxOverlay({
  nativeW, nativeH, trail, currentIdx, pxScale, frameBoxes, manualBoxes, selectedTrack, selected,
  followTrack, pencil, moveMode,
  onSelectModel, onSelectManual, onBoxMoved, onPencilBoxDrawn, onPencilPreview,
}) {
  const svgRef = useRef(null);
  const dragRef = useRef(null); // {kind, key, startX, startY, startBbox}
  const [dragActive, setDragActive] = useState(null); // {kind, key}
  const [liveBBox, setLiveBBox] = useState(null);
  const [pencilPt, setPencilPt] = useState(null);

  useEffect(() => { setPencilPt(null); }, [pencil]);

  function toImage(clientX, clientY) {
    const r = svgRef.current.getBoundingClientRect();
    return [(clientX - r.left) / r.width * nativeW, (clientY - r.top) / r.height * nativeH];
  }

  function boxUnder(x, y) {
    const hits = [];
    frameBoxes.forEach((b) => {
      const [x0, y0, x1, y1] = b.bbox;
      if (x >= x0 && x <= x1 && y >= y0 && y <= y1) {
        hits.push({ kind: "model", key: b.track, area: (x1 - x0) * (y1 - y0) });
      }
    });
    manualBoxes.forEach((m) => {
      const [x0, y0, x1, y1] = m.bbox;
      if (x >= x0 && x <= x1 && y >= y0 && y <= y1) {
        hits.push({ kind: "manual", key: m.id, area: (x1 - x0) * (y1 - y0) });
      }
    });
    hits.sort((a, b) => a.area - b.area); // the smallest box wins
    return hits[0] || null;
  }

  function bboxFor(kind, key) {
    if (kind === "model") return frameBoxes.find((b) => b.track === key)?.bbox;
    return manualBoxes.find((m) => m.id === key)?.bbox;
  }

  function handleMouseDown(e) {
    if (pencil || !moveMode || !nativeW) return;
    const [x, y] = toImage(e.clientX, e.clientY);
    const hit = boxUnder(x, y);
    if (!hit) return;
    if (hit.kind === "model") onSelectModel(hit.key); else onSelectManual(hit.key);
    const bbox = bboxFor(hit.kind, hit.key);
    if (!bbox) return;
    e.preventDefault();
    dragRef.current = { kind: hit.kind, key: hit.key, startX: x, startY: y, startBbox: bbox.slice() };
    setDragActive({ kind: hit.kind, key: hit.key });
  }

  useEffect(() => {
    if (!dragActive) return undefined;
    function onMove(e) {
      const d = dragRef.current;
      if (!d) return;
      const [x, y] = toImage(e.clientX, e.clientY);
      const dx = x - d.startX, dy = y - d.startY;
      const [x0, y0, x1, y1] = d.startBbox;
      setLiveBBox([x0 + dx, y0 + dy, x1 + dx, y1 + dy]);
    }
    function onUp() {
      const d = dragRef.current;
      setLiveBBox((live) => {
        if (d && live) onBoxMoved(d.kind, d.key, live);
        return null;
      });
      dragRef.current = null;
      setDragActive(null);
    }
    addEventListener("mousemove", onMove);
    addEventListener("mouseup", onUp);
    return () => { removeEventListener("mousemove", onMove); removeEventListener("mouseup", onUp); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dragActive]);

  function handlePencilMove(e) {
    if (!pencil || !nativeW) return;
    const [x, y] = toImage(e.clientX, e.clientY);
    if (pencilPt) {
      const bbox = [Math.min(pencilPt[0], x), Math.min(pencilPt[1], y),
                    Math.max(pencilPt[0], x), Math.max(pencilPt[1], y)];
      onPencilPreview({
        point: { cx: (bbox[0] + bbox[2]) / 2, cy: (bbox[1] + bbox[3]) / 2 },
        boxes: [{ bbox, color: "#ff1a1a" }],
        hint: "click corner 2 to finish the box",
      });
    } else {
      onPencilPreview({ point: { cx: x, cy: y }, boxes: [], hint: "click corner 1" });
    }
  }

  function handlePencilClick(e) {
    if (!pencil || !nativeW) return;
    const [x, y] = toImage(e.clientX, e.clientY);
    if (!pencilPt) { setPencilPt([x, y]); return; }
    const bbox = [Math.min(pencilPt[0], x), Math.min(pencilPt[1], y),
                  Math.max(pencilPt[0], x), Math.max(pencilPt[1], y)];
    setPencilPt(null);
    if (bbox[2] - bbox[0] < 2 || bbox[3] - bbox[1] < 2) return;
    onPencilPreview({
      point: { cx: (bbox[0] + bbox[2]) / 2, cy: (bbox[1] + bbox[3]) / 2 },
      boxes: [{ bbox, color: "#ff1a1a" }],
      hint: "saved — click corner 1 for another, or P to stop",
    });
    onPencilBoxDrawn(bbox);
  }

  function isSelected(kind, key) {
    return !!selected && selected.kind === kind &&
      (kind === "model" ? selected.track === key : selected.id === key);
  }

  const draggedBBox = (kind, key) =>
    (dragActive && dragActive.kind === kind && dragActive.key === key && liveBBox) || null;

  return (
    <svg
      ref={svgRef}
      viewBox={`0 0 ${nativeW || 1} ${nativeH || 1}`}
      onMouseDown={handleMouseDown}
      onMouseMove={handlePencilMove}
      onClick={handlePencilClick}
    >
      {trail && trail.length >= 2 && (() => {
        // Dots stay a constant ~5 screen px however far the view is zoomed.
        const r = 5 / Math.max(pxScale || 1, 0.02);
        const pts = (list) => list.map(([, x, y]) => `${x},${y}`).join(" ");
        const past = trail.filter(([i]) => i <= currentIdx);
        const ahead = trail.filter(([i]) => i >= currentIdx);
        return (
          <g className="review-trail" pointerEvents="none">
            {past.length >= 2 && <polyline className="review-trail-past" points={pts(past)} />}
            {ahead.length >= 2 && <polyline className="review-trail-ahead" points={pts(ahead)} />}
            {trail.map(([i, x, y], k) => (
              <circle key={`${i}-${k}`} cx={x} cy={y} r={i === currentIdx ? r * 1.5 : r}
                      className={i === currentIdx ? "review-trail-now" : (i < currentIdx ? "review-trail-dot" : "review-trail-dot ahead")} />
            ))}
          </g>
        );
      })()}
      {frameBoxes.map((b) => {
        const [x0, y0, x1, y1] = draggedBBox("model", b.track) || b.bbox;
        const cls = b.track === selectedTrack ? "mine" : (b.carried ? "carried" : "other");
        return (
          <rect
            key={`m-${b.track}`}
            x={x0} y={y0} width={x1 - x0} height={y1 - y0}
            className={`review-box ${cls} ${isSelected("model", b.track) ? "is-selected" : ""}`}
            onClick={(e) => { if (pencil || dragActive) return; e.stopPropagation(); onSelectModel(b.track); }}
          />
        );
      })}
      {manualBoxes.map((m) => {
        const [x0, y0, x1, y1] = draggedBBox("manual", m.id) || m.bbox;
        const followed = followTrack != null && m.track === followTrack;
        return (
          <rect
            key={`u-${m.id}`}
            x={x0} y={y0} width={x1 - x0} height={y1 - y0}
            className={`review-box ${followed ? "followed" : "manual"} ${isSelected("manual", m.id) ? "is-selected" : ""}`}
            onClick={(e) => { if (pencil || dragActive) return; e.stopPropagation(); onSelectManual(m.id); }}
          />
        );
      })}
      {pencil && pencilPt && <circle className="review-pencil-pt" cx={pencilPt[0]} cy={pencilPt[1]} r={4} />}
    </svg>
  );
}
