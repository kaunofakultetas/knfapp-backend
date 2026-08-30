############################################################
#  [*] Notifications — package marker for Expo push
#
#  No code of its own; two modules live here:
#
#    push.py    — the Expo push client (exp.host
#                 /--/api/v2/push/send). send_push_notification
#                 and send_push_batch do the HTTP;
#                 notify_channel_user, notify_channel_users and
#                 notify_channel pick tokens from push_tokens
#                 (migration v6) and all honour the opt-OUT rows
#                 in notification_channels (v7) — the
#                 channel-blind notify_user / notify_all_users
#                 are gone, they were a footgun with no caller.
#                 A "DeviceNotRegistered" verdict, from a ticket
#                 or from poll_push_receipts, flips the token
#                 inactive; prune_orphan_push_tokens drops the
#                 rows whose owner has no live session. Both
#                 jobs are run by the scraper scheduler.
#    routes.py  — token registration and the per-channel
#                 switches for the logged-in user; channels are
#                 news, chat, schedule, admin (push.py's
#                 VALID_CHANNELS); every route is require_auth
#                 and every write route is rate limited
#
#    POST   /api/notifications/register — store a device token
#    DELETE /api/notifications/register — drop it
#    GET    /api/notifications/channels — the caller's opt-ins
#    PUT    /api/notifications/channels — save them
#
#  Used by:
#    - app/__init__.py — registers notifications_bp at
#      /api/notifications (via app.notifications.routes)
#    - chat/routes.py — notify_channel_users, "chat", on a new
#      message, off the request thread
#    - scraper/knf_scraper.py, vu_scraper.py — notify_channel,
#      "news"; scraper/schedule_scraper.py — "schedule"
#    - admin/routes.py — notify_channel, "admin" broadcasts
#    - scraper/scheduler.py — poll_push_receipts every 15 min
#      and prune_orphan_push_tokens daily
#    - mobile services/notifications.ts → services/api/
#      notifications.ts — register/unregister the device token
#      and the channel switches
############################################################
