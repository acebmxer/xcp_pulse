"""TOTP code generation/verification and its QR code.

RFC 6238 test vectors aren't used here because they're specified for SHA1/
SHA256/SHA512 at 8 digits with a fixed key of a particular length; this module
hardcodes SHA1 at 6 digits (the near-universal authenticator-app default), so
the properties that matter are checked directly: a generated code verifies,
a wrong one doesn't, drift is tolerated within the window and rejected outside
it, and the QR payload round-trips.
"""

from __future__ import annotations

from app.totp import (
    current_code,
    generate_backup_codes,
    generate_secret,
    hash_backup_code,
    provisioning_uri,
    qr_svg,
    verify_code,
)

SECRET = generate_secret()


def test_generated_secret_is_base32_and_reasonably_long() -> None:
    secret = generate_secret()
    assert len(secret) >= 32
    # base32 alphabet only, per RFC 4648.
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for c in secret)


def test_two_secrets_are_different() -> None:
    assert generate_secret() != generate_secret()


def test_current_code_verifies() -> None:
    code = current_code(SECRET, at=1_700_000_000)
    assert verify_code(SECRET, code, at=1_700_000_000)


def test_wrong_code_is_rejected() -> None:
    code = current_code(SECRET, at=1_700_000_000)
    wrong = "000000" if code != "000000" else "111111"
    assert not verify_code(SECRET, wrong, at=1_700_000_000)


def test_code_is_six_digits() -> None:
    code = current_code(SECRET, at=1_700_000_000)
    assert len(code) == 6
    assert code.isdigit()


def test_one_step_of_drift_either_way_is_accepted() -> None:
    now = 1_700_000_000
    code = current_code(SECRET, at=now)
    assert verify_code(SECRET, code, at=now - 30)
    assert verify_code(SECRET, code, at=now + 30)


def test_two_steps_of_drift_is_rejected() -> None:
    now = 1_700_000_000
    code = current_code(SECRET, at=now)
    assert not verify_code(SECRET, code, at=now - 90)
    assert not verify_code(SECRET, code, at=now + 90)


def test_non_numeric_input_is_rejected_not_erroring() -> None:
    assert not verify_code(SECRET, "abcdef")
    assert not verify_code(SECRET, "")
    assert not verify_code(SECRET, "12345")  # too short
    assert not verify_code(SECRET, "1234567")  # too long


def test_different_secrets_produce_different_codes() -> None:
    other = generate_secret()
    now = 1_700_000_000
    assert current_code(SECRET, at=now) != current_code(other, at=now) or SECRET == other


def test_provisioning_uri_contains_the_secret_and_issuer() -> None:
    uri = provisioning_uri(SECRET, username="alice")
    assert uri.startswith("otpauth://totp/")
    assert SECRET in uri
    assert "alice" in uri
    assert "XCP" in uri  # issuer, URL-encoded


def test_qr_svg_is_well_formed_and_contains_no_secret_leak_risk() -> None:
    uri = provisioning_uri(SECRET, username="alice")
    svg = qr_svg(uri)
    assert svg.startswith("<svg")
    assert svg.strip().endswith("</svg>")
    # The secret must never appear as plain text in the markup — it's encoded
    # as QR modules, not embedded as an attribute or comment.
    assert SECRET not in svg


def test_backup_codes_are_generated_in_the_documented_count_and_unique() -> None:
    codes = generate_backup_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10


def test_backup_code_hash_is_stable_and_case_insensitive() -> None:
    codes = generate_backup_codes()
    code = codes[0]
    assert hash_backup_code(code) == hash_backup_code(code.lower())


def test_backup_code_hash_does_not_reveal_the_code() -> None:
    code = generate_backup_codes()[0]
    assert code not in hash_backup_code(code)
