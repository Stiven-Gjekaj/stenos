---
name: Feature request
about: Suggest a capability or an improvement
title: ""
labels: enhancement
assignees: ""
---

## What you want

A clear description of the capability, and what you would do with it.

## Why the current behaviour is not enough

What you tried, and where it fell short.

## How you imagine it working

A command, a setting, or an output format, if you have one in mind.

## Scope notes

Two things are settled and will be declined, so it is worth knowing before you
write:

- **A silent recording mode.** Start and stop are announced deliberately, for
  consent and so that a connection dropped mid-call is visible.
- **User-account automation.** Stenos uses a bot token through Discord's
  documented API. Self-bots violate the Discord Terms of Service.

Live, during-the-call transcription is not settled but is unlikely: inference on
a fanless machine while a call is running causes thermal throttling that
degrades both the transcription and the voice connection. If you want it, say
what hardware you would run it on.
