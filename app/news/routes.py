"""News feed API — unified feed with ranking, likes, comments."""

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.auth.routes import get_current_user, require_auth
from app.database import get_db

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
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(50, max(1, request.args.get("per_page", 20, type=int)))
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

        # Add like status for authenticated users
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


@news_bp.route("/<post_id>", methods=["GET"])
def get_post(post_id):
    """Get a single post by ID."""
    db = get_db()
    try:
        row = db.execute("SELECT * FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            return jsonify({"error": "Post not found"}), 404
        return jsonify(_post_to_dict(row))
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
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(50, max(1, request.args.get("per_page", 20, type=int)))
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
    if not data or not data.get("text", "").strip():
        return jsonify({"error": "Comment text required"}), 400

    db = get_db()
    try:
        post = db.execute("SELECT id FROM news_posts WHERE id = ?", (post_id,)).fetchone()
        if not post:
            return jsonify({"error": "Post not found"}), 404

        comment_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO news_comments (id, post_id, user_id, text) VALUES (?, ?, ?, ?)",
            (comment_id, post_id, request.user["id"], data["text"].strip()),
        )
        db.execute("UPDATE news_posts SET comments_count = comments_count + 1 WHERE id = ?", (post_id,))
        db.commit()

        return jsonify({
            "id": comment_id,
            "text": data["text"].strip(),
            "time": datetime.utcnow().isoformat(),
            "userName": request.user["display_name"],
            "userId": request.user["id"],
        }), 201
    finally:
        db.close()
