# A new SD card

*For the person setting up a radio's software from scratch: a new SD card for
a finished radio, a spare card, or a second Hasler.* No screen or keyboard is
needed: you prepare the card on a computer, put it in the radio and wait. The
radio sets itself up from a **signed release**, the same one it would get as an
update, and from then on updates itself ([Using your Hasler](using.md#updates)).

Building the cabinet itself? Start with the [build guide](build-guide.md);
this page replaces its software steps once the hardware works.

**What you need:** a computer with [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
(version 2 or later), a microSD card (16 GB or more), your Wi-Fi name and
password, and the radio (Pi 4B with the Amp4 Pro, keys and knobs connected).
It takes about **20 minutes**, most of it waiting.

---

## The short version

1. Raspberry Pi Imager: **moOde audio player** onto the card, with your
   Wi-Fi and the name `hasler`.
2. Copy the **Hasler first-boot files** onto the card.
3. Card into the radio, power on, wait about 20 minutes (it restarts 3-4 times).
4. Open **http://hasler.local:8080**. Set the volume ceiling, then sign in to
   the services you use.

## 1. Write moOde to the card

1. Open Raspberry Pi Imager.
2. **Device:** Raspberry Pi 4. **Operating system:** *Media player OS* →
   **moOde audio player** → the **64-bit Trixie** release (tested: 10.3.6).
   **Storage:** your card.
3. When Imager offers **OS customisation**, choose **Edit settings** and fill in:
   - **Hostname:** `hasler` (the radio is then `hasler.local`).
   - **Username and password:** username `pi`, and a password you keep. moOde
     does not work without a user.
   - **Wi-Fi:** your network's name and password, and your country.
   - **Locale:** your time zone and keyboard layout.
   - **Services:** turn on **SSH**. Makers: add your public key. Everyone
     else: password login is fine.
4. Write the card. When Imager has finished, **take the card out and put it
   back in**: the computer then shows a drive called **bootfs**.

> **Checkpoint:** a drive called *bootfs* opens and holds files such as
> `config.txt` and `cmdline.txt`. (If it also holds a `firstrun.sh`, that is
> Imager's: leave it.)

## 2. Copy the Hasler first-boot files

1. Download **[hasler-firstboot.zip](https://delgado-murphy.github.io/hasler-releases/install/hasler-firstboot.zip)**
   from the releases site.
2. Unzip it. You get six files.
3. Copy all six **to the top of the bootfs drive** (next to `config.txt`, not
   into a folder), then eject the drive.

| File | What it is | Edit it? |
|---|---|---|
| `hasler-install` | the installer | no |
| `hasler.txt` | this radio's settings | only if you want to (below) |
| `vendor-data` | starts the installer at the first start | no |
| `meta-data`, `user-data`, `network-config` | let it run (cloud-init) | no |

The zip is built from a tagged release (`scripts/hasler-install` and
`config/firstboot/`, without `moodecfg.ini`, which the release brings itself).
You don't need to trust the download itself: `hasler-install` checks every
signature from the root key it carries before it changes anything. Makers can
also check the installer first with `install/hasler-install.sig` (see "For
makers" below); `install/SHA256SUMS` lists the hashes.

**If the computer says a file already exists** (`meta-data`, `user-data` or
`network-config`): **keep the one on the card** (*Skip* / *Don't replace*). It
is Imager's, and it holds your user and Wi-Fi. Today Imager writes none of
them for moOde, so this should not happen.

**hasler.txt** is a short text file; the defaults suit a finished radio:

| Setting | Default | Other choices |
|---|---|---|
| `channel` | `stable` | `bench` only for the makers' test radio. Stable has been live since 1.0.5 (2026-10-23). |
| `update_mode` | `auto` (small updates at night, big ones ask) | `ask`, `off` |
| `variant` | `hasler-hm` | `excelsior` |
| `user` | `pi` | the username you gave Imager |
| `moode` | `yes` (Hasler's audio and casting settings in moOde) | `no` |
| `panel_optional` | `no` | `yes` on a bare Pi without the key board |

Nothing on the card is secret: there are no passwords in these files.

Eject the bootfs drive.

> **Checkpoint:** bootfs holds `hasler-install`, `hasler.txt`, `vendor-data`,
> `meta-data`, `user-data` and `network-config` next to `config.txt`.

## 3. First start: wait

Put the card in the radio and switch it on. **Don't unplug it for about 20
minutes.** It restarts several times by itself:

| | What happens | Roughly |
|---|---|---|
| 1 | Imager's settings: user, name, Wi-Fi, SSH; restart | 2 min |
| 2 | moOde's first start; it restarts once or twice more | 3 min |
| 3 | **Hasler setup:** downloads the release and checks its signatures, installs packages (AirPlay, multiroom, remote help, yt-dlp), the updater and its trust, writes this radio's settings, hands moOde its audio settings; restart | 8-12 min |
| 4 | moOde takes the audio settings (Amp4 Pro, AirPlay, Bluetooth, UPnP, names "Hasler"); restart | 1 min |
| 5 | **The release:** the updater installs it and restarts into it | 2 min |
| 6 | The updater checks the radio is healthy and keeps the release | 1 min |

The pilot lamp stays dark until stage 5; from then on it **breathes amber**
while the radio starts, as it always will.

> **Checkpoint:** after about 20 minutes, **http://hasler.local:8080** opens
> the key page, and **Settings → This radio → Updates** shows a version. Press
> key 1: a station plays.

If the key page doesn't open after 30 minutes, see [If it goes wrong](#if-it-goes-wrong).

## 4. After the install: what still needs you

The install sets up everything a release can. A few things belong to a person
and an account, so the radio can't do them alone:

| What | Where | Why it needs you |
|---|---|---|
| **Volume ceiling** - do this first | key page → Settings → **Sound** → Volume ceiling | protects the 1964 speaker; set by ear ([build guide, Phase 3a](build-guide.md#phase-3a--keys-drive-the-music)) |
| **Your stations and playlists** | key page → **Edit keys** | the shipped keys are a test set |
| **Spotify** (Premium) | key page → Settings → Phones & casting → Spotify Connect → **Sign in**, then enter the code at spotify.com/pair ([Using your Hasler](using.md#playing-from-your-phone)) | your Spotify account |
| **YouTube Music** private playlists, Liked songs | key page → Settings → This radio → YouTube Music → **Sign in** | your Google sign-in; public playlists need nothing |
| **Home Assistant** (optional) | key page → Settings → This radio → Home Assistant → paste a token | a token from your HA ([build guide, Phase 5](build-guide.md#phase-5--home-assistant)) |
| **Knob calibration** (if the knobs feel off) | key page → Settings → Keys & knobs → **Calibrate** | turning the real knobs |
| **Room name** (two or more Haslers) | key page → Settings → This radio | yours to choose |
| **Hotspot password** | `curl -s hasler.local:8080/api/wifi` → `setup.password` | the setup hotspot "Hasler" gets a random one on the first start; write it down for the owner, or choose your own in moOde → Network → Access Point ([build guide](build-guide.md#moving-the-radio-to-a-new-wi-fi-network)) |

Spotify casting from a phone (*Hasler* in the Spotify app's device list),
AirPlay, Bluetooth and UPnP work without any of this.

---

## Notes for makers

### The SSH path: one command

Already have a moOde card that boots (Imager customisation done, `ssh
pi@hasler.local` works)? Then install over SSH instead of the first-boot files.
The installer is checked **before** it runs:

```sh
# on the Pi
curl -fsSLO https://delgado-murphy.github.io/hasler-releases/install/hasler-install
curl -fsSLO https://delgado-murphy.github.io/hasler-releases/install/hasler-install.sig
echo 'hasler-root namespaces="hasler-install" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIF9gmAm746rpYzKa24jKiAZyiAiMw0bEuj30vRw38Cv7' > hasler-root.allowed
ssh-keygen -Y verify -f hasler-root.allowed -I hasler-root -n hasler-install -s hasler-install.sig < hasler-install \
  && sudo python3 -I hasler-install install          # stable; --channel bench for a test radio
```

`ssh-keygen` prints `Good "hasler-install" signature for hasler-root` and only
then does the installer run. It does the same steps as the first boot, in the
foreground, then restarts the Pi; `hasler-install.service` finishes by itself
after each restart (`journalctl -u hasler-install -f` to watch,
`sudo hasler-install status` to see where it is). Options: `--channel bench`,
`--user NAME`, `--variant excelsior`, `--panel-optional`, `--no-moode`
(leave moOde's settings alone), `--yes`.

Until the signed installer is published (see [Open points](#open-points)),
take `scripts/hasler-install` from a tagged checkout of this repository
instead, and check that it pins the root key below.

### Trust: the root key

The installer has one key built in, the **Hasler root key**, and trusts nothing
else. Its fingerprint:

    SHA256:8bc/MCrWk9aekyhNhdtmPtL9jhgB7xcdStuu4G82Ack   (ED25519, hasler-root-2026)

Check it on any copy of `hasler-install` (or of `trust/root.pub` in the
`hasler-releases` repository):

```sh
grep '^ROOT_PUB' hasler-install | cut -d'"' -f2 | ssh-keygen -lf -
```

What it checks, in order, before anything downloaded is run
([ota-format.md](ota-format.md)):

1. `keyring.json` is signed by the root key (namespace `hasler-keyring`) and
   not expired;
2. `channels/<channel>.json` is signed by a timestamp key from that keyring
   (`hasler-channel`), is for the channel asked for, is fresh (14 days) and
   its current release is not halted;
3. `release.json` has the SHA-256 the index names and is signed by a release
   key (`hasler-release`);
4. the tarball has the size and SHA-256 `release.json` names;
5. every file in it matches its `files.sha256` (and `files.sha256` matches
   `release.json`), with no links, devices or paths outside the tree;
6. the release fits this Pi (aarch64, trixie, cp313, moOde 10.x, the variant).

Only then does it run the release's own `scripts/bootstrap.sh` and
`scripts/ota-bootstrap.sh` (carried in the release under `app/setup/`).
After that, the release's **updater** stages and installs it with its own,
complete checks, exactly as it installs every later update - the installer
never puts a release in place itself.

### What the first boot does, step by step

- **cloud-init** (part of the moOde image) finds `meta-data` on the boot
  partition and runs `vendor-data` once. `user-data` and `network-config` tell
  it to leave users, hostname, locale and network alone: Imager's
  `firstrun.sh` does those. `vendor-data` runs `hasler-install hook`, which
  copies the installer to `/usr/local/sbin`, writes and enables
  `hasler-install.service` and starts it.
- **hasler-install.service** (`Type=exec`, so it never holds the boot that
  moOde's worker waits for) runs `hasler-install resume` after every boot
  until it is done:
  1. waits for the user from Imager, for moOde's worker ("Startup complete")
     and for the network;
  2. downloads and verifies the channel's release (above) into
     `/var/lib/hasler-install/work/`;
  3. `apt-get update`, then the release's `bootstrap.sh --user pi` (packages:
     zstd, liblgpio1, polkitd, libasound2-plugins, shairport-sync, snapserver
     and snapclient with Debian's units masked, tailscale with tailscaled off;
     I²C; users `hasler-update` and `ytdlp`; yt-dlp; the hotspot's captive
     portal; moOde's cover-art patch; the boot-time trims);
  4. the release's `ota-bootstrap.sh --trust` (the updater and its units, the
     polkit rule, the hardware watchdog, `/etc/hasler/trust`);
  5. writes `/etc/hasler/box.yaml` (schema 1, variant, channel, update mode,
     a new `box_id`; migration 001 marked done, since there is no old
     checkout) and, with `panel_optional = yes`, a calibration overlay with
     `io: {panel_optional: true}`;
  6. copies the release's `config/firstboot/moodecfg.ini` to
     `/boot/moodecfg.ini` (moOde 10 reads it there, **not** from the FAT
     partition) and restarts. moOde's worker applies it at its next start
     (`i2soverlay = hifiberry-amp4pro`, hardware volume, `volume_mpd_max 67`,
     AirPlay / Bluetooth / UPnP on, moOde's Spotify off, names "Hasler";
     its log is `/var/log/moode_autocfg.log`), deletes it and restarts;
  7. starts `hasler-update-check.service` (stages the release) and runs
     `hasler-update apply --seq N --force --allow-reboot`: the updater puts
     the system files in place, writes the config.txt lamp block and
     restarts into the trial; after that boot it checks the radio's health and
     commits - or rolls back, and the installer stops and says so;
  8. marks `/var/lib/hasler-install/done` and disables itself.
- Progress goes to the journal (`journalctl -u hasler-install`) and to
  **`hasler-install.log` on the boot partition**, which any computer can read
  if the radio never comes up.

The kit files on the boot partition can stay; cloud-init runs them only once
(and `bootstrap.sh` turns cloud-init off once the Wi-Fi profiles allow it).

### If it goes wrong

- **No `hasler.local` after 30 minutes.** Switch off, put the card in a
  computer and read `hasler-install.log` on bootfs. No log at all: the
  first-boot files were not found (check they sit next to `config.txt`), or
  Imager's settings did not take (no Wi-Fi: see below).
- **No Wi-Fi** (Imager's Wi-Fi name or password mistyped). Before the
  install there is **no** setup hotspot: moOde's import of Imager's Wi-Fi
  leaves the hotspot without a password, and moOde can't start it then. Either
  plug a network cable into the Pi (it is then `hasler.local` over Ethernet
  and the install carries on), or write the card again with the right Wi-Fi.
  After the install (haslerd gives the hotspot a password) the radio's own Wi-Fi page
  does this ([Using your Hasler](using.md#wi-fi)), and `hasler-wifi.txt` on
  bootfs works from then on, on a radio installed from 1.0.1 or later
  ([build guide](build-guide.md#moving-the-radio-to-a-new-wi-fi-network)).
  The installer waits for the network and carries on by itself.
- **"there is no 'stable' channel yet"**: no release has been promoted to
  stable yet; that was the case before 1.0.5 was promoted on 2026-10-23.
- **"this release cannot set up a new radio"**: releases before 1.0 do not
  carry `app/setup/`; wait for one that does.
- **"bad signature"** or **"does not match"**: something between the release
  site and the radio is not what the makers signed. Nothing was installed.
  Tell the makers.
- **"rolled back"**: the release failed its health check on this radio
  (`sudo hasler-update status` says why) and the updater will not try that
  release again here. Most often the key board is not connected. Fix the
  cause, then write the card again (step 1), with `panel_optional = yes` for
  a bare Pi.
- **Start again from the top:** write the card again (step 1).

### Testing it

- `hasler-install fetch --channel bench --out DIR` downloads, verifies and
  unpacks a release and lays out its setup tree, changing nothing else; it
  runs on any Linux or macOS with Python 3.11+, `ssh-keygen` and `zstd`.
  `--base file:///site --root-pub dev-root.pub` works against a dev channel
  ([release.md, "Bench box"](release.md#bench-box-from-the-rsync-tree-to-releases-dev-keys-local-channel)).
- `update/tests/test_hasler_install.py` builds a real release, publishes it on
  a dev channel, fetches it, runs the setup tree's `ota-bootstrap.sh` on a fake
  box and lets the real updater stage it; plus every refusal above.

### Open points

- **Re-publishing the kit when the installer changes** (done for 1.0.5 on
  2026-10-23): build it from the tag, sign `hasler-install` **offline with the
  root key** (`ssh-keygen -Y sign -f hasler-root-2026.key -n hasler-install
  hasler-install`), zip the six files, and put `hasler-firstboot.zip`,
  `hasler-install`, `hasler-install.sig` and `SHA256SUMS` into `install/` on
  the gh-pages branch of hasler-releases.
- Raspberry Pi Imager lists moOde 10.3.6 with `init_format: systemd`
  (firstrun.sh). If a later moOde image switches to cloud-init, Imager writes
  its own `meta-data` / `user-data` / `network-config`: then copy only
  `hasler-install`, `hasler.txt` and `vendor-data`.

<sub>Delgado &amp; Murphy</sub>
