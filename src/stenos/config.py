"""Runtime configuration loaded from the environment or a .env file."""

from __future__ import annotations

import os
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
    "load_config",
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
