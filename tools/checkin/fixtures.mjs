// Shared by the tests (no tests here): a fake KV and the two payload shapes.

export class FakeKV {
  constructor() { this.m = new Map(); this.ttl = new Map(); this.writes = 0; }
  async put(k, v, opts) { this.writes++; this.m.set(k, v); if (opts && opts.expirationTtl) this.ttl.set(k, opts.expirationTtl); }
  async get(k, type) { const v = this.m.get(k); return v === undefined ? null : type === "json" ? JSON.parse(v) : v; }
  async delete(k) { this.writes++; this.m.delete(k); }
  async list() { return { keys: [...this.m.keys()].map(name => ({ name })), list_complete: true }; }
}

export const BOX = "0f3a7b1e-1111-4222-8333-444455556666";

// What a 1.6.1 radio sends today (the bench radio's live preview, 2026-11-07).
export const v1 = (over = {}) => ({ schema: 1, box_id: BOX, short_id: "6bb6", variant: "hasler-hm-hat", version: "1.6.1",
  channel: "bench", update_mode: "auto", check: "ok", last_result: { ok: true, version: "1.6.2", probation: true },
  health: { ok: true, failed: [] }, faults: [], player: { restarts_last_hour: 0, stalls_today: 0 }, ytdlp: true, os_updates: null,
  uptime_days: 0.1, last_shutdown: "restart", power_cuts_month: 0, free_bytes: 49664946176, storage_bad: false, temp_c: 56,
  throttled: null, at: "2026-11-07T16:05:01Z", ...over });

// The spec's schema 2 example (docs/ota.md §5).
export const v2 = (over = {}) => ({ schema: 2, box_id: BOX, short_id: "6bb6", variant: "hasler-hm-hat", version: "1.6.2",
  channel: "bench", update_mode: "auto", check: "ok", release: { state: "probation", version: "1.6.2", from: "1.6.1", reason: null },
  health: { ok: true, failed: [] }, faults: { now: [], day: [] }, player: { stalls_24h: 0, restarts_24h: 0, daemon_restarts_24h: 0 },
  ytdlp: { working: true, version: "2026.10.21", failures_24h: 0 }, os_updates: { state: "deferred", last_ok_d: null },
  power: { last_off: "restart", cuts_30d: 0, undervolt_now: false, undervolt_24h: false, throttled_24h: false },
  temp_c: { now: 56, max_24h: 61 }, storage: { free_mb: 47364, total_gb: 59, state: "ok" }, uptime_d: 0,
  hw: { model: "Raspberry Pi 4 Model B Rev 1.5", code: "d03115", ram_gb: 8 },
  os: { debian: "13.7", kernel: "6.18.50+rpt-rpi-v8", moode: "10.3.7" }, ...over });
