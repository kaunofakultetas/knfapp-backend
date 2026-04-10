"""News feed API — unified feed with ranking, likes, comments, polls."""

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.api import parse_pagination
from app.auth.routes import get_current_user, require_auth, require_role
from app.database import get_db

# Input length limits
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 10000
MAX_COMMENT_LENGTH = 2000

news_bp = Blueprint("news", __name__)


def _post_to_dict(row):
    """Convert a news_posts row to API response dict."""
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


@news_bp.route("", methods=["GET"])
def get_feed():
    """
    Unified news feed.

    Query params:
      - page (int, default 1)
      - per_page (int, default 20, max 50)
      - source (str, optional: 'knf.vu.lt', 'vu.lt', 'faculty', 'user', 'app')

    Guest users see: scraped news + public faculty posts.
    Authenticated users see: all of the above + friend posts + personalized ranking.

    Ranking algorithm:
      score = recency_score + engagement_score + source_boost
      - recency_score: exponential decay, halving every 24h
      - engagement_score: log(likes + comments*2 + shares*3 + 1) * 5
      - source_boost: faculty/official = +20, knf.vu.lt = +15, vu.lt = +10, user = 0
    """
    page, per_page, err = parse_pagination()
    if err:
        return err
    source_filter = request.args.get("source")
    offset = (page - 1) * per_page

    user = get_current_user()

    db = get_db()
    try:
        # Build query with ranking
        where_clauses = ["1=1"]
        params = []

        if source_filter:
            where_clauses.append("source = ?")
            params.append(source_filter)

        if not user:
            # Guests: only public posts, no user posts from friends
            where_clauses.append("is_public = 1")
            where_clauses.append("source != 'user'")
        else:
            # Authenticated: see own posts + friends' posts + all public non-user posts
            friend_ids = [r["friend_id"] for r in db.execute(
                "SELECT friend_id FROM friendships WHERE user_id = ?", (user["id"],)
            ).fetchall()]
            visible_ids = [user["id"]] + friend_ids
            if not source_filter or source_filter == "user":
                placeholders = ",".join(["?"] * len(visible_ids))
                where_clauses.append(
                    f"(source != 'user' OR author_id IN ({placeholders}))"
                )
                params.extend(visible_ids)

        where_sql = " AND ".join(where_clauses)

        # Ranked query using the scoring algorithm
        # Note: SQLite lacks log(), so we use a linear engagement score
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

        # Add like status
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

        # Total count for pagination
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


@news_bp.route("", methods=["POST"])
@require_auth
def create_post():
    """
    Create a news post.
    Faculty staff (admin, curator, teacher) can create faculty/announcement posts.
    Students can create user/social posts (same as social/posts endpoint).
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Content required"}), 400

    title = (data.get("title") or "").strip() or content[:80]

    # Input length validation
    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}), 400
    if len(content) > MAX_CONTENT_LENGTH:
        return jsonify({"error": f"Content must be at most {MAX_CONTENT_LENGTH} characters"}), 400

    # NOTE: XSS protection handled by after_request output escaping

    role = request.user["role"]
    post_type = data.get("post_type")
    image_url = data.get("image_url")
    is_public = data.get("is_public", True)

    # Determine source based on role
    if role in ("admin", "curator", "teacher"):
        source = "faculty"
        if not post_type:
            post_type = "announcement"
    else:
        source = "user"
        if not post_type:
            post_type = "social"

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


@news_bp.route("/<post_id>", methods=["GET"])
def get_post(post_id):
    """Get a single post by ID. Non-public posts require authentication and friendship."""
    user = get_current_user()
    db = get_db()
    try:
        row = db.execute("SELECT * FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            return jsonify({"error": "Post not found"}), 404

        # Non-public posts only visible to author or friends
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


@news_bp.route("/<post_id>", methods=["DELETE"])
@require_auth
def delete_post(post_id):
    """
    Delete a news post. Only the post author or an admin can delete.
    Also cleans up related likes, comments, polls, and poll votes.
    """
    db = get_db()
    try:
        post = db.execute("SELECT id, author_id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        user = request.user
        if post["author_id"] != user["id"] and user["role"] != "admin":
            return jsonify({"error": "Only the post author or an admin can delete this post"}), 403

        # Clean up related data
        db.execute("DELETE FROM news_likes WHERE post_id = ?", (post_id,))
        db.execute("DELETE FROM news_comments WHERE post_id = ?", (post_id,))

        # Clean up polls if any
        poll = db.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if poll:
            db.execute("DELETE FROM poll_votes WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM poll_options WHERE poll_id = ?", (poll["id"],))
            db.execute("DELETE FROM polls WHERE id = ?", (poll["id"],))

        db.execute("DELETE FROM news_posts WHERE id = ?", (post_id,))
        db.commit()

        return jsonify({"status": "deleted"})
    finally:
        db.close()


@news_bp.route("/<post_id>/like", methods=["POST"])
@require_auth
def toggle_like(post_id):
    """Toggle like on a post."""
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
            db.execute("DELETE FROM news_likes WHERE user_id = ? AND post_id = ?", (request.user["id"], post_id))
            db.execute("UPDATE news_posts SET likes_count = MAX(0, likes_count - 1) WHERE id = ?", (post_id,))
            liked = False
        else:
            db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (request.user["id"], post_id))
            db.execute("UPDATE news_posts SET likes_count = likes_count + 1 WHERE id = ?", (post_id,))
            liked = True

        db.commit()
        count = db.execute("SELECT likes_count FROM news_posts WHERE id = ?", (post_id,)).fetchone()["likes_count"]

        return jsonify({"liked": liked, "likes": count})
    finally:
        db.close()


@news_bp.route("/<post_id>/comments", methods=["GET"])
def get_comments(post_id):
    """Get comments for a post."""
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


@news_bp.route("/<post_id>/comments", methods=["POST"])
@require_auth
def add_comment(post_id):
    """Add a comment to a post."""
    data = request.get_json()
    if not data or not isinstance(data.get("text"), str) or not data["text"].strip():
        return jsonify({"error": "Comment text required"}), 400

    comment_text = data["text"].strip()

    # Input length validation
    if len(comment_text) > MAX_COMMENT_LENGTH:
        return jsonify({"error": f"Comment must be at most {MAX_COMMENT_LENGTH} characters"}), 400

    # NOTE: XSS protection handled by after_request output escaping

    db = get_db()
    try:
        post = db.execute("SELECT id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        comment_id = str(uuid.uuid4())
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


# ── Polls ────────────────────────────────────────────────────────────────────


def _poll_to_dict(db, poll_row, user_id=None):
    """Convert a poll + its options into an API response dict."""
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


@news_bp.route("/<post_id>/poll", methods=["GET"])
def get_poll(post_id):
    """Get the poll attached to a post, if any."""
    user = get_current_user()
    db = get_db()
    try:
        poll = db.execute("SELECT * FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if not poll:
            return jsonify({"error": "No poll found for this post"}), 404
        return jsonify(_poll_to_dict(db, poll, user["id"] if user else None))
    finally:
        db.close()


@news_bp.route("/<post_id>/poll", methods=["POST"])
@require_auth
def create_poll(post_id):
    """Create a poll on an existing post. Only the post author or admin can do this."""
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

    db = get_db()
    try:
        post = db.execute("SELECT id, author_id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        # Only author or admin can create polls
        user = request.user
        if post["author_id"] != user["id"] and user["role"] != "admin":
            return jsonify({"error": "Only the post author or admin can create a poll"}), 403

        # Check no poll already exists
        existing = db.execute("SELECT id FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if existing:
            return jsonify({"error": "Post already has a poll"}), 409

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

        # Update post type to poll
        db.execute("UPDATE news_posts SET post_type = 'poll' WHERE id = ?", (post_id,))
        db.commit()

        poll = db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        return jsonify(_poll_to_dict(db, poll, user["id"])), 201
    finally:
        db.close()


@news_bp.route("/<post_id>/poll/vote", methods=["POST"])
@require_auth
def vote_poll(post_id):
    """Vote on a poll. One vote per user per poll."""
    data = request.get_json()
    if not data or not data.get("option_id"):
        return jsonify({"error": "option_id required"}), 400

    option_id = data["option_id"]
    user_id = request.user["id"]

    db = get_db()
    try:
        poll = db.execute("SELECT * FROM polls WHERE post_id = ?", (post_id,)).fetchone()
        if not poll:
            return jsonify({"error": "No poll found for this post"}), 404

        poll_id = poll["id"]

        # Check end date
        if poll["end_date"]:
            try:
                end = datetime.fromisoformat(poll["end_date"]).replace(tzinfo=None)
                if datetime.utcnow() > end:
                    return jsonify({"error": "Poll has ended"}), 400
            except ValueError:
                pass

        # Validate option belongs to this poll
        option = db.execute(
            "SELECT id FROM poll_options WHERE id = ? AND poll_id = ?",
            (option_id, poll_id),
        ).fetchone()
        if not option:
            return jsonify({"error": "Invalid option"}), 400

        # Check for existing vote
        existing = db.execute(
            "SELECT option_id FROM poll_votes WHERE user_id = ? AND poll_id = ?",
            (user_id, poll_id),
        ).fetchone()

        if existing:
            if existing["option_id"] == option_id:
                return jsonify({"error": "Already voted for this option"}), 409
            # Change vote: decrement old, increment new
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
            # New vote
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

        db.commit()

        poll = db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
        return jsonify(_poll_to_dict(db, poll, user_id))
    finally:
        db.close()
