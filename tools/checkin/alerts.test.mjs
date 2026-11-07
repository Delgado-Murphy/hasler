// node --test tools/checkin/ - alerts as GitHub issues: one per radio and kind, opened, commented
// only on a change, closed when cleared. A mocked GitHub; no network.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import worker from "./worker.js";
import { onCheckin, onCron, alertKey } from "./alerts.js";
import { FakeKV, v2 } from "./fixtures.mjs";

const BOX = "0f3a7b1e-1111-4222-8333-444455556666";
const NOW = Date.parse("2026-11-07T16:01:00Z");
let calls, next, realFetch;
beforeEach(() => {
  calls = []; next = 40; realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ url: String(url).replace("https://api.github.com/repos/Delgado-Murphy/hasler-support", ""), method: init.method, body });
    if (String(url).endsWith("/issues") && init.method === "POST") return new Response(JSON.stringify({ number: next++ }), { status: 201 });
    return new Response("{}", { status: 200 });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const env = (over = {}) => ({ CHECKINS: new FakeKV(), SUPPORT: new FakeKV(), GITHUB_TOKEN: "ghp_test", GITHUB_WEBHOOK_SECRET: "hook", ...over });
const sick = (words, over = {}) => v2({ faults: { now: words, day: words }, ...over });

test("a fault opens one issue, says nothing while unchanged, comments on a new word, closes after 2 clean check-ins", async () => {
  const e = env();
  let r = await onCheckin(e, BOX, sick(["storage"]), NOW);
  assert.equal(r.fault.state, "opened");
  assert.equal(calls.length, 1);
  const issue = calls[0];
  assert.equal(issue.url, "/issues"); assert.equal(issue.method, "POST");
  assert.equal(issue.body.title, "6bb6 · card full (storage) · 1.6.2");
  assert.deepEqual(issue.body.labels, ["alert", "alert:fault", "version:1.6.2", "model:hasler-hm-hat", "bench"]);
  assert.ok(issue.body.body.includes("https://hasler-checkin.delgado-murphy.workers.dev/dashboard/radio/6bb6"));
  assert.ok(issue.body.body.includes(`<!-- hasler-alert: ${BOX} fault -->`) && !issue.body.body.includes("hasler-box"));
  assert.ok(issue.body.body.includes('"schema": 2'));
  const writes = e.SUPPORT.writes;
  r = await onCheckin(e, BOX, sick(["storage"]), NOW + 86400000);                 // the same: silence, no write
  assert.equal(calls.length, 1); assert.equal(e.SUPPORT.writes, writes); assert.equal(r.fault.changed, false);
  r = await onCheckin(e, BOX, sick(["storage", "heat"]), NOW + 2 * 86400000);     // a new word: one comment
  assert.equal(calls.length, 2); assert.equal(calls[1].url, "/issues/40/comments"); assert.ok(calls[1].body.body.includes("too hot (heat)"));
  r = await onCheckin(e, BOX, v2(), NOW + 3 * 86400000);
  assert.equal(r.fault.state, "clearing"); assert.equal(calls.length, 2);
  r = await onCheckin(e, BOX, sick(["heat"]), NOW + 4 * 86400000);                 // back before it closed: the count restarts
  r = await onCheckin(e, BOX, v2(), NOW + 5 * 86400000);
  assert.equal(r.fault.state, "clearing");
  r = await onCheckin(e, BOX, v2(), NOW + 6 * 86400000);
  assert.equal(r.fault.state, "closed");
  assert.deepEqual(calls.slice(-2).map(c => [c.method, c.url]), [["POST", "/issues/40/comments"], ["PATCH", "/issues/40"]]);
  assert.equal(calls.at(-1).body.state, "closed");
  assert.equal(await e.SUPPORT.get(alertKey(BOX, "fault")), null);
  r = await onCheckin(e, BOX, sick(["sound"], { health: { ok: false, failed: ["audio_card"] }, channel: "stable" }), NOW + 7 * 86400000);
  assert.equal(r.fault.number, 41);
  assert.equal(calls.at(-1).body.title, "6bb6 · sound card not answering (check audio_card), no sound card (sound) · 1.6.2");
  assert.ok(!calls.at(-1).body.labels.includes("bench"));
});

test("a rollback opens once per failed version and closes by hand (the webhook clears its key)", async () => {
  const e = env();
  const rolled = v2({ version: "1.6.1", release: { state: "rolled_back", version: "1.6.2", from: "1.6.1", reason: "check panel failed (absent)" } });
  let r = await onCheckin(e, BOX, rolled, NOW);
  assert.equal(r.rollback.state, "opened");
  const issue = calls[0].body;
  assert.equal(issue.title, "6bb6 · went back from 1.6.2 to 1.6.1 · 1.6.2");
  assert.ok(issue.body.includes("the keys weren't answering after the switch") && issue.labels.includes("version:1.6.2"));
  await onCheckin(e, BOX, rolled, NOW + 86400000);
  assert.equal(calls.length, 1);
  const body = JSON.stringify({ action: "closed", repository: { full_name: "Delgado-Murphy/hasler-support" }, sender: { type: "User" },
    issue: { number: 40, body: issue.body } });
  const res = await worker.fetch(new Request("https://x/v1/github", { method: "POST", body, headers: { "x-github-event": "issues",
    "x-hub-signature-256": "sha256=" + createHmac("sha256", "hook").update(body).digest("hex") } }), e);
  assert.equal((await res.json()).why, "an alert, closed");
  assert.equal(await e.SUPPORT.get(alertKey(BOX, "rollback")), null);
  assert.equal(await e.SUPPORT.get("replies:" + BOX), null);                       // nothing goes to the owner
});

test("the cron: a radio gone quiet opens an issue that its next check-in closes; YouTube broken across the fleet; a trend", async () => {
  const e = env();
  const rec = (id, days, lastAgo, over = {}) => ({ id, rec: { payload: v2({ box_id: id, short_id: id.slice(0, 4), ...over }),
    last_seen: new Date(NOW - lastAgo * 86400000).toISOString(),
    days: Array.from({ length: days }, (_, i) => ({ d: new Date(NOW - (lastAgo + i) * 86400000).toISOString().slice(0, 10), v: over.version || "1.6.2",
      ch: "b", h: 1, f: [], s: over.s ?? 0, r: 0, dr: 0, y: 1, t: 50, uv: 0, pc: 0, st: "ok" })) } });
  const records = [rec("quie-box", 7, 3.2), rec("fine-box", 7, 0.1), rec("thin-box", 3, 5)];
  let out = await onCron(e, records, NOW);
  assert.deepEqual(out.quiet, ["quie"]);                                            // thin-box was never regular
  assert.ok(calls[0].body.title.startsWith("quie · gone quiet · last seen"));
  out = await onCron(e, records, NOW + 86400000);
  assert.deepEqual(out.quiet, []);                                                  // still open: no new issue, no comment
  assert.equal(calls.length, 1);
  const r = await onCheckin(e, "quie-box", v2({ short_id: "quie" }), NOW + 2 * 86400000);
  assert.equal(r.quiet.state, "closed");
  // YouTube Music: 2 of 3 broken today
  calls = [];
  const yt = [rec("a-box", 1, 0.1, { ytdlp: { working: false, version: "2026.10.21" } }), rec("b-box", 1, 0.1, { ytdlp: { working: false, version: "2026.10.21" } }),
    rec("c-box", 1, 0.1)];
  out = await onCron(e, yt, NOW);
  assert.equal(out.youtube.state, "opened");
  assert.equal(calls[0].body.title, "YouTube Music helper broken on 2 of 3 radios");
  out = await onCron(e, [rec("c-box", 1, 0.1)], NOW + 86400000);
  assert.equal(out.youtube.state, "closed");
  // a release trend: 1.6.2 stalls 3/day against 1.6.0's 0.25
  calls = [];
  const trend = [rec("n1-box", 6, 0.1, { s: 3 }), rec("n2-box", 6, 0.1, { s: 3 }),
    rec("o1-box", 12, 0.1, { version: "1.6.0", s: 0 }), rec("o2-box", 12, 0.1, { version: "1.6.0", s: 1 })];
  out = await onCron(e, trend, NOW);
  assert.deepEqual(out.trend, ["1.6.2"]);
  assert.equal(calls.find(c => c.method === "POST" && c.url === "/issues" && /stalls/.test(c.body.title)).body.title, "1.6.2 · stalls up against 1.6.0");
});

test("muted: ALERTS=off or no GitHub token means no issue at all; a check-in still answers 204", async () => {
  for (const e of [env({ ALERTS: "off" }), env({ GITHUB_TOKEN: "" })]) {
    calls = [];
    assert.equal(await onCheckin(e, BOX, sick(["storage"]), NOW), null);
    const body = JSON.stringify(sick(["storage"]));
    assert.equal((await worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body }), e)).status, 204);
    assert.equal(calls.length, 0);
  }
});

test("a check-in through the Worker raises the alert; forgetting the radio closes it", async () => {
  const e = env();
  const body = JSON.stringify(sick(["storage"]));
  assert.equal((await worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body }), e)).status, 204);
  assert.equal(calls.length, 1);
  assert.equal((await worker.fetch(new Request("https://x/v1/forget", { method: "POST", body: JSON.stringify({ box_id: BOX }) }), e)).status, 204);
  assert.deepEqual(calls.slice(1).map(c => [c.method, c.url]), [["POST", "/issues/40/comments"], ["PATCH", "/issues/40"]]);
  assert.equal(await e.SUPPORT.get(alertKey(BOX, "fault")), null);
});
