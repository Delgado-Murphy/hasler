# Changelog

## 0.3.5 - 2026-10-20

- Key page: easier to read (contrast), bigger buttons, settings grouped
  (Sound, Connections, Pilot lamp, Knobs, About this radio, Keys); Next is
  greyed out where it can't skip; the offline view is dimmed; lighter polling.
- Faster and steadier: the radio no longer runs a heavy moOde tool every
  second, reads the keys and knobs with fewer transfers, and survives a
  glitch on the cabinet cable instead of restarting.
- Settings files are written safely (power cuts), clearer errors, and an
  "off" update mode written by hand is understood.

## 0.3.4 - 2026-10-20

- Two update channels: Bench and Stable; new radios update automatically for
  small releases.

## 0.3.3 - 2026-10-20

- Keys: a bounced second tap no longer stops the station it just started
  (an old key's contacts can register one press as two taps).
- Key page: choose the update channel (Stable, Beta or Bench) in the Software
  card, confirmed with a press on the radio.

## 0.3.2 - 2026-10-20

- YouTube Music no longer stutters: YouTube throttles a plain download of some
  tracks to ~32 KB/s, so the radio now fetches each track in fast byte ranges
  and plays it from local storage, with the next track downloaded ahead.
- Pilot lamp at power-on: the warming-up breath starts with a clear amber glow
  (a slow 2 s fade up, then breathes 25-60 %) instead of a near-invisible 10-35 %.

## 0.3.1 - 2026-10-20

First signed release through GitHub.

- Over-the-air updates: signed releases, a health check and automatic rollback
  (also after a power cut mid-update), manual "go back" from the key page.
- Your settings and knob calibration live outside the release
  (`/etc/hasler`); a calibration wizard for every knob on the key page.
- Key page: Software card; installing, rolling back, Wi-Fi and reset need a
  press on the radio; protection against malicious web pages.
- Pilot lamp: one meaning per colour, smooth fades, night mode by the sun.
- Boot: music about 15 s after power-on (was 40-60 s); YouTube Music plays its
  first track from a cache; no music after a power cut at night.
- yt-dlp updates weekly as its own user, with a self-check.
