"""Fixtures shared by more than one test module.

Only what was already written out twice. The compatibility suite keeps its own
copies deliberately: it runs from a directory holding the tests and nothing
else, against an installed wheel, so anything it imported from here would have
to be carried into that environment and would stop being a check of the wheel
alone.
"""

from __future__ import annotations

from collections.abc import Sequence

from stenos.audio import MONO_BYTES_PER_SECOND, Segment


class ScriptedClock:
    """Return a predetermined sequence of timestamps, one per call.

    Segmentation is driven by the clock the sink reads, so handing it a script
    exercises a whole call's worth of timing without sleeping through it.
    """

    def __init__(self, times: Sequence[float]) -> None:
        self._times = list(times)
        self._index = 0

    def __call__(self) -> float:
        value = self._times[self._index]
        self._index += 1
        return value


def segment_of(seconds: float, *, user_id: int = 1, start: float = 0.0) -> Segment:
    """A segment holding the given duration of silence.

    Sized in mono bytes, because a segment holds mono from the moment it is
    written to, so a second of it costs half what a second of the wire format
    does.
    """
    return Segment(
        user_id=user_id, start=start, pcm=bytearray(int(MONO_BYTES_PER_SECOND * seconds))
    )
