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

**Tone** (the other knob) changes the treble. It also has a click at the far
left end, like the on/off switch of the old radio:

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
| **Amber, breathing slowly** | Starting up or loading music. Wait a moment. |
| A short **dip** in brightness | The radio heard your key press. |
| **Blue** | A phone is playing through the radio (Bluetooth, AirPlay or Spotify). Brighter when playing, dimmer when paused. |
| **Blue, breathing** | Bluetooth pairing is open. Connect your phone now. |
| **Warm white, breathing** | The radio needs Wi-Fi details. See [Wi-Fi](#wi-fi). |
| **Very dim orange-red** (ember) | Asleep (standby), or installing an update. |
| **Ember, breathing** | Shutting down. Wait. |
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
  This needs a Spotify Premium account.
- **AirPlay** (iPhone, iPad, Mac): choose **Hasler** from the AirPlay list.
- **Bluetooth:** hold **PU** until the lamp breathes blue. On your phone, choose
  **Hasler** in the Bluetooth settings. The next time, the phone connects by
  itself.

While a phone plays, the lamp is blue. A phone can wake the radio from standby.
To take the radio back from the phone, tap any numbered key.

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
| **Home Assistant** | Connect your smart home, so a key can run a scene or a script, switch things, or set lights (brightness, colour, warm or cool white). Optional. |
| **Wi-Fi** | Shows the network. **Change** moves the radio to another network. |
| **Volume ceiling** | The loudest the knob can go (30 to 100). Above 95 the old speaker can distort. |
| **Mono** | Mixes left and right into the one speaker, so nothing is lost. On by default. |
| **moOde player** | Opens the music system inside the radio (for advanced use). |
| **Reset keys to defaults** | Puts the factory stations back on every key. |
| **Pilot lamp** | When the lamp dims for the night: *by the sun*, *fixed hours* or *never*. For "by the sun", choose the city where the radio stands. |
| **Knobs** | Shows each knob live. **Calibrate** starts a short guide: you turn the knob to a few positions and the radio learns them. **Reset to factory** undoes it. |
| **About this radio** | Software version and updates. See [Updates](#updates). |

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
