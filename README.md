<div align="center">

<img src="assets/logo.png" alt="Stenos" width="180"/>

### A Discord bot that records every voice participant separately and transcribes the call locally

_One timestamped, speaker-attributed transcript. No audio leaves the machine_

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11%20to%203.13-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11 to 3.13"/>
  <img src="https://img.shields.io/badge/dependencies-3_direct-007ec6?style=for-the-badge" alt="Three direct runtime dependencies"/>
  <img src="https://img.shields.io/badge/tests-392_passing-427819?style=for-the-badge" alt="392 tests passing"/>
</p>

<p align="center">
  <a href="https://github.com/Stiven-Gjekaj/stenos/actions/workflows/ci.yml"><img src="https://github.com/Stiven-Gjekaj/stenos/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/Stiven-Gjekaj/stenos/actions/workflows/compat.yml"><img src="https://github.com/Stiven-Gjekaj/stenos/actions/workflows/compat.yml/badge.svg" alt="Cross-platform behaviour"/></a>
  <a href="https://github.com/Stiven-Gjekaj/stenos/releases"><img src="https://img.shields.io/github/v/release/Stiven-Gjekaj/stenos?include_prereleases&style=flat-square&color=orange&label=pre-release" alt="The latest pre-release"/></a>
  <img src="https://img.shields.io/badge/release-none-lightgrey?style=flat-square" alt="No stable release yet"/>
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT License"/>
</p>

<p align="center">
  <a href="#quick-start"><b>Quick Start</b></a> |
  <a href="#features"><b>Features</b></a> |
  <a href="#supported-platforms"><b>Platforms</b></a> |
  <a href="#documentation"><b>Documentation</b></a> |
  <a href="#consent-and-legal-note"><b>Consent</b></a>
</p>

</div>

---

## Sample output

```
[00:00:04] Alpha: right, so about the asset pipeline
[00:00:09] Bravo: which part broke
[00:00:12] Alpha: the exporter, it stopped writing normals on anything with a mirror modifier
[00:00:21] Bravo: since when
[00:00:23] Alpha: since we bumped blender, i think
[00:01:02] Charlie: i can reproduce it on my machine if you want a second data point
[00:01:09] Alpha: please, and check whether it also drops tangents
[00:01:17] Charlie: will do
[00:04:12] Bravo: ok so i found it, the exporter reads the evaluated mesh before the modifier stack runs
[00:04:19] Alpha: that would explain the mirror case exactly
```

Alongside it, a `.json` sidecar carries the raw segments, their offsets and
durations, and the user identifiers, for downstream tooling.

---

## Why this exists

**Speaker and timestamp alignment is correct.** Discord delivers a separate
audio stream per participant, which makes attribution free, but the sink
bundled with py-cord concatenates each user's received packets into one buffer.
That discards the point in the call at which each utterance happened, so a
participant who joins late, or who simply stays quiet for the first ten
minutes, is placed at the wrong offset in the merged output. Most comparable
projects inherit this drift. Stenos timestamps every packet on arrival and
opens a new segment whenever a speaker falls silent past a threshold, so the
merge is correct by construction rather than by correction.

**Inference is local.** Audio is buffered in memory during the call and
transcribed after it ends. Nothing is uploaded, and no API key is involved.

**Apple Silicon is the primary target, without CUDA.** On an M-series machine
Stenos uses `mlx-whisper`, which runs on the GPU through Metal. Transcription is
deliberately deferred until the call ends, because the intended host is a
fanless laptop that will thermally throttle under sustained inference.

---

## Features

<table>
<tr>
<td width="50%" valign="top">

### Recording

- A separate stream per participant, so attribution needs no diarisation
- Segments placed on the clock the packets carry, so buffering cannot skew them
- Segments split on transmission gaps, with no voice activity detector
- Display names cached as people join, so someone who leaves early is still named
- Start and stop announced in the text channel, always
- A recording that captured nothing says which of the two reasons applied
- Two py-cord defects that lose received audio detected and repaired

</td>
<td width="50%" valign="top">

### Transcription and output

- mlx-whisper on Apple Silicon, faster-whisper on CUDA and CPU
- The model loaded once and reused across every segment
- In-process resampling with an anti-aliasing filter, no subprocess per segment
- Near-silence discarded before the model can hallucinate a line from it
- A `.txt` transcript and a `.json` sidecar with raw timings
- UTF-8 with line feed endings and sanitised filenames on every platform

</td>
</tr>
</table>

---

## Quick Start

### Standalone executable

No Python, no uv, nothing else installed. The script checks the download
against the published checksum and refuses if it does not match:

```sh
curl -fsSL https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.sh | sh
```

On Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.ps1 | iex
```

Both install the newest **stable** release. Every release so far is an alpha,
published as a pre-release, so until the first beta those commands will report
that there is no stable release and tell you to ask for a pre-release instead:

```sh
curl -fsSL https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.sh | sh -s -- --pre
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Stiven-Gjekaj/stenos/main/scripts/install.ps1))) -Pre
```

Pass a version instead of `--pre` to pin one exactly, and run `install.sh
--help` for the full set of options.

Prebuilt executables cover Linux on x86-64 and arm64, macOS on Apple Silicon,
and Windows on x86-64. Each carries its own copy of libopus and the
faster-whisper backend. Model weights are downloaded on first use and cached.

### From source

A source install is what you want on Apple Silicon, since it can use the mlx
backend that the executable does not carry. Install
[uv](https://docs.astral.sh/uv/), clone the repository, then run the one
command for your platform:

| Platform | Command |
| --- | --- |
| macOS, Apple Silicon | `brew install opus && uv sync --extra mlx` |
| macOS, Intel | `brew install opus && uv sync --extra cuda` |
| Linux | `sudo apt install -y libopus0 libsodium23 && uv sync --extra cuda` |
| Windows | `uv sync --extra cuda` |

The `cuda` extra installs `faster-whisper`, which runs on the CPU when no
compatible GPU is present. Neither backend is installed by a plain `uv sync`,
which keeps continuous integration free of model weights.

### Configure and run

```sh
cp .env.example .env      # then set DISCORD_TOKEN
stenos --check            # report the resolved backend and whether opus loaded
stenos                    # start the bot
```

`--check` is the first thing to inspect when voice receive misbehaves. Without
connecting to Discord it reports whether libopus loaded, whether the
transcription backend can actually be imported, whether the end-to-end
encryption library is present, and whether the py-cord receive defects described
under [known limitations](#known-limitations) were found and repaired.

---

## Commands

| Command | Does |
| --- | --- |
| `/record start` | Joins your voice channel, begins recording, and announces it in the text channel |
| `/record status` | Reports elapsed time, how many participants have spoken, and the encryption state while no audio has arrived |
| `/record stop` | Stops, transcribes, and posts the transcript with a segment and speaker count |

Transcripts are written to `transcripts/stenos-<channel>-<timestamp>.txt`, and
attached to the completion message when they fit inside the server's upload
limit.

### Creating the bot

1. Open the [Discord developer portal](https://discord.com/developers/applications)
   and create an application.
2. Under **Bot**, create a bot and copy its token.
3. Select the scopes `bot` and `applications.commands`.
4. Select the permissions **View Channel**, **Connect**, **Send Messages**, and
   **Attach Files**. The permissions integer is `1084416`.
5. Invite the bot with the generated URL.

No privileged intents are required. Stenos uses the voice state intent, which is
enabled by default, and never reads message content.

---

## Supported platforms

| Operating system | Architecture | Backend | Prerequisite | Executable |
| --- | --- | --- | --- | --- |
| macOS 14+ | arm64 (Apple Silicon) | `mlx-whisper` | `opus` via Homebrew | Yes |
| macOS 13+ | x86_64 | `faster-whisper` (CPU) | `opus` via Homebrew | No |
| Linux | x86_64, aarch64 | `faster-whisper` (CUDA or CPU) | `libopus0`, `libsodium23` | Yes |
| Windows 10+ | x86_64 | `faster-whisper` (CUDA or CPU) | None | Yes |

Python 3.11, 3.12, and 3.13 are supported.

Intel macOS has no prebuilt executable and is not verified by continuous
integration, because GitHub has withdrawn its Intel runners and a freezer
cannot cross-build for another architecture. Installing from source there still
works.

Voice receive requires libopus. py-cord bundles a binary for Windows only; on
macOS and Linux it resolves the library through `ctypes.util.find_library`, so
the system package is required on both. That search does not cover the Homebrew
prefix on Apple Silicon, so Stenos looks in `/opt/homebrew/lib` and
`/usr/local/lib` itself, and `brew install opus` is enough. Set
`OPUS_LIBRARY_PATH` for an installation anywhere else.

---

## Operational notes

Transcription runs after the call ends and can take several minutes. It runs on
a worker thread so the gateway heartbeat continues, but the process must stay
alive and connected for the whole call.

**macOS.** Run under `caffeinate` so the system does not idle sleep:

```sh
caffeinate -is stenos
```

Keep the machine on mains power. `caffeinate` does not prevent sleep when the
lid is closed on battery, and a clamshell sleep drops the voice connection
mid-call. Leave the lid open, or attach an external display.

**Linux.** Systemd suspends an idle desktop session. For an unattended host:

```sh
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Run under a systemd user service with `Restart=on-failure`, and set
`HandleLidSwitch=ignore` in `/etc/systemd/logind.conf`.

**Windows.** Never sleep while plugged in, and set the lid action to **Do
nothing** under Power Options:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

**Every platform.** The realistic failure is the host losing network mid-call,
which ends the recording silently. Stenos posts start and stop messages for
exactly this reason, so a missing stop message is the signal that something
went wrong.

---

## Consent and legal note

Recording law varies by jurisdiction. Some require the consent of every
participant, some require only one party, and some distinguish private
conversations from other settings. Determining what applies to your recording is
your responsibility.

Stenos announces itself by design. `/record start` and `/record stop` post
visible, non-ephemeral messages to the text channel, and Discord itself shows
the bot as connected to the voice channel. There is no silent recording mode and
none will be added.

---

## Performance

Approximate transcription time for a one-hour call with five speakers, assuming
roughly 40 minutes of actual speech once silence is excluded.

| Model | mlx-whisper (M-series) | faster-whisper (CUDA) | faster-whisper (CPU, 8 cores) |
| --- | --- | --- | --- |
| `tiny` | about 1 min | under 1 min | about 3 min |
| `base` | about 2 min | about 1 min | about 4 min |
| `small` | about 4 min | about 1 min | about 10 min |
| `medium` | about 8 min | about 2 min | about 25 min |
| `large-v3` | about 12 min | about 4 min | about 50 min |

These are indicative figures derived from published throughput for each runtime,
not measurements taken from this project. Actual time varies with the chip,
thermal headroom, how much of the call is speech, and the number of segments.
Treat the ordering between rows as reliable and the absolute values as an
estimate. `small` is the default because it is where accuracy stops improving
quickly for conversational speech.

---

## Known limitations

**Voice receive under Discord end-to-end encryption.** Discord enforces DAVE,
its end-to-end encryption protocol for audio and video, on non-Stage voice calls
as of 2 March 2026. py-cord 2.8.1 decrypts received audio through it, and the
required library, `davey`, is a dependency of `py-cord[voice]` and is carried
inside the standalone executables. Recording an encrypted call is therefore
supported.

What is not reported by the library is failure. Audio is yielded only once a DAVE
session exists and its handshake has completed; until then every packet is
discarded, and a packet that cannot be decrypted afterwards is replaced with an
opus silence frame. Both are logged below the default level, so the visible
result is a recording of the right length holding nothing, which is
indistinguishable from a call in which nobody spoke.

Stenos separates the two rather than writing out an empty transcript. `--check`
reports whether the encryption library is present, `/record status` reports the
negotiated session state while nothing has arrived yet, and `/record stop`
explains which of the two happened instead of posting a transcript with no lines.

**Two defects in py-cord 2.8.1 are repaired at startup.** The first discards
received audio on a call carrying no encryption: `decrypt_rtp` performs the
transport decryption into a local, then returns a field that only the encryption
branch ever assigns, so the caller reads back nothing and drops the packet.

The second loses the audio on a call that does carry encryption, which since
March 2026 is every call. The RTP header extension is removed twice, once by the
transport decryption using a constant that is right only when the sender wrote
exactly two extension words, and again afterwards from the opus frame the session
has already returned. Two extension words survive as far as the decoder and then
arrive missing their first eight bytes, which the decoder rejects as a corrupted
stream. Every other size loses the wrong bytes before the session sees them, so
the packet fails to decrypt and becomes opus silence. There is no extension size
at which the audio survives, which is why a recording made against a stock 2.8.1
is silence interrupted by decode failures.

Stenos repairs both, and no more than that. The first is confined to the one
state where no encryption can have been applied: a connection with no session.
The second changes which bytes are removed and when, never whether decryption
happens. Every other state keeps py-cord's behaviour untouched.

Whether to repair is decided by running the decryptor at four different extension
sizes, not by comparing version numbers, so a py-cord that has fixed either is
left alone and that repair becomes inert rather than needing to be noticed and
removed. `--check` reports the decision on the `receive repair` line, and the
test suite fails with a message asking for the module to be deleted once the
defects are gone.

**No live transcription.** Transcription is deliberately post-call. Running
inference during a call on a fanless machine causes thermal throttling that
degrades both the transcription and the voice connection.

---

## Project structure

Packets become timestamped segments, segments become 16 kHz mono audio, audio
becomes text, and text becomes one ordered transcript.

| Stage | File | Lines | Responsibility |
| --- | --- | --- | --- |
| **Receiving** | sink.py | 393 | Places packets on the media clock they carry and splits segments on silence; loads libopus |
| **Transport** | voice.py | 205 | Reads the end-to-end encryption state a voice connection negotiated |
| **Transport** | upstream.py | 381 | Repairs the two py-cord 2.8.1 defects that lose received audio, when they are present |
| **Conversion** | audio.py | 132 | Downmixes and resamples to 16 kHz mono, discarding fragments too short to carry speech |
| **Verification** | integrity.py | 109 | Separates a recording that captured nothing from a call in which nobody spoke |
| **Transcription** | transcribe.py | 283 | Backend protocol, mlx and faster-whisper implementations, and the segment loop |
| **Output** | transcript.py | 203 | Merges, orders, and writes the transcript and its sidecar portably |
| **Commands** | bot.py | 529 | Slash commands, session state, the offline pipeline, and the CLI |
| **Configuration** | config.py | 258 | Validated environment parsing and platform-aware backend resolution |
| **Total** | **11 files** | **2519** | Plus 3847 lines of tests |

```
src/stenos/      the bot (sink, transport, audio, transcription, output, commands)
tests/           unit tests
tests/compat/    cross-platform behaviour checks
docs/            architecture, configuration, and troubleshooting
packaging/       the specification that freezes a standalone executable
scripts/         checksum-verifying installers
```

---

## Documentation

<table>
<tr>
<td align="center" width="25%" valign="top">
<h3>Internals</h3>
<p>How a call becomes<br/>a transcript</p>
<a href="docs/architecture.md"><b>Architecture</b></a>
</td>
<td align="center" width="25%" valign="top">
<h3>Configure</h3>
<p>Every setting and<br/>when to change it</p>
<a href="docs/configuration.md"><b>Configuration</b></a>
</td>
<td align="center" width="25%" valign="top">
<h3>Fix</h3>
<p>When it does not<br/>work as expected</p>
<a href="docs/troubleshooting.md"><b>Troubleshooting</b></a>
</td>
<td align="center" width="25%" valign="top">
<h3>History</h3>
<p>What changed<br/>between versions</p>
<a href="CHANGELOG.md"><b>Changelog</b></a>
</td>
</tr>
</table>

---

## Testing

```sh
uv run pytest                        # unit suite
uv run pytest tests/compat -m compat # cross-platform checks
uv run ruff check . && uv run ruff format --check .
uv run mypy src/
```

No test opens a gateway, a voice connection, or a model. The transcription
backend is mocked and the audio is synthetic, so the suite runs offline and
never downloads weights.

Beyond the unit tests, `tests/compat/` covers the failures that are genuinely
platform-specific: opus availability, output encoding, filename sanitisation,
line endings, path construction, backend selection, and the whole pipeline end
to end. Those run on Linux, macOS, and Windows.

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `ci` | push, pull request | Lint, type check, and test on Python 3.11 to 3.13 with a 70 percent coverage floor |
| `platforms` | push to `main`, pull request, weekly | Build the wheel and verify it installs and passes tests on five platform and version combinations |
| `compat` | push to `main`, pull request, weekly | Run the offline pipeline on Linux, macOS, and Windows |
| `release` | tag matching `v*` | Build an executable per platform and draft a release with them, the installers, and one checksum file |
| `tag` | manual dispatch | Create a release tag after validating it against `pyproject.toml` |
| `cleanup` | manual dispatch | Remove a release, and optionally its tag |

`main` is expected to require passing CI before merge, configured under
**Settings, Branches, Branch protection rules** with the `lint`, `typecheck`,
and `test` checks required.

---

## Versioning

Versions have four components, `X.N.V.M`:

| Component | Meaning |
| --- | --- |
| `X` | Major version |
| `N` | Beta version |
| `V` | Alpha version |
| `M` | Commit counter |

`M` increments on every commit. Bumping any higher component resets everything
to its right to zero. The version in `pyproject.toml` is the single source of
truth, and every commit subject begins with the version that commit produces,
which makes `git log --oneline` a complete version ledger.

A release covers a whole series rather than one commit: every commit sharing an
`X.N.V` belongs to it, and the tag is cut at the last of them through the `tag`
workflow. One release per series, so a series that already has a tag is refused
a second one.

The component that opened the series names the release, which is what the two
version badges above track:

| Series | Release | Title |
| --- | --- | --- |
| `V` is non-zero | Alpha, a pre-release | `Alpha v0.1.3` |
| `V` is zero and `N` is non-zero | Beta | `Beta v0.2.0` |
| `V` and `N` are both zero | Release | `Release v1.0.0` |

An alpha is marked as a pre-release on GitHub, so the two badges resolve from
what GitHub records rather than from anything kept in step by hand. Everything
so far is an alpha, which is why the release badge reads `none`.

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) to get
started, follow the [Code of Conduct](CODE_OF_CONDUCT.md), and check
[SUPPORT.md](SUPPORT.md) if you need help. The [changelog](CHANGELOG.md) records
what changed between versions.

---

## License

Released under the MIT License. See [LICENSE](LICENSE) for the full text, and
[TERMS.md](TERMS.md) for the project terms.

<div align="center">
<sub>The name is from Greek <i>stenos</i>, the root of stenography: a verbatim record of a multi-party proceeding, every line attributed to a speaker.</sub>
</div>
