"""The phone shortcuts API: the few calls a phone's shortcut makes, in one table.

Three things read this table, so they can never disagree:

- ``tools/shortcuts/make_shortcuts.py`` writes the iPhone shortcuts the makers
  host (one *Get Contents of URL* action each: POST JSON to
  ``http://hasler.local/<path>``);
- ``/do/<action>`` (``action_page``): a tiny page for Android's "Add to Home
  screen". GET only sends HTML; the page's own script makes the same-origin
  POST, so a link preview or a prefetch never changes anything;
- the tests, which check every hosted shortcut against ``PATHS``.

Everything here is an everyday action the cabinet's own keys do too. Nothing
that needs a press on the radio (web/confirm.py) belongs in this table.

The paths and bodies are a **stable public API** (docs/architecture.md, "The
phone shortcuts API"): shortcuts on people's phones keep calling them for
years, so change them only by adding.
"""

from __future__ import annotations

import html
import json
from typing import Any

STEP = 5            # one shortcut tap: louder / quieter on the knob's 0-100 scale
KEYS = 6            # Hasler key 1 ... key 6

# iOS Shortcuts sends a JSON body (and its Content-Type) only when the body has
# a field; the calls that need none carry this one, which the radio ignores.
VIA = {"via": "shortcut"}


def _actions() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        "louder": {"name": "Hasler louder", "label": "Louder", "path": "/api/volume",
                   "body": {"delta": STEP}, "done": "Louder", "glyph": "+"},
        "quieter": {"name": "Hasler quieter", "label": "Quieter", "path": "/api/volume",
                    "body": {"delta": -STEP}, "done": "Quieter", "glyph": "−"},
    }
    for n in range(1, KEYS + 1):
        out[f"key/{n}"] = {"name": f"Hasler key {n}", "label": f"Key {n}", "path": "/api/press",
                           "body": {"key": f"k{n}", "event": "tap"}, "done": f"Key {n}", "glyph": str(n)}
    out["standby"] = {"name": "Hasler standby", "label": "Standby", "path": "/api/standby",
                      "body": dict(VIA), "done": "Done", "glyph": "⏻"}
    out["playpause"] = {"name": "Hasler play/pause", "label": "Play/pause", "path": "/api/playpause",
                        "body": dict(VIA), "done": "Done", "glyph": "⏯"}
    out["next"] = {"name": "Hasler next song", "label": "Next song", "path": "/api/next",
                   "body": dict(VIA), "done": "Next", "glyph": "⏭"}
    return out


ACTIONS = _actions()
# The endpoints a shortcut may call: everyday actions, none confirm-gated.
PATHS = frozenset({"/api/volume", "/api/press", "/api/standby", "/api/playpause", "/api/next"})
assert {a["path"] for a in ACTIONS.values()} <= PATHS


def slug(action_id: str) -> str:
    """``key/3`` -> ``key-3``: file and icon names."""
    return action_id.replace("/", "-")


def listing(host: str = "hasler.local") -> list[dict]:
    """For the page's card: each action, its link on this radio and its call."""
    return [{"id": aid, "name": a["name"], "label": a["label"], "path": a["path"], "body": a["body"],
             "file": slug(aid) + ".shortcut", "link": f"http://{host}/do/{aid}"}
            for aid, a in ACTIONS.items()]


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#17100B">
<meta name="robots" content="noindex">
<meta name="apple-mobile-web-app-title" content="{short}">
<meta name="application-name" content="{short}">
<link rel="icon" type="image/png" sizes="192x192" href="/icons/do-{slug}.png">
<link rel="apple-touch-icon" href="/icons/do-{slug}.png">
<title>{name}</title>
<style>
html,body{{height:100%;margin:0;background:#17100B;color:#EDE3CC;font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
main{{min-height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:18px;padding:24px;box-sizing:border-box;text-align:center}}
.word{{font:600 34px/1.1 Georgia,"Times New Roman",serif;color:#FFE9C2;min-height:1.2em}}
.word.bad{{color:#EC5E42;font-size:22px}}
p{{margin:0;color:#A08A68;font-size:14px;max-width:22em}}
button,a.btn{{min-height:48px;min-width:140px;padding:0 22px;border-radius:999px;border:1px solid #5A4128;background:#24170F;color:#C9A15A;font:inherit;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}}
button:focus-visible,a.btn:focus-visible{{outline:2px solid #FFB238;outline-offset:2px}}
.row{{display:flex;gap:10px;flex-wrap:wrap;justify-content:center}}
[hidden]{{display:none!important}}
.mark{{position:fixed;bottom:14px;left:0;right:0;text-align:center;font:600 10px/1 system-ui,sans-serif;letter-spacing:.28em;color:#7D766B}}
.mark i{{font:italic 400 10px/1 Georgia,serif;letter-spacing:0;color:#5A544B}}
</style>
</head>
<body>
<main>
  <div class="word" id="word" role="status" aria-live="polite">{label}</div>
  <p id="note">One moment&hellip;</p>
  <div class="row">
    <button type="button" id="again" hidden>{label} again</button>
    <a class="btn" href="/" id="open">Open the radio</a>
  </div>
</main>
<div class="mark" aria-hidden="true">DELGADO <i>&amp;</i> MURPHY</div>
<script type="application/json" id="call">{call}</script>
<script>
'use strict';
// GET only sent this page. The POST below is the action, made once, when the page is
// really in front of someone: not while prerendered, hidden, restored or reloaded.
(function(){{
  var call = JSON.parse(document.getElementById('call').textContent);
  var fromCard = false;
  try {{ fromCard = !!document.referrer && new URL(document.referrer).host === location.host; }} catch (_) {{}}
  var word = document.getElementById('word'), note = document.getElementById('note'), again = document.getElementById('again');
  function show(w, n, bad){{ word.textContent = w; word.className = bad ? 'word bad' : 'word'; note.textContent = n; }}
  function leave(){{
    // A home-screen icon opened this tab: close it if the browser lets us, else go back.
    setTimeout(function(){{
      try {{ window.close(); }} catch (_) {{}}
      setTimeout(function(){{ if (history.length > 1) history.back(); }}, 150);
    }}, 900);
  }}
  function act(auto){{
    again.hidden = true;
    var x = new XMLHttpRequest();
    x.open('POST', call.path);
    x.setRequestHeader('Content-Type', 'application/json');
    x.timeout = 6000;
    x.onload = function(){{
      if (x.status >= 200 && x.status < 300){{
        try {{ history.replaceState(null, '', location.pathname + '#done'); }} catch (_) {{}}
        again.hidden = false;
        // From the radio's own card: stay, so this page can go on the home screen.
        // From a home-screen icon (no referrer): done, so close or go back.
        if (fromCard) show(call.done, /Android/.test(navigator.userAgent) ? 'For one tap next time: \u22ee \u2192 Add to Home screen.' : '', false);
        else {{ show(call.done, '', false); if (auto) leave(); }}
      }} else {{
        var why = ''; try {{ why = JSON.parse(x.responseText).error || ''; }} catch (_) {{}}
        show('Didn\\u2019t work', why || 'The radio said no.', true); again.hidden = false;
      }}
    }};
    x.onerror = x.ontimeout = function(){{
      show('Can\\u2019t reach the radio', 'This works on the home Wi-Fi.', true); again.hidden = false;
    }};
    x.send(JSON.stringify(call.body));
  }}
  again.addEventListener('click', function(){{ act(false); }});
  var nav = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || {{}};
  if (location.hash === '#done' || nav.type === 'reload' || nav.type === 'back_forward'){{
    show(call.label, 'Tap to do it again.', false); again.hidden = false; return;
  }}
  var started = false;
  function go(){{
    if (started || document.prerendering || document.visibilityState !== 'visible') return;
    started = true; act(true);
  }}
  document.addEventListener('prerenderingchange', go);
  document.addEventListener('visibilitychange', go);
  window.addEventListener('pageshow', function(e){{ if (e.persisted && !started) {{ show(call.label, 'Tap to do it again.', false); again.hidden = false; started = true; }} }});
  go();
}})();
</script>
</body>
</html>
"""


def action_page(action_id: str) -> bytes | None:
    """``GET /do/<action>``: the page that does it with a same-origin POST, or None."""
    a = ACTIONS.get(action_id)
    if a is None:
        return None
    call = json.dumps({"path": a["path"], "body": a["body"], "done": a["done"], "label": a["label"]})
    call = call.replace("<", "\\u003c")
    return _PAGE.format(name=html.escape(a["name"]), short=html.escape(a["label"]), label=html.escape(a["label"]),
                        slug=slug(action_id), call=call).encode()
