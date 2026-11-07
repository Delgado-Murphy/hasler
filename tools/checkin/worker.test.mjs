// node --test tools/checkin/ - the Worker's validation, the record and its 60-day history, schema 1
// and 2 in one shape, the fleet's numbers, the daily cron, and the dashboard's pages, with a fake
// KV. No Cloudflare, no network.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { validate, recordFor, tokenOk, daily, FLEET_PREFIX } from "./worker.js";
import { normalize, dayRow, withDay, lookReasons, piShort, platformOf, rollbackWords, HISTORY_DAYS } from "./model.js";
import { releaseHealth, versionsByChannel, currentByChannel, behind, ytdlpFleet, rollup } from "./fleet.js";
import { renderDashboard, renderRadio, rowOf, sortRows, seenWords } from "./dashboard.js";
import { FakeKV, BOX, v1, v2 } from "./fixtures.mjs";

const NOW = Date.parse("2026-11-07T16:01:00Z");
const ago = d => new Date(NOW - d * 86400000).toISOString();


const post = (env, doc, headers = {}) => {
  const body = JSON.stringify(doc);
  return worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body,
    headers: { "content-type": "application/json", "cf-connecting-ip": "203.0.113.9", "content-length": String(body.length), ...headers } }), env);
};

test("validate: only a small JSON object with a plain box_id, schema 1 or 2", () => {
  assert.equal(validate("").error, "empty body");
  assert.equal(validate("x".repeat(3000)).error, "body larger than 2048 bytes");
  assert.equal(validate("{nope").error, "not JSON");
  assert.equal(validate("[1]").error, "not a JSON object");
  assert.equal(validate(JSON.stringify({ box_id: "../etc" })).error, "box_id must be a short plain string");
  assert.equal(validate(JSON.stringify({ box_id: "a", schema: 3 })).error, "unknown schema");
  assert.equal(validate(JSON.stringify(v1())).id, BOX);
  assert.equal(validate(JSON.stringify(v2())).id, BOX);
  assert.ok(JSON.stringify(v2()).length < 1000);
});

test("the token header gates POSTs only when CHECKIN_TOKEN is set", () => {
  const req = h => ({ headers: { get: name => (name === "X-Hasler-Token" ? h : null) } });
  assert.equal(tokenOk(req(null), {}), true);
  assert.equal(tokenOk(req(null), { CHECKIN_TOKEN: "s" }), false);
  assert.equal(tokenOk(req("s"), { CHECKIN_TOKEN: "s" }), true);
});

test("schema 1 reads as schema 2: the yt-dlp boolean (B1), the release, the throttle word", () => {
  const n = normalize(v1());
  assert.deepEqual(n.ytdlp, { working: true, version: null, failures_24h: null });
  assert.equal(normalize(v1({ ytdlp: false })).ytdlp.working, false);          // broken, not "not on this radio"
  assert.equal(normalize(v1({ ytdlp: null })).ytdlp, null);
  assert.equal(normalize(v1({ ytdlp: { working: true } })).ytdlp.working, true);
  assert.deepEqual(n.release, { state: "probation", version: "1.6.2", from: null, reason: null });
  const rolled = normalize(v1({ last_result: { ok: false, version: "1.4.1", back_to: "1.4.0", reason: "check panel failed (absent)" } }));
  assert.deepEqual(rolled.release, { state: "rolled_back", version: "1.4.1", from: "1.4.0", reason: "check panel failed (absent)" });
  assert.equal(rollbackWords(rolled.release.reason), "the keys weren't answering after the switch");
  const thr = normalize(v1({ throttled: "0x50005" })).power;
  assert.equal(thr.undervolt_now, true); assert.equal(thr.undervolt_24h, true); assert.equal(thr.throttled_24h, true);
  assert.equal(normalize(v1({ storage_bad: true })).storage.state, "full");
  assert.equal(n.uptime_d, 0); assert.equal(n.temp_c.now, 56); assert.equal(n.hw, null);
  const m = normalize(v2());
  assert.equal(m.release.state, "probation"); assert.equal(m.os_updates.state, "deferred");
  assert.equal(platformOf(m), "Pi 4B · 8 GB · moOde 10.3.7"); assert.equal(platformOf(n), null);
  assert.equal(piShort("Raspberry Pi 3 Model B Plus Rev 1.3"), "Pi 3B+"); assert.equal(piShort("Raspberry Pi 5 Model B Rev 1.0"), "Pi 5B");
});

test("POST keeps {payload, last_seen to the minute, days} and nothing about the client; one write per check-in", async () => {
  const env = { CHECKINS: new FakeKV() };
  const res = await post(env, v2());
  assert.equal(res.status, 204);
  const stored = JSON.parse(env.CHECKINS.m.get(BOX));
  assert.deepEqual(Object.keys(stored).sort(), ["days", "last_seen", "payload"]);
  assert.match(stored.last_seen, /^\d{4}-\d\d-\d\dT\d\d:\d\dZ$/);
  assert.ok(!JSON.stringify(stored).includes("203.0.113.9"));
  assert.equal(stored.days.length, 1);
  assert.deepEqual(Object.keys(stored.days[0]).filter(k => /t$|at|time/.test(k) && k !== "t" && k !== "st"), []);   // dates only
  assert.equal(env.CHECKINS.writes, 1);
  assert.equal((await post(env, v1())).status, 204);                                // schema 1 still welcome
  assert.equal((await worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body: "{", headers: { "content-length": "1" } }), env)).status, 400);
  assert.equal((await worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body: "{}", headers: { "content-length": "9999" } }), env)).status, 413);
  assert.equal((await post({ ...env, CHECKIN_TOKEN: "w" }, v2())).status, 403);
  assert.equal((await post({ ...env, CHECKIN_TOKEN: "w" }, v2(), { "x-hasler-token": "w" })).status, 204);
});

test("history: one row per UTC date, a second check-in that day replaces it, newest first, 60 at most", () => {
  let rec = null;
  for (let i = 70; i >= 0; i--) rec = recordFor(rec, v2({ player: { stalls_24h: i, restarts_24h: 0, daemon_restarts_24h: 0 } }), NOW - i * 86400000);
  assert.equal(rec.days.length, HISTORY_DAYS);
  assert.equal(rec.days[0].d, "2026-11-07"); assert.equal(rec.days[0].s, 0); assert.equal(rec.days[59].d, "2026-09-09");
  rec = recordFor(rec, v2({ player: { stalls_24h: 9, restarts_24h: 1, daemon_restarts_24h: 0 } }), NOW + 3600000);
  assert.equal(rec.days.length, HISTORY_DAYS); assert.equal(rec.days[0].s, 9);
  const row = dayRow(normalize(v2({ faults: { now: [], day: ["heat"] }, release: { state: "rolled_back", version: "1.6.2", from: "1.6.1", reason: "x" } })), "2026-11-07", null);
  assert.deepEqual(row, { d: "2026-11-07", v: "1.6.2", ch: "b", rs: "r", h: 1, f: ["heat"], s: 0, r: 0, dr: 0, y: 1, t: 61, uv: 0, pc: 0, st: "ok", rb: "1.6.2", c30: 0, yv: "2026.10.21" });
  assert.ok(JSON.stringify(rec).length < 12000, JSON.stringify(rec).length);       // ~7 KB a radio
  const cut = dayRow(normalize(v2({ power: { last_off: "power-cut", cuts_30d: 1 } })), "2026-11-08", row);
  assert.equal(cut.pc, 1);
  assert.deepEqual(withDay([{ d: "2026-11-01" }], { d: "2026-11-03" }).map(r => r.d), ["2026-11-03", "2026-11-01"]);
});

test("POST /v1/forget deletes the radio's record with the same token rule", async () => {
  const env = { CHECKINS: new FakeKV(), CHECKIN_TOKEN: "w" };
  await post(env, v2(), { "x-hasler-token": "w" });
  const forget = (h = {}) => worker.fetch(new Request("https://x/v1/forget", { method: "POST", body: JSON.stringify({ schema: 1, box_id: BOX }), headers: h }), env);
  assert.equal((await forget()).status, 403);
  assert.ok(env.CHECKINS.m.has(BOX));
  assert.equal((await forget({ "x-hasler-token": "w" })).status, 204);
  assert.ok(!env.CHECKINS.m.has(BOX));
  assert.equal((await worker.fetch(new Request("https://x/v1/forget", { method: "POST", body: "{}" }), env)).status, 400);
});

// --- the fleet ------------------------------------------------------------------------------------

function fleet() {
  // 12 radios on stable 1.6.0, 3 on bench 1.6.2 with more stalls, one rolled back, one old, one quiet.
  const records = [];
  const add = (id, mk, daysBack = 20, seenAgo = 0.1) => {
    let rec = null;
    for (let i = daysBack; i >= Math.ceil(seenAgo); i--) rec = recordFor(rec, mk(i), NOW - i * 86400000);
    rec.last_seen = ago(seenAgo).slice(0, 16) + "Z";
    records.push({ id, rec });
  };
  for (let k = 0; k < 12; k++) {
    add(`stable-${k}`, i => v2({ box_id: `stable-${k}`, short_id: `s${String(k).padStart(3, "0")}`, channel: "stable", version: "1.6.0",
      release: { state: "committed", version: "1.6.0", from: "1.5.4", reason: null },
      player: { stalls_24h: i % 5 === 0 ? 1 : 0, restarts_24h: 0, daemon_restarts_24h: 0 },
      hw: { model: "Raspberry Pi 4 Model B Rev 1.4", code: "c03114", ram_gb: 4 }, os: { debian: "13.7", kernel: "6.12", moode: "10.3.0" } }));
  }
  for (let k = 0; k < 3; k++) {
    add(`bench-${k}`, () => v2({ box_id: `bench-${k}`, short_id: `b00${k}`, player: { stalls_24h: 3, restarts_24h: 1, daemon_restarts_24h: 0 } }), 6);
  }
  add("rolled", () => v2({ box_id: "rolled", short_id: "r0ll", channel: "stable", version: "1.6.0",
    release: { state: "rolled_back", version: "1.6.2", from: "1.6.0", reason: "check panel failed (absent)" } }), 3);
  add("old", () => v1({ box_id: "old", short_id: "o1d0", channel: "stable", version: "1.5.2", last_result: null, ytdlp: false }), 10);
  add("quiet", () => v2({ box_id: "quiet", short_id: "qu1e", channel: "stable", version: "1.6.0" }), 12, 4);
  return records;
}

test("release health: per version, rollbacks, faults per 100 radio-days, stalls up against the version before", () => {
  const rows = releaseHealth(fleet(), NOW);
  assert.deepEqual(rows.map(r => r.version), ["1.6.2", "1.6.1", "1.6.0", "1.5.2"].filter(v => rows.some(r => r.version === v)));
  const r162 = rows.find(r => r.version === "1.6.2");
  assert.equal(r162.radios, 3); assert.equal(r162.rollbacks, 1); assert.equal(r162.stallsPerDay, 3);
  assert.ok(r162.radioDays >= 10 && r162.rise.includes("stalls") && r162.rise.includes("restarts"), JSON.stringify(r162));
  assert.ok(r162.daily.length >= 5);
  const r160 = rows.find(r => r.version === "1.6.0");
  assert.equal(r160.rollbacks, 0); assert.equal(r160.rise.length, 0);
});

test("versions per channel, the behind rule, the YouTube helper across radios, the roll-up has no box ids", () => {
  const recs = fleet();
  const by = versionsByChannel(recs, NOW);
  assert.deepEqual(by.bench.map(e => [e.version, e.n, e.states]), [["1.6.2", 3, { probation: 3 }]]);
  assert.deepEqual(by.stable.map(e => e.version), ["1.6.0", "1.5.2"]);
  const cur = currentByChannel(by);
  assert.equal(behind("1.5.2", cur.stable), false);         // current-minus-one: not amber yet
  assert.equal(behind("1.5.2", { current: "1.6.2", previous: "1.6.0" }), true);
  const y = ytdlpFleet(recs, NOW);
  assert.equal(y.broken, 1); assert.equal(y.alarm, false);
  const r = rollup(recs, "2026-11-06");
  assert.ok(r.radios_seen >= 16 && r.by_channel.stable["1.6.0"] >= 13 && r.by_version["1.6.2"].rollbacks === 1);
  assert.equal(r.platform["Pi 4B · 4 GB · moOde 10.3.0"], 12);
  const blob = JSON.stringify(r);
  for (const id of ["stable-1", "bench-0", "rolled", "6bb6", "s001"]) assert.ok(!blob.includes(id), id);
});

test("the daily cron: yesterday's roll-up kept 400 days, records unseen for 90 days forgotten", async () => {
  const env = { CHECKINS: new FakeKV() };
  for (const { id, rec } of fleet()) await env.CHECKINS.put(id, JSON.stringify(rec));
  await env.CHECKINS.put("ancient", JSON.stringify({ payload: v1({ box_id: "ancient" }), last_seen: ago(91), days: [] }));
  await worker.scheduled({ scheduledTime: NOW }, env);
  const key = FLEET_PREFIX + "2026-11-06";
  assert.ok(env.CHECKINS.m.has(key)); assert.equal(env.CHECKINS.ttl.get(key), 400 * 86400);
  assert.ok(!env.CHECKINS.m.has("ancient") && env.CHECKINS.m.has("quiet"));
  const page = await (await worker.fetch(new Request("https://x/dashboard"), env)).text();     // the roll-up isn't a radio
  assert.ok(!page.includes("fleet:"));
  const s = await daily(env, NOW);
  assert.equal(s.rollup, "2026-11-06"); assert.deepEqual(s.forgotten, []);
});

// --- the pages ------------------------------------------------------------------------------------

test("rows: needs a look first, then the longest unseen; the reasons in words", () => {
  const rows = sortRows(fleet().map(({ id, rec }) => rowOf(id, rec, NOW)));
  assert.deepEqual(rows.slice(0, 3).map(r => r.short).sort(), ["o1d0", "qu1e", "r0ll"]);
  const quiet = rows.find(r => r.short === "qu1e");
  assert.deepEqual(quiet.look, ["quiet"]);
  assert.deepEqual(rows.find(r => r.short === "o1d0").look, ["youtube"]);
  assert.deepEqual(lookReasons(normalize(v2({ temp_c: { now: 60, max_24h: 82 } })), 0), ["heat"]);
  assert.deepEqual(lookReasons(normalize(v2()), 20), []);                        // grey, not "needs a look"
});

test("times are Madrid's, the UTC time in the title", () => {
  assert.equal(seenWords(Date.parse("2026-11-07T15:12:00Z"), NOW), "today 16:12");
  assert.equal(seenWords(Date.parse("2026-11-06T23:30:00Z"), NOW), "today 00:30");      // already the 7th in Madrid
  assert.equal(seenWords(Date.parse("2026-11-06T10:00:00Z"), NOW), "yesterday 11:00");
  assert.equal(seenWords(Date.parse("2026-11-03T10:00:00Z"), NOW), "4 days ago");
  assert.equal(seenWords(NaN, NOW), "not seen");
});

test("the dashboard: verdict line, four panels, human labels, red only for faults", () => {
  const recs = fleet();
  const html = renderDashboard({ records: recs, now: NOW, repo: "Delgado-Murphy/hasler-support",
    issues: { "bench-0": [{ number: 12, at: ago(3), state: "open" }, { number: 9, at: ago(9), state: "done" }] } });
  assert.match(html, /<b>18 radios<\/b> have checked in · 17 seen today · <b class="warn">3 need a look<\/b> · 1 open request/);
  for (const h of ["Versions", "Release health", "YouTube Music helper", "Platform"]) assert.ok(html.includes(`<h2>${h}`), h);
  assert.ok(html.includes("Hasler HM (HAT)") && html.includes("Pi 4B · 4 GB · moOde 10.3.0"));
  assert.ok(html.includes('<span class="fault">went back to 1.6.0</span>'));
  assert.ok(html.includes('<span class="fault">broken</span>'));                   // a schema-1 radio's false (B1)
  assert.ok(html.includes('<span class="warn"><a href="https://github.com/Delgado-Murphy/hasler-support/issues/12">#12 open</a></span>') && !html.includes("#9 done"));
  assert.ok(html.includes("updated 17:01 Madrid") && html.includes('title="2026-11-07 16:01 UTC"'));
  assert.ok(html.includes("DELGADO <em>&amp;</em> MURPHY"));
  assert.ok(!/class="fault"[^>]*>[^<]*(probation|behind|low voltage)/.test(html));   // amber things are never red
  const one = renderDashboard({ records: [recs[0]], now: NOW });
  assert.match(one, /<b>1 radio<\/b> has checked in/);
  assert.ok(renderDashboard({ records: [], now: NOW }).includes("No radio has checked in yet."));
});

test("the radio's page: header, now, the 60-day grid, raw payload, Forget; GET and POST through the Worker", async () => {
  const env = { CHECKINS: new FakeKV(), DASHBOARD_KEY: "k" };
  for (const { id, rec } of fleet()) await env.CHECKINS.put(id, JSON.stringify(rec));
  await post(env, v2({ faults: { now: ["storage"], day: ["storage", "heat"] }, storage: { free_mb: 9, total_gb: 59, state: "full" } }));
  const res = await worker.fetch(new Request("https://x/dashboard/radio/6bb6?key=k"), env);
  assert.equal(res.status, 200);
  const html = await res.text();
  assert.ok(html.includes("Hasler HM (HAT) · Bench · auto · Pi 4 Model B Rev 1.5 · 8 GB · Debian 13.7 · kernel 6.18.50 · moOde 10.3.7"));
  assert.ok(html.includes('<span class="fault">card full, too hot</span>') && html.includes("Last 1 day"));
  assert.ok(html.includes("&quot;schema&quot;: 2") && html.includes("Forget this radio") && html.includes("/forget?key=k"));
  const grid = renderRadio({ id: "bench-0", rec: fleet().find(r => r.id === "bench-0").rec, now: NOW });
  assert.match(grid, /Last 6 days/); assert.ok(grid.includes("█ 3"));        // stalls as a block bar
  assert.equal((await worker.fetch(new Request("https://x/dashboard/radio/nope?key=k"), env)).status, 404);
  assert.equal((await worker.fetch(new Request("https://x/dashboard/radio/6bb6"), env)).status, 403);
  const gone = await worker.fetch(new Request(`https://x/dashboard/radio/${BOX}/forget?key=k`, { method: "POST" }), env);
  assert.equal(gone.status, 303); assert.ok(!env.CHECKINS.m.has(BOX));
  const rows = await (await worker.fetch(new Request("https://x/v1/radios?key=k"), env)).json();
  assert.equal(rows.length, 18); assert.ok(rows[0].needsLook);
});
