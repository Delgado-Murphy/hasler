import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "daemon"))

VARIANTS = ROOT / "config" / "variants"



import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_bounce_guard(monkeypatch):
    """Tests tap twice instantly; the key-bounce guard (actions.AGAIN_GUARD_S)
    would swallow the second tap. Tests that need it turn it on themselves."""
    import haslerd.actions as actions_mod

    monkeypatch.setattr(actions_mod, "AGAIN_GUARD_S", 0.0)
