############################################################
#  [*] Auth — invitation-based registration, login, sessions
#
#  Opaque bearer sessions, NOT JWT: register/login mint a
#  uuid4 token, store its sha256 in the sessions table
#  (30 days; migration v13 rewrote the old plaintext rows)
#  and hand the client the raw token, so a DB or backup
#  leak never yields a usable bearer token. Every protected
#  route resolves the presented token through
#  get_current_user → resolve_session_token. The app must
#  work without login — auth adds features — so
#  registration works with or without an invitation code
#  (no code = role 'student', invited=0, lower trust).
#
#  login and resolve_session_token BOTH enforce
#  users.active (migration v8): a deactivated account can
#  neither log in nor keep using a token minted before the
#  flag flip.
#
#  require_auth / require_role are the gate decorators
#  every other blueprint imports; both leave the resolved
#  user row on request.user for the handler (narrowed
#  columns — password_hash never leaves this module). Also
#  exported for the other blueprints: get_json_object
#  (dict-or-None body parsing), rate_limit /
#  _check_rate_limit (per-user / per-IP write quotas),
#  resolve_session_token (the socket handshake's lookup)
#  and ROLES / PRIVILEGED_ROLES (the one role whitelist).
#
#    POST /api/auth/validate-code    — pre-check an invite code
#    POST /api/auth/register         — create user + session
#    POST /api/auth/login            — password → session token
#    GET  /api/auth/me               — the caller's own profile
#    PUT  /api/auth/me               — edit own profile (unused)
#    POST /api/auth/change-password  — new password, other sessions drop
#    GET  /api/auth/me/export        — the caller's data as JSON (GDPR Art. 15)
#    DELETE /api/auth/me             — erase the caller's account (GDPR Art. 17)
#    POST /api/auth/logout           — drop the presented session
#    POST /api/auth/logout-all       — drop every session of the caller
############################################################


import hashlib
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from functools import wraps

import bcrypt
from flask import Blueprint, g, jsonify, request

from app.database import get_db, utc_now_iso

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

# The one role whitelist — the users/invitation_codes CHECK
# constraints (database/__init__.py) and the mobile role gates
# mirror it. PRIVILEGED_ROLES may only be handed out by a full
# admin (admin/routes.py create_invitation / update_user).
ROLES = ("student", "teacher", "admin", "curator")
PRIVILEGED_ROLES = ("admin", "curator")

# In-memory rate limiter shared by every blueprint (via the
# rate_limit decorator below), keyed "scope:<user id>" for
# authenticated writes and "scope:<ip>" for anonymous ones.
# It lives in the one Werkzeug process main.py starts with
# socketio.run, so it resets on every restart. Monotonic
# stamps (a backward clock step cannot lock anyone out), one
# Lock around every read-modify-write (threaded server), and
# two eviction rules: a key whose pruned window is empty is
# deleted on its own next check, and past _RATE_LIMIT_MAX_KEYS
# the least-recently-touched keys are dropped (LRU) — the
# store can no longer grow without bound on spoofed IPs.
_rate_limit_store: OrderedDict[str, list[float]] = OrderedDict()
_rate_limit_lock = threading.Lock()
_RATE_LIMIT_WINDOW = 300  # seconds (5 minutes)
_RATE_LIMIT_MAX = 10  # attempts per window
_RATE_LIMIT_MAX_KEYS = 4096  # LRU ceiling on distinct keys
_LOGIN_IP_MAX = 30  # failed logins per IP — a NATed campus shares one
_SESSIONS_PER_USER = 10  # newest session rows kept per user at login

# Registration caps. The username charset also blocks
# email-shaped usernames, so login's two-column identifier
# match can never turn ambiguous for new accounts.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EMAIL_MAX = 254
_PASSWORD_MAX_BYTES = 72  # bcrypt silently truncates past 72 bytes

# Small embedded screen against the passwords every cracking
# list opens with — compared lowercased in
# _validate_new_password; entries under 6 chars could never
# match (the minimum-length check runs first)
_COMMON_PASSWORDS = frozenset({
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "password", "password1", "password123", "passw0rd", "qwerty",
    "qwerty123", "abc123", "abcdef", "111111", "121212", "123123",
    "letmein", "welcome", "monkey", "dragon", "iloveyou", "sunshine",
    "princess", "football", "admin123", "slaptazodis", "labas123",
})

# Burned once at import so the unknown-identifier 401 costs the
# same bcrypt work as a wrong password — login timing must not
# disclose whether an account exists
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"knfapp-timing-equalizer", bcrypt.gensalt())








############################################################
# _check_rate_limit
############################################################
#
# True when the key has already spent its attempt budget in
# the last 5 minutes — 10 by default, or the caller's
# max_attempts (validate-code gets 30: the register screen
# re-validates on a 600 ms typing debounce, so one honest
# code entry burns a dozen calls). Every call that is NOT
# rejected is recorded as an attempt, unless record=False
# turns the call into a pure budget probe: register/login/
# change_password probe here first and record separately
# (_record_attempt) only once the body validated / the
# authentication failed, so malformed retries and
# successful sign-ins no longer burn budget. The window is
# pruned lazily per key under the module lock; empty keys
# are deleted and the store is LRU-capped (see the comment
# on _rate_limit_store).
#
# Used by:
#   - rate_limit (below) — the decorator the other
#     blueprints put on their write routes
#   - validate_invitation_code (below) — key
#     "validate:<ip>", max_attempts 30
#   - register, login, change_password (below) —
#     record=False probes
############################################################

def _check_rate_limit(key: str, max_attempts: int = _RATE_LIMIT_MAX, record: bool = True) -> bool:
    now = time.monotonic()
    with _rate_limit_lock:
        attempts = [t for t in _rate_limit_store.get(key, []) if now - t < _RATE_LIMIT_WINDOW]
        if len(attempts) >= max_attempts:
            _rate_limit_store[key] = attempts
            _rate_limit_store.move_to_end(key)
            return True
        if record:
            attempts.append(now)
        if attempts:
            _rate_limit_store[key] = attempts
            _rate_limit_store.move_to_end(key)
        else:
            # A pruned-empty key is dropped, never kept — the reject
            # path can no longer grow the dict (defaultdict used to)
            _rate_limit_store.pop(key, None)
        while len(_rate_limit_store) > _RATE_LIMIT_MAX_KEYS:
            _rate_limit_store.popitem(last=False)
        return False








############################################################
# _record_attempt
############################################################
#
# Appends one attempt stamp to the key's window — the other
# half of a record=False _check_rate_limit probe. register
# calls it once the body passed shape validation; login and
# change_password only on a failed authentication, so an
# attacker's guesses fill the bucket, never an honest
# user's successes.
#
# Used by:
#   - register, login, change_password (below)
############################################################

def _record_attempt(key: str) -> None:
    now = time.monotonic()
    with _rate_limit_lock:
        attempts = [t for t in _rate_limit_store.get(key, []) if now - t < _RATE_LIMIT_WINDOW]
        attempts.append(now)
        _rate_limit_store[key] = attempts
        _rate_limit_store.move_to_end(key)
        while len(_rate_limit_store) > _RATE_LIMIT_MAX_KEYS:
            _rate_limit_store.popitem(last=False)








############################################################
# _rate_limited_response
############################################################
#
# The one 429 shape ({error, code: rate_limited} — the app
# translates off the code) plus an additive Retry-After
# header computed from the key's oldest stamp still inside
# the window: the seconds until that stamp ages out and one
# attempt frees up. No stamps means the window is already
# open — 1 second goes out rather than 0 so clients never
# busy-loop.
#
# Used by:
#   - rate_limit (below)
#   - validate_invitation_code, register, login,
#     change_password (below)
############################################################

def _rate_limited_response(message: str, key: str):
    now = time.monotonic()
    with _rate_limit_lock:
        attempts = [t for t in _rate_limit_store.get(key, []) if now - t < _RATE_LIMIT_WINDOW]
    retry_after = int(_RATE_LIMIT_WINDOW - (now - min(attempts))) + 1 if attempts else 1
    response = jsonify({"error": message, "code": "rate_limited"})
    response.headers["Retry-After"] = str(max(1, retry_after))
    return response, 429








############################################################
# rate_limit
############################################################
#
# Decorator factory: @rate_limit("post", max_attempts=20)
# spends one attempt per call — keyed "<scope>:<user id>"
# when require_auth already resolved the caller (stack it
# UNDER require_auth) and "<scope>:<ip>" for anonymous
# routes — and answers the house 429 with Retry-After once
# the budget is gone. The shared write-route limiter for
# the other blueprints; auth's own routes call the
# primitives directly because their record points differ
# (see _record_attempt).
#
# Used by:
#   - chat/routes.py, news/routes.py, social/routes.py,
#     uploads/routes.py, notifications/routes.py — write
#     routes (each blueprint picks its own quotas)
#   - nothing in this file — register/login need the split
#     check/record primitives instead
############################################################

def rate_limit(scope: str, max_attempts: int = _RATE_LIMIT_MAX):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = getattr(request, "user", None)
            actor = user["id"] if user else (request.remote_addr or "unknown")
            key = f"{scope}:{actor}"
            if _check_rate_limit(key, max_attempts):
                logger.warning("Rate limit hit: %s", key)
                return _rate_limited_response("Too many requests. Please wait a few minutes.", key)
            return f(*args, **kwargs)
        return decorated
    return decorator








############################################################
# get_json_object
############################################################
#
# The parsed JSON body when it is a dict, None otherwise —
# a top-level array, scalar, malformed JSON or a missing
# body all come back None instead of raising, so a write
# route's data.get(...) can never 500 on a non-dict body
# (get_json(silent=False) used to turn a "[1,2]" body into
# an AttributeError). Callers answer their own 400 ("JSON
# body required") on a falsy result, keeping the existing
# error shape.
#
# Used by:
#   - validate_invitation_code, register, login, update_me,
#     change_password, logout (below)
#   - news/social/chat/notifications/admin routes — every
#     write route's body parse
############################################################

def get_json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None








############################################################
# _hash_token
############################################################
#
# sha256 hex of a bearer token — what sessions.token stores
# since migration v13. The client keeps the raw uuid4; the
# DB (readable via DbGate, carried in backups) keeps only
# the hash, and every lookup/delete goes through this.
#
# Used by:
#   - resolve_session_token, register, login,
#     change_password, logout (below)
#   - chat/events.py resolves socket tokens through
#     resolve_session_token, so the socket path hashes
#     here too
############################################################

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()








############################################################
# _bearer_token
############################################################
#
# The token from "Authorization: Bearer <token>", parsed
# once with partition and matched case-insensitively
# (RFC 7235: auth schemes are not case-sensitive), or None
# for any other header shape. logout/change_password reuse
# this instead of re-slicing [7:].
#
# Used by:
#   - get_current_user, change_password, logout (below)
############################################################

def _bearer_token():
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None








############################################################
# resolve_session_token
############################################################
#
# The one token → user lookup for BOTH transports: REST
# (get_current_user, below) and the socket handshake
# (chat/events.py _authenticate_socket), so the two paths
# can never drift apart again — the socket copy used to
# skip the users.active check and the expired-row purge.
# Takes the RAW token, looks up its sha256, and returns
# the user as a dict narrowed to the columns handlers and
# _serialize_user actually use — password_hash never
# leaves this module — or None for an unknown/expired
# token or a deactivated account.
#
# Expiry: expires_at is compared aware-to-aware
# (fromisoformat keeps the offset; a naive legacy value is
# assumed UTC) and an unparseable value counts as expired
# — a 401, never a 500. An expired row is purged on the
# spot together with the user's push_tokens rows (a device
# that can no longer authenticate must not keep getting
# message previews; live devices re-register on their next
# app start); the purge is best-effort — during a writer's
# lock window it is skipped and the caller still gets a
# clean None instead of a 500.
#
# users.active (migration v8): admin deactivation
# (admin/routes.py) also deletes the user's sessions, so
# this check is the backstop for flags flipped outside that
# route (DbGate, direct SQL) and for a login that raced the
# deactivation.
#
# Used by:
#   - get_current_user (below)
#   - chat/events.py — _authenticate_socket (the socket
#     handshake shares this exact lookup)
############################################################

def resolve_session_token(token):
    # STEP 1: the sessions row, by token hash
    # =======================================
    db = get_db()
    try:
        session = db.execute(
            "SELECT s.user_id, s.expires_at FROM sessions s WHERE s.token = ?",
            (_hash_token(token),),
        ).fetchone()

        if not session:
            return None


        # STEP 2: aware-to-aware expiry; malformed = expired. The
        # lazy purge (sessions row + push tokens) is best-effort —
        # a locked DB must yield a 401, not a 500
        # ========================================================
        try:
            expires = datetime.fromisoformat(session["expires_at"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            expires = None

        if expires is None or expires < datetime.now(timezone.utc):
            try:
                db.execute("DELETE FROM sessions WHERE token = ?", (_hash_token(token),))
                # Push dies with the session — a device that can no
                # longer authenticate must not keep getting previews
                db.execute("DELETE FROM push_tokens WHERE user_id = ?", (session["user_id"],))
                db.commit()
            except sqlite3.OperationalError:
                logger.warning("Expired-session purge skipped (database locked)")
            return None


        # STEP 3: the user, narrowed to the public columns, then
        # the active backstop
        # ======================================================
        user = db.execute(
            """SELECT id, username, email, display_name, role, avatar_url, invited,
                      active, student_number, study_group, study_program
               FROM users WHERE id = ?""",
            (session["user_id"],),
        ).fetchone()
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
# get_current_user
############################################################
#
# Resolves the "Authorization: Bearer <token>" header
# through resolve_session_token (above) and caches the
# result on flask.g for the rest of the request, so a
# request that resolves its caller more than once pays one
# lookup, not two. None for a missing/unknown/expired
# token or a deactivated account, exactly as before; the
# cache keeps negative results too, keyed by the token, so
# a different header value would still resolve fresh.
#
# Used by:
#   - require_auth / require_role (below)
#   - social/routes.py — social_feed, get_profile,
#     get_user_posts (optional auth: None = anonymous view)
#   - news/routes.py — get_feed, get_post, get_poll (same)
#   - chat/events.py imports it but never calls it — the
#     socket path calls resolve_session_token directly
############################################################

def get_current_user():
    token = _bearer_token()
    if not token:
        return None

    # One resolution per request — flask.g dies with the request
    cached = getattr(g, "_auth_cache", None)
    if cached is not None and cached[0] == token:
        return cached[1]

    user = resolve_session_token(token)
    g._auth_cache = (token, user)
    return user








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
#   - me, update_me, change_password, logout, logout_all
#     (below)
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
# CHECK set — the module-level ROLES tuple above. The user
# dict lands on request.user exactly as with require_auth.
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
# "remainingUses"} or {"valid": false, "error", "code",
# "reason"} — the client branches on `valid`, not on the
# status, keys its translations off `reason` (unknown /
# exhausted / expired) and never shows the English `error`
# prose. Only a missing or non-string code is a 400. The
# three rejections mirror register's, in the same order and
# with the same machine codes. Rate-limited per IP like
# register/login, but with a 30-attempt budget — the
# register screen re-validates on a 600 ms typing debounce,
# so one honest code entry is a dozen calls; the limit only
# bites bulk probing.
#
# Used by:
#   - services/api/auth.ts — validateInvitationCode (the
#     register screen checks the code before submitting)
############################################################

@auth_bp.route("/validate-code", methods=["POST"])
def validate_invitation_code():
    # Same per-IP store as register/login, triple the budget —
    # the debounced typing UX must fit under it
    client_ip = request.remote_addr or "unknown"
    if _check_rate_limit(f"validate:{client_ip}", max_attempts=30):
        return _rate_limited_response("Too many attempts. Please wait a few minutes.", f"validate:{client_ip}")

    data = get_json_object()
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
            return jsonify({"valid": False, "error": "Invalid invitation code",
                            "code": "invite_invalid", "reason": "unknown"}), 200

        if invite["use_count"] >= invite["max_uses"]:
            return jsonify({"valid": False, "error": "Invitation code has been fully used",
                            "code": "invite_exhausted", "reason": "exhausted"}), 200

        # Aware-to-aware like resolve_session_token; a malformed
        # expires_at counts as expired instead of raising a 500
        try:
            invite_expires = datetime.fromisoformat(invite["expires_at"])
            if invite_expires.tzinfo is None:
                invite_expires = invite_expires.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            invite_expires = None
        if invite_expires is None or invite_expires < datetime.now(timezone.utc):
            return jsonify({"valid": False, "error": "Invitation code has expired",
                            "code": "invite_expired", "reason": "expired"}), 200

        return jsonify({
            "valid": True,
            "role": invite["role"],
            "remainingUses": invite["max_uses"] - invite["use_count"],
        })
    finally:
        db.close()








############################################################
# _validate_new_password
############################################################
#
# The one password policy, shared by register and
# change_password: 6 chars minimum (raising it would lock
# existing habits out of a live app — flagged for a human
# call, not done here), 72 BYTES maximum (bcrypt silently
# truncates past 72, so a longer password would equal its
# prefix), must not contain the username or the email's
# local part (case-insensitive; local parts under 3 chars
# are skipped — too noisy), and must not be one of the
# embedded common passwords. Returns the English error
# prose or None; callers answer 400 with code
# weak_password — the slug the app already translates.
#
# Used by:
#   - register, change_password (below)
############################################################

def _validate_new_password(password, username, email):
    if len(password) < 6:
        return "Password must be at least 6 characters"
    if len(password.encode("utf-8")) > _PASSWORD_MAX_BYTES:
        return "Password must be at most 72 characters"

    lowered = password.lower()
    if username and username.lower() in lowered:
        return "Password must not contain your username"
    local_part = (email or "").split("@", 1)[0].lower()
    if len(local_part) >= 3 and local_part in lowered:
        return "Password must not contain your email"

    if lowered in _COMMON_PASSWORDS:
        return "Password is too common"
    return None








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
# Validation caps: username 3–32 chars of [A-Za-z0-9._-]
# (blocks email-shaped names), email trimmed + lowercased
# (one canonical shape — login matches COLLATE NOCASE) with
# a ≤254-char conservative shape check, password through
# _validate_new_password, display_name stripped then 1–100
# chars and STORED stripped. The rate-limit attempt is
# recorded only after the body validates — malformed
# retries no longer eat an honest user's budget.
#
# The invitation code is consumed ATOMICALLY: a conditional
# UPDATE (use_count < max_uses AND not expired) burns one
# use before the user INSERT, and rowcount 0 answers the
# same 400 slugs as the pre-checks — two registrations
# racing for the last use can no longer both win, and a
# code revoked or deleted mid-flight is caught the same
# way. Its expiry side goes through julianday() rather than
# a text comparison, so a stored offset means the same
# instant to the burn as to the Python parse above and to
# validate-code — a code that screen calls valid registers.
# Nothing commits until the session mint, so a later
# 409 discards the burn.
#
# The uniqueness pre-check (COLLATE NOCASE — 'Tomas' blocks
# a new 'tomas', mirroring login's lookup) stays the fast
# path; the INSERT itself catches sqlite3.IntegrityError so
# the race answers the same 409 instead of a 500. Error
# bodies carry a stable machine `code` next to the English
# prose (rate_limited, weak_password, invalid_username,
# invalid_email, invite_invalid, invite_exhausted,
# invite_expired, username_taken) — the app translates off
# the code and never shows the prose.
#
# Used by:
#   - services/api/auth.ts — registerApi (AuthContext
#     register())
############################################################

@auth_bp.route("/register", methods=["POST"])
def register():
    # STEP 1: probe the per-IP budget WITHOUT spending it —
    # remote_addr is the ProxyFix-resolved client, and the
    # attempt is recorded only once STEP 2 validates
    # =====================================================
    client_ip = request.remote_addr or "unknown"
    rl_key = f"register:{client_ip}"
    if _check_rate_limit(rl_key, record=False):
        return _rate_limited_response("Too many registration attempts. Please wait a few minutes.", rl_key)


    # STEP 2: shape-check the body — presence, then type, then
    # the caps (username charset, email shape, password policy,
    # display_name stripped 1–100 and stored stripped)
    # =========================================================
    data = get_json_object()
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

    username = data["username"]
    if not _USERNAME_RE.fullmatch(username):
        return jsonify({"error": "Username must be 3-32 characters: letters, digits, dots, dashes or underscores",
                        "code": "invalid_username"}), 400

    # One canonical email shape — login matches COLLATE NOCASE, so
    # mixed-case sign-ins resolve to this row; username stays as typed
    email = data["email"].strip().lower()
    if len(email) > _EMAIL_MAX or not _EMAIL_RE.fullmatch(email):
        return jsonify({"error": "Invalid email address", "code": "invalid_email"}), 400

    password_error = _validate_new_password(data["password"], username, email)
    if password_error:
        return jsonify({"error": password_error, "code": "weak_password"}), 400

    display_name = data["display_name"].strip()
    if not display_name:
        return jsonify({"error": "Display name cannot be empty"}), 400
    if len(display_name) > 100:
        return jsonify({"error": "Display name must be at most 100 characters"}), 400

    # The body validated — only NOW does the attempt spend budget
    _record_attempt(rl_key)


    # STEP 3: resolve role/invited — defaults for the guest path,
    # overridden only by a code that passes the same three checks
    # as validate_invitation_code (here as 400s), then is burned
    # ATOMICALLY so a racing twin cannot reuse the last slot
    # ===========================================================
    db = get_db()
    try:
        raw_code = data.get("invitation_code")
        if raw_code is not None and not isinstance(raw_code, str):
            return jsonify({"error": "invitation_code must be a string"}), 400
        invite_code = (raw_code or "").strip()
        role = "student"
        invited = 0

        if invite_code:
            invite = db.execute(
                "SELECT * FROM invitation_codes WHERE code = ?",
                (invite_code,),
            ).fetchone()

            if not invite:
                return jsonify({"error": "Invalid invitation code", "code": "invite_invalid"}), 400

            if invite["use_count"] >= invite["max_uses"]:
                return jsonify({"error": "Invitation code has been fully used", "code": "invite_exhausted"}), 400

            # Aware-to-aware; a malformed expires_at counts as
            # expired instead of raising a 500
            try:
                invite_expires = datetime.fromisoformat(invite["expires_at"])
                if invite_expires.tzinfo is None:
                    invite_expires = invite_expires.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                invite_expires = None
            if invite_expires is None or invite_expires < datetime.now(timezone.utc):
                return jsonify({"error": "Invitation code has expired", "code": "invite_expired"}), 400

            # STEP 3.1: the atomic burn — the conditional UPDATE is the
            # real guard (the checks above only pick the error slug);
            # rowcount 0 means a racer took the last use or the row was
            # revoked/deleted under us, and the re-read names which.
            # julianday() on BOTH sides, never a text comparison: it
            # applies the stored offset exactly like the aware-to-aware
            # parse above, so '...T09:00:00-05:00' (14:00 UTC, still in
            # the future) burns instead of sorting under '...T12:00...'
            # and answering invite_expired to a code validate-code just
            # called valid. A malformed stamp gives NULL, so it fails
            # the predicate and falls into the expired slug below
            burned = db.execute(
                "UPDATE invitation_codes SET use_count = use_count + 1"
                " WHERE code = ? AND use_count < max_uses"
                " AND julianday(expires_at) > julianday(?)",
                (invite_code, utc_now_iso()),
            )
            if burned.rowcount == 0:
                current = db.execute(
                    "SELECT use_count, max_uses FROM invitation_codes WHERE code = ?",
                    (invite_code,),
                ).fetchone()
                if current is None:
                    return jsonify({"error": "Invalid invitation code", "code": "invite_invalid"}), 400
                if current["use_count"] >= current["max_uses"]:
                    return jsonify({"error": "Invitation code has been fully used", "code": "invite_exhausted"}), 400
                return jsonify({"error": "Invitation code has expired", "code": "invite_expired"}), 400

            role = invite["role"]
            invited = 1


        # STEP 4: reject a taken username OR email with one 409 —
        # COLLATE NOCASE to mirror login's lookup, so 'Tomas' blocks
        # a new 'tomas' (the BINARY UNIQUE constraints would let the
        # case-variant in, and login would then reach only one row)
        # ===========================================================
        existing = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
            (username, email),
        ).fetchone()
        if existing:
            return jsonify({"error": "Username or email already exists", "code": "username_taken"}), 409


        # STEP 5: insert the user (bcrypt hash, fresh salt); an
        # IntegrityError here is the pre-check's race answering the
        # same 409 — the uncommitted invite burn is discarded with it
        # ===========================================================
        user_id = str(uuid.uuid4())
        password_hash = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()

        try:
            # Explicit T-form stamps — the datetime('now') DEFAULTs
            # write space-form text that sorts wrong (see utc_now_iso)
            now = utc_now_iso()
            db.execute(
                "INSERT INTO users (id, username, email, display_name, password_hash, role, invited, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, email, display_name, password_hash, role, invited, now, now),
            )
        except sqlite3.IntegrityError:
            return jsonify({"error": "Username or email already exists", "code": "username_taken"}), 409


        # STEP 6: mint the 30-day session in the same commit — the DB
        # stores the token's sha256, the client gets the raw uuid4 —
        # and answer through _serialize_user (one user shape, avatarUrl
        # included)
        # =============================================================
        token = str(uuid.uuid4())
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.execute(
            "INSERT INTO sessions (id, user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, _hash_token(token), utc_now_iso(), expires_at),
        )
        db.commit()

        logger.info("Registered user %s (%s) role=%s invited=%d from %s",
                    user_id, username, role, invited, client_ip)
        if invited:
            logger.info("Invitation code %r consumed by user %s", invite_code, user_id)

        return jsonify({
            "user": _serialize_user({
                "id": user_id,
                "username": username,
                "email": email,
                "display_name": display_name,
                "role": role,
                "invited": invited,
                "avatar_url": None,
            }),
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
# sent becomes one identifier matched against BOTH columns
# COLLATE NOCASE, so "Jonas@X.lt" finds the account register
# stored lowercased (and a legacy mixed-case row alike);
# should two old rows differ only in case, the password is
# tried against each and the row it verifies for wins, so
# both accounts stay reachable (register now pre-checks
# NOCASE, so no new such pairs are minted).
# Answers {"user": _serialize_user(...), "token"} with a
# fresh 30-day session (sha256 at rest); earlier sessions
# stay valid but only the newest _SESSIONS_PER_USER rows
# per user survive — the cap that keeps the table bounded.
#
# Rate limiting: two buckets, BOTH counting only FAILED
# authentications — ten successful sign-ins can no longer
# lock a NATed campus out. Per-IP ("login:<ip>",
# _LOGIN_IP_MAX per 5 min, probed before the body) and
# per-identifier ("login:id:<lowercased>", 10 per 5 min) —
# spoofing X-Forwarded-For therefore still cannot hammer
# one account. The 429s carry Retry-After.
#
# Unknown user and wrong password share the same 401
# "Invalid credentials" (code invalid_credentials), and an
# unknown identifier burns one bcrypt against a dummy hash
# so response timing does not disclose account existence. A
# candidate row whose password_hash is not usable bcrypt
# (hand-edited, half-restored) is logged and skipped as a
# non-match — one bad row can no longer turn the route into
# a public 500.
# The users.active flag (migration v8) is checked only
# AFTER the password matches, so "Account deactivated"
# (403, code account_deactivated) is disclosed to the
# account holder alone.
#
# Used by:
#   - services/api/auth.ts — loginApi (AuthContext login())
############################################################

@auth_bp.route("/login", methods=["POST"])
def login():
    # STEP 1: probe the per-IP failure budget (ProxyFix-resolved
    # client) — nothing is recorded until an authentication FAILS
    # ===========================================================
    client_ip = request.remote_addr or "unknown"
    ip_key = f"login:{client_ip}"
    if _check_rate_limit(ip_key, max_attempts=_LOGIN_IP_MAX, record=False):
        return _rate_limited_response("Too many login attempts. Please wait a few minutes.", ip_key)


    # STEP 2: body — "username" wins over "email" when both are
    # sent; either must be a str or bcrypt/.encode() would blow up
    # ============================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    identifier = data.get("username") or data.get("email")
    password = data.get("password")

    if not identifier or not password:
        return jsonify({"error": "Username/email and password required"}), 400

    if not isinstance(identifier, str) or not isinstance(password, str):
        return jsonify({"error": "Username/email and password must be strings"}), 400

    # STEP 2.1: the per-identifier bucket — the strict limit an
    # X-Forwarded-For spoofer cannot dodge
    id_key = f"login:id:{identifier.strip().lower()}"
    if _check_rate_limit(id_key, record=False):
        return _rate_limited_response("Too many login attempts. Please wait a few minutes.", id_key)


    # STEP 3: look the user up and verify the password; unknown user
    # and wrong password answer the identical 401 on purpose, at the
    # same bcrypt cost (dummy hash when there is no candidate row)
    # ==============================================================
    db = get_db()
    try:
        # NOCASE on both columns — register lowercases the stored
        # email, this side forgives whatever case the user types.
        # Legacy rows can still differ only in case (the pre-check
        # was BINARY once), so try the password against EVERY match
        # instead of fetchone() — the row it verifies for wins and
        # each such account stays reachable by its own password
        candidates = db.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE",
            (identifier, identifier),
        ).fetchall()

        user = None
        for row in candidates:
            # Nothing this app writes is anything but a bcrypt hash, but
            # a hand-edited row (DbGate, a half-restored backup, a legacy
            # import) makes checkpw raise ValueError — an unauthenticated
            # 500 on a name anyone can type. Such a row simply never
            # matches, so the caller gets the documented 401 instead
            try:
                matched = bcrypt.checkpw(password.encode(), (row["password_hash"] or "").encode())
            except (TypeError, ValueError):
                logger.error("User %s has an unusable password_hash — treated as no match", row["id"])
                continue
            if matched:
                user = row
                break

        if not candidates:
            # Same work as one real check — account existence must not
            # be readable off the response time
            bcrypt.checkpw(password.encode(), _DUMMY_PASSWORD_HASH)

        if not user:
            # Only failures fill the buckets — see the banner
            _record_attempt(ip_key)
            _record_attempt(id_key)
            logger.warning("Failed login for %r from %s", identifier, client_ip)
            return jsonify({"error": "Invalid credentials", "code": "invalid_credentials"}), 401

        # Checked after the password so the flag is only revealed to the
        # account holder
        if not user["active"]:
            logger.warning("Login on deactivated account %s from %s", user["id"], client_ip)
            return jsonify({"error": "Account deactivated", "code": "account_deactivated"}), 403


        # STEP 4: mint a fresh 30-day session (sha256 stored, raw
        # token answered) and trim the user's rows to the newest
        # _SESSIONS_PER_USER — the cap that bounds the table
        # ======================================================
        token = str(uuid.uuid4())
        expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        db.execute(
            "INSERT INTO sessions (id, user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user["id"], _hash_token(token), utc_now_iso(), expires_at),
        )
        db.execute(
            """DELETE FROM sessions WHERE user_id = ? AND id NOT IN (
                   SELECT id FROM sessions WHERE user_id = ?
                   ORDER BY expires_at DESC, created_at DESC, id DESC LIMIT ?)""",
            (user["id"], user["id"], _SESSIONS_PER_USER),
        )
        db.commit()

        logger.info("Login: user=%s from %s", user["id"], client_ip)

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
# The public user shape (camelCase) shared by register,
# login and GET/PUT /me: id, username, email, displayName,
# role, avatarUrl, invited, studentNumber, studyGroup,
# studyProgram. It doubles as the whitelist —
# password_hash, active and the timestamps never leave.
# Takes a plain DICT (sqlite3.Row lacks .get(), hence the
# dict(...) at every call site) and tolerates a partial
# one: every column falls back to None via .get(), so
# register's hand-built dict serializes the same shape as
# a full row instead of raising KeyError. `invited`
# defaults to 1 when the key is missing (partial dicts
# only — migration v3 gave every row the column).
#
# Used by:
#   - register, login (above); me, update_me (below)
############################################################

def _serialize_user(u):
    return {
        "id": u.get("id"),
        "username": u.get("username"),
        "email": u.get("email"),
        "displayName": u.get("display_name"),
        "role": u.get("role"),
        "avatarUrl": u.get("avatar_url"),
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
# _delete_replaced_upload
############################################################
#
# Best-effort disk cleanup when a profile update replaces
# an /api/uploads/ avatar: hands the old path to the
# uploads package's shared delete helper. Guarded lazy
# import — until that helper lands in uploads/routes.py
# this is a silent no-op, and a failed delete never fails
# the profile update that triggered it.
#
# Used by:
#   - update_me (below)
############################################################

def _delete_replaced_upload(path):
    try:
        from app.uploads.routes import delete_upload
    except ImportError:
        return
    try:
        delete_upload(path)
    except Exception:
        logger.warning("Could not delete replaced upload %s", path)








############################################################
# update_me
############################################################
#
# PUT /api/auth/me
#
# Partial update of the caller's own row. Accepts camelCase
# (what GET returns) or snake_case for every field:
# displayName a non-empty string ≤100 chars after strip (a
# present-but-blank value is a 400 now, not a silent skip
# answering 200 with the old name), avatarUrl (a relative
# /api/uploads/ path only — null and "" clear it; an
# absolute URL would beacon every avatar render to a
# user-picked host), studentNumber / studyGroup /
# studyProgram strings ≤50 chars each with blank → NULL (a
# non-string is a 400 naming the key the client sent).
# Answers the re-read row via _serialize_user; a row
# deleted between the auth check and the re-read answers
# 401 instead of a TypeError 500. The SET list is built by
# f-string from a fixed column whitelist — no client
# string reaches the SQL text.
#
# Side effects of a display-name change: news_posts rows
# by this author get their author_name snapshot rewritten
# in the same transaction, so old posts stop pairing the
# new avatar with the old name. A replaced /api/uploads/
# avatar goes to _delete_replaced_upload after the commit.
# updated_at is stamped with utc_now_iso() — the house
# T-form UTC shape migration v17 normalised the whole
# column to (the space-form DEFAULT sorts wrong against
# it and must never fire).
#
# Used by:
#   - nothing in the mobile app — profile edits go through
#     PUT /api/social/profile (services/api/social.ts —
#     updateProfile), a parallel implementation in
#     social/routes.py update_profile — change both
############################################################

@auth_bp.route("/me", methods=["PUT"])
@require_auth
def update_me():
    # STEP 1: body, then collect "col = ?" fragments + params for
    # every field actually present
    # ===========================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:
        updates = []
        params = []
        new_display_name = None
        replaced_avatar = None
        # STEP 1.1: display name — camelCase key wins when both are
        # sent; present-but-blank is a 400, absent is simply skipped
        dn_key = "displayName" if "displayName" in data else "display_name"
        if dn_key in data:
            if not isinstance(data[dn_key], str):
                return jsonify({"error": "display_name must be a string"}), 400
            display_name = data[dn_key].strip()
            if not display_name:
                return jsonify({"error": "Display name cannot be empty"}), 400
            if len(display_name) > 100:
                return jsonify({"error": "Display name must be at most 100 characters"}), 400
            updates.append("display_name = ?")
            params.append(display_name)
            new_display_name = display_name
        # STEP 1.2: avatar — own uploads or clearing only (null and
        # "" clear it); same rule as social/routes.py update_profile.
        # A replaced own upload is remembered for disk cleanup
        av_key = "avatarUrl" if "avatarUrl" in data else "avatar_url"
        if av_key in data:
            av = data[av_key]
            if av not in (None, "") and (not isinstance(av, str) or not av.startswith("/api/uploads/")):
                return jsonify({"error": "avatar_url must be a relative /api/uploads/ path"}), 400
            updates.append("avatar_url = ?")
            params.append(av)
            old_avatar = request.user.get("avatar_url")
            if old_avatar and old_avatar != av and old_avatar.startswith("/api/uploads/"):
                replaced_avatar = old_avatar

        # STEP 1.3: student-card fields — strings only (the 400 names
        # the key the client sent), ≤50 chars after strip; an explicit
        # null or a blank string both store NULL
        for camel, snake, col in [
            ("studentNumber", "student_number", "student_number"),
            ("studyGroup", "study_group", "study_group"),
            ("studyProgram", "study_program", "study_program"),
        ]:
            field = camel if camel in data else snake
            if field in data:
                val = data[field]
                if val is not None:
                    if not isinstance(val, str):
                        return jsonify({"error": f"{field} must be a string"}), 400
                    val = val.strip()
                    if len(val) > 50:
                        return jsonify({"error": f"{field} must be at most 50 characters"}), 400
                    if not val:
                        val = None
                updates.append(f"{col} = ?")
                params.append(val)

        if not updates:
            return jsonify({"error": "No fields to update"}), 400


        # STEP 2: one UPDATE from the whitelisted fragments, stamp
        # updated_at (house T-form via utc_now_iso), rewrite the
        # author_name snapshots on a rename, re-read and serialize
        # ========================================================
        updates.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(request.user["id"])

        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        if new_display_name:
            # Same transaction as the rename — posts never show a
            # half-renamed author
            db.execute(
                "UPDATE news_posts SET author_name = ? WHERE author_id = ?",
                (new_display_name, request.user["id"]),
            )
        db.commit()

        user_row = db.execute("SELECT * FROM users WHERE id = ?", (request.user["id"],)).fetchone()
        if user_row is None:
            # The row vanished between the auth check and the re-read —
            # answer the session-dead 401 the client already handles
            return jsonify({"error": "Authentication required"}), 401

        if replaced_avatar:
            _delete_replaced_upload(replaced_avatar)

        return jsonify(_serialize_user(dict(user_row)))
    finally:
        db.close()








############################################################
# _disconnect_user_sockets
############################################################
#
# Best-effort kill switch for a user's live sockets after
# their sessions changed (logout, logout-all, password
# change): a socket authenticates once at handshake, so
# without this a revoked token kept its realtime feed
# alive. Guarded lazy import — the helper belongs to
# chat/events.py (disconnect_user_sockets); until it lands
# there this is a silent no-op, and a socket-layer failure
# never fails the auth route that triggered it. A device
# whose session is still valid simply reconnects and
# re-authenticates.
#
# Used by:
#   - change_password, logout, logout_all (below)
############################################################

def _disconnect_user_sockets(user_id):
    try:
        from app.chat.events import disconnect_user_sockets
    except ImportError:
        return
    try:
        disconnect_user_sockets(user_id)
    except Exception:
        logger.warning("Could not disconnect sockets for user %s", user_id)








############################################################
# change_password
############################################################
#
# POST /api/auth/change-password
#
# Body {"old_password", "new_password"} (camelCase aliases
# accepted, like PUT /me). Verifies the old password with
# bcrypt against a fresh read of password_hash
# (request.user no longer carries it), runs the new one
# through the same policy as register, rewrites the hash
# and drops every OTHER session of the user in one commit
# — a compromised credential dies everywhere except the
# device doing the rotation. A wrong old password is a 400
# (code invalid_credentials), NOT a 401: the mobile client
# treats any authenticated 401 as "session dead" and would
# tear the login down over a typo. Per-user rate-limited,
# counting failures only.
#
# Used by:
#   - nothing in the mobile app yet — documented in
#     swagger for the settings screen to adopt
############################################################

@auth_bp.route("/change-password", methods=["POST"])
@require_auth
def change_password():
    # STEP 1: per-user budget probe — verifying old passwords is
    # a password oracle, so failures are limited like login's
    # ==========================================================
    user_id = request.user["id"]
    rl_key = f"chpass:{user_id}"
    if _check_rate_limit(rl_key, record=False):
        return _rate_limited_response("Too many attempts. Please wait a few minutes.", rl_key)


    # STEP 2: body, both key spellings, then the shared policy
    # ========================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    old_password = data.get("oldPassword") if "oldPassword" in data else data.get("old_password")
    new_password = data.get("newPassword") if "newPassword" in data else data.get("new_password")
    if not old_password or not new_password:
        return jsonify({"error": "Old and new password required"}), 400
    if not isinstance(old_password, str) or not isinstance(new_password, str):
        return jsonify({"error": "Old and new password must be strings"}), 400

    password_error = _validate_new_password(new_password, request.user.get("username"), request.user.get("email"))
    if password_error:
        return jsonify({"error": password_error, "code": "weak_password"}), 400


    # STEP 3: verify the old password against a fresh hash read,
    # then rewrite it and drop every other session in one commit
    # ==========================================================
    db = get_db()
    try:
        row = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or not bcrypt.checkpw(old_password.encode(), row["password_hash"].encode()):
            _record_attempt(rl_key)
            logger.warning("Password change rejected (wrong old password) for user %s", user_id)
            return jsonify({"error": "Invalid credentials", "code": "invalid_credentials"}), 400

        new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        db.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (new_hash, utc_now_iso(), user_id),
        )
        # Every OTHER session dies — old tokens must not survive a
        # rotation; the presented one (by hash) is the survivor
        current_hash = _hash_token(_bearer_token() or "")
        db.execute("DELETE FROM sessions WHERE user_id = ? AND token != ?", (user_id, current_hash))
        db.commit()

        logger.info("Password changed for user %s (other sessions revoked)", user_id)
        _disconnect_user_sockets(user_id)
        return jsonify({"message": "Password changed"})
    finally:
        db.close()








############################################################
# logout
############################################################
#
# POST /api/auth/logout
#
# Deletes the session behind the presented token only
# (looked up by hash) — other devices' sessions stay
# valid, and so do their push tokens: only an optional
# additive {"pushToken"} in the body deletes THAT device's
# push_tokens row (owner-scoped), so logging out one phone
# never silences the user's other logged-in devices. The
# user's live sockets are kicked best-effort — a device
# with a still-valid session reconnects on its own. Always
# 200 {"message": "Logged out"} once past the decorator; a
# token that is already gone gets the 401 there instead.
# The mobile app fires this best-effort and tears down
# locally regardless.
#
# Used by:
#   - services/api/auth.ts — logoutApi (AuthContext
#     logout())
############################################################

@auth_bp.route("/logout", methods=["POST"])
@require_auth
def logout():
    token = _bearer_token() or ""
    user_id = request.user["id"]
    data = get_json_object() or {}

    db = get_db()
    try:
        db.execute("DELETE FROM sessions WHERE token = ?", (_hash_token(token),))

        # Owner-scoped single-device push cleanup — never the whole
        # user (their other devices are still logged in)
        push_token = data.get("pushToken")
        if isinstance(push_token, str) and push_token:
            db.execute(
                "DELETE FROM push_tokens WHERE user_id = ? AND token = ?",
                (user_id, push_token),
            )

        db.commit()
        logger.info("Logout: user=%s", user_id)
        _disconnect_user_sockets(user_id)
        return jsonify({"message": "Logged out"})
    finally:
        db.close()








############################################################
# logout_all
############################################################
#
# POST /api/auth/logout-all
#
# The revoke-everywhere switch for a compromised
# credential: deletes EVERY session of the caller — the
# presented one included, so the client must tear down
# locally after the 200 — and every push_tokens row (no
# session may keep receiving previews; devices re-register
# on their next login). Live sockets are kicked
# best-effort.
#
# Used by:
#   - nothing in the mobile app yet — documented in
#     swagger for the settings screen to adopt
############################################################

@auth_bp.route("/logout-all", methods=["POST"])
@require_auth
def logout_all():
    user_id = request.user["id"]
    db = get_db()
    try:
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        # Push dies with the sessions — same rule as the expired-
        # session purge in resolve_session_token
        db.execute("DELETE FROM push_tokens WHERE user_id = ?", (user_id,))
        db.commit()
        logger.info("Logout-all: user=%s", user_id)
        _disconnect_user_sockets(user_id)
        return jsonify({"message": "Logged out everywhere"})
    finally:
        db.close()








############################################################
# erase_user_account
############################################################
#
# The one erasure routine both deletion routes share (the
# self-service DELETE /me below and the admin route in
# admin/routes.py). The users ROW SURVIVES, anonymised —
# hard-deleting it would cascade shared history away from
# the people who lived it (news_comments and poll_votes
# cascade on users, messages would orphan) — but everything
# that identifies or belongs to the person goes:
#
#   - their uploaded files are removed from disk, their own
#     posts'/messages' references to them nulled, the
#     uploads rows dropped
#   - the denormalised counters their likes and poll votes
#     fed are decremented BEFORE the rows are deleted
#   - sessions, push tokens, notification prefs, read
#     receipts, reactions, likes, votes, friendships,
#     friend requests, blocks and room memberships are
#     hard-deleted
#   - authored posts stay readable but tombstoned: the
#     author_name snapshot becomes the deleted-user marker
#     (comments and messages have no snapshot — they join
#     users at read time and pick the marker up from there)
#   - the users row keeps only its id: unique placeholder
#     username/email, the marker as display name, a fresh
#     random password hash (never '!': login's checkpw
#     would 500 on a non-bcrypt string), student fields and
#     avatar cleared, active = 0
#
# Runs on the caller's connection inside the caller's
# transaction — commit/rollback stays the route's decision.
#
# Used by:
#   - delete_me (below)
#   - admin/routes.py delete_user
############################################################

ERASED_USER_MARKER = "Ištrintas naudotojas"


def erase_user_account(db, user_id):
    # STEP 1: files first — remove the person's uploads from
    # disk, null their own references, drop the rows
    # ======================================================
    from app.uploads.routes import delete_upload

    uploads = db.execute(
        "SELECT filename FROM uploads WHERE user_id = ?", (user_id,)
    ).fetchall()
    for row in uploads:
        try:
            delete_upload(f"/api/uploads/{row['filename']}")
        except Exception:
            logger.exception("Erasure: upload %s not deleted", row["filename"])

    db.execute(
        "UPDATE news_posts SET image_url = NULL"
        " WHERE author_id = ? AND image_url LIKE '%/api/uploads/%'",
        (user_id,),
    )
    db.execute(
        "UPDATE messages SET image_url = NULL"
        " WHERE sender_id = ? AND image_url LIKE '%/api/uploads/%'",
        (user_id,),
    )
    db.execute("DELETE FROM uploads WHERE user_id = ?", (user_id,))


    # STEP 2: the denormalised counters their engagement fed,
    # decremented while the rows still exist to be counted
    # =======================================================
    db.execute(
        """UPDATE news_posts SET likes_count = MAX(0, likes_count - 1)
           WHERE id IN (SELECT post_id FROM news_likes WHERE user_id = ?)""",
        (user_id,),
    )
    db.execute(
        """UPDATE poll_options SET votes = MAX(0, votes - 1)
           WHERE id IN (SELECT option_id FROM poll_votes WHERE user_id = ?)""",
        (user_id,),
    )


    # STEP 3: everything that is theirs alone, hard-deleted
    # =====================================================
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM push_tokens WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM notification_channels WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM message_reads WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM message_reactions WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM news_likes WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM poll_votes WHERE user_id = ?", (user_id,))
    db.execute(
        "DELETE FROM friendships WHERE user_id = ? OR friend_id = ?",
        (user_id, user_id),
    )
    db.execute(
        "DELETE FROM friend_requests WHERE from_user_id = ? OR to_user_id = ?",
        (user_id, user_id),
    )
    db.execute(
        "DELETE FROM user_blocks WHERE blocker_id = ? OR blocked_id = ?",
        (user_id, user_id),
    )
    db.execute(
        "DELETE FROM conversation_participants WHERE user_id = ?", (user_id,)
    )


    # STEP 4: tombstone the authored snapshots, anonymise the
    # row. The placeholder username/email embed the full uuid,
    # so the UNIQUE constraints cannot collide
    # =======================================================
    db.execute(
        "UPDATE news_posts SET author_name = ? WHERE author_id = ?",
        (ERASED_USER_MARKER, user_id),
    )
    unreachable_hash = bcrypt.hashpw(uuid.uuid4().hex.encode(), bcrypt.gensalt()).decode()
    db.execute(
        """UPDATE users SET
               username = ?, email = ?, display_name = ?, password_hash = ?,
               avatar_url = NULL, student_number = NULL, study_group = NULL,
               study_program = NULL, active = 0, updated_at = ?
           WHERE id = ?""",
        (f"deleted-{user_id}", f"deleted-{user_id}@deleted.invalid",
         ERASED_USER_MARKER, unreachable_hash, utc_now_iso(), user_id),
    )







############################################################
# delete_me
############################################################
#
# DELETE /api/auth/me
#
# Self-service account erasure (GDPR Art. 17), password-
# confirmed: the body must carry the CURRENT password, and
# wrong guesses burn the same per-user budget change-password
# uses — a stolen session token alone must not be enough to
# destroy an account, and the confirm must not become a
# password oracle. The last active admin cannot erase
# themselves (the same continuity rule update_user enforces);
# they hand admin over first. On success every session is
# gone (erase_user_account deletes them), so the 200 is the
# account's last authenticated response.
#
# Used by:
#   - services/api/auth.ts — deleteAccountApi
#     (app/(main)/delete-account/index.tsx)
############################################################

@auth_bp.route("/me", methods=["DELETE"])
@require_auth
def delete_me():
    # STEP 1: the same failure budget change-password runs on
    # =======================================================
    user_id = request.user["id"]
    rl_key = f"chpass:{user_id}"
    if _check_rate_limit(rl_key, record=False):
        return _rate_limited_response("Too many attempts. Please wait a few minutes.", rl_key)


    # STEP 2: the password confirm
    # ============================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    password = data.get("password")
    if not password or not isinstance(password, str):
        return jsonify({"error": "Password required"}), 400

    db = get_db()
    try:
        row = db.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None or not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
            _record_attempt(rl_key)
            logger.warning("Account deletion rejected (wrong password) for user %s", user_id)
            return jsonify({"error": "Invalid credentials", "code": "invalid_credentials"}), 400


        # STEP 3: admin continuity — the last active admin
        # cannot erase themselves out of the system
        # ================================================
        if request.user["role"] == "admin":
            remaining = db.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
                (user_id,),
            ).fetchone()["c"]
            if remaining == 0:
                return jsonify({"error": "Cannot delete the last active admin"}), 400


        # STEP 4: erase, commit, disconnect the live sockets
        # ==================================================
        erase_user_account(db, user_id)
        db.commit()

        logger.info("Account erased (self-service): user=%s", user_id)
        _disconnect_user_sockets(user_id)
        return jsonify({"status": "deleted"})
    finally:
        db.close()







############################################################
# export_me
############################################################
#
# GET /api/auth/me/export
#
# The caller's data as one JSON document (GDPR Art. 15): the
# profile row, authored posts and comments, sent messages,
# likes, poll votes, friendships and requests, blocks,
# reports filed, notification preferences, upload inventory
# and conversation memberships. Raw from the tables (only
# the password hash stays out); the response passes through
# the app's HTML-escaping JSON provider like everything
# else. Deliberately tightly rate limited — the answer spans
# the person's whole history.
#
# Used by:
#   - services/api/auth.ts — exportMyDataApi (no screen yet;
#     the API serves written Art. 15 requests meanwhile)
############################################################

@auth_bp.route("/me/export", methods=["GET"])
@require_auth
@rate_limit("export", max_attempts=5)
def export_me():
    user_id = request.user["id"]
    db = get_db()
    try:
        def rows(sql, params=(user_id,)):
            return [dict(r) for r in db.execute(sql, params).fetchall()]

        profile = db.execute(
            """SELECT id, username, email, display_name, role, invited, avatar_url,
                      student_number, study_group, study_program, active,
                      chat_push_preview, created_at, updated_at
               FROM users WHERE id = ?""",
            (user_id,),
        ).fetchone()

        return jsonify({
            "exportedAt": utc_now_iso(),
            "profile": dict(profile) if profile else None,
            "posts": rows(
                """SELECT id, title, content, summary, image_url, source, post_type,
                          is_public, likes_count, comments_count, shares_count,
                          published_at, created_at, updated_at
                   FROM news_posts WHERE author_id = ? ORDER BY created_at"""
            ),
            "comments": rows(
                "SELECT id, post_id, text, created_at FROM news_comments"
                " WHERE user_id = ? ORDER BY created_at"
            ),
            "messages": rows(
                """SELECT id, conversation_id, text, image_url, reply_to_id,
                          deleted_at, created_at
                   FROM messages WHERE sender_id = ? ORDER BY created_at"""
            ),
            "conversations": rows(
                """SELECT c.id, c.type, c.title, c.created_at, cp.last_read_at
                   FROM conversation_participants cp
                   JOIN conversations c ON c.id = cp.conversation_id
                   WHERE cp.user_id = ? ORDER BY c.created_at"""
            ),
            "likes": rows(
                "SELECT post_id, created_at FROM news_likes WHERE user_id = ? ORDER BY created_at"
            ),
            "pollVotes": rows(
                "SELECT poll_id, option_id, created_at FROM poll_votes"
                " WHERE user_id = ? ORDER BY created_at"
            ),
            "friends": rows(
                "SELECT friend_id, created_at FROM friendships WHERE user_id = ? ORDER BY created_at"
            ),
            "friendRequests": rows(
                """SELECT id, from_user_id, to_user_id, status, created_at, updated_at
                   FROM friend_requests WHERE from_user_id = ? OR to_user_id = ?
                   ORDER BY created_at""",
                (user_id, user_id),
            ),
            "blocks": rows(
                "SELECT blocked_id, created_at FROM user_blocks WHERE blocker_id = ? ORDER BY created_at"
            ),
            "reports": rows(
                """SELECT id, target_type, target_id, reason, status, created_at
                   FROM reports WHERE reporter_id = ? ORDER BY created_at"""
            ),
            "notificationChannels": rows(
                "SELECT channel, enabled, updated_at FROM notification_channels WHERE user_id = ?"
            ),
            "uploads": rows(
                "SELECT filename, byte_size, created_at FROM uploads WHERE user_id = ? ORDER BY created_at"
            ),
        })
    finally:
        db.close()
