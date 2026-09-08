############################################################
#  [*] Chat API — conversations, messages, reactions, presence
#
#  REST side of messaging. Live delivery (new_message,
#  message_deleted, reaction_update, messages_read, typing)
#  lives in chat/events.py: this module writes the rows,
#  commits, then hands the fan-out to the emit_* helpers, so
#  a client without a socket still sees everything on its
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
#      this process — right only for the single gthread
#      worker the stack runs (see chat/socket.py).
#    - Every route needs a session token (require_auth);
#      membership checks vary per route — see each banner.
#
#  EVERY view here is @transaction.non_atomic_requests: the
#  socket fan-out must fire strictly AFTER a commit (a
#  listener refetches on the event — it must find the
#  rows), so the writes ride transaction.atomic() blocks at
#  their own boundaries instead of one request-wide
#  transaction. The two places that must take the SQLite
#  write lock up front (create_conversation's
#  dedup-then-insert and _apply_mark_read) issue BEGIN
#  IMMEDIATE by hand on the autocommit connection.
#
#  The raw SQL keeps SQLite spellings where the live engine
#  is SQLite (INSERT OR IGNORE, COLLATE NOCASE, the FTS5
#  MATCH arm) — flagged for the postgres move alongside the
#  feed SQL.
############################################################


import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from django.db import IntegrityError, OperationalError, connection, transaction

from knfapp.common import ratelimit
from knfapp.common.http import get_json_object, json_error, json_response
from knfapp.users.auth import require_auth

logger = logging.getLogger(__name__)

# The fixed reaction picker set — must match the mobile
# REACTION_OPTIONS byte for byte; the heart carries its
# VS-16 variation selector
_ALLOWED_REACTIONS = frozenset(("\U0001F44D", "❤️", "\U0001F602", "\U0001F62E", "\U0001F622", "\U0001F621"))

# The most receipts one mark_read writes (and broadcasts) —
# a bound on the per-call work, not on correctness: ids past
# the cap stay unreceipted, which only softens the sender's
# status chip on ancient history
_MARK_READ_CAP = 500

# In-room search bounds: a longer needle than this is a 400,
# and the hit counter stops at the cap instead of walking the
# whole conversation — a `total` of exactly _SEARCH_TOTAL_CAP
# means "that many or more"
_SEARCH_Q_MAX = 200
_SEARCH_TOTAL_CAP = 500


def _get_sio():
    # Imported at call time: socket.py imports events.py which
    # reaches back into this module for _apply_mark_read — only
    # one of the two may bind at import time. Emitting on a
    # Server with no connected clients is a no-op, which is what
    # keeps the REST tests free of any socket server
    from knfapp.chat.socket import sio
    return sio


def _spawn(target, *args):
    # The background seam (push fan-out, link unfurl) — a
    # daemon thread, so the request answers without waiting
    # and an exit never blocks on stragglers
    threading.Thread(target=target, args=args, daemon=True).start()


def _now_naive():
    # Naive-UTC isoformat — the shape every stamp in these
    # tables carries; cursors and unread counts compare it
    # as strings
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _dict_rows(cursor):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _dict_row(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


def _q(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return _dict_rows(cursor)


def _q1(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return _dict_row(cursor)


def _exec(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount


def _is_member(conv_id, user_id):
    return _q1(
        "SELECT 1 AS x FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
        (conv_id, user_id),
    ) is not None


def _begin_immediate():
    # The SQLite write lock, taken up front — but ONLY on the
    # autocommit connection production runs on. Inside an
    # enclosing atomic block (the test harness wraps every test
    # in one) a nested BEGIN would raise, and the COMMIT below
    # would commit the harness's transaction — so there the
    # enclosing block is the serialisation and this is a no-op.
    # Returns whether a transaction was actually opened
    if connection.in_atomic_block:
        return False
    with connection.cursor() as cursor:
        cursor.execute("BEGIN IMMEDIATE")
    return True


def _end_immediate(started, commit):
    if not started:
        return
    with connection.cursor() as cursor:
        cursor.execute("COMMIT" if commit else "ROLLBACK")








############################################################
# _format_time / _epoch_ms / _escape_like
############################################################
#
# _format_time: HH:MM of a naive UTC ISO stamp — UTC, NOT
# Lithuanian time, which is why clients ignore `time` and
# format createdAt themselves. _epoch_ms pins the stamp to
# timezone.utc before .timestamp(), so the number is right
# whatever /etc/localtime says. Both fail soft ("" / 0), so
# ONE bad row can never 500 a whole listing. _escape_like
# escapes \, % and _ in user-typed search text so LIKE
# matches them literally — every LIKE built from the result
# must carry ESCAPE '\'.
############################################################

def _format_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def _epoch_ms(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _escape_like(q):
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")








############################################################
# _is_local_upload_url / _is_meme_library_url
############################################################
#
# Whether an image_url points at THIS server's /api/uploads/
# tree: the relative path the upload route returns, or its
# absolute same-origin form. ONE rule for both ends of a
# photo's life — send_message refuses every other value and
# delete_message hands exactly this set to the uploads
# cleanup helper — so a form the send accepts can never be a
# blob the unsend then orphans. `request` is optional on
# purpose: outside a request (the maintenance sweep) there
# is no host to agree with, and stored paths are relative
# anyway. A non-string answers False instead of raising.
#
# The meme twin is kept APART: the unsend/expiry cleanup
# deletes only /api/uploads/ files, never the library's.
############################################################

def _is_local_upload_url(url, request=None):
    if not isinstance(url, str):
        return False

    if url.startswith("/api/uploads/"):
        return True

    if request is None:
        return False
    parsed = urlparse(url)
    return (parsed.scheme in ("http", "https")
            and parsed.netloc == request.get_host()
            and parsed.path.startswith("/api/uploads/"))


def _is_meme_library_url(url, request=None):
    if not isinstance(url, str):
        return False
    if url.startswith("/api/memes/file/"):
        return True
    if request is None:
        return False
    parsed = urlparse(url)
    return (parsed.scheme in ("http", "https")
            and parsed.netloc == request.get_host()
            and parsed.path.startswith("/api/memes/file/"))








############################################################
# _reply_payload
############################################################
#
# The quoted-message block a client renders inside a reply
# bubble, built from the LEFT JOIN columns the pages select
# (prefix reply_*). A quoted message that was since unsent
# keeps its sender but loses its content. A DANGLING
# reply_to_id (the quoted row vanished, so the LEFT JOIN
# missed) is shaped as deleted with blank content, never as
# a live quote with a null sender. None when the message is
# not a reply.
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
            "kind": "text",
            "fileName": None,
        }

    deleted = row["reply_deleted_at"] is not None
    # A quoted gallery lends its first photo as the thumbnail
    reply_image = row["reply_image_url"]
    if not reply_image and row.get("reply_gallery"):
        try:
            first = (json.loads(row["reply_gallery"]) or [{}])[0]
            reply_image = first.get("url") if isinstance(first, dict) else None
        except (TypeError, ValueError):
            reply_image = None
    return {
        "id": row["reply_to_id"],
        "senderId": row["reply_sender_id"],
        "senderName": row["reply_sender_name"],
        "text": "" if deleted else (row["reply_text"] or ""),
        "imageUrl": None if deleted else reply_image,
        "deleted": deleted,
        # The quote line says "Video" / the file's name when the
        # quoted row has no text
        "kind": row["reply_kind"] or "text",
        "fileName": None if deleted else row["reply_file_name"],
    }








############################################################
# _insert_system_message
############################################################
#
# The room narrating itself: "X sukūrė grupę „Y“", "X paliko
# pokalbį". A 'system' row is stored like any message (the
# actor is its sender, so every JOIN keeps working) and the
# returned payload is what emit_new_message broadcasts; the
# client renders kind 'system' as a centred caption. LT text
# on purpose — the app is Lithuanian-first. Bumps the
# conversation so the room sorts to the top of the list.
# Every NOT NULL column is named — the Django-built table
# carries no DDL defaults.
############################################################

def _insert_system_message(conv_id, actor, text):
    msg_id = str(uuid.uuid4())
    now = _now_naive()
    _exec(
        "INSERT INTO messages (id, conversation_id, sender_id, text, kind, forwarded, created_at)"
        " VALUES (%s, %s, %s, %s, 'system', 0, %s)",
        (msg_id, conv_id, actor["id"], text, now),
    )
    _exec("UPDATE conversations SET updated_at = %s WHERE id = %s", (now, conv_id))
    return {
        "id": msg_id,
        "conversationId": conv_id,
        "senderId": actor["id"],
        "senderName": actor["display_name"],
        "senderAvatar": actor.get("avatar_url"),
        "text": text,
        "imageUrl": None,
        "time": _format_time(now),
        "createdAt": now,
        "clientMsgId": None,
        "reactions": [],
        "replyTo": None,
        "deleted": False,
        "kind": "system",
        "editedAt": None,
        "attachment": None,
    }








############################################################
# _attachment_payload / _media_payload /
# _link_preview_payload / _gallery_payload
############################################################
#
# The optional frames of the richer message shapes: the
# `attachment` object of a 'file' message, the media frame
# of a photo/video (the JSON column parsed), the unfurled
# card of the first URL, and the photo list of a multi-photo
# row. All None for other rows, for unsent ones (the
# blanking clears the columns; these mirror it on the wire)
# and for anything unparseable.
############################################################

def _attachment_payload(row, deleted=False):
    if deleted or not row["attachment_url"]:
        return None
    return {
        "url": row["attachment_url"],
        "name": row["attachment_name"] or "",
        "size": row["attachment_size"] or 0,
        "mime": row["attachment_mime"] or "",
    }


def _media_payload(row, deleted=False):
    raw = row.get("attachment_meta")
    if deleted or not raw:
        return None
    try:
        meta = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    return {
        "width": meta.get("width"),
        "height": meta.get("height"),
        "duration": meta.get("duration"),
        "thumbnailUrl": meta.get("thumbnailUrl"),
        "preview": meta.get("preview"),
        "waveform": meta.get("waveform"),
    }


def _link_preview_payload(row, deleted=False):
    raw = row.get("link_preview")
    if deleted or not raw:
        return None
    try:
        card = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(card, dict) or not card.get("url"):
        return None
    return {
        "url": card.get("url"),
        "title": card.get("title") or "",
        "description": card.get("description") or "",
        "siteName": card.get("siteName") or "",
        "imageUrl": card.get("imageUrl"),
        "imagePreview": card.get("imagePreview"),
    }


def _gallery_payload(row, deleted=False):
    raw = row.get("gallery")
    if deleted or not raw:
        return None
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(items, list) or not items:
        return None
    return [
        {"url": item.get("url"), "width": item.get("width"), "height": item.get("height"), "preview": item.get("preview")}
        for item in items
        if isinstance(item, dict) and item.get("url")
    ] or None








############################################################
# _sweep_expired
############################################################
#
# Disappearing messages: hard-deletes this conversation's
# rows whose expires_at has passed — files first (photo,
# attachment, poster, link card image, every gallery photo),
# then the reaction and receipt rows, then the messages.
# Called opportunistically from get_messages and
# send_message inside their own atomic blocks; clients also
# drop expired rows by their own clocks, so a row that slips
# into a page between sweeps still vanishes on screen. The
# daily maintenance command runs the same sweep for rooms
# nobody reopens.
############################################################

def _sweep_expired(conv_id, request=None):
    now = _now_naive()
    rows = _q(
        "SELECT id, image_url, attachment_url, attachment_meta, link_preview, gallery FROM messages"
        " WHERE conversation_id = %s AND expires_at IS NOT NULL AND expires_at <= %s",
        (conv_id, now),
    )
    if not rows:
        return

    for row in rows:
        stored_urls = [row["image_url"], row["attachment_url"]]
        try:
            stored_urls.append((json.loads(row["attachment_meta"]) or {}).get("thumbnailUrl") if row["attachment_meta"] else None)
        except (TypeError, ValueError):
            pass
        try:
            stored_urls.append((json.loads(row["link_preview"]) or {}).get("imageUrl") if row["link_preview"] else None)
        except (TypeError, ValueError):
            pass
        try:
            if row["gallery"]:
                stored_urls.extend(item.get("url") for item in (json.loads(row["gallery"]) or []) if isinstance(item, dict))
        except (TypeError, ValueError):
            pass
        for stored in stored_urls:
            if not _is_local_upload_url(stored, request):
                continue
            try:
                from knfapp.uploads.storage import delete_upload
                delete_upload(stored)
            except Exception:
                logger.exception("Upload cleanup failed for expired message")

    ids = [row["id"] for row in rows]
    marks = ",".join(["%s"] * len(ids))
    with transaction.atomic():
        _exec(f"DELETE FROM message_reactions WHERE message_id IN ({marks})", ids)
        _exec(f"DELETE FROM message_reads WHERE message_id IN ({marks})", ids)
        _exec(f"DELETE FROM messages WHERE id IN ({marks})", ids)








############################################################
# _find_committed_send
############################################################
#
# The idempotent-replay lookup: the caller's already
# committed message carrying this client_msg_id in this
# conversation, shaped exactly like send_message's response
# message (reply quote included, senderAvatar off the
# session user, status "sent", readBy [sender]) — or None.
# A retry after a client timeout, or the loser of a racing
# double-submit, answers with this row and a 200 instead of
# inserting a duplicate. The idempotency index on
# (conversation, sender, client_msg_id) is what closes the
# race.
############################################################

def _find_committed_send(conv_id, user_id, sender_name, sender_avatar, client_msg_id):
    row = _q1(
        """
        SELECT m.id, m.text, m.image_url, m.created_at, m.client_msg_id,
               m.reply_to_id, m.deleted_at,
               m.kind, m.edited_at, m.attachment_url, m.attachment_name, m.attachment_size, m.attachment_mime,
               m.attachment_meta, m.link_preview, m.gallery, m.pinned_at, m.pinned_by, m.forwarded, m.expires_at,
               r.sender_id AS reply_sender_id, r.text AS reply_text,
               r.image_url AS reply_image_url, r.gallery AS reply_gallery, r.deleted_at AS reply_deleted_at,
               r.kind AS reply_kind, r.attachment_name AS reply_file_name,
               ru.display_name AS reply_sender_name
        FROM messages m
        LEFT JOIN messages r ON r.id = m.reply_to_id
        LEFT JOIN users ru ON ru.id = r.sender_id
        WHERE m.conversation_id = %s AND m.sender_id = %s AND m.client_msg_id = %s
        """,
        (conv_id, user_id, client_msg_id),
    )
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
        "kind": row["kind"] or "text",
        "editedAt": row["edited_at"],
        "attachment": _attachment_payload(row, deleted),
        "media": _media_payload(row, deleted),
        "linkPreview": _link_preview_payload(row, deleted),
        "gallery": _gallery_payload(row, deleted),
        "pinnedAt": row.get("pinned_at"),
        "pinnedBy": row.get("pinned_by"),
        "forwarded": bool(row.get("forwarded")),
        "expiresAt": row.get("expires_at"),
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
# pass it (get_messages) and every group also carries
# bySelf; leave it None for the react/unreact answers and
# the reaction_update broadcast, which are read by many
# clients and therefore carry NO bySelf (the mobile side
# derives it from byUserIds). The single-message callers
# run it INSIDE their write transaction, so the snapshot
# they broadcast is exactly the state their own write
# produced. Neither membership nor the messages' existence
# is checked here: that is the routes' job.
############################################################

def _reactions_for(msg_ids, current_user_id=None):
    if not msg_ids:
        return {}

    placeholders = ",".join(["%s"] * len(msg_ids))
    rows = _q(
        f"""
        SELECT mr.message_id, mr.emoji, mr.user_id
        FROM message_reactions mr
        WHERE mr.message_id IN ({placeholders})
        """,
        list(msg_ids),
    )

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
# The chat push fan-out, run OFF the request thread — the
# send answers 201 without waiting on Expo's HTTP
# round-trip. Goes straight through the batched
# notify_channel_users (one query, one Expo batch per
# language, "chat" opt-outs honoured in SQL). No
# request context in here — everything arrives as
# arguments, and every failure is logged and swallowed
# (push never owes anybody an error). The thread's DB
# connection is closed on the way out.
############################################################

def _push_chat_message(recipient_ids, title, body, data):
    try:
        from knfapp.notifications.push import notify_channel_users
        notify_channel_users("chat", recipient_ids, title, body, data=data)
    except Exception:
        logger.exception("Chat push fan-out failed")
    finally:
        connection.close()








############################################################
# _find_direct_conversation
############################################################
#
# The id of the existing two-person direct chat between
# these users, or None. It drives from the CALLER's own
# membership rows, so the lookup touches the handful of
# conversations they are in instead of scanning the whole
# conversations table on every direct create. The
# COUNT(*) = 2 arm is what keeps a planted multi-member
# 'direct' row from being reused as these two people's DM.
############################################################

def _find_direct_conversation(user_id, other_id):
    row = _q1(
        """
        SELECT cp.conversation_id
        FROM conversation_participants cp
        JOIN conversations c ON c.id = cp.conversation_id AND c.type = 'direct'
        WHERE cp.user_id = %s
          AND EXISTS (
              SELECT 1 FROM conversation_participants o
              WHERE o.conversation_id = cp.conversation_id AND o.user_id = %s
          )
          AND (
              SELECT COUNT(*) FROM conversation_participants p
              WHERE p.conversation_id = cp.conversation_id
          ) = 2
        LIMIT 1
        """,
        (user_id, other_id),
    )

    return row["conversation_id"] if row else None








############################################################
# list_conversations
############################################################
#
# GET /api/chat/conversations
#
# Every conversation the caller belongs to, pinned first
# then newest activity, with participants, the last message
# and an unread count per row — FOUR queries for the whole
# tab (the memberships, then participants, last messages
# and unread counts set-based over the id list).
# unreadCount is other people's messages newer than the
# caller's last_read_at, compared as ISO strings; a NULL
# last_read_at counts everything, and unsent messages never
# count. A direct chat without a title is named after the
# other participant; when nobody else is (left) in it the
# title stays null and the client renders its localized
# fallback. lastUpdatedMs is _epoch_ms of updated_at — 0
# rather than a 500 on an unparseable stamp, falling back
# to created_at.
#
# Used by:
#   - services/api/chat.ts — fetchConversations
############################################################

@transaction.non_atomic_requests
@require_auth
def list_conversations(request):
    # STEP 1: the caller's memberships — pinned first, then
    # newest activity; the id list drives everything below
    # =====================================================
    user_id = request.user["id"]
    rows = _q(
        """
        SELECT c.id, c.type, c.title, c.avatar_emoji, c.created_at, c.updated_at,
               cp.pinned, cp.last_read_at
        FROM conversations c
        JOIN conversation_participants cp ON cp.conversation_id = c.id
        WHERE cp.user_id = %s
        ORDER BY cp.pinned DESC, c.updated_at DESC
        """,
        (user_id,),
    )

    conv_ids = [row["id"] for row in rows]


    # STEP 2: participants, last message and unread count for
    # the WHOLE tab in three set-based queries keyed off that
    # id list — never three queries per row
    # =======================================================
    participants_map = {}
    last_msg_map = {}
    unread_map = {}
    if conv_ids:
        placeholders = ",".join(["%s"] * len(conv_ids))

        for p in _q(
            f"""
            SELECT cp.conversation_id, u.id, u.display_name, u.avatar_url
            FROM conversation_participants cp
            JOIN users u ON u.id = cp.user_id
            WHERE cp.conversation_id IN ({placeholders})
            """,
            conv_ids,
        ):
            participants_map.setdefault(p["conversation_id"], []).append(p)

        # ROW_NUMBER picks every room's newest row in one pass;
        # the id tiebreak keeps the pick deterministic when two
        # stamps match to the microsecond
        for m in _q(
            f"""
            SELECT conversation_id, id, text, image_url, kind, created_at,
                   sender_id, deleted_at, sender_name
            FROM (
                SELECT m.conversation_id, m.id, m.text, m.image_url, m.kind, m.created_at,
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
        ):
            last_msg_map[m["conversation_id"]] = m

        # One GROUP BY, the same definition total_unread_count
        # uses: a NULL last_read_at must count every message,
        # hence the epoch floor; unsent messages are out — the
        # badge must agree with what the reader can still read
        for cnt in _q(
            f"""
            SELECT m.conversation_id, COUNT(*) AS unread
            FROM messages m
            JOIN conversation_participants cp
              ON cp.conversation_id = m.conversation_id AND cp.user_id = %s
            WHERE m.conversation_id IN ({placeholders})
              AND m.sender_id != %s
              AND m.deleted_at IS NULL
              AND m.created_at > COALESCE(cp.last_read_at, '1970-01-01T00:00:00')
            GROUP BY m.conversation_id
            """,
            [user_id] + conv_ids + [user_id],
        ):
            unread_map[cnt["conversation_id"]] = cnt["unread"]


    # STEP 3: shape each row from the three maps — no further
    # query runs from here on
    # =======================================================
    conversations = []
    for row in rows:
        conv_id = row["id"]
        participants = participants_map.get(conv_id, [])
        last_msg = last_msg_map.get(conv_id)
        unread = unread_map.get(conv_id, 0)

        # Direct chats carry no title of their own — named after
        # the other participant; once the other side has left the
        # title stays null and the client renders its localized
        # fallback
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
                "kind": last_msg["kind"] or "text",
                "time": _format_time(last_msg["created_at"]),
                "senderId": last_msg["sender_id"],
                "senderName": last_msg["sender_name"],
                "deleted": last_msg["deleted_at"] is not None,
            }

        conversations.append(conv)

    return json_response({"conversations": conversations})








############################################################
# create_conversation
############################################################
#
# POST /api/chat/conversations
#
# Body {participantIds[], type?='direct', title?,
# avatarEmoji?}. participantIds must be 1–50 non-empty
# strings, type one of direct/group, title a string ≤100 and
# avatarEmoji a string ≤16. A group REQUIRES a non-blank
# title; a direct chat stores NULL for both title and
# avatarEmoji regardless of the body, so an attacker-chosen
# title can never impersonate the counterpart. The creator
# is always added and duplicate ids collapse via set(); a
# member set that reduces to the caller ALONE is a 400, and
# a direct chat must resolve to exactly two members (400).
# The two-member dedup answers 200 with the existing id
# instead of a fresh 201, and only matches rooms whose
# participant count is really 2. It runs TWICE: once before
# the write lock (the cheap fast path) and once inside
# BEGIN IMMEDIATE, so two racing creates can no longer both
# miss and both insert. Every id must exist in users AND
# still be active (400), and no member may be in a block
# pair with the CREATOR (either direction) — a flat 403
# without naming who blocked whom. Capped at 50 creates per
# 5 min per user (429). Members start with last_read_at =
# now, so the new chat opens with unreadCount 0.
#
# Sockets only auto-join conv:* rooms at connect time and
# the creator's client emits join_conversation when it opens
# the room, so the OTHER online members are joined here
# server-side — otherwise they would miss every live message
# until reconnect and get no push either.
#
# Used by:
#   - services/api/chat.ts — createConversation
############################################################

@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_create", max_attempts=50)
def create_conversation(request):
    # STEP 1: the body
    # ================
    user_id = request.user["id"]
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    participant_ids = data.get("participantIds", [])
    if not isinstance(participant_ids, list) or not participant_ids:
        return json_error("participantIds must be a non-empty array", 400)
    if len(participant_ids) > 50:
        return json_error("participantIds must contain at most 50 ids", 400)
    if not all(isinstance(pid, str) and pid for pid in participant_ids):
        return json_error("participantIds must contain non-empty strings", 400)

    conv_type = data.get("type", "direct")
    if conv_type not in ("direct", "group"):
        return json_error("type must be one of: direct, group", 400)

    title = data.get("title")
    if title is not None and not isinstance(title, str):
        return json_error("title must be a string", 400)
    if title is not None and len(title) > 100:
        return json_error("title must be at most 100 characters", 400)

    avatar_emoji = data.get("avatarEmoji")
    if avatar_emoji is not None and not isinstance(avatar_emoji, str):
        return json_error("avatarEmoji must be a string", 400)
    if avatar_emoji is not None and len(avatar_emoji) > 16:
        return json_error("avatarEmoji must be at most 16 characters", 400)

    if conv_type == "group" and (not title or not title.strip()):
        return json_error("Group conversations require a title", 400)

    # A direct chat is always named after the counterpart —
    # a body-supplied title/emoji would override that name in
    # every list row (impersonation), so neither is stored
    if conv_type == "direct":
        title = None
        avatar_emoji = None


    # STEP 2: the member set — the creator is always in, and
    # set() also collapses duplicate ids; a set of just the
    # caller is no conversation at all, and a direct chat
    # must resolve to exactly two people
    # ======================================================
    all_ids = list(set([user_id] + participant_ids))

    if len(all_ids) < 2:
        return json_error("A conversation needs at least one other participant", 400)

    if conv_type == "direct" and len(all_ids) != 2:
        return json_error("Direct conversations must have exactly 2 participants", 400)


    # STEP 3: every id must be a real, still-active user — one
    # IN query, so a single unknown or deactivated id fails
    # the whole request
    # ========================================================
    placeholders = ",".join(["%s"] * len(all_ids))
    found = _q(f"SELECT id FROM users WHERE id IN ({placeholders}) AND active = 1", all_ids)
    if len(found) != len(all_ids):
        return json_error("One or more participant IDs are invalid", 400)


    # STEP 3.1: no member may be in a block pair with the
    # creator, either direction — one flat 403 that does not
    # say who blocked whom (the block's existence is not the
    # creator's business, only its effect is)
    # =====================================================
    other_ids = [uid for uid in all_ids if uid != user_id]
    other_ph = ",".join(["%s"] * len(other_ids))
    blocked_pair = _q1(
        f"""SELECT 1 AS x FROM user_blocks
            WHERE (blocker_id = %s AND blocked_id IN ({other_ph}))
               OR (blocked_id = %s AND blocker_id IN ({other_ph}))
            LIMIT 1""",
        [user_id, *other_ids, user_id, *other_ids],
    )
    if blocked_pair:
        return json_error("One or more participants cannot be added", 403)


    # STEP 4: a direct chat between two people is reused —
    # the existing id answers with 200, not 201. This is the
    # pre-lock fast path; the same lookup runs again under
    # BEGIN IMMEDIATE below
    # =====================================================
    other_id = None
    if conv_type == "direct":
        other_id = [uid for uid in all_ids if uid != user_id][0]
        existing = _find_direct_conversation(user_id, other_id)
        if existing:
            return json_response({"conversationId": existing}, status=200)


    # STEP 5: the conversation and its members in ONE write
    # transaction — BEGIN IMMEDIATE by hand on the autocommit
    # connection takes the write lock first, then the direct
    # dedup runs once more under it: the loser of a
    # double-submit finds its twin's row and answers 200
    # instead of inserting a second DM. last_read_at = now so
    # the chat opens with unreadCount 0 for everybody
    # =======================================================
    started = _begin_immediate()
    try:
        if conv_type == "direct":
            existing = _find_direct_conversation(user_id, other_id)
            if existing:
                _end_immediate(started, commit=False)
                return json_response({"conversationId": existing}, status=200)

        conv_id = str(uuid.uuid4())
        now = _now_naive()

        _exec(
            "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (conv_id, conv_type, title, avatar_emoji, user_id, now, now),
        )

        for uid in all_ids:
            # joined_at named too — the Django-built table has no
            # DDL default for it
            _exec(
                "INSERT INTO conversation_participants (conversation_id, user_id, pinned, last_read_at, joined_at)"
                " VALUES (%s, %s, 0, %s, %s)",
                (conv_id, uid, now, now),
            )

        # A group opens with its own first line — who made it and
        # what it is called — so the room never starts blank
        system_payload = None
        if conv_type == "group":
            system_payload = _insert_system_message(
                conv_id, request.user,
                f"{request.user['display_name']} sukūrė grupę „{(title or '').strip()}“",
            )

        _end_immediate(started, commit=True)
    except Exception:
        _end_immediate(started, commit=False)
        raise


    # STEP 6: pull the online members' sockets into the new
    # room — the OTHER online members would miss every live
    # message until reconnect otherwise (send_message skips
    # online users when pushing)
    # =====================================================
    try:
        from knfapp.chat.events import _connected_users
        sio = _get_sio()
        room = f"conv:{conv_id}"
        # list() snapshot: connects/disconnects on other threads
        # mutate the dict while this loop runs
        for sid, uid in list(_connected_users.items()):
            if uid in all_ids:
                try:
                    sio.enter_room(sid, room)
                except (KeyError, ValueError):
                    # That socket disconnected between the snapshot
                    # and this call — presence plumbing must never
                    # fail an already-committed create
                    logger.debug("enter_room skipped for departed sid %s", sid)
    except Exception:
        logger.exception("Room join failed after create")

    # The opening line reaches the members now in the room
    if system_payload:
        try:
            from knfapp.chat.events import emit_new_message
            emit_new_message(_get_sio(), conv_id, system_payload)
        except Exception:
            logger.exception("System message broadcast failed after create")

    return json_response({"conversationId": conv_id}, status=201)








############################################################
# _page_rows / get_messages / get_changes
############################################################
#
# One history page, fetched newest-first and reversed to
# chronological order. ?limit (default 50, cap 100) and
# three cursor windows sharing one SELECT (_page_rows):
#   ?before[&before_id]  strictly older than the (stamp, id)
#                        composite cursor — the id breaks
#                        stamp ties so equal-stamp siblings
#                        never fall through a page boundary;
#                        without before_id the id arm
#                        compares against NULL (never true)
#                        and degrades to the bare-stamp cut
#   ?after&after_id      strictly newer — walking forward
#                        from an anchored window to the head
#   ?around=<id>         half a page either side of one
#                        message (anchor included): a search
#                        hit or a quoted message beyond the
#                        loaded history in one round trip
# hasMore/hasNewer are exact: each side fetches limit+1 rows
# and the probe row only answers the flag. Garbage ?limit is
# a 400 and the clamp keeps a negative from reaching SQLite
# as LIMIT -n. Members only (403).
#
# Each message carries reactions (with bySelf here), readBy,
# replyTo, clientMsgId, deleted (unsent — content blanked)
# and, for the caller's OWN messages, status derived from
# how many OTHER members hold a receipt. The envelope also
# ships participants (sorted by display name) and the
# conversation row itself, so a room opened from a push
# notification draws its header without a second call, plus
# the server-clock cursor the change feed resumes from.
#
# get_changes (?since=<iso>) answers every message edited or
# unsent after that moment as full rows — the resync door
# for edits/unsends outside the client's newest page. 400 on
# a malformed since; at most 500 rows.
#
# Used by:
#   - services/api/chat.ts — fetchMessages / fetchChanges
############################################################

def _page_rows(where, params, order, limit):
    return _q(
        f"""
        SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
               m.client_msg_id,
               m.kind, m.edited_at, m.attachment_url, m.attachment_name, m.attachment_size, m.attachment_mime,
               m.attachment_meta, m.link_preview, m.gallery, m.pinned_at, m.pinned_by, m.forwarded, m.expires_at,
               u.display_name AS sender_name, u.avatar_url AS sender_avatar,
               m.reply_to_id, m.deleted_at,
               r.sender_id AS reply_sender_id, r.text AS reply_text,
               r.image_url AS reply_image_url, r.gallery AS reply_gallery, r.deleted_at AS reply_deleted_at,
               r.kind AS reply_kind, r.attachment_name AS reply_file_name,
               ru.display_name AS reply_sender_name
        FROM messages m
        JOIN users u ON u.id = m.sender_id
        LEFT JOIN messages r ON r.id = m.reply_to_id
        LEFT JOIN users ru ON ru.id = r.sender_id
        WHERE {where}
        ORDER BY m.created_at {order}, m.id {order} LIMIT %s
        """,
        (*params, limit),
    )


@transaction.non_atomic_requests
@require_auth
def get_messages(request, conv_id):
    # STEP 1: membership gate — outsiders get 403 before any
    # message row is read
    # ======================================================
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    # Disappearing messages leave before the page is read
    _sweep_expired(conv_id, request)


    # STEP 2: the page — see the banner for the windows
    # =================================================
    before = request.GET.get("before")
    before_id = request.GET.get("before_id")
    after = request.GET.get("after")
    after_id = request.GET.get("after_id")
    around = request.GET.get("around")
    try:
        limit = int(request.GET.get("limit", 50))
    except (TypeError, ValueError):
        return json_error("limit must be an integer", 400)
    limit = max(1, min(limit, 100))
    has_newer = False
    if around:
        anchor = _q1(
            "SELECT created_at, id FROM messages WHERE id = %s AND conversation_id = %s",
            (around, conv_id),
        )
        if not anchor:
            return json_error("Message not found", 404)
        half = max(1, limit // 2)
        older = _page_rows(
            "m.conversation_id = %s AND (m.created_at < %s OR (m.created_at = %s AND m.id <= %s))",
            (conv_id, anchor["created_at"], anchor["created_at"], anchor["id"]),
            "DESC",
            half + 1,
        )
        newer = _page_rows(
            "m.conversation_id = %s AND (m.created_at > %s OR (m.created_at = %s AND m.id > %s))",
            (conv_id, anchor["created_at"], anchor["created_at"], anchor["id"]),
            "ASC",
            half + 1,
        )
        has_more = len(older) > half
        has_newer = len(newer) > half
        rows = list(reversed(newer[:half])) + older[:half]
    elif after:
        newer = _page_rows(
            "m.conversation_id = %s AND (m.created_at > %s OR (m.created_at = %s AND m.id > %s))",
            (conv_id, after, after, after_id),
            "ASC",
            limit + 1,
        )
        has_newer = len(newer) > limit
        rows = list(reversed(newer[:limit]))
        # Older than this page: the caller already holds it, but
        # the flag stays truthful for a client that started here
        has_more = _q1(
            "SELECT 1 AS x FROM messages WHERE conversation_id = %s AND (created_at < %s OR (created_at = %s AND id <= %s)) LIMIT 1",
            (conv_id, after, after, after_id),
        ) is not None
    elif before:
        rows = _page_rows(
            "m.conversation_id = %s AND (m.created_at < %s OR (m.created_at = %s AND m.id < %s))",
            (conv_id, before, before, before_id),
            "DESC",
            limit + 1,
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        rows = _page_rows("m.conversation_id = %s", (conv_id,), "DESC", limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]


    # STEP 3: reactions and read receipts for the whole page
    # in two IN (...) queries instead of two per message
    # ======================================================
    msg_ids = [row["id"] for row in rows]
    reaction_map_all = _reactions_for(msg_ids, user_id)
    read_map_all = {}
    if msg_ids:
        placeholders = ",".join(["%s"] * len(msg_ids))
        for rd in _q(
            f"SELECT mrd.message_id, mrd.user_id FROM message_reads mrd WHERE mrd.message_id IN ({placeholders})",
            msg_ids,
        ):
            read_map_all.setdefault(rd["message_id"], []).append(rd["user_id"])


    # STEP 4: the members and the conversation row — the room
    # header and intro card draw portraits and the title from
    # these, and the member count feeds the own-message status
    # ========================================================
    member_rows = _q(
        """
        SELECT u.id, u.display_name, u.avatar_url
        FROM conversation_participants cp
        JOIN users u ON u.id = cp.user_id
        WHERE cp.conversation_id = %s
        ORDER BY u.display_name
        """,
        (conv_id,),
    )
    participants = [
        {"id": m["id"], "displayName": m["display_name"], "avatarUrl": m["avatar_url"]}
        for m in member_rows
    ]
    participant_count = len(member_rows)

    # The conversation itself — a room opened from a push
    # notification has no title or type in its route params
    conv_row = _q1(
        "SELECT id, type, title, avatar_emoji, message_ttl_seconds FROM conversations WHERE id = %s",
        (conv_id,),
    )
    conversation = {
        "id": conv_row["id"],
        "type": conv_row["type"],
        "title": conv_row["title"],
        "avatarEmoji": conv_row["avatar_emoji"],
        "messageTtlSeconds": conv_row.get("message_ttl_seconds"),
    } if conv_row else None


    # STEP 5: shape each message — reactions with bySelf,
    # readBy, and for the caller's own messages a status
    # derived from how many OTHER members hold a receipt
    # ===================================================
    messages = []
    for row in rows:
        msg_id = row["id"]
        reactions = reaction_map_all.get(msg_id, [])
        read_by = read_map_all.get(msg_id, [])
        is_own = row["sender_id"] == user_id
        # An unsent message keeps its slot but ships no content
        deleted = row["deleted_at"] is not None
        if is_own:
            # "read" needs a receipt from every other member,
            # "delivered" from at least one; a chat with no other
            # member is trivially read
            other_readers = [uid for uid in read_by if uid != user_id]
            others_count = participant_count - 1  # the sender's own receipt never counts
            if others_count <= 0 or len(other_readers) >= others_count:
                status = "read"
            elif len(other_readers) > 0:
                status = "delivered"
            else:
                status = "sent"
        else:
            # status only means something on own messages — a
            # fixed value for everybody else's
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
            "kind": row["kind"] or "text",
            "editedAt": row["edited_at"],
            "attachment": _attachment_payload(row, deleted),
            "media": _media_payload(row, deleted),
            "linkPreview": _link_preview_payload(row, deleted),
            "gallery": _gallery_payload(row, deleted),
            "pinnedAt": row.get("pinned_at"),
            "pinnedBy": row.get("pinned_by"),
            "forwarded": bool(row.get("forwarded")),
            "expiresAt": row.get("expires_at"),
        })


    # STEP 6: DESC fetch → chronological list
    # =======================================
    messages.reverse()

    return json_response({
        "messages": messages,
        "hasMore": has_more,
        "hasNewer": has_newer,
        "participants": participants,
        "conversation": conversation,
        # The server clock at the time of the page — the point
        # the client's change feed resumes from
        "cursor": _now_naive(),
    })


@transaction.non_atomic_requests
@require_auth
def get_changes(request, conv_id):
    # STEP 1: the cursor — an ISO stamp, nothing else
    # ===============================================
    user_id = request.user["id"]
    since = (request.GET.get("since") or "").strip()
    if not since:
        return json_error("since is required", 400)
    try:
        datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return json_error("since must be an ISO datetime", 400)
    # Naive UTC, the shape every stamp in this database has
    since_key = since.replace("Z", "").split("+")[0]


    # STEP 2: membership
    # ==================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)


    # STEP 3: the rows that moved after the cursor, shaped like
    # a history row (the client applies them to what it holds)
    # ========================================================
    rows = _q(
        """
        SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id, m.reply_to_id, m.deleted_at,
               m.client_msg_id, u.display_name AS sender_name, u.avatar_url AS sender_avatar,
               m.kind, m.edited_at, m.attachment_url, m.attachment_name, m.attachment_size, m.attachment_mime,
               m.attachment_meta, m.link_preview, m.gallery, m.pinned_at, m.pinned_by, m.forwarded, m.expires_at,
               r.sender_id AS reply_sender_id, r.text AS reply_text,
               r.image_url AS reply_image_url, r.gallery AS reply_gallery, r.deleted_at AS reply_deleted_at,
               r.kind AS reply_kind, r.attachment_name AS reply_file_name,
               ru.display_name AS reply_sender_name
        FROM messages m
        JOIN users u ON u.id = m.sender_id
        LEFT JOIN messages r ON r.id = m.reply_to_id
        LEFT JOIN users ru ON ru.id = r.sender_id
        WHERE m.conversation_id = %s
          AND (m.edited_at > %s OR m.deleted_at > %s)
        ORDER BY m.created_at ASC, m.id ASC
        LIMIT 500
        """,
        (conv_id, since_key, since_key),
    )

    messages = []
    for row in rows:
        deleted = row["deleted_at"] is not None
        messages.append({
            "id": row["id"],
            "conversationId": conv_id,
            "senderId": row["sender_id"],
            "senderName": row["sender_name"],
            "senderAvatar": row["sender_avatar"],
            "text": "" if deleted else row["text"],
            "imageUrl": None if deleted else row["image_url"],
            "time": _format_time(row["created_at"]),
            "createdAt": row["created_at"],
            "clientMsgId": row["client_msg_id"],
            "isOwn": row["sender_id"] == user_id,
            "status": "read",
            "readBy": [],
            "reactions": [],
            "replyTo": _reply_payload(row),
            "deleted": deleted,
            "kind": row["kind"] or "text",
            "editedAt": row["edited_at"],
            "attachment": _attachment_payload(row, deleted),
            "media": _media_payload(row, deleted),
            "linkPreview": _link_preview_payload(row, deleted),
            "gallery": _gallery_payload(row, deleted),
            "pinnedAt": row.get("pinned_at"),
            "pinnedBy": row.get("pinned_by"),
            "forwarded": bool(row.get("forwarded")),
            "expiresAt": row.get("expires_at"),
        })

    return json_response({
        "messages": messages,
        "cursor": _now_naive(),
    })








############################################################
# send_message
############################################################
#
# POST /api/chat/conversations/<id>/messages
#
# Body {text?, imageUrl?, replyToId?, client_msg_id?,
# attachment?, media?, gallery?, kind?, forwarded?}: text
# is stripped, a string of at most 5000 chars; at least one
# of text/imageUrl/attachment/gallery must be present.
# imageUrl must be a string — EVERY non-string is a 400,
# the falsy [] / {} / 0 / false included — and, when
# non-empty, an own /api/uploads/ path (or /api/memes/file/
# for a shared meme): any other value is a 400, so a stored
# message can never point a reader's client at a foreign
# server. replyToId must name a message in THIS conversation
# (400, blank included). client_msg_id is the idempotency
# nonce: a repeat of one already committed answers 200 with
# the EXISTING row — the idempotency index closes the race
# window. Members only (403); a DIRECT chat between a
# blocked pair refuses the send (403) — group sends stay,
# the push lane keeps blocked phones quiet there. One
# transaction inserts the message, bumps
# conversations.updated_at, moves the sender's last_read_at
# forward and writes their own read receipt.
#
# Fan-out after commit: 'new_message' to room conv:<id>,
# then push for every member WITHOUT a socket in that very
# room — room membership, not global presence. The push
# title is the sender's name (plus " · Group title" in a
# group); preview is the first 100 chars or a Lithuanian
# media marker with data.preview for re-localizing;
# recipients with chat_push_preview off get the content-
# free body; @mentions get their own lane and leave the
# plain ones. The Expo round-trips run on a daemon thread.
# A URL in the text starts the unfurl task. Capped at 150
# sends per 5 min per user (429).
#
# Used by:
#   - services/api/chat.ts — sendMessageApi
############################################################

@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_send", max_attempts=150)
def send_message(request, conv_id):
    # STEP 1: validate the body
    # =========================
    user_id = request.user["id"]
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)

    raw_text = data.get("text", "")
    if not isinstance(raw_text, str):
        return json_error("Text must be a string", 400)
    text = raw_text.strip()
    image_url = data.get("imageUrl")

    # An optional document: {url, name, size, mime} — url must be
    # an own upload (the same beacon guard photos pass), name
    # bounded, size a non-negative int
    attachment = data.get("attachment")
    if attachment is not None:
        if not isinstance(attachment, dict):
            return json_error("attachment must be an object", 400)
        att_url = attachment.get("url")
        att_name = attachment.get("name", "")
        att_size = attachment.get("size", 0)
        att_mime = attachment.get("mime", "")
        if not isinstance(att_url, str) or not _is_local_upload_url(att_url, request):
            return json_error("attachment.url must be an /api/uploads/ path", 400)
        if not isinstance(att_name, str) or not att_name.strip() or len(att_name) > 200:
            return json_error("attachment.name must be a non-blank string of at most 200 characters", 400)
        if not isinstance(att_size, int) or isinstance(att_size, bool) or att_size < 0:
            return json_error("attachment.size must be a non-negative integer", 400)
        if not isinstance(att_mime, str) or len(att_mime) > 100:
            return json_error("attachment.mime must be a short string", 400)
        attachment = {"url": att_url, "name": att_name.strip(), "size": att_size, "mime": att_mime.strip()}

    # The frame of a photo / video: optional non-negative numbers
    # and a poster that must be an own upload
    media = data.get("media")
    if media is not None:
        if not isinstance(media, dict):
            return json_error("media must be an object", 400)
        clean = {}
        for key in ("width", "height", "duration"):
            value = media.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 10_000_000:
                return json_error(f"media.{key} must be a non-negative number", 400)
            clean[key] = value
        thumb = media.get("thumbnailUrl")
        if thumb is not None:
            if not isinstance(thumb, str) or not _is_local_upload_url(thumb, request):
                return json_error("media.thumbnailUrl must be an /api/uploads/ path", 400)
            clean["thumbnailUrl"] = thumb
        # The ~14px micro copy, echoed back so every reader draws
        # the blur before the bytes; a data URI only — never a
        # fetchable address
        preview_uri = media.get("preview")
        if preview_uri is not None:
            if not isinstance(preview_uri, str) or not preview_uri.startswith("data:image/") or len(preview_uri) > 2000:
                return json_error("media.preview must be a small data:image/ URI", 400)
            clean["preview"] = preview_uri
        # A voice note's amplitude bars: up to 64 numbers in 0..1
        waveform = media.get("waveform")
        if waveform is not None:
            if not isinstance(waveform, list) or len(waveform) > 64:
                return json_error("media.waveform must be a list of at most 64 numbers", 400)
            bars = []
            for value in waveform:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1:
                    return json_error("media.waveform values must be numbers between 0 and 1", 400)
                bars.append(round(float(value), 3))
            clean["waveform"] = bars
        media = clean or None

    # Several photos in one message: a list of 2–8 {url, width,
    # height} objects, every url an own upload. A gallery rides
    # alone — no single imageUrl and no file beside it
    gallery = data.get("gallery")
    if gallery is not None:
        if not isinstance(gallery, list) or not 2 <= len(gallery) <= 8:
            return json_error("gallery must be a list of 2 to 8 photos", 400)
        clean_items = []
        for item in gallery:
            if not isinstance(item, dict):
                return json_error("every gallery item must be an object", 400)
            item_url = item.get("url")
            if not isinstance(item_url, str) or not _is_local_upload_url(item_url, request):
                return json_error("every gallery url must be an /api/uploads/ path", 400)
            clean_item = {"url": item_url}
            for key in ("width", "height"):
                value = item.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 10_000_000:
                    return json_error(f"gallery {key} must be a non-negative number", 400)
                clean_item[key] = value
            item_preview = item.get("preview")
            if item_preview is not None:
                if not isinstance(item_preview, str) or not item_preview.startswith("data:image/") or len(item_preview) > 2000:
                    return json_error("gallery preview must be a small data:image/ URI", 400)
                clean_item["preview"] = item_preview
            clean_items.append(clean_item)
        gallery = clean_items
        if image_url or attachment:
            return json_error("A gallery cannot ride with a single image or a file", 400)
        if data.get("kind") not in (None, "image"):
            return json_error("A gallery message's kind is image", 400)

    # The declared kind must match the content
    kind_param = data.get("kind")
    if kind_param is not None and kind_param not in ("text", "image", "file", "video", "audio"):
        return json_error("kind must be text, image, file, video or audio", 400)
    if kind_param in ("video", "audio") and not attachment:
        return json_error("A video or audio message needs an attachment", 400)
    if kind_param == "image" and not image_url and not gallery:
        return json_error("An image message needs imageUrl", 400)

    if not text and not image_url and not attachment and not gallery:
        return json_error("Message must have text, an image or an attachment", 400)

    if text and len(text) > 5000:
        return json_error("Message text must not exceed 5000 characters", 400)

    reply_to_id = data.get("replyToId")
    if reply_to_id is not None and not isinstance(reply_to_id, str):
        return json_error("replyToId must be a string", 400)
    # A blank quote id is a client bug, not "no reply"
    if reply_to_id is not None and not reply_to_id.strip():
        return json_error("replyToId must not be blank", 400)

    client_msg_id = data.get("client_msg_id")
    if client_msg_id is not None and not isinstance(client_msg_id, str):
        return json_error("client_msg_id must be a string", 400)
    if client_msg_id and len(client_msg_id) > 128:
        return json_error("client_msg_id too long", 400)

    # A message re-sent from another room carries only this mark
    forwarded = data.get("forwarded")
    if forwarded is not None and not isinstance(forwarded, bool):
        return json_error("forwarded must be a boolean", 400)
    forwarded = bool(forwarded)


    # STEP 1.1: imageUrl — the TYPE check sits OUTSIDE the
    # truthiness gate on purpose: a falsy non-string ([], {},
    # 0, false) would skip validation whole and reach the
    # driver as a bind parameter. An empty string stays the
    # one falsy value that passes — stored and echoed as given
    # ======================================================
    if image_url is not None and not isinstance(image_url, str):
        return json_error("imageUrl must be a string", 400)

    if image_url and not _is_local_upload_url(image_url, request) and not _is_meme_library_url(image_url, request):
        return json_error("imageUrl must be an /api/uploads/ or /api/memes/file/ path", 400)


    # STEP 2: membership gate — 403 for outsiders; a quoted
    # message must live in this very conversation (400)
    # =====================================================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    # Disappearing messages leave before the room grows
    _sweep_expired(conv_id, request)


    # STEP 2.0: a DIRECT chat between a blocked pair (either
    # direction) refuses the send — create_conversation stops
    # NEW rooms, this stops the room that already existed when
    # the block was placed. Group sends stay: membership is
    # the group's decision, and the push fan-out below keeps a
    # blocked pair's phones quiet there
    # =======================================================
    conv_type_row = _q1("SELECT type FROM conversations WHERE id = %s", (conv_id,))
    if conv_type_row and conv_type_row["type"] == "direct":
        counterpart = _q1(
            "SELECT user_id FROM conversation_participants"
            " WHERE conversation_id = %s AND user_id != %s LIMIT 1",
            (conv_id, user_id),
        )
        if counterpart:
            pair_blocked = _q1(
                "SELECT 1 AS x FROM user_blocks WHERE (blocker_id = %s AND blocked_id = %s)"
                " OR (blocker_id = %s AND blocked_id = %s)",
                (user_id, counterpart["user_id"], counterpart["user_id"], user_id),
            )
            if pair_blocked:
                return json_error("You cannot message this user", 403)

    reply_row = None
    if reply_to_id:
        reply_row = _q1(
            """
            SELECT r.id AS reply_to_id, r.sender_id AS reply_sender_id, r.text AS reply_text,
                   r.image_url AS reply_image_url, r.gallery AS reply_gallery, r.deleted_at AS reply_deleted_at,
                   r.kind AS reply_kind, r.attachment_name AS reply_file_name,
                   ru.display_name AS reply_sender_name
            FROM messages r
            JOIN users ru ON ru.id = r.sender_id
            WHERE r.id = %s AND r.conversation_id = %s
            """,
            (reply_to_id, conv_id),
        )
        if not reply_row:
            return json_error("Quoted message not found in this conversation", 400)


    # STEP 2.1: idempotent replay — a nonce already committed
    # answers 200 with the existing row; no second insert, no
    # second fan-out
    # =======================================================
    if client_msg_id:
        committed = _find_committed_send(
            conv_id, user_id, request.user["display_name"],
            request.user["avatar_url"], client_msg_id,
        )
        if committed:
            return json_response({"message": committed}, status=200)


    # STEP 3: one transaction — the message row, the
    # conversation bump that reorders the list, and the
    # sender's read state in BOTH stores so their own message
    # never shows as unread to them. The idempotency index
    # turns a racing double-submit into an IntegrityError,
    # answered like the replay above
    # ======================================================
    msg_id = str(uuid.uuid4())
    now = _now_naive()

    if attachment:
        kind = kind_param if kind_param in ("video", "audio") else "file"
    else:
        kind = "image" if image_url or gallery else "text"
    # The frame only means something on a photo, a video or a
    # voice note (its duration)
    media_json = json.dumps(media) if media and kind in ("image", "video", "audio") else None
    gallery_json = json.dumps(gallery) if gallery else None
    # Disappearing messages: the room's TTL at SEND time stamps
    # this row's hard-delete deadline — changing the TTL later
    # never touches what was already sent
    ttl_row = _q1("SELECT message_ttl_seconds FROM conversations WHERE id = %s", (conv_id,))
    ttl_seconds = ttl_row.get("message_ttl_seconds") if ttl_row else None
    expires_at = (
        (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ttl_seconds)).isoformat()
        if ttl_seconds
        else None
    )

    try:
        with transaction.atomic():
            _exec(
                """INSERT INTO messages
                   (id, conversation_id, sender_id, text, image_url, reply_to_id, client_msg_id, created_at,
                    kind, attachment_url, attachment_name, attachment_size, attachment_mime, attachment_meta, gallery,
                    forwarded, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (msg_id, conv_id, user_id, text, image_url, reply_to_id or None, client_msg_id or None, now,
                 kind,
                 attachment["url"] if attachment else None,
                 attachment["name"] if attachment else None,
                 attachment["size"] if attachment else None,
                 attachment["mime"] if attachment else None,
                 media_json, gallery_json,
                 1 if forwarded else 0, expires_at),
            )

            _exec("UPDATE conversations SET updated_at = %s WHERE id = %s", (now, conv_id))

            _exec(
                "UPDATE conversation_participants SET last_read_at = %s WHERE conversation_id = %s AND user_id = %s",
                (now, conv_id, user_id),
            )

            _exec(
                "INSERT OR IGNORE INTO message_reads (message_id, user_id, read_at) VALUES (%s, %s, %s)",
                (msg_id, user_id, now),
            )
    except IntegrityError:
        # Only the (conversation_id, sender_id, client_msg_id)
        # index can fire here — membership and the quoted row
        # were just verified; the atomic block has rolled back
        committed = None
        if client_msg_id:
            committed = _find_committed_send(
                conv_id, user_id, request.user["display_name"],
                request.user["avatar_url"], client_msg_id,
            )
        if committed:
            return json_response({"message": committed}, status=200)
        raise


    # STEP 4: live fan-out — 'new_message' to room conv:<id>,
    # the sender's own socket included (clients dedupe by id)
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
        "kind": kind,
        "editedAt": None,
        "attachment": attachment,
        "media": json.loads(media_json) if media_json else None,
        "gallery": gallery or None,
        # Filled in by the unfurl task after the send; the room
        # hears 'message_updated' when it lands
        "linkPreview": None,
        "forwarded": forwarded,
        "expiresAt": expires_at,
        "pinnedAt": None,
        "pinnedBy": None,
    }

    from knfapp.chat.events import emit_new_message
    emit_new_message(_get_sio(), conv_id, msg_data)


    # STEP 5: push for members WITHOUT a socket in this very
    # room — the room the emit above just reached, read from
    # the socket manager. A member online elsewhere (a web tab
    # on another screen, a second device) still gets the push.
    # The Expo round-trips run on a daemon thread; a failure
    # here is logged and the send still succeeds
    # =======================================================
    try:
        from knfapp.chat.events import _connected_users

        sio = _get_sio()
        try:
            room_sids = set(sio.manager.rooms.get("/", {}).get(f"conv:{conv_id}") or ())
        except Exception:
            room_sids = set()
        # list() snapshot: connects/disconnects on other threads
        # mutate the dict while this runs
        in_room_ids = {uid for sid, uid in list(_connected_users.items()) if sid in room_sids}

        participants = _q(
            "SELECT user_id FROM conversation_participants WHERE conversation_id = %s AND user_id != %s",
            (conv_id, user_id),
        )
        recipients = [p["user_id"] for p in participants if p["user_id"] not in in_room_ids]

        # Blocks silence the push lane too: in a group the message
        # stays visible in the room, but a phone in a block pair
        # with the sender (either direction) stays quiet
        if recipients:
            rec_ph = ",".join(["%s"] * len(recipients))
            block_rows = _q(
                f"""SELECT blocker_id, blocked_id FROM user_blocks
                    WHERE (blocker_id = %s AND blocked_id IN ({rec_ph}))
                       OR (blocked_id = %s AND blocker_id IN ({rec_ph}))""",
                [user_id, *recipients, user_id, *recipients],
            )
            silenced = {
                r["blocked_id"] if r["blocker_id"] == user_id else r["blocker_id"]
                for r in block_rows
            }
            recipients = [r for r in recipients if r not in silenced]

        if recipients:
            # The title says WHO wrote and, in a group, WHERE — a
            # bare display name on a lock screen leaves the reader
            # guessing which room it came from
            conv_row = _q1("SELECT type, title FROM conversations WHERE id = %s", (conv_id,))
            push_title = user["display_name"]
            if conv_row and conv_row["type"] == "group" and conv_row["title"]:
                push_title = f"{push_title} · {conv_row['title']}"

            push_data = {"type": "chat_message", "conversationId": conv_id}
            if text:
                preview = text[:100]
            elif attachment and kind == "video":
                # LT for the same reason the photo marker is
                preview = "Vaizdo įrašas"
                push_data["preview"] = "video"
            elif attachment:
                preview = "Failas"
                push_data["preview"] = "file"
            else:
                # A push with an empty body renders nothing on a
                # lock screen, so a photo-only message ships the
                # Lithuanian word (LT is the app default); the
                # marker in data lets a foreground client
                # re-localize
                preview = "Nuotrauka"
                push_data["preview"] = "photo"
            # Preview privacy: a recipient who turned
            # chat_push_preview off gets the content-free body —
            # their message text never leaves for Expo at all
            users_ph = ",".join(["%s"] * len(recipients))
            no_preview = {
                r["id"]
                for r in _q(
                    f"SELECT id FROM users WHERE id IN ({users_ph}) AND chat_push_preview = 0",
                    recipients,
                )
            }
            full_recipients = [r for r in recipients if r not in no_preview]
            quiet_recipients = [r for r in recipients if r in no_preview]

            # Mentions get their own lane: "X paminėjo jus" — a
            # lock-screen line the reader cannot mistake for room
            # chatter. Matched against each recipient's display
            # name as typed, case-insensitively, ending at a word
            # boundary ("@Ona" never claims "@Onaitė"). Mentioned
            # readers leave the plain lanes so nobody gets two
            mentioned = set()
            if text:
                lowered = text.lower()
                for name_row in _q(
                    f"SELECT id, display_name FROM users WHERE id IN ({users_ph})", recipients,
                ):
                    needle = f"@{(name_row['display_name'] or '').strip().lower()}"
                    start = lowered.find(needle) if len(needle) > 1 else -1
                    while start >= 0:
                        after = lowered[start + len(needle):start + len(needle) + 1]
                        before = lowered[start - 1] if start > 0 else " "
                        if (not after or not (after.isalnum() or after == "_")) and before.isspace():
                            mentioned.add(name_row["id"])
                            break
                        start = lowered.find(needle, start + 1)
            if mentioned:
                mention_title = f"{user['display_name']} paminėjo jus"
                if conv_row and conv_row["type"] == "group" and conv_row["title"]:
                    mention_title = f"{mention_title} · {conv_row['title']}"
                mention_data = {**push_data, "type": "chat_mention"}
                full_mentioned = [r for r in full_recipients if r in mentioned]
                quiet_mentioned = [r for r in quiet_recipients if r in mentioned]
                if full_mentioned:
                    _spawn(_push_chat_message, full_mentioned, mention_title, preview, mention_data)
                if quiet_mentioned:
                    _spawn(_push_chat_message, quiet_mentioned, mention_title,
                           "Nauja žinutė", {**mention_data, "preview": "hidden"})
                full_recipients = [r for r in full_recipients if r not in mentioned]
                quiet_recipients = [r for r in quiet_recipients if r not in mentioned]
            if full_recipients:
                _spawn(_push_chat_message, full_recipients, push_title, preview, push_data)
            if quiet_recipients:
                # LT for the same reason the photo marker is
                _spawn(_push_chat_message, quiet_recipients, push_title,
                       "Nauja žinutė", {**push_data, "preview": "hidden"})
    except Exception:
        logger.exception("Push notification failed for chat message")

    # STEP 6: a URL in the text gets its card, off the request
    # thread — see chat/linkpreview.py
    # =======================================================
    try:
        from knfapp.chat.linkpreview import find_url, unfurl_message
        link = find_url(text)
        if link:
            _spawn(unfurl_message, _get_sio(), conv_id, msg_id, link, user_id)
    except Exception:
        logger.exception("Link preview task failed to start")

    # The sender's own view of the message: only their own
    # receipt exists yet, hence status "sent"
    return json_response({"message": {**msg_data, "isOwn": True, "status": "sent", "readBy": [user_id]}}, status=201)








############################################################
# delete_message / edit_message
############################################################
#
# "Unsend": the sender soft-deletes their own message. The
# row survives — replies keep their target and cursors keep
# their order — but text and image are cleared, reactions
# dropped and deleted_at set. An unsent photo's
# /api/uploads/ files (single, attachment, poster, link
# card, every gallery photo) are handed to the uploads
# delete helper in BOTH the forms the send accepts, so
# neither can leave an orphan. Outsider → 403, unknown →
# 404, somebody else's → 403, already unsent → still 200.
# Broadcasts 'message_deleted' ONLY on the call that
# actually unsent it — a repeat is a silent 200. Capped at
# 100 unsends per 5 min per user.
#
# The edit rewrites the sender's own text — bounded like a
# send, only text/image rows, never an unsent one (409).
# edited_at is stamped so every reader shows "redaguota";
# the quote inside replies re-reads the row, so quotes
# follow the edit too. Broadcasts 'message_edited'. Capped
# at 100 edits per 5 min.
#
# Used by:
#   - services/api/chat.ts — deleteMessageApi / editMessageApi
############################################################

@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_delete", max_attempts=100)
def delete_message(request, conv_id, msg_id):
    # STEP 1: membership gate — an outsider learns nothing
    # about the room's messages, not even whether one exists
    # ======================================================
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)


    # STEP 2: the row, its owner, then the soft delete —
    # idempotent, and the broadcast lives INSIDE the guard so
    # a repeat call stays silent on the wire
    # ======================================================
    row = _q1(
        "SELECT sender_id, deleted_at, image_url, attachment_url, attachment_meta, link_preview, gallery"
        " FROM messages WHERE id = %s AND conversation_id = %s",
        (msg_id, conv_id),
    )
    if not row:
        return json_error("Message not found", 404)
    if row["sender_id"] != user_id:
        return json_error("Only the sender can delete a message", 403)

    if row["deleted_at"] is None:
        now = _now_naive()
        with transaction.atomic():
            _exec(
                "UPDATE messages SET text = '', image_url = NULL, attachment_url = NULL, attachment_name = NULL,"
                " attachment_size = NULL, attachment_mime = NULL, attachment_meta = NULL, link_preview = NULL,"
                " gallery = NULL, pinned_at = NULL, pinned_by = NULL, deleted_at = %s WHERE id = %s",
                (now, msg_id),
            )
            _exec("DELETE FROM message_reactions WHERE message_id = %s", (msg_id,))

        # The photo blob goes with the message — matched by
        # _is_local_upload_url, the SAME rule the send accepted
        # it under, so neither form can leave an orphan on disk
        poster = None
        try:
            poster = (json.loads(row["attachment_meta"]) or {}).get("thumbnailUrl") if row["attachment_meta"] else None
        except (TypeError, ValueError):
            poster = None
        preview_image = None
        try:
            preview_image = (json.loads(row["link_preview"]) or {}).get("imageUrl") if row["link_preview"] else None
        except (TypeError, ValueError):
            preview_image = None
        gallery_urls = []
        try:
            if row["gallery"]:
                gallery_urls = [item.get("url") for item in (json.loads(row["gallery"]) or []) if isinstance(item, dict)]
        except (TypeError, ValueError):
            gallery_urls = []
        for stored in (row["image_url"], row["attachment_url"], poster, preview_image, *gallery_urls):
            if not _is_local_upload_url(stored, request):
                continue
            try:
                from knfapp.uploads.storage import delete_upload
                delete_upload(stored)
            except Exception:
                logger.exception("Upload cleanup failed for unsent message")

        from knfapp.chat.events import emit_message_deleted
        emit_message_deleted(_get_sio(), conv_id, msg_id)

    return json_response({"ok": True})


@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_edit", max_attempts=100)
def edit_message(request, conv_id, msg_id):
    # STEP 1: the body — a non-blank, bounded string
    # ==============================================
    user_id = request.user["id"]
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400)
    raw_text = data.get("text")
    if not isinstance(raw_text, str):
        return json_error("Text must be a string", 400)
    text = raw_text.strip()
    if not text:
        return json_error("Message text required", 400)
    if len(text) > 5000:
        return json_error("Message text must not exceed 5000 characters", 400)


    # STEP 2: membership, the row, its owner, its state
    # =================================================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    row = _q1(
        "SELECT sender_id, deleted_at, kind FROM messages WHERE id = %s AND conversation_id = %s",
        (msg_id, conv_id),
    )
    if not row:
        return json_error("Message not found", 404)
    if row["sender_id"] != user_id:
        return json_error("Only the sender can edit a message", 403)
    if row["deleted_at"] is not None:
        return json_error("An unsent message cannot be edited", 409)
    if (row["kind"] or "text") not in ("text", "image"):
        return json_error("Only text messages can be edited", 400)


    # STEP 3: the rewrite, stamped, then the broadcast
    # ================================================
    now = _now_naive()
    with transaction.atomic():
        _exec("UPDATE messages SET text = %s, edited_at = %s WHERE id = %s", (text, now, msg_id))

    try:
        from knfapp.chat.events import emit_message_edited
        emit_message_edited(_get_sio(), conv_id, msg_id, text, now)
    except Exception:
        logger.exception("message_edited broadcast failed")

    return json_response({"id": msg_id, "text": text, "editedAt": now})








############################################################
# pin_message / get_pins / set_message_ttl
############################################################
#
# Any member may pin or unpin a message (a small room's
# etiquette is its own; the pinner's name is kept). The room
# hears 'message_updated' with {pinnedAt, pinnedBy}, the
# same patch door the link preview uses. Unsent and system
# rows cannot be pinned (404 / 400). get_pins lists the
# room's pinned messages, newest pin first, capped at 20 —
# shaped like a page row minus reactions/receipts.
#
# set_message_ttl: any member sets the room's disappearing-
# messages TTL — 0 or null switches it off, otherwise
# 60s..365d. Only messages sent AFTER the change carry an
# expires_at (history is never retroactively burned). The
# room narrates the change with a system row and every
# client hears 'conversation_updated' {messageTtlSeconds}.
#
# Used by:
#   - the mobile chat room — pin banner, room menu
############################################################

@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_pin", max_attempts=60)
def pin_message(request, conv_id, msg_id):
    # STEP 1: membership, the row, its state
    # ======================================
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    row = _q1(
        "SELECT deleted_at, kind FROM messages WHERE id = %s AND conversation_id = %s",
        (msg_id, conv_id),
    )
    if not row or row["deleted_at"] is not None:
        return json_error("Message not found", 404)
    if (row["kind"] or "text") == "system":
        return json_error("System messages cannot be pinned", 400)


    # STEP 2: flip the pin and tell the room
    # ======================================
    if request.method == "PUT":
        pinned_at = _now_naive()
        pinned_by = user_id
    else:
        pinned_at = None
        pinned_by = None
    with transaction.atomic():
        _exec("UPDATE messages SET pinned_at = %s, pinned_by = %s WHERE id = %s",
              (pinned_at, pinned_by, msg_id))

    from knfapp.chat.events import emit_message_updated
    emit_message_updated(_get_sio(), conv_id, msg_id, {"pinnedAt": pinned_at, "pinnedBy": pinned_by})
    return json_response({"pinnedAt": pinned_at, "pinnedBy": pinned_by})


@transaction.non_atomic_requests
@require_auth
def get_pins(request, conv_id):
    # STEP 1: membership gate
    # =======================
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)


    # STEP 2: the pinned rows, shaped lean
    # ====================================
    rows = _q(
        """SELECT m.id, m.text, m.image_url, m.created_at, m.client_msg_id, m.sender_id,
                  m.kind, m.edited_at, m.attachment_url, m.attachment_name, m.attachment_size, m.attachment_mime,
                  m.attachment_meta, m.link_preview, m.gallery, m.pinned_at, m.pinned_by, m.forwarded, m.expires_at,
                  m.deleted_at, u.display_name, u.avatar_url
           FROM messages m
           JOIN users u ON u.id = m.sender_id
           WHERE m.conversation_id = %s AND m.pinned_at IS NOT NULL AND m.deleted_at IS NULL
           ORDER BY m.pinned_at DESC
           LIMIT 20""",
        (conv_id,),
    )

    pins = []
    for row in rows:
        deleted = row["deleted_at"] is not None
        pins.append({
            "id": row["id"],
            "conversationId": conv_id,
            "senderId": row["sender_id"],
            "senderName": row["display_name"],
            "senderAvatar": row["avatar_url"],
            "text": row["text"] or "",
            "imageUrl": row["image_url"],
            "createdAt": row["created_at"],
            "clientMsgId": row["client_msg_id"],
            "reactions": [],
            "replyTo": None,
            "deleted": deleted,
            "kind": row["kind"] or "text",
            "editedAt": row["edited_at"],
            "attachment": _attachment_payload(row, deleted),
            "media": _media_payload(row, deleted),
            "gallery": _gallery_payload(row, deleted),
            "linkPreview": _link_preview_payload(row, deleted),
            "pinnedAt": row["pinned_at"],
            "pinnedBy": row["pinned_by"],
            "forwarded": bool(row["forwarded"]),
            "expiresAt": row["expires_at"],
        })
    return json_response({"pins": pins})


@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_ttl", max_attempts=30)
def set_message_ttl(request, conv_id):
    # STEP 1: the body — null/0 (off) or one bounded window
    # =====================================================
    user_id = request.user["id"]
    data = get_json_object(request)
    if data is None:
        return json_error("JSON body required", 400)
    seconds = data.get("seconds")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int)):
        return json_error("seconds must be an integer or null", 400)
    if seconds is not None and seconds != 0 and not 60 <= seconds <= 31_536_000:
        return json_error("seconds must be 0 (off) or between 60 and 31536000", 400)
    ttl = seconds or None


    # STEP 2: membership, the write, the narration
    # ============================================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    with transaction.atomic():
        _exec("UPDATE conversations SET message_ttl_seconds = %s WHERE id = %s", (ttl, conv_id))

        if ttl:
            if ttl < 3600:
                window = f"{ttl // 60} min."
            elif ttl < 86_400:
                window = f"{ttl // 3600} val."
            else:
                window = f"{ttl // 86_400} d."
            narration = f"{request.user['display_name']} įjungė nykstančias žinutes ({window})"
        else:
            narration = f"{request.user['display_name']} išjungė nykstančias žinutes"
        system_payload = _insert_system_message(conv_id, request.user, narration)

    from knfapp.chat.events import emit_conversation_updated, emit_new_message
    emit_new_message(_get_sio(), conv_id, system_payload)
    emit_conversation_updated(_get_sio(), conv_id, {"messageTtlSeconds": ttl})
    return json_response({"messageTtlSeconds": ttl})








############################################################
# react_to_message / remove_reaction
############################################################
#
# Sets or clears the caller's reaction — one emoji per user
# per message, so the set is a delete-then-insert sharing
# one transaction. Body {emoji}: one of the six
# _ALLOWED_REACTIONS the mobile picker offers — anything
# else is a 400. Members only (403); the message must
# belong to that conversation AND (for the set) still be
# un-unsent (404 — a chip must not resurrect on a
# placeholder bubble). Both return the authoritative
# post-write list, read INSIDE the write transaction so the
# broadcast snapshot matches this very write — a concurrent
# reaction cannot slip between commit and read — and
# broadcast as 'reaction_update' after it. No bySelf in
# either: the list goes to many clients, the mobile side
# derives it from byUserIds. The two share one 300-per-5-min
# reaction budget (429).
#
# Used by:
#   - services/api/chat.ts — reactToMessageApi /
#     removeReactionApi
############################################################

@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_react", max_attempts=300)
def react_to_message(request, conv_id, msg_id):
    user_id = request.user["id"]
    data = get_json_object(request)
    if not data or not data.get("emoji"):
        return json_error("emoji required", 400)

    if not isinstance(data["emoji"], str):
        return json_error("emoji must be a string", 400)

    # The server-side allowlist mirrors the mobile picker's
    # REACTION_OPTIONS — the only six values a client can send
    emoji = data["emoji"]
    if emoji not in _ALLOWED_REACTIONS:
        return json_error("emoji must be one of the supported reactions", 400)

    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    # The conversation id in the URL is what the membership
    # check trusted, so the message must really live there —
    # and still be un-unsent
    msg = _q1(
        "SELECT 1 AS x FROM messages WHERE id = %s AND conversation_id = %s AND deleted_at IS NULL",
        (msg_id, conv_id),
    )
    if not msg:
        return json_error("Message not found", 404)

    with transaction.atomic():
        # One emoji per user: replace, never accumulate.
        # created_at named — no DDL default on the Django table
        _exec("DELETE FROM message_reactions WHERE message_id = %s AND user_id = %s", (msg_id, user_id))
        _exec(
            "INSERT INTO message_reactions (message_id, user_id, emoji, created_at) VALUES (%s, %s, %s, %s)",
            (msg_id, user_id, emoji, _now_naive()),
        )

        # Snapshot INSIDE the transaction, broadcast after —
        # concurrent reactions can no longer slip a newer state
        # into an older broadcast
        reactions = _reactions_for([msg_id]).get(msg_id, [])

    from knfapp.chat.events import emit_reaction_update
    emit_reaction_update(_get_sio(), conv_id, msg_id, reactions)

    return json_response({"ok": True, "emoji": emoji, "reactions": reactions})


@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_react", max_attempts=300)
def remove_reaction(request, conv_id, msg_id):
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    # Same gates as react_to_message, so the returned list and
    # the broadcast can no longer leak who reacted to a message
    # the caller cannot see
    msg = _q1("SELECT 1 AS x FROM messages WHERE id = %s AND conversation_id = %s", (msg_id, conv_id))
    if not msg:
        return json_error("Message not found", 404)

    with transaction.atomic():
        _exec("DELETE FROM message_reactions WHERE message_id = %s AND user_id = %s", (msg_id, user_id))
        reactions = _reactions_for([msg_id]).get(msg_id, [])

    from knfapp.chat.events import emit_reaction_update
    emit_reaction_update(_get_sio(), conv_id, msg_id, reactions)

    return json_response({"ok": True, "reactions": reactions})








############################################################
# toggle_pin
############################################################
#
# PUT /api/chat/conversations/<id>/pin
#
# Flips the caller's own pinned flag on the membership row
# (pins are per user, not per conversation) and returns the
# new {pinned}. The flip is ONE atomic UPDATE (pinned =
# 1 - pinned), so two racing toggles land as two flips
# instead of one losing its read-modify-write; rowcount 0
# doubles as the membership gate (403). Pinned rows sort
# first in list_conversations.
#
# Used by:
#   - services/api/chat.ts — togglePinApi
############################################################

@transaction.non_atomic_requests
@require_auth
def toggle_pin(request, conv_id):
    user_id = request.user["id"]
    with transaction.atomic():
        # One atomic statement — no read-modify-write for a
        # concurrent toggle to race; 0 rows means non-member
        changed = _exec(
            "UPDATE conversation_participants SET pinned = 1 - pinned WHERE conversation_id = %s AND user_id = %s",
            (conv_id, user_id),
        )
        if changed == 0:
            return json_error("Not a participant", 403)

        # Re-read inside the same transaction for the response
        row = _q1(
            "SELECT pinned FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conv_id, user_id),
        )
    return json_response({"pinned": bool(row["pinned"])})








############################################################
# _watermark_regresses / _apply_mark_read / mark_read
############################################################
#
# _watermark_regresses: whether writing `now` over `prior`
# would move a reader's last_read_at BACKWARDS. The two
# stamps are PARSED rather than compared as text — raw
# comparison would let a legacy space-form row sort above
# every real stamp and freeze the pointer for good. A
# missing/blank/unparseable prior answers False, so the
# fresh stamp simply lands.
#
# _apply_mark_read is the ONE mark-read implementation both
# transports share — the REST route below and events.py
# handle_mark_read call it instead of carrying the logic
# twice. Inside a single hand-issued BEGIN IMMEDIATE on the
# autocommit connection: the membership lookup doubles as
# the gate AND yields the prior watermark (no TOCTOU
# window); the receipt SELECT is bounded to created_at in
# (prior, now] — unbounded below only when prior is NULL —
# capped at _MARK_READ_CAP newest rows; the receipts land
# as ONE set-based INSERT OR IGNORE; then last_read_at
# ADVANCES to `now` — a `now` older than the stamp already
# on the row is dropped, so two calls committing out of
# order can never un-read what the later one cleared.
# Returns the pre-selected id list for the frozen
# messages_read broadcast — None means "not a participant"
# (the REST edge answers 403, the socket edge drops
# silently).
#
# mark_read spends the SOCKET limiter's mark_read budget
# (10 per 10 s per user, 429 past it) so the REST path is
# not the free bypass around the socket quota; when at
# least one receipt was new, the reader id and message ids
# broadcast as 'messages_read'.
#
# Used by:
#   - services/api/chat.ts — markConversationRead
#   - chat/events.py — handle_mark_read, the socket twin
############################################################

def _watermark_regresses(prior, now):
    if not prior:
        return False

    try:
        return datetime.fromisoformat(prior) > datetime.fromisoformat(now)
    except (ValueError, TypeError):
        return False


def _apply_mark_read(conv_id, user_id, now):
    # STEP 1: BEGIN IMMEDIATE — gate and writes in ONE write
    # transaction; the membership row also carries the prior
    # watermark that bounds the receipt scan
    # ======================================================
    started = _begin_immediate()
    try:
        row = _q1(
            "SELECT last_read_at FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conv_id, user_id),
        )
        if not row:
            _end_immediate(started, commit=False)
            return None

        prior = row["last_read_at"]


        # STEP 2: the receipt candidates — foreign messages inside
        # (prior, now] without a receipt, newest first, capped so
        # one call can never scan a whole ancient history. `<= now`
        # keeps a message landing mid-call out of BOTH stores
        # ========================================================
        if prior:
            unread_msgs = _q(
                """
                SELECT m.id FROM messages m
                WHERE m.conversation_id = %s AND m.sender_id != %s
                  AND m.created_at > %s AND m.created_at <= %s
                  AND NOT EXISTS (
                      SELECT 1 FROM message_reads mr
                      WHERE mr.message_id = m.id AND mr.user_id = %s
                  )
                ORDER BY m.created_at DESC LIMIT %s
                """,
                (conv_id, user_id, prior, now, user_id, _MARK_READ_CAP),
            )
        else:
            unread_msgs = _q(
                """
                SELECT m.id FROM messages m
                WHERE m.conversation_id = %s AND m.sender_id != %s
                  AND m.created_at <= %s
                  AND NOT EXISTS (
                      SELECT 1 FROM message_reads mr
                      WHERE mr.message_id = m.id AND mr.user_id = %s
                  )
                ORDER BY m.created_at DESC LIMIT %s
                """,
                (conv_id, user_id, now, user_id, _MARK_READ_CAP),
            )

        newly_read_ids = [m["id"] for m in unread_msgs]


        # STEP 3: both stores in the same transaction — one
        # set-based insert for the receipts, then the pointer
        # that unreadCount and the tab badge read
        # ===================================================
        if newly_read_ids:
            placeholders = ",".join(["%s"] * len(newly_read_ids))
            _exec(
                f"""
                INSERT OR IGNORE INTO message_reads (message_id, user_id, read_at)
                SELECT m.id, %s, %s FROM messages m WHERE m.id IN ({placeholders})
                """,
                [user_id, now] + newly_read_ids,
            )

        # The pointer ADVANCES, it is never merely set: every call
        # takes its own `now` BEFORE the write lock, so the one
        # that started earlier can commit last, and a bare SET
        # would drag the watermark back over messages this reader
        # had already cleared. `prior` was read under this very
        # BEGIN IMMEDIATE, so nobody can move the row between the
        # test and the write
        if not _watermark_regresses(prior, now):
            _exec(
                "UPDATE conversation_participants SET last_read_at = %s WHERE conversation_id = %s AND user_id = %s",
                (now, conv_id, user_id),
            )

        _end_immediate(started, commit=True)
    except Exception:
        _end_immediate(started, commit=False)
        raise

    return newly_read_ids


@transaction.non_atomic_requests
@require_auth
def mark_read(request, conv_id):
    # STEP 1: ONE quota across both transports — the socket
    # limiter's own key, so socket + REST spend one budget
    # ====================================================
    user_id = request.user["id"]
    from knfapp.chat.events import _socket_rate_check
    if _socket_rate_check(user_id, "mark_read"):
        return json_error("Too many requests. Please slow down.", 429, code="rate_limited")


    # STEP 2: one `now` for both stores, then the shared
    # helper — None back means the caller is no member
    # ==================================================
    now = _now_naive()
    newly_read_ids = _apply_mark_read(conv_id, user_id, now)
    if newly_read_ids is None:
        return json_error("Not a participant", 403)


    # STEP 3: 'messages_read' so bubbles flip to delivered/
    # read live — skipped when nothing was new
    # =====================================================
    if newly_read_ids:
        from knfapp.chat.events import emit_read_receipt
        emit_read_receipt(_get_sio(), conv_id, user_id, newly_read_ids)

    return json_response({"ok": True, "readCount": len(newly_read_ids)})








############################################################
# leave_conversation / total_unread_count
############################################################
#
# The leave removes the caller's membership row — 404 for an
# unknown conversation, 403 for one the caller never joined.
# The leaver's own message_reads and message_reactions rows
# for the room go in the SAME transaction, so the remaining
# members' read/status math no longer counts a ghost reader;
# once nobody is left the messages, their reads and
# reactions and the conversation itself are purged too. The
# members who stay see who left (a group's narration); after
# the commit every socket of the leaver is evicted from room
# conv:<id> (best effort). The remaining members keep the
# history, with the leaver's messages still attributed.
#
# total_unread_count answers the tab badge: other people's
# messages newer than the caller's last_read_at, unsent ones
# excluded, over every conversation they belong to — one
# flat COUNT(*) join. Same definition as the per-row
# unreadCount, so the tab badge and the row badges agree;
# it does NOT consult message_reads.
#
# Used by:
#   - services/api/chat.ts — deleteConversationApi /
#     fetchTotalUnreadCount
############################################################

@transaction.non_atomic_requests
@require_auth
def leave_conversation(request, conv_id):
    # STEP 1: the gates — an unknown room is a 404, an
    # outsider a 403, and only a real member reaches the
    # delete below
    # ==================================================
    user_id = request.user["id"]
    if not _q1("SELECT 1 AS x FROM conversations WHERE id = %s", (conv_id,)):
        return json_error("Conversation not found", 404)

    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)


    # STEP 2: one transaction — the membership row AND the
    # leaver's receipts/reactions in this room; purge when
    # the last member is gone
    # ====================================================
    system_payload = None
    with transaction.atomic():
        _exec(
            "DELETE FROM conversation_participants WHERE conversation_id = %s AND user_id = %s",
            (conv_id, user_id),
        )
        _exec(
            "DELETE FROM message_reads WHERE user_id = %s AND message_id IN (SELECT id FROM messages WHERE conversation_id = %s)",
            (user_id, conv_id),
        )
        _exec(
            "DELETE FROM message_reactions WHERE user_id = %s AND message_id IN (SELECT id FROM messages WHERE conversation_id = %s)",
            (user_id, conv_id),
        )

        remaining = _q1(
            "SELECT COUNT(*) AS c FROM conversation_participants WHERE conversation_id = %s",
            (conv_id,),
        )["c"]

        if remaining == 0:
            _exec("DELETE FROM message_reads WHERE message_id IN (SELECT id FROM messages WHERE conversation_id = %s)", (conv_id,))
            _exec("DELETE FROM message_reactions WHERE message_id IN (SELECT id FROM messages WHERE conversation_id = %s)", (conv_id,))
            _exec("DELETE FROM messages WHERE conversation_id = %s", (conv_id,))
            _exec("DELETE FROM conversations WHERE id = %s", (conv_id,))
        else:
            # The members who stay see who left — a group's
            # narration; a direct chat's other half simply keeps
            # the conversation as it was
            conv_type_row = _q1("SELECT type FROM conversations WHERE id = %s", (conv_id,))
            if conv_type_row and conv_type_row["type"] == "group":
                system_payload = _insert_system_message(
                    conv_id, request.user, f"{request.user['display_name']} paliko pokalbį",
                )

    if system_payload:
        try:
            from knfapp.chat.events import emit_new_message
            emit_new_message(_get_sio(), conv_id, system_payload)
        except Exception:
            logger.exception("System message broadcast failed after leave")


    # STEP 3: evict every socket of the leaver from the room —
    # otherwise an ex-member keeps receiving live messages
    # until their next reconnect. Best effort: presence
    # plumbing must never fail the committed delete
    # ========================================================
    try:
        from knfapp.chat.events import _connected_users
        sio = _get_sio()
        room = f"conv:{conv_id}"
        # list() snapshot: connects/disconnects on other threads
        # mutate the dict while this loop runs
        for sid, uid in list(_connected_users.items()):
            if uid == user_id:
                sio.leave_room(sid, room)
    except Exception:
        logger.exception("Socket eviction failed after leave_conversation")

    return json_response({"ok": True})


@transaction.non_atomic_requests
@require_auth
def total_unread_count(request):
    user_id = request.user["id"]
    total = _q1(
        """
        SELECT COUNT(*) AS c
        FROM messages m
        JOIN conversation_participants cp
          ON cp.conversation_id = m.conversation_id AND cp.user_id = %s
        WHERE m.sender_id != %s
          AND m.deleted_at IS NULL
          AND m.created_at > COALESCE(cp.last_read_at, '1970-01-01T00:00:00')
        """,
        (user_id, user_id),
    )["c"]
    return json_response({"unreadCount": total})








############################################################
# search_messages
############################################################
#
# GET /api/chat/conversations/<id>/messages/search
#
# ?q (required, 400 when blank after strip and 400 over 200
# chars) and ?limit (default 20, clamped into 1..50,
# non-numeric → 400, parsed only after the membership gate).
# Members only (403). The match runs on the messages_fts
# FTS5 shadow table as a quoted prefix phrase — token-prefix
# semantics, proper word folding, no full scan — joined back
# to messages for the deleted_at filter; when FTS5 is
# missing from the build (or the query defeats the
# tokenizer) a LIKE '%q%' substring path answers
# instead, ASCII-only case folding and all, with q's \, %
# and _ escaped so they match literally. A q carrying a NUL
# byte is answered {messages: [], total: 0} without a query
# at all: the driver binds TEXT NUL-terminated, so the
# pattern would arrive truncated and match far more than was
# asked. Returns {messages, total}: the newest `limit` hits
# reversed to chronological order, plus the total so the UI
# can say "20 of 137" — the counter SATURATES at
# _SEARCH_TOTAL_CAP, so that value means "this many or
# more". Capped at 100 searches per 5 min per user (429).
#
# Used by:
#   - services/api/chat.ts — searchMessagesApi
############################################################

@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_msg_search", max_attempts=100)
def search_messages(request, conv_id):
    # STEP 1: q — a blank q is a 400 (unlike search_users),
    # and so is one no human would type
    # =====================================================
    user_id = request.user["id"]
    q = request.GET.get("q", "").strip()
    if len(q) < 1:
        return json_error("q parameter is required and must not be empty", 400)
    if len(q) > _SEARCH_Q_MAX:
        return json_error(f"q must be at most {_SEARCH_Q_MAX} characters", 400)


    # STEP 2: membership gate — 403 for outsiders, BEFORE any
    # other parameter is even parsed
    # =======================================================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    try:
        limit = int(request.GET.get("limit", 20))
    except (TypeError, ValueError):
        return json_error("limit must be an integer", 400)
    limit = max(1, min(limit, 50))

    # The driver binds TEXT NUL-TERMINATED, so a NUL in the
    # needle truncates the pattern INSIDE SQLite: the FTS
    # phrase loses its closing quote and the LIKE pattern
    # collapses to a bare '%' that answers the whole room. No
    # message body holds one, so the needle is answered as
    # what it is — a miss
    if "\x00" in q:
        return json_response({"messages": [], "total": 0})


    # STEP 3: the newest `limit` hits plus the saturating
    # total — FTS5 first: q rides as one quoted prefix phrase
    # ("..."*, inner quotes doubled), joined back to messages
    # for deleted_at. A build without FTS5 or a query the
    # tokenizer rejects raises OperationalError and falls back
    # to the escaped-LIKE substring scan
    # ======================================================
    rows = None
    total = 0
    fts_query = '"' + q.replace('"', '""') + '"*'
    try:
        rows = _q(
            """
            SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                   u.display_name AS sender_name, u.avatar_url AS sender_avatar
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            JOIN users u ON u.id = m.sender_id
            WHERE messages_fts MATCH %s AND m.conversation_id = %s AND m.deleted_at IS NULL
            ORDER BY m.created_at DESC
            LIMIT %s
            """,
            (fts_query, conv_id, limit),
        )

        # Counted over a capped subquery: the label needs
        # "many", not the exact number of hits in a decade of
        # history
        total = _q1(
            """
            SELECT COUNT(*) AS c FROM (
                SELECT 1 AS x
                FROM messages_fts
                JOIN messages m ON m.rowid = messages_fts.rowid
                WHERE messages_fts MATCH %s AND m.conversation_id = %s AND m.deleted_at IS NULL
                LIMIT %s
            )
            """,
            (fts_query, conv_id, _SEARCH_TOTAL_CAP),
        )["c"]
    except OperationalError:
        rows = None

    if rows is None:
        search_pattern = f"%{_escape_like(q)}%"
        rows = _q(
            r"""
            SELECT m.id, m.text, m.image_url, m.created_at, m.sender_id,
                   u.display_name AS sender_name, u.avatar_url AS sender_avatar
            FROM messages m
            JOIN users u ON u.id = m.sender_id
            WHERE m.conversation_id = %s AND m.deleted_at IS NULL AND m.text LIKE %s ESCAPE '\'
            ORDER BY m.created_at DESC
            LIMIT %s
            """,
            (conv_id, search_pattern, limit),
        )

        # Same saturating count as the FTS arm above
        total = _q1(
            r"""
            SELECT COUNT(*) AS c FROM (
                SELECT 1 AS x FROM messages
                WHERE conversation_id = %s AND deleted_at IS NULL AND text LIKE %s ESCAPE '\'
                LIMIT %s
            )
            """,
            (conv_id, search_pattern, _SEARCH_TOTAL_CAP),
        )["c"]


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

    return json_response({"messages": messages, "total": total})








############################################################
# online_status / search_users
############################################################
#
# online_status: body {userIds[]} → {online: {id: bool}} —
# whether each id currently has a socket in THIS process,
# but ONLY for users who share at least one conversation
# with the caller. Everyone else answers false, exactly like
# a genuinely offline user, so the route is not a free
# live-presence oracle over arbitrary ids. Silently
# truncated to the first 200 ids; non-string ids dropped.
#
# search_users: ?q substring match on username OR
# display_name (LIKE: ASCII-only case folding; escaped so
# \, % and _ match literally), excluding the caller, every
# DEACTIVATED account and both directions of a block pair.
# The 20 rows are RANKED: an exact username hit first, then
# a display name starting with q, then the rest, each tier
# by display name (NOCASE) with the id as the tiebreaker.
# Every call spends the 120-per-5-min budget — that is what
# actually stops directory enumeration; anything under 2
# chars (or carrying a NUL byte, which would collapse the
# pattern to '%') answers {users: []} with 200 so the
# picker can call it on every keystroke. Returns id,
# username, displayName, avatarUrl, role — no email;
# username and role stay because the mobile SearchUserResult
# types both as required (frozen contract).
#
# Used by:
#   - services/api/chat.ts — fetchOnlineStatus /
#     searchUsersApi
############################################################

@transaction.non_atomic_requests
@require_auth
def online_status(request):
    # STEP 1: body — a userIds array; truncated silently
    # rather than a 400, since the list screen sends whatever
    # it has on screen. Non-string ids are dropped
    # ======================================================
    user_id = request.user["id"]
    data = get_json_object(request)
    if not data or not isinstance(data.get("userIds"), list):
        return json_error("userIds array required", 400)

    user_ids = [uid for uid in data["userIds"] if isinstance(uid, str)]
    if len(user_ids) > 200:
        user_ids = user_ids[:200]


    # STEP 2: the relationship gate — presence is only
    # revealed for users sharing at least one conversation
    # with the caller; a stranger's id probes exactly nothing
    # ======================================================
    shared = set()
    if user_ids:
        placeholders = ",".join(["%s"] * len(user_ids))
        rows = _q(
            f"""
            SELECT DISTINCT cp2.user_id
            FROM conversation_participants cp1
            JOIN conversation_participants cp2 ON cp2.conversation_id = cp1.conversation_id
            WHERE cp1.user_id = %s AND cp2.user_id IN ({placeholders})
            """,
            [user_id] + user_ids,
        )
        shared = {r["user_id"] for r in rows}


    # STEP 3: presence is this process's socket table; an
    # import failure reads as everybody offline, never as an
    # error
    # =====================================================
    try:
        from knfapp.chat.events import _connected_users
        online_set = set(_connected_users.values())
    except Exception:
        online_set = set()

    result = {uid: (uid in shared and uid in online_set) for uid in user_ids}
    return json_response({"online": result})


@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_user_search", max_attempts=120)
def search_users(request):
    # Under 2 chars is the keystroke warm-up, not a search —
    # answered empty with no directory hit at all. A NUL byte
    # goes out the same door: bound NUL-terminated it would
    # collapse the pattern to '%' and page the WHOLE directory,
    # precisely the enumeration this gate exists to stop
    user_id = request.user["id"]
    q = request.GET.get("q", "").strip()
    if len(q) < 2 or "\x00" in q:
        return json_response({"users": []})

    search_pattern = f"%{_escape_like(q)}%"
    prefix_pattern = f"{_escape_like(q)}%"
    # COLLATE NOCASE is SQLite spelling — rewrites with the rest
    # of the raw SQL on a postgres move
    rows = _q(
        r"""
        SELECT id, username, display_name, avatar_url, role
        FROM users
        WHERE id != %s AND active = 1 AND (
            username LIKE %s ESCAPE '\' OR display_name LIKE %s ESCAPE '\'
        )
          AND id NOT IN (SELECT blocked_id FROM user_blocks WHERE blocker_id = %s)
          AND id NOT IN (SELECT blocker_id FROM user_blocks WHERE blocked_id = %s)
        ORDER BY
            CASE
                WHEN username = %s COLLATE NOCASE THEN 0
                WHEN display_name LIKE %s ESCAPE '\' THEN 1
                ELSE 2
            END,
            display_name COLLATE NOCASE,
            id
        LIMIT 20
        """,
        (user_id, search_pattern, search_pattern, user_id, user_id, q, prefix_pattern),
    )

    return json_response({
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
