"""Encrypt Twitch OAuth refresh tokens at rest (Fernet)."""
from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    """Build Fernet from TOKEN_ENCRYPTION_KEY or derive from TELEGRAM_BOT_TOKEN."""
    raw = (os.getenv("TOKEN_ENCRYPTION_KEY") or "").strip()
    if raw:
        try:
            return Fernet(raw.encode("ascii"))
        except (ValueError, TypeError):
            material = hashlib.sha256(raw.encode("utf-8")).digest()
            return Fernet(base64.urlsafe_b64encode(material))
    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").encode("utf-8")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN required to encrypt Twitch tokens")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"twitch-telegram-bot-sync-v1",
        info=b"twitch-refresh-token",
    ).derive(bot_token)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    if plaintext.startswith(_PREFIX):
        return plaintext
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt_secret(stored: str) -> str:
    if not stored:
        return ""
    if not stored.startswith(_PREFIX):
        # Legacy plaintext — returned as-is; re-encrypted on next write.
        return stored
    blob = stored[len(_PREFIX) :].encode("ascii")
    try:
        return _fernet().decrypt(blob).decode("utf-8")
    except InvalidToken:
        logger.error(
            "Failed to decrypt Twitch token — check TOKEN_ENCRYPTION_KEY / bot token"
        )
        raise
