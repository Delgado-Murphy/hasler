#!/usr/bin/env bash
# THROWAWAY development keys for bench OTA drills - never for a box that leaves
# the bench. The production root/release/timestamp keys come from the key
# ceremony (docs/release.md); they replace these on the bench box with
#   sudo scripts/ota-bootstrap.sh --trust <ceremony trust dir> --replace-trust
#
#   scripts/release/dev-keys.sh [DIR]          # default ~/.hasler-dev/keys
#   scripts/release/dev-keys.sh --force [DIR]  # throw away an existing set
#
# Makes (no passphrases; DIR is 0700, keep it off the repo):
#   DIR/root-dev(.pub)        signs the keyring          (namespace hasler-keyring)
#   DIR/release-dev(.pub)     signs release.json         (hasler-release)
#   DIR/timestamp-dev(.pub)   signs the channel indexes  (hasler-channel)
#   DIR/keyring.json(.sig)    keyring_seq 1, one year
#   DIR/trust/                root.pub + keyring.json(.sig): what the box gets
#                             (ota-bootstrap.sh --trust DIR/trust)
# Then publish a locally built release to a local channel with
# scripts/release/dev-channel.py (docs/release.md "Bench box").
set -euo pipefail

FORCE=0
if [[ "${1:-}" == --force ]]; then FORCE=1; shift; fi
DIR="${1:-$HOME/.hasler-dev/keys}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="${PYTHON:-python3}"

case "$(cd "$(dirname "$DIR")" 2>/dev/null && pwd)/" in
  "$REPO"/*) echo "dev-keys: $DIR is inside the repo - keep private keys out of it" >&2; exit 1 ;;
esac
if [[ -e "$DIR/root-dev" ]]; then
  if (( ! FORCE )); then
    echo "dev-keys: $DIR already has a key set (--force to replace it; boxes trusting it must be re-bootstrapped)" >&2
    exit 1
  fi
  rm -rf "$DIR"
fi
mkdir -p "$DIR/trust"; chmod 0700 "$DIR"

for k in root release timestamp; do
  ssh-keygen -q -t ed25519 -N '' -C "hasler-DEV-$k (throwaway, bench only)" -f "$DIR/$k-dev"
done
"$PY" "$HERE/keyring.py" new --seq 1 \
  --key "release-dev:release:$DIR/release-dev.pub" \
  --key "timestamp-dev:timestamp:$DIR/timestamp-dev.pub" -o "$DIR/keyring.json" >/dev/null
"$PY" "$HERE/keyring.py" sign --root-key "$DIR/root-dev" "$DIR/keyring.json" >/dev/null
"$PY" "$HERE/keyring.py" verify --root "$DIR/root-dev.pub" "$DIR/keyring.json" >/dev/null
cp "$DIR/root-dev.pub" "$DIR/trust/root.pub"
cp "$DIR/keyring.json" "$DIR/keyring.json.sig" "$DIR/trust/"
chmod 0444 "$DIR/trust/"*

cat <<EOF
dev-keys: THROWAWAY key set in $DIR (bench drills only; replaced at the key ceremony)
  box trust:  $DIR/trust   ->  sudo scripts/ota-bootstrap.sh --trust <copy of it on the box>
  publish:    scripts/release/dev-channel.py publish --keys $DIR --dist dist/ --site ~/.hasler-dev/site \\
                  --base-url http://<this machine>:8000
EOF
