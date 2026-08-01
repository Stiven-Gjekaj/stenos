"""Merging transcribed segments into a single speaker-attributed transcript.

Output paths and file encodings are chosen so a transcript written on Windows is
byte identical to one written on macOS or Linux.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .transcribe import TranscribedSegment

__all__ = [
    "TranscriptLine",
    "build_sidecar",
    "format_timestamp",
    "merge",
    "render",
    "sanitize_filename",
    "transcript_paths",
    "transcript_stem",
    "write_sidecar",
    "write_transcript",
]

#: Characters Windows rejects in a file name. Applied on every platform so a
#: transcript recorded on Linux can be copied to Windows unchanged.
_ILLEGAL_CHARACTERS = '<>:"/\\|?*'

#: Device names Windows reserves regardless of extension.
_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)

#: ISO 8601 basic format. The extended format separates time components with
#: colons, which are not legal in Windows file names.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

_SEPARATOR_RUN = re.compile(r"-{2,}")
_WHITESPACE_RUN = re.compile(r"\s+")


def format_timestamp(seconds: float) -> str:
    """Render an offset in seconds as a bracketed clock time.

    Hours are not wrapped at 24, so a long recording keeps increasing.
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"


def sanitize_filename(name: str, *, fallback: str = "channel", max_length: int = 80) -> str:
    """Reduce an arbitrary channel name to a portable file name component.

    Non-ASCII characters are preserved, since every supported platform accepts
    them in UTF-8 file names. Characters that are illegal on Windows, control
    characters, trailing dots and spaces, and reserved device names are not.
    """
    cleaned = "".join(
        "-" if character in _ILLEGAL_CHARACTERS or ord(character) < 32 else character
        for character in name
    )
    cleaned = _WHITESPACE_RUN.sub("-", cleaned)
    cleaned = _SEPARATOR_RUN.sub("-", cleaned).strip("-. ")
    cleaned = _SEPARATOR_RUN.sub("-", cleaned[:max_length]).strip("-. ")

    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _RESERVED_STEMS:
        # Prefixed rather than suffixed: Windows resolves a reserved device
        # name from the component before the first dot, so appending to
        # "NUL.txt" would leave it reserved.
        return f"{fallback}-{cleaned}"
    return cleaned


def transcript_stem(channel_name: str, recorded_at: datetime) -> str:
    """Build the shared file name stem for a recording."""
    stamp = recorded_at.strftime(_TIMESTAMP_FORMAT)
    return f"stenos-{sanitize_filename(channel_name)}-{stamp}"


def transcript_paths(
    output_dir: Path,
    channel_name: str,
    recorded_at: datetime,
) -> tuple[Path, Path]:
    """Return the transcript and sidecar paths for a recording."""
    stem = transcript_stem(channel_name, recorded_at)
    return output_dir / f"{stem}.txt", output_dir / f"{stem}.json"


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One attributed utterance, positioned by its offset into the recording."""

    start: float
    user_id: int
    speaker: str
    text: str


def resolve_speaker(user_id: int, names: Mapping[int, str]) -> str:
    """Name a participant from the cache captured while recording.

    Names are cached on join because a participant may disconnect before the
    call ends, at which point the guild no longer resolves them.
    """
    return names.get(user_id, f"Unknown ({user_id})")


def merge(
    results: Iterable[TranscribedSegment],
    names: Mapping[int, str],
) -> list[TranscriptLine]:
    """Flatten every speaker's segments into one ordered transcript.

    Segments that produced no text are dropped here rather than during
    transcription, so the sidecar still records them.
    """
    lines = [
        TranscriptLine(
            start=result.start,
            user_id=result.user_id,
            speaker=resolve_speaker(result.user_id, names),
            text=result.text.strip(),
        )
        for result in results
        if result.text.strip()
    ]
    return sorted(lines, key=lambda line: (line.start, line.user_id))


def render(lines: Iterable[TranscriptLine]) -> str:
    """Format merged lines as the transcript body."""
    return "".join(
        f"{format_timestamp(line.start)} {line.speaker}: {line.text}\n" for line in lines
    )


def write_transcript(path: Path, lines: Iterable[TranscriptLine]) -> Path:
    """Write the transcript as UTF-8 with line feed endings on every platform.

    Windows defaults to the system code page and to carriage return line feed
    endings, either of which would make output differ between hosts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(lines), encoding="utf-8", newline="\n")
    return path


def build_sidecar(
    results: Iterable[TranscribedSegment],
    names: Mapping[int, str],
    *,
    channel: str,
    recorded_at: datetime,
    duration: float,
    backend: str,
    model: str,
) -> dict[str, Any]:
    """Assemble the machine readable record of a recording.

    Every segment is included, including those that produced no text, so
    downstream tooling sees the full timing structure of the call.
    """
    ordered = sorted(results, key=lambda result: (result.start, result.user_id))
    return {
        "channel": channel,
        "recorded_at": recorded_at.isoformat(),
        "duration": round(duration, 3),
        "backend": backend,
        "model": model,
        "speakers": {str(user_id): name for user_id, name in sorted(names.items())},
        "segments": [
            {
                "user_id": result.user_id,
                "speaker": resolve_speaker(result.user_id, names),
                "start": round(result.start, 3),
                "duration": round(result.duration, 3),
                "text": result.text,
            }
            for result in ordered
        ],
    }


def write_sidecar(path: Path, payload: Mapping[str, Any]) -> Path:
    """Write the sidecar as UTF-8 JSON, preserving non-ASCII text verbatim."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    path.write_text(f"{body}\n", encoding="utf-8", newline="\n")
    return path
