"""A sink that keeps each segment at the point in the call where it was spoken.

The sink bundled with py-cord concatenates every packet a user sends into one
stream, which discards the point in the call at which each utterance occurred. A
participant who joins or starts speaking late is then placed at the wrong offset
in a merged transcript. This sink instead places every packet on a timeline and
opens a new segment whenever a speaker falls silent for longer than the gap
threshold.

Which timeline is the whole question. Arrival time is the obvious answer and the
wrong one: py-cord 2.8 drains a jitter buffer into the sink and synthesises
packets to cover gaps, so a burst delivers several seconds of audio in a
fraction of a second, and arrival stops tracking speech. Segments timed that way
run longer than the span they were received in and overlap the segments after
them.

Every packet carries its own answer. The RTP timestamp counts samples at the
sample rate the audio is decoded to, advancing with the audio rather than with
delivery, so it is unaffected by whatever buffering happens in front of the
sink. It is used wherever it is present, measured from each participant's first
packet, because the count starts at an arbitrary value that is unrelated between
one participant and the next. Arrival still decides where a participant's stream
begins relative to the recording, since that is the only clock the two have in
common.

Discord clients transmit only while someone is speaking, so the gaps between
packets are the silence and no separate voice activity detector is required.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import discord.opus
from discord.sinks import Filters, Sink

from .audio import (
    BYTES_PER_SECOND,
    DISCORD_CHANNELS,
    DISCORD_SAMPLE_RATE,
    DISCORD_SAMPLE_WIDTH,
    MONO_BYTES_PER_SECOND,
    Segment,
)
from .config import bundle_directory

__all__ = [
    "BYTES_PER_SECOND",
    "DEFAULT_MAX_SEGMENT",
    "DISCORD_CHANNELS",
    "DISCORD_SAMPLE_RATE",
    "DISCORD_SAMPLE_WIDTH",
    "MONO_BYTES_PER_SECOND",
    "OPUS_PATH_VARIABLE",
    "Segment",
    "TimestampedSink",
    "bundle_directory",
    "ensure_opus",
    "opus_library_candidates",
]

#: RTP timestamps count samples in an unsigned 32 bit field, so they wrap. At
#: the Discord sample rate that happens roughly once a day.
TICK_WRAP = 2**32

#: How far the media clock may run from arrival before it is read as having been
#: re-based rather than merely buffered. Buffering moves audio by fractions of a
#: second; a stream that restarts moves it by hours, since the new count begins
#: somewhere unrelated.
MAX_CLOCK_DISAGREEMENT = 60.0

DEFAULT_SEGMENT_GAP = 0.4

#: Longest a segment may run before it is closed for length rather than for
#: silence. Bounds what one speaker who never pauses can hold, and bounds the
#: work of reducing a segment once it closes. Thirty seconds is also the
#: window a Whisper encoder reads, so a segment is never longer than the
#: context the model has for it, and a long turn gets a timestamp per part
#: instead of one for the whole of it.
DEFAULT_MAX_SEGMENT = 30.0

log = logging.getLogger("stenos")

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


def _decoded(data: Any, user: Any) -> tuple[bytes | None, int | None, int | None]:
    """Normalise the two calling conventions py-cord has used for a sink write.

    Before 2.8 the router passed decoded audio and an integer user identifier.
    From 2.8 it passes a VoiceData carrying the audio, the packet it was decoded
    from, and the member it came from rather than an identifier. All three are
    read here, so the sink works against either release and so tests that call
    write directly keep saying what they were written to say.

    Any part may be absent. A packet with no audio in it is nothing to record,
    one whose speaker is unknown cannot be attributed, and one carrying no media
    timestamp has to be placed by arrival instead, so each is reported as None
    for the caller to handle.
    """
    payload = getattr(data, "pcm", data)
    if not isinstance(payload, bytes | bytearray | memoryview) or not payload:
        return None, None, None

    speaker = getattr(user, "id", user)
    ticks = getattr(getattr(data, "packet", None), "timestamp", None)
    return (
        bytes(payload),
        speaker if isinstance(speaker, int) else None,
        ticks if isinstance(ticks, int) else None,
    )


def _ticks_between(origin: int, current: int) -> int:
    """Samples from one RTP timestamp to another, taking the shorter way round.

    The field wraps, so the distance from a value near the top to one near the
    bottom is small and forward rather than enormous and backward.
    """
    delta = (current - origin) % TICK_WRAP
    return delta - TICK_WRAP if delta >= TICK_WRAP // 2 else delta


@dataclass(slots=True)
class _Speaker:
    """Where one participant's audio sits on the recording timeline.

    ``base`` is where this participant's first packet landed, measured from the
    recording origin. Everything after it is measured from that packet on the
    participant's own media clock, which is the only clock that stays true to
    the audio once a buffer sits in front of the sink.
    """

    base: float
    ticks: int | None
    position: float = field(init=False)

    def __post_init__(self) -> None:
        self.position = self.base

    def locate(self, ticks: int | None, arrival: float) -> float:
        """Where on the recording timeline the packet being written belongs."""
        if self.ticks is None or ticks is None:
            return arrival

        position = self.base + _ticks_between(self.ticks, ticks) / DISCORD_SAMPLE_RATE
        if abs(position - arrival) <= MAX_CLOCK_DISAGREEMENT:
            return position

        # A stream that restarts counts from somewhere new, and reading the old
        # origin against the new count puts the audio hours from where it
        # belongs. Arrival cannot be wrong by that much, so it settles the
        # disagreement and the media clock starts again from this packet.
        self.base = arrival
        self.ticks = ticks
        return arrival


class TimestampedSink(Sink):
    """Collect per-user segments, each placed on the clock its packets carry.

    The clock is injectable so segmentation can be driven by synthetic packet
    sequences in tests without sleeping.

    py-cord 2.8 rewrote the receive path and left its sinks behind. The router
    reads three members that no sink in that release defines, its own included,
    so starting a recording raises before any audio moves. They are supplied
    here, and the write signature accepts both the old calling convention and
    the new one, so this sink works either side of that change.

    A segment that can no longer grow is handed to a worker rather than reduced
    where it closes. Reducing thirty seconds of audio takes about seventy
    milliseconds and packets arrive every twenty, so doing it on py-cord's
    router thread would stall delivery every time somebody stopped speaking.
    Producing that audio took thirty seconds, so the worker is some four hundred
    times faster than the audio arrives and cannot fall behind.
    """

    #: Sink events to subscribe to, read by the router when a recording starts.
    #: Audio does not arrive this way, it arrives through write, so subscribing
    #: to nothing is both correct and sufficient.
    __sink_listeners__: ClassVar[list[tuple[str, str]]] = []

    def __init__(
        self,
        *,
        segment_gap: float = DEFAULT_SEGMENT_GAP,
        max_segment: float = DEFAULT_MAX_SEGMENT,
        clock: Callable[[], float] = time.perf_counter,
        filters: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(filters=filters)
        if segment_gap < 0:
            raise ValueError(f"segment_gap must not be negative, got {segment_gap}")
        if max_segment <= 0:
            raise ValueError(f"max_segment must be positive, got {max_segment}")
        self.segment_gap = segment_gap
        self.max_segment = max_segment
        self.encoding = "pcm"
        self._clock = clock
        self._lock = threading.Lock()
        self._origin: float | None = None
        self._segments: list[Segment] = []
        self._open: dict[int, Segment] = {}
        self._speakers: dict[int, _Speaker] = {}
        self._packet_count = 0
        self._last_arrival: float | None = None
        self._unattributed = 0
        self._closed: queue.SimpleQueue[Segment | None] = queue.SimpleQueue()
        self._reducer: threading.Thread | None = None

    def _reduce_closed(self) -> None:
        """Drain closed segments, reducing each. Ends on the sentinel."""
        while True:
            segment = self._closed.get()
            if segment is None:
                return
            try:
                segment.reduce()
            except Exception:
                # A segment that will not reduce is still transcribable at the
                # rate it arrived at, so this costs memory rather than audio.
                log.exception("Could not reduce a closed segment")

    def _retire(self, segment: Segment) -> None:
        """Hand a segment that can no longer grow to the worker.

        Starting the worker is guarded, because the two callers are different
        threads: the router retires a segment that closed, and cleanup retires
        whatever was still open. Unguarded, both can find no worker and both
        start one, after which only the one that assigned last is tracked. The
        other never receives the sentinel, so it outlives the recording and
        cleanup's join waits on the wrong thread.
        """
        with self._lock:
            if self._reducer is None:
                self._reducer = threading.Thread(
                    target=self._reduce_closed,
                    name=f"stenos-reducer:{id(self):#x}",
                    daemon=True,
                )
                self._reducer.start()
        self._closed.put(segment)

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
        """Buffer one decoded packet, opening a new segment after a silent gap.

        A packet arriving after cleanup is dropped. py-cord's router keeps
        draining for a moment after a recording is stopped, and cleanup has by
        then closed every segment, drained the reducer and joined it, so a
        packet accepted afterwards opens a segment nothing will ever reduce and
        grows the buffers that transcription is already reading.
        """
        if self.finished:
            return

        payload, speaker, ticks = _decoded(data, user)
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
        retiring: list[Segment] = []
        with self._lock:
            if self._origin is None:
                self._origin = now
            arrival = now - self._origin

            state = self._speakers.get(speaker)
            if state is None:
                # The first packet from a participant is the one point at which
                # arrival has to be trusted, since it is what ties their clock
                # to everyone else's.
                state = self._speakers[speaker] = _Speaker(base=arrival, ticks=ticks)
                position = arrival
            else:
                position = state.locate(ticks, arrival)

            segment = self._open.get(speaker)
            silent = segment is not None and (position - state.position) > self.segment_gap
            # Closed for length as well as for silence. Without it one speaker
            # who never pauses holds the whole call in a single segment, and
            # the work of reducing that segment grows with the call.
            overlong = segment is not None and (position - segment.start) >= self.max_segment

            if segment is None or silent or overlong:
                if segment is not None:
                    retiring.append(segment)
                segment = Segment(user_id=speaker, start=position)
                self._open[speaker] = segment
                self._segments.append(segment)

            segment.extend(payload)
            state.position = position
            self._last_arrival = now
            self._packet_count += 1

        # Outside the lock. Handing a segment over can start the worker, and
        # the router thread has no reason to hold the sink shut while it does.
        for closed in retiring:
            self._retire(closed)

    def format_audio(self, audio: Any) -> None:
        """Required by the base sink. Nothing is written to disk during recording."""

    def cleanup(self) -> None:
        """Close every open segment, reduce what is outstanding, and finish.

        Waits for the worker rather than leaving it running, because everything
        after this reads the segments and would otherwise race the reduction of
        the last few. The wait is bounded: a queue that somehow did not drain
        costs memory, and every segment is transcribable at whichever rate it
        holds, so nothing is lost by giving up on it.
        """
        with self._lock:
            self.finished = True
            outstanding = list(self._open.values())
            self._open.clear()

        for segment in outstanding:
            self._retire(segment)

        worker = self._reducer
        if worker is not None:
            self._closed.put(None)
            worker.join(timeout=30.0)
            if worker.is_alive():
                log.warning("The segment reducer did not finish, so some audio is held twice.")
            self._reducer = None

    def segments(self) -> list[Segment]:
        """Return every recorded segment ordered by position in the call."""
        with self._lock:
            return sorted(self._segments, key=lambda segment: (segment.start, segment.user_id))

    @property
    def buffered_bytes(self) -> int:
        """Audio currently held in memory, across every segment."""
        with self._lock:
            segments = list(self._segments)
        # Each read under its own lock, and outside the sink's: a segment being
        # reduced while this runs would otherwise be measured mid-swap.
        return sum(segment.held() for segment in segments)

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
            return frozenset(self._speakers)

    @property
    def duration(self) -> float:
        """Seconds from the first packet to the end of the last audio recorded.

        Not the span between the first and last arrival. The audio a packet
        carries extends past the moment it arrived, and with a buffer in front
        of the sink the last packet can land well before the audio in it ends.
        """
        with self._lock:
            if self._origin is None or self._last_arrival is None:
                return 0.0
            spans = [self._last_arrival - self._origin]
            spans += [segment.end for segment in self._segments]
            return max(spans)
