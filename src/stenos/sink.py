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
from typing import Any

import discord.opus
from discord.sinks import Filters, Sink

__all__ = [
    "BYTES_PER_SECOND",
    "DISCORD_CHANNELS",
    "DISCORD_SAMPLE_RATE",
    "DISCORD_SAMPLE_WIDTH",
    "Segment",
    "TimestampedSink",
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


def opus_library_candidates() -> list[str]:
    """Locations to try when the default library search does not find libopus.

    py-cord resolves libopus through ctypes.util.find_library on every platform
    except Windows. On Apple Silicon that search does not cover the Homebrew
    prefix, so installing the package is not by itself enough to load it.
    """
    if sys.platform == "darwin":
        return [
            "/opt/homebrew/lib/libopus.0.dylib",
            "/opt/homebrew/lib/libopus.dylib",
            "/usr/local/lib/libopus.0.dylib",
            "/usr/local/lib/libopus.dylib",
        ]
    if sys.platform.startswith("linux"):
        return ["libopus.so.0", "libopus.so"]
    return []


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
    """

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

    @Filters.container
    def write(self, data: bytes, user: int) -> None:
        """Buffer one decoded packet, opening a new segment after a silent gap."""
        now = self._clock()
        with self._lock:
            if self._origin is None:
                self._origin = now

            segment = self._open.get(user)
            previous = self._last_packet.get(user)
            if segment is None or previous is None or (now - previous) > self.segment_gap:
                segment = Segment(user_id=user, start=now - self._origin)
                self._open[user] = segment
                self._segments.append(segment)

            segment.pcm.extend(data)
            self._last_packet[user] = now
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
