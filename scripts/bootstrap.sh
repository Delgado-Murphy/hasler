#!/usr/bin/env bash
# One-time box setup for a Hasler: everything that is not part of a release.
# Run as root from a checkout on the box (scripts/install.sh calls it for the
# legacy layout; on an OTA box run it before scripts/ota-bootstrap.sh).
#
#   sudo scripts/bootstrap.sh [--user pi] [--dry-run] [--eeprom] [--early-mpd|--no-early-mpd]
#
#   --eeprom        also set the bootloader EEPROM (BOOT_ORDER=0xf41,
#                   DISABLE_HDMI=1) if it differs; firmware, so opt-in; applies
#                   at the next boot
#   --early-mpd     start MPD in the boot transaction (a drop-in + a wants link)
#                   instead of from moOde's worker; --no-early-mpd undoes it.
#                   Neither: left as it is. docs/build-guide.md "Boot time".
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
#   - boot time: unused services off (cloud-init, winbind, e2scrub, triggerhappy,
#     ModemManager), rpi-eeprom-update masked, a config.txt block (no boot
#     delay/splash, turbo, no camera/display probing), quiet kernel cmdline
#     without the serial console, Wi-Fi power save off, rc-local not waiting
#     for network-online, /usr/local/sbin/hasler-boot-report
#   - Spotify (go-librespot) login hints
# What it does not do: install haslerd itself (install.sh for the legacy rsync
# layout, the updater for /opt/hasler releases) or anything in a release
# (units, sudoers, udev, config.txt block, helpers - integration/).
set -euo pipefail

DRY=0
DUSER=pi
EEPROM=0
EARLY_MPD=-1     # -1 leave as is, 1 on, 0 off
while (( $# )); do
  case "$1" in
    --dry-run) DRY=1 ;;
    --user) DUSER="${2:?--user NAME}"; shift ;;
    --eeprom) EEPROM=1 ;;
    --early-mpd) EARLY_MPD=1 ;;
    --no-early-mpd) EARLY_MPD=0 ;;
    -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
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
# AirPlay: moOde 10.3.6 on trixie ships no shairport-sync (neither its repo nor
# nqptp for AirPlay 2), so Debian's classic build. moOde starts it itself
# (renderer.php), so Debian's own unit stays off; our conf adds moOde's hooks.
if [[ -f /var/www/inc/renderer.php ]] && ! command -v shairport-sync >/dev/null 2>&1; then
  say "AirPlay: installing shairport-sync (classic AirPlay; moOde starts it)"
  run apt-get install -y -q --no-install-recommends shairport-sync \
    && run systemctl disable --now shairport-sync \
    && run install -m 0644 "$REPO/config/moode/shairport-sync.conf" /etc/shairport-sync.conf \
    || warn "shairport-sync not installed - AirPlay stays unavailable"
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

# --- boot time -------------------------------------------------------------------------
# docs/build-guide.md "Boot time": power on -> first sound. Off: what a radio
# never uses. Kept on purpose: avahi (moOde's hasler.local), bluetooth/hciuart,
# log2ram, smbd (moOde disables and starts it itself). moOde's worker waits for
# `systemctl is-system-running` = running before it starts MPD, so every unit
# that holds the boot open delays the first sound.
FW=/boot/firmware
BOOT_REBOOT=0
BOOT_UNITS_CHANGED=0

# put_text DEST MODE [OWNER:GROUP] < content - put() for generated text
put_text() {
  local tmp rc=0
  tmp="$(mktemp)"; cat > "$tmp"
  put "$tmp" "$@" || rc=$?
  rm -f "$tmp"; return "$rc"
}
unit_exists() { [[ -n "$(systemctl list-unit-files --no-legend "$1" 2>/dev/null)" ]]; }
unit_off() {      # UNIT... - disable (and stop) the installed, enabled ones
  local u st
  for u in "$@"; do
    unit_exists "$u" || continue
    st="$(systemctl is-enabled "$u" 2>/dev/null)" || true
    [[ "$st" == enabled* ]] || continue
    say "boot time: disabling $u"
    run systemctl disable --quiet --now "$u"
  done
}
unit_mask() {     # UNIT - mask if installed (a masked unit cannot be pulled in at all)
  unit_exists "$1" || return 0
  [[ "$(systemctl is-enabled "$1" 2>/dev/null)" == masked ]] && return 0
  say "boot time: masking $1"
  run systemctl mask --quiet "$1"
}

if (( SYSTEMD )); then
  # winbind: Samba's AD/domain helper (no domain here). e2scrub: ext4 metadata
  # checks via LVM snapshots (no LVM here). ModemManager: no modem.
  unit_off winbind.service e2scrub_reap.service e2scrub_all.timer ModemManager.service
  # triggerhappy is moOde's USB volume knob; moOde enables it when that is on.
  if [[ "$(moodeutl -q "select value from cfg_system where param='usb_volknob'" 2>/dev/null)" == 1 ]]; then
    warn "moOde's USB volume knob is on - triggerhappy left enabled"
  else
    unit_off triggerhappy.socket triggerhappy.service
  fi
  # EEPROM updates come deliberately through a release, never on their own at boot.
  unit_mask rpi-eeprom-update.service
fi

# cloud-init: done after the first boot (user, hostname, Wi-Fi), but it costs
# ~4 s every boot and sits in front of basic.target. Disable it only when every
# Wi-Fi profile is NetworkManager's own file (moOde's cfgNetworks writes them
# there): a profile generated from cloud-init/netplan under /run would be lost.
if [[ -d /etc/cloud && ! -e /etc/cloud/cloud-init.disabled ]]; then
  WIFI_N=0; WIFI_ELSEWHERE=""
  if command -v nmcli >/dev/null 2>&1; then
    while IFS= read -r line; do
      [[ "${line%%:*}" == 802-11-wireless ]] || continue
      f="${line#*:}"; f="${f//\\:/:}"
      WIFI_N=$((WIFI_N + 1))
      [[ "$f" == /etc/NetworkManager/system-connections/* ]] || WIFI_ELSEWHERE+=" ${f:-(no file)}"
    done < <(nmcli -t -f TYPE,FILENAME connection show 2>/dev/null)
  fi
  if (( WIFI_N == 0 )); then
    warn "cloud-init left on: no Wi-Fi profile found to check (nmcli)"
  elif [[ -n "$WIFI_ELSEWHERE" ]]; then
    warn "cloud-init left on: Wi-Fi profile(s) not in /etc/NetworkManager/system-connections:$WIFI_ELSEWHERE"
    warn "  re-save the network in moOde (Configure -> Network) and re-run"
  else
    say "boot time: disabling cloud-init ($WIFI_N Wi-Fi profile(s), all NetworkManager's own)"
    run touch /etc/cloud/cloud-init.disabled
  fi
fi

# config.txt: our own marked block, below everything moOde manages. moOde's
# worker checks only that line 2 is its main header and that its four section
# headers exist (chkBootConfigTxt), and its audio-overlay edit rewrites the line
# after "# Audio overlays" - so the block goes at the end, after a blank line,
# before the updater's lamp block (hasler-update strips and re-appends its own
# block at the very end; ours stays put). Last value wins in config.txt.
if [[ -f "$FW/config.txt" ]]; then
  RES="$(python3 -I - "$FW/config.txt" "$DRY" <<'EOF'
import os, sys
cfg, dry = sys.argv[1], sys.argv[2] == "1"
BEGIN = "# >>> hasler boot time (scripts/bootstrap.sh; edits here are replaced) >>>"
END = "# <<< hasler boot time <<<"
BODY = ["# docs/build-guide.md \"Boot time\"", "[all]", "boot_delay=0", "disable_splash=1",
        "initial_turbo=30", "camera_auto_detect=0", "display_auto_detect=0"]
OTHERS = ("# >>> hasler (managed by hasler-update; edits here are replaced) >>>",
          "# >>> hasler pilot lamp (scripts/install.sh; edits here are replaced) >>>")
text = open(cfg, encoding="utf-8").read()
out, inside = [], False
for line in text.splitlines():
    if line == BEGIN:
        inside = True
    elif inside:
        inside = line != END
    else:
        out.append(line)
if inside:
    print("left alone: our block has no end marker"); sys.exit(0)
at = next((i for i, l in enumerate(out) if l in OTHERS), None)
if at is None or at < 2:
    at = len(out)
head, tail = out[:at], out[at:]
while head and not head[-1].strip():
    head.pop()
# No blank line after END: hasler-update renders "rest + its block" the same way.
new = "\n".join(head + ["", BEGIN] + BODY + [END] + tail) + "\n"
if new == text:
    print("ok"); sys.exit(0)
old = text.splitlines()
same = BEGIN in old and old[old.index(BEGIN) + 1:old.index(BEGIN) + 2 + len(BODY)] == BODY + [END]
if dry:
    print("would tidy the blank lines around the boot-time block" if same else "would write the boot-time block"); sys.exit(0)
open(cfg + ".hasler-boot-bak", "w", encoding="utf-8").write(text)
with open(cfg + ".tmp", "w", encoding="utf-8") as f:
    f.write(new); f.flush(); os.fsync(f.fileno())
os.replace(cfg + ".tmp", cfg)
print("tidied" if same else "updated (backup config.txt.hasler-boot-bak)")
EOF
)" || RES="could not edit $FW/config.txt"
  say "boot time: config.txt $RES"
  [[ "$RES" == updated* ]] && BOOT_REBOOT=1
fi

# Kernel cmdline: quiet, loglevel=3 (a console full of messages costs time on
# the UART/HDMI), and no serial console - nothing here (moOde included) uses
# it, and it also starts a serial-getty.
if [[ -f "$FW/cmdline.txt" ]]; then
  RES="$(python3 -I - "$FW/cmdline.txt" "$DRY" <<'EOF'
import os, sys
path, dry = sys.argv[1], sys.argv[2] == "1"
text = open(path, encoding="utf-8").read()
toks = [t for t in text.split() if t != "console=serial0,115200"]
if "quiet" not in toks:
    toks.append("quiet")
toks = [t for t in toks if not t.startswith("loglevel=")] + ["loglevel=3"]
new = " ".join(toks) + "\n"
if sorted(new.split()) == sorted(text.split()):
    print("ok"); sys.exit(0)
if dry:
    print("would become: " + new.strip()); sys.exit(0)
open(path + ".hasler-boot-bak", "w", encoding="utf-8").write(text)
with open(path + ".tmp", "w", encoding="utf-8") as f:
    f.write(new); f.flush(); os.fsync(f.fileno())
os.replace(path + ".tmp", path)
print("updated (backup cmdline.txt.hasler-boot-bak)")
EOF
)" || RES="could not edit $FW/cmdline.txt"
  say "boot time: cmdline.txt $RES"
  [[ "$RES" == updated* ]] && BOOT_REBOOT=1
fi

# /boot/firmware with the vfat `flush` option: write back on close instead of
# whenever the page cache gets to it. moOde's worker rewrites config.txt with
# `sed -i` at every boot; without flush a power cut seconds later can leave it
# empty or truncated (the worker then restores its default: no amp, no lamp).
# Shrinks the window; hasler-bootcfg-guard repairs what still slips through.
if [[ -f /etc/fstab ]]; then
  RES="$(python3 -I - /etc/fstab "$DRY" <<'EOF'
import os, sys
path, dry = sys.argv[1], sys.argv[2] == "1"
text = open(path, encoding="utf-8").read()
out, hit = [], None
for line in text.splitlines():
    f = line.split()
    if len(f) >= 4 and not line.lstrip().startswith("#") and f[1] in ("/boot/firmware", "/boot") and f[2] == "vfat":
        if "flush" in f[3].split(","):
            hit = hit or "ok"
        else:
            hit = "add"
            f[3] += ",flush"
            line = "\t".join(f)
    out.append(line)
if hit is None:
    print("no vfat /boot/firmware entry - left alone"); sys.exit(0)
if hit == "ok":
    print("ok"); sys.exit(0)
if dry:
    print("would add flush to the /boot/firmware options"); sys.exit(0)
open(path + ".hasler-boot-bak", "w", encoding="utf-8").write(text)
st = os.stat(path)
with open(path + ".tmp", "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n"); fh.flush(); os.fsync(fh.fileno())
os.chmod(path + ".tmp", st.st_mode & 0o7777)
os.replace(path + ".tmp", path)
print("updated (backup fstab.hasler-boot-bak)")
EOF
)" || RES="could not edit /etc/fstab"
  say "boot time: fstab /boot/firmware flush: $RES"
  if [[ "$RES" == updated* ]] && (( SYSTEMD )); then
    run systemctl daemon-reload
    # vfat accepts flush on a remount; if not, it applies from the next boot.
    run mount -o remount,flush "$FW" 2>/dev/null || warn "remount of $FW with flush failed - applies at the next boot"
  fi
fi

# Boot config guard (a release helper, docs/build-guide.md "Boot config guard"):
# config.txt / cmdline.txt were just edited on purpose, so they are the new
# known-good copies (saved only if sane). Not installed yet: its timer saves
# them after the first boot with a release.
if [[ -x /usr/local/sbin/hasler-bootcfg-guard ]] && (( ! DRY )); then
  /usr/bin/python3 -I /usr/local/sbin/hasler-bootcfg-guard save --force \
    || warn "hasler-bootcfg-guard: config.txt not sane - known-good copy not refreshed"
fi

# Wi-Fi power save off for every profile (moOde writes powersave=2 into its own;
# this covers any other). Takes effect at the next connection - no Wi-Fi bounce.
if [[ -d /etc/NetworkManager ]]; then
  put_text /etc/NetworkManager/conf.d/hasler-wifi-powersave.conf 0644 <<'EOF' || true
# scripts/bootstrap.sh (docs/build-guide.md "Boot time"): Wi-Fi power save off.
[connection]
wifi.powersave = 2
EOF
fi

# rc-local: Debian's drop-in makes it wait for network-online (~6 s of
# NetworkManager-wait-online). rc.local only launches moOde's worker, which
# waits for the IP address itself (checkForIpAddr, then the hotspot) - so an
# empty override of that drop-in is safe, unless rc.local does network things.
RCL_DEB=/usr/lib/systemd/system/rc-local.service.d/debian.conf
[[ -f "$RCL_DEB" ]] || RCL_DEB=/lib/systemd/system/rc-local.service.d/debian.conf
if (( SYSTEMD )) && [[ -f "$RCL_DEB" ]] && grep -q network-online "$RCL_DEB"; then
  RCL_NET="$(grep -vE '^[[:space:]]*(#|$)' /etc/rc.local 2>/dev/null \
    | grep -Ew 'mount|curl|wget|ntpdate|chronyc|ping|nc|ssh|rsync|git|apt|apt-get|nmcli|dhclient|ifup|ip' )" || true
  if [[ -n "$RCL_NET" ]]; then
    warn "rc-local still waits for network-online: /etc/rc.local does network things:"
    printf '     %s\n' "$RCL_NET"
  else
    put_text /etc/systemd/system/rc-local.service.d/debian.conf 0644 <<'EOF' && BOOT_UNITS_CHANGED=1 || true
# scripts/bootstrap.sh (docs/build-guide.md "Boot time"): this file, empty of
# settings, replaces Debian's drop-in of the same name, so rc-local (which
# starts moOde's worker) no longer waits for network-online. The worker waits
# for an IP address itself. Remove this file to restore Debian's behaviour.
EOF
  fi
fi

# Early MPD (opt-in, for A/B on the bench): MPD starts as soon as the amp's
# ALSA card exists instead of after the worker's "Linux startup: complete".
# The wants link is made with add-wants, not `enable`: Debian's [Install] has
# Also=mpd.socket, and socket activation would change how MPD listens.
MPD_DROPIN=/etc/systemd/system/mpd.service.d/hasler-early.conf
MPD_WANTS=/etc/systemd/system/multi-user.target.wants/mpd.service
if (( SYSTEMD )) && (( EARLY_MPD == 1 )); then
  CARD_ID="$(grep -i hifiberry /proc/asound/cards 2>/dev/null | head -1 | sed 's/.*\[\([^] ]*\).*/\1/')" || true
  [[ -n "$CARD_ID" ]] || { CARD_ID=sndrpihifiberry; warn "no HiFiBerry card in /proc/asound/cards - early MPD waits for '$CARD_ID'"; }
  MPD_EARLY_CONF="$(sed "s/@CARD@/$CARD_ID/g" <<'EOF'
# scripts/bootstrap.sh --early-mpd (docs/build-guide.md "Boot time").
# moOde ships mpd.service disabled and its worker starts it once the whole boot
# is done; with this drop-in and a multi-user.target wants link MPD starts in
# the boot transaction, after the amp's card exists (up to 10 s, then anyway).
# The worker's own `systemctl start mpd` is then a no-op.
# Undo: sudo scripts/bootstrap.sh --no-early-mpd
[Unit]
After=sound.target

[Service]
ExecStartPre=/bin/sh -c 'i=0; while [ ! -e /proc/asound/@CARD@ ] && [ $$i -lt 100 ]; do sleep 0.1; i=$$((i + 1)); done; exit 0'
EOF
)"
  if put_text "$MPD_DROPIN" 0644 <<<"$MPD_EARLY_CONF"; then BOOT_UNITS_CHANGED=1; fi
  if [[ ! -L "$MPD_WANTS" ]]; then
    say "boot time: MPD starts early (wants link)"
    run systemctl add-wants multi-user.target mpd.service
  fi
  [[ "$(systemctl is-enabled mpd.socket 2>/dev/null)" == enabled ]] && \
    warn "mpd.socket is enabled - moOde expects it off: sudo systemctl disable --now mpd.socket"
elif (( SYSTEMD )) && (( EARLY_MPD == 0 )); then
  if [[ -e "$MPD_DROPIN" || -L "$MPD_WANTS" ]]; then
    say "boot time: MPD back to moOde's worker starting it"
    run rm -f "$MPD_DROPIN" "$MPD_WANTS"
    BOOT_UNITS_CHANGED=1
  fi
fi
if (( SYSTEMD && BOOT_UNITS_CHANGED )); then run systemctl daemon-reload; fi

# Bootloader EEPROM (opt-in --eeprom): SD first, then USB, then retry
# (BOOT_ORDER=0xf41 - no network/NVMe probing), and no HDMI diagnostics screen
# (DISABLE_HDMI=1 - the bootloader's only; Linux HDMI is unaffected).
# rpi-eeprom-config --apply stages it; it is written at the next reboot.
if (( EEPROM )); then
  if ! command -v rpi-eeprom-config >/dev/null 2>&1; then
    warn "--eeprom: rpi-eeprom-config not found"
  elif ! EECUR="$(rpi-eeprom-config 2>/dev/null)" || [[ -z "$EECUR" ]]; then
    warn "--eeprom: could not read the bootloader config (run as root)"
  else
    EENEW="$(python3 -I -c '
import sys
want = {"BOOT_ORDER": "0xf41", "DISABLE_HDMI": "1"}
lines = sys.stdin.read().splitlines()
out, seen = [], set()
for l in lines:
    k = l.split("=", 1)[0].strip()
    if "=" in l and k in want and not l.lstrip().startswith("#"):
        out.append(k + "=" + want[k]); seen.add(k)
    else:
        out.append(l)
missing = [k + "=" + v for k, v in want.items() if k not in seen]
at = out.index("[all]") + 1 if "[all]" in out else 0
out[at:at] = missing
print("\n".join(out))
' <<<"$EECUR")"
    if [[ "$EENEW" == "$EECUR" ]]; then
      say "boot time: EEPROM ok (BOOT_ORDER=0xf41, DISABLE_HDMI=1)"
    elif (( DRY )); then
      echo "   would apply to the bootloader EEPROM (at the next boot):"
      diff <(echo "$EECUR") <(echo "$EENEW") | sed 's/^/     /' || true
    else
      EETMP="$(mktemp)"; printf '%s\n' "$EENEW" > "$EETMP"
      say "boot time: EEPROM: staging BOOT_ORDER=0xf41 DISABLE_HDMI=1 (written at the next boot)"
      if rpi-eeprom-config --apply "$EETMP"; then BOOT_REBOOT=1; else warn "rpi-eeprom-config --apply failed - EEPROM unchanged"; fi
      rm -f "$EETMP"
    fi
  fi
fi

put "$REPO/scripts/hasler-boot-report" /usr/local/sbin/hasler-boot-report 0755 || true

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
if (( BOOT_REBOOT )); then
  warn "boot-time changes (config.txt / cmdline.txt / EEPROM) take effect at the next boot:  sudo reboot"
fi
