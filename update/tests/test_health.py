"""The updater's health judge, and haslerd's /api/health report."""

from __future__ import annotations

from types import SimpleNamespace

from hasler_update.health import HealthChecker

GOOD_API = {"version": "1.1.0", "ok": True, "checks": {"mpd": True, "audio_card": True, "panel": "ok",
                                                       "lamp": True, "web": True}}


def sample(**kw):
    s = {"haslerd": True, "restarts": 0, "api": dict(GOOD_API), "mpd": True, "cards": ["sndrpihifiberry"],
         "librespot": True, "moode_ui": True}
    s.update(kw)
    return s


def test_only_regressions_count():
    base = sample(api={**GOOD_API, "version": "1.0.0", "checks": {**GOOD_API["checks"], "panel": "absent"}},
                  librespot=False, moode_ui=False)
    new = sample(api={**GOOD_API, "checks": {**GOOD_API["checks"], "panel": "absent"}}, librespot=False, moode_ui=False)
    assert HealthChecker.problems(new, base, "1.1.0", 0) == []
    lost = sample(mpd=False, cards=[], api={**GOOD_API, "checks": {**GOOD_API["checks"], "lamp": False}})
    probs = HealthChecker.problems(lost, sample(), "1.1.0", 0)
    assert any("mpd" in p for p in probs) and any("sound card" in p for p in probs) and any("lamp" in p for p in probs)


def test_wrong_version_restarts_and_no_api_fail():
    assert "reports 1.0.0" in " ".join(HealthChecker.problems(sample(api={**GOOD_API, "version": "1.0.0"}), sample(), "1.1.0", 0))
    assert "restarted" in " ".join(HealthChecker.problems(sample(restarts=1), sample(), "1.1.0", 0))
    assert "not answering" in " ".join(HealthChecker.problems(sample(api=None), sample(), "1.1.0", 0))
    # no baseline api (the pre-OTA daemon): every check must be good
    assert HealthChecker.problems(sample(), sample(api=None), "1.1.0", 0) == []


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def checker(samples):
    clock = Clock()
    hc = HealthChecker(clock=clock, sleep=clock.sleep)
    it = iter(samples)
    hc.probe = lambda: next(it)
    return hc


def test_wait_pass_hold_and_fail():
    ok = [sample(haslerd=False)] * 3 + [sample()] * 20
    assert checker(ok).wait("1.1.0", sample())[0] is True
    flap = [sample()] * 5 + [sample(restarts=1)] + [sample()] * 30
    good, why = checker(flap).wait("1.1.0", sample())
    assert not good and "during hold" in why
    never = [sample(haslerd=False)] * 40
    good, why = checker(never).wait("1.1.0", sample())
    assert not good and "within 90s" in why


# --- daemon side ---------------------------------------------------------------------


def app(standby=True, playing=None, absent=False, phone=False):
    player = SimpleNamespace(current=lambda: playing, sources={})
    actions = SimpleNamespace(standby=standby, player=player, phone_has_it=lambda *a: phone, state=None)
    panel = SimpleNamespace(absent=absent, variant=SimpleNamespace(io={}))
    return SimpleNamespace(actions=actions, panel=panel)


def test_health_report_shape_and_idle():
    from haslerd.health import health_report

    a = app()
    doc = health_report(a, now=10_000.0)
    assert set(doc) == {"version", "ok", "uptime_s", "checks", "idle", "standby", "playing", "last_play_s",
                        "recovering", "stalled_s", "stream_events_recent"}
    assert set(doc["checks"]) == {"mpd", "audio_card", "panel", "lamp", "web"}
    assert doc["standby"] is True and doc["playing"] is False
    # it saw activity now -> not idle for the next 10 minutes
    a.actions.standby, a.actions.player.current = False, (lambda: "preset")
    assert health_report(a, now=10_000.0)["playing"] is True
    a.actions.standby, a.actions.player.current = True, (lambda: None)
    assert health_report(a, now=10_300.0)["idle"] is False
    assert health_report(a, now=10_700.0)["idle"] is True
    # a phone casting is never idle
    a.actions.phone_has_it = lambda *x: True
    assert health_report(a, now=20_000.0)["idle"] is False


def test_health_report_panel_and_defensive():
    from haslerd.health import health_report

    assert health_report(app(absent=True))["checks"]["panel"] == "absent"
    assert health_report(app(absent=True))["ok"] is False
    doc = health_report(SimpleNamespace())          # nothing at all: no exception
    assert doc["idle"] is False and doc["checks"]["mpd"] is False
