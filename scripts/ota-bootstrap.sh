#!/usr/bin/env bash
# Put the OTA updater on a box, once (docs/ota.md, docs/release.md "Bench box").
# Run as root from a checkout on the box, after scripts/bootstrap.sh.
#
#   sudo scripts/ota-bootstrap.sh --trust DIR [--update-url URL] [--dry-run]
#
#   --trust DIR        install DIR/root*.pub, keyring.json, keyring.json.sig into
#                      /etc/hasler/trust (root, 0444) after checking that a root
#                      key there signed the keyring. Root keys are install-once:
#   --replace-trust    ...replacing different ones needs this (dev keys ->
#                      production keys after the key ceremony).
#   --update-url URL   set update_url in /etc/hasler/box.yaml (a local channel for
#                      bench drills, e.g. http://laptop.local:8000); "" removes
#                      it (back to the public channel). Needs box.yaml, i.e.
#                      migration 001 done - re-run with just this flag after it.
#   --force-updater    reinstall the updater from this checkout even though a
#                      release has committed (normally the release's updater wins)
#   --user NAME        the daemon user (default pi)
#   --dry-run          show what it would do
#
# Idempotent. What it does:
#   - user hasler-update; /var/lib/hasler-update (hasler-update, readable by all),
#     /var/lib/hasler-apply and /opt/hasler/releases (root, 0755); /etc/hasler and
#     /etc/hasler/variant.d root:pi 1775 (haslerd replaces box.yaml / overlays,
#     which it owns; the sticky bit keeps it from renaming trust/ aside);
#     /etc/hasler/trust root 0755 with 0444 files
#   - until the first release commits: the updater from this checkout
#     (update/hasler_update -> /opt/hasler/updater/, update/bin/hasler-update ->
#     /usr/local/sbin/hasler-update) and its units (update/systemd/)
#   - polkit rule (the daemon user may start hasler-update-apply@<valid>.service
#     and hasler-update-check.service, nothing else), hardware watchdog
#   - go-librespot's config + login: ~pi/.config/go-librespot -> /var/lib/go-librespot
#     (the release's unit reads it there); the old copy is left alone
#   - config.txt: the legacy install.sh lamp block re-marked as the updater's
#     block when its lines are what the release ships (so no reboot is needed)
#   - enables hasler-update-check.timer, hasler-update-apply.timer,
#     hasler-update-boot.service, hasler-update-resume.service
set -euo pipefail

DRY=0; TRUST=""; REPLACE_TRUST=0; FORCE_UPD=0; DUSER=pi
SET_URL=0; URL=""
while (( $# )); do
  case "$1" in
    --dry-run) DRY=1 ;;
    --trust) TRUST="${2:?--trust DIR}"; shift ;;
    --replace-trust) REPLACE_TRUST=1 ;;
    --update-url) SET_URL=1; URL="${2?--update-url URL}"; shift ;;
    --force-updater) FORCE_UPD=1 ;;
    --user) DUSER="${2:?--user NAME}"; shift ;;
    -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
    *) echo "ota-bootstrap: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${HASLER_ROOT:-}"          # tests: a fake root; never set on a box
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }
die()  { echo "ota-bootstrap: $*" >&2; exit 1; }
run()  { if (( DRY )); then printf '   would run: %s\n' "$*"; else "$@"; fi; }
PY=/usr/bin/python3; [[ -x "$PY" ]] || PY="$(command -v python3)"

if (( ! DRY )) && [[ -z "$ROOT" && "$(id -u)" != 0 ]]; then die "run as root (sudo $0)"; fi
SYSTEMD=0; [[ -z "$ROOT" ]] && command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] && SYSTEMD=1
OWN=1; [[ -n "$ROOT" || "$(id -u)" != 0 ]] && OWN=0     # chown only as real root

# put SRC DEST MODE [OWNER:GROUP] - install if different; status 0 = changed
put() {
  local src="$1" dest="$ROOT$2" mode="$3" owner="${4:-root:root}"
  if cmp -s "$src" "$dest" 2>/dev/null; then return 1; fi
  if (( DRY )); then printf '   would install %s -> %s (%s)\n' "${src#$REPO/}" "$dest" "$mode"; return 0; fi
  mkdir -p "$(dirname "$dest")"
  local tmp; tmp="$(mktemp "$(dirname "$dest")/.ota-bootstrap.XXXXXX")"
  cat "$src" > "$tmp"; chmod "$mode" "$tmp"
  (( OWN )) && chown "$owner" "$tmp"
  mv -f "$tmp" "$dest"
}
mkdir_owned() {   # DIR MODE OWNER:GROUP
  local d="$ROOT$1"
  if (( DRY )); then [[ -d "$d" ]] || printf '   would create %s (%s %s)\n' "$d" "$2" "$3"; return 0; fi
  mkdir -p "$d"; chmod "$2" "$d"
  (( OWN )) && chown "$3" "$d"
  return 0
}

for tool in ssh-keygen zstd; do
  command -v "$tool" >/dev/null 2>&1 || warn "$tool missing - the updater needs it (scripts/bootstrap.sh installs it)"
done

# --- user, directories ------------------------------------------------------------
if [[ -z "$ROOT" ]] && ! getent passwd hasler-update >/dev/null; then
  say "creating system user hasler-update"
  run useradd --system --user-group --home-dir /var/lib/hasler-update --no-create-home \
      --shell /usr/sbin/nologin hasler-update
fi
say "directories"
DGROUP="$(id -gn "$DUSER" 2>/dev/null)" || DGROUP="$DUSER"
mkdir_owned /var/lib/hasler-update 0755 hasler-update:hasler-update
mkdir_owned /var/lib/hasler-apply  0755 root:root
# haslerd ($DUSER) replaces box.yaml and the calibration overlay atomically (tmp +
# rename), so it needs write on these dirs. The sticky bit (1775) is what keeps
# it away from trust/: in a sticky dir only an entry's owner may rename or
# delete it, and trust/ is root's - without it, pi could rename trust/ aside and
# plant its own keys. So box.yaml and the overlays must be owned by $DUSER.
mkdir_owned /etc/hasler            1775 "root:$DGROUP"
mkdir_owned /etc/hasler/variant.d  1775 "root:$DGROUP"
mkdir_owned /etc/hasler/trust      0755 root:root
mkdir_owned /opt/hasler            0755 root:root
mkdir_owned /opt/hasler/releases   0755 root:root
if (( ! DRY && OWN )); then
  for f in "$ROOT/etc/hasler/box.yaml" "$ROOT"/etc/hasler/variant.d/*.yaml; do
    [[ -f "$f" && ! -L "$f" ]] || continue
    chown "$DUSER:$DGROUP" "$f"; chmod 0644 "$f"
  done
  # trust: root only, read-only files
  find "$ROOT/etc/hasler/trust" -mindepth 1 -maxdepth 1 -type f -exec chown root:root {} + -exec chmod 0444 {} +
  # haslerd reads status.json and staged/<seq>/STAGED (the check unit runs with UMask=0022)
  [[ -d "$ROOT/var/lib/hasler-update" ]] && chmod -R go+rX,go-w "$ROOT/var/lib/hasler-update"
fi

# --- the updater (until a release brings its own) ------------------------------------
UNITS_CHANGED=0
if [[ -L "$ROOT/opt/hasler/current" && $FORCE_UPD == 0 ]]; then
  say "a release is installed: keeping its updater (--force-updater to replace it from this checkout)"
else
  say "updater from this checkout -> /opt/hasler/updater, /usr/local/sbin/hasler-update"
  SRC="$REPO/update/hasler_update"
  DST="$ROOT/opt/hasler/updater"
  if diff -rq -x __pycache__ -x tests "$SRC" "$DST/hasler_update" >/dev/null 2>&1; then
    :
  elif (( DRY )); then
    echo "   would copy update/hasler_update -> $DST/hasler_update"
  else
    rm -rf "$DST.new" "$DST.old"
    mkdir -p "$DST.new"
    cp -R "$SRC" "$DST.new/hasler_update"
    find "$DST.new" -name __pycache__ -type d -prune -exec rm -rf {} +
    rm -rf "$DST.new/hasler_update/tests"
    chmod -R u=rwX,go=rX "$DST.new"
    (( OWN )) && chown -R root:root "$DST.new"
    [[ -e "$DST" ]] && mv "$DST" "$DST.old"
    mv "$DST.new" "$DST"
    rm -rf "$DST.old"
  fi
  put "$REPO/update/bin/hasler-update" /usr/local/sbin/hasler-update 0755 || true
  for f in "$REPO"/update/systemd/*; do
    put "$f" "/etc/systemd/system/$(basename "$f")" 0644 && UNITS_CHANGED=1
  done
fi

# --- polkit, watchdog ---------------------------------------------------------------
# The polkit rule names the daemon user; it is not part of a release.
POLKIT_SRC="$REPO/config/update/50-hasler-update.rules"
if [[ "$DUSER" != pi ]]; then
  POLKIT_SRC="$(mktemp)"; sed "s/subject.user == \"pi\"/subject.user == \"$DUSER\"/" \
    "$REPO/config/update/50-hasler-update.rules" > "$POLKIT_SRC"
fi
[[ -d "$ROOT/etc/polkit-1" || $DRY == 1 || -n "$ROOT" ]] \
  || warn "no /etc/polkit-1 (polkitd not installed?) - the key page's update buttons will be refused"
put "$POLKIT_SRC" /etc/polkit-1/rules.d/50-hasler-update.rules 0644 || true
WATCHDOG_CHANGED=0
put "$REPO/config/update/system.conf.d/hasler-watchdog.conf" \
    /etc/systemd/system.conf.d/hasler-watchdog.conf 0644 && WATCHDOG_CHANGED=1

# --- trust --------------------------------------------------------------------------
if [[ -n "$TRUST" ]]; then
  say "trust: $TRUST -> /etc/hasler/trust"
  [[ -f "$TRUST/keyring.json" && -f "$TRUST/keyring.json.sig" ]] || die "$TRUST needs keyring.json and keyring.json.sig"
  ls "$TRUST"/root*.pub >/dev/null 2>&1 || die "$TRUST has no root*.pub"
  # The box's own check: a root key in DIR signed the keyring, and it has not expired.
  SEQ="$("$PY" -I - "$REPO/update" "$TRUST" <<'EOF'
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hasler_update.verify import VerifyError, parse_keyring
d = Path(sys.argv[2])
try:
    ring = parse_keyring((d / "keyring.json").read_bytes(), (d / "keyring.json.sig").read_bytes(), d, time.time())
except VerifyError as e:
    sys.exit(f"keyring does not verify: {e}")
print(ring.seq)
EOF
)" || die "refusing $TRUST"
  say "trust: keyring seq $SEQ verifies against $(cd "$TRUST" && echo root*.pub)"
  T="$ROOT/etc/hasler/trust"
  for have in "$T"/root*.pub; do
    [[ -e "$have" ]] || continue
    if ! cmp -s "$have" "$TRUST/$(basename "$have")"; then
      (( REPLACE_TRUST )) || die "$have differs from $TRUST - root keys are install-once (--replace-trust to swap them)"
      run rm -f "$have"
    fi
  done
  for f in "$TRUST"/root*.pub "$TRUST/keyring.json" "$TRUST/keyring.json.sig"; do
    put "$f" "/etc/hasler/trust/$(basename "$f")" 0444 || true
  done
elif ! ls "$ROOT"/etc/hasler/trust/root*.pub >/dev/null 2>&1; then
  warn "no trust installed yet - the updater refuses everything until: --trust DIR"
fi

# --- go-librespot: config + login outside the release -----------------------------------
DHOME="$(getent passwd "$DUSER" 2>/dev/null | cut -d: -f6)" || true
GLR_NEW="$ROOT/var/lib/go-librespot"
GLR_OLD="$ROOT${DHOME:-/home/$DUSER}/.config/go-librespot"
if [[ ! -f "$GLR_NEW/config.yml" ]]; then
  mkdir_owned /var/lib/go-librespot 0700 "$DUSER:$DGROUP"
  if [[ -d "$GLR_OLD" ]]; then
    say "go-librespot: copying $GLR_OLD -> /var/lib/go-librespot (login kept)"
    if (( ! DRY )); then
      cp -a "$GLR_OLD/." "$GLR_NEW/"
      (( OWN )) && chown -R "$DUSER:$DGROUP" "$GLR_NEW"
    fi
  else
    CARD_ID="$(grep -i hifiberry "$ROOT/proc/asound/cards" 2>/dev/null | head -1 | sed 's/.*\[\([^] ]*\).*/\1/')" || true
    say "go-librespot: new config in /var/lib/go-librespot (ALSA card ${CARD_ID:-sndrpihifiberry})"
    if (( ! DRY )); then
      sed "s/CARD=sndrpihifiberry/CARD=${CARD_ID:-sndrpihifiberry}/" "$REPO/config/go-librespot/config.yml" > "$GLR_NEW/config.yml"
      chmod 0644 "$GLR_NEW/config.yml"
      (( OWN )) && chown "$DUSER:$DGROUP" "$GLR_NEW/config.yml"
    fi
  fi
fi

# --- config.txt: legacy lamp block -> the updater's block ---------------------------------
CFG="$ROOT/boot/firmware/config.txt"; [[ -f "$CFG" ]] || CFG="$ROOT/boot/config.txt"
if [[ -f "$CFG" ]]; then
  RES="$("$PY" -I - "$CFG" "$REPO/config/configtxt/hasler.block" "$DRY" <<'EOF'
import sys
cfg, block_file, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
LEGACY = ("# >>> hasler pilot lamp (scripts/install.sh; edits here are replaced) >>>", "# <<< hasler pilot lamp <<<")
NEW = ("# >>> hasler (managed by hasler-update; edits here are replaced) >>>", "# <<< hasler <<<")
text = open(cfg, encoding="utf-8").read()
lines = text.splitlines()
block = open(block_file, encoding="utf-8").read().strip("\n")
def meaningful(ls):
    return [l.strip() for l in ls if l.strip() and not l.strip().startswith("#")]
if NEW[0] in lines:
    print("already the updater's block"); sys.exit(0)
if LEGACY[0] not in lines or LEGACY[1] not in lines:
    print("no lamp block (the first release adds it, which needs: apply --allow-reboot)"); sys.exit(0)
b, e = lines.index(LEGACY[0]), lines.index(LEGACY[1])
if meaningful(lines[b + 1:e]) != meaningful(block.splitlines()):
    print("legacy lamp block differs from the release's: the first release needs apply --allow-reboot"); sys.exit(0)
out = lines[:b] + lines[e + 1:]
while out and not out[-1].strip():
    out.pop()
new = "\n".join(out) + "\n" + NEW[0] + "\n" + block + "\n" + NEW[1] + "\n"
if dry:
    print("would re-mark the legacy lamp block as the updater's (same lines, no reboot)"); sys.exit(0)
open(cfg + ".hasler-bak", "w", encoding="utf-8").write(text)
with open(cfg, "w", encoding="utf-8") as f:
    f.write(new)
    f.flush()
    import os; os.fsync(f.fileno())
print("re-marked the legacy lamp block as the updater's (same lines, no reboot; backup .hasler-bak)")
EOF
)" || RES="could not read $CFG"
  say "config.txt: $RES"
fi

# --- box.yaml: update_url -------------------------------------------------------------------
if (( SET_URL )); then
  BOX="$ROOT/etc/hasler/box.yaml"
  if [[ ! -f "$BOX" ]]; then
    warn "no $BOX yet - run migration 001 first, then: $0 --update-url '$URL'"
  elif (( DRY )); then
    echo "   would set update_url: ${URL:-(removed)} in $BOX"
  else
    "$PY" -I - "$BOX" "$URL" <<'EOF'
import os, re, sys
path, url = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
lines = [l for l in text.splitlines() if not re.match(r"^update_url\s*:", l)]
if url:
    lines.append(f"update_url: {url}   # bench: a local channel (scripts/release/dev-channel.py); remove for the public one")
st = os.stat(path)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
    f.flush(); os.fsync(f.fileno())
os.chmod(tmp, st.st_mode & 0o7777)
try:
    os.chown(tmp, st.st_uid, st.st_gid)
except PermissionError:
    pass
os.replace(tmp, path)
EOF
    say "box.yaml: update_url ${URL:-removed (public channel)}"
  fi
fi

# --- systemd ------------------------------------------------------------------------------------
if (( SYSTEMD )); then
  if (( WATCHDOG_CHANGED && ! DRY )); then
    say "hardware watchdog on (systemctl daemon-reexec)"
    systemctl daemon-reexec
  fi
  (( UNITS_CHANGED )) && run systemctl daemon-reload
  run systemctl enable --quiet hasler-update-boot.service hasler-update-resume.service
  run systemctl enable --quiet --now hasler-update-check.timer hasler-update-apply.timer
fi

say "OTA updater ready"
cat <<EOF
   status:      hasler-update status
   check now:   sudo systemctl start hasler-update-check.service && cat /var/lib/hasler-update/status.json
   next (bench box, docs/release.md "Bench box"): migration 001 (--check, then --apply),
   then the first release: sudo hasler-update apply --seq <N> --force
EOF
