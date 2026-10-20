# Using your Hasler

This guide is for the person who lives with the radio. You do not need to know
anything about computers to use it. If you want to build one, see the
[build guide](build-guide.md) instead.

Your Hasler is a Swiss radio cabinet from the 1960s. Inside, it has new parts
that play internet radio, Spotify and YouTube Music. From the outside it works
like the old radio: you press a key and music plays.

**Contents**

- [The keys](#the-keys)
- [The two knobs](#the-two-knobs)
- [The pilot lamp](#the-pilot-lamp)
- [Playing from your phone](#playing-from-your-phone)
- [Music in several rooms](#music-in-several-rooms)
- [Home Assistant](#home-assistant)
- [The key page](#the-key-page)
- [Wi-Fi](#wi-fi)
- [Updates](#updates)
- [After a power cut](#after-a-power-cut)
- [When something is wrong](#when-something-is-wrong)

---

## The keys

The radio has six numbered keys and one key marked **PU**.

| You do this | What happens |
|---|---|
| Tap key 1 to 6 | The music on that key starts: a radio station or a playlist. |
| Tap the key that is playing a **radio station** | The radio goes to sleep (standby). |
| Tap the key that is playing a **playlist** | It skips to the next song. |
| Hold key **5** | Volume goes down while you hold it. |
| Hold key **6** | Volume goes up while you hold it. |
| Hold key 1 to 4 | Nothing, unless someone gave it a job (for example a Home Assistant scene). |
| Tap **PU** | Changes the source: radio, then Bluetooth, then AirPlay, then Spotify, then back to radio. Which sources, and in what order, is up to you: **Settings → Sources & casting** on the key page. A source that is switched off is skipped. |
| Hold **PU** | Opens Bluetooth pairing for 2 minutes, so a new phone can connect. |

Any key wakes the radio from standby.

You choose what each key plays on the [key page](#the-key-page). The radio
remembers the last key you pressed, even after you unplug it.

## The two knobs

**Volume** (one knob) works for everything the radio plays: stations,
playlists, and music from your phone.

**Tone** (the other knob) shapes the sound. Out of the box it is a *tilt*:
left is warmer (more bass, less treble), right is brighter, the middle is
flat. You can give it another effect on the key page (**Settings → Knobs**):
treble, bass, loudness (fuller sound when quiet), presence (voices forward),
*1960s radio* (the old radio's voice, blended in as you turn) or stereo width.
**Listen** tries an effect for 30 seconds before you save it. Whatever it does,
the radio turns its own level down to pay for any lift, so the knob never
makes the old speaker work harder - a strong setting can sound a little
quieter; turn the volume up.

The tone knob also has a click at the far left end, like the on/off switch of
the old radio (it keeps it, whatever effect the knob plays):

| You do this | What happens |
|---|---|
| Turn the tone knob left until it clicks | Standby: the music stops and the lamp glows a very dim orange-red. |
| Turn it back on (past the click) | The radio wakes and the last music comes back. |
| Keep it clicked off for **5 seconds** | The radio shuts down. The lamp fades, breathes slowly, then goes dark. |

When the lamp is **dark**, it is safe to unplug the radio. To start it again
after a shutdown, unplug it and plug it back in with the tone knob turned on.

The volume knob never goes louder than the **volume ceiling**. This protects the
original 1964 speaker. You can change the ceiling on the key page (Settings).

## The pilot lamp

The small window on the front has a coloured light. It tells you what the radio
is doing, like the lamp of an old valve radio.

| The lamp shows | It means |
|---|---|
| **Amber, bright** | Playing music from a key. Playlists are a slightly paler amber. |
| **Amber, dim** | On and ready, but quiet. Press a key. |
| **Amber, breathing slowly** | Starting up or loading music, or joining another room's music. Wait a moment. |
| A short **dip** in brightness | The radio heard your key press. On a volume key, a dip means it is as loud (or as quiet) as it goes. |
| A short **flash of colour**, then back | The **PU** key changed the source: amber for the radio and your playlists, blue for a phone (dim blue while it waits for one). **Magenta** means another Hasler: this radio joined or left a group. |
| **Blue** | A phone is playing through the radio (Bluetooth, AirPlay or Spotify). Brighter when playing, dimmer when paused. |
| **Blue, breathing** | Bluetooth pairing is open. Connect your phone now. One brief swell when a phone has connected. |
| **Warm white, breathing** | The radio needs Wi-Fi details. See [Wi-Fi](#wi-fi). |
| **Very dim orange-red** (ember) | Asleep (standby). |
| **Ember, breathing** | Busy: shutting down, or installing an update. Wait, and do not unplug it. |
| **Dark** | Off. Safe to unplug. |
| **Three red pulses** | Something you asked for did not work (for example, a station is down). |
| **Red, breathing softly** | The radio has had no internet for more than a minute. |

At night (from sunset to sunrise) the lamp is dimmer and moves more slowly, and
it does not show the "no internet" red. You can change this on the key page.

The full design of the lamp is in [lamp.md](lamp.md).

## Playing from your phone

The radio also works as a speaker for your phone. Its name on the network is
**Hasler**.

- **Spotify:** in the Spotify app, tap the speaker icon and choose **Hasler**.
  This needs a Spotify Premium account. Guests on your Wi-Fi can do the same
  with their own Spotify, no password needed. To allow only the radio's own
  account, switch off **Guests on your Wi-Fi can play** (Settings → **Sources
  & casting**).
- **AirPlay** (iPhone, iPad, Mac): choose **Hasler** from the AirPlay list.
- **Bluetooth:** hold **PU** until the lamp breathes blue (or tap **A new
  phone** on the key page). On your phone, choose **Hasler** in the Bluetooth
  settings. The next time, the phone connects by itself, or the radio
  calls it (see [Bluetooth](#bluetooth)).
- **UPnP / DLNA** (music apps on Android, Windows and many NAS boxes): choose
  **Hasler** as the player.

While a phone plays, the lamp is blue. A phone can wake the radio from standby.
To take the radio back from the phone, tap any numbered key. When the phone
stops, the radio goes back to its own music after about 20 seconds (you can
switch that off: **Back to the radio when a phone stops**).

Which of these are switched on, and the name each one shows, is in
**Settings → Sources & casting**, under *Play from a phone*.

### Bluetooth

- **A Bluetooth key.** On the key page, set a key to **Bluetooth**. A tap
  switches the radio to Bluetooth and calls the phone you used last (then the
  one before, if that one is away). The lamp is dim blue while it waits.
  While your phone plays, the same key tapped again skips to the **next
  track**, and held it **pauses** or plays again. Choose what *again* and
  *hold* do (next, previous, pause, or nothing) under **Bluetooth** on the
  key page; the hold's own action is used whenever no phone plays.
- **Back to the phone.** When the **PU** key lands on Bluetooth, the radio
  calls your last phone too. If the radio went to standby while on
  Bluetooth, waking it goes back to Bluetooth and calls the phone, instead of
  playing a station. Both can be switched off (**Reconnect**).
- **Your phones.** The key page lists the paired phones: which one is
  connected, its battery if the phone tells, and when it was last used.
  **Connect**, **Disconnect** or **Forget** (removes the pairing; pair it
  again with **A new phone**).
- **Sound quality.** While a phone plays, the page shows the codec it uses
  (SBC, aptX or aptX HD) and the sample rate. Which one is up to the phone;
  on Android it is under *Developer options → Bluetooth audio codec*.

**More than one Hasler?** Give each one a room name (Settings → **About this
radio**) and switch on **Use the room name for casting**: phones then show
*Hasler Kitchen*, *Hasler Study* and so on, instead of several *Hasler*s.
Renaming takes a minute or two (the page shows each one as it goes), and
Spotify on the radio stops for a moment while it takes the new name. Switch it
off to go back to plain *Hasler*.

## Music in several rooms

Two or more Haslers on the same Wi-Fi can play the same music at the same
time, in step. One radio **leads**: what it plays is what everyone hears. The
others **follow**. This is off until you switch it on (key page, Settings →
**Multiroom** → *Play in several rooms*) on every radio that should take
part. Each radio needs a [room name](#playing-from-your-phone) so you can
tell them apart.

**To start:** on the leading radio's front page, tap **Also play on…** and tick
the other radios (for example *Kitchen*, *Study*). They start playing within a
few seconds. Untick a radio to give it back its own music.

While radios play together, the music starts about **1 second** after you
press a key. That small wait is what keeps them in step.

**Or from the other room:** give a key's *hold* the action **Join another
Hasler** (Edit keys). Holding it joins the radio that leads a group, or
else the radio that is playing. If several are, it joins the one that started
its music most recently. Hold it again to leave.

While a radio follows:

- Its lamp breathes while it joins, then shows the leader's amber (pale for a
  playlist). Three red pulses mean the leader could not be reached; the radio
  is then on its own again.
- **Numbered keys** change the music for the whole group: the leader plays the
  station or playlist of the key you pressed. The same radio key again stops
  the music everywhere (the leader goes to standby). In Settings → Multiroom
  you can choose instead that a key takes *that* radio out of the group and
  plays the station there alone.
- **Volume and tone** stay with each radio.
- **Standby** on a follower: only that radio sleeps and leaves the group. The
  others keep playing.
- **Standby on the leader** ends the group. Each other radio goes back to
  how it was before it joined: asleep if it was asleep, otherwise quiet and
  ready.
- **PU**, **Test on the radio**, or a phone casting to a follower also take that
  radio out of the group first.

**When the leader can't play a key:** if you press a key on a follower whose
music the leader has no way to play (for example a Spotify playlist, and
Spotify is not set up on the leader), the radio you pressed takes over. It
plays the playlist on *its* Spotify account for the whole group, and the old
leader and the other radios follow it. Nobody leaves the group. The lamp on
your radio breathes, then plays; its key page says "Kitchen is now playing for
the group". If the takeover does not work, your radio shows three red pulses
and is on its own; the others keep playing as before. A station that is down
is no takeover, just red pulses. With *Leave and play alone* chosen in
Settings, the radio leaves and plays the key on its own, as before.

**After a power cut** every radio comes back on its own. A radio that was
following comes back quiet (dim amber), not playing; it does not join again by
itself. Tick it again on the leader's page, or hold its join key.

**Phones in a group:** a phone playing to the leading radio is heard in every
room. A phone playing to a following radio takes just that radio out of the
group.

How it works inside is in [multiroom.md](multiroom.md).

## Home Assistant

The radio works perfectly well without Home Assistant. If you have it, there
are two separate things you can do.

**1. See and control the radio from Home Assistant.** Install the **Hasler
Radio** integration once, with HACS:

1. In Home Assistant, open **HACS**, tap the **⋮** menu (top right) →
   **Custom repositories**.
2. Paste `https://github.com/Delgado-Murphy/hasler-homeassistant`, choose the
   type **Integration**, and press **Add**.
3. Search HACS for **Hasler Radio**, open it and press **Download**.
4. Restart Home Assistant (Settings → System → Restart).
5. Go to **Settings → Devices & services**. Your radio shows up as
   **Discovered**: press **Add**. (If it does not, press **Add integration**,
   choose **Hasler Radio** and type `hasler.local`, port `8080`.)

Each radio becomes a device named after its room, with a media player (the
radio's keys are its sources: choosing one presses that key), the volume,
standby, what is playing, and whether an update is waiting. Installing an
update still needs a press on the radio, as always.

Do **not** add Home Assistant's *Music Player Daemon* integration for the
radio, even if it offers one: it fights the radio.

**2. Let a key on the radio run something in your home** (a scene, a light, a
script): connect Home Assistant from the radio's key page, **Settings → Home
Assistant**, then give a key a job in **Edit keys**. The person who built the
radio can help with the first connection; the steps are in the
[build guide](build-guide.md#phase-5--home-assistant).

## The key page

The key page is a web page that controls the radio. Open it on a phone or
computer that is on the **same Wi-Fi** as the radio:

**http://hasler.local:8080**

(If that does not open, try the radio's IP address from your router, with
`:8080` at the end.)

### The front screen

It looks like the radio. You can:

- tap a key to play it;
- turn the volume knob on the screen;
- put the radio in standby, or skip to the next song;
- see what is playing and the tone setting.

### Browse music

Tap **Browse music** to see YouTube Music suggestions and to search for radio
stations ("More stations"). Press play to listen now, or **+** to put it on a
key.

### Edit keys

Tap the pencil (**Edit keys**), then choose a key.

1. Choose a source: **Radio**, **Spotify** or **YouTube Music**.
2. Search for a station, song, artist, album or playlist, or paste a link.
   For Spotify, in the Spotify app use *Share → Copy link*.
3. Turn on **Shuffle** if you want playlists in random order.
4. Press **Test on the radio** to hear it.
5. Press **Save**.

You can also change what **holding** a key does, for example a Home Assistant
scene, or setting lights: which lights, on or off, how bright, what colour or
how warm a white, and how slowly to fade.

### Settings

Tap the gear icon.

| Setting | What it does |
|---|---|
| **YouTube Music** | Sign in to use your Liked songs and private playlists. The page shows the steps. |
| **Home Assistant** | Connect your smart home, so a key can run a scene or a script, switch things, or set lights (brightness, colour, warm or cool white). Optional. To see the radio *in* Home Assistant, see [Home Assistant](#home-assistant). |
| **Wi-Fi** | Shows the network. **Change** moves the radio to another network. |
| **Volume ceiling** | The loudest the knob can go (30 to 100). Above 95 the old speaker can distort. |
| **Mono** | Mixes left and right into the one speaker, so nothing is lost. On by default. |
| **moOde player** | Opens the music system inside the radio (for advanced use). |
| **Reset keys to defaults** | Puts the factory stations back on every key. |
| **Sources & casting** | What the **PU** key steps through, and in what order. Under *Play from a phone*: switch AirPlay, Bluetooth and UPnP on or off, see the name each one shows, open Bluetooth for **A new phone**, and allow Spotify for guests. **Back to the radio when a phone stops** brings your music back when a phone is done. |
| **Multiroom** | **Play in several rooms** lets this radio play together with other Haslers (off by default). *Keys on this radio while it follows another*: **Control the whole group** (default), or **Leave and play alone**. See [Music in several rooms](#music-in-several-rooms). |
| **Pilot lamp** | When the lamp dims for the night: *by the sun*, *fixed hours* or *never*. For "by the sun", choose the city where the radio stands. |
| **Knobs** | Shows each knob live. **Calibrate** starts a short guide: you turn the knob to a few positions and the radio learns them. **Reset to factory** undoes it. Each knob except the volume can also be given an **effect** (see [The two knobs](#the-two-knobs)). |
| **About this radio** | The radio's **room name** (for example *Kitchen*): shown in the multiroom list, in Home Assistant and at the top of this page. Its address (*hasler.local*) stays the same; the page shows it under the name. A second Hasler on the same Wi-Fi gets *hasler-2.local* by itself. **Use the room name for casting** makes phones see *Hasler Kitchen* (off: just *Hasler*). Also the software version and updates; see [Updates](#updates). |

### "Press any key on the radio"

Some buttons can change a lot: installing an update, going back to an older
version, changing Wi-Fi, resetting the keys. For these, the page asks you to
**press any key on the radio within 30 seconds**. This proves that the person
is standing next to the radio, and not just someone else on the Wi-Fi.

## Wi-Fi

### First time, or a new home

If the radio cannot find a Wi-Fi network it knows:

1. Plug it in and wait about 2 minutes. The lamp starts to **breathe warm white**.
   The radio has made its own small Wi-Fi network called **Hasler**.
2. On your phone, join the Wi-Fi network **Hasler**. The person who built the
   radio can give you its password.
3. A page opens by itself. If it does not, open **http://172.24.1.1:8080/wifi**.
4. Choose your home network, type its password and press **Connect**.
5. The radio restarts. After about a minute, put your phone back on your home
   Wi-Fi. The music comes back by itself.

If the lamp breathes white again, the password was wrong. Join **Hasler** again
and try once more.

The radio only makes the **Hasler** network when it starts. If your Wi-Fi stops
working later, the radio keeps trying it. To get the setup network, unplug the
radio and plug it in again.

### Changing networks at home

On the key page: **Settings → Wi-Fi → Change**. Choose the new network, then
press a key on the radio when the page asks.

## Updates

The makers sometimes publish a new version of the radio's software (a
**release**). The radio can install it by itself. Your keys, settings and knob
calibration are never changed by an update.

You find everything in **Settings → About this radio**.

| Choice | What it means |
|---|---|
| **Automatic** (the default) | Small updates install by themselves at night, while the radio is in standby. Bigger updates (a new major version, or one that has to move your settings) still ask first. |
| **Ask** | The page tells you when there is an update. You choose **Install tonight** (between 2:00 and 5:00) or **Install now**. |
| **Off** | No updates. Fixes will not reach the radio. |

### Update channel

Below the update choice is **Update channel**. There are two:

| Channel | Who it is for |
|---|---|
| **Stable · recommended** | Every radio. Each version is tried on the makers' test radio first. |
| **Bench** | The makers' test radio. It gets test versions before anyone else, and they may break. |

Leave your radio on **Stable**. To switch, tap the other channel, then press a
key on the radio when the page asks, so nobody else on the Wi-Fi can do it. If
you go from Bench back to Stable, the radio keeps the version it has until
Stable catches up; it never goes back to an older version on its own.

Good to know:

- An update only installs while the radio is in standby, never during music.
- While it installs, the lamp stays **ember**. Leave the radio plugged in.
- After the update, the radio checks itself. If something does not work, it
  goes back to the previous version by itself, even after a power cut. The page
  then says "didn't work here" and why.
- **To go back by hand:** under *About this radio*, find the older version and
  press **Go back**. Then press a key on the radio. The radio restarts on that
  version.

You can read what changed in each version on the
[releases page](https://delgado-murphy.github.io/hasler-releases/) or in the
[changelog](../CHANGELOG.md).

## After a power cut

- When the power comes back, the lamp breathes amber while the radio warms up.
  After about 15 seconds the music you were playing comes back.
- If the power comes back **at night**, the radio stays asleep (ember) so it
  does not wake anyone. Press a key in the morning.
- If the tone knob is clicked off, the radio stays asleep.
- A radio that was following another room's music comes back quiet, on its
  own (see [Music in several rooms](#music-in-several-rooms)).
- If the music cannot come back (for example, no internet), you see three red
  pulses and then dim amber. Press a key to try again.

## When something is wrong

**There is no sound.**

1. Look at the lamp. Ember means standby: press a key or turn the tone knob on.
2. Turn the volume knob up. Check the volume ceiling on the key page.
3. Blue means a phone has the radio. Stop it on the phone, or tap a numbered key.
4. Three red pulses mean the station or playlist did not start. Try another
   key. If one key never works, choose new music for it on the key page.

**The key page does not open.**

- Check that your phone is on the same Wi-Fi as the radio (not on mobile data).
- Wait one minute after the radio starts.
- Try the radio's IP address instead of `hasler.local`.
- If the lamp breathes warm white, the radio is not on your Wi-Fi. See [Wi-Fi](#wi-fi).

**The lamp breathes red.** The radio has no internet. Check your router. Music
from a phone over Bluetooth still works.

**Stations work, but YouTube Music or Spotify do not.**

- YouTube Music: if you signed in, the sign-in may have expired after some
  months. Sign in again in Settings.
- Spotify on a key needs a Premium account that was set up by the builder.

**Another Hasler is missing from "Also play on…".** Switch on **Play in
several rooms** on *both* radios (Settings → Multiroom), and check that both
are on the same Wi-Fi (not a guest network). Give it half a minute, then open
the list again.

**My phone shows several "Hasler"s.** Give each radio a room name and switch
on **Use the room name for casting** (Settings → About this radio).

**A knob feels wrong** (the volume jumps, or does not reach the top). Use
**Settings → Knobs → Calibrate**.

**An update made something worse.** Go to **Settings → About this radio** and
press **Go back** next to the previous version.

**Nothing helps.** Shut the radio down (tone knob off for 5 seconds), wait until
the lamp is dark, unplug it for 10 seconds and plug it in again. If the problem
stays, tell the person who built your radio. What the lamp shows, and the
version number in *About this radio*, help them a lot.

---

<sub>Delgado &amp; Murphy</sub>
