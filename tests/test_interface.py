"""Tests for what the interface shows, with nothing drawn.

The screens are a rendering of these values, so this is where the decisions
are: which recordings exist, what a finished one says about itself, and what a
crash left behind. None of it needs a display, which is the point of splitting
it out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stenos.interface import Library, library, live_recordings
from stenos.spill import SpillWriter

RECORDED_AT = datetime(2026, 8, 9, 15, 0, 0, tzinfo=UTC)


def sidecar(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "channel": "general",
        "recorded_at": RECORDED_AT.isoformat(),
        "duration": 90.0,
        "backend": "mock",
        "model": "small",
        "speakers": {"11": "Alpha", "22": "Bravo"},
        "segments": [{"user_id": 11, "start": 0.0, "duration": 2.0, "text": "hello"}],
    }
    payload.update(overrides)
    return payload


def write_call(directory: Path, stem: str, **overrides: Any) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    transcript = directory / f"{stem}.txt"
    transcript.write_text("[00:00:00] Alpha: hello\n", encoding="utf-8")
    (directory / f"{stem}.json").write_text(json.dumps(sidecar(**overrides)), encoding="utf-8")
    return transcript


def test_an_empty_output_directory_shows_nothing(tmp_path: Path) -> None:
    assert library(tmp_path) == Library()


def test_a_directory_that_does_not_exist_is_not_an_error(tmp_path: Path) -> None:
    # The interface opens before the first recording has written anything.
    assert library(tmp_path / "absent") == Library()


def test_a_finished_call_reads_back_from_its_sidecar(tmp_path: Path) -> None:
    write_call(tmp_path, "stenos-general-20260809T150000Z")

    found = library(tmp_path)

    assert len(found.transcripts) == 1
    item = found.transcripts[0]
    assert item.channel == "general"
    assert item.recorded_at == RECORDED_AT
    assert item.speakers == ("Alpha", "Bravo")
    assert item.segments == 1
    assert item.title == "general, 2026-08-09 15:00"
    assert item.summary == "1m 30s, Alpha, Bravo"


def test_the_newest_call_comes_first(tmp_path: Path) -> None:
    # The one somebody has just made is the one they are looking for.
    write_call(tmp_path, "older", recorded_at=datetime(2026, 8, 1, tzinfo=UTC).isoformat())
    write_call(tmp_path, "newer", recorded_at=datetime(2026, 8, 9, tzinfo=UTC).isoformat())

    order = [item.transcript.stem for item in library(tmp_path).transcripts]

    assert order == ["newer", "older"]


def test_a_transcript_with_no_sidecar_is_still_listed(tmp_path: Path) -> None:
    # The sidecar carries the speakers and the timings, so losing it costs
    # those rather than the transcript.
    (tmp_path / "orphan.txt").write_text("[00:00:00] Alpha: hello\n", encoding="utf-8")

    found = library(tmp_path)

    assert len(found.transcripts) == 1
    assert found.transcripts[0].sidecar is None
    assert found.transcripts[0].channel == "orphan"
    assert found.transcripts[0].speakers == ()


def test_a_sidecar_that_will_not_parse_is_treated_as_absent(tmp_path: Path) -> None:
    # A library that refuses to open because one file is damaged is worse than
    # a row with less on it.
    (tmp_path / "damaged.txt").write_text("[00:00:00] Alpha: hello\n", encoding="utf-8")
    (tmp_path / "damaged.json").write_text("{not json", encoding="utf-8")

    found = library(tmp_path)

    assert len(found.transcripts) == 1
    assert found.transcripts[0].sidecar is None


def test_a_sidecar_with_an_unreadable_timestamp_still_lists(tmp_path: Path) -> None:
    write_call(tmp_path, "call", recorded_at="the other day")

    item = library(tmp_path).transcripts[0]

    assert item.recorded_at is None
    assert item.title == "general, unknown time"


def test_a_recording_a_crash_left_behind_is_offered(tmp_path: Path) -> None:
    directory = tmp_path / "stenos-general-20260809T150000Z.partial"
    store = SpillWriter(directory, channel="general", started_at=RECORDED_AT, sample_rate=16000)
    store.remember(11, "Alpha")
    store.append(11, 0.0, b"\x00\x04" * 800, 16000)
    store.close()

    found = library(tmp_path)

    assert len(found.unfinished) == 1
    item = found.unfinished[0]
    assert item.channel == "general"
    assert item.started_at == RECORDED_AT
    assert item.segments == 1
    assert "never transcribed" in item.summary


def test_a_partial_directory_that_cannot_be_read_is_still_offered(tmp_path: Path) -> None:
    # Something is there and somebody should be told, even if this version
    # cannot say what it holds.
    directory = tmp_path / "broken.partial"
    directory.mkdir()
    (directory / "manifest.jsonl").write_text("not json at all\n", encoding="utf-8")

    found = library(tmp_path)

    assert len(found.unfinished) == 1
    assert found.unfinished[0].channel == "broken"
    assert found.unfinished[0].segments == 0


class FakeSink:
    def __init__(self) -> None:
        self.buffered_bytes = 4_500_000
        self.total_bytes = 9_000_000
        self.spilling = True
        self.unattributed_packets = 3
        self.user_ids = frozenset({11, 22})


class FakeSession:
    def __init__(self) -> None:
        self.guild_id = 1
        self.channel_name = "general"
        self.sink = FakeSink()

    def elapsed(self) -> float:
        return 95.0


class FakeBot:
    def __init__(self, *, connected: bool = True) -> None:
        self.sessions = {1: FakeSession()}
        self._connected = connected

    def connection_lost(self, session: Any) -> bool:
        return not self._connected


def test_a_live_recording_reports_what_the_screen_needs() -> None:
    live = live_recordings(FakeBot())[0]

    assert live.channel == "general"
    assert live.speakers == 2
    assert live.held == "4.5 MB"
    assert live.running_for == "1m 35s"
    assert live.spilling is True
    assert live.unattributed == 3
    assert live.summary == "general: recording, 1m 35s, 2 speakers"


def test_a_live_recording_says_when_the_connection_is_down() -> None:
    # The recording continues through an outage now, so the screen has to
    # distinguish one that is receiving audio from one that is waiting.
    live = live_recordings(FakeBot(connected=False))[0]

    assert live.connected is False
    assert "connection down" in live.summary


def test_no_recording_shows_no_rows() -> None:
    bot = FakeBot()
    bot.sessions = {}

    assert live_recordings(bot) == []
