############################################################
#  [*] Auth — invitation-based registration, login, sessions
#
#  Opaque bearer sessions, NOT JWT: register/login mint a
#  uuid4 token into the sessions table (30 days) and every
#  protected route resolves it through get_current_user.
#  The app must work without login — auth adds features —
#  so registration works with or without an invitation
#  code (no code = role 'student', invited=0, lower trust).
#
#  login and get_current_user BOTH enforce users.active
#  (migration v8): a deactivated account can neither log
#  in nor keep using a token minted before the flag flip.
#
#  require_auth / require_role are the gate decorators
#  every other blueprint imports; both leave the resolved
#  user row on request.user for the handler.
#
#    POST /api/auth/validate-code — pre-check an invite code
#    POST /api/auth/register      — create user + session
#    POST /api/auth/login         — password → session token
#    GET  /api/auth/me            — the caller's own profile
#    PUT  /api/auth/me            — edit own profile (unused)
#    POST /api/auth/logout        — drop the presented session
############################################################


import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
# jwt and current_app are dead imports — nothing in this file
# uses them (sessions are opaque uuid4 tokens, never JWTs)
import jwt
from flask import Blueprint, current_app, jsonify, request

from app.database import get_db

auth_bp = Blueprint("auth", __name__)

# In-memory rate limiter for register/login, keyed per client
# IP ("login:<ip>" / "register:<ip>"). It lives in the one
# Werkzeug process main.py starts with socketio.run, so it
# resets on every restart, and idle keys are never evicted —
# the dict grows with every distinct IP seen until then.
_rate_limit_store: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT_WINDOW = 300  # seconds (5 minutes)
_RATE_LIMIT_MAX = 10  # attempts per window, successes included








############################################################
# _check_rate_limit
############################################################
#
# True when the key has already spent its 10 attempts in the
# last 5 minutes. Every call that is NOT rejected is
# recorded as an attempt — successful logins count too, so
# ten logins from one IP inside the window lock out the
# eleventh. The window is pruned lazily on each call, never
# in the background.
#
# Used by:
#   - register (below) — key "register:<ip>"
#   - login (below) — key "login:<ip>"
############################################################

def _check_rate_limit(key: str) -> bool:
    now = time.time()
    attempts = _rate_limit_store[key]
    _rate_limit_store[key] = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[key]) >= _RATE_LIMIT_MAX:
        return True
    _rate_limit_store[key].append(now)
    return False








############################################################
# get_current_user
############################################################
#
# Resolves the "Authorization: Bearer <token>" header to the
# caller's FULL users row as a dict (password_hash included
# — never jsonify it raw, go through _serialize_user), or
# None for a missing/unknown/expired token or a deactivated
# account. Opens and closes its own DB connection, so every
# gated request pays one connection here plus the handler's.
#
# Expiry: expires_at was written by register/login as an
# aware UTC ISO string ("+00:00"); the offset is stripped
# and compared against utcnow() — both sides are UTC, which
# is the only reason the naive comparison is right. An
# expired row is deleted on the spot, and that lazy purge is
# the only cleanup of expired sessions anywhere (no
# sweeper): rows whose tokens are never presented again stay
# forever.
#
# users.active (migration v8): admin deactivation
# (admin/routes.py) also deletes the user's sessions, so
# this check is the backstop for flags flipped outside that
# route (DbGate, direct SQL) and for a login that raced the
# deactivation.
#
# Used by:
#   - require_auth / require_role (below)
#   - social/routes.py — social_feed, get_profile,
#     get_user_posts (optional auth: None = anonymous view)
#   - news/routes.py — get_feed, get_post, get_poll (same)
#   - chat/events.py imports it but never calls it — the
#     socket path re-implements the lookup as
#     _authenticate_socket (dead import)
############################################################

def get_current_user():
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
        if not user:
            return None

        # Deactivated accounts lose access immediately — even on a session
        # issued before the admin flipped the flag
        if not user["active"]:
            return None

        return dict(user)
    finally:
        db.close()








############################################################
# require_auth
############################################################
#
# Route decorator: 401 {"error": "Authentication required"}
# unless get_current_user resolves a live, active session;
# on success the user dict is stored on request.user for the
# handler. Stack it UNDER the @route decorator.
#
# Used by:
#   - chat/routes.py — 14 routes
#   - social/routes.py — 11 routes
#   - news/routes.py — 6 routes
#   - notifications/routes.py — 4 routes
#   - uploads/routes.py — 1 route
#   - me, update_me, logout (below)
############################################################

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Authentication required"}), 401
        request.user = user
        return f(*args, **kwargs)
    return decorated








############################################################
# require_role
############################################################
#
# Decorator factory: @require_role("admin", "curator") lets
# only those roles through — 401 when anonymous (same as
# require_auth), 403 {"error": "Insufficient permissions"}
# when logged in with another role. Roles are the users.role
# CHECK set: student, teacher, admin, curator. The user dict
# lands on request.user exactly as with require_auth.
#
# Used by:
#   - admin/routes.py — 7 routes ("admin"; two of them
#     "admin", "curator")
#   - scraper/routes.py — 4 routes ("admin")
#   - schedule/routes.py — 1 route ("admin")
#   - notifications/routes.py imports it but never applies
#     it (dead import)
############################################################

def require_role(*roles):
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








############################################################
# validate_invitation_code
############################################################
#
# POST /api/auth/validate-code
#
# Body {"code"}. Checks a code WITHOUT consuming it and
# answers 200 either way: {"valid": true, "role",
# "remainingUses"} or {"valid": false, "error"} — the client
# branches on `valid`, not on the status. Only a missing or
# non-string code is a 400. The three rejections mirror
# register's, in the same order (unknown, exhausted,
# expired). Not rate-limited, unlike register/login — codes
# can be probed freely.
#
# Used by:
#   - services/api/auth.ts — validateInvitationCode (the
#     register screen checks the code before submitting)
############################################################

@auth_bp.route("/validate-code", methods=["POST"])
def validate_invitation_code():
    data = request.get_json()
    if not data or not data.get("code"):
        return jsonify({"error": "Code required"}), 400

    if not isinstance(data["code"], str):
        return jsonify({"error": "Code must be a string"}), 400

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

        # Same naive-UTC comparison as get_current_user: expires_at is
        # an ISO string, the offset is dropped, utcnow() is the other side
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








############################################################
# register
############################################################
#
# POST /api/auth/register
#
# Body {"username", "password", "display_name", "email",
# "invitation_code"?}. Creates the user AND a 30-day session
# in one transaction and answers 201 {"user", "token"}, so
# the client is logged in straight away. The invitation code
# is optional: with a valid one the account takes the code's
# role and invited=1 (higher trust); without one it is a
# 'student' with invited=0 (guest). A code that IS given
# must be valid — a bad code is a 400, never a silent
# downgrade.
#
# Gotchas:
#   - use_count is read and later bumped in separate
#     statements: two registrations racing for the last use
#     of a code can both succeed (over-consumption).
#   - the uniqueness pre-check and the INSERT are separate
#     too; users.username/email are UNIQUE, so a race lands
#     as sqlite3.IntegrityError → 500 instead of the 409.
#   - display_name is length-checked stripped but stored as
#     sent; username/email are stored verbatim — no trim, no
#     lowercase, so uniqueness is case-sensitive.
#   - the response user is hand-built and lacks the
#     avatarUrl key that _serialize_user (login, /me) emits.
#
# Used by:
#   - services/api/auth.ts — registerApi (AuthContext
#     register())
############################################################

@auth_bp.route("/register", methods=["POST"])
def register():
    # STEP 1: rate-limit per client IP before reading the body.
    # remote_addr is the X-Forwarded-For client resolved by
    # ProxyFix (app/__init__.py), not the Caddy container
    # =========================================================
    client_ip = request.remote_addr or "unknown"
    if _check_rate_limit(f"register:{client_ip}"):
        return jsonify({"error": "Too many registration attempts. Please wait a few minutes."}), 429


    # STEP 2: shape-check the body — presence, then type, then
    # the two length rules
    # ========================================================
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    required = ["username", "password", "display_name", "email"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    # JSON can carry numbers/objects here; .encode(), .strip() and
    # len() below all assume str
    for field in required:
        if not isinstance(data[field], str):
            return jsonify({"error": f"{field} must be a string"}), 400

    if len(data["password"]) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    if len(data["display_name"].strip()) > 100:
        return jsonify({"error": "Display name must be at most 100 characters"}), 400


    # STEP 3: resolve role/invited — defaults for the guest path,
    # overridden only by a code that passes the same three checks
    # as validate_invitation_code (here as 400s)
    # ===========================================================
    db = get_db()
    try:
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


        # STEP 4: reject a taken username OR email with one 409 — the
        # UNIQUE constraints are the real guard, this only picks the
        # status code
        # ===========================================================
        existing = db.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (data["username"], data["email"]),
        ).fetchone()
        if existing:
            return jsonify({"error": "Username or email already exists"}), 409


        # STEP 5: insert the user (bcrypt hash, fresh salt) and burn one
        # use of the code — `invited` is 1 only when the code was
        # accepted, so the extra `invite_code and` is redundant
        # ==============================================================
        user_id = str(uuid.uuid4())
        password_hash = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()

        db.execute(
            "INSERT INTO users (id, username, email, display_name, password_hash, role, invited) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, data["username"], data["email"], data["display_name"], password_hash, role, invited),
        )

        if invite_code and invited:
            db.execute(
                "UPDATE invitation_codes SET use_count = use_count + 1 WHERE code = ?",
                (invite_code,),
            )


        # STEP 6: mint the 30-day session in the same commit, then answer
        # with the hand-built user (no avatarUrl — see banner)
        # ===============================================================
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








############################################################
# login
############################################################
#
# POST /api/auth/login
#
# Body {"username" | "email", "password"} — whichever key is
# sent becomes one identifier matched against BOTH columns.
# Answers {"user": _serialize_user(...), "token"} with a
# fresh 30-day session; earlier sessions stay valid (one row
# per device/login, nothing is revoked here). Unknown user
# and wrong password share the same 401 "Invalid
# credentials"; the users.active flag (migration v8) is
# checked only AFTER the password matches, so "Account
# deactivated" (403) is disclosed to the account holder
# alone. Rate-limited per IP, successes included.
#
# Used by:
#   - services/api/auth.ts — loginApi (AuthContext login())
############################################################

@auth_bp.route("/login", methods=["POST"])
def login():
    # STEP 1: rate-limit per client IP (ProxyFix-resolved), counting
    # successful logins as attempts too
    # ==============================================================
    client_ip = request.remote_addr or "unknown"
    if _check_rate_limit(f"login:{client_ip}"):
        return jsonify({"error": "Too many login attempts. Please wait a few minutes."}), 429


    # STEP 2: body — "username" wins over "email" when both are
    # sent; either must be a str or bcrypt/.encode() would blow up
    # ============================================================
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    identifier = data.get("username") or data.get("email")
    password = data.get("password")

    if not identifier or not password:
        return jsonify({"error": "Username/email and password required"}), 400

    if not isinstance(identifier, str) or not isinstance(password, str):
        return jsonify({"error": "Username/email and password must be strings"}), 400


    # STEP 3: look the user up and verify the password; unknown user
    # and wrong password answer the identical 401 on purpose
    # ==============================================================
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

        # Checked after the password so the flag is only revealed to the
        # account holder
        if not user["active"]:
            return jsonify({"error": "Account deactivated"}), 403


        # STEP 4: mint a fresh 30-day session — aware-UTC ISO expiry,
        # the format get_current_user's naive comparison relies on
        # ===========================================================
        token = str(uuid.uuid4())
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.execute(
            "INSERT INTO sessions (id, user_id, token, expires_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), user["id"], token, expires_at),
        )
        db.commit()

        # dict(): _serialize_user needs .get(), which sqlite3.Row lacks
        user_dict = dict(user)
        return jsonify({
            "user": _serialize_user(user_dict),
            "token": token,
        })

    finally:
        db.close()








############################################################
# _serialize_user
############################################################
#
# The public user shape (camelCase) shared by login and
# GET/PUT /me: id, username, email, displayName, role,
# avatarUrl, invited, studentNumber, studyGroup,
# studyProgram. It doubles as the whitelist — password_hash,
# active and the timestamps never leave. Takes a DICT: it
# uses .get(), which sqlite3.Row lacks, hence the dict(...)
# at every call site. `invited` defaults to 1 when the key
# is missing (partial dicts only — migration v3 gave every
# row the column).
#
# Used by:
#   - login, me, update_me (below)
############################################################

def _serialize_user(u):
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








############################################################
# me
############################################################
#
# GET /api/auth/me
#
# The caller's own profile straight from request.user — no
# second query; require_auth already loaded the row and
# turned an expired or deactivated session into a 401. The
# mobile app treats that 401 as "session dead, clear the
# stored auth state".
#
# Used by:
#   - services/api/auth.ts — fetchMe (AuthContext hydration
#     check; the student-card screen's pull-to-refresh)
############################################################

@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    return jsonify(_serialize_user(request.user))








############################################################
# update_me
############################################################
#
# PUT /api/auth/me
#
# Partial update of the caller's own row. Accepts camelCase
# (what GET returns) or snake_case for every field:
# displayName ≤100 chars, avatarUrl (stored verbatim, null
# and "" included), studentNumber / studyGroup /
# studyProgram ≤50 chars each with blank → NULL. Answers the
# re-read row via _serialize_user. The SET list is built by
# f-string from a fixed column whitelist — no client string
# reaches the SQL text.
#
# Gotchas:
#   - an empty/whitespace displayName is silently ignored
#     rather than rejected; only a body with NO usable field
#     is a 400.
#   - updated_at is written as utcnow().isoformat() ("T",
#     microseconds) while the column default is
#     datetime('now') ("YYYY-MM-DD HH:MM:SS") — two formats
#     share the column.
#   - the 50-char error names the key the client sent
#     (camel or snake), not the column.
#
# Used by:
#   - nothing in the mobile app — profile edits go through
#     PUT /api/social/profile (services/api/social.ts —
#     updateProfile), a parallel implementation in
#     social/routes.py update_profile
############################################################

@auth_bp.route("/me", methods=["PUT"])
@require_auth
def update_me():
    # STEP 1: body, then collect "col = ?" fragments + params for
    # every field actually present
    # ===========================================================
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:
        updates = []
        params = []
        # STEP 1.1: display name — camelCase key wins when both are sent;
        # falsy or blank values fall through untouched (no error)
        dn_key = "displayName" if "displayName" in data else "display_name"
        if dn_key in data and data[dn_key] and str(data[dn_key]).strip():
            display_name = str(data[dn_key]).strip()
            if len(display_name) > 100:
                return jsonify({"error": "Display name must be at most 100 characters"}), 400
            updates.append("display_name = ?")
            params.append(display_name)
        # STEP 1.2: avatar — any present value is written as-is, so null
        # and "" are the way to clear it
        av_key = "avatarUrl" if "avatarUrl" in data else "avatar_url"
        if av_key in data:
            updates.append("avatar_url = ?")
            params.append(data[av_key])

        # STEP 1.3: student-card fields, ≤50 chars after strip; an
        # explicit null or a blank string both store NULL
        for camel, snake, col in [
            ("studentNumber", "student_number", "student_number"),
            ("studyGroup", "study_group", "study_group"),
            ("studyProgram", "study_program", "study_program"),
        ]:
            field = camel if camel in data else snake
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


        # STEP 2: one UPDATE from the whitelisted fragments, stamp
        # updated_at (isoformat — see banner), re-read and serialize
        # ==========================================================
        updates.append("updated_at = ?")
        params.append(datetime.utcnow().isoformat())
        params.append(request.user["id"])

        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        db.commit()

        user_row = db.execute("SELECT * FROM users WHERE id = ?", (request.user["id"],)).fetchone()
        return jsonify(_serialize_user(dict(user_row)))
    finally:
        db.close()








############################################################
# logout
############################################################
#
# POST /api/auth/logout
#
# Deletes the session behind the presented token only —
# other devices' sessions stay valid. The [7:] slice trusts
# require_auth, which already proved the "Bearer " prefix.
# Always 200 {"message": "Logged out"} once past the
# decorator; a token that is already gone gets the 401 there
# instead. The mobile app fires this best-effort and tears
# down locally regardless.
#
# Used by:
#   - services/api/auth.ts — logoutApi (AuthContext
#     logout())
############################################################

@auth_bp.route("/logout", methods=["POST"])
@require_auth
def logout():
    token = request.headers.get("Authorization", "")[7:]
    db = get_db()
    try:
        db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        db.commit()
        return jsonify({"message": "Logged out"})
    finally:
        db.close()
