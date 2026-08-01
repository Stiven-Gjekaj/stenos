"""A repair for one defect in the installed py-cord, applied only when it is present.

py-cord 2.8.1 discards received audio when no end to end encryption session
exists. ``PacketDecryptor.decrypt_rtp`` performs the transport decryption into a
local, enters the DAVE branch only when a session is present and ready, and then
returns ``packet.decrypted_data``, which nothing outside that branch ever
assigns. The caller reads back ``None`` and drops the packet, logging below the
default level. On a call carrying no encryption the result is a recording that
captured nothing.

This module puts the payload back, and nothing else. The repair is confined to
the one state in which no encryption can have been applied, so there is no
question of handing still encrypted bytes to the decoder: a connection with no
session at all. Every other state keeps py-cord's behaviour exactly, including
the silence substitution for a packet that fails to decrypt.

Whether to apply it is decided by running the decryptor, not by comparing
version numbers. A py-cord that returns the payload is left alone, so this
becomes inert the moment the defect is fixed upstream rather than needing to be
noticed and removed.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PatchState",
    "apply_receive_repair",
    "receive_repair_state",
]

log = logging.getLogger("stenos")

#: A payload the probe looks for on the other side of the decryptor.
_PROBE_PAYLOAD = b"\xfc\xff\xfe" + bytes(range(60))
_PROBE_SSRC = 0xDEADBEEF
_PROBE_KEY = bytes(range(32))

_STATE: PatchState | None = None


@dataclass(frozen=True, slots=True)
class PatchState:
    """Whether the repair was needed, and what happened."""

    applied: bool
    reason: str

    @property
    def summary(self) -> str:
        """One line for the environment report."""
        return f"{'applied' if self.applied else 'not applied'} ({self.reason})"


def _probe_packet(rtp_packet: Any) -> Any:
    """An RTP packet whose payload is encrypted under a known transport key."""
    import nacl.secret

    header = bytes([0x80, 0x78]) + struct.pack(">HII", 1234, 96000, _PROBE_SSRC)
    nonce = bytearray(24)
    nonce[:12] = header
    box = nacl.secret.SecretBox(_PROBE_KEY)
    return rtp_packet(header + box.encrypt(_PROBE_PAYLOAD, bytes(nonce)).ciphertext)


class _ProbeConnection:
    """The parts of a voice connection the decryptor reads, with no session."""

    dave_session = None
    dave_protocol_version = 0

    @property
    def ssrc_user_map(self) -> dict[int, int]:
        return {_PROBE_SSRC: 4242}


class _ProbeClient:
    def __init__(self) -> None:
        self._connection = _ProbeConnection()


def _payload_survives(decryptor_type: Any, rtp_packet: Any) -> bool:
    """Whether the decryptor returns the payload when no session is present.

    A defective py-cord returns None here. Any exception is treated as the
    decryptor being shaped differently than expected, which is a reason not to
    replace it.
    """
    decryptor = decryptor_type("xsalsa20_poly1305", _PROBE_KEY, _ProbeClient())
    return decryptor.decrypt_rtp(_probe_packet(rtp_packet)) == _PROBE_PAYLOAD


def _build_replacement(silence: bytes, media_type: Any) -> Any:
    """py-cord's decrypt_rtp with the discarded payload restored."""

    def decrypt_rtp(self: Any, packet: Any) -> Any:
        state = self.client._connection
        dave = getattr(state, "dave_session", None)

        raw_payload = self._decryptor_rtp(packet)

        # The one changed branch. No session means no end to end encryption was
        # applied, so the transport decryption already yielded the opus frame.
        if dave is None:
            packet.decrypted_data = raw_payload
            return packet.decrypted_data

        # Everything below is py-cord's own behaviour, kept deliberately. A
        # session that is not ready still discards the packet, because whether
        # the payload is encrypted in that window is not established.
        if dave.ready:
            uid = state.ssrc_user_map.get(packet.ssrc)
            if uid:
                try:
                    decrypted = dave.decrypt(uid, media_type, raw_payload)
                    if packet.extended:
                        offset = packet.update_extended_header(decrypted)
                        packet.decrypted_data = decrypted[offset:]
                    else:
                        packet.decrypted_data = decrypted
                except Exception as error:
                    log.debug("Ignoring exception while decoding DAVE packet", exc_info=error)
                    packet.decrypted_data = silence

        return packet.decrypted_data

    return decrypt_rtp


def apply_receive_repair() -> PatchState:
    """Replace the receive decryption if this py-cord discards the payload.

    Idempotent. The first call decides, and later calls report that decision
    rather than probing again.
    """
    global _STATE
    if _STATE is not None:
        return _STATE

    try:
        import davey
        from discord.voice.packets.core import OPUS_SILENCE
        from discord.voice.packets.rtp import RTPPacket
        from discord.voice.receive.reader import PacketDecryptor
    except ImportError as error:
        _STATE = PatchState(False, f"py-cord internals not found: {error}")
        return _STATE

    try:
        if _payload_survives(PacketDecryptor, RTPPacket):
            _STATE = PatchState(False, "py-cord returns received audio correctly")
            return _STATE
    except Exception as error:
        _STATE = PatchState(False, f"could not probe the decryptor: {error}")
        return _STATE

    # Replacing a method on a third party class is the whole point of this
    # module, and is why it is confined to it.
    PacketDecryptor.decrypt_rtp = _build_replacement(  # type: ignore[method-assign]
        OPUS_SILENCE, davey.MediaType.audio
    )

    try:
        repaired = _payload_survives(PacketDecryptor, RTPPacket)
    except Exception as error:
        _STATE = PatchState(True, f"replaced, but the check afterwards failed: {error}")
        return _STATE

    if not repaired:
        _STATE = PatchState(True, "replaced, but audio is still discarded")
        return _STATE

    log.info("Repaired py-cord receive decryption, which discarded unencrypted audio.")
    _STATE = PatchState(True, "py-cord discarded unencrypted audio")
    return _STATE


def receive_repair_state() -> PatchState:
    """The decision made about the repair, applying it if that has not happened."""
    return apply_receive_repair()
