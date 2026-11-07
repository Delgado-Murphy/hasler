#!/usr/bin/env python3
"""Write the iPhone shortcuts the makers host, unsigned.

    python3 tools/shortcuts/make_shortcuts.py [OUT_DIR]

One file per action in daemon/haslerd/web/phone_api.py ("Hasler louder",
"Hasler key 3", ...), each a single *Get Contents of URL* action: POST, a
JSON body, to http://hasler.local<path>. iOS only opens signed files;
tools/shortcuts/build.sh signs these (macOS `shortcuts sign`) into the org
site's /shortcuts/. The unsigned files are committed here
(tools/shortcuts/unsigned/) so the tests can check every hosted shortcut
calls only the phone shortcuts API.
"""

from __future__ import annotations

import plistlib
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "daemon"))
from haslerd.web.phone_api import ACTIONS, slug  # noqa: E402

HOST = "hasler.local"
DEFAULT_OUT = Path(__file__).resolve().parent / "unsigned"
# Shortcuts' icon: the radio's amber, a speaker glyph (any glyph number works).
ICON = {"WFWorkflowIconStartColor": 4282601983, "WFWorkflowIconGlyphNumber": 59511}


def _id(name: str, part: str) -> str:
    # Stable UUIDs: rebuilding gives the same bytes, so git only shows real changes.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://delgado-murphy.github.io/shortcuts/{name}#{part}")).upper()


def _text(s: str) -> dict:
    return {"Value": {"string": s, "attachmentsByRange": {}}, "WFSerializationType": "WFTextTokenString"}


def _field(name: str, key: str, value) -> dict:
    # WFItemType: 0 text, 3 number, 4 boolean.
    if isinstance(value, bool):
        item, val = 4, {"Value": value, "WFSerializationType": "WFNumberSubstitutableState"}
    elif isinstance(value, (int, float)):
        item, val = 3, _text(str(value))
    else:
        item, val = 0, _text(str(value))
    return {"UUID": _id(name, "field-" + key), "WFItemType": item, "WFKey": _text(key), "WFValue": val}


def workflow(action_id: str, host: str = HOST) -> dict:
    a = ACTIONS[action_id]
    name = a["name"]
    return {
        "WFWorkflowClientVersion": "2607.0.4",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": ICON,
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowHasOutputFallback": False,
        "WFQuickActionSurfaces": [],
        "WFWorkflowActions": [{
            "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
            "WFWorkflowActionParameters": {
                "UUID": _id(name, "action"),
                "WFURL": f"http://{host}{a['path']}",
                "WFHTTPMethod": "POST",
                "WFHTTPBodyType": "JSON",
                "ShowHeaders": False,
                "WFJSONValues": {
                    "Value": {"WFDictionaryFieldValueItems": [_field(name, k, v) for k, v in a["body"].items()]},
                    "WFSerializationType": "WFDictionaryFieldValue",
                },
            },
        }],
    }


def file_name(action_id: str) -> str:
    # "Hasler louder.shortcut": iOS names the imported shortcut after the file.
    return ACTIONS[action_id]["name"].replace("/", "-") + ".shortcut"


def main(argv: list[str]) -> None:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    for aid in ACTIONS:
        path = out / file_name(aid)
        path.write_bytes(plistlib.dumps(workflow(aid), fmt=plistlib.FMT_BINARY, sort_keys=True))
        print(path)


if __name__ == "__main__":
    main(sys.argv)
