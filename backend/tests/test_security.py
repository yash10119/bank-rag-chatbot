import time

import jwt
import pytest

from app.config import settings
from app.security import (
    create_access_token,
    decode_access_token,
    decrypt_token,
    encrypt_token,
    make_internal_token,
    verify_internal_token,
)


def test_user_a_blob_cannot_be_opened_as_user_b():
    blob = encrypt_token("user-A", "gmail", {"access_token": "secret-A"})
    assert decrypt_token("user-A", "gmail", blob)["access_token"] == "secret-A"
    with pytest.raises(ValueError):
        decrypt_token("user-B", "gmail", blob)  # swapped row / wrong owner
    with pytest.raises(ValueError):
        decrypt_token("user-A", "calendar", blob)  # wrong service


def test_blob_is_not_plaintext():
    assert "secret-A" not in encrypt_token("user-A", "slack", {"access_token": "secret-A"})


def test_internal_token_binds_user_and_service():
    tok = make_internal_token("user-A", "gmail")
    assert verify_internal_token(tok, "gmail") == "user-A"
    with pytest.raises(jwt.PyJWTError):
        verify_internal_token(tok, "slack")  # audience mismatch


def test_internal_token_expires_and_rejects_forgery():
    expired = jwt.encode({"sub": "u", "aud": "mcp:gmail", "iss": "bank-api", "exp": int(time.time()) - 5}, settings.mcp_internal_secret, algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        verify_internal_token(expired, "gmail")
    forged = jwt.encode({"sub": "u", "aud": "mcp:gmail", "iss": "bank-api", "exp": int(time.time()) + 60}, "wrong-secret", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        verify_internal_token(forged, "gmail")


def test_session_token_roundtrip_and_session_token_not_valid_for_mcp():
    tok = create_access_token("user-A", "customer")
    assert decode_access_token(tok)["sub"] == "user-A"
    with pytest.raises(jwt.PyJWTError):
        verify_internal_token(tok, "gmail")
