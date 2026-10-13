# Ordering the Hasler HAT (rev A) from JLCPCB

Everything in `fab/` is generated: `gen/make_sch.py`, `gen/make_pcb.py`, `gen/make_fab.py`
from `gen/design.py` (`gen/build.sh` runs them all). Design decisions: `docs/hat/DESIGN.md`.

## Files to upload

| JLCPCB step | File |
|---|---|
| PCB (Gerber) | `fab/hasler-hat-gerbers.zip` |
| Assembly → BOM | `fab/hasler-hat-bom.csv` |
| Assembly → CPL (pick and place) | `fab/hasler-hat-cpl.csv` |

## PCB options

| Option | Value |
|---|---|
| Layers | **4** |
| Dimensions | 85 × 41 mm (read from the Gerbers) |
| Thickness | 1.6 mm |
| Impedance / stackup | JLC04161H-7628 (default) — nothing on this board is impedance controlled |
| **Solder mask** | **Black** |
| Silkscreen | White |
| Surface finish | ENIG (flat pads for the SSOP/VSSOP; HASL works too) |
| Via covering | Plugged (JLC's default for 4 layers, free) |
| Min via hole / diameter | 0.3 / 0.6 mm (standard, no extra charge) |
| Mark on PCB | Remove Mark (default). If you'd rather have the order number, "specify a location": the `JLCJLCJLCJLC` text on the bottom silkscreen |
| Confirm production file | Yes (€0.94): JLC's engineers check the files and you approve before fabrication |
| Edge | One notch in the bottom edge (over the Amp4's capacitor): it's in the outline, no extra option |

After the upload, open JLC's **Gerber viewer** once and check that the drill holes sit in their
pads (the drill files and Gerbers share the board's bottom-left origin).

## Assembly options

- **PCBA type:** Standard. **Assembly side: Top.** All SMD parts are on the top.
- **J1 (the 2×20 socket) is hand-soldered** — see below. It is in the BOM and CPL (on the
  `Bottom` layer), so a top-side order leaves it out by itself: the BOM step shows 34 lines
  (35 with J1). Buy five sockets yourself: LCSC **C5124634**, or any 2×20,
  2.54 mm female header **8.5 mm tall** (the height sets the gap over the Amp4).
- JLC fits J2 (2×13 box header), J4 (2×8 header) and J6 (screw terminal) from the top.
- **Confirm parts placement: Yes** (€0.40): JLC's engineers check every position and rotation
  and you approve before assembly.
- **Not fitted by design:** test points, solder jumpers, mounting holes (bare unplated holes), and
  **C7** (220 µF bulk capacitor, marked DNP: its pads stay; see DESIGN.md, Power).
- **Check the placement preview** (2D and 3D) before paying. `make_fab.py` turns KiCad's
  orientations into JLC's and was checked part by part against JLC's own footprints and in
  JLC's preview on 2026-10-13:
  - SOT-23 / SOT-23-3 / SOT-23-6 +180°, SSOP / VSSOP +270°, 4×0603 resistor arrays +270°, the
    eFuse (custom RPW footprint) and the USB-C 0°;
  - J1, J2, J4, J6 (KiCad origin on pin 1) and J3 (JLC's model 1.5 mm off KiCad's origin) are
    placed by the centre of their pads; the 2-row headers turn 270°.
  
  Look at: pin 1 of U1–U4 and U7–U12 (U4 is the 2 × 2 mm eFuse: pin 1 top-left, its two long
  IN/OUT pads vertical in the middle); the cathode marks of D1 and D5; J2's key toward the board edge; J3's contacts on their pads
  and its mouth out of the right edge, ~1 mm over it (on purpose: flush with the Pi's ports).
  JLC numbers the KT-0603 LEDs (D2, D7) pin 1 = anode, so their pin-1 dot lands on the anode:
  the polarity is right, don't let anyone "correct" it.
- Extended parts each carry JLC's one-off loading fee; there are 13 (J1 excluded).

## Before you order

1. **Stock.** Every line was in stock on 2026-10-13. The tightest were the DC3 box header
   (C5156668, ~580), the TPS259474A eFuse (C3662807, ~600), SMAJ22A (C283896, ~1.1 k) and
   MCP23017 (C506653, ~1.1 k).
2. **The Amp4's capacitor.** The notch (bottom edge, x 11.5–22.5 mm from the SD-card end,
   8.5 mm deep) clears the 8 mm electrolytic standing over its leads with ~1 mm all round.
   Hold the board over a real Amp4 Pro once before ordering if you can.
3. **J2 orientation.** In JLC's viewer: J2's pin 1 (square pad, and a "1" on the silkscreen
   beside it) is bottom-left, in the row along the board edge, and the key slot faces that
   edge. A standard 26-way IDC plug then puts ribbon wire 1 (the stripe) on pin 1.

## Soldering J1

- The socket goes on the **bottom** (the side with the J2 table), its pins up through the
  board, soldered on the **top**. It has no polarity of its own: what matters is the side,
  and that the HAT then sits with its holes over the Amp4's standoffs (it can't go on rotated
  without missing them).
- Keep it square: plug it onto the **unpowered** Amp4 Pro's header with the HAT on its
  standoffs, then solder.
- The pins are 1.26 mm from U1's fine-pitch pads (and ~0.8–0.9 mm from TP3, J5 and the JP2–JP4
  jumpers): fine tip, a strip of Kapton over them.
- GND pins have thermal spokes on every layer except J1 pins 6, 14 and 34, which join the pour
  solid (DRC couldn't give them two good spokes). Give those three a little longer. (J2 pin 24 and
  J4 pin 16 are solid too; JLC solders those.)

## Bring-up (bench, before the cabinet)

0. **Before any power**, with a meter on resistance: VBUS–GND (TP VBUS), VOUT–GND (TP 20V),
   5V–GND, 3V3–GND and 3V3A–GND must not read a short (the big caps read low at first, then
   climb).
1. **The eFuse, with a bench supply** (current limit ~0.5 A: the start-up inrush is ~0.2 A) on
   TP VBUS and GND, nothing else connected. Ramp 0 → 24 V slowly:
   - the green **20V** LED lights once VBUS passes ~10.3 V (VOUT ramps up behind it at ~1 V/ms)
     and TP **PG** goes high once a Pi powers its 3V3 (it's pulled up there; on the bench, read it
     as "not pulled low");
   - above ~22.6 V it goes out at once (OVLO), and comes back below ~20.6 V;
   - coming down, it goes out below ~9.4 V.
   
   If you have a scope, step the supply 5 → 20 V and watch TP 20V: a ~20 ms ramp, not a jump.
2. **USB-C, still no Pi, no amp.** Plug a 65 W PD charger into J3 (right edge, the Pi's port
   side). The yellow **PD** LED lights. Then:
   - TP **VBUS** and TP **20V** (VOUT) read 20 V, and the green **20V** LED is lit;
   - J6 (bottom-left, by the notch) reads 20 V, + on the left pin (marked on the silkscreen).

   A 5 V-only USB supply must leave 20V dark. A 12 V or 15 V PD charger: the eFuse passes
   both, but whether the CH224A falls back to them when 20 V isn't offered is untested — try it.
3. **Unplug the USB-C.** Make a pigtail for J6, check its polarity with the meter, and fit it
   to the Amp4 Pro's power terminal (+ to +). **Leave the Amp4's barrel jack empty**: the eFuse
   blocks back-feed, but only up to 21 V across it, so a 24 V supply there with the USB-C
   unplugged would exceed its rating. And **never
   use the Pi's own USB-C power port** while it's stacked (it would back-feed the amp's 5 V).
4. Stack Pi + Amp4 Pro + HAT on **11 mm M2.5 standoffs** (two, at the header-side holes).
   Plug the HAT's USB-C last. **Watch the start-up**: the 20V LED should come on once and stay
   on while the Pi boots. If it blinks every ~0.1 s (the eFuse overheating while it charges the
   amp's capacitor and restarting), unplug and fit C3 = 4.7 nF (0603) for a slower ramp; if the
   amp turned out to need C7, fit it only with that slower ramp. Then:
   - `i2cdetect -y 1` should show `20` (MCP23017), `48` (ADS1115) and `4d` (UU once the amp
     driver loads);
   - TP 3V3A reads 3.3 V and the red **3V3A** LED is lit.
5. Point `haslerd.service`'s `--variant` at `config/variants/hasler-hm-hat.yaml` (pins d8–d14,
   d0 for the tone switch, `addr: 0x20`). `hasler-hm.yaml` stays the prototype stripboard's
   mapping until then: the HAT file on the stripboard would read the end switch open and shut
   the Pi down. Re-crimp the HM ribbon into a 26-way plug flush to pin 1 and plug it into J2.
   Run `scripts/panel-probe.py --variant config/variants/hasler-hm-hat.yaml`.
6. Support J2 with a finger when you push the IDC plug in: the board is held along its header
   edge only, ~30 mm away.

## Debug aids on the board

- J4 (2×8): 5V, 3V3, GND, I²C, UART (GPIO 14 = Pi TX on pin 7, 15 = RX on pin 8),
  GPIO 5/6/12/13/16/26, 3V3A. The pinout is printed next to the left corner branding.
  **Pin 1 is 5 V**: never connect a USB-UART's 5 V, and keep jumpers off pins 1–2.
- Qwiic (J5): 3.3 V I²C. The Pi already has 1.8 k pull-ups on the bus: take them off a
  Qwiic module if you can (two sets of pull-ups is over the I²C spec's 3 mA).
- Test points on every rail, I²C, INT, PG, EN and the two pot wipers, all labelled. 5V is by
  the LDO at the left; VBUS, 20V, PD (the CH224A's power-good) and PG (the eFuse's) are in the
  power column.
- Solder jumpers (labelled):
  - JP1: 3V3A from the LDO or the Pi. Cut 1–2 before bridging 2–3.
  - JP2–JP4: MCP23017 address (never leave one cut and unbridged: the pin would float).
  - JP5: INT → GPIO 23.
  - JP6: RDY → GPIO 24.
  - JP7: RST ← GPIO 25 (open).
  - JP8: ADS1115 ADDR (must not float: if you cut it, bridge another address).
  - JP9/JP10: the CH224A's I²C (open; it answers at 0x22/0x23, inside the MCP's range).
- The J2 pinout, with HM and Excelsior signals, is on the bottom silkscreen.
- Keep `POWER_OFF_ON_HALT=0` (the Pi's default): the lamp anode is on 3V3A, which the amp keeps
  up, so a halted Pi with its 3V3 off would have a little current flow into GPIO 17/22/27.

## Checks already done

- ERC 0; DRC 0 violations, 0 unconnected, schematic parity clean (all severities). The rules
  set to ignore, and why, are in `docs/hat/DESIGN.md`.
- A five-reviewer pass (power, logic/pinout, layout/mechanics, fab data, schematic/docs) on
  2026-10-13: every IC pinout and polarity against its datasheet, every CPL rotation against
  JLC's own footprint, the Amp4 clearances against HiFiBerry's STEP model. Their findings are
  fixed in this revision (see `docs/hat/DESIGN.md`, "Review").
- Renders: `renders/hasler-hat-top.png`, `renders/hasler-hat-bottom.png`.
