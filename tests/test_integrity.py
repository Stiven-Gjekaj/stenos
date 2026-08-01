"""Tests for deciding whether a finished recording captured anything."""

from __future__ import annotations

from collections.abc import Callable

from stenos.integrity import (
    REASON_ALL_SILENT,
    REASON_NO_PACKETS,
    REASON_OK,
    check_recording,
)
from stenos.sink import TimestampedSink
from stenos.voice import DaveState, DaveSupport

SUPPORTED = DaveSupport(available=True, version="0.1.6", protocol_version=1)


def _clock(*values: float) -> Callable[[], float]:
    """A clock returning the given instants in order, then holding the last."""
    remaining = list(values)
    last = values[-1] if values else 0.0

    def tick() -> float:
        nonlocal last
        if remaining:
            last = remaining.pop(0)
        return last

    return tick


def _sink_with(payload: bytes, *, packets: int = 1) -> TimestampedSink:
    """A sink holding the given payload, written as consecutive packets."""
    sink = TimestampedSink(clock=_clock(*[i * 0.02 for i in range(packets + 1)]))
    for _ in range(packets):
        sink.write(payload, 42)
    return sink


def _dave(*, session: bool, ready: bool, version: int = 1) -> DaveState:
    return DaveState(
        support=SUPPORTED,
        negotiated_version=version,
        session_present=session,
        ready=ready,
        status="active" if ready else "inactive",
    )


def test_a_recording_with_audio_is_worth_transcribing() -> None:
    verdict = check_recording(_sink_with(b"\x11\x22" * 960))

    assert verdict.ok is True
    assert verdict.reason == REASON_OK
    assert verdict.detail == ""


def test_no_packets_is_reported_rather_than_transcribed() -> None:
    verdict = check_recording(TimestampedSink())

    assert verdict.ok is False
    assert verdict.reason == REASON_NO_PACKETS
    # Without any encryption state to go on, both plausible causes are named
    # rather than one being guessed at.
    assert "nobody spoke" in verdict.detail
    assert "libopus" in verdict.detail


def test_no_packets_names_the_encryption_state_when_it_explains_the_failure() -> None:
    verdict = check_recording(TimestampedSink(), _dave(session=False, ready=False, version=0))

    assert verdict.reason == REASON_NO_PACKETS
    assert "no session" in verdict.detail
    # The generic guess is replaced, not appended to, when there is a real
    # answer available.
    assert "nobody spoke" not in verdict.detail


def test_a_ready_session_is_not_blamed_for_an_empty_recording() -> None:
    verdict = check_recording(TimestampedSink(), _dave(session=True, ready=True))

    assert verdict.reason == REASON_NO_PACKETS
    assert "nobody spoke" in verdict.detail


def test_a_missing_library_is_called_out_directly() -> None:
    absent = DaveState(
        support=DaveSupport(available=False, version=None, protocol_version=0),
        negotiated_version=0,
        session_present=False,
        ready=False,
        status="absent",
    )

    verdict = check_recording(TimestampedSink(), absent)

    assert "not installed" in verdict.detail


def test_silence_throughout_is_distinguished_from_no_audio() -> None:
    verdict = check_recording(_sink_with(b"\x00" * 1920, packets=3))

    assert verdict.ok is False
    assert verdict.reason == REASON_ALL_SILENT
    assert "silence" in verdict.detail


def test_one_non_silent_sample_is_enough_to_transcribe() -> None:
    # A call is mostly silence, and a recording must not be discarded because
    # most of it is quiet. Any content at all makes it worth transcribing.
    sink = _sink_with(b"\x00" * 1919 + b"\x01")

    assert check_recording(sink).ok is True


def test_a_segment_holding_no_bytes_is_not_called_silent() -> None:
    # Zero bytes is the no-packets case, which has its own reason and its own
    # explanation. Reporting it as silence would point at the wrong cause.
    sink = TimestampedSink()

    assert check_recording(sink).reason == REASON_NO_PACKETS
