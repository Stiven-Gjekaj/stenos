"""Tests for platform-aware backend selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from stenos import config as config_module
from stenos.config import (
    BACKEND_AUTO,
    BACKEND_FASTER_WHISPER,
    BACKEND_MLX,
    BUNDLED_BACKEND_FILE,
    ConfigError,
    bundled_backend,
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


@pytest.mark.parametrize(
    "marker", ["faster-whisper", "faster_whisper", "FASTER-WHISPER", " mlx \n"]
)
def test_a_frozen_build_reads_its_marker_however_it_is_spelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    # A marker matched raw used to read as no answer at all, and no answer
    # means the platform decides. On Apple Silicon that is mlx, which the
    # executable does not carry, which is what this exists to prevent.
    (tmp_path / BUNDLED_BACKEND_FILE).write_text(marker, encoding="utf-8")
    monkeypatch.setattr(config_module, "bundle_directory", lambda: tmp_path)

    assert bundled_backend() == marker.strip().lower().replace("_", "-")


def test_a_frozen_build_with_an_unreadable_marker_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_module, "bundle_directory", lambda: tmp_path)

    assert bundled_backend() is None


@pytest.mark.parametrize("marker", ["vosk", "auto", ""])
def test_a_marker_naming_no_usable_backend_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    # auto is not an answer, and a name nothing recognises is not one either.
    (tmp_path / BUNDLED_BACKEND_FILE).write_text(marker, encoding="utf-8")
    monkeypatch.setattr(config_module, "bundle_directory", lambda: tmp_path)

    assert bundled_backend() is None


def test_a_frozen_build_resolves_to_what_it_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Apple Silicon would otherwise choose mlx, which the executable does not
    # bundle, and ask for a backend that cannot be there.
    (tmp_path / BUNDLED_BACKEND_FILE).write_text("faster-whisper\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "bundle_directory", lambda: tmp_path)

    resolved = resolve_backend("auto", system="Darwin", machine="arm64")

    assert resolved == BACKEND_FASTER_WHISPER
