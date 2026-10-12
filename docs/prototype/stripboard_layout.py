"""Stripboard layout for the Hasler prototype board, with a connectivity check.

Strips run vertically (column x, rows y). Parts put pins in holes; cuts split a
strip between rows y and y+1; links join two holes. The checker unions holes
and verifies every connected group carries exactly one net and every net is
one group.
"""
import itertools, json, sys, os
DYW = float(os.environ.get("DYW", "1.2"))
W, H = 39, 31
JX, JY, MIR, SWAP, PX, PY = map(int, os.environ.get("PLACE", "1 8 0 0 1 1").split())

pins = {}      # (x,y) -> (part, label, net)
def pin(x, y, part, label, net):
    assert (x, y) not in pins, ((x, y), pins.get((x, y)), part, label)
    pins[(x, y)] = (part, label, net)

# P: Pi ribbon, 1x8, pin 4 pulled. Top-left.
P = ["3V3", "SDA", "SCL", None, "GND", "LR_PI", "LG_PI", "LB_PI"]
REV = int(os.environ.get("PREV", "0"))
for i, n in enumerate(P):
    if n: pin(PX + (7 - i if REV else i), PY, "P", str(i + 1), n)

# R1-R3 standing, each across a cut on its own lamp strip.
for x, (a, b, r) in zip(((PX + 2, PX + 1, PX) if REV else (PX + 5, PX + 6, PX + 7)), (("LR_PI", "LR", "R1 220"), ("LG_PI", "LG", "R2 100"), ("LB_PI", "LB", "R3 100"))):
    pin(x, PY + 2, r.split()[0], "a", a); pin(x, PY + 3, r.split()[0], "b", b)

# J2 PANEL 2x8, columns x=1..8; odd row (pins 1,3..15) y=8, even row (2..16) y=9.
J2odd = {1: "GND", 3: "PA1", 5: "PA3", 7: "PA5", 9: "TONE_W", 11: "3V3", 13: "PB0", 15: "LG"}
J2even = {2: "PA0", 4: "PA2", 6: "PA4", 8: "PA6", 10: "VOL_W", 12: "GND", 14: "LR", 16: "LB"}
for c in range(8):
    x = JX + (7 - c if MIR else c)
    ya, yb = (JY, JY + 1) if not SWAP else (JY + 1, JY)
    pin(x, ya, "J2", str(2 * c + 1), J2odd[2 * c + 1])
    pin(x, yb, "J2", str(2 * c + 2), J2even[2 * c + 2])

# U1 green MCP23017, long edge only, y=22, x=10..29. Body below (rows 23-30).
U1 = ["PA7", "PA6", "PA5", "PA4", "PA3", "PA2", "PA1", "PA0", "GND", "3V3",
      "PB0", "PB1", "PB2", "PB3", "PB4", "PB5", "PB6", "PB7", "GND2", "3V3_2"]
for i, n in enumerate(U1):
    net = {"GND2": "GND", "3V3_2": "3V3", "PB7": "NC_PB7"}.get(n, n)
    pin(10 + i, 22, "U1", n, net)

# J3 EXP 1x10, y=19, x=21..30.
J3 = ["PB1", "PB2", "PB3", "PB4", "PB5", "PB6", "A2", "A3", "3V3", "GND"]
for i, n in enumerate(J3): pin(21 + i, 19, "J3", n, n)

# U2 ADS1115 socket 1x10, y=12, x=19..28. Body above (rows 5-11).
U2 = ["3V3", "GND", "SCL", "SDA", "GND", "NC_ALRT", "VOL_W", "TONE_W", "A2", "A3"]
U2lab = ["VDD", "GND", "SCL", "SDA", "ADDR", "ALRT", "A0", "A1", "A2", "A3"]
for i, (n, l) in enumerate(zip(U2, U2lab)): pin(19 + i, 12, "U2", l, n)

pin(19, 15, "C2", "+", "3V3"); pin(20, 15, "C2", "-", "GND")
pin(19, 17, "C1", "+", "3V3"); pin(20, 16, "C1", "-", "GND")
# C2 100 nF across the rails at the entry: 3V3 strip x1 and GND strip x5? (not adjacent) ->
# place C2 standing between holes on two segments chosen after routing; C1 220uF likewise.
nets_needing = {}
for (x, y), (_, _, n) in pins.items():
    nets_needing.setdefault(n, []).append((x, y))

# ---- automatic cuts: between consecutive pins of different nets on a column ----
cuts = set()       # (x, y): cut between y and y+1
for x in range(W):
    col = sorted((y, pins[(x, y)][2]) for (xx, y) in pins if xx == x)
    for (y0, n0), (y1, n1) in zip(col, col[1:]):
        if n0 != n1:
            # cut right after the first pin's run, leaving free holes on both sides if possible
            gap = y1 - y0
            cy = y0 + max(0, (gap - 1) // 2) if gap > 2 else y0
            if gap == 1: cy = y0
            cuts.add((x, cy))

def segments():
    seg = {}
    for x in range(W):
        sid = 0
        for y in range(H):
            seg[(x, y)] = (x, sid)
            if (x, y) in cuts: sid += 1
    return seg

seg = segments()
# net per segment
segnet = {}
for h, (_, _, n) in pins.items():
    s = seg[h]
    assert segnet.get(s, n) == n, ("conflict", s, segnet.get(s), n)
    segnet[s] = n
used = set(pins)
links = []

def free_holes(s):
    x, sid = s
    return [(x, y) for y in range(H) if seg[(x, y)] == s and (x, y) not in used and not body(x, y)]

def body(x, y):
    return (19 <= x <= 28 and 5 <= y <= 11) or (8 <= x <= 31 and 23 <= y <= 30)

# Keep-out: under module bodies (U1 rows 23-30 x8..31 ok for links? avoid), U2 body rows 5-11 x19..28
def blocked(h):
    x, y = h
    return False

INTERNAL = {"GND": [(18, 22), (28, 22)], "3V3": [(19, 22), (29, 22)]}
def overlaps(ha, hb):
    for (pa, pb, _) in links:
        if ha[1] == hb[1] == pa[1] == pb[1]:
            lo, hi = sorted((ha[0], hb[0])); plo, phi = sorted((pa[0], pb[0]))
            if lo < phi and plo < hi: return True
        if ha[0] == hb[0] == pa[0] == pb[0]:
            lo, hi = sorted((ha[1], hb[1])); plo, phi = sorted((pa[1], pb[1]))
            if lo < phi and plo < hi: return True
        # a link passing over another link's end hole is fine physically (insulated), skip
    return False

def join(n):
    segs = sorted({seg[h] for h in nets_needing[n]})
    if n in INTERNAL:
        a, b = (seg[h] for h in INTERNAL[n])
        if a != b and b in segs:
            segs.remove(b)
    # greedy chain by nearest free holes
    comp = [segs[0]]; rest = segs[1:]
    while rest:
        best = None
        for a in comp:
            for b in rest:
                for ha in free_holes(a):
                    for hb in free_holes(b):
                        if overlaps(ha, hb): continue
                        d = abs(ha[0] - hb[0]) + abs(ha[1] - hb[1]) * DYW
                        if best is None or d < best[0]:
                            best = (d, a, b, ha, hb)
        assert best, ("cannot join", n, comp, rest)
        _, a, b, ha, hb = best
        links.append((ha, hb, n)); used.add(ha); used.add(hb)
        comp.append(b); rest.remove(b)

skip = {"NC_PB7", "NC_ALRT", "U1GND_DUP", "U1VCC_DUP"}
# SDA/SCL from U1's end block arrive by flying wire onto the SDA/SCL nets (drawn separately).
order = ["GND", "3V3", "SDA", "SCL", "PA0", "PA1", "PA2", "PA3", "PA4", "PA5", "PA6",
         "PB0", "VOL_W", "TONE_W", "LR", "LG", "LB", "A2", "A3"] + [f"PB{i}" for i in range(1, 7)]
for n in order:
    if n in nets_needing and n not in skip: join(n)

# ---- verify with union-find ----
parent = {}
def f(a):
    parent.setdefault(a, a)
    while parent[a] != a:
        parent[a] = parent[parent[a]]; a = parent[a]
    return a
def u(a, b): parent[f(a)] = f(b)
for x in range(W):
    for y in range(H - 1):
        if (x, y) not in cuts: u((x, y), (x, y + 1))
for a, b, _ in links: u(a, b)
u((18, 22), (28, 22)); u((19, 22), (29, 22))   # bussed inside the breakout
groups = {}
for h, (_, _, n) in pins.items():
    groups.setdefault(f(h), set()).add(n)
bad = [g for g in groups.values() if len(g) > 1]
netgroups = {}
for h, (_, _, n) in pins.items():
    netgroups.setdefault(n, set()).add(f(h))
split = {n: len(g) for n, g in netgroups.items() if len(g) > 1 and n not in skip}
print("pins", len(pins), "cuts", len(cuts), "links", len(links))
print("conflicting groups:", bad)
print("split nets:", split)
for a, b, n in links: print(f"  link {n:7s} {a} -> {b}  len {abs(a[0]-b[0])+abs(a[1]-b[1])}")
json.dump({"pins": [[k[0], k[1], *v] for k, v in pins.items()], "cuts": sorted(cuts),
           "links": [[*a, *b, n] for a, b, n in links]}, open(sys.argv[1] if len(sys.argv) > 1 else "layout.json", "w"))
