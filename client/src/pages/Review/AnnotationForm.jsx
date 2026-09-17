import React, { useEffect, useRef, useState } from "react";

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
export default function AnnotationForm({ id, pencil, meta, speciesSeen, disabled, onSave, onFieldsChange }) {
  const initial = pencil ? { species: "", distance: "", size: "" }
    : { species: meta?.species || "", distance: meta?.distance ?? "", size: meta?.size || "" };
  const [species, setSpecies] = useState(initial.species);
  const [distance, setDistance] = useState(String(initial.distance ?? ""));
  const [size, setSize] = useState(initial.size);
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
      <div className="review-hint">{hint}</div>
    </section>
  );
}
