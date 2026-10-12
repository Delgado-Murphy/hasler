"""Write hasler-hat.kicad_sch and the project symbol library from design.py.

Every connected pin gets a net label at its end; every unused pin a no-connect
flag. Symbols are grouped in titled blocks. Run with any Python 3.9+:

    python3 make_sch.py ..        # writes ../hasler-hat.kicad_sch, ../hasler.kicad_sym
"""

import copy
import os
import sys
import uuid as _uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design  # noqa: E402
from sexpr import Str, dump, find, first, parse  # noqa: E402

KLIB = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols/"
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..")
PROJECT = "hasler-hat"
NS = _uuid.UUID("6b3c0d1e-4a1f-4e55-9a39-0c4d2b7e1a10")


def uid(*key):
    """Stable UUIDs, so regenerating doesn't churn the file or break PCB links."""
    return str(_uuid.uuid5(NS, "/".join(map(str, key))))


ROOT = uid("root")

# ------------------------------------------------------------ project library


def pin(num, name, typ, x, y, ang, length=2.54):
    return ["pin", typ, "line", ["at", x, y, ang], ["length", length],
            ["name", Str(name), ["effects", ["font", ["size", 1.27, 1.27]]]],
            ["number", Str(num), ["effects", ["font", ["size", 1.27, 1.27]]]]]


def prop(name, value, x=0, y=0, hide=False):
    p = ["property", Str(name), Str(value), ["at", x, y, 0],
         ["effects", ["font", ["size", 1.27, 1.27]]]]
    if hide:
        p[-1].append(["hide", "yes"])
    return p


def ch224a():
    L, Rx = -12.7, 12.7
    pins = [("1", "VHV", "power_in", L, 7.62, 0), ("8", "VBUS", "input", L, 5.08, 0),
            ("7", "CC1", "bidirectional", L, 0, 0), ("6", "CC2", "bidirectional", L, -2.54, 0),
            ("4", "DP", "bidirectional", L, -5.08, 0), ("5", "DM", "bidirectional", L, -7.62, 0),
            ("9", "CFG1", "input", Rx, 5.08, 180), ("2", "CFG2/SCL", "bidirectional", Rx, 0, 180),
            ("3", "CFG3/SDA", "bidirectional", Rx, -2.54, 180),
            ("10", "PG", "open_collector", Rx, -7.62, 180),
            ("11", "GND", "power_in", 0, -12.7, 90)]
    body = ["symbol", Str("CH224A_0_1"),
            ["rectangle", ["start", -10.16, 10.16], ["end", 10.16, -10.16],
             ["stroke", ["width", 0.254], ["type", "default"]], ["fill", ["type", "background"]]]]
    unit = ["symbol", Str("CH224A_1_1")] + [pin(n, nm, t, x, y, a) for n, nm, t, x, y, a in pins]
    return ["symbol", Str("CH224A"), ["exclude_from_sim", "no"], ["in_bom", "yes"], ["on_board", "yes"],
            prop("Reference", "U", 0, 11.43), prop("Value", "CH224A", 0, -13.97),
            prop("Footprint", "Package_SO:SSOP-10-1EP_3.9x4.9mm_P1mm_EP2.1x3.3mm", hide=True),
            prop("Datasheet", "https://www.wch-ic.com/downloads/CH224DS1_PDF.html", hide=True),
            prop("Description", "USB PD sink controller, ESSOP-10 (EP = pin 11)", hide=True),
            body, unit]


def az431(tl431):
    """TL431DBZ's drawing with the AZ431 SOT23 pinout: 1 REF, 2 K, 3 A."""
    s = copy.deepcopy(tl431)
    s[1] = Str("AZ431_SOT23")
    renum = {"K": "2", "REF": "1", "A": "3"}
    for sub in find(s, "symbol"):
        sub[1] = Str(sub[1].replace("TL431DBZ", "AZ431_SOT23"))
        for p in find(sub, "pin"):
            first(p, "number")[1] = Str(renum[first(p, "name")[1]])
    for p in find(s, "property"):
        if p[1] == "Value":
            p[2] = Str("AZ431_SOT23")
        if p[1] == "Description":
            p[2] = Str("2.5 V shunt reference, SOT23 pinout 1 REF / 2 K / 3 A (Diodes AZ431)")
    return s


_libcache = {}


def kicad_symbol(lib_id):
    lib, name = lib_id.split(":")
    if lib == "hasler":
        return HASLER[name]
    if lib not in _libcache:
        t = parse(open(KLIB + lib + ".kicad_sym").read())[0]
        _libcache[lib] = {s[1]: s for s in find(t, "symbol")}
    syms = _libcache[lib]
    s = copy.deepcopy(syms[name])
    ext = first(s, "extends")
    if ext:  # flatten: base graphics + pins, derived properties
        base = copy.deepcopy(syms[ext[1]])
        props = {p[1]: p for p in find(s, "property")}
        out = [x for x in base if not (isinstance(x, list) and x and x[0] == "property")]
        out[1] = Str(name)
        for p in find(base, "property"):
            out.insert(2, props.get(p[1], p))
        for sub in find(out, "symbol"):
            sub[1] = Str(sub[1].replace(ext[1], name, 1))
        s = out
    return s


_tl = parse(open(KLIB + "Reference_Voltage.kicad_sym").read())[0]
HASLER = {"CH224A": ch224a(),
          "AZ431_SOT23": az431([s for s in find(_tl, "symbol") if s[1] == "TL431DBZ"][0])}


def write_lib():
    lib = ["kicad_symbol_lib", ["version", 20241209], ["generator", Str("hasler_gen")],
           ["generator_version", Str("9.0")]] + list(HASLER.values())
    with open(os.path.join(OUT, "hasler.kicad_sym"), "w") as f:
        f.write(dump(lib) + "\n")


# ------------------------------------------------------------- geometry


def sym_pins(sym):
    """[(unit, number, name, type, x, y, angle)] in library coordinates."""
    out = []
    for sub in find(sym, "symbol"):
        unit = int(sub[1].rsplit("_", 2)[-2])
        for p in find(sub, "pin"):
            at = first(p, "at")
            out.append((unit, first(p, "number")[1], first(p, "name")[1], p[1],
                        float(at[1]), float(at[2]), int(float(at[3]))))
    return out


def sym_units(sym):
    us = sorted({u for u, *_ in sym_pins(sym) if u > 0})
    return us or [1]


def bbox(sym, unit):
    xs, ys = [0.0], [0.0]
    for sub in find(sym, "symbol"):
        u = int(sub[1].rsplit("_", 2)[-2])
        if u not in (0, unit):
            continue
        for g in sub:
            if not isinstance(g, list):
                continue
            for key in ("start", "end", "center"):
                e = first(g, key)
                if e:
                    xs.append(float(e[1])); ys.append(float(e[2]))
            pts = first(g, "pts")
            if pts:
                for xy in find(pts, "xy"):
                    xs.append(float(xy[1])); ys.append(float(xy[2]))
            if g[0] == "pin":
                at = first(g, "at")
                xs.append(float(at[1])); ys.append(float(at[2]))
    return min(xs), max(xs), min(ys), max(ys)


# ------------------------------------------------------------- schematic

BLOCKS = [  # (key, title, column, width mm)
    ("power", "USB-C PD in (CH224A, 20 V)", 0, 150),
    ("window", "Window 10.9-22.7 V and load switch", 0, 150),
    ("out", "20 V to the Amp4 Pro", 0, 150),
    ("3v3a", "3V3A - cabinet and ADC rail", 0, 150),
    ("pi", "Pi header (via the Amp4's top header)", 1, 120),
    ("mcp", "MCP23017 - keys (0x20)", 1, 120),
    ("ads", "ADS1115 - pots (0x48)", 1, 120),
    ("j2", "J2 - Hasler connector (IDC, DIN 41651)", 2, 140),
    ("lines", "Cabinet line protection", 2, 140),
    ("debug", "Debug / expansion", 3, 110),
    ("test", "Test points", 3, 110),
    ("mech", "Mechanical", 3, 110),
]
COLX = [20, 190, 330, 490]
GRID = 2.54


def snap(v):
    return round(v / GRID) * GRID


def label_len(net):
    return 1.0 * len(net) + 3


def main():
    write_lib()
    ns = design.nets()
    lib_symbols = {}
    items = []
    nc_flags = []
    labels = []
    texts = []
    rects = []
    col_y = [40.0] * len(COLX)

    def place_unit(c, sym, unit, x, y):
        pins = [p for p in sym_pins(sym) if p[0] in (0, unit)]
        seen = {}
        for _, num, name, typ, px, py, ang in pins:
            sx, sy = round(x + px, 2), round(y - py, 2)
            net = c["pins"].get(num)
            key = (sx, sy)
            if net is None:
                if typ == "no_connect" or key in seen:
                    continue
                nc_flags.append(["no_connect", ["at", sx, sy], ["uuid", Str(uid("nc", c["ref"], num))]])
                seen[key] = None
                continue
            if key in seen:
                assert seen[key] == net, (c["ref"], num, net, seen[key])
                continue
            seen[key] = net
            out = (ang + 180) % 360
            just = ["left", "bottom"] if out in (0, 90) else ["right", "bottom"]
            labels.append(["label", Str(net), ["at", sx, sy, out], ["fields_autoplaced", "yes"],
                           ["effects", ["font", ["size", 1.27, 1.27]], ["justify"] + just],
                           ["uuid", Str(uid("lbl", c["ref"], num))]])
        unit_pins = [p[1] for p in pins]
        lid = c["lib_id"]
        x0, x1, y0, y1 = bbox(sym, unit)
        props = [("Reference", c["ref"], x + x1 + 1.5, y - y1 - 1.5, False),
                 ("Value", c["value"], x + x1 + 1.5, y - y1 + 0.5, False),
                 ("Footprint", c["footprint"], x, y, True),
                 ("Datasheet", "", x, y, True),
                 ("Description", c["desc"], x, y, True),
                 ("LCSC", c["lcsc"] or "", x, y, True),
                 ("MPN", c["mpn"], x, y, True)]
        inst = ["symbol", ["lib_id", Str(lid)], ["at", x, y, 0], ["unit", unit],
                ["exclude_from_sim", "no"], ["in_bom", "no" if c["lcsc"] is None else "yes"],
                ["on_board", "yes"], ["dnp", "yes" if c["dnp"] else "no"],
                ["fields_autoplaced", "yes"], ["uuid", Str(uid("sym", c["ref"], unit))]]
        for name, val, px, py, hide in props:
            e = ["effects", ["font", ["size", 1.27, 1.27]], ["justify", "left"]]
            if hide:
                e.append(["hide", "yes"])
            inst.append(["property", Str(name), Str(val), ["at", round(px, 2), round(py, 2), 0], e])
        for n in unit_pins:
            inst.append(["pin", Str(n), ["uuid", Str(uid("pin", c["ref"], unit, n))]])
        inst.append(["instances", ["project", Str(PROJECT),
                     ["path", Str("/" + ROOT), ["reference", Str(c["ref"])], ["unit", unit]]]])
        items.append(inst)

    def footprint_of(c, sym, unit):
        x0, x1, y0, y1 = bbox(sym, unit)
        pins = [p for p in sym_pins(sym) if p[0] in (0, unit)]
        lpad = rpad = tpad = bpad = 1.5
        for _, num, _, _, px, py, ang in pins:
            net = c["pins"].get(num, "")
            ln = label_len(net) if net else 1
            out = (ang + 180) % 360
            if out == 180: lpad = max(lpad, ln)
            if out == 0: rpad = max(rpad, ln)
            if out == 90: tpad = max(tpad, ln)
            if out == 270: bpad = max(bpad, ln)
        rpad = max(rpad, 4 + 1.2 * max(len(c["ref"]), len(c["value"])))
        return (x0 - lpad, x1 + rpad, y0 - bpad, y1 + tpad + 3)

    by_block = {}
    for c in design.COMPONENTS:
        by_block.setdefault(c["block"], []).append(c)

    for key, title, col, width in BLOCKS:
        parts = by_block.get(key, [])
        if not parts:
            continue
        bx, by = COLX[col], col_y[col]
        x, y, row_h = bx + 4, by + 10, 0
        for c in parts:
            sym = kicad_symbol(c["lib_id"])
            lib_symbols[c["lib_id"]] = sym
            for unit in sym_units(sym):
                l, r, b, t = footprint_of(c, sym, unit)
                w, h = r - l, t - b
                if x + w > bx + width and x > bx + 4:
                    x, y, row_h = bx + 4, y + row_h, 0
                place_unit(c, sym, unit, snap(x - l), snap(y + t))
                x += w + 2
                row_h = max(row_h, h + 2)
        height = (y + row_h) - by + 4
        rects.append(["rectangle", ["start", bx, by], ["end", bx + width + 4, by + height],
                      ["stroke", ["width", 0.3], ["type", "dash"]], ["fill", ["type", "none"]],
                      ["uuid", Str(uid("rect", key))]])
        texts.append(["text", Str(title), ["exclude_from_sim", "no"], ["at", bx + 2, by + 4, 0],
                      ["effects", ["font", ["size", 2.2, 2.2], ["bold", "yes"]], ["justify", "left"]],
                      ["uuid", Str(uid("title", key))]])
        col_y[col] = by + height + 8

    # PWR_FLAGs, in the debug column
    flag_sym = kicad_symbol("power:PWR_FLAG")
    lib_symbols["power:PWR_FLAG"] = flag_sym
    fx, fy = COLX[3] + 10, col_y[3] + 10
    texts.append(["text", Str("Power flags: these nets are driven off-sheet (USB, the Pi, the amp)"),
                  ["exclude_from_sim", "no"], ["at", COLX[3], fy - 6, 0],
                  ["effects", ["font", ["size", 1.5, 1.5]], ["justify", "left"]],
                  ["uuid", Str(uid("flagtext"))]])
    for i, net in enumerate(design.PWR_FLAGS):
        x, y = snap(fx + 22 * i), snap(fy + 6)
        items.append(["symbol", ["lib_id", Str("power:PWR_FLAG")], ["at", x, y, 0], ["unit", 1],
                      ["exclude_from_sim", "no"], ["in_bom", "no"], ["on_board", "no"], ["dnp", "no"],
                      ["uuid", Str(uid("flag", net))],
                      ["property", Str("Reference"), Str("#FLG0%d" % (i + 1)), ["at", x, y + 3, 0],
                       ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]],
                      ["property", Str("Value"), Str("PWR_FLAG"), ["at", x, y - 3.8, 0],
                       ["effects", ["font", ["size", 1.27, 1.27]]]],
                      ["property", Str("Footprint"), Str(""), ["at", x, y, 0],
                       ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]],
                      ["property", Str("Datasheet"), Str(""), ["at", x, y, 0],
                       ["effects", ["font", ["size", 1.27, 1.27]], ["hide", "yes"]]],
                      ["pin", Str("1"), ["uuid", Str(uid("flagpin", net))]],
                      ["instances", ["project", Str(PROJECT),
                                     ["path", Str("/" + ROOT), ["reference", Str("#FLG0%d" % (i + 1))],
                                      ["unit", 1]]]]])
        labels.append(["label", Str(net), ["at", x, y, 270], ["fields_autoplaced", "yes"],
                       ["effects", ["font", ["size", 1.27, 1.27]], ["justify", "right", "bottom"]],
                       ["uuid", Str(uid("flaglbl", net))]])

    notes = [
        "Hasler HAT rev %s - %s" % (design.REV, design.COMPANY),
        "Generated from hardware/hat/gen/design.py - edit that, not this sheet.",
        "J2 numbering is the IDC standard (DIN 41651): pin 1 next to the key slot, odd/even rows alternate.",
        "A female IDC plug lands ribbon wire n on pin n. The HM's 16-wire ribbon goes in a 26-way plug flush to pin 1.",
        "AZ431 SOT23 pinout is 1 REF / 2 K / 3 A - NOT the TL431DBZ's. Symbol hasler:AZ431_SOT23.",
        "MCP23017 GPA7/GPB7 are output-only on current silicon: left unconnected.",
    ]
    for i, n in enumerate(notes):
        texts.append(["text", Str(n), ["exclude_from_sim", "no"], ["at", 20, 14 + 3.2 * i, 0],
                      ["effects", ["font", ["size", 1.8 if i == 0 else 1.4, 1.8 if i == 0 else 1.4]],
                       ["justify", "left"]], ["uuid", Str(uid("note", i))]])

    w = max(COLX[c] + BLOCKS[[b[2] for b in BLOCKS].index(c)][3] for c in range(len(COLX))) + 40
    h = max(col_y) + 30
    lib_list = []
    for lid, s in sorted(lib_symbols.items()):
        s = copy.deepcopy(s)
        s[1] = Str(lid)
        lib_list.append(s)
    sch = ["kicad_sch", ["version", 20250114], ["generator", Str("eeschema")],
           ["generator_version", Str("9.0")], ["uuid", Str(ROOT)],
           ["paper", Str("User"), round(w), round(h)],
           ["title_block", ["title", Str(design.TITLE)], ["date", Str("2026-10-13")],
            ["rev", Str(design.REV)], ["company", Str(design.COMPANY)],
            ["comment", 1, Str("USB-C PD 20 V for the Amp4 Pro, MCP23017 keys, ADS1115 pots, RGB lamp")],
            ["comment", 2, Str("Generated by hardware/hat/gen/make_sch.py from design.py")]],
           ["lib_symbols"] + lib_list] + rects + texts + nc_flags + labels + items + \
          [["sheet_instances", ["path", Str("/"), ["page", Str("1")]]], ["embedded_fonts", "no"]]
    with open(os.path.join(OUT, PROJECT + ".kicad_sch"), "w") as f:
        f.write(dump(sch) + "\n")
    print("schematic: %d symbols, %d labels, %d no-connects, %d nets, sheet %dx%d mm"
          % (len(items), len(labels), len(nc_flags), len(ns), w, h))


if __name__ == "__main__":
    main()
