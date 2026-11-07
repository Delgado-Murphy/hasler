# Changelog

## 1.7.2 - 2026-11-07

- **Shortcuts for your phone** (Settings → Phones): Louder, Quieter, keys
  1–6, Standby, Play/pause and Next song. iPhone: add them from our site, then
  use Siri, Back Tap, the Action button, Control Center or the Lock Screen.
  Android: put the radio's own links on the home screen. No app.
- A one-time hint on phones: put the page on your Home Screen.
- New `POST /api/playpause`; the phone shortcuts API is documented as stable.

## 1.7.1 - 2026-11-07

- Lyrics land on time: the radio now allows for the 1.7 s its sound takes to
  reach the speaker, Earlier/Later move a quarter second and show the value,
  and **Sync** sets it with one tap on the line being sung.
- Lyrics timed for a different recording are no longer used.
- A new radio's setup network has the password **haslerhm**, easy to say and
  printed on the card. A hand-over goes back to it; "Make a new one" still
  makes a random one.

## 1.7.0 - 2026-11-07

Support
- **Write to the makers**: with nothing sent, the page is where you write.
- **Your requests** are threads: Sent · Seen · Answered · Done, our replies
  with their line breaks and simple formatting, and **Write back** (it opens
  a finished request again).
- Our replies show up within a minute while the page is open.

The page
- Lyrics follow the song: the line being sung lights up amber, from YouTube
  Music or LRCLIB; plain lyrics as before when nobody has the timing.
- **Something's wrong?** sits at the bottom of Settings, easy to find.
- Handover is calmer: one "Prepare for handover", and ! only for what really
  blocks it.

Wi-Fi and the lamp
- Re-typing the password of the network you're on saves it without a restart.
- After joining a network the lamp breathes amber, then says OK - never a
  red that nothing clears; with no network at all it calls CQ for setup.
- A setup network named after the room works (Hasler-Kitchen).
- The radio's logs are kept across restarts.

For makers
- Check-ins carry more (schema 2); the dashboard is redesigned, with a page
  per radio, 60 days of history, fleet roll-ups and alert issues.
- Worker: `POST /v1/reply` for write-backs; `/owner` replies keep line breaks
  and edits; requests show open · seen · answered · done.

## 1.6.3 - 2026-11-07

- Ten new starter sets, by taste and occasion: Turn the Dial (the one a radio
  comes with), Mixtape, Level Up, Deep Cuts, After Hours, Encore, Passport,
  Sobremesa, Saturday Morning and Night Light. A radio on an old set moves to
  the closest new one; keys you changed stay yours.
- Lyrics is a small button beside the song; "Start a radio" and "Put on a key"
  live behind ⋯ next to it, so the keys sit higher on a phone.
- **Send to the makers** opens a request with us on every radio, and
  **Your requests** shows its state and our replies.
- Without internet, a request waits on the radio and goes by itself.
- The setup network's password shows on This radio, and the printed key card
  and welcome card carry it unless you untick it.

For makers
- Radios enrol their own support token on first send; the shared token
  still works. `checkin_url` defaults to the support server; `""` turns it off.

## 1.6.2 - 2026-11-07

- The 1.6.1 repair now actually runs: it hands the radio's files to the
  radio's own user (it was asking systemd for the wrong name).

## 1.6.1 - 2026-11-07

- A radio coming from an older version (Stable 1.1.4) keeps its keys and can
  save its settings after the update: files an old updater left as root are
  handed back before the radio starts.
- The Eclectic set plays RNE Radio 3 on key 3, instead of the Coffee Shop list.
- The update channels have one name each, everywhere: **Bench versions** (new
  versions reach the makers' radios first, and may break) and **Stable**
  (everyone). The page's picker said "Test versions".

For makers
- New: [How the radio works](docs/architecture.md), a one-page map with
  diagrams; a fuller [Contributing](docs/contributing.md) (running a fake
  radio on a laptop, every test suite); issue and pull-request templates;
  [SECURITY.md](SECURITY.md).
- The HAT's CERN-OHL-P licence now sits next to its design files too
  (`hardware/hat/LICENSE`).
- Docs: moOde 10.3.7 throughout, ten starter sets, the HAT brought up.

## 1.6.0 - 2026-11-07

The page
- Open **http://hasler.local** - no more `:8080` (that still works).
  moOde's own pages move to `:8081`.
- Simpler Settings: short labels, one-line summaries, details folded away.
- Phone fixes: no zoom when you tap a field, Back closes a sheet, sheets fit
  short screens, bigger tap targets.
- Fixed: the slider that floated away from its track; a key's label after
  you put something new on it.

Keys
- Ten starter sets, by who's listening: little ones, kids, teens, indie,
  grown-up eclectic, classical, jazz, golden oldies, España, world.

Help
- **Send to the makers**: your report goes straight to their private support
  list, and their answer comes back under This radio. You see when they've
  seen it and when it's done.

Player
- Tested on moOde 10.3.7 with MPD 0.24.17. A new radio on an older moOde
  updates it during setup.

For makers
- Owner requests arrive as issues in the private `hasler-support` repo;
  reply with a line starting `/owner`, label `seen`, close when done.
- The check-in server is live (`tools/checkin/`).
- The owner's guide is shorter and only for owners; maker material lives in
  the build guide.

## 1.5.2 - 2026-11-07

- When a Bluetooth phone disconnects, the radio's own music comes back in
  about two seconds instead of half a minute. A phone that only pauses still
  gets its 20 seconds.

## 1.5.1 - 2026-11-07

- Updating no longer trips over a stray named pipe in the radio's state folder
  (it stopped the switch to 1.5.0 on the bench radio).
- An update that rewrites your keys keeps them yours: 1.5.0 handed the
  migrated keys file to the radio as root, so it came up on the built-in
  stations until the owner was put back by hand.
- The 1.5.0 notes below, rewritten so they can be read.

## 1.5.0 - 2026-11-07

Settings, the first setup, moving the radio, what a radio ships with, and
getting help - all reviewed, all reworked.

The front page
- The needle no longer parks on the last key: it points to "Phone" or "From
  the page" when the music isn't from a key.
- A key shows "pressed" and "playing" separately (the Excelsior's latched keys
  included); PU lights only when PU chose the source.
- AirPlay and Spotify Connect show the song title, like Bluetooth.
- After two minutes of a dead stream: "The station may be off the air".

Settings
- Seven rows: Sound · Keys · Phones · Music accounts · Rooms · Pilot lamp ·
  This radio. Old links still work.
- Spotify and YouTube Music accounts together; the room name and Identify
  under Rooms; knob calibration under Sound; one "What phones call the radio".
- The pilot lamp's settings first, its glossary folded.
- Every page says once how its changes are kept; the browser's grey pop-ups
  are gone; a refused change says why, next to the control.
- "Needs attention" also lists a Spotify key with no account, an update that
  went back, Home Assistant out of reach, and the lamp's SOS - with a dot on
  the front page's gear.

Make it yours
- A new radio lists what is still to choose: how loud, the keys, the room,
  where it stands, a phone, the accounts, a card - and ticks them off itself.
- The Wi-Fi setup page describes the lamp it has (amber CQ, green OK).
- The Sound page explains how to set the ceiling while it is unset.
- For makers: a Handover list and a printable welcome card.

Keys
- Starter sets by taste: Eclectic, Classical & calm, Jazz & soul, Pop & hits
  · España, Raíces, Family kitchen - stations and YouTube Music playlists
  that play on a fresh radio. "Start from a set" is at the bottom of Edit
  keys; Reset keys goes back to the chosen set.
- Keys you never changed follow the release when a station moves.
- A "sleep" job for a held key or a combination.
- The Excelsior has its own section in the guide.

Moving this radio
- A page for the three cases: another room, another home, a new owner.
- Forget a Wi-Fi network; the setup network's name and password, to write
  down or print; the lamp blooms green when it joins a new Wi-Fi.
- Hand over to a new owner: forgets everything that was yours, keeps what
  belongs to the cabinet, and restarts into setup mode.
- The setup network carries the room's name; a key can open it.

Sound and health
- The volume ceiling has a top the maker sets for the speaker (90 on the HM).
- The lamp's SOS says why: a full card, dead keys, no sound card, heat, a
  stuck update.
- "How the radio is": Wi-Fi, internet, temperature, the card, the last
  switch-off, power cuts, the player - and "Restart the radio".

Help
- "Something's wrong?" checks the radio and says what it found, in plain
  words, with one thing to try.
- "Tell the makers": a report to share or copy, with nothing private in it;
  the makers' address when they left one.
- "From the makers": a note the makers leave on the radio after a repair.
- Remote help survives a restart; a knob turn can say yes when the keys are
  dead.
- Optional, off to start: "Tell the makers how the radio is" once a day,
  with "See what it sends".

For makers
- The HAT is the default model for new HMs; its tone pot ships the right way
  round.
- The bench LED's Lamp check is every model's standard calibration.
- A paired spare card (`hasler-install memory`); a support bundle; "Lamp never
  checked on this radio"; a knob that drifts says so.
- The check-in's server lives in `tools/checkin/` (a Cloudflare Worker);
  `makers.contact`, `checkin_url` and `starter` in box.yaml / hasler.txt.

## 1.4.0 - 2026-11-07

The pilot lamp, light language v2.

- Five colours, each with a job: amber (the radio), ember (asleep - and it
  never moves), moon blue (a phone), magic-eye green (all good - only as a
  short glow or Morse, never steady) and signal red (something's wrong -
  always moving, never steady).
- Playing is an incandescent filament: a warm gold with a tiny, irregular
  shimmer. Ready is a steady, brighter amber (45 %). Busy (starting, loading,
  installing an update, shutting down) is a deep amber breath - no longer to
  be confused with asleep.
- The radio talks in Morse when it matters: "CQ" (calling any station) while
  it waits to be set up, "HI" in green when you press Identify, "OK" in green
  after an update or a finished setup, SOS in red only for faults that need
  a maker (an update that couldn't be undone, the SD card failing, no sound
  card, the panel board missing, overheating).
- A failed action is HAL 9000's slow red eye; no internet is a red-alert
  double pulse every few seconds, only while the radio is awake and idle.
  Small successes (a phone paired, a confirmation, a Home Assistant action)
  get a short green glow.
- Geek mode (Settings -> Pilot lamp, off by default): Identify sends the
  room's callsign, "73" before shutting down, the Close Encounters notes
  after an update, and a little surprise to find.
- Settings -> Pilot lamp shows what the lamp is saying now, and lists every
  meaning grouped (Everyday, Replies, Needs you, Problems) with an animated
  sample, its rhythm and Show me; "Show me everything" plays them all in
  about a minute. The front page no longer narrates the lamp.
- The printed key card has a "What the lamp says" legend.
- The Lamp check now shows every sample through the lamp's own engine (what
  you pick is what you get), finds floors up to 8 %, and has a step for the
  green. A saved check stays applied after a restart.
- Fixes: no stray flashes in the "press a key" rhythm; standby while a phone
  plays no longer stays blue for 3 s; the Excelsior's key 0 shows a full
  reply on a tap and starts the shutdown fade only after half a second; after
  a power cut at night the lamp wakes gently; colour changes never flick.

## 1.3.0 - 2026-11-07

The pilot lamp, rebuilt to feel calm, warm and immediate.

- A new light language: three colours (amber = the radio, ember = asleep, a
  soft blue = a phone is in charge) and six behaviours (steady; an uneven,
  living breath and a slow one for "busy, don't unplug"; a quick brighten
  when you press a key; a sag for "no" or "failed"; a repeating double
  brighten when the radio asks you to press a key; a slow swell). Colour
  changes close and reopen the lamp instead of passing through muddy shades.
  Red, magenta, pale amber and warm white are gone - failures and setup are
  told by movement, which colour-blind owners can read too.
- Instant: the lamp answers a key the moment it goes down (about 0-20 ms,
  was about 220 ms), before the radio does anything slow; a phone that
  starts playing shows within a quarter of a second (was up to 2 s).
- Never dark by mistake: no dark moment when the radio's software starts or
  restarts during an update; a steady ember at shutdown; a slow "busy" breath
  if the software ever hangs.
- Settings -> Pilot lamp: "What the lamp means", each with Show me on the
  real lamp; Brightness (Low / Normal / Bright); key replies on or off;
  Identify this radio (also for the other Haslers in Rooms). The front page
  says what the lamp shows in words ("Lamp: asleep").
- Maker tools -> Lamp colours: a guided Lamp check (about six steps, each a
  choice between two samples on the real lamp) that calibrates floors,
  amber by day and night, the standby glow and blue's brightness; saved in
  the radio's own settings, so updates keep it.
- Realistic low-brightness limits and a planned amber-to-ember blend: no
  more red drift or colour flicks when the lamp is dim.
- The Excelsior gets the pilot lamp, and holding key 0 shows the same
  five-second fade to shutdown as the HM's tone knob.

## 1.2.2 - 2026-11-07

- Fixed: when a phone took over (Bluetooth, AirPlay, Spotify Connect), the
  page went on showing the last station instead of the phone's song, until
  the radio had been in standby. While a phone has the speaker, no preset is
  reported as playing.

## 1.2.1 - 2026-11-07

- The Excelsior gets the same key page as the HM. Its keys 1-6, PU and Tape
  show and can be set; a latching bank shows the key that is down, key 0 is
  drawn as the off key, and the front page shows SP, the speaker selector
  (both / left / right), treble and bass. Wording follows the model ("Select"
  for latching keys, no source key where there is none, the model's name).
  Latching keys have no hold job (a latched key reads as held); key 0 is
  standby, held it shuts down. Excelsiors modified so the keys spring back:
  `controls: {presets: {latching: false}}` in
  /etc/hasler/variant.d/excelsior.yaml, then tap and hold work as on the HM
  (docs/hardware.md, "Excelsior: latching or not").
- Fixed: Standby, or the tone knob, while a phone played over Bluetooth woke
  the radio again at once. Standby now asks a Bluetooth phone to pause, and
  only a new start on a phone wakes the radio.

## 1.2.0 - 2026-11-07

Keys
- Tap | Hold on every key: each key's page has two tabs, each showing what it
  does now; the hold is one tap away instead of below the whole station
  browser. "Reassign" from Home Assistant's "needs attention" opens the Hold
  tab with the search ready and the missing name shown.
- See what holding does: Edit keys lists "Tap ·" and "Hold ·" for every key,
  and the keys show a small mark when they have a hold job (a "!" when Home
  Assistant no longer has its target). Holding a key works from the keyboard
  too (Space or Enter, longer than half a second).
- Your own short label for a key's cap (up to 12 letters), under Rename.
- A printable key card (A6, or a strip for behind the keys): Edit keys ->
  Print a key card.
- Done never throws work away: with unsaved changes it asks "Save and close /
  Discard / Keep editing", the same question as Back and leaving Settings.
- Two phones can't undo each other: a save that started from older keys is
  merged with the other phone's change instead of wiping it, and says so if
  both changed the same key. An idle page shows the other phone's change.
- Test on the radio shows "Trying on the radio · 0:30" with Back; tapping
  the station that's playing says "Standby - tap again to play"; the toast no
  longer covers Save; a refused change explains "This page is open at ...,
  which can't make changes yet" with "Allow this address".
- Back no longer leaves the page after a cancelled Back.

Sleep timer
- The clock on the Standby button: 15, 30 or 60 minutes. In the last minute
  the volume fades and the lamp dims, then the radio goes to standby; the
  next wake plays at your usual volume. Any key or knob cancels it; a chip on
  the front page shows the time left.

Settings
- One rule: switches and choices apply at once, with Undo; names and places
  wait for Save. The tone knob's effect applies straight away.
- Plain words instead of internal names (no more "moOde" or "avahi").
- The room name lives in This radio -> Name and address only; "Source key &
  knobs" (or "Knobs & keys" on a radio without a source key).
- The front page's tone bar no longer looks like a slider you can drag.

Also
- Add the page to your phone's home screen: it opens like an app, named
  after the room.
- Keyboard and screen reader: focus stays put while saving, status messages
  are announced, the pairing countdown no longer talks every second.

## 1.1.8 - 2026-11-07

- After "Allow this address", whatever was refused now goes through with its
  real answer - not only a key's Save: "Try it" on a Home Assistant action,
  a setting, anything. The page sends the same request again once the radio
  has allowed the address, so its red "not this radio's address" message is
  replaced by the actual result. (1.1.7 re-sent only key saves properly.)

## 1.1.7 - 2026-11-07

- After "Allow this address", the change that was refused goes through by
  itself: a key's Save is sent again (and its red "not this radio's address"
  message goes), and any other refused change is re-sent and the page reads
  the radio again. Nothing needs saving twice.

## 1.1.6 - 2026-11-07

- Changes through your own address: when the key page is open through a name
  of yours - a home dashboard, Home Assistant's remote access, Tailscale, a
  domain - and a change is refused because the radio doesn't know that
  address, the page offers "Allow this address". One press of any key on the
  radio confirms it (a page reached from outside can ask, but can't press),
  and changes made there work from then on. Only the address the page is
  open at can be allowed, and always with a real key press, even on a bench
  radio. This radio -> Other addresses lists them, with Remove; box.yaml's
  web.hostnames still works and is listed there too.

## 1.1.5 - 2026-11-06

- The tone knob keeps up with your hand: each step reaches the sound in
  about 0.13 s instead of 0.5-0.7 s, so turning it no longer trails behind
  and catches up afterwards. The DSP helper now stays running instead of
  starting afresh for every step, and saves the setting for the next stream
  once the knob has been still for a second. The anti-clipping order (master
  level down before a boost) is unchanged.
- Bench-tested on the Hasler HAT (rev A): keys, PU, both knobs, the tone
  end switch and USB-C power all work with `variant: hasler-hm-hat`.

## 1.1.4 - 2026-10-25

- Fixed: switching away from an HLS radio station (RNE Radio 3, Onda Cero,
  Europa FM, BBC Radio 1 and 33 more) could freeze the player for good, and
  the whole radio with it. The radio now turns HLS stations into an ordinary
  stream itself, so the player never runs the decoder that got stuck (MPD
  0.24's; fixed upstream only in 0.25). HLS stations take 0.5-3 s longer to
  start.
- If the player ever stops answering for another reason, the keys and the
  key page keep working, systemd restarts the player within about 20 s (the
  radio itself does it after 30 s if needed, at most 3 times an hour), and
  what you were playing comes back. The lamp breathes meanwhile, and the
  health report says why.
- A player that stopped answering no longer makes an update roll back: an
  update is judged on the radio's own software.
- Includes 1.1.3 (the radio follows its Home Assistant to a new address),
  which the player freeze had rolled back.

## 1.1.3 - 2026-10-25

- The radio follows its Home Assistant to a new address. If HA stops
  answering (a new IP from the router, a new router, HA reinstalled), the
  radio looks for it on the network and switches only to the same HA - the
  one that announces the same identity - after checking its access token
  works there. Scenes, lights and HA sync carry on without a restart. The
  token never goes to any other address; a different HA (say, the radio has
  moved house) is never adopted.
- Settings -> Home Assistant shows the address in use, says when the radio
  found HA at a new one, and has Change address while connected. First set-up
  looks for HA as soon as the page opens.

## 1.1.2 - 2026-10-24

- The key page rides out a lost request: lookups and saves quietly try
  again (key presses, Next and Play are never sent twice), and a short gap
  shows "Reconnecting..." over the last-known state instead of "Can't reach
  the radio", which now waits for several misses. Coming back to the page
  after the phone sleeps no longer flashes "No connection".
- Saving a key says Saving, Still trying, Saved or "Couldn't save - your
  changes are still here" with Try again; if the answer was lost but the
  radio saved, the page sees that. Put on a key, Settings and My stations
  keep a Try again instead of a fading message.
- The key page answers on IPv6 as well as IPv4 (phones that try IPv6 first
  no longer get refused); IPv6 clients must be on the home network.
- Every change made from the page is one line in the radio's log, so a save
  that never arrived can be told from one that failed.

## 1.1.1 - 2026-10-24

- Browse: the Radio tab opened empty on a real radio (the world regions
  arrived in a different shape than the page expected). Fixed; Arabic and
  other right-to-left language names no longer swallow their counts.

## 1.1.0 - 2026-10-24

- A world of radio: 585 hand-picked stations from 118 countries - public
  broadcasters, iconic music stations, college and community radio, and
  specialists in every genre - each one checked by playing it on a Hasler.
- A new music browser with one tab per source: Radio, YouTube Music and
  Spotify. Radio: search as you type, filters (genre, where, language,
  quality), Near you, Staff picks, genres, a world map and languages; every
  station has a card with Play now, Put on a key, Save and "Doesn't play?".
  Internet search (unchecked stations) only when the collection has fewer
  than five matches. Edit keys uses the same browser.
- My stations: a shortlist of favourites and your own stream links (tested
  on the radio before saving), plus Recently played.
- YouTube Music: search with suggestions, artist and album pages, Moods &
  genres, Charts by country, your library when signed in, and 201 curated
  Staff picks (playlists and classic albums). A key can now hold an album,
  an artist (top songs, radio or shuffle), a radio from a song or album, a
  country chart, a mood or your Liked songs. The front screen shows the
  album art with Start radio from this song, Put this on a key, Like (when
  signed in) and lyrics. "Hide explicit songs" in This radio -> YouTube Music.

## 1.0.9 - 2026-10-24

- Key combinations: the default is now 1 + 6 (Bluetooth pairing). On the
  Hasler HM neighbouring keys block each other - with 1 held, 2 can't
  close - so 1 + 2 never worked. Radios that never changed their
  combinations get 1 + 6 by themselves.
- Edit keys -> Key combinations: "Check this pair" - hold the two keys and
  the page shows whether the radio sees both at once. While it checks, the
  keys do nothing else.

## 1.0.8 - 2026-10-24

- Key combinations: hold two keys together for about a second to do
  something extra. Out of the box, 1 + 2 opens Bluetooth pairing for two
  minutes. Neither key then does its own tap or hold, and letting go early
  does nothing. Edit keys -> Key combinations: add up to 8, each with its
  own keys, hold time and action, and try it on the radio.

## 1.0.7 - 2026-10-24

- Keys: the bounce window is half a second (was 0.35 s). Keys 2 and 3 were
  seen bouncing up to 0.34 s after a press.

## 1.0.6 - 2026-10-23

- Keys: a station key pressed again stops the radio for good. An old key's
  contacts can send one press as two taps a fifth of a second apart; the
  second used to start the station again. A second tap of the same key
  within 0.35 s is now ignored.

## 1.0.5 - 2026-10-23

- YouTube Music keeps working by itself: the radio checks for a newer
  yt-dlp every 6 hours (and right after coming back online, or as soon as
  tracks start failing), takes the newest from PyPI - stable, or the nightly
  when stable can't play - from a signed, hash-checked list, tests it on a
  real YouTube Music track before switching, and switches back by itself if
  tracks fail in the next three days. This radio -> Updates shows it; Maker
  tools can update or go back by hand.
- Security updates for the system install at night in standby (Debian
  security only; never the kernel, firmware, moOde's audio or Bluetooth),
  with a health check afterwards and the old packages put back if anything
  broke. Off with `os_updates: off` in box.yaml.
- Spotify engine go-librespot 0.10.5 and Deno 2.9.8 (what the current
  yt-dlp asks for).

## 1.0.4 - 2026-10-22

- A brief power dip on the key board can no longer press every key at once:
  until the board is set up again the radio keeps the keys as they were
  (before, for up to a second, it could change the station by itself).
- The tone knob's switch arms the 5-second shutdown only after it has
  clearly read on, so a loose ribbon can't power the radio off just after
  it starts.
- Ships the cabinet description for the Hasler HAT circuit board
  (`hasler-hm-hat`), used only once a radio is set to it.

## 1.0.3 - 2026-10-22

- A station that stops by itself comes back by itself: the radio notices
  within seconds, plays it again (and, if needed, starts the preset again),
  waiting a little longer each time; after three tries the lamp pulses red
  once (not at night). It never undoes a stop you made. The key page says
  "Reconnecting..." meanwhile instead of claiming it plays.
- Each stop and recovery is written to a small file on the memory card, so
  a silent radio can be explained afterwards (Maker tools -> Diagnostics).

## 1.0.2 - 2026-10-21

- The memory-card Wi-Fi rescue (hasler-wifi.txt) now arrives with updates
  too, not only on new SD cards.
- This radio -> Wi-Fi shows the setup network's name and password, to write
  down for the day the radio can't find your Wi-Fi.
- A last tidy-up of the key page and every document: plainer words, no
  leftovers from older layouts, Home Assistant scenes no longer say
  "unknown", the Bluetooth key reads simply "Bluetooth".

## 1.0.1 - 2026-10-21

- Spotify: sign the radio in from the key page - a code to enter at
  spotify.com/pair, no laptop. Change account or sign out there too.
- Bluetooth: the phone's volume is the radio's volume (and follows the
  knob); a steadier start after a phone connects (1 s buffer).
- Wi-Fi setup: the radio's setup network works again after a Wi-Fi change,
  any web address on it opens the setup page, and after a power cut where
  the router is slower the radio keeps trying your Wi-Fi instead of staying
  in setup mode. The setup page works without JavaScript, in English and
  Spanish, and says what the lamp will do. A file on the memory card can set
  the Wi-Fi as a last resort. (The memory-card file and the new captive page
  come with a new SD card; a radio updated over the air gets them when its
  maker re-runs the setup script.)
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
- Bluetooth can no longer hang while it starts.
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
