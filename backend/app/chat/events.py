"""Socket.IO event handlers for real-time chat."""

import logging
from datetime import datetime

from flask import request as flask_request
from flask_socketio import emit, join_room, leave_room

from app.auth.routes import get_current_user
from app.database import get_db

logger = logging.getLogger(__name__)

# Track connected users: sid -> user_id
_connected_users: dict[str, str] = {}


def _authenticate_socket():
    """Authenticate socket connection using token from auth query param."""
    token = flask_request.args.get("token", "")
    if not token:
        return None

    db = get_db()
    try:
        session = db.execute(
            "SELECT s.user_id, s.expires_at FROM sessions s WHERE s.token = ?",
            (token,),
        ).fetchone()
        if not session:
            return None

        expires = datetime.fromisoformat(session["expires_at"]).replace(tzinfo=None)
        if expires < datetime.utcnow():
            return None

        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        return dict(user) if user else None
    finally:
        db.close()


def register_socket_events(socketio):
    """Register all chat Socket.IO events."""

    @socketio.on("connect")
    def handle_connect():
        user = _authenticate_socket()
        if not user:
            logger.info("Socket connection rejected — invalid token")
            return False  # Reject connection

        sid = flask_request.sid
        user_id = user["id"]
        _connected_users[sid] = user_id

        # Auto-join all conversation rooms this user participates in
        db = get_db()
        try:
            rows = db.execute(
                "SELECT conversation_id FROM conversation_participants WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            for row in rows:
                join_room(f"conv:{row['conversation_id']}")
        finally:
            db.close()

        logger.info("Socket connected: user=%s sid=%s rooms=%d", user_id, sid, len(rows))
        emit("connected", {"userId": user_id})

    @socketio.on("disconnect")
    def handle_disconnect():
        sid = flask_request.sid
        user_id = _connected_users.pop(sid, None)
        if user_id:
            logger.info("Socket disconnected: user=%s", user_id)

    @socketio.on("join_conversation")
    def handle_join(data):
        """Join a specific conversation room (called after creating a new conversation)."""
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return

        conv_id = data.get("conversationId")
        if not conv_id:
            return

        # Verify user is a participant
        db = get_db()
        try:
            row = db.execute(
                "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
            if row:
                join_room(f"conv:{conv_id}")
        finally:
            db.close()

    @socketio.on("leave_conversation")
    def handle_leave(data):
        """Leave a conversation room."""
        conv_id = data.get("conversationId")
        if conv_id:
            leave_room(f"conv:{conv_id}")

    @socketio.on("typing")
    def handle_typing(data):
        """Broadcast typing indicator to conversation participants."""
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return

        conv_id = data.get("conversationId")
        if not conv_id:
            return

        # Get user display name
        db = get_db()
        try:
            user = db.execute("SELECT display_name FROM users WHERE id = ?", (user_id,)).fetchone()
            display_name = user["display_name"] if user else "Unknown"
        finally:
            db.close()

        emit(
            "user_typing",
            {
                "conversationId": conv_id,
                "userId": user_id,
                "displayName": display_name,
            },
            to=f"conv:{conv_id}",
            include_self=False,
        )

    @socketio.on("stop_typing")
    def handle_stop_typing(data):
        """Broadcast stop typing indicator."""
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return

        conv_id = data.get("conversationId")
        if conv_id:
            emit(
                "user_stop_typing",
                {"conversationId": conv_id, "userId": user_id},
                to=f"conv:{conv_id}",
                include_self=False,
            )

    @socketio.on("mark_read")
    def handle_mark_read(data):
        """Mark conversation as read via Socket.IO (alternative to REST endpoint).
        Inserts per-message read receipts and broadcasts to participants."""
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return

        conv_id = data.get("conversationId")
        if not conv_id:
            return

        now = datetime.utcnow().isoformat()
        db = get_db()
        try:
            # Verify user is participant
            participant = db.execute(
                "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
            if not participant:
                return

            # Update conversation-level last_read_at
            db.execute(
                "UPDATE conversation_participants SET last_read_at = ? WHERE conversation_id = ? AND user_id = ?",
                (now, conv_id, user_id),
            )

            # Insert per-message read receipts
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

            # Broadcast read receipt event
            if newly_read_ids:
                emit_read_receipt(socketio, conv_id, user_id, newly_read_ids)
        finally:
            db.close()


def emit_new_message(socketio, conv_id: str, message_data: dict):
    """Emit a new message to all participants in a conversation.
    Called from the REST send_message endpoint."""
    socketio.emit("new_message", message_data, to=f"conv:{conv_id}")


def emit_reaction_update(socketio, conv_id: str, msg_id: str, reactions: list):
    """Emit reaction update to conversation participants."""
    socketio.emit(
        "reaction_update",
        {"conversationId": conv_id, "messageId": msg_id, "reactions": reactions},
        to=f"conv:{conv_id}",
    )


def emit_read_receipt(socketio, conv_id: str, reader_id: str, message_ids: list):
    """Emit read receipt to conversation participants.
    Tells other clients that reader_id has read the given messages."""
    socketio.emit(
        "messages_read",
        {
            "conversationId": conv_id,
            "readerId": reader_id,
            "messageIds": message_ids,
        },
        to=f"conv:{conv_id}",
    )
