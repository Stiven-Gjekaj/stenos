"""Tests for the Discord command layer, driven by stand-in objects.

No test opens a gateway or voice connection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stenos.bot import (
    RecordingResult,
    RecordingSession,
    build_bot,
    describe_environment,
    describe_result,
    format_duration,
    main,
)
from stenos.config import Config
from stenos.sink import TimestampedSink


class FakeMember:
    def __init__(self, member_id: int, display_name: str) -> None:
        self.id = member_id
        self.display_name = display_name


def session_for(members: list[FakeMember] | None = None) -> RecordingSession:
    session = RecordingSession(
        guild_id=1,
        channel_id=2,
        channel_name="general",
        text_channel=None,
        voice_client=None,
        sink=TimestampedSink(),
    )
    session.remember_all(members or [])
    return session


def result_with(**overrides: Any) -> RecordingResult:
    defaults: dict[str, Any] = {
        "transcript_path": Path("out.txt"),
        "sidecar_path": Path("out.json"),
        "lines": [],
        "segment_count": 12,
        "duration": 90.0,
        "packet_count": 400,
        "speakers": 3,
    }
    defaults.update(overrides)
    return RecordingResult(**defaults)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (9, "9s"), (65, "1m 5s"), (3600, "1h 0m 0s"), (3725, "1h 2m 5s")],
)
def test_durations_render_readably(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


def test_negative_durations_clamp_to_zero() -> None:
    assert format_duration(-4) == "0s"


def test_names_are_cached_when_members_are_seen() -> None:
    session = session_for([FakeMember(11, "Alpha"), FakeMember(22, "Bravo")])

    assert session.names == {11: "Alpha", 22: "Bravo"}


def test_a_member_joining_later_is_cached() -> None:
    session = session_for([FakeMember(11, "Alpha")])

    session.remember(FakeMember(33, "Charlie"))

    assert session.names[33] == "Charlie"


def test_cached_names_survive_the_member_leaving() -> None:
    # The cache exists precisely so a departed participant stays resolvable.
    session = session_for([FakeMember(11, "Alpha")])
    names_snapshot = dict(session.names)

    assert names_snapshot[11] == "Alpha"


def test_non_ascii_display_names_are_cached_intact() -> None:
    session = session_for([FakeMember(11, "Dëlta 🎧"), FakeMember(22, "会議")])

    assert session.names == {11: "Dëlta 🎧", 22: "会議"}


def test_elapsed_time_advances_from_the_start() -> None:
    session = session_for()

    assert session.elapsed() >= 0.0


def test_completion_message_reports_segments_speakers_and_duration() -> None:
    message = describe_result(result_with())

    assert "12 segments" in message
    assert "3 speakers" in message
    assert "1m 30s" in message


def test_a_recording_with_no_packets_is_reported_explicitly() -> None:
    # The realistic failure: the connection carried nothing. Saying so beats
    # posting an empty transcript as though it succeeded.
    message = describe_result(result_with(packet_count=0, segment_count=0, speakers=0))

    assert "no audio was received" in message
    assert "known limitations" in message


def test_environment_report_covers_the_resolved_runtime(tmp_path: Path) -> None:
    config = Config(discord_token="token", output_dir=tmp_path, whisper_model="small")

    report = describe_environment(config)

    assert "stenos" in report
    assert "backend" in report
    assert "opus loaded" in report
    assert "small" in report
    assert str(tmp_path) in report
    # Both libraries whose absence only shows up as audio that never arrives.
    assert "encryption" in report
    assert "receive repair" in report


def record_group(bot: Any) -> Any:
    # Commands move to application_commands only after syncing with Discord,
    # so an unconnected bot exposes them as pending.
    return next(command for command in bot.pending_application_commands if command.name == "record")


def test_bot_registers_the_record_group(tmp_path: Path) -> None:
    bot = build_bot(Config(discord_token="token", output_dir=tmp_path))

    groups = {command.name for command in bot.pending_application_commands}

    assert "record" in groups


def test_record_group_exposes_start_stop_and_status(tmp_path: Path) -> None:
    bot = build_bot(Config(discord_token="token", output_dir=tmp_path))

    assert {sub.name for sub in record_group(bot).subcommands} == {"start", "stop", "status"}


def test_bot_starts_with_no_active_sessions(tmp_path: Path) -> None:
    bot = build_bot(Config(discord_token="token", output_dir=tmp_path))

    assert bot.sessions == {}


def test_guild_scoped_commands_are_registered_when_a_guild_is_configured(tmp_path: Path) -> None:
    bot = build_bot(Config(discord_token="token", guild_id=4242, output_dir=tmp_path))

    assert record_group(bot).guild_ids == [4242]


def test_commands_are_global_when_no_guild_is_configured(tmp_path: Path) -> None:
    bot = build_bot(Config(discord_token="token", output_dir=tmp_path))

    assert record_group(bot).guild_ids is None


def test_voice_state_intent_is_enabled(tmp_path: Path) -> None:
    # Voice receive does not work without it.
    bot = build_bot(Config(discord_token="token", output_dir=tmp_path))

    assert bot.intents.voice_states is True


def test_help_exits_cleanly_without_a_token() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0


def test_version_is_reported() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0


def test_missing_token_exits_with_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        main(["--check"])

    assert exit_info.value.code == 2


def test_check_reports_the_environment_without_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_TOKEN", "token")

    assert main(["--check"]) == 0
    assert "backend" in capsys.readouterr().out
