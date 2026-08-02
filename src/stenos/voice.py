"""Inspection of the voice transport, so a recording that captured nothing can say why.

Discord encrypts ordinary voice calls end to end with DAVE. py-cord decrypts
received audio through it, and the receive path yields audio only once a DAVE
session exists and its handshake has completed. Until then every packet is
discarded, and a packet that fails to decrypt afterwards is replaced with
silence. Both are logged below the default level, so a recording that captured
nothing is otherwise indistinguishable from a call in which nobody spoke.

Nothing here changes how audio is received. It reads the state the voice client
already holds, so that state can be reported instead of guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "PYCORD_RECEIVE_ISSUE",
    "DaveState",
    "DaveSupport",
    "ReceiveSupport",
    "dave_state",
    "dave_support",
    "receive_support",
]

#: Where py-cord tracks the state of voice reception.
PYCORD_RECEIVE_ISSUE = "https://github.com/Pycord-Development/pycord/issues/3139"


def _davey() -> Any | None:
    """The DAVE implementation, or None when it is not installed.

    Imported on each call rather than at module import because the answer is
    reported by a diagnostic command, and a stale answer cached at import time
    would be worth less than the cost of the lookup.
    """
    try:
        import davey
    except ImportError:
        return None
    return davey


@dataclass(frozen=True, slots=True)
class DaveSupport:
    """What this installation can do about end to end encryption before connecting."""

    available: bool
    version: str | None
    protocol_version: int

    @property
    def summary(self) -> str:
        """One line naming the library and the protocol it speaks."""
        if not self.available:
            return "unavailable (davey is not installed)"
        version = self.version or "unknown version"
        return f"davey {version}, protocol {self.protocol_version}"


@dataclass(frozen=True, slots=True)
class DaveState:
    """What one live voice connection negotiated.

    ``negotiated_version`` of zero means the call is not end to end encrypted.
    A session is created only when the server offers a non-zero version, so an
    absent session and a zero version normally occur together.
    """

    support: DaveSupport
    negotiated_version: int
    session_present: bool
    ready: bool
    status: str

    @property
    def receives_audio(self) -> bool:
        """Whether the receive path will currently yield decoded audio.

        py-cord returns audio only from the branch guarded by a present and
        ready session. Any other state discards the packet, so this is the
        condition under which a recording can capture anything at all.
        """
        return self.session_present and self.ready

    @property
    def summary(self) -> str:
        """One line describing the negotiated state of the connection."""
        if not self.session_present:
            return f"no session (negotiated protocol {self.negotiated_version})"
        return f"session {self.status}, protocol {self.negotiated_version}, ready {self.ready}"


@dataclass(frozen=True, slots=True)
class ReceiveSupport:
    """What the installed py-cord can do about receiving audio."""

    version: str
    adapted: bool

    @property
    def summary(self) -> str:
        """One line naming the version and whether its sinks needed adapting."""
        if not self.adapted:
            return f"py-cord {self.version}"
        return (
            f"py-cord {self.version}, sink contract adapted "
            f"to its rewritten receive path (see {PYCORD_RECEIVE_ISSUE})"
        )


def receive_support() -> ReceiveSupport:
    """Report whether this py-cord can register a sink without help.

    py-cord 2.8 rewrote the receive path and left every one of its own sinks
    behind, so a recording cannot start on a stock sink at all. This sink
    supplies what the router asks for, and reporting that says why the version
    number alone would not explain a recording that works here and nowhere
    else.
    """
    try:
        import discord
        from discord.sinks import Sink
    except Exception:
        return ReceiveSupport(version="unknown", adapted=False)

    return ReceiveSupport(
        version=getattr(discord, "__version__", "unknown"),
        adapted=not hasattr(Sink, "__sink_listeners__"),
    )


def dave_support() -> DaveSupport:
    """Report whether this installation can take part in an encrypted call.

    Answerable without a connection, so it belongs in the environment report
    beside the opus check: both are libraries whose absence is only discovered
    when audio fails to arrive.
    """
    davey = _davey()
    if davey is None:
        return DaveSupport(available=False, version=None, protocol_version=0)
    return DaveSupport(
        available=True,
        version=getattr(davey, "__version__", None),
        protocol_version=int(getattr(davey, "DAVE_PROTOCOL_VERSION", 0)),
    )


def dave_state(voice_client: Any) -> DaveState:
    """Read the encryption state of a live voice client.

    The state lives on a private attribute of the voice client, so every read
    is guarded. A future py-cord that moves or renames it should degrade to
    reporting an absent session rather than raising in the middle of a
    recording.
    """
    support = dave_support()
    connection = getattr(voice_client, "_connection", None)
    if connection is None:
        return DaveState(
            support=support,
            negotiated_version=0,
            session_present=False,
            ready=False,
            status="unknown",
        )

    negotiated = int(getattr(connection, "dave_protocol_version", 0) or 0)
    session = getattr(connection, "dave_session", None)
    if session is None:
        return DaveState(
            support=support,
            negotiated_version=negotiated,
            session_present=False,
            ready=False,
            status="absent",
        )

    return DaveState(
        support=support,
        negotiated_version=negotiated,
        session_present=True,
        ready=bool(getattr(session, "ready", False)),
        status=_status_name(getattr(session, "status", None)),
    )


def _status_name(status: Any) -> str:
    """Render a session status without depending on its type.

    davey reports a native enum member with no name attribute, whose str()
    carries the type as well as the value, as in SessionStatus.inactive. Only
    the value is wanted, and a plain string or None has to survive the same
    path.
    """
    if status is None:
        return "unknown"
    name = getattr(status, "name", None)
    if isinstance(name, str):
        return name
    return str(status).rpartition(".")[2] or str(status)
