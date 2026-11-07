// What a check-in means: schema 1 and 2 payloads in one shape, the daily history row, and the
// words the dashboard and the alerts use. Pure functions, no KV, no network (model.test.mjs).
//
// Schema 2 is update/hasler_update/checkin.py's; schema 1 (radios before 1.7) is turned into the
// same shape here so the page reads one thing. What schema 1 never knew stays null.

export const HISTORY_DAYS = 60;           // per-radio daily rows, inside the radio's own KV key
export const AMBER_DAYS = 3;              // not seen for longer than this: amber...
export const GREY_DAYS = 14;              // ...and longer than this: grey ("not seen")
export const HOT_WARN_C = 75;
export const HOT_C = 80;                  // the lamp's heat fault (lamp/faults.py HOT_C)

export const VARIANT_LABELS = {
  "hasler-hm-hat": "Hasler HM (HAT)",
  "hasler-hm": "Hasler HM (prototype board)",
  "excelsior": "Excelsior",
};
export const variantLabel = v => VARIANT_LABELS[v] || (v ? String(v) : "—");

// The lamp's fault words (lamp/faults.py) and the health checks (/api/health), in words.
export const FAULT_WORDS = { update: "update stuck", storage: "card full", sound: "no sound card", panel: "no keys", heat: "too hot" };
export const CHECK_WORDS = { mpd: "player", audio_card: "sound card", panel: "keys", lamp: "lamp", web: "page" };
export const faultWord = w => FAULT_WORDS[w] || String(w);
export const checkWord = w => CHECK_WORDS[w] || String(w);

// The updater's rollback reasons in words (the same table as daemon/haslerd/web/software.py ROLLBACK_WORDS).
const ROLLBACK_WORDS = [
  [/^check panel failed/i, "the keys weren't answering after the switch"],
  [/^check mpd failed/i, "the player wasn't answering after the switch"],
  [/^check audio_card failed/i, "the sound card wasn't found after the switch"],
  [/^check lamp failed/i, "the lamp wasn't answering after the switch"],
  [/^check web failed|\/api\/health not answering/i, "the page wasn't answering after the switch"],
  [/^check \w+ failed/i, "a self-check failed after the switch"],
  [/^haslerd (failed|not active|restarted)/i, "the new version kept crashing"],
  [/^boot loop \(2 /i, "the radio restarted twice in a row"],
  [/^boot loop/i, "the radio kept restarting"],
  [/^power cut during the switch/i, "the power went in the middle"],
  [/^rolled back by hand/i, "the owner went back from the page"],
  [/^sound card missing/i, "the sound card vanished"],
  [/^MPD not back within|^mpd lost/i, "the player didn't come back in time"],
  [/^librespot lost/i, "Spotify's helper stopped"],
  [/^moode_ui lost/i, "the player's own page stopped answering"],
  [/^\/api\/health ok=false/i, "the radio's self-check failed"],
  [/^\/api\/health reports/i, "the new version didn't start"],
  [/selftest|self-test/i, "the new version failed its self-test"],
  [/^resumed rollback/i, "the radio finished going back after a restart"],
];
export function rollbackWords(reason) {
  const parts = String(reason || "").split(";").map(s => s.trim()).filter(Boolean);
  const out = [];
  for (const p of parts) {
    const hit = ROLLBACK_WORDS.find(([re]) => re.test(p));
    const w = hit ? hit[1] : p;
    if (!out.includes(w)) out.push(w);
  }
  return out.join("; ");
}

// --- semver ---------------------------------------------------------------------------------------

export function cmpVersion(a, b) {
  const pa = String(a || "").split(/[.+-]/).map(n => parseInt(n, 10));
  const pb = String(b || "").split(/[.+-]/).map(n => parseInt(n, 10));
  for (let i = 0; i < 3; i++) {
    const x = Number.isFinite(pa[i]) ? pa[i] : -1, y = Number.isFinite(pb[i]) ? pb[i] : -1;
    if (x !== y) return x - y;
  }
  return 0;
}

// --- one shape ------------------------------------------------------------------------------------

const num = v => (typeof v === "number" && Number.isFinite(v) ? v : null);
const bool = v => (typeof v === "boolean" ? v : null);
const strs = v => (Array.isArray(v) ? v.filter(x => typeof x === "string") : []);

function v1release(r) {
  if (!r || typeof r !== "object") return null;
  if (r.ok === false) return { state: "rolled_back", version: r.version || null, from: r.back_to || null, reason: r.reason || null };
  if (r.ok === true) return { state: r.probation ? "probation" : "committed", version: r.version || null, from: null, reason: null };
  return { state: "trial", version: r.version || null, from: null, reason: null };
}

function v1power(p) {
  let word = null;
  if (typeof p.throttled === "string" && /^0x[0-9a-f]+$/i.test(p.throttled)) word = parseInt(p.throttled, 16);
  const bit = b => (word === null ? null : !!(word & (1 << b)));
  return {
    last_off: p.last_shutdown || null, cuts_30d: num(p.power_cuts_month),
    undervolt_now: bit(0), undervolt_24h: word === null ? null : bit(0) || bit(16),
    throttled_24h: word === null ? null : bit(1) || bit(2) || bit(17) || bit(18),
  };
}

function v1storage(p) {
  const free = num(p.free_bytes);
  const state = p.storage_bad === true ? "full" : free === null ? null : free < 1024 ** 3 ? "low" : "ok";
  return { free_mb: free === null ? null : Math.floor(free / 1024 ** 2), total_gb: null, state };
}

function v1ytdlp(y) {
  // Schema 1 sent the bare boolean (B1) - null meant both "no lane" and "don't know yet".
  if (typeof y === "boolean") return { working: y, version: null, failures_24h: null };
  if (y && typeof y === "object") return { working: bool(y.working), version: y.version || null, failures_24h: num(y.failures_24h) };
  return null;
}

/** Any accepted payload -> the schema 2 shape (missing parts null). */
export function normalize(p) {
  p = p && typeof p === "object" ? p : {};
  if (p.schema === 2) {
    const f = p.faults && typeof p.faults === "object" && !Array.isArray(p.faults) ? p.faults : {};
    const ytdlp = p.ytdlp && typeof p.ytdlp === "object" ? p.ytdlp : null;
    return {
      schema: 2, box_id: p.box_id, short_id: p.short_id || null, variant: p.variant || null, version: p.version || null,
      channel: p.channel || null, update_mode: p.update_mode || null, check: p.check || null,
      release: p.release && typeof p.release === "object" ? p.release : null,
      health: { ok: bool(p.health && p.health.ok), failed: strs(p.health && p.health.failed) },
      faults: { now: strs(f.now), day: strs(f.day) },
      player: { stalls_24h: num(p.player && p.player.stalls_24h), restarts_24h: num(p.player && p.player.restarts_24h),
                daemon_restarts_24h: num(p.player && p.player.daemon_restarts_24h) },
      ytdlp: ytdlp ? { working: bool(ytdlp.working), version: ytdlp.version || null, failures_24h: num(ytdlp.failures_24h) } : null,
      os_updates: p.os_updates && typeof p.os_updates === "object" ? p.os_updates : null,
      power: { last_off: null, cuts_30d: null, undervolt_now: null, undervolt_24h: null, throttled_24h: null, ...(p.power || {}) },
      temp_c: { now: num(p.temp_c && p.temp_c.now), max_24h: num(p.temp_c && p.temp_c.max_24h) },
      storage: { free_mb: null, total_gb: null, state: null, ...(p.storage || {}) },
      uptime_d: num(p.uptime_d), hw: p.hw && typeof p.hw === "object" ? p.hw : null, os: p.os && typeof p.os === "object" ? p.os : null,
    };
  }
  const t = num(p.temp_c);
  return {
    schema: 1, box_id: p.box_id, short_id: p.short_id || null, variant: p.variant || null, version: p.version || null,
    channel: p.channel || null, update_mode: p.update_mode || null, check: p.check || null,
    release: v1release(p.last_result),
    health: { ok: bool(p.health && p.health.ok), failed: strs(p.health && p.health.failed) },
    faults: { now: strs(p.faults), day: [] },
    player: { stalls_24h: num(p.player && p.player.stalls_today), restarts_24h: num(p.player && p.player.restarts_last_hour),
              daemon_restarts_24h: null },
    ytdlp: v1ytdlp(p.ytdlp),
    os_updates: typeof p.os_updates === "string" ? { state: p.os_updates, last_ok_d: null } : null,
    power: v1power(p), temp_c: { now: t, max_24h: t }, storage: v1storage(p),
    uptime_d: num(p.uptime_days) === null ? null : Math.floor(p.uptime_days), hw: null, os: null,
  };
}

// --- what needs a look ----------------------------------------------------------------------------

/** The fault words of a payload (lamp faults now and in the day, failed checks), sorted, unique. */
export function faultSet(n) {
  return [...new Set([...n.faults.now, ...n.faults.day, ...n.health.failed.map(c => `check:${c}`)])].sort();
}

export function isFault(n) {
  return faultSet(n).length > 0 || n.health.ok === false;
}

/** Why a radio needs a look today (Q2), as short words; [] when it doesn't. */
export function lookReasons(n, days) {
  const out = [];
  if (isFault(n)) out.push("fault");
  if (n.release && n.release.state === "rolled_back") out.push("rollback");
  if (n.storage.state === "full" || n.storage.state === "read-only") out.push("card");
  if (n.ytdlp && n.ytdlp.working === false) out.push("youtube");
  if (n.power.undervolt_24h === true) out.push("power");
  const t = n.temp_c.max_24h ?? n.temp_c.now;
  if (t !== null && t >= HOT_C) out.push("heat");
  if (Number.isFinite(days) && days > AMBER_DAYS && days <= GREY_DAYS) out.push("quiet");
  return out;
}

// --- the daily history row ------------------------------------------------------------------------

const RS = { trial: "t", probation: "p", committed: "c", rolled_back: "r" };
export const RS_WORDS = { t: "trial", p: "probation", c: "committed", r: "went back" };

/** One compact row for date ``d`` (UTC). ``prev`` is the previous row (for the power-cut flag). */
export function dayRow(n, d, prev) {
  const rel = n.release || {};
  const cuts = n.power.cuts_30d;
  const pc = cuts !== null && prev && typeof prev.c30 === "number" ? (cuts > prev.c30 ? 1 : 0)
    : n.power.last_off === "power-cut" && n.uptime_d === 0 ? 1 : 0;
  const row = {
    d, v: n.version || "", ch: n.channel === "bench" ? "b" : n.channel === "stable" ? "s" : "", rs: RS[rel.state] || "",
    h: n.health.ok === false ? 0 : 1, f: [...new Set([...n.faults.now, ...n.faults.day])].sort(),
    s: n.player.stalls_24h, r: n.player.restarts_24h, dr: n.player.daemon_restarts_24h,
    y: n.ytdlp ? (n.ytdlp.working === true ? 1 : n.ytdlp.working === false ? 0 : null) : null,
    t: n.temp_c.max_24h ?? n.temp_c.now, uv: n.power.undervolt_24h ? 1 : 0, pc, st: n.storage.state || "",
  };
  if (rel.state === "rolled_back" && rel.version) row.rb = rel.version;
  if (cuts !== null) row.c30 = cuts;
  if (n.ytdlp && n.ytdlp.version) row.yv = String(n.ytdlp.version).slice(0, 24);
  return row;
}

/** ``days`` (newest first) with today's row in: the same date replaces, capped at HISTORY_DAYS. */
export function withDay(days, row) {
  const rest = (Array.isArray(days) ? days : []).filter(r => r && r.d !== row.d);
  return [row, ...rest].sort((a, b) => (a.d < b.d ? 1 : a.d > b.d ? -1 : 0)).slice(0, HISTORY_DAYS);
}

// --- hardware -------------------------------------------------------------------------------------

/** "Raspberry Pi 4 Model B Rev 1.5" -> "Pi 4B". */
export function piShort(model) {
  if (!model) return null;
  return String(model).replace(/^Raspberry\s+/i, "").replace(/\s+Rev\s+[\d.]+$/i, "")
    .replace(/\s+Model\s+B\s+Plus$/i, "B+").replace(/\s+Model\s+A\s+Plus$/i, "A+")
    .replace(/\s+Model\s+([AB])$/i, "$1").replace(/\s+Model\s+/i, " ").trim();
}

/** "Pi 4B · 8 GB · moOde 10.3.7", or null when the radio didn't say (schema 1). */
export function platformOf(n) {
  if (!n.hw && !n.os) return null;
  const bits = [piShort(n.hw && n.hw.model) || "Pi ?"];
  if (n.hw && n.hw.ram_gb) bits.push(`${n.hw.ram_gb} GB`);
  bits.push(n.os && n.os.moode ? `moOde ${n.os.moode}` : "no moOde");
  return bits.join(" · ");
}
