"""Tests for the version the install scripts work out before downloading.

The default path of both scripts reads ``releases/latest``, which GitHub
defines as the newest release that is neither a draft nor a pre-release. Every
release of this project was an alpha, published as a pre-release, so that
endpoint answered 404 for the project's entire life and the path behind it has
never once resolved a version. The first beta is what switches it on, which
makes this the last moment it can be checked before the command in the README
starts being used in earnest.

install.sh is exercised end to end against a captured payload, since it parses
the JSON with sed and the shape of a single release differs from the list the
--pre path reads. install.ps1 is read rather than run: it needs a Windows API
to get as far as the version, and Invoke-RestMethod parses the JSON itself, so
there is no parsing of its own to get wrong.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# The wheel does not carry the install scripts, and platforms.yml runs this
# suite from a directory holding tests and pyproject.toml alone.
if not SCRIPTS.is_dir():
    pytest.skip(
        "not a repository checkout, so there are no install scripts to check",
        allow_module_level=True,
    )

#: What the API returns for a single release. Trimmed to the fields the script
#: reads plus enough of its neighbours to keep the shape honest: tag_name is
#: not the first key, and other keys end in _name.
LATEST = """\
{"url":"https://api.github.com/repos/Stiven-Gjekaj/stenos/releases/1",\
"id":1,"author":{"login":"Stiven-Gjekaj"},"node_id":"RE_x",\
"tag_name":"v0.2.0.3","target_commitish":"main","name":"Beta v0.2.0",\
"draft":false,"prerelease":false,"created_at":"2026-08-07T00:00:00Z"}
"""

#: What it returns for the list, which the --pre path reads. Newest first.
LISTING = """\
[{"id":2,"tag_name":"v0.2.1.4","name":"Alpha v0.2.1","prerelease":true},\
{"id":1,"tag_name":"v0.2.0.3","name":"Beta v0.2.0","prerelease":false}]
"""


def run_installer(tmp_path: Path, payload: str, *arguments: str) -> subprocess.CompletedProcess:
    """install.sh with its fetches answered from a string.

    A curl ahead of the real one on PATH serves the payload for the API and
    refuses everything else, so the script resolves a version, announces it,
    and then stops at the download. The announcement is what is being read.
    """
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "curl").write_text(
        "#!/bin/sh\n"
        'for argument in "$@"; do\n'
        '    case "$argument" in\n'
        "        *api.github.com*)\n"
        f"            cat <<'PAYLOAD'\n{payload}PAYLOAD\n"
        "            exit 0\n"
        "            ;;\n"
        "    esac\n"
        "done\n"
        "exit 22\n",  # what curl -f exits with on a response it refuses
        encoding="utf-8",
    )
    (shim / "curl").chmod(0o755)

    environment = dict(os.environ, PATH=f"{shim}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run(
        ["sh", str(SCRIPTS / "install.sh"), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
    )


needs_a_shell = pytest.mark.skipif(
    os.name == "nt", reason="install.sh is the POSIX installer, and Windows has install.ps1"
)


@needs_a_shell
def test_the_default_path_reads_the_tag_from_one_release(tmp_path: Path) -> None:
    # The path that has never run. releases/latest answers with a single
    # object, not the array the --pre path reads, and the tag is extracted from
    # it by the same sed either way.
    result = run_installer(tmp_path, LATEST)

    assert "Installing stenos v0.2.0.3" in result.stdout, result.stderr


@needs_a_shell
def test_the_pre_path_reads_the_newest_of_the_list(tmp_path: Path) -> None:
    # The list arrives as one long line and the leading .* in the pattern is
    # greedy, so without the split on commas this returns the oldest release
    # rather than the newest. It did once.
    result = run_installer(tmp_path, LISTING, "--pre")

    assert "Installing stenos v0.2.1.4" in result.stdout, result.stderr


@needs_a_shell
def test_a_version_given_by_hand_is_not_looked_up(tmp_path: Path) -> None:
    # Nothing is fetched, so the payload would be wrong if it were read.
    result = run_installer(tmp_path, LATEST, "v0.1.6.7")

    assert "Installing stenos v0.1.6.7" in result.stdout, result.stderr


@needs_a_shell
def test_an_unresolved_version_does_not_claim_to_know_why(tmp_path: Path) -> None:
    # It said every release was a pre-release, which was true for the alphas
    # and stops being true the moment a beta is published. After that the
    # message could only appear for some other reason while naming that one.
    result = run_installer(tmp_path, "")

    assert "cannot work out the newest stable release" in result.stderr
    assert "every release" not in result.stderr.lower()


def test_neither_script_claims_every_release_is_a_pre_release() -> None:
    for name in ("install.sh", "install.ps1"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        failure = text[text.index("cannot work out the newest stable release") :]

        assert "Every release so far" not in failure, name
