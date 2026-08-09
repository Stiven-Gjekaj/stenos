"""Recorded audio, and the conversions between how it arrives and how it is read.

Discord delivers 48 kHz stereo signed 16 bit. Whisper consumes 16 kHz mono
float32. A one-hour call yields several hundred segments, so the conversion runs
in process with numpy rather than spawning a resampler per segment.

It also runs as early as it can. Holding a whole call at the rate it arrives
costs 192,000 bytes for every second of speech, and none of it can be released
until transcription has finished, which is also when the model weights load. So
a segment drops a channel as its packets arrive, which is exact per packet
because averaging a pair of samples depends on nothing outside that pair, and
drops its sample rate once it can no longer grow. What is held falls to a sixth,
and the audio handed to the backend is the same audio it would have been handed
before.

``Segment`` lives here rather than in the sink because a segment is a span of
audio, and because the sink needs these conversions: with the class the other
way round the two modules could not import each other. ``sink`` re-exports it.
"""

from __future__ import annotations

import threading
import wave
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt

from .spill import Spilled, SpillWriter, read_audio

__all__ = [
    "BYTES_PER_SECOND",
    "DISCORD_CHANNELS",
    "DISCORD_SAMPLE_RATE",
    "DISCORD_SAMPLE_WIDTH",
    "MONO_BYTES_PER_SECOND",
    "SILENT_RMS",
    "TARGET_SAMPLE_RATE",
    "Segment",
    "downmix",
    "downsample",
    "loudness",
    "prepare_segments",
    "resample",
    "segment_to_audio",
    "to_float32",
    "to_int16",
    "write_speaker_wav",
]

#: Discord decodes received voice to 48 kHz, stereo, signed 16 bit little endian.
DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2

#: Byte count of one second of audio in the format Discord delivers.
BYTES_PER_SECOND = DISCORD_SAMPLE_RATE * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH

#: Byte count of one second once a channel has been dropped, which is how a
#: segment holds audio while it is still being written to.
MONO_BYTES_PER_SECOND = DISCORD_SAMPLE_RATE * DISCORD_SAMPLE_WIDTH

#: Sample rate every Whisper backend expects.
TARGET_SAMPLE_RATE = 16_000

#: Below this a segment carries no signal for a model to have recognised.
#: Deliberately near the floor: the point is to identify audio that is silent,
#: not to judge whether speech was loud enough, because quiet speech is still
#: speech. Digital silence measures zero, and py-cord substitutes opus silence
#: for any packet it cannot decrypt, so that is what this catches.
SILENT_RMS = 0.002

#: Full scale of signed 16 bit audio, used to normalise into [-1, 1).
_INT16_FULL_SCALE = 32768.0

#: Filter length per decimation step. Long enough for a clean stopband at the
#: new Nyquist frequency while staying inexpensive for short segments.
_TAPS_PER_FACTOR = 24

try:  # pragma: no cover - depends on whether scipy is installed
    from scipy.signal import resample_poly as _resample_poly
except ImportError:  # pragma: no cover - the numpy path is the default
    _resample_poly = None


def _stereo_frames(pcm: bytes | bytearray) -> npt.NDArray[np.int16]:
    """Interleaved stereo bytes as a frame by channel array.

    A trailing partial frame is discarded, which can occur if a packet is
    truncated in transit. So is a trailing partial sample: a buffer whose
    length is odd cannot be read as 16 bit at all, and numpy raises rather
    than truncating. That raise reaches py-cord's router thread through the
    sink, which ends the thread and the recording with it, so one malformed
    packet would cost every second of audio that followed it.
    """
    usable = len(pcm) - len(pcm) % DISCORD_SAMPLE_WIDTH
    raw = np.frombuffer(bytes(pcm)[:usable], dtype="<i2")
    frames = raw.size // DISCORD_CHANNELS
    if frames == 0:
        return np.zeros((0, DISCORD_CHANNELS), dtype=np.int16)
    return raw[: frames * DISCORD_CHANNELS].reshape(-1, DISCORD_CHANNELS)


def downmix(pcm: bytes | bytearray) -> bytes:
    """Average the two channels of interleaved stereo, staying in 16 bit.

    Exact per packet. Averaging a pair of samples depends on nothing outside
    that pair, so running this as audio arrives gives the same bytes as running
    it over a whole segment afterwards. No filter is involved, so there is no
    state to carry across a packet boundary and no edge to get wrong.
    """
    stereo = _stereo_frames(pcm)
    if stereo.size == 0:
        return b""
    # Summed in int32 so two full scale samples cannot wrap on the way.
    mono = stereo.astype(np.int32).mean(axis=1)
    return np.asarray(np.rint(mono), dtype="<i2").tobytes()


def to_float32(pcm: bytes | bytearray) -> npt.NDArray[np.float32]:
    """Normalise mono 16 bit bytes into the range a model reads."""
    raw = np.frombuffer(bytes(pcm), dtype="<i2")
    return np.asarray(raw.astype(np.float32) / _INT16_FULL_SCALE, dtype=np.float32)


def to_int16(samples: npt.NDArray[np.float32]) -> bytes:
    """Quantise normalised audio back to 16 bit.

    Clipped rather than scaled, because a resampling filter overshoots slightly
    around a sharp transient and scaling the whole segment to accommodate it
    would quieten everything else to hide a few samples.
    """
    scaled = np.rint(samples.astype(np.float64) * _INT16_FULL_SCALE)
    return np.asarray(
        np.clip(scaled, -_INT16_FULL_SCALE, _INT16_FULL_SCALE - 1), dtype="<i2"
    ).tobytes()


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


def downsample(pcm: bytes | bytearray) -> bytes:
    """Drop mono audio from the rate it arrives at to the rate a model reads."""
    return to_int16(resample(to_float32(pcm)))


@dataclass(slots=True)
class Segment:
    """One continuous stretch of speech from a single participant.

    ``start`` is measured in seconds from the first packet of the recording,
    which is the recording origin rather than the first packet from this user.

    The audio is mono from the moment it is written, and stays at the rate it
    arrived at until the segment can no longer grow, at which point ``reduce``
    drops it to the rate a model reads. ``sample_rate`` says which of the two is
    held, and is the only way to tell.

    A segment that has been spilled holds its audio on disk rather than in
    ``pcm``, and ``spill`` says where. Only a segment that can no longer grow is
    ever spilled, because ``extend`` appends to ``pcm`` and would write into a
    buffer nothing reads once the samples are on disk. Everything that reads the
    audio goes through ``snapshot``, which is what makes the two cases one.
    """

    user_id: int
    start: float
    pcm: bytearray = field(default_factory=bytearray)
    sample_rate: int = DISCORD_SAMPLE_RATE
    spill: Spilled | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def _length(self) -> int:
        """Bytes of audio this segment stands for. The caller holds the lock."""
        return self.spill.length if self.spill is not None else len(self.pcm)

    @property
    def duration(self) -> float:
        """Length of the buffered audio in seconds, at whichever rate it holds."""
        with self._lock:
            return self._length() / (self.sample_rate * DISCORD_SAMPLE_WIDTH)

    @property
    def end(self) -> float:
        """Offset in seconds at which this segment stops."""
        return self.start + self.duration

    def extend(self, payload: bytes) -> None:
        """Append one packet's worth of audio, dropping its second channel."""
        with self._lock:
            self.pcm.extend(downmix(payload))

    def reduce(self) -> None:
        """Drop to the rate a model reads, releasing two thirds of what is held.

        Both fields change together under the lock, because duration is read
        from the pair of them and a reader arriving between the two writes would
        measure the new audio against the old rate. Idempotent, so a segment
        that has already been reduced costs nothing.

        A segment that has been spilled is left alone. Its buffer is empty, so
        there is nothing here to resample, and stamping the rate anyway would
        leave it claiming 16 kHz over samples written to disk at 48 kHz, which
        is three times too long and transcribes as nothing recognisable.
        """
        with self._lock:
            if self.sample_rate == TARGET_SAMPLE_RATE or self.spill is not None:
                return
            reduced = downsample(self.pcm)
            self.pcm = bytearray(reduced)
            self.sample_rate = TARGET_SAMPLE_RATE

    def snapshot(self) -> tuple[bytes, int]:
        """The audio held and the rate it is held at, read as one.

        Taken together under the lock for the same reason ``reduce`` writes
        them together under it. Read separately, a caller can take the audio
        before a reduction and the rate after it, and then treat 48 kHz audio
        as though it were 16 kHz, which is a third of the speed and transcribes
        as nothing recognisable.
        """
        with self._lock:
            spilled, rate = self.spill, self.sample_rate
            if spilled is None:
                return bytes(self.pcm), rate
        # Read outside the lock. The file is append only and this region was
        # written before the reference to it existed, so nothing can change it,
        # and holding the lock across a disk read would stall the watchdog
        # measuring memory behind however long the disk takes.
        return read_audio(spilled), rate

    def held(self) -> int:
        """Bytes of audio currently in memory, which is none once it is spilled."""
        with self._lock:
            return len(self.pcm)

    def length(self) -> int:
        """Bytes of audio this segment holds, wherever it is."""
        with self._lock:
            return self._length()

    def is_silent(self) -> bool:
        """Whether every sample is zero.

        Answered from the flag recorded when the audio was spilled, rather than
        by reading it back. The caller that asks this asks it of every segment,
        and only for a recording that turns out to be silent, which is the one
        case where no read short circuits.
        """
        with self._lock:
            if self.spill is not None:
                return self.spill.silent
            return not any(self.pcm)

    def spill_to(self, writer: SpillWriter) -> bool:
        """Move this segment's audio to disk, releasing the memory it held.

        Returns whether anything moved, so a caller freeing memory can tell a
        segment it emptied from one that was already empty or already spilled.

        The write happens under the lock rather than beside it. A segment being
        spilled is closed and nothing should be appending to it, but a reader
        arriving between taking the audio and recording where it went would see
        a segment that holds neither.
        """
        with self._lock:
            if self.spill is not None or not self.pcm:
                return False
            self.spill = writer.append(self.user_id, self.start, bytes(self.pcm), self.sample_rate)
            self.pcm = bytearray()
            return True

    def clear(self) -> None:
        """Release the buffered audio, once there is nothing left to read it for."""
        with self._lock:
            self.pcm.clear()


def loudness(audio: npt.NDArray[np.float32]) -> float:
    """Root mean square amplitude, on the same scale as the audio itself.

    Preferred over the peak, which one click in an otherwise silent segment is
    enough to raise.
    """
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))


def segment_to_audio(segment: Segment) -> npt.NDArray[np.float32]:
    """The audio of one segment, in the form every backend reads.

    A reduced segment is already at the right rate and only needs normalising.
    One that was never reduced, which happens to a recording that ended before
    its worker drained, is resampled here instead, so the caller gets the same
    audio either way.
    """
    pcm, sample_rate = segment.snapshot()
    samples = to_float32(pcm)
    if sample_rate == TARGET_SAMPLE_RATE:
        return samples
    return resample(samples, sample_rate)


#: Only a fallback for a direct call. config.DEFAULT_MIN_SEGMENT is the setting,
#: and every caller inside the package passes it rather than relying on this.
#: Not imported from there, because config would then import audio and audio
#: config.
_DEFAULT_MIN_SEGMENT = 0.3


def prepare_segments(
    segments: Iterable[Segment],
    *,
    min_segment: float = _DEFAULT_MIN_SEGMENT,
) -> list[Segment]:
    """Drop segments too short to carry speech.

    Whisper transcribes near-silence confidently, emitting phantom lines such as
    "Thank you." from breath noise, so brief fragments are discarded before they
    reach the model rather than filtered out of the transcript afterwards.
    """
    return [segment for segment in segments if segment.duration >= min_segment]


def write_speaker_wav(
    path: Path,
    segments: Iterable[Segment],
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
) -> Path:
    """Write one participant's segments to a WAV, laid out on the call's timeline.

    Silence fills the gaps between segments, so an offset in the transcript is
    the same offset in the file and the sidecar indexes into it directly. That
    is what makes the file worth keeping: a line can be checked against the
    audio it came from without hunting for it.

    Written a segment at a time rather than assembled first. An hour of one
    speaker is 58 million samples, and building that as float32 to write it
    once would cost more memory than the recording it came from.

    Segments belonging to one speaker do not overlap, since a segment closes
    before the next one opens. One that did would be appended where it falls
    rather than mixed, which is wrong but not silently: the file would run
    longer than the call.
    """
    ordered = sorted(segments, key=lambda segment: segment.start)
    path.parent.mkdir(parents=True, exist_ok=True)

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(DISCORD_SAMPLE_WIDTH)
        handle.setframerate(sample_rate)

        written = 0
        for segment in ordered:
            gap = max(0, round(segment.start * sample_rate) - written)
            if gap:
                handle.writeframes(bytes(gap * DISCORD_SAMPLE_WIDTH))
                written += gap

            audio = segment_to_audio(segment)
            handle.writeframes(to_int16(audio))
            written += int(audio.size)

    return path
