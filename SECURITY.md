# Security Policy

## Supported versions

Stenos is an early-stage project. Security fixes are applied to the latest
release on the default branch. Older versions are not maintained.

## Reporting a vulnerability

Please report security issues privately, not through public issues.

- Preferred: open a private security advisory with the "Report a vulnerability"
  button on the repository's Security tab.
- Alternatively, email the maintainer at stivenagostingjekaj@gmail.com.

Please include steps to reproduce, the affected version or commit, and the
impact as you understand it. You can expect an initial response within a few
days. Once a fix is ready it will be released, and your report will be
acknowledged unless you prefer to remain anonymous.

## Scope

Stenos records the audio of everyone in a voice channel and writes it to disk as
text. Treat the following as known and documented properties rather than
vulnerabilities.

- **Recorded audio is buffered in memory unencrypted** while a call is in
  progress, and transcripts are written to `transcripts/` with no encryption at
  rest. Anyone who can read that directory can read the transcript.
- **The bot token grants full control of the bot.** It is read from the
  environment or a `.env` file, which is gitignored. Protecting it is the
  operator's responsibility.
- **Anyone who can use the slash commands can start a recording.** Stenos does
  not implement its own permission model; restrict the commands through
  Discord's own channel and role permissions if you need to.
- **Recording is announced, never silent.** The start and stop messages are
  deliberate and non-ephemeral. A request to add a silent mode is a feature
  request that will be declined, not a security report.

Model weights are downloaded from Hugging Face on first use. That is the only
network destination beyond Discord itself, and no audio or transcript is ever
sent anywhere.

## Out of scope

- The lack of receive-side DAVE support in py-cord. That is an upstream
  limitation, documented in the README, not a vulnerability in Stenos.
- Denial of service caused by recording an extremely long call. Buffering is
  bounded by the host's memory, and the operator chooses when to stop.
