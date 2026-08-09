"""Transcription backends and the loop that drives them over recorded segments.

Every backend keeps its model resident for the lifetime of the object. A call
produces hundreds of short segments, and reloading a model per segment, or
shelling out to a separate binary, would dominate total runtime.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import numpy as np
import numpy.typing as npt

from .audio import SILENT_RMS, loudness, segment_to_audio
from .config import BACKEND_AUTO, BACKEND_MLX, resolve_backend
from .sink import Segment

log = logging.getLogger("stenos")

__all__ = [
    "MAX_DISTINCT_RATIO",
    "MIN_WORDS_FOR_REPETITION",
    "MLX_MODEL_REPOS",
    "BackendUnavailableError",
    "FasterWhisperBackend",
    "MLXBackend",
    "MockBackend",
    "ProgressCallback",
    "Recognition",
    "TranscribedSegment",
    "TranscriptionBackend",
    "backend_status",
    "invented_reason",
    "load_backend",
    "mlx_repo_for",
    "recognise",
    "transcribe_segments",
]

#: Fewer words than this cannot establish a loop, and short lists that repeat
#: honestly, such as counting out loud, would look like one.
MIN_WORDS_FOR_REPETITION = 12

#: Distinct words over total, below which the text is repeating rather than
#: saying anything. The two loops captured from real calls sit at 0.004 and
#: 0.077; ordinary speech from the same calls sits above 0.6.
MAX_DISTINCT_RATIO = 0.25

#: Trimmed from each word before counting, so a phrase punctuated differently
#: on each repetition still reads as the same word.
_TRIMMED = ".,!?;:\"'-"

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


@dataclass(frozen=True, slots=True)
class Recognition:
    """What a backend heard, and how sure it was, when it can say.

    ``no_speech`` is the model's own estimate that a segment holds no speech,
    and ``logprob`` the average log probability of the tokens it chose. Both
    are None for a backend that does not report them, which is why nothing may
    rely on having them: mlx-whisper is the primary target on Apple Silicon and
    reports neither through the interface used here.
    """

    text: str
    no_speech: float | None = None
    logprob: float | None = None


def recognise(
    backend: TranscriptionBackend,
    audio: npt.NDArray[np.float32],
    language: str | None = None,
) -> Recognition:
    """Transcribe one segment, taking the confidence when the backend has it.

    Asked for rather than required. Every backend can return text, and only
    some can say how sure they were, so the protocol stays the smaller of the
    two and the richer answer is an extra a backend may offer.
    """
    detailed = getattr(backend, "recognise", None)
    if callable(detailed):
        try:
            return cast("Recognition", detailed(audio, language))
        except Exception:
            # A backend whose richer path fails still has a working one.
            log.exception("%s could not report confidence, using its text alone", backend.name)

    return Recognition(text=backend.transcribe(audio, language).strip())


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
    """One segment paired with the text recognised in it.

    ``suppressed`` names why the text cannot be what was said, when it cannot.
    The text is kept either way, so a transcript that lost a line to this can be
    checked against the sidecar rather than taken on trust.
    """

    user_id: int
    start: float
    duration: float
    text: str
    suppressed: str | None = None


def invented_reason(text: str, audio: npt.NDArray[np.float32]) -> str | None:
    """Why this text cannot be what was said, or None if it might be.

    Whisper does not decline to transcribe. Given audio with nothing in it, it
    returns a confident sentence, and given a fragment it can fall into
    repeating one phrase until the segment runs out. Both were produced by real
    calls: 250 repetitions of a single word over two and a half seconds, and a
    stock courtesy over opus silence.

    Silence is judged from the audio rather than from a list of known phrases,
    which would be fragile and would only work in English. Repetition is judged
    from the text, because audio can legitimately be a person repeating
    themselves and the giveaway is the shape of what came back.
    """
    if not text.strip():
        return None

    if loudness(audio) < SILENT_RMS:
        return "silence"

    words = text.split()
    if len(words) >= MIN_WORDS_FOR_REPETITION:
        distinct = {word.strip(_TRIMMED).casefold() for word in words}
        if len(distinct) / len(words) < MAX_DISTINCT_RATIO:
            return "repetition"

    return None


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
    return cast("Callable[..., dict[str, object]]", mlx_whisper.transcribe)


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
        return self.recognise(audio, language).text

    def recognise(
        self,
        audio: npt.NDArray[np.float32],
        language: str | None = None,
    ) -> Recognition:
        """Transcribe one segment and report what the model thought of it.

        faster-whisper returns a no speech probability and an average token log
        probability per part, and both were being discarded. They are the
        model's own account of whether it heard anything, which is what the
        text alone cannot say.

        A segment reaches the model already bounded at ``MAX_SEGMENT``, which
        is the window an encoder reads, so it usually comes back as one part.
        Where it comes back as several, the worst part decides: one stretch the
        model is unsure of is what makes the whole line worth doubting.
        """
        segments, _info = self._model.transcribe(
            audio,
            language=language,
            beam_size=self.beam_size,
        )
        # transcribe returns a generator; it must be consumed before the text
        # or the confidence is available.
        parts = list(segments)

        no_speech = [
            value for part in parts if (value := getattr(part, "no_speech_prob", None)) is not None
        ]
        logprob = [
            value for part in parts if (value := getattr(part, "avg_logprob", None)) is not None
        ]
        return Recognition(
            text=" ".join(part.text.strip() for part in parts).strip(),
            no_speech=max(no_speech) if no_speech else None,
            logprob=min(logprob) if logprob else None,
        )


def _load_faster_whisper() -> Callable[..., Any]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise BackendUnavailableError(
            "The faster-whisper backend requires faster-whisper, which is not installed. "
            "Install it with: uv sync --extra cuda."
        ) from exc
    return cast("Callable[..., Any]", WhisperModel)


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
    They are dropped when the transcript is merged, as is text this decides the
    model invented, which is marked here because this is the only point at which
    the audio and the text are both in hand.
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
                suppressed=invented_reason(text, audio),
            )
        )
        if progress is not None:
            progress(index, total)

    return results
