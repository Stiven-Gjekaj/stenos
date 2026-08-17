"""What the interface shows, worked out without drawing any of it.

The screens in [interface.md](../../docs/interface.md) need three things: a
recording in progress, the transcripts already written, and the recordings a
crash left unfinished. Deriving those is where the decisions are, and drawing
them is not, so the deriving lives here where it can be tested without a
display and the widgets stay a rendering of values.

Nothing here imports a toolkit, and nothing here talks to Discord. A live
recording is read from the session objects the bot already holds, and the
library is read from ``OUTPUT_DIR``, which is the same thing a person sees
looking in that folder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bot import RecordingSession, StenosBot, format_duration
from .spill import SPILL_SUFFIX, partial_recordings, read_spill

__all__ = [
    "Library",
    "LiveRecording",
    "PastTranscript",
    "Unfinished",
    "library",
    "live_recordings",
]


@dataclass(frozen=True, slots=True)
class LiveRecording:
    """A recording in progress, as the live screen shows it."""

    guild_id: int
    channel: str
    elapsed: float
    speakers: int
    held_bytes: int
    total_bytes: int
    spilling: bool
    connected: bool
    unattributed: int

    @property
    def held(self) -> str:
        """What is in memory, in the unit the ceilings are set in."""
        return f"{self.held_bytes / 1_000_000:.1f} MB"

    @property
    def running_for(self) -> str:
        return format_duration(self.elapsed)

    @property
    def summary(self) -> str:
        """One line, for a window that has room for one line."""
        state = "recording" if self.connected else "connection down"
        return f"{self.channel}: {state}, {self.running_for}, {self.speakers} speakers"


def live_recordings(bot: StenosBot) -> list[LiveRecording]:
    """Every recording the bot currently holds.

    Read from the sessions directly rather than from a summary written for the
    screen, so the interface cannot show a recording the bot does not have or
    miss one it does.
    """
    return [_live(bot, session) for session in list(bot.sessions.values())]


def _live(bot: StenosBot, session: RecordingSession) -> LiveRecording:
    sink = session.sink
    return LiveRecording(
        guild_id=session.guild_id,
        channel=session.channel_name,
        elapsed=session.elapsed(),
        speakers=len(sink.user_ids),
        held_bytes=sink.buffered_bytes,
        total_bytes=sink.total_bytes,
        spilling=sink.spilling,
        # Asked of the bot rather than of the client, so the interface reads
        # the same answer the watchdog acts on.
        connected=not bot.connection_lost(session),
        unattributed=sink.unattributed_packets,
    )


@dataclass(frozen=True, slots=True)
class PastTranscript:
    """A finished recording, read back from what it wrote."""

    transcript: Path
    sidecar: Path | None
    channel: str
    recorded_at: datetime | None
    duration: float
    speakers: tuple[str, ...]
    segments: int

    @property
    def title(self) -> str:
        when = self.recorded_at.strftime("%Y-%m-%d %H:%M") if self.recorded_at else "unknown time"
        return f"{self.channel}, {when}"

    @property
    def summary(self) -> str:
        who = ", ".join(self.speakers) if self.speakers else "nobody named"
        return f"{format_duration(self.duration)}, {who}"


@dataclass(frozen=True, slots=True)
class Unfinished:
    """A recording a crash left behind, waiting for ``--recover``."""

    directory: Path
    channel: str
    started_at: datetime | None
    segments: int

    @property
    def summary(self) -> str:
        when = self.started_at.strftime("%Y-%m-%d %H:%M") if self.started_at else "unknown time"
        return f"{self.channel}, {when}: {self.segments} segments, never transcribed"


@dataclass(frozen=True, slots=True)
class Library:
    """Everything in the output directory worth showing."""

    transcripts: list[PastTranscript] = field(default_factory=list)
    unfinished: list[Unfinished] = field(default_factory=list)


def library(output_dir: Path) -> Library:
    """Read the output directory as the interface presents it.

    Newest first, because that is the one somebody has just made. A transcript
    with no sidecar is still listed: the sidecar carries the speakers and the
    timings, and losing it costs those rather than the transcript.
    """
    if not output_dir.is_dir():
        return Library()

    transcripts = [_past(path) for path in sorted(output_dir.glob("*.txt"))]
    transcripts.sort(key=_sort_key, reverse=True)
    return Library(transcripts=transcripts, unfinished=_unfinished(output_dir))


def _sort_key(item: PastTranscript) -> tuple[datetime, str]:
    # A missing timestamp sorts oldest rather than crashing the comparison.
    return (item.recorded_at or datetime.min.replace(tzinfo=UTC), item.transcript.name)


def _past(transcript: Path) -> PastTranscript:
    sidecar = transcript.with_suffix(".json")
    payload = _sidecar(sidecar)
    if payload is None:
        return PastTranscript(
            transcript=transcript,
            sidecar=None,
            channel=transcript.stem,
            recorded_at=None,
            duration=0.0,
            speakers=(),
            segments=0,
        )
    speakers = payload.get("speakers") or {}
    return PastTranscript(
        transcript=transcript,
        sidecar=sidecar,
        channel=str(payload.get("channel", transcript.stem)),
        recorded_at=_when(payload.get("recorded_at")),
        duration=float(payload.get("duration", 0.0)),
        speakers=tuple(str(name) for name in speakers.values()),
        segments=len(payload.get("segments") or []),
    )


def _sidecar(path: Path) -> dict[str, Any] | None:
    """The sidecar beside a transcript, or None if it cannot be read.

    A sidecar that will not parse is treated as one that is not there. It
    carries the speakers and the timings, so the cost is a row with less on it
    rather than a library that refuses to open.
    """
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _unfinished(output_dir: Path) -> list[Unfinished]:
    found = []
    for directory in partial_recordings(output_dir):
        spilled = read_spill(directory)
        if spilled is None:
            found.append(
                Unfinished(
                    directory=directory,
                    channel=directory.name.removesuffix(SPILL_SUFFIX),
                    started_at=None,
                    segments=0,
                )
            )
            continue
        found.append(
            Unfinished(
                directory=directory,
                channel=spilled.channel,
                started_at=spilled.started_at,
                segments=len(spilled.segments),
            )
        )
    return found
