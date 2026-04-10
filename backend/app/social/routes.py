"""Social API — friend requests, friendships, user profiles, wall posts."""

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.api import parse_pagination
from app.auth.routes import get_current_user, require_auth
from app.database import get_db

# Input length limits (same as news)
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 10000

social_bp = Blueprint("social", __name__)


# ── User Profiles ────────────────────────────────────────────────────────────


@social_bp.route("/profile/<user_id>", methods=["GET"])
def get_profile(user_id):
    """Get a user's public profile with post count and friend count."""
    db = get_db()
    try:
        user = db.execute(
            "SELECT id, username, display_name, avatar_url, role, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404

        post_count = db.execute(
            "SELECT COUNT(*) as c FROM news_posts WHERE author_id = ? AND source = 'user'",
            (user_id,),
        ).fetchone()["c"]

        friend_count = db.execute(
            "SELECT COUNT(*) as c FROM friendships WHERE user_id = ?",
            (user_id,),
        ).fetchone()["c"]

        current_user = get_current_user()
        friendship_status = "none"
        if current_user and current_user["id"] != user_id:
            # Check if already friends
            is_friend = db.execute(
                "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
                (current_user["id"], user_id),
            ).fetchone()
            if is_friend:
                friendship_status = "friends"
            else:
                # Check pending requests
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
        })
    finally:
        db.close()


@social_bp.route("/profile", methods=["PUT"])
@require_auth
def update_profile():
    """Update own profile (display_name, avatar_url)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:
        updates = []
        params = []
        if "display_name" in data and data["display_name"].strip():
            updates.append("display_name = ?")
            params.append(data["display_name"].strip())
        if "avatar_url" in data:
            updates.append("avatar_url = ?")
            params.append(data["avatar_url"])

        if not updates:
            return jsonify({"error": "No fields to update"}), 400

        updates.append("updated_at = ?")
        params.append(datetime.utcnow().isoformat())
        params.append(request.user["id"])

        db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        db.commit()

        user = db.execute("SELECT * FROM users WHERE id = ?", (request.user["id"],)).fetchone()
        return jsonify({
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "displayName": user["display_name"],
            "avatarUrl": user["avatar_url"],
            "role": user["role"],
        })
    finally:
        db.close()


# ── Friend Requests ──────────────────────────────────────────────────────────


@social_bp.route("/friends/request", methods=["POST"])
@require_auth
def send_friend_request():
    """Send a friend request to another user."""
    data = request.get_json()
    if not data or not data.get("user_id"):
        return jsonify({"error": "user_id required"}), 400

    target_id = data["user_id"]
    my_id = request.user["id"]

    if target_id == my_id:
        return jsonify({"error": "Cannot friend yourself"}), 400

    db = get_db()
    try:
        # Check target exists
        target = db.execute("SELECT id, display_name FROM users WHERE id = ?", (target_id,)).fetchone()
        if not target:
            return jsonify({"error": "User not found"}), 404

        # Check if already friends
        existing = db.execute(
            "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
            (my_id, target_id),
        ).fetchone()
        if existing:
            return jsonify({"error": "Already friends"}), 409

        # Check for existing pending request (either direction)
        pending = db.execute(
            "SELECT id, from_user_id FROM friend_requests WHERE status = 'pending' AND "
            "((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))",
            (my_id, target_id, target_id, my_id),
        ).fetchone()

        if pending:
            if pending["from_user_id"] == target_id:
                # They already sent us a request — auto-accept
                db.execute(
                    "UPDATE friend_requests SET status = 'accepted', updated_at = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(), pending["id"]),
                )
                db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (my_id, target_id))
                db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (target_id, my_id))
                db.commit()
                return jsonify({"status": "accepted", "message": "Friend request auto-accepted (they already requested you)"}), 200
            return jsonify({"error": "Friend request already pending"}), 409

        req_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO friend_requests (id, from_user_id, to_user_id) VALUES (?, ?, ?)",
            (req_id, my_id, target_id),
        )
        db.commit()

        return jsonify({"id": req_id, "status": "pending"}), 201
    finally:
        db.close()


@social_bp.route("/friends/requests", methods=["GET"])
@require_auth
def list_friend_requests():
    """List pending friend requests (received by current user)."""
    direction = request.args.get("direction", "received")  # 'received' or 'sent'
    db = get_db()
    try:
        if direction == "sent":
            rows = db.execute(
                """SELECT fr.id, fr.to_user_id as user_id, fr.created_at,
                          u.display_name, u.username, u.avatar_url, u.role
                   FROM friend_requests fr
                   JOIN users u ON fr.to_user_id = u.id
                   WHERE fr.from_user_id = ? AND fr.status = 'pending'
                   ORDER BY fr.created_at DESC""",
                (request.user["id"],),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT fr.id, fr.from_user_id as user_id, fr.created_at,
                          u.display_name, u.username, u.avatar_url, u.role
                   FROM friend_requests fr
                   JOIN users u ON fr.from_user_id = u.id
                   WHERE fr.to_user_id = ? AND fr.status = 'pending'
                   ORDER BY fr.created_at DESC""",
                (request.user["id"],),
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

        return jsonify({"requests": requests_list})
    finally:
        db.close()


@social_bp.route("/friends/requests/<request_id>/accept", methods=["POST"])
@require_auth
def accept_friend_request(request_id):
    """Accept a pending friend request."""
    db = get_db()
    try:
        fr = db.execute(
            "SELECT * FROM friend_requests WHERE id = ? AND to_user_id = ? AND status = 'pending'",
            (request_id, request.user["id"]),
        ).fetchone()
        if not fr:
            return jsonify({"error": "Friend request not found"}), 404

        db.execute(
            "UPDATE friend_requests SET status = 'accepted', updated_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), request_id),
        )
        # Bidirectional friendship
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (fr["from_user_id"], fr["to_user_id"]))
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (fr["to_user_id"], fr["from_user_id"]))
        db.commit()

        return jsonify({"status": "accepted"})
    finally:
        db.close()


@social_bp.route("/friends/requests/<request_id>/reject", methods=["POST"])
@require_auth
def reject_friend_request(request_id):
    """Reject a pending friend request."""
    db = get_db()
    try:
        fr = db.execute(
            "SELECT * FROM friend_requests WHERE id = ? AND (to_user_id = ? OR from_user_id = ?) AND status = 'pending'",
            (request_id, request.user["id"], request.user["id"]),
        ).fetchone()
        if not fr:
            return jsonify({"error": "Friend request not found"}), 404

        db.execute(
            "UPDATE friend_requests SET status = 'rejected', updated_at = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), request_id),
        )
        db.commit()

        return jsonify({"status": "rejected"})
    finally:
        db.close()


@social_bp.route("/friends", methods=["GET"])
@require_auth
def list_friends():
    """List current user's friends."""
    db = get_db()
    try:
        rows = db.execute(
            """SELECT u.id, u.username, u.display_name, u.avatar_url, u.role, f.created_at as friends_since
               FROM friendships f
               JOIN users u ON f.friend_id = u.id
               WHERE f.user_id = ?
               ORDER BY u.display_name""",
            (request.user["id"],),
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

        return jsonify({"friends": friends})
    finally:
        db.close()


@social_bp.route("/friends/<user_id>", methods=["DELETE"])
@require_auth
def unfriend(user_id):
    """Remove a friend (bidirectional)."""
    db = get_db()
    try:
        my_id = request.user["id"]
        existing = db.execute(
            "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
            (my_id, user_id),
        ).fetchone()
        if not existing:
            return jsonify({"error": "Not friends"}), 404

        db.execute("DELETE FROM friendships WHERE (user_id = ? AND friend_id = ?) OR (user_id = ? AND friend_id = ?)",
                   (my_id, user_id, user_id, my_id))
        db.commit()

        return jsonify({"status": "unfriended"})
    finally:
        db.close()


# ── Wall Posts (uses news_posts table with source='user') ────────────────────


@social_bp.route("/posts", methods=["POST"])
@require_auth
def create_post():
    """Create a wall post (social post visible in the news feed)."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Post content required"}), 400

    title = (data.get("title") or "").strip() or content[:80]

    # Input length validation
    if len(title) > MAX_TITLE_LENGTH:
        return jsonify({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}), 400
    if len(content) > MAX_CONTENT_LENGTH:
        return jsonify({"error": f"Content must be at most {MAX_CONTENT_LENGTH} characters"}), 400

    # NOTE: XSS protection handled by after_request output escaping

    image_url = data.get("image_url")
    is_public = data.get("is_public", True)

    post_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    db = get_db()
    try:
        db.execute(
            """INSERT INTO news_posts
               (id, title, content, summary, image_url, author_id, author_name,
                source, source_url, post_type, is_public, published_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'user', NULL, 'social', ?, ?, ?, ?)""",
            (post_id, title, content, content[:200], image_url,
             request.user["id"], request.user["display_name"],
             1 if is_public else 0, now, now, now),
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
            "source": "user",
            "postType": "social",
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "date": now,
            "isPublic": is_public,
            "liked": False,
        }), 201
    finally:
        db.close()


@social_bp.route("/posts", methods=["GET"])
def get_user_posts():
    """Get posts by a specific user. Query param: user_id (required)."""
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id query param required"}), 400

    page, per_page, err = parse_pagination()
    if err:
        return err
    offset = (page - 1) * per_page

    current_user = get_current_user()

    db = get_db()
    try:
        # Check if the requested user exists
        target = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            return jsonify({"error": "User not found"}), 404

        # Determine if viewer can see private posts (self or friend)
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

        if can_see_private:
            rows = db.execute(
                """SELECT * FROM news_posts
                   WHERE author_id = ? AND source = 'user'
                   ORDER BY published_at DESC
                   LIMIT ? OFFSET ?""",
                (user_id, per_page, offset),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT * FROM news_posts
                   WHERE author_id = ? AND source = 'user' AND is_public = 1
                   ORDER BY published_at DESC
                   LIMIT ? OFFSET ?""",
                (user_id, per_page, offset),
            ).fetchall()

        posts = []
        for row in rows:
            p = {
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "summary": row["summary"],
                "imageUrl": row["image_url"],
                "author": row["author_name"],
                "authorId": row["author_id"],
                "source": row["source"],
                "postType": row["post_type"],
                "likes": row["likes_count"],
                "comments": row["comments_count"],
                "shares": row["shares_count"],
                "date": row["published_at"],
                "isPublic": bool(row["is_public"]),
                "liked": False,
            }
            posts.append(p)

        # Add like status for authenticated users
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

        if can_see_private:
            total = db.execute(
                "SELECT COUNT(*) as c FROM news_posts WHERE author_id = ? AND source = 'user'",
                (user_id,),
            ).fetchone()["c"]
        else:
            total = db.execute(
                "SELECT COUNT(*) as c FROM news_posts WHERE author_id = ? AND source = 'user' AND is_public = 1",
                (user_id,),
            ).fetchone()["c"]

        return jsonify({"posts": posts, "page": page, "perPage": per_page, "total": total, "hasMore": offset + per_page < total})
    finally:
        db.close()


@social_bp.route("/posts/<post_id>", methods=["PUT"])
@require_auth
def update_post(post_id):
    """Edit own wall post."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:
        post = db.execute(
            "SELECT * FROM news_posts WHERE id = ? AND author_id = ? AND source = 'user'",
            (post_id, request.user["id"]),
        ).fetchone()
        if not post:
            return jsonify({"error": "Post not found or not yours"}), 404

        updates = []
        params = []
        if "content" in data and data["content"].strip():
            content = data["content"].strip()
            if len(content) > MAX_CONTENT_LENGTH:
                return jsonify({"error": f"Content must be at most {MAX_CONTENT_LENGTH} characters"}), 400
            # NOTE: XSS protection handled by after_request output escaping
            updates.append("content = ?")
            params.append(content)
            updates.append("summary = ?")
            params.append(content[:200])
        if "title" in data:
            title = data["title"].strip()
            if len(title) > MAX_TITLE_LENGTH:
                return jsonify({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}), 400
            # NOTE: XSS protection handled by after_request output escaping
            updates.append("title = ?")
            params.append(title)
        if "image_url" in data:
            updates.append("image_url = ?")
            params.append(data["image_url"])

        if not updates:
            return jsonify({"error": "No fields to update"}), 400

        updates.append("updated_at = ?")
        params.append(datetime.utcnow().isoformat())
        params.append(post_id)

        db.execute(f"UPDATE news_posts SET {', '.join(updates)} WHERE id = ?", params)
        db.commit()

        return jsonify({"status": "updated"})
    finally:
        db.close()


@social_bp.route("/posts/<post_id>", methods=["DELETE"])
@require_auth
def delete_post(post_id):
    """Delete own wall post."""
    db = get_db()
    try:
        post = db.execute(
            "SELECT id FROM news_posts WHERE id = ? AND author_id = ? AND source = 'user'",
            (post_id, request.user["id"]),
        ).fetchone()
        if not post:
            return jsonify({"error": "Post not found or not yours"}), 404

        db.execute("DELETE FROM news_posts WHERE id = ?", (post_id,))
        db.commit()

        return jsonify({"status": "deleted"})
    finally:
        db.close()
