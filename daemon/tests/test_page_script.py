"""The key page's inline script parses (web/static/index.html).

A merge that leaves two ``let`` declarations of one name, or an unbalanced
brace, is a hard JavaScript error: the page loads blank and only the browser
tests notice, minutes later. node's ``--check`` catches it in a second, so the
unit suite does too wherever node is installed (skipped where it isn't).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "haslerd" / "web" / "static" / "index.html"
_SCRIPT = re.compile(r'<script(?![^>]*type="application/json")[^>]*>(.*?)</script>', re.S)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_inline_script_parses(tmp_path):
    blocks = _SCRIPT.findall(PAGE.read_text())
    assert blocks, "no inline script in index.html"
    for n, body in enumerate(blocks):
        src = tmp_path / f"inline_{n}.js"
        src.write_text(body)
        run = subprocess.run(["node", "--check", str(src)], capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, run.stderr.strip().splitlines()[-1:]


def test_no_name_is_declared_twice_at_top_level():
    # `let`/`const` at column 0: a duplicate is the exact mistake two parallel streams make.
    names: dict[str, int] = {}
    for body in _SCRIPT.findall(PAGE.read_text()):
        for m in re.finditer(r"^(?:let|const)\s+([A-Za-z_$][\w$]*)\s*[=;]", body, re.M):
            names[m.group(1)] = names.get(m.group(1), 0) + 1
    assert [k for k, v in names.items() if v > 1] == []
