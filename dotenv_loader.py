"""Load `.env` for local/dev, including dotenvx-encrypted values.

Plaintext `.env` (Docker/VPS) keeps working unchanged.
Encrypted values (`encrypted:…`) are decrypted via the `dotenvx` CLI when a
private key is available (`DOTENV_PRIVATE_KEY`, `.env.keys`, OS secret store,
or Bitwarden / 1Password references resolved by dotenvx).

Never commit `.env.keys` or plaintext production secrets to the public repo.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_CIPHER_PREFIX = "encrypted:"
_PUBLIC_KEY_PREFIX = "DOTENV_PUBLIC_KEY"


def parse_env_text(text: str) -> dict[str, str]:
    """Parse KEY=value lines; strip optional matching quotes on values."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def text_has_ciphertext(text: str) -> bool:
    for key, value in parse_env_text(text).items():
        if key.startswith(_PUBLIC_KEY_PREFIX):
            continue
        if value.startswith(_CIPHER_PREFIX):
            return True
    return False


def ciphertext_keys_unresolved(pairs: dict[str, str]) -> list[str]:
    """Keys still missing a plaintext value in the process environment."""
    missing: list[str] = []
    for key, value in pairs.items():
        if key.startswith(_PUBLIC_KEY_PREFIX):
            continue
        if not value.startswith(_CIPHER_PREFIX):
            continue
        cur = os.environ.get(key, "")
        if not cur or cur.startswith(_CIPHER_PREFIX):
            missing.append(key)
    return missing


def _decrypt_stdout(path: Path) -> str | None:
    exe = shutil.which("dotenvx")
    if not exe:
        return None
    try:
        # bytes + UTF-8: Windows consoles may use cp1251 and choke on dotenvx output
        proc = subprocess.run(
            [exe, "decrypt", "-f", str(path), "--stdout"],
            capture_output=True,
            text=False,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or b"").decode("utf-8", errors="replace")


def _encrypted_env_error(path: Path, missing: list[str]) -> RuntimeError:
    sample = ", ".join(missing[:8])
    more = f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""
    return RuntimeError(
        f"Encrypted values in {path} but no decrypt key for: {sample}{more}.\n"
        "Install dotenvx (https://dotenvx.com), put DOTENV_PRIVATE_KEY in "
        "Bitwarden (or `.env.keys` / OS secret store), then either:\n"
        "  • export/set the key and run `python main.py`, or\n"
        "  • `dotenvx run -- python main.py`\n"
        "Docker/VPS: keep a plaintext `.env` on the server (do not bake "
        "`.env.keys` into the image). See docs/dotenvx.md."
    )


def apply_env_pairs(pairs: dict[str, str], *, overload: bool = False) -> None:
    """Apply pairs to os.environ. Never inject DOTENV_PRIVATE_KEY* from a file."""
    for key, value in pairs.items():
        if key.startswith("DOTENV_PRIVATE_KEY"):
            continue
        if overload:
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def load_dotenv(path: Path | None = None, *, overload: bool = False) -> None:
    """Load `.env` into the process environment (setdefault unless overload)."""
    env_path = path or Path(".env")
    if not env_path.is_file():
        return
    raw = env_path.read_text(encoding="utf-8")
    pairs = parse_env_text(raw)
    if text_has_ciphertext(raw):
        decrypted = _decrypt_stdout(env_path)
        if decrypted is not None:
            pairs = parse_env_text(decrypted)
        else:
            missing = ciphertext_keys_unresolved(pairs)
            if missing:
                raise _encrypted_env_error(env_path, missing)
            return
    apply_env_pairs(pairs, overload=overload)
