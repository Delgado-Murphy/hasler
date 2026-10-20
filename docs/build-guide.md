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
| [4b](#phase-4b--move-to-signed-releases) | Switch to signed releases | The radio runs a release and updates itself |
| [5](#phase-5--home-assistant) | Home Assistant (optional) | A held key runs a scene |
| [5b](#phase-5b--casting-room-names-and-multiroom) | Casting, room names, multiroom | A phone plays to it; two Haslers play in step |
| [6](#phase-6--install-in-the-cabinet) | Close it up | One hour of playback, no overheating |

**How the software gets onto the radio.** While you build (Phases 2–4), you
install from a copy of this repository on the Pi with `scripts/install.sh`.
This *building layout* is quick to change, but it leaves out a few things that
only a release installs:

- Bluetooth pairing that says yes by itself (`hasler-bt-accept`);
- multiroom (`hasler-multiroom` and the Snapcast units) and the Spotify name
  following the room name;
- the `_hasler._tcp` announcement, which Home Assistant and other Haslers use
  to find the radio;
- the boot config guard and the clean-shutdown marker
  ([Notes for makers](#boot-time));
- **Calibrate** on the key page, which saves to `/etc/hasler/`.

So once the keys, knobs and lamp work, you move the radio once to **signed
releases** ([Phase 4b](#phase-4b--move-to-signed-releases)). (A radio whose hardware already
works - a new SD card, a spare, a second Hasler - skips all of this: [A new SD
card](new-install.md) installs a signed release from a blank card, with no
screen or keyboard.) From then on it
updates itself and you never copy code by hand again. (To try your own code
changes on a release radio, build a release from your copy and serve it from
your laptop: [release.md, "Bench box"](release.md#bench-box-from-the-rsync-tree-to-releases-dev-keys-local-channel).)

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
- [ ] Turn on the renderers **Bluetooth** and **UPnP/DLNA**, and *Resume MPD*.
      AirPlay has to wait: moOde's trixie repository has no `shairport-sync`,
      so AirPlay arrives with `scripts/bootstrap.sh` in Phase 3a.
      **Leave moOde's Spotify Connect OFF.** go-librespot replaces it in Phase 3c,
      and two Spotify players on one sound card fight.
- [ ] Check I²C: `i2cdetect -y 1` shows `UU` at `4d` (the amp's own chip, in use
      by its driver).

**Done when:** `mpc play` makes sound in the original speaker, and a phone
finds the radio over Bluetooth and plays to it.

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
- [ ] Copy the repository to the Pi (for example `git clone` into `~/hasler`,
      which is where the later steps expect it).
- [ ] Watch it live:
      ```sh
      cd ~/hasler && scripts/panel-probe.py
      ```
      The WIRE column shows which board pin each row expects. Before Phase 3a
      it runs on the system Python; if it says `No module named 'yaml'` or
      `'smbus2'`, run `sudo apt install python3-yaml python3-smbus2`.

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

- [ ] Run `scripts/install.sh` from `~/hasler` (`--dry-run` first shows what it
      will change). It runs `sudo scripts/bootstrap.sh` (one-time box setup:
      packages, I²C, users, yt-dlp, AirPlay, Snapcast, the Wi-Fi setup portal),
      then installs `haslerd` from your copy as a systemd service with a
      watchdog, plus go-librespot, deno, the helpers and the lamp's boot files.
      Run it again after every `git pull`; it is safe to repeat. Once the radio
      runs signed releases (Phase 4b), this script refuses to run and the
      updater takes over.
- [ ] **Check `config/haslerd.yaml`.** The first run makes it from
      `config/haslerd.yaml.example`. `mixer:` must be the hardware mixer name
      you noted in Phase 1, and `card:` the Amp4 Pro's id in
      `cat /proc/asound/cards`. Then `sudo systemctl restart haslerd`.
- [ ] Now turn on moOde's **AirPlay** renderer (Configure → Renderers).
- [ ] *(Optional)* The shipped `presets:` in `config/variants/hasler-hm.yaml`
      are a test set that plays (Swiss and Spanish radio, one Spotify and two
      YouTube Music playlists). Change them here if you like; later the owner
      changes them on the key page.
- [ ] **Set the volume ceiling.** On the key page (`http://hasler.local:8080`),
      Settings → **Sound** → **Volume ceiling**. Start at 50. The knobs are not
      wired yet, so **hold key 6** (volume up) until the volume stops rising,
      with dense music playing. Raise the ceiling in steps of 5 until it is as
      loud as you will ever want. **At the first sign of distortion from the
      cone, go back two steps.** The speaker is the one part in this build you
      cannot replace.

**Done when:** each key plays its station; tapping the playing station again
puts the radio in standby; turning the tone knob past the click goes to standby
and back on resumes; a phone finds the radio over AirPlay; and key 6 held to the
top is loud but clean.

## Phase 3b — Wake the knobs

*When the ADS1115 arrives.*

- [ ] Fit the ADS1115 on I²C. `i2cdetect -y 1` → `48`.
- [ ] Wire each pot as a divider across 3.3 V. **Tone wiper → A0, volume wiper →
      A1**, and a **100 nF** capacitor from each wiper to GND.
- [ ] Check that `io.analog` in the descriptor is `{backend: ads1115, addr: 0x48}`.
- [ ] Run `panel-probe.py` and turn each pot from end to end. The COUNTS column
      must move smoothly, with no jumps (one way or the other: our volume pot
      reads backwards, and its `curve:` allows for that).
- [ ] **Calibrate.** The shipped `curve:` fits our cabinet's pots; yours will
      differ. Two ways:
      - **By hand, now:** read the COUNTS column at a few positions and write
        the `curve:` points into `config/variants/hasler-hm.yaml`
        ([variants/hasler-hm.md, section 6](variants/hasler-hm.md#6-pot-calibration--phase-3b-once-the-ads1115-is-fitted)).
        The move to releases (Phase 4b) carries them over.
      - **On the key page, after Phase 4b:** Settings → **Keys & knobs** →
        **Calibrate**. It asks you to turn each knob to a few positions and
        saves the curve in `/etc/hasler/variant.d/`, so updates keep it.
- [ ] Check the volume ceiling again, now with the knob turned fully up.

**Done when:** the volume knob is silent at rest, smooth when turned, and
controls Bluetooth and AirPlay as well as radio (Spotify too, after Phase 3c).
That last part is what the hardware mixer gives you.

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

- [ ] Nothing new to install: Phase 3a's `scripts/install.sh` already added
      go-librespot (a fixed, checked version), **deno** (the JavaScript runtime
      yt-dlp needs) and `ytmusicapi`. Its `bootstrap.sh` step put yt-dlp in
      `/opt/yt-dlp` under its own user `ytdlp` (not root). It updates weekly,
      builds the new version beside the old one, and switches only if a
      self-check passes (`scripts/hasler-ytdlp-update`; `--rollback` goes back one).
- [ ] `config/haslerd.yaml` needs the `spotify:`, `ytm:` and `state_file:`
      blocks. A file made from the example has them; for an older file, copy
      them from `config/haslerd.yaml.example`. The install script warns while
      any are missing.
- [ ] **Turn moOde's Spotify renderer off:** moOde → Configure → Renderers →
      Spotify Connect → **OFF**. The Amp4 Pro can only be used by one player at a
      time.
      ```sh
      moodeutl -q "select value from cfg_system where param='spotifysvc'"   # expect 0
      ```
- [ ] **Sign the radio in to Spotify.** No laptop, no tunnel: key page →
      **Settings → Phones & casting → Spotify Connect → Sign in**, press any key
      on the radio to confirm, then on a phone go to **spotify.com/pair**, sign
      in with the **Premium** account and enter the code the page shows. The
      page switches to *Signed in as jd······fe* by itself.
      - How it works (`web/spotify_account_api.py`): haslerd backs up
        `/var/lib/go-librespot/state.json` (`state.json.before-sign-in`),
        clears its credentials, sets `credentials: type: device_auth` in
        go-librespot's `config.yml` and restarts it. go-librespot asks Spotify
        for a pairing code and offers it on `127.0.0.1:3678/auth/code`; once
        approved it stores the account in `state.json` (as before). No approval
        within 15 min, or **Cancel**: the old account is put back. **Sign out**
        backs up to `state.json.before-sign-out` and sets `type: zeroconf`
        (guests only). Guests' logins are never stored.
      - A fresh radio (config from `config/go-librespot/config.yml`, already
        `device_auth`) shows a code on the page right away.
      - Check from the Pi:
        ```sh
        curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:3678/status      # 200 = logged in, 204 = not
        curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:3678/auth/code   # 200 = a code is waiting
        ```
      - The old way still works, for a radio without the key page: tunnel the
        browser login (`credentials: type: interactive`, `callback_port: 36842`):
        `ssh -L 36842:127.0.0.1:36842 pi@hasler.local`, then open the
        `https://accounts.spotify.com…` link from `journalctl -u go-librespot`.
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
      nothing. For private ones, sign in on the key page: Settings → This
      radio → YouTube Music → **Sign in**, and follow the steps it shows (copy the request
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

*Mostly checks: the software is already in place. The lamp is the one new part.*

- [ ] The **PU** key: a tap goes to the next source (by default radio →
      Bluetooth → AirPlay → Spotify → radio; the owner can change the list on
      the key page, Settings → Keys & knobs). Holding it opens Bluetooth
      pairing for 120 s. On the building layout nothing says yes to the
      phone's code yet: confirm it on moOde's own page, or leave this check
      for after Phase 4b, where the radio does it by itself.
- [ ] Tapping a key while a phone is connected takes the radio back from the phone.
- [ ] The **tone knob** is a *tilt*: left warmer, right brighter, flat in the
      middle, pivoting at 800 Hz. `haslerd` hands it to `hasler-dsp`, which
      sets CamillaDSP live over its websocket (`:1234`) and never lets the
      sound get louder than flat. The details, and the other effects a knob can
      have, are in [sound.md](sound.md).
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

**Done when:** PU steps through the sources with a short glow of where it
landed; a quarter turn of the tone knob is clearly heard; the lamp breathes
amber at power-on, sits in a dim ember in standby, and goes dark after
`sudo poweroff`.

## Phase 4b — Move to signed releases

*Software only, once. Do it now, before the house features in Phases 5 and
5b: several of them come only with a release (see the list at the top).*

A finished radio does not run from a copy of the repository. It runs **signed
releases** from `/opt/hasler`, keeps its settings and calibration in
`/etc/hasler`, and updates itself ([ota.md](ota.md)). From `~/hasler`:

1. [ ] `sudo scripts/bootstrap.sh`: the box setup again (safe to repeat), so
       every package a release needs is there.
2. [ ] `sudo scripts/ota-bootstrap.sh --trust <dir>`: the updater, its timers,
       the polkit rule (the key page may start only its own update units), the
       hardware watchdog, and the release-signing trust (`root.pub`,
       `keyring.json` and `keyring.json.sig`, from the public
       `hasler-releases` repository). It also copies go-librespot's login to
       `/var/lib/go-librespot`, so Spotify stays logged in.
3. [ ] `sudo daemon/.venv/bin/python -m haslerd.migrations --check`, then
       `--apply`. This is migration 001: `config/haslerd.yaml` and your edited
       descriptor become `/etc/hasler/box.yaml` and `/etc/hasler/variant.d/`
       (your knob curves included). If your copy is not in `/home/pi/hasler`,
       add `--legacy <path>`. Then run `sudo scripts/ota-bootstrap.sh` once
       more (it fixes ownership).
4. [ ] Download and install the first release:
       ```sh
       sudo systemctl start hasler-update-check.service
       cat /var/lib/hasler-update/status.json          # the staged release and its seq
       sudo hasler-update apply --seq <N> --force
       ```

**Done when:** `hasler-update status` shows the release as committed;
`scripts/install.sh` now refuses to run; and the key page's **Settings → This
radio → Updates** shows the version and checks for the next one.

To practise this with throwaway keys and a channel served from your laptop, see
[release.md, "Bench box"](release.md#bench-box-from-the-rsync-tree-to-releases-dev-keys-local-channel).

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
- [ ] **Connect:** key page → **Settings → This radio → Home Assistant**. The radio looks for
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
- [ ] *(Optional)* **The Hasler integration for HA:** HACS → **⋮** → **Custom
      repositories** → `https://github.com/Delgado-Murphy/hasler-homeassistant`,
      type **Integration** → find **Hasler Radio** → **Download** → restart HA →
      **Settings → Devices & services**: the radio is **Discovered** → **Add**.
      It gives a `media_player` (the preset keys are its sources), an `update`
      entity and a health sensor. HA's media player then shows what the radio
      plays, and choosing a source presses that key.

**Done when:** holding a key bound to a scene runs it, a key set to dim a light
dims it, **and the radio still works with HA stopped**.

### Moving to another home

Key actions name things in the HA they were set up with. In a new home:
Settings → This radio → Home Assistant → **Disconnect**, then connect the new home's HA. The
page lists every key whose target the new HA does not have. Choose new targets,
or press **Clear missing** to empty them all.

## Phase 5b — Casting, room names and multiroom

*Casting is on from the start; multiroom is optional and off by default. The
details are in [multiroom.md](multiroom.md).*

- [ ] **The packages.** Already there: `bootstrap.sh` installed
      `shairport-sync` (classic AirPlay; moOde still starts it), `snapserver`
      and `snapclient` (Debian's own units masked) and `libasound2-plugins`.
      The units and the `hasler-multiroom` helper came with the release in
      Phase 4b. (A radio set up before 0.4.0 needs `sudo scripts/bootstrap.sh`
      once more: a release can't install Debian packages.)
- [ ] **Casting.** Key page → **Settings → Phones & casting**: AirPlay,
      Bluetooth and *Other music apps* (UPnP / DLNA) each show *on* and running. A
      target whose program is missing says so and can't be switched on.
      Spotify Connect is listed read-only; **Guests on your Wi-Fi can play**
      lets other people's Spotify find it.
- [ ] **Room name.** Settings → **This radio** → room name (*Kitchen*).
      The page title changes; `hasler.local` does not. With two radios, the
      second answers at `hasler-2.local` by itself. **Use the room name on
      phones** (Settings → Phones & casting) renames AirPlay, UPnP, Bluetooth and Spotify to *Hasler
      Kitchen* one by one (a minute or two; Spotify blinks once).
- [ ] **Multiroom** (needs two Haslers). On both: Settings → **Rooms** →
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

## Moving the radio to a new Wi-Fi network

No screen or keyboard needed. If the radio does not find a known network for
**90 s** after power-on, moOde starts a hotspot called **"Hasler"** and the pilot
lamp **breathes warm white**. A phone that joins it is sent to the radio's Wi-Fi
page (or open `http://172.24.1.1:8080/wifi`). Choose the network, type the
password, press **Connect**; the radio restarts onto it. The owner's steps are in
[Using your Hasler](using.md#wi-fi).

- **The hotspot password.** moOde's import of Raspberry Pi Imager's Wi-Fi
  leaves the hotspot without one, and a hotspot without a password never
  starts. haslerd checks at every start (30 s in, `Wifi.ensure_hotspot` →
  `hasler-wifi-join` `"hotspot"`, `"ensure"`): no password → it sets a random
  one (`abcd-efgh-jkmn`, no 0/o/1/l) and keeps it in
  `/var/lib/haslerd/hotspot.txt` (0600); `GET /api/wifi` returns it as
  `setup.password` - write it down for the owner. A profile without its
  address (`address1=`, see below) is regenerated. A password you chose in
  moOde → Network → Access Point is never replaced (it is stored only as a
  key, so `/api/wifi` says `password_set` without it). Keep it out of this
  repository.
- **`address1=`.** moOde's `cfgNetworks()` takes the hotspot's address
  (`ap_network_addr`, 172.24.1.1/24) from moOde's PHP session. Run outside
  moOde (from a CLI, or `hasler-wifi-join` before 2026-10-21) it writes an
  empty `address1=`, and NetworkManager puts the hotspot on 10.42.0.1, where
  the captive DNS, the lamp and the page's no-key-press rule all miss it.
  Check: `sudo grep address1 /etc/NetworkManager/system-connections/Hotspot.nmconnection`.
- **When it starts.** moOde's worker waits up to 90 s (`ipaddr_timeout`) for
  wlan0 to get an address, then brings up its `Hotspot` profile
  (`activateHotspot()`) - only at boot, and it never leaves it by itself.
  haslerd's watchdog (`haslerd/wifiwatch.py`) does: 3 min after the hotspot
  appears, then after 5, 10, 15 and every 30 min, while no phone is on the
  hotspot and nothing plays, it asks `hasler-wifi-join` (`"retry"`) to look
  for moOde's network and the saved ones. If one is back, the helper joins it
  and restarts the radio onto it; a wrong password waits an hour. So a power
  cut where the router boots slower than the radio heals by itself.
- **Captive portal.** The hotspot's DNS answers every name with `172.24.1.1`
  (`config/captive/dnsmasq-hotspot.conf`), and nginx sends every `http://`
  request that arrives at that address to `:8080/wifi`
  (`config/captive/nginx-captive.conf`) - that is what opens the phone's
  "sign in to network" sheet. moOde stays reachable on the hotspot at
  `http://172.24.1.1/index.php`, and its Network page at
  `http://172.24.1.1/net-config.php` is the fallback if haslerd is down.
- On the home network, Settings → This radio → Wi-Fi → **Change** on the key page does the
  same, after a press on the radio. The network it replaces stays saved: if
  the new one fails at boot, NetworkManager goes back to the old one.
- **No network at all** (a mistyped Wi-Fi in Imager, no hotspot): a network
  cable to the Pi's Ethernet socket (it gets an address by DHCP), or
  `hasler-wifi.txt` on the boot partition (`ssid = …`, `password = …`,
  optionally `hotspot_password = …`): `hasler-wifi-rescue.service` applies it
  before moOde's worker looks for a network, deletes it and writes
  `hasler-wifi-result.txt` ([Using your Hasler](using.md#if-nothing-works-a-file-on-the-memory-card)).

---

## Notes for makers

### Maker tools on the key page

The owner's Settings has six rows. A seventh, **Maker tools**
(`#settings/maker`), appears when you tap the version number at the bottom of
Settings seven times (each tap within 1.5 s of the last). It holds what an
owner should not stumble on:

- **Lamp colours**: each colour of the pilot lamp, set by eye at night on the
  real lamp (see [lamp.md](lamp.md#palette));
- **Knob curve details**: the calibration points behind each knob (the owner's
  **Calibrate** is in Settings → Keys & knobs);
- **Inside the player (moOde)**: a link to moOde's own pages;
- **Diagnostics**: what answers and what doesn't, with details to copy into a
  bug report.

With Maker tools on, Settings → This radio → Updates also shows the **update
channel** (Stable or Bench).

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

The last two items below come with a release (Phase 4b); the building layout
has neither.

**Clean-shutdown marker.** `hasler-clean-shutdown.service` writes
`/var/lib/haslerd/clean-shutdown` when the system shuts down or reboots. `haslerd`
reads and deletes it at start, so a missing marker means the power was cut. (After
a power cut at night, the radio stays asleep.)

**Boot config guard.** moOde's worker rewrites `config.txt` at every boot, and
the boot partition writes back lazily. A power cut seconds later can leave the
file empty or cut short. The worker then copies its own default over it: no amp,
no lamp. Three defences:

- `/boot/firmware` is mounted with the vfat `flush` option (bootstrap edits
  `/etc/fstab`; it applies at the next boot, since vfat ignores it on a
  remount). `hasler-boot-report` warns while it is not in effect, and when
  `config.txt` has lost the boot-time block.
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
