from __future__ import annotations

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

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "btn_new": "➕ New subscription",
        "btn_manage": "📋 Manage subscriptions",
        "btn_list": "📋 My subscriptions",
        "btn_edit": "✏️ Edit subscription",
        "btn_delete": "🗑 Delete subscription",
        "btn_feedback": "🐛 Report a problem",
        "btn_create_schedule": "📅 Create schedule",
        "btn_settings": "⚙️ Settings",
        "btn_language": "🌐 Language",
        "btn_admin": "⚙️ Admin",
        "btn_broadcast": "📣 Broadcast",
        "btn_broadcast_new": "➕ New broadcast",
        "btn_scheduled_broadcasts": "📅 Scheduled messages",
        "btn_stats": "📊 Statistics",
        "btn_back": "◀️ Main menu",
        "btn_wizard_back": "« Back",
        "btn_wizard_cancel": "Cancel",
        "btn_sys_notifications": "🔔 System notifications",
        "btn_sys_updates": "📬 Bot update alerts",
        "menu_subs": "Manage subscriptions:",
        "menu_settings": "Settings:",
        "menu_admin": "Admin panel:",
        "menu_broadcast": "Broadcast:",
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
        "stream_schedule_intro": (
            "Use this menu to build text for publishing your weekly schedule, starting on Monday.\n\n"
            "<b>Example:</b>\n"
            "- 13 July 15:30 Sovereign Syndicate\n"
            "- 14 July 15:30 Sovereign Syndicate\n"
            "- 15 July 15:30 Sovereign Syndicate\n"
            "- 17 July 15:30 Sovereign Syndicate"
        ),
        "stream_schedule_confirm": "Create the schedule?",
        "stream_schedule_yes": "✅ Yes",
        "stream_schedule_no": "❌ No",
        "stream_schedule_game_prompt": "What do you want to stream on {date}?",
        "stream_schedule_time_prompt": "Enter the planned stream start time in 15:30 format.",
        "stream_schedule_time_invalid": "Enter time in HH:MM format, e.g. 15:30.",
        "stream_schedule_game_empty": "Enter the stream title or game name.",
        "stream_schedule_no_stream": "No stream planned",
        "stream_schedule_finish": "Finish schedule",
        "stream_schedule_line": "- {date} {time} {game}",
        "channel_not_parsed": (
            "Could not parse the channel. Examples:\n"
            "• marfapr\n"
            "• https://www.twitch.tv/marfapr\n"
            "• https://m.twitch.tv/marfapr"
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
        "ignore_keywords_prompt": (
            "<b>Ignore keywords</b>\n\n"
            "Specify keywords in the stream title or category that will prevent "
            "the notification from being sent.\n\n"
            "If multiple words, separate them with commas.\n\n"
            "Send the list or tap Skip."
        ),
        "ignore_keywords_skip": "Skip ⏭",
        "ignore_keywords_yes_note": "Ignore keywords: {keywords}",
        "ignore_keywords_no_note": "Ignore keywords: none",
        "link_preview_prompt": "Show link preview in notifications?",
        "link_preview_on": "✅ Show preview",
        "link_preview_off": "❌ Hide preview",
        "delay_prompt": "Delay notification after stream start?",
        "delay_no": "❌ No",
        "delay_yes": "✅ Yes",
        "delay_minutes_prompt": "Enter the delay in minutes (a number):",
        "delay_minutes_invalid": "Enter a positive number of minutes, e.g. 5.",
        "repeat_prompt": "If the stream is interrupted, repeat notifications will not be sent.",
        "repeat_yes": "✅ Yes, allow repeats",
        "repeat_no": "❌ No",
        "repeat_mute_prompt": "Enter how many minutes to suppress repeat notifications:",
        "repeat_mute_invalid": "Enter a positive number of minutes, e.g. 30.",
        "repeat_yes_note": "Repeat notifications: yes",
        "repeat_no_note": "Suppress repeats: {minutes} min after first alert",
        "sub_list_dest": "• Destination: {dest} ({chat_id})",
        "sub_list_thread": "• Topic: {thread_id}",
        "sub_list_delete_yes": "• Delete old messages: yes",
        "sub_list_delete_no": "• Delete old messages: no",
        "sub_list_delete_fail": "• Notify on delete failure: yes",
        "sub_list_preview_on": "• Link preview: on",
        "sub_list_preview_off": "• Link preview: off",
        "sub_list_delay": "• Delayed send: {minutes} min",
        "sub_list_delay_none": "• Delayed send: no",
        "sub_list_repeat_allow": "• Repeat notifications: allowed",
        "sub_list_repeat_mute": "• Repeat notifications: suppress {minutes} min",
        "sub_list_ignore_yes": "• Ignore keywords: {keywords}",
        "sub_list_ignore_no": "• Ignore keywords: none",
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
        "delete_fail_notify_text": (
            "Notify about problems deleting the message?"
        ),
        "delete_fail_yes": "✅ Yes",
        "delete_fail_no": "❌ No",
        "delete_fail_notice": (
            "Could not delete the previous notification:\n{link}"
        ),
        "delete_fail_yes_note": "Notify on delete failure: yes",
        "delete_fail_no_note": "Notify on delete failure: no",
        "weekly_new_users": "New users: {count}",
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
        "dest_not_admin": (
            "You must be an admin of that channel/group to bind notifications there."
        ),
        "sub_limit": (
            "Subscription limit reached ({limit}). Delete an existing one first."
        ),
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
            "{delete_note}{delete_fail_note}\n"
            "{preview_note}\n"
            "{ignore_keywords_note}\n"
            "{delay_note}\n"
            "{repeat_note}\n\n"
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
            "• Telegram Tribute: https://t.me/tribute/app?startapp=dBlc\n"
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
        "sub_not_found": "Subscription not found.",
        "sub_enabled": "Subscription #{sub_id} enabled.",
        "sub_disabled": "Subscription #{sub_id} disabled.",
        "no_subs_short": "No subscriptions.",
        "delete_pick": "Choose a subscription to delete:",
        "sub_deleted": "Subscription #{sub_id} deleted.",
        "edit_pick": "Choose a subscription to edit:",
        "edit_menu": "Subscription #{sub_id} — {username}\n\nWhat to change?",
        "edit_template": "📝 Message template",
        "edit_ignore_keywords": "🚫 Ignore keywords",
        "edit_dest": "📍 Destination",
        "edit_delete_old": "🗑 Delete old messages",
        "edit_delete_fail_notify": "⚠️ Notify on delete failure",
        "edit_link_preview": "🔗 Link preview",
        "edit_delay": "⏱ Delay send",
        "edit_repeat": "🔁 Repeat notifications",
        "edit_repeat_menu": "If the stream is interrupted, repeat notifications will not be sent.",
        "edit_repeat_mute_prompt": (
            "Subscription #{sub_id}\n"
            "Current: {current}\n\n"
            "Enter suppression minutes (0 — allow repeats):"
        ),
        "edit_repeat_current_allow": "allowed",
        "edit_repeat_current_mute": "suppress {minutes} min",
        "edit_repeat_invalid": "Enter 0 or a positive number of minutes.",
        "edit_template_prompt": (
            "Send a new message template for subscription #{sub_id}.\n\n"
            "Placeholders: <code>{{username}}</code>, <code>{{game}}</code>, <code>{{name}}</code>"
        ),
        "edit_ignore_keywords_prompt": (
            "Subscription #{sub_id}\n"
            "Current: {current}\n\n"
            "Send keywords separated by commas.\n"
            "Empty message or Skip — disable the filter."
        ),
        "ignore_keywords_current_none": "none",
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
        "edit_delete_fail_menu": "Notify about problems deleting the message?",
        "edit_preview_menu": "Disable link preview in notifications?",
        "preview_yes": "✅ Off (no preview)",
        "preview_no": "❌ On (show preview)",
        "conflict_polling": (
            "Polling conflict — two bot instances may be running. Keep only one."
        ),
        "unhandled_error": "Unhandled error: {err}",
        "broadcast_prompt": (
            "Choose notification type:"
        ),
        "broadcast_type_bot_update": "📬 Bot update notifications",
        "broadcast_type_availability": "📡 Bot availability alerts",
        "broadcast_text_prompt": (
            "Send the message text (bold/italic and line breaks are kept).\n"
            "It will be auto-translated to each recipient's language.\n"
            "/cancel — abort."
        ),
        "broadcast_empty": "Message cannot be empty.",
        "broadcast_done": (
            "Broadcast complete.\n"
            "Sent: {sent}\n"
            "Blocked the bot: {blocked}\n"
            "Failed: {failed}\n"
            "Total recipients: {total}"
        ),
        "broadcast_scheduled": (
            "Message scheduled.\n"
            "Send time: {when}\n"
            "Recipients will receive it automatically."
        ),
        "broadcast_send_now": "Send now",
        "scheduled_list_title": "Scheduled messages:",
        "scheduled_empty": "No scheduled messages.",
        "scheduled_line": "#{id} — {when}\n{type}\n{preview}",
        "scheduled_edit_menu": "Message #{id}\n\nWhat to change?",
        "scheduled_edit_text": "✏️ Message text",
        "scheduled_edit_time": "🕐 Send time",
        "scheduled_edit_text_prompt": (
            "Send new message text for #{id}.\n"
            "/cancel — abort."
        ),
        "scheduled_edit_time_title": "Choose new send time for message #{id}:",
        "scheduled_updated": "✅ Message #{id} updated.",
        "scheduled_deleted": "✅ Message #{id} deleted.",
        "scheduled_not_found": "Scheduled message not found.",
        "scheduled_edit_btn": "✏️ #{id}",
        "scheduled_delete_btn": "🗑 #{id}",
        "schedule_title": "Choose send time (MSK, UTC+3):",
        "schedule_pick_hour": "——— Select hour ———",
        "schedule_pick_minutes": "Select minutes ↘",
        "schedule_saved_time": "Saved time ↘",
        "schedule_apply": "Apply time",
        "schedule_show_calendar": "🗓 Show calendar",
        "schedule_minutes_header": "——— Select minutes ———",
        "sys_notifications_menu": "System notifications:",
        "sys_updates_label": "Bot update notifications",
        "sys_availability_label": "Bot availability alerts",
        "bot_stats": (
            "📊 Bot statistics\n\n"
            "Users: {users}\n"
            "Created alerts: {unique_owners}\n"
            "Recipients: {notify_users}\n"
            "Subscriptions: {subscriptions_total} "
            "(✅ {subscriptions_enabled} / ⏸ {subscriptions_disabled})\n"
            "Twitch channels tracked: {unique_twitch_channels}\n\n"
            "System notifications:\n"
            "• Bot updates: {sys_updates}\n"
            "• Bot availability: {sys_availability}\n"
            "• Blocked the bot: {blocked_users}\n\n"
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
        "btn_create_schedule": "📅 Создать расписание",
        "btn_settings": "⚙️ Настройки",
        "btn_language": "🌐 Выбор языка",
        "btn_admin": "⚙️ Админка",
        "btn_broadcast": "📣 Рассылка",
        "btn_broadcast_new": "➕ Новая рассылка",
        "btn_scheduled_broadcasts": "📅 Запланированные сообщения",
        "btn_stats": "📊 Статистика",
        "btn_back": "◀️ Главное меню",
        "btn_wizard_back": "« Назад",
        "btn_wizard_cancel": "Отмена",
        "btn_sys_notifications": "🔔 Настройка системных уведомлений",
        "btn_sys_updates": "📬 Получение оповещений об обновлениях",
        "menu_subs": "Управление подписками:",
        "menu_settings": "Настройки:",
        "menu_admin": "Админка:",
        "menu_broadcast": "Рассылка:",
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
        "stream_schedule_intro": (
            "С помощью этого меню вы можете сформировать текст для публикации "
            "вашего расписания на неделю, начиная с понедельника.\n\n"
            "<b>Пример:</b>\n"
            "- 13 июля 15:30 Sovereign Syndicate\n"
            "- 14 июля 15:30 Sovereign Syndicate\n"
            "- 15 июля 15:30 Sovereign Syndicate\n"
            "- 17 июля 15:30 Sovereign Syndicate"
        ),
        "stream_schedule_confirm": "Сформировать расписание?",
        "stream_schedule_yes": "✅ Да",
        "stream_schedule_no": "❌ Нет",
        "stream_schedule_game_prompt": "Что вы хотите стримить в указание даты?\n\n{date}",
        "stream_schedule_time_prompt": "Укажите планируемое время старта стрима в формате 15:30.",
        "stream_schedule_time_invalid": "Укажите время в формате ЧЧ:ММ, например 15:30.",
        "stream_schedule_game_empty": "Введите название игры или стрима.",
        "stream_schedule_no_stream": "Стрим не планируется",
        "stream_schedule_finish": "Завершить создание расписания",
        "stream_schedule_line": "- {date} {time} {game}",
        "channel_not_parsed": (
            "Не удалось распознать канал. Примеры:\n"
            "• marfapr\n"
            "• https://www.twitch.tv/marfapr\n"
            "• https://m.twitch.tv/marfapr"
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
        "ignore_keywords_prompt": (
            "<b>Игнорировать ключевые слова</b>\n\n"
            "Укажите ключевые слова в названии стрима или игре, при наличии которых "
            "оповещение не будет отправляться.\n\n"
            "Если несколько слов, укажите их через запятую.\n\n"
            "Отправьте список слов или нажмите «Пропустить»."
        ),
        "ignore_keywords_skip": "Пропустить ⏭",
        "ignore_keywords_yes_note": "Игнорировать ключевые слова: {keywords}",
        "ignore_keywords_no_note": "Игнорировать ключевые слова: нет",
        "link_preview_prompt": "Показывать превью ссылок в уведомлениях?",
        "link_preview_on": "✅ Показывать превью",
        "link_preview_off": "❌ Скрыть превью",
        "delay_prompt": "Отложить отправку уведомления после начала стрима",
        "delay_no": "❌ Нет",
        "delay_yes": "✅ Да",
        "delay_minutes_prompt": "Укажите задержку отправки в минутах (число):",
        "delay_minutes_invalid": "Введите положительное число минут, например 5.",
        "repeat_prompt": "Если стрим прервался, повторные уведомления не будут отправляться",
        "repeat_yes": "✅ Да, разрешить повторы",
        "repeat_no": "❌ Нет",
        "repeat_mute_prompt": "Укажите в минутах, на сколько заглушить уведомления:",
        "repeat_mute_invalid": "Введите положительное число минут, например 30.",
        "repeat_yes_note": "Повторные уведомления: да",
        "repeat_no_note": "Заглушка повторов: {minutes} мин. после первого",
        "sub_list_dest": "• Куда: {dest} ({chat_id})",
        "sub_list_thread": "• Тема: {thread_id}",
        "sub_list_delete_yes": "• Удалять старые: да",
        "sub_list_delete_no": "• Удалять старые: нет",
        "sub_list_delete_fail": "• Сообщать о проблемах удаления: да",
        "sub_list_preview_on": "• Превью ссылок: включено",
        "sub_list_preview_off": "• Превью ссылок: выключено",
        "sub_list_delay": "• Отложенная отправка: {minutes} мин.",
        "sub_list_delay_none": "• Отложенная отправка: нет",
        "sub_list_repeat_allow": "• Повторные уведомления: разрешены",
        "sub_list_repeat_mute": "• Повторные уведомления: заглушка {minutes} мин.",
        "sub_list_ignore_yes": "• Игнорировать ключевые слова: {keywords}",
        "sub_list_ignore_no": "• Игнорировать ключевые слова: нет",
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
        "delete_fail_notify_text": (
            "Сообщать о проблемах при удалении сообщения?"
        ),
        "delete_fail_yes": "✅ Да",
        "delete_fail_no": "❌ Нет",
        "delete_fail_notice": (
            "Не удалось удалить предыдущее оповещение:\n{link}"
        ),
        "delete_fail_yes_note": "Сообщать о проблемах удаления: да",
        "delete_fail_no_note": "Сообщать о проблемах удаления: нет",
        "weekly_new_users": "Новых пользователей: {count}",
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
        "dest_not_admin": (
            "Чтобы привязать уведомления, вы должны быть администратором "
            "этого канала или группы."
        ),
        "sub_limit": (
            "Достигнут лимит подписок ({limit}). Сначала удалите одну из существующих."
        ),
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
            "{delete_note}{delete_fail_note}\n"
            "{preview_note}\n"
            "{ignore_keywords_note}\n"
            "{delay_note}\n"
            "{repeat_note}\n\n"
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
            "• Telegram Tribute: https://t.me/tribute/app?startapp=dBlc\n"
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
        "sub_not_found": "Подписка не найдена.",
        "sub_enabled": "Подписка #{sub_id} включена.",
        "sub_disabled": "Подписка #{sub_id} выключена.",
        "no_subs_short": "Подписок нет.",
        "delete_pick": "Выберите подписку для удаления:",
        "sub_deleted": "Подписка #{sub_id} удалена.",
        "edit_pick": "Выберите подписку для редактирования:",
        "edit_menu": "Подписка #{sub_id} — {username}\n\nЧто изменить?",
        "edit_template": "📝 Шаблон сообщения",
        "edit_ignore_keywords": "🚫 Игнорировать ключевые слова",
        "edit_dest": "📍 Куда отправлять",
        "edit_delete_old": "🗑 Удалять старые",
        "edit_delete_fail_notify": "⚠️ Сообщать о проблемах удаления",
        "edit_link_preview": "🔗 Превью ссылок",
        "edit_delay": "⏱ Задержка отправки",
        "edit_repeat": "🔁 Повторные уведомления",
        "edit_repeat_menu": "Если стрим прервался, повторные уведомления не будут отправляться",
        "edit_repeat_mute_prompt": (
            "Подписка #{sub_id}\n"
            "Сейчас: {current}\n\n"
            "Укажите минуты заглушки (0 — разрешить повторы):"
        ),
        "edit_repeat_current_allow": "разрешены",
        "edit_repeat_current_mute": "заглушка {minutes} мин.",
        "edit_repeat_invalid": "Введите 0 или положительное число минут.",
        "edit_template_prompt": (
            "Отправьте новый шаблон для подписки #{sub_id}.\n\n"
            "Ключевые слова: <code>{{username}}</code>, <code>{{game}}</code>, <code>{{name}}</code>"
        ),
        "edit_ignore_keywords_prompt": (
            "Подписка #{sub_id}\n"
            "Сейчас: {current}\n\n"
            "Отправьте ключевые слова через запятую.\n"
            "Пустое сообщение или «Пропустить» — отключить фильтр."
        ),
        "ignore_keywords_current_none": "нет",
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
        "edit_delete_fail_menu": "Сообщать о проблемах при удалении сообщения?",
        "edit_preview_menu": "Отключить превью ссылок в уведомлениях?",
        "preview_yes": "✅ Выкл (без превью)",
        "preview_no": "❌ Вкл (с превью)",
        "conflict_polling": (
            "Конфликт polling — возможно, запущено два экземпляра бота. Оставьте один."
        ),
        "unhandled_error": "Необработанная ошибка: {err}",
        "broadcast_prompt": "Выберите тип оповещения:",
        "broadcast_type_bot_update": "📬 Оповещения об обновлении бота",
        "broadcast_type_availability": "📡 Оповещения о доступности бота",
        "broadcast_text_prompt": (
            "Отправьте текст сообщения (жирный/курсив и переносы сохраняются).\n"
            "Оно будет автоматически переведено на язык каждого получателя.\n"
            "/cancel — отмена."
        ),
        "broadcast_empty": "Сообщение не может быть пустым.",
        "broadcast_done": (
            "Рассылка завершена.\n"
            "Доставлено: {sent}\n"
            "Заблокировали бота: {blocked}\n"
            "Ошибок: {failed}\n"
            "Всего получателей: {total}"
        ),
        "broadcast_scheduled": (
            "Сообщение запланировано.\n"
            "Время отправки: {when}\n"
            "Получатели получат его автоматически."
        ),
        "broadcast_send_now": "Отправить сейчас",
        "scheduled_list_title": "Запланированные сообщения:",
        "scheduled_empty": "Запланированных сообщений нет.",
        "scheduled_line": "#{id} — {when}\n{type}\n{preview}",
        "scheduled_edit_menu": "Сообщение #{id}\n\nЧто изменить?",
        "scheduled_edit_text": "✏️ Текст сообщения",
        "scheduled_edit_time": "🕐 Время отправки",
        "scheduled_edit_text_prompt": (
            "Отправьте новый текст для сообщения #{id}.\n"
            "/cancel — отмена."
        ),
        "scheduled_edit_time_title": "Выберите новое время отправки для сообщения #{id}:",
        "scheduled_updated": "✅ Сообщение #{id} обновлено.",
        "scheduled_deleted": "✅ Сообщение #{id} удалено.",
        "scheduled_not_found": "Запланированное сообщение не найдено.",
        "scheduled_edit_btn": "✏️ #{id}",
        "scheduled_delete_btn": "🗑 #{id}",
        "schedule_title": "Выберите время отправки (МСК, UTC+3):",
        "schedule_pick_hour": "——— Выберите час ———",
        "schedule_pick_minutes": "Выберите минуты ↘",
        "schedule_saved_time": "Запомненное время ↘",
        "schedule_apply": "Применить время",
        "schedule_show_calendar": "🗓 Показать календарь",
        "schedule_minutes_header": "——— Выберите минуты ———",
        "sys_notifications_menu": "Настройка системных уведомлений:",
        "sys_updates_label": "Оповещения об обновлении бота",
        "sys_availability_label": "Оповещения о доступности бота",
        "bot_stats": (
            "📊 Статистика бота\n\n"
            "Пользователей: {users}\n"
            "Создали оповещение: {unique_owners}\n"
            "Получателей: {notify_users}\n"
            "Подписок: {subscriptions_total} "
            "(✅ {subscriptions_enabled} / ⏸ {subscriptions_disabled})\n"
            "Каналов Twitch: {unique_twitch_channels}\n\n"
            "Системные оповещения:\n"
            "• Обновление бота: {sys_updates}\n"
            "• Доступность бота: {sys_availability}\n"
            "• Заблокировали бота: {blocked_users}\n\n"
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
        "create_schedule",
        "settings",
        "language",
        "admin",
        "broadcast",
        "broadcast_new",
        "scheduled_broadcasts",
        "stats",
        "back",
        "sys_notifications",
    )
    return {btn(k, loc) for k in keys for loc in SUPPORTED_LOCALES}


def all_wizard_nav_buttons() -> set[str]:
    return {btn(k, loc) for k in ("wizard_back", "wizard_cancel") for loc in SUPPORTED_LOCALES}


def main_menu(lang: str, *, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(btn("new", lang))],
        [KeyboardButton(btn("manage", lang))],
        [KeyboardButton(btn("create_schedule", lang))],
        [KeyboardButton(btn("settings", lang))],
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


def settings_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(btn("sys_notifications", lang))],
            [KeyboardButton(btn("language", lang))],
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


def broadcast_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(btn("broadcast_new", lang))],
            [KeyboardButton(btn("scheduled_broadcasts", lang))],
            [KeyboardButton(btn("back", lang))],
        ],
        resize_keyboard=True,
    )


def wizard_menu(lang: str, *, back: bool = True) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(btn("wizard_cancel", lang))]
    if back:
        row.insert(0, KeyboardButton(btn("wizard_back", lang)))
    return ReplyKeyboardMarkup([row], resize_keyboard=True)


def admin_wizard_menu(lang: str, *, back: bool = True) -> ReplyKeyboardMarkup:
    return wizard_menu(lang, back=back)


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


def delete_fail_notify_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delete_fail_yes", lang), callback_data="delete_fail:1")],
            [InlineKeyboardButton(t("delete_fail_no", lang), callback_data="delete_fail:0")],
        ]
    )


def link_preview_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("link_preview_on", lang), callback_data="link_preview:0")],
            [InlineKeyboardButton(t("link_preview_off", lang), callback_data="link_preview:1")],
        ]
    )


def ignore_keywords_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("ignore_keywords_skip", lang), callback_data="ignore_keywords:skip")],
        ]
    )


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
        ]
    )


def sys_notifications_keyboard(
    lang: str,
    *,
    updates_enabled: bool,
    availability_enabled: bool,
) -> InlineKeyboardMarkup:
    updates_mark = "✅ " if updates_enabled else "❌ "
    availability_mark = "✅ " if availability_enabled else "❌ "
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
        ]
    )


def stream_schedule_day_keyboard(
    lang: str, *, show_finish: bool, show_skip: bool = True
) -> InlineKeyboardMarkup | None:
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
    return InlineKeyboardMarkup(rows) if rows else None


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
        [InlineKeyboardButton(t("schedule_show_calendar", lang), callback_data=f"{prefix}:noop")]
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
    if page < 10:
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
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t("edit_template", lang), callback_data=f"edit_f:{sub_id}:template")],
        [InlineKeyboardButton(t("edit_ignore_keywords", lang), callback_data=f"edit_f:{sub_id}:ignore_keywords")],
        [InlineKeyboardButton(t("edit_link_preview", lang), callback_data=f"edit_f:{sub_id}:preview")],
        [InlineKeyboardButton(t("edit_delay", lang), callback_data=f"edit_f:{sub_id}:delay")],
        [InlineKeyboardButton(t("edit_repeat", lang), callback_data=f"edit_f:{sub_id}:repeat")],
        [InlineKeyboardButton(t("edit_dest", lang), callback_data=f"edit_f:{sub_id}:dest")],
    ]
    if dest_type != "dm":
        rows.append(
            [InlineKeyboardButton(t("edit_delete_old", lang), callback_data=f"edit_f:{sub_id}:delete_old")]
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
    return InlineKeyboardMarkup(rows)


def edit_bool_keyboard(sub_id: int, field: str, lang: str) -> InlineKeyboardMarkup:
    if field == "preview":
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(t("preview_yes", lang), callback_data=f"edit_set:{sub_id}:preview:1")],
                [InlineKeyboardButton(t("preview_no", lang), callback_data=f"edit_set:{sub_id}:preview:0")],
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
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delete_old_yes", lang), callback_data=f"edit_set:{sub_id}:delete_old:1")],
            [InlineKeyboardButton(t("delete_old_no", lang), callback_data=f"edit_set:{sub_id}:delete_old:0")],
        ]
    )


def dest_label(dest_type: str, lang: str) -> str:
    return t(f"dest_label_{dest_type}", lang)
