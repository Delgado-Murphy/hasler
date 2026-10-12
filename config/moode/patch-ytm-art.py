#!/usr/bin/env python3
"""Give moOde's player cover art for haslerd's YouTube Music tracks.

moOde treats our tracks (http://127.0.0.1:8099/ytm/<id>, with a duration) as
UPnP files and asks a UPnP renderer for artwork - there isn't one, so it shows
its default cover. This swaps that one line in /var/www/inc/mpd.php: for our
tracks, moOde's PHP asks haslerd (127.0.0.1:8080/art/<id>, on the Pi) where the
cover is and gives the browser that public image URL. Not haslerd's URL itself:
Chrome blocks an http page on :80 loading from :8080 on the same LAN host
(Private Network Access). Every other track goes through moOde unchanged.

Run as root by scripts/install.sh; safe to re-run. A moOde update replaces
mpd.php and removes this - re-run install.sh to put it back (nothing breaks
meanwhile; the default cover shows). Backup: mpd.php.hasler-orig.

Exit 0 = patched or already patched; 1 = anchor not found (moOde changed),
left untouched; 2 = php -l rejected the result, original restored.
"""
import shutil
import subprocess
import sys
from pathlib import Path

TARGET = Path("/var/www/inc/mpd.php")
BACKUP = TARGET.with_name("mpd.php.hasler-orig")
ANCHOR = "$current['coverurl'] = getUpnpCoverUrl();"
MARKER = "// hasler-ytm-art"

text = TARGET.read_text()
replacement = (Path(__file__).with_name("ytm-art.php")).read_text().rstrip("\n") + "\n"
lines = text.splitlines(keepends=True)
ours = [i for i, line in enumerate(lines) if MARKER in line]
if ours:
    if lines[ours[0]] == replacement:
        print("already patched")
        sys.exit(0)
    hits = ours                     # an older version of our line: upgrade it
else:
    hits = [i for i, line in enumerate(lines) if line.strip() == ANCHOR]
if len(hits) != 1:
    print(f"anchor found {len(hits)} times - moOde changed; not patching")
    sys.exit(1)
if not BACKUP.exists():
    shutil.copy2(TARGET, BACKUP)
lines[hits[0]] = replacement
TARGET.write_text("".join(lines))
if subprocess.run(["php", "-l", str(TARGET)], capture_output=True).returncode != 0:
    shutil.copy2(BACKUP, TARGET)
    print("php -l rejected the patch - original restored")
    sys.exit(2)
print("patched")
