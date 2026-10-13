# Hasler HAT — design brief

A board to order (fab + assembly) that replaces today's hand-built stripboard: it sits on
the Raspberry Pi 4B + HiFiBerry Amp4 Pro stack, takes **USB-C power**, and carries
everything the prototype board does. Written 2026-10-13 from the working bench build.

## What exists and works today (the reference)

- **Pi 4B** + **HiFiBerry Amp4 Pro** (TAS5756 DAC/amp, I²C `0x4d`, I²S on GPIO 18–21,
  hardware mute on **GPIO 4**). The amp powers the Pi from its own 5 V rail. Its DC input
  is fed from a USB-C PD trigger at **20 V** (amp absolute max **24 V**). See
  `docs/hardware.md` (power, protection: bulk cap + SMAJ/SMBJ22A TVS at the amp input).
- **Prototype board** (`docs/prototype/`, Paper page "4 · Prototype board"):
  - **MCP23017** (green breakout, I²C `0x20`, A0–A2 to GND, RST pulled up): keys,
    PU, the tone knob's end switch. **PA7/PB7 cannot be inputs** (silicon erratum) — don't use them.
  - **ADS1115** (`0x48`, ADDR to GND): pots — **A0 tone wiper, A1 volume wiper**; A2/A3 spare.
  - **RGB pilot lamp**, common **anode** (to 3.3 V): **red GPIO 17 (220 Ω), green GPIO 22
    (100 Ω), blue GPIO 27 (100 Ω)**, GPIO low = lit.
  - 220 µF + 100 nF on the 3.3 V rail entry.
  - Pi side uses only the odd header row: 1 3V3, 3 SDA, 5 SCL, 9 GND, 11/13/15 lamp; pin 7 (GPIO 4) left free for the amp's mute.
- Software: `daemon/haslerd` (Python). Pin assignments live in the variant descriptor
  (`config/variants/hasler-hm.yaml`; `excelsior.yaml` for the bigger set) — any MCP pin
  can be any key, so routing convenience beats a fixed pin order. `addr: auto` finds the
  MCP23017 anywhere in 0x20–0x27.

## J2 — the Hasler connector (the one thing that must not change)

Each cabinet ends in a **female IDC ribbon plug**. The HM's is 16-way and has been
**rewired to this pinout** (pin numbers are the **IDC standard**: pin 1 = the plug's first
wire / triangle, pin 2 opposite it, odd and even rows alternating):

| Pin | Signal | Board side |
|---|---|---|
| 1 | K1 | MCP input |
| 2 | GND — keys' shared return + tone end switch | GND |
| 3–8 | K2, K3, K4, K5, K6, PU | MCP inputs |
| 9 | tone wiper | ADS A0 |
| 10 | volume wiper | ADS A1 |
| 11 | pots' low end | GND |
| 12 | pots' high end + LED common anode | 3.3 V |
| 13 | LED red | via 220 Ω to GPIO 17 |
| 14 | tone end switch (other side on pin 2) | MCP input (PB0 today) |
| 15 | LED green | via 100 Ω to GPIO 22 |
| 16 | LED blue | via 100 Ω to GPIO 27 |
| 17–26 | expansion for the **Excelsior** (26-way): 6 more digital inputs, A2, A3, 3.3 V, GND | MCP / ADS |

The Excelsior has keys 0–6 + Q + Tape + SP (on/off) and four controls (volume, treble,
bass, a 3-position speaker selector) — see `config/variants/excelsior.yaml`. Its exact
pin order is still to be defined: propose one that fits 17–26.

**Lesson learned the hard way:** the prototype's header was laid out mirror-image to what
a female IDC plug can deliver. On the HAT use a **keyed, shrouded 2×13 box header** and
take its pin numbering from the connector datasheet / footprint, checked against the
IDC cable spec — no hand-drawn numbering. A 16-way plug in a 26-way shrouded header does
not fit, so think about how the HM's 16-way and the Excelsior's 26-way both connect
(e.g. a 2×13 header plus a 2×8 header in parallel, or a 2×13 the 16-way can share).

## Known faults on the bench (not the HAT's problem, but context)

Key 3's path on the stripboard (link L11 to PA5) is open; PU's contact chatters; the tone
end switch doesn't reach PB0 yet; the LED is disconnected. The Pi currently runs with
the end switch disabled and PU muted (local edits, not in git).

## Must-haves for the HAT

1. **USB-C power in**: PD sink negotiating **20 V** (fallbacks sensible), feeding the
   Amp4 Pro's DC input with the protection from `docs/hardware.md` (bulk cap, 22 V TVS,
   reverse/inrush as needed). The amp then powers the Pi as today — or argue for a better
   topology (budget the Pi 4B + amp at volume). Never more than 24 V to the amp.
2. MCP23017 + ADS1115 + lamp drivers on-board (chips, not breakouts), same I²C addresses.
3. J2 as above, plus whatever the Excelsior needs.
4. Clean 3.3 V for the pots and the ADC reference; ESD/series protection on the cabinet
   lines (60-year-old wiring, long ribbon).
   **The 3.3 V that leaves for the cabinet (J2 pin 12) must be current-limited** (PTC fuse
   or a load switch with current limit, or its own small LDO) so a short at a pot can't
   collapse the Pi's 3.3 V rail. This happened on the bench on 2026-10-13: while the volume
   pot was being reworked, a 3V3-GND short killed the Pi 4's 3.3 V regulator.
5. Stacks with the Amp4 Pro (which already occupies the Pi's header and has its own
   EEPROM): check its pass-through header, height, and the DC jack / speaker terminal
   positions so nothing collides. Leave GPIO 2/3 (shared I²C), 4, 18–21 to the amp.
6. Fits the Raspberry Pi HAT mechanical outline and mounting holes.
7. Orderable: KiCad project, BOM with LCSC part numbers (JLCPCB assembly, basic parts
   where possible), Gerbers, pick-and-place, and a short ordering note.

## Files to read first

- `docs/hardware.md`, `docs/build-guide.md`, `docs/variants/hasler-hm.md`
- `docs/prototype/README.md`, `stripboard_layout.json`, `schematic_blockA.svg`, `j2_plug.svg`
  (the J2 drawing shows the *old* numbering — trust the table above)
- `config/variants/hasler-hm.yaml`, `config/variants/excelsior.yaml`
- `daemon/haslerd/panel/` (backends), `daemon/haslerd/hw/gpio_lamp.py`
- Paper file "Hasler Music Box — Plan & Wiring": pages 2 (Hardware) and 4 (Prototype board)
