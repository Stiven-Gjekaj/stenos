"""Tests for downmixing, resampling, and short-segment filtering."""

from __future__ import annotations

import math
import struct

import numpy as np
import pytest

from stenos import audio
from stenos.audio import (
    TARGET_SAMPLE_RATE,
    pcm_to_mono,
    prepare_segments,
    resample,
    segment_to_audio,
)
from stenos.sink import BYTES_PER_SECOND, DISCORD_SAMPLE_RATE, Segment


def stereo_pcm(left: list[int], right: list[int]) -> bytes:
    """Interleave two channels of 16 bit samples into Discord's wire format."""
    interleaved: list[int] = []
    for lval, rval in zip(left, right, strict=True):
        interleaved.extend((lval, rval))
    return struct.pack(f"<{len(interleaved)}h", *interleaved)


def sine(frequency: float, seconds: float, rate: int) -> np.ndarray:
    positions = np.arange(int(rate * seconds), dtype=np.float64) / rate
    return (np.sin(2 * np.pi * frequency * positions) * 0.5).astype(np.float32)


def rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


def test_channels_are_averaged() -> None:
    pcm = stereo_pcm([1000, 2000], [3000, 4000])

    mono = pcm_to_mono(pcm)

    assert mono.shape == (2,)
    assert mono[0] == pytest.approx(2000 / 32768.0, abs=1e-6)
    assert mono[1] == pytest.approx(3000 / 32768.0, abs=1e-6)


def test_output_is_float32_normalised_to_unit_range() -> None:
    pcm = stereo_pcm([32767, -32768], [32767, -32768])

    mono = pcm_to_mono(pcm)

    assert mono.dtype == np.float32
    assert mono.max() <= 1.0
    assert mono.min() >= -1.0
    assert mono[1] == pytest.approx(-1.0)


def test_trailing_partial_frame_is_discarded() -> None:
    # Five int16 samples cannot form whole stereo frames; the last is dropped.
    pcm = struct.pack("<5h", 1, 2, 3, 4, 5)

    assert pcm_to_mono(pcm).shape == (2,)


def test_empty_input_yields_an_empty_array() -> None:
    mono = pcm_to_mono(b"")

    assert mono.shape == (0,)
    assert mono.dtype == np.float32


def test_single_orphan_sample_yields_an_empty_array() -> None:
    assert pcm_to_mono(struct.pack("<h", 7)).shape == (0,)


def test_one_second_of_stereo_becomes_one_second_of_mono() -> None:
    mono = pcm_to_mono(bytes(BYTES_PER_SECOND))

    assert mono.shape == (DISCORD_SAMPLE_RATE,)


@pytest.mark.parametrize("frames", [48_000, 24_000, 1_000, 7])
def test_resampling_produces_a_third_of_the_input_samples(frames: int) -> None:
    resampled = resample(np.zeros(frames, dtype=np.float32))

    assert resampled.shape == (math.ceil(frames / 3),)
    assert resampled.dtype == np.float32


def test_full_pipeline_shape_from_bytes_to_model_input() -> None:
    audio_samples = segment_to_audio(Segment(user_id=1, start=0.0, pcm=bytearray(BYTES_PER_SECOND)))

    assert audio_samples.shape == (TARGET_SAMPLE_RATE,)
    assert audio_samples.dtype == np.float32


def test_speech_band_content_survives_resampling() -> None:
    tone = sine(440.0, 1.0, DISCORD_SAMPLE_RATE)

    resampled = resample(tone)

    assert rms(resampled) == pytest.approx(rms(tone), rel=0.05)


def test_content_above_the_new_nyquist_is_attenuated_not_aliased() -> None:
    # Without a low-pass, 20 kHz would fold back to 4 kHz and land in the
    # middle of the speech band.
    tone = sine(20_000.0, 1.0, DISCORD_SAMPLE_RATE)

    resampled = resample(tone)

    assert rms(resampled) < rms(tone) / 100


def test_resampling_is_a_no_op_at_the_target_rate() -> None:
    samples = sine(300.0, 0.1, TARGET_SAMPLE_RATE)

    resampled = resample(samples, TARGET_SAMPLE_RATE, TARGET_SAMPLE_RATE)

    assert np.array_equal(resampled, samples)


def test_empty_audio_resamples_to_empty() -> None:
    assert resample(np.zeros(0, dtype=np.float32)).shape == (0,)


def test_non_integer_ratio_falls_back_to_interpolation() -> None:
    samples = sine(200.0, 0.1, 44_100)

    resampled = resample(samples, 44_100, TARGET_SAMPLE_RATE)

    assert resampled.dtype == np.float32
    assert resampled.shape == (round(samples.size * TARGET_SAMPLE_RATE / 44_100),)


def test_upsampling_uses_interpolation() -> None:
    samples = sine(200.0, 0.1, 8_000)

    resampled = resample(samples, 8_000, TARGET_SAMPLE_RATE)

    assert resampled.shape == (2 * samples.size,)


def test_numpy_path_matches_scipy_path_when_scipy_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The numpy decimation path is the default, since scipy is not a
    # dependency. Assert it is exercised and produces usable audio.
    monkeypatch.setattr(audio, "_resample_poly", None)
    tone = sine(440.0, 0.5, DISCORD_SAMPLE_RATE)

    resampled = resample(tone)

    assert resampled.shape == (math.ceil(tone.size / 3),)
    assert rms(resampled) == pytest.approx(rms(tone), rel=0.05)


def segment_of(seconds: float, *, user_id: int = 1, start: float = 0.0) -> Segment:
    return Segment(user_id=user_id, start=start, pcm=bytearray(int(BYTES_PER_SECOND * seconds)))


def test_segments_shorter_than_the_threshold_are_discarded() -> None:
    segments = [segment_of(0.1), segment_of(0.5), segment_of(0.29)]

    kept = prepare_segments(segments, min_segment=0.3)

    assert [round(segment.duration, 2) for segment in kept] == [0.5]


def test_segment_exactly_at_the_threshold_is_kept() -> None:
    kept = prepare_segments([segment_of(0.3)], min_segment=0.3)

    assert len(kept) == 1


def test_filtering_preserves_order_and_identity() -> None:
    segments = [segment_of(1.0, user_id=7, start=5.0), segment_of(2.0, user_id=9, start=1.0)]

    kept = prepare_segments(segments, min_segment=0.3)

    assert [(segment.user_id, segment.start) for segment in kept] == [(7, 5.0), (9, 1.0)]


def test_zero_threshold_keeps_everything_with_audio() -> None:
    segments = [segment_of(0.01), segment_of(0.02)]

    assert len(prepare_segments(segments, min_segment=0.0)) == 2


def test_default_threshold_is_three_tenths_of_a_second() -> None:
    assert prepare_segments([segment_of(0.2)]) == []
    assert len(prepare_segments([segment_of(0.4)])) == 1
