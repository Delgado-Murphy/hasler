#!/usr/bin/env bash
# Build an installable arm64 release on a laptop, for bench OTA drills (not for
# shipping - real releases come from CI, docs/release.md).
#
#   scripts/release/dev-build.sh 0.2.0-rc.1                 # from HEAD
#   scripts/release/dev-build.sh 0.2.0-rc.2 --worktree      # uncommitted changes too
#   scripts/release/dev-build.sh 0.2.0-rc.3 --broken        # rollback drill: haslerd exits at start
#   scripts/release/dev-build.sh 0.2.0-rc.1 --wheelhouse DIR  # prebuilt aarch64 wheels: no Docker
#
# Options: --ref REF (default HEAD), --out DIR (default ~/.hasler-dev/dist/<version>).
# The source is exported from git (or the checkout with --worktree, tracked and
# untracked-but-not-ignored files) into a temp dir, its daemon/pyproject.toml
# version set to <version>, and built with build_release.py --strict --bins
# --channel bench. Wheels are built in an arm64 debian:trixie container (Docker
# Desktop on Apple silicon runs it natively) and cached in ~/.hasler-dev/cache.
# Then: scripts/release/dev-channel.py publish --dist <out> ...
set -euo pipefail

VERSION="${1:?usage: dev-build.sh VERSION [--ref REF|--worktree] [--broken] [--out DIR] [--wheelhouse DIR]}"
shift
REF=HEAD; WORKTREE=0; BROKEN=0; OUT=""; WHEELHOUSE=""
while (( $# )); do
  case "$1" in
    --ref) REF="${2:?}"; shift ;;
    --worktree) WORKTREE=1 ;;
    --broken) BROKEN=1 ;;
    --out) OUT="${2:?}"; shift ;;
    --wheelhouse) WHEELHOUSE="$(cd "${2:?}" && pwd)"; shift ;;
    *) echo "dev-build: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-rc\.[1-9][0-9]*)?$ ]] || { echo "dev-build: version is X.Y.Z or X.Y.Z-rc.N" >&2; exit 2; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEV="${HASLER_DEV_DIR:-$HOME/.hasler-dev}"
OUT="${OUT:-$DEV/dist/$VERSION}"
CACHE="$DEV/cache"
mkdir -p "$OUT" "$CACHE"; OUT="$(cd "$OUT" && pwd)"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir "$WORK/src"

if (( WORKTREE )); then
  (cd "$REPO" && git ls-files -co --exclude-standard -z | tar --null -T - -cf - ) | tar -xf - -C "$WORK/src"
  SDE="$(date +%s)"
else
  git -C "$REPO" archive "$REF" | tar -xf - -C "$WORK/src"
  SDE="$(git -C "$REPO" log -1 --format=%ct "$REF")"
fi
PP="$WORK/src/daemon/pyproject.toml"
WH_KEY="$(grep -v '^version' "$PP" | shasum -a 256 | cut -c1-16)"
sed -i.bak -E "s/^version = \"[^\"]+\"/version = \"$VERSION\"/" "$PP" && rm -f "$PP.bak"
if (( BROKEN )); then
  M="$WORK/src/daemon/haslerd/main.py"
  grep -q '^    sys.exit(main())$' "$M" || { echo "dev-build: cannot find haslerd.main's entry line to break" >&2; exit 1; }
  sed -i.bak 's/^    sys.exit(main())$/    sys.exit("dev-build --broken: this release fails on purpose (rollback drill)")/' "$M"
  rm -f "$M.bak"
  echo "dev-build: --broken: haslerd exits at start (selftest still passes) - expect an automatic rollback" >&2
fi

BUILD_ARGS=(--version "$VERSION" --worktree --channel bench --bins --strict --source-date-epoch "$SDE")
if [[ -n "$WHEELHOUSE" ]]; then
  python3 "$WORK/src/scripts/release/build_release.py" "${BUILD_ARGS[@]}" --repo "$WORK/src" --out "$OUT" \
    --wheelhouse "$WHEELHOUSE" --bin-cache "$CACHE/bins"
else
  command -v docker >/dev/null || { echo "dev-build: needs Docker (or --wheelhouse DIR)" >&2; exit 1; }
  docker run --rm --platform linux/arm64 \
    -v "$WORK/src:/src" -v "$OUT:/out" -v "$CACHE:/cache" \
    -e WH_KEY="$WH_KEY" debian:trixie bash -euo pipefail -c "
      apt-get update -q >/dev/null
      DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
        ca-certificates curl git zstd unzip openssh-client python3 python3-venv python3-dev \
        python3-pip build-essential swig liblgpio-dev patchelf >/dev/null
      WH=/cache/wheelhouse-\$WH_KEY
      if [ ! -f \$WH/.done ]; then
        python3 -m venv /opt/uv && /opt/uv/bin/pip install -q 'uv==0.13.2' && ln -sf /opt/uv/bin/uv /usr/local/bin/uv
        rm -rf \$WH && /src/scripts/release/build_wheels.sh /src/daemon \$WH && touch \$WH/.done
      fi
      python3 /src/scripts/release/build_release.py ${BUILD_ARGS[*]} --repo /src --out /out \
        --wheelhouse \$WH --bin-cache /cache/bins
    "
fi
echo "dev-build: $OUT"
ls -l "$OUT"
