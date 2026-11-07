# Using your Hasler

This guide is for the person who lives with the radio. You don't need to know
anything about computers.

Your Hasler is a Swiss radio cabinet from the 1960s with new parts inside. It
plays internet radio, Spotify and YouTube Music, and it is a speaker for your
phone. It works like the old radio: press a key, and music plays.

**Contents**

- [The first ten minutes](#the-first-ten-minutes)
- [Everyday use](#everyday-use): [the keys](#the-keys), [the knobs](#the-two-knobs), [the pilot lamp](#the-pilot-lamp)
- [Playing from your phone](#playing-from-your-phone)
- [Music in several rooms](#music-in-several-rooms)
- [The key page](#the-key-page): [Browse music](#browse-music), [Edit keys](#edit-keys), [Settings](#settings)
- [Home Assistant](#home-assistant)
- [Wi-Fi](#wi-fi) and [Moving the radio](#moving-the-radio)
- [Updates](#updates)
- [After a power cut](#after-a-power-cut)
- [When something is wrong](#when-something-is-wrong)
- [Help from the makers](#help-from-the-makers)

---

## The first ten minutes

1. **Plug it in.** The pilot lamp (the little window on the front) breathes
   amber while the radio warms up. After about 15 seconds the music comes
   back, or the lamp settles to a dim amber: the radio is ready.
   If the lamp **taps out a slow amber Morse code** instead (CQ:
   long-short-long-short, long-long-short-long), the radio needs your Wi-Fi
   details first: see [Wi-Fi](#wi-fi).
2. **Press key 1.** Music starts. Turn the **volume** knob to taste.
3. **Try the other keys.** Each numbered key plays its own station or
   playlist. Press the playing station's key again, and the radio goes to
   sleep.
4. **Open the key page.** On a phone or computer on the same Wi-Fi, open
   **http://hasler.local**. Tap the gear: **Make it yours** lists what
   is still yours to choose, in order.
5. **Set how loud it may go.** Settings → **Sound** → Volume ceiling. It
   protects the old speaker. It is usually set already (the number is on the
   welcome card). If the page says *not set yet*: play something dense, hold
   key 6 until the volume stops rising, and move the ceiling up in steps of 5.
   At the first rattle from the cone, go back two.
6. **Put your music on the keys.** Tap the pencil (**Edit keys**), or
   **Start from a set…** for a whole set at once.
7. **Play from your phone.** In Spotify or AirPlay, choose **Hasler** as the
   speaker. For Bluetooth, hold the **PU** key until the lamp breathes blue,
   then choose **Hasler** on your phone.
8. **Keep a card by it.** Edit keys → **Print a key card**: what each key does
   and what the lamp says.
9. **Switch it off.** Turn the **tone** knob all the way left until it clicks:
   the radio sleeps (a still, dim ember glow). Keep it clicked off for 5
   seconds and it shuts down. When the lamp is **dark**, it is safe to unplug.

That's all you need. The rest is for when you want more.

## Everyday use

### The keys

The radio has six numbered keys and one key marked **PU**. (The Excelsior has
more; see [below](#the-excelsiors-keys).) The keys start with a set
chosen to show what the radio can do; there are ten, by taste and occasion
(Turn the Dial, the one it comes with; Mixtape, Level Up, Deep Cuts, After
Hours, Encore, Passport, Sobremesa, Saturday Morning and Night Light). Pick
another in [Edit keys](#edit-keys) → **Start from a set…**.

| You do this | What happens |
|---|---|
| Tap key 1 to 6 | That key's music starts: a radio station or a playlist. |
| Tap the key that is playing a **station** | The radio goes to sleep (standby). |
| Tap the key that is playing a **playlist** | It skips to the next song. |
| Hold key **5** / key **6** | Volume goes down / up while you hold it. |
| Hold key 1 to 4 | Nothing, unless you gave it a job (for example a Home Assistant scene). |
| Tap **PU** | Changes the source: radio, Bluetooth, AirPlay, Spotify, and back. Sources that are off are skipped. |
| Hold **PU** | Opens Bluetooth pairing for 2 minutes. |

A tap on any key wakes the radio. It remembers the last key you pressed, even
after you unplug it.

Out of the box the keys hold a starter set of stations and playlists. There
are ten sets, grouped by occasion: every day (Turn the Dial, the one the
radio comes with; Mixtape; Level Up; Deep Cuts), for a mood (After Hours,
Encore), go somewhere (Passport, Sobremesa) and family time (Saturday
Morning, Night Light). Choose one in **Edit keys → Start from a set…**. What PU steps through is in **Settings → Keys**.

**Sleep timer.** On the key page, tap the small clock beside **Standby** and
choose 15, 30 or 60 minutes. A "Sleep · 23 min" chip shows what is left; tap
it to cancel. In the last minute the music fades out, then the radio goes to
sleep, and next time it wakes at your usual volume. Touching any key or
knob, or unplugging, cancels the timer. A Bluetooth phone is asked to pause;
an AirPlay phone keeps sending to a silent radio until you stop it. You can
also give a key's **hold** the job **Sleep timer** in [Edit keys](#edit-keys).

#### Key combinations

Hold **two keys together** for about a second and the radio does one more
thing. Out of the box, **keys 1 + 6** open Bluetooth pairing (the lamp
flashes once, then breathes blue).

Press the second key soon after the first, well under a second. Let go too
early and nothing happens: just try again. You can change this pair, or add up
to 8 of your own, in [Edit keys](#edit-keys).

#### The Excelsior's keys

The Excelsior has ten keys and four wheels, and its keys **stay down**:
pressing one pops the one that was down before.

| You do this | What happens |
|---|---|
| Press key 1 to 6 | That key's music starts. |
| Press **Tape** | A seventh station or list (the key page calls it key 7). |
| Press **PU** | Your phone over Bluetooth: the radio calls the last phone it knew. |
| Press **0** | Standby. Hold it **5 seconds** to shut down; when the lamp is dark, it is safe to unplug. |
| Press **SP** | Silence while it is down; press again for sound. |
| Turn the **speaker** selector | Both speakers, the left or the right. |
| **Volume**, **treble**, **bass** | What they say. |

A key that stays down can't be pressed "again": to stop, press **0**; to skip
a song, use the key page. Excelsior keys have no hold jobs or combinations, so
Bluetooth pairing (**Settings → Phones → Pair a phone**) and the sleep timer
are on the key page. AirPlay and Spotify need no key: a phone just picks
*Hasler*. An Excelsior whose keys were changed to spring back works like the
Hasler HM.

### The two knobs

**Volume** works for everything the radio plays. It never goes past the
**volume ceiling**, which protects the original 1964 speaker.

**Tone** shapes the sound. Out of the box, left is warmer, right is brighter,
the middle is flat. In **Settings → Sound → What the tone knob does** you can
give it another job: treble, bass, loudness, presence (voices forward),
*1960s radio*, stereo width, or nothing. It never makes the old speaker work
harder; a strong setting can sound a touch quieter, so turn the volume up.

**The tone knob is also the on/off switch:**

| You do this | What happens |
|---|---|
| Turn it left until it clicks | Standby: the music stops, the lamp glows a dim ember. |
| Turn it back past the click | The radio wakes and the last music comes back. |
| Keep it clicked off for **5 seconds** | The radio shuts down. The lamp fades, breathes amber, then goes dark. |

When the lamp is **dark**, it is safe to unplug. To start again after a
shutdown, unplug it and plug it back in with the tone knob on.

### The pilot lamp

In short: **amber** is the radio, **blue** is a phone, a still **ember** glow
is asleep, **green** is OK, **red** is something wrong, **dark** is safe to
unplug.

| The lamp shows | It means |
|---|---|
| **Warm and bright**, like a light bulb | Playing music. |
| **Amber, dim and still** | Ready. Press a key. |
| **Amber, breathing** | Busy (starting, loading, updating, shutting down). Leave it plugged in. |
| A quick **brighten** | The radio heard your key. |
| A **green** glow | Done (a phone paired, Wi-Fi joined). Green Morse **OK** after it: set up, or updated. |
| A **red** glow that fades | Something you asked for didn't work (say, a station is down). |
| A short **dip** on a volume key | As loud (or quiet) as it goes. |
| **Two quick brightens**, again and again | Press a key on the radio to confirm what you started on the page. |
| **Amber Morse** (CQ) | The radio needs Wi-Fi details. See [Wi-Fi](#wi-fi). |
| **Two red flashes** every few seconds | No internet. Check your router; Bluetooth still works. |
| **Red Morse** (SOS: short-short-short, long-long-long, short-short-short) | Something needs the makers. See [When something is wrong](#when-something-is-wrong). |
| **Green Morse** (HI), three times | This radio saying "it's me" (Identify). |
| One slow **swell** | This radio joined or left another room's music. |
| **Blue** | A phone is playing. Dimmer when paused. |
| **Blue, breathing** | Bluetooth pairing is open. |
| **Ember**, still and dim | Asleep. If the lamp moves, it isn't asleep. |
| **Dark** | Off. Safe to unplug. |

The knobs never change the lamp: your ears are the feedback. Music from
other music apps (UPnP) shows amber, not blue. From sunset to
sunrise the lamp is dimmer and slower. Brightness, night hours and the quick
brighten are in **Settings → Pilot lamp**.

**Can't tell the colours apart?** **Settings → Pilot lamp** says it in words
(*Now: asleep*), and **Show me** plays any signal on the radio's lamp so you
learn it by looking.

**Geek mode** (Settings → Pilot lamp, off at first): Identify sends a call
sign in Morse, the radio says 73 as it shuts down, and an update ends with
five famous notes. Radio amateurs may find the last two keys, held together
for a few seconds, worth a try.

Every colour in detail: [The pilot lamp](lamp.md).

## Playing from your phone

The radio is a speaker for your phone. On the network it is called
**Hasler**.

- **Spotify** (Premium): tap the speaker icon and choose **Hasler**. Guests on
  your Wi-Fi can do the same with their own Spotify; switch off **Guests on
  your Wi-Fi can play** to stop that. A guest never replaces the radio's own
  account.
- **AirPlay** (iPhone, iPad, Mac): choose **Hasler** from the AirPlay list.
- **Bluetooth:** hold **PU** until the lamp breathes blue (or tap **Pair a
  phone** on the key page), then choose **Hasler** in your phone's Bluetooth
  settings.
- **Other music apps** (UPnP / DLNA): choose **Hasler** as the player.

While a phone plays, the lamp is blue. To take the radio back, tap any
numbered key.

When a phone stops, the radio goes back to its own music after about 20
seconds (a couple of seconds if a Bluetooth phone disconnects), unless you
chose something else meanwhile or the phone had woken it from standby. To
switch this off: **Back to the radio when a phone stops**, in **Settings →
Phones**, where every way of playing can also be switched on or off.

**The radio's own Spotify account** is the one Spotify keys play from.
**Settings → Music accounts → Spotify → Sign in**, press a key on the radio,
then go to **spotify.com/pair** on your phone and enter the code the page
shows. While a code is waiting, Spotify plays nothing on the radio; **Cancel**
(or 15 minutes) keeps the old account.

### Bluetooth

- **Your phones.** **Settings → Phones → Your phones** lists paired phones,
  with **Connect**, **Disconnect** and **Forget**.
- **The radio calls your phone.** When you switch to Bluetooth, or wake a
  radio that went to sleep on Bluetooth, it connects the last phone you used.
  The lamp is dim blue while it waits. Switches: **Reconnect to the last
  phone**.
- **Phone volume is the radio's volume**, within the safe range. Connecting a
  phone never changes how loud the radio is.
- **A second behind.** The radio keeps a second of music in hand, so the
  sound doesn't break. With video, the phone usually makes up for it.
- **A Bluetooth key.** In **Edit keys**, set a key's tap to **Bluetooth**.
  While your phone plays, tapping it again skips a track and holding it
  pauses.

### One name per room

If you have more than one Hasler, give each a room name and switch on **Use
the room name on phones** (**Settings → Rooms → This room**). Phones then show
*Hasler Kitchen*, *Hasler Study* and so on. Renaming takes a minute or two.

## Music in several rooms

Two or more Haslers on the same Wi-Fi can play the same music, in step. One
radio **leads**; the others **follow**.

1. **Switch it on** on every radio: **Settings → Rooms → Play in several
   rooms**.
2. On the leading radio's front page, tap **Also play on…** and tick the
   other radios. Untick one to give it back its own music.

Or from the other room: give a key's hold the job **Join another Hasler** in
Edit keys. Hold it to join; hold it again to leave.

Grouped, music starts about **1 second** after a key press. That keeps the
radios in step.

**While a radio follows:**

- **Numbered keys** change the music for the whole group. (To make a key take
  just this radio out to play alone: **Settings → Rooms**.)
- **Volume and tone** stay with each radio.
- **Standby on a follower** puts only that radio to sleep. **Standby on the
  leader** ends the group.
- **PU**, or a phone playing to a follower, takes that radio out of the
  group. A phone playing to the **leader** is heard everywhere.
- A red glow means the leader couldn't be reached: the radio is on its own
  again.

If the leader can't play a key (say it has no Spotify), the radio you pressed
takes over and leads. After a power cut, a follower comes back quiet: tick it
again, or hold its join key.

How it works inside: [multiroom.md](multiroom.md).

## The key page

The key page controls the radio from a phone or computer on the **same
Wi-Fi**:

**http://hasler.local**

If that doesn't open, try the radio's IP address from your router. A second Hasler is `hasler-2.local`; **Settings → Rooms**
shows each radio's address.

### The front screen

It looks like the radio. Tap a key to play it, turn the volume, put the radio
to sleep, skip a song, and see what is playing.

The dial's needle shows where the music comes from: a stop for each key, the
left end for a phone, the right end for anything started from the page. A key
with an amber bar is the one you hear. If a station stays silent for two
minutes, the dial says it may be off the air.

### Browse music

Tap **Browse music**. There is a tab for **Radio**, **YouTube Music** and
**Spotify**. Press play to listen now, or **+** to put it on a key.

**Radio** is the radio's own collection: nearly 600 stations from about 120
countries, each played on a Hasler before it went in and checked every week.
Look by region, genre or language, or type a name, city or style (accents
don't matter).

- **My stations** is your shortlist: tap the star to keep a station, or add
  your own stream links. Every phone sees the same list, and updates keep it.
- **Recently played** lists the last stations the radio played.
- **Search the internet too** also asks Radio Browser, a free directory of
  some 50,000 stations. Those aren't checked, and are marked so.
- **Doesn't play?** notes a broken station. The note stays on your radio;
  nothing is sent anywhere.

**YouTube Music**: search songs, albums, artists and playlists, or browse the
charts, moods & genres and (signed in) your library. A key can hold a
playlist or album, an artist (top songs, artist radio or shuffled), an
endless song radio, a chart, a mood list, or your Liked songs. Charts and
lists are fetched fresh each time you press the key; if YouTube Music can't
be reached, the key plays what it played last time.

While a YouTube Music song plays, two small buttons sit beside it on the
dial: **Lyrics** (opens them under the dial) and **⋯** (start a radio from
the song, put it on a key, or **Like** it when signed in). **Hide explicit
songs** is in the YouTube Music settings.

### Edit keys

Tap the pencil (**Edit keys**), then choose a key.

1. Choose **Radio**, **Spotify** or **YouTube Music**.
2. Search, or paste a link (Spotify: *Share → Copy link*).
3. Turn on **Shuffle** if you like.
4. **Test on the radio** to hear it, then **Save**.

You can also choose what **holding** a key does: a Home Assistant scene or
lights, the **Sleep timer**, **Join another Hasler**, or **Open Wi-Fi setup
now**. A key can be a [Bluetooth key](#bluetooth).

**Key combinations** are here too: pick two keys (**Check this pair** shows
whether the cabinet lets you hold both), how long to hold, what they do, and
**Try it**.

At the bottom: **Start from a set…** puts a whole starter set on the keys, and
**Reset keys to defaults** puts the built-in stations back. Both ask for a key
press on the radio, and Reset can be undone for a day. Keys you never changed
get small improvements with updates; keys you chose are never touched.

### Settings

Tap the gear. Anything that needs you (an update, a Spotify code, a Wi-Fi not
set up) is listed first, under **Needs attention**, and the gear carries a
small amber dot. **Something's wrong?** opens a page where the radio checks
itself (see [When something is wrong](#when-something-is-wrong)).

On a new radio, **Make it yours** lists the choices still open, in the order
of a first evening: the ceiling, your keys, the room's name, a phone, Spotify,
and so on. A row is done when the radio knows it is. Optional rows have
**Skip**. When all are done, the lamp blooms green and the list goes.

| Row | What you find there |
|---|---|
| **Sound** | The **volume ceiling** (30 up to the top for this speaker; 90 on the Hasler HM). The **quietest step**. **Mono** (on to start). **What the tone knob does**. **A knob feels off?**: **Calibrate**. |
| **Keys** | Edit keys, what **PU** steps through, and Reset keys. |
| **Phones** | Spotify, AirPlay, Bluetooth and UPnP on or off. **Your phones** (with the Bluetooth codec while one plays) and **Pair a phone**. **Back to the radio when a phone stops**. The name phones see. |
| **Music accounts** | **Spotify**: the radio's own account. **YouTube Music**: your sign-in, for Liked songs and private playlists (it takes a computer and a few technical steps; the makers can do it for you over remote help). |
| **Rooms** | The **room name**, the radio's address, **Identify**, **Play in several rooms**, and the other Haslers. |
| **Pilot lamp** | Brightness, key replies, night dimming (and where the radio stands, for sunset), what the lamp means now, **Show me**, Geek mode. |
| **This radio** | **Wi-Fi**, **Moving this radio**, **Updates**, **How the radio is** (with **Restart the radio**), **From the makers** (a note after a repair), **Home Assistant**, and [Help from the makers](#help-from-the-makers). |

At the very bottom of Settings: the version number and the radio's short id
(four letters and digits, like `ab12`). Read both out if you phone the
makers.

**How changes are kept.** A switch or slider works at once, with a small
**Undo** for a few seconds. Where you type something, nothing changes until
you press **Save**.

### "Press any key on the radio"

Some buttons change a lot: installing an update, going back a version,
changing Wi-Fi, resetting the keys, signing Spotify in or out, letting the
makers help, restarting, handing the radio to a new owner. For these the page
asks you to **press any key on the radio within 30 seconds**. That proves
someone is standing next to it.

One honest line: the player underneath (moOde) has its own pages on the
radio's address at `:8081`, and they ask for no key press. The radio
trusts your home network, as your printer does. If you keep guests on a
separate network, that is enough.

## Home Assistant

The radio works perfectly well without Home Assistant. If you have it, there
are two things you can do.

**1. See and control the radio in Home Assistant.** Install the **Hasler
Radio** integration once, with HACS:

1. In HACS, tap **⋮** (top right) → **Custom repositories**.
2. Paste `https://github.com/Delgado-Murphy/hasler-homeassistant`, type
   **Integration**, press **Add**.
3. Search HACS for **Hasler Radio** and press **Download**.
4. Restart Home Assistant (Settings → System → Restart).
5. In **Settings → Devices & services** the radio shows up as
   **Discovered**: press **Add**. (If not: **Add integration** → **Hasler
   Radio**, host `hasler.local`, port `8080`.)

Each radio becomes a device with a media player (its keys are the sources),
volume, standby, what is playing, and whether an update is waiting (Home
Assistant can't install it; the radio does that itself). Don't
add Home Assistant's *Music Player Daemon* integration for the radio: it
fights the radio.

**2. Let a key run something in your home** (a scene, a light, a script):
connect Home Assistant in **Settings → This radio → Home Assistant**, then
give a key's hold a job in **Edit keys**. If the key can't reach Home
Assistant, it simply does nothing.

## Wi-Fi

### First time, or a new home

If the radio can't find a Wi-Fi network it knows:

1. Plug it in and wait about 2 minutes. The lamp **taps amber Morse** (CQ):
   the radio has made its own small Wi-Fi network called **Hasler** (or
   **Hasler Kitchen**, once it has a room name).
2. On your phone, join that network. Its password is on the welcome card
   (or ask the makers).
3. A page opens by itself. If not, open **http://172.24.1.1:8080/wifi** (or
   any `http://` address, like **http://neverssl.com**).
4. Choose your home network, type its password, press **Connect**.
5. The radio restarts. Put your phone back on your home Wi-Fi. The lamp
   blooms green and taps OK; the music comes back by itself.

If the lamp glows red and taps CQ again, the password was wrong: try once
more.

**After a power cut** the radio may start before your router and make its
own network. You don't need to do anything: it keeps looking for your Wi-Fi
and goes back by itself. To hurry it, unplug and replug once the router is
up.

### Changing networks at home

**Settings → This radio → Wi-Fi → Change**, then press a key on the radio. If
the new network doesn't work, the radio goes back to the old one.

To open the setup network without forgetting anything (say the router's
password changed), give a key's hold the job **Open Wi-Fi setup now** in Edit
keys.

### If nothing works: a file on the memory card

1. Unplug the radio and take out its memory card. Put it in a computer: a
   drive called **bootfs** appears.
2. Make a plain text file there called **hasler-wifi.txt** with two lines:

   ```
   ssid = the name of your Wi-Fi
   password = its password
   ```

3. Put the card back and plug the radio in. It reads the file, deletes it and
   joins that network. What happened is in **hasler-wifi-result.txt**.

The file must be plain text (in TextEdit: Format → Make Plain Text). A third
line, `hotspot_password = …`, gives the radio's setup network a new password,
in case you lost the old one.

A network cable from the router also works: the radio is then at
**http://hasler.local**.

## Moving the radio

**Settings → This radio → Moving this radio** is the one page for when the
radio changes place.

### Another room

Unplug it, carry it, plug it in: it comes back playing what it played. Then
give it the room's name (**Rename**). If a key runs lights for the old room,
choose new ones in **Edit keys**. **Identify** makes its lamp tap HI, if you
aren't sure which radio is which.

### Another home, or a new Wi-Fi

**Before you go**, write down the setup network's name and password (**Show**,
or **Print**). **At the new home**, plug it in, wait for CQ, join the setup
network, and follow [Wi-Fi](#wi-fi). No phone handy? Use the
[file on the memory card](#if-nothing-works-a-file-on-the-memory-card) or a
network cable. No internet yet? Bluetooth from a phone still works.

**Afterwards:**

- **Home Assistant:** Disconnect the old one, connect the new home's, and
  choose new targets for the keys the page lists (or **Clear missing**).
- **Clock and place:** the radio keeps the old home's time zone until you
  change it here. Home Assistant can offer its own.
- The old Wi-Fi stays saved, in case you return. **Forget** it here if you'd
  rather.

### A new owner

**Hand over to a new owner**, then press any key on the radio. The radio
forgets you: your Wi-Fi, accounts, Home Assistant, phones, keys, stations and
room name. It keeps what belongs to the cabinet: knob calibration, lamp, the
volume ceiling, its software.

The page then shows the new setup network and password, once: **write them
down** or **Print a card for the new owner**. Their first ten minutes are the
ones at the top of this guide.

## Updates

Now and then a new version of the radio's software comes out. Your keys and
settings are never changed by an update. Everything is in **Settings → This
radio → Updates**.

| Choice | What it means |
|---|---|
| **Automatic** (to start) | Small updates install by themselves at night, in standby. Bigger ones ask first. |
| **Ask** | The page tells you. Choose **Install tonight** or **Install now**, then press a key on the radio. |
| **Off** | Never downloads or installs. Fixes won't reach the radio. |

- An update installs only while the radio rests (standby, nothing playing for
  10 minutes). After **Install now**, put the radio to sleep.
- While it installs (two or three minutes) the lamp **breathes amber**. Leave
  it plugged in. When it is done, the lamp glows green and taps OK.
- If something doesn't work afterwards, the radio goes back to the previous
  version by itself, and the page says why.
- **To go back by hand:** find the older version, press **Go back**, press a
  key on the radio.
- Security fixes for the system underneath install the same way, at night,
  and are taken back if anything is worse afterwards.
- **This Wi-Fi is a phone hotspot or capped**: the radio downloads nothing in
  the background, only when you press **Check now**.
- **Tell the makers how the radio is** (off to start): once a day the radio
  sends a few lines about whether everything works. No names, no places,
  nothing about what you listen to; **See what it sends** shows exactly what.

What changed in each version: the
[releases page](https://delgado-murphy.github.io/hasler-releases/).

## After a power cut

- The lamp breathes amber, and after about 15 seconds the music comes back.
- If the power comes back **at night**, the radio stays asleep. Press a key in
  the morning.
- If the tone knob is clicked off, the radio stays asleep.
- If the music can't come back (no internet, say), the lamp glows red, then
  dim amber. Press a key.
- If the power goes often (a plug timer? a wall switch?), use the tone knob
  instead: off for 5 seconds, wait for dark, then unplug.

## When something is wrong

**Start with Settings → Something's wrong?** The radio checks itself and says
what it found, in plain words, with one thing to try for each problem.

**No sound.**

1. Ember lamp means asleep: press a key or turn the tone knob on.
2. Turn the volume up, and check the ceiling (**Settings → Sound**).
3. Blue means a phone has the radio: stop it there, or tap a numbered key.
4. A red glow means the music didn't start. Try another key.

**The key page doesn't open.**

- **Music plays, but no page:** your phone is probably on another network
  (mobile data, a guest network). Check it is on the same Wi-Fi, and wait a
  minute after the radio starts. Try the radio's IP address instead of
  `hasler.local`. A key set to **Open Wi-Fi setup now** reaches a radio
  stuck on the wrong network.
- **The lamp breathes amber and never settles:** unplug, wait ten seconds,
  plug in. If it happens again (after ten minutes the lamp taps SOS), phone
  or write to the makers with what the lamp does.
- **Amber Morse (CQ):** the radio isn't on your Wi-Fi: see [Wi-Fi](#wi-fi).

**Dark after plugging in?** Use the charger and cable that came with it (any
65 W USB-C charger that gives 20 V works; phone chargers often don't).
Unplug, wait ten seconds, plug in. Still dark? Call a maker. Nothing inside
is dangerous, but don't open it.

**Two red flashes every few seconds.** No internet. Check your router.

**Red Morse (SOS).** Something inside needs the makers. Settings says what, at
the top. Tell them. When it clears, Settings says so.

**Stations work, but YouTube Music or Spotify don't.** Sign in again in
**Settings → Music accounts** (sign-ins can expire after months, or when a
password changes). The radio mends its YouTube Music helper by itself,
usually within hours.

**My phone won't connect over Bluetooth.** **Forget** it in **Settings →
Phones**, then hold **PU** and pair again.

**Another Hasler is missing from "Also play on…".** Switch on **Play in
several rooms** on both radios, on the same Wi-Fi. Wait half a minute.

**A knob feels wrong.** **Settings → Sound → If a knob feels off →
Calibrate**.

**An update made something worse.** **Settings → This radio → Updates → Go
back**.

**Something feels stuck.** **Settings → This radio → How the radio is →
Restart the radio**. If that doesn't help: tone knob off for 5 seconds, wait
for dark, unplug for 10 seconds, plug in.

**Still not right?** [Tell the makers](#help-from-the-makers).

## Help from the makers

**Tell the makers:** **Settings → Something's wrong? → Tell the makers**. The
radio puts together what they usually ask: the version, what the lamp says,
what's playing, what went wrong inside. Nothing private (untick **Include my
Wi-Fi name** if you like). Add a line, then **Share…** or **Copy**, or
**Email the makers**. If you only phone, read out the six lines under **Say
this to the makers**. Where the makers have set it up, **Send to the makers**
sends it straight to their private support list (you need no account), and
their answer comes back under **This radio → From the makers**; the sheet says
whether they have seen it and when it is done.

**Let them look from afar.** **Settings → This radio → Help from the makers →
Let the makers help (24 h)**, then press a key on the radio. Send the link the
page shows to the makers, and nobody else. Help ends after 24 hours, or when
you press **Stop**. The link works for an hour; if nobody came, press Start
again later. If the radio restarts meanwhile, the page shows a new link to
send, with no second key press. The radio must be on the internet. What they can see and do:
[Remote help](remote-help.md).

If the keys don't answer, the page asks you to turn the volume knob instead.
If nothing on the radio can be touched, put a plain text file called
**hasler-help.txt** with the one line `start` on the memory card's
**bootfs** drive (as for [Wi-Fi](#if-nothing-works-a-file-on-the-memory-card)).
The link appears in **hasler-help-result.txt** next to it; delete that file
once the link is sent.

**If the radio has to come back.** Send a report first. Pack the charger and
cable; leave the memory card in. Your stations and accounts stay on the
radio (sign out in Settings first, if you'd rather). Afterwards the makers
leave a note under **This radio → From the makers**.

**If the makers can't be reached.** The radio doesn't need them to play:
stations, playlists, phones and rooms all keep working, on the version it
has (after about two weeks the Updates page says there is no fresh update
information; that is all). What you can't do without them is make a new memory card, which is why
the radio came with a spare.

---

<sub>Delgado &amp; Murphy</sub>
