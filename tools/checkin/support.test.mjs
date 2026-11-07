// node --test tools/checkin/ - "Send to the makers": report -> a (mocked) GitHub issue, the
// rate limit and the de-dup, the webhook's HMAC, a /owner reply queued and served. No network.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import worker from "./worker.js";
import { validateReport, labelsFor, titleFor, bodyFor, ownerReply, signatureOk, PER_DAY } from "./support.js";

class FakeKV {
  constructor() { this.m = new Map(); this.ttl = new Map(); }
  async put(k, v, opts) { this.m.set(k, v); if (opts && opts.expirationTtl) this.ttl.set(k, opts.expirationTtl); }
  async get(k, type) { const v = this.m.get(k); return v === undefined ? null : type === "json" ? JSON.parse(v) : v; }
  async delete(k) { this.m.delete(k); }
  async list() { return { keys: [...this.m.keys()].map(name => ({ name })), list_complete: true }; }
}

const BOX = "0f3a7b1e-1111-4222-8333-444455556666";
const report = (over = {}) => ({
  schema: 1, box_id: BOX, short_id: "ab12", note: "Since yesterday it goes quiet\nafter a few minutes",
  text: "Hasler · Kitchen · radio ab12 · Hasler HM (hasler-hm-hat)\nVersion 1.5.2 · Stable\n```sneaky```\n",
  json: { radio: { short_id: "ab12", version: "1.5.2", variant: "hasler-hm-hat" },
          lamp: { words: "red, tapping SOS — something needs a maker", faults: ["storage", "Heat!"] } },
  ...over,
});

let calls, nextNumber, realFetch, failWith;
beforeEach(() => {
  calls = []; nextNumber = 12; failWith = null; realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ url: String(url), method: init.method, headers: init.headers, body });
    if (failWith) return new Response("{}", { status: failWith });
    if (String(url).endsWith("/issues")) return new Response(JSON.stringify({ number: nextNumber++, html_url: "x" }), { status: 201 });
    return new Response(JSON.stringify({ id: 1 }), { status: 201 });
  };
});
afterEach(() => { globalThis.fetch = realFetch; });

const env = (over = {}) => ({ CHECKINS: new FakeKV(), SUPPORT: new FakeKV(), GITHUB_TOKEN: "ghp_test", CHECKIN_TOKEN: "word",
  GITHUB_WEBHOOK_SECRET: "hook-secret", ...over });
const send = (e, doc, token = "word") => {
  const body = JSON.stringify(doc);
  return worker.fetch(new Request("https://x/v1/report", { method: "POST", body,
    headers: { "content-type": "application/json", "x-hasler-token": token, "content-length": String(body.length), "cf-connecting-ip": "203.0.113.9" } }), e);
};
const sign = (secret, body) => "sha256=" + createHmac("sha256", secret).update(body).digest("hex");
const hook = (e, payload, { event = "issue_comment", secret = "hook-secret" } = {}) => {
  const body = JSON.stringify(payload);
  return worker.fetch(new Request("https://x/v1/github", { method: "POST", body,
    headers: { "content-type": "application/json", "x-github-event": event, "x-hub-signature-256": sign(secret, body) } }), e);
};
const comment = (number, body, over = {}) => ({ action: "created", repository: { full_name: "Delgado-Murphy/hasler-support" },
  issue: { number, body: "" }, comment: { id: 900 + number, body, created_at: "2026-11-07T10:00:00Z", user: { type: "User" } }, ...over });

test("validate, labels, title and body", () => {
  assert.equal(validateReport("").error, "empty body");
  assert.equal(validateReport("x".repeat(17000)).error, "body larger than 16384 bytes");
  assert.equal(validateReport(JSON.stringify({ ...report(), box_id: "../x" })).error, "box_id must be a short plain string");
  assert.equal(validateReport(JSON.stringify({ ...report(), text: "" })).error, "text missing");
  assert.equal(validateReport(JSON.stringify(report())).id, BOX);
  assert.deepEqual(labelsFor(report()), ["owner-report", "fault:storage", "fault:heat", "version:1.5.2", "model:hasler-hm-hat"]);
  assert.equal(titleFor(report()), "ab12 · Since yesterday it goes quiet");
  assert.equal(titleFor(report({ note: "" })), "ab12 · red, tapping SOS — something needs a maker");
  const body = bodyFor(report());
  assert.ok(body.includes("```text\nHasler · Kitchen") && body.includes("'''sneaky'''") && body.includes(`<!-- hasler-box: ${BOX} -->`));
  assert.equal(ownerReply("thanks\n/owner  We'll post a new card on Monday.\nBest, B"), "We'll post a new card on Monday. Best, B");
  assert.equal(ownerReply("/ownerx no"), null);
  assert.equal(ownerReply("just us talking"), null);
});

test("POST /v1/report creates an issue in the private repo, remembers which radio, never the IP", async () => {
  const e = env();
  const res = await send(e, report());
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true, number: 12, deduped: false });
  assert.equal(calls.length, 1);
  const c = calls[0];
  assert.equal(c.url, "https://api.github.com/repos/Delgado-Murphy/hasler-support/issues");
  assert.equal(c.method, "POST");
  assert.equal(c.headers.authorization, "Bearer ghp_test");
  assert.ok(c.headers["user-agent"]);
  assert.equal(c.body.title, "ab12 · Since yesterday it goes quiet");
  assert.ok(c.body.labels.includes("owner-report"));
  assert.deepEqual(JSON.parse(e.SUPPORT.m.get("issue:12")), { box_id: BOX, short_id: "ab12" });
  assert.equal(JSON.parse(e.SUPPORT.m.get(`issues:${BOX}`))[0].number, 12);
  assert.ok(![...e.SUPPORT.m.values()].join("").includes("203.0.113.9"));
  assert.equal(e.CHECKINS.m.size, 0, "the dashboard's radios list stays radios only");
  // SUPPORT_REPO moves it; a bad token is refused before anything happens
  const e2 = env({ SUPPORT_REPO: "someone/elsewhere" });
  await send(e2, report());
  assert.equal(calls.at(-1).url, "https://api.github.com/repos/someone/elsewhere/issues");
  assert.equal((await send(env(), report(), "wrong")).status, 403);
  assert.equal((await send(env({ GITHUB_TOKEN: "" }), report())).status, 503);
  assert.equal((await send(env(), { ...report(), box_id: 7 })).status, 400);
});

test("the same note within 10 minutes is a comment on the same issue; labels GitHub refuses are dropped", async () => {
  const e = env();
  await send(e, report());
  const again = await (await send(e, report())).json();
  assert.deepEqual(again, { ok: true, number: 12, deduped: true });
  assert.equal(calls[1].url, "https://api.github.com/repos/Delgado-Murphy/hasler-support/issues/12/comments");
  const other = await (await send(e, report({ note: "Something else" }))).json();
  assert.deepEqual(other, { ok: true, number: 13, deduped: false });
  // a 422 (labels) retries without them
  const e2 = env();
  let first = true;
  const inner = globalThis.fetch;
  globalThis.fetch = async (url, init) => { if (first) { first = false; calls.push({ body: JSON.parse(init.body) }); return new Response("{}", { status: 422 }); } return inner(url, init); };
  const res = await (await send(e2, report())).json();
  assert.equal(res.ok, true);
  assert.equal(calls.at(-1).body.labels, undefined);
  failWith = 500;
  globalThis.fetch = inner;
  assert.equal((await send(env(), report())).status, 502);
});

test(`at most ${PER_DAY} a day per radio`, async () => {
  const e = env();
  for (let i = 0; i < PER_DAY; i++) assert.equal((await send(e, report({ note: `note ${i}` }))).status, 200);
  const res = await send(e, report({ note: "one more" }));
  assert.equal(res.status, 429);
  assert.equal((await res.json()).limit, PER_DAY);
  assert.equal((await send(e, report({ box_id: "another-radio" }))).status, 200, "per box_id");
  const key = [...e.SUPPORT.m.keys()].find(k => k.startsWith(`rate:${BOX}:`));
  assert.equal(e.SUPPORT.ttl.get(key), 2 * 86400);
});

test("the webhook is HMAC-verified; a /owner comment is queued for that issue's radio and served once acked", async () => {
  const e = env();
  await send(e, report());                           // issue 12 is BOX's
  assert.equal(await signatureOk("s", "body", sign("s", "body")), true);
  assert.equal(await signatureOk("s", "body", sign("t", "body")), false);
  assert.equal(await signatureOk("", "body", sign("", "body")), false);
  assert.equal((await hook(e, comment(12, "/owner hi"), { secret: "wrong" })).status, 401);
  assert.equal((await hook(env({ GITHUB_WEBHOOK_SECRET: "" }), comment(12, "/owner hi"))).status, 503);
  assert.equal((await hook(e, { zen: "x" }, { event: "ping" })).status, 204);
  assert.equal((await hook(e, comment(12, "a note between makers"))).status, 204);
  assert.equal((await hook(e, comment(12, "/owner hi", { repository: { full_name: "Delgado-Murphy/hasler" } }))).status, 204);
  assert.equal(e.SUPPORT.m.has(`replies:${BOX}`), false);
  const ok = await hook(e, comment(12, "Thanks!\n/owner A new memory card is in the post. Leave it plugged in."));
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).stored, true);
  await hook(e, comment(12, "Thanks!\n/owner A new memory card is in the post. Leave it plugged in."));   // redelivered
  // an issue KV forgot: the marker in its body still names the radio
  const lost = comment(40, "/owner From the marker", { issue: { number: 40, body: `x\n<!-- hasler-box: ${BOX} -->` } });
  await hook(e, lost);
  const get = () => worker.fetch(new Request(`https://x/v1/replies?box_id=${BOX}`, { headers: { "x-hasler-token": "word" } }), e);
  const { replies } = await (await get()).json();
  assert.deepEqual(replies.map(r => [r.issue, r.text]), [[12, "A new memory card is in the post. Leave it plugged in."], [40, "From the marker"]]);
  assert.equal(replies[0].at, "2026-11-07T10:00:00Z");
  assert.equal((await worker.fetch(new Request(`https://x/v1/replies?box_id=${BOX}`), e)).status, 403);
  const ack = await worker.fetch(new Request("https://x/v1/replies/ack", { method: "POST", headers: { "x-hasler-token": "word" },
    body: JSON.stringify({ box_id: BOX, ids: [replies[0].id] }) }), e);
  assert.equal(ack.status, 204);
  assert.deepEqual((await (await get()).json()).replies.map(r => r.issue), [40]);
  await worker.fetch(new Request("https://x/v1/replies/ack", { method: "POST", headers: { "x-hasler-token": "word" },
    body: JSON.stringify({ box_id: BOX, ids: [replies[1].id] }) }), e);
  assert.deepEqual((await (await get()).json()).replies, []);
});

test("the dashboard links a radio's requests", async () => {
  const e = env({ DASHBOARD_KEY: "" });
  const body = JSON.stringify({ schema: 1, box_id: BOX, short_id: "ab12", version: "1.5.2" });
  await worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body, headers: { "x-hasler-token": "word" } }), e);
  await send(e, report());
  const html = await (await worker.fetch(new Request("https://x/dashboard"), e)).text();
  assert.ok(html.includes('href="https://github.com/Delgado-Murphy/hasler-support/issues/12">#12 open</a>'));
  assert.ok(html.includes("1 open request"));
});

test("the issue's state reaches the radio: seen once, done (folding a reply not yet fetched), reopened", async () => {
  const e = env();
  await send(e, report());                           // issue 12 is BOX's
  const issues = (action, over = {}) => ({ action, repository: { full_name: "Delgado-Murphy/hasler-support" },
    issue: { number: 12, body: "", closed_at: "2026-11-08T09:00:00Z", updated_at: "2026-11-08T10:00:00Z" }, sender: { type: "User" }, ...over });
  const get = async () => (await (await worker.fetch(new Request(`https://x/v1/replies?box_id=${BOX}`, { headers: { "x-hasler-token": "word" } }), e)).json()).replies;
  assert.equal((await hook(e, issues("labeled", { label: { name: "bug" } }), { event: "issues" })).status, 204);
  assert.equal((await hook(e, issues("labeled", { label: { name: "seen" } }), { event: "issues" })).status, 200);
  await hook(e, issues("unlabeled", { label: { name: "seen" } }), { event: "issues" });
  await hook(e, issues("labeled", { label: { name: "Seen" } }), { event: "issues" });   // relabelled: said once
  let q = await get();
  assert.deepEqual(q.map(r => [r.state, r.text]), [["seen", "The makers have seen your request #12."]]);
  const state = async () => JSON.parse(e.SUPPORT.m.get(`issues:${BOX}`))[0].state;
  assert.equal(await state(), "seen");                // the dashboard says "#12 seen"
  // "close with comment": the comment first, then the close folds it in
  await hook(e, comment(12, "/owner The new card is in the post."));
  await hook(e, issues("closed"), { event: "issues" });
  await hook(e, issues("closed"), { event: "issues" });   // redelivered
  q = await get();
  assert.deepEqual(q.map(r => [r.state, r.text]), [["seen", "The makers have seen your request #12."],
    ["done", "Your request #12 is done. The new card is in the post."]]);
  assert.equal(await state(), "done");
  await hook(e, issues("reopened"), { event: "issues" });
  assert.equal(await state(), "open");
  q = await get();
  assert.deepEqual(q.at(-1), { id: "reopened:12:2026-11-08T10:00:00Z", at: q.at(-1).at, text: "The makers reopened your request #12.", issue: 12, state: "seen" });
  // not a radio's issue, another repo, a bot, a bad signature: nothing queued
  assert.equal((await (await hook(e, issues("closed", { issue: { number: 99, body: "" } }), { event: "issues" })).json()).stored, false);
  assert.equal((await hook(e, issues("closed", { sender: { type: "Bot" } }), { event: "issues" })).status, 204);
  assert.equal((await hook(e, issues("closed", { repository: { full_name: "Delgado-Murphy/hasler" } }), { event: "issues" })).status, 204);
  assert.equal((await hook(e, issues("closed"), { event: "issues", secret: "nope" })).status, 401);
  assert.equal((await get()).length, 3);
});
