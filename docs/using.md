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
- [Help from the makers](#help-from-the-makers)
- [Wi-Fi](#wi-fi)
- [Updates](#updates)
- [After a power cut](#after-a-power-cut)
- [When something is wrong](#when-something-is-wrong)

---

## The first ten minutes

1. **Plug it in.** The pilot lamp (the little window on the front) breathes
   amber while the radio warms up. After about 15 seconds the music comes
   back, or the lamp settles to a dim amber: the radio is ready.
   If the lamp **taps out a slow amber Morse code** instead (long-short-long-short,
   long-long-short-long: CQ, "calling anyone"), the radio needs your Wi-Fi
   details first: see [Wi-Fi](#wi-fi).
2. **Press key 1.** Music starts. Turn the **volume** knob to taste.
3. **Try the other keys.** Each of the six numbered keys plays its own station
   or playlist. Press the key of the station that is playing again, and the
   radio goes to sleep.
4. **Open the key page.** On a phone or computer on the same Wi-Fi, open
   **http://hasler.local:8080**. On a new radio it says so under the keys:
   *New radio? Make it yours.* Tap the gear: **Make it yours** lists what is
   still yours to choose, in order. The first one protects the old speaker.
5. **Set how loud it may go.** Settings → **Sound** → Volume ceiling. The
   people who built the radio usually set it by ear (the number is on the
   welcome card); if the page says *not set yet*, play something dense, hold
   key 6 until the volume stops rising, and move the ceiling up in steps of 5.
   At the first rattle from the cone, go back two.
6. **Put your music on the keys.** Tap the pencil (**Edit keys**) to put your
   own stations and playlists on the keys, or **Start from a set…** for a whole
   set at once.
7. **Play from your phone.** In Spotify or AirPlay, choose **Hasler** as the
   speaker. For Bluetooth, hold the **PU** key until the lamp breathes blue,
   then choose **Hasler** on your phone.
8. **Keep a card by it.** Edit keys → **Print a key card**: what each key does
   and what the lamp says. The makers may have left a welcome card already.
9. **Switch it off.** Turn the **tone** knob all the way left until it clicks:
   the radio sleeps (a still, dim ember glow). Keep it clicked off for 5 seconds and
   it shuts down. When the lamp is **dark**, it is safe to unplug.

That's all you need. The rest of this guide is for when you want more.

## Everyday use

### The keys

The radio has six numbered keys and one key marked **PU**. (The Excelsior has
a few more; see [The Excelsior's keys](#the-excelsiors-keys) below.) Out of
the box the numbered keys hold **the built-in stations**: a starter set of
stations and YouTube Music lists chosen to show what the radio can do. There
are ten sets to start from, grouped by who's listening: little ones (songs and
lullabies), kids and family, teens, indie and discovery, and for grown-ups the
eclectic one the radio comes with, classical, jazz and soul, golden oldies,
España, and world and travel. Pick another in
[Edit keys](#edit-keys) → **Start from a set…**, then change any key you like.

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

**Sleep timer.** On the key page, tap the small clock beside **Standby** and
choose 15, 30 or 60 minutes. A "Sleep · 23 min" chip shows what is left; tap
it to cancel (Undo brings it back). In the last minute the music fades out and
the lamp dims, then the radio goes to standby as if you had pressed Standby;
the next time you wake it, it plays at your usual volume. Touching the radio
(any key or knob) cancels the timer, and so does unplugging it. A Bluetooth
phone is asked to pause; an AirPlay phone keeps sending to a silent radio until
you stop it, but it doesn't wake the radio again. If you would rather set it
from the cabinet, give **holding** a key (or a key combination) the job
**Sleep timer** in [Edit keys](#edit-keys), with the minutes you want; no key
does this out of the box.

You choose what each key plays, and what holding it does, in
[Edit keys](#edit-keys). What **PU** steps through, and in what order, is in
**Settings → Keys**.

#### Key combinations

Hold **two keys down together** for about a second and the radio does one
more thing. Out of the box, **keys 1 + 6 held together** open Bluetooth
pairing for 2 minutes (the same as holding PU), and the lamp flashes once, then
breathes blue.

- Press the second key soon after the first, before the first key's own hold
  starts (well under a second). Then neither key does its usual job: no
  music starts, no volume ramps.
- Let go too early and nothing happens at all. Try again.
- A third key pressed meanwhile is ignored if it belongs to a combination too.

You can change the pairing combination, or add up to 8 of your own (any job a
key can do, and how long to hold: half a second to 3 seconds), in
[Edit keys](#edit-keys). Keys that are in no combination work exactly as
before.

On some cabinets neighbouring keys can't be held together; the editor's
check (**Check this pair**) shows which pairs work.

#### The Excelsior's keys

The Excelsior has ten keys in a row and four wheels, and its keys **stay
down** when you press them: pressing one pops the one that was down before.
That changes a few things from the table above.

| You do this | What happens |
|---|---|
| Press key 1 to 6 | That key's music starts, as on any Hasler. |
| Press **Tape** | A seventh station or list: the starter set's seventh entry (the key page calls it key 7). |
| Press **PU** | Your phone over Bluetooth: the radio calls the last phone it knew. AirPlay and Spotify need no key, a phone just picks *Hasler*. |
| Press **0** | Standby. Key 0 springs back and pops whatever key was down. Hold it for **5 seconds** and the radio shuts down, the lamp glowing down to ember meanwhile; when the lamp is dark, it is safe to unplug. |
| Press **SP** | Silence while it is down (mute); press it again for the sound. |
| Turn the **speaker** selector | Both speakers, the left or the right. |
| The **volume**, **treble** and **bass** wheels | What they say; treble and bass each shape their end of the sound. |

Because a key that is down can't tell the radio it was pressed *again*, there
is no "same key again": to stop a station press **0**, and a playlist skips
to the next song only from the key page. For the same reason the Excelsior's
keys have no **hold** jobs and no key combinations: Bluetooth pairing is on
the key page (**Settings → Phones & casting → Pair a phone**), and so is the
sleep timer. The lamp, the key page, Browse and everything else are the same.
A unit whose keys were converted to spring back behaves like the Hasler HM,
holds and all.

### The two knobs

**Volume** works for everything the radio plays: stations, playlists and your
phone. It never goes louder than the **volume ceiling**, which protects the
original 1964 speaker (**Settings → Sound**).

**Tone** shapes the sound. Out of the box it is a *tilt*: left is warmer (more
bass, less treble), right is brighter, the middle is flat. You can give it a
different job in **Settings → Sound → What the tone knob does**: treble, bass,
loudness (fuller sound when quiet), presence (voices forward), *1960s radio*
(the old radio's voice, blended in as you turn), stereo width, or nothing.
A choice applies at once, with **Undo**: turn the knob to hear it.

Whatever the tone knob does, it never makes the old speaker work harder: when
it lifts part of the sound, the radio turns everything else down a little. A
strong setting can sound a touch quieter; just turn the volume up.

**The tone knob is also the on/off switch**, like the old radio's:

| You do this | What happens |
|---|---|
| Turn the tone knob left until it clicks | Standby: the music stops and the lamp glows a dim ember (deep orange-red). |
| Turn it back past the click | The radio wakes and the last music comes back (or your phone, if it was on Bluetooth). |
| Keep it clicked off for **5 seconds** | The radio shuts down. The lamp glows down to ember over the 5 seconds, breathes amber while the radio shuts down, then holds a still ember to the very end and goes dark. |

When the lamp is **dark**, it is safe to unplug. To start the radio again after
a shutdown, unplug it and plug it back in with the tone knob turned on.

### The pilot lamp

The lamp tells you what the radio is doing, like the lamp of an old valve
radio. In short: **amber** is the radio, **blue** is a phone, a still
**ember** glow is asleep, **green** is OK, **red** is something wrong,
**dark** is safe to unplug.

| The lamp shows | It means |
|---|---|
| **Warm and bright, like a light bulb** (a faint, living shimmer) | Playing music (from a key, or with another room). |
| **Amber, dim and still** | On and ready, but quiet. Press a key. |
| **Amber, breathing** | Busy: starting up, loading music, joining another room's music, installing an update or shutting down. Wait, and leave it plugged in. |
| A quick **brighten** | The radio heard your key (you can switch this off in **Settings → Pilot lamp**). |
| A **green** glow, for a moment | OK: done (a phone paired, Wi-Fi joined, your press confirmed). Green Morse **OK** after it: set up, or an update installed. |
| A **red** glow that holds, then fades | No, or something you asked for didn't work (for example, a station is down). |
| A short **dip**, once | On a volume key: as loud (or as quiet) as it goes. |
| **Two quick brightens, again and again** | The radio is asking you to press a key on it, to confirm something you started on the page. |
| **Amber Morse** (CQ: long-short-long-short, long-long-short-long) | The radio needs Wi-Fi details. See [Wi-Fi](#wi-fi). |
| **Two red flashes every few seconds**, over dim amber | No internet. Check your router; Bluetooth from a phone still works. |
| **Red Morse** (SOS: short-short-short, long-long-long, short-short-short) | Something is wrong that needs the person who built your radio. |
| **Green Morse** (HI: four shorts, two shorts), three times | This radio saying "it's me" (Identify). |
| One slow **swell** | This radio joined or left another room's music. |
| **Blue** | A phone is playing through the radio (Bluetooth, AirPlay or Spotify). Dimmer when paused. |
| **Blue, breathing** | Bluetooth pairing is open: connect your phone now. |
| **Ember** (a still, dim deep red) | Asleep (standby). It never moves: if the lamp moves, it isn't asleep. |
| **Dark** | Off. Safe to unplug. |

When the colour changes to or from blue, the lamp closes briefly and opens
again in the new colour, so it never passes through muddy in-between shades.

Turning the knobs never changes the lamp: your ears are the feedback. Music
from other music apps (UPnP) shows amber for now, not blue.

From sunset to sunrise the lamp is dimmer and moves more slowly. To use fixed
hours instead, or never dim, and to set the lamp's brightness (Low, Normal or
Bright), see **Settings → Pilot lamp**.

**Can't tell the colours apart?** **Settings → Pilot lamp** says it in words
at the top (*Now: asleep*, *Now: a phone is playing*, *Now: busy*), and
lists everything the lamp can say, grouped, each with its rhythm and **Show
me**: the lamp on the radio plays it for a few seconds, so you learn it by
looking (at night, the night version). **Show me everything** plays them all
in turn, about a minute, with a caption for each. The printed key card
(**Edit keys → Print a key card**) has a small legend of the rhythms worth
knowing.

**Geek mode** (Settings → Pilot lamp, off at first): Identify sends your
call sign in Morse (up to three letters or digits; the room's initial unless
you choose one), the radio says 73 ("best regards") as it shuts down, and an
update ends with five famous notes. Radio amateurs may also find that the last
two keys of the row, held together for a few seconds, are worth a try.

Every colour in detail: [The pilot lamp](lamp.md).

## Playing from your phone

The radio is also a speaker for your phone. On the network it is called
**Hasler**.

- **Spotify:** in the Spotify app, tap the speaker icon and choose **Hasler**.
  This needs Spotify Premium. Guests on your Wi-Fi can do the same with their
  own Spotify, no password needed. To allow only the radio's own account,
  switch off **Guests on your Wi-Fi can play**. A guest never replaces the
  radio's own account: when they stop, the keys play from it again.
- **The radio's own Spotify account** (the one Spotify keys play from):
  **Settings → Music accounts → Spotify** says *Signed in as
  jd······fe*. To sign in or change it, tap **Sign in** (or **Change account**)
  and press any key on the radio to confirm. The page shows a code: on your
  phone or computer go to **spotify.com/pair**, sign in with a Premium account
  and enter it. The radio signs in by itself within a few seconds. **Cancel**
  keeps the old account; so does doing nothing for 15 minutes. While a code
  is waiting, Spotify plays nothing on the radio. **Sign out** (also with a key
  press) stops the Spotify keys; guests can still play.
- **AirPlay** (iPhone, iPad, Mac): choose **Hasler** from the AirPlay list.
- **Bluetooth:** hold **PU** until the lamp breathes blue (or tap **Pair a
  phone** on the key page), then choose **Hasler** in your phone's Bluetooth
  settings. After that, the phone connects by itself, or the radio calls it
  (see [Bluetooth](#bluetooth)).
- **Other music apps** (UPnP / DLNA, on Android, Windows and many NAS boxes):
  choose **Hasler** as the player.

While a phone plays, the lamp is blue. A phone can wake the radio from
standby. To take the radio back, tap any numbered key.

When a Bluetooth or AirPlay phone stops, the radio goes back to its own music
after about 20 seconds (a pause or a skip in between doesn't count), or within
a couple of seconds when a Bluetooth phone disconnects - unless you chose something else on the radio in the
meantime, or the phone had woken it from standby (then it stays quiet). To
switch this off: **Back to the radio when a phone stops**.

All of this is in **Settings → Phones**: each way of playing on or off, your
Bluetooth phones, and what phones call the radio.

### Bluetooth

- **Your phones.** **Settings → Phones → Your phones** lists the paired
  phones: which one is connected, its battery (if the phone tells), and when
  it was last used. **Connect**, **Disconnect**, or **Forget** (pair it again
  later with **Pair a phone**).
- **Sound quality.** While a phone plays, the same place shows the codec it
  uses (SBC, aptX or aptX HD) and the sample rate. The phone chooses; on
  Android it is under *Developer options → Bluetooth audio codec*.
- **The radio calls your phone.** When you switch to Bluetooth (with PU or a
  Bluetooth key), the radio connects the phone you used last, or the one
  before if that one is away. The lamp is dim blue while it waits. And if the
  radio went to sleep on Bluetooth, waking it goes back to Bluetooth and calls
  the phone, instead of playing a station. Both are switches under
  **Reconnect to the last phone** (*When I switch to Bluetooth* and *When the
  radio wakes up*), in **Settings → Phones → Your phones**. Both are on to
  start.
- **Phone volume is the radio's volume.** While a phone plays over Bluetooth,
  its volume buttons turn the radio up and down (in the knob's safe range) and
  its slider follows the knob and keys; after a change on the phone, the next
  turn of the knob sets the volume from where the knob is. Connecting a phone
  never changes how loud the radio is. Switch: **Phone volume controls the
  radio** (on to start).
- **A second behind.** The radio keeps about a second of music in hand, so a
  phone's hiccups don't break the sound. With video, the phone usually makes
  up for it.
- **A Bluetooth key.** In **Edit keys**, choose a key and set its tap to
  **Bluetooth**. Tapping it switches the radio to Bluetooth and calls your
  phone. While your phone plays, tapping it again skips to the **next track**,
  and holding it **pauses** (hold again to play). Under **While Bluetooth
  plays**, on that key in Edit keys, you can set *Press again* or *Hold* to do
  nothing instead; these apply at once. When no phone is playing, holding the
  key does its usual hold job.

### One name per room

If you have more than one Hasler, give each a room name and switch on **Use
the room name on phones**, both in **Settings → Rooms → This room**
(**Settings → Phones** shows the result). Phones then show *Hasler Kitchen*, *Hasler Study* and so on,
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

- Its lamp breathes while it joins, then shows the leader's amber. A red
  glow means the leader couldn't be reached; the radio
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
glows red and is on its own, and the others play on. (A station
that is simply down makes the lamp glow red, no takeover.)

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
`hasler-2.local` by itself; **Settings → Rooms** shows each radio's
address.)

### The front screen

It looks like the radio. Tap a key to play it, turn the volume on the screen,
put the radio to sleep or skip to the next song, and see what is playing.
**Also play on…** is here too (see [several rooms](#music-in-several-rooms)).

The dial says where the music is coming from. Its needle has a stop for each
key, a stop at the left end for a phone and one at the right end for anything
started from the page (Browse, a playlist that isn't on a key). The stop the
needle rests on is shown in bold while it plays, with the station or the
phone's name under it. When a phone stops, the needle stays where it is for a
moment, in case the radio brings the last key back; after that it parks at the
last key until something plays. In the key bank, a key with an amber bar is
the one whose music you hear; a key drawn pressed in is one that is physically
down (on an Excelsior, where the keys stay down, key 3 can be down while a
phone plays - then it is drawn down but without the bar). PU gets the bar only
when it chose the phone: a phone that connected by itself lights nothing. If a
station stays silent for more than two minutes, the dial says it may be off the
air and suggests another key.

### Browse music

Tap **Browse music**. Each kind of music has its own tab: **Radio**,
**YouTube Music** and **Spotify**. Press play to listen now, or **+** to put
it on a key.

### Radio stations

The **Radio** tab is the radio's own collection: nearly 600 stations from
about 120 countries, from the Spanish national and regional broadcasters
(RNE, SER, COPE, RAC1, Catalunya Ràdio, Canal Sur, EITB, Radio Galega...) to
public, community and college stations, jazz, classical and late-night
ambient from everywhere else. Every one was played on a Hasler before it went
in, and they are checked again every week. Look by **region and country**, by
**genre** or by **language**, or type a name, a city or a style (accents
don't matter: *radio clasica* finds *Radio Clásica*). Each station says in a
line why it is worth a listen.

- **Play** listens now without changing your keys. A link you paste is tried
  first: if it is a web page or doesn't answer, the radio says so and keeps
  playing what it was playing.
- **My stations** is your shortlist: tap the star on any station to keep it
  there, add your own stream links, and put them in the order you like. It is
  separate from the keys (a key holds one station; the shortlist can hold
  hundreds). **Recently played** lists the last stations the radio played,
  from a key or from here.
- **Search the internet too**: when a name isn't in the collection (or it
  finds fewer than five), the radio also asks Radio Browser, a free directory
  of some 50,000 stations. Those haven't been checked by us, and are marked
  so.
- **Doesn't play?** on a station tells us it's broken, sounds bad or is
  something else. The note is kept on your radio (in its state folder,
  `reports.jsonl`); nothing is sent anywhere.

Your shortlist is kept on the radio (`/var/lib/haslerd/stations.json`), so
every phone sees the same one, and it survives updates.

### YouTube Music

Search YouTube Music for songs, albums, artists and playlists, or look
through the **charts**, **moods & genres**, your **home** page and (signed
in) your **library**: Liked songs, your playlists, what you played lately,
your artists and your mixes. Open an artist for their top songs, albums and
similar artists; open an album for its tracks.

A key can hold:

| On the key | What it plays |
|---|---|
| A playlist or an album | That list, in order (or shuffled). Tap again: the next song. |
| An artist | Their **top songs**, an endless **artist radio**, or the artist **shuffled**. |
| A song or album **radio** | Music like it, endlessly: the radio keeps adding songs. |
| A **chart** (for example Spain, daily or top 100) | Today's chart each time you press it. |
| A **mood or genre** list | That list, even when YouTube Music swaps it for a new one. |
| **Liked songs** | Your likes (signed in). |

Charts, artists and mood lists are looked up again when you press the key, so
they stay current. If YouTube Music can't be reached, the key plays what it
played last time instead of nothing.

**Like**: while a YouTube Music song plays, the front screen can like it (or
take the like back) for your account. You need to be signed in, and only the
song playing now can be liked. Nothing else in your account is changed.
**Start radio** there starts an endless radio from the song playing.

Songs and albums marked explicit by YouTube show an **E**. To leave them out
of searches and lists altogether, turn on **Hide explicit songs** in the
YouTube Music settings (off to start). It doesn't change what a key already
plays.

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
fade), the **Sleep timer** (with its minutes), or **Join another Hasler**. A
key can also be a [Bluetooth key](#bluetooth).

**Key combinations** (two keys held together, see
[Key combinations](#key-combinations)) are in Edit keys too: pick the two
keys (**Check this pair** to see that the cabinet lets you hold them both),
how long to hold them and what they do, and **Try it** to hear it.

**Start from a set…** and **Reset keys to defaults** are at the bottom of
Edit keys. A starter set puts six (on the Excelsior, seven) stations and
playlists on the keys at once: the sheet shows each set with its keys, and the
one the radio is on now is marked. Reset puts the built-in stations back on
every key (the set you last chose, or the one the radio came with), with keys
1 + 6 for pairing. Both replace every key, so the radio asks for a key press
first. Keys you never changed quietly follow the makers' updates to the set
(a fixed stream, a better station); keys you chose yourself are never touched.

### Settings

Tap the gear. Each row opens its own page, and says under its name what it
holds right now. Anything that needs you (an update ready, a Spotify code
waiting, a key whose Home Assistant target has gone, a Wi-Fi not set up) is
listed first, under **Needs attention**; while that list has something in it,
the gear on the front page carries a small amber dot. Above the rows,
**Something's wrong?** opens a page where the radio checks itself and says
what it found, in plain words, with **Tell the makers** at the bottom (see
[When something is wrong](#when-something-is-wrong)).

Below it, on a radio that is still new to you, **Make it yours**: the choices
that are yours and still untouched, read from the radio itself and listed in
the order of the first evening: how loud it may go, your music on the keys,
the room's name, where it stands, a phone, Spotify, YouTube Music, Home
Assistant, a card to keep by it. Each row opens where the thing is done; it
is done when the radio knows it is, never by ticking. The optional rows have
**Skip**, and **Hide this** puts the whole list under This radio. When every
row is done or skipped the list says *That's it. It's yours.*, the lamp
blooms green and taps OK, and the list goes. For its first week, the front
page carries one line (*New radio? Make it yours*) while the two rows that
matter most, the ceiling and the keys, are open.

| Row | What you find there |
|---|---|
| **Sound** | The **volume ceiling**, the loudest the knob can go (from 30 up to a top the maker set for this speaker, 90 on the Hasler HM; the slider says so). The **quietest step**: the knob's first click. **Mono**: left and right mixed into the one speaker, so nothing is lost (on to start). **What the tone knob does**. At the bottom, **If a knob feels off**: each knob, live, with **Calibrate** (turn the knob to a few positions and the radio learns them). A calibrated knob also has **Back to as shipped**. If a knob reads past its calibration, or its reading wanders while nobody touches it, the card says so: calibrate it. |
| **Keys** | **What the keys play** points to Edit keys (stations, playlists, what holding a key does, key combinations). **What the source key (PU) steps through**, and in what order. **Starting over**: Reset keys to defaults. |
| **Phones** | **Play from a phone**: Spotify (and **Guests on your Wi-Fi can play**), AirPlay, Bluetooth and other music apps (UPnP), each on or off. **Your phones**: what is playing over Bluetooth and its codec, the paired phones, **Pair a phone**, **Reconnect to the last phone**, **Phone volume controls the radio**. **Back to the radio when a phone stops**. **What phones call the radio**: the one name every phone sees (a different name for one service is tucked underneath). |
| **Music accounts** | **Spotify**: the radio's own account, the one Spotify keys play from (**Sign in**, **Change account**, **Sign out**; the page gives a code for spotify.com/pair). **YouTube Music**: your account (sign in for your Liked songs and private playlists; it takes a computer and a few technical steps, which the makers can do for you over remote help), **Hide explicit songs**, and whether the YouTube Music helper is working. |
| **Rooms** | **This room**: the **room name** (with **Remove**) and the radio's address, **Use the room name on phones**, and **Identify this radio** (its lamp taps HI in Morse, in green). **Several rooms**: **Play in several rooms**, what the keys do while this radio follows another, and which key joins from this radio. **The other Haslers**, each with **Identify** (the page says when that radio has no lamp, or needs a software update first). |
| **Pilot lamp** | **Brightness**: low, normal or bright. **Key replies**: the lamp brightens when you press a key (on to start). **At night**: when the lamp dims (*by the sun*, *fixed hours* or *never*), and, for *by the sun*, **where the radio stands**, so it knows when the sun sets. **What the lamp means**: **Now** (what the lamp says right now, in words), the full list folded away, each colour with its rhythm and **Show me** (it plays on the radio's lamp), and **Show me everything**. **Geek mode** and its callsign (off to start). Lamp colours are set by the makers. |
| **This radio** | **Wi-Fi** and the setup network. **Moving this radio**: another room, another home, or a new owner ([Moving the radio](#moving-the-radio)). **Software**: Updates (with **This Wi-Fi is a phone hotspot or capped** and **Tell the makers how the radio is**, the once-a-day check-in, off to start). **How the radio is**: in plain words, its Wi-Fi and signal, whether the internet answers, how warm it is inside, the memory card's free space, how it was last switched off (and how often the power was cut this month), how long it has been running, whether the player and the sound card answer, and the last update - with **Restart the radio** (a press on the radio confirms it). **From the makers**: a note the makers left after a repair, with the date; **Seen** folds it away. **Home Assistant**. [Help from the makers](#help-from-the-makers), with **Tell the makers** and the makers' address. Folded at the very bottom: the other addresses allowed to change this radio. |

The version number is at the bottom of Settings, under the cabinet's name,
and after it the radio's short id (four letters and digits, like `ab12`):
the name the makers know this radio by. Read both out if you phone them.

**How changes are kept.** Every page says, under its title, how its changes
are kept. A single switch, slider or choice works at once, and a small
**Undo** appears for a few seconds in case you didn't mean it; the Undo for a
slider names the old value too. Where you type something (a room name, a name
for phones, where the radio stands), a bar shows **Not saved** with **Cancel**
and **Save**; nothing changes until you press **Save**, and leaving the page
asks first. Connecting Home Assistant and signing in to YouTube Music are
forms with their own button. A few things ask for a key press on the radio
(the next section). When the radio says no to a change, the reason appears
next to the control and the control goes back to how it was; a question that
needs a yes (forget a phone, disconnect Home Assistant, sign out) opens as a
sheet on the page, never as a browser box.

**Maker tools.** There is one more page, for the person who built the radio:
lamp colours, knob curves, the player underneath (moOde), the security
updates switch and diagnostics, with **Copy support bundle** (one file for
the makers: no passwords, tokens or cookies). It stays hidden; tap the
version number seven times to show it. You won't need it.

### "Press any key on the radio"

Some buttons change a lot: installing an update from the page (**Install
tonight**, **Install now**), going back to an older version, changing the update channel, changing or forgetting a Wi-Fi network, resetting the keys,
signing Spotify in or out, letting the makers help, restarting the radio,
handing it to a new owner. For
these the page asks you to **press any key on the radio within 30 seconds**.
That proves someone is standing next to the radio, not just anyone on the
Wi-Fi. (Resetting the keys keeps the old layout for a day: the message
offers **Undo**.)

One honest line about that guard: the player underneath the radio (moOde)
has pages of its own on the radio's address without the `:8080`, and they ask
for no key press. Anyone on your Wi-Fi who knows about them can change the
Wi-Fi there. The radio trusts your home network, as your printer does; if you
keep guests on a separate network, that is enough.

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
standby, what is playing, and whether an update is waiting. Home Assistant
can't install an update: small ones install by themselves at night (if
updates are on Automatic), and the rest need a press on the radio.

Do **not** add Home Assistant's *Music Player Daemon* integration for the
radio, even if it offers one: it fights the radio.

**2. Let a key on the radio run something in your home** (a scene, a light, a
script): connect Home Assistant in **Settings → This radio → Home Assistant**,
then give a key's hold a job in **Edit keys**. The person who built the radio
can help with the first connection; the steps are in the
[build guide](build-guide.md#phase-5--home-assistant).

## Help from the makers

**Tell the makers** first, if you can write: **Settings → Something's wrong?
→ Tell the makers** (also under This radio → Help from the makers). The radio
puts together what the makers usually ask first: the version, what the lamp
says, what's playing, and the last few things that went wrong inside. Nothing
private: no passwords, no accounts, no phone names; untick **Include my Wi-Fi
name** if you'd rather not say it. Add a line about what you noticed, then
**Share…** (WhatsApp, Mail, a message) or **Copy**. If the makers left their
address on the radio, the page names them and **Email the makers** writes the
mail for you. Send it to them, and nobody else. If you only phone, the top of
Something's wrong? shows **Say this to the makers**: six lines to read out.

If the person who built your radio then wants to look at it from afar:
**Settings → This radio → Help from the makers** (at the bottom) → **Let the
makers help (24 h)**, then press a key on the radio. The page shows a link:
send it to the makers, and nobody else. Once they approve it, the page says
when a maker is connected. Help ends by itself after 24 hours, or when you
press **Stop**. The link itself works for one hour; if nobody has come by
then, nothing is lost: press Start again when the makers say they're ready
(abroad, tell them when you'll press it). If the radio restarts during those
24 hours, your yes still stands: the page shows a new link under *The radio
restarted; send the makers this link again*, with no second key press. The
radio has to be on the internet for any of this; the page says so when it
isn't, and a report by message still works. What the makers can see and do:
[Remote help](remote-help.md).

If the radio's keys are dead (the lamp taps SOS for *the keys aren't
answering*), no key can say yes, but the volume knob can: the page asks you
to turn it a little instead. If nothing on the radio can be touched at all,
there is a file on the memory card for it, like the one for Wi-Fi: a plain
text file called **hasler-help.txt** on the **bootfs** drive with the one
line `start`. The radio reads it at the next start, deletes it, starts help,
and writes the link to **hasler-help-result.txt** next to it (delete that
file once the link is sent; it stops working after an hour anyway).

## Wi-Fi

### First time, or a new home

If the radio can't find a Wi-Fi network it knows:

1. Plug it in and wait about 2 minutes. The lamp **taps out amber Morse** (CQ): the
   radio has made its own small Wi-Fi network called **Hasler**.
2. On your phone, join the Wi-Fi network **Hasler**. The person who built the
   radio can give you its password.
3. A page opens by itself. If it doesn't, open **http://172.24.1.1:8080/wifi**
   (any `http://` address works too, for example **http://neverssl.com**).
4. Choose your home network, type its password and press **Connect**. No key
   press is needed here: only someone next to the radio can be on **Hasler**.
5. The radio restarts. After about a minute, put your phone back on your home
   Wi-Fi. The lamp blooms green and taps OK; the music comes back by itself.

If the lamp glows red and then taps CQ again, the password was wrong: join **Hasler**
again and try once more. (If the radio knew another network before, and that
one is in range, it goes back to it instead.)

**After a power cut** the radio may start before your router does and make
the **Hasler** network. You don't need to do anything: every few minutes,
while nobody is on **Hasler**, it looks for your Wi-Fi again and goes back to
it by itself (it checks less often as time goes on, at least every half hour).
To hurry it, unplug the radio and plug it in again once the router is up.

If your Wi-Fi drops out while the radio is on, it keeps trying it; it only
makes the **Hasler** network when it starts.

### Changing networks at home

**Settings → This radio → Wi-Fi → Change**. Choose the new network, then press
a key on the radio when the page asks. If the new network doesn't work, the
radio goes back to the old one when it is in range, and otherwise makes the
**Hasler** network.

### If nothing works: a file on the memory card

1. Unplug the radio and take out its memory card (or ask the person who built
   it). Put the card in a computer: a drive called **bootfs** appears.
2. In it, make a plain text file called **hasler-wifi.txt** with two lines:

   ```
   ssid = the name of your Wi-Fi
   password = its password
   ```

3. Put the card back and plug the radio in. It reads the file, deletes it and
   joins that network. What happened is in **hasler-wifi-result.txt** on the
   same drive.

   The file must be plain text, not rich text (in TextEdit: Format → Make
   Plain Text). The password line can hold spaces and quotes as they are. A
   third line, `hotspot_password = …`, gives the radio's own setup network a
   new password at the same time, should you have lost the old one.

A network cable from the router to the radio's Ethernet socket also works:
the radio is then at **http://hasler.local:8080** and you can set the Wi-Fi
there.

**Forgetting a network.** **Settings → This radio → Moving this radio** lists
the networks the radio knows, each with **Forget** (a key press on the radio).
Forgetting the one it is on restarts it into setup mode, so the page asks
first. Nothing is ever forgotten by itself: a radio that goes back to a home
it knew joins that Wi-Fi again without any help.

**Opening the setup network from a key.** In **Edit keys**, a hold or a
two-key combination can do **Open Wi-Fi setup now**: the radio opens its setup
network without forgetting anything, for when the router's password changed
or a guest network keeps your phone from reaching it. If nobody uses it, the
radio goes back to its Wi-Fi by itself in a few minutes.

## Moving the radio

**Settings → This radio → Moving this radio** is the one page for the three
times a radio changes place. Nothing on it is needed for everyday use.

### Another room

Unplug it, carry it, plug it in: it comes back playing what it played. Then
give it the room's name (**Rename** on that page, or **Settings → Rooms**):
the other rooms, your phones and Home Assistant know the radio by its name.
Not sure which radio is which? **Identify**: its lamp taps HI in green. If a
key runs lights or a scene for the old room, choose new ones in **Edit keys**.
Rooms play together just as before; there is nothing to set up again.

Once the room is named, the radio's setup network is called after it too:
**Hasler Kitchen** rather than plain **Hasler**, so two radios being set up in
one house are told apart. The Wi-Fi page says which radio it belongs to and
has **Blink the lamp**.

### Another home, or a new Wi-Fi

**Before you go, write these down** from the page: the setup network's name
and password (**Show**, or **Print** for the key card). When the radio can't
find your Wi-Fi it opens that network, so a phone can tell it the new one.

**At the new home:** plug it in, wait two minutes for the lamp to tap *CQ*
(long-short-long-short, long-long-short-long), join the setup network on your
phone, and the Wi-Fi page opens by itself ([Wi-Fi](#wi-fi) has the steps).
Music comes back on its own; the lamp blooms green and taps OK. For a day
afterwards Settings says **New Wi-Fi: …** under *Needs attention*, with a
link to this page.

**Afterwards:**

- **Home Assistant.** Key actions name things in the Home Assistant they were
  set up with. In a new home: **Settings → This radio → Home Assistant →
  Disconnect**, then connect the new home's. The page lists every key whose
  target the new one doesn't have — choose new targets, or press **Clear
  missing** to empty them all. (If the radio looks for its old Home Assistant
  and can't find it, the page says so.)
- **Clock and place.** Night dimming by fixed hours, "asleep if the power
  comes back at night" and the hours updates install in all follow the
  radio's clock, and it kept the old home's time zone. **Clock and place** on
  the Moving page (and under **Pilot lamp → At night**) changes it. When Home
  Assistant connects, the page offers once to take its place and time zone
  for both the lamp and the clock.
- The old home's Wi-Fi stays saved, so the radio goes back to it by itself if
  you return. **Forget** it on the page if you'd rather it didn't.

**No phone handy, or it won't join?** The [file on the memory card](#if-nothing-works-a-file-on-the-memory-card)
does the same, and so does a network cable. No internet yet at the new home?
Bluetooth from a phone still works.

### A new owner

**Hand over to a new owner**, then press any key on the radio within 30
seconds. The radio forgets you and keeps only what belongs to the cabinet. It
forgets: your Wi-Fi networks and the setup password (a new one is made and
shown to you once), your Spotify and YouTube Music sign-ins, Home Assistant,
your phones, your keys and stations, the room's name, the other addresses you
allowed, what it was playing, and its log. It keeps: how the knobs are
calibrated, the lamp's colours, the volume ceiling and the quietest step, its
software and updates, and the makers' notes about the cabinet.

Afterwards the page shows, once, the new setup network and password:
**write them down for the new owner**, or print them on a card (**Print a card
for the new owner**). The radio then restarts in setup mode and waits for
them; their first ten minutes are the ones at the top of this guide.

The makers can still help from afar, but only if the new owner asks them to,
with a key press. Their own way in - a key put on the radio when it was
built - stays; remote help needs a press on the radio each time and ends by
itself after 24 hours ([Help from the makers](#help-from-the-makers)).

## Updates

Now and then the makers publish a new version of the radio's software. The
radio can install it by itself. Your keys, settings and knob calibration are
never changed by an update.

Everything is in **Settings → This radio → Updates**.

| Choice | What it means |
|---|---|
| **Automatic** (to start) | Small updates install by themselves at night, while the radio is in standby; nobody needs to press anything. Bigger ones (a new major version, or one that has to move your settings) ask first. |
| **Ask** | The page tells you when there is an update. Choose **Install tonight** (between 2:00 and 5:00) or **Install now**, then press a key on the radio. |
| **Off** | Still looks for updates (the page tells you), but never downloads or installs one. Fixes won't reach the radio. |

Good to know:

- An update only installs while the radio rests: in standby, with no phone
  playing, and nothing played for 10 minutes. **Install now** starts at the
  first such moment, so put the radio to sleep after you press it.
- While it installs (two or three minutes), the lamp **breathes amber**. Leave
  the radio plugged in. When the new version is in, the lamp glows green and
  taps OK.
- Afterwards the radio checks itself. If something doesn't work, it goes back
  to the previous version by itself, even after a power cut, and the page says
  "Didn't work here" and why.
- **To go back by hand:** under Updates, find the older version, press **Go
  back**, then press a key on the radio. The radio restarts on that version.
- Debian's **security updates** install the same way, at night in standby,
  and are taken back if anything is worse afterwards (Updates shows "Security
  updates: 3 on 21 Oct"; the switch is under Maker tools, or `os_updates: off`
  in box.yaml).
- **This Wi-Fi is a phone hotspot or capped**: switch it on under Updates and
  the radio never downloads anything in the background; it still tells you when
  there is an update, and downloads it when you press **Check now**.
- **Tell the makers how the radio is**: off to start. Switched on, the radio
  sends the makers a few lines once a day, after it has looked for updates:
  the version, whether everything answers, and what went wrong inside (a
  fault, a rollback, how often the power was cut). No names, no addresses,
  nothing about what you listen to; **See what it sends** shows exactly
  today's lines. The page says when it was last sent ("Sent: today 04:12 ·
  the makers see this radio as ab12") or that it couldn't send today. The
  makers never see where the radio is, only that one like it needs a look.
- When the page says *No fresh update information for a while*, the radio
  keeps working as it is; only new versions wait.

**Update channel.** Radios follow **Stable**: each version is tried on the
makers' own radio first. The other channel, **Test versions** (*Bench*), is
for the makers' test radio; its versions come first and may break. (Until the
first Stable version is out, the makers' radios run Test versions.) The choice
only shows on the Updates page when [Maker tools](#settings) are on, or while
the radio is on Test versions, because you won't need it. Switching asks
for a press on the radio. Going back from Test versions to Stable, the radio
keeps the version it has until Stable catches up; it never goes back to an older version on its own.

What changed in each version: the
[releases page](https://delgado-murphy.github.io/hasler-releases/) or the
[changelog](../CHANGELOG.md).

## After a power cut

- When the power comes back, the lamp breathes amber while the radio warms up
  (softly, if the power comes back at night). After about 15 seconds the
  music you were playing comes back.
- If the power comes back **at night**, the radio stays asleep (ember) so it
  doesn't wake anyone. Press a key in the morning.
- If the tone knob is clicked off, the radio stays asleep.
- A radio that was following another room comes back quiet, on its own.
- If the music can't come back (no internet, say), the lamp glows red,
  then dim amber. Press a key to try again.
- The radio counts power cuts. **Settings → This radio → How the radio is**
  says how it was last switched off and how often the power went this month;
  if that is often (a plug timer? a wall switch?), the tone knob is the kinder
  way: off for 5 seconds, wait for the lamp to go dark, then unplug if you must.

## When something is wrong

**Start with the page: Settings → Something's wrong?** The radio checks
itself and says what it found, in plain words: what the lamp means right now,
six lines to read out if you phone the makers, then a card for each thing
that is wrong (the memory card is full, the keys aren't answering, no
internet, the station stopped, an update didn't work here, the power was
cut…) with one thing to try. When everything answers, it says so. **Tell the
makers** at the bottom puts a report together to send by message (see
[Help from the makers](#help-from-the-makers)).

**There is no sound.**

1. Look at the lamp. Ember means asleep: press a key or turn the tone knob on.
2. Turn the volume knob up, and check the volume ceiling (**Settings → Sound**).
3. Blue means a phone has the radio. Stop it on the phone, or tap a numbered key.
4. A red glow of the lamp means the music didn't start. Try another key. If one key
   never works, choose new music for it in **Edit keys**.

**The key page doesn't open.** The lamp tells the three cases apart:

- **The lamp is fine and music plays, but the page doesn't open:** your
  phone is on another network. Check it is on the same Wi-Fi as the radio
  (not mobile data, not a guest network; a router with "client isolation"
  keeps phones apart too). Try `http://hasler.local/` without the `:8080`:
  that is the player's own page, and if it opens, the radio is on the
  network and only the key page is the problem. Try the radio's IP address
  instead of `hasler.local`, and wait a minute after the radio starts. A key
  set to **Open Wi-Fi setup now** (see [Wi-Fi](#wi-fi)) is the way to reach a
  radio stuck on the wrong network.
- **The lamp breathes amber and never settles, no music, no page:** the
  radio's software didn't start. Unplug it, wait ten seconds, plug it in. If
  it happens again, tell the makers (after ten minutes of this the lamp taps
  SOS, so you don't have to count); a report can't be made from the page
  then, so phone or write, with the lamp and the version.
- **The lamp taps out amber Morse (CQ):** the radio isn't on your Wi-Fi: see
  [Wi-Fi](#wi-fi).

**Dark after plugging in?** Use the charger and cable that came with it (any
65 W USB-C charger that gives 20 V works; phone chargers often don't).
Unplug, wait ten seconds, plug in: the lamp should breathe amber within five
seconds. Still dark? Call a maker - it's the power board or the memory card.
Nothing inside is dangerous, but don't open it.

**The lamp flashes red twice every few seconds.** The radio has no internet.
Check your router. Bluetooth from a phone still works.

**The lamp taps out red Morse (SOS).** Something inside needs the person who
built your radio (for example the memory card is full, or the panel board
isn't answering). The page says what: at the top of Settings, on the Pilot
lamp page's **Now** line, and under **This radio → How the radio is**. Tell
them. When it clears, Settings says so once.

**A quick look inside.** **Settings → This radio → How the radio is** says in
plain words how the radio is doing: Wi-Fi and signal, internet, temperature,
memory card, how it was last switched off, how long it has run, and whether
the player answers. **Restart the radio** is there too, for when something
feels stuck: like unplugging it, but tidier. It asks for a press on the radio.

**Stations work, but YouTube Music or Spotify don't.**

- YouTube Music: if you signed in, the sign-in may have expired after some
  months. Sign in again in **Settings → Music accounts → YouTube Music**.
  When YouTube changes something, the radio fetches a newer YouTube Music
  helper by itself, usually within hours; the same page says whether it is
  working, and Settings lists it under **Needs attention** while it isn't.
- Spotify on a key needs the radio signed in to a Premium account: see
  **Settings → Music accounts → Spotify**. If it says *Signed in*
  but keys still don't play (the password was changed, or the radio was
  removed in Spotify's account page), tap **Change account** and sign in again.

**My phone won't connect over Bluetooth.** Check that Bluetooth is on in
**Settings → Phones**. If the phone is listed but won't connect,
**Forget** it there, then hold **PU** and pair it again.

**Another Hasler is missing from "Also play on…".** Switch on **Play in
several rooms** on *both* radios (**Settings → Rooms**), and check both are on
the same Wi-Fi (not a guest network). Give it half a minute, then open the
list again.

**My phone shows several "Hasler"s.** Give each radio a room name and switch
on **Use the room name on phones** (see [One name per room](#one-name-per-room)).

**A knob feels wrong** (the volume jumps, or doesn't reach the top). Use
**Settings → Sound → If a knob feels off → Calibrate**.

**An update made something worse.** **Settings → This radio → Updates**, then
**Go back** next to the previous version.

**Nothing helps.** Shut the radio down (tone knob off for 5 seconds), wait
until the lamp is dark, unplug it for 10 seconds and plug it in again. If the
problem stays, tell the person who built your radio: **Settings → Something's
wrong? → Tell the makers** sends them what they need. What the lamp shows,
and the version number and short id at the bottom of Settings, help them a
lot. They may ask you to [let them help](#help-from-the-makers) from afar.

**If the radio has to come back.** Send the makers a report first (Settings →
Something's wrong? → Tell the makers). Pack the charger and its cable with
it; leave the memory card in. Your stations, settings and accounts stay on
the radio; if you'd rather sign out of Spotify and YouTube Music and forget
your phones first, Settings → Music accounts and Settings → Phones are the
places, and the makers can put the keys back from your report. The makers
will leave a note on the page saying what they did (This radio → From the
makers).

**If the makers can't be reached.** The radio doesn't need the makers to
play: stations, playlists, your phone and the other rooms all work on their
own. Without the makers, after about two weeks the Updates page says there
is no fresh update information, and stays that way; the radio keeps the
version it has. YouTube Music keeps mending its own helper. Remote help
needs a maker to answer, so it won't. What you can't do without them is make
a new memory card - which is why the radio came with a spare.

---

<sub>Delgado &amp; Murphy</sub>
