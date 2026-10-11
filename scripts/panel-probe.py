#!/usr/bin/env python3
"""Live view of the panel. Build the wiring against this, not against the daemon.

This exists because the last two attempts at this project failed in ways that
were invisible: a Volumio plugin and an ESPHome device that each half-worked
with no way to see why. You cannot debug a panel you cannot see.

Run it on the Pi while wiring:

    scripts/panel-probe.py --variant config/variants/hasler-hm.yaml

Or on a laptop with no hardware at all, keys 0-9 toggling inputs by hand:

    scripts/panel-probe.py --fake

What to look for in Phase 2
---------------------------
* Every key lights its own row and nobody else's  (no ghosting)
* The BOUNCE column stays in low single digits per press. Tens of transitions
  means dirty contacts or a missing 100 nF - fix it in hardware, here, before
  any of it becomes the daemon's problem.
* Pots sweep monotonically end to end with no dead spots.
"""

from __future__ import annotations

import argparse
import select
import sys
import termios
import time
import tty
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "daemon"))

from haslerd.variant import Panel, gpio_mapping, load_variant  # noqa: E402

def wire_map(variant) -> dict[str, str]:
    """Logical pin -> where it is physically wired, for the WIRE column.

    gpio: ``dN`` is ``pins[N]`` (by index - see variant.gpio_mapping).
    mcp23017: the name *is* the wiring - d0-d7 = PA0-PA7, d8-d15 = PB0-PB7.
    """
    digital = variant.io.get("digital") or {}
    names = variant.pin_names()
    if digital.get("backend") == "gpio":
        try:
            return {n: f"GPIO {g}" for n, g in gpio_mapping(names, digital.get("pins") or []).items()}
        except ValueError:
            return {}
    if digital.get("backend") == "mcp23017":
        return {n: (f"PA{int(n[1:])}" if int(n[1:]) < 8 else f"PB{int(n[1:]) - 8}") for n in names}
    return {}


CLEAR, HOME, HIDE, SHOW = "\033[2J", "\033[H", "\033[?25l", "\033[?25h"
DIM, BOLD, GREEN, YELLOW, RESET = "\033[2m", "\033[1m", "\033[32m", "\033[33m", "\033[0m"


def identify(variant, panel, gpio_map) -> int:
    """Ask for each key by name and record which GPIO it actually lands on.

    You wired eight cables into eight pins in whatever order was convenient.
    Rather than tracing them, actuate each one and let the Pi work it out. The
    control->logical-pin assignments in the descriptor stay put; only the
    `io.digital.pins` array gets reordered, so nothing else in the file moves.
    """
    labels = {
        "k1": "key 1", "k2": "key 2", "k3": "key 3",
        "k4": "key 4", "k5": "key 5", "k6": "key 6",
        "pu": "the PU (pick-up) key",
        "power": "the TONE knob fully clockwise (past the click)",
    }
    wanted: list[tuple[str, str]] = []
    for spec in variant.controls:
        if pin := spec.get("pin"):
            wanted.append((spec["id"], pin))
        if end := spec.get("end_switch"):
            wanted.append((end.get("id", f"{spec['id']}_switch"), end["pin"]))

    print(f"\n{BOLD}Identifying {len(wanted)} inputs on '{variant.name}'{RESET}")
    print(f"{DIM}Actuate each one when asked. Ctrl-C to stop.{RESET}\n")

    found: dict[str, int] = {}
    used: set[str] = set()
    for cid, logical in wanted:
        prompt = labels.get(cid, cid)
        while True:
            sys.stdout.write(f"  {BOLD}{cid:<8}{RESET} press {prompt} ... ")
            sys.stdout.flush()
            pin = _wait_for_close(panel, exclude=used)
            if pin is None:
                print(f"{YELLOW}timeout - skipped{RESET}")
                break
            gpio = gpio_map.get(pin)
            if gpio is None:
                print(f"{YELLOW}{pin} has no GPIO in io.digital.pins{RESET}")
                break
            print(f"{GREEN}{pin} = GPIO {gpio}{RESET}")
            found[logical] = gpio
            used.add(pin)
            _wait_for_open(panel)
            break

    print(f"\n{BOLD}Paste this into {variant.path}:{RESET}\n")
    top = max(int(n[1:]) for n in variant.pin_names())
    ordered = [found.get(f"d{i}") for i in range(top + 1)]
    if any(v is None for v in ordered):
        print(f"{YELLOW}  # incomplete - unidentified inputs left as null{RESET}")
    print("  digital:")
    print("    backend: gpio")
    print(f"    pins: [{', '.join('null' if v is None else str(v) for v in ordered)}]\n")
    return 0


def _wait_for_close(panel, exclude, timeout=30.0, settle=0.08):
    """Block until exactly one un-claimed pin is closed and stays closed."""
    deadline = time.monotonic() + timeout
    candidate, since = None, 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        panel.poll(now)
        closed = [p for p, v in panel.raw()[0].items() if v and p not in exclude]
        current = closed[0] if len(closed) == 1 else None
        if current != candidate:
            candidate, since = current, now
        elif candidate is not None and now - since >= settle:
            return candidate
        time.sleep(0.01)
    return None


def _wait_for_open(panel, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        panel.poll(time.monotonic())
        if not any(panel.raw()[0].values()):
            return
        time.sleep(0.01)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variant", default=str(ROOT / "config/variants/hasler-hm.yaml"))
    ap.add_argument("--fake", action="store_true", help="in-memory backends; 0-9 toggle pins")
    ap.add_argument("--rate", type=float, default=50.0, help="poll Hz (default 50)")
    ap.add_argument(
        "--identify",
        action="store_true",
        help="walk through each key and print the io.digital.pins array to match your wiring",
    )
    args = ap.parse_args()

    variant = load_variant(args.variant)
    backend = (variant.io.get("digital") or {}).get("backend")
    if args.identify and backend != "gpio":
        # On the expander the logical name IS the pin (d0 = PA0, d8 = PB0), so
        # there is no array to work out - wire by the silkscreen and check here.
        print(f"--identify only applies to backend: gpio (this descriptor uses {backend}).")
        print("Wire each contact to the pin shown in the WIRE column, then run without")
        print("--identify and press each key: it must light its own row and no other.")
        return 0

    panel = Panel.from_variant(variant, fake=args.fake)
    if args.identify:
        gmap = {n: int(w.split()[1]) for n, w in wire_map(variant).items()}
        try:
            return identify(variant, panel, gmap)
        except KeyboardInterrupt:
            return 0
        finally:
            panel.close()
    wires = wire_map(variant)

    last_raw: dict[str, bool] = {}
    bounces: dict[str, int] = {}
    log: list[str] = []
    period = 1.0 / args.rate

    old_term = None
    if args.fake and sys.stdin.isatty():
        old_term = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())

    sys.stdout.write(CLEAR + HIDE)
    started = time.monotonic()
    try:
        while True:
            now = time.monotonic()

            if args.fake and sys.stdin.isatty():
                while select.select([sys.stdin], [], [], 0)[0]:
                    ch = sys.stdin.read(1)
                    if ch in ("q", "\x03"):
                        return 0
                    if ch.isdigit():
                        pin = f"d{int(ch)}"
                        if pin in panel.digital.state:
                            panel.digital.set(pin, not panel.digital.state[pin])

            for event in panel.poll(now):
                log.append(f"{now - started:7.2f}s  {event}")
            del log[:-12]

            raw_d, raw_a = panel.raw()
            for pin, value in raw_d.items():
                if pin in last_raw and last_raw[pin] != value:
                    bounces[pin] = bounces.get(pin, 0) + 1
            last_raw = dict(raw_d)

            out = [HOME, f"{BOLD}panel-probe{RESET}  {variant.name}"]
            backend = variant.io.get("digital", {}).get("backend", "fake")
            out.append(f"{DIM}{'fake backends' if args.fake else backend}"
                       f"  ·  {args.rate:.0f} Hz  ·  q to quit{RESET}\n")

            out.append(f"{BOLD}{'PIN':<5}{'WIRE':<9}{'STATE':<9}{'TRANSITIONS':<13}{RESET}")
            for pin in variant.pin_names():
                closed = raw_d.get(pin, False)
                mark = f"{GREEN}● closed{RESET}" if closed else f"{DIM}○ open  {RESET}"
                n = bounces.get(pin, 0)
                colour = YELLOW if n > 40 else DIM
                out.append(f"{pin:<5}{wires.get(pin, '-'):<9}{mark}  {colour}{n:<11}{RESET}")

            if raw_a:
                out.append(f"\n{BOLD}{'CH':<5}{'COUNTS':<10}{'SCALED':<10}{RESET}")
                scaled = {c.id: c.state() for c in panel.controls if hasattr(c, "channel")}
                by_channel = {c.channel: c.id for c in panel.controls if hasattr(c, "channel")}
                for ch, counts in sorted(raw_a.items()):
                    name = by_channel.get(ch, "?")
                    value = scaled.get(name)
                    out.append(f"{ch:<5}{counts:<10}{'-' if value is None else value:<10}{DIM}{name}{RESET}")
            else:
                out.append(f"\n{DIM}no ADC fitted (io.analog.backend: none){RESET}")

            out.append(f"\n{BOLD}CONTROL STATE{RESET}")
            for cid, state in panel.states().items():
                out.append(f"  {cid:<14}{DIM}{state}{RESET}")

            out.append(f"\n{BOLD}EVENTS{RESET}")
            out.extend(f"  {DIM}{line}{RESET}" for line in log[-12:])
            out.append("\033[J")

            sys.stdout.write("\n".join(out))
            sys.stdout.flush()
            time.sleep(max(0.0, period - (time.monotonic() - now)))
    except KeyboardInterrupt:
        return 0
    finally:
        if old_term:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_term)
        sys.stdout.write(SHOW + "\n")
        panel.close()


if __name__ == "__main__":
    raise SystemExit(main())
