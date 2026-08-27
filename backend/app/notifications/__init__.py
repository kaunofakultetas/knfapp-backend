############################################################
#  [*] Notifications — package marker for Expo push
#
#  No code of its own; two modules live here:
#
#    push.py    — the Expo push client (exp.host
#                 /--/api/v2/push/send). send_push_notification
#                 and send_push_batch do the HTTP; notify_user,
#                 notify_all_users, notify_channel_user and
#                 notify_channel pick tokens from push_tokens
#                 (migration v6), the channel variants honouring
#                 the opt-OUT rows in notification_channels
#                 (v7). A "DeviceNotRegistered" ticket flips the
#                 token inactive. notify_user and
#                 notify_all_users have no caller today (the
#                 latter is a dead import in routes.py).
#    routes.py  — token registration and the per-channel
#                 switches for the logged-in user; channels are
#                 news, chat, schedule, admin; every route is
#                 require_auth
#
#    POST   /api/notifications/register — store a device token
#    DELETE /api/notifications/register — drop it
#    GET    /api/notifications/channels — the caller's opt-ins
#    PUT    /api/notifications/channels — save them
#
#  Used by:
#    - app/__init__.py — registers notifications_bp at
#      /api/notifications (via app.notifications.routes)
#    - chat/routes.py — notify_channel_user, "chat", on a new
#      message
#    - scraper/knf_scraper.py, vu_scraper.py — notify_channel,
#      "news"; scraper/schedule_scraper.py — "schedule"
#    - admin/routes.py — notify_channel, "admin" broadcasts
#    - mobile services/notifications.ts → services/api/
#      notifications.ts — register/unregister the device token
#      and the channel switches
############################################################
