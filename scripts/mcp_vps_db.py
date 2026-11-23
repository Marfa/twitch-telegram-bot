#!/usr/bin/env python3
"""Local stdio MCP for prod Postgres via scripts/vps-psql.sh.

Live SoR = VPS (source auto/vps). Aiven is cold DR read-only through this server.
Supports SELECT and DML/DDL against live VPS — not read-only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
VPS_PSQL = REPO_ROOT / "scripts" / "vps-psql.sh"
DEFAULT_VPS_SSH_KEY = REPO_ROOT / ".ssh" / "artalk_vps"
_GIT_BASH_CANDIDATES = (
    Path(r"C:\Program Files\Git\bin\bash.exe"),
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
)


def _windows_bash() -> str:
    """Prefer Git Bash on Windows; avoid the WSL `bash.exe` stub in System32."""
    for path in _GIT_BASH_CANDIDATES:
        if path.is_file():
            return str(path)
    which = shutil.which("bash")
    if which:
        # System32\bash.exe is the WSL launcher and fails without a distro.
        resolved = str(Path(which).resolve())
        if "system32" in resolved.lower() and which.lower().endswith("bash.exe"):
            raise FileNotFoundError(
                "Git Bash not found (System32 bash is WSL-only). "
                "Install Git for Windows or set PATH to Git\\bin\\bash.exe"
            )
        return which
    raise FileNotFoundError(
        "bash not found; install Git for Windows to run scripts/vps-psql.sh"
    )


def _posix_path_for_git_bash(path: Path) -> str:
    """Convert a Windows path to a Git Bash /c/... form."""
    resolved = path.resolve()
    s = str(resolved)
    if os.name != "nt":
        return s
    # C:\foo\bar → /c/foo/bar
    if len(s) >= 2 and s[1] == ":":
        drive = s[0].lower()
        rest = s[2:].replace("\\", "/").lstrip("/")
        return f"/{drive}/{rest}"
    return s.replace("\\", "/")


def _resolve_vps_ssh_key_file(env: dict[str, str]) -> Path | None:
    """Absolute path to the private key file, or None."""
    raw = (env.get("VPS_SSH_KEY") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        # Ignore POSIX-shaped values from a previous MCP hop; resolve against repo.
        if raw.startswith("/c/") or raw.startswith("/C/"):
            # /c/Users/... → C:/Users/...
            path = Path(raw[1] + ":" + raw[2:].replace("/", "\\"))
        elif not path.is_absolute():
            path = REPO_ROOT / path
        return path if path.is_file() else None
    if DEFAULT_VPS_SSH_KEY.is_file():
        return DEFAULT_VPS_SSH_KEY
    return None


def _lf_key_copy(src: Path) -> Path:
    """Temp LF-only copy of an OpenSSH private key (Git ssh rejects CRLF)."""
    import tempfile

    data = src.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    fd, name = tempfile.mkstemp(prefix="artalk_vps_", suffix=".key")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except Exception:
        os.close(fd)
        raise
    try:
        os.chmod(name, 0o600)
    except OSError:
        pass
    return Path(name)


def _ensure_vps_ssh_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """Ensure VPS_SSH_KEY points at a usable key file for child processes.

    Relative paths resolve against the repo root (not the process cwd).
    If unset, default to <repo>/.ssh/artalk_vps when that file exists.
    On Windows the value is a LF-normalized temp copy in POSIX form so Git
    Bash + Git OpenSSH can load keys that were saved with CRLF.
    """
    out = dict(env if env is not None else os.environ)
    path = _resolve_vps_ssh_key_file(out)
    if path is None:
        return out
    if os.name == "nt":
        path = _lf_key_copy(path)
    out["VPS_SSH_KEY"] = _posix_path_for_git_bash(path)
    return out
SERVER_NAME = "vps-db"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
MAX_OUTPUT_CHARS = 200_000
DEFAULT_TIMEOUT_SEC = 120

_READ_FIRST = frozenset(
    {"SELECT", "SHOW", "EXPLAIN", "VALUES", "TABLE", "WITH"}
)
_WITH_WRITE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|MERGE|TRUNCATE|REFRESH|COPY|CALL|DO)\b",
    re.I,
)


def _strip_sql_noise(sql: str) -> str:
    """Remove comments and string literals so keyword checks ignore literals."""
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue
        if c in "'\"":
            quote = c
            i += 1
            while i < n:
                if sql[i] == quote:
                    if quote == "'" and i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                if sql[i] == "\\" and quote == '"':
                    i += 2
                    continue
                i += 1
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def looks_mutating(sql: str) -> bool:
    """True if SQL is not a clear read (SELECT/SHOW/EXPLAIN/…)."""
    cleaned = _strip_sql_noise(sql)
    for part in cleaned.split(";"):
        part = part.strip()
        if not part:
            continue
        first = part.split(None, 1)[0].upper()
        if first == "WITH":
            if _WITH_WRITE.search(part):
                return True
            continue
        if first in _READ_FIRST:
            continue
        return True
    return False


def build_cmd(
    sql: str,
    *,
    source: str = "auto",
    fmt: str = "aligned",
) -> list[str]:
    if source not in {"auto", "vps", "aiven"}:
        raise ValueError("source must be auto|vps|aiven")
    if fmt not in {"aligned", "tuples"}:
        raise ValueError("format must be aligned|tuples")
    if not VPS_PSQL.is_file():
        raise FileNotFoundError(f"missing {VPS_PSQL}")

    mutating = looks_mutating(sql)
    if mutating and source == "aiven":
        raise ValueError(
            "refusing write/DDL on Aiven cold DR; use source=vps (live SoR)"
        )
    effective = "vps" if mutating and source == "auto" else source

    script = [str(VPS_PSQL)]
    if effective in {"vps", "aiven"}:
        script.append(f"--{effective}")
    if fmt == "tuples":
        script.extend(["-At", "-c", sql])
    else:
        script.extend(["-c", sql])
    # On Windows the shebang is ignored; run via Git Bash (not WSL stub).
    if os.name == "nt":
        return [_windows_bash(), *script]
    return script


def run_sql(
    sql: str,
    *,
    source: str = "auto",
    fmt: str = "aligned",
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> tuple[str, int, str]:
    """Return (stdout, returncode, note). note explains forced --vps for writes."""
    sql = (sql or "").strip()
    if not sql:
        raise ValueError("sql is empty")
    note = ""
    if looks_mutating(sql) and source == "auto":
        note = "note: mutating SQL → forced --vps (no Aiven fallback)\n"
    cmd = build_cmd(sql, source=source, fmt=fmt)
    env = _ensure_vps_ssh_env()
    lf_key = None
    raw_key = (env.get("VPS_SSH_KEY") or "").strip()
    if os.name == "nt" and len(raw_key) >= 3 and raw_key[0] == "/" and raw_key[2] == "/":
        # Track temp LF key for cleanup (/c/... → Windows path).
        lf_key = Path(raw_key[1] + ":" + raw_key[2:].replace("/", "\\"))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=str(REPO_ROOT),
            env=env,
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        code = proc.returncode
    finally:
        if lf_key is not None:
            try:
                lf_key.unlink(missing_ok=True)
            except OSError:
                pass
    # Never echo connection URLs; script already avoids logging Aiven URL.
    text = note
    if err.strip():
        text += err
        if not err.endswith("\n"):
            text += "\n"
    text += out
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n… truncated ({len(text)} chars)\n"
    return text, code, note


TOOLS = [
    {
        "name": "run_sql",
        "description": (
            "Run SQL against prod Postgres via scripts/vps-psql.sh. "
            "Live SoR is VPS (source=auto|vps). Supports SELECT and writes "
            "(INSERT/UPDATE/DELETE/DDL). Mutating SQL never falls back to "
            "Aiven and refuses source=aiven. Aiven is cold DR (may lag; no igdb_*)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "SQL to run (read or write).",
                },
                "source": {
                    "type": "string",
                    "enum": ["auto", "vps", "aiven"],
                    "default": "auto",
                    "description": (
                        "auto: VPS then Aiven for reads; writes always VPS. "
                        "vps: live only. aiven: DR reads only."
                    ),
                },
                "format": {
                    "type": "string",
                    "enum": ["aligned", "tuples"],
                    "default": "aligned",
                    "description": "aligned = psql default; tuples = -At",
                },
            },
            "required": ["sql"],
        },
    }
]


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name") or ""
    args = params.get("arguments") or {}
    if isinstance(args, str):
        args = json.loads(args) if args else {}
    if name != "run_sql":
        return _result(f"unknown tool: {name}", is_error=True)
    try:
        text, code, _ = run_sql(
            str(args.get("sql") or ""),
            source=str(args.get("source") or "auto"),
            fmt=str(args.get("format") or "aligned"),
        )
    except subprocess.TimeoutExpired:
        return _result("error: query timed out", is_error=True)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        return _result(f"error: {exc}", is_error=True)
    if code != 0:
        return _result(text or f"error: exit {code}", is_error=True)
    return _result(text if text.strip() else "(no output)")


def _dispatch(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    req_id = msg.get("id", None)
    # Notifications have no id and get no response.
    if req_id is None and method and not str(method).startswith("rpc."):
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }
    if method == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": _handle_tools_call(msg.get("params") or {}),
        }
    if method is None and "result" in msg:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _self_check() -> None:
    assert looks_mutating("SELECT 1") is False
    assert looks_mutating("select * from users") is False
    assert looks_mutating("UPDATE users SET x=1") is True
    assert looks_mutating("WITH c AS (SELECT 1) SELECT * FROM c") is False
    assert looks_mutating("WITH c AS (SELECT 1) INSERT INTO t SELECT * FROM c") is True
    assert looks_mutating("SELECT 'UPDATE users' AS s") is False
    cmd = build_cmd("SELECT 1", source="auto", fmt="tuples")
    assert any(str(p).endswith("vps-psql.sh") for p in cmd)
    assert "-At" in cmd and "-c" in cmd
    if os.name == "nt":
        assert cmd[0].lower().endswith("bash.exe") or cmd[0].endswith("bash")
    else:
        assert cmd[0].endswith("vps-psql.sh")
    w = build_cmd("DELETE FROM t WHERE false", source="auto", fmt="aligned")
    assert "--vps" in w
    try:
        build_cmd("DELETE FROM t WHERE false", source="aiven", fmt="aligned")
        raise AssertionError("expected aiven write refusal")
    except ValueError:
        pass
    print("mcp_vps_db self-check ok", file=sys.stderr)


def main() -> None:
    if "--self-check" in sys.argv:
        _self_check()
        return
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            err = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err) + "\n")
            sys.stdout.flush()
            continue
        resp = _dispatch(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
