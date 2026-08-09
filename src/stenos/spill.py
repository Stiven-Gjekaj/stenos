"""Where a recording's audio goes when it will not fit in memory.

A recording is held in memory and written once at the end, which is what keeps
an unattended host off the disk for the length of a call. That works only while
the memory is there. A call long enough to cross the buffer ceiling used to end
early, and on a host with a gigabyte to its name that is most of a meeting.

Past the ceiling the audio moves here instead. Each participant gets one
append only file of reduced mono samples, and a manifest beside them describes
every write, so a directory left behind by a process that was killed still says
what it holds and can be transcribed afterwards.

Two properties make that true, and both are ordering rather than cleverness:

- **Samples are written before the line describing them.** A crash between the
  two leaves bytes nobody accounts for, which recovery discards, rather than a
  record pointing past the end of a file, which it could not tell from
  corruption.
- **The manifest is a line per record rather than one document.** Appending to
  JSON means rewriting it, which for a call with hundreds of segments is
  hundreds of rewrites of a growing file. A torn final line is dropped on
  recovery and costs one segment.

Every write is flushed, so a killed process loses nothing. Neither is synced,
so a host that loses power loses whatever the kernel had not yet written. That
is the trade this makes deliberately: fsync per segment on the hardware this
exists for would cost more than the ceiling it lifts.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, TextIO

__all__ = [
    "MANIFEST_NAME",
    "SPILL_SUFFIX",
    "SPILL_VERSION",
    "SpillWriter",
    "Spilled",
    "SpilledRecording",
    "SpilledSegment",
    "partial_recordings",
    "read_audio",
    "read_spill",
]

#: Schema version of the manifest, so a later format can be recognised rather
#: than misread by a version that predates it.
SPILL_VERSION = 1

#: The manifest, one JSON object per line, in the order the records happened.
MANIFEST_NAME = "manifest.jsonl"

#: Marks a directory as a recording still in progress, or abandoned by one.
SPILL_SUFFIX = ".partial"


@dataclass(frozen=True, slots=True)
class Spilled:
    """Where one segment's audio went, and what was known about it."""

    path: Path
    offset: int
    length: int
    silent: bool


@dataclass(frozen=True, slots=True)
class SpilledSegment:
    """One segment as the manifest recorded it."""

    user_id: int
    start: float
    offset: int
    length: int
    sample_rate: int
    silent: bool


@dataclass(frozen=True, slots=True)
class SpilledRecording:
    """Everything a left behind directory says about the call it holds."""

    directory: Path
    channel: str
    started_at: datetime
    sample_rate: int
    names: dict[int, str]
    segments: list[SpilledSegment]

    def audio_path(self, user_id: int) -> Path:
        return self.directory / f"{user_id}.pcm"

    def audio_of(self, segment: SpilledSegment) -> bytes:
        """The samples one recovered segment holds."""
        return _read(self.audio_path(segment.user_id), segment.offset, segment.length)


class SpillWriter:
    """Append only storage for one recording's audio.

    One file per participant plus the manifest, all opened on first use and
    held open for the length of the call, because a segment closes every few
    seconds and reopening per segment would be the only expensive part of this.

    Nothing is created until the first segment arrives. Most recordings never
    outgrow memory, and one that does not must leave the disk untouched for the
    length of the call, which is the property this exists to preserve for
    everybody else. A writer that was never used has no directory to remove.
    """

    def __init__(
        self,
        directory: Path,
        *,
        channel: str,
        started_at: datetime,
        sample_rate: int,
    ) -> None:
        self.directory = directory
        self.channel = channel
        self.started_at = started_at
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._audio: dict[int, BinaryIO] = {}
        self._offsets: dict[int, int] = {}
        self._names: dict[int, str] = {}
        self._closed = False
        self._manifest: TextIO | None = None

    @property
    def started(self) -> bool:
        """Whether anything has actually been written."""
        return self._manifest is not None

    def _start(self) -> TextIO:
        """Create the directory and open the manifest. Caller holds the lock."""
        if self._manifest is not None:
            return self._manifest
        self.directory.mkdir(parents=True, exist_ok=True)
        self._manifest = (self.directory / MANIFEST_NAME).open("a", encoding="utf-8", newline="\n")
        self._record(
            {
                "record": "recording",
                "version": SPILL_VERSION,
                "channel": self.channel,
                "started_at": self.started_at.isoformat(),
                "sample_rate": self.sample_rate,
            }
        )
        # Whatever was learned before there was anywhere to put it. Written
        # before the first segment, which is the order they happened in.
        for user_id, name in self._names.items():
            self._record({"record": "speaker", "user_id": user_id, "name": name})
        return self._manifest

    def _record(self, payload: dict[str, Any]) -> None:
        """Write one manifest line and flush it. Caller holds the lock."""
        assert self._manifest is not None
        self._manifest.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._manifest.flush()

    def _audio_file(self, user_id: int) -> BinaryIO:
        handle = self._audio.get(user_id)
        if handle is None:
            path = self.directory / f"{user_id}.pcm"
            # Sized before opening rather than asked of the handle afterwards.
            # A file opened for appending reports its position differently
            # across platforms, and this one is only ever appended to, so its
            # length on disk is the offset the next write lands at.
            self._offsets[user_id] = path.stat().st_size if path.is_file() else 0
            handle = path.open("ab")
            self._audio[user_id] = handle
        return handle

    def remember(self, user_id: int, name: str) -> None:
        """Record a participant's display name, which the guild may later lose.

        Held rather than written when nothing has spilled yet, so learning who
        is in the channel does not by itself create a directory.
        """
        with self._lock:
            if self._closed:
                return
            self._names[user_id] = name
            if self._manifest is not None:
                self._record({"record": "speaker", "user_id": user_id, "name": name})

    def append(self, user_id: int, start: float, audio: bytes, rate: int) -> Spilled:
        """Write one segment's samples and describe them, in that order.

        The rate belongs to the segment rather than to the recording. Audio is
        reduced when a segment closes and spilled when memory runs short, and
        those are different moments: a segment can reach the disk still at the
        rate it arrived at. Recorded here, a recovery reads back what was
        actually written instead of what the call was expected to hold.
        """
        with self._lock:
            if self._closed:
                raise ValueError("this recording's storage is already closed")
            self._start()
            handle = self._audio_file(user_id)
            offset = self._offsets[user_id]
            handle.write(audio)
            handle.flush()
            self._offsets[user_id] = offset + len(audio)

            silent = not any(audio)
            self._record(
                {
                    "record": "segment",
                    "user_id": user_id,
                    "start": start,
                    "offset": offset,
                    "length": len(audio),
                    "rate": rate,
                    "silent": silent,
                }
            )
            return Spilled(
                path=self.directory / f"{user_id}.pcm",
                offset=offset,
                length=len(audio),
                silent=silent,
            )

    def close(self) -> None:
        """Stop writing. Leaves the directory in place for recovery to find."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for handle in self._audio.values():
                handle.close()
            self._audio.clear()
            if self._manifest is not None:
                self._manifest.close()
                self._manifest = None

    def discard(self) -> None:
        """Close and remove the directory, once the recording is safely written.

        A recording that never outgrew memory created nothing, so there is
        nothing here to take away.
        """
        self.close()
        if not self.directory.is_dir():
            return
        for child in sorted(self.directory.glob("*")):
            child.unlink(missing_ok=True)
        self.directory.rmdir()


def _read(path: Path, offset: int, length: int) -> bytes:
    """Samples at a known place in an append only file.

    Short of the stated length means the process died between writing the
    samples and the line describing them, which is the one torn state the
    ordering above allows, so the caller gets what survived rather than an
    error.
    """
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def read_audio(spilled: Spilled) -> bytes:
    """The samples one spilled segment holds."""
    return _read(spilled.path, spilled.offset, spilled.length)


def read_spill(directory: Path) -> SpilledRecording | None:
    """What a left behind directory holds, or None if it says nothing usable.

    A final line that was being written when the process died will not parse,
    and is dropped rather than failing the whole recovery. So is a segment
    describing more samples than its file actually has.
    """
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file():
        return None

    header: dict[str, Any] | None = None
    names: dict[int, str] = {}
    segments: list[SpilledSegment] = []
    sizes: dict[int, int] = {}

    for line in manifest.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue  # The line the process was writing when it died.
        kind = payload.get("record")
        if kind == "recording" and header is None:
            header = payload
        elif kind == "speaker":
            names[int(payload["user_id"])] = str(payload["name"])
        elif kind == "segment":
            user_id = int(payload["user_id"])
            if user_id not in sizes:
                audio = directory / f"{user_id}.pcm"
                sizes[user_id] = audio.stat().st_size if audio.is_file() else 0
            offset, length = int(payload["offset"]), int(payload["length"])
            if offset + length > sizes[user_id]:
                continue  # Described, but the samples never reached the disk.
            segments.append(
                SpilledSegment(
                    user_id=user_id,
                    start=float(payload["start"]),
                    offset=offset,
                    length=length,
                    # Falls back to the recording's rate for a manifest written
                    # before the rate travelled with each segment.
                    sample_rate=int(payload.get("rate", header["sample_rate"] if header else 0)),
                    silent=bool(payload["silent"]),
                )
            )

    if header is None or int(header.get("version", 0)) != SPILL_VERSION:
        return None

    return SpilledRecording(
        directory=directory,
        channel=str(header["channel"]),
        started_at=datetime.fromisoformat(str(header["started_at"])),
        sample_rate=int(header["sample_rate"]),
        names=names,
        segments=sorted(segments, key=lambda item: (item.start, item.user_id)),
    )


def partial_recordings(output_dir: Path) -> list[Path]:
    """Directories holding a recording that was never finished."""
    if not output_dir.is_dir():
        return []
    return sorted(
        child
        for child in output_dir.glob(f"*{SPILL_SUFFIX}")
        if child.is_dir() and (child / MANIFEST_NAME).is_file()
    )
