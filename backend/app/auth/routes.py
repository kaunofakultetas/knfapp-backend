"""Authentication routes — invitation-based registration, login, sessions."""

import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
import jwt
from flask import Blueprint, current_app, jsonify, request

from app.database import get_db

auth_bp = Blueprint("auth", __name__)

# Simple in-memory rate limiter for auth endpoints
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_WINDOW = 300  # 5 minutes
_RATE_LIMIT_MAX = 10  # max attempts per window


def _check_rate_limit(key: str) -> bool:
    """Return True if rate limit exceeded."""
    now = time.time()
    attempts = _rate_limit_store[key]
    # Prune old entries
    _rate_limit_store[key] = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[key]) >= _RATE_LIMIT_MAX:
        return True
    _rate_limit_store[key].append(now)
    return False


def get_current_user():
    """Extract user from Authorization header. Returns user dict or None."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]
    db = get_db()
    try:
        session = db.execute(
            "SELECT s.user_id, s.expires_at FROM sessions s WHERE s.token = ?",
            (token,),
        ).fetchone()

        if not session:
            return None

        expires = datetime.fromisoformat(session["expires_at"]).replace(tzinfo=None)
        if expires < datetime.utcnow():
            db.execute("DELETE FROM sessions WHERE token = ?", (token,))
            db.commit()
            return None

        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        return dict(user) if user else None
    finally:
        db.close()


def require_auth(f):
    """Decorator requiring valid authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        request.user = user
        return f(*args, **kwargs)
    return decorated


def require_role(*roles):
    """Decorator requiring specific role(s)."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = get_current_user()
            if not user:
                return jsonify({"error": "Authentication required"}), 401
            if user["role"] not in roles:
                return jsonify({"error": "Insufficient permissions"}), 403
            request.user = user
            return f(*args, **kwargs)
        return decorated
    return decorator


@auth_bp.route("/validate-code", methods=["POST"])
def validate_invitation_code():
    """Validate an invitation code without consuming it. Returns code details."""
    data = request.get_json()
    if not data or not data.get("code"):
        return jsonify({"error": "Code required"}), 400

    db = get_db()
    try:
        invite = db.execute(
            "SELECT * FROM invitation_codes WHERE code = ?",
            (data["code"],),
        ).fetchone()

        if not invite:
            return jsonify({"valid": False, "error": "Invalid invitation code"}), 200

        if invite["use_count"] >= invite["max_uses"]:
            return jsonify({"valid": False, "error": "Invitation code has been fully used"}), 200

        invite_expires = datetime.fromisoformat(invite["expires_at"]).replace(tzinfo=None)
        if invite_expires < datetime.utcnow():
            return jsonify({"valid": False, "error": "Invitation code has expired"}), 200

        return jsonify({
            "valid": True,
            "role": invite["role"],
            "remainingUses": invite["max_uses"] - invite["use_count"],
        })
    finally:
        db.close()


@auth_bp.route("/register", methods=["POST"])
def register():
    """Register a new user.

    Invitation code is OPTIONAL:
    - With valid code: user gets the role from the code and invited=1 (higher trust).
    - Without code: user registers as 'student' with invited=0 (guest, limited features).
    App works without login or invite code — auth adds features, doesn't gate.
    """
    client_ip = request.remote_addr or "unknown"
    if _check_rate_limit(f"register:{client_ip}"):
        return jsonify({"error": "Too many registration attempts. Please wait a few minutes."}), 429

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ["username", "password", "display_name", "email"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    if len(data["password"]) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    if len(data["display_name"].strip()) > 100:
        return jsonify({"error": "Display name must be at most 100 characters"}), 400

    db = get_db()
    try:
        # Determine role and invited status based on invitation code
        invite_code = (data.get("invitation_code") or "").strip()
        role = "student"
        invited = 0

        if invite_code:
            invite = db.execute(
                "SELECT * FROM invitation_codes WHERE code = ?",
                (invite_code,),
            ).fetchone()

            if not invite:
                return jsonify({"error": "Invalid invitation code"}), 400

            if invite["use_count"] >= invite["max_uses"]:
                return jsonify({"error": "Invitation code has been fully used"}), 400

            invite_expires = datetime.fromisoformat(invite["expires_at"]).replace(tzinfo=None)
            if invite_expires < datetime.utcnow():
                return jsonify({"error": "Invitation code has expired"}), 400

            role = invite["role"]
            invited = 1

        # Check uniqueness
        existing = db.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (data["username"], data["email"]),
        ).fetchone()
        if existing:
            return jsonify({"error": "Username or email already exists"}), 409

        # Create user
        user_id = str(uuid.uuid4())
        password_hash = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()

        db.execute(
            "INSERT INTO users (id, username, email, display_name, password_hash, role, invited) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, data["username"], data["email"], data["display_name"], password_hash, role, invited),
        )

        # Increment invitation use count if code was used
        if invite_code and invited:
            db.execute(
                "UPDATE invitation_codes SET use_count = use_count + 1 WHERE code = ?",
                (invite_code,),
            )

        # Create session
        token = str(uuid.uuid4())
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.execute(
            "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, token, expires_at),
        )
        db.commit()

        return jsonify({
            "user": {
                "id": user_id,
                "username": data["username"],
                "email": data["email"],
                "displayName": data["display_name"],
                "role": role,
                "invited": bool(invited),
                "studentNumber": None,
                "studyGroup": None,
                "studyProgram": None,
            },
            "token": token,
        }), 201

    finally:
        db.close()


@auth_bp.route("/login", methods=["POST"])
def login():
    """Login with username/email and password."""
    client_ip = request.remote_addr or "unknown"
    if _check_rate_limit(f"login:{client_ip}"):
        return jsonify({"error": "Too many login attempts. Please wait a few minutes."}), 429

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    identifier = data.get("username") or data.get("email")
    password = data.get("password")

    if not identifier or not password:
        return jsonify({"error": "Username/email and password required"}), 400

    db = get_db()
    try:
        user = db.execute(
            "SELECT * FROM users WHERE username = ? OR email = ?",
            (identifier, identifier),
        ).fetchone()

        if not user:
            return jsonify({"error": "Invalid credentials"}), 401

        if not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
            return jsonify({"error": "Invalid credentials"}), 401

        # Create session
        token = str(uuid.uuid4())
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.execute(
            "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), user["id"], token, expires_at),
        )
        db.commit()

        user_dict = dict(user)
        return jsonify({
            "user": _serialize_user(user_dict),
            "token": token,
        })

    finally:
        db.close()


def _serialize_user(u):
    """Serialize a user dict/Row to the JSON shape the client expects."""
    return {
        "id": u["id"],
        "username": u["username"],
        "email": u["email"],
        "displayName": u["display_name"],
        "role": u["role"],
        "avatarUrl": u["avatar_url"],
        "invited": bool(u.get("invited", 1)),
        "studentNumber": u.get("student_number"),
        "studyGroup": u.get("study_group"),
        "studyProgram": u.get("study_program"),
    }


@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    """Get current user info."""
    return jsonify(_serialize_user(request.user))


@auth_bp.route("/me", methods=["PUT"])
@require_auth
def update_me():
    """Update current user's display name and/or avatar."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:
        updates = []
        params = []
        if "display_name" in data and data["display_name"].strip():
            display_name = data["display_name"].strip()
            if len(display_name) > 100:
                return jsonify({"error": "Display name must be at most 100 characters"}), 400
            updates.append("display_name = ?")
            params.append(display_name)
        if "avatar_url" in data:
            updates.append("avatar_url = ?")
            params.append(data["avatar_url"])

        # Student ID fields (max 50 chars each)
        for field, col in [
            ("student_number", "student_number"),
            ("study_group", "study_group"),
            ("study_program", "study_program"),
        ]:
            if field in data:
                val = data[field]
                if val is not None:
                    val = str(val).strip()
                    if len(val) > 50:
                        return jsonify({"error": f"{field} must be at most 50 characters"}), 400
                    if not val:
                        val = None
                updates.append(f"{col} = ?")
                params.append(val)

        if not updates:
            return jsonify({"error": "No fields to update"}), 400

        updates.append("updated_at = ?")
        params.append(datetime.utcnow().isoformat())
        params.append(request.user["id"])

        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        db.commit()

        user_row = db.execute("SELECT * FROM users WHERE id = ?", (request.user["id"],)).fetchone()
        return jsonify(_serialize_user(dict(user_row)))
    finally:
        db.close()


@auth_bp.route("/logout", methods=["POST"])
@require_auth
def logout():
    """Invalidate current session."""
    token = request.headers.get("Authorization", "")[7:]
    db = get_db()
    try:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        db.commit()
        return jsonify({"message": "Logged out"})
    finally:
        db.close()
