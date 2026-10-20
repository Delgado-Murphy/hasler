# The knobs' sound effects (for makers)

Every pot except the volume knob can carry a sound effect, done in CamillaDSP.
The owner picks them on the key page (**Settings → Sound → What the tone knob does**); this page is how
it works and how to give a new cabinet's pots their defaults.

## The pieces

| Piece | What it does |
|---|---|
| `daemon/haslerd/dsp/effects.py` | The effects: knob position (0 left … 1 right) + parameters → CamillaDSP block values. Pure, tested. |
| `daemon/haslerd/dsp/knobs.py` | Which effect each knob plays (descriptor default, the owner's choice in `settings.json`, a 30 s *try*), renders the whole chain and hands it on, on a worker thread. |
| `scripts/hasler-dsp` | Installed as `/usr/local/bin/hasler-dsp`, run as `pi`. Writes `Hasler Tone.yml` and tells the running CamillaDSP over its websocket (port 1234). Owns the anti-clipping rule. |
| `scripts/hasler-tone` | The old command (`hasler-tone GAIN`, treble shelf), now `hasler-dsp tone GAIN`. Kept for haslerd.yaml files that name it. |
| `daemon/haslerd/web/knobs_api.py` | `GET/PUT /api/knobs/functions`, `POST/DELETE /api/knobs/try`. |

## One config, always the same shape

`/usr/share/camilladsp/configs/Hasler Tone.yml` holds every block any effect
needs, in a fixed pipeline: the `Stereo` mixer (width), then per channel
`Master gain`, `Treble`, `Bass`, `Tilt low/high`, `Loudness low/high`,
`Presence`, `Vintage low/mid/high`. All are shelves or peaks, and a shelf or
peak at 0 dB is an exact pass-through, so neutral is bit-for-bit flat. A knob
only ever changes numbers, which CamillaDSP takes live without a gap.

`hasler-dsp` keeps the file's `devices:` (and title, description) exactly as
they are and rewrites the rest. A box whose file is the old two-block one is
upgraded at the first knob move: the file, and the running CamillaDSP once
(with its own devices). Both audio paths read the same file: moOde's
per-stream CamillaDSP (multiroom off), and `camilladsp-hasler` while grouped
(`hasler-multiroom tone-config` copies the file at its start); both listen on
port 1234.

```
hasler-dsp set '{"replace": true, "filters": {"Tilt low": {"gain": -4}, "Tilt high": {"gain": 4}}}'
hasler-dsp tone -6      # the old hasler-tone
hasler-dsp reset        # everything neutral
hasler-dsp status       # the file's values, and the live ones + clipped samples
```

Only the blocks above and their own `freq`, `gain`, `q` are accepted;
`width` is 0 (mono) … 1 (stereo). The master gain is never taken from the
caller.

## Never louder than flat

The master gain is minus the highest point of the whole chain's frequency
response (computed from the biquads, 20 Hz–20 kHz), plus some room for phase
whenever anything is shaped: 1 dB, and 1 dB more per 8 dB of the deepest cut,
at most 3 dB. A filter turns the phase as well as the level, so a loud master
with flat tops comes out of a deep bass cut with higher peaks even though no
frequency rose. Live changes are ordered so no chunk plays a boost before its
gain (master down first, blocks a chunk later, master up last; 3 dB extra
while a block jumps more than 3 dB).

So: no digital clipping, and under the volume ceiling no frequency gets more
than with the knobs flat - the old speaker is never pushed harder. The cost
is level: a strong setting sounds a little quieter.

Bench (Pi 4, CamillaDSP 4.1.3, 2026-10-20): every effect at both ends and all
of them at their maxima, through a 0 dBFS heavily limited master and live
switching: 0 clipped samples. CPU with every block active: ~2.2 % of one
core (1.3 % with the old two blocks).

## The effects

| id | Knob | Parameters (default) |
|---|---|---|
| `tilt` | warmer ↔ brighter around a pivot; flat in the middle | range ±2–12 dB (6), pivot 300–2000 Hz (800) |
| `treble` | high shelf | cut 0–18 dB (12), boost 0–9 dB (6), corner 1–8 kHz (2.5 k) |
| `bass` | low shelf | cut 0–18 dB (12), boost 0–9 dB (6), corner 60–500 Hz (200) |
| `loudness` | bass/treble lift that grows as the volume falls; knob = how much | bass 0–12 dB (9), treble 0–8 dB (3), starts below volume 20–100 % (70); full at volume 10 % |
| `presence` | peak where voices are | range ±2–9 dB (6), centre 1.5–5 kHz (3 k) |
| `vintage` | 1960s radio voice, blended in 0 … 1 | bottom corner 60–300 Hz (120, −24 dB shelf), top corner 2.5–10 kHz (6 k, −18 dB shelf), mid bump at 1.5 kHz 0–6 dB (3) |
| `width` | mono … stereo (the mixer) | none. Does nothing while **Mono** is on: the ALSA route sums left and right before CamillaDSP. |
| `none` | nothing | |

The bipolar effects have a flat spot of ±2 % around the middle: a 60-year-old
pot parked at noon is never exactly at noon. One knob per effect (two knobs
on the same blocks would fight); the API refuses the second.

**Why the Hasler HM's tone knob is `tilt`.** The old default, a treble shelf
at 2.5 kHz, worked on the top of what this speaker plays (about 5 kHz of
range); its bright half mostly turned everything else down. A tilt pivoting at
800 Hz moves both ends of the band the speaker really reproduces, so a quarter
turn is clearly heard - which is the whole point of a knob whose only feedback
is the ear (lamp.md).

## A new cabinet's pots

No code. In the descriptor, give the pot an `effect:` (and optionally
`effect_params:`), as `config/variants/excelsior.yaml` does:

```yaml
- {id: treble, kind: pot, channel: 1, taper: log, effect: treble}
- {id: extra,  kind: pot, channel: 3, effect: presence, effect_params: {range_db: 4}}
```

A pot with an effect needs no line in `actions:`. Without `effect:` the
binding decides: `set_tone` → tilt, `set_treble` → treble, `set_bass` →
bass. A pot bound to `set_volume` or `volume_step` is the volume knob and
can't carry an effect.

## Settings

`settings.json`: `"knob_functions": {"tone": {"effect": "vintage", "params": {"low_cut": 150}}}`.
A bad entry is logged and skipped. In `box.yaml`/`haslerd.yaml` under
`moode:`, `dsp_command` (default `/usr/local/bin/hasler-dsp`; `""` = off,
and then `tone_commands` drives the knobs the old way).
