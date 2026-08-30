############################################################
#  [*] Social — profiles, friendships, wall posts, feed
#
#  The user-to-user layer over the users and news_posts
#  tables: public profiles, the friend-request state machine
#  (pending → accepted/rejected, then one friendships row
#  PER DIRECTION), wall posts (news_posts rows with
#  source='user', post_type='social') and a ranked feed of
#  them. Mounted at /api/social by create_app
#  (app/__init__.py). Auth is optional on the feed, the
#  profile-by-id and posts-by-user reads — get_current_user
#  returning None means the anonymous, public-only view;
#  everything that writes sits behind require_auth.
#
#  Wall posts live in news_posts, so likes, comments and
#  polls belong to news/routes.py — this file only creates,
#  edits and deletes the post row and reads news_likes for
#  the "liked" flag. Visibility everywhere: the author and
#  their friends see private posts, everyone else only
#  is_public = 1. Body strings are stored RAW; HTML escaping
#  happens on output in the after_request hook.
#
#  A deactivated account (users.active = 0) drops out of
#  everything here — its posts leave the feed, its profile
#  and post list answer 404 to everyone but an admin, it
#  disappears from friends lists and request lists, and it
#  cannot be sent a friend request. Every write route sits
#  behind the shared rate_limit decorator from
#  auth/routes.py.
#
#  Two routes are dead as far as the mobile app goes —
#  GET /profile (the app reads itself via GET /api/auth/me)
#  and POST /posts (the app posts through POST /api/news).
#  The rest map 1:1 onto services/api/social.ts, and
#  swagger/swagger.yaml documents every route below under
#  the "social" tag.
#
#  Blocks: one user_blocks row per (blocker, blocked) pair.
#  A pair with a row in EITHER direction cannot friend-
#  request, cannot be pulled into a new conversation
#  together, cannot message each other's DMs and drops out
#  of each other's chat user search (enforced here and in
#  chat/routes.py). Blocking also severs an existing
#  friendship and clears pending requests. Reports are the
#  complaint ledger the admin panel reads.
#
#    GET    /api/social/feed                         — ranked user-post feed
#    GET    /api/social/profile/<user_id>            — public profile + friendship status
#    GET    /api/social/profile                      — own profile (unused)
#    PUT    /api/social/profile                      — edit own profile
#    POST   /api/social/friends/request              — send (or auto-accept) a request
#    GET    /api/social/friends/requests             — pending requests, either direction
#    POST   /api/social/friends/requests/<id>/accept — accept a received request
#    POST   /api/social/friends/requests/<id>/reject — reject, or cancel a sent one
#    GET    /api/social/friends                      — the friends list
#    DELETE /api/social/friends/<user_id>            — unfriend (both directions)
#    POST   /api/social/posts                        — create a wall post (unused)
#    GET    /api/social/posts?user_id=<id>           — one user's posts
#    PUT    /api/social/posts/<post_id>              — edit own post
#    DELETE /api/social/posts/<post_id>              — delete own post
#    POST   /api/social/blocks                       — block a user
#    DELETE /api/social/blocks/<user_id>             — unblock a user
#    GET    /api/social/blocks                       — the caller's block list
#    POST   /api/social/reports                      — report a user/post/message
############################################################


import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from app.api import (
    FEED_SCORE_SQL,
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    SUMMARY_LENGTH,
    parse_pagination,
)
from app.auth.routes import get_current_user, get_json_object, rate_limit, require_auth
from app.database import get_db, utc_now_iso

logger = logging.getLogger(__name__)

social_bp = Blueprint("social", __name__)

# Feed bounds. The ranked window keeps the sort on the live
# part of the wall (a profile's own list is NOT windowed, so
# nothing becomes unreachable), and the page caps keep the
# OFFSET the routes build from page out of full-scan land —
# parse_pagination's own default would allow OFFSET 999 900.
_FEED_WINDOW_DAYS = 180
_FEED_MAX_PAGE = 200
_LIST_MAX_PAGE = 200

# Passed explicitly rather than left to parse_pagination's
# default, so the number swagger documents for these two
# routes cannot drift again when the shared default moves
_FEED_PER_PAGE_MAX = 50

# Friends / requests are returned in one generous page: the
# app sends no pagination and counts the rows it gets, so
# the default per_page IS the cap
_LIST_PER_PAGE = 200

# Friend-request quotas: 20 sends per 5-minute window (the
# rate_limit decorator's window) and, after a rejection, a
# 7-day cooldown before the same sender may ask that person
# again. Rejected rows are kept exactly that long — the
# opportunistic purge in send_friend_request drops the older
# ones
_FRIEND_REQUEST_MAX = 20
_FRIEND_REQUEST_COOLDOWN_DAYS = 7








############################################################
# _post_row_to_dict
############################################################
#
# One news_posts row → the wire shape every post-serving
# route in this file answers with. It used to be built by
# hand in three places, which is how the feed grew an
# "authorAvatar" the profile list never had and create_post
# grew a raw "isPublic" echo.
#
# The row must come from a query that joins users as "au"
# and aliases au.avatar_url / au.display_name — "author" is
# the author's CURRENT display name with the author_name
# snapshot only as the fallback for scraped rows whose
# author_id is NULL, so a rename no longer pairs a new
# avatar with an old name.
#
# truncate=True is for the LIST endpoints: "content" goes
# out trimmed to SUMMARY_LENGTH with an additive
# "truncated" flag, so a page of 100 posts stops carrying
# 100 full 10 000-char bodies. Both keys stay in the
# response — the mobile card renders summary || content and
# the post screen refetches the full body through
# GET /api/news/<id>.
#
# Used by:
#   - social_feed, create_post, get_user_posts (below)
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








############################################################
# _parse_before
############################################################
#
# The optional ?before pin, as an ISO-T UTC string, or a
# ready-made 400. The feed ranks by an age term, so between
# a client's page 1 and page 2 the scraper's inserts would
# otherwise re-rank the whole wall under the reader's feet
# and OFFSET pagination would duplicate and drop posts. A
# client that sends its page-1 request time as ?before gets
# every later page computed against that instant and
# published_at <= it; a client that omits it (the mobile app
# today) keeps the live window it has always had.
#
# A "Z" suffix and a naive value are both accepted — the
# latter is read as UTC, like every other timestamp in this
# database — and anything unparseable is a 400 with a stable
# code rather than a silently NULL score. The second attempt
# is for the "+" of a "+00:00" offset that reached us as a
# space: an un-encoded plus in a query string decodes to
# one, and answering 400 for a correct timestamp would be a
# nasty little trap.
#
# The shift to UTC is inside the guard too, and every
# failure leaves by the one 400: a pin at the very edge of
# the datetime range next to a far offset (?before=
# 0001-01-01T00:00:00+14:00 and its 9999 mirror) parses
# happily and then overflows on the conversion, which used
# to leave the route as a 500.
#
# Used by:
#   - social_feed (below)
############################################################

def _parse_before():
    raw = request.args.get("before")
    if raw is None:
        return None, None

    # STEP 1: parse — the second attempt repairs the "+" that
    # decoded to a space
    # =======================================================
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        try:
            stamp = datetime.fromisoformat(raw.replace(" ", "+"))
        except (AttributeError, ValueError):
            stamp = None


    # STEP 2: naive means UTC, then the conversion — an edge-of-
    # range pin has no UTC instant to name and falls through to
    # the same 400 as an unparseable one
    # ==========================================================
    if stamp is not None:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        try:
            return stamp.astimezone(timezone.utc).isoformat(), None
        except (OverflowError, OSError, ValueError):
            pass

    return None, (jsonify({
        "error": "before must be an ISO-8601 timestamp",
        "code": "invalid_before",
    }), 400)








############################################################
# _delete_upload_file
############################################################
#
# Best-effort removal of an /api/uploads/ file whose last
# reference just went away — a deleted post's image, a
# replaced avatar. Guarded lazy import: the helper belongs
# to the uploads package (uploads/routes.py delete_upload)
# and until it lands there this is a silent no-op, exactly
# like auth/routes.py _delete_replaced_upload. A disk-level
# failure must never fail the route that triggered it, so
# the write is already committed when this runs.
#
# Used by:
#   - update_profile, delete_post (below)
############################################################

def _delete_upload_file(path):
    if not path or not path.startswith("/api/uploads/"):
        return
    try:
        from app.uploads.routes import delete_upload
    except ImportError:
        return
    try:
        delete_upload(path)
    except Exception:
        logger.warning("Could not delete upload %s", path)








############################################################
# social_feed
############################################################
#
# GET /api/social/feed
#
# The "community" feed: user wall posts only (source =
# 'user'), ranked by recency + engagement. Logged in, the
# viewer gets their own and their friends' posts at ANY
# visibility plus every public user post; anonymous, public
# user posts only. A deactivated author's posts are nobody's.
# per_page caps at _FEED_PER_PAGE_MAX and page at
# _FEED_MAX_PAGE — far below parse_pagination's 10 000
# default, because every page is an OFFSET scan — and
# only the last _FEED_WINDOW_DAYS days
# are ranked, which bounds the sort to the live wall (a
# profile's own list, get_user_posts, stays unwindowed, so
# nothing becomes unreachable).
#
# Ranking is api/__init__.py's FEED_SCORE_SQL — the single
# copy of the formula, shared with news/routes.py — computed
# ONCE as the feed_score alias the ORDER BY names, with
# published_at DESC and finally id DESC as tie-breakers so
# equal scores keep a deterministic order across pages. The
# optional ?before (the client's page-1 request time) pins
# the formula's "now" AND adds published_at <= before, so a
# scraper insert between two pages cannot shift rows across
# the boundary; omitting it keeps the live window the app
# has always seen. julianday() copes with both published_at
# formats in the column (ISO "T" from create_post,
# datetime('now') from the scraper).
#
# Gotchas:
#   - the author is joined in (au), so the avatar costs no
#     per-row SELECT and "author" is the CURRENT display
#     name rather than create_post's snapshot.
#   - "content" is trimmed to SUMMARY_LENGTH with an
#     additive "truncated" flag — see _post_row_to_dict.
#   - "liked" is filled by one IN (...) query afterwards;
#     anonymous viewers always get liked = False.
#   - the COUNT(*) reuses where_sql and its own params, so
#     total covers the same visibility set.
#   - the friend ids are spliced in as "?" placeholders, one
#     per friend, so the statement grows with the friend
#     count (SQLite's bound-variable limit is far away).
#
# Used by:
#   - services/api/social.ts — fetchSocialFeed
#   - app/(main)/tabs/news.tsx — the "community" feed mode,
#     called for guests too: the anonymous branch serves them
#     the public-only view, an account only ADDS friends' rows
############################################################

@social_bp.route("/feed", methods=["GET"])
def social_feed():
    # STEP 1: pagination (its own page cap), the optional
    # ?before pin, and the optional viewer — None means the
    # anonymous, public-only view
    # ======================================================
    page, per_page, err = parse_pagination(
        max_per_page=_FEED_PER_PAGE_MAX,
        max_page=_FEED_MAX_PAGE,
    )
    if err:
        return err
    offset = (page - 1) * per_page

    before, err = _parse_before()
    if err:
        return err

    current_user = get_current_user()

    db = get_db()
    try:


        # STEP 2: visibility WHERE — own + friends' posts at any
        # visibility, or public-only for guests; plus the active
        # author and recency-window floors everybody pays
        # ======================================================
        where_clauses = [
            "p.source = 'user'",
            # LEFT JOIN, so a NULL author (scraped/hand-inserted
            # row) passes; only an explicit active = 0 is filtered
            "COALESCE(au.active, 1) = 1",
            f"p.published_at > date('now', '-{_FEED_WINDOW_DAYS} day')",
        ]
        where_params = []

        if current_user:
            friend_ids = [
                r["friend_id"]
                for r in db.execute(
                    "SELECT friend_id FROM friendships WHERE user_id = ?",
                    (current_user["id"],),
                ).fetchall()
            ]
            visible_ids = [current_user["id"]] + friend_ids
            placeholders = ",".join(["?"] * len(visible_ids))
            where_clauses.append(
                f"(p.author_id IN ({placeholders}) OR p.is_public = 1)"
            )
            where_params.extend(visible_ids)
        else:
            where_clauses.append("p.is_public = 1")

        if before:
            where_clauses.append("p.published_at <= ?")
            where_params.append(before)

        where_sql = " AND ".join(where_clauses)


        # STEP 3: the ranked page — the score computed once as an
        # alias the ORDER BY names, the author joined in. The pin
        # is bound TWICE when present (score + window), which is
        # why its params ride in their own list
        # =======================================================
        score_sql = FEED_SCORE_SQL.format(now="?" if before else "'now'")
        score_params = [before] if before else []

        query = f"""
            SELECT p.*,
                   au.avatar_url AS author_avatar,
                   au.display_name AS author_display_name,
                   {score_sql} AS feed_score
            FROM news_posts p
            LEFT JOIN users au ON au.id = p.author_id
            WHERE {where_sql}
            ORDER BY feed_score DESC, p.published_at DESC, p.id DESC
            LIMIT ? OFFSET ?
        """
        rows = db.execute(
            query,
            score_params + where_params + [per_page, offset],
        ).fetchall()


        # STEP 4: the one wire shape, bodies trimmed for a list
        # =====================================================
        posts = [_post_row_to_dict(row, truncate=True) for row in rows]


        # STEP 5: "liked" flags for the viewer in one IN (...) query
        # ==========================================================
        if current_user and posts:
            post_ids = [p["id"] for p in posts]
            ph = ",".join(["?"] * len(post_ids))
            liked = db.execute(
                f"SELECT post_id FROM news_likes WHERE user_id = ? AND post_id IN ({ph})",
                [current_user["id"]] + post_ids,
            ).fetchall()
            liked_set = {r["post_id"] for r in liked}
            for p in posts:
                p["liked"] = p["id"] in liked_set


        # STEP 6: total for hasMore — same WHERE and join, and
        # only the WHERE's own params
        # ====================================================
        total = db.execute(
            f"""SELECT COUNT(*) as c
                FROM news_posts p
                LEFT JOIN users au ON au.id = p.author_id
                WHERE {where_sql}""",
            where_params,
        ).fetchone()["c"]

        return jsonify({
            "posts": posts,
            "page": page,
            "perPage": per_page,
            "total": total,
            "hasMore": offset + per_page < total,
        })
    finally:
        db.close()








############################################################
# get_profile
############################################################
#
# GET /api/social/profile/<user_id>
#
# Anyone's public profile — no auth needed. A deactivated
# account (users.active = 0) is a 404 for everyone except an
# admin, who still needs to reach it from the admin screens.
# postCount covers the user's own posts of source 'user'
# AND 'faculty' (staff publish through POST /api/news as
# 'faculty' — without it a curator's profile showed zero),
# and it applies the same visibility split as GET /posts:
# private posts count only for the author and accepted
# friends, so the stat always matches the visible list.
# friendCount leaves deactivated accounts out for the same
# reason — list_friends already hides them, and the two
# numbers sit next to each other on the profile screen.
# friendshipStatus is from the VIEWER's side: 'friends',
# 'request_sent' (the viewer asked), 'request_received'
# (the profile owner asked) or 'none' — also 'none' for
# anonymous viewers and for a user looking at their own
# profile. The viewer and the friendship row are resolved
# ONCE, up front — postCount and friendshipStatus share the
# lookup.
#
# Used by:
#   - services/api/social.ts — fetchUserProfile
#   - app/(main)/profile/index.tsx — profile header and the
#     add / accept / unfriend action button
############################################################

@social_bp.route("/profile/<user_id>", methods=["GET"])
def get_profile(user_id):
    db = get_db()
    try:
        user = db.execute(
            "SELECT id, username, display_name, avatar_url, role, created_at, active FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404

        # The viewer first — postCount's visibility split and
        # friendshipStatus both hang off this one lookup (the
        # friendships row is checked in the viewer's direction only)
        current_user = get_current_user()

        # A deactivated account reads as gone; an admin keeps
        # seeing it (the admin screens link here)
        if not user["active"] and not (current_user and current_user["role"] == "admin"):
            return jsonify({"error": "User not found"}), 404

        is_friend = None
        if current_user and current_user["id"] != user_id:
            is_friend = db.execute(
                "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
                (current_user["id"], user_id),
            ).fetchone()

        # Same rule as get_user_posts: the author and accepted
        # friends see private posts, everyone else public only —
        # and 'faculty' rows count alongside 'user' ones
        can_see_private = bool(current_user) and (
            current_user["id"] == user_id or bool(is_friend)
        )
        if can_see_private:
            post_count = db.execute(
                "SELECT COUNT(*) as c FROM news_posts WHERE author_id = ? AND source IN ('user', 'faculty')",
                (user_id,),
            ).fetchone()["c"]
        else:
            post_count = db.execute(
                "SELECT COUNT(*) as c FROM news_posts WHERE author_id = ? AND source IN ('user', 'faculty') AND is_public = 1",
                (user_id,),
            ).fetchone()["c"]

        # Deactivated friends are counted out, exactly as list_friends
        # leaves them out of the list the number sits next to
        friend_count = db.execute(
            "SELECT COUNT(*) as c FROM friendships f JOIN users u ON f.friend_id = u.id"
            " WHERE f.user_id = ? AND u.active = 1",
            (user_id,),
        ).fetchone()["c"]

        # Whether the VIEWER has blocked this profile — the
        # profile stays readable (a block silences contact, it
        # does not erase people), but the client needs the flag
        # to offer "unblock" instead of "block"
        blocked_by_me = False
        if current_user and current_user["id"] != user_id:
            blocked_by_me = bool(db.execute(
                "SELECT 1 FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?",
                (current_user["id"], user_id),
            ).fetchone())

        # A pending request in either direction decides sent/received
        friendship_status = "none"
        if current_user and current_user["id"] != user_id:
            if is_friend:
                friendship_status = "friends"
            else:
                pending = db.execute(
                    "SELECT id, from_user_id FROM friend_requests WHERE status = 'pending' AND "
                    "((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))",
                    (current_user["id"], user_id, user_id, current_user["id"]),
                ).fetchone()
                if pending:
                    friendship_status = "request_sent" if pending["from_user_id"] == current_user["id"] else "request_received"

        return jsonify({
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
    finally:
        db.close()








############################################################
# get_own_profile
############################################################
#
# GET /api/social/profile
#
# The caller's own profile with post and friend counts —
# the private twin of get_profile (adds email and the
# student-card fields, has no friendshipStatus).
# request.user is the dict resolve_session_token built, so
# .get() covers the migration-v5 columns — and created_at is
# NOT among the columns that lookup narrowed to, hence the
# small SELECT here rather than a KeyError. postCount counts
# source 'user' AND 'faculty' like get_profile and
# get_user_posts do: a staff member publishing through
# POST /api/news as 'faculty' saw their own profile claim
# zero posts next to a list that showed them. No mobile
# caller: the app reads its own identity through
# GET /api/auth/me (services/api/auth.ts fetchMe).
#
# Used by:
#   - nothing calls this at the moment
############################################################

@social_bp.route("/profile", methods=["GET"])
@require_auth
def get_own_profile():
    user = request.user
    db = get_db()
    try:
        row = db.execute(
            "SELECT created_at FROM users WHERE id = ?",
            (user["id"],),
        ).fetchone()

        post_count = db.execute(
            "SELECT COUNT(*) as c FROM news_posts WHERE author_id = ? AND source IN ('user', 'faculty')",
            (user["id"],),
        ).fetchone()["c"]

        # Same rule as get_profile and list_friends: a deactivated
        # account is out of the friends list, so it is out of the count
        friend_count = db.execute(
            "SELECT COUNT(*) as c FROM friendships f JOIN users u ON f.friend_id = u.id"
            " WHERE f.user_id = ? AND u.active = 1",
            (user["id"],),
        ).fetchone()["c"]

        return jsonify({
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
    finally:
        db.close()








############################################################
# update_profile
############################################################
#
# PUT /api/social/profile
#
# Edits the caller's own users row — display name, avatar
# and the migration-v5 student-card fields (student_number,
# study_group, study_program). This is the profile editor
# the app actually calls; auth/routes.py update_me is the
# unused parallel implementation. Keys are accepted in
# camelCase (what the GETs return) or snake_case (what the
# app sends), camelCase winning when both are present.
#
# Gotchas:
#   - a present-but-blank displayName is a 400, not a silent
#     skip answering 200 with the old name; an absent key is
#     simply not touched, and a body with NO usable field is
#     still 400.
#   - avatar_url must be a relative /api/uploads/ path —
#     an absolute URL would beacon every list that renders
#     the avatar to a host the user picked; null and ""
#     still clear it. The before_request hook in
#     app/__init__.py enforces the same rule on both key
#     spellings, this check is the per-route belt. The
#     replaced own upload is deleted from disk after the
#     commit — including when the row vanished mid-request
#     and the answer is the session-dead 401, since the
#     commit already dropped the file's last reference.
#   - student fields: strings only (a non-string is a 400
#     naming the key the client sent, never str()-coerced),
#     blank → NULL, over 50 chars → 400.
#   - a display-name change rewrites the author_name
#     snapshot on this user's news_posts rows in the SAME
#     transaction, so old posts stop pairing the new avatar
#     with the old name.
#   - updated_at is stamped with utc_now_iso(), the one
#     T-form UTC shape migration v17 normalised the column
#     to (the space-form DEFAULT sorts wrong against it and
#     must never fire).
#   - the response is a hand-picked subset (no createdAt,
#     no active) — the app merges it into its cached user
#     instead of replacing it; `invited` is in it so that
#     merge can no longer drop the trust flag.
#
# Used by:
#   - services/api/social.ts — updateProfile
#   - app/(main)/tabs/id.tsx — student-card field edits
#   - app/(main)/profile/index.tsx — avatar / name edits
############################################################

@social_bp.route("/profile", methods=["PUT"])
@require_auth
@rate_limit("profile", max_attempts=30)
def update_profile():
    # STEP 1: body — a JSON array or scalar is a 400, not an
    # AttributeError 500 on data.get
    # ======================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:


        # STEP 2: collect "col = ?" fragments — display name and
        # avatar first, camelCase key winning over snake_case
        # ======================================================
        updates = []
        params = []
        new_display_name = None
        replaced_avatar = None
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
        av_key = "avatarUrl" if "avatarUrl" in data else "avatar_url"
        if av_key in data:
            # Own uploads or clearing only — see the banner
            av = data[av_key]
            if av not in (None, "") and (not isinstance(av, str) or not av.startswith("/api/uploads/")):
                return jsonify({"error": "avatar_url must be a relative /api/uploads/ path"}), 400
            updates.append("avatar_url = ?")
            params.append(av)
            old_avatar = request.user.get("avatar_url")
            if old_avatar and old_avatar != av:
                replaced_avatar = old_avatar

        # STEP 2.1: student-card fields — col comes from this
        # literal list, never from the client, so the f-string is safe
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


        # STEP 3: one UPDATE, the author_name snapshots with it,
        # then re-read the row for the response
        # ======================================================
        updates.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(request.user["id"])

        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        if new_display_name:
            # Same transaction as the rename — posts never show a
            # half-renamed author (auth/routes.py update_me does
            # the same on its side)
            db.execute(
                "UPDATE news_posts SET author_name = ? WHERE author_id = ?",
                (new_display_name, request.user["id"]),
            )
        db.commit()

        user = db.execute("SELECT * FROM users WHERE id = ?", (request.user["id"],)).fetchone()

        # The UPDATE is committed, so the replaced file has lost
        # its last reference either way — the cleanup runs BEFORE
        # the session-dead exit, or a row deleted mid-request
        # would leave the old avatar on disk forever
        if replaced_avatar:
            _delete_upload_file(replaced_avatar)

        if user is None:
            # The row vanished between the auth check and the
            # re-read — the session-dead 401 the client handles
            return jsonify({"error": "Authentication required"}), 401

        return jsonify({
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
    finally:
        db.close()








############################################################
# send_friend_request
############################################################
#
# POST /api/social/friends/request
#
# Body {"user_id"}. Creates a pending friend_requests row —
# unless the target already asked US, in which case their
# request is accepted on the spot (200 "accepted", both
# friendships rows written, their request row deleted)
# instead of a second pending row. 400 self-request or a
# non-string user_id (a JSON number used to reach sqlite3 as
# an unbindable type and 500), 404 unknown or deactivated
# target, 409 already friends or a request already pending
# in either direction.
#
# Two brakes on request spam, since nothing else stops one
# account from carpet-bombing the faculty: the shared
# rate_limit decorator (_FRIEND_REQUEST_MAX per 5-minute
# window, keyed on the sender) and a per-pair cooldown —
# once the target has DECLINED, the same sender waits
# _FRIEND_REQUEST_COOLDOWN_DAYS days before asking that
# person again (a sender who cancels their own request
# leaves no row, so it never binds them — see
# reject_friend_request), answered 429 with the stable code
# "friend_request_cooldown". Rejected rows are kept exactly
# for that window and the older ones are purged
# opportunistically here, so friend_requests stops being a
# permanent record of who declined whom; an accept on either
# path clears the pair's rejections outright, since two
# people who became friends have settled the decline. A
# block in EITHER direction answers the same 404 an unknown
# id gets — whether and why a request cannot be delivered is
# not the requester's business.
#
# The "already friends" check reads only the (me, target)
# direction — fine while accept / auto-accept always write
# both rows. STEP 4's insert can still lose a race against
# a second tab, so migration v21's partial unique index
# decides it and the IntegrityError becomes the same 409.
#
# Used by:
#   - services/api/social.ts — sendFriendRequest
#   - app/(main)/profile/index.tsx — the "add friend" action
############################################################

@social_bp.route("/friends/request", methods=["POST"])
@require_auth
@rate_limit("friendreq", max_attempts=_FRIEND_REQUEST_MAX)
def send_friend_request():
    # STEP 1: body + the type and self-request guards
    # ===============================================
    data = get_json_object()
    if not data or not data.get("user_id"):
        return jsonify({"error": "user_id required"}), 400

    target_id = data["user_id"]
    my_id = request.user["id"]

    if not isinstance(target_id, str):
        return jsonify({"error": "user_id must be a string"}), 400

    if target_id == my_id:
        return jsonify({"error": "Cannot friend yourself"}), 400

    db = get_db()
    try:


        # STEP 2: target must exist, be active, and not already
        # be a friend
        # =====================================================
        target = db.execute(
            "SELECT id, display_name, active FROM users WHERE id = ?",
            (target_id,),
        ).fetchone()
        if not target or not target["active"]:
            return jsonify({"error": "User not found"}), 404

        # A blocked pair (either direction) reads as missing —
        # the 404 above, not a distinguishable refusal
        blocked = db.execute(
            "SELECT 1 FROM user_blocks WHERE (blocker_id = ? AND blocked_id = ?) "
            "OR (blocker_id = ? AND blocked_id = ?)",
            (my_id, target_id, target_id, my_id),
        ).fetchone()
        if blocked:
            return jsonify({"error": "User not found"}), 404

        existing = db.execute(
            "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
            (my_id, target_id),
        ).fetchone()
        if existing:
            return jsonify({"error": "Already friends"}), 409


        # STEP 3: a pending request in either direction — theirs
        # is accepted on the spot (the row is the handshake, the
        # two friendships rows are the state of record, so it
        # goes), ours is a duplicate (409)
        # ======================================================
        pending = db.execute(
            "SELECT id, from_user_id FROM friend_requests WHERE status = 'pending' AND "
            "((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))",
            (my_id, target_id, target_id, my_id),
        ).fetchone()

        if pending:
            if pending["from_user_id"] == target_id:
                # OR IGNORE: a mutual send can leave one direction
                # already written, and the primary key must not 500.
                # created_at is stamped for the same reason STEP 4
                # stamps its own: the column's DEFAULT writes
                # space-form text and list_friends serves it raw
                since = utc_now_iso()
                db.execute(
                    "INSERT OR IGNORE INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
                    (my_id, target_id, since),
                )
                db.execute(
                    "INSERT OR IGNORE INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
                    (target_id, my_id, since),
                )
                db.execute("DELETE FROM friend_requests WHERE id = ?", (pending["id"],))
                # Friendship outranks a decline: a rejected row this
                # pair has since grown past would otherwise go on
                # firing STEP 3.1's cooldown after they unfriend
                db.execute(
                    "DELETE FROM friend_requests WHERE status = 'rejected' AND "
                    "((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))",
                    (my_id, target_id, target_id, my_id),
                )
                db.commit()
                return jsonify({"status": "accepted", "message": "Friend request auto-accepted (they already requested you)"}), 200
            return jsonify({"error": "Friend request already pending"}), 409


        # STEP 3.1: the rejection cooldown, and the purge of every
        # rejected row that has outlived it
        # ========================================================
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_FRIEND_REQUEST_COOLDOWN_DAYS)
        ).isoformat()

        recent_rejection = db.execute(
            "SELECT 1 FROM friend_requests WHERE status = 'rejected' AND from_user_id = ? "
            "AND to_user_id = ? AND COALESCE(updated_at, created_at) > ?",
            (my_id, target_id, cutoff),
        ).fetchone()
        if recent_rejection:
            return jsonify({
                "error": "This person declined your last request. Please try again later.",
                "code": "friend_request_cooldown",
            }), 429

        db.execute(
            "DELETE FROM friend_requests WHERE status = 'rejected' "
            "AND COALESCE(updated_at, created_at) <= ?",
            (cutoff,),
        )


        # STEP 4: a fresh pending row — the unique index on the
        # pending pair (migration v21) settles a lost race. The
        # stamps are explicit: the columns' datetime('now')
        # DEFAULT writes space-form text, which sorts below every
        # T-form value of the same day (migration v17 left plenty
        # in this very column) and put an older request on top of
        # a newer one in list_friend_requests
        # =====================================================
        req_id = str(uuid.uuid4())
        now = utc_now_iso()
        try:
            db.execute(
                "INSERT INTO friend_requests (id, from_user_id, to_user_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (req_id, my_id, target_id, now, now),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return jsonify({"error": "Friend request already pending"}), 409

        return jsonify({"id": req_id, "status": "pending"}), 201
    finally:
        db.close()








############################################################
# list_friend_requests
############################################################
#
# GET /api/social/friends/requests
#
# Pending requests only, newest first, with the OTHER
# party's public columns joined in — deactivated accounts
# are left out of both directions. ?direction=sent lists
# what the caller sent, ?direction=received (the default)
# what they received, and anything else is a 400 with the
# code "invalid_direction" instead of silently meaning
# received. The request id is what accept / reject take —
# the profile screen fetches this list purely to find the id
# before accepting.
#
# Paging is optional and generous: page/per_page ride
# through parse_pagination with _LIST_PER_PAGE as both the
# default and the cap, so the app's un-paged call still gets
# every request it would have got, and the additive total /
# hasMore keys let the badge stop counting rows client-side.
# The {"requests": [...]} envelope is unchanged — the mobile
# contract is frozen.
#
# Used by:
#   - services/api/social.ts — fetchFriendRequests
#   - app/(main)/friend-requests/index.tsx — the received
#     list
#   - app/(main)/friends/index.tsx — pending-count badge
#   - app/(main)/profile/index.tsx — resolve the request id
#     before accepting (direction=received) or cancelling a
#     sent request (direction=sent) from the profile
############################################################

@social_bp.route("/friends/requests", methods=["GET"])
@require_auth
def list_friend_requests():
    # STEP 1: direction + optional paging
    # ===================================
    direction = request.args.get("direction", "received")
    if direction not in ("sent", "received"):
        return jsonify({
            "error": "direction must be 'sent' or 'received'",
            "code": "invalid_direction",
        }), 400

    page, per_page, err = parse_pagination(
        max_per_page=_LIST_PER_PAGE,
        default_per_page=_LIST_PER_PAGE,
        max_page=_LIST_MAX_PAGE,
    )
    if err:
        return err
    offset = (page - 1) * per_page

    db = get_db()
    try:


        # STEP 2: the page — the two directions differ only in
        # which end of the row is the caller and which is joined.
        # Both names come from this literal branch, never from
        # the client, so the f-string is safe
        # =======================================================
        if direction == "sent":
            mine_col, other_col = "fr.from_user_id", "fr.to_user_id"
        else:
            mine_col, other_col = "fr.to_user_id", "fr.from_user_id"

        rows = db.execute(
            f"""SELECT fr.id, {other_col} as user_id, fr.created_at,
                       u.display_name, u.username, u.avatar_url, u.role
                FROM friend_requests fr
                JOIN users u ON {other_col} = u.id
                WHERE {mine_col} = ? AND fr.status = 'pending' AND u.active = 1
                ORDER BY fr.created_at DESC, fr.id DESC
                LIMIT ? OFFSET ?""",
            (request.user["id"], per_page, offset),
        ).fetchall()

        requests_list = [
            {
                "id": r["id"],
                "userId": r["user_id"],
                "displayName": r["display_name"],
                "username": r["username"],
                "avatarUrl": r["avatar_url"],
                "role": r["role"],
                "createdAt": r["created_at"],
            }
            for r in rows
        ]


        # STEP 3: the same set counted, for the additive
        # total / hasMore keys
        # ==============================================
        total = db.execute(
            f"""SELECT COUNT(*) as c
                FROM friend_requests fr
                JOIN users u ON {other_col} = u.id
                WHERE {mine_col} = ? AND fr.status = 'pending' AND u.active = 1""",
            (request.user["id"],),
        ).fetchone()["c"]

        return jsonify({
            "requests": requests_list,
            "total": total,
            "hasMore": offset + per_page < total,
        })
    finally:
        db.close()








############################################################
# accept_friend_request
############################################################
#
# POST /api/social/friends/requests/<request_id>/accept
#
# Only the RECIPIENT of a still-pending request can accept
# (anything else is a 404, not a 403). Writes the
# friendships row in both directions and DELETES the request
# in one commit — the two friendships rows are the state of
# record, so keeping a settled handshake row forever only
# grew a table nothing reads (the same is true of the
# auto-accept path in send_friend_request). Every reader
# (feeds, counts, unfriend) only ever looks up its own
# direction, so both rows must exist. The inserts are OR
# IGNORE: a mutual-send race can leave a second pending row
# for a pair that already became friends when the first one
# was accepted, and accepting that leftover must settle it
# instead of 500ing on the (user_id, friend_id) primary key.
#
# Every 'rejected' row between the two goes in the same
# commit (the auto-accept path does it too): those rows are
# what send_friend_request's per-pair cooldown reads, and a
# decline the pair has since grown past must not lock a
# re-add for the rest of the window once they unfriend.
# created_at is stamped with utc_now_iso() rather than left
# to the column's datetime('now') DEFAULT — the space form
# it writes is what list_friends would then serve as
# friendsSince, and every other timestamp this API answers
# with is T-form UTC.
#
# Used by:
#   - services/api/social.ts — acceptFriendRequest
#   - app/(main)/friend-requests/index.tsx — accept button
#   - app/(main)/profile/index.tsx — accept from the profile
#     ('request_received' state)
############################################################

@social_bp.route("/friends/requests/<request_id>/accept", methods=["POST"])
@require_auth
@rate_limit("friendaction", max_attempts=60)
def accept_friend_request(request_id):
    db = get_db()
    try:
        fr = db.execute(
            "SELECT * FROM friend_requests WHERE id = ? AND to_user_id = ? AND status = 'pending'",
            (request_id, request.user["id"]),
        ).fetchone()
        if not fr:
            return jsonify({"error": "Friend request not found"}), 404

        # Both directions — nothing ever queries the reverse
        # one. OR IGNORE keeps the accept idempotent when the
        # rows already exist, and created_at is stamped rather
        # than left to the space-form DEFAULT (see the banner)
        since = utc_now_iso()
        db.execute(
            "INSERT OR IGNORE INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
            (fr["from_user_id"], fr["to_user_id"], since),
        )
        db.execute(
            "INSERT OR IGNORE INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
            (fr["to_user_id"], fr["from_user_id"], since),
        )
        # The handshake is over; the friendships rows carry it now
        db.execute("DELETE FROM friend_requests WHERE id = ?", (request_id,))
        # And so does any earlier decline between the two — see
        # the banner
        db.execute(
            "DELETE FROM friend_requests WHERE status = 'rejected' AND "
            "((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))",
            (fr["from_user_id"], fr["to_user_id"], fr["to_user_id"], fr["from_user_id"]),
        )
        db.commit()

        return jsonify({"status": "accepted"})
    finally:
        db.close()








############################################################
# reject_friend_request
############################################################
#
# POST /api/social/friends/requests/<request_id>/reject
#
# Either party may call it on a pending request and both
# answer {"status": "rejected"} — the wire shape the app
# reads — but they settle the row differently: the
# RECIPIENT declining leaves it as status 'rejected', the
# record send_friend_request's per-pair cooldown reads,
# while the SENDER cancelling deletes it. A withdrawal is
# not a rejection: keeping it locked the sender out of that
# pair for the whole cooldown and told them the other person
# had declined, and the app's 'request_sent' button IS this
# route. 404 for an unknown, foreign or already-settled
# request. A kept rejection lives exactly as long as the
# cooldown — send_friend_request's opportunistic purge drops
# the older ones, so this is no longer a permanent record of
# who declined whom.
#
# Used by:
#   - services/api/social.ts — rejectFriendRequest
#   - app/(main)/friend-requests/index.tsx — decline button
#     (the recipient path)
#   - app/(main)/profile/index.tsx — the 'request_sent'
#     button cancels the pending request here (the sender
#     path doubles as the app's cancel-sent UI)
############################################################

@social_bp.route("/friends/requests/<request_id>/reject", methods=["POST"])
@require_auth
@rate_limit("friendaction", max_attempts=60)
def reject_friend_request(request_id):
    db = get_db()
    try:
        fr = db.execute(
            "SELECT * FROM friend_requests WHERE id = ? AND (to_user_id = ? OR from_user_id = ?) AND status = 'pending'",
            (request_id, request.user["id"], request.user["id"]),
        ).fetchone()
        if not fr:
            return jsonify({"error": "Friend request not found"}), 404

        if fr["from_user_id"] == request.user["id"]:
            # A cancel, not a rejection — no cooldown record (see
            # the banner)
            db.execute("DELETE FROM friend_requests WHERE id = ?", (request_id,))
        else:
            db.execute(
                "UPDATE friend_requests SET status = 'rejected', updated_at = ? WHERE id = ?",
                (utc_now_iso(), request_id),
            )
        db.commit()

        return jsonify({"status": "rejected"})
    finally:
        db.close()








############################################################
# list_friends
############################################################
#
# GET /api/social/friends
#
# The caller's friends with friendsSince (the friendships
# row's created_at, i.e. the accept time — T-form UTC, both
# accept paths stamp it rather than letting the column's
# space-form DEFAULT fire). Deactivated accounts are left
# out.
#
# ORDER BY u.display_name COLLATE NOCASE is the server
# floor: SQLite's default BINARY collation sorts uppercase
# before lowercase and every Lithuanian letter after all of
# ASCII, so the raw order is not what a user would call
# alphabetical. NOCASE only folds ASCII case — real
# Lithuanian collation lives in the friends screen, which
# re-sorts with Intl.Collator(activeLocale()). u.id breaks
# ties so paging stays deterministic.
#
# Paging is optional and generous, exactly like
# list_friend_requests: _LIST_PER_PAGE is both default and
# cap, the {"friends": [...]} envelope is unchanged, and
# total / hasMore are additive.
#
# Used by:
#   - services/api/social.ts — fetchFriends
#   - app/(main)/friends/index.tsx — the friends list
############################################################

@social_bp.route("/friends", methods=["GET"])
@require_auth
def list_friends():
    # STEP 1: optional paging
    # =======================
    page, per_page, err = parse_pagination(
        max_per_page=_LIST_PER_PAGE,
        default_per_page=_LIST_PER_PAGE,
        max_page=_LIST_MAX_PAGE,
    )
    if err:
        return err
    offset = (page - 1) * per_page

    db = get_db()
    try:


        # STEP 2: the page, alphabetical as far as SQLite can
        # ===================================================
        rows = db.execute(
            """SELECT u.id, u.username, u.display_name, u.avatar_url, u.role, f.created_at as friends_since
               FROM friendships f
               JOIN users u ON f.friend_id = u.id
               WHERE f.user_id = ? AND u.active = 1
               ORDER BY u.display_name COLLATE NOCASE, u.id
               LIMIT ? OFFSET ?""",
            (request.user["id"], per_page, offset),
        ).fetchall()

        friends = [
            {
                "id": r["id"],
                "username": r["username"],
                "displayName": r["display_name"],
                "avatarUrl": r["avatar_url"],
                "role": r["role"],
                "friendsSince": r["friends_since"],
            }
            for r in rows
        ]


        # STEP 3: the same set counted, for the additive
        # total / hasMore keys
        # ==============================================
        total = db.execute(
            """SELECT COUNT(*) as c
               FROM friendships f
               JOIN users u ON f.friend_id = u.id
               WHERE f.user_id = ? AND u.active = 1""",
            (request.user["id"],),
        ).fetchone()["c"]

        return jsonify({
            "friends": friends,
            "total": total,
            "hasMore": offset + per_page < total,
        })
    finally:
        db.close()








############################################################
# unfriend
############################################################
#
# DELETE /api/social/friends/<user_id>
#
# Removes both friendships rows in one commit; 404 only when
# the DELETE matched nothing at all. The old pre-check read
# just the (me, them) row, so a half-present friendship —
# one direction written, the other lost to a crash or a
# hand-edit — could never be cleared from either side: the
# owner of the missing row got a 404 while the leftover row
# kept them in the other user's list. The accept path
# deletes its friend_requests row, so nothing blocks a new
# request either.
#
# Used by:
#   - services/api/social.ts — unfriendUser
#   - app/(main)/profile/index.tsx — remove-friend action
#     (behind a ConfirmDialog)
############################################################

@social_bp.route("/friends/<user_id>", methods=["DELETE"])
@require_auth
@rate_limit("friendaction", max_attempts=60)
def unfriend(user_id):
    db = get_db()
    try:
        my_id = request.user["id"]
        cur = db.execute(
            "DELETE FROM friendships WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)",
            (my_id, user_id, user_id, my_id),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Not friends"}), 404
        db.commit()

        return jsonify({"status": "unfriended"})
    finally:
        db.close()








############################################################
# create_post
############################################################
#
# POST /api/social/posts
#
# Inserts a wall post: source 'user', post_type 'social',
# summary = the first SUMMARY_LENGTH chars, title = the
# given one or the first 80 chars of content. author_name is
# snapshotted from the caller's display_name (update_profile
# rewrites it on a rename). is_public must be a real JSON
# boolean — a truthy string used to make a post public while
# the 201 echoed the raw value back, so "false" both
# published the post and answered off-contract; the response
# now always carries the stored boolean. image_url must be a
# relative /api/uploads/ path, absent, null or "" — the same
# beacon guard as the news twin's, and every other value
# (including a falsy non-string like [] or False) is a 400
# rather than an unbindable INSERT. published_at, created_at
# and updated_at all receive the same utc_now_iso().
#
# The 201 body is the re-read row through _post_row_to_dict,
# the same producer the feed and the profile list use, so
# the shape can no longer drift (it gained sourceUrl: null
# that way — additive, and what every read path already
# answered).
#
# Not used by the app: create-post/index.tsx posts through
# POST /api/news (news/routes.py create_post), which is the
# same insert plus role-based source / post_type and the
# poll attachment. The two handlers stay separate ON PURPOSE
# — that split is the semantics (a wall post is always
# source 'user'; /api/news maps staff to 'faculty' and hooks
# polls) — but every NUMBER they enforce (MAX_TITLE_LENGTH,
# MAX_CONTENT_LENGTH, SUMMARY_LENGTH) now comes from
# app/api/__init__.py in both files, so the rules can no
# longer drift by a missed hand-sync.
#
# Used by:
#   - nothing calls this at the moment
############################################################

@social_bp.route("/posts", methods=["POST"])
@require_auth
@rate_limit("post", max_attempts=20)
def create_post():
    # STEP 1: body + content — a non-string is 400, empty is 400
    # ==========================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    raw_content = data.get("content")
    if raw_content is not None and not isinstance(raw_content, str):
        return jsonify({"error": "content must be a string"}), 400
    content = (raw_content or "").strip()
    if not content:
        return jsonify({"error": "Post content required"}), 400


    # STEP 2: title (falls back to the first 80 chars of content)
    # and the length limits
    # ===========================================================
    raw_title = data.get("title")
    if raw_title is not None and not isinstance(raw_title, str):
        return jsonify({"error": "title must be a string"}), 400
    title = (raw_title or "").strip() or content[:80]

    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}), 400
    if len(content) > MAX_CONTENT_LENGTH:
        return jsonify({"error": f"Content must be at most {MAX_CONTENT_LENGTH} characters"}), 400

    # Stored raw on purpose — HTML escaping happens on output
    # (the after_request hook in app/__init__.py)

    image_url = data.get("image_url")

    # A real boolean or nothing — the documented type, and the
    # only way the stored flag and the echoed one can agree
    is_public = data.get("is_public", True)
    if not isinstance(is_public, bool):
        return jsonify({"error": "is_public must be a boolean"}), 400

    # Own uploads only — a foreign host would beacon every
    # reader's IP/UA to whoever the author picked. Absent, null
    # and "" are the only non-paths that pass: a truthiness test
    # here let [] and {} through to the INSERT (an unbindable
    # type, i.e. a 500) and stored False / 0 as an off-contract
    # imageUrl (update_post's guard has always had this shape)
    if image_url not in (None, "") and (not isinstance(image_url, str) or not image_url.startswith("/api/uploads/")):
        return jsonify({"error": "image_url must be a relative /api/uploads/ path"}), 400


    # STEP 3: insert, then answer the re-read row so the 201 and
    # every later read come out of the one producer
    # ==========================================================
    post_id = str(uuid.uuid4())
    now = utc_now_iso()

    db = get_db()
    try:
        db.execute(
            """INSERT INTO news_posts
               (id, title, content, summary, image_url, author_id, author_name,
                source, source_url, post_type, is_public, published_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'user', NULL, 'social', ?, ?, ?, ?)""",
            (post_id, title, content, content[:SUMMARY_LENGTH], image_url,
             request.user["id"], request.user["display_name"],
             1 if is_public else 0, now, now, now),
        )
        db.commit()

        row = db.execute(
            """SELECT p.*,
                      au.avatar_url AS author_avatar,
                      au.display_name AS author_display_name
               FROM news_posts p
               LEFT JOIN users au ON au.id = p.author_id
               WHERE p.id = ?""",
            (post_id,),
        ).fetchone()

        return jsonify(_post_row_to_dict(row)), 201
    finally:
        db.close()








############################################################
# get_user_posts
############################################################
#
# GET /api/social/posts?user_id=<id>
#
# One user's posts, newest first, paged like the feed
# (the same per_page cap, page at _LIST_MAX_PAGE) — source
# 'user' AND 'faculty', so a staff member's announcements
# (published through POST /api/news as 'faculty') appear on
# their own profile alongside wall posts. No recency window
# here: the feed's is what keeps the ranked sort bounded,
# and a profile must stay able to reach its own history.
# Private posts are included only when the viewer IS that
# user or their friend (checked in the viewer's friendships
# direction); anyone else — anonymous included — gets
# is_public = 1 only. 404 for an unknown user, and for a
# deactivated one unless the viewer is an admin. The author
# is joined in like the feed does, so "author" is the
# current display name and "authorAvatar" comes for free;
# "content" is trimmed to SUMMARY_LENGTH with the additive
# "truncated" flag. "liked" is filled the same way as in
# social_feed. The public/private branches differ by one
# WHERE fragment, built once and shared by the page query
# and the COUNT.
#
# Used by:
#   - services/api/social.ts — fetchUserPosts
#   - app/(main)/profile/index.tsx — the profile post list
############################################################

@social_bp.route("/posts", methods=["GET"])
def get_user_posts():
    # STEP 1: user_id, pagination, optional viewer
    # ============================================
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id query param required"}), 400

    page, per_page, err = parse_pagination(
        max_per_page=_FEED_PER_PAGE_MAX,
        max_page=_LIST_MAX_PAGE,
    )
    if err:
        return err
    offset = (page - 1) * per_page

    current_user = get_current_user()

    db = get_db()
    try:


        # STEP 2: target must exist and be active (an admin still
        # sees a deactivated one); private posts only for self or
        # a friend
        # =======================================================
        target = db.execute("SELECT id, active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            return jsonify({"error": "User not found"}), 404
        if not target["active"] and not (current_user and current_user["role"] == "admin"):
            return jsonify({"error": "User not found"}), 404

        can_see_private = False
        if current_user:
            if current_user["id"] == user_id:
                can_see_private = True
            else:
                is_friend = db.execute(
                    "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
                    (current_user["id"], user_id),
                ).fetchone()
                can_see_private = bool(is_friend)


        # STEP 3: the page, newest first — id DESC breaks ties so
        # two posts of the same second cannot swap between pages
        # =======================================================
        visibility_sql = "" if can_see_private else " AND p.is_public = 1"

        rows = db.execute(
            f"""SELECT p.*,
                       au.avatar_url AS author_avatar,
                       au.display_name AS author_display_name
                FROM news_posts p
                LEFT JOIN users au ON au.id = p.author_id
                WHERE p.author_id = ? AND p.source IN ('user', 'faculty'){visibility_sql}
                ORDER BY p.published_at DESC, p.id DESC
                LIMIT ? OFFSET ?""",
            (user_id, per_page, offset),
        ).fetchall()

        posts = [_post_row_to_dict(row, truncate=True) for row in rows]


        # STEP 4: "liked" flags for the viewer in one IN (...) query
        # ==========================================================
        if current_user and posts:
            post_ids = [p["id"] for p in posts]
            placeholders = ",".join(["?"] * len(post_ids))
            liked = db.execute(
                f"SELECT post_id FROM news_likes WHERE user_id = ? AND post_id IN ({placeholders})",
                [current_user["id"]] + post_ids,
            ).fetchall()
            liked_set = {r["post_id"] for r in liked}
            for p in posts:
                p["liked"] = p["id"] in liked_set


        # STEP 5: total for hasMore, same visibility fragment
        # ===================================================
        total = db.execute(
            f"""SELECT COUNT(*) as c FROM news_posts p
                WHERE p.author_id = ? AND p.source IN ('user', 'faculty'){visibility_sql}""",
            (user_id,),
        ).fetchone()["c"]

        return jsonify({"posts": posts, "page": page, "perPage": per_page, "total": total, "hasMore": offset + per_page < total})
    finally:
        db.close()








############################################################
# update_post
############################################################
#
# PUT /api/social/posts/<post_id>
#
# Edits content / title / image_url of the caller's own
# post; a post that exists but belongs to someone else (or
# is neither source 'user' nor 'faculty') is a 404, not a
# 403. The source pair matches delete_post, so a staff
# member can edit the announcements their own profile lists
# — the lookup used to accept 'user' only, which made every
# faculty post uneditable through this route.
#
# Validation mirrors create_post now: a present content that
# strips to empty is a 400 rather than a silent skip, and a
# present title that strips to empty falls back to the first
# 80 chars of the new content (or, when only the title is
# being edited, of the stored one) instead of storing "".
# published_at is untouched, so an edit never re-ranks the
# post in the feed. Stored raw and escaped on output like
# everything else.
#
# Used by:
#   - mobile services/api/social.ts updatePost — the article
#     screen's edit action (create-post in edit mode)
############################################################

@social_bp.route("/posts/<post_id>", methods=["PUT"])
@require_auth
@rate_limit("post", max_attempts=20)
def update_post(post_id):
    # STEP 1: body
    # ============
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:


        # STEP 2: ownership — someone else's post reads as missing
        # ========================================================
        post = db.execute(
            "SELECT * FROM news_posts WHERE id = ? AND author_id = ? AND source IN ('user', 'faculty')",
            (post_id, request.user["id"]),
        ).fetchone()
        if not post:
            return jsonify({"error": "Post not found or not yours"}), 404


        # STEP 3: collect "col = ?" fragments for the fields present
        # ==========================================================
        updates = []
        params = []
        content = None
        if "content" in data:
            if not isinstance(data["content"], str):
                return jsonify({"error": "content must be a string"}), 400
            content = data["content"].strip()
            if not content:
                return jsonify({"error": "Post content required"}), 400
            if len(content) > MAX_CONTENT_LENGTH:
                return jsonify({"error": f"Content must be at most {MAX_CONTENT_LENGTH} characters"}), 400
            updates.append("content = ?")
            params.append(content)
            updates.append("summary = ?")
            params.append(content[:SUMMARY_LENGTH])
        if "title" in data:
            if not isinstance(data["title"], str):
                return jsonify({"error": "title must be a string"}), 400
            # Same fallback as create_post: a blank title becomes
            # the head of the body being stored, never ""
            title = data["title"].strip() or (content or post["content"] or "")[:80]
            if len(title) > MAX_TITLE_LENGTH:
                return jsonify({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}), 400
            updates.append("title = ?")
            params.append(title)
        if "image_url" in data:
            # Own uploads or clearing only — same beacon guard as
            # create_post's
            iv = data["image_url"]
            if iv not in (None, "") and (not isinstance(iv, str) or not iv.startswith("/api/uploads/")):
                return jsonify({"error": "image_url must be a relative /api/uploads/ path"}), 400
            updates.append("image_url = ?")
            params.append(iv)

        if not updates:
            return jsonify({"error": "No fields to update"}), 400


        # STEP 4: one UPDATE, updated_at stamped in the house
        # T-form UTC shape
        # ===================================================
        updates.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(post_id)

        db.execute(f"UPDATE news_posts SET {', '.join(updates)} WHERE id = ?", params)
        db.commit()

        return jsonify({"status": "updated"})
    finally:
        db.close()








############################################################
# delete_post
############################################################
#
# DELETE /api/social/posts/<post_id>
#
# Owner-only: no admin override here (news/routes.py
# delete_post has one and deletes likes / comments / polls
# by hand). Covers the caller's source 'user' AND 'faculty'
# rows — staff delete their announcements from their own
# profile through this route too, the ownership check
# unchanged. This one is a bare DELETE on news_posts and
# relies on the ON DELETE CASCADE from news_likes,
# news_comments and polls (→ poll_options, poll_votes) —
# which only fires because get_db turns PRAGMA foreign_keys
# on per connection. Like update_post, someone else's post
# is a 404 rather than a 403. The post's own /api/uploads/
# image goes with it — after the commit, best-effort, via
# the uploads package's delete helper.
#
# Used by:
#   - services/api/social.ts — deletePost
#   - app/(main)/profile/index.tsx — own-post delete menu
#     (behind a ConfirmDialog)
############################################################

@social_bp.route("/posts/<post_id>", methods=["DELETE"])
@require_auth
@rate_limit("post_delete", max_attempts=40)
def delete_post(post_id):
    db = get_db()
    try:
        post = db.execute(
            "SELECT id, image_url FROM news_posts WHERE id = ? AND author_id = ? AND source IN ('user', 'faculty')",
            (post_id, request.user["id"]),
        ).fetchone()
        if not post:
            return jsonify({"error": "Post not found or not yours"}), 404

        db.execute("DELETE FROM news_posts WHERE id = ?", (post_id,))
        db.commit()

        _delete_upload_file(post["image_url"])

        return jsonify({"status": "deleted"})
    finally:
        db.close()








############################################################
# block_user
############################################################
#
# POST /api/social/blocks
#
# Body {user_id}. Blocking is one-directional in storage
# (one row, blocker → blocked) but bidirectional in effect:
# every enforcement site checks the pair both ways, so
# neither side can contact the other. The write also severs
# what exists — both friendships rows and any pending
# friend request between the pair — because "blocked but
# still friends" is not a state anyone means. Repeat blocks
# are a 200 (INSERT OR IGNORE), self-blocks a 400, unknown
# or deactivated targets a 404. Admins are blockable like
# anyone else: a block only silences chat and requests, it
# does not blunt moderation (admin routes read nothing from
# user_blocks).
#
# Used by:
#   - services/api/social.ts — blockUser
#     (app/(main)/profile/index.tsx — the block action)
############################################################

@social_bp.route("/blocks", methods=["POST"])
@require_auth
@rate_limit("block", max_attempts=60)
def block_user():
    # STEP 1: body + the self-block guard
    # ===================================
    data = get_json_object()
    if not data or not data.get("user_id"):
        return jsonify({"error": "user_id required"}), 400

    target_id = data["user_id"]
    my_id = request.user["id"]

    if not isinstance(target_id, str):
        return jsonify({"error": "user_id must be a string"}), 400

    if target_id == my_id:
        return jsonify({"error": "Cannot block yourself"}), 400

    db = get_db()
    try:


        # STEP 2: the target must be a real, active account
        # =================================================
        target = db.execute(
            "SELECT 1 FROM users WHERE id = ? AND active = 1",
            (target_id,),
        ).fetchone()
        if not target:
            return jsonify({"error": "User not found"}), 404


        # STEP 3: the block row, plus the severing — the
        # friendship (both directions) and any pending request
        # between the pair go with it, in one transaction
        # ====================================================
        db.execute(
            "INSERT OR IGNORE INTO user_blocks (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)",
            (my_id, target_id, utc_now_iso()),
        )
        db.execute(
            "DELETE FROM friendships WHERE (user_id = ? AND friend_id = ?) "
            "OR (user_id = ? AND friend_id = ?)",
            (my_id, target_id, target_id, my_id),
        )
        db.execute(
            "DELETE FROM friend_requests WHERE status = 'pending' AND "
            "((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))",
            (my_id, target_id, target_id, my_id),
        )
        db.commit()

        return jsonify({"status": "blocked"})
    finally:
        db.close()







############################################################
# unblock_user
############################################################
#
# DELETE /api/social/blocks/<user_id>
#
# Removes the caller's own block row. Idempotent — a repeat
# (or an id that was never blocked) is the same 200, so a
# retried tap cannot error. Unblocking restores nothing:
# the friendship and any requests the block severed stay
# gone, the pair simply may contact each other again.
#
# Used by:
#   - services/api/social.ts — unblockUser
#     (app/(main)/profile/index.tsx — the unblock action)
############################################################

@social_bp.route("/blocks/<user_id>", methods=["DELETE"])
@require_auth
@rate_limit("block", max_attempts=60)
def unblock_user(user_id):
    db = get_db()
    try:
        db.execute(
            "DELETE FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?",
            (request.user["id"], user_id),
        )
        db.commit()
        return jsonify({"status": "unblocked"})
    finally:
        db.close()







############################################################
# list_blocks
############################################################
#
# GET /api/social/blocks
#
# The caller's block list, newest first, joined to users for
# the row the client renders. Deactivated accounts stay in
# the list (the block outlives the account — reactivation
# must not resurrect contact), rendered from whatever
# profile fields remain.
#
# Used by:
#   - services/api/social.ts — fetchBlockedUsers
#     (the profile screen's unblock affordance)
############################################################

@social_bp.route("/blocks", methods=["GET"])
@require_auth
def list_blocks():
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT u.id, u.username, u.display_name, u.avatar_url, u.role, b.created_at
            FROM user_blocks b
            JOIN users u ON u.id = b.blocked_id
            WHERE b.blocker_id = ?
            ORDER BY b.created_at DESC
            """,
            (request.user["id"],),
        ).fetchall()

        return jsonify({
            "blocked": [
                {
                    "id": r["id"],
                    "username": r["username"],
                    "displayName": r["display_name"],
                    "avatarUrl": r["avatar_url"],
                    "role": r["role"],
                    "blockedAt": r["created_at"],
                }
                for r in rows
            ]
        })
    finally:
        db.close()







############################################################
# create_report
############################################################
#
# POST /api/social/reports
#
# Body {target_type: user|post|message, target_id, reason}.
# The complaint ledger: one row per report, read and
# resolved in the admin panel (admin/routes.py). The target
# must exist in the table its type names (404 otherwise), so
# the ledger cannot fill with pointers to nothing; reason is
# a required, stripped string ≤ 1000. The reporter's own id
# comes from the session — a report is never anonymous to
# the admins, only to the reported.
#
# Used by:
#   - services/api/social.ts — reportTarget
#     (app/(main)/profile/index.tsx — the report action)
############################################################

_REPORT_TABLES = {"user": "users", "post": "news_posts", "message": "messages"}

@social_bp.route("/reports", methods=["POST"])
@require_auth
@rate_limit("report", max_attempts=20)
def create_report():
    # STEP 1: body — a known type, a non-empty id, a bounded
    # non-empty reason
    # ======================================================
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    target_type = data.get("target_type")
    if target_type not in _REPORT_TABLES:
        return jsonify({"error": "target_type must be one of: user, post, message"}), 400

    target_id = data.get("target_id")
    if not isinstance(target_id, str) or not target_id.strip():
        return jsonify({"error": "target_id required"}), 400
    target_id = target_id.strip()

    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return jsonify({"error": "reason required"}), 400
    reason = reason.strip()
    if len(reason) > 1000:
        return jsonify({"error": "reason must be at most 1000 characters"}), 400

    db = get_db()
    try:


        # STEP 2: the target must exist in its own table — the
        # table name comes from the whitelist dict above, never
        # from the request
        # ====================================================
        exists = db.execute(
            f"SELECT 1 FROM {_REPORT_TABLES[target_type]} WHERE id = ?",
            (target_id,),
        ).fetchone()
        if not exists:
            return jsonify({"error": "Report target not found"}), 404


        # STEP 3: the row
        # ===============
        report_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO reports (id, reporter_id, target_type, target_id, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (report_id, request.user["id"], target_type, target_id, reason, utc_now_iso()),
        )
        db.commit()

        return jsonify({"status": "submitted", "id": report_id}), 201
    finally:
        db.close()
