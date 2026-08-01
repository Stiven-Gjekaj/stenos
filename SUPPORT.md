# Getting help

Need help with Stenos? Here is where to look.

## Diagnose it first

```sh
stenos --check
```

It reports the resolved backend, whether that backend can be imported, and
whether libopus loaded, without connecting to Discord. Most problems are visible
in its output, and it is the first thing an issue should include.

## Read

- [docs/troubleshooting.md](docs/troubleshooting.md) covers the common failures:
  an empty transcript, a missing backend, segments splitting oddly, commands not
  appearing.
- [docs/configuration.md](docs/configuration.md) documents every setting and
  when to change it.
- [docs/architecture.md](docs/architecture.md) explains how a call becomes a
  transcript, which is the place to start before changing anything.
- The [README](README.md) covers setup, supported platforms, the sleep and power
  settings an unattended host needs, and the known limitations.

## Ask a question or report a problem

- Search the existing
  [issues](https://github.com/Stiven-Gjekaj/stenos/issues) first, in case
  someone has already asked.
- If you found a bug, open a bug report and include the output of
  `stenos --check`.
- If you want a feature, open a feature request.

Please do not use the issue tracker for security problems. See
[SECURITY.md](SECURITY.md) for how to report those privately.

## Before reporting an empty transcript

Discord began enforcing DAVE, its end-to-end encryption protocol for voice, on
2 March 2026, and py-cord's receive-side support for it is not complete. A
recording that produces no packets is most likely this rather than a bug in
Stenos. The README's known limitations section covers it, and `/record stop`
says so explicitly rather than writing an empty file.

## Contributing

If you would like to help improve Stenos, see
[CONTRIBUTING.md](CONTRIBUTING.md).
