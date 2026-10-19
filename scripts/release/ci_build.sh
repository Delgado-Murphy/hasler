#!/usr/bin/env bash
# The release build, run inside an arm64 debian:trixie container. On a GitHub
# Free plan with a private repo there are no arm64 runners, so release.yml runs
# this under QEMU on an x86 runner (slow, ~15-30 min, fine for releases).
# Env: TAG (vX.Y.Z[-rc.N]), RUNNER_TEMP, GITHUB_STEP_SUMMARY (optional).
set -euo pipefail
cd "$(dirname "$0")/../.."
RUNNER_TEMP=${RUNNER_TEMP:-/tmp/rt}; mkdir -p "$RUNNER_TEMP"
GITHUB_STEP_SUMMARY=${GITHUB_STEP_SUMMARY:-/dev/null}

apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
  ca-certificates curl git zstd unzip openssh-client \
  python3 python3-venv python3-dev python3-pip build-essential swig liblgpio-dev patchelf
# uv only compiles the lock if daemon/requirements-pi.lock is not committed
python3 -m venv /opt/uv && /opt/uv/bin/pip install -q 'uv==0.13.2' && ln -sf /opt/uv/bin/uv /usr/local/bin/uv

git config --global --add safe.directory "$PWD"
echo "$TAG" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-rc\.[1-9][0-9]*)?$' \
  || { echo "::error::tag $TAG is not vX.Y.Z or vX.Y.Z-rc.N"; exit 1; }
VERSION=${TAG#v}
export SOURCE_DATE_EPOCH=$(git log -1 --format=%ct "$TAG")

# Wheelhouse (aarch64 cp313, lgpio built once and repaired)
scripts/release/build_wheels.sh daemon "$RUNNER_TEMP/wheelhouse"

# --strict: tag == pyproject version, units free of /home/pi, wheels repaired or apt deps declared
python3 scripts/release/build_release.py --version "$VERSION" --ref "$TAG" --out dist \
  --wheelhouse "$RUNNER_TEMP/wheelhouse" --bins --bin-cache "$RUNNER_TEMP/bins" --strict
cp "$RUNNER_TEMP/wheelhouse/source.lock" dist/requirements-pi.source.lock 2>/dev/null || true

# Reproducible: a second build gives the same bytes
python3 scripts/release/build_release.py --version "$VERSION" --ref "$TAG" --out "$RUNNER_TEMP/dist2" \
  --wheelhouse "$RUNNER_TEMP/wheelhouse" --bins --bin-cache "$RUNNER_TEMP/bins" --strict
cmp dist/hasler-$VERSION-arm64.tar.zst "$RUNNER_TEMP/dist2/hasler-$VERSION-arm64.tar.zst"
cmp dist/release.json "$RUNNER_TEMP/dist2/release.json"

# Offline install test, as the box does it
python3 scripts/release/make_manifest.py check --release dist/release.json \
  --tarball dist/hasler-$VERSION-arm64.tar.zst
if ! python3 -c 'import json,sys; sys.exit("apt" in json.load(open("dist/release.json"))["requires"])'; then
  echo "release declares apt deps; keeping liblgpio installed"
else
  apt-get purge -y -q liblgpio-dev 'liblgpio1*' || true
fi
t="$RUNNER_TEMP/box"; mkdir -p "$t"
zstd -dc dist/hasler-$VERSION-arm64.tar.zst | tar -x -C "$t"
r="$t/hasler-$VERSION"
python3 -m venv "$t/venv"
"$t/venv/bin/pip" install -q --no-index --find-links "$r/wheelhouse" --require-hashes -r "$r/requirements.lock"
"$t/venv/bin/pip" check
(cd "$r/app" && "$t/venv/bin/python" -c \
  'import haslerd.main, lgpio, gpiozero, smbus2, mpd, paho.mqtt.client, ytmusicapi, yaml; print("imports ok")')

# SBOM (CycloneDX, optional)
( python3 -m venv "$RUNNER_TEMP/sbom" && "$RUNNER_TEMP/sbom/bin/pip" install -q 'cyclonedx-bom==7.5.0' \
  && "$RUNNER_TEMP/sbom/bin/cyclonedx-py" environment "$t/venv" --output-format JSON -o dist/hasler-$VERSION.cdx.json ) || true

# Checksums + summary
( cd dist && sha256sum * > SHA256SUMS && cat SHA256SUMS
  { echo "### hasler $VERSION"
    echo "Tarball sha256 for sign-publish:"
    echo '```'; sha256sum hasler-$VERSION-arm64.tar.zst | cut -d' ' -f1; echo '```'
  } >> "$GITHUB_STEP_SUMMARY" )
echo "version=$VERSION" > dist/.version
chown -R "${HOST_UID:-0}:${HOST_GID:-0}" dist || true
