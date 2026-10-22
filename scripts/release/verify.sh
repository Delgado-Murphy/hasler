#!/usr/bin/env bash
# Verify an OpenSSH signature the way the box does (docs/ota-format.md).
#
#   verify.sh -n hasler-release -I release-2026a -k release.pub release.json [release.json.sig]
#   verify.sh -n hasler-release -I release-2026a -a allowed_signers release.json
#   verify.sh -n hasler-release -r release -K keyring.json release.json      # any release key in the keyring
#
# -k PUB  a public key file or an "ssh-ed25519 AAAA..." string
# -a FILE an allowed_signers file (e.g. from `keyring.py allowed-signers`)
# -K FILE a keyring.json (NOT itself verified here - use keyring.py verify) +
#         -r ROLE (release|timestamp); tries each key of that role, honouring not_after.
# Exit 0 = good signature.
set -euo pipefail

NS=""; ID=""; PUB=""; ALLOWED=""; KEYRING=""; ROLE=""
usage() { sed -n '2,12p' "$0" >&2; exit 2; }
while getopts "n:I:k:a:K:r:h" o; do
  case "$o" in n) NS="$OPTARG";; I) ID="$OPTARG";; k) PUB="$OPTARG";; a) ALLOWED="$OPTARG";;
    K) KEYRING="$OPTARG";; r) ROLE="$OPTARG";; *) usage;; esac
done
shift $((OPTIND - 1))
[[ -n "$NS" && $# -ge 1 && $# -le 2 ]] || usage
FILE="$1"; SIG="${2:-$1.sig}"
[[ -f "$FILE" && -f "$SIG" ]] || { echo "verify: missing $FILE or $SIG" >&2; exit 1; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

if [[ -n "$KEYRING" ]]; then
  [[ -n "$ROLE" ]] || usage
  python3 "$(dirname "$0")/keyring.py" allowed-signers --role "$ROLE" "$KEYRING" > "$TMP/allowed"
  while read -r kid _; do
    if ssh-keygen -q -Y verify -f "$TMP/allowed" -I "$kid" -n "$NS" -s "$SIG" < "$FILE" >/dev/null 2>&1; then
      echo "verify: good $NS signature on $FILE by $kid"; exit 0
    fi
  done < "$TMP/allowed"
  echo "verify: no $ROLE key in $KEYRING signed $FILE" >&2; exit 1
fi
if [[ -n "$PUB" ]]; then
  [[ -f "$PUB" ]] && PUB="$(head -1 "$PUB")"
  ID="${ID:-signer}"
  echo "$ID namespaces=\"$NS\" $(cut -d' ' -f1,2 <<<"$PUB")" > "$TMP/allowed"
  ALLOWED="$TMP/allowed"
fi
[[ -n "$ALLOWED" && -n "$ID" ]] || usage
if ssh-keygen -q -Y verify -f "$ALLOWED" -I "$ID" -n "$NS" -s "$SIG" < "$FILE" >/dev/null; then
  echo "verify: good $NS signature on $FILE by $ID"
else
  echo "verify: BAD signature on $FILE" >&2; exit 1
fi
