"""Tests for gap-based segmentation and arrival timestamping."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from stenos.sink import BYTES_PER_SECOND, Segment, TimestampedSink

#: One Discord voice frame is 20 ms of 48 kHz stereo signed 16 bit audio.
FRAME_BYTES = BYTES_PER_SECOND // 50
FRAME = b"\x00" * FRAME_BYTES


class ScriptedClock:
    """Return a predetermined sequence of timestamps, one per call."""

    def __init__(self, times: Sequence[float]) -> None:
        self._times = list(times)
        self._index = 0

    def __call__(self) -> float:
        value = self._times[self._index]
        self._index += 1
        return value


def record(
    packets: Sequence[tuple[float, int]],
    *,
    segment_gap: float = 0.4,
    payload: bytes = FRAME,
) -> TimestampedSink:
    """Drive a sink with a synthetic sequence of (arrival time, user) packets."""
    sink = TimestampedSink(
        segment_gap=segment_gap,
        clock=ScriptedClock([timestamp for timestamp, _ in packets]),
    )
    for _, user in packets:
        sink.write(payload, user)
    return sink


def boundaries(sink: TimestampedSink) -> list[tuple[int, float]]:
    return [(segment.user_id, round(segment.start, 6)) for segment in sink.segments()]


def test_no_packets_produces_no_segments() -> None:
    sink = TimestampedSink()
    assert sink.segments() == []
    assert sink.packet_count == 0
    assert sink.user_ids == frozenset()
    assert sink.duration == pytest.approx(0.0)


def test_continuous_packets_form_one_segment() -> None:
    sink = record([(0.0, 1), (0.02, 1), (0.04, 1), (0.06, 1)])

    assert boundaries(sink) == [(1, 0.0)]
    assert sink.packet_count == 4


def test_gap_larger_than_threshold_opens_a_new_segment() -> None:
    sink = record([(0.0, 1), (0.02, 1), (1.5, 1)], segment_gap=0.4)

    assert boundaries(sink) == [(1, 0.0), (1, 1.5)]


def test_gap_exactly_at_threshold_stays_in_the_same_segment() -> None:
    sink = record([(0.0, 1), (0.4, 1)], segment_gap=0.4)

    assert boundaries(sink) == [(1, 0.0)]


def test_gap_just_under_threshold_stays_in_the_same_segment() -> None:
    sink = record([(0.0, 1), (0.39, 1)], segment_gap=0.4)

    assert boundaries(sink) == [(1, 0.0)]


def test_gap_just_over_threshold_opens_a_new_segment() -> None:
    sink = record([(0.0, 1), (0.41, 1)], segment_gap=0.4)

    assert boundaries(sink) == [(1, 0.0), (1, 0.41)]


def test_gap_is_measured_from_the_previous_packet_not_the_segment_start() -> None:
    # Steps of 0.3 s never exceed a 0.4 s threshold even though the segment
    # spans 0.9 s in total.
    sink = record([(0.0, 1), (0.3, 1), (0.6, 1), (0.9, 1)], segment_gap=0.4)

    assert boundaries(sink) == [(1, 0.0)]


def test_origin_is_the_first_packet_of_the_recording() -> None:
    sink = record([(100.0, 1), (100.02, 1)])

    assert boundaries(sink) == [(1, 0.0)]


def test_late_joiner_keeps_its_offset_from_the_recording_origin() -> None:
    # The failure mode this sink exists to prevent: user 2 starts speaking ten
    # seconds in and must land ten seconds in, not at the start.
    sink = record([(0.0, 1), (0.02, 1), (10.0, 2), (10.02, 2)])

    assert boundaries(sink) == [(1, 0.0), (2, 10.0)]


def test_gaps_are_tracked_per_user() -> None:
    # User 1 transmits continuously while user 2 pauses. Only user 2 splits.
    packets = [
        (0.0, 1),
        (0.1, 2),
        (0.2, 1),
        (0.4, 1),
        (0.6, 1),
        (0.8, 2),
    ]
    sink = record(packets, segment_gap=0.4)

    assert boundaries(sink) == [(1, 0.0), (2, 0.1), (2, 0.8)]


def test_interleaved_users_do_not_reset_each_others_segments() -> None:
    packets = [(0.0, 1), (0.05, 2), (0.10, 1), (0.15, 2), (0.20, 1)]
    sink = record(packets, segment_gap=0.4)

    assert boundaries(sink) == [(1, 0.0), (2, 0.05)]


def test_segments_are_returned_in_call_order() -> None:
    # User 3 opens the recording, then user 1 speaks later, then user 3 again.
    # Insertion order already differs from start order for user 3's second turn.
    sink = record([(0.0, 3), (2.0, 1), (4.0, 3)], segment_gap=0.4)

    starts = [segment.start for segment in sink.segments()]
    assert starts == sorted(starts)
    assert boundaries(sink) == [(3, 0.0), (1, 2.0), (3, 4.0)]


def test_payload_is_appended_to_the_open_segment() -> None:
    sink = record([(0.0, 1), (0.02, 1), (0.04, 1)])

    (segment,) = sink.segments()
    assert len(segment.pcm) == 3 * FRAME_BYTES


def test_packet_count_and_user_ids_are_reported() -> None:
    sink = record([(0.0, 1), (0.02, 2), (0.04, 1)])

    assert sink.packet_count == 3
    assert sink.user_ids == frozenset({1, 2})


def test_duration_spans_first_to_last_packet() -> None:
    sink = record([(4.0, 1), (9.5, 1)], segment_gap=10.0)

    assert sink.duration == pytest.approx(5.5)


def test_cleanup_marks_the_sink_finished() -> None:
    sink = record([(0.0, 1)])
    sink.cleanup()

    assert sink.finished is True
    assert boundaries(sink) == [(1, 0.0)]


def test_writes_after_cleanup_open_a_fresh_segment() -> None:
    sink = TimestampedSink(clock=ScriptedClock([0.0, 0.02]), segment_gap=0.4)
    sink.write(FRAME, 1)
    sink.cleanup()
    sink.write(FRAME, 1)

    assert len(sink.segments()) == 2


def test_filtered_users_are_ignored() -> None:
    sink = TimestampedSink(
        clock=ScriptedClock([0.0, 0.02]),
        filters={"users": [1], "time": 0, "max_size": 0},
    )
    sink.write(FRAME, 1)
    sink.write(FRAME, 2)

    assert sink.user_ids == frozenset({1})


def test_negative_gap_is_rejected() -> None:
    with pytest.raises(ValueError, match="segment_gap"):
        TimestampedSink(segment_gap=-0.1)


def test_zero_gap_splits_on_every_packet() -> None:
    sink = record([(0.0, 1), (0.02, 1), (0.04, 1)], segment_gap=0.0)

    assert boundaries(sink) == [(1, 0.0), (1, 0.02), (1, 0.04)]


def test_segment_duration_derives_from_the_byte_count() -> None:
    segment = Segment(user_id=1, start=2.0, pcm=bytearray(BYTES_PER_SECOND))

    assert segment.duration == pytest.approx(1.0)
    assert segment.end == pytest.approx(3.0)


def test_empty_segment_has_zero_duration() -> None:
    assert Segment(user_id=1, start=0.0).duration == pytest.approx(0.0)
