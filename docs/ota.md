# Over-the-air releases

How a Hasler in someone else's home gets a new version of our software -
safely, signed, without touching its owner's settings, and back to the last
good version on its own if a release breaks something. Planned 2026-10-20 from
four reviews (config inventory, update + failsafe, security, release + fleet).
Status: **plan**, nothing built yet.

## The shape in one paragraph

CI builds a release tarball (code + a wheelhouse + a bundled Python), a
maintainer **signs** its manifest with a hardware key, and it is published to a
public releases repo. Each box checks nightly, verifies the signature before
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
| `/etc/hasler/keys/` | release-signing public key + keyring | install only (root, 0444) |
| `/var/lib/haslerd/` | web-written config (`keys.yaml`, `settings.json`, both with `schema`), state (`state.json`, caches) | haslerd |
| `/var/lib/haslerd/secrets/` (0700) | HA token, YTM cookies, account files | haslerd; never in releases, logs or API responses |
| moOde DB, Wi-Fi, Bluetooth, go-librespot credentials | untouched by updates; only backed up | moOde / owner |

**Precedence** (later wins; dicts deep-merged, lists replaced, controls matched
by `id`): code defaults < shipped variant < `variant.d` overlay < `box.yaml` <
web settings < secrets. Every config file carries `schema: N`; a release ships
idempotent `migrations/NNN_*.py`, run against a snapshot, before the switch.

## 2. Releases (pipeline)

- **GitHub org `delgado-murphy`:** private `hasler` (code) + public
  `hasler-releases` (signed artifacts only; the signed channel index served by
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
- **Signing (offline):** a maintainer downloads CI's output, re-checks the
  hashes, signs `release.json` with an ed25519 (minisign-format) key held on a
  **YubiKey**, uploads the signature. A CI-held key would make one GitHub
  compromise a fleet compromise.
- **RC path:** `vX.Y.Z-rc.N` goes only to the `bench` channel (the owner's
  box) for 48 h incl. nights and standby cycles; the *same artifact* is then
  promoted by re-signing the channel index - never rebuilt.

## 3. Security

| Threat | Defence |
|---|---|
| tampered download, MITM, bad mirror | signed manifest; every artifact and file hash-checked; HTTPS is transport only |
| freeze (updates withheld) | manifest `expires` (~120 d); box shows "updates stale", never installs expired |
| rollback attack (old vulnerable release) | monotonic `seq` stored root-owned on the box; network downgrades refused unless signed as a downgrade |
| stolen release key | offline **root key** signs a keyring of 1-2 release keys (with seq + expiry); rotate/revoke by a new keyring |
| compromised laptop / CI | YubiKey touch to sign; maintainer compares CI hashes |
| dependency supply chain | wheels + binaries pinned, hashed, bundled; boxes never hit PyPI during an update |
| LAN attacker on the key page | the page can only *request* install/rollback; it takes a **press on the radio** to confirm (also gate today's unauthenticated `/api/wifi`, `/api/reset`) |
| physical access | out of scope; secrets wiped on factory reset |

On the box: an unprivileged `hasler-update` user fetches, verifies (signature,
keyring, expiry, seq, compatibility), and extracts safely (no absolute paths,
`..`, escaping links, devices or setuid) into a staging dir. A root oneshot
`hasler-apply@<seq>` re-verifies the staged tree itself and is the only thing
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

## 6. Fix now, regardless of OTA

- yt-dlp upgrades itself weekly **as root from PyPI, unpinned** - move to a
  dedicated user (later: ship it in releases).
- the key page's `/api/wifi`, `/api/reset` are unauthenticated on the LAN.
- `Hasler Tone.yml` is installed 0666.
- the daemon can write its own code (code in `/home/pi`, runs as `pi`).
- calibration lives in the shipped descriptor; `haslerd.yaml` lives in the code
  tree; paths found via `parents[n]` (`web/catalog.py`, `web/stations.py`,
  `main.py`) break once packaged.

## 7. Phases

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

## One-time org setup (owner)

GitHub org `delgado-murphy`; repos `hasler` (private) and `hasler-releases`
(public); branch protection on `master` (CI green, no force-push); Actions:
allow only pinned/approved actions, read-only default token; a `release`
environment with you as required reviewer for the publish job (on the
GitHub Free plan required reviewers only work on public repos - fine, since v1
signs and publishes from your laptop anyway); arm64 builds on
`ubuntu-24.04-arm` (never the bench Pi - it is the test target); two YubiKeys
(release key + a backup), and an offline root key kept apart (paper/age
backup in two places).
