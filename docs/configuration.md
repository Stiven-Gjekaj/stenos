# Configuration

Every setting Stenos reads, what it does, and when to change it. Copy
`.env.example` to `.env` and edit it, or set the variables in the environment.

Values are validated on startup. A malformed one stops the process with a
message naming the setting, rather than failing later at the point of use.

---

## Discord

### `DISCORD_TOKEN`

**Required.** The bot token from the Discord developer portal.

There is no default and no way to run without it. `stenos --help` and
`stenos --version` work without one; everything else refuses.

### `GUILD_ID`

*Default: unset.*

When set, slash commands register to that one guild and appear immediately.
Left unset, they register globally, which can take up to an hour to propagate.
Set it while developing.

---

## Transcription

### `WHISPER_BACKEND`

*Default: `mlx` in `.env.example`. Unset behaves as `auto`.*

One of `mlx`, `faster-whisper`, or `auto`. `auto` resolves to `mlx` on Apple
Silicon and `faster-whisper` on every other platform.

Selecting a backend that is not installed produces an install instruction, not
an import traceback. Check before a call:

```sh
stenos --check
```

Underscores and case are accepted, so `faster_whisper` and `MLX` both work.

### `WHISPER_MODEL`

*Default: `small`.*

One of `tiny`, `base`, `small`, `medium`, `large-v3`, or an `.en` variant of
the first three. A Hugging Face repository path is also accepted and passed
through unchanged, which is how you use a quantised community build.

`small` is the default because it is where accuracy stops improving quickly for
conversational speech. See the performance table in the README for the cost of
each size.

### `LANGUAGE`

*Default: `auto`.*

An ISO 639-1 code such as `en` or `sq`, or `auto` to let the model detect the
language of each segment.

Setting it explicitly is faster and more accurate when you know the language,
because detection runs per segment and short segments give it little to work
with.

---

## Segmentation

### `SEGMENT_GAP`

*Default: `0.4` seconds.*

How long a speaker must stop transmitting before their current segment is
closed and the next packet starts a new one.

Discord transmits only during speech, so this threshold is what separates
utterances. Raising it merges sentences separated by a short pause into one
segment; lowering it splits a single sentence across several. The default suits
conversational speech.

The comparison is strictly greater than, so a gap of exactly `SEGMENT_GAP` stays
in the current segment.

### `MIN_SEGMENT`

*Default: `0.3` seconds.*

Segments shorter than this are discarded before transcription.

This exists because Whisper hallucinates confidently on near-silence, emitting
lines like "Thank you." or "Subscribe" from breath noise and keyboard clicks.
Lower it only if you are losing genuine one-word answers, and expect phantom
lines in exchange.

### `MAX_SEGMENT`

*Default: `30` seconds.*

The point at which a segment closes for length rather than for silence.

Two reasons, and neither is arbitrary. A speaker who never pauses longer than
`SEGMENT_GAP` would otherwise hold the whole call in one segment, so nothing
bounds what is held in memory or the work of reducing it at the end. And 30
seconds is the window a Whisper encoder reads, so a segment longer than this is
chunked by the backend anyway, at a boundary you do not choose and with no
timestamp of its own. Splitting here gives each part the offset it happened at.

A split can land mid-sentence. That is already true of the backend's internal
chunking, and the bound is worth it.

### `MAX_BUFFER_MB`

*Default: `1024` megabytes. `0` removes the limit.*

Buffered audio at which a recording stops itself, transcribes what it captured,
and says so in the text channel.

A recording holds about 32,000 bytes for every second of speech, summed across
speakers, so the default is roughly nine hours before it fires. Nothing else
bounds a recording: without this, a long enough call ends with the host out of
memory and the whole recording lost. With it, the call ends early and the
transcript survives.

Raise it on a host with memory to spare. Set it to `0` only if you would rather
lose a recording than have one stop on its own.

### `DISCONNECT_GRACE`

*Default: `60` seconds. `0` waits forever.*

How long a recording waits for a lost voice connection to come back before it
ends itself, transcribes what it captured, and says so in the text channel.

Losing the network takes the gateway with it, so the event that would report a
disconnect never arrives and the only account left is the voice connection's
own state. py-cord reconnects and resumes on its own, and reads as disconnected
for the whole of that attempt, so this cannot be short: its connect timeout
alone is 30 seconds, and a recording ended during a recovery is a recording cut
in half for no reason.

Being moved to another channel is different. That arrives as an event, cannot
be anything but a real move, and ends the recording at once. Being removed from
the channel waits like a network loss does, because py-cord's reconnect asks
Discord to remove the bot before rejoining and the two are indistinguishable
when the event arrives.

Setting this to `0` therefore leaves a kicked recording running too, since
nothing else ends one.

---

## Output

### `OUTPUT_DIR`

*Default: `transcripts`.*

Where the `.txt` transcript and its `.json` sidecar are written. Created if it
does not exist.

A relative path is relative to the working directory the bot was started from,
which is the natural place for a run started by hand and rarely what a service
wants; give one an absolute path. A leading `~` is expanded.

### `KEEP_AUDIO`

*Default: `false`.*

Buffered audio is held in memory and released once the transcript is written.
Set this to `true` to keep it, which is useful when comparing models on the
same recording.

Accepts `true`, `1`, `yes`, `on` and their negatives, in any case.

---

## Runtime

### `OPUS_LIBRARY_PATH`

*Default: unset.*

The full path to libopus, for an installation the standard search does not
find.

Stenos already searches the Homebrew prefixes on macOS, the usual system paths
on Linux, and its own payload when running as a frozen executable. This is the
escape hatch for anything else. Confirm the result with `stenos --check`, which
reports whether opus loaded.

---

## Where transcripts go

`OUTPUT_DIR` decides, defaulting to `transcripts/` beside the working
directory. Files are named `stenos-<channel>-<timestamp>.txt` with a `.json`
sidecar beside each one.

The channel name is sanitised on every platform, so a channel called
`voice: general <main>` produces `stenos-voice-general-main-20260801T161443Z.txt`
rather than a path Windows rejects.
