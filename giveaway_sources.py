"""GamerPower + IsThereAnyDeal giveaways: fetch, normalize, dedupe."""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# Load .env into os.environ without importing config (avoids hard fail on placeholders).
_env_path = Path(".env")
if _env_path.is_file():
    for _raw in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _raw.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        _k, _v = _k.strip(), _v.strip()
        if (_v.startswith('"') and _v.endswith('"')) or (
            _v.startswith("'") and _v.endswith("'")
        ):
            _v = _v[1:-1]
        os.environ.setdefault(_k, _v)

_GP_BASE = "https://www.gamerpower.com/api"
_ITAD_BASE = "https://api.isthereanydeal.com"
_CACHE_TTL_SEC = 20 * 60
_SESSION = requests.Session()
_SESSION.headers.update(
    {"User-Agent": "twitch-telegram-bot/giveaways (+https://github.com/Marfa/twitch-telegram-bot)"}
)

# Canonical store ids → display key (i18n giveaway_store_<id>)
STORES: list[tuple[str, tuple[str, ...]]] = [
    ("steam", ("steam",)),
    ("epic", ("epic", "epic games store", "epic-games-store", "epic games")),
    ("gog", ("gog",)),
    ("itch", ("itch.io", "itchio", "itch")),
    ("ubisoft", ("ubisoft", "ubisoft connect", "uplay")),
    ("indiegala", ("indiegala",)),
    ("humble", ("humble", "humble bundle", "humble store")),
    ("fanatical", ("fanatical",)),
    ("playstation_store", ("playstation store", "ps store", "playstation")),
    ("xbox_store", ("xbox store", "microsoft store", "xbox")),
    ("nintendo_eshop", ("nintendo eshop", "eshop", "nintendo")),
    ("alienware", ("alienware", "alienware arena")),
    ("other", ("other", "drm-free", "drm free")),
]

# Canonical platform ids → aliases (GP tags + ITAD names)
PLATFORMS: list[tuple[str, tuple[str, ...]]] = [
    ("pc", ("pc", "windows", "win")),
    ("mac", ("mac", "macos", "osx")),
    ("linux", ("linux",)),
    ("ps4", ("ps4", "playstation 4")),
    ("ps5", ("ps5", "playstation 5")),
    ("xbox_one", ("xbox one", "xbox-one")),
    ("xbox_series", ("xbox series", "xbox series x/s", "xbox-series-xs", "xbox series x", "xbox series s")),
    ("switch", ("switch", "nintendo switch")),
    ("android", ("android",)),
    ("ios", ("ios", "iphone", "ipad")),
    ("vr", ("vr",)),
    ("drm_free", ("drm-free", "drm free", "drmfree")),
]

STORE_IDS = [s[0] for s in STORES]
PLATFORM_IDS = [p[0] for p in PLATFORMS]

_STORE_ALIAS: dict[str, str] = {}
for _sid, _aliases in STORES:
    for a in _aliases:
        _STORE_ALIAS[a] = _sid

_PLATFORM_ALIAS: dict[str, str] = {}
for _pid, _aliases in PLATFORMS:
    for a in _aliases:
        _PLATFORM_ALIAS[a] = _pid

# ITAD shop id → canonical store (common shops; rest map by name)
_ITAD_SHOP_ID_STORE: dict[int, str] = {
    61: "steam",
    16: "epic",
    35: "gog",
    37: "humble",
    6: "fanatical",
    25: "itch",
    13: "ubisoft",
}


@dataclass(frozen=True)
class GiveawayOffer:
    source: str  # gamerpower | itad
    external_id: str
    title: str
    store_id: str
    platform_ids: tuple[str, ...]
    claim_url: str
    start_at: str  # ISO or display
    end_at: str
    description: str
    image_url: str
    dedupe_key: str


@dataclass
class _CatalogCache:
    offers: list[GiveawayOffer] = field(default_factory=list)
    fetched_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


_cache = _CatalogCache()


def _norm_token(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def canonicalize_store(raw: str) -> str:
    t = _norm_token(raw)
    if not t:
        return "other"
    if t in _STORE_ALIAS:
        return _STORE_ALIAS[t]
    for alias, sid in _STORE_ALIAS.items():
        if alias in t or t in alias:
            return sid
    return "other"


def canonicalize_platforms(raw_parts: list[str]) -> tuple[str, ...]:
    found: list[str] = []
    for part in raw_parts:
        t = _norm_token(part)
        if not t:
            continue
        pid = _PLATFORM_ALIAS.get(t)
        if pid is None:
            for alias, cand in _PLATFORM_ALIAS.items():
                if alias in t or t in alias:
                    pid = cand
                    break
        if pid and pid not in found:
            found.append(pid)
    return tuple(found)


def normalize_title(title: str) -> str:
    s = (title or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(
        r"\b(steam|epic|gog|key|giveaway|free|pc|ps4|ps5|xbox|switch|"
        r"drm[- ]?free|on|via|store|games)\b",
        " ",
        s,
    )
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _dedupe_key(store_id: str, title: str) -> str:
    return f"{store_id}:{normalize_title(title)}"


def _parse_gp_platforms(platforms: str) -> tuple[str, str, tuple[str, ...]]:
    """Return (store_id, claim_hint_unused, platform_ids) from GP platforms string."""
    parts = [p.strip() for p in (platforms or "").split(",") if p.strip()]
    store_id = "other"
    plats: list[str] = []
    for p in parts:
        cs = canonicalize_store(p)
        if cs != "other" and store_id == "other":
            store_id = cs
        for pid in canonicalize_platforms([p]):
            if pid not in plats:
                plats.append(pid)
    if not plats and store_id in ("steam", "epic", "gog", "itch", "ubisoft", "indiegala"):
        plats = ["pc"]
    if store_id == "other" and any(_norm_token(p) == "pc" for p in parts):
        store_id = "other"
    return store_id, "", tuple(plats)


def _fetch_gamerpower() -> list[GiveawayOffer]:
    url = f"{_GP_BASE}/giveaways?type=game"
    try:
        r = _SESSION.get(url, timeout=25)
        if r.status_code == 201:
            return []
        r.raise_for_status()
        data = r.json()
    except Exception:
        logger.exception("gamerpower fetch failed")
        return []
    if not isinstance(data, list):
        return []
    out: list[GiveawayOffer] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("type") or "").lower() not in ("game",):
            continue
        if str(raw.get("status") or "Active").lower() not in ("active", ""):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        store_id, _, plats = _parse_gp_platforms(str(raw.get("platforms") or ""))
        eid = str(raw.get("id") or "").strip()
        if not eid:
            continue
        claim = str(
            raw.get("open_giveaway_url")
            or raw.get("open_giveaway")
            or raw.get("gamerpower_url")
            or ""
        ).strip()
        out.append(
            GiveawayOffer(
                source="gamerpower",
                external_id=eid,
                title=title,
                store_id=store_id,
                platform_ids=plats or ("pc",),
                claim_url=claim,
                start_at=str(raw.get("published_date") or "").strip(),
                end_at=str(raw.get("end_date") or "").strip(),
                description=str(raw.get("description") or "").strip(),
                image_url=str(raw.get("image") or raw.get("thumbnail") or "").strip(),
                dedupe_key=_dedupe_key(store_id, title),
            )
        )
    return out


def _itad_api_key() -> str:
    # Prefer env directly so a broken optional config import cannot hide the key.
    key = (os.getenv("ISTHEREANYDEAL_API_KEY") or "").strip()
    if key:
        return key
    try:
        from config import ISTHEREANYDEAL_API_KEY

        return (ISTHEREANYDEAL_API_KEY or "").strip()
    except Exception:
        return ""


def _fetch_itad() -> list[GiveawayOffer]:
    key = _itad_api_key()
    if not key:
        return []
    out: list[GiveawayOffer] = []
    offset = 0
    limit = 50
    while offset < 500:
        url = f"{_ITAD_BASE}/giveaways/v1"
        try:
            r = _SESSION.get(
                url,
                params={
                    "offset": offset,
                    "limit": limit,
                    "expired": "false",
                    "mature": "false",
                    "sort": "-publish",
                },
                headers={"ITAD-API-Key": key},
                timeout=25,
            )
            if r.status_code == 429:
                retry = int(r.headers.get("Retry-After") or "5")
                time.sleep(min(retry, 30))
                continue
            if r.status_code in (401, 403):
                logger.warning("itad giveaways auth failed status=%s", r.status_code)
                return []
            r.raise_for_status()
            data = r.json()
        except Exception:
            logger.exception("itad giveaways fetch failed offset=%s", offset)
            break
        if not isinstance(data, list) or not data:
            break
        for raw in data:
            if not isinstance(raw, dict):
                continue
            games = raw.get("games") or []
            if not isinstance(games, list):
                continue
            shop = raw.get("shop") or {}
            shop_id = int(shop.get("id") or 0) if isinstance(shop, dict) else 0
            shop_name = str(shop.get("name") or "") if isinstance(shop, dict) else ""
            store_id = _ITAD_SHOP_ID_STORE.get(shop_id) or canonicalize_store(shop_name)
            claim = str(raw.get("url") or "").strip()
            start_at = str(raw.get("publish") or "").strip()
            end_at = str(raw.get("expiry") or "").strip() or "N/A"
            gid = str(raw.get("id") or "").strip()
            for game in games:
                if not isinstance(game, dict):
                    continue
                if str(game.get("type") or "").lower() != "game":
                    continue
                title = str(game.get("title") or "").strip()
                if not title:
                    continue
                plats_raw = game.get("platforms") or []
                plat_names = []
                if isinstance(plats_raw, list):
                    for p in plats_raw:
                        if isinstance(p, dict):
                            plat_names.append(str(p.get("name") or ""))
                        else:
                            plat_names.append(str(p))
                plats = canonicalize_platforms(plat_names) or ("pc",)
                assets = game.get("assets") or {}
                image = ""
                if isinstance(assets, dict):
                    image = str(
                        assets.get("boxart")
                        or assets.get("banner400")
                        or assets.get("banner300")
                        or ""
                    ).strip()
                ext = f"{gid}:{game.get('id') or title}"
                out.append(
                    GiveawayOffer(
                        source="itad",
                        external_id=ext,
                        title=title,
                        store_id=store_id,
                        platform_ids=plats,
                        claim_url=claim,
                        start_at=start_at,
                        end_at=end_at,
                        description=str(raw.get("note") or "").strip(),
                        image_url=image,
                        dedupe_key=_dedupe_key(store_id, title),
                    )
                )
        if len(data) < limit:
            break
        offset += limit
    return out


def _merge_dedupe(gp: list[GiveawayOffer], itad: list[GiveawayOffer]) -> list[GiveawayOffer]:
    by_key: dict[str, GiveawayOffer] = {}
    # Prefer ITAD when both match
    for offer in gp:
        by_key[offer.dedupe_key] = offer
    for offer in itad:
        by_key[offer.dedupe_key] = offer
    return sorted(by_key.values(), key=lambda o: (o.start_at or "", o.title), reverse=True)


def fetch_active_giveaways(*, force: bool = False) -> list[GiveawayOffer]:
    """Shared cached catalog (GP always; ITAD if key set)."""
    now = time.monotonic()
    with _cache.lock:
        if (
            not force
            and _cache.offers
            and (now - _cache.fetched_at) < _CACHE_TTL_SEC
        ):
            return list(_cache.offers)
    gp = _fetch_gamerpower()
    itad = _fetch_itad()
    merged = _merge_dedupe(gp, itad)
    with _cache.lock:
        _cache.offers = merged
        _cache.fetched_at = time.monotonic()
    return list(merged)


def filter_giveaways(
    offers: list[GiveawayOffer],
    *,
    stores: set[str],
    platforms: set[str],
) -> list[GiveawayOffer]:
    if not stores or not platforms:
        return []
    out: list[GiveawayOffer] = []
    for o in offers:
        if o.store_id not in stores:
            continue
        if not (set(o.platform_ids) & platforms):
            continue
        out.append(o)
    return out


def attribution_html(*, used_gp: bool, used_itad: bool) -> str:
    parts: list[str] = []
    if used_gp:
        parts.append(
            '<a href="https://www.gamerpower.com">GamerPower.com</a>'
        )
    if used_itad:
        parts.append(
            '<a href="https://isthereanydeal.com">IsThereAnyDeal.com</a>'
        )
    if not parts:
        return ""
    return " · ".join(parts)


def claim_url_or_search(offer: GiveawayOffer) -> str:
    if offer.claim_url:
        return offer.claim_url
    q = quote(offer.title)
    return f"https://www.gamerpower.com/giveaways?q={q}"


def demo_self_check() -> None:
    """ponytail: fails if store/platform maps or dedupe regress."""
    assert canonicalize_store("Epic Games Store") == "epic"
    assert canonicalize_store("STEAM") == "steam"
    assert "pc" in canonicalize_platforms(["PC", "Steam"])
    assert "ps5" in canonicalize_platforms(["Playstation 5"])
    a = GiveawayOffer(
        source="gamerpower",
        external_id="1",
        title="Foo (Steam) Giveaway",
        store_id="steam",
        platform_ids=("pc",),
        claim_url="https://example.com/a",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Foo (Steam) Giveaway"),
    )
    b = GiveawayOffer(
        source="itad",
        external_id="2",
        title="Foo",
        store_id="steam",
        platform_ids=("pc", "mac"),
        claim_url="https://example.com/b",
        start_at="",
        end_at="",
        description="",
        image_url="",
        dedupe_key=_dedupe_key("steam", "Foo"),
    )
    merged = _merge_dedupe([a], [b])
    assert len(merged) == 1 and merged[0].source == "itad"
    filtered = filter_giveaways(merged, stores={"steam"}, platforms={"mac"})
    assert len(filtered) == 1
    assert filter_giveaways(merged, stores={"steam"}, platforms={"switch"}) == []
    assert filter_giveaways(merged, stores=set(), platforms={"pc"}) == []


if __name__ == "__main__":
    demo_self_check()
    print("giveaway_sources: ok")
