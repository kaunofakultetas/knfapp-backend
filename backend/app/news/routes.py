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
#  writes sit behind require_auth. Nothing is escaped on the
#  way IN — text is stored raw and the after_request hook in
#  app/__init__.py html-escapes every string in the JSON on
#  the way out (the v1/v2 migration history in
#  database/__init__.py explains why).
#
#  Timestamps this file writes are datetime.utcnow()
#  .isoformat() (naive UTC, 'T' separator, microseconds —
#  utcnow() is deprecated since Python 3.12 and the image
#  runs 3.13, a DeprecationWarning for now); columns left to
#  their DEFAULT get SQLite's datetime('now') (UTC, space
#  separator). Both are UTC, so the julianday() ranking and
#  the poll end-date compare hold, but clients see two
#  shapes of the same field (comment time, poll vote time).
#
#    GET    /api/news                      — ranked feed page
#    POST   /api/news                      — create a post
#    GET    /api/news/<post_id>            — one post
#    DELETE /api/news/<post_id>            — author/admin delete
#    POST   /api/news/<post_id>/like       — toggle a like
#    GET    /api/news/<post_id>/comments   — comments page
#    POST   /api/news/<post_id>/comments   — add a comment
#    GET    /api/news/<post_id>/poll       — the post's poll
#    POST   /api/news/<post_id>/poll       — attach a poll
#    POST   /api/news/<post_id>/poll/vote  — cast/move a vote
############################################################


import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.api import parse_pagination
# require_role is a dead import — nothing here gates on a
# role; create_post derives the post's source from the
# caller's role instead
from app.auth.routes import get_current_user, require_auth, require_role
from app.database import get_db

# Hard caps behind the 400s in create_post / add_comment.
# social/routes.py duplicates the first two for POST
# /api/social/posts, migration v1 truncated pre-existing rows
# to them, and the mobile app mirrors them as input maxLength
# (create-post/index.tsx — content even tighter at 5000 —
# and components/news/CommentComposer.tsx). Poll titles and
# option texts have no cap at all.
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 10000
MAX_COMMENT_LENGTH = 2000

news_bp = Blueprint("news", __name__)








############################################################
# _post_to_dict
############################################################
#
# The wire shape of one news_posts row (camelCase keys — the
# mobile NewsPost type). NOT included: the viewer's `liked`
# flag — get_feed adds it per page afterwards, get_post
# never does, and create_post rebuilds this very dict by
# hand with liked=False (keep the three in step). `date` is
# published_at; likes / comments / shares are the
# denormalised counters on the row, not live COUNT(*)s.
#
# Used by:
#   - get_feed, get_post (below)
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
# get_feed
############################################################
#
# GET /api/news
#
# The unified feed, one page at a time: ?page / ?per_page
# (parse_pagination — default 20, capped at 100, 400 on
# garbage) and an optional ?source out of app / knf.vu.lt /
# vu.lt / faculty / user. Who sees what:
#
#   guest   is_public = 1 AND source != 'user' — scraped
#           articles and public faculty posts only. A guest
#           asking ?source=user gets an empty page (the two
#           clauses contradict), which is what the mobile
#           "user" chip yields when logged out.
#   member  every non-user row regardless of is_public, plus
#           wall posts by the caller and by the caller's
#           friendships rows — private ones included.
#           friendships is written in both directions on
#           accept (social/routes.py), so one direction is
#           enough here. Public wall posts of NON-friends
#           never appear in this feed; /api/social/feed is
#           where those live.
#
# Ranking is computed in SQL — the three score columns are
# selected for inspection and the ORDER BY repeats the
# formula instead of referencing them:
#
#   recency     100 / (1 + days since published_at) — a
#               hyperbolic decay, not the exponential one
#               the old docstring claimed; julianday('now')
#               is UTC like every stored timestamp
#   engagement  MIN(likes + 2*comments + 3*shares, 100)
#               * 0.5 — linear, capped at 50 (SQLite has no
#               log())
#   boost       faculty 20, knf.vu.lt 15, vu.lt 10, app 5,
#               user 0
#
# Ties fall back to published_at DESC. The total behind
# hasMore reuses where_sql with the LIMIT/OFFSET pair sliced
# off the end of params, so the count and the page can never
# disagree on the filter.
#
# Used by:
#   - services/api/news.ts fetchNewsFeed —
#     app/(main)/tabs/news.tsx through hooks/useFeed.ts (the
#     source chips map straight onto ?source)
############################################################

@news_bp.route("", methods=["GET"])
def get_feed():
    # STEP 1: pagination, the optional ?source and the optional
    # caller — a bad ?page / ?per_page is a 400 straight from
    # parse_pagination
    # =========================================================
    page, per_page, err = parse_pagination()
    if err:
        return err
    source_filter = request.args.get("source")
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

        where_sql = " AND ".join(where_clauses)


        # STEP 3: the ranked page — formula in the banner; the
        # SQL comments inside the string are SQLite's, not ours
        # =====================================================
        query = f"""
            SELECT *,
                -- Recency: days since published, exponential-like decay
                (1.0 / (1.0 + (julianday('now') - julianday(published_at)))) * 100 AS recency_score,
                -- Engagement (linear, capped)
                MIN(likes_count + comments_count * 2 + shares_count * 3, 100) * 0.5 AS engagement_score,
                -- Source boost
                (CASE source
                    WHEN 'faculty' THEN 20
                    WHEN 'knf.vu.lt' THEN 15
                    WHEN 'vu.lt' THEN 10
                    WHEN 'app' THEN 5
                    ELSE 0
                END) AS source_boost
            FROM news_posts
            WHERE {where_sql}
            ORDER BY (
                (1.0 / (1.0 + (julianday('now') - julianday(published_at)))) * 100
                + MIN(likes_count + comments_count * 2 + shares_count * 3, 100) * 0.5
                + (CASE source
                    WHEN 'faculty' THEN 20
                    WHEN 'knf.vu.lt' THEN 15
                    WHEN 'vu.lt' THEN 10
                    WHEN 'app' THEN 5
                    ELSE 0
                END)
            ) DESC, published_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([per_page, offset])

        rows = db.execute(query, params).fetchall()
        posts = [_post_to_dict(row) for row in rows]


        # STEP 4: the caller's like flag — one IN query for the
        # whole page; a guest is simply never "liked"
        # =====================================================
        if user:
            post_ids = [p["id"] for p in posts]
            if post_ids:
                placeholders = ",".join(["?"] * len(post_ids))
                liked = db.execute(
                    f"SELECT post_id FROM news_likes WHERE user_id = ? AND post_id IN ({placeholders})",
                    [user["id"]] + post_ids,
                ).fetchall()
                liked_set = {r["post_id"] for r in liked}
                for p in posts:
                    p["liked"] = p["id"] in liked_set
        else:
            for p in posts:
                p["liked"] = False


        # STEP 5: total for hasMore — same WHERE, params minus the
        # trailing LIMIT/OFFSET pair (the len() guard is redundant:
        # params always ends with exactly those two)
        # =========================================================
        count_row = db.execute(
            f"SELECT COUNT(*) as total FROM news_posts WHERE {where_sql}",
            params[:-2] if len(params) > 2 else [],
        ).fetchone()
        total = count_row["total"]

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
# create_post
############################################################
#
# POST /api/news
#
# Creates a post from {content, title?, post_type?,
# image_url?, is_public?}. There is no role gate — the role
# picks the source instead: admin / curator / teacher
# publish as source 'faculty' (post_type defaults to
# 'announcement'), everyone else as 'user' (defaults to
# 'social'), the same row shape POST /api/social/posts
# writes. title falls back to content[:80], summary is
# always content[:200], source_url stays NULL and
# published/created/updated_at all get one utcnow() stamp.
#
# Validation is thinner than the social twin's: content and
# title are not type-checked (a non-string value hits
# .strip() → AttributeError → 500); post_type and image_url
# are stored as sent — a post_type outside the table's CHECK
# list ('article', 'social', 'announcement', 'poll', 'link')
# dies as IntegrityError → 500; is_public is echoed back raw
# while the row stores 1/0, so a truthy non-bool such as
# "false" is saved public and echoed as "false". The 201
# body is _post_to_dict's shape rebuilt by hand plus
# liked=False.
#
# Used by:
#   - services/api/news.ts createPost —
#     app/(main)/create-post/index.tsx (content, title,
#     image_url from uploadImageApi, is_public); a poll is
#     attached by a follow-up POST .../poll (create_poll)
############################################################

@news_bp.route("", methods=["POST"])
@require_auth
def create_post():
    # STEP 1: body checks — content required, title optional,
    # both capped (400s); no escaping here, the after_request
    # hook escapes on output
    # =======================================================
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Content required"}), 400

    title = (data.get("title") or "").strip() or content[:80]

    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}), 400
    if len(content) > MAX_CONTENT_LENGTH:
        return jsonify({"error": f"Content must be at most {MAX_CONTENT_LENGTH} characters"}), 400


    # STEP 2: the caller's role decides the source; post_type
    # only gets a default when the client sent none
    # =======================================================
    role = request.user["role"]
    post_type = data.get("post_type")
    image_url = data.get("image_url")
    is_public = data.get("is_public", True)

    if role in ("admin", "curator", "teacher"):
        source = "faculty"
        if not post_type:
            post_type = "announcement"
    else:
        source = "user"
        if not post_type:
            post_type = "social"


    # STEP 3: insert and echo the row back as a 201
    # =============================================
    post_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    db = get_db()
    try:
        db.execute(
            """INSERT INTO news_posts
               (id, title, content, summary, image_url, author_id, author_name,
                source, source_url, post_type, is_public, published_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)""",
            (post_id, title, content, content[:200], image_url,
             request.user["id"], request.user["display_name"],
             source, post_type, 1 if is_public else 0, now, now, now),
        )
        db.commit()

        return jsonify({
            "id": post_id,
            "title": title,
            "content": content,
            "summary": content[:200],
            "imageUrl": image_url,
            "author": request.user["display_name"],
            "authorId": request.user["id"],
            "source": source,
            "postType": post_type,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "date": now,
            "isPublic": is_public,
            "liked": False,
        }), 201
    finally:
        db.close()








############################################################
# get_post
############################################################
#
# GET /api/news/<post_id>
#
# One post by id. A private wall post (is_public = 0 AND
# source = 'user') is served only to its author and the
# author's friends; everyone else, guests included, gets the
# same 404 as for a missing id, so existence never leaks. A
# private NON-user post (faculty with is_public = 0) has no
# such check and is served to anyone, although get_feed
# hides it from guests. The body is _post_to_dict only — no
# `liked` flag — the mobile detail screen carries like state
# over from the feed item it was opened from.
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
        row = db.execute("SELECT * FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            return jsonify({"error": "Post not found"}), 404

        # Private wall posts: author and friends only. The 404 (not
        # 403) is deliberate — a stranger cannot tell "private"
        # from "missing"
        if not row["is_public"] and row["source"] == "user":
            if not user:
                return jsonify({"error": "Post not found"}), 404
            if row["author_id"] != user["id"]:
                is_friend = db.execute(
                    "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
                    (user["id"], row["author_id"]),
                ).fetchone()
                if not is_friend:
                    return jsonify({"error": "Post not found"}), 404

        return jsonify(_post_to_dict(row))
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
# only an admin can drop those). Likes, comments, the poll
# and its votes/options are deleted by hand before the post
# row; the schema's ON DELETE CASCADE (get_db turns
# foreign_keys on) would do the same, so the manual sweep is
# belt and braces — DELETE /api/social/posts/<id> in
# social/routes.py trusts the cascade alone.
#
# Used by:
#   - nothing calls this at the moment — the mobile app
#     deletes wall posts through services/api/social.ts
#     deletePost → DELETE /api/social/posts/<id>; only
#     swagger/swagger.yaml documents this one
############################################################

@news_bp.route("/<post_id>", methods=["DELETE"])
@require_auth
def delete_post(post_id):
    # STEP 1: the post must exist and the caller must be its
    # author or an admin
    # ======================================================
    db = get_db()
    try:
        post = db.execute("SELECT id, author_id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        user = request.user
        if post["author_id"] != user["id"] and user["role"] != "admin":
            return jsonify({"error": "Only the post author or an admin can delete this post"}), 403


        # STEP 2: dependants first — likes, comments, then the poll
        # with its votes and options (the cascade would do this
        # too, see the banner)
        # =========================================================
        db.execute("DELETE FROM news_likes WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM news_comments WHERE post_id = ?", (post_id,))

        poll = db.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if poll:
            db.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM poll_options WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM polls WHERE id = ?", (poll["id"],))


        # STEP 3: the post row itself, one commit for the lot
        # ===================================================
        db.execute("DELETE FROM news_posts WHERE id = ?", (post_id,))
        db.commit()

        return jsonify({"status": "deleted"})
    finally:
        db.close()








############################################################
# toggle_like
############################################################
#
# POST /api/news/<post_id>/like
#
# Flips the caller's like on a post: a news_likes row (PK
# user_id + post_id) is inserted or deleted and
# news_posts.likes_count moves with it, floored at 0 on the
# way down. The count in the reply is re-read after the
# commit, so it includes likes other users landed meanwhile.
# Two toggles from the same user racing each other can both
# miss the SELECT and collide on the PK at INSERT →
# IntegrityError → 500.
#
# Used by:
#   - services/api/news.ts toggleLikeApi —
#     app/(main)/tabs/news.tsx (feed heart, optimistic) and
#     app/(main)/news-post/index.tsx (detail heart)
############################################################

@news_bp.route("/<post_id>/like", methods=["POST"])
@require_auth
def toggle_like(post_id):
    db = get_db()
    try:
        post = db.execute("SELECT id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        existing = db.execute(
            "SELECT 1 FROM news_likes WHERE user_id = ? AND post_id = ?",
            (request.user["id"], post_id),
        ).fetchone()

        if existing:
            # MAX(0, …) keeps a counter that drifted below the real
            # number of like rows from going negative
            db.execute("DELETE FROM news_likes WHERE user_id = ? AND post_id = ?", (request.user["id"], post_id))
            db.execute("UPDATE news_posts SET likes_count = MAX(0, likes_count - 1) WHERE id = ?", (post_id,))
            liked = False
        else:
            db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (request.user["id"], post_id))
            db.execute("UPDATE news_posts SET likes_count = likes_count + 1 WHERE id = ?", (post_id,))
            liked = True

        db.commit()
        # Re-read after the commit — the reply carries the real
        # counter, not a likes±1 computed locally
        count = db.execute("SELECT likes_count FROM news_posts WHERE id = ?", (post_id,)).fetchone()["likes_count"]

        return jsonify({"liked": liked, "likes": count})
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
# as in get_feed). The post itself is never looked up: an
# unknown id answers an empty page with total 0, not a 404.
# `time` is news_comments.created_at exactly as SQLite wrote
# it via the column default (datetime('now') — "YYYY-MM-DD
# HH:MM:SS", UTC), a different shape from the isoformat()
# value add_comment echoes; the mobile app formats both
# locally and never displays the raw string.
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

    db = get_db()
    try:
        rows = db.execute(
            """SELECT c.id, c.text, c.created_at, u.display_name, u.avatar_url, u.id as user_id
               FROM news_comments c
               JOIN users u ON c.user_id = u.id
               WHERE c.post_id = ?
               ORDER BY c.created_at DESC
               LIMIT ? OFFSET ?""",
            (post_id, per_page, offset),
        ).fetchall()

        comments = [
            {
                "id": r["id"],
                "text": r["text"],
                "time": r["created_at"],
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
# Appends {text} to a post — a non-blank string of at most
# MAX_COMMENT_LENGTH (400 otherwise, 404 for an unknown
# post) — and bumps news_posts.comments_count, which nothing
# ever decrements (there is no delete-comment route). The
# 201 body is built from request.user, and its `time` is a
# utcnow().isoformat() taken AFTER the insert, not the
# created_at SQLite stored through the column default (see
# get_comments for that shape).
#
# Used by:
#   - services/api/news.ts addCommentApi —
#     app/(main)/news-comments/index.tsx and
#     app/(main)/news-post/index.tsx (both via
#     components/news/CommentComposer.tsx)
############################################################

@news_bp.route("/<post_id>/comments", methods=["POST"])
@require_auth
def add_comment(post_id):
    data = request.get_json()
    if not data or not isinstance(data.get("text"), str) or not data["text"].strip():
        return jsonify({"error": "Comment text required"}), 400

    comment_text = data["text"].strip()

    if len(comment_text) > MAX_COMMENT_LENGTH:
        return jsonify({"error": f"Comment must be at most {MAX_COMMENT_LENGTH} characters"}), 400

    db = get_db()
    try:
        post = db.execute("SELECT id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        comment_id = str(uuid.uuid4())
        # created_at is left to the column default — the reply's
        # `time` below is a separate clock read
        db.execute(
            "INSERT INTO news_comments (id, post_id, user_id, text) VALUES (?, ?, ?, ?)",
            (comment_id, post_id, request.user["id"], comment_text),
        )
        db.execute("UPDATE news_posts SET comments_count = comments_count + 1 WHERE id = ?", (post_id,))
        db.commit()

        return jsonify({
            "id": comment_id,
            "text": comment_text,
            "time": datetime.utcnow().isoformat(),
            "userName": request.user["display_name"],
            "userAvatar": request.user.get("avatar_url"),
            "userId": request.user["id"],
        }), 201
    finally:
        db.close()








############################################################
# _poll_to_dict
############################################################
#
# The wire shape of a polls row plus its options (the mobile
# PollResponse): options come back in rowid order — the
# order the creator sent them — and userVote is the caller's
# poll_votes.option_id, or None when no user_id was given or
# they have not voted. Runs two more queries on the caller's
# open connection.
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

    return {
        "id": poll_id,
        "postId": poll_row["post_id"],
        "title": poll_row["title"],
        "endDate": poll_row["end_date"],
        "totalVotes": poll_row["total_votes"],
        "createdAt": poll_row["created_at"],
        "userVote": user_vote,
        "options": [
            {"id": o["id"], "text": o["text"], "votes": o["votes"]}
            for o in options
        ],
    }








############################################################
# get_poll
############################################################
#
# GET /api/news/<post_id>/poll
#
# The poll attached to a post, with the caller's own vote
# when logged in. 404 when the post has no poll — the mobile
# fetchPoll turns exactly that status into null and rethrows
# everything else — and the same 404 for an unknown post,
# since the post is never looked up separately.
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
# post (409) — and flips the post's post_type to 'poll'
# whatever it was before. The checks are shallow: options is
# only len()-checked (2..10) — a string passes and is then
# iterated character by character, a dict yields its keys —
# blank entries are dropped silently AFTER that check, so a
# poll can end up with fewer than two options; title and
# option text have no length cap; end_date is stored
# verbatim (vote_poll parses it with fromisoformat and
# treats an unparseable value as "never ends"). Nothing
# restricts the post's source, so an admin can hang a poll
# on a scraped article.
#
# Used by:
#   - services/api/news.ts createPollApi —
#     app/(main)/create-post/index.tsx, right after
#     createPost (with a retry prompt when this call fails)
############################################################

@news_bp.route("/<post_id>/poll", methods=["POST"])
@require_auth
def create_poll(post_id):
    # STEP 1: body checks — title required, 2..10 options; only
    # len() is looked at, see the banner for what slips through
    # =========================================================
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    title = (data.get("title") or "").strip()
    options = data.get("options", [])
    end_date = data.get("end_date")

    if not title:
        return jsonify({"error": "Poll title required"}), 400
    if len(options) < 2:
        return jsonify({"error": "At least 2 options required"}), 400
    if len(options) > 10:
        return jsonify({"error": "Maximum 10 options allowed"}), 400


    # STEP 2: the post must exist, the caller must own it (or
    # be admin), and it must not have a poll yet
    # =======================================================
    db = get_db()
    try:
        post = db.execute("SELECT id, author_id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        user = request.user
        if post["author_id"] != user["id"] and user["role"] != "admin":
            return jsonify({"error": "Only the post author or admin can create a poll"}), 403

        existing = db.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if existing:
            return jsonify({"error": "Post already has a poll"}), 409


        # STEP 3: the poll row, then one poll_options row per
        # non-blank option — blank ones vanish without a 400
        # ===================================================
        poll_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        db.execute(
            "INSERT INTO polls (id, post_id, title, end_date, created_at) VALUES (?, ?, ?, ?, ?)",
            (poll_id, post_id, title, end_date, now),
        )

        for opt_text in options:
            opt_text = str(opt_text).strip()
            if opt_text:
                opt_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO poll_options (id, poll_id, text) VALUES (?, ?, ?)",
                    (opt_id, poll_id, opt_text),
                )


        # STEP 4: the post becomes a 'poll' post whatever it was,
        # then commit and answer with the fresh poll (userVote
        # None, 201)
        # =======================================================
        db.execute("UPDATE news_posts SET post_type = 'poll' WHERE id = ?", (post_id,))
        db.commit()

        poll = db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        return jsonify(_poll_to_dict(db, poll, user["id"])), 201
    finally:
        db.close()








############################################################
# vote_poll
############################################################
#
# POST /api/news/<post_id>/poll/vote
#
# Casts or moves the caller's vote {option_id} on the post's
# poll — one poll_votes row per (user_id, poll_id). A first
# vote bumps the option's votes AND polls.total_votes; a
# change moves one vote between options and leaves
# total_votes alone (still one voter); re-voting the option
# already held is a 409, which the mobile votePollApi treats
# as a no-op and answers with a refetched poll.
#
# The end-date gate compares a naive end_date with utcnow():
# an offset in the stored string is DROPPED, not converted,
# so "…+03:00" closes three hours late; a value that
# fromisoformat cannot parse is swallowed (ValueError →
# pass) and that poll never closes. On a vote change
# created_at is rewritten as isoformat() ('T' shape) while
# the insert path keeps the column default (space shape) —
# two formats in one column.
#
# Used by:
#   - services/api/news.ts votePollApi —
#     components/news/PollWidget.tsx (option tap)
############################################################

@news_bp.route("/<post_id>/poll/vote", methods=["POST"])
@require_auth
def vote_poll(post_id):
    # STEP 1: body — option_id is the only field, and it must be
    # truthy
    # ==========================================================
    data = request.get_json()
    if not data or not data.get("option_id"):
        return jsonify({"error": "option_id required"}), 400

    option_id = data["option_id"]
    user_id = request.user["id"]


    # STEP 2: the poll, then its end-date gate (banner: naive
    # compare, unparseable = open forever)
    # =======================================================
    db = get_db()
    try:
        poll = db.execute("SELECT * FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if not poll:
            return jsonify({"error": "No poll found for this post"}), 404

        poll_id = poll["id"]

        if poll["end_date"]:
            try:
                end = datetime.fromisoformat(poll["end_date"]).replace(tzinfo=None)
                if datetime.utcnow() > end:
                    return jsonify({"error": "Poll has ended"}), 400
            except ValueError:
                pass


        # STEP 3: the option must belong to THIS poll — a foreign
        # option id is a plain 400
        # =======================================================
        option = db.execute(
            "SELECT id FROM poll_options WHERE id = ? AND poll_id = ?",
            (option_id, poll_id),
        ).fetchone()
        if not option:
            return jsonify({"error": "Invalid option"}), 400


        # STEP 4: cast or move — the same option twice is a 409
        # =====================================================
        existing = db.execute(
            "SELECT option_id FROM poll_votes WHERE user_id = ? AND poll_id = ?",
            (user_id, poll_id),
        ).fetchone()

        if existing:
            if existing["option_id"] == option_id:
                return jsonify({"error": "Already voted for this option"}), 409
            # STEP 4.1: move — old option down (floored at 0), new one up, total untouched
            db.execute(
                "UPDATE poll_options SET votes = MAX(0, votes - 1) WHERE id = ?",
                (existing["option_id"],),
            )
            db.execute(
                "UPDATE poll_options SET votes = votes + 1 WHERE id = ?",
                (option_id,),
            )
            db.execute(
                "UPDATE poll_votes SET option_id = ?, created_at = ? WHERE user_id = ? AND poll_id = ?",
                (option_id, datetime.utcnow().isoformat(), user_id, poll_id),
            )
        else:
            # STEP 4.2: first vote — the option and total_votes both go up
            db.execute(
                "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
                (user_id, poll_id, option_id),
            )
            db.execute(
                "UPDATE poll_options SET votes = votes + 1 WHERE id = ?",
                (option_id,),
            )
            db.execute(
                "UPDATE polls SET total_votes = total_votes + 1 WHERE id = ?",
                (poll_id,),
            )


        # STEP 5: commit and answer with the fresh poll state
        # ===================================================
        db.commit()

        poll = db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        return jsonify(_poll_to_dict(db, poll, user_id))
    finally:
        db.close()
