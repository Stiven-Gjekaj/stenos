"""The sink against py-cord's real receive path, rather than against write alone.

Every other test calls ``sink.write`` directly, which is why a release that
changed what the router expects of a sink passed the whole suite and then failed
on the first real recording. These tests construct py-cord's own reader, so the
contract is checked against the installed version rather than assumed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from discord.voice.receive.reader import AudioReader

from helpers import ScriptedClock
from stenos.sink import TimestampedSink, _decoded


class Client:
    """The parts of a voice client the reader reads while being constructed."""

    mode: ClassVar[str] = "aead_xchacha20_poly1305_rtpsize"
    secret_key: ClassVar[list[int]] = list(range(32))

    def __init__(self) -> None:
        self._connection = SimpleNamespace(dave_session=None, dave_protocol_version=0)


def test_the_reader_accepts_this_sink() -> None:
    # The failure this covers is an AttributeError raised while the reader is
    # being built, before any audio moves, so constructing one is the whole
    # test. start is left off so no threads run.
    reader = AudioReader(TimestampedSink(), Client(), start=False)

    assert reader.sink.__class__ is TimestampedSink
    assert reader.packet_router is not None
    assert reader.event_router is not None


def test_the_router_registers_no_events_for_this_sink() -> None:
    # Audio arrives through write, not through the event router, so subscribing
    # to nothing is correct. Asserting it keeps a future listener from being
    # added without a matching handler.
    reader = AudioReader(TimestampedSink(), Client(), start=False)

    assert reader.event_router._event_listeners == {}


def test_audio_is_requested_decoded_rather_than_as_opus() -> None:
    # The decoder consults this to decide whether to decode. Opus frames would
    # reach write instead of linear audio, and every duration derived from byte
    # counts would be wrong.
    assert TimestampedSink().is_opus() is False


class VoiceData:
    """The shape py-cord 2.8 hands to a sink."""

    def __init__(self, pcm: bytes, timestamp: int | None = None) -> None:
        self.pcm = pcm
        self.packet = None if timestamp is None else SimpleNamespace(timestamp=timestamp)


def test_the_current_calling_convention_is_understood() -> None:
    payload, speaker, ticks = _decoded(VoiceData(b"\x01\x02", 96000), SimpleNamespace(id=77))

    assert payload == b"\x01\x02"
    assert speaker == 77
    assert ticks == 96000


def test_the_previous_calling_convention_still_works() -> None:
    # No packet came with the audio, so there is no media clock to read and the
    # sink has to fall back to arrival.
    payload, speaker, ticks = _decoded(b"\x01\x02", 77)

    assert payload == b"\x01\x02"
    assert speaker == 77
    assert ticks is None


@pytest.mark.parametrize(
    ("data", "user"),
    [
        (VoiceData(b""), SimpleNamespace(id=77)),
        (b"", 77),
        (None, 77),
        (SimpleNamespace(pcm=None), 77),
    ],
)
def test_a_packet_with_no_audio_is_nothing_to_record(data: Any, user: Any) -> None:
    assert _decoded(data, user) == (None, None, None)


def test_an_unknown_speaker_is_reported_rather_than_guessed() -> None:
    payload, speaker, _ = _decoded(VoiceData(b"\x01\x02"), None)

    assert payload == b"\x01\x02"
    assert speaker is None


def test_a_packet_carrying_no_media_timestamp_is_placed_by_arrival() -> None:
    # A shape that carries a packet without a usable timestamp is treated the
    # same as one that carries no packet at all, rather than reading whatever
    # the attribute happens to hold.
    _, _, ticks = _decoded(
        SimpleNamespace(pcm=b"\x01\x02", packet=SimpleNamespace(timestamp=None)), 77
    )

    assert ticks is None


def test_writing_through_the_current_convention_records_a_segment() -> None:
    ticks = iter([0.0, 0.02, 0.04])
    sink = TimestampedSink(clock=lambda: next(ticks))
    member = SimpleNamespace(id=77)

    sink.write(VoiceData(b"\x11\x22" * 480), member)

    segments = sink.segments()
    assert len(segments) == 1
    assert segments[0].user_id == 77
    assert sink.packet_count == 1


def test_audio_from_an_unknown_speaker_is_counted_not_buffered() -> None:
    sink = TimestampedSink()

    sink.write(VoiceData(b"\x11\x22" * 480), None)

    # Kept out of the transcript, but not lost silently: a recording can say it
    # discarded audio it could not attribute.
    assert sink.segments() == []
    assert sink.packet_count == 0
    assert sink.unattributed_packets == 1


def test_a_speaker_learned_late_is_attributed_from_then_on() -> None:
    # ssrc_user_map is populated asynchronously, so the first packets of a call
    # can arrive before Discord has said who they belong to. Those are counted
    # and dropped, and everything after the mapping arrives is recorded
    # normally rather than the whole speaker being written off.
    sink = TimestampedSink()

    sink.write(VoiceData(b"\x11\x22" * 480), None)
    sink.write(VoiceData(b"\x11\x22" * 480), None)
    sink.write(VoiceData(b"\x11\x22" * 480), SimpleNamespace(id=77))

    assert sink.unattributed_packets == 2
    assert sink.packet_count == 1
    assert [segment.user_id for segment in sink.segments()] == [77]


def test_an_unattributed_packet_does_not_open_the_recording() -> None:
    # The recording origin is the first packet that could be placed on the
    # timeline. Starting it on one nobody can be attributed would put the first
    # real speaker later than they spoke.
    sink = TimestampedSink(clock=ScriptedClock([0.0, 5.0]))

    sink.write(VoiceData(b"\x11\x22" * 480), None)
    sink.write(VoiceData(b"\x11\x22" * 480), SimpleNamespace(id=77))

    assert [round(segment.start, 3) for segment in sink.segments()] == [0.0]
