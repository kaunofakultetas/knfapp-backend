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
#  Three routes are dead as far as the mobile app goes —
#  GET /profile (the app reads itself via GET /api/auth/me),
#  POST /posts (the app posts through POST /api/news) and
#  PUT /posts/<id> (no edit UI). The rest map 1:1 onto
#  services/api/social.ts. Nothing in swagger/ documents
#  this blueprint.
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
#    PUT    /api/social/posts/<post_id>              — edit own post (unused)
#    DELETE /api/social/posts/<post_id>              — delete own post
############################################################


import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.api import parse_pagination
from app.auth.routes import get_current_user, require_auth
from app.database import get_db

# Same values as news/routes.py — duplicated there rather
# than imported, so the two pairs are kept in sync by hand
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 10000

social_bp = Blueprint("social", __name__)








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
# user posts only. Pagination is parse_pagination's default,
# so per_page caps at 100 — not the 50 the old docstring
# promised.
#
# Ranking is computed in SQL: 100 / (1 + age in days) for
# recency plus min(likes + 2*comments + 3*shares, 100) / 2
# for engagement — engagement tops out at 50, so a brand
# new post always outranks a day-old one. The two SELECT
# aliases (recency_score, engagement_score) are never read:
# the ORDER BY repeats the expression instead of naming
# them and the loop below ignores them. julianday() copes
# with both published_at formats in the column (ISO "T"
# from create_post, datetime('now') from the scraper).
#
# Gotchas:
#   - one extra SELECT per post for the author avatar (N+1,
#     up to 100 per page); author_name itself is the
#     snapshot create_post stored, so a display-name change
#     never reaches old posts.
#   - "liked" is filled by one IN (...) query afterwards;
#     anonymous viewers always get liked = False.
#   - the COUNT(*) reuses where_sql so total covers the
#     same visibility set; params[:-2] drops LIMIT/OFFSET
#     (the len > 2 guard is redundant — slicing a 2-list to
#     [:-2] is already []).
#   - the friend ids are spliced in as "?" placeholders, one
#     per friend, so the statement grows with the friend
#     count (SQLite's bound-variable limit is far away).
#
# Used by:
#   - services/api/social.ts — fetchSocialFeed
#   - app/(main)/tabs/news.tsx — the "community" feed mode,
#     called only when logged in (the anonymous branch is
#     reachable through the API but never from the app)
############################################################

@social_bp.route("/feed", methods=["GET"])
def social_feed():
    # STEP 1: pagination + optional viewer — None means the
    # anonymous, public-only view
    # =====================================================
    page, per_page, err = parse_pagination()
    if err:
        return err
    offset = (page - 1) * per_page

    current_user = get_current_user()

    db = get_db()
    try:


        # STEP 2: visibility WHERE — own + friends' posts at any
        # visibility, or public-only for guests
        # ======================================================
        where_clauses = ["source = 'user'"]
        params = []

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
                f"(author_id IN ({placeholders}) OR is_public = 1)"
            )
            params.extend(visible_ids)
        else:
            where_clauses.append("is_public = 1")

        where_sql = " AND ".join(where_clauses)


        # STEP 3: the ranked page (formula in the banner)
        # ===============================================
        query = f"""
            SELECT *,
                (1.0 / (1.0 + (julianday('now') - julianday(published_at)))) * 100 AS recency_score,
                MIN(likes_count + comments_count * 2 + shares_count * 3, 100) * 0.5 AS engagement_score
            FROM news_posts
            WHERE {where_sql}
            ORDER BY (
                (1.0 / (1.0 + (julianday('now') - julianday(published_at)))) * 100
                + MIN(likes_count + comments_count * 2 + shares_count * 3, 100) * 0.5
            ) DESC, published_at DESC
            LIMIT ? OFFSET ?
        """
        params.extend([per_page, offset])

        rows = db.execute(query, params).fetchall()


        # STEP 4: shape the rows for the client, one avatar
        # lookup per post
        # =================================================
        posts = []
        for row in rows:
            # source='user' rows always carry an author_id; the
            # guard only matters for hand-inserted rows
            author_avatar = None
            if row["author_id"]:
                author_row = db.execute(
                    "SELECT avatar_url FROM users WHERE id = ?",
                    (row["author_id"],),
                ).fetchone()
                if author_row:
                    author_avatar = author_row["avatar_url"]

            posts.append({
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "summary": row["summary"],
                "imageUrl": row["image_url"],
                "author": row["author_name"],
                "authorId": row["author_id"],
                "authorAvatar": author_avatar,
                "source": row["source"],
                "postType": row["post_type"],
                "likes": row["likes_count"],
                "comments": row["comments_count"],
                "shares": row["shares_count"],
                "date": row["published_at"],
                "isPublic": bool(row["is_public"]),
                "liked": False,
            })


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


        # STEP 6: total for hasMore — same WHERE, LIMIT/OFFSET
        # params stripped
        # ====================================================
        count_params = params[:-2] if len(params) > 2 else []
        total = db.execute(
            f"SELECT COUNT(*) as c FROM news_posts WHERE {where_sql}",
            count_params,
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
# Anyone's public profile — no auth needed, and a
# deactivated account (users.active = 0) is still served.
# postCount counts the user's wall posts regardless of
# visibility, so a stranger can see a higher count than
# GET /posts will list for them. friendshipStatus is from
# the VIEWER's side: 'friends', 'request_sent' (the viewer
# asked), 'request_received' (the profile owner asked) or
# 'none' — also 'none' for anonymous viewers and for a user
# looking at their own profile.
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

        # Friendship from the viewer's side: the friendships row
        # is checked in the viewer's direction only, then a
        # pending request in either direction decides sent/received
        current_user = get_current_user()
        friendship_status = "none"
        if current_user and current_user["id"] != user_id:
            is_friend = db.execute(
                "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
                (current_user["id"], user_id),
            ).fetchone()
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
# request.user is the dict get_current_user built, hence
# .get() for the migration-v5 columns. No mobile caller:
# the app reads its own identity through GET /api/auth/me
# (services/api/auth.ts fetchMe).
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
        post_count = db.execute(
            "SELECT COUNT(*) as c FROM news_posts WHERE author_id = ? AND source = 'user'",
            (user["id"],),
        ).fetchone()["c"]

        friend_count = db.execute(
            "SELECT COUNT(*) as c FROM friendships WHERE user_id = ?",
            (user["id"],),
        ).fetchone()["c"]

        return jsonify({
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "displayName": user["display_name"],
            "avatarUrl": user["avatar_url"],
            "role": user["role"],
            "createdAt": user["created_at"],
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
#   - an empty/whitespace displayName is silently skipped,
#     not rejected; only a body with NO usable field is 400.
#   - avatar_url is stored as sent: no type or scheme check
#     (the migration-time http/https cleanup was one-off),
#     null clears it, and a list/object makes sqlite3 refuse
#     the bind → 500.
#   - student fields: blank → NULL, over 50 chars → 400 that
#     names the key the client sent, not the column.
#   - updated_at is written as utcnow().isoformat() ("T",
#     microseconds) while the column default is
#     datetime('now') — two formats share the column.
#   - author_name on existing posts is NOT rewritten: it is
#     the snapshot create_post took.
#   - the response is a hand-picked subset (no invited,
#     createdAt, active) — the app merges it into its cached
#     user instead of replacing it.
#
# Used by:
#   - services/api/social.ts — updateProfile
#   - app/(main)/tabs/id.tsx — student-card field edits
#   - app/(main)/profile/index.tsx — avatar / name edits
############################################################

@social_bp.route("/profile", methods=["PUT"])
@require_auth
def update_profile():
    # STEP 1: body
    # ============
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:


        # STEP 2: collect "col = ?" fragments — display name and
        # avatar first, camelCase key winning over snake_case
        # ======================================================
        updates = []
        params = []
        dn_key = "displayName" if "displayName" in data else "display_name"
        if dn_key in data:
            if not isinstance(data[dn_key], str):
                return jsonify({"error": "display_name must be a string"}), 400
            display_name = data[dn_key].strip()
            if display_name:
                if len(display_name) > 100:
                    return jsonify({"error": "Display name must be at most 100 characters"}), 400
                updates.append("display_name = ?")
                params.append(display_name)
        av_key = "avatarUrl" if "avatarUrl" in data else "avatar_url"
        if av_key in data:
            updates.append("avatar_url = ?")
            params.append(data[av_key])

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
                    val = str(val).strip()
                    if len(val) > 50:
                        return jsonify({"error": f"{field} must be at most 50 characters"}), 400
                    if not val:
                        val = None
                updates.append(f"{col} = ?")
                params.append(val)

        if not updates:
            return jsonify({"error": "No fields to update"}), 400


        # STEP 3: one UPDATE, then re-read the row for the response
        # =========================================================
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
# friendships rows written) instead of a second pending
# row. 400 self-request, 404 unknown target, 409 already
# friends or a request already pending in either
# direction. A previously rejected request does not block a
# new one — every attempt is a fresh row, so history piles
# up in friend_requests.
#
# The "already friends" check reads only the (me, target)
# direction — fine while accept / auto-accept always write
# both rows. The target's users.active flag is not checked:
# deactivated accounts can still be requested.
#
# Used by:
#   - services/api/social.ts — sendFriendRequest
#   - app/(main)/profile/index.tsx — the "add friend" action
############################################################

@social_bp.route("/friends/request", methods=["POST"])
@require_auth
def send_friend_request():
    # STEP 1: body + the self-request guard
    # =====================================
    data = request.get_json()
    if not data or not data.get("user_id"):
        return jsonify({"error": "user_id required"}), 400

    target_id = data["user_id"]
    my_id = request.user["id"]

    if target_id == my_id:
        return jsonify({"error": "Cannot friend yourself"}), 400

    db = get_db()
    try:


        # STEP 2: target must exist and not already be a friend
        # =====================================================
        target = db.execute("SELECT id, display_name FROM users WHERE id = ?", (target_id,)).fetchone()
        if not target:
            return jsonify({"error": "User not found"}), 404

        existing = db.execute(
            "SELECT 1 FROM friendships WHERE user_id = ? AND friend_id = ?",
            (my_id, target_id),
        ).fetchone()
        if existing:
            return jsonify({"error": "Already friends"}), 409


        # STEP 3: a pending request in either direction — theirs
        # is accepted on the spot, ours is a duplicate (409)
        # ======================================================
        pending = db.execute(
            "SELECT id, from_user_id FROM friend_requests WHERE status = 'pending' AND "
            "((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))",
            (my_id, target_id, target_id, my_id),
        ).fetchone()

        if pending:
            if pending["from_user_id"] == target_id:
                db.execute(
                    "UPDATE friend_requests SET status = 'accepted', updated_at = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(), pending["id"]),
                )
                db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (my_id, target_id))
                db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (target_id, my_id))
                db.commit()
                return jsonify({"status": "accepted", "message": "Friend request auto-accepted (they already requested you)"}), 200
            return jsonify({"error": "Friend request already pending"}), 409


        # STEP 4: a fresh pending row
        # ===========================
        req_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO friend_requests (id, from_user_id, to_user_id) VALUES (?, ?, ?)",
            (req_id, my_id, target_id),
        )
        db.commit()

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
# party's public columns joined in. ?direction=sent lists
# what the caller sent; anything else (the default
# included) lists what they received. The request id is
# what accept / reject take — the profile screen fetches
# this list purely to find the id before accepting.
#
# Used by:
#   - services/api/social.ts — fetchFriendRequests
#   - app/(main)/friend-requests/index.tsx — the received
#     list (the app never asks for direction=sent)
#   - app/(main)/friends/index.tsx — pending-count badge
#   - app/(main)/profile/index.tsx — resolve the request id
#     before accepting from the profile
############################################################

@social_bp.route("/friends/requests", methods=["GET"])
@require_auth
def list_friend_requests():
    direction = request.args.get("direction", "received")  # anything but 'sent' means received
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








############################################################
# accept_friend_request
############################################################
#
# POST /api/social/friends/requests/<request_id>/accept
#
# Only the RECIPIENT of a still-pending request can accept
# (anything else is a 404, not a 403). Flips status to
# 'accepted' and writes the friendships row in both
# directions in one commit — every reader (feeds, counts,
# unfriend) only ever looks up its own direction, so both
# rows must exist. Should the pair somehow already have a
# friendships row, the INSERT hits the (user_id, friend_id)
# primary key and the request 500s.
#
# Used by:
#   - services/api/social.ts — acceptFriendRequest
#   - app/(main)/friend-requests/index.tsx — accept button
#   - app/(main)/profile/index.tsx — accept from the profile
#     ('request_received' state)
############################################################

@social_bp.route("/friends/requests/<request_id>/accept", methods=["POST"])
@require_auth
def accept_friend_request(request_id):
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
        # Both directions — nothing ever queries the reverse one
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (fr["from_user_id"], fr["to_user_id"]))
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (fr["to_user_id"], fr["from_user_id"]))
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
# Either party may call it on a pending request: the
# recipient declines, the sender cancels — both land as
# status 'rejected' (there is no 'cancelled'). 404 for an
# unknown, foreign or already-settled request. The row is
# kept rather than deleted and does not stop a fresh
# request later.
#
# Used by:
#   - services/api/social.ts — rejectFriendRequest
#   - app/(main)/friend-requests/index.tsx — decline button
#     (the app has no cancel-sent UI, so the sender path is
#     reachable through the API only)
############################################################

@social_bp.route("/friends/requests/<request_id>/reject", methods=["POST"])
@require_auth
def reject_friend_request(request_id):
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








############################################################
# list_friends
############################################################
#
# GET /api/social/friends
#
# The caller's friends with friendsSince (the friendships
# row's created_at, i.e. the accept time). ORDER BY
# u.display_name runs under SQLite's default BINARY
# collation: uppercase sorts before lowercase and
# Lithuanian letters after all of ASCII, so the list is not
# what a user would call alphabetical. Deactivated friends
# are still listed.
#
# Used by:
#   - services/api/social.ts — fetchFriends
#   - app/(main)/friends/index.tsx — the friends list
############################################################

@social_bp.route("/friends", methods=["GET"])
@require_auth
def list_friends():
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








############################################################
# unfriend
############################################################
#
# DELETE /api/social/friends/<user_id>
#
# Removes both friendships rows in one commit; 404 when the
# (me, them) row is missing. The old 'accepted'
# friend_requests row stays as history and does not block a
# new request — either side can ask again immediately.
#
# Used by:
#   - services/api/social.ts — unfriendUser
#   - app/(main)/profile/index.tsx — remove-friend action
#     (behind a ConfirmDialog)
############################################################

@social_bp.route("/friends/<user_id>", methods=["DELETE"])
@require_auth
def unfriend(user_id):
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








############################################################
# create_post
############################################################
#
# POST /api/social/posts
#
# Inserts a wall post: source 'user', post_type 'social',
# summary = the first 200 chars, title = the given one or
# the first 80 chars of content. author_name is snapshotted
# from the caller's display_name. is_public is any truthy
# JSON value (stored as 1/0) but the response echoes it
# verbatim, so a client can get "isPublic": "yes" back.
# image_url is unvalidated. published_at, created_at and
# updated_at all receive the same utcnow().isoformat().
#
# Not used by the app: create-post/index.tsx posts through
# POST /api/news (news/routes.py create_post), which is the
# same insert plus role-based source / post_type and the
# poll attachment.
#
# Used by:
#   - nothing calls this at the moment
############################################################

@social_bp.route("/posts", methods=["POST"])
@require_auth
def create_post():
    # STEP 1: body + content — a non-string is 400, empty is 400
    # ==========================================================
    data = request.get_json()
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
    is_public = data.get("is_public", True)


    # STEP 3: insert and echo the row the client would otherwise
    # have to refetch
    # ==========================================================
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








############################################################
# get_user_posts
############################################################
#
# GET /api/social/posts?user_id=<id>
#
# One user's wall posts, newest first, paged like the feed
# (per_page capped at 100). Private posts are included only
# when the viewer IS that user or their friend (checked in
# the viewer's friendships direction); anyone else —
# anonymous included — gets is_public = 1 only. 404 for an
# unknown user; a deactivated user's posts are still served.
# "liked" is filled the same way as in social_feed. The
# public/private branches are two full query pairs rather
# than a WHERE fragment, so the page query and the COUNT
# each exist twice.
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

    page, per_page, err = parse_pagination()
    if err:
        return err
    offset = (page - 1) * per_page

    current_user = get_current_user()

    db = get_db()
    try:


        # STEP 2: target must exist; private posts only for self
        # or a friend
        # ======================================================
        target = db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
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


        # STEP 3: the page, newest first
        # ==============================
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


        # STEP 5: total for hasMore, same visibility split
        # ================================================
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








############################################################
# update_post
############################################################
#
# PUT /api/social/posts/<post_id>
#
# Edits content / title / image_url of the caller's own
# wall post; a post that exists but belongs to someone else
# (or is not source 'user') is a 404, not a 403.
# Asymmetries with create_post: empty content is silently
# skipped (only non-empty content updates content +
# summary), but an empty title IS stored — there is no
# "first 80 chars" fallback here. published_at is untouched,
# so an edit never re-ranks the post in the feed. Stored
# raw and escaped on output like everything else.
#
# Used by:
#   - nothing calls this at the moment — the app has no
#     post-edit UI (services/api/social.ts stops at delete)
############################################################

@social_bp.route("/posts/<post_id>", methods=["PUT"])
@require_auth
def update_post(post_id):
    # STEP 1: body
    # ============
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    db = get_db()
    try:


        # STEP 2: ownership — someone else's post reads as missing
        # ========================================================
        post = db.execute(
            "SELECT * FROM news_posts WHERE id = ? AND author_id = ? AND source = 'user'",
            (post_id, request.user["id"]),
        ).fetchone()
        if not post:
            return jsonify({"error": "Post not found or not yours"}), 404


        # STEP 3: collect "col = ?" fragments for the fields present
        # ==========================================================
        updates = []
        params = []
        if "content" in data:
            if not isinstance(data["content"], str):
                return jsonify({"error": "content must be a string"}), 400
            content = data["content"].strip()
            if content:
                if len(content) > MAX_CONTENT_LENGTH:
                    return jsonify({"error": f"Content must be at most {MAX_CONTENT_LENGTH} characters"}), 400
                updates.append("content = ?")
                params.append(content)
                updates.append("summary = ?")
                params.append(content[:200])
        if "title" in data:
            if not isinstance(data["title"], str):
                return jsonify({"error": "title must be a string"}), 400
            title = data["title"].strip()
            if len(title) > MAX_TITLE_LENGTH:
                return jsonify({"error": f"Title must be at most {MAX_TITLE_LENGTH} characters"}), 400
            # no emptiness check — "" is a valid new title here
            updates.append("title = ?")
            params.append(title)
        if "image_url" in data:
            updates.append("image_url = ?")
            params.append(data["image_url"])

        if not updates:
            return jsonify({"error": "No fields to update"}), 400


        # STEP 4: one UPDATE, updated_at stamped in isoformat
        # ===================================================
        updates.append("updated_at = ?")
        params.append(datetime.utcnow().isoformat())
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
# by hand). This one is a bare DELETE on news_posts and
# relies on the ON DELETE CASCADE from news_likes,
# news_comments and polls (→ poll_options, poll_votes) —
# which only fires because get_db turns PRAGMA foreign_keys
# on per connection. Like update_post, someone else's post
# is a 404 rather than a 403.
#
# Used by:
#   - services/api/social.ts — deletePost
#   - app/(main)/profile/index.tsx — own-post delete menu
#     (behind a ConfirmDialog)
############################################################

@social_bp.route("/posts/<post_id>", methods=["DELETE"])
@require_auth
def delete_post(post_id):
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
