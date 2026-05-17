"""
Symmetric encryption for Webflow API tokens stored at rest.

Tokens are encrypted with Fernet (AES-128-CBC + HMAC) using a key
derived from WEBFLOW_TOKEN_KEY (or the app SECRET_KEY as a fallback).

decrypt_token is intentionally tolerant: if a stored value is not a
valid ciphertext (e.g. a legacy plaintext row written before this
change, or a key rotation), it is returned unchanged so the app keeps
working and the value gets re-encrypted on the next save.
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _fernet():
    secret = (
        os.getenv("WEBFLOW_TOKEN_KEY")
        or os.getenv("SECRET_KEY")
        or "change-this-to-a-random-secret-key"
    )
    # Derive a stable 32-byte urlsafe-base64 key from whatever secret we have.
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_token(plaintext):
    if not plaintext:
        return plaintext
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(stored):
    if not stored:
        return stored
    try:
        return _fernet().decrypt(stored.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        # Legacy plaintext or undecryptable value — return as-is.
        return stored
