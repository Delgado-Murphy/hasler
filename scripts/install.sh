#!/usr/bin/env bash
# Install haslerd on the Pi. Run from the repo root, on the box itself.
#
#   scripts/install.sh              # install or update
#   scripts/install.sh --dry-run    # show what it would do
#
# Idempotent: safe to re-run after every git pull.
#
# Installs: haslerd (venv + unit), go-librespot (pinned binary + config + unit),
# yt-dlp (own venv, weekly upgrade timer), deno (yt-dlp's JS runtime, pinned),
# /var/lib/haslerd, and a sudoers drop-in for the panel's clean shutdown.
set -euo pipefail

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

# --- pins --------------------------------------------------------------------
# Bump deliberately. SHA-256 is GitHub's own asset digest for the arm64 build.
GLR_VERSION=v0.10.3
GLR_SHA256=fdab3c06f3a776a3f696afb26aa59fbdea63557e24d3c39f2df2830ab1851a42
DENO_VERSION=v2.9.7
DENO_SHA256=c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf
# yt-dlp is deliberately NOT pinned: YouTube breaks old versions within weeks.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/daemon/.venv"
UNIT_DIR=/etc/systemd/system
USER_NAME="${SUDO_USER:-$USER}"
USER_HOME="$(getent passwd "$USER_NAME" 2>/dev/null | cut -d: -f6)" || true
[[ -n "$USER_HOME" ]] || USER_HOME="$(eval echo "~$USER_NAME")"
GROUP_NAME="$(id -gn "$USER_NAME")"
GLR_CONF_DIR="$USER_HOME/.config/go-librespot"
YTDLP_VENV=/opt/yt-dlp
STATE_DIR=/var/lib/haslerd
STAMPS=/usr/local/share/hasler     # records which pinned versions are installed

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }
run()  { if (( DRY )); then printf '   would run: %s\n' "$*"; else eval "$@"; fi; }

# Binaries below are linux/arm64 only. Anywhere else: say so and skip them.
ARM64=0; [[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] && ARM64=1
SYSTEMD=0; command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] && SYSTEMD=1

say "repo:  $REPO"
say "user:  $USER_NAME ($USER_HOME)"

# --- sanity ------------------------------------------------------------------
if ! grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null; then
  warn "this does not look like a Raspberry Pi - continuing anyway"
fi
(( ARM64 ))   || warn "not linux/aarch64 - go-librespot, deno and yt-dlp will be skipped"
(( SYSTEMD )) || warn "no systemd - units will be shown but not installed"
if ! command -v moodeutl >/dev/null 2>&1; then
  warn "moodeutl not found - is this a moOde image? the daemon will run but"
  warn "renderer detection will fall back to 'radio' always"
fi

# Download $1 to $3 and check it against SHA-256 $2. Refuses to keep a mismatch.
fetch() {
  curl -fsSL --retry 3 -o "$3" "$1"
  if ! echo "$2  $3" | sha256sum -c --quiet - >/dev/null 2>&1; then
    rm -f "$3"; warn "checksum mismatch: $1"; return 1
  fi
}

# Render a unit from config/ (paths and User= for this box) into /etc/systemd.
put_unit() {
  local src="$REPO/config/$1" dest="$UNIT_DIR/$1" tmp
  tmp="$(mktemp)"
  sed -e "s#/home/pi/hasler#$REPO#g" -e "s#/home/pi/#$USER_HOME/#g" \
      -e "s#^User=pi#User=$USER_NAME#" "$src" > "$tmp"
  if (( DRY )); then
    printf '   would write %s:\n' "$dest"; sed 's/^/     /' "$tmp"
  elif (( SYSTEMD )); then
    sudo install -m 0644 "$tmp" "$dest"
  fi
  rm -f "$tmp"
}

# --- I2C ---------------------------------------------------------------------
if [[ ! -e /dev/i2c-1 ]]; then
  warn "/dev/i2c-1 missing. Enable I2C first:"
  warn "  sudo raspi-config nonint do_i2c 0 && sudo reboot"
else
  say "I2C present. Devices seen:"
  command -v i2cdetect >/dev/null && i2cdetect -y 1 2>/dev/null | tail -n +2 || true
  say "expect 0x77 (power controller); 0x20 / 0x48 once the expander and ADC are fitted"
fi

# --- python ------------------------------------------------------------------
if [[ ! -d "$VENV" ]]; then
  say "creating venv"
  if command -v uv >/dev/null 2>&1; then
    run "uv venv --python 3.11 '$VENV'"
  else
    run "python3 -m venv '$VENV'"
  fi
fi

say "installing haslerd with hardware extras"
if command -v uv >/dev/null 2>&1; then
  run "VIRTUAL_ENV='$VENV' uv pip install -e '$REPO/daemon[pi]'"
else
  run "'$VENV/bin/pip' install --upgrade pip -q"
  run "'$VENV/bin/pip' install -e '$REPO/daemon[pi]'"
fi

# --- config ------------------------------------------------------------------
if [[ ! -f "$REPO/config/haslerd.yaml" ]]; then
  say "seeding config/haslerd.yaml from the example"
  run "cp '$REPO/config/haslerd.yaml.example' '$REPO/config/haslerd.yaml'"
  warn "edit it: set the HARDWARE mixer name from"
  warn "  moodeutl -q \"select value from cfg_system where param='amixname'\""
else
  for key in spotify: ytm: state_file:; do
    grep -q "^$key" "$REPO/config/haslerd.yaml" || \
      warn "config/haslerd.yaml has no '$key' - copy it from haslerd.yaml.example"
  done
fi

# --- state -------------------------------------------------------------------
# systemd also creates this (StateDirectory=haslerd); doing it here means
# state.json and a cookies file can be put in place before the first start.
if [[ ! -d "$STATE_DIR" ]]; then
  say "creating $STATE_DIR"
  run "sudo install -d -o '$USER_NAME' -g '$GROUP_NAME' -m 0755 '$STATE_DIR'"
fi

# --- groups ------------------------------------------------------------------
for group in gpio i2c audio; do
  command -v getent >/dev/null 2>&1 || { warn "no getent - skipping group setup"; break; }
  if getent group "$group" >/dev/null 2>&1 && ! id -nG "$USER_NAME" | grep -qw "$group"; then
    say "adding $USER_NAME to $group"
    run "sudo usermod -aG '$group' '$USER_NAME'"
    warn "log out and back in for the $group group to take effect"
  fi
done

# --- sudo for shutdown -------------------------------------------------------
# The tone knob held off -> haslerd runs `sudo systemctl poweroff`. Allow that
# and nothing else. Both paths because Bookworm is usrmerged.
SUDOERS=/etc/sudoers.d/haslerd
TMP="$(mktemp)"
cat > "$TMP" <<EOF
# haslerd: clean shutdown from the panel. Installed by scripts/install.sh.
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /bin/systemctl poweroff
EOF
if [[ -f "$SUDOERS" ]] && sudo -n cmp -s "$TMP" "$SUDOERS" 2>/dev/null; then
  :
elif (( DRY )); then
  printf '   would write %s (0440, after visudo -cf):\n' "$SUDOERS"; sed 's/^/     /' "$TMP"
elif command -v visudo >/dev/null 2>&1 && sudo visudo -cf "$TMP" >/dev/null; then
  say "installing $SUDOERS"
  sudo install -m 0440 -o root -g root "$TMP" "$SUDOERS"
else
  warn "visudo missing or rejected the drop-in - not installing $SUDOERS"
fi
rm -f "$TMP"

# --- go-librespot ------------------------------------------------------------
# Spotify presets. A Spotify Connect device haslerd drives over 127.0.0.1:3678.
if (( ARM64 )); then
  if [[ "$(cat "$STAMPS/go-librespot.version" 2>/dev/null)" != "$GLR_VERSION" ]]; then
    say "installing go-librespot $GLR_VERSION"
    if (( DRY )); then
      printf '   would download go-librespot_linux_arm64.tar.gz %s -> /usr/local/bin\n' "$GLR_VERSION"
    else
      DL="$(mktemp -d)"
      fetch "https://github.com/devgianlu/go-librespot/releases/download/$GLR_VERSION/go-librespot_linux_arm64.tar.gz" \
            "$GLR_SHA256" "$DL/glr.tgz"
      tar -xzf "$DL/glr.tgz" -C "$DL" go-librespot
      sudo install -m 0755 "$DL/go-librespot" /usr/local/bin/go-librespot
      sudo install -d "$STAMPS"; echo "$GLR_VERSION" | sudo tee "$STAMPS/go-librespot.version" >/dev/null
      rm -rf "$DL"
    fi
  fi
fi

# Point it at the Amp4 Pro by card id, read from the kernel rather than assumed.
CARD_ID="$(grep -i hifiberry /proc/asound/cards 2>/dev/null | head -1 | sed 's/.*\[\([^] ]*\).*/\1/')" || true
if [[ -z "$CARD_ID" ]]; then
  CARD_ID=sndrpihifiberry
  warn "no HiFiBerry card in /proc/asound/cards - go-librespot config assumes '$CARD_ID'"
  warn "  check with: aplay -l"
fi
TMP="$(mktemp)"
sed "s/CARD=sndrpihifiberry/CARD=$CARD_ID/" "$REPO/config/go-librespot/config.yml" > "$TMP"
GLR_CONF="$GLR_CONF_DIR/config.yml"
if [[ -f "$GLR_CONF" ]] && cmp -s "$TMP" "$GLR_CONF"; then
  :
elif [[ -f "$GLR_CONF" ]]; then
  warn "$GLR_CONF differs from the template - keeping yours, writing config.yml.new"
  (( DRY )) && echo "   would write $GLR_CONF.new" || \
    sudo install -o "$USER_NAME" -g "$GROUP_NAME" -m 0644 "$TMP" "$GLR_CONF.new"
else
  say "installing go-librespot config (ALSA card: $CARD_ID)"
  if (( DRY )); then
    echo "   would write $GLR_CONF (dir 0700, owned by $USER_NAME)"
  else
    sudo install -d -o "$USER_NAME" -g "$GROUP_NAME" -m 0700 "$GLR_CONF_DIR"
    sudo install -o "$USER_NAME" -g "$GROUP_NAME" -m 0644 "$TMP" "$GLR_CONF"
  fi
fi
rm -f "$TMP"

# Two Spotify devices on one exclusive ALSA card fight. moOde's must be off.
if command -v moodeutl >/dev/null 2>&1; then
  if [[ "$(moodeutl -q "select value from cfg_system where param='spotifysvc'" 2>/dev/null)" == 1 ]]; then
    warn "moOde's Spotify Connect renderer is ON. Turn it off:"
    warn "  moOde -> Configure -> Renderers -> Spotify Connect -> OFF"
  fi
fi

# --- yt-dlp + deno -----------------------------------------------------------
# YouTube Music presets. yt-dlp in its own venv (fast start, `pip -U` keeps
# yt-dlp-ejs matched), deno as its JS runtime - the one yt-dlp recommends and
# enables by default. Bookworm has no quickjs package at all.
if (( ARM64 )); then
  if ! command -v unzip >/dev/null 2>&1; then
    run "sudo apt-get install -y -q unzip"
  fi
  if [[ "$(deno --version 2>/dev/null | head -1)" != "deno ${DENO_VERSION#v}"* ]]; then
    say "installing deno $DENO_VERSION"
    if (( DRY )); then
      printf '   would download deno-aarch64-unknown-linux-gnu.zip %s -> /usr/local/bin\n' "$DENO_VERSION"
    else
      DL="$(mktemp -d)"
      fetch "https://github.com/denoland/deno/releases/download/$DENO_VERSION/deno-aarch64-unknown-linux-gnu.zip" \
            "$DENO_SHA256" "$DL/deno.zip"
      unzip -q "$DL/deno.zip" -d "$DL"
      sudo install -m 0755 "$DL/deno" /usr/local/bin/deno
      rm -rf "$DL"
    fi
  fi

  if [[ ! -x "$YTDLP_VENV/bin/pip" ]]; then
    say "creating yt-dlp venv at $YTDLP_VENV"
    run "sudo python3 -m venv '$YTDLP_VENV'"
  fi
  say "installing / upgrading yt-dlp"
  run "sudo '$YTDLP_VENV/bin/pip' install --upgrade --quiet 'yt-dlp[default]'"
  run "sudo ln -sfn '$YTDLP_VENV/bin/yt-dlp' /usr/local/bin/yt-dlp"
fi

# MPD plays what the ytm proxy redirects to: AAC-in-m4a or Opus-in-webm over
# HTTPS. That needs the curl input plugin (follows redirects) and ffmpeg.
if command -v mpd >/dev/null 2>&1; then
  MPDV="$(mpd --version 2>/dev/null)" || true
  grep -qw curl   <<<"$MPDV" || warn "mpd has no curl input plugin - ytm: presets will not play"
  grep -qw ffmpeg <<<"$MPDV" || warn "mpd has no ffmpeg decoder - ytm: presets will not play"
fi

# --- services ----------------------------------------------------------------
say "installing systemd units"
for unit in haslerd.service go-librespot.service yt-dlp-update.service yt-dlp-update.timer; do
  put_unit "$unit"
done
if (( ! DRY && SYSTEMD )); then
  sudo systemctl daemon-reload
  if (( ARM64 )); then
    sudo systemctl enable --quiet go-librespot yt-dlp-update.timer
    sudo systemctl restart go-librespot
    sudo systemctl start yt-dlp-update.timer
  fi
  sudo systemctl enable --quiet haslerd
  sudo systemctl restart haslerd
fi

say "done"
if (( ! DRY && SYSTEMD )); then
  echo
  systemctl --no-pager --lines=15 status haslerd || true
  echo
  say "logs:  journalctl -u haslerd -f"
  say "probe: scripts/panel-probe.py"
fi

# --- one-time Spotify login --------------------------------------------------
# /status: 200 + JSON once logged in, 204 before. Fall back to the state file
# when the service is not up (yet).
GLR_STATUS="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:3678/status 2>/dev/null)" || true
if [[ "$GLR_STATUS" != 200 ]] && \
   ! grep -qs '"username":"[^"]' "$GLR_CONF_DIR/state.json" "$GLR_CONF_DIR/credentials.json"; then
  echo
  say "Spotify is not logged in yet. Once, from your laptop (Premium account):"
  echo "     ssh -L 36842:127.0.0.1:36842 $USER_NAME@$(hostname -s 2>/dev/null || echo hasler).local"
  echo "     journalctl -u go-librespot -n 50 | grep -o 'https://accounts.spotify.com[^ \"]*'"
  echo "   Open that link in the laptop's browser and log in. The redirect comes back"
  echo "   through the tunnel; credentials land in $GLR_CONF_DIR/state.json."
  echo "   No tunnel? Copy the failed 127.0.0.1:36842 URL from the browser and curl it on the Pi."
  echo "   Check: curl -s -o /dev/null -w '%{http_code}\\n' 127.0.0.1:3678/status   -> 200"
fi
