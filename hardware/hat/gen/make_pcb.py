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
import sys

import pcbnew

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design  # noqa: E402
from make_sch import uid  # noqa: E402  (same UUIDs as the schematic)

FPLIB = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints/"
OUT = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), ".."))
PCB = os.path.join(OUT, "hasler-hat.kicad_pcb")
OX, OY = 100.0, 100.0          # board origin on the KiCad sheet
W, H = 85.0, 41.0


def V(x, y):
    return pcbnew.VECTOR2I_MM(OX + x, OY + y)


def mm(v):
    return pcbnew.ToMM(v)


# --------------------------------------------------------------- placement
# ref: (x, y, rotation, side). Fixed parts first; "near" parts are legalized.
J1_PIN1 = (8.37, 10.19)       # Amp4 top header: inner row, SD-card end (DESIGN.md)
J2_PIN1 = (28.6, 37.3)        # odd row on the board-edge side, key slot facing the edge

FIXED = {
    "H1": (3.5, 3.5, 0), "H2": (61.5, 3.5, 0),
    "J5": (32.4, 3.0, 180),           # Qwiic, mouth to the top edge
    # power column (x 65..85, over the Pi's USB/Ethernet), flowing top to bottom:
    # USB-C -> CH224A -> window -> P-FET -> bulk/TVS -> J6
    "U3": (71.2, 6.2, 0),
    "U4": (69.6, 17.0, 0),
    "Q1": (79.6, 21.4, 90),
    "C7": (78.6, 34.6, 0),
    "J6": (67.4, 36.4, 0),            # wire entry faces the bottom edge -> Amp4 power terminal
    # logic
    "U1": (33.6, 16.15, 90),
    "U2": (49.5, 14.6, 0),
    "U6": (5.0, 15.0, 0),
}
for i in range(6):                    # ESD arrays in a row behind J2
    FIXED["U%d" % (7 + i)] = (26.4 + 6.5 * i, 29.1, 90)
for i in range(9):                    # 1k series (RN1-5) then 10k pull-ups (RN6-9)
    FIXED["RN%d" % (1 + i)] = (25.2 + 3.6 * i, 25.0, 0)
for k in range(18):                   # line caps, 0402, one per line
    FIXED["C%d" % (20 + k)] = (25.0 + 1.25 * k, 22.05, 90)

ZONES = {  # allowed rectangles for legalized parts
    "pwr": (65.2, 0.3, 84.7, 40.7),
    "lft": (0.3, 11.3, 27.9, 30.9),
    "mid": (22.8, 11.3, 64.9, 31.0),
    "top": (6.6, 0.3, 58.4, 6.6),
}
NEAR = {  # ref: (zone, x, y, rotation)
    # USB-C + CH224A
    "R1": ("pwr", 71.2, 10.6, 0), "C1": ("pwr", 67.0, 3.4, 90), "C2": ("pwr", 76.0, 13.4, 0),
    "JP9": ("pwr", 67.0, 7.4, 90), "JP10": ("pwr", 67.0, 10.6, 90),
    "D1": ("pwr", 80.6, 14.4, 0), "TP1": ("pwr", 83.6, 17.0, 0), "TP11": ("pwr", 75.2, 1.2, 0),
    # status LEDs beside the USB-C, readable from the port face
    "R2": ("pwr", 75.6, 10.8, 0), "D2": ("pwr", 83.4, 13.0, 90), "D6": ("pwr", 83.4, 21.0, 90),
    # window
    "R3": ("pwr", 66.0, 21.6, 90), "U5": ("pwr", 69.0, 22.0, 0),
    "R4": ("pwr", 72.0, 21.6, 90), "R5": ("pwr", 73.7, 21.6, 90), "R6": ("pwr", 75.4, 21.6, 90),
    "R14": ("pwr", 72.0, 28.0, 90), "R15": ("pwr", 75.4, 25.2, 90), "R16": ("pwr", 66.2, 31.0, 90),
    "C3": ("pwr", 72.0, 25.2, 90), "C4": ("pwr", 73.7, 25.2, 90), "C5": ("pwr", 69.6, 13.4, 0),
    "R7": ("pwr", 66.2, 25.4, 90), "D3": ("pwr", 68.6, 26.0, 90), "Q2": ("pwr", 76.2, 26.2, 0),
    "TP12": ("pwr", 66.2, 28.6, 0),
    # P-FET gate
    "R8": ("pwr", 79.6, 26.6, 0), "R9": ("pwr", 79.6, 16.6, 0), "D4": ("pwr", 75.8, 17.4, 90),
    "C6": ("pwr", 83.4, 26.0, 90),
    # out
    "C8": ("pwr", 70.0, 30.0, 0), "D5": ("pwr", 76.0, 29.6, 0), "R10": ("pwr", 82.0, 29.0, 90),
    "TP2": ("pwr", 83.6, 31.6, 0),
    # left corner: 3V3A and the MCP's strapping
    "C9": ("lft", 1.6, 15.0, 90), "C10": ("lft", 8.4, 15.0, 90), "JP1": ("lft", 5.0, 19.0, 0),
    "C11": ("lft", 5.0, 22.0, 0), "C12": ("lft", 8.4, 22.0, 90), "C13": ("lft", 1.6, 22.0, 90),
    "R11": ("lft", 11.6, 15.0, 90), "D7": ("lft", 11.6, 18.6, 90),
    "JP2": ("lft", 17.0, 13.4, 90), "JP3": ("lft", 20.0, 13.4, 90), "JP4": ("lft", 23.0, 13.4, 90),
    "JP5": ("lft", 17.0, 19.0, 90), "JP7": ("lft", 20.0, 19.0, 90), "R12": ("lft", 23.0, 19.0, 90),
    "C14": ("lft", 26.4, 16.2, 90),
    # ADS1115
    "C15": ("mid", 44.6, 14.0, 90), "C16": ("mid", 46.0, 14.0, 90), "R13": ("mid", 54.2, 14.6, 90),
    "JP6": ("mid", 56.6, 14.6, 90), "JP8": ("mid", 49.5, 18.2, 0),
    "R20": ("mid", 48.6, 22.2, 0), "R21": ("mid", 52.2, 22.2, 0), "R22": ("mid", 55.8, 22.2, 0),
}
TOP_TPS = ["TP3", "TP4", "TP5", "TP6", "TP7", "TP8", "TP9", "TP10", "TP13", "TP14"]
for i, ref in enumerate(TOP_TPS):
    NEAR[ref] = ("top", 39.0 + 3.6 * (i % 5), 1.6 if i < 5 else 4.8, 0)


def load_fp(fpid):
    lib, name = fpid.split(":")
    fp = pcbnew.FootprintLoad(FPLIB + lib + ".pretty", name)
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
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
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
            return True
    return False


def place():
    board = pcbnew.NewBoard(PCB)
    board.SetCopperLayerCount(4)
    board.SetLayerType(pcbnew.In1_Cu, pcbnew.LT_POWER)
    board.SetLayerName(pcbnew.In1_Cu, "In1.GND")
    board.SetLayerName(pcbnew.In2_Cu, "In2.Sig")
    ds = board.GetDesignSettings()
    ds.SetCopperLayerCount(4)
    ds.m_TrackMinWidth = pcbnew.FromMM(0.127)
    ds.m_MinClearance = pcbnew.FromMM(0.127)
    ds.m_ViasMinSize = pcbnew.FromMM(0.5)
    ds.m_MinThroughDrill = pcbnew.FromMM(0.3)
    ds.m_ViasMinAnnularWidth = pcbnew.FromMM(0.1)
    ds.m_CopperEdgeClearance = pcbnew.FromMM(0.3)
    ds.m_HoleClearance = pcbnew.FromMM(0.25)
    ds.m_HoleToHoleMin = pcbnew.FromMM(0.25)
    ds.m_SilkClearance = pcbnew.FromMM(0.0)

    ns = ds.m_NetSettings
    dflt = ns.GetDefaultNetclass()
    dflt.SetClearance(pcbnew.FromMM(0.15))
    dflt.SetTrackWidth(pcbnew.FromMM(0.2))
    dflt.SetViaDiameter(pcbnew.FromMM(0.6))
    dflt.SetViaDrill(pcbnew.FromMM(0.3))
    for name, width, nets in (("Power", 1.0, design.POWER_NETS), ("Rail", 0.4, design.RAIL_NETS)):
        nc = pcbnew.NETCLASS(name)
        nc.SetClearance(pcbnew.FromMM(0.2))
        nc.SetTrackWidth(pcbnew.FromMM(width))
        nc.SetViaDiameter(pcbnew.FromMM(0.8 if name == "Power" else 0.6))
        nc.SetViaDrill(pcbnew.FromMM(0.4 if name == "Power" else 0.3))
        ns.SetNetclass(name, nc)
        for n in nets:
            ns.SetNetclassPatternAssignment(n, name)

    # outline: 65 x 41, 3 mm corner radius on the two free corners
    edge = pcbnew.PCB_SHAPE(board)
    edge.SetShape(pcbnew.SHAPE_T_RECTANGLE) if False else None
    pts = rounded_rect(0, 0, W, H, 3.0)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(V(x0, y0)); seg.SetEnd(V(x1, y1))
        seg.SetLayer(pcbnew.Edge_Cuts); seg.SetWidth(pcbnew.FromMM(0.1))
        board.Add(seg)

    nets = {}
    for n in sorted(design.nets()):
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
        if c["lcsc"] is None:
            fp.SetExcludedFromBOM(True)
            fp.SetExcludedFromPosFiles(True)
        if c["dnp"]:
            fp.SetDNP(True)
        board.Add(fp)
        for p in fp.Pads():
            n = c["pins"].get(p.GetNumber())
            if n:
                p.SetNet(nets[n])
        fps[c["ref"]] = fp
        fp.SetPosition(V(0, 0))

    # J1 on the bottom: pin1 inner row at the SD end, pin2 toward the edge, pin3 along +x
    j1 = fps["J1"]
    orient_to(j1, "1", "3", (2.54, 0.0), flip=True)
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
        if pad_xy(j3, "A1")[0] < -3.0:      # contact pads behind (left of) the body
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
    ok = legalize(rects, movable, region)
    for ref in movable:
        fp = fps[ref]
        old = courtyard(fp)
        new = rects[ref]
        p = fp.GetPosition()
        fp.SetPosition(pcbnew.VECTOR2I(p.x + pcbnew.FromMM(new[0] - old[0]),
                                       p.y + pcbnew.FromMM(new[1] - old[1])))
    print("legalized:", ok)

    # Reference designators: small, on the fab layer; silkscreen gets curated labels later
    for ref, fp in fps.items():
        r = fp.Reference()
        r.SetTextSize(pcbnew.VECTOR2I_MM(0.6, 0.6))
        r.SetTextThickness(pcbnew.FromMM(0.1))
        if ref[0] in "RCDQUJ" and not ref.startswith(("J", "U1", "U2", "U3", "U4", "U6")):
            r.SetLayer(pcbnew.B_Fab if fp.IsFlipped() else pcbnew.F_Fab)
        fp.Value().SetVisible(False)

    board.Save(PCB)
    pcbnew.ExportSpecctraDSN(board, os.path.join(OUT, "build", "hasler-hat.dsn"))
    print("placed %d footprints" % len(fps))


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


if __name__ == "__main__":
    os.makedirs(os.path.join(OUT, "build"), exist_ok=True)
    if sys.argv[1] == "place":
        place()
