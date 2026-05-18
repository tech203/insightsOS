"""Token-at-rest encryption (webflow_crypto).

Self-contained: imports only webflow_crypto, so it runs without the
full Flask app. Requires the `cryptography` package.
"""

import importlib

import pytest

wc = pytest.importorskip("webflow_crypto")


@pytest.fixture(autouse=True)
def _fixed_key(monkeypatch):
    # _fernet() reads the env at call time, so setting it here is enough.
    monkeypatch.setenv("WEBFLOW_TOKEN_KEY", "unit-test-key-aaaa")
    yield


def test_roundtrip():
    token = "wf_live_abc123SECRET"
    ciphertext = wc.encrypt_token(token)
    assert ciphertext != token
    assert wc.decrypt_token(ciphertext) == token


def test_ciphertext_is_not_plaintext_substring():
    token = "super-secret-value"
    assert token not in wc.encrypt_token(token)


@pytest.mark.parametrize("value", ["", None])
def test_empty_and_none_passthrough(value):
    assert wc.encrypt_token(value) == value
    assert wc.decrypt_token(value) == value


def test_legacy_plaintext_is_tolerated():
    # A value written before encryption existed must still be readable.
    assert wc.decrypt_token("legacy-plain-token") == "legacy-plain-token"


def test_key_rotation_does_not_crash(monkeypatch):
    ciphertext = wc.encrypt_token("rotate-me")
    monkeypatch.setenv("WEBFLOW_TOKEN_KEY", "a-different-key-bbbb")
    # Undecryptable under the new key -> return stored value, no exception.
    assert wc.decrypt_token(ciphertext) == ciphertext


def test_secret_key_is_used_when_token_key_absent(monkeypatch):
    monkeypatch.delenv("WEBFLOW_TOKEN_KEY", raising=False)
    monkeypatch.setenv("SECRET_KEY", "fallback-secret-cccc")
    importlib.reload(wc)
    token = "via-secret-key"
    assert wc.decrypt_token(wc.encrypt_token(token)) == token
