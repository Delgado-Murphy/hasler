// Each radio's own word: enrolment and the per-radio token (SUPPORT.md "How a radio gets its line").
//
// Every radio ships able to "Send to the makers" (the Worker's URL is the release default), but a
// single shared token in the public repo would be no token at all. So a radio asks for its own:
//
//   POST /v1/enrol   {schema: 1, box_id}   box_id = the random UUID hasler-install wrote to box.yaml.
//                    First come, first served ("trust on first use"): the Worker mints 32 random bytes,
//                    keeps only their SHA-256 under radio:<box_id>, and answers {ok, token} once. The
//                    radio keeps the token in /var/lib/haslerd/secrets/support.json and sends it as
//                    X-Hasler-Token from then on. An id already enrolled is 409 (the radio says so and
//                    the owner can still Share or Email); a maker clears radio:<box_id> to let it enrol
//                    again (a radio reinstalled with its old box.yaml but without its secrets).
//                    At most ENROL_PER_HOUR enrolments an hour across all radios (429 after), so nobody
//                    can mint ids by the thousand; the IP is still never read.
//
// Who may use a route that names a radio (authFor): the shared CHECKIN_TOKEN (radios that have it in
// box.yaml keep working exactly as before), or that radio's own token. A radio that has enrolled
// needs its token (or the shared word) even when no CHECKIN_TOKEN is set, so nobody else can read its
// replies. With no CHECKIN_TOKEN and no enrolment the route stays open, as it always was.
//
// KV keys (SUPPORT): radio:<box> {hash, at} (kept), enrolled:<hour> (a count, 2 hours),
// reports-all:<day> (reports made with a radio's own token, all radios, 2 days).

export const ENROL_PER_HOUR = 30;
export const REPORTS_ALL_PER_DAY = 200;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const json = (doc, status = 200) => new Response(JSON.stringify(doc), { status, headers: { "content-type": "application/json; charset=utf-8" } });

export async function sha256hex(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function sameHex(a, b) {                        // constant time for equal lengths
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

const limit = (env, name, dflt) => {
  const n = Number(env && env[name]);
  return Number.isFinite(n) && n >= 0 ? n : dflt;
};

// {ok, kind: "shared" | "radio" | "open"} for a request about radio ``box``.
export async function authFor(request, env, box) {
  const given = request.headers.get("X-Hasler-Token") || "";
  const shared = (env && env.CHECKIN_TOKEN) || "";
  if (shared && given && sameHex(given, shared)) return { ok: true, kind: "shared" };
  const rec = env && env.SUPPORT && typeof box === "string" && box ? await env.SUPPORT.get(`radio:${box}`, "json") : null;
  if (rec && given && sameHex(await sha256hex(given), String(rec.hash || ""))) return { ok: true, kind: "radio" };
  if (!shared && !rec) return { ok: true, kind: "open" };
  return { ok: false, kind: null };
}

export async function handleEnrol(request, env, now) {
  if (!env.SUPPORT) return json({ ok: false, error: "support isn't set up on this server" }, 503);
  const length = Number(request.headers.get("content-length") || 0);
  if (length > 512) return json({ ok: false, error: "too large" }, 413);
  let doc;
  try { doc = JSON.parse(await request.text()); } catch (_) { return json({ ok: false, error: "not JSON" }, 400); }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return json({ ok: false, error: "not a JSON object" }, 400);
  if (doc.schema !== undefined && doc.schema !== 1) return json({ ok: false, error: "unknown schema" }, 400);
  const box = doc.box_id;
  if (typeof box !== "string" || !UUID_RE.test(box)) return json({ ok: false, error: "box_id must be the radio's UUID" }, 400);
  if (await env.SUPPORT.get(`radio:${box}`)) return json({ ok: false, error: "already enrolled" }, 409);
  const hour = new Date(now).toISOString().slice(0, 13);
  const countKey = `enrolled:${hour}`;
  const count = Number(await env.SUPPORT.get(countKey)) || 0;
  if (count >= limit(env, "ENROL_PER_HOUR", ENROL_PER_HOUR)) return json({ ok: false, error: "too many new radios this hour" }, 429);
  await env.SUPPORT.put(countKey, String(count + 1), { expirationTtl: 2 * 3600 });
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const token = [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
  await env.SUPPORT.put(`radio:${box}`, JSON.stringify({ hash: await sha256hex(token), at: new Date(now).toISOString() }));
  return json({ ok: true, token });
}

// Reports made with a radio's own token: a ceiling across all radios, on top of each radio's PER_DAY.
// The shared word is the makers' own radios and doesn't count. Returns a 429 Response, or null.
export async function fleetCap(env, kind, now) {
  if (kind !== "radio" && kind !== "open") return null;
  const key = `reports-all:${new Date(now).toISOString().slice(0, 10)}`;
  const n = Number(await env.SUPPORT.get(key)) || 0;
  if (n >= limit(env, "REPORTS_ALL_PER_DAY", REPORTS_ALL_PER_DAY)) return json({ ok: false, error: "the makers' list is full today" }, 429);
  await env.SUPPORT.put(key, String(n + 1), { expirationTtl: 2 * 86400 });
  return null;
}
