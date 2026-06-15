import pytest
from app.auth import create_access_token, verify_access_token, hash_password, verify_password


def test_hash_and_verify_password():
    h = hash_password("secret123")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)


def test_hash_is_not_plaintext():
    h = hash_password("secret123")
    assert h != "secret123"
    assert h.startswith("$2b$")


def test_create_and_verify_token():
    token = create_access_token(user_id="abc-123", role="user")
    payload = verify_access_token(token)
    assert payload["sub"] == "abc-123"
    assert payload["role"] == "user"


def test_verify_invalid_token_returns_none():
    assert verify_access_token("not.a.token") is None


def test_verify_tampered_token_returns_none():
    token = create_access_token(user_id="abc", role="user")
    tampered = token[:-4] + "xxxx"
    assert verify_access_token(tampered) is None
