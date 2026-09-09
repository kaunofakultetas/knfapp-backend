############################################################
#  [*] News API — the feed and everything hanging off it
#
#  The ranked unified feed with its weak-ETag 304 path,
#  post create/read/delete, the like toggle and share
#  counter, the comment thread, and the poll lifecycle —
#  behind the frozen wire contract, plus the two
#  invariants every write here honours: counters are
#  RECOMPUTED from child rows (never ±1), and a hidden post
#  answers the same 404 as a missing one.
#
#  Split into:
#
#    get_feed / create_post          — the collection
#    get_post / delete_post          — one post
#    toggle_like / share_post        — the counters
#    get_comments / add_comment / delete_comment
#    get_poll / create_poll / delete_poll / vote_poll
############################################################


import hashlib
import logging
import uuid
from datetime import datetime, timezone


from django.db import IntegrityError, models, transaction
from django.db.models.functions import Coalesce, Greatest, Least
from django.http import HttpResponse
from django.views.decorators.http import require_POST


from knfapp.common import ratelimit
from knfapp.common.expressions import JulianDay, JulianDayNow
from knfapp.common.http import client_ip, get_json_object, json_error, json_response, parse_pagination
from knfapp.common.timestamps import utc_now
from knfapp.news import core
from knfapp.news.models import (
    SCRAPED_SOURCES,
    SOURCES,
    DeletedSourceUrl,
    NewsComment,
    NewsLike,
    NewsPost,
    Poll,
    PollOption,
    PollVote,
)
from knfapp.social.activity import drop_activity, record_activity
from knfapp.social.models import Friendship
from knfapp.uploads.storage import delete_upload
from knfapp.users.auth import get_current_user, require_auth
from knfapp.users.models import User


logger = logging.getLogger(__name__)

# post_type values a CLIENT may send — 'poll' is excluded, so
# no one can mint a poll card with no poll behind it
CLIENT_POST_TYPES = ("article", "social", "announcement", "link")

# The ranking, as one ORM annotation: recency decays on the
# Julian day (the vendor-split JulianDay expressions carry
# the engine spelling), engagement is capped so a viral post
# cannot pin the feed, and the source bonus keeps faculty
# word above the scrapers. COALESCE floors the whole score
# at 0 when the recency term goes NULL over an unparsable
# stamp.
def _feed_score(ref):
    recency = 1.0 / (1.0 + Greatest(models.Value(0.0), ref - JulianDay(models.F("published_at")))) * 100.0
    engagement = Least(
        models.F("likes_count") + models.F("comments_count") * 2 + models.F("shares_count") * 3,
        models.Value(100),
    ) * 0.5
    bonus = models.Case(
        models.When(source="faculty", then=models.Value(20.0)),
        models.When(source="knf.vu.lt", then=models.Value(15.0)),
        models.When(source="vu.lt", then=models.Value(10.0)),
        models.When(source="app", then=models.Value(5.0)),
        default=models.Value(0.0),
        output_field=models.FloatField(),
    )
    return Coalesce(
        models.ExpressionWrapper(recency + engagement + bonus, output_field=models.FloatField()),
        models.Value(0.0),
    )


def _gate_row(post_id):
    return NewsPost.objects.filter(id=post_id).values(*core.GATE_FIELDS).first()


def _post_row(post_id):
    # POST_FIELDS plus the live author name, LEFT-JOINed so a
    # post whose author was erased still answers
    return (
        NewsPost.objects.filter(id=post_id)
        .annotate(live_author_name=models.F("author__display_name"))
        .values(*core.POST_FIELDS, "live_author_name")
        .first()
    )


def _cacheable(response, tag, shared):
    response["ETag"] = f'W/"{tag}"'
    response["Cache-Control"] = f"{'public' if shared else 'private'}, max-age={core.FEED_CACHE_MAX_AGE}"
    return response








############################################################
# get_feed
############################################################
#
# GET /api/news — the unified feed, one ranked page at a
# time: ?page/?per_page, an optional ?source out of SOURCES
# and an optional ?before window pin (rows published after
# it stay out of every page of the run, so a scraper insert
# mid-paging cannot shift the OFFSET window). Guests see
# public non-wall rows; members add their own rows and
# their friends' wall posts, and non-staff never see a
# private faculty draft. The ranking runs on a NARROW
# id-only query; only the page of ids is joined out to
# full rows. Every answer carries a weak ETag over the
# feed fingerprint plus the caller, their friend set and
# the query — a 304 is decided before any ranking work.
#
# Used by:
#   - services/api/news.ts fetchNewsFeed — the news tab's
#     source chips map straight onto ?source
############################################################

def get_feed(request):
    # STEP 1: pagination, the ?source whitelist, the ?before pin
    # ==========================================================
    page, per_page, err = parse_pagination(request)
    if err:
        return err

    source_filter = request.GET.get("source")
    if source_filter is not None and source_filter not in SOURCES:
        return json_error(f"source must be one of: {', '.join(SOURCES)}", 400)

    before = request.GET.get("before")
    if before is not None:
        pinned = core.as_utc(core.parse_iso(before))
        if pinned is None:
            return json_error("before must be an ISO-8601 timestamp", 400)
        before = pinned

    offset = (page - 1) * per_page
    user = get_current_user(request)


    # STEP 2: the visibility filter, as composable Q objects
    # ======================================================
    visibility = models.Q()

    if source_filter:
        visibility &= models.Q(source=source_filter)
    if before:
        visibility &= models.Q(published_at__lte=before)

    friend_ids = []
    if not user:
        visibility &= models.Q(is_public=1) & ~models.Q(source="user")
    else:
        friend_ids = list(
            Friendship.objects.filter(user_id=user["id"]).values_list("friend_id", flat=True)
        )
        visible_ids = [user["id"]] + friend_ids
        if not source_filter or source_filter == "user":
            visibility &= ~models.Q(source="user") | models.Q(author_id__in=visible_ids)
        if user["role"] not in core.STAFF_ROLES:
            visibility &= (models.Q(is_public=1) | models.Q(source="user")
                           | models.Q(author_id=user["id"]))


    # STEP 3: the ETag seed — fingerprint + caller + friend set +
    # query; a matching If-None-Match ends the request here
    # ===========================================================
    seed = "|".join((
        core.feed_version(),
        user["id"] if user else "guest",
        hashlib.sha256(",".join(sorted(friend_ids)).encode("utf-8")).hexdigest()[:16] if user else "-",
        str(page), str(per_page), source_filter or "-", core.feed_stamp(before),
    ))
    tag = core.etag_for(seed)

    if core.if_none_match_contains(request.headers.get("If-None-Match"), tag):
        return _cacheable(HttpResponse(status=304), tag, user is None)


    # STEP 4: the ranked ids — narrow on purpose
    # ==========================================
    ref = JulianDay(models.Value(before)) if before else JulianDayNow()
    post_ids = list(
        NewsPost.objects.filter(visibility)
        .annotate(feed_score=_feed_score(ref))
        .order_by("-feed_score", "-published_at", "-id")
        .values_list("id", flat=True)[offset:offset + per_page]
    )
    total = NewsPost.objects.filter(visibility).count()


    # STEP 5: the page rows by id, back into ranked order
    # ===================================================
    posts = []
    if post_ids:
        by_id = {
            row["id"]: row for row in
            NewsPost.objects.filter(id__in=post_ids)
            .annotate(live_author_name=models.F("author__display_name"))
            .values(*core.POST_FIELDS, "live_author_name")
        }
        posts = [core.post_to_dict(by_id[pid]) for pid in post_ids if pid in by_id]


    # STEP 6: the caller's like flags, one IN query per page
    # ======================================================
    liked_set = set()
    if user and post_ids:
        liked_set = set(
            NewsLike.objects.filter(user_id=user["id"], post_id__in=post_ids)
            .values_list("post_id", flat=True)
        )
    for p in posts:
        p["liked"] = p["id"] in liked_set


    # STEP 7: poll cards ship their poll inline — three batched
    # queries for the page
    # =========================================================
    poll_ids = [p["id"] for p in posts if p["postType"] == "poll"]
    if poll_ids:
        polls = core.polls_for_posts(poll_ids, user["id"] if user else None)
        for p in posts:
            if p["id"] in polls:
                p["poll"] = polls[p["id"]]

    return _cacheable(json_response({
        "posts": posts,
        "page": page,
        "perPage": per_page,
        "total": total,
        "hasMore": offset + per_page < total,
    }), tag, user is None)








############################################################
# create_post
############################################################
#
# POST /api/news — {content, title?, post_type?, image_url?,
# is_public?}. No role gate: the role picks the SOURCE —
# staff publish as 'faculty' (default type 'announcement'),
# everyone else as 'user' (default 'social'). Every input is
# typed before SQL; post_type is whitelisted with 'poll'
# excluded; is_public must be a real boolean; image_url must
# be a relative /api/uploads/ path (a foreign host would
# beacon every reader to an attacker-chosen server). The 201
# re-reads the row through post_to_dict + liked=False, and a
# public faculty post rings the 'news' push channel (import-
# guarded; a push failure never fails the 201).
#
# Used by:
#   - services/api/news.ts createPost — the create-post
#     screen; a poll follows via create_poll
############################################################

@require_POST
@require_auth
@ratelimit.per_user("news_post", max_attempts=20)
def create_post(request):
    # STEP 1: body checks — content and title typed and capped
    # ========================================================
    data = get_json_object(request)
    if not data:
        return json_error("JSON object body required", 400)

    raw_content = data.get("content")
    if raw_content is not None and not isinstance(raw_content, str):
        return json_error("content must be a string", 400)
    content = (raw_content or "").strip()
    if not content:
        return json_error("Content required", 400)

    raw_title = data.get("title")
    if raw_title is not None and not isinstance(raw_title, str):
        return json_error("title must be a string", 400)
    title = (raw_title or "").strip() or content[:80]

    if len(title) > core.MAX_TITLE_LENGTH:
        return json_error(f"Title must be at most {core.MAX_TITLE_LENGTH} characters", 400)
    if len(content) > core.MAX_CONTENT_LENGTH:
        return json_error(f"Content must be at most {core.MAX_CONTENT_LENGTH} characters", 400)


    # STEP 2: the role picks the source; post_type whitelisted,
    # is_public a real boolean, image_url own-uploads-or-absent
    # =========================================================
    role = request.user["role"]
    post_type = data.get("post_type")
    image_url = data.get("image_url")
    is_public = data.get("is_public", True)

    if post_type and post_type not in CLIENT_POST_TYPES:
        return json_error(f"post_type must be one of: {', '.join(CLIENT_POST_TYPES)}", 400)
    if not isinstance(is_public, bool):
        return json_error("is_public must be a boolean", 400)
    if image_url is not None and not isinstance(image_url, str):
        return json_error("image_url must be a relative /api/uploads/ path", 400)
    if image_url and not image_url.startswith("/api/uploads/"):
        return json_error("image_url must be a relative /api/uploads/ path", 400)

    if role in core.STAFF_ROLES:
        source = "faculty"
        post_type = post_type or "announcement"
    else:
        source = "user"
        post_type = post_type or "social"


    # STEP 3: insert, then re-read so the 201 carries exactly the
    # shape every read path serves
    # ===========================================================
    post_id = str(uuid.uuid4())
    now = utc_now()
    NewsPost.objects.create(
        id=post_id, title=title, content=content, summary=content[:core.SUMMARY_LENGTH],
        image_url=image_url, author_id=request.user["id"], author_name=request.user["display_name"],
        source=source, source_url=None, post_type=post_type,
        is_public=is_public, published_at=now, created_at=now, updated_at=now,
    )
    logger.info("News post %s created by %s (source=%s, post_type=%s, public=%s)",
                post_id, request.user["id"], source, post_type, is_public)
    body = {**core.post_to_dict(_post_row(post_id)), "liked": False}


    # STEP 4: a public faculty announcement rings the 'news'
    # channel — the import is guarded so the route stands even
    # without the push module; a failure is logged, never a 500
    # =========================================================
    if source == "faculty" and is_public:
        try:
            from knfapp.notifications.push import notify_channel
        except ImportError:
            notify_channel = None
        if notify_channel:
            try:
                notify_channel("news", title, content[:core.SUMMARY_LENGTH],
                               data={"type": "news", "source": "faculty", "postId": post_id},
                               exclude_user_id=request.user["id"])
            except Exception:
                logger.exception("Failed to push the new faculty post %s on the 'news' channel", post_id)

    return json_response(body, status=201)








############################################################
# get_post / delete_post
############################################################
#
# GET serves one visible post (404 for missing AND hidden —
# existence never leaks) with the viewer's liked flag and
# the additive poll object. DELETE is author-or-admin;
# scraped articles get their source_url tombstoned first so
# the scrapers cannot resurrect them, dependants go before
# the row (belt and braces beside the FK cascade), and the
# cover upload is deleted only after the commit.
#
# Used by:
#   - services/api/news.ts fetchNewsPost (the detail screen);
#     DELETE is swagger-documented for moderation
############################################################

def get_post(request, post_id):
    user = get_current_user(request)
    row = _post_row(post_id)
    if not row or not core.can_view_post(row, user):
        return json_error("Post not found", 404)

    body = core.post_to_dict(row)
    body["liked"] = bool(user) and NewsLike.objects.filter(user_id=user["id"], post_id=post_id).exists()

    if row["post_type"] == "poll":
        poll = core.polls_for_posts([post_id], user["id"] if user else None).get(post_id)
        if poll:
            body["poll"] = poll

    return json_response(body)


@require_auth
@ratelimit.per_user("news_delete", max_attempts=60)
def delete_post(request, post_id):
    # STEP 1: exists + author-or-admin
    # ================================
    post = NewsPost.objects.filter(id=post_id).values(
        "id", "author_id", "source", "source_url", "image_url",
    ).first()
    if not post:
        return json_error("Post not found", 404)

    user = request.user
    if post["author_id"] != user["id"] and user["role"] != "admin":
        return json_error("Only the post author or an admin can delete this post", 403)


    # STEP 2: tombstone a scraped URL so the next tick cannot
    # resurrect the article
    # =======================================================
    if post["source_url"]:
        DeletedSourceUrl.objects.get_or_create(
            source_url=post["source_url"],
            defaults={"deleted_by_id": user["id"], "deleted_at": utc_now()},
        )


    # STEP 3: dependants first, then the row — one transaction
    # (ATOMIC_REQUESTS) for the lot
    # ========================================================
    NewsLike.objects.filter(post_id=post_id).delete()
    NewsComment.objects.filter(post_id=post_id).delete()
    for poll_id in Poll.objects.filter(post_id=post_id).values_list("id", flat=True):
        PollVote.objects.filter(poll_id=poll_id).delete()
        PollOption.objects.filter(poll_id=poll_id).delete()
        Poll.objects.filter(id=poll_id).delete()
    NewsPost.objects.filter(id=post_id).delete()


    # STEP 4: the cover file, after the delete is definitely in —
    # only our own uploads; a failure never fails the response
    # ==========================================================
    image_url = post["image_url"]
    if isinstance(image_url, str) and image_url.startswith("/api/uploads/"):
        transaction.on_commit(lambda: delete_upload(image_url))

    if post["author_id"] == user["id"]:
        logger.info("News post %s deleted by its author %s", post_id, user["id"])
    else:
        logger.warning("News post %s (author %s, source %s) deleted by admin %s",
                       post_id, post["author_id"], post["source"], user["id"])

    return json_response({"status": "deleted"})








############################################################
# toggle_like / share_post
############################################################
#
# The like flips the caller's news_likes row on a post they
# may SEE (get_or_create absorbs the concurrent-toggle PK
# race) and RECOMPUTES likes_count from the rows; the
# author's activity row rides the same
# transaction — a like lands one, an unlike takes it back.
# The share bumps shares_count with no auth (guests share
# too) but the same visibility gate — a 200-vs-404 split on
# a private post would leak its existence. Both re-read the
# counter after the write so the reply carries the real
# number, and a post deleted in that window answers the
# same 404, never a crash.
#
# Used by:
#   - services/api/news.ts toggleLikeApi / sharePostApi
############################################################

@require_POST
@require_auth
@ratelimit.per_user("news_like", max_attempts=300)
def toggle_like(request, post_id):
    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, request.user):
        return json_error("Post not found", 404)

    _, created = NewsLike.objects.get_or_create(
        user_id=request.user["id"], post_id=post_id,
        defaults={"created_at": utc_now()},
    )
    if created:
        liked = True
    else:
        NewsLike.objects.filter(user_id=request.user["id"], post_id=post_id).delete()
        liked = False

    # Recomputed from the rows, not ±1 — a drifted counter heals
    NewsPost.objects.filter(id=post_id).update(
        likes_count=NewsLike.objects.filter(post_id=post_id).count(),
    )

    author = NewsPost.objects.filter(id=post_id).values("author_id", "title").first()
    if author:
        if liked:
            record_activity(author["author_id"], "like", request.user["id"],
                            post_id, (author["title"] or "")[:80] or None)
        else:
            drop_activity(author["author_id"], "like", request.user["id"], post_id)

    fresh = NewsPost.objects.filter(id=post_id).values("likes_count").first()
    if not fresh:
        return json_error("Post not found", 404)
    return json_response({"liked": liked, "likes": fresh["likes_count"]})


@require_POST
def share_post(request, post_id):
    # Optional caller — what the gate needs to tell a friend's
    # private wall post from a stranger's
    user = get_current_user(request)

    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, user):
        return json_error("Post not found", 404)

    NewsPost.objects.filter(id=post_id).update(shares_count=models.F("shares_count") + 1)

    fresh = NewsPost.objects.filter(id=post_id).values("shares_count").first()
    if not fresh:
        return json_error("Post not found", 404)
    return json_response({"shares": fresh["shares_count"]})








############################################################
# get_comments / add_comment / delete_comment
############################################################
#
# The thread under one visible post (the parent gates
# every route — the thread is exactly as private as its
# post). Pages are newest-first with id breaking
# created_at ties, the users JOIN is LEFT so an orphaned
# comment still counts AND renders ('Deleted user'), and
# stamps go out through to_utc_iso. add_comment
# recomputes comments_count in its transaction and answers
# the row it wrote with ONE clock read; delete_comment is
# comment-author / post-author / admin, and the comment
# must belong to the post in the path.
#
# Used by:
#   - services/api/news.ts fetchComments / addCommentApi;
#     the delete is swagger-documented moderation
############################################################

def get_comments(request, post_id):
    page, per_page, err = parse_pagination(request)
    if err:
        return err
    offset = (page - 1) * per_page

    user = get_current_user(request)
    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, user):
        return json_error("Post not found", 404)

    rows = (
        NewsComment.objects.filter(post_id=post_id)
        .annotate(display_name=models.F("user__display_name"), avatar_url=models.F("user__avatar_url"))
        .order_by("-created_at", "-id")
        .values("id", "text", "created_at", "user_id", "display_name", "avatar_url")[offset:offset + per_page]
    )

    comments = [
        {
            "id": r["id"],
            "text": r["text"],
            "time": core.to_utc_iso(r["created_at"]),
            "userName": r["display_name"] or "Deleted user",
            "userAvatar": r["avatar_url"],
            "userId": r["user_id"],
        }
        for r in rows
    ]
    total = NewsComment.objects.filter(post_id=post_id).count()

    return json_response({"comments": comments, "total": total, "page": page, "perPage": per_page})


@require_auth
@ratelimit.per_user("news_comment", max_attempts=60)
def add_comment(request, post_id):
    data = get_json_object(request)
    if not data or not isinstance(data.get("text"), str) or not data["text"].strip():
        return json_error("Comment text required", 400)
    comment_text = data["text"].strip()
    if len(comment_text) > core.MAX_COMMENT_LENGTH:
        return json_error(f"Comment must be at most {core.MAX_COMMENT_LENGTH} characters", 400)

    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, request.user):
        return json_error("Post not found", 404)

    comment_id = str(uuid.uuid4())
    now = utc_now()
    NewsComment.objects.create(id=comment_id, post_id=post_id, user_id=request.user["id"],
                               text=comment_text, created_at=now)
    NewsPost.objects.filter(id=post_id).update(
        comments_count=NewsComment.objects.filter(post_id=post_id).count(),
    )
    # The author hears about it; the POST id keys the row, so
    # repeat comments refresh one row with the newest excerpt
    record_activity(post["author_id"], "comment", request.user["id"], post_id, comment_text[:80])

    return json_response({
        "id": comment_id,
        "text": comment_text,
        "time": now,
        "userName": request.user["display_name"],
        "userAvatar": request.user.get("avatar_url"),
        "userId": request.user["id"],
    }, status=201)


@require_auth
@ratelimit.per_user("news_comment_delete", max_attempts=60)
def delete_comment(request, post_id, comment_id):
    # STEP 1: the post gates the thread, then the comment must be
    # one of ITS comments
    # ===========================================================
    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, request.user):
        return json_error("Post not found", 404)

    comment = NewsComment.objects.filter(id=comment_id, post_id=post_id).values("id", "user_id").first()
    if not comment:
        return json_error("Comment not found", 404)


    # STEP 2: comment author, post author or admin
    # ============================================
    user = request.user
    if (comment["user_id"] != user["id"] and post["author_id"] != user["id"] and user["role"] != "admin"):
        return json_error("Only the comment author, the post author or an admin can delete this comment", 403)


    # STEP 3: delete and recompute from the rows
    # ==========================================
    NewsComment.objects.filter(id=comment_id).delete()
    count = NewsComment.objects.filter(post_id=post_id).count()
    NewsPost.objects.filter(id=post_id).update(comments_count=count)

    logger.info("Comment %s on post %s deleted by %s", comment_id, post_id, user["id"])
    return json_response({"status": "deleted", "comments": count})








############################################################
# get_poll / create_poll / delete_poll / vote_poll
############################################################
#
# The poll lifecycle under a visible post (the same 404 for
# missing, hidden and poll-less — the mobile fetchPoll turns
# exactly that into null). create is author-or-admin, one
# poll per post (the unique constraint answers the racing
# twin the same 409), scraped articles refused, end_date
# normalised to explicit UTC at the door. delete restores
# the post_type its source implies. vote casts or moves via
# an ON CONFLICT upsert; re-voting the held option is the
# 409 the client treats as a no-op; the option counters are
# RECOMPUTED from the rows (the poll total is derived from
# them at shape time, never stored).
#
# Used by:
#   - services/api/news.ts fetchPoll / createPollApi /
#     votePollApi (PollWidget); delete is swagger-documented
############################################################

def get_poll(request, post_id):
    user = get_current_user(request)
    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, user):
        return json_error("No poll found for this post", 404)

    poll = Poll.objects.filter(post_id=post_id).values(
        "id", "post_id", "title", "end_date", "created_at",
    ).first()
    if not poll:
        return json_error("No poll found for this post", 404)
    return json_response(core.poll_to_dict(poll, user["id"] if user else None))


@require_POST
@require_auth
@ratelimit.per_user("news_poll", max_attempts=20)
def create_poll(request, post_id):
    # STEP 1: body — a non-blank capped title
    # =======================================
    data = get_json_object(request)
    if not data:
        return json_error("JSON object body required", 400)

    raw_title = data.get("title")
    if raw_title is not None and not isinstance(raw_title, str):
        return json_error("title must be a string", 400)
    title = (raw_title or "").strip()
    if not title:
        return json_error("Poll title required", 400)
    if len(title) > core.MAX_TITLE_LENGTH:
        return json_error(f"Poll title must be at most {core.MAX_TITLE_LENGTH} characters", 400)


    # STEP 2: the options — a list of strings, blanks stripped
    # BEFORE the 2..10 count check
    # ========================================================
    raw_options = data.get("options", [])
    if not isinstance(raw_options, list) or any(not isinstance(o, str) for o in raw_options):
        return json_error("options must be an array of strings", 400)
    options = [o.strip() for o in raw_options if o.strip()]
    if len(options) < core.MIN_POLL_OPTIONS:
        return json_error(f"At least {core.MIN_POLL_OPTIONS} options required", 400)
    if len(options) > core.MAX_POLL_OPTIONS:
        return json_error(f"Maximum {core.MAX_POLL_OPTIONS} options allowed", 400)
    if any(len(o) > core.MAX_POLL_OPTION_LENGTH for o in options):
        return json_error(f"Each option must be at most {core.MAX_POLL_OPTION_LENGTH} characters", 400)


    # STEP 3: the optional end date — parsed AND moved onto UTC
    # here, so the vote gate never has to guess
    # =========================================================
    end_date = data.get("end_date")
    if end_date is not None:
        pinned = core.as_utc(core.parse_iso(end_date))
        if pinned is None:
            return json_error("end_date must be an ISO-8601 timestamp", 400)
        end_date = pinned


    # STEP 4: visible + owned (or admin) + not scraped + not
    # already polled
    # ======================================================
    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, request.user):
        return json_error("Post not found", 404)

    user = request.user
    if post["author_id"] != user["id"] and user["role"] != "admin":
        return json_error("Only the post author or admin can create a poll", 403)
    if post["source"] in SCRAPED_SOURCES:
        return json_error("A scraped article cannot carry a poll", 400)
    if Poll.objects.filter(post_id=post_id).exists():
        return json_error("Post already has a poll", 409)


    # STEP 5: the poll row — the unique constraint is the real
    # guard, its IntegrityError answering the racing twin's 409
    # =========================================================
    poll_id = str(uuid.uuid4())
    now = utc_now()
    try:
        with transaction.atomic():
            Poll.objects.create(id=poll_id, post_id=post_id, title=title, end_date=end_date, created_at=now)
    except IntegrityError:
        logger.warning("Concurrent poll creation on post %s lost the race", post_id)
        return json_error("Post already has a poll", 409)

    for position, opt_text in enumerate(options):
        PollOption.objects.create(id=str(uuid.uuid4()), poll_id=poll_id, text=opt_text, position=position)


    # STEP 6: the post becomes a 'poll' post, and the 201 carries
    # the fresh poll (userVote None)
    # ===========================================================
    NewsPost.objects.filter(id=post_id).update(post_type="poll")
    logger.info("Poll %s (%d options) attached to post %s by %s", poll_id, len(options), post_id, user["id"])

    poll = Poll.objects.filter(id=poll_id).values(
        "id", "post_id", "title", "end_date", "created_at",
    ).first()
    return json_response(core.poll_to_dict(poll, user["id"]), status=201)


@require_auth
@ratelimit.per_user("news_poll", max_attempts=20)
def delete_poll(request, post_id):
    # STEP 1: the post gates the poll, then author-or-admin
    # =====================================================
    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, request.user):
        return json_error("Post not found", 404)

    user = request.user
    if post["author_id"] != user["id"] and user["role"] != "admin":
        return json_error("Only the post author or admin can delete this poll", 403)

    poll_ids = list(Poll.objects.filter(post_id=post_id).values_list("id", flat=True))
    if not poll_ids:
        return json_error("No poll found for this post", 404)


    # STEP 2: votes and options before the poll rows, then the
    # post_type its source implies
    # ========================================================
    PollVote.objects.filter(poll_id__in=poll_ids).delete()
    PollOption.objects.filter(poll_id__in=poll_ids).delete()
    Poll.objects.filter(id__in=poll_ids).delete()

    if post["source"] == "user":
        restored = "social"
    elif post["source"] in SCRAPED_SOURCES:
        restored = "article"
    else:
        restored = "announcement"
    NewsPost.objects.filter(id=post_id).update(post_type=restored)

    logger.info("Poll on post %s deleted by %s, post_type restored to %s", post_id, user["id"], restored)
    return json_response({"status": "deleted", "postType": restored})


@require_POST
@require_auth
@ratelimit.per_user("news_vote", max_attempts=120)
def vote_poll(request, post_id):
    # STEP 1: option_id must be a non-blank string
    # ============================================
    data = get_json_object(request)
    if not data:
        return json_error("JSON object body required", 400)
    option_id = data.get("option_id")
    if not isinstance(option_id, str) or not option_id.strip():
        return json_error("option_id required", 400)
    option_id = option_id.strip()
    user_id = request.user["id"]


    # STEP 2: the post gates its poll
    # ===============================
    post = _gate_row(post_id)
    if not post or not core.can_view_post(post, request.user):
        return json_error("No poll found for this post", 404)

    poll = Poll.objects.filter(post_id=post_id).values("id", "end_date").first()
    if not poll:
        return json_error("No poll found for this post", 404)
    poll_id = poll["id"]


    # STEP 3: the end-date gate, aware to aware; unparseable
    # means "open", logged
    # ======================================================
    if poll["end_date"]:
        end = core.parse_iso(poll["end_date"])
        if end is None:
            logger.warning("Poll %s has an unparseable end_date %r — treated as still open",
                           poll_id, poll["end_date"])
        elif datetime.now(timezone.utc) > end:
            return json_error("Poll has ended", 400)


    # STEP 4: the option must belong to THIS poll
    # ===========================================
    if not PollOption.objects.filter(id=option_id, poll_id=poll_id).exists():
        return json_error("Invalid option", 400)


    # STEP 5: cast or move — the held option answers the 409 the
    # client treats as a no-op; the write is an ON CONFLICT
    # upsert on the (user, poll) PK, so a racing pair cannot
    # collide or double-count (the counters are recomputed
    # either way). unique_fields must name the PK's COMPONENT
    # columns — ["pk"] raises on a composite-PK model
    # ==========================================================
    existing = PollVote.objects.filter(user_id=user_id, poll_id=poll_id).values("option_id").first()
    if existing and existing["option_id"] == option_id:
        return json_error("Already voted for this option", 409)

    PollVote.objects.bulk_create(
        [PollVote(user_id=user_id, poll_id=poll_id, option_id=option_id, created_at=utc_now())],
        update_conflicts=True,
        unique_fields=["user_id", "poll_id"],
        update_fields=["option_id", "created_at"],
    )


    # STEP 6: counters recomputed from the rows, never ±1 —
    # COALESCE lands a zero-vote option on 0, not NULL
    # =====================================================
    PollOption.objects.filter(poll_id=poll_id).update(
        votes=Coalesce(
            models.Subquery(
                PollVote.objects.filter(option_id=models.OuterRef("id"))
                .values("option_id").annotate(c=models.Count("option_id")).values("c")[:1],
                output_field=models.IntegerField(),
            ),
            models.Value(0),
        ),
    )


    # STEP 7: the fresh poll state
    # ============================
    fresh = Poll.objects.filter(id=poll_id).values(
        "id", "post_id", "title", "end_date", "created_at",
    ).first()
    return json_response(core.poll_to_dict(fresh, user_id))
