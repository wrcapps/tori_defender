"use strict";

// ---------------------------------------------------------------- state
const CFG = window.APP_CONFIG || {default_site: "site", model: "live", sites: []};
// The site currently open in Review. Switching it wipes every piece of state
// below that belongs to the old site (folders, verdicts, notes, manual boxes) --
// none of it means anything once the site underneath it has changed.
let currentSite = CFG.default_site || (CFG.sites[0] && CFG.sites[0].name) || "site";

let folders = [];           // session list from /api/folders
let folder = null;          // the open session: {id, day, window, width, height, frames[], uncertain[]}
let frameIdx = 0;
let frameBoxes = [];        // model boxes on the frame in view
let selectedTrack = null;   // which detection the verdict/annotation panels act on

let verdicts = {};          // {track_id: keep|drop|unsure}
let trackMeta = {};         // {track_id: {species, distance, size}}
let manualBoxes = [];
let speciesSeen = [];
let history = [];           // [{id, prev}] for undo

let nativeW = 0, nativeH = 0, scale = 1;
// Bumped every time the view moves (another session, another frame). Any fetch
// started before the bump is stale when it lands, and applying it would paint
// one frame's boxes over another's image -- or worse, let a verdict be written
// against the session the reviewer just left.
let generation = 0;
// Set when the server could not measure the session's native frame size. The
// overlay is drawn in native coordinates, so until that is known the native
// frame itself has to be loaded -- guessing from the downscaled proxy would put
// every box, and every hand-drawn one that gets SAVED, at the wrong scale.
let nativeSizeUnknown = false;
let pencil = false, pencilPt = null;
let metaTimer = null;

// Editing state. `selected` is whichever box the delete/move/follow buttons act
// on: either one of the model's ({kind:"model", track}) or one you drew
// ({kind:"manual", id}).
let selected = null;
let moveMode = false;
let drag = null;              // {startX, startY, bbox} while a box is being dragged
// A followed bird: the manual track its boxes share, plus the last two positions,
// which is what the next frame's position is predicted from.
let follow = null;            // {track, history: [[cx,cy,w,h], ...]}

const $ = (id) => document.getElementById(id);
const viewport = $("viewport"), stage = $("stage"), img = $("img"), svg = $("svg");
const mag = $("mag"), magCtx = mag.getContext("2d");
magCtx.imageSmoothingEnabled = false;

const MAG_NATIVE = 224;   // matches the saved crops, so live magnifier and crops agree
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const trackId = (day, window_, track) => `${day}/${window_}/t${String(track).padStart(4, "0")}`;
const siteUrl = (url) => `${url}${url.includes("?") ? "&" : "?"}site=${encodeURIComponent(currentSite)}`;

// Transient feedback goes in the toolbar, beside the controls that cause it --
// the sidebar is too far from where the reviewer is looking to be noticed.
let statusTimer = null;
function setStatus(text) {
  $("status").textContent = text;
  clearTimeout(statusTimer);
  if (text) statusTimer = setTimeout(() => { $("status").textContent = ""; }, 4000);
}

function currentFrame() {
  if (!folder || !folder.frames.length) return null;
  return folder.frames[clamp(frameIdx, 0, folder.frames.length - 1)];
}
function framePath(file) { return `${folder.day}/${folder.window}/${file}`; }

// The native frame is several MB; the scrubber pulls a downscaled proxy so
// stepping through frames stays responsive over a forwarded port. Zooming in or
// drawing a box needs the real pixels, so those switch to the native frame.
function wantsNative() { return pencil || nativeSizeUnknown || scale > 1.05; }

// ---------------------------------------------------------------- sessions
async function loadFolders(keepOpen = true) {
  const data = await fetch(siteUrl("/api/folders")).then(r => r.json());
  folders = data.folders || [];
  if (data.species) setSpecies(data.species);
  renderFolders();
  if (!keepOpen || !folder) {
    const first = folders.find(f => !f.reviewed) || folders[0];
    if (first) openFolder(first.id);
  }
}

function renderFolders() {
  const list = $("folderlist");
  if (!folders.length) {
    list.innerHTML = `<div class=empty>No capture sessions yet.<br>Run live_capture.py, or point
      <code>--frames-root</code> at existing frames.</div>`;
    return;
  }
  const todo = folders.filter(f => !f.reviewed);
  const done = folders.filter(f => f.reviewed);
  list.innerHTML =
    group("To review", todo) +
    (done.length ? group("Reviewed manually", done) : "");

  list.querySelectorAll(".folder").forEach(el => {
    el.onclick = () => openFolder(el.dataset.id);
    const tick = el.querySelector(".tickbtn");
    if (tick) tick.onclick = (e) => { e.stopPropagation(); toggleReviewed(el.dataset.id); };
  });
}

function group(title, items) {
  return `<div class=grouphead>${title} &middot; ${items.length}</div>` + items.map(f => `
    <div class="folder ${folder && folder.id === f.id ? "cur" : ""}" data-id="${f.id}">
      <div class=name>
        <span>${f.camera ? esc(f.camera) + " " : ""}${esc(f.window)}</span>
        ${f.reviewed ? '<span class=tick title="reviewed manually">&#10003;</span>' : ""}
        ${f.recording ? '<span class=rec title="still being written">&#9679; REC</span>' : ""}
        <button class="tickbtn" style="margin-left:auto" title="${f.reviewed
          ? "move back to the review queue" : "mark this session reviewed"}"
          ${f.recording ? "disabled" : ""}>${f.reviewed ? "undo" : "done"}</button>
      </div>
      <div class=meta>${esc(f.day)} &middot; ${f.frames} frames &middot; ${f.tracks} detections${
        f.undecided ? ` &middot; ${f.undecided} undecided` : ""}</div>
    </div>`).join("");
}

async function toggleReviewed(id) {
  const entry = folders.find(f => f.id === id);
  if (!entry) return;
  const res = await fetch("/api/folder_status", {
    method: "POST", body: JSON.stringify({id, reviewed: !entry.reviewed})
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    setStatus(err.error || "could not update that session");
    return;
  }
  entry.reviewed = !entry.reviewed;
  renderFolders();
}

async function openFolder(id) {
  if (follow) stopFollow("stopped following: different session");
  const gen = ++generation;
  const opened = await fetch(siteUrl(`/api/folder?id=${encodeURIComponent(id)}`))
    .then(r => r.json()).catch(() => null);
  if (!opened || gen !== generation) return;
  folder = opened;
  nativeSizeUnknown = !folder.width;
  frameIdx = 0;
  selectedTrack = null;
  pencilPt = null;
  nativeW = folder.width || 0;
  nativeH = folder.height || 0;
  $("foldername").textContent = `${folder.day} / ${folder.window}`;
  renderFolders();
  renderTicks();
  showFrame(0);
}

// ---------------------------------------------------------------- frames
function renderTicks() {
  if (!folder || !folder.frames.length) { $("ticks").innerHTML = ""; return; }
  const n = folder.frames.length;
  $("ticks").innerHTML = folder.frames.map((f, i) =>
    f.boxes ? `<i style="left:${(i / Math.max(1, n - 1)) * 100}%"></i>` : "").join("");
}

async function showFrame(i, keepSelection = false) {
  if (!folder || !folder.frames.length) {
    img.removeAttribute("src"); svg.innerHTML = ""; $("framelabel").textContent = "—";
    $("info").textContent = "no frames in this session";
    return;
  }
  frameIdx = clamp(i, 0, folder.frames.length - 1);
  if (!keepSelection) pencilPt = null;
  const frame = currentFrame();

  $("scrub").max = String(folder.frames.length - 1);
  $("scrub").value = String(frameIdx);
  $("framelabel").textContent = `frame ${frameIdx + 1} / ${folder.frames.length}`;

  img.onload = () => {
    if (!nativeW) { nativeW = img.naturalWidth; nativeH = img.naturalHeight; }
    // Without an explicit viewBox the SVG's user units follow its rendered CSS
    // size, so a box at native (x, y) lands in the wrong place entirely.
    svg.setAttribute("viewBox", `0 0 ${nativeW} ${nativeH}`);
    if (scale === 1 && !wantsNative()) fitView();
    else applyScale(scale);
    renderOverlay();
  };
  img.src = `${wantsNative() ? "/frame/" : "/proxy/"}${currentSite}/${framePath(frame.file)}`;

  const gen = ++generation;
  const fetched = await fetchBoxes(frame.file);
  if (gen !== generation) return;   // the reviewer has already scrubbed on
  frameBoxes = fetched;
  if (!keepSelection || !frameBoxes.some(b => b.track === selectedTrack)) {
    selectedTrack = frameBoxes.length
      ? frameBoxes.reduce((a, b) => (b.conf < a.conf ? b : a)).track
      : null;
  }
  renderOverlay();
  renderSidebar();

  if (follow) {
    const here = frameManualBoxes().some(b => b.track === follow.track);
    if (!here) placeFollowBox();
  }
}

function fetchBoxes(file) {
  const q = `day=${encodeURIComponent(folder.day)}&window=${encodeURIComponent(folder.window)}` +
            `&file=${encodeURIComponent(file)}`;
  return fetch(siteUrl(`/api/boxes?${q}`)).then(r => r.json()).catch(() => []);
}

function frameManualBoxes() {
  const frame = currentFrame();
  if (!frame) return [];
  return manualBoxes.filter(b => b.day === folder.day && b.window === folder.window &&
                                 b.file === frame.file);
}

function renderOverlay() {
  if (!folder || !nativeW) { svg.innerHTML = ""; return; }
  const isSel = (kind, key) => selected && selected.kind === kind &&
    (kind === "model" ? selected.track === key : selected.id === key);
  let html = frameBoxes.map(b => {
    const [x0, y0, x1, y1] = b.bbox;
    const cls = b.track === selectedTrack ? "mine" : (b.carried ? "carried" : "other");
    return `<rect x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}"
      class="box ${cls} ${isSel("model", b.track) ? "selected" : ""}"
      data-track="${b.track}"></rect>`;
  }).join("");
  html += frameManualBoxes().map(b => {
    const [x0, y0, x1, y1] = b.bbox;
    const followed = follow && b.track === follow.track;
    return `<rect x="${x0}" y="${y0}" width="${x1 - x0}" height="${y1 - y0}"
      class="box ${followed ? "followed" : "manual"} ${isSel("manual", b.id) ? "selected" : ""}"
      data-manual="${b.id}"></rect>`;
  }).join("");
  if (pencilPt) html += `<circle class=pt cx="${pencilPt[0]}" cy="${pencilPt[1]}" r="4"></circle>`;
  svg.innerHTML = html;
  svg.querySelectorAll("rect[data-track]").forEach(r => {
    r.onclick = (e) => {
      if (pencil || drag) return;
      e.stopPropagation();
      selectTrack(Number(r.dataset.track));
    };
  });
  svg.querySelectorAll("rect[data-manual]").forEach(r => {
    r.onclick = (e) => {
      if (pencil || drag) return;
      e.stopPropagation();
      selectManual(r.dataset.manual);
    };
  });
  if (!pencil) magnifySelected();
}

function selectTrack(track) {
  selectedTrack = track;
  selected = {kind: "model", track};
  renderOverlay();
  renderSidebar();
}

function selectManual(id) {
  selected = {kind: "manual", id};
  renderOverlay();
  renderSidebar();
}

function selectedBox() {
  if (!selected) return null;
  if (selected.kind === "model") {
    const b = frameBoxes.find(x => x.track === selected.track);
    return b ? {bbox: b.bbox, model: b} : null;
  }
  const m = manualBoxes.find(x => x.id === selected.id);
  return m ? {bbox: m.bbox, manual: m} : null;
}

// Saving a moved box: a hand-drawn one is rewritten in place, a model one gets
// a correction recorded beside detections.jsonl rather than in it.
async function saveBox(bbox) {
  if (!selected) return;
  if (selected.kind === "manual") {
    const res = await post("/api/manual_box/update", {id: selected.id, bbox});
    if (!res) { setStatus("could not save that move"); return; }
    const row = await res.json();
    const i = manualBoxes.findIndex(m => m.id === row.id);
    if (i >= 0) manualBoxes[i] = row;
  } else {
    const frame = currentFrame();
    const res = await post("/api/box_edit", {day: folder.day, window: folder.window,
                                             file: frame.file, track: selected.track, bbox});
    if (!res) { setStatus("could not save that move"); return; }
    const b = frameBoxes.find(x => x.track === selected.track);
    if (b) { b.bbox = bbox; b.edited = true; }
  }
}

async function deleteSelected() {
  const sel = selectedBox();
  if (!sel) return;
  if (selected.kind === "manual") {
    if (!await post("/api/manual_box/delete", {id: selected.id})) {
      setStatus("could not delete that box"); return;
    }
    manualBoxes = manualBoxes.filter(m => m.id !== selected.id);
    setStatus("box deleted");
  } else {
    const frame = currentFrame();
    if (!await post("/api/box_edit", {day: folder.day, window: folder.window,
                                      file: frame.file, track: selected.track, deleted: true})) {
      setStatus("could not remove that detection"); return;
    }
    frameBoxes = frameBoxes.filter(b => b.track !== selected.track);
    setStatus("detection removed from this frame (the track's verdict is separate)");
  }
  selected = null;
  renderOverlay();
  renderSidebar();
}

// ---------------------------------------------------------------- sidebar
function renderTools() {
  $("movebtn").classList.toggle("active", moveMode);
  $("followbtn").classList.toggle("active", !!follow);
  $("gonebtn").disabled = !follow;
  $("deletebtn").disabled = !selectedBox();
  viewport.classList.toggle("moving", moveMode && !drag);
}

function renderSidebar() {
  const box = frameBoxes.find(b => b.track === selectedTrack);
  const id = box ? trackId(folder.day, folder.window, box.track) : null;
  const verdict = id ? verdicts[id] : null;

  $("info").innerHTML = !box ? "no detection on this frame" : `<dl>
    <dt>detection</dt><dd>t${String(box.track).padStart(4, "0")}</dd>
    <dt>confidence</dt><dd>${box.conf.toFixed(2)}${box.carried ? " (carried)" : ""}</dd>
    <dt>size</dt><dd>${Math.round(box.bbox[2] - box.bbox[0])}&times;${Math.round(box.bbox[3] - box.bbox[1])} px</dd>
    <dt>verdict</dt><dd class="verdicttag ${verdict || ""}">${verdict || "undecided"}</dd>
  </dl>`;

  // While the pencil is out the form belongs to the box about to be drawn, not
  // to the selected detection -- repopulating it here would silently hand a new
  // box the previous detection's species, which is the one thing it must not do.
  if (!pencil) {
    const meta = (id && trackMeta[id]) || {};
    if (document.activeElement !== $("species")) $("species").value = meta.species || "";
    if (document.activeElement !== $("distance")) $("distance").value = meta.distance ?? "";
    if (document.activeElement !== $("size")) $("size").value = meta.size || "";
  }
  const disabled = !id && !pencil;
  ["species", "distance", "size"].forEach(f => { $(f).disabled = disabled; });
  $("annotationhint").textContent = pencil
    ? "these fields will be attached to the box you draw next"
    : (id ? "applies to the selected detection" : "select a detection first");

  ["keepbtn", "dropbtn", "unsurebtn"].forEach(b => { $(b).disabled = !id; });
  $("undobtn").disabled = history.length === 0;
  renderTools();

  const manual = frameManualBoxes().filter(b => b.crop);
  $("manualwrap").hidden = manual.length === 0;
  $("manualstrip").innerHTML = manual.map(b => `<img src="/img/${currentSite}/${b.crop}" alt="manual box">`).join("");

  updateCounts();
}

function updateCounts() {
  if (!folder) { $("counts").textContent = ""; $("bar").style.width = "0%"; return; }
  const entry = folders.find(f => f.id === folder.id);
  const total = entry ? entry.tracks : 0;
  const undecided = entry ? entry.undecided : 0;
  const done = Math.max(0, total - undecided);
  $("counts").innerHTML = total
    ? `<b>${done}</b>/${total} detections judged in this session`
    : "no detections in this session";
  $("bar").style.width = total ? `${(100 * done) / total}%` : "0%";
}

function setSpecies(list) {
  speciesSeen = list;
  $("specieslist").innerHTML = list.map(s => `<option value="${esc(s)}">`).join("");
}

// ---------------------------------------------------------------- magnifier
function magnify(cx, cy, boxes) {
  if (!nativeW || !img.naturalWidth) return;
  // The displayed image may be a downscaled proxy, so native coordinates have
  // to be converted into the loaded image's own pixel space.
  const k = img.naturalWidth / nativeW;
  const span = MAG_NATIVE * k;
  const sx = clamp(cx * k - span / 2, 0, Math.max(0, img.naturalWidth - span));
  const sy = clamp(cy * k - span / 2, 0, Math.max(0, img.naturalHeight - span));
  magCtx.clearRect(0, 0, 260, 260);
  magCtx.drawImage(img, sx, sy, span, span, 0, 0, 260, 260);
  const z = 260 / span;
  (boxes || []).forEach(({bbox, color}) => {
    const [x0, y0, x1, y1] = bbox;
    magCtx.strokeStyle = color;
    magCtx.lineWidth = 2;
    magCtx.strokeRect((x0 * k - sx) * z, (y0 * k - sy) * z,
                      (x1 - x0) * k * z, (y1 - y0) * k * z);
  });
}

function magnifySelected() {
  const box = frameBoxes.find(b => b.track === selectedTrack);
  if (!box) { magCtx.clearRect(0, 0, 260, 260); $("maghint").textContent = "no detection selected"; return; }
  const [x0, y0, x1, y1] = box.bbox;
  magnify((x0 + x1) / 2, (y0 + y1) / 2, [{bbox: box.bbox, color: "#f0c14b"}]);
  $("maghint").textContent = "selected detection, zoomed";
}

// ---------------------------------------------------------------- zoom/pan
function applyScale(next, anchorX, anchorY) {
  if (!nativeW) return;
  const rect = viewport.getBoundingClientRect();
  const ax = anchorX != null ? anchorX - rect.left : rect.width / 2;
  const ay = anchorY != null ? anchorY - rect.top : rect.height / 2;
  const beforeX = viewport.scrollLeft + ax, beforeY = viewport.scrollTop + ay;
  const wasNative = wantsNative();
  const ratio = next / scale;
  scale = clamp(next, 0.05, 24);
  stage.style.width = (nativeW * scale) + "px";
  stage.style.height = (nativeH * scale) + "px";
  viewport.scrollLeft = beforeX * ratio - ax;
  viewport.scrollTop = beforeY * ratio - ay;
  $("zoomlabel").textContent = Math.round(scale * 100) + "%";
  // Crossing into zoomed-in territory swaps the proxy for the real pixels.
  if (wantsNative() !== wasNative) {
    const frame = currentFrame();
    if (frame) img.src = `${wantsNative() ? "/frame/" : "/proxy/"}${currentSite}/${framePath(frame.file)}`;
  }
}
function zoomBy(f, x, y) { applyScale(scale * f, x, y); }
function fitView() {
  if (!nativeW) return;
  applyScale(Math.min(viewport.clientWidth / nativeW, viewport.clientHeight / nativeH, 1));
  viewport.scrollLeft = 0;
  viewport.scrollTop = 0;
}
viewport.addEventListener("wheel", e => {
  if (!nativeW) return;
  e.preventDefault();
  zoomBy(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX, e.clientY);
}, {passive: false});

function toImage(clientX, clientY) {
  const r = svg.getBoundingClientRect();
  return [(clientX - r.left) / r.width * nativeW, (clientY - r.top) / r.height * nativeH];
}

// ---------------------------------------------------------------- verdicts
async function mark(v) {
  if (selectedTrack == null || !folder) return;
  const id = trackId(folder.day, folder.window, selectedTrack);
  history.push({id, prev: verdicts[id]});
  verdicts[id] = v;
  bumpUndecided(verdicts[id], history[history.length - 1].prev);
  renderSidebar();
  const ok = await post("/api/verdict", {id, verdict: v});
  if (!ok) {
    // Put the UI back where the saved state actually is, rather than showing a
    // verdict that only exists in this tab.
    const last = history.pop();
    if (last.prev === undefined) delete verdicts[id]; else verdicts[id] = last.prev;
    bumpUndecided(last.prev, v);
    renderSidebar();
    setStatus("could not save that verdict -- is the review server still running?");
  }
}

// Every write goes through here so a failure is surfaced instead of swallowed:
// a save that silently did not happen is worse than an error, because the
// reviewer moves on believing the work is recorded.
async function post(url, body) {
  try {
    const res = await fetch(url, {method: "POST", body: JSON.stringify({site: currentSite, ...body})});
    return res.ok ? res : null;
  } catch (e) {
    return null;
  }
}

async function undoLast() {
  const last = history.pop();
  if (!last) return;
  const prev = verdicts[last.id];
  if (last.prev === undefined) delete verdicts[last.id]; else verdicts[last.id] = last.prev;
  bumpUndecided(last.prev, prev);
  renderSidebar();
  if (!await post("/api/verdict",
                  {id: last.id, verdict: last.prev === undefined ? "clear" : last.prev})) {
    setStatus("could not save that undo -- is the review server still running?");
  }
}

function bumpUndecided(now, before) {
  const entry = folders.find(f => f.id === folder.id);
  if (!entry) return;
  if (before === undefined && now !== undefined) entry.undecided = Math.max(0, entry.undecided - 1);
  if (before !== undefined && now === undefined) entry.undecided += 1;
  renderFolders();
}

function jumpUncertain() {
  const next = (folder ? folder.uncertain : []).find(u => !verdicts[u.id]);
  if (!next) {
    setStatus("nothing undecided left in this session");
    return;
  }
  selectedTrack = next.track;
  setStatus(`jumped to the lowest-confidence undecided detection (${next.conf.toFixed(2)})`);
  showFrame(next.index, true);
}

// ---------------------------------------------------------------- annotation
// The track id is captured when the field changes, not when the timer fires:
// stepping to another frame mid-debounce would otherwise save these values
// against whatever detection happens to be selected by then.
function scheduleMetaSave() {
  // With the pencil out these values are destined for the next hand-drawn box,
  // so they must not be written onto the selected detection as a side effect.
  if (pencil || selectedTrack == null || !folder) return;
  const id = trackId(folder.day, folder.window, selectedTrack);
  const values = readForm();
  clearTimeout(metaTimer);
  metaTimer = setTimeout(() => saveMeta(id, values), 400);
}

function flushMetaSave() {
  if (!metaTimer) return;
  clearTimeout(metaTimer);
  metaTimer = null;
  if (pencil || selectedTrack == null || !folder) return;
  saveMeta(trackId(folder.day, folder.window, selectedTrack), readForm());
}

function readForm() {
  const distance = $("distance").value.trim();
  return {
    species: $("species").value.trim(),
    distance: distance === "" ? "" : Number(distance),
    size: $("size").value,
  };
}

async function saveMeta(id, values) {
  const response = await post("/api/track_meta", {id, ...values});
  const res = response ? await response.json().catch(() => null) : null;
  if (!res) { setStatus("could not save those notes"); return; }
  if (Object.keys(res.meta || {}).length) trackMeta[id] = res.meta; else delete trackMeta[id];
  // Grown locally as well as on the server, so a species typed now autocompletes
  // on the next detection instead of only after a reload.
  if (res.species) setSpecies(res.species);
}

// ---------------------------------------------------------------- follow
// Carrying one bird across a window: the box is re-created on each frame you
// step to, at the position its own recent motion predicts. A bird holding a
// steady line often needs no correction at all -- you just hold the arrow key.
async function startFollow() {
  const sel = selectedBox();
  if (!sel) { setStatus("select a box first, then follow it"); return; }
  const res = await post("/api/manual_track", {});
  if (!res) { setStatus("could not start a track"); return; }
  const {track} = await res.json();
  follow = {track, history: [sel.bbox.slice()]};
  moveMode = true;                     // you will be nudging it, every frame
  renderTools();
  setStatus(`following as ${track} — step frames with →, X when it leaves the scene`);
}

function stopFollow(reason) {
  if (!follow) return;
  const track = follow.track;
  follow = null;
  renderTools();
  setStatus(reason || `stopped following ${track}`);
}

function predictNext() {
  const h = follow.history;
  const last = h[h.length - 1];
  if (h.length < 2) return last.slice();
  const prev = h[h.length - 2];
  // constant velocity: where it would be if it keeps the line and speed it
  // has just been on
  const dx = last[0] - prev[0], dy = last[1] - prev[1];
  const [x0, y0, x1, y1] = last;
  return [
    clamp(x0 + dx, 0, nativeW), clamp(y0 + dy, 0, nativeH),
    clamp(x1 + dx, 0, nativeW), clamp(y1 + dy, 0, nativeH),
  ];
}

// Called after the frame has changed, while following.
async function placeFollowBox() {
  const frame = currentFrame();
  if (!follow || !frame) return;
  const bbox = predictNext();
  const res = await post("/api/manual_box", {
    day: folder.day, window: folder.window, file: frame.file,
    bbox, track: follow.track, ...readForm(),
  });
  if (!res) { setStatus("could not place the box on this frame"); return; }
  const row = await res.json();
  manualBoxes.push(row);
  follow.history.push(bbox);
  selected = {kind: "manual", id: row.id};
  renderOverlay();
  renderSidebar();
}

// ---------------------------------------------------------------- dragging
function boxUnder(x, y) {
  const hits = [];
  frameBoxes.forEach(b => {
    const [x0, y0, x1, y1] = b.bbox;
    if (x >= x0 && x <= x1 && y >= y0 && y <= y1) hits.push({kind: "model", track: b.track, area: (x1-x0)*(y1-y0)});
  });
  frameManualBoxes().forEach(m => {
    const [x0, y0, x1, y1] = m.bbox;
    if (x >= x0 && x <= x1 && y >= y0 && y <= y1) hits.push({kind: "manual", id: m.id, area: (x1-x0)*(y1-y0)});
  });
  // the smallest box wins, so a bird inside a big close-up box is still reachable
  hits.sort((a, b) => a.area - b.area);
  return hits[0] || null;
}

svg.addEventListener("mousedown", e => {
  if (pencil || !moveMode || !nativeW) return;
  const [x, y] = toImage(e.clientX, e.clientY);
  const hit = boxUnder(x, y);
  if (!hit) return;
  selected = hit;
  if (hit.kind === "model") selectedTrack = hit.track;
  const sel = selectedBox();
  if (!sel) return;
  e.preventDefault();
  drag = {x, y, bbox: sel.bbox.slice()};
  viewport.classList.add("dragging");
  renderOverlay();
  renderSidebar();
});

addEventListener("mousemove", e => {
  if (!drag || !nativeW) return;
  const [x, y] = toImage(e.clientX, e.clientY);
  const dx = x - drag.x, dy = y - drag.y;
  const [x0, y0, x1, y1] = drag.bbox;
  const moved = [x0 + dx, y0 + dy, x1 + dx, y1 + dy];
  const sel = selectedBox();
  if (sel) { sel.bbox[0] = moved[0]; sel.bbox[1] = moved[1]; sel.bbox[2] = moved[2]; sel.bbox[3] = moved[3]; }
  renderOverlay();
});

addEventListener("mouseup", async () => {
  if (!drag) return;
  const sel = selectedBox();
  drag = null;
  viewport.classList.remove("dragging");
  if (!sel) return;
  await saveBox(sel.bbox.slice());
  // the correction is also the best information about where it is going next
  if (follow && selected && selected.kind === "manual") {
    follow.history[follow.history.length - 1] = sel.bbox.slice();
  }
  renderSidebar();
});

// ---------------------------------------------------------------- pencil
function togglePencil() {
  // Switching modes hands the form to a different owner, so drop focus first:
  // the render below deliberately leaves a focused field alone (so a poll can't
  // overwrite what is being typed), which would otherwise strand the old mode's
  // text in a form that now means something else.
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  pencil = !pencil;
  pencilPt = null;
  $("pencilbtn").classList.toggle("active", pencil);
  $("pencilhint").textContent = pencil
    ? "move to the bird, watch the magnifier, click two opposite corners" : "";
  if (pencil) {
    // A hand-drawn box is usually a DIFFERENT bird from the selected detection,
    // so it must not silently inherit that detection's species/size.
    ["species", "distance", "size"].forEach(f => { $(f).value = ""; $(f).disabled = false; });
  }
  const frame = currentFrame();
  if (frame) img.src = `${wantsNative() ? "/frame/" : "/proxy/"}${currentSite}/${framePath(frame.file)}`;
  renderOverlay();
  renderSidebar();
}

svg.addEventListener("mousemove", e => {
  if (!pencil || !nativeW) return;
  const [x, y] = toImage(e.clientX, e.clientY);
  if (pencilPt) {
    const bbox = [Math.min(pencilPt[0], x), Math.min(pencilPt[1], y),
                  Math.max(pencilPt[0], x), Math.max(pencilPt[1], y)];
    magnify((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2, [{bbox, color: "#ff1a1a"}]);
    $("maghint").textContent = "click corner 2 to finish the box";
  } else {
    magnify(x, y, []);
    $("maghint").textContent = "click corner 1";
  }
});

svg.addEventListener("click", e => {
  if (!pencil || !nativeW || !folder) return;
  const [x, y] = toImage(e.clientX, e.clientY);
  if (!pencilPt) { pencilPt = [x, y]; renderOverlay(); return; }
  const bbox = [Math.min(pencilPt[0], x), Math.min(pencilPt[1], y),
                Math.max(pencilPt[0], x), Math.max(pencilPt[1], y)];
  pencilPt = null;
  if (bbox[2] - bbox[0] < 2 || bbox[3] - bbox[1] < 2) { renderOverlay(); return; }
  magnify((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2, [{bbox, color: "#ff1a1a"}]);
  $("maghint").textContent = "saved — click corner 1 for another, or P to stop";
  const frame = currentFrame();
  post("/api/manual_box", {day: folder.day, window: folder.window, file: frame.file,
                           bbox, ...readForm()})
    .then(res => res ? res.json() : null)
    .then(row => {
      if (!row) { setStatus("could not save that box"); renderOverlay(); return; }
      manualBoxes.push(row);
      renderOverlay();
      renderSidebar();
    });
});

// ---------------------------------------------------------------- controls
$("fitbtn").onclick = () => fitView();
$("zoomoutbtn").onclick = () => zoomBy(0.8);
$("zoominbtn").onclick = () => zoomBy(1.25);
$("pencilbtn").onclick = togglePencil;
$("movebtn").onclick = () => { moveMode = !moveMode; if (moveMode && pencil) togglePencil(); renderTools(); };
$("followbtn").onclick = () => { follow ? stopFollow() : startFollow(); };
$("gonebtn").onclick = () => stopFollow("that bird is out of the scene");
$("deletebtn").onclick = deleteSelected;
$("uncertainbtn").onclick = jumpUncertain;
$("prevbtn").onclick = () => showFrame(frameIdx - 1, true);
$("nextbtn").onclick = () => showFrame(frameIdx + 1, true);
$("scrub").oninput = (e) => showFrame(Number(e.target.value), true);
$("refreshbtn").onclick = () => loadFolders();
$("keepbtn").onclick = () => mark("keep");
$("dropbtn").onclick = () => mark("drop");
$("unsurebtn").onclick = () => mark("unsure");
$("undobtn").onclick = undoLast;
["species", "distance", "size"].forEach(f => {
  $(f).oninput = scheduleMetaSave;
  // Leaving the field commits it now rather than waiting out the debounce,
  // which a click straight onto the next session would otherwise discard.
  $(f).onblur = flushMetaSave;
});
addEventListener("beforeunload", flushMetaSave);

addEventListener("keydown", e => {
  // Escape is handled before the typing guard below: it means "get me out of
  // this mode" even when the cursor is sitting in the annotation form.
  if (e.key === "Escape" && pencil) { togglePencil(); return; }
  if (["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName)) return;
  const step = e.shiftKey ? 10 : 1;
  if (e.key === "ArrowLeft") showFrame(frameIdx - step, true);
  else if (e.key === "ArrowRight") showFrame(frameIdx + step, true);
  else if (e.key === "y" || e.key === "Y") mark("keep");
  else if (e.key === "n" || e.key === "N") mark("drop");
  else if (e.key === " ") { e.preventDefault(); mark("unsure"); }
  else if (e.key === "u" || e.key === "U") jumpUncertain();
  else if (e.key === "p" || e.key === "P") togglePencil();
  else if (e.key === "m" || e.key === "M") $("movebtn").click();
  else if (e.key === "f" || e.key === "F") $("followbtn").click();
  else if (e.key === "x" || e.key === "X") { if (follow) $("gonebtn").click(); }
  else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelected(); }
  else if (e.key === "z" || e.key === "Z") undoLast();
  else if (e.key === "+" || e.key === "=") zoomBy(1.25);
  else if (e.key === "-" || e.key === "_") zoomBy(0.8);
  else if (e.key === "0") fitView();
});

// ---------------------------------------------------------------- polling
// A session being reviewed while live_capture.py is still writing it must grow
// under the reviewer rather than go stale: pull the frame list again, and
// re-fetch the current frame's boxes, which only exist once the model has run.
async function poll() {
  if (!folder || pencil) return;   // never move the ground under a box being drawn
  const id = folder.id;
  const gen = generation;
  const entry = folders.find(f => f.id === id);
  const updated = await fetch(siteUrl(`/api/folder?id=${encodeURIComponent(id)}`))
    .then(r => r.json()).catch(() => null);
  if (!updated || gen !== generation || !folder || folder.id !== id) return;
  const grew = updated.frames.length !== folder.frames.length;
  folder = updated;
  if (grew) { renderTicks(); $("scrub").max = String(folder.frames.length - 1); }
  const frame = currentFrame();
  if (frame) {
    const fetched = await fetchBoxes(frame.file);
    if (gen !== generation || !folder || folder.id !== id) return;
    frameBoxes = fetched;
    renderOverlay();
    renderSidebar();
  }
  if (entry && entry.recording) loadFolders();
}

// ---------------------------------------------------------------- sites
// Switching sites throws away every piece of state scoped to the old one --
// there is no meaningful "carry the open frame over", since a different site
// has entirely different sessions, verdicts and cameras.
async function loadSite(name) {
  if (follow) stopFollow();
  currentSite = name;
  folder = null; frameBoxes = []; selectedTrack = null; selected = null;
  pencil = false; moveMode = false; history = [];
  $("sitepicker").value = name;
  $("title").textContent = `bird review — ${currentSite}/${CFG.model}`;
  const [v, m, mb] = await Promise.all([
    fetch(siteUrl("/api/verdicts")).then(r => r.json()),
    fetch(siteUrl("/api/track_meta")).then(r => r.json()),
    fetch(siteUrl("/api/manual_boxes")).then(r => r.json()),
  ]);
  verdicts = v; trackMeta = m; manualBoxes = mb;
  await loadFolders(false);
}

// ---------------------------------------------------------------- boot
$("sitepicker").innerHTML = (CFG.sites || []).map(s =>
  `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
$("sitepicker").onchange = (e) => loadSite(e.target.value);
loadSite(currentSite);
setInterval(poll, 5000);
