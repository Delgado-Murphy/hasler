import json
d = json.load(open("v3.json"))
W, H, S, OX, OY = 39, 31, 24, 40, 56
JYTOP = min(p[1] for p in d["pins"] if p[2]=="J2")
def X(x): return OX + x * S + S / 2
def Y(y): return OY + y * S + S / 2
NETC = {"3V3": "#E5484D", "GND": "#9A948A", "SDA": "#46D17A", "SCL": "#5B8CFF",
        "LR": "#FF6B5B", "LG": "#6FE09A", "LB": "#7EA6FF", "LR_PI": "#FF8C3A", "LG_PI": "#EFE6D2", "LB_PI": "#A97BFF",
        "VOL_W": "#FFD34D", "TONE_W": "#5FD4E6"}
def col(n):
    if n in NETC: return NETC[n]
    if n.startswith("PA") or n in ("PB0",): return "#FFB238"
    if n.startswith("PB") or n in ("A2", "A3"): return "#C9A15A"
    return "#6E675D"
out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{OX*2+W*S}" height="{OY+H*S+70}" viewBox="0 0 {OX*2+W*S} {OY+H*S+70}">']
out.append(f'<rect x="{OX-10}" y="{OY-10}" width="{W*S+20}" height="{H*S+20}" rx="10" fill="#1C1814" stroke="#3A332B"/>')
# copper strips (vertical), with gaps at cuts
cuts = {(x, y) for x, y in d["cuts"]}
for x in range(W):
    y0 = 0
    for y in range(H):
        if (x, y) in cuts or y == H - 1:
            y1 = y
            out.append(f'<rect x="{X(x)-8}" y="{Y(y0)-9}" width="16" height="{(y1-y0)*S+18}" rx="4" fill="#5A3B22" opacity="0.55"/>')
            y0 = y + 1
for x in range(W):
    for y in range(H):
        out.append(f'<circle cx="{X(x)}" cy="{Y(y)}" r="2.6" fill="#14110F"/>')
# column / row numbers
for x in range(0, W, 2):
    out.append(f'<text x="{X(x)}" y="{OY-18}" font-family="JetBrains Mono" font-size="11" fill="#8A8378" text-anchor="middle">{x}</text>')
for y in range(0, H, 2):
    out.append(f'<text x="{OX-20}" y="{Y(y)+4}" font-family="JetBrains Mono" font-size="11" fill="#8A8378" text-anchor="middle">{y}</text>')
# module outlines
def box(x0, y0, x1, y1, label, dashed=False, color="#3E6B4A"):
    da = ' stroke-dasharray="6 5"' if dashed else ''
    out.append(f'<rect x="{X(x0)-12}" y="{Y(y0)-12}" width="{(x1-x0)*S+24}" height="{(y1-y0)*S+24}" rx="6" fill="{color}" fill-opacity="0.16" stroke="{color}" stroke-width="1.5"{da}/>')
    out.append(f'<text x="{X(x0)-4}" y="{Y(y1)+4}" font-family="JetBrains Mono" font-size="12" font-weight="700" fill="#EFE6D2">{label}</text>')
box(8, 22, 35, 29.4, "U1  green MCP23017 · body over rows 23-30 · end block NOT plugged in", color="#3E8A57")
box(19, 5, 28, 12, "", dashed=True, color="#5B8CFF")
out.append(f'<text x="{X(19)-4}" y="{Y(5)+6}" font-family="JetBrains Mono" font-size="12" font-weight="700" fill="#EFE6D2">U2 ADS1115 · socket · phase 3b</text>')
# cuts
for x, y in d["cuts"]:
    cy = (Y(y) + Y(y + 1)) / 2
    out.append(f'<path d="M{X(x)-7} {cy-5} L{X(x)+7} {cy+5} M{X(x)-7} {cy+5} L{X(x)+7} {cy-5}" stroke="#FF4D3D" stroke-width="2.6" stroke-linecap="round"/>')
# links
for i, (x1, y1, x2, y2, n) in enumerate(d["links"], 1):
    c = col(n)
    mx, my = (X(x1) + X(x2)) / 2, (Y(y1) + Y(y2)) / 2
    out.append(f'<text x="{mx}" y="{my - 6}" font-family="JetBrains Mono" font-size="10" font-weight="700" fill="{c}" text-anchor="middle">L{i}</text>')
    out.append(f'<path d="M{X(x1)} {Y(y1)} L{X(x2)} {Y(y2)}" stroke="{c}" stroke-width="3.4" stroke-linecap="round" opacity="0.95"/>')
    for x, y in ((x1, y1), (x2, y2)):
        out.append(f'<circle cx="{X(x)}" cy="{Y(y)}" r="4.2" fill="{c}"/>')
# flying wires from U1 end block (off to the right, rows 23-29) to SDA/SCL strips
out.append(f'<path d="M{X(36)} {Y(25)} C {X(37)} {Y(18)}, {X(26)} {Y(3)}, {X(22)} {Y(3)}" fill="none" stroke="#46D17A" stroke-width="2.4" stroke-dasharray="7 5"/>')
out.append(f'<path d="M{X(36)} {Y(26)} C {X(38)} {Y(17)}, {X(25)} {Y(2)}, {X(21)} {Y(2)}" fill="none" stroke="#5B8CFF" stroke-width="2.4" stroke-dasharray="7 5"/>')
for x, y, c in ((22, 3, "#46D17A"), (21, 2, "#5B8CFF")):
    out.append(f'<circle cx="{X(x)}" cy="{Y(y)}" r="4.2" fill="{c}"/>')
out.append(f'<text x="{X(29)}" y="{Y(8)}" font-family="JetBrains Mono" font-size="11" fill="#8A8378">flying wires from the</text><text x="{X(29)}" y="{Y(8)+14}" font-family="JetBrains Mono" font-size="11" fill="#8A8378">U1 end block: SDA → 22·3,</text><text x="{X(29)}" y="{Y(8)+28}" font-family="JetBrains Mono" font-size="11" fill="#8A8378">SCL → 21·2</text>')
# pins
for x, y, part, label, net in d["pins"]:
    c = col(net)
    sq = part in ("P", "J2", "J3") and label == "1"
    if sq:
        out.append(f'<rect x="{X(x)-6}" y="{Y(y)-6}" width="12" height="12" fill="{c}" stroke="#14110F" stroke-width="1.5"/>')
    else:
        out.append(f'<circle cx="{X(x)}" cy="{Y(y)}" r="6" fill="{c}" stroke="#14110F" stroke-width="1.5"/>')
# part labels and pin names
labels = {}
for x, y, part, label, net in d["pins"]:
    labels.setdefault(part, []).append((x, y, label, net))
def name_row(part, dy, rot=False, show="label"):
    for x, y, label, net in labels[part]:
        t = label if show == "label" else net
        if rot:
            out.append(f'<text transform="translate({X(x)+4} {Y(y)+dy}) rotate(-90)" font-family="JetBrains Mono" font-size="10" fill="#CFC6B4">{t}</text>')
        else:
            out.append(f'<text x="{X(x)}" y="{Y(y)+dy}" font-family="JetBrains Mono" font-size="10" fill="#CFC6B4" text-anchor="middle">{t}</text>')
name_row("U1", 34, rot=True)
name_row("U2", -14, rot=True)
name_row("J3", -12, rot=True)
for x, y, label, net in labels["P"]:
    out.append(f'<text transform="translate({X(x)+4} {Y(y)-10}) rotate(-90)" font-family="JetBrains Mono" font-size="10" fill="#CFC6B4">{net.replace("_PI","")}</text>')
for x, y, label, net in labels["J2"]:
    out.append(f'<text x="{X(x)}" y="{Y(y)+(-11 if y==JYTOP else 21)}" font-family="JetBrains Mono" font-size="9" fill="#CFC6B4" text-anchor="middle">{label}</text>')
def tag(x, y, t, c="#FFB238", anchor="start"):
    out.append(f'<text x="{x}" y="{y}" font-family="JetBrains Mono" font-size="12" font-weight="700" fill="{c}" text-anchor="{anchor}">{t}</text>')
tag(X(16)-12, Y(1)-38, "P → Pi (reversed: pin 1 at the right)")
tag(X(3)-12, Y(JYTOP)-26, "J2 PANEL 2×8 · pin 1 = square · cabinet ribbon")
tag(X(21)-12, Y(19)+30, "J3 EXP")
tag(X(15)-10, Y(3)+5, "R3 R2 R1", "#CFC6B4", "end")
tag(X(15)-10, Y(4)+9, "standing, across a cut", "#8A8378", "end")
tag(X(21)+10, Y(16)+5, "C2 100n · C1 220µ", "#CFC6B4")
out.append("</svg>")
open("board.svg", "w").write("".join(out))
print(len("".join(out)))
