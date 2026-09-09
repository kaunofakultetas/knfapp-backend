############################################################
#  [*] Social API — profiles, friendships, walls, blocks
#
#  The community half of the app: the shared feed of wall
#  posts, public profiles with the viewer-side
#  friendship status, the friend-request state machine
#  (pending → accept/auto-accept/reject-or-cancel, then one
#  friendships row PER DIRECTION), the caller's own wall
#  CRUD, blocks (one-directional rows, bidirectional
#  effect), the report ledger and the activity list. Paths,
#  bodies, statuses and slugs are the frozen wire contract
#  (swagger/swagger.yaml).
#
#  Wall posts live in news_posts — likes/comments/polls
#  belong to the news app; this one only creates, edits and
#  deletes the row and reads news_likes for the flag. A
#  deactivated account drops out of everything here.
#
#  Split into:
#
#    social_feed / get_user_posts    — the ranked wall reads
#    get_profile / get_own_profile / update_profile
#    send_friend_request / list_friend_requests /
#    accept_friend_request / reject_friend_request /
#    list_friends / unfriend
#    create_post / update_post / delete_post
#    block_user / unblock_user / list_blocks
#    create_report
#    list_activity / mark_activity_read / activity_unread_count
############################################################


import logging
import uuid
from datetime import datetime, timedelta, timezone


from django.db import IntegrityError, models, transaction
from django.db.models.functions import Coalesce, Greatest, Least


from knfapp.chat.models import Message
from knfapp.common import ratelimit
from knfapp.common.expressions import JulianDay, JulianDayNow
from knfapp.common.http import get_json_object, json_error, json_response, parse_pagination
from knfapp.common.timestamps import as_aware, utc_now
from knfapp.news.core import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    SUMMARY_LENGTH,
    as_utc,
    parse_iso,
)
from knfapp.news.models import NewsLike, NewsPost
from knfapp.social.activity import drop_activity, record_activity
from knfapp.social.models import Activity, FriendRequest, Friendship, Report, UserBlock
from knfapp.uploads.storage import delete_upload
from knfapp.users.auth import get_current_user, require_auth
from knfapp.users.models import User


logger = logging.getLogger(__name__)

# Feed bounds — the ranked window keeps the sort on the live
# part of the wall (a profile's own list is NOT windowed, so
# nothing becomes unreachable), and the page caps keep the
# OFFSET scans bounded
FEED_WINDOW_DAYS = 180
FEED_MAX_PAGE = 200
LIST_MAX_PAGE = 200
FEED_PER_PAGE_MAX = 50

# Friends / requests come in one generous page: the app sends
# no pagination and counts the rows it gets
LIST_PER_PAGE = 200

# Friend-request quotas: sends per 5-minute window, and the
# post-decline cooldown before the same sender may re-ask
FRIEND_REQUEST_MAX = 20
FRIEND_REQUEST_COOLDOWN_DAYS = 7

ACTIVITY_PER_PAGE = 30

# The community ranking — recency decay + capped engagement,
# NO source bonus (walls only rank against walls). Each term
# is floored to 0 on its own, matching the news formula's
# spirit but not its grouping. One ORM annotation; the
# vendor-split JulianDay expressions carry the engine
# spelling.
def _wall_score(ref):
    recency = Coalesce(
        models.ExpressionWrapper(
            1.0 / (1.0 + Greatest(models.Value(0.0), ref - JulianDay(models.F("published_at")))) * 100.0,
            output_field=models.FloatField(),
        ),
        models.Value(0.0),
    )
    engagement = Coalesce(
        models.ExpressionWrapper(
            Least(
                models.F("likes_count") + models.F("comments_count") * 2 + models.F("shares_count") * 3,
                models.Value(100),
            ) * 0.5,
            output_field=models.FloatField(),
        ),
        models.Value(0.0),
    )
    return models.ExpressionWrapper(recency + engagement, output_field=models.FloatField())

# The report whitelist: the model a target_type is checked
# against — always this map's, never the request's
REPORT_TARGET_MODELS = {"user": User, "post": NewsPost, "message": Message}








############################################################
# _post_row_to_dict / _parse_before / _blocked_pair
############################################################
#
# The one wire shape every post-serving route here answers
# (author = the CURRENT display name, snapshot as fallback;
# truncate=True trims list bodies to SUMMARY_LENGTH with the
# additive "truncated" flag); the ?before pin with its
# edge-of-calendar 400; and the either-direction block test
# the request and chat paths share.
#
# Used by:
#   - social_feed, get_user_posts, create_post (below)
#   - send_friend_request (below) — the block gate
############################################################

def _post_row_to_dict(row, truncate=False):
    content = row["content"] or ""
    truncated = truncate and len(content) > SUMMARY_LENGTH
    return {
        "id": row["id"],
        "title": row["title"],
        "content": content[:SUMMARY_LENGTH] if truncated else content,
        "summary": row["summary"],
        "imageUrl": row["image_url"],
        "author": row["author_display_name"] or row["author_name"],
        "authorId": row["author_id"],
        "authorAvatar": row["author_avatar"],
        "source": row["source"],
        "sourceUrl": row["source_url"],
        "postType": row["post_type"],
        "likes": row["likes_count"],
        "comments": row["comments_count"],
        "shares": row["shares_count"],
        "date": row["published_at"],
        "isPublic": bool(row["is_public"]),
        "liked": False,
        "truncated": truncated,
    }


def _parse_before(request):
    raw = request.GET.get("before")
    if raw is None:
        return None, None
    pinned = as_utc(parse_iso(raw.replace("Z", "+00:00")))
    if pinned is None:
        return None, json_error("before must be an ISO-8601 timestamp", 400, code="invalid_before")
    return pinned, None


def _blocked_pair(a, b):
    return UserBlock.objects.filter(
        models.Q(blocker_id=a, blocked_id=b) | models.Q(blocker_id=b, blocked_id=a),
    ).exists()


_POST_ROW_FIELDS = (
    "id", "title", "content", "summary", "image_url", "author_id", "author_name",
    "source", "source_url", "post_type", "likes_count", "comments_count",
    "shares_count", "published_at", "is_public", "author_avatar", "author_display_name",
)


def _attach_liked(posts, user):
    if not user or not posts:
        return
    liked_set = set(
        NewsLike.objects.filter(user_id=user["id"], post_id__in=[p["id"] for p in posts])
        .values_list("post_id", flat=True)
    )
    for p in posts:
        p["liked"] = p["id"] in liked_set








############################################################
# social_feed
############################################################
#
# GET /api/social/feed — the community feed: wall posts only
# (source 'user'), ranked by recency + engagement over the
# last FEED_WINDOW_DAYS. Logged in: own + friends' posts at
# ANY visibility plus every public wall post; anonymous:
# public only. A deactivated author's posts are nobody's.
# The optional ?before pins the formula's "now" AND caps
# published_at, so a mid-paging insert cannot shift the
# OFFSET window.
#
# Used by:
#   - services/api/social.ts fetchSocialFeed — the news
#     tab's "community" chip, guests included
############################################################

def social_feed(request):
    # STEP 1: pagination (its own caps), the pin, the viewer
    # ======================================================
    page, per_page, err = parse_pagination(request, max_per_page=FEED_PER_PAGE_MAX, max_page=FEED_MAX_PAGE)
    if err:
        return err
    offset = (page - 1) * per_page

    before, err = _parse_before(request)
    if err:
        return err

    user = get_current_user(request)


    # STEP 2: visibility filter — the active-author and window
    # floors everybody pays, then the viewer's slice. The
    # window bound is computed HERE, floored to midnight UTC of
    # the cutoff day so any stamp on that day stays inside the
    # window — an ordinary typed range on the stamp column
    # ========================================================
    window_floor = (datetime.now(timezone.utc) - timedelta(days=FEED_WINDOW_DAYS)) \
        .replace(hour=0, minute=0, second=0, microsecond=0)
    visibility = (
        models.Q(source="user")
        & (models.Q(author__active=1) | models.Q(author__isnull=True))
        & models.Q(published_at__gt=window_floor)
    )

    if user:
        friend_ids = list(Friendship.objects.filter(user_id=user["id"]).values_list("friend_id", flat=True))
        visible_ids = [user["id"]] + friend_ids
        visibility &= models.Q(author_id__in=visible_ids) | models.Q(is_public=1)
    else:
        visibility &= models.Q(is_public=1)

    if before:
        visibility &= models.Q(published_at__lte=before)


    # STEP 3: the ranked page, the author joined in
    # =============================================
    ref = JulianDay(models.Value(before)) if before else JulianDayNow()
    base = NewsPost.objects.filter(visibility)
    rows = list(
        base.annotate(
            feed_score=_wall_score(ref),
            author_avatar=models.F("author__avatar_url"),
            author_display_name=models.F("author__display_name"),
        )
        .order_by("-feed_score", "-published_at", "-id")
        .values(*[f.attname for f in NewsPost._meta.concrete_fields],
                "author_avatar", "author_display_name")[offset:offset + per_page]
    )
    total = base.count()


    # STEP 4: the wire shape (list bodies trimmed) + liked flags
    # ==========================================================
    posts = [_post_row_to_dict(row, truncate=True) for row in rows]
    _attach_liked(posts, user)

    return json_response({
        "posts": posts,
        "page": page,
        "perPage": per_page,
        "total": total,
        "hasMore": offset + per_page < total,
    })








############################################################
# get_profile / get_own_profile / update_profile
############################################################
#
# The public profile (postCount under the same visibility
# split as the post list, friendCount without deactivated
# accounts, friendshipStatus from the VIEWER's side,
# blockedByMe so the client can offer "unblock"), the
# unused private twin, and the profile editor the app
# actually calls — whose display-name change rewrites the
# author_name snapshots in the same transaction and whose
# replaced own-upload avatar is deleted after the commit.
#
# Used by:
#   - services/api/social.ts fetchUserProfile /
#     updateProfile — the profile screen and the id card
############################################################

def get_profile(request, user_id):
    user = User.objects.filter(id=user_id).values(
        "id", "username", "display_name", "avatar_url", "role", "created_at", "active",
    ).first()
    if not user:
        return json_error("User not found", 404)

    viewer = get_current_user(request)

    # A deactivated account reads as gone; an admin keeps seeing it
    if not user["active"] and not (viewer and viewer["role"] == "admin"):
        return json_error("User not found", 404)

    is_friend = False
    if viewer and viewer["id"] != user_id:
        is_friend = Friendship.objects.filter(user_id=viewer["id"], friend_id=user_id).exists()

    # Same rule as get_user_posts: the author and accepted friends
    # see private posts — and 'faculty' rows count alongside 'user'
    can_see_private = bool(viewer) and (viewer["id"] == user_id or is_friend)
    post_query = NewsPost.objects.filter(author_id=user_id, source__in=("user", "faculty"))
    if not can_see_private:
        post_query = post_query.filter(is_public=1)
    post_count = post_query.count()

    friend_count = Friendship.objects.filter(user_id=user_id, friend__active=1).count()

    blocked_by_me = False
    if viewer and viewer["id"] != user_id:
        blocked_by_me = UserBlock.objects.filter(blocker_id=viewer["id"], blocked_id=user_id).exists()

    # A pending request in either direction decides sent/received
    friendship_status = "none"
    if viewer and viewer["id"] != user_id:
        if is_friend:
            friendship_status = "friends"
        else:
            pending = FriendRequest.objects.filter(status="pending").filter(
                models.Q(from_user_id=viewer["id"], to_user_id=user_id)
                | models.Q(from_user_id=user_id, to_user_id=viewer["id"]),
            ).values("from_user_id").first()
            if pending:
                friendship_status = "request_sent" if pending["from_user_id"] == viewer["id"] else "request_received"

    return json_response({
        "id": user["id"],
        "username": user["username"],
        "displayName": user["display_name"],
        "avatarUrl": user["avatar_url"],
        "role": user["role"],
        "createdAt": user["created_at"],
        "postCount": post_count,
        "friendCount": friend_count,
        "friendshipStatus": friendship_status,
        "blockedByMe": blocked_by_me,
    })


@require_auth
def get_own_profile(request):
    user = request.user
    row = User.objects.filter(id=user["id"]).values("created_at").first()
    post_count = NewsPost.objects.filter(author_id=user["id"], source__in=("user", "faculty")).count()
    friend_count = Friendship.objects.filter(user_id=user["id"], friend__active=1).count()

    return json_response({
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "displayName": user["display_name"],
        "avatarUrl": user["avatar_url"],
        "role": user["role"],
        "createdAt": row["created_at"] if row else None,
        "postCount": post_count,
        "friendCount": friend_count,
        "studentNumber": user.get("student_number"),
        "studyGroup": user.get("study_group"),
        "studyProgram": user.get("study_program"),
    })


@require_auth
@ratelimit.per_user("profile", max_attempts=30)
def update_profile(request):
    # STEP 1: body — an array or scalar is a 400, never a 500
    # =======================================================
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)


    # STEP 2: collect the fields present — camelCase wins
    # ===================================================
    updates = {}
    new_display_name = None
    replaced_avatar = None

    dn_key = "displayName" if "displayName" in data else "display_name"
    if dn_key in data:
        if not isinstance(data[dn_key], str):
            return json_error("display_name must be a string", 400)
        display_name = data[dn_key].strip()
        if not display_name:
            return json_error("Display name cannot be empty", 400)
        if len(display_name) > 100:
            return json_error("Display name must be at most 100 characters", 400)
        updates["display_name"] = display_name
        new_display_name = display_name

    av_key = "avatarUrl" if "avatarUrl" in data else "avatar_url"
    if av_key in data:
        # Own uploads or clearing only — a foreign host would
        # beacon every avatar render to whoever the user picked
        av = data[av_key]
        if av not in (None, "") and (not isinstance(av, str) or not av.startswith("/api/uploads/")):
            return json_error("avatar_url must be a relative /api/uploads/ path", 400)
        updates["avatar_url"] = av
        old_avatar = request.user.get("avatar_url")
        if old_avatar and old_avatar != av:
            replaced_avatar = old_avatar

    for camel, snake, column in [
        ("studentNumber", "student_number", "student_number"),
        ("studyGroup", "study_group", "study_group"),
        ("studyProgram", "study_program", "study_program"),
    ]:
        field = camel if camel in data else snake
        if field in data:
            val = data[field]
            if val is not None:
                if not isinstance(val, str):
                    return json_error(f"{field} must be a string", 400)
                val = val.strip()
                if len(val) > 50:
                    return json_error(f"{field} must be at most 50 characters", 400)
                if not val:
                    val = None
            updates[column] = val

    if not updates:
        return json_error("No fields to update", 400)


    # STEP 3: one UPDATE, the snapshots with it, then re-read
    # =======================================================
    updates["updated_at"] = utc_now()
    User.objects.filter(id=request.user["id"]).update(**updates)
    if new_display_name:
        # Same transaction as the rename — posts never show a
        # half-renamed author
        NewsPost.objects.filter(author_id=request.user["id"]).update(author_name=new_display_name)

    user = User.objects.filter(id=request.user["id"]).values().first()

    # The commit drops the replaced file's last reference either
    # way — the cleanup runs even on the session-dead exit below
    if replaced_avatar and replaced_avatar.startswith("/api/uploads/"):
        transaction.on_commit(lambda: delete_upload(replaced_avatar))

    if user is None:
        return json_error("Authentication required", 401)

    return json_response({
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "displayName": user["display_name"],
        "avatarUrl": user["avatar_url"],
        "role": user["role"],
        "invited": bool(user["invited"]),
        "studentNumber": user["student_number"],
        "studyGroup": user["study_group"],
        "studyProgram": user["study_program"],
    })








############################################################
# send_friend_request
############################################################
#
# POST /api/social/friends/request — body {"user_id"}. A
# pending row, unless the target already asked US — then
# their request is accepted on the spot (both friendships
# rows, their row deleted). A blocked pair (either
# direction) reads as the same 404 an unknown id gets. Two
# brakes on spam: the per-sender rate limit and the
# post-decline per-pair cooldown (429
# friend_request_cooldown), whose expired rows are purged
# opportunistically here. The partial unique index settles
# a lost race as the same 409.
#
# Used by:
#   - services/api/social.ts sendFriendRequest — the
#     profile's "add friend" action
############################################################

@require_auth
@ratelimit.per_user("friendreq", max_attempts=FRIEND_REQUEST_MAX)
def send_friend_request(request):
    # STEP 1: body + the type and self-request guards
    # ===============================================
    data = get_json_object(request)
    if not data or not data.get("user_id"):
        return json_error("user_id required", 400)

    target_id = data["user_id"]
    my_id = request.user["id"]
    if not isinstance(target_id, str):
        return json_error("user_id must be a string", 400)
    if target_id == my_id:
        return json_error("Cannot friend yourself", 400)


    # STEP 2: target exists, is active, is not blocked either way,
    # is not already a friend
    # ============================================================
    target = User.objects.filter(id=target_id).values("id", "active").first()
    if not target or not target["active"]:
        return json_error("User not found", 404)
    if _blocked_pair(my_id, target_id):
        # Whether and why a request cannot be delivered is not the
        # requester's business
        return json_error("User not found", 404)
    if Friendship.objects.filter(user_id=my_id, friend_id=target_id).exists():
        return json_error("Already friends", 409)


    # STEP 3: a pending row in either direction — theirs is the
    # handshake completing, ours a duplicate
    # =========================================================
    pending = FriendRequest.objects.filter(status="pending").filter(
        models.Q(from_user_id=my_id, to_user_id=target_id)
        | models.Q(from_user_id=target_id, to_user_id=my_id),
    ).values("id", "from_user_id").first()

    if pending:
        if pending["from_user_id"] == target_id:
            since = utc_now()
            # get_or_create: a mutual send can leave one direction
            # already written, and the composite PK must not 500
            Friendship.objects.get_or_create(user_id=my_id, friend_id=target_id,
                                             defaults={"created_at": since})
            Friendship.objects.get_or_create(user_id=target_id, friend_id=my_id,
                                             defaults={"created_at": since})
            FriendRequest.objects.filter(id=pending["id"]).delete()
            # Friendship outranks a decline — a stale rejection must
            # not fire the cooldown after a later unfriend
            FriendRequest.objects.filter(status="rejected").filter(
                models.Q(from_user_id=my_id, to_user_id=target_id)
                | models.Q(from_user_id=target_id, to_user_id=my_id),
            ).delete()
            record_activity(target_id, "connect_accept", my_id)
            drop_activity(my_id, "connect_request", target_id, pending["id"])
            return json_response({"status": "accepted",
                                  "message": "Friend request auto-accepted (they already requested you)"})
        return json_error("Friend request already pending", 409)


    # STEP 3.1: the rejection cooldown + the purge of rows past it
    # ============================================================
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRIEND_REQUEST_COOLDOWN_DAYS)
    recently_rejected = FriendRequest.objects.filter(
        status="rejected", from_user_id=my_id, to_user_id=target_id,
    ).annotate(
        settled_at=models.functions.Coalesce("updated_at", "created_at"),
    ).filter(settled_at__gt=cutoff).exists()
    if recently_rejected:
        return json_error("This person declined your last request. Please try again later.",
                          429, code="friend_request_cooldown")

    FriendRequest.objects.filter(status="rejected").annotate(
        settled_at=models.functions.Coalesce("updated_at", "created_at"),
    ).filter(settled_at__lte=cutoff).delete()


    # STEP 4: the fresh pending row — the partial unique index
    # settles a lost race as the same 409
    # ========================================================
    req_id = str(uuid.uuid4())
    now = utc_now()
    try:
        with transaction.atomic():
            FriendRequest.objects.create(id=req_id, from_user_id=my_id, to_user_id=target_id,
                                         created_at=now, updated_at=now)
    except IntegrityError:
        return json_error("Friend request already pending", 409)
    record_activity(target_id, "connect_request", my_id, req_id)

    return json_response({"id": req_id, "status": "pending"}, status=201)








############################################################
# list_friend_requests / accept / reject / list_friends /
# unfriend
############################################################
#
# The handshake's read and settle sides. Accept (recipient
# only) writes BOTH friendships rows and deletes the
# request plus every stale rejection between the pair;
# reject settles differently per side — the recipient's
# decline is the cooldown record, the sender's cancel
# deletes the row (a withdrawal is not a rejection). The
# friends list leaves deactivated accounts out and sorts as
# alphabetically as the engine can; unfriend clears BOTH
# directions and 404s only when nothing matched.
#
# Used by:
#   - services/api/social.ts — the friends screens and the
#     profile action button
############################################################

@require_auth
def list_friend_requests(request):
    direction = request.GET.get("direction", "received")
    if direction not in ("sent", "received"):
        return json_error("direction must be 'sent' or 'received'", 400, code="invalid_direction")

    page, per_page, err = parse_pagination(request, max_per_page=LIST_PER_PAGE,
                                           default_per_page=LIST_PER_PAGE, max_page=LIST_MAX_PAGE)
    if err:
        return err
    offset = (page - 1) * per_page

    if direction == "sent":
        base = FriendRequest.objects.filter(from_user_id=request.user["id"], status="pending",
                                            to_user__active=1)
        other = "to_user"
    else:
        base = FriendRequest.objects.filter(to_user_id=request.user["id"], status="pending",
                                            from_user__active=1)
        other = "from_user"

    rows = base.annotate(
        other_id=models.F(f"{other}__id"),
        display_name=models.F(f"{other}__display_name"),
        username=models.F(f"{other}__username"),
        avatar_url=models.F(f"{other}__avatar_url"),
        role=models.F(f"{other}__role"),
    ).order_by("-created_at", "-id").values(
        "id", "other_id", "created_at", "display_name", "username", "avatar_url", "role",
    )[offset:offset + per_page]

    requests_list = [
        {
            "id": r["id"],
            "userId": r["other_id"],
            "displayName": r["display_name"],
            "username": r["username"],
            "avatarUrl": r["avatar_url"],
            "role": r["role"],
            "createdAt": r["created_at"],
        }
        for r in rows
    ]
    total = base.count()

    return json_response({"requests": requests_list, "total": total,
                          "hasMore": offset + per_page < total})


@require_auth
@ratelimit.per_user("friendaction", max_attempts=60)
def accept_friend_request(request, request_id):
    fr = FriendRequest.objects.filter(id=request_id, to_user_id=request.user["id"],
                                      status="pending").values("id", "from_user_id", "to_user_id").first()
    if not fr:
        return json_error("Friend request not found", 404)

    # Both directions — every reader only checks its own; the
    # get_or_create keeps a mutual-send leftover idempotent
    since = utc_now()
    Friendship.objects.get_or_create(user_id=fr["from_user_id"], friend_id=fr["to_user_id"],
                                     defaults={"created_at": since})
    Friendship.objects.get_or_create(user_id=fr["to_user_id"], friend_id=fr["from_user_id"],
                                     defaults={"created_at": since})
    # The handshake is over; the friendships rows carry it now —
    # and so does any earlier decline between the two
    FriendRequest.objects.filter(id=request_id).delete()
    FriendRequest.objects.filter(status="rejected").filter(
        models.Q(from_user_id=fr["from_user_id"], to_user_id=fr["to_user_id"])
        | models.Q(from_user_id=fr["to_user_id"], to_user_id=fr["from_user_id"]),
    ).delete()
    record_activity(fr["from_user_id"], "connect_accept", request.user["id"])
    drop_activity(fr["to_user_id"], "connect_request", fr["from_user_id"], request_id)

    return json_response({"status": "accepted"})


@require_auth
@ratelimit.per_user("friendaction", max_attempts=60)
def reject_friend_request(request, request_id):
    fr = FriendRequest.objects.filter(id=request_id, status="pending").filter(
        models.Q(to_user_id=request.user["id"]) | models.Q(from_user_id=request.user["id"]),
    ).values("id", "from_user_id", "to_user_id").first()
    if not fr:
        return json_error("Friend request not found", 404)

    if fr["from_user_id"] == request.user["id"]:
        # A cancel, not a rejection — no cooldown record
        FriendRequest.objects.filter(id=request_id).delete()
    else:
        FriendRequest.objects.filter(id=request_id).update(status="rejected", updated_at=utc_now())
    # Withdrawn or declined, the ask leaves the activity list
    drop_activity(fr["to_user_id"], "connect_request", fr["from_user_id"], request_id)

    return json_response({"status": "rejected"})


@require_auth
def list_friends(request):
    page, per_page, err = parse_pagination(request, max_per_page=LIST_PER_PAGE,
                                           default_per_page=LIST_PER_PAGE, max_page=LIST_MAX_PAGE)
    if err:
        return err
    offset = (page - 1) * per_page

    base = Friendship.objects.filter(user_id=request.user["id"], friend__active=1)
    rows = base.annotate(
        username=models.F("friend__username"),
        display_name=models.F("friend__display_name"),
        avatar_url=models.F("friend__avatar_url"),
        role=models.F("friend__role"),
        # Lower() folds ASCII case only on SQLite; real
        # Lithuanian collation lives client-side
        sort_name=models.functions.Lower("friend__display_name"),
    ).order_by("sort_name", "friend_id").values(
        "friend_id", "username", "display_name", "avatar_url", "role", "created_at",
    )[offset:offset + per_page]

    friends = [
        {
            "id": r["friend_id"],
            "username": r["username"],
            "displayName": r["display_name"],
            "avatarUrl": r["avatar_url"],
            "role": r["role"],
            "friendsSince": r["created_at"],
        }
        for r in rows
    ]
    total = base.count()

    return json_response({"friends": friends, "total": total, "hasMore": offset + per_page < total})


@require_auth
@ratelimit.per_user("friendaction", max_attempts=60)
def unfriend(request, user_id):
    my_id = request.user["id"]
    deleted, _ = Friendship.objects.filter(
        models.Q(user_id=my_id, friend_id=user_id) | models.Q(user_id=user_id, friend_id=my_id),
    ).delete()
    if deleted == 0:
        return json_error("Not friends", 404)
    return json_response({"status": "unfriended"})








############################################################
# create_post / get_user_posts / update_post / delete_post
############################################################
#
# The caller's wall CRUD over news_posts rows. Creation is
# always source 'user' / type 'social' (the news route maps
# staff to 'faculty'); reads cover 'user' AND 'faculty' so
# a staff profile lists its announcements; ownership
# answers 404 (never 403); an edit never touches
# published_at, so it cannot re-rank the feed; the delete
# trusts the FK cascade and takes the own-upload cover with
# it after the commit.
#
# Used by:
#   - services/api/social.ts fetchUserPosts / updatePost /
#     deletePost — the profile list and the edit/delete menu
############################################################

@require_auth
@ratelimit.per_user("post", max_attempts=20)
def create_post(request):
    # STEP 1: content — a non-string is 400, empty is 400
    # ===================================================
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    raw_content = data.get("content")
    if raw_content is not None and not isinstance(raw_content, str):
        return json_error("content must be a string", 400)
    content = (raw_content or "").strip()
    if not content:
        return json_error("Post content required", 400)

    raw_title = data.get("title")
    if raw_title is not None and not isinstance(raw_title, str):
        return json_error("title must be a string", 400)
    title = (raw_title or "").strip() or content[:80]

    if len(title) > MAX_TITLE_LENGTH:
        return json_error(f"Title must be at most {MAX_TITLE_LENGTH} characters", 400)
    if len(content) > MAX_CONTENT_LENGTH:
        return json_error(f"Content must be at most {MAX_CONTENT_LENGTH} characters", 400)

    image_url = data.get("image_url")
    is_public = data.get("is_public", True)
    if not isinstance(is_public, bool):
        return json_error("is_public must be a boolean", 400)
    # Absent, null and "" are the only non-paths that pass — a
    # falsy non-string ([], False) is a 400, never an unbindable
    # INSERT or an off-contract echo
    if image_url not in (None, "") and (not isinstance(image_url, str) or not image_url.startswith("/api/uploads/")):
        return json_error("image_url must be a relative /api/uploads/ path", 400)


    # STEP 2: insert, then answer the re-read row through the one
    # producer every read path uses
    # ===========================================================
    post_id = str(uuid.uuid4())
    now = utc_now()
    NewsPost.objects.create(
        id=post_id, title=title, content=content, summary=content[:SUMMARY_LENGTH],
        image_url=image_url,
        author_id=request.user["id"], author_name=request.user["display_name"],
        source="user", source_url=None, post_type="social",
        is_public=is_public, published_at=now, created_at=now, updated_at=now,
    )

    row = NewsPost.objects.filter(id=post_id).annotate(
        author_avatar=models.F("author__avatar_url"),
        author_display_name=models.F("author__display_name"),
    ).values(*_POST_ROW_FIELDS).first()
    return json_response(_post_row_to_dict(row), status=201)


def get_user_posts(request):
    # STEP 1: user_id, pagination, optional viewer
    # ============================================
    user_id = request.GET.get("user_id")
    if not user_id:
        return json_error("user_id query param required", 400)

    page, per_page, err = parse_pagination(request, max_per_page=FEED_PER_PAGE_MAX, max_page=LIST_MAX_PAGE)
    if err:
        return err
    offset = (page - 1) * per_page

    viewer = get_current_user(request)


    # STEP 2: target exists and is active (admins still see);
    # private rows only for self or a friend
    # =======================================================
    target = User.objects.filter(id=user_id).values("id", "active").first()
    if not target:
        return json_error("User not found", 404)
    if not target["active"] and not (viewer and viewer["role"] == "admin"):
        return json_error("User not found", 404)

    can_see_private = False
    if viewer:
        can_see_private = viewer["id"] == user_id or Friendship.objects.filter(
            user_id=viewer["id"], friend_id=user_id,
        ).exists()


    # STEP 3: the page, newest first, id breaking ties
    # ================================================
    base = NewsPost.objects.filter(author_id=user_id, source__in=("user", "faculty"))
    if not can_see_private:
        base = base.filter(is_public=1)

    rows = base.annotate(
        author_avatar=models.F("author__avatar_url"),
        author_display_name=models.F("author__display_name"),
    ).order_by("-published_at", "-id").values(*_POST_ROW_FIELDS)[offset:offset + per_page]

    posts = [_post_row_to_dict(row, truncate=True) for row in rows]
    _attach_liked(posts, viewer)
    total = base.count()

    return json_response({"posts": posts, "page": page, "perPage": per_page,
                          "total": total, "hasMore": offset + per_page < total})


@require_auth
@ratelimit.per_user("post", max_attempts=20)
def update_post(request, post_id):
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    # Someone else's post reads as missing — 404, never 403
    post = NewsPost.objects.filter(id=post_id, author_id=request.user["id"],
                                   source__in=("user", "faculty")).values("id", "content").first()
    if not post:
        return json_error("Post not found or not yours", 404)

    updates = {}
    content = None
    if "content" in data:
        if not isinstance(data["content"], str):
            return json_error("content must be a string", 400)
        content = data["content"].strip()
        if not content:
            return json_error("Post content required", 400)
        if len(content) > MAX_CONTENT_LENGTH:
            return json_error(f"Content must be at most {MAX_CONTENT_LENGTH} characters", 400)
        updates["content"] = content
        updates["summary"] = content[:SUMMARY_LENGTH]
    if "title" in data:
        if not isinstance(data["title"], str):
            return json_error("title must be a string", 400)
        # A blank title becomes the head of the body being stored,
        # never ""
        title = data["title"].strip() or (content or post["content"] or "")[:80]
        if len(title) > MAX_TITLE_LENGTH:
            return json_error(f"Title must be at most {MAX_TITLE_LENGTH} characters", 400)
        updates["title"] = title
    if "image_url" in data:
        iv = data["image_url"]
        if iv not in (None, "") and (not isinstance(iv, str) or not iv.startswith("/api/uploads/")):
            return json_error("image_url must be a relative /api/uploads/ path", 400)
        updates["image_url"] = iv

    if not updates:
        return json_error("No fields to update", 400)

    # published_at untouched — an edit never re-ranks the feed
    updates["updated_at"] = utc_now()
    NewsPost.objects.filter(id=post_id).update(**updates)
    return json_response({"status": "updated"})


@require_auth
@ratelimit.per_user("post_delete", max_attempts=40)
def delete_post(request, post_id):
    post = NewsPost.objects.filter(id=post_id, author_id=request.user["id"],
                                   source__in=("user", "faculty")).values("id", "image_url").first()
    if not post:
        return json_error("Post not found or not yours", 404)

    # The FK cascade takes likes, comments and polls with it
    NewsPost.objects.filter(id=post_id).delete()

    image_url = post["image_url"]
    if isinstance(image_url, str) and image_url.startswith("/api/uploads/"):
        transaction.on_commit(lambda: delete_upload(image_url))

    return json_response({"status": "deleted"})








############################################################
# block_user / unblock_user / list_blocks
############################################################
#
# One row blocker→blocked, bidirectional in effect at every
# enforcement site. Blocking severs the friendship (both
# rows) and any pending request in the same transaction —
# "blocked but still friends" is not a state anyone means.
# Repeat blocks and unknown unblocks are 200s (idempotent
# taps); unblocking restores nothing. The list keeps
# deactivated accounts — the block outlives the account.
#
# Used by:
#   - services/api/social.ts blockUser / unblockUser /
#     fetchBlockedUsers — the profile's block actions
############################################################

@require_auth
@ratelimit.per_user("block", max_attempts=60)
def block_user(request):
    data = get_json_object(request)
    if not data or not data.get("user_id"):
        return json_error("user_id required", 400)

    target_id = data["user_id"]
    my_id = request.user["id"]
    if not isinstance(target_id, str):
        return json_error("user_id must be a string", 400)
    if target_id == my_id:
        return json_error("Cannot block yourself", 400)

    if not User.objects.filter(id=target_id, active=1).exists():
        return json_error("User not found", 404)

    UserBlock.objects.get_or_create(blocker_id=my_id, blocked_id=target_id,
                                    defaults={"created_at": utc_now()})
    Friendship.objects.filter(
        models.Q(user_id=my_id, friend_id=target_id) | models.Q(user_id=target_id, friend_id=my_id),
    ).delete()
    FriendRequest.objects.filter(status="pending").filter(
        models.Q(from_user_id=my_id, to_user_id=target_id)
        | models.Q(from_user_id=target_id, to_user_id=my_id),
    ).delete()

    return json_response({"status": "blocked"})


@require_auth
@ratelimit.per_user("block", max_attempts=60)
def unblock_user(request, user_id):
    UserBlock.objects.filter(blocker_id=request.user["id"], blocked_id=user_id).delete()
    return json_response({"status": "unblocked"})


@require_auth
def list_blocks(request):
    rows = UserBlock.objects.filter(blocker_id=request.user["id"]).annotate(
        username=models.F("blocked__username"),
        display_name=models.F("blocked__display_name"),
        avatar_url=models.F("blocked__avatar_url"),
        role=models.F("blocked__role"),
    ).order_by("-created_at").values(
        "blocked_id", "username", "display_name", "avatar_url", "role", "created_at",
    )

    return json_response({
        "blocked": [
            {
                "id": r["blocked_id"],
                "username": r["username"],
                "displayName": r["display_name"],
                "avatarUrl": r["avatar_url"],
                "role": r["role"],
                "blockedAt": r["created_at"],
            }
            for r in rows
        ]
    })








############################################################
# create_report
############################################################
#
# POST /api/social/reports — {target_type, target_id,
# reason}. The target must exist in the model its type
# names (the whitelist map, never the request); a report
# is never anonymous to the admins, only to the reported.
# An unknown type is the 400 naming the three valid ones,
# a missing target the plain 404.
#
# Used by:
#   - services/api/social.ts reportTarget — the profile's
#     report action
############################################################

@require_auth
@ratelimit.per_user("report", max_attempts=20)
def create_report(request):
    # STEP 1: a known type, a non-empty id, a bounded reason
    # ======================================================
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    target_type = data.get("target_type")
    if target_type not in REPORT_TARGET_MODELS:
        return json_error("target_type must be one of: user, post, message", 400)

    target_id = data.get("target_id")
    if not isinstance(target_id, str) or not target_id.strip():
        return json_error("target_id required", 400)
    target_id = target_id.strip()

    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return json_error("reason required", 400)
    reason = reason.strip()
    if len(reason) > 1000:
        return json_error("reason must be at most 1000 characters", 400)


    # STEP 2: the target must exist in the model its type
    # names — the whitelist map's, never the request's
    # ===================================================
    if not REPORT_TARGET_MODELS[target_type].objects.filter(id=target_id).exists():
        return json_error("Report target not found", 404)


    # STEP 3: the row
    # ===============
    report_id = str(uuid.uuid4())
    Report.objects.create(id=report_id, reporter_id=request.user["id"], target_type=target_type,
                          target_id=target_id, reason=reason, created_at=utc_now())
    return json_response({"status": "submitted", "id": report_id}, status=201)








############################################################
# list_activity / mark_activity_read / activity_unread_count
############################################################
#
# The in-app notification list, keyset-paged ("<created_at>
# |<id>", opaque to clients; malformed input reads as the
# first page — the cursor is ours), the read-all flip and
# the cheap badge COUNT. Deactivated actors still show —
# their gesture happened; deleted ones are gone with the
# cascade.
#
# Used by:
#   - @knf/socialengine's adapter — the activity screen and
#     the unread badge
############################################################

@require_auth
def list_activity(request):
    cursor_param = request.GET.get("cursor", "")
    before, before_id = None, None
    if cursor_param and "|" in cursor_param:
        # parse_iso repairs what a query string does to a stamp
        # (the '+' of the offset arrives as a space)
        raw_before, before_id = cursor_param.split("|", 1)
        before = as_utc(parse_iso(raw_before))

    base = Activity.objects.filter(user_id=request.user["id"])
    if before and before_id:
        base = base.filter(
            models.Q(created_at__lt=before) | models.Q(created_at=before, id__lt=before_id),
        )

    rows = list(base.annotate(
        actor_name=models.F("actor__display_name"),
        actor_avatar=models.F("actor__avatar_url"),
    ).order_by("-created_at", "-id").values(
        "id", "kind", "subject_id", "subject_preview", "created_at", "read",
        "actor_id", "actor_name", "actor_avatar",
    )[:ACTIVITY_PER_PAGE + 1])

    has_more = len(rows) > ACTIVITY_PER_PAGE
    page = rows[:ACTIVITY_PER_PAGE]

    notifications = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "actor": {
                "id": r["actor_id"],
                "displayName": r["actor_name"],
                "avatarUrl": r["actor_avatar"],
            },
            "createdAt": r["created_at"],
            "read": bool(r["read"]),
            "subjectId": r["subject_id"],
            "subjectPreview": r["subject_preview"],
        }
        for r in page
    ]
    next_cursor = f'{page[-1]["created_at"].isoformat()}|{page[-1]["id"]}' if page and has_more else None
    return json_response({
        "notifications": notifications,
        "hasMore": has_more,
        **({"cursor": next_cursor} if next_cursor else {}),
    })


@require_auth
def mark_activity_read(request):
    Activity.objects.filter(user_id=request.user["id"], read=0).update(read=1)
    return json_response({"status": "ok"})


@require_auth
def activity_unread_count(request):
    count = Activity.objects.filter(user_id=request.user["id"], read=0).count()
    return json_response({"count": count})
