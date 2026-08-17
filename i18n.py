from __future__ import annotations

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

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "btn_new": "➕ New subscription",
        "btn_import_twitch": "⬇️ Import from Twitch",
        "btn_manage": "📋 Manage subscriptions",
        "btn_list": "📋 My subscriptions",
        "btn_edit": "✏️ Edit subscription",
        "btn_delete": "🗑 Delete subscription",
        "btn_feedback": "🐛 Report a problem",
        "btn_create_schedule": "📅 Create schedule",
        "btn_alert_history": "📜 Alert history",
        "btn_other": "📦 Other",
        "btn_settings": "⚙️ Settings",
        "btn_language": "🌐 Language",
        "btn_admin": "⚙️ Admin",
        "btn_demo": "🎬 Demo mode",
        "btn_broadcast": "📣 Broadcast",
        "btn_broadcast_new": "➕ New broadcast",
        "btn_scheduled_broadcasts": "📅 Scheduled messages",
        "btn_stats": "📊 Statistics",
        "btn_back": "◀️ Main menu",
        "btn_wizard_back": "« Back",
        "btn_wizard_cancel": "Cancel",
        "btn_sys_notifications": "🔔 System alerts",
        "btn_ignored_words": "🚫 Ignored words",
        "btn_whisper_alerts": "💬 Whisper alerts",
        "btn_advanced_mode": "🎛 Advanced mode",
        "btn_beta_mode": "🧪 Beta mode",
        "btn_sys_updates": "📬 Bot update alerts",
        "btn_sync_subs": "🔄 Subscription sync",
        "btn_premium": "⭐ Premium",
        "btn_premium_pay": "Pay with Stars",
        "btn_premium_trial": "Trial period",
        "btn_premium_trial_confirm": "Activate 7-day trial",
        "btn_premium_month": "Monthly — {stars} Stars",
        "btn_premium_year": "Yearly — {stars} Stars",
        "btn_premium_features": "Pay only for features you need",
        "btn_premium_lifetime": "Lifetime — {stars} Stars",
        "btn_premium_feat_pay": "Pay {stars} Stars / month",
        "btn_premium_feat_back": "Back",
        "btn_premium_marfapr": "Create marfapr alert",
        "btn_premium_cancel_stars": "Cancel subscription",
        "btn_premium_owned": "Purchased subscriptions",
        "btn_premium_cancel_feat": "Cancel",
        "btn_premium_get": "Get Premium",
        "btn_premium_skip": "Skip",
        "btn_premium_oferta": "Offer",
        "btn_partner": "🤝 Partner program",
        "btn_partner_stats": "📈 My stats",
        "btn_partner_link": "🔗 Get link",
        "btn_partner_withdraw": "💸 Request withdrawal",
        "btn_partner_withdrawals": "📋 My requests",
        "btn_back_settings": "◀️ Settings",
        "btn_admin_withdrawals": "💸 Withdrawals",
        "btn_watch": "🎲 What to watch?",
        "watch_cats_prompt": (
            "What to watch — step 1/5\n\n"
            "Type a Twitch category name (game or Just Chatting).\n"
            "You can add up to {max} categories."
        ),
        "watch_cats_added": (
            "Added: {name}\n"
            "Selected ({count}/{max}): {list}\n\n"
            "Type another category, or tap Done."
        ),
        "watch_cats_pick": "Pick a category:",
        "watch_cats_not_found": "No categories found for «{query}». Try another name.",
        "watch_cats_full": "Maximum {max} categories. Tap Done or remove one.",
        "watch_cats_need_one": "Add at least one category.",
        "watch_cats_done": "Done",
        "watch_cats_clear": "Clear list",
        "watch_cats_lucky": "🎲 I'm feeling lucky",
        "watch_lucky_searching": "Searching live streams…",
        "watch_lucky_empty": (
            "No live streams for random / recently released games. "
            "Try again or type a category."
        ),
        "watch_tags_prompt": (
            "What to watch — step 2/5\n\n"
            "Stream tags (optional).\n"
            "Send comma-separated tags; the stream must have <b>all</b> of them "
            "(e.g. <code>English, fps</code>).\n"
            "Or tap Skip."
        ),
        "watch_tags_skip": "Skip",
        "watch_tags_bad": "Send tags separated by commas, or tap Skip.",
        "watch_viewers_prompt": (
            "What to watch — step 3/5\n\n"
            "Viewer range:\n"
            "• <code>100-500</code> — min–max\n"
            "• <code>50</code> — at least 50\n"
            "• or tap Any"
        ),
        "watch_viewers_any": "Any",
        "watch_viewers_bad": "Send a number, a range like 100-500, or tap Any.",
        "watch_lang_prompt": (
            "What to watch — step 4/5\n\n"
            "Stream language (optional):"
        ),
        "watch_lang_any": "Any language",
        "watch_lang_ru": "Russian",
        "watch_lang_en": "English",
        "watch_lang_other": "Other code…",
        "watch_lang_other_prompt": "Send a 2-letter language code (e.g. de, fr, ja):",
        "watch_lang_bad": "Send a 2-letter code like de, or go Back.",
        "watch_mature_prompt": (
            "What to watch — step 5/5\n\n"
            "Exclude mature (18+) streams?"
        ),
        "watch_mature_exclude": "Exclude 18+",
        "watch_mature_allow": "Allow 18+",
        "watch_save_prompt": (
            "Save this as a filter for later? You can keep up to {max} filters.\n\n"
            "{summary}"
        ),
        "watch_save_yes": "Save filter",
        "watch_save_no": "Just this once",
        "watch_pick_prompt": (
            "Choose a saved filter or start a new search:"
        ),
        "watch_pick_new": "➕ New search",
        "watch_pick_delete_btn": "🗑 Delete filters",
        "watch_pick_empty": "No saved filters left. Starting a new search.",
        "watch_delete_pick": "Choose filters to delete (tap to select):",
        "watch_delete_go": "🗑 Delete selected ({count})",
        "watch_delete_clear": "Clear selection",
        "watch_delete_none": "Nothing selected.",
        "watch_delete_back": "« Back",
        "watch_deleted": "Deleted filters: {count}",
        "watch_suggest_header": "Here's what is live now:",
        "watch_suggest_item": (
            "{n}. <b>{display}</b> (@{login})\n"
            "{title}\n"
            "🎮 {game} · 👁 {viewers}\n"
            "https://twitch.tv/{login}"
        ),
        "watch_suggest_empty": (
            "No live streams match your filters right now.\n"
            "Try again later or change preferences."
        ),
        "watch_suggest_vod_header": (
            "No one is live right now. Recent VODs for your filters:"
        ),
        "watch_suggest_vod_item": (
            "{n}. <b>{display}</b> (@{login})\n"
            "{title}\n"
            "🎮 {game} · ⏱ {duration}\n"
            "{url}"
        ),
        "watch_suggest_error": "Could not fetch streams from Twitch. Try again later.",
        "watch_again": "Suggest again",
        "watch_change": "Filters / new search",
        "watch_create_alerts": "Watch new streams by this filter?",
        "watch_create_alerts_ok": (
            "Category alert created: <b>{name}</b>\n\n"
            "{summary}{paused_note}\n\n"
            "I'll notify you when new streams matching the filter go live."
        ),
        "watch_create_alerts_none": (
            "No filter to watch. Run «What to watch?» again first."
        ),
        "watch_create_alerts_dup": (
            "You already have a stream-start alert for this filter."
        ),
        "watch_create_alerts_paused_note": (
            "\n\nAlert created paused (free active limit: {free_limit})."
        ),
        "edit_watch_locked": (
            "This alert watches streams by category/filter and uses default settings.\n"
            "It cannot be edited — only deleted (Manage subscriptions → Delete)."
        ),
        "watch_prefs_summary": (
            "Filters: {cats}\n"
            "Viewers: {viewers}\n"
            "Language: {language}\n"
            "Tags: {tags}\n"
            "Mature: {mature}"
        ),
        "watch_viewers_label_any": "any",
        "watch_viewers_label_min": "from {min}",
        "watch_viewers_label_range": "{min}–{max}",
        "watch_lang_label_any": "any",
        "watch_tags_label_any": "any",
        "watch_mature_label_exclude": "excluded",
        "watch_mature_label_allow": "allowed",
        "menu_subs": "Manage subscriptions:",
        "menu_settings": "Settings:",
        "menu_other": "Other:",
        "alert_history_title": "Alert history — last {days} days ({n}):",
        "alert_history_empty": "No alerts yet.",
        "alert_history_day": "<b>📅 {date}</b>",
        "alert_history_line": "• {time} — <b>{username}</b>",
        "alert_history_go_stream": "Go to stream",
        "alert_history_body": "{text}",
        "alert_history_type_live": "Went live",
        "alert_history_type_end": "Stream ended",
        "alert_history_type_category": "Category change",
        "alert_history_type_schedule": "Schedule reminder",
        "btn_alert_history_more": "Show more",
        "menu_partner": "Partner program:",
        "partner_intro": (
            "Invite friends with your link. You get {percent}% of every Stars Premium "
            "payment they make.\n"
            "Withdrawal is manual; minimum {min_stars} Stars."
        ),
        "partner_stats": (
            "Invited: {invited}\n"
            "Their Stars payments: {payments}\n"
            "Available to withdraw: {available} Stars"
        ),
        "partner_link": "Your partner link:\n{link}",
        "partner_withdraw_ok": (
            "Withdrawal request #{id} for {amount} Stars sent to the admin."
        ),
        "partner_withdraw_min": (
            "Minimum withdrawal is {min_stars} Stars. Available: {available}."
        ),
        "partner_withdraw_admin": (
            "Partner withdrawal request #{id}\n"
            "User: <code>{user_id}</code>\n"
            "Amount: {amount} Stars"
        ),
        "partner_withdrawals_empty": "No withdrawal requests yet.",
        "partner_withdrawals_title": "Your withdrawal requests:",
        "partner_withdrawal_line": "#{id} — {amount} Stars — {status}",
        "partner_wd_status_pending": "pending",
        "partner_wd_status_paid": "paid",
        "partner_wd_status_rejected": "rejected",
        "partner_wd_paid_user": (
            "Your withdrawal request #{id} for {amount} Stars was marked as paid."
        ),
        "partner_wd_rejected_user": (
            "Your withdrawal request #{id} for {amount} Stars was rejected. "
            "The amount was returned to your balance."
        ),
        "admin_withdrawals_empty": "No pending withdrawal requests.",
        "admin_withdrawals_title": "Pending withdrawals:",
        "admin_withdrawal_line": "#{id} — user <code>{user_id}</code> — {amount} Stars",
        "btn_wd_paid": "✅ Paid",
        "btn_wd_reject": "❌ Reject",
        "admin_wd_resolved_paid": "Request #{id} marked as paid.",
        "admin_wd_resolved_rejected": "Request #{id} rejected, balance restored.",
        "admin_wd_already": "Request #{id} is already resolved ({status}).",
        "premium_title": (
            "⭐ Premium\n\n"
            "Benefits:\n"
            "• More than {free_limit} active alerts (inactive unlimited)\n"
            "• Alert types beyond live start (category / upcoming / stream end)\n"
            "• Twitch follow auto-sync\n"
            "• Ignore keywords (per alert + global list)\n"
            "• Delayed send\n"
            "• Repeat notification mute\n"
            "• Delete previous bot messages (+ delete-fail notify)\n"
            "• Publish schedule to Twitch\n"
            "• Alert history for 60 days (7 days on free)\n\n"
            "How to get:\n"
            "• Pay for a subscription (buttons below), or\n"
            "• Active Twitch subscription to https://www.twitch.tv/{channel}\n\n"
            "{status}"
        ),
        "premium_status_permanent": "Status: lifetime Premium.",
        "premium_status_trial": "Status: trial until {until}.",
        "premium_status_stars": "Status: monthly subscription until {until}.",
        "premium_status_stars_canceled": (
            "Status: monthly subscription until {until} (auto-renew off)."
        ),
        "premium_status_twitch": "Status: Twitch sub to {channel} verified.",
        "premium_status_features": "Status: unlocked features:\n{features}",
        "premium_status_none": "Status: free plan.",
        "premium_buy_after_current": (
            "<b>Purchase of new plans will be available after the current "
            "subscription ends.</b>"
        ),
        "premium_feat_extra_alerts": "More than {free_limit} active alerts",
        "premium_feat_alert_types": "Alert types beyond live start",
        "premium_feat_twitch_sync": "Twitch follow auto-sync",
        "premium_feat_advanced_mode": "Advanced mode",
        "premium_feat_ignore_keywords": "Ignore keywords",
        "premium_feat_delay": "Delayed send",
        "premium_feat_repeat": "Repeat notification mute",
        "premium_feat_delete_prev": "Delete previous messages",
        "premium_feat_schedule_publish": "Publish schedule to Twitch",
        "premium_feat_alert_history": "Alert history for 60 days",
        "premium_feat_line": "• {name} until {until}",
        "premium_feat_line_canceled": "• {name} until {until} (auto-renew off)",
        "premium_feat_pick": (
            "Select features ({price} Stars / month each).\n"
            "Only selected features unlock."
        ),
        "premium_gate": "⭐ This step requires Premium.\nGet Premium or {action}.",
        "premium_gate_feature": (
            "⭐ {feature} requires Premium.\nGet Premium or {action}."
        ),
        "premium_gate_action_skip": "skip this step",
        "premium_gate_action_cancel": "cancel",
        "premium_pay_title": "Bot Premium",
        "premium_pay_description": "Monthly Premium ({stars} Stars)",
        "premium_pay_year_title": "Bot Premium — 1 year",
        "premium_pay_year_description": "Yearly Premium ({stars} Stars)",
        "premium_pay_life_title": "Bot Premium — lifetime",
        "premium_pay_life_description": "Lifetime Premium ({stars} Stars)",
        "premium_pay_feat_title": "Bot Premium — features",
        "premium_pay_feat_description": "Selected features ({stars} Stars / month)",
        "premium_pay_done": "Premium activated. Thank you!",
        "premium_pay_link": "Open the invoice to pay with Stars:",
        "premium_trial_confirm": (
            "Activate a free {days}-day trial?\n\n"
            "Everything stays after the trial, but alerts are paused. "
            "You can delete live-start alerts; enabling anything needs Premium again.\n"
            "One trial per account."
        ),
        "premium_trial_started": "Trial activated until {until}.",
        "premium_trial_used": "Trial already used on this account.",
        "premium_trial_active": "Trial is already active until {until}.",
        "premium_trial_expired": (
            "Trial ended. Your alerts are paused. "
            "Live-start alerts can be deleted; enabling needs Premium."
        ),
        "premium_cancel_done": (
            "Subscription auto-renew canceled. Premium stays until the end of the paid period {until}."
        ),
        "premium_cancel_feat_done": (
            "Subscription auto-renew canceled. Premium stays until the end of the paid period {until}."
        ),
        "premium_cancel_none": "No active subscription to cancel.",
        "premium_cancel_failed": (
            "Could not cancel auto-renew via Telegram. Try again later or cancel in Telegram Settings → Stars."
        ),
        "premium_pay_failed": "Could not create the Stars invoice. Try again later.",
        "premium_owned_title": "Purchased subscriptions:\n{items}",
        "premium_owned_empty": "No purchased subscriptions.",
        "premium_owned_stars": "• Monthly subscription until {until}",
        "premium_owned_stars_canceled": (
            "• Monthly subscription until {until} (auto-renew off)"
        ),
        "premium_owned_feat": "• {name} until {until}",
        "premium_owned_feat_canceled": "• {name} until {until} (auto-renew off)",
        "premium_feat_owned": "Already purchased",
        "premium_plans_blocked": "A Premium plan is already active.",
        "premium_marfapr_need_sub": (
            "No active Twitch subscription to {channel} found.\n"
            "Subscribe at https://www.twitch.tv/{channel} and try again."
        ),
        "premium_marfapr_ok": (
            "Twitch subscription verified. Premium unlocked.\n"
            "Alert for {channel} is ready."
        ),
        "premium_marfapr_ok_exists": (
            "Twitch subscription verified. Premium unlocked.\n"
            "You already have an alert for {channel}."
        ),
        "premium_marfapr_oauth": (
            "Link Twitch to verify your subscription to {channel}:"
        ),
        "premium_active_limit": (
            "Free plan allows up to {limit} active alerts.\n"
            "Disable one or get Premium."
        ),
        "premium_created_disabled": (
            "Alert created as paused: free plan allows {limit} active alerts. "
            "Enable it after upgrading or pausing another."
        ),
        "premium_trial_paused_enable": (
            "This alert was paused after the trial. Get Premium to enable it again."
        ),
        "menu_admin": "Admin panel:",
        "demo_on": (
            "🎬 Demo mode ON.\n\n"
            "You see the bot as a regular free user (no Premium).\n"
            "Demo subscriptions are ready — try the menus.\n"
            "Everything created or changed here is discarded when you exit.\n"
            "Tap Demo mode again to exit."
        ),
        "demo_off": (
            "🎬 Demo mode OFF.\n\n"
            "Demo subscriptions removed. Your real subscriptions are unchanged."
        ),
        "demo_seed_template": (
            "{username} is live! (demo)\n"
            "{name}\n"
            "Category: {game}"
        ),
        "demo_seed_template_2": (
            "🔴 DEMO — {username}\n"
            "{game}"
        ),
        "menu_broadcast": "Broadcast:",
        "menu_main": "Main menu",
        "lang_pick": "Choose your language:",
        "lang_set": "Language set to English.",
        "start_welcome": (
            "Hi! I send notifications when Twitch streams go live.\n"
            "Commands help: /help\n"
            "Tap New subscription to add a new subscription."
        ),
        "new_sub_prompt": "Enter a Twitch channel: link, mobile link, or username.",
        "alert_type_prompt": (
            "Here you can set up different Twitch stream alerts. "
            "Choose which alert type to configure:\n"
            "- when a stream starts\n"
            "- when the stream category changes (no start alert; every change until the stream ends)\n"
            "- for an upcoming stream, if the streamer has a schedule\n"
            "- when a stream ends"
        ),
        "alert_type_live": "Stream start",
        "alert_type_category": "Category change",
        "alert_type_upcoming": "Upcoming stream",
        "alert_type_end": "Stream end",
        "alert_type_no_schedule": (
            "This channel has no Twitch schedule. Choose another channel "
            "or a different alert type."
        ),
        "alert_note_live": "When {twitch_username} goes live — I'll send a notification.",
        "alert_note_category": (
            "When {twitch_username} changes the stream category — I'll send a notification "
            "(every change until the stream ends; no alert when the stream starts)."
        ),
        "alert_note_end": "When {twitch_username} ends a stream — I'll send a notification.",
        "sub_list_alert_live": "• Alert: stream start",
        "sub_list_alert_category": "• Alert: category change",
        "sub_list_alert_end": "• Alert: stream end",
        "sub_list_alert_upcoming": "• Alert: upcoming stream reminder",
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
        "stream_schedule_publish_prompt": "Publish schedule on Twitch?",
        "stream_schedule_publish_yes": "✅ Publish on Twitch",
        "stream_schedule_publish_no": "❌ No",
        "stream_schedule_duration_prompt": (
            "How long is a typical stream (hours)?\n"
            "Existing Twitch schedule slots will be cleared before sync."
        ),
        "stream_schedule_duration_hour": "{hours} h",
        "stream_schedule_duration_unsure": "Not sure",
        "stream_schedule_publish_auth": "Authorize the bot to manage your Twitch schedule.",
        "stream_schedule_publish_auth_button": "Authorize on Twitch",
        "stream_schedule_publish_auth_unavailable": "Twitch schedule publishing is not configured (set PUBLIC_BASE_URL).",
        "stream_schedule_publishing": "Publishing schedule on Twitch…",
        "stream_schedule_publish_ok": "✅ Schedule published on Twitch!",
        "stream_schedule_publish_ok_recurring": (
            "✅ Schedule published on Twitch as weekly recurring segments "
            "(one-off slots are only available for Partner/Affiliate)."
        ),
        "stream_schedule_publish_fail": "❌ Failed to publish schedule: {error}",
        "stream_schedule_publish_partial": "⚠️ Published {ok}/{total} segments. Errors: {errors}",
        "stream_schedule_save_token": "💾 Save authorization",
        "stream_schedule_token_saved": "Authorization data saved. Next time you won't need to re-authorize.",
        "channel_not_parsed": (
            "Could not parse the channel. Examples:\n"
            "• marfapr\n"
            "• https://www.twitch.tv/marfapr\n"
            "• https://m.twitch.tv/marfapr"
        ),
        "channel_not_found": 'Channel "{username}" not found on Twitch. Try again.',
        "channel_found": (
            "Channel: {display_name}\n\n"
            "Set the message format. Example placeholders:\n"
            "• <code>{{username}}</code> — channel name\n"
            "• <code>{{game}}</code> — stream category\n"
            "• <code>{{name}}</code> — stream title\n\n"
            "{placeholders_link}\n\n"
            "For example, you specified this template:\n"
            "<code>{{username}} is live with {{game}}. {{name}}</code>\n\n"
            "The notification will say:\n"
            "<code>{display_name} is live with Just Chatting. Test stream</code>\n\n"
            "«Clean title» — strips streamer mentions and commands from the stream title.\n\n"
            "You can add an image on the next step"
        ),
        "placeholders_link_label": "Full list",
        "placeholders_link_unavailable": "Full list (not available on this server)",
        "placeholders_page_title": "Message placeholders",
        "placeholders_page_intro": (
            "Use these keywords in the notification template "
            "(curly braces required):"
        ),
        "placeholders_page_body": (
            "<ul>"
            "<li><code>{username}</code> — channel login</li>"
            "<li><code>{game}</code> — stream category name</li>"
            "<li><code>{name}</code> — stream title</li>"
            "<li><code>{minutes}</code> — minutes until scheduled start (upcoming alerts)</li>"
            "<li><code>{started_at}</code> — stream start time (UTC)</li>"
            "<li><code>{viewer_count}</code> — viewers at poll time</li>"
            "<li><code>{thumbnail_url}</code> — preview image URL</li>"
            "<li><code>{tags}</code> — stream tags</li>"
            "<li><code>{language}</code> — stream language</li>"
            "<li><code>{is_mature}</code> — 18+ flag</li>"
            "<li><code>{game_id}</code> — category ID</li>"
            "<li><code>{id}</code> — stream ID</li>"
            "<li><code>{type}</code> — usually live</li>"
            "</ul>"
        ),
        "oferta_page_title": "Public offer for paid bot features",
        "oferta_page_intro": (
            "This legal document is published in Russian. "
            "Open /oferta in the bot with Russian language selected."
        ),
        "oferta_page_body": (
            "<p>The full offer text is available in Russian only.</p>"
        ),
        "channel_dup_prompt": (
            "An alert for this streamer is already set up. "
            "Open the editor or continue creating a new one?"
        ),
        "channel_dup_edit": "✏️ Open editor",
        "channel_dup_continue": "➡️ Continue",
        "lucky_btn": "🎲 I'm feeling lucky",
        "lucky_hint": "Or generate a template automatically:",
        "lucky_generating": "Generating a template…",
        "lucky_failed": "Could not generate a template. Try again or write your own.",
        "lucky_preview": (
            "Generated template:\n"
            "<code>{template}</code>\n\n"
            "Example:\n"
            "<code>{preview}</code>"
        ),
        "lucky_continue": "✅ Continue",
        "lucky_again": "🎲 I'm feeling lucky",
        "lucky_full_wizard": "🛠 Full wizard",
        "image_ask": "Add an image?",
        "image_add": "🖼 Add",
        "image_skip": "Skip ⏭",
        "edit_image_prompt": "Change the image for this subscription?",
        "edit_image_replace": "🖼 Replace",
        "edit_image_keep": "Leave as is",
        "image_send_prompt": "Send an image to the bot.",
        "image_need_photo": "Please send an image (photo).",
        "image_position_prompt": "Show the image at the beginning or at the end of the post?",
        "image_position_before": "⬆️ At the beginning",
        "image_position_after": "⬇️ At the end",
        "template_empty": "Template cannot be empty.",
        "template_typo_prompt": (
            "Possible typo in placeholders:\n"
            "{typos}\n\n"
            "Fix it?"
        ),
        "template_typo_item": "• <code>{found}</code> → <code>{suggested}</code>",
        "template_typo_resend": (
            "Send a corrected message template.\n\n"
            "Example: <code>{{username}}</code>, <code>{{game}}</code>, "
            "<code>{{name}}</code>\n"
            "{placeholders_link}"
        ),
        "ignore_keywords_prompt": (
            "<b>Ignore keywords</b>\n\n"
            "Specify keywords in the stream title or category that will prevent "
            "the notification from being sent.\n\n"
            "If multiple words, separate them with commas.\n"
            "Regexp is supported (case-insensitive), e.g. <code>just.?chatting|irl</code>.\n\n"
            "Send the list or tap Skip.\n"
            "Tap «Use global list» to apply Settings → Ignored words and continue."
        ),
        "ignore_keywords_skip": "Skip ⏭",
        "ignore_keywords_use_global": "Use global list",
        "ignore_keywords_yes_note": "Ignore keywords: {keywords}",
        "ignore_keywords_yes_global_note": "Ignore keywords: {keywords} (+ global list)",
        "ignore_keywords_global_only_note": "Ignore keywords: global list",
        "ignore_keywords_no_note": "Ignore keywords: none",
        "ignored_words_prompt": (
            "<b>Ignored words</b>\n\n"
            "Current: {current}\n\n"
            "This global list can be applied to alerts via "
            "«Use global list» when setting ignore keywords.\n\n"
            "If multiple words, separate them with commas.\n"
            "Regexp is supported (case-insensitive), e.g. <code>just.?chatting|irl</code>.\n"
            "{hint}"
        ),
        "ignored_words_hint_empty": "Send words to add to the list.",
        "ignored_words_hint_edit": (
            "Send words to add (they are appended). «Clear» — remove the list. "
            "«Cancel» — leave unchanged."
        ),
        "ignored_words_clear": "Clear list",
        "ignored_words_cancel": "Cancel",
        "ignored_words_saved": "✅ Ignored words saved.",
        "ignored_words_cleared": "✅ Ignored words cleared.",
        "whisper_alerts_screen": (
            "<b>Whisper alerts</b>\n\n"
            "When this option is on, you will get alerts about new Twitch "
            "direct messages."
        ),
        "whisper_alerts_enable": "Enable",
        "whisper_alerts_oauth_prompt": (
            "Authorize the bot on Twitch so it can notify you about incoming whispers."
        ),
        "whisper_alerts_oauth_button": "Authorize on Twitch",
        "whisper_alerts_oauth_unavailable": (
            "Whisper alerts are not configured on this server "
            "(set PUBLIC_BASE_URL and the OAuth redirect URL in Twitch Console)."
        ),
        "whisper_alerts_enabled": "✅ Whisper alerts are on.",
        "whisper_alerts_disabled": "Whisper alerts are off.",
        "whisper_alerts_failed": "Could not enable whisper alerts. Try again.",
        "whisper_alerts_denied": "Twitch authorization was cancelled.",
        "whisper_alerts_revoked": (
            "Twitch stopped whisper alerts. Open Other → Whisper alerts to turn them on again."
        ),
        "whisper_alert_message": (
            "💬 New Twitch whisper\n\n"
            "From: <b>{name}</b> (@{login})\n"
            "{text}\n\n"
            '<a href="{url}">Open conversation</a>'
        ),
        "advanced_mode_screen": (
            "When advanced mode is on, creating or editing an alert shows extra options:\n"
            "• Ignore keywords (skip the alert if the title or category matches stop words)\n"
            "• Delayed send (wait N minutes after stream start before sending)\n"
            "• Repeat notification mute (no repeats if the stream drops within N minutes)\n"
            "• Delete previous messages (remove the bot’s last alert in the chat before a new one)"
        ),
        "advanced_mode_activate": "Activate mode",
        "advanced_mode_premium_only": (
            "Advanced mode is available to Premium users only."
        ),
        "beta_mode_menu": (
            "🧪 <b>Beta mode</b>\n\n"
            "Try new features before public release. Tap Join to enable a feature "
            "on your account (Premium features are free during beta).\n\n"
            "{features_block}"
        ),
        "beta_mode_empty": "There are no active beta features right now.",
        "beta_mode_admin_note": "Admins have all beta features enabled automatically.",
        "beta_mode_join": "Join",
        "beta_mode_leave": "Leave",
        "beta_mode_report_bug": "🐛 Report bug",
        "beta_mode_admin_toggle": "Admins always have beta access.",
        "beta_mode_opt_in": "✅ Joined beta: {name}",
        "beta_mode_opt_out": "Left beta: {name}",
        "wizard_simple_mode_note": (
            "<b>You are in simplified mode. Open Settings → Advanced mode "
            "to show all wizard steps.</b>"
        ),
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
        "schedule_reminder_prompt": (
            "This streamer has a Twitch schedule. Set reminders for upcoming streams?"
        ),
        "schedule_reminder_yes": "✅ Yes",
        "schedule_reminder_no": "❌ No",
        "schedule_reminder_minutes_prompt": (
            "How many minutes before the stream should I remind you?"
        ),
        "schedule_reminder_minutes_invalid": (
            "Enter a positive number of minutes, e.g. 30."
        ),
        "schedule_reminder_yes_note": "Stream reminder: {minutes} min before",
        "schedule_reminder_no_note": "Stream reminder: no",
        "schedule_live_add_prompt": (
            "Upcoming stream reminders are configured.\n"
            "Do you want to set up go-live notifications too?"
        ),
        "schedule_live_add_yes": "✅ Yes",
        "schedule_live_add_no": "❌ No",
        "setup_schedule_only_done": (
            "✅ Setup complete!\n\n"
            "Subscription #{sub_id} created.\n"
            "Twitch channel: {twitch_username}\n"
            "{schedule_reminder_note}\n"
            "Notifications: {dest}{thread_note}\n\n"
            "Upcoming stream reminders are on.\n"
            "Go-live notifications are off."
        ),
        "sub_list_dest": "• Destination: {dest} ({chat_id})",
        "sub_list_thread": "• Topic: {thread_id}",
        "sub_list_delete_yes": "• Delete old messages: yes",
        "sub_list_delete_no": "• Delete old messages: no",
        "sub_list_delete_fail": "• Notify on delete failure: yes",
        "sub_list_delete_other_yes": "• Delete other alerts: yes",
        "sub_list_delete_other_no": "• Delete other alerts: category-change only",
        "sub_list_preview_on": "• Link preview: on",
        "sub_list_preview_off": "• Link preview: off",
        "sub_list_delay": "• Delayed send: {minutes} min",
        "sub_list_delay_none": "• Delayed send: no",
        "sub_list_repeat_allow": "• Repeat notifications: allowed",
        "sub_list_repeat_mute": "• Repeat notifications: suppress {minutes} min",
        "sub_list_schedule_reminder": "• Stream reminder: {minutes} min before",
        "sub_list_schedule_reminder_none": "• Stream reminder: no",
        "sub_list_ignore_yes": "• Ignore keywords: {keywords}",
        "sub_list_ignore_yes_global": "• Ignore keywords: {keywords} (+ global)",
        "sub_list_ignore_global_only": "• Ignore keywords: global list",
        "sub_list_ignore_no": "• Ignore keywords: none",
        "sub_list_image_no": "• Image: none",
        "sub_list_image_before": "• Image: at the beginning",
        "sub_list_image_after": "• Image: at the end",
        "image_no_note": "Image: none",
        "image_before_note": "Image: at the beginning",
        "image_after_note": "Image: at the end",
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
            "• Delete own messages (for “delete old”)\n"
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
            "In channels and groups the bot needs permission to delete its own messages.\n"
            "Telegram allows deleting only messages younger than ~48 hours."
        ),
        "delete_old_text_category": (
            "Delete the bot's previous category-change message when the category changes again?\n\n"
            "By default only category-change alerts are deleted — other bot notifications are left alone.\n"
            "In channels and groups the bot needs permission to delete its own messages.\n"
            "Telegram allows deleting only messages younger than ~48 hours."
        ),
        "delete_sibling_text": (
            "You already have subscriptions that will send alerts for this streamer "
            "to the same destination. Delete other notifications too?"
        ),
        "delete_sibling_yes": "✅ Yes — delete all",
        "delete_sibling_no": "❌ No — only category changes",
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
        "weekly_new_users": "New users: {count}\nPaid users (Stars): {paid}",
        "posthog_issue_created": "🔴 New Issue",
        "posthog_issue_reopened": "🔄 Issue reopened",
        "posthog_issue_body": (
            "{title}\n\n"
            "<b>{name}</b>\n"
            "{description}"
            "{link}"
        ),
        "broadcast_footer": "—\n{type}. You can turn these off in Settings.",
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
            "{image_note}\n"
            "{ignore_keywords_note}\n"
            "{preview_note}\n"
            "{delay_note}\n"
            "{repeat_note}\n"
            "{schedule_reminder_note}\n"
            "Notifications: {dest}{thread_note}\n"
            "{delete_note}{delete_fail_note}\n\n"
            "{alert_note}\n\n"
            "To avoid duplicate alerts, you can manually turn off Twitch "
            "notifications in settings: "
            "https://www.twitch.tv/settings/notifications"
        ),
        "thread_note": "\nTopic: {thread_id}",
        "delete_yes": "Delete old messages: yes",
        "delete_yes_category": "Delete old messages: category-change only",
        "delete_yes_all": "Delete old messages: all bot alerts",
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
        "callback_stale": "Bot was updating. Tap again or open the menu.",
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
            "Your ID: <code>{user_id}</code>"
        ),
        "help": (
            "Available commands:\n"
            "/start — open the main menu\n"
            "/help — show this help\n"
            "/cancel — cancel the current wizard\n"
            "/schedule — create a weekly stream schedule\n"
            "/feedback — report a problem\n"
            "/settings — open settings\n\n"
            "Menu:\n"
            "• {btn_new} — Twitch channel, message template, optional image, filters, destination\n"
            "• {btn_import_twitch} — authorize and import followed channels\n"
            "• {btn_manage} — list, enable/disable, edit, delete\n"
            "• {btn_alert_history} — sent alerts history\n"
            "• {btn_other} — {btn_whisper_alerts}, {btn_create_schedule}, {btn_watch}\n"
            "• {btn_settings} — premium, sync, system alerts, language, partner program\n"
            "• {btn_feedback}"
        ),
        "no_subs": (
            "No subscriptions yet.\n\n"
            "Tap ➕ New subscription.\n\n"
            "Help: /help"
        ),
        "subs_list": "Your subscriptions (tap to enable/disable):\n\n",
        "import_oauth_prompt": (
            "Authorize the bot on Twitch to import channels you follow."
        ),
        "import_oauth_button": "Authorize on Twitch",
        "import_oauth_unavailable": (
            "Twitch import is not configured on this server "
            "(set PUBLIC_BASE_URL and the OAuth redirect URL in Twitch Console)."
        ),
        "import_default_template": (
            "Streamer {username} went live with {game}\n"
            "https://twitch.tv/{username}"
        ),
        "import_success": (
            "Import finished successfully.\n"
            "Added: {imported}, skipped (already listed): {skipped}"
            "{removed_note}{limit_note}"
        ),
        "import_limit_note": "\nLimit reached ({limit}): {limited} channel(s) not imported.",
        "import_removed_note": "\nRemoved (unfollowed): {removed}",
        "import_failed": "Twitch authorization failed. Try again: ⬇️ Import from Twitch.",
        "import_denied": "Twitch authorization was cancelled.",
        "import_empty": "No followed channels to import.",
        "import_mode_prompt": (
            "Authorization successful.\n\n"
            "Sync subscriptions periodically, or run a one-time import?"
        ),
        "import_mode_sync": "🔄 Sync",
        "import_mode_once": "⬇️ One-time import",
        "import_sync_days_prompt": (
            "How often should the bot sync follows from Twitch?\n"
            "Send a number of days (1–365)."
        ),
        "import_sync_days_invalid": "Send an integer from 1 to 365.",
        "import_sync_enabled": (
            "Sync enabled every {days} day(s).\n"
            "New follows will be added as enabled DM alerts; unfollowed sync "
            "imports will be removed. Manually added subscriptions are never touched."
        ),
        "import_sync_no_refresh": (
            "Twitch did not return a refresh token. "
            "One-time import only — try Sync again after reconnecting."
        ),
        "import_pending_expired": "Import session expired. Tap ⬇️ Import from Twitch again.",
        "sync_menu_off": (
            "Subscription sync is off.\n\n"
            "To enable it, open Import from Twitch."
        ),
        "sync_menu_on": (
            "Subscription sync is on.\n"
            "Period: every {days} day(s).\n"
            "Next sync: {next_at}"
        ),
        "sync_change_period": "⏱ Change period",
        "sync_now": "🔄 Sync now",
        "sync_disable": "⏸ Disable sync",
        "sync_disabled": "Sync disabled. Twitch token removed.",
        "sync_period_updated": "Sync period updated: every {days} day(s).",
        "sync_now_running": "Syncing follows from Twitch…",
        "sync_now_ok": (
            "Sync finished.\n"
            "Added: {imported}, skipped: {skipped}"
            "{removed_note}{limit_note}"
        ),
        "sync_now_none": "Sync finished. No changes.",
        "sync_job_done": (
            "Twitch sync: added {imported}, skipped {skipped}"
            "{removed_note}{limit_note}"
        ),
        "sync_job_failed": (
            "Twitch sync failed (token expired or revoked). "
            "Sync disabled — authorize again via Import."
        ),
        "sync_unfollow_ask": (
            "You unfollowed streamer(s): {list}, for whom you still have "
            "manual alerts. Delete alerts?"
        ),
        "sync_unfollow_yes": "Yes",
        "sync_unfollow_no": "No",
        "sync_unfollow_deleted": "Deleted alerts for: {list}",
        "sync_unfollow_kept": "Kept alerts for: {list}",
        "sync_unfollow_expired": "This prompt expired. Run sync again if needed.",
        "oauth_web_done_title": "Done",
        "oauth_web_done_body": "You can close this tab and return to Telegram.",
        "oauth_web_expired_title": "Session expired",
        "oauth_web_expired_body": "Open the bot again and tap Import.",
        "oauth_web_cancelled_title": "Authorization cancelled",
        "oauth_web_cancelled_body": "Return to Telegram.",
        "oauth_web_failed_title": "Authorization failed",
        "oauth_web_failed_body": "Return to Telegram and try again.",
        "enable_all": "✅ Enable all",
        "enable_all_done": "Enabled {count} subscription(s).",
        "enable_all_none": "Nothing to enable — all subscriptions are already on.",
        "toggle_off": "⏸ Off",
        "toggle_on": "✅ On",
        "strip_name_label": "Clean",
        "sub_not_found": "Subscription not found.",
        "sub_enabled": "Subscription #{sub_id} enabled.",
        "sub_disabled": "Subscription #{sub_id} disabled.",
        "no_subs_short": "No subscriptions.",
        "delete_pick": "Choose subscriptions to delete (tap to select):",
        "delete_type_pick": "Choose an alert type to delete:",
        "delete_go": "🗑 Delete selected ({count})",
        "delete_clear": "Clear selection",
        "delete_none": "Nothing selected.",
        "subs_deleted": "Deleted subscriptions: {count}.",
        "sub_deleted": "Subscription #{sub_id} deleted.",
        "edit_pick": "Choose a subscription to edit:",
        "edit_type_pick": "Choose an alert type to edit:",
        "list_type_pick": "Choose an alert type to view:",
        "edit_menu": "Subscription #{sub_id} — {username}\n\nWhat to change?",
        "edit_template": "📝 Message template",
        "edit_image": "🖼 Update image",
        "edit_image_add": "🖼 Add image",
        "edit_image_update": "🖼 Update image",
        "edit_image_delete": "🗑 Remove image",
        "edit_ignore_keywords": "🚫 Ignore keywords",
        "edit_dest": "📍 Destination",
        "edit_delete_old": "🗑 Delete old messages",
        "edit_delete_fail_notify": "⚠️ Notify on delete failure",
        "edit_delete_other": "🗑 Delete other alerts too",
        "edit_link_preview": "🔗 Link preview",
        "edit_delay": "⏱ Delay send",
        "edit_repeat": "🔁 Repeat notifications",
        "edit_schedule_reminder": "📅 Stream reminders",
        "edit_schedule_reminder_prompt": (
            "Subscription #{sub_id}\n"
            "Current: {current}\n\n"
            "Enter minutes before stream start (0 — disable reminders):"
        ),
        "edit_schedule_reminder_current_off": "off",
        "edit_schedule_reminder_current": "{minutes} min before",
        "edit_schedule_reminder_invalid": "Enter 0 or a positive number of minutes.",
        "edit_schedule_reminder_no_schedule": (
            "This streamer no longer has a Twitch schedule.\n"
            "Schedule reminders have been disabled."
        ),
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
            "Subscription #{sub_id}\n\n"
            "Current format:\n"
            "<code>{current}</code>\n\n"
            "How it will look:\n"
            "<code>{preview}</code>\n\n"
            "Send a new message template.\n\n"
            "Example placeholders:\n"
            "• <code>{{username}}</code> — channel name\n"
            "• <code>{{game}}</code> — stream category\n"
            "• <code>{{name}}</code> — stream title\n\n"
            "{placeholders_link}\n\n"
            "«Clean title» — strips streamer mentions and commands from the stream title."
        ),
        "edit_ignore_keywords_prompt": (
            "Subscription #{sub_id}\n"
            "Current: {current}\n\n"
            "Send keywords separated by commas.\n"
            "Regexp is supported (case-insensitive), e.g. <code>just.?chatting|irl</code>.\n"
            "Tap «Use global list» to apply Settings → Ignored words and finish.\n"
            "{hint}"
        ),
        "edit_ignore_keywords_hint_skip": (
            "Empty message or Skip — disable the filter."
        ),
        "edit_ignore_keywords_hint_cancel": (
            "Cancel — leave unchanged. Empty message — disable the filter."
        ),
        "ignore_keywords_cancel": "Cancel",
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
        "edit_delete_old_menu_category": (
            "Delete the previous category-change message on the next change?\n\n"
            "By default only category-change alerts are deleted. "
            "Use «Delete other alerts too» to also remove other bot notifications.\n"
            "Telegram allows deleting only messages younger than ~48 hours."
        ),
        "edit_delete_fail_menu": "Notify about problems deleting the message?",
        "edit_delete_other_menu": (
            "Delete other bot notifications for this streamer "
            "in the same destination too?"
        ),
        "edit_preview_menu": "Disable link preview in notifications?",
        "preview_yes": "❌ Off (no preview)",
        "preview_no": "✅ On (show preview)",
        "conflict_polling": (
            "Polling conflict — two bot instances may be running. Keep only one."
        ),
        "network_transient": "Transient Telegram network error (will retry): {err}",
        "unhandled_error": "Unhandled error: {err}",
        "broadcast_prompt": (
            "Choose notification type:"
        ),
        "broadcast_type_bot_update": "📬 Bot update notifications",
        "broadcast_type_availability": "📡 Bot availability alerts",
        "broadcast_type_other": "📢 Other",
        "broadcast_audience_prompt": "Who should receive this message?",
        "broadcast_audience_all": "Send to everyone",
        "broadcast_audience_ids": "Specify IDs",
        "broadcast_ids_prompt": (
            "Send recipient Telegram IDs separated by commas.\n"
            "Example: 123456789, 987654321\n"
            "/cancel — cancel."
        ),
        "broadcast_ids_invalid": "No valid IDs found. Send numbers separated by commas.",
        "broadcast_text_prompt": (
            "Send the message text (bold/italic and line breaks are kept).\n"
            "It will be auto-translated to each recipient's language.\n"
            "/cancel — abort."
        ),
        "broadcast_empty": "Message cannot be empty.",
        "broadcast_done": (
            "Broadcast complete.\n"
            "Sent: {sent}\n"
            "Total recipients: {total}\n"
            "Blocked the bot: {blocked_users}"
        ),
        "broadcast_started": (
            "Broadcast started. The bot keeps working; stats arrive when it finishes."
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
            "Current text:\n{text}\n\n"
            "Send new message text for #{id}.\n"
            "/cancel — abort."
        ),
        "scheduled_edit_text_ask": (
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
        "sys_notifications_menu": (
            "System notifications:\n\n"
            "Availability alerts also cover Twitch outages "
            "(status.twitch.com)."
        ),
        "sys_updates_label": "Bot update notifications",
        "sys_availability_label": "Bot / Twitch availability alerts",
        "sys_other_label": "Other notifications",
        "sys_sync_label": "Sync notifications",
        "twitch_status_title": "📡 Twitch Status",
        "posthog_status_title": "🦔 PostHog Status (US Cloud)",
        "twitch_indicator_none": "✅ All Systems Operational",
        "twitch_indicator_minor": "⚠️ Minor issues",
        "twitch_indicator_major": "🟠 Major issues",
        "twitch_indicator_critical": "🔴 Critical outage",
        "twitch_indicator_maintenance": "🛠 Maintenance",
        "twitch_comp_operational": "Operational",
        "twitch_comp_degraded": "Degraded Performance",
        "twitch_comp_partial": "Partial Outage",
        "twitch_comp_major": "Major Outage",
        "twitch_comp_maintenance": "Maintenance",
        "twitch_status_affected": "Affected components:",
        "twitch_status_incidents": "Incidents:",
        "bot_stats": (
            "📊 Bot statistics\n\n"
            "Users: {users}\n"
            "Created alerts: {unique_owners}\n"
            "Recipients: {notify_users}\n"
            "Subscriptions: {subscriptions_total} "
            "(✅ {subscriptions_enabled} / ⏸ {subscriptions_disabled})\n"
            "Twitch channels tracked: {unique_twitch_channels}\n"
            "Paid Premium users: {premium_paid}\n\n"
            "System notifications:\n"
            "• Bot updates: {sys_updates}\n"
            "• Bot availability: {sys_availability}\n"
            "• Other: {sys_other}\n"
            "• Blocked the bot: {blocked_users}\n\n"
            "Languages:\n"
            "• English: {locale_en}\n"
            "• Russian: {locale_ru}\n"
            "• Not set: {locale_unset}"
        ),
    },
    "ru": {
        "btn_new": "➕ Новая подписка",
        "btn_import_twitch": "⬇️ Импорт подписок из Twitch",
        "btn_manage": "📋 Управление подписками",
        "btn_list": "📋 Мои подписки",
        "btn_edit": "✏️ Редактировать подписку",
        "btn_delete": "🗑 Удалить подписку",
        "btn_feedback": "🐛 Сообщить о проблеме",
        "btn_create_schedule": "📅 Создать расписание",
        "btn_alert_history": "📜 История оповещений",
        "btn_other": "📦 Прочее",
        "btn_settings": "⚙️ Настройки",
        "btn_language": "🌐 Выбор языка",
        "btn_admin": "⚙️ Админка",
        "btn_demo": "🎬 Демо режим",
        "btn_broadcast": "📣 Рассылка",
        "btn_broadcast_new": "➕ Новая рассылка",
        "btn_scheduled_broadcasts": "📅 Запланированные сообщения",
        "btn_stats": "📊 Статистика",
        "btn_back": "◀️ Главное меню",
        "btn_wizard_back": "« Назад",
        "btn_wizard_cancel": "Отмена",
        "btn_sys_notifications": "🔔 Системные уведомления",
        "btn_ignored_words": "🚫 Игнорируемые слова",
        "btn_whisper_alerts": "💬 Оповещения об ЛС",
        "btn_advanced_mode": "🎛 Продвинутый режим",
        "btn_beta_mode": "🧪 Бета-режим",
        "btn_sys_updates": "📬 Получение оповещений об обновлениях",
        "btn_sync_subs": "🔄 Синхронизация подписок",
        "btn_premium": "⭐ Премиум",
        "btn_premium_pay": "Оплатить подписку",
        "btn_premium_trial": "Пробный период",
        "btn_premium_trial_confirm": "Активировать триал на 7 дней",
        "btn_premium_month": "Подписка на месяц — {stars} ⭐",
        "btn_premium_year": "Подписка на год — {stars} ⭐",
        "btn_premium_features": "Оплатить только нужные функции",
        "btn_premium_lifetime": "Пожизненная — {stars} ⭐",
        "btn_premium_feat_pay": "Оплатить {stars} ⭐ / мес",
        "btn_premium_feat_back": "Назад",
        "btn_premium_marfapr": "Создать подписку на marfapr",
        "btn_premium_cancel_stars": "Отменить подписку",
        "btn_premium_owned": "Купленные подписки",
        "btn_premium_cancel_feat": "Отменить",
        "btn_premium_get": "Оформить премиум",
        "btn_premium_skip": "Пропустить",
        "btn_premium_oferta": "Оферта",
        "btn_partner": "🤝 Партнёрка",
        "btn_partner_stats": "📈 Моя статистика",
        "btn_partner_link": "🔗 Получить ссылку",
        "btn_partner_withdraw": "💸 Запросить вывод",
        "btn_partner_withdrawals": "📋 Мои заявки",
        "btn_back_settings": "◀️ Настройки",
        "btn_admin_withdrawals": "💸 Выводы",
        "btn_watch": "🎲 Что посмотреть?",
        "watch_cats_prompt": (
            "Что посмотреть — шаг 1/5\n\n"
            "Введите название категории Twitch (игра или Just Chatting).\n"
            "Можно добавить до {max} категорий."
        ),
        "watch_cats_added": (
            "Добавлено: {name}\n"
            "Выбрано ({count}/{max}): {list}\n\n"
            "Введите ещё категорию или нажмите Готово."
        ),
        "watch_cats_pick": "Выберите категорию:",
        "watch_cats_not_found": "Категории по запросу «{query}» не найдены. Попробуйте другое название.",
        "watch_cats_full": "Максимум {max} категорий. Нажмите Готово или очистите список.",
        "watch_cats_need_one": "Добавьте хотя бы одну категорию.",
        "watch_cats_done": "Готово",
        "watch_cats_clear": "Очистить список",
        "watch_cats_lucky": "🎲 Мне повезёт",
        "watch_lucky_searching": "Ищем стримы…",
        "watch_lucky_empty": (
            "Нет лайвов по случайным / недавно вышедшим играм. "
            "Попробуйте ещё раз или введите категорию."
        ),
        "watch_tags_prompt": (
            "Что посмотреть — шаг 2/5\n\n"
            "Теги стрима (опционально).\n"
            "Через запятую; у стрима должны быть <b>все</b> указанные теги "
            "(например <code>русский, игры</code>).\n"
            "Или нажмите Пропустить."
        ),
        "watch_tags_skip": "Пропустить",
        "watch_tags_bad": "Отправьте теги через запятую или нажмите Пропустить.",
        "watch_viewers_prompt": (
            "Что посмотреть — шаг 3/5\n\n"
            "Диапазон зрителей:\n"
            "• <code>100-500</code> — мин–макс\n"
            "• <code>50</code> — не меньше 50\n"
            "• или нажмите Любое"
        ),
        "watch_viewers_any": "Любое",
        "watch_viewers_bad": "Отправьте число, диапазон вида 100-500 или нажмите Любое.",
        "watch_lang_prompt": (
            "Что посмотреть — шаг 4/5\n\n"
            "Язык стрима (опционально):"
        ),
        "watch_lang_any": "Любой язык",
        "watch_lang_ru": "Русский",
        "watch_lang_en": "English",
        "watch_lang_other": "Другой код…",
        "watch_lang_other_prompt": "Отправьте двухбуквенный код языка (например de, fr, ja):",
        "watch_lang_bad": "Отправьте код из 2 букв, например de, или Назад.",
        "watch_mature_prompt": (
            "Что посмотреть — шаг 5/5\n\n"
            "Исключить стримы с меткой 18+?"
        ),
        "watch_mature_exclude": "Исключить 18+",
        "watch_mature_allow": "Разрешить 18+",
        "watch_save_prompt": (
            "Сохранить как фильтр на потом? Можно хранить до {max} фильтров.\n\n"
            "{summary}"
        ),
        "watch_save_yes": "Сохранить фильтр",
        "watch_save_no": "Только сейчас",
        "watch_pick_prompt": (
            "Выберите сохранённый фильтр или начните новый поиск:"
        ),
        "watch_pick_new": "➕ Новый поиск",
        "watch_pick_delete_btn": "🗑 Удалить фильтры",
        "watch_pick_empty": "Сохранённых фильтров больше нет. Начинаем новый поиск.",
        "watch_delete_pick": "Выберите фильтры для удаления (нажмите, чтобы отметить):",
        "watch_delete_go": "🗑 Удалить выбранные ({count})",
        "watch_delete_clear": "Сбросить выбор",
        "watch_delete_none": "Ничего не выбрано.",
        "watch_delete_back": "« Назад",
        "watch_deleted": "Удалено фильтров: {count}",
        "watch_suggest_header": "Сейчас в эфире:",
        "watch_suggest_item": (
            "{n}. <b>{display}</b> (@{login})\n"
            "{title}\n"
            "🎮 {game} · 👁 {viewers}\n"
            "https://twitch.tv/{login}"
        ),
        "watch_suggest_empty": (
            "Сейчас нет стримов по вашим фильтрам.\n"
            "Попробуйте позже или измените настройки."
        ),
        "watch_suggest_vod_header": (
            "Сейчас никто не в эфире. Недавние VOD по вашим фильтрам:"
        ),
        "watch_suggest_vod_item": (
            "{n}. <b>{display}</b> (@{login})\n"
            "{title}\n"
            "🎮 {game} · ⏱ {duration}\n"
            "{url}"
        ),
        "watch_suggest_error": "Не удалось получить стримы с Twitch. Попробуйте позже.",
        "watch_again": "Ещё варианты",
        "watch_change": "Фильтры / новый поиск",
        "watch_create_alerts": "Ловить новые стримы по фильтру?",
        "watch_create_alerts_ok": (
            "Оповещение по категории создано: <b>{name}</b>\n\n"
            "{summary}{paused_note}\n\n"
            "Буду писать, когда появятся новые стримы по этому фильтру."
        ),
        "watch_create_alerts_none": (
            "Нет фильтра для отслеживания. Сначала снова откройте «Что посмотреть?»."
        ),
        "watch_create_alerts_dup": (
            "Оповещение о начале стрима с таким фильтром уже есть."
        ),
        "watch_create_alerts_paused_note": (
            "\n\nОповещение создано на паузе (лимит активных без Premium: {free_limit})."
        ),
        "edit_watch_locked": (
            "Это оповещение ловит стримы по категории/фильтру с настройками по умолчанию.\n"
            "Редактировать нельзя — только удалить (Управление подписками → Удалить)."
        ),
        "watch_prefs_summary": (
            "Фильтры: {cats}\n"
            "Зрители: {viewers}\n"
            "Язык: {language}\n"
            "Теги: {tags}\n"
            "18+: {mature}"
        ),
        "watch_viewers_label_any": "любое",
        "watch_viewers_label_min": "от {min}",
        "watch_viewers_label_range": "{min}–{max}",
        "watch_lang_label_any": "любой",
        "watch_tags_label_any": "любые",
        "watch_mature_label_exclude": "исключены",
        "watch_mature_label_allow": "разрешены",
        "menu_subs": "Управление подписками:",
        "menu_settings": "Настройки:",
        "menu_other": "Прочее:",
        "alert_history_title": "История оповещений — за {days} дн. ({n}):",
        "alert_history_empty": "Оповещений пока нет.",
        "alert_history_day": "<b>📅 {date}</b>",
        "alert_history_line": "• {time} — <b>{username}</b>",
        "alert_history_go_stream": "Перейти к стриму",
        "alert_history_body": "{text}",
        "alert_history_type_live": "В эфире",
        "alert_history_type_end": "Эфир окончен",
        "alert_history_type_category": "Смена категории",
        "alert_history_type_schedule": "Напоминание о стриме",
        "btn_alert_history_more": "Показать больше",
        "menu_partner": "Партнёрская программа:",
        "partner_intro": (
            "Приглашайте друзей по своей ссылке. Вы получаете {percent}% от каждой "
            "оплаты Stars Premium приглашённых.\n"
            "Вывод вручную, минимум {min_stars} Stars."
        ),
        "partner_stats": (
            "Приглашено: {invited}\n"
            "Оплат Stars у них: {payments}\n"
            "Доступно к выводу: {available} Stars"
        ),
        "partner_link": "Ваша партнёрская ссылка:\n{link}",
        "partner_withdraw_ok": (
            "Заявка на вывод #{id} на {amount} Stars отправлена администратору."
        ),
        "partner_withdraw_min": (
            "Минимум для вывода — {min_stars} Stars. Доступно: {available}."
        ),
        "partner_withdraw_admin": (
            "Заявка на вывод партнёра #{id}\n"
            "Пользователь: <code>{user_id}</code>\n"
            "Сумма: {amount} Stars"
        ),
        "partner_withdrawals_empty": "Заявок на вывод пока нет.",
        "partner_withdrawals_title": "Ваши заявки на вывод:",
        "partner_withdrawal_line": "#{id} — {amount} Stars — {status}",
        "partner_wd_status_pending": "в ожидании",
        "partner_wd_status_paid": "выплачено",
        "partner_wd_status_rejected": "отклонено",
        "partner_wd_paid_user": (
            "Заявка на вывод #{id} на {amount} Stars отмечена как выплаченная."
        ),
        "partner_wd_rejected_user": (
            "Заявка на вывод #{id} на {amount} Stars отклонена. "
            "Сумма возвращена на баланс."
        ),
        "admin_withdrawals_empty": "Нет заявок на вывод в ожидании.",
        "admin_withdrawals_title": "Заявки на вывод:",
        "admin_withdrawal_line": "#{id} — пользователь <code>{user_id}</code> — {amount} Stars",
        "btn_wd_paid": "✅ Выплачено",
        "btn_wd_reject": "❌ Отклонить",
        "admin_wd_resolved_paid": "Заявка #{id} отмечена как выплаченная.",
        "admin_wd_resolved_rejected": "Заявка #{id} отклонена, баланс возвращён.",
        "admin_wd_already": "Заявка #{id} уже обработана ({status}).",
        "premium_title": (
            "⭐ Премиум\n\n"
            "Возможности:\n"
            "• Активных оповещений больше {free_limit} (неактивные без лимита)\n"
            "• Типы кроме старта стрима (категория / скоро / конец)\n"
            "• Автосинхронизация фолловов с Twitch\n"
            "• Игнор ключевых слов (на алерт + глобальный список)\n"
            "• Отложенная отправка\n"
            "• Заглушка повторных уведомлений\n"
            "• Удаление предыдущих сообщений бота (+ уведомление об ошибках)\n"
            "• Публикация расписания на Twitch\n"
            "• История оповещений за 60 дней (на бесплатном — 7 дней)\n\n"
            "Как получить:\n"
            "• Оплата подписки (кнопки ниже), или\n"
            "• Активная подписка Twitch на https://www.twitch.tv/{channel}\n\n"
            "{status}"
        ),
        "premium_status_permanent": "Статус: пожизненный премиум.",
        "premium_status_trial": "Статус: триал до {until}.",
        "premium_status_stars": "Статус: подписка на месяц до {until}.",
        "premium_status_stars_canceled": (
            "Статус: подписка на месяц до {until} (автопродление выкл.)."
        ),
        "premium_status_twitch": "Статус: подписка Twitch на {channel} подтверждена.",
        "premium_status_features": "Статус: разблокированные функции:\n{features}",
        "premium_status_none": "Статус: бесплатный план.",
        "premium_buy_after_current": (
            "<b>Покупка новых тарифов будет доступна после окончания "
            "действующей подписки.</b>"
        ),
        "premium_feat_extra_alerts": "Активных оповещений больше {free_limit}",
        "premium_feat_alert_types": "Типы кроме старта стрима",
        "premium_feat_twitch_sync": "Автосинк фолловов Twitch",
        "premium_feat_advanced_mode": "Продвинутый режим",
        "premium_feat_ignore_keywords": "Игнор ключевых слов",
        "premium_feat_delay": "Отложенная отправка",
        "premium_feat_repeat": "Заглушка повторов",
        "premium_feat_delete_prev": "Удаление предыдущих сообщений",
        "premium_feat_schedule_publish": "Публикация расписания на Twitch",
        "premium_feat_alert_history": "История оповещений за 60 дней",
        "premium_feat_line": "• {name} до {until}",
        "premium_feat_line_canceled": "• {name} до {until} (автопродление выкл.)",
        "premium_feat_pick": (
            "Выберите функции ({price} ⭐ / месяц каждая).\n"
            "Разблокируются только выбранные."
        ),
        "premium_gate": "⭐ Этот шаг доступен в премиуме.\nОформите премиум или {action}.",
        "premium_gate_feature": (
            "⭐ {feature} — функция Premium.\nОформите премиум или {action}."
        ),
        "premium_gate_action_skip": "пропустите шаг",
        "premium_gate_action_cancel": "отмените",
        "premium_pay_title": "Премиум бота",
        "premium_pay_description": "Премиум на месяц ({stars} Stars)",
        "premium_pay_year_title": "Премиум бота — 1 год",
        "premium_pay_year_description": "Премиум на год ({stars} Stars)",
        "premium_pay_life_title": "Премиум бота — навсегда",
        "premium_pay_life_description": "Пожизненный премиум ({stars} Stars)",
        "premium_pay_feat_title": "Премиум бота — функции",
        "premium_pay_feat_description": "Выбранные функции ({stars} Stars / месяц)",
        "premium_pay_done": "Премиум активирован. Спасибо!",
        "premium_pay_link": "Откройте счёт для оплаты Stars:",
        "premium_trial_confirm": (
            "Активировать бесплатный триал на {days} дней?\n\n"
            "После окончания всё останется, но оповещения встанут на паузу. "
            "Оповещения о начале стрима можно удалить; включить без премиума нельзя.\n"
            "Один триал на аккаунт."
        ),
        "premium_trial_started": "Триал активирован до {until}.",
        "premium_trial_used": "Триал на этом аккаунте уже использован.",
        "premium_trial_active": "Триал уже активен до {until}.",
        "premium_trial_expired": (
            "Триал закончился. Оповещения на паузе. "
            "Оповещения о начале стрима можно удалить; для включения нужен премиум."
        ),
        "premium_cancel_done": (
            "Автопродление подписки отключено. Премиум действует до конца оплаченного периода {until}."
        ),
        "premium_cancel_feat_done": (
            "Автопродление подписки отключено. Премиум действует до конца оплаченного периода {until}."
        ),
        "premium_cancel_none": "Нет активной подписки для отмены.",
        "premium_cancel_failed": (
            "Не удалось отменить автопродление через Telegram. "
            "Попробуйте позже или отмените в настройках Telegram → Stars."
        ),
        "premium_pay_failed": "Не удалось создать счёт Stars. Попробуйте позже.",
        "premium_owned_title": "Купленные подписки:\n{items}",
        "premium_owned_empty": "Нет купленных подписок.",
        "premium_owned_stars": "• Подписка на месяц до {until}",
        "premium_owned_stars_canceled": (
            "• Подписка на месяц до {until} (автопродление выкл.)"
        ),
        "premium_owned_feat": "• {name} до {until}",
        "premium_owned_feat_canceled": (
            "• {name} до {until} (автопродление выкл.)"
        ),
        "premium_feat_owned": "Уже куплено",
        "premium_plans_blocked": "Премиум-план уже активен.",
        "premium_marfapr_need_sub": (
            "Активная подписка Twitch на {channel} не найдена.\n"
            "Оформите её на https://www.twitch.tv/{channel} и попробуйте снова."
        ),
        "premium_marfapr_ok": (
            "Подписка Twitch подтверждена. Премиум открыт.\n"
            "Оповещение на {channel} создано."
        ),
        "premium_marfapr_ok_exists": (
            "Подписка Twitch подтверждена. Премиум открыт.\n"
            "Оповещение на {channel} у вас уже есть."
        ),
        "premium_marfapr_oauth": (
            "Привяжите Twitch, чтобы проверить подписку на {channel}:"
        ),
        "premium_active_limit": (
            "На бесплатном плане не больше {limit} активных оповещений.\n"
            "Отключите одно или оформите премиум."
        ),
        "premium_created_disabled": (
            "Оповещение создано на паузе: на бесплатном плане лимит {limit} активных. "
            "Включите после апгрейда или паузы другого."
        ),
        "premium_trial_paused_enable": (
            "Оповещение на паузе после триала. Оформите премиум, чтобы включить."
        ),
        "menu_admin": "Админка:",
        "demo_on": (
            "🎬 Демо режим включён.\n\n"
            "Вы видите бота как обычный пользователь без Premium.\n"
            "Демо-подписки уже созданы — можно пробовать меню.\n"
            "Всё созданное и изменённое здесь сбросится при выходе.\n"
            "Нажмите «Демо режим» ещё раз, чтобы выйти."
        ),
        "demo_off": (
            "🎬 Демо режим выключен.\n\n"
            "Демо-подписки удалены. Ваши настоящие подписки не тронуты."
        ),
        "demo_seed_template": (
            "{username} в эфире! (демо)\n"
            "{name}\n"
            "Категория: {game}"
        ),
        "demo_seed_template_2": (
            "🔴 ДЕМО — {username}\n"
            "{game}"
        ),
        "menu_broadcast": "Рассылка:",
        "menu_main": "Главное меню",
        "lang_pick": "Выберите язык / Choose your language:",
        "lang_set": "Язык: русский.",
        "start_welcome": (
            "Привет! Я присылаю уведомления о старте стримов на Twitch.\n"
            "Справка по командам: /help\n"
            "Нажмите кнопку Новая подписка, чтобы добавить новую подписку."
        ),
        "new_sub_prompt": "Укажите канал Twitch: ссылку, мобильную ссылку или username.",
        "alert_type_prompt": (
            "Здесь можно настроить различные оповещения о стримах на Twitch. "
            "Выберите какой тип оповещения настроить:\n"
            "- о начале стрима\n"
            "- о смене категории (без оповещения о старте; при каждой смене до конца стрима)\n"
            "- о предстоящем стриме, если у стримера есть расписание\n"
            "- об окончании стрима"
        ),
        "alert_type_live": "Начало стрима",
        "alert_type_category": "Смена категории",
        "alert_type_upcoming": "Предстоящий стрим",
        "alert_type_end": "Окончание стрима",
        "alert_type_no_schedule": (
            "У этого канала нет расписания на Twitch. Укажите другой канал "
            "или выберите другой тип оповещения."
        ),
        "alert_note_live": "Когда {twitch_username} начнёт стрим — пришлю уведомление.",
        "alert_note_category": (
            "Когда {twitch_username} сменит категорию стрима — пришлю уведомление "
            "(при каждой смене до конца стрима; о начале стрима не пишу)."
        ),
        "alert_note_end": "Когда {twitch_username} закончит стрим — пришлю уведомление.",
        "sub_list_alert_live": "• Оповещение: начало стрима",
        "sub_list_alert_category": "• Оповещение: смена категории",
        "sub_list_alert_end": "• Оповещение: окончание стрима",
        "sub_list_alert_upcoming": "• Оповещение: напоминание о предстоящем стриме",
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
        "stream_schedule_publish_prompt": "Опубликовать расписание на Twitch?",
        "stream_schedule_publish_yes": "✅ Опубликовать на Twitch",
        "stream_schedule_publish_no": "❌ Нет",
        "stream_schedule_duration_prompt": (
            "Сколько обычно длится стрим (в часах)?\n"
            "Перед синхронизацией все текущие слоты на Twitch будут удалены."
        ),
        "stream_schedule_duration_hour": "{hours} ч",
        "stream_schedule_duration_unsure": "Не уверен",
        "stream_schedule_publish_auth": "Авторизуйте бота для управления расписанием на Twitch.",
        "stream_schedule_publish_auth_button": "Авторизоваться на Twitch",
        "stream_schedule_publish_auth_unavailable": "Публикация расписания не настроена (нужен PUBLIC_BASE_URL).",
        "stream_schedule_publishing": "Публикую расписание на Twitch…",
        "stream_schedule_publish_ok": "✅ Расписание опубликовано на Twitch!",
        "stream_schedule_publish_ok_recurring": (
            "✅ Расписание опубликовано на Twitch как еженедельные слоты "
            "(разовые сегменты доступны только Partner/Affiliate)."
        ),
        "stream_schedule_publish_fail": "❌ Не удалось опубликовать расписание: {error}",
        "stream_schedule_publish_partial": "⚠️ Опубликовано {ok}/{total} сегментов. Ошибки: {errors}",
        "stream_schedule_save_token": "💾 Сохранить авторизацию",
        "stream_schedule_token_saved": "Данные авторизации сохранены. В следующий раз повторная авторизация не потребуется.",
        "channel_not_parsed": (
            "Не удалось распознать канал. Примеры:\n"
            "• marfapr\n"
            "• https://www.twitch.tv/marfapr\n"
            "• https://m.twitch.tv/marfapr"
        ),
        "channel_not_found": "Канал «{username}» не найден на Twitch. Попробуйте ещё раз.",
        "channel_found": (
            "Канал: {display_name}\n\n"
            "Задайте формат сообщения. Пример ключевых слов:\n"
            "• <code>{{username}}</code> — имя канала\n"
            "• <code>{{game}}</code> — категория стрима\n"
            "• <code>{{name}}</code> — название стрима\n\n"
            "{placeholders_link}\n\n"
            "Например, вы указали шаблон:\n"
            "<code>{{username}} в эфире с игрой {{game}}. {{name}}</code>\n\n"
            "В оповещении будет текст:\n"
            "<code>{display_name} в эфире с игрой Just Chatting. Тестовый стрим</code>\n\n"
            "«Очистка названия» — убирает из названия стрима упоминания стримеров и команды.\n\n"
            "Изображение можно добавить на следующем шаге"
        ),
        "placeholders_link_label": "Полный список",
        "placeholders_link_unavailable": "Полный список (недоступен на этом сервере)",
        "placeholders_page_title": "Ключевые слова шаблона",
        "placeholders_page_intro": (
            "Используйте эти ключевые слова в шаблоне уведомления "
            "(обязательны фигурные скобки):"
        ),
        "placeholders_page_body": (
            "<ul>"
            "<li><code>{username}</code> — логин канала</li>"
            "<li><code>{game}</code> — название категории</li>"
            "<li><code>{name}</code> — название стрима</li>"
            "<li><code>{minutes}</code> — минут до старта по расписанию (предстоящий стрим)</li>"
            "<li><code>{started_at}</code> — время старта (UTC)</li>"
            "<li><code>{viewer_count}</code> — зрители на момент запроса</li>"
            "<li><code>{thumbnail_url}</code> — URL превью кадра</li>"
            "<li><code>{tags}</code> — теги стрима</li>"
            "<li><code>{language}</code> — язык стрима</li>"
            "<li><code>{is_mature}</code> — метка 18+</li>"
            "<li><code>{game_id}</code> — ID категории</li>"
            "<li><code>{id}</code> — ID стрима</li>"
            "<li><code>{type}</code> — обычно live</li>"
            "</ul>"
        ),
        "oferta_page_title": "Публичная оферта на платные функции бота",
        "oferta_page_intro": (
            "Настоящий документ является официальным предложением "
            "(публичной офертой) заключить договор на условиях ниже."
        ),
        "oferta_page_body": (
            "<h2>1. Исполнитель</h2>"
            "<p>Индивидуальный предприниматель Докучаев Константин Георгиевич<br>"
            "ИНН 760403963548<br>"
            "ОГРНИП 318762700036170<br>"
            "Сайт: "
            "<a href=\"https://blog.themarfa.name/\">blog.themarfa.name</a><br>"
            "E-mail: "
            "<a href=\"mailto:biz@themarfa.name\">biz@themarfa.name</a><br>"
            "Телефон: +7-915-968-1682</p>"
            "<h2>2. Предмет оферты</h2>"
            "<p>Исполнитель предоставляет Пользователю доступ к платным "
            "(премиум) функциям Telegram-бота для оповещений о стримах Twitch "
            "(далее — Бот) на условиях настоящей оферты.</p>"
            "<h2>3. Платные функции</h2>"
            "<p>В состав премиум-доступа входят, в частности:</p>"
            "<ul>"
            "<li>более {free_limit} активных оповещений "
            "(неактивные — без лимита);</li>"
            "<li>типы оповещений кроме старта стрима "
            "(категория / скоро / конец);</li>"
            "<li>автосинхронизация фолловов с Twitch;</li>"
            "<li>игнор ключевых слов (на алерт и глобальный список);</li>"
            "<li>отложенная отправка;</li>"
            "<li>заглушка повторных уведомлений;</li>"
            "<li>удаление предыдущих сообщений бота;</li>"
            "<li>публикация расписания на Twitch;</li>"
            "<li>история оповещений за 60 дней "
            "(на бесплатном плане — 7 дней).</li>"
            "</ul>"
            "<p>Состав функций может уточняться в интерфейсе Бота. "
            "Бесплатный пробный период — {trial_days} дней "
            "(один раз на аккаунт Telegram), если он доступен.</p>"
            "<p>Премиум также может предоставляться без оплаты Stars "
            "при активной подписке Twitch на канал "
            "<a href=\"https://www.twitch.tv/{channel}\">{channel}</a> "
            "либо на иных условиях, указанных в Боте "
            "(например, участие в указанном чате).</p>"
            "<h2>4. Цены</h2>"
            "<p>Оплата производится через платёжную систему Telegram "
            "в валюте Telegram Stars (XTR). Ниже указаны цены услуг "
            "и ориентировочный эквивалент в рублях "
            "из расчёта {rub_per_star}&nbsp;₽ за 1 Star "
            "(фактическая стоимость покупки Stars для Пользователя "
            "определяется Telegram и платёжными системами "
            "на момент покупки Stars и может отличаться).</p>"
            "<ul>"
            "<li>Подписка на месяц (с автопродлением): "
            "{month_stars} Stars — {month_rub}&nbsp;₽;</li>"
            "<li>Подписка на год (разовый платёж): "
            "{year_stars} Stars — {year_rub}&nbsp;₽;</li>"
            "<li>Пожизненный доступ: "
            "{life_stars} Stars — {life_rub}&nbsp;₽;</li>"
            "<li>Одна выбранная функция на месяц: "
            "{feat_stars} Stars — {feat_rub}&nbsp;₽ "
            "(можно оплатить несколько функций; сумма кратна числу выбранных).</li>"
            "</ul>"
            "<p>Актуальные цены в Stars также отображаются на кнопках оплаты "
            "в разделе «Настройки → Премиум» Бота.</p>"
            "<h2>5. Акцепт оферты и оплата</h2>"
            "<p>Акцептом оферты является оплата выбранного тарифа "
            "через интерфейс Бота (счёт Telegram Stars) либо иное "
            "предусмотренное Ботом действие, явно подтверждающее "
            "получение платного доступа. Договор считается заключённым "
            "с момента успешной оплаты (или подтверждения иного основания "
            "доступа) и действует в течение оплаченного периода "
            "либо бессрочно для пожизненного тарифа.</p>"
            "<h2>6. Порядок оказания услуг</h2>"
            "<p>Доступ к платным функциям активируется автоматически "
            "после успешной оплаты. При отмене автопродления подписки "
            "доступ сохраняется до конца уже оплаченного периода.</p>"
            "<h2>7. Возврат средств</h2>"
            "<p>Услуга считается оказанной с момента предоставления доступа "
            "к платным функциям. Возврат Stars регулируется правилами "
            "Telegram и применимым законодательством. По вопросам, "
            "связанным с оплатой через Бота, обращайтесь "
            "на biz@themarfa.name.</p>"
            "<h2>8. Ответственность</h2>"
            "<p>Исполнитель не гарантирует бесперебойную работу сторонних "
            "сервисов (Telegram, Twitch и др.). Исполнитель не несёт "
            "ответственности за сбои, вызванные действиями Пользователя, "
            "третьих лиц или непреодолимой силой. Совокупная "
            "ответственность Исполнителя ограничена стоимостью "
            "оплаченного Пользователем тарифа за последний период оплаты.</p>"
            "<h2>9. Персональные данные</h2>"
            "<p>Для оказания услуг обрабатываются идентификаторы "
            "и данные, необходимые для работы Бота в Telegram/Twitch "
            "(в объёме, требуемом функциональностью). Обработка ведётся "
            "в целях исполнения договора и поддержки сервиса.</p>"
            "<h2>10. Заключительные положения</h2>"
            "<p>К настоящей оферте применяется право Российской Федерации. "
            "Споры подлежат рассмотрению в соответствии с "
            "законодательством РФ по месту регистрации Исполнителя, "
            "если иное не предусмотрено императивными нормами. "
            "Исполнитель вправе изменять условия оферты; новая редакция "
            "публикуется по адресу этой страницы и применяется "
            "к последующим оплатам.</p>"
            "<p>Реквизиты и контакты Исполнителя также опубликованы на "
            "<a href=\"https://blog.themarfa.name/sotrudnichiestvo/"
            "#%D1%8E%D1%80%D0%B8%D0%B4%D0%B8%D1%87%D0%B5%D1%81%D0%BA%D0%BE%D0%B5-"
            "%D0%BB%D0%B8%D1%86%D0%BE\">странице сотрудничества</a>.</p>"
        ),
        "channel_dup_prompt": (
            "Уже есть настроенное оповещение для этого стримера. "
            "Хотите перейти к редактированию или продолжить?"
        ),
        "channel_dup_edit": "✏️ Перейти к редактированию",
        "channel_dup_continue": "➡️ Продолжить",
        "lucky_btn": "🎲 Мне повезёт",
        "lucky_hint": "Или сгенерировать шаблон автоматически:",
        "lucky_generating": "Генерирую шаблон…",
        "lucky_failed": "Не удалось сгенерировать шаблон. Попробуйте ещё раз или напишите свой.",
        "lucky_preview": (
            "Сгенерированный шаблон:\n"
            "<code>{template}</code>\n\n"
            "Пример:\n"
            "<code>{preview}</code>"
        ),
        "lucky_continue": "✅ Продолжить",
        "lucky_again": "🎲 Мне повезёт",
        "lucky_full_wizard": "🛠 Перейти к полному мастеру",
        "image_ask": "Добавить изображение?",
        "image_add": "🖼 Добавить",
        "image_skip": "Пропустить ⏭",
        "edit_image_prompt": "Изменить изображение для этой подписки?",
        "edit_image_replace": "🖼 Заменить",
        "edit_image_keep": "Оставить как есть",
        "image_send_prompt": "Отправьте изображение боту",
        "image_need_photo": "Пожалуйста, отправьте изображение (фото).",
        "image_position_prompt": "Отображать картинку в начале или конце поста?",
        "image_position_before": "⬆️ В начале",
        "image_position_after": "⬇️ В конце",
        "template_empty": "Шаблон не может быть пустым.",
        "template_typo_prompt": (
            "Похоже, опечатка в ключевых словах:\n"
            "{typos}\n\n"
            "Исправить?"
        ),
        "template_typo_item": "• <code>{found}</code> → <code>{suggested}</code>",
        "template_typo_resend": (
            "Отправьте исправленный шаблон сообщения.\n\n"
            "Пример: <code>{{username}}</code>, <code>{{game}}</code>, "
            "<code>{{name}}</code>\n"
            "{placeholders_link}"
        ),
        "ignore_keywords_prompt": (
            "<b>Игнорировать ключевые слова</b>\n\n"
            "Укажите ключевые слова в названии стрима или игре, при наличии которых "
            "оповещение не будет отправляться.\n\n"
            "Если несколько слов, укажите их через запятую.\n"
            "Поддерживается regexp (без учёта регистра), например "
            "<code>just.?chatting|irl</code>.\n\n"
            "Отправьте список слов или нажмите «Пропустить».\n"
            "Нажмите «Использовать глобальный список», чтобы применить "
            "Настройки → Игнорируемые слова и перейти дальше."
        ),
        "ignore_keywords_skip": "Пропустить ⏭",
        "ignore_keywords_use_global": "Использовать глобальный список",
        "ignore_keywords_yes_note": "Игнорировать ключевые слова: {keywords}",
        "ignore_keywords_yes_global_note": (
            "Игнорировать ключевые слова: {keywords} (+ глобальный список)"
        ),
        "ignore_keywords_global_only_note": "Игнорировать ключевые слова: глобальный список",
        "ignore_keywords_no_note": "Игнорировать ключевые слова: нет",
        "ignored_words_prompt": (
            "<b>Игнорируемые слова</b>\n\n"
            "Сейчас: {current}\n\n"
            "Этот глобальный список можно применить к оповещениям через "
            "«Использовать глобальный список» при настройке игнорируемых ключевых слов.\n\n"
            "Если несколько слов, укажите их через запятую.\n"
            "Поддерживается regexp (без учёта регистра), например "
            "<code>just.?chatting|irl</code>.\n"
            "{hint}"
        ),
        "ignored_words_hint_empty": "Отправьте слова, чтобы добавить в список.",
        "ignored_words_hint_edit": (
            "Отправьте слова, чтобы добавить (они допишутся к списку). "
            "«Очистить» — удалить список. «Отмена» — без изменений."
        ),
        "ignored_words_clear": "Очистить список",
        "ignored_words_cancel": "Отмена",
        "ignored_words_saved": "✅ Игнорируемые слова сохранены.",
        "ignored_words_cleared": "✅ Игнорируемые слова очищены.",
        "whisper_alerts_screen": (
            "<b>Оповещения об ЛС</b>\n\n"
            "При включении этой опции вы будете получать оповещения о новых "
            "личных сообщениях на Twitch."
        ),
        "whisper_alerts_enable": "Включить",
        "whisper_alerts_oauth_prompt": (
            "Авторизуйте бота на Twitch, чтобы получать оповещения о входящих личных сообщениях."
        ),
        "whisper_alerts_oauth_button": "Авторизоваться на Twitch",
        "whisper_alerts_oauth_unavailable": (
            "Оповещения об ЛС не настроены на этом сервере "
            "(нужны PUBLIC_BASE_URL и OAuth Redirect URL в Twitch Console)."
        ),
        "whisper_alerts_enabled": "✅ Оповещения об ЛС включены.",
        "whisper_alerts_disabled": "Оповещения об ЛС выключены.",
        "whisper_alerts_failed": "Не удалось включить оповещения об ЛС. Попробуйте ещё раз.",
        "whisper_alerts_denied": "Авторизация Twitch отменена.",
        "whisper_alerts_revoked": (
            "Twitch отключил оповещения об ЛС. Включите их снова в Прочем → Оповещения об ЛС."
        ),
        "whisper_alert_message": (
            "💬 Новое личное сообщение на Twitch\n\n"
            "От: <b>{name}</b> (@{login})\n"
            "{text}\n\n"
            '<a href="{url}">Открыть переписку</a>'
        ),
        "advanced_mode_screen": (
            "При активации продвинутого режима в создании или редактировании "
            "сообщения у вас появятся дополнительные опции:\n"
            "• Игнор ключевых слов (не отправлять оповещение, если в названии "
            "или категории есть стоп-слова)\n"
            "• Отложенная отправка (подождать N минут после старта стрима "
            "перед отправкой)\n"
            "• Заглушка повторов (не слать повторные оповещения при обрыве "
            "стрима в течение N минут)\n"
            "• Удаление предыдущих сообщений (удалять прошлое оповещение бота "
            "в чате перед новым)"
        ),
        "advanced_mode_activate": "Активировать режим",
        "advanced_mode_premium_only": (
            "Активация продвинутого режима доступна только Premium-пользователям."
        ),
        "beta_mode_menu": (
            "🧪 <b>Бета-режим</b>\n\n"
            "Пробуйте новые функции до публичного релиза. Нажмите «Присоединиться», "
            "чтобы включить функцию (Premium на время бета — бесплатно).\n\n"
            "{features_block}"
        ),
        "beta_mode_empty": "Сейчас нет активных бета-функций.",
        "beta_mode_admin_note": "Админам все бета-функции включены автоматически.",
        "beta_mode_join": "Присоединиться",
        "beta_mode_leave": "Выйти",
        "beta_mode_report_bug": "🐛 Сообщить об ошибке",
        "beta_mode_admin_toggle": "У админов бета-доступ всегда включён.",
        "beta_mode_opt_in": "✅ Вы в бете: {name}",
        "beta_mode_opt_out": "Вы вышли из беты: {name}",
        "wizard_simple_mode_note": (
            "<b>Вы работаете в упрощённом режиме, перейдите в Настройках "
            "в продвинутый режим для отображения всех шагов мастера.</b>"
        ),
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
        "schedule_reminder_prompt": (
            "У стримера есть расписание. Настроить напоминания о предстоящих стримах?"
        ),
        "schedule_reminder_yes": "✅ Да",
        "schedule_reminder_no": "❌ Нет",
        "schedule_reminder_minutes_prompt": (
            "За сколько минут напомнить до начала стрима?"
        ),
        "schedule_reminder_minutes_invalid": (
            "Введите положительное число минут, например 30."
        ),
        "schedule_reminder_yes_note": "Напоминание о стриме: за {minutes} мин.",
        "schedule_reminder_no_note": "Напоминание о стриме: нет",
        "schedule_live_add_prompt": (
            "Оповещение о предстоящих стримах настроено.\n"
            "Хотите настроить оповещение о начале стримов?"
        ),
        "schedule_live_add_yes": "✅ Да",
        "schedule_live_add_no": "❌ Нет",
        "setup_schedule_only_done": (
            "✅ Настройка завершена!\n\n"
            "Подписка #{sub_id} создана.\n"
            "Канал Twitch: {twitch_username}\n"
            "{schedule_reminder_note}\n"
            "Уведомления: {dest}{thread_note}\n\n"
            "Напоминания о предстоящих стримах включены.\n"
            "Оповещения о начале стрима отключены."
        ),
        "sub_list_dest": "• Куда: {dest} ({chat_id})",
        "sub_list_thread": "• Тема: {thread_id}",
        "sub_list_delete_yes": "• Удалять старые: да",
        "sub_list_delete_no": "• Удалять старые: нет",
        "sub_list_delete_fail": "• Сообщать о проблемах удаления: да",
        "sub_list_delete_other_yes": "• Удалять другие оповещения: да",
        "sub_list_delete_other_no": "• Удалять другие оповещения: только смена категории",
        "sub_list_preview_on": "• Превью ссылок: включено",
        "sub_list_preview_off": "• Превью ссылок: выключено",
        "sub_list_delay": "• Отложенная отправка: {minutes} мин.",
        "sub_list_delay_none": "• Отложенная отправка: нет",
        "sub_list_repeat_allow": "• Повторные уведомления: разрешены",
        "sub_list_repeat_mute": "• Повторные уведомления: заглушка {minutes} мин.",
        "sub_list_schedule_reminder": "• Напоминание о стриме: за {minutes} мин.",
        "sub_list_schedule_reminder_none": "• Напоминание о стриме: нет",
        "sub_list_ignore_yes": "• Игнорировать ключевые слова: {keywords}",
        "sub_list_ignore_yes_global": "• Игнорировать ключевые слова: {keywords} (+ глобальный)",
        "sub_list_ignore_global_only": "• Игнорировать ключевые слова: глобальный список",
        "sub_list_ignore_no": "• Игнорировать ключевые слова: нет",
        "sub_list_image_no": "• Изображение: нет",
        "sub_list_image_before": "• Изображение: в начале",
        "sub_list_image_after": "• Изображение: в конце",
        "image_no_note": "Изображение: нет",
        "image_before_note": "Изображение: в начале",
        "image_after_note": "Изображение: в конце",
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
            "• Удаление своих сообщений (для «удалять старые»)\n"
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
            "В канале и группе боту нужно право удалять свои сообщения.\n"
            "Telegram позволяет удалять только сообщения младше ~48 часов."
        ),
        "delete_old_text_category": (
            "Удалять предыдущее сообщение бота при следующей смене категории?\n\n"
            "По умолчанию удаляются только оповещения о смене категории — "
            "другие оповещения бота не трогаются.\n"
            "В канале и группе боту нужно право удалять свои сообщения.\n"
            "Telegram позволяет удалять только сообщения младше ~48 часов."
        ),
        "delete_sibling_text": (
            "У вас уже есть подписки, по которым бот будет отправлять оповещения. "
            "Удалять другие уведомления?"
        ),
        "delete_sibling_yes": "✅ Да — удалять все",
        "delete_sibling_no": "❌ Нет — только смену категории",
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
        "weekly_new_users": "Новых пользователей: {count}\nПлатных (Stars): {paid}",
        "posthog_issue_created": "🔴 Новый Issue",
        "posthog_issue_reopened": "🔄 Issue снова открыт",
        "posthog_issue_body": (
            "{title}\n\n"
            "<b>{name}</b>\n"
            "{description}"
            "{link}"
        ),
        "broadcast_footer": "—\n{type}. Можно отключить в настройках",
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
            "{image_note}\n"
            "{ignore_keywords_note}\n"
            "{preview_note}\n"
            "{delay_note}\n"
            "{repeat_note}\n"
            "{schedule_reminder_note}\n"
            "Уведомления: {dest}{thread_note}\n"
            "{delete_note}{delete_fail_note}\n\n"
            "{alert_note}\n\n"
            "Чтобы избежать дублирования оповещений, вы можете вручную "
            "отключить оповещения Twitch в разделе настроек: "
            "https://www.twitch.tv/settings/notifications"
        ),
        "thread_note": "\nТема: {thread_id}",
        "delete_yes": "Удалять старые сообщения: да",
        "delete_yes_category": "Удалять старые сообщения: только смена категории",
        "delete_yes_all": "Удалять старые сообщения: все оповещения",
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
        "callback_stale": "Бот обновлялся. Нажмите ещё раз или откройте меню.",
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
            "Ваш ID: <code>{user_id}</code>"
        ),
        "help": (
            "Доступные команды:\n"
            "/start — главное меню\n"
            "/help — эта справка\n"
            "/cancel — отменить текущий мастер\n"
            "/schedule — создать расписание стримов\n"
            "/feedback — сообщить о проблеме\n"
            "/settings — настройки\n\n"
            "Меню:\n"
            "• {btn_new} — канал Twitch, шаблон, опционально картинка, фильтры, куда слать\n"
            "• {btn_import_twitch} — авторизация и импорт фолловов\n"
            "• {btn_manage} — список, вкл/выкл, редактирование, удаление\n"
            "• {btn_alert_history} — история отправленных оповещений\n"
            "• {btn_other} — {btn_whisper_alerts}, {btn_create_schedule}, {btn_watch}\n"
            "• {btn_settings} — премиум, sync, системные уведомления, язык, партнёрка\n"
            "• {btn_feedback}"
        ),
        "no_subs": (
            "Подписок пока нет.\n\n"
            "Нажмите ➕ Новая подписка.\n\n"
            "Справка: /help"
        ),
        "subs_list": "Ваши подписки (нажмите, чтобы включить/выключить):\n\n",
        "import_oauth_prompt": (
            "Авторизуйте бота на Twitch, чтобы импортировать каналы, на которые вы подписаны."
        ),
        "import_oauth_button": "Авторизоваться на Twitch",
        "import_oauth_unavailable": (
            "Импорт из Twitch не настроен на этом сервере "
            "(нужны PUBLIC_BASE_URL и OAuth Redirect URL в Twitch Console)."
        ),
        "import_default_template": (
            "Стример {username} вышел в эфир с игрой {game}\n"
            "https://twitch.tv/{username}"
        ),
        "import_success": (
            "Импорт прошёл успешно.\n"
            "Добавлено: {imported}, пропущено (уже есть): {skipped}"
            "{removed_note}{limit_note}"
        ),
        "import_limit_note": "\nЛимит ({limit}): не импортировано каналов: {limited}.",
        "import_removed_note": "\nУдалено (отписка): {removed}",
        "import_failed": "Не удалось авторизоваться на Twitch. Попробуйте снова: ⬇️ Импорт подписок из Twitch.",
        "import_denied": "Авторизация на Twitch отменена.",
        "import_empty": "Нет каналов для импорта.",
        "import_mode_prompt": (
            "Авторизация прошла успешно.\n\n"
            "Синхронизировать подписки периодически или выполнить одноразовый импорт?"
        ),
        "import_mode_sync": "🔄 Синхронизировать",
        "import_mode_once": "⬇️ Одноразовый импорт",
        "import_sync_days_prompt": (
            "Как часто сверять фолловы Twitch с ботом?\n"
            "Отправьте число дней (1–365)."
        ),
        "import_sync_days_invalid": "Отправьте целое число от 1 до 365.",
        "import_sync_enabled": (
            "Синхронизация включена раз в {days} дн.\n"
            "Новые фолловы добавятся как включённые оповещения в личку; "
            "отфолловленные импорты удалятся. Вручную добавленные подписки не трогаются."
        ),
        "import_sync_no_refresh": (
            "Twitch не вернул refresh token. "
            "Доступен только одноразовый импорт — попробуйте синхронизацию снова."
        ),
        "import_pending_expired": "Сессия импорта истекла. Нажмите ⬇️ Импорт подписок из Twitch снова.",
        "sync_menu_off": (
            "Синхронизация подписок выключена.\n\n"
            "Чтобы включить, перейдите в меню Импорт подписок из Twitch."
        ),
        "sync_menu_on": (
            "Синхронизация подписок включена.\n"
            "Период: раз в {days} дн.\n"
            "Следующая сверка: {next_at}"
        ),
        "sync_change_period": "⏱ Изменить период",
        "sync_now": "🔄 Синхронизировать сейчас",
        "sync_disable": "⏸ Отключить синхронизацию",
        "sync_disabled": "Синхронизация отключена. Токен Twitch удалён.",
        "sync_period_updated": "Период синхронизации: раз в {days} дн.",
        "sync_now_running": "Синхронизирую подписки с Twitch…",
        "sync_now_ok": (
            "Синхронизация завершена.\n"
            "Добавлено: {imported}, пропущено: {skipped}"
            "{removed_note}{limit_note}"
        ),
        "sync_now_none": "Синхронизация завершена. Изменений нет.",
        "sync_job_done": (
            "Синхронизация Twitch: добавлено {imported}, пропущено {skipped}"
            "{removed_note}{limit_note}"
        ),
        "sync_job_failed": (
            "Синхронизация Twitch не удалась (токен истёк или отозван). "
            "Синхронизация отключена — авторизуйтесь снова через Импорт."
        ),
        "sync_unfollow_ask": (
            "Вы отписались от стримера(ов): {list}, для которых у вас созданы "
            "ручные оповещения. Удалить оповещения?"
        ),
        "sync_unfollow_yes": "Да",
        "sync_unfollow_no": "Нет",
        "sync_unfollow_deleted": "Удалены оповещения для: {list}",
        "sync_unfollow_kept": "Оповещения оставлены для: {list}",
        "sync_unfollow_expired": "Этот запрос устарел. При необходимости запустите синхронизацию снова.",
        "oauth_web_done_title": "Готово",
        "oauth_web_done_body": "Можете закрыть эту вкладку и вернуться в Telegram.",
        "oauth_web_expired_title": "Сессия истекла",
        "oauth_web_expired_body": "Откройте бота снова и нажмите «Импорт».",
        "oauth_web_cancelled_title": "Авторизация отменена",
        "oauth_web_cancelled_body": "Вернитесь в Telegram.",
        "oauth_web_failed_title": "Авторизация не удалась",
        "oauth_web_failed_body": "Вернитесь в Telegram и попробуйте снова.",
        "enable_all": "✅ Включить все",
        "enable_all_done": "Включено подписок: {count}.",
        "enable_all_none": "Нечего включать — все подписки уже активны.",
        "toggle_off": "⏸ Выкл",
        "toggle_on": "✅ Вкл",
        "strip_name_label": "Очистка",
        "sub_not_found": "Подписка не найдена.",
        "sub_enabled": "Подписка #{sub_id} включена.",
        "sub_disabled": "Подписка #{sub_id} выключена.",
        "no_subs_short": "Подписок нет.",
        "delete_pick": "Выберите подписки для удаления (нажмите, чтобы отметить):",
        "delete_type_pick": "Выберите тип оповещения для удаления:",
        "delete_go": "🗑 Удалить выбранные ({count})",
        "delete_clear": "Сбросить выбор",
        "delete_none": "Ничего не выбрано.",
        "subs_deleted": "Удалено подписок: {count}.",
        "sub_deleted": "Подписка #{sub_id} удалена.",
        "edit_pick": "Выберите подписку для редактирования:",
        "edit_type_pick": "Выберите тип оповещения для редактирования:",
        "list_type_pick": "Выберите тип оповещения для просмотра:",
        "edit_menu": "Подписка #{sub_id} — {username}\n\nЧто изменить?",
        "edit_template": "📝 Шаблон сообщения",
        "edit_image": "🖼 Обновить изображение",
        "edit_image_add": "🖼 Добавить изображение",
        "edit_image_update": "🖼 Обновить изображение",
        "edit_image_delete": "🗑 Удалить изображение",
        "edit_ignore_keywords": "🚫 Игнорировать ключевые слова",
        "edit_dest": "📍 Куда отправлять",
        "edit_delete_old": "🗑 Удалять старые",
        "edit_delete_fail_notify": "⚠️ Сообщать о проблемах удаления",
        "edit_delete_other": "🗑 Удалять и другие оповещения",
        "edit_link_preview": "🔗 Превью ссылок",
        "edit_delay": "⏱ Задержка отправки",
        "edit_repeat": "🔁 Повторные уведомления",
        "edit_schedule_reminder": "📅 Напоминания о стримах",
        "edit_schedule_reminder_prompt": (
            "Подписка #{sub_id}\n"
            "Сейчас: {current}\n\n"
            "Укажите за сколько минут напомнить до стрима (0 — отключить):"
        ),
        "edit_schedule_reminder_current_off": "выкл.",
        "edit_schedule_reminder_current": "за {minutes} мин.",
        "edit_schedule_reminder_invalid": "Введите 0 или положительное число минут.",
        "edit_schedule_reminder_no_schedule": (
            "У стримера больше нет расписания на Twitch.\n"
            "Напоминания по расписанию отключены."
        ),
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
            "Подписка #{sub_id}\n\n"
            "Текущий формат:\n"
            "<code>{current}</code>\n\n"
            "Как будет выглядеть:\n"
            "<code>{preview}</code>\n\n"
            "Отправьте новый шаблон сообщения.\n\n"
            "Пример ключевых слов:\n"
            "• <code>{{username}}</code> — имя канала\n"
            "• <code>{{game}}</code> — категория стрима\n"
            "• <code>{{name}}</code> — название стрима\n\n"
            "{placeholders_link}\n\n"
            "«Очистка названия» — убирает из названия стрима упоминания стримеров и команды."
        ),
        "edit_ignore_keywords_prompt": (
            "Подписка #{sub_id}\n"
            "Сейчас: {current}\n\n"
            "Отправьте ключевые слова через запятую.\n"
            "Поддерживается regexp (без учёта регистра), например "
            "<code>just.?chatting|irl</code>.\n"
            "Нажмите «Использовать глобальный список», чтобы применить "
            "Настройки → Игнорируемые слова и завершить.\n"
            "{hint}"
        ),
        "edit_ignore_keywords_hint_skip": (
            "Пустое сообщение или «Пропустить» — отключить фильтр."
        ),
        "edit_ignore_keywords_hint_cancel": (
            "«Отмена» — без изменений. Пустое сообщение — отключить фильтр."
        ),
        "ignore_keywords_cancel": "Отмена",
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
        "edit_delete_old_menu_category": (
            "Удалять предыдущее сообщение о смене категории при следующей смене?\n\n"
            "По умолчанию удаляются только оповещения о смене категории. "
            "Пункт «Удалять и другие оповещения» — чтобы убирать и остальные сообщения бота.\n"
            "Telegram позволяет удалять только сообщения младше ~48 часов."
        ),
        "edit_delete_fail_menu": "Сообщать о проблемах при удалении сообщения?",
        "edit_delete_other_menu": (
            "Удалять и другие оповещения бота по этому стримеру "
            "в том же месте публикации?"
        ),
        "edit_preview_menu": "Отключить превью ссылок в уведомлениях?",
        "preview_yes": "❌ Выкл (без превью)",
        "preview_no": "✅ Вкл (с превью)",
        "conflict_polling": (
            "Конфликт polling — возможно, запущено два экземпляра бота. Оставьте один."
        ),
        "network_transient": "Временная сетевая ошибка Telegram (будет повтор): {err}",
        "unhandled_error": "Необработанная ошибка: {err}",
        "broadcast_prompt": "Выберите тип оповещения:",
        "broadcast_type_bot_update": "📬 Оповещения об обновлении бота",
        "broadcast_type_availability": "📡 Оповещения о доступности бота",
        "broadcast_type_other": "📢 Прочие",
        "broadcast_audience_prompt": "Кому отправить сообщение?",
        "broadcast_audience_all": "Разослать всем",
        "broadcast_audience_ids": "Указать ID",
        "broadcast_ids_prompt": (
            "Отправьте Telegram ID получателей через запятую.\n"
            "Пример: 123456789, 987654321\n"
            "/cancel — отмена."
        ),
        "broadcast_ids_invalid": "Не найдено валидных ID. Отправьте числа через запятую.",
        "broadcast_text_prompt": (
            "Отправьте текст сообщения (жирный/курсив и переносы сохраняются).\n"
            "Оно будет автоматически переведено на язык каждого получателя.\n"
            "/cancel — отмена."
        ),
        "broadcast_empty": "Сообщение не может быть пустым.",
        "broadcast_done": (
            "Рассылка завершена.\n"
            "Доставлено: {sent}\n"
            "Всего получателей: {total}\n"
            "Заблокировали бота: {blocked_users}"
        ),
        "broadcast_started": (
            "Рассылка запущена. Бот продолжает работать; статистика придёт по завершении."
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
            "Текущий текст:\n{text}\n\n"
            "Отправьте новый текст для сообщения #{id}.\n"
            "/cancel — отмена."
        ),
        "scheduled_edit_text_ask": (
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
        "sys_notifications_menu": (
            "Настройка системных уведомлений:\n\n"
            "Оповещения о доступности также включают падения Twitch "
            "(status.twitch.com)."
        ),
        "sys_updates_label": "Оповещения об обновлении бота",
        "sys_availability_label": "Оповещения о доступности (бот / Twitch)",
        "sys_other_label": "Прочие оповещения",
        "sys_sync_label": "Оповещения о синхронизации",
        "twitch_status_title": "📡 Twitch Status",
        "posthog_status_title": "🦔 PostHog Status (US Cloud)",
        "twitch_indicator_none": "✅ Все системы в норме",
        "twitch_indicator_minor": "⚠️ Незначительные проблемы",
        "twitch_indicator_major": "🟠 Серьёзные проблемы",
        "twitch_indicator_critical": "🔴 Критический сбой",
        "twitch_indicator_maintenance": "🛠 Техработы",
        "twitch_comp_operational": "Работает",
        "twitch_comp_degraded": "Снижена производительность",
        "twitch_comp_partial": "Частичный сбой",
        "twitch_comp_major": "Крупный сбой",
        "twitch_comp_maintenance": "Техработы",
        "twitch_status_affected": "Затронутые компоненты:",
        "twitch_status_incidents": "Инциденты:",
        "bot_stats": (
            "📊 Статистика бота\n\n"
            "Пользователей: {users}\n"
            "Создали оповещение: {unique_owners}\n"
            "Получателей: {notify_users}\n"
            "Подписок: {subscriptions_total} "
            "(✅ {subscriptions_enabled} / ⏸ {subscriptions_disabled})\n"
            "Каналов Twitch: {unique_twitch_channels}\n"
            "Платный премиум: {premium_paid}\n\n"
            "Системные оповещения:\n"
            "• Обновление бота: {sys_updates}\n"
            "• Доступность бота: {sys_availability}\n"
            "• Прочие: {sys_other}\n"
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


def oferta_url() -> str:
    from config import PUBLIC_BASE_URL

    if not PUBLIC_BASE_URL:
        return ""
    return f"{PUBLIC_BASE_URL}/oferta"


def with_premium_oferta(
    lang: str, markup: InlineKeyboardMarkup | None
) -> InlineKeyboardMarkup | None:
    """Append RU-only «Оферта» URL button at the end of the Premium keyboard."""
    if lang != "ru":
        return markup
    url = oferta_url()
    if not url:
        return markup
    row = [InlineKeyboardButton(btn("premium_oferta", lang), url=url)]
    if markup is None:
        return InlineKeyboardMarkup([row])
    rows = [list(r) for r in markup.inline_keyboard]
    rows.append(row)
    return InlineKeyboardMarkup(rows)


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
        "stats",
        "back",
        "sys_notifications",
        "ignored_words",
        "whisper_alerts",
        "advanced_mode",
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
        "watch",
    )
    return {btn(k, loc) for k in keys for loc in SUPPORTED_LOCALES}


def all_wizard_nav_buttons() -> set[str]:
    return {btn(k, loc) for k in ("wizard_back", "wizard_cancel") for loc in SUPPORTED_LOCALES}


def main_menu(
    lang: str, *, is_admin: bool = False, demo_active: bool = False
) -> ReplyKeyboardMarkup:
    rows = [
        [
            KeyboardButton(btn("new", lang)),
            KeyboardButton(btn("import_twitch", lang)),
        ],
        [
            KeyboardButton(btn("manage", lang)),
            KeyboardButton(btn("alert_history", lang)),
        ],
        [
            KeyboardButton(btn("other", lang)),
        ],
        [
            KeyboardButton(btn("settings", lang)),
            KeyboardButton(btn("feedback", lang)),
        ],
    ]
    if demo_active:
        rows.append([KeyboardButton(btn("demo", lang))])
    elif is_admin:
        rows.append([KeyboardButton(btn("admin", lang))])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def subscriptions_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(btn("list", lang)),
                KeyboardButton(btn("edit", lang)),
            ],
            [
                KeyboardButton(btn("delete", lang)),
                KeyboardButton(btn("back", lang)),
            ],
        ],
        resize_keyboard=True,
    )


def other_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(btn("whisper_alerts", lang))],
            [KeyboardButton(btn("create_schedule", lang))],
            [KeyboardButton(btn("watch", lang))],
            [KeyboardButton(btn("back", lang))],
        ],
        resize_keyboard=True,
    )


def settings_menu(
    lang: str, *, beta_enrolled: int = 0, beta_total: int = 0
) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(btn("premium", lang)),
                KeyboardButton(btn("sync_subs", lang)),
            ],
            [
                KeyboardButton(btn("ignored_words", lang)),
                KeyboardButton(btn("advanced_mode", lang)),
            ],
            [
                KeyboardButton(beta_mode_btn(lang, beta_enrolled, beta_total)),
                KeyboardButton(btn("sys_notifications", lang)),
            ],
            [
                KeyboardButton(btn("language", lang)),
                KeyboardButton(btn("partner", lang)),
            ],
            [
                KeyboardButton(btn("back", lang)),
            ],
        ],
        resize_keyboard=True,
    )


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
    from premium import FEATURE_IDS, feature_label_key, stars_feature_price

    owned = owned or set()
    rows: list[list[InlineKeyboardButton]] = []
    for fid in FEATURE_IDS:
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
    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(btn("broadcast", lang)),
                KeyboardButton(btn("stats", lang)),
            ],
            [
                KeyboardButton(btn("admin_withdrawals", lang)),
            ],
            [KeyboardButton(btn("demo", lang))],
            [KeyboardButton(btn("back", lang))],
        ],
        resize_keyboard=True,
    )


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


def watch_cats_nav_keyboard(lang: str, *, has_cats: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                t("watch_cats_lucky", lang), callback_data="watch_cat:lucky"
            )
        ]
    ]
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


def advanced_mode_keyboard(lang: str, *, enabled: bool) -> InlineKeyboardMarkup:
    mark = "✅ " if enabled else "⬜️ "
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    mark + t("advanced_mode_activate", lang),
                    callback_data="advanced_mode:toggle",
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
        ]
    )


def template_typo_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("delay_yes", lang), callback_data="template_typo:1")],
            [InlineKeyboardButton(t("delay_no", lang), callback_data="template_typo:0")],
        ]
    )


def lucky_start_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t("lucky_btn", lang), callback_data="lucky:go")]]
    )


def template_strip_keyboard(
    lang: str,
    *,
    enabled: bool = False,
    show_lucky: bool = False,
    show_back: bool = False,
    show_cancel: bool = False,
) -> InlineKeyboardMarkup:
    mark = "✅ " if enabled else "❌ "
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                mark + t("strip_name_label", lang),
                callback_data="strip_name:toggle",
            )
        ],
    ]
    if show_lucky:
        rows.append(
            [InlineKeyboardButton(t("lucky_btn", lang), callback_data="lucky:go")]
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


def lucky_preview_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("lucky_continue", lang), callback_data="lucky:continue")],
            [InlineKeyboardButton(t("lucky_again", lang), callback_data="lucky:go")],
            [InlineKeyboardButton(t("lucky_full_wizard", lang), callback_data="lucky:full")],
        ]
    )


def image_ask_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("image_add", lang), callback_data="image_ask:add")],
            [InlineKeyboardButton(t("image_skip", lang), callback_data="image_ask:skip")],
        ]
    )


def image_edit_keyboard(lang: str, *, has_image: bool) -> InlineKeyboardMarkup:
    if has_image:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t("edit_image_replace", lang),
                        callback_data="image_ask:add",
                    )
                ],
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
    return image_ask_keyboard(lang)


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
    has_image: bool = False,
    show_link_preview: bool = True,
    schedule_reminder_configured: bool = False,
    notify_on_category_change: bool = False,
    notify_on_end: bool = False,
    is_upcoming: bool = False,
    show_advanced: bool = True,
) -> InlineKeyboardMarkup:
    # Same order as create wizard: template → image → ignore → preview → delay
    # → repeat → schedule reminder → dest → delete.
    # Schedule reminder only if configured at creation (unchanged policy).
    # Delay/repeat skipped for upcoming; repeat also skipped for category/end.
    image_label = t("edit_image_update", lang) if has_image else t("edit_image_add", lang)
    rows = [
        [InlineKeyboardButton(t("edit_template", lang), callback_data=f"edit_f:{sub_id}:template")],
        [InlineKeyboardButton(image_label, callback_data=f"edit_f:{sub_id}:image")],
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
    if show_advanced:
        rows.append(
            [
                InlineKeyboardButton(
                    t("edit_ignore_keywords", lang),
                    callback_data=f"edit_f:{sub_id}:ignore_keywords",
                )
            ]
        )
    if show_link_preview:
        rows.append(
            [InlineKeyboardButton(t("edit_link_preview", lang), callback_data=f"edit_f:{sub_id}:preview")]
        )
    if show_advanced and not is_upcoming:
        rows.append(
            [InlineKeyboardButton(t("edit_delay", lang), callback_data=f"edit_f:{sub_id}:delay")]
        )
        if not notify_on_category_change and not notify_on_end:
            rows.append(
                [InlineKeyboardButton(t("edit_repeat", lang), callback_data=f"edit_f:{sub_id}:repeat")]
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
    if show_advanced and dest_type != "dm":
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
            if notify_on_category_change:
                rows.append(
                    [
                        InlineKeyboardButton(
                            t("edit_delete_other", lang),
                            callback_data=f"edit_f:{sub_id}:delete_other",
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
