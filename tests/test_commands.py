"""Tests for the record command handlers, driven by stand-in Discord objects."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from discord.client import _cancel_tasks as cancel_tasks

from stenos import bot as bot_module
from stenos import upstream
from stenos.bot import build_bot
from stenos.config import Config
from stenos.spill import partial_recordings
from stenos.transcribe import BackendUnavailableError, MockBackend


class FakeMember:
    def __init__(self, member_id: int, display_name: str, channel: Any = None) -> None:
        self.id = member_id
        self.display_name = display_name
        self.voice = FakeVoiceState(channel)


class FakeVoiceState:
    def __init__(self, channel: Any) -> None:
        self.channel = channel


def dave_connection(*, ready: bool = True, version: int = 1) -> SimpleNamespace:
    """A stand in for the connection state a voice client exposes."""
    return SimpleNamespace(
        dave_protocol_version=version,
        dave_session=SimpleNamespace(ready=ready, status="active" if ready else "inactive"),
    )


class FakeVoiceClient:
    def __init__(self, connection: Any = None) -> None:
        self.recording = False
        self.disconnected = False
        self.sink: Any = None
        self.connected = True
        # An instance attribute rather than a method, so a test can delete it
        # and get a client on which the probe is genuinely missing.
        self.is_connected = lambda: self.connected
        # Left unset rather than set to None when absent, so the attribute is
        # genuinely missing the way it would be on a py-cord that renamed it.
        if connection is not None:
            self._connection = connection

    def start_recording(self, sink: Any, callback: Any) -> None:
        self.recording = True
        self.sink = sink

    def stop_recording(self) -> None:
        self.recording = False

    async def disconnect(self) -> None:
        self.disconnected = True


class FakeVoiceChannel:
    def __init__(
        self,
        channel_id: int,
        name: str,
        members: list[FakeMember],
        connection: Any = None,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.members = members
        self.voice_client = FakeVoiceClient(connection)

    async def connect(self) -> FakeVoiceClient:
        return self.voice_client


#: One 20 ms frame carrying signal. Transcription suppresses text over audio
#: with nothing in it, so a fixture standing in for speech has to be loud
#: enough to be speech.
SPEECH = b"\x00\x04" * 960


def feed(session: Any, *, packets: int = 40, payload: bytes = SPEECH) -> None:
    """Write audio into a session's sink so it reads as somebody speaking."""
    for _ in range(packets):
        session.sink.write(payload, 11)


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def send(self, content: str, file: Any = None) -> None:
        self.sent.append((content, file))


class FakeTextChannel:
    """The channel a session posts to when it ends without being asked to."""

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
        self.channel = FakeTextChannel()
        self.guild = FakeGuild(guild_id) if guild_id is not None else None
        self.responses: list[tuple[str, bool]] = []
        self.deferred = False
        self.deferred_ephemeral: bool | None = None
        self.followup = FakeFollowup()

    async def respond(self, content: str, ephemeral: bool = False) -> None:
        self.responses.append((content, ephemeral))

    async def defer(self, ephemeral: bool = False) -> None:
        self.deferred = True
        self.deferred_ephemeral = ephemeral


def make_bot(tmp_path: Path, **overrides: Any) -> Any:
    settings: dict[str, Any] = {
        "discord_token": "token",
        "output_dir": tmp_path,
        "min_segment": 0.0,
    }
    settings.update(overrides)
    return build_bot(Config(**settings))


def command(bot: Any, name: str) -> Any:
    group = next(item for item in bot.pending_application_commands if item.name == "record")
    return next(sub for sub in group.subcommands if sub.name == name).callback


async def test_start_refuses_outside_a_guild(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    ctx = FakeContext(None, FakeMember(11, "Alpha"))

    await command(bot, "start")(ctx)

    assert "only inside a server" in ctx.responses[0][0]
    assert bot.sessions == {}


async def test_start_refuses_when_the_caller_is_not_in_a_voice_channel(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    ctx = FakeContext(1, FakeMember(11, "Alpha", channel=None))

    await command(bot, "start")(ctx)

    assert "Join a voice channel first." in ctx.responses[0][0]
    assert bot.sessions == {}


async def test_start_joins_and_announces_visibly(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    ctx = FakeContext(1, author)

    await command(bot, "start")(ctx)

    message, _file = ctx.followup.sent[0]
    assert "Recording general" in message
    # Deferred before connecting, because joining voice outlasts the three
    # seconds an interaction token is good for.
    assert ctx.deferred is True
    # Announced publicly on purpose: consent and failure visibility.
    assert ctx.deferred_ephemeral is False
    assert channel.voice_client.recording is True


async def test_start_caches_names_of_members_already_present(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author, FakeMember(22, "Bravo"), FakeMember(33, "Dëlta 🎧")]
    ctx = FakeContext(1, author)

    await command(bot, "start")(ctx)

    assert bot.sessions[1].names == {11: "Alpha", 22: "Bravo", 33: "Dëlta 🎧"}


async def test_start_refuses_a_second_concurrent_recording(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]

    await command(bot, "start")(FakeContext(1, author))
    second = FakeContext(1, author)
    await command(bot, "start")(second)

    assert "already in progress" in second.responses[0][0]
    assert len(bot.sessions) == 1


async def test_stop_refuses_when_nothing_is_recording(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    ctx = FakeContext(1, FakeMember(11, "Alpha"))

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
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    session = bot.sessions[1]
    for _ in range(40):
        session.sink.write(SPEECH, 11)

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    message, attachment = stop_ctx.followup.sent[0]
    assert stop_ctx.deferred is True
    assert "Transcribed 1 segments" in message
    assert attachment is not None
    assert bot.sessions == {}
    assert channel.voice_client.disconnected is True


async def test_stop_reports_a_recording_that_captured_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unused(*args: object, **kwargs: object) -> None:
        raise AssertionError("the backend must not be loaded for an empty recording")

    monkeypatch.setattr(bot_module, "load_backend", unused)
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    message, attachment = stop_ctx.followup.sent[0]
    assert "No audio was received" in message
    assert attachment is None
    assert bot.sessions == {}


async def test_stop_names_the_encryption_state_when_nothing_was_captured(
    tmp_path: Path,
) -> None:
    # A session that never finished its handshake discards every packet, and
    # that is the answer worth reporting rather than a guess about who spoke.
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [], connection=dave_connection(ready=False))
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    message, _attachment = stop_ctx.followup.sent[0]
    assert "session inactive" in message
    assert "nobody spoke" not in message


async def test_stop_reports_a_recording_of_pure_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Packets that cannot be decrypted are replaced with silence, which yields
    # a recording of the right length holding nothing.
    def unused(*args: object, **kwargs: object) -> None:
        raise AssertionError("the backend must not be loaded for a silent recording")

    monkeypatch.setattr(bot_module, "load_backend", unused)
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    feed(bot.sessions[1], payload=b"\x00" * 1920)

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    message, attachment = stop_ctx.followup.sent[0]
    assert "every sample was silence" in message
    assert attachment is None


async def test_status_refuses_when_nothing_is_recording(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    ctx = FakeContext(1, FakeMember(11, "Alpha"))

    await command(bot, "status")(ctx)

    assert "No recording is in progress." in ctx.responses[0][0]


async def test_status_reports_elapsed_time_and_speaker_count(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
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
    # Packets are arriving, so the encryption state is not worth raising.
    assert "No audio has arrived" not in message


async def test_status_warns_while_no_audio_has_arrived(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [], connection=dave_connection(ready=False))
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    ctx = FakeContext(1, author)
    await command(bot, "status")(ctx)

    # Reported during the call rather than discovered afterwards, when the
    # audio is already gone.
    assert "No audio has arrived yet" in ctx.responses[0][0]
    assert "session inactive" in ctx.responses[0][0]


async def test_status_stays_quiet_once_a_session_is_ready(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [], connection=dave_connection(ready=True))
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    ctx = FakeContext(1, author)
    await command(bot, "status")(ctx)

    assert "No audio has arrived" not in ctx.responses[0][0]


async def test_a_member_joining_mid_recording_is_cached(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    joiner = FakeMember(44, "Charlie", channel=channel)
    joiner.guild = FakeGuild(1)  # type: ignore[attr-defined]
    await bot.on_voice_state_update(joiner, FakeVoiceState(None), FakeVoiceState(channel))

    assert bot.sessions[1].names[44] == "Charlie"


async def test_voice_state_updates_for_other_channels_are_ignored(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    other = FakeVoiceChannel(99, "elsewhere", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    stranger = FakeMember(55, "Elsewhere", channel=other)
    stranger.guild = FakeGuild(1)  # type: ignore[attr-defined]
    await bot.on_voice_state_update(stranger, FakeVoiceState(None), FakeVoiceState(other))

    assert 55 not in bot.sessions[1].names


# Losing the voice connection. Nothing used to notice, so the audio sat in
# memory with the session still registered and the call was lost unless
# somebody thought to run the stop command.

#: The bot's own user id, for the voice state updates that are about itself.
SELF_ID = 999


async def recording_with_self(tmp_path: Path, **overrides: Any) -> tuple[Any, Any, Any]:
    """A started recording, on a bot that knows which member it is."""
    bot = make_bot(tmp_path, **overrides)
    # bot.user reads through to the connection state, which is empty until a
    # real login. A bot that does not know itself cannot tell its own voice
    # state update from a participant's.
    bot._connection.user = SimpleNamespace(id=SELF_ID)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    return bot, channel, bot.sessions[1]


def self_state(channel: Any) -> tuple[Any, Any]:
    """The bot's own member, and the voice state naming where it now is."""
    member = FakeMember(SELF_ID, "stenos")
    member.guild = FakeGuild(1)  # type: ignore[attr-defined]
    return member, FakeVoiceState(channel)


async def test_being_removed_starts_the_clock_rather_than_ending_at_once(
    tmp_path: Path,
) -> None:
    # py-cord's reconnect asks Discord to remove the bot from the channel
    # before rejoining it, so this event arrives during a recovery as well as
    # during a kick. Ending here would cut every recovery short.
    bot, channel, session = await recording_with_self(tmp_path)

    member, gone = self_state(None)
    await bot.on_voice_state_update(member, FakeVoiceState(channel), gone)

    assert 1 in bot.sessions
    assert session.disconnected_since is not None


async def test_being_removed_for_good_ends_the_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The kick, told apart from the reconnect by the connection not coming
    # back. What was captured is still written out, which is the whole point.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot, channel, session = await recording_with_self(tmp_path, disconnect_grace=0.01)
    feed(session)

    member, gone = self_state(None)
    await bot.on_voice_state_update(member, FakeVoiceState(channel), gone)
    channel.voice_client.connected = False
    time.sleep(0.02)
    await bot.enforce_connection()

    assert 1 not in bot.sessions
    (message, _attachment) = session.text_channel.sent[0]
    assert "was lost and did not come back" in message
    assert "Transcribed" in message


async def test_a_reconnect_leaves_the_recording_running(tmp_path: Path) -> None:
    # The other half of the same pair. Removed, then back on the same channel,
    # which is what py-cord's own reconnect looks like from here.
    bot, channel, session = await recording_with_self(tmp_path)

    member, gone = self_state(None)
    await bot.on_voice_state_update(member, FakeVoiceState(channel), gone)
    # Back on the same channel. The clock is the watchdog's to clear, since a
    # connection reading as up is the evidence, not the event.
    await bot.on_voice_state_update(member, gone, FakeVoiceState(channel))
    await bot.enforce_connection()

    assert 1 in bot.sessions
    assert session.disconnected_since is None


async def test_being_moved_to_another_channel_ends_the_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Carrying on would file this call's speech under the other channel's name.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot, channel, session = await recording_with_self(tmp_path)
    feed(session)

    member, moved = self_state(FakeVoiceChannel(77, "elsewhere", []))
    await bot.on_voice_state_update(member, FakeVoiceState(channel), moved)

    assert 1 not in bot.sessions
    assert "moved out of general" in session.text_channel.sent[0][0]


async def test_the_bot_muting_itself_does_not_end_the_recording(tmp_path: Path) -> None:
    # Every mute, deafen and server change arrives as a voice state update for
    # the same channel. Only leaving it means anything.
    bot, channel, _session = await recording_with_self(tmp_path)

    member, same = self_state(channel)
    await bot.on_voice_state_update(member, FakeVoiceState(channel), same)

    assert 1 in bot.sessions


async def test_a_participant_leaving_does_not_end_the_recording(tmp_path: Path) -> None:
    # The check is on identity, not on the channel being empty. One person
    # leaving a call is not the call ending.
    bot, channel, _session = await recording_with_self(tmp_path)

    leaver = FakeMember(11, "Alpha")
    leaver.guild = FakeGuild(1)  # type: ignore[attr-defined]
    await bot.on_voice_state_update(leaver, FakeVoiceState(channel), FakeVoiceState(None))

    assert 1 in bot.sessions


async def test_a_move_is_only_acted_on_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Discord repeats voice state updates, and the buffer check runs on its own
    # loop. Two arrivals must not transcribe the same recording twice.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend())
    bot, channel, session = await recording_with_self(tmp_path)
    feed(session)

    member, moved = self_state(FakeVoiceChannel(77, "elsewhere", []))
    await bot.on_voice_state_update(member, FakeVoiceState(channel), moved)
    await bot.on_voice_state_update(member, FakeVoiceState(channel), moved)

    assert len(session.text_channel.sent) == 1


# The other half of losing a connection. A host that loses its network loses
# the gateway with it, so no event arrives to say so and the connection's own
# state is the only account left.


async def test_a_connected_recording_is_left_alone(tmp_path: Path) -> None:
    bot, channel, _session = await recording_with_self(tmp_path)
    channel.voice_client.connected = True

    await bot.enforce_connection()

    assert 1 in bot.sessions


async def test_a_dropped_connection_is_given_time_to_come_back(tmp_path: Path) -> None:
    # A reconnect reads as disconnected for the whole of the attempt. Ending on
    # the first check that finds it missing would cut every recovery short.
    bot, channel, session = await recording_with_self(tmp_path)
    channel.voice_client.connected = False

    await bot.enforce_connection()

    assert 1 in bot.sessions
    assert session.disconnected_since is not None


async def test_a_connection_that_comes_back_is_forgotten(tmp_path: Path) -> None:
    # Otherwise the next outage inherits the first one's clock and ends the
    # recording on its first check.
    bot, channel, session = await recording_with_self(tmp_path)
    channel.voice_client.connected = False
    await bot.enforce_connection()

    channel.voice_client.connected = True
    await bot.enforce_connection()

    assert session.disconnected_since is None


async def test_a_connection_that_stays_down_ends_the_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot, channel, session = await recording_with_self(tmp_path, disconnect_grace=0.01)
    feed(session)
    channel.voice_client.connected = False

    await bot.enforce_connection()
    # Past the grace, which is what the second check measures rather than the
    # first. Compared against perf_counter, so it has to be real elapsed time.
    time.sleep(0.02)
    await bot.enforce_connection()

    assert 1 not in bot.sessions
    (message, _attachment) = session.text_channel.sent[0]
    assert "was lost and did not come back" in message
    assert "Transcribed" in message
    assert "DISCONNECT_GRACE" in message


async def test_a_zero_grace_waits_forever(tmp_path: Path) -> None:
    # For a host where a recording surviving a long outage matters more than
    # one that is quietly receiving nothing.
    bot, channel, _session = await recording_with_self(tmp_path, disconnect_grace=0.0)
    channel.voice_client.connected = False

    await bot.enforce_connection()
    await bot.enforce_connection()

    assert 1 in bot.sessions


async def test_a_voice_client_without_the_probe_is_treated_as_connected(
    tmp_path: Path,
) -> None:
    # is_connected is py-cord's. A build that renames it would otherwise read
    # as every recording having dropped, and end all of them on the next check.
    bot, channel, _session = await recording_with_self(tmp_path, disconnect_grace=0.01)
    del channel.voice_client.is_connected

    await bot.enforce_connection()
    time.sleep(0.02)
    await bot.enforce_connection()

    assert 1 in bot.sessions


# Transcription progress. The longest part of a recording, and the only part
# with nothing to show that it is working.


def test_progress_reports_the_first_and_last_segment(caplog: pytest.LogCaptureFixture) -> None:
    # The first says the work started, the last says it finished rather than
    # stalled. Everything between is on a timer, so an interval nothing can
    # reach leaves exactly those two.
    report = bot_module.log_progress(every=3600.0)

    with caplog.at_level(logging.INFO, logger="stenos"):
        for done in range(1, 6):
            report(done, 5)

    assert caplog.messages == [
        "Transcribed 1 of 5 segments (20%).",
        "Transcribed 5 of 5 segments (100%).",
    ]


def test_progress_reports_every_segment_when_nothing_is_held_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An hour of conversation is hundreds of segments, which is why the timer
    # exists. This is what it holds back.
    report = bot_module.log_progress(every=0.0)

    with caplog.at_level(logging.INFO, logger="stenos"):
        for done in range(1, 4):
            report(done, 3)

    assert len(caplog.messages) == 3


async def test_stopping_reports_progress_to_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # run_pipeline has taken a progress callback since it was written and
    # nothing ever passed one, so a long call transcribed in silence.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    feed(bot.sessions[1])

    with caplog.at_level(logging.INFO, logger="stenos"):
        await command(bot, "stop")(FakeContext(1, author))

    assert any("Transcribed" in message and "segments" in message for message in caplog.messages)


async def test_stop_reports_a_missing_backend_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The caller is waiting on a deferred response. An unanswered command is
    # indistinguishable from a transcription that is merely slow.
    def unavailable(*args: object, **kwargs: object) -> None:
        raise BackendUnavailableError("Install it with: uv sync --extra mlx.")

    monkeypatch.setattr(bot_module, "load_backend", unavailable)
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    feed(bot.sessions[1])

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    message, attachment = stop_ctx.followup.sent[0]
    assert "backend is unavailable" in message
    assert "uv sync --extra mlx" in message
    assert attachment is None
    assert bot.sessions == {}


async def test_stop_reports_an_unexpected_transcription_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend())
    monkeypatch.setattr(bot_module, "run_pipeline", explode)
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    feed(bot.sessions[1])

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    message, _attachment = stop_ctx.followup.sent[0]
    assert "transcription failed" in message
    assert "OSError: disk full" in message


async def test_start_reports_a_failure_rather_than_going_quiet(tmp_path: Path) -> None:
    # A recording that cannot start used to leave the command unanswered and a
    # session registered, so the bot went on describing a recording that had
    # never begun.
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]

    def refuse(sink: Any, callback: Any) -> None:
        raise AttributeError("'TimestampedSink' object has no attribute '__sink_listeners__'")

    channel.voice_client.start_recording = refuse  # type: ignore[method-assign]

    ctx = FakeContext(1, author)
    await command(bot, "start")(ctx)

    message, _file = ctx.followup.sent[0]
    assert "Could not start recording" in message
    assert "3139" in message
    assert bot.sessions == {}
    assert channel.voice_client.disconnected is True


async def test_stop_answers_even_when_stopping_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["so about the pipeline"])
    )
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    feed(bot.sessions[1])

    def refuse() -> None:
        raise RuntimeError("You are not recording")

    channel.voice_client.stop_recording = refuse  # type: ignore[method-assign]

    stop_ctx = FakeContext(1, author)
    await command(bot, "stop")(stop_ctx)

    # Answered rather than left thinking, and the session released either way.
    assert stop_ctx.followup.sent
    assert bot.sessions == {}


async def test_the_sink_knows_its_client_before_recording_starts(tmp_path: Path) -> None:
    # py-cord 2.8 never tells a sink which client it belongs to, and its opus
    # decoder asserts on that while handling the first packet. Without this the
    # router thread dies with the audio already decrypted and one step from
    # being buffered, which reads as a recording that captured nothing.
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]

    await command(bot, "start")(FakeContext(1, author))

    assert bot.sessions[1].sink.client is channel.voice_client


def test_the_finished_callback_is_not_a_coroutine() -> None:
    # py-cord calls it from the router thread rather than awaiting it, so a
    # coroutine would never run and the error it reports would be lost.
    import inspect

    assert not inspect.iscoroutinefunction(bot_module._on_recording_finished)


# The ceilings. A recording that runs until the host is out of memory takes the
# whole call with it. Past MAX_BUFFER_MB it continues on disk instead, and past
# MAX_DISK_MB it stops and what it captured is written out.


async def recording_at(tmp_path: Path, *, held_mb: float, limit_mb: float) -> tuple[Any, Any]:
    """A started recording holding roughly held_mb, under a limit of limit_mb.

    Both ceilings are set, because the one that ends a recording is the disk.
    Left at its default the recording would spill and carry on, which is the
    behaviour its own tests cover.
    """
    bot = make_bot(tmp_path, max_buffer_mb=limit_mb, max_disk_mb=limit_mb)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))

    session = bot.sessions[1]
    # A channel is dropped as each packet is written, so what is held is
    # half of what was handed over.
    packets = int(held_mb * 1_000_000 / (len(SPEECH) // 2)) + 1
    feed(session, packets=packets)
    return bot, session


async def test_a_recording_under_the_limit_is_left_alone(tmp_path: Path) -> None:
    bot, _session = await recording_at(tmp_path, held_mb=0.1, limit_mb=1024.0)

    await bot.enforce_buffer_limit()

    assert 1 in bot.sessions


async def test_a_recording_over_the_limit_is_stopped_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        bot_module,
        "load_backend",
        lambda *args, **kwargs: MockBackend(texts=["so about the asset pipeline"]),
    )
    bot, session = await recording_at(tmp_path, held_mb=0.5, limit_mb=0.1)

    await bot.enforce_buffer_limit()

    assert 1 not in bot.sessions
    (message, _attachment) = session.text_channel.sent[0]
    # Named by the setting rather than by the word "buffer", since a recording
    # with somewhere to spill is bounded by the disk instead and stops with the
    # same sentence naming the other one.
    assert "0.1 MB limit" in message
    # What it captured is still transcribed and still reported.
    assert "Transcribed" in message
    assert "MAX_DISK_MB" in message


async def test_a_zero_limit_never_stops_a_recording(tmp_path: Path) -> None:
    # A host with the memory for it, and a reason to use it.
    bot, _session = await recording_at(tmp_path, held_mb=0.5, limit_mb=0.0)

    await bot.enforce_buffer_limit()

    assert 1 in bot.sessions


async def test_a_session_over_the_limit_is_only_stopped_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two checks overlapping must not both transcribe the same recording.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend())
    bot, session = await recording_at(tmp_path, held_mb=0.5, limit_mb=0.1)

    await bot.enforce_buffer_limit()
    await bot.enforce_buffer_limit()

    assert len(session.text_channel.sent) == 1


async def test_stopping_by_hand_and_by_the_limit_produce_the_same_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole reason the two paths share one function. Everything after the
    # opening sentence has to match, or one of them has drifted.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))

    bot, session = await recording_at(tmp_path, held_mb=0.5, limit_mb=0.1)
    await bot.enforce_buffer_limit()
    automatic = session.text_channel.sent[0][0]

    bot2, _ = await recording_at(tmp_path, held_mb=0.5, limit_mb=1024.0)
    stop_ctx = FakeContext(1, FakeMember(11, "Alpha"))
    await command(bot2, "stop")(stop_ctx)
    requested = stop_ctx.followup.sent[0][0]

    tail = "Transcribed"
    assert (
        automatic[automatic.index(tail) :].removesuffix(" Raise MAX_DISK_MB to record for longer.")
        == requested[requested.index(tail) :]
    )


# The loop that drives both automatic stops. Nothing exercised it, so the two
# checks were tested and the only thing that calls them in production was not.


async def test_the_watchdog_runs_both_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, _channel, _session = await recording_with_self(tmp_path)
    ran: list[str] = []

    async def note(name: str) -> None:
        ran.append(name)

    monkeypatch.setattr(bot, "enforce_connection", lambda: note("connection"))
    monkeypatch.setattr(bot, "enforce_buffer_limit", lambda: note("buffer"))

    await bot._watch_recordings()

    # Connection first: a recording nothing arrives on has no reason to be
    # measured against a ceiling it can no longer approach.
    assert ran == ["connection", "buffer"]


async def test_becoming_ready_starts_the_watchdog_once(tmp_path: Path) -> None:
    # on_ready fires again after every gateway reconnect, and starting a loop
    # that is already running raises, which would leave the bot connected with
    # nothing watching the recordings it holds.
    bot, _channel, _session = await recording_with_self(tmp_path)
    try:
        await bot.on_ready()
        assert bot._watch_recordings.is_running()

        await bot.on_ready()

        assert bot._watch_recordings.is_running()
    finally:
        bot._watch_recordings.cancel()


async def test_a_probe_that_raises_reads_as_still_connected(tmp_path: Path) -> None:
    # Same reasoning as a probe that is missing. A py-cord whose is_connected
    # raises must not be read as every recording having dropped.
    bot, channel, session = await recording_with_self(tmp_path, disconnect_grace=0.01)

    def refuse() -> bool:
        raise RuntimeError("moved in this version")

    channel.voice_client.is_connected = refuse

    await bot.enforce_connection()
    time.sleep(0.02)
    await bot.enforce_connection()

    assert 1 in bot.sessions
    assert session.disconnected_since is None


async def test_start_answers_when_joining_the_channel_fails(tmp_path: Path) -> None:
    # Joining voice times out after thirty seconds and refuses outright without
    # the Connect permission, and it runs after the deferral. Raising there
    # answered nothing, so the caller watched a spinner until Discord gave up.
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]

    async def refuse() -> Any:
        raise TimeoutError("timed out waiting for the voice handshake")

    channel.connect = refuse  # type: ignore[method-assign]
    ctx = FakeContext(1, author)

    await command(bot, "start")(ctx)

    assert ctx.followup.sent, "the deferred interaction was left unanswered"
    assert "Could not join general" in ctx.followup.sent[0][0]
    assert "Connect permission" in ctx.followup.sent[0][0]
    # And no session left behind claiming to record a channel never joined.
    assert bot.sessions == {}


async def test_a_stop_whose_interaction_expired_posts_to_the_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A deferred interaction is good for fifteen minutes. An hour of speech on
    # a CPU backend transcribes for longer, which is what this is built for, so
    # the token is dead by the time there is anything to say. The transcript is
    # written either way; the question is whether anybody is told.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    feed(bot.sessions[1])
    session = bot.sessions[1]

    ctx = FakeContext(1, author)

    async def expired(content: str, file: Any = None) -> None:
        raise RuntimeError("404 Not Found (error code: 10015): Unknown Webhook")

    ctx.followup.send = expired  # type: ignore[method-assign]

    await command(bot, "stop")(ctx)

    assert session.text_channel.sent, "the result was lost when the token expired"
    assert "Transcribed" in session.text_channel.sent[0][0]


async def test_a_recording_that_cannot_be_announced_does_not_start(tmp_path: Path) -> None:
    # The session used to be registered before the announcement, so a failure
    # there escaped with a recording running that nobody had been told about.
    # Recording law varies by jurisdiction and there is no silent mode.
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    ctx = FakeContext(1, author)

    async def refuse(content: str, file: Any = None) -> None:
        raise RuntimeError("403 Forbidden: Missing Permissions")

    ctx.followup.send = refuse  # type: ignore[method-assign]

    await command(bot, "start")(ctx)

    assert bot.sessions == {}, "a recording nobody was told about was left running"
    assert channel.voice_client.recording is False
    assert channel.voice_client.disconnected is True


async def test_status_reports_what_the_recording_holds(tmp_path: Path) -> None:
    # Nothing surfaced the buffer before, so the first sign of approaching the
    # ceiling was the recording stopping itself at it.
    bot, _channel, session = await recording_with_self(tmp_path, max_buffer_mb=512.0)
    feed(session, packets=600)
    ctx = FakeContext(1, FakeMember(11, "Alpha"))

    await command(bot, "status")(ctx)

    reply = ctx.responses[0][0]
    assert "holding" in reply
    assert "of 512 MB" in reply
    # A megabyte of speech is 1,000,000 bytes held, not 1,048,576, which is the
    # same unit MAX_BUFFER_MB is measured in.
    held = session.sink.buffered_bytes / 1_000_000
    assert f"{held:.1f} MB" in reply


async def test_status_says_when_no_buffer_limit_is_set(tmp_path: Path) -> None:
    bot, _channel, session = await recording_with_self(tmp_path, max_buffer_mb=0.0)
    feed(session)
    ctx = FakeContext(1, FakeMember(11, "Alpha"))

    await command(bot, "status")(ctx)

    # "of 0 MB" would read as a ceiling of nothing rather than no ceiling.
    assert "with no limit set" in ctx.responses[0][0]


async def test_status_reports_packets_nobody_could_be_matched_to(tmp_path: Path) -> None:
    # The sink has always counted these rather than guessing an attribution,
    # and nothing ever showed the count.
    bot, _channel, session = await recording_with_self(tmp_path)
    session.sink.write(SPEECH, None)
    session.sink.write(SPEECH, None)
    ctx = FakeContext(1, FakeMember(11, "Alpha"))

    await command(bot, "status")(ctx)

    assert "2 packets arrived before their speaker was known" in ctx.responses[0][0]


async def test_status_stays_quiet_about_attribution_when_there_is_nothing_to_say(
    tmp_path: Path,
) -> None:
    bot, _channel, session = await recording_with_self(tmp_path)
    feed(session)
    ctx = FakeContext(1, FakeMember(11, "Alpha"))

    await command(bot, "status")(ctx)

    assert "not attributed" not in ctx.responses[0][0]


async def test_a_failing_check_does_not_stop_the_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A discord.ext task re-raises after reporting, which ends the loop for the
    # lifetime of the process. Every later recording would run with no ceiling
    # and no disconnect detection, and the only sign would be one traceback.
    bot, _channel, _session = await recording_with_self(tmp_path)
    ran: list[str] = []

    async def refuse() -> None:
        raise ValueError("cannot convert float NaN to integer")

    async def note() -> None:
        ran.append("buffer")

    monkeypatch.setattr(bot, "enforce_connection", refuse)
    monkeypatch.setattr(bot, "enforce_buffer_limit", note)

    with caplog.at_level(logging.ERROR, logger="stenos"):
        await bot._watch_recordings()

    # The second check still ran, and the failure was reported rather than lost.
    assert ran == ["buffer"]
    assert any("check failed" in message for message in caplog.messages)


async def test_a_second_recording_does_not_inherit_the_first_skip_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    monkeypatch.setattr(upstream, "_skipped", 4, raising=False)

    messages = []
    for _ in range(2):
        bot = make_bot(tmp_path)
        channel = FakeVoiceChannel(2, "general", [])
        author = FakeMember(11, "Alpha", channel=channel)
        channel.members = [author]
        await command(bot, "start")(FakeContext(1, author))
        feed(bot.sessions[1])
        ctx = FakeContext(1, author)
        await command(bot, "stop")(ctx)
        messages.append(ctx.followup.sent[0][0])

    assert "4 packets would not decode" in messages[0]
    assert "would not decode" not in messages[1]


async def test_a_recording_logs_the_packets_the_repairs_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The count has been kept since the repair was written and surfaced
    # nowhere, so how much audio it saved was invisible.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    monkeypatch.setattr(upstream, "_recovered", 12, raising=False)
    bot = make_bot(tmp_path)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    feed(bot.sessions[1])

    with caplog.at_level(logging.INFO, logger="stenos"):
        await command(bot, "stop")(FakeContext(1, author))

    assert any("Recovered 12 packets" in message for message in caplog.messages)
    # Taken, so the next recording does not claim them too.
    assert upstream.recovered_frames() == 0


async def test_where_the_transcript_went_is_on_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Both ways of announcing a recording can fail: an interaction that expired
    # and a channel the bot can no longer post to. The transcript exists either
    # way, so the log has to be able to say where it went.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot = make_bot(tmp_path, keep_audio=True)
    channel = FakeVoiceChannel(2, "general", [])
    author = FakeMember(11, "Alpha", channel=channel)
    channel.members = [author]
    await command(bot, "start")(FakeContext(1, author))
    feed(bot.sessions[1])
    ctx = FakeContext(1, author)

    async def refuse(content: str, file: Any = None) -> None:
        raise RuntimeError("Unknown Webhook")

    ctx.followup.send = refuse  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger="stenos"):
        await command(bot, "stop")(ctx)

    written = [message for message in caplog.messages if message.startswith("Wrote ")]
    assert any(".txt" in message for message in written)
    assert any(".wav" in message for message in written)


# Shutting down. A recording exists only in memory until it is transcribed and
# written, so a process that exits with one running loses the whole call.


async def test_closing_finishes_a_live_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot, channel, session = await recording_with_self(tmp_path)
    feed(session)

    await bot.close()

    assert bot.sessions == {}
    (message, _attachment) = session.text_channel.sent[0]
    assert "shutting down" in message
    assert "Transcribed" in message
    assert channel.voice_client.disconnected is True


async def test_closing_with_nothing_recording_is_quiet(tmp_path: Path) -> None:
    bot = make_bot(tmp_path)

    await bot.close()

    assert bot.sessions == {}


async def test_closing_twice_does_not_transcribe_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend())
    bot, _channel, session = await recording_with_self(tmp_path)
    feed(session)

    await bot.close()
    await bot.close()

    assert len(session.text_channel.sent) == 1


async def test_closing_stops_the_watchdog(tmp_path: Path) -> None:
    # It would otherwise fire against sessions being torn down underneath it.
    bot, _channel, _session = await recording_with_self(tmp_path)
    await bot.on_ready()
    assert bot._watch_recordings.is_running()

    await bot.close()
    # cancel() marks the task; it reports as stopped once the loop runs again.
    await asyncio.sleep(0)

    assert not bot._watch_recordings.is_running()


needs_loop_signals = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "an event loop takes no signal handlers on Windows, so there is nothing "
        "to bind, and raising SIGTERM there runs the default handler and ends "
        "the process. The refusal itself is covered below."
    ),
)


@needs_loop_signals
async def test_a_termination_signal_finishes_the_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # py-cord binds both signals to the loop's stop, which returns from run and
    # cancels every task, so the close that would finish a recording is
    # cancelled part way through. Bound after py-cord's, a later binding wins.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot, _channel, session = await recording_with_self(tmp_path)
    feed(session)

    assert bot.watch_for_shutdown()
    signal.raise_signal(signal.SIGTERM)
    # The handler schedules the close, and transcription runs on a thread, so
    # the loop has to actually run rather than merely yield.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if session.text_channel.sent:
            break

    assert bot.sessions == {}
    assert "shutting down" in session.text_channel.sent[0][0]


@needs_loop_signals
async def test_a_second_signal_does_not_start_a_second_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Transcription takes minutes. Somebody pressing Ctrl+C again during it
    # would otherwise start a second close and transcribe the call twice.
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend())
    bot, _channel, session = await recording_with_self(tmp_path)
    feed(session)
    assert bot.watch_for_shutdown()

    with caplog.at_level(logging.WARNING, logger="stenos"):
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if session.text_channel.sent:
                break

    assert len(session.text_channel.sent) == 1
    assert any("Already shutting down" in message for message in caplog.messages)


async def test_a_platform_without_loop_signal_handlers_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows has none. Ctrl+C there raises into the loop and reaches close by
    # its own route, and there is no SIGTERM to catch.
    bot = make_bot(tmp_path)
    loop = asyncio.get_running_loop()

    def refuse(*args: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", refuse)

    assert bot.watch_for_shutdown() is False


def test_the_route_a_platform_without_signal_handlers_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows finishes a recording through py-cord's cleanup, not a handler.

    Nothing is bound there, so Ctrl+C raises out of run_forever, py-cord
    cancels every task, and the runner's finally awaits close. That close is
    running inside a task that has already been cancelled, which is the reason
    to doubt it: an await there could raise the cancellation straight back and
    lose the call. It does not, because the cancel is requested once and the
    delivery is over by the time the finally runs.

    Synchronous, because _cancel_tasks runs the loop itself and cannot be
    called from inside one, which is why the tests above cannot reach here.
    """
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    policy = asyncio.get_event_loop_policy()
    try:
        previous: Any = policy.get_event_loop()
    except RuntimeError:
        previous = None  # The asynchronous tests leave none behind.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        bot, _channel, session = loop.run_until_complete(recording_with_self(tmp_path))
        feed(session)

        async def runner() -> None:
            try:
                await asyncio.sleep(3600)  # The gateway, which never returns.
            finally:
                if not bot.is_closed():
                    await bot.close()

        gateway = asyncio.ensure_future(runner(), loop=loop)
        loop.call_soon(loop.stop)  # Stands in for the KeyboardInterrupt.
        loop.run_forever()
        cancel_tasks(loop)
    finally:
        loop.close()
        asyncio.set_event_loop(previous)

    # Cancelled rather than merely finished, so the close above really did run
    # on the path this is here to hold rather than on an ordinary return.
    assert gateway.cancelled()
    assert bot.sessions == {}
    assert "shutting down" in session.text_channel.sent[0][0]
    assert session.text_channel.sent[0][1] is not None  # The transcript.


# Spilling, end to end. The ceiling used to end a recording because there was
# nowhere for the audio to go; now it moves and the call carries on.


async def test_a_recording_past_the_memory_ceiling_continues_on_disk(tmp_path: Path) -> None:
    bot, session = await recording_at(tmp_path, held_mb=0.5, limit_mb=1024.0)
    session.sink._spill_above = 1  # Cross it without holding a gigabyte first.
    session.sink.cleanup()

    assert session.sink.spilling is True
    assert session.sink.buffered_bytes == 0
    assert session.sink.total_bytes > 0
    # Still recording, because the disk ceiling is what ends a call now.
    assert 1 in bot.sessions
    assert partial_recordings(tmp_path) != []


async def test_a_spilled_recording_transcribes_and_cleans_up_after_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_module, "load_backend", lambda *a, **k: MockBackend(texts=["a line"]))
    bot, _channel, session = await recording_with_self(tmp_path)
    session.sink._spill_above = 1
    feed(session)
    session.sink.cleanup()
    assert session.sink.spilling is True

    await command(bot, "stop")(FakeContext(1, FakeMember(11, "Alpha")))

    assert session.sink.segments()
    # The audio was read back off disk to transcribe it, and the directory is
    # gone now that the transcript is written.
    assert partial_recordings(tmp_path) == []
    assert list(tmp_path.glob("*.txt"))


async def test_a_recording_told_to_keep_everything_resident_has_nowhere_to_spill(
    tmp_path: Path,
) -> None:
    # MAX_BUFFER_MB of zero asks for the whole call in memory, so there is no
    # ceiling to cross and nothing to open.
    bot, _channel, session = await recording_with_self(tmp_path, max_buffer_mb=0.0)

    assert session.sink.storage is None
    assert session.sink.spills is False
    assert bot.ceiling(session)[1] == "MAX_BUFFER_MB"


async def test_the_ceiling_that_ends_a_recording_is_the_disk_when_it_can_spill(
    tmp_path: Path,
) -> None:
    bot, _channel, session = await recording_with_self(tmp_path)

    assert session.sink.spills is True
    assert bot.ceiling(session) == (bot.config.max_disk_mb, "MAX_DISK_MB")
