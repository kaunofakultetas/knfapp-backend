"""Push notification routes -- token registration and channel management."""

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.auth.routes import require_auth, require_role
from app.database import get_db
from app.notifications.push import notify_all_users

notifications_bp = Blueprint("notifications", __name__)

# Valid notification channels
VALID_CHANNELS = ("news", "chat", "schedule", "admin")


@notifications_bp.route("/register", methods=["POST"])
@require_auth
def register_token():
    """Register an Expo push token for the authenticated user.

    Body:
      - token: str (Expo push token, e.g. ExponentPushToken[xxx])
      - platform: str (ios|android|web, optional, default 'unknown')
    """
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

    db = get_db()
    try:
        # Check if token already exists for this user
        existing = db.execute(
            "SELECT id, active FROM push_tokens WHERE user_id = ? AND token = ?",
            (user_id, token),
        ).fetchone()

        if existing:
            # Reactivate if it was deactivated
            if not existing["active"]:
                db.execute(
                    "UPDATE push_tokens SET active = 1, updated_at = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(), existing["id"]),
                )
                db.commit()
            return jsonify({"registered": True, "tokenId": existing["id"]})

        # Check if this token is registered to another user (device changed hands)
        other = db.execute(
            "SELECT id FROM push_tokens WHERE token = ? AND user_id != ?",
            (token, user_id),
        ).fetchone()
        if other:
            # Transfer token to new user
            db.execute("DELETE FROM push_tokens WHERE id = ?", (other["id"],))

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


@notifications_bp.route("/register", methods=["DELETE"])
@require_auth
def unregister_token():
    """Remove a push token for the authenticated user.

    Body:
      - token: str (Expo push token to remove)
    """
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


@notifications_bp.route("/channels", methods=["GET"])
@require_auth
def get_channels():
    """Get the user's notification channel preferences.

    Returns a dict of channel -> enabled status.
    Channels without explicit records default to enabled.
    """
    user_id = request.user["id"]
    db = get_db()
    try:
        rows = db.execute(
            "SELECT channel, enabled FROM notification_channels WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        # Build result with defaults (all enabled if no record)
        channels = {ch: True for ch in VALID_CHANNELS}
        for row in rows:
            channels[row["channel"]] = bool(row["enabled"])

        return jsonify({"channels": channels})
    finally:
        db.close()


@notifications_bp.route("/channels", methods=["PUT"])
@require_auth
def update_channels():
    """Update notification channel preferences.

    Body:
      - channels: dict[str, bool] e.g. {"news": true, "chat": false}
    """
    data = request.get_json()
    if not data or not isinstance(data.get("channels"), dict):
        return jsonify({"error": "channels dict required"}), 400

    channels_input = data["channels"]
    user_id = request.user["id"]
    now = datetime.utcnow().isoformat()

    db = get_db()
    try:
        for channel, enabled in channels_input.items():
            if channel not in VALID_CHANNELS:
                continue
            if not isinstance(enabled, bool):
                continue

            db.execute(
                """INSERT INTO notification_channels (user_id, channel, enabled, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id, channel)
                   DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at""",
                (user_id, channel, 1 if enabled else 0, now),
            )
        db.commit()

        # Return updated state
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
