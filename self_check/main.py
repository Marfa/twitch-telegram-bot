"""Orchestrate characterization self-checks."""
import os
import shutil
import subprocess
from pathlib import Path

from .checks_core import check_core
from .checks_db_premium import check_db_premium
from .checks_handler_smoke import check_handler_smoke
from .checks_handlers import check_handlers

_ROOT = Path(__file__).resolve().parents[1]


def check_undefined_names() -> None:
    """Fail on used-but-not-imported names (post-split NameError class)."""
    ruff = shutil.which("ruff")
    assert ruff, (
        "ruff is required for F821 checks; install with: "
        "pip install -r requirements-dev.txt"
    )
    proc = subprocess.run(
        [ruff, "check", "--select", "F821", "--exclude", ".venv", str(_ROOT)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        "ruff F821 failed (undefined names):\n"
        f"{proc.stdout}{proc.stderr}"
    )


def main() -> None:
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_SELF_CHECK")
    check_undefined_names()
    check_core()
    check_db_premium()
    check_handlers()
    check_handler_smoke()
    print("ok")


if __name__ == "__main__":
    main()
