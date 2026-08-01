"""Output encoding and line endings.

Windows defaults to the system code page rather than UTF-8, so any open()
without an explicit encoding either raises or silently mangles non-ASCII
output. It also translates line feeds to carriage return line feed pairs.
"""

from __future__ import annotations

import json
import locale
from datetime import UTC, datetime
from pathlib import Path

from stenos.transcribe import TranscribedSegment
from stenos.transcript import build_sidecar, merge, render, write_sidecar, write_transcript

# Albanian, CJK, emoji, and a right-to-left script.
SPEAKERS = {
    11: "Enxhi",
    22: "Zoë 🎧",
    33: "会議",
    44: "مستخدم",
}

UTTERANCES = [
    TranscribedSegment(user_id=11, start=0.0, duration=1.0, text="ç'kemi, si po shkon"),
    TranscribedSegment(user_id=22, start=1.0, duration=1.0, text="përshëndetje 🎧"),
    TranscribedSegment(user_id=33, start=2.0, duration=1.0, text="こんにちは、会議を始めます"),
    TranscribedSegment(user_id=44, start=3.0, duration=1.0, text="مرحبا"),
]


def test_transcript_round_trips_at_the_byte_level(tmp_path: Path) -> None:
    lines = merge(UTTERANCES, SPEAKERS)
    expected = render(lines)

    path = write_transcript(tmp_path / "transcript.txt", lines)

    assert path.read_bytes() == expected.encode("utf-8")
    assert path.read_text(encoding="utf-8") == expected


def test_every_script_survives_the_round_trip(tmp_path: Path) -> None:
    path = write_transcript(tmp_path / "transcript.txt", merge(UTTERANCES, SPEAKERS))
    body = path.read_text(encoding="utf-8")

    for fragment in ["ç'kemi", "përshëndetje", "🎧", "会議", "مرحبا", "Zoë"]:
        assert fragment in body


def test_no_carriage_returns_are_written(tmp_path: Path) -> None:
    # A log written on Windows must be byte identical to one written on macOS.
    path = write_transcript(tmp_path / "transcript.txt", merge(UTTERANCES, SPEAKERS))
    raw = path.read_bytes()

    assert b"\r" not in raw
    assert raw.count(b"\n") == len(UTTERANCES)


def test_output_is_independent_of_the_preferred_encoding(tmp_path: Path) -> None:
    # If the writer relied on the platform default, this assertion would fail
    # wherever that default is not UTF-8.
    path = write_transcript(tmp_path / "transcript.txt", merge(UTTERANCES, SPEAKERS))

    assert path.read_bytes().decode("utf-8").startswith("[00:00:00] Enxhi:")
    assert locale.getpreferredencoding(False) is not None


def test_sidecar_round_trips_without_escaping(tmp_path: Path) -> None:
    payload = build_sidecar(
        UTTERANCES,
        SPEAKERS,
        channel="bisedë 会議",
        recorded_at=datetime(2026, 8, 1, 16, 14, 43, tzinfo=UTC),
        duration=4.0,
        backend="mock",
        model="small",
    )

    path = write_sidecar(tmp_path / "transcript.json", payload)
    raw = path.read_bytes()

    assert b"\r" not in raw
    assert "会議".encode() in raw
    assert json.loads(path.read_text(encoding="utf-8")) == payload


def test_reading_without_an_explicit_encoding_would_be_the_bug(tmp_path: Path) -> None:
    # Demonstrates that the bytes on disk are UTF-8 regardless of platform.
    path = write_transcript(tmp_path / "transcript.txt", merge(UTTERANCES, SPEAKERS))

    decoded = path.read_bytes().decode("utf-8")

    assert "会議" in decoded
