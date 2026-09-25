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
#    - Wire stamps are naive UTC (no offset, microseconds):
#      json_response below opts into the naive-stamp
#      encoder — policy in common/http.py +
#      common/timestamps.py.
#    - Two independent read-state stores: the membership
#      row's last_read_at drives unreadCount and the tab
#      badge; per-message message_reads rows drive status
#      and readBy. send_message and mark_read write both,
#      through the one helper (_apply_mark_read).
#    - Disappearing messages are a sweep AND a predicate:
#      the room views hard-delete overdue rows on entry
#      (_sweep_expired), and the surfaces read without a
#      sweep (the conversation-list preview, the pin
#      banner, the in-room search) filter on expires_at
#      themselves, so an expired row is unreadable from
#      the instant it lapses, not from the next sweep.
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
#  transaction. The one place that must take the write
#  lock up front (_apply_mark_read) opens a hand
#  transaction on the autocommit connection — BEGIN
#  IMMEDIATE on SQLite, a plain BEGIN elsewhere
#  (_begin_immediate picks); create_conversation's direct
#  dedup leans on conversations.direct_key UNIQUE instead,
#  which both engines enforce.
#
#  Row traffic goes through the ORM. Raw SQL only where it
#  is load-bearing: mark_read's hand-issued write-lock
#  transaction and the statements inside it, the
#  last-message seek and the unread aggregates, and the
#  TTL/purge DELETEs that must not cascade.
############################################################


import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from django.db import IntegrityError, connection, transaction
from django.db.models import Case, F, Q, When
from django.db.models.functions import Lower

from knfapp.chat.models import (Conversation, ConversationParticipant, Message,
                                MessageReaction, MessageRead)
from knfapp.common import ratelimit
from knfapp.common.db import execute as _exec, q as _q, q1 as _q1
from knfapp.common.http import clean_param, get_json_object, json_error, require_methods
from knfapp.common.http import json_response as _json_response
from knfapp.common.timestamps import as_aware, utc_now
from knfapp.social.models import UserBlock
from knfapp.users.auth import require_auth
from knfapp.users.models import User

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

# How far BEFORE the request a change-feed cursor is stamped.
# The writers stamp edited_at / deleted_at and only then
# commit, so a cursor read at "now" could sit past a write
# that commits after the page's SELECT — that edit would be
# neither on the page nor in any later feed. Backdating the
# cursor re-delivers the last few seconds of changes instead
# (the client applies a change idempotently); a write
# transaction open longer than this is not one chat has
_CHANGES_CURSOR_SLACK = timedelta(seconds=10)


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


def json_response(payload, status=200):
    # The one place chat's naive-UTC wire shape is decided
    return _json_response(payload, status=status, naive_stamps=True)


def _bind(stamp):
    # Raw parameters ride the DRIVER's adapter, which keeps an
    # aware stamp's offset in SQLite's stored text; the ORM's
    # adapter writes the column's uniform naive-UTC form —
    # every raw datetime bind goes through it
    return connection.ops.adapt_datetimefield_value(stamp)


def _is_member(conv_id, user_id):
    return ConversationParticipant.objects.filter(
        conversation_id=conv_id, user_id=user_id,
    ).exists()


def _begin_immediate():
    # The write lock, taken up front — but ONLY on the
    # autocommit connection production runs on. Inside an
    # enclosing atomic block (the test harness wraps every test
    # in one) a nested BEGIN would raise, and the COMMIT below
    # would commit the harness's transaction — so there the
    # enclosing block is the serialisation and this is a no-op.
    # BEGIN IMMEDIATE is the SQLite spelling that reserves the
    # write lock at open; other engines take a plain BEGIN and
    # lean on their row-level locking instead.
    # Returns whether a transaction was actually opened
    if connection.in_atomic_block:
        return False
    with connection.cursor() as cursor:
        cursor.execute("BEGIN IMMEDIATE" if connection.vendor == "sqlite" else "BEGIN")
    return True


def _end_immediate(started, commit):
    if not started:
        return
    with connection.cursor() as cursor:
        cursor.execute("COMMIT" if commit else "ROLLBACK")








############################################################
# _format_time / _epoch_ms
############################################################
#
# _format_time: HH:MM of the stamp in UTC, NOT Lithuanian
# time — which is why clients ignore `time` and format
# createdAt themselves. _epoch_ms pins the stamp to
# timezone.utc before .timestamp(), so the number is right
# whatever /etc/localtime says. Both fail soft ("" / 0), so
# ONE bad row can never 500 a whole listing.
############################################################

def _format_time(value):
    dt = as_aware(value)
    return dt.strftime("%H:%M") if dt else ""


def _epoch_ms(value):
    dt = as_aware(value)
    return int(dt.timestamp() * 1000) if dt else 0








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
# This is a SHAPE test, not an ownership test — on purpose.
# The mobile client forwards a message by re-sending the
# original sender's upload urls verbatim (packages/
# chatengine/src/core/forward.ts), so a send-time owner
# check would 400 every forward. Ownership is enforced on
# the delete side instead: delete_message and
# _sweep_expired hand each url to storage.delete_upload AS
# THE SENDER, and the sink refuses a file the sender never
# owned or one another live message still shows.
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
    gallery = row.get("reply_gallery")
    if not reply_image and isinstance(gallery, list) and gallery:
        first = gallery[0]
        reply_image = first.get("url") if isinstance(first, dict) else None
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
# The room narrating itself. A 'system' row is stored like
# any message (the actor is its sender, so every JOIN keeps
# working) and the returned payload is what
# emit_new_message broadcasts; the client renders kind
# 'system' as a centred caption.
#
# The row carries TWO forms of the same line: `event`, a
# code plus parameters — {"event": "group_created",
# "title": …}, {"event": "left"}, {"event": "ttl_on",
# "seconds": …}, {"event": "ttl_off"} — kept in
# attachment_meta.system (a system row has no media, and a
# JSON column needs no migration), and `text`, the
# Lithuanian sentence. Clients render the event through
# their own catalog with the sender as the actor, so an
# English reader never sees a Lithuanian line; the prose
# stays only as the fallback for rows written before events
# existed and for clients that do not know an event yet.
# Every wire shape ships the event as `system`
# (_system_payload). A system line is never UNREAD — both
# unread aggregates skip kind 'system' — and a caller that
# has just stamped watermarks passes `at` so the line shares
# that instant. Bumps the conversation so the room sorts to
# the top of the list. kind, forwarded and created_at are
# stamped explicitly — the table carries no DDL defaults.
############################################################

def _insert_system_message(conv_id, actor, text, event=None, at=None):
    msg_id = str(uuid.uuid4())
    now = at or utc_now()
    Message.objects.create(
        id=msg_id, conversation_id=conv_id, sender_id=actor["id"], text=text,
        kind="system", forwarded=False, created_at=now,
        attachment_meta={"system": event} if event else None,
    )
    Conversation.objects.filter(id=conv_id).update(updated_at=now)
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
        "system": event,
    }








############################################################
# _attachment_payload / _media_payload /
# _link_preview_payload / _gallery_payload /
# _system_payload
############################################################
#
# The optional frames of the richer message shapes: the
# `attachment` object of a 'file' message, the media frame
# of a photo/video, the unfurled card of the first URL, the
# photo list of a multi-photo row, and the event behind a
# 'system' row. All None for other rows, for unsent ones
# (the blanking clears the columns; these mirror it on the
# wire) and for anything that is not the shape its writer
# stores. A system row keeps its event in attachment_meta
# (see _insert_system_message) — it never has a media frame,
# so _media_payload skips it instead of shipping an all-null
# frame the client would read as a photo's.
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


def _system_payload(row):
    if (row.get("kind") or "text") != "system":
        return None
    meta = row.get("attachment_meta")
    event = meta.get("system") if isinstance(meta, dict) else None
    return event if isinstance(event, dict) and isinstance(event.get("event"), str) else None


def _media_payload(row, deleted=False):
    meta = row.get("attachment_meta")
    if deleted or not isinstance(meta, dict) or (row.get("kind") or "text") == "system":
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
    card = row.get("link_preview")
    if deleted or not isinstance(card, dict) or not card.get("url"):
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
    items = row.get("gallery")
    if deleted or not isinstance(items, list) or not items:
        return None
    return [
        {"url": item.get("url"), "width": item.get("width"), "height": item.get("height"), "preview": item.get("preview")}
        for item in items
        if isinstance(item, dict) and item.get("url")
    ] or None








############################################################
# _stored_upload_urls
############################################################
#
# Every stored-file slot of one message row, in the order
# the deletion paths walk them: the photo, the attachment,
# the video poster (attachment_meta.thumbnailUrl), the link
# card's picture and every gallery photo. `row` is a
# values() dict carrying those five columns; slots that are
# empty or not the shape their writer stores are skipped,
# and only this server's /api/uploads/ files are answered —
# the SAME rule (_is_local_upload_url) the send accepted
# them under, so no form the send takes can be one a delete
# leaves behind. ONE walker on purpose: the last-member
# purge once dropped a whole room's messages without it and
# orphaned every file they held.
#
# Used by:
#   - _sweep_expired (below) — disappearing messages
#   - delete_message — the unsend
#   - leave_conversation — the last member's purge
############################################################

def _stored_upload_urls(row, request=None):
    stored_urls = [row.get("image_url"), row.get("attachment_url")]
    meta = row.get("attachment_meta")
    if isinstance(meta, dict):
        stored_urls.append(meta.get("thumbnailUrl"))
    card = row.get("link_preview")
    if isinstance(card, dict):
        stored_urls.append(card.get("imageUrl"))
    gallery = row.get("gallery")
    if isinstance(gallery, list):
        stored_urls.extend(item.get("url") for item in gallery if isinstance(item, dict))
    return [stored for stored in stored_urls if _is_local_upload_url(stored, request)]








############################################################
# _sweep_expired / _delete_message_uploads
############################################################
#
# Disappearing messages: hard-deletes this conversation's
# rows whose expires_at has passed — the reaction and
# receipt rows, then the messages, in one atomic block —
# and hands their files (_stored_upload_urls) to the
# uploads sink ON THE COMMIT, as the sender: the sink
# refuses a file the sender never owned (a forwarded copy
# of somebody else's photo) and one another live message
# still shows, and because the expired rows are gone by the
# time the callback runs, they cannot hold their own files
# back. Called opportunistically from get_messages,
# send_message and search_messages (autocommit views — the
# callback runs right after the block commits; the partial
# index idx_messages_expires makes the lookup a no-op for a
# room without a TTL). The surfaces that read WITHOUT a
# sweep — list_conversations' preview seek, get_pins —
# filter on expires_at instead, and the room's clients drop
# expired rows by their own clocks too, so a row that slips
# into a page between sweeps still vanishes on screen. The
# daily maintenance command runs the same sweep for rooms
# nobody reopens — no transaction there, so on_commit runs
# at once. A file that will not go is logged, never raised:
# the rows stay gone either way. _delete_message_uploads is
# that on-commit half, shared with the last-member purge.
############################################################

def _sweep_expired(conv_id, request=None):
    # STEP 1: the overdue rows and every upload slot they carry,
    # each paired with the sender the delete acts for
    # ==========================================================
    now = datetime.now(timezone.utc)
    rows = list(Message.objects.filter(
        conversation_id=conv_id, expires_at__isnull=False, expires_at__lte=now,
    ).values("id", "sender_id", "image_url", "attachment_url", "attachment_meta", "link_preview", "gallery"))
    if not rows:
        return

    doomed = []
    for row in rows:
        doomed.extend((stored, row["sender_id"]) for stored in _stored_upload_urls(row, request))


    # STEP 2: the rows first — the files only once these are gone,
    # so the reference guard never sees the expiring message itself
    # =============================================================
    ids = [row["id"] for row in rows]
    marks = ",".join(["%s"] * len(ids))
    with transaction.atomic():
        MessageReaction.objects.filter(message_id__in=ids).delete()
        MessageRead.objects.filter(message_id__in=ids).delete()
        # Raw on purpose: the ORM delete would run the
        # reply-quote SET_NULL cascade and erase the ghost-quote
        # wire shape the read path relies on
        _exec(f"DELETE FROM messages WHERE id IN ({marks})", ids)

    if doomed:
        transaction.on_commit(lambda: _delete_message_uploads(doomed))


def _delete_message_uploads(doomed):
    from knfapp.uploads.storage import delete_upload
    for stored, sender_id in doomed:
        try:
            delete_upload(stored, sender_id)
        except Exception:
            logger.exception("Upload cleanup failed for a deleted message")








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
    row = Message.objects.filter(
        conversation_id=conv_id, sender_id=user_id, client_msg_id=client_msg_id,
    ).values(
        "id", "text", "image_url", "created_at", "client_msg_id",
        "reply_to_id", "deleted_at",
        "kind", "edited_at", "attachment_url", "attachment_name", "attachment_size",
        "attachment_mime", "attachment_meta", "link_preview", "gallery",
        "pinned_at", "pinned_by", "forwarded", "expires_at",
        reply_sender_id=F("reply_to__sender_id"), reply_text=F("reply_to__text"),
        reply_image_url=F("reply_to__image_url"), reply_gallery=F("reply_to__gallery"),
        reply_deleted_at=F("reply_to__deleted_at"), reply_kind=F("reply_to__kind"),
        reply_file_name=F("reply_to__attachment_name"),
        reply_sender_name=F("reply_to__sender__display_name"),
    ).first()
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

    # Ordered by the reaction stamp on purpose — the group and
    # byUserIds order must not depend on which index the
    # planner walks
    rows = MessageReaction.objects.filter(message_id__in=list(msg_ids)) \
        .order_by("message_id", "created_at").values("message_id", "emoji", "user_id")

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
# _receipts_for / _own_status
############################################################
#
# The read-receipt half of a message row, shared by every
# route that ships full rows (the history page AND the
# change feed — the feed once hard-coded status "read",
# readBy [] and reactions [], so a resync flipped the
# sender's unread message to the read tick and wiped real
# receipts off the screen).
#
# _receipts_for: {message_id: [reader ids]} for a whole id
# list in one IN (...) query, ordered by the receipt stamp
# so readBy never depends on which index the planner walks.
#
# _own_status: the delivery ladder of the CALLER'S own
# message — "read" needs a receipt from every other member,
# "delivered" from at least one, a room with no other
# member is trivially read; the sender's own receipt never
# counts. Everybody else's message is simply "read" (status
# only means something on own rows).
#
# Used by:
#   - get_messages / get_changes (below)
############################################################

def _receipts_for(msg_ids):
    read_map = {}
    if not msg_ids:
        return read_map
    for rd in MessageRead.objects.filter(message_id__in=list(msg_ids)) \
            .order_by("message_id", "read_at").values("message_id", "user_id"):
        read_map.setdefault(rd["message_id"], []).append(rd["user_id"])
    return read_map


def _own_status(is_own, read_by, user_id, participant_count):
    if not is_own:
        return "read"
    other_readers = [uid for uid in read_by if uid != user_id]
    others_count = participant_count - 1
    if others_count <= 0 or len(other_readers) >= others_count:
        return "read"
    if other_readers:
        return "delivered"
    return "sent"








############################################################
# _push_chat_message
############################################################
#
# The chat push fan-out, run OFF the request thread — the
# send answers 201 without waiting on Expo's HTTP
# round-trip. Goes straight through the batched
# notify_channel_users (one query, one Expo batch per
# language, "chat" opt-outs honoured in SQL). title/body
# are the Lithuanian copy; title_en/body_en the English one
# for devices registered as 'en' — passed whenever the
# BACKEND composed the words (a media marker, the
# content-free body, the mention title), left None where
# the words are the sender's own (their name, their text),
# which reads the same in any language. No request context
# in here — everything arrives as arguments, and every
# failure is logged and swallowed (push never owes anybody
# an error). The thread's DB connection is closed on the
# way out.
############################################################

def _push_chat_message(recipient_ids, title, body, data, title_en=None, body_en=None):
    try:
        from knfapp.notifications.push import notify_channel_users
        notify_channel_users("chat", recipient_ids, title, body, data=data,
                             title_en=title_en, body_en=body_en)
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
# then newest activity, with participants, the last LIVE
# message and an unread count per row — FOUR queries for
# the whole tab (the memberships, then participants, last
# messages and unread counts set-based over the id list),
# plus a fifth only when some room's newest row is a
# system line (its event, for a localized preview).
# A disappearing message past its expires_at never
# previews: the seek skips it and lands on the previous
# live message, so the row reads exactly as it will after
# the sweep instead of showing a lapsed body or going blank.
# unreadCount is other people's messages newer than the
# caller's last_read_at; a NULL last_read_at counts
# everything, and unsent messages and system lines never
# count. A direct
# chat without a title is named after the other
# participant; when nobody else is (left) in it the
# title stays null and the client renders its localized
# fallback. lastUpdatedMs is _epoch_ms of updated_at — 0
# rather than a 500 on an unparseable stamp, falling back
# to created_at.
#
# Used by:
#   - services/api/chat.ts — fetchConversations
############################################################

@require_methods("GET")
@transaction.non_atomic_requests
@require_auth
def list_conversations(request):
    # STEP 1: the caller's memberships — pinned first, then
    # newest activity; the id list drives everything below
    # =====================================================
    user_id = request.user["id"]
    rows = list(
        ConversationParticipant.objects.filter(user_id=user_id)
        .order_by("-pinned", "-conversation__updated_at")
        .values(
            "pinned", "last_read_at",
            id=F("conversation__id"), type=F("conversation__type"),
            title=F("conversation__title"), avatar_emoji=F("conversation__avatar_emoji"),
            created_at=F("conversation__created_at"), updated_at=F("conversation__updated_at"),
        )
    )

    conv_ids = [row["id"] for row in rows]


    # STEP 2: participants, last message and unread count for
    # the WHOLE tab in three set-based queries keyed off that
    # id list — never three queries per row
    # =======================================================
    participants_map = {}
    last_msg_map = {}
    unread_map = {}
    system_events = {}
    if conv_ids:
        placeholders = ",".join(["%s"] * len(conv_ids))

        for p in ConversationParticipant.objects.filter(conversation_id__in=conv_ids).values(
            "conversation_id",
            id=F("user__id"), display_name=F("user__display_name"), avatar_url=F("user__avatar_url"),
        ):
            participants_map.setdefault(p["conversation_id"], []).append(p)

        # One descending index seek per room picks its newest
        # LIVE message (the id tiebreak keeps the pick
        # deterministic when two stamps match to the
        # microsecond) — a window function over the same rows
        # would visit EVERY message of every listed room, and
        # this is the app-open query. The expiry predicate sits
        # INSIDE the correlated seek on purpose: filtering the
        # picked row afterwards would blank the preview of a
        # room whose newest row lapsed, instead of falling back
        # to the previous live one
        now = utc_now()
        for m in _q(
            f"""
            SELECT m.conversation_id, m.id, m.text, m.image_url, m.kind, m.created_at,
                   m.sender_id, m.deleted_at, u.display_name AS sender_name
            FROM conversations c
            JOIN messages m ON m.id = (SELECT m2.id FROM messages m2
                                       WHERE m2.conversation_id = c.id
                                         AND (m2.expires_at IS NULL OR m2.expires_at > %s)
                                       ORDER BY m2.created_at DESC, m2.id DESC LIMIT 1)
            JOIN users u ON u.id = m.sender_id
            WHERE c.id IN ({placeholders})
            """,
            [_bind(now)] + conv_ids,
        ):
            last_msg_map[m["conversation_id"]] = m

        # A system row previews through its event (the reader's
        # own language) — read through the ORM, which decodes the
        # JSON column alike on both engines; only rooms whose
        # newest row IS a system line pay for this query
        system_ids = [m["id"] for m in last_msg_map.values() if m["kind"] == "system"]
        if system_ids:
            for row in Message.objects.filter(id__in=system_ids).values("id", "kind", "attachment_meta"):
                system_events[row["id"]] = _system_payload(row)

        # One GROUP BY, the same definition total_unread_count
        # uses: a NULL last_read_at must count every message,
        # hence the epoch floor; unsent messages are out — the
        # badge must agree with what the reader can still read —
        # and so are system lines ("X left", "X created the
        # group"): the room narrating itself is nobody's unread
        for cnt in _q(
            f"""
            SELECT m.conversation_id, COUNT(*) AS unread
            FROM messages m
            JOIN conversation_participants cp
              ON cp.conversation_id = m.conversation_id AND cp.user_id = %s
            WHERE m.conversation_id IN ({placeholders})
              AND m.sender_id != %s
              AND m.deleted_at IS NULL
              AND m.kind != 'system'
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
                # A system line's event — the client words the
                # preview in the reader's language (None otherwise)
                "system": system_events.get(last_msg["id"]),
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
# is always added; a member set that reduces to the caller
# ALONE is a 400, and a direct chat must resolve to exactly
# two members (400). An existing two-person DM answers 200
# with its id instead of a fresh 201 — a racing
# double-create is settled by the direct_key UNIQUE (STEP
# 5). Every id must exist in users AND still be active
# (400), and no member may be in a block pair with the
# CREATOR (either direction) — a flat 403 without naming
# who blocked whom. Capped at 50 creates per 5 min per user
# (429). Members start with last_read_at = now, so the new
# chat opens with unreadCount 0.
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

@require_methods("POST")
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
    found = User.objects.filter(id__in=all_ids, active=1).count()
    if found != len(all_ids):
        return json_error("One or more participant IDs are invalid", 400)


    # STEP 3.1: no member may be in a block pair with the
    # creator, either direction — one flat 403 that does not
    # say who blocked whom (the block's existence is not the
    # creator's business, only its effect is)
    # =====================================================
    other_ids = [uid for uid in all_ids if uid != user_id]
    blocked_pair = UserBlock.objects.filter(
        Q(blocker_id=user_id, blocked_id__in=other_ids)
        | Q(blocked_id=user_id, blocker_id__in=other_ids),
    ).exists()
    if blocked_pair:
        return json_error("One or more participants cannot be added", 403)


    # STEP 4: a direct chat between two people is reused —
    # the existing id answers with 200, not 201. This is the
    # cheap fast path; the direct_key UNIQUE below settles
    # the race the fast path can miss
    # =====================================================
    other_id = None
    if conv_type == "direct":
        other_id = [uid for uid in all_ids if uid != user_id][0]
        existing = _find_direct_conversation(user_id, other_id)
        if existing:
            return json_response({"conversationId": existing}, status=200)


    # STEP 5: the conversation and its members in ONE atomic
    # block. A direct room carries direct_key — the UNIQUE the
    # database enforces — so the loser of a racing
    # double-submit lands in the except arm, finds its twin's
    # committed row and answers 200 instead of inserting a
    # second DM. last_read_at = now so the chat opens with
    # unreadCount 0 for everybody
    # =======================================================
    conv_id = str(uuid.uuid4())
    now = utc_now()
    direct_key = "|".join(sorted((user_id, other_id))) if conv_type == "direct" else None

    try:
        with transaction.atomic():
            Conversation.objects.create(
                id=conv_id, type=conv_type, title=title, avatar_emoji=avatar_emoji,
                created_by_id=user_id, direct_key=direct_key,
                created_at=now, updated_at=now,
            )

            ConversationParticipant.objects.bulk_create([
                ConversationParticipant(conversation_id=conv_id, user_id=uid,
                                        pinned=0, last_read_at=now, joined_at=now)
                for uid in all_ids
            ])

            # A group opens with its own first line — who made it and
            # what it is called — so the room never starts blank
            system_payload = None
            if conv_type == "group":
                # Stamped with the SAME `now` as every member's
                # last_read_at — a later stamp read as one unread
                # message for everybody the creator invited
                group_title = (title or "").strip()
                system_payload = _insert_system_message(
                    conv_id, request.user,
                    f"{request.user['display_name']} sukūrė grupę „{group_title}“",
                    event={"event": "group_created", "title": group_title}, at=now,
                )
    except IntegrityError:
        # Caught OUTSIDE the atomic block (the failed statement
        # poisons it on postgres). Only direct_key can fire on a
        # valid body — the racing twin's row answers 200; any
        # other integrity failure propagates as a 500
        existing = _find_direct_conversation(user_id, other_id) if conv_type == "direct" else None
        if existing:
            return json_response({"conversationId": existing}, status=200)
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
# _history_row / _page_rows / get_messages / get_changes
############################################################
#
# One history page, fetched newest-first and reversed to
# chronological order. ?limit (default 50, cap 100) and
# three cursor windows sharing one SELECT (_page_rows):
#   ?before[&before_id]  strictly older than the (stamp, id)
#                        composite cursor — the id breaks
#                        stamp ties so equal-stamp siblings
#                        never fall through a page boundary;
#                        without before_id the id arm is
#                        omitted and the window degrades to
#                        the bare-stamp cut
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
# replyTo, clientMsgId, deleted (unsent — content blanked),
# the system event of a 'system' row and, for the caller's
# OWN messages, status derived from how many OTHER members
# hold a receipt (_own_status). The envelope also ships
# participants (sorted by display name) and the
# conversation row itself, so a room opened from a push
# notification draws its header without a second call, plus
# the server-clock cursor the change feed resumes from.
#
# get_changes (?since=<iso>) answers every message edited or
# unsent after that moment as full rows — status, readBy
# and reactions computed exactly as the page computes them,
# never placeholders — the resync door for edits/unsends
# outside the client's newest page. 400 on a malformed
# since; at most 500 rows.
#
# Both cursors are stamped FIRST, before a single row is
# read, and backdated by _CHANGES_CURSOR_SLACK: a write that
# commits while the page is being read lands in the next
# feed instead of falling between the two. The price is a
# few seconds of re-delivered changes, which the client
# applies idempotently.
#
# Used by:
#   - packages/chatengine/src/adapters/knf/rest.ts —
#     fetchMessages (the room's first page, older pages, the
#     around/after windows of a jump) and fetchChanges (the
#     resync after a reconnect)
############################################################

def _history_row(row, conv_id, user_id, reaction_map, read_map, participant_count):
    # One wire row of a history page or change feed. An unsent
    # message keeps its slot but ships no content
    msg_id = row["id"]
    is_own = row["sender_id"] == user_id
    read_by = read_map.get(msg_id, [])
    deleted = row["deleted_at"] is not None
    return {
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
        "status": _own_status(is_own, read_by, user_id, participant_count),
        "readBy": read_by,
        "reactions": reaction_map.get(msg_id, []),
        "replyTo": _reply_payload(row),
        "deleted": deleted,
        "kind": row["kind"] or "text",
        "editedAt": row["edited_at"],
        "attachment": _attachment_payload(row, deleted),
        "media": _media_payload(row, deleted),
        "linkPreview": _link_preview_payload(row, deleted),
        "gallery": _gallery_payload(row, deleted),
        "system": _system_payload(row),
        "pinnedAt": row.get("pinned_at"),
        "pinnedBy": row.get("pinned_by"),
        "forwarded": bool(row.get("forwarded")),
        "expiresAt": row.get("expires_at"),
    }


def _page_rows(window, order, limit):
    ordering = ("-created_at", "-id") if order == "DESC" else ("created_at", "id")
    return list(
        Message.objects.filter(window)
        .order_by(*ordering)
        .values(
            "id", "text", "image_url", "created_at", "sender_id", "client_msg_id",
            "kind", "edited_at", "attachment_url", "attachment_name", "attachment_size",
            "attachment_mime", "attachment_meta", "link_preview", "gallery",
            "pinned_at", "pinned_by", "forwarded", "expires_at",
            "reply_to_id", "deleted_at",
            sender_name=F("sender__display_name"), sender_avatar=F("sender__avatar_url"),
            reply_sender_id=F("reply_to__sender_id"), reply_text=F("reply_to__text"),
            reply_image_url=F("reply_to__image_url"), reply_gallery=F("reply_to__gallery"),
            reply_deleted_at=F("reply_to__deleted_at"), reply_kind=F("reply_to__kind"),
            reply_file_name=F("reply_to__attachment_name"),
            reply_sender_name=F("reply_to__sender__display_name"),
        )[:limit]
    )


@require_methods("GET")
@transaction.non_atomic_requests
@require_auth
def get_messages(request, conv_id):
    # STEP 1: the change-feed cursor, before anything is read
    # (see the banner), then the membership gate — outsiders
    # get 403 before any message row is read
    # ======================================================
    cursor = utc_now() - _CHANGES_CURSOR_SLACK
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    # Disappearing messages leave before the page is read
    _sweep_expired(conv_id, request)


    # STEP 2: the page — see the banner for the windows
    # =================================================
    # Cursor stamps come back exactly as a page served them —
    # parsed to aware datetimes so the ORM binds them in each
    # engine's own type; a stamp no page could have produced
    # reads as no cursor
    before = clean_param(request.GET.get("before"))
    before = as_aware(before) if before else None
    before_id = clean_param(request.GET.get("before_id"))
    after = clean_param(request.GET.get("after"))
    after = as_aware(after) if after else None
    after_id = clean_param(request.GET.get("after_id"))
    around = clean_param(request.GET.get("around"))
    try:
        limit = int(request.GET.get("limit", 50))
    except (TypeError, ValueError):
        return json_error("limit must be an integer", 400)
    limit = max(1, min(limit, 100))
    has_newer = False
    if around:
        anchor = Message.objects.filter(id=around, conversation_id=conv_id) \
            .values("created_at", "id").first()
        if not anchor:
            return json_error("Message not found", 404)
        half = max(1, limit // 2)
        anchor_at, anchor_id = anchor["created_at"], anchor["id"]
        older = _page_rows(
            Q(conversation_id=conv_id)
            & (Q(created_at__lt=anchor_at) | Q(created_at=anchor_at, id__lte=anchor_id)),
            "DESC", half + 1,
        )
        newer = _page_rows(
            Q(conversation_id=conv_id)
            & (Q(created_at__gt=anchor_at) | Q(created_at=anchor_at, id__gt=anchor_id)),
            "ASC", half + 1,
        )
        has_more = len(older) > half
        has_newer = len(newer) > half
        rows = list(reversed(newer[:half])) + older[:half]
    elif after:
        window = Q(created_at__gt=after)
        if after_id:
            window |= Q(created_at=after, id__gt=after_id)
        newer = _page_rows(Q(conversation_id=conv_id) & window, "ASC", limit + 1)
        has_newer = len(newer) > limit
        rows = list(reversed(newer[:limit]))
        # Older than this page: the caller already holds it, but
        # the flag stays truthful for a client that started here
        probe = Q(created_at__lt=after)
        if after_id:
            probe |= Q(created_at=after, id__lte=after_id)
        has_more = Message.objects.filter(Q(conversation_id=conv_id) & probe).exists()
    elif before:
        window = Q(created_at__lt=before)
        if before_id:
            window |= Q(created_at=before, id__lt=before_id)
        rows = _page_rows(Q(conversation_id=conv_id) & window, "DESC", limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        rows = _page_rows(Q(conversation_id=conv_id), "DESC", limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]


    # STEP 3: reactions and read receipts for the whole page
    # in two IN (...) queries instead of two per message
    # ======================================================
    msg_ids = [row["id"] for row in rows]
    reaction_map_all = _reactions_for(msg_ids, user_id)
    read_map_all = _receipts_for(msg_ids)


    # STEP 4: the members and the conversation row — the room
    # header and intro card draw portraits and the title from
    # these, and the member count feeds the own-message status
    # ========================================================
    member_rows = list(
        ConversationParticipant.objects.filter(conversation_id=conv_id)
        .order_by("user__display_name")
        .values(id=F("user__id"), display_name=F("user__display_name"),
                avatar_url=F("user__avatar_url"))
    )
    participants = [
        {"id": m["id"], "displayName": m["display_name"], "avatarUrl": m["avatar_url"]}
        for m in member_rows
    ]
    participant_count = len(member_rows)

    # The conversation itself — a room opened from a push
    # notification has no title or type in its route params
    conv_row = Conversation.objects.filter(id=conv_id) \
        .values("id", "type", "title", "avatar_emoji", "message_ttl_seconds").first()
    conversation = {
        "id": conv_row["id"],
        "type": conv_row["type"],
        "title": conv_row["title"],
        "avatarEmoji": conv_row["avatar_emoji"],
        "messageTtlSeconds": conv_row.get("message_ttl_seconds"),
    } if conv_row else None


    # STEP 5: shape each message (_history_row) — reactions
    # with bySelf, readBy, and for the caller's own messages a
    # status derived from how many OTHER members hold a receipt
    # =========================================================
    messages = [
        _history_row(row, conv_id, user_id, reaction_map_all, read_map_all, participant_count)
        for row in rows
    ]


    # STEP 6: DESC fetch → chronological list
    # =======================================
    messages.reverse()

    return json_response({
        "messages": messages,
        "hasMore": has_more,
        "hasNewer": has_newer,
        "participants": participants,
        "conversation": conversation,
        # The point the client's change feed resumes from —
        # stamped before the page was read (STEP 1)
        "cursor": cursor,
    })


@require_methods("GET")
@transaction.non_atomic_requests
@require_auth
def get_changes(request, conv_id):
    # STEP 1: the NEXT cursor, before anything is read (see the
    # banner), then the incoming one — an ISO stamp, nothing else
    # ============================================================
    cursor = utc_now() - _CHANGES_CURSOR_SLACK
    user_id = request.user["id"]
    since = (clean_param(request.GET.get("since")) or "").strip()
    if not since:
        return json_error("since is required", 400)
    since_key = as_aware(since.replace("Z", "+00:00"))
    if since_key is None:
        return json_error("since must be an ISO datetime", 400)


    # STEP 2: membership
    # ==================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)


    # STEP 3: the rows that moved after the cursor, shaped like
    # a history row (the client applies them to what it holds)
    # ========================================================
    rows = list(
        Message.objects.filter(conversation_id=conv_id)
        .filter(Q(edited_at__gt=since_key) | Q(deleted_at__gt=since_key))
        .order_by("created_at", "id")
        .values(
            "id", "text", "image_url", "created_at", "sender_id", "reply_to_id",
            "deleted_at", "client_msg_id",
            "kind", "edited_at", "attachment_url", "attachment_name", "attachment_size",
            "attachment_mime", "attachment_meta", "link_preview", "gallery",
            "pinned_at", "pinned_by", "forwarded", "expires_at",
            sender_name=F("sender__display_name"), sender_avatar=F("sender__avatar_url"),
            reply_sender_id=F("reply_to__sender_id"), reply_text=F("reply_to__text"),
            reply_image_url=F("reply_to__image_url"), reply_gallery=F("reply_to__gallery"),
            reply_deleted_at=F("reply_to__deleted_at"), reply_kind=F("reply_to__kind"),
            reply_file_name=F("reply_to__attachment_name"),
            reply_sender_name=F("reply_to__sender__display_name"),
        )[:500]
    )


    # STEP 4: the SAME shaping as a history page — real
    # receipts, reactions and own-message status, never
    # placeholders the client would paint over the truth
    # ======================================================
    msg_ids = [row["id"] for row in rows]
    reaction_map = _reactions_for(msg_ids, user_id)
    read_map = _receipts_for(msg_ids)
    participant_count = ConversationParticipant.objects.filter(conversation_id=conv_id).count() if rows else 0
    messages = [
        _history_row(row, conv_id, user_id, reaction_map, read_map, participant_count)
        for row in rows
    ]

    return json_response({
        "messages": messages,
        "cursor": cursor,
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
# imageUrl, when non-empty, must be a local /api/uploads/
# path (or /api/memes/file/ for a shared meme) — anything
# else is a 400, so a stored message can never point a
# reader's client at a foreign server. The path's OWNER is
# not checked here (a forward re-sends somebody else's
# urls verbatim — see _is_local_upload_url); the unsend and
# the expiry sweep enforce it when the file would go.
# replyToId must name
# a message in THIS conversation (400, blank included).
# client_msg_id is the idempotency nonce: a repeat of one
# already committed answers 200 with the EXISTING row — the
# idempotency index closes the race window. Members only
# (403); a DIRECT chat between a blocked pair refuses the
# send (403) — group sends are allowed, the push lane keeps
# blocked phones quiet there. One transaction inserts the
# message, bumps conversations.updated_at and settles the
# sender's read state through _apply_mark_read — a receipt
# for every foreign message up to this instant and the
# watermark ADVANCED, never set back — plus the sender's
# own receipt. Replying is reading: a reply typed inside
# the client's read debounce used to move the watermark by
# hand with no receipt pass, which excluded those messages
# from every later mark_read for good and left the
# counterpart's bubbles at "sent" forever.
#
# Fan-out after commit: 'new_message' to room conv:<id>,
# 'messages_read' for the receipts the send wrote (targeted
# exactly as mark_read targets them), then push for every
# member WITHOUT a socket in that very
# room — room membership, not global presence. The push
# title is the sender's name (plus " · Group title" in a
# group); preview is the first 100 chars or a media marker
# (photo, video, voice message, file) with data.preview
# naming the kind; recipients with chat_push_preview off
# get the content-free body; @mentions get their own lane
# and leave the plain ones. Every line the BACKEND words —
# the markers, the content-free body, "X mentioned you" —
# goes out in Lithuanian AND English, and devices
# registered as 'en' get the English copy (the sender's
# own name and text read the same in both). The Expo
# round-trips run on a daemon thread.
# A URL in the text starts the unfurl task. Capped at 150
# sends per 5 min per user (429).
#
# Every refusal carries a stable code the phone triages on
# (the English prose is never shown): text_too_long and
# quote_not_found are the two the mobile engine names —
# "too long" and "the message you replied to no longer
# exists" — and every other 400 (json_required, bad_text,
# bad_attachment, bad_upload_url, bad_media,
# gallery_invalid, bad_kind, kind_mismatch, empty_message,
# bad_reply_id, bad_client_msg_id, bad_forwarded,
# bad_image_url) reads as "could not send". The 403s are
# not_a_participant and pair_blocked. edit_message,
# react_to_message and set_message_ttl below code theirs
# the same way.
#
# Used by:
#   - packages/chatengine/src/adapters/knf/rest.ts —
#     sendMessage (every composer send, retry and outbox
#     replay)
#   - app/(main)/chat-room/index.tsx — the forward flow,
#     through the same transport
############################################################

@require_methods("POST")
@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_send", max_attempts=150)
def send_message(request, conv_id):
    # STEP 1: validate the body
    # =========================
    user_id = request.user["id"]
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400, code="json_required")

    raw_text = data.get("text", "")
    if not isinstance(raw_text, str):
        return json_error("Text must be a string", 400, code="bad_text")
    text = raw_text.strip()
    image_url = data.get("imageUrl")

    # An optional document: {url, name, size, mime} — url must be
    # an own upload (the same beacon guard photos pass), name
    # bounded, size a non-negative int
    attachment = data.get("attachment")
    if attachment is not None:
        if not isinstance(attachment, dict):
            return json_error("attachment must be an object", 400, code="bad_attachment")
        att_url = attachment.get("url")
        att_name = attachment.get("name", "")
        att_size = attachment.get("size", 0)
        att_mime = attachment.get("mime", "")
        if not isinstance(att_url, str) or not _is_local_upload_url(att_url, request):
            return json_error("attachment.url must be an /api/uploads/ path", 400, code="bad_upload_url")
        if not isinstance(att_name, str) or not att_name.strip() or len(att_name) > 200:
            return json_error("attachment.name must be a non-blank string of at most 200 characters", 400, code="bad_attachment")
        if not isinstance(att_size, int) or isinstance(att_size, bool) or att_size < 0:
            return json_error("attachment.size must be a non-negative integer", 400, code="bad_attachment")
        if not isinstance(att_mime, str) or len(att_mime) > 100:
            return json_error("attachment.mime must be a short string", 400, code="bad_attachment")
        attachment = {"url": att_url, "name": att_name.strip(), "size": att_size, "mime": att_mime.strip()}

    # The frame of a photo / video: optional non-negative numbers
    # and a poster that must be an own upload
    media = data.get("media")
    if media is not None:
        if not isinstance(media, dict):
            return json_error("media must be an object", 400, code="bad_media")
        clean = {}
        for key in ("width", "height", "duration"):
            value = media.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 10_000_000:
                return json_error(f"media.{key} must be a non-negative number", 400, code="bad_media")
            clean[key] = value
        thumb = media.get("thumbnailUrl")
        if thumb is not None:
            if not isinstance(thumb, str) or not _is_local_upload_url(thumb, request):
                return json_error("media.thumbnailUrl must be an /api/uploads/ path", 400, code="bad_upload_url")
            clean["thumbnailUrl"] = thumb
        # The ~14px micro copy, echoed back so every reader draws
        # the blur before the bytes; a data URI only — never a
        # fetchable address
        preview_uri = media.get("preview")
        if preview_uri is not None:
            if not isinstance(preview_uri, str) or not preview_uri.startswith("data:image/") or len(preview_uri) > 2000:
                return json_error("media.preview must be a small data:image/ URI", 400, code="bad_media")
            clean["preview"] = preview_uri
        # A voice note's amplitude bars: up to 64 numbers in 0..1
        waveform = media.get("waveform")
        if waveform is not None:
            if not isinstance(waveform, list) or len(waveform) > 64:
                return json_error("media.waveform must be a list of at most 64 numbers", 400, code="bad_media")
            bars = []
            for value in waveform:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1:
                    return json_error("media.waveform values must be numbers between 0 and 1", 400, code="bad_media")
                bars.append(round(float(value), 3))
            clean["waveform"] = bars
        media = clean or None

    # Several photos in one message: a list of 2–8 {url, width,
    # height} objects, every url an own upload. A gallery rides
    # alone — no single imageUrl and no file beside it
    gallery = data.get("gallery")
    if gallery is not None:
        if not isinstance(gallery, list) or not 2 <= len(gallery) <= 8:
            return json_error("gallery must be a list of 2 to 8 photos", 400, code="gallery_invalid")
        clean_items = []
        for item in gallery:
            if not isinstance(item, dict):
                return json_error("every gallery item must be an object", 400, code="gallery_invalid")
            item_url = item.get("url")
            if not isinstance(item_url, str) or not _is_local_upload_url(item_url, request):
                return json_error("every gallery url must be an /api/uploads/ path", 400, code="bad_upload_url")
            clean_item = {"url": item_url}
            for key in ("width", "height"):
                value = item.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 10_000_000:
                    return json_error(f"gallery {key} must be a non-negative number", 400, code="gallery_invalid")
                clean_item[key] = value
            item_preview = item.get("preview")
            if item_preview is not None:
                if not isinstance(item_preview, str) or not item_preview.startswith("data:image/") or len(item_preview) > 2000:
                    return json_error("gallery preview must be a small data:image/ URI", 400, code="gallery_invalid")
                clean_item["preview"] = item_preview
            clean_items.append(clean_item)
        gallery = clean_items
        if image_url or attachment:
            return json_error("A gallery cannot ride with a single image or a file", 400, code="gallery_invalid")
        if data.get("kind") not in (None, "image"):
            return json_error("A gallery message's kind is image", 400, code="gallery_invalid")

    # The declared kind must match the content
    kind_param = data.get("kind")
    if kind_param is not None and kind_param not in ("text", "image", "file", "video", "audio"):
        return json_error("kind must be text, image, file, video or audio", 400, code="bad_kind")
    if kind_param in ("video", "audio") and not attachment:
        return json_error("A video or audio message needs an attachment", 400, code="kind_mismatch")
    if kind_param == "image" and not image_url and not gallery:
        return json_error("An image message needs imageUrl", 400, code="kind_mismatch")

    if not text and not image_url and not attachment and not gallery:
        return json_error("Message must have text, an image or an attachment", 400, code="empty_message")

    if text and len(text) > 5000:
        return json_error("Message text must not exceed 5000 characters", 400, code="text_too_long")

    reply_to_id = data.get("replyToId")
    if reply_to_id is not None and not isinstance(reply_to_id, str):
        return json_error("replyToId must be a string", 400, code="bad_reply_id")
    # A blank quote id is a client bug, not "no reply"
    if reply_to_id is not None and not reply_to_id.strip():
        return json_error("replyToId must not be blank", 400, code="bad_reply_id")

    client_msg_id = data.get("client_msg_id")
    if client_msg_id is not None and not isinstance(client_msg_id, str):
        return json_error("client_msg_id must be a string", 400, code="bad_client_msg_id")
    if client_msg_id and len(client_msg_id) > 128:
        return json_error("client_msg_id too long", 400, code="bad_client_msg_id")

    # A message re-sent from another room carries only this mark
    forwarded = data.get("forwarded")
    if forwarded is not None and not isinstance(forwarded, bool):
        return json_error("forwarded must be a boolean", 400, code="bad_forwarded")
    forwarded = bool(forwarded)


    # STEP 1.1: imageUrl — the TYPE check sits OUTSIDE the
    # truthiness gate on purpose: a falsy non-string ([], {},
    # 0, false) would skip validation whole and reach the
    # driver as a bind parameter. An empty string stays the
    # one falsy value that passes — stored and echoed as given
    # ======================================================
    if image_url is not None and not isinstance(image_url, str):
        return json_error("imageUrl must be a string", 400, code="bad_image_url")

    # Shape only, no ownership check — here and for attachment,
    # media.thumbnailUrl and gallery above: a forwarded message
    # carries the original sender's urls, so owning them cannot
    # be a condition of sending. Ownership is enforced where the
    # file would leave disk (delete_message, _sweep_expired)
    if image_url and not _is_local_upload_url(image_url, request) and not _is_meme_library_url(image_url, request):
        return json_error("imageUrl must be an /api/uploads/ or /api/memes/file/ path", 400, code="bad_image_url")


    # STEP 2: membership gate — 403 for outsiders; a quoted
    # message must live in this very conversation (400)
    # =====================================================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403, code="not_a_participant")

    # Disappearing messages leave before the room grows
    _sweep_expired(conv_id, request)


    # STEP 2.0: a DIRECT chat between a blocked pair (either
    # direction) refuses the send — create_conversation stops
    # NEW rooms, this stops the room that predates the block.
    # Group sends are allowed: membership is the group's
    # decision, and the push fan-out below keeps a blocked
    # pair's phones quiet there
    # =======================================================
    conv_type_row = Conversation.objects.filter(id=conv_id).values("type").first()
    if conv_type_row and conv_type_row["type"] == "direct":
        counterpart = ConversationParticipant.objects.filter(conversation_id=conv_id) \
            .exclude(user_id=user_id).values("user_id").first()
        if counterpart:
            pair_blocked = UserBlock.objects.filter(
                Q(blocker_id=user_id, blocked_id=counterpart["user_id"])
                | Q(blocker_id=counterpart["user_id"], blocked_id=user_id),
            ).exists()
            if pair_blocked:
                return json_error("You cannot message this user", 403, code="pair_blocked")

    reply_row = None
    if reply_to_id:
        quoted = Message.objects.filter(id=reply_to_id, conversation_id=conv_id).values(
            "id", "sender_id", "text", "image_url", "gallery", "deleted_at", "kind",
            "attachment_name", sender_name=F("sender__display_name"),
        ).first()
        if not quoted:
            return json_error("Quoted message not found in this conversation", 400, code="quote_not_found")
        # The reply_* keys _reply_payload shapes the quote from
        reply_row = {
            "reply_to_id": quoted["id"],
            "reply_sender_id": quoted["sender_id"],
            "reply_text": quoted["text"],
            "reply_image_url": quoted["image_url"],
            "reply_gallery": quoted["gallery"],
            "reply_deleted_at": quoted["deleted_at"],
            "reply_kind": quoted["kind"],
            "reply_file_name": quoted["attachment_name"],
            "reply_sender_name": quoted["sender_name"],
        }


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
    # never shows as unread to them and everything they had
    # in front of them counts as read. The idempotency index
    # turns a racing double-submit into an IntegrityError,
    # answered like the replay above
    # ======================================================
    msg_id = str(uuid.uuid4())
    now = utc_now()
    newly_read_ids = []

    if attachment:
        kind = kind_param if kind_param in ("video", "audio") else "file"
    else:
        kind = "image" if image_url or gallery else "text"
    # The frame only means something on a photo, a video or a
    # voice note (its duration)
    stored_media = media if media and kind in ("image", "video", "audio") else None
    # Disappearing messages: the room's TTL at SEND time stamps
    # this row's hard-delete deadline — changing the TTL later
    # never touches what was already sent
    ttl_row = Conversation.objects.filter(id=conv_id).values("message_ttl_seconds").first()
    ttl_seconds = ttl_row.get("message_ttl_seconds") if ttl_row else None
    expires_at = utc_now() + timedelta(seconds=ttl_seconds) if ttl_seconds else None

    try:
        with transaction.atomic():
            Message.objects.create(
                id=msg_id, conversation_id=conv_id, sender_id=user_id, text=text,
                image_url=image_url, reply_to_id=reply_to_id or None,
                client_msg_id=client_msg_id or None, created_at=now, kind=kind,
                attachment_url=attachment["url"] if attachment else None,
                attachment_name=attachment["name"] if attachment else None,
                attachment_size=attachment["size"] if attachment else None,
                attachment_mime=attachment["mime"] if attachment else None,
                attachment_meta=stored_media, gallery=gallery or None,
                forwarded=forwarded, expires_at=expires_at,
            )

            Conversation.objects.filter(id=conv_id).update(updated_at=now)

            # The sender's read state through the ONE mark-read
            # implementation: receipts for every foreign message
            # up to this instant (bounded by _MARK_READ_CAP), then
            # the watermark — advanced, never merely set, so a
            # racing device cannot drag it back. Inside this
            # atomic block the helper joins the transaction
            # instead of opening its own (_begin_immediate). None
            # back (the membership row vanished under the gate)
            # is simply "nothing read" — the send still lands
            newly_read_ids = _apply_mark_read(conv_id, user_id, now) or []

            # The sender's own receipt — refused silently when a
            # racing twin already wrote it (the composite key)
            MessageRead.objects.bulk_create(
                [MessageRead(message_id=msg_id, user_id=user_id, read_at=now)],
                ignore_conflicts=True,
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
        "media": stored_media,
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

    # The receipts the send wrote for what the sender had not yet
    # marked: 'messages_read' to the affected senders and the
    # reader's own devices, exactly what mark_read would have
    # sent — the counterpart's bubbles flip live, not on refetch
    if newly_read_ids:
        from knfapp.chat.events import emit_read_receipt
        emit_read_receipt(_get_sio(), conv_id, user_id, newly_read_ids)


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

        participants = ConversationParticipant.objects.filter(conversation_id=conv_id) \
            .exclude(user_id=user_id).values("user_id")
        recipients = [p["user_id"] for p in participants if p["user_id"] not in in_room_ids]

        # Blocks silence the push lane too: in a group the message
        # stays visible in the room, but a phone in a block pair
        # with the sender (either direction) stays quiet
        if recipients:
            block_rows = UserBlock.objects.filter(
                Q(blocker_id=user_id, blocked_id__in=recipients)
                | Q(blocked_id=user_id, blocker_id__in=recipients),
            ).values("blocker_id", "blocked_id")
            silenced = {
                r["blocked_id"] if r["blocker_id"] == user_id else r["blocker_id"]
                for r in block_rows
            }
            recipients = [r for r in recipients if r not in silenced]

        if recipients:
            # The title says WHO wrote and, in a group, WHERE — a
            # bare display name on a lock screen leaves the reader
            # guessing which room it came from
            conv_row = Conversation.objects.filter(id=conv_id).values("type", "title").first()
            push_title = user["display_name"]
            if conv_row and conv_row["type"] == "group" and conv_row["title"]:
                push_title = f"{push_title} · {conv_row['title']}"

            push_data = {"type": "chat_message", "conversationId": conv_id}
            # A push with an empty body renders nothing on a lock
            # screen, so a caption-less media message ships the
            # word for its kind — in BOTH languages: the push lane
            # sends preview_en to devices registered as 'en' (None
            # means "the sender's own words", identical in both);
            # data.preview names the kind for a foreground client
            preview_en = None
            if text:
                preview = text[:100]
            elif attachment and kind == "video":
                preview, preview_en = "Vaizdo įrašas", "Video"
                push_data["preview"] = "video"
            elif attachment and kind == "audio":
                preview, preview_en = "Balso žinutė", "Voice message"
                push_data["preview"] = "audio"
            elif attachment:
                preview, preview_en = "Failas", "File"
                push_data["preview"] = "file"
            else:
                preview, preview_en = "Nuotrauka", "Photo"
                push_data["preview"] = "photo"
            # Preview privacy: a recipient who turned
            # chat_push_preview off gets the content-free body —
            # their message text never leaves for Expo at all
            no_preview = set(
                User.objects.filter(id__in=recipients, chat_push_preview=0)
                .values_list("id", flat=True)
            )
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
                for name_row in User.objects.filter(id__in=recipients).values("id", "display_name"):
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
                mention_title_en = f"{user['display_name']} mentioned you"
                if conv_row and conv_row["type"] == "group" and conv_row["title"]:
                    mention_title = f"{mention_title} · {conv_row['title']}"
                    mention_title_en = f"{mention_title_en} · {conv_row['title']}"
                mention_data = {**push_data, "type": "chat_mention"}
                full_mentioned = [r for r in full_recipients if r in mentioned]
                quiet_mentioned = [r for r in quiet_recipients if r in mentioned]
                if full_mentioned:
                    _spawn(_push_chat_message, full_mentioned, mention_title, preview, mention_data,
                           mention_title_en, preview_en)
                if quiet_mentioned:
                    _spawn(_push_chat_message, quiet_mentioned, mention_title,
                           "Nauja žinutė", {**mention_data, "preview": "hidden"},
                           mention_title_en, "New message")
                full_recipients = [r for r in full_recipients if r not in mentioned]
                quiet_recipients = [r for r in quiet_recipients if r not in mentioned]
            if full_recipients:
                _spawn(_push_chat_message, full_recipients, push_title, preview, push_data,
                       None, preview_en)
            if quiet_recipients:
                # The content-free body, worded per language too
                _spawn(_push_chat_message, quiet_recipients, push_title,
                       "Nauja žinutė", {**push_data, "preview": "hidden"},
                       None, "New message")
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
# sink in BOTH the forms the send accepts, so neither can
# leave an orphan — AS THE SENDER: a forwarded copy of
# somebody else's photo is refused and their file
# survives, and a photo the sender still shows in another
# message stays until that one goes; neither ever fails
# the unsend. Outsider → 403, unknown →
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
#   - packages/chatengine/src/adapters/knf/rest.ts —
#     deleteMessage (the room's optimistic unsend and its
#     offline replay) / editMessage (edit mode's save and its
#     offline replay)
############################################################

@require_methods("DELETE")
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
    row = Message.objects.filter(id=msg_id, conversation_id=conv_id).values(
        "sender_id", "deleted_at", "image_url", "attachment_url",
        "attachment_meta", "link_preview", "gallery",
    ).first()
    if not row:
        return json_error("Message not found", 404)
    if row["sender_id"] != user_id:
        return json_error("Only the sender can delete a message", 403)

    if row["deleted_at"] is None:
        now = datetime.now(timezone.utc)
        with transaction.atomic():
            Message.objects.filter(id=msg_id).update(
                text="", image_url=None, attachment_url=None, attachment_name=None,
                attachment_size=None, attachment_mime=None, attachment_meta=None,
                link_preview=None, gallery=None, pinned_at=None, pinned_by=None,
                deleted_at=now,
            )
            MessageReaction.objects.filter(message_id=msg_id).delete()

        # The photo blob goes with the message — every slot
        # _stored_upload_urls walks, matched by the SAME rule the
        # send accepted it under, so neither form can leave an
        # orphan on disk. The sink acts as the sender:
        # "forbidden" (never theirs — a forward) and "referenced"
        # (still shown by another message) keep the file and are
        # only logged; the slots above are already NULL, so this
        # row cannot hold its own file back
        for stored in _stored_upload_urls(row, request):
            try:
                from knfapp.uploads.storage import delete_upload
                outcome = delete_upload(stored, user_id)
                if outcome in ("forbidden", "referenced"):
                    logger.info("Unsend of message %s kept its upload (%s)", msg_id, outcome)
            except Exception:
                logger.exception("Upload cleanup failed for unsent message")

        from knfapp.chat.events import emit_message_deleted
        emit_message_deleted(_get_sio(), conv_id, msg_id)

    return json_response({"ok": True})


@require_methods("PUT")
@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_edit", max_attempts=100)
def edit_message(request, conv_id, msg_id):
    # STEP 1: the body — a non-blank, bounded string
    # ==============================================
    user_id = request.user["id"]
    data = get_json_object(request)
    if not data:
        return json_error("JSON body required", 400, code="json_required")
    raw_text = data.get("text")
    if not isinstance(raw_text, str):
        return json_error("Text must be a string", 400, code="bad_text")
    text = raw_text.strip()
    if not text:
        return json_error("Message text required", 400, code="empty_message")
    if len(text) > 5000:
        return json_error("Message text must not exceed 5000 characters", 400, code="text_too_long")


    # STEP 2: membership, the row, its owner, its state
    # =================================================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403, code="not_a_participant")

    row = Message.objects.filter(id=msg_id, conversation_id=conv_id) \
        .values("sender_id", "deleted_at", "kind").first()
    if not row:
        return json_error("Message not found", 404, code="message_not_found")
    if row["sender_id"] != user_id:
        return json_error("Only the sender can edit a message", 403, code="not_the_sender")
    if row["deleted_at"] is not None:
        return json_error("An unsent message cannot be edited", 409, code="message_unsent")
    if (row["kind"] or "text") not in ("text", "image"):
        return json_error("Only text messages can be edited", 400, code="not_editable")


    # STEP 3: the rewrite, stamped, then the broadcast
    # ================================================
    now = utc_now()
    with transaction.atomic():
        Message.objects.filter(id=msg_id).update(text=text, edited_at=now)

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
# room narrates the change with a system row (event ttl_on
# with the window in SECONDS, or ttl_off — the client words
# the window in its own language) and every client hears
# 'conversation_updated' {messageTtlSeconds}. Setting the
# window the room already has is a no-op answer — no second
# "turned it off" line for every member, no room bump.
#
# Used by:
#   - packages/chatengine/src/adapters/knf/rest.ts —
#     pinMessage / unpinMessage / fetchPins (the room's
#     pinned banner and menu rows, via usePins) and
#     setMessageTtl (the room's disappearing-messages sheet)
############################################################

@require_methods("PUT", "DELETE")
@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_pin", max_attempts=60)
def pin_message(request, conv_id, msg_id):
    # STEP 1: membership, the row, its state
    # ======================================
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    row = Message.objects.filter(id=msg_id, conversation_id=conv_id) \
        .values("deleted_at", "kind").first()
    if not row or row["deleted_at"] is not None:
        return json_error("Message not found", 404)
    if (row["kind"] or "text") == "system":
        return json_error("System messages cannot be pinned", 400)


    # STEP 2: flip the pin and tell the room — PUT pins, DELETE
    # (the only other verb the guard admits) unpins
    # =========================================================
    if request.method == "PUT":
        pinned_at = utc_now()
        pinned_by = user_id
    else:
        pinned_at = None
        pinned_by = None
    with transaction.atomic():
        Message.objects.filter(id=msg_id).update(pinned_at=pinned_at, pinned_by=pinned_by)

    from knfapp.chat.events import emit_message_updated
    emit_message_updated(_get_sio(), conv_id, msg_id, {"pinnedAt": pinned_at, "pinnedBy": pinned_by})
    return json_response({"pinnedAt": pinned_at, "pinnedBy": pinned_by})


@require_methods("GET")
@transaction.non_atomic_requests
@require_auth
def get_pins(request, conv_id):
    # STEP 1: membership gate
    # =======================
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)


    # STEP 2: the pinned rows, shaped lean — a pin whose
    # disappearing-message deadline has passed is out HERE,
    # not at the next sweep: this list is read without one
    # =====================================================
    now = utc_now()
    rows = Message.objects.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
        conversation_id=conv_id, pinned_at__isnull=False, deleted_at__isnull=True,
    ).order_by("-pinned_at").values(
        "id", "text", "image_url", "created_at", "client_msg_id", "sender_id",
        "kind", "edited_at", "attachment_url", "attachment_name", "attachment_size",
        "attachment_mime", "attachment_meta", "link_preview", "gallery",
        "pinned_at", "pinned_by", "forwarded", "expires_at", "deleted_at",
        display_name=F("sender__display_name"), avatar_url=F("sender__avatar_url"),
    )[:20]

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


@require_methods("PUT")
@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_ttl", max_attempts=30)
def set_message_ttl(request, conv_id):
    # STEP 1: the body — null/0 (off) or one bounded window
    # =====================================================
    user_id = request.user["id"]
    data = get_json_object(request)
    if data is None:
        return json_error("JSON body required", 400, code="json_required")
    seconds = data.get("seconds")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int)):
        return json_error("seconds must be an integer or null", 400, code="bad_ttl")
    if seconds is not None and seconds != 0 and not 60 <= seconds <= 31_536_000:
        return json_error("seconds must be 0 (off) or between 60 and 31536000", 400, code="bad_ttl")
    ttl = seconds or None


    # STEP 2: membership, the write, the narration
    # ============================================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403, code="not_a_participant")

    with transaction.atomic():
        current = (Conversation.objects.select_for_update().filter(id=conv_id)
                   .values_list("message_ttl_seconds", flat=True).first())
        if (current or None) == ttl:
            return json_response({"messageTtlSeconds": ttl})
        Conversation.objects.filter(id=conv_id).update(message_ttl_seconds=ttl)

        if ttl:
            if ttl < 3600:
                window = f"{ttl // 60} min."
            elif ttl < 86_400:
                window = f"{ttl // 3600} val."
            else:
                window = f"{ttl // 86_400} d."
            narration = f"{request.user['display_name']} įjungė nykstančias žinutes ({window})"
            event = {"event": "ttl_on", "seconds": ttl}
        else:
            narration = f"{request.user['display_name']} išjungė nykstančias žinutes"
            event = {"event": "ttl_off"}
        system_payload = _insert_system_message(conv_id, request.user, narration, event=event)

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
#   - packages/chatengine/src/adapters/knf/rest.ts —
#     setReaction / removeReaction (the room's picker and the
#     bubbles' accessibility React action, via useReactions,
#     and their offline replay)
############################################################

@require_methods("POST")
@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_react", max_attempts=300)
def react_to_message(request, conv_id, msg_id):
    user_id = request.user["id"]
    data = get_json_object(request)
    if not data or not data.get("emoji"):
        return json_error("emoji required", 400, code="emoji_required")

    if not isinstance(data["emoji"], str):
        return json_error("emoji must be a string", 400, code="bad_emoji")

    # The server-side allowlist mirrors the mobile picker's
    # REACTION_OPTIONS — the only six values a client can send
    emoji = data["emoji"]
    if emoji not in _ALLOWED_REACTIONS:
        return json_error("emoji must be one of the supported reactions", 400, code="bad_emoji")

    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403, code="not_a_participant")

    # The conversation id in the URL is what the membership
    # check trusted, so the message must really live there —
    # and still be un-unsent
    msg = Message.objects.filter(
        id=msg_id, conversation_id=conv_id, deleted_at__isnull=True,
    ).exists()
    if not msg:
        return json_error("Message not found", 404, code="message_not_found")

    with transaction.atomic():
        # One emoji per user: replace, never accumulate
        MessageReaction.objects.filter(message_id=msg_id, user_id=user_id).delete()
        MessageReaction.objects.create(
            message_id=msg_id, user_id=user_id, emoji=emoji,
            created_at=datetime.now(timezone.utc),
        )

        # Snapshot INSIDE the transaction, broadcast after — a
        # concurrent reaction cannot slip a newer state into an
        # older broadcast
        reactions = _reactions_for([msg_id]).get(msg_id, [])

    from knfapp.chat.events import emit_reaction_update
    emit_reaction_update(_get_sio(), conv_id, msg_id, reactions)

    return json_response({"ok": True, "emoji": emoji, "reactions": reactions})


@require_methods("DELETE")
@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_react", max_attempts=300)
def remove_reaction(request, conv_id, msg_id):
    user_id = request.user["id"]
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    # Same gates as react_to_message — the returned list and
    # the broadcast cannot leak who reacted to a message the
    # caller cannot see
    msg = Message.objects.filter(id=msg_id, conversation_id=conv_id).exists()
    if not msg:
        return json_error("Message not found", 404)

    with transaction.atomic():
        MessageReaction.objects.filter(message_id=msg_id, user_id=user_id).delete()
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

@require_methods("PUT")
@transaction.non_atomic_requests
@require_auth
def toggle_pin(request, conv_id):
    user_id = request.user["id"]
    with transaction.atomic():
        # 0 rows means non-member
        changed = ConversationParticipant.objects.filter(
            conversation_id=conv_id, user_id=user_id,
        ).update(pinned=1 - F("pinned"))
        if changed == 0:
            return json_error("Not a participant", 403)

        # Re-read inside the same transaction for the response
        row = ConversationParticipant.objects.filter(
            conversation_id=conv_id, user_id=user_id,
        ).values("pinned").first()
    return json_response({"pinned": bool(row["pinned"])})








############################################################
# _apply_mark_read / mark_read
############################################################
#
# _apply_mark_read is the ONE mark-read implementation both
# transports share — the REST route below and events.py
# handle_mark_read call it instead of carrying the logic
# twice. It runs inside a single hand-issued write
# transaction (_begin_immediate): the membership lookup
# doubles as the gate AND yields the prior watermark, so
# there is no TOCTOU window between gate and writes — the
# STEP comments carry the rest. Returns the pre-selected id
# list for the frozen messages_read broadcast — None means
# "not a participant" (the REST edge answers 403, the
# socket edge drops silently).
#
# mark_read spends the SOCKET limiter's mark_read budget
# (10 per 10 s per user, 429 past it) so the REST path is
# not the free bypass around the socket quota; when at
# least one receipt was new, the reader id and message ids
# broadcast as 'messages_read'.
#
# Used by:
#   - packages/chatengine/src/adapters/knf/rest.ts — markRead
#     (the durable twin the room sends beside the socket's
#     volatile mark_read)
#   - chat/events.py — handle_mark_read, the socket twin
############################################################

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
                (conv_id, user_id, _bind(prior), _bind(now), user_id, _MARK_READ_CAP),
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
                (conv_id, user_id, _bind(now), user_id, _MARK_READ_CAP),
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
                INSERT INTO message_reads (message_id, user_id, read_at)
                SELECT m.id, %s, %s FROM messages m WHERE m.id IN ({placeholders})
                ON CONFLICT (message_id, user_id) DO NOTHING
                """,
                [user_id, _bind(now)] + newly_read_ids,
            )

        # The pointer ADVANCES, it is never merely set: every call
        # takes its own `now` BEFORE the write lock, so the one
        # that started earlier can commit last, and a bare SET
        # would drag the watermark back over messages this reader
        # had already cleared. The guard lives in the statement
        # itself, so the same UPDATE holds on any engine
        _exec(
            "UPDATE conversation_participants SET last_read_at = %s"
            " WHERE conversation_id = %s AND user_id = %s"
            "   AND (last_read_at IS NULL OR last_read_at < %s)",
            (_bind(now), conv_id, user_id, _bind(now)),
        )

        _end_immediate(started, commit=True)
    except Exception:
        _end_immediate(started, commit=False)
        raise

    return newly_read_ids


@require_methods("PUT")
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
    now = utc_now()
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
# members' read/status math never counts a ghost reader;
# once nobody is left the messages, their reads and
# reactions and the conversation itself are purged too —
# and every file those messages held goes to the uploads
# sink on the commit, as its sender (_stored_upload_urls),
# so a destroyed room leaves no orphan on disk. The
# members who stay see who left (a group's narration); after
# the commit every socket of the leaver is evicted from room
# conv:<id> (best effort). The remaining members keep the
# history, with the leaver's messages still attributed.
#
# total_unread_count answers the tab badge: other people's
# messages newer than the caller's last_read_at, unsent ones
# and system lines excluded, over every conversation they
# belong to — one flat COUNT(*) join. Same definition as the per-row
# unreadCount, so the tab badge and the row badges agree;
# it does NOT consult message_reads.
#
# Used by:
#   - services/api/chat.ts — deleteConversationApi /
#     fetchTotalUnreadCount
############################################################

@require_methods("DELETE")
@transaction.non_atomic_requests
@require_auth
def leave_conversation(request, conv_id):
    # STEP 1: the gates — an unknown room is a 404, an
    # outsider a 403, and only a real member reaches the
    # delete below
    # ==================================================
    user_id = request.user["id"]
    if not Conversation.objects.filter(id=conv_id).exists():
        return json_error("Conversation not found", 404)

    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)


    # STEP 2: one transaction — the membership row AND the
    # leaver's receipts/reactions in this room; purge when
    # the last member is gone
    # ====================================================
    system_payload = None
    with transaction.atomic():
        ConversationParticipant.objects.filter(conversation_id=conv_id, user_id=user_id).delete()
        conv_msg_ids = Message.objects.filter(conversation_id=conv_id).values("id")
        MessageRead.objects.filter(user_id=user_id, message_id__in=conv_msg_ids).delete()
        MessageReaction.objects.filter(user_id=user_id, message_id__in=conv_msg_ids).delete()

        remaining = ConversationParticipant.objects.filter(conversation_id=conv_id).count()

        if remaining == 0:
            # Every file the dying messages hold, paired with the
            # sender the delete acts for — collected BEFORE the
            # rows go, handed to the uploads sink once they are
            # gone (on the commit, exactly as the expiry sweep
            # does): a forwarded copy of somebody else's photo is
            # refused and one still shown elsewhere is kept
            doomed = []
            for row in Message.objects.filter(conversation_id=conv_id).values(
                "sender_id", "image_url", "attachment_url", "attachment_meta", "link_preview", "gallery",
            ):
                doomed.extend((stored, row["sender_id"]) for stored in _stored_upload_urls(row, request))
            MessageRead.objects.filter(message_id__in=conv_msg_ids).delete()
            MessageReaction.objects.filter(message_id__in=conv_msg_ids).delete()
            # Raw on purpose — the ORM delete would collect
            # every row and run the reply-quote SET_NULL pass
            # over rows that are all dying anyway
            _exec("DELETE FROM messages WHERE conversation_id = %s", (conv_id,))
            Conversation.objects.filter(id=conv_id).delete()
            if doomed:
                transaction.on_commit(lambda: _delete_message_uploads(doomed))
        else:
            # The members who stay see who left — a group's
            # narration; a direct chat's other half simply keeps
            # the conversation as it was
            conv_type_row = Conversation.objects.filter(id=conv_id).values("type").first()
            if conv_type_row and conv_type_row["type"] == "direct":
                # The half-left room stops claiming the pair — a
                # fresh create for these two must insert anew, not
                # answer 200 onto a room the leaver abandoned
                Conversation.objects.filter(id=conv_id).update(direct_key=None)
            if conv_type_row and conv_type_row["type"] == "group":
                system_payload = _insert_system_message(
                    conv_id, request.user, f"{request.user['display_name']} paliko pokalbį",
                    event={"event": "left"},
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


@require_methods("GET")
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
          AND m.kind != 'system'
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
# ?q (required, 400 when blank after strip or over 200
# chars) and ?limit (default 20, clamped into 1..50,
# non-numeric → 400, parsed only after the membership gate).
# Members only (403). ONE substring match — the same
# icontains on the room's un-unsent rows on both engines,
# but the case folding is the ENGINE's: SQLite's LIKE folds
# ASCII only, PostgreSQL folds by collation, so a capitalised
# Lithuanian diacritic in the needle or the row misses only
# on SQLite — the needle's \, % and _ escaped by the
# lookup so they match literally. NUL and the other C0
# controls are stripped from q before anything else looks
# at it (clean_param — bound NUL-terminated, a NUL would
# collapse the LIKE pattern to a bare '%' and page the
# whole room), so "La\x00bas" searches for "Labas" and a
# NUL alone is the blank-q 400. Returns {messages, total}:
# the newest `limit` hits reversed to chronological order,
# plus the total so the UI can say "20 of 137" — the
# counter SATURATES at _SEARCH_TOTAL_CAP, so that value
# means "this many or more". Capped at 100 searches per 5
# min per user (429). A disappearing message past its
# expires_at is never a hit: the room is swept first
# (_sweep_expired — free for a room without a TTL) and the
# query filters on expires_at besides, for the window
# between the two; a hit carries expiresAt so the client
# can drop it the moment it lapses on screen.
#
# Used by:
#   - services/api/chat.ts — searchMessagesApi
############################################################

@require_methods("GET")
@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_msg_search", max_attempts=100)
def search_messages(request, conv_id):
    # STEP 1: q — a blank q is a 400 (unlike search_users);
    # a control byte is gone before the length is judged
    # =====================================================
    user_id = request.user["id"]
    q = clean_param(request.GET.get("q", "")).strip()
    if len(q) < 1:
        return json_error("q parameter is required and must not be empty", 400)
    if len(q) > _SEARCH_Q_MAX:
        return json_error(f"q must be at most {_SEARCH_Q_MAX} characters", 400)


    # STEP 2: membership gate — 403 for outsiders, BEFORE any
    # other parameter is even parsed
    # =======================================================
    if not _is_member(conv_id, user_id):
        return json_error("Not a participant", 403)

    # Disappearing messages leave before the room is searched
    _sweep_expired(conv_id, request)

    try:
        limit = int(request.GET.get("limit", 20))
    except (TypeError, ValueError):
        return json_error("limit must be an integer", 400)
    limit = max(1, min(limit, 50))


    # STEP 3: the newest `limit` LIVE hits plus the saturating
    # total — icontains escapes the needle's wildcards itself
    # and carries the case folding per engine; the expiry
    # predicate covers a row that lapses between the sweep
    # above and this SELECT
    # ========================================================
    now = utc_now()
    hits = Message.objects.filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
        conversation_id=conv_id, deleted_at__isnull=True, text__icontains=q,
    )
    rows = list(
        hits.order_by("-created_at").values(
            "id", "text", "image_url", "created_at", "sender_id", "expires_at",
            sender_name=F("sender__display_name"), sender_avatar=F("sender__avatar_url"),
        )[:limit]
    )

    # Counted over a capped subquery: the label needs "many",
    # not the exact number of hits in a decade of history
    total = hits[:_SEARCH_TOTAL_CAP].count()


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
            "expiresAt": row["expires_at"],
        })

    messages.reverse()

    return json_response({"messages": messages, "total": total})








############################################################
# online_status / search_users
############################################################
#
# online_status: body {userIds[]} →
# {online: {id: bool}, lastSeen: {id: iso|null}} — whether
# each id currently has a socket in THIS process and when
# they last held one (users.last_active_at, stamped on both
# socket edges), but ONLY for users who chose a relationship
# with the caller: an accepted friend, or someone who has
# actually written in a conversation the caller is in. Bare
# co-membership is NOT enough — anyone can create a room
# with anyone, so a gate on membership alone let a stranger
# manufacture it in one request and then poll a victim's
# online timeline. A block pair (either direction) reveals
# nothing. Everyone else answers false/null, exactly like a
# genuinely offline user nobody ever saw, so the route is
# not a free presence-or-history oracle over arbitrary ids.
# Silently truncated to the first 200 ids; non-string ids
# dropped.
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
# chars answers {users: []} with 200 so the picker can
# call it on every keystroke. NUL and the other C0
# controls are stripped first (clean_param — a NUL bound
# NUL-terminated would collapse the pattern to '%' and page
# the whole directory), so the 2-char floor judges what is
# left. Returns id, username, displayName, avatarUrl, role
# — no email; username and role are required by the mobile
# SearchUserResult type (frozen contract).
#
# Used by:
#   - services/api/chat.ts — fetchOnlineStatus /
#     searchUsersApi
############################################################

@require_methods("POST")
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


    # STEP 2: the relationship gate — presence is only revealed
    # for a friend or for someone who has written in one of the
    # caller's conversations (a relationship THEY took part in);
    # a stranger's id, or one merely added to a room, probes
    # exactly nothing, and a block pair hides both ways
    # ========================================================
    shared = set()
    if user_ids:
        from knfapp.social.models import Friendship

        my_rooms = ConversationParticipant.objects.filter(user_id=user_id).values("conversation_id")
        spoke = set(
            Message.objects.filter(sender_id__in=user_ids, conversation_id__in=my_rooms)
            .values_list("sender_id", flat=True).distinct()
        )
        friends = set(
            Friendship.objects.filter(user_id=user_id, friend_id__in=user_ids)
            .values_list("friend_id", flat=True)
        )
        blocked = set()
        for row in UserBlock.objects.filter(
            Q(blocker_id=user_id, blocked_id__in=user_ids) | Q(blocked_id=user_id, blocker_id__in=user_ids),
        ).values("blocker_id", "blocked_id"):
            blocked.add(row["blocked_id"] if row["blocker_id"] == user_id else row["blocker_id"])
        # The caller may always see themselves
        shared = ((spoke | friends) - blocked) | ({user_id} & set(user_ids))


    # STEP 3: presence is this process's socket table; an
    # import failure reads as everybody offline, never as an
    # error
    # =====================================================
    try:
        from knfapp.chat.events import _connected_users
        online_set = set(_connected_users.values())
    except Exception:
        online_set = set()


    # STEP 4: last-seen rides the SAME gate — an ungated id
    # answers null exactly like an account that never held a
    # socket, so history leaks nothing presence would not
    # =====================================================
    stamps = dict(User.objects.filter(id__in=shared).values_list("id", "last_active_at")) if shared else {}

    result = {uid: (uid in shared and uid in online_set) for uid in user_ids}
    last_seen = {
        uid: stamps[uid].isoformat() if uid in shared and stamps.get(uid) else None
        for uid in user_ids
    }
    return json_response({"online": result, "lastSeen": last_seen})


@require_methods("GET")
@transaction.non_atomic_requests
@require_auth
@ratelimit.per_user("chat_user_search", max_attempts=120)
def search_users(request):
    # Under 2 chars is the keystroke warm-up, not a search —
    # answered empty with no directory hit at all. The floor
    # is judged AFTER clean_param has dropped any control
    # byte: a NUL bound NUL-terminated would collapse the
    # pattern to '%' and page the WHOLE directory, precisely
    # the enumeration this gate exists to stop
    user_id = request.user["id"]
    q = clean_param(request.GET.get("q", "")).strip()
    if len(q) < 2:
        return json_response({"users": []})

    # The case-insensitive lookups carry the folding per
    # engine — SQLite's ASCII LIKE, real folding where LIKE
    # is case-sensitive — and escape the needle's wildcards
    # themselves
    rows = list(
        User.objects.exclude(id=user_id).filter(active=1)
        .filter(Q(username__icontains=q) | Q(display_name__icontains=q))
        .exclude(id__in=UserBlock.objects.filter(blocker_id=user_id).values("blocked_id"))
        .exclude(id__in=UserBlock.objects.filter(blocked_id=user_id).values("blocker_id"))
        .annotate(rank=Case(
            When(username__iexact=q, then=0),
            When(display_name__istartswith=q, then=1),
            default=2,
        ))
        .order_by("rank", Lower("display_name"), "id")
        .values("id", "username", "display_name", "avatar_url", "role")[:20]
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
