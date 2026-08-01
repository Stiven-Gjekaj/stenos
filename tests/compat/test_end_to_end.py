"""The full offline pipeline on the running platform.

A synthetic packet sequence with known gaps is fed through the sink, converted,
transcribed with the mock backend, and merged. This is the usability guarantee:
no Discord connection, no model weights, and it must hold on every supported
operating system.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stenos.bot import run_pipeline
from stenos.config import Config
from stenos.sink import BYTES_PER_SECOND, TimestampedSink
from stenos.transcribe import MockBackend

RECORDED_AT = datetime(2026, 8, 1, 16, 14, 43, tzinfo=UTC)

#: One 20 ms Discord voice frame of non-silent audio.
FRAME = b"\x01\x00" * (BYTES_PER_SECOND // 100)

SPEAKERS = {11: "Stiven", 22: "Enxhi", 33: "Zoë 🎧"}

TRANSCRIPTS = [
    "so about the asset pipeline",
    "which part broke",
    "the exporter, it stopped writing normals",
    "përshëndetje 会議",
]


class ScriptedClock:
    def __init__(self, times: Sequence[float]) -> None:
        self._times = list(times)
        self._index = 0

    def __call__(self) -> float:
        value = self._times[self._index]
        self._index += 1
        return value


def burst(start: float, user: int, frames: int = 40) -> list[tuple[float, int]]:
    """A continuous utterance: frames arriving 20 ms apart."""
    return [(start + index * 0.02, user) for index in range(frames)]


#: Four utterances separated by gaps far larger than the 0.4 s threshold, with
#: a third speaker who only joins two minutes in.
CONVERSATION = burst(0.0, 11) + burst(5.0, 22) + burst(12.0, 11) + burst(120.0, 33)


def record(packets: Sequence[tuple[float, int]]) -> TimestampedSink:
    sink = TimestampedSink(
        segment_gap=0.4,
        clock=ScriptedClock([timestamp for timestamp, _ in packets]),
    )
    for _, user in packets:
        sink.write(FRAME, user)
    sink.cleanup()
    return sink


@pytest.fixture
def result(tmp_path: Path):  # type: ignore[no-untyped-def]
    return run_pipeline(
        record(CONVERSATION),
        SPEAKERS,
        channel_name="voice: general <main>",
        config=Config(discord_token="token", output_dir=tmp_path, whisper_model="small"),
        backend=MockBackend(texts=TRANSCRIPTS),
        recorded_at=RECORDED_AT,
    )


def test_every_utterance_becomes_one_segment(result) -> None:  # type: ignore[no-untyped-def]
    assert result.segment_count == 4


def test_all_three_speakers_are_attributed(result) -> None:  # type: ignore[no-untyped-def]
    assert result.speakers == 3


def test_the_transcript_is_ordered_and_attributed(result) -> None:  # type: ignore[no-untyped-def]
    body = result.transcript_path.read_text(encoding="utf-8")

    assert body == (
        "[00:00:00] Stiven: so about the asset pipeline\n"
        "[00:00:05] Enxhi: which part broke\n"
        "[00:00:12] Stiven: the exporter, it stopped writing normals\n"
        "[00:02:00] Zoë 🎧: përshëndetje 会議\n"
    )


def test_the_late_speaker_keeps_its_offset(result) -> None:  # type: ignore[no-untyped-def]
    # A speaker who joins two minutes in must appear two minutes in.
    body = result.transcript_path.read_text(encoding="utf-8")

    assert "[00:02:00] Zoë 🎧:" in body


def test_output_files_exist_with_a_sanitised_name(result) -> None:  # type: ignore[no-untyped-def]
    assert result.transcript_path.is_file()
    assert result.sidecar_path.is_file()
    assert ":" not in result.transcript_path.name
    assert "<" not in result.transcript_path.name


def test_no_carriage_returns_on_any_platform(result) -> None:  # type: ignore[no-untyped-def]
    assert b"\r" not in result.transcript_path.read_bytes()
    assert b"\r" not in result.sidecar_path.read_bytes()


def test_the_sidecar_records_raw_timings_and_identifiers(result) -> None:  # type: ignore[no-untyped-def]
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))

    assert [segment["user_id"] for segment in payload["segments"]] == [11, 22, 11, 33]
    assert [round(segment["start"]) for segment in payload["segments"]] == [0, 5, 12, 120]
    assert payload["speakers"]["33"] == "Zoë 🎧"
    assert payload["backend"] == "mock"


def test_the_backend_received_sixteen_kilohertz_audio(tmp_path: Path) -> None:
    backend = MockBackend(texts=TRANSCRIPTS)

    run_pipeline(
        record(CONVERSATION),
        SPEAKERS,
        channel_name="general",
        config=Config(discord_token="token", output_dir=tmp_path),
        backend=backend,
        recorded_at=RECORDED_AT,
    )

    # Forty 20 ms frames is 0.8 s, which is 12800 samples at 16 kHz.
    assert [samples for samples, _language in backend.calls] == [12_800] * 4


def test_short_utterances_never_reach_the_backend(tmp_path: Path) -> None:
    backend = MockBackend()
    packets = burst(0.0, 11, frames=2) + burst(5.0, 22, frames=40)

    run_pipeline(
        record(packets),
        SPEAKERS,
        channel_name="general",
        config=Config(discord_token="token", output_dir=tmp_path),
        backend=backend,
        recorded_at=RECORDED_AT,
    )

    assert len(backend.calls) == 1


def test_buffered_audio_is_released_after_transcription(tmp_path: Path) -> None:
    sink = record(CONVERSATION)

    run_pipeline(
        sink,
        SPEAKERS,
        channel_name="general",
        config=Config(discord_token="token", output_dir=tmp_path, keep_audio=False),
        backend=MockBackend(texts=TRANSCRIPTS),
        recorded_at=RECORDED_AT,
    )

    assert all(len(segment.pcm) == 0 for segment in sink.segments())
