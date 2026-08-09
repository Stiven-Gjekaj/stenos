"""Discord bot commands and the offline pipeline that turns a recording into files."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import platform
import signal
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import discord
from discord.ext import tasks

from . import __version__
from .audio import TARGET_SAMPLE_RATE, Segment, prepare_segments, write_speaker_wav
from .config import Config, ConfigError, certificate_bundle, load_config
from .integrity import check_recording
from .sink import OPUS_PATH_VARIABLE, TimestampedSink, bundle_directory, ensure_opus
from .spill import (
    SPILL_SUFFIX,
    SpilledRecording,
    SpillWriter,
    partial_recordings,
    read_spill,
)
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
    resolve_speaker,
    sanitize_filename,
    split_hms,
    transcript_paths,
    transcript_stem,
    write_sidecar,
    write_transcript,
)
from .upstream import (
    apply_receive_repair,
    quieten_rtcp_reports,
    quieten_stale_receive_warning,
    receive_repair_state,
    recover_decoded_audio,
    recover_flushed_packets,
    take_recovered_frames,
    take_skipped_frames,
    tolerate_double_stop,
    tolerate_undecodable_frames,
)
from .voice import PYCORD_RECEIVE_ISSUE, dave_state, dave_support, receive_support

__all__ = [
    "BUFFER_CHECK_SECONDS",
    "PROGRESS_INTERVAL",
    "RecordingResult",
    "RecordingSession",
    "StenosBot",
    "build_bot",
    "describe_environment",
    "describe_result",
    "discard_audio",
    "finish_recording",
    "format_duration",
    "log_progress",
    "main",
    "output_state",
    "register_commands",
    "run_pipeline",
    "save_audio",
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
    #: Written only when KEEP_AUDIO is set, one file per participant.
    audio_paths: list[Path] = field(default_factory=list)


def discard_audio(sink: TimestampedSink) -> None:
    """Release the audio held by a sink, wherever it is being held.

    A recording that stayed inside its memory ceiling is emptied. One that
    outgrew it also has a directory of samples to take away, which is only safe
    here: this runs after the transcript and the sidecar are on disk, so what
    is being removed has already been read and written out.

    A recording that ends any other way leaves the directory behind on purpose.
    That is the whole point of it, and ``--recover`` is what reads it.
    """
    for segment in sink.segments():
        segment.clear()
    storage = sink.storage
    if storage is not None:
        try:
            storage.discard()
        except OSError:
            # The transcript is already written, so this costs disk rather than
            # the recording, and the directory can be removed by hand.
            log.warning("Could not remove %s.", storage.directory, exc_info=True)


#: Longest a speaker's name may run inside an audio file name.
_SPEAKER_IN_FILENAME = 32


def save_audio(
    sink: TimestampedSink,
    names: Mapping[int, str],
    *,
    transcript_path: Path,
) -> list[Path]:
    """Write each participant's audio beside the transcript, one file each.

    Named after the speaker as well as their identifier: the name is what makes
    a directory of these readable, and the identifier is what keeps two people
    called the same thing in separate files.

    The stem comes from the transcript rather than being worked out again,
    because a transcript whose name was already taken carries a counter. Built
    separately, the audio would miss that counter, land on the previous
    recording's files, and pair with the wrong transcript.
    """
    stem = transcript_path.stem
    by_speaker: dict[int, list[Segment]] = {}
    for segment in sink.segments():
        by_speaker.setdefault(segment.user_id, []).append(segment)

    written: list[Path] = []
    for user_id, segments in sorted(by_speaker.items()):
        # Shortened well below what a channel name is allowed. The stem is
        # already up to 104 characters, and Windows measures the whole path
        # against 260, so a full length name here plus an identifier plus a
        # directory to live in can pass it.
        speaker = sanitize_filename(
            resolve_speaker(user_id, names), fallback="speaker", max_length=_SPEAKER_IN_FILENAME
        )
        try:
            written.append(
                write_speaker_wav(
                    transcript_path.with_name(f"{stem}-{speaker}-{user_id}.wav"), segments
                )
            )
        except Exception:
            # The transcript is the deliverable and it is already written. A
            # disk that will not take the audio must not lose the text.
            log.exception("Could not write the audio for %s", user_id)
    return written


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

    audio_paths: list[Path] = []
    if config.keep_audio:
        audio_paths = save_audio(sink, names, transcript_path=transcript_path)

    # Always, now that keeping it means writing it out. Holding the buffers
    # instead did nothing: the session is dropped the moment the command that
    # owns it returns, so what was kept was freed a few lines later and could
    # not be reached in between.
    discard_audio(sink)

    return RecordingResult(
        transcript_path=transcript_path,
        sidecar_path=sidecar_path,
        lines=lines,
        segment_count=len(segments),
        duration=duration,
        packet_count=packet_count,
        speakers=len({line.user_id for line in lines}),
        audio_paths=audio_paths,
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
    #: Read for the upload limit when attaching a transcript. Held on the
    #: session rather than taken from the command, so a recording that stops
    #: itself reads the same thing a stopped one does.
    guild: Any = None
    names: dict[int, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_monotonic: float = field(default_factory=time.perf_counter)
    #: When the voice connection was first seen down, or None while it is up.
    #: A reconnect reads as disconnected until it succeeds, so what decides a
    #: recording is how long this has been set rather than that it is.
    disconnected_since: float | None = None

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
    """Render a duration as hours, minutes, and seconds, dropping empty units."""
    hours, minutes, secs = split_hms(seconds)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def describe_result(result: RecordingResult, *, stopped: str = "Recording stopped") -> str:
    """Summarise a finished recording for the text channel.

    ``stopped`` opens the message, so a recording that ended itself can say why
    in the same sentence rather than in one placed awkwardly before it.
    """
    if result.packet_count == 0:
        return (
            f"{stopped}, but no audio was received. "
            "No transcript was produced. This is expected when the voice "
            "connection carried no decodable audio; see the known limitations "
            "section of the documentation."
        )
    if not result.lines:
        # Audio arrived and was transcribed, and none of it survived into the
        # transcript: every segment came back empty, or was held back as
        # something the model invented rather than heard. Reporting the segment
        # count and "from 0 speakers" described that as a success.
        return (
            f"{stopped} after {format_duration(result.duration)}. "
            f"{result.segment_count} segments were transcribed and none produced "
            f"a usable line, so the transcript is empty. Audio with nothing in it "
            f"is held back rather than written out, since the model returns a "
            f"confident sentence for it."
        )
    return (
        f"{stopped}. Transcribed {result.segment_count} segments "
        f"from {result.speakers} speakers over {format_duration(result.duration)}."
    )


#: How often a recording is measured against the buffer ceiling and its
#: connection checked. Often enough that a runaway is caught within a few
#: seconds of audio, rarely enough to be free.
BUFFER_CHECK_SECONDS = 15.0

#: Shortest gap between two progress lines, in seconds.
PROGRESS_INTERVAL = 15.0

log = logging.getLogger("stenos")


def log_progress(every: float = PROGRESS_INTERVAL) -> ProgressCallback:
    """Report transcription to the log, sparingly.

    Transcription is the longest part of a recording and the only part with no
    outward sign that it is working. A recording that stopped itself has nobody
    waiting on an interaction either, so the log is the only place its progress
    can appear at all.

    Reported on a timer rather than per segment, since an hour of conversation
    is hundreds of them and a line each would bury everything else. The first
    and last always report: the first is what says the work started, and the
    last is what says it finished rather than stalled.
    """
    # None rather than zero. perf_counter counts from an arbitrary point, and
    # on a host where that point is recent, zero is a time within the interval
    # and the opening report is the one held back.
    last: float | None = None

    def report(done: int, total: int) -> None:
        nonlocal last
        now = time.perf_counter()
        if last is not None and done < total and now - last < every:
            return
        last = now
        log.info("Transcribed %d of %d segments (%.0f%%).", done, total, 100.0 * done / total)

    return report


class StenosBot(discord.Bot):  # type: ignore[misc]
    """Discord client owning at most one recording per guild."""

    def __init__(self, config: Config, **options: Any) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents, **options)
        self.config = config
        self.sessions: dict[int, RecordingSession] = {}
        self._shutting_down = False

    @tasks.loop(seconds=BUFFER_CHECK_SECONDS)
    async def _watch_recordings(self) -> None:
        # Connection first. A recording nothing is arriving on has no reason to
        # be measured against a ceiling it can no longer approach.
        #
        # Each is guarded separately, and neither is allowed to escape. A
        # discord.ext task re-raises after reporting, which ends the loop for
        # the lifetime of the process: every later recording would then run
        # with no ceiling and no disconnect detection, and the only sign would
        # be one traceback long since scrolled past. A check that fails is
        # worth a line in the log and another attempt in fifteen seconds.
        for check in (self.enforce_connection, self.enforce_buffer_limit):
            try:
                await check()
            except Exception:
                log.exception("A recording check failed, and will run again shortly")

    def watch_for_shutdown(self) -> bool:
        """Ask to be told about a termination signal, rather than stopped by it.

        py-cord binds both signals to the event loop's stop, which returns from
        run and cancels every task, so the close that would finish a recording
        is cancelled part way through. Bound here instead, after py-cord has
        bound them, because a later binding replaces an earlier one.

        Windows has no signal handlers on an event loop and says so. Ctrl+C
        there raises into the loop and reaches close by its own route, and
        there is no SIGTERM to catch.
        """
        loop = asyncio.get_running_loop()
        try:
            for received in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(received, self._begin_shutdown, received)
        except (NotImplementedError, RuntimeError, AttributeError, ValueError):
            log.debug("This platform does not take signal handlers on the event loop.")
            return False
        return True

    def _begin_shutdown(self, received: signal.Signals) -> None:
        """Close on the loop, once, however many signals arrive.

        A second signal while a recording is still transcribing would otherwise
        start a second close and transcribe it twice.
        """
        if self._shutting_down:
            log.warning("Already shutting down. %s ignored.", received.name)
            return
        self._shutting_down = True
        log.info("%s received. Finishing any recording before exit.", received.name)
        self.loop.create_task(self.close())

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)
        # on_ready fires again after every reconnect, and starting a loop that
        # is already running raises.
        if not self._watch_recordings.is_running():
            self._watch_recordings.start()
        self.watch_for_shutdown()

    async def over_budget(self) -> list[RecordingSession]:
        """Sessions holding more audio than the configured ceiling allows.

        Removed from the register as they are found, so a session cannot be
        stopped twice by two checks overlapping, and so the stop command sees a
        recording that is already ending as one that is not there.
        """
        over = [session for session in list(self.sessions.values()) if self._past_ceiling(session)]
        for session in over:
            self.sessions.pop(session.guild_id, None)
        return over

    def ceiling(self, session: RecordingSession) -> tuple[float, str]:
        """The limit that ends this recording, and the setting that named it.

        A recording with somewhere to spill is bounded by the disk it is
        spilling to. One without is bounded by memory, as every recording was
        before there was anywhere else for the audio to go.
        """
        if session.sink.spills:
            return self.config.max_disk_mb, "MAX_DISK_MB"
        return self.config.max_buffer_mb, "MAX_BUFFER_MB"

    def _past_ceiling(self, session: RecordingSession) -> bool:
        limit, _setting = self.ceiling(session)
        if limit <= 0:
            return False
        # Both halves, because a recording that spilled holds most of itself on
        # disk and measuring only what is resident would never fire again.
        return session.sink.total_bytes > int(limit * 1_000_000)

    def connection_lost(self, session: RecordingSession) -> bool:
        """Whether a session's voice client reports itself no longer connected.

        Read through a guard rather than directly. A py-cord that renames or
        removes ``is_connected`` would otherwise read as every recording having
        dropped, and the next check would end all of them.
        """
        probe = getattr(session.voice_client, "is_connected", None)
        if not callable(probe):
            return False
        try:
            return not bool(probe())
        except Exception:
            return False

    async def stranded(self) -> list[RecordingSession]:
        """Sessions whose voice connection has been gone longer than the grace.

        Losing the network takes the gateway with it, so the voice state update
        that would have said so never arrives and the only account left is the
        connection's own. py-cord reconnects and resumes on its own and reads
        as disconnected for the whole of that attempt, which is why a recording
        ends on how long the connection has been gone rather than on the first
        check that finds it missing.

        Removed from the register as they are found, for the same reason
        ``over_budget`` removes them: two checks overlapping must not both end
        the same recording.
        """
        grace = self.config.disconnect_grace
        if grace <= 0:
            return []

        now = time.perf_counter()
        lost = []
        for session in list(self.sessions.values()):
            if not self.connection_lost(session):
                # Cleared rather than left. A connection that came back must
                # not count the time it was away against the next outage.
                session.disconnected_since = None
                continue
            if session.disconnected_since is None:
                session.disconnected_since = now
                log.warning(
                    "Voice connection to %s is down, waiting %.0fs for it to come back.",
                    session.channel_name,
                    grace,
                )
            elif now - session.disconnected_since >= grace:
                lost.append(session)

        for session in lost:
            self.sessions.pop(session.guild_id, None)
        return lost

    async def enforce_connection(self) -> None:
        """End any recording whose connection did not come back, and say why."""
        for session in await self.stranded():
            log.warning(
                "Voice connection to %s did not come back, stopping the recording.",
                session.channel_name,
            )
            await self.finish_and_report(
                session,
                stopped=(
                    f"Recording stopped after {format_duration(session.elapsed())}: "
                    f"the voice connection to {session.channel_name} was lost and did "
                    f"not come back within {self.config.disconnect_grace:g} seconds"
                ),
                closing=(
                    "Everything captured before the connection dropped was transcribed. "
                    "Raise DISCONNECT_GRACE to wait longer for a recovery."
                ),
            )

    async def finish_and_report(
        self,
        session: RecordingSession,
        *,
        stopped: str,
        closing: str = "",
    ) -> None:
        """Finish a recording nobody asked to stop, and post what came of it.

        There is no interaction to answer, so what the stop command would have
        replied goes to the channel the recording was started from instead.
        Sending is guarded: that channel may be gone by now, and failing to
        announce a transcript must not discard one already written to disk.
        """
        message, attachment = await finish_recording(
            self, session, stopped=stopped, closing=closing
        )
        await _reply(session.text_channel, message, attachment)

    async def enforce_buffer_limit(self) -> None:
        """End any recording that has outgrown the ceiling, and say why.

        A recording that runs until the host is out of memory takes the whole
        call with it. Stopping it leaves everything captured so far written out
        and a message naming the setting that decided it.
        """
        for session in await self.over_budget():
            limit, setting = self.ceiling(session)
            held = session.sink.total_bytes / 1_000_000
            log.warning(
                "Recording in %s reached the %s limit at %.1f MB, stopping it.",
                session.channel_name,
                setting,
                held,
            )
            await self.finish_and_report(
                session,
                stopped=(
                    f"Recording stopped at the {limit:g} MB limit after "
                    f"{format_duration(session.elapsed())}"
                ),
                closing=f"Raise {setting} to record for longer.",
            )

    async def finish_all(self, *, stopped: str, closing: str = "") -> None:
        """End every live recording, one at a time, reporting each.

        Sessions are taken from the register as they are handled, so a check
        running alongside this finds nothing left to end rather than ending the
        same recording twice.
        """
        for guild_id in list(self.sessions):
            session = self.sessions.pop(guild_id, None)
            if session is None:
                continue
            log.warning("Finishing the recording in %s before exit.", session.channel_name)
            await self.finish_and_report(session, stopped=stopped, closing=closing)

    async def close(self) -> None:
        """Finish every live recording before disconnecting.

        A recording exists only in memory until it is transcribed and written,
        so a process that exits with one running loses the whole call. The
        intended host is unattended and runs under a service manager that
        restarts it, which makes this the ordinary path rather than a rare one.

        Transcription can take minutes, and this is what keeps the process
        alive for them. A service manager that kills rather than waits is the
        one thing that can still lose a recording here, which is why the
        operational notes ask for a stop timeout that allows for it.
        """
        if self._watch_recordings.is_running():
            self._watch_recordings.cancel()

        await self.finish_all(
            stopped="Recording stopped because the bot is shutting down",
            closing="Everything captured up to that point was transcribed.",
        )
        await super().close()

    def is_self(self, member: Any) -> bool:
        """Whether a voice state update is about this bot rather than a participant."""
        user = self.user
        if user is None:
            return False
        return int(getattr(member, "id", 0)) == int(user.id)

    async def on_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """Track a recorded channel's membership, and notice the bot leaving it."""
        session = self.sessions.get(getattr(member.guild, "id", 0))
        if session is None:
            return

        if self.is_self(member):
            await self.left_the_channel(session, after)
            return

        if getattr(after.channel, "id", None) == session.channel_id:
            session.remember(member)

    async def left_the_channel(self, session: RecordingSession, after: Any) -> None:
        """React to the bot's own voice state changing during a recording.

        Being moved to another channel ends the recording at once. It cannot be
        carried on through: what was captured belongs to the channel the
        transcript is named after, and a recording that quietly followed the
        bot elsewhere would file one call's speech under another's. A reconnect
        rejoins the channel it left, so a different one is always a real move.

        Leaving the channel entirely does not end it, though it looks like the
        clearer signal of the two. py-cord's reconnect asks Discord to remove
        the bot from the channel before rejoining, so this arrives during a
        recovery exactly as it does during a kick, and nothing here can tell
        them apart. It starts the disconnect clock instead and leaves the
        decision to the grace period, which costs a kicked recording that grace
        and saves a recovering one entirely.
        """
        channel_id = getattr(getattr(after, "channel", None), "id", None)
        if channel_id == session.channel_id:
            return

        if channel_id is None:
            if session.disconnected_since is None:
                session.disconnected_since = time.perf_counter()
                log.warning(
                    "Removed from %s. Waiting to see whether it is a reconnect.",
                    session.channel_name,
                )
            return

        # Deregistered here, since the handler above only looked the session up.
        # Discord repeats voice state updates, so a session left registered
        # would be transcribed once per arrival.
        if self.sessions.pop(session.guild_id, None) is None:
            return

        log.warning("Moved out of %s, stopping the recording.", session.channel_name)
        await self.finish_and_report(
            session,
            stopped=(
                f"Recording stopped after {format_duration(session.elapsed())}: "
                f"the bot was moved out of {session.channel_name}"
            ),
            closing="Everything captured up to that point was transcribed.",
        )


async def finish_recording(
    bot: StenosBot,
    session: RecordingSession,
    *,
    stopped: str = "Recording stopped",
    closing: str = "",
) -> tuple[str, Any]:
    """Stop, transcribe, and write out one recording, whoever asked for it.

    Shared by the stop command and the buffer ceiling, so a recording that ends
    itself produces the same transcript and the same message as one that was
    asked to stop, rather than a second implementation that drifts from this.

    Returns what to say and what to attach, if anything. Nothing here raises: a
    caller is usually answering a deferred command, and an exception escaping
    would leave it answering forever, which is indistinguishable from a
    transcription that is merely slow.
    """
    try:
        session.voice_client.stop_recording()
    except Exception as error:
        log.warning("Stopping the recording failed: %s", error)
    session.sink.cleanup()

    # Read before disconnecting. The connection state is gone afterwards, and
    # it is the only account of why a recording captured nothing.
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
        return f"{stopped}. {integrity.detail}", None

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
            progress=log_progress(),
        )
    except BackendUnavailableError as error:
        log.error("Transcription backend unavailable: %s", error)
        return (
            f"{stopped}, but the transcription backend is unavailable, "
            f"so the audio could not be transcribed. {error}"
        ), None
    except Exception as error:
        log.exception("Transcription failed")
        return (f"{stopped}, but transcription failed: {error.__class__.__name__}: {error}"), None

    # Recorded before anything is sent. Both ways of announcing a recording can
    # fail, an interaction that expired and a channel the bot can no longer
    # post to, and the transcript exists either way: the log is then the only
    # thing that can say where it went.
    log.info(
        "Wrote %s (%d lines, %d segments).",
        result.transcript_path,
        len(result.lines),
        result.segment_count,
    )
    for path in result.audio_paths:
        log.info("Wrote %s.", path)

    message = describe_result(result, stopped=stopped)
    # Taken rather than read. The counter belongs to a method shared by every
    # recording, so leaving it standing makes the next recording report these
    # frames as its own.
    skipped = take_skipped_frames()

    # Logged rather than posted. Recovered packets are audio the repairs kept
    # rather than anything the caller has to act on, and the count has never
    # been surfaced anywhere despite being kept since the repair was written.
    recovered = take_recovered_frames()
    if recovered:
        log.info(
            "Recovered %d packets a jitter buffer flush would have discarded.",
            recovered,
        )
    if skipped:
        # A gap in a transcript should have a stated cause rather than looking
        # like a pause in the conversation.
        message += (
            f" {skipped} packets would not decode and were skipped, "
            f"so short stretches of audio are missing."
        )
    if closing:
        message += f" {closing}"

    return message, _attachment_for(result, session.guild)


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

        # Guarded for the same reason the recording below is. Joining voice is
        # the step most likely to fail outright: it times out after thirty
        # seconds, and it refuses outright without the Connect permission.
        # Raising here answers nothing, and the deferred reply the line above
        # sent stays a spinner until Discord gives up on it.
        try:
            voice_client = await channel.connect()
        except Exception as error:
            log.exception("Could not join %s", channel.name)
            await _reply(
                ctx.followup,
                f"Could not join {channel.name}: {error.__class__.__name__}: {error}. "
                f"Check that the bot has the Connect permission for that channel.",
                None,
            )
            return

        started_at = datetime.now(UTC)
        channel_name = str(channel.name)
        sink = TimestampedSink(
            segment_gap=bot.config.segment_gap,
            max_segment=bot.config.max_segment,
            storage=open_storage(bot.config, channel_name, started_at),
            spill_above=int(bot.config.max_buffer_mb * 1_000_000),
        )
        session = RecordingSession(
            guild_id=int(guild_id),
            channel_id=int(channel.id),
            channel_name=channel_name,
            text_channel=ctx.channel,
            voice_client=voice_client,
            sink=sink,
            guild=getattr(ctx, "guild", None),
            started_at=started_at,
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
            await _reply(
                ctx.followup,
                f"Could not start recording: {error.__class__.__name__}: {error}. "
                f"Voice reception is currently broken in {receive_support().version}, "
                f"tracked at {PYCORD_RECEIVE_ISSUE}.",
                None,
            )
            return

        # Announced unconditionally and non-ephemerally. Recording law varies
        # by jurisdiction and silent recording is never the intent, so a
        # recording that cannot say it has started does not start: the session
        # was registered before this and the failure escaped, which left one
        # running that nobody had been told about.
        announced = await _reply(
            ctx.followup,
            f"Recording {channel.name}. Every participant is recorded separately "
            f"and transcribed locally when the recording stops.",
            None,
        )
        if not announced:
            log.warning(
                "Could not announce the recording in %s, so it was not started.", channel.name
            )
            with contextlib.suppress(Exception):
                voice_client.stop_recording()
            sink.cleanup()
            with contextlib.suppress(Exception):
                await voice_client.disconnect()
            return

        bot.sessions[session.guild_id] = session

    @group.command(name="stop", description="Stop recording and post the transcript")
    async def record_stop(ctx: discord.ApplicationContext) -> None:
        session = bot.sessions.pop(getattr(ctx, "guild_id", 0), None)
        if session is None:
            await ctx.respond("No recording is in progress.", ephemeral=True)
            return

        await ctx.defer()
        message, attachment = await finish_recording(bot, session)

        if not await _reply(ctx.followup, message, attachment):
            # A deferred interaction is good for fifteen minutes. An hour of
            # conversation on a CPU backend transcribes for longer than that,
            # which is the case this project is built for, and the token is
            # dead by the time there is anything to say. The transcript is
            # already written, so the only thing at stake is whether anyone is
            # told: the channel the recording was started from is told instead.
            log.warning("The stop interaction expired, so the result went to the channel.")
            await _reply(session.text_channel, message, attachment)

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

        # What it holds, against what it is allowed to hold. Nothing surfaced
        # this before, so the first sign of approaching the ceiling was the
        # recording stopping itself at it.
        held = session.sink.buffered_bytes / 1_000_000
        limit = bot.config.max_buffer_mb
        ceiling = f"of {limit:g} MB" if limit > 0 else "with no limit set"
        unattributed = session.sink.unattributed_packets
        if unattributed:
            note += (
                f" {unattributed} packets arrived before their speaker was known "
                f"and were not attributed to anyone."
            )

        await ctx.respond(
            f"Recording {session.channel_name} for {format_duration(session.elapsed())}. "
            f"{len(session.sink.user_ids)} participants have spoken so far, "
            f"holding {held:.1f} MB {ceiling}.{note}",
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


async def _reply(destination: Any, message: str, attachment: Any) -> bool:
    """Send a result, reporting whether it arrived rather than raising.

    The keyword is omitted rather than passed as None. Discord's helper reads
    an attribute off whatever it is given, so an explicit None raises while
    building the message and the caller is left waiting.
    """
    try:
        if attachment is None:
            await destination.send(message)
        else:
            await destination.send(message, file=attachment)
    except Exception as error:
        log.warning("Could not deliver the result: %s: %s", error.__class__.__name__, error)
        return False
    return True


def _attachment_for(result: RecordingResult, guild: Any) -> Any:
    """Attach the transcript when it fits inside the guild's upload limit."""
    if result.packet_count == 0 or not result.lines:
        return None
    limit = getattr(guild, "filesize_limit", 0) or 0
    if result.transcript_path.stat().st_size >= limit:
        return None
    return discord.File(result.transcript_path)


def build_bot(config: Config) -> StenosBot:
    """Construct a bot with the record commands registered."""
    bot = StenosBot(config)
    guild_ids = [config.guild_id] if config.guild_id is not None else None
    register_commands(bot, guild_ids)
    return bot


def _repaired(applied: bool) -> str:
    """How a repair that reports only whether it was needed reads in the report."""
    return "applied" if applied else "not needed"


def _limit(value: float, unit: str) -> str:
    """A ceiling that zero switches off, rendered so the report says which."""
    return f"{value:g}{unit}" if value > 0 else "none"


def open_storage(config: Config, channel_name: str, started_at: datetime) -> SpillWriter | None:
    """Somewhere for this recording's audio to go if it outgrows memory.

    Handed to the sink at the start rather than made when the ceiling is
    reached, so the manifest carries the channel and the moment the call began
    rather than the moment memory ran out. Nothing is created until something
    actually spills.

    A ceiling of zero means the caller wants the whole call resident, so there
    is nowhere to spill and nothing to open.
    """
    if config.max_buffer_mb <= 0:
        return None
    stem = transcript_stem(channel_name, started_at)
    return SpillWriter(
        config.output_dir / f"{stem}{SPILL_SUFFIX}",
        channel=channel_name,
        started_at=started_at,
        sample_rate=TARGET_SAMPLE_RATE,
    )


def output_state(directory: Path) -> str:
    """Whether a transcript could actually be written to this directory.

    Tried rather than inspected, because the answer depends on the filesystem
    and on who is running, and because a recording finds out at the end of a
    call: the transcript is written once, after transcription, and a directory
    that will not take it costs the whole recording. This is the one command
    somebody runs before leaving a host unattended.
    """
    probe = directory / ".stenos-write-check"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        reason = error.strerror or error
        return f"{directory} CANNOT BE WRITTEN ({error.__class__.__name__}: {reason})"
    return str(directory)


def describe_environment(config: Config) -> str:
    """Report the resolved runtime configuration without connecting to Discord.

    Reporting a repair applies it, which is the same thing asking about the
    decryption has always done. The command exits immediately afterwards, so
    nothing is left half prepared.
    """
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
        f"maximum segment  {config.max_segment}s",
        f"buffer limit     {_limit(config.max_buffer_mb, 'MB')}",
        f"disk limit       {_limit(config.max_disk_mb, 'MB')}",
        f"disconnect grace {_limit(config.disconnect_grace, 's')}",
        f"output directory {output_state(config.output_dir)}",
        f"keep audio       {config.keep_audio}",
        f"opus loaded      {ensure_opus()}",
        f"encryption       {dave_support().summary}",
        f"receive          {receive_support().summary}",
        f"receive repair   {receive_repair_state().summary}",
        f"decode repair    {_repaired(recover_decoded_audio())}",
        f"handoff repair   {_repaired(recover_flushed_packets())}",
        f"certificates     {certificate_bundle() or 'system default'}",
    ]
    return "\n".join(lines)


def recover_recording(found: SpilledRecording, config: Config) -> RecordingResult:
    """Transcribe a recording its own process never finished.

    The audio is read back into segments and put through the same pipeline a
    live recording ends with, so a recovered transcript is the same file, named
    the same way, as the one the call would have produced. Nothing about the
    directory says which participant said what beyond the identifiers, which is
    why the manifest carries the names.
    """
    sink = TimestampedSink()
    for item in found.segments:
        # Appended rather than written through, because write places a packet on
        # a clock and these already carry the offsets the call gave them.
        sink._segments.append(
            Segment(
                user_id=item.user_id,
                start=item.start,
                pcm=bytearray(found.audio_of(item)),
                sample_rate=item.sample_rate,
            )
        )
    return run_pipeline(
        sink,
        found.names,
        channel_name=found.channel,
        config=config,
        backend=load_backend(config.whisper_backend, config.whisper_model),
        recorded_at=found.started_at,
    )


def recover(config: Config) -> int:
    """Transcribe every recording left behind by a process that did not finish.

    Reports rather than raises, per directory, because one unreadable manifest
    among several is not a reason to leave the rest where they are.
    """
    left = partial_recordings(config.output_dir)
    if not left:
        print(f"Nothing to recover in {config.output_dir}.")  # noqa: T201  this is the output
        return 0

    failed = 0
    for directory in left:
        found = read_spill(directory)
        if found is None:
            log.warning("%s holds no recording this version can read.", directory)
            failed += 1
            continue
        log.info(
            "Recovering %s from %s: %d segments.",
            found.channel,
            directory,
            len(found.segments),
        )
        try:
            result = recover_recording(found, config)
        except Exception:
            log.exception("Could not transcribe %s, so it is left where it is.", directory)
            failed += 1
            continue
        print(f"{directory} -> {result.transcript_path}")  # noqa: T201  this is the output
        for child in sorted(directory.glob("*")):
            child.unlink(missing_ok=True)
        directory.rmdir()

    return 1 if failed else 0


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
        "--recover",
        action="store_true",
        help="transcribe recordings left behind by a process that did not finish",
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
        print(describe_environment(config))  # noqa: T201  the report is this command's output
        return 0

    if args.recover:
        return recover(config)

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

    # Before the tolerance below, which wraps the method this replaces. The
    # other order would wrap the version being replaced, and the tolerance
    # would go with it.
    recover_decoded_audio()

    # py-cord drops every buffered packet but one at the first sign of a
    # sequence gap, which is most likely where a stream starts.
    recover_flushed_packets()

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
