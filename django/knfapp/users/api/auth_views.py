############################################################
#  [*] Auth API — the account and session routes
#
#  Accounts and bearer sessions: invitation-code checks,
#  register, login, the caller's profile (GET/PUT/DELETE),
#  the GDPR export, password rotation and the two logout
#  shapes. Paths, bodies, status codes and the machine
#  `code` slugs are the frozen wire contract
#  (swagger/swagger.yaml) — clients translate off `code`
#  and never parse the prose.
#
#  Split into:
#
#    validate_code   — POST /api/auth/validate-code
#    register        — POST /api/auth/register
#    login           — POST /api/auth/login
#    me              — GET  /api/auth/me (PUT → update_me,
#                      DELETE → delete_me)
#    export_me       — GET  /api/auth/me/export
#    change_password — POST /api/auth/change-password
#    logout          — POST /api/auth/logout
#    logout_all      — POST /api/auth/logout-all
#    (+ _invite_rejection and the socket hook; the profile
#    update's rules live in users/profile.py, shared with
#    PUT /api/social/profile)
############################################################


import logging
import re
import uuid
from datetime import datetime, timezone


import bcrypt
from django.db import IntegrityError, models, transaction


from knfapp.common import ratelimit
from knfapp.common.http import client_ip, get_json_object, json_error, json_response, require_methods
from knfapp.common.timestamps import as_naive_utc, parse_stored, utc_now, utc_now_iso
from knfapp.notifications.models import PushToken
from knfapp.users.auth import (
    DUMMY_PASSWORD_HASH,
    bearer_token,
    hash_token,
    mint_session,
    require_auth,
    serialize_user,
    validate_new_password,
)
from knfapp.users.models import InvitationCode, Session, User


from knfapp.users.erasure import erase_user_account
from knfapp.users.profile import apply_profile_patch, commit_profile_patch


logger = logging.getLogger(__name__)

LOGIN_IP_MAX = 30  # failed logins per IP — a NATed campus shares one

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX = 254








############################################################
# _invite_rejection
############################################################
#
# The three invitation-code checks in their canonical
# order — unknown, exhausted, expired — shared by
# validate_code (200 + valid:false) and register (400).
# Returns (slug, prose, reason) or None for a usable code.
# Expiry is aware-to-aware; a malformed stamp counts as
# expired instead of raising a 500.
#
# Used by:
#   - validate_code, register (below)
############################################################

def _invite_rejection(invite):
    if invite is None:
        return ("invite_invalid", "Invalid invitation code", "unknown")
    if invite.use_count >= invite.max_uses:
        return ("invite_exhausted", "Invitation code has been fully used", "exhausted")
    expires = parse_stored(invite.expires_at)
    if expires is None or expires < datetime.now(timezone.utc):
        return ("invite_expired", "Invitation code has expired", "expired")
    return None








############################################################
# validate_code
############################################################
#
# POST /api/auth/validate-code — body {"code"}. Checks a
# code WITHOUT consuming it and answers 200 either way:
# {"valid": true, "role", "remainingUses"} or {"valid":
# false, "error", "code", "reason"} — the client branches
# on `valid`, keys its translations off `reason` and never
# shows the English prose. Only a missing or non-string
# code is a 400. Rate-limited per IP with a 30-attempt
# budget — the register screen re-validates on a typing
# debounce, so one honest entry is a dozen calls.
#
# Used by:
#   - services/api/auth.ts — validateInvitationCode
############################################################

@require_methods("POST")
def validate_code(request):
    ip = client_ip(request)
    key = f"validate:{ip}"
    if ratelimit.check(key, max_attempts=30):
        return ratelimit.limited_response("Too many attempts. Please wait a few minutes.", key)

    data = get_json_object(request)
    if not data or not data.get("code"):
        return json_error("Code required", 400)
    if not isinstance(data["code"], str):
        return json_error("Code must be a string", 400)

    invite = InvitationCode.objects.filter(code=data["code"]).first()
    rejection = _invite_rejection(invite)
    if rejection:
        slug, prose, reason = rejection
        return json_response({"valid": False, "error": prose, "code": slug, "reason": reason})

    return json_response({
        "valid": True,
        "role": invite.role,
        "remainingUses": invite.max_uses - invite.use_count,
    })








############################################################
# register
############################################################
#
# POST /api/auth/register — body {"username", "password",
# "display_name", "email", "invitation_code"?}. Creates the
# user AND a 30-day session in one transaction
# (ATOMIC_REQUESTS) and answers 201 {"user", "token"} — the
# client is signed in straight away. A given code must be
# valid (a bad one is a 400, never a silent downgrade); a
# valid one grants its role and invited=True, no code means
# student/invited=False.
#
# The body is validated BEFORE the per-IP slot is taken —
# malformed retries must not eat an honest user's budget —
# and taking it is ONE step (ratelimit.reserve judges and
# records in a single lock hold; a probe that a burst of
# threads all pass would reserve nothing). Every validated
# attempt keeps its slot, a 201 included: this budget caps
# account creation, not only guessing. Uniqueness is checked
# case-insensitively (iexact — 'Tomas' blocks a new 'tomas',
# mirroring login's lookup) BEFORE the invitation is spent: a
# 4xx return COMMITS under ATOMIC_REQUESTS, so a typo in the
# username must never cost a single-use curator code its one
# use. The burn itself is an ATOMIC conditional UPDATE on
# use_count (a racing twin cannot reuse the last slot); its
# expiry gate is the aware parse of _invite_rejection. The
# one 4xx left after the burn — the INSERT's IntegrityError
# for the race the pre-check cannot see — marks the
# transaction for rollback explicitly, so the burn goes with
# it on every engine (SQLite keeps a transaction usable after
# an IntegrityError and would otherwise commit the burn
# behind the 409).
#
# Error bodies carry the stable machine `code` next to the
# English prose: rate_limited, invalid_username,
# invalid_email, invite_invalid, invite_exhausted,
# invite_expired, username_taken, and the per-reason
# password codes from validate_new_password
# (password_too_short/_too_long/_contains_username/
# _contains_email/_too_common).
#
# Used by:
#   - services/api/auth.ts — registerApi
############################################################

@require_methods("POST")
def register(request):
    # STEP 1: shape-check the body — presence, then type, then the
    # caps (username charset, canonical lowercased email, password
    # policy, display_name stripped 1–100 and stored stripped).
    # No slot is taken yet: a malformed retry costs no budget
    # ============================================================
    ip = client_ip(request)
    rl_key = f"register:{ip}"
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    required = ["username", "password", "display_name", "email"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return json_error(f"Missing fields: {', '.join(missing)}", 400)
    for field in required:
        if not isinstance(data[field], str):
            return json_error(f"{field} must be a string", 400)

    username = data["username"]
    if not USERNAME_RE.fullmatch(username):
        return json_error("Username must be 3-32 characters: letters, digits, dots, dashes or underscores",
                          400, code="invalid_username")

    email = data["email"].strip().lower()
    if len(email) > EMAIL_MAX or not EMAIL_RE.fullmatch(email):
        return json_error("Invalid email address", 400, code="invalid_email")

    password_error = validate_new_password(data["password"], username, email)
    if password_error:
        slug, prose = password_error
        return json_error(prose, 400, code=slug)

    display_name = data["display_name"].strip()
    if not display_name:
        return json_error("Display name cannot be empty", 400)
    if len(display_name) > 100:
        return json_error("Display name must be at most 100 characters", 400)


    # STEP 2: the body validated — only NOW is the per-IP slot
    # taken, in one atomic step (reserve prunes, compares and
    # appends under one lock hold — a probe answered "free" to 24
    # threads at once would let all 24 through). The slot stays
    # spent whatever follows, success included: the budget caps
    # how many accounts one IP can create, not only bad guesses
    # ===========================================================
    if not ratelimit.reserve(rl_key):
        return ratelimit.limited_response("Too many registration attempts. Please wait a few minutes.", rl_key)


    # STEP 3: judge the invitation code — guest defaults, overridden
    # only by a code that passes the canonical checks. Judged, not
    # yet spent: nothing below may cost the code a use until the
    # registration is known to go through
    # ==============================================================
    raw_code = data.get("invitation_code")
    if raw_code is not None and not isinstance(raw_code, str):
        return json_error("invitation_code must be a string", 400)
    invite_code = (raw_code or "").strip()
    role = "student"
    invited = False
    invite = None

    if invite_code:
        invite = InvitationCode.objects.filter(code=invite_code).first()
        rejection = _invite_rejection(invite)
        if rejection:
            slug, prose, _ = rejection
            return json_error(prose, 400, code=slug)


    # STEP 4: one 409 for a taken username OR email, checked
    # case-insensitively to mirror login's lookup — and checked
    # BEFORE the burn: a 4xx return commits under ATOMIC_REQUESTS,
    # so a burn ahead of this line would spend a single-use code
    # over a typo
    # ============================================================
    if User.objects.filter(models.Q(username__iexact=username) | models.Q(email__iexact=email)).exists():
        return json_error("Username or email already exists", 409, code="username_taken")


    # STEP 5: burn the code ATOMICALLY (conditional UPDATE; rowcount
    # 0 = a racer took the last use or the row vanished, and the
    # re-read names which — nothing was spent on that path either)
    # ==============================================================
    if invite is not None:
        burned = InvitationCode.objects.filter(
            code=invite_code, use_count__lt=models.F("max_uses"),
        ).update(use_count=models.F("use_count") + 1)
        if burned == 0:
            current = InvitationCode.objects.filter(code=invite_code).first()
            slug, prose, _ = _invite_rejection(current) or ("invite_exhausted", "Invitation code has been fully used", "exhausted")
            return json_error(prose, 400, code=slug)

        role = invite.role
        invited = True


    # STEP 6: the user (bcrypt, fresh salt); an IntegrityError is
    # the pre-check's race answering the same 409 — with the
    # transaction marked for rollback FIRST, so the burn above is
    # discarded on every engine (PostgreSQL has already aborted
    # the transaction, SQLite would happily commit it)
    # =============================================================
    user_id = str(uuid.uuid4())
    password_hash = bcrypt.hashpw(data["password"].encode(), bcrypt.gensalt()).decode()
    now = utc_now()
    try:
        User.objects.create(
            id=user_id, username=username, email=email, display_name=display_name,
            password_hash=password_hash, role=role, invited=invited,
            created_at=now, updated_at=now,
        )
    except IntegrityError:
        transaction.set_rollback(True)
        return json_error("Username or email already exists", 409, code="username_taken")


    # STEP 7: the session in the same transaction; the answer goes
    # through serialize_user — one user shape everywhere
    # ============================================================
    token = mint_session(user_id)

    logger.info("Registered user %s (%s) role=%s invited=%s from %s", user_id, username, role, invited, ip)
    if invited:
        logger.info("Invitation code %r consumed by user %s", invite_code, user_id)

    return json_response({
        "user": serialize_user({
            "id": user_id, "username": username, "email": email,
            "display_name": display_name, "role": role, "invited": invited,
            "avatar_url": None,
        }),
        "token": token,
    }, status=201)








############################################################
# login
############################################################
#
# POST /api/auth/login — body {"username" | "email",
# "password"}: whichever key is sent becomes one identifier
# matched case-insensitively against BOTH columns; should
# the table ever hold case-variant duplicates (the CI
# unique indexes forbid new ones), the password is tried
# against every match — the row it verifies for wins.
# Answers
# {"user", "token"} with a fresh 30-day session; the
# newest SESSIONS_PER_USER rows survive.
#
# Two failure-only rate buckets, per IP (LOGIN_IP_MAX per
# 5 min) and per identifier (10 per 5 min): each slot is
# RESERVED before the bcrypt work — ratelimit.reserve judges
# and records in one lock hold, so a burst of threads cannot
# all pass one probe and spend nothing — and refunded once
# the password proves right. An X-Forwarded-For spoofer
# still cannot hammer one account, and successful sign-ins
# never lock a NATed campus out; the body is checked before
# either slot, so a malformed one reserves nothing. Unknown
# user and wrong password share the
# identical 401 at the same bcrypt cost (dummy hash when no
# candidate row); an unusable stored hash is logged and
# skipped as a non-match, never a 500. users.active is
# checked only AFTER the password matches, so the 403 is
# disclosed to the account holder alone.
#
# Used by:
#   - services/api/auth.ts — loginApi
############################################################

@require_methods("POST")
def login(request):
    # STEP 1: the body — "username" wins over "email"; both must be
    # strings or bcrypt/.encode() would blow up. Before any slot is
    # taken: a malformed body reserves nothing
    # =============================================================
    ip = client_ip(request)
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    identifier = data.get("username") or data.get("email")
    password = data.get("password")
    if not identifier or not password:
        return json_error("Username/email and password required", 400)
    if not isinstance(identifier, str) or not isinstance(password, str):
        return json_error("Username/email and password must be strings", 400)


    # STEP 2: reserve the per-IP slot, then the per-identifier one
    # a spoofer cannot dodge — both taken BEFORE the bcrypt work,
    # each in one lock hold, so the budgets bound the guesses made
    # rather than the guesses that looked; a refused identifier
    # hands the IP slot straight back (a 429 spends nothing)
    # ============================================================
    ip_key = f"login:{ip}"
    if not ratelimit.reserve(ip_key, max_attempts=LOGIN_IP_MAX):
        return ratelimit.limited_response("Too many login attempts. Please wait a few minutes.", ip_key)

    id_key = f"login:id:{identifier.strip().lower()}"
    if not ratelimit.reserve(id_key):
        ratelimit.refund(ip_key)
        return ratelimit.limited_response("Too many login attempts. Please wait a few minutes.", id_key)


    # STEP 3: find the account and verify the password; unknown user
    # and wrong password answer the identical 401 at identical cost
    # ==============================================================
    candidates = list(User.objects.filter(
        models.Q(username__iexact=identifier) | models.Q(email__iexact=identifier),
    ))

    user = None
    for row in candidates:
        # A hand-edited row (DbGate, a half-restored backup) makes
        # checkpw raise — such a row simply never matches, so the
        # caller gets the documented 401 instead of a public 500
        try:
            matched = bcrypt.checkpw(password.encode(), (row.password_hash or "").encode())
        except (TypeError, ValueError):
            logger.error("User %s has an unusable password_hash — treated as no match", row.id)
            continue
        if matched:
            user = row
            break

    if not candidates:
        # Same work as one real check — account existence must not be
        # readable off the response time
        bcrypt.checkpw(password.encode(), DUMMY_PASSWORD_HASH)

    if not user:
        # A failure — both reserved slots stay spent
        logger.warning("Failed login for %r from %s", identifier, ip)
        return json_error("Invalid credentials", 401, code="invalid_credentials")

    # The password matched — only failures spend budget, so both
    # slots go back; before the active check on purpose, a
    # deactivated holder who knows the password is not a guesser
    ratelimit.refund(ip_key)
    ratelimit.refund(id_key)

    # After the password on purpose — the flag is the holder's alone
    if not user.active:
        logger.warning("Login on deactivated account %s from %s", user.id, ip)
        return json_error("Account deactivated", 403, code="account_deactivated")


    # STEP 4: mint the session (sha256 at rest, raw token answered)
    # =============================================================
    token = mint_session(user.id)
    logger.info("Login: user=%s from %s", user.id, ip)

    return json_response({
        "user": serialize_user({
            "id": user.id, "username": user.username, "email": user.email,
            "display_name": user.display_name, "role": user.role,
            "avatar_url": user.avatar_url, "invited": user.invited,
            "student_number": user.student_number, "study_group": user.study_group,
            "study_program": user.study_program,
        }),
        "token": token,
    })








############################################################
# me
############################################################
#
# GET /api/auth/me — the caller's own profile straight from
# request.user; require_auth already loaded the row and
# turned an expired or deactivated session into the 401 the
# app treats as "session dead".
#
# Used by:
#   - services/api/auth.ts — fetchMe
############################################################

@require_auth
def me(request):
    # One path, three verbs — PUT is the partial update, DELETE
    # the password-confirmed erasure, both below
    if request.method == "PUT":
        return update_me(request)
    if request.method == "DELETE":
        return delete_me(request)
    if request.method != "GET":
        return json_error("Method not allowed", 405)
    return json_response(serialize_user(request.user))








############################################################
# logout
############################################################
#
# POST /api/auth/logout — the single-DEVICE sign-out: deletes
# the PRESENTED session row, the one owner-scoped push row
# the body names as pushToken (this device's — one request,
# one transaction, so the push row cannot outlive the
# session), and cuts the sockets of that session alone. The
# account's other devices keep their sessions, their push
# rows and their realtime; logout_all is the everywhere
# switch.
#
# Used by:
#   - services/api/auth.ts — logoutApi (the captured-bearer
#     detached call, carrying this device's push token)
############################################################

@require_methods("POST")
@require_auth
def logout(request):
    token = bearer_token(request) or ""
    user_id = request.user["id"]
    data = get_json_object(request) or {}

    session_hash = hash_token(token)
    Session.objects.filter(token=session_hash).delete()

    # Owner-scoped single-device push cleanup — never the whole user
    push_token = data.get("pushToken")
    if isinstance(push_token, str) and push_token:
        PushToken.objects.filter(user_id=user_id, token=push_token).delete()

    logger.info("Logout: user=%s", user_id)
    # Only THIS session's sockets: the tablet that stays signed
    # in keeps its realtime too
    _disconnect_user_sockets(user_id, only_session=session_hash)
    return json_response({"message": "Logged out"})








############################################################
# logout_all
############################################################
#
# POST /api/auth/logout-all — the revoke-everywhere switch
# for a compromised credential: EVERY session of the caller
# (the presented one included, so the client tears down
# locally after the 200) and every push row die together.
#
# Used by:
#   - documented in swagger for the settings screen to adopt
############################################################

@require_methods("POST")
@require_auth
def logout_all(request):
    user_id = request.user["id"]
    Session.objects.filter(user_id=user_id).delete()
    # Push dies with the sessions — same rule as the expired purge
    PushToken.objects.filter(user_id=user_id).delete()
    logger.info("Logout-all: user=%s", user_id)
    _disconnect_user_sockets(user_id)
    return json_response({"message": "Logged out everywhere"})








############################################################
# _disconnect_user_sockets
############################################################
#
# The cross-app hook the auth routes fire best-effort: a
# credential change or an erasure kicks the user's live
# sockets. The scope keywords pass straight through to
# chat/events.py disconnect_user_sockets — only_session for
# the single-device logout, except_session for the password
# change that keeps the caller signed in, neither for the
# account-wide paths. The chat app owns the socket layer,
# so the import is guarded and a socket-layer failure never
# fails the auth route that triggered it. (The rename hook
# that used to sit beside it moved to users/profile.py with
# the rest of the profile update.)
#
# Used by:
#   - delete_me, change_password, logout, logout_all (below)
############################################################

def _disconnect_user_sockets(user_id, *, only_session=None, except_session=None):
    try:
        from knfapp.chat.events import disconnect_user_sockets
    except ImportError:
        return
    try:
        disconnect_user_sockets(user_id, only_session=only_session, except_session=except_session)
    except Exception:
        logger.warning("Could not disconnect sockets for user %s", user_id)








############################################################
# update_me
############################################################
#
# PUT /api/auth/me — partial profile update: display name,
# avatar and the three student-card fields. The rules, the
# UPDATE, the rename propagation and the replaced avatar's
# cleanup on the commit live in users/profile.py, shared
# with PUT /api/social/profile; the answer is the re-read
# row through serialize_user — the same key set from
# either route. Thirty updates per user per 5 minutes
# (429, the "profile" bucket both routes share) — the
# limit sits on THIS function, not on `me`: GET
# /api/auth/me is polled by the app. request.user is
# already set by `me`'s require_auth.
#
# Used by:
#   - services/api/auth.ts — updateMe (profile screen, the
#     student-card editor)
############################################################

@ratelimit.per_user("profile", max_attempts=30)
def update_me(request):
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    patch, error = apply_profile_patch(request, data)
    if error:
        return error
    row, error = commit_profile_patch(request, patch)
    if error:
        return error
    return json_response(serialize_user(row))








############################################################
# change_password
############################################################
#
# POST /api/auth/change-password — body {"old_password",
# "new_password"} (camelCase aliases accepted, like
# PUT /me). Verifies the old password against a FRESH read
# of password_hash (request.user never carries it), runs
# the new one through the register policy, rewrites the
# hash and drops every OTHER session in one commit — a
# compromised credential dies everywhere except the device
# doing the rotation, whose socket is likewise the one left
# standing. A wrong old password is a 400 (code
# invalid_credentials), NOT a 401: the client treats any
# authenticated 401 as "session dead" and would tear the
# login down over a typo. Per-user limited, failures only —
# verifying old passwords is a password oracle — with the
# slot RESERVED before the bcrypt check (one lock hold, no
# probe a burst can all pass) and refunded on a match.
#
# Used by:
#   - documented in swagger for the settings screen to adopt
############################################################

@require_methods("POST")
@require_auth
def change_password(request):
    # STEP 1: body, both key spellings, then the shared policy —
    # before any slot is taken, so a malformed body or a rejected
    # new password reserves nothing
    # ===========================================================
    user_id = request.user["id"]
    rl_key = f"chpass:{user_id}"
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    old_password = data.get("oldPassword") if "oldPassword" in data else data.get("old_password")
    new_password = data.get("newPassword") if "newPassword" in data else data.get("new_password")
    if not old_password or not new_password:
        return json_error("Old and new password required", 400)
    if not isinstance(old_password, str) or not isinstance(new_password, str):
        return json_error("Old and new password must be strings", 400)

    password_error = validate_new_password(new_password, request.user.get("username"), request.user.get("email"))
    if password_error:
        slug, prose = password_error
        return json_error(prose, 400, code=slug)


    # STEP 2: take the failure-budget slot BEFORE the bcrypt oracle
    # (reserve judges and records in one lock hold), then verify
    # against a fresh hash read — a wrong old password keeps the
    # slot, a right one hands it back
    # =============================================================
    if not ratelimit.reserve(rl_key):
        return ratelimit.limited_response("Too many attempts. Please wait a few minutes.", rl_key)

    row = User.objects.filter(id=user_id).values("password_hash").first()
    try:
        matched = row is not None and bcrypt.checkpw(old_password.encode(), row["password_hash"].encode())
    except (TypeError, ValueError):
        matched = False
    if not matched:
        logger.warning("Password change rejected (wrong old password) for user %s", user_id)
        return json_error("Invalid credentials", 400, code="invalid_credentials")
    ratelimit.refund(rl_key)


    # STEP 3: rewrite, and drop every other session in the same
    # commit
    # =========================================================
    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    User.objects.filter(id=user_id).update(password_hash=new_hash, updated_at=utc_now())

    # Every OTHER session dies — the presented one (by hash) is
    # the survivor
    current_hash = hash_token(bearer_token(request) or "")
    Session.objects.filter(user_id=user_id).exclude(token=current_hash).delete()

    logger.info("Password changed for user %s (other sessions revoked)", user_id)
    # The surviving session keeps its socket — every other one goes
    _disconnect_user_sockets(user_id, except_session=current_hash)
    return json_response({"message": "Password changed"})








############################################################
# delete_me / export_me
############################################################
#
# The self-service GDPR pair. Erasure is password-confirmed
# — a stolen session token alone must not destroy an
# account — with wrong guesses burning change-password's
# budget (never a password oracle; the slot is reserved
# before the bcrypt check and refunded on a match), and the
# last active
# admin cannot erase themselves out of the system. On
# success every session is gone, so the 200 is the
# account's last authenticated response. Export answers the
# caller's whole history as one JSON document (the chat
# sections answer [] on a deployment stripped of the chat
# tables); tightly rate limited — the body spans everything.
#
# Used by:
#   - services/api/auth.ts — deleteAccountApi /
#     exportMyDataApi
############################################################

def delete_me(request):
    # STEP 1: the password confirm's body — before any slot is
    # taken, so a malformed body reserves nothing
    # ========================================================
    user_id = request.user["id"]
    rl_key = f"chpass:{user_id}"
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)
    password = data.get("password")
    if not password or not isinstance(password, str):
        return json_error("Password required", 400)


    # STEP 2: the same failure budget change-password runs on,
    # taken BEFORE the bcrypt oracle (one lock hold — no probe a
    # burst can all pass) and handed back once the password proves
    # right; a wrong guess keeps its slot
    # ============================================================
    if not ratelimit.reserve(rl_key):
        return ratelimit.limited_response("Too many attempts. Please wait a few minutes.", rl_key)

    row = User.objects.filter(id=user_id).values("password_hash").first()
    try:
        matched = row is not None and bcrypt.checkpw(password.encode(), row["password_hash"].encode())
    except (TypeError, ValueError):
        matched = False
    if not matched:
        logger.warning("Account deletion rejected (wrong password) for user %s", user_id)
        return json_error("Invalid credentials", 400, code="invalid_credentials")
    ratelimit.refund(rl_key)


    # STEP 3: admin continuity — the last active admin hands
    # admin over first
    # ======================================================
    if request.user["role"] == "admin":
        remaining = User.objects.filter(role="admin", active=1).exclude(id=user_id).count()
        if remaining == 0:
            return json_error("Cannot delete the last active admin", 400)


    # STEP 4: erase — one transaction — then kick live sockets
    # ========================================================
    erase_user_account(user_id)
    logger.info("Account erased (self-service): user=%s", user_id)
    _disconnect_user_sockets(user_id)
    return json_response({"status": "deleted"})


@require_auth
@ratelimit.per_user("export", max_attempts=5)
def export_me(request):
    user_id = request.user["id"]

    def orm_rows(queryset):
        return list(queryset)

    def guarded_rows(queryset):
        # Resilience for the chat sections — a missing table
        # (a stripped-down deployment) answers an empty
        # section, never a 500
        try:
            return list(queryset)
        except Exception:
            return []

    from knfapp.assistant.models import AssistantMessage, AssistantThread
    from knfapp.chat.models import ConversationParticipant, Message, MessageReaction
    from knfapp.memes.models import Meme
    from knfapp.news.models import NewsComment, NewsLike, NewsPost, PollVote
    from knfapp.notifications.core import token_digest
    from knfapp.notifications.models import NotificationChannel
    from knfapp.social.models import Activity, FriendRequest, Friendship, Report, UserBlock
    from knfapp.uploads.models import Upload

    profile = User.objects.filter(id=user_id).values(
        "id", "username", "email", "display_name", "role", "invited", "avatar_url",
        "student_number", "study_group", "study_program", "active",
        "chat_push_preview", "created_at", "updated_at",
    ).first()

    return json_response({
        "exportedAt": utc_now_iso(),
        "profile": profile,
        "posts": orm_rows(NewsPost.objects.filter(author_id=user_id).order_by("created_at").values(
            "id", "title", "content", "summary", "image_url", "source", "post_type",
            "is_public", "likes_count", "comments_count", "shares_count",
            "published_at", "created_at", "updated_at")),
        "comments": orm_rows(NewsComment.objects.filter(user_id=user_id).order_by("created_at")
                             .values("id", "post_id", "text", "created_at")),
        # The chat sections are the export's ONE naive-wire
        # island: the response mixes both stamp shapes, so the
        # per-response encoder flag cannot serve it — these
        # as_naive_utc wraps are the deliberate per-field
        # exception to the aware-everywhere rule
        "messages": [
            {**row, "deleted_at": as_naive_utc(row["deleted_at"]) if row["deleted_at"] else None,
             "created_at": as_naive_utc(row["created_at"])}
            for row in guarded_rows(
                Message.objects.filter(sender_id=user_id).order_by("created_at").values(
                    "id", "conversation_id", "text", "image_url", "reply_to_id",
                    "deleted_at", "created_at"))
        ],
        "conversations": [
            {**row, "created_at": as_naive_utc(row["created_at"]),
             "last_read_at": as_naive_utc(row["last_read_at"]) if row["last_read_at"] else None}
            for row in guarded_rows(
                ConversationParticipant.objects.filter(user_id=user_id)
                .order_by("conversation__created_at")
                .values("last_read_at",
                        id=models.F("conversation__id"),
                        type=models.F("conversation__type"),
                        title=models.F("conversation__title"),
                        created_at=models.F("conversation__created_at")))
        ],
        "likes": orm_rows(NewsLike.objects.filter(user_id=user_id).order_by("created_at")
                          .values("post_id", "created_at")),
        "pollVotes": orm_rows(PollVote.objects.filter(user_id=user_id).order_by("created_at")
                              .values("poll_id", "option_id", "created_at")),
        "friends": orm_rows(Friendship.objects.filter(user_id=user_id).order_by("created_at")
                            .values("friend_id", "created_at")),
        "friendRequests": orm_rows(
            FriendRequest.objects.filter(models.Q(from_user_id=user_id) | models.Q(to_user_id=user_id))
            .order_by("created_at")
            .values("id", "from_user_id", "to_user_id", "status", "created_at", "updated_at")),
        "blocks": orm_rows(UserBlock.objects.filter(blocker_id=user_id).order_by("created_at")
                           .values("blocked_id", "created_at")),
        "reports": orm_rows(Report.objects.filter(reporter_id=user_id).order_by("created_at")
                            .values("id", "target_type", "target_id", "reason", "status", "created_at")),
        "notificationChannels": orm_rows(NotificationChannel.objects.filter(user_id=user_id)
                                         .values("channel", "enabled", "updated_at")),
        "uploads": orm_rows(Upload.objects.filter(user_id=user_id).order_by("created_at")
                            .values("filename", "byte_size", "created_at")),
        # The device registry — the raw token is a live push
        # credential and an export file gets shared, so only
        # its 8-hex digest (the same handle the logs use) rides
        "pushTokens": [
            {"tokenDigest": token_digest(row.pop("token")), **row}
            for row in PushToken.objects.filter(user_id=user_id).order_by("created_at")
            .values("token", "platform", "language", "active", "created_at")
        ],
        # The stored column is already only a hash — the stamps
        # are the personal data here
        "sessions": orm_rows(Session.objects.filter(user_id=user_id).order_by("created_at")
                             .values("created_at", "expires_at")),
        "activity": orm_rows(Activity.objects.filter(user_id=user_id).order_by("created_at")
                             .values("kind", "actor_id", "subject_id", "subject_preview",
                                     "created_at", "read")),
        "reactions": [
            {**row, "created_at": as_naive_utc(row["created_at"])}
            for row in guarded_rows(
                MessageReaction.objects.filter(user_id=user_id).order_by("created_at")
                .values("message_id", "emoji", "created_at"))
        ],
        "memes": orm_rows(Meme.objects.filter(added_by_id=user_id).order_by("created_at")
                          .values("id", "title", "tags", "created_at")),
        # The AI assistant's stored conversations — GDPR export
        # covers the transcripts too (guest threads carry no
        # user and are not attributable, so they cannot ride).
        # content is the verbatim stored UIMessage: the person's
        # words, the answers, and the tool traffic between them.
        "assistantThreads": [
            {**row, "id": str(row["id"])}
            for row in AssistantThread.objects.filter(user_id=user_id).order_by("created_at")
            .values("id", "title", "preview", "language",
                    "created_at", "last_message_at", "deleted_at")
        ],
        "assistantMessages": [
            {**row, "thread_id": str(row["thread_id"])}
            for row in AssistantMessage.objects.filter(thread__user_id=user_id)
            .order_by("created_at")
            .values("thread_id", "id", "content", "rating", "created_at")
        ],
    })
