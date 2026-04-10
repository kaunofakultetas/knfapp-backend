"""Chat/messaging routes — conversations, messages, reactions."""

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.auth.routes import require_auth
from app.database import get_db

chat_bp = Blueprint("chat", __name__)


def _get_socketio():
    """Lazy import socketio to avoid circular imports."""
    from app import socketio
    return socketio


def _format_time(iso_str):
    """Format ISO datetime to HH:MM for display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _emit_reaction_update(db, conv_id, msg_id, current_user_id):
    """Fetch current reactions for a message and emit to the conversation room."""
    rows = db.execute(
        "SELECT mr.emoji, mr.user_id FROM message_reactions mr WHERE mr.message_id = ?",
        (msg_id,),
    ).fetchall()

    reaction_map = {}
    for r in rows:
        emoji = r["emoji"]
        if emoji not in reaction_map:
            reaction_map[emoji] = []
        reaction_map[emoji].append(r["user_id"])

    reactions = [
        {"emoji": emoji, "count": len(uids), "byUserIds": uids}
        for emoji, uids in reaction_map.items()
    ]

    from app.chat.events import emit_reaction_update
    emit_reaction_update(_get_socketio(), conv_id, msg_id, reactions)


@chat_bp.route("/conversations", methods=["GET"])
@require_auth
def list_conversations():
    """List all conversations the current user participates in."""
    user_id = request.user["id"]
    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT c.id, c.type, c.title, c.avatar_emoji, c.created_at, c.updated_at,
                   cp.pinned, cp.last_read_at
            FROM conversations c
            JOIN conversation_participants cp ON cp.conversation_id = c.id
            WHERE cp.user_id = ?
            ORDER BY cp.pinned DESC, c.updated_at DESC
            """,
            (user_id,),
        ).fetchall()

        conversations = []
        for row in rows:
            conv_id = row["id"]

            # Get participants
            participants = db.execute(
                """
                SELECT u.id, u.display_name, u.avatar_url
                FROM conversation_participants cp
                JOIN users u ON u.id = cp.user_id
                WHERE cp.conversation_id = ?
                """,
                (conv_id,),
            ).fetchall()

            # Get last message
            last_msg = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       u.display_name AS sender_name
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                WHERE m.conversation_id = ?
                ORDER BY m.created_at DESC LIMIT 1
                """,
                (conv_id,),
            ).fetchone()

            # Count unread messages
            last_read = row["last_read_at"] or "1970-01-01T00:00:00"
            unread = db.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE conversation_id = ? AND sender_id != ? AND created_at > ?
                """,
                (conv_id, user_id, last_read),
            ).fetchone()[0]

            # Build title for direct conversations
            title = row["title"]
            if row["type"] == "direct" and not title:
                other = [p for p in participants if p["id"] != user_id]
                title = other[0]["display_name"] if other else "Chat"

            conv = {
                "id": conv_id,
                "type": row["type"],
                "title": title,
                "avatarEmoji": row["avatar_emoji"],
                "pinned": bool(row["pinned"]),
                "unreadCount": unread,
                "lastUpdatedMs": int(
                    datetime.fromisoformat(row["updated_at"]).timestamp() * 1000
                ),
                "participants": [
                    {
                        "id": p["id"],
                        "displayName": p["display_name"],
                        "avatarUrl": p["avatar_url"],
                    }
                    for p in participants
                ],
            }

            if last_msg:
                conv["lastMessage"] = {
                    "id": last_msg["id"],
                    "text": last_msg["text"] or "",
                    "imageUrl": last_msg["image_url"],
                    "time": _format_time(last_msg["created_at"]),
                    "senderId": last_msg["sender_id"],
                    "senderName": last_msg["sender_name"],
                }

            conversations.append(conv)

        return jsonify({"conversations": conversations})
    finally:
        db.close()


@chat_bp.route("/conversations", methods=["POST"])
@require_auth
def create_conversation():
    """Create a new conversation (direct or group)."""
    user_id = request.user["id"]
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    participant_ids = data.get("participantIds", [])
    if not isinstance(participant_ids, list) or not participant_ids:
        return jsonify({"error": "participantIds must be a non-empty array"}), 400

    conv_type = data.get("type", "direct")
    title = data.get("title")
    avatar_emoji = data.get("avatarEmoji")

    # Ensure creator is included
    all_ids = list(set([user_id] + participant_ids))

    # For direct chats, check if conversation already exists between these two users
    if conv_type == "direct" and len(all_ids) == 2:
        db = get_db()
        try:
            other_id = [uid for uid in all_ids if uid != user_id][0]
            existing = db.execute(
                """
                SELECT c.id FROM conversations c
                WHERE c.type = 'direct'
                AND EXISTS (SELECT 1 FROM conversation_participants WHERE conversation_id = c.id AND user_id = ?)
                AND EXISTS (SELECT 1 FROM conversation_participants WHERE conversation_id = c.id AND user_id = ?)
                """,
                (user_id, other_id),
            ).fetchone()

            if existing:
                return jsonify({"conversationId": existing["id"]}), 200
        finally:
            db.close()

    db = get_db()
    try:
        # Validate all participant IDs exist
        placeholders = ",".join("?" * len(all_ids))
        found = db.execute(
            f"SELECT id FROM users WHERE id IN ({placeholders})", all_ids
        ).fetchall()
        if len(found) != len(all_ids):
            return jsonify({"error": "One or more participant IDs are invalid"}), 400

        conv_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        db.execute(
            "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conv_id, conv_type, title, avatar_emoji, user_id, now, now),
        )

        for uid in all_ids:
            db.execute(
                "INSERT INTO conversation_participants (conversation_id, user_id, last_read_at) VALUES (?, ?, ?)",
                (conv_id, uid, now),
            )

        db.commit()
        return jsonify({"conversationId": conv_id}), 201
    finally:
        db.close()


@chat_bp.route("/conversations/<conv_id>/messages", methods=["GET"])
@require_auth
def get_messages(conv_id):
    """Get messages for a conversation, paginated by cursor."""
    user_id = request.user["id"]
    db = get_db()
    try:
        # Verify user is participant
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        before = request.args.get("before")  # cursor: created_at ISO string
        limit = min(int(request.args.get("limit", 50)), 100)

        if before:
            rows = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       u.display_name AS sender_name, u.avatar_url AS sender_avatar
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                WHERE m.conversation_id = ? AND m.created_at < ?
                ORDER BY m.created_at DESC LIMIT ?
                """,
                (conv_id, before, limit),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       u.display_name AS sender_name, u.avatar_url AS sender_avatar
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                WHERE m.conversation_id = ?
                ORDER BY m.created_at DESC LIMIT ?
                """,
                (conv_id, limit),
            ).fetchall()

        # Batch-load all reactions for fetched messages
        msg_ids = [row["id"] for row in rows]
        reaction_map_all = {}
        read_map_all = {}
        if msg_ids:
            placeholders = ",".join("?" * len(msg_ids))

            reactions_rows = db.execute(
                f"""
                SELECT mr.message_id, mr.emoji, mr.user_id, u.display_name
                FROM message_reactions mr
                JOIN users u ON u.id = mr.user_id
                WHERE mr.message_id IN ({placeholders})
                """,
                msg_ids,
            ).fetchall()

            for r in reactions_rows:
                mid = r["message_id"]
                if mid not in reaction_map_all:
                    reaction_map_all[mid] = {}
                emoji = r["emoji"]
                if emoji not in reaction_map_all[mid]:
                    reaction_map_all[mid][emoji] = []
                reaction_map_all[mid][emoji].append(r["user_id"])

            # Batch-load read receipts
            reads_rows = db.execute(
                f"""
                SELECT mrd.message_id, mrd.user_id
                FROM message_reads mrd
                WHERE mrd.message_id IN ({placeholders})
                """,
                msg_ids,
            ).fetchall()

            for rd in reads_rows:
                mid = rd["message_id"]
                if mid not in read_map_all:
                    read_map_all[mid] = []
                read_map_all[mid].append(rd["user_id"])

        # Get participant count for read receipt status
        participant_count = db.execute(
            "SELECT COUNT(*) FROM conversation_participants WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()[0]

        messages = []
        for row in rows:
            msg_id = row["id"]

            # Build reactions from batch data
            msg_reactions = reaction_map_all.get(msg_id, {})
            reactions = []
            for emoji, uids in msg_reactions.items():
                reactions.append({
                    "emoji": emoji,
                    "count": len(uids),
                    "bySelf": user_id in uids,
                    "byUserIds": uids,
                })

            # Build read receipt status
            read_by = read_map_all.get(msg_id, [])
            is_own = row["sender_id"] == user_id
            if is_own:
                # For own messages: "read" if all other participants read it,
                # "delivered" if some read, "sent" if none
                other_readers = [uid for uid in read_by if uid != user_id]
                others_count = participant_count - 1  # exclude sender
                if others_count <= 0 or len(other_readers) >= others_count:
                    status = "read"
                elif len(other_readers) > 0:
                    status = "delivered"
                else:
                    status = "sent"
            else:
                status = "read"

            messages.append({
                "id": msg_id,
                "conversationId": conv_id,
                "senderId": row["sender_id"],
                "senderName": row["sender_name"],
                "senderAvatar": row["sender_avatar"],
                "text": row["text"],
                "imageUrl": row["image_url"],
                "time": _format_time(row["created_at"]),
                "createdAt": row["created_at"],
                "isOwn": is_own,
                "status": status,
                "readBy": read_by,
                "reactions": reactions,
            })

        # Reverse to chronological order
        messages.reverse()

        has_more = len(rows) == limit

        return jsonify({"messages": messages, "hasMore": has_more})
    finally:
        db.close()


@chat_bp.route("/conversations/<conv_id>/messages", methods=["POST"])
@require_auth
def send_message(conv_id):
    """Send a message to a conversation."""
    user_id = request.user["id"]
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    raw_text = data.get("text", "")
    if not isinstance(raw_text, str):
        return jsonify({"error": "Text must be a string"}), 400
    text = raw_text.strip()
    image_url = data.get("imageUrl")

    if not text and not image_url:
        return jsonify({"error": "Message must have text or image"}), 400

    if text and len(text) > 5000:
        return jsonify({"error": "Message text must not exceed 5000 characters"}), 400

    db = get_db()
    try:
        # Verify user is participant
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        msg_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        db.execute(
            "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, conv_id, user_id, text, image_url, now),
        )

        # Update conversation timestamp
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conv_id),
        )

        # Update sender's last_read_at
        db.execute(
            "UPDATE conversation_participants SET last_read_at = ? WHERE conversation_id = ? AND user_id = ?",
            (now, conv_id, user_id),
        )

        # Sender auto-reads their own message
        db.execute(
            "INSERT OR IGNORE INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
            (msg_id, user_id, now),
        )

        db.commit()

        user = request.user
        msg_data = {
            "id": msg_id,
            "conversationId": conv_id,
            "senderId": user_id,
            "senderName": user["display_name"],
            "text": text,
            "imageUrl": image_url,
            "time": _format_time(now),
            "createdAt": now,
            "reactions": [],
        }

        # Emit real-time event to all conversation participants
        from app.chat.events import emit_new_message
        emit_new_message(_get_socketio(), conv_id, msg_data)

        # Push notifications for offline participants
        try:
            from app.chat.events import _connected_users
            from app.notifications.push import notify_user

            # Find which users are online in this conversation via Socket.IO
            online_user_ids = set(_connected_users.values())

            # Get all participants except sender
            participants = db.execute(
                "SELECT user_id FROM conversation_participants WHERE conversation_id = ? AND user_id != ?",
                (conv_id, user_id),
            ).fetchall()

            sender_name = user["display_name"]
            preview = text[:100] if text else "(image)"
            for p in participants:
                pid = p["user_id"]
                if pid not in online_user_ids:
                    notify_user(
                        pid,
                        sender_name,
                        preview,
                        data={"type": "chat_message", "conversationId": conv_id},
                    )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Push notification failed for chat message")

        # REST response includes isOwn=True and status for the sender
        # New message: only sender has read it, so status is "sent" (no other readers yet)
        return jsonify({"message": {**msg_data, "isOwn": True, "status": "sent", "readBy": [user_id]}}), 201
    finally:
        db.close()


@chat_bp.route("/conversations/<conv_id>/messages/<msg_id>/react", methods=["POST"])
@require_auth
def react_to_message(conv_id, msg_id):
    """Add or change reaction on a message. One emoji per user per message."""
    user_id = request.user["id"]
    data = request.get_json()
    if not data or not data.get("emoji"):
        return jsonify({"error": "emoji required"}), 400

    if not isinstance(data["emoji"], str):
        return jsonify({"error": "emoji must be a string"}), 400

    emoji = data["emoji"]
    if len(emoji) > 32:
        return jsonify({"error": "emoji too long"}), 400
    db = get_db()
    try:
        # Verify user is participant
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        # Verify message exists in this conversation
        msg = db.execute(
            "SELECT 1 FROM messages WHERE id = ? AND conversation_id = ?",
            (msg_id, conv_id),
        ).fetchone()
        if not msg:
            return jsonify({"error": "Message not found"}), 404

        # Upsert: delete existing reaction, insert new one
        db.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND user_id = ?",
            (msg_id, user_id),
        )
        db.execute(
            "INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
            (msg_id, user_id, emoji),
        )
        db.commit()

        # Fetch updated reactions and emit to room
        _emit_reaction_update(db, conv_id, msg_id, user_id)

        return jsonify({"ok": True, "emoji": emoji})
    finally:
        db.close()


@chat_bp.route("/conversations/<conv_id>/messages/<msg_id>/react", methods=["DELETE"])
@require_auth
def remove_reaction(conv_id, msg_id):
    """Remove user's reaction from a message."""
    user_id = request.user["id"]
    db = get_db()
    try:
        db.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND user_id = ?",
            (msg_id, user_id),
        )
        db.commit()

        # Emit updated reactions to room
        _emit_reaction_update(db, conv_id, msg_id, user_id)

        return jsonify({"ok": True})
    finally:
        db.close()


@chat_bp.route("/conversations/<conv_id>/pin", methods=["PUT"])
@require_auth
def toggle_pin(conv_id):
    """Toggle pin status for a conversation."""
    user_id = request.user["id"]
    db = get_db()
    try:
        row = db.execute(
            "SELECT pinned FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not row:
            return jsonify({"error": "Not a participant"}), 403

        new_pinned = 0 if row["pinned"] else 1
        db.execute(
            "UPDATE conversation_participants SET pinned = ? WHERE conversation_id = ? AND user_id = ?",
            (new_pinned, conv_id, user_id),
        )
        db.commit()
        return jsonify({"pinned": bool(new_pinned)})
    finally:
        db.close()


@chat_bp.route("/conversations/<conv_id>/read", methods=["PUT"])
@require_auth
def mark_read(conv_id):
    """Mark conversation as read up to now.
    Also inserts per-message read receipts and broadcasts to participants."""
    user_id = request.user["id"]
    now = datetime.utcnow().isoformat()
    db = get_db()
    try:
        # Verify user is participant
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        # Update the conversation-level last_read_at
        db.execute(
            "UPDATE conversation_participants SET last_read_at = ? WHERE conversation_id = ? AND user_id = ?",
            (now, conv_id, user_id),
        )

        # Insert per-message read receipts for all unread messages in this conversation
        # (messages from OTHER users that this user hasn't read yet)
        unread_msgs = db.execute(
            """
            SELECT m.id FROM messages m
            WHERE m.conversation_id = ? AND m.sender_id != ?
            AND NOT EXISTS (
                SELECT 1 FROM message_reads mr
                WHERE mr.message_id = m.id AND mr.user_id = ?
            )
            """,
            (conv_id, user_id, user_id),
        ).fetchall()

        newly_read_ids = []
        for msg in unread_msgs:
            db.execute(
                "INSERT OR IGNORE INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
                (msg["id"], user_id, now),
            )
            newly_read_ids.append(msg["id"])

        db.commit()

        # Broadcast read receipt event to conversation participants
        if newly_read_ids:
            from app.chat.events import emit_read_receipt
            emit_read_receipt(
                _get_socketio(), conv_id, user_id, newly_read_ids
            )

        return jsonify({"ok": True, "readCount": len(newly_read_ids)})
    finally:
        db.close()


@chat_bp.route("/conversations/<conv_id>", methods=["DELETE"])
@require_auth
def leave_conversation(conv_id):
    """Leave (remove self from) a conversation."""
    user_id = request.user["id"]
    db = get_db()
    try:
        db.execute(
            "DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        )

        # If no participants remain, delete the conversation and its messages
        remaining = db.execute(
            "SELECT COUNT(*) FROM conversation_participants WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()[0]

        if remaining == 0:
            db.execute("DELETE FROM message_reads WHERE message_id IN (SELECT id FROM messages WHERE conversation_id = ?)", (conv_id,))
            db.execute("DELETE FROM message_reactions WHERE message_id IN (SELECT id FROM messages WHERE conversation_id = ?)", (conv_id,))
            db.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))

        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@chat_bp.route("/unread-count", methods=["GET"])
@require_auth
def total_unread_count():
    """Get total unread message count across all conversations.
    Used for the Messages tab badge."""
    user_id = request.user["id"]
    db = get_db()
    try:
        total = db.execute(
            """
            SELECT COALESCE(SUM(unread), 0) AS total FROM (
                SELECT COUNT(*) AS unread
                FROM messages m
                JOIN conversation_participants cp
                  ON cp.conversation_id = m.conversation_id AND cp.user_id = ?
                WHERE m.conversation_id = cp.conversation_id
                  AND m.sender_id != ?
                  AND m.created_at > COALESCE(cp.last_read_at, '1970-01-01T00:00:00')
                GROUP BY m.conversation_id
            )
            """,
            (user_id, user_id),
        ).fetchone()[0]
        return jsonify({"unreadCount": total})
    finally:
        db.close()


@chat_bp.route("/conversations/<conv_id>/messages/search", methods=["GET"])
@require_auth
def search_messages(conv_id):
    """Search messages within a conversation by text content.

    Query params:
      - q (str, required): search query (min 1 char)
      - limit (int, default 20, max 50): max results
    """
    user_id = request.user["id"]
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify({"error": "q parameter is required and must not be empty"}), 400

    limit = min(int(request.args.get("limit", 20)), 50)

    db = get_db()
    try:
        # Verify user is participant
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        # Search messages containing the query text (case-insensitive via LIKE)
        search_pattern = f"%{q}%"
        rows = db.execute(
            """
            SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                   u.display_name AS sender_name, u.avatar_url AS sender_avatar
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            WHERE m.conversation_id = ? AND m.text LIKE ?
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (conv_id, search_pattern, limit),
        ).fetchall()

        total = db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND text LIKE ?",
            (conv_id, search_pattern),
        ).fetchone()[0]

        messages = []
        for row in rows:
            messages.append({
                "id": row["id"],
                "conversationId": conv_id,
                "senderId": row["sender_id"],
                "senderName": row["sender_name"],
                "senderAvatar": row["sender_avatar"],
                "text": row["text"],
                "imageUrl": row["image_url"],
                "time": _format_time(row["created_at"]),
                "createdAt": row["created_at"],
                "isOwn": row["sender_id"] == user_id,
            })

        # Chronological order
        messages.reverse()

        return jsonify({"messages": messages, "total": total})
    finally:
        db.close()


@chat_bp.route("/users/search", methods=["GET"])
@require_auth
def search_users():
    """Search users by name/username for starting new conversations."""
    user_id = request.user["id"]
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify({"users": []})

    db = get_db()
    try:
        rows = db.execute(
            """
            SELECT id, username, display_name, avatar_url, role
            FROM users
            WHERE id != ? AND (
                username LIKE ? OR display_name LIKE ?
            )
            LIMIT 20
            """,
            (user_id, f"%{q}%", f"%{q}%"),
        ).fetchall()

        return jsonify({
            "users": [
                {
                    "id": r["id"],
                    "username": r["username"],
                    "displayName": r["display_name"],
                    "avatarUrl": r["avatar_url"],
                    "role": r["role"],
                }
                for r in rows
            ]
        })
    finally:
        db.close()
