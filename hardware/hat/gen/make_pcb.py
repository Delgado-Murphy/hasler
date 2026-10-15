"""Build hasler-hat.kicad_pcb from design.py (placement, outline, rules, silkscreen).

Runs under KiCad's Python (pcbnew). Two stages:

    kpython make_pcb.py place ..     # board with parts placed, unrouted -> also writes .dsn
    kpython make_pcb.py finish ..    # import Freerouting's .ses, pour planes, silkscreen

Coordinates below are "HAT local": mm from the board's top-left corner (the
Pi-header edge, SD-card end), y down. The board is 85 x 41: the Pi's full length so the
USB-C lines up with the Pi's USB/Ethernet face, cut short at the Amp4 Pro's terminal
strip (docs/hat/DESIGN.md). Mounting holes and header follow the HAT spec.
"""

import math
import os
import re
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design  # noqa: E402
from make_sch import uid  # noqa: E402  (same UUIDs as the schematic)

FPLIB = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/"
OUT = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), ".."))
PCB = os.path.join(OUT, "hasler-hat.kicad_pcb")
OX, OY = 100.0, 100.0          # board origin on the KiCad sheet
VIA_D, VIA_DRILL, CLR = 0.6, 0.3, 0.2
MIN_W = 0.127                  # tracks are brought up to this where they fit (JLC's minimum: 0.09)
SHAVE_MIN = 0.12               # ...and a track a hair too close to a pad is shaved no further than this
W, H = 85.0, 41.0


def V(x, y):
    return pcbnew.VECTOR2I_MM(OX + x, OY + y)


def mm(v):
    return pcbnew.ToMM(v)


# --------------------------------------------------------------- placement
# ref: (x, y, rotation, side). Fixed parts first; "near" parts are legalized.
J1_PIN1 = (8.37, 10.19)       # Amp4 top header: inner row, SD-card end (DESIGN.md)
J2_PIN1 = (31.9, 37.3)        # odd row on the board-edge side, key slot facing the edge
# Notch in the bottom edge over the Amp4 Pro's 8 mm electrolytic. Its leads are at HAT x 15.13
# and 18.93, y_up 18.2 (holes in HiFiBerry's STEP), so standing it covers x ~12.9-21.2,
# y_up ~14.0-22.4, and it is taller than the HAT's underside (~11 mm above the amp).
# ~1.1-1.4 mm of margin all round.
NOTCH = (11.5, 32.5, 22.5)    # x0, y0, x1 (open to the bottom edge)

FIXED = {
    "H1": (3.5, 3.5, 0), "H2": (61.5, 3.5, 0),
    "J5": (32.8, 3.1, 180),           # Qwiic, mouth to the top edge
    # power column (x 65..85, over the Pi's USB/Ethernet), rows following the power
    # flow: USB-C/CH224A -> TVS -> eFuse -> TVS (C7 pads, not fitted) -> J6. Hand-placed.
    "D2": (66.0, 2.0, 270), "D6": (67.7, 2.0, 270), "U3": (72.6, 3.2, 0),   # LED pad 2 down to its resistor
    "R2": (66.0, 5.3, 90), "R10": (67.7, 5.3, 90),
    "C1": (72.6, 6.9, 180),           # VHV/VBUS decoupling right under U3, VBUS pad toward pin 8
    "JP9": (66.6, 9.6, 90), "JP10": (69.4, 9.6, 90), "R1": (71.6, 9.6, 90),
    "TP11": (75.95, 9.6, 0),
    "TP1": (70.2, 13.6, 0), "C2": (73.2, 13.6, 0), "D1": (80.6, 13.7, 0),
    # eFuse: IN/OUT are two 0.3 mm strips 0.2 mm apart (fp x -/+0.25), fed from their ends only:
    # IN from the top end (D1 above), OUT from the bottom end (D5 below). Control pins 1-4 left,
    # 7-10 right.
    "U4": (80.6, 19.6, 0),
    "C5": (79.2, 16.6, 0),            # IN ceramic: its VBUS pad on the 0.8 mm run into the IN strip
    "C17": (82.2, 22.05, 0),          # OUT ceramic: its VOUT pad on the run out of the OUT strip
    "C8": (71.3, 30.95, 0), "TP2": (70.0, 35.0, 0), "D5": (81.55, 28.15, 270),  # upright below U4's OUT strip
    "C7": (79.3, 36.4, 0),
    "J6": (4.6, 36.4, 0),             # bottom-left: wire entry down, ~25 mm to the Amp4 power terminal
    # logic: the MCP23017 upright, pin-1 end (all GPA/GPB pins) facing the line rows
    "U1": (41.0, 16.75, 180),
    "U2": (53.5, 13.6, 0),
    "U6": (5.0, 15.0, 0),
}
# Row behind J2: ESD arrays (upright) interleaved with the 10k pull-up arrays (cabinet side)
for i, ref in enumerate(("U7", "RN6", "U8", "RN7", "U9", "RN8", "U10", "RN9", "U11", "U12")):
    FIXED[ref] = (28.6 + 3.65 * i, 28.9, 90 if ref.startswith("U") else 0)
# ESD arrays' VCC bypass (C38-C43 for U7-U12): in the gaps of the 1k row just above them,
# U7's to its left, U12's above it (its neighbour is U11, not an array)
# (upright, against the side nearest their array, so each gap keeps a 2 mm routing channel)
for ref, xy, rot in zip(("C38", "C39", "C40", "C41", "C42", "C43"),
                        ((25.8, 27.8), (34.5, 24.9), (41.5, 24.9), (48.5, 24.9), (55.5, 24.9), (61.5, 25.0)),
                        (90, 90, 90, 90, 90, 0)):
    FIXED[ref] = (xy[0], xy[1], rot)
# 1k series arrays, spaced out under the MCP: pins 1-4 (cabinet) down, 5-8 (MCP) up
for i, ref in enumerate(("RN1", "RN2", "RN5", "RN3", "RN4")):
    FIXED[ref] = (30.0 + 7.0 * i, 24.6, 90)
# line caps (0402) either side of the MCP, nearest their arrays
_LEFT = ["K1", "K2", "K3", "K4", "K5", "K6", "PU", "TSW", "A0"]
_RIGHT = ["A1", "X1", "X2", "X3", "X4", "X5", "X6", "A2", "A3"]
for side, sigs, x0 in ((0, _LEFT, 24.4), (1, _RIGHT, 46.6)):
    for j, sig in enumerate(sigs):
        ref = "C%d" % (20 + design.SERIES.index(sig))
        FIXED[ref] = (x0 + 1.3 * j, 21.85, 90)

ZONES = {  # allowed rectangles for legalized parts
    "pwr": (65.2, 0.3, 84.7, 40.7),
    "lft": (0.3, 11.3, 27.9, 30.9),
    "mid": (22.8, 11.3, 64.9, 31.0),
    "top": (6.6, 0.3, 58.4, 6.6),
}
NEAR = {  # ref: (zone, x, y, rotation)
    # left corner: 3V3A and the MCP's strapping
    "C9": ("lft", 1.6, 15.0, 90), "C10": ("lft", 8.4, 15.0, 90), "JP1": ("lft", 5.0, 19.0, 0),
    "C11": ("lft", 5.0, 22.0, 0), "C12": ("lft", 8.4, 22.0, 90), "C13": ("lft", 1.6, 22.0, 90),
    "R11": ("lft", 11.6, 15.0, 90), "D7": ("lft", 11.6, 18.6, 90),
    "JP2": ("lft", 17.0, 13.4, 90), "JP3": ("lft", 20.0, 13.4, 90), "JP4": ("lft", 23.0, 13.4, 90),
    "JP5": ("lft", 17.0, 19.0, 90), "JP7": ("lft", 20.0, 19.0, 90), "R12": ("lft", 23.0, 19.0, 90),
    "C14": ("mid", 47.0, 18.0, 90),
    # ADS1115
    "C15": ("mid", 53.5, 16.6, 0), "C16": ("mid", 53.5, 18.4, 0), "R13": ("mid", 58.0, 13.6, 90),
    "JP6": ("mid", 60.6, 13.6, 90), "JP8": ("mid", 49.0, 13.6, 90),
    "R20": ("mid", 59.0, 18.6, 0), "R21": ("mid", 59.0, 20.4, 0), "R22": ("mid", 62.6, 18.6, 90),
}
NEAR["TP3"] = ("lft", 11.0, 12.4, 0)   # 5V, at the LDO input (J4 pin 1 also carries it)
# eFuse dividers left of it (EN/UVLO, OVLO, PGTH, PG pull-up), timing parts right of it
NEAR.update({
    "R3": ("pwr", 77.0, 21.8, 90), "R4": ("pwr", 75.4, 21.8, 90), "R5": ("pwr", 73.8, 21.8, 90),
    "R6": ("pwr", 72.2, 21.8, 90), "R7": ("pwr", 70.6, 21.8, 90), "R16": ("pwr", 69.0, 21.8, 90),
    "JP11": ("pwr", 67.0, 21.8, 90),
    "R8": ("pwr", 77.0, 25.2, 90), "R9": ("pwr", 75.4, 25.2, 90), "R15": ("pwr", 73.8, 25.2, 90),
    "TP12": ("pwr", 71.6, 25.2, 0),
    "C4": ("pwr", 83.6, 17.2, 90), "R14": ("pwr", 83.6, 19.6, 90), "C3": ("pwr", 83.3, 23.6, 0),
})
TOP_TPS = ["TP4", "TP5", "TP6", "TP7", "TP8", "TP9", "TP10", "TP13", "TP14"]
for i, ref in enumerate(TOP_TPS):
    NEAR[ref] = ("top", 39.6 + 3.6 * (i % 5), 1.4 if i < 5 else 4.4, 0)


def schematic_nets():
    """{(ref, pin): net} from kicad-cli's netlist export of the generated schematic."""
    import subprocess
    from sexpr import find, first, parse
    out = os.path.join(OUT, "build", "hasler-hat.net")
    subprocess.run(["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli", "sch", "export", "netlist",
                    "--format", "kicadsexpr", "-o", out, os.path.join(OUT, "hasler-hat.kicad_sch")],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t = parse(open(out).read())[0]
    res = {}
    for n in find(first(t, "nets"), "net"):
        name = first(n, "name")[1]
        for node in find(n, "node"):
            res[(first(node, "ref")[1], first(node, "pin")[1])] = str(name)
    return res


def load_fp(fpid):
    lib, name = fpid.split(":")
    path = os.path.join(OUT, "hasler.pretty") if lib == "hasler" else FPLIB + lib + ".pretty"
    fp = pcbnew.FootprintLoad(path, name)
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp


def courtyard(fp):
    cy = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
    bb = cy.BBox() if cy.OutlineCount() else fp.GetBoundingBox(False)
    return [mm(bb.GetLeft()) - OX, mm(bb.GetTop()) - OY, mm(bb.GetRight()) - OX, mm(bb.GetBottom()) - OY]


def pad_xy(fp, num):
    for p in fp.Pads():
        if p.GetNumber() == num:
            v = p.GetPosition()
            return round(mm(v.x) - OX, 3), round(mm(v.y) - OY, 3)
    raise KeyError(num)


def orient_to(fp, a, b, want, flip=False):
    """Rotate (and optionally flip to the bottom) so pad a->b points along `want`."""
    if flip:
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    for ang in range(0, 360, 90):
        fp.SetOrientationDegrees(ang)
        (ax, ay), (bx, by) = pad_xy(fp, a), pad_xy(fp, b)
        d = (round(bx - ax, 2), round(by - ay, 2))
        if d == want:
            return ang
    raise RuntimeError("no orientation for %s" % fp.GetReference())


def move_pad_to(fp, num, x, y):
    px, py = pad_xy(fp, num)
    p = fp.GetPosition()
    fp.SetPosition(pcbnew.VECTOR2I(p.x + pcbnew.FromMM(x - px), p.y + pcbnew.FromMM(y - py)))


def legalize(rects, movable, region, iters=400, gap=0.15):
    """Push overlapping movable rectangles apart; keep each in its region."""
    names = list(rects)
    for _ in range(iters):
        moved = False
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if a not in movable and b not in movable:
                    continue
                ra, rb = rects[a], rects[b]
                ox = min(ra[2], rb[2]) - max(ra[0], rb[0]) + gap
                oy = min(ra[3], rb[3]) - max(ra[1], rb[1]) + gap
                if ox <= 0 or oy <= 0:
                    continue
                moved = True
                ca = ((ra[0] + ra[2]) / 2, (ra[1] + ra[3]) / 2)
                cb = ((rb[0] + rb[2]) / 2, (rb[1] + rb[3]) / 2)
                both = a in movable and b in movable
                share = 0.5 if both else 1.0
                if ox < oy:
                    s = 1 if ca[0] <= cb[0] else -1
                    dx, dy = ox * share * s, 0
                else:
                    s = 1 if ca[1] <= cb[1] else -1
                    dx, dy = 0, oy * share * s
                if a in movable:
                    k = share if both else 1
                    rects[a] = [ra[0] - dx * (1 if both else 1), ra[1] - dy, ra[2] - dx, ra[3] - dy] \
                        if both else [ra[0] - dx, ra[1] - dy, ra[2] - dx, ra[3] - dy]
                if b in movable:
                    rects[b] = [rb[0] + dx, rb[1] + dy, rb[2] + dx, rb[3] + dy]
        for n in movable:  # clamp into region
            r = rects[n]
            x0, y0, x1, y1 = region[n]
            sx = max(0, x0 - r[0]) - max(0, r[2] - x1)
            sy = max(0, y0 - r[1]) - max(0, r[3] - y1)
            rects[n] = [r[0] + sx, r[1] + sy, r[2] + sx, r[3] + sy]
        if not moved:
            return []
    left = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ra, rb = rects[a], rects[b]
            if min(ra[2], rb[2]) - max(ra[0], rb[0]) > 0.01 and min(ra[3], rb[3]) - max(ra[1], rb[1]) > 0.01:
                left.append((a, b))
    return left


def place():
    board = pcbnew.NewBoard(PCB)
    board.SetCopperLayerCount(4)
    board.SetLayerType(pcbnew.In1_Cu, pcbnew.LT_POWER)
    board.SetLayerName(pcbnew.In1_Cu, "In1.GND")
    board.SetLayerName(pcbnew.In2_Cu, "In2.Sig")
    ds = board.GetDesignSettings()
    ds.SetCopperLayerCount(4)
    ds.m_TrackMinWidth = pcbnew.FromMM(0.09)      # JLC 4-layer: 0.09 mm
    ds.m_MinClearance = pcbnew.FromMM(0.1)
    ds.m_ViasMinSize = pcbnew.FromMM(0.5)
    ds.m_MinThroughDrill = pcbnew.FromMM(0.3)
    ds.m_ViasMinAnnularWidth = pcbnew.FromMM(0.1)
    ds.m_CopperEdgeClearance = pcbnew.FromMM(0.3)
    ds.m_HoleClearance = pcbnew.FromMM(0.2)
    ds.m_HoleToHoleMin = pcbnew.FromMM(0.25)
    ds.m_SilkClearance = pcbnew.FromMM(0.0)

    ns = ds.m_NetSettings
    dflt = ns.GetDefaultNetclass()
    dflt.SetClearance(pcbnew.FromMM(0.15))
    dflt.SetTrackWidth(pcbnew.FromMM(0.15))
    dflt.SetViaDiameter(pcbnew.FromMM(0.6))
    dflt.SetViaDrill(pcbnew.FromMM(0.3))
    for name, width, clear, via, drill, nets in (
            ("Power", 0.25, 0.15, 0.6, 0.3, design.POWER_NETS),  # main paths prerouted; widened after
            ("Rail", 0.3, 0.15, 0.6, 0.3, design.RAIL_NETS),
            ("Gnd", 0.3, 0.15, 0.6, 0.3, design.GND_NETS)):
        nc = pcbnew.NETCLASS(name)
        nc.SetClearance(pcbnew.FromMM(clear))
        nc.SetTrackWidth(pcbnew.FromMM(width))
        nc.SetViaDiameter(pcbnew.FromMM(via))
        nc.SetViaDrill(pcbnew.FromMM(drill))
        ns.SetNetclass(name, nc)
        for n in nets:
            ns.SetNetclassPatternAssignment("/" + n, name)

    ds.SetBoardThickness(pcbnew.FromMM(1.6))
    ds.m_TentViasFront = ds.m_TentViasBack = True       # no mask openings on vias

    # outline, 3 mm corner radius on the two free corners
    pts = outline()
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(V(x0, y0)); seg.SetEnd(V(x1, y1))
        seg.SetLayer(pcbnew.Edge_Cuts); seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)

    # Net names exactly as the schematic gives them ("/GND", "unconnected-(...)"), so the
    # board passes DRC's schematic-parity check.
    pin_net = schematic_nets()
    nets = {}
    for n in sorted(set(pin_net.values())):
        ni = pcbnew.NETINFO_ITEM(board, n)
        board.Add(ni)
        nets[n] = ni

    fps = {}
    for c in design.COMPONENTS:
        fp = load_fp(c["footprint"])
        fp.SetReference(c["ref"])
        fp.SetValue(c["value"])
        fp.SetPath(pcbnew.KIID_PATH("/" + uid("sym", c["ref"], 1)))
        fp.SetField("LCSC", c["lcsc"] or "")
        fp.SetField("MPN", c["mpn"])
        for fname in ("LCSC", "MPN"):
            f = fp.GetFieldByName(fname)
            f.SetVisible(False)
            f.SetLayer(pcbnew.F_Fab)
        if c["lcsc"] is None:
            fp.SetExcludedFromBOM(True)
            fp.SetExcludedFromPosFiles(True)
        if c["dnp"]:
            fp.SetDNP(True)
        board.Add(fp)
        for p in fp.Pads():
            n = pin_net.get((c["ref"], p.GetNumber()))
            if n:
                assert n.lstrip("/") == c["pins"].get(p.GetNumber(), n.lstrip("/")), (c["ref"], n)
                p.SetNet(nets[n])
        fps[c["ref"]] = fp
        fp.SetPosition(V(0, 0))

    # J1 on the bottom: pin1 inner row at the SD end, pin2 toward the edge, pin3 along +x
    j1 = fps["J1"]
    orient_to(j1, "1", "3", (2.54, 0.0), flip=True)
    for p in j1.Pads():   # flipping drops the inner layers from the pads; a PTH pad is on all
        p.SetLayerSet(pcbnew.PAD.PTHMask())
    move_pad_to(j1, "1", *J1_PIN1)
    assert pad_xy(j1, "2") == (8.37, 7.65), pad_xy(j1, "2")
    # J2: odd row along the edge, pins increase along +x, even row inboard
    j2 = fps["J2"]
    orient_to(j2, "1", "3", (2.54, 0.0))
    move_pad_to(j2, "1", *J2_PIN1)
    assert pad_xy(j2, "2") == (J2_PIN1[0], round(J2_PIN1[1] - 2.54, 3)), pad_xy(j2, "2")
    # J3 USB-C: mouth (footprint +y) out of the right edge, flush with the Pi's USB/Ethernet
    # face (their jacks overhang the Pi's PCB edge by ~2 mm); front face 1.0 mm over the edge
    j3 = fps["J3"]
    for ang in range(0, 360, 90):
        j3.SetOrientationDegrees(ang)
        if pad_xy(j3, "A1")[0] < -3.9:      # contact pads (fp y -4.04) behind, i.e. left of, the body
            break
    j3.SetPosition(V(82.35, 6.2))
    # J4 debug header in the top strip: pins along +x, even row toward the edge
    j4 = fps["J4"]
    orient_to(j4, "1", "3", (2.54, 0.0))
    move_pad_to(j4, "1", 9.0, 4.6)
    assert pad_xy(j4, "2")[1] < 4.6

    rects, region, movable = {}, {}, set()
    for ref, (x, y, rot) in FIXED.items():
        fp = fps[ref]
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(V(x, y))
    for ref, (zone, x, y, rot) in NEAR.items():
        fp = fps[ref]
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(V(x, y))
        movable.add(ref)
        region[ref] = ZONES[zone]
    unplaced = [r for r in fps if r not in FIXED and r not in NEAR and r not in ("J1", "J2", "J3", "J4")]
    assert not unplaced, unplaced
    for ref, fp in fps.items():
        if ref == "J1":
            continue
        rects[ref] = courtyard(fp)
    # keep top-side parts off J1's through-hole rows and out of the mounting-hole areas
    rects["_J1PINS"] = [6.9, 6.75, 58.1, 11.1]
    rects["_NOTCH"] = [NOTCH[0] - 0.3, NOTCH[1] - 0.3, NOTCH[2] + 0.3, H]
    ok = legalize(rects, movable, region, iters=1500)
    for ref in movable:
        fp = fps[ref]
        old = courtyard(fp)
        new = rects[ref]
        p = fp.GetPosition()
        fp.SetPosition(pcbnew.VECTOR2I(p.x + pcbnew.FromMM(new[0] - old[0]),
                                       p.y + pcbnew.FromMM(new[1] - old[1])))
    print("overlaps left:", ok)

    # Reference designators: small, on the fab layer; silkscreen gets curated labels later
    for ref, fp in fps.items():
        r = fp.Reference()
        r.SetTextSize(pcbnew.VECTOR2I_MM(0.6, 0.6))
        r.SetTextThickness(pcbnew.FromMM(0.1))
        if ref[0] in "RCDQUJ" and not ref.startswith(("J", "U1", "U2", "U3", "U4", "U6")):
            r.SetLayer(pcbnew.B_Fab if fp.IsFlipped() else pcbnew.F_Fab)
        fp.Value().SetVisible(False)

    for fp in fps.values():   # JLC: silkscreen lines >= 0.15 mm
        for g in fp.GraphicalItems():
            if g.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS) and g.GetClass() != "PCB_TEXT" \
                    and mm(g.GetWidth()) < 0.15:
                g.SetWidth(pcbnew.FromMM(0.15))
    for ref in ("H1", "H2"):  # screw heads / metal standoffs: no tracks or vias under them
        hole_keepout(board, *fp_xy(board, ref))
    vout_trunk(board, nets["/VOUT"])
    # Links the router would lose in tight spots, and the VBUS power path, laid first and
    # locked; widen_power() takes the power ones as wide as their surroundings allow.
    preroute(board, "/VBUS", ("U3", "8"), ("J3", "A9"), 0.3)
    preroute(board, "/VBUS", ("J3", "A4"), ("D1", "1"), 0.5)
    efuse_power(board)
    vbus_strap(board)
    # CH224A: VHV (pin 1) and VBUS (pin 8) are on opposite sides of the chip; CC1 has to
    # cross CC2 between the chip and the receptacle. Both under the top layer, laid first.
    hop(board, "/VBUS", ("U3", "1"), ("U3", "8"), pcbnew.B_Cu, w=0.3)
    hop(board, "/CC1", ("U3", "7"), ("J3", "A5"), pcbnew.In2_Cu)
    hop(board, "/USB_DP", ("U3", "4"), ("J3", "B6"), pcbnew.In2_Cu)   # D+ to the chip's far side
    cu_ = Copper(board)
    for x_, y_ in STRAP_VIAS:
        print("VBUS strap: second via: %s" % double_via(cu_, board.FindNet("/VBUS").GetNetCode(), x_, y_))
    # the LEDs straight down to their resistors (1.7 mm), before anything can cross the gap
    preroute(board, "/LED_PD", ("D2", "2"), ("R2", "2"), 0.2)
    preroute(board, "/LED_OUT", ("D6", "2"), ("R10", "2"), 0.2)
    # the CH224A's I2C pins to their jumpers, and the MCP-ADS SDA link: short local runs the
    # router tends to leave in this corner
    preroute(board, "/PD_SCL", ("U3", "2"), ("JP9", "1"), 0.15, required=False)
    preroute(board, "/PD_SDA", ("U3", "3"), ("JP10", "1"), 0.15, required=False)
    preroute(board, "/SDA", ("U1", "13"), ("U2", "9"), 0.15, required=False)
    # The receptacle's two D+ pads sit either side of a D- pad: join them round the pad tips
    j3 = board.FindFootprintByReference("J3")
    cu = Copper(board)
    net = board.FindNet("/USB_DP").GetNetCode()
    b6, a6 = j3.FindPadByNumber("B6"), j3.FindPadByNumber("A6")
    xl = mm(j3.FindPadByNumber("A7").GetBoundingBox().GetRight()) + 0.15 + 0.075 + 0.03
    loop = [(mm(b6.GetPosition().x), mm(b6.GetPosition().y)), (xl, mm(b6.GetPosition().y)),
            (xl, mm(a6.GetPosition().y)), (mm(a6.GetPosition().x), mm(a6.GetPosition().y))]
    assert cu.path_fits(net, pcbnew.F_Cu, loop, 0.15), "D+ loop round the J3 pad tips"
    for p_, q_ in zip(loop, loop[1:]):
        cu.track(net, pcbnew.F_Cu, p_, q_, 0.15, locked=True)
    preroute(board, "/VOUT", ("D5", "1"), ("C7", "1"), 0.8)
    for c_, u_ in (("C41", "U10"), ("C42", "U11")):   # the two that otherwise route long
        preroute(board, "/3V3A", (c_, "1"), (u_, "5"), 0.25, required=False)
    try:   # and join those two ESD supply islands to each other underneath, so neither hangs off
        hop(board, "/3V3A", ("C41", "1"), ("C42", "1"), pcbnew.In2_Cu, w=0.25)
    except (AssertionError, RuntimeError) as e:
        print("3V3A C41-C42 hop:", e)
    try:   # RDY leaves the ADS1115 on the far side from its pull-up and jumper: go under the chip
        hop(board, "/ADS_RDY", ("U2", "2"), ("JP6", "1"), pcbnew.In2_Cu)
    except (AssertionError, RuntimeError) as e:
        print("ADS_RDY hop:", e)
    try:   # SDA from the MCP/ADS pair up to the Qwiic / header side, under the J1 field
        hop(board, "/SDA", ("U1", "13"), ("J5", "3"), pcbnew.In2_Cu, w=0.15)
    except (AssertionError, RuntimeError) as e:
        print("SDA hop:", e)
    # GND fan-out before routing. Not the USB-C: its GND pins sit beside VBUS at 0.5 mm pitch
    # (VBUS must route first), and its plated shell tabs already reach the plane.
    n, failed = stitch_gnd(Copper(board), locked=True, skip=("J3",))
    print("GND fan-out: %d pads, no room at %s" % (n, [p.GetParentFootprint().GetReference() + "."
                                                        + p.GetNumber() for p in failed] or "none"))
    add_zone(board, nets["/GND"], pcbnew.In1_Cu, priority=0, thermal=True)  # solid GND plane
    board.Save(PCB)
    board.Save(os.path.join(OUT, "build", "placed.kicad_pcb"))

    pcbnew.ExportSpecctraDSN(board, os.path.join(OUT, "build", "hasler-hat.dsn"))
    print("placed %d footprints" % len(fps))


VOUT_TRUNK = [(70.3, 36.4), (66.5, 36.4), (66.5, 39.5), (28.6, 39.5), (27.4, 38.3), (27.4, 31.2),
              (4.6, 31.2), (4.6, 36.4)]   # B.Cu: under J2's pins along the edge, over the notch


def hole_keepout(board, cx, cy, r=3.0):
    """Rule area around a mounting hole: no tracks or vias within r (M2.5 head / hex standoff
    reach ~2.9 mm), on every copper layer. The GND pour may still fill it."""
    z = pcbnew.ZONE(board)
    z.SetIsRuleArea(True)
    z.SetDoNotAllowTracks(True)
    z.SetDoNotAllowVias(True)
    z.SetDoNotAllowPads(False)
    z.SetDoNotAllowCopperPour(False)
    z.SetDoNotAllowFootprints(False)
    ls = pcbnew.LSET()
    for l in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu):
        ls.AddLayer(l)
    z.SetLayerSet(ls)
    ol = z.Outline()
    ol.NewOutline()
    for i in range(24):
        a = 2 * math.pi * i / 24
        ol.Append(pcbnew.FromMM(OX + cx + r * math.cos(a)), pcbnew.FromMM(OY + cy + r * math.sin(a)))
    ol.Outline(0).SetClosed(True)
    board.Add(z)


def vbus_strap(board, w=0.9):
    """The USB-C's two VBUS pad pairs (A9/B4 and A4/B9) sit either side of CC and D+/D- at
    0.5 mm pitch, so on the top layer they can only be joined by a thin track. Drop each to
    the bottom layer through a via right behind it and join them there, w mm wide."""
    cu = Copper(board)
    net = board.FindNet("/VBUS").GetNetCode()
    j3 = board.FindFootprintByReference("J3")
    ends = []
    for num in ("A9", "A4"):
        pad = [p for p in j3.Pads() if p.GetNumber() == num][0]
        bb = pad.GetBoundingBox()
        px, py = mm(pad.GetPosition().x), mm(pad.GetPosition().y)
        x_in = mm(bb.GetLeft())                       # the inboard end of the pad (mouth is +x)
        for dx in (0.45, 0.6, 0.8, 1.0, 1.2):
            for dy in (0.0, -0.15, 0.15, -0.3, 0.3):
                x, y = x_in - dx, py + dy
                if cu.fits("pt", (x, y), None, VIA_D / 2, net) and \
                        cu.path_fits(net, pcbnew.F_Cu, [(px, py), (x, y)], 0.5):
                    break
            else:
                continue
            break
        else:
            raise RuntimeError("vbus_strap: no via spot behind J3.%s" % num)
        cu.track(net, pcbnew.F_Cu, (px, py), (x, y), 0.5, locked=True)
        cu.via(net, x, y, locked=True)
        ends.append((x, y))
    STRAP_VIAS[:] = ends              # doubled later, once the CC/D+ hops have their vias
    (ax, ay), (bx, by) = ends
    # Under the pad row first (nothing else is on B.Cu beneath the receptacle), so the strip
    # just behind the pads stays free for CC1/CC2/D+/D-'s vias on their way to the CH224A.
    ux = mm(j3.FindPadByNumber("A5").GetPosition().x) + 0.1
    under = [(ax, ay), (ux, ay + 0.8), (ux, by - 0.8), (bx, by)]
    for path in (under, [(ax, ay), (bx, by)], [(ax, ay), (ax, by), (bx, by)], [(ax, ay), (bx, ay), (bx, by)]):
        if cu.path_fits(net, pcbnew.B_Cu, path, w):
            break
    else:
        raise RuntimeError("vbus_strap: no room on B.Cu")
    for p, q in zip(path, path[1:]):
        cu.track(net, pcbnew.B_Cu, p, q, w, locked=True)
    # ...and a second path to D1 (the TVS, then the eFuse) on B.Cu, in parallel with the
    # top-layer run from the A4/B9 pads, which can only be 0.6 mm wide between J3's pins
    d1 = board.FindFootprintByReference("D1").FindPadByNumber("1")
    dx, dy = mm(d1.GetPosition().x), mm(d1.GetPosition().y)
    half = max(mm(d1.GetBoundingBox().GetWidth()), mm(d1.GetBoundingBox().GetHeight())) / 2
    for x, y in ring(dx, dy, half, steps=(0.4, 0.6, 0.8, 1.0, 1.3), n=32, prefer=math.atan2(by - dy, bx - dx)):
        if not (cu.fits("pt", (x, y), None, VIA_D / 2, net) and
                cu.path_fits(net, pcbnew.F_Cu, [(dx, dy), (x, y)], 0.6)):
            continue
        leg = None
        for cand in ([(bx, by), (x, y)], [(bx, by), (bx, y), (x, y)], [(bx, by), (x, by), (x, y)]):
            if cu.path_fits(net, pcbnew.B_Cu, cand, w):
                leg = cand
                break
        if leg is None:
            leg = grid_route(cu, net, pcbnew.B_Cu, (bx, by), [(x, y)], w=w, window=10.0)
        if leg:
            cu.via(net, x, y, locked=True)
            cu.track(net, pcbnew.F_Cu, (dx, dy), (x, y), 0.6, locked=True)
            for p, q in zip(leg, leg[1:]):
                cu.track(net, pcbnew.B_Cu, p, q, w, locked=True)
            print("VBUS: second path J3 -> D1 on B.Cu, second via: %s" % double_via(cu, net, x, y))
            break
    else:
        print("VBUS: no room for the second J3 -> D1 path")


STRAP_VIAS = []


def double_via(cu, net, x, y, w=0.6, locked=True):
    """A second via beside the one at (x, y), joined to it on the top and bottom layers."""
    for vx, vy in ring(x, y, 0.0, steps=(0.8, 0.95, 1.1), n=24):
        if cu.fits("pt", (vx, vy), None, VIA_D / 2, net) and \
                all(cu.path_fits(net, l, [(x, y), (vx, vy)], w) for l in (pcbnew.F_Cu, pcbnew.B_Cu)):
            cu.via(net, vx, vy, locked=locked)
            for l in (pcbnew.F_Cu, pcbnew.B_Cu):
                cu.track(net, l, (x, y), (vx, vy), w, locked=locked)
            return True
    return False


def efuse_power(board, w=0.8):
    """The eFuse's IN and OUT pads are 0.3 mm strips 0.2 mm apart, reachable only at their ends.
    IN leaves the strip end facing D1 (VBUS), OUT the end facing D5 (VOUT): a 0.3 mm neck along
    the strip axis to clear the package, then w mm to the target, all locked before routing."""
    cu = Copper(board)
    u = board.FindFootprintByReference("U4")
    for num, netname, target in (("5", "/VBUS", ("D1", "1")), ("6", "/VOUT", ("D5", "1"))):
        net = board.FindNet(netname).GetNetCode()
        pad = u.FindPadByNumber(num)
        bb = pad.GetBoundingBox()
        px, py = mm(pad.GetPosition().x), mm(pad.GetPosition().y)
        assert mm(bb.GetHeight()) > mm(bb.GetWidth()), "U4 strips are expected to run along y"
        tp = board.FindFootprintByReference(target[0]).FindPadByNumber(target[1]).GetPosition()
        tx, ty = mm(tp.x), mm(tp.y)
        sgn = -1 if ty < py else 1                      # leave from the end facing the target
        end = mm(bb.GetTop()) if sgn < 0 else mm(bb.GetBottom())
        a, b = (px, end - sgn * 0.15), (px, end + sgn * 0.55)
        assert cu.path_fits(net, pcbnew.F_Cu, [a, b], 0.3), "U4 %s neck" % netname
        cu.track(net, pcbnew.F_Cu, a, b, 0.3, locked=True)
        for ww in (w, 0.6, 0.5):
            path = None
            for cand in ([b, (tx, ty)], [b, (b[0], ty), (tx, ty)], [b, (tx, b[1]), (tx, ty)]):
                if cu.path_fits(net, pcbnew.F_Cu, cand, ww):
                    path = cand
                    break
            if path is None:
                path = grid_route(cu, net, pcbnew.F_Cu, b, [(tx, ty)], w=ww,
                                  window=max(8.0, 2.4 * math.hypot(tx - b[0], ty - b[1])))
            if path:
                break
        assert path, "U4 %s to %s" % (netname, target)
        for p_, q_ in zip(path, path[1:]):
            cu.track(net, pcbnew.F_Cu, p_, q_, ww, locked=True)
        print("eFuse %s: neck 0.3 mm, then %.1f mm to %s.%s" % (netname, ww, *target))


def hop(board, netname, a, b, layer, w=0.2):
    """A locked two-via link between pads a and b through `layer`, for a connection that can't
    stay on the top layer (it has to cross another, or get to the far side of a chip)."""
    cu = Copper(board)
    net = board.FindNet(netname).GetNetCode()
    ends = []
    for ref, num in (a, b):
        pad = board.FindFootprintByReference(ref).FindPadByNumber(num)
        bb = pad.GetBoundingBox()
        px, py = mm(pad.GetPosition().x), mm(pad.GetPosition().y)
        half = max(mm(bb.GetWidth()), mm(bb.GetHeight())) / 2
        c = pad.GetParentFootprint().GetPosition()
        out = math.atan2(py - mm(c.y), px - mm(c.x))            # away from the part first
        for x, y in ring(px, py, half, steps=(0.4, 0.55, 0.7, 0.9, 1.1, 1.4, 1.8), n=32, prefer=out):
            if cu.fits("pt", (x, y), None, VIA_D / 2, net) and \
                    cu.path_fits(net, pcbnew.F_Cu, [(px, py), (x, y)], w):
                break
        else:
            raise RuntimeError("hop %s: no via beside %s.%s" % (netname, ref, num))
        cu.track(net, pcbnew.F_Cu, (px, py), (x, y), w, locked=True)
        cu.via(net, x, y, locked=True)
        ends.append((x, y))
    (ax, ay), (bx, by) = ends
    path = grid_route(cu, net, layer, (ax, ay), [(bx, by)], w=w,
                      window=max(6.0, 2.4 * math.hypot(bx - ax, by - ay)))
    assert path, "hop %s %s-%s: no route on %s" % (netname, a, b, board.GetLayerName(layer))
    for p, q in zip(path, path[1:]):
        cu.track(net, layer, p, q, w, locked=True)


def via_link(board, netname, a, b, layer, w=0.2):
    """Like hop, but ends on the existing via of b's net nearest pad b (for a pad with no
    room left beside it for a via of its own)."""
    cu = Copper(board)
    net = board.FindNet(netname).GetNetCode()
    pb = board.FindFootprintByReference(b[0]).FindPadByNumber(b[1])
    bx0, by0 = mm(pb.GetPosition().x), mm(pb.GetPosition().y)
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetCode() == net]
    v = min(vias, key=lambda t: math.hypot(mm(t.GetPosition().x) - bx0, mm(t.GetPosition().y) - by0))
    bx, by = mm(v.GetPosition().x), mm(v.GetPosition().y)
    assert math.hypot(bx - bx0, by - by0) < 2.0, "via_link %s: no via near %s.%s" % (netname, *b)
    pad = board.FindFootprintByReference(a[0]).FindPadByNumber(a[1])
    bb = pad.GetBoundingBox()
    px, py = mm(pad.GetPosition().x), mm(pad.GetPosition().y)
    half = max(mm(bb.GetWidth()), mm(bb.GetHeight())) / 2
    for x, y in ring(px, py, half, steps=(0.4, 0.55, 0.7, 0.9, 1.1, 1.4, 1.8), n=32,
                     prefer=math.atan2(by - py, bx - px)):
        if cu.fits("pt", (x, y), None, VIA_D / 2, net) and \
                cu.path_fits(net, pcbnew.F_Cu, [(px, py), (x, y)], w):
            break
    else:
        raise RuntimeError("via_link %s: no via beside %s.%s" % (netname, *a))
    path = grid_route(cu, net, layer, (x, y), [(bx, by)], w=w,
                      window=max(6.0, 2.4 * math.hypot(bx - x, by - y)))
    assert path, "via_link %s: no route on %s" % (netname, board.GetLayerName(layer))
    cu.track(net, pcbnew.F_Cu, (px, py), (x, y), w, locked=True)
    cu.via(net, x, y, locked=True)
    for p, q in zip(path, path[1:]):
        cu.track(net, layer, p, q, w, locked=True)
    return sum(math.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(path, path[1:]))


def preroute(board, netname, a, b, w, required=True):
    """A locked track between two pads before autorouting, for a link the router would
    otherwise lose in a tight spot (straight, L, or maze on the top layer)."""
    cu = Copper(board)
    net = board.FindNet(netname).GetNetCode()
    pa = [p for p in board.FindFootprintByReference(a[0]).Pads() if p.GetNumber() == a[1]][0]
    pb = [p for p in board.FindFootprintByReference(b[0]).Pads() if p.GetNumber() == b[1]][0]
    s = (mm(pa.GetPosition().x), mm(pa.GetPosition().y))
    e = (mm(pb.GetPosition().x), mm(pb.GetPosition().y))
    for path in ([s, e], [s, (e[0], s[1]), e], [s, (s[0], e[1]), e]):
        if cu.path_fits(net, pcbnew.F_Cu, path, w):
            break
    else:
        path = grid_route(cu, net, pcbnew.F_Cu, s, [e], w=w,
                          window=max(8.0, 2.4 * math.hypot(e[0] - s[0], e[1] - s[1])))
    if not path and not required:
        print("preroute %s-%s: no room, left to the router" % (a, b))
        return
    assert path, "preroute %s-%s failed (%.2f,%.2f -> %.2f,%.2f)" % (a, b, s[0] - OX, s[1] - OY,
                                                                    e[0] - OX, e[1] - OY)
    for p, q in zip(path, path[1:]):
        cu.track(net, pcbnew.F_Cu, p, q, w, locked=True)


def vout_trunk(board, net, w=1.6):
    """VOUT is up to 3 A over 70 mm, from the bulk cap (power column) to J6 (bottom-left):
    routed by hand and locked: a 1.6 mm bottom-layer trunk along the board edge under J2, then
    over the capacitor notch (keeping the ESD row free for GND vias).
    Three vias drop it from C7's + pad (top) to the bottom layer."""
    def seg(a, b, layer):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(V(*a)); t.SetEnd(V(*b))
        t.SetWidth(pcbnew.FromMM(w)); t.SetLayer(layer); t.SetNet(net); t.SetLocked(True)
        board.Add(t)
    seg((76.05, 36.4), (70.3, 36.4), pcbnew.F_Cu)
    for x in (72.5, 71.4, 70.3):
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(V(x, 36.4)); v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(0.8)); v.SetDrill(pcbnew.FromMM(0.4))
        v.SetNet(net); v.SetLocked(True)
        board.Add(v)
    seg((72.5, 36.4), VOUT_TRUNK[0], pcbnew.B_Cu)
    for a, b in zip(VOUT_TRUNK, VOUT_TRUNK[1:]):
        seg(a, b, pcbnew.B_Cu)


def efuse_copper(board, cu):
    """The eFuse has no exposed pad: its IN and OUT strips are the heat path (TI 8.4.1). A small
    VBUS pour above the IN end and a VOUT pour below the OUT end, on the top and bottom layers,
    stitched with vias. The pours fill around everything else with clearance."""
    u = board.FindFootprintByReference("U4")
    ux, uy = mm(u.GetPosition().x) - OX, mm(u.GetPosition().y) - OY
    for netname, rect in (("/VBUS", (ux - 2.7, uy - 4.8, ux - 0.1, uy - 1.25)),
                          ("/VOUT", (ux + 0.1, uy + 1.25, ux + 1.8, uy + 6.4))):
        net = board.FindNet(netname)
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            z = add_zone(board, net, layer, priority=1, rect=rect)
            z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        n = 0
        x0, y0, x1, y1 = rect
        y = y0 + 0.6
        while y < y1 - 0.5 and n < 4:
            x = x0 + 0.6
            while x < x1 - 0.5 and n < 4:
                if cu.fits("pt", (OX + x, OY + y), None, VIA_D / 2 + 0.05, net.GetNetCode()):
                    cu.via(net.GetNetCode(), OX + x, OY + y)
                    n += 1
                x += 0.9
            y += 0.9
        print("eFuse heat copper %s: 2 pours, %d vias" % (netname, n))


def add_zone(board, net, layer, priority=0, inset=0.3, thermal=False, rect=None):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNetCode(net.GetNetCode())
    z.SetAssignedPriority(priority)
    z.SetLocalClearance(pcbnew.FromMM(0.15))
    z.SetMinThickness(pcbnew.FromMM(0.15))
    z.SetThermalReliefGap(pcbnew.FromMM(0.3))
    z.SetThermalReliefSpokeWidth(pcbnew.FromMM(0.4))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_THT_THERMAL if thermal else pcbnew.ZONE_CONNECTION_FULL)
    z.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    ol = z.Outline()
    ol.NewOutline()
    x0, y0, x1, y1 = rect or (0, 0, W, H)               # the filler clips to the outline
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        ol.Append(pcbnew.FromMM(OX + x), pcbnew.FromMM(OY + y))
    ol.Outline(0).SetClosed(True)
    board.Add(z)
    return z


def arc(cx, cy, r, a0, a1, n=6):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def outline():
    """85 x 41: square corners on the header edge, 3 mm radius on the cut edge, and the
    capacitor notch with 1 mm inner radii (JLC's router)."""
    nx0, ny0, nx1 = NOTCH
    pts = [(0, 0), (W, 0)]
    pts += arc(W - 3, H - 3, 3, 0, 90)
    pts += [(nx1, H)] + arc(nx1 - 1, ny0 + 1, 1, 0, -90)
    pts += arc(nx0 + 1, ny0 + 1, 1, -90, -180) + [(nx0, H)]
    pts += arc(3, H - 3, 3, 90, 180)
    return pts


def rounded_rect(x0, y0, x1, y1, r):
    """Square corners on the header edge (HAT spec), rounded on the cut edge."""
    pts = [(x0, y0), (x1, y0), (x1, y1 - r)]
    for i in range(1, 6):
        a = math.radians(90 * i / 6)
        pts.append((x1 - r + r * math.cos(a), y1 - r + r * math.sin(a)))
    pts += [(x1 - r, y1), (x0 + r, y1)]
    for i in range(1, 6):
        a = math.radians(90 + 90 * i / 6)
        pts.append((x0 + r + r * math.cos(a), y1 - r + r * math.sin(a)))
    pts.append((x0, y1 - r))
    return pts




# ------------------------------------------------------------------ finish

def text(board, s, x, y, size=1.0, layer=pcbnew.F_SilkS, bold=False, just="left", angle=0, wf=1.0):
    """wf < 1 narrows the characters (JLC's minimum is on the height, 1.0 mm)."""
    t = pcbnew.PCB_TEXT(board)
    t.SetText(s)
    t.SetLayer(layer)
    t.SetTextSize(pcbnew.VECTOR2I_MM(size * wf, size))
    t.SetTextThickness(pcbnew.FromMM(max(0.15, size * (0.2 if bold else 0.15))))
    t.SetHorizJustify({"left": pcbnew.GR_TEXT_H_ALIGN_LEFT, "center": pcbnew.GR_TEXT_H_ALIGN_CENTER,
                       "right": pcbnew.GR_TEXT_H_ALIGN_RIGHT}[just])
    if layer == pcbnew.B_SilkS:
        t.SetMirrored(True)
    t.SetTextAngleDegrees(angle)
    t.SetPosition(V(x, y))
    board.Add(t)
    return t


def fp_xy(board, ref):
    p = board.FindFootprintByReference(ref).GetPosition()
    return mm(p.x) - OX, mm(p.y) - OY


def silkscreen(board):
    """Curated labels. JLC's minimum is 1.0 mm text height, 0.15 mm stroke: nothing smaller."""
    for fp in board.GetFootprints():
        r = fp.Reference()
        r.SetLayer(pcbnew.B_Fab if fp.IsFlipped() else pcbnew.F_Fab)
    F, B = pcbnew.F_SilkS, pcbnew.B_SilkS
    T = 1.0

    # --- branding and the J4 pin map, top, left corner (above J6, left of the notch)
    text(board, design.COMPANY.upper(), 1.0, 24.3, 1.1, bold=True)
    text(board, "HASLER HAT rev %s  2026" % design.REV, 1.0, 25.65, T)
    j4 = ["J4  1 5V   2 3V3  3 GND  4 GND", "    5 SDA  6 SCL  7 TXD  8 RXD",
          "    9 G5  10 G6  11 G12 12 G13", "   13 G16 14 G26 15 3V3A 16 GND"]
    for i, line in enumerate(j4):
        text(board, line, 1.0, 27.0 + 1.2 * i, T, wf=0.75)

    # --- test points (top strip): label between / under the rows
    x, y = fp_xy(board, "TP3")
    text(board, "5V", x + 1.3, y, T)
    labels = {"TP4": "3V3", "TP5": "3V3A", "TP6": "GND", "TP7": "GND",
              "TP8": "SDA", "TP9": "SCL", "TP10": "INT", "TP13": "A0", "TP14": "A1"}
    for ref, lab in labels.items():
        x, y = fp_xy(board, ref)
        text(board, lab, x, y + 1.5, T, just="center")
    for ref, lab, dx, dy in (("TP1", "VBUS", 0, 1.6), ("TP2", "20V", 0, -1.5), ("TP11", "PD", 0, 1.6),
                             ("TP12", "PG", 0, -1.55)):
        x, y = fp_xy(board, ref)
        text(board, lab, x + dx, y + dy, T, just="center")

    # --- LEDs and connectors
    text(board, "PD 20V", 64.35, 2.1, T, just="center", angle=90, wf=0.6)   # D2, D6; clear of H2's head
    x, y = fp_xy(board, "D7")
    text(board, "3V3A", x + 1.55, y, T, just="center", angle=90)
    text(board, "QWIIC", 37.3, 3.2, T, just="center", angle=90)
    x, y = fp_xy(board, "J6")
    text(board, "+20V GND", x + 1.75, 32.0, T, just="center")
    # J2 pin 1: the library's marker sits past the board edge, so mark it inboard as well
    text(board, "1", 25.0, 37.3, T, bold=True, just="center")

    # --- jumpers
    x, y = fp_xy(board, "JP1")            # 3V3A source: pad 1 = LDO, pad 3 = the Pi's 3V3
    text(board, "1 LDO", x + 2.4, y - 0.6, T, wf=0.6)
    text(board, "3 PI", x + 2.4, y + 0.6, T, wf=0.6)
    for ref, lab, dx, dy, ang in (("JP2", "A0", 0, 3.0, 0), ("JP3", "A1", 0, 3.0, 0), ("JP4", "A2", 0, 3.0, 0),
                                  ("JP5", "INT", 0, 3.0, 0), ("JP7", "RST", 0, 3.0, 0),
                                  ("JP6", "RDY", 0, 3.0, 0), ("JP8", "ADDR", -1.7, 0, 90),
                                  ("JP9", "PD I2C", 1.4, 2.4, 0), ("JP11", "10V", 0, 2.6, 0)):
        x, y = fp_xy(board, ref)
        text(board, lab, x + dx, y + dy, T, just="center", angle=ang)

    # --- bottom: J2 map (middle), notes and branding (under the power column), order number
    # 1.6 mm pitch: descenders (p, y, g) and brackets reach ~0.4 mm below the line
    text(board, "J2 IDC: wire 1 = pin 1", 46.0, 12.35, T, layer=B, just="center")
    for i in range(13):
        a, b = 2 * i + 1, 2 * i + 2
        text(board, "%2d %s" % (a, design.J2_LABELS[a]), 62.0, 13.9 + 1.58 * i, T, layer=B)
        text(board, "%2d %s" % (b, design.J2_LABELS[b]), 46.0, 13.9 + 1.58 * i, T, layer=B)
    notes = ["J6 -> Amp4 power in,", "+ to +. Amp4 DC jack", "stays empty.",
             "Never 28 V (R1 120k).", "eFuse OV 22.6V 3.3A", "JP11 shut: UV 10.3V",
             "I2C MCP23017 0x20", "ADS1115 0x48", "AMP 0x4D", "CH224A 0x22 JP9/10"]
    for i, line in enumerate(notes):
        if line:
            text(board, line, 74.6, 12.2 + 1.6 * i, T, layer=B, just="center")
    text(board, design.COMPANY.upper(), 74.6, 28.6, 1.3, layer=B, just="center", bold=True)
    text(board, "Hasler HAT rev %s  2026" % design.REV, 74.6, 30.5, T, layer=B, just="center")
    # easter egg, bottom, under the Amp4 (seen only with the HAT off): the eFuse blocks reverse current
    for i, line in enumerate(("REVERSE THE POLARITY", "OF THE NEUTRON FLOW", "", "(U4 won't let you)",
                              "", "Bigger on the inside.")):
        if line:
            text(board, line, 15.5, 16.0 + 1.6 * i, T, layer=B, just="center")


# ----------------------------------------------- copper model and finishing passes

ROUTE_LAYERS = (pcbnew.F_Cu, pcbnew.B_Cu, pcbnew.In2_Cu)


def _rect_dist(px, py, r):
    return math.hypot(max(r[0] - px, 0, px - r[2]), max(r[1] - py, 0, py - r[3]))


def _seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    L = vx * vx + vy * vy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * vx + (py - ay) * vy) / L))
    return math.hypot(px - ax - t * vx, py - ay - t * vy)


def _seg_seg_dist(a, b):
    (ax, ay, bx, by), (cx, cy, dx, dy) = a, b

    def orient(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    o1, o2 = orient(ax, ay, bx, by, cx, cy), orient(ax, ay, bx, by, dx, dy)
    o3, o4 = orient(cx, cy, dx, dy, ax, ay), orient(cx, cy, dx, dy, bx, by)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return 0.0
    return min(_seg_dist(ax, ay, cx, cy, dx, dy), _seg_dist(bx, by, cx, cy, dx, dy),
               _seg_dist(cx, cy, ax, ay, bx, by), _seg_dist(dx, dy, ax, ay, bx, by))


def _seg_rect_dist(seg, r, n=24):
    ax, ay, bx, by = seg
    if r[0] <= ax <= r[2] and r[1] <= ay <= r[3]:
        return 0.0
    return min(_rect_dist(ax + (bx - ax) * i / n, ay + (by - ay) * i / n, r) for i in range(n + 1))


def _dist(k1, g1, k2, g2):
    if k1 == "pt" and k2 == "pt":
        return math.hypot(g1[0] - g2[0], g1[1] - g2[1])
    if k1 == "seg" and k2 == "seg":
        return _seg_seg_dist(g1, g2)
    if k1 == "rect" and k2 == "rect":
        return max(0, max(g1[0] - g2[2], g2[0] - g1[2]), max(g1[1] - g2[3], g2[1] - g1[3]))
    if k1 != "pt" and k2 == "pt":
        return _dist(k2, g2, k1, g1)
    if k1 == "rect" and k2 == "seg":
        return _dist(k2, g2, k1, g1)
    if k1 == "pt":
        return _seg_dist(g1[0], g1[1], *g2) if k2 == "seg" else _rect_dist(g1[0], g1[1], g2)
    return _seg_rect_dist(g1, g2)          # seg - rect


class Copper:
    """Every piece of copper as a point (via), segment (track) or box (pad), for clearance checks
    by the passes that add copper after routing. Zones are ignored: they are refilled after."""

    def __init__(self, board):
        self.board = board
        self.power = {board.FindNet("/" + n).GetNetCode() for n in design.POWER_NETS}
        self.items = []    # (kind, geom, layer or None = all, half width, netcode, is_smd_pad, object)
        for t in board.GetTracks():
            self.add(t)
        for fp in board.GetFootprints():
            for p in fp.Pads():
                npth = p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH
                if not p.IsOnCopperLayer() and not npth:   # paste-only apertures
                    continue
                bb = p.GetBoundingBox()
                r = (mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom()))
                if npth and r[2] - r[0] > 2.0:   # mounting hole: keep clear of the screw head
                    c = ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
                    r = (c[0] - 3.0, c[1] - 3.0, c[0] + 3.0, c[1] + 3.0)
                tht = p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH)
                layer = None if tht else (pcbnew.B_Cu if fp.IsFlipped() else pcbnew.F_Cu)
                self.items.append(("rect", r, layer, 0, p.GetNetCode(), not tht, p))

    def add(self, t):
        if t.GetClass() == "PCB_VIA":
            p = t.GetPosition()
            self.items.append(("pt", (mm(p.x), mm(p.y)), None, mm(t.GetWidth(pcbnew.F_Cu)) / 2,
                               t.GetNetCode(), False, t))
        else:
            s, e = t.GetStart(), t.GetEnd()
            self.items.append(("seg", (mm(s.x), mm(s.y), mm(e.x), mm(e.y)), t.GetLayer(),
                               mm(t.GetWidth()) / 2, t.GetNetCode(), False, t))

    @staticmethod
    def inside(kind, g, hw):
        xs = (g[0],) if kind == "pt" else (g[0], g[2])
        ys = (g[1],) if kind == "pt" else (g[1], g[3])
        m = hw + 0.45
        if min(xs) - m < OX or max(xs) + m > OX + W or min(ys) - m < OY or max(ys) + m > OY + H:
            return False
        nx0, ny0, nx1 = NOTCH
        if max(xs) + m > OX + nx0 and min(xs) - m < OX + nx1 and max(ys) + m > OY + ny0:
            return False
        for cx in (OX + 3, OX + W - 3):            # the two 3 mm corners on the cut edge
            for x, y in zip(xs, ys):
                if abs(x - cx) < 3 and y > OY + H - 3 and \
                        math.hypot(x - cx, y - (OY + H - 3)) > 3 - m:
                    return False
        return True

    def fits(self, kind, g, layer, hw, net, skip=None):
        """A new via ('pt') or track ('seg') of `net` keeps CLR from other nets' copper.
        Vias also stay off every SMD pad, own net included, except `skip` (an exposed pad)."""
        if not self.inside(kind, g, hw):
            return False
        for k2, g2, l2, h2, n2, smd, obj in self.items:
            if obj is skip:
                continue
            if layer is not None and l2 is not None and l2 != layer:
                continue
            d = _dist(kind, g, k2, g2)
            clr = 0.15
            if n2 != net and d < hw + h2 + clr:
                return False
            if kind == "pt" and smd and n2 == net and d < hw + 0.05:
                return False
            if kind == "pt" and k2 == "pt" and d < hw + h2 + 0.1:          # drill spacing, any net
                return False
            if kind == "pt" and k2 == "rect" and l2 is None and d < hw + 0.1:  # off THT pads too
                return False
        return True

    def via(self, net, x, y, locked=False):
        v = pcbnew.PCB_VIA(self.board)
        v.SetLocked(locked)
        v.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
        v.SetWidth(pcbnew.F_Cu, pcbnew.FromMM(VIA_D))
        v.SetDrill(pcbnew.FromMM(VIA_DRILL))
        v.SetNetCode(net)
        self.board.Add(v)
        self.add(v)

    def track(self, net, layer, a, b, w, locked=False):
        t = pcbnew.PCB_TRACK(self.board)
        t.SetLocked(locked)
        t.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
        t.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
        t.SetWidth(pcbnew.FromMM(w)); t.SetLayer(layer); t.SetNetCode(net)
        self.board.Add(t)
        self.add(t)

    def path_fits(self, net, layer, pts, w):
        return all(self.fits("seg", (a[0], a[1], b[0], b[1]), layer, w / 2, net)
                   for a, b in zip(pts, pts[1:]))


def grid_route(cu, net, layer, start, goals, w=0.2, window=8.0, step=0.1):
    """Breadth-first maze route on a 0.1 mm grid, on one layer, from `start` to the nearest of
    `goals` (points of the same net). Returns the simplified path or None."""
    from collections import deque
    sx, sy = start
    x0, y0 = sx - window / 2, sy - window / 2
    n = int(window / step) + 1
    near = [it for it in cu.items if (it[2] is None or it[2] == layer) and it[4] != net and
            _dist("pt", (sx, sy), it[0], it[1]) < window]
    hw = w / 2

    def free(i, j):
        x, y = x0 + i * step, y0 + j * step
        if not cu.inside("pt", (x, y), hw):
            return False
        for k2, g2, l2, h2, n2, smd, o in near:
            clr = 0.2 if (net in cu.power or n2 in cu.power) else 0.15
            if _dist("pt", (x, y), k2, g2) < hw + h2 + clr:
                return False
        return True
    goal_cells = {}
    for gx, gy in goals:
        gi, gj = round((gx - x0) / step), round((gy - y0) / step)
        if 0 <= gi < n and 0 <= gj < n:
            goal_cells[(gi, gj)] = (gx, gy)
    if not goal_cells:
        return None
    si, sj = round((sx - x0) / step), round((sy - y0) / step)
    prev = {(si, sj): None}
    ok = {}
    q = deque([(si, sj)])
    end = None
    while q:
        c = q.popleft()
        if c in goal_cells:
            end = c
            break
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nb = (c[0] + di, c[1] + dj)
            if nb in prev or not (0 <= nb[0] < n and 0 <= nb[1] < n):
                continue
            # cells within the start/goal pads are allowed (own copper)
            near_end = math.hypot(nb[0] - si, nb[1] - sj) * step < 0.45 or nb in goal_cells
            if not near_end:
                if nb not in ok:
                    ok[nb] = free(*nb)
                if not ok[nb]:
                    continue
            prev[nb] = c
            q.append(nb)
    if end is None:
        return None
    cells = []
    while end is not None:
        cells.append(end)
        end = prev[end]
    cells.reverse()
    pts = [(x0 + i * step, y0 + j * step) for i, j in cells]
    pts[0], pts[-1] = start, goal_cells[cells[-1]]
    simple = [pts[0]]                       # drop collinear points
    for k in range(1, len(pts) - 1):
        (ax, ay), (bx, by), (cx_, cy_) = simple[-1], pts[k], pts[k + 1]
        if abs((bx - ax) * (cy_ - by) - (by - ay) * (cx_ - bx)) > 1e-9:
            simple.append(pts[k])
    simple.append(pts[-1])
    return simple


def ring(cx, cy, r0, steps=(0.55, 0.8, 1.1, 1.5, 2.0, 2.6, 3.2, 4.0), n=24, prefer=None):
    """Candidate points around (cx, cy), nearest first; at each radius, closest to `prefer`
    (an angle in radians) first."""
    for d in steps:
        ks = sorted(range(n), key=lambda k: 0 if prefer is None else
                    abs((2 * math.pi * k / n - prefer + math.pi) % (2 * math.pi) - math.pi))
        for k in ks:
            a = 2 * math.pi * k / n
            yield cx + (r0 + d) * math.cos(a), cy + (r0 + d) * math.sin(a)


def widen_power(cu, widths=(1.2, 1.0, 0.8, 0.6)):
    """Power nets were routed at 0.4 mm so they can reach fine-pitch pins; give each segment the
    widest width its surroundings allow."""
    power = {cu.board.FindNet("/" + n).GetNetCode() for n in design.POWER_NETS}
    n = 0
    for i, (kind, g, layer, hw0, net, _, t) in enumerate(list(cu.items)):
        if kind != "seg" or net not in power:
            continue
        for w in widths:
            if w / 2 <= hw0:
                break
            if cu.fits("seg", g, layer, w / 2, net, skip=t):
                t.SetWidth(pcbnew.FromMM(w))
                cu.items[i] = (kind, g, layer, w / 2, net, False, t)
                n += 1
                break
    return n


def stitch_gnd(cu, locked=False, skip=()):
    """GND was never routed (dropped from the DSN): a via into the In1 plane beside every
    top-side SMD GND pad, on a short track; exposed pads get vias inside them."""
    gnd = cu.board.FindNet("/GND").GetNetCode()
    have = [(g[0], g[1]) for k, g, l, h, n, s, o in cu.items if k in ("pt", "seg") and n == gnd]
    pads = [(k, g, obj) for k, g, l, h, n, smd, obj in cu.items if smd and n == gnd and l == pcbnew.F_Cu
            and obj.GetParentFootprint().GetReference() not in skip
            and not any(r_x0 <= x <= r_x1 and r_y0 <= y <= r_y1 for x, y in have
                        for r_x0, r_y0, r_x1, r_y1 in (g,))]          # skip pads already tied down
    done, failed = 0, []
    for _, r, pad in pads:
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        if min(r[2] - r[0], r[3] - r[1]) > 1.8:                     # exposed pad: vias inside
            ok = [(x, y) for x in (cx - 0.6, cx, cx + 0.6) for y in (cy - 0.6, cy, cy + 0.6)
                  if cu.fits("pt", (x, y), None, VIA_D / 2, gnd, skip=pad)]
            for x, y in ok[:3]:
                cu.via(gnd, x, y, locked)
            done += bool(ok)
            if not ok:
                failed.append(pad)
            continue
        half = max(r[2] - r[0], r[3] - r[1]) / 2
        c = pad.GetParentFootprint().GetPosition()               # outward from the part first
        out = math.atan2(cy - mm(c.y), cx - mm(c.x))
        for x, y in ring(cx, cy, half, prefer=out):
            if cu.fits("pt", (x, y), None, VIA_D / 2, gnd) and \
                    cu.path_fits(gnd, pcbnew.F_Cu, [(cx, cy), (x, y)], 0.3):
                cu.via(gnd, x, y, locked)
                cu.track(gnd, pcbnew.F_Cu, (cx, cy), (x, y), 0.3, locked)
                done += 1
                break
        else:
            failed.append(pad)
    return done, failed


def stitch_grid(cu, pitch=1.6):
    """GND vias wherever they fit on a grid, so every scrap of GND pour on the outer and
    In2 layers drops into the In1 plane instead of being discarded as an island."""
    gnd = cu.board.FindNet("/GND").GetNetCode()
    n = 0
    y = OY + 0.9
    while y < OY + H - 0.9:
        x = OX + 0.9
        while x < OX + W - 0.9:
            if cu.fits("pt", (x, y), None, VIA_D / 2 + 0.05, gnd):
                cu.via(gnd, x, y)
                n += 1
            x += pitch
        y += pitch
    return n


def drc_report(path):
    import json
    import subprocess
    rpt = os.path.join(OUT, "build", "gaps.json")
    subprocess.run(["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli", "pcb", "drc", "--format",
                    "json", "-o", rpt, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.load(open(rpt))


def drc_unconnected(path):
    return [u for u in drc_report(path)["unconnected_items"]
            if not all(i["description"].startswith("Zone") for i in u["items"])]


def _endpoint(cu, item, toward):
    """(x, y, layers) where a new track may start on a DRC-reported item."""
    obj = cu.board.GetItem(pcbnew.KIID(item["uuid"]))
    obj = obj.Cast() if obj is not None else None
    x, y = item["pos"]["x"], item["pos"]["y"]
    if obj is None:
        return x, y, ROUTE_LAYERS
    cls = obj.GetClass()
    if cls == "PCB_TRACK":
        s, e = obj.GetStart(), obj.GetEnd()
        ends = [(mm(s.x), mm(s.y)), (mm(e.x), mm(e.y))]
        x, y = min(ends, key=lambda p: math.hypot(p[0] - toward[0], p[1] - toward[1]))
        return x, y, (obj.GetLayer(),)
    if cls == "PCB_VIA":
        return x, y, ROUTE_LAYERS
    if cls == "PAD":
        p = obj.GetPosition()
        if obj.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
            return mm(p.x), mm(p.y), ROUTE_LAYERS
        return mm(p.x), mm(p.y), (pcbnew.B_Cu if obj.GetParentFootprint().IsFlipped() else pcbnew.F_Cu,)
    return x, y, ROUTE_LAYERS


def close_gaps(cu, gaps):
    """Join each unconnected pair with a straight or L-shaped track on a shared layer, or by
    hopping through a via; a GND item just gets its own via into the plane."""
    gnd = cu.board.FindNet("/GND").GetNetCode()
    fixed = 0
    for g in gaps:
        a_it, b_it = g["items"]
        obj = cu.board.GetItem(pcbnew.KIID(a_it["uuid"]))
        obj = obj.Cast() if obj is not None else None
        if obj is None:
            continue
        net = obj.GetNetCode()
        ax, ay, al = _endpoint(cu, a_it, (b_it["pos"]["x"], b_it["pos"]["y"]))
        bx, by, bl = _endpoint(cu, b_it, (ax, ay))
        w = 0.3 if net == gnd else 0.25 if net in cu.power else 0.15
        done = False
        for layer in [l for l in ROUTE_LAYERS if l in al and l in bl]:
            for pts in ([(ax, ay), (bx, by)], [(ax, ay), (bx, ay), (bx, by)], [(ax, ay), (ax, by), (bx, by)]):
                if cu.path_fits(net, layer, pts, w):
                    for p, q in zip(pts, pts[1:]):
                        cu.track(net, layer, p, q, w)
                    done = True
                    break
            if done:
                break
        if not done:   # maze route on a shared layer
            for layer in [l for l in ROUTE_LAYERS if l in al and l in bl]:
                path = grid_route(cu, net, layer, (ax, ay), [(bx, by)], w=w,
                                  window=max(4.0, 2.4 * math.hypot(bx - ax, by - ay)))
                if path:
                    for p, q in zip(path, path[1:]):
                        cu.track(net, layer, p, q, w)
                    done = True
                    break
        if not done and net == gnd:
            for (x0, y0, l0) in ((ax, ay, al), (bx, by, bl)):
                if pcbnew.F_Cu not in l0:
                    continue
                for x, y in ring(x0, y0, 0.3):
                    if cu.fits("pt", (x, y), None, VIA_D / 2, net) and \
                            cu.path_fits(net, pcbnew.F_Cu, [(x0, y0), (x, y)], w):
                        cu.via(net, x, y)
                        cu.track(net, pcbnew.F_Cu, (x0, y0), (x, y), w)
                        done = True
                        break
                if done:
                    break
        if not done:   # hop: a via beside one end, then a straight run on a layer the other end has
            for (x0, y0, l0), (x1, y1, l1) in (((ax, ay, al), (bx, by, bl)), ((bx, by, bl), (ax, ay, al))):
                for x, y in ring(x0, y0, 0.3, steps=(0.6, 0.9, 1.3, 1.8, 2.4, 3.0, 3.6), n=32):
                    if not cu.fits("pt", (x, y), None, VIA_D / 2, net):
                        continue
                    la = [l for l in l0 if cu.path_fits(net, l, [(x0, y0), (x, y)], w)]
                    if not la:
                        continue
                    leg = None
                    for l in l1:
                        if cu.path_fits(net, l, [(x, y), (x1, y1)], w):
                            leg = (l, [(x, y), (x1, y1)])
                            break
                    if leg is None:      # second leg by maze, on any layer the far end has
                        for l in l1:
                            pth = grid_route(cu, net, l, (x, y), [(x1, y1)], w=w,
                                             window=max(4.0, 2.4 * math.hypot(x1 - x, y1 - y)))
                            if pth:
                                leg = (l, pth)
                                break
                    if leg:
                        cu.via(net, x, y)
                        cu.track(net, la[0], (x0, y0), (x, y), w)
                        for p, q in zip(leg[1], leg[1][1:]):
                            cu.track(net, leg[0], p, q, w)
                        done = True
                        break
                if done:
                    break
        if not done:   # last resort: maze a stub to any free via spot nearby, then maze the far leg
            done = _maze_hop(cu, net, w, (ax, ay, al), (bx, by, bl)) or \
                _maze_hop(cu, net, w, (bx, by, bl), (ax, ay, al)) or \
                _two_via(cu, net, w, (ax, ay, al), (bx, by, bl))
        fixed += done
    return fixed


def _two_via(cu, net, w, a, b):
    """Both ends on the top layer and no way between them there: a via beside each end and a
    maze route between the vias on In2, else B.Cu."""
    (x0, y0, l0), (x1, y1, l1) = a, b
    if pcbnew.F_Cu not in l0 or pcbnew.F_Cu not in l1:
        return False

    def spots(x, y):
        return [p for p in ring(x, y, 0.3, steps=(0.6, 0.9, 1.3, 1.8, 2.4), n=24)
                if cu.fits("pt", p, None, VIA_D / 2, net) and cu.path_fits(net, pcbnew.F_Cu, [(x, y), p], w)]
    sa, sb = spots(x0, y0)[:3], spots(x1, y1)[:12]
    if not sa or not sb:
        return False
    for layer in (pcbnew.In2_Cu, pcbnew.B_Cu):
        for pa in sa:
            pth = grid_route(cu, net, layer, pa, sb, w=w,
                             window=max(6.0, 2.4 * math.hypot(x1 - pa[0], y1 - pa[1])))
            if not pth:
                continue
            pb = pth[-1]
            cu.track(net, pcbnew.F_Cu, (x0, y0), pa, w)
            cu.via(net, *pa)
            for p, q in zip(pth, pth[1:]):
                cu.track(net, layer, p, q, w)
            cu.via(net, *pb)
            cu.track(net, pcbnew.F_Cu, pb, (x1, y1), w)
            return True
    return False


def _maze_hop(cu, net, w, a, b):
    (x0, y0, l0), (x1, y1, l1) = a, b
    spots = [p for p in ring(x0, y0, 0.3, steps=(0.6, 0.9, 1.3, 1.8, 2.4, 3.0, 3.6, 4.2), n=32)
             if cu.fits("pt", p, None, VIA_D / 2, net)]
    if not spots:
        return False
    for la in l0:
        stub = grid_route(cu, net, la, (x0, y0), spots, w=w, window=10.0)
        if not stub:
            continue
        vx, vy = stub[-1]
        for lb in l1:
            leg = grid_route(cu, net, lb, (vx, vy), [(x1, y1)], w=w,
                             window=max(4.0, 2.4 * math.hypot(x1 - vx, y1 - vy)))
            if leg:
                for p, q in zip(stub, stub[1:]):
                    cu.track(net, la, p, q, w)
                cu.via(net, vx, vy)
                for p, q in zip(leg, leg[1:]):
                    cu.track(net, lb, p, q, w)
                return True
    return False


def join_fragments(cu):
    """GND pour fragments that hold a pad but reach no via: drop a via inside the fragment,
    or run a short track from its pad to grounded copper outside it."""
    board, gnd = cu.board, cu.board.FindNet("/GND").GetNetCode()
    vias = [(g[0], g[1]) for k, g, l, h, n, s, o in cu.items if k == "pt" and n == gnd]
    fixed = 0
    for z in board.Zones():
        if z.GetNetCode() != gnd or z.GetLayer() == pcbnew.In1_Cu:
            continue
        layer = z.GetLayer()
        polys = z.GetFilledPolysList(layer)
        for i in range(polys.OutlineCount()):
            ol = polys.Outline(i)
            inside = lambda x, y: ol.PointInside(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))
            if any(inside(x, y) for x, y in vias):
                continue
            bb = ol.BBox()
            x0, y0, x1, y1 = mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom())
            pts = [(x0 + 0.1 * i, y0 + 0.1 * j) for i in range(int((x1 - x0) / 0.1) + 1)
                   for j in range(int((y1 - y0) / 0.1) + 1)]
            spot = next(((x, y) for x, y in pts if inside(x, y)
                         and cu.fits("pt", (x, y), None, VIA_D / 2, gnd)), None)
            if spot:
                cu.via(gnd, *spot)
                vias.append(spot)
                fixed += 1
                continue
            # a pad in this fragment -> nearest grounded via, straight or L, 0.2 mm
            pads = [(o, g) for k, g, l, h, n, s, o in cu.items
                    if k == "rect" and n == gnd and l in (None, layer) and inside((g[0] + g[2]) / 2, (g[1] + g[3]) / 2)]
            for pad, r in pads:
                cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
                near = [v for v in vias if math.hypot(v[0] - cx, v[1] - cy) < 3.8]
                path = grid_route(cu, gnd, layer, (cx, cy), near) if near else None
                if path:
                    for p, q in zip(path, path[1:]):
                        cu.track(gnd, layer, p, q, 0.2)
                    fixed += 1
                    break
                for vx, vy in sorted(vias, key=lambda v: math.hypot(v[0] - cx, v[1] - cy))[:12]:
                    for path in ([(cx, cy), (vx, vy)], [(cx, cy), (vx, cy), (vx, vy)], [(cx, cy), (cx, vy), (vx, vy)]):
                        if cu.path_fits(gnd, layer, path, 0.2):
                            for p, q in zip(path, path[1:]):
                                cu.track(gnd, layer, p, q, 0.2)
                            fixed += 1
                            break
                    else:
                        continue
                    break
                else:
                    continue
                break
            else:
                print("  GND fragment on %s near %.1f,%.1f with %s: no way down" % (
                    board.GetLayerName(layer), (x0 + x1) / 2 - OX, (y0 + y1) / 2 - OY,
                    [p.GetParentFootprint().GetReference() + "." + p.GetNumber() for p, _ in pads]))
    return fixed


def tidy(board, report):
    """Delete vias the router left dangling; shave a track that misses clearance by a hair;
    bring any router neck up to JLC's 0.09 mm (min_width() takes them further where they fit)."""
    gone = 0
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK" and mm(t.GetWidth()) < 0.09:
            t.SetWidth(pcbnew.FromMM(0.09))
            gone += 1
    for v in report["violations"]:
        if v["type"] == "via_dangling":
            obj = board.GetItem(pcbnew.KIID(v["items"][0]["uuid"]))
            if obj is not None:
                board.Remove(obj.Cast())
                gone += 1
        if v["type"] == "clearance":
            m_ = re.search(r"clearance ([0-9.]+) mm; actual ([0-9.]+) mm", v["description"])
            short = float(m_.group(1)) - float(m_.group(2)) if m_ else 1
            for it in v["items"]:
                obj = board.GetItem(pcbnew.KIID(it["uuid"]))
                obj = obj.Cast() if obj is not None else None
                if obj is not None and obj.GetClass() == "PCB_TRACK" and short < 0.02:
                    w = mm(obj.GetWidth()) - 2 * short - 0.004
                    if w >= SHAVE_MIN:
                        obj.SetWidth(pcbnew.FromMM(w))
                        gone += 1
                        break
    return gone


def min_width(cu):
    """Any track still under MIN_W (router necks, gap-closer shaves) up to it where it fits."""
    ok = left = 0
    for i, (kind, g, layer, hw, net, _, t) in enumerate(list(cu.items)):
        if kind != "seg" or 2 * hw >= MIN_W - 1e-6:
            continue
        if cu.fits("seg", g, layer, MIN_W / 2, net, skip=t):
            t.SetWidth(pcbnew.FromMM(MIN_W))
            cu.items[i] = (kind, g, layer, MIN_W / 2, net, False, t)
            ok += 1
        else:
            left += 1
    return ok, left


def fill(board):
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())


def gap_passes(tag, rounds=6):
    history = []
    for i in range(rounds):
        rpt = drc_report(PCB)
        gaps = rpt["unconnected_items"]
        if not gaps:
            break
        history.append(len(gaps))
        if len(history) >= 3 and history[-1] >= history[-3]:
            print("%s: no progress in two passes, stopping at %d" % (tag, len(gaps)))
            break
        board = pcbnew.LoadBoard(PCB)
        cu = Copper(board)
        fixed = close_gaps(cu, [g for g in gaps if not all(it["description"].startswith("Zone")
                                                             for it in g["items"])])
        fixed += join_fragments(cu)
        fixed += tidy(board, rpt)
        fill(board)
        board.Save(PCB)
        print("%s pass %d: %d unconnected, %d fixes" % (tag, i + 1, len(gaps), fixed))
        if not fixed:
            break


def finish():
    import shutil
    shutil.copy(os.path.join(OUT, "build", "placed.kicad_pcb"), PCB)
    board = pcbnew.LoadBoard(PCB)
    assert pcbnew.ImportSpecctraSES(board, os.path.join(OUT, "build", "hasler-hat.ses")), "SES import failed"
    # Gerber/drill/position origin at the board's bottom-left corner: positive coordinates
    board.GetDesignSettings().SetAuxOrigin(V(0, H))
    board.GetDesignSettings().SetGridOrigin(V(0, H))
    cu = Copper(board)
    print("power segments widened:", widen_power(cu))
    n, failed = stitch_gnd(cu)
    print("GND pads stitched: %d, %d left for the gap pass" % (n, len(failed)))
    gnd = board.FindNet("/GND")
    for layer in (pcbnew.F_Cu, pcbnew.In2_Cu, pcbnew.B_Cu):
        add_zone(board, gnd, layer, thermal=True)   # spokes on THT pads: J1 is hand-soldered
    efuse_copper(board, cu)
    silkscreen(board)
    fill(board)
    board.Save(PCB)
    gap_passes("gap")
    # The GND stitching grid only after every signal is joined: its vias would otherwise take
    # the spots the gap-closer needs in the dense rows. Then once more for pour fragments.
    board = pcbnew.LoadBoard(PCB)
    print("GND stitching grid vias:", stitch_grid(Copper(board)))
    fill(board)
    board.Save(PCB)
    gap_passes("after grid")
    board = pcbnew.LoadBoard(PCB)
    ok, left = min_width(Copper(board))
    print("tracks brought up to %.3f mm: %d, no room: %d" % (MIN_W, ok, left))
    # C1 (the CH224A's VHV/VBUS cap) is fed from J3's side by the router: give it a short
    # bottom-layer link to pin 8 as well, once everything else is in place
    try:
        n = via_link(board, "/VBUS", ("C1", "1"), ("U3", "8"), pcbnew.B_Cu, w=0.4)
        print("C1 -> U3.8 VBUS link on B.Cu: %.1f mm" % n)
    except (AssertionError, RuntimeError) as e:
        print("C1 -> U3.8 VBUS link:", e)
    fill(board)
    board.Save(PCB)
    for i in range(3):
        rpt = drc_report(PCB)
        board = pcbnew.LoadBoard(PCB)
        n = tidy(board, rpt)
        # a thermal spoke that can only land on a scrap of pour: that pad joins solid instead
        for v in rpt["violations"]:
            if v["type"] == "starved_thermal":
                for it in v["items"]:
                    obj = board.GetItem(pcbnew.KIID(it["uuid"]))
                    obj = obj.Cast() if obj is not None else None
                    if obj is not None and obj.GetClass() == "PAD":
                        obj.SetLocalZoneConnection(pcbnew.ZONE_CONNECTION_FULL)
                        print("  solid GND connection (starved spokes): %s.%s" % (
                            obj.GetParentFootprint().GetReference(), obj.GetNumber()))
                        n += 1
        fill(board)
        board.Save(PCB)
        print("final pass %d: %d fixes" % (i + 1, n))
        if not n:
            break
    set_stackup(PCB)
    set_rule_severities()
    print("finished: %d unconnected left" % len(drc_report(PCB)["unconnected_items"]))


STACKUP = """		(stackup
			(layer "F.SilkS" (type "Top Silk Screen") (color "White"))
			(layer "F.Paste" (type "Top Solder Paste"))
			(layer "F.Mask" (type "Top Solder Mask") (color "Black") (thickness 0.01))
			(layer "F.Cu" (type "copper") (thickness 0.035))
			(layer "dielectric 1" (type "prepreg") (thickness 0.2104) (material "FR4") (epsilon_r 4.4) (loss_tangent 0.02))
			(layer "In1.Cu" (type "copper") (thickness 0.0152))
			(layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4") (epsilon_r 4.6) (loss_tangent 0.02))
			(layer "In2.Cu" (type "copper") (thickness 0.0152))
			(layer "dielectric 3" (type "prepreg") (thickness 0.2104) (material "FR4") (epsilon_r 4.4) (loss_tangent 0.02))
			(layer "B.Cu" (type "copper") (thickness 0.035))
			(layer "B.Mask" (type "Bottom Solder Mask") (color "Black") (thickness 0.01))
			(layer "B.Paste" (type "Bottom Solder Paste"))
			(layer "B.SilkS" (type "Bottom Silk Screen") (color "White"))
			(copper_finish "ENIG")
			(dielectric_constraints no)
		)
"""


def set_rule_severities():
    """One DRC check off, on purpose: library silkscreen of parts that sit on the board edge
    by design (USB-C, J2's key side, C7 by the corner) runs past the outline; the fab
    clips silkscreen at the outline anyway. Everything else stays on."""
    import json
    pro = os.path.join(OUT, "hasler-hat.kicad_pro")
    d = json.load(open(pro))
    ds = d.setdefault("board", {}).setdefault("design_settings", {})
    ds.setdefault("rule_severities", {})["silk_edge_clearance"] = "ignore"
    json.dump(d, open(pro, "w"), indent=2)


def set_stackup(path):
    """JLC 4-layer 1.6 mm (JLC04161H-7628), black mask, white silk, ENIG.
    The Python API doesn't expose the stackup, so write it into (setup)."""
    s = open(path).read()
    if "(stackup" not in s:
        s = s.replace("\t(setup\n", "\t(setup\n" + STACKUP, 1)
        assert "(stackup" in s
        open(path, "w").write(s)


if __name__ == "__main__":
    os.makedirs(os.path.join(OUT, "build"), exist_ok=True)
    {"place": place, "finish": finish}[sys.argv[1]]()
