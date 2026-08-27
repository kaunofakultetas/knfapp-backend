############################################################
#  [*] Notifications — Expo push delivery and fan-out
#
#  The only path from the backend to a phone: every helper
#  here ends in a POST to Expo's push service
#  (exp.host/--/api/v2/push/send), which relays to APNs and
#  FCM. Tokens arrive through POST /api/notifications/
#  register (notifications/routes.py) and live in
#  push_tokens; a "DeviceNotRegistered" ticket flips the
#  row to active=0 and the next register from that device
#  flips it back.
#
#  Return values count Expo TICKETS ("ok" = Expo accepted
#  the message), not deliveries — push receipts are never
#  fetched, so an accepted message can still die at APNs/
#  FCM unnoticed. No retries, no backoff, no Expo access
#  token header (the Expo project has to keep enhanced push
#  security off). Failures are logged and swallowed: push
#  is best-effort everywhere and never fails a request.
#
#  Three tiers of helpers:
#    send_push_notification / send_push_batch — raw Expo
#      calls (one ticket / slices of 100)
#    notify_user / notify_all_users — every active token,
#      channel preferences IGNORED (both currently unused)
#    notify_channel_user / notify_channel — honour the
#      per-user opt-out in notification_channels: a
#      missing row means ENABLED, only an explicit
#      enabled=0 suppresses (opt-out model, migration v7);
#      data["channel"] is stamped on the payload
#
#  Payload contract with the app: app/_layout.tsx routes a
#  tapped notification on data.type — "chat_message" (+
#  conversationId) opens the room, "news" and
#  "admin_announcement" open the news tab. Nothing in the
#  app reads the "channel" stamp yet.
############################################################


import logging
from typing import Optional

import requests

from app.database import get_db

logger = logging.getLogger(__name__)

# One URL for both shapes: a single message object or an
# array of up to 100 of them
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"








############################################################
# send_push_notification
############################################################
#
# One message to one token, 10 s timeout, True only for an
# "ok" ticket. The body is parsed BEFORE the status code is
# checked, so a non-JSON error page from Expo (a 5xx) lands
# in the generic except and is logged as "Failed to send",
# never as the "HTTP <code>" warning — that branch is only
# reachable when a non-200 response is still valid JSON.
# A "DeviceNotRegistered" ticket deactivates the token; the
# log line shows token[:20], which is "ExponentPushToken["
# plus two characters and identifies almost nothing. Only
# this sender takes a badge count — the batch sender does
# not.
#
# Used by:
#   - nothing calls this at the moment — every notify_*
#     helper goes through send_push_batch, even for a
#     single device
############################################################

def send_push_notification(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
    badge: Optional[int] = None,
) -> bool:
    message = {
        "to": token,
        "title": title,
        "body": body,
        "sound": "default",
    }
    if data:
        message["data"] = data
    if badge is not None:
        message["badge"] = badge

    try:
        resp = requests.post(
            EXPO_PUSH_URL,
            json=message,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        # Parsed before the status check — a non-JSON body
        # jumps straight to the except below (see the banner)
        result = resp.json()
        # A 200 still carries the per-message verdict in
        # data.status; details.error names the reason
        if resp.status_code == 200:
            status = result.get("data", {}).get("status")
            if status == "error":
                detail = result.get("data", {}).get("details", {})
                error_type = detail.get("error")
                if error_type == "DeviceNotRegistered":
                    _deactivate_token(token)
                    logger.info("Deactivated unregistered token: %s...", token[:20])
                else:
                    logger.warning("Expo push error: %s", result)
                return False
            return True
        logger.warning("Expo push HTTP %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.exception("Failed to send push notification")
        return False








############################################################
# send_push_batch
############################################################
#
# The same title/body to many tokens, POSTed in slices of
# 100 (Expo's per-request cap), 30 s timeout per slice.
# Returns the number of "ok" tickets. Tickets come back in
# request order — the only reason batch[idx] can map a
# "DeviceNotRegistered" ticket back to its token. Other
# per-ticket errors (MessageTooBig, rate limits, ...) are
# counted as not-sent and otherwise SILENT here, unlike the
# single sender which logs them. A failed slice (non-200 or
# exception) is logged and skipped; later slices still go
# out. The same `data` dict object rides in every message
# of the batch — fine, it is only serialised.
#
# Used by:
#   - notify_user, notify_all_users, notify_channel_user,
#     notify_channel (below)
############################################################

def send_push_batch(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> int:
    if not tokens:
        return 0


    # STEP 1: one message per token, identical apart from "to"
    # ========================================================
    messages = []
    for token in tokens:
        msg = {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
        }
        if data:
            msg["data"] = data
        messages.append(msg)


    # STEP 2: POST in slices of 100 and count the "ok" tickets;
    # tickets arrive in request order, hence batch[idx]
    # =========================================================
    sent = 0
    for i in range(0, len(messages), 100):
        batch = messages[i : i + 100]
        try:
            resp = requests.post(
                EXPO_PUSH_URL,
                json=batch,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                results = resp.json().get("data", [])
                for idx, r in enumerate(results):
                    if r.get("status") == "ok":
                        sent += 1
                    elif r.get("status") == "error":
                        # Only the dead-device case is acted on;
                        # every other error is dropped unlogged
                        detail = r.get("details", {})
                        if detail.get("error") == "DeviceNotRegistered":
                            _deactivate_token(batch[idx]["to"])
            else:
                logger.warning("Expo batch push HTTP %d", resp.status_code)
        except Exception:
            logger.exception("Failed to send push batch")

    # Per token, not per slice
    logger.info("Push batch: %d/%d accepted", sent, len(tokens))
    return sent








############################################################
# _deactivate_token
############################################################
#
# Flips push_tokens.active to 0 for a token Expo reported
# as "DeviceNotRegistered" (app uninstalled, permission
# revoked). token is UNIQUE (idx_push_tokens_token), so at
# most one row moves. The row is kept, not deleted: the
# next POST /api/notifications/register from that device
# (notifications/routes.py, register_token) finds it and
# sets active=1 again. Opens its own connection per call —
# a batch with many dead devices opens one connection each,
# from inside the sender's ticket loop. Never raises; the
# outer try also covers get_db() itself failing.
#
# Used by:
#   - send_push_notification, send_push_batch (above)
############################################################

def _deactivate_token(token: str):
    try:
        db = get_db()
        try:
            db.execute(
                "UPDATE push_tokens SET active = 0 WHERE token = ?",
                (token,),
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to deactivate token")








############################################################
# notify_user
############################################################
#
# Every active device of one user with NO channel check —
# the user's notification_channels opt-outs are ignored.
# Returns accepted tickets (devices, not users).
#
# Used by:
#   - nothing calls this at the moment — chat/routes.py
#     went with notify_channel_user so the "chat" opt-out
#     is honoured
############################################################

def notify_user(user_id: str, title: str, body: str, data: Optional[dict] = None) -> int:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT token FROM push_tokens WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchall()
    finally:
        db.close()

    tokens = [r["token"] for r in rows]
    if not tokens:
        return 0

    return send_push_batch(tokens, title, body, data)








############################################################
# notify_all_users
############################################################
#
# Every active token in the table, optionally minus one
# user's devices, again ignoring channel opt-outs. The
# DISTINCT is redundant — token is UNIQUE — but harmless.
#
# Used by:
#   - nothing calls this at the moment — notifications/
#     routes.py imports it and never uses it (dead import);
#     admin broadcasts go through notify_channel("admin")
############################################################

def notify_all_users(title: str, body: str, data: Optional[dict] = None, exclude_user_id: Optional[str] = None) -> int:
    db = get_db()
    try:
        if exclude_user_id:
            rows = db.execute(
                "SELECT DISTINCT token FROM push_tokens WHERE active = 1 AND user_id != ?",
                (exclude_user_id,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT DISTINCT token FROM push_tokens WHERE active = 1"
            ).fetchall()
    finally:
        db.close()

    tokens = [r["token"] for r in rows]
    if not tokens:
        return 0

    return send_push_batch(tokens, title, body, data)








############################################################
# notify_channel_user
############################################################
#
# One user, one channel: returns 0 without sending only
# when the user has an explicit notification_channels row
# with enabled=0 for that channel — no row means enabled,
# the opt-out model. The caller's `data` is copied before
# "channel" is stamped in, so the dict the caller holds is
# never mutated. The channel name is not validated here;
# the CHECK constraint on the table means an unknown name
# can never have an opt-out row, so it always sends.
#
# Used by:
#   - chat/routes.py — send_message, "chat" channel, for
#     every participant without a live socket in this
#     process
############################################################

def notify_channel_user(channel: str, user_id: str, title: str, body: str, data: Optional[dict] = None) -> int:
    db = get_db()
    try:
        # Only an explicit enabled=0 row suppresses; absence
        # of a row is the default-on case
        row = db.execute(
            "SELECT enabled FROM notification_channels WHERE user_id = ? AND channel = ?",
            (user_id, channel),
        ).fetchone()

        if row and not row["enabled"]:
            return 0

        tokens_rows = db.execute(
            "SELECT token FROM push_tokens WHERE user_id = ? AND active = 1",
            (user_id,),
        ).fetchall()
    finally:
        db.close()

    tokens = [r["token"] for r in tokens_rows]
    if not tokens:
        return 0

    # Copy first — the caller's dict must not grow a "channel"
    push_data = dict(data or {})
    push_data["channel"] = channel

    return send_push_batch(tokens, title, body, push_data)








############################################################
# notify_channel
############################################################
#
# Broadcast to every active token whose owner has NOT
# opted out of the channel: NOT EXISTS on a
# notification_channels row with enabled=0, so users who
# never touched their settings are included (opt-out
# model). The exclude clause is appended after the NOT
# EXISTS closes, so it ANDs at the top level as intended.
# The count is devices, not users; "channel" is stamped on
# a copy of `data`. The text is one language for every
# recipient — the scrapers pass Lithuanian; push copy is
# never localised per user.
#
# Used by:
#   - scraper/knf_scraper.py — scrape_knf_news, "news"
#   - scraper/vu_scraper.py — scrape_vu_news, "news"
#   - scraper/schedule_scraper.py — scrape_knf_schedule,
#     "schedule"
#   - admin/routes.py — send_admin_notification, "admin"
#     (that route itself has no caller in the app yet)
############################################################

def notify_channel(channel: str, title: str, body: str, data: Optional[dict] = None, exclude_user_id: Optional[str] = None) -> int:
    db = get_db()
    try:
        # Opt-out model in SQL: a user is excluded only by an
        # explicit enabled=0 row for this channel
        query = """
            SELECT DISTINCT pt.token
            FROM push_tokens pt
            WHERE pt.active = 1
              AND NOT EXISTS (
                SELECT 1 FROM notification_channels nc
                WHERE nc.user_id = pt.user_id
                  AND nc.channel = ?
                  AND nc.enabled = 0
              )
        """
        params: list = [channel]

        if exclude_user_id:
            query += " AND pt.user_id != ?"
            params.append(exclude_user_id)

        rows = db.execute(query, params).fetchall()
    finally:
        db.close()

    tokens = [r["token"] for r in rows]
    if not tokens:
        return 0

    # Copy first — the caller's dict must not grow a "channel"
    push_data = dict(data or {})
    push_data["channel"] = channel

    return send_push_batch(tokens, title, body, push_data)
