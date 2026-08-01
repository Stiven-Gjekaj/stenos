"""Tests for the record command handlers, driven by stand-in Discord objects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stenos import bot as bot_module
from stenos.bot import build_bot
from stenos.config import Config
from stenos.transcribe import MockBackend


class FakeMember:
    def __init__(self, member_id: int, display_name: str, channel: Any = None) -> None:
        self.id = member_id
        self.display_name = display_name
        self.voice = FakeVoiceState(channel)


class FakeVoiceState:
    def __init__(self, channel: Any) -> None:
        self.channel = channel


class FakeVoiceClient:
    def __init__(self) -> None:
        self.recording = False
        self.disconnected = False
        self.sink: Any = None

    def start_recording(self, sink: Any, callback: Any) -> None:
        self.recording = True
        self.sink = sink

    def stop_recording(self) -> None:
        self.recording = False

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeVoiceChannel:
    def __init__(self, channel_id: int, name: str, members: list[FakeMember]) -> None:
        self.id = channel_id
        self.name = name
        self.members = members
        self.voice_client = FakeVoiceClient()

    async def connect(self) -> FakeVoiceClient:
        return self.voice_client


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def send(self, content: str, file: Any = None) -> None:
        self.sent.append((content, file))


class FakeGuild:
    def __init__(self, guild_id: int, filesize_limit: int = 8_388_608) -> None:
        self.id = guild_id
        self.filesize_limit = filesize_limit


class FakeContext:
    def __init__(self, guild_id: int | None, author: FakeMember) -> None:
        self.guild_id = guild_id
        self.author = author
        self.channel = object()
        self.guild = FakeGuild(guild_id) if guild_id is not None else None
        self.responses: list[tuple[str, bool]] = []
        self.deferred = False
        self.followup = FakeFollowup()

    async def respond(self, content: str, ephemeral: bool = False) -> None:
        self.responses.append((content, ephemeral))

    async def defer(self) -> None:
        self.deferred = True


def make_bot(tmp_path: Path) -> Any:
    return build_bot(Config(discord_token="token", output_dir=tmp_path, min_segment=0.0))


def command(bot: Any, name: str) -> Any:
    group = next(item for item in bot.pending_application_commands if item.name == "record")
    return next(sub for sub in group.subcommands if sub.name == name).callback


async def test_start_refuses_outside_a_guild(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    ctx = FakeContext(None, FakeMember(11, "Stiven"))

    await command(bot, "start")(ctx)

    assert "only inside a server" in ctx.responses[0][0]
    assert bot.sessions == {}


async def test_start_refuses_when_the_caller_is_not_in_a_voice_channel(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    ctx = FakeContext(1, FakeMember(11, "Stiven", channel=None))

    await command(bot, "start")(ctx)

    assert "Join a voice channel first." in ctx.responses[0][0]
    assert bot.sessions == {}


async def test_start_joins_and_announces_visibly(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Stiven", channel=channel)
    channel.members = [author]
    ctx = FakeContext(1, author)

    await command(bot, "start")(ctx)

    message, ephemeral = ctx.responses[0]
    assert "Recording general" in message
    # Announced publicly on purpose: consent and failure visibility.
    assert ephemeral is False
    assert channel.voice_client.recording is True


async def test_start_caches_names_of_members_already_present(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Stiven", channel=channel)
    channel.members = [author, FakeMember(22, "Enxhi"), FakeMember(33, "Zoë 🎧")]
    ctx = FakeContext(1, author)

    await command(bot, "start")(ctx)

    assert bot.sessions[1].names == {11: "Stiven", 22: "Enxhi", 33: "Zoë 🎧"}


async def test_start_refuses_a_second_concurrent_recording(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Stiven", channel=channel)
    channel.members = [author]

    await command(bot, "start")(FakeContext(1, author))
    second = FakeContext(1, author)
    await command(bot, "start")(second)

    assert "already in progress" in second.responses[0][0]
    assert len(bot.sessions) == 1


async def test_stop_refuses_when_nothing_is_recording(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    ctx = FakeContext(1, FakeMember(11, "Stiven"))

    await command(bot, "stop")(ctx)

    assert "No recording is in progress." in ctx.responses[0][0]


async def test_stop_transcribes_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bot_module,
        "load_backend",
        lambda *args, **kwargs: MockBackend(texts=["so about the asset pipeline"]),
    )
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Stiven", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    session = bot.sessions[1]
    for _ in range(40):
        session.sink.write(b"\x01\x00" * 960, 11)

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    message, attachment = stop_ctx.followup.sent[0]
    assert stop_ctx.deferred is True
    assert "Transcribed 1 segments" in message
    assert attachment is not None
    assert bot.sessions == {}
    assert channel.voice_client.disconnected is True


async def test_stop_reports_a_silent_recording_without_attaching_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_module, "load_backend", lambda *args, **kwargs: MockBackend())
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Stiven", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    message, attachment = stop_ctx.followup.sent[0]
    assert "no audio was received" in message
    assert attachment is None


async def test_status_refuses_when_nothing_is_recording(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    ctx = FakeContext(1, FakeMember(11, "Stiven"))

    await command(bot, "status")(ctx)

    assert "No recording is in progress." in ctx.responses[0][0]


async def test_status_reports_elapsed_time_and_speaker_count(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Stiven", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    bot.sessions[1].sink.write(b"\x00" * 3840, 11)
    bot.sessions[1].sink.write(b"\x00" * 3840, 22)

    ctx = FakeContext(1, author)
    await command(bot, "status")(ctx)

    message, ephemeral = ctx.responses[0]
    assert "Recording general" in message
    assert "2 participants" in message
    assert ephemeral is True


async def test_a_member_joining_mid_recording_is_cached(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Stiven", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    joiner = FakeMember(44, "Ana", channel=channel)
    joiner.guild = FakeGuild(1)  # type: ignore[attr-defined]
    await bot.on_voice_state_update(joiner, FakeVoiceState(None), FakeVoiceState(channel))

    assert bot.sessions[1].names[44] == "Ana"


async def test_voice_state_updates_for_other_channels_are_ignored(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    other = FakeVoiceChannel(99, "elsewhere", [])
    author = FakeMember(11, "Stiven", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    stranger = FakeMember(55, "Elsewhere", channel=other)
    stranger.guild = FakeGuild(1)  # type: ignore[attr-defined]
    await bot.on_voice_state_update(stranger, FakeVoiceState(None), FakeVoiceState(other))

    assert 55 not in bot.sessions[1].names
