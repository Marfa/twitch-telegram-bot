from __future__ import annotations

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

SUPPORTED_LOCALES = ("en", "ru")
DEFAULT_LOCALE = "en"

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "btn_new": "➕ New subscription",
        "btn_manage": "📋 Manage subscriptions",
        "btn_list": "📋 My subscriptions",
        "btn_edit": "✏️ Edit subscription",
        "btn_delete": "🗑 Delete subscription",
        "btn_feedback": "🐛 Report a problem",
        "btn_admin": "⚙️ Admin",
        "btn_broadcast": "📣 Broadcast",
        "btn_stats": "📊 Statistics",
        "btn_back": "◀️ Main menu",
        "menu_subs": "Manage subscriptions:",
        "menu_admin": "Admin panel:",
        "menu_main": "Main menu",
        "lang_pick": "Choose your language:",
        "lang_set": "Language set to English.",
        "start_welcome": (
            "Hi! I send notifications when Twitch streams go live.\n\n"
            "Commands: /help\n"
            "/start does not delete your subscriptions — it starts a new setup.\n\n"
            "Enter a Twitch channel: link, mobile link, or username."
        ),
        "new_sub_prompt": "Enter a Twitch channel: link, mobile link, or username.",
        "finish_setup_first": "Finish the subscription setup or tap /cancel.",
        "channel_not_parsed": (
            "Could not parse the channel. Examples:\n"
            "• ninja\n"
            "• https://twitch.tv/ninja\n"
            "• https://m.twitch.tv/ninja"
        ),
        "channel_not_found": 'Channel "{username}" not found on Twitch. Try again.',
        "channel_found": (
            "Channel: {display_name}\n\n"
            "Set the message format. Available placeholders:\n"
            "• <code>{{username}}</code> — channel name\n"
            "• <code>{{game}}</code> — stream category\n"
            "• <code>{{name}}</code> — stream title\n\n"
            "Example:\n"
            "<code>{{username}} is live!\n"
            "{{name}}\n"
            "Category: {{game}}</code>"
        ),
        "template_empty": "Template cannot be empty.",
        "link_preview_prompt": "Show link preview in notifications?",
        "link_preview_on": "✅ Show preview",
        "link_preview_off": "❌ Hide preview",
        "delay_prompt": "Delay sending the notification?",
        "delay_no": "❌ No",
        "delay_yes": "✅ Yes",
        "delay_minutes_prompt": "Enter the delay in minutes (a number):",
        "delay_minutes_invalid": "Enter a positive number of minutes, e.g. 5.",
        "dest_prompt": "Where should notifications be sent?",
        "dest_dm": "📩 To DM",
        "dest_channel": "📢 To channel",
        "dest_group": "💬 To group or community",
        "dest_label_dm": "DM",
        "dest_label_channel": "channel",
        "dest_label_group": "group or community",
        "channel_setup": (
            "Add the bot to the channel as an admin with posting rights.\n\n"
            "Then send the channel @username or forward a message from the channel."
        ),
        "group_setup": (
            "Add the bot to the group or community.\n\n"
            "Bot permissions:\n"
            "• Send messages (required)\n"
            "• Admin is not required if members can post\n\n"
            "Send one of:\n"
            "• Topic link: https://t.me/c/name/30\n"
            "• Group @username (no topic — general chat)\n"
            "• Group ID (e.g. -1001234567890)\n"
            "• Forwarded message from the group (must say «Forwarded from: …», not from DM)\n\n"
            "For groups with topics, a topic link is the most reliable option."
        ),
        "delete_old_text": (
            "Delete the bot's previous message when a new stream starts?\n\n"
            "If enabled, the bot deletes its last message in this chat before sending a new one.\n"
            "In channels the bot needs permission to delete messages.\n"
            "Telegram allows deleting only messages younger than ~48 hours."
        ),
        "delete_old_yes": "✅ Yes, delete",
        "delete_old_no": "❌ No",
        "group_not_found": "Group not found. Add the bot and check the link.",
        "dest_not_found_channel": "Channel not found. Check @username.",
        "dest_not_found_group": "Group not found. Check @username.",
        "fwd_from_dm": (
            "Forward from DM does not work. Need «Forwarded from: Group name» "
            "or a topic link: https://t.me/c/name/30"
        ),
        "dest_hint_group": (
            "Send a topic link, @username, group ID, or forward a message from the group."
        ),
        "dest_hint_channel": (
            "Send channel @username, ID, or forward a message from the channel."
        ),
        "chat_not_determined": "Could not determine the chat. Try again.",
        "not_a_channel": "This is not a channel. Specify a channel or forward from one.",
        "bot_no_channel": "The bot cannot see this channel. Add it as an admin.",
        "not_a_group": "This is not a group or community.",
        "bot_no_group": "The bot cannot see this group. Add it to the group.",
        "test_ok": "✅ Test: the bot can send notifications here.",
        "test_failed": (
            "Could not send a test message. Check the bot's permissions and try again."
        ),
        "save_failed": "Could not save the subscription. Try again: /start",
        "sub_created_short": "✅ Subscription created.",
        "setup_done": (
            "✅ Setup complete!\n\n"
            "Subscription #{sub_id} created.\n"
            "Twitch channel: {twitch_username}\n"
            "Notifications: {dest}{thread_note}\n"
            "{delete_note}\n"
            "{preview_note}\n"
            "{delay_note}\n\n"
            "Sample message:\n{preview}\n\n"
            "When {twitch_username} goes live — I'll send a notification.\n"
            "Help: /help"
        ),
        "thread_note": "\nTopic: {thread_id}",
        "delete_yes": "Delete old messages: yes",
        "delete_no": "Delete old messages: no",
        "preview_off": "Link preview: off",
        "preview_on": "Link preview: on",
        "delay_yes_note": "Delayed send: {minutes} min",
        "delay_no_note": "Delayed send: no",
        "delayed_not_sent": (
            "Delayed notification was not sent — streamer is offline.\n\n"
            "Message:\n{message}"
        ),
        "preview_stream": "Test stream",
        "cancelled": "Cancelled.",
        "feedback": (
            "Feedback:\n"
            "• Telegram: @immarfa\n"
            "• GitHub Issues: {github}\n\n"
            "Support:\n"
            "• DonationAlerts: https://www.donationalerts.com/r/themarfa\n"
            "• Crypto: https://nowpayments.io/donation/themarfa\n\n"
            "Links:\n"
            "• Twitch: https://www.twitch.tv/marfapr\n"
            "• Telegram: https://t.me/themarfa\n"
            "• Website: https://blog.themarfa.name/\n\n"
            "Bot version: <code>{bot_version}</code>\n"
            "Your ID: <code>{user_id}</code>"
        ),
        "help": (
            "Available commands:\n"
            "/start — set up a stream subscription\n"
            "/help — show this help\n"
            "/cancel — cancel current setup\n\n"
            "Menu buttons:\n"
            "• {btn_new}\n"
            "• {btn_manage} — list, edit, delete\n"
            "• {btn_feedback}"
        ),
        "no_subs": (
            "No subscriptions yet.\n\n"
            "Tap ➕ New subscription.\n\n"
            "Help: /help"
        ),
        "subs_list": "Your subscriptions (tap to enable/disable):\n\n",
        "toggle_off": "⏸ Off",
        "toggle_on": "✅ On",
        "sub_line_thread": ", topic {thread_id}",
        "sub_line_delete": ", delete old",
        "sub_line_delay": ", delay {minutes} min",
        "sub_not_found": "Subscription not found.",
        "sub_enabled": "Subscription #{sub_id} enabled.",
        "sub_disabled": "Subscription #{sub_id} disabled.",
        "no_subs_short": "No subscriptions.",
        "delete_pick": "Choose a subscription to delete:",
        "sub_deleted": "Subscription #{sub_id} deleted.",
        "edit_pick": "Choose a subscription to edit:",
        "edit_menu": "Subscription #{sub_id} — {username}\n\nWhat to change?",
        "edit_template": "📝 Message template",
        "edit_dest": "📍 Destination",
        "edit_delete_old": "🗑 Delete old messages",
        "edit_link_preview": "🔗 Link preview",
        "edit_delay": "⏱ Delay send",
        "edit_template_prompt": (
            "Send a new message template for subscription #{sub_id}.\n\n"
            "Placeholders: <code>{{username}}</code>, <code>{{game}}</code>, <code>{{name}}</code>"
        ),
        "edit_updated": "✅ Subscription #{sub_id} updated.",
        "edit_delay_prompt": (
            "Subscription #{sub_id}\n"
            "Current delay: {current}\n\n"
            "Enter delay in minutes (0 — send immediately):"
        ),
        "edit_delay_current_none": "none (immediate)",
        "edit_delay_current": "{minutes} min",
        "edit_delay_invalid": "Enter 0 or a positive number of minutes.",
        "edit_delete_old_menu": (
            "Delete old messages on new stream?\n\n"
            "Telegram allows deleting only messages younger than ~48 hours."
        ),
        "edit_preview_menu": "Disable link preview in notifications?",
        "preview_yes": "✅ Off (no preview)",
        "preview_no": "❌ On (show preview)",
        "render_maintenance": (
            "⚠️ Render: planned maintenance\n"
            "The bot may be unavailable.\n\n"
            "{title}\n{body}\n\n"
            "{link}"
        ),
        "aiven_outage": (
            "⚠️ Aiven: service disruption\n"
            "Subscriptions may be unavailable.\n\n"
            "{title}\n{body}\n\n"
            "{link}"
        ),
        "conflict_polling": (
            "Polling conflict — two bot instances may be running "
            "(Render + local?). Keep only one."
        ),
        "unhandled_error": "Unhandled error: {err}",
        "broadcast_prompt": (
            "Send the message to broadcast to all users.\n"
            "/cancel — abort."
        ),
        "broadcast_empty": "Message cannot be empty.",
        "broadcast_done": (
            "Broadcast complete.\n"
            "Sent: {sent}\n"
            "Failed: {failed}\n"
            "Total recipients: {total}"
        ),
        "bot_stats": (
            "📊 Bot statistics\n\n"
            "Users: {users}\n"
            "Recipients (users + owners): {notify_users}\n"
            "Subscriptions: {subscriptions_total} "
            "(✅ {subscriptions_enabled} / ⏸ {subscriptions_disabled})\n"
            "Unique owners: {unique_owners}\n"
            "Twitch channels tracked: {unique_twitch_channels}\n\n"
            "Languages:\n"
            "• English: {locale_en}\n"
            "• Russian: {locale_ru}\n"
            "• Not set: {locale_unset}"
        ),
    },
    "ru": {
        "btn_new": "➕ Новая подписка",
        "btn_manage": "📋 Управление подписками",
        "btn_list": "📋 Мои подписки",
        "btn_edit": "✏️ Редактировать подписку",
        "btn_delete": "🗑 Удалить подписку",
        "btn_feedback": "🐛 Сообщить о проблеме",
        "btn_admin": "⚙️ Админка",
        "btn_broadcast": "📣 Рассылка",
        "btn_stats": "📊 Статистика",
        "btn_back": "◀️ Главное меню",
        "menu_subs": "Управление подписками:",
        "menu_admin": "Админка:",
        "menu_main": "Главное меню",
        "lang_pick": "Выберите язык / Choose your language:",
        "lang_set": "Язык: русский.",
        "start_welcome": (
            "Привет! Я присылаю уведомления о старте стримов на Twitch.\n\n"
            "Справка по командам: /help\n"
            "/start не удаляет ваши подписки — только запускает новую настройку.\n\n"
            "Укажите канал Twitch: ссылку, мобильную ссылку или username."
        ),
        "new_sub_prompt": "Укажите канал Twitch: ссылку, мобильную ссылку или username.",
        "finish_setup_first": "Сначала завершите настройку подписки или нажмите /cancel.",
        "channel_not_parsed": (
            "Не удалось распознать канал. Примеры:\n"
            "• ninja\n"
            "• https://twitch.tv/ninja\n"
            "• https://m.twitch.tv/ninja"
        ),
        "channel_not_found": "Канал «{username}» не найден на Twitch. Попробуйте ещё раз.",
        "channel_found": (
            "Канал: {display_name}\n\n"
            "Задайте формат сообщения. Доступные ключевые слова:\n"
            "• <code>{{username}}</code> — имя канала\n"
            "• <code>{{game}}</code> — категория стрима\n"
            "• <code>{{name}}</code> — название стрима\n\n"
            "Пример:\n"
            "<code>{{username}} в эфире!\n"
            "{{name}}\n"
            "Категория: {{game}}</code>"
        ),
        "template_empty": "Шаблон не может быть пустым.",
        "link_preview_prompt": "Показывать превью ссылок в уведомлениях?",
        "link_preview_on": "✅ Показывать превью",
        "link_preview_off": "❌ Скрыть превью",
        "delay_prompt": "Отложить отправку?",
        "delay_no": "❌ Нет",
        "delay_yes": "✅ Да",
        "delay_minutes_prompt": "Укажите задержку отправки в минутах (число):",
        "delay_minutes_invalid": "Введите положительное число минут, например 5.",
        "dest_prompt": "Куда отправлять уведомления?",
        "dest_dm": "📩 В личку",
        "dest_channel": "📢 В канал",
        "dest_group": "💬 В группу или сообщество",
        "dest_label_dm": "личку",
        "dest_label_channel": "канал",
        "dest_label_group": "группу или сообщество",
        "channel_setup": (
            "Добавьте бота в канал как администратора с правом публикации.\n\n"
            "Затем отправьте @username канала или перешлите сообщение из канала."
        ),
        "group_setup": (
            "Добавьте бота в группу или сообщество.\n\n"
            "Права бота:\n"
            "• Отправка сообщений (обязательно)\n"
            "• Администратор не нужен, если участникам разрешено писать\n\n"
            "Отправьте одно из:\n"
            "• Ссылку на тему: https://t.me/c/название/30\n"
            "• @username группы (без темы — в общий чат)\n"
            "• ID группы (например -1001234567890)\n"
            "• Пересланное сообщение из группы (должно быть «Переслано из: …», не из лички)\n\n"
            "Для групп с темами ссылка на тему — самый надёжный способ."
        ),
        "delete_old_text": (
            "Удалять предыдущее сообщение бота при новом стриме?\n\n"
            "Если включено — перед новым уведомлением бот удалит своё прошлое в этом чате.\n"
            "В канале боту нужно право удалять сообщения.\n"
            "Telegram позволяет удалять только сообщения младше ~48 часов."
        ),
        "delete_old_yes": "✅ Да, удалять",
        "delete_old_no": "❌ Нет",
        "group_not_found": "Группа не найдена. Добавьте бота и проверьте ссылку.",
        "dest_not_found_channel": "Канал не найден. Проверьте @username.",
        "dest_not_found_group": "Группа не найдена. Проверьте @username.",
        "fwd_from_dm": (
            "Пересылка из лички не подходит. Нужно «Переслано из: Название группы» "
            "или ссылка на тему: https://t.me/c/название/30"
        ),
        "dest_hint_group": (
            "Отправьте ссылку на тему, @username, ID группы "
            "или перешлите сообщение из группы."
        ),
        "dest_hint_channel": (
            "Отправьте @username канала, ID или перешлите сообщение из канала."
        ),
        "chat_not_determined": "Не удалось определить чат. Попробуйте ещё раз.",
        "not_a_channel": "Это не канал. Укажите канал или перешлите из канала.",
        "bot_no_channel": "Бот не видит этот канал. Добавьте бота как администратора.",
        "not_a_group": "Это не группа или сообщество.",
        "bot_no_group": "Бот не видит эту группу. Добавьте бота в группу.",
        "test_ok": "✅ Тест: бот может отправлять уведомления сюда.",
        "test_failed": (
            "Не удалось отправить тестовое сообщение. "
            "Проверьте права бота и попробуйте снова."
        ),
        "save_failed": "Не удалось сохранить подписку. Попробуйте ещё раз: /start",
        "sub_created_short": "✅ Подписка создана.",
        "setup_done": (
            "✅ Настройка завершена!\n\n"
            "Подписка #{sub_id} создана.\n"
            "Канал Twitch: {twitch_username}\n"
            "Уведомления: {dest}{thread_note}\n"
            "{delete_note}\n"
            "{preview_note}\n"
            "{delay_note}\n\n"
            "Пример сообщения:\n{preview}\n\n"
            "Когда {twitch_username} начнёт стрим — пришлю уведомление.\n"
            "Справка: /help"
        ),
        "thread_note": "\nТема: {thread_id}",
        "delete_yes": "Удалять старые сообщения: да",
        "delete_no": "Удалять старые сообщения: нет",
        "preview_off": "Превью ссылок: выключено",
        "preview_on": "Превью ссылок: включено",
        "delay_yes_note": "Отложенная отправка: {minutes} мин.",
        "delay_no_note": "Отложенная отправка: нет",
        "delayed_not_sent": (
            "Отложенное сообщение не было отправлено. Стример офлайн.\n\n"
            "Сообщение:\n{message}"
        ),
        "preview_stream": "Тестовый стрим",
        "cancelled": "Отменено.",
        "feedback": (
            "Обратная связь:\n"
            "• Telegram: @immarfa\n"
            "• GitHub Issues: {github}\n\n"
            "Поддержка:\n"
            "• DonationAlerts: https://www.donationalerts.com/r/themarfa\n"
            "• Криптой: https://nowpayments.io/donation/themarfa\n\n"
            "Ссылки:\n"
            "• Twitch: https://www.twitch.tv/marfapr\n"
            "• Telegram: https://t.me/themarfa\n"
            "• Сайт: https://blog.themarfa.name/\n\n"
            "Версия бота: <code>{bot_version}</code>\n"
            "Ваш ID: <code>{user_id}</code>"
        ),
        "help": (
            "Доступные команды:\n"
            "/start — настроить подписку на стрим\n"
            "/help — показать эту справку\n"
            "/cancel — отменить текущую настройку\n\n"
            "Кнопки меню:\n"
            "• {btn_new}\n"
            "• {btn_manage} — список, редактирование, удаление\n"
            "• {btn_feedback}"
        ),
        "no_subs": (
            "Подписок пока нет.\n\n"
            "Нажмите ➕ Новая подписка.\n\n"
            "Справка: /help"
        ),
        "subs_list": "Ваши подписки (нажмите, чтобы включить/выключить):\n\n",
        "toggle_off": "⏸ Выкл",
        "toggle_on": "✅ Вкл",
        "sub_line_thread": ", тема {thread_id}",
        "sub_line_delete": ", удалять старые",
        "sub_line_delay": ", задержка {minutes} мин.",
        "sub_not_found": "Подписка не найдена.",
        "sub_enabled": "Подписка #{sub_id} включена.",
        "sub_disabled": "Подписка #{sub_id} выключена.",
        "no_subs_short": "Подписок нет.",
        "delete_pick": "Выберите подписку для удаления:",
        "sub_deleted": "Подписка #{sub_id} удалена.",
        "edit_pick": "Выберите подписку для редактирования:",
        "edit_menu": "Подписка #{sub_id} — {username}\n\nЧто изменить?",
        "edit_template": "📝 Шаблон сообщения",
        "edit_dest": "📍 Куда отправлять",
        "edit_delete_old": "🗑 Удалять старые",
        "edit_link_preview": "🔗 Превью ссылок",
        "edit_delay": "⏱ Задержка отправки",
        "edit_template_prompt": (
            "Отправьте новый шаблон для подписки #{sub_id}.\n\n"
            "Ключевые слова: <code>{{username}}</code>, <code>{{game}}</code>, <code>{{name}}</code>"
        ),
        "edit_updated": "✅ Подписка #{sub_id} обновлена.",
        "edit_delay_prompt": (
            "Подписка #{sub_id}\n"
            "Текущая задержка: {current}\n\n"
            "Укажите задержку в минутах (0 — отправлять сразу):"
        ),
        "edit_delay_current_none": "нет (сразу)",
        "edit_delay_current": "{minutes} мин.",
        "edit_delay_invalid": "Введите 0 или положительное число минут.",
        "edit_delete_old_menu": (
            "Удалять старые сообщения при новом стриме?\n\n"
            "Telegram позволяет удалять только сообщения младше ~48 часов."
        ),
        "edit_preview_menu": "Отключить превью ссылок в уведомлениях?",
        "preview_yes": "✅ Выкл (без превью)",
        "preview_no": "❌ Вкл (с превью)",
        "render_maintenance": (
            "⚠️ Render: запланированы работы\n"
            "Бот может быть недоступен.\n\n"
            "{title}\n{body}\n\n"
            "{link}"
        ),
        "aiven_outage": (
            "⚠️ Aiven: сбой в работе\n"
            "Подписки могут быть недоступны.\n\n"
            "{title}\n{body}\n\n"
            "{link}"
        ),
        "conflict_polling": (
            "Конфликт polling — возможно, запущено два экземпляра бота "
            "(Render + локально?). Оставляем один."
        ),
        "unhandled_error": "Необработанная ошибка: {err}",
        "broadcast_prompt": (
            "Отправьте сообщение для рассылки всем пользователям.\n"
            "/cancel — отмена."
        ),
        "broadcast_empty": "Сообщение не может быть пустым.",
        "broadcast_done": (
            "Рассылка завершена.\n"
            "Доставлено: {sent}\n"
            "Ошибок: {failed}\n"
            "Всего получателей: {total}"
        ),
        "bot_stats": (
            "📊 Статистика бота\n\n"
            "Пользователей: {users}\n"
            "Получателей (users + owners): {notify_users}\n"
            "Подписок: {subscriptions_total} "
            "(✅ {subscriptions_enabled} / ⏸ {subscriptions_disabled})\n"
            "Уникальных владельцев: {unique_owners}\n"
            "Каналов Twitch: {unique_twitch_channels}\n\n"
            "Языки:\n"
            "• English: {locale_en}\n"
            "• Русский: {locale_ru}\n"
            "• Не выбран: {locale_unset}"
        ),
    },
}


def t(key: str, lang: str, **kwargs: object) -> str:
    locale = lang if lang in SUPPORTED_LOCALES else DEFAULT_LOCALE
    text = _STRINGS[locale].get(key) or _STRINGS[DEFAULT_LOCALE][key]
    return text.format(**kwargs) if kwargs else text


def btn(key: str, lang: str) -> str:
    return t(f"btn_{key}", lang)


def all_btn_texts(key: str) -> set[str]:
    return {btn(key, loc) for loc in SUPPORTED_LOCALES}


def all_menu_buttons() -> set[str]:
    keys = (
        "new",
        "manage",
        "list",
        "edit",
        "delete",
        "feedback",
        "admin",
        "broadcast",
        "stats",
        "back",
    )
    return {btn(k, loc) for k in keys for loc in SUPPORTED_LOCALES}


def main_menu(lang: str, *, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(btn("new", lang))],
        [KeyboardButton(btn("manage", lang))],
        [KeyboardButton(btn("feedback", lang))],
    ]
    if is_admin:
        rows.append([KeyboardButton(btn("admin", lang))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def subscriptions_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(btn("list", lang)),
                KeyboardButton(btn("edit", lang)),
            ],
            [KeyboardButton(btn("delete", lang))],
            [KeyboardButton(btn("back", lang))],
        ],
        resize_keyboard=True,
    )


def admin_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(btn("broadcast", lang)),
                KeyboardButton(btn("stats", lang)),
            ],
            [KeyboardButton(btn("back", lang))],
        ],
        resize_keyboard=True,
    )


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("English", callback_data="lang:en")],
            [InlineKeyboardButton("Русский", callback_data="lang:ru")],
        ]
    )


def dest_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("dest_dm", lang), callback_data="dest:dm")],
            [InlineKeyboardButton(t("dest_channel", lang), callback_data="dest:channel")],
            [InlineKeyboardButton(t("dest_group", lang), callback_data="dest:group")],
        ]
    )


def delete_old_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delete_old_yes", lang), callback_data="delete_old:1")],
            [InlineKeyboardButton(t("delete_old_no", lang), callback_data="delete_old:0")],
        ]
    )


def link_preview_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("link_preview_on", lang), callback_data="link_preview:0")],
            [InlineKeyboardButton(t("link_preview_off", lang), callback_data="link_preview:1")],
        ]
    )


def delay_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delay_no", lang), callback_data="delay_send:0")],
            [InlineKeyboardButton(t("delay_yes", lang), callback_data="delay_send:1")],
        ]
    )


def edit_options_keyboard(sub_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("edit_template", lang), callback_data=f"edit_f:{sub_id}:template")],
            [InlineKeyboardButton(t("edit_dest", lang), callback_data=f"edit_f:{sub_id}:dest")],
            [InlineKeyboardButton(t("edit_delay", lang), callback_data=f"edit_f:{sub_id}:delay")],
            [InlineKeyboardButton(t("edit_delete_old", lang), callback_data=f"edit_f:{sub_id}:delete_old")],
            [InlineKeyboardButton(t("edit_link_preview", lang), callback_data=f"edit_f:{sub_id}:preview")],
        ]
    )


def edit_bool_keyboard(sub_id: int, field: str, lang: str) -> InlineKeyboardMarkup:
    if field == "preview":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("preview_yes", lang), callback_data=f"edit_set:{sub_id}:preview:1")],
                [InlineKeyboardButton(t("preview_no", lang), callback_data=f"edit_set:{sub_id}:preview:0")],
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
