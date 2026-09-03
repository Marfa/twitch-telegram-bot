from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from .models import (
    AlertHistoryEntry,
    BotStats,
    ChatAuth,
    DeletedSubscriptionCartItem,
    PremiumChannel,
    ReferralCreditRef,
    ReferralStats,
    ReferralWithdrawal,
    ScheduledBroadcast,
    Subscription,
    TwitchSync,
    WatchFilter,
    WatchPrefs,
    WhisperAlert,
)

class Database(Protocol):
    def add_subscription(
        self,
        owner_id: int,
        twitch_username: str,
        twitch_user_id: str,
        message_template: str,
        dest_type: str,
        chat_id: int,
        thread_id: int | None,
        delete_previous: bool = False,
        notify_delete_fail: bool = False,
        disable_link_preview: bool = False,
        strip_name_mentions: bool = False,
        attach_chat_button: bool = False,
        delay_minutes: int = 0,
        suppress_repeat_minutes: int = 0,
        schedule_reminder_minutes: int = 0,
        schedule_reminder_configured: bool = False,
        ignore_keywords: str = "",
        use_global_ignore: bool = False,
        image_file_id: str | None = None,
        image_position: str = "",
        enabled: bool = True,
        from_twitch_sync: bool = False,
        from_watch_suggest: bool = False,
        category_watch_prefs: str = "",
        notify_on_live: bool = True,
        notify_on_end: bool = False,
        notify_on_category_change: bool = False,
        delete_other_alerts: bool = False,
        is_demo: bool = False,
    ) -> int: ...

    def set_last_message_id(self, sub_id: int, message_id: int | None) -> None: ...

    def set_notify_cooldown(self, sub_id: int, minutes: int) -> None: ...

    def set_last_schedule_reminder_segment(
        self, sub_id: int, segment_id: str
    ) -> None: ...

    def get_subscription_by_id(self, sub_id: int) -> Subscription | None: ...

    def get_subscriptions_by_owner(self, owner_id: int) -> list[Subscription]: ...

    def get_subscription(self, sub_id: int, owner_id: int) -> Subscription | None: ...

    def get_unique_schedule_reminder_twitch_ids(self) -> list[str]: ...

    def toggle_subscription(self, sub_id: int, owner_id: int) -> bool | None: ...

    def enable_all_subscriptions(
        self, owner_id: int, *, demo: bool = False, max_count: int | None = None
    ) -> int: ...

    def delete_subscription(self, sub_id: int, owner_id: int, *, to_cart: bool = True) -> bool: ...

    def list_deleted_subscriptions(
        self,
        owner_id: int,
        *,
        days: int,
        is_demo: bool,
        limit: int = 100,
    ) -> list[DeletedSubscriptionCartItem]: ...

    def restore_deleted_subscriptions(
        self,
        owner_id: int,
        cart_ids: list[int],
        *,
        days: int,
        is_demo: bool,
        max_enabled: int | None = None,
    ) -> tuple[int, int]: ...

    def update_subscription(self, sub_id: int, owner_id: int, **fields: object) -> bool: ...

    def get_user_locale(self, user_id: int) -> str | None: ...

    def get_user_locales(self, user_ids: list[int]) -> dict[int, str | None]: ...

    def set_user_locale(self, user_id: int, locale: str) -> None: ...

    def get_unique_twitch_user_ids(self) -> list[str]: ...

    def get_enabled_category_watch_subscriptions(self) -> list[Subscription]: ...

    def set_category_watch_live_state(
        self, sub_id: int, live_ids: list[str], *, primed: bool
    ) -> None: ...

    def get_enabled_by_twitch_user_id(self, twitch_user_id: str) -> list[Subscription]: ...

    def get_all_owner_ids(self) -> list[int]: ...

    def upsert_user(self, user_id: int) -> None: ...

    def user_exists(self, user_id: int) -> bool: ...

    def count_new_users_since(self, since: datetime) -> int: ...

    def count_stars_payers_since(self, since: datetime) -> int: ...

    def list_active_trial_users(self, *, now_unix: int | None = None) -> list[tuple[int, int]]: ...

    def list_expired_trial_users(self, *, now_unix: int | None = None) -> list[tuple[int, int]]: ...

    def set_referred_by(self, user_id: int, referrer_id: int) -> bool: ...

    def get_referred_by(self, user_id: int) -> int | None: ...

    def add_referral_credit(
        self,
        *,
        referrer_id: int,
        invitee_id: int,
        charge_id: str,
        stars_paid: int,
        commission_stars: int,
    ) -> bool: ...

    def get_referral_stats(self, user_id: int) -> ReferralStats: ...

    def request_referral_withdrawal(self, user_id: int, amount: int) -> int | None: ...

    def get_referral_withdrawal(self, withdrawal_id: int) -> ReferralWithdrawal | None: ...

    def list_referral_withdrawals(
        self, user_id: int, *, limit: int = 20
    ) -> list[ReferralWithdrawal]: ...

    def list_pending_referral_withdrawals(self) -> list[ReferralWithdrawal]: ...

    def add_alert_history(
        self,
        owner_id: int,
        *,
        subscription_id: int | None,
        twitch_username: str,
        alert_type: str,
        message_text: str = "",
        twitch_user_id: str = "",
        stream_id: str = "",
        vod_id: str = "",
        vod_offset_seconds: int | None = None,
    ) -> None: ...

    def set_alert_history_vod_id(self, history_id: int, vod_id: str) -> None: ...

    def set_alert_history_viewed(
        self, owner_id: int, history_id: int, *, viewed: bool
    ) -> bool: ...

    def set_alert_history_viewed_below(
        self, owner_id: int, history_id: int, *, viewed: bool = True
    ) -> int: ...

    def list_alert_history(
        self,
        owner_id: int,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[AlertHistoryEntry]: ...

    def resolve_referral_withdrawal(
        self, withdrawal_id: int, status: str
    ) -> ReferralWithdrawal | None: ...

    def set_bot_blocked(self, user_id: int, blocked: bool) -> None: ...

    def is_bot_blocked(self, user_id: int) -> bool: ...

    def get_bot_blocked_at(self, user_id: int) -> int | None: ...

    def set_bot_blocked_at(self, user_id: int, blocked_at_unix: int) -> None: ...

    def list_blocked_user_ids(self) -> list[int]: ...

    def delete_user_data(self, user_id: int) -> bool: ...

    def purge_expired_blocked_users(
        self, *, now_unix: int | None = None, retention_days: int | None = None
    ) -> int: ...

    def set_chat_unreachable(self, chat_id: int, unreachable: bool) -> None: ...

    def is_chat_unreachable(self, chat_id: int) -> bool: ...

    def pause_delivery_for_chat(self, chat_id: int) -> int: ...

    def list_delivery_paused_for_chat(self, chat_id: int) -> list[Subscription]: ...

    def clear_delivery_paused(self, sub_id: int, *, enabled: bool) -> None: ...

    def get_notify_user_ids(self) -> list[int]: ...

    def get_bot_update_recipients(self) -> list[int]: ...

    def get_availability_recipients(self) -> list[int]: ...

    def get_other_recipients(self) -> list[int]: ...

    def get_receive_bot_updates(self, user_id: int) -> bool: ...

    def set_receive_bot_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_receive_availability_updates(self, user_id: int) -> bool: ...

    def set_receive_availability_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_receive_other_updates(self, user_id: int) -> bool: ...

    def set_receive_other_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_receive_sync_updates(self, user_id: int) -> bool: ...

    def set_receive_sync_updates(self, user_id: int, enabled: bool) -> None: ...

    def get_notifications_paused_until(self, user_id: int) -> int: ...

    def set_notifications_paused_until(self, user_id: int, until_ts: int) -> None: ...

    def mark_template_typo_notice_sent(self, user_id: int) -> bool: ...

    def get_global_ignore_keywords(self, user_id: int) -> str: ...

    def set_global_ignore_keywords(self, user_id: int, keywords: str) -> None: ...

    def get_advanced_mode_setting(self, user_id: int) -> bool | None: ...

    def set_advanced_mode_setting(self, user_id: int, enabled: bool) -> None: ...

    def owner_has_advanced_subscription_options(self, owner_id: int) -> bool: ...

    def get_saved_schedule(self, user_id: int) -> tuple[int | None, int | None]: ...

    def set_saved_schedule(self, user_id: int, hour: int, minute: int) -> None: ...

    def get_schedule_utc_offset_minutes(self, user_id: int) -> int | None: ...

    def set_schedule_utc_offset_minutes(self, user_id: int, offset_minutes: int) -> None: ...

    def get_schedule_utc_offsets_for_users(
        self, user_ids: list[int]
    ) -> dict[int, int | None]: ...

    def record_broadcast_offset_sent(
        self, broadcast_id: int, utc_offset_minutes: int, sent: int
    ) -> None: ...

    def get_broadcast_sent_offsets(self, broadcast_id: int) -> set[int]: ...

    def reset_broadcast_send_progress(self, broadcast_id: int) -> None: ...

    def get_broadcast_sent_count(self, broadcast_id: int) -> int: ...

    def get_watch_prefs(self, user_id: int) -> WatchPrefs | None: ...

    def set_watch_prefs(self, user_id: int, prefs: WatchPrefs) -> None: ...

    def clear_watch_prefs(self, user_id: int) -> None: ...

    def get_watch_filters(self, user_id: int) -> list[WatchFilter]: ...

    def set_watch_filters(self, user_id: int, filters: list[WatchFilter]) -> None: ...

    def add_watch_filter(
        self, user_id: int, prefs: WatchPrefs, *, name: str | None = None
    ) -> WatchFilter: ...

    def delete_watch_filter(self, user_id: int, filter_id: str) -> bool: ...

    def count_enabled_subscriptions(self, owner_id: int, *, demo: bool = False) -> int: ...

    def delete_demo_subscriptions(self, owner_id: int) -> int: ...

    def delete_whisper_alert(self, owner_id: int) -> None: ...

    def delete_chat_auth(self, owner_id: int) -> None: ...

    def get_premium_status(self, user_id: int) -> Any: ...

    def set_premium_stars(
        self,
        user_id: int,
        *,
        charge_id: str,
        until_unix: int,
        canceled: bool,
    ) -> None: ...

    def set_premium_stars_canceled(self, user_id: int, canceled: bool) -> None: ...

    def set_premium_permanent(self, user_id: int, permanent: bool) -> None: ...

    def clear_premium(self, user_id: int) -> None: ...

    def set_premium_trial(
        self, user_id: int, *, until_unix: int, used: bool = True
    ) -> None: ...

    def expire_premium_trial(self, user_id: int) -> int: ...

    def extend_premium_features(
        self,
        user_id: int,
        feature_ids: list[str],
        *,
        until_unix: int,
        charge_id: str = "",
    ) -> None: ...

    def clear_premium_feature(self, user_id: int, feature_id: str) -> None: ...

    def set_premium_feature_canceled(self, user_id: int, feature_id: str) -> None: ...

    def set_premium_twitch(
        self,
        user_id: int,
        *,
        active: bool,
        twitch_user_id: str | None = None,
        refresh_token: str | None = None,
    ) -> None: ...

    def set_premium_twitch_refresh(self, user_id: int, refresh_token: str) -> None: ...

    def get_premium_twitch_refresh(self, user_id: int) -> str | None: ...

    def list_premium_twitch_user_ids(self) -> list[int]: ...

    def add_scheduled_broadcast(
        self,
        msg_type: str,
        text: str,
        scheduled_at: str,
        created_by: int,
        recipient_ids: str = "",
    ) -> int: ...

    def get_pending_scheduled_broadcasts(self) -> list[ScheduledBroadcast]: ...

    def get_unsent_scheduled_broadcasts(self) -> list[ScheduledBroadcast]: ...

    def get_scheduled_broadcast(self, broadcast_id: int) -> ScheduledBroadcast | None: ...

    def update_scheduled_broadcast(self, broadcast_id: int, **fields: object) -> bool: ...

    def delete_scheduled_broadcast(self, broadcast_id: int) -> bool: ...

    def mark_scheduled_broadcast_sent(self, broadcast_id: int) -> None: ...

    def get_sent_broadcasts(self, *, retention_days: int = 30) -> list[ScheduledBroadcast]: ...

    def purge_old_sent_broadcasts(self, *, retention_days: int = 30) -> int: ...

    def add_broadcast_delivery(
        self, broadcast_id: int, user_id: int, message_id: int
    ) -> None: ...

    def get_broadcast_deliveries(self, broadcast_id: int) -> list[tuple[int, int]]: ...

    def get_broadcast_feedback_vote(
        self, broadcast_id: int, user_id: int
    ) -> int | None: ...

    def set_broadcast_feedback(
        self, broadcast_id: int, user_id: int, vote: int
    ) -> None: ...

    def clear_broadcast_feedback(self, broadcast_id: int, user_id: int) -> None: ...

    def get_broadcast_feedback_counts(self, broadcast_id: int) -> tuple[int, int]: ...

    def add_lucky_template(self, locale: str, text: str) -> None: ...

    def pick_lucky_template(self, locale: str) -> str | None: ...

    def get_bot_stats(self) -> BotStats: ...

    def upsert_twitch_sync(
        self,
        owner_id: int,
        twitch_user_id: str,
        refresh_token: str,
        period_days: int,
        next_sync_at: str,
        last_sync_at: str | None = None,
    ) -> None: ...

    def get_twitch_sync(self, owner_id: int) -> TwitchSync | None: ...

    def delete_twitch_sync(self, owner_id: int) -> bool: ...

    def set_twitch_sync_period(
        self, owner_id: int, period_days: int, next_sync_at: str
    ) -> bool: ...

    def update_twitch_sync_tokens(
        self,
        owner_id: int,
        refresh_token: str,
        *,
        last_sync_at: str,
        next_sync_at: str,
    ) -> None: ...

    def get_due_twitch_syncs(self, now_iso: str) -> list[TwitchSync]: ...

    def get_whisper_alert(self, owner_id: int) -> WhisperAlert | None: ...

    def get_whisper_alerts_by_twitch_user_id(
        self, twitch_user_id: str
    ) -> list[WhisperAlert]: ...

    def upsert_whisper_alert(
        self,
        owner_id: int,
        *,
        enabled: bool,
        twitch_user_id: str,
        twitch_login: str,
        refresh_token: str,
        eventsub_id: str = "",
    ) -> None: ...

    def set_whisper_alert_enabled(
        self,
        owner_id: int,
        enabled: bool,
        *,
        eventsub_id: str | None = None,
    ) -> None: ...

    def disable_whisper_alerts_for_twitch_user(self, twitch_user_id: str) -> list[int]: ...

    def get_chat_auth(self, owner_id: int) -> ChatAuth | None: ...

    def upsert_chat_auth(
        self,
        owner_id: int,
        *,
        twitch_user_id: str,
        twitch_login: str,
        refresh_token: str,
    ) -> None: ...

    def get_chat_send_count(self, owner_id: int, day: str) -> int: ...

    def increment_chat_send_count(self, owner_id: int, day: str) -> int: ...

    def delete_synced_subscriptions_missing(
        self, owner_id: int, keep_twitch_user_ids: set[str], *, to_cart: bool = True
    ) -> list[str]: ...

    def get_unfollowed_manual_alert_streamers(
        self,
        owner_id: int,
        keep_twitch_user_ids: set[str],
        *,
        is_demo: bool = False,
    ) -> list[dict[str, str]]: ...

    def delete_subscriptions_for_twitch_users(
        self,
        owner_id: int,
        twitch_user_ids: set[str],
        *,
        is_demo: bool = False,
        to_cart: bool = True,
    ) -> int: ...

    def beta_enrollment_explicit(self, user_id: int, feature_id: str) -> bool | None: ...

    def set_beta_enrollment(
        self, user_id: int, feature_id: str, enrolled: bool
    ) -> None: ...

    def clear_beta_enrollment(self, user_id: int, feature_id: str) -> None: ...

    def list_beta_enrolled_user_ids(self, feature_ids: list[str]) -> list[int]: ...

    def is_premium_channel_login(self, login: str) -> bool: ...

    def list_premium_channel_logins(self) -> list[str]: ...

    def list_premium_channels(self) -> list[PremiumChannel]: ...

    def get_premium_channel(self, twitch_user_id: str) -> PremiumChannel | None: ...

    def upsert_premium_channel(
        self,
        *,
        twitch_user_id: str,
        twitch_login: str,
        display_name: str,
        owner_telegram_id: int,
        charge_id: str,
    ) -> None: ...

    def get_premium_channel_by_charge(self, charge_id: str) -> PremiumChannel | None: ...

    def delete_premium_channel_by_charge(self, charge_id: str) -> bool: ...

    def find_user_id_by_premium_charge(self, charge_id: str) -> int | None: ...

    def get_referral_credit_by_charge(
        self, charge_id: str
    ) -> ReferralCreditRef | None: ...

    def delete_referral_credit_by_charge(self, charge_id: str) -> bool: ...

    def ensure_alert_share_token(
        self, owner_id: int, source_sub_id: int, snapshot: dict[str, Any]
    ) -> str: ...

    def get_alert_share_snapshot(self, token: str) -> dict[str, Any] | None: ...
