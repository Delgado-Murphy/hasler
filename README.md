# Hasler Music Box

<sub><i>A Delgado &amp; Murphy restoration</i></sub>

A gutted **Hasler HM** (Bern, ~1964) — a Telefónica *Hilo Musical* wired-music receiver whose
piano keys once selected preset music channels over the telephone line — rebuilt around a
Raspberry Pi. Tap a key, get a radio station, a Spotify playlist or a YouTube Music playlist — no
phone, no cloud app, no Home Assistant needed. Plus Bluetooth, AirPlay, Spotify Connect and DLNA,
and Home Assistant if you want it.

```
keys ──▶ digital backend ─┐            ┌──▶ MPD (moOde) ◀── radio: stream URL
         (GPIO │ MCP23017)│            │       ▲
wheels ─▶ analog backend ─┼──▶ haslerd ┤       └── ytm: :8099/ytm/<id> ──302──▶ yt-dlp-resolved stream
         (none │ ADS1115) │       │    │
RGB lamp ─▶ Power Controller      │    └──▶ go-librespot :3678 ◀── spotify: playlist URI
                                  │
                                  └──▶ Home Assistant (optional - key actions, now-playing sensor)

MPD │ go-librespot │ BT │ AirPlay ──▶ Amp4 Pro (hardware mixer = the knob) ──▶ original speaker
```

**Not a one-cabinet project.** An *Excelsior* and others are waiting, and their panels behave
completely differently — momentary keys here, a mechanically interlocked bank there. So the panel
is **data, not code**: one daemon, five control primitives, a YAML descriptor per cabinet.

## Presets

One source per key, in the cabinet's descriptor:

```yaml
presets:
  - {n: 1, name: "Jazz",    radio: "http://example.com/jazz.mp3"}
  - {n: 2, name: "Morning", spotify: "spotify:playlist:<id>", shuffle: true}
  - {n: 3, name: "Kitchen", ytm: "https://music.youtube.com/playlist?list=<id>"}
  - {n: 4, name: "Legacy",  url: "http://example.com/stream"}   # plain url: still means radio
```

Tap a key to play it. Tap a playing playlist key again to skip. The last preset survives a
power-cycle. **Spotify needs Premium** — go-librespot is a Connect device, logged in once over SSH;
no Web API, no developer app. YouTube Music needs nothing for public playlists and a `cookies.txt`
for private ones. Setup: [build guide, Phase 3c](docs/build-guide.md#phase-3c--streaming-sources-spotify--youtube-music).

## Layout

| | |
|---|---|
| `daemon/haslerd/` | the service. `panel/` holds the IO backends and control primitives |
| `config/variants/` | one descriptor per cabinet — `hasler-hm.yaml`, `excelsior.yaml` |
| `config/` | `haslerd.yaml.example`, systemd units, go-librespot config template |
| `scripts/install.sh` | idempotent installer: haslerd, go-librespot, yt-dlp + deno. `--dry-run` first |
| `scripts/panel-probe.py` | live view of the panel. Wire against this |
| `docs/hardware.md` | bill of materials, GPIO map, and the two steps that can destroy something |

## Wiring the panel

Cables into any eight GPIOs, then let the Pi work out which is which:

```sh
scripts/panel-probe.py --identify        # actuate each key, get the pins: array
scripts/panel-probe.py                   # live view - check for bounce and ghosting
```

`--identify` prints a corrected `io.digital.pins` array to paste into the descriptor. Control
assignments (`k1: d0`, …) never move; only that one array is reordered.

Watch for: each key lighting its own row and nobody else's, and the TRANSITIONS column staying in
low single digits per press. Tens means dirty contacts or a missing 100 nF — fix it in hardware
before it becomes the daemon's problem.

No hardware to hand? `--fake` runs the whole thing on a laptop with 0–9 toggling inputs.

## Develop

```sh
cd daemon && uv venv --python 3.11 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The panel is a pure function of (time, input snapshot) → events, with time passed in rather than
read from the clock — so a 5-second hold costs nothing to test and the Excelsior can be developed
before it is wired. On the Pi, add the hardware extras: `uv pip install -e ".[pi]"`.

## Why not a Volumio plugin

That was attempt one, and an ESPHome device was attempt two. Both failed the same way — code living
inside someone else's sandbox, half-working with no way to see why. moOde is plain Raspberry Pi OS
+ MPD with a documented REST API, so this is an ordinary systemd service that we own end to end,
and `panel-probe` means the hardware is never a black box.

---

<sub>Delgado &amp; Murphy · Hasler HM, Bern 1964 — restored 2026</sub>
