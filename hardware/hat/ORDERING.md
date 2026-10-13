# Ordering the Hasler HAT (rev A) from JLCPCB

Everything in `fab/` is generated: `gen/make_sch.py`, `gen/make_pcb.py`, `gen/make_fab.py`
from `gen/design.py`. Design decisions: `docs/hat/DESIGN.md`.

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
| Min via hole / diameter | 0.3 / 0.6 mm (standard, no extra charge) |
| Remove order number | **Specify a location**: the `JLCJLCJLCJLC` text on the bottom silkscreen |
| Edge | One notch in the bottom edge (over the Amp4's capacitor): it's in the outline, no extra option |

## Assembly options

- **PCBA type:** Standard (the board has extended parts and through-hole parts).
- **Assembly side:** **Top**. All SMD parts are on the top.
- **Through-hole parts:** J2 (2×13 box header), J4 (2×8 header) and J6 (screw terminal) go in
  from the top. **J1, the 2×20 socket, goes in from the bottom** and is soldered on the top. In
  the CPL it is listed as `Bottom`. If JLC won't do bottom-inserted THT for a sensible price, leave
  J1 off (remove it from the BOM) and solder it by hand. That's 40 easy joints; keep the socket
  square to the board, ideally by plugging it onto the Amp4 Pro while soldering.
- **Not fitted by design:** test points, solder jumpers, mounting holes (copper only). Nothing is DNP.
- **Check every rotation in JLC's placement preview** before paying. KiCad and JLC orient
  footprints differently; `make_fab.py` applies the usual offsets (SOT-23, SOIC/SSOP/VSSOP,
  resistor arrays, the electrolytic; the USB-C needs none), but verify these by eye:
  - pin 1 of U1 (MCP23017), U2 (ADS1115), U3 (CH224A), U4 (LM393), U7–U12 (SRV05-4);
  - the cathode bands of D1, D5 (SMAJ22A), D3, D4 (zeners) and the LEDs D2, D6, D7;
  - the + of C7;
  - the USB-C (J3): its contacts on the pads, its mouth out of the right edge, ~1 mm over it
    (on purpose: flush with the Pi's USB/Ethernet jacks). Checked in JLC's preview 2026-10-13.
- The through-hole connectors (J1, J2, J4, J6) have their KiCad origin at pin 1; JLC places
  by body centre, so `make_fab.py` writes their pad centre and turns the 2-row headers 270°.
  Checked in JLC's 3D preview on 2026-10-13: all four sit on their pads, J2's key faces the edge.
- Extended parts each carry JLC's one-off feeder fee; there are about 17 of them.

## Before you order

1. **Stock.** BOM line items were in stock on 2026-10-13. The tightest were the SMAJ22A
   (C283896, ~1 k) and the DC3 box header (C5156668, ~580). Re-check them in the BOM step.
2. **J2 orientation, one last time.** Look at the silkscreen in JLC's viewer: J2's pin 1 (square
   pad) is bottom-left, in the row along the board edge, and the key slot faces that edge. A
   standard 26-way IDC plug then puts ribbon wire 1 (the stripe) on pin 1.
3. **The Amp4's top header.** The HAT's J1 sits 5.4 mm inboard of the Pi's own header, on the
   Amp4 Pro's offset top header. Confirmed on the bench (2026-10-13) against HiFiBerry's model.

## Bring-up (bench, before the cabinet)

1. **No Pi, no amp.** Plug USB-C from a 65 W PD charger into J3 (right edge, the Pi's port side).
   The yellow **PD** LED lights. Then:
   - TP **VBUS** reads 20 V;
   - the green **20V** LED lights;
   - TP **20V** (VOUT) reads 20 V;
   - J6 (bottom-left, by the notch) reads 20 V, + on the left pin (marked on the silkscreen).
   A 5 V-only USB supply must leave 20V dark.
2. Fit J6's pigtail to the Amp4 Pro's power terminal (+ to +). **Leave the Amp4's barrel jack
   empty.**
3. Stack the Pi + Amp4 Pro + HAT. Boot. Then:
   - `i2cdetect -y 1` should show `20` (MCP23017), `48` (ADS1115) and `4d` (UU once the amp
     driver loads);
   - TP 3V3A reads 3.3 V and the red **3V3A** LED is lit.
4. Deploy the `hat` branch's `config/variants/hasler-hm.yaml` (pins d8–d14, d0 for the tone switch;
   `addr: 0x20`). Re-crimp the HM ribbon into a 26-way plug flush to pin 1 and plug it into J2.
   Run `scripts/panel-probe.py`.

## Debug aids on the board

- J4 (2×8): 5V, 3V3, GND, I²C, UART (GPIO 14/15), GPIO 5/6/12/13/16/26, 3V3A. The pinout is
  printed next to the left corner branding.
- Qwiic (J5): 3.3 V I²C.
- Test points on every rail, I²C, INT, PG, EN and the two pot wipers, all labelled. 5V is by
  the LDO at the left; VBUS, 20V, PG and EN are in the power column.
- Solder jumpers (labelled):
  - JP1: 3V3A from the LDO or the Pi. Cut 1–2 before bridging 2–3.
  - JP2–JP4: MCP23017 address.
  - JP5: INT → GPIO 23.
  - JP6: RDY → GPIO 24.
  - JP7: RST ← GPIO 25 (open).
  - JP8: ADS1115 ADDR.
  - JP9/JP10: the CH224A's I²C (open; it answers at 0x22/0x23, inside the MCP's range).
- The J2 pinout, with HM and Excelsior signals, is on the bottom silkscreen.

## Checks already done

- ERC 0, DRC 0 (all severities, schematic parity), 0 unconnected. `silk_edge_clearance` is off
  on purpose (see `docs/hat/DESIGN.md`).
- Every IC pinout and polarity was checked against its LCSC datasheet by an independent review.
  Those found the AZ431's SOT-23 pinout differs from KiCad's TL431 symbol; the project uses its
  own symbol.
- Renders: `renders/hasler-hat-top.png`, `renders/hasler-hat-bottom.png`.
