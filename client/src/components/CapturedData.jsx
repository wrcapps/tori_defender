import React, { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api.js";
import { useSiteNames } from "../hooks/useSiteNames.js";
import "./CapturedData.css";

const PAGE_SIZE = 48;
const GROUPS = [
  { key: "unreviewed", label: "Not reviewed", help: "captured, nobody has judged whether it is a true or a false positive" },
  { key: "with_detections", label: "With detections", help: "windows a person has already judged or drawn on" },
  { key: "no_detections", label: "No detections", help: "single frames saved while the detector found nothing (hard negatives)" },
];

function errorText(err) {
  if (err instanceof ApiError) return err.message;
  return "Can't reach the server.";
}

export default function CapturedData() {
  const sites = useSiteNames();
  const [site, setSite] = useState(null);
  const [group, setGroup] = useState("unreviewed");
  const [counts, setCounts] = useState(null);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState(null);           // {total, items} | null while loading
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [zoom, setZoom] = useState(null);

  useEffect(() => { if (sites && sites.length && !site) setSite(sites[0]); }, [sites, site]);

  const load = useCallback(async () => {
    if (!site) return;
    try {
      const [summary, page] = await Promise.all([
        api.capturedSummary(site),
        api.capturedItems({ site, group, offset, limit: PAGE_SIZE }),
      ]);
      setCounts(summary.groups);
      setData(page);
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [site, group, offset]);

  useEffect(() => { setData(null); load(); }, [load]);

  function pick(g) { setGroup(g); setOffset(0); setSelected(new Set()); }

  function toggle(id) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  const items = data ? data.items : [];
  const allOnPage = items.length > 0 && items.every((i) => selected.has(i.id));

  async function removeSelected() {
    const ids = [...selected];
    if (!ids.length) return;
    const what = group === "no_detections" ? "frame(s)" : "window(s) with all their frames";
    if (!window.confirm(`Permanently delete ${ids.length} ${what}? This cannot be undone.`)) return;
    setBusy(true);
    try {
      const res = await api.capturedDelete(site, group, ids);
      setNote(`Deleted ${res.deleted.length}${res.skipped.length ? `, ${res.skipped.length} skipped` : ""}.`);
      setSelected(new Set());
      await load();
    } catch (err) {
      setNote(`Delete failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function sendToReview(item) {
    setBusy(true);
    try {
      await api.capturedToReview(site, item.id);
      setNote("Sent to Review: open it there to draw the box.");
      setSelected((prev) => { const n = new Set(prev); n.delete(item.id); return n; });
      await load();
    } catch (err) {
      setNote(`Could not send to Review: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  if (sites === null) return <div className="state-page skeleton-block" style={{ height: 200 }} />;
  if (!sites.length) return <div className="state-page"><h2>No sites</h2></div>;

  const active = GROUPS.find((g) => g.key === group);
  const zoomSrc = zoom && (zoom.kind === "negative"
    ? `/negative/${encodeURIComponent(site)}/${zoom.day}/${zoom.file}`
    : zoom.thumb);

  return (
    <div className="captured">
      <div className="captured-head">
        {sites.length > 1 && (
          <select value={site || ""} onChange={(e) => { setSite(e.target.value); setOffset(0); setSelected(new Set()); }}>
            {sites.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        )}
        <div className="captured-groups">
          {GROUPS.map((g) => (
            <button key={g.key} type="button" className={g.key === group ? "is-current" : ""} onClick={() => pick(g.key)}>
              {g.label} <span>{counts ? counts[g.key] : "…"}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="captured-bar">
        <span className="captured-help">{active.help}</span>
        <span className="captured-note" role="status">{note}</span>
        <label>
          <input type="checkbox" checked={allOnPage} disabled={!items.length}
                 onChange={() => setSelected(allOnPage ? new Set() : new Set(items.map((i) => i.id)))} />
          select page
        </label>
        <button type="button" className="captured-delete" disabled={busy || !selected.size} onClick={removeSelected}>
          Delete selected ({selected.size})
        </button>
        <span className="captured-page">
          {data && data.total ? `${offset + 1}–${offset + items.length} of ${data.total}` : "0 of 0"}
        </span>
        <button type="button" disabled={offset <= 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>prev</button>
        <button type="button" disabled={!data || offset + PAGE_SIZE >= data.total} onClick={() => setOffset((o) => o + PAGE_SIZE)}>next</button>
      </div>

      {error ? (
        <div className="state-page"><h2>Can't load captured data</h2><p>{error}</p>
          <button type="button" onClick={load}>Retry</button></div>
      ) : data === null ? (
        <div className="captured-grid">
          {Array.from({ length: 12 }).map((_, i) => <div key={i} className="skeleton-block" style={{ height: 150 }} />)}
        </div>
      ) : items.length === 0 ? (
        <div className="captured-empty">Nothing here.</div>
      ) : (
        <div className="captured-grid">
          {items.map((it) => (
            <figure key={it.id} className={`captured-cell ${selected.has(it.id) ? "is-selected" : ""}`}>
              <img src={it.thumb} alt={it.id} loading="lazy" onClick={() => toggle(it.id)} />
              <figcaption>
                <label>
                  <input type="checkbox" checked={selected.has(it.id)} onChange={() => toggle(it.id)} />
                  {it.camera}
                </label>
                <span>{it.kind === "negative" ? `${it.day} ${it.time}` : `${it.day} · ${it.tracks} track(s)`}</span>
                <span className="captured-actions">
                  <button type="button" onClick={() => setZoom(it)}>enlarge</button>
                  {it.kind === "negative" && (
                    <button type="button" disabled={busy} onClick={() => sendToReview(it)}>there's a bird → review</button>
                  )}
                </span>
              </figcaption>
            </figure>
          ))}
        </div>
      )}
      {group !== "no_detections" && items.length > 0 && (
        <p className="captured-foot">Judge these in <Link to="/app/review">Review</Link>; delete here what is not worth keeping.</p>
      )}

      {zoom && (
        <div className="captured-zoom" onClick={() => setZoom(null)}>
          <img src={zoomSrc} alt="" />
        </div>
      )}
    </div>
  );
}
