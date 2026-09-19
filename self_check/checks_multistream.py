"""Multistream URL parse / dump helpers."""

from __future__ import annotations

from multistream import (
    MultistreamChannel,
    dump_multistream_channels,
    error_line_looks_like_youtube,
    parse_multistream_channels,
    parse_multistream_url,
)


def check_multistream_parse_urls() -> None:
    gg = parse_multistream_url("https://goodgame.ru/Abver")
    assert gg is not None
    assert gg.platform == "goodgame"
    assert gg.channel_id == "Abver"

    vk = parse_multistream_url("https://live.vkvideo.ru/play_code")
    assert vk is not None
    assert vk.platform == "vkplay"
    assert vk.channel_id == "play_code"

    yt = parse_multistream_url("https://www.youtube.com/channel/UCXuqSBlHAE6Xw-yeJA0Tunw")
    assert yt is not None
    assert yt.platform == "youtube"
    assert yt.channel_id.startswith("UC")

    assert parse_multistream_url("https://example.com/nope") is None
    assert parse_multistream_url("https://youtu.be/dQw4w9WgXcQ") is None

    assert error_line_looks_like_youtube("https://www.youtube.com/@someone")
    assert error_line_looks_like_youtube("youtu.be/dQw4w9WgXcQ")
    assert not error_line_looks_like_youtube("https://evil.com/youtube.com/phish")
    assert not error_line_looks_like_youtube("https://goodgame.ru/x")


def check_multistream_dump_roundtrip() -> None:
    raw = dump_multistream_channels(
        [
            MultistreamChannel(
                platform="goodgame",
                url="https://goodgame.ru/Abver",
                channel_id="Abver",
                label="Abver",
            ),
            MultistreamChannel(
                platform="vkplay",
                url="https://live.vkvideo.ru/play_code",
                channel_id="play_code",
                label="play_code",
            ),
        ]
    )
    parsed = parse_multistream_channels(raw)
    assert len(parsed) == 2
    assert parsed[0].platform == "goodgame"
    assert parsed[1].channel_id == "play_code"
    assert parse_multistream_channels("") == []
    assert parse_multistream_channels("[]") == []


def check_multistream_status_placeholders() -> None:
    from multistream import STATUS_OFFLINE, STATUS_ONLINE, status_placeholders
    from unittest.mock import patch

    raw = dump_multistream_channels(
        [
            MultistreamChannel(
                platform="goodgame",
                url="https://goodgame.ru/Abver",
                channel_id="Abver",
                label="Abver",
            )
        ]
    )
    with patch("multistream._is_online", return_value=True):
        vals = status_placeholders(raw, "{goodgame_status} {vkplay_status}")
    assert vals["goodgame_status"] == STATUS_ONLINE
    assert vals["vkplay_status"] == "—"
    assert vals["youtube_status"] == "—"
    with patch("multistream._is_online", return_value=False):
        vals = status_placeholders(raw, "{goodgame_status}")
    assert vals["goodgame_status"] == STATUS_OFFLINE
    # Template without status tokens → still returns defaults, no live checks needed
    # beyond parse; empty needed list early path when template has no tokens:
    empty = status_placeholders(raw, "hello {username}")
    assert empty["goodgame_status"] == "—"


def check_multistream_prompt_i18n_format() -> None:
    """Literal {goodgame_status} in prompts must be escaped for str.format."""
    from i18n import SUPPORTED_LOCALES, t
    from multistream import MULTISTREAM_MAX

    for lang in SUPPORTED_LOCALES:
        empty = t("multistream_prompt_empty", lang, max=MULTISTREAM_MAX)
        assert "{goodgame_status}" in empty
        assert "{vkplay_status}" in empty
        assert "{youtube_status}" in empty
        filled = t(
            "multistream_prompt",
            lang,
            max=MULTISTREAM_MAX,
            list="• GoodGame: Abver",
        )
        assert "Abver" in filled
        assert "{goodgame_status}" in filled
