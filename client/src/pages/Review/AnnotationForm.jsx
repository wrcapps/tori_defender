import React, { useEffect, useRef, useState } from "react";

// What each impact level triggers. The app records the decision; it is not connected to a
// deterrent or a turbine controller (see backend/risk.py), and the panel says so.
const DECISION_TEXT = { low: "no action", medium: "deterrent activated", high: "turbine stopped" };

function autoRisk(distance, policy) {
  if (!policy || distance === "" || distance == null) return null;
  const d = Number(distance);
  if (!Number.isFinite(d) || d < 0) return null;
  if (d < policy.high_below_m) return "high";
  if (d < policy.medium_below_m) return "medium";
  return "low";
}

// Species/distance/size, all optional. Debounced 400ms, flushed on blur and
// on page unload -- a save that silently never happened is worse than a
// visible delay, so every path that could lose an in-flight edit flushes it.
//
// Rendered with `key={pencil ? "pencil" : id ?? "none"}` by the caller: while
// the pencil is out, the form belongs to the box about to be drawn, not
// whatever detection happens to be selected underneath it, so it must not
// resync from `meta` on every render -- remounting on that key change is
// what gives it fresh, blank local state exactly when pencil mode starts
// (matching legacy's explicit clear in togglePencil), without silently
// overwriting what's being typed if a background selection changes while
// pencil stays on.
export default function AnnotationForm({ id, pencil, meta, speciesSeen, disabled, riskPolicy, onSave, onFieldsChange }) {
  const initial = pencil ? { species: "", distance: "", size: "", risk: "" }
    : { species: meta?.species || "", distance: meta?.distance ?? "", size: meta?.size || "", risk: meta?.risk || "" };
  const [species, setSpecies] = useState(initial.species);
  const [distance, setDistance] = useState(String(initial.distance ?? ""));
  const [size, setSize] = useState(initial.size);
  const [risk, setRisk] = useState(initial.risk);
  const timer = useRef(null);
  const pending = useRef(null);

  function schedule(next) {
    pending.current = { id, values: next };
    clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      const payload = pending.current;
      pending.current = null;
      if (payload && payload.id) onSave(payload.id, payload.values);
    }, 400);
  }

  function flush() {
    clearTimeout(timer.current);
    const payload = pending.current;
    pending.current = null;
    if (payload && payload.id) onSave(payload.id, payload.values);
  }

  useEffect(() => {
    onFieldsChange?.(initial);
    const handler = () => flush();
    addEventListener("beforeunload", handler);
    return () => { removeEventListener("beforeunload", handler); flush(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function readValues(overrides) {
    const d = overrides.distance ?? distance;
    return {
      species: (overrides.species ?? species).trim(),
      distance: d === "" ? "" : Number(d),
      size: overrides.size ?? size,
      risk: overrides.risk ?? risk,
    };
  }

  const hint = pencil ? "these fields will be attached to the box you draw next"
    : (id ? "applies to the selected detection" : "select a detection first");

  return (
    <section className="review-section">
      <h2>Annotation <span className="review-hint">(all optional)</span></h2>
      <div className="review-form">
        <label htmlFor="review-species">species</label>
        <input
          id="review-species" list="review-specieslist" autoComplete="off"
          placeholder="e.g. buzzard" disabled={disabled} value={species}
          onChange={(e) => {
            setSpecies(e.target.value);
            const next = readValues({ species: e.target.value });
            schedule(next); onFieldsChange?.(next);
          }}
          onBlur={flush}
        />
        <datalist id="review-specieslist">
          {speciesSeen.map((s) => <option key={s} value={s} />)}
        </datalist>

        <label htmlFor="review-distance">distance (m)</label>
        <input
          id="review-distance" type="number" min="0" step="1" placeholder="e.g. 250"
          disabled={disabled} value={distance}
          onChange={(e) => {
            setDistance(e.target.value);
            const next = readValues({ distance: e.target.value });
            schedule(next); onFieldsChange?.(next);
          }}
          onBlur={flush}
        />

        <label htmlFor="review-size">size</label>
        <select
          id="review-size" disabled={disabled} value={size}
          onChange={(e) => {
            setSize(e.target.value);
            const next = readValues({ size: e.target.value });
            schedule(next); onFieldsChange?.(next);
          }}
          onBlur={flush}
        >
          <option value="">&mdash;</option>
          <option value="small">small</option>
          <option value="medium">medium</option>
          <option value="large">large</option>
        </select>
      </div>
      <div className="review-form" style={{ marginTop: 6 }}>
        <label htmlFor="review-risk">impact risk</label>
        <select
          id="review-risk" disabled={disabled} value={risk}
          onChange={(e) => {
            setRisk(e.target.value);
            const next = readValues({ risk: e.target.value });
            schedule(next); onFieldsChange?.(next);
          }}
          onBlur={flush}
        >
          <option value="">auto (from distance)</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </select>
      </div>
      {(() => {
        const level = risk || autoRisk(distance, riskPolicy);
        if (!level) return <div className="review-hint">risk needs a distance (or pick a level)</div>;
        return (
          <div className="review-risk">
            <div>
              <span className={`risk-chip ${level}`}>{level.toUpperCase()}</span>{" "}
              &rarr; {DECISION_TEXT[level]}
            </div>
            <div
              className="review-hint"
              title={riskPolicy ? `High < ${riskPolicy.high_below_m} m, Medium < ${riskPolicy.medium_below_m} m, else Low` : ""}
            >
              logged to alerts.json, nothing actuated
              {!risk && riskPolicy && !riskPolicy.configured ? " · placeholder zones" : ""}
            </div>
          </div>
        );
      })()}
      <div className="review-hint">{hint}</div>
    </section>
  );
}
