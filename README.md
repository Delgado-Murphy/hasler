# Hasler Music Box

<sub><i>A Delgado &amp; Murphy restoration</i></sub>

<p align="center">
  <img src="docs/img/hasler.jpg" width="720"
       alt="The Hasler HAT, rev A: a green circuit board the size of a Raspberry Pi, with the cabinet's ribbon-cable connector along the bottom edge, a green 20 V power terminal at the left and 'Delgado &amp; Murphy, Hasler HAT rev A 2026' printed on it">
</p>

A Swiss radio from the 1960s that plays music again.

The **Hasler HM** was built in Bern around 1964 for Telefónica's *Hilo Musical*,
a service that sent music to homes over the telephone line. Its piano keys chose
one of six music channels. We took out the old electronics and put a small
computer (a Raspberry Pi) and a modern amplifier inside. The keys, the knobs,
the pilot lamp and the original speaker all still work.

<!-- docs/img/hasler.jpg is a render of the HAT board for now; swap in a photo of the finished cabinet when there is one. -->

## What it can do

- **Press a key, hear music.** Each key plays an internet radio station, a
  Spotify playlist or a YouTube Music playlist. No phone and no app needed.
- **Be a speaker for your phone** with Spotify Connect (guests on your Wi-Fi
  too), AirPlay, Bluetooth or UPnP/DLNA.
- **Play in several rooms.** Two or more Haslers can play the same music in
  step: tick *Also play on…* and the kitchen joins the living room. Each radio
  keeps its own volume, and its own room name (*Hasler Kitchen*).
- **One volume knob for everything**, with a safe ceiling to protect the old
  speaker. The tone knob sets the treble and puts the radio to sleep.
- **A pilot lamp that talks:** amber when playing, blue for a phone, a dim ember
  when asleep, dark when it is safe to unplug.
- **A key page** in your browser (`http://hasler.local:8080`) to choose music for
  each key, change settings and calibrate the knobs.
- **Safe updates** that install at night and go back by themselves if something
  goes wrong. Small ones install by themselves; bigger ones ask first. Every
  radio is on the **Stable** channel; **Bench** is the makers' test radio.
- **Home Assistant**, if you want it: a key can run a scene or switch a light,
  and the [Hasler Radio integration](https://github.com/Delgado-Murphy/hasler-homeassistant)
  shows each radio in Home Assistant. Everything else works without it.

## Where to start

| You are... | Read this |
|---|---|
| **Living with a Hasler** and want to know how to use it | [Using your Hasler](docs/using.md) |
| **Building one** | [Build guide](docs/build-guide.md), then [Hardware](docs/hardware.md) |
| **Helping with the code** | [Contributing](docs/contributing.md) |
| **Curious what changed** | [Releases](https://delgado-murphy.github.io/hasler-releases/) and the [changelog](CHANGELOG.md) |

## All the documents

**For owners**

- [Using your Hasler](docs/using.md): keys, knobs, lamp, phones, several rooms, Home Assistant, key page, Wi-Fi, updates, problems.
- [The pilot lamp](docs/lamp.md): every colour and what it means, in detail.

**For makers**

- [Build guide](docs/build-guide.md): from an empty cabinet to a working radio, step by step.
- [Hardware](docs/hardware.md): parts list, wiring, power and protection.
- [Multiroom and casting](docs/multiroom.md): how Haslers play in step (Snapcast), room names, AirPlay, UPnP, Bluetooth, Spotify.
- [Hasler HM panel survey](docs/variants/hasler-hm.md): measuring this cabinet's keys, knobs and speaker.
- [Prototype board](docs/prototype/README.md) and the [HAT design brief](docs/hat/BRIEF.md).

**For contributors and maintainers**

- [Contributing](docs/contributing.md): code layout, tests, how a cabinet's panel is described.
- [Over-the-air updates](docs/ota.md): how updates stay safe.
- [Releasing](docs/release.md): how a new version is built, signed and published.
- [OTA formats](docs/ota-format.md): the exact file formats (a contract between the builder and the radio).

## How it works, in one picture

```
keys ───▶ MCP23017 (or Pi GPIO) ──┐
knobs ──▶ ADS1115 ────────────────┤
                                  ▼
                               haslerd ──▶ pilot lamp
                    ┌─────────────┼──────────────┬───────────────────┐
                    ▼             ▼              ▼                   ▼
             MPD (moOde)    go-librespot     key page :8080    Home Assistant
          radio, YouTube      Spotify                            (optional)
              Music

MPD │ go-librespot │ Bluetooth │ AirPlay │ UPnP ──▶ Amp4 Pro (hardware mixer = the knob) ──▶ original speaker
                         │
                         └─ in a group: Snapcast ──▶ the other Haslers (each through its own amp and knob)
```

`haslerd` is our small service on the Pi. It reads the keys and knobs, starts
the music, drives the lamp and serves the key page. The music system underneath
is [moOde audio](https://moodeaudio.org/). Bluetooth, AirPlay (shairport-sync)
and UPnP come from moOde; Spotify comes from go-librespot; YouTube Music
streams are found by yt-dlp. Several Haslers play in step through
[Snapcast](https://github.com/badaix/snapcast) ([multiroom.md](docs/multiroom.md)).

**More cabinets are coming.** An *Excelsior* and others are waiting, and their
panels work differently (for example, keys that lock down). So the panel is
**data, not code**: one service, a few control types, and one YAML file per
cabinet in `config/variants/`.

---

<sub>Delgado &amp; Murphy · Hasler HM, Bern 1964 — restored 2026</sub>
