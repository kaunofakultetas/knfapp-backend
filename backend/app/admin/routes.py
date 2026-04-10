"""Admin API — invitation code management, user management."""

import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from app.auth.routes import require_role
from app.database import get_db

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/invitations", methods=["POST"])
@require_role("admin", "curator")
def create_invitation():
    """
    Create a new invitation code.

    Body:
      - role: str (student, teacher, admin, curator)
      - max_uses: int (default 1)
      - expires_hours: int (default from config, typically 168 = 7 days)
    """
    data = request.get_json() or {}
    role = data.get("role", "student")
    raw_max_uses = data.get("max_uses", 1)
    raw_expires_hours = data.get("expires_hours", 168)

    if not isinstance(raw_max_uses, int) or isinstance(raw_max_uses, bool):
        return jsonify({"error": "max_uses must be an integer"}), 400
    if not isinstance(raw_expires_hours, int) or isinstance(raw_expires_hours, bool):
        return jsonify({"error": "expires_hours must be an integer"}), 400

    max_uses = max(1, raw_max_uses)
    expires_hours = raw_expires_hours

    if role not in ("student", "teacher", "admin", "curator"):
        return jsonify({"error": "Invalid role"}), 400

    # Only admins can create admin/curator invitations
    if role in ("admin", "curator") and request.user["role"] != "admin":
        return jsonify({"error": "Only admins can create admin/curator invitations"}), 403

    code_id = str(uuid.uuid4())
    code = uuid.uuid4().hex[:12].upper()  # 12-char alphanumeric code
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()

    db = get_db()
    try:
        db.execute(
            """INSERT INTO invitation_codes (id, code, role, created_by, max_uses, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (code_id, code, role, request.user["id"], max_uses, expires_at),
        )
        db.commit()

        return jsonify({
            "id": code_id,
            "code": code,
            "role": role,
            "maxUses": max_uses,
            "useCount": 0,
            "expiresAt": expires_at,
        }), 201

    finally:
        db.close()


@admin_bp.route("/invitations", methods=["GET"])
@require_role("admin", "curator")
def list_invitations():
    """List all invitation codes."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM invitation_codes ORDER BY created_at DESC"
        ).fetchall()

        invitations = [
            {
                "id": r["id"],
                "code": r["code"],
                "role": r["role"],
                "maxUses": r["max_uses"],
                "useCount": r["use_count"],
                "expiresAt": r["expires_at"],
                "createdAt": r["created_at"],
                "expired": datetime.fromisoformat(r["expires_at"]).replace(tzinfo=None) < datetime.utcnow(),
                "fullyUsed": r["use_count"] >= r["max_uses"],
            }
            for r in rows
        ]

        return jsonify({"invitations": invitations})
    finally:
        db.close()


@admin_bp.route("/invitations/<code_id>", methods=["DELETE"])
@require_role("admin")
def delete_invitation(code_id):
    """Delete/revoke an invitation code."""
    db = get_db()
    try:
        result = db.execute("DELETE FROM invitation_codes WHERE id = ?", (code_id,))
        db.commit()
        if result.rowcount == 0:
            return jsonify({"error": "Invitation not found"}), 404
        return jsonify({"message": "Invitation deleted"})
    finally:
        db.close()


@admin_bp.route("/users", methods=["GET"])
@require_role("admin")
def list_users():
    """List all users."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, username, email, display_name, role, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()

        users = [
            {
                "id": r["id"],
                "username": r["username"],
                "email": r["email"],
                "displayName": r["display_name"],
                "role": r["role"],
                "createdAt": r["created_at"],
            }
            for r in rows
        ]

        return jsonify({"users": users})
    finally:
        db.close()


@admin_bp.route("/users/<user_id>", methods=["PATCH"])
@require_role("admin")
def update_user(user_id):
    """
    Update a user's role or active status.

    Body:
      - role: str (student, teacher, admin, curator) — optional
      - active: bool — optional (false = deactivated)
    """
    data = request.get_json() or {}
    new_role = data.get("role")
    active = data.get("active")

    if new_role is not None and new_role not in ("student", "teacher", "admin", "curator"):
        return jsonify({"error": "Invalid role"}), 400

    # Prevent admin from deactivating themselves
    if active is False and user_id == request.user["id"]:
        return jsonify({"error": "Cannot deactivate your own account"}), 400

    if new_role is None and active is None:
        return jsonify({"error": "Nothing to update"}), 400

    db = get_db()
    try:
        user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404

        if new_role is not None:
            db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))

        if active is not None:
            # Use a status column — if it doesn't exist yet, add it
            try:
                db.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
            except Exception:
                # Column doesn't exist — add it and retry
                db.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
                db.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))

            # If deactivating, also invalidate all their sessions
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

        db.commit()

        updated = db.execute(
            "SELECT id, username, email, display_name, role, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        return jsonify({
            "id": updated["id"],
            "username": updated["username"],
            "email": updated["email"],
            "displayName": updated["display_name"],
            "role": updated["role"],
            "createdAt": updated["created_at"],
        })
    finally:
        db.close()


@admin_bp.route("/stats", methods=["GET"])
@require_role("admin")
def admin_stats():
    """Get admin dashboard stats."""
    db = get_db()
    try:
        user_count = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        post_count = db.execute("SELECT COUNT(*) as c FROM news_posts").fetchone()["c"]
        scraped_count = db.execute("SELECT COUNT(*) as c FROM news_posts WHERE source IN ('knf.vu.lt', 'vu.lt')").fetchone()["c"]
        comment_count = db.execute("SELECT COUNT(*) as c FROM news_comments").fetchone()["c"]
        active_invitations = db.execute(
            "SELECT COUNT(*) as c FROM invitation_codes WHERE use_count < max_uses AND expires_at > datetime('now')"
        ).fetchone()["c"]

        return jsonify({
            "users": user_count,
            "posts": post_count,
            "scrapedArticles": scraped_count,
            "comments": comment_count,
            "activeInvitations": active_invitations,
        })
    finally:
        db.close()


@admin_bp.route("/notifications", methods=["POST"])
@require_role("admin")
def send_admin_notification():
    """Send a push notification to all registered users.

    Body:
      - title: str (notification title)
      - body: str (notification body text)
      - data: dict (optional extra data payload)
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    raw_title = data.get("title", "")
    raw_body = data.get("body", "")
    if not isinstance(raw_title, str) or not isinstance(raw_body, str):
        return jsonify({"error": "Title and body must be strings"}), 400

    title = raw_title.strip()
    body_text = raw_body.strip()

    if not title or not body_text:
        return jsonify({"error": "Title and body are required"}), 400

    if len(title) > 200:
        return jsonify({"error": "Title must be at most 200 characters"}), 400
    if len(body_text) > 1000:
        return jsonify({"error": "Body must be at most 1000 characters"}), 400

    from app.notifications.push import notify_channel

    extra_data = data.get("data") if isinstance(data.get("data"), dict) else None
    if extra_data is None:
        extra_data = {"type": "admin_announcement"}

    sent = notify_channel("admin", title, body_text, data=extra_data)

    return jsonify({"sent": sent, "message": f"Notification sent to {sent} devices"})
