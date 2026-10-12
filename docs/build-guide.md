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
      stay unconnected. Full diagram: Paper sheets 09–10.
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
- [ ] `scripts/install.sh` — installs `haslerd` as a systemd service with a watchdog. Re-run it after
      every `git pull`; it is idempotent, and `--dry-run` shows what it would change.
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

- [ ] `scripts/install.sh` — adds go-librespot (pinned binary, config, unit), yt-dlp in
      `/opt/yt-dlp` with a weekly upgrade timer, **deno** as its JavaScript runtime,
      `/var/lib/haslerd`, and the sudoers drop-in that lets the tone knob power off cleanly.
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
- [ ] Wire the RGB LED to the **Power Controller** (I²C `0x77`, reg `0x01` mode, `0x02`–`0x04` R/G/B).
      Amber = radio, blue = Bluetooth, cyan/green/magenta = AirPlay/Spotify/DLNA, dim red = standby,
      flashing red = no network.
- [ ] Tone wheel drives CamillaDSP shelves over its websocket (`:1234`).

## Phase 5 — Home Assistant

- [ ] Add HA's built-in **MPD integration** → `hasler.local:6600` → `media_player.hasler`. No code.
- [ ] Confirm whether an **MQTT broker exists** — the instance runs Hue/Matter/WiZ/ESPHome and may
      have none. If not: add Mosquitto, or set `ha.transport: rest` and use a long-lived token.
- [ ] Bind hold actions to existing automations: `1775164862373` (All Off), `1775164862375` (Movie),
      `living_room_switch_hold_b4_relax`.
- [ ] **Delete** the stale ESPHome `hasler.yaml` device and the *"hasler Button 1 turned on"*
      automation, so nothing stale is left triggering.

**Done when:** `media_player.hasler` appears in HA, holding a key fires its scene, **and the radio
still works with HA stopped.**

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

`scripts/install.sh` adds two lines to moOde for YouTube Music tracks: one in `/var/www/inc/mpd.php`
so moOde's player shows the album cover, one in `/var/www/inc/music-library.php` so a new track's
title appears at once instead of after a ~4 s `mediainfo` probe. A moOde update replaces those files:
covers and the quick titles go back to moOde's defaults until you re-run `scripts/install.sh` (safe to
repeat). Originals are kept as `*.hasler-orig`.

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
