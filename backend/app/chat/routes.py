############################################################
#  [*] Chat — conversations, messages, reactions, presence
#
#  REST side of messaging, mounted at /api/chat by
#  create_app(). Live delivery (new_message, message_deleted,
#  reaction_update, messages_read, typing) lives in
#  chat/events.py: this module writes the rows, commits,
#  then hands the fan-out to the emit_* helpers there, so a
#  client without a socket still sees everything on its
#  next GET.
#
#  Contract facts the screens depend on:
#    - Every `time` field is HH:MM preformatted from the
#      naive UTC stamp, i.e. 2–3 h off in Lithuania. Clients
#      ignore it and format `createdAt` / `lastUpdatedMs`.
#    - Stamps are datetime.utcnow().isoformat(): naive, no
#      zone, microseconds. Paging cursors and unread counts
#      are plain string comparisons on them.
#    - Two independent read-state stores: the membership
#      row's last_read_at drives unreadCount and the tab
#      badge; per-message message_reads rows drive status
#      and readBy. send_message and mark_read write both.
#    - Presence (_connected_users in events.py) is a dict in
#      this process — right only for the single threading-
#      mode worker the stack runs.
#    - Every route needs a session token (require_auth);
#      membership checks vary per route — see each banner.
#
#    GET    /api/chat/conversations                            — list
#    POST   /api/chat/conversations                            — create / reuse direct
#    GET    /api/chat/conversations/<id>/messages              — history page
#    POST   /api/chat/conversations/<id>/messages              — send
#    DELETE /api/chat/conversations/<id>/messages/<mid>        — unsend own message
#    POST   /api/chat/conversations/<id>/messages/<mid>/react  — set own reaction
#    DELETE /api/chat/conversations/<id>/messages/<mid>/react  — clear own reaction
#    PUT    /api/chat/conversations/<id>/pin                   — toggle own pin
#    PUT    /api/chat/conversations/<id>/read                  — mark read
#    DELETE /api/chat/conversations/<id>                       — leave
#    GET    /api/chat/unread-count                             — tab badge total
#    GET    /api/chat/conversations/<id>/messages/search       — text search
#    POST   /api/chat/online-status                            — presence lookup
#    GET    /api/chat/users/search                             — people picker
############################################################


import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_socketio import join_room

from app.auth.routes import require_auth
from app.database import get_db

chat_bp = Blueprint("chat", __name__)








############################################################
# _get_socketio
############################################################
#
# The SocketIO instance is bound in app/__init__.py, which
# imports this module from inside create_app(); the lookup
# is deferred to call time as the guard against that
# package importing us while it is itself half-loaded. It
# binds socketio before the factory runs, so the cycle does
# not bite today — the guard simply costs nothing.
#
# Used by:
#   - _emit_reaction_update, send_message, delete_message,
#     mark_read (below)
############################################################

def _get_socketio():
    from app import socketio
    return socketio








############################################################
# _format_time
############################################################
#
# HH:MM of a naive UTC ISO stamp — UTC, NOT Lithuanian
# time, which is why clients ignore `time` and format
# createdAt themselves. None or an unparseable value gives
# "" rather than an exception, so one bad row never breaks
# a whole listing.
#
# Used by:
#   - list_conversations, get_messages, send_message,
#     search_messages (below) — every `time` field
############################################################

def _format_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return ""








############################################################
# _reply_payload
############################################################
#
# The quoted-message block a client renders inside a reply
# bubble, built from the LEFT JOIN columns get_messages and
# send_message select (prefix reply_*). A quoted message
# that was since unsent keeps its sender but loses its
# content — the client shows the placeholder there too. None
# when the message is not a reply.
#
# Used by:
#   - get_messages, send_message (below)
############################################################

def _reply_payload(row):
    if not row["reply_to_id"]:
        return None
    deleted = row["reply_deleted_at"] is not None
    return {
        "id": row["reply_to_id"],
        "senderId": row["reply_sender_id"],
        "senderName": row["reply_sender_name"],
        "text": "" if deleted else (row["reply_text"] or ""),
        "imageUrl": None if deleted else row["reply_image_url"],
        "deleted": deleted,
    }








############################################################
# _emit_reaction_update
############################################################
#
# Reads the current reactions of one message, broadcasts
# them as 'reaction_update' to room conv:<id> and returns
# the same list so the REST caller can echo it. Shape is
# [{emoji, count, byUserIds}] — NO bySelf here, unlike the
# get_messages shape; consumers derive it from byUserIds.
# current_user_id is accepted but never used (dead
# parameter). Neither the caller's membership nor the
# message's existence is checked here — that is the
# routes' job, and remove_reaction skips it.
#
# Used by:
#   - react_to_message, remove_reaction (below)
############################################################

def _emit_reaction_update(db, conv_id, msg_id, current_user_id):
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
    return reactions








############################################################
# list_conversations
############################################################
#
# GET /api/chat/conversations
#
# Every conversation the caller belongs to, pinned first
# then newest activity, with participants, the last
# message and an unread count per row — three extra
# queries per conversation (N+1; fine at faculty scale).
# unreadCount is other people's messages newer than the
# caller's last_read_at, compared as ISO strings; a NULL
# last_read_at counts everything. A direct chat without a
# title is named after the other participant, "Chat" when
# nobody else is (left) in it. lastUpdatedMs is updated_at
# → epoch ms via a NAIVE datetime, so it is only right
# while the process runs in UTC (the container does — no
# TZ is set anywhere). lastMessage.time is UTC HH:MM.
#
# Used by:
#   - services/api/chat.ts — fetchConversations
#     (app/(main)/tabs/messages.tsx — the Messages tab)
############################################################

@chat_bp.route("/conversations", methods=["GET"])
@require_auth
def list_conversations():
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

            participants = db.execute(
                """
                SELECT u.id, u.display_name, u.avatar_url
                FROM conversation_participants cp
                JOIN users u ON u.id = cp.user_id
                WHERE cp.conversation_id = ?
                """,
                (conv_id,),
            ).fetchall()

            last_msg = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id, m.deleted_at,
                       u.display_name AS sender_name
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                WHERE m.conversation_id = ?
                ORDER BY m.created_at DESC LIMIT 1
                """,
                (conv_id,),
            ).fetchone()

            # A NULL last_read_at (rows older than the column)
            # must count every message, hence the epoch floor;
            # the comparison is string-wise on ISO stamps
            last_read = row["last_read_at"] or "1970-01-01T00:00:00"
            unread = db.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE conversation_id = ? AND sender_id != ? AND created_at > ?
                """,
                (conv_id, user_id, last_read),
            ).fetchone()[0]

            # Direct chats carry no title of their own — the
            # "Chat" fallback only fires once the other side
            # has left (or the chat was created with self only)
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
                # naive datetime → .timestamp() assumes LOCAL
                # time; epoch ms only while the process is UTC
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
                    "deleted": last_msg["deleted_at"] is not None,
                }

            conversations.append(conv)

        return jsonify({"conversations": conversations})
    finally:
        db.close()








############################################################
# create_conversation
############################################################
#
# POST /api/chat/conversations
#
# Body {participantIds[], type?='direct', title?,
# avatarEmoji?}. The creator is always added and duplicate
# ids collapse via set(). A direct chat between exactly two
# people is deduplicated — the existing id comes back with
# 200 instead of a fresh 201 — but a 'direct' with three
# or more members is neither deduplicated nor rejected,
# and type is never validated against direct/group. Every
# id must exist in users (400 otherwise). Members start
# with last_read_at = now, so the new chat opens with
# unreadCount 0 for everyone.
#
# Sockets only auto-join conv:* rooms at connect time
# (events.py handle_connect) and the creator's client emits
# join_conversation when it opens the room, so the OTHER
# online members are joined here server-side — otherwise
# they would miss every live message until reconnect and
# get no push either (send_message skips online users).
#
# Used by:
#   - services/api/chat.ts — createConversation
#     (app/(main)/new-chat/index.tsx)
############################################################

@chat_bp.route("/conversations", methods=["POST"])
@require_auth
def create_conversation():
    # STEP 1: validate the body — participantIds must be a
    # non-empty list; type/title/avatarEmoji are taken as-is
    # ======================================================
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


    # STEP 2: the member set — the creator is always in, and
    # set() also collapses duplicate ids sent by the client
    # ======================================================
    all_ids = list(set([user_id] + participant_ids))


    # STEP 3: a direct chat between two people is reused — the
    # existing id answers with 200, not 201. A 'direct' with
    # three or more members skips this and is created like a
    # group. Own connection, closed before STEP 4 opens one
    # ========================================================
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


    # STEP 4: every id must be a real user — one IN query, so
    # a single unknown id fails the whole request
    # =======================================================
    db = get_db()
    try:
        placeholders = ",".join("?" * len(all_ids))
        found = db.execute(
            f"SELECT id FROM users WHERE id IN ({placeholders})", all_ids
        ).fetchall()
        if len(found) != len(all_ids):
            return jsonify({"error": "One or more participant IDs are invalid"}), 400


        # STEP 5: the conversation and its members in one
        # transaction; last_read_at = now so the chat opens
        # with unreadCount 0 for everybody
        # =================================================
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


        # STEP 6: pull the online members' sockets into the new
        # room. Sockets only auto-join conv:* rooms at connect
        # time (events.py handle_connect) and the creator's
        # client emits join_conversation itself when it opens
        # the room — without this the OTHER online members
        # would miss every live message until reconnect and get
        # no push either (send_message skips online users)
        # =====================================================
        from app.chat.events import _connected_users
        room = f"conv:{conv_id}"
        # list() snapshot: connects/disconnects on other threads
        # mutate the dict while this loop runs
        for sid, uid in list(_connected_users.items()):
            if uid in all_ids:
                join_room(room, sid=sid, namespace="/")

        return jsonify({"conversationId": conv_id}), 201
    finally:
        db.close()








############################################################
# get_messages
############################################################
#
# GET /api/chat/conversations/<id>/messages
#
# One history page, fetched newest-first and reversed to
# chronological order. ?limit (default 50, cap 100) and
# ?before=<createdAt> as the cursor: strictly older than
# that ISO string, so two messages sharing a stamp to the
# microsecond could be skipped across a page boundary.
# hasMore is len(rows) == limit — a false positive when
# exactly `limit` messages remain. A non-numeric limit
# raises → 500; a negative one reaches SQLite as LIMIT -n,
# which means "no limit". Members only (403).
#
# Each message carries reactions [{emoji, count, bySelf,
# byUserIds}], readBy (user ids holding a message_reads
# row), replyTo (_reply_payload, null when not a reply),
# deleted (unsent — text/imageUrl blanked) and, for the
# caller's OWN messages, status: "read" once every other
# member has a receipt, "delivered" when some have, else
# "sent"; others' messages are always "read". Reactions
# and receipts are batch-loaded with two IN (...) queries
# per page. The envelope also ships participants (sorted
# by display name) and the conversation row itself, so a
# room opened from a push notification can draw its header
# without a second call. `time` is UTC HH:MM — format
# createdAt instead.
#
# Used by:
#   - services/api/chat.ts — fetchMessages
#     (hooks/chat/useChatMessages.ts — initial load,
#     resync, and the older-page cursor)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages", methods=["GET"])
@require_auth
def get_messages(conv_id):
    # STEP 1: membership gate — outsiders get 403 before any
    # message row is read
    # ======================================================
    user_id = request.user["id"]
    db = get_db()
    try:
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403


        # STEP 2: the page — newest first, strictly older than
        # the ?before cursor when given. The cursor is the raw
        # createdAt string compared as text; int(limit) raises
        # on garbage (→ 500) and a negative value reaches
        # SQLite as LIMIT -n, meaning no limit at all
        # ====================================================
        before = request.args.get("before")
        limit = min(int(request.args.get("limit", 50)), 100)

        if before:
            rows = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       u.display_name AS sender_name, u.avatar_url AS sender_avatar,
                       m.reply_to_id, m.deleted_at,
                       r.sender_id AS reply_sender_id, r.text AS reply_text,
                       r.image_url AS reply_image_url, r.deleted_at AS reply_deleted_at,
                       ru.display_name AS reply_sender_name
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                LEFT JOIN messages r ON r.id = m.reply_to_id
                LEFT JOIN users ru ON ru.id = r.sender_id
                WHERE m.conversation_id = ? AND m.created_at < ?
                ORDER BY m.created_at DESC LIMIT ?
                """,
                (conv_id, before, limit),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       u.display_name AS sender_name, u.avatar_url AS sender_avatar,
                       m.reply_to_id, m.deleted_at,
                       r.sender_id AS reply_sender_id, r.text AS reply_text,
                       r.image_url AS reply_image_url, r.deleted_at AS reply_deleted_at,
                       ru.display_name AS reply_sender_name
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                LEFT JOIN messages r ON r.id = m.reply_to_id
                LEFT JOIN users ru ON ru.id = r.sender_id
                WHERE m.conversation_id = ?
                ORDER BY m.created_at DESC LIMIT ?
                """,
                (conv_id, limit),
            ).fetchall()


        # STEP 3: reactions and read receipts for the whole page
        # in two IN (...) queries instead of two per message
        # ======================================================
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


        # STEP 4: the members and the conversation row — the
        # room header and intro card draw portraits and the title
        # from these, and the member count feeds the own-message
        # status below
        # =======================================================
        member_rows = db.execute(
            """
            SELECT u.id, u.display_name, u.avatar_url
            FROM conversation_participants cp
            JOIN users u ON u.id = cp.user_id
            WHERE cp.conversation_id = ?
            ORDER BY u.display_name
            """,
            (conv_id,),
        ).fetchall()
        participants = [
            {"id": m["id"], "displayName": m["display_name"], "avatarUrl": m["avatar_url"]}
            for m in member_rows
        ]
        participant_count = len(member_rows)

        # The conversation itself — a room opened from a push
        # notification has no title or type in its route params
        conv_row = db.execute(
            "SELECT id, type, title, avatar_emoji FROM conversations WHERE id = ?",
            (conv_id,),
        ).fetchone()
        conversation = {
            "id": conv_row["id"],
            "type": conv_row["type"],
            "title": conv_row["title"],
            "avatarEmoji": conv_row["avatar_emoji"],
        } if conv_row else None


        # STEP 5: shape each message — reactions with bySelf,
        # readBy, and for the caller's own messages a status
        # derived from how many OTHER members hold a receipt
        # ===================================================
        messages = []
        for row in rows:
            msg_id = row["id"]

            msg_reactions = reaction_map_all.get(msg_id, {})
            reactions = []
            for emoji, uids in msg_reactions.items():
                reactions.append({
                    "emoji": emoji,
                    "count": len(uids),
                    "bySelf": user_id in uids,
                    "byUserIds": uids,
                })

            read_by = read_map_all.get(msg_id, [])
            is_own = row["sender_id"] == user_id
            # An unsent message keeps its slot but ships no content
            deleted = row["deleted_at"] is not None
            if is_own:
                # "read" needs a receipt from every other member,
                # "delivered" from at least one; a chat with no
                # other member (others_count 0) is trivially read
                other_readers = [uid for uid in read_by if uid != user_id]
                others_count = participant_count - 1  # the sender's own receipt never counts
                if others_count <= 0 or len(other_readers) >= others_count:
                    status = "read"
                elif len(other_readers) > 0:
                    status = "delivered"
                else:
                    status = "sent"
            else:
                # status only means something on own messages —
                # a fixed value for everybody else's
                status = "read"

            messages.append({
                "id": msg_id,
                "conversationId": conv_id,
                "senderId": row["sender_id"],
                "senderName": row["sender_name"],
                "senderAvatar": row["sender_avatar"],
                "text": "" if deleted else row["text"],
                "imageUrl": None if deleted else row["image_url"],
                "time": _format_time(row["created_at"]),
                "createdAt": row["created_at"],
                "isOwn": is_own,
                "status": status,
                "readBy": read_by,
                "reactions": reactions,
                "replyTo": _reply_payload(row),
                "deleted": deleted,
            })


        # STEP 6: DESC fetch → chronological list; hasMore is a
        # guess — a full page may also be the very last page
        # =====================================================
        messages.reverse()

        has_more = len(rows) == limit

        return jsonify({
            "messages": messages,
            "hasMore": has_more,
            "participants": participants,
            "conversation": conversation,
        })
    finally:
        db.close()








############################################################
# send_message
############################################################
#
# POST /api/chat/conversations/<id>/messages
#
# Body {text?, imageUrl?, replyToId?}: text is stripped,
# must be a string of at most 5000 chars, and at least one
# of text/imageUrl must be present. imageUrl is stored as
# given — the relative path uploads returns — with no
# validation. replyToId must name a message in THIS
# conversation (400 otherwise); its quote rides along as
# replyTo (see _reply_payload). Members only (403). One
# transaction inserts the message, bumps
# conversations.updated_at (list ordering), moves the
# sender's last_read_at forward and writes the sender's
# own read receipt, so the sender never counts their own
# message as unread.
#
# Fan-out after commit: 'new_message' to room conv:<id>
# (the sender's own socket included — clients dedupe by
# id), then a push per member who is NOT online anywhere.
# "Online" is the global _connected_users set, not room
# membership: a member connected but never joined to the
# room gets neither. Push goes through notify_channel_user
# so a user who disabled the "chat" channel is skipped;
# preview is the first 100 chars or "(image)"; any push
# failure is logged and the send still succeeds.
#
# Response {message} is the socket payload plus isOwn=true,
# status="sent", readBy=[self]; neither carries
# senderAvatar, unlike get_messages. `time` is UTC HH:MM —
# format createdAt instead.
#
# Used by:
#   - services/api/chat.ts — sendMessageApi
#     (hooks/chat/useChatComposer.ts)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages", methods=["POST"])
@require_auth
def send_message(conv_id):
    # STEP 1: validate the body — text is stripped, capped at
    # 5000 chars and must be a string; imageUrl is stored as
    # given; at least one of the two must be present
    # =======================================================
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

    reply_to_id = data.get("replyToId")
    if reply_to_id is not None and not isinstance(reply_to_id, str):
        return jsonify({"error": "replyToId must be a string"}), 400


    # STEP 2: membership gate — 403 for outsiders; a quoted
    # message must live in this very conversation (400)
    # =====================================================
    db = get_db()
    try:
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        reply_row = None
        if reply_to_id:
            reply_row = db.execute(
                """
                SELECT r.id AS reply_to_id, r.sender_id AS reply_sender_id, r.text AS reply_text,
                       r.image_url AS reply_image_url, r.deleted_at AS reply_deleted_at,
                       ru.display_name AS reply_sender_name
                FROM messages r
                JOIN users ru ON ru.id = r.sender_id
                WHERE r.id = ? AND r.conversation_id = ?
                """,
                (reply_to_id, conv_id),
            ).fetchone()
            if not reply_row:
                return jsonify({"error": "Quoted message not found in this conversation"}), 400


        # STEP 3: one transaction — the message row, the
        # conversation bump that reorders the list, and the
        # sender's read state in BOTH stores so their own
        # message never shows as unread to them
        # =================================================
        msg_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        db.execute(
            "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, reply_to_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, conv_id, user_id, text, image_url, reply_to_id or None, now),
        )

        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conv_id),
        )

        db.execute(
            "UPDATE conversation_participants SET last_read_at = ? WHERE conversation_id = ? AND user_id = ?",
            (now, conv_id, user_id),
        )

        db.execute(
            "INSERT OR IGNORE INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
            (msg_id, user_id, now),
        )

        db.commit()


        # STEP 4: live fan-out — 'new_message' to room conv:<id>,
        # the sender's own socket included (clients dedupe by
        # id). This payload has no senderAvatar/isOwn/status,
        # unlike the get_messages shape
        # =======================================================
        user = request.user
        msg_data = {
            "id": msg_id,
            "conversationId": conv_id,
            "senderId": user_id,
            "senderName": user["display_name"],
            "text": text,
            "imageUrl": image_url,
            # UTC HH:MM — clients format createdAt instead
            "time": _format_time(now),
            "createdAt": now,
            "reactions": [],
            "replyTo": _reply_payload(reply_row) if reply_row else None,
            "deleted": False,
        }

        from app.chat.events import emit_new_message
        emit_new_message(_get_socketio(), conv_id, msg_data)


        # STEP 5: push for members who are NOT online — "online"
        # means any socket in this process, not membership of
        # the room. notify_channel_user honours the user's
        # "chat" channel opt-out. A push failure is logged and
        # the send still succeeds: the row is already committed
        # ======================================================
        try:
            from app.chat.events import _connected_users
            from app.notifications.push import notify_channel_user

            online_user_ids = set(_connected_users.values())

            participants = db.execute(
                "SELECT user_id FROM conversation_participants WHERE conversation_id = ? AND user_id != ?",
                (conv_id, user_id),
            ).fetchall()

            sender_name = user["display_name"]
            preview = text[:100] if text else "(image)"
            for p in participants:
                pid = p["user_id"]
                if pid not in online_user_ids:
                    notify_channel_user(
                        "chat",
                        pid,
                        sender_name,
                        preview,
                        data={"type": "chat_message", "conversationId": conv_id},
                    )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Push notification failed for chat message")

        # The sender's own view of the message: only their own
        # receipt exists yet, hence status "sent"
        return jsonify({"message": {**msg_data, "isOwn": True, "status": "sent", "readBy": [user_id]}}), 201
    finally:
        db.close()








############################################################
# delete_message
############################################################
#
# DELETE /api/chat/conversations/<id>/messages/<mid>
#
# "Unsend": the sender soft-deletes their own message. The
# row survives — replies keep their target and cursors keep
# their order — but text and image are cleared, reactions
# dropped and deleted_at set, so every later read shows a
# placeholder. Unknown id in this conversation → 404,
# somebody else's message → 403, already unsent → still 200.
# Broadcasts 'message_deleted' {conversationId, messageId}
# to the room.
#
# Used by:
#   - services/api/chat.ts — deleteMessageApi
#     (hooks/chat/useChatMessages.ts)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages/<msg_id>", methods=["DELETE"])
@require_auth
def delete_message(conv_id, msg_id):
    user_id = request.user["id"]
    db = get_db()
    try:
        row = db.execute(
            "SELECT sender_id, deleted_at FROM messages WHERE id = ? AND conversation_id = ?",
            (msg_id, conv_id),
        ).fetchone()
        if not row:
            return jsonify({"error": "Message not found"}), 404
        if row["sender_id"] != user_id:
            return jsonify({"error": "Only the sender can delete a message"}), 403

        # Idempotent: a second delete changes nothing but still
        # re-broadcasts, which is harmless
        if row["deleted_at"] is None:
            now = datetime.utcnow().isoformat()
            db.execute(
                "UPDATE messages SET text = '', image_url = NULL, deleted_at = ? WHERE id = ?",
                (now, msg_id),
            )
            db.execute("DELETE FROM message_reactions WHERE message_id = ?", (msg_id,))
            db.commit()

        from app.chat.events import emit_message_deleted
        emit_message_deleted(_get_socketio(), conv_id, msg_id)
        return jsonify({"ok": True})
    finally:
        db.close()








############################################################
# react_to_message
############################################################
#
# POST /api/chat/conversations/<id>/messages/<mid>/react
#
# Sets the caller's reaction on a message — one emoji per
# user per message, so it is a delete-then-insert rather
# than a true upsert (both statements share one commit).
# Body {emoji}: non-empty string, max 32 chars (room for
# multi-codepoint emoji). Members only (403); the message
# must belong to that conversation (404). Returns {ok,
# emoji, reactions} where reactions is the authoritative
# post-write list ([{emoji, count, byUserIds}]) — the same
# list broadcast as 'reaction_update'. The mobile hook
# updates optimistically and reconciles from the socket
# event rather than from this body.
#
# Used by:
#   - services/api/chat.ts — reactToMessageApi
#     (hooks/chat/useChatReactions.ts)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages/<msg_id>/react", methods=["POST"])
@require_auth
def react_to_message(conv_id, msg_id):
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
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        # The conversation id in the URL is what the membership
        # check trusted, so the message must really live there
        msg = db.execute(
            "SELECT 1 FROM messages WHERE id = ? AND conversation_id = ?",
            (msg_id, conv_id),
        ).fetchone()
        if not msg:
            return jsonify({"error": "Message not found"}), 404

        # One emoji per user: replace, never accumulate
        db.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND user_id = ?",
            (msg_id, user_id),
        )
        db.execute(
            "INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
            (msg_id, user_id, emoji),
        )
        db.commit()

        reactions = _emit_reaction_update(db, conv_id, msg_id, user_id)

        return jsonify({"ok": True, "emoji": emoji, "reactions": reactions})
    finally:
        db.close()








############################################################
# remove_reaction
############################################################
#
# DELETE /api/chat/conversations/<id>/messages/<mid>/react
#
# Deletes the caller's own reaction (a no-op when there is
# none) and returns {ok, reactions} — the authoritative
# list after the delete, also broadcast as
# 'reaction_update'. No membership or message-existence
# check: the delete only ever touches the caller's own
# row, but the broadcast and the returned list are
# produced for ANY conv/message id, so a non-member can
# read who reacted to a message they cannot otherwise see.
#
# Used by:
#   - services/api/chat.ts — removeReactionApi
#     (hooks/chat/useChatReactions.ts)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages/<msg_id>/react", methods=["DELETE"])
@require_auth
def remove_reaction(conv_id, msg_id):
    user_id = request.user["id"]
    db = get_db()
    try:
        db.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND user_id = ?",
            (msg_id, user_id),
        )
        db.commit()

        reactions = _emit_reaction_update(db, conv_id, msg_id, user_id)

        return jsonify({"ok": True, "reactions": reactions})
    finally:
        db.close()








############################################################
# toggle_pin
############################################################
#
# PUT /api/chat/conversations/<id>/pin
#
# Flips the caller's own pinned flag on the membership row
# (pins are per user, not per conversation) and returns
# the new {pinned}. Members only (403). Pinned rows sort
# first in list_conversations.
#
# Used by:
#   - services/api/chat.ts — togglePinApi
#     (app/(main)/tabs/messages.tsx — swipe action or row
#     long-press)
############################################################

@chat_bp.route("/conversations/<conv_id>/pin", methods=["PUT"])
@require_auth
def toggle_pin(conv_id):
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








############################################################
# mark_read
############################################################
#
# PUT /api/chat/conversations/<id>/read
#
# Marks the whole conversation read for the caller: moves
# last_read_at to now (the unread-count store) and writes a
# message_reads receipt for every message from OTHER
# people that lacks one (the status/readBy store). Members
# only (403). When at least one receipt was new, the
# reader id and message ids are broadcast as
# 'messages_read' to the room so senders flip to
# delivered/read live; nothing is emitted otherwise.
# Returns {ok, readCount}. events.py handle_mark_read is
# the socket twin with the same logic copied — change
# both.
#
# Used by:
#   - services/api/chat.ts — markConversationRead
#     (hooks/chat/useChatMessages.ts — on open, on
#     incoming messages and on resync)
############################################################

@chat_bp.route("/conversations/<conv_id>/read", methods=["PUT"])
@require_auth
def mark_read(conv_id):
    # STEP 1: membership gate — one `now` for both stores so
    # last_read_at and the receipts agree to the microsecond
    # ======================================================
    user_id = request.user["id"]
    now = datetime.utcnow().isoformat()
    db = get_db()
    try:
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403


        # STEP 2: the conversation-level pointer — what
        # unreadCount and the tab badge read
        # =============================================
        db.execute(
            "UPDATE conversation_participants SET last_read_at = ? WHERE conversation_id = ? AND user_id = ?",
            (now, conv_id, user_id),
        )


        # STEP 3: a receipt per unread message from OTHER people
        # — what status/readBy read. Selected first so the ids
        # can be broadcast; INSERT OR IGNORE covers a racing
        # socket mark_read writing the same rows in between
        # ======================================================
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


        # STEP 4: 'messages_read' to the room so senders flip
        # to delivered/read live — skipped when nothing was new
        # =====================================================
        if newly_read_ids:
            from app.chat.events import emit_read_receipt
            emit_read_receipt(
                _get_socketio(), conv_id, user_id, newly_read_ids
            )

        return jsonify({"ok": True, "readCount": len(newly_read_ids)})
    finally:
        db.close()








############################################################
# leave_conversation
############################################################
#
# DELETE /api/chat/conversations/<id>
#
# Removes the caller's membership row; once nobody is left
# the messages, their reads and reactions and the
# conversation itself are purged in the same transaction.
# get_db() turns PRAGMA foreign_keys on and the schema
# cascades conversations → messages → reads/reactions, so
# the three child deletes are redundant with the last one
# (harmless). No membership check and no 404: an unknown
# id, or a conversation the caller never joined, still
# answers {ok: true} — the delete is simply a no-op. The
# remaining members keep the history, with the leaver's
# messages still attributed to them.
#
# Used by:
#   - services/api/chat.ts — deleteConversationApi
#     (app/(main)/tabs/messages.tsx — row swipe action)
############################################################

@chat_bp.route("/conversations/<conv_id>", methods=["DELETE"])
@require_auth
def leave_conversation(conv_id):
    user_id = request.user["id"]
    db = get_db()
    try:
        db.execute(
            "DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        )

        # Purge when the last member is gone — also reached for
        # an unknown id, where every delete below is a no-op
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








############################################################
# total_unread_count
############################################################
#
# GET /api/chat/unread-count
#
# {unreadCount}: other people's messages newer than the
# caller's last_read_at, summed over every conversation
# they belong to — one query. Same definition as the
# per-row unreadCount in list_conversations, so the tab
# badge and the row badges agree; it does NOT consult
# message_reads. The inner WHERE repeats the JOIN
# condition (harmless).
#
# Used by:
#   - services/api/chat.ts — fetchTotalUnreadCount
#     (hooks/useUnreadCount.ts — the Messages tab badge)
############################################################

@chat_bp.route("/unread-count", methods=["GET"])
@require_auth
def total_unread_count():
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








############################################################
# search_messages
############################################################
#
# GET /api/chat/conversations/<id>/messages/search
#
# ?q (required, 400 when blank after strip) and ?limit
# (default 20, cap 50, non-numeric → 500). Members only
# (403). Substring match via LIKE '%q%': case-insensitive
# for ASCII only (SQLite does not fold Lithuanian
# diacritics), and q's own % and _ act as wildcards since
# nothing escapes them. Returns {messages, total}: the
# newest `limit` hits reversed to chronological order,
# plus the UNCAPPED total so the UI can say "20 of 137".
# No reactions/status on hits; `time` is UTC HH:MM —
# format createdAt instead.
#
# Used by:
#   - services/api/chat.ts — searchMessagesApi
#     (app/(main)/chat-room/index.tsx — in-room search)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages/search", methods=["GET"])
@require_auth
def search_messages(conv_id):
    # STEP 1: parameters — a blank q is a 400 (unlike
    # search_users); limit defaults to 20, capped at 50, and
    # int() raises → 500 on garbage
    # ======================================================
    user_id = request.user["id"]
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify({"error": "q parameter is required and must not be empty"}), 400

    limit = min(int(request.args.get("limit", 20)), 50)


    # STEP 2: membership gate — 403 for outsiders
    # ===========================================
    db = get_db()
    try:
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403


        # STEP 3: the newest `limit` hits plus the UNCAPPED total.
        # LIKE folds case for ASCII only, and q's own % and _
        # are live wildcards — nothing escapes them
        # ========================================================
        search_pattern = f"%{q}%"
        rows = db.execute(
            """
            SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                   u.display_name AS sender_name, u.avatar_url AS sender_avatar
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            WHERE m.conversation_id = ? AND m.deleted_at IS NULL AND m.text LIKE ?
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (conv_id, search_pattern, limit),
        ).fetchall()

        total = db.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND deleted_at IS NULL AND text LIKE ?",
            (conv_id, search_pattern),
        ).fetchone()[0]


        # STEP 4: shape the hits — no reactions/status here, and
        # the DESC fetch is reversed to chronological order
        # ======================================================
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

        messages.reverse()

        return jsonify({"messages": messages, "total": total})
    finally:
        db.close()








############################################################
# online_status
############################################################
#
# POST /api/chat/online-status
#
# Body {userIds[]} → {online: {id: bool}}: whether each id
# currently has a Socket.IO connection in THIS process
# (events.py _connected_users). Silently truncated to the
# first 200 ids. No DB access — unknown ids simply come
# back false. The try/except around the presence lookup
# degrades to "everyone offline" if events.py cannot be
# imported.
#
# Used by:
#   - services/api/chat.ts — fetchOnlineStatus
#     (app/(main)/tabs/messages.tsx for the list dots,
#     app/(main)/chat-room/index.tsx for the counterpart's
#     presence indicator)
############################################################

@chat_bp.route("/online-status", methods=["POST"])
@require_auth
def online_status():
    data = request.get_json()
    if not data or not isinstance(data.get("userIds"), list):
        return jsonify({"error": "userIds array required"}), 400

    # Silent truncation rather than a 400 — the list screen
    # sends whatever it has on screen
    user_ids = data["userIds"]
    if len(user_ids) > 200:
        user_ids = user_ids[:200]

    # Presence is this process's socket table; an import
    # failure reads as everybody offline, never as an error
    try:
        from app.chat.events import _connected_users
        online_set = set(_connected_users.values())
    except Exception:
        online_set = set()

    result = {uid: uid in online_set for uid in user_ids}
    return jsonify({"online": result})








############################################################
# search_users
############################################################
#
# GET /api/chat/users/search
#
# ?q substring match on username OR display_name (LIKE:
# ASCII-only case folding, unescaped % and _ wildcards),
# excluding the caller, first 20 rows in table order. A
# blank q answers {users: []} with 200 rather than a 400,
# so the picker can call it on every keystroke. Returns
# id, username, displayName, avatarUrl, role — no email.
#
# Used by:
#   - services/api/chat.ts — searchUsersApi
#     (app/(main)/new-chat/index.tsx — the people picker)
############################################################

@chat_bp.route("/users/search", methods=["GET"])
@require_auth
def search_users():
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
