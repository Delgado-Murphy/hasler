# Releasing Hasler

This page is for maintainers: how a new version of the radio's software is
built, signed and sent to the radios. If you only want to know how updates look
to an owner, see [Using your Hasler](using.md#updates).

**The short version.** Every release goes through the same steps:

1. Bump the version and write the changelog.
2. Tag a **release candidate** (`vX.Y.Z-rc.N`). CI builds it.
3. Check the build, then approve the signing workflow. The release goes to the
   `bench` channel (the makers' own radio).
4. Let the bench radio live with it for 48 hours, including nights and standby.
5. Tag the final version and publish it to `stable`. Every radio on `stable`
   then sees it.

Background: [ota.md](ota.md), sections 2-3 (why it works this way). File
formats: [ota-format.md](ota-format.md). Tools: `scripts/release/`. Workflows:
`.github/workflows/ci.yml` and `release.yml` (in `hasler`, this repository) and
`release-tools/hasler-releases/.github/workflows/sign-publish.yml` (copied to
the public `hasler-releases` repository). Published releases:
<https://delgado-murphy.github.io/hasler-releases/>.

## Release checklist

1. `master` green in CI. Bench box on the previous release.
2. Bump `version` in `daemon/pyproject.toml`; add the `## X.Y.Z - date`
   section to `CHANGELOG.md` (it becomes the release notes). MAJOR if config
   schema, descriptor format, units/udev/sudoers/config.txt, apt deps or the
   OS/moOde floor changed (docs/ota.md §2). New migration? It ships from
   `daemon/haslerd/migrations/NNN_name.py`.
3. Try it locally: `python scripts/release/build_release.py --version X.Y.Z --out dist/ --no-wheels`
   (any machine; warnings list what `--strict` will refuse in CI).
4. Release candidate first: `git tag -a vX.Y.Z-rc.1 -m ... && git push origin vX.Y.Z-rc.1`.
   `release.yml` tests, builds in debian:trixie on arm64, installs the venv
   offline, uploads the artifact
   `hasler-release-X.Y.Z-rc.1`. The run summary shows the run id and the
   tarball's sha256.
5. Before signing (laptop): download the artifact, `make_manifest.py check`,
   skim `files.sha256` and diff `integration/` against the last release
   (`zstd -dc ... | tar -t`). Anything surprising: stop.
6. In `hasler-releases` -> Actions -> sign-publish -> Run: mode `publish`,
   version, artifact sha256, run id, channel `bench`. Approve the `sign`
   environment when asked. It re-checks the run (release.yml on that tag,
   green, tag still at that commit), the artifact digest, the sha256, the
   attestation (if enabled), the tarball against release.json, the keyring;
   signs, creates GitHub release `vX.Y.Z-rc.1`, points `bench` at it.
7. Bench box: 48 h incl. nights and standby cycles; run the test matrix in
   docs/ota.md (rollback drill, power-cut drill, halt drill).
8. Promote: tag `vX.Y.Z` on the same commit -> steps 4-6 with channel
   `stable`, or mode `promote` to point `stable` at the release already on
   `bench` without rebuilding. There are only two channels, `bench` and
   `stable`.
9. Bad release: `channel.py halt --index channels/<ch>.json --seq N`, sign
   with the timestamp key (`sign.sh -n hasler-channel`), push gh-pages; then
   ship the fix as a new, higher version.

`seq` is derived from the version - `(M*10000 + m*100 + p)*100 + (N for rc.N, 99 for final)` -
so it follows semver with no counter to keep (minor/patch < 100). `--seq` overrides.

## CI speed

- `ci.yml` runs `lint` (ruff), `pytest (daemon)`, `pytest (updater)` and
  `release-smoke` as parallel jobs; `test` only gathers their results, so
  branch protection still requires the single check `ci / test`. Both suites
  run under pytest-xdist (`-n auto`; ~9 s / ~7 s on a laptop vs 73 s / 24 s
  serial) - new tests must not share fixed ports or paths (use `tmp_path`
  and port 0). Tool pins: `.github/ci-requirements.txt` (also the pip cache
  key). A newer push to a branch/PR cancels the older run; tag runs never.
- `release.yml` keeps four caches (actions/cache), mounted at `/cache` in the
  container (`CACHE_DIR`): apt `.deb`s, pip/uv downloads, the finished
  wheelhouse (`build_wheels.sh` reuses it when the lock, the script and the
  toolchain versions match - lgpio is not rebuilt) and the go-librespot/deno
  downloads (keyed on their pins, sha256-checked on use). Saved only when
  their content changed. Same caches on the native arm64 runner and on QEMU.
- Caches from a tag run are invisible to the next tag; only default-branch
  caches are shared. The weekly scheduled run (and any manual dispatch from
  `master`) rebuilds the latest tag and so warms them for the next release.
- The byte-for-byte second build runs only on that weekly schedule or a
  dispatch with `reproducibility: true`, not on every release. Before
  signing an important release you can dispatch it on the tag yourself.
- Expect: CI ~1-1.5 min wall clock; a release on `ubuntu-24.04-arm` with warm
  caches ~4-6 min, cold ~10 min; on QEMU roughly 3-5x that.

## One-time org setup (owner, once the org exists)

1. Org `delgado-murphy`; repos `hasler` (this code; private at first, now
   public) and `hasler-releases` (public). Settings -> Actions -> General, both repos:
   "Allow actions created by GitHub" only (all used actions are `actions/*`,
   pinned by SHA); workflow permissions **read-only**; no PR approval by Actions.
2. `hasler`: branch protection on `master` (require `ci / test`, no force
   push, no deletion). Tag ruleset for `v*`: only you can create, nobody can
   update or delete. Repo variable `ATTESTATIONS=true`: attestations in a
   *private* repo need GitHub Enterprise Cloud; while `hasler` was private on
   the Free plan it stayed unset, and sign-publish relied on run metadata +
   artifact digest. Now that `hasler` is public, attestations should work on
   the Free plan too; turning `ATTESTATIONS=true` on is still to be decided.
   `ubuntu-24.04-arm` runners must be available to the repo on the plan;
   if not, make the build job `runs-on` a self-hosted arm64 runner (never the bench Pi).
3. `hasler-releases`:
   - copy `release-tools/hasler-releases/.github/` to the repo root, and
     `scripts/release/{make_manifest.py,keyring.py,channel.py,sign.sh,verify.sh}`
     to `tools/` (re-copy when they change); `trust/root.pub` (and
     `trust/root2.pub`) from the key ceremony.
   - branch `gh-pages` with `keyring.json`, `keyring.json.sig`,
     `channels/` (empty) and an `index.html`; Settings -> Pages -> deploy from
     branch `gh-pages` /. Indexes appear at
     `https://delgado-murphy.github.io/hasler-releases/channels/<channel>.json`.
   - environment `sign`: required reviewer = you, "prevent self-review" off,
     deployment branches = `main` only; secrets `RELEASE_SIGNING_KEY`
     (the release private key, unencrypted OpenSSH format) and
     `BUILD_READ_TOKEN` (fine-grained PAT, resource owner the org, repo
     `hasler` only, permissions Actions: read, Contents: read, Metadata: read;
     expiry 1 year - calendar it).
   - environment `timestamp`: no reviewer (the daily job must run alone),
     deployment branches = `main` only; secret `TIMESTAMP_SIGNING_KEY`.
   - branch protection on `main` and `gh-pages`: no force push, no deletion.
     (Actions' token must still push gh-pages: allow `github-actions` or leave
     gh-pages unprotected beyond no-force-push.)
   - ruleset: nobody but you may create releases/tags `v*`.

## Key ceremony

Offline machine (no network), a fresh USB stick, paper. Three key kinds:

| key | where | signs | namespace |
|---|---|---|---|
| root (1, ideally 2) | encrypted file on 2 USB sticks + paper, never online | `keyring.json` | `hasler-keyring` |
| release | GitHub secret in `sign` (v1); YubiKey later | `release.json` | `hasler-release` |
| timestamp | GitHub secret in `timestamp` | channel indexes | `hasler-channel` |

```sh
# root: passphrase from a dice/diceware list, written on the paper backup
ssh-keygen -t ed25519 -a 100 -C hasler-root-2026 -f hasler-root-2026.key
# paper: the private key file is short text - print it (or `qrencode`) and
# store the paper apart from the sticks; test restoring from paper once.

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

Then: `hasler-root-2026.key.pub` -> `trust/root.pub` (public repo) and into
every box's `/etc/hasler/trust/` at install; `keyring.json(.sig)` -> gh-pages;
paste `release-2026a` and `timestamp-2026a` (private halves) into the two
secrets, then **delete them from the offline machine** (`shred -u`). Keep the
`.pub` files.

**Rotation / revocation** (leaked release key, or yearly): new key pair, then
`keyring.py new --from keyring.json --drop release-2026a --key release-2026b:release:release-2026b.pub -o keyring.json`
(seq goes up), sign with root offline, publish to gh-pages, replace the secret.
Boxes accept it because a root key signed it and its `keyring_seq` is higher.
After a timestamp-key rotation, re-sign each channel once with mode
`promote` or by hand - the daily job refuses indexes the current keyring
cannot verify. Renew the keyring well before `expires`.

## Gaps against docs/ota-format.md (to settle with the box side)

- Channel index signatures use namespace `hasler-channel` (the contract does
  not name one).
- `manifest.json` in the tarball is release.json **without** `artifacts`
  (a file cannot hold its own archive's hash).
- `files.sha256` lists every file except itself, including `manifest.json`.
- `requires.apt` (list of Debian packages) appears only if a native wheel
  could not be repaired with auditwheel.
- `app/` holds `haslerd/` + `hasler_update/` + `config/` (variants, catalog,
  stations, camilladsp, captive, go-librespot, moode, examples) - data that
  scripts/bootstrap.sh puts in NetworkManager/nginx/camilladsp/moOde places, which the
  integration allowlist has no destination for yet.
- Integration sources: `config/*.service|*.timer` (OTA units, `/opt/hasler/current`),
  `config/sudoers/haslerd`, `config/configtxt/hasler.block`, `config/udev/`, `scripts/hasler-*`,
  `update/systemd/`, `update/bin/`. The legacy rsync layout's units live in `config/legacy/`
  (used only by `scripts/install.sh`, never shipped). Not in releases: the polkit rules and
  the watchdog (`config/update/`, put in place by `scripts/ota-bootstrap.sh` /
  `scripts/bootstrap.sh`), and the captive portal / CamillaDSP / moOde files (`bootstrap.sh`).
- `python/` (bundled CPython) is not built in v1: wheels target trixie's
  python3 (cp313).

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

`--force` skips the night window and the idle veto (bench only). The first
release is `major` (no previous tag), so it would wait to be asked anyway. The
legacy lamp block was re-marked by ota-bootstrap, so no reboot is needed; if it
says the block differs, add `--allow-reboot`. After the first commit
`scripts/install.sh` refuses to run - the box is updated by releases now.

Drills (each a new, higher rc; publish, check, apply as above):

| drill | how | expect |
|---|---|---|
| upgrade | `dev-build.sh 0.2.0-rc.2`, publish | committed, probation, `previous` -> rc.1 |
| rollback (health) | `dev-build.sh 0.2.0-rc.3 --broken` | haslerd exits at start -> rolled back to rc.2, rc.3 marked bad |
| manual go back | `sudo systemctl start hasler-update-apply@rollback.service` (what the key page does) | back to the previous release |
| power cut | pull power during download, stage, right after "switched", and at first boot | always lands on a working release; boot counter rolls a trial back at the 2nd boot |
| halt | `dev-channel.py halt --seq <seq>` before applying | status says halted, staged copy dropped |
| expiry | skip `dev-channel.py refresh` for 14 days (or set the Pi clock ahead in a scratch test) | "no fresh update info", nothing installed |

Index expiry is 14 days: `dev-channel.py refresh` re-signs. To move the bench
box to production keys after the ceremony:
`sudo scripts/ota-bootstrap.sh --trust <ceremony trust dir> --replace-trust` and
`--update-url ""` (back to the public channel); releases signed with the dev
keys stop verifying at that moment.
