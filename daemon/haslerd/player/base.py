"""What the action layer is allowed to ask of a player.

Deliberately small. Everything here is something a piano key can plausibly mean;
nothing here knows about MPD, moOde, ALSA or HTTP. That keeps the whole action
table testable on a laptop against ``FakePlayer``, which is what lets the
Excelsior's behaviour be written and verified before it is wired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Everything the lamp and the PU key can report. "radio" means MPD itself.
SOURCES = ("radio", "bluetooth", "airplay", "spotify", "ytm")

# Sources a preset can name. The router inside the player owns these; the rest
# of SOURCES are renderers moOde arbitrates for us (a phone connects, moOde
# stops MPD).
PRESET_SOURCES = ("radio", "spotify", "ytm")


@dataclass(frozen=True)
class Preset:
    """One key's worth of music. ``source`` picks the backend that plays ``uri``.

    ``radio``    an HTTP stream URL, played by MPD
    ``spotify``  a ``spotify:playlist:…`` / album / track URI, played by go-librespot
    ``ytm``      a YouTube Music playlist (or watch) URL, queued into MPD via the proxy

    A ytm preset may also hold a reference (``ref``: kind, id, mode, seed,
    country, list, title, params - player/sources/ytm_refs.py) that the ytm
    source resolves when pressed; ``uri`` is then the link it last resolved
    to, played as-is when YouTube Music can't be asked. No kind = a playlist
    link, exactly as before.
    """

    n: int
    name: str
    uri: str
    source: str = "radio"
    shuffle: bool = False
    # ((field, value), ...) sorted: hashable, so the preset stays frozen.
    ref: tuple[tuple[str, str], ...] = ()

    @property
    def ytm_ref(self) -> dict[str, str]:
        """The ytm reference as a dict ({} = a plain link)."""
        return dict(self.ref)

    @property
    def kind(self) -> str:
        return dict(self.ref).get("kind", "")

    @property
    def url(self) -> str:
        """Legacy name - radio presets were plain ``url:`` before sources existed."""
        return self.uri

    @classmethod
    def from_spec(cls, spec: dict) -> "Preset":
        """Parse a ``presets:`` entry. Exactly one of radio/spotify/ytm/url.

        Strict on purpose: a preset naming two sources is a typo whose meaning
        we would otherwise have to guess, and guessing wrong means a key that
        plays the wrong thing with no screen to say why. ``url`` is the
        pre-sources spelling of ``radio`` and stays accepted.
        """
        if not isinstance(spec, dict):
            raise ValueError(f"preset must be a mapping, not {type(spec).__name__}")
        if spec.get("n") is None:
            raise ValueError(f"preset {spec!r} has no n")
        try:
            n = int(spec["n"])
        except (TypeError, ValueError):
            raise ValueError(f"preset n {spec['n']!r} is not an integer") from None
        given = [key for key in ("radio", "spotify", "ytm", "url") if spec.get(key)]
        if not given and spec.get("kind") and not any(spec.get(k) for k in ("radio", "spotify", "url")):
            given = ["ytm"]     # a ytm reference not yet resolved to a link (a chart, an artist)
        if not given:
            raise ValueError(f"preset {n} has no radio/spotify/ytm/url")
        if len(given) > 1:
            raise ValueError(f"preset {n} names more than one source: {', '.join(given)}")
        key = given[0]
        ref: tuple[tuple[str, str], ...] = ()
        if key == "ytm" and spec.get("kind"):
            from .sources.ytm_refs import REF_KEYS  # noqa: PLC0415 - pure module, no cycle

            ref = tuple(sorted((k, str(spec[k])) for k in REF_KEYS if spec.get(k) not in (None, "")))
        return cls(
            n=n,
            name=str(spec.get("name", f"Preset {n}")),
            uri=str(spec.get(key) or ""),
            source="radio" if key == "url" else key,
            shuffle=bool(spec.get("shuffle", False)),
            ref=ref,
        )


@runtime_checkable
class Source(Protocol):
    """One way of making sound. The router keeps exactly one of these active.

    Every method must be safe to call when the backend is down: log and return,
    never raise into the panel loop for ``stop``/``is_active``.
    """

    name: str

    def play(self, preset: Preset) -> None: ...

    def stop(self) -> None: ...

    def next_track(self) -> None: ...

    def is_active(self) -> bool: ...

    def close(self) -> None: ...


@runtime_checkable
class Player(Protocol):
    def play_preset(self, preset: Preset) -> None: ...

    def stop(self) -> None: ...

    def next_track(self) -> None: ...

    def current(self) -> Preset | None:
        """The preset last started and not since stopped, whatever its source."""
        ...

    def set_volume(self, value: float) -> None: ...

    def get_volume(self) -> float | None:
        """Volume on the knob's 0-100 scale, or None if nobody knows yet."""
        ...

    def set_mute(self, muted: bool) -> None: ...

    def set_tone(self, control: str, value: float) -> None: ...

    def active_source(self) -> str: ...

    def select_source(self, source: str) -> None: ...

    def bluetooth_pair(self, seconds: int) -> None: ...

    def route_speaker(self, position: int) -> None: ...

    def shutdown(self) -> None: ...

    def prefetch(self, presets: list[Preset]) -> None:
        """Get presets ready in the background so a press starts quickly."""
        ...


@dataclass
class FakePlayer:
    """Records what it was asked to do. The test double for the action table.

    ``source`` doubles as "what active_source() reports"; a test that wants a
    preset's source to have died out from under us sets it to something else.
    """

    calls: list[tuple] = field(default_factory=list)
    playing: Preset | None = None
    volume: float | None = None
    muted: bool = False
    source: str = "radio"
    tone: dict[str, float] = field(default_factory=dict)
    speaker: int | None = None
    shutdown_called: bool = False
    fail_play: Exception | None = None

    def play_preset(self, preset: Preset) -> None:
        self.calls.append(("play_preset", preset.n, preset.source))
        if self.fail_play is not None:
            self.playing = None
            raise self.fail_play
        self.playing = preset
        self.source = preset.source

    def stop(self) -> None:
        self.calls.append(("stop",))
        self.playing = None

    def prefetch(self, presets: list[Preset]) -> None:
        self.calls.append(("prefetch", tuple(p.n for p in presets)))

    def next_track(self) -> None:
        self.calls.append(("next_track",))

    def current(self) -> Preset | None:
        return self.playing

    def set_volume(self, value: float) -> None:
        self.calls.append(("set_volume", value))
        self.volume = value

    def get_volume(self) -> float | None:
        return self.volume

    def set_mute(self, muted: bool) -> None:
        self.calls.append(("set_mute", muted))
        self.muted = muted

    def set_tone(self, control: str, value: float) -> None:
        self.calls.append(("set_tone", control, value))
        self.tone[control] = value

    def active_source(self) -> str:
        return self.source

    def select_source(self, source: str) -> None:
        self.calls.append(("select_source", source))
        self.source = source
        # Mirrors MoodePlayer: taking the path back for MPD pauses go-librespot.
        if source != "radio" or (self.playing is not None and self.playing.source == "spotify"):
            self.playing = None

    def bluetooth_pair(self, seconds: int) -> None:
        self.calls.append(("bluetooth_pair", seconds))

    def route_speaker(self, position: int) -> None:
        self.calls.append(("route_speaker", position))
        self.speaker = position

    def shutdown(self) -> None:
        self.calls.append(("shutdown",))
        self.shutdown_called = True

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]
