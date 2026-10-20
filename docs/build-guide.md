# Build guide

Work top to bottom. Each phase ends in a check you can actually perform — if it fails, stop there
rather than stacking another layer on top. That is the whole lesson of the two previous attempts:
both half-worked, and neither could be seen.

Two steps in this build can destroy hardware. They are marked **⚠️ DESTRUCTIVE**. Everything else
is recoverable.

---

## Phase 0 — Survey the panel

*Bench work. Multimeter only, nothing powered. Output: a filled-in variant descriptor.*

The keys are already out with a shared ground, so most of this is the wheels and the speaker.

- [ ] **Keys** — confirm each is momentary SPST and springs back. Note if any has extra poles
      (only one is needed).
- [ ] **Key identification** — *skip the multimeter*. On the MCP23017 you wire by the silkscreen
      and check each key lights its own row in Phase 2's `panel-probe`; on direct GPIO,
      `--identify` works the order out for you.
- [ ] **Volume pot** — measure end-to-end resistance. Note the value.
- [ ] **Tone pot** — measure end-to-end resistance, then **continuity-test the end-switch through
      the full rotation**. Confirm it is a plain SPST that opens only at the CCW detent and is
      independent of the resistive track (it should be — two separate wires).
- [ ] **Both pots** — determine taper. Set the knob to the mechanical midpoint and measure
      wiper-to-end: ~50 % of total is **linear**, ~10–20 % is **audio/log**. Expect log.
- [ ] **Clean both pots** with contact cleaner and work them 20+ times. Do this *before* concluding
      anything is faulty — 60-year-old carbon tracks are scratchy, not broken.
- [x] **Speaker** — measured **14.2 Ω DCR → 16 Ω nominal**, direct voice coil, no transformer.
      Drives straight off the Amp4 Pro. Yields ~10 W against a cone good for 1–3 W, so
      **`volume_limit` must be calibrated** (Phase 3a) and the channels **must never be bridged**.
- [ ] **Pilot lamp housing** — measure the aperture so the RGB LED fits the original bezel.
- [ ] Record everything in `docs/variants/hasler-hm.md`.

**⚠️ DESTRUCTIVE — while you are in there:** the original mains transformer and all mains wiring come
out **entirely**, and the old capacitors get discharged before they go in the bin.

**Done when:** every measurement above is written down.

---

## Phase 1a — The Pi on its own, first

*Pi 4B, microSD, and any ordinary 5 V USB-C phone charger. No amp, no PD trigger, no 20 V.*

Prove the boring half works before introducing anything that can destroy hardware. If moOde will not
boot or join WiFi, you want to be finding that out with a £5 charger attached, not with a 20 V rail
and a Power Controller in the path.

- [ ] Flash **moOde, 64-bit** (bench-tested on **10.3.0 / Debian 13 trixie**), SSH enabled, hostname `hasler`. **Pin the version** and note it.
- [ ] Boot the Pi **alone** on a plain 5 V USB-C supply into its own power port.
- [ ] Join WiFi, confirm `ssh hasler.local`, confirm the moOde web UI loads.
- [ ] Enable I²C: `sudo raspi-config nonint do_i2c 0 && sudo reboot`

**Done when:** you can SSH in and reach the web UI. No sound yet — there is no amplifier.

> **⚠️ Never power the Pi from its own USB-C port *and* the Amp4 Pro at the same time.** Once the
> amp is fitted it feeds the Pi from its own 5 V rail. Pick one source, not both.

## Phase 1 — Base image and audio

*Now bring in the amp and the 20 V rail. Goal: the box makes a sound.*

- [ ] Fit the **Amp4 Pro** on the **Pi 4B** (not the Pi 5 — its 5 V rail is rated "up to the Pi4B").
- [ ] Set up the power path — see *Power bring-up* below. **Do not skip the JP1 check.**
- [ ] moOde → Audio config: select the Amp4 Pro, set **Volume type: Hardware**.
- [ ] Note the mixer name — this is what the volume knob will drive:
      ```sh
      moodeutl -q "select value from cfg_system where param='amixname'"
      ```
- [ ] Enable renderers: **Bluetooth, AirPlay, UPnP/DLNA**. Turn *Resume MPD* on.
      **Leave moOde's Spotify Connect OFF** — go-librespot replaces it in Phase 3c, and two Spotify
      devices on one exclusive ALSA card fight.
- [ ] Enable I²C, then confirm the Power Controller answers:
      ```sh
      sudo raspi-config nonint do_i2c 0    # or dtparam=i2c_arm=on in /boot/firmware/config.txt
      i2cdetect -y 1                       # expect 0x77
      ```
- [ ] Add the six stream URLs and put the real values into `presets:` in the descriptor.
- [ ] Commit every config change. This box should be rebuildable from the repo.

**Done when:** `mpc play` produces sound from the original speaker, and each enabled renderer
appears on the network from a phone and plays.

### Power bring-up

Order matters. Do these in sequence, with a meter.

1. [ ] **Configure the HW-398**: bridge the **20 V** pad to the resistor immediately next to it.
       **One pad only** — bridging two parallels the config resistors and the result is undefined.
       Solder generously and tug-test.
2. [ ] **Verify it, unloaded**: plug the trigger into the charger with **nothing on the output** and
       meter Vout. Expect **20.0 V ±5 %**.
3. [ ] **Power-cycle three times, re-metering each time.** This is the real test — it proves the
       setting is sticky and the board does not come up at 5 V. The charger must advertise a 20 V
       PDO; many 30–45 W chargers stop at 15 V and the board silently downshifts.
4. [ ] **Now pick the TVS from the kit** using the rail you just measured: **≤ 20.5 V → a 22**,
       **> 20.5 V → a 24** (SMAJ or SMBJ, either family). Fit it and the **220–470 µF / 35 V** cap
       across the PD board's output.
       **⚠️ If it is a unidirectional `A` part, the banded end goes to the POSITIVE rail** — backwards
       is a dead short across 20 V. A bidirectional `CA` has no orientation to get wrong.
       Then check the TVS stays **stone cold** with 20 V applied — if it is even warm it is
       conducting, so go up a size.
5. [ ] **⚠️ DESTRUCTIVE — verify JP1 on the Power Controller is OFF, with a meter, not by eye.**
       HiFiBerry: *"Connecting a higher voltage with JP1 set will destroy both the power controller
       and the Raspberry Pi!"* JP1 exists only for the 5.1 V-straight-to-Pi case, which is not this.
6. [ ] Wire **PD Vout → Power Controller Vin**, and **Power Controller Vout → Amp4 Pro**. The Pi is
       fed by the Amp4 Pro's own 5 V rail. Keep the PD-to-controller run short.
7. [ ] Power up. If the supply hiccups or retries, that is inrush into the amp's bulk caps — fit the
       NTC limiter.

---

## Phase 2 — Wire the panel and prove it

*The phase both previous attempts skipped.*

- [ ] **MCP23017 board** (the default; CQRobot preferred) — **four wires** to the Amp4 Pro's top
      header: VCC → **3.3 V** pin 1 (never 5 V), GND → pin 6, SDA → pin 3, SCL → pin 5. INTA/INTB
      stay unconnected. Full diagram: Paper sheets H1 (system) and H4 (key board).
- [ ] Green breakout only: meter **RST ↔ VCC** — it must read connected, or the chip sits in reset.
- [ ] `i2cdetect -y 1` → **`27`** for the CQRobot (`20` for the green board — set `addr:` to
      match), plus `77` for the Power Controller.
- [ ] Wire the contacts **by the silkscreen**: keys 1–6 → PA0–PA5, PU → PA6, tone end-switch →
      **PB0**. Common ground to the PA header's GND pin. **Nothing on PA7 or PB7** — output-only on current
      silicon; the daemon refuses them.
- [ ] Fit a **100 nF** across each key.
- [ ] Watch it live:
      ```sh
      scripts/panel-probe.py
      ```
      The WIRE column shows the PA/PB pin each row expects.

> **No board to hand?** Use the direct-GPIO fallback block in `hasler-hm.yaml` instead, wire to any
> eight free GPIOs, and let `scripts/panel-probe.py --identify` work out the `pins:` array.

**Done when — and check all three:**
- every key lights **its own row and nobody else's** (no ghosting);
- the **TRANSITIONS** column stays in low single digits per press. Tens means dirty contacts or a
  missing capacitor — **fix it in hardware here**, not in software later;
- the tone end-switch reads closed with the knob on, open past the detent.

---

## Phase 3a — Keys drive the music

*Needs no parts you do not already have.*

- [ ] `cd daemon && uv venv --python 3.11 && uv pip install -e ".[pi]"`
- [ ] Fill in real stream URLs in `presets:`.
- [ ] `scripts/install.sh` — runs `scripts/bootstrap.sh` (the one-time box setup), then installs
      `haslerd` from the checkout as a systemd service with a watchdog. Re-run it after every
      `git pull`; it is idempotent, and `--dry-run` shows what it would change. This is the
      **legacy rsync layout**: once the box runs signed releases from `/opt/hasler` (see
      [Over-the-air releases](#over-the-air-releases-instead-of-installsh) below) it refuses to
      run and the updater takes over.
- [ ] Map the hold actions to the automations already in the house.

- [ ] **Calibrate `volume_limit`.** Start at 50, play dense music with the knob at maximum, raise in
      steps of 5 until it is as loud as you ever want it, then **stop at the first hint of cone
      distortion and back off two steps.** The speaker is the one irreplaceable part in this build.

**Done when:** each key plays its station; tapping the playing one again stops it; turning the tone
knob past the detent drops to standby and back again resumes; and the knob at maximum is loud but
clean.

## Phase 3b — Wake the wheels (when the ADS1115 lands)

- [ ] Fit the ADS1115 on I²C. `i2cdetect -y 1` → `0x48`.
- [ ] Pots as dividers across 3.3 V, wipers to A0/A1, **100 nF wiper-to-GND**.
- [ ] Switch `io.analog` to `{backend: ads1115, addr: 0x48}`.
- [ ] Run `panel-probe.py` and sweep each pot end to end. Record counts at min, midpoint and max —
      and for tone, **the count at the detent**, since the usable range starts above it.
- [ ] Replace `taper`/`range` in the descriptor with measured `curve` points.

**Done when:** the volume wheel is silent at rest, smooth in motion, and controls Bluetooth, AirPlay
and Spotify as well as radio — that last part is what the hardware mixer buys you.

## Phase 3c — Streaming sources (Spotify + YouTube Music)

*Software only. No phone, no Spotify Web API, no Home Assistant. Spotify needs **Premium**.*

A key can now start a radio station, a **Spotify playlist** or a **YouTube Music playlist**.
Spotify plays through **go-librespot**, a Connect device on the Pi that `haslerd` drives over its
local API (`127.0.0.1:3678`). YouTube Music goes through MPD: `haslerd` queues
`http://127.0.0.1:8099/ytm/<id>`, and each request is redirected to a stream **yt-dlp** resolves on
the spot — so expired stream URLs are never a problem.

- [ ] `scripts/install.sh` — adds go-librespot (pinned binary, config, unit), **deno** as yt-dlp's
      JavaScript runtime, and the sudoers drop-in that lets the tone knob power off cleanly;
      its `bootstrap.sh` step puts yt-dlp in `/opt/yt-dlp` under its own user `ytdlp` (not root)
      with a weekly upgrade that builds the new version in a second venv and switches only if it
      passes a self-check (`scripts/hasler-ytdlp-update`; `--rollback` goes back one).
- [ ] Copy the new `spotify:`, `ytm:` and `state_file:` blocks from `config/haslerd.yaml.example`
      into `config/haslerd.yaml`. The install script warns while any are missing.
- [ ] **Turn moOde's Spotify renderer off**: moOde → Configure → Renderers → Spotify Connect →
      **OFF**. The Amp4 Pro is one exclusive ALSA device; two Spotify players will fight over it.
      ```sh
      moodeutl -q "select value from cfg_system where param='spotifysvc'"   # expect 0
      ```
- [ ] **Log go-librespot in — once.** It is headless, so tunnel the login callback from a laptop:
      ```sh
      ssh -L 36842:127.0.0.1:36842 pi@hasler.local
      journalctl -u go-librespot -n 50 | grep -o 'https://accounts.spotify.com[^ "]*'
      ```
      Open that link in the laptop's browser and log in with the **Premium** account. The redirect
      comes back through the tunnel and credentials land in `~/.config/go-librespot/state.json`.
      No tunnel? Copy the failed `127.0.0.1:36842/login?code=…` URL from the browser and `curl` it
      on the Pi. Prefer not to use a browser at all? Set `credentials: {type: device_auth}` in
      `~/.config/go-librespot/config.yml`, restart it, and enter the logged code at
      `spotify.com/pair`.
      ```sh
      curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:3678/status   # 200 = logged in, 204 = not
      ```
- [ ] **yt-dlp resolves, with a JS runtime.** Without deno YouTube serves fewer formats or none,
      with no clear error. Install the runtime, then prove it:
      ```sh
      deno --version                       # 2.9.x
      yt-dlp --flat-playlist --print id 'https://music.youtube.com/playlist?list=<id>' | head -3
      yt-dlp -f bestaudio -g -- <one of those ids>   # one googlevideo URL, no JS-runtime warning
      systemctl list-timers yt-dlp-update.timer      # weekly upgrade is armed
      sudo systemctl start yt-dlp-update.service; journalctl -u yt-dlp-update -n 5   # "already yt-dlp ..." 
      ```
- [ ] **MPD can play what it is redirected to** — AAC in m4a, Opus in webm, over HTTPS. moOde's
      MPD has both pieces; check once anyway (the install script warns if not):
      ```sh
      mpd --version | grep -woE 'curl|ffmpeg' | sort -u   # both: curl follows the redirect, ffmpeg decodes
      ```
- [ ] **Fill `presets:`** in `config/variants/hasler-hm.yaml`. Exactly one source per key; legacy
      `url:` still means radio.
      ```yaml
      presets:
        - {n: 1, name: "Jazz",    radio: "http://example.com/jazz.mp3"}
        - {n: 2, name: "Morning", spotify: "spotify:playlist:<id>", shuffle: true}
        - {n: 3, name: "Kitchen", ytm: "https://music.youtube.com/playlist?list=<id>"}
        - {n: 4, name: "Legacy",  url: "http://example.com/stream"}      # = radio:
      ```
      Spotify: in the app, *Share → Copy link*, then turn `open.spotify.com/playlist/<id>` into
      `spotify:playlist:<id>`. YouTube Music: the playlist URL as it appears in the browser.
- [ ] **Private YouTube Music playlists — cookies.** Public playlists need nothing. For a private
      one, give yt-dlp a logged-in session:
      1. Open a **private browser window**, log in to music.youtube.com, export the cookies as a
         Netscape `cookies.txt` (any "Get cookies.txt" extension), then **close the window without
         logging out** — logging out invalidates the cookies.
      2. Put it where the service can read *and write* it — yt-dlp rewrites rotated cookies:
         ```sh
         sudo install -o pi -g pi -m 600 cookies.txt /var/lib/haslerd/ytm-cookies.txt
         ```
      3. Set `ytm.cookies: /var/lib/haslerd/ytm-cookies.txt` and restart `haslerd`.
      The file is the account's login — keep it out of the repo. A spare Google account is safest.
      Easier: sign in from the key page instead (next item) — it writes the same file for you.
- [ ] **YouTube Music on the key page — search, names, endless radio, your account.** With
      `ytmusicapi` installed (the install script does it, via the `pi` extra) the key page can
      search songs, artists, albums and playlists and turn a result into a preset: a **song** or
      **artist** becomes an *endless radio* (when the queue nears its end `haslerd` fetches more of
      the same radio), an **album** or **playlist** plays as itself. moOde's player and the key page
      show real titles and artists instead of `127.0.0.1:8099/ytm/…` URLs. `ytm: {catalog: false}`
      turns all of this off and leaves plain yt-dlp.
      ```sh
      ~/hasler/daemon/.venv/bin/python -c 'import ytmusicapi; print(ytmusicapi.__version__)'  # 1.12+
      ```
      **Signing in** adds Liked songs and private playlists, and gives yt-dlp the same login:
      1. On a desktop browser, open **music.youtube.com** and make sure you are signed in.
      2. Open DevTools (F12) → **Network**, type `browse` in the filter, then click around (Library,
         Home) until a **POST** to `browse?…` appears. Click it.
      3. Copy its request headers.
         **Firefox:** right-click the request → *Copy Value* → **Copy Request Headers**.
         **Chrome / Edge:** *Headers* tab → *Request Headers* → tick **Raw** (or "view source") →
         select everything in that block → copy.
      4. On the key page, open YouTube Music → **Sign in**, paste, and save. It shows the account
         name when YouTube accepts the login. Pasting only the value of the `Cookie:` header works
         too, as long as it contains `__Secure-3PAPISID`.

      This is the owner's account cookie: **treat it like a password**. It is kept in
      `/var/lib/haslerd/ytm-browser.json` and `ytm-cookies.txt`, both mode 600, is never logged,
      and **Sign out** on the key page deletes both. The cookie lasts months, not forever — if Liked
      songs stop loading (or the page says "not signed in"), sign in again. Signing out of that
      browser session invalidates it at once, so close the tab rather than logging out.
- [ ] `sudo systemctl restart haslerd && journalctl -u haslerd -f`, then press every key.

**Done when:** with the **phone off** and **Home Assistant stopped**, each key plays its own source;
pressing a playing **playlist** key again skips to the next track; and after a **power-cycle** the
box comes back playing the last preset (`cat /var/lib/haslerd/state.json` shows it).

---

## Phase 4 — Sources, tone, lamp

- [ ] PU key cycles Bluetooth → AirPlay → Spotify → radio; hold enters pairing for 120 s.
      (Spotify is go-librespot now — moOde's renderer stays off.)
- [ ] Tapping a preset while a renderer is connected disconnects it and returns to MPD.
- [ ] Pilot lamp on the Pi's GPIO — see *Lamp* below.
- [ ] Tone wheel drives CamillaDSP shelves over its websocket (`:1234`).

### Lamp

The common-anode RGB LED on GPIO 17 (red, pin 11), 22 (green, pin 15), 27 (blue, pin 13), common
to 3V3 — wiring in [`hardware.md`](hardware.md#pilot-lamp--rgb-led-resistors-on-the-prototype-board),
what it shows in [`lamp.md`](lamp.md). `scripts/install.sh` sets up all of it (on a release box
the same files come from the release's `integration/`):

- a marked block at the end of `/boot/firmware/config.txt`: `gpio=17,22,27=op,dh` (dark from
  the firmware on) and one `dtoverlay=pwm-gpio,gpio=N` per pin (kernel PWM, ~800 Hz);
- `/etc/udev/rules.d/99-hasler-pwm.rules`, so haslerd (user `pi`, group `gpio`) can drive
  `/sys/class/pwm`;
- `hasler-lamp-boot.service`: the amber breath from ~3-5 s until haslerd starts (haslerd stops it
  and carries on);
- `/usr/lib/systemd/system-shutdown/hasler-lamp-halt`: the LED goes dark at halt.

- [ ] Run `scripts/install.sh`, then **reboot** (config.txt only takes effect at boot).
- [ ] `ls /sys/class/pwm` shows three `pwmchip`s; `systemctl status hasler-lamp-boot` is
      *inactive (dead)* once haslerd runs, and its log names the chip for each GPIO.
- [ ] Power on: dark, then within ~5 s a soft amber breath, never a hard colour or a flash.
- [ ] `sudo poweroff`: ember breath while it shuts down, then **dark** — safe to unplug.

## Phase 5 — Home Assistant

*Optional, and not tied to any particular home: the radio works the same with no HA at all, and
whatever HA the house has is connected from the key page — no file editing, no restart.*

- [ ] **Media player, no code:** in HA, Settings → Devices & services → Add integration → **Music
      Player Daemon (MPD)** → host `hasler.local`, port `6600` → `media_player.hasler`.
- [ ] **Make a token:** in HA, click your user (bottom left) → **Security** → *Long-lived access
      tokens* → **Create token**, name it "Hasler radio", copy it (HA shows it only once).
- [ ] **Connect:** open `http://hasler.local:8080` → **Settings → Home Assistant**. The radio looks
      for HA on the network (mDNS `_home-assistant._tcp` via `avahi-browse`, plus
      `homeassistant.local:8123`) and lists what it finds; pick one or type the address
      (`http://<ip>:8123` — prefer the LAN `http://` address: an `https://` one with a self-signed
      certificate is refused). Paste the token → **Connect**. The radio checks it on the spot and says
      plainly what's wrong: can't reach the address, not Home Assistant, token rejected, certificate
      not trusted. The connection is saved in `/var/lib/haslerd/ha.json` (mode 0600; the token is
      never logged or sent back to the page).
- [ ] **Bind keys:** on the key page, a key's tap or hold can now be *Home Assistant scene*,
      *Run a script*, *Toggle a device* (light, switch, input_boolean, fan, cover, media player) or
      *Trigger an automation*. The picker lists that HA's entities, grouped by area; **Try it** runs
      the action now. Key presses are fire-and-forget: HA being down never delays a key.
- [ ] *(Optional)* **Publish what's playing:** turn on *Show the radio in Home Assistant*. The radio
      then keeps `sensor.hasler_radio` up to date — state `playing` / `standby` / `idle`, attributes
      `station`, `title`, `artist`, `source` — at most every 5 s and only on change. It's created
      through HA's REST API, so it isn't stored by HA: after an HA restart it reappears with the next
      change (or within 10 minutes). Turning the option off removes it.

**Done when:** `media_player.hasler` appears in HA, holding a key bound to a scene fires it, **and
the radio still works with HA stopped** (the key just does nothing).

### Moving to another home

The key actions name entities of the HA they were set up with. In a new home: Settings → Home
Assistant → **Disconnect**, then connect the new home's HA as above. The page flags every key whose
entity the new HA doesn't have ("Key 2 hold: scene.evening — not found", and that the keys were set
up with a different Home Assistant); rebind them, or **Clear missing** to empty them in one go. Until
then such a key logs once and does nothing.

## Phase 6 — Install in the cabinet

- [ ] Mount the Pi + Amp4 Pro + Power Controller stack.
- [ ] Dress and strain-relieve the panel loom. Use connectors — you will open this again.
- [ ] Fit the RGB lamp in the original bezel; wire the speaker and any external terminals.
- [ ] **Vent the rear panel.** A sealed wooden box has no airflow.

**Done when:** one hour of lid-on playback with no throttling (`vcgencmd get_throttled` → `0x0`),
and a power-cycle has it playing again within ~30 s.

---

## Two proofs worth doing at the end

Both are cheap, and both check that the abstraction actually earned its keep:

1. Swapping `io.digital` from `gpio` to `mcp23017` should change **nothing but that one line**.
2. Wiring the Excelsior should need **nothing but a new YAML** — with the interlocked-bank
   changeover never emitting a spurious "cleared". Watch `panel-probe.py` while cycling stations
   fast; the test suite already covers it, but the real switches are the real test.

## After a moOde update

`scripts/bootstrap.sh` (also run by `scripts/install.sh`) adds two lines to moOde for YouTube Music tracks: one in `/var/www/inc/mpd.php`
so moOde's player shows the album cover, one in `/var/www/inc/music-library.php` so a new track's
title appears at once instead of after a ~4 s `mediainfo` probe. A moOde update replaces those files:
covers and the quick titles go back to moOde's defaults until you re-run `sudo scripts/bootstrap.sh`
(safe to repeat). Originals are kept as `*.hasler-orig`.

## Over-the-air releases instead of install.sh

The finished radio does not run from a checkout: it runs signed releases from `/opt/hasler`,
its settings and calibration live in `/etc/hasler`, and it updates itself (docs/ota.md). Moving a
box over, once:

1. `sudo scripts/bootstrap.sh` — box setup (apt packages incl. `zstd` and `liblgpio1`, I²C,
   groups, the `hasler-update` and `ytdlp` users, CamillaDSP tone config owned by `pi` 0644,
   captive portal, moOde patch, yt-dlp off root).
2. `sudo scripts/ota-bootstrap.sh --trust <dir>` — the updater, its units and timers, the polkit
   rule (haslerd may start only the key page's update units), the hardware watchdog, and the
   release-signing trust (`root.pub`, `keyring.json(.sig)`); copies go-librespot's login to
   `/var/lib/go-librespot`.
3. `sudo daemon/.venv/bin/python -m haslerd.migrations --check`, then `--apply` (migration 001:
   `config/haslerd.yaml` and the edited descriptor become `/etc/hasler/box.yaml` and
   `variant.d/`), then `sudo scripts/ota-bootstrap.sh` once more (ownership; `--update-url` for a
   local channel).
4. The first release: `sudo hasler-update apply --seq <N> --force` once it is staged. From then on
   the key page's "About this radio" card checks, installs and goes back.

The bench box does this with throwaway dev keys and a channel served from a laptop:
[release.md, "Bench box"](release.md#bench-box-from-the-rsync-tree-to-releases-dev-keys-local-channel).

## Boot time

A radio should play soon after the switch, not two minutes later. moOde's worker waits for the
*whole* boot (`systemctl is-system-running` = running) before it starts MPD, so anything that holds
the boot open — waiting for the network, first-boot tools, a slow oneshot — delays the first sound.
`sudo scripts/bootstrap.sh` trims the OS (idempotent; `--dry-run` shows each change first):

- **Off:** cloud-init (only once every Wi-Fi profile is NetworkManager's own file in
  `/etc/NetworkManager/system-connections`; otherwise it says so and leaves it), winbind,
  `e2scrub_reap` + `e2scrub_all.timer`, triggerhappy (unless moOde's USB volume knob is on),
  ModemManager. **Masked:** `rpi-eeprom-update` — EEPROM updates come deliberately with a release.
  **Kept:** avahi (`hasler.local`), Bluetooth/hciuart, log2ram, smbd.
- **config.txt:** a marked block `# >>> hasler boot time ... >>>` after moOde's sections and before
  the updater's lamp block: `boot_delay=0`, `disable_splash=1`, `initial_turbo=30`,
  `camera_auto_detect=0`, `display_auto_detect=0`. moOde only checks its header lines, which stay
  untouched. Backup: `config.txt.hasler-boot-bak`.
- **cmdline.txt:** `quiet loglevel=3`, and no `console=serial0,115200` (nothing uses the serial
  console). Backup: `cmdline.txt.hasler-boot-bak`.
- **Wi-Fi power save off** (`/etc/NetworkManager/conf.d/hasler-wifi-powersave.conf`).
- **No waiting for the network:** haslerd and go-librespot no longer pull `network-online.target`
  (haslerd copes offline; go-librespot retries every 5 s until Spotify answers), and an empty
  `/etc/systemd/system/rc-local.service.d/debian.conf` stops rc-local (which launches moOde's worker)
  from waiting for it — the worker waits for an IP address itself, then falls back to the hotspot.
- **Opt-in `--eeprom`:** bootloader `BOOT_ORDER=0xf41` (SD, USB, retry) and `DISABLE_HDMI=1`, only if
  different; staged with `rpi-eeprom-config --apply`, written at the next reboot.
- **Opt-in `--early-mpd`** (`--no-early-mpd` undoes it): MPD starts in the boot transaction, as soon
  as the amp's ALSA card exists, instead of from the worker. The worker's later `systemctl start mpd`
  is a no-op. Caveat: if the worker ever logs "ALSA card: has been reassigned", MPD is running with
  the old card number until `sudo systemctl restart mpd` (`hasler-boot-report` warns).

config.txt, cmdline and EEPROM changes need a reboot (bootstrap says so). Then measure:

    sudo hasler-boot-report

It prints the kernel/userspace totals and critical chain, when rc-local, mpd, go-librespot and
haslerd started (seconds since the kernel started), the worker's milestones and `wrkready`,
haslerd's "first sound at X s" line, and whatever still pulls `network-online.target`.

**Clean-shutdown marker.** `hasler-clean-shutdown.service` (a release unit, pulled in by haslerd)
writes `/var/lib/haslerd/clean-shutdown` when systemd shuts down or reboots; haslerd reads and
deletes it at start, so a missing marker means the power was cut. It is not the halt hook's job:
`hasler-lamp-halt` runs after the filesystems are read-only.

## Moving the radio to a new Wi-Fi network

No screen, no keyboard — moOde's fallback hotspot does it.

1. Power the radio up where the old network isn't. moOde tries the saved Wi-Fi for **90 s**, then
   starts its own hotspot **"Hasler"**. The pilot lamp **breathes white** while it's up.
2. On a phone, join **Hasler** (password set in moOde → Network → Access Point; this box: see the
   owner's notes, not this file).
3. Open **http://172.24.1.1** → Configure → Network → pick the new SSID, enter its password, **Save**.
4. Reboot (moOde's power menu, or the tone knob held off 5 s, then on). It joins the new network; the
   lamp goes back to its source colour. The key page is at `http://hasler.local:8080` again.

The hotspot only starts at boot. If the Wi-Fi drops later, the box keeps trying it; power-cycle to get
the hotspot.

---

<sub>Delgado &amp; Murphy</sub>
