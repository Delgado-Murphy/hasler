# Remote help

Sometimes a radio needs looking at by the people who built it, and the radio
is in someone else's home. **Let the makers help (24 h)** on the key page
opens a private link from the radio to the makers for one day, over
[Tailscale](https://tailscale.com). It is off until the owner asks, it needs a
press on the radio to start, and it ends on its own.

**Contents**

- [For owners](#for-owners)
- [For makers](#for-makers)
- [How it works](#how-it-works)
- [The API](#the-api)
- [Checking and fixing](#checking-and-fixing)

---

## For owners

### What it does

1. On the key page: **Settings → This radio → Help from the makers** (at the
   bottom), then **Let the makers help (24 h)**.
2. The lamp dips twice: press any key on the radio within 30 seconds. That
   press is how the radio knows it is you and not someone else on your Wi-Fi.
3. After a few seconds the page shows a link: **Send this link to the makers**
   (copy it, or share it by message or email).
4. One of the makers opens it and approves. The page then says
   **Connected to Delgado & Murphy's help network**, with the radio's name and
   address there and when help ends. Above that, a line says whether a maker
   is on the radio right now: *A maker is connected now (…)* or *Connected -
   waiting for a maker*.

Help ends by itself **24 hours** after you started it, or **after one
hour** if nobody has approved the link by then. It also ends when you press
**Stop** on the key page, or when the radio restarts (unplugging it counts).
To give the makers another day, start it again.

### The link is like a key

The link can be used once, by anyone who has it, to connect your radio to
*their* network. Send it to the makers only, not to a group chat. If you sent
it to the wrong person, press **Stop**: the link no longer works after that.
Once a maker has approved, the page shows which network the radio joined:
Delgado & Murphy's is `delgado-murphy.org.github`. If it shows anything else,
press Stop.

### What the makers can see and do

While help is on, the makers can reach the radio as if they were in your
home, but only the radio:

- **The key page**, the same page you use, and everything on it.
- **A command line on the radio (ssh)**, with the keys they put on it when
  they built it. With that they can read its logs, change its settings,
  install a fix and restart it. Anything you could do to the radio by
  hand, they can do.

They **can't** reach your other devices (phones, computers, the router)
through the radio. The radio doesn't take any settings from their network,
and nothing on your Wi-Fi is changed. What the radio can see (the names of
other devices on your Wi-Fi, for example) a maker on the radio can see too.
Your Wi-Fi password is stored on the radio, as it is on any device that
joins your Wi-Fi.

What the makers' network learns: the radio's name there (`hasler-<room>-<id>`,
for example `hasler-kitchen-ab12`), its addresses, and roughly where it is
on the internet. Tailscale (the company) relays the connection when it
can't be direct; it can't read the traffic, which is encrypted end to end.
The radio sends no logs to Tailscale.

### How to stop

- **Stop** on the key page, at any time. No key press needed.
- Or pull the plug and plug it back in: a restart always ends help.

When help ends the radio leaves the makers' network and forgets it; nothing
stays behind.

---

## For makers

### The tailnet

The makers' tailnet is **`delgado-murphy.org.github`** ("Delgado & Murphy's
help network" on the key page). Its members sign in through the Delgado &
Murphy GitHub org; admin console: <https://login.tailscale.com/admin>. The
radio carries **no key or secret** for it (the code is public). Which tailnet
a radio joins is decided only by the maker who approves the link: whoever
opens it while signed in somewhere else would add the radio *there*. So
check the account before you approve.

### Once: access rules (ACL) and a tag

Out of the box Tailscale lets every device on a tailnet reach every other,
so without rules a radio could reach the makers' laptops too. In the admin
console, **Access controls**, a policy along these lines lets makers reach
radios on ssh and the key page, and lets radios reach nothing:

```jsonc
{
  "groups":    { "group:makers": ["<github-user>@github", "<github-user>@github"] },
  "tagOwners": { "tag:hasler-help": ["group:makers"] },
  "acls": [
    // the makers' own devices, as before
    { "action": "accept", "src": ["group:makers"], "dst": ["autogroup:self:*"] },
    // makers -> radios: ssh and the key page only
    { "action": "accept", "src": ["group:makers"], "dst": ["tag:hasler-help:22,8080"] }
    // nothing has tag:hasler-help as src: a radio can't start a connection
  ]
}
```

(GitHub-org members appear as `<user>@github` in the Users page. Use the
names shown there.) Add rules for the makers' other machines as you need.
Tailscale SSH isn't used, so the policy needs no `"ssh"` section. Optional:
**Settings → Device management → Device approval**, so every new device,
a radio included, also waits for an admin's click under Machines.

### Approving a radio

1. The owner sends you a link like `https://login.tailscale.com/a/…`.
   Open it while signed in to **delgado-murphy.org.github** (check the
   account in the corner of the page) and press **Connect**.
2. In the admin console, **Machines**: the radio shows up as
   `hasler-<room>-<id>`, marked **Ephemeral**. If Device approval is on,
   press **Approve**. Then **⋯ → Edit ACL tags → `tag:hasler-help`**. Until
   it is tagged, only the approving maker's own devices can reach it (it
   counts as theirs).
3. The owner's page shows the radio's `100.x.y.z` address. Reach it as usual:

   ```sh
   ssh pi@100.x.y.z                 # or pi@hasler-kitchen-ab12 with MagicDNS
   open http://100.x.y.z:8080/      # the key page
   ```

   The radio's own ssh keys stay the gate: your key has to be in
   `~pi/.ssh/authorized_keys` (put it there when you build the radio, with
   password login off). Tailscale SSH is off on purpose, so a tailnet login
   alone never gives you a shell.

### After help: removing the device

- When you're done, ask the owner to press **Stop** (or let the 24 hours
  run out). The radio logs out, and because it is an **ephemeral** node
  (in-memory state, its node key never written to the SD card) logging out
  removes it from the tailnet.
- Then check **Machines**: the radio should be gone. If it is still listed
  (the radio lost power or Wi-Fi before it could log out), remove it:
  **⋯ → Remove**. Tailscale also removes an offline ephemeral node by itself
  after a while.
- Key expiry: a tagged device has key expiry switched off by Tailscale.
  That doesn't matter here, because no session outlives 24 hours and the
  node doesn't outlive the session. Nothing needs revoking afterwards.
- Leave a short note in the radio's record of what you changed.

### Things to avoid

- Don't use an auth key or tag the radio at login: either would mean a
  secret on the radio, and anyone can read the code.
- Don't enable `tailscaled.service` on a radio. The package's own service
  stays disabled; remote help runs its own copy only while the owner asks.

---

## How it works

Pieces:

| Piece | What it does |
| --- | --- |
| `scripts/bootstrap.sh` | Installs `tailscale` from Tailscale's apt repo for Debian trixie (`pkgs.tailscale.com`, `signed-by` keyring). `tailscaled.service` is masked across the install, so it never starts, then left **disabled** (not masked). |
| `/usr/local/sbin/hasler-help` (`scripts/hasler-help`) | The root helper: `start HOSTNAME`, `status`, `stop [why]`. Allowed to haslerd (user pi) by `config/sudoers/haslerd`; it checks its own arguments. |
| `web/help_api.py` | `/api/help` for the key page; start needs a press on the radio (`web/confirm.py`, action `help`). |

`hasler-help start hasler-kitchen-ab12`:

1. Sets the end first, as two transient systemd timers, so they hold even if
   haslerd dies: `hasler-help-end.timer` (24 h → `hasler-help stop expired`)
   and `hasler-help-unapproved.timer` (1 h → `hasler-help stop unapproved`,
   which does nothing once the radio is connected).
2. Starts `tailscaled` as the transient unit `hasler-help-tailscaled.service`
   with `--state=mem:` (an ephemeral node; nothing on the SD card), its other
   state in `/run/hasler-help`, and `--no-logs-no-support` (no log upload to
   Tailscale).
3. Runs `tailscale up --hostname=… --ssh=false --accept-dns=false
   --accept-routes=false` (unit `hasler-help-up.service`) **without an auth
   key**. Tailscale then has a login URL, which the helper reads from
   `tailscale status --json` and prints for haslerd.

The login URL is treated like a password: `tailscale up`'s output goes to
`/dev/null`, tailscaled's journal lines carrying it are dropped
(`LogFilterPatterns=`; with an older systemd, tailscaled's output is
dropped altogether), haslerd never logs it, and the API returns it only
while the radio waits for approval.

`hasler-help stop` runs `tailscale logout` (an ephemeral node is removed
from the tailnet), stops the transient units and timers, disables
`tailscaled.service` should anything have enabled it, removes
`/run/hasler-help`, and records why in `/run/hasler-help.last.json`. A
reboot loses the transient units and `/run`, so it ends help too: nothing
resumes after a restart, on purpose. A haslerd restart changes nothing,
because the session lives in systemd and `/run`, not in haslerd.

The lamp doesn't change while help is on. The page shows it.

---

## The API

```
GET  /api/help
  {"available": true, "state": "off|starting|waiting|connected|ending",
   "login_url": "https://login.tailscale.com/a/…" | null,   # only while waiting
   "hostname": "hasler-kitchen-ab12", "started_at": 1761000000,
   "expires_at": 1761086400, "approve_by": 1761003600,      # epoch seconds
   "address": "100.x.y.z", "dns_name": "hasler-kitchen-ab12.tailXXXX.ts.net",
   "status_text": "A maker is connected now (wmurphy@github).",
   "peers_active": [{"name": "…", "user": "wmurphy@github"}],  # live sessions, last ~2 min
   "network": "Delgado & Murphy's help network",
   "tailnet": "delgado-murphy.org.github", "expected_tailnet": null,
   "ssh": "pi@100.x.y.z", "page": "http://100.x.y.z:8080/",
   "last": {"at": …, "why": "stopped|expired|unapproved|wrong-tailnet|failed",
            "text": "The 24 hours were up."} | null,         # only when off
   "error": null}

POST /api/confirm/request {"action": "help"}  -> {"token": …}  (then a key press)
POST /api/help/start      {"confirm": token}  -> {"ok": true, ...the GET doc}
POST /api/help/stop       {}                  -> {"ok": true, ...}
```

`available` is false when Tailscale or the helper isn't installed.
`tailnet` is what the radio actually joined, shown for the owner to see.
`expected_tailnet` is `delgado-murphy.org.github` unless `web.help_tailnet`
in haslerd.yaml says otherwise (`""` switches the check off). A radio that
joins any other tailnet ends help at once (`last.why` = `wrong-tailnet`). It is an extra guard, not
what decides the tailnet: the approving maker decides that. Errors:
403 `confirm_required` (no confirmed press), 409 `confirm_unavailable` (no
panel to press), 503 the helper failed (its message in `error`). The page
polls `GET /api/help` every few seconds while it is open; the status is
cached for two seconds.

---

## Checking and fixing

```sh
sudo hasler-help status                      # JSON: the state as the helper sees it
systemctl list-timers 'hasler-help-*'        # when it ends
systemctl status hasler-help-tailscaled      # the daemon (its log has no login URL)
sudo hasler-help stop                        # end it by hand
dpkg -s tailscale | grep Version             # installed?
systemctl is-enabled tailscaled              # should say disabled
```

- *"Tailscale isn't installed"*: re-run `sudo scripts/bootstrap.sh` (it
  installs the package and adds Tailscale's apt source).
- *"tailscaled.service is running"*: someone started the package's service
  by hand. `sudo systemctl disable --now tailscaled`, then Start again.
- The page sits at *Starting…*: `journalctl -u hasler-help-tailscaled` (the
  radio needs the internet; Tailscale works through ordinary home routers).
