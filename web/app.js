// Model map: reads the manifests, stacks one raster layer per forecast hour (or radar/satellite
// frame) and animates by switching opacity, so playback never waits on tile requests.
"use strict";

const REFRESH_MS = 20000;
const MAX_FRAMES = 40;
const CATEGORY_LABELS = { global: "Global", ai: "AI models", regional: "Regional", ensemble: "Ensembles",
  observed: "Observed" };

const state = { index: null, latest: null, manifest: null, source: null, run: "live", field: null, frames: [],
  frame: 0, playing: null, opacity: 0.85, layerIds: [] };
const $ = (id) => document.getElementById(id);

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

// ------------------------------------------------------------------ map
const map = new maplibregl.Map({
  container: "map",
  center: [-96, 38.5],
  zoom: 3.4,
  minZoom: 1,
  maxZoom: 9,
  attributionControl: { compact: true, customAttribution: "Coastlines and borders: Natural Earth" },
  style: {
    version: 8,
    sources: {
      land: { type: "geojson", data: "geo/ne_110m_land.geojson" },
      lakes: { type: "geojson", data: "geo/ne_50m_lakes.geojson" },
      coast: { type: "geojson", data: "geo/ne_50m_coastline.geojson" },
      borders: { type: "geojson", data: "geo/ne_50m_admin_0_boundary_lines_land.geojson" },
      states: { type: "geojson", data: "geo/ne_50m_admin_1_states_provinces_lines.geojson" },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#c9d6e2" } },
      { id: "land", type: "fill", source: "land", paint: { "fill-color": "#eef0ea" } },
      { id: "lakes", type: "fill", source: "lakes", paint: { "fill-color": "#c9d6e2" } },
      { id: "wx-anchor", type: "background", paint: { "background-opacity": 0 } },
      { id: "states", type: "line", source: "states", paint: { "line-color": "#3a3f47", "line-width": 0.5,
        "line-opacity": 0.6 } },
      { id: "borders", type: "line", source: "borders", paint: { "line-color": "#22262c", "line-width": 0.9 } },
      { id: "coast", type: "line", source: "coast", paint: { "line-color": "#22262c", "line-width": 0.8 } },
    ],
  },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

// ------------------------------------------------------------------ helpers
function runTime(run) {
  const y = +run.slice(0, 4), mo = +run.slice(4, 6) - 1, d = +run.slice(6, 8), h = +run.slice(8, 10);
  const mi = run.length > 10 ? +run.slice(10, 12) : 0;
  return new Date(Date.UTC(y, mo, d, h, mi));
}
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtTime(t, withMinutes) {
  const hh = String(t.getUTCHours()).padStart(2, "0");
  const mm = String(t.getUTCMinutes()).padStart(2, "0");
  return `${DAYS[t.getUTCDay()]} ${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]} ${hh}${withMinutes ? ":" + mm : ""}Z`;
}
function sourceInfo() { return state.index.sources.find((s) => s.id === state.source); }
function resolvedRun() {
  if (state.run !== "live") return state.run;
  return state.latest && state.latest.live_run;
}

// ------------------------------------------------------------------ controls
function fillModels() {
  const sel = $("model");
  sel.innerHTML = "";
  const groups = {};
  for (const s of state.index.sources) (groups[s.category] = groups[s.category] || []).push(s);
  for (const [cat, list] of Object.entries(groups)) {
    const og = document.createElement("optgroup");
    og.label = CATEGORY_LABELS[cat] || cat;
    for (const s of list) {
      const o = document.createElement("option");
      o.value = s.id;
      o.textContent = s.name;
      og.appendChild(o);
    }
    sel.appendChild(og);
  }
  sel.value = state.source;
}

function runLabel(r, live) {
  const t = runTime(r.run);
  const base = state.latest.is_obs ? fmtTime(t, true) : `${String(t.getUTCHours()).padStart(2, "0")}Z ${DAYS[t.getUTCDay()]} ${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]}`;
  if (r.state === "held") return `${base}, held`;
  if (r.state === "in_progress") return `${base}, posting ${r.steps_done}/${r.steps_expected}`;
  return r.run === live ? `${base}, default` : base;
}

function fillRuns() {
  const sel = $("run");
  sel.innerHTML = "";
  const live = state.latest.live_run;
  const o = document.createElement("option");
  o.value = "live";
  o.textContent = state.latest.is_obs ? "Latest frames (loop)" : (live ? `Default run (${live.slice(8, 10)}Z)` : "No complete run yet");
  sel.appendChild(o);
  if (!state.latest.is_obs) {
    for (const r of [...state.latest.runs].reverse()) {
      const op = document.createElement("option");
      op.value = r.run;
      op.textContent = runLabel(r, live);
      sel.appendChild(op);
    }
  }
  sel.value = [...sel.options].some((x) => x.value === state.run) ? state.run : "live";
  state.run = sel.value;
}

function showRunNote() {
  const note = $("run-note");
  note.className = "note";
  note.hidden = true;
  if (state.latest.is_obs) {
    const frames = state.latest.runs.filter((r) => r.state === "complete");
    const last = frames[frames.length - 1];
    if (last) {
      note.hidden = false;
      note.textContent = `${frames.length} frames, newest ${fmtTime(runTime(last.run), true)}`;
    }
    return;
  }
  const run = resolvedRun();
  const r = state.latest.runs.find((x) => x.run === run);
  const held = state.latest.runs.filter((x) => x.state === "held");
  if (r && r.state === "in_progress") {
    note.hidden = false;
    note.classList.add("progress");
    note.textContent = `Still posting: ${r.steps_done} of ${r.steps_expected} steps in. Not the default until complete.`;
  } else if (r && r.state === "held") {
    note.hidden = false;
    note.classList.add("held");
    note.textContent = `Held: ${r.held_reason}`;
  } else if (state.run === "live" && held.length) {
    const h = held[held.length - 1];
    if (runTime(h.run) > runTime(run)) {
      note.hidden = false;
      note.classList.add("held");
      note.textContent = `${h.run.slice(8, 10)}Z run held (${h.held_reason}), so this map stays on ${run.slice(8, 10)}Z.`;
    }
  }
}

function fillFields() {
  const box = $("fields");
  box.innerHTML = "";
  const info = sourceInfo();
  const have = state.latest.is_obs ? Object.keys(info.fields) : Object.keys((state.manifest && state.manifest.fields) || {});
  const names = Object.keys(info.fields).filter((f) => have.includes(f));
  if (!names.includes(state.field)) state.field = names[0] || null;
  for (const f of names) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(f === state.field));
    b.textContent = info.fields[f].label;
    b.onclick = () => { state.field = f; fillFields(); buildFrames(); };
    box.appendChild(b);
  }
}

function drawLegend() {
  const info = sourceInfo();
  const f = info.fields[state.field];
  if (!f) return;
  $("legend-label").textContent = f.label;
  $("legend-units").textContent = f.units;
  const stops = f.legend.stops;
  const lo = stops[0][0], hi = stops[stops.length - 1][0];
  const pct = (v) => ((v - lo) / (hi - lo)) * 100;
  let grad;
  if (f.legend.steps) {
    const parts = [];
    stops.forEach((s, i) => {
      const end = i + 1 < stops.length ? pct(stops[i + 1][0]) : 100;
      parts.push(`${s[1]} ${pct(s[0])}%`, `${s[1]} ${end}%`);
    });
    grad = parts.join(", ");
  } else {
    grad = stops.map((s) => `${s[1]} ${pct(s[0])}%`).join(", ");
  }
  $("legend").style.background = `linear-gradient(90deg, ${grad})`;
  const ticks = $("legend-ticks");
  ticks.innerHTML = "";
  // Label stops at least 14% of the bar apart, always including both ends.
  const picked = [];
  stops.forEach((s) => {
    if (!picked.length || pct(s[0]) - pct(picked[picked.length - 1][0]) >= 14) picked.push(s);
  });
  const last = stops[stops.length - 1];
  if (picked[picked.length - 1] !== last) {
    if (pct(last[0]) - pct(picked[picked.length - 1][0]) < 14) picked.pop();
    picked.push(last);
  }
  picked.forEach((s) => {
    const sp = document.createElement("span");
    sp.style.left = `${Math.min(96, Math.max(4, pct(s[0])))}%`;
    sp.textContent = s[0];
    ticks.appendChild(sp);
  });
}

// ------------------------------------------------------------------ frames and layers
function clearLayers() {
  for (const id of state.layerIds) {
    if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource(id)) map.removeSource(id);
  }
  state.layerIds = [];
}

function buildFrames() {
  clearLayers();
  drawLegend();
  const src = state.source, field = state.field;
  state.frames = [];
  if (!field) return updateTime();
  if (state.latest.is_obs) {
    const frames = state.latest.runs.filter((r) => r.state === "complete").slice(-MAX_FRAMES);
    state.frames = frames.map((r) => ({ run: r.run, fhour: 0, t: runTime(r.run) }));
  } else if (state.manifest) {
    const run = state.manifest.run;
    const hours = (state.manifest.fields[field] || {}).fhours || [];
    state.frames = hours.slice(0, MAX_FRAMES).map((h) => ({ run, fhour: h, t: new Date(runTime(run).getTime() + h * 3600e3) }));
  }
  state.layerIds = state.frames.map((_, i) => `wx-${i}`);
  const slider = $("hour");
  slider.max = Math.max(0, state.frames.length - 1);
  if (state.latest.is_obs) state.frame = state.frames.length - 1;
  state.frame = Math.min(state.frame, state.frames.length - 1);
  slider.value = Math.max(0, state.frame);
  showFrame(Math.max(0, state.frame));
  preloadFrames();
}

// The visible frame's layer is added first; the rest follow once the map is idle, so the frame on
// screen never waits behind the tiles of frames that are not shown yet.
function ensureLayer(i) {
  const fr = state.frames[i];
  const id = `wx-${i}`;
  if (!fr || map.getLayer(id)) return;
  map.addSource(id, { type: "raster", tileSize: 256, maxzoom: 8,
    tiles: [`${location.origin}/tiles/${state.source}/${fr.run}/${state.field}/${fr.fhour}/{z}/{x}/{y}.png`] });
  map.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": 0, "raster-fade-duration": 0,
    "raster-resampling": "linear" } }, "wx-anchor");
}

let preloadToken = 0;
function preloadFrames() {
  const token = ++preloadToken;
  map.once("idle", () => {
    if (token !== preloadToken) return;
    state.frames.forEach((_, i) => ensureLayer(i));
    showFrame(state.frame);
  });
}

function showFrame(i) {
  state.frame = i;
  ensureLayer(i);
  state.layerIds.forEach((id, j) => {
    if (map.getLayer(id)) map.setPaintProperty(id, "raster-opacity", j === i ? state.opacity : 0);
  });
  updateTime();
  writeHash();
}

function updateTime() {
  const fr = state.frames[state.frame];
  if (!fr) { $("valid").textContent = "No data for this field yet"; return; }
  $("valid").textContent = state.latest.is_obs
    ? `${fmtTime(fr.t, true)}  (frame ${state.frame + 1} of ${state.frames.length})`
    : `f${String(fr.fhour).padStart(3, "0")}  Valid ${fmtTime(fr.t)}`;
}

// ------------------------------------------------------------------ loading
async function loadSource(keepHour) {
  state.latest = await getJSON(`/manifests/${state.source}/latest.json`);
  fillRuns();
  const run = resolvedRun();
  state.manifest = null;
  if (!state.latest.is_obs && run) state.manifest = await getJSON(`/manifests/${state.source}/${run}.json`);
  if (!keepHour) state.frame = 0;
  showRunNote();
  fillFields();
  buildFrames();
}

async function refresh() {
  try {
    const before = state.latest && state.latest.live_run;
    const latest = await getJSON(`/manifests/${state.source}/latest.json`);
    const changed = latest.live_run !== before || JSON.stringify(latest.runs) !== JSON.stringify(state.latest.runs);
    if (changed) {
      state.latest = latest;
      if (state.latest.is_obs || state.run === "live") { await loadSource(true); }
      else { fillRuns(); showRunNote(); }
    }
  } catch (e) { console.warn(e); }
}

// ------------------------------------------------------------------ hover readout
let hoverTimer = null;
map.on("mousemove", (e) => {
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(async () => {
    const fr = state.frames[state.frame];
    if (!fr || !state.field) return;
    const { lng, lat } = e.lngLat.wrap();
    if (Math.abs(lat) > 85) return;
    const q = new URLSearchParams({ source: state.source, run: fr.run, field: state.field, fhour: fr.fhour,
      lon: lng.toFixed(4), lat: lat.toFixed(4) });
    try {
      const r = await getJSON(`/api/point?${q}`);
      $("readout").textContent = r.value === null ? "no data" : `${r.value} ${r.units}`;
    } catch (err) { $("readout").textContent = "no data"; }
  }, 120);
});

// ------------------------------------------------------------------ URL state
function writeHash() {
  const fr = state.frames[state.frame];
  const parts = [state.source, state.run, state.field || "", fr ? (state.latest.is_obs ? "last" : fr.fhour) : ""];
  history.replaceState(null, "", `#${parts.join("/")}`);
}
function readHash() {
  const [src, run, field, hour] = decodeURIComponent(location.hash.slice(1)).split("/");
  return { src, run, field, hour };
}

// ------------------------------------------------------------------ wiring
$("model").onchange = async (e) => { state.source = e.target.value; state.run = "live"; await loadSource(false); };
$("run").onchange = async (e) => { state.run = e.target.value; await loadSource(true); };
$("hour").oninput = (e) => showFrame(+e.target.value);
$("opacity").oninput = (e) => { state.opacity = +e.target.value; showFrame(state.frame); };
$("play").onclick = () => {
  if (state.playing) {
    clearInterval(state.playing); state.playing = null; $("play").innerHTML = "&#9654;"; return;
  }
  $("play").innerHTML = "&#10074;&#10074;";
  state.playing = setInterval(() => {
    if (!state.frames.length) return;
    const n = (state.frame + 1) % state.frames.length;
    $("hour").value = n;
    showFrame(n);
  }, state.latest && state.latest.is_obs ? 450 : 650);
};

map.on("load", async () => {
  state.index = await getJSON("/manifests/index.json");
  const h = readHash();
  state.source = state.index.sources.some((s) => s.id === h.src) ? h.src : "gfs";
  state.run = h.run || "live";
  state.field = h.field || null;
  fillModels();
  await loadSource(true);
  if (h.hour && h.hour !== "last") {
    const i = state.frames.findIndex((f) => String(f.fhour) === h.hour);
    if (i >= 0) { $("hour").value = i; showFrame(i); }
  }
  setInterval(refresh, REFRESH_MS);
});
