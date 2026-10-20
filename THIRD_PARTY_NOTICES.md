# Third-party notices

Hasler's own code is MIT licensed ([LICENSE](LICENSE)). A Hasler release
tarball (`hasler-<version>-arm64.tar.zst`) also carries other people's work,
listed here with its licence. This file, `LICENSE` and the `licenses/` folder
sit at the root of every release (`/opt/hasler/current/` on a radio).

Parts of this repository that are not MIT:

| Path | Licence | Why |
|---|---|---|
| `config/moode/patch-ytm-art.py`, `config/moode/ytm-art.php` | GPL-3.0-or-later | They modify moOde audio player (GPL-3.0-or-later); see [config/moode/LICENSE](config/moode/LICENSE) |
| `docs/hat/` (HAT hardware design files) | CERN-OHL-P-2.0 | Hardware; see [docs/hat/LICENSE](docs/hat/LICENSE) (not shipped in releases) |

## Programs in `bin/`

Shipped unmodified, as separate programs that haslerd starts; they are not
linked into Hasler's code.

| Program | Version | Licence | Source | Licence text |
|---|---|---|---|---|
| [go-librespot](https://github.com/devgianlu/go-librespot) | v0.10.3 (`go-librespot_linux_arm64.tar.gz`) | GPL-3.0 | [tag v0.10.3](https://github.com/devgianlu/go-librespot/tree/v0.10.3), [source archive](https://github.com/devgianlu/go-librespot/archive/refs/tags/v0.10.3.tar.gz) | [licenses/GPL-3.0.txt](licenses/GPL-3.0.txt) |
| [Deno](https://github.com/denoland/deno) | v2.9.7 (`deno-aarch64-unknown-linux-gnu.zip`) | MIT, Copyright 2018-2026 the Deno authors | [tag v2.9.7](https://github.com/denoland/deno/tree/v2.9.7) | [licenses/MIT-deno.txt](licenses/MIT-deno.txt) |

**go-librespot (GPL-3.0).** The binary is the upstream release asset for
v0.10.3, byte for byte (its SHA-256 is pinned in `scripts/install.sh`); its
Corresponding Source is the upstream tag above. If that source ever stops
being available there, ask for it by opening an issue at
<https://github.com/Delgado-Murphy/hasler> and we will provide it, for as long
as we distribute that binary and at least three years after. The Go modules
compiled into it are listed in its `go.mod` at that tag, under their own
licences.

**Deno (MIT).** The binary also contains V8 and other components compiled in
by the Deno project, under their own (permissive) licences; see the Deno
repository at the tag above.

## Fonts embedded in the key page

`app/haslerd/web/static/index.html` embeds these fonts (WOFF2, latin subset,
as `data:` URLs) so the page works without a network. Both are under the SIL
Open Font License 1.1; the fonts are subsetted but not otherwise modified, and
keep their original names.

| Font | Styles embedded | Copyright | Licence text |
|---|---|---|---|
| [Fraunces](https://github.com/undercasetype/Fraunces) | 600, 700, 600 italic | Copyright 2018 The Fraunces Project Authors | [licenses/OFL-1.1-Fraunces.txt](licenses/OFL-1.1-Fraunces.txt) |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | 400 | Copyright 2020 The JetBrains Mono Project Authors | [licenses/OFL-1.1-JetBrains-Mono.txt](licenses/OFL-1.1-JetBrains-Mono.txt) |

## Python wheels in `wheelhouse/`

Installed into the radio's virtualenv from `requirements.lock`. Licences as
declared in each wheel's metadata (`*.dist-info/METADATA`); every wheel also
carries its own licence file (with the copyright notice) in its `.dist-info`,
which pip installs alongside the package. Versions are those of the current
lock and move with it.

| Package | Version | Licence | Needed by | Licence text |
|---|---|---|---|---|
| [PyYAML](https://pypi.org/project/PyYAML/) | 6.0.3 | MIT | haslerd | in the wheel |
| [gpiozero](https://pypi.org/project/gpiozero/) | 2.0.1.post3 | BSD-3-Clause | haslerd | in the wheel |
| [colorzero](https://pypi.org/project/colorzero/) | 2.0 | BSD-3-Clause | gpiozero | in the wheel |
| [lgpio](https://pypi.org/project/lgpio/) | 0.2.2.0 | Unlicense (public domain) | haslerd | in the wheel |
| [smbus2](https://pypi.org/project/smbus2/) | 0.6.1 | MIT | haslerd | in the wheel |
| [python-mpd2](https://pypi.org/project/python-mpd2/) | 3.1.1 | LGPL-3.0 | haslerd | [licenses/LGPL-3.0.txt](licenses/LGPL-3.0.txt) (+ [GPL-3.0](licenses/GPL-3.0.txt), which it builds on) |
| [paho-mqtt](https://pypi.org/project/paho-mqtt/) | 2.1.0 | EPL-2.0 OR BSD-3-Clause (EDL-1.0); we use it under EDL-1.0 | haslerd | in the wheel; [licenses/EPL-2.0.txt](licenses/EPL-2.0.txt) |
| [ytmusicapi](https://pypi.org/project/ytmusicapi/) | 1.12.3 | MIT | haslerd | in the wheel |
| [requests](https://pypi.org/project/requests/) | 2.34.2 | Apache-2.0 | ytmusicapi | [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt) (its NOTICE is in the wheel) |
| [urllib3](https://pypi.org/project/urllib3/) | 2.8.0 | MIT | requests | in the wheel |
| [charset-normalizer](https://pypi.org/project/charset-normalizer/) | 3.5.2 | MIT | requests | in the wheel |
| [idna](https://pypi.org/project/idna/) | 3.20 | BSD-3-Clause | requests | in the wheel |
| [certifi](https://pypi.org/project/certifi/) | 2026.10.18 | MPL-2.0 | requests | [licenses/MPL-2.0.txt](licenses/MPL-2.0.txt) |
| [setuptools](https://pypi.org/project/setuptools/) | 84.2.0 | MIT (vendored parts: MIT, Apache-2.0 OR BSD-2-Clause; files in the wheel) | colorzero | in the wheel |

python-mpd2 (LGPL-3.0) and certifi (MPL-2.0) ship as unmodified pure-Python
source, so their source is in the wheel itself; it is also on PyPI at the
versions above. Hasler only imports them; you may replace them with any
compatible version in the radio's virtualenv.

lgpio's C library (`liblgpio1`) is not in the wheel: it comes from Debian's
package of that name (`requires.apt` in `release.json`).

## Not in the release

moOde audio, MPD, shairport-sync, CamillaDSP, Snapcast, BlueZ and the rest of
Raspberry Pi OS come from their own images and Debian packages under their own
licences. yt-dlp (Unlicense) is installed on the radio from PyPI by
`hasler-ytdlp-update`, not shipped in the release.

## `licenses/`

Full texts of the licences above that require their text to travel with the
software, from the canonical sources (gnu.org, mozilla.org, apache.org,
eclipse.org, and the upstream repositories for the font and Deno notices):
`GPL-3.0.txt`, `LGPL-3.0.txt`, `MPL-2.0.txt`, `EPL-2.0.txt`, `Apache-2.0.txt`,
`MIT-deno.txt`, `OFL-1.1-Fraunces.txt`, `OFL-1.1-JetBrains-Mono.txt`.
