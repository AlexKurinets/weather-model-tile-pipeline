// Pipeline status: polls /api/status and /api/events every few seconds.
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const KIND_CLASS = { failover: "warn", integrity: "warn", missing: "bad", qc_failed: "bad", held: "bad",
  promoted: "good", lease_expired: "warn", job_failed: "bad", ai_run: "good" };

function runLabel(run) {
  if (!run) return "-";
  return run.length > 10 ? `${run.slice(4, 6)}/${run.slice(6, 8)} ${run.slice(8, 10)}:${run.slice(10, 12)}Z`
    : `${run.slice(4, 6)}/${run.slice(6, 8)} ${run.slice(8, 10)}Z`;
}
function mb(b) { return b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(0)} MB`; }
function kpi(k, v, s, cls) {
  return `<div class="kpi ${cls || ""}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="s">${esc(s || "")}</div></div>`;
}

async function load() {
  const [st, ev] = await Promise.all([fetch("/api/status").then((r) => r.json()),
    fetch("/api/events?limit=40").then((r) => r.json())]);
  const t = st.totals;
  $("clock").textContent = st.clock.replace("T", " ").replace(":00Z", "Z");
  const ttm = t.time_to_map_n ? `${t.time_to_map_s_p50} s` : "n/a";
  $("kpis").innerHTML = [
    kpi("Feeds late", t.sources_late, `of ${st.sources.length} feeds`, t.sources_late ? "warn" : ""),
    kpi("Runs held", t.runs_held, `${t.runs_complete} complete, ${t.runs_in_progress} posting`, t.runs_held ? "bad" : ""),
    kpi("Steps ingested", t.steps_done, `${t.steps_missing} missing, ${t.steps_qc_failed} failed QC`),
    kpi("Downloaded", t.fetched_share === null ? "-" : `${Math.round(t.fetched_share * 100)}%`,
      `${mb(t.bytes_fetched)} of ${mb(t.bytes_full)} upstream`),
    kpi("Failovers", t.failovers, `${t.integrity_failures} integrity, ${t.lease_takeovers} lease takeovers`,
      t.failovers ? "warn" : ""),
    kpi("Ingest per step", t.ingest_ms_p50 === null ? "-" : `${(t.ingest_ms_p50 / 1000).toFixed(1)} s`,
      t.ingest_ms_p95 === null ? "" : `p95 ${(t.ingest_ms_p95 / 1000).toFixed(1)} s`),
    kpi("Posted to on the map", ttm, t.time_to_map_n ? `median of ${t.time_to_map_n} steps, p95 ${t.time_to_map_s_p95} s`
      : "advance the clock to measure"),
  ].join("");

  $("feeds").innerHTML = st.sources.map((s) => {
    const notes = [];
    if (s.late) notes.push(`<span class="pill warn">late since ${esc(s.late_since.slice(11, 16))}Z</span>`);
    if (s.held_run) notes.push(`<span class="pill bad">held ${esc(runLabel(s.held_run))}</span> ${esc(s.held_reason)}`);
    const pct = s.newest_total ? Math.round((100 * s.newest_done) / s.newest_total) : 0;
    const age = s.live_age_h === null ? "-" : s.is_obs ? `${Math.round(s.live_age_h * 60)} min` : `${s.live_age_h} h`;
    return `<tr>
      <td class="nowrap"><strong>${esc(s.name)}</strong><br><span class="muted">${esc(s.category)}</span></td>
      <td class="nowrap">${esc(runLabel(s.live_run))}</td>
      <td class="num">${esc(age)}</td>
      <td class="nowrap">${esc(runLabel(s.newest_run))} <span class="muted">${esc((s.newest_state || "").replace("_", " "))}</span></td>
      <td class="nowrap"><span class="bar"><i style="width:${pct}%"></i></span>${s.newest_done}/${s.newest_total}</td>
      <td class="num">${s.fetched_share === null ? "-" : Math.round(s.fetched_share * 100) + "%"}</td>
      <td class="num">${s.from_fallback}</td>
      <td class="num">${s.ingest_ms_p50 === null ? "-" : (s.ingest_ms_p50 / 1000).toFixed(1) + " s"}</td>
      <td>${notes.join("<br>")}</td></tr>`;
  }).join("");

  $("events").innerHTML = ev.map((e) => `<tr>
      <td class="nowrap">${esc((e.at || "").slice(5, 16).replace("T", " "))}</td>
      <td><span class="pill ${KIND_CLASS[e.kind] || ""}">${esc(e.kind.replace("_", " "))}</span></td>
      <td class="nowrap">${esc(e.source || "")}</td><td class="nowrap">${esc(runLabel(e.run))}</td>
      <td>${esc(e.detail)}</td></tr>`).join("");
}

load();
setInterval(() => load().catch(console.warn), 5000);
