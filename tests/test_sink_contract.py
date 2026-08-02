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

    def __init__(self, pcm: bytes) -> None:
        self.pcm = pcm


def test_the_current_calling_convention_is_understood() -> None:
    payload, speaker = _decoded(VoiceData(b"\x01\x02"), SimpleNamespace(id=77))

    assert payload == b"\x01\x02"
    assert speaker == 77


def test_the_previous_calling_convention_still_works() -> None:
    payload, speaker = _decoded(b"\x01\x02", 77)

    assert payload == b"\x01\x02"
    assert speaker == 77


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
    assert _decoded(data, user) == (None, None)


def test_an_unknown_speaker_is_reported_rather_than_guessed() -> None:
    payload, speaker = _decoded(VoiceData(b"\x01\x02"), None)

    assert payload == b"\x01\x02"
    assert speaker is None


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
