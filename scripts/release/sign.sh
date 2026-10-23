#!/usr/bin/env bash
# Sign files with an OpenSSH key: FILE -> FILE.sig (docs/ota-format.md "Signatures").
#
#   sign.sh -n hasler-release -k ~/keys/release.key dist/release.json
#   sign.sh -n hasler-channel -e TIMESTAMP_SIGNING_KEY channels/stable.json   # key in an env var (CI)
#   sign.sh -n hasler-keyring -k /media/usb/root.key -p root.pub keyring.json
#
# -p PUB   refuse unless the signing key's public half is PUB (a file or an
#          "ssh-ed25519 AAAA..." string) - catches signing with the wrong key.
# Every signature is verified against the key's public half before exit.
# Namespaces: hasler-release (release.json), hasler-keyring (keyring.json, root
# keys), hasler-channel (channel indexes, timestamp key), hasler-components
# (components.json, the release key).
set -euo pipefail

NS=""; KEY=""; ENVVAR=""; EXPECT=""
usage() { sed -n '2,14p' "$0" >&2; exit 2; }
while getopts "n:k:e:p:h" o; do
  case "$o" in n) NS="$OPTARG";; k) KEY="$OPTARG";; e) ENVVAR="$OPTARG";; p) EXPECT="$OPTARG";; *) usage;; esac
done
shift $((OPTIND - 1))
[[ -n "$NS" && $# -ge 1 ]] || usage
case "$NS" in hasler-release|hasler-keyring|hasler-channel|hasler-components) ;; *) echo "sign: unknown namespace $NS" >&2; exit 2;; esac
if [[ -n "$KEY" && -n "$ENVVAR" ]] || [[ -z "$KEY" && -z "$ENVVAR" ]]; then usage; fi

TMP="$(umask 077; mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [[ -n "$ENVVAR" ]]; then
  [[ -n "${!ENVVAR:-}" ]] || { echo "sign: \$$ENVVAR is empty" >&2; exit 1; }
  (umask 077; printf '%s\n' "${!ENVVAR}" > "$TMP/key")
  KEY="$TMP/key"
fi
[[ -r "$KEY" ]] || { echo "sign: cannot read key $KEY" >&2; exit 1; }

# public half: KEY.pub if it is there (an encrypted root key then asks for its
# passphrase only once), else derived from the private key.
if [[ -f "$KEY.pub" ]]; then PUB="$(cut -d' ' -f1,2 < "$KEY.pub")"
else PUB="$(ssh-keygen -y -f "$KEY" | cut -d' ' -f1,2)"; fi
if [[ -n "$EXPECT" ]]; then
  [[ -f "$EXPECT" ]] && EXPECT="$(head -1 "$EXPECT")"
  if [[ "$(cut -d' ' -f1,2 <<<"$EXPECT")" != "$PUB" ]]; then
    echo "sign: this key is not the expected one ($PUB)" >&2; exit 1
  fi
fi
echo "signer namespaces=\"$NS\" $PUB" > "$TMP/allowed"

for f in "$@"; do
  rm -f "$f.sig"
  ssh-keygen -q -Y sign -f "$KEY" -n "$NS" "$f" >/dev/null
  ssh-keygen -q -Y verify -f "$TMP/allowed" -I signer -n "$NS" -s "$f.sig" < "$f" >/dev/null \
    || { echo "sign: fresh signature of $f does not verify" >&2; exit 1; }
  echo "signed $f -> $f.sig ($NS, $(ssh-keygen -lf <(echo "$PUB") | cut -d' ' -f2))"
done
