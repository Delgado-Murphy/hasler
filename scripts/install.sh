#!/usr/bin/env bash
# Install haslerd on the Pi from this checkout - the LEGACY rsync layout
# (code runs from the checkout, e.g. /home/pi/hasler). Run from the repo root,
# on the box itself.
#
#   scripts/install.sh              # install or update
#   scripts/install.sh --dry-run    # show what it would do
#
# Idempotent: safe to re-run after every rsync / git pull. A box that runs
# signed releases from /opt/hasler (docs/ota.md) is updated by hasler-update
# instead; this script refuses to touch it. The bench box moves over with
# scripts/ota-bootstrap.sh + migration 001 (docs/release.md "Bench box").
#
# First scripts/bootstrap.sh (one-time box setup: apt packages, I2C, groups,
# system users, CamillaDSP tone config, captive portal, moOde patch, yt-dlp as
# its own user with a checked weekly upgrade), then the legacy layout: haslerd
# (venv + unit), go-librespot (pinned binary + config + unit), deno (yt-dlp's
# JS runtime, pinned), a sudoers drop-in (config/sudoers/haslerd), the helpers,
# and the pilot lamp's boot path (config.txt block, udev rule, early breath
# service, halt hook - see docs/lamp.md).
set -euo pipefail

DRY=0
[[ "${1:-}" == "--dry-run" ]] && DRY=1

# --- pins --------------------------------------------------------------------
# Bumped by .github/workflows/deps-bump.yml (weekly PR; patch-level go-librespot
# and same-major deno merge by themselves). SHA-256 of the arm64 asset, hashed by
# the workflow and checked against GitHub's own asset digest.
# (scripts/release/build_release.py reads these too, for the release's bin/.)
GLR_VERSION=v0.10.5
GLR_SHA256=35274c1d3576090096d3459ecf76ee9c025258d19dc76f0bf724f95c525467e1
DENO_VERSION=v2.9.8
DENO_SHA256=d49596c20b9788cf8f5d8ddf644103d4bde56ed239753f0bdb92ba9361d51905
# yt-dlp is deliberately NOT pinned: YouTube breaks old versions within weeks.
# scripts/bootstrap.sh sets it up (user ytdlp, scripts/hasler-ytdlp-update).

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/daemon/.venv"
UNIT_DIR=/etc/systemd/system
USER_NAME="${SUDO_USER:-$USER}"
USER_HOME="$(getent passwd "$USER_NAME" 2>/dev/null | cut -d: -f6)" || true
[[ -n "$USER_HOME" ]] || USER_HOME="$(eval echo "~$USER_NAME")"
GROUP_NAME="$(id -gn "$USER_NAME")"
GLR_CONF_DIR="$USER_HOME/.config/go-librespot"
STAMPS=/usr/local/share/hasler     # records which pinned versions are installed

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }
run()  { if (( DRY )); then printf '   would run: %s\n' "$*"; else eval "$@"; fi; }

# Binaries below are linux/arm64 only. Anywhere else: say so and skip them.
ARM64=0; [[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] && ARM64=1
SYSTEMD=0; command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]] && SYSTEMD=1

if [[ -L /opt/hasler/current ]]; then
  warn "this box runs signed releases from /opt/hasler ($(readlink /opt/hasler/current))."
  warn "install.sh is for the old rsync layout and would fight the updater - not touching it."
  warn "update with: hasler-update status / the key page; box setup only: sudo scripts/bootstrap.sh"
  exit 1
fi

say "repo:  $REPO"
say "user:  $USER_NAME ($USER_HOME)"

# --- sanity ------------------------------------------------------------------
(( ARM64 ))   || warn "not linux/aarch64 - go-librespot and deno will be skipped"
(( SYSTEMD )) || warn "no systemd - units will be shown but not installed"

# --- one-time box setup --------------------------------------------------------
if [[ "$(uname -s)" == Linux ]]; then
  BOOT_ARGS=(--user "$USER_NAME"); (( DRY )) && BOOT_ARGS+=(--dry-run)
  sudo bash "$REPO/scripts/bootstrap.sh" "${BOOT_ARGS[@]}"
else
  warn "not Linux - skipping scripts/bootstrap.sh"
fi

# Download $1 to $3 and check it against SHA-256 $2. Refuses to keep a mismatch.
fetch() {
  curl -fsSL --retry 3 -o "$3" "$1"
  if ! echo "$2  $3" | sha256sum -c --quiet - >/dev/null 2>&1; then
    rm -f "$3"; warn "checksum mismatch: $1"; return 1
  fi
}

# Render a legacy unit (config/legacy/, else config/) with this box's paths and User=.
put_unit() {
  local src="$REPO/config/legacy/$1" dest="$UNIT_DIR/$1" tmp
  [[ -f "$src" ]] || src="$REPO/config/$1"
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

put_file() {   # put_file SRC DEST MODE - install if different, say whether it changed
  local src="$1" dest="$2" mode="$3"
  if sudo -n cmp -s "$src" "$dest" 2>/dev/null; then return 1; fi
  if (( DRY )); then printf '   would install %s -> %s (%s)\n' "$src" "$dest" "$mode"; return 1; fi
  sudo install -D -m "$mode" -o root -g root "$src" "$dest"
}

# --- I2C ---------------------------------------------------------------------
if [[ -e /dev/i2c-1 ]]; then
  say "I2C present. Devices seen:"
  command -v i2cdetect >/dev/null && i2cdetect -y 1 2>/dev/null | tail -n +2 || true
  say "expect the Amp4 Pro DAC (shown as UU: its driver holds it); 0x20-0x27 / 0x48 once the expander and ADC are fitted"
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
# lgpio has no aarch64 wheel for newer Pythons (trixie ships 3.13), so pip
# builds it - which needs swig and the C library.
if (( ARM64 )) && ! dpkg -s swig liblgpio-dev >/dev/null 2>&1; then
  run "sudo apt-get install -y -q swig liblgpio-dev"
fi
if command -v uv >/dev/null 2>&1; then
  run "VIRTUAL_ENV='$VENV' uv pip install -e '$REPO/daemon[pi]'"
else
  run "'$VENV/bin/pip' install --upgrade pip -q"
  run "'$VENV/bin/pip' install -e '$REPO/daemon[pi]'"
fi

# --- config ------------------------------------------------------------------
# Once /etc/hasler/box.yaml exists (migration 001), haslerd reads that and
# config/haslerd.yaml is only a fallback for the old code.
if [[ -f /etc/hasler/box.yaml ]]; then
  say "config: /etc/hasler/box.yaml (config/haslerd.yaml is no longer read)"
elif [[ ! -f "$REPO/config/haslerd.yaml" ]]; then
  say "seeding config/haslerd.yaml from the example"
  run "cp '$REPO/config/haslerd.yaml.example' '$REPO/config/haslerd.yaml'"
  warn "edit it: set the HARDWARE mixer name from"
  warn "  moodeutl -q \"select value from cfg_system where param='amixname'\""
else
  for key in spotify: ytm: state_file:; do
    grep -Eq "^[[:space:]]*$key" "$REPO/config/haslerd.yaml" || \
      warn "config/haslerd.yaml has no '$key' - copy it from haslerd.yaml.example"
  done
fi

# --- sudo for shutdown, Wi-Fi, mono -------------------------------------------
# config/sudoers/haslerd (the same file every release ships), for this user.
SUDOERS=/etc/sudoers.d/haslerd
TMP="$(mktemp)"
sed "s/^pi ALL=/$USER_NAME ALL=/" "$REPO/config/sudoers/haslerd" > "$TMP"
if [[ -f "$SUDOERS" ]] && sudo -n cmp -s "$TMP" "$SUDOERS" 2>/dev/null; then
  :
elif (( DRY )); then
  printf '   would write %s (0440, after visudo -cf):\n' "$SUDOERS"; sed 's/^/     /' "$TMP"
elif sudo sh -c "command -v visudo" >/dev/null 2>&1 && sudo visudo -cf "$TMP" >/dev/null; then
  say "installing $SUDOERS"
  sudo install -m 0440 -o root -g root "$TMP" "$SUDOERS"
else
  warn "visudo (root PATH) missing or rejected the drop-in - not installing $SUDOERS"
fi
rm -f "$TMP"

# --- helpers -----------------------------------------------------------------
say "helpers: mono switch, tone knob, Wi-Fi join"
put_file "$REPO/scripts/hasler-audio-mono" /usr/local/sbin/hasler-audio-mono 0755 || true
put_file "$REPO/scripts/hasler-dsp" /usr/local/bin/hasler-dsp 0755 || true
put_file "$REPO/scripts/hasler-tone" /usr/local/bin/hasler-tone 0755 || true
put_file "$REPO/scripts/hasler-wifi-join" /usr/local/sbin/hasler-wifi-join 0755 || true

# --- pilot lamp: firmware, kernel PWM, boot breath, halt ----------------------
# docs/lamp.md "Boot and shutdown". The LED (common anode, R17 / G22 / B27,
# LOW = lit) is held dark by the firmware, then driven by the kernel's
# pwm-gpio driver: hasler-lamp-boot breathes amber from ~3-5 s until haslerd
# takes over, and a system-shutdown hook leaves it dark at halt.
#
# config.txt block = config/configtxt/hasler.block (what every release ships),
# kept between the updater's markers so a later switch to releases finds it
# unchanged (an older install.sh block is replaced in place):
#   gpio=17,22,27=op,dh  firmware sets the pins output-high (= dark) right after
#                        reading config.txt, ending the faint glow the reset
#                        pull-downs give a common-anode LED.
#   dtoverlay=pwm-gpio,gpio=N  one software-PWM chip per pin (Raspberry Pi
#                        overlays README, "Name: pwm-gpio"); the overlay
#                        rewrites its reg so it can be loaded more than once.
# The overlay's pinctrl only selects "output, no pull" (the latch keeps the
# firmware's HIGH) and the driver takes the GPIO "as is", so the LED stays dark
# until hasler-lamp-boot sets polarity "inversed" and fades in.
BOOT_CFG=/boot/firmware/config.txt
[[ -f "$BOOT_CFG" ]] || BOOT_CFG=/boot/config.txt
LAMP_BEGIN='# >>> hasler (managed by hasler-update; edits here are replaced) >>>'
LAMP_END='# <<< hasler <<<'
OLD_BEGIN='# >>> hasler pilot lamp (scripts/install.sh; edits here are replaced) >>>'
OLD_END='# <<< hasler pilot lamp <<<'
REBOOT_NEEDED=0
# only the lines that matter: a re-marked block with the same lines needs no reboot
lamp_lines() { awk -v b="$LAMP_BEGIN" -v e="$LAMP_END" -v ob="$OLD_BEGIN" -v oe="$OLD_END" \
    '$0==b||$0==ob{on=1; next} $0==e||$0==oe{on=0} on && !/^[[:space:]]*(#|$)/' "$1"; }
if [[ -f "$BOOT_CFG" ]]; then
  TMP="$(mktemp)"
  # Everything outside our block(s), then the block at the end (it starts with
  # [all], so it never lands inside a [pi5] / [cm4] conditional section).
  awk -v b="$LAMP_BEGIN" -v e="$LAMP_END" -v ob="$OLD_BEGIN" -v oe="$OLD_END" \
      '$0==b||$0==ob{skip=1; next} $0==e||$0==oe{skip=0; next} !skip' "$BOOT_CFG" > "$TMP"
  { printf '%s\n' "$LAMP_BEGIN"; cat "$REPO/config/configtxt/hasler.block"; printf '%s\n' "$LAMP_END"; } >> "$TMP"
  if cmp -s "$TMP" "$BOOT_CFG"; then
    :
  elif (( DRY )); then
    printf '   would update the lamp block in %s:\n' "$BOOT_CFG"
    diff "$BOOT_CFG" "$TMP" | sed 's/^/     /' || true
  else
    say "pilot lamp: updating $BOOT_CFG (backup $BOOT_CFG.hasler-bak)"
    [[ "$(lamp_lines "$BOOT_CFG")" == "$(lamp_lines "$TMP")" ]] || REBOOT_NEEDED=1
    sudo cp -p "$BOOT_CFG" "$BOOT_CFG.hasler-bak"
    sudo cp "$TMP" "$BOOT_CFG"     # FAT: no modes to keep
  fi
  rm -f "$TMP"
  # Anything else on our pins would fight the overlays.
  if awk -v b="$LAMP_BEGIN" -v e="$LAMP_END" '$0==b{skip=1} !skip; $0==e{skip=0}' "$BOOT_CFG" \
       | grep -Eq '^[[:space:]]*(gpio=([0-9,-]*[,=])?(17|22|27)[,=]|dtoverlay=.*[=,](17|22|27)([^0-9]|$))'; then
    warn "$BOOT_CFG mentions GPIO 17/22/27 outside the lamp block - check it"
  fi
  [[ -e "$(dirname "$BOOT_CFG")/overlays/pwm-gpio.dtbo" ]] || \
    warn "no overlays/pwm-gpio.dtbo next to $BOOT_CFG - the lamp needs a newer kernel"
else
  warn "no config.txt found - pilot lamp firmware/overlay lines not added"
fi

say "pilot lamp: boot breath, halt hook, PWM permissions"
put_file "$REPO/scripts/hasler-lamp-boot" /usr/local/sbin/hasler-lamp-boot 0755 || true
put_file "$REPO/scripts/hasler-lamp-halt" /usr/lib/systemd/system-shutdown/hasler-lamp-halt 0755 || true
if put_file "$REPO/config/udev/99-hasler-pwm.rules" /etc/udev/rules.d/99-hasler-pwm.rules 0644; then
  # Re-apply to chips that already exist ("change" also covers exported pwm0/).
  sudo udevadm control --reload
  sudo udevadm trigger --subsystem-match=pwm --action=change || true
fi

say "mDNS: announce the radio's API (_hasler._tcp) for Home Assistant"
# avahi-daemon watches /etc/avahi/services: no reload needed.
put_file "$REPO/config/avahi/hasler.service" /etc/avahi/services/hasler.service 0644 || true

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

# --- deno (yt-dlp's JS runtime) ----------------------------------------------
# yt-dlp itself: scripts/bootstrap.sh (user ytdlp, /opt/yt-dlp). deno is the JS
# runtime yt-dlp recommends and enables by default; trixie has no quickjs.
if (( ARM64 )); then
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
fi

# --- services ----------------------------------------------------------------
say "installing systemd units"
for unit in haslerd.service hasler-lamp-boot.service go-librespot.service; do
  put_unit "$unit"
done
if (( ! DRY && SYSTEMD )); then
  sudo systemctl daemon-reload
  if (( ARM64 )); then
    sudo systemctl enable --quiet go-librespot
    sudo systemctl restart go-librespot
  fi
  sudo systemctl enable --quiet haslerd
  # Boot-only: enabled for the next boot, never started here (haslerd owns
  # the LED once it runs, and its ExecStartPre stops this at every start).
  sudo systemctl enable --quiet hasler-lamp-boot
  # Fails until the panel's expander answers on I2C - keep going.
  sudo systemctl restart haslerd || warn "haslerd did not start - see journalctl -u haslerd"
fi

say "done"
if (( ! DRY && SYSTEMD )); then
  echo
  systemctl --no-pager --lines=15 status haslerd || true
  echo
  say "logs:  journalctl -u haslerd -f"
  say "probe: scripts/panel-probe.py"
fi
# Not in this legacy layout (on purpose): multiroom (snapcast units and
# hasler-multiroom from /opt/hasler/current) and renaming the Spotify device
# with the room name (go-librespot's config is managed by release installs).
echo
warn "note: multiroom and the room name for Spotify Connect need a release install"
warn "  (/opt/hasler/current); this rsync layout does without them."
if (( REBOOT_NEEDED )); then
  echo
  warn "$BOOT_CFG changed (pilot lamp: gpio= and pwm-gpio overlays)."
  warn "  reboot for it to take effect:  sudo reboot"
fi
