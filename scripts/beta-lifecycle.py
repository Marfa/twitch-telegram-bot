#!/usr/bin/env python3
"""Promote beta features to GA: merge ready PRs and update beta/manifest.json.

Run locally or from .github/workflows/beta-lifecycle.yml (needs gh + GITHUB_TOKEN).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "beta" / "manifest.json"
READY_LABEL = "beta/ready-to-merge"
MIN_PR_AGE_DAYS = 7

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    logger.debug("run: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def _gh_json(args: list[str]) -> object | None:
    proc = _run(["gh", *args], check=False)
    if proc.returncode != 0:
        logger.warning("gh failed: %s %s", proc.stderr.strip(), args)
        return None
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        return None


def _pr_age_days(created_at: str) -> float:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - created.astimezone(timezone.utc)
    return delta.total_seconds() / 86400.0


def _open_issue_count(label: str) -> int:
    data = _gh_json(
        [
            "api",
            "repos/{owner}/{repo}/issues".format(
                owner=os.environ.get("GITHUB_REPOSITORY", "Marfa/twitch-telegram-bot").split("/")[0],
                repo=os.environ.get("GITHUB_REPOSITORY", "Marfa/twitch-telegram-bot").split("/")[1],
            ),
            "-f",
            f"labels={label}",
            "-f",
            "state=open",
            "-f",
            "per_page=1",
        ]
    )
    if not isinstance(data, list):
        return 0
    # gh api issues endpoint returns list; use search for count if needed
    search = _gh_json(
        [
            "api",
            "search/issues",
            "-f",
            f"q=repo:{os.environ.get('GITHUB_REPOSITORY', 'Marfa/twitch-telegram-bot')} "
            f"is:issue is:open label:{label}",
        ]
    )
    if isinstance(search, dict):
        return int(search.get("total_count") or 0)
    return len(data)


def _find_open_pr(branch: str) -> dict[str, object] | None:
    repo = os.environ.get("GITHUB_REPOSITORY", "Marfa/twitch-telegram-bot")
    data = _gh_json(
        [
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "number,title,createdAt,labels,statusCheckRollup,headRefName",
            "--limit",
            "5",
        ]
    )
    if not isinstance(data, list) or not data:
        return None
    for pr in data:
        if str(pr.get("headRefName") or "") == branch:
            return pr
    return data[0] if data else None


def _pr_has_ready_label(pr: dict[str, object]) -> bool:
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return False
    for item in labels:
        if isinstance(item, dict) and item.get("name") == READY_LABEL:
            return True
    return False


def _pr_ci_green(pr: dict[str, object]) -> bool:
    rollup = pr.get("statusCheckRollup")
    if not isinstance(rollup, list) or not rollup:
        return True
    for check in rollup:
        if not isinstance(check, dict):
            continue
        state = str(check.get("state") or check.get("conclusion") or "").upper()
        if state in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED"):
            return False
    return True


def _merge_pr(number: int) -> bool:
    proc = _run(
        ["gh", "pr", "merge", str(number), "--squash", "--delete-branch"],
        check=False,
    )
    if proc.returncode != 0:
        logger.error("merge failed #%s: %s", number, proc.stderr.strip())
        return False
    logger.info("merged PR #%s", number)
    return True


def main() -> int:
    if not MANIFEST.is_file():
        logger.error("manifest missing: %s", MANIFEST)
        return 1
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    features = raw.get("features")
    if not isinstance(features, list):
        logger.error("invalid manifest")
        return 1

    changed = False
    for entry in features:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("stage") or "").lower() != "beta":
            continue
        fid = str(entry.get("id") or "")
        branch = str(entry.get("branch") or f"feat/{fid}")
        issue_label = str(entry.get("issue_label") or f"beta/{fid}")
        pr = _find_open_pr(branch)
        if pr is None:
            logger.info("skip %s: no open PR for %s", fid, branch)
            continue
        if not _pr_has_ready_label(pr):
            logger.info("skip %s: PR #%s lacks %s", fid, pr.get("number"), READY_LABEL)
            continue
        created = str(pr.get("createdAt") or "")
        if created and _pr_age_days(created) < MIN_PR_AGE_DAYS:
            logger.info("skip %s: PR younger than %s days", fid, MIN_PR_AGE_DAYS)
            continue
        if _open_issue_count(issue_label) > 0:
            logger.info("skip %s: open issues with label %s", fid, issue_label)
            continue
        if not _pr_ci_green(pr):
            logger.info("skip %s: CI not green", fid)
            continue
        number = int(pr.get("number") or 0)
        if number <= 0:
            continue
        if not _merge_pr(number):
            continue
        entry["stage"] = "ga"
        changed = True
        logger.info("promoted %s to ga", fid)

    if changed:
        MANIFEST.write_text(
            json.dumps({"features": features}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("manifest updated")
    else:
        logger.info("no lifecycle changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
