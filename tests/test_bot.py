"""Tests for the Discord command layer, driven by stand-in objects.

No test opens a gateway or voice connection.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from stenos.bot import (
    RecordingResult,
    RecordingSession,
    build_bot,
    combine_progress,
    describe_environment,
    describe_result,
    discord_progress,
    format_duration,
    main,
)
from stenos.config import Config
from stenos.sink import TimestampedSink
from stenos.transcript import TranscriptLine


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
    # The lines match the speaker count rather than being empty. speakers is
    # derived from them in the real pipeline, so a result claiming three of
    # them and carrying none is a state that cannot occur, and a report about
    # an empty transcript reads it as one.
    defaults: dict[str, Any] = {
        "transcript_path": Path("out.txt"),
        "sidecar_path": Path("out.json"),
        "lines": [
            TranscriptLine(start=float(index), user_id=index, speaker=name, text="said something")
            for index, name in enumerate(("Alpha", "Bravo", "Charlie"), start=1)
        ],
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
    # Names the py-cord version and whether its sinks needed adapting, which
    # is why a recording can work here and nowhere else.
    assert "receive " in report
    assert "certificates" in report


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


def test_the_environment_report_names_every_limit_that_ends_a_recording() -> None:
    # --check is what someone runs before an unattended call. Three of the
    # settings that decide when a recording stops itself were missing from it,
    # so the report described a configuration it was not fully reporting.
    report = describe_environment(Config(discord_token="x", output_dir=Path("transcripts")))

    assert "maximum segment  30.0s" in report
    assert "buffer limit     1024MB" in report
    assert "disconnect grace 60s" in report


def test_a_limit_of_zero_is_reported_as_switched_off() -> None:
    report = describe_environment(
        Config(
            discord_token="x",
            output_dir=Path("transcripts"),
            max_buffer_mb=0.0,
            disconnect_grace=0.0,
        )
    )

    assert "buffer limit     none" in report
    assert "disconnect grace none" in report


def test_a_recording_whose_lines_were_all_held_back_says_so() -> None:
    # Audio arrived and was transcribed, and nothing survived into the
    # transcript: every segment was empty or was held back as invented. The
    # report used to read "Transcribed 5 segments from 0 speakers", which
    # describes that as a success and names a speaker count of nobody.
    result = RecordingResult(
        transcript_path=Path("t.txt"),
        sidecar_path=Path("t.json"),
        lines=[],
        segment_count=5,
        duration=12.0,
        packet_count=100,
        speakers=0,
    )

    message = describe_result(result)

    assert "0 speakers" not in message
    assert "none produced a usable line" in message
    assert "5 segments" in message


def test_the_environment_report_says_the_output_directory_is_writable(tmp_path: Path) -> None:
    report = describe_environment(Config(discord_token="x", output_dir=tmp_path / "calls"))

    assert f"output directory {tmp_path / 'calls'}" in report
    assert "CANNOT BE WRITTEN" not in report
    # Tried rather than inspected, and the probe does not survive the attempt.
    assert not list((tmp_path / "calls").iterdir())


def test_the_environment_report_says_when_it_cannot_write(tmp_path: Path) -> None:
    # A recording finds this out at the end of a call: the transcript is
    # written once, after transcription, so a directory that will not take it
    # costs the whole recording. --check is what somebody runs before leaving a
    # host unattended.
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory", encoding="utf-8")

    report = describe_environment(Config(discord_token="x", output_dir=blocker / "calls"))

    assert "CANNOT BE WRITTEN" in report
    assert "NotADirectoryError" in report


class FakeMessage:
    """Stand-in for the message ``discord_progress`` edits in place."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.edits: list[str] = []

    async def edit(self, *, content: str) -> None:
        self.content = content
        self.edits.append(content)


class FakeChannel:
    """Stand-in for a text channel. ``fail`` mimics one that cannot be posted to."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.message: FakeMessage | None = None
        self.fail = fail

    async def send(self, content: str) -> FakeMessage:
        if self.fail:
            raise RuntimeError("cannot post")
        self.sent.append(content)
        self.message = FakeMessage(content)
        return self.message


async def _settle() -> None:
    # discord_progress hands its report to the loop with
    # run_coroutine_threadsafe instead of awaiting it, so a scheduled report
    # needs a few real turns of the loop before it has actually run.
    for _ in range(5):
        await asyncio.sleep(0)


async def test_discord_progress_posts_once_and_then_edits() -> None:
    channel = FakeChannel()
    report = discord_progress(channel, asyncio.get_running_loop(), every=0.0)

    report(1, 10)
    await _settle()
    report(2, 10)
    await _settle()

    assert channel.sent == ["Transcribing... 1/10 segments (10%)"]
    assert channel.message is not None
    assert channel.message.edits == ["Transcribing... 2/10 segments (20%)"]


async def test_discord_progress_is_rate_limited() -> None:
    # No time passes between these two calls, so the second is dropped
    # exactly the way log_progress drops it, on the shared PROGRESS_INTERVAL.
    channel = FakeChannel()
    report = discord_progress(channel, asyncio.get_running_loop())

    report(1, 10)
    await _settle()
    report(2, 10)
    await _settle()

    assert len(channel.sent) == 1
    assert channel.message is not None
    assert channel.message.edits == []


async def test_discord_progress_always_reports_completion() -> None:
    # done == total bypasses the rate limit, the same as log_progress, so a
    # recording finishing between two intervals still shows a final message.
    channel = FakeChannel()
    report = discord_progress(channel, asyncio.get_running_loop())

    report(10, 10)
    await _settle()

    assert channel.sent == ["Transcribing... 10/10 segments (100%)"]


async def test_a_channel_that_cannot_be_posted_to_does_not_raise() -> None:
    # The transcript is the deliverable. A channel error must look like
    # nothing happened rather than like the transcription failed.
    channel = FakeChannel(fail=True)
    report = discord_progress(channel, asyncio.get_running_loop(), every=0.0)

    report(1, 10)
    await _settle()

    assert channel.sent == []


def test_combine_progress_runs_every_callback() -> None:
    calls: list[tuple[str, int, int]] = []

    def make(name: str):
        def report(done: int, total: int) -> None:
            calls.append((name, done, total))

        return report

    combined = combine_progress(make("a"), make("b"))
    combined(3, 10)

    assert calls == [("a", 3, 10), ("b", 3, 10)]
