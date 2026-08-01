"""Conversion from Discord voice frames to the format Whisper expects.

Discord delivers 48 kHz stereo signed 16 bit audio. Whisper consumes 16 kHz mono
float32. A one-hour call yields several hundred segments, so the conversion runs
in process with numpy rather than spawning a resampler per segment.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import numpy.typing as npt

from .sink import DISCORD_CHANNELS, DISCORD_SAMPLE_RATE, Segment

__all__ = [
    "TARGET_SAMPLE_RATE",
    "pcm_to_mono",
    "prepare_segments",
    "resample",
    "segment_to_audio",
]

#: Sample rate every Whisper backend expects.
TARGET_SAMPLE_RATE = 16_000

#: Full scale of signed 16 bit audio, used to normalise into [-1, 1).
_INT16_FULL_SCALE = 32768.0

#: Filter length per decimation step. Long enough for a clean stopband at the
#: new Nyquist frequency while staying inexpensive for short segments.
_TAPS_PER_FACTOR = 24

DEFAULT_MIN_SEGMENT = 0.3

try:  # pragma: no cover - depends on whether scipy is installed
    from scipy.signal import resample_poly as _resample_poly
except ImportError:  # pragma: no cover - the numpy path is the default
    _resample_poly = None


def pcm_to_mono(pcm: bytes | bytearray) -> npt.NDArray[np.float32]:
    """Downmix interleaved 16 bit stereo bytes to normalised float32 mono.

    A trailing partial frame is discarded, which can occur if a packet is
    truncated in transit.
    """
    raw = np.frombuffer(bytes(pcm), dtype="<i2")
    frames = raw.size // DISCORD_CHANNELS
    if frames == 0:
        return np.zeros(0, dtype=np.float32)

    stereo = raw[: frames * DISCORD_CHANNELS].reshape(-1, DISCORD_CHANNELS)
    mono = stereo.mean(axis=1, dtype=np.float32)
    return np.asarray(mono / _INT16_FULL_SCALE, dtype=np.float32)


def _lowpass_taps(factor: int) -> npt.NDArray[np.float32]:
    """Build a Hamming windowed sinc low-pass at the decimated Nyquist frequency."""
    length = _TAPS_PER_FACTOR * factor + 1
    cutoff = 0.5 / factor
    positions = np.arange(length, dtype=np.float64) - (length - 1) / 2
    response = 2 * cutoff * np.sinc(2 * cutoff * positions) * np.hamming(length)
    return np.asarray(response / response.sum(), dtype=np.float32)


def _decimate(samples: npt.NDArray[np.float32], factor: int) -> npt.NDArray[np.float32]:
    """Low-pass then keep every nth sample, avoiding aliasing on the way down.

    The full convolution is sliced explicitly rather than using mode="same",
    which returns the longer of its two operands and so yields more samples than
    the input when a segment is shorter than the filter.
    """
    taps = _lowpass_taps(factor)
    offset = (taps.size - 1) // 2
    filtered = np.convolve(samples, taps, mode="full")[offset : offset + samples.size]
    return np.asarray(filtered[::factor], dtype=np.float32)


def _interpolate(
    samples: npt.NDArray[np.float32], source_rate: int, target_rate: int
) -> npt.NDArray[np.float32]:
    """Linear resampling for ratios that are not a whole number."""
    count = round(samples.size * target_rate / source_rate)
    if count == 0:
        return np.zeros(0, dtype=np.float32)
    source_positions = np.arange(samples.size, dtype=np.float64)
    target_positions = np.arange(count, dtype=np.float64) * (source_rate / target_rate)
    return np.asarray(
        np.interp(target_positions, source_positions, samples),
        dtype=np.float32,
    )


def resample(
    samples: npt.NDArray[np.float32],
    source_rate: int = DISCORD_SAMPLE_RATE,
    target_rate: int = TARGET_SAMPLE_RATE,
) -> npt.NDArray[np.float32]:
    """Resample mono float32 audio, preferring scipy when it is available."""
    if source_rate == target_rate or samples.size == 0:
        return np.asarray(samples, dtype=np.float32)
    if source_rate < target_rate:
        return _interpolate(samples, source_rate, target_rate)

    factor, remainder = divmod(source_rate, target_rate)
    if remainder != 0:
        return _interpolate(samples, source_rate, target_rate)

    if _resample_poly is not None:
        return np.asarray(_resample_poly(samples, 1, factor), dtype=np.float32)
    return _decimate(samples, factor)


def segment_to_audio(segment: Segment) -> npt.NDArray[np.float32]:
    """Convert one recorded segment to 16 kHz mono float32."""
    return resample(pcm_to_mono(segment.pcm))


def prepare_segments(
    segments: Iterable[Segment],
    *,
    min_segment: float = DEFAULT_MIN_SEGMENT,
) -> list[Segment]:
    """Drop segments too short to carry speech.

    Whisper transcribes near-silence confidently, emitting phantom lines such as
    "Thank you." from breath noise, so brief fragments are discarded before they
    reach the model rather than filtered out of the transcript afterwards.
    """
    return [segment for segment in segments if segment.duration >= min_segment]
