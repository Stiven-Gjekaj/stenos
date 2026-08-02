"""Tests for pointing OpenSSL at a certificate list that exists on this machine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from stenos.config import CERT_FILE_VARIABLE, certificate_bundle


def test_an_existing_setting_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Anyone pointing at a corporate root keeps it.
    monkeypatch.setenv(CERT_FILE_VARIABLE, "/etc/pki/corporate.pem")

    assert certificate_bundle() == "/etc/pki/corporate.pem"


def test_a_blank_setting_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CERT_FILE_VARIABLE, "")

    chosen = certificate_bundle()

    assert chosen is not None
    assert chosen != ""


def test_certifi_is_chosen_and_exported(monkeypatch: pytest.MonkeyPatch) -> None:
    certifi = pytest.importorskip("certifi")
    monkeypatch.delenv(CERT_FILE_VARIABLE, raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    chosen = certificate_bundle()

    assert chosen == certifi.where()
    # Exported rather than merely returned, because OpenSSL reads the
    # environment and never sees a return value.
    import os

    assert os.environ[CERT_FILE_VARIABLE] == chosen
    assert os.environ["SSL_CERT_DIR"] == str(Path(chosen).parent)


def test_the_chosen_bundle_is_a_real_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CERT_FILE_VARIABLE, raising=False)

    chosen = certificate_bundle()

    assert chosen is not None
    assert Path(chosen).is_file()
    # A certificate list that holds no certificates would verify nothing.
    assert "BEGIN CERTIFICATE" in Path(chosen).read_text(encoding="utf-8", errors="ignore")


def test_a_missing_certifi_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CERT_FILE_VARIABLE, raising=False)
    monkeypatch.setitem(sys.modules, "certifi", None)

    # Nothing to point at is not an error. The system paths may well work; the
    # frozen executable is the case where they do not.
    assert certificate_bundle() is None


def test_a_certifi_pointing_at_nothing_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    monkeypatch.delenv(CERT_FILE_VARIABLE, raising=False)
    stub = types.SimpleNamespace(where=lambda: "/nonexistent/cacert.pem")
    monkeypatch.setitem(sys.modules, "certifi", stub)

    assert certificate_bundle() is None
    import os

    assert CERT_FILE_VARIABLE not in os.environ
