# -----------------------------------------------------------
#  [*] Tests — chat read state and presence, exhaustive pass
#
#  The gap-closing pass over four functions of
#  app/chat/routes.py:
#
#    _apply_mark_read   — the mark-read transaction both
#                         transports share
#    mark_read          — PUT  /api/chat/conversations/<id>/read
#    total_unread_count — GET  /api/chat/unread-count
#    online_status      — POST /api/chat/online-status
#
#  What it proves that the broad suite did not:
#
#    - online_status had NO test at all. Every arm is here:
#      the body guard, the non-string drop, the 200-id cap and
#      its boundary, the relationship gate (presence is only
#      revealed for people the caller shares a room with — a
#      stranger's id probes nothing), the self-lookup, and the
#      import-failure path that degrades to "everybody
#      offline" instead of a 500.
#    - The read stores are two independent stores, and the
#      boundaries where they DISAGREE: a message stamped
#      exactly at the watermark, one microsecond past it, one
#      at the epoch floor a NULL watermark uses, an unsent
#      message (out of the badge, still receipted), and a
#      future-stamped one (on the badge, never receipted).
#    - _apply_mark_read driven directly: the non-member
#      rollback leaves the connection usable, the receipts are
#      committed before it returns, and a racing twin's rows
#      are absorbed by INSERT OR IGNORE instead of raising.
#    - The watermark ADVANCES rather than being set: a `now`
#      past the stored stamp replaces it, an out-of-order one
#      older than it is dropped, so a late commit can no
#      longer un-read what an earlier call cleared.
#
#  Nothing here sleeps and nothing reaches the network; the
#  clock-dependent assertions run under time_machine.
# -----------------------------------------------------------

import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import time_machine

# The routes under test
READ_URL = "/api/chat/conversations/{}/read"
UNREAD_URL = "/api/chat/unread-count"
ONLINE_URL = "/api/chat/online-status"

# Every seeded stamp hangs off this base, comfortably in the
# past, so a route's own datetime.now() always sorts after it
# whatever day the suite runs
_BASE = datetime(2021, 5, 4, 8, 0, 0)

# The module's own bounds, restated so a test that trips one
# reads as a boundary test
_MARK_READ_CAP = 500
_ONLINE_ID_CAP = 200
_SOCKET_MARK_READ_BUDGET = 10

# The floor COALESCE puts under a NULL last_read_at
_EPOCH_FLOOR = "1970-01-01T00:00:00"




# -----------------------------------------------------------
# _stamp
# -----------------------------------------------------------
#
# A naive-UTC isoformat stamp `offset` seconds after _BASE —
# the exact string shape chat/routes.py writes and compares as
# text, so ordering seeded rows is ordering integers.
# -----------------------------------------------------------

def _stamp(offset=0):
    return (_BASE + timedelta(seconds=offset)).isoformat()




# -----------------------------------------------------------
# _seed_room
# -----------------------------------------------------------
#
# A conversation plus its membership rows written straight to
# the database: POST /conversations cannot set last_read_at,
# cannot plant a blank or future watermark, and spends a rate
# budget these tests need elsewhere. `last_read_at` takes one
# value for everybody or a {user_id: value} map.
# -----------------------------------------------------------

def _seed_room(db, member_ids, conv_type="group", title="Gilus kambarys", last_read_at=None):
    conv_id = str(uuid.uuid4())
    created = _stamp(0)

    db.execute(
        "INSERT INTO conversations (id, type, title, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, conv_type, title, member_ids[0], created, created),
    )

    if not isinstance(last_read_at, dict):
        last_read_at = {uid: last_read_at for uid in member_ids}

    db.executemany(
        "INSERT INTO conversation_participants (conversation_id, user_id, last_read_at)"
        " VALUES (?, ?, ?)",
        [(conv_id, uid, last_read_at.get(uid)) for uid in member_ids],
    )
    db.commit()

    return conv_id




# -----------------------------------------------------------
# _seed_message
# -----------------------------------------------------------
#
# One message row. `created_at` takes a raw string so a test
# can plant a future stamp, a pre-epoch one or text that is no
# date at all; `deleted` soft-deletes it the way delete_message
# would.
# -----------------------------------------------------------

def _seed_message(db, conv_id, sender_id, text="labas", offset=1, deleted=False, created_at=None):
    msg_id = str(uuid.uuid4())
    stamp = created_at if created_at is not None else _stamp(offset)

    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at, deleted_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, stamp, _stamp(offset) if deleted else None),
    )
    db.commit()

    return msg_id




# -----------------------------------------------------------
# _seed_many
# -----------------------------------------------------------
#
# `count` messages one second apart in ONE transaction — the
# cap test needs 500 rows and a commit per row would make it
# minutes long. Returns the ids oldest-first.
# -----------------------------------------------------------

def _seed_many(db, conv_id, sender_id, count, first_offset=1):
    rows = [
        (str(uuid.uuid4()), conv_id, sender_id, f"zinute {i}", _stamp(first_offset + i))
        for i in range(count)
    ]
    db.executemany(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()

    return [r[0] for r in rows]




# -----------------------------------------------------------
# _receipts
# -----------------------------------------------------------
#
# The message ids `user_id` holds a message_reads row for.
# -----------------------------------------------------------

def _receipts(db, user_id):
    rows = db.execute(
        "SELECT message_id FROM message_reads WHERE user_id = ?", (user_id,)
    ).fetchall()
    return {r["message_id"] for r in rows}




# -----------------------------------------------------------
# _watermark
# -----------------------------------------------------------
#
# The caller's last_read_at on one membership row, or the
# sentinel when the row is gone.
# -----------------------------------------------------------

def _watermark(db, conv_id, user_id):
    row = db.execute(
        "SELECT last_read_at FROM conversation_participants"
        " WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user_id),
    ).fetchone()
    return row["last_read_at"] if row else "<no membership row>"




# -----------------------------------------------------------
# _post_online
# -----------------------------------------------------------
#
# POST /online-status with the payload serialised by the
# STDLIB json, not by the app's own provider — TESTPLAN rule
# 10: a `json=` kwarg is html-escaped on the way out, which
# would silently rewrite any id carrying markup before it ever
# reached the route. `payload` may also be raw bytes, for the
# malformed and non-object body tests.
# -----------------------------------------------------------

def _post_online(client, headers, payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return client.post(ONLINE_URL, data=body,
                       headers={**(headers or {}), "Content-Type": "application/json"})




# -----------------------------------------------------------
# _ReceiptRacer
# -----------------------------------------------------------
#
# A connection wrapper that plants exactly the receipts
# _apply_mark_read is about to write, at the one instant a
# racing twin could: after the candidate SELECT has run and
# before the set-based INSERT OR IGNORE. It writes on the SAME
# connection, so the rows land inside the helper's own
# BEGIN IMMEDIATE and the insert really does collide.
#
# Used by:
#   - the racing-twin test below — the only deterministic way
#     to drive the OR IGNORE arm the module banner promises
# -----------------------------------------------------------

class _ReceiptRacer:

    def __init__(self, conn, user_id, read_at):
        self._conn = conn
        self._user_id = user_id
        self._read_at = read_at
        self.planted = []

    def execute(self, sql, params=()):
        cursor = self._conn.execute(sql, params)

        if "SELECT m.id FROM messages m" not in sql:
            return cursor

        rows = cursor.fetchall()
        for row in rows:
            self._conn.execute(
                "INSERT OR IGNORE INTO message_reads (message_id, user_id, read_at)"
                " VALUES (?, ?, ?)",
                (row["id"], self._user_id, self._read_at),
            )
            self.planted.append(row["id"])

        return SimpleNamespace(fetchall=lambda: rows)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()




# -----------------------------------------------------------
# fresh_limiter
# -----------------------------------------------------------
#
# The app's rate limiter (auth/routes.py _rate_limit_store) is
# PROCESS state, not database state, so the fresh-database
# fixtures do not reset it. This file makes hundreds of
# requests from one client IP; without the clear it would spend
# create_app's global 600-per-IP budget and the 429s would land
# on innocent modules later in the run. Cleared on both sides
# so this file neither inherits nor exports a spent budget.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_limiter():
    from app.auth.routes import _rate_limit_lock, _rate_limit_store

    with _rate_limit_lock:
        _rate_limit_store.clear()

    yield

    with _rate_limit_lock:
        _rate_limit_store.clear()




# -----------------------------------------------------------
# duo
# -----------------------------------------------------------
#
# The standing cast: alice (the signed-in caller) and bob, in
# one direct room with one message from bob and no watermark.
# Bob never logs in — most tests only need him to exist as a
# room-mate and a sender, and a login costs a bcrypt round.
# -----------------------------------------------------------

@pytest.fixture
def duo(db, actor, make_user):
    alice, alice_h = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], conv_type="direct", title=None)
    msg_id = _seed_message(db, conv_id, bob["id"], "labas", offset=1)

    return SimpleNamespace(alice=alice, alice_h=alice_h, bob=bob,
                           conv=conv_id, msg=msg_id)




# -----------------------------------------------------------
# presence
# -----------------------------------------------------------
#
#   presence(alice["id"], bob["id"])   — both hold a socket
#
# Replaces events.py's process-wide sid → user id table for
# the duration of one test. online_status imports it at CALL
# time, so rebinding the module attribute is what the route
# actually reads — and the real table, which the socket tests
# in this run fill and empty, can never make an assertion here
# flap.
# -----------------------------------------------------------

@pytest.fixture
def presence(monkeypatch):

    def _set(*user_ids):
        table = {f"sid-{i}": uid for i, uid in enumerate(user_ids)}
        monkeypatch.setattr("app.chat.events._connected_users", table)
        return table

    return _set




# -----------------------------------------------------------
# read_receipts
# -----------------------------------------------------------
#
# Captures the 'messages_read' fan-out mark_read performs
# instead of letting it reach the socket layer: a list of
# (conversationId, readerId, messageIds) tuples, in call order.
# -----------------------------------------------------------

@pytest.fixture
def read_receipts(monkeypatch):
    import app.chat.events as chat_events

    calls = []
    monkeypatch.setattr(
        chat_events, "emit_read_receipt",
        lambda sio, conv_id, reader_id, message_ids: calls.append(
            (conv_id, reader_id, list(message_ids))),
    )

    return calls




# ===========================================================
#  online_status — the body guard
# ===========================================================


def test_the_presence_lookup_requires_a_session_token(client):
    response = _post_online(client, None, {"userIds": []})

    assert response.status_code == 401


def test_a_presence_lookup_without_a_body_is_refused(client, actor):
    _, headers = actor

    response = client.post(ONLINE_URL, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "userIds array required"}


def test_a_presence_lookup_with_malformed_json_is_refused(client, actor):
    _, headers = actor

    response = _post_online(client, headers, b'{"userIds": [')

    assert response.status_code == 400
    assert response.get_json() == {"error": "userIds array required"}


def test_a_presence_lookup_with_an_empty_object_is_refused(client, actor):
    _, headers = actor

    response = _post_online(client, headers, {})

    assert response.status_code == 400
    assert response.get_json() == {"error": "userIds array required"}


def test_a_presence_lookup_with_another_field_only_is_refused(client, actor):
    _, headers = actor

    response = _post_online(client, headers, {"ids": ["a"], "userIDs": ["b"]})

    assert response.status_code == 400
    assert response.get_json() == {"error": "userIds array required"}


@pytest.mark.parametrize("body", [b'["a", "b"]', b'"labas"', b"42", b"null", b"true"])
def test_a_presence_lookup_with_a_non_object_body_is_refused(client, actor, body):
    # the app-wide validate_json_input hook catches a top-level
    # array or scalar before the route ever runs, so the message
    # differs — what matters is that no presence map comes back
    _, headers = actor

    response = _post_online(client, headers, body)

    assert response.status_code == 400
    assert "online" not in response.get_json()


@pytest.mark.parametrize("user_ids", ["abc", 7, 1.5, True, None, {"a": 1}])
def test_a_user_ids_field_that_is_not_an_array_is_refused(client, actor, user_ids):
    _, headers = actor

    response = _post_online(client, headers, {"userIds": user_ids})

    assert response.status_code == 400
    assert response.get_json() == {"error": "userIds array required"}


def test_a_presence_lookup_rejects_a_get(client, actor):
    _, headers = actor

    response = client.get(ONLINE_URL, headers=headers)

    assert response.status_code == 405




# ===========================================================
#  online_status — the answer map
# ===========================================================


def test_an_empty_id_array_answers_an_empty_map(client, actor):
    _, headers = actor

    response = _post_online(client, headers, {"userIds": []})

    assert response.status_code == 200
    assert response.get_json() == {"online": {}}


def test_an_array_of_only_non_string_ids_answers_an_empty_map(client, actor, presence):
    alice, headers = actor
    presence(alice["id"])

    response = _post_online(client, headers, {"userIds": [1, None, True, {"id": "x"}, ["x"]]})

    assert response.status_code == 200
    assert response.get_json() == {"online": {}}


def test_non_string_ids_are_dropped_and_the_rest_answered(client, actor):
    _, headers = actor
    stranger = str(uuid.uuid4())

    response = _post_online(client, headers,
                            {"userIds": [1, stranger, None, 2.5, False]})

    assert list(response.get_json()["online"]) == [stranger]


def test_an_id_that_names_nobody_reads_offline(client, actor):
    _, headers = actor
    ghost = str(uuid.uuid4())

    response = _post_online(client, headers, {"userIds": [ghost]})

    assert response.get_json() == {"online": {ghost: False}}


def test_an_empty_string_id_is_answered_offline(client, actor):
    _, headers = actor

    response = _post_online(client, headers, {"userIds": [""]})

    assert response.get_json() == {"online": {"": False}}


def test_a_repeated_id_collapses_to_one_key(client, actor, presence, duo):
    presence(duo.bob["id"])

    response = _post_online(client, duo.alice_h,
                            {"userIds": [duo.bob["id"], duo.bob["id"], duo.bob["id"]]})

    assert response.get_json() == {"online": {duo.bob["id"]: True}}


def test_a_huge_id_is_answered_offline_instead_of_crashing(client, actor):
    _, headers = actor
    huge = "x" * 10000

    response = _post_online(client, headers, {"userIds": [huge]})

    assert response.status_code == 200
    assert response.get_json()["online"][huge] is False


def test_an_id_carrying_markup_comes_back_verbatim(client, actor):
    # raw bytes on the wire (TESTPLAN rule 10): a `json=` body
    # would arrive ALREADY html-escaped and the assertion would
    # prove nothing. The answer's keys survive intact because
    # the escaping JSON provider rewrites values, never keys —
    # so an id the client sent is the id it can look up again
    _, headers = actor
    weird = '<b>&"labas"</b>'

    response = _post_online(client, headers, {"userIds": [weird]})

    assert list(response.get_json()["online"]) == [weird]


@pytest.mark.contract
def test_the_presence_answer_is_a_flat_id_to_boolean_map(client, presence, duo):
    # services/api/chat.ts fetchOnlineStatus merges these maps
    # chunk by chunk into one Record<string, boolean>
    presence(duo.bob["id"])
    stranger = str(uuid.uuid4())

    body = _post_online(client, duo.alice_h,
                        {"userIds": [duo.bob["id"], stranger]}).get_json()

    assert set(body) == {"online"}
    assert body["online"] == {duo.bob["id"]: True, stranger: False}
    assert all(isinstance(v, bool) for v in body["online"].values())




# ===========================================================
#  online_status — the relationship gate
# ===========================================================


def test_a_connected_room_mate_reads_online(client, presence, duo):
    presence(duo.bob["id"])

    response = _post_online(client, duo.alice_h, {"userIds": [duo.bob["id"]]})

    assert response.get_json()["online"][duo.bob["id"]] is True


def test_a_room_mate_without_a_socket_reads_offline(client, presence, duo):
    presence()

    response = _post_online(client, duo.alice_h, {"userIds": [duo.bob["id"]]})

    assert response.get_json()["online"][duo.bob["id"]] is False


def test_a_connected_stranger_reads_offline(client, actor, make_user, presence):
    _, headers = actor
    stranger = make_user()
    presence(stranger["id"])

    response = _post_online(client, headers, {"userIds": [stranger["id"]]})

    assert response.get_json()["online"][stranger["id"]] is False


def test_a_stranger_without_a_socket_reads_offline(client, actor, make_user, presence):
    _, headers = actor
    stranger = make_user()
    presence()

    response = _post_online(client, headers, {"userIds": [stranger["id"]]})

    assert response.get_json()["online"][stranger["id"]] is False


def test_one_call_separates_room_mates_from_strangers(client, make_user, presence, duo):
    stranger = make_user()
    presence(duo.bob["id"], stranger["id"])

    body = _post_online(client, duo.alice_h,
                        {"userIds": [duo.bob["id"], stranger["id"]]}).get_json()

    assert body["online"] == {duo.bob["id"]: True, stranger["id"]: False}


def test_a_group_room_mate_counts_as_shared(client, db, actor, make_user, presence):
    alice, headers = actor
    mate = make_user()
    _seed_room(db, [alice["id"], mate["id"], str(uuid.uuid4())], conv_type="group")
    presence(mate["id"])

    response = _post_online(client, headers, {"userIds": [mate["id"]]})

    assert response.get_json()["online"][mate["id"]] is True


def test_sharing_any_one_room_of_several_is_enough(client, db, actor, make_user, presence):
    alice, headers = actor
    mate = make_user()
    _seed_room(db, [alice["id"], str(uuid.uuid4())], conv_type="group", title="be jo")
    _seed_room(db, [alice["id"], mate["id"]], conv_type="group", title="su juo")
    presence(mate["id"])

    response = _post_online(client, headers, {"userIds": [mate["id"]]})

    assert response.get_json()["online"][mate["id"]] is True


def test_a_mate_who_left_the_only_shared_room_reads_offline(client, db, presence, duo):
    presence(duo.bob["id"])
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (duo.conv, duo.bob["id"]))
    db.commit()

    response = _post_online(client, duo.alice_h, {"userIds": [duo.bob["id"]]})

    assert response.get_json()["online"][duo.bob["id"]] is False


def test_a_caller_who_left_the_only_shared_room_sees_nobody(client, db, presence, duo):
    presence(duo.bob["id"])
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (duo.conv, duo.alice["id"]))
    db.commit()

    response = _post_online(client, duo.alice_h, {"userIds": [duo.bob["id"]]})

    assert response.get_json()["online"][duo.bob["id"]] is False


def test_the_caller_reads_their_own_presence_from_inside_a_room(client, presence, duo):
    # the participants self-join matches cp1 = cp2, so a caller
    # who belongs to at least one room shares it with themselves
    presence(duo.alice["id"])

    response = _post_online(client, duo.alice_h, {"userIds": [duo.alice["id"]]})

    assert response.get_json()["online"][duo.alice["id"]] is True


def test_a_caller_in_no_room_cannot_even_see_their_own_presence(client, actor, presence):
    alice, headers = actor
    presence(alice["id"])

    response = _post_online(client, headers, {"userIds": [alice["id"]]})

    assert response.get_json()["online"][alice["id"]] is False


def test_a_second_device_keeps_the_mate_online(client, presence, duo):
    # two sids, one user id — set(values()) collapses them
    presence(duo.bob["id"], duo.bob["id"])

    response = _post_online(client, duo.alice_h, {"userIds": [duo.bob["id"]]})

    assert response.get_json()["online"][duo.bob["id"]] is True


def test_presence_reports_the_socket_not_the_account_state(client, db, presence, duo):
    # deactivation evicts the sockets elsewhere (events.py
    # disconnect_user_sockets); this route reads presence alone
    # and never joins users, so a still-connected deactivated
    # mate reads online
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (duo.bob["id"],))
    db.commit()
    presence(duo.bob["id"])

    response = _post_online(client, duo.alice_h, {"userIds": [duo.bob["id"]]})

    assert response.get_json()["online"][duo.bob["id"]] is True


@pytest.mark.parametrize("role", ["student", "teacher", "curator", "admin"])
def test_the_gate_is_the_shared_room_for_every_caller_role(client, db, make_user,
                                                           auth_headers, presence, role):
    caller = make_user(role=role)
    mate = make_user()
    stranger = make_user()
    _seed_room(db, [caller["id"], mate["id"]], conv_type="direct", title=None)
    presence(mate["id"], stranger["id"])

    body = _post_online(client, auth_headers(caller),
                        {"userIds": [mate["id"], stranger["id"]]}).get_json()

    # not even an admin gets presence for somebody they share
    # no conversation with
    assert body["online"] == {mate["id"]: True, stranger["id"]: False}




# ===========================================================
#  online_status — the 200-id cap
# ===========================================================


def test_exactly_two_hundred_ids_are_all_answered(client, presence, duo):
    presence(duo.bob["id"])
    ids = [duo.bob["id"]] + [str(uuid.uuid4()) for _ in range(_ONLINE_ID_CAP - 1)]

    body = _post_online(client, duo.alice_h, {"userIds": ids}).get_json()

    assert len(body["online"]) == _ONLINE_ID_CAP
    assert body["online"][duo.bob["id"]] is True


def test_the_two_hundred_and_first_id_is_dropped(client, presence, duo):
    presence(duo.bob["id"])
    ids = [str(uuid.uuid4()) for _ in range(_ONLINE_ID_CAP)] + [duo.bob["id"]]

    body = _post_online(client, duo.alice_h, {"userIds": ids}).get_json()

    assert len(body["online"]) == _ONLINE_ID_CAP
    # the online mate rode past the cap and is simply absent —
    # not answered false; the mobile client chunks by 200 for
    # exactly this reason
    assert duo.bob["id"] not in body["online"]


def test_the_same_id_is_answered_once_it_fits_inside_the_cap(client, presence, duo):
    presence(duo.bob["id"])
    filler = [str(uuid.uuid4()) for _ in range(_ONLINE_ID_CAP + 50)]

    dropped = _post_online(client, duo.alice_h,
                           {"userIds": filler + [duo.bob["id"]]}).get_json()
    kept = _post_online(client, duo.alice_h,
                        {"userIds": [duo.bob["id"]] + filler}).get_json()

    assert duo.bob["id"] not in dropped["online"]
    assert kept["online"][duo.bob["id"]] is True


def test_non_string_ids_do_not_spend_the_cap(client, presence, duo):
    # the filter runs BEFORE the truncation, so 100 integers
    # cost nothing and all 200 strings are still answered
    presence(duo.bob["id"])
    ids = list(range(100)) + [duo.bob["id"]] + [str(uuid.uuid4()) for _ in range(_ONLINE_ID_CAP - 1)]

    body = _post_online(client, duo.alice_h, {"userIds": ids}).get_json()

    assert len(body["online"]) == _ONLINE_ID_CAP
    assert body["online"][duo.bob["id"]] is True




# ===========================================================
#  online_status — the presence table is unreachable
# ===========================================================


def test_presence_degrades_to_offline_when_the_socket_table_is_gone(client, monkeypatch,
                                                                    presence, duo):
    import app.chat.events as chat_events

    presence(duo.bob["id"])
    online = _post_online(client, duo.alice_h, {"userIds": [duo.bob["id"]]}).get_json()

    # the import inside the route now raises, which must read as
    # "everybody offline" rather than a 500
    monkeypatch.delattr(chat_events, "_connected_users")
    degraded = _post_online(client, duo.alice_h, {"userIds": [duo.bob["id"]]})

    assert online["online"][duo.bob["id"]] is True
    assert degraded.status_code == 200
    assert degraded.get_json() == {"online": {duo.bob["id"]: False}}




# ===========================================================
#  total_unread_count — GET /api/chat/unread-count
# ===========================================================


def test_the_tab_badge_requires_a_session_token(client):
    assert client.get(UNREAD_URL).status_code == 401


def test_the_tab_badge_rejects_a_post(client, actor):
    _, headers = actor

    assert client.post(UNREAD_URL, headers=headers).status_code == 405


@pytest.mark.contract
def test_the_tab_badge_answers_a_single_integer_field(client, duo):
    body = client.get(UNREAD_URL, headers=duo.alice_h).get_json()

    assert set(body) == {"unreadCount"}
    assert body["unreadCount"] == 1
    assert isinstance(body["unreadCount"], int)


def test_the_tab_badge_is_zero_without_any_conversation(client, actor):
    _, headers = actor

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 0


def test_the_tab_badge_sums_every_conversation(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    first = _seed_room(db, [alice["id"], bob["id"]], title="pirmas")
    second = _seed_room(db, [alice["id"], bob["id"]], title="antras")
    _seed_many(db, first, bob["id"], 2)
    _seed_many(db, second, bob["id"], 3)

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 5


def test_the_tab_badge_never_counts_the_callers_own_messages(client, db, duo):
    _seed_message(db, duo.conv, duo.alice["id"], "mano", offset=2)

    assert client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"] == 1


def test_the_tab_badge_skips_unsent_messages(client, db, duo):
    _seed_message(db, duo.conv, duo.bob["id"], "", offset=2, deleted=True)

    assert client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"] == 1


def test_a_message_stamped_exactly_at_the_watermark_is_already_read(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=_stamp(10))
    _seed_message(db, conv_id, bob["id"], created_at=_stamp(10))

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 0


def test_a_message_one_microsecond_past_the_watermark_is_unread(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=_stamp(10))
    _seed_message(db, conv_id, bob["id"], created_at=_stamp(10) + ".000001")

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 1


def test_a_null_watermark_counts_the_whole_history(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=None)
    _seed_many(db, conv_id, bob["id"], 4)

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 4


def test_a_message_at_the_epoch_floor_is_not_unread(client, db, actor, make_user):
    # COALESCE puts 1970-01-01T00:00:00 under a NULL watermark
    # and the comparison is strict, so the floor itself is read
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=None)
    _seed_message(db, conv_id, bob["id"], created_at=_EPOCH_FLOOR)

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 0


def test_a_message_older_than_the_epoch_floor_is_never_unread(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=None)
    _seed_message(db, conv_id, bob["id"], created_at="1969-12-31T23:59:59")
    _seed_message(db, conv_id, bob["id"], created_at=_EPOCH_FLOOR + ".000001")

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 1


def test_a_blank_watermark_counts_the_whole_history(client, db, actor, make_user):
    # COALESCE keeps '' — it is not NULL — and every stamp sorts
    # after the empty string, so a blank column reads as "never
    # read anything" rather than as "read everything"
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at="")
    _seed_message(db, conv_id, bob["id"], created_at="1969-12-31T23:59:59")
    _seed_message(db, conv_id, bob["id"], offset=3)

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 2


def test_a_stamp_that_is_no_date_is_still_compared_as_text(client, db, actor, make_user):
    # the badge never parses a stamp; 'labas' sorts after any
    # ISO watermark, so the row counts
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=_stamp(10))
    _seed_message(db, conv_id, bob["id"], created_at="labas")

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 1


def test_messages_of_a_room_the_caller_never_joined_do_not_count(client, db, actor, make_user):
    _, headers = actor
    bob = make_user()
    carol = make_user()
    theirs = _seed_room(db, [bob["id"], carol["id"]])
    _seed_many(db, theirs, bob["id"], 3)

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 0


def test_leaving_a_room_drops_its_messages_from_the_badge(client, db, duo):
    before = client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"]

    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (duo.conv, duo.alice["id"]))
    db.commit()

    assert before == 1
    assert client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"] == 0


def test_a_departed_senders_messages_still_count(client, db, duo):
    # the count joins the CALLER's membership only; the sender's
    # row is not consulted, so history left behind stays unread
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (duo.conv, duo.bob["id"]))
    db.commit()

    assert client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"] == 1


def test_a_deactivated_senders_messages_still_count(client, db, duo):
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (duo.bob["id"],))
    db.commit()

    assert client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"] == 1


def test_the_badge_never_consults_the_receipt_store(client, db, duo):
    # the two read stores are independent: a receipt written
    # without moving the watermark leaves the badge alone
    db.execute("INSERT INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
               (duo.msg, duo.alice["id"], _stamp(5)))
    db.commit()

    assert client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"] == 1


def test_the_badge_is_counted_per_caller(client, db, actor, make_user, auth_headers):
    alice, alice_h = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]],
                         last_read_at={alice["id"]: None, bob["id"]: _stamp(100)})
    _seed_many(db, conv_id, bob["id"], 3)
    _seed_message(db, conv_id, alice["id"], "mano", offset=50)

    assert client.get(UNREAD_URL, headers=alice_h).get_json()["unreadCount"] == 3
    assert client.get(UNREAD_URL, headers=auth_headers(bob)).get_json()["unreadCount"] == 0


def test_the_badge_agrees_with_the_sum_of_the_row_badges(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    first = _seed_room(db, [alice["id"], bob["id"]], title="pirmas")
    second = _seed_room(db, [alice["id"], bob["id"]], title="antras",
                        last_read_at={alice["id"]: _stamp(2), bob["id"]: None})
    _seed_many(db, first, bob["id"], 2)
    _seed_many(db, second, bob["id"], 4)

    total = client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"]
    rows = client.get("/api/chat/conversations", headers=headers).get_json()["conversations"]

    assert total == sum(row["unreadCount"] for row in rows)


def test_a_large_backlog_is_counted_exactly(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]])
    _seed_many(db, conv_id, bob["id"], 250)

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 250


def test_marking_read_clears_the_badge(client, db, duo):
    _seed_many(db, duo.conv, duo.bob["id"], 3, first_offset=10)

    assert client.put(READ_URL.format(duo.conv), headers=duo.alice_h).status_code == 200
    assert client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"] == 0


def test_a_future_stamped_message_survives_a_mark_read_on_the_badge(client, db, duo):
    # the `<= now` bound keeps it out of the receipt store, and
    # the watermark lands before it, so the badge keeps it too —
    # the two stores agree about a message nobody has seen yet
    _seed_message(db, duo.conv, duo.bob["id"], "is ateities", created_at="2099-01-01T00:00:00")

    read = client.put(READ_URL.format(duo.conv), headers=duo.alice_h).get_json()

    assert read["readCount"] == 1
    assert client.get(UNREAD_URL, headers=duo.alice_h).get_json()["unreadCount"] == 1




# ===========================================================
#  mark_read — the REST edge
# ===========================================================


def test_marking_a_room_read_requires_a_session_token(client, duo):
    assert client.put(READ_URL.format(duo.conv)).status_code == 401


@pytest.mark.parametrize("method", ["get", "post", "delete", "patch"])
def test_the_read_route_answers_only_to_put(client, duo, method):
    response = getattr(client, method)(READ_URL.format(duo.conv), headers=duo.alice_h)

    assert response.status_code == 405
    assert response.get_json() == {"error": "Method not allowed"}


def test_an_unknown_room_is_a_403_not_a_404(client, duo):
    # the membership lookup is the only gate, so a room that
    # never existed and one the caller never joined are
    # indistinguishable — an id cannot be probed for existence
    response = client.put(READ_URL.format(str(uuid.uuid4())), headers=duo.alice_h)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}


def test_a_non_member_cannot_mark_a_room_read(client, db, duo, make_user, auth_headers):
    outsider = make_user()

    response = client.put(READ_URL.format(duo.conv), headers=auth_headers(outsider))

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}
    assert _receipts(db, outsider["id"]) == set()
    assert _watermark(db, duo.conv, outsider["id"]) == "<no membership row>"


def test_an_admin_who_is_not_a_member_is_refused_like_anybody_else(client, db, admin, duo):
    _, admin_h = admin

    response = client.put(READ_URL.format(duo.conv), headers=admin_h)

    assert response.status_code == 403
    assert _receipts(db, duo.alice["id"]) == set()


@pytest.mark.parametrize("role", ["student", "teacher", "curator", "admin"])
def test_every_role_can_mark_a_room_it_belongs_to_read(client, db, make_user,
                                                       auth_headers, role):
    caller = make_user(role=role)
    mate = make_user()
    conv_id = _seed_room(db, [caller["id"], mate["id"]])
    msg_id = _seed_message(db, conv_id, mate["id"])

    response = client.put(READ_URL.format(conv_id), headers=auth_headers(caller))

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "readCount": 1}
    assert _receipts(db, caller["id"]) == {msg_id}


def test_marking_read_after_leaving_is_forbidden(client, db, duo):
    assert client.put(READ_URL.format(duo.conv), headers=duo.alice_h).status_code == 200

    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (duo.conv, duo.alice["id"]))
    db.commit()

    assert client.put(READ_URL.format(duo.conv), headers=duo.alice_h).status_code == 403


def test_a_conversation_id_needing_url_encoding_is_forbidden(client, duo):
    # the id reaches the route percent-decoded and is matched as
    # plain text, so a padded copy of a REAL id is a stranger
    response = client.put(f"/api/chat/conversations/%20{duo.conv}%20/read",
                          headers=duo.alice_h)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}


def test_marking_read_ignores_the_request_body(client, duo):
    response = client.put(
        READ_URL.format(duo.conv),
        data=json.dumps({"conversationId": "kitas", "readCount": 99}).encode(),
        headers={**duo.alice_h, "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "readCount": 1}


def test_marking_read_ignores_a_body_that_is_not_json(client, duo):
    response = client.put(READ_URL.format(duo.conv),
                          data=b"visai ne json",
                          headers={**duo.alice_h, "Content-Type": "text/plain"})

    assert response.status_code == 200
    assert response.get_json()["readCount"] == 1




# ===========================================================
#  mark_read — the messages_read fan-out
# ===========================================================


def test_the_broadcast_carries_the_reader_and_the_new_ids(client, db, duo, read_receipts):
    second = _seed_message(db, duo.conv, duo.bob["id"], "antra", offset=2)

    body = client.put(READ_URL.format(duo.conv), headers=duo.alice_h).get_json()

    assert body["readCount"] == 2
    assert len(read_receipts) == 1
    conv_id, reader_id, message_ids = read_receipts[0]
    assert conv_id == duo.conv
    assert reader_id == duo.alice["id"]
    assert set(message_ids) == {duo.msg, second}
    assert len(message_ids) == body["readCount"]


def test_the_broadcast_ids_are_newest_first(client, db, duo, read_receipts):
    # the candidate SELECT orders created_at DESC, so the frozen
    # {messageIds} payload is newest-first — the cap takes the
    # newest rows from the same order
    ids = _seed_many(db, duo.conv, duo.bob["id"], 3, first_offset=10)

    client.put(READ_URL.format(duo.conv), headers=duo.alice_h)

    _, _, message_ids = read_receipts[0]
    assert message_ids == list(reversed(ids)) + [duo.msg]


def test_nothing_is_broadcast_when_no_receipt_is_new(client, duo, read_receipts):
    first = client.put(READ_URL.format(duo.conv), headers=duo.alice_h).get_json()
    second = client.put(READ_URL.format(duo.conv), headers=duo.alice_h).get_json()

    assert first["readCount"] == 1
    assert second["readCount"] == 0
    assert len(read_receipts) == 1


def test_a_refused_call_broadcasts_nothing(client, duo, make_user, auth_headers, read_receipts):
    outsider = make_user()

    client.put(READ_URL.format(duo.conv), headers=auth_headers(outsider))

    assert read_receipts == []


def test_an_empty_room_broadcasts_nothing(client, db, actor, read_receipts):
    alice, headers = actor
    conv_id = _seed_room(db, [alice["id"]], title="tik as")

    response = client.put(READ_URL.format(conv_id), headers=headers)

    assert response.get_json() == {"ok": True, "readCount": 0}
    assert read_receipts == []




# ===========================================================
#  mark_read — what the two stores actually record
# ===========================================================


def test_a_receipt_that_already_exists_is_not_counted_again(client, db, duo):
    second = _seed_message(db, duo.conv, duo.bob["id"], "antra", offset=2)
    db.execute("INSERT INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
               (duo.msg, duo.alice["id"], _stamp(5)))
    db.commit()

    response = client.put(READ_URL.format(duo.conv), headers=duo.alice_h)

    assert response.get_json()["readCount"] == 1
    assert _receipts(db, duo.alice["id"]) == {duo.msg, second}


def test_the_watermark_moves_even_when_nothing_was_new(client, db, actor):
    alice, headers = actor
    conv_id = _seed_room(db, [alice["id"], str(uuid.uuid4())], title="tuscias")

    assert _watermark(db, conv_id, alice["id"]) is None
    assert client.put(READ_URL.format(conv_id), headers=headers).get_json()["readCount"] == 0
    assert _watermark(db, conv_id, alice["id"]) is not None


def test_a_second_call_pushes_the_watermark_further_forward(client, db, duo):
    with time_machine.travel(datetime(2026, 2, 3, 9, 0, 0), tick=False) as traveller:
        client.put(READ_URL.format(duo.conv), headers=duo.alice_h)
        first = _watermark(db, duo.conv, duo.alice["id"])

        traveller.shift(60)
        client.put(READ_URL.format(duo.conv), headers=duo.alice_h)
        second = _watermark(db, duo.conv, duo.alice["id"])

    assert second > first


def test_marking_read_receipts_an_unsent_message_the_badge_ignores(client, db, actor, make_user):
    # deliberate asymmetry: the badge drops soft-deleted rows so
    # it agrees with what a reader can still read, while the
    # receipt store has no deleted_at filter at all
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]])
    gone = _seed_message(db, conv_id, bob["id"], "", offset=2, deleted=True)

    assert client.get(UNREAD_URL, headers=headers).get_json()["unreadCount"] == 0

    response = client.put(READ_URL.format(conv_id), headers=headers)

    assert response.get_json()["readCount"] == 1
    assert _receipts(db, alice["id"]) == {gone}


def test_a_blank_watermark_takes_the_unbounded_scan(client, db, actor, make_user):
    # '' is falsy, so the helper drops the lower bound entirely
    # and an ancient row is receipted just as a NULL watermark
    # would receipt it
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at="")
    ancient = _seed_message(db, conv_id, bob["id"], created_at="1969-12-31T23:59:59")

    response = client.put(READ_URL.format(conv_id), headers=headers)

    assert response.get_json()["readCount"] == 1
    assert _receipts(db, alice["id"]) == {ancient}


def test_receipts_never_cross_into_another_room(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    here = _seed_room(db, [alice["id"], bob["id"]], title="cia")
    there = _seed_room(db, [alice["id"], bob["id"]], title="ten")
    mine = _seed_message(db, here, bob["id"], "cia")
    theirs = _seed_message(db, there, bob["id"], "ten")

    client.put(READ_URL.format(here), headers=headers)

    assert _receipts(db, alice["id"]) == {mine}
    assert theirs not in _receipts(db, alice["id"])
    assert _watermark(db, there, alice["id"]) is None


def test_only_the_callers_read_state_moves(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    carol = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"], carol["id"]])
    _seed_message(db, conv_id, bob["id"], "nuo bobo")
    _seed_message(db, conv_id, carol["id"], "nuo karolinos", offset=2)

    assert client.put(READ_URL.format(conv_id), headers=headers).get_json()["readCount"] == 2

    assert _watermark(db, conv_id, bob["id"]) is None
    assert _watermark(db, conv_id, carol["id"]) is None
    assert _receipts(db, bob["id"]) == set()
    assert _receipts(db, carol["id"]) == set()


def test_the_callers_own_messages_are_never_receipted(client, db, actor, make_user):
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]])
    foreign = _seed_message(db, conv_id, bob["id"], "tavo")
    _seed_message(db, conv_id, alice["id"], "mano", offset=2)

    response = client.put(READ_URL.format(conv_id), headers=headers)

    assert response.get_json()["readCount"] == 1
    assert _receipts(db, alice["id"]) == {foreign}


def test_a_room_of_one_reports_nothing_read(client, db, actor):
    alice, headers = actor
    conv_id = _seed_room(db, [alice["id"]], title="tik as")
    _seed_message(db, conv_id, alice["id"], "mano")

    response = client.put(READ_URL.format(conv_id), headers=headers)

    assert response.get_json() == {"ok": True, "readCount": 0}
    assert _receipts(db, alice["id"]) == set()


@pytest.mark.slow
def test_exactly_the_cap_is_receipted_in_one_call(client, db, actor, make_user):
    # the boundary itself: _MARK_READ_CAP rows, none left over
    alice, headers = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]])
    ids = _seed_many(db, conv_id, bob["id"], _MARK_READ_CAP)

    response = client.put(READ_URL.format(conv_id), headers=headers)

    assert response.get_json()["readCount"] == _MARK_READ_CAP
    assert _receipts(db, alice["id"]) == set(ids)




# ===========================================================
#  mark_read — the shared socket budget
# ===========================================================


def test_the_tenth_call_still_passes_and_the_eleventh_is_refused(client, db, duo):
    with time_machine.travel(datetime(2026, 2, 3, 9, 0, 0), tick=False):
        codes = [client.put(READ_URL.format(duo.conv), headers=duo.alice_h).status_code
                 for _ in range(_SOCKET_MARK_READ_BUDGET + 1)]

    assert codes[:_SOCKET_MARK_READ_BUDGET] == [200] * _SOCKET_MARK_READ_BUDGET
    assert codes[-1] == 429


def test_a_refused_call_answers_the_house_rate_limit_shape(client, duo):
    with time_machine.travel(datetime(2026, 2, 3, 9, 0, 0), tick=False):
        for _ in range(_SOCKET_MARK_READ_BUDGET):
            client.put(READ_URL.format(duo.conv), headers=duo.alice_h)
        response = client.put(READ_URL.format(duo.conv), headers=duo.alice_h)

    assert response.status_code == 429
    assert response.get_json() == {"error": "Too many requests. Please slow down.",
                                   "code": "rate_limited"}


def test_a_refused_call_leaves_the_watermark_where_it_was(client, db, duo, read_receipts):
    with time_machine.travel(datetime(2026, 2, 3, 9, 0, 0), tick=False):
        for _ in range(_SOCKET_MARK_READ_BUDGET):
            client.put(READ_URL.format(duo.conv), headers=duo.alice_h)
        before = _watermark(db, duo.conv, duo.alice["id"])
        emitted = len(read_receipts)

        assert client.put(READ_URL.format(duo.conv), headers=duo.alice_h).status_code == 429

    assert _watermark(db, duo.conv, duo.alice["id"]) == before
    assert len(read_receipts) == emitted


def test_the_budget_is_spent_per_user_not_per_room(client, db, duo):
    other = _seed_room(db, [duo.alice["id"], duo.bob["id"]], title="antras")

    with time_machine.travel(datetime(2026, 2, 3, 9, 0, 0), tick=False):
        for _ in range(_SOCKET_MARK_READ_BUDGET):
            client.put(READ_URL.format(duo.conv), headers=duo.alice_h)

        # a different conversation, the same per-user budget
        assert client.put(READ_URL.format(other), headers=duo.alice_h).status_code == 429




# ===========================================================
#  _apply_mark_read — driven directly
# ===========================================================


def test_apply_mark_read_answers_none_for_a_non_member(app, db, actor, make_user):
    from app.chat.routes import _apply_mark_read
    from app.database import get_db

    alice, _ = actor
    bob = make_user()
    conv_id = _seed_room(db, [bob["id"]], title="ne alisos")
    _seed_message(db, conv_id, bob["id"])

    conn = get_db()
    try:
        result = _apply_mark_read(conn, conv_id, alice["id"], _stamp(100))
    finally:
        conn.close()

    assert result is None
    assert _receipts(db, alice["id"]) == set()


def test_apply_mark_read_answers_none_for_an_unknown_room(app, db, actor):
    from app.chat.routes import _apply_mark_read
    from app.database import get_db

    alice, _ = actor

    conn = get_db()
    try:
        result = _apply_mark_read(conn, str(uuid.uuid4()), alice["id"], _stamp(100))
    finally:
        conn.close()

    assert result is None


def test_the_non_member_rollback_leaves_the_connection_usable(app, db, actor, make_user):
    # the guard rolls the BEGIN IMMEDIATE back; without it the
    # next call on the same connection would raise "cannot start
    # a transaction within a transaction"
    from app.chat.routes import _apply_mark_read
    from app.database import get_db

    alice, _ = actor
    bob = make_user()
    mine = _seed_room(db, [alice["id"], bob["id"]], title="mano")
    msg_id = _seed_message(db, mine, bob["id"])

    conn = get_db()
    try:
        refused = _apply_mark_read(conn, str(uuid.uuid4()), alice["id"], _stamp(100))
        allowed = _apply_mark_read(conn, mine, alice["id"], _stamp(100))
    finally:
        conn.close()

    assert refused is None
    assert allowed == [msg_id]


def test_apply_mark_read_commits_before_it_returns(app, db, actor, make_user):
    from app.chat.routes import _apply_mark_read
    from app.database import get_db

    alice, _ = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]])
    msg_id = _seed_message(db, conv_id, bob["id"])

    conn = get_db()
    try:
        ids = _apply_mark_read(conn, conv_id, alice["id"], _stamp(100))
    finally:
        conn.close()

    # a SECOND connection sees both stores, so the transaction
    # really closed rather than leaning on the caller
    assert ids == [msg_id]
    assert _receipts(db, alice["id"]) == {msg_id}
    assert _watermark(db, conv_id, alice["id"]) == _stamp(100)


def test_the_now_argument_bounds_the_receipt_window(app, db, actor, make_user):
    from app.chat.routes import _apply_mark_read
    from app.database import get_db

    alice, _ = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=_stamp(10))
    _seed_message(db, conv_id, bob["id"], "per sena", offset=5)
    at_bound = _seed_message(db, conv_id, bob["id"], "ties riba", offset=20)
    _seed_message(db, conv_id, bob["id"], "per nauja", offset=25)

    conn = get_db()
    try:
        ids = _apply_mark_read(conn, conv_id, alice["id"], _stamp(20))
    finally:
        conn.close()

    # (prior, now] — the lower bound is exclusive, the upper one
    # inclusive, and the row exactly at `now` is inside
    assert ids == [at_bound]


def test_a_racing_twins_receipt_is_absorbed_not_raised(app, db, actor, make_user):
    from app.chat.routes import _apply_mark_read
    from app.database import get_db

    alice, _ = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]])
    first = _seed_message(db, conv_id, bob["id"], "pirma", offset=1)
    second = _seed_message(db, conv_id, bob["id"], "antra", offset=2)

    conn = get_db()
    try:
        racer = _ReceiptRacer(conn, alice["id"], _stamp(50))
        ids = _apply_mark_read(racer, conv_id, alice["id"], _stamp(100))
    finally:
        conn.close()

    # the twin got there first; INSERT OR IGNORE keeps its rows
    # and the ids are STILL returned, so the broadcast is not
    # lost just because somebody else wrote the receipt
    assert set(racer.planted) == {first, second}
    assert set(ids) == {first, second}
    assert _receipts(db, alice["id"]) == {first, second}
    assert db.execute(
        "SELECT read_at FROM message_reads WHERE message_id = ? AND user_id = ?",
        (first, alice["id"]),
    ).fetchone()["read_at"] == _stamp(50)


def test_the_watermark_takes_the_now_argument_when_it_moves_forward(app, db, actor, make_user):
    # the forward half of the pair below: a `now` past the
    # stamp already on the row replaces it, receipts or no
    # receipts
    from app.chat.routes import _apply_mark_read
    from app.database import get_db

    alice, _ = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=_stamp(50))

    conn = get_db()
    try:
        ids = _apply_mark_read(conn, conv_id, alice["id"], _stamp(100))
    finally:
        conn.close()

    assert ids == []
    assert _watermark(db, conv_id, alice["id"]) == _stamp(100)


def test_an_out_of_order_call_never_moves_the_watermark_backwards(app, db, actor, make_user):
    # two concurrent mark_reads each take their own `now`; the
    # one that started EARLIER can commit last, and a bare SET
    # would drag the watermark back over messages the later call
    # had already cleared — they would reappear on the tab
    # badge. The pointer only ever advances
    from app.chat.routes import _apply_mark_read
    from app.database import get_db

    alice, _ = actor
    bob = make_user()
    conv_id = _seed_room(db, [alice["id"], bob["id"]], last_read_at=_stamp(0))
    _seed_message(db, conv_id, bob["id"], "tarp ju", offset=50)

    conn = get_db()
    try:
        _apply_mark_read(conn, conv_id, alice["id"], _stamp(100))
        _apply_mark_read(conn, conv_id, alice["id"], _stamp(10))
    finally:
        conn.close()

    assert _watermark(db, conv_id, alice["id"]) == _stamp(100)
