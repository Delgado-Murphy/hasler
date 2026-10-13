# Hasler HAT — design decisions (rev A)

Companion to `BRIEF.md`. Decided 2026-10-13. The KiCad project is in `hardware/hat/`;
everything there is generated from `hardware/hat/gen/design.py`, which is the single
source for the netlist below.

## The stack

Measured from HiFiBerry's own STEP model (`amp4.step`, board 65 × 56, origin at its centre),
confirmed on the bench (the tall connectors stand ~14 mm):

| Amp4 Pro part | Where (HAT coords, mm, x from the SD-card edge, y up from the Pi's USB-C edge) | Top above amp PCB |
|---|---|---|
| 2×20 socket, underneath | standard Pi header position, row centre y = 52.5 | — |
| **2×20 pin header on top** | **offset 5.4 mm inboard: row centre y = 47.08** (x 7.1–57.9) | 8.6 mm pins |
| Screw terminals ×3 (speaker L, speaker R, power) | x 4.5–57.7, y 1.1–10.9 | **13.9 mm** |
| DC barrel jack (CUI PJ-102A) | x 17.5–26.9, y −1.7–12.7 | 11.1 mm |
| 8 mm electrolytic | x 13–24, y 15.4–21 | 8.1 mm |
| 2×2 pin header | x 58.7–63.8, y 36.2–41.3 | 8.6 mm |

So the HAT:

- plugs its **2×20 socket (8.5 mm, J1)** onto the Amp4's offset top header — the HAT's own
  header position is 5.4 mm inboard of the HAT spec's, its **outline and mounting holes
  are the HAT spec's**;
- is **cut short at y = 15**: the terminal strip and jack stay uncovered and reachable with a
  screwdriver, the stack stays low. Two M2.5 holes at (3.5, 52.5) and (61.5, 52.5) — the other
  two would land over the terminals;
- is **85 mm long** (the Pi's full length, not the HAT's 65): the HAT sits ~23 mm above the Pi's
  board and the Pi's USB/Ethernet jacks top out at ~16 mm, so the board runs over them and the
  **USB-C sits on that edge, flush with the Pi's port face** — every cable leaves one side of the
  cabinet. The power section lives in that extension. The far end is 20 mm past the last
  standoff: strain-relieve the USB-C cable to the cabinet rather than letting it hang;
- 4 layers (In1 solid GND), 1.6 mm, **black solder mask, white silkscreen**, ENIG;
- carries no HAT ID EEPROM: the Amp4 Pro has one on ID_SD/ID_SC and two would collide.

## Power

```
USB-C ─ CH224A (asks 20 V) ─ VBUS ─┬─ SMAJ22A ─ GND
                                   ├─ window 10.75 V < VBUS < 22.8 V (LM393 + AZ431 2.5 V ref)
                                   └─ AO4407A P-FET ── VOUT ─┬─ 220 µF/35 V + 10 µF + SMAJ22A
                                        (gate from window)   └─ J6 screw terminal → Amp4 power terminal
Amp4 Pro's own 5 V ─ Pi 5 V pins ─ XC6206 LDO ─ 3V3A (pots, ADC, J2 pin 12/26, lamp anode)
```

- The amp keeps powering the Pi (HiFiBerry: 5 V "up to 5 A", "any Pi up to the Pi 4B").
  A second 5 V source on the HAT would fight the amp's regulator on the Pi's 5 V pins.
- Budget at 20 V: Pi 4B ~15 W worst case + amp at volume into one speaker ~30 W peak
  → < 50 W, inside a 65 W charger (3.25 A).
- CH224A: CFG1 = 120 kΩ → **20 V**. 28 V needs 210 kΩ or an I²C write; the window opens the
  FET only between 10.75 V and 22.8 V (tolerances: 10.6–11.2 V, 22.1–23.4 V), so a 12 V or
  15 V charger still plays and nothing above 22.8 V reaches the amp (absolute max 24 V).
  **Never configure 28 V**: the amp is safe, but D1 would clamp the charger continuously and the
  USB-C receptacle is rated 20 V. (The CH224A's own "OVP" is only a 32 V reset, not protection.)
- UV and OV have separate dividers; 1 MΩ from EN back to the UV tap gives ~0.35 V hysteresis
  so a sagging supply can't make the FET chatter. 1 nF on each tap: spikes filtered, ~5 µs lag.
- Soft start: 100 nF gate–drain (C6) → ~3–7 A into the bulk cap instead of ~30 A when the FET
  opens mid PD transition, which could otherwise trip the charger's OCP into a boot loop.
- **Don't also plug a supply into the Amp4's barrel jack**: VOUT would back-feed VBUS through
  the FET's body diode.
- Its I²C status port (0x22/0x23 — inside the MCP23017's 0x20–0x27 range) is on solder
  jumpers JP9/JP10, **open** as shipped. Close them only with `addr: 0x20` pinned.
- LEDs: **PD** (yellow, CH224A power-good), **OUT** (green, switched rail live),
  **3V3** (red, 3V3A).

## J2 — the Hasler connector

One keyed, shrouded **2×13 box header** (DC3-2.54-26PAS, LCSC C5156668), numbering from the
KiCad footprint `IDC-Header_2x13_P2.54mm_Vertical` (DIN 41651): pin 1 in the row next to the
key slot, odd/even alternating. A standard female IDC plug lands ribbon wire *n* on pin *n*.
The HM's 16-wire ribbon is re-crimped into a **26-way plug, flush to pin 1**: wires 1–16 land
on pins 1–16, 17–26 stay empty.

| Pin | HM | Excelsior (proposed) | Board |
|---|---|---|---|
| 1 | K1 | key 1 | MCP GPB0 (d8) |
| 2 | GND (keys' return, tone switch) | GND | GND |
| 3 | K2 | key 2 | GPB1 (d9) |
| 4 | K3 | key 3 | GPB2 (d10) |
| 5 | K4 | key 4 | GPB3 (d11) |
| 6 | K5 | key 5 | GPB4 (d12) |
| 7 | K6 | key 6 | GPB5 (d13) |
| 8 | PU | Q | GPB6 (d14) |
| 9 | tone wiper | treble wiper | ADS A0 |
| 10 | volume wiper | volume wiper | ADS A1 |
| 11 | pots' low end | pots' low end | GND |
| 12 | pots' high end + LED anode | pots' high end + LED anode | 3V3A |
| 13 | LED red | LED red | 220 Ω → GPIO 17 |
| 14 | tone end switch | key 0 (release) | GPA0 (d0) |
| 15 | LED green | LED green | 100 Ω → GPIO 22 |
| 16 | LED blue | LED blue | 100 Ω → GPIO 27 |
| 17 | — | Tape | GPA1 (d1) |
| 18 | — | SP | GPA2 (d2) |
| 19 | — | speaker selector a | GPA3 (d3) |
| 20 | — | speaker selector b | GPA4 (d4) |
| 21 | — | spare | GPA5 (d5) |
| 22 | — | spare | GPA6 (d6) |
| 23 | — | bass wiper | ADS A2 |
| 24 | — | GND | GND |
| 25 | — | spare analog | ADS A3 |
| 26 | — | 3.3 V | 3V3A |

GPA7 / GPB7 (d7 / d15) are not connected — output-only on current MCP23017 silicon.

Every cabinet line: SRV05-4 ESD array at the connector → 1 kΩ series → (digital) 10 kΩ pull-up
to 3V3 + 100 nF to GND, (analog) 100 nF to GND. Lamp lines keep their resistors (220/100/100 Ω)
as the series element. Contact closed = low; the daemon's IPOL inversion makes it read 1.

## Pi header use (J1, via the Amp4's top header)

| Pin | GPIO | Use |
|---|---|---|
| 1, 17 | 3V3 | MCP23017, key pull-ups, Qwiic |
| 2, 4 | 5V | XC6206 LDO → 3V3A |
| 3, 5 | 2, 3 | I²C (shared with the amp's TAS5756 at 0x4d) |
| 11, 15, 13 | 17, 22, 27 | lamp R, G, B |
| 16 | 23 | MCP23017 INTA (JP5, closed) |
| 18 | 24 | ADS1115 ALERT/RDY (JP6, closed) |
| 22 | 25 | MCP23017 RESET (JP7, open) |
| 8, 10 | 14, 15 | UART → debug header J4 |
| 29, 31, 32, 33, 36, 37 | 5, 6, 12, 13, 16, 26 | spare → J4 |
| 7, 12, 35, 38, 40 | 4, 18–21 | amp (mute, I²S) — not touched |
| 27, 28 | ID_SD/SC | amp's EEPROM — not touched |

I²C map: 0x20 MCP23017 (A0–A2 to GND via JP2–JP4) · 0x48 ADS1115 (ADDR to GND via JP8)
· 0x4d amp · (0x22/0x23 CH224A only if JP9/JP10 are closed).

## Debug and upgrade

- **J4** 2×8 header: 5V, 3V3, GND, GND, SDA, SCL, TXD, RXD, GPIO 5, 6, 12, 13, 16, 26, 3V3A, GND.
- **J5** Qwiic / STEMMA QT (3.3 V I²C) for a future sensor or display.
- Test points (labelled): VBUS, VOUT, 5V, 3V3, 3V3A, GND ×2, SDA, SCL, INT, PG, EN, A0, A1.
- JP1 chooses 3V3A's source: XC6206 (default) or the Pi's 3V3 — to tell a noisy LDO from a
  noisy pot. **Cut 1–2 before bridging 2–3**, or the LDO fights the Pi's 3V3.
- `int_gpio: 23` is wired (JP5) but the daemon still polls; using the interrupt is a software
  change (INTCON/GPINTEN + an edge watch on GPIO 23).
- The AZ431's SOT-23 pinout (1 REF, 2 K, 3 A) differs from the TL431DBZ's in KiCad's library;
  the project symbol `hasler:AZ431_SOT23` carries the right one. Same trap as J2.
- Solder jumpers for every address, interrupt and reset line, so any of them can be
  disconnected while debugging.
