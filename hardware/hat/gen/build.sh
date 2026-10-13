#!/bin/sh
# Regenerate the whole Hasler HAT from design.py: schematic, board, routing, checks, fab files.
#
#   FREEROUTING_JAR=... JAVA=... gen/build.sh        (from hardware/hat/)
#
# Needs KiCad 9 (kicad-cli + its Python), Freerouting 2.4 on Java 25. GND is not autorouted:
# make_pcb.py place fans every SMD GND pad out to the In1 plane with a locked via first, it is
# dropped from the DSN, and finish pours GND on the other layers and closes any leftovers.
set -eu
cd "$(dirname "$0")/.."
KICAD=/Applications/KiCad/KiCad.app/Contents
KPY=$KICAD/Frameworks/Python.framework/Versions/Current/bin/python3
CLI=$KICAD/MacOS/kicad-cli
: "${FREEROUTING_JAR:?set FREEROUTING_JAR}" "${JAVA:=java}" "${PASSES:=100}"

python3 gen/make_sch.py .
$CLI sch erc --severity-all --exit-code-violations -o build/erc.rpt hasler-hat.kicad_sch >/dev/null
$KPY gen/make_pcb.py place . > build/place.log 2>&1 || { grep -v Fontconfig build/place.log | tail -5; exit 1; }
grep -E 'overlaps|fan-out|placed' build/place.log || true

# signals only
python3 - <<'EOF'
import re
s = open("build/hasler-hat.dsn").read()
# GND is fanned out to the In1 plane before routing (locked vias), so the router skips it
s2, n = re.subn(r'\(net /GND\s*\n\s*\(pins[^)]*\)\s*\)', '', s)
assert n == 1, n
# KiCad's exporter gives the bottom-side socket J1 top/bottom-only padstacks ([TB]), so the
# router would run inner-layer tracks through its pins. Use the identical all-layer ones.
for shape in ("Round", "Rect"):
    s2 = s2.replace("(pin %s[TB]Pad_1700" % shape, "(pin %s[A]Pad_1700" % shape)
assert "(pin Round[TB]Pad_1700" not in s2 and "(padstack Round[A]Pad_1700" in s2
open("build/hasler-hat-nognd.dsn", "w").write(s2)
EOF
rm -f build/hasler-hat.ses
"$JAVA" -Djava.awt.headless=true -jar "$FREEROUTING_JAR" -de build/hasler-hat-nognd.dsn \
    -do build/hasler-hat.ses -mp "$PASSES" -mt 6 --gui.enabled=false > build/freerouting.log 2>&1
grep -E 'Optimization stage completed' build/freerouting.log | sed -E 's/.*final score: ([^,]*\)).*/routing: \1/'

$KPY gen/make_pcb.py finish . > build/finish.log 2>&1 || { grep -v Fontconfig build/finish.log | tail -5; exit 1; }
grep -E 'widened|stitched|grid|gap pass|grid pass|brought|final pass|solid GND|finished' build/finish.log || true
# DRC (all severities, schematic parity) must be clean before any fab file is written
if ! $CLI pcb drc --schematic-parity --severity-all --exit-code-violations -o build/drc.rpt \
        hasler-hat.kicad_pcb >/dev/null; then
    grep -E '^\[' build/drc.rpt | sort | uniq -c | sort -rn
    echo "DRC failed: see build/drc.rpt (no fab files written)"; exit 1
fi
echo "DRC: clean"
$KPY gen/make_fab.py .
