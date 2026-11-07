// The fleet, counted: release health per version (Q1, Q3), the version spread per channel (Q5),
// the YouTube Music helper across radios (Q4), the platforms, and the daily anonymous roll-up the
// cron keeps for 400 days. Pure functions over the radios' records ({id, rec: {payload, last_seen,
// days}}); no KV, no network (fleet.test.mjs).

import { normalize, cmpVersion, platformOf, GREY_DAYS } from "./model.js";

export const HEALTH_WINDOW_DAYS = 30;
export const TREND_MIN_RADIO_DAYS = 10;
export const YTDLP_MIN_BROKEN = 2;       // the fleet alert and the red panel: at least this many radios...
export const YTDLP_MIN_SHARE = 0.2;      // ...and at least this share of the radios seen

const isoDay = t => new Date(t).toISOString().slice(0, 10);
const daysBefore = (now, n) => isoDay(now - n * 86400000);

/** Every daily row in the window, with its radio: [{id, row}]. */
function rowsSince(records, since) {
  const out = [];
  for (const { id, rec } of records) for (const row of (rec && rec.days) || []) if (row && row.d >= since) out.push({ id, row });
  return out;
}

const rate = (sum, n) => (n > 0 ? sum / n : null);

/** One row per version seen in the last HEALTH_WINDOW_DAYS, newest version first. */
export function releaseHealth(records, now) {
  const since = daysBefore(now, HEALTH_WINDOW_DAYS);
  const all = rowsSince(records, since);
  const by = new Map();
  const get = v => {
    if (!by.has(v)) by.set(v, { version: v, radios: new Set(), radioDays: 0, rollbacks: new Set(), faultDays: 0,
      stalls: 0, stallDays: 0, restarts: 0, restartDays: 0, crashes: 0, crashDays: 0, uv: 0, daily: new Map() });
    return by.get(v);
  };
  for (const { id, row } of all) {
    if (row.v) {
      const g = get(row.v);
      g.radios.add(id); g.radioDays++;
      if ((row.f && row.f.length) || row.h === 0) g.faultDays++;
      if (typeof row.s === "number") { g.stalls += row.s; g.stallDays++; }
      if (typeof row.r === "number") { g.restarts += row.r; g.restartDays++; }
      if (typeof row.dr === "number") { g.crashes += row.dr; g.crashDays++; }
      if (row.uv) g.uv++;
      if (typeof row.s === "number") {
        const d = g.daily.get(row.d) || [0, 0];
        g.daily.set(row.d, [d[0] + row.s, d[1] + 1]);
      }
    }
    if (row.rb) get(row.rb).rollbacks.add(id);
  }
  const rows = [...by.values()].sort((a, b) => cmpVersion(b.version, a.version)).map(g => ({
    version: g.version, radios: g.radios.size, radioDays: g.radioDays, rollbacks: g.rollbacks.size,
    faultsPer100: g.radioDays ? (100 * g.faultDays) / g.radioDays : null,
    stallsPerDay: rate(g.stalls, g.stallDays), restartsPerDay: rate(g.restarts, g.restartDays),
    crashesPerDay: rate(g.crashes, g.crashDays), uvDays: g.uv,
    daily: [...g.daily.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1)).map(([d, [s, n]]) => ({ d, v: s / n })),
  }));
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i], prev = rows[i + 1] || null;
    r.prev = prev ? prev.version : null;
    r.rise = [];
    if (prev && r.radioDays >= TREND_MIN_RADIO_DAYS && prev.radioDays >= TREND_MIN_RADIO_DAYS) {
      for (const [k, word] of [["stallsPerDay", "stalls"], ["restartsPerDay", "restarts"], ["crashesPerDay", "crashes"]]) {
        const a = r[k], b = prev[k];
        if (a === null || b === null) continue;
        if ((b > 0 && a >= 2 * b) || (b === 0 && a >= 0.2)) r.rise.push(word);
      }
    }
  }
  return rows;
}

/** {channel: [{version, n, states: {probation: n, ...}}]} from the radios seen in the last GREY_DAYS. */
export function versionsByChannel(records, now) {
  const out = {};
  for (const { rec } of records) {
    const seen = Date.parse(rec && rec.last_seen);
    if (!Number.isFinite(seen) || now - seen > GREY_DAYS * 86400000) continue;
    const n = normalize(rec.payload);
    const ch = n.channel || "?", v = n.version || "?";
    const list = (out[ch] = out[ch] || []);
    let e = list.find(x => x.version === v);
    if (!e) list.push(e = { version: v, n: 0, states: {} });
    e.n++;
    const st = n.release && n.release.version === v ? n.release.state : null;
    if (st && st !== "committed") e.states[st] = (e.states[st] || 0) + 1;
  }
  for (const ch of Object.keys(out)) out[ch].sort((a, b) => cmpVersion(b.version, a.version));
  return out;
}

/** The newest version on each channel, and the one before it (the "behind" rule: older than that is amber). */
export function currentByChannel(byChannel) {
  const out = {};
  for (const [ch, list] of Object.entries(byChannel)) {
    const vs = list.map(x => x.version).filter(v => v !== "?");
    out[ch] = { current: vs[0] || null, previous: vs[1] || null };
  }
  return out;
}

export function behind(version, cur) {
  if (!cur || !cur.current || !version) return false;
  const floor = cur.previous || cur.current;
  return cmpVersion(version, floor) < 0;
}

/** The YouTube Music helper across the radios seen today (UTC date of ``now``). */
export function ytdlpFleet(records, now) {
  const today = isoDay(now);
  let lane = 0, working = 0, broken = 0;
  const versions = {};
  for (const { rec } of records) {
    if (!rec || !rec.last_seen || isoDay(Date.parse(rec.last_seen)) !== today) continue;
    const n = normalize(rec.payload);
    if (!n.ytdlp) continue;
    lane++;
    if (n.ytdlp.working === true) working++;
    if (n.ytdlp.working === false) broken++;
    if (n.ytdlp.version) versions[n.ytdlp.version] = (versions[n.ytdlp.version] || 0) + 1;
  }
  const alarm = broken >= YTDLP_MIN_BROKEN && lane > 0 && broken / lane >= YTDLP_MIN_SHARE;
  return { lane, working, broken, versions, alarm };
}

/** [{platform, n, uv}] for the radios seen in the last GREY_DAYS that said which Pi they are. */
export function platforms(records, now) {
  const m = new Map();
  let unknown = 0;
  for (const { rec } of records) {
    const seen = Date.parse(rec && rec.last_seen);
    if (!Number.isFinite(seen) || now - seen > GREY_DAYS * 86400000) continue;
    const n = normalize(rec.payload);
    const p = platformOf(n);
    if (!p) { unknown++; continue; }
    const e = m.get(p) || { platform: p, n: 0, uv: 0 };
    e.n++;
    if (n.power.undervolt_24h) e.uv++;
    m.set(p, e);
  }
  return { list: [...m.values()].sort((a, b) => b.n - a.n || a.platform.localeCompare(b.platform)), unknown };
}

/** The anonymous daily roll-up for UTC date ``date``: counts only, no box ids (fleet:<date>, 400 days). */
export function rollup(records, date) {
  const out = { date, radios_seen: 0, by_channel: {}, by_version: {}, ytdlp: { working: 0, broken: 0, by_version: {} }, platform: {} };
  for (const { rec } of records) {
    const row = ((rec && rec.days) || []).find(r => r && r.d === date);
    if (!row) continue;
    out.radios_seen++;
    const ch = row.ch === "b" ? "bench" : row.ch === "s" ? "stable" : "other";
    const v = row.v || "?";
    out.by_channel[ch] = out.by_channel[ch] || {};
    out.by_channel[ch][v] = (out.by_channel[ch][v] || 0) + 1;
    const g = (out.by_version[v] = out.by_version[v] || { n: 0, rollbacks: 0, faults: 0, stalls: 0, restarts: 0, daemon_restarts: 0, uv: 0 });
    g.n++;
    if ((row.f && row.f.length) || row.h === 0) g.faults++;
    g.stalls += row.s || 0; g.restarts += row.r || 0; g.daemon_restarts += row.dr || 0; g.uv += row.uv ? 1 : 0;
    if (row.rb) {
      const b = (out.by_version[row.rb] = out.by_version[row.rb] || { n: 0, rollbacks: 0, faults: 0, stalls: 0, restarts: 0, daemon_restarts: 0, uv: 0 });
      b.rollbacks++;
    }
    if (row.y === 1) out.ytdlp.working++;
    if (row.y === 0) out.ytdlp.broken++;
    if (row.yv) out.ytdlp.by_version[row.yv] = (out.ytdlp.by_version[row.yv] || 0) + 1;
    const p = platformOf(normalize(rec.payload));
    if (p) out.platform[p] = (out.platform[p] || 0) + 1;
  }
  return out;
}
