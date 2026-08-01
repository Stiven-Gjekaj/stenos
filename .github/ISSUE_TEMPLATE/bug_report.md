---
name: Bug report
about: Report something that does not work as expected
title: ""
labels: bug
assignees: ""
---

## What happened

A clear description of the bug.

## Diagnostics

Paste the full output of `stenos --check`. It reports the resolved backend,
whether that backend can be imported, and whether libopus loaded, which is what
most reports turn out to hinge on.

```

```

## How to reproduce

Steps, or the smallest sequence that shows the problem.

## What you expected

What you expected to happen instead.

## What actually happened

The actual output, transcript, or error, copied exactly. If a transcript is
wrong, the matching `.json` sidecar is more useful than the `.txt`, because it
carries the raw segment timings.

## Environment

- Stenos version (from `stenos --version`):
- Installed how: standalone executable, or from source with `uv sync`?
- Operating system and architecture:
- Python version, if installed from source:

## Before you file

If the transcript came out empty and `--check` shows `opus loaded True`, this is
most likely the DAVE limitation described in the README's known limitations
section rather than a bug here. Please read that section first.
