# The pilot lamp

One RGB LED behind the cabinet's original window. It should read like a valve
radio's pilot lamp that learned a few quiet words: nobody reads a manual.
Agreed 2026-10-19 from an inventory of today's code, an interaction design and
a hardware check (see "Today" at the end for what this replaces).

## Principles

1. **Amber is home.** Everything started from the radio's own keys is amber.
2. **One meaning per colour.** Amber = the radio, ember = asleep, blue = a
   phone is in charge, warm white = "set me up", red = something failed.
3. **Brightness says how much is going on.** Dim = idle, full = playing,
   ember = asleep, **dark = safe to unplug**.
4. **Motion means working or needs you.** Steady = nothing to do. A breath =
   wait, busy. A dip or pulse = a one-off reply to a hand.
5. **Never alarm without a reason.** Red only for something you tried that
   failed (or will). Never in standby, never over a phone that plays fine,
   never for network blips under 60 s.
6. **Hands get a reply; the sound does the rest.** Keys get a dip. Knobs get
   nothing - the ear is the feedback.
7. **Respect the dark room.** Sunset to sunrise everything is dimmer and
   slower, and idle red is suppressed.
8. **Never look frozen.** No pause over 2 s while the box is busy.

## Palette

Levels are perceptual (gamma 2.2 applied after), balance per channel after
that. Starting balance for this LED (R 220 Ω, G/B 100 Ω on 3.3 V): R 1.0,
G 0.15, B 0 for amber - calibrate by eye at night.

| Name | Target look | Meaning |
|---|---|---|
| Amber | filament orange-amber | the radio, any preset |
| Amber, pale | amber a touch lighter/cooler | **subtle source tint**: playlists (YouTube Music, Spotify presets); radio stays plain amber. Never a new hue. |
| Ember | deep warm orange-red, very dim | asleep / going to sleep |
| Blue | soft blue | a phone is driving, or pairing |
| Warm white | incandescent white | setup: Wi-Fi details needed |
| Red | red-orange | something failed |

## Behaviour

| Moment | Colour | Level | Motion | Lasts |
|---|---|---|---|---|
| Ready (on, silent) | Amber | 35% | solid | until changed |
| Playing a preset | Amber (pale for playlists) | 100% | 1.5 s fade up, then solid | while playing |
| Key pressed | current | dip to 30% and back | 120 ms down, 200 ms up | 0.3 s |
| Loading / buffering | Amber | 25-70% | breath, 1.2 s | until sound; after 20 s -> failure |
| Same key, radio (stop) | Amber | 100 -> 35% | 0.8 s fade | once |
| Same key, playlist (next) | Amber | one dip | as key press | 0.3 s |
| Failure (stream down, key while offline, HA call failed) | Red | 60% | 3 soft pulses, 0.6 s each | ~2 s, then back |
| Offline > 60 s, on and idle | Red | 10-30% | breath, 4 s | until online; not at night |
| Volume / tone turning | - | - | **no change** | - |
| Standby (knob clicked off, or same radio key) | Ember | 6% | 1.5 s fade from current | until woken |
| Knob held off -> shutdown (5 s) | -> Ember | falls to 2% | linear over the 5 s; knob on snaps back in 0.5 s | 5 s |
| Shutting down | Ember | 4-20% | breath, 2.5 s | ~10-15 s |
| Halted, still powered | dark | 0 | 2 s fade-out by the last service | until power-on |
| Setup / hotspot | Warm white | 15-60% | breath, 3 s | until configured |
| Bluetooth pairing window | Blue | 15-60% | breath, 2 s; 1 s to solid on success | up to 2 min |
| Phone casting (Bluetooth, AirPlay, Spotify Connect) | Blue | 80% playing, 30% paused | 1.5 s crossfade | while the phone has it; amber 3 s after it lets go |
| **Phone casts while in standby** | Ember -> Blue | 80% | 1.5 s crossfade | **the phone wakes it** (standby ends, mixer unmuted) |
| Home Assistant (key hold) | current | two dips | 2 x 150 ms at the hold threshold | 0.5 s; red pulses only if it failed |
| Night (sunset -> sunrise) | same colours | cap 35%, ember 2% | fades x1.5; idle red suppressed | the night |
| Panel board missing | Red | 10-30% | slow breath | until fixed (the keys can't work) |

**No change at all for:** volume, tone, track changes nobody asked for,
metadata, network blips under 60 s, offline while a phone plays locally, and
anything in standby except setup and a phone waking it.

## Priority (highest first)

1. Shutdown countdown / shutting down / halted
2. Short replies (key dip, HA dips, failure pulses) - overlay of at most 3 s,
   then whatever is underneath
3. Setup / hotspot
4. Phone casting (it beats standby: a phone wakes the radio) and offline
5. Standby
6. Boot warm-up
7. Bluetooth pairing window
8. Loading
9. Offline idle red (only when the next key press would fail)
10. Playing / ready

## Boot and shutdown

A valve radio: light comes first, sound as the valves warm. The breath grows
warmer stage by stage so the ~40 s wait never looks frozen.

| t | Stage | Lamp |
|---|---|---|
| 0-3 s | power, firmware | dark - `gpio=17,22,27=op,dh` in config.txt holds the LED off (the reset pull-downs would otherwise glow it faintly); firmware can't fade, and a hard colour here reads as a fault |
| ~3-5 s | early `hasler-lamp-boot` service | 1 s fade into an amber breath, 10 -> 35%, 3 s period |
| ~10 s | haslerd takes over (reads the knob) | breath continues, floor rises to 20% |
| ~15-25 s | network up | floor 30%, peak 50% |
| ~40 s | music resumes | 1.5 s fade to solid amber 100% with the first sound |
| ~40 s, nothing to resume | ready | settle to solid amber 35% |

- **Knob off at power-on:** the early breath is ember (2-8%), then settles to
  solid ember; never amber, never sound.
- **No Wi-Fi:** amber warm-up, then a 2 s crossfade to the warm-white setup
  breath when the hotspot comes up. No red: it needs telling, not fixing.
- **After a power cut while playing:** the normal timeline ending in the old
  station (or blue if a phone reconnects); if it can't come back, three red
  pulses then ready amber.
- **Shutdown:** knob off -> ember -> 5 s countdown fade -> ember breath
  through the halt -> a `system-shutdown` hook drives the pins dark ->
  **dark = safe to unplug**.

## How it is built (current board)

- Lamp gets its **own thread**: sleeps while steady, renders at 50 Hz during
  fades/breaths; float levels, raised-cosine breathing, gamma 2.2, then
  per-channel balance. Never blocked by a slow preset start (today the main
  loop freezes the lamp while yt-dlp expands a playlist).
- PWM: kernel `pwm-gpio` overlays on GPIO 17/22/27 at ~800 Hz (today:
  gpiozero/lgpio software PWM at 100 Hz, which flickers at night). Never
  pigpio - its DMA timing uses the PCM block the amp's I2S needs.
- Deep night levels are limited on this wiring (green's share of a 2% amber is
  microseconds); below ~5% amber shifts towards red - acceptable for ember.
- Night = sunset to sunrise for the box's location (computed locally; HA's
  sun entity when connected).

## For the HAT

An I2C PWM driver (PCA9685 at 0x40, or a constant-current RGB indicator
driver) with three low-side N-FETs (gate pull-downs keep the LED dark at
power-on) and the LED's common anode on **5 V**: green/blue get headroom,
deep dimming stays amber, three GPIOs come free, and the LED current leaves
the Pi's 3.3 V regulator. The shutdown hook then writes the halt colour over
I2C (the chip holds its last state).

## Today (what this replaces, 2026-10-19)

Nine colours, one per source; offline = hard red flash (also wrongly during
AirPlay); standby = dim pure red; no key acknowledgement, no loading, no
shutdown countdown, pairing colour defined but never shown; dark for the first
~10 s of boot; lamp freezes during slow starts; 100 Hz linear PWM without
gamma; radio amber (255,150,40) renders yellow-green on this LED. Docs
disagree on the pin map - the descriptor (R17 / G22 / B27, common anode) is
right; `docs/hardware.md` and `gpio_lamp.py` defaults are stale.
