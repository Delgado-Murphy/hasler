// The makers' pages: /dashboard (one verdict line, four panels, the table) and
// /dashboard/radio/<id> (one radio, its last 60 days). Server-rendered HTML, no script, no
// library; the key page's palette (daemon/haslerd/web/static/index.html :root) and the quiet
// Delgado & Murphy mark. Red is for faults only: a fault or failed check, a rollback, a full or
// read-only card, a broken YouTube Music helper, heat at the fault line. Every coloured state
// also carries a word. Times are Europe/Madrid, the UTC time in the title.

import {
  normalize, lookReasons, variantLabel, faultWord, checkWord, rollbackWords, platformOf, piShort,
  RS_WORDS, AMBER_DAYS, GREY_DAYS, HOT_WARN_C, HOT_C,
} from "./model.js";
import { releaseHealth, versionsByChannel, currentByChannel, behind, ytdlpFleet, platforms } from "./fleet.js";

export const TZ = "Europe/Madrid";
const OPEN_REQUEST_AMBER_DAYS = 2;

export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, one, many = one + "s") => `${n} ${n === 1 ? one : many}`;

// --- time -----------------------------------------------------------------------------------------

const madridDay = t => new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" }).format(t);
const madridHM = t => new Intl.DateTimeFormat("en-GB", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false }).format(t);
export const utcTitle = t => new Date(t).toISOString().slice(0, 16).replace("T", " ") + " UTC";

/** "today 06:12", "yesterday", "4 days ago", "not seen" - in Madrid's calendar. */
export function seenWords(seenMs, now) {
  if (!Number.isFinite(seenMs)) return "not seen";
  const a = Date.parse(madridDay(seenMs) + "T00:00:00Z"), b = Date.parse(madridDay(now) + "T00:00:00Z");
  const d = Math.round((b - a) / 86400000);
  if (d <= 0) return `today ${madridHM(seenMs)}`;
  if (d === 1) return `yesterday ${madridHM(seenMs)}`;
  return `${d} days ago`;
}
export const seenToday = (seenMs, now) => Number.isFinite(seenMs) && madridDay(seenMs) === madridDay(now);

// --- one row --------------------------------------------------------------------------------------

function sizeWords(mb) {
  if (typeof mb !== "number") return null;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB free` : `${mb} MB free`;
}

/** Everything the table, the cards and /v1/radios say about one radio. */
export function rowOf(id, rec, now, { issues = [], current = {} } = {}) {
  const n = normalize(rec && rec.payload);
  const seen = rec && rec.last_seen ? Date.parse(rec.last_seen) : NaN;
  const days = Number.isFinite(seen) ? (now - seen) / 86400000 : Infinity;
  const look = lookReasons(n, days);
  const rel = n.release || {};
  const faults = [...new Set([...n.faults.now, ...n.faults.day])];
  const temp = n.temp_c.max_24h ?? n.temp_c.now;
  const requests = (issues || []).filter(i => i && Number.isInteger(i.number));
  const live = requests.filter(i => (i.state || "open") !== "done");
  return {
    id, short: n.short_id || String(id).slice(0, 4), variant: n.variant || "", model: variantLabel(n.variant),
    version: n.version || "", channel: n.channel || "", mode: n.update_mode || "", check: n.check || "", schema: n.schema,
    release: rel.state ? { state: rel.state, version: rel.version || null, from: rel.from || null, reason: rel.reason || null,
      words: rel.state === "rolled_back" ? rollbackWords(rel.reason) : "" } : null,
    behind: behind(n.version, current[n.channel]),
    seen: Number.isFinite(seen) ? new Date(seen).toISOString() : null, seenMs: seen, days,
    healthOk: n.health.ok, failed: n.health.failed, faults, faultsNow: n.faults.now,
    stalls: n.player.stalls_24h, restarts: n.player.restarts_24h, crashes: n.player.daemon_restarts_24h,
    ytdlp: n.ytdlp ? (n.ytdlp.working === true ? "works" : n.ytdlp.working === false ? "broken" : "checking") : "—",
    ytdlpVersion: n.ytdlp ? n.ytdlp.version : null,
    cuts: n.power.cuts_30d, lowVoltage: n.power.undervolt_24h === true, lowVoltageNow: n.power.undervolt_now === true,
    lastOff: n.power.last_off, temp, tempNow: n.temp_c.now,
    card: n.storage.state, freeMb: n.storage.free_mb, totalGb: n.storage.total_gb,
    uptime: n.uptime_d, platform: platformOf(n), hw: n.hw, os: n.os, osUpdates: n.os_updates,
    requests, openRequests: live.length,
    look, needsLook: look.length > 0,
  };
}

export function sortRows(rows) {
  // needs a look first; then the longest unseen; then by short id
  return rows.slice().sort((a, b) => (b.needsLook - a.needsLook) || (b.days - a.days) || a.short.localeCompare(b.short));
}

/** Two radios with the same 4 hex: add two characters of the box id to both. */
export function disambiguate(rows) {
  const count = {};
  for (const r of rows) count[r.short] = (count[r.short] || 0) + 1;
  for (const r of rows) r.label = count[r.short] > 1 ? `${r.short}·${String(r.id).slice(-2)}` : r.short;
  return rows;
}

// --- cells ----------------------------------------------------------------------------------------

const w = (cls, text) => `<span class="${cls}">${esc(text)}</span>`;
const chip = ch => (ch ? `<i class="chip">${esc(ch === "bench" ? "Bench" : ch === "stable" ? "Stable" : ch)}</i>` : "");

function versionCell(r) {
  const bits = [`<b class="num">${esc(r.version || "?")}</b>`, chip(r.channel)];
  const rel = r.release;
  if (rel && rel.state === "rolled_back") bits.push(w("fault", `went back to ${rel.from || "?"}`));
  else if (rel && (rel.state === "probation" || rel.state === "trial") && (!rel.version || rel.version === r.version)) bits.push(w("warn", rel.state));
  if (r.behind) bits.push(w("warn", "behind"));
  if (r.mode && r.mode !== "auto") bits.push(w("mut", r.mode === "off" ? "manual (off)" : "asks first"));
  return bits.join(" ");
}

function healthCell(r) {
  const words = [...r.failed.map(checkWord), ...r.faults.map(faultWord)];
  if (r.healthOk === false && !r.failed.length) words.unshift("self-check failed");
  if (!words.length) return r.healthOk === true ? "OK" : w("mut", "?");
  const cleared = r.faults.length && !r.faultsNow.length && !r.failed.length;
  return w("fault", [...new Set(words)].join(", ")) + (cleared ? ` <small>earlier today</small>` : "");
}

function playerCell(r) {
  if (r.stalls === null && r.restarts === null) return w("mut", "—");
  const s = r.stalls ?? 0, re = r.restarts ?? 0;
  let out = `<span class="num">${s} · ${re}</span>`;
  if (r.crashes) out += ` · ${w("warn", plural(r.crashes, "crash", "crashes"))}`;
  return s >= 3 || re >= 1 ? `<span class="warn">${out}</span>` : out;
}

function ytdlpCell(r) {
  if (r.ytdlp === "broken") return w("fault", "broken");
  if (r.ytdlp === "works") return "works";
  return w("mut", r.ytdlp);
}

function powerCell(r) {
  const bits = [];
  if (r.cuts !== null && r.cuts !== undefined) bits.push(`<span class="num">${esc(plural(r.cuts, "cut"))}</span>`);
  if (r.lowVoltage) bits.push(w("warn", r.lowVoltageNow ? "low voltage now" : "low voltage"));
  const off = { "power-cut": "last off: cut", clean: "last off: clean", restart: "last: restart" }[r.lastOff];
  if (off) bits.push(`<small>${esc(off)}</small>`);
  const html = bits.join(" ") || w("mut", "—");
  return (r.cuts ?? 0) >= 3 && !r.lowVoltage ? `<span class="warn">${html}</span>` : html;
}

function tempCell(r) {
  if (r.temp === null || r.temp === undefined) return w("mut", "—");
  const cls = r.temp >= HOT_C ? "fault" : r.temp >= HOT_WARN_C ? "warn" : "";
  const t = `<span class="num${cls ? " " + cls : ""}" title="now ${esc(r.tempNow ?? "?")}°">${esc(r.temp)}°</span>`;
  return r.temp >= HOT_C ? t + " " + w("fault", "hot") : t;
}

function cardCell(r) {
  if (r.card === "full") return w("fault", "full");
  if (r.card === "read-only") return w("fault", "read-only");
  const free = sizeWords(r.freeMb);
  if (r.card === "low") return w("warn", "low") + (free ? ` <small>${esc(free)}</small>` : "");
  return free ? `<span class="num">${esc(free)}</span>` : w("mut", "—");
}

function requestsCell(r, repo, now) {
  const live = r.requests.filter(i => (i.state || "open") !== "done").slice(0, 2);
  if (!live.length) return "";
  return live.map(i => {
    const st = i.state || "open";
    const old = st === "open" && Number.isFinite(Date.parse(i.at)) && now - Date.parse(i.at) > OPEN_REQUEST_AMBER_DAYS * 86400000;
    const text = `#${i.number} ${st}`;
    const inner = repo ? `<a href="https://github.com/${esc(repo)}/issues/${esc(i.number)}">${esc(text)}</a>` : esc(text);
    return old ? `<span class="warn">${inner}</span>` : inner;
  }).join(" ");
}

function seenCell(r, now) {
  const cls = r.days > GREY_DAYS ? "mut" : r.days > AMBER_DAYS ? "warn" : "";
  const title = Number.isFinite(r.seenMs) ? utcTitle(r.seenMs) : "never";
  return `<span class="${cls}" title="${esc(title)}">${esc(seenWords(r.seenMs, now))}</span>`;
}

// --- shared page parts ----------------------------------------------------------------------------

const CSS = `
:root{--ground:#17100B;--veneer:#24170F;--sheet:#1F140D;--line:#3A2A1C;--line2:#5A4128;--cream:#EDE3CC;--brass:#C9A15A;
--amber:#FFB238;--red:#EC5E42;--muted:#A08A68;--text2:#B9A584;--title:#FFE9C2;--faint:#7A6A52;
--serif:"Fraunces",Georgia,"Times New Roman",serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color-scheme:dark}
*{box-sizing:border-box}
html,body{background:var(--ground)}
body{margin:0;padding:28px 24px 40px;color:var(--cream);font:14px/1.45 var(--sans);max-width:1480px;margin:0 auto}
a{color:var(--brass);text-underline-offset:3px}
h1{font:600 26px/1.15 var(--serif);color:var(--title);margin:0 0 6px;letter-spacing:.01em}
h2{font:600 15px/1.2 var(--serif);color:var(--title);margin:0 0 10px}
.verdict{font-size:19px;margin:2px 0 4px;color:var(--text2)}.verdict b{color:var(--cream);font-weight:600}
.stamp,.key{color:var(--faint);font-size:12px;margin:0}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:13px}
.warn{color:var(--amber);font-style:normal}.fault{color:var(--red);font-style:normal}.mut{color:var(--faint)}
small{color:var(--muted);font-size:11px}
.chip{font-style:normal;font-size:11px;border:1px solid var(--line2);border-radius:3px;padding:0 5px;color:var(--text2);white-space:nowrap}
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px;margin:22px 0 26px}
.panels article{background:var(--sheet);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.panels p{margin:0 0 6px}.panels dl{margin:0}.panels dt{color:var(--muted);font-size:12px;margin-top:6px}.panels dd{margin:2px 0 0}
.panels ul{list-style:none;padding:0;margin:0}.panels li{display:flex;justify-content:space-between;gap:12px;padding:3px 0;border-bottom:1px solid var(--line)}
.panels li:last-child{border-bottom:0}
.mini{border-collapse:collapse;width:100%;font-size:12px}
.mini th,.mini td{text-align:right;padding:3px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.mini th:first-child,.mini td:first-child{text-align:left}.mini th{color:var(--muted);font-weight:500}
.spark{vertical-align:middle}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}
@media (min-width:1100px){.panels article.span2{grid-column:span 2}}
.fleet{border-collapse:collapse;width:100%}
.fleet th,.fleet td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
.fleet th{color:var(--muted);font-weight:500;font-size:12px;white-space:nowrap}
.fleet td small{display:block}
.fleet tr.grey td,.fleet tr.grey td *{color:var(--faint)}
.fleet tr.look .id{color:var(--amber)}
.id{font:600 15px var(--mono);color:var(--cream)}
.mark{margin-top:36px;padding-top:14px;border-top:1px solid #2A2622;text-align:right;font:600 11px var(--sans);letter-spacing:.28em;color:#7D766B}
.mark em{font:italic 11px Georgia,serif;letter-spacing:0;color:#5A544B}
details{margin-top:8px}summary{cursor:pointer;color:var(--brass)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--well,#150E09);border:1px solid var(--line);border-radius:6px;padding:12px;font:12px/1.5 var(--mono);color:var(--text2)}
.grid{border-collapse:collapse;font-size:12px;width:100%}
.grid th,.grid td{padding:3px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
.grid th{color:var(--muted);font-weight:500}
.grid tr.vchange td{border-bottom:1px solid var(--cream)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--cream)}
.dot.warn{background:var(--amber)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin:14px 0 22px}
.cards div{background:var(--sheet);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.cards b.k{display:block;color:var(--muted);font-weight:500;font-size:11px;margin-bottom:2px}.cards small{display:block}
button.forget{background:none;border:1px solid var(--line2);color:var(--text2);border-radius:6px;padding:6px 12px;font:13px var(--sans);cursor:pointer}
@media (max-width:760px){
  body{padding:20px 16px 32px}
  h1{font-size:22px}.verdict{font-size:17px}
  .fleet thead,.fleet .wide{display:none}
  .fleet,.fleet tbody{display:block}
  .fleet tr{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;padding:12px 0;border-bottom:1px solid var(--line)}
  .fleet td{border:0;padding:0;min-width:0}
  .fleet td.radio{grid-column:1/-1}
  .fleet td::before{content:attr(data-l);display:block;color:var(--faint);font-size:11px}
  .fleet td.radio::before{display:none}
  .grid .hide-s{display:none}
}`;

function page(title, inner) {
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>${esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600&display=swap" rel="stylesheet">
<style>${CSS}</style></head><body>
${inner}
<footer class="mark">DELGADO <em>&amp;</em> MURPHY</footer>
</body></html>`;
}

/** A tiny line: ``points`` [{d, v}] oldest first, 80x16, one hover title per point. */
export function sparkline(points, { width = 80, height = 16 } = {}) {
  if (!points || points.length < 2) return "";
  const max = Math.max(...points.map(p => p.v), 0.5);
  const x = i => (i * (width - 4)) / (points.length - 1) + 2, y = v => height - 2 - (v / max) * (height - 4);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(p.v).toFixed(1)}`).join("");
  const hits = points.map((p, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="4" fill="transparent"><title>${esc(p.d)}: ${p.v.toFixed(2)} stalls per radio</title></circle>`).join("");
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="daily stalls per radio">` +
    `<path d="${path}" fill="none" stroke="var(--text2)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>${hits}</svg>`;
}

// --- the dashboard --------------------------------------------------------------------------------

function versionsPanel(byCh, current) {
  const order = ["stable", "bench", ...Object.keys(byCh).filter(c => c !== "stable" && c !== "bench")].filter(c => byCh[c]);
  if (!order.length) return `<article><h2>Versions</h2><p class="mut">No radio seen in the last ${GREY_DAYS} days.</p></article>`;
  const dd = order.map(ch => {
    const items = byCh[ch].map(e => {
      const st = Object.keys(e.states).map(s => (s === "rolled_back" ? w("fault", "went back") : w("warn", s))).join(" ");
      const old = behind(e.version, current[ch]);
      const v = `<span class="num${old ? " warn" : ""}">${esc(e.version)}</span> <span class="num">×${e.n}</span>`;
      return `${v}${st ? " " + st : ""}${old ? " " + w("warn", "old") : ""}`;
    }).join(" · ");
    return `<dt>${esc(ch === "stable" ? "Stable" : ch === "bench" ? "Bench" : ch)}</dt><dd>${items}</dd>`;
  }).join("");
  return `<article><h2>Versions</h2><dl>${dd}</dl></article>`;
}

function healthPanel(rows) {
  if (!rows.length) return `<article><h2>Release health</h2><p class="mut">No daily rows yet: the history starts with the first check-in.</p></article>`;
  const f = (v, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
  const tr = rows.slice(0, 6).map(r => {
    const up = k => (r.rise.includes(k) ? " warn" : "");
    const rb = r.rollbacks ? `<td class="num fault" title="radios that went back from it">${r.rollbacks}</td>` : `<td class="num">0</td>`;
    const ft = r.faultsPer100 ? `<td class="num fault" title="radio-days with a fault, per 100">${f(r.faultsPer100, 1)}</td>` : `<td class="num">0</td>`;
    return `<tr><td><b class="num">${esc(r.version)}</b>${r.prev ? `<small>vs ${esc(r.prev)}</small>` : ""}</td>` +
      `<td class="num">${r.radios}</td><td class="num">${r.radioDays}</td>${rb}${ft}` +
      `<td class="num${up("stalls")}">${f(r.stallsPerDay)}</td><td class="num${up("restarts")}">${f(r.restartsPerDay)}</td>` +
      `<td>${sparkline(r.daily)}</td></tr>`;
  }).join("");
  return `<article class="span2"><h2>Release health <small>last 30 days</small></h2><div class="scroll"><table class="mini"><thead><tr><th>Version</th>` +
    `<th title="radios">Radios</th><th title="radio-days">Days</th><th title="rollbacks">Back</th><th title="faults per 100 radio-days">Faults</th>` +
    `<th title="stalls per radio-day">Stalls</th><th title="MPD restarts per radio-day">Restarts</th><th>Stalls/day</th></tr></thead><tbody>${tr}</tbody></table></div></article>`;
}

function ytdlpPanel(y) {
  if (!y.lane) return `<article><h2>YouTube Music helper</h2><p class="mut">No radio with the helper has checked in today.</p></article>`;
  const vs = Object.entries(y.versions).sort((a, b) => b[1] - a[1]).map(([v, n]) => `<span class="num">${esc(v)}</span>${n !== y.lane ? ` <span class="num">×${n}</span>` : ""}`).join(" · ");
  const head = y.alarm
    ? `<p class="fault">broken on ${y.broken} of ${y.lane}</p>`
    : `<p>works on <b>${y.working} of ${y.lane}</b>${y.broken ? ` · ${w("warn", `${y.broken} broken`)}` : ""}${y.lane - y.working - y.broken ? ` · ${w("mut", `${y.lane - y.working - y.broken} checking`)}` : ""}</p>`;
  return `<article><h2>YouTube Music helper</h2>${head}${vs ? `<p>${vs}</p>` : ""}<p class="key">today's check-ins · red at 2 or more radios and 20 % or more</p></article>`;
}

function platformPanel(p) {
  if (!p.list.length) return `<article><h2>Platform</h2><p class="mut">No radio has said which Pi it is yet (it comes with 1.7).</p></article>`;
  const li = p.list.map(e => `<li><span>${esc(e.platform)}</span><span><span class="num">×${e.n}</span>${e.uv ? " " + w("warn", `${e.uv} low voltage`) : ""}</span></li>`).join("");
  return `<article><h2>Platform</h2><ul>${li}</ul>${p.unknown ? `<p class="key">${plural(p.unknown, "radio")} not saying yet</p>` : ""}</article>`;
}

/** The whole page. ``records`` [{id, rec}], ``issues`` {id: [...]}, ``q`` the "?key=..." to keep on links. */
export function renderDashboard({ records = [], now = Date.now(), repo = "", issues = {}, q = "" } = {}) {
  const byCh = versionsByChannel(records, now);
  const current = currentByChannel(byCh);
  const rows = disambiguate(sortRows(records.map(({ id, rec }) => rowOf(id, rec, now, { issues: issues[id], current }))));
  const seenTodayN = rows.filter(r => seenToday(r.seenMs, now)).length;
  const look = rows.filter(r => r.needsLook).length;
  const open = rows.reduce((a, r) => a + r.openRequests, 0);
  const verdict = rows.length
    ? `<b>${plural(rows.length, "radio")}</b> ${rows.length === 1 ? "has" : "have"} checked in · ${seenTodayN} seen today · ` +
      (look ? `<b class="warn">${look} need${look === 1 ? "s" : ""} a look</b>` : "none needs a look") +
      ` · ${plural(open, "open request")}`
    : "No radio has checked in yet.";
  const tr = rows.map(r => {
    const cls = [r.days > GREY_DAYS ? "grey" : "", r.needsLook ? "look" : ""].filter(Boolean).join(" ");
    const href = `/dashboard/radio/${encodeURIComponent(r.label.includes("·") ? r.id : r.short)}${q}`;
    return `<tr${cls ? ` class="${cls}"` : ""}>` +
      `<td class="radio" data-l="Radio"><a href="${esc(href)}" class="id">${esc(r.label)}</a><small>${esc(r.model)}</small></td>` +
      `<td data-l="Version">${versionCell(r)}</td><td data-l="Seen">${seenCell(r, now)}</td>` +
      `<td data-l="Health">${healthCell(r)}</td><td data-l="Player · stalls · restarts">${playerCell(r)}</td>` +
      `<td data-l="YouTube">${ytdlpCell(r)}</td><td data-l="Power">${powerCell(r)}</td><td data-l="Temp">${tempCell(r)}</td>` +
      `<td data-l="Card">${cardCell(r)}</td><td data-l="Up"><span class="num">${r.uptime === null ? "—" : r.uptime < 1 ? "&lt;1 d" : esc(r.uptime) + " d"}</span></td>` +
      `<td class="wide" data-l="Platform">${r.platform ? esc(r.platform) : w("mut", "—")}</td>` +
      `<td data-l="Requests">${requestsCell(r, repo, now) || w("mut", "—")}</td></tr>`;
  }).join("\n");
  const stamp = `<p class="stamp" title="${esc(utcTitle(now))}">updated ${esc(madridHM(now))} Madrid</p>`;
  return page("Hasler radios", `<header><h1>Hasler radios</h1><p class="verdict">${verdict}</p>${stamp}</header>
<section class="panels">${versionsPanel(byCh, current)}${healthPanel(releaseHealth(records, now))}${ytdlpPanel(ytdlpFleet(records, now))}${platformPanel(platforms(records, now))}</section>
<table class="fleet"><thead><tr><th>Radio</th><th>Version</th><th>Seen</th><th>Health</th><th title="stalls · MPD restarts, last 24 h">Player</th><th>YouTube</th><th>Power</th><th title="hottest in the last 24 h">Temp</th><th>Card</th><th>Up</th><th class="wide">Platform</th><th>Requests</th></tr></thead>
<tbody>
${tr}
</tbody></table>
<p class="key" style="margin-top:14px">Needs a look first, then the longest unseen. Player: stalls · restarts in the last 24 h. Amber: needs watching · red: a fault · grey: not seen for ${GREY_DAYS}+ days (off, offline or the check-in switched off - this page can't tell which).</p>`);
}

// --- one radio ------------------------------------------------------------------------------------

function bar(v, max) {
  if (v === null || v === undefined) return `<span class="mut">·</span>`;
  const blocks = "▁▂▃▄▅▆▇█";
  const i = v <= 0 ? 0 : Math.min(7, Math.ceil((v / Math.max(max, 1)) * 7));
  return `<span class="num" title="${esc(v)}">${blocks[i]} ${esc(v)}</span>`;
}

/** /dashboard/radio/<id>: header, now (cards), the last 60 days, requests, raw, Forget. */
export function renderRadio({ id, rec, now = Date.now(), repo = "", issues = [], q = "" }) {
  const r = rowOf(id, rec, now, { issues });
  const n = normalize(rec && rec.payload);
  const hw = n.hw || {}, os = n.os || {};
  const head = [r.model, r.channel === "bench" ? "Bench" : r.channel === "stable" ? "Stable" : r.channel, r.mode || null,
    hw.model ? hw.model.replace(/^Raspberry\s+/i, "") : null, hw.ram_gb ? `${hw.ram_gb} GB` : null,
    os.debian ? `Debian ${os.debian}` : null, os.kernel ? `kernel ${String(os.kernel).split("+")[0]}` : null,
    os.moode ? `moOde ${os.moode}` : null].filter(Boolean).map(esc).join(" · ");
  const rel = r.release;
  const relCard = rel ? (rel.state === "rolled_back"
    ? `${w("fault", `went back from ${rel.version || "?"} to ${rel.from || "?"}`)}${rel.words ? `<br>${esc(rel.words)}` : ""}${rel.reason ? `<small>${esc(rel.reason)}</small>` : ""}`
    : `${esc(RS_WORDS[{ trial: "t", probation: "p", committed: "c" }[rel.state]] || rel.state)} ${esc(rel.version || "")}${rel.from ? ` <small>from ${esc(rel.from)}</small>` : ""}`) : w("mut", "—");
  const os_up = r.osUpdates ? `${esc(r.osUpdates.state)}${r.osUpdates.last_ok_d !== null && r.osUpdates.last_ok_d !== undefined ? ` <small>last good ${esc(r.osUpdates.last_ok_d)} d ago</small>` : ""}` : w("mut", "not on this radio");
  const cards = [
    ["Version", versionCell(r)], ["Release", relCard], ["Seen", seenCell(r, now)], ["Health", healthCell(r)],
    ["Player, 24 h", playerCell(r) + `<small>stalls · MPD restarts${r.crashes !== null ? " · haslerd restarts " + esc(r.crashes) : ""}</small>`],
    ["YouTube Music", ytdlpCell(r) + (r.ytdlpVersion ? `<small>${esc(r.ytdlpVersion)}</small>` : "")],
    ["Power", powerCell(r)], ["Temperature", tempCell(r) + `<small>hottest in 24 h · now ${esc(r.tempNow ?? "?")}°</small>`],
    ["Card", cardCell(r) + (r.totalGb ? `<small>of ${esc(r.totalGb)} GB</small>` : "")],
    ["Up", r.uptime === null ? w("mut", "—") : `${r.uptime < 1 ? "under a day" : esc(r.uptime) + " d"}`],
    ["Updates", `${esc(r.mode || "?")} · check ${esc(r.check || "?")}`], ["OS updates", os_up],
  ].map(([k, v]) => `<div><b class="k">${esc(k)}</b>${v}</div>`).join("");
  const days = (rec && rec.days) || [];
  const maxS = Math.max(1, ...days.map(d => d.s || 0));
  const rows = days.map((d, i) => {
    const vchange = days[i + 1] && days[i + 1].v !== d.v;
    const faults = (d.f || []).map(faultWord).join(", ");
    return `<tr${vchange ? ' class="vchange"' : ""}><td class="num">${esc(d.d)}</td><td class="num">${esc(d.v)}${d.rs === "r" ? " " + w("fault", "went back") : d.rs === "p" || d.rs === "t" ? " " + w("warn", RS_WORDS[d.rs]) : ""}</td>` +
      `<td><span class="dot" title="checked in"></span></td><td>${bar(d.s, maxS)}</td><td class="num">${d.r ?? "·"}</td>` +
      `<td class="hide-s num">${d.dr ?? "·"}</td><td>${faults ? w("fault", faults) : d.h === 0 ? w("fault", "self-check") : ""}</td>` +
      `<td class="num${d.t >= HOT_C ? " fault" : d.t >= HOT_WARN_C ? " warn" : ""}">${d.t ?? "·"}${d.t !== null && d.t !== undefined ? "°" : ""}</td>` +
      `<td>${d.uv ? '<span class="dot warn" title="low voltage"></span>' : ""}</td><td>${d.pc ? '<span class="dot warn" title="power cut"></span>' : ""}</td>` +
      `<td class="hide-s">${d.y === 0 ? w("fault", "broken") : d.y === 1 ? "works" : ""}</td></tr>`;
  }).join("\n");
  const reqs = (issues || []).length ? `<ul>${issues.map(i => {
    const st = i.state || "open";
    const text = `#${i.number} · ${st} · ${String(i.at || "").slice(0, 10)}`;
    return `<li>${repo ? `<a href="https://github.com/${esc(repo)}/issues/${esc(i.number)}">${esc(text)}</a>` : esc(text)}</li>`;
  }).join("")}</ul>` : `<p class="mut">None.</p>`;
  const forget = `<form method="post" action="/dashboard/radio/${esc(encodeURIComponent(id))}/forget${esc(q)}" onsubmit="return confirm('Forget ${esc(r.short)}? Its record and history go now; it comes back only if it checks in again.')"><button class="forget" type="submit">Forget this radio</button> <small style="display:inline">for a radio that has been returned</small></form>`;
  return page(`${r.short} · Hasler radios`, `<p class="stamp"><a href="/dashboard${esc(q)}">← all radios</a></p>
<header><h1><span class="num" style="font-size:24px">${esc(r.short)}</span></h1><p class="verdict" style="font-size:15px">${head}</p>
<p class="stamp" title="${esc(utcTitle(now))}">updated ${esc(madridHM(now))} Madrid</p></header>
<h2 style="margin-top:22px">Now</h2><section class="cards">${cards}</section>
<h2>Last ${days.length} day${days.length === 1 ? "" : "s"}</h2>
<div class="scroll"><table class="grid"><thead><tr><th>Date (UTC)</th><th>Version</th><th>Seen</th><th>Stalls</th><th title="MPD restarts">Restarts</th><th class="hide-s" title="haslerd restarts">Crashes</th><th>Faults</th><th>Temp max</th><th>Low V</th><th>Cut</th><th class="hide-s">YouTube</th></tr></thead>
<tbody>${rows || `<tr><td colspan="11" class="mut">No daily rows yet.</td></tr>`}</tbody></table></div>
<p class="key" style="margin-top:6px">A cream rule marks the day the version changed. Days without a row: not seen.</p>
<h2 style="margin-top:22px">Owner requests</h2>${reqs}
<details><summary>Raw: the last check-in, exactly as the radio sent it</summary><pre>${esc(JSON.stringify(rec && rec.payload, null, 1))}</pre></details>
<div style="margin-top:22px">${forget}</div>`);
}

export { piShort };
