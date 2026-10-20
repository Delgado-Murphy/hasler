# Build guide

This guide is for makers: it takes you from an empty Hasler cabinet to a working
radio. You need basic soldering, a multimeter, and some comfort with a Linux
command line over SSH. The parts are listed in [hardware.md](hardware.md).
If you already have a finished radio, read [Using your Hasler](using.md) instead.

**How to use this guide.** Work from top to bottom. Each phase ends with a check
you can really do. If the check fails, stop and fix it before you add the next
layer. This is the main lesson of our two earlier attempts: both half-worked,
and in neither could we see why.

**Two steps can destroy hardware.** They are marked **⚠️ DESTRUCTIVE**.
Everything else can be undone.

## The path at a glance

| Phase | What you do | Done when |
|---|---|---|
| [0](#phase-0--survey-the-panel) | Measure the keys, knobs and speaker | Every value is written down |
| [1a](#phase-1a--the-pi-on-its-own-first) | Set up the Pi alone, on a phone charger | You can SSH in |
| [1](#phase-1--base-image-and-audio) | Add the amplifier and 20 V power | The speaker makes a sound |
| [2](#phase-2--wire-the-panel-and-prove-it) | Wire the keys, watch them live | Every key shows clean presses |
| [3a](#phase-3a--keys-drive-the-music) | Install the software, keys play music | Each key plays its station |
| [3b](#phase-3b--wake-the-knobs) | Connect the knobs | Volume works for every source |
| [3c](#phase-3c--streaming-sources-spotify--youtube-music) | Spotify and YouTube Music | Playlists play from keys |
| [4](#phase-4--sources-tone-lamp) | PU key, tone, pilot lamp | The lamp breathes amber at power-on |
| [5](#phase-5--home-assistant) | Home Assistant (optional) | A held key runs a scene |
| [5b](#phase-5b--casting-room-names-and-multiroom) | Casting, room names, multiroom | A phone plays to it; two Haslers play in step |
| [6](#phase-6--install-in-the-cabinet) | Close it up | One hour of playback, no overheating |
| [After](#moving-to-over-the-air-releases) | Switch to signed releases | The radio updates itself |

**How the software gets onto the radio.** While you build, you install from a
copy of this repository on the Pi (`scripts/install.sh`). When the radio is
finished, you move it once to **signed releases**: from then on it updates
itself and you never copy code by hand again. See
[Moving to over-the-air releases](#moving-to-over-the-air-releases).

---

## Phase 0 — Survey the panel

*Bench work. Multimeter only, nothing powered. Result: a filled-in panel survey.*

The keys are already out, with a shared ground, so most of this phase is about
the knobs and the speaker. Write the results in
[variants/hasler-hm.md](variants/hasler-hm.md), which also explains how to
measure a pot.

- [ ] **Keys:** check that each is momentary (springs back) and has one contact.
      Note any key with extra contacts.
- [ ] **Which wire is which key:** skip the multimeter. With the MCP23017 you
      wire by the board's labels and check each key in Phase 2's `panel-probe`.
      With direct GPIO, `panel-probe.py --identify` finds the order for you.
- [ ] **Volume pot:** measure the resistance from end to end.
- [ ] **Tone pot:** measure end to end, then test the **end switch** through the
      whole turn. It should open only at the far-left click, and be separate
      from the resistive track (two separate wires).
- [ ] **Both pots, taper:** set the knob to the middle and measure wiper to end.
      About 50 % of the total means **linear**; about 10–20 % means
      **audio (log)**. Expect log.
- [ ] **Clean both pots** with contact cleaner and turn them 20 times or more.
      Do this *before* you decide one is broken. Old carbon tracks are
      scratchy, not dead.
- [x] **Speaker:** measured **14.2 Ω DC → 16 Ω nominal**, no transformer. It runs
      straight from the Amp4 Pro. The amp gives about 10 W into it; the old cone
      can take about 1–3 W. So you **must set the volume ceiling** (Phase 3a),
      and you **must never bridge the two amp channels**.
- [ ] **Pilot lamp window:** measure the opening, so the RGB LED fits the
      original bezel.

**⚠️ DESTRUCTIVE — while you are inside:** take out the original mains
transformer and **all** mains wiring. Discharge the old capacitors before they
go in the bin.

**Done when:** every measurement above is written down.

---

## Phase 1a — The Pi on its own, first

*Pi 4B, microSD card, and any ordinary 5 V USB-C phone charger. No amp, no
20 V.*

First prove the easy half works, before you add anything that can destroy
hardware. If moOde will not start or join Wi-Fi, you want to find out with a
cheap charger attached, not with a 20 V supply.

- [ ] Flash **moOde, 64-bit** (tested with **10.3.0 / Debian 13 trixie**). Turn
      SSH on and set the hostname to `hasler`. **Note the exact version.**
- [ ] Start the Pi **alone**, on a plain 5 V USB-C charger in its own power port.
- [ ] Join Wi-Fi. Check that `ssh hasler.local` works and the moOde web page loads.
- [ ] Turn on I²C: `sudo raspi-config nonint do_i2c 0 && sudo reboot`

**Done when:** you can SSH in and open the moOde page. No sound yet: there is no
amplifier.

> **⚠️ Never power the Pi from its own USB-C port *and* from the Amp4 Pro at the
> same time.** Once the amp is fitted, it feeds the Pi from its own 5 V. Use one
> source, never both.

## Phase 1 — Base image and audio

*Now add the amp and the 20 V supply. Goal: the speaker makes a sound.*

- [ ] Fit the **Amp4 Pro** on the **Pi 4B** (not a Pi 5: the amp's 5 V supply is
      rated "up to the Pi 4B").
- [ ] Set up the power, in order: see [Power bring-up](#power-bring-up) below.
- [ ] moOde → Audio config: choose the Amp4 Pro, set **Volume type: Hardware**.
- [ ] Note the mixer name. The volume knob will drive it:
      ```sh
      moodeutl -q "select value from cfg_system where param='amixname'"
      ```
- [ ] Turn on the renderers: **Bluetooth, AirPlay, UPnP/DLNA**. Turn *Resume MPD* on.
      **Leave moOde's Spotify Connect OFF.** go-librespot replaces it in Phase 3c,
      and two Spotify players on one sound card fight.
- [ ] Check I²C: `i2cdetect -y 1` shows `UU` at `4d` (the amp's own chip, in use
      by its driver).

**Done when:** `mpc play` makes sound in the original speaker, and every renderer
you turned on appears on a phone and plays.

### Power bring-up

Do these in order, with a meter. The details and the reasons are in
[hardware.md](hardware.md#protection-about-2-in-parts).

1. [ ] **Set the HW-398 PD trigger to 20 V:** bridge the **20 V** pad to the
       resistor next to it. **One pad only.** Bridging two puts the resistors in
       parallel and the result is unknown. Solder well and pull-test it.
2. [ ] **Measure it with nothing connected:** plug the trigger into the charger,
       nothing on the output, and measure. Expect **20.0 V ±5 %**.
3. [ ] **Unplug and replug three times, measuring each time.** This is the real
       test: it proves the setting holds and the board never starts at 5 V. The
       charger must offer 20 V; many 30–45 W chargers stop at 15 V, and the
       board then quietly gives less.
4. [ ] **Fit the protection** across the output, at the amp end: the
       **220–470 µF / 35 V** capacitor and a **22 V TVS diode** (SMAJ22A or
       SMBJ22A). Use a 24 only if the rail measured above 20.5 V.
       **⚠️ A one-way `A` diode has a band: the band goes to PLUS.** Backwards it
       is a dead short across 20 V. A two-way `CA` part cannot go in wrong.
       With 20 V applied, the TVS must stay **cold**. If it is even warm, it is
       the wrong part.
5. [ ] **⚠️ DESTRUCTIVE — never more than 24 V into the Amp4 Pro.** Wire **PD
       output → Amp4 Pro DC input**. Keep the wires short. The amp then feeds the
       Pi from its own 5 V.
6. [ ] Power up. If the charger stops and restarts, that is the inrush current
       into the amp's capacitors: fit the NTC limiter.

---

## Phase 2 — Wire the panel and prove it

*The phase both earlier attempts skipped.*

- [ ] **MCP23017 board** (the default; the CQRobot board is our choice): **four
      wires** to the Amp4 Pro's top header. VCC → **3.3 V** pin 1 (never 5 V),
      GND → pin 6, SDA → pin 3, SCL → pin 5. Leave INTA and INTB unconnected.
      Drawings: Paper sheets H1 (system) and H4 (key board).
- [ ] Green breakout only: measure **RST ↔ VCC**. It must read connected, or the
      chip stays in reset.
- [ ] `i2cdetect -y 1` → **`27`** for the CQRobot board, **`20`** for the green
      board. The descriptor says `addr: auto`, so either works with no edit.
- [ ] Wire the contacts to the board, with the common ground to the PA header's
      GND pin. **Nothing on PA7 or PB7:** they cannot be inputs on current chips,
      and the service refuses them. Which key goes to which pin is set in
      `config/variants/hasler-hm.yaml` (`controls:`); the shipped file follows our
      prototype board ([hardware.md](hardware.md#keys)).
- [ ] Fit a **100 nF** capacitor across each key.
- [ ] Watch it live:
      ```sh
      scripts/panel-probe.py
      ```
      The WIRE column shows which board pin each row expects.

> **No MCP23017?** Use the direct-GPIO block in `hasler-hm.yaml` instead. Wire to
> any eight free GPIOs and let `scripts/panel-probe.py --identify` work out the
> `pins:` list.

**Done when** all three are true:

- every key lights **its own row and no other** (no "ghosting");
- the **TRANSITIONS** column stays in low single digits per press. Tens mean
  dirty contacts or a missing capacitor: **fix it in hardware now**, not in
  software later;
- the tone end switch reads closed with the knob on, and open past the click.

---

## Phase 3a — Keys drive the music

*No new parts needed.*

- [ ] Copy the repository to the Pi (for example `git clone` into `~/hasler`).
- [ ] Put real stream URLs into `presets:` in `config/variants/hasler-hm.yaml`
      (later, owners change them on the key page).
- [ ] Run `scripts/install.sh` (`--dry-run` first shows what it will change).
      It runs `scripts/bootstrap.sh` (one-time box setup), then installs
      `haslerd` from your copy as a systemd service with a watchdog. Run it again
      after every `git pull`; it is safe to repeat.
      This is the **building** layout. Once the radio runs signed releases (see
      [Moving to over-the-air releases](#moving-to-over-the-air-releases)), this
      script refuses to run and the updater takes over.
- [ ] **Set the volume ceiling.** On the key page (`http://hasler.local:8080`),
      Settings → **Volume ceiling**. Start at 50. Play dense music with the knob
      at maximum. Raise it in steps of 5 until it is as loud as you will ever
      want. **At the first sign of distortion from the cone, go back two
      steps.** The speaker is the one part in this build you cannot replace.

**Done when:** each key plays its station; tapping the playing station again
puts the radio in standby; turning the tone knob past the click goes to standby
and back on resumes; and the knob at maximum is loud but clean.

## Phase 3b — Wake the knobs

*When the ADS1115 arrives.*

- [ ] Fit the ADS1115 on I²C. `i2cdetect -y 1` → `48`.
- [ ] Wire each pot as a divider across 3.3 V. **Tone wiper → A0, volume wiper →
      A1**, and a **100 nF** capacitor from each wiper to GND.
- [ ] Check that `io.analog` in the descriptor is `{backend: ads1115, addr: 0x48}`.
- [ ] Run `panel-probe.py` and turn each pot from end to end. The COUNTS column
      must rise smoothly, with no jumps.
- [ ] **Calibrate:** key page → Settings → **Knobs** → **Calibrate**. It asks you
      to turn each knob to a few positions and saves the curve on this radio
      (in `/etc/hasler/variant.d/`, so updates keep it). You can also write
      measured `curve:` points into the descriptor by hand
      ([variants/hasler-hm.md](variants/hasler-hm.md#6-pot-calibration--phase-3b-once-the-ads1115-is-fitted)).

**Done when:** the volume knob is silent at rest, smooth when turned, and
controls Bluetooth, AirPlay and Spotify as well as radio. That last part is what
the hardware mixer gives you.

## Phase 3c — Streaming sources (Spotify + YouTube Music)

*Software only. No phone, no Spotify developer account, no Home Assistant.
Spotify needs **Premium**.*

A key can now start a radio station, a **Spotify playlist** or a **YouTube Music
playlist**.

- **Spotify** plays through **go-librespot**, a Spotify Connect player on the Pi.
  `haslerd` controls it through its local API (`127.0.0.1:3678`).
- **YouTube Music** goes through MPD. `haslerd` finds each track with **yt-dlp**
  and plays it, so old stream links never expire on you.

Steps:

- [ ] `scripts/install.sh` adds go-librespot (a fixed, checked version), **deno**
      (the JavaScript runtime yt-dlp needs), and the permission that lets the tone
      knob shut the Pi down cleanly. Its `bootstrap.sh` step puts yt-dlp in
      `/opt/yt-dlp` under its own user `ytdlp` (not root). It updates weekly,
      builds the new version beside the old one, and switches only if a
      self-check passes (`scripts/hasler-ytdlp-update`; `--rollback` goes back one).
- [ ] Copy the `spotify:`, `ytm:` and `state_file:` blocks from
      `config/haslerd.yaml.example` into `config/haslerd.yaml`. The install
      script warns while any are missing.
- [ ] **Turn moOde's Spotify renderer off:** moOde → Configure → Renderers →
      Spotify Connect → **OFF**. The Amp4 Pro can only be used by one player at a
      time.
      ```sh
      moodeutl -q "select value from cfg_system where param='spotifysvc'"   # expect 0
      ```
- [ ] **Log go-librespot in, once.** It has no screen, so tunnel the login from a
      laptop:
      ```sh
      ssh -L 36842:127.0.0.1:36842 pi@hasler.local
      journalctl -u go-librespot -n 50 | grep -o 'https://accounts.spotify.com[^ "]*'
      ```
      Open that link in the laptop's browser and log in with the **Premium**
      account. The answer comes back through the tunnel, and the login is saved
      (in `~/.config/go-librespot/state.json`; on a release radio in
      `/var/lib/go-librespot`).
      - No tunnel? Copy the failed `127.0.0.1:36842/login?code=…` address from the
        browser and `curl` it on the Pi.
      - No browser at all? Set `credentials: {type: device_auth}` in
        go-librespot's `config.yml`, restart it, and enter the code from its log
        at `spotify.com/pair`.
      ```sh
      curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:3678/status   # 200 = logged in, 204 = not
      ```
- [ ] **Check that yt-dlp works, with deno.** Without deno, YouTube gives fewer
      formats or none, with no clear error:
      ```sh
      deno --version                       # 2.9.x
      yt-dlp --flat-playlist --print id 'https://music.youtube.com/playlist?list=<id>' | head -3
      yt-dlp -f bestaudio -g -- <one of those ids>   # one googlevideo URL, no JS-runtime warning
      systemctl list-timers yt-dlp-update.timer      # weekly update is set
      sudo systemctl start yt-dlp-update.service; journalctl -u yt-dlp-update -n 5
      ```
- [ ] **Check MPD can play the streams** (AAC and Opus over HTTPS). moOde's MPD
      has what it needs; check once anyway:
      ```sh
      mpd --version | grep -woE 'curl|ffmpeg' | sort -u   # both must appear
      ```
- [ ] **Fill in `presets:`.** One source per key; a plain `url:` still means radio.
      ```yaml
      presets:
        - {n: 1, name: "Jazz",    radio: "http://example.com/jazz.mp3"}
        - {n: 2, name: "Morning", spotify: "spotify:playlist:<id>", shuffle: true}
        - {n: 3, name: "Kitchen", ytm: "https://music.youtube.com/playlist?list=<id>"}
      ```
      Spotify: in the app, *Share → Copy link*, then turn
      `open.spotify.com/playlist/<id>` into `spotify:playlist:<id>`. YouTube
      Music: the playlist address from the browser. (The key page does all of
      this for you, with search.)
- [ ] **YouTube Music on the key page.** With `ytmusicapi` installed (the install
      script does it), the key page can search songs, artists, albums and
      playlists and put a result on a key. A **song** or **artist** becomes an
      *endless radio*; an **album** or **playlist** plays as itself. Players show
      real titles instead of internal addresses. `ytm: {catalog: false}` turns
      this off.
- [ ] **Private playlists and Liked songs: sign in.** Public playlists need
      nothing. For private ones, sign in on the key page: Settings → YouTube
      Music → **Sign in**, and follow the steps it shows (copy the request
      headers of a `browse` request from your browser's developer tools and
      paste them). The page shows the account name when YouTube accepts it.
      - This is the account's login cookie: **treat it like a password**. It is
        saved with mode 600 (`ytm-browser.json` and `ytm-cookies.txt` in
        `/var/lib/haslerd/`, or `/var/lib/haslerd/secrets/` on a release radio),
        never logged, and **Sign out** deletes both. A spare Google account is
        safest.
      - It lasts months, not forever. If Liked songs stop loading, sign in again.
        Logging out of that browser session ends it at once, so close the tab
        instead of logging out.
- [ ] `sudo systemctl restart haslerd && journalctl -u haslerd -f`, then press
      every key.

**Done when:** with the **phone off** and **Home Assistant stopped**, each key
plays its own source; pressing a playing **playlist** key again skips to the next
track; and after you **unplug and replug** the radio, it comes back playing the
last key (`cat /var/lib/haslerd/state.json` shows it).

---

## Phase 4 — Sources, tone, lamp

- [ ] The **PU** key: a tap goes to the next source (radio → Bluetooth → AirPlay →
      Spotify → radio); holding it opens Bluetooth pairing for 120 s.
- [ ] Tapping a key while a phone is connected takes the radio back from the phone.
- [ ] The tone knob drives CamillaDSP treble shelves (over its websocket, `:1234`).
- [ ] The pilot lamp, on the Pi's GPIO: see below.

### Lamp

A common-anode RGB LED: red on GPIO 17 (header pin 11), green on GPIO 22 (pin 15),
blue on GPIO 27 (pin 13), common to 3.3 V. Wiring is in
[hardware.md](hardware.md#pilot-lamp--rgb-led-resistors-on-the-prototype-board);
what it shows is in [lamp.md](lamp.md). `scripts/install.sh` sets up all of it
(on a release radio, the same files come from the release):

- a marked block at the end of `/boot/firmware/config.txt`: `gpio=17,22,27=op,dh`
  (dark from the moment the firmware starts) and one `dtoverlay=pwm-gpio,gpio=N`
  per pin (kernel PWM at about 800 Hz);
- `/etc/udev/rules.d/99-hasler-pwm.rules`, so `haslerd` (user `pi`, group `gpio`)
  can drive `/sys/class/pwm`;
- `hasler-lamp-boot.service`: the amber breath from about 3–5 s after power-on
  until `haslerd` takes over;
- `/usr/lib/systemd/system-shutdown/hasler-lamp-halt`: the LED goes dark at halt.

Checks:

- [ ] Run `scripts/install.sh`, then **reboot** (config.txt only changes at boot).
- [ ] `ls /sys/class/pwm` shows three `pwmchip`s. `systemctl status hasler-lamp-boot`
      is *inactive (dead)* once `haslerd` runs.
- [ ] Power on: dark, then within about 5 s a soft amber breath. Never a hard
      colour or a flash.
- [ ] `sudo poweroff`: an ember breath while it shuts down, then **dark**: safe to
      unplug.

## Phase 5 — Home Assistant

*Optional. The radio works the same with no Home Assistant (HA) at all. Whatever
HA the house has is connected from the key page, with no file editing.*

- [ ] **Not the MPD integration.** HA may offer *Music Player Daemon* for the
      radio: skip it. MPD only sees the internet-radio stream (not Spotify,
      YouTube Music, Bluetooth or AirPlay, presets or standby), and an HA
      "play"/"stop" behind the radio's back confuses it. The radio announces
      itself on the network as `_hasler._tcp` for the Hasler integration for
      HA instead (`GET http://hasler.local:8080/api/info` answers who it is).
- [ ] **Make a token:** in HA, click your user (bottom left) → **Security** →
      *Long-lived access tokens* → **Create token**. Name it "Hasler radio" and
      copy it (HA shows it only once).
- [ ] **Connect:** key page → **Settings → Home Assistant**. The radio looks for
      HA on the network and lists what it finds. Choose one, or type the address
      (`http://<ip>:8123`; use the plain `http://` address, because an
      `https://` one with a self-signed certificate is refused). Paste the token
      → **Connect**. The radio checks it at once and says clearly what is wrong,
      if anything. The token is saved with mode 600 and never sent back to the
      page.
- [ ] **Give keys a job:** in Edit keys, a key's tap or hold can now run a
      *scene* (with a fade), a *script* or an *automation*, *toggle* a light,
      switch, fan, cover or media player, or **set lights**: one light or
      several, on / off / toggle, brightness, a colour or a white temperature,
      and a fade time. Each bulb gets what it can do (a plain dimmable bulb
      dims and ignores the colour). **Try it** runs it now and says what HA
      changed. A key never waits for HA: if HA is down, the key just does
      nothing.
- [ ] *(Optional)* **Show what is playing:** turn on *Show the radio in Home
      Assistant*. The radio keeps `sensor.hasler_radio` up to date (state
      `playing` / `standby` / `idle`, with `station`, `title`, `artist`,
      `source`). After an HA restart it comes back with the next change, or
      within 10 minutes.

**Done when:** holding a key bound to a scene runs it, a key set to dim a light
dims it, **and the radio still works with HA stopped**.

- [ ] *(Optional)* **The Hasler integration for HA:** HACS → **⋮** → **Custom
      repositories** → `https://github.com/Delgado-Murphy/hasler-homeassistant`,
      type **Integration** → find **Hasler Radio** → **Download** → restart HA →
      **Settings → Devices & services**: the radio is **Discovered** → **Add**.
      It gives a `media_player` (the preset keys are its sources), an `update`
      entity and a health sensor. Done when HA's media player shows what the
      radio plays and choosing a source presses that key.

### Moving to another home

Key actions name things in the HA they were set up with. In a new home:
Settings → Home Assistant → **Disconnect**, then connect the new home's HA. The
page lists every key whose target the new HA does not have. Choose new targets,
or press **Clear missing** to empty them all.

## Phase 5b — Casting, room names and multiroom

*Casting is on from the start; multiroom is optional and off by default. The
details are in [multiroom.md](multiroom.md).*

- [ ] **The packages.** `sudo scripts/bootstrap.sh` installs `shairport-sync`
      (classic AirPlay; moOde's trixie repository has none, moOde still starts
      it), `snapserver` and `snapclient` (Debian's own units masked) and
      `libasound2-plugins`. On a radio set up before 0.4.0, run it once more:
      a release can't install Debian packages.
- [ ] **Casting.** Key page → **Settings → Sources & casting** → *Play from a
      phone*: AirPlay, Bluetooth and UPnP / DLNA each show *on* and running. A
      target whose program is missing says so and can't be switched on.
      Spotify Connect is listed read-only; **Guests on your Wi-Fi can play**
      lets other people's Spotify find it.
- [ ] **Room name.** Settings → **About this radio** → room name (*Kitchen*).
      The page title changes; `hasler.local` does not. With two radios, the
      second answers at `hasler-2.local` by itself. **Use the room name for
      casting** renames AirPlay, UPnP, Bluetooth and Spotify to *Hasler
      Kitchen* one by one (a minute or two; Spotify blinks once).
- [ ] **Multiroom** (needs two Haslers). On both: Settings → **Multiroom** →
      *Play in several rooms*. On one, front page → **Also play on…** → tick the
      other. `sudo hasler-multiroom status` says `lead` on one and `follow` on
      the other.

**Done when:** a phone plays to the radio over AirPlay, Bluetooth and Spotify;
two radios play the same station in step (sound about 1 s after a key);
standby on the follower leaves the leader playing; standby on the leader ends
the group; and after unplugging both, each comes back on its own.

## Phase 6 — Install in the cabinet

- [ ] Mount the Pi + Amp4 Pro stack.
- [ ] Tidy the panel wiring and give it strain relief. Use connectors: you will
      open this again.
- [ ] Fit the RGB lamp in the original bezel. Wire the speaker (left channel
      only, L+ and L−).
- [ ] **Make vents in the back panel.** A closed wooden box has no airflow.

**Done when:** one hour of playback with the back on and no throttling
(`vcgencmd get_throttled` → `0x0`), and after unplugging it plays again within
about 30 s.

---

## Moving to over-the-air releases

A finished radio does not run from a copy of the repository. It runs **signed
releases** from `/opt/hasler`, keeps its settings and calibration in
`/etc/hasler`, and updates itself ([ota.md](ota.md)). You move a radio over once:

1. `sudo scripts/bootstrap.sh`: box setup (packages incl. `zstd` and
   `liblgpio1`, I²C, groups, the `hasler-update` and `ytdlp` users, the CamillaDSP
   tone file, the Wi-Fi setup portal, the moOde patch, yt-dlp off root).
2. `sudo scripts/ota-bootstrap.sh --trust <dir>`: the updater, its timers, the
   polkit rule (the key page may start only its own update units), the hardware
   watchdog, and the release-signing trust (`root.pub`, `keyring.json` and
   `keyring.json.sig`, from the public `hasler-releases` repository). It also
   copies go-librespot's login to `/var/lib/go-librespot`.
3. `sudo daemon/.venv/bin/python -m haslerd.migrations --check`, then `--apply`.
   This is migration 001: `config/haslerd.yaml` and your edited descriptor become
   `/etc/hasler/box.yaml` and `/etc/hasler/variant.d/`. Then run
   `sudo scripts/ota-bootstrap.sh` once more (it fixes ownership).
4. Install the first release once it is downloaded:
   `sudo hasler-update apply --seq <N> --force`. From then on, the key page's
   **About this radio** section checks, installs and goes back.

To practise this with throwaway keys and a channel served from your laptop, see
[release.md, "Bench box"](release.md#bench-box-from-the-rsync-tree-to-releases-dev-keys-local-channel).

## Moving the radio to a new Wi-Fi network

No screen or keyboard needed. If the radio does not find a known network for
**90 s** after power-on, moOde starts a hotspot called **"Hasler"** and the pilot
lamp **breathes warm white**. A phone that joins it is sent to the radio's Wi-Fi
page (or open `http://172.24.1.1:8080/wifi`). Choose the network, type the
password, press **Connect**; the radio restarts onto it. The owner's steps are in
[Using your Hasler](using.md#wi-fi).

- The hotspot password is set in moOde → Network → Access Point. Keep it out of
  this repository and give it to the owner.
- The hotspot only starts at boot. If the Wi-Fi drops later, the radio keeps
  trying it; unplug and replug to get the hotspot.
- On the home network, Settings → Wi-Fi → **Change** on the key page does the
  same, after a press on the radio.

---

## Notes for makers

### After a moOde update

`scripts/bootstrap.sh` adds two lines to moOde for YouTube Music: one in
`/var/www/inc/mpd.php` (album covers in moOde's player) and one in
`/var/www/inc/music-library.php` (a new track's title appears at once, not after
a ~4 s probe). A moOde update replaces those files. Covers and quick titles then
go back to moOde's defaults until you run `sudo scripts/bootstrap.sh` again (safe
to repeat). The originals are kept as `*.hasler-orig`.

### Boot time

A radio should play soon after you switch it on. moOde's worker waits for the
*whole* boot to finish before it starts MPD, so anything that holds the boot open
(waiting for the network, first-boot tools, a slow service) delays the first
sound. `sudo scripts/bootstrap.sh` trims the system (safe to repeat; `--dry-run`
shows each change first):

- **Turned off:** cloud-init (only once every Wi-Fi profile is NetworkManager's
  own file; otherwise it says so and leaves it), winbind, `e2scrub_reap` and
  `e2scrub_all.timer`, triggerhappy (unless moOde's USB volume knob is on),
  ModemManager. **Masked:** `rpi-eeprom-update` (EEPROM updates come with a
  release, on purpose). **Kept:** avahi (`hasler.local`), Bluetooth, log2ram,
  smbd.
- **config.txt:** a marked block `# >>> hasler boot time ... >>>` after moOde's
  sections and before the lamp block: `boot_delay=0`, `disable_splash=1`,
  `initial_turbo=30`, `camera_auto_detect=0`, `display_auto_detect=0`. moOde's
  header lines stay untouched. Backup: `config.txt.hasler-boot-bak`.
- **cmdline.txt:** `quiet loglevel=3`, and no serial console. Backup:
  `cmdline.txt.hasler-boot-bak`.
- **Wi-Fi power saving off** (`/etc/NetworkManager/conf.d/hasler-wifi-powersave.conf`).
- **No waiting for the network:** `haslerd` and go-librespot do not wait for
  `network-online.target` (haslerd copes offline; go-librespot retries every
  5 s). An empty `/etc/systemd/system/rc-local.service.d/debian.conf` stops
  rc-local (which starts moOde's worker) from waiting for it. The worker waits
  for an IP address itself, then falls back to the hotspot.
- **Optional `--eeprom`:** bootloader `BOOT_ORDER=0xf41` (SD, USB, retry) and
  `DISABLE_HDMI=1`, only if different.
- **Optional `--early-mpd`** (`--no-early-mpd` undoes it): MPD starts as soon as
  the amp's sound card exists, instead of from the worker. If the worker ever
  logs "ALSA card: has been reassigned", run `sudo systemctl restart mpd`
  (`hasler-boot-report` warns about this).

config.txt, cmdline and EEPROM changes need a reboot. Then measure:

    sudo hasler-boot-report

It prints the boot totals, when rc-local, mpd, go-librespot and haslerd started,
moOde's milestones, haslerd's "first sound at X s" line, and anything that still
waits for the network.

**Clean-shutdown marker.** `hasler-clean-shutdown.service` writes
`/var/lib/haslerd/clean-shutdown` when the system shuts down or reboots. `haslerd`
reads and deletes it at start, so a missing marker means the power was cut. (After
a power cut at night, the radio stays asleep.)

**Boot config guard.** moOde's worker rewrites `config.txt` at every boot, and
the boot partition writes back lazily. A power cut seconds later can leave the
file empty or cut short. The worker then copies its own default over it: no amp,
no lamp. Three defences:

- `/boot/firmware` is mounted with the vfat `flush` option (bootstrap edits
  `/etc/fstab`).
- `hasler-bootcfg-save.timer` (4 min after boot), the updater and bootstrap run
  `hasler-bootcfg-guard save`: a *sane* `config.txt`/`cmdline.txt` is copied to
  `/etc/hasler/boot/*.good`. Sane means: moOde's main header on line 2 and its
  four section headers, the line under `# Audio overlays` equal to moOde's
  `cfg_system.i2soverlay`, and every hasler block the good copy has.
- `hasler-bootcfg-guard.service` runs `repair` early in boot, before moOde's
  worker. If the file is not sane, the good copy is written back, the broken one
  is kept in `/var/lib/hasler-bootcfg/`, and the radio reboots once (at most once
  per 10 min). A repair that did not hold is logged
  (`journalctl -u hasler-bootcfg-guard -p crit`). A broken `cmdline.txt` is
  restored without a reboot. `sudo hasler-bootcfg-guard check` tells you what it
  would do.

### Two proofs worth doing at the end

Both are cheap, and both check that the "panel as data" idea really works:

1. Switching `io.digital` from `gpio` to `mcp23017` should change **nothing but
   that one line**.
2. Wiring the Excelsior should need **nothing but a new YAML file**, and its
   locking key bank should never report a false "cleared" while you change
   stations fast. Watch `panel-probe.py` while you do it.

---

<sub>Delgado &amp; Murphy</sub>
