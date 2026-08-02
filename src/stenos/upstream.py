"""Repairs for defects in the installed py-cord, applied only when present.

Two defects in py-cord 2.8.1 sit between a voice packet and the decoder, and
between them they account for every packet on a real call.

The first discards received audio when no end to end encryption session exists.
``PacketDecryptor.decrypt_rtp`` performs the transport decryption into a local,
enters the DAVE branch only when a session is present and ready, and then
returns ``packet.decrypted_data``, which nothing outside that branch ever
assigns. The caller reads back ``None`` and drops the packet, logging below the
default level. On a call carrying no encryption the result is a recording that
captured nothing.

The second removes the RTP header extension twice on a call that does carry
encryption, which is now every voice call. The transport decryption already
removes it: ``_decrypt_rtp_aead_xchacha20_poly1305_rtpsize`` computes the offset
with ``update_extended_header``, discards that offset, and slices a constant
eight bytes instead, which is right only when the sender wrote exactly two
extension words. ``decrypt_rtp`` then applies the offset a second time, to the
opus frame DAVE just returned. A packet carrying two extension words loses the
first eight bytes of its audio and the decoder rejects the remainder as a
corrupted stream. A packet carrying any other number loses the wrong bytes
before DAVE sees it, fails to decrypt, and is replaced with opus silence. There
is no extension size for which the audio survives, which is why a recording made
against a stock 2.8.1 is silence interrupted by decode failures.

Each repair is confined to what it fixes. The first touches only the one state
in which no encryption can have been applied, so there is no question of handing
still encrypted bytes to the decoder: a connection with no session at all. The
second changes which bytes are removed, never whether decryption happens. Every
other state keeps py-cord's behaviour exactly, including the silence
substitution for a packet that fails to decrypt.

Whether to apply either is decided by running the decryptor, not by comparing
version numbers. A py-cord that returns the payload intact is left alone, so
these become inert the moment the defects are fixed upstream rather than needing
to be noticed and removed.

A third defect sits after the decoder rather than before it. ``_decode_packet``
turns the payload into linear audio and then hands that audio to
``dave.decrypt`` whenever the session reports the speaker as passthrough, which
is a second decryption of something that stopped being ciphertext two steps
earlier. It either corrupts the audio or raises inside the router thread, which
ends the recording. Passthrough follows a DAVE downgrade, reset, or transition
recovery, so in practice it follows somebody joining or leaving the channel.

Three smaller things live here for the same reason. A packet that will not
decode is skipped rather than allowed to end the recording. The router thread is
stopped from reporting its own stop as an error. And py-cord is stopped from
telling the user that reception cannot work at the moment it does, and from
logging an ordinary RTCP sender report as a surprise several times a minute.
"""

from __future__ import annotations

import logging
import re
import struct
import warnings
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PatchState",
    "apply_receive_repair",
    "quieten_rtcp_reports",
    "quieten_stale_receive_warning",
    "receive_repair_state",
    "recover_decoded_audio",
    "skipped_frames",
    "tolerate_double_stop",
    "tolerate_undecodable_frames",
]

log = logging.getLogger("stenos")

#: A payload the probe looks for on the other side of the decryptor.
_PROBE_PAYLOAD = b"\xfc\xff\xfe" + bytes(range(60))
_PROBE_SSRC = 0xDEADBEEF
_PROBE_KEY = bytes(range(32))

#: The nonce carried in the last four bytes of an rtpsize packet.
_PROBE_NONCE = b"\x00\x00\x00\x01"

#: Extension word counts the second probe tries. Discord writes two, which is
#: the one count the constant in py-cord happens to match, so a probe that tried
#: only that would report the decryptor as sound.
_PROBE_EXTENSION_WORDS = (0, 1, 2, 3)

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


def _rtpsize_packet(rtp_packet: Any, words: int) -> Any:
    """An rtpsize packet carrying the payload behind ``words`` extension words.

    The extension header stays outside the encrypted region and the extension
    values stay inside it, which is what the rtpsize modes mean.
    """
    import nacl.secret

    extended = words > 0
    header = bytes([0x80 | (0x10 if extended else 0), 0x78]) + struct.pack(
        ">HII", 7, 96000, _PROBE_SSRC
    )
    extension = b"\xbe\xde" + struct.pack(">H", words) if extended else b""
    body = nacl.secret.Aead(_PROBE_KEY).encrypt(
        bytes(words * 4) + _PROBE_PAYLOAD,
        header + extension,
        _PROBE_NONCE + bytes(20),
    )
    return rtp_packet(header + extension + body.ciphertext + _PROBE_NONCE)


def _extension_handling(decryptor_type: Any, rtp_packet: Any) -> str:
    """What the rtpsize decryptor leaves in front of the payload.

    ``removed`` when it returns the payload alone, which means anything applying
    the offset again is applying it twice. ``left`` when it returns the
    extension values as well, which means the caller is right to remove them.
    ``wrong`` when it returns neither for at least one extension size.
    """
    decryptor = decryptor_type("aead_xchacha20_poly1305_rtpsize", _PROBE_KEY, _ProbeClient())
    decrypt = decryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize
    results = {}
    for words in _PROBE_EXTENSION_WORDS:
        try:
            results[words] = decrypt(_rtpsize_packet(rtp_packet, words))
        except Exception:
            # A size the decryptor cannot carry at all is still a size on which
            # audio is lost, so it counts against it rather than stopping here.
            results[words] = None

    if all(payload == _PROBE_PAYLOAD for payload in results.values()):
        return "removed"
    if all(payload == bytes(words * 4) + _PROBE_PAYLOAD for words, payload in results.items()):
        return "left"
    return "wrong"


def _build_rtpsize_replacement() -> Any:
    """py-cord's rtpsize decryption with the computed offset actually used."""
    from nacl.exceptions import CryptoError

    def _decrypt_rtp_aead_xchacha20_poly1305_rtpsize(self: Any, packet: Any) -> bytes:
        packet.adjust_rtpsize()

        try:
            result = self.box.decrypt(
                packet.decrypted_data or packet.data,
                bytes(packet.header),
                packet.nonce + bytes(20),
            )
        except Exception as error:
            # Raised as CryptoError because the reader catches that to drop one
            # packet, and anything else to log a traceback per packet.
            raise CryptoError(error) from error

        # The one changed line. py-cord computes this offset, discards it, and
        # removes a constant eight bytes. Returns zero when the packet carries
        # no extension, so the payload is untouched in that case.
        return result[packet.update_extended_header(result) :]

    return _decrypt_rtp_aead_xchacha20_poly1305_rtpsize


def _build_replacement(silence: bytes, media_type: Any, *, strip_extension: bool) -> Any:
    """py-cord's decrypt_rtp with the discarded payload restored.

    ``strip_extension`` says whether the transport decryption leaves the header
    extension in front of the payload, and so whether this has to remove it. It
    is decided from the rtpsize decryptor because that is the mode Discord
    negotiates, and all four of py-cord's modes agree on it.
    """

    def decrypt_rtp(self: Any, packet: Any) -> Any:
        state = self.client._connection
        dave = getattr(state, "dave_session", None)

        raw_payload = self._decryptor_rtp(packet)

        # The extension is removed here or not at all, and either way before the
        # session sees the payload. py-cord removes it afterwards instead, from
        # bytes the session has already turned into an opus frame, which takes
        # the offset off the front of the audio.
        if strip_extension and packet.extended:
            raw_payload = raw_payload[packet.update_extended_header(raw_payload) :]

        # No session means no end to end encryption was applied, so the
        # transport decryption already yielded the opus frame. py-cord returns
        # an unassigned attribute here and the caller drops the packet.
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
                    packet.decrypted_data = dave.decrypt(uid, media_type, raw_payload)
                except Exception as error:
                    log.debug("Ignoring exception while decoding DAVE packet", exc_info=error)
                    packet.decrypted_data = silence

        return packet.decrypted_data

    return decrypt_rtp


def _describe(findings: dict[str, bool]) -> str:
    """The findings that hold, joined into something a sentence can carry."""
    return " and ".join(name for name, holds in findings.items() if holds)


def apply_receive_repair() -> PatchState:
    """Replace the receive decryption where this py-cord loses the audio.

    Idempotent. The first call decides, and later calls report that decision
    rather than probing again.
    """
    global _STATE
    if _STATE is not None:
        return _STATE

    # Not just ImportError. Importing discord.voice raises
    # MissingVoiceDependenciesError when a voice dependency will not load,
    # which is a plain exception rather than an import failure, and a frozen
    # build is exactly where that happens. Nothing this module can fail at is
    # worth stopping the program for, so every failure is caught and reported.
    try:
        import davey
        from discord.voice.packets.core import OPUS_SILENCE
        from discord.voice.packets.rtp import RTPPacket
        from discord.voice.receive.reader import PacketDecryptor
    except Exception as error:
        _STATE = PatchState(False, f"voice support unavailable: {error}")
        return _STATE

    try:
        discards_payload = not _payload_survives(PacketDecryptor, RTPPacket)
        handling = _extension_handling(PacketDecryptor, RTPPacket)
    except Exception as error:
        _STATE = PatchState(False, f"could not probe the decryptor: {error}")
        return _STATE

    # "left" is the shape py-cord's decrypt_rtp is written for: the extension
    # still in front of the payload, waiting to be removed once. Any other shape
    # means removing it there is a second removal.
    if not discards_payload and handling == "left":
        _STATE = PatchState(False, "py-cord returns received audio correctly")
        return _STATE

    # Replacing methods on a third party class is the whole point of this
    # module, and is why it is confined to it.
    if handling == "wrong":
        PacketDecryptor._decrypt_rtp_aead_xchacha20_poly1305_rtpsize = (  # type: ignore[method-assign]
            _build_rtpsize_replacement()
        )

    PacketDecryptor.decrypt_rtp = _build_replacement(  # type: ignore[method-assign]
        OPUS_SILENCE, davey.MediaType.audio, strip_extension=handling == "left"
    )

    try:
        payload_repaired = _payload_survives(PacketDecryptor, RTPPacket)
        extension_repaired = _extension_handling(PacketDecryptor, RTPPacket) != "wrong"
    except Exception as error:
        _STATE = PatchState(True, f"replaced, but the check afterwards failed: {error}")
        return _STATE

    unfixed = _describe(
        {
            "audio is still discarded": not payload_repaired,
            "the extension is still mishandled": not extension_repaired,
        }
    )
    if unfixed:
        _STATE = PatchState(True, f"replaced, but {unfixed}")
        return _STATE

    found = _describe(
        {
            "discarded unencrypted audio": discards_payload,
            "mishandled the packet extension": handling == "wrong",
            "removed the packet extension twice": handling == "removed",
        }
    )
    log.info("Repaired py-cord receive decryption, which %s.", found)
    _STATE = PatchState(True, f"py-cord {found}")
    return _STATE


def receive_repair_state() -> PatchState:
    """The decision made about the repair, applying it if that has not happened."""
    return apply_receive_repair()


_skipped = 0
_decode_patched = False


def skipped_frames() -> int:
    """Packets discarded because they would not decode."""
    return _skipped


def tolerate_undecodable_frames() -> bool:
    """Let a packet that will not decode be skipped rather than end the recording.

    py-cord lets a decode failure out of the router thread, which stops that
    thread, which stops the recording. One malformed frame therefore discards
    every second of audio that would have followed it.

    A frame that will not decode is a fragment of one utterance. Losing it
    costs a syllable. Losing the rest of the call costs the recording, so the
    failure is confined to the packet that caused it and counted, and the
    reason a transcript has gaps stays visible.

    Every exception is caught, not only the one opus raises. What ends a
    recording is the thread dying, and the thread does not care which exception
    killed it. The type is named in the log so the cause stays legible.

    Must be applied after recover_decoded_audio, which replaces the method this
    wraps. Applied first, it would wrap the version being replaced and its
    tolerance would be discarded along with it.
    """
    global _decode_patched
    if _decode_patched:
        return True

    try:
        from discord.opus import PacketDecoder
    except Exception as error:
        log.debug("Cannot make decoding tolerant: %s", error)
        return False

    original = PacketDecoder._decode_packet

    def _decode_packet(self: Any, packet: Any) -> Any:
        global _skipped
        try:
            return original(self, packet)
        except Exception as error:
            _skipped += 1
            if _skipped == 1:
                log.warning(
                    "A packet would not decode (%s: %s). Skipping it and any "
                    "others like it, rather than ending the recording.",
                    error.__class__.__name__,
                    error,
                )
            # An empty payload, which the sink reads as nothing to record.
            return packet, b""

    PacketDecoder._decode_packet = _decode_packet  # type: ignore[method-assign]
    _decode_patched = True
    return True


_stop_patched = False


def tolerate_double_stop() -> bool:
    """Let py-cord's router thread end a recording without reporting an error.

    ``PacketRouter.run`` calls ``stop_recording`` from its finally block, on
    every path including the one where the recording was stopped by the caller
    a moment earlier. The second call raises, in a thread with nothing to catch
    it, so a recording that worked ends with a traceback describing nothing the
    caller did and nothing that failed.

    The exception is swallowed where the thread would otherwise print it, and
    only that exception, so a router that stops for any other reason still says
    so.
    """
    global _stop_patched
    if _stop_patched:
        return True

    try:
        from discord.sinks.errors import RecordingException
        from discord.voice.receive.router import PacketRouter
    except Exception as error:
        log.debug("Cannot quieten the router thread: %s", error)
        return False

    original = PacketRouter.run

    def run(self: Any) -> None:
        try:
            original(self)
        except RecordingException:
            log.debug("py-cord stopped a recording that had already been stopped.")

    PacketRouter.run = run  # type: ignore[method-assign]
    _stop_patched = True
    return True


#: The opening of the warning py-cord raises from start_recording and
#: stop_recording. Matched on its first clause, which is enough to identify it
#: and short enough to survive the wording of the rest changing.
_STALE_WARNING = "Voice reception is currently broken"


def quieten_stale_receive_warning() -> bool:
    """Stop py-cord telling the user that reception cannot work, once it does.

    Starting and stopping a recording each warn that voice reception is broken
    upstream. On a stock install that is true. It stops being true the moment
    the decryption is repaired, and leaving it in place tells everyone their
    recording will fail while it is being written to disk in front of them.

    Suppressed only when a repair was actually applied, so a py-cord this
    module decided to leave alone keeps whatever it has to say about itself.
    """
    if not apply_receive_repair().applied:
        return False

    warnings.filterwarnings("ignore", message=re.escape(_STALE_WARNING), category=RuntimeWarning)
    return True


class _SenderReports(logging.Filter):
    """Drop the reader's complaint about a packet that is not unexpected."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not str(record.msg).startswith("Received unexpected rtcp packet")


def quieten_rtcp_reports() -> bool:
    """Stop the reader logging every RTCP sender report as a surprise.

    A sender report is an ordinary part of an RTP session, sent several times a
    minute by every participant. py-cord logs each one at INFO as an unexpected
    packet, which buries anything worth reading under forty lines a minute that
    describe nothing wrong.

    Only that one message is dropped, so the logger keeps saying everything
    else it says.
    """
    logger = logging.getLogger("discord.voice.receive.reader")
    if not any(isinstance(item, _SenderReports) for item in logger.filters):
        logger.addFilter(_SenderReports())
    return True


#: What the probe's decoder returns, standing in for decoded audio.
_PROBE_PCM = b"\x00\x01" * 480

_decode_replaced = False


class _ProbeOpusDecoder:
    """A decoder that returns known audio without needing libopus."""

    def decode(self, data: Any, *, fec: bool = False) -> bytes:
        return _PROBE_PCM


class _ProbeBuffer:
    """A jitter buffer holding nothing, so the probe takes the plain path."""

    def peek_next(self) -> Any:
        return None


class _PassthroughSession:
    """A session reporting every user as passthrough, recording what it decrypts."""

    ready = True

    def __init__(self) -> None:
        self.decrypted: list[Any] = []

    def can_passthrough(self, user_id: int) -> bool:
        return True

    def decrypt(self, user_id: int, media_type: Any, payload: Any) -> Any:
        self.decrypted.append(payload)
        return payload


class _ProbePacket:
    """A packet carrying a payload, truthy so the plain decode path is taken."""

    decrypted_data = b"opus"
    sequence = 7
    timestamp = 96000


def _decodes_without_decrypting(decoder_type: Any) -> bool:
    """Whether decoding leaves the audio alone rather than decrypting it again.

    The decoder is built without its ``__init__`` so the probe needs neither
    libopus nor a real jitter buffer, and its parts are supplied directly. What
    is being asked is only whether the session is reached at all.
    """
    session = _PassthroughSession()
    sink = type(
        "Sink",
        (),
        {
            "is_opus": lambda self: False,
            "client": type(
                "Client", (), {"_connection": type("C", (), {"dave_session": session})()}
            )(),
        },
    )()

    decoder = decoder_type.__new__(decoder_type)
    decoder.router = type("Router", (), {"sink": sink})()
    decoder.ssrc = _PROBE_SSRC
    decoder._decoder = _ProbeOpusDecoder()
    decoder._buffer = _ProbeBuffer()
    decoder._cached_id = 4242
    decoder._last_seq = -1
    decoder._last_ts = -1

    decoder._decode_packet(_ProbePacket())
    return not session.decrypted


def _build_decode_replacement() -> Any:
    """py-cord's decoding with the second decryption of the audio removed."""

    def _decode_packet(self: Any, packet: Any) -> Any:
        if packet:
            return packet, self._decoder.decode(packet.decrypted_data, fec=False)

        # A placeholder standing in for a packet that never arrived. The one
        # after it conceals the gap when it carries forward error correction,
        # and otherwise the decoder is asked to invent the interval itself.
        following = self._buffer.peek_next()
        if following is not None:
            return packet, self._decoder.decode(following.decrypted_data, fec=True)
        return packet, self._decoder.decode(None, fec=False)

    return _decode_packet


def recover_decoded_audio() -> bool:
    """Stop the audio being handed back to the session after it was decoded.

    ``_decode_packet`` turns the payload into linear audio and then, when the
    session reports the speaker as passthrough, gives that audio to
    ``dave.decrypt``. The payload was decrypted in ``decrypt_rtp`` before it was
    ever decoded, so this is a second decryption of something that is no longer
    ciphertext. It either corrupts the audio or raises, and it raises inside the
    router thread, which ends the recording.

    Passthrough is not the rare state it sounds like. py-cord turns it on from
    three places, on a DAVE downgrade, a session reset, and a transition
    recovery, all of which follow somebody joining or leaving the channel.

    Decided by running the decoder rather than by comparing versions, like every
    other repair here, so it retires itself once upstream removes the call.
    """
    global _decode_replaced
    if _decode_replaced:
        return True

    try:
        from discord.opus import PacketDecoder
    except Exception as error:
        log.debug("Cannot check how audio is decoded: %s", error)
        return False

    try:
        if _decodes_without_decrypting(PacketDecoder):
            return False
    except Exception as error:
        log.debug("Could not probe the decoder, so leaving it alone: %s", error)
        return False

    PacketDecoder._decode_packet = _build_decode_replacement()  # type: ignore[method-assign]
    _decode_replaced = True
    log.info("Repaired py-cord decoding, which decrypted audio it had already decoded.")
    return True
