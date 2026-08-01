"""Tests for platform-aware backend selection."""

from __future__ import annotations

import pytest

from stenos.config import (
    BACKEND_AUTO,
    BACKEND_FASTER_WHISPER,
    BACKEND_MLX,
    ConfigError,
    resolve_backend,
)


def test_apple_silicon_resolves_to_mlx() -> None:
    assert resolve_backend(BACKEND_AUTO, system="Darwin", machine="arm64") == BACKEND_MLX


@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("Linux", "x86_64"),
        ("Linux", "aarch64"),
        ("Windows", "AMD64"),
        ("Darwin", "x86_64"),
    ],
)
def test_other_platforms_resolve_to_faster_whisper(system: str, machine: str) -> None:
    assert resolve_backend(BACKEND_AUTO, system=system, machine=machine) == BACKEND_FASTER_WHISPER


def test_explicit_backend_is_not_overridden_by_platform() -> None:
    assert resolve_backend(BACKEND_MLX, system="Linux", machine="x86_64") == BACKEND_MLX
    assert (
        resolve_backend(BACKEND_FASTER_WHISPER, system="Darwin", machine="arm64")
        == BACKEND_FASTER_WHISPER
    )


def test_machine_comparison_is_case_insensitive() -> None:
    assert resolve_backend(BACKEND_AUTO, system="Darwin", machine="ARM64") == BACKEND_MLX


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(ConfigError, match="Unknown transcription backend"):
        resolve_backend("vosk")


def test_error_message_lists_supported_backends() -> None:
    with pytest.raises(ConfigError, match="faster-whisper"):
        resolve_backend("whisper.cpp")


def test_resolution_without_injected_platform_returns_a_concrete_backend() -> None:
    assert resolve_backend(BACKEND_AUTO) in {BACKEND_MLX, BACKEND_FASTER_WHISPER}
