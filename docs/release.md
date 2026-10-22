# Releasing Hasler

*For makers:* how a new version of the radio's software is built, signed and
sent to the radios. How updates look to an owner: [Using your
Hasler](using.md#updates). Why it works this way: [ota.md](ota.md). The exact
file formats: [ota-format.md](ota-format.md).

**The short version.** Tag it, let GitHub build it, start the signing
workflow. Nobody has to approve or sign anything by hand.

1. Bump the version and write the changelog, on `master`.
2. Tag `vX.Y.Z` and push the tag. `release.yml` tests and builds it (about
   3 minutes).
3. Run `sign-publish.yml` in `hasler-releases` with mode `publish` and
   channel `bench`. It checks the build, signs it, creates the GitHub release
   and points the bench channel at it on GitHub Pages (under a minute).
4. The bench radio picks it up at its next daily check, or at once with
   "Check now".
5. When the bench radio has lived with it happily, **promote** the same
   release to `stable`.

Tools: `scripts/release/`. Workflows: `.github/workflows/ci.yml` and
`release.yml` in `hasler` (this repository), and
`release-tools/hasler-releases/.github/workflows/sign-publish.yml` (copied to
the public `hasler-releases` repository). Published releases:
<https://delgado-murphy.github.io/hasler-releases/>.

## Release checklist

1. `master` green in CI; the bench radio on the previous release.
2. Bump `version` in `daemon/pyproject.toml` **and** `__version__` in
   `daemon/haslerd/__init__.py` (`--strict` checks both), and add a
   `## X.Y.Z - date` section to `CHANGELOG.md` (it becomes the release notes,
   shown on the key page and the GitHub release). Pick the number by what
   changed - the number alone decides the release's `kind`, measured from the
   newest **final** `v*` tag (release candidates don't count: stable radios
   never ran one):
   - **major** (`X+1.0.0`): a new kind of system file (see below), a config
     schema change with a migration (`daemon/haslerd/migrations/NNN_name.py`),
     or a raised OS/moOde floor. Radios always ask before installing it.
   - **minor**: features with defaults. **patch**: fixes. Both install by
     themselves at night on radios set to Automatic. New units, udev rules,
     helpers or drop-ins of a kind radios already know are fine here.
3. Try it locally (any machine):
   `python scripts/release/build_release.py --version X.Y.Z --out dist/ --no-wheels`.
   Its warnings list what `--strict` will refuse in CI.
4. Tag and push:

   ```sh
   git tag -a vX.Y.Z -m X.Y.Z && git push origin vX.Y.Z
   ```

   `release.yml` runs the tests, builds in `debian:trixie` on a native arm64
   runner, installs the venv offline as a radio would, attests the build and
   uploads the artifact `hasler-release-X.Y.Z`. The run summary shows the
   tarball's sha256.
5. Optional, for anything unusual: download the artifact
   (`gh run download <run id> -R Delgado-Murphy/hasler -n hasler-release-X.Y.Z`),
   run `scripts/release/make_manifest.py check`, and diff `integration/`
   against the last release (`zstd -dc ... | tar -t`).
6. Publish to bench:

   ```sh
   gh workflow run sign-publish.yml -R Delgado-Murphy/hasler-releases \
     -f mode=publish -f version=X.Y.Z -f artifact_sha256=<sha256> \
     -f run_id=<release.yml run id> -f channel=bench
   ```

   It checks that the run is `release.yml` on that tag, green, and that the
   tag still points at the commit built; then the artifact digest, the
   sha256, the build attestation, the tarball against `release.json`, and the
   keyring. Then it signs, creates the GitHub release `vX.Y.Z`, lists it in
   `releases.json` (the website) and points `bench` at it.
7. On the bench radio: Check now, install, live with it - nights and standby
   cycles included. For bigger changes run the drills in
   [ota.md](ota.md#drills-for-each-release).
8. Promote to stable - the same artifact, never rebuilt:

   ```sh
   gh workflow run sign-publish.yml -R Delgado-Murphy/hasler-releases \
     -f mode=promote -f version=X.Y.Z -f channel=stable
   ```

9. A bad release: halt it on its channel, then ship the fix as a new, higher
   version. Radios that haven't installed it never will; radios already
   running it stay there until the fix arrives.

   ```sh
   gh workflow run sign-publish.yml -R Delgado-Murphy/hasler-releases \
     -f mode=halt -f version=X.Y.Z -f channel=stable
   ```

**Release candidates** (`vX.Y.Z-rc.N`) work the same way and are marked as
pre-releases on GitHub. They are optional; keep them on `bench`. They don't
change the final's `kind`: `v1.0.0` after `v1.0.0-rc.1` is still `major`
(measured from `v0.4.6`).

**A higher major version always asks**, whatever its signed `kind`: a radio
on 1.x treats 2.0.1 as major even though it is a patch on top of 2.0.0
(updater 1.0.0 and later; `asks_first` in `update/hasler_update/check.py`).
Radios still on 0.x run the older rule - signed `kind` only - so a 1.0.x
fix reaches a 0.x radio at night as a patch (see 1.0.0 below).

**seq** is derived from the version, so there is no counter to keep:
`(X*10000 + Y*100 + Z)*100 + N` for `-rc.N`, `+ 99` for a final (Y and Z
below 100). Radios only ever move to a higher seq.

**Expiry.** A signed `release.json` is valid for 120 days: promote within that
time. Channel indexes expire after 14 days; `sign-publish` re-signs them every
night (03:17 UTC), so nothing needs doing - unless that job stops running.

## 1.0.0: the first stable release

**Where it stands (2026-10-21):** steps 1-5 are done for 1.0.0, and 1.0.1
(a patch) followed it to bench; the bench radio runs 1.0.1. Nothing is on
stable yet. The soak (step 6) and the promote (step 7) are next; which
version to promote is an owner decision (*Promoting 1.0.1 instead of 1.0.0*,
below).

What is special: `stable.json` doesn't exist until the first promote (radios
on Stable report "no update info" until then; the promote creates it - the
`channel` job builds a fresh index when the file is missing, and the nightly
refresh keeps it signed from then on), and 1.0.0 is `major`, so every radio
asks first, the bench radio included.

1. Pre-flight: CI green on `master`; both suites green locally; a local
   `build_release.py --version 1.0.0 --out dist/ --no-wheels` shows
   `kind: major`, `min_from_seq: 0`, `requires_migration: false`.
2. Bump both version strings to `1.0.0`, date the CHANGELOG heading, commit,
   push, wait for `ci`.
3. Tag `v1.0.0`, push it; `release.yml` builds it. Note the run id and the
   sha256 in the run summary.
4. `sign-publish` mode `publish`, channel `bench` (as in step 6 above).
   Check: <https://delgado-murphy.github.io/hasler-releases/> lists 1.0.0
   (`major`) with Bench on it; `channels/bench.json` says seq 1000099.
5. Bench radio (on 0.4.6, `auto`): Check now. The Updates page shows "Update
   available: 1.0.0" as a bigger update that waits; the nightly apply
   (`journalctl -u hasler-update-apply`) logs
   `deferred: major or migration release`. Install now, press a key on the
   radio, put it in standby: committed within about 3 minutes;
   `hasler-update status` shows 1.0.0 in probation, previous 0.4.6.
6. Soak on bench: at least **3 nights** (2:00-05:00 checks run and find
   nothing), 3 standby/wake cycles, one cold power-on (music in ~15 s), one
   power cut while playing. Each source once: every key (radio, Spotify,
   YouTube Music), Spotify Connect, AirPlay, Bluetooth (reconnect to the last
   phone), UPnP; a multiroom group if a second radio is at hand; knobs,
   lamp colours, Home Assistant key action. `journalctl -u haslerd -p
   warning` stays quiet; probation ends with `committed`. Leave the "Go
   back" drill out of the soak, or do it last: going back marks 1.0.0 bad on
   that radio and `max_seq` stays at 1000099, so it can't install 1.0.0 again
   without editing `/var/lib/hasler-apply/state.json` by hand.
7. Promote: `sign-publish` mode `promote`, version `1.0.0`, channel
   `stable`. Check that `channels/stable.json` and `.sig` exist on gh-pages,
   `python3 scripts/release/channel.py check` on a downloaded copy says
   1.0.0, and the website shows 1.0.0 on Stable.
8. A Stable radio then shows "Update ready: 1.0.0 - A bigger update - it
   waits for you" and installs only when its owner presses Install and the
   radio key. Tell the owners.

**Promoting 1.0.1 instead of 1.0.0.** Promote works on any bench release.
1.0.1's signed `kind` is `patch` (measured from v1.0.0), so a stable radio
still on 0.4.6 would install it at night **without asking** - unlike 1.0.0,
which asks. Content-wise that is safe (no migration, no new kind of system
file), but if every 0.x stable radio should be asked, promote 1.0.0 first
(those radios then ask), then 1.0.1.

**If 1.0.0 is bad:** before promote, halt it on bench and fix forward with
1.0.1 (the bench radio on 1.0.0 takes it at night). After promote, halt it on
stable: radios that haven't installed it never will; radios on it keep it,
and each owner can Go back to 0.4.6 on the key page. Then ship 1.0.1 through
bench to stable. Radios still on 0.4.6 install that 1.0.1 at night without
asking (their updater only reads the signed `kind`, `patch`). That is safe for
1.0.x - 1.0.0 brings no migration and no new kind of system file - but if
every 0.x radio must be asked, `release.yml` needs a `kind` input passed to
`build_release.py --kind major` first (not there yet).

## A new kind of system file: major releases only

A new top dir under `integration/` (a new *kind*, say `tmpfiles/`) needs a
rule in `update/hasler_update/integrate.py`, a pattern in `build_release.py`'s
`INTEGRATION_OK`, and a **major** version: the radios' current updater has no
rule for it, so in a patch or minor release they would refuse the whole update
(as 0.3.5 did with 0.3.6's `avahi/`). The build refuses that for you (the kinds
radios know are `KNOWN_KINDS_SINCE` in `make_manifest.py`). In a major release
the old updater leaves those files out of the trial, and the release's own
updater puts them in place right after the commit. `hasler-update status`
shows `pending_integration` with the error if that didn't work; the radio
retries at the next night apply and at boot. This needs updater 0.4.2 or later
on the radios. Once the major release is out, add the kind to
`KNOWN_KINDS_SINCE` (the test against the newest tag tells you).

A file for another service's unit goes in as a systemd drop-in,
`integration/systemd/<unit>.service.d/hasler-<name>.conf` (from
`config/systemd-dropins/`), e.g. `bluetooth.service.d/hasler-speaker.conf`.
That is the known `systemd` kind, fine in any release.

A release that changes the `config.txt` block needs a reboot and isn't
installed by the nightly timer or the key page yet: on the radio,
`sudo hasler-update apply --allow-reboot` (see [ota.md](ota.md), §4).

## CI speed

- `ci.yml` runs `lint` (ruff), `pytest (daemon)`, `pytest (updater)` and
  `release-smoke` as parallel jobs; `test` only gathers their results, so
  branch protection can require the single check `ci / test`. Both suites
  run under pytest-xdist (`-n auto`) - new tests must not share fixed ports or
  paths (use `tmp_path` and port 0). Tool pins: `.github/ci-requirements.txt`
  (also the pip cache key). A newer push to a branch or PR cancels the older
  run; tag runs never.
- `release.yml` keeps four caches (actions/cache), mounted at `/cache` in the
  container (`CACHE_DIR`): apt `.deb`s, pip/uv downloads, the finished
  wheelhouse (`build_wheels.sh` reuses it when the lock, the script and the
  toolchain versions match - lgpio is not rebuilt) and the go-librespot/deno
  downloads (keyed on their pins, sha256-checked on use). Saved only when
  their content changed.
- Caches from a tag run are invisible to the next tag; only default-branch
  caches are shared. The weekly scheduled run (Mondays) rebuilds the latest
  tag and so warms them for the next release.
- The byte-for-byte second build runs only on that weekly schedule or on a
  dispatch with `reproducibility: true`, not on every release. Before an
  important release you can dispatch it on the tag yourself.
- Expect: CI about 1-1.5 min; a release build on `ubuntu-24.04-arm` (repo
  variable `ARM_RUNNER`) with warm caches about 3-4 min, cold about 10 min.
  Without `ARM_RUNNER` it falls back to x86 + QEMU, roughly 3-5 times slower.

## How the org is set up

Done once, in October 2026. Kept here so it can be checked or redone.

1. Org `delgado-murphy`; repos `hasler` (this code, public) and
   `hasler-releases` (public). Settings -> Actions -> General, both repos:
   GitHub-owned actions only (all used actions are `actions/*`, pinned by
   SHA); workflow permissions **read-only**; no PR approval by Actions.
2. `hasler`: `master` protected against force push and deletion. Repo
   variables: `ARM_RUNNER=ubuntu-24.04-arm` (native arm64 runners, free for
   public repos) and `ATTESTATIONS=true` (makes `release.yml` attest the
   build). Not set up yet, worth doing: require the check `ci / test`
   on `master`, and a tag ruleset for `v*` (only the owner creates them,
   nobody updates or deletes them).
3. `hasler-releases`:
   - `release-tools/hasler-releases/.github/` copied to the repo root, and
     `scripts/release/{make_manifest.py,keyring.py,channel.py,sign.sh,verify.sh}`
     to `tools/` - **re-copy them when they change**. `trust/root.pub` from
     the key ceremony.
   - branch `gh-pages` with `keyring.json`, `keyring.json.sig`, `channels/`,
     `releases.json` and `index.html` (the website); Settings -> Pages ->
     deploy from branch `gh-pages` /.
   - environment `sign`: no reviewer, deployment branches = `main` only;
     secrets `RELEASE_SIGNING_KEY` (the release private key, OpenSSH format)
     and `BUILD_READ_TOKEN` (fine-grained PAT, owner the org, repo `hasler`
     only, Actions: read, Contents: read, Metadata: read; expires after a
     year - renew it in time).
   - environment `timestamp`: no reviewer (the nightly job runs alone),
     deployment branches = `main` only; secret `TIMESTAMP_SIGNING_KEY`.
   - `main` and `gh-pages` protected against force push and deletion (the
     workflows push `gh-pages` with the Actions token).
   - repo variable `ATTESTATIONS=true` here too: without it `sign-publish`
     skips the attestation check (set on 2026-10-21; 1.0.0 and 1.0.1 were
     signed without the check). It is needed in **both** repos: `hasler` to
     make the attestation, `hasler-releases` to check it.
   - not set up yet: a ruleset so that only the owner may create releases and
     `v*` tags.

## Key ceremony

On an offline machine (no network), with a fresh USB stick and paper. Three
kinds of key:

| Key | Where | Signs | Namespace |
|---|---|---|---|
| root (one today; a second can be added) | offline only, with a paper backup | `keyring.json` | `hasler-keyring` |
| release | GitHub secret in `sign` | `release.json` | `hasler-release` |
| timestamp | GitHub secret in `timestamp` | channel indexes | `hasler-channel` |

```sh
# root: a passphrase from a diceware list, written on the paper backup
ssh-keygen -t ed25519 -a 100 -C hasler-root-2026 -f hasler-root-2026.key
# paper: the private key file is short text - print it (or `qrencode`), keep
# the paper apart from the sticks, and test restoring from it once.

# release + timestamp: no passphrase (they live in GitHub secrets)
ssh-keygen -t ed25519 -N '' -C release-2026a   -f release-2026a
ssh-keygen -t ed25519 -N '' -C timestamp-2026a -f timestamp-2026a

# first keyring, valid one year, signed by root
python3 scripts/release/keyring.py new --seq 1 --expires 2027-10-20 \
    --key release-2026a:release:release-2026a.pub \
    --key timestamp-2026a:timestamp:timestamp-2026a.pub -o keyring.json
python3 scripts/release/keyring.py sign --root-key hasler-root-2026.key keyring.json
python3 scripts/release/keyring.py verify --root hasler-root-2026.key.pub keyring.json
```

Then: `hasler-root-2026.key.pub` -> `trust/root.pub` in `hasler-releases` and
into every radio's `/etc/hasler/trust/` at install; `keyring.json(.sig)` ->
gh-pages; paste the private halves of `release-2026a` and `timestamp-2026a`
into the two secrets, then **delete them from the offline machine**
(`shred -u`). Keep the `.pub` files.

**Rotation or revocation** (a leaked release key, or yearly): a new key pair,
then
`keyring.py new --from keyring.json --drop release-2026a --key release-2026b:release:release-2026b.pub -o keyring.json`
(the seq goes up), sign it with root offline, publish it to gh-pages, replace
the secret. Radios accept it because a root key signed it and its
`keyring_seq` is higher. After rotating the timestamp key, re-sign each
channel once (mode `promote`, or by hand) - the nightly job refuses indexes
the current keyring can't verify. Renew the keyring (it expires 2027-10-20)
well before then.

## Where release files come from

`build_release.py` builds the tarball from an allowlist (its `ALLOWLIST`):

- `app/`: `daemon/haslerd/`, `daemon/pyproject.toml`, `update/hasler_update/`,
  `config/{variants,catalog,stations,camilladsp,snapcast,captive,go-librespot,moode}`,
  the examples, `CHANGELOG.md`.
- `integration/`: `config/*.service|*.timer`, `config/systemd/`,
  `update/systemd/` (the updater's units), `update/bin/hasler-update` and the
  `scripts/hasler-*` helpers (`sbin/`, `bin/`, `shutdown/`),
  `config/sudoers/haslerd`, `config/udev/`, `config/systemd-dropins/`,
  `config/configtxt/hasler.block`, `config/avahi/hasler.service`, or a
  repo-level `integration/` tree. The legacy rsync layout's units in
  `config/legacy/` are never shipped.
- **Not in releases** (put in place by `scripts/ota-bootstrap.sh` /
  `scripts/bootstrap.sh`): the polkit rules and the watchdog
  (`config/update/`), and the captive portal, CamillaDSP, Snapcast and moOde
  files - `app/config/` carries their data, but the integration allowlist has
  no destination for them yet. apt packages are installed by `bootstrap.sh`
  too (`requires.apt` in `release.json` names the ones the wheels need).
- No bundled Python yet: wheels target trixie's `python3` (cp313).

## Bench box: from the rsync tree to releases (dev keys, local channel)

A full OTA cycle on the bench Pi without GitHub, with **throwaway dev keys**
that the key ceremony's production keys replace later. Order matters:
box setup -> updater -> migration 001 -> first release -> drills.

Laptop (Docker Desktop running; keys stay in `~/.hasler-dev`, never in the repo):

```sh
scripts/release/dev-keys.sh                               # ~/.hasler-dev/keys (+ keys/trust/ for the box)
scripts/release/dev-build.sh 0.2.0-rc.1 --worktree        # arm64 wheels in debian:trixie, --strict
scripts/release/dev-channel.py publish --dist ~/.hasler-dev/dist/0.2.0-rc.1 \
    --base-url http://<laptop>.local:8000                 # site in ~/.hasler-dev/site
scripts/release/dev-channel.py serve                      # leave running
rsync -a ~/.hasler-dev/keys/trust/ pi@hasler.local:/tmp/hasler-trust/   # public halves only
rsync -a --delete --exclude .git --exclude .venv ./ pi@hasler.local:hasler/
```

Pi (`cd ~/hasler`):

```sh
sudo scripts/bootstrap.sh --dry-run && sudo scripts/bootstrap.sh     # apt, users, yt-dlp as ytdlp, ...
sudo scripts/ota-bootstrap.sh --dry-run --trust /tmp/hasler-trust
sudo scripts/ota-bootstrap.sh --trust /tmp/hasler-trust              # updater, units, polkit, watchdog, trust
sudo daemon/.venv/bin/python -m haslerd.migrations --check           # migration 001: the plan
sudo daemon/.venv/bin/python -m haslerd.migrations --apply           # writes /etc/hasler, moves secrets
sudo scripts/ota-bootstrap.sh --update-url http://<laptop>.local:8000  # box.yaml + ownership after 001
sudo systemctl start hasler-update-check.service; cat /var/lib/hasler-update/status.json   # "0.2.0-rc.1 staged"
sudo hasler-update apply --seq <seq> --force                         # first install: integrate, switch, health
hasler-update status
```

`--force` skips the night window and the idle check (bench only). The first
release is `major` (no previous tag), so it would wait to be asked anyway. The
legacy lamp block was re-marked by ota-bootstrap, so no reboot is needed; if it
says the block differs, add `--allow-reboot`. After the first commit
`scripts/install.sh` refuses to run - the radio is updated by releases now.

Drills (each a new, higher rc; publish, check, apply as above):

| Drill | How | Expect |
|---|---|---|
| upgrade | `dev-build.sh 0.2.0-rc.2`, publish | committed, probation, `previous` -> rc.1 |
| rollback (health) | `dev-build.sh 0.2.0-rc.3 --broken` | haslerd exits at start -> rolled back to rc.2, rc.3 marked bad |
| manual go back | `sudo systemctl start hasler-update-apply@rollback.service` (what the key page does) | back to the previous release |
| power cut | pull power during download, stage, right after "switched", and at first boot | always lands on a working release; the boot counter rolls a trial back at the 2nd boot |
| halt | `dev-channel.py halt --seq <seq>` before applying | status says halted, staged copy dropped |
| expiry | skip `dev-channel.py refresh` for 14 days (or set the Pi clock ahead in a scratch test) | "no fresh update info", nothing installed |

Index expiry is 14 days: `dev-channel.py refresh` re-signs. To move the bench
radio to production keys:
`sudo scripts/ota-bootstrap.sh --trust <ceremony trust dir> --replace-trust` and
`--update-url ""` (back to the public channel); releases signed with the dev
keys stop verifying at that moment. (The bench radio has been on the
production keys and the public channel since 0.3.1.)
