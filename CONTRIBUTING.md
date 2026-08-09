# Contributing to Stenos

Thanks for your interest in Stenos, a Discord bot that records each voice
participant separately and transcribes the call locally. Contributions of all
kinds are welcome: bug reports, documentation fixes, platform support, and
features.

## Ways to contribute

- Report a bug or request a feature by opening an issue.
- Improve the documentation in `docs/` or the README.
- Add a transcription backend, following the guide at the end of
  [docs/architecture.md](docs/architecture.md).
- Widen platform coverage in `tests/compat/`.

Before starting significant work, please open an issue to discuss it, so we can
agree on the approach before you spend time on a pull request.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 or later. libopus is
needed for anything touching voice receive.

    git clone https://github.com/Stiven-Gjekaj/stenos
    cd stenos
    sudo apt install -y libopus0 libsodium23   # or: brew install opus
    uv sync

A plain `uv sync` installs neither transcription backend. That is deliberate:
continuous integration must never download model weights. Add `--extra mlx` on
Apple Silicon or `--extra cuda` elsewhere when you want to run a real
transcription.

Confirm the result:

    uv run stenos --check

## Before you open a pull request

Every change must keep the project green. Run these locally, exactly as CI does:

    uv run ruff check .
    uv run ruff format --check .
    uv run mypy src/
    uv run pytest
    uv run pytest tests/compat -m compat

Install the hooks so the first two run before each commit rather than in CI:

    uv run pre-commit install

- `pytest` runs the unit suite. Coverage must stay above 70 percent.
- `pytest tests/compat -m compat` runs the cross-platform checks. They are
  excluded from the default run because they assert things about the host, such
  as whether libopus is installed.
- The README states how many tests there are and how long each source file is,
  and three tests fail the commit that lets one of those drift. Adding a test
  or a function moves them, so the answer is a command rather than arithmetic:

      uv run python scripts/refresh_counts.py

  Pass `--check` to see what is stale without rewriting anything.
- No test may open a gateway, a voice connection, or a model. Mock the backend
  with `MockBackend`, which ships with the package for exactly this reason.

If you change how audio is segmented, converted, or merged, add cases that pin
the behaviour rather than describe it. The sink takes an injectable clock so
segmentation can be driven by scripted timestamps with no sleeping, and the
existing tests in `tests/test_sink.py` show the pattern.

## Coding style

- Match the surrounding code. The project favours small, focused functions and
  clear names over cleverness.
- Type hints throughout. `mypy` runs over `src/` with untyped definitions
  disallowed.
- Add dependencies sparingly. Stenos has four direct runtime dependencies, and
  both transcription backends are optional extras. A pull request that adds one
  should justify the need and prefer the standard library where practical.
- Write documentation and comments in plain prose. Do not use em-dashes or
  emoji in source, docs, commit messages, or examples. Do not use real people's
  names in examples; the placeholders are Alpha, Bravo, Charlie, and Delta.

## Versioning and commit messages

Versions have four components, `X.N.V.M`, described in the versioning section of
the README. `M` increments on every commit, and the version in `pyproject.toml`
is the single source of truth.

Each commit subject begins with the version that commit produces:

    0.1.1.4: add gap-based segment boundary detection to sink

    Open a new segment when the interval since a user's previous packet
    exceeds SEGMENT_GAP. Discord transmits only during speech, so packet
    gaps delimit utterances without a separate voice activity pass.

- Bump the version in `pyproject.toml` in the same commit as the change it
  describes, never in a separate one, and run `uv lock` so the lock agrees.
- Keep each commit to one logical change. A module and its tests are two
  commits, and neither may leave the tree failing.
- The subject is imperative, present tense, no trailing period, 72 characters at
  most. The body is required and explains what changed and why.
- Do not add co-author trailers or tool attribution.

Add a section to [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
Release notes are read from that file, so it is written for a person rather
than generated from history.

## Reporting security issues

Please do not open a public issue for a security problem. See
[SECURITY.md](SECURITY.md) for how to report it privately.

## Code of conduct

By taking part in this project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).
