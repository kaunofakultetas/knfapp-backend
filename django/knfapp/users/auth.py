############################################################
#  [*] Bearer-session authentication — the one token scheme
#
#  Opaque bearer sessions, NOT JWT and NOT cookies:
#  register/login mint a uuid4 token, store its sha256 in
#  the sessions table with a 30-day expiry and hand the
#  client the raw value — a DB or backup leak never yields
#  a usable bearer. Every protected route resolves the
#  presented token through resolve_session_token, which
#  re-loads the user on every request so a deactivated
#  account is locked out immediately, expired rows are
#  purged lazily (push tokens die with them), and the
#  password hash never leaves this module's queries.
#
#  The decorators attach the resolved user to request.user,
#  so a handler body reads its caller off the request and
#  never re-resolves the token.
#
#    hash_token / bearer_token       — primitives
#    resolve_session_token(token)    — token → user dict | None
#    get_current_user(request)       — header → user, cached
#                                      per request
#    require_auth / require_role     — the route gates
#    mint_session(user_id)           — 30-day row + LRU trim
#    serialize_user(u)               — the public camelCase
#                                      shape (the whitelist)
#    validate_new_password(...)      — the one password policy
############################################################


import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps


import bcrypt
from django.db import OperationalError


from knfapp.common.http import json_error
from knfapp.common.timestamps import parse_stored, utc_now_iso
from knfapp.notifications.models import PushToken
from knfapp.users.models import Session, User


logger = logging.getLogger(__name__)

SESSION_DAYS = 30
SESSIONS_PER_USER = 10  # newest rows kept per user at login

PASSWORD_MAX_BYTES = 72  # bcrypt silently truncates past 72 bytes

# Burned once at import so an unknown identifier costs the same
# bcrypt work as a wrong password — login timing must not reveal
# whether an account exists
DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"knfapp-timing-equalizer", bcrypt.gensalt())

COMMON_PASSWORDS = frozenset({
    "123456", "1234567", "12345678", "123456789", "1234567890",
    "password", "password1", "password123", "passw0rd", "qwerty",
    "qwerty123", "abc123", "abcdef", "111111", "121212", "123123",
    "letmein", "welcome", "monkey", "dragon", "iloveyou", "sunshine",
    "princess", "football", "admin123", "slaptazodis", "labas123",
})








############################################################
# hash_token / bearer_token
############################################################
#
# sha256 hex is what sessions.token stores; the raw uuid4
# lives only on the client. bearer_token parses
# "Authorization: Bearer <token>" once, scheme matched
# case-insensitively (RFC 7235), None for any other shape.
#
# Used by:
#   - resolve_session_token / get_current_user (below)
#   - api/auth_views.py — register, login, logout
############################################################

def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def bearer_token(request):
    scheme, _, token = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None








############################################################
# resolve_session_token
############################################################
#
# The one token → user lookup for BOTH transports — REST
# here, and the socket handshake once the chat app lands —
# so the two paths can never drift apart. Takes the RAW
# token, looks up its sha256, and returns the user narrowed
# to the public columns as a plain dict, or None for an
# unknown/expired token or a deactivated account.
#
# Expiry is compared aware-to-aware (parse_stored assumes
# UTC for naive legacy stamps; malformed counts as expired
# — a 401, never a 500). An expired row is purged on the
# spot together with the user's push_tokens rows (a device
# that can no longer authenticate must not keep getting
# message previews); the purge is best-effort — a locked
# database yields a clean None, not a 500.
#
# users.active is the backstop for flags flipped outside
# the admin route (DbGate, direct SQL) and for a login that
# raced a deactivation.
#
# Used by:
#   - get_current_user (below)
#   - the chat app's socket handshake, when it lands
############################################################

_PUBLIC_FIELDS = (
    "id", "username", "email", "display_name", "role", "avatar_url",
    "invited", "active", "student_number", "study_group", "study_program",
)


def resolve_session_token(token):
    # STEP 1: the sessions row, by token hash
    # =======================================
    row = Session.objects.filter(token=hash_token(token)).values("user_id", "expires_at").first()
    if not row:
        return None


    # STEP 2: aware-to-aware expiry; malformed = expired. The lazy
    # purge (session row + push tokens) is best-effort
    # ============================================================
    expires = parse_stored(row["expires_at"])
    if expires is None or expires < datetime.now(timezone.utc):
        try:
            Session.objects.filter(token=hash_token(token)).delete()
            # Push dies with the session — a device that can no
            # longer authenticate must not keep getting previews
            PushToken.objects.filter(user_id=row["user_id"]).delete()
        except OperationalError:
            logger.warning("Expired-session purge skipped (database locked)")
        return None


    # STEP 3: the user, narrowed to the public columns, then the
    # active backstop
    # ==========================================================
    user = User.objects.filter(id=row["user_id"]).values(*_PUBLIC_FIELDS).first()
    if not user or not user["active"]:
        return None
    return dict(user)








############################################################
# get_current_user
############################################################
#
# Resolves the bearer header through resolve_session_token
# and caches the result on the request object, so a request
# that resolves its caller more than once pays one lookup.
# Negative results are cached too, keyed by the token, so a
# different header value would still resolve fresh.
#
# Used by:
#   - require_auth / require_role (below)
#   - news/social feed views (optional auth), when they land
############################################################

def get_current_user(request):
    token = bearer_token(request)
    if not token:
        return None

    cached = getattr(request, "_auth_cache", None)
    if cached is not None and cached[0] == token:
        return cached[1]

    user = resolve_session_token(token)
    request._auth_cache = (token, user)
    return user








############################################################
# require_auth / require_role
############################################################
#
# View decorators: 401 {"error": "Authentication required"}
# unless a live, active session resolves; require_role adds
# 403 {"error": "Insufficient permissions"} for the wrong
# role. On success the user dict lands on request.user for
# the handler — the one contract every api view leans on.
#
# Used by:
#   - api/auth_views.py — me, logout, logout_all
#   - every protected route of the apps still to come
############################################################

def require_auth(view):
    @wraps(view)
    def decorated(request, *args, **kwargs):
        user = get_current_user(request)
        if not user:
            return json_error("Authentication required", 401)
        request.user = user
        return view(request, *args, **kwargs)
    return decorated


def require_role(*roles):
    def decorator(view):
        @wraps(view)
        def decorated(request, *args, **kwargs):
            user = get_current_user(request)
            if not user:
                return json_error("Authentication required", 401)
            if user["role"] not in roles:
                return json_error("Insufficient permissions", 403)
            request.user = user
            return view(request, *args, **kwargs)
        return decorated
    return decorator








############################################################
# mint_session
############################################################
#
# A fresh 30-day session for user_id: the DB stores the
# sha256, the caller hands the client the returned RAW
# token. The user's rows are trimmed to the newest
# SESSIONS_PER_USER — the cap that bounds the table however
# many devices sign in. Runs inside the request's
# transaction (ATOMIC_REQUESTS), so register's user INSERT
# and its session commit together or not at all.
#
# Used by:
#   - api/auth_views.py — register, login
############################################################

def mint_session(user_id):
    token = str(uuid.uuid4())
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    Session.objects.create(
        id=str(uuid.uuid4()),
        user_id=user_id,
        token=hash_token(token),
        created_at=utc_now_iso(),
        expires_at=expires_at,
    )

    keep = list(
        Session.objects.filter(user_id=user_id)
        .order_by("-expires_at", "-created_at", "-id")
        .values_list("id", flat=True)[:SESSIONS_PER_USER]
    )
    Session.objects.filter(user_id=user_id).exclude(id__in=keep).delete()
    return token








############################################################
# serialize_user
############################################################
#
# The public user shape (camelCase) shared by register,
# login and /me. It doubles as the whitelist —
# password_hash, active and the timestamps never leave.
# Tolerates a partial dict (register's hand-built one):
# absent columns fall back to None, `invited` to 1.
#
# Used by:
#   - api/auth_views.py — register, login, me
############################################################

def serialize_user(u):
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
# validate_new_password
############################################################
#
# The one password policy, shared by register and (later)
# change-password: 6 chars minimum, 72 BYTES maximum
# (bcrypt truncates past 72, so a longer password would
# equal its prefix), must not contain the username or the
# email's local part (case-insensitive; local parts under 3
# chars are too noisy to check), and must not be one of the
# embedded common passwords. Returns the English error
# prose or None; callers answer 400 with code weak_password
# — the slug the app translates.
#
# Used by:
#   - api/auth_views.py — register
############################################################

def validate_new_password(password, username, email):
    if len(password) < 6:
        return "Password must be at least 6 characters"
    if len(password.encode("utf-8")) > PASSWORD_MAX_BYTES:
        return "Password must be at most 72 characters"

    lowered = password.lower()
    if username and username.lower() in lowered:
        return "Password must not contain your username"
    local_part = (email or "").split("@", 1)[0].lower()
    if len(local_part) >= 3 and local_part in lowered:
        return "Password must not contain your email"

    if lowered in COMMON_PASSWORDS:
        return "Password is too common"
    return None
