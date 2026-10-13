"""Quick HAT sanity check, seconds not minutes: python3 gen/check.py   (from hardware/hat/)

No KiCad needed. Checks design.py itself, the committed BOM against it, and the
eFuse thresholds the docs quote.
"""
import collections
import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import design  # noqa: E402

fail = []
refs = [c["ref"] for c in design.COMPONENTS]
dup = [r for r, n in collections.Counter(refs).items() if n > 1]
if dup:
    fail.append("duplicate refs: %s" % dup)
single = [n for n, v in design.nets().items() if len(v) < 2]
if single:
    fail.append("single-pin nets: %s" % single)
for c in design.COMPONENTS:
    if c["lcsc"] and not re.fullmatch(r"C\d+", c["lcsc"]):
        fail.append("%s: bad LCSC %r" % (c["ref"], c["lcsc"]))

# committed BOM == what design.py says (make_fab.bom()'s grouping)
groups = {}
for c in design.COMPONENTS:
    if c["lcsc"] and not c["dnp"]:
        comment = c["mpn"] if c["mpn"] != c["value"] else c["value"]
        groups.setdefault((c["lcsc"], comment, c["footprint"].split(":")[1]), set()).add(c["ref"])
want = {k: v for k, v in groups.items()}
with open(os.path.join(HERE, "..", "fab", "hasler-hat-bom.csv")) as f:
    got = {(r["LCSC Part #"], r["Comment"], r["Footprint"]): set(r["Designator"].split(","))
           for r in csv.DictReader(f)}
if got != want:
    fail.append("fab/hasler-hat-bom.csv is stale: rerun make_fab.py")
cpl = {r["Designator"] for r in csv.DictReader(open(os.path.join(HERE, "..", "fab", "hasler-hat-cpl.csv")))}
if cpl != set().union(*want.values()):
    fail.append("CPL refs != BOM refs: %s" % sorted(cpl ^ set().union(*want.values())))

# eFuse thresholds (1.20 V rising / 1.09 V falling), as quoted in DESIGN.md / ORDERING.md
val = {c["ref"]: c["value"] for c in design.COMPONENTS}
ohm = lambda r: float(val[r].replace("k", "e3").replace("M", "e6").replace("Ω", ""))  # noqa: E731
uv = (ohm("R3") + ohm("R4") + ohm("R16")) / (ohm("R4") + ohm("R16"))   # with JP11 bridged
ov = (ohm("R5") + ohm("R6") + ohm("R7")) / (ohm("R6") + ohm("R7"))
pg = (ohm("R8") + ohm("R9")) / ohm("R9")
for name, got_v, doc in (("UVLO on", 1.2 * uv, 10.3), ("UVLO off", 1.09 * uv, 9.35),
                         ("OVLO off", 1.2 * ov, 22.6), ("OVLO on", 1.09 * ov, 20.55),
                         ("PG", 1.2 * pg, 11.2), ("ILIM", 3334 / ohm("R14"), 3.3)):
    if abs(got_v - doc) > 0.05:
        fail.append("%s: design gives %.2f, docs say %.1f" % (name, got_v, doc))
if [c["ref"] for c in design.COMPONENTS if c["dnp"]] != ["C7"]:
    fail.append("DNP set changed: update ORDERING.md")

print("%d components, %d nets, %d BOM lines / %d placements"
      % (len(refs), len(design.nets()), len(want), sum(len(v) for v in want.values())))
for f in fail:
    print("FAIL:", f)
sys.exit(1 if fail else 0)
