#!/usr/bin/env bash
# One-time box setup for a Hasler: everything that is not part of a release.
# Run as root from a checkout on the box (scripts/install.sh calls it for the
# legacy layout; on an OTA box run it before scripts/ota-bootstrap.sh).
#
#   sudo scripts/bootstrap.sh [--user pi] [--dry-run]
#
# Idempotent: safe to re-run (after a moOde update, re-run it for the moOde
# patch). What it does:
#   - apt packages the box needs at runtime (zstd, liblgpio1, ...)
#   - I2C on, the daemon user in gpio/i2c/audio
#   - system users: hasler-update (the unprivileged updater), ytdlp (yt-dlp)
#   - /var/lib/haslerd
#   - CamillaDSP "Hasler Tone.yml" (seeded once, owned by the daemon user, 0644)
#   - the captive portal for moOde's fallback hotspot (dnsmasq + nginx)
#   - moOde: playlist-cover permissions, the YouTube cover-art patch
#   - yt-dlp off root: /opt/yt-dlp owned by ytdlp, two venv slots, a weekly
#     upgrade that switches only after a self-check, polkit for "update now"
#   - Spotify (go-librespot) login hints
# What it does not do: install haslerd itself (install.sh for the legacy rsync
# layout, the updater for /opt/hasler releases) or anything in a release
# (units, sudoers, udev, config.txt block, helpers - integration/).
set -euo pipefail

DRY=0
DUSER=pi
while (( $# )); do
  case "$1" in
    --dry-run) DRY=1 ;;
    --user) DUSER="${2:?--user NAME}"; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "bootstrap: unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }
run()  { if (( DRY )); then printf '   would run: %s\n' "$*"; else "$@"; fi; }
REBOOT_NEEDED=0

if (( ! DRY )) && [[ "$(id -u)" != 0 ]]; then
  echo "bootstrap: run as root (sudo $0)" >&2; exit 1
fi
getent passwd "$DUSER" >/dev/null || { echo "bootstrap: no user $DUSER" >&2; exit 1; }
DHOME="$(getent passwd "$DUSER" | cut -d: -f6)"
DGROUP="$(id -gn "$DUSER")"
SYSTEMD=0; command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] && SYSTEMD=1

# put SRC DEST MODE [OWNER:GROUP] - install if different; status 0 = changed
put() {
  local src="$1" dest="$2" mode="$3" owner="${4:-root:root}"
  if cmp -s "$src" "$dest" 2>/dev/null; then
    run chown "$owner" "$dest"; run chmod "$mode" "$dest"; return 1
  fi
  if (( DRY )); then printf '   would install %s -> %s (%s %s)\n' "$src" "$dest" "$mode" "$owner"; return 0; fi
  install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$src" "$dest"
}

say "box setup for daemon user $DUSER (repo $REPO)"
grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null || warn "this does not look like a Raspberry Pi"
command -v moodeutl >/dev/null 2>&1 || warn "moodeutl not found - is this a moOde image?"

# --- apt -------------------------------------------------------------------------
# zstd: release tarballs. liblgpio1: lgpio's C library (if a release's wheel
# is not self-contained). python3-venv: release venvs and yt-dlp's. The rest:
# yt-dlp's deno zip, i2cdetect, polkit for the key page's update buttons.
PKGS=(zstd liblgpio1 python3-venv curl unzip i2c-tools polkitd)
MISSING=()
for p in "${PKGS[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p"); done
if (( ${#MISSING[@]} )); then
  say "apt: installing ${MISSING[*]}"
  run apt-get install -y -q --no-install-recommends "${MISSING[@]}" \
    || warn "apt-get failed for ${MISSING[*]} - (apt-get update first?)"
fi

# --- I2C, groups -------------------------------------------------------------------
if [[ ! -e /dev/i2c-1 ]]; then
  if command -v raspi-config >/dev/null 2>&1; then
    say "enabling I2C (takes effect after a reboot)"
    run raspi-config nonint do_i2c 0
    REBOOT_NEEDED=1
  else
    warn "/dev/i2c-1 missing and no raspi-config - enable I2C by hand"
  fi
fi
for group in gpio i2c audio; do
  if getent group "$group" >/dev/null && ! id -nG "$DUSER" | grep -qw "$group"; then
    say "adding $DUSER to $group"
    run usermod -aG "$group" "$DUSER"
  fi
done

# --- system users ------------------------------------------------------------------
sysuser() {   # NAME HOME
  getent passwd "$1" >/dev/null && return 0
  say "creating system user $1"
  run useradd --system --user-group --home-dir "$2" --no-create-home --shell /usr/sbin/nologin "$1"
}
sysuser hasler-update /var/lib/hasler-update
sysuser ytdlp /opt/yt-dlp

# --- state -------------------------------------------------------------------------
if [[ ! -d /var/lib/haslerd ]]; then
  say "creating /var/lib/haslerd"
  run install -d -o "$DUSER" -g "$DGROUP" -m 0755 /var/lib/haslerd
fi

# --- tone knob: CamillaDSP config -----------------------------------------------------
# hasler-tone (run by haslerd as $DUSER) rewrites the gains in this file, so
# $DUSER owns it; nobody else may write it (it was 0666 before).
CDSP_DIR=/usr/share/camilladsp/configs
CDSP_TONE="$CDSP_DIR/Hasler Tone.yml"
if [[ -d "$CDSP_DIR" ]]; then
  if [[ ! -e "$CDSP_TONE" ]]; then
    say "CamillaDSP: seeding Hasler Tone.yml"
    run install -m 0644 -o "$DUSER" -g "$DGROUP" "$REPO/config/camilladsp/Hasler Tone.yml" "$CDSP_TONE"
    run ln -sf "$CDSP_TONE" /usr/share/camilladsp/working_config.yml
    run sh "$REPO/scripts/hasler-audio-mono" dsp-on || warn "could not route through CamillaDSP (hasler-audio-mono dsp-on)"
  else
    run chown "$DUSER:$DGROUP" "$CDSP_TONE"
    run chmod 0644 "$CDSP_TONE"
  fi
fi

# --- captive portal for the fallback hotspot -----------------------------------------
# moOde starts the "Hasler" hotspot when no known Wi-Fi answers at boot. Every
# name resolves to the Pi (hotspot only) and the phones' connectivity checks
# are redirected to haslerd's Wi-Fi page.
put "$REPO/config/captive/dnsmasq-hotspot.conf" /etc/NetworkManager/dnsmasq-shared.d/hasler-captive.conf 0644 || true
if [[ -d /etc/nginx/sites-enabled ]] && put "$REPO/config/captive/nginx-captive.conf" \
     /etc/nginx/sites-enabled/hasler-captive.conf 0644 && (( ! DRY )); then
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx
  else
    warn "nginx rejected hasler-captive.conf - removing it; moOde's web UI is unaffected"
    rm -f /etc/nginx/sites-enabled/hasler-captive.conf
  fi
fi

# --- moOde ---------------------------------------------------------------------------
# haslerd writes "YT · ..." playlist covers to moOde's root-owned covers dir.
COVERS=/var/local/www/imagesw/playlist-covers
if [[ -d "$COVERS" ]] && [[ "$(stat -c %G "$COVERS")" != audio ]]; then
  run chgrp audio "$COVERS"; run chmod 2775 "$COVERS"
fi
# Album covers and quick titles for YouTube Music tracks (re-run after a moOde update).
if [[ -f /var/www/inc/mpd.php ]]; then
  if (( DRY )); then
    echo "   would patch moOde for YouTube cover art (config/moode/patch-ytm-art.py)"
  else
    say "moOde YouTube cover art: $(python3 "$REPO/config/moode/patch-ytm-art.py")" \
      || warn "moOde cover-art patch not applied - default covers stay; everything else works"
  fi
fi
if command -v moodeutl >/dev/null 2>&1 && \
   [[ "$(moodeutl -q "select value from cfg_system where param='spotifysvc'" 2>/dev/null)" == 1 ]]; then
  warn "moOde's Spotify Connect renderer is ON - turn it off (Configure -> Renderers); go-librespot replaces it"
fi
if command -v mpd >/dev/null 2>&1; then
  MPDV="$(mpd --version 2>/dev/null)" || true
  grep -qw curl   <<<"$MPDV" || warn "mpd has no curl input plugin - ytm: presets will not play"
  grep -qw ffmpeg <<<"$MPDV" || warn "mpd has no ffmpeg decoder - ytm: presets will not play"
fi

# --- yt-dlp, off root ------------------------------------------------------------------
# /opt/yt-dlp belongs to the ytdlp user; two venv slots and a `current` link
# (scripts/hasler-ytdlp-update). The weekly unit runs as ytdlp, sandboxed to
# /opt/yt-dlp; haslerd ($DUSER) may start it via polkit. A box that had the
# old root venv directly in /opt/yt-dlp keeps it until the first new build works.
say "yt-dlp: user ytdlp, /opt/yt-dlp, weekly checked upgrade"
if [[ ! -d /opt/yt-dlp ]]; then
  run install -d -o ytdlp -g ytdlp -m 0755 /opt/yt-dlp
elif [[ "$(stat -c %U /opt/yt-dlp 2>/dev/null)" != ytdlp ]]; then
  run chown ytdlp:ytdlp /opt/yt-dlp; run chmod 0755 /opt/yt-dlp
fi
put "$REPO/scripts/hasler-ytdlp-update" /usr/local/sbin/hasler-ytdlp-update 0755 || true
UNITS_CHANGED=0
for u in yt-dlp-update.service yt-dlp-update.timer; do
  put "$REPO/config/$u" "/etc/systemd/system/$u" 0644 && UNITS_CHANGED=1
done
if [[ -d /etc/polkit-1/rules.d ]] || (( DRY )); then
  put "$REPO/config/update/51-hasler-ytdlp.rules" /etc/polkit-1/rules.d/51-hasler-ytdlp.rules 0644 || true
else
  warn "no /etc/polkit-1/rules.d (polkitd missing?) - haslerd cannot start the yt-dlp upgrade"
fi
if (( SYSTEMD && ! DRY )); then
  (( UNITS_CHANGED )) && systemctl daemon-reload
  systemctl enable --quiet --now yt-dlp-update.timer
  if [[ ! -L /opt/yt-dlp/current ]]; then
    say "yt-dlp: first build as ytdlp (needs the network; a minute or two)"
    systemctl start yt-dlp-update.service \
      || warn "first yt-dlp build failed (journalctl -u yt-dlp-update) - the old yt-dlp stays; re-run later"
  fi
fi
if [[ -L /opt/yt-dlp/current ]]; then
  if [[ "$(readlink /usr/local/bin/yt-dlp 2>/dev/null)" != /opt/yt-dlp/current/bin/yt-dlp ]]; then
    run ln -sfn /opt/yt-dlp/current/bin/yt-dlp /usr/local/bin/yt-dlp
  fi
  if [[ -f /opt/yt-dlp/pyvenv.cfg ]]; then
    say "yt-dlp: removing the old root-owned venv from /opt/yt-dlp"
    for p in bin include lib lib64 share pyvenv.cfg; do
      [[ -e "/opt/yt-dlp/$p" || -L "/opt/yt-dlp/$p" ]] && run rm -rf "/opt/yt-dlp/$p"
    done
  fi
fi

# --- Spotify login hints -------------------------------------------------------------------
GLR_DIR=/var/lib/go-librespot
[[ -d "$GLR_DIR" ]] || GLR_DIR="$DHOME/.config/go-librespot"
GLR_STATUS="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:3678/status 2>/dev/null)" || true
if [[ "$GLR_STATUS" != 200 ]] && \
   ! grep -qs '"username":"[^"]' "$GLR_DIR/state.json" "$GLR_DIR/credentials.json"; then
  echo
  say "Spotify is not logged in yet. Once, from your laptop (Premium account):"
  echo "     ssh -L 36842:127.0.0.1:36842 $DUSER@$(hostname -s 2>/dev/null || echo hasler).local"
  echo "     journalctl -u go-librespot -n 50 | grep -o 'https://accounts.spotify.com[^ \"]*'"
  echo "   Open that link in the laptop's browser and log in; credentials land in $GLR_DIR/state.json."
  echo "   Check: curl -s -o /dev/null -w '%{http_code}\\n' 127.0.0.1:3678/status   -> 200"
fi

say "box setup done"
if (( REBOOT_NEEDED )); then
  warn "I2C was just enabled - reboot for it to take effect:  sudo reboot"
fi
