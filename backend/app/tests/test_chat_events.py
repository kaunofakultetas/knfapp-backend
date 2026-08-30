# -----------------------------------------------------------
#  [*] Tests — chat/events.py, the Socket.IO layer
#
#  The live side of chat, proved WITHOUT a live socket: the
#  handlers are closures register_socket_events() hands to a
#  SocketIO instance, so a fake instance captures them by name
#  and every test calls them directly. No engineio, no
#  transport, no thread — just a Flask request context with a
#  `sid` on it, exactly the one thing flask-socketio gives a
#  handler.
#
#  What this module proves:
#
#    - the handshake reads `auth: {token}` first and falls
#      back to the legacy ?token= query string, and resolves
#      it through auth's resolve_session_token — so the raw
#      token matches a sha256 row (migration v13), an expired
#      session is refused AND purged, and a deactivated user
#      cannot keep a socket
#    - a rejected handshake leaves NO presence row behind (a
#      ghost "online" user nothing ever disconnects)
#    - the connection caps hold at their boundaries, per user
#      and per process
#    - membership gates: an outsider cannot join a room, and
#      cannot inject or clear a typing indicator in one
#    - mark_read writes both read stores through the shared
#      helper and broadcasts only what was new — and spends
#      the SAME per-user budget as its REST twin
#    - the per-user sliding-window limiter bounds every
#      client → server event, prunes on every check and caps
#      its own key store
#    - the fan-out helpers address the room (or the individual
#      sids a receipt concerns) with the payload shapes
#      mobile/app/services/socket.ts declares
# -----------------------------------------------------------

import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import flask
import pytest
import time_machine

from app.chat import events as chat_events


# The window and caps the module publishes — the tests read
# them from the module so a retuned constant moves the
# boundary tests with it instead of silently passing
WINDOW = chat_events._SOCKET_RATE_WINDOW
PER_USER_CAP = chat_events._MAX_SOCKETS_PER_USER
PROCESS_CAP = chat_events._MAX_TOTAL_SOCKETS




# -----------------------------------------------------------
# _SocketHarness
# -----------------------------------------------------------
#
# Stands in for both halves of flask-socketio:
#
#   - as the SocketIO instance, it collects the handlers
#     register_socket_events() decorates (`on`,
#     `on_error_default`) and records server-level emits, so
#     handle_mark_read's closure hands IT to emit_read_receipt
#   - as the socket runtime, `context()` pushes a real Flask
#     request context and pins a sid on the request the way
#     flask-socketio's _handle_event does
#
# The context-bound primitives — emit, join_room, leave_room —
# are patched INSIDE the events module by the fixture below,
# so a handler's fan-out lands in these lists.
# -----------------------------------------------------------

class _SocketHarness:

    def __init__(self, app):
        self.app = app
        self.handlers = {}
        self.error_handler = None
        self.emits = []          # flask_socketio.emit — from inside a handler
        self.server_emits = []   # socketio.emit — the REST-side helpers
        self.joined = []
        self.left = []
        self.emit_raises = None

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
        if self.emit_raises is not None:
            raise self.emit_raises
        self.emits.append({"event": event, "payload": payload,
                           "to": to, "include_self": include_self})

    def record_join(self, room, sid=None, namespace=None):
        self.joined.append(room)

    def record_leave(self, room, sid=None, namespace=None):
        self.left.append(room)

    # --- driving the handlers ---

    @contextmanager
    def context(self, sid="sid-a", query=""):
        with self.app.test_request_context("/socket.io/" + query):
            flask.request.sid = sid
            yield

    def connect(self, auth=None, sid="sid-a", query_token=None):
        query = f"?token={query_token}" if query_token else ""
        with self.context(sid, query):
            return self.handlers["connect"](auth)

    def fire(self, event, data=None, sid="sid-a"):
        with self.context(sid):
            return self.handlers[event](data)

    def present(self, sid, user_id, display_name="Testas"):
        chat_events._connected_users[sid] = user_id
        chat_events._connected_names[sid] = display_name

    def events_named(self, name, server=False):
        source = self.server_emits if server else self.emits
        return [e for e in source if e["event"] == name]




# -----------------------------------------------------------
# sockets
# -----------------------------------------------------------
#
# The harness, wired up. _connected_users, _connected_names
# and _socket_rate are MODULE-level dicts that outlive a test,
# so they are cleared on the way in and on the way out — a
# leaked sid would make the next test's presence lookups lie.
# -----------------------------------------------------------

@pytest.fixture
def sockets(app, monkeypatch):
    harness = _SocketHarness(app)

    monkeypatch.setattr(chat_events, "emit", harness.record_emit)
    monkeypatch.setattr(chat_events, "join_room", harness.record_join)
    monkeypatch.setattr(chat_events, "leave_room", harness.record_leave)

    _reset_socket_state()
    chat_events.register_socket_events(harness)

    yield harness

    _reset_socket_state()


def _reset_socket_state():
    chat_events._connected_users.clear()
    chat_events._connected_names.clear()
    chat_events._socket_rate.clear()




# -----------------------------------------------------------
# _raw_token
# -----------------------------------------------------------
#
# The token the handshake carries is the one REST sends as a
# bearer — minted by the real login through auth_headers, not
# hand-built, so the sha256-at-rest lookup is genuinely
# exercised.
# -----------------------------------------------------------

def _raw_token(headers):
    return headers["Authorization"].split(" ", 1)[1]




# -----------------------------------------------------------
# _seed_conversation / _seed_message
# -----------------------------------------------------------
#
# Rows chat/events.py reads but never writes: a conversation
# with its participant rows, and a message stamped a minute in
# the past so it falls inside _apply_mark_read's (prior, now]
# window whatever second the test runs in.
# -----------------------------------------------------------

def _seed_conversation(db, *user_ids):
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"
    db.execute("INSERT INTO conversations (id, type, created_by) VALUES (?, 'group', ?)",
               (conv_id, user_ids[0]))
    for user_id in user_ids:
        db.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
                   (conv_id, user_id))
    db.commit()
    return conv_id


def _seed_message(db, conv_id, sender_id, text="Labas"):
    msg_id = f"msg-{uuid.uuid4().hex[:8]}"
    created = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, created),
    )
    db.commit()
    return msg_id




# -----------------------------------------------------------
# Handshake — token extraction
# -----------------------------------------------------------


def test_handshake_accepts_the_token_from_the_auth_payload(sockets, actor):
    user, headers = actor

    result = sockets.connect({"token": _raw_token(headers)}, sid="sid-1")

    assert result is not False
    assert chat_events._connected_users["sid-1"] == user["id"]
    assert sockets.events_named("connected") == [
        {"event": "connected", "payload": {"userId": user["id"]},
         "to": None, "include_self": True}
    ]


def test_handshake_falls_back_to_the_legacy_query_string_token(sockets, actor):
    user, headers = actor

    result = sockets.connect(None, sid="sid-1", query_token=_raw_token(headers))

    assert result is not False
    assert chat_events._connected_users["sid-1"] == user["id"]


def test_handshake_prefers_the_auth_payload_over_the_query_string(sockets, make_user, auth_headers):
    payload_user = make_user(username="payload_user")
    query_user = make_user(username="query_user")

    result = sockets.connect(
        {"token": _raw_token(auth_headers(payload_user))},
        sid="sid-1",
        query_token=_raw_token(auth_headers(query_user)),
    )

    assert result is not False
    assert chat_events._connected_users["sid-1"] == payload_user["id"]


def test_handshake_with_a_non_dict_auth_still_reads_the_query_string(sockets, actor):
    user, headers = actor

    result = sockets.connect(["not", "a", "dict"], sid="sid-1", query_token=_raw_token(headers))

    assert result is not False
    assert chat_events._connected_users["sid-1"] == user["id"]


def test_handshake_without_any_token_is_rejected(sockets):
    assert sockets.connect(None, sid="sid-1") is False
    assert chat_events._connected_users == {}


def test_handshake_with_an_empty_auth_token_and_no_query_is_rejected(sockets):
    assert sockets.connect({"token": ""}, sid="sid-1") is False
    assert chat_events._connected_users == {}


def test_handshake_with_a_non_string_token_is_rejected(sockets):
    assert sockets.connect({"token": 12345}, sid="sid-1") is False
    assert chat_events._connected_users == {}


def test_handshake_with_an_unknown_token_is_rejected(sockets):
    assert sockets.connect({"token": "not-a-session-token"}, sid="sid-1") is False
    assert chat_events._connected_users == {}


def test_handshake_resolves_the_raw_token_against_the_hashed_session_row(sockets, actor, db):
    user, headers = actor
    token = _raw_token(headers)

    stored = db.execute("SELECT token FROM sessions WHERE user_id = ?", (user["id"],)).fetchone()
    # Migration v13: the raw token is nowhere in the table
    assert stored["token"] != token
    assert len(stored["token"]) == 64

    assert sockets.connect({"token": token}, sid="sid-1") is not False


def test_handshake_with_an_expired_session_is_rejected_and_purges_the_row(sockets, actor, db):
    user, headers = actor
    token = _raw_token(headers)

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=31), tick=False):
        assert sockets.connect({"token": token}, sid="sid-1") is False

    assert chat_events._connected_users == {}
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 0


def test_handshake_from_a_deactivated_user_is_rejected(sockets, actor, db):
    user, headers = actor
    token = _raw_token(headers)

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert sockets.connect({"token": token}, sid="sid-1") is False
    assert chat_events._connected_users == {}


def test_a_rejected_handshake_emits_nothing(sockets):
    sockets.connect({"token": "bogus"}, sid="sid-1")

    assert sockets.emits == []




# -----------------------------------------------------------
# Handshake — auto-join, presence, failure cleanup
# -----------------------------------------------------------


def test_an_accepted_handshake_joins_every_room_of_its_user(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="pasnekovas")
    first = _seed_conversation(db, user["id"], other["id"])
    second = _seed_conversation(db, user["id"], other["id"])

    sockets.connect({"token": _raw_token(headers)}, sid="sid-1")

    assert sorted(sockets.joined) == sorted([f"conv:{first}", f"conv:{second}"])


def test_an_accepted_handshake_joins_no_room_when_the_user_has_no_conversations(sockets, actor):
    user, headers = actor

    sockets.connect({"token": _raw_token(headers)}, sid="sid-1")

    assert sockets.joined == []


def test_a_handshake_joins_no_room_of_a_conversation_the_user_left(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="likes")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (conv_id, user["id"]))
    db.commit()

    sockets.connect({"token": _raw_token(headers)}, sid="sid-1")

    assert sockets.joined == []


def test_the_handshake_caches_the_display_name_for_the_typing_fan_out(sockets, make_user, auth_headers):
    user = make_user(username="ona", display_name="Ona Onaite")

    sockets.connect({"token": _raw_token(auth_headers(user))}, sid="sid-1")

    assert chat_events._connected_names["sid-1"] == "Ona Onaite"


def test_a_user_without_a_display_name_is_cached_as_unknown(sockets, actor, db):
    user, headers = actor
    db.execute("UPDATE users SET display_name = '' WHERE id = ?", (user["id"],))
    db.commit()

    sockets.connect({"token": _raw_token(headers)}, sid="sid-1")

    assert chat_events._connected_names["sid-1"] == "Unknown"


def test_a_second_device_is_a_second_sid_for_the_same_user(sockets, actor):
    user, headers = actor
    token = _raw_token(headers)

    sockets.connect({"token": token}, sid="phone")
    sockets.connect({"token": token}, sid="laptop")

    assert chat_events._connected_users == {"phone": user["id"], "laptop": user["id"]}
    assert set(chat_events._connected_users.values()) == {user["id"]}


def test_a_handshake_that_fails_after_presence_leaves_no_ghost_online_user(sockets, actor):
    user, headers = actor
    # The ack is the last thing handle_connect does — presence
    # is already recorded when it blows up
    sockets.emit_raises = RuntimeError("socket died mid-handshake")

    result = sockets.connect({"token": _raw_token(headers)}, sid="sid-1")

    assert result is False
    assert chat_events._connected_users == {}
    assert chat_events._connected_names == {}


def test_a_handshake_whose_room_query_fails_is_rejected(sockets, actor, monkeypatch):
    user, headers = actor

    def _broken_db():
        raise RuntimeError("database gone")

    monkeypatch.setattr(chat_events, "get_db", _broken_db)

    assert sockets.connect({"token": _raw_token(headers)}, sid="sid-1") is False
    assert chat_events._connected_users == {}




# -----------------------------------------------------------
# Connection caps
# -----------------------------------------------------------


def test_a_user_may_hold_exactly_the_per_user_socket_cap(sockets, actor):
    user, headers = actor
    token = _raw_token(headers)
    for index in range(PER_USER_CAP - 1):
        sockets.present(f"old-{index}", user["id"])

    assert sockets.connect({"token": token}, sid="sid-last") is not False
    assert len(chat_events._connected_users) == PER_USER_CAP


def test_one_socket_past_the_per_user_cap_is_rejected(sockets, actor):
    user, headers = actor
    for index in range(PER_USER_CAP):
        sockets.present(f"old-{index}", user["id"])

    assert sockets.connect({"token": _raw_token(headers)}, sid="sid-extra") is False
    assert "sid-extra" not in chat_events._connected_users


def test_the_per_user_cap_does_not_block_a_different_user(sockets, make_user, auth_headers):
    hog = make_user(username="hog")
    guest = make_user(username="guest")
    for index in range(PER_USER_CAP):
        sockets.present(f"hog-{index}", hog["id"])

    assert sockets.connect({"token": _raw_token(auth_headers(guest))}, sid="guest-1") is not False


def test_a_handshake_is_rejected_once_the_process_cap_is_reached(sockets, actor):
    user, headers = actor
    for index in range(PROCESS_CAP):
        sockets.present(f"crowd-{index}", f"user-{index}")

    assert sockets.connect({"token": _raw_token(headers)}, sid="sid-extra") is False
    assert "sid-extra" not in chat_events._connected_users


def test_the_last_socket_below_the_process_cap_still_connects(sockets, actor):
    user, headers = actor
    for index in range(PROCESS_CAP - 1):
        sockets.present(f"crowd-{index}", f"user-{index}")

    assert sockets.connect({"token": _raw_token(headers)}, sid="sid-last") is not False




# -----------------------------------------------------------
# disconnect
# -----------------------------------------------------------


def test_disconnect_drops_the_sid_from_presence_and_from_the_name_cache(sockets, actor):
    user, headers = actor
    sockets.connect({"token": _raw_token(headers)}, sid="sid-1")

    sockets.fire("disconnect", "client namespace disconnect", sid="sid-1")

    assert chat_events._connected_users == {}
    assert chat_events._connected_names == {}


def test_disconnect_of_an_unknown_sid_is_a_no_op(sockets):
    sockets.fire("disconnect", None, sid="never-connected")

    assert chat_events._connected_users == {}


def test_a_user_stays_online_while_another_device_is_connected(sockets, actor):
    user, headers = actor
    token = _raw_token(headers)
    sockets.connect({"token": token}, sid="phone")
    sockets.connect({"token": token}, sid="laptop")

    sockets.fire("disconnect", None, sid="phone")

    assert set(chat_events._connected_users.values()) == {user["id"]}
    assert "laptop" in chat_events._connected_users




# -----------------------------------------------------------
# join_conversation
# -----------------------------------------------------------


def test_a_member_joins_the_conversation_room(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.joined == [f"conv:{conv_id}"]


def test_a_non_member_cannot_join_a_conversation_room(sockets, actor, make_user, db):
    user, headers = actor
    insider = make_user(username="insider")
    other = make_user(username="kitas")
    conv_id = _seed_conversation(db, insider["id"], other["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.joined == []


def test_join_from_an_unknown_sid_is_dropped(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])

    sockets.fire("join_conversation", {"conversationId": conv_id}, sid="ghost")

    assert sockets.joined == []


@pytest.mark.parametrize("payload", [None, [], "conv-1", 7])
def test_join_ignores_a_payload_that_is_not_a_dict(sockets, actor, payload):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("join_conversation", payload, sid="sid-1")

    assert sockets.joined == []


@pytest.mark.parametrize("conv_id", [None, "", 42, {"nested": "id"}])
def test_join_ignores_a_conversation_id_that_is_not_a_non_empty_string(sockets, actor, conv_id):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.joined == []


def test_join_is_capped_at_ten_per_ten_seconds_per_user(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.present("sid-1", user["id"])
    limit = chat_events._SOCKET_RATE_LIMITS["join_conversation"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(limit):
            sockets.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")
        assert len(sockets.joined) == limit

        sockets.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")
        assert len(sockets.joined) == limit

        # The window slides, not resets — one second past it the
        # oldest slots are free again
        traveller.shift(WINDOW + 1)
        sockets.fire("join_conversation", {"conversationId": conv_id}, sid="sid-1")
        assert len(sockets.joined) == limit + 1




# -----------------------------------------------------------
# leave_conversation
# -----------------------------------------------------------


def test_leave_leaves_the_room_of_a_member(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("leave_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.left == [f"conv:{conv_id}"]


def test_leave_needs_no_membership_because_leaving_a_foreign_room_is_a_no_op(sockets, actor):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("leave_conversation", {"conversationId": "conv-nobody-is-in"}, sid="sid-1")

    assert sockets.left == ["conv:conv-nobody-is-in"]


def test_leave_from_an_unknown_sid_is_dropped(sockets):
    sockets.fire("leave_conversation", {"conversationId": "conv-1"}, sid="ghost")

    assert sockets.left == []


@pytest.mark.parametrize("payload", [None, [], "conv-1"])
def test_leave_ignores_a_payload_that_is_not_a_dict(sockets, actor, payload):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("leave_conversation", payload, sid="sid-1")

    assert sockets.left == []


@pytest.mark.parametrize("conv_id", [None, "", 42])
def test_leave_ignores_a_conversation_id_that_is_not_a_non_empty_string(sockets, actor, conv_id):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("leave_conversation", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.left == []


def test_leave_is_capped_at_ten_per_ten_seconds_per_user(sockets, actor):
    user, headers = actor
    sockets.present("sid-1", user["id"])
    limit = chat_events._SOCKET_RATE_LIMITS["leave_conversation"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(limit + 5):
            sockets.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1")
        assert len(sockets.left) == limit

        traveller.shift(WINDOW + 1)
        sockets.fire("leave_conversation", {"conversationId": "conv-1"}, sid="sid-1")
        assert len(sockets.left) == limit + 1




# -----------------------------------------------------------
# typing / stop_typing
# -----------------------------------------------------------


@pytest.mark.contract
def test_typing_fans_out_to_the_room_without_the_typist(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.present("sid-1", user["id"], display_name="Jonas Jonaitis")

    sockets.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.emits == [{
        "event": "user_typing",
        "payload": {"conversationId": conv_id, "userId": user["id"],
                    "displayName": "Jonas Jonaitis"},
        "to": f"conv:{conv_id}",
        "include_self": False,
    }]


def test_an_outsider_cannot_inject_a_typing_indicator(sockets, actor, make_user, db):
    user, headers = actor
    insider = make_user(username="insider")
    other = make_user(username="kitas")
    conv_id = _seed_conversation(db, insider["id"], other["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.emits == []


def test_typing_from_an_unknown_sid_is_dropped(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])

    sockets.fire("typing", {"conversationId": conv_id}, sid="ghost")

    assert sockets.emits == []


@pytest.mark.parametrize("payload", [None, [], "conv-1"])
def test_typing_ignores_a_payload_that_is_not_a_dict(sockets, actor, payload):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("typing", payload, sid="sid-1")

    assert sockets.emits == []


@pytest.mark.parametrize("conv_id", [None, "", 42])
def test_typing_ignores_a_conversation_id_that_is_not_a_non_empty_string(sockets, actor, conv_id):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.emits == []


def test_typing_uses_the_name_cached_at_the_handshake_not_the_users_table(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.connect({"token": _raw_token(headers)}, sid="sid-1")
    sockets.emits.clear()

    # Renamed mid-session: the room keeps seeing the handshake
    # name until that socket reconnects — no users SELECT per
    # keystroke
    db.execute("UPDATE users SET display_name = 'Naujas Vardas' WHERE id = ?", (user["id"],))
    db.commit()

    sockets.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.emits[0]["payload"]["displayName"] == user["username"].title()


def test_typing_falls_back_to_unknown_when_the_name_cache_lost_the_sid(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    chat_events._connected_users["sid-1"] = user["id"]

    sockets.fire("typing", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.emits[0]["payload"]["displayName"] == "Unknown"


def test_typing_is_capped_at_twenty_per_ten_seconds_per_user(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.present("sid-1", user["id"])
    limit = chat_events._SOCKET_RATE_LIMITS["typing"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(limit + 3):
            sockets.fire("typing", {"conversationId": conv_id}, sid="sid-1")
        assert len(sockets.emits) == limit

        traveller.shift(WINDOW + 1)
        sockets.fire("typing", {"conversationId": conv_id}, sid="sid-1")
        assert len(sockets.emits) == limit + 1


def test_the_typing_budget_is_spent_per_user_not_per_socket(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.present("phone", user["id"])
    sockets.present("laptop", user["id"])
    limit = chat_events._SOCKET_RATE_LIMITS["typing"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False):
        for index in range(limit + 4):
            sockets.fire("typing", {"conversationId": conv_id},
                         sid="phone" if index % 2 else "laptop")

    assert len(sockets.emits) == limit


@pytest.mark.contract
def test_stop_typing_fans_out_to_the_room_without_the_typist(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.present("sid-1", user["id"], display_name="Jonas")

    sockets.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.emits == [{
        "event": "user_stop_typing",
        "payload": {"conversationId": conv_id, "userId": user["id"]},
        "to": f"conv:{conv_id}",
        "include_self": False,
    }]


def test_an_outsider_cannot_clear_a_typing_indicator(sockets, actor, make_user, db):
    user, headers = actor
    insider = make_user(username="insider")
    other = make_user(username="kitas")
    conv_id = _seed_conversation(db, insider["id"], other["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.emits == []


def test_stop_typing_from_an_unknown_sid_is_dropped(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])

    sockets.fire("stop_typing", {"conversationId": conv_id}, sid="ghost")

    assert sockets.emits == []


@pytest.mark.parametrize("payload", [None, [], "conv-1"])
def test_stop_typing_ignores_a_payload_that_is_not_a_dict(sockets, actor, payload):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("stop_typing", payload, sid="sid-1")

    assert sockets.emits == []


@pytest.mark.parametrize("conv_id", [None, "", 42])
def test_stop_typing_ignores_a_conversation_id_that_is_not_a_non_empty_string(sockets, actor, conv_id):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.emits == []


def test_stop_typing_is_capped_at_twenty_per_ten_seconds_per_user(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    sockets.present("sid-1", user["id"])
    limit = chat_events._SOCKET_RATE_LIMITS["stop_typing"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(limit + 3):
            sockets.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")
        assert len(sockets.emits) == limit

        traveller.shift(WINDOW + 1)
        sockets.fire("stop_typing", {"conversationId": conv_id}, sid="sid-1")
        assert len(sockets.emits) == limit + 1




# -----------------------------------------------------------
# mark_read
# -----------------------------------------------------------


def _last_read_at(db, conv_id, user_id):
    row = db.execute(
        "SELECT last_read_at FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user_id),
    ).fetchone()
    return row["last_read_at"]


def test_mark_read_writes_receipts_and_moves_the_watermark(sockets, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _seed_conversation(db, user["id"], sender["id"])
    first = _seed_message(db, conv_id, sender["id"])
    second = _seed_message(db, conv_id, sender["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    receipts = {r["message_id"] for r in db.execute(
        "SELECT message_id FROM message_reads WHERE user_id = ?", (user["id"],))}
    assert receipts == {first, second}
    assert _last_read_at(db, conv_id, user["id"]) is not None


@pytest.mark.contract
def test_mark_read_broadcasts_messages_read_to_the_sender_and_the_reader(sockets, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _seed_conversation(db, user["id"], sender["id"])
    msg_id = _seed_message(db, conv_id, sender["id"])
    sockets.present("sid-1", user["id"])
    sockets.present("sender-sid", sender["id"])

    sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    payloads = [e["payload"] for e in sockets.events_named("messages_read", server=True)]
    assert payloads and all(
        p == {"conversationId": conv_id, "readerId": user["id"], "messageIds": [msg_id]}
        for p in payloads
    )
    assert {e["to"] for e in sockets.events_named("messages_read", server=True)} == {"sid-1", "sender-sid"}


def test_mark_read_never_writes_a_receipt_for_the_readers_own_message(sockets, actor, make_user, db):
    user, headers = actor
    other = make_user(username="narys")
    conv_id = _seed_conversation(db, user["id"], other["id"])
    _seed_message(db, conv_id, user["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert db.execute("SELECT COUNT(*) FROM message_reads WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 0
    assert sockets.server_emits == []


def test_mark_read_broadcasts_nothing_when_there_is_nothing_new(sockets, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _seed_conversation(db, user["id"], sender["id"])
    _seed_message(db, conv_id, sender["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")
    sockets.server_emits.clear()
    sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.server_emits == []


def test_mark_read_from_a_non_participant_is_a_silent_drop(sockets, actor, make_user, db):
    user, headers = actor
    insider = make_user(username="insider")
    other = make_user(username="kitas")
    conv_id = _seed_conversation(db, insider["id"], other["id"])
    _seed_message(db, conv_id, insider["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.server_emits == []
    assert db.execute("SELECT COUNT(*) FROM message_reads WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 0


def test_mark_read_from_an_unknown_sid_is_dropped(sockets, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _seed_conversation(db, user["id"], sender["id"])
    _seed_message(db, conv_id, sender["id"])

    sockets.fire("mark_read", {"conversationId": conv_id}, sid="ghost")

    assert db.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0


@pytest.mark.parametrize("payload", [None, [], "conv-1"])
def test_mark_read_ignores_a_payload_that_is_not_a_dict(sockets, actor, make_user, db, payload):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _seed_conversation(db, user["id"], sender["id"])
    _seed_message(db, conv_id, sender["id"])
    sockets.present("sid-1", user["id"])

    sockets.fire("mark_read", payload, sid="sid-1")

    assert db.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0


@pytest.mark.parametrize("conv_id", [None, "", 42])
def test_mark_read_ignores_a_conversation_id_that_is_not_a_non_empty_string(sockets, actor, conv_id):
    user, headers = actor
    sockets.present("sid-1", user["id"])

    sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

    assert sockets.server_emits == []


def test_mark_read_is_capped_at_ten_per_ten_seconds_per_user(sockets, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _seed_conversation(db, user["id"], sender["id"])
    sockets.present("sid-1", user["id"])
    limit = chat_events._SOCKET_RATE_LIMITS["mark_read"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(limit):
            sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

        # An accepted call always moves the watermark, so a
        # sentinel left untouched is a rejected one
        db.execute("UPDATE conversation_participants SET last_read_at = 'sentinel'"
                   " WHERE conversation_id = ? AND user_id = ?", (conv_id, user["id"]))
        db.commit()

        sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")
        assert _last_read_at(db, conv_id, user["id"]) == "sentinel"

        traveller.shift(WINDOW + 1)
        sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")
        assert _last_read_at(db, conv_id, user["id"]) != "sentinel"


def test_the_socket_mark_read_budget_is_shared_with_the_rest_twin(sockets, client, actor, make_user, db):
    user, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _seed_conversation(db, user["id"], sender["id"])
    sockets.present("sid-1", user["id"])
    limit = chat_events._SOCKET_RATE_LIMITS["mark_read"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False):
        for _ in range(limit):
            sockets.fire("mark_read", {"conversationId": conv_id}, sid="sid-1")

        response = client.put(f"/api/chat/conversations/{conv_id}/read", headers=headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"




# -----------------------------------------------------------
# _socket_rate_check — the limiter itself
# -----------------------------------------------------------


def test_an_event_with_no_configured_limit_is_never_rejected(sockets):
    assert all(chat_events._socket_rate_check("user-1", "unlisted_event") is False
               for _ in range(200))
    assert ("user-1", "unlisted_event") not in chat_events._socket_rate


def test_a_rejected_event_does_not_spend_a_slot_of_the_next_window(sockets):
    limit = chat_events._SOCKET_RATE_LIMITS["typing"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(limit):
            assert chat_events._socket_rate_check("user-1", "typing") is False
        for _ in range(50):
            assert chat_events._socket_rate_check("user-1", "typing") is True

        traveller.shift(WINDOW + 1)
        # The 50 rejections were never recorded, so the whole
        # quota is back
        accepted = sum(0 if chat_events._socket_rate_check("user-1", "typing") else 1
                       for _ in range(limit))
        assert accepted == limit


def test_the_window_slides_instead_of_resetting(sockets):
    limit = chat_events._SOCKET_RATE_LIMITS["mark_read"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(limit):
            chat_events._socket_rate_check("user-1", "mark_read")

        # Half the window later the old timestamps are still live
        traveller.shift(WINDOW / 2)
        assert chat_events._socket_rate_check("user-1", "mark_read") is True

        # Past the window they are pruned, and the store keeps
        # only the live ones
        traveller.shift(WINDOW / 2 + 1)
        assert chat_events._socket_rate_check("user-1", "mark_read") is False
        assert len(chat_events._socket_rate[("user-1", "mark_read")]) == 1


def test_each_user_and_event_pair_has_its_own_budget(sockets):
    limit = chat_events._SOCKET_RATE_LIMITS["mark_read"]

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False):
        for _ in range(limit):
            chat_events._socket_rate_check("user-1", "mark_read")

        assert chat_events._socket_rate_check("user-1", "mark_read") is True
        assert chat_events._socket_rate_check("user-1", "typing") is False
        assert chat_events._socket_rate_check("user-2", "mark_read") is False


def test_the_rate_store_is_bounded_in_lru_order(sockets):
    over = chat_events._SOCKET_RATE_MAX_KEYS + 1

    with time_machine.travel(datetime(2026, 3, 1, tzinfo=timezone.utc), tick=False):
        for index in range(over):
            chat_events._socket_rate_check(f"user-{index}", "typing")

    assert len(chat_events._socket_rate) == chat_events._SOCKET_RATE_MAX_KEYS
    # The oldest untouched key went first
    assert ("user-0", "typing") not in chat_events._socket_rate
    assert (f"user-{over - 1}", "typing") in chat_events._socket_rate




# -----------------------------------------------------------
# disconnect_user_sockets — the revocation kill switch
# -----------------------------------------------------------


def test_disconnect_user_sockets_closes_every_socket_of_that_user(sockets, monkeypatch):
    from app import socketio as real_socketio

    closed_sids = []
    monkeypatch.setattr(real_socketio.server, "disconnect",
                        lambda sid, namespace=None: closed_sids.append(sid))
    sockets.present("phone", "user-1")
    sockets.present("laptop", "user-1")
    sockets.present("other", "user-2")

    assert chat_events.disconnect_user_sockets("user-1") == 2
    assert sorted(closed_sids) == ["laptop", "phone"]
    assert chat_events._connected_users == {"other": "user-2"}
    assert chat_events._connected_names == {"other": "Testas"}


def test_disconnect_user_sockets_returns_zero_for_a_user_with_no_sockets(sockets):
    sockets.present("other", "user-2")

    assert chat_events.disconnect_user_sockets("user-1") == 0
    assert chat_events._connected_users == {"other": "user-2"}


def test_a_socket_that_cannot_be_closed_still_leaves_presence_clean(sockets, monkeypatch):
    from app import socketio as real_socketio

    def _raise(sid, namespace=None):
        raise RuntimeError("sid already gone")

    monkeypatch.setattr(real_socketio.server, "disconnect", _raise)
    sockets.present("phone", "user-1")

    assert chat_events.disconnect_user_sockets("user-1") == 0
    assert chat_events._connected_users == {}
    assert chat_events._connected_names == {}


def test_disconnect_user_sockets_survives_a_socket_layer_that_is_not_up(sockets, monkeypatch):
    monkeypatch.delattr(sys.modules["app"], "socketio")
    sockets.present("phone", "user-1")

    assert chat_events.disconnect_user_sockets("user-1") == 0
    # Nothing was closed, so presence is left exactly as it was
    assert chat_events._connected_users == {"phone": "user-1"}




# -----------------------------------------------------------
# on_error_default
# -----------------------------------------------------------


def test_the_default_error_handler_answers_the_offending_socket(sockets):
    sockets.present("sid-1", "user-1")

    with sockets.context("sid-1"):
        flask.request.event = {"message": "typing", "args": ({},)}
        sockets.error_handler(RuntimeError("boom"))

    assert sockets.emits == [{"event": "error", "payload": {"message": "Internal error"},
                              "to": None, "include_self": True}]


def test_the_default_error_handler_tolerates_a_non_dict_event(sockets):
    with sockets.context("sid-1"):
        flask.request.event = "typing"
        sockets.error_handler(RuntimeError("boom"))

    assert sockets.events_named("error")


def test_the_default_error_handler_turns_one_exception_into_one(sockets):
    sockets.emit_raises = RuntimeError("the socket is already gone")

    with sockets.context("sid-1"):
        sockets.error_handler(RuntimeError("boom"))

    assert sockets.emits == []




# -----------------------------------------------------------
# The fan-out helpers the REST routes call
# -----------------------------------------------------------


@pytest.mark.contract
def test_emit_new_message_addresses_the_conversation_room(sockets):
    payload = {"id": "m1", "conversationId": "c1", "senderId": "u1", "text": "Labas"}

    chat_events.emit_new_message(sockets, "c1", payload)

    assert sockets.server_emits == [{"event": "new_message", "payload": payload, "to": "conv:c1"}]


@pytest.mark.contract
def test_emit_reaction_update_carries_the_authoritative_reactions(sockets):
    reactions = [{"emoji": "👍", "count": 2, "byUserIds": ["u1", "u2"]}]

    chat_events.emit_reaction_update(sockets, "c1", "m1", reactions)

    assert sockets.server_emits == [{
        "event": "reaction_update",
        "payload": {"conversationId": "c1", "messageId": "m1", "reactions": reactions},
        "to": "conv:c1",
    }]


@pytest.mark.contract
def test_emit_message_deleted_names_the_conversation_and_the_message(sockets):
    chat_events.emit_message_deleted(sockets, "c1", "m1")

    assert sockets.server_emits == [{
        "event": "message_deleted",
        "payload": {"conversationId": "c1", "messageId": "m1"},
        "to": "conv:c1",
    }]


def test_a_read_receipt_reaches_the_senders_and_the_readers_own_devices_only(sockets, actor, make_user, db):
    reader, headers = actor
    sender = make_user(username="siuntejas")
    bystander = make_user(username="stebetojas")
    conv_id = _seed_conversation(db, reader["id"], sender["id"], bystander["id"])
    msg_id = _seed_message(db, conv_id, sender["id"])
    sockets.present("reader-phone", reader["id"])
    sockets.present("reader-laptop", reader["id"])
    sockets.present("sender-sid", sender["id"])
    sockets.present("bystander-sid", bystander["id"])

    chat_events.emit_read_receipt(sockets, conv_id, reader["id"], [msg_id])

    assert {e["to"] for e in sockets.server_emits} == {"reader-phone", "reader-laptop", "sender-sid"}


def test_a_read_receipt_for_no_messages_falls_back_to_the_room(sockets):
    chat_events.emit_read_receipt(sockets, "c1", "u1", [])

    assert sockets.server_emits == [{
        "event": "messages_read",
        "payload": {"conversationId": "c1", "readerId": "u1", "messageIds": []},
        "to": "conv:c1",
    }]


def test_a_read_receipt_too_wide_for_one_in_clause_falls_back_to_the_room(sockets):
    message_ids = [f"m{index}" for index in range(901)]

    chat_events.emit_read_receipt(sockets, "c1", "u1", message_ids)

    assert [e["to"] for e in sockets.server_emits] == ["conv:c1"]


def test_a_read_receipt_falls_back_to_the_room_when_targeting_fails(sockets, monkeypatch):
    def _broken_db():
        raise RuntimeError("database gone")

    monkeypatch.setattr(chat_events, "get_db", _broken_db)

    chat_events.emit_read_receipt(sockets, "c1", "u1", ["m1"])

    assert [e["to"] for e in sockets.server_emits] == ["conv:c1"]


def test_a_receipt_for_a_sender_who_is_offline_reaches_the_reader_alone(sockets, actor, make_user, db):
    reader, headers = actor
    sender = make_user(username="siuntejas")
    conv_id = _seed_conversation(db, reader["id"], sender["id"])
    msg_id = _seed_message(db, conv_id, sender["id"])
    sockets.present("reader-phone", reader["id"])

    chat_events.emit_read_receipt(sockets, conv_id, reader["id"], [msg_id])

    assert [e["to"] for e in sockets.server_emits] == ["reader-phone"]


def test_the_receipt_targeting_ignores_message_ids_that_do_not_exist(sockets, actor):
    reader, headers = actor
    sockets.present("reader-phone", reader["id"])

    chat_events.emit_read_receipt(sockets, "c1", reader["id"], ["no-such-message"])

    assert [e["to"] for e in sockets.server_emits] == ["reader-phone"]
