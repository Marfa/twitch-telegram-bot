#!/usr/bin/env python3
"""Freeze TOKEN_ENCRYPTION_KEY / TWITCH_EVENTSUB_SECRET from current TELEGRAM_BOT_TOKEN.

Idempotent: existing non-empty values are left unchanged.
Never prints secret values — only presence, lengths, and short fingerprints.
Run on the VPS host against /opt/twitch-telegram-bot/.env (or pass path).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import sys
from pathlib import Path

_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _KEY_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def _hkdf_sha256(ikm: bytes, *, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-SHA256 — matches cryptography HKDF used in token_crypto."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _fernet_key_from_bot_token(bot_token: str) -> str:
    derived = _hkdf_sha256(
        bot_token.encode("utf-8"),
        salt=b"twitch-telegram-bot-sync-v1",
        info=b"twitch-refresh-token",
        length=32,
    )
    return base64.urlsafe_b64encode(derived).decode("ascii")


def _eventsub_secret_from_bot_token(bot_token: str) -> str:
    material = f"{bot_token}:eventsub-v1".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _upsert_env(path: Path, updates: dict[str, str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line if line.endswith("\n") else line + "\n")
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}\n")
            seen.add(key)
        else:
            new_lines.append(line if line.endswith("\n") else line + "\n")
    for key, val in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={val}\n")
    path.write_text("".join(new_lines), encoding="utf-8")


def main() -> int:
    env_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
    if not env_path.is_file():
        print(f"error: missing env file: {env_path}", file=sys.stderr)
        return 1

    env = _parse_env(env_path.read_text(encoding="utf-8"))
    bot = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not bot or bot.startswith("your_"):
        print("error: TELEGRAM_BOT_TOKEN missing/placeholder", file=sys.stderr)
        return 1

    updates: dict[str, str] = {}
    enc = (env.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    if enc:
        print(f"TOKEN_ENCRYPTION_KEY: keep len={len(enc)} fp={_fp(enc)}")
    else:
        updates["TOKEN_ENCRYPTION_KEY"] = _fernet_key_from_bot_token(bot)
        print(
            "TOKEN_ENCRYPTION_KEY: set "
            f"len={len(updates['TOKEN_ENCRYPTION_KEY'])} "
            f"fp={_fp(updates['TOKEN_ENCRYPTION_KEY'])}"
        )

    sub = (env.get("TWITCH_EVENTSUB_SECRET") or "").strip()
    if sub:
        print(f"TWITCH_EVENTSUB_SECRET: keep len={len(sub)} fp={_fp(sub)}")
    else:
        updates["TWITCH_EVENTSUB_SECRET"] = _eventsub_secret_from_bot_token(bot)
        print(
            "TWITCH_EVENTSUB_SECRET: set "
            f"len={len(updates['TWITCH_EVENTSUB_SECRET'])} "
            f"fp={_fp(updates['TWITCH_EVENTSUB_SECRET'])}"
        )

    if not updates:
        print("noop: both secrets already set")
        return 0

    bak = env_path.with_suffix(env_path.suffix + ".bak-freeze")
    bak.write_text(env_path.read_text(encoding="utf-8"), encoding="utf-8")
    _upsert_env(env_path, updates)
    print(f"wrote {env_path} (backup {bak.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
