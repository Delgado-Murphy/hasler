// The makers' check-in endpoint: a Cloudflare Worker with one KV namespace.
//
// What it does, and all it does (docs/ota.md section 5; this file is in the
// public repo so an owner can read what the server does):
//
//   POST /v1/checkin   a radio's daily check-in (update/hasler_update/checkin.py):
//                      a JSON object of at most 2 KB with a string box_id. Stored as
//                      box_id -> {payload, last_seen}. The IP is never read, never
//                      stored, never logged. Anything else is 400; a bad or missing
//                      X-Hasler-Token is 403 when CHECKIN_TOKEN is set. Answers 204.
//   GET  /dashboard    one HTML table for the makers, sorted "needs a look first".
//                      Put it behind Cloudflare Access, or set DASHBOARD_KEY and
//                      open /dashboard?key=... . A radio not heard from is "not seen",
//                      never "broken": the dashboard can't tell off from offline.
//   GET  /v1/radios    the same rows as JSON (same gate as the dashboard).
//
// Bindings (wrangler.toml): CHECKINS (KV). Vars/secrets: CHECKIN_TOKEN (optional,
// the shared word radios send in X-Hasler-Token; box.yaml checkin_token),
// DASHBOARD_KEY (optional, the dashboard's ?key=). README.md has the steps.

const MAX_BODY = 2048;
const MAX_ID = 64;
const AMBER_DAYS = 3;   // a row older than this is amber...
const GREY_DAYS = 14;   // ...and older than this grey ("not seen")

// --- validation (exported for the test) ---------------------------------------------------------

export function validate(text) {
  if (typeof text !== "string" || text.length === 0) return { error: "empty body" };
  if (text.length > MAX_BODY) return { error: `body larger than ${MAX_BODY} bytes` };
  let doc;
  try { doc = JSON.parse(text); } catch (_) { return { error: "not JSON" }; }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return { error: "not a JSON object" };
  const id = doc.box_id;
  if (typeof id !== "string" || !/^[A-Za-z0-9._-]{1,64}$/.test(id)) return { error: "box_id must be a short plain string" };
  if (doc.schema !== undefined && doc.schema !== 1) return { error: "unknown schema" };
  return { doc, id };
}

export function tokenOk(request, env) {
  const want = (env && env.CHECKIN_TOKEN) || "";
  if (!want) return true;
  return request.headers.get("X-Hasler-Token") === want;
}

function dashboardOk(url, request, env) {
  const key = (env && env.DASHBOARD_KEY) || "";
  if (!key) return true;                 // Cloudflare Access (or nothing) gates it
  return url.searchParams.get("key") === key || request.headers.get("Authorization") === `Bearer ${key}`;
}

// --- rows ----------------------------------------------------------------------------------------

export function rowOf(id, rec, now) {
  const p = (rec && rec.payload) || {};
  const seenAt = rec && rec.last_seen ? Date.parse(rec.last_seen) : NaN;
  const days = Number.isFinite(seenAt) ? (now - seenAt) / 86400000 : Infinity;
  const res = p.last_result || null;
  const health = p.health || {};
  const faults = Array.isArray(p.faults) ? p.faults : [];
  const rollback = res && res.ok === false ? `${res.version || "?"}→${res.back_to || "?"}` : "";
  const needsLook = (health.ok === false) || faults.length > 0 || !!rollback || p.storage_bad === true || days > GREY_DAYS;
  return {
    id, short: p.short_id || id.slice(0, 4), variant: p.variant || "", version: p.version || "", channel: p.channel || "",
    seen: rec && rec.last_seen ? rec.last_seen : null, days,
    health: health.ok === true ? "OK" : health.ok === false ? "!" : "?",
    failed: Array.isArray(health.failed) ? health.failed.join(" ") : "",
    faults: faults.join(" "), rollback, reason: res && res.reason ? String(res.reason) : "",
    cuts: p.power_cuts_month ?? "", stalls: p.player && p.player.stalls_today != null ? p.player.stalls_today : "",
    restarts: p.player && p.player.restarts_last_hour != null ? p.player.restarts_last_hour : "",
    free: p.free_bytes, temp: p.temp_c ?? "", throttled: p.throttled || "",
    ytdlp: p.ytdlp ? (p.ytdlp.working === true ? "ok" : p.ytdlp.working === false ? "not working" : "?") : "—",
    uptime: p.uptime_days ?? "", shutdown: p.last_shutdown || "", mode: p.update_mode || "",
    needsLook,
  };
}

export function sortRows(rows) {
  // needs a look first; among those, and among the rest, the longest unseen first
  return rows.slice().sort((a, b) => (b.needsLook - a.needsLook) || (b.days - a.days) || a.short.localeCompare(b.short));
}

function size(n) {
  if (typeof n !== "number") return "?";
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  return `${Math.floor(n / 1024 ** 2)} MB`;
}

function ago(days) {
  if (!Number.isFinite(days)) return "not seen";
  if (days < 1 / 24) return "just now";
  if (days < 1) return `${Math.round(days * 24)} h ago`;
  return `${Math.round(days)} d ago`;
}

const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function renderDashboard(rows, now) {
  const tr = rows.map(r => {
    const cls = r.days > GREY_DAYS ? "grey" : r.days > AMBER_DAYS ? "amber" : "";
    const look = r.needsLook ? " look" : "";
    return `<tr class="${cls}${look}"><td>${esc(r.short)}</td><td>${esc(r.variant)}</td><td>${esc(r.version)}</td><td>${esc(r.channel)}</td>` +
      `<td title="${esc(r.seen || "")}">${esc(ago(r.days))}</td><td>${esc(r.health)}${r.failed ? " " + esc(r.failed) : ""}</td>` +
      `<td>${esc(r.faults || "—")}</td><td title="${esc(r.reason)}">${esc(r.rollback || "—")}</td><td>${esc(r.cuts)}</td>` +
      `<td>${esc(r.stalls)}</td><td>${esc(r.restarts)}</td><td>${esc(size(r.free))}</td><td>${esc(r.temp)}${r.throttled && r.throttled !== "0x0" ? " " + esc(r.throttled) : ""}</td>` +
      `<td>${esc(r.ytdlp)}</td><td>${esc(r.uptime)}</td><td>${esc(r.shutdown)}</td><td>${esc(r.mode)}</td></tr>`;
  }).join("\n");
  const stamp = new Date(now).toISOString().replace("T", " ").slice(0, 16);
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hasler radios</title>
<style>
body{margin:0;padding:24px;background:#1B140C;color:#F2E6D0;font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
h1{font-size:18px;font-weight:600;margin:0 0 4px}p{margin:0 0 16px;color:#B29F80}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #3A2C1C;white-space:nowrap}
th{color:#B29F80;font-weight:500}tr.look td:first-child{color:#F3CF8C;font-weight:600}
tr.amber td{color:#E9B35C}tr.grey td{color:#7A6A52}
.key{margin-top:16px;color:#7A6A52;font-size:12px}
</style></head><body>
<h1>Hasler radios</h1>
<p>${rows.length} radio${rows.length === 1 ? "" : "s"} have checked in · ${esc(stamp)} UTC · needs a look first, then the longest unseen.</p>
<table><thead><tr><th>radio</th><th>variant</th><th>version</th><th>ch</th><th>seen</th><th>health</th><th>faults</th><th>rollback</th><th>cuts</th><th>stalls</th><th>restarts</th><th>free</th><th>temp</th><th>yt-dlp</th><th>up (d)</th><th>last off</th><th>mode</th></tr></thead>
<tbody>
${tr}
</tbody></table>
<p class="key">amber: not seen for ${AMBER_DAYS}+ days · grey: ${GREY_DAYS}+ days (off, offline, or the check-in switched off - this page can't tell which). "Not seen" is all it says; a bold id is a radio that needs a look.</p>
</body></html>`;
}

// --- the worker ----------------------------------------------------------------------------------

async function listAll(kv) {
  const rows = [];
  let cursor;
  do {
    const page = await kv.list({ cursor, limit: 1000 });
    for (const k of page.keys) {
      const rec = await kv.get(k.name, "json");
      if (rec) rows.push({ id: k.name, rec });
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return rows;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const now = Date.now();
    if (request.method === "POST" && url.pathname === "/v1/checkin") {
      if (!tokenOk(request, env)) return new Response("no", { status: 403 });
      const length = Number(request.headers.get("content-length") || 0);
      if (length > MAX_BODY) return new Response("too large", { status: 413 });
      const text = await request.text();
      const v = validate(text);
      if (v.error) return new Response(v.error, { status: 400 });
      // Stored: the payload and when. Not stored, not read: the IP, the headers.
      await env.CHECKINS.put(v.id, JSON.stringify({ payload: v.doc, last_seen: new Date(now).toISOString() }));
      return new Response(null, { status: 204 });
    }
    if (request.method === "GET" && (url.pathname === "/dashboard" || url.pathname === "/v1/radios")) {
      if (!dashboardOk(url, request, env)) return new Response("no", { status: 403 });
      const rows = sortRows((await listAll(env.CHECKINS)).map(({ id, rec }) => rowOf(id, rec, now)));
      if (url.pathname === "/v1/radios") {
        return new Response(JSON.stringify(rows, null, 1), { headers: { "content-type": "application/json; charset=utf-8" } });
      }
      return new Response(renderDashboard(rows, now), { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
    }
    if (request.method === "GET" && url.pathname === "/") {
      return new Response("hasler check-in: POST /v1/checkin\n", { headers: { "content-type": "text/plain" } });
    }
    return new Response("not found", { status: 404 });
  },
};
