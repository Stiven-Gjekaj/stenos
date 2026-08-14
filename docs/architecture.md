# Architecture

How a call becomes a transcript, and why each stage is built the way it is.

```
Discord voice  ->  sink.py  ->  audio.py  ->  transcribe.py  ->  transcript.py
   packets        segments      16 kHz mono      text            merged file
                     |______________|         |
                   the conversion runs as     spill.py, only once a call
                   the packets arrive,        holds more than it may keep
                   not after                  in memory
```

Every stage after the sink is pure: it takes values and returns values, touches
no network, and is exercised offline by the test suite. Only `sink.py` and the
command handlers in `bot.py` know that Discord exists.

---

## 1. Receiving, `sink.py`

Discord sends a separate stream per participant and decodes it to 48 kHz
stereo signed 16 bit audio. Attribution is therefore free: the speaker is
whichever stream the packet arrived on. Position in the call is not.

`TimestampedSink` subclasses the py-cord sink and overrides `write`. The
question it answers is which timeline a packet belongs on, and arrival time is
the obvious answer and the wrong one: py-cord drains a jitter buffer into the
sink and synthesises packets to cover gaps, so a burst delivers several seconds
of audio in a fraction of a second. Segments timed that way run longer than the
span they arrived in and overlap the ones after them.

Every packet carries its own answer. The RTP timestamp counts samples at the
rate the audio decodes to, so it advances with the audio rather than with
delivery and is unaffected by any buffering in front of the sink. It is used
wherever it is present, measured from each participant's first packet, because
the count starts somewhere unrelated between one participant and the next.
Arrival still fixes where a participant's stream begins relative to the
recording, since that is the only clock they all share, and settles a
disagreement larger than `MAX_CLOCK_DISAGREEMENT`, which means the stream
restarted rather than merely buffered.

A segment closes when the media clock shows a gap longer than `SEGMENT_GAP`,
and also at `MAX_SEGMENT`, without which one speaker who never pauses holds the
whole call in a single segment.

Two consequences worth stating:

- **No voice activity detector is needed.** A Discord client transmits only
  while someone speaks, so the gaps between packets already are the silence.
- **Late joiners land correctly.** The sink bundled with py-cord concatenates
  each user's packets into one buffer, which discards when they spoke. A
  participant who joins ten minutes in then appears at the start of the merged
  transcript. Placing each packet on the clock it carries makes the merge
  correct by construction rather than by correction.

A closed segment is handed to a worker thread that reduces it. Reducing thirty
seconds of audio measures about seventy milliseconds and packets arrive every
twenty, so doing it where the segment closes would stall py-cord's router
thread every time somebody stopped speaking.

The clock is a constructor argument. Tests drive segmentation with scripted
timestamps and never sleep.

---

## 2. Converting, `audio.py`

Whisper consumes 16 kHz mono float32. The conversion is exactly 3:1, and runs
in process with numpy: a one-hour call yields several hundred segments, and
spawning a resampler per segment would dominate the total.

It runs in two parts, as early as each can. Holding a whole call at the rate it
arrives costs 192,000 bytes for every second of speech, and none of it can be
released until transcription finishes, which is also when the model weights
load. So `downmix` drops a channel as each packet arrives, reinterpreting the
buffer as little endian int16 regardless of host byte order and discarding a
trailing partial frame; and `downsample` drops the sample rate once a segment
can no longer grow. What is held falls to a sixth, 32,000 bytes per second of
speech rather than 192,000.

Dropping a channel per packet is exact, because averaging a pair of samples
depends on nothing outside that pair, so there is no state to carry across a
packet boundary and no edge to get wrong. Dropping the rate is not, which is
why it waits until the segment is complete. `to_float32` scales what is held to
`[-1, 1)` when a model reads it.

`resample` low-passes with a Hamming windowed sinc before decimating. Without
that filter a 20 kHz component would fold to 4 kHz and land in the middle of
the speech band. The filter is measured in the test suite: a 440 Hz tone
survives with its amplitude intact while a 20 kHz tone is attenuated by about
63 dB.

`prepare_segments` discards anything shorter than `MIN_SEGMENT`. Whisper
transcribes near-silence confidently, inventing lines like "Thank you." from
breath noise, so brief fragments are dropped before the model sees them rather
than filtered out of the transcript afterwards.

scipy is used when it happens to be installed and is not a dependency. The
numpy path is the one that normally runs.

---

## 3. Transcribing, `transcribe.py`

`TranscriptionBackend` is a protocol with one method. Three implementations
satisfy it:

| Backend | Where it runs | Notes |
| --- | --- | --- |
| `MLXBackend` | Apple Silicon | mlx-whisper caches the model against the repository path, so a stable path keeps the weights resident |
| `FasterWhisperBackend` | CUDA and CPU | Constructs the model once and holds it on the instance |
| `MockBackend` | Everywhere | Deterministic. Ships with the package so the pipeline can be exercised against an installed wheel with no weights present |

The model is loaded once and reused across every segment. Shelling out to a
separate binary per segment would make process start and model load dominate
the runtime of a call with hundreds of short clips.

Model repositories are mapped explicitly rather than derived from the model
name, because the upstream names are not uniformly suffixed:
`whisper-small-mlx` but `whisper-medium-mlx-fp32`.

`backend_status` attempts only the import, never the model construction, so a
diagnostic never downloads weights.

A backend returns text, and may also say how sure it was. `recognise` asks for
the richer answer and falls back to the plain one, so the protocol stays the
smaller of the two: every backend can transcribe, and only some can report a no
speech probability and a token confidence. faster-whisper does, mlx-whisper
does not through this interface, and Apple Silicon is the primary target, so
the measurement that stands in where a backend cannot say is not a leftover.

Those two numbers are what decides whether a line was invented, when they are
there. Whisper's decoder uses a no speech probability above 0.6 together with
an average log probability below -1.0 to call a segment silence, and so does
this, on the same pair of thresholds. Both have to hold: an unsure
transcription of real speech is somebody mumbling, and a confident
transcription of quiet speech is somebody speaking quietly.

---

## 4. Merging and writing, `transcript.py`

Segments from every speaker are flattened to `(start, user_id, text)`, sorted
by offset, and rendered:

```
[00:04:12] Alpha: so about the asset pipeline
[00:04:19] Bravo: which part broke
```

Names come from a cache populated while recording, because a participant may
disconnect before the call ends, after which the guild no longer resolves them.

Three portability decisions live here, each covered by the compatibility suite:

- Filenames are sanitised on **every** platform, not only on Windows, so a
  transcript recorded on Linux can be copied to Windows unchanged. A reserved
  device name is escaped by prefix rather than suffix, because Windows resolves
  the device from the component before the first dot: `NUL.txt` is still the
  NUL device.
- Timestamps use ISO 8601 **basic** form. The extended form embeds colons,
  which are illegal in Windows filenames.
- Files are opened with an explicit encoding and `newline="\n"`. Windows
  otherwise writes the system code page and translates line endings, so the
  same call would produce different bytes on different hosts.

`KEEP_AUDIO` adds a WAV per participant beside the transcript, through
`write_speaker_wav`, laid out on the call's timeline with silence in the gaps
so the sidecar's offsets index into it. It takes the transcript's stem rather
than working one out, since a stem already taken carries a counter and the two
would otherwise diverge.

**A recording that fits in memory writes nothing while it runs.** Every file
such a call produces is created after it ends. The transcript and the sidecar
are one write each; the audio is streamed a segment at a time, so an hour of it
never has to exist in memory twice. A call that captured nothing writes nothing
at all. Measured on an hour with four speakers, that is about 220 KB in two
files, or 461 MB in six when the audio is kept, since a participant's file
spans the whole call whether they spoke through it or not.

That property is what an unattended host on a small disk is given, and it holds
only while the memory holds. Past `MAX_BUFFER_MB` the recording continues on
disk instead of ending, which is the subject of the next section. Nothing is
created until that happens, so a host with the memory for its calls never finds
out the machinery is there.

---

## 6. Outgrowing memory, `spill.py`

A reduced segment costs 32,000 bytes for every second of speech and stays
resident until transcription, so a call holds about 115 MB per hour of speech.
On a server that is nothing. On a host with a gigabyte it is the whole machine,
and until `0.2.3` crossing `MAX_BUFFER_MB` ended the call, which on that host
is most of a meeting.

Past the ceiling each segment moves to disk as it closes and the memory is
released, so what is resident falls to the segments still open, at most
`MAX_SEGMENT` per speaker. `MAX_DISK_MB` is what ends a recording now, counting
both halves.

Three things make the change smaller than it looks:

- **Every reader already went through `Segment.snapshot`.** Spilling is one
  more thing that answers there, so transcription, the WAV writer and the
  sidecar cannot tell where the audio lives.
- **The reducer already visits every segment that can no longer grow.** That is
  where the spill happens, on the same worker and for the same reason: it is
  off py-cord's router thread.
- **Only settled segments move.** `extend` appends to the buffer a spill
  empties, so moving an open segment would write out what it holds and then
  collect the rest of that speaker's sentence into a buffer nothing reads.

Entering the spilling state is one way. A recording that crossed the mark will
cross it again the moment anything is held back, and alternating would leave
half a call in each place for no gain.

### What is on disk

One append-only file of samples per participant, plus a manifest of one JSON
object per line, in a `.partial` directory beside the transcripts. Both choices
are about a process that does not survive the call:

- **Samples are written before the line describing them.** The only tear a
  crash can leave is bytes nobody accounts for, which recovery discards. The
  reverse would be a record pointing past the end of a file, which cannot be
  told from corruption.
- **A line per record rather than one document.** Appending to JSON means
  rewriting it, and a call with hundreds of segments would rewrite a growing
  file hundreds of times. A torn final line is dropped and costs one segment.

Writes are flushed and not synced. A killed process loses nothing; a host that
loses power loses whatever the kernel had not written yet. An fsync per segment
on the hardware this exists for would cost more than the ceiling it lifts.

The directory is removed once the transcript is written. One still there means
the process did not get that far, and `stenos --recover` reads it back through
the same pipeline, producing the file the call would have produced. That is
what the manifest carries the channel, the start time and the display names
for: none of it can be recovered from the samples, and a participant who has
since left the guild cannot be resolved again.

Alongside the transcript body, `write_sidecar` produces a JSON sidecar
(`build_sidecar`) for downstream tooling with the following schema:

- `version`: integer schema version, currently 1
- `channel`: recorded channel name
- `recorded_at`: ISO 8601 UTC timestamp
- `duration`: call duration in seconds rounded to three decimal places
- `backend`: transcription backend used
- `model`: model name
- `speakers`: map of string user IDs to display names
- `segments`: list of segment dictionaries sorted by offset, each containing:
  - `user_id`: integer participant ID
  - `speaker`: display name or fallback
  - `start`: start offset in seconds rounded to three decimal places
  - `duration`: segment duration in seconds rounded to three decimal places
  - `text`: transcribed text
  - `suppressed`: optional string reason, present only when the segment was suppressed
  - `no_speech`: optional probability the model gave that the segment holds no
    speech, present only when the backend reports one
  - `logprob`: optional average token log probability, present on the same
    terms

---

## 7. Commands, `bot.py`

`run_pipeline` is a plain function taking a sink, a name cache, and a config.
It is deliberately separate from the command handlers, which is what lets the
whole path run offline in tests and on a worker thread at runtime.

Transcription is dispatched with `asyncio.to_thread`. Blocking the event loop
for minutes would stop the gateway heartbeat and drop the connection.

`/record start` and `/record stop` post visible, non-ephemeral messages. That is
deliberate and is covered in the consent section of the README: there is no
silent recording mode. The start message gates the recording rather than
accompanying it, so a channel the bot cannot post to is a channel it does not
record.

A recording that received no packets is reported as such rather than presented
as an empty transcript, and a failed transcription is reported rather than left
as a deferred response that never resolves.

Three things end a recording, and only one of them is the stop command. The
buffer ceiling and a lost voice connection end it too, so `finish_recording`
holds everything the stop command did after acknowledging: a recording that
ends itself produces the same transcript and the same message as one that was
asked to stop, differing only in the sentence that opens it. Having no
interaction to answer, it posts to the channel the recording was started from.

The two automatic reasons are found differently. A ceiling is measured, so a
watchdog loop measures it, every `BUFFER_CHECK_SECONDS`.

A lost connection is harder, because the event that reports one is ambiguous
and the worst case sends no event at all. Losing the network loses the gateway
with it, so nothing arrives to say so; and when something does arrive, py-cord
asks Discord to remove the bot from the channel before every reconnect, so a
recovery and a kick look identical. The same watchdog therefore reads the voice
connection's own state and waits `DISCONNECT_GRACE` before believing it, since
a reconnect reads as disconnected for the whole of its attempt.
`on_voice_state_update` only starts that clock early.

The exception is a move to a different channel, which a reconnect never
produces because it rejoins the channel it left. That ends the recording at
once.

---

## The graphical interface

Designed but not built. What it is written in, what it shows, and why it
listens on no port are settled in [interface.md](interface.md). Everything it
drives is already a function taking values and returning values, which is the
reason the seams above were built the way they were.

---

## Adding a backend

1. Implement `transcribe(audio, language) -> str` and a `name` attribute.
2. Add an import helper that raises `BackendUnavailableError` with an install
   instruction, following `_load_mlx_whisper`.
3. Extend `resolve_backend` in `config.py` if it should be selected
   automatically on some platform.
4. Add the dependency as an optional extra in `pyproject.toml`, never as a core
   one. Continuous integration must never pull model weights.
