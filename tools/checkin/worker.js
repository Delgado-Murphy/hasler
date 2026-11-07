// The makers' check-in endpoint: a Cloudflare Worker with one KV namespace for the radios.
//
// What it does, and all it does (docs/ota.md section 5; this file is in the public repo so an
// owner can read what the server does):
//
//   POST /v1/checkin   a radio's daily check-in (update/hasler_update/checkin.py): a JSON object of
//                      at most 2 KB with a string box_id, schema 1 (radios before 1.7) or 2. Stored
//                      as box_id -> {payload, last_seen, days}: the latest payload verbatim, when
//                      (to the minute, this record only), and one compact row per UTC date for the
//                      last 60 days (dates only, no times; model.js dayRow). One KV write per
//                      check-in. The IP is never read, never stored, never logged. Anything else is
//                      400; X-Hasler-Token must be CHECKIN_TOKEN or the radio's own (enrol.js) when
//                      either is set up, else 403. Answers 204. Then the fault / rollback / quiet
//                      alerts for that radio (alerts.js), after the answer.
//   POST /v1/forget    {box_id}, same token rule: the owner switched the check-in off, or handed
//                      the radio over - its record and history are deleted now. Answers 204.
//   POST /v1/enrol     a radio asks for its own token, once (enrol.js).
//   GET  /dashboard    the makers' page (dashboard.js). Behind Cloudflare Access, or DASHBOARD_KEY
//                      as /dashboard?key=... . A radio not heard from is "not seen", never "broken".
//   GET  /dashboard/radio/<short_id|box_id>   one radio and its last 60 days; POST .../forget deletes it.
//   GET  /v1/radios    the table's rows as JSON (same gate as the dashboard).
//   cron (wrangler.toml [triggers], daily 06:00 UTC): yesterday's anonymous fleet roll-up as
//                      fleet:<date> (counts only, no box ids; kept 400 days), records not seen for
//                      90 days deleted, and the quiet / YouTube / trend alerts.
//   POST /v1/report, POST /v1/github, GET /v1/replies, POST /v1/replies/ack:
//                      "Send to the makers" (support.js; SUPPORT.md).
//
// Bindings (wrangler.toml): CHECKINS (KV). Vars/secrets: CHECKIN_TOKEN (optional, the shared word),
// DASHBOARD_KEY (optional), DASHBOARD_URL (the alerts' links), ALERTS ("off" mutes them).
// Support (optional): SUPPORT (KV), GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET, SUPPORT_REPO,
// ENROL_PER_HOUR, REPORTS_ALL_PER_DAY (enrol.js; defaults 30 and 200). README.md has the steps.

import { handleReport, handleGithub, handleReplies, issuesFor, repoOf } from "./support.js";
import { handleEnrol, authFor } from "./enrol.js";
import { normalize, dayRow, withDay } from "./model.js";
import { rollup } from "./fleet.js";
import { onCheckin, onCron, forgetAlerts } from "./alerts.js";
import { renderDashboard, renderRadio, rowOf, sortRows, disambiguate } from "./dashboard.js";
import { versionsByChannel, currentByChannel } from "./fleet.js";

export { rowOf, sortRows, renderDashboard, renderRadio };

const MAX_BODY = 2048;
const BOX_RE = /^[A-Za-z0-9._-]{1,64}$/;
export const FORGET_AFTER_DAYS = 90;          // a record not seen for this long goes (the cron's sweep)
export const FLEET_KEEP_DAYS = 400;           // fleet:<date> roll-ups
export const FLEET_PREFIX = "fleet:";
export const SCHEMAS = [1, 2];

// --- validation (exported for the test) ---------------------------------------------------------

export function validate(text) {
  if (typeof text !== "string" || text.length === 0) return { error: "empty body" };
  if (text.length > MAX_BODY) return { error: `body larger than ${MAX_BODY} bytes` };
  let doc;
  try { doc = JSON.parse(text); } catch (_) { return { error: "not JSON" }; }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return { error: "not a JSON object" };
  const id = doc.box_id;
  if (typeof id !== "string" || !BOX_RE.test(id)) return { error: "box_id must be a short plain string" };
  if (doc.schema !== undefined && !SCHEMAS.includes(doc.schema)) return { error: "unknown schema" };
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

// --- the record ---------------------------------------------------------------------------------

/** The new record for a check-in: the payload, when, and today's row in the 60-day history. */
export function recordFor(prev, payload, now) {
  const d = new Date(now).toISOString().slice(0, 10);
  const days = (prev && Array.isArray(prev.days)) ? prev.days : [];
  const before = days.find(r => r && r.d < d) || null;
  return { payload, last_seen: new Date(now).toISOString().slice(0, 16) + "Z", days: withDay(days, dayRow(normalize(payload), d, before)) };
}

export async function listRecords(kv) {
  const out = [];
  let cursor;
  do {
    const page = await kv.list({ cursor, limit: 1000 });
    for (const k of page.keys) {
      if (k.name.startsWith(FLEET_PREFIX)) continue;      // the roll-ups share the namespace
      const rec = await kv.get(k.name, "json");
      if (rec) out.push({ id: k.name, rec });
    }
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return out;
}

async function findRecord(kv, key) {
  if (BOX_RE.test(key) && key.length > 8) {
    const rec = await kv.get(key, "json");
    if (rec) return { id: key, rec };
  }
  const all = await listRecords(kv);
  return all.find(r => r.id === key) || all.find(r => ((r.rec.payload || {}).short_id || r.id.slice(0, 4)) === key) || null;
}

async function issuesMap(env, records) {
  const out = {};
  for (const { id } of records) out[id] = await issuesFor(env, id);
  return out;
}

const html = (body, status = 200) => new Response(body, { status, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
const later = (ctx, work) => {
  const p = Promise.resolve(work).catch(e => console.log("after the answer:", e && e.message));
  if (ctx && typeof ctx.waitUntil === "function") { ctx.waitUntil(p); return null; }
  return p;
};

// --- the cron -----------------------------------------------------------------------------------

/** Yesterday's roll-up, the 90-day sweep, the cron alerts. Writes: 1 + one per forgotten radio + alerts. */
export async function daily(env, now) {
  const records = await listRecords(env.CHECKINS);
  const yesterday = new Date(now - 86400000).toISOString().slice(0, 10);
  const summary = rollup(records, yesterday);
  await env.CHECKINS.put(FLEET_PREFIX + yesterday, JSON.stringify(summary), { expirationTtl: FLEET_KEEP_DAYS * 86400 });
  const kept = [], forgotten = [];
  for (const r of records) {
    const seen = Date.parse(r.rec.last_seen);
    if (!Number.isFinite(seen) || now - seen > FORGET_AFTER_DAYS * 86400000) {
      await env.CHECKINS.delete(r.id);
      await forgetAlerts(env, r.id, `Not seen for ${FORGET_AFTER_DAYS} days: the server forgot this radio.`);
      forgotten.push(r.id.slice(0, 8));
    } else kept.push(r);
  }
  const alerts = await onCron(env, kept, now);
  return { rollup: yesterday, radios_seen: summary.radios_seen, forgotten, alerts };
}

// --- the worker ----------------------------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const now = Date.now();
    const q = url.searchParams.get("key") ? `?key=${encodeURIComponent(url.searchParams.get("key"))}` : "";
    if (request.method === "POST" && url.pathname === "/v1/checkin") {
      const length = Number(request.headers.get("content-length") || 0);
      if (length > MAX_BODY) return new Response("too large", { status: 413 });
      const text = await request.text();
      const v = validate(text);
      if (v.error) return new Response(v.error, { status: 400 });
      if (!(await authFor(request, env, v.id)).ok) return new Response("no", { status: 403 });   // the shared word, or the radio's own
      // Stored: the payload, when, and the daily rows. Not stored, not read: the IP, the headers.
      const prev = await env.CHECKINS.get(v.id, "json");
      await env.CHECKINS.put(v.id, JSON.stringify(recordFor(prev, v.doc, now)));
      const work = later(ctx, onCheckin(env, v.id, v.doc, now));
      if (work) await work;
      return new Response(null, { status: 204 });
    }
    if (request.method === "POST" && url.pathname === "/v1/forget") {
      const text = await request.text();
      let doc = null;
      try { doc = JSON.parse(text); } catch (_) { doc = null; }
      const id = doc && doc.box_id;
      if (typeof id !== "string" || !BOX_RE.test(id)) return new Response("box_id must be a short plain string", { status: 400 });
      if (!(await authFor(request, env, id)).ok) return new Response("no", { status: 403 });
      await env.CHECKINS.delete(id);
      const work = later(ctx, forgetAlerts(env, id, "The owner switched the check-in off: the server forgot this radio."));
      if (work) await work;
      return new Response(null, { status: 204 });
    }
    if (url.pathname === "/dashboard" || url.pathname === "/v1/radios" || url.pathname.startsWith("/dashboard/radio/")) {
      if (!dashboardOk(url, request, env)) return new Response("no", { status: 403 });
      const repo = env.SUPPORT ? repoOf(env) : "";
      const m = /^\/dashboard\/radio\/([^/]{1,64})(\/forget)?$/.exec(url.pathname);
      if (m) {
        const found = await findRecord(env.CHECKINS, decodeURIComponent(m[1]));
        if (!found) return html("<!doctype html><p>No such radio here.</p>", 404);
        if (m[2]) {
          if (request.method !== "POST") return new Response("POST only", { status: 405 });
          await env.CHECKINS.delete(found.id);
          await forgetAlerts(env, found.id, "Forgotten from the dashboard (a returned radio).");
          return new Response(null, { status: 303, headers: { location: `/dashboard${q}` } });
        }
        if (request.method !== "GET") return new Response("GET only", { status: 405 });
        return html(renderRadio({ ...found, now, repo, issues: await issuesFor(env, found.id), q }));
      }
      if (request.method !== "GET") return new Response("GET only", { status: 405 });
      const records = await listRecords(env.CHECKINS);
      const issues = await issuesMap(env, records);
      if (url.pathname === "/v1/radios") {
        const current = currentByChannel(versionsByChannel(records, now));
        const rows = disambiguate(sortRows(records.map(({ id, rec }) => rowOf(id, rec, now, { issues: issues[id], current }))));
        return new Response(JSON.stringify(rows, null, 1), { headers: { "content-type": "application/json; charset=utf-8" } });
      }
      return html(renderDashboard({ records, now, repo, issues, q }));
    }
    if (request.method === "POST" && url.pathname === "/v1/enrol") {
      return handleEnrol(request, env, now);    // a radio asks for its own token (enrol.js)
    }
    if (request.method === "POST" && url.pathname === "/v1/report") {
      return handleReport(request, env, now);   // checks the radio's word itself, once it knows the radio
    }
    if (request.method === "POST" && url.pathname === "/v1/github") {
      return handleGithub(request, env);        // GitHub signs it; the radios' token isn't GitHub's
    }
    if ((request.method === "GET" && url.pathname === "/v1/replies") || (request.method === "POST" && url.pathname === "/v1/replies/ack")) {
      return handleReplies(request, env, url);
    }
    if (request.method === "GET" && url.pathname === "/") {
      return new Response("hasler check-in: POST /v1/checkin, /v1/forget, /v1/enrol, /v1/report\n", { headers: { "content-type": "text/plain" } });
    }
    return new Response("not found", { status: 404 });
  },

  async scheduled(event, env, ctx) {
    const work = daily(env, event && event.scheduledTime ? event.scheduledTime : Date.now())
      .then(s => console.log("daily:", JSON.stringify(s)));
    if (ctx && typeof ctx.waitUntil === "function") ctx.waitUntil(work);
    else await work;
  },
};
