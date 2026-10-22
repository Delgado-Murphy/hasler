#!/usr/bin/env bash
# Build the aarch64 cp313 wheelhouse for haslerd[pi]. arm64 Debian trixie only
# (CI: release.yml, debian:trixie container on ubuntu-24.04-arm).
#
#   scripts/release/build_wheels.sh daemon/ wheelhouse/
#
# 1. lock: daemon/requirements-pi.lock if committed, else `uv pip compile
#    --generate-hashes` now (and the lock is copied next to the wheelhouse so
#    it can be committed).
# 2. `pip wheel --require-hashes` every pinned dist (sdists like lgpio are
#    built once here - needs swig + liblgpio-dev; boxes never compile).
# 3. `auditwheel repair` any linux_aarch64 wheel, vendoring its C libraries
#    (liblgpio). If repair fails the wheel is kept as is and the Debian
#    packages providing its libraries are written to <wheelhouse>/apt-deps.txt,
#    which build_release.py turns into release.json requires.apt.
#
# Needs: python3 (3.13), python3-venv, python3-dev, build-essential, swig,
# liblgpio-dev, patchelf, uv. Installs pip tools into a throwaway venv.
#
# WHEEL_CACHE=<dir> (optional, CI): a finished wheelhouse is kept under
# <dir>/<key>, key = sha256 of the lock + this script + the toolchain package
# versions. Same key next time -> copied, nothing built (lgpio is the slow
# one). Older keys are pruned. The lock's hashes still pin every input.
set -euo pipefail

DAEMON="$(cd "${1:?daemon dir}" && pwd)"
OUT="${2:?wheelhouse dir}"
PY="${PYTHON:-python3}"
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"

[[ "$(uname -s)/$(uname -m)" == Linux/aarch64 ]] || { echo "build_wheels: arm64 Linux only" >&2; exit 1; }
"$PY" -c 'import sys; assert sys.version_info[:2] == (3, 13), sys.version' \
  || { echo "build_wheels: need CPython 3.13 (trixie's python3)" >&2; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
LOCK="$DAEMON/requirements-pi.lock"
if [[ ! -f "$LOCK" ]]; then
  command -v uv >/dev/null || { echo "build_wheels: no $LOCK and no uv to compile one" >&2; exit 1; }
  echo "build_wheels: compiling a fresh lock (commit daemon/requirements-pi.lock to pin it)" >&2
  LOCK="$WORK/requirements-pi.lock"
  (cd "$DAEMON" && uv pip compile pyproject.toml --extra pi --generate-hashes \
      --python "$PY" --no-header --quiet -o "$LOCK")
fi
cp "$LOCK" "$OUT/source.lock"   # the input lock, for the record (not shipped)

if [[ -n "${WHEEL_CACHE:-}" ]]; then
  KEY="$( { cat "$LOCK" "${BASH_SOURCE[0]}"; uname -m; "$PY" -c 'import sys; print(sys.version_info[:2])'
            dpkg-query -W -f='${Package} ${Version}\n' liblgpio-dev swig python3-dev patchelf 2>/dev/null || true
          } | sha256sum | cut -c1-32)"
  HIT="$WHEEL_CACHE/$KEY"
  if [[ -f "$HIT/.complete" ]]; then
    echo "build_wheels: wheelhouse cache hit ($KEY)" >&2
    find "$HIT" -maxdepth 1 -type f ! -name .complete ! -name source.lock -exec cp {} "$OUT/" \;
    ls -1 "$OUT" >&2
    exit 0
  fi
  echo "build_wheels: wheelhouse cache miss ($KEY), building" >&2
fi

"$PY" -m venv "$WORK/venv"
VPY="$WORK/venv/bin/python"
# Tool versions pinned; TODO: hash-lock the tools too (scripts/release/tools.lock).
"$VPY" -m pip install -q 'pip==26.2.1' 'auditwheel==6.8.2' 'wheel==0.48.0'

"$VPY" -m pip wheel -q --require-hashes --no-deps -r "$LOCK" -w "$WORK/raw"

: > "$WORK/apt-deps"
for whl in "$WORK"/raw/*.whl; do
  case "$whl" in
    *-linux_aarch64.whl)
      if "$WORK/venv/bin/auditwheel" repair -q -w "$OUT" "$whl"; then
        echo "build_wheels: repaired $(basename "$whl")" >&2
      else
        echo "build_wheels: auditwheel could not repair $(basename "$whl"); keeping it + apt deps" >&2
        cp "$whl" "$OUT/"
        x="$WORK/x/$(basename "$whl")"; mkdir -p "$x"
        "$VPY" -m zipfile -e "$whl" "$x"
        find "$x" -name '*.so*' -print0 | xargs -0 -r ldd 2>/dev/null \
          | awk '$3 ~ /^\// {print $3}' | sort -u \
          | xargs -r -n1 sh -c 'dpkg -S "$(readlink -f "$0")" 2>/dev/null || dpkg -S "$0"' \
          | cut -d: -f1 | grep -vE '^(libc6|libgcc-s1|libstdc\+\+6)$' >> "$WORK/apt-deps" || true
      fi ;;
    *) cp "$whl" "$OUT/" ;;
  esac
done
sort -u "$WORK/apt-deps" > "$WORK/apt-deps.sorted"
if [[ -s "$WORK/apt-deps.sorted" ]]; then
  cp "$WORK/apt-deps.sorted" "$OUT/apt-deps.txt"
  echo "build_wheels: apt runtime deps: $(tr '\n' ' ' < "$OUT/apt-deps.txt")" >&2
else
  rm -f "$OUT/apt-deps.txt"
fi
if [[ -n "${WHEEL_CACHE:-}" ]]; then
  mkdir -p "$WHEEL_CACHE"
  find "$WHEEL_CACHE" -mindepth 1 -maxdepth 1 ! -name "$KEY" -exec rm -rf {} +
  rm -rf "$HIT"; mkdir -p "$HIT"
  cp "$OUT"/* "$HIT/" && touch "$HIT/.complete"
fi
ls -1 "$OUT" >&2
