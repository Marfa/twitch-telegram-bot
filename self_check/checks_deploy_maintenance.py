"""Self-check: one restart-safe window serves every bot restart.

scripts/bot-maintenance.sh snapshots the running bot image, stops the bot, runs
the step and brings the bot back on every exit path. The two upgrade scripts take
their row-count table list from one source that follows the database catalog.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "bot-maintenance.sh"
ROW_COUNTS = ROOT / "scripts" / "pg-row-counts.sh"
COMPOSE = "compose.vps.yml"
IMAGE_REF = "twitch-telegram-bot-bot:latest"
SNAPSHOT_REF = "twitch-telegram-bot-bot:maintenance-snapshot"
IMAGE_ID = "sha256:0123456789ab"
CONTAINER = "ctr-bot-maintenance"

# Stands in for docker: records every call and answers the two inspect calls the
# window uses to find the image the bot runs.
_FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$FAKE_DOCKER_LOG"
if [[ "$1" == "compose" ]]; then
  shift
  shift
  shift
  if [[ "$1" == "ps" ]]; then
    printf '%s\\n' "$FAKE_DOCKER_CONTAINER"
  fi
  exit 0
fi
if [[ "$1" == "inspect" ]]; then
  case "$3" in
    *Config.Image*) printf '%s\\n' "$FAKE_DOCKER_IMAGE_REF" ;;
    *) printf '%s\\n' "$FAKE_DOCKER_IMAGE_ID" ;;
  esac
  exit 0
fi
exit 0
"""

_FAKE_CURL = """#!/usr/bin/env bash
exit "${FAKE_HEALTH_EXIT:-1}"
"""


def _fake_env(tmp: Path, **extra: str) -> dict[str, str]:
    """Install the fake docker and curl for one temp dir and build their environment."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in (("docker", _FAKE_DOCKER), ("curl", _FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    scripts_dir = tmp / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    (scripts_dir / "wait-bot-health.sh").write_text(
        (ROOT / "scripts" / "wait-bot-health.sh").read_text(encoding="utf-8"), encoding="utf-8"
    )
    log = tmp / "docker.log"
    path_env = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    env = {
        "PATH": path_env,
        "APP_DIR": str(tmp),
        "COMPOSE_FILE": COMPOSE,
        "FAKE_DOCKER_LOG": str(log),
        "FAKE_DOCKER_CONTAINER": CONTAINER,
        "FAKE_DOCKER_IMAGE_REF": IMAGE_REF,
        "FAKE_DOCKER_IMAGE_ID": IMAGE_ID,
        "FAKE_HEALTH_EXIT": "0",
    }
    env.update(extra)
    return dict(os.environ, **env)


def _run_window(tmp: Path, *, health_exit: int, step: list[str]) -> tuple[int, str, list[str]]:
    """Run the window with a fake docker and curl; return exit code, stderr, docker calls."""
    log = tmp / "docker.log"
    env = _fake_env(tmp, FAKE_HEALTH_EXIT=str(health_exit))
    proc = subprocess.run(
        ["bash", str(HELPER), "--health-tries", "1", "--health-interval", "0", "--", *step],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc.returncode, proc.stderr, calls


def _index(calls: list[str], match: Callable[[str], bool], start: int = 0) -> int:
    for i in range(start, len(calls)):
        if match(calls[i]):
            return i
    raise AssertionError(f"the window never ran this docker call: {calls}")


def _check_failed_step_keeps_the_previous_bot() -> None:
    with tempfile.TemporaryDirectory() as raw:
        rc, stderr, calls = _run_window(Path(raw), health_exit=0, step=["false"])
    assert rc != 0, "the window must pass a failing step's exit code on"
    snapshot_at = _index(calls, lambda c: c == f"tag {IMAGE_ID} {SNAPSHOT_REF}")
    stop_at = _index(calls, lambda c: c == f"compose -f {COMPOSE} stop bot")
    restore_at = _index(calls, lambda c: c == f"tag {SNAPSHOT_REF} {IMAGE_REF}", start=stop_at)
    up_at = _index(
        calls,
        lambda c: c == f"compose -f {COMPOSE} up -d --no-deps --force-recreate bot",
        start=stop_at,
    )
    assert snapshot_at < stop_at, "the running image must be snapshotted before the bot stops"
    assert restore_at < up_at, "a failed step must recreate the bot on the snapshot image"
    assert "step failed" in stderr, stderr


def _check_successful_step_recreates_the_bot() -> None:
    with tempfile.TemporaryDirectory() as raw:
        rc, stderr, calls = _run_window(Path(raw), health_exit=0, step=["true"])
    assert rc == 0, stderr
    stop_at = _index(calls, lambda c: c == f"compose -f {COMPOSE} stop bot")
    _index(
        calls,
        lambda c: c == f"compose -f {COMPOSE} up -d --no-deps --force-recreate bot",
        start=stop_at,
    )
    rolled_back = [c for c in calls if c == f"tag {SNAPSHOT_REF} {IMAGE_REF}"]
    assert not rolled_back, "a successful step must keep the requested image"


def _check_failing_health_wait_exits_nonzero() -> None:
    with tempfile.TemporaryDirectory() as raw:
        rc, stderr, calls = _run_window(Path(raw), health_exit=1, step=["true"])
    assert rc != 0, "a bot that never becomes healthy must fail the window"
    assert "never became healthy" in stderr, stderr
    recreated = [c for c in calls if c == f"compose -f {COMPOSE} up -d --no-deps --force-recreate bot"]
    assert recreated, "the window must try to bring the bot back before it fails"


def _check_one_row_count_source() -> None:
    counts = ROW_COUNTS.read_text(encoding="utf-8")
    assert "information_schema.tables" in counts, "the row-count table list must come from the catalog"
    for name in ("pg-upgrade-to-18.sh", "pg-upgrade-to-19.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "scripts/pg-row-counts.sh" in text, f"{name} must use the one row-count source"
        assert "stop bot" not in text, f"{name} must not stop the bot itself"
        assert "UNION ALL SELECT '" not in text, f"{name} still holds a hand-copied table list"


def _run_upgrade_check(tmp: Path, script: str, db_image: str, compose_image: str) -> int:
    """Run one upgrade script's --check-needed against a fake docker."""
    log = tmp / "docker.log"
    env = _fake_env(tmp, FAKE_DOCKER_IMAGE_REF=db_image)
    (tmp / COMPOSE).write_text(f"services:\n  db:\n    image: {compose_image}\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / script), "--check-needed"],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert log.exists() or proc.returncode == 1, proc.stderr
    return proc.returncode


def _check_noop_upgrade_asks_for_no_window() -> None:
    # A database that already runs the compose major reports "nothing to do", so
    # the deploy opens no window and a steady-state deploy keeps one restart.
    with tempfile.TemporaryDirectory() as raw:
        assert _run_upgrade_check(Path(raw), "pg-upgrade-to-19.sh", "postgres:19-alpine", "postgres:19-alpine") == 1
    with tempfile.TemporaryDirectory() as raw:
        assert _run_upgrade_check(Path(raw), "pg-upgrade-to-18.sh", "postgres:19-alpine", "postgres:19-alpine") == 1
    # A pending upgrade asks for the window.
    with tempfile.TemporaryDirectory() as raw:
        assert _run_upgrade_check(Path(raw), "pg-upgrade-to-19.sh", "postgres:18-alpine", "postgres:19-alpine") == 0
    with tempfile.TemporaryDirectory() as raw:
        assert _run_upgrade_check(Path(raw), "pg-upgrade-to-18.sh", "postgres:17-alpine", "postgres:18-alpine") == 0


def _check_one_health_wait() -> None:
    wait = ROOT / "scripts" / "wait-bot-health.sh"
    assert wait.is_file(), "the health wait must live in one script"
    assert "/health" in wait.read_text(encoding="utf-8"), "the health wait must poll /health"
    helper = (ROOT / "scripts" / "bot-maintenance.sh").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "vps-deploy.sh").read_text(encoding="utf-8")
    for rel, text in (("scripts/bot-maintenance.sh", helper), ("scripts/vps-deploy.sh", deploy)):
        assert "scripts/wait-bot-health.sh" in text, f"{rel} must call the one health wait"
    assert "8080/health" not in deploy, "vps-deploy.sh must not poll /health on its own"


def _check_every_restart_path_uses_the_window() -> None:
    for rel in (
        "scripts/vps-deploy.sh",
        ".github/workflows/ops-sync-telegram-token.yml",
        ".github/workflows/ops-freeze-crypto-keys.yml",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "scripts/bot-maintenance.sh" in text, f"{rel} must restart the bot through the window"
    deploy = (ROOT / "scripts" / "vps-deploy.sh").read_text(encoding="utf-8")
    assert "bash scripts/bot-maintenance.sh -- bash" in deploy, "each pg upgrade must run inside the window"
    assert "--check-needed" in deploy, "the deploy must skip the window when no upgrade is pending"
    for name in ("pg-upgrade-to-18.sh", "pg-upgrade-to-19.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "--check-needed" in text, f"{name} must report whether an upgrade is pending"


def run() -> None:
    for check in (
        _check_failed_step_keeps_the_previous_bot,
        _check_successful_step_recreates_the_bot,
        _check_failing_health_wait_exits_nonzero,
        _check_one_row_count_source,
        _check_one_health_wait,
        _check_noop_upgrade_asks_for_no_window,
        _check_every_restart_path_uses_the_window,
    ):
        check()
    print("  bot maintenance window OK")
