# Contributing

This page is for people who want to change the code: fix a bug, add a feature,
or describe a new cabinet. If you only want to use a Hasler, see
[Using your Hasler](using.md). To build one, see the [build guide](build-guide.md).

## Where things are

| Path | What is there |
|---|---|
| `daemon/haslerd/` | The service that runs on the radio. |
| `daemon/haslerd/panel/` | Input backends (GPIO, MCP23017, ADS1115) and the control types. |
| `daemon/haslerd/player/` | Playing radio, Spotify (go-librespot) and YouTube Music; moOde's casting targets (`renderers.py`). |
| `daemon/haslerd/multiroom/` | Several Haslers in step: finding peers, the group, the Snapcast audio path ([multiroom.md](multiroom.md)). |
| `daemon/haslerd/lamp/` | The pilot lamp: what to show ([lamp.md](lamp.md)) and how to fade. |
| `daemon/haslerd/dsp/` | The knobs' sound effects ([sound.md](sound.md)); `scripts/hasler-dsp` applies them. |
| `daemon/haslerd/ha/` | Home Assistant: finding it, its REST API, what a key can ask it to do. |
| `daemon/haslerd/web/` | The key page (`static/index.html`) and the Wi-Fi setup page, and their API. |
| `daemon/tests/` | Tests for the service. |
| `update/` | The over-the-air updater that runs on the radio ([ota.md](ota.md)). |
| `config/variants/` | One file per cabinet: `hasler-hm.yaml`, `excelsior.yaml`. |
| `config/` | Example settings (`box.yaml.example`), systemd units, go-librespot and moOde files. |
| `scripts/` | Box setup (`bootstrap.sh`, `ota-bootstrap.sh`), helpers, and `panel-probe.py`. |
| `scripts/release/` | Building and signing releases ([release.md](release.md)). |

## Run the tests

You do not need a Raspberry Pi. The tests run on a laptop.

```sh
cd daemon
uv venv --python 3.13 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q -n auto          # the service

cd ../update
../daemon/.venv/bin/python -m pytest -q -n auto tests   # the updater
```

CI runs the same two test suites, a quick lint (`ruff`), and a test build of a
release, on every push. The radios use Python 3.13 (Debian trixie). On the Pi
itself, add the hardware extras: `uv pip install -e ".[pi]"`.

Tests run in parallel, so a new test must not use a fixed port or a fixed path.
Use `tmp_path` and port 0.

**Why testing is easy here:** the panel is a pure function of (time, input
snapshot) → events. Time is passed in, not read from the clock. So a 5-second
hold costs nothing to test, and a cabinet can be developed before it is wired.

## Try the panel without hardware

```sh
daemon/.venv/bin/python scripts/panel-probe.py --fake   # keys 0-9 toggle inputs on a laptop
```

On a real radio, the same tool shows every key and knob live. Use it while
wiring: see the [build guide, Phase 2](build-guide.md#phase-2--wire-the-panel-and-prove-it).

## Describing a cabinet

Each cabinet has one YAML file in `config/variants/`. It says:

- **io:** which chips read the keys and knobs, and which pins drive the lamp;
- **controls:** each key or knob, its type (`momentary`, `latching_toggle`,
  `pot`, ...) and its pin; a pot can also name its default sound effect
  (`effect: tilt`, see [sound.md](sound.md#a-new-cabinets-pots));
- **actions:** what each control does (`k1.tap: {do: preset, n: 1}`);
- **presets:** the music on each key, one source per key:

```yaml
presets:
  - {n: 1, name: "Jazz",    radio: "http://example.com/jazz.mp3"}
  - {n: 2, name: "Morning", spotify: "spotify:playlist:<id>", shuffle: true}
  - {n: 3, name: "Kitchen", ytm: "https://music.youtube.com/playlist?list=<id>"}
  - {n: 4, name: "Legacy",  url: "http://example.com/stream"}   # plain url: means radio
```

Nothing in the code knows a GPIO number. Actions name controls, controls name
logical pins, and only the `io` block knows the hardware. A new cabinet should
need a new YAML file, not new code.

Owners normally change presets on the key page, not in this file. The file
holds the factory defaults that ship with each release. A radio's own knob
calibration lives outside the release, in `/etc/hasler/variant.d/`
(see [ota.md, section 1](ota.md#1-what-lives-where-config-separate-from-updates)).

## Making a release

See [release.md](release.md). In short: bump the version in
`daemon/pyproject.toml` and `daemon/haslerd/__init__.py`, add a section to
`CHANGELOG.md`, tag it, publish it to the bench radio, live with it, then
promote it to stable.

## Style

- Keep the radio working without the internet, without Home Assistant and
  without a phone. Every outside service is optional.
- A key press must never wait for a slow network call.
- Write user-facing text (key page, docs) in plain, short English. Some of our
  readers speak Spanish first.
- Change the code and its doc together. [lamp.md](lamp.md), [sound.md](sound.md)
  and [multiroom.md](multiroom.md) are the designs the code follows; the owner's
  side is in [using.md](using.md).

## Why not a Volumio plugin?

That was the first attempt, and an ESPHome device was the second. Both failed
the same way: the code lived inside someone else's sandbox, half-working, with
no way to see why. moOde is plain Raspberry Pi OS with MPD and a documented
API, so `haslerd` is an ordinary systemd service that we own from end to end.
And `panel-probe` means the hardware is never a black box.

---

<sub>Delgado &amp; Murphy</sub>
