# Send to the makers: owners' requests as GitHub issues

Owners don't have GitHub accounts and never need one. On every radio
**Write to the makers** (a sheet, and a page under This radio) is a field and
**Send to the makers** ("Goes to Delgado & Murphy only. They'll answer here."): the radio
POSTs its report (the same text and JSON twin as Share/Copy,
secrets already stripped by `daemon/haslerd/web/report.py`) and the owner's
line to this Worker's `/v1/report`. The Worker opens an issue in a
**private** repo, and a maker's reply comes back to the radio as a note
under **From the makers**.

```
radio ──POST /v1/enrol {box_id} (first send only)──▶ Worker ──▶ its own token
radio ──POST /v1/report (X-Hasler-Token)──▶ Worker ──REST──▶ private repo: issue #12
radio ◀─"Sent · request #12"─────────────┘
radio ──POST /v1/reply {issue: 12, note} (the owner writes back)──▶ Worker ──▶ a comment on #12 (reopened if closed)
maker comments "/owner A new card is in the post." on #12
maker labels #12 "seen" … closes #12
GitHub ──webhook issue_comment, issues (HMAC)──▶ Worker /v1/github ──▶ KV replies:<box_id>
radio (every half hour while a request is open; every minute while its page is open) ──GET /v1/replies──▶ note "by: makers" ──ack──▶ Worker
```

On the radio the owner sees **Your requests** on the Write to the makers page
(This radio → Help from the makers, and Something's wrong?): each request as a
thread - their words, your `/owner` replies, their write-backs, in order - with
its state (Sent / Seen / Answered / Done) and **Write back**.
A new reply also shows as **New reply from the makers** under Needs attention
and as a dot on the front page's gear. A send without internet waits on the
radio and goes by itself once it's back.

What the Worker does (`support.js`, read it - it is short):

- **Title** `<short id> · <first line of the note>` (or the lamp's words
  when there is no note). **Body**: the report text in a code block, the
  JSON twin folded under it, a hidden `<!-- hasler-box: … -->` marker.
- **Labels**: `owner-report`, `fault:<reason>` per lamp fault,
  `version:<v>`, `model:<variant>`. If GitHub refuses a label (422) the
  issue is opened without labels rather than lost.
- **At most 5 a day per radio** (429 after; the page says to use Share/Email),
  at most `REPORTS_ALL_PER_DAY` (200) a day across radios using their own
  token, and at most `ENROL_PER_HOUR` (30) new radios an hour.
- **De-dup**: the same radio sending the same note within 10 minutes adds a
  comment ("Sent again from the radio") to the issue it already made, and
  the radio shows the same number.
- **Replies**: a comment with a line starting `/owner ` (anything after it,
  to the end of the comment, line breaks and indentation kept, up to 2000
  characters) is queued for that issue's radio; the page shows GitHub-style
  Markdown, safely (bold, italic, strikethrough, `code` and fenced code,
  quotes, nested and task lists, headings, rules, https and mailto links;
  images as a link, tables and HTML as plain text). Editing that comment sends
  the new words in place of the old. Everything else on the issue - triage, notes between the
  two of you - stays private; the owner sees only `/owner` text. Closing
  with such a comment works too (GitHub sends the comment first).
- **The issue's state** (the `issues` event) reaches the owner too, as a
  note and on the request (a plain `/owner` reply makes it **Answered**):

  | On GitHub | The owner reads | The page says |
  |---|---|---|
  | (just sent) | - | Request #12 · Sent · 7 Nov |
  | label **`seen`** (once per issue, however often it's re-added) | The makers have seen your request #12. | Request #12 · Seen · 7 Nov |
  | **closed** | Your request #12 is done. *(+ the last `/owner` text, if the radio hadn't fetched it yet - so "close with comment" arrives as one note)* | Request #12 · Done · 7 Nov |
  | **reopened** | The makers reopened your request #12. | Request #12 · Seen · … |

- **Write back**: the owner's follow-up (`POST /v1/reply`) is a comment on
  the same issue, their lines quoted (`> `), so nothing they write can come
  back as an `/owner` line. A closed issue opens again, quietly (no
  "reopened" note goes back); at most 20 a day per radio.

  Other labels, assignments and edits of the issue say nothing to the owner. A `seen`
  label added after closing doesn't undo "done"; reopening does.
- **Reactions say nothing.** GitHub sends no webhook for a 👀 or 👍 on an
  issue, so the Worker can't hear them (polling every issue for reactions
  isn't worth it). The **`seen` label is the 👀**: add it when you've read
  the request and haven't answered yet.
- Never reads or stores the radio's IP. The issue body is what the owner's
  sheet showed: no passwords, tokens, phone names or accounts; the Wi-Fi
  name only if the owner left **Include my Wi-Fi name** ticked.
- The dashboard's **requests** column links each radio's last issues.

## Setup (once)

1. **The private repo.** Create `Delgado-Murphy/hasler-support`,
   **Private**, no template. (Another name: set `SUPPORT_REPO` in
   `wrangler.toml`.) Never point this at `hasler` or any public repo: the
   reports carry the owner's words and the radio's state.

2. **Labels** (GitHub creates missing ones itself when the token may):
   `owner-report`, **`seen`** (yours to add: it tells the owner), and as
   they come `fault:storage`, `fault:panel`, `fault:sound`, `fault:heat`,
   `fault:update`. `version:*` and `model:*` appear on their own.

   ```sh
   gh label create owner-report -R Delgado-Murphy/hasler-support -c F3CF8C -d "From a radio's Send to the makers"
   gh label create seen -R Delgado-Murphy/hasler-support -c 0E8A16 -d "Tells the owner: the makers have seen your request"
   for f in storage panel sound heat update; do gh label create "fault:$f" -R Delgado-Murphy/hasler-support -c B60205; done
   ```

3. **A fine-grained token** (GitHub → Settings → Developer settings →
   Fine-grained tokens): resource owner **Delgado-Murphy**, repository
   access **Only select repositories → hasler-support**, permissions
   **Issues: Read and write** (Metadata: Read is added by GitHub). Nothing
   else. Expiry: a year; put the renewal in the calendar.

4. **The Worker's KV and secrets** (in `tools/checkin/`):

   ```sh
   wrangler kv namespace create SUPPORT        # put the id in wrangler.toml's SUPPORT block (uncomment it)
   wrangler secret put GITHUB_TOKEN            # the fine-grained token
   openssl rand -hex 32                        # the webhook secret - keep it for step 5
   wrangler secret put GITHUB_WEBHOOK_SECRET   # paste it
   wrangler deploy
   ```

   `CHECKIN_TOKEN` (README.md), the makers' shared word, still opens
   `/v1/checkin`, `/v1/report` and `/v1/replies` for any radio that has it in
   box.yaml; every other radio uses its own token (below).

5. **The webhook** (hasler-support → Settings → Webhooks → Add webhook):
   payload URL `https://hasler-checkin.<account>.workers.dev/v1/github`,
   content type `application/json`, secret = the one from step 4, **Let me
   select individual events → Issue comments** and **Issues** (closed,
   reopened and labeled are the ones it reads; the rest are ignored).
   GitHub's ping should show a green tick (the Worker answers 204). By API:
   `gh api -X PATCH repos/Delgado-Murphy/hasler-support/hooks/<id> -f 'events[]=issue_comment' -f 'events[]=issues'`.

6. **Radios**: nothing to do. Every release points at this Worker (the
   default `checkin_url`, `https://hasler-checkin.delgado-murphy.workers.dev`),
   so every radio with a `box_id` shows **Send to the makers**. box.yaml can
   name another server (the root or `…/v1/checkin`, either works) or switch
   it off with `checkin_url: ""`. The daily check-in switch doesn't need to be on.

## How a radio gets its line

A single token in the public repo would be no secret, so each radio gets its
own ("trust on first use", `enrol.js`):

- At the owner's first **Send to the makers** the radio POSTs
  `/v1/enrol {"schema": 1, "box_id": "<its UUID>"}`. The Worker mints 32
  random bytes, keeps only their SHA-256 under `radio:<box_id>` in the
  SUPPORT namespace, and answers the token once. The radio keeps it in
  `/var/lib/haslerd/secrets/support.json` (0600; the hand-over keeps it, it
  is the cabinet's) and sends it as `X-Hasler-Token` for reports, replies
  and the daily check-in.
- From then on that radio's routes need its token (or the shared word):
  nobody else can read its replies or report as it, even with no
  `CHECKIN_TOKEN` set.
- `box_id` is a random UUID written by `hasler-install`; a new card is a new
  radio and enrols afresh. An id that's already enrolled is refused (409),
  and the owner reads "The makers' server doesn't know this radio any more…
  they can reset the radio's line". That happens when a radio was
  reinstalled with its old `box.yaml` (the spare card's memory) but without
  its secrets. **To reset**: `wrangler kv key delete --binding SUPPORT
  "radio:<box_id>"` (the box_id is in the issue's hidden marker); the radio
  enrols again at its next send. A radio whose token the Worker no longer
  knows (403) asks once more by itself.
- Abuse: the IP is still never read. Minting ids is capped by
  `ENROL_PER_HOUR`, reports by each radio's 5 a day and the fleet's
  `REPORTS_ALL_PER_DAY`. If that's ever not enough, add a Cloudflare rate
  limiting rule on `/v1/enrol` and `/v1/report` (it counts IPs at the edge,
  not in the Worker).
- The makers' own radios can keep `checkin_token` (the shared word) in
  box.yaml; it works exactly as before and isn't counted in the fleet cap.

## Try it

```sh
W=https://hasler-checkin.<account>.workers.dev
curl -s -X POST $W/v1/report -H 'content-type: application/json' -H 'X-Hasler-Token: <the word>' \
  -d '{"schema":1,"box_id":"test-0001","short_id":"t001","note":"A test from the bench","text":"Hasler · bench · radio t001\n"}'
# -> {"ok":true,"number":1,"deduped":false}; comment "/owner It works." on issue #1, then:
curl -s "$W/v1/replies?box_id=test-0001" -H 'X-Hasler-Token: <the word>'
# A radio's own word (a throwaway UUID; delete radio:<it> afterwards):
curl -s -X POST $W/v1/enrol -H 'content-type: application/json' -d '{"schema":1,"box_id":"00000000-0000-4000-8000-000000000001"}'
# -> {"ok":true,"token":"…"}; the same again -> 409 {"ok":false,"error":"already enrolled"}
```

`node --test` in this directory runs the routes against a fake KV and a
mocked GitHub.

## Answering

- Read the issue; the first six lines are what you'd ask on the phone.
- Reply to the owner with `/owner …` in plain words (the rule for notes:
  name parts and settings, never people). It reaches the radio within half
  an hour (sooner when the owner opens the page) and shows in the request's
  thread under Your requests, with a dot on the gear until it's read.
- Read it but can't answer yet? Add the **`seen`** label: the owner reads
  "The makers have seen your request #12".
- Everything else in the thread is yours. **Close** the issue when done: the
  owner reads "Your request #12 is done." (close with an `/owner` comment to
  add a line). Reopening tells them too.
- If the owner has remote help on, the report's last line says so.

## Alerts

The same Worker opens issues labelled **`alert`** in the same repo when the
daily check-ins say something needs a look (`alerts.js`). Nothing to set up
beyond the token above; `ALERTS = "off"` in wrangler.toml's `[vars]` mutes
them, and `DASHBOARD_URL` is where their links point.

| Label | Fires when | Closes |
|---|---|---|
| `alert:fault` | a lamp fault now or in the last 24 h, or a failed health check | by itself, after 2 check-ins in a row without one |
| `alert:rollback` | a radio went back from a new version (a new failed version opens a new one) | by hand, when the fix ships |
| `alert:quiet` | a radio seen on 5 of its last 7 days hasn't checked in for 3 days (daily cron) | by itself, at its next check-in |
| `alert:youtube` | the YouTube Music helper is broken on 2 or more radios and 20 % or more of today's (daily cron) | by itself, when the cron sees fewer |
| `alert:trend` | a version with 10+ radio-days has twice the stalls, restarts or haslerd restarts per radio-day of the one before, or 2+ radios of one platform saw low voltage (daily cron) | by hand |

Also on each: `version:<v>`, `model:<variant>`, and **`bench`** for a Bench
radio's fault or rollback, so the makers' own desk radio can be filtered out.
One open issue per radio and kind; a repeat is a comment only when it
changes (a new fault word, a new version); nothing at all when nothing
changed. Alert issues are never the owner's: no `/owner` reply goes to a
radio from one, and closing one by hand lets a new one open later (the
issue's `<!-- hasler-alert -->` line is how the Worker knows it).
