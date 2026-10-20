"""hasler-update: the box side of over-the-air releases (docs/ota.md §3-4).

Stdlib only - it must run on the system Python with no release venv, because
it is what repairs a release whose venv is broken. The one outside tool is
``ssh-keygen`` (openssh-client) for signature checks, plus ``zstd`` when the
running Python has no ``compression.zstd``.

Privilege split:

* ``check`` / ``stage`` run as the unprivileged ``hasler-update`` user: fetch,
  verify (signatures, keyring, expiry, seq, compatibility), extract safely
  into ``/var/lib/hasler-update/staged/<seq>/``.
* ``apply`` / ``rollback`` / ``commit`` / ``boot`` run as root. Apply trusts
  nothing staged: it re-verifies the signatures with the root-owned keyring,
  copies the tree into a root-owned place and re-hashes it there before
  anything is used.

Every path is under ``HASLER_ROOT`` (default ``/``) so all of it runs against
a fake root on a laptop.
"""

__version__ = "0.1.0"
