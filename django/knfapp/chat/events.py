############################################################
#  [*] Chat events — Socket.IO handshake, rooms, fan-out
#
#  Live side of messaging, bound to the Server instance in
#  socket.py. The REST views in api/views.py write the rows
#  and commit; the emit_* helpers at the bottom of this
#  file do the fan-out, so a client without a socket still
#  sees everything on its next GET.
#
#  Facts the rest of the stack leans on:
#    - Auth is the session token in the handshake's auth
#      payload (the client passes `auth: { token }`), with
#      a ?token=… query-string fallback for older clients.
#      The lookup is auth's resolve_session_token, the SAME
#      function REST goes through: sha256 token hashing,
#      aware expiry, expired-row purge, users.active gate.
#    - A socket authenticates ONCE, at the handshake.
#      Everything that revokes access afterwards (logout,
#      logout-all, password change, admin deactivation,
#      erasure) calls disconnect_user_sockets below — the
#      target of the guarded lazy imports elsewhere. The
#      cut is scoped like the revocation: logout takes the
#      presented SESSION's sockets, a password change every
#      other session's, the account-wide paths every sid.
#    - Presence is _connected_users, a plain dict in this
#      process (sid → user id). Gunicorn runs ONE gthread
#      worker (see socket.py), which is the only reason a
#      process-local dict is right. api/views.py reads it
#      for create_conversation, the send_message push skip
#      and /online-status; the display name lives in the
#      sid-keyed _connected_names beside it, the handshake's
#      session hash in _connected_sessions.
#    - Connections are capped per user and per process; an
#      excess handshake is rejected, live sockets are left
#      alone.
#    - python-socketio conventions: handlers receive their
#      sid explicitly, environ carries the WSGI request of
#      the polling transport, emits address rooms by name
#      and skip_sid excludes the actor. Rooms are
#      "conv:<conversation id>"; a socket auto-joins every
#      room of its user at connect.
#    - Every EXPECTED failure inside a handler is a silent
#      drop — no ack, no error event; the mobile app never
#      waits for one. An UNEXPECTED one reaches
#      handle_socket_error, which logs event, sid and user.
#    - Database access runs in whatever engineio thread the
#      event arrived on — connections are closed after each
#      handler that opened one, so a quiet socket never
#      parks an idle connection. The close is skipped inside
#      an atomic block (_release_connection): there it would
#      not recycle an idle connection but kill a live
#      transaction — on PostgreSQL the caller's next query
#      answers "the connection is closed".
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
#    user_typing / user_stop_typing
#    new_message / message_edited / message_updated
#    message_deleted / reaction_update
#    conversation_updated
#    messages_read      — emit_read_receipt (per sid)
#    error              — handle_socket_error (nobody listens)
############################################################


import logging
import threading
import time
from collections import OrderedDict
from urllib.parse import parse_qs

from django.db import close_old_connections, connection

# Refusals must carry a REASON the client can triage: only an
# auth refusal may show "session expired" — a capacity or
# transient one renders as retryable. A bare `return False`
# sends the stock message, indistinguishable from a bad token.
from socketio.exceptions import ConnectionRefusedError

from knfapp.chat.models import ConversationParticipant, Message

# The one token → user lookup in the backend: REST reaches it
# through get_current_user, the handshake below calls it
# directly (there is no Authorization header on a socket).
# hash_token is what the sessions table stores — the handshake
# keeps the same digest beside the sid so a revocation can
# name the session it is cutting
from knfapp.users.auth import hash_token, resolve_session_token

logger = logging.getLogger(__name__)

# Presence: sid → user id for every authenticated socket in
# THIS process. One user on two devices is two sids mapping
# to the same id (readers collapse it with set(values())).
# Mutated on other threads while api/views.py iterates it,
# hence the list() snapshots there.
_connected_users: dict = {}

# The display name that came with the handshake, sid-keyed
# beside _connected_users — handle_typing fans it out
# without going back to the users table on every keystroke.
# Kept SEPARATE on purpose: api/views.py reads
# set(_connected_users.values()) as user ids, so those
# values must stay bare ids.
_connected_names: dict = {}

# The sha256 of the session token the handshake presented —
# the sessions.token digest — sid-keyed beside the two above.
# It is what lets a logout cut the sockets of the ONE session
# it revoked and a password change cut every session's but
# the caller's, instead of every socket of the account. Kept
# SEPARATE for the same reason as _connected_names.
_connected_sessions: dict = {}

# Connection caps. Per user: NEWEST WINS — a fresh handshake
# past the cap evicts the user's oldest socket instead of
# being refused. The slots a phone burns through are mostly
# zombies (polling sockets that died without a clean close
# and sit in the table until the ping timeout reaps them);
# refusing the newcomer told a freshly logged-in user their
# session was dead while five corpses held the door. Per
# process: the backstop for many users at once — an excess
# handshake is refused with reason 'busy', live sockets are
# left alone.
_MAX_SOCKETS_PER_USER = 5
_MAX_TOTAL_SOCKETS = 500

# Per-user, per-event sliding-window rate limiter for the
# client → server events, keyed (user id, event name) →
# monotonic timestamps. Read-modify-write under
# _socket_rate_lock (several threads reach it: one packet
# thread per connection, plus the REST worker threads —
# api/views.py mark_read spends the same budget). Expired
# timestamps are dropped on every check and the store itself
# is bounded to _SOCKET_RATE_MAX_KEYS in LRU order. Events
# missing from the table are unlimited.
_socket_rate: "OrderedDict" = OrderedDict()
_socket_rate_lock = threading.Lock()
_SOCKET_RATE_MAX_KEYS = 4096
_SOCKET_RATE_WINDOW = 10  # seconds
_SOCKET_RATE_LIMITS: dict = {
    "typing": 20,         # 20 per 10s
    "stop_typing": 20,    # 20 per 10s
    "mark_read": 10,      # 10 per 10s
    "join_conversation": 10,   # 10 per 10s
    "leave_conversation": 10,  # 10 per 10s
}


def _is_member(conv_id, user_id):
    return ConversationParticipant.objects.filter(
        conversation_id=conv_id, user_id=user_id,
    ).exists()








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
# without it two threads reading the same window would both
# see room and both record — one slot spent N times. The
# store bounds itself in the same pass: a key
# never keeps expired timestamps and the LRU tail is dropped
# once the key count passes _SOCKET_RATE_MAX_KEYS.
#
# Used by:
#   - every client → server handler except the handshake pair
#   - api/views.py — mark_read, so the REST twin spends the
#     SAME per-user budget as the socket event instead of
#     being the free way around it
############################################################

def _socket_rate_check(user_id, event) -> bool:
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


def reset_socket_state():
    # The test seam: presence, names, sessions and the rate windows
    with _socket_rate_lock:
        _socket_rate.clear()
    _connected_users.clear()
    _connected_names.clear()
    _connected_sessions.clear()








############################################################
# _release_connection
############################################################
#
# The connection hygiene every handler runs on its way out.
# An engineio packet thread lives as long as its socket and
# runs on autocommit outside any transaction, so after a
# handler that touched the database the thread's connection
# is closed — a quiet socket never parks an idle one, and
# the next event reconnects. Inside an atomic block the same
# close_old_connections is the wrong tool: it sees
# autocommit off against AUTOCOMMIT=True and drops the LIVE
# connection under the open transaction — harmless on the
# in-memory SQLite the suite runs on (its close() is a
# no-op), fatal on PostgreSQL, where the caller's next query
# answers "the connection is closed". The TestCase harness
# wraps every test in one such block, and so would any
# future in-request caller of a handler; the in_atomic_block
# check tells the two apart, the same way
# notifications/push.py _deactivate_tokens does.
#
# Used by:
#   - register_socket_events (below) — _guarded, the
#     handshake's finally arm and handle_disconnect
############################################################

def _release_connection():
    # Only a thread outside any transaction may recycle its
    # connection — inside one the close would abort it
    if not connection.in_atomic_block:
        close_old_connections()








############################################################
# _stamp_last_active
############################################################
#
# users.last_active_at ← now, best effort: the "matytas (-a)
# prieš X" a direct chat's header shows through the gated
# online-status route. Called on BOTH socket edges — connect
# and disconnect — so the stored stamp is never older than
# the start of the user's latest online stretch even if the
# process dies mid-session. Presence plumbing must never
# take a socket down, so a database hiccup logs and yields.
#
# Used by:
#   - register_socket_events (below) — handle_connect and
#     handle_disconnect
############################################################

def _stamp_last_active(user_id):
    try:
        from knfapp.common.timestamps import utc_now
        from knfapp.users.models import User
        User.objects.filter(id=user_id).update(last_active_at=utc_now())
    except Exception:
        logger.exception("Could not stamp last_active_at for user=%s", user_id)








############################################################
# _authenticate_socket
############################################################
#
# Resolves the handshake token — the `auth: { token }`
# payload, or the ?token= query-parameter fallback older
# clients send, off the WSGI environ — to a (user dict,
# session hash) pair, or None for a missing, unknown,
# unencodable, expired or deactivated one. Only the token
# EXTRACTION lives here: the lookup is auth's
# resolve_session_token, byte for byte the one REST uses.
# The second element is hash_token(token), the digest the
# sessions row stores — handle_connect files it beside the
# sid so a logout can later cut exactly this session's
# sockets; the raw token itself never leaves this frame.
#
# The one thing the lookup cannot be handed is a string with
# no utf-8 encoding: the sha256 hashing inside it does
# token.encode() unguarded, so a lone UTF-16 surrogate would
# raise past handle_connect and be ACCEPTED as a socket with
# no presence row. Refused here instead.
#
# Used by:
#   - handle_connect (below) — the only caller
############################################################

def _authenticate_socket(environ, auth):
    token = auth.get("token", "") if isinstance(auth, dict) else ""
    if not token:
        query = parse_qs(environ.get("QUERY_STRING", "") if environ else "")
        token = (query.get("token") or [""])[0]
    if not isinstance(token, str) or not token:
        return None

    try:
        token.encode()
    except UnicodeEncodeError:
        return None

    user = resolve_session_token(token)
    if not user:
        return None
    return user, hash_token(token)








############################################################
# disconnect_user_sockets
############################################################
#
# Cuts the live sockets of one user, best effort, scoped to
# the SESSION the caller revoked. A socket authenticates
# once at the handshake and is never re-checked, so without
# this a logout, a password change, an admin deactivation or
# an erasure would leave the revoked session reading the
# room in realtime until the client felt like reconnecting.
# The scope keyword picks the sids, matched on the handshake
# hash filed in _connected_sessions:
#
#   only_session=<hash>    just that session's sockets — a
#                          single-device logout, so the
#                          account's OTHER devices keep their
#                          realtime along with their sessions
#   except_session=<hash>  every socket of the user but that
#                          session's — a password change,
#                          which keeps the rotating device
#                          signed in and must not cut it
#   neither                every socket of the user — logout-
#                          all, admin deactivation, erasure
#
# A sid whose handshake hash was never recorded proves
# nothing: only_session cuts what is KNOWN to be that
# session, except_session keeps what is KNOWN to be the
# caller's — so an unrecorded sid survives the first scope
# and falls to the second (the single-device cut errs toward
# keeping, the revocation toward cutting). Before the hash
# was filed every scope was the account-wide one, and one
# phone signing out silenced the tablet's chat until it was
# backgrounded or the network blinked.
#
# Iterates a list() snapshot (other threads mutate the dict)
# and asks python-socketio to close each sid; the presence
# rows go too, so /online-status cannot keep showing a user
# whose disconnect handler never ran. Returns the number of
# sockets closed. A socket layer that is not up, or a sid
# that died between the snapshot and the call, is not an
# error — the caller's own route must not fail over it.
#
# Every revocation path (auth logout/change-password/
# erasure, admin deactivate/delete) reaches it through a
# guarded lazy import.
#
# Used by:
#   - users/api/auth_views.py — _disconnect_user_sockets
#     (logout → only_session, change_password →
#     except_session, logout_all / delete_me → account-wide)
#   - admin/api/views.py — the same guarded helper, always
#     account-wide
############################################################

def disconnect_user_sockets(user_id, *, only_session=None, except_session=None) -> int:
    try:
        from knfapp.chat.socket import sio
    except ImportError:
        logger.warning("Socket layer unavailable — cannot disconnect user=%s", user_id)
        return 0

    closed = 0
    for sid, uid in list(_connected_users.items()):
        if uid != user_id:
            continue
        session = _connected_sessions.get(sid)
        if only_session is not None and session != only_session:
            continue
        if except_session is not None and session == except_session:
            continue
        try:
            sio.disconnect(sid)
            closed += 1
        except Exception:
            logger.warning("Could not disconnect socket sid=%s user=%s", sid, user_id)
        # The disconnect handler normally does this; doing it
        # here too keeps presence honest if it never ran
        _connected_users.pop(sid, None)
        _connected_names.pop(sid, None)
        _connected_sessions.pop(sid, None)

    if closed:
        scope = "session" if only_session else ("other-sessions" if except_session else "account")
        logger.info("Disconnected %d socket(s) for user=%s scope=%s", closed, user_id, scope)
    return closed








############################################################
# register_socket_events
############################################################
#
# Binds every client → server handler below to the Server
# on the default "/" namespace. Called exactly once by
# socket.py at import. python-socketio hands each handler
# its sid explicitly; the handlers that care resolve it
# through _connected_users — a sid the connect handler never
# recorded is dropped without a word.
#
# Every payload-carrying handler takes data=None and opens
# with an isinstance(dict) gate, so an emit with no payload
# (or with a list, or a bare string) is a clean drop; a
# conversationId must be a non-empty str before it can
# reach the database as a bind parameter. Handlers that
# touched the database close the thread's connection on
# the way out (_release_connection above) — an engineio
# packet thread lives as long as its socket, and must not
# park an idle connection.
############################################################

def register_socket_events(sio):


    def handle_socket_error(event, sid, exc):
        logger.exception(
            "Socket handler failed: event=%s sid=%s user=%s",
            event, sid, _connected_users.get(sid),
            exc_info=exc,
        )
        try:
            sio.emit("error", {"message": "Internal error"}, to=sid)
        except Exception:
            pass


    def _guarded(event, handler):
        # python-socketio has no namespace-wide error handler in
        # threading mode — each handler carries the guard, so an
        # unexpected exception is logged with its event and sid
        # instead of vanishing into the packet thread
        def wrapped(sid, *args):
            try:
                return handler(sid, *args)
            except Exception as e:
                handle_socket_error(event, sid, e)
            finally:
                _release_connection()
        return wrapped




    ############################################################
    # handle_connect
    ############################################################
    #
    # "connect" — the handshake. Returning False rejects the
    # socket: the client sees connect_error, treats a
    # non-transport error as a dead token and stops retrying
    # it. A rejection is logged WITH the sid and peer address,
    # so a flood is attributable. An accepted socket is joined
    # to every conv:* room of its user in one go — the only
    # automatic room membership there is; conversations
    # created later need join_conversation from the client or
    # create_conversation's server-side enter_room.
    #
    # Order matters: the room query runs FIRST and presence is
    # recorded only once it succeeded — a sid left in
    # _connected_users by a failed handshake is a ghost
    # "online" user forever. Two caps guard the process (see
    # the module banner).
    ############################################################

    def handle_connect(sid, environ, auth=None):
        resolved = _authenticate_socket(environ, auth)
        if not resolved:
            logger.info("Socket connection rejected — invalid token (sid=%s ip=%s)",
                        sid, environ.get("REMOTE_ADDR") if environ else None)
            # 'unauthorized' is the ONE reason the client may
            # render as "session expired"
            raise ConnectionRefusedError("unauthorized")

        user, session_hash = resolved
        user_id = user["id"]

        if len(_connected_users) >= _MAX_TOTAL_SOCKETS:
            logger.warning("Socket connection rejected — process cap %d reached (user=%s)",
                           _MAX_TOTAL_SOCKETS, user_id)
            raise ConnectionRefusedError("busy")

        # Newest wins: past the per-user cap, evict this user's
        # OLDEST socket(s) rather than refuse the fresh one. The
        # dict is insertion-ordered, so the first matching sid is
        # the oldest; a sid that died between snapshot and call is
        # popped regardless — exactly what disconnect_user_sockets
        # does. A live older device just reconnects and, if all
        # slots are truly live, evicts the next-oldest in turn.
        while sum(1 for uid in list(_connected_users.values()) if uid == user_id) >= _MAX_SOCKETS_PER_USER:
            oldest = next((s for s, uid in list(_connected_users.items()) if uid == user_id), None)
            if oldest is None:
                break
            try:
                sio.disconnect(oldest)
            except Exception:
                logger.warning("Could not disconnect evicted socket sid=%s user=%s", oldest, user_id)
            _connected_users.pop(oldest, None)
            _connected_names.pop(oldest, None)
            _connected_sessions.pop(oldest, None)
            logger.info("Evicted oldest socket sid=%s for user=%s — newest wins past cap %d",
                        oldest, user_id, _MAX_SOCKETS_PER_USER)

        try:
            room_ids = list(ConversationParticipant.objects.filter(user_id=user_id)
                            .values_list("conversation_id", flat=True))
            for room_id in room_ids:
                sio.enter_room(sid, f"conv:{room_id}")

            _connected_users[sid] = user_id
            _connected_names[sid] = user["display_name"] or "Unknown"
            # The session behind this sid — what a single-device
            # logout matches on (disconnect_user_sockets)
            _connected_sessions[sid] = session_hash
            _stamp_last_active(user_id)

            logger.info("Socket connected: user=%s sid=%s rooms=%d", user_id, sid, len(room_ids))
            sio.emit("connected", {"userId": user_id}, to=sid)
        except Exception:
            # No half-connected sid may survive in the presence
            # table — the refusal tears the session down. Reason
            # 'error' so the client retries instead of telling the
            # user their session died over a transient DB hiccup
            _connected_users.pop(sid, None)
            _connected_names.pop(sid, None)
            _connected_sessions.pop(sid, None)
            logger.exception("Socket handshake failed: user=%s sid=%s", user_id, sid)
            raise ConnectionRefusedError("error")
        finally:
            _release_connection()

    sio.on("connect", handler=handle_connect)




    ############################################################
    # handle_disconnect
    ############################################################
    #
    # "disconnect" — drops the sid from the presence table and
    # the display-name and session caches beside it. Rooms need
    # no cleanup:
    # python-socketio clears them for a closed socket itself.
    # The user stays "online" for /online-status and the push
    # skip as long as ANY other sid of theirs is in the table.
    # last_active_at is stamped HERE (and on connect): the two
    # socket edges bracket every online stretch, and a crashed
    # client's missing disconnect still fires through the
    # engine's ping timeout.
    ############################################################

    def handle_disconnect(sid, reason=None):
        user_id = _connected_users.pop(sid, None)
        _connected_names.pop(sid, None)
        _connected_sessions.pop(sid, None)
        if user_id:
            _stamp_last_active(user_id)
            _release_connection()
            logger.info("Socket disconnected: user=%s sid=%s reason=%s", user_id, sid, reason)

    sio.on("disconnect", handler=handle_disconnect)




    ############################################################
    # handle_join / handle_leave
    ############################################################
    #
    # "join_conversation" {conversationId} joins THIS socket
    # to one room after a membership check; a non-member, an
    # unknown sid or a missing id is dropped silently. Meant
    # for a conversation created after the socket connected.
    # "leave_conversation" leaves the room — membership stays
    # unchecked on purpose: leaving a room the socket is not
    # in is a no-op. Both capped at 10 per 10 s per user.
    ############################################################

    def handle_join(sid, data=None):
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "join_conversation"):
            return

        if not isinstance(data, dict):
            return
        conv_id = data.get("conversationId")
        if not isinstance(conv_id, str) or not conv_id:
            return

        # Membership decides the join — the client can name any id
        if _is_member(conv_id, user_id):
            sio.enter_room(sid, f"conv:{conv_id}")

    sio.on("join_conversation", handler=_guarded("join_conversation", handle_join))


    def handle_leave(sid, data=None):
        user_id = _connected_users.get(sid)
        if not user_id:
            return
        if _socket_rate_check(user_id, "leave_conversation"):
            return

        if not isinstance(data, dict):
            return
        conv_id = data.get("conversationId")
        if isinstance(conv_id, str) and conv_id:
            sio.leave_room(sid, f"conv:{conv_id}")

    sio.on("leave_conversation", handler=_guarded("leave_conversation", handle_leave))




    ############################################################
    # handle_typing / handle_stop_typing
    ############################################################
    #
    # "typing" {conversationId} → "user_typing"
    # {conversationId, userId, displayName} to everyone else
    # in the room (skip_sid, so the typist never sees their
    # own indicator); "stop_typing" clears it. Capped at 20
    # per 10 s per user. Members only: a non-member's event
    # is dropped silently, so knowing a conversation id is
    # not enough to plant a stray "X is typing". The display
    # name comes from _connected_names, cached at the
    # handshake — no users SELECT per keystroke; a rename
    # mid-session shows the old name until that socket
    # reconnects, and the indicator lives five seconds, so
    # nobody can tell.
    ############################################################

    def handle_typing(sid, data=None):
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

        if not _is_member(conv_id, user_id):
            return

        display_name = _connected_names.get(sid) or "Unknown"
        sio.emit(
            "user_typing",
            {"conversationId": conv_id, "userId": user_id, "displayName": display_name},
            to=f"conv:{conv_id}",
            skip_sid=sid,
        )

    sio.on("typing", handler=_guarded("typing", handle_typing))


    def handle_stop_typing(sid, data=None):
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

        if not _is_member(conv_id, user_id):
            return

        sio.emit(
            "user_stop_typing",
            {"conversationId": conv_id, "userId": user_id},
            to=f"conv:{conv_id}",
            skip_sid=sid,
        )

    sio.on("stop_typing", handler=_guarded("stop_typing", handle_stop_typing))




    ############################################################
    # handle_mark_read
    ############################################################
    #
    # "mark_read" {conversationId} — the socket twin of
    # PUT /api/chat/conversations/<id>/read. Both transports
    # call api/views.py _apply_mark_read (see its banner);
    # only the transport gates live here. A None back from
    # the helper (not a participant) is a silent drop where
    # the REST twin answers 403. Capped at 10 per 10 s per
    # user, a budget SHARED with the REST twin. When at
    # least one receipt was new, "messages_read" goes out
    # through emit_read_receipt — targeted at the affected
    # senders and the reader's own sockets, not the room.
    ############################################################

    def handle_mark_read(sid, data=None):
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

        # Imported lazily: api/views.py reaches back into this
        # module the same way, and only one of the two may bind
        # at import time
        from knfapp.chat.api.views import _apply_mark_read
        from knfapp.common.timestamps import utc_now

        now = utc_now()
        newly_read_ids = _apply_mark_read(conv_id, user_id, now)

        if newly_read_ids:
            emit_read_receipt(sio, conv_id, user_id, newly_read_ids)

    sio.on("mark_read", handler=_guarded("mark_read", handle_mark_read))








############################################################
# The emit helpers — the fan-out the REST views call
############################################################
#
# Server-level emits (a REST handler has no socket context),
# addressed to room "conv:<id>" — the actor's own sockets
# included; clients dedupe the echo against their optimistic
# state. emit_read_receipt is the exception: it addresses
# the individual sids a receipt actually concerns (the
# senders of the read messages and the reader's own other
# devices) — a room-wide emit would make every member
# answer with an unread refetch, one read costing
# O(members) requests. If the targets cannot be resolved,
# the room emit is the fallback, so a receipt is never lost.
############################################################

def emit_new_message(sio, conv_id, message_data):
    sio.emit("new_message", message_data, to=f"conv:{conv_id}")


def emit_reaction_update(sio, conv_id, msg_id, reactions):
    sio.emit(
        "reaction_update",
        {"conversationId": conv_id, "messageId": msg_id, "reactions": reactions},
        to=f"conv:{conv_id}",
    )


def emit_message_edited(sio, conv_id, msg_id, text, edited_at):
    # every client in the room replaces the row's text in place
    sio.emit(
        "message_edited",
        {"conversationId": conv_id, "messageId": msg_id, "text": text, "editedAt": edited_at},
        to=f"conv:{conv_id}",
    )


def emit_message_updated(sio, conv_id, msg_id, patch):
    # the server filled something in after the send (the link
    # preview card, a pin) — clients merge the patch into the row
    sio.emit(
        "message_updated",
        {"conversationId": conv_id, "messageId": msg_id, "patch": patch},
        to=f"conv:{conv_id}",
    )


def emit_conversation_updated(sio, conv_id, patch):
    # a room setting changed (the disappearing-messages TTL) —
    # clients merge the patch into the conversation meta
    sio.emit(
        "conversation_updated",
        {"conversationId": conv_id, "patch": patch},
        to=f"conv:{conv_id}",
    )


def emit_message_deleted(sio, conv_id, msg_id):
    sio.emit(
        "message_deleted",
        {"conversationId": conv_id, "messageId": msg_id},
        to=f"conv:{conv_id}",
    )


def _read_receipt_sids(reader_id, message_ids):
    # The sids a "messages_read" actually concerns; None when it
    # cannot tell (empty list, too long for one IN clause, or a
    # DB error) — the caller falls back to the room. 900 keeps
    # the IN list under SQLite's default variable limit
    if not message_ids or len(message_ids) > 900:
        return None

    try:
        rows = list(Message.objects.filter(id__in=list(message_ids))
                    .values_list("sender_id", flat=True).distinct())
    except Exception:
        logger.exception("Read-receipt targeting failed for reader=%s", reader_id)
        return None

    interested = set(rows)
    interested.add(reader_id)
    # list() snapshot: other threads connect and disconnect
    return [sid for sid, uid in list(_connected_users.items()) if uid in interested]


def emit_read_receipt(sio, conv_id, reader_id, message_ids):
    payload = {
        "conversationId": conv_id,
        "readerId": reader_id,
        "messageIds": message_ids,
    }

    sids = _read_receipt_sids(reader_id, message_ids)
    if sids is None:
        sio.emit("messages_read", payload, to=f"conv:{conv_id}")
        return

    # Each sid is its own room in Socket.IO, so `to=sid` is
    # the one-socket address
    for sid in sids:
        sio.emit("messages_read", payload, to=sid)
