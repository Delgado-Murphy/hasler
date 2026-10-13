# Hardware

## Already owned — nothing to buy here

| Item | Role | Note |
|---|---|---|
| **Raspberry Pi 4B** | the brain | **Use this one.** HiFiBerry rate the Amp4 Pro's 5 V supply as good "up to the Pi4B", so the Pi 5 exceeds its budget — and the 4B runs cooler in a sealed wooden cabinet. The 3B+ has weaker WiFi/BT |
| **HiFiBerry Amp4 Pro** | DAC + amp, ~2×30 W | Hardware ALSA mixer — that is what makes one volume knob control radio, Bluetooth, AirPlay and Spotify alike. Powers the Pi from its own 5 V rail |
| ~~HiFiBerry Power Controller v1.1~~ | **dropped** | Not used in this build. The lamp went to Pi GPIO instead (below); the power-path and JP1 notes further down only apply if it comes back |
| **HW-398 PD trigger** | 20 V from a USB-C charger | Fastsoc FS312. Bridge the **20 V** pad only |
| **RGB LED, 4-wire** | pilot lamp | Pi header pins 11/15/13 (GPIO 17 red, 22 green, 27 blue) + common, three resistors — see *Pilot lamp* |
| Hasler HM cabinet | keys, pots, speaker | 7 momentary keys, volume + tone pots, elliptical speaker |

## Buy now — Stage 1, makes it play

| Item | Qty | Why |
|---|---|---|
| **USB-C PD charger, 65 W GaN** | 1 | **Must advertise a 20 V PDO** — many 30–45 W chargers stop at 15 V. 65 W gives 20 V/3.25 A, ample |
| USB-C cable | 1 | Any decent cable to 3 A; above that needs an e-marked one |
| **microSD card, 32 GB A1/A2** | 1 | moOde. Endurance-rated is worth it in an always-on box |
| **Multimeter** | 1 | Phase 0 is *entirely* metering. Also the only safe way to verify JP1 |
| Silicone hookup wire, 26–28 AWG stranded | ~5 m | Panel loom: 7 keys + end-switch + common ground. Stranded, because solid core fatigues where a lid opens |
| **100 nF ceramic capacitors** | 10 | One across each key for hardware debounce. Cheap; skipping them shows up as bounce in `panel-probe` |
| Dupont / JST-XH connectors | 1 set | So the panel unplugs from the Pi. You will open this box more than once |
| Small heatsink set for Pi 4B | 1 | Sealed cabinet, no airflow |
| Contact cleaner (DeoxIT F5 or similar) | 1 | 60-year-old carbon tracks and key contacts. Clean them *before* deciding they are faulty |

## Buy now — protection, ~€2 total

Cheap insurance against a failure that would be miserable to diagnose from inside a closed cabinet.

> **With the Hasler HAT** (`hardware/hat/`) skip this section: the HAT's eFuse, TVS and USB-C PD
> input replace it, and an extra 220–470 µF at the amp would only add to the eFuse's start-up
> heating (`docs/hat/DESIGN.md`, Power).

| Item | Qty | Why |
|---|---|---|
| **220–470 µF / 35 V electrolytic** | 1 | Across the **Amp4 Pro's DC input**, at the amp end. Without the Power Controller this is the main protection: it soaks up plug/unplug transients |
| **SMAJ24A TVS diode** (see below) | 1 | Clamps what the cap doesn't |
| NTC inrush limiter (e.g. 5 Ω) | 1 | *Only if* the PD supply trips when Vout closes. Buy it, fit it only if needed |

> **Without the Power Controller (the current build)** the limit is the **Amp4 Pro's absolute maximum of 24 V**, not
> the Controller's 30 V. A 22-standoff TVS breaks down at 24.4–26.9 V, slightly *above* that, so it only catches fast
> spikes and the bulk capacitor does the real work. Still fit an SMAJ/SMBJ**22**A (not a 24) at the amp input. Wiring:
> Paper sheet H2 (Power).

### Choosing the TVS — the number means different things in different families

A 20 V PD rail sits at up to **21 V** with tolerance, so the part needs a **reverse standoff voltage
(V_RWM) of at least 22 V**, unidirectional. The trap is that two naming conventions are in use:

| Family | The number in the part name is… | So for a 20 V rail |
|---|---|---|
| **SMAJ / SMBJ / SMCJ** `xxA` | **standoff** V_RWM | pick **22** or **24** |
| **P6SMB / 1.5KE / P6KE** `xxA` | **breakdown** V_BR (standoff ≈ 0.85×) | pick **27** or **30** |

Get this backwards and you fit a P6SMB24A — 24 V *breakdown* but only **20.5 V standoff** — which
conducts continuously on your own rail, heats up and drags the voltage down.

**SMAJ (400 W, SMA) and SMBJ (600 W, SMB) both work.** Both number by standoff, so take a **22** or
a **24** from either kit. SMBJ is physically bigger, which makes it slightly easier to hand-solder.

| Part | Standoff V_RWM | Breakdown V_BR | Clamp V_C @ I_PP |
|---|---|---|---|
| SMAJ22A | 22.0 V | 24.40 – 26.90 V | 35.5 V @ 11.3 A |
| SMAJ24A | 24.0 V | 26.70 – 29.50 V | 38.9 V @ 10.3 A |
| SMBJ22A | 22.0 V | 24.40 – 26.90 V | 35.5 V @ 16.9 A |
| SMBJ24A / **24CA** | 24.0 V | 26.70 – 29.50 V | 38.9 V @ 15.4 A |

Neither clamps under the Power Controller's 30 V at *rated* surge — but rated surge is a
10/1000 µs pulse, vastly larger than a short wire's inductance can deliver when Vout opens on a few
amps. At the currents actually in play each clamps near its **breakdown** voltage: ~24–27 V for a
22, ~27–30 V for a 24. So a 22 protects slightly better, at the cost of 1 V of standoff headroom
over the 21 V worst-case rail instead of 3 V.

**Pick with the meter**, during the unloaded PD check in the build guide:

- rail measures **≤ 20.5 V → a 22** (clamps earlier and lower)
- rail measures **> 20.5 V → a 24** (more headroom against a charger running high)

### `A` vs `CA` — unidirectional or bidirectional

`A` = **unidirectional**, `CA` = **bidirectional**. Same standoff either way; genuinely different
parts.

**Unidirectional is the textbook choice for a DC rail** — it avalanches at V_BR on a positive
transient and conducts forward on a negative one, clamping the rail to about −0.7 V. Bidirectional
clamps symmetrically instead, so it gives up that behaviour.

**But `CA` works perfectly well here**, and buys back something that matters for hand assembly:

| | Unidirectional `A` | Bidirectional `CA` |
|---|---|---|
| Overvoltage clamping | ✅ | ✅ |
| Negative-transient clamp to −0.7 V | ✅ | ✗ |
| Can be fitted backwards | **⚠️ yes — see below** | ✗ nothing to get wrong |

**⚠️ Orientation, if you use an `A` part:** the **banded (cathode) end goes to the positive rail**,
the other end to ground. Fit it the other way round and it forward-conducts at 0.7 V — a dead short
across a 20 V supply. There is no such risk with a `CA`.

So: prefer an `A` if the kit has 22A or 24A and you are confident about the band. If **SMBJ24CA** is
what is in front of you, fit it — the protection is equivalent for this job and it cannot be
installed wrong.

### The usable window, and what will not do

Two constraints bracket the choice: standoff must clear the **21 V** worst-case rail, and breakdown
must stay under the Power Controller's **30 V** limit or it never clamps in time to protect anything.

| Standoff | Verdict |
|---|---|
| 18, 20 | ❌ conducts continuously on your own rail — heats, drags the voltage down |
| **22** | ✅ best clamping (V_BR 24.4 V) |
| **24** | ✅ best headroom (V_BR 26.7 V) |
| 26 | ⚠️ V_BR 28.9 V, right on the limit |
| 28+ | ❌ breaks down above 30 V — too late to matter |

Other families work on the same rule, remembering the numbering convention above: SMCJ22A/24A
(1500 W), P6SMB27A/30A, or 1.5KE27A/30A — the last being through-hole axial and the easiest of all
to hand-solder onto the PD board's output pads.

**An ordinary diode is not a substitute.** A rectifier (1N4007, 1N5819) reverse-biased across the
rail does nothing until it fails destructively. A Zener does clamp, but a 1 W 1N47xx has a tiny
fraction of a TVS's surge capability and responds in microseconds rather than picoseconds — a TVS
die is built to absorb hundreds of watts for a microsecond, a Zener to dissipate one watt forever.
A 5 W part (1N5359B, 24 V) is a stopgap, not an equivalent.

**Ranking, if the ideal part is not to hand:** TVS at 22 or 24 → the bulk capacitor alone with short
wires (genuinely effective, and doing most of the work anyway) → a 5 W Zener → nothing. An ordinary
rectifier belongs in none of those places.

**After fitting, sanity-check it:** with 20 V applied, the TVS should draw microamps and stay
**stone cold**. If it is even warm, the standoff is too low — wrong part, take it off.

**If you cannot source one at all:** fit the bulk capacitor anyway and keep the wire run short. The
cap does most of the work; the TVS is belt-and-braces. This is a reliability hedge, not a
safety-critical part — the box will run happily without it, and it can be added later.

## Buy now — Phase 3b, wakes the wheels up

| Item | Qty | Why |
|---|---|---|
| **ADS1115 module** | 1 | The Pi has no analog input. Until this arrives the pots are dead and volume comes from the moOde UI / HA |
| 100 nF ceramic | 2 | Wiper to ground on each pot. **Not optional if the pots are 1 MΩ** — at that value the ADS1115's sampling input needs the cap to supply its charge, or readings sag and wander |

## Optional / later

| Item | Why |
|---|---|
| ~~MCP23017 module~~ | **Owned and now the default** — see *Wiring quick reference*. Collapses the panel loom to 4 wires |
| Panel-mount USB-C female socket | The HW-398 ends in a *male plug*, so it must plug into something. This puts a modern USB-C inlet on the cabinet and keeps the DC run short. Check its current rating |
| **USB Bluetooth dongle** | The Pi's onboard chip shares one radio between WiFi and BT and A2DP stutters. Ethernet + onboard BT is better; this is the fallback |
| DPDT relay module | Internal/external speaker switching — Excelsior |
| Speaker binding posts | External speaker terminals |
| 4 Ω elliptical speaker | Only if the original ever dies. Phase 0 confirms it is a healthy 16 Ω direct voice coil |

## The speaker — measured

**14.2 Ω DCR → 16 Ω nominal**, direct voice coil, no output transformer (a transformer secondary
would read under 1 Ω). It drives straight off the Amp4 Pro.

**Impedance is fine.** 16 Ω is above HiFiBerry's stated 4–8 Ω, but that is the *safe* direction: a
higher load draws *less* current from a voltage-source amplifier. The 4 Ω minimum is the hard limit;
the top of a stated range is only where rated power stops being guaranteed. The one real caveat is
that a class-D output LC filter is tuned for a nominal load, so a higher impedance leaves it
under-damped and can peak around 20–40 kHz — inaudible on a cone with maybe 5 kHz of bandwidth.

**Power is the actual risk.** Into 16 Ω the Amp4 Pro gives roughly half its 8 Ω figure — about
**10 W** — against a vintage cone good for perhaps **1–3 W**. Five to ten times more amplifier than
the speaker can survive, on a part with no authentic replacement.

Mitigated by **`moode.volume_limit`** in `config/haslerd.yaml`, which caps the hardware mixer while
leaving the knob its full sweep and resolution. Calibrate it by ear — the procedure is in
`docs/variants/hasler-hm.md`.

**⚠️ Never wire the speaker across the two amp channels.** Class-D outputs are already bridge-tied;
connecting across two of them is a dead short that destroys the amplifier. One channel only, with
L+R summed to mono upstream so nothing is lost.

## Wiring quick reference

**Keys via the MCP23017** (the default in `hasler-hm.yaml`). Two boards on hand, same chip —
the pin rules below apply to both; only the connector and the default address differ.

**CQRobot MCP23017 IO Expansion Board** (black, preferred). Its 6-pin JST cable *and* the 6-pin
header opposite carry the same six signals, so the board doubles as an I²C hub: Pi → JST cable,
right-hand header → ADS1115.

| Signal | Cable colour* | Pi (via the Amp4 Pro's top header) | Note |
|---|---|---|---|
| VCC | red | 3.3 V, pin 1 | the board runs at 3.3–5 V; **3.3 V** keeps SDA/SCL at the Pi's level |
| GND | black | GND, pin 6 | |
| SDA | green | pin 3 (GPIO 2) | shared with the Amp4 Pro's amp chip (0x4d) |
| SCL | blue | pin 5 (GPIO 3) | |
| INTA | yellow | — | leave unconnected; haslerd polls |
| INTB | orange | — | leave unconnected |

\* Colours as supplied — trust the silkscreen on the back, not the colour.
**Address `0x27`**: A0–A2 pads open (pulled high). Bridging pads lowers it; `0x20` = all bridged.
The PA/PB headers each carry their own **GND** and **VCC** pins — use the PA header's GND as the
key loom's common ground.

**Green breakout** (alternative). Four wires from its top header:

| Board | Pi | Note |
|---|---|---|
| VCC | 3.3 V (pin 1 or 17) | **Not 5 V** — the board's 4.7 kΩ I²C pull-ups go to VCC, and the Pi's pins are 3.3 V only |
| GND | GND (pin 9) | the key loom's common ground lands here too |
| SDA | pin 3 (GPIO 2) | shared bus with the Amp4 Pro (0x4d) |
| SCL | pin 5 (GPIO 3) | |
| RST | — | must sit at VCC — meter RST↔VCC; if open, bridge it, or the chip stays in reset |
| ITA / ITB | — | leave open; haslerd polls |
| A0–A2 | — | to GND → `0x20` — change `addr:` in the descriptor to match |

**Keys — identical on either board:**

| Logical | Contact | Board pin | | Logical | Contact | Board pin |
|---|---|---|---|---|---|---|
| d0 | Key 1 | PA0 | | d4 | Key 5 | PA4 |
| d1 | Key 2 | PA1 | | d5 | Key 6 | PA5 |
| d2 | Key 3 | PA2 | | d6 | PU | PA6 |
| d3 | Key 4 | PA3 | | d8 | Tone end-switch | **PB0** |

**⚠️ PA7 and PB7 are output-only** on current MCP23017 silicon (Microchip datasheet rev. D). A
switch there may never read. The daemon refuses `d7`/`d15` at startup, which is why the end-switch
skips to PB0.

**Fallback: direct GPIO map** — avoids the Amp4 Pro's 2, 3, 4, 18–21, the HAT EEPROM (0, 1), UART (14, 15) and the lamp (17, 22, 27). Commented out in `hasler-hm.yaml`:

| Logical | Key | GPIO | | Logical | Key | GPIO |
|---|---|---|---|---|---|---|
| d0 | 1 | 5 | | d4 | 5 | 16 |
| d1 | 2 | 6 | | d5 | 6 | 23 |
| d2 | 3 | 12 | | d6 | PU | 24 |
| d3 | 4 | 13 | | d8 | tone end-switch | 25 |

All keys to a common ground rail, pull-ups enabled in software, 100 nF across each.

## Pilot lamp — RGB LED, resistors on the prototype board

The board's 7-wire ribbon uses only the header's **odd row** (Paper PB1): pins 1 3V3 · 3 SDA · 5 SCL ·
*7 empty (amp mute)* · 9 GND · 11 · 13 · 15 = lamp. Nothing lands on the even row, which carries 5 V.
haslerd drives the lamp with software PWM (`io.lamp` in `hasler-hm.yaml`).

| Header pin | Pi | LED leg | Series resistor (on the board) |
|---|---|---|---|
| 11 | GPIO 17 | red | **220 Ω** |
| 15 | GPIO 22 | green | **100 Ω** |
| 13 | GPIO 27 | blue | **100 Ω** |
| — | board GND rail | common (**common cathode**, long leg) | — |
| — | board 3V3 rail | common (**common anode** only — move jumper J1) | — |

- **Confirm the cabinet wire colours with a meter.** The code drives green on GPIO 22 and blue on
  GPIO 27; if the lamp shows them swapped, change `pins:` under `io.lamp` in the variant YAML.
- **Which type?** Meter in diode mode, red probe on the long leg: colours glow → common anode; glow
  only with the black probe there → common cathode. Set `common:` to match. Backwards just inverts the
  colours (off becomes white); nothing breaks.
- **Resistors go on the colour legs, never the common.** These keep each pin at roughly 4–6 mA at 3.3 V, well inside what a GPIO can source
  (red drops ~2.0 V, green/blue ~2.9 V). Too dim or one colour dominant: adjust `balance:` in the
  descriptor before changing parts.
- The pots' 3.3 V and GND come from the board's rails too (header V), not from the Pi.

**I²C map:** `0x27` MCP23017 keys (CQRobot; `0x20` green board) · `0x48` ADS1115 (Phase 3b) · `0x4d` the Amp4 Pro's own amp chip (`UU` once its driver loads) — leave it alone

## The two steps that can destroy something

1. **More than 24 V into the Amp4 Pro**, or the speaker wired **across two channels**. Meter the PD
   trigger unloaded before it ever touches the amp (19.5–20.5 V), and put the speaker on L+ / L− only.
2. **The original mains transformer and wiring come out entirely**, and the old capacitors get discharged
   before they go in the bin.

(If the Power Controller ever comes back: **JP1 must be OFF** before any 12–24 V is connected — with
JP1 set, 20 V goes straight to the Pi and destroys both boards. Verify with a meter, not by eye.)

Everything else is recoverable.

---

<sub>Delgado &amp; Murphy</sub>
