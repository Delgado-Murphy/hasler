"""Fabrication outputs for JLCPCB: Gerbers + drill (zipped), BOM and CPL CSVs.

    <KiCad python3> make_fab.py ..      # writes ../fab/ (needs pcbnew)

Needs kicad-cli (KiCad 9). The BOM comes from design.py, so it lists exactly the
parts with an LCSC number, minus DNP. The CPL comes from KiCad's position file,
with JLC's rotation offsets applied (KiCad footprints are IPC-7351 zero-oriented,
JLC's library is tape-oriented); always check the rotations in JLC's preview.
"""

import csv
import os
import re
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design  # noqa: E402

KICAD_CLI = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
PCB = os.path.join(OUT, "hasler-hat.kicad_pcb")
FAB = os.path.join(OUT, "fab")

# Footprint-name regex -> degrees added for JLC (after kicad-jlcpcb-tools' table).
ROTATIONS = [
    (r"^SOT-23", 180), (r"^SOIC-", 270), (r"^SSOP-", 270), (r"^VSSOP-", 270),
    (r"^CP_Elec_", 180), (r"^R_Array_Convex_4x0603", 90),
    # (the HRO USB-C needs no offset: checked in JLC's 3D preview, mouth out of the edge)
    # JLC draws pin headers/sockets lying along X; KiCad's stand along Y
    (r"^IDC-Header_2x", 270), (r"^PinHeader_2x", 270), (r"^PinSocket_2x", 270),
]
# Footprints whose origin is pin 1, not the body: JLC places by centre, so the CPL
# gives the centre of their pads instead.
PIN1_ORIGIN = r"^(IDC-Header_|PinHeader_|PinSocket_|TerminalBlock_)"


def pad_centres():
    """ref -> (x, y) pad-bounding-box centre, mm, relative to the aux origin, y up."""
    import pcbnew
    b = pcbnew.LoadBoard(PCB)
    ox, oy = [pcbnew.ToMM(v) for v in b.GetDesignSettings().GetAuxOrigin()]
    out = {}
    for f in b.GetFootprints():
        ps = [p.GetPosition() for p in f.Pads()]
        if not ps:
            continue
        xs = [pcbnew.ToMM(p.x) for p in ps]
        ys = [pcbnew.ToMM(p.y) for p in ps]
        out[f.GetReference()] = ((min(xs) + max(xs)) / 2 - ox, oy - (min(ys) + max(ys)) / 2)
    return out


def run(*args):
    subprocess.run([KICAD_CLI] + list(args), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def gerbers():
    g = os.path.join(FAB, "gerber")
    shutil.rmtree(g, ignore_errors=True)
    os.makedirs(g)
    layers = "F.Cu,In1.Cu,In2.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,F.Mask,B.Mask,Edge.Cuts"
    run("pcb", "export", "gerbers", "--layers", layers, "--subtract-soldermask",
        "--use-drill-file-origin", "-o", g + "/", PCB)
    run("pcb", "export", "drill", "--format", "excellon", "--drill-origin", "absolute",
        "--excellon-separate-th", "--generate-map", "--map-format", "gerberx2", "-o", g + "/", PCB)
    z = os.path.join(FAB, "hasler-hat-gerbers.zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as f:
        for n in sorted(os.listdir(g)):
            f.write(os.path.join(g, n), n)
    return z


def bom():
    groups = {}
    for c in design.COMPONENTS:
        if not c["lcsc"] or c["dnp"]:
            continue
        comment = c["mpn"] if c["mpn"] != c["value"] else c["value"]
        key = (c["lcsc"], comment, c["footprint"].split(":")[1])
        groups.setdefault(key, []).append(c["ref"])
    path = os.path.join(FAB, "hasler-hat-bom.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for (lcsc, value, fp), refs in sorted(groups.items(), key=lambda kv: kv[1][0]):
            refs.sort(key=lambda r: (re.sub(r"\d", "", r), int(re.sub(r"\D", "", r) or 0)))
            w.writerow([value, ",".join(refs), fp, lcsc])
    return path, sum(len(v) for v in groups.values()), len(groups)


def cpl():
    raw = os.path.join(FAB, "pos-raw.csv")
    run("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both",
        "--use-drill-file-origin", "-o", raw, PCB)
    keep = {c["ref"] for c in design.COMPONENTS if c["lcsc"] and not c["dnp"]}
    centres = pad_centres()
    path = os.path.join(FAB, "hasler-hat-cpl.csv")
    with open(raw) as f, open(path, "w", newline="") as g:
        w = csv.writer(g)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for row in csv.DictReader(f):
            ref = row["Ref"]
            if ref not in keep:
                continue
            rot = float(row["Rot"])
            for pat, add in ROTATIONS:
                if re.match(pat, row["Package"]):
                    rot += add
                    break
            x, y = row["PosX"], row["PosY"]
            if re.match(PIN1_ORIGIN, row["Package"]):
                x, y = ("%.4f" % v for v in centres[ref])
            w.writerow([ref, x, y, "Top" if row["Side"] == "top" else "Bottom",
                        "%g" % (rot % 360)])
    os.remove(raw)
    return path


if __name__ == "__main__":
    os.makedirs(FAB, exist_ok=True)
    print(gerbers())
    p, n, k = bom()
    print(p, "%d placements, %d line items" % (n, k))
    print(cpl())
