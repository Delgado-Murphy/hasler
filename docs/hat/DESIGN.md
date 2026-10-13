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
                                   └─ TPS259474A eFuse ── VOUT ─┬─ 1 µF + 10 µF + SMAJ22A (220 µF pads, not fitted)
          on from 5 V (JP11: 10.3 V), off > 22.6 V, 3.3 A, no back-feed │
                                                                └─ J6 (bottom-left) → Amp4 power terminal
Amp4 Pro's own 5 V ─ Pi 5 V pins ─ XC6206 LDO ─ 3V3A (pots, ADC, J2 pin 12/26, lamp anode)
```

- The amp keeps powering the Pi (HiFiBerry: 5 V "up to 5 A", "any Pi up to the Pi 4B").
  A second 5 V source on the HAT would fight the amp's regulator on the Pi's 5 V pins.
- Budget at 20 V: Pi 4B ~15 W worst case + amp at volume into one speaker ~30 W peak
  → < 50 W, inside a 65 W charger (3.25 A).
- CH224A: CFG1 = 120 kΩ → **20 V**. 28 V needs 210 kΩ or an I²C write. Whether the CH224A
  falls back to 12/15 V when a charger has no 20 V isn't in its datasheet: test it (the eFuse
  passes 12 and 15 V). **Never configure 28 V**: the eFuse's absolute maximum is 28 V, D1 would
  clamp the charger continuously, and the USB-C receptacle is rated 20 V. (The CH224A's own
  "OVP" is only a 32 V reset, not protection.)
- **3V3A is current-limited** (BRIEF must-have 4: a 3V3–GND short at a pot killed the bench
  Pi's 3.3 V regulator). It comes from its own XC6206 LDO fed from the 5 V rail, so a short at a
  pot or on J2 pins 12/26 meets the LDO's current limit and fold-back short-circuit protection,
  never the Pi's 3V3. JP1 2–3 (3V3A from the Pi's 3V3) bypasses that: bench only, never with
  the cabinet connected.
- **The switch is a TI TPS259474A eFuse** (U4, LCSC C3662807; TI RPW VQFN-HR 2 × 2 mm, footprint
  `hasler:TI_RPW0010A_VQFN-HR-10_2x2mm_P0.45mm` after TI's land pattern). It replaced the
  first design's LM393/AZ431 window and discrete P-FET after review: with a Miller-capacitor soft
  start, the inrush and the FET's stress scaled with the Amp4's unknown input capacitance, and
  the slow turn-off left an over-voltage on the amp for ~50 ms. The eFuse, all thresholds from
  its 1.20 V rising / 1.09 V falling references (datasheet SLVSFC9C):
  - **Turn-on: with the charger's 5 V, before the PD contract (JP11 open, as shipped).** EN is
    pulled up by R3 1 M alone (TI allows ≥ 350 k to IN above 5 V), so U4 switches on at its own
    ~2.5 V input threshold and ramps VOUT to 5 V; the CH224A then asks for 20 V and the charger
    makes the 5 → 20 V step with U4 already fully on (0.03 Ω): U4 never drops 15–20 V while the
    load is drawing.
    Why: an eFuse ramping VOUT from 0 to 20 V dissipates (VIN − VOUT) × current, and the Amp4's
    5 V buck and the Pi start drawing part-way up the ramp, as a constant-power load whose
    current is highest at low VOUT. The power review modelled it against TI's "time to thermal
    shutdown during inrush" curve (SLVSFC9C Fig 6-36: 10 W lasts ~6.5 ms, 20 W ~1 ms): with
    the Amp4's measured 220 µF and a 3–5 W boot load starting at 4.5–6 V, U4 reaches its
    154 °C shutdown at any ramp rate (C3 0.5–10 nF), restarts after 110 ms, and loops. The
    Amp4's start-up voltage isn't published. Starting at 5 V, the same load costs U4 well under
    1 W.
    The price: a 5 V-only charger, or one that never gets to a contract, feeds the amp 5 V (the
    Pi then browns out; nothing is damaged), and the load draws through the PD voltage change
    (PD asks a sink to idle at 2.5 W then; chargers in practice don't check — it is what a PD
    trigger board feeding the amp directly does).
  - **JP11 bridged: UVLO 10.3 V** (R3 1 M / R4 120 k + R16 12 k; 9.9–10.7 V worst case with
    the reference, 1 % resistors and pin leakage; off below 9.35 V). A 5 V or 9 V contract then
    never reaches the amp, but U4 does the linear 0 → 20 V start-up above; use it only if
    the amp turns out to start late (bench: VOUT ramps through ~10 V before the Amp4 draws).
  - **OVLO** (R5 2 M / R6 100 k + R7 12 k): off above **22.6 V** (21.7–23.7 V worst case) within
    **1.2 µs**, back on below 20.55 V (19.7 V worst case: a charger sitting at 20.5–21 V after an
    over-voltage trip may need a re-plug). A 20 V contract is ≤ 21 V (21.5 V transient), and the
    amp's absolute maximum is 24 V. The OVLO works in both JP11 settings.
  - **dV/dt** (C3 2.2 nF): VOUT ramps at **~1.0 V/ms** (0.3–1.75 V/ms over the dV/dt current's
    spread): ~5 ms to 5 V as shipped. On VOUT: 11 µF of ceramic plus the Amp4's own input
    capacitor, **measured 220 µF / 35 V**. **C7 (220 µF) is not fitted** (its pads stay): it adds
    nothing the Amp4's can doesn't, and with JP11 bridged it would double U4's start-up heat.
    Don't change C3 to slow the ramp: a slower ramp lengthens the time the load draws through
    U4 and makes the start-up heating worse.
  - **Current**: circuit breaker at **3.3 A** (R14 1 k, ±10 %, about a 65 W charger's 3.25 A),
    with an 18 ms blanking window (C4 22 nF) so the amp's peaks up to 6.6 A pass; above that,
    or for longer, it trips and retries (auto-retry variant). Fast trip at 2 × ILIM.
  - **True reverse-current blocking**: VOUT can't back-feed VBUS, so a supply on the Amp4's
    barrel jack, or the Amp4's own input capacitor after unplugging, can't reach the USB-C side.
  - **PG** (open drain, pulled up to the Pi's 3V3 by R15 100 k) goes high when VOUT > ~11.2 V
    (R8 1 M / R9 120 k on PGTH; up to ~1 V higher with pin leakage) and the switch is fully on:
    test point **PG** (TP12) only.
  - Thermal shutdown at 154 °C. RON 28 mΩ (45 max): ~0.25 W at 2.25 A.
  - IN/OUT are two 0.3 mm strips 0.2 mm apart, reachable only at their ends: IN is fed from the
    top end (D1, the TVS, above), OUT leaves from the bottom end (D5 below), each through a
    0.3 mm neck that widens to 0.8 mm once clear of the package. Having no exposed pad, the
    package sheds its heat through those strips: a small VBUS pour above and a VOUT pour below,
    on the top and bottom layers, stitched with vias, carry it away. C5 (1 µF) sits on the IN run
    and C17 (1 µF) on the OUT run, right at the strips, as TI's layout example has them.
  - **Back-feed limit**: VOUT may be at most 21 V above VIN. With no USB-C, a supply on the
    Amp4's barrel jack above ~20 V would exceed that: leave the jack empty while the HAT is on.
- The CH224A's I²C status port (0x22/0x23 — inside the MCP23017's 0x20–0x27 range) is on solder
  jumpers JP9/JP10, **open** as shipped. Close them only with `addr: 0x20` pinned.
- VBUS capacitance at attach: C1 + C2 + C5 ≈ 3 µF (USB PD allows 10 µF); the eFuse keeps VOUT's
  capacitance off VBUS.
- SMAJ22A on both sides: standoff 22 V, breakdown 24.4 V, so it doesn't conduct below the OVLO
  point. Its clamping voltage at full surge current (35.5 V) is above the eFuse's 28 V absolute
  maximum: it's protection against ringing and ESD, not against a lightning-class surge.
- LEDs: **PD** (yellow, CH224A power-good), **20V** (green, the switched rail is live — it reads
  12 or 15 V on a lower contract), **3V3A** (red, the cabinet rail).
- **Don't fit the extra 220–470 µF** that `docs/hardware.md` describes at the amp's DC input: it
  would multiply the eFuse's start-up heating (see dV/dt). The HAT has the TVS and the eFuse.

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
- Test points (labelled): VBUS, 20V (VOUT), 5V, 3V3, 3V3A, GND ×2, SDA, SCL, INT, PD (CH224A
  power-good), PG (eFuse), A0, A1.
- JP1 chooses 3V3A's source: XC6206 (default) or the Pi's 3V3 — to tell a noisy LDO from a
  noisy pot. **Cut 1–2 before bridging 2–3**, or the LDO fights the Pi's 3V3. 2–3 loses the
  cabinet rail's current limit: bench only, never with the cabinet connected.
- JP11 (open as shipped) sets the eFuse's turn-on: open = with the charger's 5 V, bridged =
  above 10.3 V (see Power).
- `int_gpio: 23` is wired (JP5) but the daemon still polls; using the interrupt is a software
  change (INTCON/GPINTEN + an edge watch on GPIO 23). All the HM keys are on port B and only
  INTA is wired, so set IOCON = 0x40 (MIRROR) when doing it.
- The eFuse (TI RPW package) has no KiCad library footprint: `hardware/hat/hasler.pretty` holds
  one built from TI's land pattern and checked against JLC's own (C3662807). Same kind of trap
  as J2: check pin 1 in JLC's preview.
- Solder jumpers for every address, interrupt and reset line, so any of them can be
  disconnected while debugging.

## Layout and routing

- Power column on the right (over the Pi's ports): USB-C → CH224A → TVS → eFuse → TVS (and C7's
  pads, not fitted).
  VOUT reaches **J6 at the bottom-left** (next to the notch, ~25 mm from the Amp4's power
  terminal) on a locked 1.6 mm bottom-layer trunk along the board edge under J2.
- The USB-C's two VBUS pad pairs (A4/B9, A9/B4) sit either side of CC and D+/D- at 0.5 mm pitch.
  Each drops through a via right behind it onto a **1.0–1.2 mm bottom-layer strap under the
  pad row**, so all four contacts share the current (the first layout joined one pair through
  0.25 mm), and a second bottom-layer path runs from that strap to a via beside D1, in parallel
  with the top-layer run. Copper on the main path, as built: pads → TVS → eFuse 0.6–0.8 mm on top
  (0.6 is set by the pad pitch, a few mm), eFuse OUT → D5 / C7's pads 0.8–1.2 mm, then the
  1.6 mm trunk to J6 (~33 mΩ, ~0.1 V and ~10 K at 3.3 A). Fine
  for the ~1 A typical / 2.5 A peak this board sees. The 0.19–0.25 mm VBUS/VOUT branches only
  feed the eFuse's dividers, the LEDs and the test points. The eFuse's IN/OUT strips are fed
  through 0.3 mm necks at their ends (the package allows nothing wider), widening to 0.8 mm.
- Laid by hand and locked before the autorouter: the VBUS strap, the CH224A's VHV ↔ VBUS link
  (pins 1 and 8 are on opposite sides of the chip, bottom layer), CC1 and D+ (they cross CC2 and
  D− between chip and receptacle: inner layer), and the loop joining J3's two D+ pads round the
  pad tips.
- GND: In1 is a solid plane. Every SMD GND pad is fanned out to it with its own via *before*
  autorouting; GND is poured on the other three layers and stitched to the plane on a 1.6 mm grid
  (laid after the signals are joined, so its vias don't take the gap-closer's spots). Through-hole
  GND pads have **thermal spokes** on every layer (J1 is hand-soldered); J1 pins 6, 14 and 34,
  J2 pin 24 and J4 pin 16 join solid, where DRC found a spoke landing on a scrap of pour.
- **No tracks or vias within 3 mm of the mounting holes** (rule areas): the M2.5 heads and metal
  standoffs (Ø ~5.8 mm) only touch GND. (The first layout ran SDA 1.7 mm from H2.)
- Signals: Freerouting (DSN/SES round trip), then a gap-closer, a power-track widening pass and a
  minimum-width pass in `make_pcb.py`. Tracks are 0.127 mm or wider, except two short router
  necks (0.112 mm; JLC's minimum is 0.09) between pads. The routed board is the artifact;
  re-running the router gives a different layout — `build.sh` refuses to
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
- R13's pull-up to the Pi's 3V3, decoupling at every
  ESD array, the VBUS strap, thermal spokes, the mounting-hole keepouts, silkscreen sizes and
  J2's pin-1 mark;
- `hasler-hm.yaml` stays the stripboard's mapping (the HAT's is `hasler-hm-hat.yaml`), the
  lamp driver's default pins no longer include GPIO 23 (the MCP's INTA), `build.sh` stops on
  DRC errors, and ORDERING.md's bring-up gained the pre-power, start-up and wiring checks.

A second round (three reviewers, same day) checked those fixes. It found the first fix for the
turn-on — a larger Miller capacitor plus a gate–source capacitor on the discrete P-FET — made the
inrush depend on the Amp4's unknown input capacitance and slowed over-voltage turn-off to
~50 ms. So the window and the FET became the **TPS259474A eFuse** (see Power). It also asked
for a second VBUS path from J3 to D1 (done), and shorter routes from C40–C42 to their ESD
arrays' VCC pins: every one is now within 6 mm of copper of its pin (C41 and C42 pre-routed).
A third round (two reviewers) checked the eFuse: its thresholds were re-centred for resistor
tolerance (UVLO 10.3 V so a 12 V −5 % supply starts, OVLO 22.6 V), C7 came off the BOM to keep
the eFuse's start-up heating low, and C5/C17 now sit on the IN/OUT runs with small heat pours.

A fourth round (five reviewers: power, logic and software, fab data, layout and mechanics, repo)
re-checked everything. No wiring errors. Changed:

- **the eFuse's turn-on** (JP11, see Power): a start-up model with the Amp4's measured 220 µF
  and the Pi booting part-way up the ramp showed U4 could overheat and restart for ever at a
  10.3 V UVLO, at any ramp rate; it now switches on at the charger's 5 V;
- C1 (the CH224A's VHV/VBUS capacitor) moved right under the chip (it was ~20 mm of copper away);
- C7's pads get no paste; the Gerber zip leaves out KiCad's job file (it numbers the inner
  layers wrongly) and the drill maps; the bottom silkscreen's eFuse line and the `JLCJLCJLCJLC`
  placeholder (order with "Remove Mark");
- master's 3V3A requirement (current-limited cabinet rail) is documented: the LDO is it;
- the daemon: no shutdown from an end switch that has read "off" since boot (an unplugged
  ribbon), and the MCP23017's registers are re-written if the chip resets.

Left as they are, noted: no gating of the eFuse on the CH224A's PG (see Power: as shipped the load
sees the PD voltage change); no third support near J2 (hold it when plugging in; a silicone bumper
on the Pi's USB jacks under the HAT's far end helps); J4
keeps 5 V on pin 1; no ESD diodes on D+/D− (the CH224A only uses them for BC1.2 detection);
pot readings may jitter a few counts with the lamp pulsing (shared 3V3A wire).

## Rebuilding

Everything under `hardware/hat/` is generated from `hardware/hat/gen/design.py`:

    cd hardware/hat
    FREEROUTING_JAR=/path/freerouting-2.4.1.jar JAVA=/path/java25/bin/java gen/build.sh

Needs KiCad 9 (its kicad-cli and Python; the scripts expect it in /Applications, as on macOS),
the system `python3` for the schematic, and Freerouting 2.4 (github.com/freerouting/freerouting
releases, the `-executable.jar`) on Java 25. ~25 minutes (routing ~2, finishing ~15). It stops before writing fab files if
DRC isn't clean.

The committed `fab/` files are the order: a rebuild re-routes and gives a different (equally
checked) board. To regenerate only the fab files from the committed board:
`<KiCad python3> gen/make_fab.py .` from `hardware/hat`. `PASSES` (default 100) sets
Freerouting's pass limit.
