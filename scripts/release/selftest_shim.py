#!/usr/bin/env python3
"""Release pre-flight entry point (docs/ota-format.md): `python selftest.py --etc /etc/hasler`.

Shipped as selftest.py at the top of every release tarball by
scripts/release/build_release.py. Runs haslerd.selftest from this release's
app/ with the interpreter it was started with (the release venv). Exit 0 = ok.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))
try:
    from haslerd import selftest
except ImportError as e:
    print(f"selftest: cannot import haslerd.selftest: {e}", file=sys.stderr)
    sys.exit(2)
sys.exit(selftest.main(sys.argv[1:]))
