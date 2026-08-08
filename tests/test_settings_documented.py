"""Every setting the code reads, against every setting the project documents.

Three files describe the same set of environment variables and none of them is
derived from the others: `config.py` reads them, `.env.example` is what people
copy, and `docs/configuration.md` explains them. A setting added to one and not
the rest is invisible to whoever needed it, which is what happened to
`OUTPUT_DIR`: the field existed and was plumbed the whole way through, and
nothing read it or mentioned it anywhere.

The numbers are checked too, because a default stated in the example and a
default compiled into the code are two claims about one value.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from stenos import config as settings

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
REFERENCE = ROOT / "docs" / "configuration.md"

if not EXAMPLE.is_file():
    pytest.skip(
        "not a repository checkout, so there is no .env.example to check",
        allow_module_level=True,
    )

#: Read by config.py through one of the two helpers that take a key.
_LOOKUP = re.compile(r'_lookup\(env, "([A-Z_]+)"\)')
_READER = re.compile(r'_read_\w+\(\s*env,\s*"([A-Z_]+)"')

#: Named in .env.example, whether commented out or not.
_ASSIGNMENT = re.compile(r"^(?:# )?([A-Z_]+)=(.*)$", re.MULTILINE)

#: Given a section of its own in the configuration reference.
_HEADING = re.compile(r"^### `([A-Z_]+)`", re.MULTILINE)

#: Read somewhere other than config.py, so absent from what it parses. Listed
#: rather than discovered, since the point is that the set is small enough to
#: name and each entry has a reason.
READ_ELSEWHERE = {
    # sink.py, since it points at a library rather than configuring behaviour.
    "OPUS_LIBRARY_PATH",
}


def settings_read() -> set[str]:
    source = (ROOT / "src" / "stenos" / "config.py").read_text(encoding="utf-8")
    return set(_LOOKUP.findall(source)) | set(_READER.findall(source))


def settings_shown() -> dict[str, str]:
    return dict(_ASSIGNMENT.findall(EXAMPLE.read_text(encoding="utf-8")))


def test_every_setting_read_is_in_the_example_file() -> None:
    missing = settings_read() - set(settings_shown())

    assert not missing, f"read by config.py and absent from .env.example: {sorted(missing)}"


def test_every_setting_read_has_a_section_in_the_reference() -> None:
    documented = set(_HEADING.findall(REFERENCE.read_text(encoding="utf-8")))
    missing = settings_read() - documented

    assert not missing, f"read by config.py and undocumented: {sorted(missing)}"


def test_the_example_file_offers_nothing_that_is_not_read() -> None:
    # The other direction, and the more misleading one: a setting somebody sets
    # and the code never looks at does nothing, silently.
    unread = set(settings_shown()) - settings_read() - READ_ELSEWHERE

    assert not unread, f"offered by .env.example and read by nothing: {sorted(unread)}"


@pytest.mark.parametrize(
    ("key", "default"),
    [
        ("SEGMENT_GAP", settings.DEFAULT_SEGMENT_GAP),
        ("MIN_SEGMENT", settings.DEFAULT_MIN_SEGMENT),
        ("MAX_SEGMENT", settings.DEFAULT_MAX_SEGMENT),
        ("MAX_BUFFER_MB", settings.DEFAULT_MAX_BUFFER_MB),
        ("DISCONNECT_GRACE", settings.DEFAULT_DISCONNECT_GRACE),
    ],
)
def test_the_example_states_the_default_the_code_uses(key: str, default: float) -> None:
    # Copying .env.example must not change how anything behaves.
    assert float(settings_shown()[key]) == pytest.approx(default)


def test_the_example_names_the_model_default() -> None:
    assert settings_shown()["WHISPER_MODEL"] == settings.DEFAULT_MODEL


def test_the_example_output_directory_matches_the_default() -> None:
    assert Path(settings_shown()["OUTPUT_DIR"]) == settings.DEFAULT_OUTPUT_DIR
