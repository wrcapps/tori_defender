import React, { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api.js";
import "./Dataset.css";

const PAGE_SIZE = 48;

export default function Dataset() {
  const [summary, setSummary] = useState(null);
  const [category, setCategory] = useState("all");
  const [split, setSplit] = useState("val");
  const [offset, setOffset] = useState(0);
  const [hideDone, setHideDone] = useState(false);
  const [items, setItems] = useState(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState(null);
  const [lightboxId, setLightboxId] = useState(null);
  const [status, setStatusText] = useState("");

  function setStatus(text) {
    setStatusText(text);
    if (text) setTimeout(() => setStatusText(""), 3000);
  }

  const loadSummary = useCallback(async () => {
    try {
      const data = await api.datasetSummary();
      setSummary(data);
      setError(null);
      if (!(data.splits || []).some((s) => s.name === "val")) setSplit("all");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "network-error");
    }
  }, []);

  useEffect(() => { loadSummary(); }, [loadSummary]);

  const loadPage = useCallback(async () => {
    if (!summary || !summary.dataset) return;
    try {
      const data = await api.datasetItems({
        category, split, offset, limit: PAGE_SIZE, hide_done: hideDone ? 1 : 0,
      });
      setItems(data.items);
      setTotal(data.total);
    } catch {
      setStatus("could not load this page");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary, category, split, offset, hideDone]);

  useEffect(() => { loadPage(); }, [loadPage]);

  function selectCategory(name) {
    setCategory(name);
    setOffset(0);
  }

  function selectSplit(name) {
    setSplit(name);
    setOffset(0);
  }

  async function toggle(item) {
    const bad = !(item.decision && item.decision.bad);
    setItems((prev) => prev.map((it) => (it.id === item.id ? { ...it, decision: bad ? { bad: true } : {} } : it)));
    try {
      await api.datasetMark(item.id, bad);
      const wasJudged = item.decision && Object.keys(item.decision).length;
      setSummary((s) => ({
        ...s,
        rejected: s.rejected + (bad ? 1 : -1),
        reviewed: s.reviewed + (!wasJudged && bad ? 1 : 0) - (wasJudged && !bad ? 1 : 0),
      }));
    } catch {
      setItems((prev) => prev.map((it) => (it.id === item.id ? item : it)));
      setStatus("could not save that decision");
    }
  }

  useEffect(() => {
    function onKeyDown(e) {
      if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
      if (e.key === "Escape") { setLightboxId(null); return; }
      const maxOffset = Math.max(0, total - PAGE_SIZE);
      if ((e.key === "n" || e.key === "N") && offset < maxOffset) setOffset((o) => o + PAGE_SIZE);
      else if ((e.key === "p" || e.key === "P") && offset > 0) setOffset((o) => Math.max(0, o - PAGE_SIZE));
    }
    addEventListener("keydown", onKeyDown);
    return () => removeEventListener("keydown", onKeyDown);
  }, [offset, total]);

  if (error) {
    return <div className="state-page"><h2>Can't load Dataset</h2><p>{error}</p></div>;
  }
  if (summary === null) return <div className="state-page skeleton-block" style={{ height: 300 }} />;
  if (!summary.dataset) {
    return (
      <div className="state-page">
        <h2>No dataset configured</h2>
        <p>Start the app with <code>--dataset path/to/data.yaml</code> to enable label review.</p>
      </div>
    );
  }

  const rows = [{ name: "all", count: summary.labels, help: "every label, worst first" }]
    .concat(summary.categories || []);
  const pct = summary.labels ? (100 * summary.reviewed) / summary.labels : 0;
  const activeCat = (summary.categories || []).find((c) => c.name === category);
  const lightboxItem = items && items.find((i) => i.id === lightboxId);

  return (
    <div className="dataset-page">
      <div className="dataset-head">
        <span className="tick-label">Dataset</span>
        <span className="dataset-path">{summary.dataset}</span>
        {(summary.splits || []).length > 1 && (
          <div className="dataset-splits">
            {[{ name: "all", count: summary.labels }].concat(summary.splits).map((s) => (
              <button
                key={s.name} type="button"
                className={`dataset-split-btn ${s.name === split ? "is-current" : ""}`}
                onClick={() => selectSplit(s.name)}
              >
                {s.name} <span>{s.count}</span>
              </button>
            ))}
          </div>
        )}
        <span className="dataset-status">{status}</span>
      </div>

      <div className="dataset-progress">
        <div className="dataset-progress-bar"><div style={{ width: `${pct}%` }} /></div>
        <div className="dataset-counts">
          <b>{summary.reviewed}</b>/{summary.labels} labels judged &middot;{" "}
          <span className="dataset-rejected">{summary.rejected} rejected</span> &middot;{" "}
          {summary.tiles_with_labels} labelled tiles, {summary.tiles_empty} empty
        </div>
      </div>

      <div className="dataset-layout">
        <nav className="dataset-categories">
          {rows.map((c) => (
            <div
              key={c.name} className={`dataset-cat ${c.name === category ? "is-current" : ""}`}
              onClick={() => selectCategory(c.name)}
            >
              <div className="dataset-cat-name"><span>{c.name}</span><span>{c.count}</span></div>
              <div className="dataset-cat-help">{c.help}</div>
            </div>
          ))}
          <label className="dataset-hidedone">
            <input type="checkbox" checked={hideDone}
                   onChange={(e) => { setHideDone(e.target.checked); setOffset(0); }} />
            hide judged
          </label>
        </nav>

        <div className="dataset-main">
          <div className="dataset-pagebar">
            <span>{activeCat ? activeCat.help : ""}</span>
            <span className="dataset-pagelabel">
              {total ? `${offset + 1}–${offset + (items ? items.length : 0)} of ${total}` : "0 of 0"}
            </span>
            <button type="button" disabled={offset <= 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
              prev
            </button>
            <button type="button" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset((o) => o + PAGE_SIZE)}>
              next
            </button>
          </div>

          {items === null ? (
            <div className="dataset-grid">
              {Array.from({ length: 12 }).map((_, i) => <div key={i} className="skeleton-block" style={{ height: 120 }} />)}
            </div>
          ) : items.length === 0 ? (
            <div className="dataset-empty">Nothing left here.</div>
          ) : (
            <div className="dataset-grid">
              {items.map((it) => (
                <figure
                  key={it.id} className={`dataset-cell ${it.decision && it.decision.bad ? "is-bad" : ""}`}
                  onClick={(e) => (e.shiftKey ? setLightboxId(it.id) : toggle(it))}
                >
                  <img src={`/dsthumb/${encodeURIComponent(it.id)}`} alt={`label ${it.id}`} loading="lazy" />
                  <figcaption>
                    <span>{it.px[0]}&times;{it.px[1]}px</span>
                    <span className="dataset-split">{it.split}</span>
                  </figcaption>
                </figure>
              ))}
            </div>
          )}
        </div>
      </div>

      {lightboxItem && (
        <div className="dataset-lightbox" onClick={(e) => { if (e.target === e.currentTarget) setLightboxId(null); }}>
          <img src={`/dstile/${encodeURIComponent(lightboxItem.id)}`} alt="" />
          <div className="dataset-lightbox-info">
            {lightboxItem.id} &middot; {lightboxItem.px[0]}&times;{lightboxItem.px[1]}px &middot;{" "}
            {lightboxItem.category} &middot; yellow = this label, blue = others on the same tile
          </div>
          <div className="dataset-lightbox-actions">
            <button type="button" onClick={() => { toggle(lightboxItem); setLightboxId(null); }}>
              {lightboxItem.decision && lightboxItem.decision.bad ? "un-reject" : "reject this label"}
            </button>
            <button type="button" onClick={() => setLightboxId(null)}>close</button>
          </div>
        </div>
      )}
    </div>
  );
}
