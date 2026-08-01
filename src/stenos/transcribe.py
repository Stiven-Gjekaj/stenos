"""Transcription backends and the loop that drives them over recorded segments.

Every backend keeps its model resident for the lifetime of the object. A call
produces hundreds of short segments, and reloading a model per segment, or
shelling out to a separate binary, would dominate total runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from .audio import segment_to_audio
from .config import BACKEND_AUTO, BACKEND_MLX, resolve_backend
from .sink import Segment

__all__ = [
    "MLX_MODEL_REPOS",
    "BackendUnavailableError",
    "FasterWhisperBackend",
    "MLXBackend",
    "MockBackend",
    "ProgressCallback",
    "TranscribedSegment",
    "TranscriptionBackend",
    "load_backend",
    "mlx_repo_for",
    "transcribe_segments",
]

#: Hugging Face repositories holding the converted weights for each model size.
#: The names are not uniformly suffixed upstream, so the mapping is explicit
#: rather than derived from the model name.
MLX_MODEL_REPOS = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "tiny.en": "mlx-community/whisper-tiny.en-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "base.en": "mlx-community/whisper-base.en-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "medium": "mlx-community/whisper-medium-mlx-fp32",
    "medium.en": "mlx-community/whisper-medium.en-mlx-fp32",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

#: Called with the number of segments completed and the total, after each one.
ProgressCallback = Callable[[int, int], None]


class BackendUnavailableError(RuntimeError):
    """Raised when the selected backend cannot be used on this machine.

    Carries an instruction for installing the missing dependency, so an
    operator sees a usable message rather than an import traceback.
    """


class TranscriptionBackend(Protocol):
    """A loaded speech recognition model that transcribes 16 kHz mono float32 audio."""

    name: str

    def transcribe(
        self,
        audio: npt.NDArray[np.float32],
        language: str | None = None,
    ) -> str:
        """Return the text spoken in one segment, or an empty string for silence."""
        ...


@dataclass(frozen=True, slots=True)
class TranscribedSegment:
    """One segment paired with the text recognised in it."""

    user_id: int
    start: float
    duration: float
    text: str


@dataclass
class MockBackend:
    """Deterministic backend used by the test suite and the compatibility checks.

    Ships with the package rather than the tests so the offline pipeline can be
    exercised against an installed wheel without any model weights present.
    """

    texts: Sequence[str] = ()
    default: str = "mock transcript"
    name: str = "mock"
    calls: list[tuple[int, str | None]] = field(default_factory=list)

    def transcribe(
        self,
        audio: npt.NDArray[np.float32],
        language: str | None = None,
    ) -> str:
        index = len(self.calls)
        self.calls.append((int(audio.size), language))
        if index < len(self.texts):
            return self.texts[index]
        return self.default


def mlx_repo_for(model: str) -> str:
    """Map a model size to its converted repository, passing through explicit paths."""
    if "/" in model:
        return model
    try:
        return MLX_MODEL_REPOS[model]
    except KeyError:
        known = ", ".join(sorted(MLX_MODEL_REPOS))
        raise BackendUnavailableError(
            f"No mlx weights are mapped for model {model!r}. "
            f"Use one of: {known}. A Hugging Face repository path is also accepted."
        ) from None


class MLXBackend:
    """Apple Silicon backend built on mlx-whisper.

    mlx-whisper caches the loaded model against the repository path, so passing
    a stable path keeps the weights resident across every segment in a call.
    """

    name = "mlx"

    def __init__(self, model: str = "small") -> None:
        self.model = model
        self.repo = mlx_repo_for(model)
        self._transcribe = _load_mlx_whisper()

    def transcribe(
        self,
        audio: npt.NDArray[np.float32],
        language: str | None = None,
    ) -> str:
        options: dict[str, object] = {"path_or_hf_repo": self.repo}
        if language is not None:
            options["language"] = language
        result = self._transcribe(audio, **options)
        return str(result.get("text", "")).strip()


def _load_mlx_whisper() -> Callable[..., dict[str, object]]:
    try:
        import mlx_whisper
    except ImportError as exc:
        raise BackendUnavailableError(
            "The mlx backend requires mlx-whisper, which is not installed. "
            "Install it with: uv sync --extra mlx. "
            "mlx runs on Apple Silicon only; use WHISPER_BACKEND=faster-whisper elsewhere."
        ) from exc
    transcribe: Callable[..., dict[str, object]] = mlx_whisper.transcribe
    return transcribe


class FasterWhisperBackend:
    """CUDA and CPU backend built on faster-whisper.

    The model is constructed once and held on the instance, so the weights stay
    resident across every segment in a call.
    """

    name = "faster-whisper"

    def __init__(
        self,
        model: str = "small",
        *,
        device: str = "auto",
        compute_type: str = "default",
        beam_size: int = 5,
    ) -> None:
        self.model = model
        self.beam_size = beam_size
        whisper_model = _load_faster_whisper()
        self._model: Any = whisper_model(model, device=device, compute_type=compute_type)

    def transcribe(
        self,
        audio: npt.NDArray[np.float32],
        language: str | None = None,
    ) -> str:
        segments, _info = self._model.transcribe(
            audio,
            language=language,
            beam_size=self.beam_size,
        )
        # transcribe returns a generator; it must be consumed before the text
        # is available.
        return " ".join(part.text.strip() for part in segments).strip()


def _load_faster_whisper() -> Callable[..., Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise BackendUnavailableError(
            "The faster-whisper backend requires faster-whisper, which is not installed. "
            "Install it with: uv sync --extra cuda."
        ) from exc
    model: Callable[..., Any] = WhisperModel
    return model


def backend_status(
    name: str = BACKEND_AUTO,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> tuple[str, bool, str]:
    """Report the resolved backend and whether its dependency can be imported.

    Only the import is attempted, never the model construction, so this stays
    fast and never downloads weights. Knowing the backend is unusable before a
    call starts is worth more than discovering it after an hour of recording.
    """
    resolved = resolve_backend(name, system=system, machine=machine)
    loader = _load_mlx_whisper if resolved == BACKEND_MLX else _load_faster_whisper
    try:
        loader()
    except BackendUnavailableError as error:
        return resolved, False, str(error)
    return resolved, True, "installed"


def load_backend(
    name: str = BACKEND_AUTO,
    model: str = "small",
    *,
    system: str | None = None,
    machine: str | None = None,
) -> TranscriptionBackend:
    """Construct the backend selected for this platform, loading its model once.

    Raises BackendUnavailableError when the resolved backend is not installed,
    so the caller can surface an instruction instead of an import traceback.
    """
    resolved = resolve_backend(name, system=system, machine=machine)
    if resolved == BACKEND_MLX:
        return MLXBackend(model)
    return FasterWhisperBackend(model)


def transcribe_segments(
    segments: Iterable[Segment],
    backend: TranscriptionBackend,
    *,
    language: str | None = None,
    progress: ProgressCallback | None = None,
) -> list[TranscribedSegment]:
    """Transcribe every segment in order, reporting progress as each completes.

    Empty results are retained here so the sidecar records the full segment set.
    They are dropped when the transcript is merged.
    """
    ordered = list(segments)
    total = len(ordered)
    results: list[TranscribedSegment] = []

    for index, segment in enumerate(ordered, start=1):
        audio = segment_to_audio(segment)
        text = backend.transcribe(audio, language).strip()
        results.append(
            TranscribedSegment(
                user_id=segment.user_id,
                start=segment.start,
                duration=segment.duration,
                text=text,
            )
        )
        if progress is not None:
            progress(index, total)

    return results
