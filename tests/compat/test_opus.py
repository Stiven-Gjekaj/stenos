"""Opus availability.

Voice receive does not work without libopus. py-cord bundles binaries for
macOS and Windows; on Linux the library comes from the system, which is the
most common platform failure for a voice bot and one that surfaces at runtime
rather than at import.
"""

from __future__ import annotations

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
            "libopus is not available. On Linux install libopus0 and libsodium23; "
            "macOS and Windows use the binaries bundled with py-cord."
        )

    assert discord.opus.is_loaded() is True


def test_voice_receive_dependencies_are_importable() -> None:
    # PyNaCl is required to decrypt incoming voice packets.
    import nacl.secret

    assert nacl.secret.SecretBox is not None
