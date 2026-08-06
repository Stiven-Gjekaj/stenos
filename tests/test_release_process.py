"""Tests for the file that cuts a release, and the trigger that watches it.

A release is started by writing a version into .github/release-version and
pushing. Neither half can be exercised from here, since one is a GitHub trigger
and the other is a workflow, but the join between them can be: a rename on
either side would disable releasing entirely, and the symptom would be a push
that quietly does nothing rather than anything that looks like a failure.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# Everything here asserts something about the repository rather than about the
# installed package, so it has nothing to say where only the wheel and a copy of
# the tests are present. platforms.yml is exactly that: it runs the suite from a
# directory holding tests and pyproject.toml alone, against a clean environment
# carrying neither .github nor the dev dependencies. Skipping is checked before
# yaml is imported, because that environment does not have it either.
if not WORKFLOWS.is_dir():
    pytest.skip(
        "not a repository checkout, so there are no workflows to check",
        allow_module_level=True,
    )

import yaml  # noqa: E402  after the skip, which must not need a dev dependency

#: Every version in this project, including the one the marker holds.
VERSION = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def workflow(name: str) -> dict:
    """One workflow as parsed YAML.

    ``on`` is read through the boolean as well as the string, because YAML 1.1
    resolves a bare ``on:`` key to True and PyYAML follows it.
    """
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return document


def triggers(name: str) -> dict:
    document = workflow(name)
    return document.get("on", document.get(True))


def marker_path() -> Path:
    """The file the tag workflow says it watches."""
    return ROOT / triggers("tag.yml")["push"]["paths"][0]


def marker_version() -> str:
    """The version the marker names, read the way the workflow reads it."""
    lines = marker_path().read_text(encoding="utf-8").splitlines()
    return next(
        line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")
    )


def packaged_version() -> str:
    packaging = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(packaging["project"]["version"])


@pytest.mark.parametrize("name", sorted(path.name for path in WORKFLOWS.glob("*.yml")))
def test_every_workflow_parses(name: str) -> None:
    # A syntax error here is otherwise found by GitHub ignoring the file, which
    # looks exactly like a trigger that did not fire.
    assert isinstance(workflow(name), dict)


def test_the_release_trigger_names_a_file_that_exists() -> None:
    # The join. Rename either side without the other and pushing a version stops
    # releasing anything, silently.
    assert marker_path().is_file()


def test_the_release_trigger_watches_main_alone() -> None:
    push = triggers("tag.yml")["push"]

    assert push["branches"] == ["main"]
    assert len(push["paths"]) == 1


def test_dispatching_by_hand_is_still_possible() -> None:
    # The push path depends on a trigger this repository cannot test. Losing the
    # manual route as well would leave no way in at all.
    inputs = triggers("tag.yml")["workflow_dispatch"]["inputs"]

    assert "version" in inputs
    assert "force" in inputs


def test_the_marker_holds_one_well_formed_version() -> None:
    assert VERSION.match(marker_version()), marker_version()


def test_the_marker_does_not_name_a_release_from_the_future() -> None:
    # It names the last release cut, so it is normally behind the version being
    # worked on and equal to it at the moment of release. Ahead of it means a
    # typo naming something that does not exist.
    marker = tuple(int(part) for part in marker_version().split("."))
    packaged = tuple(int(part) for part in packaged_version().split("."))

    assert marker <= packaged, f"marker {marker_version()} is ahead of {packaged_version()}"


def test_the_packaged_version_is_well_formed() -> None:
    assert VERSION.match(packaged_version()), packaged_version()


def test_the_release_gate_requires_every_workflow_not_a_list() -> None:
    # The first release cut by pushing went out with platforms red, because the
    # gate named ci and compat and platforms was not on the list. A list has to
    # be remembered; asking for every workflow that ran does not.
    steps = workflow("tag.yml")["jobs"]["tag"]["steps"]
    gate = next(step for step in steps if "green" in step.get("name", ""))

    # Excludes itself by name and requires the rest, rather than naming any.
    assert "github.workflow" in gate["env"]["SELF"]
    assert 'select(.name != \\"${SELF}\\")' in gate["run"]
    assert "ci compat" not in gate["run"]


def test_a_release_cannot_go_out_with_no_notes() -> None:
    steps = workflow("tag.yml")["jobs"]["tag"]["steps"]
    validate = next(step for step in steps if step.get("name", "").startswith("Validate"))

    assert "CHANGELOG.md" in validate["run"]


def test_this_series_has_notes_to_release() -> None:
    # The same check the workflow makes, so a series without notes fails here
    # rather than three minutes into a release.
    series = ".".join(packaged_version().split(".")[:3])
    heading = re.compile(rf"^## {re.escape(series)}([ \t(]|$)", re.MULTILINE)

    assert heading.search((ROOT / "CHANGELOG.md").read_text(encoding="utf-8")), (
        f"CHANGELOG.md has no '## {series}' section"
    )
