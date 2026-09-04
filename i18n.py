from __future__ import annotations

import calendar as cal_mod
import html
import re
from datetime import date, datetime, timedelta, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

SUPPORTED_LOCALES = ("en", "ru")
DEFAULT_LOCALE = "en"
SCHEDULE_TZ = timezone(timedelta(hours=3))
# IANA name for Twitch Helix schedule API (must not be "UTC+03:00").
SCHEDULE_TZ_NAME = "Europe/Moscow"


def _load_strings() -> dict[str, dict[str, str]]:
    import json
    from pathlib import Path

    base = Path(__file__).resolve().parent / "locales"
    out: dict[str, dict[str, str]] = {}
    for loc in SUPPORTED_LOCALES:
        path = base / f"{loc}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"Invalid locale file: {path}")
        out[loc] = {str(k): str(v) for k, v in data.items()}
    return out


_STRINGS: dict[str, dict[str, str]] = _load_strings()


def t(key: str, lang: str, **kwargs: object) -> str:
    locale = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    text = _STRINGS[locale].get(key) or _STRINGS[DEFAULT_LOCALE][key]
    return text.format(**kwargs) if kwargs else text


def t_bullet(key: str, lang: str, **kwargs: object) -> str:
    """Subscription list line: same copy as wizard notes, with a leading bullet."""
    return f"• {t(key, lang, **kwargs)}"


def placeholders_list_url(lang: str) -> str:
    from config import PUBLIC_BASE_URL

    if not PUBLIC_BASE_URL:
        return ""
    loc = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    return f"{PUBLIC_BASE_URL}/placeholders?lang={loc}"


def placeholders_link_html(lang: str) -> str:
    url = placeholders_list_url(lang)
    if not url:
        return html.escape(t("placeholders_link_unavailable", lang))
    label = html.escape(t("placeholders_link_label", lang))
    return f'<a href="{html.escape(url)}">{label}</a>'


def channel_dup_keyboard(lang: str, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("channel_dup_edit", lang),
                    callback_data=f"dup:edit:{sub_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    t("channel_dup_continue", lang),
                    callback_data="dup:continue",
                )
            ],
        ]
    )


def btn(key: str, lang: str) -> str:
    return t(f"btn_{key}", lang)


def all_btn_texts(key: str) -> set[str]:
    return {btn(key, loc) for loc in SUPPORTED_LOCALES}


_BETA_COUNT_SUFFIX = re.compile(r" \(\d+/\d+\)$")


def beta_mode_btn(lang: str, enrolled: int, total: int) -> str:
    return f"{btn('beta_mode', lang)} ({enrolled}/{total})"


def is_menu_button(text: str) -> bool:
    if text in all_menu_buttons():
        return True
    stripped = _BETA_COUNT_SUFFIX.sub("", text)
    return stripped != text and stripped in {
        btn("beta_mode", loc) for loc in SUPPORTED_LOCALES
    }


def all_menu_buttons() -> set[str]:
    keys = (
        "new",
        "import_twitch",
        "manage",
        "list",
        "edit",
        "delete",
        "cart",
        "pause_notifications",
        "feedback",
        "create_schedule",
        "alert_history",
        "other",
        "settings",
        "language",
        "admin",
        "demo",
        "broadcast",
        "broadcast_new",
        "scheduled_broadcasts",
        "sent_broadcasts",
        "stats",
        "back",
        "sys_notifications",
        "ignored_words",
        "whisper_alerts",
        "beta_mode",
        "sync_subs",
        "premium",
        "partner",
        "partner_stats",
        "partner_link",
        "partner_withdraw",
        "partner_withdrawals",
        "back_settings",
        "admin_withdrawals",
        "admin_refund",
        "watch",
        "chat",
    )
    return {btn(k, loc) for k in keys for loc in SUPPORTED_LOCALES}


def all_wizard_nav_buttons() -> set[str]:
    return {btn(k, loc) for k in ("wizard_back", "wizard_cancel") for loc in SUPPORTED_LOCALES}


def main_menu(
    lang: str, *, is_admin: bool = False, demo_active: bool = False
) -> ReplyKeyboardMarkup:
    from config import show_help_button

    rows = [
        [
            KeyboardButton(btn("new", lang)),
            KeyboardButton(btn("import_twitch", lang)),
        ],
        [
            KeyboardButton(btn("list", lang)),
            KeyboardButton(btn("alert_history", lang)),
        ],
        [
            KeyboardButton(btn("other", lang)),
            KeyboardButton(btn("settings", lang)),
        ],
    ]
    if show_help_button():
        rows.append([KeyboardButton(btn("feedback", lang))])
    if demo_active:
        rows.append([KeyboardButton(btn("demo", lang))])
    elif is_admin:
        rows.append([KeyboardButton(btn("admin", lang))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _pair_reply_rows(buttons: list[KeyboardButton]) -> list[list[KeyboardButton]]:
    """Two buttons per row; a lone last button gets its own row."""
    rows: list[list[KeyboardButton]] = []
    i = 0
    while i + 1 < len(buttons):
        rows.append([buttons[i], buttons[i + 1]])
        i += 2
    if i < len(buttons):
        rows.append([buttons[i]])
    return rows


def subscriptions_menu(
    lang: str,
    *,
    cart: bool = False,
    pause_notifications: bool = False,
) -> ReplyKeyboardMarkup:
    keys: list[str] = []
    if cart:
        keys.append("cart")
    if pause_notifications:
        keys.append("pause_notifications")
    keys.append("back")
    buttons = [KeyboardButton(btn(k, lang)) for k in keys]
    return ReplyKeyboardMarkup(_pair_reply_rows(buttons), resize_keyboard=True)


def other_menu(lang: str) -> ReplyKeyboardMarkup:
    keys = ["whisper_alerts", "create_schedule", "watch", "chat", "back"]
    buttons = [KeyboardButton(btn(k, lang)) for k in keys]
    return ReplyKeyboardMarkup(_pair_reply_rows(buttons), resize_keyboard=True)


def settings_menu(
    lang: str, *, beta_enrolled: int = 0, beta_total: int = 0
) -> ReplyKeyboardMarkup:
    from config import show_partner_ui, show_premium_ui

    buttons: list[KeyboardButton] = []
    if show_premium_ui():
        buttons.append(KeyboardButton(btn("premium", lang)))
    buttons.extend(
        [
            KeyboardButton(btn("sync_subs", lang)),
            KeyboardButton(btn("ignored_words", lang)),
            KeyboardButton(beta_mode_btn(lang, beta_enrolled, beta_total)),
            KeyboardButton(btn("sys_notifications", lang)),
            KeyboardButton(btn("language", lang)),
        ]
    )
    if show_partner_ui():
        buttons.append(KeyboardButton(btn("partner", lang)))
    rows = _pair_reply_rows(buttons)
    rows.append([KeyboardButton(btn("back", lang))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def partner_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(btn("partner_stats", lang)),
                KeyboardButton(btn("partner_link", lang)),
            ],
            [
                KeyboardButton(btn("partner_withdraw", lang)),
                KeyboardButton(btn("partner_withdrawals", lang)),
            ],
            [
                KeyboardButton(btn("back_settings", lang)),
            ],
        ],
        resize_keyboard=True,
    )


def premium_actions_keyboard(
    lang: str,
    *,
    show_trial: bool = True,
    show_plans: bool = True,
    show_features: bool = True,
    show_owned: bool = False,
    user_id: int | None = None,
) -> InlineKeyboardMarkup:
    from premium import stars_feature_price, stars_lifetime_price, stars_price, stars_year_price

    rows: list[list[InlineKeyboardButton]] = []
    if show_trial:
        rows.append(
            [InlineKeyboardButton(btn("premium_trial", lang), callback_data="premium:trial")]
        )
    rows.append(
        [
            InlineKeyboardButton(
                btn("premium_marfapr", lang), callback_data="premium:marfapr"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                btn("premium_channel", lang), callback_data="premium:channel"
            )
        ]
    )
    if show_plans:
        rows.append(
            [
                InlineKeyboardButton(
                    t("btn_premium_month", lang, stars=stars_price(user_id)),
                    callback_data="premium:month",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    t("btn_premium_year", lang, stars=stars_year_price(user_id)),
                    callback_data="premium:year",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    t("btn_premium_lifetime", lang, stars=stars_lifetime_price(user_id)),
                    callback_data="premium:life",
                )
            ]
        )
    if show_features:
        rows.append(
            [
                InlineKeyboardButton(
                    btn("premium_features", lang), callback_data="premium:features"
                )
            ]
        )
    if show_owned:
        rows.append(
            [
                InlineKeyboardButton(
                    btn("premium_owned", lang), callback_data="premium:owned"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def premium_features_keyboard(
    lang: str,
    selected: set[str],
    *,
    user_id: int | None = None,
    owned: set[str] | None = None,
) -> InlineKeyboardMarkup:
    from config import PREMIUM_FREE_ACTIVE_LIMIT
    from premium import feature_label_key, purchasable_feature_ids, stars_feature_price

    owned = owned or set()
    rows: list[list[InlineKeyboardButton]] = []
    for fid in purchasable_feature_ids():
        if fid in owned:
            continue
        mark = "✅" if fid in selected else "⬜️"
        label = t(
            feature_label_key(fid),
            lang,
            free_limit=PREMIUM_FREE_ACTIVE_LIMIT,
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark} {label}",
                    callback_data=f"premium:feat_toggle:{fid}",
                )
            ]
        )
    n = len(selected)
    total = n * stars_feature_price(user_id)
    if n > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    t("btn_premium_feat_pay", lang, stars=total),
                    callback_data="premium:feat_pay",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                btn("premium_feat_back", lang), callback_data="premium:feat_back"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def premium_owned_keyboard(
    lang: str,
    *,
    stars_cancelable: bool,
    feature_ids: list[str],
) -> InlineKeyboardMarkup:
    from config import PREMIUM_FREE_ACTIVE_LIMIT
    from premium import feature_label_key

    rows: list[list[InlineKeyboardButton]] = []
    if stars_cancelable:
        rows.append(
            [
                InlineKeyboardButton(
                    btn("premium_cancel_stars", lang),
                    callback_data="premium:cancel",
                )
            ]
        )
    for fid in feature_ids:
        name = t(
            feature_label_key(fid),
            lang,
            free_limit=PREMIUM_FREE_ACTIVE_LIMIT,
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"{btn('premium_cancel_feat', lang)} — {name}",
                    callback_data=f"premium:cancel_feat:{fid}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                btn("premium_feat_back", lang), callback_data="premium:feat_back"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def premium_gate_keyboard(lang: str, *, first_step: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(btn("premium_get", lang), callback_data="premium_gate:get")],
    ]
    if first_step:
        rows.append(
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang), callback_data="premium_gate:cancel"
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    btn("premium_skip", lang), callback_data="premium_gate:skip"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def import_mode_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("import_mode_sync", lang), callback_data="import_mode:sync")],
            [InlineKeyboardButton(t("import_mode_once", lang), callback_data="import_mode:once")],
        ]
    )


def sync_settings_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("sync_now", lang), callback_data="sync:now")],
            [InlineKeyboardButton(t("sync_change_period", lang), callback_data="sync:period")],
            [InlineKeyboardButton(t("sync_disable", lang), callback_data="sync:disable")],
        ]
    )


def admin_menu(lang: str) -> ReplyKeyboardMarkup:
    from config import show_partner_ui

    rows: list[list[KeyboardButton]] = [
        [
            KeyboardButton(btn("broadcast", lang)),
            KeyboardButton(btn("stats", lang)),
        ],
    ]
    if show_partner_ui():
        rows.append(
            [
                KeyboardButton(btn("admin_withdrawals", lang)),
                KeyboardButton(btn("demo", lang)),
            ]
        )
    else:
        rows.append([KeyboardButton(btn("demo", lang))])
    rows.append([KeyboardButton(btn("admin_refund", lang))])
    rows.append([KeyboardButton(btn("back", lang))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def withdrawal_actions_keyboard(withdrawal_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("btn_wd_paid", lang),
                    callback_data=f"ref_wd:paid:{withdrawal_id}",
                ),
                InlineKeyboardButton(
                    t("btn_wd_reject", lang),
                    callback_data=f"ref_wd:reject:{withdrawal_id}",
                ),
            ]
        ]
    )


def broadcast_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(btn("broadcast_new", lang))],
            [KeyboardButton(btn("scheduled_broadcasts", lang))],
            [KeyboardButton(btn("sent_broadcasts", lang))],
            [KeyboardButton(btn("back", lang))],
        ],
        resize_keyboard=True,
    )


def broadcast_feedback_keyboard(
    broadcast_id: int, up_count: int, down_count: int
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"👍 {up_count}",
                    callback_data=f"bcf:up:{broadcast_id}",
                ),
                InlineKeyboardButton(
                    f"👎 {down_count}",
                    callback_data=f"bcf:down:{broadcast_id}",
                ),
            ],
        ]
    )


def wizard_menu(lang: str, *, back: bool = True) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(btn("wizard_cancel", lang))]
    if back:
        row.insert(0, KeyboardButton(btn("wizard_back", lang)))
    return ReplyKeyboardMarkup([row], resize_keyboard=True)


def admin_wizard_menu(lang: str, *, back: bool = True) -> ReplyKeyboardMarkup:
    return wizard_menu(lang, back=back)


def language_keyboard(lang: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("English", callback_data="lang:en")],
        [InlineKeyboardButton("Русский", callback_data="lang:ru")],
    ]
    if lang:
        rows.append(
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang), callback_data="lang:cancel"
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def welcome_demo_keyboard(lang: str, sub_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    btn("welcome_demo_edit", lang),
                    callback_data=f"edit:{sub_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    btn("welcome_demo_delete", lang),
                    callback_data=f"welcome_del:{sub_id}",
                )
            ],
        ]
    )


def watch_cats_nav_keyboard(
    lang: str, *, has_cats: bool, show_recommended: bool = False
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                t("watch_cats_lucky", lang), callback_data="watch_cat:lucky"
            )
        ]
    ]
    if show_recommended:
        rows.append(
            [
                InlineKeyboardButton(
                    t("watch_cats_recommended", lang),
                    callback_data="watch_cat:recommended",
                )
            ]
        )
    if has_cats:
        rows.append(
            [InlineKeyboardButton(t("watch_cats_done", lang), callback_data="watch_cat:done")]
        )
        rows.append(
            [InlineKeyboardButton(t("watch_cats_clear", lang), callback_data="watch_cat:clear")]
        )
    rows.append(
        [InlineKeyboardButton(btn("wizard_cancel", lang), callback_data="watch_nav:cancel")]
    )
    return InlineKeyboardMarkup(rows)


def sync_unfollow_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("sync_unfollow_yes", lang), callback_data="sync_unfollow:yes"
                ),
                InlineKeyboardButton(
                    t("sync_unfollow_no", lang), callback_data="sync_unfollow:no"
                ),
            ]
        ]
    )


def delete_all_confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("delete_all_yes", lang), callback_data="delete_all:yes"
                ),
                InlineKeyboardButton(
                    t("delete_all_no", lang), callback_data="delete_all:no"
                ),
            ]
        ]
    )


def _watch_nav_row(lang: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(btn("wizard_back", lang), callback_data="watch_nav:back"),
        InlineKeyboardButton(btn("wizard_cancel", lang), callback_data="watch_nav:cancel"),
    ]


def watch_cats_pick_keyboard(
    lang: str, cats: list[dict[str, str]]
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                (c.get("name") or "?")[:64],
                callback_data=f"watch_cat:pick:{i}",
            )
        ]
        for i, c in enumerate(cats)
    ]
    return InlineKeyboardMarkup(rows)


def watch_viewers_keyboard(lang: str, *, show_nav: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t("watch_viewers_any", lang), callback_data="watch_viewers:any")],
    ]
    if show_nav:
        rows.append(_watch_nav_row(lang))
    return InlineKeyboardMarkup(rows)


def watch_lang_keyboard(lang: str, *, show_nav: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t("watch_lang_any", lang), callback_data="watch_lang:any")],
        [
            InlineKeyboardButton(t("watch_lang_ru", lang), callback_data="watch_lang:ru"),
            InlineKeyboardButton(t("watch_lang_en", lang), callback_data="watch_lang:en"),
        ],
        [InlineKeyboardButton(t("watch_lang_other", lang), callback_data="watch_lang:other")],
    ]
    if show_nav:
        rows.append(_watch_nav_row(lang))
    return InlineKeyboardMarkup(rows)


def watch_mature_keyboard(lang: str, *, show_nav: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                t("watch_mature_exclude", lang), callback_data="watch_mature:1"
            )
        ],
        [
            InlineKeyboardButton(
                t("watch_mature_allow", lang), callback_data="watch_mature:0"
            )
        ],
    ]
    if show_nav:
        rows.append(_watch_nav_row(lang))
    return InlineKeyboardMarkup(rows)


def watch_tags_keyboard(lang: str, *, show_nav: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t("watch_tags_skip", lang), callback_data="watch_tags:skip")],
    ]
    if show_nav:
        rows.append(_watch_nav_row(lang))
    return InlineKeyboardMarkup(rows)


def watch_save_keyboard(lang: str, *, show_nav: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t("watch_save_yes", lang), callback_data="watch_save:1")],
        [InlineKeyboardButton(t("watch_save_no", lang), callback_data="watch_save:0")],
    ]
    if show_nav:
        rows.append(_watch_nav_row(lang))
    return InlineKeyboardMarkup(rows)


def watch_pick_keyboard(lang: str, filters: list) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for f in filters:
        name = str(getattr(f, "name", "") or "?")[:64]
        fid = str(getattr(f, "id", ""))
        rows.append(
            [InlineKeyboardButton(name, callback_data=f"watch_pick:{fid}")]
        )
    rows.append(
        [InlineKeyboardButton(t("watch_pick_new", lang), callback_data="watch_pick:new")]
    )
    if filters:
        rows.append(
            [
                InlineKeyboardButton(
                    t("watch_pick_delete_btn", lang),
                    callback_data="watch_pick:delete",
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def watch_delete_pick_keyboard(
    lang: str, filters: list, selected: set[str]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, f in enumerate(filters, 1):
        fid = str(getattr(f, "id", ""))
        name = str(getattr(f, "name", "") or "?")[:48]
        mark = "✅ " if fid in selected else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark}🗑 #{i} {name}",
                    callback_data=f"watch_del_sel:{fid}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("watch_delete_go", lang, count=len(selected)),
                callback_data="watch_del_go",
            )
        ]
    )
    if selected:
        rows.append(
            [
                InlineKeyboardButton(
                    t("watch_delete_clear", lang),
                    callback_data="watch_del_clear",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("watch_delete_back", lang),
                callback_data="watch_del_back",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def watch_suggest_keyboard(
    lang: str, *, offer_create_alerts: bool = False
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if offer_create_alerts:
        rows.append(
            [
                InlineKeyboardButton(
                    t("watch_create_alerts", lang),
                    callback_data="watch:create_alerts",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(t("watch_again", lang), callback_data="watch:again")]
    )
    rows.append(
        [InlineKeyboardButton(t("watch_change", lang), callback_data="watch:change")]
    )
    return InlineKeyboardMarkup(rows)


def dest_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("dest_dm", lang), callback_data="dest:dm")],
            [InlineKeyboardButton(t("dest_chat", lang), callback_data="dest:chat")],
        ]
    )


def advanced_options_keyboard(
    lang: str,
    *,
    want_image: bool,
    want_strip: bool,
    want_ignore: bool,
    want_delay: bool,
    want_repeat: bool,
    want_delete: bool,
    want_chat: bool,
    want_preview: bool = False,
    show_delay: bool = True,
    show_repeat: bool = True,
    show_preview: bool = False,
    locked: frozenset[str] | set[str] | None = None,
) -> InlineKeyboardMarkup:
    locked = frozenset(locked or ())

    def _row(flag: bool, label_key: str, toggle: str) -> list[InlineKeyboardButton]:
        mark = "✅ " if flag else "⬜️ "
        label = t(label_key, lang)
        if toggle in locked and not flag:
            label = f"🔒 {label}"
        return [
            InlineKeyboardButton(
                mark + label,
                callback_data=f"advopt:toggle:{toggle}",
            )
        ]

    rows: list[list[InlineKeyboardButton]] = [
        _row(want_image, "advanced_options_image", "image"),
        _row(want_strip, "advanced_options_strip", "strip"),
        _row(want_ignore, "advanced_options_ignore", "ignore"),
    ]
    if show_delay:
        rows.append(_row(want_delay, "advanced_options_delay", "delay"))
    if show_repeat:
        rows.append(_row(want_repeat, "advanced_options_repeat", "repeat"))
    rows.append(_row(want_delete, "advanced_options_delete", "delete"))
    rows.append(_row(want_chat, "advanced_options_chat", "chat"))
    if show_preview:
        rows.append(_row(want_preview, "advanced_options_preview", "preview"))
    rows.append(
        [
            InlineKeyboardButton(
                t("advanced_options_next", lang),
                callback_data="advopt:next",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def delete_old_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delete_old_yes", lang), callback_data="delete_old:1")],
            [InlineKeyboardButton(t("delete_old_no", lang), callback_data="delete_old:0")],
        ]
    )


def delete_fail_notify_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delete_fail_yes", lang), callback_data="delete_fail:1")],
            [InlineKeyboardButton(t("delete_fail_no", lang), callback_data="delete_fail:0")],
        ]
    )


def delivery_fail_notice_keyboard(sub_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("delivery_fail_edit_btn", lang),
                    callback_data=f"edit:{sub_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    t("delivery_fail_delete_btn", lang),
                    callback_data=f"delivery_fail_del:{sub_id}",
                )
            ],
        ]
    )


def link_preview_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("link_preview_on", lang), callback_data="link_preview:0")],
            [InlineKeyboardButton(t("link_preview_off", lang), callback_data="link_preview:1")],
        ]
    )


def chat_button_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("chat_button_no", lang), callback_data="chat_button:0")],
            [InlineKeyboardButton(t("chat_button_yes", lang), callback_data="chat_button:1")],
        ]
    )


def ignore_keywords_keyboard(
    lang: str,
    *,
    as_cancel: bool = False,
    use_global: bool = False,
    show_back: bool = False,
    show_cancel: bool = False,
) -> InlineKeyboardMarkup:
    mark = "✅ " if use_global else "❌ "
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                mark + t("ignore_keywords_use_global", lang),
                callback_data="ignore_keywords:global_toggle",
            )
        ]
    ]
    if as_cancel:
        rows.append(
            [
                InlineKeyboardButton(
                    t("ignore_keywords_cancel", lang),
                    callback_data="ignore_keywords:cancel",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    t("ignore_keywords_skip", lang),
                    callback_data="ignore_keywords:skip",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if show_back:
        nav.append(
            InlineKeyboardButton(
                btn("wizard_back", lang), callback_data="ignore_keywords:back"
            )
        )
    if show_cancel:
        nav.append(
            InlineKeyboardButton(
                btn("wizard_cancel", lang), callback_data="ignore_keywords:cancel"
            )
        )
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def ignored_words_keyboard(lang: str, *, has_words: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_words:
        rows.append(
            [
                InlineKeyboardButton(
                    t("ignored_words_clear", lang),
                    callback_data="ignored_words:clear",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("ignored_words_cancel", lang),
                callback_data="ignored_words:cancel",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def whisper_alerts_keyboard(lang: str, *, enabled: bool) -> InlineKeyboardMarkup:
    mark = "✅ " if enabled else "⬜️ "
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    mark + t("whisper_alerts_enable", lang),
                    callback_data="whisper_alerts:toggle",
                )
            ]
        ]
    )


def message_draft_keyboard(lang: str, *, enabled: bool) -> InlineKeyboardMarkup:
    mark = "✅ " if enabled else "⬜️ "
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    mark + t("message_draft_activate", lang),
                    callback_data="message_draft:toggle",
                )
            ]
        ]
    )


def beta_mode_keyboard(
    lang: str,
    features: list[tuple[str, str, bool, str]],
) -> InlineKeyboardMarkup:
    """features: (id, title, enrolled, bug_url) per row."""
    rows: list[list[InlineKeyboardButton]] = []
    for fid, title, enrolled, bug_url in features:
        mark = "✅ " if enrolled else "⬜️ "
        action = t("beta_mode_leave", lang) if enrolled else t("beta_mode_join", lang)
        rows.append(
            [
                InlineKeyboardButton(
                    mark + action + ": " + title,
                    callback_data=f"beta:toggle:{fid}",
                ),
                InlineKeyboardButton(
                    t("beta_mode_report_bug", lang),
                    url=bug_url,
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def delay_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delay_yes", lang), callback_data="delay_send:1")],
            [InlineKeyboardButton(t("delay_no", lang), callback_data="delay_send:0")],
        ]
    )


def repeat_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("repeat_yes", lang), callback_data="repeat:1")],
            [InlineKeyboardButton(t("repeat_no", lang), callback_data="repeat:0")],
        ]
    )


def schedule_reminder_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("schedule_reminder_yes", lang),
                    callback_data="sched_remind:1",
                )
            ],
            [
                InlineKeyboardButton(
                    t("schedule_reminder_no", lang),
                    callback_data="sched_remind:0",
                )
            ],
        ]
    )


def alert_type_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("alert_type_live", lang),
                    callback_data="alert_type:live",
                )
            ],
            [
                InlineKeyboardButton(
                    t("alert_type_category", lang),
                    callback_data="alert_type:category",
                )
            ],
            [
                InlineKeyboardButton(
                    t("alert_type_upcoming", lang),
                    callback_data="alert_type:upcoming",
                )
            ],
            [
                InlineKeyboardButton(
                    t("alert_type_end", lang),
                    callback_data="alert_type:end",
                )
            ],
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang),
                    callback_data="alert_type:cancel",
                )
            ],
        ]
    )


def delete_sibling_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("delete_sibling_yes", lang),
                    callback_data="delete_sibling:1",
                )
            ],
            [
                InlineKeyboardButton(
                    t("delete_sibling_no", lang),
                    callback_data="delete_sibling:0",
                )
            ],
        ]
    )


def schedule_live_add_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("schedule_live_add_yes", lang),
                    callback_data="sched_live:1",
                )
            ],
            [
                InlineKeyboardButton(
                    t("schedule_live_add_no", lang),
                    callback_data="sched_live:0",
                )
            ],
        ]
    )


def admin_type_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("broadcast_type_bot_update", lang),
                    callback_data="admin_type:bot_update",
                )
            ],
            [
                InlineKeyboardButton(
                    t("broadcast_type_availability", lang),
                    callback_data="admin_type:availability",
                )
            ],
            [
                InlineKeyboardButton(
                    t("broadcast_type_other", lang),
                    callback_data="admin_type:other",
                )
            ],
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang), callback_data="admin_type:cancel"
                )
            ],
        ]
    )


def admin_other_audience_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("broadcast_audience_ids", lang),
                    callback_data="admin_audience:ids",
                )
            ],
            [
                InlineKeyboardButton(
                    t("broadcast_audience_all", lang),
                    callback_data="admin_audience:all",
                )
            ],
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang), callback_data="admin_audience:cancel"
                )
            ],
        ]
    )


def sys_notifications_keyboard(
    lang: str,
    *,
    updates_enabled: bool,
    availability_enabled: bool,
    other_enabled: bool,
    sync_enabled: bool,
) -> InlineKeyboardMarkup:
    updates_mark = "✅ " if updates_enabled else "❌ "
    availability_mark = "✅ " if availability_enabled else "❌ "
    other_mark = "✅ " if other_enabled else "❌ "
    sync_mark = "✅ " if sync_enabled else "❌ "
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    updates_mark + t("sys_updates_label", lang),
                    callback_data="sys_updates:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    availability_mark + t("sys_availability_label", lang),
                    callback_data="sys_availability:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    other_mark + t("sys_other_label", lang),
                    callback_data="sys_other:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    sync_mark + t("sys_sync_label", lang),
                    callback_data="sys_sync:toggle",
                )
            ],
        ]
    )


_WEEKDAYS = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
}
_MONTHS = {
    "en": ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "ru": ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"],
}
# Nominative short names for month picker buttons (genitive _MONTHS is for date phrases).
_MONTH_BUTTONS = {
    "en": ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "ru": ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"],
}
SCHEDULE_CALENDAR_MONTHS = 12
SCHEDULE_MAX_DAY_OFFSET = 366


def _format_schedule_date(d: date, lang: str) -> str:
    loc = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    wd = _WEEKDAYS[loc][d.weekday()]
    month = _MONTHS[loc][d.month]
    if loc == "ru":
        return f"{wd}, {d.day} {month}"
    return f"{wd}, {month} {d.day}"


def format_stream_schedule_date(d: date, lang: str) -> str:
    loc = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    month = _MONTHS[loc][d.month]
    if loc == "ru":
        return f"{d.day} {month}"
    return f"{d.day} {month}"


def format_stream_schedule_prompt_date(d: date, lang: str) -> str:
    return _format_schedule_date(d, lang)


def format_stream_schedule_result(entries: list[dict], lang: str) -> str:
    lines = [
        t(
            "stream_schedule_line",
            lang,
            date=format_stream_schedule_date(entry["date"], lang),
            time=entry["time"],
            game=entry["game"],
        )
        for entry in entries
    ]
    return "\n".join(lines)


def stream_schedule_confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("stream_schedule_yes", lang), callback_data="stream_sched:confirm:1")],
            [InlineKeyboardButton(t("stream_schedule_no", lang), callback_data="stream_sched:confirm:0")],
            [
                InlineKeyboardButton(
                    t("stream_schedule_mode_tz_btn", lang),
                    callback_data="stream_sched:tz:confirm",
                )
            ],
        ]
    )


def stream_schedule_mode_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("stream_schedule_mode_week_btn", lang),
                    callback_data="stream_sched:mode:week",
                )
            ],
            [
                InlineKeyboardButton(
                    t("stream_schedule_mode_day_btn", lang),
                    callback_data="stream_sched:mode:day",
                )
            ],
            [
                InlineKeyboardButton(
                    t("stream_schedule_mode_tz_btn", lang),
                    callback_data="stream_sched:tz:mode",
                )
            ],
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang), callback_data="stream_sched:cancel"
                )
            ],
        ]
    )


def stream_schedule_publish_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("stream_schedule_publish_yes", lang), callback_data="stream_sched:publish:1")],
            [InlineKeyboardButton(t("stream_schedule_publish_no", lang), callback_data="stream_sched:publish:0")],
        ]
    )


def stream_schedule_duration_keyboard(lang: str) -> InlineKeyboardMarkup:
    hour_row = [
        InlineKeyboardButton(
            t("stream_schedule_duration_hour", lang, hours=h),
            callback_data=f"stream_sched:duration:{h}",
        )
        for h in (1, 2, 3, 4)
    ]
    return InlineKeyboardMarkup(
        [
            hour_row,
            [
                InlineKeyboardButton(
                    t("stream_schedule_duration_unsure", lang),
                    callback_data="stream_sched:duration:0",
                )
            ],
            [
                InlineKeyboardButton(
                    btn("wizard_cancel", lang), callback_data="stream_sched:cancel"
                )
            ],
        ]
    )


def template_typo_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delay_yes", lang), callback_data="template_typo:1")],
            [InlineKeyboardButton(t("delay_no", lang), callback_data="template_typo:0")],
        ]
    )


def stored_typo_fix_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delay_yes", lang), callback_data="stored_typo_fix:1")],
            [InlineKeyboardButton(t("delay_no", lang), callback_data="stored_typo_fix:0")],
        ]
    )



def template_strip_keyboard(
    lang: str,
    *,
    enabled: bool = False,
    show_strip: bool = True,
    show_back: bool = False,
    show_cancel: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if show_strip:
        mark = "✅ " if enabled else "❌ "
        rows.append(
            [
                InlineKeyboardButton(
                    mark + t("strip_name_label", lang),
                    callback_data="strip_name:toggle",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if show_back:
        nav.append(
            InlineKeyboardButton(btn("wizard_back", lang), callback_data="strip_name:back")
        )
    if show_cancel:
        nav.append(
            InlineKeyboardButton(
                btn("wizard_cancel", lang), callback_data="strip_name:cancel"
            )
        )
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def image_ask_keyboard(lang: str, *, show_game_cover: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t("image_add", lang), callback_data="image_ask:add")]
    ]
    if show_game_cover:
        rows.append(
            [
                InlineKeyboardButton(
                    t("image_game_cover", lang), callback_data="image_ask:game_cover"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(t("image_skip", lang), callback_data="image_ask:skip")]
    )
    return InlineKeyboardMarkup(rows)


def image_edit_keyboard(
    lang: str, *, has_image: bool, show_game_cover: bool = False
) -> InlineKeyboardMarkup:
    if has_image:
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    t("edit_image_replace", lang),
                    callback_data="image_ask:add",
                )
            ],
        ]
        if show_game_cover:
            rows.append(
                [
                    InlineKeyboardButton(
                        t("image_game_cover", lang),
                        callback_data="image_ask:game_cover",
                    )
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        t("edit_image_delete", lang),
                        callback_data="image_ask:delete",
                    )
                ],
                [
                    InlineKeyboardButton(
                        t("edit_image_keep", lang),
                        callback_data="image_ask:keep",
                    )
                ],
            ]
        )
        return InlineKeyboardMarkup(rows)
    return image_ask_keyboard(lang, show_game_cover=show_game_cover)


def image_position_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("image_position_before", lang),
                    callback_data="image_pos:before",
                )
            ],
            [
                InlineKeyboardButton(
                    t("image_position_after", lang),
                    callback_data="image_pos:after",
                )
            ],
        ]
    )


def stream_schedule_day_keyboard(
    lang: str, *, show_finish: bool, show_skip: bool = True
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if show_skip:
        rows.append(
            [
                InlineKeyboardButton(
                    t("stream_schedule_no_stream", lang),
                    callback_data="stream_sched:skip",
                )
            ]
        )
    if show_finish:
        rows.append(
            [
                InlineKeyboardButton(
                    t("stream_schedule_finish", lang),
                    callback_data="stream_sched:finish",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                btn("wizard_cancel", lang), callback_data="stream_sched:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def stream_schedule_more_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("stream_schedule_more_yes", lang),
                    callback_data="stream_sched:more:1",
                )
            ],
            [
                InlineKeyboardButton(
                    t("stream_schedule_more_no", lang),
                    callback_data="stream_sched:more:0",
                )
            ],
        ]
    )


def stream_schedule_occupied_keyboard(
    lang: str, slots: list[dict]
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, slot in enumerate(slots):
        if not slot.get("id"):
            continue
        label = f"{slot.get('time', '')} {slot.get('game', '')}".strip()
        if len(label) > 48:
            label = label[:45] + "…"
        rows.append(
            [
                InlineKeyboardButton(label or "—", callback_data="stream_sched:noop"),
                InlineKeyboardButton(
                    t("stream_schedule_edit_slot", lang),
                    callback_data=f"stream_sched:edit:{i}",
                ),
                InlineKeyboardButton(
                    t("stream_schedule_delete_slot", lang),
                    callback_data=f"stream_sched:delete:{i}",
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t("stream_schedule_add_slot", lang),
                callback_data="stream_sched:add",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                t("stream_schedule_slots_done", lang),
                callback_data="stream_sched:slots_done",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                btn("wizard_cancel", lang), callback_data="stream_sched:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def stream_schedule_fix_day_keyboard(
    lang: str, dates: list[date]
) -> InlineKeyboardMarkup:
    loc = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    rows: list[list[InlineKeyboardButton]] = []
    for i, d in enumerate(dates):
        # Short day buttons; the full date is shown in the next prompt.
        day_short = _WEEKDAYS[loc][d.weekday()].upper()
        rows.append(
            [InlineKeyboardButton(day_short, callback_data=f"stream_sched:fix_day:{i}")]
        )
    rows.append(
        [
            InlineKeyboardButton(
                btn("wizard_cancel", lang), callback_data="stream_sched:cancel"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def format_schedule_month_label(year: int, month: int, lang: str) -> str:
    loc = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    label = _MONTH_BUTTONS[loc][month]
    now_year = datetime.now(SCHEDULE_TZ).year
    if year != now_year:
        return f"{label} {year}"
    return label


def schedule_month_keyboard(lang: str, *, prefix: str = "sched") -> InlineKeyboardMarkup:
    loc = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    now = datetime.now(SCHEDULE_TZ).date()
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    year, month = now.year, now.month
    for _ in range(SCHEDULE_CALENDAR_MONTHS):
        label = format_schedule_month_label(year, month, loc)
        row.append(
            InlineKeyboardButton(
                label, callback_data=f"{prefix}:month:{year}-{month:02d}"
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
        month += 1
        if month > 12:
            month = 1
            year += 1
    if row:
        rows.append(row)
    rows.append(
        [InlineKeyboardButton(btn("wizard_back", lang), callback_data=f"{prefix}:time")]
    )
    return InlineKeyboardMarkup(rows)


def schedule_calendar_days_keyboard(
    lang: str,
    year: int,
    month: int,
    schedule: dict,
    *,
    prefix: str = "sched",
) -> InlineKeyboardMarkup:
    loc = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    now = datetime.now(SCHEDULE_TZ).date()
    selected_offset = int(schedule.get("date_offset", 0))
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(wd, callback_data=f"{prefix}:noop")
            for wd in _WEEKDAYS[loc]
        ]
    ]
    week: list[InlineKeyboardButton] = []
    for d in cal_mod.Calendar(firstweekday=0).itermonthdates(year, month):
        if d.month != month:
            week.append(InlineKeyboardButton("·", callback_data=f"{prefix}:noop"))
        else:
            offset = (d - now).days
            if offset < 0 or offset > SCHEDULE_MAX_DAY_OFFSET:
                week.append(
                    InlineKeyboardButton(str(d.day), callback_data=f"{prefix}:noop")
                )
            else:
                label = f"✅{d.day}" if offset == selected_offset else str(d.day)
                week.append(
                    InlineKeyboardButton(
                        label, callback_data=f"{prefix}:date:{offset}"
                    )
                )
        if len(week) == 7:
            rows.append(week)
            week = []
    rows.append(
        [
            InlineKeyboardButton(
                btn("wizard_back", lang), callback_data=f"{prefix}:calendar"
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def schedule_keyboard(
    lang: str,
    schedule: dict,
    *,
    prefix: str = "sched",
    show_send_now: bool = True,
) -> InlineKeyboardMarkup:
    now = datetime.now(SCHEDULE_TZ)
    page = int(schedule.get("date_page", 0))
    selected_offset = int(schedule.get("date_offset", 0))
    hour = schedule.get("hour")
    minute = schedule.get("minute")
    show_minutes = bool(schedule.get("show_minutes"))
    rows: list[list[InlineKeyboardButton]] = []

    rows.append(
        [
            InlineKeyboardButton(
                t("schedule_show_calendar", lang),
                callback_data=f"{prefix}:calendar",
            )
        ]
    )

    date_row: list[InlineKeyboardButton] = []
    for i in range(3):
        offset = page * 3 + i
        d = now.date() + timedelta(days=offset)
        label = _format_schedule_date(d, lang)
        if offset == selected_offset:
            label = f"✅ {label}"
        date_row.append(
            InlineKeyboardButton(label, callback_data=f"{prefix}:date:{offset}")
        )
    max_page = SCHEDULE_MAX_DAY_OFFSET // 3
    if page < max_page:
        date_row.append(InlineKeyboardButton("→", callback_data=f"{prefix}:date_next"))
    rows.append(date_row)

    rows.append([InlineKeyboardButton(t("schedule_saved_time", lang), callback_data=f"{prefix}:saved")])

    rows.append([InlineKeyboardButton(t("schedule_pick_hour", lang), callback_data=f"{prefix}:noop")])
    for block in range(4):
        hour_row = []
        for h in range(block * 6, block * 6 + 6):
            label = f"{h:02d}"
            if hour == h:
                label = f"✅ {label}"
            hour_row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:hour:{h}"))
        rows.append(hour_row)

    if show_minutes:
        rows.append([InlineKeyboardButton(t("schedule_minutes_header", lang), callback_data=f"{prefix}:noop")])
        min_row: list[InlineKeyboardButton] = []
        for m in range(0, 60, 5):
            label = f"{m:02d}"
            if minute == m:
                label = f"✅ {label}"
            min_row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:min:{m}"))
            if len(min_row) == 6:
                rows.append(min_row)
                min_row = []
        if min_row:
            rows.append(min_row)
    else:
        rows.append([InlineKeyboardButton(t("schedule_pick_minutes", lang), callback_data=f"{prefix}:toggle_min")])

    rows.append(
        [InlineKeyboardButton(t("schedule_apply", lang), callback_data=f"{prefix}:apply")]
    )
    if show_send_now:
        rows.append([InlineKeyboardButton(t("broadcast_send_now", lang), callback_data=f"{prefix}:now")])
    return InlineKeyboardMarkup(rows)


def scheduled_edit_keyboard(broadcast_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("scheduled_edit_text", lang),
                    callback_data=f"sb_edit_f:{broadcast_id}:text",
                )
            ],
            [
                InlineKeyboardButton(
                    t("scheduled_edit_time", lang),
                    callback_data=f"sb_edit_f:{broadcast_id}:time",
                )
            ],
            [
                InlineKeyboardButton(
                    t("scheduled_delete_btn", lang, id=broadcast_id),
                    callback_data=f"sb_delete:{broadcast_id}",
                )
            ],
        ]
    )


def scheduled_list_keyboard(items: list[int], lang: str) -> InlineKeyboardMarkup | None:
    if not items:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    for broadcast_id in items:
        rows.append(
            [
                InlineKeyboardButton(
                    t("scheduled_edit_btn", lang, id=broadcast_id),
                    callback_data=f"sb_edit:{broadcast_id}",
                ),
                InlineKeyboardButton(
                    t("scheduled_delete_btn", lang, id=broadcast_id),
                    callback_data=f"sb_delete:{broadcast_id}",
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def edit_options_keyboard(
    sub_id: int,
    lang: str,
    *,
    dest_type: str = "dm",
    delete_previous: bool = False,
    has_image: bool = False,
    strip_name_mentions: bool = False,
    show_link_preview: bool = True,
    schedule_reminder_configured: bool = False,
    notify_on_category_change: bool = False,
    notify_on_end: bool = False,
    is_upcoming: bool = False,
    show_advanced: bool = True,
) -> InlineKeyboardMarkup:
    # Shared Extras block order: image → strip → ignore → delay → repeat →
    # delete → chat. Edit-only: template, image remove, preview, schedule, dest,
    # delete sub-options, type/copy.
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(t("edit_template", lang), callback_data=f"edit_f:{sub_id}:template")],
        [
            InlineKeyboardButton(
                t("advanced_options_image", lang),
                callback_data=f"edit_f:{sub_id}:image",
            )
        ],
    ]
    if has_image:
        rows.append(
            [
                InlineKeyboardButton(
                    t("edit_image_delete", lang),
                    callback_data=f"edit_f:{sub_id}:image_del",
                )
            ]
        )
    strip_mark = "✅ " if strip_name_mentions else "⬜️ "
    rows.append(
        [
            InlineKeyboardButton(
                strip_mark + t("advanced_options_strip", lang),
                callback_data=f"edit_f:{sub_id}:strip",
            )
        ]
    )
    if show_advanced:
        rows.append(
            [
                InlineKeyboardButton(
                    t("advanced_options_ignore", lang),
                    callback_data=f"edit_f:{sub_id}:ignore_keywords",
                )
            ]
        )
    if show_advanced and not is_upcoming:
        rows.append(
            [
                InlineKeyboardButton(
                    t("advanced_options_delay", lang),
                    callback_data=f"edit_f:{sub_id}:delay",
                )
            ]
        )
        if not notify_on_category_change and not notify_on_end:
            rows.append(
                [
                    InlineKeyboardButton(
                        t("advanced_options_repeat", lang),
                        callback_data=f"edit_f:{sub_id}:repeat",
                    )
                ]
            )
    if show_advanced and dest_type != "dm":
        rows.append(
            [
                InlineKeyboardButton(
                    t("advanced_options_delete", lang),
                    callback_data=f"edit_f:{sub_id}:delete_old",
                )
            ]
        )
        if delete_previous:
            rows.append(
                [
                    InlineKeyboardButton(
                        t("edit_delete_fail_notify", lang),
                        callback_data=f"edit_f:{sub_id}:delete_fail",
                    )
                ]
            )
            if notify_on_category_change:
                rows.append(
                    [
                        InlineKeyboardButton(
                            t("edit_delete_other", lang),
                            callback_data=f"edit_f:{sub_id}:delete_other",
                        )
                    ]
                )
    if show_advanced:
        rows.append(
            [
                InlineKeyboardButton(
                    t("advanced_options_chat", lang),
                    callback_data=f"edit_f:{sub_id}:chat_button",
                )
            ]
        )
    if show_link_preview:
        rows.append(
            [
                InlineKeyboardButton(
                    t("advanced_options_preview", lang),
                    callback_data=f"edit_f:{sub_id}:preview",
                )
            ]
        )
    if schedule_reminder_configured:
        rows.append(
            [
                InlineKeyboardButton(
                    t("edit_schedule_reminder", lang),
                    callback_data=f"edit_f:{sub_id}:sched_remind",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(t("edit_dest", lang), callback_data=f"edit_f:{sub_id}:dest")]
    )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    t("edit_change_type", lang),
                    callback_data=f"edit_f:{sub_id}:change_type",
                )
            ],
            [
                InlineKeyboardButton(
                    t("edit_copy", lang),
                    callback_data=f"edit_f:{sub_id}:copy",
                )
            ],
            [
                InlineKeyboardButton(
                    t("edit_copy_change", lang),
                    callback_data=f"edit_f:{sub_id}:copy_change",
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


def edit_bool_keyboard(sub_id: int, field: str, lang: str) -> InlineKeyboardMarkup:
    if field == "preview":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("preview_yes", lang), callback_data=f"edit_set:{sub_id}:preview:1")],
                [InlineKeyboardButton(t("preview_no", lang), callback_data=f"edit_set:{sub_id}:preview:0")],
            ]
        )
    if field == "chat_button":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("chat_button_no", lang), callback_data=f"edit_set:{sub_id}:chat_button:0")],
                [InlineKeyboardButton(t("chat_button_yes", lang), callback_data=f"edit_set:{sub_id}:chat_button:1")],
            ]
        )
    if field == "repeat":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("repeat_yes", lang), callback_data=f"edit_set:{sub_id}:repeat:1")],
                [InlineKeyboardButton(t("repeat_no", lang), callback_data=f"edit_set:{sub_id}:repeat:0")],
            ]
        )
    if field == "delete_fail":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("delete_fail_yes", lang),
                        callback_data=f"edit_set:{sub_id}:delete_fail:1",
                    )
                ],
                [
                    InlineKeyboardButton(
                        t("delete_fail_no", lang),
                        callback_data=f"edit_set:{sub_id}:delete_fail:0",
                    )
                ],
            ]
        )
    if field == "delete_other":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("delete_sibling_yes", lang),
                        callback_data=f"edit_set:{sub_id}:delete_other:1",
                    )
                ],
                [
                    InlineKeyboardButton(
                        t("delete_sibling_no", lang),
                        callback_data=f"edit_set:{sub_id}:delete_other:0",
                    )
                ],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delete_old_yes", lang), callback_data=f"edit_set:{sub_id}:delete_old:1")],
            [InlineKeyboardButton(t("delete_old_no", lang), callback_data=f"edit_set:{sub_id}:delete_old:0")],
        ]
    )


def dest_label(dest_type: str, lang: str) -> str:
    return t(f"dest_label_{dest_type}", lang)
