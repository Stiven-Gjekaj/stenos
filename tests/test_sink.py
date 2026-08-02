"""Tests for gap-based segmentation and the timeline segments are placed on."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

import pytest

from stenos.sink import (
    BYTES_PER_SECOND,
    DISCORD_SAMPLE_RATE,
    MAX_CLOCK_DISAGREEMENT,
    Segment,
    TimestampedSink,
)

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


# The media clock. Every test below drives the sink through the shape py-cord
# 2.8 uses, where a packet arrives with the RTP timestamp it was decoded from,
# and where arrival no longer tracks speech because a jitter buffer sits in
# front of the sink.

#: Samples one 20 ms frame advances an RTP timestamp by.
FRAME_TICKS = 960


class Packet:
    """The part of an RTP packet the sink reads."""

    def __init__(self, timestamp: int) -> None:
        self.timestamp = timestamp


class Data:
    """The shape py-cord 2.8 hands to a sink."""

    def __init__(self, timestamp: int, pcm: bytes = FRAME) -> None:
        self.pcm = pcm
        self.packet = Packet(timestamp)


class Member:
    def __init__(self, id: int) -> None:
        self.id = id


def record_media(
    packets: Sequence[tuple[float, int, int]],
    *,
    segment_gap: float = 0.4,
) -> TimestampedSink:
    """Drive a sink with (arrival time, media timestamp, user) packets."""
    sink = TimestampedSink(
        segment_gap=segment_gap,
        clock=ScriptedClock([arrival for arrival, _, _ in packets]),
    )
    for _, ticks, user in packets:
        sink.write(Data(ticks), Member(user))
    return sink


def test_segments_do_not_overlap_when_delivery_outruns_the_audio() -> None:
    # The recorded failure. A burst delivered five seconds of audio in one, and
    # segments timed by arrival ran past the segments that followed them.
    base = 1_000_000
    burst = [(0.2 + index * 0.004, base + index * FRAME_TICKS, 1) for index in range(50)]
    later = [(1.5 + index * 0.02, base + 96_000 + index * FRAME_TICKS, 1) for index in range(10)]

    segments = record_media(burst + later).segments()

    assert len(segments) == 2
    assert segments[0].duration == pytest.approx(1.0)
    # One second of audio starting at 0.0, and the next segment starts after it
    # rather than a fifth of the way into it.
    assert segments[0].end == pytest.approx(1.0)
    assert segments[1].start == pytest.approx(2.0)
    for earlier, following in pairwise(segments):
        assert earlier.end <= following.start


def test_a_gap_in_the_audio_splits_even_when_packets_arrive_together() -> None:
    # Both packets land in the same millisecond, so arrival says there was no
    # silence. The media clock says a second passed, and it is right.
    sink = record_media([(0.0, 1_000_000, 1), (0.001, 1_000_000 + 48_000, 1)])

    assert boundaries(sink) == [(1, 0.0), (1, 1.0)]


def test_audio_that_is_continuous_stays_one_segment_however_it_arrives() -> None:
    # The converse. Arrival gaps far past the threshold, media timestamps
    # contiguous, so nothing was actually missed and nothing should split.
    packets = [(index * 2.0, 500 + index * FRAME_TICKS, 1) for index in range(5)]

    assert boundaries(record_media(packets)) == [(1, 0.0)]


def test_timestamps_wrapping_past_the_field_do_not_jump_backwards() -> None:
    # RTP timestamps are unsigned 32 bit and wrap roughly once a day. Read as
    # plain integers the second packet lands billions of seconds before the
    # first, and the recording would be nonsense from that point on.
    packets = [(0.0, 2**32 - FRAME_TICKS, 1), (0.02, 0, 1), (0.04, FRAME_TICKS, 1)]

    assert boundaries(record_media(packets)) == [(1, 0.0)]


#: A base a restarted stream might pick. The field is chosen at random across
#: its whole range, so a new one lands more than half a day from the old one far
#: more often than it lands near it.
RESTART_TICKS = 2_500_000_000


def test_a_stream_that_restarts_is_placed_by_arrival_rather_than_hours_away() -> None:
    # A reconnect gives a participant a fresh, unrelated timestamp base. Trusting
    # it would put the rest of their call half a day from where it belongs, so
    # arrival settles the disagreement and the media clock starts again.
    packets = [
        (0.0, 1_000_000, 1),
        (0.02, 1_000_000 + FRAME_TICKS, 1),
        (2.0, RESTART_TICKS, 1),
    ]

    assert boundaries(record_media(packets)) == [(1, 0.0), (1, 2.0)]


def test_a_restarted_stream_keeps_its_new_base_for_what_follows() -> None:
    packets = [
        (0.0, 1_000_000, 1),
        (2.0, RESTART_TICKS, 1),
        (2.004, RESTART_TICKS + FRAME_TICKS, 1),
        (2.008, RESTART_TICKS + 2 * FRAME_TICKS, 1),
    ]

    segments = record_media(packets).segments()

    assert len(segments) == 2
    # Three frames after the restart, measured on the new base rather than on
    # the compressed arrivals that delivered them.
    assert segments[1].start == pytest.approx(2.0)
    assert segments[1].duration == pytest.approx(0.06)


def test_a_disagreement_a_buffer_could_explain_is_not_treated_as_a_restart() -> None:
    # The media clock running well ahead of arrival is exactly what a burst of
    # buffered audio looks like, and re-basing on it would undo the fix. Only a
    # disagreement no amount of buffering could produce counts as a restart.
    ahead = int(MAX_CLOCK_DISAGREEMENT * DISCORD_SAMPLE_RATE) - FRAME_TICKS
    packets = [(0.0, 1_000_000, 1), (0.02, 1_000_000 + ahead, 1)]

    segments = record_media(packets).segments()

    assert segments[-1].start == pytest.approx(ahead / DISCORD_SAMPLE_RATE)


def test_participants_with_unrelated_bases_keep_their_offsets() -> None:
    # Two participants count from different arbitrary values, so their
    # timestamps cannot be compared to each other. Arrival places the first
    # packet of each, and the media clock takes over from there.
    packets = [
        (0.0, 1_000_000, 1),
        (5.0, 40_000_000, 2),
        (5.004, 1_000_000 + 48_000 * 5, 1),
        (5.008, 40_000_000 + FRAME_TICKS, 2),
    ]

    sink = record_media(packets)

    assert boundaries(sink) == [(1, 0.0), (1, 5.0), (2, 5.0)]


def test_duration_reaches_the_end_of_the_audio_not_the_last_arrival() -> None:
    burst = [(0.0 + index * 0.001, 1_000_000 + index * FRAME_TICKS, 1) for index in range(50)]

    sink = record_media(burst)

    # Fifty frames is a second of audio, delivered in a twentieth of one.
    assert sink.duration == pytest.approx(1.0)
