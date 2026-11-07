# Security

A Hasler sits on a home network, installs updates by itself and can, when its
owner says so, let the makers in for remote help. If you find a way to break
any of that, please tell us privately first.

## How to report

- **Privately on GitHub:** the repository's **Security** tab → **Report a
  vulnerability**.
- **Or by email:** murphy@litio.net, subject "Hasler security".

Please **don't open a public issue**. Say what you found, which version
(Settings → This radio → Updates, or the tag), and how to reproduce it. We
answer within a week, and tell you when a fix is out. Credit is yours if you
want it.

## What matters most

- The update chain: release signing, the keyring, channel indexes, the
  updater on the radio ([ota.md](docs/ota.md), [ota-format.md](docs/ota-format.md)).
- Remote help: the owner's yes, the 24-hour limit, the tailnet rules
  ([remote-help.md](docs/remote-help.md)).
- The check-in Worker and **Send to the makers** (`tools/checkin/`): what a
  radio sends and who can read it.
- The key page and its API on the home network, and the root helpers it
  calls through sudoers (`scripts/hasler-*`, `config/sudoers/`).

The key page has no login by design: it trusts the home network, like
moOde's own pages. A report that only says "the page has no password" is
known; one that reaches past the home network, or past the press on the radio
that risky changes ask for, is very welcome.

## Supported versions

Only the newest **Stable** release gets fixes. Radios update themselves, so
a fix usually reaches them the next night.

---

<sub>Delgado &amp; Murphy</sub>
