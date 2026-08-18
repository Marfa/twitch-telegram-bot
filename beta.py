"""Beta feature catalog (manifest.json) + enrollment checks."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

if TYPE_CHECKING:
    from db import Database

logger = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).resolve().parent / "beta" / "manifest.json"
_GITHUB_REPO = "Marfa/twitch-telegram-bot"
_ISSUE_TEMPLATE = "beta-bug.yml"
_ACTIVE_STAGES = frozenset({"alpha", "beta"})
_VISIBLE_STAGES = frozenset({"alpha", "beta"})


@dataclass(frozen=True)
class BetaFeature:
    id: str
    branch: str
    title_key: str
    description_key: str
    issue_label: str
    stage: str
    premium_feature_id: str | None = None
    flag_key: str | None = None
    created_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> BetaFeature | None:
        fid = str(raw.get("id") or "").strip()
        if not fid:
            return None
        branch = str(raw.get("branch") or f"feat/{fid}").strip()
        title_key = str(raw.get("title_key") or f"beta_feat_{fid}").strip()
        description_key = str(
            raw.get("description_key") or f"beta_feat_{fid}_desc"
        ).strip()
        issue_label = str(raw.get("issue_label") or f"beta/{fid}").strip()
        stage = str(raw.get("stage") or "draft").strip().lower()
        premium_raw = raw.get("premium_feature_id")
        premium_feature_id = (
            str(premium_raw).strip() if premium_raw not in (None, "", "null") else None
        )
        flag_raw = raw.get("flag_key")
        flag_key = str(flag_raw).strip() if flag_raw not in (None, "", "null") else None
        created_raw = raw.get("created_at")
        created_at = str(created_raw).strip() if created_raw else None
        return cls(
            id=fid,
            branch=branch,
            title_key=title_key,
            description_key=description_key,
            issue_label=issue_label,
            stage=stage,
            premium_feature_id=premium_feature_id,
            flag_key=flag_key,
            created_at=created_at,
        )


_features: list[BetaFeature] = []
_features_by_id: dict[str, BetaFeature] = {}


def manifest_path() -> Path:
    return _MANIFEST_PATH


def load_manifest(path: Path | None = None) -> list[BetaFeature]:
    """Load beta/manifest.json. Safe to call repeatedly (reloads cache)."""
    global _features, _features_by_id
    src = path or _MANIFEST_PATH
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Beta manifest missing: %s", src)
        _features = []
        _features_by_id = {}
        return []
    except (OSError, json.JSONDecodeError) as exc:
        logger.exception("Beta manifest load failed: %s", exc)
        return list(_features)

    items = raw.get("features") if isinstance(raw, dict) else raw
    parsed: list[BetaFeature] = []
    if isinstance(items, list):
        for entry in items:
            if not isinstance(entry, dict):
                continue
            feat = BetaFeature.from_dict(entry)
            if feat is not None:
                parsed.append(feat)
    _features = parsed
    _features_by_id = {f.id: f for f in parsed}
    return list(_features)


def list_features(*, stages: frozenset[str] | None = None) -> list[BetaFeature]:
    if not _features_by_id and _MANIFEST_PATH.exists():
        load_manifest()
    wanted = stages or _VISIBLE_STAGES
    return [f for f in _features if f.stage in wanted]


def get_feature(feature_id: str) -> BetaFeature | None:
    if not _features_by_id and _MANIFEST_PATH.exists():
        load_manifest()
    return _features_by_id.get(feature_id)


def is_admin(user_id: int) -> bool:
    from config import ADMIN_USER_IDS

    return user_id in ADMIN_USER_IDS


def _user_enrolled(db: Database, user_id: int, feature_id: str) -> bool:
    return db.is_beta_enrolled(user_id, feature_id)


def is_enabled(db: Database, user_id: int, feature_id: str) -> bool:
    """Runtime gate for beta-only code paths."""
    from demo_mode import is_active

    if is_active(user_id):
        return False
    feat = get_feature(feature_id)
    if feat is None or feat.stage not in _ACTIVE_STAGES:
        return False
    if is_admin(user_id):
        return True
    return _user_enrolled(db, user_id, feature_id)


def is_enrolled(db: Database, user_id: int, feature_id: str) -> bool:
    """UI checkbox state — False in demo so the toggle matches reality."""
    from demo_mode import is_active

    if is_active(user_id):
        return False
    feat = get_feature(feature_id)
    if feat is None or feat.stage not in _VISIBLE_STAGES:
        return False
    if is_admin(user_id):
        return True
    return _user_enrolled(db, user_id, feature_id)


def enrollment_counts(db: Database, user_id: int) -> tuple[int, int]:
    """(joined visible betas, total visible betas) for the Settings button."""
    from demo_mode import is_active

    features = list_features()
    total = len(features)
    if is_active(user_id):
        return 0, total
    joined = sum(1 for feat in features if is_enrolled(db, user_id, feat.id))
    return joined, total


def user_ids_with_active_enrollment(db: Database) -> list[int]:
    """Users opted into at least one currently alpha/beta feature."""
    from demo_mode import is_active

    feature_ids = [feat.id for feat in list_features(stages=_ACTIVE_STAGES)]
    if not feature_ids:
        return []
    return [
        uid
        for uid in db.list_beta_enrolled_user_ids(feature_ids)
        if not is_active(uid)
    ]


def grants_premium_feature(db: Database, user_id: int, premium_feature_id: str) -> bool:
    """Beta bypass for prem.has_feature_sync (alpha/beta stages only)."""
    from demo_mode import is_active

    if is_active(user_id):
        return False
    if not premium_feature_id:
        return False
    admin = is_admin(user_id)
    for feat in list_features(stages=_ACTIVE_STAGES):
        if feat.premium_feature_id != premium_feature_id:
            continue
        if admin or _user_enrolled(db, user_id, feat.id):
            return True
    return False


def issue_url(feature: BetaFeature, *, user_id: int | None = None) -> str:
    params: dict[str, str] = {
        "template": _ISSUE_TEMPLATE,
        "labels": feature.issue_label,
        "title": f"[Beta] {feature.id}",
    }
    if user_id is not None:
        params["title"] = f"[Beta] {feature.id} (user {user_id})"
    return (
        f"https://github.com/{_GITHUB_REPO}/issues/new?"
        + urlencode(params)
    )


def dump_manifest(features: list[BetaFeature], path: Path | None = None) -> None:
    """Serialize features back to manifest (used by beta-lifecycle script)."""
    dest = path or _MANIFEST_PATH
    payload = {
        "features": [
            {
                "id": f.id,
                "branch": f.branch,
                "title_key": f.title_key,
                "description_key": f.description_key,
                "issue_label": f.issue_label,
                "stage": f.stage,
                **({"premium_feature_id": f.premium_feature_id} if f.premium_feature_id else {}),
                **({"flag_key": f.flag_key} if f.flag_key else {}),
                **({"created_at": f.created_at} if f.created_at else {}),
            }
            for f in features
        ]
    }
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    load_manifest(dest)


def _self_check() -> None:
    import tempfile

    from db import SqliteDatabase

    sample = {
        "features": [
            {
                "id": "demo_feat",
                "branch": "feat/demo-feat",
                "title_key": "beta_feat_demo_feat",
                "description_key": "beta_feat_demo_feat_desc",
                "issue_label": "beta/demo-feat",
                "stage": "beta",
                "premium_feature_id": "alert_history",
                "created_at": "2026-01-01",
            }
        ]
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifest.json"
        path.write_text(json.dumps(sample), encoding="utf-8")
        load_manifest(path)
        assert len(list_features()) == 1
        feat = get_feature("demo_feat")
        assert feat is not None
        assert "demo-feat" in issue_url(feat, user_id=42)
        db = SqliteDatabase(Path(tmp) / "t.db")
        db.upsert_user(1)
        assert not is_enabled(db, 1, "demo_feat")
        db.set_beta_enrollment(1, "demo_feat", True)
        assert is_enabled(db, 1, "demo_feat")
        assert enrollment_counts(db, 1) == (1, 1)
        assert grants_premium_feature(db, 1, "alert_history")
        assert not grants_premium_feature(db, 1, "twitch_sync")
        assert user_ids_with_active_enrollment(db) == [1]
        db.set_beta_enrollment(1, "demo_feat", False)
        assert user_ids_with_active_enrollment(db) == []
        db.set_beta_enrollment(1, "demo_feat", True)
        db.set_beta_enrollment(2, "retired_feat", True)
        assert user_ids_with_active_enrollment(db) == [1]
        db.upsert_user(3)
        db.set_beta_enrollment(3, "demo_feat", True)
        db.set_bot_blocked(3, True)
        assert user_ids_with_active_enrollment(db) == [1]
        import config

        old_admins = config.ADMIN_USER_IDS
        config.ADMIN_USER_IDS = frozenset({777})
        try:
            assert is_enabled(db, 777, "demo_feat")
            assert is_enrolled(db, 777, "demo_feat")
        finally:
            config.ADMIN_USER_IDS = old_admins
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["features"][0]["stage"] = "ga"
        path.write_text(json.dumps(raw), encoding="utf-8")
        load_manifest(path)
        assert user_ids_with_active_enrollment(db) == []
        load_manifest(_MANIFEST_PATH)


if __name__ == "__main__":
    _self_check()
    print("beta self-check ok")
