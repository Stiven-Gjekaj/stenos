"""Tests for the numbers the README states about this repository.

Every one of them is a count of something that changes whenever the code does:
how many tests there are, how long each source file is. Nothing recomputes
them, so each is correct only until the next commit, and wrong quietly. The
release badge had the same shape and read `none` for the project's whole life.

These ask rather than remember. A count that drifts fails here, in the commit
that drifted it, rather than being noticed by a reader months later.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "stenos"
CHANGELOG = ROOT / "CHANGELOG.md"

# The wheel carries the package and a copy of the tests, but no README and no
# sources to measure. platforms.yml runs the suite from exactly that, so there
# is nothing here for it to check.
if not (ROOT / "README.md").is_file() or not SOURCE.is_dir():
    pytest.skip(
        "not a repository checkout, so there is no README to check",
        allow_module_level=True,
    )


def readme() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8")


def lines_in(path: Path) -> int:
    """Lines in one file, counted the way the README's table counts them."""
    return len(path.read_text(encoding="utf-8").splitlines())


def collected_tests() -> int:
    """How many tests this repository has, asked of pytest rather than counted.

    Collection alone, so this does not run the suite from inside itself. A
    plain run deselects the compat marker and prints both numbers, of which
    the one after the slash is the total.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    found = re.search(r"(?:\d+/)?(\d+) tests? collected", completed.stdout)
    assert found is not None, completed.stdout[-2000:] + completed.stderr[-2000:]
    return int(found.group(1))


def test_the_test_count_badge_is_current() -> None:
    stated = re.search(r"badge/tests-(\d+)_passing", readme())

    assert stated is not None, "the tests badge is no longer a count"
    assert int(stated.group(1)) == collected_tests()


def test_the_changelog_reads_newest_first() -> None:
    # It did not. Each of 0.2.0 and 0.2.1 was inserted above the section that
    # was newest when it was written, which is the section below the one it
    # should have gone above, so the two arrived in the order they were cut
    # rather than the reverse of it. A reader takes the top entry as current.
    headings = re.findall(r"^## (\d+)\.(\d+)\.(\d+)", CHANGELOG.read_text(encoding="utf-8"), re.M)
    versions = [tuple(int(part) for part in heading) for heading in headings]

    assert versions == sorted(versions, reverse=True), versions


#: One row of the source table: a module and the length claimed for it.
TABLE_ROW = re.compile(r"^\|[^|]*\|\s*(\w+\.py)\s*\|\s*(\d+)\s*\|", re.MULTILINE)

#: Its closing row, which counts every module rather than only the listed ones.
TOTAL_ROW = re.compile(
    r"^\|\s*\*\*Total\*\*\s*\|\s*\*\*(\d+) files\*\*\s*\|\s*\*\*(\d+)\*\*\s*"
    r"\|\s*Plus (\d+) lines of tests",
    re.MULTILINE,
)


def test_the_source_table_lists_files_that_exist() -> None:
    rows = TABLE_ROW.findall(readme())

    assert rows, "the source table has no rows, or no longer parses"
    for name, _length in rows:
        assert (SOURCE / name).is_file(), f"{name} is in the table and not on disk"


@pytest.mark.parametrize(("name", "stated"), TABLE_ROW.findall(readme()))
def test_each_stated_file_length_is_current(name: str, stated: str) -> None:
    assert int(stated) == lines_in(SOURCE / name)


def test_the_stated_totals_are_current() -> None:
    # The totals count every module, including the two too short to be worth a
    # row of their own, which is why they do not add up to the rows above.
    total = TOTAL_ROW.search(readme())
    assert total is not None, "the total row no longer parses"

    files, source_lines, test_lines = (int(part) for part in total.groups())
    modules = sorted(SOURCE.glob("*.py"))

    assert files == len(modules)
    assert source_lines == sum(lines_in(path) for path in modules)
    assert test_lines == sum(lines_in(path) for path in sorted(Path(ROOT / "tests").rglob("*.py")))


def module_exports() -> dict[str, set[str]]:
    """What each module of the package declares in ``__all__``."""
    exports: dict[str, set[str]] = {}
    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                getattr(target, "id", "") == "__all__" for target in node.targets
            ):
                names = {element.value for element in node.value.elts}  # type: ignore[attr-defined]
                exports[path.stem] = names
    return exports


def test_every_name_one_module_takes_from_another_is_exported() -> None:
    # backend_status and OPUS_PATH_VARIABLE were both imported by bot.py and
    # absent from the __all__ of the module they came from. Nothing breaks
    # while the import is spelled out, so the lists drifted from what they
    # describe and stopped being an account of the surface between modules.
    exports = module_exports()
    missing: list[str] = []

    for path in sorted(SOURCE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or node.level != 1 or node.module is None:
                continue
            declared = exports.get(node.module)
            if declared is None:
                continue
            missing += [
                f"{path.name} takes {alias.name} from {node.module}, which does not export it"
                for alias in node.names
                if alias.name not in declared
            ]

    assert not missing, "\n".join(missing)


#: How many the project actually declares.
_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six"}


def declared_dependencies() -> int:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return len(declared["project"]["dependencies"])


def test_the_dependency_badge_counts_the_dependencies() -> None:
    # It read three while pyproject declared four. certifi was added when a
    # frozen build turned out to carry no certificate store, and the badge was
    # not part of that change, so it undercounted from then on.
    stated = re.search(r"badge/dependencies-(\d+)_direct", readme())

    assert stated is not None, "the dependencies badge is no longer a count"
    assert int(stated.group(1)) == declared_dependencies()


def test_the_contributing_guide_counts_the_dependencies() -> None:
    # Written out in words there, and stale for the same reason and since the
    # same commit. It is the sentence asking contributors not to add more.
    guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    stated = re.search(r"Stenos has (\w+) direct runtime dependencies", guide)

    assert stated is not None, "the guide no longer states a count"
    assert stated.group(1) == _WORDS[declared_dependencies()]
