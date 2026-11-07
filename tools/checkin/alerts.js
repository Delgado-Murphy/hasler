// Alerts: GitHub issues labelled "alert" in the makers' private support repo (SUPPORT_REPO), opened
// and closed by the Worker itself. The makers already get notifications for that repo; nothing new
// to set up, no cost. SUPPORT.md "Alerts" has the list; docs/ota.md §5 says what the server keeps.
//
//   fault     a lamp fault now or in the last 24 h, or a failed health check     on check-in   closed after 2 clean check-ins in a row
//   rollback  release.state rolled_back (a new failed version re-opens)          on check-in   closed by hand (the release's bug)
//   quiet     seen on >= 5 of its last 7 days, now unseen for 3 days              daily cron    closed by the next check-in
//   youtube   the YouTube Music helper broken on >= 2 radios and >= 20 % of today's   daily cron    closed when the cron sees it under that
//   trend     a version with >= 10 radio-days has 2x the stalls/restarts/crashes of the one before,
//             or undervoltage on >= 2 radios of one platform                     daily cron    closed by hand
//
// Against noise: one open issue per (radio, kind), its state in SUPPORT under alert:<box>:<kind>
// ({number, sig, clean, at}; "fleet" stands in for the box on the fleet-wide kinds). A repeat is a
// comment only when the content changes (a new fault word, a new version), never once a day; bench
// radios' fault and rollback issues carry the label "bench" too; and nothing at all when nothing
// changed - no "all is well" digest. KV: no write on a check-in that changes nothing; at most 2 per
// alert's life besides the open and the close. A maker closing an alert issue by hand clears its
// key (support.js handleGithub sees the issue's <!-- hasler-alert --> marker).

import { github, repoOf } from "./support.js";
import { normalize, faultSet, faultWord, checkWord, variantLabel, rollbackWords, platformOf } from "./model.js";
import { releaseHealth, ytdlpFleet, platforms } from "./fleet.js";

export const DEFAULT_DASHBOARD_URL = "https://hasler-checkin.delgado-murphy.workers.dev";
export const CLEAN_TO_CLOSE = 2;
export const QUIET_DAYS = 3;
const MARK_RE = /<!-- hasler-alert: ([A-Za-z0-9._-]{1,64}) ([A-Za-z0-9._-]{1,64}) -->/;

export const alertsOn = env => !!(env && env.SUPPORT && env.GITHUB_TOKEN) && String(env.ALERTS || "on").toLowerCase() !== "off";
export const dashboardBase = env => String((env && env.DASHBOARD_URL) || DEFAULT_DASHBOARD_URL).replace(/\/+$/, "");
export const alertKey = (box, kind) => `alert:${box}:${kind}`;

const labelPart = s => String(s ?? "").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
const fence = s => String(s).replace(/`{3,}/g, m => "'".repeat(m.length));
const isoDay = t => new Date(t).toISOString().slice(0, 10);

/** "card full (storage)", "player not answering (check mpd)". */
export function faultPhrase(w) {
  if (w.startsWith("check:")) return `${checkWord(w.slice(6))} not answering (check ${w.slice(6)})`;
  return `${faultWord(w)} (${w})`;
}

function body(lines, link, payload, marker) {
  const parts = [...lines, "", `Dashboard: ${link}`];
  if (payload) {
    parts.push("", "<details><summary>The check-in, as the radio sent it</summary>", "", "```json",
      fence(JSON.stringify(payload, null, 1)), "```", "", "</details>");
  }
  parts.push("", `<!-- hasler-alert: ${marker} -->`);
  return parts.join("\n");
}

// --- GitHub ---------------------------------------------------------------------------------------

async function openIssue(env, issue) {
  const repo = repoOf(env);
  let r = await github(env, "POST", `/repos/${repo}/issues`, issue);
  if (r.status === 422) {                       // a label GitHub won't take: the issue matters more
    const { labels, ...bare } = issue;
    r = await github(env, "POST", `/repos/${repo}/issues`, bare);
  }
  return r.status < 300 && r.doc && Number.isInteger(r.doc.number) ? r.doc.number : null;
}

const comment = (env, n, text) => github(env, "POST", `/repos/${repoOf(env)}/issues/${n}/comments`, { body: text });

async function closeIssue(env, n, text) {
  if (text) await comment(env, n, text);
  return github(env, "PATCH", `/repos/${repoOf(env)}/issues/${n}`, { state: "closed", state_reason: "completed" });
}

async function raise(env, box, kind, { title, lines, labels, payload, sig, link }, now) {
  const key = alertKey(box, kind);
  const have = await env.SUPPORT.get(key, "json");
  if (have && Number.isInteger(have.number)) {
    if (have.sig === sig && !have.clean) return { state: "open", number: have.number, changed: false };
    if (have.sig !== sig) await comment(env, have.number, ["Changed:", "", ...lines].join("\n"));
    await env.SUPPORT.put(key, JSON.stringify({ ...have, sig, clean: 0 }));
    return { state: "open", number: have.number, changed: have.sig !== sig };
  }
  const number = await openIssue(env, { title, body: body(lines, link, payload, `${box} ${kind}`), labels });
  if (number === null) return { state: "failed" };
  await env.SUPPORT.put(key, JSON.stringify({ number, sig, clean: 0, at: new Date(now).toISOString() }));
  return { state: "opened", number };
}

async function clear(env, box, kind, words, { needClean = 1 } = {}) {
  const key = alertKey(box, kind);
  const have = await env.SUPPORT.get(key, "json");
  if (!have || !Number.isInteger(have.number)) return { state: "none" };
  const clean = (have.clean || 0) + 1;
  if (clean < needClean) {
    await env.SUPPORT.put(key, JSON.stringify({ ...have, clean }));
    return { state: "clearing", number: have.number, clean };
  }
  await closeIssue(env, have.number, words);
  await env.SUPPORT.delete(key);
  return { state: "closed", number: have.number };
}

function radioLabels(n, kind) {
  const out = ["alert", `alert:${kind}`];
  const v = labelPart(n.version); if (v) out.push(`version:${v}`);
  const m = labelPart(n.variant); if (m) out.push(`model:${m}`);
  if (n.channel === "bench") out.push("bench");
  return out;
}

// --- on check-in ----------------------------------------------------------------------------------

/** Fault, rollback and quiet for one radio's check-in. Returns what happened, per kind. */
export async function onCheckin(env, id, payload, now) {
  if (!alertsOn(env)) return null;
  const n = normalize(payload);
  const short = n.short_id || id.slice(0, 4);
  const link = `${dashboardBase(env)}/dashboard/radio/${encodeURIComponent(short)}`;
  const out = {};
  const faults = faultSet(n);
  if (n.health.ok === false && !n.health.failed.length) faults.push("check:health");
  if (faults.length) {
    const words = faults.map(faultPhrase);
    out.fault = await raise(env, id, "fault", {
      title: `${short} · ${words.join(", ")} · ${n.version || "?"}`.slice(0, 120),
      lines: [`**${short}** (${variantLabel(n.variant)}, ${n.channel || "?"}, ${n.version || "?"}) says:`, "",
        ...words.map(w => `- ${w}`), "",
        n.faults.now.length ? `Now: ${n.faults.now.map(faultWord).join(", ")}.` : "Not now: it cleared, but it was there in the last 24 hours."],
      labels: radioLabels(n, "fault"), payload, sig: `${faults.join(",")}@${n.version || ""}`, link,
    }, now);
  } else {
    out.fault = await clear(env, id, "fault", `Cleared: ${CLEAN_TO_CLOSE} check-ins in a row without a fault (${n.version || "?"}).`,
      { needClean: CLEAN_TO_CLOSE });
  }
  const rel = n.release;
  if (rel && rel.state === "rolled_back" && rel.version) {
    const why = rollbackWords(rel.reason);
    out.rollback = await raise(env, id, "rollback", {
      title: `${short} · went back from ${rel.version} to ${rel.from || "?"} · ${rel.version}`.slice(0, 120),
      lines: [`**${short}** (${variantLabel(n.variant)}, ${n.channel || "?"}) went back from **${rel.version}** to ${rel.from || "?"}.`, "",
        why ? `Why: ${why}.` : "Why: it didn't say.", rel.reason ? `Raw: \`${String(rel.reason).replace(/`/g, "'")}\`` : "",
        "", "Close this by hand when the fix ships."],
      labels: [...radioLabels(n, "rollback").filter(l => !l.startsWith("version:")), `version:${labelPart(rel.version)}`],
      payload, sig: rel.version, link,
    }, now);
  }
  out.quiet = await clear(env, id, "quiet", `Seen again ${isoDay(now)} (${n.version || "?"}).`);
  return out;
}

// --- the daily cron -------------------------------------------------------------------------------

/** Quiet radios, the YouTube Music helper across the fleet, release trends. Returns a summary. */
export async function onCron(env, records, now) {
  if (!alertsOn(env)) return null;
  const base = dashboardBase(env);
  const out = { quiet: [], youtube: null, trend: [] };
  for (const { id, rec } of records) {
    const seen = Date.parse(rec && rec.last_seen);
    if (!Number.isFinite(seen) || now - seen < QUIET_DAYS * 86400000) continue;
    const last = isoDay(seen), from = isoDay(seen - 6 * 86400000);
    const days = ((rec && rec.days) || []).filter(r => r && r.d >= from && r.d <= last).length;
    if (days < 5) continue;
    const n = normalize(rec.payload);
    const short = n.short_id || id.slice(0, 4);
    const r = await raise(env, id, "quiet", {
      title: `${short} · gone quiet · last seen ${last}`,
      lines: [`**${short}** (${variantLabel(n.variant)}, ${n.channel || "?"}, ${n.version || "?"}) checked in on ${days} of the 7 days to ${last}, and not since.`,
        "", "Off, offline or switched off - the server can't tell which. This closes by itself at its next check-in."],
      labels: ["alert", "alert:quiet", ...(n.channel === "bench" ? ["bench"] : [])], payload: null, sig: last,
      link: `${base}/dashboard/radio/${encodeURIComponent(short)}`,
    }, now);
    if (r.state === "opened") out.quiet.push(short);
  }
  const y = ytdlpFleet(records, now);
  if (y.alarm) {
    const vs = Object.entries(y.versions).map(([v, k]) => `${v} ×${k}`).join(", ") || "version not said";
    out.youtube = await raise(env, "fleet", "youtube", {
      title: `YouTube Music helper broken on ${y.broken} of ${y.lane} radios`,
      lines: [`The YouTube Music helper (yt-dlp) is broken on **${y.broken} of ${y.lane}** radios that checked in today.`, "",
        `Versions: ${vs}.`, "", "Push the component lane, or roll yt-dlp back. This closes by itself once fewer are broken."],
      labels: ["alert", "alert:youtube"], payload: null, sig: `${y.broken}/${y.lane}`, link: `${base}/dashboard`,
    }, now);
  } else {
    out.youtube = await clear(env, "fleet", "youtube", `Under the threshold today: ${y.broken} of ${y.lane} broken.`);
  }
  for (const r of releaseHealth(records, now)) {
    if (!r.rise.length) continue;
    const key = `trend-${labelPart(r.version)}`;
    const res = await raise(env, "fleet", key, {
      title: `${r.version} · ${r.rise.join(", ")} up against ${r.prev}`,
      lines: [`Over the last 30 days, **${r.version}** (${r.radioDays} radio-days on ${r.radios} radios) has at least twice the ` +
        `${r.rise.join(" and ")} per radio-day of ${r.prev}.`, "",
        `Stalls ${fmt(r.stallsPerDay)}, MPD restarts ${fmt(r.restartsPerDay)}, haslerd restarts ${fmt(r.crashesPerDay)} per radio-day.`,
        "", "Halt it, or ship a fix. Close this by hand."],
      labels: ["alert", "alert:trend", `version:${labelPart(r.version)}`], payload: null, sig: r.rise.join(","), link: `${base}/dashboard`,
    }, now);
    if (res.state === "opened") out.trend.push(r.version);
  }
  for (const p of platforms(records, now).list) {
    if (p.uv < 2) continue;
    const key = `uv-${labelPart(p.platform)}`;
    const res = await raise(env, "fleet", key, {
      title: `Low voltage on ${p.uv} radios · ${p.platform}`,
      lines: [`${p.uv} of the ${p.n} radios on **${p.platform}** saw their supply sag in the last 24 hours.`, "",
        "A batch of power supplies, or the HAT's PD contract? Close this by hand."],
      labels: ["alert", "alert:trend"], payload: null, sig: String(p.uv), link: `${base}/dashboard`,
    }, now);
    if (res.state === "opened") out.trend.push(p.platform);
  }
  return out;
}

const fmt = v => (v === null || v === undefined ? "?" : v.toFixed(2));

/** The webhook saw an alert issue closed (by hand, or by us): forget its key. */
export async function closedByHand(env, issue) {
  const m = MARK_RE.exec(String((issue && issue.body) || ""));
  if (!m || !env.SUPPORT) return false;
  const [, box, kind] = m;
  if (!box || !kind) return false;
  const key = alertKey(box, kind);
  const have = await env.SUPPORT.get(key, "json");
  if (have && have.number === issue.number) await env.SUPPORT.delete(key);
  return true;
}

/** A forgotten radio: close what is open for it (the 90-day sweep, POST /v1/forget). */
export async function forgetAlerts(env, box, why) {
  if (!alertsOn(env)) return;
  for (const kind of ["fault", "rollback", "quiet"]) {
    const have = await env.SUPPORT.get(alertKey(box, kind), "json");
    if (have && Number.isInteger(have.number)) {
      await closeIssue(env, have.number, why);
      await env.SUPPORT.delete(alertKey(box, kind));
    }
  }
}

export { MARK_RE, platformOf };
