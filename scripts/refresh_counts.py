"""Rewrite the counts the README states about this repository.

The README claims how many tests there are, how long each source file is, and
what those add up to. Every one of them changes whenever the code does, and
`tests/test_documentation.py` fails the commit that lets one drift. This is
what makes them current again, so the answer to that failure is a command
rather than arithmetic by hand.

    uv run python scripts/refresh_counts.py

Pass ``--check`` to report what is stale and write nothing, which is what a
hook wants. It counts exactly what the documentation tests count, so the two
agree or one of them fails.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "stenos"
TESTS = ROOT / "tests"
README = ROOT / "README.md"

#: The tests badge, and the alt text beside it that says the same number.
BADGE = re.compile(r"badge/tests-(\d+)_passing")
BADGE_ALT = re.compile(r'alt="(\d+) tests passing"')

#: One row of the source table, split so only the length is rewritten and the
#: column widths survive. The anchors match test_documentation.py's own.
TABLE_ROW = re.compile(r"^(\|[^|]*\|\s*)(\w+\.py)(\s*\|\s*)(\d+)(\s*\|)", re.MULTILINE)

#: Its closing row, which counts every module rather than only the listed ones.
TOTAL_ROW = re.compile(
    r"^(\|\s*\*\*Total\*\*\s*\|\s*\*\*)(\d+)( files\*\*\s*\|\s*\*\*)(\d+)(\*\*\s*"
    r"\|\s*Plus )(\d+)( lines of tests)",
    re.MULTILINE,
)


def lines_in(path: Path) -> int:
    """Lines in one file, counted the way the README's table counts them."""
    return len(path.read_text(encoding="utf-8").splitlines())


def collected_tests() -> int:
    """How many tests this repository has, asked of pytest rather than counted.

    Collection alone, so nothing runs the suite to count it. A plain run
    deselects the compat marker and prints both numbers, of which the one after
    the slash is the total.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    found = re.search(r"(?:\d+/)?(\d+) tests? collected", completed.stdout)
    if found is None:
        raise SystemExit(
            "could not collect the tests, so their count is unknown:\n"
            + completed.stdout[-3000:]
            + completed.stderr[-3000:]
        )
    return int(found.group(1))


def refreshed(text: str) -> tuple[str, list[str]]:
    """The README with every count current, and a line per count that moved."""
    moved: list[str] = []

    def note(what: str, stated: str, actual: int) -> None:
        if int(stated) != actual:
            moved.append(f"{what}: {stated} -> {actual}")

    tests = collected_tests()
    for pattern in (BADGE, BADGE_ALT):
        found = pattern.search(text)
        if found is not None:
            note("tests", found.group(1), tests)
    text = BADGE.sub(f"badge/tests-{tests}_passing", text)
    text = BADGE_ALT.sub(f'alt="{tests} tests passing"', text)

    def one_row(match: re.Match[str]) -> str:
        actual = lines_in(SOURCE / match.group(2))
        note(match.group(2), match.group(4), actual)
        return f"{match.group(1)}{match.group(2)}{match.group(3)}{actual}{match.group(5)}"

    text = TABLE_ROW.sub(one_row, text)

    modules = sorted(SOURCE.glob("*.py"))
    source_lines = sum(lines_in(path) for path in modules)
    test_lines = sum(lines_in(path) for path in sorted(TESTS.rglob("*.py")))

    def total(match: re.Match[str]) -> str:
        note("files", match.group(2), len(modules))
        note("source lines", match.group(4), source_lines)
        note("test lines", match.group(6), test_lines)
        return (
            f"{match.group(1)}{len(modules)}{match.group(3)}{source_lines}"
            f"{match.group(5)}{test_lines}{match.group(7)}"
        )

    text = TOTAL_ROW.sub(total, text)
    return text, moved


def main(argv: list[str] | None = None) -> int:
    checking = "--check" in (argv if argv is not None else sys.argv[1:])
    before = README.read_text(encoding="utf-8")
    after, moved = refreshed(before)

    if not moved:
        print("Every stated count is current.")
        return 0

    for line in moved:
        print(line)
    if checking:
        print("Run scripts/refresh_counts.py to bring the README up to date.")
        return 1
    README.write_text(after, encoding="utf-8")
    print(f"Rewrote {README.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
