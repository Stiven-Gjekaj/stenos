"""Tests for gap-based segmentation and the timeline segments are placed on."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from unittest import mock

import pytest

from helpers import ScriptedClock
from stenos import sink as sink_module
from stenos.audio import TARGET_SAMPLE_RATE
from stenos.sink import (
    BYTES_PER_SECOND,
    DEFAULT_MAX_SEGMENT,
    DISCORD_CHANNELS,
    DISCORD_SAMPLE_RATE,
    MAX_CLOCK_DISAGREEMENT,
    MONO_BYTES_PER_SECOND,
    Segment,
    TimestampedSink,
    opus_library_candidates,
)
from stenos.spill import SpillWriter

#: One Discord voice frame is 20 ms of 48 kHz stereo signed 16 bit audio.
FRAME_BYTES = BYTES_PER_SECOND // 50
assert FRAME_BYTES == 3840
FRAME = b"\x00" * FRAME_BYTES


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
    # Half of what arrived: a channel is dropped as each packet is written.
    assert len(segment.pcm) == 3 * FRAME_BYTES // DISCORD_CHANNELS


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


def test_writes_after_cleanup_are_dropped() -> None:
    # This used to open a fresh segment, which was the mechanical consequence
    # of cleanup emptying the open ones rather than anything anybody wanted:
    # the reducer has been drained and joined by then, so that segment would
    # never be reduced, and the buffers would grow while transcription read
    # them. The clock is not read either, so a refused packet costs nothing.
    sink = TimestampedSink(clock=ScriptedClock([0.0]), segment_gap=0.4)
    sink.write(FRAME, 1)
    sink.cleanup()

    sink.write(FRAME, 1)

    assert len(sink.segments()) == 1


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
    segment = Segment(user_id=1, start=2.0, pcm=bytearray(MONO_BYTES_PER_SECOND))

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
    def __init__(self, member_id: int) -> None:
        self.id = member_id


def record_media(
    packets: Sequence[tuple[float, int, int]],
    *,
    segment_gap: float = 0.4,
    max_segment: float = DEFAULT_MAX_SEGMENT,
) -> TimestampedSink:
    """Drive a sink with (arrival time, media timestamp, user) packets."""
    sink = TimestampedSink(
        segment_gap=segment_gap,
        max_segment=max_segment,
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


# Reducing what closes. A segment that can no longer grow is handed to a worker
# rather than reduced where it closes, because reducing thirty seconds of audio
# takes about seventy milliseconds and packets arrive every twenty.


def test_a_closed_segment_is_reduced_and_the_open_one_is_not() -> None:
    # The gap closes the first segment. The second is still being written to,
    # so it stays at the rate it arrived at.
    sink = record([(0.0, 1), (0.02, 1), (5.0, 1)], segment_gap=0.4)
    sink.cleanup()

    first, second = sink.segments()
    assert first.sample_rate == TARGET_SAMPLE_RATE
    # cleanup retires whatever was still open, so by now both are reduced.
    assert second.sample_rate == TARGET_SAMPLE_RATE


def test_cleanup_waits_for_the_worker_before_returning() -> None:
    # Everything after cleanup reads the segments. Returning while the worker is
    # still going would race the reduction of the last of them.
    sink = record([(0.0, 1), (5.0, 1), (10.0, 1)], segment_gap=0.4)

    sink.cleanup()

    assert all(segment.sample_rate == TARGET_SAMPLE_RATE for segment in sink.segments())
    assert sink._reducer is None


def test_buffered_bytes_falls_as_segments_close() -> None:
    sink = record([(index * 0.02, 1) for index in range(50)], segment_gap=0.4)
    while_open = sink.buffered_bytes

    sink.cleanup()

    # One second of audio: 96,000 bytes mono at the rate it arrives, 32,000 once
    # it has been reduced.
    assert while_open == pytest.approx(MONO_BYTES_PER_SECOND, rel=0.05)
    assert sink.buffered_bytes == pytest.approx(MONO_BYTES_PER_SECOND / 3, rel=0.05)


def test_an_empty_sink_holds_nothing() -> None:
    assert TimestampedSink().buffered_bytes == 0


def test_cleanup_is_safe_with_no_worker_ever_started() -> None:
    sink = TimestampedSink()

    sink.cleanup()

    assert sink.finished is True


# Closing for length. Without it one speaker who never pauses holds the whole
# call in a single segment, and reducing that segment costs more the longer the
# call runs.


def test_a_segment_closes_once_it_reaches_the_maximum_length() -> None:
    # Packets 20 ms apart with no gap ever exceeding the threshold, so only the
    # length can split this.
    packets = [(index * 0.02, index * FRAME_TICKS, 1) for index in range(300)]

    starts = boundaries(record_media(packets, max_segment=2.0))

    assert starts == [(1, 0.0), (1, 2.0), (1, 4.0)]


def test_the_length_cap_does_not_split_a_segment_that_ends_first() -> None:
    packets = [(index * 0.02, index * FRAME_TICKS, 1) for index in range(50)]

    assert boundaries(record_media(packets, max_segment=30.0)) == [(1, 0.0)]


def test_a_non_positive_maximum_length_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_segment"):
        TimestampedSink(max_segment=0.0)


def test_a_long_recording_holds_a_sixth_of_what_arrived() -> None:
    # The measurement the whole release is for, end to end rather than per
    # segment: two minutes of continuous speech, crossing several segment
    # boundaries, held at the rate a model reads instead of the rate it arrived.
    packets = 120 * 50
    speech = packets * 0.02

    class Ticking:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            now = self.now
            self.now += 0.02
            return now

    sink = TimestampedSink(clock=Ticking(), max_segment=30.0)
    for _ in range(packets):
        sink.write(FRAME, 11)
    sink.cleanup()

    held_per_second = sink.buffered_bytes / speech
    assert held_per_second == pytest.approx(BYTES_PER_SECOND / 6, rel=0.01)
    # The cap did its work: no one segment holds the whole two minutes.
    assert len(sink.segments()) == 4


def test_only_one_reducer_is_started_when_two_threads_retire_at_once() -> None:
    # The router retires a segment that closed and cleanup retires whatever was
    # still open, from different threads. Unguarded, both find no worker and
    # both start one, and only the one assigned last is tracked: the other
    # never gets the sentinel and cleanup joins the wrong thread.
    started: list[threading.Thread] = []
    real = threading.Thread

    class Slow(real):  # type: ignore[misc, valid-type]
        """Slow to construct, so the check and the assignment can interleave."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            time.sleep(0.05)
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            started.append(self)

    sink = TimestampedSink()
    with mock.patch.object(threading, "Thread", Slow):
        racers = [
            real(target=sink._retire, args=(Segment(user_id=index, start=0.0),)) for index in (1, 2)
        ]
        for racer in racers:
            racer.start()
        for racer in racers:
            racer.join(timeout=5.0)

    assert len(started) == 1
    sink.cleanup()


def test_a_packet_arriving_after_cleanup_is_refused() -> None:
    # py-cord's router keeps draining briefly after a recording is stopped.
    # Cleanup has by then closed every segment and joined the reducer, so a
    # packet accepted afterwards opens a segment nothing will reduce and grows
    # the buffers while transcription is already reading them.
    sink = record([(0.0, 11)])
    sink.cleanup()
    before = sink.packet_count
    held = sink.buffered_bytes

    sink.write(FRAME, 11)

    assert sink.packet_count == before
    assert sink.buffered_bytes == held


def test_a_truncated_packet_does_not_end_the_recording() -> None:
    # write runs on py-cord's router thread. Anything raising there stops the
    # thread, and the recording stops with it.
    sink = TimestampedSink(clock=ScriptedClock([0.0, 0.02]))

    sink.write(FRAME + b"\x07", 1)
    sink.write(FRAME, 1)

    assert sink.packet_count == 2


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", ["/opt/homebrew/lib/libopus.0.dylib"]),
        ("linux", ["libopus.so.0", "libopus.so"]),
        # Neither macOS, Windows, nor Linux. The platform key treats it as
        # Linux and picks Linux names, and the candidate list used to read the
        # platform a second time and add none of them, leaving the fallback
        # search empty on a BSD.
        ("freebsd14", ["libopus.so.0", "libopus.so"]),
    ],
)
def test_the_opus_search_follows_the_platform_key(platform: str, expected: list[str]) -> None:
    with mock.patch.object(sys, "platform", platform):
        candidates = opus_library_candidates()

    assert candidates, f"nothing to try on {platform}"
    for name in expected:
        assert name in candidates


def test_windows_has_no_fallback_search_of_its_own() -> None:
    # py-cord bundles a binary for Windows, so the default search finds one and
    # there is nothing useful to guess at.
    with mock.patch.object(sys, "platform", "win32"):
        assert opus_library_candidates() == []


def test_a_frozen_build_looks_inside_its_own_payload_first(tmp_path: Path) -> None:
    # A standalone executable must not depend on the host having libopus.
    with (
        mock.patch.object(sys, "platform", "linux"),
        mock.patch.object(sink_module, "bundle_directory", lambda: tmp_path),
    ):
        candidates = opus_library_candidates()

    assert candidates[0].startswith(str(tmp_path))
    assert any("discord" in candidate for candidate in candidates)


# A recording is held in memory and written once at the end, which stops being
# possible on a host whose memory the call outgrows. Past a ceiling it moves to
# disk instead of the recording ending, which is what used to happen.


def store_for(tmp_path: Path) -> SpillWriter:
    return SpillWriter(
        tmp_path / "call.partial",
        channel="general",
        started_at=datetime(2026, 8, 9, tzinfo=UTC),
        sample_rate=TARGET_SAMPLE_RATE,
    )


def spilling_sink(
    packets: Sequence[tuple[float, int]],
    store: SpillWriter,
    *,
    spill_above: int,
) -> TimestampedSink:
    sink = TimestampedSink(
        segment_gap=0.4,
        clock=ScriptedClock([timestamp for timestamp, _ in packets]),
        storage=store,
        spill_above=spill_above,
    )
    for _, user in packets:
        sink.write(FRAME, user)
    return sink


def test_a_recording_under_the_ceiling_never_touches_the_disk(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    sink = spilling_sink([(0.0, 1), (0.02, 1), (5.0, 1)], store, spill_above=10_000_000)

    sink.cleanup()

    assert sink.spilling is False
    assert all(segment.spill is None for segment in sink.segments())
    assert sink.buffered_bytes == sink.total_bytes


def test_crossing_the_ceiling_moves_what_is_already_settled(tmp_path: Path) -> None:
    # Not only the segment that crossed it. The point is to free the memory the
    # recording is already holding, which is nearly all in the earlier ones.
    store = store_for(tmp_path)
    sink = spilling_sink([(0.0, 1), (5.0, 1), (10.0, 1), (15.0, 1)], store, spill_above=1)

    sink.cleanup()

    assert sink.spilling is True
    assert all(segment.spill is not None for segment in sink.segments())


def test_spilling_frees_the_memory_and_keeps_the_audio(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    sink = spilling_sink([(0.0, 1), (5.0, 1), (10.0, 1)], store, spill_above=1)
    sink.cleanup()

    assert sink.buffered_bytes == 0
    assert sink.total_bytes > 0
    # The audio is still readable, which is the only thing that matters.
    assert all(segment.snapshot()[0] for segment in sink.segments())


def test_a_sink_with_nowhere_to_spill_keeps_holding_it(tmp_path: Path) -> None:
    # The behaviour every existing recording has, and the fallback when the
    # output directory cannot be written to.
    sink = record([(0.0, 1), (5.0, 1), (10.0, 1)], segment_gap=0.4)

    sink.cleanup()

    assert sink.spills is False
    assert sink.spilling is False
    assert sink.buffered_bytes == sink.total_bytes > 0


def test_a_disk_that_refuses_the_write_costs_memory_and_not_the_recording(
    tmp_path: Path,
) -> None:
    # The reducer thread runs for the whole call. An exception escaping it ends
    # the thread, after which nothing reduces or spills any later segment, so
    # the failure has to stop at the segment it happened to.
    store = store_for(tmp_path)
    store.close()  # Every append now raises.
    sink = spilling_sink([(0.0, 1), (5.0, 1), (10.0, 1)], store, spill_above=1)

    sink.cleanup()

    assert [segment.spill for segment in sink.segments()] == [None, None, None]
    assert sink.total_bytes > 0
    assert all(segment.sample_rate == TARGET_SAMPLE_RATE for segment in sink.segments())


def test_a_segment_still_being_written_to_is_left_in_memory(tmp_path: Path) -> None:
    # extend appends to the buffer spilling empties, so moving an open segment
    # would write out what it holds and then collect the rest of that speaker's
    # sentence into a buffer nothing ever reads.
    store = store_for(tmp_path)
    arrivals = [0.0, 5.0, 10.0, 15.0, 15.02]
    sink = TimestampedSink(
        segment_gap=0.4,
        clock=ScriptedClock(arrivals),
        storage=store,
        spill_above=1,
    )
    for _ in arrivals[:-1]:
        sink.write(FRAME, 1)
    # The reducer runs on its own thread, so the ceiling is crossed there.
    for _ in range(200):
        if sink.spilling:
            break
        time.sleep(0.01)

    assert sink.spilling is True
    latest = sink.segments()[-1]
    assert latest.spill is None
    before = latest.held()

    sink.write(FRAME, 1)

    assert latest.held() > before
