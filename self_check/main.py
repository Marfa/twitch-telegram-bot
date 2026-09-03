"""Orchestrate characterization self-checks."""
import os
import shutil
import subprocess
from pathlib import Path

# Monetization flags default off in source; self_check exercises production gates.
os.environ["ENABLE_PREMIUM"] = "1"
os.environ["ENABLE_HELP"] = "1"
os.environ["ENABLE_PARTNER"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_TOKEN_FOR_SELF_CHECK")

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


def check_product_flags() -> None:
    """self_check forces flags on; off paths unlock features / hide UI."""
    import config as cfg

    assert cfg.ENABLE_PREMIUM is True
    assert cfg.ENABLE_HELP is True
    assert cfg.ENABLE_PARTNER is True
    assert cfg.paid_features_free() is False
    assert cfg.show_premium_ui() is True
    assert cfg.show_help_button() is True
    assert cfg.show_partner_ui() is True
    saved = (cfg.ENABLE_PREMIUM, cfg.ENABLE_HELP, cfg.ENABLE_PARTNER)
    try:
        cfg.ENABLE_PREMIUM = False
        cfg.ENABLE_HELP = True
        assert cfg.paid_features_free() is True
        assert cfg.show_premium_ui() is False
        cfg.ENABLE_PREMIUM = True
        cfg.ENABLE_HELP = False
        assert cfg.paid_features_free() is True
        assert cfg.show_help_button() is False
        assert cfg.show_premium_ui() is False
        cfg.ENABLE_PARTNER = False
        assert cfg.show_partner_ui() is False
    finally:
        cfg.ENABLE_PREMIUM, cfg.ENABLE_HELP, cfg.ENABLE_PARTNER = saved


def main() -> None:
    check_undefined_names()
    check_product_flags()
    check_core()
    check_db_premium()
    check_handlers()
    from .checks_group_chat import check_group_chat

    check_group_chat()
    check_handler_smoke()
    from .checks_callback_wiring import check_callback_wiring
    from .checks_flow_nav import check_flow_nav

    check_flow_nav()
    check_callback_wiring()
    print("ok")


if __name__ == "__main__":
    main()
