# Using your Hasler

This guide is for the person who lives with the radio. You don't need to know
anything about computers. (Building one? See the [build guide](build-guide.md).)

Your Hasler is a Swiss radio cabinet from the 1960s with new parts inside. It
plays internet radio, Spotify and YouTube Music, and it is a speaker for your
phone. From the outside it works like the old radio: press a key, and music
plays.

**Contents**

- [The first ten minutes](#the-first-ten-minutes)
- [Everyday use](#everyday-use): [the keys](#the-keys), [the knobs](#the-two-knobs), [the pilot lamp](#the-pilot-lamp)
- [Playing from your phone](#playing-from-your-phone)
- [Music in several rooms](#music-in-several-rooms)
- [The key page and Settings](#the-key-page)
- [Home Assistant](#home-assistant)
- [Wi-Fi](#wi-fi)
- [Updates](#updates)
- [After a power cut](#after-a-power-cut)
- [When something is wrong](#when-something-is-wrong)

---

## The first ten minutes

1. **Plug it in.** The pilot lamp (the little window on the front) breathes
   amber while the radio warms up. After about 15 seconds the music comes
   back, or the lamp settles to a dim amber: the radio is ready.
   If the lamp **breathes warm white** instead, the radio needs your Wi-Fi
   details first: see [Wi-Fi](#wi-fi).
2. **Press key 1.** Music starts. Turn the **volume** knob to taste.
3. **Try the other keys.** Each of the six numbered keys plays its own station
   or playlist. Press the key of the station that is playing again, and the
   radio goes to sleep.
4. **Open the key page.** On a phone or computer on the same Wi-Fi, open
   **http://hasler.local:8080**. Tap the pencil (**Edit keys**) to put your
   own stations and playlists on the keys.
5. **Play from your phone.** In Spotify or AirPlay, choose **Hasler** as the
   speaker. For Bluetooth, hold the **PU** key until the lamp breathes blue,
   then choose **Hasler** on your phone.
6. **Switch it off.** Turn the **tone** knob all the way left until it clicks:
   the radio sleeps (a dim ember glow). Keep it clicked off for 5 seconds and
   it shuts down. When the lamp is **dark**, it is safe to unplug.

That's all you need. The rest of this guide is for when you want more.

## Everyday use

### The keys

The radio has six numbered keys and one key marked **PU**.

| You do this | What happens |
|---|---|
| Tap key 1 to 6 | That key's music starts: a radio station or a playlist. |
| Tap the key that is playing a **station** | The radio goes to sleep (standby). |
| Tap the key that is playing a **playlist** | It skips to the next song. |
| Hold key **5** | Volume goes down while you hold it. |
| Hold key **6** | Volume goes up while you hold it. |
| Hold key 1 to 4 | Nothing, unless you gave it a job (for example a Home Assistant scene). |
| Tap **PU** | Changes the source: radio, then Bluetooth, then AirPlay, then Spotify, then back to the radio. Sources that are switched off or not set up are skipped. |
| Hold **PU** | Opens Bluetooth pairing for 2 minutes, so a new phone can connect. |

A tap on any key wakes the radio from standby. The radio remembers the last
key you pressed, even after you unplug it.

You choose what each key plays, and what holding it does, in
[Edit keys](#edit-keys). What **PU** steps through, and in what order, is in
**Settings → Keys & knobs**.

### The two knobs

**Volume** works for everything the radio plays: stations, playlists and your
phone. It never goes louder than the **volume ceiling**, which protects the
original 1964 speaker (**Settings → Sound**).

**Tone** shapes the sound. Out of the box it is a *tilt*: left is warmer (more
bass, less treble), right is brighter, the middle is flat. You can give it a
different job in **Settings → Sound → What the tone knob does**: treble, bass,
loudness (fuller sound when quiet), presence (voices forward), *1960s radio*
(the old radio's voice, blended in as you turn), stereo width, or nothing.
**Try it** plays your choice for 30 seconds before you save it.

Whatever the tone knob does, it never makes the old speaker work harder: when
it lifts part of the sound, the radio turns everything else down a little. A
strong setting can sound a touch quieter; just turn the volume up.

**The tone knob is also the on/off switch**, like the old radio's:

| You do this | What happens |
|---|---|
| Turn the tone knob left until it clicks | Standby: the music stops and the lamp glows a dim ember (deep orange-red). |
| Turn it back past the click | The radio wakes and the last music comes back (or your phone, if it was on Bluetooth). |
| Keep it clicked off for **5 seconds** | The radio shuts down. The lamp fades, breathes slowly, then goes dark. |

When the lamp is **dark**, it is safe to unplug. To start the radio again after
a shutdown, unplug it and plug it back in with the tone knob turned on.

### The pilot lamp

The lamp tells you what the radio is doing, like the lamp of an old valve
radio. In short: **amber** is the radio, **blue** is a phone, **ember** is
asleep, **dark** is safe to unplug.

| The lamp shows | It means |
|---|---|
| **Amber, bright** | Playing music from a key. Playlists are a slightly paler amber. |
| **Amber, dim** | On and ready, but quiet. Press a key. |
| **Amber, breathing** | Starting up, loading music, or joining another room's music. Wait a moment. |
| A short **dip** | The radio heard your key. On a volume key, a dip at the end means it is as loud (or as quiet) as it goes. |
| A short **glow of colour**, then back | After **PU**: where it went. Amber for the radio, pale amber for a playlist, blue for a phone (a dim blue while it waits for one). |
| A short **glow of magenta** | Another Hasler: this radio joined or left a group. |
| **Blue** | A phone is playing through the radio (Bluetooth, AirPlay or Spotify). Brighter when playing, dimmer when paused. |
| **Blue, breathing** | Bluetooth pairing is open: connect your phone now. One soft swell when it has connected. |
| **Warm white, breathing** | The radio needs Wi-Fi details. See [Wi-Fi](#wi-fi). |
| **Ember** (dim deep orange-red) | Asleep (standby). |
| **Ember, breathing** | Busy: shutting down or installing an update. Leave it plugged in. |
| **Dark** | Off. Safe to unplug. |
| **Three red pulses** | Something you asked for didn't work (for example, a station is down). |
| **Red, breathing softly** | No internet for more than a minute. |

Turning the knobs never changes the lamp: your ears are the feedback. Music
from other music apps (UPnP) shows amber for now, not blue.

From sunset to sunrise the lamp is dimmer and moves more slowly, and it
doesn't show the "no internet" red. To use fixed hours instead, or never dim,
see **Settings → Pilot lamp**.

Every colour in detail: [The pilot lamp](lamp.md).

## Playing from your phone

The radio is also a speaker for your phone. On the network it is called
**Hasler**.

- **Spotify:** in the Spotify app, tap the speaker icon and choose **Hasler**.
  This needs Spotify Premium. Guests on your Wi-Fi can do the same with their
  own Spotify, no password needed. To allow only the radio's own account,
  switch off **Guests on your Wi-Fi can play**.
- **AirPlay** (iPhone, iPad, Mac): choose **Hasler** from the AirPlay list.
- **Bluetooth:** hold **PU** until the lamp breathes blue (or tap **Pair a new
  phone** on the key page), then choose **Hasler** in your phone's Bluetooth
  settings. After that, the phone connects by itself, or the radio calls it
  (see [Bluetooth](#bluetooth)).
- **Other music apps** (UPnP / DLNA, on Android, Windows and many NAS boxes):
  choose **Hasler** as the player.

While a phone plays, the lamp is blue. A phone can wake the radio from
standby. To take the radio back, tap any numbered key.

When a Bluetooth or AirPlay phone stops, the radio goes back to its own music
after about 20 seconds, unless you chose something else on the radio in the
meantime, or the phone had woken it from standby (then it stays quiet). To
switch this off: **Back to the radio when a phone stops**.

All of this is in **Settings → Phones & casting**: each way of playing on or
off, your Bluetooth phones, and the names phones see.

### Bluetooth

- **Your phones.** **Settings → Phones & casting → Bluetooth** lists the paired
  phones: which one is connected, its battery (if the phone tells), and when
  it was last used. **Connect**, **Disconnect**, or **Forget** (pair it again
  later with **Pair a new phone**).
- **Sound quality.** While a phone plays, the same place shows the codec it
  uses (SBC, aptX or aptX HD) and the sample rate. The phone chooses; on
  Android it is under *Developer options → Bluetooth audio codec*.
- **The radio calls your phone.** When you switch to Bluetooth (with PU or a
  Bluetooth key), the radio connects the phone you used last, or the one
  before if that one is away. The lamp is dim blue while it waits. And if the
  radio went to sleep on Bluetooth, waking it goes back to Bluetooth and calls
  the phone, instead of playing a station. Both are switches under
  **Reconnect to the last phone** (*When I switch to Bluetooth* and *When the
  radio wakes up*), in **Settings → Phones & casting → Bluetooth**. Both are
  on to start.
- **A Bluetooth key.** In **Edit keys**, choose a key and set its tap to
  **Bluetooth**. Tapping it switches the radio to Bluetooth and calls your
  phone. While your phone plays, tapping it again skips to the **next track**,
  and holding it **pauses** (hold again to play). Under **While Bluetooth
  plays**, on that key in Edit keys, you can set *Press again* or *Hold* to do
  nothing instead; these apply at once. When no phone is playing, holding the
  key does its usual hold job.

### One name per room

If you have more than one Hasler, give each a room name (**Settings → This
radio**) and switch on **Use the room name on phones** (**Settings → Phones &
casting**). Phones then show *Hasler Kitchen*, *Hasler Study* and so on,
instead of several *Hasler*s. Renaming takes a minute or two (the page shows
each step), and Spotify on the radio stops for a moment while it takes the
new name. Switch it off to go back to plain *Hasler*.

## Music in several rooms

Two or more Haslers on the same Wi-Fi can play the same music, in step. One
radio **leads**: what it plays, everyone hears. The others **follow**.

**Switch it on first**, on every radio that should take part: **Settings →
Rooms → Play in several rooms** (it is off to start). A radio without a room
name is asked for one there, so you can tell the radios apart.

**To start a group:** on the leading radio's front page, tap **Also play on…**
and tick the other radios (*Kitchen*, *Study*). They join within a few
seconds. Untick a radio to give it back its own music.

**Or from the other room:** in **Edit keys**, give a key's *hold* the job
**Join another Hasler**. Holding it joins the radio that leads a group, or
else the one that is playing (if several are, the one that started most
recently). Hold it again to leave.

While radios play together, music starts about **1 second** after you press a
key. That small wait keeps them in step.

**While a radio follows:**

- Its lamp breathes while it joins, then shows the leader's amber (pale for a
  playlist). Three red pulses mean the leader couldn't be reached; the radio
  is then on its own again.
- **Numbered keys** change the music for the whole group: the leader plays
  the key you pressed. The playing station's key again stops the music
  everywhere. If you'd rather a key took just *this* radio out of the group to
  play on its own, choose **Leave and play alone** in **Settings → Rooms**
  (*Keys on this radio while it follows another*).
- **Volume and tone** stay with each radio.
- **Standby on a follower** puts only that radio to sleep; the others play on.
- **Standby on the leader** ends the group. Each other radio goes back to how
  it was before it joined: asleep if it was asleep, otherwise quiet and ready.
- **PU**, **Test on the radio**, or a phone playing to a follower take that
  radio out of the group first. A phone playing to the **leader** is heard in
  every room.

**When the leader can't play a key.** Say you press a Spotify key on a
follower, and the leader has no Spotify set up. Then the radio you pressed
takes over: it plays the playlist on its own account, and the rest of the
group follows it. Nobody leaves the group; the key page says, for example,
"Kitchen is now playing for the group". If that doesn't work, your radio
shows three red pulses and is on its own, and the others play on. (A station
that is simply down gives red pulses, no takeover.)

**After a power cut** each radio comes back on its own. One that was
following comes back quiet (dim amber) and doesn't rejoin by itself: tick it
again on the leader's page, or hold its join key.

How it works inside: [multiroom.md](multiroom.md).

## The key page

The key page is a web page that controls the radio. Open it on a phone or
computer on the **same Wi-Fi** as the radio:

**http://hasler.local:8080**

(If that doesn't open, try the radio's IP address from your router, with
`:8080` at the end. A second Hasler on the same network gets
`hasler-2.local` by itself; **Settings → This radio** shows each radio's
address.)

### The front screen

It looks like the radio. Tap a key to play it, turn the volume on the screen,
put the radio to sleep or skip to the next song, and see what is playing.
**Also play on…** is here too (see [several rooms](#music-in-several-rooms)).

### Browse music

Tap **Browse music** for YouTube Music suggestions and to search for radio
stations (**More stations**). Press play to listen now, or **+** to put it on
a key.

### Edit keys

Tap the pencil (**Edit keys**), then choose a key.

1. Choose a source: **Radio**, **Spotify** or **YouTube Music**.
2. Search for a station, song, artist, album or playlist, or paste a link.
   For Spotify, use *Share → Copy link* in the Spotify app.
3. Turn on **Shuffle** if you want a playlist in random order.
4. Press **Test on the radio** to hear it.
5. Press **Save**.

You can also choose what **holding** a key does: for example a Home Assistant
scene, lights (which ones, on or off, how bright, what colour, how slowly to
fade), or **Join another Hasler**. A key can also be a
[Bluetooth key](#bluetooth).

**Reset keys to defaults** (the factory stations back on every key) is at the
bottom of Edit keys.

### Settings

Tap the gear. Each row opens its own page.

| Row | What you find there |
|---|---|
| **Sound** | The **volume ceiling**, the loudest the knob can go (30 to 100; above 95 the old speaker can rattle and distort). The **quietest step**: the knob's first click. **Mono**: left and right mixed into the one speaker, so nothing is lost (on to start). **What the tone knob does**, with **Try it**. |
| **Phones & casting** | Spotify (and **Guests on your Wi-Fi can play**), AirPlay, Bluetooth and other music apps (UPnP), each on or off. Bluetooth: what is playing and its codec, your phones, **Pair a new phone**, **Reconnect to the last phone**. **Back to the radio when a phone stops**. **Use the room name on phones**. |
| **Rooms** | **Play in several rooms**, what the keys do while this radio follows another, and which key joins from this radio. |
| **Pilot lamp** | When the lamp dims at night (*by the sun*, *fixed hours* or *never*), and **where the radio stands**, so it knows when the sun sets. |
| **Keys & knobs** | **What the source key (PU) steps through**, and in what order. Each knob, live, with **Calibrate**: turn the knob to a few positions and the radio learns them. |
| **This radio** | The **room name** and the radio's address. **Wi-Fi**. **Updates**. **Home Assistant**. Your **YouTube Music** account (sign in for your Liked songs and private playlists; the page shows how). |

The version number is at the bottom of Settings.

**How changes are kept.** A single switch or slider works at once, and a small
**Undo** appears for a few seconds in case you didn't mean it. Where you fill
in several things, a bar shows **Not saved** with **Cancel** and **Save**;
nothing changes until you press **Save**.

**Maker tools.** There is one more page, for the person who built the radio:
lamp colours, knob curves, the player underneath (moOde) and diagnostics. It
stays hidden; tap the version number seven times to show it. You won't need it.

### "Press any key on the radio"

Some buttons change a lot: installing an update, going back to an older
version, changing the update channel, changing Wi-Fi, resetting the keys. For
these the page asks you to **press any key on the radio within 30 seconds**.
That proves someone is standing next to the radio, not just anyone on the
Wi-Fi.

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
   **Discovered**: press **Add**. (If it doesn't, press **Add integration**,
   choose **Hasler Radio** and type `hasler.local`, port `8080`.)

Each radio becomes a device named after its room, with a media player (the
radio's keys are its sources: choosing one presses that key), the volume,
standby, what is playing, and whether an update is waiting. Installing an
update still needs a press on the radio itself.

Do **not** add Home Assistant's *Music Player Daemon* integration for the
radio, even if it offers one: it fights the radio.

**2. Let a key on the radio run something in your home** (a scene, a light, a
script): connect Home Assistant in **Settings → This radio → Home Assistant**,
then give a key's hold a job in **Edit keys**. The person who built the radio
can help with the first connection; the steps are in the
[build guide](build-guide.md#phase-5--home-assistant).

## Wi-Fi

### First time, or a new home

If the radio can't find a Wi-Fi network it knows:

1. Plug it in and wait about 2 minutes. The lamp **breathes warm white**: the
   radio has made its own small Wi-Fi network called **Hasler**.
2. On your phone, join the Wi-Fi network **Hasler**. The person who built the
   radio can give you its password.
3. A page opens by itself. If it doesn't, open **http://172.24.1.1:8080/wifi**.
4. Choose your home network, type its password and press **Connect**.
5. The radio restarts. After about a minute, put your phone back on your home
   Wi-Fi. The music comes back by itself.

If the lamp breathes warm white again, the password was wrong: join **Hasler**
again and try once more.

The radio only makes the **Hasler** network when it starts. If your Wi-Fi
drops out later, the radio keeps trying it; to get the setup network, unplug
the radio and plug it in again.

### Changing networks at home

**Settings → This radio → Wi-Fi → Change**. Choose the new network, then press
a key on the radio when the page asks.

## Updates

Now and then the makers publish a new version of the radio's software. The
radio can install it by itself. Your keys, settings and knob calibration are
never changed by an update.

Everything is in **Settings → This radio → Updates**.

| Choice | What it means |
|---|---|
| **Automatic** (to start) | Small updates install by themselves at night, while the radio is in standby. Bigger ones (a new major version, or one that has to move your settings) ask first. |
| **Ask** | The page tells you when there is an update. Choose **Install tonight** (between 2:00 and 5:00) or **Install now**. |
| **Off** | Still looks for updates (the page tells you), but never downloads or installs one. Fixes won't reach the radio. |

Good to know:

- An update only installs while the radio rests: in standby, with no phone
  playing, and nothing played for 10 minutes. **Install now** starts at the
  first such moment, so put the radio to sleep after you press it.
- While it installs (two or three minutes), the lamp **breathes ember**. Leave
  the radio plugged in.
- Afterwards the radio checks itself. If something doesn't work, it goes back
  to the previous version by itself, even after a power cut, and the page says
  "Didn't work here" and why.
- **To go back by hand:** under Updates, find the older version, press **Go
  back**, then press a key on the radio. The radio restarts on that version.

**Update channel.** Every radio is on **Stable**: each version is tried on the
makers' own radio first. The other channel, **Test versions** (*Bench*), is
for the makers' test radio; its versions come first and may break. The choice
only shows on the Updates page when [Maker tools](#settings) are on, because
you won't need it. Switching asks for a press on the radio. Going back from
Test versions to Stable, the radio keeps the version it has until Stable
catches up; it never goes back to an older version on its own.

What changed in each version: the
[releases page](https://delgado-murphy.github.io/hasler-releases/) or the
[changelog](../CHANGELOG.md).

## After a power cut

- When the power comes back, the lamp breathes amber while the radio warms up.
  After about 15 seconds the music you were playing comes back.
- If the power comes back **at night**, the radio stays asleep (ember) so it
  doesn't wake anyone. Press a key in the morning.
- If the tone knob is clicked off, the radio stays asleep.
- A radio that was following another room comes back quiet, on its own.
- If the music can't come back (no internet, say), you see three red pulses,
  then dim amber. Press a key to try again.

## When something is wrong

**There is no sound.**

1. Look at the lamp. Ember means asleep: press a key or turn the tone knob on.
2. Turn the volume knob up, and check the volume ceiling (**Settings → Sound**).
3. Blue means a phone has the radio. Stop it on the phone, or tap a numbered key.
4. Three red pulses mean the music didn't start. Try another key. If one key
   never works, choose new music for it in **Edit keys**.

**The key page doesn't open.**

- Check that your phone is on the same Wi-Fi as the radio (not mobile data,
  not a guest network).
- Wait a minute after the radio starts.
- Try the radio's IP address instead of `hasler.local`.
- If the lamp breathes warm white, the radio isn't on your Wi-Fi: see
  [Wi-Fi](#wi-fi).

**The lamp breathes red.** The radio has no internet. Check your router.
Bluetooth from a phone still works.

**Stations work, but YouTube Music or Spotify don't.**

- YouTube Music: if you signed in, the sign-in may have expired after some
  months. Sign in again in **Settings → This radio → YouTube Music**.
- Spotify on a key needs a Premium account set up on the radio by the person
  who built it.

**My phone won't connect over Bluetooth.** Check that Bluetooth is on in
**Settings → Phones & casting**. If the phone is listed but won't connect,
**Forget** it there, then hold **PU** and pair it again.

**Another Hasler is missing from "Also play on…".** Switch on **Play in
several rooms** on *both* radios (**Settings → Rooms**), and check both are on
the same Wi-Fi (not a guest network). Give it half a minute, then open the
list again.

**My phone shows several "Hasler"s.** Give each radio a room name and switch
on **Use the room name on phones** (see [One name per room](#one-name-per-room)).

**A knob feels wrong** (the volume jumps, or doesn't reach the top). Use
**Settings → Keys & knobs → Calibrate**.

**An update made something worse.** **Settings → This radio → Updates**, then
**Go back** next to the previous version.

**Nothing helps.** Shut the radio down (tone knob off for 5 seconds), wait
until the lamp is dark, unplug it for 10 seconds and plug it in again. If the
problem stays, tell the person who built your radio. What the lamp shows, and
the version number at the bottom of Settings, help them a lot.

---

<sub>Delgado &amp; Murphy</sub>
