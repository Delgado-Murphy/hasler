# Changelog

## 0.3.9 - 2026-10-20

- Casting: a target whose program isn't installed (AirPlay without
  shairport-sync) is shown as unavailable, with the reason, and can't be
  switched on - moOde would otherwise restart it every 10 s, forever.
- Casting: moOde can take a minute to answer while it starts a renderer;
  that's no longer reported as a failure when the change went through.

## 0.3.8 - 2026-10-20

- The radio announces itself on the network again, so Home Assistant finds
  it by itself (needs 0.3.7 or later installed first).

## 0.3.7 - 2026-10-20

- Same as 0.3.6, but installable on radios running 0.3.5 or older: their
  updater refused 0.3.6's new network announcement file. 0.3.7 leaves it out
  and brings an updater that accepts it; the next release adds it back.

## 0.3.6 - 2026-10-20

- Sources: choose which sources the PU key cycles through and in what order;
  any key can be set to "Cycle sources".
- Casting: AirPlay, Spotify (guests on your Wi-Fi too) and UPnP/DLNA can be
  switched on and named on the key page; the radio goes back to its station
  20 s after a phone stops.
- Volume: the quietest knob step is now a setting (Sound).
- Home Assistant: a key can turn lights on/off, set colour, brightness and
  white temperature, start a scene, script or automation - with "Try it"
  before saving. The radio also announces itself so Home Assistant finds it,
  for the new Hasler integration (hasler-homeassistant).

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
