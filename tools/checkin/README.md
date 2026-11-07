# The check-in endpoint

The other end of the radio's opt-in daily check-in ([docs/ota.md §5](../../docs/ota.md#5-channels-and-the-fleet)):
a Cloudflare Worker with one KV namespace, about a hundred lines, in the
public repo so an owner can read what the server does. It keeps
`box_id → {payload, last_seen}` and nothing else: the radio's IP is never
read, never stored, never logged. Anything over 2 KB or not a JSON object
with a plain `box_id` is refused. One page, `/dashboard`, lists the radios
sorted "needs a look first".

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

Two lines in `/etc/hasler/box.yaml` (the maker's; `hasler-install` writes
`checkin_url` from `checkin_url = …` in `hasler.txt`):

```yaml
checkin_url: https://hasler-checkin.<account>.workers.dev/v1/checkin
checkin_token: <the word>        # only with CHECKIN_TOKEN set on the Worker
```

The switch itself is the owner's: **Settings → This radio → Updates → Tell
the makers how the radio is** (box.yaml `checkin: true`), with **See what it
sends** showing today's payload verbatim. Nothing is sent while it is off,
and nothing is ever sent without a `checkin_url`. The radio sends once a
day after its update check, skips it on a metered connection, and shows
"Couldn't send today" on the page if the Worker is unreachable; a failed
send is never retried in a hurry.

## What the dashboard answers

Four questions, and no more should be built: the version spread before
promoting a release; who rolled back the last release and why; an SOS whose
owner hasn't noticed; a radio not seen for days. A row older than 3 days is
amber, older than 14 grey, and the word is "not seen", never "broken": the
dashboard can't tell a radio that is off from one that is offline or whose
owner switched the check-in off. `/v1/radios` is the same table as JSON, for
`curl | jq`.

What a row holds (all from the payload; see `update/hasler_update/checkin.py`
for the exact list and the keys that are never sent): the radio's short id,
variant, version, channel, when it was last seen, health and the failed
checks, the lamp's fault reasons, a rollback and its raw reason, power cuts
this month, stalls today, player restarts in the last hour, free space,
CPU temperature and the throttle bits, the yt-dlp lane, uptime in days, how
it was last switched off, the update mode. No IP, no SSID, no hostname, no
room, no place, nothing about what plays.
