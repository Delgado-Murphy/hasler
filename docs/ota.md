# Over-the-air releases

**What this is, in plain words.** A Hasler in someone's home gets new versions
of our software by itself, over the internet. Each new version (a **release**)
is signed by us, so the radio can tell it is genuine. The radio installs it only
while it sleeps, checks itself afterwards, and goes back to the previous version
on its own if anything is wrong. The owner's keys, settings and knob calibration
are never touched.

Owners do not need this page: the short version is in
[Using your Hasler](using.md#updates). This page is for contributors and
maintainers. The exact file formats are in [ota-format.md](ota-format.md); the
steps to publish a release are in [release.md](release.md).

**Status (October 2026):** the first version shipped in release **0.3.1**. What
is built and what is still planned:

| Part | Status |
|---|---|
| Settings kept outside the release (`/etc/hasler`, calibration overlay, secrets) | built |
| CI build, signing in GitHub, public `hasler-releases` channel | built |
| Stage, switch, health check, automatic rollback, boot counter | built |
| Key page: version, check, install tonight / now, put off, go back | built |
| Update mode `auto` / `ask` / `off` (shipped default: `ask`) | built |
| Channels `bench` and `stable`, halt a bad release | built |
| `beta` channel and rings of boxes | planned |
| tryboot for `config.txt` changes | planned |
| Opt-in daily check-in, remote help via Tailscale | planned |
| Release key on a YubiKey; bundled Python | planned |

**Owner decisions (2026-10-20):** updates install automatically for patch/minor
releases (at night, in standby) and ask - with a press on the radio - for major
or migration releases; the daily fleet check-in is opt-in, asked at setup;
remote help via Tailscale is opt-in and off by default; **the release signing
key lives in GitHub for now** (see "Signing for v1" below), YubiKeys later.

## The shape in one paragraph

CI builds a release tarball (code + a wheelhouse; a bundled Python later), a
maintainer approves a separate workflow that **signs** its manifest (with a key
held in GitHub for v1, a hardware key later), and it is published to a public
releases repo. Each box checks daily, verifies the signature before
touching anything, stages the release next to the running one, and - only
while the radio is in standby - switches a symlink, restarts, and health-checks.
Pass: it commits. Fail, hang, boot loop or power cut: it **rolls back to the
previous release that is already on the box**. Owner settings, calibration,
state and secrets live outside the release tree and are never overwritten.
moOde and Debian are not updated by this mechanism.

## 1. What lives where (config separate from updates)

| Path | What | Who writes it |
|---|---|---|
| `/opt/hasler/releases/<version>/` | code, assets, variants (as shipped), `venv/`, bundled `python/`, `bin/` (go-librespot, deno), `integration/` (units, udev, sudoers, helpers, config.txt block, moOde patchers), `migrations/`, `manifest.json` | the updater; root-owned, read-only |
| `/opt/hasler/current` -> one release | atomic symlink swap; the previous release is kept | the updater |
| `/etc/hasler/box.yaml` | `schema`, `variant`, channel, update mode, and **only overrides** of defaults (mixer, card, volume floor/limit, tone commands...) | owner / migration |
| `/etc/hasler/variant.d/<variant>.yaml` | **calibration overlay** keyed by control id: pot curves, hysteresis, MCP address, lamp palette/balance | owner (today these live in the shipped descriptor - must move) |
| `/etc/hasler/trust/` | release-signing public key + keyring | install only (root, 0444) |
| `/var/lib/haslerd/` | web-written config (`keys.yaml`, `settings.json`, both with `schema`), state (`state.json`, caches) | haslerd |
| `/var/lib/haslerd/secrets/` (0700) | HA token, YTM cookies, account files | haslerd; never in releases, logs or API responses |
| moOde DB, Wi-Fi, Bluetooth, go-librespot credentials | untouched by updates; only backed up | moOde / owner |

**Precedence** (later wins; dicts deep-merged, lists replaced, controls matched
by `id`): code defaults < shipped variant < `variant.d` overlay < `box.yaml` <
web settings < secrets. Every config file carries `schema: N`; a release ships
idempotent `migrations/NNN_*.py`, run against a snapshot, before the switch.

## 2. Releases (pipeline)

- **GitHub org `delgado-murphy`:** `hasler` (code, this repository; now
  public) + public `hasler-releases` (signed artifacts only; the signed channel index served by
  GitHub Pages, e.g. `delgado-murphy.github.io/hasler-releases/channels/stable.json`,
  later behind our own domain) - boxes need no GitHub token. CI fails if the tag
  and `pyproject.toml` disagree; `CHANGELOG.md` (Keep a Changelog) per release.
- **Versions:** semver. MAJOR = config schema / descriptor format / system
  integration (units, sudoers, udev, config.txt, apt deps) / raised OS or moOde
  floor; MINOR = features with defaults; PATCH = fixes.
- **CI (GitHub Actions):** every push: pytest + ruff. On a tag: an
  `ubuntu-24.04-arm` job in a `debian:trixie` container builds the aarch64
  wheelhouse (lgpio built once and vendored - no compiler on boxes),
  hash-locked deps (`--require-hashes`), bundles a pinned Python, writes the
  tarball (reproducible: sorted, fixed owner/mtime), `files.sha256`, an SBOM
  and an **unsigned** `release.json`.
- **Signing:** v1 signs in GitHub (see §3 "Signing for v1"). The later,
  stronger path: a maintainer downloads CI's output, re-checks the
  hashes, signs `release.json` with an `ed25519-sk` key (OpenSSH signature) held on a
  **YubiKey**, uploads the signature. A CI-held key would make one GitHub
  compromise a fleet compromise.
- **RC path:** `vX.Y.Z-rc.N` goes only to the `bench` channel (the owner's
  box) for 48 h incl. nights and standby cycles; the *same artifact* is then
  promoted by re-signing the channel index - never rebuilt.

## 3. Security

**Signing for v1 (key in GitHub).** The release key is an ed25519 OpenSSH key
stored only as a secret of a `sign` environment in the **public**
`hasler-releases` repo (required reviewer = the owner works on public repos on
the Free plan). The signing workflow is separate from the build, uses no
third-party actions (only `ssh-keygen` and `curl`), takes the artifact's
SHA-256 as input, re-downloads and checks it and its build attestation, and
signs `release.json`. The **root key never goes to GitHub** - an encrypted file
plus a paper backup, used only to sign `keyring.json`. A leaked release key is
revoked and replaced by a new keyring, with no reinstall on the boxes; moving
the release key to a YubiKey later is the same one keyring change.

| Threat | Defence |
|---|---|
| tampered download, MITM, bad mirror | signed manifest; every artifact and file hash-checked; HTTPS is transport only |
| freeze (updates withheld) | the channel index is re-signed daily by a CI **timestamp key** and expires after 14 days; manifests expire too; box shows "no fresh update info", never installs expired |
| rollback attack (old vulnerable release) | monotonic `seq` stored root-owned on the box; network downgrades refused unless shipped as a new, higher-`seq` release |
| stolen release key | two offline **root** YubiKeys (2-of-2) sign a `keyring.json` of release + timestamp keys with validity windows; rotate/revoke by a new keyring |
| stolen timestamp key (lives in CI) | worst case a freeze, never new code - it can only sign `{seq, manifest sha256, expires}` |
| compromised laptop / CI workflow | release key on a YubiKey (touch to sign); before signing: verify the build attestation, rebuild locally and compare SHA-256, diff the install destinations against the last release |
| dependency supply chain | wheels + binaries pinned, hashed, bundled; boxes never hit PyPI during an update |
| LAN attacker / malicious web page (CSRF, DNS rebinding) | every POST checks `Origin`/`Host` and wants JSON; install/rollback need a **press on the radio** within 30 s (also gate today's unauthenticated `/api/wifi`, `/api/reset`); a LAN PIN later |
| physical access | out of scope; the box holds only public keys; secrets wiped on factory reset |

**Signing tool:** OpenSSH signatures (`ssh-keygen -Y sign -n hasler-release`)
with `ed25519-sk` keys on YubiKeys - touch to sign, verified on the box with
the stock openssh-client, the namespace stops reuse elsewhere. Keys on the box:
`/etc/hasler/trust/` (root pubkeys + keyring, root 0444). Move to python-tuf
only if the fleet or team grows.

On the box: an unprivileged `hasler-update` user fetches, verifies (signature,
keyring, expiry, seq, compatibility), and extracts safely (no absolute paths,
`..`, escaping links, devices or setuid) into a staging dir. A root oneshot
`hasler-update-apply@<seq>` re-verifies the staged tree itself and is the only thing
that writes to `/opt/hasler` and system paths. The daemon can no longer write
the code it runs (today it can: the code lives in `/home/pi`).

## 4. Update and failsafe

1. **Check** - `hasler-update-check.timer`, daily, randomised, persistent;
   downloads with resume; needs 2x free space.
2. **Verify** - as above; nothing unpacked before it passes.
3. **Stage** - extract to `releases/<v>.tmp`, build the venv offline at its
   final path, precompile, fsync, rename. Harmless; any time.
4. **Pre-flight** - compatibility (moOde range, Pi model, Python ABI),
   `python -m haslerd.selftest` (imports, parses `/etc/hasler` with the new
   schema, dry-runs migrations), `visudo -cf`, `udevadm verify`.
5. **When** - only in **standby with nothing played for 10 min**, in the
   02:00-05:00 window or on "Update now"; never mid-music. haslerd can veto.
6. **Baseline** - run the health probe on the current release first, so only
   regressions count (a box that never had a panel doesn't fail on "no panel").
7. **Switch** - write `{phase: trial, from, to, boots: 0}` (tmp + fsync +
   rename), `hasler-integrate apply <to>` (declarative units/udev/sudoers/
   patches, each replaced atomically, old copy kept), swap `current`, restart.
8. **Health** - within 90 s and holding for 60 s: haslerd active with
   READY=1 and no restarts, `/api/health` 200 reporting the new version, MPD
   answering, the amp card present, panel ok (or allowed-missing per
   baseline), lamp ok, go-librespot as before, moOde UI 200.
9. **Commit or roll back** - pass: committed + 24 h probation, keep 3
   releases. Fail: `integrate apply <from>`, swap back, restart, mark `<to>` bad.

**Boot loops and hangs:** an early `hasler-update-boot.service` counts boots
while in trial and rolls back before haslerd starts at the 2nd; haslerd's
`OnFailure=` rolls back during trial/probation; the hardware watchdog
(`RuntimeWatchdogSec=15`) resets a hung box. The updater is small, stdlib-only,
runs on the bundled Python, and a new updater is only used after its release
passes.

**config.txt changes** (e.g. the lamp's PWM overlays) use the Pi's
**tryboot**: the candidate goes to `tryboot.txt`, the box reboots once into it;
any hang or power cut falls back to the old `config.txt` by firmware. Promote
only after the trial boot passes. Such releases apply in the night window only.

**Power cut at any step** lands on a working version: download/stage resume or
are discarded; before the state write nothing changed; after it the boot
counter or health check decides; tryboot falls back by itself.

**Manual rollback:** the key page lists kept releases with a "Go back to X"
button (confirmed on the radio), run through the same switch + health path.

## 5. Channels and the fleet

- Channels **bench** (owner's box) / **beta** (1-2 family boxes) / **stable**,
  in a signed `channels.json`: per channel `current`, `min_version`, `halted`.
  With a handful of boxes: explicit rings by box id, promoted by hand.
- **Halt** a bad release by signing a new index; **pull** moves `current`
  back (installed as a normal, signed update).
- **Update mode** per box: `auto` (patch/minor at night), `ask`, `off`; MAJOR
  and migration releases always ask.
- **Key page "About this radio":** version, channel, last check, "update
  available" + changelog, "install tonight / now", last result (ok / rolled
  back and why), "go back to X".
- **Lamp:** nothing during check/download; installs happen in standby, and
  the box **holds ember - never dark - until the update is committed** (dark
  means safe to unplug); pressing a key aborts the update until tomorrow; a failed update never shows red on its own (nobody asked) -
  it goes to the page. Optional later: one slow amber swell at the first wake
  after an update.
- **Fleet visibility (opt-in):** a daily check-in `{random box id, variant,
  version, channel, last result, health}` to a tiny endpoint (e.g. a
  Cloudflare Worker) - no IPs, SSIDs, history or accounts.
- **Remote help (opt-in, off by default):** Tailscale installed but only
  started by "Let the makers help (24 h)" on the key page; ACL-limited, ephemeral.

## 6. Fixed alongside OTA (done in 0.3.1)

These problems were found during the OTA reviews and are now fixed:

- yt-dlp upgraded itself weekly as root, from PyPI, unpinned. Now it runs as its
  own user `ytdlp` and switches only after a self-check.
- the key page's `/api/wifi` and `/api/reset` were open to anyone on the LAN.
  Now every state-changing request checks `Host` and `Origin`, and Wi-Fi, reset,
  install and go-back need a press on the radio.
- `Hasler Tone.yml` was installed 0666. Now it is owned by `pi`, 0644.
- the daemon could write its own code (code in `/home/pi`). Now releases are
  root-owned and read-only in `/opt/hasler`.
- calibration lived in the shipped descriptor, and paths were found with
  `parents[n]`. Now calibration is in `/etc/hasler/variant.d/` and paths are
  package-relative.

## 7. Phases

The original plan. See the status table at the top for what is done.

- **v1** - config separation (`/etc/hasler`, `variant.d` overlay, secrets dir,
  schema 1, migration 001 for the bench box); package-relative paths; CI
  build + wheelhouse + bundled Python; offline YubiKey signing; versioned
  releases + symlink + health check + automatic rollback + boot counter;
  `hasler-integrate`; channels bench + stable with halt; key page version /
  update / last result / go back (radio-confirmed); mode `ask` by default;
  the "fix now" list.
- **v2** - beta channel and rings, `auto` mode, migrations framework beyond
  001, tryboot for config.txt, fleet check-in + dashboard, Tailscale help,
  yt-dlp in releases.
- **Later** - python-tuf with 2-of-n signing, coordinated OS campaigns,
  A/B root filesystem only if SD corruption appears.

## Test matrix (each release)

Bench box, both variants: fresh install; upgrade from N-1 (and N-2 if
allowed); config carried over untouched; migration on an old sample config;
**rollback drill** (deliberately failing health check -> automatic revert;
manual "go back"); **power-cut drill** (pull power during download, stage,
switch, first boot); halt drill; update deferred while playing; offline and
metered skip quietly.

## One-time org setup

Moved to [release.md, "One-time org setup"](release.md#one-time-org-setup-owner-once-the-org-exists),
which has the full, current steps.
