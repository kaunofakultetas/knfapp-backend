############################################################
#  [*] Chat events — Socket.IO handshake, rooms, fan-out
#
#  Live side of messaging, bound to the module-level
#  SocketIO instance by create_app() (app/__init__.py) right
#  after the blueprints. The REST routes in chat/routes.py
#  write the rows and commit; the emit_* helpers at the
#  bottom of this file do the fan-out, so a client without
#  a socket still sees everything on its next GET.
#
#  Facts the rest of the stack leans on:
#    - Auth is the session token in the handshake QUERY
#      STRING (?token=…), not an Authorization header —
#      services/socket.ts passes `query: { token }`. The
#      lookup is a copy of auth get_current_user WITHOUT
#      the users.active check and WITHOUT the expired-row
#      purge; the get_current_user import itself is dead.
#    - Presence is _connected_users, a plain dict in this
#      process (sid → user id). The stack runs ONE Werkzeug
#      worker in threading mode (main.py socketio.run,
#      polling transport only — no simple-websocket), which
#      is the only reason a process-local dict is right.
#      chat/routes.py reads it for create_conversation,
#      the send_message push skip and /online-status.
#    - Rooms are "conv:<conversation id>". A socket
#      auto-joins every room of its user at connect; later
#      conversations reach it through the client's
#      join_conversation or create_conversation's
#      server-side join_room.
#    - Handlers use flask_socketio.emit (context-bound,
#      include_self honoured); the helpers use
#      socketio.emit (server-level: every socket in the
#      room, the actor's own included) because a REST
#      handler has no socket context.
#    - Every failure inside a handler is a silent drop —
#      no ack, no error event; the mobile app never waits
#      for one.
#    - Caddy proxies /socket.io/* to knfapp-backend:8000;
#      ALLOWED_ORIGINS (docker-compose) doubles as the
#      Socket.IO Origin check.
#
#  Events, client → server:
#    connect            — token handshake, presence, auto-join
#    disconnect         — drop from the presence table
#    join_conversation  — join one room (member check)
#    leave_conversation — leave one room (no checks)
#    typing             — fan out user_typing
#    stop_typing        — fan out user_stop_typing
#    mark_read          — receipts + messages_read (REST twin)
#
#  Events, server → client:
#    connected          — handshake ack (nobody listens)
#    user_typing        — handle_typing
#    user_stop_typing   — handle_stop_typing
#    new_message        — emit_new_message
#    reaction_update    — emit_reaction_update
#    message_deleted    — emit_message_deleted
#    messages_read      — emit_read_receipt
############################################################


import logging
import time
from collections import defaultdict
from datetime import datetime

from flask import request as flask_request
from flask_socketio import emit, join_room, leave_room

# Dead import — the socket path re-implements the session
# lookup as _authenticate_socket and never calls this
from app.auth.routes import get_current_user
from app.database import get_db

logger = logging.getLogger(__name__)

# Presence: sid → user id for every authenticated socket in
# THIS process. One user on two devices is two sids mapping
# to the same id (readers collapse it with set(values())).
# Mutated on other threads while chat/routes.py iterates
# it, hence the list() snapshot there.
_connected_users: dict[str, str] = {}

# Per-user, per-event sliding-window rate limiter for the
# client → server events, keyed (user id, event name) →
# monotonic timestamps. A key is pruned lazily on its own
# next event and never evicted, so the dict grows with
# every distinct (user, event) pair seen since the last
# restart. Events missing from the table are unlimited.
_socket_rate: dict[tuple[str, str], list[float]] = defaultdict(list)
_SOCKET_RATE_WINDOW = 10  # seconds
_SOCKET_RATE_LIMITS: dict[str, int] = {
    "typing": 20,         # 20 per 10s
    "stop_typing": 20,    # 20 per 10s
    "mark_read": 10,      # 10 per 10s
    "join_conversation": 10,   # dead — handle_join never checks
    "leave_conversation": 10,  # dead — handle_leave never checks
}








############################################################
# _socket_rate_check
############################################################
#
# True when the event must be REJECTED: the user has already
# spent the event's quota inside the last 10 s. Every call
# that is NOT rejected is recorded, so accepted events fill
# the window and rejected ones do not. time.monotonic — a
# wall-clock jump never opens or closes a window. An event
# with no entry in _SOCKET_RATE_LIMITS always passes.
#
# Used by:
#   - handle_typing, handle_stop_typing, handle_mark_read
#     (below). handle_join / handle_leave do NOT call it,
#     although the table carries their limits
############################################################

def _socket_rate_check(user_id: str, event: str) -> bool:
    limit = _SOCKET_RATE_LIMITS.get(event)
    if not limit:
        return False
    key = (user_id, event)
    now = time.monotonic()
    timestamps = _socket_rate[key]
    # Lazy prune — the only place a key ever shrinks
    _socket_rate[key] = [t for t in timestamps if now - t < _SOCKET_RATE_WINDOW]
    if len(_socket_rate[key]) >= limit:
        return True
    _socket_rate[key].append(now)
    return False








############################################################
# _authenticate_socket
############################################################
#
# Resolves the handshake's ?token= query parameter (the
# only place the mobile app puts it — see socket.ts
# establish) to the caller's FULL users row as a dict,
# password_hash included, or None for a missing, unknown or
# expired token. A re-implementation of auth
# get_current_user with two gaps: users.active is NOT
# checked — a deactivated account whose session row
# survived (flag flipped in DbGate rather than through the
# admin route, which also deletes the sessions) can still
# connect — and an expired row is NOT deleted, only
# ignored; the lazy purge lives in get_current_user alone.
#
# Expiry: expires_at was written by register/login as an
# aware UTC ISO string ("+00:00"); the offset is stripped
# and compared against naive utcnow() — both sides are UTC,
# which is the only reason the naive comparison is right.
#
# Used by:
#   - handle_connect (below) — the only caller
############################################################

def _authenticate_socket():
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

        # Both sides naive UTC after the strip — see the banner
        expires = datetime.fromisoformat(session["expires_at"]).replace(tzinfo=None)
        if expires < datetime.utcnow():
            return None

        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        return dict(user) if user else None
    finally:
        db.close()








############################################################
# register_socket_events
############################################################
#
# Binds every client → server handler below to the SocketIO
# instance on the default "/" namespace. Called exactly once
# by create_app(); the handlers are closures over `socketio`
# so handle_mark_read can hand its broadcast to
# emit_read_receipt. Inside a handler flask-socketio exposes
# the socket id as flask_request.sid, and the handlers that
# care resolve it through _connected_users — a sid the
# connect handler never recorded is dropped without a word.
#
# Payloads are read with data.get(...) and no guard: an emit
# without a payload (data is None) raises inside the handler
# instead of being dropped cleanly.
#
# Handlers, in order: handle_connect, handle_disconnect,
# handle_join, handle_leave, handle_typing,
# handle_stop_typing, handle_mark_read.
#
# Used by:
#   - app/__init__.py — create_app(), after the blueprints
############################################################

def register_socket_events(socketio):






    ############################################################
    # handle_connect
    ############################################################
    #
    # "connect" — the handshake. Returning False rejects the
    # socket: the client sees connect_error, and socket.ts
    # treats a non-transport error as a dead token and stops
    # retrying it (a later connectSocket() re-reads storage).
    # An accepted socket lands in the presence table and is
    # joined to every conv:* room of its user in one go — the
    # only automatic room membership there is; conversations
    # created later need join_conversation from the client
    # or create_conversation's server-side join_room.
    #
    # The closing "connected" {userId} emit goes to this
    # socket only, and no client listens for it — socket.ts
    # tracks Socket.IO's own 'connect' event instead.
    #
    # Used by:
    #   - services/socket.ts — establish (io() with the token
    #     in the query; polling transport only)
    ############################################################

    @socketio.on("connect")
    def handle_connect():
        # STEP 1: handshake auth — False is the rejection; there
        # is no error payload for the client to read
        # ======================================================
        user = _authenticate_socket()
        if not user:
            logger.info("Socket connection rejected — invalid token")
            return False


        # STEP 2: presence — a second device is a second sid
        # for the same user id
        # ==================================================
        sid = flask_request.sid
        user_id = user["id"]
        _connected_users[sid] = user_id


        # STEP 3: auto-join every room of the user's current
        # conversations — a membership added later is not
        # covered here
        # ==================================================
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


        # STEP 4: ack to this socket only — unlistened on the
        # client side
        # ===================================================
        logger.info("Socket connected: user=%s sid=%s rooms=%d", user_id, sid, len(rows))
        emit("connected", {"userId": user_id})






    ############################################################
    # handle_disconnect
    ############################################################
    #
    # "disconnect" — drops the sid from the presence table.
    # Rooms need no cleanup: flask-socketio leaves them for a
    # closed socket by itself. The user stays "online" for
    # /online-status and the push skip as long as ANY other
    # sid of theirs is still in the table. Fires on every
    # transport drop, so a flaky connection cycles presence
    # off and on.
    #
    # Used by:
    #   - services/socket.ts — disconnectSocket (logout,
    #     token change) and every transport drop / reconnect
    #     cycle
    ############################################################

    @socketio.on("disconnect")
    def handle_disconnect():
        sid = flask_request.sid
        # pop with a default: a sid missing from the table is
        # not an error, just nothing to log
        user_id = _connected_users.pop(sid, None)
        if user_id:
            logger.info("Socket disconnected: user=%s", user_id)






    ############################################################
    # handle_join
    ############################################################
    #
    # "join_conversation" {conversationId} — joins THIS
    # socket to one room after a membership check; a
    # non-member, an unknown sid or a missing id is dropped
    # silently. Meant for a conversation created after the
    # socket connected (handle_connect's auto-join predates
    # it). _SOCKET_RATE_LIMITS carries a 10-per-10 s entry
    # for this event, but the handler never calls
    # _socket_rate_check.
    #
    # Used by:
    #   - services/socket.ts — joinConversation, emitted by
    #     hooks/chat/useChatMessages on chat-room mount once
    #     connectSocket() resolves
    #   - chat/routes.py — create_conversation does the same
    #     server-side (join_room with sid=) for the OTHER
    #     online members, who never emit this
    ############################################################

    @socketio.on("join_conversation")
    def handle_join(data):
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return

        conv_id = data.get("conversationId")
        if not conv_id:
            return

        # Membership decides the join — the client can name
        # any id
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






    ############################################################
    # handle_leave
    ############################################################
    #
    # "leave_conversation" {conversationId} — leaves the
    # room, no questions asked: no presence lookup, no
    # membership check, and the 10-per-10 s entry in
    # _SOCKET_RATE_LIMITS is never consulted. Harmless —
    # leaving a room the socket is not in is a no-op for
    # Socket.IO.
    #
    # Used by:
    #   - services/socket.ts — leaveConversation exists, but
    #     nothing calls it at the moment: useChatMessages
    #     deliberately stays in the room on unmount, since
    #     leaving would silence the list previews and the
    #     unread badge until the next reconnect
    ############################################################

    @socketio.on("leave_conversation")
    def handle_leave(data):
        conv_id = data.get("conversationId")
        if conv_id:
            leave_room(f"conv:{conv_id}")






    ############################################################
    # handle_typing
    ############################################################
    #
    # "typing" {conversationId} → "user_typing"
    # {conversationId, userId, displayName} to everyone else
    # in the room (include_self=False, so the typist never
    # sees their own indicator). Capped at 20 per 10 s per
    # user — the composer heartbeats every 2 s while
    # keystrokes keep coming, and the indicator hook expires
    # a typer 5 s after its last event. One users lookup per
    # accepted event, for the display name.
    #
    # No membership check: the emit reaches whoever is in
    # the room, so any connected user who knows a
    # conversation id can make its members see a stray
    # "X is typing".
    #
    # Used by:
    #   - services/socket.ts — emitTyping, from
    #     hooks/chat/useChatComposer on input change; the
    #     broadcast lands in onTyping →
    #     hooks/chat/useTypingIndicator
    ############################################################

    @socketio.on("typing")
    def handle_typing(data):
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "typing"):
            return

        conv_id = data.get("conversationId")
        if not conv_id:
            return

        # "Unknown" only if the users row vanished under a
        # live session
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






    ############################################################
    # handle_stop_typing
    ############################################################
    #
    # "stop_typing" {conversationId} → "user_stop_typing"
    # {conversationId, userId} to everyone else in the room.
    # Same 20-per-10 s cap and the same missing membership
    # check as handle_typing; no DB access.
    #
    # Used by:
    #   - services/socket.ts — emitStopTyping, from
    #     hooks/chat/useChatComposer after 3 s of keystroke
    #     silence and on send; lands in onStopTyping →
    #     hooks/chat/useTypingIndicator
    ############################################################

    @socketio.on("stop_typing")
    def handle_stop_typing(data):
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "stop_typing"):
            return

        conv_id = data.get("conversationId")
        if conv_id:
            emit(
                "user_stop_typing",
                {"conversationId": conv_id, "userId": user_id},
                to=f"conv:{conv_id}",
                include_self=False,
            )






    ############################################################
    # handle_mark_read
    ############################################################
    #
    # "mark_read" {conversationId} — the socket twin of
    # PUT /api/chat/conversations/<id>/read (chat/routes.py
    # mark_read) with the same logic copied — change both.
    # Moves the caller's last_read_at to now (the
    # unread-count store) and writes a message_reads receipt
    # for every message from OTHER people that lacks one
    # (the status/readBy store); when at least one receipt
    # was new, "messages_read" goes to the whole room — the
    # reader's own sockets included, since emit_read_receipt
    # uses the server-level emit. Non-members, unknown sids
    # and a missing id are dropped silently where the REST
    # twin answers 403/400. Capped at 10 per 10 s per user.
    #
    # The mobile app fires this AND the REST call back to
    # back on every read (socket for speed, REST as the
    # fallback), so normally the second one finds nothing
    # new and stays quiet; in threading mode the two can
    # interleave before either inserts, and then both
    # broadcast the same ids.
    #
    # Used by:
    #   - services/socket.ts — emitMarkRead, from
    #     hooks/chat/useChatMessages on room open, on every
    #     incoming foreign message while open, and on
    #     resync; the broadcast lands in onMessagesRead →
    #     useChatMessages (own bubbles → read) and
    #     hooks/useUnreadCount (recount)
    ############################################################

    @socketio.on("mark_read")
    def handle_mark_read(data):
        # STEP 1: identity, quota and payload gates — each one
        # a silent drop
        # ====================================================
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "mark_read"):
            return

        conv_id = data.get("conversationId")
        if not conv_id:
            return


        # STEP 2: one `now` for both stores, so last_read_at and
        # the receipts agree to the microsecond; then the
        # membership gate
        # ======================================================
        now = datetime.utcnow().isoformat()
        db = get_db()
        try:
            participant = db.execute(
                "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
            if not participant:
                return


            # STEP 3: last_read_at — what the list badges and
            # /unread-count compare created_at strings against
            # ================================================
            db.execute(
                "UPDATE conversation_participants SET last_read_at = ? WHERE conversation_id = ? AND user_id = ?",
                (now, conv_id, user_id),
            )


            # STEP 4: a receipt per foreign message without one.
            # INSERT OR IGNORE, so a receipt the REST twin raced
            # in first is not an error — but its id is still
            # re-broadcast, the list is built before the insert
            # ==================================================
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


            # STEP 5: broadcast only when something changed, and
            # only after the commit so a listener's GET sees the
            # rows
            # ==================================================
            if newly_read_ids:
                emit_read_receipt(socketio, conv_id, user_id, newly_read_ids)
        finally:
            db.close()








############################################################
# emit_new_message
############################################################
#
# "new_message" with send_message's response payload to
# the whole room, the sender's own sockets included — the
# room hook dedupes that echo against its optimistic
# bubble, whichever of echo and REST response lands first.
# Server-level emit: the REST handler has no socket
# context. A member who is online but not in the room
# (connected before the conversation existed, no join yet)
# hears nothing AND gets no push, since send_message skips
# push for every online user — create_conversation's
# server-side join_room exists to close that gap.
#
# Used by:
#   - chat/routes.py — send_message, after the commit; lands
#     in socket.ts onNewMessage → hooks/chat/useChatMessages
#     (append), hooks/useUnreadCount (badge bump + recount),
#     app/(main)/tabs/messages.tsx (list refresh)
############################################################

def emit_new_message(socketio, conv_id: str, message_data: dict):
    socketio.emit("new_message", message_data, to=f"conv:{conv_id}")








############################################################
# emit_reaction_update
############################################################
#
# "reaction_update" {conversationId, messageId, reactions}
# — the AUTHORITATIVE reactions array after a react or
# unreact; the REST responses return only {ok, emoji}, so
# this event IS the reaction state, and the actor's own
# sockets get it too.
#
# Used by:
#   - chat/routes.py — _emit_reaction_update, for
#     react_to_message and remove_reaction; lands in
#     socket.ts onReactionUpdate →
#     hooks/chat/useChatMessages
############################################################

def emit_reaction_update(socketio, conv_id: str, msg_id: str, reactions: list):
    socketio.emit(
        "reaction_update",
        {"conversationId": conv_id, "messageId": msg_id, "reactions": reactions},
        to=f"conv:{conv_id}",
    )








############################################################
# emit_message_deleted
############################################################
#
# "message_deleted" {conversationId, messageId} after an
# unsend. The row survives with text cleared and deleted_at
# set (chat/routes.py delete_message); clients swap the
# body for the placeholder. Re-broadcast on a repeated
# delete of the same message — harmless.
#
# Used by:
#   - chat/routes.py — delete_message; lands in socket.ts
#     onMessageDeleted → hooks/chat/useChatMessages (live
#     unsend) and app/(main)/tabs/messages.tsx (last-message
#     preview → placeholder)
############################################################

def emit_message_deleted(socketio, conv_id: str, msg_id: str):
    socketio.emit(
        "message_deleted",
        {"conversationId": conv_id, "messageId": msg_id},
        to=f"conv:{conv_id}",
    )








############################################################
# emit_read_receipt
############################################################
#
# "messages_read" {conversationId, readerId, messageIds}:
# reader_id now holds a receipt for each id. Server-level
# emit — every socket in the room, the reader's own
# included (there is no include_self here), so a reader's
# second device learns of the read too.
#
# Used by:
#   - handle_mark_read (above) — the socket path
#   - chat/routes.py — mark_read, the REST path; both land
#     in socket.ts onMessagesRead →
#     hooks/chat/useChatMessages (own bubbles → read) and
#     hooks/useUnreadCount (recount)
############################################################

def emit_read_receipt(socketio, conv_id: str, reader_id: str, message_ids: list):
    socketio.emit(
        "messages_read",
        {
            "conversationId": conv_id,
            "readerId": reader_id,
            "messageIds": message_ids,
        },
        to=f"conv:{conv_id}",
    )
