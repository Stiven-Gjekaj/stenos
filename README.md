# Stenos

A Discord bot that records every voice participant separately and produces one timestamped, speaker-attributed transcript, transcribed entirely on your own machine.

## Sample output

```
[00:00:04] Stiven: right, so about the asset pipeline
[00:00:09] Enxhi: which part broke
[00:00:12] Stiven: the exporter, it stopped writing normals on anything with a mirror modifier
[00:00:21] Enxhi: since when
[00:00:23] Stiven: since we bumped blender, i think
[00:01:02] Ana: i can reproduce it on my machine if you want a second data point
[00:01:09] Stiven: please, and check whether it also drops tangents
[00:01:17] Ana: will do
[00:04:12] Enxhi: ok so i found it, the exporter reads the evaluated mesh before the modifier stack runs
[00:04:19] Stiven: that would explain the mirror case exactly
```

Alongside it, a `.json` sidecar carries the raw segments, their offsets and durations, and the user identifiers, for downstream tooling.

## Why this exists

**Speaker and timestamp alignment is correct.** Discord delivers a separate audio stream per participant, which makes attribution free, but the sink bundled with py-cord concatenates each user's received packets into one buffer. That discards the point in the call at which each utterance happened, so a participant who joins late, or who simply stays quiet for the first ten minutes, is placed at the wrong offset in the merged output. Most comparable projects inherit this drift. Stenos timestamps every packet on arrival and opens a new segment whenever a speaker falls silent past a threshold, so the merge is correct by construction rather than by correction.

**Inference is local.** Audio is buffered in memory during the call and transcribed after it ends. Nothing is uploaded, and no API key is involved.

**Apple Silicon is the primary target, without CUDA.** On an M-series machine Stenos uses `mlx-whisper`, which runs on the GPU through Metal. Transcription is deliberately deferred until the call ends, because the intended host is a fanless laptop that will thermally throttle under sustained inference.

Discord clients transmit only while someone is speaking, so the gaps between packets already mark the silence. No separate voice activity detector is used.

## Supported platforms

| Operating system | Architecture | Backend | Additional prerequisite |
|---|---|---|---|
| macOS 14+ | arm64 (Apple Silicon) | `mlx-whisper` | None |
| macOS 13+ | x86_64 | `faster-whisper` (CPU) | None |
| Linux | x86_64, aarch64 | `faster-whisper` (CUDA or CPU) | `libopus0`, `libsodium23` |
| Windows 10+ | x86_64 | `faster-whisper` (CUDA or CPU) | None |

Python 3.11, 3.12, and 3.13 are supported. py-cord bundles opus and libsodium binaries for macOS and Windows; on Linux both come from the system package manager, and voice receive fails at runtime rather than at import when they are missing.

## Setup

### 1. Create the bot

1. Open the [Discord developer portal](https://discord.com/developers/applications) and create a new application.
2. Under **Bot**, create a bot and copy its token.
3. Under **Installation** or **OAuth2 URL Generator**, select the scopes `bot` and `applications.commands`.
4. Select these bot permissions: **View Channel**, **Connect**, **Send Messages**, **Attach Files**. The corresponding permissions integer is `1084416`.
5. Invite the bot to your server with the generated URL.

No privileged intents are required. Stenos uses the voice state intent, which is enabled by default, and never reads message content.

### 2. Install

Install [uv](https://docs.astral.sh/uv/), then clone the repository and run the single command for your platform.

macOS, Apple Silicon:

```sh
uv sync --extra mlx
```

macOS on Intel:

```sh
uv sync --extra cuda
```

Linux:

```sh
sudo apt install -y libopus0 libsodium23 && uv sync --extra cuda
```

Windows, in PowerShell:

```powershell
uv sync --extra cuda
```

The `cuda` extra installs `faster-whisper`, which runs on the CPU when no compatible GPU is present. Neither backend is installed by the plain `uv sync`, which keeps continuous integration free of model weights.

### 3. Configure

```sh
cp .env.example .env
```

Set `DISCORD_TOKEN`. Setting `GUILD_ID` registers the slash commands to one server, where they appear immediately rather than taking up to an hour to propagate globally.

| Setting | Default | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | none | Bot token. Required. |
| `GUILD_ID` | unset | Register commands to one guild for immediate propagation. |
| `WHISPER_BACKEND` | `mlx` in `.env.example` | `mlx`, `faster-whisper`, or `auto`. Left unset, it resolves per platform as `auto` does. |
| `WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3`. |
| `LANGUAGE` | `auto` | ISO 639-1 code, or `auto` to detect. |
| `SEGMENT_GAP` | `0.4` | Seconds of silence that close a segment. |
| `MIN_SEGMENT` | `0.3` | Segments shorter than this are discarded. |
| `KEEP_AUDIO` | `false` | Retain buffered audio after transcription. |

### 4. First run

```sh
uv run stenos --check
```

This reports the resolved backend, the configured thresholds, and whether opus loaded, without connecting to Discord. It is the first thing to inspect when voice receive misbehaves. Then start the bot:

```sh
uv run stenos
```

In Discord:

- `/record start` joins your voice channel and begins recording.
- `/record status` reports elapsed time and how many participants have spoken.
- `/record stop` stops, transcribes, and posts the transcript.

Transcripts are written to `transcripts/stenos-<channel>-<timestamp>.txt`, and attached to the completion message when they fit inside the server's upload limit.

## Operational notes

Transcription runs after the call ends and can take several minutes. It runs on a worker thread so the gateway heartbeat continues, but the process must stay alive and connected for the whole call.

**macOS.** Run the bot under `caffeinate` so the system does not idle sleep:

```sh
caffeinate -is uv run stenos
```

Keep the machine on mains power. `caffeinate` does not prevent sleep when the lid is closed on battery, and a clamshell sleep drops the voice connection mid-call. Leave the lid open, or use an external display so the machine stays awake in clamshell mode.

**Linux.** Systemd will suspend an idle desktop session. For an unattended host, mask the sleep targets:

```sh
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

Run the bot under a systemd user service with `Restart=on-failure` so it survives a crash, and disable any laptop lid switch handling in `/etc/systemd/logind.conf` by setting `HandleLidSwitch=ignore`.

**Windows.** Set the power plan to never sleep while plugged in, and disable USB selective suspend if the microphone drops out:

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```

Closing the lid still suspends by default on a laptop; change the lid action to **Do nothing** under Power Options.

**All platforms.** The realistic failure is the host losing network mid-call, which ends the recording silently. Stenos posts start and stop messages to the text channel for exactly this reason, so a missing stop message is the signal that something went wrong.

## Consent and legal note

Recording law varies by jurisdiction. Some require the consent of every participant, some require only one party, and some distinguish private conversations from other settings. Determining what applies to your recording is your responsibility.

Stenos announces itself by design. `/record start` and `/record stop` post visible, non-ephemeral messages to the text channel, and Discord itself shows the bot as connected to the voice channel. There is no silent recording mode and none will be added.

## Performance

Approximate transcription time for a one-hour call with five speakers, assuming roughly 40 minutes of actual speech once silence is excluded.

| Model | mlx-whisper (M-series) | faster-whisper (CUDA) | faster-whisper (CPU, 8 cores) |
|---|---|---|---|
| `tiny` | about 1 min | under 1 min | about 3 min |
| `base` | about 2 min | about 1 min | about 4 min |
| `small` | about 4 min | about 1 min | about 10 min |
| `medium` | about 8 min | about 2 min | about 25 min |
| `large-v3` | about 12 min | about 4 min | about 50 min |

These are indicative figures derived from published throughput for each runtime, not measurements taken from this project. Actual time varies with the specific chip, thermal headroom, how much of the call is speech, and the number of segments. Treat the ordering between rows as reliable and the absolute values as an estimate. `small` is the default because it is the point where accuracy stops improving quickly for conversational speech.

## Known limitations

**Voice receive under Discord end-to-end encryption.** Discord began enforcing DAVE, its end-to-end encryption protocol for audio and video, on non-Stage voice calls on 2 March 2026. py-cord added DAVE support in 2.8.0 scoped to voice sending, and its documentation notes that recording may not work as expected under the protocol. Receive-side support is upstream work that Stenos cannot substitute for.

Everything from the sink onward is unaffected and fully exercised offline by the test suite, so Stenos will work without code changes once py-cord lands receive-side DAVE. In the meantime, if a recording produces no packets, `/record stop` reports that explicitly rather than posting an empty transcript. Check `uv run stenos --check` to confirm opus is loaded before concluding the problem is elsewhere.

**No live transcription.** Transcription is deliberately post-call. Running inference during a call on a fanless machine causes thermal throttling that degrades both the transcription and the voice connection.

## Development

```sh
uv sync
uv run pytest                        # unit suite
uv run pytest tests/compat -m compat # cross-platform checks
uv run ruff check . && uv run ruff format --check .
uv run mypy src/
```

Install the pre-commit hooks so lint runs before each commit rather than in continuous integration:

```sh
uv run pre-commit install
```

Neither transcription backend is installed by `uv sync`. Tests mock the backend and never load model weights.

### Continuous integration

| Workflow | Trigger | Purpose |
|---|---|---|
| `ci` | push, pull request | Lint, type check, and test on Python 3.11 to 3.13 with a 70 percent coverage floor. |
| `platforms` | push to `main`, pull request, weekly | Build the wheel and verify it installs and passes tests on six platform and version combinations. |
| `compat` | push to `main`, pull request, weekly | Run the offline pipeline on Linux, macOS, and Windows. |
| `release` | tag matching `v*` | Build distributions and publish a release. |
| `tag` | manual dispatch | Create a release tag after validating it against `pyproject.toml`. |

`main` is expected to require passing CI before merge. Configure this under **Settings, Branches, Branch protection rules**, requiring the `lint`, `typecheck`, and `test` checks.

### Versioning

Versions have four components, `X.N.V.M`:

| Component | Meaning |
|---|---|
| `X` | Major version |
| `N` | Beta version |
| `V` | Alpha version |
| `M` | Commit counter |

`M` increments on every commit. Bumping any higher component resets everything to its right to zero. The version in `pyproject.toml` is the single source of truth, and every commit subject begins with the version that commit produces, which makes `git log --oneline` a complete version ledger.

Tags are created only on an `X`, `N`, or `V` bump, through the `tag` workflow.

## License

MIT. See [LICENSE](LICENSE).
