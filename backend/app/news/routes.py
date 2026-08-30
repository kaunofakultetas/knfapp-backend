############################################################
#  [*] News — unified feed, posts, likes, comments, polls
#
#  One news_posts table carries everything the feed shows:
#  articles the scrapers pull in (source 'knf.vu.lt' /
#  'vu.lt', author_id NULL), staff posts ('faculty') and
#  member wall posts ('user' — the very rows social/routes.py
#  serves under /api/social/posts and /api/social/feed).
#  Polls hang off a post (polls / poll_options / poll_votes);
#  likes and comments keep denormalised counters on the post
#  row (likes_count / comments_count) next to their own
#  tables.
#
#  Works without login: the reads call get_current_user()
#  optionally (guests get public rows and liked=False), the
#  writes sit behind require_auth plus auth's shared
#  rate_limit decorator. Nothing is escaped on the way IN —
#  text is stored raw and the after_request hook in
#  app/__init__.py html-escapes every string in the JSON on
#  the way out (the v1/v2 migration history in
#  database/__init__.py explains why).
#
#  Visibility is ONE predicate — _can_view_post — and every
#  read AND write here runs it before answering, so a private
#  post's comments, poll and votes are exactly as closed as
#  the post itself. A failed gate is always a 404, never a
#  403: existence must not leak.
#
#  Timestamps this file writes come from
#  database.utc_now_iso() (aware UTC, ISO-8601 T-form with
#  the +00:00 offset — migration v17 normalised the legacy
#  space-form rows written by the column DEFAULTs to the same
#  shape), so the julianday() ranking, the string cursors and
#  the poll end-date compare all agree and clients see ONE
#  shape per field. The DEFAULTs stay as never-firing
#  backstops.
#
#    GET    /api/news                            — ranked feed page
#    POST   /api/news                            — create a post
#    GET    /api/news/<post_id>                  — one post
#    DELETE /api/news/<post_id>                  — author/admin delete
#    POST   /api/news/<post_id>/like             — toggle a like
#    POST   /api/news/<post_id>/share            — count a share
#    GET    /api/news/<post_id>/comments         — comments page
#    POST   /api/news/<post_id>/comments         — add a comment
#    DELETE /api/news/<post_id>/comments/<c_id>  — delete a comment
#    GET    /api/news/<post_id>/poll             — the post's poll
#    POST   /api/news/<post_id>/poll             — attach a poll
#    DELETE /api/news/<post_id>/poll             — detach a poll
#    POST   /api/news/<post_id>/poll/vote        — cast/move a vote
############################################################


import hashlib
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, make_response, request

from app.api import MAX_CONTENT_LENGTH, MAX_TITLE_LENGTH, SUMMARY_LENGTH, parse_pagination
from app.auth.routes import get_current_user, get_json_object, rate_limit, require_auth
from app.database import get_db, utc_now_iso

# Hard caps behind the 400s in add_comment / create_poll.
# The post-body pair (MAX_TITLE_LENGTH / MAX_CONTENT_LENGTH)
# comes from app/api with SUMMARY_LENGTH — one home shared
# with social/routes.py, so the two POST endpoints can no
# longer drift apart by a missed hand-sync. Migration v1
# truncated pre-existing rows to these, and the mobile app
# mirrors them as input maxLength (create-post/index.tsx —
# content even tighter at 5000 — and
# components/news/CommentComposer.tsx).
MAX_COMMENT_LENGTH = 2000
MAX_POLL_OPTION_LENGTH = 100
MIN_POLL_OPTIONS = 2
MAX_POLL_OPTIONS = 10

# The news_posts CHECK lists, mirrored here so a bad client
# value is a 400 instead of an IntegrityError → 500. 'poll'
# is missing from POST_TYPES on purpose: only create_poll's
# server-side flip may set it, so a client can no longer mint
# a poll card with no poll behind it.
POST_TYPES = ("article", "social", "announcement", "link")
SOURCES = ("app", "knf.vu.lt", "vu.lt", "faculty", "user")

# Scraped rows belong to the scrapers: a poll may not be hung
# on one, because attaching it rewrites post_type to 'poll'
# and no later scrape restores 'article'
SCRAPED_SOURCES = ("knf.vu.lt", "vu.lt")

# The roles whose posts go out as source 'faculty'; they also
# see the not-yet-public faculty rows in the feed
STAFF_ROLES = ("admin", "curator", "teacher")

# How long a feed page may be reused before revalidating.
# Deliberately far shorter than the scrapers' 20-minute tick
# and schedule/info's hours: this feed also carries likes,
# comments and the caller's own fresh posts, so the value the
# ETag delivers is the 304 on an unchanged page, not the
# staleness window.
FEED_CACHE_MAX_AGE = 60

# The feed ranking formula, written ONCE — get_feed builds
# its ORDER BY out of it and /api/social/feed imports it, so
# the two streams can no longer drift apart. {ref} is the
# instant the recency term measures against: julianday('now')
# normally, julianday(?) when the caller pinned the paging
# window with ?before.
#
#   recency     100 / (1 + days since published_at) — a
#               hyperbolic decay, not the exponential one the
#               old comment claimed. MAX(0, …) floors a
#               FUTURE published_at at zero age: a stamp a
#               few hours ahead used to divide by a number
#               near zero (or exactly zero → NULL) and pin
#               the row above everything else.
#   engagement  MIN(likes + 2*comments + 3*shares, 100) * 0.5
#               — linear, capped at 50 (SQLite has no log())
#   boost       faculty 20, knf.vu.lt 15, vu.lt 10, app 5,
#               user 0
#
# COALESCE(…, 0) catches an unparseable published_at, which
# makes julianday() NULL — and a NULL score sorts LAST under
# DESC, silently burying the row instead of ranking it.
FEED_SCORE_SQL = """
                COALESCE(
                    (1.0 / (1.0 + MAX(0, {ref} - julianday(published_at)))) * 100
                    + MIN(likes_count + comments_count * 2 + shares_count * 3, 100) * 0.5
                    + (CASE source
                        WHEN 'faculty' THEN 20
                        WHEN 'knf.vu.lt' THEN 15
                        WHEN 'vu.lt' THEN 10
                        WHEN 'app' THEN 5
                        ELSE 0
                    END), 0)
"""

# Exactly the columns _post_to_dict reads — SELECT * used to
# drag every article body through the ranking sort. The LEFT
# JOIN serves the author's CURRENT display name; the
# author_name snapshot on the row survives only as the
# fallback for scraped rows, whose author_id is NULL.
_POST_SELECT = """
                p.id, p.title, p.content, p.summary, p.image_url, p.author_id,
                COALESCE(u.display_name, p.author_name) AS author_name,
                p.source, p.source_url, p.post_type,
                p.likes_count, p.comments_count, p.shares_count,
                p.published_at, p.is_public
"""

# The columns _can_view_post needs, for the routes that only
# gate and never serve the row
_POST_GATE_SELECT = "id, author_id, source, is_public"

news_bp = Blueprint("news", __name__)
logger = logging.getLogger(__name__)








############################################################
# _parse_iso
############################################################
#
# One aware-UTC datetime out of a stored or client-supplied
# timestamp string, or None when it is missing/unparseable —
# never an exception. A zoneless value is READ as UTC
# (everything this app stores is UTC) and an offset-bearing
# one keeps its offset. Two shapes are repaired first:
#
#   - the legacy space-form separator at index 10
#     ("2026-08-29 12:00:00"), written by a column DEFAULT
#     before migration v17 normalised the stored rows
#   - a '+' that arrived as a SPACE, which is what a query
#     string does to it: "?before=…12:00:00+00:00" reaches
#     request.args as "…12:00:00 00:00". Only spaces left
#     after the separator fix are treated this way, so both
#     repairs can apply to one value.
#
# Used by:
#   - _to_utc_iso (below)
#   - get_feed (below) — the ?before window pin
#   - create_poll (below) — end_date validation
#   - vote_poll (below) — the end-date gate, aware-to-aware
############################################################

def _parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if len(text) > 10 and text[10] == " ":
        text = text[:10] + "T" + text[11:]
    text = text.replace(" ", "+")

    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)








############################################################
# _as_utc
############################################################
#
# One aware datetime moved onto UTC, or None when UTC cannot
# hold it. astimezone() raises OverflowError for a perfectly
# valid ISO-8601 stamp whose UTC equivalent falls off the ends
# of datetime's calendar — "0001-01-01T00:00:00+14:00" and
# "9999-12-31T23:59:59-14:00" are both parseable and both
# crash it — and _parse_iso accepts them happily, so the crash
# used to land PAST every validation gate: a 500 out of
# ?before, out of create_poll's end_date check, out of a poll
# read, and out of the reply vote_poll assembles once its vote
# has already committed.
#
# Used by:
#   - _to_utc_iso (below)
#   - get_feed (below) — the ?before window pin
#   - create_poll (below) — end_date validation
############################################################

def _as_utc(parsed):
    if parsed is None:
        return None

    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        return None








############################################################
# _to_utc_iso
############################################################
#
# Normalises one timestamp string to explicit-UTC ISO T-form
# ("2026-08-29T12:00:00+00:00") so client and server can
# never disagree about the instant. An unparseable string is
# handed back untouched rather than dropped — a null endDate
# would read as "never closes" on the client — and so is one
# UTC cannot hold (_as_utc above), which is still a stamp the
# client can read. Anything that is not a non-blank string
# becomes None.
#
# Used by:
#   - _poll_shape (below) — endDate on the way out
#   - get_comments (below) — legacy comment stamps
############################################################

def _to_utc_iso(value):
    parsed = _as_utc(_parse_iso(value))
    if parsed is None:
        return value if isinstance(value, str) and value.strip() else None
    return parsed.isoformat()








############################################################
# _can_view_post
############################################################
#
# The ONE visibility predicate: True when `user` (None for a
# guest) may see this news_posts row. Public rows are open to
# everyone. A private row is served to its author, to an
# admin, to the other STAFF_ROLES when it is not a wall post
# (the roles that publish as source 'faculty' also proof-read
# the unpublished ones) and — for wall posts, source 'user' —
# to the author's friends. friendships is written in BOTH
# directions on accept (social/routes.py), so one direction
# is enough. Costs one extra query, and only on the private
# wall-post path.
#
# Callers answer 404 (never 403) on False: a stranger must
# not be able to tell "private" from "missing".
#
# The row must carry is_public, author_id and source —
# _POST_GATE_SELECT is exactly that column list.
#
# Used by:
#   - get_post, toggle_like, share_post, get_comments,
#     add_comment, delete_comment, get_poll, create_poll,
#     delete_poll, vote_poll (below)
############################################################

def _can_view_post(db, row, user):
    if row["is_public"]:
        return True

    if not user:
        return False

    if row["author_id"] and row["author_id"] == user["id"]:
        return True

    if user["role"] == "admin":
        return True

    if row["source"] != "user":
        return user["role"] in STAFF_ROLES

    return bool(db.execute(
        "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
        (user["id"], row["author_id"]),
    ).fetchone())








############################################################
# _post_to_dict
############################################################
#
# The wire shape of one news_posts row (camelCase keys — the
# mobile NewsPost type), and the ONE producer of it: get_feed
# and get_post serve it, create_post re-reads its inserted
# row and returns it too (the hand-built 201 body it used to
# assemble silently omitted sourceUrl). NOT included: the
# viewer's `liked` flag and the additive `poll` object, which
# the callers attach per page / per row. `date` is
# published_at; likes / comments / shares are the
# denormalised counters on the row, not live COUNT(*)s — the
# writes in this module recompute them from the child tables.
#
# The row must come from a query built on _POST_SELECT, whose
# author_name is already the JOINed live display name.
#
# Used by:
#   - get_feed, create_post, get_post (below)
############################################################

def _post_to_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "summary": row["summary"],
        "imageUrl": row["image_url"],
        "author": row["author_name"],
        "authorId": row["author_id"],
        "source": row["source"],
        "sourceUrl": row["source_url"],
        "postType": row["post_type"],
        "likes": row["likes_count"],
        "comments": row["comments_count"],
        "shares": row["shares_count"],
        "date": row["published_at"],
        "isPublic": bool(row["is_public"]),
    }








############################################################
# _feed_version
############################################################
#
# A cheap "has anything changed" fingerprint of news_posts:
# the row count, the newest published_at and updated_at, and
# each of the three engagement counters summed on its OWN
# term. The counters matter — a like or a comment changes the
# feed's bytes without moving any timestamp, so a watermark
# built on stamps alone would answer 304 for a page that did
# change. One summed term was not enough either: an unlike and
# a new comment cancel out inside it, and the page they both
# changed answered 304 with the old body.
#
# Derived from the DATA, never from the response body:
# app/__init__.py's escape_json_output hook rewrites the body
# after the view returns, so a body hash would describe bytes
# this function never sees (the same reasoning as
# schedule/routes.py _table_version).
#
# Used by:
#   - get_feed (below)
############################################################

def _feed_version(db):
    row = db.execute("""
        SELECT COUNT(*) AS rows_total,
               MAX(published_at) AS newest,
               MAX(updated_at) AS touched,
               COALESCE(SUM(likes_count), 0) AS likes,
               COALESCE(SUM(comments_count), 0) AS comments,
               COALESCE(SUM(shares_count), 0) AS shares
        FROM news_posts
    """).fetchone()

    return (
        f"{row['rows_total']}:{row['newest'] or '-'}:{row['touched'] or '-'}"
        f":{row['likes']}:{row['comments']}:{row['shares']}"
    )








############################################################
# _cacheable
############################################################
#
# Stamps a weak ETag and a Cache-Control on a feed response.
# The scope is the VIEWER's, not a constant: a member's feed
# carries their friends' private posts and their own liked
# flags, so it is `private` and must never sit in a shared
# cache; a guest's feed is the same bytes for everyone and
# may be `public`. max-age stays short either way
# (FEED_CACHE_MAX_AGE) — the win here is the 304, not the
# staleness, since likes and comments move constantly.
#
# Weak on purpose: escape_json_output re-serialises the body
# afterwards, so the tag identifies the DATA, not an
# octet-exact entity.
#
# Used by:
#   - get_feed (below)
############################################################

def _cacheable(response, tag, shared):
    response.set_etag(tag, weak=True)
    response.headers["Cache-Control"] = f"{'public' if shared else 'private'}, max-age={FEED_CACHE_MAX_AGE}"
    return response








############################################################
# get_feed
############################################################
#
# GET /api/news
#
# The unified feed, one page at a time: ?page / ?per_page
# (parse_pagination — default 20, capped at 100, 400 on
# garbage), an optional ?source out of SOURCES (400 on
# anything else) and an optional ?before. Who sees what:
#
#   guest   is_public = 1 AND source != 'user' — scraped
#           articles and public faculty posts only. A guest
#           asking ?source=user gets an empty page (the two
#           clauses contradict), which is what the mobile
#           "user" chip yields when logged out.
#   member  public non-user rows, the caller's OWN rows
#           whatever their state, and wall posts by the
#           caller's friendships rows — private ones
#           included, because a friend's wall is what this
#           feed is for. A private NON-wall row (an
#           unpublished faculty draft) reaches only its
#           author and STAFF_ROLES; it used to reach every
#           logged-in member. Public wall posts of
#           NON-friends never appear here; /api/social/feed
#           is where those live.
#
# ?before is the additive paging anchor: the client sends
# page 1's request time and every later page repeats it, so
# the recency term is measured against that pinned instant
# AND rows published later are excluded — a scraper insert
# mid-paging can no longer shift the OFFSET window and make
# pages duplicate or drop posts. Omitting it keeps the old
# live-'now' behavior exactly. The ranking runs on a NARROW
# id-only query (FEED_SCORE_SQL, ties broken by published_at
# DESC then id DESC), and only the resulting page of ids is
# joined out to full rows, so article bodies no longer travel
# through the sorter. Page and COUNT share one read
# transaction, so total/hasMore can never contradict the page
# they were computed with.
#
# Every answer carries a weak ETag over the news_posts
# watermark plus the caller, their friend set and the query,
# with Cache-Control private (member) or public (guest) — a
# relaunch that finds nothing new costs a 304 and no body,
# and the 304 is decided BEFORE any of the ranking work.
#
# Used by:
#   - services/api/news.ts fetchNewsFeed —
#     app/(main)/tabs/news.tsx through hooks/useFeed.ts (the
#     source chips map straight onto ?source)
############################################################

@news_bp.route("", methods=["GET"])
def get_feed():
    # STEP 1: pagination, the ?source whitelist, the optional
    # ?before window pin and the optional caller — a bad ?page
    # / ?per_page is a 400 straight from parse_pagination
    # ========================================================
    page, per_page, err = parse_pagination()
    if err:
        return err

    source_filter = request.args.get("source")
    if source_filter is not None and source_filter not in SOURCES:
        return jsonify({"error": f"source must be one of: {', '.join(SOURCES)}"}), 400

    before = request.args.get("before")
    if before is not None:
        # _as_utc, not a bare astimezone: an edge-of-calendar
        # stamp parses and then overflows on the way to UTC, and
        # that is a 400 like any other unusable ?before
        pinned = _as_utc(_parse_iso(before))
        if pinned is None:
            return jsonify({"error": "before must be an ISO-8601 timestamp"}), 400
        before = pinned.isoformat()

    offset = (page - 1) * per_page
    user = get_current_user()


    # STEP 2: assemble the WHERE clause — "1=1" seeds the
    # AND-join so every extra clause is appended the same way
    # =======================================================
    db = get_db()
    try:
        where_clauses = ["1=1"]
        params = []

        if source_filter:
            where_clauses.append("source = ?")
            params.append(source_filter)

        # The pinned ceiling: rows published after the client
        # opened the feed stay out of every page of this run
        if before:
            where_clauses.append("published_at <= ?")
            params.append(before)

        if not user:
            # STEP 2.1: guests — public rows only, never wall posts
            where_clauses.append("is_public = 1")
            where_clauses.append("source != 'user'")
        else:
            # STEP 2.2: members — own and friends' wall posts join in
            friend_ids = [r["friend_id"] for r in db.execute(
                "SELECT friend_id FROM friendships WHERE user_id = ?", (user["id"],)
            ).fetchall()]
            visible_ids = [user["id"]] + friend_ids
            # A ?source naming a non-user source already excludes
            # wall posts, so the author clause is only needed for
            # the mixed feed and for ?source=user
            if not source_filter or source_filter == "user":
                placeholders = ",".join(["?"] * len(visible_ids))
                where_clauses.append(
                    f"(source != 'user' OR author_id IN ({placeholders}))"
                )
                params.extend(visible_ids)

            # STEP 2.3: is_public is NOT decoration — a private
            # faculty row is for its author and for staff. The
            # clause above already limits source 'user' to self
            # and friends, so exempting it here keeps a friend's
            # private wall post visible, as intended
            if user["role"] not in STAFF_ROLES:
                where_clauses.append("(is_public = 1 OR source = 'user' OR author_id = ?)")
                params.append(user["id"])

        where_sql = " AND ".join(where_clauses)
        where_params = list(params)


        # STEP 3: the cache fingerprint — the data watermark plus
        # everything that shapes THIS answer: who is asking, whose
        # walls they can see, and the query itself. A matching
        # If-None-Match ends the request right here, before the
        # ranking scan, the page join and the poll batch
        # ========================================================
        seed = "|".join((
            _feed_version(db),
            user["id"] if user else "guest",
            hashlib.sha256(",".join(sorted(friend_ids)).encode("utf-8")).hexdigest()[:16] if user else "-",
            str(page), str(per_page), source_filter or "-", before or "-",
        ))
        tag = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

        if request.if_none_match.contains_weak(tag):
            return _cacheable(make_response("", 304), tag, user is None)


        # STEP 4: one read snapshot for the page AND the count,
        # then the ranked ids — narrow on purpose, see the banner
        # =======================================================
        db.execute("BEGIN")

        score_sql = FEED_SCORE_SQL.format(ref="julianday(?)" if before else "julianday('now')")
        rank_params = where_params + ([before] if before else []) + [per_page, offset]

        post_ids = [r["id"] for r in db.execute(
            f"""
            SELECT id FROM news_posts
            WHERE {where_sql}
            ORDER BY {score_sql} DESC, published_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            rank_params,
        ).fetchall()]


        # STEP 5: the page rows themselves, fetched by id and put
        # back into the ranked order Python-side
        # =======================================================
        posts = []
        if post_ids:
            placeholders = ",".join(["?"] * len(post_ids))
            by_id = {row["id"]: row for row in db.execute(
                f"""SELECT {_POST_SELECT}
                    FROM news_posts p
                    LEFT JOIN users u ON u.id = p.author_id
                    WHERE p.id IN ({placeholders})""",
                post_ids,
            ).fetchall()}
            posts = [_post_to_dict(by_id[pid]) for pid in post_ids if pid in by_id]


        # STEP 6: the caller's like flag — one IN query for the
        # whole page; a guest is simply never "liked"
        # =====================================================
        liked_set = set()
        if user and post_ids:
            placeholders = ",".join(["?"] * len(post_ids))
            liked_set = {r["post_id"] for r in db.execute(
                f"SELECT post_id FROM news_likes WHERE user_id = ? AND post_id IN ({placeholders})",
                [user["id"]] + post_ids,
            ).fetchall()}

        for p in posts:
            p["liked"] = p["id"] in liked_set


        # STEP 7: poll cards ship their poll inline — three
        # batched queries for the whole page instead of one
        # extra round trip per card. Purely additive field
        # =================================================
        poll_ids = [p["id"] for p in posts if p["postType"] == "poll"]
        if poll_ids:
            polls = _polls_for_posts(db, poll_ids, user["id"] if user else None)
            for p in posts:
                if p["id"] in polls:
                    p["poll"] = polls[p["id"]]


        # STEP 8: total for hasMore — same WHERE and the same
        # snapshot, then close the read transaction and ship the
        # page under the ETag computed in STEP 3
        # ======================================================
        total = db.execute(
            f"SELECT COUNT(*) as total FROM news_posts WHERE {where_sql}",
            where_params,
        ).fetchone()["total"]
        db.commit()

        return _cacheable(make_response(jsonify({
            "posts": posts,
            "page": page,
            "perPage": per_page,
            "total": total,
            "hasMore": offset + per_page < total,
        })), tag, user is None)

    finally:
        db.close()








############################################################
# create_post
############################################################
#
# POST /api/news
#
# Creates a post from {content, title?, post_type?,
# image_url?, is_public?}. There is no role gate — the role
# picks the source instead: STAFF_ROLES publish as source
# 'faculty' (post_type defaults to 'announcement'), everyone
# else as 'user' (defaults to 'social'), the same row shape
# POST /api/social/posts writes. title falls back to
# content[:80], summary is always content[:SUMMARY_LENGTH],
# source_url
# stays NULL and published/created/updated_at all get one
# utc_now_iso() stamp.
#
# Every input is now typed before it reaches SQL: content and
# title must be strings (they used to hit .strip() and 500 on
# anything else), post_type must be in POST_TYPES — 'poll'
# excluded, so no client can mint a poll card with no poll —
# and is_public must be a real boolean, because the truthy
# string "false" used to be stored public AND echoed back
# verbatim as a string. image_url must be a string and a
# relative /api/uploads/ path (or absent): an absolute URL
# would turn every reader into a beacon to an attacker-chosen
# host, and a non-string of ANY truthiness is a 400 like every
# other mistyped field here; the scrapers write their
# knf.vu.lt/vu.lt image URLs directly, never through this
# route.
#
# The 201 body is the inserted row re-read through
# _post_to_dict plus liked=False — one function owns the wire
# shape, and sourceUrl (null here) is no longer missing from
# the create response alone.
#
# A public 'faculty' post rings the 'news' push channel after
# the commit, the same channel the scrapers push on; a push
# failure is logged and never fails the 201.
#
# Used by:
#   - services/api/news.ts createPost —
#     app/(main)/create-post/index.tsx (content, title,
#     image_url from uploadImageApi, is_public); a poll is
#     attached by a follow-up POST .../poll (create_poll)
############################################################

@news_bp.route("", methods=["POST"])
@require_auth
@rate_limit("news_post", max_attempts=20)
def create_post():
    # STEP 1: body checks — an object body, content a non-blank
    # string, title an optional string, both capped (400s); no
    # escaping here, the after_request hook escapes on output
    # =========================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON object body required"}), 400

    raw_content = data.get("content")
    if raw_content is not None and not isinstance(raw_content, str):
        return jsonify({"error": "content must be a string"}), 400
    content = (raw_content or "").strip()
    if not content:
        return jsonify({"error": "Content required"}), 400

    raw_title = data.get("title")
    if raw_title is not None and not isinstance(raw_title, str):
        return jsonify({"error": "title must be a string"}), 400
    title = (raw_title or "").strip() or content[:80]

    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}), 400
    if len(content) > MAX_CONTENT_LENGTH:
        return jsonify({"error": f"Content must be at most {MAX_CONTENT_LENGTH} characters"}), 400


    # STEP 2: the caller's role decides the source; post_type is
    # whitelisted (absent or blank still takes the default) and
    # is_public must be a boolean
    # ==========================================================
    role = request.user["role"]
    post_type = data.get("post_type")
    image_url = data.get("image_url")
    is_public = data.get("is_public", True)

    if post_type and post_type not in POST_TYPES:
        return jsonify({"error": f"post_type must be one of: {', '.join(POST_TYPES)}"}), 400

    if not isinstance(is_public, bool):
        return jsonify({"error": "is_public must be a boolean"}), 400

    # Own uploads only — foreign hosts would beacon every reader's
    # IP/UA to whoever the author picked (see the banner). The
    # type is checked on its own, because a FALSY non-string (0,
    # false) used to slip past a truthiness-guarded isinstance and
    # reach the TEXT column as the string "0", which the mobile
    # card then tried to load as /0. "" stays a legal "no cover"
    if image_url is not None and not isinstance(image_url, str):
        return jsonify({"error": "image_url must be a relative /api/uploads/ path"}), 400
    if image_url and not image_url.startswith("/api/uploads/"):
        return jsonify({"error": "image_url must be a relative /api/uploads/ path"}), 400

    if role in STAFF_ROLES:
        source = "faculty"
        if not post_type:
            post_type = "announcement"
    else:
        source = "user"
        if not post_type:
            post_type = "social"


    # STEP 3: insert, then re-read the row so the 201 carries
    # exactly the shape every read path serves
    # =======================================================
    post_id = str(uuid.uuid4())
    now = utc_now_iso()
    summary = content[:SUMMARY_LENGTH]

    db = get_db()
    try:
        db.execute(
            """INSERT INTO news_posts
               (id, title, content, summary, image_url, author_id, author_name,
                source, source_url, post_type, is_public, published_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
            (post_id, title, content, summary, image_url,
             request.user["id"], request.user["display_name"],
             source, post_type, 1 if is_public else 0, now, now, now),
        )
        db.commit()

        logger.info(
            "News post %s created by %s (source=%s, post_type=%s, public=%s)",
            post_id, request.user["id"], source, post_type, is_public,
        )

        row = db.execute(
            f"""SELECT {_POST_SELECT}
                FROM news_posts p
                LEFT JOIN users u ON u.id = p.author_id
                WHERE p.id = ?""",
            (post_id,),
        ).fetchone()
        body = {**_post_to_dict(row), "liked": False}
    finally:
        db.close()


    # STEP 4: a public faculty announcement rings the 'news'
    # channel. notify_channel opens its OWN connection, so it
    # runs after the close above; a failure is logged only
    # =======================================================
    if source == "faculty" and is_public:
        try:
            # Lazy import, not a cycle guard — push.py only
            # imports app.database
            from app.notifications.push import notify_channel
            notify_channel(
                "news", title, summary,
                data={"type": "news", "source": "faculty", "postId": post_id},
                exclude_user_id=request.user["id"],
            )
        except Exception:
            logger.exception("Failed to push the new faculty post %s on the 'news' channel", post_id)

    return jsonify(body), 201








############################################################
# get_post
############################################################
#
# GET /api/news/<post_id>
#
# One post by id, gated by _can_view_post: a private row
# reaches its author, an admin, staff (non-wall rows) and the
# author's friends (wall rows); everyone else, guests
# included, gets the same 404 as for a missing id, so
# existence never leaks. The private NON-wall case used to
# have no check at all and was served to anyone who knew the
# id. The body is _post_to_dict plus the viewer's `liked`
# flag (the same news_likes lookup as get_feed's STEP 5,
# false for guests) — the mobile detail screen seeds its
# heart from this load — plus the additive `poll` object on a
# poll post.
#
# Used by:
#   - services/api/news.ts fetchNewsPost —
#     app/(main)/news-post/index.tsx (opened from the feed
#     and from profile/index.tsx)
############################################################

@news_bp.route("/<post_id>", methods=["GET"])
def get_post(post_id):
    user = get_current_user()
    db = get_db()
    try:
        row = db.execute(
            f"""SELECT {_POST_SELECT}
                FROM news_posts p
                LEFT JOIN users u ON u.id = p.author_id
                WHERE p.id = ?""",
            (post_id,),
        ).fetchone()
        if not row:
            return jsonify({"error": "Post not found"}), 404

        # The 404 (not 403) is deliberate — a stranger cannot tell
        # "private" from "missing"
        if not _can_view_post(db, row, user):
            return jsonify({"error": "Post not found"}), 404

        body = _post_to_dict(row)

        # The viewer's own like, one lookup — a guest is simply
        # never "liked" (mirrors get_feed's STEP 5)
        body["liked"] = False
        if user:
            body["liked"] = bool(db.execute(
                "SELECT 1 FROM news_likes WHERE user_id = ? AND post_id = ?",
                (user["id"], post_id),
            ).fetchone())

        # Additive: the poll travels with the post, so PollWidget
        # can paint before its own fetch returns
        if row["post_type"] == "poll":
            poll = _polls_for_posts(db, [post_id], user["id"] if user else None).get(post_id)
            if poll:
                body["poll"] = poll

        return jsonify(body)
    finally:
        db.close()








############################################################
# delete_post
############################################################
#
# DELETE /api/news/<post_id>
#
# Removes a post when the caller is its author or an admin
# (403 otherwise — scraped articles have author_id NULL, so
# only an admin can drop those). Likes, comments, the polls
# and their votes/options are deleted by hand before the post
# row; the schema's ON DELETE CASCADE (get_db turns
# foreign_keys on) would do the same, so the manual sweep is
# belt and braces — DELETE /api/social/posts/<id> in
# social/routes.py trusts the cascade alone. The poll sweep
# iterates ALL rows for the post, not fetchone(): duplicates
# were possible before migration v26's unique index and a
# leftover would block the delete on the FK.
#
# A row with a source_url is a scraped article, and deleting
# it used to be pointless — both scrapers dedupe on
# source_url against news_posts alone, so the next run
# (20 minutes at most) re-inserted the very article an admin
# had removed. The URL is therefore written to the
# deleted_source_urls tombstone table (migration v25), which
# the scrapers' dedupe now also consults.
#
# Used by:
#   - nothing calls this at the moment — the mobile app
#     deletes wall posts through services/api/social.ts
#     deletePost → DELETE /api/social/posts/<id>; only
#     swagger/swagger.yaml documents this one
############################################################

@news_bp.route("/<post_id>", methods=["DELETE"])
@require_auth
@rate_limit("news_delete", max_attempts=60)
def delete_post(post_id):
    # STEP 1: the post must exist and the caller must be its
    # author or an admin
    # ======================================================
    db = get_db()
    try:
        post = db.execute(
            "SELECT id, author_id, source, source_url, image_url FROM news_posts WHERE id = ?",
            (post_id,),
        ).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        user = request.user
        if post["author_id"] != user["id"] and user["role"] != "admin":
            return jsonify({"error": "Only the post author or an admin can delete this post"}), 403


        # STEP 2: tombstone the scraped URL so the scrapers do
        # not resurrect the article on their next tick
        # ====================================================
        if post["source_url"]:
            db.execute(
                """INSERT OR IGNORE INTO deleted_source_urls (source_url, deleted_by, deleted_at)
                   VALUES (?, ?, ?)""",
                (post["source_url"], user["id"], utc_now_iso()),
            )


        # STEP 3: dependants first — likes, comments, then every
        # poll with its votes and options (the cascade would do
        # this too, see the banner)
        # ======================================================
        db.execute("DELETE FROM news_likes WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM news_comments WHERE post_id = ?", (post_id,))

        for poll in db.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchall():
            db.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM poll_options WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM polls WHERE id = ?", (poll["id"],))


        # STEP 4: the post row itself, one commit for the lot
        # ===================================================
        db.execute("DELETE FROM news_posts WHERE id = ?", (post_id,))
        db.commit()


        # STEP 5: the cover file, once the row is definitely gone
        # — only our own uploads, and only after the commit so a
        # failed delete never orphans a live post's image
        # =======================================================
        image_url = post["image_url"]
        if isinstance(image_url, str) and image_url.startswith("/api/uploads/"):
            try:
                from app.uploads.routes import delete_upload

                delete_upload(image_url)
            except Exception:
                logger.warning("Could not delete upload for post %s", post_id, exc_info=True)

        if post["author_id"] == user["id"]:
            logger.info("News post %s deleted by its author %s", post_id, user["id"])
        else:
            logger.warning(
                "News post %s (author %s, source %s) deleted by admin %s",
                post_id, post["author_id"], post["source"], user["id"],
            )

        return jsonify({"status": "deleted"})
    finally:
        db.close()








############################################################
# toggle_like
############################################################
#
# POST /api/news/<post_id>/like
#
# Flips the caller's like on a post they may actually SEE
# (_can_view_post — the route used to accept any post id, so
# a private post could be liked, and its counter moved, by a
# stranger): INSERT OR IGNORE on the news_likes row (PK
# user_id + post_id) — a fresh insert means "liked", an
# ignored one means the like already existed and is deleted
# instead, so two racing toggles from one user can no longer
# collide on the PK (the old SELECT-then-INSERT died as
# IntegrityError → 500). likes_count is then recomputed from
# the news_likes rows in the same transaction, so the counter
# can neither drift nor go negative; the count in the reply
# is re-read after the commit, so it includes likes other
# users landed meanwhile — and when that re-read finds the
# post deleted in the meantime the answer is the same 404 the
# gate would have given, never a crash on a missing row.
#
# Used by:
#   - services/api/news.ts toggleLikeApi —
#     app/(main)/tabs/news.tsx (feed heart, optimistic) and
#     app/(main)/news-post/index.tsx (detail heart)
############################################################

@news_bp.route("/<post_id>/like", methods=["POST"])
@require_auth
@rate_limit("news_like", max_attempts=300)
def toggle_like(post_id):
    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, request.user):
            return jsonify({"error": "Post not found"}), 404

        # rowcount 1 = the like landed; 0 = the row already existed,
        # so this toggle removes it — no SELECT window to race
        cur = db.execute(
            "INSERT OR IGNORE INTO news_likes (user_id, post_id) VALUES (?, ?)",
            (request.user["id"], post_id),
        )
        if cur.rowcount == 1:
            liked = True
        else:
            db.execute("DELETE FROM news_likes WHERE user_id = ? AND post_id = ?", (request.user["id"], post_id))
            liked = False

        # Recomputed from the rows, not ±1 — a drifted counter heals
        # here instead of compounding
        db.execute(
            "UPDATE news_posts SET likes_count = (SELECT COUNT(*) FROM news_likes WHERE post_id = ?) WHERE id = ?",
            (post_id, post_id),
        )

        db.commit()
        # Re-read after the commit — the reply carries the real
        # counter, not a likes±1 computed locally. The row can be
        # GONE by now (its author or an admin deleting it in that
        # window), and the subscript on a missing row used to be a
        # TypeError → 500 where the very same request a moment
        # earlier answered 404
        fresh = db.execute("SELECT likes_count FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not fresh:
            return jsonify({"error": "Post not found"}), 404

        return jsonify({"liked": liked, "likes": fresh["likes_count"]})
    finally:
        db.close()








############################################################
# share_post
############################################################
#
# POST /api/news/<post_id>/share
#
# Counts one completed share: bumps news_posts.shares_count
# and answers {"shares": <fresh count>} for the caller's
# reconcile patch. No auth on purpose — guests can use the
# OS share sheet too, matching the client, and nothing here
# writes per-user state — but no auth is not no GATE: the row
# goes through _can_view_post like every other route in this
# file, so a private wall post or an unpublished faculty draft
# answers 404 (the route used to answer 200 and move the
# counter for anyone who knew the id, which also leaked the
# row's existence through the 200-vs-404 split, and
# shares_count feeds the feed ranking's engagement term).
#
# 404 for an unknown id too, and the count is re-read after
# the commit like toggle_like's, so it includes shares other
# users landed meanwhile — a post deleted in that window is
# the same 404, not a crash on the missing row. Nothing ever
# decrements this counter — a cancelled sheet is never
# reported here.
#
# Used by:
#   - services/api/news.ts sharePostApi —
#     app/(main)/tabs/news.tsx, after the OS share sheet
#     reports completion
############################################################

@news_bp.route("/<post_id>/share", methods=["POST"])
def share_post(post_id):
    # The caller is optional (get_current_user, not require_auth),
    # but it is what _can_view_post needs to tell a friend's
    # private wall post from a stranger's
    user = get_current_user()

    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, user):
            return jsonify({"error": "Post not found"}), 404

        db.execute("UPDATE news_posts SET shares_count = shares_count + 1 WHERE id = ?", (post_id,))
        db.commit()

        # The row may be gone by now — the same 404 as above, not a
        # subscript on None (see the banner)
        fresh = db.execute("SELECT shares_count FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not fresh:
            return jsonify({"error": "Post not found"}), 404

        return jsonify({"shares": fresh["shares_count"]})
    finally:
        db.close()








############################################################
# get_comments
############################################################
#
# GET /api/news/<post_id>/comments
#
# One page of a post's comments, newest first, joined to
# users for the author's name and avatar (?page / ?per_page
# as in get_feed). id breaks created_at ties — without it two
# comments in the same second could swap places between pages
# and the OFFSET window would duplicate or drop rows.
#
# The parent post is loaded and run through _can_view_post
# first: a private post's comments used to be readable
# WITHOUT auth, since the gate existed only on the post
# itself. A missing post and a hidden one both answer 404 —
# an empty page for one and a 404 for the other would tell a
# stranger which is which.
#
# The users JOIN is a LEFT JOIN so the page and the total
# count the same rows: an orphaned comment (user gone, the
# CASCADE missed) used to vanish from every page while still
# counting toward total, and paging never converged. Its
# author renders as 'Deleted user'. `time` goes out through
# _to_utc_iso, so legacy space-form stamps reach the client
# in the same explicit-UTC shape add_comment writes today —
# the client used to read the space form as LOCAL time.
#
# Used by:
#   - services/api/news.ts fetchComments —
#     app/(main)/news-comments/index.tsx (full thread) and
#     app/(main)/news-post/index.tsx (inline preview)
############################################################

@news_bp.route("/<post_id>/comments", methods=["GET"])
def get_comments(post_id):
    page, per_page, err = parse_pagination()
    if err:
        return err
    offset = (page - 1) * per_page

    user = get_current_user()
    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, user):
            return jsonify({"error": "Post not found"}), 404

        rows = db.execute(
            """SELECT c.id, c.text, c.created_at, c.user_id,
                      COALESCE(u.display_name, 'Deleted user') AS display_name, u.avatar_url
               FROM news_comments c
               LEFT JOIN users u ON u.id = c.user_id
               WHERE c.post_id = ?
               ORDER BY c.created_at DESC, c.id DESC
               LIMIT ? OFFSET ?""",
            (post_id, per_page, offset),
        ).fetchall()

        comments = [
            {
                "id": r["id"],
                "text": r["text"],
                "time": _to_utc_iso(r["created_at"]),
                "userName": r["display_name"],
                "userAvatar": r["avatar_url"],
                "userId": r["user_id"],
            }
            for r in rows
        ]

        total = db.execute("SELECT COUNT(*) as c FROM news_comments WHERE post_id = ?", (post_id,)).fetchone()["c"]

        return jsonify({"comments": comments, "total": total, "page": page, "perPage": per_page})
    finally:
        db.close()








############################################################
# add_comment
############################################################
#
# POST /api/news/<post_id>/comments
#
# Appends {text} to a post the caller may SEE — a non-blank
# string of at most MAX_COMMENT_LENGTH (400 otherwise, 404
# for an unknown or hidden post; the route used to accept any
# post id, so a private post could be commented on by a
# stranger).
#
# comments_count is RECOMPUTED from news_comments in the same
# transaction (the old +1 could not survive delete_comment
# below, and migration v14 had to reset the drift the bumps
# left in production). ONE clock read serves both the row's
# created_at and the 201 body's `time`, so a comment no
# longer appears to jump in time when the list refetches —
# the two used to be separate reads in two different shapes.
#
# Used by:
#   - services/api/news.ts addCommentApi —
#     app/(main)/news-comments/index.tsx and
#     app/(main)/news-post/index.tsx (both via
#     components/news/CommentComposer.tsx)
############################################################

@news_bp.route("/<post_id>/comments", methods=["POST"])
@require_auth
@rate_limit("news_comment", max_attempts=60)
def add_comment(post_id):
    data = get_json_object()
    if not data or not isinstance(data.get("text"), str) or not data["text"].strip():
        return jsonify({"error": "Comment text required"}), 400

    comment_text = data["text"].strip()

    if len(comment_text) > MAX_COMMENT_LENGTH:
        return jsonify({"error": f"Comment must be at most {MAX_COMMENT_LENGTH} characters"}), 400

    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, request.user):
            return jsonify({"error": "Post not found"}), 404

        comment_id = str(uuid.uuid4())
        now = utc_now_iso()

        db.execute(
            "INSERT INTO news_comments (id, post_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (comment_id, post_id, request.user["id"], comment_text, now),
        )
        db.execute(
            "UPDATE news_posts SET comments_count = (SELECT COUNT(*) FROM news_comments WHERE post_id = ?) WHERE id = ?",
            (post_id, post_id),
        )
        db.commit()

        return jsonify({
            "id": comment_id,
            "text": comment_text,
            "time": now,
            "userName": request.user["display_name"],
            "userAvatar": request.user.get("avatar_url"),
            "userId": request.user["id"],
        }), 201
    finally:
        db.close()








############################################################
# delete_comment
############################################################
#
# DELETE /api/news/<post_id>/comments/<comment_id>
#
# Removes one comment when the caller wrote it, owns the post
# it hangs under, or is an admin (403 otherwise) — until this
# route existed nothing could take a comment back, which is
# also why comments_count only ever grew. comments_count is
# recomputed from the rows after the delete, the same idiom
# add_comment and the like toggle use.
#
# Additive route: the mobile app does not call it yet, so no
# existing response changes shape. The comment must belong to
# the post in the path, or the 404 fires — a comment id from
# another thread must not be deletable through a post the
# caller happens to own.
#
# Used by:
#   - nothing calls this at the moment — swagger documents it
#     for the admin/moderation flow the app has yet to grow
############################################################

@news_bp.route("/<post_id>/comments/<comment_id>", methods=["DELETE"])
@require_auth
@rate_limit("news_comment_delete", max_attempts=60)
def delete_comment(post_id, comment_id):
    # STEP 1: the post gates the thread, then the comment must
    # really be one of ITS comments
    # ========================================================
    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, request.user):
            return jsonify({"error": "Post not found"}), 404

        comment = db.execute(
            "SELECT id, user_id FROM news_comments WHERE id = ? AND post_id = ?",
            (comment_id, post_id),
        ).fetchone()
        if not comment:
            return jsonify({"error": "Comment not found"}), 404


        # STEP 2: comment author, post author or admin — nobody
        # else may remove someone else's words
        # =====================================================
        user = request.user
        if (comment["user_id"] != user["id"]
                and post["author_id"] != user["id"]
                and user["role"] != "admin"):
            return jsonify({"error": "Only the comment author, the post author or an admin can delete this comment"}), 403


        # STEP 3: delete and recompute the counter from the rows
        # ======================================================
        db.execute("DELETE FROM news_comments WHERE id = ?", (comment_id,))
        db.execute(
            "UPDATE news_posts SET comments_count = (SELECT COUNT(*) FROM news_comments WHERE post_id = ?) WHERE id = ?",
            (post_id, post_id),
        )
        db.commit()

        count = db.execute(
            "SELECT comments_count FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()["comments_count"]

        logger.info("Comment %s on post %s deleted by %s", comment_id, post_id, user["id"])

        return jsonify({"status": "deleted", "comments": count})
    finally:
        db.close()








############################################################
# _poll_shape
############################################################
#
# The ONE producer of the poll wire shape (the mobile
# PollResponse): a polls row, its already-fetched option rows
# in rowid order — the order the creator sent them — and the
# caller's own option id (None for a guest or a non-voter).
# endDate goes out as explicit-UTC ISO through _to_utc_iso:
# it used to ship zone-less, so the client's local-time
# reading closed the poll at the wrong hour.
#
# Used by:
#   - _polls_for_posts, _poll_to_dict (below)
############################################################

def _poll_shape(poll_row, option_rows, user_vote):
    return {
        "id": poll_row["id"],
        "postId": poll_row["post_id"],
        "title": poll_row["title"],
        "endDate": _to_utc_iso(poll_row["end_date"]),
        "totalVotes": poll_row["total_votes"],
        "createdAt": poll_row["created_at"],
        "userVote": user_vote,
        "options": [
            {"id": o["id"], "text": o["text"], "votes": o["votes"]}
            for o in option_rows
        ],
    }








############################################################
# _polls_for_posts
############################################################
#
# {post_id: poll dict} for a whole page of post ids, in THREE
# queries however many polls the page holds — the polls, all
# their options at once, and the caller's votes at once. The
# feed embeds its poll cards through this, so a page of ten
# polls costs three queries instead of the twenty
# _poll_to_dict would run (and the twenty round trips the
# client used to make).
#
# Used by:
#   - get_feed, get_post (above)
############################################################

def _polls_for_posts(db, post_ids, user_id=None):
    if not post_ids:
        return {}

    post_ph = ",".join(["?"] * len(post_ids))
    poll_rows = db.execute(
        f"SELECT id, post_id, title, end_date, total_votes, created_at FROM polls WHERE post_id IN ({post_ph})",
        list(post_ids),
    ).fetchall()
    if not poll_rows:
        return {}

    poll_ids = [r["id"] for r in poll_rows]
    poll_ph = ",".join(["?"] * len(poll_ids))

    options = {}
    for opt in db.execute(
        f"SELECT id, poll_id, text, votes FROM poll_options WHERE poll_id IN ({poll_ph}) ORDER BY rowid",
        poll_ids,
    ).fetchall():
        options.setdefault(opt["poll_id"], []).append(opt)

    votes = {}
    if user_id:
        for vote in db.execute(
            f"SELECT poll_id, option_id FROM poll_votes WHERE user_id = ? AND poll_id IN ({poll_ph})",
            [user_id] + poll_ids,
        ).fetchall():
            votes[vote["poll_id"]] = vote["option_id"]

    return {
        row["post_id"]: _poll_shape(row, options.get(row["id"], []), votes.get(row["id"]))
        for row in poll_rows
    }








############################################################
# _poll_to_dict
############################################################
#
# One already-fetched polls row as the wire shape, for the
# single-poll routes: two more queries on the caller's open
# connection (its options, then the caller's vote) feeding
# _poll_shape. A whole page of polls goes through
# _polls_for_posts instead.
#
# Used by:
#   - get_poll, create_poll, vote_poll (below)
############################################################

def _poll_to_dict(db, poll_row, user_id=None):
    poll_id = poll_row["id"]
    options = db.execute(
        "SELECT id, text, votes FROM poll_options WHERE poll_id = ? ORDER BY rowid",
        (poll_id,),
    ).fetchall()

    user_vote = None
    if user_id:
        vote = db.execute(
            "SELECT option_id FROM poll_votes WHERE poll_id = ? AND user_id = ?",
            (poll_id, user_id),
        ).fetchone()
        if vote:
            user_vote = vote["option_id"]

    return _poll_shape(poll_row, options, user_vote)








############################################################
# get_poll
############################################################
#
# GET /api/news/<post_id>/poll
#
# The poll attached to a post, with the caller's own vote
# when logged in. 404 when the post has no poll — the mobile
# fetchPoll turns exactly that status into null and rethrows
# everything else — and the SAME 404, with the same body, for
# an unknown post and for a post the caller may not see. The
# post used to be skipped entirely, so a private post's poll
# (its question, its options and its running tally) was
# readable without any auth at all.
#
# Used by:
#   - services/api/news.ts fetchPoll —
#     components/news/PollWidget.tsx (initial load, and the
#     refetch votePollApi does after a 409)
############################################################

@news_bp.route("/<post_id>/poll", methods=["GET"])
def get_poll(post_id):
    user = get_current_user()
    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, user):
            return jsonify({"error": "No poll found for this post"}), 404

        poll = db.execute("SELECT * FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if not poll:
            return jsonify({"error": "No poll found for this post"}), 404
        return jsonify(_poll_to_dict(db, poll, user["id"] if user else None))
    finally:
        db.close()








############################################################
# create_poll
############################################################
#
# POST /api/news/<post_id>/poll
#
# Attaches a poll {title, options[], end_date?} to an
# existing post — author or admin only (403), one poll per
# post (409) — and flips the post's post_type to 'poll'.
#
# Every field is typed and bounded now. options must be a
# LIST of strings (a bare string used to pass the len() check
# and be iterated character by character; a dict yielded its
# keys; an int raised straight into a 500); blanks are
# stripped BEFORE the 2..10 count check, so a poll can no
# longer be created with fewer options than it declared;
# title and each option carry a length cap; end_date must
# parse as ISO-8601 AND survive the move onto UTC (an
# edge-of-calendar stamp used to crash there rather than earn
# a 400), and is stored normalised to explicit UTC, so
# vote_poll's gate never has to guess.
#
# The one-poll-per-post rule is enforced twice: the friendly
# pre-check, and migration v26's UNIQUE index on
# polls(post_id) whose IntegrityError becomes the same 409 —
# two concurrent calls used to hang two polls on one post.
# A scraped article is refused outright (400): flipping its
# post_type is not something a later scrape would undo.
#
# Used by:
#   - services/api/news.ts createPollApi —
#     app/(main)/create-post/index.tsx, right after
#     createPost (with a retry prompt when this call fails)
############################################################

@news_bp.route("/<post_id>/poll", methods=["POST"])
@require_auth
@rate_limit("news_poll", max_attempts=20)
def create_poll(post_id):
    # STEP 1: body checks — an object body with a non-blank
    # capped title
    # =====================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON object body required"}), 400

    raw_title = data.get("title")
    if raw_title is not None and not isinstance(raw_title, str):
        return jsonify({"error": "title must be a string"}), 400
    title = (raw_title or "").strip()
    if not title:
        return jsonify({"error": "Poll title required"}), 400
    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({"error": f"Poll title must be at most {MAX_TITLE_LENGTH} characters"}), 400


    # STEP 2: the options — a list of strings, stripped of
    # blanks first, then counted and capped
    # ====================================================
    raw_options = data.get("options", [])
    if not isinstance(raw_options, list) or any(not isinstance(o, str) for o in raw_options):
        return jsonify({"error": "options must be an array of strings"}), 400

    options = [o.strip() for o in raw_options if o.strip()]
    if len(options) < MIN_POLL_OPTIONS:
        return jsonify({"error": f"At least {MIN_POLL_OPTIONS} options required"}), 400
    if len(options) > MAX_POLL_OPTIONS:
        return jsonify({"error": f"Maximum {MAX_POLL_OPTIONS} options allowed"}), 400
    if any(len(o) > MAX_POLL_OPTION_LENGTH for o in options):
        return jsonify({"error": f"Each option must be at most {MAX_POLL_OPTION_LENGTH} characters"}), 400


    # STEP 3: the optional end date — parsed AND moved onto UTC
    # here so vote_poll never faces a value it cannot compare,
    # stored in the one explicit-UTC shape. An edge-of-calendar
    # stamp ("0001-01-01T00:00:00+14:00") parses and then
    # overflows on the way to UTC; it is refused with the same
    # 400 as any other unusable end_date instead of crashing
    # =========================================================
    end_date = data.get("end_date")
    if end_date is not None:
        pinned = _as_utc(_parse_iso(end_date))
        if pinned is None:
            return jsonify({"error": "end_date must be an ISO-8601 timestamp"}), 400
        end_date = pinned.isoformat()


    # STEP 4: the post must exist and be visible, the caller
    # must own it (or be admin), and it must not be scraped
    # ======================================================
    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, request.user):
            return jsonify({"error": "Post not found"}), 404

        user = request.user
        if post["author_id"] != user["id"] and user["role"] != "admin":
            return jsonify({"error": "Only the post author or admin can create a poll"}), 403

        if post["source"] in SCRAPED_SOURCES:
            return jsonify({"error": "A scraped article cannot carry a poll"}), 400

        existing = db.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if existing:
            return jsonify({"error": "Post already has a poll"}), 409


        # STEP 5: the poll row — the unique index is the real
        # guard, so its IntegrityError answers the same 409
        # ===================================================
        poll_id = str(uuid.uuid4())
        now = utc_now_iso()

        try:
            db.execute(
                "INSERT INTO polls (id, post_id, title, end_date, created_at) VALUES (?, ?, ?, ?, ?)",
                (poll_id, post_id, title, end_date, now),
            )
        except sqlite3.IntegrityError:
            db.rollback()
            logger.warning("Concurrent poll creation on post %s lost the race", post_id)
            return jsonify({"error": "Post already has a poll"}), 409

        for opt_text in options:
            db.execute(
                "INSERT INTO poll_options (id, poll_id, text) VALUES (?, ?, ?)",
                (str(uuid.uuid4()), poll_id, opt_text),
            )


        # STEP 6: the post becomes a 'poll' post (delete_poll
        # below puts the type back), then commit and answer with
        # the fresh poll (userVote None, 201)
        # ======================================================
        db.execute("UPDATE news_posts SET post_type = 'poll' WHERE id = ?", (post_id,))
        db.commit()

        logger.info(
            "Poll %s (%d options) attached to post %s by %s",
            poll_id, len(options), post_id, user["id"],
        )

        poll = db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        return jsonify(_poll_to_dict(db, poll, user["id"])), 201
    finally:
        db.close()








############################################################
# delete_poll
############################################################
#
# DELETE /api/news/<post_id>/poll
#
# Detaches the post's poll — author or admin only (403) —
# with its options and votes, and puts post_type back to what
# the row's source implies: 'social' for a wall post,
# 'article' for a scraped row (legacy only, create_poll
# refuses those now) and 'announcement' for everything else.
# Without this route create_poll's unconditional flip to
# 'poll' was one-way, and a post whose poll was dropped by
# hand kept advertising a poll card the feed could not fill.
#
# Additive route: the mobile app does not call it yet.
#
# Used by:
#   - nothing calls this at the moment — swagger documents it
############################################################

@news_bp.route("/<post_id>/poll", methods=["DELETE"])
@require_auth
@rate_limit("news_poll", max_attempts=20)
def delete_poll(post_id):
    # STEP 1: the post gates the poll, then author-or-admin
    # =====================================================
    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, request.user):
            return jsonify({"error": "Post not found"}), 404

        user = request.user
        if post["author_id"] != user["id"] and user["role"] != "admin":
            return jsonify({"error": "Only the post author or admin can delete this poll"}), 403

        polls = db.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchall()
        if not polls:
            return jsonify({"error": "No poll found for this post"}), 404


        # STEP 2: votes and options before the poll row, then the
        # post_type its source implies
        # =======================================================
        for poll in polls:
            db.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM poll_options WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM polls WHERE id = ?", (poll["id"],))

        if post["source"] == "user":
            restored = "social"
        elif post["source"] in SCRAPED_SOURCES:
            restored = "article"
        else:
            restored = "announcement"

        db.execute("UPDATE news_posts SET post_type = ? WHERE id = ?", (restored, post_id))
        db.commit()

        logger.info("Poll on post %s deleted by %s, post_type restored to %s", post_id, user["id"], restored)

        return jsonify({"status": "deleted", "postType": restored})
    finally:
        db.close()








############################################################
# vote_poll
############################################################
#
# POST /api/news/<post_id>/poll/vote
#
# Casts or moves the caller's vote {option_id} on the post's
# poll — one poll_votes row per (user_id, poll_id). Re-voting
# the option already held is a 409, which the mobile
# votePollApi treats as a no-op and answers with a refetched
# poll; everything else lands as a cast or a move.
#
# The whole gate-and-write sequence runs inside ONE BEGIN
# IMMEDIATE transaction, and the write itself is an
# ON CONFLICT(user_id, poll_id) DO UPDATE. Two first votes
# racing used to collide on the primary key (IntegrityError →
# 500) and, when they missed each other, each bumped the
# counters separately — so the tallies drifted upward and
# migration v14 had to reset them. The counters are now
# RECOMPUTED from the poll_votes rows instead of nudged by
# ±1, which makes a repeated or interleaved call harmless.
#
# The end-date gate compares aware to aware: an offset in the
# stored string used to be DROPPED rather than converted, so
# a "…+03:00" poll closed three hours late. An unparseable
# end_date still means "open", but it is logged now instead
# of being swallowed by a bare except.
#
# Used by:
#   - services/api/news.ts votePollApi —
#     components/news/PollWidget.tsx (option tap)
############################################################

@news_bp.route("/<post_id>/poll/vote", methods=["POST"])
@require_auth
@rate_limit("news_vote", max_attempts=120)
def vote_poll(post_id):
    # STEP 1: body — option_id is the only field, and it must be
    # a non-blank STRING (a dict or a list used to reach the SQL
    # binder and 500 there)
    # ==========================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON object body required"}), 400

    option_id = data.get("option_id")
    if not isinstance(option_id, str) or not option_id.strip():
        return jsonify({"error": "option_id required"}), 400
    option_id = option_id.strip()

    user_id = request.user["id"]


    # STEP 2: the post gates its poll — a private post's poll is
    # not votable by a stranger, and the 404 body is the one the
    # client already knows
    # ==========================================================
    db = get_db()
    try:
        post = db.execute(
            f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not post or not _can_view_post(db, post, request.user):
            return jsonify({"error": "No poll found for this post"}), 404


        # STEP 3: BEGIN IMMEDIATE — the poll read, the end-date
        # gate, the option check, the vote and the counter
        # recompute all sit in ONE write transaction, so two taps
        # can neither interleave nor collide
        # =======================================================
        db.execute("BEGIN IMMEDIATE")

        poll = db.execute("SELECT * FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if not poll:
            db.rollback()
            return jsonify({"error": "No poll found for this post"}), 404

        poll_id = poll["id"]

        if poll["end_date"]:
            end = _parse_iso(poll["end_date"])
            if end is None:
                logger.warning(
                    "Poll %s has an unparseable end_date %r — treated as still open",
                    poll_id, poll["end_date"],
                )
            elif datetime.now(timezone.utc) > end:
                db.rollback()
                return jsonify({"error": "Poll has ended"}), 400


        # STEP 4: the option must belong to THIS poll — a foreign
        # option id is a plain 400
        # =======================================================
        option = db.execute(
            "SELECT id FROM poll_options WHERE id = ? AND poll_id = ?",
            (option_id, poll_id),
        ).fetchone()
        if not option:
            db.rollback()
            return jsonify({"error": "Invalid option"}), 400


        # STEP 5: cast or move. The prior row is read inside this
        # transaction, so the 409 branch is exact; the upsert then
        # makes the write itself idempotent
        # ========================================================
        existing = db.execute(
            "SELECT option_id FROM poll_votes WHERE user_id = ? AND poll_id = ?",
            (user_id, poll_id),
        ).fetchone()
        if existing and existing["option_id"] == option_id:
            db.rollback()
            return jsonify({"error": "Already voted for this option"}), 409

        db.execute(
            """INSERT INTO poll_votes (user_id, poll_id, option_id, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, poll_id)
               DO UPDATE SET option_id = excluded.option_id, created_at = excluded.created_at""",
            (user_id, poll_id, option_id, utc_now_iso()),
        )


        # STEP 6: counters recomputed from the rows, never ±1 —
        # every option of the poll at once, then the poll's own
        # total, so a drifted tally heals on the next vote
        # =====================================================
        db.execute(
            """UPDATE poll_options
               SET votes = (SELECT COUNT(*) FROM poll_votes WHERE poll_votes.option_id = poll_options.id)
               WHERE poll_id = ?""",
            (poll_id,),
        )
        db.execute(
            "UPDATE polls SET total_votes = (SELECT COUNT(*) FROM poll_votes WHERE poll_id = ?) WHERE id = ?",
            (poll_id, poll_id),
        )
        db.commit()


        # STEP 7: answer with the fresh poll state, re-read after
        # the commit so it includes votes that landed meanwhile
        # =======================================================
        poll = db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        return jsonify(_poll_to_dict(db, poll, user_id))
    finally:
        db.close()
