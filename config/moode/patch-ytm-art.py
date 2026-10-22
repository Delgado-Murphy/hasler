#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Delgado & Murphy (Bill Murphy)
# SPDX-License-Identifier: GPL-3.0-or-later
# Modifies moOde audio player (GPL-3.0-or-later); see LICENSE in this directory.
"""Make moOde's player handle haslerd's YouTube Music tracks well.

moOde treats our tracks (http://127.0.0.1:8099/ytm/<id>, tagged with Artist)
as UPnP files. Two consequences, each fixed by one line here:

1. Cover art (inc/mpd.php): moOde asks a UPnP renderer for artwork - there
   isn't one, so it shows its default cover. Our line makes moOde's PHP ask
   haslerd (127.0.0.1:8080/art/<id>, on the Pi) where the cover is and hand
   the browser that public image URL. Not haslerd's URL itself: Chrome blocks
   an http page on :80 loading from :8080 on the same LAN host (Private
   Network Access).
2. Track-change delay (inc/music-library.php, getEncodedAt): for a UPnP file
   moOde runs `mediainfo` on the stream URL to label its format - ~3.8 s on
   the Pi, through our proxy to YouTube, before the new title shows. Our line
   is an extra branch ahead of that one that labels our tracks without
   probing them (they are always AAC from YouTube).

Every other track goes through moOde unchanged. Run as root by
scripts/install.sh; safe to re-run, and upgrades an older version of our own
lines. A moOde update replaces these files - re-run install.sh (nothing
breaks meanwhile). Backups: <file>.hasler-orig.

Exit 0 = all applied; 1 = an anchor not found (moOde changed), that file left
untouched; 2 = php -l rejected a result, that file restored.
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

# (file, anchor line, marker, our line, "replace" the anchor or insert "before" it)
PATCHES = [
    (Path("/var/www/inc/mpd.php"), "$current['coverurl'] = getUpnpCoverUrl();",
     "// hasler-ytm-art", (HERE / "ytm-art.php").read_text(), "replace"),
    (Path("/var/www/inc/music-library.php"),
     "} else if (substr($songData['file'], 0, 4) == 'http' && isset($songData['Artist'])) {",
     "// hasler-ytm-info",
     "\t} else if (strpos($songData['file'], 'http://127.0.0.1:8099/ytm/') === 0) { "
     "$encodedAt = 'YouTube Music AAC'; // hasler-ytm-info\n",
     "before"),
]


def apply(target: Path, anchor: str, marker: str, ours: str, how: str) -> int:
    ours = ours.rstrip("\n") + "\n"
    lines = target.read_text().splitlines(keepends=True)
    mine = [i for i, line in enumerate(lines) if marker in line]
    if mine:
        if lines[mine[0]] == ours:
            print(f"{target.name}: already patched")
            return 0
        lines[mine[0]] = ours                       # older version of our line
    else:
        hits = [i for i, line in enumerate(lines) if line.strip() == anchor]
        if len(hits) != 1:
            print(f"{target.name}: anchor found {len(hits)} times - moOde changed; not patching")
            return 1
        if how == "replace":
            lines[hits[0]] = ours
        else:
            lines.insert(hits[0], ours)
    backup = target.with_name(target.name + ".hasler-orig")
    if not backup.exists():
        shutil.copy2(target, backup)
    original = target.read_text()
    target.write_text("".join(lines))
    if subprocess.run(["php", "-l", str(target)], capture_output=True).returncode != 0:
        target.write_text(original)
        print(f"{target.name}: php -l rejected the patch - restored")
        return 2
    print(f"{target.name}: patched")
    return 0


sys.exit(max(apply(*p) for p in PATCHES))
