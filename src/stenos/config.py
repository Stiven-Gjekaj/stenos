"""Runtime configuration loaded from the environment or a .env file."""

from __future__ import annotations

import os
import platform
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

__all__ = [
    "BACKEND_AUTO",
    "BACKEND_FASTER_WHISPER",
    "BACKEND_MLX",
    "Config",
    "ConfigError",
    "bundle_directory",
    "bundled_backend",
    "load_config",
    "resolve_backend",
]

BACKEND_MLX = "mlx"
BACKEND_FASTER_WHISPER = "faster-whisper"
BACKEND_AUTO = "auto"

VALID_BACKENDS = frozenset({BACKEND_MLX, BACKEND_FASTER_WHISPER, BACKEND_AUTO})

DEFAULT_BACKEND = BACKEND_AUTO
DEFAULT_MODEL = "small"
DEFAULT_SEGMENT_GAP = 0.4
DEFAULT_MIN_SEGMENT = 0.3
DEFAULT_OUTPUT_DIR = Path("transcripts")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """Raised when the environment holds a missing or unusable setting."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated settings for one bot process."""

    discord_token: str
    guild_id: int | None = None
    whisper_backend: str = DEFAULT_BACKEND
    whisper_model: str = DEFAULT_MODEL
    language: str | None = None
    segment_gap: float = DEFAULT_SEGMENT_GAP
    min_segment: float = DEFAULT_MIN_SEGMENT
    keep_audio: bool = False
    output_dir: Path = field(default=DEFAULT_OUTPUT_DIR)


_APPLE_SILICON_MACHINES = frozenset({"arm64", "aarch64"})

#: Name of the file a frozen build carries to record which backend it bundles.
BUNDLED_BACKEND_FILE = "BUNDLED_BACKEND"


def bundle_directory() -> Path | None:
    """Directory holding files bundled into a frozen executable, if frozen.

    A one file build unpacks its payload to a temporary directory recorded in
    sys._MEIPASS. Returns None when running from a normal installation.
    """
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def bundled_backend() -> str | None:
    """Return the backend a frozen build carries, or None if not frozen.

    A frozen build contains exactly one backend, chosen when it was built. That
    is not necessarily the one this platform would otherwise prefer.
    """
    bundle = bundle_directory()
    if bundle is None:
        return None
    try:
        name = (bundle / BUNDLED_BACKEND_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if name in VALID_BACKENDS and name != BACKEND_AUTO:
        return name
    return None


def resolve_backend(
    name: str = BACKEND_AUTO,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    """Resolve a backend name to a concrete backend for a given platform.

    The platform is injected rather than read directly so that selection can be
    asserted for every supported target from a single test runner. mlx runs only
    on Apple Silicon; every other platform falls back to faster-whisper.

    A frozen executable resolves to the backend it actually carries. Apple
    Silicon would otherwise select mlx, which the executable does not bundle,
    and ask for a backend that cannot be there. An explicit setting still wins,
    so anyone who knows better can override it.
    """
    normalised = name.strip().lower().replace("_", "-")
    if normalised not in VALID_BACKENDS:
        supported = ", ".join(sorted(VALID_BACKENDS))
        raise ConfigError(f"Unknown transcription backend {name!r}. Supported: {supported}.")
    if normalised != BACKEND_AUTO:
        return normalised

    bundled = bundled_backend()
    if bundled is not None:
        return bundled

    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system == "Darwin" and machine.lower() in _APPLE_SILICON_MACHINES:
        return BACKEND_MLX
    return BACKEND_FASTER_WHISPER


def _lookup(env: Mapping[str, str], key: str) -> str | None:
    """Return a stripped value for key, or None when unset or blank."""
    raw = env.get(key)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _read_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _lookup(env, key)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ConfigError(f"{key} must be a boolean value, got {raw!r}")


def _read_float(env: Mapping[str, str], key: str, default: float, *, minimum: float) -> float:
    raw = _lookup(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be at least {minimum}, got {value}")
    return value


def _read_guild_id(env: Mapping[str, str]) -> int | None:
    raw = _lookup(env, "GUILD_ID")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"GUILD_ID must be an integer, got {raw!r}") from exc


def _read_backend(env: Mapping[str, str]) -> str:
    raw = _lookup(env, "WHISPER_BACKEND")
    if raw is None:
        return DEFAULT_BACKEND
    normalised = raw.strip().lower().replace("_", "-")
    if normalised not in VALID_BACKENDS:
        supported = ", ".join(sorted(VALID_BACKENDS))
        raise ConfigError(f"WHISPER_BACKEND must be one of {supported}, got {raw!r}")
    return normalised


def _read_language(env: Mapping[str, str]) -> str | None:
    raw = _lookup(env, "LANGUAGE")
    if raw is None or raw.lower() == BACKEND_AUTO:
        return None
    return raw.lower()


def load_config(env: Mapping[str, str] | None = None, *, use_dotenv: bool = True) -> Config:
    """Build a Config from a mapping, defaulting to the process environment.

    Passing an explicit mapping keeps tests independent of the ambient
    environment and of any .env file present in the working directory.
    """
    if env is None:
        if use_dotenv:
            load_dotenv()
        env = os.environ

    token = _lookup(env, "DISCORD_TOKEN")
    if token is None:
        raise ConfigError("DISCORD_TOKEN is required. Copy .env.example to .env and set it.")

    model = _lookup(env, "WHISPER_MODEL") or DEFAULT_MODEL

    return Config(
        discord_token=token,
        guild_id=_read_guild_id(env),
        whisper_backend=_read_backend(env),
        whisper_model=model,
        language=_read_language(env),
        segment_gap=_read_float(env, "SEGMENT_GAP", DEFAULT_SEGMENT_GAP, minimum=0.0),
        min_segment=_read_float(env, "MIN_SEGMENT", DEFAULT_MIN_SEGMENT, minimum=0.0),
        keep_audio=_read_bool(env, "KEEP_AUDIO", False),
        output_dir=DEFAULT_OUTPUT_DIR,
    )
