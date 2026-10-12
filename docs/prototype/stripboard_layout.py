"""Stripboard v5: J2 2x13 directly above U1 so its lower row lands on U1 strips.
Keys are mapped to whichever MCP pin their J2 pin lands on (descriptor follows)."""
import json, sys, os, itertools
W, H = 39, 31
UX, UY = int(os.environ.get("UX", 19)), int(os.environ.get("UY", 9))       # U2 socket
PX, PY, REV = map(int, os.environ.get("PP", "1 2 0").split())             # P header
pins = {}
def pin(x, y, part, label, net):
    assert 1 <= y <= H - 2 and 0 <= x < W, (part, label, x, y)
    assert (x, y) not in pins, ((x, y), pins[(x, y)], part, label)
    pins[(x, y)] = (part, label, net)
# U1 long edge row 22, x10..29
U1lab = ["PA7","PA6","PA5","PA4","PA3","PA2","PA1","PA0","GND","VCC","PB0","PB1","PB2","PB3","PB4","PB5","PB6","PB7","GND","VCC"]
MAP = {"PA4": "K1", "PA3": "K3", "PA2": "K5", "PA1": "PU", "PA0": "K2", "PA5": "K4", "PA6": "K6",
       "PB0": "TONE", "PB1": "EXP2", "PB2": "EXP4", "PB3": "EXP6", "PB4": "EXP1", "PB5": "EXP3", "PB6": "EXP5",
       "GND": "GND", "VCC": "3V3", "PA7": "NC_PA7", "PB7": "NC_PB7"}
_k = os.environ.get("KMAP"); _e = os.environ.get("EMAP")
if _k:
    for p, n in zip(("PA0", "PA5", "PA6"), _k.split(",")): MAP[p] = n
if _e:
    for p, n in zip(("PB4", "PB5", "PB6"), _e.split(",")): MAP[p] = n
for i, l in enumerate(U1lab): pin(10 + i, 22, "U1", l, MAP[l])
# J2 2x13 at x13..25: odd row y18 (up), even row y19 (down to U1)
odd = ["GND","K2","K4","K6","TONE_W","3V3","TONE","LG","EXP1","EXP3","EXP5","A2","3V3"]
even = ["K1","K3","K5","PU","VOL_W","GND","LR","LB","EXP2","EXP4","EXP6","A3","GND"]
for c in range(13):
    pin(13 + c, 18, "J2", str(2 * c + 1), odd[c]); pin(13 + c, 19, "J2", str(2 * c + 2), even[c])
# U2 ADS1115 socket, body above it (7 rows)
for i, (l, n) in enumerate(zip(["VDD","GND","SCL","SDA","ADDR","ALRT","A0","A1","A2","A3"],
                               ["3V3","GND","SCL","SDA","GND","NC_ALRT","VOL_W","TONE_W","A2","A3"])):
    pin(UX + i, UY, "U2", l, n)
P = ["3V3","SDA","SCL",None,"GND","LR_PI","LG_PI","LB_PI"]
for i, n in enumerate(P):
    if n: pin(PX + (7 - i if REV else i), PY, "P", str(i + 1), n)
for k, (a, b) in enumerate((("LR_PI","LR"),("LG_PI","LG"),("LB_PI","LB"))):
    x = PX + ((7 - (5 + k)) if REV else 5 + k)
    pin(x, PY + 2, f"R{k+1}", "a", a); pin(x, PY + 3, f"R{k+1}", "b", b)
CX, CY = map(int, os.environ.get("CC", "34 10").split())
pin(CX, CY, "C1", "+", "3V3"); pin(CX + 1, CY, "C1", "-", "GND")
pin(CX, CY + 2, "C2", "a", "3V3"); pin(CX + 1, CY + 2, "C2", "b", "GND")
def body(x, y):
    return (UX - 1 <= x <= UX + 10 and UY - 7 <= y <= UY - 1) or (8 <= x <= 35 and 23 <= y <= 30)
for (x, y) in pins:
    if body(x, y): raise SystemExit("pin under a body")
nets = {}
for h, (_, _, n) in pins.items(): nets.setdefault(n, []).append(h)
cuts = set()
for x in range(W):
    col = sorted((y, pins[(x, y)][2]) for (xx, y) in pins if xx == x)
    for (y0, n0), (y1, n1) in zip(col, col[1:]):
        if n0 != n1:
            gap = y1 - y0
            cuts.add((x, y0 + (gap - 1) // 2 if gap > 2 else y0))
def segof():
    seg = {}
    for x in range(W):
        sid = 0
        for y in range(H):
            if y in (0, H - 1): seg[(x, y)] = (x, "e", y); continue
            seg[(x, y)] = (x, sid)
            if (x, y) in cuts: sid += 1
    return seg
seg = segof()
segnet = {}
for h, (_, _, n) in pins.items():
    s = seg[h]
    if segnet.get(s, n) != n: raise SystemExit(f"conflict {s} {segnet[s]} {n}")
    segnet[s] = n
used = set(pins); links = []
def free(s): return [(x, y) for (x, y), ss in seg.items() if ss == s and 1 <= y <= H - 2 and (x, y) not in used and not body(x, y)]
def overlaps(a, b):
    if a[1] == b[1]:
        lo, hi = sorted((a[0], b[0]))
        if any(pins[h][0] in ("C1", "C2", "R1", "R2", "R3") and h[1] == a[1] and lo < h[0] < hi for h in pins): return True
    for p, q, _ in links:
        if a[1] == b[1] == p[1] == q[1]:
            lo, hi = sorted((a[0], b[0])); plo, phi = sorted((p[0], q[0]))
            if lo < phi and plo < hi: return True
        if a[0] == b[0] == p[0] == q[0]:
            lo, hi = sorted((a[1], b[1])); plo, phi = sorted((p[1], q[1]))
            if lo < phi and plo < hi: return True
    return False
INTERNAL = {"GND": [(18, 22), (28, 22)], "3V3": [(19, 22), (29, 22)]}
skip = {"NC_PA7", "NC_PB7", "NC_ALRT"}
for n in sorted(nets, key=lambda n: -len({seg[h] for h in nets[n]})):
    if n in skip: continue
    segs = sorted({seg[h] for h in nets[n]}, key=str)
    if n in INTERNAL:
        a, b = (seg[h] for h in INTERNAL[n])
        if a != b and b in segs and a in segs: segs.remove(b)
    comp, rest = [segs[0]], segs[1:]
    while rest:
        best = None
        for a in comp:
            fa = free(a)
            for b in rest:
                for ha in fa:
                    for hb in free(b):
                        if overlaps(ha, hb): continue
                        d = abs(ha[0] - hb[0]) + 3 * abs(ha[1] - hb[1])
                        if best is None or d < best[0]: best = (d, b, ha, hb)
        if not best: raise SystemExit(f"cannot join {n}")
        _, b, ha, hb = best
        links.append((ha, hb, n)); used |= {ha, hb}; comp.append(b); rest.remove(b)
# verify
parent = {}
def f(a):
    parent.setdefault(a, a)
    while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
    return a
for x in range(W):
    for y in range(1, H - 2):
        if (x, y) not in cuts: parent[f((x, y))] = f((x, y + 1))
for a, b, _ in links: parent[f(a)] = f(b)
parent[f((18, 22))] = f((28, 22)); parent[f((19, 22))] = f((29, 22))
g = {}
for h, (_, _, n) in pins.items(): g.setdefault(f(h), set()).add(n)
bad = [x for x in g.values() if len(x) > 1]
ng = {}
for h, (_, _, n) in pins.items(): ng.setdefault(n, set()).add(f(h))
split = {n: len(v) for n, v in ng.items() if len(v) > 1 and n not in skip}
def crosses(a, b):
    (x1, y1), (x2, y2), _ = a; (x3, y3), (x4, y4), _ = b
    if {(x1, y1), (x2, y2)} & {(x3, y3), (x4, y4)}: return False
    o = lambda ax, ay, bx, by, cx, cy: (bx-ax)*(cy-ay) - (by-ay)*(cx-ax)
    return o(x3,y3,x4,y4,x1,y1)*o(x3,y3,x4,y4,x2,y2) < 0 and o(x1,y1,x2,y2,x3,y3)*o(x1,y1,x2,y2,x4,y4) < 0
cr = sum(crosses(a, b) for a, b in itertools.combinations(links, 2))
L = sum(abs(a[0]-b[0]) + abs(a[1]-b[1]) for a, b, _ in links)
diag = sum(1 for a, b, _ in links if a[0] != b[0] and a[1] != b[1])
print(f"RESULT bad={len(bad)} split={len(split)} links={len(links)} cuts={len(cuts)} crossings={cr} diag={diag} length={L}")
fly = {}
for n in ("SDA", "SCL"):
    s0 = seg[next(h for h in nets[n] if pins[h][0] == "P")]
    cand = [h for h in free(s0) if h not in fly.values()]
    fly[n] = max(cand, key=lambda h: (h[1], h[0]))
    used.add(fly[n])
print("flying", fly)
if len(sys.argv) > 1:
    json.dump({"pins": [[k[0], k[1], *v] for k, v in pins.items()], "cuts": sorted(cuts),
               "links": [[*a, *b, n] for a, b, n in links], "map": MAP, "fly": fly}, open(sys.argv[1], "w"))
