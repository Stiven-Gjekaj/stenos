"""Discord bot commands and the offline pipeline that turns a recording into files."""

from __future__ import annotations

import argparse
import asyncio
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
from .config import Config, ConfigError, load_config, resolve_backend
from .sink import TimestampedSink
from .transcribe import (
    ProgressCallback,
    TranscriptionBackend,
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
        bot.sessions[session.guild_id] = session

        voice_client.start_recording(sink, _on_recording_finished)

        # Announced unconditionally and non-ephemerally. Recording law varies
        # by jurisdiction and silent recording is never the intent.
        await ctx.respond(
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
        session.voice_client.stop_recording()
        await session.voice_client.disconnect()

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

        await ctx.followup.send(
            describe_result(result),
            file=_attachment_for(result, ctx),
        )

    @group.command(name="status", description="Report the state of the current recording")
    async def record_status(ctx: discord.ApplicationContext) -> None:
        session = bot.sessions.get(getattr(ctx, "guild_id", 0))
        if session is None:
            await ctx.respond("No recording is in progress.", ephemeral=True)
            return

        await ctx.respond(
            f"Recording {session.channel_name} for {format_duration(session.elapsed())}. "
            f"{len(session.sink.user_ids)} participants have spoken so far.",
            ephemeral=True,
        )

    return group


async def _on_recording_finished(exception: BaseException | None = None) -> None:
    """Surface any error raised by the receive loop."""
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
    resolved = resolve_backend(config.whisper_backend)
    lines = [
        f"stenos {__version__}",
        f"python           {platform.python_version()} on {platform.system()} {platform.machine()}",
        f"backend          {config.whisper_backend} resolves to {resolved}",
        f"model            {config.whisper_model}",
        f"language         {config.language or 'auto'}",
        f"segment gap      {config.segment_gap}s",
        f"minimum segment  {config.min_segment}s",
        f"output directory {config.output_dir}",
        f"keep audio       {config.keep_audio}",
        f"opus loaded      {discord.opus.is_loaded()}",
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

    build_bot(config).run(config.discord_token)
    return 0
