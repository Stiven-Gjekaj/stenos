"""Transcription backends and the loop that drives them over recorded segments.

Every backend keeps its model resident for the lifetime of the object. A call
produces hundreds of short segments, and reloading a model per segment, or
shelling out to a separate binary, would dominate total runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import numpy.typing as npt

from .audio import segment_to_audio
from .sink import Segment

__all__ = [
    "BackendUnavailableError",
    "MockBackend",
    "ProgressCallback",
    "TranscribedSegment",
    "TranscriptionBackend",
    "transcribe_segments",
]

#: Called with the number of segments completed and the total, after each one.
ProgressCallback = Callable[[int, int], None]


class BackendUnavailableError(RuntimeError):
    """Raised when the selected backend cannot be used on this machine.

    Carries an instruction for installing the missing dependency, so an
    operator sees a usable message rather than an import traceback.
    """


class TranscriptionBackend(Protocol):
    """A loaded speech recognition model that transcribes 16 kHz mono float32 audio."""

    name: str

    def transcribe(
        self,
        audio: npt.NDArray[np.float32],
        language: str | None = None,
    ) -> str:
        """Return the text spoken in one segment, or an empty string for silence."""
        ...


@dataclass(frozen=True, slots=True)
class TranscribedSegment:
    """One segment paired with the text recognised in it."""

    user_id: int
    start: float
    duration: float
    text: str


@dataclass
class MockBackend:
    """Deterministic backend used by the test suite and the compatibility checks.

    Ships with the package rather than the tests so the offline pipeline can be
    exercised against an installed wheel without any model weights present.
    """

    texts: Sequence[str] = ()
    default: str = "mock transcript"
    name: str = "mock"
    calls: list[tuple[int, str | None]] = field(default_factory=list)

    def transcribe(
        self,
        audio: npt.NDArray[np.float32],
        language: str | None = None,
    ) -> str:
        index = len(self.calls)
        self.calls.append((int(audio.size), language))
        if index < len(self.texts):
            return self.texts[index]
        return self.default


def transcribe_segments(
    segments: Iterable[Segment],
    backend: TranscriptionBackend,
    *,
    language: str | None = None,
    progress: ProgressCallback | None = None,
) -> list[TranscribedSegment]:
    """Transcribe every segment in order, reporting progress as each completes.

    Empty results are retained here so the sidecar records the full segment set.
    They are dropped when the transcript is merged.
    """
    ordered = list(segments)
    total = len(ordered)
    results: list[TranscribedSegment] = []

    for index, segment in enumerate(ordered, start=1):
        audio = segment_to_audio(segment)
        text = backend.transcribe(audio, language).strip()
        results.append(
            TranscribedSegment(
                user_id=segment.user_id,
                start=segment.start,
                duration=segment.duration,
                text=text,
            )
        )
        if progress is not None:
            progress(index, total)

    return results
