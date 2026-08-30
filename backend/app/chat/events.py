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
#    - Auth is the session token in the handshake's auth
#      payload — services/socket.ts passes `auth: { token }`
#      — with the legacy ?token=… query string kept as a
#      fallback so pre-switch clients keep realtime. The
#      lookup itself is auth's resolve_session_token, the
#      SAME function REST goes through, so the socket path
#      gets the sha256 token hashing (migration v13), the
#      aware expiry check, the expired-row purge and the
#      users.active gate instead of the private copy that
#      used to skip them.
#    - A socket authenticates ONCE, at the handshake.
#      Everything that revokes access afterwards (logout,
#      logout-all, password change, admin deactivation)
#      calls disconnect_user_sockets below to cut the
#      sockets already handed out.
#    - Presence is _connected_users, a plain dict in this
#      process (sid → user id). The stack runs ONE Werkzeug
#      worker in threading mode (main.py socketio.run,
#      polling transport only — no simple-websocket), which
#      is the only reason a process-local dict is right.
#      chat/routes.py reads it for create_conversation,
#      the send_message push skip and /online-status, so its
#      VALUES stay bare user ids (set(...) collapses them);
#      the display name lives in the parallel sid-keyed
#      _connected_names.
#    - Connections are capped per user
#      (_MAX_SOCKETS_PER_USER) and per process
#      (_MAX_TOTAL_SOCKETS): one client can no longer open
#      sockets until the process runs out of threads.
#    - Handler dispatch is serialised per connection when
#      create_app passes async_handlers=False to init_app
#      (app/__init__.py): without it the socketio layer
#      spawns a thread PER EVENT before the rate limiter is
#      ever consulted, so the limiter caps nothing that
#      costs anything. Serialised, the limiter is real
#      back-pressure — and the handlers below tolerate it:
#      typing/mark_read only queue behind each other for
#      the one client that sent them. The per-connection
#      engineio packet thread stays either way (inherent to
#      threading mode; only the deferred server swap removes
#      it), so this module's state is still touched from
#      several threads and _socket_rate takes a lock.
#    - Rooms are "conv:<conversation id>". A socket
#      auto-joins every room of its user at connect; later
#      conversations reach it through the client's
#      join_conversation or create_conversation's
#      server-side join_room.
#    - Handlers use flask_socketio.emit (context-bound,
#      include_self honoured); the helpers use
#      socketio.emit (server-level, the actor's own sockets
#      included) because a REST handler has no socket
#      context. They address the room, except
#      emit_read_receipt, which addresses the individual
#      sids a receipt actually concerns.
#    - Every EXPECTED failure inside a handler is a silent
#      drop — no ack, no error event; the mobile app never
#      waits for one. An UNEXPECTED one reaches the
#      on_error_default handler, which logs event, sid and
#      user and answers the offending socket with "error".
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
#    messages_read      — emit_read_receipt (per sid)
#    error              — on_error_default (nobody listens)
############################################################


import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from flask import request as flask_request
from flask_socketio import emit, join_room, leave_room

# The one token → user lookup in the backend: REST reaches it
# through get_current_user, the handshake below calls it
# directly (there is no Authorization header on a socket)
from app.auth.routes import resolve_session_token
from app.database import get_db

logger = logging.getLogger(__name__)

# Presence: sid → user id for every authenticated socket in
# THIS process. One user on two devices is two sids mapping
# to the same id (readers collapse it with set(values())).
# Mutated on other threads while chat/routes.py iterates
# it, hence the list() snapshot there.
_connected_users: dict[str, str] = {}

# The display name that came with the handshake, sid-keyed
# beside _connected_users — handle_typing fans it out
# without going back to the users table on every keystroke.
# Kept SEPARATE on purpose: chat/routes.py reads
# set(_connected_users.values()) as user ids, so those
# values must stay bare ids.
_connected_names: dict[str, str] = {}

# Connection caps. Per user: a client that reconnects in a
# loop (or a script) cannot hold more than this many sockets
# at once, each of which costs an engineio packet thread.
# Per process: the backstop for many users doing it at once,
# a soft cap — an excess handshake is rejected, live sockets
# are left alone.
_MAX_SOCKETS_PER_USER = 5
_MAX_TOTAL_SOCKETS = 500

# Per-user, per-event sliding-window rate limiter for the
# client → server events, keyed (user id, event name) →
# monotonic timestamps. Read-modify-write under
# _socket_rate_lock (several threads reach it: one packet
# thread per connection, plus the REST worker threads —
# chat/routes.py mark_read spends the same budget). Expired
# timestamps are dropped on every check and the store itself
# is bounded to _SOCKET_RATE_MAX_KEYS in LRU order, so it
# can no longer grow with every (user, event) pair seen
# since the last restart. Events missing from the table are
# unlimited.
_socket_rate: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()
_socket_rate_lock = threading.Lock()
_SOCKET_RATE_MAX_KEYS = 4096
_SOCKET_RATE_WINDOW = 10  # seconds
_SOCKET_RATE_LIMITS: dict[str, int] = {
    "typing": 20,         # 20 per 10s
    "stop_typing": 20,    # 20 per 10s
    "mark_read": 10,      # 10 per 10s
    "join_conversation": 10,   # 10 per 10s
    "leave_conversation": 10,  # 10 per 10s
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
# The whole prune-count-append runs under _socket_rate_lock:
# without it two threads that read the same window both saw
# room and both recorded, so N threads could spend one slot
# N times. The store bounds itself in the same pass: a key
# never keeps expired timestamps (each check rewrites it to
# the live window alone) and the LRU tail is dropped once
# the key count passes _SOCKET_RATE_MAX_KEYS, so a caller
# cycling user ids or event names cannot grow it forever.
#
# Used by:
#   - handle_join, handle_leave, handle_typing,
#     handle_stop_typing, handle_mark_read (below) — every
#     client → server handler except the handshake pair
#   - chat/routes.py — mark_read, so the REST twin spends
#     the SAME per-user mark_read budget as the socket event
#     instead of being the free way around it
############################################################

def _socket_rate_check(user_id: str, event: str) -> bool:
    limit = _SOCKET_RATE_LIMITS.get(event)
    if not limit:
        return False

    key = (user_id, event)
    now = time.monotonic()

    with _socket_rate_lock:
        # Lazy prune — the only place a key ever shrinks
        pruned = [t for t in _socket_rate.get(key, ()) if now - t < _SOCKET_RATE_WINDOW]

        if len(pruned) >= limit:
            _socket_rate[key] = pruned
            _socket_rate.move_to_end(key)
            return True

        pruned.append(now)
        _socket_rate[key] = pruned
        _socket_rate.move_to_end(key)

        # Fixed-size LRU: the oldest untouched keys go first
        while len(_socket_rate) > _SOCKET_RATE_MAX_KEYS:
            _socket_rate.popitem(last=False)

        return False








############################################################
# _authenticate_socket
############################################################
#
# Resolves the handshake token — the `auth: { token }`
# payload socket.ts sends in the io() options, or, for
# clients from before that switch, the legacy ?token=
# query parameter — to the caller's user dict, or None for
# a missing, unknown, unencodable, expired or deactivated
# one.
#
# Only the token EXTRACTION lives here: the lookup is auth's
# resolve_session_token, byte for byte the one REST uses.
# That is what closes the three gaps the private copy had —
# it never checked users.active (a flag flipped in DbGate
# rather than through the admin route left the socket
# alive), it never purged the expired row, and it looked the
# RAW token up in a sessions.token column that migration v13
# rewrote to sha256 hashes, which would have failed EVERY
# handshake after the migration. Expiry handling (aware
# comparison, malformed value = expired) and the narrowed
# column list — no password_hash — come with it.
#
# The one thing the lookup cannot be handed is a string with
# no utf-8 encoding: auth's _hash_token does token.encode()
# unguarded, so a lone UTF-16 surrogate would raise past
# handle_connect's try block and be ACCEPTED as a socket
# (see the encodability guard below). Refused here instead.
#
# Used by:
#   - handle_connect (below) — the only caller
############################################################

def _authenticate_socket(auth):
    # The auth payload first; the query string only as the
    # fallback for pre-switch clients still sending ?token=
    token = auth.get("token", "") if isinstance(auth, dict) else ""
    if not token:
        token = flask_request.args.get("token", "")
    if not isinstance(token, str) or not token:
        return None

    # A lone UTF-16 surrogate survives JSON decoding — a client
    # really can send auth: {"token": "\ud800"} — but has no
    # utf-8 encoding, so the sha256 hashing inside the lookup
    # raises UnicodeEncodeError out of _authenticate_socket.
    # handle_connect calls this BEFORE its own try block, so
    # the exception would reach the default error handler,
    # whose None return python-socketio reads as ACCEPT: a
    # socket with no presence row, in no room, that the
    # per-user cap can never see or close. Real tokens are
    # uuid4 hex, so refusing the unencodable costs nothing
    try:
        token.encode()
    except UnicodeEncodeError:
        return None

    return resolve_session_token(token)








############################################################
# disconnect_user_sockets
############################################################
#
# Cuts every live socket of one user, best effort. A socket
# authenticates once at the handshake and is never
# re-checked, so without this a logout, a password change or
# an admin deactivation left the revoked session reading the
# room in realtime until the client felt like reconnecting.
# Iterates a list() snapshot (other threads mutate the dict)
# and asks python-socketio to close each sid; the presence
# rows go too, so /online-status cannot keep showing a user
# whose disconnect handler never ran. Returns the number of
# sockets it closed. A socket layer that is not up yet, or a
# sid that died between the snapshot and the call, is not an
# error — the caller's own route must not fail over it.
#
# Used by:
#   - auth/routes.py — _disconnect_user_sockets, for logout,
#     logout_all and change_password
#   - admin/routes.py — update_user, after committing
#     active = 0
############################################################

def disconnect_user_sockets(user_id: str) -> int:
    # Lazy, like chat/routes.py _get_socketio: the instance is
    # bound in app/__init__.py, which imports this module
    try:
        from app import socketio
    except ImportError:
        logger.warning("Socket layer unavailable — cannot disconnect user=%s", user_id)
        return 0

    closed = 0
    for sid, uid in list(_connected_users.items()):
        if uid != user_id:
            continue
        try:
            socketio.server.disconnect(sid, namespace="/")
            closed += 1
        except Exception:
            logger.warning("Could not disconnect socket sid=%s user=%s", sid, user_id)
        # The disconnect handler normally does this; doing it
        # here too keeps presence honest if it never ran
        _connected_users.pop(sid, None)
        _connected_names.pop(sid, None)

    if closed:
        logger.info("Disconnected %d socket(s) for user=%s", closed, user_id)
    return closed








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
# Every payload-carrying handler takes data=None and opens
# with an isinstance(dict) gate, so an emit with no payload
# (or with a list, or a bare string) is a clean drop instead
# of an AttributeError inside the handler; a conversationId
# must be a non-empty str before it can reach sqlite as a
# bind parameter.
#
# Handlers, in order: handle_socket_error (the default error
# handler), handle_connect, handle_disconnect, handle_join,
# handle_leave, handle_typing, handle_stop_typing,
# handle_mark_read.
#
# Used by:
#   - app/__init__.py — create_app(), after the blueprints
############################################################

def register_socket_events(socketio):






    ############################################################
    # handle_socket_error
    ############################################################
    #
    # The namespace-wide fallback for an UNEXPECTED exception
    # raised inside any handler above. Without it python-
    # socketio swallowed the traceback and the client saw a
    # working socket that silently did nothing — a whole class
    # of chat bugs left no trace anywhere. Logs the event
    # name, the sid and the user the sid maps to (all three
    # are what makes a report reproducible) and answers that
    # one socket with "error"; the event is additive and no
    # client listens for it today, so it changes no contract.
    #
    # Expected rejections — no session, no membership, over
    # quota, bad payload — never come through here: those
    # return early and stay silent by design.
    #
    # Used by:
    #   - python-socketio — every handler on the "/" namespace
    ############################################################

    @socketio.on_error_default
    def handle_socket_error(e):
        sid = getattr(flask_request, "sid", None)
        event = getattr(flask_request, "event", None) or {}
        logger.exception(
            "Socket handler failed: event=%s sid=%s user=%s",
            event.get("message") if isinstance(event, dict) else None,
            sid,
            _connected_users.get(sid),
        )
        # Best effort — a dead socket must not turn one
        # exception into two
        try:
            emit("error", {"message": "Internal error"})
        except Exception:
            pass






    ############################################################
    # handle_connect
    ############################################################
    #
    # "connect" — the handshake. `auth` is the io() options'
    # auth payload ({ token } from socket.ts), handed over
    # by flask-socketio; _authenticate_socket falls back to
    # the legacy ?token= query parameter when it is absent.
    # Returning False rejects the socket: the client sees
    # connect_error, and socket.ts treats a non-transport
    # error as a dead token and stops retrying it (a later
    # connectSocket() re-reads storage). A rejection is
    # logged WITH the sid and the peer address, so a flood is
    # attributable.
    # An accepted socket is joined to every conv:* room of
    # its user in one go — the only automatic room membership
    # there is; conversations created later need
    # join_conversation from the client or
    # create_conversation's server-side join_room.
    #
    # Order matters: the room query runs FIRST and presence is
    # recorded only once it succeeded, because a sid left in
    # _connected_users by a failed handshake is a ghost
    # "online" user forever (nothing ever disconnects a socket
    # that never came up). The body is wrapped for the same
    # reason — anything unexpected pops the sid and rejects.
    #
    # Two caps guard the process: _MAX_SOCKETS_PER_USER live
    # sids for one user, _MAX_TOTAL_SOCKETS in the process.
    # Each socket costs an engineio packet thread, so an
    # uncapped reconnect loop was a thread bomb.
    #
    # The closing "connected" {userId} emit goes to this
    # socket only, and no client listens for it — socket.ts
    # tracks Socket.IO's own 'connect' event instead.
    #
    # Used by:
    #   - services/socket.ts — establish (io() with the token
    #     in the auth payload; polling transport only)
    ############################################################

    @socketio.on("connect")
    def handle_connect(auth=None):
        # STEP 1: handshake auth — resolve_session_token, the
        # same gate REST passes; False is the rejection, there
        # is no error payload for the client to read
        # ====================================================
        sid = flask_request.sid
        user = _authenticate_socket(auth)
        if not user:
            logger.info(
                "Socket connection rejected — invalid token (sid=%s ip=%s)",
                sid, flask_request.remote_addr,
            )
            return False

        user_id = user["id"]


        # STEP 2: connection caps — process first, then the
        # per-user one; both are a plain rejection
        # =================================================
        if len(_connected_users) >= _MAX_TOTAL_SOCKETS:
            logger.warning(
                "Socket connection rejected — process cap %d reached (user=%s ip=%s)",
                _MAX_TOTAL_SOCKETS, user_id, flask_request.remote_addr,
            )
            return False

        if sum(1 for uid in list(_connected_users.values()) if uid == user_id) >= _MAX_SOCKETS_PER_USER:
            logger.info(
                "Socket connection rejected — user cap %d reached (user=%s ip=%s)",
                _MAX_SOCKETS_PER_USER, user_id, flask_request.remote_addr,
            )
            return False


        # STEP 3: auto-join every room of the user's current
        # conversations — a membership added later is not
        # covered here. BEFORE presence: see the banner
        # ==================================================
        try:
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


            # STEP 4: presence — a second device is a second sid
            # for the same user id; the display name rides along
            # so handle_typing needs no query
            # ==================================================
            _connected_users[sid] = user_id
            _connected_names[sid] = user["display_name"] or "Unknown"


            # STEP 5: ack to this socket only — unlistened on the
            # client side
            # ===================================================
            logger.info("Socket connected: user=%s sid=%s rooms=%d", user_id, sid, len(rows))
            emit("connected", {"userId": user_id})
        except Exception:
            # No half-connected sid may survive in the presence
            # table — returning False tears the session down
            _connected_users.pop(sid, None)
            _connected_names.pop(sid, None)
            logger.exception("Socket handshake failed: user=%s sid=%s", user_id, sid)
            return False






    ############################################################
    # handle_disconnect
    ############################################################
    #
    # "disconnect" — drops the sid from the presence table
    # and from the display-name cache beside it. Rooms need
    # no cleanup: flask-socketio leaves them for a closed
    # socket by itself. The user stays "online" for
    # /online-status and the push skip as long as ANY other
    # sid of theirs is still in the table. Fires on every
    # transport drop, so a flaky connection cycles presence
    # off and on.
    #
    # `reason` is python-socketio's disconnect reason, passed
    # explicitly rather than relying on the deprecated
    # zero-argument signature the library still tolerates by
    # inspecting the handler — a pin-free rebuild that drops
    # the fallback would otherwise break presence cleanup
    # outright.
    #
    # Used by:
    #   - services/socket.ts — disconnectSocket (logout,
    #     token change) and every transport drop / reconnect
    #     cycle
    #   - disconnect_user_sockets (above) — the revocation
    #     kill switch closes the socket, this cleans up
    ############################################################

    @socketio.on("disconnect")
    def handle_disconnect(reason=None):
        sid = flask_request.sid
        # pop with a default: a sid missing from the table is
        # not an error, just nothing to log
        user_id = _connected_users.pop(sid, None)
        _connected_names.pop(sid, None)
        if user_id:
            logger.info("Socket disconnected: user=%s sid=%s reason=%s", user_id, sid, reason)






    ############################################################
    # handle_join
    ############################################################
    #
    # "join_conversation" {conversationId} — joins THIS
    # socket to one room after a membership check; a
    # non-member, an unknown sid or a missing id is dropped
    # silently. Meant for a conversation created after the
    # socket connected (handle_connect's auto-join predates
    # it). Capped at 10 per 10 s per user
    # (_socket_rate_check).
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
    def handle_join(data=None):
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "join_conversation"):
            return

        # A payload that is not a dict, or an id that is not a
        # non-empty string, never reaches sqlite as a bind
        if not isinstance(data, dict):
            return
        conv_id = data.get("conversationId")
        if not isinstance(conv_id, str) or not conv_id:
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
    # room. An unknown sid is dropped and the event is
    # capped at 10 per 10 s per user, but membership stays
    # unchecked on purpose: leaving a room the socket is
    # not in is a no-op for Socket.IO.
    #
    # Used by:
    #   - services/socket.ts — leaveConversation exists, but
    #     nothing calls it at the moment: useChatMessages
    #     deliberately stays in the room on unmount, since
    #     leaving would silence the list previews and the
    #     unread badge until the next reconnect
    ############################################################

    @socketio.on("leave_conversation")
    def handle_leave(data=None):
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "leave_conversation"):
            return

        if not isinstance(data, dict):
            return
        conv_id = data.get("conversationId")
        if isinstance(conv_id, str) and conv_id:
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
    # a typer 5 s after its last event.
    #
    # Members only: a non-member's event is dropped silently
    # (sockets stay mute where the REST routes answer 403),
    # so knowing a conversation id is no longer enough to
    # plant a stray "X is typing" in it. The membership gate
    # is the ONLY query left per accepted event: the display
    # name comes from _connected_names, cached at the
    # handshake, instead of a users SELECT on every keystroke
    # heartbeat. A rename mid-session shows the old name to
    # the room until that socket reconnects — the indicator
    # lives five seconds, so nobody can tell.
    #
    # Used by:
    #   - services/socket.ts — emitTyping, from
    #     hooks/chat/useChatComposer on input change; the
    #     broadcast lands in onTyping →
    #     hooks/chat/useTypingIndicator
    ############################################################

    @socketio.on("typing")
    def handle_typing(data=None):
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "typing"):
            return

        if not isinstance(data, dict):
            return
        conv_id = data.get("conversationId")
        if not isinstance(conv_id, str) or not conv_id:
            return

        # Membership decides the fan-out — a non-member is a
        # silent drop
        db = get_db()
        try:
            member = db.execute(
                "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
            if not member:
                return
        finally:
            db.close()

        # "Unknown" only if the handshake cache lost the sid
        # between the presence read above and here
        display_name = _connected_names.get(sid) or "Unknown"

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
    # Same 20-per-10 s cap and the same silent-drop
    # membership gate as handle_typing — a non-member cannot
    # clear (or fake away) an indicator either; one
    # membership lookup per accepted event.
    #
    # Used by:
    #   - services/socket.ts — emitStopTyping, from
    #     hooks/chat/useChatComposer after 3 s of keystroke
    #     silence and on send; lands in onStopTyping →
    #     hooks/chat/useTypingIndicator
    ############################################################

    @socketio.on("stop_typing")
    def handle_stop_typing(data=None):
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "stop_typing"):
            return

        if not isinstance(data, dict):
            return
        conv_id = data.get("conversationId")
        if not isinstance(conv_id, str) or not conv_id:
            return

        # Same membership gate as handle_typing — silent drop
        db = get_db()
        try:
            member = db.execute(
                "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
            if not member:
                return
        finally:
            db.close()

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
    # mark_read). The two no longer carry a copy of the logic
    # each: both call chat/routes.py _apply_mark_read, which
    # does the membership gate, the bounded receipt scan, the
    # single set-based INSERT and the last_read_at move inside
    # ONE BEGIN IMMEDIATE transaction — no unbounded scan, no
    # insert-per-row under the write lock, and no window
    # between the gate and the writes for a departing member
    # to slip through. Only the transport gates stay here:
    # unknown sid, quota, payload, and a None back from the
    # helper (not a participant) is a silent drop where the
    # REST twin answers 403.
    #
    # When at least one receipt was new, "messages_read" goes
    # out through emit_read_receipt — targeted at the affected
    # senders and the reader's own sockets, not the whole
    # room. Capped at 10 per 10 s per user, a budget now
    # SHARED with the REST twin.
    #
    # The mobile app fires this AND the REST call back to
    # back on every read (socket for speed, REST as the
    # fallback), so normally the second one finds nothing
    # new and stays quiet; if the two do interleave, BEGIN
    # IMMEDIATE serialises them and the loser sees no unread
    # rows left to claim.
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
    def handle_mark_read(data=None):
        # STEP 1: identity, quota and payload gates — each one
        # a silent drop
        # ====================================================
        sid = flask_request.sid
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "mark_read"):
            return

        if not isinstance(data, dict):
            return
        conv_id = data.get("conversationId")
        if not isinstance(conv_id, str) or not conv_id:
            return


        # STEP 2: the shared helper does both stores in one
        # transaction. `now` is taken here, right before it, so
        # a message landing mid-call is bounded out of BOTH.
        # Imported lazily: chat/routes.py reaches back into this
        # module the same way, and only one of the two may bind
        # at import time
        # =====================================================
        from app.chat.routes import _apply_mark_read

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        db = get_db()
        try:
            newly_read_ids = _apply_mark_read(db, conv_id, user_id, now)
        finally:
            db.close()


        # STEP 3: broadcast only when something changed, and
        # only after the commit so a listener's GET sees the
        # rows. None = not a participant, nothing was written
        # ==================================================
        if newly_read_ids:
            emit_read_receipt(socketio, conv_id, user_id, newly_read_ids)








############################################################
# emit_new_message
############################################################
#
# "new_message" with send_message's response payload to
# the whole room, the sender's own sockets included — the
# room hook dedupes that echo against its optimistic
# bubble, whichever of echo and REST response lands first.
# Server-level emit: the REST handler has no socket
# context. A member who is online but not in THIS room
# (connected before the conversation existed, no join yet)
# hears nothing here — but send_message now keys its push
# skip on the room's own sids rather than on global
# presence, so that member gets the push instead of
# silence; create_conversation's server-side join_room
# still closes the gap for the live case.
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
# unreact. The REST responses return the same array (the
# acting client reconciles from its response body); this
# event mirrors it to every other client, and the actor's
# own sockets get it too.
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

def emit_message_edited(socketio, conv_id: str, msg_id: str, text: str, edited_at: str):
    # "message_edited" {conversationId, messageId, text, editedAt}
    # — every client in the room replaces the row's text in place
    socketio.emit(
        "message_edited",
        {"conversationId": conv_id, "messageId": msg_id, "text": text, "editedAt": edited_at},
        to=f"conv:{conv_id}",
    )


def emit_message_updated(socketio, conv_id: str, msg_id: str, patch: dict):
    # "message_updated" {conversationId, messageId, patch} — the
    # server filled something in after the send (today: the link
    # preview card); every client merges the patch into the row
    socketio.emit(
        "message_updated",
        {"conversationId": conv_id, "messageId": msg_id, "patch": patch},
        to=f"conv:{conv_id}",
    )


def emit_conversation_updated(socketio, conv_id: str, patch: dict):
    # "conversation_updated" {conversationId, patch} — a room
    # setting changed (today: the disappearing-messages TTL);
    # clients merge the patch into the conversation meta they hold
    socketio.emit(
        "conversation_updated",
        {"conversationId": conv_id, "patch": patch},
        to=f"conv:{conv_id}",
    )


def emit_message_deleted(socketio, conv_id: str, msg_id: str):
    socketio.emit(
        "message_deleted",
        {"conversationId": conv_id, "messageId": msg_id},
        to=f"conv:{conv_id}",
    )








############################################################
# _read_receipt_sids
############################################################
#
# The sids a "messages_read" actually concerns: the sockets
# of the people who SENT the read messages (their bubbles
# flip to read) plus every socket of the reader (a second
# device follows along). Returns None when it cannot tell —
# an empty id list, a list too long for one IN clause, or a
# DB error — and the caller falls back to the room.
#
# Used by:
#   - emit_read_receipt (below) — the only caller
############################################################

def _read_receipt_sids(reader_id: str, message_ids: list):
    # 900 keeps the IN list under SQLite's default variable
    # limit; the caller's cap is well below it
    if not message_ids or len(message_ids) > 900:
        return None

    try:
        db = get_db()
        try:
            placeholders = ",".join("?" * len(message_ids))
            rows = db.execute(
                f"SELECT DISTINCT sender_id FROM messages WHERE id IN ({placeholders})",
                list(message_ids),
            ).fetchall()
        finally:
            db.close()
    except Exception:
        logger.exception("Read-receipt targeting failed for reader=%s", reader_id)
        return None

    interested = {row["sender_id"] for row in rows}
    interested.add(reader_id)
    # list() snapshot: other threads connect and disconnect
    return [sid for sid, uid in list(_connected_users.items()) if uid in interested]








############################################################
# emit_read_receipt
############################################################
#
# "messages_read" {conversationId, readerId, messageIds}:
# reader_id now holds a receipt for each id. Server-level
# emit (the REST caller has no socket context), addressed
# per sid rather than to the whole room: only the senders of
# those messages and the reader's own other devices can do
# anything with the event — everyone else used to receive it
# and answer with an unread-count refetch, which made one
# read cost O(members) requests. The payload is unchanged,
# and a socket that gets nothing here was going to ignore it
# anyway. If the targets cannot be resolved the room emit is
# still the fallback, so a receipt is never lost.
#
# Used by:
#   - handle_mark_read (above) — the socket path
#   - chat/routes.py — mark_read, the REST path; both land
#     in socket.ts onMessagesRead →
#     hooks/chat/useChatMessages (own bubbles → read) and
#     hooks/useUnreadCount (recount)
############################################################

def emit_read_receipt(socketio, conv_id: str, reader_id: str, message_ids: list):
    payload = {
        "conversationId": conv_id,
        "readerId": reader_id,
        "messageIds": message_ids,
    }

    sids = _read_receipt_sids(reader_id, message_ids)
    if sids is None:
        socketio.emit("messages_read", payload, to=f"conv:{conv_id}")
        return

    # Each sid is its own room in Socket.IO, so `to=sid` is
    # the one-socket address
    for sid in sids:
        socketio.emit("messages_read", payload, to=sid)
