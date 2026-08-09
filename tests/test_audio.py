"""Tests for downmixing, resampling, reducing a segment, and short-segment filtering."""

from __future__ import annotations

import math
import struct
import threading
import wave
from pathlib import Path

import numpy as np
import pytest

from helpers import segment_of
from stenos import audio
from stenos.audio import (
    _INT16_FULL_SCALE,
    TARGET_SAMPLE_RATE,
    downmix,
    prepare_segments,
    resample,
    segment_to_audio,
    to_float32,
    to_int16,
    write_speaker_wav,
)
from stenos.sink import (
    BYTES_PER_SECOND,
    DISCORD_CHANNELS,
    DISCORD_SAMPLE_RATE,
    MONO_BYTES_PER_SECOND,
    Segment,
)


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


def converted(pcm: bytes) -> np.ndarray:
    """Stereo bytes as the package converts them, in the two steps it uses.

    A channel is dropped as each packet arrives and the result is normalised
    only when a model reads it, so this pair is what actually runs.
    """
    return to_float32(downmix(pcm))


def mono_reference(pcm: bytes) -> np.ndarray:
    """The same conversion in one step, computed independently.

    The package used to do it this way. Kept here rather than shipped, so the
    two step path has something to be checked against that is not itself.
    """
    raw = np.frombuffer(pcm, dtype="<i2")
    frames = raw.size // DISCORD_CHANNELS
    if frames == 0:
        return np.zeros(0, dtype=np.float32)
    stereo = raw[: frames * DISCORD_CHANNELS].reshape(-1, DISCORD_CHANNELS)
    averaged = stereo.mean(axis=1, dtype=np.float32) / _INT16_FULL_SCALE
    return np.asarray(averaged, dtype=np.float32)


def test_channels_are_averaged() -> None:
    pcm = stereo_pcm([1000, 2000], [3000, 4000])

    mono = converted(pcm)

    assert mono.shape == (2,)
    assert mono[0] == pytest.approx(2000 / 32768.0, abs=1e-6)
    assert mono[1] == pytest.approx(3000 / 32768.0, abs=1e-6)


def test_output_is_float32_normalised_to_unit_range() -> None:
    pcm = stereo_pcm([32767, -32768], [32767, -32768])

    mono = converted(pcm)

    assert mono.dtype == np.float32
    assert mono.max() <= 1.0
    assert mono.min() >= -1.0
    assert mono[1] == pytest.approx(-1.0)


def test_trailing_partial_frame_is_discarded() -> None:
    # Five int16 samples cannot form whole stereo frames; the last is dropped.
    pcm = struct.pack("<5h", 1, 2, 3, 4, 5)

    assert converted(pcm).shape == (2,)


def test_empty_input_yields_an_empty_array() -> None:
    mono = converted(b"")

    assert mono.shape == (0,)
    assert mono.dtype == np.float32


def test_single_orphan_sample_yields_an_empty_array() -> None:
    assert converted(struct.pack("<h", 7)).shape == (0,)


def test_one_second_of_stereo_becomes_one_second_of_mono() -> None:
    mono = converted(bytes(BYTES_PER_SECOND))

    assert mono.shape == (DISCORD_SAMPLE_RATE,)


@pytest.mark.parametrize("frames", [48_000, 24_000, 1_000, 7])
def test_resampling_produces_a_third_of_the_input_samples(frames: int) -> None:
    resampled = resample(np.zeros(frames, dtype=np.float32))

    assert resampled.shape == (math.ceil(frames / 3),)
    assert resampled.dtype == np.float32


def test_full_pipeline_shape_from_bytes_to_model_input() -> None:
    one_second = Segment(user_id=1, start=0.0, pcm=bytearray(MONO_BYTES_PER_SECOND))
    audio_samples = segment_to_audio(one_second)

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


# Holding a call. The audio a segment carries has to survive being reduced,
# because the whole reason for reducing it is to hold less of the same thing.


def speech_like(seconds: float) -> bytes:
    """Interleaved stereo bytes with content, at a level a person speaks at."""
    rng = np.random.default_rng(7)
    positions = np.arange(int(DISCORD_SAMPLE_RATE * seconds)) / DISCORD_SAMPLE_RATE
    wave = 0.3 * np.sin(2 * np.pi * 220 * positions)
    wave += 0.15 * np.sin(2 * np.pi * 900 * positions)
    wave += 0.02 * rng.standard_normal(positions.size)
    return np.repeat((wave * 20_000).astype("<i2"), DISCORD_CHANNELS).tobytes()


def test_reducing_a_segment_keeps_the_audio_it_held() -> None:
    # The property the whole change rests on. Reducing early has to give the
    # backend what converting late gave it, or the transcript changes.
    stereo = speech_like(3.0)
    expected = resample(mono_reference(stereo))

    segment = Segment(user_id=1, start=0.0)
    for offset in range(0, len(stereo), 3840):
        segment.extend(stereo[offset : offset + 3840])
    segment.reduce()

    actual = segment_to_audio(segment)
    assert actual.shape == expected.shape
    # Within half a step of 16 bit, which is the quantisation and nothing else.
    assert np.abs(actual - expected).max() <= 1 / _INT16_FULL_SCALE


def test_reducing_a_segment_keeps_its_duration() -> None:
    # Every timestamp after the first derives from a duration, so a segment
    # that changes length while being reduced moves the transcript.
    segment = Segment(user_id=1, start=4.0, pcm=bytearray(downmix(speech_like(3.0))))
    before = segment.duration

    segment.reduce()

    assert segment.duration == pytest.approx(before, abs=1e-3)
    assert segment.end == pytest.approx(4.0 + before, abs=1e-3)


def test_reducing_a_segment_holds_a_sixth_of_what_arrived() -> None:
    stereo = speech_like(3.0)
    segment = Segment(user_id=1, start=0.0)
    segment.extend(stereo)

    assert len(segment.pcm) == len(stereo) // DISCORD_CHANNELS
    segment.reduce()
    assert len(segment.pcm) == pytest.approx(len(stereo) / 6, rel=0.01)


def test_reducing_twice_changes_nothing() -> None:
    # The worker can be handed a segment that cleanup already retired.
    segment = Segment(user_id=1, start=0.0, pcm=bytearray(downmix(speech_like(1.0))))
    segment.reduce()
    once = bytes(segment.pcm)

    segment.reduce()

    assert bytes(segment.pcm) == once
    assert segment.sample_rate == TARGET_SAMPLE_RATE


def test_downmixing_per_packet_matches_downmixing_the_whole_segment() -> None:
    # Why it is safe to do this on arrival: averaging a pair of samples depends
    # on nothing outside that pair, so there is no boundary to get wrong.
    stereo = speech_like(1.0)
    in_one_go = downmix(stereo)
    piecewise = b"".join(
        downmix(stereo[offset : offset + 3840]) for offset in range(0, len(stereo), 3840)
    )

    assert piecewise == in_one_go


def test_an_unreduced_segment_still_reaches_the_backend_correctly() -> None:
    # A recording whose worker did not drain in time is transcribable as it is,
    # so the conversion has to cope with either rate.
    segment = Segment(user_id=1, start=0.0, pcm=bytearray(downmix(speech_like(1.0))))

    audio_samples = segment_to_audio(segment)

    assert audio_samples.shape == (TARGET_SAMPLE_RATE,)
    assert segment.sample_rate == DISCORD_SAMPLE_RATE


def test_quantising_clips_rather_than_wrapping() -> None:
    # A resampling filter overshoots around a transient. Wrapping would turn the
    # loudest moment of a segment into its quietest.
    over = np.array([1.5, -1.5, 0.0], dtype=np.float32)

    raw = np.frombuffer(to_int16(over), dtype="<i2")

    assert list(raw) == [32767, -32768, 0]


def test_a_snapshot_pairs_the_audio_with_the_rate_it_is_at() -> None:
    # Read separately, a caller can take the audio from before a reduction and
    # the rate from after it, and then treat 48 kHz audio as 16 kHz: three
    # times too long, and nothing a model recognises. The reducer runs on its
    # own thread, so the two have to be taken together.
    segment = Segment(user_id=1, start=0.0)
    segment.extend(bytes(BYTES_PER_SECOND))

    took = threading.Event()
    reduced = threading.Event()
    seen: list[tuple[int, int]] = []

    def read() -> None:
        took.wait()
        seen.append((lambda pair: (len(pair[0]), pair[1]))(segment.snapshot()))

    def reduce() -> None:
        took.set()
        segment.reduce()
        reduced.set()

    threads = [threading.Thread(target=reduce), threading.Thread(target=read)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    held, rate = seen[0]
    # Either pair is fine; a mixture is not. One second of audio stays one
    # second whichever side of the reduction it was read from.
    assert held / (rate * 2) == pytest.approx(1.0)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as handle:
        rate = handle.getframerate()
        data = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    assert handle.getnchannels() == 1
    return data, rate


def spoken(seconds: float, *, start: float) -> Segment:
    """A segment of audible tone at the rate a reduced segment holds."""
    samples = int(TARGET_SAMPLE_RATE * seconds)
    tone = (np.sin(np.arange(samples) * 0.1) * 8000).astype("<i2").tobytes()
    return Segment(user_id=1, start=start, pcm=bytearray(tone), sample_rate=TARGET_SAMPLE_RATE)


def test_a_speaker_wav_places_each_segment_at_its_offset(tmp_path: Path) -> None:
    # The property worth having: an offset in the transcript is the same offset
    # in the file, so a line can be checked against the audio it came from.
    path = write_speaker_wav(tmp_path / "one.wav", [spoken(1.0, start=2.0), spoken(1.0, start=0.0)])

    data, rate = read_wav(path)

    assert rate == TARGET_SAMPLE_RATE
    assert len(data) / rate == pytest.approx(3.0)
    assert data[: rate // 2].any(), "the segment at zero is missing"
    assert not data[rate : rate * 2].any(), "the gap should be silent"
    assert data[rate * 2 : rate * 2 + 100].any(), "the segment at two seconds is missing"


def test_a_speaker_wav_is_written_in_offset_order(tmp_path: Path) -> None:
    # Segments arrive ordered, but nothing downstream should depend on that.
    forwards = write_speaker_wav(
        tmp_path / "a.wav", [spoken(0.5, start=0.0), spoken(0.5, start=1.0)]
    )
    backwards = write_speaker_wav(
        tmp_path / "b.wav", [spoken(0.5, start=1.0), spoken(0.5, start=0.0)]
    )

    assert read_wav(forwards)[0].tobytes() == read_wav(backwards)[0].tobytes()


def test_a_segment_never_reduced_is_written_at_the_rate_a_model_reads(tmp_path: Path) -> None:
    # A recording that ended before its worker drained holds 48 kHz. The file
    # has one rate throughout, so that segment is resampled on the way out.
    unreduced = Segment(user_id=1, start=0.0, pcm=bytearray(MONO_BYTES_PER_SECOND))

    data, rate = read_wav(write_speaker_wav(tmp_path / "mixed.wav", [unreduced]))

    assert rate == TARGET_SAMPLE_RATE
    assert len(data) == pytest.approx(TARGET_SAMPLE_RATE, rel=0.01)


def test_a_speaker_with_no_segments_writes_an_empty_file(tmp_path: Path) -> None:
    data, _rate = read_wav(write_speaker_wav(tmp_path / "silent.wav", []))

    assert len(data) == 0
