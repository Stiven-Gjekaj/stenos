"""Whether a finished recording actually captured anything, and what to say when it did not.

A recording that captured nothing produces a transcript with no lines, which
reads exactly like a call in which nobody spoke. The two have very different
causes, and the difference matters: one is a quiet meeting, the other is a bot
that cannot hear. Both states are recognisable from the buffered audio, so they
are separated here and reported rather than written out as an empty transcript.

The silent case is the more misleading of the two. When a packet cannot be
decrypted, py-cord substitutes an opus silence frame instead of reporting the
failure, so the recording ends up the right length and entirely empty.
"""

from __future__ import annotations

from dataclasses import dataclass

from .sink import Segment, TimestampedSink
from .voice import DaveState

__all__ = [
    "REASON_ALL_SILENT",
    "REASON_NO_PACKETS",
    "REASON_OK",
    "RecordingIntegrity",
    "check_recording",
]

#: The recording holds audio and is worth transcribing.
REASON_OK = "ok"

#: Not one packet arrived over the whole recording.
REASON_NO_PACKETS = "no-packets"

#: Packets arrived, and every sample in them was silence.
REASON_ALL_SILENT = "all-silent"


@dataclass(frozen=True, slots=True)
class RecordingIntegrity:
    """The verdict on one finished recording."""

    ok: bool
    reason: str
    detail: str


def _all_silence(segments: list[Segment]) -> bool:
    """Whether every buffered sample is zero.

    Returns False for a recording with no bytes at all, which is the separate
    and more obvious no-packets case. Real audio leaves the loop on its first
    non-zero byte, so the full scan only happens for a recording that is
    genuinely silent.

    Reads whichever representation a segment holds. Silence survives both the
    downmix and the resample, so the verdict is the same either way: the mean
    of two zeroes is zero, and a filter over zeroes returns them.
    """
    total = 0
    for segment in segments:
        if any(segment.pcm):
            return False
        total += len(segment.pcm)
    return total > 0


def _encryption_note(dave: DaveState | None) -> str:
    """A sentence naming the encryption state, when it explains the failure."""
    if dave is None or dave.receives_audio:
        return ""
    if not dave.support.available:
        return (
            " The end to end encryption library is not installed, so encrypted "
            "audio cannot be decoded at all."
        )
    return f" The voice connection reported {dave.summary}."


def check_recording(
    sink: TimestampedSink,
    dave: DaveState | None = None,
) -> RecordingIntegrity:
    """Decide whether a recording is worth transcribing, and say why when it is not.

    Called before the transcription backend is loaded, so a recording that
    captured nothing does not pay for a model that has nothing to do.
    """
    if sink.packet_count == 0:
        note = _encryption_note(dave)
        if not note:
            note = " Either nobody spoke, or libopus could not decode the incoming packets."
        return RecordingIntegrity(
            ok=False,
            reason=REASON_NO_PACKETS,
            detail=f"No audio was received, so there was nothing to transcribe.{note}",
        )

    if _all_silence(sink.segments()):
        return RecordingIntegrity(
            ok=False,
            reason=REASON_ALL_SILENT,
            detail=(
                "Audio arrived but every sample was silence, so there was nothing "
                "to transcribe. Packets that cannot be decrypted are replaced with "
                "silence rather than reported as an error, which produces a "
                f"recording of the right length and no content.{_encryption_note(dave)}"
            ),
        )

    return RecordingIntegrity(ok=True, reason=REASON_OK, detail="")
