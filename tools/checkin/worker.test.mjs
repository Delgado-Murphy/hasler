// node --test tools/checkin/worker.test.mjs - the Worker's validation and the dashboard's order,
// with a fake KV. No Cloudflare, no network.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { validate, rowOf, sortRows, renderDashboard, tokenOk } from "./worker.js";

class FakeKV {
  constructor() { this.m = new Map(); }
  async put(k, v) { this.m.set(k, v); }
  async get(k, type) { const v = this.m.get(k); return v === undefined ? null : type === "json" ? JSON.parse(v) : v; }
  async list() { return { keys: [...this.m.keys()].map(name => ({ name })), list_complete: true }; }
}

const payload = (over = {}) => ({ schema: 1, box_id: "0f3a7b1e-1111-4222-8333-444455556666", short_id: "ab12", variant: "hasler-hm-hat",
  version: "1.4.0", channel: "stable", update_mode: "auto", health: { ok: true, failed: [] }, faults: [], free_bytes: 9 * 1024 ** 3,
  temp_c: 61, ytdlp: { working: true }, power_cuts_month: 3, player: { stalls_today: 0, restarts_last_hour: 0 }, ...over });

test("validate: only a small JSON object with a plain box_id", () => {
  assert.equal(validate("").error, "empty body");
  assert.equal(validate("x".repeat(3000)).error, "body larger than 2048 bytes");
  assert.equal(validate("{nope").error, "not JSON");
  assert.equal(validate("[1]").error, "not a JSON object");
  assert.equal(validate(JSON.stringify({ box_id: "../etc" })).error, "box_id must be a short plain string");
  assert.equal(validate(JSON.stringify({ box_id: "a", schema: 2 })).error, "unknown schema");
  assert.equal(validate(JSON.stringify(payload())).id, "0f3a7b1e-1111-4222-8333-444455556666");
});

test("the token header gates POSTs only when CHECKIN_TOKEN is set", () => {
  const req = h => ({ headers: { get: name => (name === "X-Hasler-Token" ? h : null) } });
  assert.equal(tokenOk(req(null), {}), true);
  assert.equal(tokenOk(req(null), { CHECKIN_TOKEN: "s" }), false);
  assert.equal(tokenOk(req("s"), { CHECKIN_TOKEN: "s" }), true);
});

test("POST stores box_id -> {payload, last_seen} and nothing about the client; GET /dashboard lists it", async () => {
  const env = { CHECKINS: new FakeKV() };
  const body = JSON.stringify(payload());
  const res = await worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body,
    headers: { "content-type": "application/json", "cf-connecting-ip": "203.0.113.9", "content-length": String(body.length) } }), env);
  assert.equal(res.status, 204);
  const stored = JSON.parse(env.CHECKINS.m.get("0f3a7b1e-1111-4222-8333-444455556666"));
  assert.deepEqual(Object.keys(stored).sort(), ["last_seen", "payload"]);
  assert.ok(!JSON.stringify(stored).includes("203.0.113.9"));
  const bad = await worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body: "{", headers: { "content-length": "1" } }), env);
  assert.equal(bad.status, 400);
  const big = await worker.fetch(new Request("https://x/v1/checkin", { method: "POST", body: "{}", headers: { "content-length": "9999" } }), env);
  assert.equal(big.status, 413);
  const page = await worker.fetch(new Request("https://x/dashboard"), env);
  assert.equal(page.status, 200);
  const html = await page.text();
  assert.ok(html.includes("ab12") && html.includes("hasler-hm-hat") && html.includes("1.4.0"));
  const gated = await worker.fetch(new Request("https://x/dashboard"), { ...env, DASHBOARD_KEY: "k" });
  assert.equal(gated.status, 403);
  const ok = await worker.fetch(new Request("https://x/dashboard?key=k"), { ...env, DASHBOARD_KEY: "k" });
  assert.equal(ok.status, 200);
});

test("rows: needs a look first, then the longest unseen; old rows amber and grey, never 'broken'", () => {
  const now = Date.parse("2026-11-07T12:00:00Z");
  const seen = d => new Date(now - d * 86400000).toISOString();
  const rows = [
    rowOf("fine", { payload: payload({ short_id: "fine" }), last_seen: seen(0.2) }, now),
    rowOf("storage", { payload: payload({ short_id: "stor", faults: ["storage"], health: { ok: true, failed: [] } }), last_seen: seen(2) }, now),
    rowOf("rolled", { payload: payload({ short_id: "roll", last_result: { ok: false, version: "1.4.1", back_to: "1.4.0", reason: "check panel failed (absent)" } }), last_seen: seen(0.5) }, now),
    rowOf("quiet", { payload: payload({ short_id: "quie" }), last_seen: seen(11) }, now),
    rowOf("gone", { payload: payload({ short_id: "gone" }), last_seen: seen(20) }, now),
  ];
  const order = sortRows(rows).map(r => r.short);
  assert.deepEqual(order, ["gone", "stor", "roll", "quie", "fine"]);
  const html = renderDashboard(sortRows(rows), now);
  assert.ok(html.includes('class="grey look"') && html.includes('class="amber"'));
  assert.ok(html.includes("1.4.1→1.4.0") && html.includes("not seen") === false || html.includes("20 d ago"));
  assert.ok(!/broken/i.test(html));
});
