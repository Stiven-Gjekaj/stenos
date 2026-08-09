"""Tests for merge ordering, rendering, and file output."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stenos.transcribe import TranscribedSegment
from stenos.transcript import (
    TranscriptLine,
    build_sidecar,
    display_name,
    merge,
    render,
    write_sidecar,
    write_transcript,
)

RECORDED_AT = datetime(2026, 8, 1, 16, 14, 43, tzinfo=UTC)

NAMES = {11: "Alpha", 22: "Bravo"}


def result(start: float, user_id: int, text: str, duration: float = 1.0) -> TranscribedSegment:
    return TranscribedSegment(user_id=user_id, start=start, duration=duration, text=text)


def test_interleaved_speakers_sort_by_offset() -> None:
    results = [
        result(259.0, 22, "which part broke"),
        result(252.0, 11, "so about the asset pipeline"),
    ]

    lines = merge(results, NAMES)

    assert [(line.start, line.speaker) for line in lines] == [
        (252.0, "Alpha"),
        (259.0, "Bravo"),
    ]


def test_rendered_transcript_matches_the_documented_shape() -> None:
    results = [
        result(252.0, 11, "so about the asset pipeline"),
        result(259.0, 22, "which part broke"),
    ]

    body = render(merge(results, NAMES))

    assert body == (
        "[00:04:12] Alpha: so about the asset pipeline\n[00:04:19] Bravo: which part broke\n"
    )


def test_many_speakers_interleave_correctly() -> None:
    results = [
        result(30.0, 22, "third"),
        result(10.0, 11, "first"),
        result(40.0, 11, "fourth"),
        result(20.0, 33, "second"),
    ]

    lines = merge(results, {**NAMES, 33: "Charlie"})

    assert [line.text for line in lines] == ["first", "second", "third", "fourth"]


def test_simultaneous_starts_are_ordered_deterministically() -> None:
    results = [result(5.0, 22, "b"), result(5.0, 11, "a")]

    first = merge(results, NAMES)
    second = merge(list(reversed(results)), NAMES)

    assert [line.user_id for line in first] == [11, 22]
    assert first == second


def test_empty_results_are_dropped() -> None:
    results = [result(1.0, 11, "kept"), result(2.0, 22, ""), result(3.0, 11, "   ")]

    lines = merge(results, NAMES)

    assert [line.text for line in lines] == ["kept"]


def test_text_is_stripped() -> None:
    lines = merge([result(1.0, 11, "  padded  ")], NAMES)

    assert lines[0].text == "padded"


def test_unknown_speakers_are_labelled_with_their_identifier() -> None:
    lines = merge([result(1.0, 99, "who said this")], NAMES)

    assert lines[0].speaker == "Unknown (99)"


def test_names_captured_at_record_time_are_used() -> None:
    # A participant who disconnected before the call ended is still resolvable
    # because the cache was populated while recording.
    departed = {**NAMES, 44: "Departed"}

    lines = merge([result(1.0, 44, "left early")], departed)

    assert lines[0].speaker == "Departed"


def test_no_results_render_as_an_empty_transcript() -> None:
    assert render(merge([], NAMES)) == ""


def test_transcript_is_written_as_utf8(tmp_path: Path) -> None:
    names = {11: "Bravo", 22: "会議", 33: "Dëlta 🎧"}
    results = [
        result(1.0, 11, "ç'kemi, si po shkon"),
        result(2.0, 22, "こんにちは"),
        result(3.0, 33, "emoji in the name"),
    ]

    path = write_transcript(tmp_path / "out.txt", merge(results, names))

    assert path.read_text(encoding="utf-8").startswith("[00:00:01] Bravo: ç'kemi")


def test_written_bytes_round_trip_exactly(tmp_path: Path) -> None:
    names = {11: "Dëlta"}
    lines = merge([result(1.0, 11, "ë ç 会議 🎧")], names)

    path = write_transcript(tmp_path / "out.txt", lines)

    assert path.read_bytes() == render(lines).encode("utf-8")


def test_line_endings_are_line_feeds_only(tmp_path: Path) -> None:
    lines = merge([result(1.0, 11, "one"), result(2.0, 22, "two")], NAMES)

    path = write_transcript(tmp_path / "out.txt", lines)
    raw = path.read_bytes()

    assert b"\r\n" not in raw
    assert raw.count(b"\n") == 2


def test_output_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "out.txt"

    write_transcript(target, merge([result(1.0, 11, "text")], NAMES))

    assert target.exists()


def test_sidecar_records_every_segment_including_empty_ones() -> None:
    results = [result(1.0, 11, "spoken"), result(2.0, 22, "")]

    payload = build_sidecar(
        results,
        NAMES,
        channel="general",
        recorded_at=RECORDED_AT,
        duration=12.5,
        backend="mock",
        model="small",
    )

    assert len(payload["segments"]) == 2
    assert payload["segments"][1]["text"] == ""


def test_sidecar_carries_run_metadata() -> None:
    payload = build_sidecar(
        [result(1.0, 11, "text")],
        NAMES,
        channel="general",
        recorded_at=RECORDED_AT,
        duration=12.5,
        backend="mlx",
        model="small",
    )

    assert payload["version"] == 1
    assert payload["channel"] == "general"
    assert payload["recorded_at"] == "2026-08-01T16:14:43+00:00"
    assert payload["duration"] == 12.5
    assert payload["backend"] == "mlx"
    assert payload["model"] == "small"
    assert payload["speakers"] == {"11": "Alpha", "22": "Bravo"}


def test_sidecar_schema_version_and_required_fields() -> None:
    payload = build_sidecar(
        [result(1.0, 11, "text")],
        NAMES,
        channel="general",
        recorded_at=RECORDED_AT,
        duration=12.5,
        backend="mlx",
        model="small",
    )

    required_top_level = {
        "version",
        "channel",
        "recorded_at",
        "duration",
        "backend",
        "model",
        "speakers",
        "segments",
    }
    assert required_top_level.issubset(payload.keys())
    assert isinstance(payload["version"], int)
    assert payload["version"] == 1

    segment = payload["segments"][0]
    required_segment = {"user_id", "speaker", "start", "duration", "text"}
    assert required_segment.issubset(segment.keys())
    assert "suppressed" not in segment


def test_sidecar_segments_are_ordered_by_offset() -> None:
    payload = build_sidecar(
        [result(9.0, 22, "later"), result(1.0, 11, "earlier")],
        NAMES,
        channel="general",
        recorded_at=RECORDED_AT,
        duration=10.0,
        backend="mock",
        model="small",
    )

    assert [segment["start"] for segment in payload["segments"]] == [1.0, 9.0]


def test_sidecar_round_trips_non_ascii_without_escaping(tmp_path: Path) -> None:
    names = {11: "Dëlta 🎧"}
    payload = build_sidecar(
        [result(1.0, 11, "ç'kemi 会議")],
        names,
        channel="bisedë",
        recorded_at=RECORDED_AT,
        duration=2.0,
        backend="mock",
        model="small",
    )

    path = write_sidecar(tmp_path / "out.json", payload)
    raw = path.read_bytes()

    assert "ç'kemi 会議".encode() in raw
    assert b"\\u" not in raw
    assert b"\r\n" not in raw
    assert json.loads(path.read_text(encoding="utf-8")) == payload


@pytest.mark.parametrize(
    ("name", "rendered"),
    [
        ("Alpha", "Alpha"),
        ("Alpha: not really", "Alpha not really"),
        ("Alpha:Bravo", "Alpha Bravo"),
        ("Alpha : Bravo", "Alpha Bravo"),
        # Nothing but separators leaves nothing, and falling back to what was
        # given would put the separator straight back.
        ("::", "Unknown"),
        ("", "Unknown"),
        # Everything else survives: a transcript reads as the names people chose.
        ("Dëlta 🎧", "Dëlta 🎧"),
        ("Alpha (host)", "Alpha (host)"),
    ],
)
def test_a_rendered_name_cannot_be_mistaken_for_the_separator(name: str, rendered: str) -> None:
    assert display_name(name) == rendered


def test_a_name_carrying_the_separator_leaves_one_colon_in_the_line() -> None:
    # "[HH:MM:SS] Speaker: text" has a colon in the timestamp and one after the
    # speaker. A name carrying another leaves no way to tell where it ends.
    line = TranscriptLine(start=1.0, user_id=11, speaker="Alpha: not really", text="hello")

    body = render([line]).strip()

    assert body == "[00:00:01] Alpha not really: hello"
    assert body.count(":") == 3


def test_the_sidecar_keeps_the_name_as_it_was() -> None:
    # Only the transcript body has a format to protect. The sidecar is
    # structured, so it records what the participant was actually called.
    payload = build_sidecar(
        [result(1.0, 11, "text")],
        {11: "Alpha: not really"},
        channel="general",
        recorded_at=RECORDED_AT,
        duration=1.0,
        backend="mlx",
        model="small",
    )

    assert payload["speakers"]["11"] == "Alpha: not really"
    assert payload["segments"][0]["speaker"] == "Alpha: not really"


def test_the_sidecar_records_what_the_model_thought() -> None:
    # A line held back as invented can then be checked against the number that
    # held it back, rather than taken on trust.
    payload = build_sidecar(
        [
            TranscribedSegment(
                user_id=11,
                start=1.0,
                duration=2.0,
                text="Thank you.",
                suppressed="no-speech",
                no_speech=0.9512,
                logprob=-1.8034,
            )
        ],
        NAMES,
        channel="general",
        recorded_at=RECORDED_AT,
        duration=3.0,
        backend="faster-whisper",
        model="small",
    )

    segment = payload["segments"][0]
    assert segment["suppressed"] == "no-speech"
    assert segment["no_speech"] == 0.9512
    assert segment["logprob"] == -1.8034


def test_a_backend_that_cannot_say_leaves_the_numbers_out() -> None:
    # Absent rather than null, so a reader can tell a model that was unsure
    # from one that was never asked, and an older reader sees no change.
    payload = build_sidecar(
        [result(1.0, 11, "said something")],
        NAMES,
        channel="general",
        recorded_at=RECORDED_AT,
        duration=3.0,
        backend="mlx",
        model="small",
    )

    segment = payload["segments"][0]
    assert "no_speech" not in segment
    assert "logprob" not in segment
