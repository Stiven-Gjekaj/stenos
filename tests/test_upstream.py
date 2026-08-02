"""Tests for the repairs applied to the installed py-cord.

Every test starts from stock py-cord. The replacements are installed on classes
belonging to another package, so the originals are captured once at import,
before anything has had a chance to replace them, and put back after each test.
"""

from __future__ import annotations

import logging
import warnings
from collections import deque
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from discord.opus import PacketDecoder
from discord.voice.packets.core import OPUS_SILENCE
from discord.voice.packets.rtp import RTPPacket
from discord.voice.receive.reader import PacketDecryptor
from discord.voice.receive.router import PacketRouter

from stenos import upstream

_STOCK_DECRYPT_RTP = PacketDecryptor.decrypt_rtp
_STOCK_RTPSIZE = PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize
_STOCK_RUN = PacketRouter.run
_STOCK_DECODE = PacketDecoder._decode_packet
_STOCK_HANDOFF = PacketDecoder._get_next_packet
_STOCK_READY = PacketDecoder._flag_ready_state
_STOCK_RESET = PacketDecoder.reset
_STOCK_DESTROY = PacketDecoder.destroy


def _restore_stock_pycord() -> None:
    upstream._STATE = None
    upstream._stop_patched = False
    upstream._decode_replaced = False
    upstream._decode_patched = False
    upstream._skipped = 0
    PacketDecryptor.decrypt_rtp = _STOCK_DECRYPT_RTP  # type: ignore[method-assign]
    PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize = (  # type: ignore[method-assign]
        _STOCK_RTPSIZE
    )
    PacketRouter.run = _STOCK_RUN  # type: ignore[method-assign]
    PacketDecoder._decode_packet = _STOCK_DECODE  # type: ignore[method-assign]
    upstream._flush_patched = False
    upstream._recovered = 0
    PacketDecoder._get_next_packet = _STOCK_HANDOFF  # type: ignore[method-assign]
    PacketDecoder._flag_ready_state = _STOCK_READY  # type: ignore[method-assign]
    PacketDecoder.reset = _STOCK_RESET  # type: ignore[method-assign]
    PacketDecoder.destroy = _STOCK_DESTROY  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def stock_pycord() -> Iterator[None]:
    _restore_stock_pycord()
    yield
    _restore_stock_pycord()


def _decrypt(connection: Any) -> Any:
    """Run one encrypted packet through the decryptor against a given state."""

    class Client:
        _connection = connection

    decryptor = PacketDecryptor("xsalsa20_poly1305", upstream._PROBE_KEY, Client())
    return decryptor.decrypt_rtp(upstream._probe_packet(RTPPacket))


def _decrypt_rtpsize(connection: Any, words: int) -> Any:
    """Run one rtpsize packet carrying ``words`` extension words through it.

    Discord writes two, which is the one count py-cord's constant happens to
    match, so anything checking only that would call a broken decryptor sound.
    """

    class Client:
        _connection = connection

    decryptor = PacketDecryptor("aead_xchacha20_poly1305_rtpsize", upstream._PROBE_KEY, Client())
    return decryptor.decrypt_rtp(upstream._rtpsize_packet(RTPPacket, words))


def _leaves_the_extension(self: Any, packet: Any) -> bytes:
    """An rtpsize decryptor of the shape py-cord's decrypt_rtp is written for.

    It returns the extension along with the payload, leaving the single removal
    to the caller, which is the arrangement that needs no repair at all.
    """
    packet.adjust_rtpsize()
    return self.box.decrypt(
        packet.decrypted_data or packet.data,
        bytes(packet.header),
        packet.nonce + bytes(20),
    )


class Connection:
    """A voice connection carrying the given encryption session."""

    def __init__(self, session: Any = None) -> None:
        self.dave_session = session
        self.dave_protocol_version = 0 if session is None else 1

    @property
    def ssrc_user_map(self) -> dict[int, int]:
        return {upstream._PROBE_SSRC: 4242}


#: What a session hands back, long enough that losing eight bytes off the front
#: is visible rather than emptying it.
_DAVE_OUTPUT = b"OPUSFRAME" + bytes(range(40))


class _Dave:
    """A session that returns audio for the payload, and refuses anything else.

    Refusing rather than asserting is what a real one does, and it is what makes
    py-cord substitute silence, which is half of what a stock recording contains.
    """

    ready = True

    def decrypt(self, user_id: int, media: object, payload: bytes) -> bytes:
        if payload != upstream._PROBE_PAYLOAD:
            raise ValueError("Failed to decrypt: NoDecryptorForUser")
        return _DAVE_OUTPUT


def _require_repair() -> None:
    """Skip when the installed py-cord does not need repairing.

    Keeps the tripwire below as the single failure on a py-cord that has fixed
    this, rather than burying it under every test that assumes the repair was
    installed.
    """
    if not upstream.apply_receive_repair().applied:
        pytest.skip("py-cord returns received audio correctly, so there is nothing to repair")


def test_stock_pycord_still_discards_unencrypted_audio() -> None:
    # The tripwire. This asserts the defect the repair exists for is still
    # present in the installed py-cord. When a future version fixes it, this
    # test fails, and stenos/upstream.py should be deleted rather than carried
    # as a replacement that no longer replaces anything.
    assert _decrypt(Connection()) is None, (
        "py-cord now returns received audio without a session. "
        "The repair in stenos/upstream.py is obsolete and should be removed."
    )


def test_stock_pycord_still_mishandles_the_packet_extension() -> None:
    # The second tripwire. py-cord computes the offset the extension occupies,
    # discards it, and removes a constant eight bytes, then removes it a second
    # time after DAVE hands back the audio. When a future version fixes this,
    # the repair in stenos/upstream.py should be removed with it.
    assert upstream._extension_handling(PacketDecryptor, RTPPacket) == "wrong", (
        "py-cord now handles the packet extension correctly. "
        "The repair in stenos/upstream.py is obsolete and should be removed."
    )


def test_stock_pycord_delivers_no_audio_at_any_extension_size() -> None:
    # Why a recording made against a stock 2.8.1 is silence interrupted by
    # decode failures. Two extension words survive DAVE and then lose the first
    # eight bytes of opus, which the decoder rejects. Every other size loses the
    # wrong bytes before DAVE sees them, so the packet becomes opus silence.
    delivered = {
        words: _decrypt_rtpsize(Connection(_Dave()), words) == _DAVE_OUTPUT
        for words in upstream._PROBE_EXTENSION_WORDS
    }

    assert not any(delivered.values()), delivered
    assert _decrypt_rtpsize(Connection(_Dave()), 2) == _DAVE_OUTPUT[8:]


def test_the_repair_delivers_audio_at_every_extension_size() -> None:
    _require_repair()

    upstream.apply_receive_repair()

    for words in upstream._PROBE_EXTENSION_WORDS:
        assert _decrypt_rtpsize(Connection(_Dave()), words) == _DAVE_OUTPUT, (
            f"audio was lost on a packet carrying {words} extension words"
        )


def test_a_pycord_that_leaves_the_extension_keeps_the_single_removal() -> None:
    # The removal in decrypt_rtp is only a second removal because the transport
    # decryption already did it. Against one that does not, it is the first, and
    # taking it away would hand the extension to the decoder as if it were audio.
    PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize = (  # type: ignore[method-assign]
        _leaves_the_extension
    )

    state = upstream.apply_receive_repair()

    assert "extension" not in state.reason
    for words in upstream._PROBE_EXTENSION_WORDS:
        assert _decrypt_rtpsize(Connection(_Dave()), words) == _DAVE_OUTPUT


def test_an_extension_probe_that_raises_leaves_pycord_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("unfamiliar decryptor")

    monkeypatch.setattr(upstream, "_extension_handling", explode)

    state = upstream.apply_receive_repair()

    assert state.applied is False
    assert "could not probe" in state.reason
    assert PacketDecryptor.decrypt_rtp is _STOCK_DECRYPT_RTP


def test_the_repair_is_applied_when_the_defect_is_present() -> None:
    _require_repair()

    state = upstream.apply_receive_repair()

    assert state.applied is True
    assert "discarded" in state.reason
    assert "applied" in state.summary


def test_the_repair_restores_the_payload() -> None:
    _require_repair()

    upstream.apply_receive_repair()

    assert _decrypt(Connection()) == upstream._PROBE_PAYLOAD


def test_deciding_happens_once() -> None:
    first = upstream.apply_receive_repair()

    assert upstream.apply_receive_repair() is first
    assert upstream.receive_repair_state() is first


def test_a_correct_pycord_is_left_alone() -> None:
    def correct(self: Any, packet: Any) -> Any:
        packet.decrypted_data = self._decryptor_rtp(packet)
        return packet.decrypted_data

    PacketDecryptor.decrypt_rtp = correct  # type: ignore[method-assign]
    PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize = (  # type: ignore[method-assign]
        _leaves_the_extension
    )

    state = upstream.apply_receive_repair()

    assert state.applied is False
    assert "correctly" in state.reason
    # Left in place rather than replaced with an equivalent of its own.
    assert PacketDecryptor.decrypt_rtp is correct


def test_a_probe_that_raises_leaves_pycord_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # A py-cord shaped differently than expected is a reason not to replace its
    # decryption, not a reason to try.
    def explode(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("unfamiliar decryptor")

    monkeypatch.setattr(upstream, "_payload_survives", explode)

    state = upstream.apply_receive_repair()

    assert state.applied is False
    assert "could not probe" in state.reason
    assert PacketDecryptor.decrypt_rtp is _STOCK_DECRYPT_RTP


def _refuse_import(monkeypatch: pytest.MonkeyPatch, target: str, error: Exception) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object) -> Any:
        if name == target:
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)


def test_missing_internals_are_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _refuse_import(monkeypatch, "davey", ImportError("no davey here"))

    state = upstream.apply_receive_repair()

    assert state.applied is False
    assert "voice support unavailable" in state.reason


def test_a_voice_import_that_raises_something_other_than_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Importing discord.voice raises MissingVoiceDependenciesError, which is not
    # an ImportError, when a voice dependency will not load. A frozen build is
    # exactly where that happens, and catching only ImportError took the whole
    # program down with it.
    class MissingVoiceDependenciesError(Exception):
        pass

    _refuse_import(
        monkeypatch,
        "discord.voice.packets.core",
        MissingVoiceDependenciesError("PyNaCl is required for voice support."),
    )

    state = upstream.apply_receive_repair()

    assert state.applied is False
    assert "PyNaCl" in state.reason


def test_an_unready_session_still_discards() -> None:
    # Whether the payload is encrypted before the handshake completes is not
    # established, so the repair deliberately does not touch this state.
    _require_repair()

    class Unready:
        ready = False

    upstream.apply_receive_repair()

    assert _decrypt(Connection(Unready())) is None


def test_a_failed_decrypt_still_becomes_silence() -> None:
    # py-cord's own behaviour, kept. Changing it would hand undecrypted bytes
    # to the decoder.
    _require_repair()

    class Failing:
        ready = True

        def decrypt(self, *args: object, **kwargs: object) -> bytes:
            raise ValueError("Failed to decrypt: NoDecryptorForUser")

    upstream.apply_receive_repair()

    assert _decrypt(Connection(Failing())) == OPUS_SILENCE


def test_a_ready_session_still_decrypts_through_dave() -> None:
    _require_repair()

    class Ready:
        ready = True

        def decrypt(self, user_id: int, media: object, payload: bytes) -> bytes:
            assert payload == upstream._PROBE_PAYLOAD
            return b"decrypted by dave"

    upstream.apply_receive_repair()

    assert _decrypt(Connection(Ready())) == b"decrypted by dave"


# The three smaller repairs. Each removes something py-cord says or does that
# describes a problem the caller does not have.


class Router:
    """The parts of a packet router py-cord's own run method touches."""

    def __init__(self, stopping: Exception | None) -> None:
        self.reader = SimpleNamespace(client=SimpleNamespace(stop_recording=self._stop), error=None)
        self.waiter = Waiter()
        self.ran = False
        self._stopping = stopping

    def _do_run(self) -> None:
        self.ran = True

    def _stop(self) -> None:
        if self._stopping is not None:
            raise self._stopping


def test_the_router_stopping_a_recording_it_already_stopped_is_not_an_error() -> None:
    # py-cord calls stop_recording from run's finally on every path, including
    # the one where the caller stopped the recording a moment earlier. The
    # second call raises in a thread with nothing to catch it, so a recording
    # that worked ends with a traceback.
    from discord.sinks.errors import RecordingException

    assert upstream.tolerate_double_stop() is True
    router = Router(RecordingException("You are not recording"))

    PacketRouter.run(router)

    assert router.ran is True


def test_a_router_that_fails_for_another_reason_still_says_so() -> None:
    upstream.tolerate_double_stop()
    router = Router(ValueError("the socket went away"))

    with pytest.raises(ValueError, match="socket"):
        PacketRouter.run(router)


def test_quietening_the_router_happens_once() -> None:
    assert upstream.tolerate_double_stop() is True
    patched = PacketRouter.run

    assert upstream.tolerate_double_stop() is True
    assert PacketRouter.run is patched


def test_the_stale_receive_warning_is_dropped_once_reception_is_repaired() -> None:
    _require_repair()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        applied = upstream.quieten_stale_receive_warning()
        warnings.warn(f"{upstream._STALE_WARNING} due to DAVE.", RuntimeWarning, stacklevel=1)
        warnings.warn("Something else entirely.", RuntimeWarning, stacklevel=1)

    assert applied is True
    assert [str(entry.message) for entry in caught] == ["Something else entirely."]


def test_a_pycord_that_was_left_alone_keeps_what_it_says_about_itself() -> None:
    # Nothing was repaired, so there is no basis for contradicting it.
    def correct(self: Any, packet: Any) -> Any:
        packet.decrypted_data = self._decryptor_rtp(packet)
        return packet.decrypted_data

    PacketDecryptor.decrypt_rtp = correct  # type: ignore[method-assign]
    PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize = (  # type: ignore[method-assign]
        _leaves_the_extension
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        applied = upstream.quieten_stale_receive_warning()
        warnings.warn(f"{upstream._STALE_WARNING} due to DAVE.", RuntimeWarning, stacklevel=1)

    assert applied is False
    assert len(caught) == 1


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("reader", logging.INFO, "reader.py", 1, message, (), None)


def test_a_sender_report_is_dropped_and_the_rest_of_the_log_is_kept() -> None:
    noise = upstream._SenderReports()

    assert noise.filter(_record("Received unexpected rtcp packet type=%s, %s")) is False
    assert noise.filter(_record("Voice connection completed")) is True


def test_the_filter_is_attached_to_the_reader_that_emits_it() -> None:
    logger = logging.getLogger("discord.voice.receive.reader")
    before = list(logger.filters)
    try:
        assert upstream.quieten_rtcp_reports() is True
        added = [item for item in logger.filters if item not in before]

        assert len(added) == 1
        assert isinstance(added[0], upstream._SenderReports)
    finally:
        logger.filters = before


# Decoding. py-cord hands the decoded audio back to the session whenever it
# reports the speaker as passthrough, which is a second decryption of something
# that stopped being ciphertext in decrypt_rtp.


class Decoder:
    """An opus decoder that records its calls and needs no libopus."""

    def __init__(self, pcm: bytes = b"pcm") -> None:
        self.pcm = pcm
        self.calls: list[tuple[Any, bool]] = []

    def decode(self, data: Any, *, fec: bool = False) -> bytes:
        self.calls.append((data, fec))
        return self.pcm


class Lost:
    """The placeholder py-cord puts in for a packet that never arrived."""

    def __bool__(self) -> bool:
        return False


def _decoder(buffer: Any, pcm: bytes = b"pcm") -> Any:
    """A PacketDecoder assembled without its __init__, which would need libopus."""
    decoder = PacketDecoder.__new__(PacketDecoder)
    decoder._decoder = Decoder(pcm)
    decoder._buffer = buffer
    return decoder


class NoSuccessor:
    def peek_next(self) -> Any:
        return None


class Successor:
    def peek_next(self) -> Any:
        return SimpleNamespace(decrypted_data=b"the next payload")


def test_stock_pycord_still_decrypts_audio_it_has_already_decoded() -> None:
    # The third tripwire. When a future version stops handing decoded audio back
    # to the session, this fails and the repair should be removed with it.
    assert upstream._decodes_without_decrypting(PacketDecoder) is False, (
        "py-cord no longer decrypts audio it has already decoded. "
        "The repair in stenos/upstream.py is obsolete and should be removed."
    )


def test_the_repair_stops_the_audio_being_decrypted_again() -> None:
    assert upstream.recover_decoded_audio() is True
    assert upstream._decodes_without_decrypting(PacketDecoder) is True


def test_a_pycord_that_decodes_cleanly_is_left_alone() -> None:
    def clean(self: Any, packet: Any) -> Any:
        return packet, self._decoder.decode(packet.decrypted_data, fec=False)

    PacketDecoder._decode_packet = clean  # type: ignore[method-assign]

    assert upstream.recover_decoded_audio() is False
    assert PacketDecoder._decode_packet is clean


def test_a_decoder_probe_that_raises_leaves_pycord_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("unfamiliar decoder")

    monkeypatch.setattr(upstream, "_decodes_without_decrypting", explode)

    assert upstream.recover_decoded_audio() is False
    assert PacketDecoder._decode_packet is _STOCK_DECODE


def test_replacing_the_decoder_happens_once() -> None:
    assert upstream.recover_decoded_audio() is True
    replaced = PacketDecoder._decode_packet

    assert upstream.recover_decoded_audio() is True
    assert PacketDecoder._decode_packet is replaced


def test_a_packet_that_arrived_is_decoded_as_it_is() -> None:
    upstream.recover_decoded_audio()
    decoder = _decoder(NoSuccessor())

    _, pcm = decoder._decode_packet(SimpleNamespace(decrypted_data=b"opus"))

    assert pcm == b"pcm"
    assert decoder._decoder.calls == [(b"opus", False)]


def test_a_lost_packet_is_concealed_from_the_one_after_it() -> None:
    # py-cord's own behaviour, kept. The successor carries forward error
    # correction for the interval that went missing.
    upstream.recover_decoded_audio()
    decoder = _decoder(Successor())

    decoder._decode_packet(Lost())

    assert decoder._decoder.calls == [(b"the next payload", True)]


def test_a_lost_packet_with_nothing_after_it_is_invented_by_the_decoder() -> None:
    upstream.recover_decoded_audio()
    decoder = _decoder(NoSuccessor())

    decoder._decode_packet(Lost())

    assert decoder._decoder.calls == [(None, False)]


def test_a_failure_that_is_not_an_opus_error_is_still_skipped() -> None:
    # What ends a recording is the router thread dying, and the thread does not
    # care which exception killed it. The one this exists for is raised by the
    # session, not by opus.
    def explode(self: Any, packet: Any) -> Any:
        raise ValueError("Failed to decrypt: NoDecryptorForUser")

    PacketDecoder._decode_packet = explode  # type: ignore[method-assign]
    assert upstream.tolerate_undecodable_frames() is True

    _, pcm = PacketDecoder._decode_packet(_decoder(NoSuccessor()), object())

    assert pcm == b""
    assert upstream.skipped_frames() == 1


def test_tolerance_applied_after_the_replacement_covers_it() -> None:
    # The ordering the two depend on. Applied the other way round, the tolerance
    # would wrap the method being replaced and go with it.
    upstream.recover_decoded_audio()
    upstream.tolerate_undecodable_frames()

    decoder = _decoder(NoSuccessor())
    decoder._decoder.decode = lambda data, *, fec=False: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ValueError("libopus said no")
    )

    _, pcm = decoder._decode_packet(SimpleNamespace(decrypted_data=b"opus"))

    assert pcm == b""
    assert upstream.skipped_frames() == 1


# The packet handoff. py-cord flushes the whole buffer at the first sign of a
# sequence gap, returns the earliest packet, and drops the rest.


class _ProbeFlushPacket:
    """A packet with nothing on it but identity, which is all the handoff reads."""


class Waiter:
    """The parts of the router's waiter these repairs touch."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    def clear(self) -> None:
        self.items.clear()

    def register(self, item: Any) -> None:
        self.items.append(item)

    def unregister(self, item: Any) -> None:
        if item in self.items:
            self.items.remove(item)


class EmptyBuffer:
    def __len__(self) -> int:
        return 0

    def peek(self, **kwargs: Any) -> Any:
        return None

    def pop(self, *, timeout: float = 0) -> Any:
        return None

    def reset(self) -> None:
        return None


def _handoff_decoder(buffer: Any) -> Any:
    """A PacketDecoder carrying only what the handoff and readiness flag read."""
    decoder = PacketDecoder.__new__(PacketDecoder)
    decoder._buffer = buffer
    decoder.ssrc = upstream._PROBE_SSRC
    decoder._last_seq = 4
    decoder._last_ts = 96000
    decoder.router = SimpleNamespace(waiter=Waiter())
    return decoder


def test_stock_pycord_still_discards_the_packets_it_flushed() -> None:
    # The fourth tripwire. py-cord returns the earliest flushed packet and drops
    # the others, having already moved the buffer past all of them.
    assert upstream._flush_delivers_everything(PacketDecoder) is False, (
        "py-cord now delivers every flushed packet. "
        "The repair in stenos/upstream.py is obsolete and should be removed."
    )


def test_the_repair_delivers_every_flushed_packet_in_order() -> None:
    assert upstream.recover_flushed_packets() is True

    packets = [_ProbeFlushPacket(), _ProbeFlushPacket(), _ProbeFlushPacket()]
    decoder = _handoff_decoder(upstream._ProbeFlushBuffer(packets))

    assert [decoder._get_next_packet(0) for _ in packets] == packets
    assert upstream.recovered_frames() == 2


def test_a_lone_flushed_packet_is_not_counted_as_recovered() -> None:
    upstream.recover_flushed_packets()
    decoder = _handoff_decoder(upstream._ProbeFlushBuffer([_ProbeFlushPacket()]))

    decoder._get_next_packet(0)

    assert upstream.recovered_frames() == 0


def test_a_packet_the_buffer_releases_is_passed_straight_through() -> None:
    upstream.recover_flushed_packets()
    released = _ProbeFlushPacket()
    decoder = _handoff_decoder(SimpleNamespace(pop=lambda *, timeout=0: released))

    assert decoder._get_next_packet(0) is released


def test_an_empty_buffer_still_yields_nothing() -> None:
    upstream.recover_flushed_packets()

    assert _handoff_decoder(EmptyBuffer())._get_next_packet(0) is None


def test_a_decoder_holding_packets_keeps_being_polled() -> None:
    # Without this the readiness flag asks the buffer alone, unregisters a
    # decoder whose buffer is empty, and the held audio is never collected.
    upstream.recover_flushed_packets()
    decoder = _handoff_decoder(EmptyBuffer())

    decoder._flag_ready_state()
    assert decoder.router.waiter.items == []

    setattr(decoder, upstream._HELD, deque([_ProbeFlushPacket()]))
    decoder._flag_ready_state()
    assert decoder.router.waiter.items == [decoder]


def test_emptying_the_decoder_empties_what_it_was_holding() -> None:
    # Held packets belong to the recording that received them, not the next one.
    upstream.recover_flushed_packets()
    decoder = _handoff_decoder(EmptyBuffer())
    decoder._decoder = None
    held = deque([_ProbeFlushPacket()])
    setattr(decoder, upstream._HELD, held)

    decoder.destroy()

    assert not held


def test_forgetting_held_packets_still_runs_what_it_wraps() -> None:
    calls: list[str] = []
    wrapped = upstream._forget_held(lambda self: calls.append("original"))
    holder = SimpleNamespace()
    held = deque(["a", "b"])
    setattr(holder, upstream._HELD, held)

    wrapped(holder)

    assert calls == ["original"]
    assert not held


def test_repairing_the_handoff_happens_once() -> None:
    assert upstream.recover_flushed_packets() is True
    replaced = PacketDecoder._get_next_packet

    assert upstream.recover_flushed_packets() is True
    assert PacketDecoder._get_next_packet is replaced


def test_a_pycord_that_delivers_everything_is_left_alone() -> None:
    def complete(self: Any, timeout: float) -> Any:
        packet = self._buffer.pop(timeout=timeout)
        if packet is None and len(self._buffer):
            self._held = deque(self._buffer.flush())
        if getattr(self, "_held", None):
            return self._held.popleft()
        return packet

    PacketDecoder._get_next_packet = complete  # type: ignore[method-assign]

    assert upstream.recover_flushed_packets() is False
    assert PacketDecoder._get_next_packet is complete


def test_a_handoff_probe_that_raises_leaves_pycord_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("unfamiliar buffer")

    monkeypatch.setattr(upstream, "_flush_delivers_everything", explode)

    assert upstream.recover_flushed_packets() is False
    assert PacketDecoder._get_next_packet is _STOCK_HANDOFF


def test_probing_does_not_report_a_loss_that_did_not_happen(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The probe provokes the defect deliberately. py-cord warns as it discards
    # the packets, and at startup, with no recording running, that warning would
    # describe a loss of nothing.
    with caplog.at_level(logging.WARNING, logger="discord.opus"):
        upstream._flush_delivers_everything(PacketDecoder)

    assert "were lost being flushed" not in caplog.text
