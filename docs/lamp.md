# The pilot lamp

The pilot lamp is the coloured light in the cabinet's original window. It tells
you what the radio is doing, the way the lamp of a valve radio did, so nobody
needs a manual.

- **Owners:** the short version is in
  [Using your Hasler](using.md#the-pilot-lamp). This page has every detail.
- **Makers and contributors:** this page is the design that the code follows
  (`daemon/haslerd/lamp/`). Change the code and this page together.

**In one breath:** amber is the radio (playing, it glows like an
incandescent filament), ember (a still, deep red glow) means asleep, moon blue means a phone is in charge, green means OK, red means
something is wrong. A breath means busy (don't unplug), a quick brighten
means "heard you", a red eye means "no", two brightens mean "press a key",
Morse means a message (HI, CQ, OK, SOS). Dark means safe to unplug.

The light language v2 (owner-approved, November 2026): **5 colours, 8
behaviours, 2 transition rules**, and a named signal table so a new design is
a change of table, not of code.

## Principles

1. **Amber is home.** Everything the radio does by itself is amber -
   including being busy.
2. **One meaning per colour.** Amber = the radio, ember = asleep, moon blue =
   a phone, green = OK, signal red = something is wrong.
3. **Ember never moves; green and red never stand still.** If the lamp moves,
   it isn't asleep. Green only blooms or sends Morse; red only flashes,
   stares (the HAL eye) or sends SOS.
4. **Brightness says how much is going on.** Ready is dim, playing is bright,
   **dark = safe to unplug** - and nothing else is ever dark.
5. **Motion means working or needs you.** Steady = nothing to do. A breath =
   busy. Kicks, the eye, the bloom, the alert and Morse are replies and
   messages.
6. **Hands get a reply within one frame.** A key press kicks the lamp within
   25 ms. Knobs get nothing - the ear is the feedback.
7. **Respect the dark room.** Sunset to sunrise everything is dimmer and
   slower (x1.5; the red alert x2). Identify stays bright: you asked for it.
8. **Never look frozen, never jump.** No pause over 2 s while busy; no colour
   ever changes in one frame (a Morse mark's 22 ms attack is a deliberate
   edge, not a jump).

## Palette

Five colours. Ratios are linear R : G : B before calibration
(`lamp/engine.py` `DEFAULT_PALETTE`; each radio calibrates its own, see
"Calibration"; `io.lamp.palette` may add more).

| Colour | Linear R : G : B | Level gain | Looks like | Means |
|---|---|---|---|---|
| **Amber** | 1.0 : 0.17 : 0 | 1.0 | a warm filament | the radio |
| **Gold** (amber's playing tint) | 1.0 : 0.22 : 0 | 1.0 | an incandescent bulb, a little yellower | playing (one family with amber) |
| **Ember** | 1.0 : 0.06 : 0, drawn as pure red | 1.0 | a deep red glow; low levels only | asleep (never moves) |
| **Moon blue** | 0.10 : 0 : 1.0 | 1.0 | a cool, soft blue | a phone is in charge |
| **Magic-eye green** | 0 : 1 : 0 | 0.55 | the green of a tuning eye | OK (never steady, never dim) |
| **Signal red** | 1 : 0 : 0 | 1.0 | a clear red | something is wrong (never steady) |

Green's level gain (0.45-0.69 by eye; `io.lamp.level_gain.green`) matches the
bright green die to amber. Ember is drawn with its main channel alone at
every level and brightness, so asleep can never pick up a trace of green and
read as ready. Old names still work: `amber_pale` and `white` draw amber,
`magenta` draws moon blue (`contract.ALIASES`). **`red` is signal red now**
(it used to mean ember).

**Hue families** (what a person tells apart): amber (and gold), red (ember and signal
red - told apart because ember never moves and red never stands still), blue,
green.

## Behaviours

Eight. Levels are perceptual (gamma 2.2 is applied after).

| Behaviour | Shape | Says | Code |
|---|---|---|---|
| **Steady** | the level, nothing moves | nothing to do | `Look(motion="solid")` |
| *Filament* (steady, but alive) | playing only: like an incandescent filament - a tiny, irregular shimmer (+-4 % of the level by day, +-3 % at night; band-passed seeded noise, ~0.1-1 Hz, no rhythm anyone can pick out; repeats every ~8.5 min). The colour is judged at the steady level, so the minor channel never toggles with it | music is playing | `Look(motion="filament")`, signal `playing` |
| **Breath** | amber 22 % -> 70 %: rise 1.1 s ease-out, fall 1.4 s ease-in-out, rest 0.3 s (2.8 s). Night: 18 % -> 35 % asked, drawn 22 % -> 40 % (amber can't go under the band; a breath keeps >= 1.8x, ~3.6:1 in light), 4.2 s; the phase carries over at dusk | busy - shutting down, installing, starting, loading, joining | signal `busy` |
| **Kick** | attack to +40 % of the level (at least 50 %; 36 % at night; from ember only 35 % / 28 %) in 30 ms, then a ~350 ms filament cool-down; first frame < 25 ms | I heard you | reply `kick` |
| **Swell** | +30 % and back, raised cosine, 1.2 s | a radio joined or left the group | reply `swell` |
| **Green bloom** | cross-fade to green (70 %, night 40 %) in 200 ms, hold 300, back to the Look in 500 (night x1.5) | a small OK: setup done, Wi-Fi joined, phone paired, confirm accepted, HA confirmed | reply `bloom` (`confirmed` maps on) |
| **Beckon** | two kicks 250 ms apart to 80 % (night 55 %), then 2.2 s, until resolved | asking you to press a key on the radio | signal `asking` |
| **Refuse** | the **HAL 9000 eye**: cross-fade to red 65 % in 300 ms, hold 700, back in 900 (1.9 s); night 35 %, x1.5. The **sag** (one dip to 25 %, no red) is only for a volume key at its limit | no / that failed: a stream down, a press that needs internet while offline, HA unreachable, pairing failed, a wrong Wi-Fi password | replies `refuse` (`fail`), `refuse1` (`nudge`) |
| **Alert** | Star Trek red alert: 2 x (200 ms red 75 %, 120 ms dip to 10 %) over the Look, every 6 s (night: red 45 %, every 12 s) | no internet - only while awake and idle | signal `no_internet` |
| **Morse** | marks with a crisp 22 ms attack and ~25 ms exponential release, timed on absolute time; unit 130 ms (signals) / 180 ms (persistent codes), dash 3, gaps 1 / 3 / 7; night x1.5 | identify `HI` (green), setup `CQ` (amber), done `OK` (green), fault `SOS` (red) | `contract.code()`, signals below |

## Signals

The designed signatures live in one table, `lamp/signals.py` (`TABLE`,
`get(name)`); the policy and the engine look a signal up when they draw it,
so a remap shows at once. A variant remaps them without code in
`io.lamp.signals` (see the module's doc; a bad entry is logged and skipped).

| Signal | What | Kind |
|---|---|---|
| `playing` | the gold filament, 80 % (night 35 %) | state |
| `busy`, `starting` | the amber breath | state |
| `pairing` | the same breath in moon blue (55 %, night 30 %) | state |
| `setup` | `CQ` in amber, 80 % marks on 25 % (night 45 % on 22 %), 180 ms units, then 3 s; persistent | state |
| `asking` | the beckon | state |
| `no_internet` | the red alert over ready | state |
| `fault` | `SOS` in red, 80 % marks (night 45 %), 150 ms units, then 7 s at a 10 % red glow; persistent, even in standby | state |
| `identify` | `HI` in green at full (night 75 %), gaps at 10 % of the colour on show, ~3 s a repeat, three times; ignores the owner's brightness | overlay |
| `bloom`, `ok` | the green bloom; the bloom then green `OK` once | overlay |
| `no` | the HAL eye | overlay |
| `update_ok` | the bloom and `OK` (geek mode: `update_ok_geek`, the bloom and Close Encounters) | overlay |
| `73` | geek mode: `73` in amber just before the shutdown fade | overlay |
| `close_encounters` | the five notes 55/70/45/25/80 %, 300 ms each, the last 900 ms, amber | overlay |

Overlays are patterns drawn once over whatever shows (`engine.play(name)`).
A pattern's steps are (share of the level, seconds[, colour[, edge]]);
colour `show` is the colour on show, `base` eases back to the Look
underneath. A step to or from moon blue dips to the 10 % trough first (the
hue rule).

**Geek mode** (the owner's setting, off by default; `set_geek(True)`):
identify sends the room's callsign (`set_callsign("k1")`: up to 3 letters or
digits) instead of HI; `73` plays before the shutdown fade
(`engine.farewell()`); an update ends with Close Encounters instead of OK.
Easter eggs: `play_egg("close_encounters" | "tos_working" | None)` (`EGGS`;
TOS "working" is 4-6 random 60-90 ms amber twinkles).

## Transitions

Two rules, applied by the engine to every change of Look:

1. **Brightness:** up in 0.6 s (ease-out: under way at once), down in 1.2 s
   with a long exponential tail. Never symmetric.
2. **Hue:** amber, ember, green and red cross-fade directly. A change to or
   from **moon blue** never fades through RGB (that would pass through
   purple): the lamp falls to 10 % in 0.6 s, swaps hue at the bottom, and
   rises in 0.9 s. Overlays do the same with a 0.3 s dip.

A reply that interrupts a reply blends from the frame on show over 70 ms
(the countdown too); anything else that ends off its Look blends back in
0.3-0.4 s. No colour ever jumps in one frame.

## Levels

Re-derived from the floors measured on the owner's lamp (red 0.03, green
0.045, blue 0.03 duty: 20 % / 24 % / 20 % perceptual) so no meaning sits on
the floor.

| State | Day | Night |
|---|---|---|
| Playing (the filament, +-4 % / +-3 %) | 80 % (headroom for a kick) | 35 % |
| Ready | 45 % | 30 % |
| Standby ember | 20 % | 18 % (both calibratable: `io.lamp.levels`) |
| Phone casting | 55 % | 30 % |
| Phone paused | 20 % | 20 % |

**Brightness** (Settings -> Pilot lamp, `engine.set_brightness`): low 0.6,
normal 1.0, bright 1.25 - a factor on every level. It never pushes a Look
above 80 % (the replies need the headroom), never takes ember below its
night level, anything lit below 17 % or amber under the amber band.

## State map

Highest priority first. Looks come from `lamp/policy.py` (`look_for`),
replies and overlays are drawn over whatever Look is on show.

| Moment | Lamp | How |
|---|---|---|
| Halted | dark - safe to unplug | the halt hook |
| A fault that needs a maker: rollback failed, SD read-only or full, no sound card, panel board missing, overheating | **SOS** in red, persistent, even in standby | Look (`BoxState.fault`, `panel_missing`) |
| Shutting down; installing an update; starting; loading a preset; joining a group | the amber **breath** | Look - busy, don't unplug |
| Knob held off (5 s countdown to shutdown) | glides from the Look down to the night ember; snaps back (0.4 s) on release | `countdown(progress)` |
| Setup needed (hotspot) | **CQ** in amber | Look (`hotspot` or `setup`) |
| Confirm request (the page wants a press) | **beckon** until a key is pressed (amber over ember: asleep never moves), then a green bloom | Look (`confirming`) + reply `confirmed` |
| Identify | **HI** in green, x3 | `identify(seconds)` |
| An update installed | bloom + green **OK** (geek: Close Encounters) | reply `update_ok` (`policy.events_for`) |
| Key press | **kick** | reply (`ack`, `next`) |
| A small OK: setup done, Wi-Fi joined, phone paired, confirm accepted, HA confirmed | **green bloom** (setup done: bloom + OK) | reply `bloom` / `ok` |
| A failure: stream down, press needing internet while offline, HA unreachable, pairing failed, wrong Wi-Fi password | the **HAL eye** | reply `refuse` (`fail`) |
| Volume key at its floor or ceiling | **one sag**, no red | reply `refuse1` (`nudge`) |
| No internet, awake and idle (never while music or a phone plays, never in standby) | the **red alert** over ready | Look (`offline_s` >= 60 s or `offline`) |
| A radio joins or leaves the group | **swell** | reply (`group` -> `swell`) |
| Bluetooth pairing window | the blue **breath** | Look |
| Phone casting (Bluetooth, AirPlay, Spotify Connect) | moon blue 55 %; paused 20 % | Look |
| Standby | ember, still | Look |
| Playing; following a group that plays | the gold **filament**, 80 % | Look (signal `playing`) |
| Ready | amber 45 % | Look |
| Sleep timer's last minute | a 20 s glow down to 25 % | Look |

**No change at all for:** volume and tone knobs, track changes nobody asked
for, metadata, Home Assistant notices, network blips, offline while a phone
plays.

**Phones are all moon blue**, whatever they use. UPnP/DLNA casting is the
exception for now: upmpdcli plays through MPD and moOde keeps no "UPnP
active" flag, so it reads as the radio (amber).

The owner's **key replies** switch (`set_key_replies(False)`) drops the kick
on key presses; the eye, the bloom and the beckon stay.

## Telling them apart

**The contrast rule:** any two meanings differ in at least two of {hue
family, motion class (still / smooth / sharp / coded), tempo >= 2x, level or
swing >= 3:1 in light}, at least one of them not hue; every moving look
swings >= 3:1 in light. `tests/test_lamp_signatures.py` renders every
meaning through the whole pipeline on a fake clock, day and night, with the
default floors and with the measured ones, and checks it
(`python -m tests.test_lamp_signatures` prints the table).

By day, default floors (luminance after gamma, rough photopic weights):

| Meaning | Hue | Motion | Tempo | Mean light | Swing |
|---|---|---|---|---|---|
| playing | amber (gold) | filament | none | 0.264 | 1.2:1 |
| ready | amber | still | - | 0.069 | 1:1 |
| asleep | red | still | - | 0.009 | 1:1 |
| phone | blue | still | - | 0.038 | 1:1 |
| phone paused | blue | still | - | 0.003 | 1:1 |
| busy | amber | smooth | 2.8 s | 0.096 | 13:1 |
| asking | amber | sharp | 2.8 s | 0.080 | 3.5:1 |
| setup (CQ) | amber | coded | 7.9 s | 0.117 | 13:1 |
| no internet | amber + red | sharp | 6.0 s | 0.073 | 84:1 |
| fault (SOS) | red | coded | 11.1 s | 0.048 | 97:1 |
| pairing | blue | smooth | 2.8 s | 0.015 | 13:1 |
| identify (HI) | green | coded | 3.0 s | 0.046 | 85:1 |

At night the tempos stretch (breath 4.2 s, CQ 11.8 s, alert 12 s, SOS
16.6 s, HI 4.5 s) and the swings shrink (breath 3.6:1, asking 3.8:1, CQ
4.8:1, the filament 1.1:1).

The filament is the one calm motion: it is exempt from ">= 3:1", must show
no rhythm (no autocorrelation peak), and is pinned apart from ready (steady,
80 % vs 45 %: 3.8:1 in light by day) and busy (swing 1.2:1 vs 13:1, no
tempo vs 2.8 s).

**Pairs that rest on one dimension (for the owner to decide):**

- *Brightness is the message:* a phone playing / paused differ by level
  alone (>= 3:1 by day, 2.4:1 at night). **At night playing 35 % and ready
  30 % are nearly the same light (1.4:1)**; only the filament's shimmer
  tells them apart - the levels the owner picked could (e.g. ready 22 % at
  night).
- *Colour is the message:* ready / a phone, asleep / a phone (paused): two
  still states of different hue at similar levels. A colour-blind owner can't
  tell them apart from the lamp; the page says it in words.
- *The spec's own timings:* busy (breath, 2.8 s) and asking (two kicks +
  2.2 s = 2.83 s) share a tempo and a colour; by day the breath's 13:1 swing
  adds "level", at night it doesn't.
- *Measured floors:* with a 4.5 % green floor amber can't be dim *and* amber.
  The engine holds amber's green at its floor (a little yellow, g/r up to
  0.6) down to night ready, so amber stays amber; but at night that green
  pedestal flattens amber motions (the breath 2.0:1, the beckon 2.2:1 in
  light), CQ's 25 % base turns red like the alert's flashes by day, and the
  beckon (3.1:1) sits close to the filament by day. A
  driver with FETs and the anode on 5 V (see "On the HAT") would fix the
  hardware side.

## Boot and shutdown

Like a valve radio: light comes first, and sound follows as the valves warm.

| t | Stage | Lamp |
|---|---|---|
| 0-3 s | power, firmware | dark - `gpio=17,22,27=op,dh` in config.txt holds the LED off |
| ~3-5 s | early `hasler-lamp-boot` service | fades up into the amber breath (22-70 %, 2.8 s) |
| ~10 s | haslerd takes over | the engine writes nothing until its first Look and glides from the boot glow (`seed`) - no blink at the handover |
| music resumes | playing | rise to the gold filament (80 %) with the first sound |
| nothing to resume | ready | settle to amber 45 % |

- **Knob off at power-on:** starting is busy: the amber breath, then the
  still ember.
- **No Wi-Fi:** the amber warm-up, then CQ when the hotspot is up.
- **Panel board missing:** the HAL eye once at boot, then SOS.
- **Shutdown:** knob held off -> glide down to ember -> (geek mode: `73`) ->
  the amber breath through the halt -> the shutdown hook drives the pins dark
  -> **dark = safe to unplug**.
- **Between two haslerds** (a restart, an update): the boot script breathes
  the same amber breath (`lamp/handover.py`; the note's mode is still called
  "ember" for old haslerds).
- **The keeper stops ticking** (haslerd stuck): after `watchdog(seconds)`
  without `show()`/`feed()` the engine falls back to the busy breath on its
  own and returns to the Look when fed again.

## Calibration

Each radio's LED, resistors and window differ; the calibration page writes
these into `io.lamp` of the variant overlay (`/etc/hasler/variant.d/<name>.yaml`).
Everything is optional; missing values are the defaults.

```yaml
io:
  lamp:
    min_duty: {red: 0.03, green: 0.045, blue: 0.03}      # shortest pulse a lit channel gets
    balance: {red: 1.0, green: 1.0, blue: 1.0}           # per channel, after gamma
    palette:                                             # [r, g, b], day/night anchors, or {rgb, family}
      amber: {day: [1.0, 0.17, 0.0], night: [1.0, 0.14, 0.0]}
      ember: [1.0, 0.06, 0.0]
      blue: [0.10, 0.0, 1.0]
      green: [0.0, 1.0, 0.0]
      red: [1.0, 0.0, 0.0]
      violet: {rgb: [0.6, 0.0, 1.0], family: blue}      # a new colour: any unused name
    level_gain: {amber: 1.0, ember: 1.0, blue: 1.2, green: 0.55, red: 1.0}
    levels: {ember_day: 0.20, ember_night: 0.18}
    amber_band: [0.12, 0.20]                             # optional, see below
    signals: {}                                          # optional remaps, lamp/signals.py
```

- **min_duty:** pwm-gpio can't make a pulse shorter than ~6-10 us at 800 Hz,
  so 0.006 is the default; the owner's frosted LED on 3.3 V measured 0.03 /
  0.045 / 0.03.
- **palette:** a colour's `night` ratio is exact at level 0.35, `day` at 1.0,
  interpolated over log(level) between. A name the release doesn't use is a
  new colour (its own fade family unless `family` says otherwise; old names
  are ignored).
- **level_gain:** a perceived-brightness match per colour.
- **The minor channels.** A minor channel (amber's green, blue's red) is lit
  or dark for a whole motion, decided at the motion's lowest level with
  hysteresis, and a lit one is held at its floor rather than vanishing. It
  only switches while it is within 1.5x of its floor (never more than a
  floor's step); one that must come on higher fades in over 0.4 s. Amber's
  green is held down to a quarter of what it wants (with a high floor, night
  amber stays amber, a little yellow). Below the **amber band** (12-20 % by
  default, raised to where amber's green reaches a quarter of its floor)
  amber blends into ember-red, once; amber Looks are never drawn below the
  band (up to 50 % by day / 30 % at night with high floors). A breath's
  trough stays above the main channel's floor and its peak >= 1.7x the trough
  (~3:1 in light).
- The page's saved colours (`settings.json` `lamp_palette`, `set_palette`)
  lie over the release's palette.

## The engine's API

`lamp/engine.py` `LampEngine` - thread-safe, never blocks the caller:

| Call | What it does |
|---|---|
| `show(look, fade_s=0, cut=False)` | settle into a Look: the standard transition (0), a slow glide (`fade_s`), or a cut |
| `reply(name)` | `kick`, `refuse` (HAL eye), `refuse1` (sag), `swell`, `stop`, `bloom`, `ok`, `update_ok`; old words map on (`ack`/`next`/`ha`/`signature:*` kick, `fail` refuses, `nudge` sags, `confirmed` blooms) |
| `play(look_or_signal_name, times=1)` | a pattern overlay once (Morse, a remapped signal) |
| `countdown(progress or None)` | the knob-off glide; None snaps back |
| `set_night(bool)` | night levels and timing |
| `set_brightness("low"/"normal"/"bright" or factor)`, `brightness` | the owner's brightness |
| `set_key_replies(bool)`, `key_replies` | key-press kicks on/off |
| `identify(seconds=9)`, `identifying` | HI (geek: the callsign) as many ~3 s repeats as fit; 0 ends it |
| `set_geek(bool)`, `geek`, `set_callsign(str)`, `callsign` | geek mode |
| `farewell()` | `73` before the shutdown fade (geek mode only; False otherwise) |
| `play_egg(name or None)`, `EGGS` | an Easter egg |
| `seed(duty_rgb)`, `seed_level(colour, level)` | what the boot glow left; writes nothing |
| `watchdog(seconds or None)`, `feed()`, `tripped` | the busy fallback when the keeper stops |
| `calibrate(io_lamp_dict)`, `calibration()`, `calibration_defaults()` | apply / read the calibration live |
| `set_palette(overrides)` | the page's saved colours |
| `preview(rgb, level, hold_s)`, `preview_colour(colour, level, hold_s)`, `preview_duty(duty_rgb, hold_s)`, `previewing` | calibration previews |
| `preview_look(meaning, seconds)`, `preview_reply(name)`, `stop_preview()` | the lamp page's "Show me": looks `playing`, `ready`, `asleep`, `phone`, `busy`, `starting`, `pairing`, `asking`, `setup`, `no_internet`, `fault`; replies `heard`, `no`, `ok`, `hello` |
| `duty_for(colour, level)` | the duty a steady colour gets (no night, no brightness) |
| `close(halt_dark=True)` | stop; dark unless told otherwise |

`policy.meaning_for(state)` names what the lamp says in the page's words
(`playing`, `ready`, `asleep`, `phone`, `busy`, `asking`, `setup`,
`starting`, `pairing`, `no_internet`, `fault`, `off`);
`policy.events_for(before, now)` turns `BoxState.update_ok` into the one-shot
`update_ok` reply. `BoxState` flags for the keeper: `fault`, `setup` (or
`hotspot`), `offline` (or `offline_s`), `update_ok`.

## For the owner: the page

Settings → **Pilot lamp** speaks in meanings, not colours
(`daemon/haslerd/lamp/meanings.py`), so it survives a change of palette. Its
top line says what the lamp means now (*Now: asleep*), for owners who can't
tell the colours apart.

| Meaning | Kind | Show me plays |
|---|---|---|
| Playing · Ready · Asleep · A phone is playing · Busy · Starting · Pairing · Asking you to press a key · Set me up · No internet · Needs its maker | look (`playing`, `ready`, `asleep`, `phone`, `busy`, `starting`, `pairing`, `asking`, `setup`, `no_internet`, `fault`) | that look for 8 s |
| I heard you · No / something failed · OK · Hello (identify) | reply (`heard`, `no`, `ok`, `hello`) | that reply once |

- **Show me**: `POST /api/lamp/preview {"look": name}` or `{"reply": name}`
  (`{"look": null}` ends it).
- **Brightness** low / normal / bright and **key replies** on / off:
  `PUT /api/settings {"lamp": {"brightness", "key_replies"}}`.
- **Identify**: `POST /api/lamp/identify {"seconds": 1..60}` calls
  `identify(seconds)`; `{"peer": id}` asks another Hasler at home.
- **In words**: `/api/status` `lamp` is the meaning showing now (null without
  a lamp).

## How it is built

- The lamp has its **own thread**: it sleeps while steady and renders at 50 Hz
  while anything moves. A slow preset start never blocks it.
- A frame is a mix of the palette's colours, a perceptual level and an
  *envelope* (the level its colour is judged at); it is blended in that space
  and resolved once: calibrated ratio -> level gain -> gamma 2.2 -> balance ->
  minimum duty. Patterns are functions of absolute time, so an edge never
  lands on a frame boundary by accident.
- PWM: kernel `pwm-gpio` overlays on GPIO 17/22/27 at about 800 Hz, so there is
  no flicker at night. `backend: gpio` (software PWM) is only a fallback.
  Never pigpio: its DMA timing uses the PCM block that the amp's I2S needs.
- Night = sunset to sunrise for the box's location, computed locally
  (lamp/night.py: NOAA's equations, DST-aware, the poles handled) from the
  Settings page's location - a city, exact coordinates or the phone's. Nothing
  is asked of Home Assistant. Without a location the radio assumes Madrid
  until someone sets one; the Pilot lamp page nudges.

## On the HAT

The Hasler HAT, rev A (ordered October 2026, [hat/BRIEF.md](hat/BRIEF.md);
the design is on the `hat` branch), keeps the prototype board's lamp wiring:
GPIO 17/22/27 through 220/100/100 Ω, common anode on the HAT's
current-limited 3.3 V. Nothing in the software changes. An I²C PWM driver
with FETs and the anode on 5 V (more headroom for green and blue, deep
dimming that stays amber, three GPIOs free) remains an idea for a later
revision - the measured floors above are the strongest argument for it.

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

Later in November 2026, after the owner watched every "Show me" on the real
lamp ("identify is not obvious", "asleep and busy are too alike", "set me
up is not obvious") and measured the real floors (red 3 %, green 4.5 %, blue
3 % duty), the **light language v2**: 5 colours (magic-eye green for OK,
signal red for faults; `red` stopped meaning ember), 8 behaviours (the green
bloom, the HAL eye, the red alert and Morse joined), busy became one amber
breath (never ember: asleep never moves), identify became HI in green Morse,
setup CQ, a fault SOS, ready rose to 45 % / 30 %, ember to 20 % / 18 %, and
playing became an incandescent filament (gold, a tiny irregular shimmer). The
designed signals moved into a named table (`lamp/signals.py`), the palette
took new colours from config, and a rendered contrast test
(`tests/test_lamp_signatures.py`) keeps every pair apart. The engine also
stopped drawing amber ember-red at low levels, snapping minor channels in
one frame, and spending the replies' headroom on "bright"; and gained geek
mode (callsign, 73, Close Encounters, eggs).
