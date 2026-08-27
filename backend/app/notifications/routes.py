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
#  request.user. require_role and notify_all_users are
#  imported but never used here — dead imports.
#
#    POST   /api/notifications/register — add or reactivate
#    DELETE /api/notifications/register — remove a token
#    GET    /api/notifications/channels — the four switches
#    PUT    /api/notifications/channels — flip some switches
############################################################


import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

# require_role and notify_all_users are dead imports — every
# route here is plain @require_auth and nothing sends a push
from app.auth.routes import require_auth, require_role
from app.database import get_db
from app.notifications.push import notify_all_users

notifications_bp = Blueprint("notifications", __name__)

# The channel names the switches accept — mirrors the CHECK
# on notification_channels.channel (database/__init__.py),
# which is the real guard; update_channels skips unknown
# names silently rather than rejecting them
VALID_CHANNELS = ("news", "chat", "schedule", "admin")








############################################################
# register_token
############################################################
#
# POST /api/notifications/register
#
# Body {"token", "platform"?}. token must be a string that
# starts with "ExponentPushToken[" (the closing bracket is
# not checked) and is at most 200 chars after strip();
# a platform outside ios/android/web is quietly stored as
# "unknown" — the app only ever sends ios or android.
#
# Three outcomes, all {"registered": true, "tokenId"}:
#   - the user already holds this token → 200, and a row
#     push.py had deactivated is switched back on
#   - the token belongs to ANOTHER user (device changed
#     hands) → that row is deleted first, because
#     idx_push_tokens_token is UNIQUE and the INSERT would
#     otherwise die with IntegrityError → 500
#   - otherwise a new active row → 201
#
# The pre-checks and the INSERT are separate statements,
# so two racing registrations of one new token can still
# hit the unique index; one device registering at a time
# makes that academic. Timestamps are utcnow().isoformat()
# ("T", no offset), not the datetime('now') column default.
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
def register_token():
    # STEP 1: validate the body — shape, prefix, length and
    # the platform whitelist
    # =====================================================
    data = request.get_json()
    if not data or not data.get("token"):
        return jsonify({"error": "Push token required"}), 400

    if not isinstance(data["token"], str):
        return jsonify({"error": "Token must be a string"}), 400

    token = data["token"].strip()
    if len(token) > 200:
        return jsonify({"error": "Token too long"}), 400
    if not token.startswith("ExponentPushToken["):
        return jsonify({"error": "Invalid Expo push token format"}), 400

    platform = data.get("platform", "unknown")
    if platform not in ("ios", "android", "web", "unknown"):
        platform = "unknown"

    user_id = request.user["id"]


    # STEP 2: the user already has this token — reactivate
    # it if push.py had flipped active to 0 on
    # DeviceNotRegistered, and answer 200 either way
    # ====================================================
    db = get_db()
    try:
        existing = db.execute(
            "SELECT id, active FROM push_tokens WHERE user_id = ? AND token = ?",
            (user_id, token),
        ).fetchone()

        if existing:
            if not existing["active"]:
                db.execute(
                    "UPDATE push_tokens SET active = 1, updated_at = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(), existing["id"]),
                )
                db.commit()
            return jsonify({"registered": True, "tokenId": existing["id"]})


        # STEP 3: the same device now belongs to someone else —
        # drop the old owner's row so the UNIQUE token index
        # lets the INSERT through
        # =====================================================
        other = db.execute(
            "SELECT id FROM push_tokens WHERE token = ? AND user_id != ?",
            (token, user_id),
        ).fetchone()
        if other:
            db.execute("DELETE FROM push_tokens WHERE id = ?", (other["id"],))


        # STEP 4: a fresh row, active from the start
        # ==========================================
        token_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        db.execute(
            """INSERT INTO push_tokens (id, user_id, token, platform, created_at, updated_at, active)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (token_id, user_id, token, platform, now, now),
        )
        db.commit()

        return jsonify({"registered": True, "tokenId": token_id}), 201
    finally:
        db.close()








############################################################
# unregister_token
############################################################
#
# DELETE /api/notifications/register
#
# Body {"token"} — the same string/200-char checks as the
# POST, minus the prefix check. Deletes the caller's OWN
# row for that token (user_id AND token), so a token that
# has since moved to another user answers 404 "Token not
# found", as does one never registered. The mobile wrapper
# swallows every error here (logout must never block), so
# that 404 is invisible in practice. This is real removal,
# unlike the active=0 push.py uses for dead tokens.
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
def unregister_token():
    data = request.get_json()
    if not data or not data.get("token"):
        return jsonify({"error": "Push token required"}), 400

    if not isinstance(data["token"], str):
        return jsonify({"error": "Token must be a string"}), 400

    token = data["token"].strip()
    if len(token) > 200:
        return jsonify({"error": "Token too long"}), 400

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
# partial is fine, only the names present are written.
# Names outside VALID_CHANNELS are skipped silently (in
# validation and in the write loop alike); a value that is
# not a JSON boolean (1/0 included) is a 400 naming the
# channel and the Python type it got. Validation runs over
# the whole dict BEFORE the first write, so a bad value
# never leaves a half-applied batch; an empty dict is a
# no-op 200. Each channel is upserted through
# ON CONFLICT(user_id, channel) — the table's composite
# PRIMARY KEY — under one commit, with updated_at as
# utcnow().isoformat(). The response is the full resulting
# state in GET's shape; settings.tsx takes it as the
# confirmed truth after a debounced batch of toggles.
#
# Used by:
#   - services/api/notifications.ts —
#     updateNotificationChannels, flushChannels in
#     app/(main)/tabs/settings.tsx (one merged request per
#     debounce window)
############################################################

@notifications_bp.route("/channels", methods=["PUT"])
@require_auth
def update_channels():
    # STEP 1: shape check, then every value must be a real
    # boolean before anything is written
    # ====================================================
    data = request.get_json()
    if not data or not isinstance(data.get("channels"), dict):
        return jsonify({"error": "channels dict required"}), 400

    channels_input = data["channels"]
    user_id = request.user["id"]
    now = datetime.utcnow().isoformat()

    for channel, enabled in channels_input.items():
        if channel not in VALID_CHANNELS:
            continue
        if not isinstance(enabled, bool):
            return jsonify({"error": f"Channel '{channel}' value must be a boolean (true/false), got {type(enabled).__name__}"}), 400


    # STEP 2: upsert the listed channels under one commit —
    # ON CONFLICT rides on the (user_id, channel) primary key
    # =======================================================
    db = get_db()
    try:
        for channel, enabled in channels_input.items():
            if channel not in VALID_CHANNELS:
                continue

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
