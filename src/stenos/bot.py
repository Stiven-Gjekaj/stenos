"""Discord bot commands and the offline pipeline that turns a recording into files."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .audio import prepare_segments
from .config import Config
from .sink import TimestampedSink
from .transcribe import ProgressCallback, TranscriptionBackend, transcribe_segments
from .transcript import (
    TranscriptLine,
    build_sidecar,
    merge,
    transcript_paths,
    write_sidecar,
    write_transcript,
)

__all__ = [
    "RecordingResult",
    "RecordingSession",
    "describe_result",
    "discard_audio",
    "format_duration",
    "run_pipeline",
]


@dataclass(frozen=True, slots=True)
class RecordingResult:
    """Everything produced by one recording, for reporting back to the caller."""

    transcript_path: Path
    sidecar_path: Path
    lines: list[TranscriptLine]
    segment_count: int
    duration: float
    packet_count: int
    speakers: int


def discard_audio(sink: TimestampedSink) -> None:
    """Release the buffered audio held by a sink.

    Recordings are held in memory rather than on disk, so discarding means
    emptying the buffers once the transcript has been written.
    """
    for segment in sink.segments():
        segment.pcm.clear()


def run_pipeline(
    sink: TimestampedSink,
    names: Mapping[int, str],
    *,
    channel_name: str,
    config: Config,
    backend: TranscriptionBackend,
    recorded_at: datetime | None = None,
    progress: ProgressCallback | None = None,
) -> RecordingResult:
    """Convert, transcribe, merge, and write out one recording.

    Separated from the Discord command handlers so the whole path can be
    exercised offline, and so it can be run on a worker thread without
    blocking the gateway heartbeat.
    """
    recorded_at = recorded_at or datetime.now(UTC)
    duration = sink.duration
    packet_count = sink.packet_count

    segments = prepare_segments(sink.segments(), min_segment=config.min_segment)
    results = transcribe_segments(
        segments,
        backend,
        language=config.language,
        progress=progress,
    )
    lines = merge(results, names)

    transcript_path, sidecar_path = transcript_paths(config.output_dir, channel_name, recorded_at)
    write_transcript(transcript_path, lines)
    write_sidecar(
        sidecar_path,
        build_sidecar(
            results,
            names,
            channel=channel_name,
            recorded_at=recorded_at,
            duration=duration,
            backend=backend.name,
            model=config.whisper_model,
        ),
    )

    if not config.keep_audio:
        discard_audio(sink)

    return RecordingResult(
        transcript_path=transcript_path,
        sidecar_path=sidecar_path,
        lines=lines,
        segment_count=len(segments),
        duration=duration,
        packet_count=packet_count,
        speakers=len({line.user_id for line in lines}),
    )


@dataclass
class RecordingSession:
    """State held for one guild while a recording is in progress."""

    guild_id: int
    channel_id: int
    channel_name: str
    text_channel: Any
    voice_client: Any
    sink: TimestampedSink
    names: dict[int, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = field(default_factory=time.perf_counter)

    def elapsed(self) -> float:
        """Seconds since recording began."""
        return time.perf_counter() - self.started_monotonic

    def remember(self, member: Any) -> None:
        """Cache a participant's display name.

        Names are resolved while recording because a participant may leave
        before the call ends, after which the guild no longer resolves them.
        """
        self.names[int(member.id)] = str(getattr(member, "display_name", member))

    def remember_all(self, members: Iterable[Any]) -> None:
        for member in members:
            self.remember(member)


def format_duration(seconds: float) -> str:
    """Render a duration as hours, minutes, and seconds."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def describe_result(result: RecordingResult) -> str:
    """Summarise a finished recording for the text channel."""
    if result.packet_count == 0:
        return (
            "Recording stopped, but no audio was received. "
            "No transcript was produced. This is expected when the voice "
            "connection carried no decodable audio; see the known limitations "
            "section of the documentation."
        )
    return (
        f"Recording stopped. Transcribed {result.segment_count} segments "
        f"from {result.speakers} speakers over {format_duration(result.duration)}."
    )
