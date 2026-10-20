# Over-the-air updates

**In plain words.** A Hasler gets new versions of its software by itself, over
the internet. Each new version (a **release**) is signed by us, so the radio can
tell it is genuine. The radio installs it only while it rests, checks itself
afterwards, and goes back to the version it had if anything is wrong. Your keys,
settings and knob calibration are never touched.

The first half of this page is for anyone curious about how that works. The
second half ("For makers") has the detail. The exact file formats are in
[ota-format.md](ota-format.md); how we publish a release is in
[release.md](release.md). The short owner's version is in
[Using your Hasler](using.md#updates).

## What you'll notice

- **Nothing, most of the time.** The radio looks for an update once a day, at
  a random time, and downloads it quietly in the background.
- **Small updates install at night.** With updates set to *Automatic* (the
  setting for new radios), a small update (fixes, or a new feature) installs
  between 02:00 and 05:00, and only if the radio is in standby and nothing has
  played for 10 minutes. It never interrupts music.
- **Big updates ask first.** A major release, or one that changes how your
  settings are stored, always waits for you. The key page shows it under
  Settings → This radio → Updates; you pick "tonight" or "now" and press any
  key on the radio to say yes. "Now" still waits until the radio is in standby
  and has been quiet for 10 minutes.
- **The lamp breathes ember while it installs.** That means "busy". Leave it
  plugged in if you can - but if the power does go, the radio still comes back
  on a working version.
- **If something is wrong, it goes back by itself.** After the switch the radio
  checks that everything still works. If not, it returns to the version it had,
  and the key page says why.
- **You can go back by hand.** The Updates page lists the versions kept on the
  radio (up to three) with "Go back to …". You confirm with a key press, and it
  happens at once.
- **You choose how updates come:** *Automatic*, *Ask* (it downloads, then waits
  for you) or *Off* (it only looks).
- **Your things stay yours.** Keys, settings, calibration, Wi-Fi, accounts and
  the Home Assistant token live outside the software and are never replaced.
  moOde and Debian themselves are not updated this way.

## What is built, and what isn't yet

As of 0.4.5 (October 2026):

| Part | Status |
|---|---|
| Signed releases, built and signed in GitHub, published at [delgado-murphy.github.io/hasler-releases](https://delgado-murphy.github.io/hasler-releases/) | built (since 0.3.1) |
| Signing with no manual step (GitHub environments `sign` and `timestamp`) | built |
| Download, verify, install next to the running version, health check, automatic rollback, boot counter, power-cut recovery | built |
| Key page: version, check now, install tonight / now, go back, last result | built |
| Update modes `auto` / `ask` / `off` (new radios: `auto`) | built |
| Two channels, `bench` and `stable`; halting a bad release | built - every release so far has gone to `bench`; `stable` gets its index with the first promotion |
| Channel picker on the key page (with Maker tools on) | built |
| New kinds of system files in major releases, put in place by the release's own updater | built (radios on 0.4.2 or later) |
| systemd drop-ins for other services (e.g. `bluetooth.service.d/hasler-speaker.conf`) | built (first shipped in 0.4.3) |
| Lamp breathes ember while an update installs | built (0.4.3) |
| Remote help: "Let the makers help (24 h)" | in progress |
| Staged rollout (rings of radios inside a channel) | the format and the radio support it; our tools still open every ring at once |
| `config.txt` changes through the Pi's tryboot | planned - today such a release is installed by hand (§4) |
| Opt-in daily check-in | planned - the `box.yaml` key exists, nothing is sent |
| A bundled Python in the release | planned - releases use Debian trixie's Python 3.13 |
| Release key on a hardware key (YubiKey) | later |

## How it works, in one paragraph

When we tag a version, GitHub builds the release (code, Python wheels, pinned
helper programs) on an arm64 runner in a Debian trixie container. A second
workflow, in the public `hasler-releases` repository, checks that build,
**signs** it and points a channel at it. Each radio fetches its channel's
signed index once a day, verifies every signature and hash before it unpacks
anything, and prepares the release next to the running one. Then - at night in
standby, or when you say so - it switches a symlink, restarts and
health-checks. Pass: it keeps the new version. Fail, hang, boot loop or power
cut: it **goes back to the previous version, which is still on the radio**.

---

# For makers

The numbered sections are referred to from the code (`boxconfig.py`,
`software.py`, `apply.py`, ...); keep the numbers.

## 1. What lives where (config separate from updates)

| Path | What | Who writes it |
|---|---|---|
| `/opt/hasler/releases/<version>/` | code and shipped data (`app/`), `venv/`, `bin/` (go-librespot, deno), `integration/` (system files), `migrations/`, `release.json` | the updater; root-owned, read-only |
| `/opt/hasler/current`, `/opt/hasler/previous` | symlinks to releases, swapped atomically | the updater |
| `/opt/hasler/updater/` | the updater package that actually runs - replaced only when a release commits | the updater |
| `/etc/hasler/box.yaml` | `schema`, `variant`, channel, update mode, and **only overrides** of defaults | owner / key page / migration |
| `/etc/hasler/variant.d/<variant>.yaml` | **calibration overlay** keyed by control id: pot curves, hysteresis, MCP address, lamp palette/balance | the key page's calibration pages |
| `/etc/hasler/trust/` | root public key(s) and the signed keyring | install only (root, 0444) |
| `/var/lib/haslerd/` | web-written config (`keys.yaml`, `settings.json`), state, caches | haslerd |
| `/var/lib/haslerd/secrets/` (0700) | HA token, YTM cookies, account files | haslerd; never in releases, logs or API responses |
| `/var/lib/hasler-update/` | downloads, staged releases, `status.json` | the unprivileged `hasler-update` user |
| `/var/lib/hasler-apply/` | `state.json`, backups of replaced system files, config snapshots | root |
| moOde DB, Wi-Fi, Bluetooth pairings, go-librespot credentials | untouched by updates | moOde / owner |

**Precedence** (later wins; dicts deep-merged, lists replaced, controls matched
by `id`): code defaults < shipped variant < `variant.d` overlay < `box.yaml` <
web settings < secrets. Config files carry `schema: N`. A release that changes
a schema ships an idempotent migration (`daemon/haslerd/migrations/NNN_*.py`),
which the updater runs on a copy of the config before the switch.

### The keys of box.yaml

`config/box.yaml.example` is a minimal, valid file. Keep a real one short:
list only what differs from the defaults, so a later release's better default
reaches the radio.

| Key | Meaning |
|---|---|
| `schema` | Of this file (1). A release that changes it ships a migration. |
| `variant` | Which cabinet: `config/variants/<variant>.yaml` in the release. |
| `channel` | `stable` (everyone; the default) or `bench` (the makers' test radio; may break). Anything else, the retired `beta` included, reads as `stable`. |
| `update_mode` | `auto` (the default: patch/minor at night in standby; major or migration releases still ask), `ask`, `off`. An unknown word reads as `ask`. |
| `box_id` | A random UUID, made once by migration 001. Places the radio in a ring (and names it in a future check-in). |
| `checkin` | Reserved for the planned opt-in daily check-in. Nothing is sent today. |
| `metered` | `true`: never download in the background (phone hotspot, capped plans). |
| `update_url` | Bench only: a local channel (`ota-bootstrap.sh --update-url`). Without it: the public channel. |
| `migrations` | Written by `python -m haslerd.migrations --apply`: the ids applied here. Don't edit. |

The updater reads only flat `key: value` lines (it has no YAML parser). The
optional haslerd override blocks are the same as in the old
`config/haslerd.yaml` (the code has the defaults; add only what differs):

- `moode:` `mpd_host`, `mpd_port`, `mixer` (the hardware mixer, `Digital`),
  `card` (`sndrpihifiberry`), `volume_limit` (mixer ceiling %; the key page's
  saved one wins), `volume_floor` (28), `dsp_command`
  (`/usr/local/bin/hasler-dsp`, the knob effects; `""` = off), `tone_commands`
  (only without `dsp_command`), `source_flags`, `renderer_units`, the tool
  paths (`moodeutl`, `amixer`, `bluetoothctl`), `spotify:` (`enabled`,
  `base_url` `http://127.0.0.1:3678`, `timeout`, `status_timeout`) and `ytm:`
  (`enabled`, `port` 8099 - keep it fixed, `ytdlp`, `cookies`, `catalog`).
- `web:` `enabled`, `host`, `port` (8080), `hostnames`, `allow_unconfirmed`
  (bench only: no key press needed to confirm).
- `connectivity:` `host`, `port`, `timeout_s`, `interval_s` (the probe behind
  the lamp's offline look).
- `state_file`, `keys_file` (both default to `/var/lib/haslerd`).

Not in box.yaml: the Home Assistant token. Connect HA from the key page; it is
kept in `/var/lib/haslerd/secrets/ha.json` (0600).

The calibration overlay `/etc/hasler/variant.d/<variant>.yaml` is keyed by the
shipped descriptor's control ids (an unknown id is an error). Only what
differs from the release, for example:

```yaml
schema: 1
controls:
  volume:
    curve: [[25, 100], [1879, 87.5], [10882, 75], [26150, 0]]
    hysteresis_counts: 30
  tone:
    range: [0, 25500]
    taper: linear
    end_switch: {off_hold_ms: 5000}
io:
  digital: {addr: 0x27}
  lamp:
    palette: {ember: [1.0, 0.10, 0.0]}
    balance: {red: 1.0, green: 1.0, blue: 1.0}
    min_duty: {red: 0.002, green: 0.0005, blue: 0.0005}
```

## 2. Releases

- **Two repositories** in the GitHub org `delgado-murphy`: `hasler` (this code,
  public) and `hasler-releases` (public; signed artifacts only, plus the signed
  channel indexes on GitHub Pages, e.g.
  `delgado-murphy.github.io/hasler-releases/channels/stable.json`). Radios
  need no GitHub token.
- **Versions** are semver, and the version number decides the release's
  `kind` (compared with the previous tag). **Major** for a new kind of system
  file (§4), a config schema change with a migration, or a raised OS/moOde
  floor; **minor** for features with defaults; **patch** for fixes. New units,
  udev rules or helpers of a kind radios already know are fine in a minor or a
  patch. Major and migration releases are never installed without asking.
- **Build** (`release.yml`, on a tag `vX.Y.Z` or `vX.Y.Z-rc.N`): the tests,
  then a native `ubuntu-24.04-arm` runner builds in a `debian:trixie`
  container - the aarch64 wheelhouse (hash-locked, `--require-hashes`), the
  pinned go-librespot and deno, a reproducible tarball (sorted, fixed owner
  and mtime), `files.sha256`, an SBOM, a build attestation and an **unsigned**
  `release.json`. It then installs the venv offline, as a radio would. About
  3 minutes with warm caches. The build fails if the tag and
  `daemon/pyproject.toml` disagree; the release notes are the version's
  section of `CHANGELOG.md`.
- **Sign and publish** (`sign-publish.yml` in `hasler-releases`): re-checks
  the build run, the tag, the artifact digest, the attestation and the
  tarball against `release.json`, signs it with the release key, creates the
  GitHub release and points a channel at it. Nobody has to approve it: the
  keys are secrets of the `sign` and `timestamp` environments, which only
  `main` may use. The steps are in [release.md](release.md).
- **Bench first.** A release goes to `bench` (the makers' own radio) and is
  promoted to `stable` later - the *same artifact*, never rebuilt.
- A signed `release.json` is valid for 120 days. After that radios refuse it
  and it can't be promoted any more.

## 3. Security

**Three kinds of key.**

| Key | Lives | Signs | If it leaks |
|---|---|---|---|
| root | offline, never on GitHub | the keyring: which release and timestamp keys count | new trust on every radio |
| release | GitHub secret of the `sign` environment | `release.json` | a new keyring drops it; radios follow without a reinstall |
| timestamp | GitHub secret of the `timestamp` environment | the channel indexes, every day | at worst a freeze - it can't vouch for code |

There is one root key today. A radio accepts a keyring signed by any root key
it holds, so a second one can be added later. Moving the release key to a
YubiKey is a later step - the same single keyring change.

| Threat | Defence |
|---|---|
| tampered download, MITM, bad mirror | signed `release.json`; the artifact and every file hash-checked; HTTPS is transport only |
| freeze (updates withheld) | the channel index is re-signed daily and expires after 14 days; the key page says "no fresh update info" after 3 days without a good check; nothing expired is installed |
| rollback attack (an old, vulnerable release) | a monotonic `seq`, kept root-owned on the radio; anything not above it is refused. Going back fleet-wide means shipping a new, higher version |
| stolen release key | revoked by a new keyring, signed with the root key |
| stolen timestamp key | worst case a freeze: it signs indexes, never releases |
| tampered build | the signing workflow uses no third-party actions, re-downloads the artifact, checks its digest and build attestation, and that the tag still points at the commit that was built |
| dependency supply chain | wheels and binaries pinned, hashed and shipped in the release; radios never reach PyPI during an update |
| LAN attacker, malicious web page (CSRF, DNS rebinding) | every state-changing request checks `Host` and `Origin` and wants JSON; install, go back, channel, Wi-Fi and reset need a **press on the radio** within 30 s |
| physical access | out of scope; the radio holds only public keys |

**On the radio, two users split the work.** The unprivileged `hasler-update`
user fetches, verifies (signatures, keyring, expiry, seq, compatibility) and
extracts safely (no absolute paths, no `..`, no links leaving the tree, no
devices, no setuid) into a staging dir. Root (`hasler-update apply`) trusts
none of that: it verifies again, re-extracts the hash-checked artifact into a
root-owned place, and is the only thing that writes to `/opt/hasler` and
system paths. haslerd can't write the code it runs; the key page can only
start the updater's own units, and a polkit rule allows exactly those names.

## 4. Update and failsafe

1. **Check** - `hasler-update-check.timer`: daily, at a random time within
   6 hours, caught up after the radio was off. Fetches the channel index,
   decides, fetches `release.json`.
2. **Verify** - as in §3; nothing is unpacked before it passes. A halted
   release, one that failed here before, or one whose ring isn't open yet is
   skipped.
3. **Stage** - download with resume (needs twice the artifact's size free),
   hash-check, extract into `staged/<seq>/`. Harmless, any time. Skipped when
   `metered` or mode `off`.
4. **When** - mode `auto`: `hasler-update-apply.timer` tries every half hour
   from 02:00 to 04:30, patch/minor only. Otherwise the key page's approval:
   "tonight" (inside 02:00-05:00) or "now". Always only when haslerd says
   **idle**: standby, no phone casting, nothing played for 10 minutes.
5. **Install** - root copies and re-verifies the tree into `releases/<v>.tmp`,
   renames it, builds the venv offline from the release's own wheelhouse,
   precompiles, fsyncs.
6. **Pre-flight** - compatibility (arch, OS codename, Python ABI, moOde range,
   variant), the release's `selftest.py`, `visudo -cf` for sudoers,
   `udevadm verify`, the avahi file must parse. Migrations, if any, run on a
   snapshot copy of `/etc/hasler` and `/var/lib/haslerd`.
7. **Baseline** - the health probe on the *current* release, so only
   regressions count (a radio that never had a panel doesn't fail on "no
   panel").
8. **Switch** - write `phase: trial` (atomically), put the release's system
   files in place (each replaced atomically, the old copy kept), copy the
   migrated config back, swap `current`, restart haslerd. haslerd is asked
   again right before: if the radio woke up meanwhile, it waits.
9. **Health** - pass within 90 s (300 s when judged after a reboot) and keep
   passing for 60 s: haslerd running with no restarts, `/api/health`
   reporting the new version with no check worse than the baseline, MPD
   answering, the sound cards still there, go-librespot and the moOde UI as
   they were.
10. **Commit or roll back** - pass: 24 h of probation, then committed; the
    release's own updater is installed; three releases are kept. Fail: system
    files and config restored, `current` swapped back, haslerd restarted, and
    the release marked bad on this radio so it isn't tried again.

The lamp breathes ember during steps 8 to 10 (`phase: trial`). Before that
nothing has changed, and a power cut is harmless.

**Boot loops and hangs.** `hasler-update-boot.service` runs early, before
haslerd: in a trial it counts boots and rolls back at the 2nd, finishes an
interrupted rollback, and undoes a switch that a power cut cut short.
`hasler-update-resume.service` finishes judging a trial after a reboot.
haslerd's `OnFailure=` rolls back during trial and probation once haslerd has
failed for good (5 crashes in 2 minutes). The hardware watchdog
(`RuntimeWatchdogSec=15`) resets a hung radio.

**The updater guards itself.** It is stdlib-only and runs on the system
Python. A trial is always run by the updater already on the radio; the
release's updater (package, launcher and `hasler-update-*` units) is installed
only once its release commits. A broken release can't break its own rollback.

**New kinds of system files** (a new top dir under `integration/`) come only
in a major release. The updater running the trial has no rule for them, so it
leaves them out and records them as `pending_integration`; right after the
commit, the release's own updater puts them in place. A failure there never
undoes the commit - it is retried at the next night apply and at the next
boot. This needs updater 0.4.2 or later on the radio. Exact rules:
[ota-format.md](ota-format.md#integration-system-files).

**config.txt changes** (the hasler block, e.g. the lamp's PWM overlay) need a
reboot. Today the updater installs such a release only when started by hand
with `hasler-update apply --allow-reboot`; it then reboots into the trial and
the boot counter decides. The nightly timer and the key page refuse it. Using
the Pi's **tryboot** (the firmware falls back to the old `config.txt` by
itself) is planned. Separately, `hasler-bootcfg-guard` puts a known-good
`config.txt` back if a power cut ever leaves it broken.

**A power cut at any step** lands on a working version. Download and stage are
resumed or thrown away; before the trial is written nothing has changed;
after that, the boot service and the health check decide.

**Going back by hand.** The key page's "Go back to X" (confirmed on the radio)
starts `hasler-update-apply@rollback-X.service`, which goes through the same
switch and health check - at once, even while music plays. The release it
leaves is marked bad, so auto mode won't reinstall it.

## 5. Channels and the fleet

- **Two channels:** `bench` (the makers' test radio - may break) and `stable`
  (everyone). Each is a signed `channels/<channel>.json` naming its `current`
  release, the `halted` ones and optional rings.
- The key page's **Update channel** picker (Settings → This radio → Updates)
  shows when Maker tools are on (tap the version number seven times) or when
  the radio isn't on Stable. Switching asks for a press on the radio.
- **Halt** a bad release with `sign-publish` mode `halt`. Radios that haven't
  installed it never will, and drop a staged copy. Radios already running it
  stay on it until a new, higher release arrives - so ship the fix.
- **Rings** inside a channel (a staged rollout) are in the index format and
  the radio obeys them; `channel.py` still opens every ring at once.
- **Fleet check-in** (planned, opt-in): one daily
  `{random box id, variant, version, channel, last result, health}` to a tiny
  endpoint - no IPs, SSIDs, history or accounts.
- **Remote help** ("Let the makers help (24 h)" on the key page): in progress.

## Drills for each release

On the bench radio (both variants where it matters): fresh install; upgrade
from the previous release; config carried over untouched; a migration on an
old sample config; **rollback drill** (a deliberately failing health check ->
automatic revert; manual "go back"); **power-cut drill** (pull the power
during download, stage, switch and first boot); halt drill; an update deferred
while playing; offline and metered skip quietly. How to run them with dev keys
and a local channel:
[release.md](release.md#bench-box-from-the-rsync-tree-to-releases-dev-keys-local-channel).
