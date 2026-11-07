# The pilot lamp

The pilot lamp is the coloured light in the cabinet's original window. It tells
you what the radio is doing, the way the lamp of a valve radio did, so nobody
needs a manual.

- **Owners:** the short version is in
  [Using your Hasler](using.md#the-pilot-lamp). This page has every detail.
- **Makers and contributors:** this page is the design that the code follows
  (`daemon/haslerd/lamp/`). Change the code and this page together.

**In one breath:** amber is the radio, ember means asleep, moon blue means a
phone is in charge. A breath means busy (don't unplug), a quick flash means
"heard you", a sag means "no", two flashes mean "I need you", a gentle swell
means something arrived or left. Dark means safe to unplug.

The language (agreed November 2026): **3 colours, 6 behaviours, 2 transition
rules.** Nothing else.

## Principles

1. **Amber is home.** Everything the radio does by itself is amber.
2. **One meaning per colour.** Amber = the radio, ember = asleep, moon blue =
   a phone is in charge. No red, no white, no magenta: failure is a motion
   (refuse), setup is a motion (beckon), another Hasler is a motion (swell).
3. **Brightness says how much is going on.** Ready is dim, playing is bright,
   ember is asleep, **dark = safe to unplug** - and nothing else is ever dark.
4. **Motion means working or needs you.** Steady = nothing to do. A breath =
   busy. A kick, a refusal, a beckon or a swell = a reply.
5. **Never alarm without a reason.** Offline is not a state: only a press that
   can't work refuses. Network blips, offline while a phone plays, the
   volume knob: nothing.
6. **Hands get a reply within one frame.** A key press kicks the lamp within
   25 ms. Knobs get nothing - the ear is the feedback.
7. **Respect the dark room.** Sunset to sunrise everything is dimmer, falls
   and swells are slower and beckons are half as tall. (On the key page,
   Settings -> Pilot lamp can also use fixed hours, or never dim.)
8. **Never look frozen, never jump.** No pause over 2 s while the box is busy;
   no colour ever changes in one frame.

## Palette

Three colours. Ratios are linear R : G : B before calibration
(`lamp/engine.py` `DEFAULT_PALETTE`; each radio calibrates its own, see
"Calibration").

| Colour | Linear R : G : B | Looks like | Means |
|---|---|---|---|
| **Amber** | 1.0 : 0.17 : 0 | a warm filament | the radio |
| **Ember** | 1.0 : 0.06 : 0 | a deep orange-red glow; low levels only | asleep |
| **Moon blue** | 0.10 : 0 : 1.0, peak 55 % | a cool, soft blue | a phone is in charge |

The old names still work so old settings and callers never crash: `amber_pale`
and `white` draw amber, `red` draws ember, `magenta` draws moon blue
(`contract.ALIASES`). A palette entry under an old name is ignored.

## Behaviours

Six, and only six. Levels are perceptual (gamma 2.2 is applied after).

| Behaviour | Shape | Says | Code |
|---|---|---|---|
| **Steady** | the level, nothing moves | nothing to do | `Look(motion="solid")` |
| **Breath** | asymmetric: rise 1.3 s ease-out, fall 1.7 s ease-in-out, rest 0.4 s at the trough; period 3.4 s, floor 45 % of the peak. **Slow tempo:** 6 s, floor 40 % | busy - don't unplug | `breathing(colour, peak, slow=)` |
| **Kick** | attack to +40 % of the level (at least 50 % absolute) in 30 ms, then a ~350 ms exponential filament cool-down. The first changed frame is written within one frame (< 25 ms); no slow start | I heard you | reply `kick` |
| **Refuse** | sag to 25 % of the level in 150 ms, hold 200 ms, recover in 600 ms - twice. A single sag for a volume key at its floor or ceiling; three for a missing panel at boot | no / that failed | replies `refuse`, `refuse1`, `refuse3` |
| **Beckon** | two kicks 250 ms apart, then 2.2 s rest, repeating until resolved. Setup: 4 s rest. Identify ("which radio?"): 1 s rest for ~10 s | I need you / it's me | `beckoning(colour, level, rest_s=)`, `identify()` |
| **Swell** | +30 % and back, raised cosine, 1.2 s | something arrived or left | reply `swell` |

## Transitions

Two rules, applied by the engine to every change of Look:

1. **Brightness:** up in 0.6 s (ease-out: under way at once), down in 1.2 s
   with a long exponential tail. Never symmetric.
2. **Hue:** a change between amber/ember and moon blue never fades through
   RGB (that would pass through purple): the lamp falls to 10 % in 0.6 s,
   swaps hue at the bottom, and rises in 0.9 s. Amber and ember are one
   family and fade directly.

A reply that interrupts a reply blends from the frame on show over 70 ms;
anything else that ends off its Look (an identify, a preview, the countdown
released) blends back in 0.3-0.4 s. No colour ever jumps in one frame.

## Levels

| State | Day | Night |
|---|---|---|
| Playing | 80 % (headroom for a kick) | 35 % |
| Ready | 30 % | 20 % |
| Standby ember | 22 % | 15 % (both calibratable: `io.lamp.levels`) |
| Phone casting | 55 % | 30 % |
| Phone paused | 20 % | 20 % |

At night falls, kick cool-downs and swells are x1.5 slower (rises stay
quick), and a beckon is half as tall. Dusk and dawn glide over 1.5 s.

**Brightness** (Settings -> Pilot lamp, `engine.set_brightness`): low 0.6,
normal 1.0, bright 1.25 - a factor on every level, capped at 100 %. It never
takes ember below its night level nor anything lit below 10 %: dark only
means off.

## State map

Highest priority first. Looks come from `lamp/policy.py` (`look_for`), replies
are drawn over whatever Look is on show.

| Moment | Lamp | How |
|---|---|---|
| Shutting down; installing an update | ember, slow breath | Look - busy, don't unplug; held through haslerd's restart |
| Knob held off (5 s countdown to shutdown) | glows from the Look down to the night ember along the countdown; snaps back (0.4 s) on release | `countdown(progress)` |
| Halted | dark - safe to unplug | the halt hook |
| Key press, key combination, same playlist key (next) | **kick** | reply (`ack`, `next` map on) |
| Volume key at its floor or ceiling | **one refuse sag** | reply `refuse1` (`nudge`) |
| A failure: stream down, HA call failed, leader lost, a press while offline | **refuse** | reply `refuse` (`fail`) |
| HA action sent | **kick** | reply (`ha`) |
| Confirm request (the key page wants a press) | **beckon** in the colour on show until a key is pressed, then a kick and steady | Look (`BoxState.confirming`) + reply `confirmed` |
| Setup / hotspot | amber **beckon**, 4 s rest | Look |
| A phone pairs | hue change to moon blue + **swell** | Look + reply `swell` |
| Phone casting (Bluetooth, AirPlay, Spotify Connect) | moon blue steady 55 %; paused: glow down to 20 % | Look |
| A phone wakes the radio from standby | ember -> moon blue hue change | Look |
| Bluetooth pairing window | moon blue **breath** | Look |
| Standby | ember steady | Look |
| Boot | amber breath, the floor rising stage by stage (ember if the knob is off) | Look |
| Loading a preset; joining a group | amber **breath** | Look |
| A radio joins or leaves the group | **swell** | reply (`group` -> `swell`) |
| PU key (next source) | **kick**, then the hue change / glow to where it went | reply (`source:` -> `kick`) + Look |
| Same radio key (stop) | glides down to ready | reply `stop` |
| Playing | amber steady 80 % | Look |
| Ready | amber steady 30 % | Look |
| Sleep timer's last minute | a 20 s glow down to 25 % | Look |
| Offline, idle; panel board missing | **no state of its own** - the next press refuses; a missing panel refuses three times at boot | replies |

**No change at all for:** volume and tone knobs, track changes nobody asked
for, metadata, Home Assistant notices, network blips, offline while a phone
plays.

**Phones are all moon blue**, whatever they use. UPnP/DLNA casting is the
exception for now: upmpdcli plays through MPD and moOde keeps no "UPnP
active" flag, so it reads as the radio (amber).

The owner's **key replies** switch (`set_key_replies(False)`) drops the kick
on key presses; refusals, beckons and the confirm press's kick (`confirmed`)
stay.

## Boot and shutdown

Like a valve radio: light comes first, and sound follows as the valves warm.

| t | Stage | Lamp |
|---|---|---|
| 0-3 s | power, firmware | dark - `gpio=17,22,27=op,dh` in config.txt holds the LED off |
| ~3-5 s | early `hasler-lamp-boot` service | fades up into an amber breath |
| ~10 s | haslerd takes over | the engine writes nothing until its first Look and glides from the boot glow (`seed`) - no blink at the handover; floor 18 % |
| ~15-25 s | network up | peak 50 %, floor 22 % |
| music resumes | playing | rise to amber 80 % with the first sound |
| nothing to resume | ready | settle to amber 30 % |

- **Knob off at power-on:** an ember breath, then steady ember; never amber.
- **No Wi-Fi:** the amber warm-up, then the setup beckon when the hotspot is up.
- **Panel board missing:** three refusals at boot, then the normal looks.
- **Shutdown:** knob held off -> glow down to ember -> slow ember breath
  through the halt -> the shutdown hook drives the pins dark -> **dark =
  safe to unplug**.
- **The keeper stops ticking** (haslerd stuck): after `watchdog(seconds)`
  without `show()`/`feed()` the engine falls back to the slow ember breath on
  its own - busy, don't unplug - and returns to the Look when fed again.

## Calibration

Each radio's LED, resistors and window differ; the calibration page writes
these into `io.lamp` of the variant overlay (`/etc/hasler/variant.d/<name>.yaml`).
Everything is optional; missing values are the defaults.

```yaml
io:
  lamp:
    min_duty: {red: 0.006, green: 0.006, blue: 0.006}   # shortest pulse a lit channel gets
    balance: {red: 1.0, green: 1.0, blue: 1.0}           # per channel, after gamma
    palette:                                             # [r, g, b], or day/night anchors
      amber: {day: [1.0, 0.17, 0.0], night: [1.0, 0.14, 0.0]}
      ember: [1.0, 0.06, 0.0]
      blue: [0.10, 0.0, 1.0]
    level_gain: {amber: 1.0, ember: 1.0, blue: 1.2}      # match perceived brightness
    levels: {ember_day: 0.22, ember_night: 0.15}
    amber_band: [0.12, 0.20]                             # optional, see below
```

- **min_duty:** pwm-gpio can't make a pulse shorter than ~6-10 us at 800 Hz,
  so 0.006 (7.5 us) is the realistic default (it was 0.0005 before 2026-11).
- **palette:** a colour's `night` ratio is exact at level 0.35, `day` at 1.0,
  interpolated over log(level) between (and clamped outside). One `[r, g, b]`
  is both.
- **level_gain:** a perceived-brightness match per colour (blue may need more
  than 1); a channel never goes past 100 %.
- **The minor channels.** Amber's green and blue's red are lit or dark for a
  whole motion, decided at the motion's lowest level (a breath's trough, the
  base under a kick) with hysteresis: lit while that level wants at least
  half its channel's floor, relit at 70 %. A lit minor channel is held at its
  floor rather than vanishing, so **no breath trough ever flicks hue**. Below
  the **amber band** (20 % -> 12 % by default) amber blends into ember-red,
  so a fall to a deep level loses its green with the colour, once. Ready at
  night (20 %) sits on the band's top and stays amber. With default floors
  standby ember is pure red - by design.
- The page's saved colours (`settings.json` `lamp_palette`, `set_palette`)
  lie over the release's palette.

## The engine's API

`lamp/engine.py` `LampEngine` - thread-safe, never blocks the caller:

| Call | What it does |
|---|---|
| `show(look, fade_s=0, cut=False)` | settle into a Look: the standard transition (0), a slow glide (`fade_s`), or a cut |
| `reply(name)` | `kick`, `refuse`, `refuse1`, `refuse3`, `swell`, `stop`, `confirmed`; old words map on (`ack`/`next`/`ha`/`signature:*` kick, `fail` refuses, `nudge` sags once) |
| `countdown(progress or None)` | the knob-off glow; None snaps back |
| `set_night(bool)` | night levels and timing |
| `set_brightness("low"/"normal"/"bright" or factor)`, `brightness` | the owner's brightness |
| `set_key_replies(bool)`, `key_replies` | key-press kicks on/off |
| `identify(seconds=10)`, `identifying` | "which radio?" beckon in the colour on show; 0 ends it |
| `seed(duty_rgb)`, `seed_level(colour, level)` | what the boot glow left; writes nothing |
| `watchdog(seconds or None)`, `feed()`, `tripped` | the busy fallback when the keeper stops |
| `calibrate(io_lamp_dict)`, `calibration()` | apply / read the calibration live |
| `set_palette(overrides)` | the page's saved colours |
| `preview(rgb, level, hold_s)`, `preview_colour(colour, level, hold_s)`, `preview_duty(duty_rgb, hold_s)`, `previewing` | calibration previews: raw ratio, palette colour through the whole pipeline, raw duty |
| `preview_look(meaning, seconds)`, `preview_reply("heard"/"no")`, `stop_preview()` | the lamp page's "show me" |
| `duty_for(colour, level)` | the duty a steady colour gets (no night, no brightness) |
| `close(halt_dark=True)` | stop; dark unless told otherwise |

`policy.meaning_for(state)` names what the lamp says in the page's words
(`playing`, `ready`, `asleep`, `phone`, `busy`, `asking`, `setup`,
`starting`, `pairing`, `offline`).

## For the owner: the page

Settings → **Pilot lamp** speaks in meanings, not colours
(`daemon/haslerd/lamp/meanings.py`), so it survives a change of palette:

| Meaning | Kind | Show me plays |
|---|---|---|
| Playing · Ready · Asleep · A phone is playing · Busy - don't unplug · Asking you to press a key on the radio · Set me up | look (`playing`, `ready`, `asleep`, `phone`, `busy`, `asking`, `setup`) | that look for 5 s |
| I heard you (a key press) · No / something failed | reply (`heard`, `no`) | that reply once |

- **Show me**: `POST /api/lamp/preview {"look": name}` or `{"reply": name}`
  (`{"look": null}` ends it). The engine's `preview_look(name, seconds)` /
  `preview_reply(name)` draw it; an engine without them shows a still colour
  from its palette, or its old reply (`ack`, `fail`, `ha`). The raw
  `{"rgb", "level"}` preview stays for Maker tools.
- **Brightness** low / normal / bright and **key replies** on / off:
  `PUT /api/settings {"lamp": {"brightness", "key_replies"}}`, kept in
  settings.json `lamp` and handed to the engine at start and on every change
  (`set_brightness`, `set_key_replies`). Without those, the LampKeeper scales
  each Look itself (never the ember: asleep must not look dark) and drops the
  key-press replies (`ack`, `next`, `nudge`).
- **Identify**: `POST /api/lamp/identify {"seconds": 1..60}` (10 by default)
  calls the engine's `identify(seconds)` (fallback: warm white for that
  long). `{"peer": id}` asks another Hasler at home (multiroom) to do it, on
  its own `/api/lamp/identify`.
- **In words**: `/api/status` `lamp` is the meaning showing now (the looks
  above, plus `starting`, `pairing`, `offline`; `asking` while the page waits
  for a key press; null without a lamp). The front page shows it as
  *Lamp: asleep*, for owners who can't tell the colours apart.

## How it is built

- The lamp has its **own thread**: it sleeps while steady and renders at 50 Hz
  while anything moves. A slow preset start never blocks it.
- A frame is a mix of the three colours, a perceptual level and an
  *envelope* (the level its colour is judged at); it is blended in that space
  and resolved once: calibrated ratio -> level gain -> gamma 2.2 -> balance ->
  minimum duty.
- PWM: kernel `pwm-gpio` overlays on GPIO 17/22/27 at about 800 Hz, so there is
  no flicker at night. `backend: gpio` (software PWM) is only a fallback, used
  with a warning if the overlays are missing. Never pigpio: its DMA timing uses
  the PCM block that the amp's I2S needs.
- Night = sunset to sunrise for the box's location (computed locally; HA's
  sun entity when connected).

## On the HAT

The Hasler HAT, rev A (ordered October 2026, [hat/BRIEF.md](hat/BRIEF.md);
the design is on the `hat` branch), keeps the prototype board's lamp wiring:
GPIO 17/22/27 through 220/100/100 Ω, common anode on the HAT's
current-limited 3.3 V. Nothing in the software changes. An I²C PWM driver
with FETs and the anode on 5 V (more headroom for green and blue, deep
dimming that stays amber, three GPIOs free) remains an idea for a later
revision.

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

In 0.4.3 (2026-10-21), after an interaction design and LED review: blue lost its green
(green was ~70 % of its light, so it read green) and pale amber its blue;
magenta became the house (another Hasler), as brief signatures only; deep
ember became pure red (no green floor); three new replies - signature, swell,
nudge - for the PU key's destination, a group join/leave, a phone pairing and
a volume key at its end; installing an update became the ember breath (it was
a solid ember, which reads as "asleep").

In 0.4.4 ember became visible again. It had been 6 % (2 % at night), about
0.2 % red duty: invisible behind the window, so standby looked dark. The owner
picked about 3.5 % duty by eye, so ember is now 22 % (15 % at night) and the
shutdown breath 15-35 %.

In November 2026 the lamp got its light language: **3 colours, 6 behaviours, 2
transition rules**. Pale amber, warm white, red and magenta went (their names
map onto the three colours); failure became a motion (refuse), setup a
beckon, another Hasler a swell. Offline and a missing panel stopped being idle
states. Playing dropped from 100 % to 80 % (headroom for the kick), ready from
35 % to 30 %, casting blue from 80 % to 55 %. Hue changes go through a 10 %
trough instead of fading through RGB, key replies became a kick written
within one frame, and calibration grew day/night palettes, level gains and
realistic minimum duties (0.006); the minor-channel snap became a planned
blend (the amber band) so no breath trough flicks hue.
