"""Tests for reading the encryption state of a voice connection."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from stenos.voice import (
    PYCORD_RECEIVE_ISSUE,
    DaveSupport,
    ReceiveSupport,
    _status_name,
    dave_state,
    dave_support,
    receive_support,
)


def _client(**connection: Any) -> SimpleNamespace:
    """A stand in for a voice client carrying the given connection state."""
    return SimpleNamespace(_connection=SimpleNamespace(**connection))


def test_support_reports_the_installed_library() -> None:
    support = dave_support()

    # davey is a dependency of py-cord's voice extra, so it is present wherever
    # the sink can run at all.
    assert support.available is True
    assert support.protocol_version >= 1
    assert "davey" in support.summary


def test_support_reports_a_missing_library(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry in sys.modules makes the import statement raise ImportError,
    # which exercises the guard rather than the wrapper around it.
    monkeypatch.setitem(sys.modules, "davey", None)

    support = dave_support()

    assert support.available is False
    assert support.version is None
    assert support.protocol_version == 0
    assert "not installed" in support.summary


def test_state_of_a_client_without_a_connection() -> None:
    # A py-cord that renames the attribute must degrade to reporting an absent
    # session rather than raising part way through a recording.
    state = dave_state(object())

    assert state.session_present is False
    assert state.receives_audio is False
    assert state.status == "unknown"


def test_state_of_an_unencrypted_call() -> None:
    state = dave_state(_client(dave_protocol_version=0, dave_session=None))

    assert state.negotiated_version == 0
    assert state.session_present is False
    assert state.status == "absent"
    assert state.receives_audio is False
    assert "no session" in state.summary


def test_state_of_a_session_still_completing_its_handshake() -> None:
    session = SimpleNamespace(ready=False, status="pending")

    state = dave_state(_client(dave_protocol_version=1, dave_session=session))

    assert state.session_present is True
    assert state.ready is False
    # Present but not ready still discards every packet, so this must not be
    # reported as a connection that can record.
    assert state.receives_audio is False
    assert "pending" in state.summary


def test_state_of_a_ready_session() -> None:
    session = SimpleNamespace(ready=True, status="active")

    state = dave_state(_client(dave_protocol_version=1, dave_session=session))

    assert state.session_present is True
    assert state.ready is True
    assert state.receives_audio is True


def test_missing_attributes_do_not_raise() -> None:
    # A session object that reports neither readiness nor status is treated as
    # not ready, which is the safe direction: it reports a problem that may not
    # exist rather than staying silent about one that does.
    state = dave_state(_client(dave_session=SimpleNamespace()))

    assert state.session_present is True
    assert state.ready is False
    assert state.status == "unknown"
    assert state.negotiated_version == 0


def test_native_enum_status_loses_its_type_prefix() -> None:
    davey = pytest.importorskip("davey")

    # davey's status is a native enum with no name attribute, whose str carries
    # the type as well as the value.
    assert _status_name(davey.SessionStatus.inactive) == "inactive"
    assert _status_name(davey.SessionStatus.active) == "active"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "unknown"),
        ("active", "active"),
        (SimpleNamespace(name="ready"), "ready"),
    ],
)
def test_status_rendering_survives_other_shapes(status: Any, expected: str) -> None:
    assert _status_name(status) == expected


def test_support_summary_without_a_reported_version() -> None:
    support = DaveSupport(available=True, version=None, protocol_version=1)

    assert "unknown version" in support.summary


def test_a_pycord_needing_no_adaptation_is_reported_plainly() -> None:
    assert ReceiveSupport(version="9.9.9", adapted=False).summary == "py-cord 9.9.9"


def test_an_adapted_pycord_says_what_was_adapted_and_not_that_it_cannot_work() -> None:
    # It said reception was broken upstream, which was true of a stock install
    # and stopped being true once the decryption was repaired. Leaving it there
    # told everyone their recording would fail while it was being written.
    summary = ReceiveSupport(version="2.8.1", adapted=True).summary

    assert "broken" not in summary
    assert "sink contract adapted" in summary
    assert PYCORD_RECEIVE_ISSUE in summary


def test_the_installed_pycord_is_reported() -> None:
    support = receive_support()

    assert support.version in support.summary
