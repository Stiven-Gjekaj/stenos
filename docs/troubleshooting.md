# Troubleshooting

Start here:

```sh
stenos --check
```

It reports the resolved backend, whether that backend can be imported, and
whether libopus loaded, without connecting to Discord. Most problems below are
visible in its output.

```
stenos 0.1.2.0 (frozen executable)
python           3.12.8 on Darwin arm64
backend          auto resolves to mlx
backend usable   True (installed)
model            small
language         auto
segment gap      0.4s
minimum segment  0.3s
output directory transcripts
keep audio       False
opus loaded      True
```

---

## The bot joins the channel but the transcript is empty

`/record stop` reports "no audio was received" rather than writing an empty
file, so this is distinguishable from a transcription that found no speech.

### Check that opus loaded

```
opus loaded      False
```

Voice receive cannot decode anything without libopus, and the failure appears at
runtime rather than at import. py-cord bundles a binary for Windows only.

- **macOS:** `brew install opus`. Stenos searches `/opt/homebrew/lib` and
  `/usr/local/lib` itself, so nothing further is needed.
- **Linux:** `sudo apt install -y libopus0 libsodium23`, or the equivalent for
  your distribution.
- **Anywhere else:** set `OPUS_LIBRARY_PATH` to the full path of the library and
  check again.

A standalone executable carries its own copy and should never report `False`
for this reason. If it does, the download is damaged; check it against
`SHA256SUMS`.

### Otherwise it is probably DAVE

Discord began enforcing DAVE, its end-to-end encryption protocol for voice, on
2 March 2026. py-cord added DAVE support for voice *sending* in 2.8.0, and its
own documentation notes that recording may not work as expected under the
protocol. Receive-side support is upstream work.

There is no workaround inside Stenos. Everything downstream of the sink is
unaffected, so the moment py-cord lands receive-side DAVE this works with no
change here.

---

## Transcription fails or the backend is missing

```
backend usable   False (The mlx backend requires mlx-whisper, which is not installed...)
```

The message names the command that fixes it. In short:

| Backend | Install | Runs on |
| --- | --- | --- |
| `mlx` | `uv sync --extra mlx` | Apple Silicon only |
| `faster-whisper` | `uv sync --extra cuda` | Everywhere, CPU when no GPU |

Selecting `mlx` on a machine that is not Apple Silicon will never work. Set
`WHISPER_BACKEND=auto` to let the platform decide, or `faster-whisper`
explicitly.

`/record stop` reports a backend failure in the text channel rather than leaving
the command hanging, so this is visible without reading logs.

---

## The first recording takes far longer than the performance table

Model weights are downloaded on first use and cached. The first call with a
given model pays for the download; later calls do not.

To pay that cost before a call rather than during one, start a recording, stop
it immediately, and let the short transcription pull the weights.

---

## The recording stopped on its own

The realistic cause is the host losing network or going to sleep. Stenos posts a
message when recording starts and another when it stops, so a missing stop
message means the process died rather than finishing.

See the operational notes in the [README](../README.md) for the sleep and power
settings each platform needs for an unattended run. In short: `caffeinate -is`
on macOS with the lid open and mains power, masked sleep targets on Linux, and
`powercfg` with the lid action set to do nothing on Windows.

---

## Speakers are merged or split oddly

Segments are delimited by gaps in transmission, controlled by `SEGMENT_GAP`
(default 0.4 seconds).

- **Sentences merged into one long line:** lower `SEGMENT_GAP`.
- **One sentence split across several lines:** raise it.

This never affects attribution, only where the line breaks fall. Attribution
comes from which stream a packet arrived on and cannot drift.

---

## Short interjections are missing

`MIN_SEGMENT` (default 0.3 seconds) discards anything shorter before it reaches
the model. That threshold exists because Whisper invents confident lines from
breath noise and keyboard clicks.

Lower it if genuine one-word answers are being lost, and expect phantom lines in
exchange. Setting it to `0` will produce them.

---

## A speaker is labelled "Unknown"

Display names are cached as participants join the channel being recorded. A
participant who was already speaking before the bot joined, or whose name could
not be resolved, falls back to `Unknown (<user id>)`.

The identifier is in the `.json` sidecar, so the line can still be attributed by
hand.

---

## Slash commands do not appear

Global commands take up to an hour to propagate. Set `GUILD_ID` to your server's
identifier and they register to that guild, where they appear immediately.

Check also that the bot was invited with both the `bot` and
`applications.commands` scopes. The second is what allows slash commands at all,
and adding it later requires re-inviting.

---

## The transcript was not attached to the message

It is attached only when it fits inside the server's upload limit, which is 10
MiB for an unboosted guild. A long call can exceed it.

The file is always written to `transcripts/` regardless, so nothing is lost.

---

## Reporting something else

Open an issue with the output of `stenos --check`, the platform, and what you
expected. See [SUPPORT.md](../SUPPORT.md).
