"""Tests for timestamp rendering and portable output paths."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stenos.transcript import (
    format_timestamp,
    sanitize_filename,
    transcript_paths,
    transcript_stem,
)

RECORDED_AT = datetime(2026, 8, 1, 16, 14, 43, tzinfo=UTC)

WINDOWS_ILLEGAL = '<>:"/\\|?*'


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "[00:00:00]"),
        (9, "[00:00:09]"),
        (60, "[00:01:00]"),
        (252, "[00:04:12]"),
        (259, "[00:04:19]"),
        (3600, "[01:00:00]"),
        (3661, "[01:01:01]"),
    ],
)
def test_offsets_render_as_clock_times(seconds: float, expected: str) -> None:
    assert format_timestamp(seconds) == expected


def test_fractional_seconds_are_truncated() -> None:
    assert format_timestamp(252.9) == "[00:04:12]"


def test_negative_offsets_clamp_to_zero() -> None:
    assert format_timestamp(-5.0) == "[00:00:00]"


def test_hours_are_not_wrapped_at_a_day() -> None:
    assert format_timestamp(90_000) == "[25:00:00]"


def test_illegal_windows_characters_are_replaced() -> None:
    cleaned = sanitize_filename("voice: general <main>")

    assert cleaned == "voice-general-main"
    assert not any(character in cleaned for character in WINDOWS_ILLEGAL)


@pytest.mark.parametrize("character", list(WINDOWS_ILLEGAL))
def test_every_illegal_character_is_removed(character: str) -> None:
    cleaned = sanitize_filename(f"team{character}chat")

    assert character not in cleaned


def test_path_separators_cannot_escape_the_output_directory() -> None:
    cleaned = sanitize_filename("../../etc/passwd")

    assert "/" not in cleaned
    assert "\\" not in cleaned
    assert not cleaned.startswith(".")


def test_control_characters_are_removed() -> None:
    assert "\x07" not in sanitize_filename("bell\x07channel")


def test_whitespace_becomes_a_single_separator() -> None:
    assert sanitize_filename("general   voice  chat") == "general-voice-chat"


def test_trailing_dots_and_spaces_are_stripped() -> None:
    # Windows silently drops these, which would make two channels collide.
    assert sanitize_filename("standup...  ") == "standup"


@pytest.mark.parametrize("reserved", ["CON", "nul", "COM1", "LPT9", "aux"])
def test_reserved_device_names_are_escaped(reserved: str) -> None:
    cleaned = sanitize_filename(reserved)

    assert cleaned.split(".")[0].upper() not in {"CON", "NUL", "COM1", "LPT9", "AUX"}


def test_reserved_name_with_an_extension_is_escaped() -> None:
    assert sanitize_filename("NUL.txt").split(".")[0].upper() != "NUL"


def test_non_ascii_channel_names_are_preserved() -> None:
    # Every supported platform accepts UTF-8 file names.
    assert sanitize_filename("bisedë-ekipi") == "bisedë-ekipi"
    assert sanitize_filename("会議") == "会議"


def test_empty_or_fully_stripped_names_fall_back() -> None:
    assert sanitize_filename("") == "channel"
    assert sanitize_filename("...") == "channel"
    assert sanitize_filename("///") == "channel"


def test_long_names_are_truncated() -> None:
    cleaned = sanitize_filename("x" * 500)

    assert len(cleaned) <= 80


def test_truncation_does_not_leave_a_trailing_separator() -> None:
    cleaned = sanitize_filename(("word " * 40).strip())

    assert not cleaned.endswith("-")


def test_stem_uses_the_iso_basic_timestamp() -> None:
    stem = transcript_stem("general", RECORDED_AT)

    assert stem == "stenos-general-20260801T161443Z"
    assert ":" not in stem


def test_paths_share_a_stem_and_differ_by_suffix(tmp_path: Path) -> None:
    transcript, sidecar = transcript_paths(tmp_path, "general", RECORDED_AT)

    assert transcript.name == "stenos-general-20260801T161443Z.txt"
    assert sidecar.name == "stenos-general-20260801T161443Z.json"
    assert transcript.parent == tmp_path


def test_paths_are_built_with_pathlib(tmp_path: Path) -> None:
    transcript, sidecar = transcript_paths(tmp_path, "voice: general <main>", RECORDED_AT)

    assert isinstance(transcript, Path)
    assert isinstance(sidecar, Path)
    assert transcript.parent == tmp_path


def test_a_hostile_channel_name_produces_a_writable_path(tmp_path: Path) -> None:
    transcript, _sidecar = transcript_paths(tmp_path, 'voice: general <main> | "x"?*', RECORDED_AT)

    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("line", encoding="utf-8")

    assert transcript.read_text(encoding="utf-8") == "line"
