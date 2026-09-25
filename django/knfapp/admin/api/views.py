############################################################
#  [*] Admin API — codes, users, stats, broadcast, reports
#
#  The console behind the mobile admin screens: minting and
#  revoking invitation codes (curators only their own
#  student/teacher ones), the user list with role/active
#  editing under the two continuity guards, the GDPR
#  erasure path, the cached dashboard counters, the
#  background broadcast job, and the complaint queue. Every
#  mutating handler writes one audit row inside its own
#  transaction.
#
#  Split into:
#
#    create_invitation / list_invitations / delete_invitation
#    list_users / update_user / delete_user
#    admin_stats
#    send_admin_notification / broadcast_job_status
#    list_reports / resolve_report
#    list_audit
#    list_uploads
#    get_reported_message
#    list_tombstones / restore_tombstone
############################################################


import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone


from django.db import transaction
from django.db.models import Count, F


from knfapp.admin.audit import write_audit
from knfapp.admin.models import AdminAudit
from knfapp.chat.models import Message
from knfapp.common import ratelimit
from knfapp.common.http import clean_param, get_json_object, json_error, json_response, require_methods
from knfapp.common.timestamps import parse_stored, utc_now, utc_now_iso
from knfapp.news.models import DeletedSourceUrl, NewsComment, NewsPost
from knfapp.notifications.models import PushToken
from knfapp.social.models import Report
from knfapp.uploads.models import Upload
from knfapp.users.auth import require_role
from knfapp.users.erasure import erase_user_account
from knfapp.users.models import ROLES, PRIVILEGED_ROLES, InvitationCode, Session, User


logger = logging.getLogger(__name__)

# Invitation bounds — REJECTED outside them, never clamped (a
# silently rewritten 10000 would answer 201 with a maxUses
# nobody asked for); privileged codes are credentials, not
# flyers: single-use and short-lived whoever mints them
MAX_USES_MIN = 1
MAX_USES_MAX = 1000
EXPIRES_HOURS_MIN = 1
EXPIRES_HOURS_MAX = 8760          # 365 days
PRIVILEGED_EXPIRES_MAX = 72       # 3 days
INVITE_EXPIRY_DEFAULT = 168       # 7 days

# ?limit / ?offset bounds for both listings; absent params keep
# the return-everything behaviour the app relies on
PAGE_LIMIT_MIN = 1
PAGE_LIMIT_MAX = 500
PAGE_OFFSET_MAX = 2 ** 63 - 1

# The roles a curator may mint AND revoke
CURATOR_ROLES = tuple(r for r in ROLES if r not in PRIVILEGED_ROLES)

# The dashboard snapshot — five counters, rebuilt at most every
# 45 s (the admin screen refetches on every focus); cleared
# only by a restart. reset_stats_cache() is the test seam
_stats_cache: dict = {}
_stats_cache_lock = threading.Lock()
STATS_CACHE_TTL = 45

# Broadcast bookkeeping — in-process like the rate limiter: a
# restart forgets every job and only the newest survive, which
# is why the status read may 404 a job that really ran
_broadcast_jobs: "OrderedDict[str, dict]" = OrderedDict()
_broadcast_jobs_lock = threading.Lock()
BROADCAST_JOBS_MAX = 50
BROADCAST_DATA_MAX = 3072         # Expo refuses messages past 4 KiB


def reset_stats_cache():
    with _stats_cache_lock:
        _stats_cache.clear()








############################################################
# _pagination_clause / _invitation_flags
############################################################
#
# The optional ?limit/?offset pair as (limit, offset, error)
# — (None, 0, None) when neither is present, so the listings
# keep returning everything; offset is bounded ABOVE too (a
# 64-bit overflow would otherwise be the one param to 500 a
# listing). The two derived invitation flags read hand-
# edited garbage conservatively: an unparsable expires_at is
# expired, a non-numeric counter is fully used — one bad row
# must never 500 the whole listing.
#
# Used by:
#   - list_invitations, list_users, create_invitation (below)
############################################################

def _pagination_clause(request):
    raw_limit = request.GET.get("limit")
    raw_offset = request.GET.get("offset")
    if raw_limit is None and raw_offset is None:
        return None, 0, None

    limit = None
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return None, 0, "limit must be an integer"
        if not PAGE_LIMIT_MIN <= limit <= PAGE_LIMIT_MAX:
            return None, 0, f"limit must be between {PAGE_LIMIT_MIN} and {PAGE_LIMIT_MAX}"

    offset = 0
    if raw_offset is not None:
        try:
            offset = int(raw_offset)
        except (TypeError, ValueError):
            return None, 0, "offset must be an integer"
        if offset < 0:
            return None, 0, "offset must be zero or greater"
        if offset > PAGE_OFFSET_MAX:
            return None, 0, f"offset must be at most {PAGE_OFFSET_MAX}"

    return limit, offset, None


def _page(queryset, limit, offset):
    if limit is None and offset == 0:
        return queryset
    if limit is None:
        return queryset[offset:]
    return queryset[offset:offset + limit]


def _invitation_expired(expires_at):
    parsed = parse_stored(expires_at)
    if parsed is None:
        return True
    return parsed < datetime.now(timezone.utc)


def _invitation_fully_used(use_count, max_uses):
    try:
        return int(use_count) >= int(max_uses)
    except (TypeError, ValueError):
        return True


def _invitation_payload(r):
    return {
        "id": r["id"],
        "code": r["code"],
        "role": r["role"],
        "maxUses": r["max_uses"],
        "useCount": r["use_count"],
        "expiresAt": r["expires_at"],
        "createdAt": r["created_at"],
        "createdBy": r["created_by_id"],
        "expired": _invitation_expired(r["expires_at"]),
        "fullyUsed": _invitation_fully_used(r["use_count"], r["max_uses"]),
    }


_INVITE_FIELDS = ("id", "code", "role", "max_uses", "use_count", "expires_at", "created_at", "created_by_id")








############################################################
# create_invitation / list_invitations / delete_invitation
############################################################
#
# Minting: curators may mint student/teacher codes; admin
# and curator codes are admin-only, single-use and capped
# at 72 hours even for admins — a photographed QR must not
# be a standing backdoor. Out-of-range numbers are rejected.
# The listing gives admins everything and a curator only
# the mintable-role codes THEY created (an admin code
# string must never reach a curator's screen); the revoke
# is scoped identically, so a curator aiming at somebody
# else's code gets the same 404 as a nonexistent one.
#
# Used by:
#   - services/api/admin.ts — the invitation screens
############################################################

@require_methods("POST")
@require_role("admin", "curator")
@ratelimit.per_user("invite", max_attempts=30)
def create_invitation(request):
    # STEP 1: validate — ints first (bool is an int subclass),
    # then the ranges, then the role
    # ========================================================
    data = get_json_object(request)
    if data is None:
        return json_error("JSON object body required", 400)

    role = data.get("role", "student")
    raw_max_uses = data.get("max_uses", 1)
    raw_expires_hours = data.get("expires_hours", INVITE_EXPIRY_DEFAULT)

    if not isinstance(raw_max_uses, int) or isinstance(raw_max_uses, bool):
        return json_error("max_uses must be an integer", 400)
    if not isinstance(raw_expires_hours, int) or isinstance(raw_expires_hours, bool):
        return json_error("expires_hours must be an integer", 400)
    if not MAX_USES_MIN <= raw_max_uses <= MAX_USES_MAX:
        return json_error(f"max_uses must be between {MAX_USES_MIN} and {MAX_USES_MAX}", 400)
    if not EXPIRES_HOURS_MIN <= raw_expires_hours <= EXPIRES_HOURS_MAX:
        return json_error(f"expires_hours must be between {EXPIRES_HOURS_MIN} and {EXPIRES_HOURS_MAX}", 400)

    max_uses = raw_max_uses
    expires_hours = raw_expires_hours

    if role not in ROLES:
        return json_error("Invalid role", 400)

    # Curators pass the decorator too — escalation via a
    # self-minted admin/curator code is blocked here
    if role in PRIVILEGED_ROLES and request.user["role"] != "admin":
        return json_error("Only admins can create admin/curator invitations", 403)

    if role in PRIVILEGED_ROLES:
        if max_uses != 1:
            return json_error("Admin/curator invitations must be single-use", 400)
        if "expires_hours" in data and expires_hours > PRIVILEGED_EXPIRES_MAX:
            return json_error(f"Admin/curator invitations must expire within {PRIVILEGED_EXPIRES_MAX} hours", 400)
        expires_hours = min(expires_hours, PRIVILEGED_EXPIRES_MAX)


    # STEP 2: mint + audit — one transaction, so the trail
    # cannot drift from the table
    # ====================================================
    code_id = str(uuid.uuid4())
    code = uuid.uuid4().hex[:12].upper()
    created_at = utc_now()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)

    InvitationCode.objects.create(id=code_id, code=code, role=role, created_by_id=request.user["id"],
                                  max_uses=max_uses, use_count=0, expires_at=expires_at, created_at=created_at)
    write_audit(request.user["id"], "invitation.create", code_id,
                {"role": role, "maxUses": max_uses, "expiresAt": expires_at.isoformat()})

    return json_response({
        "id": code_id, "code": code, "role": role, "maxUses": max_uses, "useCount": 0,
        "expiresAt": expires_at, "createdAt": created_at, "createdBy": request.user["id"],
        "expired": False, "fullyUsed": False,
    }, status=201)


@require_methods("GET")
@require_role("admin", "curator")
def list_invitations(request):
    limit, offset, error = _pagination_clause(request)
    if error:
        return json_error(error, 400)

    base = InvitationCode.objects.order_by("-created_at")
    if request.user["role"] != "admin":
        # Curator scope: own codes, mintable roles only
        base = base.filter(created_by_id=request.user["id"], role__in=CURATOR_ROLES)

    rows = _page(base.values(*_INVITE_FIELDS), limit, offset)
    return json_response({"invitations": [_invitation_payload(r) for r in rows]})


@require_methods("DELETE")
@require_role("admin", "curator")
def delete_invitation(request, code_id):
    base = InvitationCode.objects.filter(id=code_id)
    if request.user["role"] != "admin":
        base = base.filter(created_by_id=request.user["id"], role__in=CURATOR_ROLES)

    deleted, _ = base.delete()
    if deleted == 0:
        return json_error("Invitation not found", 404)

    write_audit(request.user["id"], "invitation.revoke", code_id)
    return json_response({"message": "Invitation deleted"})








############################################################
# _disconnect_user_sockets
############################################################
#
# Kick a user's live chat sockets — a socket authenticates
# once, at handshake, so a deactivated or erased user would
# keep reading every room they had joined. Guarded the same
# way as the auth-side helper: the socket layer's own
# failures are logged, never raised — presence plumbing
# must not fail an admin request.
#
# Used by:
#   - update_user (below) — after a deactivation
#   - delete_user (below) — after an erasure
############################################################

def _disconnect_user_sockets(user_id):
    try:
        from knfapp.chat.events import disconnect_user_sockets
    except ImportError:
        return
    try:
        disconnect_user_sockets(user_id)
    except Exception:
        logger.warning("Could not disconnect sockets for user %s", user_id)


def _user_payload(u):
    return {
        "id": u["id"],
        "username": u["username"],
        "email": u["email"],
        "displayName": u["display_name"],
        "role": u["role"],
        "active": bool(u["active"]),
        "createdAt": u["created_at"],
    }


_USER_FIELDS = ("id", "username", "email", "display_name", "role", "active", "created_at")








############################################################
# list_users / update_user / delete_user
############################################################
#
# The whole directory (admin-only — curators cannot read
# accounts), newest first. The PATCH edits role and/or the
# active flag; an unknown id is a 404 BEFORE the body is
# looked at, `active` must be a real JSON boolean (a 0 or
# "false" would slip past the `is False` self-deactivation
# guard), and two continuity guards keep the console from
# locking everyone out of itself: no admin strips their OWN
# admin role, and no change may leave zero active admins.
# Deactivation purges the user's sessions AND push tokens
# and kicks their sockets — the flag alone already locks
# them out (get_current_user refuses inactive users), the
# purge makes it immediate and stops message previews on the
# signed-out device. Gotcha: a bare role DEMOTION leaves
# existing sessions alive until they expire.
#
# The DELETE is the admin's GDPR erasure path — the same
# erase_user_account routine the self-service DELETE
# /api/auth/me runs (anonymised users row, tombstoned
# posts, personal rows hard-deleted, uploads off the disk),
# minus the password confirmation and plus the continuity
# guard; an admin erases their OWN account through
# /api/auth/me. Audited as user.delete.
#
# Used by:
#   - services/api/admin.ts — the admin-users screen
############################################################

@require_methods("GET")
@require_role("admin")
def list_users(request):
    limit, offset, error = _pagination_clause(request)
    if error:
        return json_error(error, 400)

    rows = _page(User.objects.order_by("-created_at").values(*_USER_FIELDS), limit, offset)
    return json_response({"users": [_user_payload(r) for r in rows]})


@require_methods("PATCH")
@require_role("admin")
def update_user(request, user_id):
    # STEP 1: the target must exist — an unknown id is a 404
    # before any complaint about the body
    # ======================================================
    target = User.objects.filter(id=user_id).values("id", "role", "active").first()
    if not target:
        return json_error("User not found", 404)


    # STEP 2: validate — object body, role whitelist,
    # self-deactivation guard, then reject an empty patch
    # ===================================================
    data = get_json_object(request)
    if data is None:
        return json_error("JSON object body required", 400)

    new_role = data.get("role")
    active = data.get("active")

    if new_role is not None and new_role not in ROLES:
        return json_error("Invalid role", 400)

    # Only true/false may reach the guards below
    if active is not None and not isinstance(active, bool):
        return json_error("active must be a boolean", 400)

    if active is False and user_id == request.user["id"]:
        return json_error("Cannot deactivate your own account", 400)

    if new_role is None and active is None:
        return json_error("Nothing to update", 400)


    # STEP 3: admin continuity — nobody demotes themselves out
    # of admin, and the last active admin stays. The count is
    # a backstop by design: the actor is always an active
    # admin themselves, so it can only fire on a self-change
    # the guards above already refused, and on whatever path a
    # future caller opens
    # ========================================================
    demotes_admin = target["role"] == "admin" and new_role is not None and new_role != "admin"

    if demotes_admin and user_id == request.user["id"]:
        return json_error("Cannot remove your own admin role", 400)

    if demotes_admin or (active is False and target["role"] == "admin"):
        remaining_admins = User.objects.filter(role="admin", active=1).exclude(id=user_id).count()
        if remaining_admins == 0:
            return json_error("Cannot remove the last active admin", 400)


    # STEP 4: apply each field on its own, audit both, and
    # purge the sessions and push tokens on deactivation
    # ====================================================
    now = utc_now()

    if new_role is not None:
        User.objects.filter(id=user_id).update(role=new_role, updated_at=now)
        write_audit(request.user["id"], "user.role", user_id, {"from": target["role"], "to": new_role})

    if active is not None:
        # The write alone already locks the user out — login() and
        # get_current_user() both enforce the flag
        User.objects.filter(id=user_id).update(active=active, updated_at=now)
        write_audit(request.user["id"], "user.active", user_id, {"active": active})

        # Drop live sessions too — makes the logout immediate even if
        # the auth check were ever relaxed — and the push tokens with
        # them, or the signed-out device would keep receiving previews
        if not active:
            Session.objects.filter(user_id=user_id).delete()
            PushToken.objects.filter(user_id=user_id).delete()

    if active is False:
        _disconnect_user_sockets(user_id)


    # STEP 5: answer the fresh row in the list_users shape — or
    # the same 404 STEP 1 would have given when the row is gone
    # by now (a concurrent DELETE)
    # =========================================================
    updated = User.objects.filter(id=user_id).values(*_USER_FIELDS).first()
    if not updated:
        return json_error("User not found", 404)

    return json_response(_user_payload(updated))


@require_methods("DELETE")
@require_role("admin")
def delete_user(request, user_id):
    # STEP 1: the target must exist, must not be the caller,
    # and admin continuity holds
    # ======================================================
    if user_id == request.user["id"]:
        return json_error("Delete your own account through DELETE /api/auth/me", 400)

    target = User.objects.filter(id=user_id).values("id", "role", "active").first()
    if not target:
        return json_error("User not found", 404)

    if target["role"] == "admin" and target["active"]:
        remaining = User.objects.filter(role="admin", active=1).exclude(id=user_id).count()
        if remaining == 0:
            return json_error("Cannot remove the last active admin", 400)


    # STEP 2: erase, audit, kick the target's live sockets
    # ====================================================
    erase_user_account(user_id)
    write_audit(request.user["id"], "user.delete", user_id)

    logger.info("Account erased by admin %s: user=%s", request.user["id"], user_id)
    _disconnect_user_sockets(user_id)

    return json_response({"status": "deleted"})








############################################################
# admin_stats
############################################################
#
# GET /api/admin/stats
#
# Five counters for the dashboard tiles: users, posts,
# scrapedArticles (news_posts whose source is knf.vu.lt or
# vu.lt — the two news scrapers), comments,
# activeInvitations. posts and scrapedArticles come out of
# ONE grouped pass over news_posts, and the finished tile
# numbers are cached in-process for STATS_CACHE_TTL seconds
# — the admin screen refetches on every focus and these five
# counts would otherwise be five table scans each time.
#
# activeInvitations is a typed comparison: expires_at is a
# real datetime column, so "not yet expired" is one __gt
# against the current instant.
#
# Used by:
#   - services/api/admin.ts — the dashboard tiles
############################################################

@require_methods("GET")
@require_role("admin")
def admin_stats(request):
    # STEP 1: serve the snapshot while it is fresh — a counter
    # lagging by under a minute is invisible on a tile
    # ========================================================
    now = time.monotonic()
    with _stats_cache_lock:
        if _stats_cache and now - _stats_cache["at"] < STATS_CACHE_TTL:
            return json_response(_stats_cache["stats"])


    # STEP 2: rebuild — one grouped pass over news_posts, three
    # single counts
    # =========================================================
    user_count = User.objects.count()

    post_count = 0
    scraped_count = 0
    for row in NewsPost.objects.values("source").annotate(c=Count("id")):
        post_count += row["c"]
        if row["source"] in ("knf.vu.lt", "vu.lt"):
            scraped_count += row["c"]

    comment_count = NewsComment.objects.count()

    active_invitations = (
        InvitationCode.objects
        .filter(use_count__lt=F("max_uses"), expires_at__gt=datetime.now(timezone.utc))
        .count()
    )


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

    return json_response(stats)








############################################################
# The broadcast job registry
############################################################
#
# _set_broadcast_job creates or updates one record under the
# registry lock and returns a COPY — the caller must never
# hold a reference the background task keeps mutating. Every
# touch moves the record to the end and the oldest fall off
# past BROADCAST_JOBS_MAX, so the dict cannot grow on a
# process that never restarts. _update_broadcast_job is the
# same write for a job that must ALREADY be there: the
# fan-out finishes long after the request, and 50 newer
# broadcasts evict the record it started from — a plain
# setdefault would RESURRECT the job as a bare fragment
# missing the title and createdAt its own 202 had promised.
# An evicted job is forgotten by design, so the finishing
# write lets it stay forgotten and the read is the 404.
#
# _fanout_counts reads (sent, failed) out of whatever
# notify_channel hands back — a bare count, a 2-tuple, or a
# {"sent", "failed"} dict — so this module reports `failed`
# the moment the sender grows that half and 0 until then.
############################################################

def _set_broadcast_job(job_id, **fields):
    with _broadcast_jobs_lock:
        job = _broadcast_jobs.setdefault(job_id, {"jobId": job_id})
        job.update(fields)
        _broadcast_jobs.move_to_end(job_id)
        while len(_broadcast_jobs) > BROADCAST_JOBS_MAX:
            _broadcast_jobs.popitem(last=False)
        return dict(job)


def _update_broadcast_job(job_id, **fields):
    with _broadcast_jobs_lock:
        job = _broadcast_jobs.get(job_id)
        if job is None:
            return None

        job.update(fields)
        _broadcast_jobs.move_to_end(job_id)
        return dict(job)


def _broadcast_job(job_id):
    with _broadcast_jobs_lock:
        job = _broadcast_jobs.get(job_id)
        return dict(job) if job else None


def _fanout_counts(result):
    if isinstance(result, tuple) and len(result) == 2:
        return int(result[0]), int(result[1])
    if isinstance(result, dict):
        return int(result.get("sent", 0)), int(result.get("failed", 0))
    return int(result or 0), 0








############################################################
# _run_broadcast / _spawn_broadcast
############################################################
#
# The fan-out itself, on a plain daemon thread: one Expo
# POST per 100 device tokens, each with a 30 s timeout,
# which is exactly why it never runs inside the request.
# Failures are recorded on the job and logged, never
# raised — there is no caller left to receive them, and
# that covers a result shape _fanout_counts refuses as
# well: it marks the job failed instead of leaving it
# "running" for good.
#
# The notify_channel import rides INSIDE the try, so ANY
# failure — an import problem included — lands the job
# "failed" with the documented message instead of stranding
# it in "running" with nobody left to catch the exception.
#
# The running mark REGISTERS the job (a fan-out is real even
# for an id the registry never saw), while the finishing
# mark only updates: a record the LRU dropped mid-flight
# stays dropped rather than coming back half-built.
#
# _spawn_broadcast is the seam the tests patch — the suite
# must observe the queued record, not race a real thread.
############################################################

def _run_broadcast(job_id, title, body_text, extra_data):
    _set_broadcast_job(job_id, status="running")

    # The stats dict is where notify_channel reports what the
    # bare return cannot: failed slices and the DISTINCT owners
    # behind the tokens — tickets are devices, not people
    stats: dict = {}
    try:
        from knfapp.notifications.push import notify_channel
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


def _spawn_broadcast(job_id, title, body_text, extra_data):
    threading.Thread(
        target=_run_broadcast,
        args=(job_id, title, body_text, extra_data),
        daemon=True,
    ).start()








############################################################
# send_admin_notification / broadcast_job_status
############################################################
#
# POST /api/admin/notifications — broadcast push on the
# "admin" channel. Body: title (<= 200 chars) and body
# (<= 1000), both required strings, trimmed; an optional
# `data` object rides along as the push payload. The answer
# is 202 with a job id — the fan-out is a background thread,
# because a faculty-wide broadcast would hold a worker
# open for minutes. `sent` counts tickets Expo ACCEPTED,
# never delivered devices — the finished message says so.
#
# The caller's `data` is merged UNDER the type marker and
# "type" is then forced back to "admin_announcement" — the
# app routes announcements on that marker, and a merge the
# other way would silently drop it. The serialised payload
# is bounded at BROADCAST_DATA_MAX because Expo refuses a
# message over 4 KiB outright. The thread is spawned via
# transaction.on_commit, so the audit row is on disk before
# the fan-out starts.
#
# GET /api/admin/notifications/<job_id> answers the record
# the 202 handed out — 404 once the job is unknown, which
# includes every job from before the last restart and
# anything 50 newer broadcasts evicted.
#
# Used by:
#   - nothing in the app yet — services/api/admin.ts has no
#     wrapper; the pair exists so the job id is resolvable
############################################################

@require_methods("POST")
@require_role("admin")
def send_admin_notification(request):
    # STEP 1: validate — strings only, trimmed, length-bounded
    # ========================================================
    data = get_json_object(request)
    if data is None:
        return json_error("JSON object body required", 400)

    raw_title = data.get("title", "")
    raw_body = data.get("body", "")
    if not isinstance(raw_title, str) or not isinstance(raw_body, str):
        return json_error("Title and body must be strings", 400)

    title = raw_title.strip()
    body_text = raw_body.strip()

    if not title or not body_text:
        return json_error("Title and body are required", 400)

    if len(title) > 200:
        return json_error("Title must be at most 200 characters", 400)
    if len(body_text) > 1000:
        return json_error("Body must be at most 1000 characters", 400)


    # STEP 2: build the payload — the type marker survives any
    # caller-supplied "type", and the whole thing is bounded
    # ========================================================
    raw_extra = data.get("data")
    if raw_extra is not None and not isinstance(raw_extra, dict):
        return json_error("data must be an object", 400)

    extra_data = dict(raw_extra or {})
    extra_data["type"] = "admin_announcement"

    if len(json.dumps(extra_data, ensure_ascii=False).encode()) > BROADCAST_DATA_MAX:
        return json_error(f"data must serialise to at most {BROADCAST_DATA_MAX} bytes", 400)


    # STEP 3: register the job, audit it, and hand the Expo
    # round-trips to a background thread once the audit commits
    # =========================================================
    job_id = str(uuid.uuid4())

    write_audit(request.user["id"], "notification.broadcast", job_id, {"title": title})

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

    transaction.on_commit(lambda: _spawn_broadcast(job_id, title, body_text, extra_data))

    return json_response(job, status=202)


@require_methods("GET")
@require_role("admin")
def broadcast_job_status(request, job_id):
    job = _broadcast_job(job_id)
    if job is None:
        return json_error("Broadcast job not found", 404)
    return json_response(job)








############################################################
# list_reports / resolve_report
############################################################
#
# The complaint ledger (POST /api/social/reports), newest
# first, with the reporter's display name — and, for 'user'
# targets, the target's name too (one bulk fetch), so the
# row renders without a second lookup. No status filter
# means open only: the panel's job is the queue, the
# archive is opt-in. Capped at 200.
# The PUT moves a report between 'open' and 'resolved' —
# reopening is allowed, a resolve tapped by mistake must be
# reversible. Audited like every other privileged write.
#
# Used by:
#   - the admin panel's reports view
############################################################

@require_methods("GET")
@require_role("admin", "curator")
def list_reports(request):
    status = clean_param(request.GET.get("status", "open"))
    if status not in ("open", "resolved"):
        return json_error("status must be one of: open, resolved", 400)

    rows = list(
        Report.objects.filter(status=status)
        .select_related("reporter")
        .order_by("-created_at")[:200]
    )

    # The names behind 'user' targets, one query for the page
    target_ids = {r.target_id for r in rows if r.target_type == "user"}
    target_names = dict(User.objects.filter(id__in=target_ids).values_list("id", "display_name")) if target_ids else {}

    return json_response({
        "reports": [
            {
                "id": r.id,
                "reporterId": r.reporter_id,
                "reporterName": r.reporter.display_name,
                "targetType": r.target_type,
                "targetId": r.target_id,
                "targetUserName": target_names.get(r.target_id) if r.target_type == "user" else None,
                "reason": r.reason,
                "status": r.status,
                "createdAt": r.created_at,
            }
            for r in rows
        ]
    })


@require_methods("PUT")
@require_role("admin", "curator")
def resolve_report(request, report_id):
    # STEP 1: the body — a known status
    # =================================
    data = get_json_object(request)
    new_status = data.get("status") if data else None
    if new_status not in ("open", "resolved"):
        return json_error("status must be one of: open, resolved", 400)


    # STEP 2: the row must exist; then one write + the audit
    # ======================================================
    row = Report.objects.filter(id=report_id).values("status").first()
    if not row:
        return json_error("Report not found", 404)

    Report.objects.filter(id=report_id).update(status=new_status)
    write_audit(request.user["id"], "report.status", report_id,
                {"from": row["status"], "to": new_status})

    return json_response({"status": new_status})








############################################################
# list_audit
############################################################
#
# GET /api/admin/audit
#
# The trail every mutating handler here writes, newest
# first, admin-only — this is the record of who wielded the
# console, so curators reading it would see admin actions
# they cannot see the subjects of. Same optional
# ?limit/?offset pair as the sibling listings. The payload
# is a JSON column, so it arrives from the ORM as the
# structure write_audit stored (or None) and is served
# directly. Reading the trail deliberately leaves no trail
# of its own.
#
# Used by:
#   - the admin panel's audit view
############################################################

@require_methods("GET")
@require_role("admin")
def list_audit(request):
    limit, offset, error = _pagination_clause(request)
    if error:
        return json_error(error, 400)

    rows = _page(
        AdminAudit.objects.select_related("actor").order_by("-created_at"),
        limit, offset,
    )

    return json_response({
        "audit": [
            {
                "id": r.id,
                "actorId": r.actor_id,
                "actorName": r.actor.display_name if r.actor else None,
                "action": r.action,
                "target": r.target,
                "payload": r.payload,
                "createdAt": r.created_at,
            }
            for r in rows
        ]
    })








############################################################
# list_uploads
############################################################
#
# GET /api/admin/uploads
#
# The whole ownership ledger, newest first, admin-only: one
# row per stored file with its owner's name (None for the
# ownerless rows an erasure leaves behind — exactly the
# files an admin wants to find and sweep). The url field is
# ready to open (the file GET is public), and the existing
# owner-or-admin DELETE /api/uploads/<filename> removes a
# row from here. Same optional ?limit/?offset pair as the
# sibling listings.
#
# Used by:
#   - the admin panel's stored-files view
############################################################

@require_methods("GET")
@require_role("admin")
def list_uploads(request):
    limit, offset, error = _pagination_clause(request)
    if error:
        return json_error(error, 400)

    rows = _page(
        Upload.objects.select_related("user").order_by("-created_at"),
        limit, offset,
    )

    return json_response({
        "uploads": [
            {
                "id": r.id,
                "filename": r.filename,
                "url": f"/api/uploads/{r.filename}",
                "userId": r.user_id,
                "userName": r.user.display_name if r.user else None,
                "size": r.byte_size,
                "createdAt": r.created_at,
            }
            for r in rows
        ]
    })








############################################################
# get_reported_message
############################################################
#
# GET /api/admin/messages/<message_id>
#
# The moderation window into chat: every other message
# read is membership-scoped, so without this route a report
# would hand the queue nothing but an id. Admin AND curator —
# the same pair that reads the report queue — may fetch the
# reported message itself: sender, room, text and the
# attachment facts, with deleted/edited stamps included (an
# unsent message is often exactly what was reported). A
# soft-deleted row still answers here on purpose. Reads are
# not audited anywhere in this module; this one is no
# exception.
#
# Used by:
#   - the admin panel's report details view
############################################################

@require_methods("GET")
@require_role("admin", "curator")
def get_reported_message(request, message_id):
    row = (
        Message.objects.select_related("sender", "conversation")
        .filter(id=message_id)
        .first()
    )
    if row is None:
        return json_error("Message not found", 404)

    return json_response({
        "id": row.id,
        "conversationId": row.conversation_id,
        "conversationType": row.conversation.type,
        "senderId": row.sender_id,
        "senderName": row.sender.display_name,
        "text": row.text,
        "kind": row.kind,
        "imageUrl": row.image_url,
        "attachmentName": row.attachment_name,
        "deletedAt": row.deleted_at,
        "editedAt": row.edited_at,
        "createdAt": row.created_at,
    })








############################################################
# list_tombstones / restore_tombstone
############################################################
#
# GET  /api/admin/tombstones
# POST /api/admin/tombstones/restore
#
# The scraper skip-list: deleting a scraped post tombstones
# its source_url so the next run cannot resurrect it. The
# listing is the table verbatim (admin-only, newest first,
# the sibling ?limit/?offset pair). The restore lifts one
# tombstone by exact source_url — the article returns on
# the scraper's next pass over a page that still carries it;
# nothing is re-fetched eagerly. Restoring is a privileged
# write, so it is audited; a url that is not tombstoned is
# a 404, and restoring twice answers exactly that.
#
# Used by:
#   - the admin panel's deleted-sources view
############################################################

@require_methods("GET")
@require_role("admin")
def list_tombstones(request):
    limit, offset, error = _pagination_clause(request)
    if error:
        return json_error(error, 400)

    rows = _page(
        DeletedSourceUrl.objects.select_related("deleted_by").order_by("-deleted_at"),
        limit, offset,
    )

    return json_response({
        "tombstones": [
            {
                "sourceUrl": r.source_url,
                "deletedAt": r.deleted_at,
                "deletedById": r.deleted_by_id,
                "deletedByName": r.deleted_by.display_name if r.deleted_by else None,
            }
            for r in rows
        ]
    })


@require_methods("POST")
@require_role("admin")
def restore_tombstone(request):
    data = get_json_object(request)
    source_url = data.get("source_url") if data else None
    if not isinstance(source_url, str) or not source_url.strip():
        return json_error("source_url is required", 400)

    deleted, _ = DeletedSourceUrl.objects.filter(source_url=source_url).delete()
    if deleted == 0:
        return json_error("Tombstone not found", 404)

    write_audit(request.user["id"], "tombstone.restore", source_url)
    return json_response({"status": "restored"})
