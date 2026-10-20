# Changelog

## 1.0.1 - 2026-10-21

- Spotify: sign the radio in from the key page - a code to enter at
  spotify.com/pair, no laptop. Change account or sign out there too.
- Bluetooth: the phone's volume is the radio's volume (and follows the
  knob); a steadier start after a phone connects (1 s buffer); phones see a
  loudspeaker; the Bluetooth service can no longer hang at start.
- Wi-Fi setup: the radio's setup network works again after a Wi-Fi change,
  any web address on it opens the setup page, and after a power cut where
  the router is slower the radio keeps trying your Wi-Fi instead of staying
  in setup mode. The setup page works without JavaScript, in English and
  Spanish, and says what the lamp will do. A file on the memory card can set
  the Wi-Fi as a last resort.
- This radio: remove the room name with one tap.

## 1.0.0 - 2026-10-21

The first stable version. Nothing you set up changes: your keys, stations,
settings and knob calibration stay as they are.

What your Hasler does:

- **Press a key, hear music.** Each of the six keys plays an internet radio
  station, a Spotify playlist or a YouTube Music playlist. No phone, no app.
  Music starts about 15 seconds after you switch the radio on.
- **A speaker for your phone:** Spotify Connect (guests on your Wi-Fi too),
  AirPlay, Bluetooth and UPnP. It remembers your Bluetooth phones and calls
  the last one when you switch to Bluetooth. When a phone stops, the radio
  goes back to its station.
- **Several rooms:** Haslers on the same network can play the same music in
  step. Give each radio a room name ("Kitchen") and tap "Also play on...".
- **Two knobs, like the old radio.** Volume, with a floor and a ceiling that
  protect the old speaker. Tone makes the sound warmer or brighter (or one
  of several other effects) and clicks off to put the radio to sleep.
- **A pilot lamp that tells you how things are:** amber for the radio, blue
  for a phone, a dim ember in standby, breathing ember while it installs an
  update, dark when it is safe to unplug.
- **The key page** in your browser (`http://hasler.local:8080`): choose the
  music on each key, sound, phones and casting, rooms, the lamp, Wi-Fi and
  updates. Anything risky asks for a press on the radio itself.
- **Home Assistant**, if you use it: a held key can set the lights or start
  a scene, and the Hasler integration shows each radio.
- **Safe updates.** Small ones install by themselves at night in standby;
  bigger ones ask first. If an update doesn't work, the radio goes back to
  the version before by itself, even after a power cut.
- **Help from the makers**, only if you ask for it: Settings -> This radio,
  for 24 hours, ended at any time.

New in this version:

- Updates: a version with a higher first number (2.0, 3.0...) always waits
  for you, even if it is only a small fix on top of one you haven't
  installed yet.
- "Help from the makers" moved to the bottom of This radio, and it says
  plainly when a maker is connected right now.
- Keys answer at once, even when you switch stations quickly: a new press
  replaces one still starting, and the radio no longer stops its player
  twice on the way.
- Reboots can't hang for minutes any more (a stuck service gets 20 s), and a
  small log stays on the card so a bad shutdown can be looked into.
- New radios can be set up without a screen or keyboard: flash moOde, copy
  six files to the card, switch on (docs/new-install.md).
- Open source: MIT licence, with the licences of everything bundled in each
  release (THIRD_PARTY_NOTICES.md).

Version 1.0.0 is a big step in name only, so the radio asks before
installing it: Settings -> This radio -> Updates -> Install. After that,
updates carry on as before.

## 0.4.6 - 2026-10-21

- Remote help: Settings -> This radio -> "Let the makers help (24 h)". After
  a press on the radio, the page shows a link to send to the makers; once
  they approve it in Delgado & Murphy's Tailscale network they can reach the
  radio for 24 hours (or until you press Stop, or the radio restarts).
  Nothing is stored on the SD card; any other network ends it at once.
- Documentation reviewed against the code: the owner's guide (with "The
  first ten minutes"), the build guide (signed releases earlier, as Phase
  4b), hardware, lamp, multiroom, OTA and release notes.

## 0.4.5 - 2026-10-21

- Settings, rethought: a short home with seven rows - Sound, Phones &
  casting, Rooms, Pilot lamp, Keys & knobs, This radio, and Maker tools
  (hidden: tap the version number 7 times). Each row says how things stand
  now; anything waiting (an update) shows on top.
- One way of working everywhere: a switch or slider applies at once with
  Undo; a change of several fields gets a "Not saved - Cancel - Save" bar;
  risky things ask for a press on the radio.
- Reset keys and the Bluetooth key options moved into Edit keys; plain words
  instead of jargon.

## 0.4.4 - 2026-10-21

- Bluetooth: paired phones on the key page (connect, disconnect, forget,
  battery, last used); which codec is playing (SBC / aptX / aptX HD); a
  "Bluetooth" key action - tap to switch and call your last phone, again for
  the next track, hold to pause (configurable); reconnect to the last phone
  when you switch to Bluetooth or wake the radio (configurable).
- Pilot lamp: ember is visible again - standby 22 % (night 15 %), picked by
  eye; it was too faint to see, so standby looked like "safe to unplug".

## 0.4.3 - 2026-10-21

- Tone knob: now a tilt (warmer <-> brighter around 800 Hz, +-6 dB) - you
  hear it. Settings -> Knobs: choose what a knob does (tilt, treble, bass,
  loudness, presence, 1960s radio voice, stereo width) and how strong, with
  "Try it" before saving. Boosts never clip. The Excelsior's knobs can use
  any of them.
- Pilot lamp: the phone blue is really blue now; a short glow shows where PU
  landed (amber = radio, blue = phone); magenta glows when a radio joins or
  leaves a group; a swell when a phone pairs; a dip when the volume keys hit
  the end; ember breathes while an update installs (don't unplug) and stays
  a steady deep red at night.
- Settings -> Pilot lamp -> Lamp colours: tune each colour by eye on the
  real lamp.
- Bluetooth: phones see the radio as a loudspeaker.

## 0.4.2 - 2026-10-21

- Bluetooth pairing works: while the pairing window is open the radio says
  yes to the phone's code by itself (moOde's helper waited for a yes on its
  own web page, which nobody sees on a Hasler).
- AirPlay and UPnP show as running on the key page (no more "Starting...").
- Spotify for guests reaches radios set up before: a switch in Sources &
  casting, on by default.
- Multiroom: a track keeps its place when a radio starts leading; better
  resampling; quieter logs.
- Updates: a release that brings a new kind of system file must be a major
  one (always asked first); its own updater installs those files.
- Docs: multiroom, casting, room names and Home Assistant; a photo in the
  README; a short box.yaml example.

## 0.4.1 - 2026-10-21

- Room name: call each radio "Kitchen", "Living"... (Settings -> About this
  radio). It shows in the room list, the page title and Home Assistant; the
  page address stays the same. Optional: "Use the room name for casting"
  makes AirPlay, Spotify, UPnP and Bluetooth "Hasler Kitchen".
- Multiroom: when a following radio's key asks for a source the leading one
  doesn't have (say Spotify), the pressed radio takes over and the group
  carries on with its music.

## 0.4.0 - 2026-10-21

- Music in several rooms: Haslers on the same network can play in step
  (Snapcast). Turn it on in Settings -> Multiroom, then "Also play on..." on
  the front page, or give a key "Join another Hasler". Keys on a following
  radio control the whole group (or leave it - a setting); volume, tone and
  standby stay with each radio. Off by default; grouped, sound starts about
  1 s after a key press.
- AirPlay works: shairport-sync (classic AirPlay) is installed by bootstrap,
  started by moOde as before.
- A casting target's own reason ("not installed") is shown.

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
