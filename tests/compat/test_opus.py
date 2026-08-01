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


def load_opus() -> bool:
    """Attempt the same load py-cord performs when a voice client is created."""
    if discord.opus.is_loaded():
        return True
    try:
        discord.opus._load_default()
    except Exception:
        # Any failure to load means opus is unavailable on this host.
        return False
    return discord.opus.is_loaded()


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
