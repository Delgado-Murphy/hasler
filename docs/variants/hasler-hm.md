# Hasler HM — panel survey

> Fill this in on the bench. Every blank here becomes a value in
> `config/variants/hasler-hm.yaml`. Nothing downstream can be finished until it is.

**Cabinet:** Hasler HM, Bern, ~1964 · Telefónica *Hilo Musical* wired-music receiver
**Surveyed:** ______________  **By:** ______________

---

## 1. Keys — 7 momentary (1–6 + PU)

Already out with a shared ground. **Do not trace them by hand** — wire them into any eight GPIOs
and let `scripts/panel-probe.py --identify` map them. Only record anomalies here.

| Key | Momentary? | Extra poles? | Notes |
|---|---|---|---|
| 1 | ☐ | | |
| 2 | ☐ | | |
| 3 | ☐ | | |
| 4 | ☐ | | |
| 5 | ☐ | | |
| 6 | ☐ | | |
| PU | ☐ | | |

Common ground wire colour: ______________

**Anything not momentary changes its `kind:` in the descriptor** — a key that stays down is a
`latching_toggle`, not a `momentary`.

---

## How to measure a pot

```
      shaft toward you
      ┌───────────────┐
      │      ( )      │
      └───┬─────┬────┬┘
          1     2     3
          └──track──┘
                │
              WIPER
```

| Reading | Probes | Behaviour |
|---|---|---|
| **Total resistance** | 1 ↔ 3 (the two outers) | **Steady** as you turn — that is how you confirm you have the right pair |
| **Position / taper** | 2 ↔ 1 (wiper to an outer) | **Changes** as you turn |

**Taper test:** knob at the *mechanical midpoint*, measure wiper → the outer tag that corresponds to
**fully counter-clockwise**, divide by total. ~50 % → linear. ~10–20 % → audio/log. Expect log.

**Smoothness:** leave the probes on wiper-to-outer and sweep slowly end to end, watching for jumps
or an `OL` flicker (a dead spot). A DMM updates 2–3× a second so this is coarse — `panel-probe` in
Phase 3b is the real test — but it catches gross faults.

### Two things that give wrong numbers

- **Do not pinch both probe tips with your fingers.** At 500 kΩ–1 MΩ your body is roughly 1 MΩ in
  parallel with the pot and you will read substantially low. Hold the insulation, or use clips.
- **Measure fully disconnected.** Any remaining wiring adds parallel paths.

### The tone pot has five tags

Three for the track, **two for the switch**. The switch pair reads ~0 Ω or `OL` and flips at the
detent — never anything in between. A *fourth* track terminal would be a **centre tap** (loudness
compensation on some sets), not a fault.

### ⚠️ The two pots share their outer rails (confirmed on this cabinet)

Pot A tag 1 ↔ pot B tag 1, and tag 3 ↔ tag 3 — the original shared supply and ground rails.

**This invalidates the total-resistance reading.** 1 ↔ 3 now measures both tracks *in parallel*: two
1 MΩ pots read ~500 kΩ and look like 500 kΩ pots. To get real values, **lift one link**, measure
each pot's 1 ↔ 3 alone, then reconnect.

- [ ] Link 1–1 measures ~0 Ω (plain wire)
- [ ] Link 3–3 measures ~0 Ω (plain wire)

> **Anything other than ~0 Ω means a surviving component is bridging them** — and vintage tone
> controls are full of these, typically a capacitor from an end tag or the wiper to ground. Trace it
> and remove it. Tone is done in DSP now, so the original network is dead weight that will skew the
> divider and produce a pot that "sort of works but drifts".

**The topology itself is what we want**, so keep it. Each pot has to be a divider across 3.3 V, and
the commoning is already done:

```
   3.3 V ──┬── tag 3 (pot A) ──╮
           └── tag 3 (pot B) ──┤  tracks
                               │
   GND ────┬── tag 1 (pot A) ──╯
           └── tag 1 (pot B)

   wiper A ──── ADS1115 A0        wiper B ──── ADS1115 A1
```

**The pot loom is 4 wires, not 6:** 3.3 V, ground, and the two wipers. Loading is irrelevant — two
1 MΩ tracks across 3.3 V draw about 7 µA.

- [ ] Confirm tag 3 is the *same physical end* on both pots relative to knob rotation (a mirrored
      mounting reads backwards — harmless, but you want to know)

**Measured values — both comfortable for the ADC, and the low one is the better of the two:**

| | Tone 53 kΩ | Volume 600 Ω |
|---|---|---|
| Current across 3.3 V | 62 µA | 5.5 mA — fine, the header rail supplies ~500 mA |
| Wiper source impedance, worst case | ~13 kΩ | **~150 Ω** — excellent for the ADS1115 |
| What 100 nF actually filters | 1.3 ms constant, useful | 15 µs — negligible |

So the ADS1115 sampling-charge concern does not arise at these values. But note the last row: **on
the volume pot the 100 nF barely filters anything.** If `panel-probe` shows it scratchy, either fit
1 µF or lean on the software median-of-5, which exists for exactly this. Measure before fitting the
bigger cap.

**Leave the shared links cut.** Wire each pot's outer tags to the 3.3 V and ground rails directly
when building the ADC harness — electrically identical, but driven from a low-impedance supply there
is no path for the pots to interact and no loop to confuse anyone in two years.

### Reading the fault: a non-monotonic sweep

**A pot measured on its own cannot be non-monotonic.** Wiper-to-end is physically a fraction of the
track, so it must rise steadily from 0 to full. If it rises and then *collapses*, current has a
second path — you are measuring a loop.

The shape identifies it. Two parallel paths from wiper to an end — the direct one (R) and the way
round through the rest of the track (R_total − R) — give **R ∥ (R_total − R)**: a parabola, zero at
both ends, peaking at **R_total / 4**.

> Observed on this cabinet: rises 0 → **16.5 kΩ** at roughly ¾ turn, then falls to ~70 Ω at full CW.
>
> **Resolved:** the loop was the two shared links themselves — current ran from the wiper, round
> through the *other* pot's track, and back. Cutting them gave clean readings: **tone 53 kΩ, volume
> 600 Ω**. The peak sits near (53 k + 600)/4 ≈ 13 k, in the right region for a cheap meter.

**Fix:** lift one link, re-measure. Expect a clean ramp, 0 → ~50–68 kΩ, never falling.

**Find the loop before wiring the ADC** — it would make the two pots interact, so moving the tone
knob would shift the volume reading. Most likely culprits:

- [ ] Tone pot wired as a **rheostat** (its wiper strapped to one of its own ends) — common in
      vintage tone networks, and it shorts the two rails through part of its track
- [ ] A surviving wire or component from the original circuit

### When the numbers contradict each other: the 3x3 table

Observed on the volume pot: total 596 Ω, yet wiper-to-outer sweeping 0 → **1208 Ω** before dropping
to 389 Ω. **A wiper reading can never exceed the end-to-end total** — it is physically a fraction of
the same track. So either the "total" was not across the two outers, or something is still connected.

Resolve it without guessing which tag is which. Label them **A, B, C** and fill in all nine cells:

| Knob | A↔B | A↔C | B↔C |
|---|---|---|---|
| Full CCW | | | |
| Midpoint | | | |
| Full CW | | | |

1. **The pair reading the same in all three rows is the two outers**, and that value is the total.
   The remaining tag is the wiper.
2. **The sum rule:** at every position, `(wiper↔A) + (wiper↔B) = (A↔B)`.

Rule 2 is a property of a plain resistive track and cannot fail — unless current has somewhere else
to go. Rows that do not add up mean something is still wired to the pot.

**If the sum rule fails structurally rather than randomly**, the volume control may not be a plain
pot. A **tapped pot** or a constant-impedance **L-pad** is appropriate for a 600 Ω telephone-line
feed, and an L-pad is *designed* to hold input impedance constant while the division changes —
which looks exactly like a stubbornly steady total alongside wiper readings that rise then fall.
Either is still usable; we just need to know which pair behaves as the divider.

- [ ] Checked for a **fourth terminal**, including any tag or lug on the pot body (a tapped pot has
      one partway along the track)

### Direction does not matter

If a pot ends up backwards — clockwise reading low — do not rewire it. Swap the two outer wires, or
enter the `curve` points descending. `panel-probe` shows live counts, so you will see it at once.

---

## 2. Volume pot

| Measurement | How | Value |
|---|---|---|
| Total resistance | across both outer tags, links lifted | ⚠️ **unresolved** — 596 Ω claimed, but wiper reads up to 1208 Ω. Run the 3x3 table above |
| Steady while turning? | must not change — otherwise a probe is on the wiper | ☐ confirm |
| Wiper at mechanical midpoint | wiper → CCW tag, as % of total | ________ % |
| Taper | ~50 % → **linear**; ~10–20 % → **audio/log** | ☐ linear ☐ log |
| Cleaned? | contact cleaner + 20 full sweeps | ☐ |
| Scratchy after cleaning? | | ☐ |

**600 Ω is low for a volume control, and exactly right for this device.** Hilo Musical distributed
audio over telephone pairs at **600 Ω** — the standard telephony line impedance — so the volume
control is a line-matched attenuator, where an ordinary valve radio would use 500 kΩ at a grid. An
earlier reading of 383 Ω was wiper-to-outer at some knob position, not the full track; 600 Ω is a
standard value and 383 Ω is a standard nothing.

---

## 3. Tone pot — the one with the switch

| Measurement | How | Value |
|---|---|---|
| Total resistance | across both outer tags, links lifted | **53 kΩ** ✅ measured (a 50 k part) |
| Wiper at midpoint | as % of total | ________ % |
| Taper | | ☐ linear ☐ log |
| Cleaned? | | ☐ |

### The end-switch

Two independent wires — the original mains on/off, ganged to the CCW end-stop.

- [ ] Continuity-tested through the **full** rotation
- [ ] Confirmed **open only at the CCW detent**, closed everywhere else
- [ ] Confirmed **electrically independent** of the resistive track

| | |
|---|---|
| Closed = knob **on** (expected) | ☐ confirmed ☐ inverted — note it! |
| Switch wire colours | ______________ |

> If it turns out **inverted** (closed when off), say so — it is one line in the descriptor, but
> silently wrong behaviour if missed: the box would boot straight into standby.

---

## 4. Speaker

| Measurement | Value | Meaning |
|---|---|---|
| DC resistance | **14.2 Ω** ✅ measured | Voice-coil DCR runs ~0.8× nominal → **16 Ω nominal** |
| Transformer in path? | **No** | A transformer secondary would read under 1 Ω. This is the coil itself — **drive it directly from the Amp4 Pro** |
| Cone condition | ☐ intact ☐ torn ☐ rubbing | |
| Frame dimensions | ________ mm | for a replacement if ever needed |

> Subtract your meter's lead resistance (short the probes on the 200 Ω range —
> typically 0.2–0.5 Ω). It does not change the conclusion here.

### What 16 Ω means

**Impedance is fine.** It sits above HiFiBerry's stated 4–8 Ω range, but that is the *safe*
direction: a higher load draws *less* current from a voltage-source amplifier. The minimum (4 Ω) is
the hard limit; the top of the range is only where rated power stops being guaranteed. The one real
caveat is that a class-D output LC filter is tuned for a nominal load, so a higher impedance leaves
it under-damped and can peak somewhere around 20–40 kHz — inaudible on a cone with maybe 5 kHz of
usable bandwidth.

**Power is the actual risk.** Into 16 Ω the Amp4 Pro delivers roughly half its 8 Ω figure — about
**10 W**. A vintage elliptical this size is good for perhaps **1–3 W**. That is five to ten times
more amplifier than the cone can survive, on a part that cannot be replaced with anything
authentic.

Mitigated in software by `moode.volume_limit` in `config/haslerd.yaml`. **Calibrate it:**

- [ ] Set `volume_limit: 50` and restart the daemon
- [ ] Play something dense — full-band music, not speech — with the knob at maximum
- [ ] Raise the limit in steps of 5, restarting each time, until it is as loud as you ever want it
- [ ] **Stop at the first hint of cone distortion or buzzing** and back off two steps
- [ ] Record the final value here: `volume_limit: ________`

The knob keeps its full mechanical sweep and resolution either way — it just tops out somewhere
safe, which is how the original would have behaved.

### Wiring the mono cabinet

The Amp4 Pro is stereo; the Hasler is mono.

- [ ] Speaker on **one channel only** (left), with L+R summed to mono upstream so nothing is lost
- [ ] **⚠️ Never wire the speaker across the two + terminals.** Class-D outputs are already
      bridge-tied; connecting across two of them is a dead short that destroys the amplifier.

---

## 5. Pilot lamp

| | Value |
|---|---|
| Original lamp type / voltage | ______________ |
| Bezel aperture | ________ mm |
| RGB LED fits? | ☐ |

---

## 6. Pot calibration — Phase 3b, once the ADS1115 is fitted

Run `scripts/panel-probe.py` and read the **COUNTS** column while sweeping each pot.

### Volume (channel 0)

| Position | Counts |
|---|---|
| Fully CCW | ________ |
| 25 % | ________ |
| 50 % | ________ |
| 75 % | ________ |
| Fully CW | ________ |

### Tone (channel 1)

**The detent count is the important one** — the switch eats the bottom of the sweep, so the usable
range starts *above* it. Everything below the detent is the off position, not a tone setting.

| Position | Counts |
|---|---|
| **At the detent (just as it clicks on)** | ________ |
| 25 % past detent | ________ |
| 50 % | ________ |
| 75 % | ________ |
| Fully CW | ________ |

---

## 7. Transfer into the descriptor

Replace the `taper` + `range` placeholders in `config/variants/hasler-hm.yaml` with the measured
points — a `curve` beats a guessed taper every time:

```yaml
  - id: volume
    kind: pot
    channel: 0
    curve: [[<fully CCW>, 0], [<25%>, 25], [<50%>, 50], [<75%>, 75], [<fully CW>, 100]]

  - id: tone
    kind: pot
    channel: 1
    curve: [[<AT THE DETENT>, 0], [<25%>, 25], [<50%>, 50], [<75%>, 75], [<fully CW>, 100]]
    end_switch: {id: power, pin: d7, off_hold_ms: 5000}
```

And paste the `pins:` array printed by `panel-probe.py --identify`.

**Then re-run the tests** — they load this descriptor, so a malformed curve fails on the laptop
rather than in the cabinet:

```sh
cd daemon && .venv/bin/python -m pytest -q
```
