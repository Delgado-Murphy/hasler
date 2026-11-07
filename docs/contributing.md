# Contributing

This page is for people who want to change the code: fix a bug, add a feature,
or describe a new cabinet. If you only want to use a Hasler, see
[Using your Hasler](using.md). To build one, see the [build guide](build-guide.md).
For the big picture first, read [How the radio works](architecture.md).

## Where things are

The repository, top level:

| Path | What is there |
|---|---|
| `daemon/` | `haslerd`, the service that runs on the radio (Python 3.13), and its tests. |
| `update/` | `hasler-update`, the over-the-air updater on the radio (stdlib Python), and its tests ([ota.md](ota.md)). |
| `config/` | What a release ships besides code: one YAML per cabinet (`variants/`), starter sets (`presets/`), the station collection (`stations/`), systemd units, moOde, go-librespot, Snapcast, CamillaDSP and captive-portal files, and the examples `box.yaml.example` and `firstboot/hasler.txt`. |
| `scripts/` | Root helpers the service calls through sudoers (`hasler-*`), box setup (`bootstrap.sh`, `install.sh`, `hasler-install`), `panel-probe.py`. |
| `scripts/release/` | Building and signing releases ([release.md](release.md)). |
| `scripts/ci/`, `scripts/stations/` | The weekly station check and the dependency bump; building the station collection. |
| `tools/checkin/` | The Cloudflare Worker behind the daily check-in and **Send to the makers** ([README](../tools/checkin/README.md), [SUPPORT.md](../tools/checkin/SUPPORT.md)). |
| `hardware/hat/` | The Hasler HAT in KiCad, the scripts that generate it (`gen/`), fabrication files, [ordering and bring-up](../hardware/hat/ORDERING.md). |
| `docs/` | Owner guide (`using.md`, `lamp.md`, `remote-help.md`), maker and developer docs, HAT brief and design (`hat/`), the prototype board (`prototype/`). |
| `release-tools/hasler-releases/` | A copy of the workflows and tools of the public `hasler-releases` repo, which signs and publishes releases. |
| `site/` | The project's GitHub Pages site. |
| `.github/workflows/` | CI (`ci.yml`), release builds (`release.yml`, `auto-release.yml`), the weekly station check and dependency bump. |
| `licenses/`, `THIRD_PARTY_NOTICES.md` | Licences of what a release bundles. |

Inside the service, `daemon/haslerd/`:

| Path | What is there |
|---|---|
| `main.py` | Startup: reads the config, builds everything, runs the main loop. |
| `actions.py` | What an event does (`k3.tap` → play key 3). |
| `panel/` | Input backends (GPIO, MCP23017, ADS1115) and the control types. |
| `player/` | Playing radio, Spotify (go-librespot) and YouTube Music; moOde's casting targets (`renderers.py`). YouTube Music: `sources/ytm.py` (playing), `ytmusic.py` (catalogue, sign-in), `ytm_browse.py` (browsing, caches, preset lookups), `ytm_refs.py` (what a key can hold). |
| `multiroom/` | Several Haslers in step: finding peers, the group, the Snapcast audio path ([multiroom.md](multiroom.md)). |
| `lamp/` | The pilot lamp: what to show ([lamp.md](lamp.md)) and how to fade. |
| `dsp/` | The knobs' sound effects ([sound.md](sound.md)); `scripts/hasler-dsp` applies them. |
| `ha/` | Home Assistant: finding it, its REST API, what a key can ask it to do. |
| `web/` | The key page (`static/index.html`, one file, no build step), the Wi-Fi setup page, and their API (one `*_api.py` per area). |
| `migrations/` | One-time changes to a radio's settings when a release needs them. |

## Run it on a laptop

You do not need a Raspberry Pi. Set up once, with [uv](https://docs.astral.sh/uv/):

```sh
cd daemon
uv venv --python 3.13 && uv pip install -e ".[dev]"
```

Then run a fake radio: an in-memory panel and player, the real key page.

```sh
mkdir -p /tmp/hasler/etc /tmp/hasler/state
cat > /tmp/hasler/etc/box.yaml <<'EOF'
schema: 1
variant: hasler-hm
web: {host: 127.0.0.1, port: 8080, allow_unconfirmed: true}
EOF
.venv/bin/python -m haslerd.main --fake --etc /tmp/hasler/etc --state /tmp/hasler/state
```

Open <http://127.0.0.1:8080>. `allow_unconfirmed` stands in for the key press
on the radio that some changes ask for. `variant: excelsior` gives the other
cabinet. Nothing plays, but every page, sheet and API call works.

To watch the panel itself without hardware:

```sh
.venv/bin/python ../scripts/panel-probe.py --fake   # keys 0-9 toggle inputs
```

On a real radio, the same tool shows every key and knob live. Use it while
wiring: see the [build guide, Phase 2](build-guide.md#phase-2--wire-the-panel-and-prove-it).

## Run the tests

| Suite | Command | Needs |
|---|---|---|
| The service | `cd daemon && .venv/bin/python -m pytest -q -n auto` | the venv above |
| The updater | `cd update && ../daemon/.venv/bin/python -m pytest -q -n auto tests` | the same venv; `zstd` and `ssh-keygen` |
| The key page in a browser | `cd daemon && node tests/e2e/<name>.cjs` (one file per area: `settings_ia`, `starter_sets`, `wrong`, ...) | Node 18+, Playwright (`npm i playwright`, or `PLAYWRIGHT=/path/to/node_modules/playwright`), its Chromium or Chrome (`PW_CHANNEL=chrome`) |
| The check-in Worker | `cd tools/checkin && node --test` | Node 18+; no Cloudflare, no network |
| Lint | `ruff check --isolated --select E9,F63,F7,F82 daemon scripts update` | `ruff` (version in `.github/ci-requirements.txt`) |

CI runs lint, the two pytest suites and a test build of a release on every
push and pull request. The browser tests start their own fake radios
(`haslerd --fake`) on free ports; run the ones near your change before you
open a pull request, and all of them if you touched `index.html`.

Tests run in parallel, so a new test must not use a fixed port or a fixed path.
Use `tmp_path` and port 0. The radios use Python 3.13 (Debian trixie). On the
Pi itself, add the hardware extras: `uv pip install -e ".[pi]"`.

**Why testing is easy here:** the panel is a pure function of (time, input
snapshot) → events. Time is passed in, not read from the clock. So a 5-second
hold costs nothing to test, and a cabinet can be developed before it is wired.

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
says only how many keys hold music (`presets: {slots: 6}`); the built-in
stations are the ten starter sets in `config/presets/` (by collection and
catalogue id, `haslerd/presets.py`). A radio's own knob calibration lives
outside the release, in `/etc/hasler/variant.d/`
(see [ota.md, section 1](ota.md#1-what-lives-where-config-separate-from-updates)).

## Making a release

See [release.md](release.md). In short: bump the version in
`daemon/pyproject.toml` and `daemon/haslerd/__init__.py`, add a section to
`CHANGELOG.md`, tag it, publish it to **Bench versions** (the makers' radios; it may
break there), live with it, then promote it to **Stable**. Signing and
publishing are the maintainers' job.

## Style

- **Match the code around you.** Same naming, same layout, same size of
  function. No new dependency without a good reason: the radio has to build
  its venv offline.
- **Comments say why, not what.** Most modules open with a docstring that
  explains the design and links its doc; keep that up, and keep inline
  comments as sparse as the file you are in.
- Keep the radio working without the internet, without Home Assistant and
  without a phone. Every outside service is optional.
- A key press must never wait for a slow network call.
- Write user-facing text (key page, docs) in plain, short English. Some of our
  readers speak Spanish first. The owner's docs stay free of maker detail.
- Change the code and its doc together. [lamp.md](lamp.md), [sound.md](sound.md)
  and [multiroom.md](multiroom.md) are the designs the code follows; the owner's
  side is in [using.md](using.md). Add a line to `CHANGELOG.md` for anything an
  owner or a maker would notice.
- The update channels are **Bench versions** (new versions first, on the makers'
  radios; may break) and **Stable** (everyone). Use those words.

## Commits and pull requests

- One change per pull request, with tests. Say what an owner would notice,
  if anything, and how you tested it (on a laptop, or on a radio).
- Commit messages: a short first line in plain words, then why.
- Work done with an AI assistant says so in a trailer, for example
  `Co-Authored-By: Claude <noreply@anthropic.com>`. Don't remove trailers
  that are already there.
- By contributing you agree that your code is MIT licensed, like the rest
  (hardware changes in `hardware/hat/`: CERN-OHL-P-2.0). See
  [the licence section of the README](../README.md#licence).

## Where to get help

- **Makers and developers:** open an issue in this repository (bug report or
  idea). Search first; say which version and which cabinet.
- **Owners** don't need GitHub: on the key page, **Settings → Something's
  wrong? → Write to the makers** ([Using your Hasler](using.md#help-from-the-makers)).
- **A security problem:** not in an issue. See [SECURITY.md](../SECURITY.md).

## Why not a Volumio plugin?

That was the first attempt, and an ESPHome device was the second. Both failed
the same way: the code lived inside someone else's sandbox, half-working, with
no way to see why. moOde is plain Raspberry Pi OS with MPD and a documented
API, so `haslerd` is an ordinary systemd service that we own from end to end.
And `panel-probe` means the hardware is never a black box.

---

<sub>Delgado &amp; Murphy</sub>
