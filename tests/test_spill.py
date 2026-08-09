"""Tests for the on disk format a recording spills into.

Nothing here runs a recording. The format is exercised directly, including the
states a process that was killed leaves behind, which is the whole reason it is
append only and describes itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from stenos.spill import (
    MANIFEST_NAME,
    SPILL_VERSION,
    SpillWriter,
    partial_recordings,
    read_audio,
    read_spill,
)

STARTED = datetime(2026, 8, 9, 15, 0, 0, tzinfo=UTC)
SPEECH = b"\x00\x04" * 800
SILENCE = bytes(1600)


def writer(directory: Path, *, sample_rate: int = 16000) -> SpillWriter:
    return SpillWriter(directory, channel="general", started_at=STARTED, sample_rate=sample_rate)


def test_a_segment_is_readable_at_the_offset_it_reports(tmp_path: Path) -> None:
    store = writer(tmp_path / "call.partial")

    first = store.append(11, 0.0, SPEECH)
    second = store.append(11, 4.0, SILENCE)
    store.close()

    assert read_audio(first) == SPEECH
    assert read_audio(second) == SILENCE
    # Appended rather than overwritten, so the second lands after the first.
    assert second.offset == len(SPEECH)


def test_each_participant_gets_their_own_file(tmp_path: Path) -> None:
    store = writer(tmp_path / "call.partial")

    alpha = store.append(11, 0.0, SPEECH)
    bravo = store.append(22, 0.0, SILENCE)
    store.close()

    assert alpha.path != bravo.path
    # Both start at nothing, because the offsets are per file rather than shared.
    assert alpha.offset == bravo.offset == 0


def test_silence_is_decided_while_the_samples_are_in_hand(tmp_path: Path) -> None:
    # The alternative is reading the whole call back to answer it, in exactly
    # the case that has to read every byte because nothing short circuits.
    store = writer(tmp_path / "call.partial")

    assert store.append(11, 0.0, SILENCE).silent is True
    assert store.append(11, 1.0, SPEECH).silent is False


def test_a_finished_recording_is_recovered_whole(tmp_path: Path) -> None:
    store = writer(tmp_path / "call.partial")
    store.remember(11, "Alpha")
    store.remember(22, "Bravo")
    store.append(11, 0.0, SPEECH)
    store.append(22, 4.0, SPEECH)
    store.close()

    found = read_spill(tmp_path / "call.partial")

    assert found is not None
    assert found.channel == "general"
    assert found.started_at == STARTED
    assert found.sample_rate == 16000
    assert found.names == {11: "Alpha", 22: "Bravo"}
    assert [segment.start for segment in found.segments] == [0.0, 4.0]
    assert found.audio_of(found.segments[0]) == SPEECH


def test_segments_come_back_in_call_order_rather_than_write_order(tmp_path: Path) -> None:
    # One participant's segments are written as they close, so two speakers
    # interleave in the manifest in an order the call does not have.
    store = writer(tmp_path / "call.partial")
    store.append(11, 9.0, SPEECH)
    store.append(22, 3.0, SPEECH)
    store.append(11, 6.0, SPEECH)
    store.close()

    found = read_spill(tmp_path / "call.partial")

    assert found is not None
    assert [segment.start for segment in found.segments] == [3.0, 6.0, 9.0]


def test_a_torn_final_line_costs_one_segment_and_not_the_recording(tmp_path: Path) -> None:
    # What a process killed mid write leaves: the last line stops part way.
    directory = tmp_path / "call.partial"
    store = writer(directory)
    store.remember(11, "Alpha")
    store.append(11, 0.0, SPEECH)
    store.append(11, 4.0, SPEECH)
    store.close()

    manifest = directory / MANIFEST_NAME
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text[: -len(text.splitlines()[-1]) // 2], encoding="utf-8")

    found = read_spill(directory)

    assert found is not None
    assert found.names == {11: "Alpha"}
    assert len(found.segments) == 1


def test_samples_that_never_reached_the_disk_are_dropped(tmp_path: Path) -> None:
    # The opposite tear, and the one the write order exists to prevent: the
    # manifest describes more than the audio file actually holds.
    directory = tmp_path / "call.partial"
    store = writer(directory)
    store.append(11, 0.0, SPEECH)
    store.append(11, 4.0, SPEECH)
    store.close()

    audio = directory / "11.pcm"
    audio.write_bytes(audio.read_bytes()[: len(SPEECH)])

    found = read_spill(directory)

    assert found is not None
    assert len(found.segments) == 1
    assert found.audio_of(found.segments[0]) == SPEECH


def test_a_directory_with_no_manifest_is_not_a_recording(tmp_path: Path) -> None:
    (tmp_path / "call.partial").mkdir()

    assert read_spill(tmp_path / "call.partial") is None


def test_a_manifest_from_a_later_format_is_refused(tmp_path: Path) -> None:
    # Better to say nothing than to read a layout this version predates.
    directory = tmp_path / "call.partial"
    store = writer(directory)
    store.append(11, 0.0, SPEECH)
    store.close()

    manifest = directory / MANIFEST_NAME
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            f'"version": {SPILL_VERSION}', f'"version": {SPILL_VERSION + 1}'
        ),
        encoding="utf-8",
    )

    assert read_spill(directory) is None


def test_unfinished_recordings_are_found_and_finished_ones_are_not(tmp_path: Path) -> None:
    kept = writer(tmp_path / "one.partial")
    kept.append(11, 0.0, SPEECH)
    kept.close()
    gone = writer(tmp_path / "two.partial")
    gone.append(22, 0.0, SPEECH)
    gone.discard()
    (tmp_path / "not-a-recording.partial").mkdir()

    assert partial_recordings(tmp_path) == [tmp_path / "one.partial"]


def test_discarding_removes_the_directory_entirely(tmp_path: Path) -> None:
    store = writer(tmp_path / "call.partial")
    store.append(11, 0.0, SPEECH)
    store.append(22, 0.0, SPEECH)

    store.discard()

    assert not (tmp_path / "call.partial").exists()


def test_writing_after_closing_is_refused_rather_than_silently_lost(tmp_path: Path) -> None:
    store = writer(tmp_path / "call.partial")
    store.close()

    with pytest.raises(ValueError, match="already closed"):
        store.append(11, 0.0, SPEECH)


def test_closing_twice_is_harmless(tmp_path: Path) -> None:
    # Finishing a recording closes it, and so does the shutdown path behind it.
    store = writer(tmp_path / "call.partial")
    store.close()

    store.close()


def test_a_name_learned_after_the_audio_is_still_recorded(tmp_path: Path) -> None:
    # Names arrive from the gateway, which is not what drives the audio, so a
    # participant can speak before the guild has told the bot who they are.
    directory = tmp_path / "call.partial"
    store = writer(directory)
    store.append(11, 0.0, SPEECH)
    store.remember(11, "Alpha")
    store.close()

    found = read_spill(directory)

    assert found is not None
    assert found.names == {11: "Alpha"}


def test_a_recording_that_never_spilled_leaves_nothing_behind(tmp_path: Path) -> None:
    # Most recordings never outgrow memory, and those must not touch the disk
    # at all. Learning who is in the channel is not a reason to create a file.
    directory = tmp_path / "call.partial"
    store = writer(directory)
    store.remember(11, "Alpha")

    assert store.started is False
    assert not directory.exists()

    store.close()

    assert not directory.exists()
    assert partial_recordings(tmp_path) == []


def test_discarding_storage_that_was_never_used_is_harmless(tmp_path: Path) -> None:
    store = writer(tmp_path / "call.partial")

    store.discard()

    assert not (tmp_path / "call.partial").exists()


def test_a_name_learned_before_the_first_spill_is_still_recorded(tmp_path: Path) -> None:
    # Held until there is somewhere to put it, then written ahead of the audio,
    # which is the order the two actually happened in.
    directory = tmp_path / "call.partial"
    store = writer(directory)
    store.remember(11, "Alpha")
    store.append(11, 0.0, SPEECH)
    store.close()

    found = read_spill(directory)

    assert found is not None
    assert found.names == {11: "Alpha"}
