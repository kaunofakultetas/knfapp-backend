############################################################
#  [*] Notifications — push tokens and channel switches
#
#  The client-facing half of Expo push: a device registers
#  its ExponentPushToken[...] after login (push_tokens) and
#  drops it on logout, and a user flips the four topic
#  switches news / chat / schedule / admin
#  (notification_channels). Delivery itself lives in
#  notifications/push.py — the scrapers, the admin
#  broadcast and chat send through it and it honours the
#  switches; nothing in this file sends a push.
#
#  Opt-out model: a missing notification_channels row means
#  enabled, so a fresh user hears everything and only an
#  explicit enabled=0 silences a topic. push.py flips
#  push_tokens.active to 0 when Expo answers
#  DeviceNotRegistered; the next register call from that
#  device (every app start) flips it back to 1, and a still
#  dead token just gets deactivated again after the next
#  failed send.
#
#  Every route is @require_auth, the user coming from
#  request.user, and every WRITE route is rate limited under
#  it with auth's shared decorator (per-user key, the house
#  429 body). The channel names come from push.py's
#  VALID_CHANNELS — one list, used by the sender and by the
#  switches alike.
#
#    POST   /api/notifications/register — add or reactivate
#    DELETE /api/notifications/register — remove a token
#    GET    /api/notifications/channels — the four switches
#    PUT    /api/notifications/channels — flip some switches
############################################################


import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

from app.auth.routes import get_json_object, rate_limit, require_auth
from app.database import get_db
from app.notifications.push import VALID_CHANNELS, token_digest

logger = logging.getLogger(__name__)

notifications_bp = Blueprint("notifications", __name__)

# The whole token grammar, not just the prefix: Expo mints
# "ExponentPushToken[" + an opaque id + "]", so anything with
# a control character, a quote or a tag in it was never a
# token. Migration v47 deleted the rows that predate this.
_TOKEN_RE = re.compile(r"ExponentPushToken\[[A-Za-z0-9_-]{10,64}\]")

# A phone, a tablet, the odd reinstall — ten rows is already
# generous. Past that the oldest row goes, so one account can
# never amplify a broadcast without bound
MAX_TOKENS_PER_USER = 10








############################################################
# register_token
############################################################
#
# POST /api/notifications/register
#
# Body {"token", "platform"?, "language"?}. token must match
# the WHOLE Expo grammar (_TOKEN_RE) — the old prefix-only
# check let control characters, markup and SQL-looking
# payloads into the table and out again through every log
# line that quoted them; a platform outside ios/android/web
# is quietly stored as "unknown" — the app only ever sends
# ios or android. language is the app language 'lt' or 'en'
# (anything else, or absent, becomes 'lt'), stored on the
# row (migration v11) so push.py can pick per-language copy;
# the app re-registers on every start and on a language
# switch, so the stored value tracks the setting.
#
# Two outcomes, both {"registered": true, "tokenId"}:
#   - the caller already holds this token → 200; a row
#     push.py had deactivated comes back on and platform,
#     language and updated_at are refreshed either way (the
#     old code skipped the UPDATE when nothing had changed,
#     so a live device's row aged as if it were dead)
#   - a new token, or one that belonged to ANOTHER user
#     (the device changed hands) → 201
#
# One atomic INSERT ... ON CONFLICT(token) DO UPDATE covers
# all of it. The SELECT/DELETE/INSERT it replaces could
# interleave with a racing registration — a duplicate INSERT
# died on the UNIQUE index as a 500, and a takeover deleted
# the previous owner's row with nothing written in its place.
# A takeover is legitimate (phones do change hands) but never
# silent any more: it is logged with both user ids and the
# token's digest. DO UPDATE keeps the ORIGINAL row id, so
# tokenId is re-selected from the table rather than assumed.
#
# The route is rate limited per user (20 per 5-minute window)
# and the caller's row count is capped at MAX_TOKENS_PER_USER
# — a script could otherwise register tokens forever and
# every broadcast would carry the weight.
#
# Timestamps are naive-UTC isoformat ("T", no offset), not
# the datetime('now') column default.
#
# Used by:
#   - services/api/notifications.ts — registerPushToken,
#     via services/notifications.ts
#     registerForPushNotifications: context/AuthContext.tsx
#     runs it after login/register and on every app start,
#     app/(main)/tabs/settings.tsx when the push switch is
#     turned on
############################################################

@notifications_bp.route("/register", methods=["POST"])
@require_auth
@rate_limit("push_register", max_attempts=20)
def register_token():
    # STEP 1: validate the body — shape, the full token
    # grammar, length and the platform whitelist
    # =================================================
    data = get_json_object()
    if not data or not data.get("token"):
        return jsonify({"error": "Push token required"}), 400

    if not isinstance(data["token"], str):
        return jsonify({"error": "Token must be a string"}), 400

    token = data["token"].strip()
    if len(token) > 200:
        return jsonify({"error": "Token too long"}), 400
    if not _TOKEN_RE.fullmatch(token):
        return jsonify({"error": "Invalid Expo push token format"}), 400

    platform = data.get("platform", "unknown")
    if platform not in ("ios", "android", "web", "unknown"):
        platform = "unknown"

    # 'lt' is the app default; anything unexpected falls back to it
    language = data.get("language")
    if language not in ("lt", "en"):
        language = "lt"

    user_id = request.user["id"]
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


    # STEP 2: who holds this token today — that decides 200
    # vs 201 and is the only place a device changing hands
    # can be noticed
    # =====================================================
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id, user_id FROM push_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        own = bool(existing) and existing["user_id"] == user_id

        if existing and not own:
            logger.warning(
                "Push token reassigned from user %s to user %s (token:%s)",
                existing["user_id"], user_id, token_digest(token),
            )


        # STEP 3: keep the caller's fleet bounded — the rows
        # that have gone longest without re-registering are
        # the ones that go
        # ==================================================
        surplus = db.execute(
            """SELECT id FROM push_tokens
               WHERE user_id = ? AND token != ?
               ORDER BY updated_at DESC
               LIMIT -1 OFFSET ?""",
            (user_id, token, MAX_TOKENS_PER_USER - 1),
        ).fetchall()

        if surplus:
            placeholders = ",".join("?" * len(surplus))
            db.execute(
                f"DELETE FROM push_tokens WHERE id IN ({placeholders})",
                [r["id"] for r in surplus],
            )
            logger.info("Dropped %d push token(s) over the cap for user %s", len(surplus), user_id)


        # STEP 4: one atomic upsert — insert, reactivate and
        # reassign are the same statement, so nothing can
        # interleave between a check and a write
        # ==================================================
        try:
            db.execute(
                """INSERT INTO push_tokens (id, user_id, token, platform, language, created_at, updated_at, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(token) DO UPDATE SET
                       user_id = excluded.user_id,
                       platform = excluded.platform,
                       language = excluded.language,
                       active = 1,
                       updated_at = excluded.updated_at""",
                (str(uuid.uuid4()), user_id, token, platform, language, now, now),
            )
            db.commit()
        except sqlite3.IntegrityError:
            # Belt and braces: a racing writer won: their row
            # says the same thing ours would have, so answer
            # with it instead of a 500
            db.rollback()
            logger.warning("Push token registration raced (token:%s)", token_digest(token))


        # STEP 5: DO UPDATE keeps the original row id, so the
        # id goes out of the TABLE, never out of the INSERT
        # ===================================================
        row = db.execute("SELECT id FROM push_tokens WHERE token = ?", (token,)).fetchone()
        if not row:
            logger.error("Push token vanished during registration (token:%s)", token_digest(token))
            return jsonify({"error": "Could not register push token"}), 500

        return jsonify({"registered": True, "tokenId": row["id"]}), (200 if own else 201)
    finally:
        db.close()








############################################################
# unregister_token
############################################################
#
# DELETE /api/notifications/register
#
# Body {"token"} — a string, and that is the only check on
# it: neither the token grammar nor the POST's 200-character
# cap applies here, because an owner must be able to remove
# a legacy row of ANY shape that predates _TOKEN_RE. The cap
# used to be shared with the POST and answered 400 "Token
# too long" for the longest of those rows, which left them
# in the table with nobody able to name them. Dropping it
# unbounds nothing: the body is capped at MAX_CONTENT_LENGTH
# (app/__init__.py), the statement is parameterised and the
# match is owner-scoped, so an absurd string simply hits no
# row. Deletes the caller's OWN row for that token (user_id
# AND token), so a token that has since moved to another
# user answers 404 "Token not found", as does one never
# registered. The mobile wrapper swallows every error here
# (logout must never block), so that 404 is invisible in
# practice. This is real removal, unlike the active=0
# push.py uses for dead tokens.
#
# Used by:
#   - services/api/notifications.ts — unregisterPushToken,
#     via services/notifications.ts
#     unregisterPushNotifications: context/AuthContext.tsx
#     logout and app/(main)/tabs/settings.tsx when the push
#     switch is turned off
############################################################

@notifications_bp.route("/register", methods=["DELETE"])
@require_auth
@rate_limit("push_register", max_attempts=20)
def unregister_token():
    data = get_json_object()
    if not data or not data.get("token"):
        return jsonify({"error": "Push token required"}), 400

    if not isinstance(data["token"], str):
        return jsonify({"error": "Token must be a string"}), 400

    # No length cap on this route: a legacy row longer than the
    # POST's 200 characters would otherwise be unnameable by the
    # only person entitled to remove it
    token = data["token"].strip()

    user_id = request.user["id"]

    db = get_db()
    try:
        result = db.execute(
            "DELETE FROM push_tokens WHERE user_id = ? AND token = ?",
            (user_id, token),
        )
        db.commit()

        if result.rowcount == 0:
            return jsonify({"error": "Token not found"}), 404

        return jsonify({"unregistered": True})
    finally:
        db.close()








############################################################
# get_channels
############################################################
#
# GET /api/notifications/channels
#
# {"channels": {"news", "chat", "schedule", "admin" → bool}}
# — always all four keys. The dict starts all-True and only
# rows that exist override it: the opt-out model push.py's
# notify_channel implements (a user is skipped only on an
# explicit enabled=0). The same read-back is repeated
# verbatim at the end of update_channels.
#
# Used by:
#   - services/api/notifications.ts —
#     fetchNotificationChannels, the switch list load in
#     app/(main)/tabs/settings.tsx
############################################################

@notifications_bp.route("/channels", methods=["GET"])
@require_auth
def get_channels():
    user_id = request.user["id"]
    db = get_db()
    try:
        rows = db.execute(
            "SELECT channel, enabled FROM notification_channels WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        # A missing row means enabled — the opt-out model
        channels = {ch: True for ch in VALID_CHANNELS}
        for row in rows:
            channels[row["channel"]] = bool(row["enabled"])

        return jsonify({"channels": channels})
    finally:
        db.close()








############################################################
# update_channels
############################################################
#
# PUT /api/notifications/channels
#
# Body {"channels": {"news": true, "chat": false, ...}} —
# partial is fine, only the names present are written. A
# name outside VALID_CHANNELS is a 400 naming it (it used to
# be skipped silently in validation and in the write loop
# alike, so a typo answered 200 and changed nothing);
# contract-safe because the app's typed NotificationChannel
# can only send the four real names. A value that is not a
# JSON boolean (1/0 included) is a 400 naming the channel
# and the Python type it got. Validation runs over the whole
# dict BEFORE the first write, so a bad entry never leaves a
# half-applied batch; an empty dict is a no-op 200. Each
# channel is upserted through ON CONFLICT(user_id, channel)
# — the table's composite PRIMARY KEY — under one commit.
# The response is the full resulting state in GET's shape;
# settings.tsx takes it as the confirmed truth after a
# debounced batch of toggles.
#
# Used by:
#   - services/api/notifications.ts —
#     updateNotificationChannels, flushChannels in
#     app/(main)/tabs/settings.tsx (one merged request per
#     debounce window)
############################################################

@notifications_bp.route("/channels", methods=["PUT"])
@require_auth
@rate_limit("push_channels", max_attempts=60)
def update_channels():
    # STEP 1: shape check, then every name must be real and
    # every value a real boolean before anything is written
    # =====================================================
    data = get_json_object()
    if not data or not isinstance(data.get("channels"), dict):
        return jsonify({"error": "channels dict required"}), 400

    channels_input = data["channels"]
    user_id = request.user["id"]
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    for channel, enabled in channels_input.items():
        if channel not in VALID_CHANNELS:
            return jsonify({"error": f"Unknown channel '{channel}'"}), 400
        if not isinstance(enabled, bool):
            return jsonify({"error": f"Channel '{channel}' value must be a boolean (true/false), got {type(enabled).__name__}"}), 400


    # STEP 2: upsert the listed channels under one commit —
    # ON CONFLICT rides on the (user_id, channel) primary key
    # =======================================================
    db = get_db()
    try:
        for channel, enabled in channels_input.items():
            db.execute(
                """INSERT INTO notification_channels (user_id, channel, enabled, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, channel)
                   DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at""",
                (user_id, channel, 1 if enabled else 0, now),
            )
        db.commit()


        # STEP 3: read back the full state — same shape and
        # same all-True default as get_channels
        # =================================================
        rows = db.execute(
            "SELECT channel, enabled FROM notification_channels WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        result = {ch: True for ch in VALID_CHANNELS}
        for row in rows:
            result[row["channel"]] = bool(row["enabled"])

        return jsonify({"channels": result})
    finally:
        db.close()








############################################################
# get_chat_preview / update_chat_preview
############################################################
#
# GET  /api/notifications/chat-preview → {enabled}
# PUT  /api/notifications/chat-preview {enabled} → {enabled}
#
# The "show message text in notifications" switch
# (users.chat_push_preview, migration v56). Enabled ships
# the first 100 characters of a chat message to Expo as the
# push body, exactly as before the setting existed; disabled
# sends the content-free "Nauja žinutė" instead, so private
# text never leaves for the push processor at all. Separate
# from the channels dict on purpose: this is not an on/off
# topic subscription, and a new key of a different shape
# inside "channels" would be a contract change for the
# mobile settings screen.
#
# Used by:
#   - services/api/notifications.ts — fetchChatPreview /
#     updateChatPreview (the settings screen's toggle)
############################################################

@notifications_bp.route("/chat-preview", methods=["GET"])
@require_auth
def get_chat_preview():
    db = get_db()
    try:
        row = db.execute(
            "SELECT chat_push_preview FROM users WHERE id = ?",
            (request.user["id"],),
        ).fetchone()
        return jsonify({"enabled": bool(row["chat_push_preview"]) if row else True})
    finally:
        db.close()


@notifications_bp.route("/chat-preview", methods=["PUT"])
@require_auth
@rate_limit("notif_prefs", max_attempts=30)
def update_chat_preview():
    # STEP 1: the body — enabled must be an actual boolean
    # ====================================================
    data = get_json_object()
    if not data or not isinstance(data.get("enabled"), bool):
        return jsonify({"error": "enabled must be a boolean"}), 400


    # STEP 2: one column write on the caller's own row
    # ================================================
    db = get_db()
    try:
        db.execute(
            "UPDATE users SET chat_push_preview = ? WHERE id = ?",
            (1 if data["enabled"] else 0, request.user["id"]),
        )
        db.commit()
        return jsonify({"enabled": data["enabled"]})
    finally:
        db.close()
