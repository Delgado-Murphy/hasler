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
| 8 mm electrolytic (leads at x 15.13 / 18.93, y 18.2) | standing: x 12.9–21.2, y 14.0–22.4 | ~12 mm standing (the model draws it lying down) |
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
- has a **notch in its bottom edge** (HAT x 11.5–22.5, up to y 23.5; 8.5 mm deep from the
  board's cut edge at y 15) over the Amp4's 8 mm electrolytic. HiFiBerry's model draws it lying
  down; on a real board it stands ~12 mm over its leads (x 15.13 / 18.93, y 18.2 — holes in the
  STEP), taller than the HAT's underside (11.04 mm above the amp), so the notch clears the
  standing can with ~1 mm all round. (Rev A's first layout had the notch at x 15.5–26, from the
  lying-down model: it would have sat on the can.)
- stands on **11 mm M2.5 standoffs** at the two header-side holes;
- 4 layers (In1 solid GND), 1.6 mm, **black solder mask, white silkscreen**, ENIG;
- carries no HAT ID EEPROM: the Amp4 Pro has one on ID_SD/ID_SC and two would collide.

## Power

```
USB-C ─ CH224A (asks 20 V) ─ VBUS ─┬─ SMAJ22A ─ GND
                                   ├─ window 10.8 V < VBUS < 22.8 V (LM393 + AZ431 2.5 V ref)
                                   └─ AO4407A P-FET ── VOUT ─┬─ 220 µF/35 V + 10 µF + SMAJ22A
                    (gate from window, slow ramp)            └─ J6 (bottom-left) → Amp4 power terminal
Amp4 Pro's own 5 V ─ Pi 5 V pins ─ XC6206 LDO ─ 3V3A (pots, ADC, J2 pin 12/26, lamp anode)
```

- The amp keeps powering the Pi (HiFiBerry: 5 V "up to 5 A", "any Pi up to the Pi 4B").
  A second 5 V source on the HAT would fight the amp's regulator on the Pi's 5 V pins.
- Budget at 20 V: Pi 4B ~15 W worst case + amp at volume into one speaker ~30 W peak
  → < 50 W, inside a 65 W charger (3.25 A).
- CH224A: CFG1 = 120 kΩ → **20 V**. 28 V needs 210 kΩ or an I²C write; the window opens the
  FET only between 10.8 V and 22.8 V (UV 2.5 V × 43/10 = 10.75 V, 10.83 V with R16's load;
  OV 2.5 V × 42.8/4.7 = 22.77 V; tolerances ~10.6–11.2 V and 22.1–23.4 V), so a 12 V or 15 V
  supply would play and nothing above 22.8 V reaches the amp (absolute max 24 V). Whether the
  CH224A falls back to 12/15 V when a charger has no 20 V isn't in its datasheet: test it.
  **Never configure 28 V**: the amp is safe, but D1 would clamp the charger continuously and the
  USB-C receptacle is rated 20 V. (The CH224A's own "OVP" is only a 32 V reset, not protection.)
- UV and OV have separate dividers; 1 MΩ from EN back to the UV tap gives ~0.3 V hysteresis
  (on at 10.83 V, off below ~10.5 V) so a sagging supply can't make the FET chatter. 1 nF on
  each tap: spikes filtered, ~4–8 µs lag. OV has no hysteresis (a 20 V contract never sits at
  22.8 V).
- Turn-on, three parts (changed after review: the first version's 100 nF alone let ~7–16 A
  into the bulk caps and let a fast VBUS edge turn the FET on by itself):
  - **C18, 1 µF on EN** (with R7's 100 k): the FET waits ~10 ms after the window opens, so it
    doesn't load VBUS mid PD transition (the sink should stay at standby until PS_RDY).
  - **C6, 470 nF gate–drain** (Miller): on the plateau the gate current is
    (VBUS − 3 V)/R8 − 3 V/R9, 3–7 mA, so VOUT ramps at ~6–15 V/ms: ~1.4–3.5 A into C7 + C8
    (230 µF), plus the Amp4's own input caps in proportion (their value is unknown) — inside a
    65 W charger's 3.25 A, instead of the 7–16 A the first version allowed.
  - **C17, 10 µF gate–source**: with the FET off, C6 holds the gate at VOUT, so a fast rise of
    the source (hot-plug, the 5 → 20 V step, up to 30 V/ms in PD) would pull Vgs past the
    threshold. With Cgs ≫ C6 only ~5 % of a step reaches Vgs. It also slows turn-off: UV/OV
    take tens of ms to open the FET, which is fine for unplugging; fast transients are the
    TVS diodes' job.
  
  Measure the ramp on the bench (ORDERING.md, bring-up 1).
- **Don't also plug a supply into the Amp4's barrel jack**: VOUT would back-feed VBUS through
  the FET's body diode.
- Its I²C status port (0x22/0x23 — inside the MCP23017's 0x20–0x27 range) is on solder
  jumpers JP9/JP10, **open** as shipped. Close them only with `addr: 0x20` pinned.
- LEDs: **PD** (yellow, CH224A power-good), **20V** (green, switched rail live — it reads 12 or
  15 V on a lower contract), **3V3A** (red, the cabinet rail). Silkscreen labels: PD, 20V, 3V3A.

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

Every cabinet line: SRV05-4 ESD array at the connector (each with 100 nF on its VCC pin,
C38–C43), a 10 kΩ pull-up to 3V3 on the cabinet side (digital lines), then 1 kΩ series and
100 nF to GND at the MCP/ADC. A closed contact reads
~0 V at the MCP; the MCP's own ~100 k pull-ups (always on in the daemon) sit behind the 1 k and
don't matter. Lamp lines keep their resistors (220/100/100 Ω) as the series element. Contact
closed = low; the daemon's IPOL inversion makes it read 1.

## Pi header use (J1, via the Amp4's top header)

| Pin | GPIO | Use |
|---|---|---|
| 1, 17 | 3V3 | MCP23017, key pull-ups, Qwiic |
| 2, 4 | 5V | XC6206 LDO → 3V3A |
| 3, 5 | 2, 3 | I²C (shared with the amp's TAS5756 at 0x4d) |
| 11, 15, 13 | 17, 22, 27 | lamp R, G, B |
| 16 | 23 | MCP23017 INTA (JP5, closed) |
| 18 | 24 | ADS1115 ALERT/RDY (JP6, closed; 10 k pull-up to the Pi's 3V3, not 3V3A) |
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

## Layout and routing

- Power column on the right (over the Pi's ports): USB-C → CH224A → window → P-FET → bulk cap.
  VOUT reaches **J6 at the bottom-left** (next to the notch, ~25 mm from the Amp4's power
  terminal) on a locked 1.6 mm bottom-layer trunk along the board edge under J2.
- The USB-C's two VBUS pad pairs (A4/B9, A9/B4) sit either side of CC and D+/D- at 0.5 mm pitch.
  Each drops through a via right behind it onto a **1.0–1.2 mm bottom-layer strap under the
  pad row**, so all four contacts share the current (the first layout joined one pair through
  0.25 mm). Copper on the main path, as built: pads → TVS → P-FET 0.6–0.8 mm on top (0.6 is set
  by the pad pitch, a few mm), drains → bulk cap / TVS 0.8–1.2 mm, bulk cap → J6 1.6 mm. Fine
  for the ~1 A typical / 2.5 A peak this board sees. The 0.19–0.25 mm VBUS/VOUT branches only
  feed the window, LEDs and test points.
- Laid by hand and locked before the autorouter: the VBUS strap, the CH224A's VHV ↔ VBUS link
  (pins 1 and 8 are on opposite sides of the chip, bottom layer), CC1 and D+ (they cross CC2 and
  D− between chip and receptacle: inner layer), and the loop joining J3's two D+ pads round the
  pad tips.
- GND: In1 is a solid plane. Every SMD GND pad is fanned out to it with its own via *before*
  autorouting; GND is poured on the other three layers and stitched to the plane on a 1.6 mm grid
  (laid after the signals are joined, so its vias don't take the gap-closer's spots). Through-hole
  GND pads have **thermal spokes** on every layer (J1 is hand-soldered); J1 pins 6 and 39 and J2
  pin 11 join solid, where DRC found a spoke landing on a scrap of pour.
- **No tracks or vias within 3 mm of the mounting holes** (rule areas): the M2.5 heads and metal
  standoffs (Ø ~5.8 mm) only touch GND. (The first layout ran SDA 1.7 mm from H2.)
- Signals: Freerouting (DSN/SES round trip), then a gap-closer, a power-track widening pass and a
  minimum-width pass in `make_pcb.py`. Tracks are 0.127 mm or wider, except six short router
  necks (0.09–0.112 mm, JLC's minimum is 0.09) between pads where nothing wider fits. The routed
  board is the artifact; re-running the router gives a different layout — `build.sh` refuses to
  write fab files unless DRC is clean.
- Silkscreen: text ≥ 1.0 mm high (JLC's minimum; some lines use narrower characters to fit),
  every line ≥ 0.15 mm. J2's pin 1 is marked with a "1" inboard, because the library marker sits
  past the board edge.
- DRC: **0 violations, 0 unconnected, schematic parity clean** (kicad-cli, all severities).
  Checks set to ignore in `hasler-hat.kicad_pro`, on purpose:
  - `silk_edge_clearance`: the library silkscreen of the parts that sit on the edge (USB-C, J6,
    J2's key side, C7 by the corner) runs past the outline; the fab clips silkscreen there.
  - `footprint_filters_mismatch`, `footprint_type_mismatch`: the generated symbols' footprint
    filters and the SMD/THT attribute of a few library footprints don't match KiCad's
    heuristics; the footprints themselves are the library's.
  - `missing_courtyard`, `npth_inside_courtyard`, `pth_inside_courtyard`: test points, solder
    jumpers and mounting holes have no courtyard, and connector pegs sit inside their own.

## Review (2026-10-13)

Five independent reviewers (power/USB-PD, logic and pinout, layout and mechanics, fab data,
schematic and docs) went through rev A before ordering. Fixed in this revision:

- the notch, which had been placed from HiFiBerry's lying-down capacitor model (it would have
  sat on the standing can);
- the drill files' origin (they were ~100 mm off the Gerbers), C7's polarity in the CPL and the
  resistor arrays' pin 1;
- the turn-on (C6 470 nF, C17, C18 above), R13's pull-up to the Pi's 3V3, decoupling at every
  ESD array, the VBUS strap, thermal spokes, the mounting-hole keepouts, silkscreen sizes and
  J2's pin-1 mark;
- `hasler-hm.yaml` stays the stripboard's mapping (the HAT's is `hasler-hm-hat.yaml`), the
  lamp driver's default pins no longer include GPIO 23 (the MCP's INTA), `build.sh` stops on
  DRC errors, and ORDERING.md's bring-up gained the pre-power, window-ramp and wiring checks.

Left as they are, noted: no second FET against back-feed (the barrel-jack warning stands); no
gating of the FET on the CH224A's PG; no third support near J2 (hold it when plugging in); J4
keeps 5 V on pin 1; no ESD diodes on D+/D− (the CH224A only uses them for BC1.2 detection);
pot readings may jitter a few counts with the lamp pulsing (shared 3V3A wire).

## Rebuilding

Everything under `hardware/hat/` is generated from `hardware/hat/gen/design.py`:

    cd hardware/hat
    FREEROUTING_JAR=/path/freerouting-2.4.1.jar JAVA=/path/java25/bin/java gen/build.sh

Needs KiCad 9 (its kicad-cli and Python; the scripts expect it in /Applications, as on macOS),
the system `python3` for the schematic, and Freerouting 2.4 (github.com/freerouting/freerouting
releases, the `-executable.jar`) on Java 25. ~5 minutes. It stops before writing fab files if
DRC isn't clean.
