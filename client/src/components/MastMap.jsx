import React from "react";
import { summarize } from "../lib/grouping.js";
import "./MastMap.css";

const KIND_COLOR = {
  live: "var(--accent)",
  starting: "var(--info)",
  stale: "var(--warn)",
  "no-signal": "var(--text-faint)",
  error: "var(--danger)",
};

const SIZE = 280;
const CENTER = SIZE / 2;
const RADIUS = 100;
const NODE_R = 22;

// A schematic layout, not a survey drawing: node order follows config order,
// not measured compass bearing, and is spaced evenly regardless of the real
// angle between masts. Earlier drafts considered hardcoding a "6 masts, 60
// degrees apart" true layout (this project has measured that geometry for
// Babadag), but that number lives in tower-survey documents this app has no
// access to verify against at runtime -- a wrong hardcoded angle would be a
// silent, unnoticeable inaccuracy in front of a client. This draws only what
// the backend actually knows (how many groups, each one's live status) and
// says so below the diagram, rather than implying a precision it doesn't have.
export default function MastMap({ groups, activeKey, onSelect }) {
  if (groups.length <= 1) {
    const group = groups[0];
    const kind = group ? summarize(group.cameras).kind : "no-signal";
    return (
      <div className="mast-map mast-map-single">
        <div className="mast-node" style={{ "--node-color": KIND_COLOR[kind] }}>
          <span className="mast-node-dot" />
        </div>
        <p className="mast-map-caption">{group ? group.cameras.length : 0} camera(s)</p>
      </div>
    );
  }

  const nodes = groups.map((group, i) => {
    const angle = (i / groups.length) * Math.PI * 2 - Math.PI / 2;
    const x = CENTER + RADIUS * Math.cos(angle);
    const y = CENTER + RADIUS * Math.sin(angle);
    const kind = summarize(group.cameras).kind;
    return { group, x, y, kind, index: i + 1 };
  });

  return (
    <div className="mast-map">
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label="Schematic layout of camera masts">
        {nodes.map((n) => (
          <line
            key={`line-${n.group.key}`}
            x1={CENTER} y1={CENTER} x2={n.x} y2={n.y}
            stroke="var(--border)" strokeWidth="1.5"
          />
        ))}
        <circle cx={CENTER} cy={CENTER} r={10} fill="var(--surface-3)" stroke="var(--border)" />
        {nodes.map((n) => (
          <g
            key={n.group.key}
            transform={`translate(${n.x}, ${n.y})`}
            className={`mast-map-node ${activeKey === n.group.key ? "is-active" : ""}`}
            onClick={() => onSelect(n.group.key)}
            role="button"
            tabIndex={0}
            aria-label={`Mast ${n.index}, ${n.kind}`}
            onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(n.group.key)}
          >
            <circle r={NODE_R} fill="var(--surface-2)" stroke={KIND_COLOR[n.kind]} strokeWidth="2.5" />
            <text textAnchor="middle" dy="5" className="mast-map-node-label">{n.index}</text>
          </g>
        ))}
      </svg>
      <p className="mast-map-caption">Schematic layout — not to scale or true bearing</p>
    </div>
  );
}
