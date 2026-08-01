"""Tests for the repair applied to py-cord's receive decryption.

Every test starts from stock py-cord. The replacement is installed on a class
belonging to another package, so the original is captured once at import, before
anything has had a chance to replace it, and put back after each test.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from discord.voice.packets.core import OPUS_SILENCE
from discord.voice.packets.rtp import RTPPacket
from discord.voice.receive.reader import PacketDecryptor

from stenos import upstream

_STOCK_DECRYPT_RTP = PacketDecryptor.decrypt_rtp


@pytest.fixture(autouse=True)
def stock_pycord() -> Iterator[None]:
    upstream._STATE = None
    PacketDecryptor.decrypt_rtp = _STOCK_DECRYPT_RTP  # type: ignore[method-assign]
    yield
    upstream._STATE = None
    PacketDecryptor.decrypt_rtp = _STOCK_DECRYPT_RTP  # type: ignore[method-assign]


def _decrypt(connection: Any) -> Any:
    """Run one encrypted packet through the decryptor against a given state."""

    class Client:
        _connection = connection

    decryptor = PacketDecryptor("xsalsa20_poly1305", upstream._PROBE_KEY, Client())
    return decryptor.decrypt_rtp(upstream._probe_packet(RTPPacket))


class Connection:
    """A voice connection carrying the given encryption session."""

    def __init__(self, session: Any = None) -> None:
        self.dave_session = session
        self.dave_protocol_version = 0 if session is None else 1

    @property
    def ssrc_user_map(self) -> dict[int, int]:
        return {upstream._PROBE_SSRC: 4242}


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
