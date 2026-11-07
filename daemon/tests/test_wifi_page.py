"""The Wi-Fi setup page: rendered on the server, a form that works without JavaScript."""

from __future__ import annotations

import re
from urllib.parse import urlencode

import pytest

from haslerd.web import wifi_page
from haslerd.web.server import STATIC, WebServer
from test_web_confirm import call, rig  # noqa: F401  (fixture)

PAGE = (STATIC / "wifi.html").read_text("utf-8")
NETS = [{"ssid": "Casa", "signal": 80, "security": "wpa-psk"},
        {"ssid": "<b>x</b> {{save}}", "signal": 30, "security": "sae"},
        {"ssid": "Bar", "signal": 10, "security": "none"}]


@pytest.fixture
def hotspot(rig):  # noqa: F811
    app = rig[0]
    joined = []
    app.wifi.status = lambda: {"hotspot": True, "current": "Vieja", "networks": NETS}
    app.wifi.join = lambda ssid, pwd, sec: joined.append((ssid, pwd, sec))
    app.hotspot_addrs = {"127.0.0.1"}     # this test's socket stands in for 172.24.1.1
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield f"127.0.0.1:{server.port}", app, joined
    server.close()


def post_form(hp, fields, **headers):
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://{hp}"}
    hdrs.update(headers)
    status, doc = call(hp, "POST", "/wifi", None, hdrs, raw=urlencode(fields).encode())
    return status, text(doc)


def text(page):
    return page.decode() if isinstance(page, bytes) else page


# -- the page itself ----------------------------------------------------------------------


def test_page_is_small_and_self_contained():
    assert len(PAGE.encode()) < 60_000
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", PAGE.replace('href=\\"http://hasler.local/\\"', ""))
    assert "url(http" not in PAGE and "@import" not in PAGE and "localStorage" not in PAGE


def test_both_languages_have_every_string():
    table = wifi_page.strings(PAGE)
    assert set(table) == {"en", "es"} and table["en"].keys() == table["es"].keys()
    used = set(re.findall(r"\{\{(\w+)\}\}", PAGE)) | set(re.findall(r"""data-t=["'](\w+)["']""", PAGE))
    used |= set(re.findall(r"""\bt\('(\w+)'""", PAGE))
    assert used <= table["en"].keys(), used - table["en"].keys()


@pytest.mark.parametrize("chosen, accept, lang", [
    (None, "es-ES,es;q=0.9,en;q=0.8", "es"), (None, "fr-FR,es;q=0.5,en;q=0.9", "en"),
    (None, "de, es;q=0.3", "es"), (None, "", "en"), ("es", "en-GB", "es"), ("xx", "es", "es")])
def test_pick_lang(chosen, accept, lang):
    assert wifi_page.pick_lang(chosen, accept) == lang


def test_render_escapes_network_names_and_fills_every_slot():
    html = wifi_page.render(PAGE, {"hotspot": True, "current": "Vieja", "networks": NETS}, "en")
    assert not re.search(r"\{\{=?\w+\}\}", html.replace("&lt;/b&gt; {{save}}", ""))
    assert "&lt;b&gt;x&lt;/b&gt; {{save}}" in html and "<b>x</b>" not in html
    assert 'value="wpa-psk:Casa"' in html and 'class="bars s4"' in html and 'class="bars s1"' in html
    assert "couldn’t join <b>Vieja</b>" in html and 'class="back"' not in html    # setup mode: no key page
    es = wifi_page.render(PAGE, {"hotspot": False, "current": "Casa", "networks": NETS}, "es")
    assert '<html lang="es">' in es and "Guardar y reiniciar" in es and 'class="back"' in es


def test_no_networks_picks_other():
    html = wifi_page.render(PAGE, {"hotspot": True, "current": "", "networks": []}, "en")
    assert 'id="netother" checked' in html and "found no networks" in html


# The first page a new owner reads: it describes the lamp as it is (light language v2),
# welcomes them, and says what comes after the Wi-Fi.
def test_the_legend_is_the_real_lamp_and_the_copy_welcomes():
    table = wifi_page.strings(PAGE)
    for lang in ("en", "es"):
        s = table[lang]
        assert "CQ" in s["lamp_cq"] and "CQ" in s["lamp_fail"] and 'class="pic green"' in s["lamp_busy"]
        assert not any("white" in v.lower() or "blanco" in v.lower() for v in s.values()), lang
        assert "Look again" in s["note_hotspot"] or "Busca otra vez" in s["note_hotspot"]
        assert "{n}" not in s["press"] and s["press_wait"]
    en = table["en"]
    assert en["lede_first"].startswith("Welcome.") and "key page" in en["lede_first"]
    assert "power cut" in en["lede_retry"] and "by itself" in en["lede_retry"]
    assert "Make it yours" in en["next_2"]
    assert "set me up" in en["lamp_cq"] and "mistyped password" in en["lamp_fail"]
    # The CQ pictogram is drawn in the page itself, dits and dahs like the printed key card's.
    html = wifi_page.render(PAGE, {"hotspot": True, "current": "", "networks": NETS}, "en")
    assert html.count('<i class="dah"></i>') >= 5 and "warm white" not in html.lower()
    assert "looks again every 20 seconds" in html and 'href="/wifi">Look again</a>' in html


# A mistake is said next to the field it concerns, the other slots stay empty.
@pytest.mark.parametrize("key, slot", [("err_short", "errpw"), ("err_long", "errpw"), ("err_name", "errother"),
                                       ("err_choose", "errnet"), ("err_save", "err"), (None, "err")])
def test_the_error_sits_by_its_field(key, slot):
    html = wifi_page.render(PAGE, {"hotspot": True, "current": "", "networks": NETS}, "en", error="Oops <x>", error_key=key)
    filled = re.findall(r'<p class="err(?: near)?" id="(\w+)" role="alert">Oops &lt;x&gt;</p>', html)
    assert filled == [slot], filled
    assert len(re.findall(r'<p class="err(?: near)?" id="\w+" role="alert"></p>', html)) == 3


@pytest.mark.parametrize("form, body", [
    ({"net": "wpa-psk:Ca:sa", "password": "12345678"}, {"ssid": "Ca:sa", "password": "12345678", "security": "wpa-psk"}),
    ({"net": "none:Bar", "password": "ignored"}, {"ssid": "Bar", "password": "", "security": "none"}),
    ({"net": "other", "other_ssid": " Oculta ", "security": "sae", "password": "abcdefgh"},
     {"ssid": "Oculta", "password": "abcdefgh", "security": "sae"}),
    ({"net": "other", "other_ssid": "Libre", "security": "auto", "password": ""},
     {"ssid": "Libre", "password": "", "security": "auto"}),
    ({"net": "wpa-psk:Casa", "password": "a" * 64}, {"ssid": "Casa", "password": "a" * 64, "security": "wpa-psk"}),
])
def test_join_body(form, body):
    assert wifi_page.join_body(form) == body


@pytest.mark.parametrize("form, key", [
    ({}, "err_choose"), ({"net": "other", "other_ssid": " "}, "err_name"),
    ({"net": "wpa-psk:Casa", "password": "short"}, "err_short"),
    ({"net": "other", "other_ssid": "X", "security": "auto", "password": "short"}, "err_short"),
    ({"net": "sae:Casa", "password": "x" * 64}, "err_long")])
def test_join_body_mistakes(form, key):
    with pytest.raises(wifi_page.FormError) as exc:
        wifi_page.join_body(form)
    assert exc.value.key == key


# -- over HTTP ----------------------------------------------------------------------------


def test_get_renders_in_the_phones_language(hotspot):
    hp, _, _ = hotspot
    status, page = call(hp, "GET", "/wifi", None, {"Accept-Language": "es-ES,es;q=0.9"})
    page = text(page)
    assert status == 200 and '<html lang="es">' in page and "Casa" in page
    status, page = call(hp, "GET", "/wifi?lang=en", None, {"Accept-Language": "es-ES"})
    assert '<html lang="en">' in text(page)


def test_form_post_from_the_hotspot_joins(hotspot):
    hp, _, joined = hotspot
    status, page = post_form(hp, {"net": "wpa-psk:Casa", "password": "12345678", "lang": "es"})
    assert status == 200 and joined == [("Casa", "12345678", "wpa-psk")]
    assert 'data-state="done"' in page and "Guardado" in page and "Casa" in page


def test_the_network_it_is_on_again_says_saved_and_stays(hotspot):
    """Re-typing the password of the Wi-Fi it is on: no restart, and the page says so."""
    hp, app, joined = hotspot
    app.wifi.join = lambda ssid, pwd, sec: joined.append(ssid) or {
        "ssid": ssid, "same_network": True, "changed": False, "restarting": False}
    status, page = post_form(hp, {"net": "wpa-psk:Vieja", "password": "12345678"})
    assert status == 200 and joined == ["Vieja"]
    assert 'data-state="kept"' in page and "the radio stays connected to <b>Vieja</b>" in page
    status, page = post_form(hp, {"net": "wpa-psk:Vieja", "password": "12345678", "lang": "es"})
    assert "sigue conectada" in page


def test_api_wifi_answers_whether_it_restarts(hotspot):
    hp, app, _ = hotspot
    app.wifi.join = lambda ssid, pwd, sec: {"ssid": ssid, "same_network": True, "changed": True, "restarting": False}
    status, doc = call(hp, "POST", "/api/wifi", {"ssid": "Vieja", "password": "12345678", "security": "wpa-psk"},
                       {"Origin": f"http://{hp}"})
    assert status == 200 and doc["ok"] and doc["restarting"] is False and doc["changed"] is True


def test_form_mistake_keeps_the_choice_not_the_password(hotspot):
    hp, _, joined = hotspot
    status, page = post_form(hp, {"net": "wpa-psk:Casa", "password": "short"})
    assert status == 400 and joined == []
    assert "at least 8 characters" in page and 'value="wpa-psk:Casa" data-sec="wpa-psk" checked' in page
    assert 'id="errpw" role="alert">Wi-Fi passwords are at least 8 characters.</p>' in page   # by the password field
    assert "short" not in re.sub(r"<script.*?</script>", "", page, flags=re.S).replace("shortly", "").replace("long-short", "")
    status, page = post_form(hp, {"net": "other", "other_ssid": " "})
    assert status == 400 and 'id="errother" role="alert">Type the network’s name.</p>' in page


def test_form_helper_error_is_shown(hotspot):
    hp, app, _ = hotspot

    def fail(*a):
        raise ValueError("the Wi-Fi helper failed")
    app.wifi.join = fail
    status, page = post_form(hp, {"net": "none:Bar"})
    assert status == 400 and "Couldn’t save: the Wi-Fi helper failed" in page


def test_form_post_from_the_home_network_needs_javascript(hotspot):
    hp, app, joined = hotspot
    app.hotspot_addrs = set()
    status, page = post_form(hp, {"net": "wpa-psk:Casa", "password": "12345678"})
    assert status == 400 and joined == [] and "needs JavaScript" in page


@pytest.mark.parametrize("headers", [{"Origin": "http://evil.example"}, {"Host": "evil.example"},
                                     {"Content-Type": "text/plain"}])
def test_form_post_keeps_the_csrf_rules(hotspot, headers):
    hp, _, joined = hotspot
    status, _ = post_form(hp, {"net": "wpa-psk:Casa", "password": "12345678"}, **headers)
    assert status == 403 and joined == []


def test_api_still_refuses_forms(hotspot):
    hp, _, joined = hotspot
    status, _ = call(hp, "POST", "/api/wifi", None, {"Content-Type": "application/x-www-form-urlencoded"},
                     raw=b"ssid=Casa&password=12345678")
    assert status == 403 and joined == []
