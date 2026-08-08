"""Tests for environment parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from stenos.config import (
    BACKEND_AUTO,
    BACKEND_FASTER_WHISPER,
    BACKEND_MLX,
    Config,
    ConfigError,
    load_config,
)

MINIMAL_ENV = {"DISCORD_TOKEN": "token-value"}


def test_token_is_required() -> None:
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_config({})


def test_blank_token_is_treated_as_missing() -> None:
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_config({"DISCORD_TOKEN": "   "})


def test_defaults_match_the_documented_values() -> None:
    config = load_config(MINIMAL_ENV)

    assert config.discord_token == "token-value"
    assert config.guild_id is None
    assert config.whisper_backend == BACKEND_AUTO
    assert config.whisper_model == "small"
    assert config.language is None
    assert config.segment_gap == pytest.approx(0.4)
    assert config.min_segment == pytest.approx(0.3)
    assert config.keep_audio is False
    assert config.output_dir == Path("transcripts")


def test_values_are_read_from_the_mapping() -> None:
    config = load_config(
        {
            "DISCORD_TOKEN": "abc",
            "GUILD_ID": "1234567890",
            "WHISPER_BACKEND": "faster-whisper",
            "WHISPER_MODEL": "medium",
            "LANGUAGE": "sq",
            "SEGMENT_GAP": "0.75",
            "MIN_SEGMENT": "0.5",
            "KEEP_AUDIO": "true",
        }
    )

    assert config.guild_id == 1234567890
    assert config.whisper_backend == BACKEND_FASTER_WHISPER
    assert config.whisper_model == "medium"
    assert config.language == "sq"
    assert config.segment_gap == pytest.approx(0.75)
    assert config.min_segment == pytest.approx(0.5)
    assert config.keep_audio is True


def test_language_auto_becomes_none() -> None:
    assert load_config({**MINIMAL_ENV, "LANGUAGE": "auto"}).language is None
    assert load_config({**MINIMAL_ENV, "LANGUAGE": "AUTO"}).language is None
    assert load_config({**MINIMAL_ENV, "LANGUAGE": ""}).language is None


def test_language_is_normalised_to_lower_case() -> None:
    assert load_config({**MINIMAL_ENV, "LANGUAGE": "EN"}).language == "en"


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_truthy_boolean_spellings(value: str) -> None:
    assert load_config({**MINIMAL_ENV, "KEEP_AUDIO": value}).keep_audio is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_falsy_boolean_spellings(value: str) -> None:
    assert load_config({**MINIMAL_ENV, "KEEP_AUDIO": value}).keep_audio is False


def test_invalid_boolean_is_rejected() -> None:
    with pytest.raises(ConfigError, match="KEEP_AUDIO"):
        load_config({**MINIMAL_ENV, "KEEP_AUDIO": "maybe"})


def test_invalid_guild_id_is_rejected() -> None:
    with pytest.raises(ConfigError, match="GUILD_ID"):
        load_config({**MINIMAL_ENV, "GUILD_ID": "not-a-number"})


def test_invalid_float_is_rejected() -> None:
    with pytest.raises(ConfigError, match="SEGMENT_GAP"):
        load_config({**MINIMAL_ENV, "SEGMENT_GAP": "soon"})


def test_negative_gap_is_rejected() -> None:
    with pytest.raises(ConfigError, match="at least"):
        load_config({**MINIMAL_ENV, "SEGMENT_GAP": "-1"})


def test_unknown_backend_is_rejected() -> None:
    with pytest.raises(ConfigError, match="WHISPER_BACKEND"):
        load_config({**MINIMAL_ENV, "WHISPER_BACKEND": "vosk"})


def test_backend_underscore_alias_is_accepted() -> None:
    config = load_config({**MINIMAL_ENV, "WHISPER_BACKEND": "faster_whisper"})
    assert config.whisper_backend == BACKEND_FASTER_WHISPER


def test_backend_is_case_insensitive() -> None:
    assert load_config({**MINIMAL_ENV, "WHISPER_BACKEND": "MLX"}).whisper_backend == BACKEND_MLX


def test_blank_optional_values_fall_back_to_defaults() -> None:
    config = load_config(
        {
            "DISCORD_TOKEN": "abc",
            "GUILD_ID": "",
            "WHISPER_MODEL": "",
            "SEGMENT_GAP": "",
        }
    )

    assert config.guild_id is None
    assert config.whisper_model == "small"
    assert config.segment_gap == pytest.approx(0.4)


def test_config_is_immutable() -> None:
    config = load_config(MINIMAL_ENV)
    with pytest.raises((AttributeError, TypeError)):
        config.whisper_model = "large-v3"  # type: ignore[misc]


def test_config_can_be_constructed_directly(tmp_path: Path) -> None:
    config = Config(discord_token="abc", output_dir=tmp_path)
    assert config.output_dir == tmp_path
    assert config.whisper_backend == BACKEND_AUTO


def test_the_segment_and_buffer_limits_have_defaults() -> None:
    config = load_config(MINIMAL_ENV)

    assert config.max_segment == pytest.approx(30.0)
    assert config.max_buffer_mb == pytest.approx(1024.0)


def test_the_segment_and_buffer_limits_are_read_from_the_environment() -> None:
    config = load_config(MINIMAL_ENV | {"MAX_SEGMENT": "12.5", "MAX_BUFFER_MB": "256"})

    assert config.max_segment == pytest.approx(12.5)
    assert config.max_buffer_mb == pytest.approx(256.0)


def test_a_zero_buffer_limit_is_accepted_as_no_limit() -> None:
    # A host with the memory for it, and a reason to use it.
    assert load_config(MINIMAL_ENV | {"MAX_BUFFER_MB": "0"}).max_buffer_mb == 0.0


def test_the_disconnect_grace_has_a_default_and_is_read_from_the_environment() -> None:
    assert load_config(MINIMAL_ENV).disconnect_grace == pytest.approx(60.0)
    assert load_config(MINIMAL_ENV | {"DISCONNECT_GRACE": "15"}).disconnect_grace == pytest.approx(
        15.0
    )


def test_a_zero_disconnect_grace_is_accepted_as_waiting_forever() -> None:
    assert load_config(MINIMAL_ENV | {"DISCONNECT_GRACE": "0"}).disconnect_grace == 0.0


def test_a_segment_length_of_zero_is_rejected() -> None:
    # It would close a segment on every packet, and each one would then be
    # discarded for being shorter than the minimum.
    with pytest.raises(ConfigError, match="MAX_SEGMENT"):
        load_config(MINIMAL_ENV | {"MAX_SEGMENT": "0"})


def test_the_output_directory_defaults_and_is_read_from_the_environment() -> None:
    # The field existed and was plumbed through the whole pipeline, and nothing
    # ever read it from anywhere, so the only way to move transcripts off the
    # default was to edit the source.
    assert load_config(MINIMAL_ENV).output_dir == Path("transcripts")
    assert load_config(MINIMAL_ENV | {"OUTPUT_DIR": "calls"}).output_dir == Path("calls")


def test_a_leading_tilde_in_the_output_directory_is_expanded() -> None:
    # Otherwise a literal directory named "~" appears in the working directory,
    # which is never what anybody meant.
    expanded = load_config(MINIMAL_ENV | {"OUTPUT_DIR": "~/calls"}).output_dir

    assert not str(expanded).startswith("~")
    assert expanded == Path.home() / "calls"
