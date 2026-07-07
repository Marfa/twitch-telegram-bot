from __future__ import annotations

import asyncio
import logging
import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MessageOriginChannel,
    MessageOriginChat,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, Conflict, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from db import Database, Subscription
from links import TelegramTopicLink, chat_ref_to_id, parse_telegram_topic_link
from twitch import TwitchClient, render_template

logger = logging.getLogger(__name__)

CHANNEL, TEMPLATE, DEST_TYPE, DEST_CHAT = range(4)

BTN_NEW = "➕ Новая подписка"
BTN_LIST = "📋 Мои подписки"
BTN_DELETE = "🗑 Удалить подписку"
BTN_FEEDBACK = "🐛 Сообщить о проблеме"
MENU_BUTTONS = {BTN_NEW, BTN_LIST, BTN_DELETE, BTN_FEEDBACK}

GITHUB_ISSUES_URL = "https://github.com/Marfa/twitch-telegram-bot/issues"
FEEDBACK_TEXT = (
    "Обратная связь:\n"
    "• Telegram: @immarfa\n"
    f"• GitHub Issues: {GITHUB_ISSUES_URL}"
)

HELP_TEXT = (
    "Доступные команды:\n"
    "/start — настроить подписку на стрим\n"
    "/help — показать эту справку\n"
    "/cancel — отменить текущую настройку\n\n"
    "Кнопки меню:\n"
    f"• {BTN_NEW}\n"
    f"• {BTN_LIST}\n"
    f"• {BTN_DELETE}\n"
    f"• {BTN_FEEDBACK}"
)

DEST_LABELS = {
    "dm": "личку",
    "channel": "канал",
    "group": "группу или сообщество",
}

GROUP_SETUP_TEXT = (
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
)

MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_NEW)],
        [KeyboardButton(BTN_LIST), KeyboardButton(BTN_DELETE)],
        [KeyboardButton(BTN_FEEDBACK)],
    ],
    resize_keyboard=True,
)


def _dest_label(dest_type: str) -> str:
    return DEST_LABELS.get(dest_type, dest_type)


def _format_sub_line(sub: Subscription) -> str:
    status = "✅" if sub.enabled else "⏸"
    thread = f", тема {sub.thread_id}" if sub.thread_id else ""
    return (
        f"{status} #{sub.id} — {sub.twitch_username}\n"
        f"   → {_dest_label(sub.dest_type)} ({sub.chat_id}{thread})"
    )


async def _send_test(bot, chat_id: int, thread_id: int | None, text: str) -> bool:
    kwargs: dict = {"chat_id": chat_id, "text": text}
    if thread_id:
        kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_message(**kwargs)
        return True
    except (BadRequest, Forbidden) as exc:
        logger.warning("Cannot send to %s: %s", chat_id, exc)
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Привет! Я присылаю уведомления о старте стримов на Twitch.\n\n"
        "Справка по командам: /help\n\n"
        "Укажите канал Twitch: ссылку, мобильную ссылку или username.",
        reply_markup=MAIN_MENU,
    )
    return CHANNEL


async def start_new_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Укажите канал Twitch: ссылку, мобильную ссылку или username.",
        reply_markup=MAIN_MENU,
    )
    return CHANNEL


async def receive_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.effective_message.text or ""
    if text in MENU_BUTTONS:
        await update.effective_message.reply_text(
            "Сначала завершите настройку подписки или нажмите /cancel."
        )
        return CHANNEL

    twitch: TwitchClient = context.application.bot_data["twitch"]
    username = twitch.parse_username(text)
    if not username:
        await update.effective_message.reply_text(
            "Не удалось распознать канал. Примеры:\n"
            "• ninja\n"
            "• https://twitch.tv/ninja\n"
            "• https://m.twitch.tv/ninja"
        )
        return CHANNEL

    user = await asyncio.to_thread(twitch.get_user, username)
    if not user:
        await update.effective_message.reply_text(
            f"Канал «{username}» не найден на Twitch. Попробуйте ещё раз."
        )
        return CHANNEL

    context.user_data["twitch_username"] = user["login"]
    context.user_data["twitch_user_id"] = user["id"]
    await update.effective_message.reply_text(
        f"Канал: {user['display_name']}\n\n"
        "Задайте формат сообщения. Доступные ключевые слова:\n"
        "• {username} — имя канала\n"
        "• {game} — категория стрима\n"
        "• {name} — название стрима\n\n"
        "Пример:\n"
        "{username} в эфире!\n"
        "{name}\n"
        "Категория: {game}"
    )
    return TEMPLATE


async def receive_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    template = (update.effective_message.text or "").strip()
    if template in MENU_BUTTONS:
        await update.effective_message.reply_text(
            "Сначала завершите настройку подписки или нажмите /cancel."
        )
        return TEMPLATE
    if not template:
        await update.effective_message.reply_text("Шаблон не может быть пустым.")
        return TEMPLATE

    context.user_data["message_template"] = template
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📩 В личку", callback_data="dest:dm")],
            [InlineKeyboardButton("📢 В канал", callback_data="dest:channel")],
            [InlineKeyboardButton("💬 В группу или сообщество", callback_data="dest:group")],
        ]
    )
    await update.effective_message.reply_text(
        "Куда отправлять уведомления?",
        reply_markup=keyboard,
    )
    return DEST_TYPE


async def receive_dest_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    dest_type = query.data.split(":", 1)[1]
    context.user_data["dest_type"] = dest_type

    if dest_type == "dm":
        return await _finish_subscription(update, context, query.from_user.id, query.from_user.id, None)

    if dest_type == "channel":
        await query.edit_message_text(
            "Добавьте бота в канал как администратора с правом публикации.\n\n"
            "Затем отправьте @username канала или перешлите сообщение из канала."
        )
        return DEST_CHAT

    await query.edit_message_text(GROUP_SETUP_TEXT)
    return DEST_CHAT


async def _resolve_chat_ref(bot, ref: str) -> int:
    numeric = chat_ref_to_id(ref)
    if numeric is not None:
        return numeric
    chat = await bot.get_chat(f"@{ref.lstrip('@')}")
    return chat.id


async def _resolve_from_topic_link(bot, link: TelegramTopicLink) -> tuple[int, int]:
    chat_id = await _resolve_chat_ref(bot, link.chat_ref)
    return chat_id, link.thread_id


def _extract_forward_chat(message) -> tuple[int | None, int | None]:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.id, None
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.id, message.message_thread_id or None
    if message.forward_from_chat:
        return message.forward_from_chat.id, message.message_thread_id or None
    return None, None


async def _parse_dest_input(
    bot,
    message,
    dest_type: str,
) -> tuple[int | None, int | None, str | None]:
    text = (message.text or message.caption or "").strip()

    topic = parse_telegram_topic_link(text)
    if topic:
        try:
            chat_id, thread_id = await _resolve_from_topic_link(bot, topic)
            return chat_id, thread_id, None
        except BadRequest:
            return None, None, "Группа не найдена. Добавьте бота и проверьте ссылку."

    fwd_chat, fwd_thread = _extract_forward_chat(message)
    if fwd_chat is not None:
        return fwd_chat, fwd_thread, None

    if text.startswith("@"):
        try:
            chat = await bot.get_chat(text)
            return chat.id, None, None
        except BadRequest:
            label = "Канал" if dest_type == "channel" else "Группа"
            return None, None, f"{label} не найдена. Проверьте @username."

    if text and re.fullmatch(r"-?\d+", text):
        return int(text), None, None

    if message.forward_origin or message.forward_from_chat:
        return (
            None,
            None,
            "Пересылка из лички не подходит. Нужно «Переслано из: Название группы» "
            "или ссылка на тему: https://t.me/c/название/30",
        )

    hint = (
        "Отправьте ссылку на тему, @username, ID группы "
        "или перешлите сообщение из группы."
        if dest_type == "group"
        else "Отправьте @username канала, ID или перешлите сообщение из канала."
    )
    return None, None, hint


async def receive_dest_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    dest_type = context.user_data.get("dest_type", "")

    chat_id, thread_id, error = await _parse_dest_input(context.bot, message, dest_type)
    if error:
        await message.reply_text(error)
        return DEST_CHAT
    if chat_id is None:
        await message.reply_text("Не удалось определить чат. Попробуйте ещё раз.")
        return DEST_CHAT

    if dest_type == "channel":
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type != ChatType.CHANNEL:
                await message.reply_text("Это не канал. Укажите канал или перешлите из канала.")
                return DEST_CHAT
        except BadRequest:
            await message.reply_text("Бот не видит этот канал. Добавьте бота как администратора.")
            return DEST_CHAT

    if dest_type == "group":
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
                await message.reply_text("Это не группа или сообщество.")
                return DEST_CHAT
        except BadRequest:
            await message.reply_text("Бот не видит эту группу. Добавьте бота в группу.")
            return DEST_CHAT

    ok = await _send_test(
        context.bot,
        chat_id,
        thread_id,
        "✅ Тест: бот может отправлять уведомления сюда.",
    )
    if not ok:
        await message.reply_text(
            "Не удалось отправить тестовое сообщение. "
            "Проверьте права бота и попробуйте снова."
        )
        return DEST_CHAT

    return await _finish_subscription(update, context, update.effective_user.id, chat_id, thread_id)


async def _finish_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_id: int,
    chat_id: int,
    thread_id: int | None,
) -> int:
    db: Database = context.application.bot_data["db"]
    data = dict(context.user_data)

    try:
        sub_id = db.add_subscription(
            owner_id=owner_id,
            twitch_username=data["twitch_username"],
            twitch_user_id=data["twitch_user_id"],
            message_template=data["message_template"],
            dest_type=data["dest_type"],
            chat_id=chat_id,
            thread_id=thread_id,
        )
    except Exception:
        logger.exception("Failed to save subscription for owner %s", owner_id)
        await context.bot.send_message(
            owner_id,
            "Не удалось сохранить подписку. Попробуйте ещё раз: /start",
            reply_markup=MAIN_MENU,
        )
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data.clear()

    preview = render_template(
        data["message_template"],
        data["twitch_username"],
        "Just Chatting",
        "Тестовый стрим",
    )
    thread_note = f"\nТема: {thread_id}" if thread_id else ""
    text = (
        f"✅ Настройка завершена!\n\n"
        f"Подписка #{sub_id} создана.\n"
        f"Канал Twitch: {data['twitch_username']}\n"
        f"Уведомления: {_dest_label(data['dest_type'])}{thread_note}\n\n"
        f"Пример сообщения:\n{preview}\n\n"
        f"Когда {data['twitch_username']} начнёт стрим — пришлю уведомление.\n"
        f"Справка: /help"
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text("✅ Подписка создана.")
        except BadRequest:
            pass

    await context.bot.send_message(owner_id, text, reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.effective_message.reply_text("Отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def report_problem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        FEEDBACK_TEXT,
        reply_markup=MAIN_MENU,
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, reply_markup=MAIN_MENU)


async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    db: Database = context.application.bot_data["db"]
    subs = db.get_subscriptions_by_owner(update.effective_user.id)
    if not subs:
        await update.effective_message.reply_text("Подписок пока нет.", reply_markup=MAIN_MENU)
        return

    lines = [_format_sub_line(s) for s in subs]
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'⏸ Выкл' if s.enabled else '✅ Вкл'} #{s.id} {s.twitch_username}",
                callback_data=f"toggle:{s.id}",
            )
        ]
        for s in subs
    ]
    await update.effective_message.reply_text(
        "Ваши подписки (нажмите, чтобы включить/выключить):\n\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    db: Database = context.application.bot_data["db"]
    subs = db.get_subscriptions_by_owner(update.effective_user.id)
    if not subs:
        await update.effective_message.reply_text("Подписок нет.", reply_markup=MAIN_MENU)
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑 #{s.id} {s.twitch_username}", callback_data=f"delete:{s.id}")]
        for s in subs
    ]
    await update.effective_message.reply_text(
        "Выберите подписку для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    new_state = db.toggle_subscription(sub_id, query.from_user.id)
    if new_state is None:
        await query.edit_message_text("Подписка не найдена.")
        return
    label = "включена" if new_state else "выключена"
    await query.edit_message_text(f"Подписка #{sub_id} {label}.")


async def on_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    sub_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    if db.delete_subscription(sub_id, query.from_user.id):
        await query.edit_message_text(f"Подписка #{sub_id} удалена.")
    else:
        await query.edit_message_text("Подписка не найдена.")


async def check_streams(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    twitch: TwitchClient = context.application.bot_data["twitch"]
    last_live: dict[str, bool] = context.application.bot_data.setdefault("last_live", {})

    user_ids = db.get_unique_twitch_user_ids()
    if not user_ids:
        return

    try:
        live_streams = await asyncio.to_thread(twitch.get_live_streams, user_ids)
    except Exception:
        logger.exception("Twitch poll failed")
        return

    for uid in user_ids:
        is_live = uid in live_streams
        was_live = last_live.get(uid, False)
        if is_live and not was_live:
            stream = live_streams[uid]
            username = stream.get("user_login", stream.get("user_name", ""))
            game = stream.get("game_name", "")
            title = stream.get("title", "")
            for sub in db.get_enabled_by_twitch_user_id(uid):
                text = render_template(sub.message_template, username, game, title)
                await _send_test(context.bot, sub.chat_id, sub.thread_id, text)
        last_live[uid] = is_live


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logger.warning(
            "Конфликт polling — возможно, запущено два экземпляра бота "
            "(Render + локально?). Оставляем один."
        )
        return
    logger.exception("Необработанная ошибка: %s", err)


def build_application(token: str, db: Database, twitch: TwitchClient) -> Application:
    app = Application.builder().token(token).build()
    app.bot_data["db"] = db
    app.bot_data["twitch"] = twitch
    app.bot_data["last_live"] = {}
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("help", help_command), group=0)
    app.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_FEEDBACK)}$"), report_problem),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_LIST)}$"), list_subscriptions),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(BTN_DELETE)}$"), delete_menu),
        group=0,
    )
    app.add_handler(CallbackQueryHandler(on_toggle, pattern=r"^toggle:"), group=0)
    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^delete:"), group=0)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(f"^{re.escape(BTN_NEW)}$"), start_new_subscription),
        ],
        states={
            CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel)],
            TEMPLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_template)],
            DEST_TYPE: [CallbackQueryHandler(receive_dest_type, pattern=r"^dest:")],
            DEST_CHAT: [MessageHandler(filters.TEXT | filters.FORWARDED, receive_dest_chat)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("help", help_command),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv, group=1)

    from config import CHECK_INTERVAL

    app.job_queue.run_repeating(check_streams, interval=CHECK_INTERVAL, first=10)
    return app
