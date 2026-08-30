############################################################
#  [*] Admin — invitation codes, user management, stats
#
#  The admin console behind the mobile app's admin screens:
#  minting and revoking invitation codes (optional at
#  registration — a code grants its role and invited=1, no
#  code means a guest 'student'), the user list with role /
#  active editing, the dashboard counters and a broadcast
#  push. Every route sits behind require_role — a curator
#  may only mint, list and revoke the student/teacher codes
#  they created themselves, everything else is admin-only.
#  Mounted at /api/admin by app/__init__.py.
#
#  The rows touched here are the same ones app/auth/routes.py
#  consumes: register() burns use_count with an atomic
#  conditional UPDATE and checks expires_at, login() answers
#  403 and get_current_user() returns None for users with
#  active = 0 (the column added by migration v8 in
#  app/database/__init__.py).
#
#  Every mutating handler writes one admin_audit row
#  (migration v40) INSIDE its own transaction, so the trail
#  cannot record an action that rolled back nor miss one
#  that committed. Responses leave with Cache-Control:
#  no-store — stamped for the whole /api surface by
#  add_security_headers in app/__init__.py, which matters
#  most here: these bodies carry live invitation code
#  strings and every user's e-mail address.
#
#    POST   /api/admin/invitations            — mint a code
#    GET    /api/admin/invitations            — list codes
#    DELETE /api/admin/invitations/<code_id>  — revoke a code
#    GET    /api/admin/users                  — list users
#    PATCH  /api/admin/users/<user_id>        — role / active
#    DELETE /api/admin/users/<user_id>        — erase an account (GDPR)
#    GET    /api/admin/stats                  — dashboard counters
#    POST   /api/admin/notifications          — broadcast push
#    GET    /api/admin/notifications/<job_id> — broadcast job
#    GET    /api/admin/reports                — the complaint queue
#    PUT    /api/admin/reports/<report_id>    — resolve / reopen
############################################################


import json
import logging
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request

from app.auth.routes import (
    PRIVILEGED_ROLES,
    ROLES,
    erase_user_account,
    get_json_object,
    rate_limit,
    require_role,
)
from app.database import get_db, utc_now_iso


# Mounted at /api/admin (app/__init__.py); every route below is relative
admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger(__name__)

# Invitation bounds. max_uses is REJECTED outside them, never
# clamped — a silently rewritten 10000 used to answer 201 with
# a maxUses nobody asked for. expires_hours stops at a year:
# timedelta raises OverflowError somewhere past 10**16 hours
# (a 500), 0 or a negative value minted an already-expired
# code with a cheerful 201, and a decade-long code is a typo,
# never an intent. The mobile form only ever sends 1..100 uses
# and 1..168 hours, so the frozen contract never meets a 400.
_MAX_USES_MIN = 1
_MAX_USES_MAX = 1000
_EXPIRES_HOURS_MIN = 1
_EXPIRES_HOURS_MAX = 8760  # 365 days

# Admin/curator codes are credentials, not flyers: single-use
# and short-lived, whoever mints them. A photographed QR or a
# code read off the list stops being a standing backdoor
_PRIVILEGED_EXPIRES_MAX = 72  # 3 days
_INVITE_EXPIRY_FALLBACK = 168  # 7 days — mirrors the config default in app/__init__.py

# Bounds for the optional ?limit= on both listings; ?offset=
# rides along with it. Absent params keep the old
# return-everything behaviour, which is what the app relies on
_PAGE_LIMIT_MIN = 1
_PAGE_LIMIT_MAX = 500

# ?offset= needs no ceiling of its own — a page past the last
# row is an empty list — but SQLite has one: an integer past
# the signed 64-bit range raises OverflowError out of
# db.execute, and that was the single page param a caller
# could 500 the listing with while every other bad one is a
# clean 400
_PAGE_OFFSET_MAX = 2 ** 63 - 1

# The roles a curator may mint AND revoke: ROLES minus the two
# only a full admin hands out. Both tuples live in
# app/auth/routes.py — the users / invitation_codes CHECK
# constraints in app/database/__init__.py mirror them, and so
# do the mobile role gates. The placeholder run is built from
# the tuple's LENGTH, never from its values, so the f-string
# SQL below stays fully parameterised.
_CURATOR_ROLES = tuple(r for r in ROLES if r not in PRIVILEGED_ROLES)
_CURATOR_ROLE_SLOTS = ", ".join("?" for _ in _CURATOR_ROLES)

# The dashboard is five COUNT(*) table scans and the admin
# screen reloads it on every focus. One process-wide snapshot,
# rebuilt at most every 45 s: a tile that lags by under a
# minute is indistinguishable from a live one, and nothing
# else reads these numbers. Cleared only by a restart.
_stats_cache: dict = {}
_stats_cache_lock = threading.Lock()
_STATS_CACHE_TTL = 45  # seconds

# Broadcast bookkeeping. POST /notifications answers 202 with
# a job id and does the Expo round-trips on a background task
# — a 4000-device fan-out is 40 sequential HTTP calls and used
# to hold the request (and a worker) open for minutes. The
# registry is in-process like auth's rate limiter: a restart
# forgets every job and only the newest _BROADCAST_JOBS_MAX
# survive, which is why GET /notifications/<job_id> may 404 a
# job that really ran.
_broadcast_jobs: "OrderedDict[str, dict]" = OrderedDict()
_broadcast_jobs_lock = threading.Lock()
_BROADCAST_JOBS_MAX = 50

# Serialised ceiling for the caller's `data` payload. Expo
# refuses a message over 4 KiB outright, so an oversized
# payload has to die here instead of fanning out to fail once
# per device
_BROADCAST_DATA_MAX = 3072  # bytes of UTF-8 JSON








############################################################
# _pagination_clause
############################################################
#
# Turns an optional ?limit= / ?offset= pair into the SQL
# suffix and its bound parameters: ("", [], None) when
# NEITHER param is present, so both listings keep returning
# everything and the frozen mobile contract never notices
# this exists. A bad value comes back as the third element,
# the 400 message — the caller answers it. offset without
# limit rides on "LIMIT -1", SQLite's unlimited row count,
# because OFFSET is only legal after a LIMIT.
#
# offset is bounded ABOVE as well as below: a value past
# _PAGE_OFFSET_MAX cannot be bound at all (OverflowError out
# of db.execute), so it is refused here rather than crashing
# the listing it pages.
#
# Used by:
#   - list_invitations, list_users (below)
############################################################

def _pagination_clause():
    raw_limit = request.args.get("limit")
    raw_offset = request.args.get("offset")

    if raw_limit is None and raw_offset is None:
        return "", [], None

    # -1 is SQLite's "no ceiling" — the value used when only
    # an offset was supplied
    limit = -1
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return "", [], "limit must be an integer"
        if not _PAGE_LIMIT_MIN <= limit <= _PAGE_LIMIT_MAX:
            return "", [], f"limit must be between {_PAGE_LIMIT_MIN} and {_PAGE_LIMIT_MAX}"

    offset = 0
    if raw_offset is not None:
        try:
            offset = int(raw_offset)
        except (TypeError, ValueError):
            return "", [], "offset must be an integer"
        if offset < 0:
            return "", [], "offset must be zero or greater"
        # Past this, sqlite3 cannot bind the value at all
        if offset > _PAGE_OFFSET_MAX:
            return "", [], f"offset must be at most {_PAGE_OFFSET_MAX}"

    return " LIMIT ? OFFSET ?", [limit, offset], None








############################################################
# _invitation_expired
############################################################
#
# The `expired` flag list_invitations returns for one row.
# Parsing is the whole point: codes minted here are aware
# isoformat, but a row hand-edited through DbGate can hold
# anything, and ONE unparsable expires_at used to raise
# inside the list comprehension and 500 the entire listing.
# An unparsable (or NULL) value therefore reads as expired —
# the conservative answer, and the same verdict register()
# reaches for that row. Naive values are read as UTC and the
# comparison is aware-to-aware, byte for byte what
# app/auth/routes.py does, so the list and the registration
# path can never disagree about a code.
#
# Used by:
#   - list_invitations (below) — once per row
############################################################

def _invitation_expired(expires_at):
    try:
        parsed = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed < datetime.now(timezone.utc)








############################################################
# _invitation_fully_used
############################################################
#
# The `fullyUsed` flag list_invitations returns for one row.
# Both counters are INTEGER columns, but SQLite keeps
# whatever a hand edit through DbGate puts in them, and a
# TEXT value made the bare `use_count >= max_uses` raise
# TypeError inside the list comprehension — the exact
# failure mode _invitation_expired exists to prevent one
# column over. A counter that is not a number therefore
# reads as fully used: the conservative answer on a screen
# whose only action on a live code is to hand it out.
# Numeric strings still compare as the numbers they are.
#
# Used by:
#   - list_invitations (below) — once per row
############################################################

def _invitation_fully_used(use_count, max_uses):
    try:
        return int(use_count) >= int(max_uses)
    except (TypeError, ValueError):
        return True








############################################################
# _write_audit
############################################################
#
# Appends one admin_audit row (migration v40) on the
# caller's OPEN connection, so the trail is committed by the
# handler's own db.commit() — a mutation and its audit entry
# land together or not at all. actor_id comes from
# request.user, which require_role guarantees. payload is
# whatever context the action needs, stored as JSON text.
#
# Never raises: an audit write must not be able to fail an
# admin action. A pre-v40 database file (missing table) or
# any other sqlite error is logged and swallowed — the
# statement failing leaves the surrounding transaction open
# and untouched, so the real write still commits.
#
# Used by:
#   - create_invitation, delete_invitation, update_user,
#     send_admin_notification (below) — every mutating
#     handler in this module
############################################################

def _write_audit(db, action, target=None, payload=None):
    try:
        db.execute(
            """INSERT INTO admin_audit (id, actor_id, action, target, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                request.user["id"],
                action,
                target,
                json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                utc_now_iso(),
            ),
        )
    except sqlite3.Error:
        logger.warning("admin_audit unavailable — '%s' by %s went unrecorded", action, request.user["id"])








############################################################
# _disconnect_user_sockets
############################################################
#
# Kill switch for a deactivated user's live Socket.IO
# connections. A socket authenticates ONCE at handshake, so
# without this a banned user kept reading (and writing to)
# every conversation room until they closed the app —
# deleting their session rows only stops the next REST call.
# Guarded lazy import: the helper belongs to chat/events.py
# (disconnect_user_sockets), and until it lands there this
# is a silent no-op rather than an import error. A
# socket-layer failure never fails the admin request that
# triggered it. app/auth/routes.py keeps its own identical
# copy for the logout paths.
#
# Used by:
#   - update_user (below) — after the active = 0 commit
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
# create_invitation
############################################################
#
# POST /api/admin/invitations
#
# Mints one invitation code. Body: role (default student),
# max_uses (int 1..1000, default 1), expires_hours (int
# 1..8760, default INVITATION_EXPIRY_HOURS from the config —
# 168 = 7 days unless the deployment says otherwise).
# Curators may mint student / teacher codes; admin and
# curator codes are admin-only (403). Out-of-range numbers
# are REJECTED with a 400 rather than clamped, and the body
# must be a JSON object. Answers 201 with the new row in
# EXACTLY the camelCase shape list_invitations uses (plus
# the additive createdBy) — a fresh code is expired: false /
# fullyUsed: false, so the client can prepend the object to
# its list unchanged.
#
# The code is the first 12 hex chars of a uuid4, uppercased
# (48 bits, hex only — not full alphanumeric), stored in a
# UNIQUE column: a collision would surface as IntegrityError
# → 500, accepted as negligible. expires_at is an aware-UTC
# isoformat string ("YYYY-MM-DDTHH:MM:SS.ffffff+00:00") —
# auth's checks and admin_stats' string comparison both
# depend on exactly that shape. created_at is stamped the
# same way with utc_now_iso() instead of being left to the
# column DEFAULT: datetime('now') writes the space-separated
# form migration v17 spent a whole pass removing, and a
# space-form row sorts BEFORE every T-form row of the same
# day, which would file a brand-new code halfway down the
# list's ORDER BY created_at DESC.
#
# Minting is rate limited per actor (30 per 5-minute window)
# — a stolen curator token could otherwise print role grants
# in a loop.
#
# Used by:
#   - services/api/admin.ts — createInvitation (the "new
#     code" form on app/(main)/admin/index.tsx)
############################################################

@admin_bp.route("/invitations", methods=["POST"])
@require_role("admin", "curator")
@rate_limit("invite", max_attempts=30)
def create_invitation():
    # STEP 1: validate the body — ints first (bool is an int
    # subclass, hence the explicit exclusion), then the range,
    # then the role
    # ========================================================
    data = get_json_object()
    if data is None:
        return jsonify({"error": "JSON object body required"}), 400

    role = data.get("role", "student")

    # The deployed INVITATION_EXPIRY_HOURS was read at startup
    # and then ignored here for the literal 168. It is clamped
    # into the accepted range so a nonsense env value cannot
    # turn every default mint into a 400
    config_hours = current_app.config.get("INVITATION_EXPIRY_HOURS", _INVITE_EXPIRY_FALLBACK)
    if not isinstance(config_hours, int) or isinstance(config_hours, bool):
        config_hours = _INVITE_EXPIRY_FALLBACK
    default_hours = min(max(config_hours, _EXPIRES_HOURS_MIN), _EXPIRES_HOURS_MAX)

    raw_max_uses = data.get("max_uses", 1)
    raw_expires_hours = data.get("expires_hours", default_hours)

    if not isinstance(raw_max_uses, int) or isinstance(raw_max_uses, bool):
        return jsonify({"error": "max_uses must be an integer"}), 400
    if not isinstance(raw_expires_hours, int) or isinstance(raw_expires_hours, bool):
        return jsonify({"error": "expires_hours must be an integer"}), 400

    # Rejected, not clamped — see the bounds block at the top
    if not _MAX_USES_MIN <= raw_max_uses <= _MAX_USES_MAX:
        return jsonify({"error": f"max_uses must be between {_MAX_USES_MIN} and {_MAX_USES_MAX}"}), 400
    if not _EXPIRES_HOURS_MIN <= raw_expires_hours <= _EXPIRES_HOURS_MAX:
        return jsonify({"error": f"expires_hours must be between {_EXPIRES_HOURS_MIN} and {_EXPIRES_HOURS_MAX}"}), 400

    max_uses = raw_max_uses
    expires_hours = raw_expires_hours

    if role not in ROLES:
        return jsonify({"error": "Invalid role"}), 400

    # Curators pass the decorator too — escalation via a self-minted
    # admin/curator code is blocked here, not by require_role
    if role in PRIVILEGED_ROLES and request.user["role"] != "admin":
        return jsonify({"error": "Only admins can create admin/curator invitations"}), 403

    # Privileged codes are single-use and short-lived, even for
    # admins: an explicit multi-use or long expiry is rejected,
    # and the config default is clamped under the cap so an
    # omitted expires_hours still mints
    if role in PRIVILEGED_ROLES:
        if max_uses != 1:
            return jsonify({"error": "Admin/curator invitations must be single-use"}), 400
        if "expires_hours" in data and expires_hours > _PRIVILEGED_EXPIRES_MAX:
            return jsonify({
                "error": f"Admin/curator invitations must expire within {_PRIVILEGED_EXPIRES_MAX} hours"
            }), 400
        expires_hours = min(expires_hours, _PRIVILEGED_EXPIRES_MAX)


    # STEP 2: mint the code and insert it with its audit row —
    # one transaction, so the trail cannot drift from the table
    # =========================================================
    code_id = str(uuid.uuid4())
    code = uuid.uuid4().hex[:12].upper()  # 12 uppercase hex chars (48 bits)
    created_at = utc_now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).isoformat()

    db = get_db()
    try:
        db.execute(
            """INSERT INTO invitation_codes (id, code, role, created_by, max_uses, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (code_id, code, role, request.user["id"], max_uses, expires_at, created_at),
        )
        _write_audit(db, "invitation.create", code_id,
                     {"role": role, "maxUses": max_uses, "expiresAt": expires_at})
        db.commit()

        return jsonify({
            "id": code_id,
            "code": code,
            "role": role,
            "maxUses": max_uses,
            "useCount": 0,
            "expiresAt": expires_at,
            "createdAt": created_at,
            "createdBy": request.user["id"],
            "expired": False,
            "fullyUsed": False,
        }), 201

    finally:
        db.close()








############################################################
# list_invitations
############################################################
#
# GET /api/admin/invitations
#
# Invitation codes, newest first, with two derived flags:
# expired (_invitation_expired — aware-to-aware, the exact
# comparison auth's register() makes, so the two never
# disagree, and an unparsable stored value reads as expired
# instead of 500-ing the whole listing) and fullyUsed
# (_invitation_fully_used — use_count >= max_uses, reading a
# hand-edited non-numeric counter as used for the same
# reason). Admins see every code; a curator
# sees only the student/teacher codes THEY created — an
# admin-role code string must never reach a curator's screen
# (its copy/QR actions would hand out an escalation path),
# and the role filter also hides any pre-existing rows an
# admin later granted them. Revoked codes are hard deleted,
# so they never appear here.
#
# Optional ?limit= (1..500) and ?offset= page the result;
# with neither param the listing returns every row exactly
# as it always has, which is what the app asks for.
# createdBy is additive — it names the minting admin, the
# one column the audit trail cannot reconstruct for codes
# that predate migration v40.
#
# Used by:
#   - services/api/admin.ts — fetchAdminInvitations (the
#     invitation list on app/(main)/admin/index.tsx)
############################################################

@admin_bp.route("/invitations", methods=["GET"])
@require_role("admin", "curator")
def list_invitations():
    page_sql, page_params, page_error = _pagination_clause()
    if page_error:
        return jsonify({"error": page_error}), 400

    db = get_db()
    try:
        if request.user["role"] == "admin":
            rows = db.execute(
                "SELECT * FROM invitation_codes ORDER BY created_at DESC" + page_sql,
                page_params,
            ).fetchall()
        else:
            # Curator scope: own codes, mintable roles only — see
            # the banner. The IN slots are '?' characters counted
            # off _CURATOR_ROLES, so nothing user-supplied reaches
            # the SQL text
            rows = db.execute(
                f"""SELECT * FROM invitation_codes
                    WHERE created_by = ? AND role IN ({_CURATOR_ROLE_SLOTS})
                    ORDER BY created_at DESC{page_sql}""",
                [request.user["id"], *_CURATOR_ROLES, *page_params],
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
                "createdBy": r["created_by"],
                "expired": _invitation_expired(r["expires_at"]),
                "fullyUsed": _invitation_fully_used(r["use_count"], r["max_uses"]),
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
# 404 when nothing matched. Users who already registered
# with the code are unaffected; the row's use_count history
# goes with it.
#
# Open to curators as well as admins — a curator could mint
# codes but never take one back, so a code posted in the
# wrong chat stayed live until an admin noticed. The DELETE
# is scoped exactly like the curator's half of
# list_invitations (own rows, mintable roles only), which
# means a curator aiming at somebody else's — or an
# admin-role — code gets the same 404 as a nonexistent one
# and learns nothing about it.
#
# Revocation does NOT race registration: auth's register()
# burns the code with a conditional UPDATE and treats
# rowcount 0 (row gone, or exhausted) as a refusal, so a
# code deleted between its SELECT and its INSERT can no
# longer grant a role.
#
# Used by:
#   - services/api/admin.ts — revokeInvitation (the delete
#     action on app/(main)/admin/index.tsx)
############################################################

@admin_bp.route("/invitations/<code_id>", methods=["DELETE"])
@require_role("admin", "curator")
def delete_invitation(code_id):
    db = get_db()
    try:
        if request.user["role"] == "admin":
            result = db.execute("DELETE FROM invitation_codes WHERE id = ?", (code_id,))
        else:
            result = db.execute(
                f"""DELETE FROM invitation_codes
                    WHERE id = ? AND created_by = ? AND role IN ({_CURATOR_ROLE_SLOTS})""",
                [code_id, request.user["id"], *_CURATOR_ROLES],
            )

        if result.rowcount == 0:
            return jsonify({"error": "Invitation not found"}), 404

        _write_audit(db, "invitation.revoke", code_id)
        db.commit()
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
# Optional ?limit= (1..500) and ?offset= page the result;
# with neither param every user comes back, exactly as the
# app expects. This is the body that made Cache-Control:
# no-store worth adding app-wide — it carries every e-mail
# address the faculty holds.
#
# Used by:
#   - services/api/admin.ts — fetchAdminUsers (the user
#     list on app/(main)/admin-users/index.tsx)
############################################################

@admin_bp.route("/users", methods=["GET"])
@require_role("admin")
def list_users():
    page_sql, page_params, page_error = _pagination_clause()
    if page_error:
        return jsonify({"error": page_error}), 400

    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, username, email, display_name, role, active, created_at FROM users ORDER BY created_at DESC" + page_sql,
            page_params,
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
# (optional, one of ROLES), active (optional bool). Both
# absent → 400 "Nothing to update". Deactivating also
# deletes every session AND every push token of that user,
# kills their live Socket.IO connections, and the flag bites
# on the REST side too: get_current_user() refuses inactive
# users on every request and login() answers 403 (both rely
# on the migration-v8 column). Answers the fresh row in the
# list_users shape.
#
# An unknown user_id is a 404 BEFORE the body is looked at —
# a PATCH with both a bad id and a bad body used to report
# the body problem and let the caller believe the user
# existed. A target that disappears BETWEEN that check and
# the answer (a concurrent DELETE) gets the same 404 rather
# than the 500 the post-commit re-read used to raise on the
# missing row.
#
# `active` must be a real JSON boolean — a 0 or "false"
# used to slip past the `is False` self-deactivation guard
# and deactivate (and log out) the calling admin, so any
# non-bool answers 400 before the guards run.
#
# Two guards keep the console from locking everyone out of
# itself: an admin may not strip their OWN admin role (the
# mirror of the self-deactivation guard — DbGate runs
# without a database volume, so there is no manual recovery
# path), and no role change or deactivation may leave the
# users table with zero active admins. That second count is
# a backstop by design: the actor is always an active admin
# themselves, so it can only ever fire on a self-change the
# first guard already refused, and on whatever path a future
# caller opens.
#
# Gotcha (documented, not fixed): a role change leaves
# existing sessions alive, so a demoted admin keeps their
# admin token until it expires — only DEACTIVATION revokes
# sessions.
#
# Used by:
#   - services/api/admin.ts — updateAdminUser (the role /
#     active editors on app/(main)/admin-users/index.tsx)
############################################################

@admin_bp.route("/users/<user_id>", methods=["PATCH"])
@require_role("admin")
def update_user(user_id):
    db = get_db()
    try:
        # STEP 1: the target must exist — an unknown id is a 404
        # before any complaint about the body (see the banner)
        # ======================================================
        target = db.execute(
            "SELECT id, role, active FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not target:
            return jsonify({"error": "User not found"}), 404


        # STEP 2: validate — object body, role whitelist,
        # self-deactivation guard, then reject an empty patch
        # ===================================================
        data = get_json_object()
        if data is None:
            return jsonify({"error": "JSON object body required"}), 400

        new_role = data.get("role")
        active = data.get("active")

        if new_role is not None and new_role not in ROLES:
            return jsonify({"error": "Invalid role"}), 400

        # Only true/false may reach the guards below — see the banner
        if active is not None and not isinstance(active, bool):
            return jsonify({"error": "active must be a boolean"}), 400

        if active is False and user_id == request.user["id"]:
            return jsonify({"error": "Cannot deactivate your own account"}), 400

        if new_role is None and active is None:
            return jsonify({"error": "Nothing to update"}), 400


        # STEP 3: admin continuity — nobody demotes themselves
        # out of admin, and the last active admin stays
        # ====================================================
        demotes_admin = target["role"] == "admin" and new_role is not None and new_role != "admin"

        if demotes_admin and user_id == request.user["id"]:
            return jsonify({"error": "Cannot remove your own admin role"}), 400

        if demotes_admin or (active is False and target["role"] == "admin"):
            remaining_admins = db.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
                (user_id,),
            ).fetchone()["c"]
            if remaining_admins == 0:
                return jsonify({"error": "Cannot remove the last active admin"}), 400


        # STEP 4: apply each field on its own, audit both, and
        # purge the sessions and push tokens on deactivation.
        # updated_at is stamped with utc_now_iso() — the column
        # DEFAULT would write migration v17's space-separated
        # form back into a T-form table
        # =====================================================
        now = utc_now_iso()

        if new_role is not None:
            db.execute(
                "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                (new_role, now, user_id),
            )
            _write_audit(db, "user.role", user_id, {"from": target["role"], "to": new_role})

        if active is not None:
            # Column guaranteed by migration v8; login() and get_current_user()
            # both enforce it, so the write alone already locks the user out
            db.execute(
                "UPDATE users SET active = ?, updated_at = ? WHERE id = ?",
                (1 if active else 0, now, user_id),
            )
            _write_audit(db, "user.active", user_id, {"active": active})

            # Drop live sessions too — frees the rows and makes the logout
            # immediate even if the auth check were ever relaxed — and the
            # push tokens with them, or the signed-out device would keep
            # receiving message previews
            if not active:
                db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                db.execute("DELETE FROM push_tokens WHERE user_id = ?", (user_id,))

        db.commit()

        # A socket authenticates once, at handshake: without this the
        # banned user keeps reading every room they had joined. After
        # the commit, so a failed write never kills a live connection
        if active is False:
            _disconnect_user_sockets(user_id)


        # STEP 5: answer the fresh row in the list_users shape —
        # or the same 404 STEP 1 would have given when the row is
        # gone by now (a concurrent DELETE); the patch itself
        # matched nothing, and there is no row left to hand back
        # =======================================================
        updated = db.execute(
            "SELECT id, username, email, display_name, role, active, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not updated:
            return jsonify({"error": "User not found"}), 404

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
# delete_user
############################################################
#
# DELETE /api/admin/users/<user_id>
#
# The admin's erasure path (GDPR Art. 17 requests reach the
# faculty, not the phone): runs the same erase_user_account
# routine the self-service DELETE /api/auth/me uses — the
# users row survives anonymised, authored posts are
# tombstoned, everything personal is hard-deleted and the
# uploaded files leave the disk (the full inventory lives on
# that helper's banner). Admin-only — a curator can neither
# read nor erase accounts. Guards: an admin erases their OWN
# account through /api/auth/me (password-confirmed), not
# here; erasing another admin obeys the same last-active-
# admin continuity rule update_user enforces. Idempotent in
# effect but not in answer: an already-anonymised target is
# just a user row like any other, so a repeat is a second
# (harmless) erasure. Audited as user.delete.
#
# Used by:
#   - services/api/admin.ts — deleteUserApi
#     (app/(main)/admin-users/index.tsx — the erase action)
############################################################

@admin_bp.route("/users/<user_id>", methods=["DELETE"])
@require_role("admin")
def delete_user(user_id):
    # STEP 1: the target must exist, must not be the caller,
    # and admin continuity holds
    # ======================================================
    if user_id == request.user["id"]:
        return jsonify({"error": "Delete your own account through DELETE /api/auth/me"}), 400

    db = get_db()
    try:
        target = db.execute(
            "SELECT id, role, active FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not target:
            return jsonify({"error": "User not found"}), 404

        if target["role"] == "admin" and target["active"]:
            remaining = db.execute(
                "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND active = 1 AND id != ?",
                (user_id,),
            ).fetchone()["c"]
            if remaining == 0:
                return jsonify({"error": "Cannot remove the last active admin"}), 400


        # STEP 2: erase, audit, commit — one transaction, then
        # drop the target's live sockets
        # ====================================================
        erase_user_account(db, user_id)
        _write_audit(db, "user.delete", user_id)
        db.commit()

        logger.info("Account erased by admin %s: user=%s", request.user["id"], user_id)
        from app.chat.events import disconnect_user_sockets
        try:
            disconnect_user_sockets(user_id)
        except Exception:
            logger.exception("Socket disconnect failed after erasure of %s", user_id)

        return jsonify({"status": "deleted"})
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
# posts and scrapedArticles come out of ONE grouped pass
# over news_posts instead of two full scans (idx_news_posts_
# source carries it), and the finished tile numbers are
# cached in-process for _STATS_CACHE_TTL seconds — the admin
# screen refetches on every focus and these five counts used
# to be five table scans each time.
#
# activeInvitations compares dates as strings on purpose.
# expires_at is Python isoformat — 'T' separator, fractional
# seconds, "+00:00" suffix — while SQLite's datetime('now')
# prints a space separator. Compared raw, 'T' sorts after
# ' ', so a same-day code that had already expired would
# still count as active. Both sides are therefore cut to
# the 19-char YYYY-MM-DDTHH:MM:SS shape (strftime with a
# literal T), and the stored value has its separator
# normalised first so a hand-edited space-form row reaches
# the same verdict as list_invitations' fromisoformat flag.
# Both clocks are UTC, so no row escapes the comparison.
#
# A string comparison only means anything on a string that
# is a date, though: 'netrukus' sorts above every timestamp
# the clock can print and used to be COUNTED, while the
# listing read the same row as expired. A GLOB on the first
# ten characters throws out anything that does not open with
# a YYYY-MM-DD, so an unparsable (or NULL) expires_at is not
# active here either. The residue is a row that starts with
# a date and turns to nonsense after it — nothing writes
# one, and it is the one shape still counted while the
# listing calls it expired.
#
# Used by:
#   - services/api/admin.ts — fetchAdminStats (the dashboard
#     tiles on app/(main)/admin/index.tsx)
############################################################

@admin_bp.route("/stats", methods=["GET"])
@require_role("admin")
def admin_stats():
    # STEP 1: serve the snapshot while it is fresh — a counter
    # lagging by under a minute is invisible on a tile
    # ========================================================
    now = time.monotonic()
    with _stats_cache_lock:
        if _stats_cache and now - _stats_cache["at"] < _STATS_CACHE_TTL:
            return jsonify(_stats_cache["stats"])


    # STEP 2: rebuild — one grouped pass over news_posts, three
    # single counts
    # =========================================================
    db = get_db()
    try:
        user_count = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]

        post_count = 0
        scraped_count = 0
        for row in db.execute("SELECT source, COUNT(*) as c FROM news_posts GROUP BY source").fetchall():
            post_count += row["c"]
            if row["source"] in ("knf.vu.lt", "vu.lt"):
                scraped_count += row["c"]

        comment_count = db.execute("SELECT COUNT(*) as c FROM news_comments").fetchone()["c"]
        # expires_at is ISO with a 'T' separator while datetime('now') uses
        # a space — normalise the stored separator and compare on the same
        # 19-char shape so same-day codes count correctly. The GLOB is the
        # sanity gate: a value that does not even open with a date is not a
        # timestamp, and a string comparison would sort it ABOVE the clock
        # and count it as active (see the banner)
        active_invitations = db.execute(
            "SELECT COUNT(*) as c FROM invitation_codes WHERE use_count < max_uses AND substr(replace(expires_at, ' ', 'T'), 1, 10) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' AND substr(replace(expires_at, ' ', 'T'), 1, 19) > strftime('%Y-%m-%dT%H:%M:%S', 'now')"
        ).fetchone()["c"]
    finally:
        db.close()


    # STEP 3: publish the snapshot and answer it
    # ==========================================
    stats = {
        "users": user_count,
        "posts": post_count,
        "scrapedArticles": scraped_count,
        "comments": comment_count,
        "activeInvitations": active_invitations,
    }

    with _stats_cache_lock:
        _stats_cache["at"] = time.monotonic()
        _stats_cache["stats"] = stats

    return jsonify(stats)








############################################################
# _get_socketio
############################################################
#
# The SocketIO instance is bound in app/__init__.py, which
# imports this module from inside create_app(); the lookup
# is deferred to call time as the guard against that package
# importing us while it is itself half-loaded. It binds
# socketio before the factory runs, so the cycle does not
# bite today — the guard simply costs nothing.
# chat/routes.py keeps the identical helper.
#
# Used by:
#   - send_admin_notification (below) — start_background_task
############################################################

def _get_socketio():
    from app import socketio
    return socketio








############################################################
# _fanout_counts
############################################################
#
# (sent, failed) out of whatever notify_channel handed back.
# It returns a bare accepted-ticket count today, and
# app/notifications/push.py is growing a failure count
# alongside it; a 2-tuple or a {"sent", "failed"} dict is
# therefore read as well, so this module reports `failed`
# the moment that half lands and 0 until then. Nothing here
# needs changing when it does.
#
# Used by:
#   - _run_broadcast (below)
############################################################

def _fanout_counts(result):
    if isinstance(result, tuple) and len(result) == 2:
        return int(result[0]), int(result[1])
    if isinstance(result, dict):
        return int(result.get("sent", 0)), int(result.get("failed", 0))
    return int(result or 0), 0








############################################################
# _set_broadcast_job
############################################################
#
# Creates or updates one job record under the registry lock
# and returns a COPY of it — the caller must never hold a
# reference the background task keeps mutating. The record
# is moved to the end on every touch and the oldest are
# dropped past _BROADCAST_JOBS_MAX, so the dict cannot grow
# on a process that never restarts.
#
# Used by:
#   - send_admin_notification (below) — the queued record
#   - _run_broadcast (below) — the running record
############################################################

def _set_broadcast_job(job_id, **fields):
    with _broadcast_jobs_lock:
        job = _broadcast_jobs.setdefault(job_id, {"jobId": job_id})
        job.update(fields)
        _broadcast_jobs.move_to_end(job_id)
        while len(_broadcast_jobs) > _BROADCAST_JOBS_MAX:
            _broadcast_jobs.popitem(last=False)
        return dict(job)








############################################################
# _update_broadcast_job
############################################################
#
# _set_broadcast_job for a job that must ALREADY be in the
# registry: the copy when it is there, None when it is not.
# The fan-out finishes long after the request, and 50 newer
# broadcasts in between evict the record it started from —
# a plain setdefault then RESURRECTED the job as a bare
# jobId/status/sent/failed/finishedAt/message record, and
# GET /notifications/<id> answered 200 with a body missing
# the title and createdAt its own 202 had promised. An
# evicted job is forgotten by design, so the finishing write
# lets it stay forgotten and the read is the documented 404.
#
# Used by:
#   - _run_broadcast (below) — the done / failed record
############################################################

def _update_broadcast_job(job_id, **fields):
    with _broadcast_jobs_lock:
        job = _broadcast_jobs.get(job_id)
        if job is None:
            return None

        job.update(fields)
        _broadcast_jobs.move_to_end(job_id)
        return dict(job)








############################################################
# _broadcast_job
############################################################
#
# A copy of one job record, or None when the id is unknown —
# which includes every job from before the last restart.
#
# Used by:
#   - broadcast_job_status (below)
############################################################

def _broadcast_job(job_id):
    with _broadcast_jobs_lock:
        job = _broadcast_jobs.get(job_id)
        return dict(job) if job else None








############################################################
# _run_broadcast
############################################################
#
# The fan-out itself, on a background task: one Expo POST
# per 100 device tokens, each with a 30 s timeout, which is
# exactly why it no longer runs inside the request. Nothing
# here touches `request` or an app context — notify_channel
# opens its own connection through get_db(). Failures are
# recorded on the job and logged, never raised: there is no
# caller left to receive them, and that covers reading the
# result as well as fetching it — a shape _fanout_counts
# refuses marks the job failed instead of leaving it
# "running" for good.
#
# The running mark REGISTERS the job (a fan-out is real even
# for an id the registry never saw), while the finishing
# mark only updates: a record the LRU dropped mid-flight
# stays dropped rather than coming back half-built.
#
# The local import of notify_channel is not a cycle guard —
# push.py imports only app.database and is already loaded at
# startup via notifications/routes.py.
#
# Used by:
#   - send_admin_notification (below) — via
#     socketio.start_background_task
############################################################

def _run_broadcast(job_id, title, body_text, extra_data):
    from app.notifications.push import notify_channel

    _set_broadcast_job(job_id, status="running")

    # The stats dict is where notify_channel reports what the
    # bare return cannot: failed slices and the DISTINCT owners
    # behind the tokens — tickets are devices, not people
    stats: dict = {}
    try:
        result = notify_channel("admin", title, body_text, data=extra_data, stats=stats)
        # Inside the try on purpose: a result shape _fanout_counts
        # cannot read is a failed broadcast, not an exception
        # escaping the task and stranding the job in "running"
        sent, failed = _fanout_counts(result)
        failed = int(stats.get("failed", failed))
        users = int(stats.get("users", 0))
    except Exception:
        logger.exception("Admin broadcast job %s failed", job_id)
        _update_broadcast_job(job_id, status="failed", finishedAt=utc_now_iso(),
                              message="Broadcast failed — see the server log")
        return

    _update_broadcast_job(
        job_id,
        status="done",
        sent=sent,
        failed=failed,
        distinctUsers=users,
        finishedAt=utc_now_iso(),
        message=f"Accepted by Expo for {sent} device token(s) across {users} user(s)",
    )
    logger.info("Admin broadcast %s: %d accepted, %d failed, %d users", job_id, sent, failed, users)








############################################################
# send_admin_notification
############################################################
#
# POST /api/admin/notifications
#
# Broadcast push on the "admin" channel. Body: title (<= 200
# chars) and body (<= 1000), both required strings, trimmed
# before the checks; an optional `data` object rides along
# as the push payload. Delivery goes through notify_channel,
# which targets every active push token whose user has NOT
# opted out of the admin channel (opt-out model, see
# app/notifications/push.py) and stamps data["channel"] =
# "admin" on the way out.
#
# The fan-out is a background task and the answer is 202
# with a job id: every 100 device tokens are one Expo POST
# with a 30 s timeout, so a faculty-wide broadcast held the
# request — and a worker — open for minutes. `sent` and
# `failed` start at 0 and the real numbers arrive on GET
# /api/admin/notifications/<job_id>. `sent` keeps its name
# but never meant delivered devices: it counts tickets Expo
# ACCEPTED, which is why the finished message says so.
#
# The caller's `data` is merged UNDER the type marker rather
# than replacing the whole default dict, and "type" is then
# forced back to "admin_announcement" — the app routes
# announcements on that marker, and a custom payload used to
# silently drop it. The serialised payload is bounded at
# _BROADCAST_DATA_MAX because Expo refuses a message over
# 4 KiB outright.
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
    data = get_json_object()
    if data is None:
        return jsonify({"error": "JSON object body required"}), 400

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


    # STEP 2: build the payload — the type marker survives any
    # caller-supplied "type", and the whole thing is bounded
    # ========================================================
    raw_extra = data.get("data")
    if raw_extra is not None and not isinstance(raw_extra, dict):
        return jsonify({"error": "data must be an object"}), 400

    extra_data = dict(raw_extra or {})
    extra_data["type"] = "admin_announcement"

    if len(json.dumps(extra_data, ensure_ascii=False).encode()) > _BROADCAST_DATA_MAX:
        return jsonify({"error": f"data must serialise to at most {_BROADCAST_DATA_MAX} bytes"}), 400


    # STEP 3: register the job, audit it, and hand the Expo
    # round-trips to a background task
    # =====================================================
    job_id = str(uuid.uuid4())

    db = get_db()
    try:
        _write_audit(db, "notification.broadcast", job_id, {"title": title})
        db.commit()
    finally:
        db.close()

    job = _set_broadcast_job(
        job_id,
        status="queued",
        sent=0,
        failed=0, distinctUsers=0,
        title=title,
        createdAt=utc_now_iso(),
        finishedAt=None,
        message="Broadcast accepted for delivery on the admin channel",
    )

    _get_socketio().start_background_task(_run_broadcast, job_id, title, body_text, extra_data)

    return jsonify(job), 202








############################################################
# broadcast_job_status
############################################################
#
# GET /api/admin/notifications/<job_id>
#
# The record send_admin_notification's 202 handed out:
# status (queued / running / done / failed), the accepted
# ticket count in `sent`, `failed` beside it, and the
# timestamps. 404 once the job is unknown — the registry
# lives in this process only, so a restart or 50 newer
# broadcasts forget it.
#
# Used by:
#   - nothing calls this at the moment — it exists so the
#     job id in the 202 can be resolved at all
############################################################

@admin_bp.route("/notifications/<job_id>", methods=["GET"])
@require_role("admin")
def broadcast_job_status(job_id):
    job = _broadcast_job(job_id)
    if job is None:
        return jsonify({"error": "Broadcast job not found"}), 404
    return jsonify(job)








############################################################
# list_reports
############################################################
#
# GET /api/admin/reports?status=open|resolved
#
# The complaint ledger (POST /api/social/reports), newest
# first, joined to users for the reporter's name — and, for
# 'user' targets, to users again so the row can be rendered
# without a second lookup. No status filter means open only:
# the panel's job is the queue, the archive is opt-in.
# Capped at 200 rows.
#
# Used by:
#   - the admin panel's reports view
############################################################

@admin_bp.route("/reports", methods=["GET"])
@require_role("admin", "curator")
def list_reports():
    status = request.args.get("status", "open")
    if status not in ("open", "resolved"):
        return jsonify({"error": "status must be one of: open, resolved"}), 400

    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT r.id, r.reporter_id, ru.display_name AS reporter_name,
                   r.target_type, r.target_id, r.reason, r.status, r.created_at,
                   tu.display_name AS target_user_name
            FROM reports r
            JOIN users ru ON ru.id = r.reporter_id
            LEFT JOIN users tu ON r.target_type = 'user' AND tu.id = r.target_id
            WHERE r.status = ?
            ORDER BY r.created_at DESC
            LIMIT 200
            """,
            (status,),
        ).fetchall()

        return jsonify({
            "reports": [
                {
                    "id": r["id"],
                    "reporterId": r["reporter_id"],
                    "reporterName": r["reporter_name"],
                    "targetType": r["target_type"],
                    "targetId": r["target_id"],
                    "targetUserName": r["target_user_name"],
                    "reason": r["reason"],
                    "status": r["status"],
                    "createdAt": r["created_at"],
                }
                for r in rows
            ]
        })
    finally:
        db.close()







############################################################
# resolve_report
############################################################
#
# PUT /api/admin/reports/<report_id> {status}
#
# Moves a report between 'open' and 'resolved' (reopening is
# allowed — a resolve tapped by mistake must be reversible).
# Unknown id → 404. Audited like every other privileged
# write.
#
# Used by:
#   - the admin panel's reports view
############################################################

@admin_bp.route("/reports/<report_id>", methods=["PUT"])
@require_role("admin", "curator")
def resolve_report(report_id):
    # STEP 1: the body — a known status
    # =================================
    data = get_json_object()
    new_status = data.get("status") if data else None
    if new_status not in ("open", "resolved"):
        return jsonify({"error": "status must be one of: open, resolved"}), 400


    # STEP 2: the row must exist; then one write + the audit
    # ======================================================
    db = get_db()
    try:
        row = db.execute(
            "SELECT status FROM reports WHERE id = ?", (report_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Report not found"}), 404

        db.execute(
            "UPDATE reports SET status = ? WHERE id = ?",
            (new_status, report_id),
        )
        _write_audit(db, "report.status", report_id,
                     {"from": row["status"], "to": new_status})
        db.commit()

        return jsonify({"status": new_status})
    finally:
        db.close()
