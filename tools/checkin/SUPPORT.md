# Send to the makers: owners' requests as GitHub issues

Owners don't have GitHub accounts and never need one. On a radio with a
`checkin_url`, the **Tell the makers** sheet has **Send to the makers**:
the radio POSTs its report (the same text and JSON twin as Share/Copy,
secrets already stripped by `daemon/haslerd/web/report.py`) and the owner's
line to this Worker's `/v1/report`. The Worker opens an issue in a
**private** repo, and a maker's reply comes back to the radio as a note
under **From the makers**.

```
radio ──POST /v1/report (X-Hasler-Token)──▶ Worker ──REST──▶ private repo: issue #12
radio ◀─"Sent · request #12"───────────────┘
maker comments "/owner A new card is in the post." on #12
GitHub ──webhook issue_comment (HMAC)──▶ Worker /v1/github ──▶ KV replies:<box_id>
radio (page opens This radio, or after the daily check-in) ──GET /v1/replies──▶ note "by: makers" ──ack──▶ Worker
```

What the Worker does (`support.js`, read it - it is short):

- **Title** `<short id> · <first line of the note>` (or the lamp's words
  when there is no note). **Body**: the report text in a code block, the
  JSON twin folded under it, a hidden `<!-- hasler-box: … -->` marker.
- **Labels**: `owner-report`, `fault:<reason>` per lamp fault,
  `version:<v>`, `model:<variant>`. If GitHub refuses a label (422) the
  issue is opened without labels rather than lost.
- **At most 5 a day per radio** (429 after; the page says to use Share/Copy).
- **De-dup**: the same radio sending the same note within 10 minutes adds a
  comment ("Sent again from the radio") to the issue it already made, and
  the radio shows the same number.
- **Replies**: a comment with a line starting `/owner ` (anything after it,
  to the end of the comment, up to 600 characters) is queued for that
  issue's radio. Everything else on the issue - triage, notes between the
  two of you - stays private; the owner sees only `/owner` text. Closing
  with such a comment works too (GitHub sends the comment first).
- Never reads or stores the radio's IP. The issue body is what the owner's
  sheet showed: no passwords, tokens, phone names or accounts; the Wi-Fi
  name only if the owner left **Include my Wi-Fi name** ticked.
- The dashboard's **requests** column links each radio's last issues.

## Setup (once)

1. **The private repo.** Create `Delgado-Murphy/hasler-support`,
   **Private**, no template. (Another name: set `SUPPORT_REPO` in
   `wrangler.toml`.) Never point this at `hasler` or any public repo: the
   reports carry the owner's words and the radio's state.

2. **Labels** (optional but tidy; GitHub creates missing ones itself when
   the token may): `owner-report`, and as they come `fault:storage`,
   `fault:panel`, `fault:sound`, `fault:heat`, `fault:update`. `version:*`
   and `model:*` appear on their own.

   ```sh
   gh label create owner-report -R Delgado-Murphy/hasler-support -c F3CF8C -d "From a radio's Send to the makers"
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

   `CHECKIN_TOKEN` (README.md) gates `/v1/report` and `/v1/replies` too.

5. **The webhook** (hasler-support → Settings → Webhooks → Add webhook):
   payload URL `https://hasler-checkin.<account>.workers.dev/v1/github`,
   content type `application/json`, secret = the one from step 4, **Let me
   select individual events → Issue comments** only. GitHub's ping should
   show a green tick (the Worker answers 204).

6. **Radios**: nothing new. Any radio with `checkin_url` (and
   `checkin_token`) in `/etc/hasler/box.yaml` and a `box_id` shows **Send to
   the makers**. The daily check-in switch doesn't need to be on.

## Try it

```sh
W=https://hasler-checkin.<account>.workers.dev
curl -s -X POST $W/v1/report -H 'content-type: application/json' -H 'X-Hasler-Token: <the word>' \
  -d '{"schema":1,"box_id":"test-0001","short_id":"t001","note":"A test from the bench","text":"Hasler · bench · radio t001\n"}'
# -> {"ok":true,"number":1,"deduped":false}; comment "/owner It works." on issue #1, then:
curl -s "$W/v1/replies?box_id=test-0001" -H 'X-Hasler-Token: <the word>'
```

`node --test` in this directory runs the routes against a fake KV and a
mocked GitHub.

## Answering

- Read the issue; the first six lines are what you'd ask on the phone.
- Reply to the owner with `/owner …` in plain words (the rule for notes:
  name parts and settings, never people). It reaches the radio the next
  time the owner opens This radio, or after its daily check-in.
- Everything else in the thread is yours. Close the issue when done.
- If the owner has remote help on, the report's last line says so.
