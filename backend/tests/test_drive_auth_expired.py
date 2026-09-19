"""Drive auth expiry helpers."""

from app.drive.google_client import DriveAuthExpiredError, _is_invalid_grant


def test_invalid_grant_detection():
    assert _is_invalid_grant(Exception("invalid_grant: Token has been expired or revoked."))
    assert _is_invalid_grant(Exception("('invalid_grant: Token has been expired or revoked.', {})"))
    assert not _is_invalid_grant(Exception("rate limit exceeded"))


def test_drive_auth_expired_error_code():
    err = DriveAuthExpiredError("reconnect")
    assert err.error_code == "drive_auth_expired"
    assert "reconnect" in str(err)
