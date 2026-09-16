"""Normalize free-text game/category search queries (punctuation-tolerant)."""

from __future__ import annotations

import re
from typing import Any

# Letters/digits across Latin + Cyrillic; punctuation/colons become separators.
_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")

# Common title acronyms → full tokens (local IGDB name search has no alt-names dump).
_ACRONYMS: dict[str, tuple[str, ...]] = {
    "gta": ("grand", "theft", "auto"),
}

# Sequel numerals: roman ↔ arabic (whole-token only).
_NUM_EQUIV: dict[str, str] = {}
for _a, _r in (
    ("2", "ii"),
    ("3", "iii"),
    ("4", "iv"),
    ("5", "v"),
    ("6", "vi"),
    ("7", "vii"),
    ("8", "viii"),
    ("9", "ix"),
    ("10", "x"),
    ("11", "xi"),
    ("12", "xii"),
    ("13", "xiii"),
    ("14", "xiv"),
    ("15", "xv"),
    ("16", "xvi"),
    ("17", "xvii"),
    ("18", "xviii"),
    ("19", "xix"),
    ("20", "xx"),
):
    _NUM_EQUIV[_a] = _r
    _NUM_EQUIV[_r] = _a


def search_tokens(query: str) -> list[str]:
    """Split query into alphanumeric tokens (lowercased)."""
    return [m.group(0).casefold() for m in _TOKEN_RE.finditer(query or "")]


def normalize_search_query(query: str) -> str:
    """Collapse punctuation: 'Worms: Galactic Tactics' → 'worms galactic tactics'."""
    return " ".join(search_tokens(query))


def _canon_num_token(token: str) -> str:
    """Map roman sequel tokens to arabic; leave others unchanged."""
    if token.isalpha() and token in _NUM_EQUIV:
        return _NUM_EQUIV[token]
    return token


def expand_search_token_sets(query: str) -> list[list[str]]:
    """OR-able token lists: raw tokens plus acronym / numeral variants."""
    tokens = search_tokens(query)
    if not tokens:
        return []
    out: list[list[str]] = [tokens]
    expanded: list[str] = []
    changed = False
    for t in tokens:
        if t in _ACRONYMS:
            expanded.extend(_ACRONYMS[t])
            changed = True
        else:
            expanded.append(t)
    if changed:
        out.append(expanded)
    # Flip each numeral/roman token once (GTA 6 ↔ GTA VI).
    bases = list(out)
    for base in bases:
        for i, t in enumerate(base):
            alt = _NUM_EQUIV.get(t)
            if not alt:
                continue
            variant = list(base)
            variant[i] = alt
            if variant not in out:
                out.append(variant)
    return out


def name_matches_token_set(name: str, tokens: list[str]) -> bool:
    """True if every query token appears as a whole word (roman≈arabic)."""
    if not tokens:
        return False
    name_set = {_canon_num_token(t) for t in search_tokens(name)}
    return all(_canon_num_token(t) in name_set for t in tokens)


def name_matches_any_token_set(name: str, token_sets: list[list[str]]) -> bool:
    return any(name_matches_token_set(name, ts) for ts in token_sets)


def _tokens_prefix_match(name_toks: list[str], query_toks: list[str]) -> bool:
    if not query_toks or len(name_toks) < len(query_toks):
        return False
    head = [_canon_num_token(t) for t in name_toks[: len(query_toks)]]
    want = [_canon_num_token(t) for t in query_toks]
    return head == want


def rank_igdb_game_hit(
    game: dict[str, Any], *, query: str, token_sets: list[list[str]]
) -> tuple:
    """Sort key: exact → prefix → newer → more ratings → shorter → A–Z."""
    name = str(game.get("name") or "")
    nf = name.casefold()
    qf = (query or "").strip().casefold()
    exact = 0 if nf == qf else 1
    name_toks = search_tokens(name)
    # Prefer titles whose leading words match a query token set (e.g. Control*).
    prefix = 1
    for ts in token_sets:
        if not ts:
            continue
        if _tokens_prefix_match(name_toks, ts):
            prefix = 0
            break
        if len(ts) == 1 and name_toks and _canon_num_token(name_toks[0]) == _canon_num_token(
            ts[0]
        ):
            prefix = 0
            break
    released = int(game.get("first_release_date") or 0)
    rating = int(game.get("total_rating_count") or 0)
    return (
        exact,
        prefix,
        -released,
        -rating,
        len(name),
        nf,
        int(game.get("id") or 0),
    )
