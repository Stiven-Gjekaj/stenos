"""Backend selection across platforms.

mlx runs on Apple Silicon only. Every other supported target falls back to
faster-whisper, and a backend that is selected but not installed must report an
instruction rather than an import traceback.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest

from stenos.config import (
    BACKEND_AUTO,
    BACKEND_FASTER_WHISPER,
    BACKEND_MLX,
    BUNDLED_BACKEND_FILE,
    ConfigError,
    bundled_backend,
    resolve_backend,
)
from stenos.transcribe import BackendUnavailableError, load_backend


def test_apple_silicon_selects_mlx() -> None:
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
def test_other_targets_select_a_non_mlx_backend(system: str, machine: str) -> None:
    resolved = resolve_backend(BACKEND_AUTO, system=system, machine=machine)

    assert resolved != BACKEND_MLX
    assert resolved == BACKEND_FASTER_WHISPER


def test_the_running_platform_resolves_to_the_expected_backend() -> None:
    resolved = resolve_backend(BACKEND_AUTO)
    is_apple_silicon = platform.system() == "Darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }

    assert resolved == (BACKEND_MLX if is_apple_silicon else BACKEND_FASTER_WHISPER)


def test_an_unknown_backend_reports_the_supported_names() -> None:
    with pytest.raises(ConfigError) as error:
        resolve_backend("whisper.cpp")

    assert "faster-whisper" in str(error.value)
    assert "mlx" in str(error.value)


def test_a_missing_backend_reports_an_instruction_not_a_traceback() -> None:
    # Neither optional backend is installed in the compatibility environment,
    # which is exactly the situation an operator hits on a fresh checkout.
    with pytest.raises(BackendUnavailableError) as error:
        load_backend(BACKEND_MLX, "small")

    message = str(error.value)
    assert "uv sync --extra mlx" in message
    assert "Traceback" not in message


def test_a_missing_faster_whisper_reports_an_instruction() -> None:
    with pytest.raises(BackendUnavailableError) as error:
        load_backend(BACKEND_FASTER_WHISPER, "small")

    assert "uv sync --extra cuda" in str(error.value)


def test_the_error_is_catchable_as_a_runtime_error() -> None:
    # Callers should be able to handle it without importing the module.
    with pytest.raises(RuntimeError):
        load_backend(BACKEND_MLX, "small")


def test_a_frozen_build_resolves_to_the_backend_it_carries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The failure this prevents: on Apple Silicon the platform rule chooses
    # mlx, but the executable bundles faster-whisper, so it would ask for a
    # backend that cannot be present.
    (tmp_path / BUNDLED_BACKEND_FILE).write_text("faster-whisper\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert bundled_backend() == BACKEND_FASTER_WHISPER
    assert resolve_backend(BACKEND_AUTO, system="Darwin", machine="arm64") == (
        BACKEND_FASTER_WHISPER
    )


def test_an_explicit_setting_still_overrides_the_bundled_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / BUNDLED_BACKEND_FILE).write_text("faster-whisper\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert resolve_backend(BACKEND_MLX) == BACKEND_MLX


def test_a_missing_or_unusable_marker_falls_back_to_the_platform_rule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert bundled_backend() is None
    assert resolve_backend(BACKEND_AUTO, system="Darwin", machine="arm64") == BACKEND_MLX

    (tmp_path / BUNDLED_BACKEND_FILE).write_text("nonsense\n", encoding="utf-8")
    assert bundled_backend() is None


def test_an_ordinary_installation_reports_no_bundled_backend() -> None:
    assert bundled_backend() is None
