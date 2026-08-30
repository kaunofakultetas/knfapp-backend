# -----------------------------------------------------------
#  [*] Tests — chat/events.py, register_socket_events and the
#      handlers it binds (the exhaustive pass)
#
#  Slice: register_socket_events itself plus every closure it
#  registers on the "/" namespace — handle_socket_error,
#  handle_connect, handle_disconnect, handle_join,
#  handle_leave, handle_typing, handle_stop_typing and
#  handle_mark_read. The fan-out helpers below them
#  (emit_new_message, emit_read_receipt, …) and the
#  disconnect_user_sockets kill switch belong to other files;
#  they appear here only where a handler calls them.
#
#  test_chat_events.py already proves the happy paths and the
#  headline gates. This file is the gap-closing pass over the
#  same handlers: it drives the arms that the first pass left
#  implicit.
#
#    - token EXTRACTION beyond present/absent: a dict with no
#      token key, a None token, a truthy non-string token
#      (which must NOT fall through to the query string), a
#      Bearer prefix, whitespace, an empty ?token=
#    - session RESOLUTION beyond valid/expired: an orphaned
#      session whose user row is gone, an expiry landing on
#      the exact call instant, one microsecond past it, a
#      malformed expiry, a token revoked by logout
#    - the cap ORDER (process before per-user), a reconnect on
#      a sid already at the cap, and a cap that frees up again
#    - the handshake's ordering contract: rooms first, then
#      presence, then the ack — proved by failing each stage
#      and by reading presence from inside the ack
#    - that every handler closes the connection it opened,
#      on the failure path as well as the happy one
#    - the ORDER of the gates in every payload handler: an
#      unknown sid spends no quota, a payload the handler is
#      about to drop spends one anyway
#    - the boundaries a client can actually send: a bool id, a
#      10k-character id, a quoted id that must never reach
#      sqlite as anything but a bind parameter, extra keys
#    - mark_read's (prior, now] window at both ends: a message
#      stamped at the exact call instant is claimed, one a
#      microsecond later is not, and one below the previous
#      watermark never is
#    - what a socket keeps after the account behind it is
#      deactivated — it authenticates ONCE, at the handshake
# -----------------------------------------------------------

import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import flask
import pytest
import time_machine

from app.chat import events as chat_events


WINDOW = chat_events._SOCKET_RATE_WINDOW
PER_USER_CAP = chat_events._MAX_SOCKETS_PER_USER
PROCESS_CAP = chat_events._MAX_TOTAL_SOCKETS
LIMITS = chat_events._SOCKET_RATE_LIMITS

# A fixed instant every clock-sensitive test travels to, so a
# frozen `now` has no microseconds and an ISO string can be
# built to match it character for character
FROZEN = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_ISO = "2026-03-01T12:00:00"

# The default message timestamp: one minute below FROZEN, so a
# seeded row is inside mark_read's (prior, now] window whether
# the test travels to FROZEN or runs on the real clock
BEFORE_FROZEN_ISO = "2026-03-01T11:59:00"




# -----------------------------------------------------------
# _Wire
# -----------------------------------------------------------
#
# The whole flask-socketio surface a handler can see, faked:
#
#   - as the SocketIO instance it collects what
#     register_socket_events decorates (`on`,
#     `on_error_default`) and records server-level emits, so
#     handle_mark_read's closure hands IT to emit_read_receipt
#   - as the runtime, `context()` pushes a real Flask request
#     context and pins a sid the way _handle_event does;
#     `bare_context()` pins nothing, for the error handler's
#     getattr fallbacks
#
# emit / join_room / leave_room are patched INSIDE the events
# module by the `wire` fixture, and each can be made to raise
# so the handlers' failure paths are drivable.
# -----------------------------------------------------------

class _Wire:

    def __init__(self, app):
        self.app = app
        self.handlers = {}
        self.error_handler = None
        self.emits = []          # flask_socketio.emit — inside a handler
        self.server_emits = []   # socketio.emit — the fan-out helpers
        self.joined = []
        self.left = []
        self.emit_raises = None
        self.join_raises = None
        self.leave_raises = None
        self.emit_hook = None    # called with no args on every emit
        self.join_hook = None    # called with no args on every join_room

    # --- the SocketIO surface register_socket_events uses ---

    def on(self, event):
        def _bind(handler):
            self.handlers[event] = handler
            return handler
        return _bind

    def on_error_default(self, handler):
        self.error_handler = handler
        return handler

    def emit(self, event, payload=None, to=None, **kwargs):
        self.server_emits.append({"event": event, "payload": payload, "to": to})

    # --- the patched flask_socketio primitives ---

    def record_emit(self, event, payload=None, to=None, include_self=True, **kwargs):
        if self.emit_hook is not None:
            self.emit_hook()
        if self.emit_raises is not None:
            raise self.emit_raises
        self.emits.append({"event": event, "payload": payload,
                           "to": to, "include_self": include_self})

    def record_join(self, room, sid=None, namespace=None):
        if self.join_hook is not None:
            self.join_hook()
        if self.join_raises is not None:
            raise self.join_raises
        self.joined.append(room)

    def record_leave(self, room, sid=None, namespace=None):
        if self.leave_raises is not None:
            raise self.leave_raises
        self.left.append(room)

    # --- driving the handlers ---

    @contextmanager
    def context(self, sid="sid-a", query=""):
        with self.app.test_request_context("/socket.io/" + query):
            flask.request.sid = sid
            yield

    @contextmanager
    def bare_context(self, query=""):
        with self.app.test_request_context("/socket.io/" + query):
            yield

    def connect(self, auth=None, sid="sid-a", query=""):
        with self.context(sid, query):
            return self.handlers["connect"](auth)

    def fire(self, event, data=None, sid="sid-a"):
        with self.context(sid):
            return self.handlers[event](data)

    def fire_bare(self, event, sid="sid-a"):
        # The zero-argument call: every payload handler defaults
        # its data, so an emit with no payload must be a clean drop
        with self.context(sid):
            return self.handlers[event]()

    def present(self, sid, user_id, display_name="Testas"):
        chat_events._connected_users[sid] = user_id
        chat_events._connected_names[sid] = display_name

    def named(self, name, server=False):
        source = self.server_emits if server else self.emits
        return [e for e in source if e["event"] == name]




# -----------------------------------------------------------
# wire
# -----------------------------------------------------------
#
# _connected_users, _connected_names and _socket_rate are
# MODULE-level and outlive a test, so they are wiped on the
# way in AND on the way out — a leaked sid makes the next
# test's presence lookups lie, a leaked timestamp makes its
# rate assertions lie.
# -----------------------------------------------------------

@pytest.fixture
def wire(app, monkeypatch):
    harness = _Wire(app)

    monkeypatch.setattr(chat_events, "emit", harness.record_emit)
    monkeypatch.setattr(chat_events, "join_room", harness.record_join)
    monkeypatch.setattr(chat_events, "leave_room", harness.record_leave)

    _wipe()
    chat_events.register_socket_events(harness)

    yield harness

    _wipe()


def _wipe():
    chat_events._connected_users.clear()
    chat_events._connected_names.clear()
    chat_events._socket_rate.clear()




# -----------------------------------------------------------
# _RecordingDb
# -----------------------------------------------------------
#
# A real connection that counts its own close() calls, so the
# "closes the connection it opened" assertions are about the
# handler and not about sqlite. Everything else delegates.
# -----------------------------------------------------------

class _RecordingDb:

    def __init__(self, conn):
        self._conn = conn
        self.closes = 0

    def close(self):
        self.closes += 1
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)




# -----------------------------------------------------------
# _watch_db
# -----------------------------------------------------------
#
# Wraps chat_events.get_db so every connection a handler opens
# lands in the returned list. Used by the close() assertions.
# -----------------------------------------------------------

def _watch_db(monkeypatch):
    opened = []
    real = chat_events.get_db

    def _spy():
        recorder = _RecordingDb(real())
        opened.append(recorder)
        return recorder

    monkeypatch.setattr(chat_events, "get_db", _spy)
    return opened




# -----------------------------------------------------------
# Row helpers
# -----------------------------------------------------------
#
# `db` is the shared fixture connection — sqlite3.connect
# leaves foreign_keys OFF on it, which is what lets the
# orphaned-session test delete a user row and keep its
# session.
# -----------------------------------------------------------

def _token(headers):
    return headers["Authorization"].split(" ", 1)[1]


def _conversation(db, *user_ids):
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"
    db.execute("INSERT INTO conversations (id, type, created_by) VALUES (?, 'group', ?)",
               (conv_id, user_ids[0]))
    for user_id in user_ids:
        db.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
                   (conv_id, user_id))
    db.commit()
    return conv_id


def _message(db, conv_id, sender_id, created_at=None, text="Labas", deleted_at=None):
    msg_id = f"msg-{uuid.uuid4().hex[:8]}"
    created_at = created_at or BEFORE_FROZEN_ISO
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at, deleted_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, created_at, deleted_at),
    )
    db.commit()
    return msg_id


def _watermark(db, conv_id, user_id):
    row = db.execute(
        "SELECT last_read_at FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user_id),
    ).fetchone()
    return row["last_read_at"] if row else None


def _receipts(db, user_id):
    return {r["message_id"] for r in db.execute(
        "SELECT message_id FROM message_reads WHERE user_id = ?", (user_id,))}




# -----------------------------------------------------------
# _explode
# -----------------------------------------------------------
#
# A get_db that must never be called — it proves a handler
# reaches sqlite only on the paths that actually need it.
# -----------------------------------------------------------

def _explode(*args, **kwargs):
    raise AssertionError("the handler must not open a database connection here")




# -----------------------------------------------------------
# register_socket_events — the registration itself
# -----------------------------------------------------------


def test_register_socket_events_binds_exactly_the_documented_events(wire):
    assert set(wire.handlers) == {
        "connect", "disconnect", "join_conversation",
        "leave_conversation", "typing", "stop_typing", "mark_read",
    }


def test_register_socket_events_installs_a_default_error_handler(wire):
    assert callable(wire.error_handler)


def test_register_socket_events_returns_nothing(app):
    assert chat_events.register_socket_events(_Wire(app)) is None


def test_every_payload_handler_tolerates_being_called_with_no_payload(wire, actor):
    user, _ = actor
    wire.present("sid-1", user["id"])

    for event in ("join_conversation", "leave_conversation", "typing",
                  "stop_typing", "mark_read"):
        assert wire.fire_bare(event, sid="sid-1") is None

    assert wire.emits == [] and wire.joined == [] and wire.left == []


def test_the_handlers_close_over_the_instance_they_were_registered_on(wire, app, actor, make_user, db):
    reader, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, reader["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    second = _Wire(app)
    chat_events.register_socket_events(second)
    wire.present("sid-1", reader["id"])

    with second.context("sid-1"):
        second.handlers["mark_read"]({"conversationId": conv_id})

    # The receipt went out through the instance that registered
    # THAT closure, not through the first one
    assert second.named("messages_read", server=True)
    assert wire.server_emits == []




# -----------------------------------------------------------
# connect — token extraction
# -----------------------------------------------------------


def test_an_auth_dict_without_a_token_key_falls_back_to_the_query_string(wire, actor):
    user, headers = actor

    result = wire.connect({"deviceId": "phone"}, sid="sid-1",
                          query=f"?token={_token(headers)}")

    assert result is None
    assert chat_events._connected_users["sid-1"] == user["id"]


def test_an_empty_auth_dict_falls_back_to_the_query_string(wire, actor):
    user, headers = actor

    assert wire.connect({}, sid="sid-1", query=f"?token={_token(headers)}") is None
    assert chat_events._connected_users["sid-1"] == user["id"]


@pytest.mark.parametrize("token", [None, "", 0, False, [], {}])
def test_a_falsy_auth_token_falls_back_to_the_query_string(wire, actor, token):
    user, headers = actor

    assert wire.connect({"token": token}, sid="sid-1",
                        query=f"?token={_token(headers)}") is None
    assert chat_events._connected_users["sid-1"] == user["id"]


@pytest.mark.parametrize("token", [None, "", 0, False, [], {}])
def test_a_falsy_auth_token_with_no_query_string_is_rejected(wire, token):
    assert wire.connect({"token": token}, sid="sid-1") is False
    assert chat_events._connected_users == {}


def test_a_truthy_non_string_token_is_refused_without_consulting_the_query_string(wire, actor):
    user, headers = actor

    # The extraction stops at the first TRUTHY value, so a
    # non-string in the auth payload shadows a perfectly good
    # legacy query token instead of falling through to it
    result = wire.connect({"token": 12345}, sid="sid-1",
                          query=f"?token={_token(headers)}")

    assert result is False
    assert chat_events._connected_users == {}


@pytest.mark.parametrize("token", [12345, 3.5, True, ["t"], {"t": 1}])
def test_a_truthy_non_string_token_is_rejected(wire, token):
    assert wire.connect({"token": token}, sid="sid-1") is False


def test_an_empty_query_token_is_rejected(wire):
    assert wire.connect(None, sid="sid-1", query="?token=") is False
    assert chat_events._connected_users == {}


def test_a_query_string_without_a_token_parameter_is_rejected(wire):
    assert wire.connect(None, sid="sid-1", query="?EIO=4&transport=polling") is False


def test_a_bearer_prefixed_token_is_rejected(wire, actor):
    user, headers = actor

    # The handshake carries the RAW token; the Authorization
    # header's scheme is a REST-only wrapper
    assert wire.connect({"token": f"Bearer {_token(headers)}"}, sid="sid-1") is False


@pytest.mark.parametrize("wrap", [" {0}", "{0} ", "\t{0}\n"])
def test_a_token_with_surrounding_whitespace_is_rejected(wire, actor, wrap):
    user, headers = actor

    assert wire.connect({"token": wrap.format(_token(headers))}, sid="sid-1") is False


def test_a_truncated_token_is_rejected(wire, actor):
    user, headers = actor

    assert wire.connect({"token": _token(headers)[:-1]}, sid="sid-1") is False


def test_a_token_of_another_user_authenticates_that_other_user(wire, make_user, auth_headers):
    first = make_user(username="pirmas")
    second = make_user(username="antras")
    auth_headers(first)

    wire.connect({"token": _token(auth_headers(second))}, sid="sid-1")

    assert chat_events._connected_users["sid-1"] == second["id"]


@pytest.mark.parametrize("auth", [None, 0, "", [], (), "token-string", 42, True])
def test_a_non_dict_auth_payload_reads_the_query_string_alone(wire, actor, auth):
    user, headers = actor

    assert wire.connect(auth, sid="sid-1", query=f"?token={_token(headers)}") is None
    assert chat_events._connected_users["sid-1"] == user["id"]




# -----------------------------------------------------------
# connect — session resolution edges
# -----------------------------------------------------------


def test_a_session_whose_user_row_is_gone_is_rejected(wire, actor, db):
    user, headers = actor
    # The fixture connection leaves foreign_keys OFF, so the
    # session row survives its owner and the handshake reaches
    # resolve_session_token's "no user" arm
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    db.commit()

    assert wire.connect({"token": _token(headers)}, sid="sid-1") is False
    assert chat_events._connected_users == {}


def test_a_session_expiring_at_this_exact_instant_still_connects(wire, actor, db):
    user, headers = actor
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?",
               (FROZEN.isoformat(), user["id"]))
    db.commit()

    with time_machine.travel(FROZEN, tick=False):
        assert wire.connect({"token": _token(headers)}, sid="sid-1") is None


def test_a_session_that_expired_one_microsecond_ago_is_rejected(wire, actor, db):
    user, headers = actor
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?",
               ((FROZEN - timedelta(microseconds=1)).isoformat(), user["id"]))
    db.commit()

    with time_machine.travel(FROZEN, tick=False):
        assert wire.connect({"token": _token(headers)}, sid="sid-1") is False


@pytest.mark.parametrize("expiry", ["", "not-a-date", "2026-13-45T99:99:99", "0"])
def test_a_malformed_expiry_is_treated_as_expired_and_purged(wire, actor, db, expiry):
    user, headers = actor
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (expiry, user["id"]))
    db.commit()

    assert wire.connect({"token": _token(headers)}, sid="sid-1") is False
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 0


def test_a_naive_expiry_is_read_as_utc(wire, actor, db):
    user, headers = actor
    naive = (FROZEN + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (naive, user["id"]))
    db.commit()

    with time_machine.travel(FROZEN, tick=False):
        assert wire.connect({"token": _token(headers)}, sid="sid-1") is None


def test_a_token_revoked_by_logout_cannot_open_a_socket(wire, client, actor):
    user, headers = actor
    token = _token(headers)
    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert wire.connect({"token": token}, sid="sid-1") is False
    assert chat_events._connected_users == {}


def test_two_sessions_of_one_user_both_open_sockets(wire, make_user, auth_headers):
    user = make_user(username="dvi_sesijos")
    first = auth_headers(user)
    second = auth_headers(user)

    assert wire.connect({"token": _token(first)}, sid="sid-1") is None
    assert wire.connect({"token": _token(second)}, sid="sid-2") is None
    assert set(chat_events._connected_users.values()) == {user["id"]}


def test_deactivating_a_user_refuses_the_next_handshake_but_not_the_live_socket(wire, actor, db):
    user, headers = actor
    token = _token(headers)
    assert wire.connect({"token": token}, sid="sid-1") is None

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert wire.connect({"token": token}, sid="sid-2") is False
    # A socket authenticates ONCE — the live one is only cut by
    # disconnect_user_sockets, never by the next handshake
    assert chat_events._connected_users == {"sid-1": user["id"]}


def test_a_rejected_handshake_is_logged_with_the_sid_and_the_peer_address(wire, caplog):
    caplog.set_level(logging.INFO, logger="app.chat.events")

    wire.connect({"token": "bogus"}, sid="sid-loud")

    assert "sid-loud" in caplog.text
    assert "invalid token" in caplog.text




# -----------------------------------------------------------
# connect — the two caps
# -----------------------------------------------------------


def test_the_process_cap_is_checked_before_the_per_user_cap(wire, actor, caplog):
    user, headers = actor
    caplog.set_level(logging.INFO, logger="app.chat.events")
    # Both caps would trip: the process one answers first
    for index in range(PROCESS_CAP):
        wire.present(f"mine-{index}", user["id"])

    assert wire.connect({"token": _token(headers)}, sid="sid-extra") is False
    assert "process cap" in caplog.text
    assert "user cap" not in caplog.text


def test_only_the_per_user_cap_speaks_when_the_process_has_room(wire, actor, caplog):
    user, headers = actor
    caplog.set_level(logging.INFO, logger="app.chat.events")
    for index in range(PER_USER_CAP):
        wire.present(f"mine-{index}", user["id"])

    assert wire.connect({"token": _token(headers)}, sid="sid-extra") is False
    assert "user cap" in caplog.text
    assert "process cap" not in caplog.text


def test_a_reconnect_on_a_sid_already_counted_is_still_rejected_at_the_cap(wire, actor):
    user, headers = actor
    for index in range(PER_USER_CAP):
        wire.present(f"mine-{index}", user["id"])

    # Reusing an existing sid would not GROW the table, but the
    # cap counts before it looks at which sid is asking
    assert wire.connect({"token": _token(headers)}, sid="mine-0") is False
    assert len(chat_events._connected_users) == PER_USER_CAP


def test_the_per_user_cap_frees_up_again_after_a_disconnect(wire, actor):
    user, headers = actor
    token = _token(headers)
    for index in range(PER_USER_CAP):
        wire.present(f"mine-{index}", user["id"])
    assert wire.connect({"token": token}, sid="sid-new") is False

    wire.fire("disconnect", None, sid="mine-0")

    assert wire.connect({"token": token}, sid="sid-new") is None


def test_the_process_cap_frees_up_again_after_a_disconnect(wire, actor):
    user, headers = actor
    token = _token(headers)
    for index in range(PROCESS_CAP):
        wire.present(f"crowd-{index}", f"other-{index}")
    assert wire.connect({"token": token}, sid="sid-new") is False

    wire.fire("disconnect", None, sid="crowd-0")

    assert wire.connect({"token": token}, sid="sid-new") is None


def test_a_capped_handshake_joins_no_room_and_emits_nothing(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])
    for index in range(PER_USER_CAP):
        wire.present(f"mine-{index}", user["id"])

    wire.connect({"token": _token(headers)}, sid="sid-extra")

    assert wire.joined == []
    assert wire.emits == []


def test_the_per_user_cap_counts_only_that_users_sids(wire, actor, make_user, auth_headers):
    user, headers = actor
    stranger = make_user(username="svetimas")
    for index in range(PER_USER_CAP - 1):
        wire.present(f"mine-{index}", user["id"])
    for index in range(PER_USER_CAP * 3):
        wire.present(f"theirs-{index}", stranger["id"])

    assert wire.connect({"token": _token(headers)}, sid="sid-last") is None




# -----------------------------------------------------------
# connect — auto-join, presence, ordering and cleanup
# -----------------------------------------------------------


def test_an_accepted_handshake_returns_none_rather_than_true(wire, actor):
    user, headers = actor

    assert wire.connect({"token": _token(headers)}, sid="sid-1") is None


def test_a_handshake_whose_room_join_fails_leaves_no_presence_row(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])
    wire.join_raises = RuntimeError("room registry died")

    assert wire.connect({"token": _token(headers)}, sid="sid-1") is False
    assert chat_events._connected_users == {}
    assert chat_events._connected_names == {}


def test_the_handshake_closes_its_connection_on_the_happy_path(wire, actor, monkeypatch):
    user, headers = actor
    opened = _watch_db(monkeypatch)

    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert [recorder.closes for recorder in opened] == [1]


def test_the_handshake_closes_its_connection_when_the_room_join_fails(wire, actor, make_user, db, monkeypatch):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])
    opened = _watch_db(monkeypatch)
    wire.join_raises = RuntimeError("room registry died")

    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert [recorder.closes for recorder in opened] == [1]


def test_presence_is_recorded_before_the_ack_goes_out(wire, actor):
    user, headers = actor
    seen = {}
    wire.emit_hook = lambda: seen.update(dict(chat_events._connected_users))

    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert seen == {"sid-1": user["id"]}


def test_the_name_cache_is_filled_before_the_ack_goes_out(wire, make_user, auth_headers):
    user = make_user(username="ona", display_name="Ona Onaite")
    seen = {}
    wire.emit_hook = lambda: seen.update(dict(chat_events._connected_names))

    wire.connect({"token": _token(auth_headers(user))}, sid="sid-1")

    assert seen == {"sid-1": "Ona Onaite"}


def test_the_rooms_are_joined_before_presence_is_recorded(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])
    seen = []
    wire.join_hook = lambda: seen.append(dict(chat_events._connected_users))

    wire.connect({"token": _token(headers)}, sid="sid-1")

    # A sid left behind by a handshake that then fails is a
    # ghost "online" user forever, so presence goes last
    assert seen == [{}]


def test_the_unknown_fallback_is_only_reachable_through_an_empty_display_name(wire, actor, db):
    user, headers = actor
    # users.display_name is NOT NULL, so "" is the ONLY falsy
    # value the handshake can ever read out of it
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE users SET display_name = NULL WHERE id = ?", (user["id"],))
    db.rollback()
    db.execute("UPDATE users SET display_name = '' WHERE id = ?", (user["id"],))
    db.commit()

    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert chat_events._connected_names["sid-1"] == "Unknown"


def test_a_whitespace_display_name_is_cached_verbatim(wire, actor, db):
    user, headers = actor
    db.execute("UPDATE users SET display_name = ' ' WHERE id = ?", (user["id"],))
    db.commit()

    wire.connect({"token": _token(headers)}, sid="sid-1")

    # Only a FALSY name becomes "Unknown"; a blank one is a
    # profile problem, not a handshake one
    assert chat_events._connected_names["sid-1"] == " "


def test_a_handshake_joins_one_room_per_conversation_at_scale(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    expected = {f"conv:{_conversation(db, user['id'], other['id'])}" for _ in range(25)}

    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert set(wire.joined) == expected
    assert len(wire.joined) == 25


def test_the_handshake_logs_the_number_of_rooms_it_joined(wire, actor, make_user, db, caplog):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])
    _conversation(db, user["id"], other["id"])
    caplog.set_level(logging.INFO, logger="app.chat.events")

    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert "rooms=2" in caplog.text


def test_a_second_handshake_on_the_same_sid_overwrites_the_presence_row(wire, make_user, auth_headers):
    first = make_user(username="pirmas")
    second = make_user(username="antras")

    wire.connect({"token": _token(auth_headers(first))}, sid="shared")
    wire.connect({"token": _token(auth_headers(second))}, sid="shared")

    assert chat_events._connected_users == {"shared": second["id"]}
    assert chat_events._connected_names["shared"] == "Antras"


def test_a_handshake_records_presence_for_a_user_with_no_conversations(wire, actor):
    user, headers = actor

    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert chat_events._connected_users == {"sid-1": user["id"]}
    assert wire.joined == []


def test_a_rejected_handshake_joins_no_room(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])

    wire.connect({"token": "bogus"}, sid="sid-1")

    assert wire.joined == []


def test_the_handshake_joins_no_room_of_a_conversation_it_only_watches(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    mine = _conversation(db, user["id"], other["id"])
    _conversation(db, other["id"])

    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert wire.joined == [f"conv:{mine}"]


def test_a_handshake_that_fails_mid_ack_still_leaves_the_rooms_it_joined(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.emit_raises = RuntimeError("socket died mid-handshake")

    assert wire.connect({"token": _token(headers)}, sid="sid-1") is False
    # The rooms were joined before the failure; returning False
    # tears the whole session down, so they go with it
    assert wire.joined == [f"conv:{conv_id}"]
    assert chat_events._connected_users == {}




# -----------------------------------------------------------
# disconnect
# -----------------------------------------------------------


@pytest.mark.parametrize("reason", [None, "client namespace disconnect",
                                    "transport error", "ping timeout", 7, {"code": 1}])
def test_disconnect_accepts_any_reason_the_library_hands_it(wire, actor, reason):
    user, headers = actor
    wire.connect({"token": _token(headers)}, sid="sid-1")

    assert wire.fire("disconnect", reason, sid="sid-1") is None
    assert chat_events._connected_users == {}


def test_disconnect_drops_the_cached_name_even_when_presence_lost_the_sid(wire):
    chat_events._connected_names["orphan"] = "Vardas"

    wire.fire("disconnect", None, sid="orphan")

    assert chat_events._connected_names == {}


def test_disconnecting_the_same_sid_twice_is_idempotent(wire, actor):
    user, headers = actor
    wire.connect({"token": _token(headers)}, sid="sid-1")

    wire.fire("disconnect", None, sid="sid-1")
    wire.fire("disconnect", None, sid="sid-1")

    assert chat_events._connected_users == {}
    assert chat_events._connected_names == {}


def test_disconnect_leaves_no_room_by_hand(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])
    wire.connect({"token": _token(headers)}, sid="sid-1")

    wire.fire("disconnect", None, sid="sid-1")

    # flask-socketio drops a closed socket's rooms itself
    assert wire.left == []


def test_disconnect_spends_no_rate_limit_budget(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"])

    for _ in range(200):
        wire.fire("disconnect", None, sid="sid-1")

    assert chat_events._socket_rate == {}


def test_disconnect_after_a_rejected_handshake_is_a_no_op(wire):
    wire.connect({"token": "bogus"}, sid="sid-1")

    wire.fire("disconnect", "transport close", sid="sid-1")

    assert chat_events._connected_users == {}


def test_disconnect_of_one_device_keeps_the_other_name_cached(wire, make_user, auth_headers):
    user = make_user(username="ona", display_name="Ona Onaite")
    token = _token(auth_headers(user))
    wire.connect({"token": token}, sid="phone")
    wire.connect({"token": token}, sid="laptop")

    wire.fire("disconnect", None, sid="phone")

    assert chat_events._connected_names == {"laptop": "Ona Onaite"}


def test_disconnect_logs_only_for_a_sid_it_knew(wire, actor, caplog):
    user, headers = actor
    wire.connect({"token": _token(headers)}, sid="sid-1")
    caplog.clear()
    caplog.set_level(logging.INFO, logger="app.chat.events")

    wire.fire("disconnect", None, sid="never-seen")
    assert "Socket disconnected" not in caplog.text

    wire.fire("disconnect", None, sid="sid-1")
    assert "Socket disconnected" in caplog.text


def test_disconnect_opens_no_database_connection(wire, actor, monkeypatch):
    user, headers = actor
    wire.present("sid-1", user["id"])
    monkeypatch.setattr(chat_events, "get_db", _explode)

    wire.fire("disconnect", None, sid="sid-1")

    assert chat_events._connected_users == {}




# -----------------------------------------------------------
# join_conversation
# -----------------------------------------------------------


def test_join_spends_quota_even_on_a_payload_it_is_about_to_drop(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        # The quota gate sits BEFORE the payload gate, so junk
        # events cost the client its own budget
        for _ in range(LIMITS["join_conversation"]):
            wire.fire("join_conversation", "not-a-dict", sid="sid-1")

        wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert wire.joined == []


def test_join_from_an_unknown_sid_spends_no_quota(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["join_conversation"] * 5):
            wire.fire("join_conversation", {"conversationId": conv_id}, sid="ghost")
        # The identity gate is the FIRST one, so an unrecorded
        # sid never reaches the limiter and leaves no key behind
        assert chat_events._socket_rate == {}

        wire.present("sid-1", user["id"])
        wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert wire.joined == [f"conv:{conv_id}"]


def test_join_is_idempotent_for_a_member(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")
    wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    # join_room itself is the idempotent one; the handler just
    # asks twice
    assert wire.joined == [f"conv:{conv_id}", f"conv:{conv_id}"]


@pytest.mark.parametrize("conv_id", [True, False, 0, 1, 3.5, [], ["c"], (), b"conv-1"])
def test_join_ignores_a_conversation_id_of_the_wrong_type(wire, actor, conv_id):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert wire.joined == []


def test_join_of_a_conversation_the_user_was_removed_from_is_dropped(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (conv_id, user["id"]))
    db.commit()

    wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert wire.joined == []


def test_join_never_lets_a_conversation_id_reach_sqlite_as_anything_but_a_bind(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    wire.fire("join_conversation",
              {"conversationId": "x' OR '1'='1"}, sid="sid-1")

    assert wire.joined == []
    # The participants table is intact, so nothing was executed
    assert db.execute("SELECT COUNT(*) FROM conversation_participants").fetchone()[0] == 2


def test_join_tolerates_a_huge_conversation_id(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("join_conversation", {"conversationId": "c" * 10000}, sid="sid-1")

    assert wire.joined == []


def test_join_ignores_extra_payload_keys(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    wire.fire("join_conversation",
              {"conversationId": conv_id, "sid": "spoofed", "userId": other["id"]},
              sid="sid-1")

    assert wire.joined == [f"conv:{conv_id}"]


def test_join_works_for_a_conversation_with_a_single_participant(wire, actor, db):
    user, headers = actor
    conv_id = _conversation(db, user["id"])
    wire.present("sid-1", user["id"])

    wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert wire.joined == [f"conv:{conv_id}"]


def test_join_closes_its_connection_whether_or_not_it_joins(wire, actor, make_user, db, monkeypatch):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])
    opened = _watch_db(monkeypatch)

    wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")
    wire.fire("join_conversation", {"conversationId": "conv-nope"}, sid="sid-1")

    assert [recorder.closes for recorder in opened] == [1, 1]


def test_join_closes_its_connection_when_the_room_join_fails(wire, actor, make_user, db, monkeypatch):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])
    opened = _watch_db(monkeypatch)
    wire.join_raises = RuntimeError("room registry died")

    with pytest.raises(RuntimeError):
        wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert [recorder.closes for recorder in opened] == [1]


def test_a_database_failure_inside_join_reaches_the_default_error_handler(wire, actor, monkeypatch):
    user, headers = actor
    wire.present("sid-1", user["id"])

    def _broken():
        raise RuntimeError("database gone")

    monkeypatch.setattr(chat_events, "get_db", _broken)

    # An UNEXPECTED failure is NOT a silent drop: it escapes the
    # handler so on_error_default can log it and answer "error"
    with pytest.raises(RuntimeError):
        wire.fire("join_conversation", {"conversationId": "c1"}, sid="sid-1")


def test_each_user_has_its_own_join_budget(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("mine", user["id"])
    wire.present("theirs", other["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["join_conversation"] + 3):
            wire.fire("join_conversation", {"conversationId": conv_id}, sid="mine")
        wire.fire("join_conversation", {"conversationId": conv_id}, sid="theirs")

    assert len(wire.joined) == LIMITS["join_conversation"] + 1


def test_the_join_budget_is_separate_from_the_leave_budget(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["join_conversation"] + 5):
            wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")
        wire.fire("leave_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert len(wire.joined) == LIMITS["join_conversation"]
    assert wire.left == [f"conv:{conv_id}"]


def test_the_join_gate_is_membership_not_conversation_existence(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, other["id"])
    wire.present("sid-1", user["id"])

    wire.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert wire.joined == []




# -----------------------------------------------------------
# leave_conversation
# -----------------------------------------------------------


def test_leave_never_opens_a_database_connection(wire, actor, monkeypatch):
    user, headers = actor
    wire.present("sid-1", user["id"])
    monkeypatch.setattr(chat_events, "get_db", _explode)

    wire.fire("leave_conversation", {"conversationId": "conv-anything"}, sid="sid-1")

    assert wire.left == ["conv:conv-anything"]


def test_leave_spends_quota_even_on_a_payload_it_is_about_to_drop(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["leave_conversation"]):
            wire.fire("leave_conversation", None, sid="sid-1")
        wire.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1")

    assert wire.left == []


def test_leave_from_an_unknown_sid_spends_no_quota(wire, actor):
    user, headers = actor

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["leave_conversation"] * 5):
            wire.fire("leave_conversation", {"conversationId": "conv-1"}, sid="ghost")

        wire.present("sid-1", user["id"])
        wire.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1")

    assert wire.left == ["conv:conv-1"]


@pytest.mark.parametrize("conv_id", [True, False, 0, 1, 3.5, [], (), b"conv-1", {"id": "c"}])
def test_leave_ignores_a_conversation_id_of_the_wrong_type(wire, actor, conv_id):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("leave_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert wire.left == []


def test_leave_ignores_a_missing_conversation_id_key(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("leave_conversation", {"conversation_id": "conv-1"}, sid="sid-1")

    assert wire.left == []


def test_leave_is_idempotent(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1")
    wire.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1")

    assert wire.left == ["conv:conv-1", "conv:conv-1"]


def test_leave_does_not_touch_presence_or_the_name_cache(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"], display_name="Ona")

    wire.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1")

    assert chat_events._connected_users == {"sid-1": user["id"]}
    assert chat_events._connected_names == {"sid-1": "Ona"}


def test_leave_tolerates_a_huge_conversation_id(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("leave_conversation", {"conversationId": "c" * 10000}, sid="sid-1")

    assert wire.left == ["conv:" + "c" * 10000]


def test_a_leave_room_failure_reaches_the_default_error_handler(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"])
    wire.leave_raises = RuntimeError("room registry died")

    with pytest.raises(RuntimeError):
        wire.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1")


def test_leave_returns_nothing_on_every_arm(wire, actor):
    user, headers = actor
    wire.present("sid-1", user["id"])

    assert wire.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1") is None
    assert wire.fire("leave_conversation", {"conversationId": ""}, sid="sid-1") is None
    assert wire.fire("leave_conversation", None, sid="sid-1") is None
    assert wire.fire("leave_conversation", {"conversationId": "c"}, sid="ghost") is None




# -----------------------------------------------------------
# typing
# -----------------------------------------------------------


def test_typing_spends_quota_even_on_a_payload_it_is_about_to_drop(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["typing"]):
            wire.fire("typing", {"conversationId": ""}, sid="sid-1")
        wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert wire.emits == []


def test_typing_from_an_unknown_sid_spends_no_quota(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["typing"] * 3):
            wire.fire("typing", {"conversationId": conv_id}, sid="ghost")

        wire.present("sid-1", user["id"])
        wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert len(wire.emits) == 1


def test_typing_falls_back_to_unknown_when_the_cached_name_is_empty(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"], display_name="")

    wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert wire.emits[0]["payload"]["displayName"] == "Unknown"


def test_typing_survives_a_deactivation_because_a_socket_authenticates_once(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.connect({"token": _token(headers)}, sid="sid-1")
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    # Only disconnect_user_sockets cuts a live socket; the
    # handlers never re-check the account
    assert len(wire.named("user_typing")) == 1


def test_typing_in_two_conversations_shares_one_budget(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    first = _conversation(db, user["id"], other["id"])
    second = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        for index in range(LIMITS["typing"] + 6):
            wire.fire("typing", {"conversationId": first if index % 2 else second}, sid="sid-1")

    assert len(wire.emits) == LIMITS["typing"]


def test_typing_after_the_membership_row_is_deleted_is_dropped(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])
    wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (conv_id, user["id"]))
    db.commit()
    wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert len(wire.emits) == 1


def test_typing_closes_its_connection_on_both_arms(wire, actor, make_user, db, monkeypatch):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    outsider_conv = _conversation(db, other["id"])
    wire.present("sid-1", user["id"])
    opened = _watch_db(monkeypatch)

    wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")
    wire.fire("typing", {"conversationId": outsider_conv}, sid="sid-1")

    assert [recorder.closes for recorder in opened] == [1, 1]


def test_a_typing_emit_failure_reaches_the_default_error_handler(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])
    wire.emit_raises = RuntimeError("socket gone")

    with pytest.raises(RuntimeError):
        wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")


def test_typing_never_lets_a_conversation_id_reach_sqlite_as_anything_but_a_bind(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    wire.fire("typing", {"conversationId": "x' OR '1'='1"}, sid="sid-1")

    assert wire.emits == []


@pytest.mark.parametrize("conv_id", [True, False, 0, 1, 3.5, [], (), b"c", {"id": "c"}])
def test_typing_ignores_a_conversation_id_of_the_wrong_type(wire, actor, conv_id):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert wire.emits == []


@pytest.mark.contract
def test_a_typing_broadcast_carries_exactly_three_keys(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"], display_name="Jonas")

    wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert set(wire.emits[0]["payload"]) == {"conversationId", "userId", "displayName"}


def test_typing_in_a_solo_conversation_still_addresses_the_room(wire, actor, db):
    user, headers = actor
    conv_id = _conversation(db, user["id"])
    wire.present("sid-1", user["id"])

    wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert wire.emits[0]["to"] == f"conv:{conv_id}"
    assert wire.emits[0]["include_self"] is False


def test_typing_ignores_a_spoofed_user_id_in_the_payload(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"], display_name="Jonas")

    wire.fire("typing",
              {"conversationId": conv_id, "userId": other["id"], "displayName": "Kitas"},
              sid="sid-1")

    assert wire.emits[0]["payload"]["userId"] == user["id"]
    assert wire.emits[0]["payload"]["displayName"] == "Jonas"


def test_each_socket_fans_out_the_name_cached_for_that_sid(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("phone", user["id"], display_name="Telefonas")
    wire.present("laptop", user["id"], display_name="Nesiojamas")

    wire.fire("typing", {"conversationId": conv_id}, sid="phone")
    wire.fire("typing", {"conversationId": conv_id}, sid="laptop")

    assert [e["payload"]["displayName"] for e in wire.emits] == ["Telefonas", "Nesiojamas"]




# -----------------------------------------------------------
# stop_typing
# -----------------------------------------------------------


def test_stop_typing_spends_quota_even_on_a_payload_it_is_about_to_drop(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["stop_typing"]):
            wire.fire("stop_typing", 12345, sid="sid-1")
        wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert wire.emits == []


def test_stop_typing_from_an_unknown_sid_spends_no_quota(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["stop_typing"] * 3):
            wire.fire("stop_typing", {"conversationId": conv_id}, sid="ghost")

        wire.present("sid-1", user["id"])
        wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert len(wire.emits) == 1


@pytest.mark.contract
def test_a_stop_typing_broadcast_carries_no_display_name(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"], display_name="Jonas")

    wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert set(wire.emits[0]["payload"]) == {"conversationId", "userId"}


def test_the_typing_and_stop_typing_budgets_are_separate(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["typing"] + 4):
            wire.fire("typing", {"conversationId": conv_id}, sid="sid-1")
        wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert len(wire.named("user_typing")) == LIMITS["typing"]
    assert len(wire.named("user_stop_typing")) == 1


def test_stop_typing_after_the_membership_row_is_deleted_is_dropped(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (conv_id, user["id"]))
    db.commit()

    wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert wire.emits == []


def test_stop_typing_closes_its_connection_on_both_arms(wire, actor, make_user, db, monkeypatch):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    outsider_conv = _conversation(db, other["id"])
    wire.present("sid-1", user["id"])
    opened = _watch_db(monkeypatch)

    wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")
    wire.fire("stop_typing", {"conversationId": outsider_conv}, sid="sid-1")

    assert [recorder.closes for recorder in opened] == [1, 1]


def test_a_stop_typing_emit_failure_reaches_the_default_error_handler(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])
    wire.emit_raises = RuntimeError("socket gone")

    with pytest.raises(RuntimeError):
        wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")


@pytest.mark.parametrize("conv_id", [True, False, 0, 1, 3.5, [], (), b"c"])
def test_stop_typing_ignores_a_conversation_id_of_the_wrong_type(wire, actor, conv_id):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert wire.emits == []


def test_stop_typing_ignores_a_spoofed_user_id_in_the_payload(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    wire.fire("stop_typing", {"conversationId": conv_id, "userId": other["id"]}, sid="sid-1")

    assert wire.emits[0]["payload"]["userId"] == user["id"]


def test_stop_typing_survives_a_deactivation(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.connect({"token": _token(headers)}, sid="sid-1")
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    wire.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert len(wire.named("user_stop_typing")) == 1




# -----------------------------------------------------------
# mark_read
# -----------------------------------------------------------


def test_mark_read_spends_quota_even_on_a_payload_it_is_about_to_drop(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["mark_read"]):
            wire.fire("mark_read", None, sid="sid-1")
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert _receipts(db, user["id"]) == set()
    assert _watermark(db, conv_id, user["id"]) is None


def test_mark_read_from_a_non_participant_still_spends_quota(wire, actor, make_user, db):
    user, headers = actor
    insider = make_user(username="insider")
    stranger_conv = _conversation(db, insider["id"])
    mine = _conversation(db, user["id"], insider["id"])
    _message(db, mine, insider["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["mark_read"]):
            wire.fire("mark_read", {"conversationId": stranger_conv}, sid="sid-1")
        wire.fire("mark_read", {"conversationId": mine}, sid="sid-1")

    assert _watermark(db, mine, user["id"]) is None


def test_mark_read_from_an_unknown_sid_spends_no_quota(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])

    with time_machine.travel(FROZEN, tick=False):
        for _ in range(LIMITS["mark_read"] * 3):
            wire.fire("mark_read", {"conversationId": conv_id}, sid="ghost")

        wire.present("sid-1", user["id"])
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert len(_receipts(db, user["id"])) == 1


def test_a_message_stamped_at_the_exact_call_instant_is_claimed(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    msg_id = _message(db, conv_id, sender["id"], created_at=FROZEN_ISO)
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    # The window is (prior, now] — inclusive at the top
    assert _receipts(db, user["id"]) == {msg_id}


def test_a_message_one_microsecond_past_the_call_instant_is_not_claimed(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"], created_at=FROZEN_ISO + ".000001")
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert _receipts(db, user["id"]) == set()
    assert wire.server_emits == []


def test_a_message_from_the_future_is_bounded_out_of_both_stores(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    future = (FROZEN + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    _message(db, conv_id, sender["id"], created_at=future)
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert _receipts(db, user["id"]) == set()
    assert _watermark(db, conv_id, user["id"]) == FROZEN_ISO


def test_a_message_older_than_the_watermark_is_never_claimed(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    db.execute("UPDATE conversation_participants SET last_read_at = ?"
               " WHERE conversation_id = ? AND user_id = ?",
               (FROZEN_ISO, conv_id, user["id"]))
    db.commit()
    _message(db, conv_id, sender["id"],
             created_at=(FROZEN - timedelta(hours=1)).replace(tzinfo=None).isoformat())
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN + timedelta(minutes=30), tick=False):
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    # The scan is bounded by the previous watermark, so a
    # back-dated row below it is out of reach forever
    assert _receipts(db, user["id"]) == set()


def test_mark_read_moves_the_watermark_in_a_conversation_with_no_messages(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert _watermark(db, conv_id, user["id"]) == FROZEN_ISO
    assert wire.server_emits == []


def test_mark_read_moves_the_watermark_when_only_own_messages_exist(wire, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    _message(db, conv_id, user["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False):
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert _watermark(db, conv_id, user["id"]) == FROZEN_ISO
    assert _receipts(db, user["id"]) == set()


def test_mark_read_claims_an_unsent_message_too(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    msg_id = _message(db, conv_id, sender["id"], text="",
                      deleted_at=FROZEN.replace(tzinfo=None).isoformat())
    wire.present("sid-1", user["id"])

    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    # An unsent row keeps its place in the thread, so the reader
    # still holds a receipt for it
    assert _receipts(db, user["id"]) == {msg_id}


def test_a_second_mark_read_broadcasts_nothing_but_still_moves_the_watermark(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])

    with time_machine.travel(FROZEN, tick=False) as traveller:
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")
        first = _watermark(db, conv_id, user["id"])
        wire.server_emits.clear()

        traveller.shift(timedelta(minutes=5))
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert wire.server_emits == []
    assert _watermark(db, conv_id, user["id"]) > first


def test_mark_read_writes_one_receipt_per_message_however_often_it_is_called(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])

    for _ in range(LIMITS["mark_read"]):
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert db.execute("SELECT COUNT(*) FROM message_reads WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 1


def test_mark_read_claims_only_the_foreign_messages_of_a_mixed_thread(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    mine = _message(db, conv_id, user["id"])
    theirs = _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])

    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert _receipts(db, user["id"]) == {theirs}
    assert mine not in _receipts(db, user["id"])


def test_mark_read_claims_nothing_from_another_conversation(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    read_me = _conversation(db, user["id"], sender["id"])
    leave_me = _conversation(db, user["id"], sender["id"])
    here = _message(db, read_me, sender["id"])
    _message(db, leave_me, sender["id"])
    wire.present("sid-1", user["id"])

    wire.fire("mark_read", {"conversationId": read_me}, sid="sid-1")

    assert _receipts(db, user["id"]) == {here}
    assert _watermark(db, leave_me, user["id"]) is None


@pytest.mark.parametrize("conv_id", [True, False, 0, 1, 3.5, [], (), b"c", {"id": "c"}])
def test_mark_read_ignores_a_conversation_id_of_the_wrong_type(wire, actor, conv_id):
    user, headers = actor
    wire.present("sid-1", user["id"])

    assert wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1") is None
    assert wire.server_emits == []


def test_mark_read_of_an_unknown_conversation_id_is_a_silent_drop(wire, actor, db):
    user, headers = actor
    wire.present("sid-1", user["id"])

    wire.fire("mark_read", {"conversationId": "conv-does-not-exist"}, sid="sid-1")

    assert wire.server_emits == []
    assert db.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0


def test_mark_read_from_a_user_removed_from_the_conversation_is_dropped(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (conv_id, user["id"]))
    db.commit()

    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert _receipts(db, user["id"]) == set()
    assert wire.server_emits == []


def test_mark_read_closes_its_connection_on_the_happy_path(wire, actor, make_user, db, monkeypatch):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])
    opened = _watch_db(monkeypatch)

    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    # One for the helper's transaction, one for the receipt
    # targeting inside emit_read_receipt
    assert opened and all(recorder.closes == 1 for recorder in opened)


def test_mark_read_closes_its_connection_when_the_helper_raises(wire, actor, make_user, db, monkeypatch):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    wire.present("sid-1", user["id"])
    opened = _watch_db(monkeypatch)

    from app.chat import routes as chat_routes

    def _broken(*args, **kwargs):
        raise RuntimeError("write transaction failed")

    monkeypatch.setattr(chat_routes, "_apply_mark_read", _broken)

    with pytest.raises(RuntimeError):
        wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert [recorder.closes for recorder in opened] == [1]


def test_mark_read_broadcasts_only_to_the_senders_and_the_reader(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    bystander = make_user(username="stebetojas")
    conv_id = _conversation(db, user["id"], sender["id"], bystander["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])
    wire.present("sender-sid", sender["id"])
    wire.present("bystander-sid", bystander["id"])

    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert {e["to"] for e in wire.server_emits} == {"sid-1", "sender-sid"}


def test_mark_read_broadcasts_every_claimed_id_in_one_event(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    claimed = {_message(db, conv_id, sender["id"]) for _ in range(5)}
    wire.present("sid-1", user["id"])

    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    payloads = [e["payload"] for e in wire.named("messages_read", server=True)]
    assert payloads and all(set(p["messageIds"]) == claimed for p in payloads)


def test_the_socket_path_leaves_nothing_for_the_rest_twin_to_claim(wire, client, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])

    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")
    response = client.put(f"/api/chat/conversations/{conv_id}/read", headers=headers)

    # The mobile app fires both on every read; the second one
    # finds nothing left to claim and stays quiet
    assert response.status_code == 200
    assert response.get_json()["readCount"] == 0


def test_the_rest_twin_leaves_nothing_for_the_socket_path_to_claim(wire, client, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])

    assert client.put(f"/api/chat/conversations/{conv_id}/read",
                      headers=headers).status_code == 200
    wire.server_emits.clear()
    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert wire.server_emits == []


def test_a_reader_with_two_devices_gets_the_receipt_on_both(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("phone", user["id"])
    wire.present("laptop", user["id"])

    wire.fire("mark_read", {"conversationId": conv_id}, sid="phone")

    assert {e["to"] for e in wire.server_emits} == {"phone", "laptop"}


def test_mark_read_emits_nothing_through_the_context_bound_emit(wire, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _conversation(db, user["id"], sender["id"])
    _message(db, conv_id, sender["id"])
    wire.present("sid-1", user["id"])

    wire.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    # The receipt goes out server-level, so a REST caller can
    # reuse the very same helper
    assert wire.emits == []
    assert wire.server_emits




# -----------------------------------------------------------
# on_error_default
# -----------------------------------------------------------


@pytest.mark.parametrize("event", [None, {}, [], (), 0, "", False])
def test_the_error_handler_tolerates_a_falsy_event(wire, event):
    with wire.context("sid-1"):
        flask.request.event = event
        assert wire.error_handler(RuntimeError("boom")) is None

    assert len(wire.named("error")) == 1


@pytest.mark.parametrize("event", ["typing", ["typing"], 7, 3.5, ("typing",)])
def test_the_error_handler_tolerates_a_truthy_non_dict_event(wire, event):
    with wire.context("sid-1"):
        flask.request.event = event
        wire.error_handler(RuntimeError("boom"))

    assert len(wire.named("error")) == 1


def test_the_error_handler_works_without_a_sid_on_the_request(wire):
    with wire.bare_context():
        wire.error_handler(RuntimeError("boom"))

    assert len(wire.named("error")) == 1


def test_the_error_handler_names_the_event_the_sid_and_the_user(wire, caplog):
    caplog.set_level(logging.ERROR, logger="app.chat.events")
    wire.present("sid-loud", "user-42")

    with wire.context("sid-loud"):
        flask.request.event = {"message": "mark_read", "args": ({},)}
        wire.error_handler(RuntimeError("boom"))

    assert "mark_read" in caplog.text
    assert "sid-loud" in caplog.text
    assert "user-42" in caplog.text


def test_the_error_handler_names_no_user_for_an_unknown_sid(wire, caplog):
    caplog.set_level(logging.ERROR, logger="app.chat.events")

    with wire.context("ghost"):
        flask.request.event = {"message": "typing"}
        wire.error_handler(RuntimeError("boom"))

    assert "user=None" in caplog.text


def test_the_error_handler_logs_the_traceback_of_the_original_exception(wire, caplog):
    caplog.set_level(logging.ERROR, logger="app.chat.events")

    with wire.context("sid-1"):
        try:
            raise ValueError("the original failure")
        except ValueError as exc:
            wire.error_handler(exc)

    assert "ValueError" in caplog.text
    assert "the original failure" in caplog.text


@pytest.mark.contract
def test_the_error_event_carries_no_internal_detail(wire):
    with wire.context("sid-1"):
        flask.request.event = {"message": "typing"}
        wire.error_handler(RuntimeError("connection string: secret"))

    assert wire.named("error")[0]["payload"] == {"message": "Internal error"}


def test_the_error_handler_addresses_the_offending_socket_alone(wire):
    with wire.context("sid-1"):
        wire.error_handler(RuntimeError("boom"))

    # No `to` — flask_socketio.emit inside a handler answers the
    # socket that raised
    assert wire.named("error")[0]["to"] is None


def test_the_error_handler_returns_nothing_when_the_reply_also_fails(wire):
    wire.emit_raises = RuntimeError("the socket is already gone")

    with wire.context("sid-1"):
        assert wire.error_handler(RuntimeError("boom")) is None

    assert wire.emits == []


def test_a_handler_failure_and_its_error_reply_are_one_round_trip(wire, actor, make_user, db, monkeypatch):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _conversation(db, user["id"], other["id"])
    wire.present("sid-1", user["id"])

    def _broken():
        raise RuntimeError("database gone")

    monkeypatch.setattr(chat_events, "get_db", _broken)

    # What python-socketio does for real: the handler raises,
    # the namespace's default error handler answers that socket
    with wire.context("sid-1"):
        flask.request.event = {"message": "typing", "args": ({"conversationId": conv_id},)}
        try:
            wire.handlers["typing"]({"conversationId": conv_id})
        except RuntimeError as exc:
            wire.error_handler(exc)

    assert [e["event"] for e in wire.emits] == ["error"]
