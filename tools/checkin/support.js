// Owner requests: "Send to the makers" on the radio becomes an issue in the makers' PRIVATE
// GitHub repo, and a maker's "/owner ..." comment on it comes back to the radio as a note.
// SUPPORT.md has the setup. Owners never need a GitHub account; nothing here touches a public repo.
//
//   POST /v1/report          a radio's report (daemon/haslerd/web/support_api.py), X-Hasler-Token the
//                            shared word or the radio's own (enrol.js): {schema: 1, box_id, short_id, note, text, json}, at most
//                            16 KB. At most PER_DAY a day per box_id (429 after). The same box_id with
//                            the same note within DEDUP_S is a comment on the issue it made, not a new
//                            one. Answers {ok, number, deduped}. The IP is never read or stored.
//   POST /v1/reply           the owner writes back on a request: {schema: 1, box_id, issue, note}, same
//                            token as a report. Only for an issue this radio opened (issue:<n>); a
//                            comment on it, the owner's lines quoted (so none can start "/owner"); a
//                            closed issue opens again, quietly (no "reopened" note goes back). At most
//                            FOLLOWUPS_PER_DAY a day per box_id. Answers {ok, number, reopened}.
//   POST /v1/github          GitHub's webhook (issue_comment, issues), HMAC-SHA256 verified with
//                            GITHUB_WEBHOOK_SECRET (refused when unset). A comment with a line that
//                            starts "/owner " is queued for that issue's radio, everything after
//                            "/owner" with its line breaks (MAX_REPLY); editing that comment queues the
//                            new words under the same id ({edited: true}); so are the issue's
//                            states: closed ("done", folding in a /owner reply not yet fetched),
//                            reopened, and the label "seen" (once). Those replies carry state: seen|done.
//   GET  /v1/replies?box_id= the radio's queued replies (the shared word or that radio's own token): {replies: [{id, at, text, issue}]}
//   POST /v1/replies/ack     {box_id, ids: [...]}: the radio kept them; they leave the queue.
//
// Bindings: SUPPORT (KV, its own namespace so the dashboard's list stays radios only).
// Secrets: GITHUB_TOKEN (fine-grained, Issues read/write on the support repo only),
// GITHUB_WEBHOOK_SECRET. Var: SUPPORT_REPO (default Delgado-Murphy/hasler-support).
//
// KV keys (SUPPORT): rate:<box>:<day> (a count, 2 days), rate:reply:<box>:<day> (the same, for write-backs),
// quiet:reopen:<n> (the owner's write-back opened it again: no "reopened" note, an hour), last:<box> (the last note's hash and
// issue, DEDUP_S), issue:<n> (which radio, kept), issues:<box> (its last few issue numbers and their state, open / seen / answered / done, for the
// dashboard), replies:<box> (the queue, at most MAX_REPLIES), said:seen:<n> ("seen" said once).
// Who may report for, or read the replies of, a radio: enrol.js authFor (the shared CHECKIN_TOKEN, or
// that radio's own token from POST /v1/enrol).

import { authFor, fleetCap } from "./enrol.js";
import { closedByHand } from "./alerts.js";

export const MAX_REPORT = 16384;
export const PER_DAY = 5;
export const DEDUP_S = 600;
export const DEFAULT_REPO = "Delgado-Murphy/hasler-support";
const MAX_TEXT = 12000;
const MAX_NOTE = 600;
const MAX_REPLY = 2000;
const MAX_FOLLOWUP = 2000;
export const FOLLOWUPS_PER_DAY = 20;
const MAX_REPLIES = 20;
const ISSUES_KEPT = 10;
const BOX_RE = /^[A-Za-z0-9._-]{1,64}$/;

const json = (doc, status = 200) => new Response(JSON.stringify(doc), { status, headers: { "content-type": "application/json; charset=utf-8" } });
const oneLine = s => String(s ?? "").replace(/\s+/g, " ").trim();
// Line breaks kept: spaces squeezed within each line, at most one blank line in a row.
export const tidyLines = s => String(s ?? "").replace(/\r\n?/g, "\n").split("\n").map(l => l.replace(/[ \t\f\v]+/g, " ").trim())
  .filter((l, i, a) => l || (i > 0 && a[i - 1])).join("\n").trim();

export function repoOf(env) {
  const r = String((env && env.SUPPORT_REPO) || DEFAULT_REPO).trim();
  return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r) ? r : DEFAULT_REPO;
}

// --- the report -----------------------------------------------------------------------------------

export function validateReport(text) {
  if (typeof text !== "string" || text.length === 0) return { error: "empty body" };
  if (new TextEncoder().encode(text).length > MAX_REPORT) return { error: `body larger than ${MAX_REPORT} bytes` };
  let doc;
  try { doc = JSON.parse(text); } catch (_) { return { error: "not JSON" }; }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return { error: "not a JSON object" };
  if (doc.schema !== undefined && doc.schema !== 1) return { error: "unknown schema" };
  if (typeof doc.box_id !== "string" || !BOX_RE.test(doc.box_id)) return { error: "box_id must be a short plain string" };
  if (typeof doc.text !== "string" || !doc.text.trim()) return { error: "text missing" };
  if (doc.text.length > MAX_TEXT) return { error: "text too long" };
  if (doc.note != null && typeof doc.note !== "string") return { error: "note must be a string" };
  if (doc.json != null && (typeof doc.json !== "object" || Array.isArray(doc.json))) return { error: "json must be an object" };
  return { doc, id: doc.box_id };
}

const labelPart = s => oneLine(s).toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

export function labelsFor(doc) {
  const j = doc.json || {};
  const radio = j.radio || {}, lamp = j.lamp || {};
  const out = ["owner-report"];
  for (const f of Array.isArray(lamp.faults) ? lamp.faults.slice(0, 5) : []) {
    const p = labelPart(f); if (p) out.push(`fault:${p}`);
  }
  const v = labelPart(radio.version); if (v) out.push(`version:${v}`);
  const m = labelPart(radio.variant); if (m) out.push(`model:${m}`);
  return [...new Set(out)];
}

export function titleFor(doc) {
  const j = doc.json || {};
  const short = oneLine(doc.short_id || (j.radio && j.radio.short_id) || doc.box_id.slice(0, 4)).slice(0, 8);
  const first = oneLine(String(doc.note || "").split("\n")[0]) || oneLine(j.lamp && j.lamp.words) || "a report";
  const t = `${short} · ${first}`;
  return t.length > 90 ? t.slice(0, 89) + "…" : t;
}

const fence = s => String(s).replace(/`{3,}/g, m => "'".repeat(m.length));

export function bodyFor(doc) {
  const parts = [
    "From a radio's **Write to the makers** sheet. Reply to the owner with a comment that starts `/owner ` " +
    "(the rest of the comment goes to the radio as a note from the makers; it's all the owner sees).",
    "", "```text", fence(doc.text.trimEnd()), "```",
  ];
  if (doc.json) {
    let j = JSON.stringify(doc.json, null, 1);
    if (j.length > 40000) j = j.slice(0, 40000) + "\n…";
    parts.push("", "<details><summary>The same, as JSON</summary>", "", "```json", fence(j), "```", "", "</details>");
  }
  parts.push("", `<!-- hasler-box: ${doc.box_id} -->`);
  return parts.join("\n");
}

async function sha256(s) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

export async function github(env, method, path, body) {
  const res = await fetch(`https://api.github.com${path}`, {
    method,
    headers: {
      "authorization": `Bearer ${env.GITHUB_TOKEN}`, "accept": "application/vnd.github+json",
      "x-github-api-version": "2022-11-28", "user-agent": "hasler-support-worker", "content-type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let doc = null;
  try { doc = await res.json(); } catch (_) { doc = null; }
  return { status: res.status, doc };
}

export async function handleReport(request, env, now) {
  if (!env.SUPPORT) return json({ ok: false, error: "support isn't set up on this server" }, 503);
  if (!env.GITHUB_TOKEN) return json({ ok: false, error: "support isn't set up on this server" }, 503);
  const length = Number(request.headers.get("content-length") || 0);
  if (length > MAX_REPORT) return json({ ok: false, error: "too large" }, 413);
  const v = validateReport(await request.text());
  if (v.error) return json({ ok: false, error: v.error }, 400);
  const { doc, id } = v;
  const auth = await authFor(request, env, id);
  if (!auth.ok) return json({ ok: false, error: "this radio isn't known here" }, 403);
  const day = new Date(now).toISOString().slice(0, 10);
  const rateKey = `rate:${id}:${day}`;
  const count = Number(await env.SUPPORT.get(rateKey)) || 0;
  if (count >= PER_DAY) return json({ ok: false, error: `at most ${PER_DAY} requests a day`, limit: PER_DAY }, 429);
  await env.SUPPORT.put(rateKey, String(count + 1), { expirationTtl: 2 * 86400 });
  const full = await fleetCap(env, auth.kind, now);
  if (full) return full;

  const repo = repoOf(env);
  const noteHash = await sha256(oneLine(doc.note));
  const last = await env.SUPPORT.get(`last:${id}`, "json");
  if (last && last.note === noteHash && now - Date.parse(last.at) < DEDUP_S * 1000 && Number.isInteger(last.number)) {
    const c = await github(env, "POST", `/repos/${repo}/issues/${last.number}/comments`,
      { body: "Sent again from the radio:\n\n```text\n" + fence(doc.text.trimEnd()) + "\n```" });
    if (c.status >= 300) return json({ ok: false, error: `GitHub answered ${c.status}` }, 502);
    return json({ ok: true, number: last.number, deduped: true });
  }

  const issue = { title: titleFor(doc), body: bodyFor(doc), labels: labelsFor(doc) };
  let r = await github(env, "POST", `/repos/${repo}/issues`, issue);
  if (r.status === 422) {                       // a label GitHub won't take: the issue matters more
    const { labels, ...bare } = issue;
    r = await github(env, "POST", `/repos/${repo}/issues`, bare);
  }
  if (r.status >= 300 || !r.doc || !Number.isInteger(r.doc.number)) return json({ ok: false, error: `GitHub answered ${r.status}` }, 502);
  const number = r.doc.number;
  const at = new Date(now).toISOString();
  await env.SUPPORT.put(`last:${id}`, JSON.stringify({ note: noteHash, number, at }), { expirationTtl: DEDUP_S });
  await env.SUPPORT.put(`issue:${number}`, JSON.stringify({ box_id: id, short_id: oneLine(doc.short_id).slice(0, 8) }));
  const kept = (await env.SUPPORT.get(`issues:${id}`, "json")) || [];
  await env.SUPPORT.put(`issues:${id}`, JSON.stringify([{ number, at }, ...kept].slice(0, ISSUES_KEPT)));
  return json({ ok: true, number, deduped: false });
}

// --- the owner writes back on a request ------------------------------------------------------------

export function followupBody(note) {
  const quoted = tidyLines(note).slice(0, MAX_FOLLOWUP).split("\n").map(l => l ? "> " + l : ">").join("\n");
  return "**The owner wrote back** from the radio:\n\n" + quoted;
}

export async function handleFollowup(request, env, now) {
  if (!env.SUPPORT || !env.GITHUB_TOKEN) return json({ ok: false, error: "support isn't set up on this server" }, 503);
  const text = await request.text();
  if (new TextEncoder().encode(text).length > MAX_REPORT) return json({ ok: false, error: "too large" }, 413);
  let doc;
  try { doc = JSON.parse(text); } catch (_) { return json({ ok: false, error: "not JSON" }, 400); }
  if (!doc || typeof doc !== "object" || typeof doc.box_id !== "string" || !BOX_RE.test(doc.box_id)) return json({ ok: false, error: "box_id must be a short plain string" }, 400);
  if (!Number.isInteger(doc.issue) || doc.issue < 1) return json({ ok: false, error: "issue must be a number" }, 400);
  if (typeof doc.note !== "string" || !tidyLines(doc.note)) return json({ ok: false, error: "note missing" }, 400);
  const id = doc.box_id, number = doc.issue;
  const auth = await authFor(request, env, id);
  if (!auth.ok) return json({ ok: false, error: "this radio isn't known here" }, 403);
  const rec = await env.SUPPORT.get(`issue:${number}`, "json");
  if (!rec || rec.box_id !== id) return json({ ok: false, error: "not this radio's request" }, 404);
  const day = new Date(now).toISOString().slice(0, 10);
  const rateKey = `rate:reply:${id}:${day}`;
  const count = Number(await env.SUPPORT.get(rateKey)) || 0;
  if (count >= FOLLOWUPS_PER_DAY) return json({ ok: false, error: `at most ${FOLLOWUPS_PER_DAY} a day`, limit: FOLLOWUPS_PER_DAY }, 429);
  await env.SUPPORT.put(rateKey, String(count + 1), { expirationTtl: 2 * 86400 });
  const repo = repoOf(env);
  const c = await github(env, "POST", `/repos/${repo}/issues/${number}/comments`, { body: followupBody(doc.note) });
  if (c.status >= 300) return json({ ok: false, error: `GitHub answered ${c.status}` }, 502);
  let reopened = false;
  const i = await github(env, "GET", `/repos/${repo}/issues/${number}`);
  if (i.status < 300 && i.doc && i.doc.state === "closed") {
    await env.SUPPORT.put(`quiet:reopen:${number}`, "1", { expirationTtl: 3600 });
    const r = await github(env, "PATCH", `/repos/${repo}/issues/${number}`, { state: "open" });
    reopened = r.status < 300;
  }
  await setIssueState(env, id, number, "open");    // the makers owe a reply again (the radio: "sent")
  return json({ ok: true, number, reopened });
}

// --- GitHub's webhook -----------------------------------------------------------------------------

function hexToBytes(hex) {
  if (!/^[0-9a-f]*$/i.test(hex) || hex.length % 2) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16);
  return out;
}

export async function signatureOk(secret, body, header) {
  if (!secret || typeof header !== "string" || !header.startsWith("sha256=")) return false;
  const given = hexToBytes(header.slice(7));
  if (!given || given.length !== 32) return false;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("HMAC", key, given, new TextEncoder().encode(body));   // constant-time
}

export function ownerReply(commentBody) {
  const m = /^\/owner[ \t]+(\S[\s\S]*)$/m.exec(String(commentBody || ""));
  if (!m) return null;
  // Markdown kept as written: indentation (nested lists, code) and spaces inside a line stay; only
  // trailing spaces go and at most one blank line in a row (tidyLines would flatten the indents).
  const text = String(m[1]).replace(/\r\n?/g, "\n").split("\n").map(l => l.trimEnd()).join("\n")
    .replace(/\n{3,}/g, "\n\n").replace(/^\n+/, "").slice(0, MAX_REPLY).trimEnd();
  return text || null;
}

export const SEEN_LABEL = "seen";

// What the owner reads when the issue's state changes (the radio shows it like a reply).
export const STATE_WORDS = {
  done: n => `Your request #${n} is done.`,
  reopened: n => `The makers reopened your request #${n}.`,
  seen: n => `The makers have seen your request #${n}.`,
};

async function boxOf(env, issue) {
  const rec = await env.SUPPORT.get(`issue:${issue.number}`, "json");
  let box = rec && rec.box_id;
  if (!box) {                                   // the issue's own marker, if KV lost the map
    const m = /<!-- hasler-box: ([A-Za-z0-9._-]{1,64}) -->/.exec(String(issue.body || ""));
    box = m ? m[1] : null;
  }
  return box && BOX_RE.test(box) ? box : null;
}

// What an event says to the owner: {id, text, state} or null. ``queue`` is the radio's pending
// replies: closing folds a /owner reply the radio hasn't fetched yet into "done".
export function stateReply(p, queue, now) {
  const n = p.issue.number, at = new Date(now).toISOString();
  if (p.action === "closed") {
    const pending = queue.filter(r => r.issue === n && !r.state);
    const last = pending.at(-1);
    const text = STATE_WORDS.done(n) + (last ? " " + last.text : "");
    return { id: `closed:${n}:${p.issue.closed_at || at}`, text: text.slice(0, MAX_REPLY), state: "done", fold: pending.map(r => r.id) };
  }
  if (p.action === "reopened" && p.quiet) return null;       // the owner wrote back: they know
  if (p.action === "reopened") return { id: `reopened:${n}:${p.issue.updated_at || at}`, text: STATE_WORDS.reopened(n), state: "seen" };   // open again, and looked at
  if (p.action === "labeled" && p.label && String(p.label.name).toLowerCase() === SEEN_LABEL) {
    return { id: `seen:${n}`, text: STATE_WORDS.seen(n), state: "seen", once: true };
  }
  return null;
}

export async function handleGithub(request, env, now = Date.now()) {
  if (!env.SUPPORT || !env.GITHUB_WEBHOOK_SECRET) return new Response("not set up", { status: 503 });
  const body = await request.text();
  if (!(await signatureOk(env.GITHUB_WEBHOOK_SECRET, body, request.headers.get("x-hub-signature-256")))) {
    return new Response("bad signature", { status: 401 });
  }
  const event = request.headers.get("x-github-event");
  if (event !== "issue_comment" && event !== "issues") return new Response(null, { status: 204 });   // ping, the rest
  let p;
  try { p = JSON.parse(body); } catch (_) { return new Response("not JSON", { status: 400 }); }
  if (!p || !p.issue || !Number.isInteger(p.issue.number)) return new Response(null, { status: 204 });
  if (String(p.repository && p.repository.full_name || "").toLowerCase() !== repoOf(env).toLowerCase()) return new Response(null, { status: 204 });
  if (p.sender && p.sender.type === "Bot") return new Response(null, { status: 204 });
  if (event === "issues" && p.action === "closed" && await closedByHand(env, p.issue)) {
    return json({ stored: false, why: "an alert, closed" }, 200);     // alerts.js: its key goes, a new one may open
  }
  let item = null;
  const number = p.issue.number;
  if (event === "issue_comment") {
    if ((p.action !== "created" && p.action !== "edited") || !p.comment || (p.comment.user && p.comment.user.type === "Bot")) return new Response(null, { status: 204 });
    const text = ownerReply(p.comment.body);
    if (!text) return new Response(null, { status: 204 });
    item = { id: p.comment.id, at: p.comment.created_at || new Date(now).toISOString(), text, issue: number };
    if (p.action === "edited") item.edited = true;     // the radio puts the new words in place of the old
  }
  const box = await boxOf(env, p.issue);
  if (!box) return json({ stored: false, why: "not a radio's issue" }, 200);
  let queue = (await env.SUPPORT.get(`replies:${box}`, "json")) || [];
  if (event === "issues") {
    if (p.action === "reopened" && await env.SUPPORT.get(`quiet:reopen:${number}`)) {
      await env.SUPPORT.delete(`quiet:reopen:${number}`);
      p.quiet = true;
    }
    const s = stateReply(p, queue, now);
    if (!s) return new Response(null, { status: 204 });
    if (s.once) {                               // "seen" says so once per issue, however often it's relabelled
      if (await env.SUPPORT.get(`said:${s.id}`)) return new Response(null, { status: 204 });
      await env.SUPPORT.put(`said:${s.id}`, "1");
    }
    if (s.fold) queue = queue.filter(r => !s.fold.includes(r.id));
    item = { id: s.id, at: new Date(now).toISOString(), text: s.text, issue: number, state: s.state };
    await setIssueState(env, box, number, s.state);   // the dashboard's "#12 seen"; reopened by the makers: seen
  } else if (!item.edited) {
    await setIssueState(env, box, number, "answered", { unless: "done" });   // a /owner reply: "#12 answered"
  }
  if (item.edited) queue = queue.filter(r => r.id !== item.id);    // an edit replaces what waits
  if (!queue.some(r => r.id === item.id)) queue.push(item);
  await env.SUPPORT.put(`replies:${box}`, JSON.stringify(queue.slice(-MAX_REPLIES)));
  return json({ stored: true, box: box.slice(0, 8), state: item.state || null }, 200);
}

// --- the radio fetches its replies ----------------------------------------------------------------

export async function handleReplies(request, env, url) {
  if (!env.SUPPORT) return json({ replies: [] });
  if (request.method === "GET") {
    const box = url.searchParams.get("box_id") || "";
    if (!BOX_RE.test(box)) return json({ error: "box_id must be a short plain string" }, 400);
    if (!(await authFor(request, env, box)).ok) return json({ error: "no" }, 403);
    return json({ replies: (await env.SUPPORT.get(`replies:${box}`, "json")) || [] });
  }
  let doc;
  try { doc = JSON.parse(await request.text()); } catch (_) { return json({ error: "not JSON" }, 400); }
  const box = doc && doc.box_id;
  if (typeof box !== "string" || !BOX_RE.test(box) || !Array.isArray(doc.ids)) return json({ error: "box_id and ids" }, 400);
  if (!(await authFor(request, env, box)).ok) return json({ error: "no" }, 403);
  const queue = (await env.SUPPORT.get(`replies:${box}`, "json")) || [];
  const left = queue.filter(r => !doc.ids.includes(r.id));
  if (left.length) await env.SUPPORT.put(`replies:${box}`, JSON.stringify(left));
  else await env.SUPPORT.delete(`replies:${box}`);
  return new Response(null, { status: 204 });
}

// The issue's state beside its number in issues:<box>, for the dashboard. One model, the radio's too:
//   open (sent) -> seen (the "seen" label) -> answered (a /owner reply) -> done (closed).
//   Reopened by the makers: seen. The owner writes back (POST /v1/reply, reopening a closed issue
//   quietly): open again - the makers owe a reply.
export async function setIssueState(env, box, number, state, { unless } = {}) {
  const kept = (await env.SUPPORT.get(`issues:${box}`, "json")) || [];
  const i = kept.findIndex(x => x && x.number === number);
  if (i < 0 || kept[i].state === state || (unless && (kept[i].state || "open") === unless)) return;
  kept[i] = { ...kept[i], state };
  await env.SUPPORT.put(`issues:${box}`, JSON.stringify(kept));
}

// The dashboard's "requests" cell: a radio's last few issues ({number, at, state}; no state = open).
export async function issuesFor(env, box) {
  if (!env || !env.SUPPORT) return [];
  return (await env.SUPPORT.get(`issues:${box}`, "json")) || [];
}
