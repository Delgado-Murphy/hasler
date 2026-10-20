# OTA formats - the contract between the release builder and the radio

*For people who work on the release tools or the updater.* This page describes,
field by field, what travels from our build to a radio - the release tarball,
its signed `release.json`, the keys and the channel index - and what the radio
keeps about it. The build side (`scripts/release/`, `sign-publish.yml`) and the
radio side (`update/hasler_update/`) both follow it, so a change here is made on
both sides at once. Why it works this way: [ota.md](ota.md).

## Release tarball `hasler-<version>-arm64.tar.zst`

Built by `scripts/release/build_release.py` from an allowlist of repo paths at
a git ref, never from a working tree. Reproducible: sorted entries, uid/gid 0,
mtime = `SOURCE_DATE_EPOCH` (the tag's commit time), zstd. No setuid, setgid
or sticky bits, no group/other write, no devices, FIFOs or hard links;
symlinks only relative and inside the tree.

```
hasler-<version>/
  manifest.json      # release.json without "artifacts" (informational)
  files.sha256       # "<sha256>  <octal mode>  <path>" for every file but itself
  app/               # haslerd/ (incl. web/static), hasler_update/ (the updater),
                     # config/ (variants, catalog, stations, camilladsp, snapcast,
                     # captive, go-librespot, moode, examples), CHANGELOG.md
  wheelhouse/        # every wheel for daemon[pi], aarch64 cp313
  requirements.lock  # pip --require-hashes lock: the venv is built offline from it
  bin/               # pinned go-librespot and deno
  integration/       # system files, see below
  migrations/        # NNN_name.py (a copy of app/haslerd/migrations/)
  selftest.py        # python selftest.py --etc DIR --state DIR; exit 0 = ok
```

`files.sha256` lists symlinks without a hash. There is no `python/` yet: the
venv is built with the radio's `/usr/bin/python3` (Debian trixie, cp313). A
release that bundles one sets `"bundles_python": true` and ships
`python/bin/python3`; the updater then uses it and skips the `python_abi`
check.

## integration/: system files

Every file under `integration/` is placed by an allowlist rule; anything else
fails the apply. The top dir is the file's **kind**.

| Kind | Name pattern | Destination | Mode | Checked first |
|---|---|---|---|---|
| `systemd` | `<unit>.service`, `<unit>.timer` | `/etc/systemd/system/` | 0644 | no `/opt/hasler/releases/` path inside |
| `systemd` (drop-in) | `<unit>.service.d/hasler-*.conf` | `/etc/systemd/system/<unit>.service.d/` | 0644 | as above |
| `udev` | `*.rules` | `/etc/udev/rules.d/` | 0644 | `udevadm verify` |
| `sbin` | `hasler-*` | `/usr/local/sbin/` | 0755 | |
| `bin` | `hasler-*` | `/usr/local/bin/` | 0755 | |
| `sudoers` | `haslerd` | `/etc/sudoers.d/haslerd` | 0440 | `visudo -cf` |
| `shutdown` | `hasler-*` | `/usr/lib/systemd/system-shutdown/` | 0755 | |
| `configtxt` | `hasler.block` | the marked hasler block in `/boot/firmware/config.txt` | 0644 | only if unchanged, or with `apply --allow-reboot` (then a reboot into the trial) |
| `avahi` | `hasler.service` | `/etc/avahi/services/` | 0644 | parses as a `service-group` |

- Units reference `/opt/hasler/current/...`, never a version path.
- Names starting with `hasler-update` (the updater's launcher and units) are
  **reserved**: never placed during a trial, installed only when their release
  commits, together with `app/hasler_update/` -> `/opt/hasler/updater/`.
- Each destination's old content (or the fact that there was none) is backed
  up in `/var/lib/hasler-apply/backup/<version>/` before it is first replaced;
  a rollback restores exactly that.
- **A new kind only in a major release.** The kinds above are the ones the
  0.4.x updater knows (`KNOWN_KINDS_SINCE` in `make_manifest.py`; a test keeps
  it equal to the newest tag's `integrate.py`). `build_release.py` and
  `make_manifest.py check` refuse any other kind unless `kind` is `major`. On
  the radio (updater 0.4.2 or later), a file of an unknown kind in a major
  release must look like `<kind>/<name>` with the kind matching
  `[a-z][a-z0-9-]*` and no dot files. The trial does not place it; it is
  recorded as `pending_integration` (history `integration-deferred`). After
  the commit, the new updater runs `hasler-update integrate-pending --locked`
  in a new process, places those files with its own rules (idempotent;
  backups with the release's own) and clears the field (history
  `integrate-pending`). A failure there never undoes the commit: the error
  and attempt count go into `pending_integration` (history
  `integrate-pending-failed`), and it is retried at the next `apply` and
  `resume`. A rollback puts back the value from before the switch
  (`pending_prev`). Always refused, major or not: a known kind with a name off
  its pattern, symlinks, anything that is not a regular file. Updaters before
  0.4.2 refuse a new kind even in a major release.

## release.json (signed: release.json.sig)

The published 0.4.4, shortened:

```json
{
  "schema": 1,
  "product": "hasler",
  "version": "0.4.4",
  "seq": 40499,
  "channel": "stable",
  "created": "2026-10-20T21:19:14Z",
  "expires": "2027-02-17T21:19:14Z",
  "kind": "patch",
  "requires_migration": false,
  "min_from_seq": 0,
  "requires": {"os": "trixie", "moode": ">=10.0,<11", "python_abi": "cp313",
               "arch": "aarch64", "apt": ["liblgpio1"]},
  "config_schema": 1,
  "variants": ["excelsior", "hasler-hm"],
  "artifacts": [{"name": "hasler-0.4.4-arm64.tar.zst", "sha256": "333e27f5...", "size": 40297588,
                 "url": "https://github.com/delgado-murphy/hasler-releases/releases/download/v0.4.4/hasler-0.4.4-arm64.tar.zst"}],
  "notes": "the CHANGELOG.md section for 0.4.4",
  "keyring_seq": 1
}
```

| Field | Rule |
|---|---|
| `version` | `X.Y.Z` or `X.Y.Z-rc.N` |
| `seq` | `(X*10000 + Y*100 + Z)*100 + N` for `-rc.N`, `+ 99` for a final; Y and Z below 100, N below 99 (`build_release.py --seq` overrides). The radio installs only a `seq` above every one it has committed (`max_seq`) |
| `channel` | `bench` for an rc, `stable` for a final, by default. Informational: the radio follows the channel index, not this |
| `created`, `expires` | ISO 8601 UTC; `expires` = `created` + 120 days. Expired = refused |
| `kind` | `patch`, `minor` or `major`, from the previous `v*` tag (no previous tag: `major`) |
| `requires_migration` | `true`: asks first, like a major. CI builds always write `false`, so a migration ships in a major |
| `min_from_seq` | the radio must already run at least this `seq` (0 = any) |
| `requires` | `arch`, `os` (codename), `python_abi`, `moode` (a range) must match the radio. `apt`: Debian packages the wheels need at runtime (installed by `bootstrap.sh`; the radio doesn't check them) |
| `config_schema` | the release's `boxconfig.SCHEMA`; above the radio's `box.yaml` `schema` -> migrations run before the switch |
| `variants` | the cabinets it ships; the radio's `variant` must be listed |
| `artifacts[0]` | the tarball's `name`, `sha256`, `size`, `url` |
| `notes` | shown on the key page |
| `keyring_seq` | the keyring it needs; a radio with an older one fetches the published keyring first |
| `files_sha256` | optional: sha256 of the tarball's `files.sha256`. With it, root verifies the staged tree in place; without it (today's builds), root re-extracts the hash-checked artifact |
| `bundles_python` | optional, see the tarball |

## Signatures

OpenSSH signatures: `ssh-keygen -Y sign -f <key> -n <namespace> <file>` ->
`<file>.sig` (`scripts/release/sign.sh`). The radio verifies with
`ssh-keygen -Y verify`, writing a one-line `allowed_signers` per key from the
verified keyring, so a key only counts for the role it is listed with.

| Namespace | Signs | Key role |
|---|---|---|
| `hasler-keyring` | `keyring.json` | root |
| `hasler-release` | `release.json` | release |
| `hasler-channel` | `channels/<channel>.json` | timestamp |

## Keyring and trust

On the radio, `/etc/hasler/trust/` (root, files 0444): `root.pub` (any
`root*.pub` counts; one valid root signature is enough), `keyring.json` and
`keyring.json.sig`. Published at `<base>/keyring.json(.sig)`, next to
`channels/`.

```json
{"schema": 1, "keyring_seq": 1, "expires": "2027-10-20T00:00:00Z",
 "keys": [{"id": "release-2026a", "role": "release", "pub": "ssh-ed25519 AAAA...",
           "not_after": "2027-10-20T00:00:00Z"},
          {"id": "timestamp-2026a", "role": "timestamp", "pub": "ssh-ed25519 AAAA...",
           "not_after": "2027-10-20T00:00:00Z"}]}
```

A radio takes a new keyring only if a root key verifies it and its
`keyring_seq` is higher than the one it has. An expired keyring, or a key past
its `not_after`, doesn't count.

## Channel index (signed by a timestamp key)

`<base>/channels/<channel>.json` + `.sig`, with `<base>` =
`https://delgado-murphy.github.io/hasler-releases` (or `box.yaml`'s
`update_url`) and `<channel>` one of **`bench` | `stable`**. Written by
`scripts/release/channel.py`; re-signed daily by `sign-publish`.

```json
{"schema": 1, "channel": "bench", "issued": "2026-10-20T21:23:06Z",
 "expires": "2026-11-03T21:23:06Z",
 "current": {"version": "0.4.4", "seq": 40499,
             "release_url": "https://github.com/delgado-murphy/hasler-releases/releases/download/v0.4.4/release.json",
             "release_sha256": "..."},
 "halted": [], "min_supported_seq": 0,
 "rings": {"0": [], "1": [], "2": ["*"]}, "ring_open": {"0": "...", "1": "...", "2": "..."}}
```

- `channel` must be the one the radio asked for; `expires` = `issued` + 14
  days; an `issued` more than an hour in the future is refused.
- `current.release_sha256` is the sha256 of the signed `release.json` bytes;
  its `seq` and `version` must match them.
- `halted`: seqs never installed; a staged copy is dropped. `current` stays
  as it is: a halted release is replaced by promoting a higher one.
- `rings`: lists of `box_id`s, `"*"` = everyone else. A radio belongs to the
  lowest ring that names it, else to the one with `"*"`, else to none (not
  served). It is served once its ring's `ring_open` time has passed (no time =
  open). No `rings` = everyone. `channel.py promote` opens every ring at once.
- `min_supported_seq`: written by `channel.py`, not checked by the radio yet.
- `channel.py promote` refuses a `seq` not above the current one, a halted
  one, or an expired release.

## On the radio

- `/opt/hasler/releases/<version>/` (root, read-only; complete once it has
  `.complete`), with `release.json(.sig)` copied from the verified bytes;
  the `/opt/hasler/current` and `/opt/hasler/previous` symlinks;
  `/opt/hasler/updater/hasler_update/` (the updater that runs).
- `/var/lib/hasler-update/` (user `hasler-update`): `incoming/` (downloads,
  the signed files, a newer keyring for root to install), `staged/<seq>/`
  (`tree/`, `release.json(.sig)`, `index.json(.sig)`, and `STAGED` once
  complete) and `status.json`, the last check: `checked`, `channel`, `mode`,
  `installed`, `available`, `index_expires`, `result`, `message`, `staged`.
- `/var/lib/hasler-apply/state.json` (root, 0644 so haslerd can read it).
  Every write: tmp + fsync + rename + fsync of the dir.

```json
{"max_seq": 40499, "keyring_seq": 1,
 "phase": "idle|trial|probation|committed|rolled_back",
 "step": "integrate|switched|restarted|rebooting|rollback|null",
 "from": "0.4.3", "to": "0.4.4", "from_seq": 40399, "to_seq": 40499,
 "boots": 0, "boot_id": "...", "deadline": "...", "bad": [],
 "baseline": {}, "integrated": ["/etc/systemd/system/haslerd.service"],
 "migrated": false, "reason": null, "manual": false, "failed": null,
 "pending_integration": null, "pending_prev": null,
 "history": [{"at": "...", "event": "switch|commit|rollback|...", "...": "..."}]}
```

  `deadline` is 150 s after the trial begins, then the end of the 24 h
  probation; `boot_id` is the kernel boot id the trial began under (a haslerd
  restart is not a boot); `manual` = a go-back, which has no probation and
  doesn't raise `max_seq`; `bad` = seqs that failed here; `failed` = the
  version last rolled back. The last 30 history events are kept.
- `/etc/hasler/box.yaml` keys the updater reads (flat `key: value` lines
  only): `channel` (bench|stable; missing or anything else = stable),
  `update_mode` (auto|ask|off; missing = auto, unknown = ask), `box_id`,
  `metered` (bool), `variant`, `update_url`, and `schema` for migrations.

## Commands and units

`hasler-update` (`/usr/local/sbin/hasler-update`, runs `/opt/hasler/updater`).
Exit codes: 0 ok or nothing to do, 1 error, 75 deferred (radio in use,
outside the window, mode).

| Command | Run by | Does |
|---|---|---|
| `check [--stage] [--force]`, `stage` | `hasler-update-check.service` (daily timer; user `hasler-update`) | look, and download + extract; `--force` ignores `metered` and `off` |
| `apply --auto` | `hasler-update-apply.service` (timer: 02:00-04:30, every 30 min) | install the staged release if mode `auto`, not major/migration, in the window, idle |
| `instance <seq>` | `hasler-update-apply@<seq>.service` (the key page's install) | as `apply --now`: no window, still idle |
| `instance rollback`, `instance rollback-X.Y.Z` | `hasler-update-apply@rollback[-X.Y.Z].service` (the key page's go back) | go back now, even while playing |
| `boot` | `hasler-update-boot.service` (early, before haslerd) | boot counter; finish or undo an interrupted switch |
| `resume` | `hasler-update-resume.service` (after haslerd at boot) | judge a trial whose apply died; retry `pending_integration` |
| `on-failure` | `hasler-update-rollback.service` (haslerd's `OnFailure=`) | roll back during trial or probation |
| `apply [--seq N] [--now] [--force] [--allow-reboot]`, `rollback [--to V]`, `commit`, `status`, `integrate-pending` | by hand (root; `status` anyone) | `--force` skips the window and the idle check (bench drills) |

haslerd (user `pi`) starts only `hasler-update-check.service` and
`hasler-update-apply@{<seq>|rollback|rollback-X.Y.Z}.service`, with
`systemctl start --no-block`, allowed by the polkit rule
`config/update/50-hasler-update.rules` (not sudoers: a `*` there also
matches spaces). The updater checks the instance name again.

## Health endpoint (haslerd)

`GET http://127.0.0.1:8080/api/health` -> 200 with

```json
{"version": "0.4.4", "ok": true, "uptime_s": 42,
 "checks": {"mpd": true, "audio_card": true, "panel": "ok|absent|allowed_absent",
            "lamp": true, "web": true},
 "idle": true, "standby": true, "playing": false, "last_play_s": 1234}
```

`idle` = safe to update now: standby, no phone casting, nothing played for at
least 10 minutes; `playing` includes casting. haslerd builds it in
`haslerd/health.py` (`health_report(...)`). In the updater's health check, a
check only has to stay good if it was good on the old release; without a
baseline (an old release with no `/api/health`) every check must be good.
