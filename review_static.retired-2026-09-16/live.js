"use strict";

const CFG = window.APP_CONFIG || {default_site: "site", sites: []};
const SITES = CFG.sites || [];
// Camera -> its site's name, so a click can name whichever camera it is
// without the caller having to remember which site listed it.
const CAMERA_SITE = {};
SITES.forEach(s => (s.cameras || []).forEach(name => { CAMERA_SITE[name] = s.name; }));
const ALL_CAMERAS = Object.keys(CAMERA_SITE);
const MULTI_SITE = SITES.length > 1;

const shown = new Set();       // cameras whose stream is pulled into the grid
const recording = new Set();   // cameras currently saving to disk (from polling)

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

$("title").textContent = MULTI_SITE ? "live detections" : `live detections — ${CFG.default_site}`;

let statusTimer = null;
function setStatus(text) {
  const el = $("livestatus");
  if (!el) return;
  el.textContent = text;
  clearTimeout(statusTimer);
  if (text) statusTimer = setTimeout(() => { el.textContent = ""; }, 4000);
}

function renderCameras() {
  if (!ALL_CAMERAS.length) {
    $("camlist").innerHTML = `<div class=empty>No cameras in any configured site.</div>`;
    return;
  }
  // Name on its own line: a camera name and a status column fighting for one
  // line's width is what made two different cameras (modul2-47/modul2-52)
  // truncate to the same ellipsis -- the name now always gets the full row.
  $("camlist").innerHTML = SITES.map(site => `
    <div class=grouphead>${esc(site.name)} &middot; ${(site.cameras || []).length}</div>
    ${(site.cameras || []).map(name => `
    <div class="folder cam" data-name="${esc(name)}">
      <label class=camrow>
        <input type=checkbox value="${esc(name)}" ${shown.has(name) ? "checked" : ""}>
        <span class="dot" id="dot-${esc(name)}"></span>
        <span class="camname">${esc(name)}</span>
      </label>
      <div class="camstatus">
        <span class="age" id="age-${esc(name)}">?</span>
        <button class="recbtn" id="rec-${esc(name)}" data-name="${esc(name)}"
                title="save this camera's detections to disk and send them to Review">
          &#9679; record
        </button>
      </div>
    </div>`).join("")}`).join("");

  $("camlist").querySelectorAll("input[type=checkbox]").forEach(box => {
    box.onchange = () => {
      if (box.checked) shown.add(box.value); else shown.delete(box.value);
      renderGrid();
    };
  });
  $("camlist").querySelectorAll(".recbtn").forEach(btn => {
    btn.onclick = () => toggleRecording(btn.dataset.name);
  });
  syncRecordButtons();
}

// Only a checked camera gets an <img>. Removing the element is what actually
// stops the stream -- a hidden one would keep pulling frames in the background,
// which is exactly the bandwidth this page is meant to avoid spending.
function renderGrid() {
  const grid = $("grid");
  grid.querySelectorAll("figure").forEach(fig => {
    if (!shown.has(fig.dataset.name)) fig.remove();
  });
  for (const name of shown) {
    if (grid.querySelector(`figure[data-name="${CSS.escape(name)}"]`)) continue;
    const fig = document.createElement("figure");
    fig.dataset.name = name;
    const label = MULTI_SITE ? `${esc(CAMERA_SITE[name])} / ${esc(name)}` : esc(name);
    fig.innerHTML =
      `<figcaption><span class="dot" id="figdot-${esc(name)}"></span>${label}
        <span class="figrec" id="figrec-${esc(name)}" hidden>&#9679; REC</span></figcaption>` +
      `<img alt="live view of ${esc(name)}" src="/stream/${encodeURIComponent(name)}.mjpg">`;
    grid.appendChild(fig);
  }
  const empty = grid.querySelector(".empty");
  if (shown.size && empty) empty.remove();
  if (!shown.size && !empty) {
    grid.innerHTML = `<div class=empty>Pick a camera on the left.</div>`;
  }
  $("allbtn").textContent = shown.size === ALL_CAMERAS.length && ALL_CAMERAS.length ? "none" : "all";
}

// The button reflects what the server confirmed, not what was clicked -- a
// request that fails (server restarted, camera name typo) must not leave the
// button claiming a state the marker file was never actually set to.
async function toggleRecording(name) {
  const goal = !recording.has(name);
  const res = await fetch("/api/recording", {
    method: "POST", body: JSON.stringify({name, enabled: goal}),
  }).catch(() => null);
  if (!res || !res.ok) {
    const body = res ? await res.json().catch(() => null) : null;
    setStatus(body && body.error ? body.error
      : `could not ${goal ? "start" : "stop"} recording ${name}`);
    return;
  }
  if (goal) recording.add(name); else recording.delete(name);
  syncRecordButtons();
  setStatus(goal
    ? `recording ${name} — its sessions will appear in Review`
    : `stopped recording ${name}`);
}

function syncRecordButtons() {
  ALL_CAMERAS.forEach(name => {
    const on = recording.has(name);
    const btn = $("rec-" + name);
    if (btn) {
      btn.classList.toggle("active", on);
      btn.textContent = on ? "● recording" : "● record";
    }
    const fig = $("figrec-" + name);
    if (fig) fig.hidden = !on;
  });
}

$("allbtn").onclick = () => {
  if (shown.size === ALL_CAMERAS.length) shown.clear();
  else ALL_CAMERAS.forEach(n => shown.add(n));
  renderCameras();
  renderGrid();
};

// Picking a camera starts its process, and a finetuned YOLO model takes a few
// seconds to load onto the GPU before the first preview frame exists -- shown
// as "starting..." rather than "no signal", which reads as broken.
const warnedAbout = new Set();   // don't repeat the same camera's error every poll

async function poll() {
  const rows = await fetch("/api/cameras").then(r => r.json()).catch(() => []);
  let recordingChanged = false;
  rows.forEach(r => {
    const age = $("age-" + r.name);
    if (age) {
      age.classList.toggle("error", !!r.error);
      // Whether a process is running comes first, before anything about a
      // preview file: a camera that ran successfully an hour ago and was
      // stopped since still has that old latest.jpg sitting on disk (and,
      // for one captured before this build, maybe no status.json at all).
      // Checking mtime/stable ahead of `running` read that leftover file as
      // "still connecting" forever, for a camera nothing was touching.
      //
      // Once it IS running: no frame yet is "starting...", however long that
      // takes (loading the model, opening the stream). A frame existing is
      // not the same as the link being good -- a connection that just
      // dropped and reconnected can still hand back one frame before
      // dropping again -- so "live" additionally waits for the CURRENT
      // connection to have held for a few seconds, shown as "connecting..."
      // in between even though a frame is already arriving.
      age.textContent = r.error ? r.error
        : !r.running ? "no signal"
        : r.mtime === null ? "starting…"
        : !r.stable ? "connecting…"
        : r.live ? "live"
        : `${r.age}s ago`;
      // Phase 0 instrumentation: `age` above is an mtime-based proxy (already
      // includes decode/inference/encode time); `capture_age`, when present,
      // is the true capture-to-now clock. Shown as a tooltip, not a second
      // visible number, to keep this legacy page's layout unchanged.
      age.title = r.capture_age === null || r.capture_age === undefined ? ""
        : `capture age: ${r.capture_age}s`;
    }
    [$("dot-" + r.name), $("figdot-" + r.name)].forEach(dot => {
      if (dot) dot.classList.toggle("live", r.live && r.stable);
    });
    if (r.error && !warnedAbout.has(r.name)) {
      warnedAbout.add(r.name);
      setStatus(`${r.name}: ${r.error}`);
    }
    const was = recording.has(r.name);
    if (r.recording !== was) {
      recordingChanged = true;
      if (r.recording) recording.add(r.name); else recording.delete(r.name);
    }
  });
  if (recordingChanged) syncRecordButtons();
}

renderCameras();
renderGrid();
poll();
setInterval(poll, 3000);
