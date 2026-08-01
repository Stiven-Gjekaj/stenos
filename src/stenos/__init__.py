"""Stenos records Discord voice participants separately and transcribes them locally.

The package name is taken from Greek stenos, the root of stenography: a verbatim
record of a multi-party proceeding with every line attributed to a speaker.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("stenos")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    __version__ = "0.0.0.0"
