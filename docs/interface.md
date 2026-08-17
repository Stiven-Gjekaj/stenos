# The graphical interface

The design agreed before any of it is written, answering
[issue #10](https://github.com/Stiven-Gjekaj/stenos/issues/10). v0.3 is cut when
this works and needs nothing further.

---

## Written in Tkinter, from the standard library

The project has four direct runtime dependencies and ships as a frozen
executable that already carries libopus and a Whisper backend. A fifth
dependency for the interface would be the largest of them, and the freeze is
where it would hurt: PyInstaller has to be told about every toolkit's data
files, and a toolkit that ships its own binaries makes the download larger on
three platforms.

Tkinter adds no dependency to install. It is in the standard library,
PyInstaller handles it without being told, and it runs on all three platforms
the release already builds for.

**It is not always present, which this document originally got wrong.** Tkinter
is in the standard library only when Python was built against Tk, and on Linux
that is routinely a separate distribution package rather than part of the
interpreter. The machine this was written on has no Python with it. So the
window is imported where it is used rather than at the top of the package,
nothing else in the program depends on it, and `--interface` on a Python
without it prints the package to install instead of an ImportError. The frozen
builds are made on runners that have it, so a downloaded executable is
unaffected.

It looks dated, and building the transcript library will be more manual than it
would be in a browser. That is the trade, taken knowingly.

## Local only, with nothing listening

Nothing leaves the machine. That is the reason this project exists rather than
using a hosted transcription service, and it is the claim the README makes
first.

A local web interface was the obvious alternative and is rejected. Serving one
means binding a port, and a port is reachable by anything else on the host and,
misconfigured, by anything on the network. The position is not "nothing leaves
the machine unless you configure it badly". Tkinter draws to the screen and
opens no socket, so there is nothing to misconfigure.

## It starts the bot rather than talking to one

The interface runs the bot in its own process rather than attaching to one
already running.

The alternative needs a control channel between two processes, and every
version of that is a socket, a named pipe, or a file being polled. The first is
the port this design just rejected. The others are worse: a pipe with no
authentication is a local privilege boundary nobody has thought about, and a
polled file is a protocol invented by accident.

Running in process means the interface owns the bot's lifetime, which is also
what makes the live view honest: it reads the session objects directly rather
than a summary written for it.

The cost is that a bot started from a terminal cannot be attached to later.
That is acceptable. The two ways of running are for different people: a server
runs `stenos` under a service manager, and a laptop runs the interface.

## It shows three things

- **The live recording.** Which channel, how long, how much is buffered,
  whether the connection is up, and the unattributed packet count. This is
  `/record status` without having to type it, and it is the screen that
  justifies the interface: an unattended recording currently reports only into
  a channel nobody is watching.
- **A library of past transcripts.** Read from `OUTPUT_DIR` and its sidecars,
  which already carry the channel, the speakers, and every offset. Also where a
  `.partial` directory left by a crash is offered for recovery, which is
  `--recover` with something to click.
- **Configuration.** The settings in `docs/configuration.md`, with the
  validation `load_config` already performs, so a bad value is refused where it
  is typed rather than at the next start.

---

## What it drives

The interface is a view. Everything it does is already a function that takes
values and returns values, which is what makes it replaceable and testable:

- `run_pipeline` transcribes without knowing Discord exists.
- `transcribe_files` reads audio and produces a transcript, added in `0.2.4.9`.
- `recover` finishes a recording a crash interrupted, added in `0.2.3.44`.
- `read_spill` and `partial_recordings` describe what is waiting to be
  recovered.

Starting a recording is one of them as of `0.2.5.1`. `begin_recording` joins a
channel and starts it, `keep_recording` registers one that has been announced,
and `abandon_recording` undoes one that could not be. The slash command
validates an interaction and renders the answer; none of the work knows how it
was asked for, which is what lets the interface drive the same path rather than
imitating a command.

Stopping followed in `0.2.5.2`, and turned out to be nearly free.
`finish_recording` was already a module level function returning a message and
an attachment, knowing nothing about interactions, because the buffer ceiling,
a lost connection and a shutdown all needed it before the interface did.
`end_recording` takes a guild identifier, deregisters the session before
transcribing it so a second stop cannot transcribe the same call twice, and
returns None when there is nothing recording rather than a sentence written for
Discord.

One thing it still inherits: it hands back a rendered message and a
`discord.File`, which is what every existing caller wanted. An interface wants
the transcript path and the speaker list, so `finish_recording` should
eventually return the `RecordingResult` alongside the rendering. That is a
small change and it is not made yet.

## What it is built out of

`interface.py`, added in `0.2.5.4`, works out what the screens show without
drawing any of it: the recordings in progress, read from the sessions the bot
holds rather than from a summary written for the screen, so the interface
cannot show one the bot does not have; and the library, read from `OUTPUT_DIR`
the way a person reads that folder, including the `.partial` directories a
crash left behind.

Splitting it this way is what makes the decisions testable. Which recordings
exist, what a finished one says about itself, what happens to a transcript
whose sidecar is damaged, and whether a connection that is down looks different
from one that is up, are all answered here and none of them needs a display.
The widgets become a rendering of values.

A damaged sidecar is listed with less on it rather than refusing the library,
and a `.partial` directory this version cannot read is still offered, because
something is there and somebody should be told.

## The window as it stands

`window.py`, added in `0.2.5.5`, draws three tabs: the transcripts already
written, the recordings a crash left unfinished, and the settings in force. It
can transcribe the unfinished ones, on a worker thread, because transcription
takes minutes and doing it on the drawing thread would freeze the window for
the whole of it, which reads as a crash.

It is read only otherwise. Starting and stopping a recording needs the bot
running in the same process as the loop that draws this, and integrating an
asyncio loop with a Tk mainloop is a piece of design in its own right rather
than something to attempt alongside the first window.

## What this rules out

- Anything listening on a port, including on loopback.
- Remote control of a bot on another machine. That is the same port, and the
  project has no authentication story to put in front of it.
- Any interface that becomes the only way to do something. The command line
  stays complete, because a server has no display and is the primary target.
