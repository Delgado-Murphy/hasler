// node --test tools/checkin/ - each radio's own word (enrol.js): enrol once, report and read replies
// with the radio's token, the shared CHECKIN_TOKEN still works, nobody else reads a radio's replies,
// the hourly and fleet ceilings. Fake KV, mocked GitHub; no network.
import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.js";
import { ENROL_PER_HOUR } from "./enrol.js";

class FakeKV {
  constructor() { this.m = new Map(); }
  async put(k, v) { this.m.set(k, v); }
  async get(k, type) { const v = this.m.get(k); return v === undefined ? null : type === "json" ? JSON.parse(v) : v; }
  async delete(k) { this.m.delete(k); }
  async list() { return { keys: [...this.m.keys()].map(name => ({ name })), list_complete: true }; }
}

const BOX = "0f3a7b1e-1111-4222-8333-444455556666";
const OTHER = "9a9a9a9a-1111-4222-8333-444455556666";
let realFetch, nextNumber;
beforeEach(() => {
  nextNumber = 40; realFetch = globalThis.fetch;
  globalThis.fetch = async (url) => String(url).endsWith("/issues")
    ? new Response(JSON.stringify({ number: nextNumber++ }), { status: 201 })
    : new Response("{}", { status: 201 });
});
afterEach(() => { globalThis.fetch = realFetch; });

const env = (over = {}) => ({ CHECKINS: new FakeKV(), SUPPORT: new FakeKV(), GITHUB_TOKEN: "ghp_test", GITHUB_WEBHOOK_SECRET: "s", ...over });
const call = (e, method, path, doc, token) => {
  const headers = { "content-type": "application/json" };
  if (token) headers["x-hasler-token"] = token;
  const body = doc === undefined ? undefined : JSON.stringify(doc);
  return worker.fetch(new Request("https://x" + path, { method, body, headers }), e);
};
const enrol = (e, box = BOX) => call(e, "POST", "/v1/enrol", { schema: 1, box_id: box });
const report = (box, note = "It goes quiet") => ({ schema: 1, box_id: box, short_id: "ab12", note, text: "Hasler · radio ab12\n" });

test("a radio enrols once and gets its own token; the Worker keeps only its hash", async () => {
  const e = env();
  const r = await enrol(e);
  assert.equal(r.status, 200);
  const { ok, token } = await r.json();
  assert.ok(ok && /^[0-9a-f]{64}$/.test(token));
  const rec = JSON.parse(e.SUPPORT.m.get(`radio:${BOX}`));
  assert.ok(/^[0-9a-f]{64}$/.test(rec.hash) && rec.hash !== token && !JSON.stringify([...e.SUPPORT.m]).includes(token));
  // the same id again: refused, the first token stays the only one
  const again = await enrol(e);
  assert.equal(again.status, 409);
  assert.equal((await again.json()).error, "already enrolled");
  // not a UUID, not JSON, no SUPPORT binding
  assert.equal((await enrol(e, "test-0001")).status, 400);
  assert.equal((await worker.fetch(new Request("https://x/v1/enrol", { method: "POST", body: "{nope" }), e)).status, 400);
  assert.equal((await enrol(env({ SUPPORT: undefined }))).status, 503);
});

test("the radio's token opens its own report, replies and check-in, and nobody else's", async () => {
  const e = env();
  const { token } = await (await enrol(e)).json();
  // without a CHECKIN_TOKEN on the Worker an enrolled radio still needs its word
  assert.equal((await call(e, "POST", "/v1/report", report(BOX))).status, 403);
  assert.equal((await call(e, "POST", "/v1/report", report(BOX), "f".repeat(64))).status, 403);
  const ok = await call(e, "POST", "/v1/report", report(BOX), token);
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).number, 40);
  assert.equal((await call(e, "GET", `/v1/replies?box_id=${BOX}`, undefined, token)).status, 200);
  assert.equal((await call(e, "GET", `/v1/replies?box_id=${BOX}`)).status, 403);
  assert.equal((await call(e, "POST", "/v1/replies/ack", { box_id: BOX, ids: [] })).status, 403);
  assert.equal((await call(e, "POST", "/v1/replies/ack", { box_id: BOX, ids: [] }, token)).status, 204);
  assert.equal((await call(e, "POST", "/v1/checkin", { schema: 1, box_id: BOX, short_id: "ab12" }, token)).status, 204);
  assert.equal((await call(e, "POST", "/v1/checkin", { schema: 1, box_id: BOX, short_id: "ab12" })).status, 403);
  // one radio's token is not another's
  await enrol(e, OTHER);
  assert.equal((await call(e, "POST", "/v1/report", report(OTHER), token)).status, 403);
  assert.equal((await call(e, "GET", `/v1/replies?box_id=${OTHER}`, undefined, token)).status, 403);
});

test("the shared CHECKIN_TOKEN keeps working, for any radio, enrolled or not", async () => {
  const e = env({ CHECKIN_TOKEN: "word" });
  assert.equal((await call(e, "POST", "/v1/report", report(BOX), "word")).status, 200);
  assert.equal((await call(e, "POST", "/v1/report", report(BOX, "x"))).status, 403);
  const { token } = await (await enrol(e)).json();
  assert.equal((await call(e, "POST", "/v1/report", report(BOX, "y"), "word")).status, 200);
  assert.equal((await call(e, "POST", "/v1/report", report(BOX, "z"), token)).status, 200);
  assert.equal((await call(e, "POST", "/v1/checkin", { schema: 1, box_id: BOX }, "word")).status, 204);
  // an unenrolled radio without the word: still refused when the Worker has one
  assert.equal((await call(e, "POST", "/v1/report", report(OTHER))).status, 403);
});

test("a maker clears radio:<box_id> and the radio may enrol again", async () => {
  const e = env();
  const { token } = await (await enrol(e)).json();
  await e.SUPPORT.delete(`radio:${BOX}`);
  const { token: fresh } = await (await enrol(e)).json();
  assert.notEqual(fresh, token);
  assert.equal((await call(e, "POST", "/v1/report", report(BOX), token)).status, 403);
  assert.equal((await call(e, "POST", "/v1/report", report(BOX), fresh)).status, 200);
});

test("at most ENROL_PER_HOUR new radios an hour, and a ceiling on reports across radios", async () => {
  const e = env({ ENROL_PER_HOUR: "2", REPORTS_ALL_PER_DAY: "1" });
  const a = await (await enrol(e)).json();
  await enrol(e, OTHER);
  assert.equal((await enrol(e, "11111111-1111-4222-8333-444455556666")).status, 429);
  assert.equal(ENROL_PER_HOUR, 30);
  assert.equal((await call(e, "POST", "/v1/report", report(BOX), a.token)).status, 200);
  const full = await call(e, "POST", "/v1/report", report(BOX, "another"), a.token);
  assert.equal(full.status, 429);
  // the makers' own radios (the shared word) aren't counted
  const e2 = env({ CHECKIN_TOKEN: "word", REPORTS_ALL_PER_DAY: "0" });
  assert.equal((await call(e2, "POST", "/v1/report", report(BOX), "word")).status, 200);
});
