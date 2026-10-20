# The pilot lamp

The pilot lamp is the coloured light in the cabinet's original window. It tells
you what the radio is doing, the way the lamp of a valve radio did, so nobody
needs a manual.

- **Owners:** the short version is in
  [Using your Hasler](using.md#the-pilot-lamp). This page has every detail.
- **Makers and contributors:** this page is the design that the code follows
  (`daemon/haslerd/lamp/`). Change the code and this page together.

**In one breath:** amber is the radio, blue is a phone, warm white means "set
me up", a dim ember means asleep (breathing: busy, don't unplug), red means
something failed, a flash of magenta is another Hasler, and dark means safe to
unplug.

## Principles

1. **Amber is home.** Everything started from the radio's own keys is amber.
2. **One meaning per colour.** Amber = the radio, ember = asleep, blue = a
   phone is in charge, warm white = "set me up", red = something failed,
   magenta = the house (another Hasler; brief signatures only, never steady).
   No green, no teal, and no Home Assistant notices on the lamp.
3. **Brightness says how much is going on.** Dim = idle, full = playing,
   ember = asleep, **dark = safe to unplug**.
4. **Motion means working or needs you.** Steady = nothing to do. A breath =
   wait, busy. A dip, pulse, swell or signature = a one-off reply (see
   "Replies").
5. **Never alarm without a reason.** Red only for something you tried that
   failed (or will). Never in standby, never over a phone that plays fine,
   never for network blips under 60 s.
6. **Hands get a reply; the sound does the rest.** Keys get a dip. Knobs get
   nothing - the ear is the feedback.
7. **Respect the dark room.** Sunset to sunrise everything is dimmer and
   slower, and idle red is suppressed. (On the key page, Settings → Pilot
   lamp can also use fixed hours, or never dim.)
8. **Never look frozen.** No pause over 2 s while the box is busy.

## Palette

Levels are perceptual (gamma 2.2 applied after), balance per channel after
that. Starting balance for this LED (R 220 Ω, G/B 100 Ω on 3.3 V): R 1.0,
G 0.15, B 0 for amber - calibrate by eye at night.

| Name | Linear R, G, B | Target look | Meaning |
|---|---|---|---|
| Amber | 1.0, 0.15, 0 | filament orange-amber | the radio, any preset |
| Amber, pale | 1.0, 0.22, 0 | amber a touch lighter | **subtle source tint**: playlists (YouTube Music, Spotify presets); radio stays plain amber. Never a new hue. |
| Ember | 1.0, 0.10, 0 | deep warm orange-red, very dim; pure red at its deep levels (below) | asleep / going to sleep; breathing = busy |
| Blue | 0, 0, 1.0 | blue | a phone is driving, or pairing |
| Warm white | 1.0, 0.35, 0.06 | incandescent white | setup: Wi-Fi details needed |
| Red | 1.0, 0.03, 0 | red-orange | something failed |
| Magenta | 0.25, 0, 1.0 | blue-violet magenta | **the house**: another Hasler. Signatures only, never a steady look. |

Each channel has a minimum duty (red 0.002, green and blue 0.0005 = 625 ns
of the 800 Hz period: below that the PWM can't draw a pulse reliably). A
channel only gets its floor if it carries a real share of the colour (at
least 20 % of the brightest channel); a trace under its floor goes dark.

Ember was 6 % (2 % at night) until 2026-10-21: that is ~0.2 % red duty,
invisible behind the window - standby looked dark, which must mean "safe to
unplug". The owner picked ~3.5 % red duty by eye, so ember is now 22 % (15 %
at night, ~1.5 % duty) and the shutdown breath 15-35 %.

## Behaviour

| Moment | Colour | Level | Motion | Lasts |
|---|---|---|---|---|
| Ready (on, silent) | Amber | 35% | solid | until changed |
| Playing a preset | Amber (pale for playlists) | 100% | 1.5 s fade up, then solid | while playing |
| Key pressed | current | dip to 30% and back | 120 ms down, 200 ms up | 0.3 s |
| PU key (next source) | **signature** of where it went: amber for the radio, pale amber for a playlist, blue for a phone's stop (blue 30% while no phone is there yet) | the Look's, at least 35% | signature (below) | 1.6 s, then loading/ready/blue as usual |
| Volume key (hold) reaches the floor or ceiling | current | one **nudge** | as a key dip | once per press, not per repeat |
| Loading / buffering | Amber | 25-70% | breath, 1.2 s | until sound; after 20 s -> failure |
| Same key, radio (stop) | Amber | 100 -> 35% | 0.8 s fade | once |
| Same key, playlist (next) | Amber | one dip | as key press | 0.3 s |
| Failure (stream down, key while offline, HA call failed) | Red | 60% | 3 soft pulses, 0.6 s each | ~2 s, then back |
| Offline > 60 s, on and idle | Red | 10-30% | breath, 4 s | until online; not at night |
| Volume / tone turning | - | - | **no change** | - |
| Standby (knob clicked off, or same radio key) | Ember | 22% | 1.5 s fade from current | until woken |
| Knob held off -> shutdown (5 s) | -> Ember | falls to 15% | linear over the 5 s; knob on snaps back in 0.5 s | 5 s |
| Shutting down | Ember | 15-35% | breath, 2.5 s | ~10-15 s |
| **Installing an update** (the updater's trial: switch, restart, health check) | Ember | 15-35% | breath, 2.5 s (the shutdown look: busy, don't unplug) - **never solid ember** | until it commits or rolls back (~2-3 min); held through haslerd's restart |
| Halted, still powered | dark | 0 | 2 s fade-out by the last service | until power-on |
| Setup / hotspot | Warm white | 15-60% | breath, 3 s | until configured |
| Bluetooth pairing window | Blue | 15-60% | breath, 2 s | up to 2 min |
| A phone pairs (connects in the window) | Blue | 80%, one **swell** | crossfade to solid blue, swell over it | 0.8 s, then casting blue |
| Phone casting (Bluetooth, AirPlay, Spotify Connect) | Blue | 80% playing, 30% paused | 1.5 s crossfade | while the phone has it; amber 3 s after it lets go |
| **Phone casts while in standby** | Ember -> Blue | 80% | 1.5 s crossfade | **the phone wakes it** (standby ends, mixer unmuted) |
| Home Assistant (key hold) | current | two dips | 2 x 150 ms at the hold threshold | 0.5 s; red pulses only if it failed |
| **Joining a group** (multiroom follower) | Amber | 25-70% | breath, 1.2 s (as loading) | until the leader's stream plays; red pulses if it can't |
| **A radio joins or leaves a group** | **Magenta** signature, on the radio that joins/leaves **and** on the leader | the Look's, at least 35% | signature | 1.6 s; not for standby (ember says it), not for a handover's moves, red instead if it failed |
| **Following a group** | Amber (pale if the leader plays a playlist) | 100% | 1.5 s fade, then solid; no sign of following | while the leader plays; ready amber 35% while it is silent |
| Group leader can't be reached (follower) | Red | 60% | 3 soft pulses | ~2 s, then ready (the radio is alone again) |
| Preset key on a follower | current | dip, as key press | 0.3 s | the leader then shows loading/playing; the follower's tint follows within ~5 s |
| Preset key on a follower, the leader lacks its source (handover) | Amber | 25-70%, then 100% | breath (as loading), then as playing | the pressed radio leads: breath until its music plays; the others stay playing amber, in its tint at once; red pulses on the pressed radio if the takeover fails |
| Night (sunset -> sunrise) | same colours | cap 35%, ember 15% | fades, signatures, swells and nudges x1.5; idle red suppressed | the night |
| Panel board missing | Red | 10-30% | slow breath | until fixed (the keys can't work) |

A multiroom **leader** looks like any radio playing: nothing new. The
follower's states come from `BoxState.group` (`""`, `"joining"`,
`"following"`), `group_source` and `group_playing`, filled from
`multiroom/group.py` (`Group.lamp()`); the follower learns the leader's source
by reading its `/api/info` every 5 s.

**No change at all for:** volume and tone knobs, track changes nobody asked
for, metadata, Home Assistant notices, network blips under 60 s, offline while
a phone plays locally, and anything in standby except setup and a phone
waking it.

**Phones are all blue**, whatever they use (Bluetooth, AirPlay, Spotify
Connect). UPnP/DLNA casting is the exception for now: upmpdcli plays through
MPD and moOde keeps no "UPnP active" flag, so it reads as the radio (amber).
Telling it apart would mean comparing MPD's current URI with the one we
queued - not done yet.

## Replies

One-off motions over the current look, at most 3 s, then the look again; a
newer one replaces an older one. They come from a hand (keys) or from an
event the owner caused (a phone pairing, a radio joining).

| Reply | Shape | Used for |
|---|---|---|
| dip (`ack`, `next`) | to 30% and back, 120 ms down, 200 ms up | a key heard; same playlist key again |
| nudge | one dip, as a key press | a volume key at the floor or ceiling |
| stop | 0.8 s fade 100 -> 35% | same radio key again |
| two dips (`ha`) | 2 x 150 ms | Home Assistant action sent |
| fail | 3 soft red pulses, 0.6 s each | something you asked for failed |
| swell | +25% and back over 0.8 s | a phone paired |
| signature | 0.4 s crossfade to a hue, hold 0.6 s, 0.6 s back | the PU key's destination; magenta for a group join/leave |

Signature, swell and nudge are x1.5 slower at night (a signature is then
2.4 s). In code, a signature travels as one string, `signature:<colour>[@level]`
(`lamp/contract.py`); Actions and the group say what happened
(`source:<stop>[:<preset source>]`, `group`) and `lamp/policy.py`
(`reply_for`) picks the colour.

## Priority (highest first)

1. Shutdown countdown / shutting down / halted, and installing an update
2. Short replies (key dip, nudge, HA dips, failure pulses, swell, signatures) - overlay of at most 3 s,
   then whatever is underneath
3. Setup / hotspot
4. Phone casting (it beats standby: a phone wakes the radio) and offline
5. Standby
6. Boot warm-up
7. Bluetooth pairing window
8. Loading, and joining a group (the same breath)
9. Following a group whose leader plays (the leader's tint; never idle red
   over it - it plays)
10. Offline idle red (only when the next key press would fail)
11. Playing / ready

## Boot and shutdown

Like a valve radio: light comes first, and sound follows as the valves warm.
The breath grows stronger stage by stage, so the wait never looks frozen.

| t | Stage | Lamp |
|---|---|---|
| 0-3 s | power, firmware | dark - `gpio=17,22,27=op,dh` in config.txt holds the LED off (the reset pull-downs would otherwise glow it faintly); firmware can't fade, and a hard colour here reads as a fault |
| ~3-5 s | early `hasler-lamp-boot` service | slow 2 s fade up into an amber breath, 25 -> 60%, 3 s period |
| ~10 s | haslerd takes over (reads the knob) | breath continues, floor rises to 20% |
| ~15-25 s | network up | floor 30%, peak 50% |
| ~15 s (was ~40 s before 0.3.1) | music resumes | 1.5 s fade to solid amber 100% with the first sound |
| same, nothing to resume | ready | settle to solid amber 35% |

- **Knob off at power-on:** the early breath is ember (12-25%), then settles to
  solid ember; never amber, never sound.
- **No Wi-Fi:** amber warm-up, then a 2 s crossfade to the warm-white setup
  breath when the hotspot comes up. No red: it needs telling, not fixing.
- **After a power cut while playing:** the normal timeline ending in the old
  station (or blue if a phone reconnects); if it can't come back, three red
  pulses then ready amber. **At night** (sunset to sunrise) it stays asleep in
  ember instead.
- **Shutdown:** knob off -> ember -> 5 s countdown fade -> ember breath
  through the halt -> a `system-shutdown` hook drives the pins dark ->
  **dark = safe to unplug**.

## How it is built

On the current board (the prototype board on Pi GPIO):

- The lamp has its **own thread**: it sleeps while steady and renders at 50 Hz
  during fades and breaths (float levels, raised-cosine breathing, gamma 2.2,
  then per-channel balance). A slow preset start never blocks it.
- PWM: kernel `pwm-gpio` overlays on GPIO 17/22/27 at about 800 Hz, so there is
  no flicker at night. `backend: gpio` (software PWM) is only a fallback, used
  with a warning if the overlays are missing. Never pigpio: its DMA timing uses
  the PCM block that the amp's I2S needs.
- Deep night levels are limited on this wiring (green's share of a 2% amber is
  well under a microsecond); a minor channel under its minimum duty goes dark
  (see "Palette"), so deep ember is pure red - by design.
- Night = sunset to sunrise for the box's location (computed locally; HA's
  sun entity when connected).

## For the HAT (planned)

The HAT is the ordered circuit board that will replace the prototype board
([hat/BRIEF.md](hat/BRIEF.md)). The lamp plan for it:

An I2C PWM driver (PCA9685 at 0x40, or a constant-current RGB indicator
driver) with three low-side N-FETs (gate pull-downs keep the LED dark at
power-on) and the LED's common anode on **5 V**: green/blue get headroom,
deep dimming stays amber, three GPIOs come free, and the LED current leaves
the Pi's 3.3 V regulator. The shutdown hook then writes the halt colour over
I2C (the chip holds its last state).

## History

This design was agreed on 2026-10-19, from an inventory of the code at the time,
an interaction design and a hardware check. It shipped in release 0.3.1. What it
replaced:

- nine colours, one per source;
- offline = a hard red flash (also, wrongly, during AirPlay);
- standby = dim pure red;
- no key acknowledgement, no loading breath, no shutdown countdown; the pairing
  colour was defined but never shown;
- dark for the first ~10 s of boot, and the lamp froze during slow starts;
- 100 Hz software PWM without gamma, so it flickered at night; the radio's amber
  (255,150,40) looked yellow-green on this LED;
- docs disagreed on the pin map. The descriptor (R17 / G22 / B27, common anode)
  was right, and the other places have since been fixed.

In 0.3.2 the boot breath became brighter (a 2 s fade up, then 25-60 %), because
the first 10-35 % breath was almost invisible.

On 2026-10-21, after an interaction design and LED review: blue lost its green
(green was ~70 % of its light, so it read green) and pale amber its blue;
magenta became the house (another Hasler), as brief signatures only; deep
ember became pure red (no green floor); three new replies - signature, swell,
nudge - for the PU key's destination, a group join/leave, a phone pairing and
a volume key at its end; installing an update became the ember breath (it was
a solid ember, which reads as "asleep").
