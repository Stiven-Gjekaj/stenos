"""Tests for backend selection and the segment transcription loop.

No test loads a real model. The mock backend stands in for every real one.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from stenos.audio import TARGET_SAMPLE_RATE
from stenos.sink import BYTES_PER_SECOND, Segment
from stenos.transcribe import (
    MLX_MODEL_REPOS,
    BackendUnavailableError,
    FasterWhisperBackend,
    MLXBackend,
    MockBackend,
    TranscribedSegment,
    backend_status,
    load_backend,
    mlx_repo_for,
    transcribe_segments,
)


def segment_of(seconds: float, *, user_id: int = 1, start: float = 0.0) -> Segment:
    return Segment(user_id=user_id, start=start, pcm=bytearray(int(BYTES_PER_SECOND * seconds)))


def test_every_segment_is_transcribed_in_order() -> None:
    backend = MockBackend(texts=["first", "second", "third"])
    segments = [
        segment_of(1.0, user_id=1, start=0.0),
        segment_of(1.0, user_id=2, start=5.0),
        segment_of(1.0, user_id=1, start=9.0),
    ]

    results = transcribe_segments(segments, backend)

    assert [result.text for result in results] == ["first", "second", "third"]
    assert [result.user_id for result in results] == [1, 2, 1]
    assert [result.start for result in results] == [0.0, 5.0, 9.0]


def test_segment_duration_is_carried_through() -> None:
    results = transcribe_segments([segment_of(2.0)], MockBackend())

    assert results[0].duration == pytest.approx(2.0)


def test_backend_receives_sixteen_kilohertz_audio() -> None:
    backend = MockBackend()

    transcribe_segments([segment_of(1.0)], backend)

    (sample_count, _language) = backend.calls[0]
    assert sample_count == TARGET_SAMPLE_RATE


def test_language_is_forwarded_to_the_backend() -> None:
    backend = MockBackend()

    transcribe_segments([segment_of(1.0)], backend, language="sq")

    assert backend.calls[0][1] == "sq"


def test_language_defaults_to_none_for_automatic_detection() -> None:
    backend = MockBackend()

    transcribe_segments([segment_of(1.0)], backend)

    assert backend.calls[0][1] is None


def test_progress_callback_receives_each_completion() -> None:
    seen: list[tuple[int, int]] = []

    def progress(done: int, total: int) -> None:
        seen.append((done, total))

    transcribe_segments([segment_of(1.0) for _ in range(3)], MockBackend(), progress=progress)

    assert seen == [(1, 3), (2, 3), (3, 3)]


def test_progress_is_optional() -> None:
    assert len(transcribe_segments([segment_of(1.0)], MockBackend())) == 1


def test_empty_input_produces_no_results() -> None:
    backend = MockBackend()

    assert transcribe_segments([], backend) == []
    assert backend.calls == []


def test_surrounding_whitespace_is_stripped() -> None:
    backend = MockBackend(texts=["  padded text \n"])

    results = transcribe_segments([segment_of(1.0)], backend)

    assert results[0].text == "padded text"


def test_empty_results_are_retained_for_the_sidecar() -> None:
    backend = MockBackend(texts=["", "spoken"])

    results = transcribe_segments([segment_of(1.0), segment_of(1.0)], backend)

    assert [result.text for result in results] == ["", "spoken"]


def test_transcribed_segment_is_immutable() -> None:
    result = TranscribedSegment(user_id=1, start=0.0, duration=1.0, text="text")

    with pytest.raises((AttributeError, TypeError)):
        result.text = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("model", sorted(MLX_MODEL_REPOS))
def test_every_mapped_model_resolves_to_a_repository(model: str) -> None:
    repo = mlx_repo_for(model)

    assert repo.startswith("mlx-community/")


def test_medium_uses_the_irregular_upstream_suffix() -> None:
    # Upstream names are not uniform, so the mapping cannot be derived from
    # the model name with a format string.
    assert mlx_repo_for("small") == "mlx-community/whisper-small-mlx"
    assert mlx_repo_for("medium") == "mlx-community/whisper-medium-mlx-fp32"


def test_explicit_repository_path_passes_through() -> None:
    assert mlx_repo_for("someone/custom-whisper") == "someone/custom-whisper"


def test_unmapped_model_reports_the_known_names() -> None:
    with pytest.raises(BackendUnavailableError, match="large-v3"):
        mlx_repo_for("enormous")


def test_missing_mlx_dependency_reports_an_install_instruction() -> None:
    with pytest.raises(BackendUnavailableError, match="uv sync --extra mlx"):
        MLXBackend("small")


def test_missing_faster_whisper_dependency_reports_an_install_instruction() -> None:
    with pytest.raises(BackendUnavailableError, match="uv sync --extra cuda"):
        FasterWhisperBackend("small")


def test_loader_reports_a_clear_error_rather_than_an_import_traceback() -> None:
    # Neither optional backend is installed in this environment, which is the
    # same situation continuous integration runs in.
    with pytest.raises(BackendUnavailableError):
        load_backend("auto", "small", system="Linux", machine="x86_64")

    with pytest.raises(BackendUnavailableError):
        load_backend("auto", "small", system="Darwin", machine="arm64")


def install_fake_module(monkeypatch: pytest.MonkeyPatch, name: str, module: ModuleType) -> None:
    monkeypatch.setitem(sys.modules, name, module)


def test_mlx_backend_calls_through_with_the_mapped_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_transcribe(audio: np.ndarray, **options: object) -> dict[str, object]:
        seen.update(options)
        seen["samples"] = audio.size
        return {"text": "  recognised text "}

    module = ModuleType("mlx_whisper")
    module.transcribe = fake_transcribe  # type: ignore[attr-defined]
    install_fake_module(monkeypatch, "mlx_whisper", module)

    backend = MLXBackend("small")
    text = backend.transcribe(np.zeros(TARGET_SAMPLE_RATE, dtype=np.float32), "en")

    assert text == "recognised text"
    assert seen["path_or_hf_repo"] == "mlx-community/whisper-small-mlx"
    assert seen["language"] == "en"


def test_mlx_backend_omits_language_when_detecting_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_transcribe(audio: np.ndarray, **options: object) -> dict[str, object]:
        seen.update(options)
        return {"text": "text"}

    module = ModuleType("mlx_whisper")
    module.transcribe = fake_transcribe  # type: ignore[attr-defined]
    install_fake_module(monkeypatch, "mlx_whisper", module)

    MLXBackend("small").transcribe(np.zeros(16, dtype=np.float32), None)

    assert "language" not in seen


def test_faster_whisper_backend_consumes_the_segment_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeModel:
        def __init__(self, model: str, device: str, compute_type: str) -> None:
            self.model = model

        def transcribe(
            self, audio: np.ndarray, language: str | None, beam_size: int
        ) -> tuple[object, object]:
            parts = (SimpleNamespace(text=" hello "), SimpleNamespace(text="there "))
            return iter(parts), SimpleNamespace(language=language)

    module = ModuleType("faster_whisper")
    module.WhisperModel = FakeModel  # type: ignore[attr-defined]
    install_fake_module(monkeypatch, "faster_whisper", module)

    backend = FasterWhisperBackend("small")
    text = backend.transcribe(np.zeros(TARGET_SAMPLE_RATE, dtype=np.float32), "en")

    assert text == "hello there"


def test_loader_builds_the_platform_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("mlx_whisper")
    module.transcribe = lambda audio, **options: {"text": ""}  # type: ignore[attr-defined]
    install_fake_module(monkeypatch, "mlx_whisper", module)

    backend = load_backend("auto", "small", system="Darwin", machine="arm64")

    assert backend.name == "mlx"


def test_mock_backend_falls_back_to_its_default_text() -> None:
    backend = MockBackend(texts=["only one"], default="filler")

    results = transcribe_segments([segment_of(1.0), segment_of(1.0)], backend)

    assert [result.text for result in results] == ["only one", "filler"]


def test_backend_status_reports_a_missing_dependency() -> None:
    resolved, available, detail = backend_status("auto", system="Linux", machine="x86_64")

    assert resolved == "faster-whisper"
    assert available is False
    assert "uv sync --extra cuda" in detail


def test_backend_status_reports_an_importable_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("mlx_whisper")
    module.transcribe = lambda audio, **options: {"text": ""}  # type: ignore[attr-defined]
    install_fake_module(monkeypatch, "mlx_whisper", module)

    resolved, available, detail = backend_status("auto", system="Darwin", machine="arm64")

    assert resolved == "mlx"
    assert available is True
    assert detail == "installed"


def test_backend_status_never_constructs_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # Constructing one would download weights, which a status probe must not do.
    constructed: list[str] = []

    class ExplodingModel:
        def __init__(self, *args: object, **kwargs: object) -> None:
            constructed.append("built")
            raise AssertionError("backend_status must not construct a model")

    module = ModuleType("faster_whisper")
    module.WhisperModel = ExplodingModel  # type: ignore[attr-defined]
    install_fake_module(monkeypatch, "faster_whisper", module)

    _resolved, available, _detail = backend_status("auto", system="Linux", machine="x86_64")

    assert available is True
    assert constructed == []
