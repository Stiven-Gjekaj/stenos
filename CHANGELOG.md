# Changelog

All notable changes to Stenos are recorded here. The format is based on
Keep a Changelog (https://keepachangelog.com).

Versions have four components, `X.N.V.M`, described in the versioning section
of the [README](README.md). A release covers a whole series: every commit
sharing an `X.N.V` belongs to it, and the tag is cut at the last of them. The
sections below are therefore headed by the series rather than by one version.

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
