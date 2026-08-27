############################################################
#  [*] Admin — invitation codes, user management, stats
#
#  The admin console behind the mobile app's admin screens:
#  minting and revoking invitation codes (optional at
#  registration — a code grants its role and invited=1, no
#  code means a guest 'student'), the user list with role /
#  active editing, the dashboard counters and a broadcast
#  push. Every route sits behind require_role — curators
#  may only mint and list codes, everything else is
#  admin-only. Mounted at /api/admin by app/__init__.py.
#
#  The rows touched here are the same ones app/auth/routes.py
#  consumes: register() bumps use_count and checks
#  expires_at, login() answers 403 and get_current_user()
#  returns None for users with active = 0 (the column added
#  by migration v8 in app/database/__init__.py).
#
#    POST   /api/admin/invitations           — mint a code
#    GET    /api/admin/invitations           — list codes
#    DELETE /api/admin/invitations/<code_id> — revoke a code
#    GET    /api/admin/users                 — list users
#    PATCH  /api/admin/users/<user_id>       — role / active
#    GET    /api/admin/stats                 — dashboard counters
#    POST   /api/admin/notifications         — broadcast push
############################################################


import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from app.auth.routes import require_role
from app.database import get_db


# Mounted at /api/admin (app/__init__.py); every route below is relative
admin_bp = Blueprint("admin", __name__)








############################################################
# create_invitation
############################################################
#
# POST /api/admin/invitations
#
# Mints one invitation code. Body: role (default student),
# max_uses (int, default 1, clamped to >= 1), expires_hours
# (int, default 168 = 7 days). Curators may mint student /
# teacher codes; admin and curator codes are admin-only
# (403). Answers 201 with the new row in the camelCase
# shape list_invitations uses, minus createdAt / expired /
# fullyUsed.
#
# The code is the first 12 hex chars of a uuid4, uppercased
# (48 bits, hex only — not full alphanumeric), stored in a
# UNIQUE column: a collision would surface as IntegrityError
# → 500, accepted as negligible. expires_at is an aware-UTC
# isoformat string ("YYYY-MM-DDTHH:MM:SS.ffffff+00:00") —
# auth's checks and admin_stats' string comparison both
# depend on exactly that shape.
#
# Gotchas (documented, not fixed): expires_hours is only
# type-checked — 0 or a negative value mints an already
# expired code and still answers 201, and an absurdly large
# value raises OverflowError inside timedelta → 500.
# max_uses has no upper bound.
#
# Used by:
#   - services/api/admin.ts — createInvitation (the "new
#     code" form on app/(main)/admin/index.tsx)
############################################################

@admin_bp.route("/invitations", methods=["POST"])
@require_role("admin", "curator")
def create_invitation():
    # STEP 1: validate the body — ints first (bool is an int
    # subclass, hence the explicit exclusion), then the role
    # ======================================================
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

    # Curators pass the decorator too — escalation via a self-minted
    # admin/curator code is blocked here, not by require_role
    if role in ("admin", "curator") and request.user["role"] != "admin":
        return jsonify({"error": "Only admins can create admin/curator invitations"}), 403


    # STEP 2: mint the code and insert it
    # ===================================
    code_id = str(uuid.uuid4())
    code = uuid.uuid4().hex[:12].upper()  # 12 uppercase hex chars (48 bits)
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








############################################################
# list_invitations
############################################################
#
# GET /api/admin/invitations
#
# Every invitation code, newest first, with two derived
# flags: expired (expires_at parsed, tzinfo stripped and
# compared against naive utcnow — the same comparison
# auth's register() makes, so the two never disagree) and
# fullyUsed (use_count >= max_uses). Revoked codes are hard
# deleted, so they never appear here. datetime.utcnow() is
# deprecated on the python:3.13 image but still works.
#
# Used by:
#   - services/api/admin.ts — fetchAdminInvitations (the
#     invitation list on app/(main)/admin/index.tsx)
############################################################

@admin_bp.route("/invitations", methods=["GET"])
@require_role("admin", "curator")
def list_invitations():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT * FROM invitation_codes ORDER BY created_at DESC"
        ).fetchall()

        # expired: aware isoformat → naive UTC, versus naive utcnow — both
        # sides UTC, mirrors app/auth/routes.py
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








############################################################
# delete_invitation
############################################################
#
# DELETE /api/admin/invitations/<code_id>
#
# Hard-deletes one code by its id (not by the code string).
# Admin only — a curator cannot revoke what they minted.
# 404 when nothing matched. Users who already registered
# with the code are unaffected; the row's use_count history
# goes with it.
#
# Used by:
#   - services/api/admin.ts — revokeInvitation (the delete
#     action on app/(main)/admin/index.tsx)
############################################################

@admin_bp.route("/invitations/<code_id>", methods=["DELETE"])
@require_role("admin")
def delete_invitation(code_id):
    db = get_db()
    try:
        result = db.execute("DELETE FROM invitation_codes WHERE id = ?", (code_id,))
        db.commit()
        if result.rowcount == 0:
            return jsonify({"error": "Invitation not found"}), 404
        return jsonify({"message": "Invitation deleted"})
    finally:
        db.close()








############################################################
# list_users
############################################################
#
# GET /api/admin/users
#
# All users newest first, without password hashes. `active`
# is the migration-v8 flag coerced to a bool — returned
# here and by update_user; the mobile AdminUser type keeps
# it optional only so an older backend reads as "unknown".
#
# Used by:
#   - services/api/admin.ts — fetchAdminUsers (the user
#     list on app/(main)/admin-users/index.tsx)
############################################################

@admin_bp.route("/users", methods=["GET"])
@require_role("admin")
def list_users():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, username, email, display_name, role, active, created_at FROM users ORDER BY created_at DESC"
        ).fetchall()

        users = [
            {
                "id": r["id"],
                "username": r["username"],
                "email": r["email"],
                "displayName": r["display_name"],
                "role": r["role"],
                "active": bool(r["active"]),
                "createdAt": r["created_at"],
            }
            for r in rows
        ]

        return jsonify({"users": users})
    finally:
        db.close()








############################################################
# update_user
############################################################
#
# PATCH /api/admin/users/<user_id>
#
# Changes a user's role and/or active flag. Body: role
# (optional, one of student/teacher/admin/curator), active
# (optional bool). Both absent → 400 "Nothing to update".
# Deactivating also deletes every session of that user so
# the flag bites immediately; get_current_user() refuses
# inactive users on every request and login() answers 403
# (both rely on the migration-v8 column). Answers the
# fresh row in the list_users shape.
#
# `active` must be a real JSON boolean — a 0 or "false"
# used to slip past the `is False` self-deactivation guard
# and deactivate (and log out) the calling admin, so any
# non-bool now answers 400 before the guards run.
#
# Gotchas (documented, not fixed): nothing stops an admin
# demoting themselves, or the last admin, to student. A
# role change leaves existing sessions untouched.
#
# Used by:
#   - services/api/admin.ts — updateAdminUser (the role /
#     active editors on app/(main)/admin-users/index.tsx)
############################################################

@admin_bp.route("/users/<user_id>", methods=["PATCH"])
@require_role("admin")
def update_user(user_id):
    # STEP 1: validate — role whitelist, self-deactivation
    # guard, then reject an empty patch
    # ====================================================
    data = request.get_json() or {}
    new_role = data.get("role")
    active = data.get("active")

    if new_role is not None and new_role not in ("student", "teacher", "admin", "curator"):
        return jsonify({"error": "Invalid role"}), 400

    # Only true/false may reach the guards below — see the banner
    if active is not None and not isinstance(active, bool):
        return jsonify({"error": "active must be a boolean"}), 400

    if active is False and user_id == request.user["id"]:
        return jsonify({"error": "Cannot deactivate your own account"}), 400

    if new_role is None and active is None:
        return jsonify({"error": "Nothing to update"}), 400


    # STEP 2: confirm the user exists, then apply each field on
    # its own — sessions are purged on deactivation
    # =========================================================
    db = get_db()
    try:
        user = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404

        if new_role is not None:
            db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))

        if active is not None:
            # Column guaranteed by migration v8; login() and get_current_user()
            # both enforce it, so the write alone already locks the user out
            db.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))

            # Drop live sessions too — frees the rows and makes the logout
            # immediate even if the auth check were ever relaxed
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

        db.commit()


        # STEP 3: answer the fresh row in the list_users shape
        # ====================================================
        updated = db.execute(
            "SELECT id, username, email, display_name, role, active, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

        return jsonify({
            "id": updated["id"],
            "username": updated["username"],
            "email": updated["email"],
            "displayName": updated["display_name"],
            "role": updated["role"],
            "active": bool(updated["active"]),
            "createdAt": updated["created_at"],
        })
    finally:
        db.close()








############################################################
# admin_stats
############################################################
#
# GET /api/admin/stats
#
# Five counters for the dashboard tiles: users, posts,
# scrapedArticles (news_posts whose source is knf.vu.lt or
# vu.lt — the two news scrapers; faculty/user/app posts are
# excluded), comments, activeInvitations.
#
# activeInvitations compares dates as strings on purpose.
# expires_at is Python isoformat — 'T' separator, fractional
# seconds, "+00:00" suffix — while SQLite's datetime('now')
# prints a space separator. Compared raw, 'T' sorts after
# ' ', so a same-day code that had already expired would
# still count as active. Both sides are therefore cut to
# the 19-char YYYY-MM-DDTHH:MM:SS shape (strftime with a
# literal T). Both clocks are UTC, and the seed code from
# database seeding uses the same isoformat shape, so no row
# escapes the comparison.
#
# Used by:
#   - services/api/admin.ts — fetchAdminStats (the dashboard
#     tiles on app/(main)/admin/index.tsx)
############################################################

@admin_bp.route("/stats", methods=["GET"])
@require_role("admin")
def admin_stats():
    db = get_db()
    try:
        user_count = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        post_count = db.execute("SELECT COUNT(*) as c FROM news_posts").fetchone()["c"]
        scraped_count = db.execute("SELECT COUNT(*) as c FROM news_posts WHERE source IN ('knf.vu.lt', 'vu.lt')").fetchone()["c"]
        comment_count = db.execute("SELECT COUNT(*) as c FROM news_comments").fetchone()["c"]
        # expires_at is ISO with a 'T' separator while datetime('now') uses
        # a space — compare on the same 19-char shape so same-day codes
        # count correctly (see the banner)
        active_invitations = db.execute(
            "SELECT COUNT(*) as c FROM invitation_codes WHERE use_count < max_uses AND substr(expires_at, 1, 19) > strftime('%Y-%m-%dT%H:%M:%S', 'now')"
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








############################################################
# send_admin_notification
############################################################
#
# POST /api/admin/notifications
#
# Broadcast push on the "admin" channel. Body: title (<= 200
# chars) and body (<= 1000), both required strings, trimmed
# before the checks; an optional `data` dict rides along as
# the push payload. Delivery goes through notify_channel,
# which targets every active push token whose user has NOT
# opted out of the admin channel (opt-out model, see
# app/notifications/push.py) and stamps data["channel"] =
# "admin" on the way out. `sent` counts devices, not users.
#
# Gotchas: a caller-supplied `data` REPLACES the default
# {"type": "admin_announcement"} rather than merging with
# it, so custom payloads lose the type marker. The local
# import of notify_channel is not a cycle guard — push.py
# imports only app.database and is already loaded at
# startup via notifications/routes.py.
#
# Used by:
#   - nothing calls this at the moment — services/api/
#     admin.ts has no wrapper for it and no screen posts
#     here
############################################################

@admin_bp.route("/notifications", methods=["POST"])
@require_role("admin")
def send_admin_notification():
    # STEP 1: validate — strings only, trimmed, length-bounded
    # ========================================================
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


    # STEP 2: hand off to the channel fan-out
    # =======================================
    from app.notifications.push import notify_channel

    # A caller-supplied dict wins outright — the default type marker is not
    # merged into it
    extra_data = data.get("data") if isinstance(data.get("data"), dict) else None
    if extra_data is None:
        extra_data = {"type": "admin_announcement"}

    sent = notify_channel("admin", title, body_text, data=extra_data)

    return jsonify({"sent": sent, "message": f"Notification sent to {sent} devices"})
