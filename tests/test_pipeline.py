"""Tests for the offline recording pipeline, from sink to written files."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stenos.bot import RecordingResult, discard_audio, run_pipeline
from stenos.config import Config
from stenos.sink import BYTES_PER_SECOND, TimestampedSink
from stenos.transcribe import MockBackend

RECORDED_AT = datetime(2026, 8, 1, 16, 14, 43, tzinfo=UTC)

#: One Discord voice frame is 20 ms of audio.
FRAME = b"\x01\x00" * (BYTES_PER_SECOND // 100)


class ScriptedClock:
    def __init__(self, times: Sequence[float]) -> None:
        self._times = list(times)
        self._index = 0

    def __call__(self) -> float:
        value = self._times[self._index]
        self._index += 1
        return value


def build_sink(
    packets: Sequence[tuple[float, int]], *, segment_gap: float = 0.4
) -> TimestampedSink:
    sink = TimestampedSink(
        segment_gap=segment_gap,
        clock=ScriptedClock([timestamp for timestamp, _ in packets]),
    )
    for _, user in packets:
        sink.write(FRAME, user)
    return sink


def speech(start: float, user: int, frames: int = 40) -> list[tuple[float, int]]:
    """A continuous burst of frames from one user, 20 ms apart."""
    return [(start + index * 0.02, user) for index in range(frames)]


def config_for(tmp_path: Path, **overrides: object) -> Config:
    defaults: dict[str, object] = {
        "discord_token": "token",
        "output_dir": tmp_path,
        "min_segment": 0.3,
        "whisper_model": "small",
    }
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def run(
    tmp_path: Path,
    packets: Sequence[tuple[float, int]],
    names: dict[int, str],
    *,
    backend: MockBackend | None = None,
    **overrides: object,
) -> RecordingResult:
    return run_pipeline(
        build_sink(packets),
        names,
        channel_name="general",
        config=config_for(tmp_path, **overrides),
        backend=backend or MockBackend(),
        recorded_at=RECORDED_AT,
    )


def test_two_speakers_produce_an_ordered_transcript(tmp_path: Path) -> None:
    packets = speech(0.0, 11) + speech(5.0, 22) + speech(10.0, 11)
    backend = MockBackend(texts=["so about the asset pipeline", "which part broke", "the exporter"])

    result = run(tmp_path, packets, {11: "Alpha", 22: "Bravo"}, backend=backend)

    assert result.transcript_path.read_text(encoding="utf-8") == (
        "[00:00:00] Alpha: so about the asset pipeline\n"
        "[00:00:05] Bravo: which part broke\n"
        "[00:00:10] Alpha: the exporter\n"
    )


def test_late_joiner_lands_at_its_offset(tmp_path: Path) -> None:
    # The drift failure this project exists to avoid: a speaker who joins two
    # minutes in must appear two minutes in.
    packets = speech(0.0, 11) + speech(120.0, 22)
    backend = MockBackend(texts=["opening remarks", "joined late"])

    result = run(tmp_path, packets, {11: "Alpha", 22: "Bravo"}, backend=backend)

    assert result.transcript_path.read_text(encoding="utf-8") == (
        "[00:00:00] Alpha: opening remarks\n[00:02:00] Bravo: joined late\n"
    )


def test_both_output_files_are_written(tmp_path: Path) -> None:
    result = run(tmp_path, speech(0.0, 11), {11: "Alpha"})

    assert result.transcript_path.exists()
    assert result.sidecar_path.exists()
    assert result.transcript_path.suffix == ".txt"
    assert result.sidecar_path.suffix == ".json"


def test_sidecar_holds_raw_timings_and_identifiers(tmp_path: Path) -> None:
    packets = speech(0.0, 11) + speech(5.0, 22)

    result = run(tmp_path, packets, {11: "Alpha", 22: "Bravo"})
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))

    assert payload["channel"] == "general"
    assert payload["backend"] == "mock"
    assert payload["model"] == "small"
    assert payload["speakers"] == {"11": "Alpha", "22": "Bravo"}
    assert [segment["user_id"] for segment in payload["segments"]] == [11, 22]
    assert payload["segments"][1]["start"] == pytest.approx(5.0)


def test_short_segments_never_reach_the_backend(tmp_path: Path) -> None:
    # A two frame burst is 40 ms, far below the 0.3 s threshold.
    packets = speech(0.0, 11, frames=2) + speech(5.0, 22, frames=40)
    backend = MockBackend()

    result = run(tmp_path, packets, {11: "Alpha", 22: "Bravo"}, backend=backend)

    assert len(backend.calls) == 1
    assert result.segment_count == 1


def test_language_is_forwarded_from_configuration(tmp_path: Path) -> None:
    backend = MockBackend()

    run(tmp_path, speech(0.0, 11), {11: "Alpha"}, backend=backend, language="sq")

    assert backend.calls[0][1] == "sq"


def test_progress_is_reported_for_every_segment(tmp_path: Path) -> None:
    seen: list[tuple[int, int]] = []
    packets = speech(0.0, 11) + speech(5.0, 22)

    run_pipeline(
        build_sink(packets),
        {11: "Alpha", 22: "Bravo"},
        channel_name="general",
        config=config_for(tmp_path),
        backend=MockBackend(),
        recorded_at=RECORDED_AT,
        progress=lambda done, total: seen.append((done, total)),
    )

    assert seen == [(1, 2), (2, 2)]


def test_audio_is_discarded_after_transcription(tmp_path: Path) -> None:
    sink = build_sink(speech(0.0, 11))

    run_pipeline(
        sink,
        {11: "Alpha"},
        channel_name="general",
        config=config_for(tmp_path, keep_audio=False),
        backend=MockBackend(),
        recorded_at=RECORDED_AT,
    )

    assert all(len(segment.pcm) == 0 for segment in sink.segments())


def test_audio_is_retained_when_requested(tmp_path: Path) -> None:
    sink = build_sink(speech(0.0, 11))

    run_pipeline(
        sink,
        {11: "Alpha"},
        channel_name="general",
        config=config_for(tmp_path, keep_audio=True),
        backend=MockBackend(),
        recorded_at=RECORDED_AT,
    )

    assert any(len(segment.pcm) > 0 for segment in sink.segments())


def test_discarding_audio_keeps_the_written_transcript(tmp_path: Path) -> None:
    sink = build_sink(speech(0.0, 11))
    result = run_pipeline(
        sink,
        {11: "Alpha"},
        channel_name="general",
        config=config_for(tmp_path),
        backend=MockBackend(texts=["retained text"]),
        recorded_at=RECORDED_AT,
    )

    discard_audio(sink)

    assert "retained text" in result.transcript_path.read_text(encoding="utf-8")


def test_a_recording_with_no_packets_writes_an_empty_transcript(tmp_path: Path) -> None:
    result = run_pipeline(
        TimestampedSink(),
        {},
        channel_name="general",
        config=config_for(tmp_path),
        backend=MockBackend(),
        recorded_at=RECORDED_AT,
    )

    assert result.packet_count == 0
    assert result.segment_count == 0
    assert result.transcript_path.read_text(encoding="utf-8") == ""


def test_result_reports_counts_and_duration(tmp_path: Path) -> None:
    packets = speech(0.0, 11) + speech(5.0, 22)

    result = run(tmp_path, packets, {11: "Alpha", 22: "Bravo"})

    assert result.segment_count == 2
    assert result.speakers == 2
    assert result.packet_count == 80
    assert result.duration == pytest.approx(5.78, abs=0.01)


def test_hostile_channel_names_still_produce_a_written_file(tmp_path: Path) -> None:
    result = run_pipeline(
        build_sink(speech(0.0, 11)),
        {11: "Alpha"},
        channel_name='voice: general <main> | "x"?*',
        config=config_for(tmp_path),
        backend=MockBackend(),
        recorded_at=RECORDED_AT,
    )

    assert result.transcript_path.exists()
    assert ":" not in result.transcript_path.name


def test_output_directory_is_created_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "does" / "not" / "exist"

    result = run(tmp_path=target, packets=speech(0.0, 11), names={11: "Alpha"})

    assert result.transcript_path.exists()


def test_unknown_speaker_is_labelled(tmp_path: Path) -> None:
    result = run(tmp_path, speech(0.0, 77), {}, backend=MockBackend(texts=["anonymous"]))

    assert "Unknown (77)" in result.transcript_path.read_text(encoding="utf-8")
