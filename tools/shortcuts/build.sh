#!/bin/sh
# Build and sign the iPhone shortcuts the makers host at
# https://delgado-murphy.github.io/shortcuts/ (macOS only: `shortcuts sign`).
#
#   tools/shortcuts/build.sh [SITE_REPO]      # default ../delgado-murphy.github.io
#
# 1. writes the unsigned files from daemon/haslerd/web/phone_api.py into
#    tools/shortcuts/unsigned/ (committed here; the tests read them);
# 2. signs each for anyone (`--mode anyone`: Apple countersigns, no iCloud
#    link needed; iOS 15+ opens the file straight into Shortcuts) into
#    SITE_REPO/shortcuts/. Commit there; GitHub Pages serves them.
# Rebuild after any change to phone_api.py, and after a macOS update if
# Apple ever stops accepting old signatures.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
SITE=${1:-"$ROOT/../delgado-murphy.github.io"}
OUT="$SITE/shortcuts"
[ -d "$SITE" ] || { echo "no site repo at $SITE" >&2; exit 1; }
command -v shortcuts >/dev/null || { echo "needs macOS (the shortcuts command)" >&2; exit 1; }

python3 "$HERE/make_shortcuts.py" "$HERE/unsigned" >/dev/null
mkdir -p "$OUT"
for f in "$HERE"/unsigned/*.shortcut; do
  name=$(basename "$f")
  # shortcuts prints harmless ObjC "attribute string" noise on stderr.
  shortcuts sign --mode anyone --input "$f" --output "$OUT/$name" 2>/dev/null
  head -c 4 "$OUT/$name" | grep -q AEA1 || { echo "signing failed: $name" >&2; exit 1; }
  chmod 644 "$OUT/$name"
  echo "signed  shortcuts/$name"
done
