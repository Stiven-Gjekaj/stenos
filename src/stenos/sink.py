"""A sink that records packet arrival times so segments keep their position in the call.

The sink bundled with py-cord concatenates every packet a user sends into one
stream, which discards the point in the call at which each utterance occurred. A
participant who joins or starts speaking late is then placed at the wrong offset
in a merged transcript. This sink instead timestamps each packet on arrival and
opens a new segment whenever a speaker falls silent for longer than the gap
threshold.

Discord clients transmit only while someone is speaking, so the gaps between
packets are the silence and no separate voice activity detector is required.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import discord.opus
from discord.sinks import Filters, Sink

from .config import bundle_directory

__all__ = [
    "BYTES_PER_SECOND",
    "DISCORD_CHANNELS",
    "DISCORD_SAMPLE_RATE",
    "DISCORD_SAMPLE_WIDTH",
    "Segment",
    "TimestampedSink",
    "bundle_directory",
    "ensure_opus",
    "opus_library_candidates",
]

#: Discord decodes received voice to 48 kHz, stereo, signed 16 bit little endian.
DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2

#: Byte count of one second of decoded audio in the format above.
BYTES_PER_SECOND = DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH

DEFAULT_SEGMENT_GAP = 0.4

#: Environment variable pointing at libopus when it lives somewhere unusual.
OPUS_PATH_VARIABLE = "OPUS_LIBRARY_PATH"


#: File names libopus is published under, per platform.
_OPUS_FILE_NAMES = {
    "darwin": ("libopus.0.dylib", "libopus.dylib"),
    "win32": ("libopus-0.x64.dll", "libopus-0.x86.dll", "opus.dll"),
    "linux": ("libopus.so.0", "libopus.so"),
}


def _platform_key() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "win32"
    return "linux"


def opus_library_candidates() -> list[str]:
    """Locations to try when the default library search does not find libopus.

    py-cord resolves libopus through ctypes.util.find_library on every platform
    except Windows. On Apple Silicon that search does not cover the Homebrew
    prefix, so installing the package is not by itself enough to load it, and a
    frozen executable carries its own copy that no system search would find.
    """
    names = _OPUS_FILE_NAMES[_platform_key()]
    candidates: list[str] = []

    # A bundled copy comes first: a standalone executable must not depend on
    # the host having libopus installed.
    bundle = bundle_directory()
    if bundle is not None:
        candidates.extend(str(bundle / name) for name in names)
        candidates.extend(str(bundle / "discord" / "bin" / name) for name in names)

    if sys.platform == "darwin":
        candidates += [
            "/opt/homebrew/lib/libopus.0.dylib",
            "/opt/homebrew/lib/libopus.dylib",
            "/usr/local/lib/libopus.0.dylib",
            "/usr/local/lib/libopus.dylib",
        ]
    elif sys.platform.startswith("linux"):
        candidates += list(names)

    return candidates


def _try_load(candidate: str) -> bool:
    try:
        discord.opus.load_opus(candidate)
    except Exception:
        # Any failure means this candidate is unusable; try the next one.
        return False
    return bool(discord.opus.is_loaded())


def ensure_opus(path: str | None = None) -> bool:
    """Load libopus, looking beyond the default search path when necessary.

    Returns whether opus is loaded. Voice receive cannot decode audio without
    it, and the failure appears at runtime rather than at import.
    """
    if discord.opus.is_loaded():
        return True

    explicit = [item for item in (path, os.environ.get(OPUS_PATH_VARIABLE)) if item]
    for candidate in explicit:
        if _try_load(candidate):
            return True

    try:
        if discord.opus._load_default():
            return True
    except Exception:
        pass

    return any(_try_load(candidate) for candidate in opus_library_candidates())


def _decoded(data: Any, user: Any) -> tuple[bytes | None, int | None]:
    """Normalise the two calling conventions py-cord has used for a sink write.

    Before 2.8 the router passed decoded audio and an integer user identifier.
    From 2.8 it passes a VoiceData carrying the audio, and the member object it
    came from rather than an identifier. Both are read here, so the sink works
    against either release and so tests that call write directly keep saying
    what they were written to say.

    Either half may be absent. A packet with no audio in it is nothing to
    record, and one whose speaker is unknown cannot be attributed, so each is
    reported as None for the caller to handle.
    """
    payload = getattr(data, "pcm", data)
    if not isinstance(payload, bytes | bytearray | memoryview) or not payload:
        return None, None

    speaker = getattr(user, "id", user)
    return bytes(payload), speaker if isinstance(speaker, int) else None


@dataclass(slots=True)
class Segment:
    """One continuous stretch of speech from a single participant.

    ``start`` is measured in seconds from the first packet of the recording,
    which is the recording origin rather than the first packet from this user.
    """

    user_id: int
    start: float
    pcm: bytearray = field(default_factory=bytearray)

    @property
    def duration(self) -> float:
        """Length of the buffered audio in seconds."""
        return len(self.pcm) / BYTES_PER_SECOND

    @property
    def end(self) -> float:
        """Offset in seconds at which this segment stops."""
        return self.start + self.duration


class TimestampedSink(Sink):
    """Collect per-user segments keyed on wall-clock arrival time.

    The clock is injectable so segmentation can be driven by synthetic packet
    sequences in tests without sleeping.

    py-cord 2.8 rewrote the receive path and left its sinks behind. The router
    reads three members that no sink in that release defines, its own included,
    so starting a recording raises before any audio moves. They are supplied
    here, and the write signature accepts both the old calling convention and
    the new one, so this sink works either side of that change.
    """

    #: Sink events to subscribe to, read by the router when a recording starts.
    #: Audio does not arrive this way, it arrives through write, so subscribing
    #: to nothing is both correct and sufficient.
    __sink_listeners__: ClassVar[list[tuple[str, str]]] = []

    def __init__(
        self,
        *,
        segment_gap: float = DEFAULT_SEGMENT_GAP,
        clock: Callable[[], float] = time.perf_counter,
        filters: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(filters=filters)
        if segment_gap < 0:
            raise ValueError(f"segment_gap must not be negative, got {segment_gap}")
        self.segment_gap = segment_gap
        self.encoding = "pcm"
        self._clock = clock
        self._lock = threading.Lock()
        self._origin: float | None = None
        self._segments: list[Segment] = []
        self._open: dict[int, Segment] = {}
        self._last_packet: dict[int, float] = {}
        self._packet_count = 0
        self._last_arrival: float | None = None
        self._unattributed = 0

    def walk_children(self) -> list[Sink]:
        """Child sinks to register alongside this one. There are none."""
        return []

    def is_opus(self) -> bool:
        """Whether to receive opus frames rather than decoded audio.

        False, so py-cord decodes each packet and write receives the linear
        audio this sink measures durations in.
        """
        return False

    @Filters.container
    def write(self, data: Any, user: Any) -> None:
        """Buffer one decoded packet, opening a new segment after a silent gap."""
        payload, speaker = _decoded(data, user)
        if payload is None:
            return
        if speaker is None:
            # Audio from nobody identifiable cannot be attributed, and guessing
            # would put words in someone's mouth. Counted so a recording can
            # report having discarded it rather than quietly losing it.
            with self._lock:
                self._unattributed += 1
            return

        now = self._clock()
        with self._lock:
            if self._origin is None:
                self._origin = now

            segment = self._open.get(speaker)
            previous = self._last_packet.get(speaker)
            if segment is None or previous is None or (now - previous) > self.segment_gap:
                segment = Segment(user_id=speaker, start=now - self._origin)
                self._open[speaker] = segment
                self._segments.append(segment)

            segment.pcm.extend(payload)
            self._last_packet[speaker] = now
            self._last_arrival = now
            self._packet_count += 1

    def format_audio(self, audio: Any) -> None:
        """Required by the base sink. Nothing is written to disk during recording."""
        return None

    def cleanup(self) -> None:
        """Close every open segment and mark the sink finished."""
        with self._lock:
            self.finished = True
            self._open.clear()

    def segments(self) -> list[Segment]:
        """Return every recorded segment ordered by position in the call."""
        with self._lock:
            return sorted(self._segments, key=lambda segment: (segment.start, segment.user_id))

    @property
    def packet_count(self) -> int:
        """Number of packets received across all participants."""
        with self._lock:
            return self._packet_count

    @property
    def unattributed_packets(self) -> int:
        """Packets carrying audio that no known speaker could be matched to."""
        with self._lock:
            return self._unattributed

    @property
    def user_ids(self) -> frozenset[int]:
        """Identifiers of every participant that transmitted at least one packet."""
        with self._lock:
            return frozenset(self._last_packet)

    @property
    def duration(self) -> float:
        """Seconds between the first and last packet received."""
        with self._lock:
            if self._origin is None or self._last_arrival is None:
                return 0.0
            return self._last_arrival - self._origin
