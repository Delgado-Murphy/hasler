# The check-in endpoint

The other end of the radio's opt-in daily check-in ([docs/ota.md §5](../../docs/ota.md#5-channels-and-the-fleet)):
a Cloudflare Worker with one KV namespace for the radios, in the public repo
so an owner can read what the server does. It keeps
`box_id → {payload, last_seen, days}` and, per day, anonymous fleet counts;
the radio's IP is never read, never stored, never logged. Anything over
2 KB or not a JSON object with a plain `box_id` is refused; schema 1
(radios before 1.7) and schema 2 are both read.

| File | What |
|---|---|
| `worker.js` | the routes, the record, the daily cron |
| `model.js` | schema 1 and 2 in one shape, the daily row, the words (variants, faults, rollback reasons) |
| `fleet.js` | release health per version, versions per channel, the YouTube helper, platforms, the roll-up |
| `dashboard.js` | `/dashboard` and `/dashboard/radio/<id>` |
| `alerts.js` | alert issues (SUPPORT.md "Alerts") |
| `support.js`, `enrol.js` | Send to the makers, each radio's own word (SUPPORT.md) |

## What it keeps, and for how long

- **The latest payload**, verbatim, and when it came (to the minute; only
  here). Deleted by the daily cron 90 days after the radio was last seen,
  or at once by `POST /v1/forget` (the radio sends it when the owner
  switches the check-in off, and at the hand-over) or the dashboard's
  **Forget this radio**.
- **60 days of daily rows** inside the same key (`days`, newest first; a
  second check-in the same UTC date replaces that day's row): date only,
  version, channel, release state, health, fault words, stalls, restarts,
  haslerd restarts, YouTube helper, hottest °C, undervoltage, a power cut,
  card state. About 130 bytes a row, ~8 KB a radio.
- **`fleet:<date>`**: the cron's anonymous roll-up of yesterday (radios
  seen, versions per channel, rollbacks / faults / stalls per version, the
  helper, platforms; no box ids), kept 400 days (`expirationTtl`).

## KV budget (free tier: 1,000 writes, 100,000 reads a day)

One write per check-in (the radio's own key holds its history). The cron
writes one roll-up, plus one delete per radio past 90 days. Alerts write to
SUPPORT only when one opens, changes, starts clearing or closes - never on
a check-in that changes nothing. So the free tier carries roughly 900
radios; reads (the dashboard lists every key, plus up to 3 SUPPORT reads
per check-in for its alert state) are far from their limit.

Nothing here is deployed by the repo. The steps below are for Bill's
Cloudflare account (free tier is plenty for a few dozen radios).

## Deploy

```sh
cd tools/checkin
npm install -g wrangler                 # once
wrangler login                          # the Cloudflare account
wrangler kv namespace create CHECKINS   # prints an id: put it in wrangler.toml
wrangler secret put CHECKIN_TOKEN       # optional: a shared word the radios must send
wrangler secret put DASHBOARD_KEY       # optional: /dashboard?key=... (or use Cloudflare Access instead)
wrangler deploy                         # prints https://hasler-checkin.<account>.workers.dev
```

Test it:

```sh
curl -i -X POST https://hasler-checkin.<account>.workers.dev/v1/checkin \
  -H 'content-type: application/json' -H 'X-Hasler-Token: <the word>' \
  -d '{"schema":1,"box_id":"test-0001","short_id":"t001","variant":"hasler-hm-hat","version":"1.5.0","channel":"stable","health":{"ok":true,"failed":[]},"faults":[]}'
open 'https://hasler-checkin.<account>.workers.dev/dashboard?key=<the key>'
```

`node --test` (in this directory) runs the Worker's validation, the
dashboard's order and the support routes against a fake KV and a mocked
GitHub (Node 18+; no Cloudflare, no network).

**Send to the makers** (owners' requests as issues in a private GitHub repo,
and the makers' `/owner` replies back to the radio) is the same Worker's
`support.js`; [SUPPORT.md](SUPPORT.md) has its setup. Without the `SUPPORT`
binding and the GitHub secrets those routes answer 503 and nothing else
changes.

## On a radio

Nothing to set: every release points at `https://hasler-checkin.delgado-murphy.workers.dev`
(the root; the radio derives `/v1/checkin`, `/v1/report`, `/v1/replies`,
`/v1/enrol`). A radio without a shared word enrols its own at the owner's
first **Send to the makers** ([SUPPORT.md](SUPPORT.md#how-a-radio-gets-its-line))
and the daily check-in uses it too. To point a radio elsewhere, or keep using
the shared word, `/etc/hasler/box.yaml` (`hasler-install` writes `checkin_url`
from `checkin_url = …` in `hasler.txt`):

```yaml
checkin_url: https://hasler-checkin.<account>.workers.dev   # or …/v1/checkin; "" = nowhere
checkin_token: <the word>        # the makers' own radios, with CHECKIN_TOKEN set on the Worker
```

The switch itself is the owner's: **Settings → This radio → Updates → Tell
the makers how the radio is** (box.yaml `checkin: true`), with **See what it
sends** showing today's payload verbatim. Nothing is sent while it is off,
and nothing with `checkin_url: ""`. The radio sends once a
day after its update check, skips it on a metered connection, and shows
"Couldn't send today" on the page if the Worker is unreachable; a failed
send is never retried in a hurry.

## What the dashboard answers

Six questions, at most one look a day:

1. Is this release safe to promote to Stable? **Release health**: per version
   seen in 30 days, radios, radio-days, rollbacks, faults per 100 radio-days,
   stalls and restarts per radio-day against the version before, a daily
   stall line. Red only for rollbacks and faults; amber when stalls or
   restarts double over 10+ radio-days.
2. Which radios need a look today? The **verdict line** ("31 radios have
   checked in · 29 seen today · 2 need a look · 1 open request") and the
   table, needs-a-look first: a fault or failed check, a rollback, a full or
   read-only card, a broken YouTube helper, low voltage, heat at the fault
   line, or gone quiet for 3-14 days.
3. Did a fault trend start after a release? Release health, and each
   radio's **60-day grid** (`/dashboard/radio/<short id>`; a cream rule on
   the day the version changed).
4. Is the YouTube Music helper broken across the fleet? Its panel: works on
   N of M today, and the versions.
5. Which radios are stuck on old versions? **Versions** per channel (older
   than the newest-but-one in amber), and each row's mode and check.
6. Does this radio have an open request? The **Requests** column (`#12 open`,
   `#9 seen`; amber when open for 2+ days), from the GitHub webhook.

Plus **Platform**: Pi model, RAM and moOde per count, with low voltage beside
it. Times are Europe/Madrid, the UTC time on hover. Every coloured state also
carries a word; red is for faults only. A row older than 3 days is amber,
older than 14 grey, and the word is "not seen", never "broken": the dashboard
can't tell a radio that is off from one that is offline or whose owner
switched the check-in off. On a phone the rows become cards. `/v1/radios` is
the same rows as JSON, for `curl | jq`.

## The daily cron

`[triggers] crons = ["0 6 * * *"]` in wrangler.toml (free plan) runs
`scheduled()`: yesterday's `fleet:<date>`, the 90-day sweep, and the quiet,
YouTube and trend alerts. To run it by hand against a local dev server:
`wrangler dev --test-scheduled` and `curl 'http://localhost:8787/__scheduled?cron=0+6+*+*+*'`.
