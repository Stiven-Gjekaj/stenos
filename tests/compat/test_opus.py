"""Opus availability.

Voice receive does not work without libopus. py-cord bundles a binary for
Windows only; on Linux and macOS it calls ctypes.util.find_library and so
depends on the system package. This is the most common platform failure for a
voice bot, and it surfaces at runtime rather than at import.
"""

from __future__ import annotations

import sys
from pathlib import Path

import discord
import pytest

from stenos.sink import (
    _try_load,
    bundle_directory,
    ensure_opus,
    opus_library_candidates,
)


def load_opus() -> bool:
    """Load opus the way the package does at startup."""
    return ensure_opus()


def test_opus_module_is_importable() -> None:
    assert hasattr(discord.opus, "is_loaded")
    assert hasattr(discord.opus, "load_opus")


def test_opus_loads_after_the_documented_install_steps() -> None:
    if not load_opus():
        pytest.fail(
            "libopus is not available. On Linux install libopus0 and libsodium23, "
            "on macOS run brew install opus. Windows uses the binary bundled with "
            "py-cord and needs nothing further."
        )

    assert discord.opus.is_loaded() is True


def test_only_windows_receives_a_bundled_binary() -> None:
    # Records the upstream behaviour the platform prerequisites depend on. If
    # py-cord starts shipping binaries for other platforms, the documented
    # install steps can be relaxed.
    bundled = list((Path(discord.__file__).parent / "bin").glob("*opus*"))

    if sys.platform == "win32":
        assert bundled, "py-cord should bundle an opus binary for Windows"
    else:
        assert all(item.suffix == ".dll" for item in bundled), (
            "py-cord bundles opus for Windows only; other platforms use the system library"
        )


def test_voice_receive_dependencies_are_importable() -> None:
    # PyNaCl is required to decrypt incoming voice packets.
    import nacl.secret

    assert nacl.secret.SecretBox is not None


def test_candidate_paths_cover_the_homebrew_prefix() -> None:
    # find_library does not search the Homebrew prefix on Apple Silicon, so
    # installing the package is not by itself enough to load the library.
    candidates = opus_library_candidates()

    if sys.platform == "darwin":
        assert any("/opt/homebrew/lib" in candidate for candidate in candidates)
        assert any("/usr/local/lib" in candidate for candidate in candidates)
    elif sys.platform.startswith("linux"):
        assert "libopus.so.0" in candidates
    else:
        assert candidates == []


def test_loading_reports_failure_rather_than_raising() -> None:
    # A missing library must be reported so the caller can print an
    # instruction instead of a traceback. On a host where opus is already
    # loaded this returns True from the early exit, which is also correct.
    assert isinstance(ensure_opus("/nonexistent/libopus.dylib"), bool)


def test_an_unusable_candidate_is_rejected_without_raising() -> None:
    assert _try_load("/nonexistent/libopus.dylib") is False


def test_no_bundle_directory_outside_a_frozen_build() -> None:
    # Running from an installation, not an executable, so there is no payload
    # directory to search.
    assert bundle_directory() is None


def test_a_frozen_build_searches_its_own_payload_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    candidates = opus_library_candidates()

    assert bundle_directory() == tmp_path
    assert candidates, "a frozen build must offer at least one candidate"
    assert str(tmp_path) in candidates[0], "the bundled copy must be tried before the system"
    # Windows ships its binary under discord/bin, so that path is searched too.
    assert any(str(tmp_path / "discord" / "bin") in candidate for candidate in candidates)
