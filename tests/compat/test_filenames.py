"""Filename sanitisation and path construction.

Transcripts are named from the channel name and a timestamp, both of which are
hostile to Windows. The sanitiser runs on every platform so a file produced on
Linux can be copied to Windows unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from stenos.transcript import sanitize_filename, transcript_paths

RECORDED_AT = datetime(2026, 8, 1, 16, 14, 43, tzinfo=UTC)

WINDOWS_ILLEGAL = '<>:"/\\|?*'

HOSTILE_CHANNEL_NAMES = [
    "voice: general <main>",
    'quotes "inside" the name',
    "pipes | and ? marks *",
    "back\\slash/forward",
    "trailing dots...",
    "trailing spaces   ",
    "CON",
    "NUL.txt",
    "COM1",
    "LPT9",
    "bisedë ekipi",
    "会議室",
    "control\x07character",
    "",
]


@pytest.mark.parametrize("channel", HOSTILE_CHANNEL_NAMES)
def test_hostile_channel_names_produce_writable_paths(channel: str, tmp_path: Path) -> None:
    transcript, sidecar = transcript_paths(tmp_path, channel, RECORDED_AT)

    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("[00:00:00] Speaker: text\n", encoding="utf-8", newline="\n")
    sidecar.write_text(json.dumps({"channel": channel}), encoding="utf-8", newline="\n")

    assert transcript.is_file()
    assert sidecar.is_file()


@pytest.mark.parametrize("channel", HOSTILE_CHANNEL_NAMES)
def test_produced_names_contain_no_illegal_characters(channel: str, tmp_path: Path) -> None:
    transcript, sidecar = transcript_paths(tmp_path, channel, RECORDED_AT)

    for name in (transcript.name, sidecar.name):
        assert not any(character in name for character in WINDOWS_ILLEGAL)


@pytest.mark.parametrize("channel", HOSTILE_CHANNEL_NAMES)
def test_names_have_no_trailing_dot_or_space(channel: str, tmp_path: Path) -> None:
    transcript, _sidecar = transcript_paths(tmp_path, channel, RECORDED_AT)

    assert not transcript.stem.endswith((".", " "))


def test_the_timestamp_carries_no_colons(tmp_path: Path) -> None:
    # The ISO extended format would embed colons, which Windows rejects.
    transcript, _sidecar = transcript_paths(tmp_path, "general", RECORDED_AT)

    assert ":" not in transcript.name
    assert "20260801T161443Z" in transcript.name


def test_sanitisation_is_applied_uniformly_not_only_on_windows() -> None:
    # The same input yields the same output regardless of host, so a
    # transcript recorded on Linux remains portable.
    assert sanitize_filename("voice: general <main>") == "voice-general-main"


def test_paths_never_escape_the_output_directory(tmp_path: Path) -> None:
    transcript, _sidecar = transcript_paths(tmp_path, "../../../etc/passwd", RECORDED_AT)

    assert transcript.parent == tmp_path
    assert transcript.resolve().parent == tmp_path.resolve()


def test_paths_are_pathlib_objects_not_joined_strings(tmp_path: Path) -> None:
    transcript, sidecar = transcript_paths(tmp_path, "general", RECORDED_AT)

    assert isinstance(transcript, Path)
    assert isinstance(sidecar, Path)
    # A string-joined separator would appear literally in the final component.
    assert "/" not in transcript.name
    assert "\\" not in transcript.name


def test_source_contains_no_string_joined_separators() -> None:
    import stenos.bot
    import stenos.transcript

    for module in (stenos.transcript, stenos.bot):
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        assert '+ "/"' not in source
        assert "+ '/'" not in source
        assert "os.path.join" not in source
