#!/usr/bin/env python3
"""Promote beta features to GA by updating beta/manifest.json.

Expected model (matches .cursor/rules/beta-feature-branch.mdc):
  1. Feature PR is already merged into main (code live, gated by enrollment).
  2. Label beta/ready-to-merge marks the start of the 7-day beta window.
  3. This script flips stage beta → ga when the window elapsed and no open issues.

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
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "beta" / "manifest.json"
READY_LABEL = "beta/ready-to-merge"
MIN_MERGED_AGE_DAYS = 7

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


def _repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "Marfa/twitch-telegram-bot")


def _age_days(iso_ts: str) -> float:
    created = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - created.astimezone(timezone.utc)
    return delta.total_seconds() / 86400.0


def _label_names(pr: dict[str, object]) -> set[str]:
    labels = pr.get("labels")
    if not isinstance(labels, list):
        return set()
    out: set[str] = set()
    for item in labels:
        if isinstance(item, dict) and item.get("name"):
            out.add(str(item["name"]))
    return out


def _open_issue_count(label: str) -> int | None:
    """Return open issue count for label, or None if the check failed."""
    q = quote(f"repo:{_repo()} is:issue is:open label:{label}")
    search = _gh_json(["api", f"search/issues?q={q}"])
    if not isinstance(search, dict) or "total_count" not in search:
        return None
    return int(search.get("total_count") or 0)


def _list_merged_ready_prs() -> list[dict[str, object]]:
    data = _gh_json(
        [
            "pr",
            "list",
            "--state",
            "merged",
            "--label",
            READY_LABEL,
            "--json",
            "number,title,createdAt,mergedAt,labels,headRefName",
            "--limit",
            "50",
        ]
    )
    if not isinstance(data, list):
        return []
    return [pr for pr in data if isinstance(pr, dict) and pr.get("mergedAt")]


def _find_merged_ready_pr(
    *,
    branch: str,
    issue_label: str,
    ready_prs: list[dict[str, object]],
) -> dict[str, object] | None:
    """Match by head branch or by feature issue_label on a ready-to-merge PR."""
    candidates: list[dict[str, object]] = []
    for pr in ready_prs:
        names = _label_names(pr)
        if READY_LABEL not in names:
            continue
        head = str(pr.get("headRefName") or "")
        if head == branch or issue_label in names:
            candidates.append(pr)
    if not candidates:
        return None
    candidates.sort(key=lambda pr: str(pr.get("mergedAt") or ""), reverse=True)
    return candidates[0]


def main() -> int:
    if not MANIFEST.is_file():
        logger.error("manifest missing: %s", MANIFEST)
        return 1
    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    features = raw.get("features")
    if not isinstance(features, list):
        logger.error("invalid manifest")
        return 1

    ready_prs = _list_merged_ready_prs()
    changed = False
    for entry in features:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("stage") or "").lower() != "beta":
            continue
        fid = str(entry.get("id") or "")
        branch = str(entry.get("branch") or f"feat/{fid}")
        issue_label = str(entry.get("issue_label") or f"beta/{fid}")
        pr = _find_merged_ready_pr(
            branch=branch, issue_label=issue_label, ready_prs=ready_prs
        )
        if pr is None:
            logger.info(
                "skip %s: no merged PR with %s matching %s / %s",
                fid,
                READY_LABEL,
                branch,
                issue_label,
            )
            continue
        merged_at = str(pr.get("mergedAt") or "")
        age = _age_days(merged_at) if merged_at else 0.0
        if age < MIN_MERGED_AGE_DAYS:
            logger.info(
                "skip %s: PR #%s merged %.1f days ago (< %s)",
                fid,
                pr.get("number"),
                age,
                MIN_MERGED_AGE_DAYS,
            )
            continue
        open_issues = _open_issue_count(issue_label)
        if open_issues is None:
            logger.info("skip %s: could not check open issues for %s", fid, issue_label)
            continue
        if open_issues > 0:
            logger.info(
                "skip %s: %s open issue(s) with label %s",
                fid,
                open_issues,
                issue_label,
            )
            continue
        entry["stage"] = "ga"
        changed = True
        logger.info(
            "promoted %s to ga (PR #%s, merged %.1f days ago)",
            fid,
            pr.get("number"),
            age,
        )

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
