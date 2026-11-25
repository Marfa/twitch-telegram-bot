"""Self-check: one app-token budget governs every Helix caller."""

from __future__ import annotations

import inspect
import time
from typing import Any


def run() -> None:
    from twitch import (
        HelixBudgetExhausted,
        TwitchClient,
        _HelixBudget,
        _install_rate_limit_backoff,
    )

    # The reserve is the go-live poll's share: a user-facing caller stops at
    # limit - reserve, and the priority caller still spends the reserve.
    budget = _HelixBudget(points=10, window_sec=60.0, reserve=4)
    for _ in range(6):
        budget.admit(1)
    try:
        budget.admit(1)
        raise AssertionError("a user-facing caller must shed before the reserve")
    except HelixBudgetExhausted as exc:
        assert 1.0 <= exc.retry_after <= 60.0
    for _ in range(4):
        budget.admit(1, priority=True)
    try:
        budget.admit(1, priority=True)
        raise AssertionError("the priority caller must shed when the window is full")
    except HelixBudgetExhausted:
        pass

    # A 429 returns at once, without a sleep and retry, and sheds every caller
    # until the reset Twitch reports.
    calls: list[tuple[str, str]] = []

    class _DrySession:
        def request(self, method: str, url: str, **kwargs: Any):
            import requests

            calls.append((method, url))
            resp = requests.Response()
            resp.status_code = 429
            resp.url = url
            resp.headers["Ratelimit-Reset"] = str(int(time.time()) + 20)
            resp._content = b"{}"
            return resp

    session = _DrySession()
    governed = _HelixBudget(points=10, window_sec=60.0, reserve=4)
    _install_rate_limit_backoff(
        session,
        budget=governed,
        is_app_token_request=lambda method, url, headers: True,
    )
    first = session.request(
        "GET", "https://api.twitch.tv/helix/streams", headers={"X": "y"}
    )
    assert first.status_code == 429
    assert len(calls) == 1
    try:
        session.request("GET", "https://api.twitch.tv/helix/streams", headers={"X": "y"})
        raise AssertionError("a dry budget must shed before the second network call")
    except HelixBudgetExhausted as exc:
        assert 1.0 <= exc.retry_after <= 25.0
    assert len(calls) == 1

    # Twitch's reset must outlive the sleep-loop 60 s cap: a Retry-After of
    # 120 s keeps the dry period at 120 s so callers shed instead of hitting
    # the same 429 again after a minute.
    long_calls: list[str] = []

    class _LongDrySession:
        def request(self, method: str, url: str, **kwargs: Any):
            import requests

            long_calls.append(url)
            resp = requests.Response()
            resp.status_code = 429
            resp.url = url
            resp.headers["Retry-After"] = "120"
            resp._content = b"{}"
            return resp

    long_budget = _HelixBudget(points=10, window_sec=60.0, reserve=4)
    long_session = _LongDrySession()
    _install_rate_limit_backoff(
        long_session,
        budget=long_budget,
        is_app_token_request=lambda method, url, headers: True,
    )
    assert long_session.request(
        "GET", "https://api.twitch.tv/helix/streams", headers={"X": "y"}
    ).status_code == 429
    try:
        long_session.request(
            "GET", "https://api.twitch.tv/helix/streams", headers={"X": "y"}
        )
        raise AssertionError("a 120 s Twitch reset must keep the budget dry")
    except HelixBudgetExhausted as exc:
        assert 110.0 <= exc.retry_after <= 120.0, exc.retry_after
    assert len(long_calls) == 1

    # The app token's Helix calls are governed; a user-token Helix call and the
    # token and IGDB endpoints keep their own buckets.
    client = TwitchClient()
    client._token = "app-token"
    client._token_expires = time.time() + 3600
    app_headers = {"Authorization": "Bearer app-token"}
    assert client._is_app_token_request(
        "GET", "https://api.twitch.tv/helix/streams", app_headers
    )
    assert not client._is_app_token_request(
        "POST",
        "https://api.twitch.tv/helix/chat/messages",
        {"Authorization": "Bearer user-token"},
    )
    assert not client._is_app_token_request(
        "POST", "https://api.igdb.com/v4/games", app_headers
    )
    assert not client._is_app_token_request(
        "POST", "https://id.twitch.tv/oauth2/token", app_headers
    )
    # A dry window sheds the client's own Helix path before any network call.
    client._budget.drained(30)
    try:
        client.get_users_by_login(["marfapr"])
        raise AssertionError("the client must shed a Helix call on a dry budget")
    except HelixBudgetExhausted as exc:
        assert exc.retry_after > 0

    # Wiring: the go-live poll is the priority consumer, and the Mini App read
    # path answers a shed instead of keeping a private global counter.
    import chat_webapp as cw
    import handlers.notifications as notifications

    assert "priority=True" in inspect.getsource(notifications.check_streams)
    assert not hasattr(cw, "_CHAT_READ_GLOBAL_LIMIT")
    for handler in (cw.api_online, cw.api_resolve, cw.api_send):
        assert "HelixBudgetExhausted" in inspect.getsource(handler)
