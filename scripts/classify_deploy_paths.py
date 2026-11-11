#!/usr/bin/env python3
"""Classify git path lists for VPS deploy: site-only vs full bot recreate.

Site-only paths are static files the running bot already re-reads from disk
on each request — they can be docker-cp'd without rebuilding the image.
"""

from __future__ import annotations

import sys

SITE_PREFIXES = (
    "webapp/site/",
    "webapp/guide/",
    "assets/",
)

IGNORABLE_PREFIXES = (
    "docs/",
    ".cursor/",
    "self_check/",
    ".github/",
    "tmp/",
    "repo-tmp/",
    "agent-transcripts/",
)

IGNORABLE_EXACT = frozenset(
    {
        ".gitignore",
        ".dockerignore",
        ".gitattributes",
        "LICENSE",
        "LICENSE.txt",
    }
)


def is_ignorable(path: str) -> bool:
    p = (path or "").replace("\\", "/").lstrip("./")
    if not p or p in IGNORABLE_EXACT:
        return True
    if p.endswith(".md"):
        return True
    return any(p.startswith(pref) for pref in IGNORABLE_PREFIXES)


def is_site_path(path: str) -> bool:
    p = (path or "").replace("\\", "/").lstrip("./")
    return any(p.startswith(pref) for pref in SITE_PREFIXES)


def classify_deploy_paths(paths: list[str]) -> str:
    """Return 'noop' | 'site' | 'full' for a list of changed repo paths."""
    meaningful = [p for p in paths if p and not is_ignorable(p)]
    if not meaningful:
        return "noop"
    if all(is_site_path(p) for p in meaningful):
        return "site"
    return "full"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    print(classify_deploy_paths(args))
    return 0


if __name__ == "__main__":
    # Runnable check: smallest thing that fails if the classifier breaks.
    assert classify_deploy_paths([]) == "noop"
    assert classify_deploy_paths(["README.md", "docs/todo.md"]) == "noop"
    assert (
        classify_deploy_paths(
            ["webapp/site/css/site.css", "webapp/site/js/i18n.js"]
        )
        == "site"
    )
    assert (
        classify_deploy_paths(["assets/gallery/ph-gallery-01-main-menu.png"])
        == "site"
    )
    assert classify_deploy_paths(["webapp/guide/ru.html"]) == "site"
    assert (
        classify_deploy_paths(
            ["webapp/site/index.html", "README.md", "docs/x.md"]
        )
        == "site"
    )
    assert classify_deploy_paths(["web_site.py"]) == "full"
    assert classify_deploy_paths(["health.py", "webapp/site/index.html"]) == "full"
    assert classify_deploy_paths(["scripts/vps-deploy.sh"]) == "full"
    raise SystemExit(main())
