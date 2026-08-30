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
#    - Stamps are naive-UTC isoformat strings (now(timezone
#      .utc) with the offset dropped): no zone, microseconds.
#      Paging cursors and unread counts are plain string
#      comparisons on them.
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


import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request
from flask_socketio import join_room, leave_room

from app.auth.routes import get_json_object, rate_limit, require_auth
from app.database import get_db

chat_bp = Blueprint("chat", __name__)

logger = logging.getLogger(__name__)

# The fixed reaction picker set — must match the mobile
# REACTION_OPTIONS (hooks/chat/useChatReactions.ts) byte for
# byte; the heart carries its VS-16 variation selector
_ALLOWED_REACTIONS = frozenset(("\U0001F44D", "❤️", "\U0001F602", "\U0001F62E", "\U0001F622", "\U0001F621"))

# The most receipts one mark_read writes (and broadcasts) —
# a bound on the per-call work, not on correctness: ids past
# the cap stay unreceipted, which only softens the sender's
# status chip on ancient history
_MARK_READ_CAP = 500

# In-room search bounds: a longer needle than this is a 400
# (nobody types 200 chars into a search field, but a script
# would), and the hit counter stops at the cap instead of
# walking the whole conversation — a `total` of exactly
# _SEARCH_TOTAL_CAP means "that many or more"
_SEARCH_Q_MAX = 200
_SEARCH_TOTAL_CAP = 500








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
# _epoch_ms
############################################################
#
# Epoch milliseconds of a naive UTC ISO stamp — the stamp is
# pinned to timezone.utc before .timestamp(), so the number
# is right whatever /etc/localtime says in the process. Same
# fail-soft contract as _format_time: None, a legacy space-
# form row or any unparseable text gives 0 instead of an
# exception, so ONE bad conversations.updated_at can no
# longer 500 the whole Messages tab (the caller falls back
# to created_at, and a 0 only sorts the row last).
#
# Used by:
#   - list_conversations (below) — lastUpdatedMs
############################################################

def _epoch_ms(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return 0








############################################################
# _escape_like
############################################################
#
# Escapes \, % and _ in user-typed search text so LIKE
# matches them literally — every LIKE built from the result
# must carry ESCAPE '\'. Case folding stays SQLite's own:
# ASCII only, Lithuanian diacritics unfolded (an ICU /
# lower() rework deliberately left out of this pass).
#
# Used by:
#   - search_messages, search_users (below)
############################################################

def _escape_like(q):
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")








############################################################
# _is_local_upload_url
############################################################
#
# Whether an image_url points at THIS server's /api/uploads/
# tree: the relative path the upload route returns, or its
# absolute same-origin form (http(s)://<request.host>/api/
# uploads/...). ONE rule for both ends of a photo's life —
# send_message refuses every other value and delete_message
# hands exactly this set to the uploads cleanup helper — so a
# form the send accepts can no longer be a blob the unsend
# then orphans. request.host makes it a request-context-only
# helper. A non-string answers False instead of raising, so
# the caller can type-check separately and this never has to
# guess what a list "starts with".
#
# Used by:
#   - send_message (STEP 1.1) — the accept gate
#   - delete_message (below) — the blob cleanup gate
############################################################

def _is_local_upload_url(url):
    if not isinstance(url, str):
        return False

    if url.startswith("/api/uploads/"):
        return True

    # Absolute only counts when scheme, host AND path all
    # agree: "//localhost/api/uploads/a.png" carries no scheme
    # and "localhost:9999" is a different origin
    parsed = urlparse(url)
    return (parsed.scheme in ("http", "https")
            and parsed.netloc == request.host
            and parsed.path.startswith("/api/uploads/"))








############################################################
# _reply_payload
############################################################
#
# The quoted-message block a client renders inside a reply
# bubble, built from the LEFT JOIN columns get_messages and
# send_message select (prefix reply_*). A quoted message
# that was since unsent keeps its sender but loses its
# content — the client shows the placeholder there too. A
# DANGLING reply_to_id (the quoted row vanished under FK-off
# writes, so the LEFT JOIN missed) is shaped as deleted with
# blank content, never as a live quote with a null sender.
# None when the message is not a reply.
#
# Used by:
#   - get_messages, send_message, _find_committed_send
#     (below)
############################################################

def _reply_payload(row):
    if not row["reply_to_id"]:
        return None

    # The join found nothing — treat the ghost quote exactly
    # like an unsent one instead of deleted:false + null sender
    if row["reply_sender_id"] is None:
        return {
            "id": row["reply_to_id"],
            "senderId": None,
            "senderName": None,
            "text": "",
            "imageUrl": None,
            "deleted": True,
        }

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
# _find_committed_send
############################################################
#
# The idempotent-replay lookup: the caller's already
# committed message carrying this client_msg_id in this
# conversation, shaped exactly like send_message's response
# message (reply quote included, senderAvatar off the
# session user, status "sent", readBy [sender]) — or None
# when no such row exists. A retry
# after a client timeout, or the loser of a racing
# double-submit, answers with this row and a 200 instead
# of inserting a duplicate. The unique index behind it is
# migration v10 (database/__init__.py).
#
# Used by:
#   - send_message (below) — the pre-insert check and the
#     unique-index race catch
############################################################

def _find_committed_send(db, conv_id, user_id, sender_name, sender_avatar, client_msg_id):
    row = db.execute(
        """
        SELECT m.id, m.text, m.image_url, m.created_at, m.client_msg_id,
               m.reply_to_id, m.deleted_at,
               r.sender_id AS reply_sender_id, r.text AS reply_text,
               r.image_url AS reply_image_url, r.deleted_at AS reply_deleted_at,
               ru.display_name AS reply_sender_name
        FROM messages m
        LEFT JOIN messages r ON r.id = m.reply_to_id
        LEFT JOIN users ru ON ru.id = r.sender_id
        WHERE m.conversation_id = ? AND m.sender_id = ? AND m.client_msg_id = ?
        """,
        (conv_id, user_id, client_msg_id),
    ).fetchone()
    if not row:
        return None

    # The replayed row may since have been unsent — mirror the
    # get_messages blanking so the client never sees stale content
    deleted = row["deleted_at"] is not None
    return {
        "id": row["id"],
        "conversationId": conv_id,
        "senderId": user_id,
        "senderName": sender_name,
        "senderAvatar": sender_avatar,
        "text": "" if deleted else row["text"],
        "imageUrl": None if deleted else row["image_url"],
        "time": _format_time(row["created_at"]),
        "createdAt": row["created_at"],
        "clientMsgId": row["client_msg_id"],
        "reactions": [],
        "replyTo": _reply_payload(row),
        "deleted": deleted,
        "isOwn": True,
        "status": "sent",
        "readBy": [user_id],
    }








############################################################
# _reactions_for
############################################################
#
# The ONE reaction shaper both transports read from: the
# reactions of a whole id list as {message_id: [{emoji,
# count, byUserIds}]}, one IN (...) query for the batch.
# current_user_id is what separates the two wire shapes —
# pass it (get_messages, where a caller identity exists)
# and every group also carries bySelf; leave it None for
# the react/unreact answers and the reaction_update
# broadcast, which are read by many clients and therefore
# carry NO bySelf (mobile ApiReactionGroup derives it from
# byUserIds). No users join: the display names were never
# put on the wire. The single-message callers run it INSIDE
# their write transaction, before db.commit(), so the
# snapshot they broadcast is exactly the state their own
# write produced — a concurrent reaction cannot slip
# between commit and read. Neither membership nor the
# messages' existence is checked here: that is the routes'
# job, and every caller does it.
#
# Used by:
#   - get_messages (batch, with bySelf), react_to_message,
#     remove_reaction (one id, no bySelf) — below
############################################################

def _reactions_for(db, msg_ids, current_user_id=None):
    if not msg_ids:
        return {}

    placeholders = ",".join("?" * len(msg_ids))
    rows = db.execute(
        f"""
        SELECT mr.message_id, mr.emoji, mr.user_id
        FROM message_reactions mr
        WHERE mr.message_id IN ({placeholders})
        """,
        list(msg_ids),
    ).fetchall()

    # message id → emoji → the user ids holding it, insertion
    # ordered so the shaped groups keep the row order
    grouped = {}
    for r in rows:
        mid = r["message_id"]
        if mid not in grouped:
            grouped[mid] = {}
        emoji = r["emoji"]
        if emoji not in grouped[mid]:
            grouped[mid][emoji] = []
        grouped[mid][emoji].append(r["user_id"])

    shaped = {}
    for mid, by_emoji in grouped.items():
        groups = []
        for emoji, uids in by_emoji.items():
            group = {"emoji": emoji, "count": len(uids)}
            # bySelf only where the caller is one identity —
            # a broadcast has no "self" to speak of
            if current_user_id is not None:
                group["bySelf"] = current_user_id in uids
            group["byUserIds"] = uids
            groups.append(group)
        shaped[mid] = groups

    return shaped








############################################################
# _push_chat_message
############################################################
#
# The chat push fan-out, run OFF the request thread via
# socketio.start_background_task — send_message answers 201
# without waiting on Expo's HTTP round-trip. Prefers the
# batched notify_channel_users the notifications package is
# contracted to grow (one query, one Expo batch per
# language); until that lands, the standalone fallback does
# the same shape itself: ONE query joining push_tokens
# against the "chat" opt-outs for the whole recipient set,
# then ONE send_push_batch call. No request context in
# here — everything arrives as arguments, and every failure
# is logged and swallowed (push never owes anybody an
# error).
#
# Used by:
#   - send_message (below) — STEP 5, after the commit
############################################################

def _push_chat_message(recipient_ids, title, body, data):
    try:
        # STEP 1: the batched helper, once notifications ships
        # it — the fallback below retires the day it lands
        # ====================================================
        try:
            from app.notifications.push import notify_channel_users
        except ImportError:
            notify_channel_users = None

        if notify_channel_users is not None:
            notify_channel_users("chat", recipient_ids, title, body, data=data)
            return


        # STEP 2: standalone fallback — every active token of
        # the recipients minus explicit chat opt-outs (a
        # missing notification_channels row means enabled)
        # ===================================================
        from app.notifications.push import send_push_batch

        db = get_db()
        try:
            placeholders = ",".join("?" * len(recipient_ids))
            rows = db.execute(
                f"""
                SELECT pt.token FROM push_tokens pt
                WHERE pt.active = 1 AND pt.user_id IN ({placeholders})
                AND NOT EXISTS (
                    SELECT 1 FROM notification_channels nc
                    WHERE nc.user_id = pt.user_id AND nc.channel = 'chat' AND nc.enabled = 0
                )
                """,
                recipient_ids,
            ).fetchall()
        finally:
            db.close()

        tokens = [r["token"] for r in rows]
        if not tokens:
            return

        # Copy first — the caller's dict must not grow a
        # "channel" (same rule as notifications/push.py)
        push_data = dict(data or {})
        push_data["channel"] = "chat"
        send_push_batch(tokens, title, body, push_data)
    except Exception:
        logger.exception("Chat push fan-out failed")








############################################################
# _find_direct_conversation
############################################################
#
# The id of the existing two-person direct chat between
# these users, or None. It drives from the CALLER's own
# membership rows (idx_conversation_participants_user), so
# the lookup touches the handful of conversations they are
# in instead of scanning the whole conversations table on
# every direct create. The COUNT(*) = 2 arm is what keeps a
# planted multi-member 'direct' row from being reused as
# these two people's DM (the same guard migration v49
# applied to historic rows).
#
# Used by:
#   - create_conversation (below) — the pre-lock fast path
#     and the re-check inside BEGIN IMMEDIATE
############################################################

def _find_direct_conversation(db, user_id, other_id):
    row = db.execute(
        """
        SELECT cp.conversation_id
        FROM conversation_participants cp
        JOIN conversations c ON c.id = cp.conversation_id AND c.type = 'direct'
        WHERE cp.user_id = ?
          AND EXISTS (
              SELECT 1 FROM conversation_participants o
              WHERE o.conversation_id = cp.conversation_id AND o.user_id = ?
          )
          AND (
              SELECT COUNT(*) FROM conversation_participants p
              WHERE p.conversation_id = cp.conversation_id
          ) = 2
        LIMIT 1
        """,
        (user_id, other_id),
    ).fetchone()

    return row["conversation_id"] if row else None








############################################################
# list_conversations
############################################################
#
# GET /api/chat/conversations
#
# Every conversation the caller belongs to, pinned first
# then newest activity, with participants, the last
# message and an unread count per row — FOUR queries for
# the whole tab (the memberships, then participants, last
# messages and unread counts set-based over the id list),
# where this used to run three extra queries per row.
# unreadCount is other people's messages newer than the
# caller's last_read_at, compared as ISO strings; a NULL
# last_read_at counts everything, and unsent (soft-
# deleted) messages never count. A direct chat without a
# title is named after the other participant; when nobody
# else is (left) in it the title stays null and the
# client renders its localized fallback
# (messages.conversationFallback). lastUpdatedMs is
# _epoch_ms of updated_at — epoch ms regardless of the
# process TZ, and 0 rather than a 500 on an unparseable
# stamp. lastMessage.time is UTC HH:MM.
#
# Used by:
#   - services/api/chat.ts — fetchConversations
#     (app/(main)/tabs/messages.tsx — the Messages tab)
############################################################

@chat_bp.route("/conversations", methods=["GET"])
@require_auth
def list_conversations():
    # STEP 1: the caller's memberships — pinned first, then
    # newest activity; the id list drives everything below
    # =====================================================
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

        conv_ids = [row["id"] for row in rows]


        # STEP 2: participants, last message and unread count
        # for the WHOLE tab in three set-based queries keyed
        # off that id list — this is where the old three-per-
        # conversation N+1 lived
        # ===================================================
        participants_map = {}
        last_msg_map = {}
        unread_map = {}
        if conv_ids:
            placeholders = ",".join("?" * len(conv_ids))

            for p in db.execute(
                f"""
                SELECT cp.conversation_id, u.id, u.display_name, u.avatar_url
                FROM conversation_participants cp
                JOIN users u ON u.id = cp.user_id
                WHERE cp.conversation_id IN ({placeholders})
                """,
                conv_ids,
            ).fetchall():
                cid = p["conversation_id"]
                if cid not in participants_map:
                    participants_map[cid] = []
                participants_map[cid].append(p)

            # ROW_NUMBER picks every room's newest row in one
            # pass; the id tiebreak keeps the pick deterministic
            # when two stamps match to the microsecond
            for m in db.execute(
                f"""
                SELECT conversation_id, id, text, image_url, created_at,
                       sender_id, deleted_at, sender_name
                FROM (
                    SELECT m.conversation_id, m.id, m.text, m.image_url, m.created_at,
                           m.sender_id, m.deleted_at, u.display_name AS sender_name,
                           ROW_NUMBER() OVER (
                               PARTITION BY m.conversation_id
                               ORDER BY m.created_at DESC, m.id DESC
                           ) AS rn
                    FROM messages m
                    JOIN users u ON u.id = m.sender_id
                    WHERE m.conversation_id IN ({placeholders})
                )
                WHERE rn = 1
                """,
                conv_ids,
            ).fetchall():
                last_msg_map[m["conversation_id"]] = m

            # One GROUP BY, the same definition total_unread_count
            # uses: a NULL last_read_at (rows older than the
            # column) must count every message, hence the epoch
            # floor; the comparison is string-wise on ISO stamps,
            # and unsent messages are out — the badge must agree
            # with what the reader can actually still read
            for cnt in db.execute(
                f"""
                SELECT m.conversation_id, COUNT(*) AS unread
                FROM messages m
                JOIN conversation_participants cp
                  ON cp.conversation_id = m.conversation_id AND cp.user_id = ?
                WHERE m.conversation_id IN ({placeholders})
                  AND m.sender_id != ?
                  AND m.deleted_at IS NULL
                  AND m.created_at > COALESCE(cp.last_read_at, '1970-01-01T00:00:00')
                GROUP BY m.conversation_id
                """,
                [user_id] + conv_ids + [user_id],
            ).fetchall():
                unread_map[cnt["conversation_id"]] = cnt["unread"]


        # STEP 3: shape each row from the three maps — no
        # further query runs from here on
        # ================================================
        conversations = []
        for row in rows:
            conv_id = row["id"]
            participants = participants_map.get(conv_id, [])
            last_msg = last_msg_map.get(conv_id)
            unread = unread_map.get(conv_id, 0)

            # Direct chats carry no title of their own — named
            # after the other participant; once the other side
            # has left (or the chat was created with self only)
            # the title stays null and the client renders its
            # localized messages.conversationFallback
            title = row["title"]
            if row["type"] == "direct" and not title:
                other = [p for p in participants if p["id"] != user_id]
                title = other[0]["display_name"] if other else None

            conv = {
                "id": conv_id,
                "type": row["type"],
                "title": title,
                "avatarEmoji": row["avatar_emoji"],
                "pinned": bool(row["pinned"]),
                "unreadCount": unread,
                # _epoch_ms pins the naive UTC stamp to
                # timezone.utc before .timestamp() and answers 0
                # instead of raising — an unparseable updated_at
                # falls back to created_at, and a row with
                # neither only sorts last in the client's list
                # rather than 500ing the whole tab
                "lastUpdatedMs": _epoch_ms(row["updated_at"]) or _epoch_ms(row["created_at"]),
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
# avatarEmoji?}. participantIds must be 1–50 non-empty
# strings, type one of direct/group (400 otherwise, so the
# CHECK constraint can never 500), title a string ≤100 and
# avatarEmoji a string ≤16. A group REQUIRES a non-blank
# title (a null group title crashes the mobile Messages tab
# search); a direct chat stores NULL for both title and
# avatarEmoji regardless of the body, so an attacker-chosen
# title can never impersonate the counterpart — the list
# names direct chats after the other member. The creator is
# always added and duplicate ids collapse via set(); a
# member set that reduces to the caller ALONE is a 400
# (a self-chat is not a feature, and nothing stopped a
# client from creating them without limit), and a direct
# chat must resolve to exactly two members (400).
# The two-member dedup answers 200 with the existing id
# instead of a fresh 201, and only matches rooms whose
# participant count is really 2 — a planted multi-member
# 'direct' row can no longer swallow two people's DM
# (migration v49 demoted any such rows to groups). It runs
# TWICE on one connection: once before the write lock (the
# cheap fast path) and once inside BEGIN IMMEDIATE, so two
# racing creates can no longer both miss and both insert.
# Every id must exist in users AND still be active (400
# otherwise — a deactivated account cannot be dragged into
# a new room), and no member may be in a block pair with
# the CREATOR (either direction) — that answers a flat 403
# without naming who blocked whom, so consent can no longer
# be skipped by simply inserting someone into a room. Capped at 50 creates per 5 min per user
# (429). Members start with last_read_at = now, so the new
# chat opens with unreadCount 0 for everyone.
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
@rate_limit("chat_create", max_attempts=50)
def create_conversation():
    # STEP 1: the body — participantIds must be 1–50
    # non-empty strings, type direct|group, title and
    # avatarEmoji bounded strings; a group must bring a
    # non-blank title, a direct chat stores NULL for both
    # ===================================================
    user_id = request.user["id"]
    data = get_json_object()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    participant_ids = data.get("participantIds", [])
    if not isinstance(participant_ids, list) or not participant_ids:
        return jsonify({"error": "participantIds must be a non-empty array"}), 400
    if len(participant_ids) > 50:
        return jsonify({"error": "participantIds must contain at most 50 ids"}), 400
    if not all(isinstance(pid, str) and pid for pid in participant_ids):
        return jsonify({"error": "participantIds must contain non-empty strings"}), 400

    conv_type = data.get("type", "direct")
    if conv_type not in ("direct", "group"):
        return jsonify({"error": "type must be one of: direct, group"}), 400

    title = data.get("title")
    if title is not None and not isinstance(title, str):
        return jsonify({"error": "title must be a string"}), 400
    if title is not None and len(title) > 100:
        return jsonify({"error": "title must be at most 100 characters"}), 400

    avatar_emoji = data.get("avatarEmoji")
    if avatar_emoji is not None and not isinstance(avatar_emoji, str):
        return jsonify({"error": "avatarEmoji must be a string"}), 400
    if avatar_emoji is not None and len(avatar_emoji) > 16:
        return jsonify({"error": "avatarEmoji must be at most 16 characters"}), 400

    if conv_type == "group" and (not title or not title.strip()):
        return jsonify({"error": "Group conversations require a title"}), 400

    # A direct chat is always named after the counterpart —
    # a body-supplied title/emoji would override that name in
    # every list row (impersonation), so neither is stored
    if conv_type == "direct":
        title = None
        avatar_emoji = None


    # STEP 2: the member set — the creator is always in, and
    # set() also collapses duplicate ids sent by the client;
    # a set of just the caller is no conversation at all, and
    # a direct chat must resolve to exactly two people
    # ======================================================
    all_ids = list(set([user_id] + participant_ids))

    if len(all_ids) < 2:
        return jsonify({"error": "A conversation needs at least one other participant"}), 400

    if conv_type == "direct" and len(all_ids) != 2:
        return jsonify({"error": "Direct conversations must have exactly 2 participants"}), 400


    # STEP 3: every id must be a real, still-active user — one
    # IN query, so a single unknown or deactivated id fails
    # the whole request. From here on ONE connection carries
    # the dedup, the write lock and the inserts
    # ========================================================
    db = get_db()
    try:
        placeholders = ",".join("?" * len(all_ids))
        found = db.execute(
            f"SELECT id FROM users WHERE id IN ({placeholders}) AND active = 1", all_ids
        ).fetchall()
        if len(found) != len(all_ids):
            return jsonify({"error": "One or more participant IDs are invalid"}), 400


        # STEP 3.1: no member may be in a block pair with the
        # creator, either direction — one flat 403 that does not
        # say who blocked whom (the block's existence is not the
        # creator's business, only its effect is)
        # =====================================================
        other_ids = [uid for uid in all_ids if uid != user_id]
        other_ph = ",".join("?" * len(other_ids))
        blocked_pair = db.execute(
            f"""SELECT 1 FROM user_blocks
                WHERE (blocker_id = ? AND blocked_id IN ({other_ph}))
                   OR (blocked_id = ? AND blocker_id IN ({other_ph}))
                LIMIT 1""",
            [user_id, *other_ids, user_id, *other_ids],
        ).fetchone()
        if blocked_pair:
            return jsonify({"error": "One or more participants cannot be added"}), 403


        # STEP 4: a direct chat between two people is reused —
        # the existing id answers with 200, not 201. This is
        # the pre-lock fast path; the same lookup runs again
        # under BEGIN IMMEDIATE below, so a racing twin cannot
        # slip between a miss here and the insert there
        # ====================================================
        other_id = None
        if conv_type == "direct":
            other_id = [uid for uid in all_ids if uid != user_id][0]
            existing = _find_direct_conversation(db, user_id, other_id)
            if existing:
                return jsonify({"conversationId": existing}), 200


        # STEP 5: the conversation and its members in ONE write
        # transaction — BEGIN IMMEDIATE takes the write lock
        # first, then the direct dedup runs once more under it:
        # the loser of a double-submit now finds its twin's row
        # and answers 200 instead of inserting a second DM.
        # last_read_at = now so the chat opens with unreadCount
        # 0 for everybody
        # =====================================================
        db.execute("BEGIN IMMEDIATE")

        if conv_type == "direct":
            existing = _find_direct_conversation(db, user_id, other_id)
            if existing:
                db.rollback()
                return jsonify({"conversationId": existing}), 200

        conv_id = str(uuid.uuid4())
        # Naive-UTC isoformat — utcnow() is deprecated on 3.13,
        # so the aware now has its offset dropped to keep the
        # string format every cursor comparison relies on
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

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
                try:
                    join_room(room, sid=sid, namespace="/")
                except (KeyError, ValueError):
                    # That socket disconnected between the
                    # snapshot and this call — presence plumbing
                    # must never fail an already-committed create
                    logger.debug("join_room skipped for departed sid %s", sid)

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
# ?before=<createdAt> plus ?before_id=<id> as a composite
# cursor: strictly older than that (stamp, id) pair under
# the page order created_at DESC, id DESC, so two messages
# sharing a stamp to the microsecond cannot be skipped
# across a page boundary. Old clients sending only ?before
# keep the previous strictly-older-stamp behavior.
# hasMore is exact: the page fetches limit+1 rows, ships
# the first `limit` of them and reports the probe row, so a
# last page that happens to be exactly full no longer
# promises an older page that does not exist. A non-numeric
# limit is a 400 and the value is clamped into 1..100, so a
# negative can no longer reach SQLite as LIMIT -n ("no
# limit"). Members only (403).
#
# Each message carries reactions [{emoji, count, bySelf,
# byUserIds}], readBy (user ids holding a message_reads
# row), replyTo (_reply_payload, null when not a reply),
# clientMsgId (the sender's idempotency nonce — null on
# rows older than migration v10 or sent without one),
# deleted (unsent — text/imageUrl blanked) and, for the
# caller's OWN messages, status: "read" once every other
# member has a receipt, "delivered" when some have, else
# "sent"; others' messages are always "read". Reactions
# (_reactions_for, bySelf included here) and receipts are
# batch-loaded with two IN (...) queries per page. The envelope also ships participants (sorted
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
        # the (?before, ?before_id) composite cursor when
        # given. The stamp is the raw createdAt string compared
        # as text, the id breaks stamp ties so equal-stamp
        # siblings never fall through a page boundary; without
        # before_id the id arm compares against NULL (never
        # true) and the filter degrades to the old bare-stamp
        # cut. Garbage in ?limit is a 400, and the clamp keeps
        # a negative from reaching SQLite as LIMIT -n ("no
        # limit"). SQLite is asked for limit+1 rows: the extra
        # one never leaves the server, it only answers hasMore
        # ====================================================
        before = request.args.get("before")
        before_id = request.args.get("before_id")
        try:
            limit = int(request.args.get("limit", 50))
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be an integer"}), 400
        limit = max(1, min(limit, 100))

        if before:
            rows = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       m.client_msg_id,
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
                  AND (m.created_at < ? OR (m.created_at = ? AND m.id < ?))
                ORDER BY m.created_at DESC, m.id DESC LIMIT ?
                """,
                (conv_id, before, before, before_id, limit + 1),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       m.client_msg_id,
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
                ORDER BY m.created_at DESC, m.id DESC LIMIT ?
                """,
                (conv_id, limit + 1),
            ).fetchall()

        # The probe row answers hasMore exactly and is then
        # dropped — the page itself is never longer than limit
        has_more = len(rows) > limit
        rows = rows[:limit]


        # STEP 3: reactions and read receipts for the whole page
        # in two IN (...) queries instead of two per message
        # ======================================================
        msg_ids = [row["id"] for row in rows]
        reaction_map_all = _reactions_for(db, msg_ids, user_id)
        read_map_all = {}
        if msg_ids:
            placeholders = ",".join("?" * len(msg_ids))

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

            # already shaped with bySelf by _reactions_for
            reactions = reaction_map_all.get(msg_id, [])

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
                "clientMsgId": row["client_msg_id"],
                "isOwn": is_own,
                "status": status,
                "readBy": read_by,
                "reactions": reactions,
                "replyTo": _reply_payload(row),
                "deleted": deleted,
            })


        # STEP 6: DESC fetch → chronological list; hasMore was
        # settled by the probe row back in STEP 2
        # =====================================================
        messages.reverse()

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
# Body {text?, imageUrl?, replyToId?, client_msg_id?}:
# text is stripped, must be a string of at most 5000
# chars, and at least one of text/imageUrl must be
# present. imageUrl must be a string — EVERY non-string is
# a 400, the falsy [] / {} / 0 / false included, which is
# what keeps a bind parameter sqlite3 cannot take out of
# the INSERT — and, when non-empty, the relative
# /api/uploads/... path uploads returns (or its same-origin
# absolute form): any other value is a 400, so a stored
# message can never point a reader's client at a foreign
# server. The empty string is the one falsy value that
# passes, stored and echoed as given.
# replyToId must name a message in THIS conversation (400
# otherwise, blank string included — "not a reply" is
# spelled by omitting the field, so there is ONE code path
# for it); its quote rides along as replyTo (see
# _reply_payload). client_msg_id is the client's
# optimistic-send nonce: a repeat of one already committed
# (a retry after a timeout) answers 200 with the EXISTING
# row instead of inserting a duplicate — the v10 unique
# index (database/__init__.py) closes the race window.
# Members only (403). One transaction inserts the message,
# bumps conversations.updated_at (list ordering), moves
# the sender's last_read_at forward and writes the
# sender's own read receipt, so the sender never counts
# their own message as unread.
#
# Fan-out after commit: 'new_message' to room conv:<id>
# (the sender's own socket included — clients match it to
# their optimistic bubble by clientMsgId, or dedupe by
# id), then push for every member WITHOUT a socket in that
# very room — room membership, not global presence, so a
# web tab parked on another screen no longer silences the
# phone. The Expo HTTP round-trip runs in a
# start_background_task thread (_push_chat_message), so the
# request returns 201 without waiting on exp.host; the task
# honours the "chat" channel opt-out. The push title is the
# sender's display name in a direct chat and
# "Sender · Group title" in a group, so a lock-screen
# banner says WHERE the message landed. Preview is the
# first 100 chars or, for a photo-only message,
# "Nuotrauka" (LT is the app default, matching the in-app
# list preview) plus data.preview='photo' so a client
# handling the notification in-app can re-localize; any
# push failure is logged and the send still succeeds.
# Capped at 150 sends per 5 min per user (429).
#
# Response {message} is the socket payload plus isOwn=true,
# status="sent", readBy=[self]; both carry senderAvatar,
# so an optimistic bubble and the get_messages shape agree.
# `time` is UTC HH:MM — format createdAt instead.
#
# Used by:
#   - services/api/chat.ts — sendMessageApi
#     (hooks/chat/useChatComposer.ts)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages", methods=["POST"])
@require_auth
@rate_limit("chat_send", max_attempts=150)
def send_message(conv_id):
    # STEP 1: validate the body — text is stripped, capped at
    # 5000 chars and must be a string; at least one of
    # text/imageUrl must be present
    # =======================================================
    user_id = request.user["id"]
    data = get_json_object()
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
    # A blank quote id is a client bug, not "no reply" — say
    # so instead of silently sending an unquoted message
    if reply_to_id is not None and not reply_to_id.strip():
        return jsonify({"error": "replyToId must not be blank"}), 400

    client_msg_id = data.get("client_msg_id")
    if client_msg_id is not None and not isinstance(client_msg_id, str):
        return jsonify({"error": "client_msg_id must be a string"}), 400
    if client_msg_id and len(client_msg_id) > 128:
        return jsonify({"error": "client_msg_id too long"}), 400


    # STEP 1.1: imageUrl must be a string and, when it names
    # anything at all, the /api/uploads/... path the upload
    # route returns — relative, or absolute on this very
    # origin. Anything else (a foreign host above all) is
    # rejected, so a stored message can never make a reader's
    # client call out to a server an attacker chose. The TYPE
    # check sits OUTSIDE the truthiness gate on purpose: a
    # falsy non-string ([], {}, 0, false) used to skip
    # validation whole and reach sqlite3 as a bind parameter,
    # an uncaught ProgrammingError any member could fire at
    # will. An empty string stays the one falsy value that
    # passes — it is stored and echoed as given
    # =======================================================
    if image_url is not None and not isinstance(image_url, str):
        return jsonify({"error": "imageUrl must be a string"}), 400

    if image_url and not _is_local_upload_url(image_url):
        return jsonify({"error": "imageUrl must be an /api/uploads/ path"}), 400


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


        # STEP 2.0: a DIRECT chat between a blocked pair (either
        # direction) refuses the send — create_conversation stops
        # NEW rooms, this stops the room that already existed
        # when the block was placed. Group sends stay: membership
        # is the group's decision, and the push fan-out below
        # keeps a blocked pair's phones quiet there
        # ======================================================
        conv_type_row = db.execute(
            "SELECT type FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if conv_type_row and conv_type_row["type"] == "direct":
            counterpart = db.execute(
                "SELECT user_id FROM conversation_participants "
                "WHERE conversation_id = ? AND user_id != ? LIMIT 1",
                (conv_id, user_id),
            ).fetchone()
            if counterpart:
                pair_blocked = db.execute(
                    "SELECT 1 FROM user_blocks WHERE (blocker_id = ? AND blocked_id = ?) "
                    "OR (blocker_id = ? AND blocked_id = ?)",
                    (user_id, counterpart["user_id"], counterpart["user_id"], user_id),
                ).fetchone()
                if pair_blocked:
                    return jsonify({"error": "You cannot message this user"}), 403

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


        # STEP 2.1: idempotent replay — a nonce already
        # committed (the retry of a send that timed out on the
        # client) answers 200 with the existing row; no second
        # insert, no second fan-out
        # ====================================================
        if client_msg_id:
            committed = _find_committed_send(
                db, conv_id, user_id, request.user["display_name"],
                request.user["avatar_url"], client_msg_id,
            )
            if committed:
                return jsonify({"message": committed}), 200


        # STEP 3: one transaction — the message row, the
        # conversation bump that reorders the list, and the
        # sender's read state in BOTH stores so their own
        # message never shows as unread to them. The v10
        # unique index turns a racing double-submit into an
        # IntegrityError, answered like the replay above
        # =================================================
        msg_id = str(uuid.uuid4())
        # Naive-UTC isoformat, same shape utcnow() produced —
        # cursors and unread counts compare these as strings
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        try:
            db.execute(
                "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, reply_to_id, client_msg_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (msg_id, conv_id, user_id, text, image_url, reply_to_id or None, client_msg_id or None, now),
            )
        except sqlite3.IntegrityError:
            # Only the (conversation_id, sender_id,
            # client_msg_id) index can fire here — membership
            # and the quoted row were just verified
            db.rollback()
            committed = None
            if client_msg_id:
                committed = _find_committed_send(
                    db, conv_id, user_id, request.user["display_name"],
                    request.user["avatar_url"], client_msg_id,
                )
            if committed:
                return jsonify({"message": committed}), 200
            raise

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
        # id). senderAvatar rides along (straight off the
        # session user, no extra lookup) so this payload and the
        # 201 body draw the same portrait get_messages does; only
        # isOwn/status/readBy stay off the socket shape
        # =======================================================
        user = request.user
        msg_data = {
            "id": msg_id,
            "conversationId": conv_id,
            "senderId": user_id,
            "senderName": user["display_name"],
            "senderAvatar": user["avatar_url"],
            "text": text,
            "imageUrl": image_url,
            # UTC HH:MM — clients format createdAt instead
            "time": _format_time(now),
            "createdAt": now,
            # the sender's own nonce rides along so its clients
            # match the echo to the optimistic bubble by it
            "clientMsgId": client_msg_id or None,
            "reactions": [],
            "replyTo": _reply_payload(reply_row) if reply_row else None,
            "deleted": False,
        }

        from app.chat.events import emit_new_message
        emit_new_message(_get_socketio(), conv_id, msg_data)


        # STEP 5: push for members WITHOUT a socket in this very
        # room — the room the emit above just reached, read from
        # socketio's room manager. A member online elsewhere (a
        # web tab on another screen, a second device) still gets
        # the push. The Expo round-trip itself runs in a
        # background task so the client never waits on exp.host;
        # a failure here is logged and the send still succeeds:
        # the row is already committed
        # ======================================================
        try:
            from app.chat.events import _connected_users

            sio = _get_socketio()
            try:
                room_sids = set(sio.server.manager.rooms.get("/", {}).get(f"conv:{conv_id}") or ())
            except Exception:
                room_sids = set()
            # list() snapshot: connects/disconnects on other
            # threads mutate the dict while this runs
            in_room_ids = {uid for sid, uid in list(_connected_users.items()) if sid in room_sids}

            participants = db.execute(
                "SELECT user_id FROM conversation_participants WHERE conversation_id = ? AND user_id != ?",
                (conv_id, user_id),
            ).fetchall()
            recipients = [p["user_id"] for p in participants if p["user_id"] not in in_room_ids]

            # Blocks silence the push lane too: in a group the
            # message stays visible in the room, but a phone in a
            # block pair with the sender (either direction) stays
            # quiet — no buzz from someone you cut off
            if recipients:
                rec_ph = ",".join("?" * len(recipients))
                block_rows = db.execute(
                    f"""SELECT blocker_id, blocked_id FROM user_blocks
                        WHERE (blocker_id = ? AND blocked_id IN ({rec_ph}))
                           OR (blocked_id = ? AND blocker_id IN ({rec_ph}))""",
                    [user_id, *recipients, user_id, *recipients],
                ).fetchall()
                silenced = {
                    r["blocked_id"] if r["blocker_id"] == user_id else r["blocker_id"]
                    for r in block_rows
                }
                recipients = [r for r in recipients if r not in silenced]

            if recipients:
                # The title says WHO wrote and, in a group, WHERE
                # — a bare display name on a lock screen leaves
                # the reader guessing which room it came from.
                # Direct chats keep the plain name: the room IS
                # the sender there
                conv_row = db.execute(
                    "SELECT type, title FROM conversations WHERE id = ?",
                    (conv_id,),
                ).fetchone()
                push_title = user["display_name"]
                if conv_row and conv_row["type"] == "group" and conv_row["title"]:
                    push_title = f"{push_title} · {conv_row['title']}"

                push_data = {"type": "chat_message", "conversationId": conv_id}
                if text:
                    preview = text[:100]
                else:
                    # A push with an empty body renders nothing on a
                    # lock screen, so a photo-only message ships the
                    # Lithuanian word (LT is the app default and the
                    # in-app list preview says the same); the marker
                    # in data lets a foreground client re-localize.
                    # Per-recipient language waits on LENS-I18N-004
                    preview = "Nuotrauka"
                    push_data["preview"] = "photo"
                # Preview privacy: a recipient who turned
                # chat_push_preview off gets the content-free
                # body — their message text never leaves for
                # Expo at all (the data payload still carries
                # the conversationId, so the tap-through works)
                users_ph = ",".join("?" * len(recipients))
                no_preview = {
                    r["id"]
                    for r in db.execute(
                        f"SELECT id FROM users WHERE id IN ({users_ph}) AND chat_push_preview = 0",
                        recipients,
                    ).fetchall()
                }
                full_recipients = [r for r in recipients if r not in no_preview]
                quiet_recipients = [r for r in recipients if r in no_preview]
                if full_recipients:
                    sio.start_background_task(
                        _push_chat_message, full_recipients, push_title, preview, push_data
                    )
                if quiet_recipients:
                    # LT for the same reason the photo marker is
                    # (app default; data marker re-localizes)
                    sio.start_background_task(
                        _push_chat_message, quiet_recipients, push_title,
                        "Nauja žinutė", {**push_data, "preview": "hidden"},
                    )
        except Exception:
            logger.exception("Push notification failed for chat message")

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
# placeholder. An unsent photo's /api/uploads/ file is
# handed to the uploads package's delete helper (best
# effort, once that package ships it) so the blob does not
# outlive the message — in BOTH the forms send_message
# accepts (_is_local_upload_url), relative and absolute
# same-origin, so neither can leave an orphan behind. Outsider → 403 before any message
# row is read (the same gate the sibling routes carry),
# unknown id in this conversation → 404, somebody else's
# message → 403, already unsent → still 200. Capped at 100
# unsends per 5 min per user (429). Broadcasts
# 'message_deleted' {conversationId, messageId} to the room
# ONLY on the call that actually unsent it — a repeat is a
# silent 200, so a retry cannot re-animate the event for
# every client in the room.
#
# Used by:
#   - services/api/chat.ts — deleteMessageApi
#     (hooks/chat/useChatMessages.ts)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages/<msg_id>", methods=["DELETE"])
@require_auth
@rate_limit("chat_delete", max_attempts=100)
def delete_message(conv_id, msg_id):
    # STEP 1: membership gate — an outsider learns nothing
    # about the room's messages, not even whether one exists
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


        # STEP 2: the row, its owner, then the soft delete —
        # idempotent, and the broadcast lives INSIDE the guard
        # so a repeat call stays silent on the wire
        # ===================================================
        row = db.execute(
            "SELECT sender_id, deleted_at, image_url FROM messages WHERE id = ? AND conversation_id = ?",
            (msg_id, conv_id),
        ).fetchone()
        if not row:
            return jsonify({"error": "Message not found"}), 404
        if row["sender_id"] != user_id:
            return jsonify({"error": "Only the sender can delete a message"}), 403

        if row["deleted_at"] is None:
            now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            db.execute(
                "UPDATE messages SET text = '', image_url = NULL, deleted_at = ? WHERE id = ?",
                (now, msg_id),
            )
            db.execute("DELETE FROM message_reactions WHERE message_id = ?", (msg_id,))
            db.commit()

            # The photo blob goes with the message — through the
            # uploads package's shared delete helper, which does
            # not exist yet; until it lands the ImportError makes
            # this a silent no-op (today's behavior). The url is
            # matched by _is_local_upload_url, the SAME rule
            # send_message accepted it under: a bare prefix test
            # missed the absolute same-origin form that route
            # also takes, and every photo sent that way outlived
            # its message on disk. The helper reads the last path
            # segment, so it takes either form verbatim
            image_url = row["image_url"]
            if _is_local_upload_url(image_url):
                try:
                    from app.uploads.routes import delete_upload
                    delete_upload(image_url)
                except ImportError:
                    pass
                except Exception:
                    logger.exception("Upload cleanup failed for unsent message")

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
# Body {emoji}: one of the six _ALLOWED_REACTIONS the
# mobile picker offers — anything else is a 400, so an
# arbitrary 32-char string can no longer ride in as a
# "reaction". Members only (403); the message must belong
# to that conversation AND still be un-unsent (404 — a chip
# must not resurrect on a placeholder bubble). Returns {ok,
# emoji, reactions} where reactions is the authoritative
# post-write list ([{emoji, count, byUserIds}]), read
# inside the write transaction so the broadcast snapshot
# matches this very write — the same list broadcast as
# 'reaction_update' after the commit. The mobile hook
# updates optimistically and reconciles from this body on
# success; the socket event carries the same list to the
# other clients. Capped at 300 reaction writes per 5 min
# per user (429), shared with remove_reaction.
#
# Used by:
#   - services/api/chat.ts — reactToMessageApi
#     (hooks/chat/useChatReactions.ts)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages/<msg_id>/react", methods=["POST"])
@require_auth
@rate_limit("chat_react", max_attempts=300)
def react_to_message(conv_id, msg_id):
    user_id = request.user["id"]
    data = get_json_object()
    if not data or not data.get("emoji"):
        return jsonify({"error": "emoji required"}), 400

    if not isinstance(data["emoji"], str):
        return jsonify({"error": "emoji must be a string"}), 400

    # The server-side allowlist mirrors the mobile picker's
    # REACTION_OPTIONS — the only six values a client can send
    emoji = data["emoji"]
    if emoji not in _ALLOWED_REACTIONS:
        return jsonify({"error": "emoji must be one of the supported reactions"}), 400
    db = get_db()
    try:
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        # The conversation id in the URL is what the membership
        # check trusted, so the message must really live there —
        # and still be un-unsent: a reaction must never
        # resurrect a chip on a placeholder bubble
        msg = db.execute(
            "SELECT 1 FROM messages WHERE id = ? AND conversation_id = ? AND deleted_at IS NULL",
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

        # Snapshot INSIDE the transaction, broadcast after the
        # commit — concurrent reactions can no longer slip a
        # newer state into an older broadcast. No current user
        # passed: this list goes to many clients, so it carries
        # no bySelf (they derive it from byUserIds)
        reactions = _reactions_for(db, [msg_id]).get(msg_id, [])
        db.commit()

        from app.chat.events import emit_reaction_update
        emit_reaction_update(_get_socketio(), conv_id, msg_id, reactions)

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
# list after the delete, read inside the write transaction
# (same snapshot rule as react_to_message) and broadcast as
# 'reaction_update' after the commit. Members only (403)
# and the message must live in this very conversation (404)
# — the same gates as react_to_message, so the returned
# list and the broadcast can no longer leak who reacted to
# a message the caller cannot see. Shares react_to_message's
# 300-per-5-min reaction budget (429).
#
# Used by:
#   - services/api/chat.ts — removeReactionApi
#     (hooks/chat/useChatReactions.ts)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages/<msg_id>/react", methods=["DELETE"])
@require_auth
@rate_limit("chat_react", max_attempts=300)
def remove_reaction(conv_id, msg_id):
    user_id = request.user["id"]
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

        db.execute(
            "DELETE FROM message_reactions WHERE message_id = ? AND user_id = ?",
            (msg_id, user_id),
        )

        # Snapshot INSIDE the transaction, broadcast after the
        # commit — same ordering rule (and same no-bySelf
        # shape) as react_to_message
        reactions = _reactions_for(db, [msg_id]).get(msg_id, [])
        db.commit()

        from app.chat.events import emit_reaction_update
        emit_reaction_update(_get_socketio(), conv_id, msg_id, reactions)

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
# the new {pinned}. The flip is ONE atomic UPDATE (pinned =
# 1 - pinned), so two racing toggles land as two flips
# instead of one losing its read-modify-write; rowcount 0
# doubles as the membership gate (403). Pinned rows sort
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
        # One atomic statement — no read-modify-write for a
        # concurrent toggle to race; 0 rows means non-member
        cur = db.execute(
            "UPDATE conversation_participants SET pinned = 1 - pinned WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Not a participant"}), 403

        # Re-read inside the same transaction for the response
        row = db.execute(
            "SELECT pinned FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        db.commit()
        return jsonify({"pinned": bool(row["pinned"])})
    finally:
        db.close()








############################################################
# _watermark_regresses
############################################################
#
# Whether writing `now` over `prior` would move a reader's
# last_read_at BACKWARDS in time. The two stamps are PARSED
# rather than compared as text — the rest of the module
# compares these strings raw, which is right for a cursor
# but would let a legacy space-form row (or any non-stamp)
# sort above every real stamp and freeze the pointer for
# good. A missing/blank prior is no watermark at all and a
# prior that parses as nothing is not comparable: both
# answer False, so the fresh stamp simply lands.
#
# Used by:
#   - _apply_mark_read (below) — STEP 3, the pointer write
############################################################

def _watermark_regresses(prior, now):
    if not prior:
        return False

    try:
        return datetime.fromisoformat(prior) > datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return False








############################################################
# _apply_mark_read
############################################################
#
# The ONE mark-read implementation both transports share —
# this REST route below and events.py handle_mark_read
# import it instead of carrying the logic twice. Inside a
# single BEGIN IMMEDIATE transaction (call it on a fresh
# get_db() connection with nothing pending): the membership
# lookup doubles as the gate AND yields the caller's
# previous last_read_at, so a departed member can write
# nothing (no TOCTOU window); the receipt SELECT is bounded
# to created_at in (prior, now] — unbounded below only when
# prior is NULL — capped at _MARK_READ_CAP newest rows; the
# receipts land as ONE set-based INSERT OR IGNORE (a racing
# twin's rows are not an error, their ids are still
# re-broadcast); then last_read_at ADVANCES to `now` — a
# `now` older than the stamp already on the row is dropped
# (_watermark_regresses), so two calls committing out of
# order can no longer un-read what the later one cleared —
# and the `<= now` bound is what keeps the two read stores
# agreeing about a message that arrives mid-call. Returns the
# pre-selected id list for the frozen messages_read
# {messageIds} broadcast — None means "not a participant"
# (the REST edge answers 403, the socket edge drops
# silently).
#
# Used by:
#   - mark_read (below) — the REST transport
#   - chat/events.py — handle_mark_read, the socket twin
############################################################

def _apply_mark_read(db, conv_id, user_id, now):
    # STEP 1: BEGIN IMMEDIATE — gate and writes in ONE write
    # transaction; the membership row also carries the prior
    # watermark that bounds the receipt scan
    # ======================================================
    db.execute("BEGIN IMMEDIATE")
    row = db.execute(
        "SELECT last_read_at FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user_id),
    ).fetchone()
    if not row:
        db.rollback()
        return None

    prior = row["last_read_at"]


    # STEP 2: the receipt candidates — foreign messages inside
    # (prior, now] without a receipt, newest first, capped so
    # one call can never scan a whole ancient history. `<= now`
    # keeps a message landing mid-call out of BOTH stores
    # ========================================================
    if prior:
        unread_msgs = db.execute(
            """
            SELECT m.id FROM messages m
            WHERE m.conversation_id = ? AND m.sender_id != ?
              AND m.created_at > ? AND m.created_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM message_reads mr
                  WHERE mr.message_id = m.id AND mr.user_id = ?
              )
            ORDER BY m.created_at DESC LIMIT ?
            """,
            (conv_id, user_id, prior, now, user_id, _MARK_READ_CAP),
        ).fetchall()
    else:
        unread_msgs = db.execute(
            """
            SELECT m.id FROM messages m
            WHERE m.conversation_id = ? AND m.sender_id != ?
              AND m.created_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM message_reads mr
                  WHERE mr.message_id = m.id AND mr.user_id = ?
              )
            ORDER BY m.created_at DESC LIMIT ?
            """,
            (conv_id, user_id, now, user_id, _MARK_READ_CAP),
        ).fetchall()

    newly_read_ids = [m["id"] for m in unread_msgs]


    # STEP 3: both stores in the same transaction — one
    # set-based insert for the receipts, then the pointer
    # that unreadCount and the tab badge read
    # ===================================================
    if newly_read_ids:
        placeholders = ",".join("?" * len(newly_read_ids))
        db.execute(
            f"""
            INSERT OR IGNORE INTO message_reads (message_id, user_id, read_at)
            SELECT m.id, ?, ? FROM messages m WHERE m.id IN ({placeholders})
            """,
            [user_id, now] + newly_read_ids,
        )

    # The pointer ADVANCES, it is never merely set: every call
    # takes its own `now` BEFORE the write lock, so the one
    # that started earlier can commit last, and a bare SET let
    # that drag the watermark back over messages this reader
    # had already cleared — they reappeared on the tab badge
    # and on every row badge. A backwards clock step did the
    # same. `prior` was read under this very BEGIN IMMEDIATE,
    # so nobody can move the row between the test and the write
    if not _watermark_regresses(prior, now):
        db.execute(
            "UPDATE conversation_participants SET last_read_at = ? WHERE conversation_id = ? AND user_id = ?",
            (now, conv_id, user_id),
        )

    db.commit()

    return newly_read_ids








############################################################
# mark_read
############################################################
#
# PUT /api/chat/conversations/<id>/read
#
# Marks the whole conversation read for the caller: moves
# last_read_at FORWARD to now (the unread-count store — a
# call that arrives out of order never drags it back) and
# writes a
# message_reads receipt for every foreign message inside
# the watermark window (the status/readBy store) — all via
# _apply_mark_read (above), the one implementation shared
# with the socket twin (events.py handle_mark_read), so
# the two transports can no longer drift apart. Members
# only (403). Shares the socket limiter's mark_read budget
# (_socket_rate_check, 10 per 10 s per user) and answers
# 429 on excess, so the REST path is no longer the free
# bypass around the socket quota. When at least one
# receipt was new, the reader id and message ids are
# broadcast as 'messages_read' so senders flip to
# delivered/read live; nothing is emitted otherwise.
# Returns {ok, readCount}.
#
# Used by:
#   - services/api/chat.ts — markConversationRead
#     (hooks/chat/useChatMessages.ts — on open, on
#     incoming messages and on resync)
############################################################

@chat_bp.route("/conversations/<conv_id>/read", methods=["PUT"])
@require_auth
def mark_read(conv_id):
    # STEP 1: ONE quota across both transports — the socket
    # limiter's own key, so socket + REST spend one budget
    # ====================================================
    user_id = request.user["id"]
    from app.chat.events import _socket_rate_check
    if _socket_rate_check(user_id, "mark_read"):
        return jsonify({"error": "Too many requests. Please slow down.", "code": "rate_limited"}), 429


    # STEP 2: one `now` for both stores, then the shared
    # helper — None back means the caller is no member
    # ==================================================
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    db = get_db()
    try:
        newly_read_ids = _apply_mark_read(db, conv_id, user_id, now)
        if newly_read_ids is None:
            return jsonify({"error": "Not a participant"}), 403


        # STEP 3: 'messages_read' to the readers/senders so
        # bubbles flip to delivered/read live — skipped when
        # nothing was new
        # ==================================================
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
# Removes the caller's membership row — 404 for an unknown
# conversation, 403 for one the caller never joined (the
# mobile swipe handler restores the row on either error,
# which is the desired outcome). The leaver's own
# message_reads and message_reactions rows for the room go
# in the SAME transaction, so the remaining members' read/
# status math no longer counts a ghost reader; once nobody
# is left the messages, their reads and reactions and the
# conversation itself are purged too. get_db() turns PRAGMA
# foreign_keys on and the schema cascades conversations →
# messages → reads/reactions, so the purge's child deletes
# are redundant with the last one (harmless). After the
# commit every socket of the leaver is evicted from room
# conv:<id> (best effort), so an ex-member stops receiving
# the room's live traffic without waiting for a reconnect.
# The remaining members keep the history, with the leaver's
# messages still attributed to them.
#
# Used by:
#   - services/api/chat.ts — deleteConversationApi
#     (app/(main)/tabs/messages.tsx — row swipe action)
############################################################

@chat_bp.route("/conversations/<conv_id>", methods=["DELETE"])
@require_auth
def leave_conversation(conv_id):
    # STEP 1: the gates the sibling routes already have — an
    # unknown room is a 404, an outsider a 403, and only a
    # real member reaches the delete below
    # =====================================================
    user_id = request.user["id"]
    db = get_db()
    try:
        conv = db.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if not conv:
            return jsonify({"error": "Conversation not found"}), 404

        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403


        # STEP 2: one transaction — the membership row AND the
        # leaver's receipts/reactions in this room, so the
        # remaining members' read/status math never counts a
        # ghost reader who is no longer a participant
        # ====================================================
        db.execute(
            "DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        )
        db.execute(
            "DELETE FROM message_reads WHERE user_id = ? AND message_id IN (SELECT id FROM messages WHERE conversation_id = ?)",
            (user_id, conv_id),
        )
        db.execute(
            "DELETE FROM message_reactions WHERE user_id = ? AND message_id IN (SELECT id FROM messages WHERE conversation_id = ?)",
            (user_id, conv_id),
        )

        # Purge when the last member is gone
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


        # STEP 3: evict every socket of the leaver from the room
        # — otherwise an ex-member keeps receiving live messages
        # until their next reconnect. Best effort: presence
        # plumbing must never fail the committed delete
        # ======================================================
        try:
            from app.chat.events import _connected_users
            room = f"conv:{conv_id}"
            # list() snapshot: connects/disconnects on other
            # threads mutate the dict while this loop runs
            for sid, uid in list(_connected_users.items()):
                if uid == user_id:
                    leave_room(room, sid=sid, namespace="/")
        except Exception:
            logger.exception("Socket eviction failed after leave_conversation")

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
# caller's last_read_at, unsent (soft-deleted) ones
# excluded, counted over every conversation they belong to
# — one flat COUNT(*) join, no GROUP-BY-inside-SUM temp
# B-tree. Same definition as the per-row unreadCount in
# list_conversations, so the tab badge and the row badges
# agree; it does NOT consult message_reads.
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
            SELECT COUNT(*)
            FROM messages m
            JOIN conversation_participants cp
              ON cp.conversation_id = m.conversation_id AND cp.user_id = ?
            WHERE m.sender_id != ?
              AND m.deleted_at IS NULL
              AND m.created_at > COALESCE(cp.last_read_at, '1970-01-01T00:00:00')
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
# ?q (required, 400 when blank after strip and 400 over
# 200 chars — nobody types a novel into a search field, a
# script would) and ?limit (default 20, clamped into 1..50,
# non-numeric → 400, parsed only after the membership
# gate). Members only (403). The match runs on the
# messages_fts FTS5 shadow table (migration v20) as a
# quoted prefix phrase —
# token-prefix semantics, proper word folding, no full
# scan — joined back to messages for the deleted_at
# filter; when FTS5 is missing from the build (or the
# query defeats the tokenizer) the old LIKE '%q%'
# substring path answers instead, ASCII-only case folding
# and all, with q's \, % and _ escaped (_escape_like +
# ESCAPE '\') so they match literally. A q carrying a NUL
# byte is answered {messages: [], total: 0} without a query
# at all: sqlite3 binds TEXT NUL-terminated, so the pattern
# would arrive truncated and match far more than was asked
# for. Returns {messages,
# total}: the newest `limit` hits reversed to
# chronological order, plus the total so the UI can say
# "20 of 137". The counter SATURATES at _SEARCH_TOTAL_CAP
# (it counts a capped subquery, not the whole room), so
# that value means "this many or more" and one search can
# never walk an entire conversation. Capped at 100
# searches per 5 min per user (429).
# No reactions/status on hits; `time` is UTC HH:MM —
# format createdAt instead.
#
# Used by:
#   - services/api/chat.ts — searchMessagesApi
#     (app/(main)/chat-room/index.tsx — in-room search)
############################################################

@chat_bp.route("/conversations/<conv_id>/messages/search", methods=["GET"])
@require_auth
@rate_limit("chat_msg_search", max_attempts=100)
def search_messages(conv_id):
    # STEP 1: q — a blank q is a 400 (unlike search_users),
    # and so is one no human would type: an unbounded needle
    # is an unbounded LIKE/MATCH per call
    # ======================================================
    user_id = request.user["id"]
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify({"error": "q parameter is required and must not be empty"}), 400
    if len(q) > _SEARCH_Q_MAX:
        return jsonify({"error": f"q must be at most {_SEARCH_Q_MAX} characters"}), 400


    # STEP 2: membership gate — 403 for outsiders, BEFORE any
    # other parameter is even parsed
    # =======================================================
    db = get_db()
    try:
        participant = db.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (conv_id, user_id),
        ).fetchone()
        if not participant:
            return jsonify({"error": "Not a participant"}), 403

        try:
            limit = int(request.args.get("limit", 20))
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be an integer"}), 400
        limit = max(1, min(limit, 50))

        # python-sqlite3 binds TEXT NUL-TERMINATED, so a NUL in
        # the needle truncates the pattern INSIDE SQLite: the
        # FTS phrase loses its closing quote (OperationalError,
        # straight into the fallback) and the LIKE pattern loses
        # its trailing '%', collapsing to a bare '%' that
        # answers the whole room. No message body holds one, so
        # the needle is answered as what it is — a miss
        if "\x00" in q:
            return jsonify({"messages": [], "total": 0})


        # STEP 3: the newest `limit` hits plus the UNCAPPED
        # total — FTS5 first (migration v50): q rides as one
        # quoted prefix phrase ("..."*, inner quotes doubled),
        # joined back to messages for deleted_at. A build
        # without FTS5, a pre-v50 file or a query the
        # tokenizer rejects raises OperationalError and falls
        # back to the old escaped-LIKE substring scan
        # ==================================================
        rows = None
        total = 0
        fts_query = '"' + q.replace('"', '""') + '"*'
        try:
            rows = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       u.display_name AS sender_name, u.avatar_url AS sender_avatar
                FROM messages_fts
                JOIN messages m ON m.rowid = messages_fts.rowid
                JOIN users u ON u.id = m.sender_id
                WHERE messages_fts MATCH ? AND m.conversation_id = ? AND m.deleted_at IS NULL
                ORDER BY m.created_at DESC
                LIMIT ?
                """,
                (fts_query, conv_id, limit),
            ).fetchall()

            # Counted over a capped subquery: the label needs
            # "many", not the exact number of hits in a decade
            # of history
            total = db.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1
                    FROM messages_fts
                    JOIN messages m ON m.rowid = messages_fts.rowid
                    WHERE messages_fts MATCH ? AND m.conversation_id = ? AND m.deleted_at IS NULL
                    LIMIT ?
                )
                """,
                (fts_query, conv_id, _SEARCH_TOTAL_CAP),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            rows = None

        if rows is None:
            search_pattern = f"%{_escape_like(q)}%"
            rows = db.execute(
                """
                SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                       u.display_name AS sender_name, u.avatar_url AS sender_avatar
                FROM messages m
                JOIN users u ON u.id = m.sender_id
                WHERE m.conversation_id = ? AND m.deleted_at IS NULL AND m.text LIKE ? ESCAPE '\\'
                ORDER BY m.created_at DESC
                LIMIT ?
                """,
                (conv_id, search_pattern, limit),
            ).fetchall()

            # Same saturating count as the FTS arm above
            total = db.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT 1 FROM messages
                    WHERE conversation_id = ? AND deleted_at IS NULL AND text LIKE ? ESCAPE '\\'
                    LIMIT ?
                )
                """,
                (conv_id, search_pattern, _SEARCH_TOTAL_CAP),
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
# (events.py _connected_users) — but ONLY for users who
# share at least one conversation with the caller. Everyone
# else answers false, exactly like a genuinely offline
# user, so the route no longer works as a free live-
# presence oracle over arbitrary ids (the mobile call
# sites only ever ask about conversation counterparts).
# Silently truncated to the first 200 ids; non-string ids
# are dropped from the answer instead of crashing the
# lookup. The try/except around the presence lookup
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
    # STEP 1: body — a userIds array; truncated silently
    # rather than a 400, since the list screen sends
    # whatever it has on screen. Non-string ids are dropped
    # ====================================================
    user_id = request.user["id"]
    data = get_json_object()
    if not data or not isinstance(data.get("userIds"), list):
        return jsonify({"error": "userIds array required"}), 400

    user_ids = [uid for uid in data["userIds"] if isinstance(uid, str)]
    if len(user_ids) > 200:
        user_ids = user_ids[:200]


    # STEP 2: the relationship gate — presence is only
    # revealed for users sharing at least one conversation
    # with the caller; everyone else reads as offline, so a
    # stranger's id probes exactly nothing
    # ====================================================
    shared = set()
    if user_ids:
        db = get_db()
        try:
            placeholders = ",".join("?" * len(user_ids))
            rows = db.execute(
                f"""
                SELECT DISTINCT cp2.user_id
                FROM conversation_participants cp1
                JOIN conversation_participants cp2 ON cp2.conversation_id = cp1.conversation_id
                WHERE cp1.user_id = ? AND cp2.user_id IN ({placeholders})
                """,
                [user_id] + user_ids,
            ).fetchall()
            shared = {r["user_id"] for r in rows}
        finally:
            db.close()


    # STEP 3: presence is this process's socket table; an
    # import failure reads as everybody offline, never as an
    # error
    # =====================================================
    try:
        from app.chat.events import _connected_users
        online_set = set(_connected_users.values())
    except Exception:
        online_set = set()

    result = {uid: (uid in shared and uid in online_set) for uid in user_ids}
    return jsonify({"online": result})








############################################################
# search_users
############################################################
#
# GET /api/chat/users/search
#
# ?q substring match on username OR display_name (LIKE:
# ASCII-only case folding; q's \, % and _ escaped via
# _escape_like + ESCAPE so they match literally),
# excluding the caller and every DEACTIVATED account (an
# admin-disabled user must not be offered as a chat
# partner — create_conversation refuses them too). The 20
# rows are RANKED, not the table order SQLite happened to
# scan: an exact username hit first, then a display name
# starting with q, then the rest, each tier by display
# name (NOCASE) with the id as the tiebreaker — so the
# same query always answers the same 20 people. Offset
# paging can follow later as additive query params.
# Every call spends the shared rate_limit budget (120 per
# 5 min per user, 429 past it) — that is what actually
# stops directory enumeration; anything under 2 chars then
# answers {users: []} with 200 rather than a 400, so the
# picker can call it on every keystroke while a single
# letter no longer pages through the whole directory — and
# so does a q carrying a NUL byte, which sqlite3 would bind
# NUL-terminated and thereby collapse the pattern to '%'
# right past that gate. Returns id, username, displayName,
# avatarUrl, role — no email; username and role stay in
# the payload because the mobile SearchUserResult types
# both as required (frozen contract).
#
# Used by:
#   - services/api/chat.ts — searchUsersApi
#     (app/(main)/new-chat/index.tsx — the people picker)
############################################################

@chat_bp.route("/users/search", methods=["GET"])
@require_auth
@rate_limit("chat_user_search", max_attempts=120)
def search_users():
    # Under 2 chars is the keystroke warm-up, not a search —
    # answered empty with no directory hit at all. A NUL byte
    # goes out the same door: python-sqlite3 binds TEXT
    # NUL-TERMINATED, so SQLite receives the pattern truncated
    # at the first one — "\0\0" survives the length gate above
    # and then binds as a bare '%' that pages the WHOLE
    # directory, which is precisely the enumeration this gate
    # exists to stop (and "kregzd\0zzz" loses its trailing '%'
    # too, silently turning into a suffix match). No account
    # name holds one, so such a needle matches nobody
    user_id = request.user["id"]
    q = request.args.get("q", "").strip()
    if len(q) < 2 or "\x00" in q:
        return jsonify({"users": []})

    db = get_db()
    try:
        search_pattern = f"%{_escape_like(q)}%"
        prefix_pattern = f"{_escape_like(q)}%"
        rows = db.execute(
            """
            SELECT id, username, display_name, avatar_url, role
            FROM users
            WHERE id != ? AND active = 1 AND (
                username LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\'
            )
              AND id NOT IN (SELECT blocked_id FROM user_blocks WHERE blocker_id = ?)
              AND id NOT IN (SELECT blocker_id FROM user_blocks WHERE blocked_id = ?)
            ORDER BY
                CASE
                    WHEN username = ? COLLATE NOCASE THEN 0
                    WHEN display_name LIKE ? ESCAPE '\\' THEN 1
                    ELSE 2
                END,
                display_name COLLATE NOCASE,
                id
            LIMIT 20
            """,
            (user_id, search_pattern, search_pattern, user_id, user_id, q, prefix_pattern),
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
