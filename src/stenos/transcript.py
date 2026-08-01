"""Merging transcribed segments into a single speaker-attributed transcript.

Output paths and file encodings are chosen so a transcript written on Windows is
byte identical to one written on macOS or Linux.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

__all__ = [
    "format_timestamp",
    "sanitize_filename",
    "transcript_paths",
    "transcript_stem",
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
