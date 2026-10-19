# Releasing Hasler

The human side of docs/ota.md §2-3. Formats: docs/ota-format.md. Tools:
`scripts/release/`. Workflows: `.github/workflows/ci.yml`, `release.yml`
(private `hasler`), `release-tools/hasler-releases/.github/workflows/sign-publish.yml`
(public `hasler-releases`).

## Release checklist

1. `master` green in CI. Bench box on the previous release.
2. Bump `version` in `daemon/pyproject.toml`; add the `## [X.Y.Z] - date`
   section to `CHANGELOG.md` (it becomes the release notes). MAJOR if config
   schema, descriptor format, units/udev/sudoers/config.txt, apt deps or the
   OS/moOde floor changed (docs/ota.md §2). New migration? It ships from
   `daemon/haslerd/migrations/NNN_name.py`.
3. Try it locally: `python scripts/release/build_release.py --version X.Y.Z --out dist/ --no-wheels`
   (any machine; warnings list what `--strict` will refuse in CI).
4. Release candidate first: `git tag -a vX.Y.Z-rc.1 -m ... && git push origin vX.Y.Z-rc.1`.
   `release.yml` tests, builds in debian:trixie on arm64, rebuilds to prove the
   bytes are reproducible, installs the venv offline, uploads the artifact
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
   `stable` (or `beta` first), or mode `promote` to point another channel at
   an already published release without rebuilding.
9. Bad release: `channel.py halt --index channels/<ch>.json --seq N`, sign
   with the timestamp key (`sign.sh -n hasler-channel`), push gh-pages; then
   ship the fix as a new, higher version.

`seq` is derived from the version - `(M*10000 + m*100 + p)*100 + (N for rc.N, 99 for final)` -
so it follows semver with no counter to keep (minor/patch < 100). `--seq` overrides.

## One-time org setup (owner, once the org exists)

1. Org `delgado-murphy`; repos `hasler` (private, this code) and
   `hasler-releases` (public). Settings -> Actions -> General, both repos:
   "Allow actions created by GitHub" only (all used actions are `actions/*`,
   pinned by SHA); workflow permissions **read-only**; no PR approval by Actions.
2. `hasler`: branch protection on `master` (require `ci / test`, no force
   push, no deletion). Tag ruleset for `v*`: only you can create, nobody can
   update or delete. Repo variable `ATTESTATIONS=true` **only** on GitHub
   Enterprise Cloud (attestations in private repos need it); on Free, leave it
   unset - sign-publish then relies on run metadata + artifact digest.
   `ubuntu-24.04-arm` runners must be available to private repos on the plan;
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
  install.sh puts in NetworkManager/nginx/camilladsp/moOde places, which the
  integration allowlist has no destination for yet.
- No sudoers or config.txt block source in the repo yet; the builder ships
  `config/sudoers/haslerd` and `config/configtxt/hasler.block` (or a
  repo-level `integration/` tree) once they exist.
- Units still say `/home/pi`; `--strict` (CI) refuses that until they use
  `/opt/hasler/current`.
- `python/` (bundled CPython) is not built in v1: wheels target trixie's
  python3 (cp313).
