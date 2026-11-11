#!/usr/bin/env python3
"""Local stdio MCP for prod Postgres via scripts/vps-psql.sh.

Live SoR = VPS (source auto/vps). Aiven is cold DR read-only through this server.
Supports SELECT and DML/DDL against live VPS — not read-only.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
VPS_PSQL = REPO_ROOT / "scripts" / "vps-psql.sh"
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

    cmd = [str(VPS_PSQL)]
    if effective in {"vps", "aiven"}:
        cmd.append(f"--{effective}")
    if fmt == "tuples":
        cmd.extend(["-At", "-c", sql])
    else:
        cmd.extend(["-c", sql])
    return cmd


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
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        cwd=str(REPO_ROOT),
    )
    out = proc.stdout or ""
    err = proc.stderr or ""
    # Never echo connection URLs; script already avoids logging Aiven URL.
    text = note
    if err.strip():
        text += err
        if not err.endswith("\n"):
            text += "\n"
    text += out
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n… truncated ({len(text)} chars)\n"
    return text, proc.returncode, note


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
    assert cmd[0].endswith("vps-psql.sh")
    assert "-At" in cmd and "-c" in cmd
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
