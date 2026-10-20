"""Accept Bluetooth pairing requests while the pairing window is open.

moOde's agent (bt-agent) uses Numeric Comparison: the phone shows a code and
the agent waits for a yes from moOde's web page, which nobody watches on a
Hasler - so pairing hung and timed out ("Cancel" after ~35 s). While the
owner's pairing window is open, this watches the agent's log for requests
and answers yes through the root helper hasler-bt-accept. Outside the window
nothing answers, and moOde's own page still can.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/hasler-bt-accept"
# "confirm(/org/bluez/hci0/dev_B0_..) code=921218 -> req b5039031"
REQUEST = re.compile(r"^(confirm|authorize)\((\S+)\) code=\S* -> req ([0-9a-f]{8})\s*$")


def parse_request(line: str) -> tuple[str, str, str] | None:
    """(method, device path, request id) from one agent log line, else None."""
    m = REQUEST.match(line.strip())
    return (m.group(1), m.group(2), m.group(3)) if m else None


class PairAcceptor:
    """One watcher thread per open window; a new window extends it."""

    def __init__(
        self,
        follow: Callable[[], subprocess.Popen] | None = None,
        accept: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._follow = follow or self._journal
        self._accept = accept or self._run_helper
        self._clock = clock
        self._lock = threading.Lock()
        self._until = 0.0
        self._thread: threading.Thread | None = None
        self.accepted: list[str] = []

    def open(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, self._clock() + seconds)
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._watch, name="bt-accept", daemon=True)
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._until = 0.0

    def _open_now(self) -> bool:
        with self._lock:
            return self._clock() < self._until

    def _watch(self) -> None:
        try:
            proc = self._follow()
        except OSError as exc:
            log.warning("can't watch moOde's pairing agent: %s", exc)
            return
        stopper = threading.Thread(target=self._stop_when_closed, args=(proc,), daemon=True)
        stopper.start()
        try:
            for line in proc.stdout:
                if not self._open_now():
                    break
                req = parse_request(line)
                if req is None:
                    continue
                method, device, req_id = req
                if self._accept(req_id):
                    self.accepted.append(req_id)
                    log.info("bluetooth pairing accepted (%s %s)", method, device.rsplit("/", 1)[-1])
        finally:
            try:
                proc.kill()
            except OSError:
                pass

    def _stop_when_closed(self, proc: subprocess.Popen) -> None:
        while self._open_now() and proc.poll() is None:
            time.sleep(0.5)
        try:
            proc.kill()
        except OSError:
            pass

    @staticmethod
    def _journal() -> subprocess.Popen:
        # pi is in group adm, so it may read the system journal.
        return subprocess.Popen(
            ["journalctl", "-u", "bt-agent", "-f", "-n", "0", "-o", "cat"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    @staticmethod
    def _run_helper(req_id: str) -> bool:
        try:
            result = subprocess.run(["sudo", "-n", HELPER, req_id], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("could not accept the pairing request: %s", exc)
            return False
        if result.returncode != 0:
            log.warning("could not accept the pairing request: %s", (result.stderr or "").strip()[-200:])
            return False
        return True
