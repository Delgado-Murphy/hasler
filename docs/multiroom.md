# Multiroom and casting, for makers

This is how two or more Haslers play the same music in step, and how phones
cast to a radio. It is for the person who builds or repairs a radio. The
owner's side (what to tap, what the lamp shows) is in
[Using your Hasler](using.md#music-in-several-rooms).

**Contents**

- [In one paragraph](#in-one-paragraph)
- [What a radio needs](#what-a-radio-needs)
- [The audio path](#the-audio-path)
- [How radios find and talk to each other](#how-radios-find-and-talk-to-each-other)
- [Roles, keys and standby](#roles-keys-and-standby)
- [Handover](#handover)
- [Power cuts](#power-cuts)
- [Room names and casting names](#room-names-and-casting-names)
- [Casting](#casting)
- [Home Assistant](#home-assistant)
- [Checking and fixing](#checking-and-fixing)

---

## In one paragraph

Multiroom is between Haslers only, and off until the owner switches it on
(Settings → **Rooms** → *Play in several rooms*) on each radio. One radio **leads**: it sends what
it plays to the others through [Snapcast](https://github.com/badaix/snapcast),
and plays the same stream itself, so every speaker is in step. The others
**follow**: their own players are stopped and a Snapcast client plays the
leader's stream. Volume, tone and standby stay with each radio. Grouped, sound
starts about **1 second** after a key press (that is Snapcast's buffer). The
cost while grouped is about **30 MB of memory and 3–4 % CPU** on a Pi; while
multiroom is off or the radio is alone, nothing extra runs.

## What a radio needs

| Part | Where it comes from |
|---|---|
| `snapserver`, `snapclient` (Debian's 0.31), `libasound2-plugins` (the speex resampler for players not at 44.1 kHz) | `scripts/bootstrap.sh` installs them and **masks** Debian's own `snapserver.service` / `snapclient.service`, so they never start by themselves. |
| `snapserver-hasler`, `snapclient-hasler`, `camilladsp-hasler` units | Every release (`config/*.service`). They are never enabled: only `hasler-multiroom` starts and stops them. |
| `/usr/local/sbin/hasler-multiroom` | Every release. The one root helper; sudoers allows `haslerd` exactly this file. |
| `config/snapcast/snapserver.conf` | Every release, as `app/config/snapcast/snapserver.conf`. |
| The `_hasler._tcp` announcement | `config/avahi/hasler.service` (port 8080, `path=/api/info`). |

Multiroom needs a **release install** (`/opt/hasler/current`): the building
layout from `scripts/install.sh` has neither the helper nor the units, and says
so at the end of every run. Move the radio over first
([build guide, Phase 4b](build-guide.md#phase-4b--move-to-signed-releases)).

A radio set up before 0.4.0 gets the Snapcast packages (and AirPlay's
`shairport-sync`) by running `sudo scripts/bootstrap.sh` once more. A release
cannot install Debian packages; until they are there, switching multiroom on
says "Multiroom isn't installed on this radio (Snapcast is missing)".

## The audio path

Every local player (MPD, go-librespot, AirPlay, Bluetooth, UPnP through MPD)
already plays into moOde's ALSA name `_audioout`. Multiroom changes only where
that name points.

```
alone   players ─▶ _audioout ─▶ card (mono, tone CamillaDSP) ─▶ speaker

lead    players ─▶ _audioout = Loopback 0,0 ─▶ snapserver-hasler ─▶ network ─┐
                                                                            ▼
        snapclient-hasler (127.0.0.1) ─▶ hasler_local ─▶ card ─▶ speaker   followers

follow  snapclient-hasler (leader) ─▶ hasler_local ─▶ card ─▶ speaker
        (_audioout still points at the Loopback, which nobody captures)
```

- **The switch is one file in /run.** A fixed hook,
  `/etc/alsa/conf.d/zzz-hasler-multiroom.conf`, loads
  `/run/hasler-multiroom/alsa.conf` if it exists, and does nothing otherwise.
  `/run` is empty after every boot, so a radio always starts **alone**. moOde's
  own files are never touched.
- **`hasler-multiroom lead | follow HOST [PORT] | alone | status`** does the
  root work and prints one JSON line. Any failure in `lead` or `follow` goes
  back to `alone`, so the radio is never left silent.
- **The Loopback** (`snd-aloop`) is loaded only while grouped, and unloaded
  again afterwards unless moOde's own "ALSA loopback" uses it.
- **snapserver** listens on port **1704** for audio. Its control port (1705) is
  bound to `127.0.0.1`, and there is no web UI: nobody on the network can change
  volumes or groups. FLAC, 44.1 kHz / 16 bit, `buffer = 1000` ms.
- **snapclient** runs `--mixer none`: it never touches the volume. Each radio's
  knob drives its own card's hardware mixer, as always.
- **The tone knob while grouped.** moOde's per-stream CamillaDSP reports a
  delay that jumps in ~90 ms steps, and snapclient re-syncs (audibly) on every
  jump. So while grouped, and only if the tone DSP is on, `camilladsp-hasler`
  runs one CamillaDSP from a second Loopback substream to the card, with the
  same `Hasler Tone.yml` and the same websocket port (1234). `hasler-dsp`
  turns it live, so every knob effect works the same ([sound.md](sound.md)).
- ALSA reads its routing when a device opens. After a switch `haslerd` restarts
  a playing MPD; the other players move at their next open.

The code side is `daemon/haslerd/multiroom/audio.py` (`SnapAudio`: `lead()`,
`follow()`, `alone()`, `status()`). Every call is idempotent.

## How radios find and talk to each other

- **Finding.** Each radio announces `_hasler._tcp`. `haslerd` browses with
  `avahi-browse`, asks each address `GET /api/info` for its `id`, `name` and
  `multiroom` block, and leaves itself out. Results are cached for 30 s; a radio
  not seen for 10 minutes drops off the list.
- **Talking.** Plain JSON over HTTP on port 8080, short timeouts (2 s). There
  are **no tokens between radios**: the home network is trusted, as the key
  page trusts it.
- **The calls** (`daemon/haslerd/web/group_api.py`):

| Call | From → to | What for |
|---|---|---|
| `PUT /api/group` | key page → radio | `{"members": [...]}` (on the leader), `{"enabled": ...}`, `{"follower_keys": "group" \| "leave"}` |
| `POST /api/group/join`, `/leave` | key page or join key → radio | follow a leader (or pick one), leave |
| `POST /api/peer/follow`, `/release` | leader → follower | start or stop following |
| `POST /api/peer/join`, `/left` | follower → leader | a follower joins or leaves by itself |
| `POST /api/peer/key` | follower → leader | a numbered key pressed on a follower |
| `POST /api/peer/handover`, `/handover/abort` | follower → leader | see [Handover](#handover) |

- **Keeping in touch.** A follower reads its leader's `/api/info` every 5 s:
  that drives its lamp, and a leader that no longer counts it releases it.
  Three unanswered reads in a row (~15 s): the leader is gone, red pulses, alone.
  The leader reads each member every 15 s and drops one that follows someone
  else, or that does not answer 8 times in a row (~2 min).
- **Never waiting.** Keys and web requests never wait on the network or on
  audio: the state flips at once (so the lamp answers), and the radio-to-radio
  calls run in order on one worker thread.

## Roles, keys and standby

Each radio is `alone`, `lead` or `follow`. Nothing about a group is kept
across a restart.

- **Forming a group.** "Also play on…" on the leader's front page ticks
  members; the leader asks each one to follow. Or, on another radio, a key held
  with the action **Join another Hasler** (`join_group`) joins a
  leader: one that leads a group, else one that plays alone; among several, the
  one whose music started most recently. The same key again leaves.
- **Keys on a follower** follow the setting **Keys on this radio while it
  follows another** (`follower_keys`):
  - **Control the whole group** (`group`, the default): the key goes to the
    leader, which plays that preset for everyone. "Same key again" is judged
    at the leader, so a radio key pressed again stops the leader, which ends
    the group.
  - **Leave and play alone** (`leave`): the follower leaves and plays the key
    itself.
- **PU, Test on the radio, a phone casting to a follower:** that radio leaves
  the group first.
- **Volume and tone** are always local.
- **Standby on a follower** takes only that radio out; the rest play on.
- **Standby or shutdown on the leader** ends the group. Each follower goes back
  to how it was before it joined: asleep if it was asleep, otherwise quiet and
  ready. The last member leaving ends the lead.

## Handover

In `group` mode, a follower may forward a key whose **source** the leader does
not have (say a Spotify playlist, and Spotify is not set up on the leader). The
leader answers `409 {"reason": "no_source"}`, and the radio where the key was
pressed takes over, if it has that source itself. A stream that fails to play
is not a handover; it is just red pulses.

With F the pressing follower, L the old leader and M the other members:

1. **Prepare.** F asks L `POST /api/peer/handover` with a fresh token. L checks
   F is its member and no other handover is open, holds this one open for 30 s
   (any other key, join or handover meanwhile gets `409 busy`, so two followers
   pressing at once make exactly one new leader) and answers with its members.
   L keeps playing.
2. **Commit, on F.** F becomes leader of M + L, starts Snapcast, plays the preset
   on its own account, then asks each M, and L last, to follow it (with the
   token and `previous: L`). An M only moves while it still follows L; L only
   while that very handover is open. Nobody's lamp breathes: each moves
   straight to playing amber.
3. **Failures.** If F fails before anything moved, it tells L
   `/handover/abort` and is alone with red pulses; L leads on. An M that does
   not move notices L no longer leads it and is alone. If L never commits, its
   members have moved and L drops them at its next check. No step ever leaves a
   radio following two leaders.

The full story, step by step, is the docstring at the top of
`daemon/haslerd/multiroom/group.py`.

## Power cuts

Every radio comes back **alone** (`/run` is empty). A follower comes back
quiet, never playing its own old key and never rejoining by itself:
`state.json` says it was following, so the boot resume plays nothing. The
leader comes back alone and its old followers notice within seconds. To group
again, tick them on the leader's page or hold the join key.

## Room names and casting names

Each radio can have a **room name** (Settings → *This radio*, up to 32
characters), saved in `settings.json` as `"room": {"name": "Kitchen",
"casting_names": false}`. The room name is shown in the multiroom list, at the
top of the key page, and in Home Assistant (as `/api/info`'s `name`; HA keys on
`id`, so renaming is safe).

The host name never changes. The first radio is `hasler.local`; avahi gives a
second one `hasler-2.local` by itself, and the page shows the address really
in use.

**Use the room name on phones** (Settings → *Phones & casting*, off by default) renames the casting targets
to *Hasler Kitchen*: AirPlay, UPnP and Bluetooth through moOde's own Renderers
form, Spotify Connect through go-librespot's `device_name`. moOde's worker takes
one job at a time and can take a minute, so the radio applies them one by one
in the background and the page shows each step. go-librespot reads its name
only at start, so it is restarted at the end (Spotify stops for a moment).
Renaming Spotify needs a release install, which manages go-librespot's config.
Switching it off puts *Hasler* back everywhere. The code is
`daemon/haslerd/web/room.py`.

## Casting

Settings → **Phones & casting** on the key page.

| Target | What plays it | Notes |
|---|---|---|
| **AirPlay** (classic, AirPlay 1) | `shairport-sync`, started by moOde | moOde's trixie repository has no shairport-sync, so `bootstrap.sh` installs Debian's, disables its own unit and puts `config/moode/shairport-sync.conf` in place. moOde starts it as before. |
| **UPnP / DLNA** | `upmpdcli`, started by moOde | Plays through MPD. |
| **Bluetooth** | BlueZ + bluealsa, moOde | Hold **PU**, or *A new phone* → **Pair a phone** on the page, opens pairing for 2 minutes. While it is open, `haslerd` answers yes to the phone's code itself (`player/btaccept.py` through the root helper `hasler-bt-accept`; moOde's agent would wait for a yes on its own web page). A systemd drop-in for `bluetooth.service` makes phones show the radio as a loudspeaker. Paired phones, the codec in use, reconnecting to the last phone and *Phone volume controls the radio* are `/api/bluetooth` (`web/bluetooth_api.py`); the phone's slider is the radio's mixer through `player/btvolume.py`. A drop-in for moOde's `bluealsa-aplay@.service` gives 1 s of buffer instead of 500 ms (Android phones stall for up to ~0.9 s in the first 20 s), and one for Debian's `bluealsa-aplay.service` keeps it from ever running beside moOde's. |
| **Spotify Connect** | go-librespot | Always on, because the same program plays the Spotify keys. The radio's own account is signed in or out on the page with a code ([build guide, Phase 3c](build-guide.md#phase-3c--streaming-sources-spotify--youtube-music); `web/spotify_account_api.py`). **Spotify for guests:** with *Guests on your Wi-Fi can play* on (the default), anyone on the Wi-Fi with Spotify Premium sees *Hasler* in their own app and plays with their own account, no login on the radio. The switch (`spotify_guests` in `settings.json`) keeps go-librespot's `zeroconf_enabled` in step. Off: only the radio's own account sees it. |

moOde owns AirPlay, UPnP and Bluetooth: `haslerd` never starts their units
itself. It reads moOde's flags and names (one read-only sqlite query) and
switches them through moOde's own Renderers form, one at a time
(`daemon/haslerd/player/renderers.py`). A target whose program is not
installed is shown as unavailable, with the reason, and cannot be switched on
(moOde would otherwise restart it every 10 s, forever). moOde's own Spotify
(`spotifysvc`) must stay off: go-librespot is the radio's Spotify.

**Back to the radio when a phone stops** (on by default; `resume_after_cast`)
brings the last key's music back 20 s after a Bluetooth or AirPlay cast ends,
unless someone chose something on the radio meanwhile. Spotify is left alone:
go-librespot reports a pause exactly as it reports an end.

In a group, a phone casting to a **follower** takes that radio out of the group
first. A phone casting to the **leader** is heard on every radio in the group,
like any other source the leader plays.

## Home Assistant

The [Hasler Radio integration](https://github.com/Delgado-Murphy/hasler-homeassistant)
gives Home Assistant one device per radio, named after its room. The owner's
install steps (HACS) are in
[Using your Hasler](using.md#home-assistant). For makers:

- It talks to the radio's own API on port 8080 (no cloud, no account) and
  finds radios through the same `_hasler._tcp` announcement as multiroom.
- Entities: a `media_player` (the preset keys are its sources), an `update`
  entity (read-only: HA can't install; Automatic radios install small
  updates at night by themselves, and majors, *Install now* and *Go back*
  need a press on the radio), a *Health
  problem* binary sensor, and sensors for the update channel and when the
  software started.
- **Not the MPD integration.** HA may offer *Music Player Daemon* for the radio.
  Skip it: the radio drives MPD itself, and a second controller fights it.
- Keys that run HA scenes, scripts or lights are a separate thing, set up from
  the key page; see [build guide, Phase 5](build-guide.md#phase-5--home-assistant).

## Checking and fixing

```sh
sudo hasler-multiroom status          # mode, server/client running, leader
systemctl status snapserver-hasler snapclient-hasler camilladsp-hasler
journalctl -u snapclient-hasler -n 50
cat /run/hasler-multiroom/client.env  # who a follower listens to
curl -s http://hasler.local:8080/api/group | python3 -m json.tool
avahi-browse -rtp _hasler._tcp        # which Haslers this radio can see
```

- **The other radio is not in the list.** Both need multiroom on, the same
  network (no guest Wi-Fi isolation between them), and `avahi-browse` must see
  both. Routers that block mDNS between Wi-Fi and Ethernet need it allowed.
- **"Multiroom isn't installed on this radio (Snapcast is missing)."** Run
  `sudo scripts/bootstrap.sh`.
- **Sound stutters or drifts only while grouped.** Check `journalctl -u
  snapclient-hasler` for re-syncs, and that `camilladsp-hasler` is running if
  the tone DSP is on.
- **Back to normal, by hand:** `sudo hasler-multiroom alone`. A reboot does the
  same.

---

<sub>Delgado &amp; Murphy</sub>
