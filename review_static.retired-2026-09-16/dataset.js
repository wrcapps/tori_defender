"use strict";

const CFG = window.APP_CONFIG || {};
const PAGE_SIZE = 48;

let summary = null;
let category = "all";
let offset = 0;
let items = [];
let hideDone = false;

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

let statusTimer = null;
function setStatus(text) {
  $("status").textContent = text;
  clearTimeout(statusTimer);
  if (text) statusTimer = setTimeout(() => { $("status").textContent = ""; }, 3000);
}

async function post(url, body) {
  try {
    const res = await fetch(url, {method: "POST", body: JSON.stringify(body)});
    return res.ok ? res : null;
  } catch (e) { return null; }
}

// ---------------------------------------------------------------- sidebar
async function loadSummary() {
  summary = await fetch("/api/dataset/summary").then(r => r.json());
  if (!summary.dataset) {
    $("catlist").innerHTML = `<div class=empty>No dataset configured.<br>
      Start the app with <code>--dataset path/to/data.yaml</code>.</div>`;
    $("grid").innerHTML = "";
    return false;
  }
  $("title").textContent = "training set";
  $("datasetpath").textContent = summary.dataset;
  const rows = [{name: "all", count: summary.labels, help: "every label, worst first"}]
    .concat(summary.categories);
  $("catlist").innerHTML = rows.map(c => `
    <div class="folder cat ${c.name === category ? "cur" : ""}" data-cat="${esc(c.name)}">
      <div class=name><span>${esc(c.name)}</span><span class="age">${c.count}</span></div>
      <div class=meta>${esc(c.help)}</div>
    </div>`).join("");
  $("catlist").querySelectorAll(".cat").forEach(el => {
    el.onclick = () => { category = el.dataset.cat; offset = 0; loadSummary(); loadPage(); };
  });
  updateCounts();
  return true;
}

function updateCounts() {
  if (!summary) return;
  const pct = summary.labels ? (100 * summary.reviewed / summary.labels) : 0;
  $("counts").innerHTML =
    `<b>${summary.reviewed}</b>/${summary.labels} labels judged &middot; ` +
    `<span class=drop>${summary.rejected} rejected</span> &middot; ` +
    `${summary.tiles_with_labels} labelled tiles, ${summary.tiles_empty} empty`;
  $("bar").style.width = pct + "%";
}

// ---------------------------------------------------------------- grid
async function loadPage() {
  const q = `category=${encodeURIComponent(category)}&offset=${offset}` +
            `&limit=${PAGE_SIZE}&hide_done=${hideDone ? 1 : 0}`;
  const data = await fetch(`/api/dataset/items?${q}`).then(r => r.json()).catch(() => null);
  if (!data) return;
  items = data.items;
  const from = data.total ? offset + 1 : 0;
  $("pagelabel").textContent = `${from}–${offset + items.length} of ${data.total}`;
  const cat = (summary.categories || []).find(c => c.name === category);
  $("cathelp").textContent = cat ? cat.help : "";
  $("prevpage").disabled = offset <= 0;
  $("nextpage").disabled = offset + PAGE_SIZE >= data.total;

  if (!items.length) {
    $("grid").innerHTML = `<div class=empty>Nothing left here.</div>`;
    return;
  }
  $("grid").innerHTML = items.map(it => `
    <figure class="cell ${it.decision && it.decision.bad ? "bad" : ""}" data-id="${esc(it.id)}">
      <img src="/dsthumb/${encodeURIComponent(it.id)}" alt="label ${esc(it.id)}" loading="lazy">
      <figcaption>
        <span>${it.px[0]}&times;${it.px[1]}px</span>
        <span class="who">${esc(it.split)}</span>
      </figcaption>
    </figure>`).join("");

  $("grid").querySelectorAll(".cell").forEach(el => {
    el.onclick = (e) => {
      // shift-click opens the whole tile: the cell is a crop around one box, and
      // sometimes the question is what ELSE is in that tile -- an unlabelled
      // bird next to the labelled one, say.
      if (e.shiftKey) { openTile(el.dataset.id); return; }
      toggle(el);
    };
  });
}

async function toggle(el) {
  const id = el.dataset.id;
  const bad = !el.classList.contains("bad");
  el.classList.toggle("bad", bad);          // optimistic: the grid must feel instant
  const res = await post("/api/dataset/mark", {id, bad});
  if (!res) {
    el.classList.toggle("bad", !bad);
    setStatus("could not save that decision");
    return;
  }
  const item = items.find(i => i.id === id);
  const wasJudged = item && item.decision && Object.keys(item.decision).length;
  if (item) item.decision = bad ? {bad: true} : {};
  summary.rejected += bad ? 1 : -1;
  if (!wasJudged && bad) summary.reviewed += 1;
  if (wasJudged && !bad) summary.reviewed -= 1;
  updateCounts();
}

// ---------------------------------------------------------------- lightbox
let lightboxId = null;
function openTile(id) {
  lightboxId = id;
  const it = items.find(i => i.id === id);
  $("lbimg").src = `/dstile/${encodeURIComponent(id)}`;
  $("lbinfo").textContent =
    `${it.id}  ·  ${it.px[0]}×${it.px[1]}px  ·  ${it.category}  ·  ` +
    `yellow = this label, blue = others on the same tile`;
  $("lbreject").textContent = (it.decision && it.decision.bad) ? "un-reject" : "reject this label";
  $("lightbox").hidden = false;
}
$("lbclose").onclick = () => { $("lightbox").hidden = true; };
$("lightbox").onclick = (e) => { if (e.target.id === "lightbox") $("lightbox").hidden = true; };
$("lbreject").onclick = () => {
  const el = $("grid").querySelector(`.cell[data-id="${CSS.escape(lightboxId)}"]`);
  if (el) toggle(el);
  $("lightbox").hidden = true;
};

// ---------------------------------------------------------------- controls
$("prevpage").onclick = () => { offset = Math.max(0, offset - PAGE_SIZE); loadPage(); };
$("nextpage").onclick = () => { offset += PAGE_SIZE; loadPage(); };
$("hidedone").onchange = (e) => { hideDone = e.target.checked; offset = 0; loadPage(); };

addEventListener("keydown", e => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
  if (e.key === "Escape") { $("lightbox").hidden = true; return; }
  if (e.key === "n" || e.key === "N") { if (!$("nextpage").disabled) $("nextpage").click(); }
  else if (e.key === "p" || e.key === "P") { if (!$("prevpage").disabled) $("prevpage").click(); }
});

loadSummary().then(ok => { if (ok) loadPage(); });
