"""Credential diagnostics describe the supported storage/platform contract."""

import pytest
from cryptography.fernet import Fernet

from observability.doctor import run_checks


def snapshot(token, *, windows=False, has_key=True):
    return {"mode": "cloud", "provider": "fixture", "has_key": has_key,
            "is_windows": windows, "key_tokens": {"fixture": token}}


def test_valid_fernet_credentials_are_supported_off_windows():
    cipher = Fernet(Fernet.generate_key())
    encrypted = cipher.encrypt(b"synthetic-diagnostic-key")
    assert cipher.decrypt(encrypted) == b"synthetic-diagnostic-key"
    result = run_checks(snapshot("fernet:" + encrypted.decode()))
    assert result["summary"] == "ok", result


@pytest.mark.parametrize("token,windows", [("dpapi:synthetic", True)])
def test_supported_windows_credentials_are_not_flagged_as_plaintext(token, windows):
    assert run_checks(snapshot(token, windows=windows))["summary"] == "ok"


@pytest.mark.parametrize("token,windows,storage", [
    ("dpapi:synthetic", False, "DPAPI"),
    ("fernet:synthetic", True, "Fernet"),
])
def test_cross_platform_encryption_is_reported_as_unsupported(token, windows, storage):
    result = run_checks(snapshot(token, windows=windows))
    assert result["summary"] == "critical"
    assert any(storage in row["detail"] for row in result["findings"])
    assert not any("not encrypted" in row["title"] for row in result["findings"])


@pytest.mark.parametrize("windows,expected", [(False, "warn"), (True, "critical")])
def test_legacy_plaintext_is_dev_only_off_windows(windows, expected):
    result = run_checks(snapshot("plain:c3ludGhldGlj", windows=windows))
    assert result["summary"] == expected
    assert "Settings" in result["findings"][0]["fix"]


@pytest.mark.parametrize("windows", [False, True])
def test_unrecognized_storage_stays_critical(windows):
    assert run_checks(snapshot("synthetic-raw-token", windows=windows))["summary"] == "critical"


def test_unavailable_configured_key_still_reports_connection_problem():
    result = run_checks(snapshot("fernet:synthetic", has_key=False))
    assert result["summary"] == "warn"
    assert any("no key" in row["title"] for row in result["findings"])
    assert any("Settings" in row["fix"] for row in result["findings"])
