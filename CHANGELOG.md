# Changelog

All notable changes to Stenos are recorded here. The format is based on
Keep a Changelog (https://keepachangelog.com).

Versions have four components, `X.N.V.M`, described in the versioning section
of the [README](README.md). A release covers a whole series: every commit
sharing an `X.N.V` belongs to it, and the tag is cut at the last of them. The
sections below are therefore headed by the series rather than by one version.

## 0.2.0 (unreleased)

The first beta, and the first release that is not a pre-release. It opens the
maintenance line: fixes and upkeep, cut as alphas whenever enough of them
accumulate, while a graphical interface is built alongside. Nothing in the
recording path changes here.

### Fixed

- **The release badge was a picture of the word `none`.** It was a hardcoded
  shields.io value sitting beside a claim, two lines below it, that both
  version badges resolve from what GitHub records rather than from anything
  kept in step by hand. That was true of the pre-release badge and false of
  this one, and publishing a stable release is exactly the moment the
  difference would have shown, to a reader who had already concluded there was
  nothing to install. It is now the same query as its neighbour without
  `include_prereleases`, and a test refuses a hardcoded value in its place.

- **Both install scripts said every release was a pre-release.** That was the
  reason the default path could not resolve a version, and it stops being the
  reason the moment a stable release exists. After that the same message could
  only appear because a request failed, while naming a cause that no longer
  applies. Neither script can tell the two apart, so neither claims to: they
  now report that the newest stable release could not be worked out, and
  suggest `--pre` for a project that has only pre-releases.

  The path itself is unchanged. It has also never run, since `releases/latest`
  answers 404 until a release is neither a draft nor a pre-release, so
  `install.sh` is now exercised end to end against captured payloads for both
  shapes the endpoint returns. The single release the default path reads is not
  the array the `--pre` path reads, and one `sed` parses both.

## 0.1.6 (2026-08-06)

The release about how long a call can be. Recording worked; recording for an
hour did not, and nothing in the code noticed.

### Fixed

- **A recording held the whole call at the rate it arrived.** Discord delivers
  48 kHz stereo signed 16 bit, which is 192,000 bytes for every second of
  speech, summed across speakers. None of it was released until transcription
  finished, which is also the moment the model weights load. An hour of
  conversation held 691 MB, three hours held 2 GB, and the intended host is a
  fanless laptop.

  Whisper reads 16 kHz mono, and the conversion to it already existed; it just
  ran at the end. A segment now drops a channel as its packets arrive, which is
  exact because averaging a pair of samples depends on nothing outside that
  pair, and drops its sample rate once it can no longer grow. Measured end to
  end, a recording holds 32,000 bytes per second of speech instead of 192,000:
  115 MB per hour rather than 691.

  The audio handed to the backend is the same audio, to within half a step of
  16 bit, which is the requantisation and nothing else.

- **One speaker who never paused held the whole call in a single segment.**
  Segments closed on silence alone, so a monologue was unbounded, and the work
  of reducing it grew with the call. A segment now also closes at 30 seconds,
  which is the window a Whisper encoder reads, so a long turn is never longer
  than the context the model has for it and gets a timestamp per part instead
  of one for all of it.

### Added

- **A recording that outgrows its buffer stops itself.** The reduction is a
  constant factor rather than a bound, so `MAX_BUFFER_MB` ends a recording that
  passes it, transcribes what was captured, and says in the channel which
  setting decided it. Zero removes the limit. The default of 1024 is about nine
  hours of speech.

  Everything the stop command did after acknowledging moved into
  `finish_recording`, so a recording that ends itself produces the same
  transcript and the same message as one that was asked to stop, rather than a
  second implementation that drifts from the first.

- **`MAX_SEGMENT`**, for the segment length cap, alongside the two thresholds it
  sits with in `.env.example`.

- **A release is cut by pushing rather than by a person.** Writing a version
  into `.github/release-version` and pushing to `main` tags the commit and
  builds the release. Alpha 0.1.5 sat finished and untagged for a day because
  the only way in was a workflow dispatch, and the fallback of pushing the tag
  by hand returned 403 from the git proxy.

  Because nothing human now stands between the decision and the tag, the
  workflow first waits for every other workflow that ran on that commit and
  refuses unless all of them succeeded. It asks for every one rather than a
  chosen few, which the first attempt did not: that gate named `ci` and
  `compat`, and the release went out with `platforms` red. It gates on workflow
  names rather than on the names of individual check runs, since `lint`,
  `typecheck`, `test (python 3.12)` and the compat matrix all move whenever a
  job or a matrix changes, and a gate naming those would quietly stop gating.

  A cancelled run counts as a refusal: `ci` cancels in progress runs when a
  newer commit lands, so a cancelled one means `main` has moved. Finding no runs
  at all counts as waiting rather than passing, because the push that starts a
  release starts the others too. And a series with no section in this file is
  refused, since `release.yml` would otherwise fall back to generated notes and
  nobody would find out until afterwards.

  What arrives is still a draft. Publishing remains a person's decision.

### Notes

Reducing a segment where it closes was the obvious design and the wrong one.
Timed against the numpy path, which is the one that has to work because scipy
is an optional import that no dependency declares, a 30 second segment takes
74 ms and a 60 second segment 146 ms. Packets arrive every 20 ms, so doing it
on py-cord's router thread would stall delivery every time somebody stopped
speaking. A worker drains a queue of closed segments instead; producing 30
seconds of audio takes 30 seconds and reducing it takes 74 ms, so it cannot
fall behind.

## 0.1.5 (2026-08-02)

Three more defects in py-cord's receive path, and the first pass at keeping
text out of the transcript that nobody said. As with 0.1.4, every item came
from reading or running the receive path rather than from the test suite.

### Fixed

- **The audio was decrypted a second time after it had been decoded.**
  `PacketDecoder._decode_packet` turns the payload into linear audio and then
  hands that audio to `dave.decrypt` whenever the session reports the speaker as
  passthrough. The payload was decrypted in `decrypt_rtp` before it was ever
  decoded, so this is a second decryption of something that is no longer
  ciphertext. It either corrupts the audio or raises, and it raises inside the
  router thread, which ends the recording.

  Passthrough is not the rare state it sounds like. py-cord turns it on from
  three places, on a DAVE downgrade, a session reset, and a transition recovery,
  all of which follow somebody joining or leaving the channel. No recording made
  so far has hit it, because every one of them was a single speaker on a stable
  channel, which is the one shape that avoids it.

- **Packets held in the jitter buffer were discarded rather than delivered.**
  `_get_next_packet` flushes the whole buffer the first time the next packet is
  out of sequence, returns the earliest of them, and drops the rest. The flush
  has already moved the buffer's idea of what has been sent past all of them, so
  what is dropped cannot arrive again. py-cord logs a warning naming the count
  as it happens; a recent recording lost five packets, about a tenth of a
  second, in its first second.

  Because the buffer is polled with no timeout, this fires at the first sign of
  a gap rather than after any wait, and a gap is most likely where a stream
  starts. The packets are held and handed out in order instead. The readiness
  flag counts them too, since it asks the buffer alone whether more is coming
  and would otherwise stop the router polling a decoder that still has audio.

- **A decode failure of any other kind still ended the recording.** The
  tolerance added in 0.1.4 caught the error opus raises and nothing else, so an
  exception from the encryption layer went straight through it and killed the
  router thread. What ends a recording is the thread dying, and the thread does
  not care which exception killed it.

### Added

- **Text the model invented is kept out of the transcript.** Whisper does not
  decline to transcribe. Given audio with nothing in it, it returns a confident
  sentence; given a fragment, it can repeat one phrase until the segment runs
  out. Both were produced by real calls: 250 repetitions of a single word over
  two and a half seconds, and a stock courtesy over opus silence.

  Silence is judged from the audio rather than from a list of known phrases,
  which would be fragile and would only work in English. Repetition is judged
  from the shape of the text, because audio can legitimately be somebody
  repeating themselves. The thresholds are set against the two captured samples,
  which sit at ratios of 0.004 and 0.077 distinct words to total, against 0.6
  and above for ordinary speech from the same calls.

  A suppressed line is dropped from the transcript and kept in the sidecar with
  the reason, so the decision can be checked rather than taken on trust.

- **`--check` reports the two new repairs**, on their own lines beside the
  existing one for the decryption.

## 0.1.4 (2026-08-02)

The release in which a recording first contained the call. Everything below was
found by running the bot against a real voice channel; none of it was visible
from the test suite, which was passing throughout.

### Fixed

- **py-cord removed the RTP header extension twice, and no audio survived it.**
  On a call carrying encryption, which since March 2026 is every call, the
  transport decryption removes the extension using a constant that is right only
  when the sender wrote exactly two extension words, and `decrypt_rtp` then
  applies the offset a second time to the opus frame the session has already
  returned.

  A packet carrying two extension words reaches the decoder missing the first
  eight bytes of its audio and is rejected as a corrupted stream. Every other
  size loses the wrong bytes before the session sees them, fails to decrypt, and
  is replaced with opus silence. There is no extension size at which the audio
  survives, which is why a recording made against a stock 2.8.1 is silence
  interrupted by decode failures. A live recording that produced 927 decode
  failures in forty seconds now produces none.

  The offset is applied once, before the session sees the payload, and is probed
  at four different extension sizes rather than at the one Discord happens to
  write, since a probe using only that would call the broken decryptor sound.

- **Segments were timed by delivery rather than by the audio.** py-cord 2.8
  drains a jitter buffer into the sink and covers gaps with synthesised packets,
  so a burst delivers several seconds of speech in a fraction of a second and
  arrival time stops tracking speech. Segments timed that way ran longer than
  the span they were received in and overlapped the segments after them.

  Every packet carries an RTP timestamp counting samples, which advances with
  the audio whatever the delivery does. Segments are now placed and split on
  that, measured from each participant's first packet because the count starts
  at a value unrelated between one participant and the next. Arrival still ties
  one participant's stream to another's, and takes over again if a stream
  restarts on a new count.

- **The sink could not be registered at all.** py-cord 2.8 rewrote the receive
  path and left every one of its own sinks behind, including `WaveSink`. The
  router reads three members that no sink in that release defines, so starting a
  recording raised before any audio moved. It also stopped calling `init` on the
  sink, so the first packet killed the router thread on an assertion.

- **A recording that captured nothing reported the wrong length.** The duration
  spanned the first arrival to the last, but the audio a packet carries extends
  past the moment it arrived, and with a buffer in front of the sink it can
  extend well past it.

- **One malformed frame discarded the rest of the call.** py-cord let an opus
  decode failure out of the router thread, which stopped the thread, which
  stopped the recording. A frame that will not decode is now skipped and
  counted, and `/record stop` says how many were lost.

- **`/record start` failed with an unknown interaction.** Connecting to voice
  takes about five seconds and an interaction token expires after three, so the
  reply always arrived too late. The command is now acknowledged before the
  connection is attempted, and a failure to start recording is reported rather
  than left as a silent timeout.

- **The standalone executables had no certificate list.** A frozen `ssl` looks
  where the build machine kept its certificates, which is nowhere on the
  machine that runs the executable, so logging in failed at the TLS handshake
  with an error about a missing local issuer. `certifi` is now a dependency and
  its bundle is selected before connecting; a build whose certificates resolve
  outside the bundle fails in continuous integration.

- **The Apple Silicon extra could not be installed.** With numpy uncapped the
  resolver backtracked to a 2021 numba that supports no Python this project
  runs on, so `uv sync --extra mlx` failed to build. Nothing in continuous
  integration had ever installed that extra; the compatibility workflow now
  does, on every supported Python.

- **A working recording ended with a traceback, and said it could not work.**
  py-cord's router stops a recording its own caller stopped a moment earlier
  and lets the resulting exception out of a thread with nothing to catch it. It
  also warns twice a recording that reception is broken, which stopped being
  true once the decryption was repaired, and logs an ordinary RTCP sender
  report as an unexpected packet several times a minute.

### Added

- **The sink is checked against py-cord's own reader.** A test now constructs
  the real `AudioReader` around it, so a change to what the router expects
  fails in continuous integration rather than on a voice channel. It caught a
  real defect the first time it ran.

- **`--check` reports the receive path.** Which py-cord is installed, whether
  its sink contract needed adapting, and which repairs were applied to it.

## 0.1.3 (2026-08-01)

### Added

- **A recording that captured nothing now says so, and says why.** An empty
  transcript reads exactly like a call in which nobody spoke, and the two have
  very different causes. `/record stop` now inspects the recording before
  loading a transcription backend and reports which of the two happened rather
  than writing out a transcript with no lines.

  Two failure states are recognised. A recording that received no packets at
  all, and a recording that received packets carrying nothing but silence. The
  second is the more misleading: when a packet cannot be decrypted, py-cord
  substitutes an opus silence frame instead of reporting the failure, so the
  recording ends up the right length and entirely empty.

- **The end-to-end encryption state is reported rather than guessed at.**
  `--check` reports whether the encryption library is present and which
  protocol version it speaks. `/record status` reports the negotiated session
  state, but only while no audio has arrived, since packets actually arriving
  are better evidence than anything the connection reports.

  Discord enforces DAVE on non-Stage voice calls, and py-cord yields received
  audio only once a session exists and its handshake has completed. Before
  that, every packet is discarded. Both that and a failed decrypt are logged
  below the default level, so neither was previously visible.

- **Audio that py-cord discards is put back.** Version 2.8.1 performs the
  transport decryption into a local, then returns a field that only its
  encryption branch ever assigns, so a call carrying no encryption records
  nothing at all. Stenos restores the discarded payload.

  The repair is confined to the one state in which no encryption can have been
  applied, a connection with no session, so there is no question of handing
  still-encrypted bytes to the decoder. Every other state keeps py-cord's
  behaviour exactly, including the silence substitution for a packet that fails
  to decrypt.

  Whether to apply it is decided by running the decryptor rather than by
  comparing version numbers, so a py-cord that has fixed this is left alone.
  `--check` reports the decision, and the test suite fails with a message asking
  for the module to be deleted once the defect is gone.

### Fixed

- **The standalone executables could join no voice channel.** PyNaCl's compiled
  module imports `_cffi_backend` from C rather than through any Python
  statement, so the freezer never saw it and left it out of the bundle. Without
  it PyNaCl does not import and py-cord reports its voice dependencies as
  missing, which meant every executable released so far could start, report
  `opus loaded True`, and then fail the moment it was asked to connect.

  This affects the `v0.1.2.0` executables, which should not be used. The
  release smoke test now checks voice support alongside opus and the
  transcription backend, so a freeze that loses a hidden import is caught by
  the job that built it.

- **The documented limitation was wrong.** The README claimed py-cord had no
  receive-side support for DAVE and that recording an encrypted call was
  therefore not possible. Reading 2.8.1 shows it decrypts received audio, and
  that `davey` is a dependency of `py-cord[voice]` carried inside the
  standalone executables. Recording an encrypted call is supported; what was
  missing was any report when it fails.

### Changed

- **A release now covers an alpha series rather than a single commit.** Every
  commit sharing an `X.N.V` belongs to one release, the tag is cut at the last
  of them, and the release is titled by the kind of bump that opened the
  series, as in `Alpha 0.1.3`. Cutting a second release for a series that
  already has one is refused.

## 0.1.2 (2026-08-01)

### Added

- **Standalone executables for Linux, macOS, and Windows.** A single file that
  runs without Python, uv, or any system library installed. Download it from
  the release, or install it with one command:

  ```
  curl -fsSL https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.sh | sh
  ```

  The executable carries its own copy of libopus, so voice receive works on a
  machine that has never had it installed. This was verified by removing
  libopus from the build machine entirely and confirming the executable still
  loaded it. It also carries the faster-whisper backend, because an executable
  that can join a call but not transcribe it would have no purpose.

  Model weights are not bundled. They are downloaded on first use and cached,
  which keeps the executable the same size whichever model is configured.

  Apple Silicon is a deliberate exception in one respect: the executable uses
  faster-whisper rather than mlx, so it is slower there than a source install.
  Anyone who wants the mlx acceleration should install from source, which the
  README covers.

  A frozen build records which backend it carries and resolves an automatic
  selection to that, rather than to what the platform would otherwise prefer.
  Without it an Apple Silicon executable asked for mlx, which it does not
  bundle, and refused to transcribe anything. An explicit `WHISPER_BACKEND`
  still wins.

- **`--check` reports whether the backend can actually be used.** It previously
  reported which backend name resolved, which proved only that the name was
  known. It now attempts the import, without constructing a model so that no
  weights are downloaded, and reports the result:

  ```
  $ stenos --check
  stenos 0.1.2.0 (frozen executable)
  backend          auto resolves to faster-whisper
  backend usable   True (installed)
  opus loaded      True
  ```

  Learning that the backend is unusable before a call beats learning it after
  an hour of recording.

- **`python -m stenos`** starts the bot, alongside the `stenos` command.

- **Installers.** `scripts/install.sh` for Linux and macOS, and
  `scripts/install.ps1` for Windows. Both check the download against the
  published `SHA256SUMS` and refuse to install if it does not match. An
  executable installed quietly from a corrupted download is worse than no
  executable.

### Changed

- **Releases are drafts, and their notes come from this file.** The notes were
  generated from commit subjects, which produced an accurate but unreadable
  wall of forty lines. A release now carries the section written for it here,
  and is created as a draft so it can be read before anyone can download it.

- **Release assets.** Each platform gets a `.zip` holding the executable, the
  README, the licence, and `.env.example`. A single `SHA256SUMS` covers every
  asset, and both installers are attached alongside.

  A release carries nothing else. No wheel and no source distribution: Stenos
  is a program to run rather than a library to import, so those would be
  noise beside the executables. Install from source with `uv sync` if that is
  what you want, which the README covers.

## 0.1.1 (2026-08-01)

### Fixed

- **libopus now loads on Apple Silicon.** py-cord bundles an opus binary for
  Windows only and resolves the library through `ctypes.util.find_library`
  everywhere else. That search does not cover the Homebrew prefix, so
  `brew install opus` was not by itself enough and the bot would connect to a
  voice channel and record nothing. Stenos now searches `/opt/homebrew/lib` and
  `/usr/local/lib` itself, and `OPUS_LIBRARY_PATH` overrides the search for an
  installation somewhere else.

  This was the primary deployment target, and the documented setup did not work
  on it.

- **A failed transcription is reported instead of hanging.** A missing backend
  or a write error escaped the `/record stop` handler and left the caller
  waiting on a deferred response that never arrived, which is indistinguishable
  from a transcription that is merely slow. Both cases now post what went
  wrong.

- **The type checker follows the running interpreter.** numpy ships different
  stubs per Python version, and its 3.12 stubs use PEP 695 type statements that
  mypy rejects when pinned to 3.11. The pin passed locally and failed in
  continuous integration.

### Changed

- **The macOS x86_64 job was dropped from the platform matrix.** GitHub no
  longer allocates runners for that architecture. The label still resolves, so
  the job queued rather than failing and held the whole run open until the
  queue limit expired it, which meant the workflow could never report green.
  The platform remains supported; it is no longer verified automatically.

## 0.1.0 (2026-08-01)

### Added

- **First feature-complete build.** A Discord bot that records every voice
  participant separately and produces one timestamped, speaker-attributed
  transcript, transcribed locally.

  - A sink that timestamps every packet on arrival and opens a new segment
    after a silent gap, so a participant who joins late lands at the right
    offset rather than at the start. The sink bundled with py-cord
    concatenates each user's packets and loses that position.
  - In-process conversion from 48 kHz stereo to the 16 kHz mono float32 that
    Whisper expects, with a windowed sinc low-pass before decimation so content
    above the new Nyquist frequency cannot alias into the speech band.
  - mlx-whisper on Apple Silicon and faster-whisper elsewhere, each loading its
    model once and reusing it across every segment.
  - Merged output as a `.txt` transcript and a `.json` sidecar carrying the raw
    segments, timings, and user identifiers.
  - `/record start`, `/record stop`, and `/record status`, with start and stop
    announced in the text channel unconditionally, for consent and so a
    connection dropped mid-call is visible.
  - A compatibility suite covering opus availability, output encoding, filename
    sanitisation, line endings, path construction, backend selection, and the
    whole offline pipeline, run on Linux, macOS, and Windows.
