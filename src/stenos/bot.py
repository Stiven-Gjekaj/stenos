"""Discord bot commands and the offline pipeline that turns a recording into files."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import platform
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import discord

from . import __version__
from .audio import prepare_segments
from .config import Config, ConfigError, certificate_bundle, load_config
from .integrity import check_recording
from .sink import OPUS_PATH_VARIABLE, TimestampedSink, bundle_directory, ensure_opus
from .transcribe import (
    BackendUnavailableError,
    ProgressCallback,
    TranscriptionBackend,
    backend_status,
    load_backend,
    transcribe_segments,
)
from .transcript import (
    TranscriptLine,
    build_sidecar,
    merge,
    transcript_paths,
    write_sidecar,
    write_transcript,
)
from .upstream import (
    apply_receive_repair,
    quieten_rtcp_reports,
    quieten_stale_receive_warning,
    receive_repair_state,
    skipped_frames,
    tolerate_double_stop,
    tolerate_undecodable_frames,
)
from .voice import PYCORD_RECEIVE_ISSUE, dave_state, dave_support, receive_support

__all__ = [
    "RecordingResult",
    "RecordingSession",
    "StenosBot",
    "build_bot",
    "describe_environment",
    "describe_result",
    "discard_audio",
    "format_duration",
    "main",
    "register_commands",
    "run_pipeline",
]


@dataclass(frozen=True, slots=True)
class RecordingResult:
    """Everything produced by one recording, for reporting back to the caller."""

    transcript_path: Path
    sidecar_path: Path
    lines: list[TranscriptLine]
    segment_count: int
    duration: float
    packet_count: int
    speakers: int


def discard_audio(sink: TimestampedSink) -> None:
    """Release the buffered audio held by a sink.

    Recordings are held in memory rather than on disk, so discarding means
    emptying the buffers once the transcript has been written.
    """
    for segment in sink.segments():
        segment.pcm.clear()


def run_pipeline(
    sink: TimestampedSink,
    names: Mapping[int, str],
    *,
    channel_name: str,
    config: Config,
    backend: TranscriptionBackend,
    recorded_at: datetime | None = None,
    progress: ProgressCallback | None = None,
) -> RecordingResult:
    """Convert, transcribe, merge, and write out one recording.

    Separated from the Discord command handlers so the whole path can be
    exercised offline, and so it can be run on a worker thread without
    blocking the gateway heartbeat.
    """
    recorded_at = recorded_at or datetime.now(UTC)
    duration = sink.duration
    packet_count = sink.packet_count

    segments = prepare_segments(sink.segments(), min_segment=config.min_segment)
    results = transcribe_segments(
        segments,
        backend,
        language=config.language,
        progress=progress,
    )
    lines = merge(results, names)

    transcript_path, sidecar_path = transcript_paths(config.output_dir, channel_name, recorded_at)
    write_transcript(transcript_path, lines)
    write_sidecar(
        sidecar_path,
        build_sidecar(
            results,
            names,
            channel=channel_name,
            recorded_at=recorded_at,
            duration=duration,
            backend=backend.name,
            model=config.whisper_model,
        ),
    )

    if not config.keep_audio:
        discard_audio(sink)

    return RecordingResult(
        transcript_path=transcript_path,
        sidecar_path=sidecar_path,
        lines=lines,
        segment_count=len(segments),
        duration=duration,
        packet_count=packet_count,
        speakers=len({line.user_id for line in lines}),
    )


@dataclass
class RecordingSession:
    """State held for one guild while a recording is in progress."""

    guild_id: int
    channel_id: int
    channel_name: str
    text_channel: Any
    voice_client: Any
    sink: TimestampedSink
    names: dict[int, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = field(default_factory=time.perf_counter)

    def elapsed(self) -> float:
        """Seconds since recording began."""
        return time.perf_counter() - self.started_monotonic

    def remember(self, member: Any) -> None:
        """Cache a participant's display name.

        Names are resolved while recording because a participant may leave
        before the call ends, after which the guild no longer resolves them.
        """
        self.names[int(member.id)] = str(getattr(member, "display_name", member))

    def remember_all(self, members: Iterable[Any]) -> None:
        for member in members:
            self.remember(member)


def format_duration(seconds: float) -> str:
    """Render a duration as hours, minutes, and seconds."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def describe_result(result: RecordingResult) -> str:
    """Summarise a finished recording for the text channel."""
    if result.packet_count == 0:
        return (
            "Recording stopped, but no audio was received. "
            "No transcript was produced. This is expected when the voice "
            "connection carried no decodable audio; see the known limitations "
            "section of the documentation."
        )
    return (
        f"Recording stopped. Transcribed {result.segment_count} segments "
        f"from {result.speakers} speakers over {format_duration(result.duration)}."
    )


log = logging.getLogger("stenos")


class StenosBot(discord.Bot):  # type: ignore[misc]
    """Discord client owning at most one recording per guild."""

    def __init__(self, config: Config, **options: Any) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents, **options)
        self.config = config
        self.sessions: dict[int, RecordingSession] = {}

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)

    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """Cache the display name of anyone who joins a channel being recorded."""
        session = self.sessions.get(getattr(member.guild, "id", 0))
        if session is None:
            return
        if getattr(after.channel, "id", None) == session.channel_id:
            session.remember(member)


def register_commands(bot: StenosBot, guild_ids: list[int] | None = None) -> Any:
    """Attach the record command group to a bot instance."""
    group = bot.create_group(
        "record",
        "Record the voice channel and produce a speaker attributed transcript",
        guild_ids=guild_ids,
    )

    @group.command(name="start", description="Join your voice channel and begin recording")
    async def record_start(ctx: discord.ApplicationContext) -> None:
        guild_id = ctx.guild_id
        if guild_id is None:
            await ctx.respond("This command works only inside a server.", ephemeral=True)
            return

        voice_state = getattr(ctx.author, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if channel is None:
            await ctx.respond("Join a voice channel first.", ephemeral=True)
            return

        if guild_id in bot.sessions:
            await ctx.respond("A recording is already in progress.", ephemeral=True)
            return

        # Deferred before connecting, not after. Joining a voice channel takes
        # several seconds and an interaction token expires after three, so a
        # reply written afterwards is refused as an unknown interaction and the
        # caller is told the application did not respond.
        await ctx.defer()

        voice_client = await channel.connect()
        sink = TimestampedSink(segment_gap=bot.config.segment_gap)
        session = RecordingSession(
            guild_id=int(guild_id),
            channel_id=int(channel.id),
            channel_name=str(channel.name),
            text_channel=ctx.channel,
            voice_client=voice_client,
            sink=sink,
        )
        session.remember_all(getattr(channel, "members", []))

        # Reported rather than raised, and registered only once recording has
        # actually begun. A failure here used to leave the command unanswered
        # and a session behind, so the bot went on describing a recording that
        # had never started.
        # py-cord 2.8 never tells a sink which client it belongs to. The line
        # that did is commented out in its reader, and the opus decoder asserts
        # on it while handling the first packet, so the router thread dies with
        # the audio already decrypted and one step from being buffered.
        sink.init(voice_client)

        # One malformed frame would otherwise stop the router thread and
        # with it the recording, discarding everything that follows.
        tolerate_undecodable_frames()

        try:
            voice_client.start_recording(sink, _on_recording_finished)
        except Exception as error:
            log.exception("Could not start recording")
            with contextlib.suppress(Exception):
                await voice_client.disconnect()
            await ctx.followup.send(
                f"Could not start recording: {error.__class__.__name__}: {error}. "
                f"Voice reception is currently broken in {receive_support().version}, "
                f"tracked at {PYCORD_RECEIVE_ISSUE}."
            )
            return

        bot.sessions[session.guild_id] = session

        # Announced unconditionally and non-ephemerally. Recording law varies
        # by jurisdiction and silent recording is never the intent.
        await ctx.followup.send(
            f"Recording {channel.name}. Every participant is recorded separately "
            f"and transcribed locally when the recording stops."
        )

    @group.command(name="stop", description="Stop recording and post the transcript")
    async def record_stop(ctx: discord.ApplicationContext) -> None:
        session = bot.sessions.pop(getattr(ctx, "guild_id", 0), None)
        if session is None:
            await ctx.respond("No recording is in progress.", ephemeral=True)
            return

        await ctx.defer()
        # The caller is waiting on a deferred response, so nothing between here
        # and the reply may raise. Stopping a recording that never started
        # raises, which used to leave the command answering forever.
        try:
            session.voice_client.stop_recording()
        except Exception as error:
            log.warning("Stopping the recording failed: %s", error)
        session.sink.cleanup()
        # Read before disconnecting. The connection state is gone afterwards,
        # and it is the only account of why a recording captured nothing.
        dave = dave_state(session.voice_client)
        with contextlib.suppress(Exception):
            await session.voice_client.disconnect()

        # Checked before the backend is loaded, so a recording that captured
        # nothing does not wait on a model that has nothing to do.
        integrity = check_recording(session.sink, dave)
        if not integrity.ok:
            log.warning(
                "Recording captured no usable audio (%s): %s",
                integrity.reason,
                dave.summary,
            )
            await ctx.followup.send(f"Recording stopped. {integrity.detail}")
            return

        # Failures are reported back to the channel rather than left to the
        # library's exception logger. The caller is waiting on a deferred
        # response, and an unanswered command is indistinguishable from a
        # transcription that is merely slow.
        try:
            backend = await asyncio.to_thread(
                load_backend,
                bot.config.whisper_backend,
                bot.config.whisper_model,
            )
            result = await asyncio.to_thread(
                run_pipeline,
                session.sink,
                session.names,
                channel_name=session.channel_name,
                config=bot.config,
                backend=backend,
                recorded_at=session.started_at,
            )
        except BackendUnavailableError as error:
            log.error("Transcription backend unavailable: %s", error)
            await ctx.followup.send(
                f"Recording stopped, but the transcription backend is unavailable, "
                f"so the audio could not be transcribed. {error}"
            )
            return
        except Exception as error:
            log.exception("Transcription failed")
            await ctx.followup.send(
                f"Recording stopped, but transcription failed: {error.__class__.__name__}: {error}"
            )
            return

        # The keyword is omitted rather than passed as None. Discord's helper
        # reads an attribute off whatever it is given, so an explicit None
        # raises while building the message and the caller is left waiting.
        message = describe_result(result)
        skipped = skipped_frames()
        if skipped:
            # A gap in a transcript should have a stated cause rather than
            # looking like a pause in the conversation.
            message += (
                f" {skipped} packets would not decode and were skipped, "
                f"so short stretches of audio are missing."
            )

        attachment = _attachment_for(result, ctx)
        if attachment is None:
            await ctx.followup.send(message)
        else:
            await ctx.followup.send(message, file=attachment)

    @group.command(name="status", description="Report the state of the current recording")
    async def record_status(ctx: discord.ApplicationContext) -> None:
        session = bot.sessions.get(getattr(ctx, "guild_id", 0))
        if session is None:
            await ctx.respond("No recording is in progress.", ephemeral=True)
            return

        # Packets arriving is the ground truth, so the encryption state is only
        # raised when nothing has been captured yet. Reporting it while audio
        # is plainly coming in would be noise, and would misread any py-cord
        # that moves the attribute this is read from.
        note = ""
        if session.sink.packet_count == 0:
            dave = dave_state(session.voice_client)
            if not dave.receives_audio:
                note = f" No audio has arrived yet: {dave.summary}."

        await ctx.respond(
            f"Recording {session.channel_name} for {format_duration(session.elapsed())}. "
            f"{len(session.sink.user_ids)} participants have spoken so far.{note}",
            ephemeral=True,
        )

    return group


def _on_recording_finished(exception: BaseException | None = None) -> None:
    """Surface any error raised by the receive loop.

    Deliberately not a coroutine. py-cord calls this straight from the router
    thread rather than awaiting it, so a coroutine would be created, never run,
    and the error it was meant to report would be lost behind a warning about
    a coroutine that was never awaited.
    """
    if exception is not None:
        log.error("Recording ended with an error: %s", exception)


def _attachment_for(result: RecordingResult, ctx: Any) -> Any:
    """Attach the transcript when it fits inside the guild's upload limit."""
    if result.packet_count == 0 or not result.lines:
        return None
    limit = getattr(getattr(ctx, "guild", None), "filesize_limit", 0) or 0
    if result.transcript_path.stat().st_size >= limit:
        return None
    return discord.File(result.transcript_path)


def build_bot(config: Config) -> StenosBot:
    """Construct a bot with the record commands registered."""
    bot = StenosBot(config)
    guild_ids = [config.guild_id] if config.guild_id is not None else None
    register_commands(bot, guild_ids)
    return bot


def describe_environment(config: Config) -> str:
    """Report the resolved runtime configuration without connecting to Discord."""
    resolved, available, detail = backend_status(config.whisper_backend)
    runtime = "frozen executable" if bundle_directory() is not None else "installation"
    lines = [
        f"stenos {__version__} ({runtime})",
        f"python           {platform.python_version()} on {platform.system()} {platform.machine()}",
        f"backend          {config.whisper_backend} resolves to {resolved}",
        f"backend usable   {available} ({detail})",
        f"model            {config.whisper_model}",
        f"language         {config.language or 'auto'}",
        f"segment gap      {config.segment_gap}s",
        f"minimum segment  {config.min_segment}s",
        f"output directory {config.output_dir}",
        f"keep audio       {config.keep_audio}",
        f"opus loaded      {ensure_opus()}",
        f"encryption       {dave_support().summary}",
        f"receive          {receive_support().summary}",
        f"receive repair   {receive_repair_state().summary}",
        f"certificates     {certificate_bundle() or 'system default'}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the stenos command."""
    parser = argparse.ArgumentParser(
        prog="stenos",
        description=(
            "Record each participant of a Discord voice channel separately and "
            "produce a timestamped, speaker attributed transcript locally."
        ),
    )
    parser.add_argument("--version", action="version", version=f"stenos {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report the resolved configuration and exit without connecting",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (default: INFO)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        config = load_config()
    except ConfigError as error:
        parser.exit(2, f"configuration error: {error}\n")

    if args.check:
        print(describe_environment(config))
        return 0

    if not ensure_opus():
        log.warning(
            "libopus could not be loaded, so received audio cannot be decoded. "
            "Install it with brew install opus on macOS or libopus0 on Linux, "
            "or set %s to its path.",
            OPUS_PATH_VARIABLE,
        )

    # Before connecting. py-cord 2.8.1 loses received audio twice over, once on
    # a call that carries no encryption and once on every call that does, which
    # would otherwise produce a recording of silence. Applied only when that is
    # what the installed version actually does.
    apply_receive_repair()

    # With reception repaired, what py-cord says about reception is out of date,
    # and the traceback its router prints at the end of a working recording
    # describes nothing that went wrong.
    quieten_stale_receive_warning()
    quieten_rtcp_reports()
    tolerate_double_stop()

    # Before connecting. Without a certificate list that exists on this
    # machine, logging in fails at the TLS handshake with an error about a
    # missing local issuer, which says nothing about the real cause.
    certificate_bundle()

    build_bot(config).run(config.discord_token)
    return 0
