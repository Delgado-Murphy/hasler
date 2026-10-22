# Hardware

This page is for makers. It lists the parts, shows how they are wired, and
explains the two places where a mistake can destroy something. The steps to put
it all together are in the [build guide](build-guide.md).

## At a glance

- **Brain:** a Raspberry Pi 4B running moOde.
- **Sound:** a HiFiBerry Amp4 Pro on top of the Pi drives the original 16 Ω
  speaker. It also powers the Pi.
- **Power:** a 65 W USB-C charger and a small "PD trigger" board that asks the
  charger for 20 V.
- **Panel:** an MCP23017 chip reads the keys; an ADS1115 chip reads the two
  knobs. Both talk to the Pi over I²C.
- **Pilot lamp:** one RGB LED on three Pi pins.
- **Never:** more than 24 V into the amp, or the speaker across both amp channels.

This page describes the prototype build: breakout boards and a hand-wired
prototype board ([prototype/README.md](prototype/README.md)). The **Hasler
HAT** (rev A, ordered October 2026; [brief](hat/BRIEF.md), [design](hat/DESIGN.md),
[ordering and bring-up](../hardware/hat/ORDERING.md)) puts the 20 V power, its
protection, the key and knob chips and the lamp resistors on one board, with
the same chips. Only the panel's wiring to them differs: a cabinet on the HAT
uses its own descriptor (`config/variants/hasler-hm-hat.yaml`).

---

## Parts we already had

| Item | Role | Note |
|---|---|---|
| **Raspberry Pi 4B** | the brain | **Use this one.** HiFiBerry rate the Amp4 Pro's 5 V supply as good "up to the Pi 4B", so a Pi 5 is over its budget. The 4B also runs cooler in a closed wooden box. The 3B+ has weaker Wi-Fi and Bluetooth. |
| **HiFiBerry Amp4 Pro** | DAC + amp, about 2 × 30 W | Hardware ALSA mixer: this is why one volume knob controls radio, Bluetooth, AirPlay and Spotify alike. Powers the Pi from its own 5 V. |
| **HW-398 PD trigger** | 20 V from a USB-C charger | Fastsoc FS312. Bridge the **20 V** pad only. |
| **RGB LED, 4 legs, common anode** | pilot lamp | Pi header pins 11 / 15 / 13 (GPIO 17 red / 22 green / 27 blue), common to 3.3 V, three resistors. See [Pilot lamp](#pilot-lamp--rgb-led-resistors-on-the-prototype-board). |
| Hasler HM cabinet | keys, knobs, speaker | 7 momentary keys, volume and tone pots, elliptical speaker. |

## Buy now — Stage 1, makes it play

| Item | Qty | Why |
|---|---|---|
| **USB-C PD charger, 65 W GaN** | 1 | **Must offer 20 V** (a "20 V PDO"). Many 30–45 W chargers stop at 15 V. 65 W gives 20 V / 3.25 A, plenty. |
| USB-C cable | 1 | Any good cable rated 3 A. Above that you need an e-marked cable. |
| **MCP23017 board** (CQRobot, or the green breakout) | 1 | Reads the keys over I²C, so the panel wiring is 4 wires to the Pi. The default in `hasler-hm.yaml`. See [Wiring quick reference](#wiring-quick-reference). |
| **microSD card, 32 GB A1/A2** | 1 | For moOde. An "endurance" card is worth it in a box that is always on. |
| **Multimeter** | 1 | Phase 0 is all measuring. It is also the only safe way to check the power. |
| Silicone hookup wire, 26–28 AWG stranded | ~5 m | Panel wiring: 7 keys, end switch and common ground. Stranded, because solid wire breaks where a lid opens. |
| **100 nF ceramic capacitors** | 10 | One across each key, to calm contact bounce. Skip them and you see bounce in `panel-probe`. |
| Dupont / JST-XH connectors | 1 set | So the panel unplugs from the Pi. You will open this box more than once. |
| Small heatsink set for Pi 4B | 1 | Closed cabinet, no airflow. |
| Contact cleaner (DeoxIT F5 or similar) | 1 | 60-year-old carbon tracks and key contacts. Clean them *before* you decide they are broken. |

## Protection (about €2 in parts)

Cheap insurance against a fault that would be miserable to find inside a closed
cabinet.

> **With the Hasler HAT** skip this section: the HAT's eFuse, TVS and USB-C PD
> input replace it, and an extra 220–470 µF at the amp would only add to the
> eFuse's start-up heating ([hat/DESIGN.md](hat/DESIGN.md), Power).

| Item | Qty | Why |
|---|---|---|
| **220–470 µF / 35 V electrolytic capacitor** | 1 | Across the **Amp4 Pro's DC input**, at the amp end. This is the main protection: it absorbs plug and unplug spikes. |
| **SMAJ22A or SMBJ22A TVS diode** | 1 | Clamps fast spikes the capacitor misses. How to choose: below. |
| NTC inrush limiter (e.g. 5 Ω) | 1 | *Only if* the charger trips when you connect the amp. Buy it; fit it only if needed. |

**The limit is the Amp4 Pro's absolute maximum: 24 V.** A 22 V TVS starts to
conduct at 24.4–26.9 V, slightly *above* that limit. So it only catches fast
spikes, and the capacitor does the real work. Still fit one, at the amp input.
Wiring: Paper sheet H2 (Power).

### Choosing the TVS — the number means different things in different families

A 20 V USB-C supply can sit at up to **21 V**. So the TVS needs a **standoff
voltage (V_RWM) of at least 22 V**, one-way (unidirectional). The trap: two
naming systems are in use.

| Family | The number in the name is... | So for a 20 V rail |
|---|---|---|
| **SMAJ / SMBJ / SMCJ** `xxA` | the **standoff** voltage V_RWM | pick **22** (or 24) |
| **P6SMB / 1.5KE / P6KE** `xxA` | the **breakdown** voltage V_BR (standoff ≈ 0.85 ×) | pick **27** (or 30) |

Get this backwards and you fit a P6SMB24A: 24 V *breakdown* but only **20.5 V
standoff**. It conducts all the time on your own rail, gets hot and pulls the
voltage down.

**SMAJ (400 W, SMA package) and SMBJ (600 W, SMB package) both work.** Both are
numbered by standoff. SMBJ is bigger, which makes it a little easier to
hand-solder.

| Part | Standoff V_RWM | Breakdown V_BR | Clamp V_C @ I_PP |
|---|---|---|---|
| SMAJ22A | 22.0 V | 24.40 – 26.90 V | 35.5 V @ 11.3 A |
| SMAJ24A | 24.0 V | 26.70 – 29.50 V | 38.9 V @ 10.3 A |
| SMBJ22A | 22.0 V | 24.40 – 26.90 V | 35.5 V @ 16.9 A |
| SMBJ24A / **24CA** | 24.0 V | 26.70 – 29.50 V | 38.9 V @ 15.4 A |

The rated clamp voltage is for a large standard test pulse. The spikes in this
box (a short wire, a few amps) are much smaller, so in practice each part clamps
near its **breakdown** voltage: about 24–27 V for a 22, about 27–30 V for a 24.
Both are above the amp's 24 V, so **the lower one, a 22, protects better**. It
leaves 1 V of headroom over the 21 V worst case.

**Check with the meter** during the unloaded test in the build guide:

- rail measures **≤ 20.5 V → fit a 22** (the normal case);
- rail measures **> 20.5 V → a 24**, and consider a charger that runs closer to 20 V.

| Standoff | Verdict |
|---|---|
| 18, 20 | ❌ conducts all the time on your own rail: heats, pulls the voltage down |
| **22** | ✅ the right choice (V_BR 24.4 V) |
| 24 | ⚠️ only if the rail runs high (V_BR 26.7 V, later clamping) |
| 26+ | ❌ breaks down far above the amp's 24 V: too late to help |

### `A` or `CA` — one-way or two-way

`A` = **unidirectional** (one-way), `CA` = **bidirectional** (two-way). The
standoff is the same; they are different parts.

**One-way is the textbook choice for a DC rail.** It clamps high spikes, and it
also clamps negative spikes to about −0.7 V. A two-way part clamps both
directions at the same level, so it loses that second benefit.

**But `CA` works well here**, and it cannot be fitted wrong:

| | One-way `A` | Two-way `CA` |
|---|---|---|
| Clamps over-voltage | ✅ | ✅ |
| Clamps negative spikes to −0.7 V | ✅ | ✗ |
| Can be fitted backwards | **⚠️ yes, see below** | ✗ nothing to get wrong |

**⚠️ Orientation, if you use an `A` part:** the **end with the band (cathode)
goes to plus**, the other end to ground. Fitted the other way, it conducts at
0.7 V: a dead short across a 20 V supply. A `CA` part has no such risk.

So: use an `A` if you have a 22A and you are sure about the band. If an
SMBJ24CA is what you have, it is fine for this job and it cannot go in wrong.

### Other parts, and what will not do

Other families follow the same rule (remember the naming above): SMCJ22A
(1500 W), P6SMB27A, or 1.5KE27A. The last is a through-hole part with wire legs,
the easiest of all to solder onto the PD board's output pads.

**An ordinary diode is not a substitute.** A rectifier (1N4007, 1N5819)
reverse-biased across the rail does nothing until it fails. A Zener does clamp,
but a 1 W 1N47xx handles a tiny fraction of a TVS's surge and reacts much more
slowly. A 5 W Zener (1N5359B, 24 V) is a stopgap, not an equal.

**If the right part is not to hand,** in order of preference: a 22 V TVS → the
capacitor alone with short wires (it does most of the work anyway) → a 5 W
Zener → nothing. An ordinary rectifier belongs nowhere on this list.

**After fitting, check it:** with 20 V applied, the TVS should draw microamps
and stay **cold**. If it is even warm, the standoff is too low: wrong part, take
it off.

**If you cannot get one at all,** fit the capacitor and keep the wires short.
The TVS is extra safety, not a safety-critical part. The box runs happily
without it, and you can add it later.

## Buy now — Phase 3b, wakes the knobs

| Item | Qty | Why |
|---|---|---|
| **ADS1115 module** | 1 | The Pi has no analog input. Without it the knobs do nothing (keys 5 and 6 held still change the volume). |
| 100 nF ceramic | 2 | Wiper to ground on each pot. **Needed if the pots are high value (about 1 MΩ):** the ADS1115's input then needs the capacitor's charge, or readings sag and wander. This cabinet's pots are 53 kΩ and 600 Ω, so it matters less here. |

## Optional / later

| Item | Why |
|---|---|
| Panel-mount USB-C socket | The HW-398 ends in a *male plug*. This gives the cabinet a modern USB-C inlet and keeps the DC wires short. Check its current rating. |
| **USB Bluetooth dongle** | The Pi's own chip shares one radio between Wi-Fi and Bluetooth, and Bluetooth audio can stutter. Ethernet + built-in Bluetooth is better; the dongle is the fallback. |
| DPDT relay module | Internal / external speaker switching, for the Excelsior. |
| Speaker binding posts | External speaker terminals. |
| 4 Ω elliptical speaker | Only if the original ever dies. It measured as a healthy 16 Ω voice coil. |

## The speaker — measured

**14.2 Ω DC resistance → 16 Ω nominal**, voice coil driven directly, no output
transformer (a transformer would read under 1 Ω). It runs straight from the
Amp4 Pro.

**The impedance is fine.** 16 Ω is above HiFiBerry's stated 4–8 Ω, but that is
the *safe* direction: a higher load draws *less* current. 4 Ω is the hard lower
limit; the top of the range is only where full power stops being guaranteed.
(A class-D output filter is tuned for a nominal load, so 16 Ω can give a small
peak around 20–40 kHz. Nobody can hear that on a cone with about 5 kHz of
range.)

**Power is the real risk.** Into 16 Ω the Amp4 Pro gives about half its 8 Ω
figure, about **10 W**. A vintage cone like this is good for about **1–3 W**.
That is five to ten times more amplifier than the speaker can survive, and the
speaker cannot be replaced with anything original.

The fix is the **volume ceiling** (key page → Settings → Sound → Volume ceiling; stored
as `volume_limit`). It caps the hardware mixer but keeps the full turn and
resolution of the knob. Set it by ear: the steps are in the
[build guide, Phase 3a](build-guide.md#phase-3a--keys-drive-the-music).
The knob effects add no power: whatever a knob lifts, the DSP turns the
whole signal down by at least as much ([sound.md](sound.md)), so under the
ceiling no frequency ever gets more than it does with the knobs flat.

**⚠️ Never wire the speaker across the two amp channels.** Class-D outputs are
already bridged inside; connecting across two of them is a dead short that
destroys the amplifier. Use one channel only (L+ / L−). The key page's **Mono**
setting (on by default) mixes left and right into it, so nothing is lost.

## Wiring quick reference

### I²C board

Keys go through an **MCP23017** (the default in `hasler-hm.yaml`). Two boards
work, with the same chip; only the connector and the default address differ.
The descriptor says `addr: auto`, so either is found without an edit.

**CQRobot MCP23017 board** (black, our choice). Its 6-pin JST cable *and* the
6-pin header opposite carry the same six signals, so the board also works as an
I²C hub: Pi → JST cable, right-hand header → ADS1115.

| Signal | Cable colour* | Pi (via the Amp4 Pro's top header) | Note |
|---|---|---|---|
| VCC | red | 3.3 V, pin 1 | The board runs at 3.3–5 V; **3.3 V** keeps SDA/SCL at the Pi's level. |
| GND | black | GND, pin 6 | |
| SDA | green | pin 3 (GPIO 2) | Shared with the Amp4 Pro's amp chip (0x4d). |
| SCL | blue | pin 5 (GPIO 3) | |
| INTA | yellow | — | Leave unconnected; `haslerd` polls. |
| INTB | orange | — | Leave unconnected. |

\* Colours as supplied. Trust the printing on the back of the board, not the colour.
**Address `0x27`** with the A0–A2 pads open. Bridging pads lowers it; `0x20` = all
bridged. The PA and PB headers each have their own **GND** and **VCC** pins: use
the PA header's GND as the keys' common ground.

**Green breakout** (alternative). Four wires from its top header:

| Board | Pi | Note |
|---|---|---|
| VCC | 3.3 V (pin 1 or 17) | **Not 5 V.** The board's I²C pull-ups go to VCC, and the Pi's pins take 3.3 V only. |
| GND | GND (pin 9) | The keys' common ground lands here too. |
| SDA | pin 3 (GPIO 2) | Shared bus with the Amp4 Pro (0x4d). |
| SCL | pin 5 (GPIO 3) | |
| RST | — | Must sit at VCC. Measure RST ↔ VCC; if open, bridge it, or the chip stays in reset. |
| ITA / ITB | — | Leave open; `haslerd` polls. |
| A0–A2 | — | To GND → address `0x20`. |

### Keys

Each key is a logical pin: `d0`–`d7` = PA0–PA7, `d8`–`d15` = PB0–PB7. Which key
sits on which pin is set in `config/variants/hasler-hm.yaml` under `controls:`,
so you can wire in whatever order suits your board and change one line per key.

The shipped descriptor follows our **prototype board**
([prototype/README.md](prototype/README.md)), where each key lands on the pin its
cabinet wire reaches:

| Key | Logical | Board pin | | Key | Logical | Board pin |
|---|---|---|---|---|---|---|
| Key 1 | d4 | PA4 | | Key 5 | d6 | PA6 |
| Key 2 | d3 | PA3 | | Key 6 | d1 | PA1 |
| Key 3 | d5 | PA5 | | PU | d0 | PA0 |
| Key 4 | d2 | PA2 | | Tone end switch | d8 | **PB0** |

If you wire your own board in plain order (key 1 → PA0 ... key 6 → PA5, PU → PA6,
end switch → PB0), change the `pin:` values to match. `panel-probe.py` shows the
pin each row expects.

**⚠️ PA7 and PB7 are output-only** on current MCP23017 chips (Microchip
datasheet rev. D). A switch there may never read. The service refuses `d7` and
`d15` at start, which is why the end switch skips to PB0.

**Fallback: keys straight to Pi GPIO.** This avoids the Amp4 Pro's pins (2, 3, 4,
18–21), the HAT EEPROM (0, 1), the UART (14, 15) and the lamp (17, 22, 27). It is
commented out in `hasler-hm.yaml`. Its `pins:` list gives each logical pin a
GPIO (`d7` is a placeholder, so `d8` lands on GPIO 25); the keys keep their
`controls:` pins, so with the shipped file:

| Logical | GPIO | Key | | Logical | GPIO | Key |
|---|---|---|---|---|---|---|
| d0 | 5 | PU | | d4 | 16 | Key 1 |
| d1 | 6 | Key 6 | | d5 | 23 | Key 3 |
| d2 | 12 | Key 4 | | d6 | 24 | Key 5 |
| d3 | 13 | Key 2 | | d8 | 25 | tone end switch |

Easier: wire the keys to any of those GPIOs in whatever order is convenient
and let `scripts/panel-probe.py --identify` rewrite the `pins:` list for you.

All keys go to a common ground, with pull-ups turned on in software and 100 nF
across each.

### Knobs

The **ADS1115** sits at I²C address `0x48` (ADDR to GND). Each pot is a divider
across 3.3 V and ground:

| ADS1115 input | Pot | Descriptor |
|---|---|---|
| A0 | tone wiper | `tone`, `channel: 0` |
| A1 | volume wiper | `volume`, `channel: 1` |
| A2, A3 | spare | |

## Pilot lamp — RGB LED, resistors on the prototype board

The prototype board's 7-wire ribbon uses only the Pi header's **odd row** (Paper
PB1): pins 1 3.3 V · 3 SDA · 5 SCL · *7 empty (amp mute)* · 9 GND · 11 · 13 · 15
= lamp. Nothing lands on the even row, which carries 5 V. The lamp is driven by
the kernel's `pwm-gpio` driver, one PWM chip per pin at about 800 Hz (a
config.txt block put in place by `scripts/install.sh` or by the release). What the
lamp shows is in [lamp.md](lamp.md).

| Header pin | Pi | LED leg | Series resistor (on the board) |
|---|---|---|---|
| 11 | GPIO 17 | red | **220 Ω** |
| 15 | GPIO 22 | green | **100 Ω** |
| 13 | GPIO 27 | blue | **100 Ω** |
| — | board 3.3 V rail | common (**common anode**, long leg) | — |

- **Common anode** (measured: in diode mode, with the red probe on the long leg,
  the colours glow). So a GPIO pin **LOW lights** its colour and HIGH is dark.
  `gpio=17,22,27=op,dh` in config.txt holds all three HIGH from the moment the
  firmware starts. Without it, the reset pull-downs leave a faint glow for the
  first second or so.
- Dark = safe to unplug: a shutdown hook (`hasler-lamp-halt`) drives the pins
  HIGH at halt.
- **Resistors go on the colour legs, never on the common.** They keep each pin at
  about 4–6 mA at 3.3 V, well within what a GPIO pin can sink (red drops about
  2.0 V, green and blue about 2.9 V). If it is too dim or one colour wins,
  adjust the colours by eye on the key page (Settings → Maker tools → *Lamp
  colours*), or `palette:` / `balance:` in the descriptor, before you change
  parts.
- The pots' 3.3 V and GND come from the board's rails too, not straight from
  the Pi.

**I²C addresses:** `0x27` MCP23017 (CQRobot; `0x20` green board) · `0x48`
ADS1115 · `0x4d` the Amp4 Pro's amp chip (shows `UU` once its driver loads;
leave it alone).

## The two steps that can destroy something

1. **More than 24 V into the Amp4 Pro**, or the speaker wired **across two
   channels**. Measure the PD trigger with nothing connected before it ever
   touches the amp (20 V ±5 %; the steps are in the
   [build guide's power bring-up](build-guide.md#power-bring-up)), and put the speaker on L+ / L− only.
2. **The original mains transformer and wiring come out completely**, and the
   old capacitors are discharged before they go in the bin.

Everything else can be fixed.

---

## History

- **HiFiBerry Power Controller v1.1: dropped.** Early plans put a Power
  Controller between the PD trigger and the amp, and drove the lamp from it. The
  lamp moved to Pi GPIO and the controller left the build. If it ever comes
  back: **its JP1 jumper must be OFF** before any 12–24 V is connected. With JP1
  set, 20 V goes straight to the Pi and destroys both boards. Check with a meter,
  not by eye. Its input limit is 30 V; the TVS advice above is for the amp's
  24 V.

---

<sub>Delgado &amp; Murphy</sub>
