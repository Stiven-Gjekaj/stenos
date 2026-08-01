"""Shared marking for the cross-platform compatibility suite.

Every test in this directory carries the compat marker, so the default run
excludes them and `pytest tests/compat -m compat` selects them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_COMPAT_DIRECTORY = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # This hook is global: a conftest in a subdirectory still receives every
    # collected item, so the path is checked rather than marking everything.
    for item in items:
        path = Path(str(item.fspath))
        if path == _COMPAT_DIRECTORY or _COMPAT_DIRECTORY in path.parents:
            item.add_marker(pytest.mark.compat)
