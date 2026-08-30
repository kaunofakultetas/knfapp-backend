# -----------------------------------------------------------
#  [*] Tests — chat messages, the exhaustive pass
#
#  The gap-closing sweep over FOUR functions of
#  app/chat/routes.py and nothing else:
#
#    get_messages          — GET  .../conversations/<id>/messages
#    send_message          — POST .../conversations/<id>/messages
#    _find_committed_send  — the idempotent-replay lookup
#    _push_chat_message    — the off-thread push fan-out
#
#  Where the broad suite proves the happy paths, this file
#  walks the arms it does not reach: every guard clause with
#  the value that trips it, every boundary on both sides (0,
#  1, cap, cap+1, empty, blank, None, wrong type, huge), every
#  arm of the own-message status math including the ones a
#  ghost receipt or a departed member produces, the composite
#  cursor's OR arms one by one, every imageUrl a client can
#  put on the wire, and the four ways the push task can be
#  handed a broken world (no batched notifier, no
#  send_push_batch at all, no recipients, a raising transport)
#  — each of which it must swallow, because push never owes
#  anybody an error.
#
#  Arrangement is planted straight through the `db` fixture
#  wherever a route refuses to create the state: fabricated
#  ordered stamps, equal-stamp siblings, receipts from people
#  who are not members, a quote whose sender row vanished.
#  Where what is ON THE WIRE matters (entities, NUL bytes) the
#  body is posted as raw bytes — a `json=` kwarg would be
#  html-escaped by the app's own provider before it left the
#  test client (TESTPLAN rule 10).
# -----------------------------------------------------------


import json
import sqlite3
import uuid

import pytest




# -----------------------------------------------------------
# chat_routes / chat_events
# -----------------------------------------------------------
#
# The modules under test, imported only after the `app`
# fixture has pinned DB_PATH — the package must never be
# pulled in against a stray environment at collection time.
#
# Used by:
#   - the unit tests and every monkeypatching test below
# -----------------------------------------------------------

@pytest.fixture
def chat_routes(app):
    from app.chat import routes
    return routes


@pytest.fixture
def chat_events(app):
    from app.chat import events
    return events




# -----------------------------------------------------------
# _stamp
# -----------------------------------------------------------
#
# A naive-UTC isoformat stamp in the exact shape the routes
# write and every cursor compares as TEXT. Far in the past so
# a planted row always sorts before anything a route stamps
# with the real clock, and zero-padded so string order equals
# chronological order.
#
# Used by:
#   - every planting helper and all the paging tests
# -----------------------------------------------------------

def _stamp(index, micro=0):
    return f"2020-01-01T10:{index // 60:02d}:{index % 60:02d}.{micro:06d}"




# -----------------------------------------------------------
# _plant_* — rows the routes refuse to create
# -----------------------------------------------------------
#
# Written through the test connection, which runs with PRAGMA
# foreign_keys OFF: that is what lets a test plant a receipt
# from somebody who is not a member, a quote whose sender row
# vanished, or a group with no title.
#
# Used by:
#   - most tests here
# -----------------------------------------------------------

def _plant_conversation(db, member_ids, conv_type="group", title="Kursiokai",
                        conv_id=None, avatar_emoji=None):
    conv_id = conv_id or f"conv-{uuid.uuid4().hex[:8]}"
    now = _stamp(0)
    db.execute(
        "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conv_id, conv_type, None if conv_type == "direct" else title, avatar_emoji,
         member_ids[0] if member_ids else None, now, now),
    )
    for uid in member_ids:
        db.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id, last_read_at)"
            " VALUES (?, ?, NULL)",
            (conv_id, uid),
        )
    db.commit()
    return conv_id


def _plant_message(db, conv_id, sender_id, text="", created_at=None, msg_id=None,
                   image_url=None, reply_to_id=None, deleted_at=None, client_msg_id=None):
    msg_id = msg_id or f"msg-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, reply_to_id,"
        " client_msg_id, deleted_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, image_url, reply_to_id, client_msg_id,
         deleted_at, _stamp(0) if created_at is None else created_at),
    )
    db.commit()
    return msg_id


def _plant_receipt(db, msg_id, user_id):
    db.execute("INSERT OR IGNORE INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
               (msg_id, user_id, _stamp(1)))
    db.commit()


def _plant_reaction(db, msg_id, user_id, emoji):
    db.execute("INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
               (msg_id, user_id, emoji))
    db.commit()


def _plant_token(db, user_id, token, active=1):
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform, active) VALUES (?, ?, ?, 'ios', ?)",
        (str(uuid.uuid4()), user_id, token, active),
    )
    db.commit()




# -----------------------------------------------------------
# _post_raw
# -----------------------------------------------------------
#
# A body that reaches the server EXACTLY as written. The test
# client serialises a `json=` kwarg through the app's own
# escaping provider, so `{"text": "I <3 you"}` would arrive
# already entity-encoded — which no real client ever sends and
# which silently falsifies every assertion about stored text.
#
# Used by:
#   - the entity / NUL-byte / verbatim-storage tests
# -----------------------------------------------------------

def _post_raw(client, path, payload, headers):
    return client.post(path, data=json.dumps(payload),
                       headers={**headers, "Content-Type": "application/json"})




# -----------------------------------------------------------
# room / trio / paged_room / big_room
# -----------------------------------------------------------
#
# room      — a two-person direct chat (actor + Ona)
# trio      — a three-person group (actor + Ona + Jonas), the
#             arrangement the status math needs: others_count
#             is 2, so "delivered" has a middle ground
# paged_room— five planted messages on ordered stamps
# big_room  — 105 planted messages, for the 100-row cap
#
# Used by:
#   - the sections named in each banner below
# -----------------------------------------------------------

@pytest.fixture
def room(db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user(display_name="Ona Onaitė")
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="direct")
    return conv_id, user, headers, other, auth_headers(other)


@pytest.fixture
def trio(db, actor, make_user, auth_headers):
    user, headers = actor
    ona = make_user(display_name="Ona Onaitė")
    jonas = make_user(display_name="Jonas Jonaitis")
    conv_id = _plant_conversation(db, [user["id"], ona["id"], jonas["id"]])
    return conv_id, user, headers, ona, jonas


@pytest.fixture
def paged_room(db, room):
    conv_id, user, headers, other, _ = room
    ids = [_plant_message(db, conv_id, user["id"], text=f"m{i}",
                          created_at=_stamp(i), msg_id=f"msg-{i:03d}")
           for i in range(5)]
    return conv_id, headers, ids


@pytest.fixture
def big_room(db, room):
    conv_id, user, headers, other, _ = room
    rows = [(f"msg-{i:03d}", conv_id, user["id"], f"m{i}", None, None, None, None, _stamp(i))
            for i in range(105)]
    db.executemany(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, reply_to_id,"
        " client_msg_id, deleted_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()
    return conv_id, headers




# -----------------------------------------------------------
# no_push
# -----------------------------------------------------------
#
# Swallows the background push task so a send test never
# spawns a thread that outlives it, and hands back what the
# route passed to start_background_task so the fan-out tests
# read their assertions off it.
#
# Used by:
#   - every test that POSTs a message
# -----------------------------------------------------------

@pytest.fixture
def no_push(chat_routes, monkeypatch):
    calls = []
    monkeypatch.setattr(chat_routes._get_socketio(), "start_background_task",
                        lambda func, *args, **kwargs: calls.append((func, args, kwargs)))
    return calls




# -----------------------------------------------------------
# quiet_socket
# -----------------------------------------------------------
#
# Silences the live 'new_message' emit. Needed by the test
# that breaks socketio's room table: the real emit walks that
# very table, while the code under test is the push-recipient
# selection that reads it afterwards.
#
# Used by:
#   - the broken-room-table push test
# -----------------------------------------------------------

@pytest.fixture
def quiet_socket(chat_events, monkeypatch):
    monkeypatch.setattr(chat_events, "emit_new_message", lambda *args, **kwargs: None)




# -----------------------------------------------------------
# push_module
# -----------------------------------------------------------
#
# app.notifications.push with BOTH entry points removed, so a
# test opts back in to exactly the one it is about: leave it
# bare and _push_chat_message's imports fail; add
# send_push_batch and the standalone fallback runs; add
# notify_channel_users and the batched path wins.
#
# Used by:
#   - the _push_chat_message unit tests
# -----------------------------------------------------------

@pytest.fixture
def push_module(app, monkeypatch):
    from app.notifications import push as module
    monkeypatch.delattr(module, "notify_channel_users", raising=False)
    monkeypatch.delattr(module, "send_push_batch", raising=False)
    return module








# ===========================================================
#  GET .../messages — the page and its composite cursor
# ===========================================================


def test_an_empty_before_parameter_falls_back_to_the_unfiltered_page(client, paged_room):
    conv_id, headers, _ = paged_room

    response = client.get(f"/api/chat/conversations/{conv_id}/messages?before=", headers=headers)

    assert response.status_code == 200
    assert [m["text"] for m in response.get_json()["messages"]] == ["m0", "m1", "m2", "m3", "m4"]


def test_the_page_ships_the_newest_rows_not_the_oldest(client, paged_room):
    conv_id, headers, _ = paged_room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=2",
                      headers=headers).get_json()

    assert [m["text"] for m in body["messages"]] == ["m3", "m4"]
    assert body["hasMore"] is True


def test_a_second_page_continues_without_a_gap_or_an_overlap(client, paged_room):
    conv_id, headers, _ = paged_room

    first = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=2",
                       headers=headers).get_json()["messages"]
    oldest = first[0]
    second = client.get(
        f"/api/chat/conversations/{conv_id}/messages?limit=2"
        f"&before={oldest['createdAt']}&before_id={oldest['id']}",
        headers=headers,
    ).get_json()

    assert [m["text"] for m in second["messages"]] == ["m1", "m2"]
    assert second["hasMore"] is True


def test_equal_stamp_siblings_page_by_id_under_the_same_stamp(client, db, room):
    conv_id, user, headers, _, _ = room
    for msg_id in ("msg-a", "msg-b", "msg-c"):
        _plant_message(db, conv_id, user["id"], text=msg_id, created_at=_stamp(5), msg_id=msg_id)

    body = client.get(
        f"/api/chat/conversations/{conv_id}/messages?before={_stamp(5)}&before_id=msg-b",
        headers=headers,
    ).get_json()

    # Strictly "older" under (created_at DESC, id DESC) means a
    # smaller id when the stamps match to the microsecond
    assert [m["id"] for m in body["messages"]] == ["msg-a"]
    assert body["hasMore"] is False


def test_a_cursor_id_below_every_sibling_ends_the_page(client, db, room):
    conv_id, user, headers, _, _ = room
    for msg_id in ("msg-a", "msg-b", "msg-c"):
        _plant_message(db, conv_id, user["id"], created_at=_stamp(5), msg_id=msg_id)

    body = client.get(
        f"/api/chat/conversations/{conv_id}/messages?before={_stamp(5)}&before_id=msg-a",
        headers=headers,
    ).get_json()

    assert body["messages"] == []
    assert body["hasMore"] is False


def test_a_strictly_older_stamp_wins_whatever_the_cursor_id_says(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="senas", created_at=_stamp(1), msg_id="msg-z")
    _plant_message(db, conv_id, user["id"], text="naujas", created_at=_stamp(5), msg_id="msg-a")

    body = client.get(
        f"/api/chat/conversations/{conv_id}/messages?before={_stamp(5)}&before_id=msg-a",
        headers=headers,
    ).get_json()

    # "msg-z" > "msg-a" as text, but its stamp is older — the
    # first arm of the OR is what must catch it
    assert [m["text"] for m in body["messages"]] == ["senas"]


def test_a_cursor_stamp_newer_than_everything_answers_the_whole_history(client, paged_room):
    conv_id, headers, _ = paged_room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?before=9999-01-01T00:00:00",
                      headers=headers).get_json()

    assert len(body["messages"]) == 5
    assert body["hasMore"] is False


def test_a_cursor_stamp_older_than_everything_answers_an_empty_page(client, paged_room):
    conv_id, headers, _ = paged_room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?before=1970-01-01T00:00:00",
                      headers=headers).get_json()

    assert body["messages"] == []
    assert body["hasMore"] is False








# ===========================================================
#  GET .../messages — ?limit, every boundary of the clamp
# ===========================================================


@pytest.mark.parametrize("raw,expected", [
    ("1", 1),
    ("4", 4),
    ("5", 5),
    (" 3 ", 3),
    ("+2", 2),
    ("0", 1),
    ("-1", 1),
    ("-9999", 1),
])
def test_the_limit_is_clamped_into_one_to_one_hundred(client, paged_room, raw, expected):
    conv_id, headers, _ = paged_room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit={raw}",
                      headers=headers).get_json()

    assert len(body["messages"]) == expected


@pytest.mark.parametrize("raw", ["abc", "", "  ", "1.5", "0x10", "1e2", "--1", "1,000", "5 5", "null"])
def test_a_limit_that_is_not_an_integer_is_a_400(client, paged_room, raw):
    conv_id, headers, _ = paged_room

    response = client.get(f"/api/chat/conversations/{conv_id}/messages?limit={raw}",
                          headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "limit must be an integer"}


def test_a_limit_of_exactly_one_hundred_ships_a_full_page(client, big_room):
    conv_id, headers = big_room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=100",
                      headers=headers).get_json()

    assert len(body["messages"]) == 100
    assert body["hasMore"] is True


@pytest.mark.parametrize("raw", ["101", "1000", "999999999999999999999"])
def test_a_limit_past_the_cap_never_ships_more_than_one_hundred(client, big_room, raw):
    conv_id, headers = big_room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit={raw}",
                      headers=headers).get_json()

    assert len(body["messages"]) == 100


def test_the_first_limit_parameter_wins_when_it_is_repeated(client, paged_room):
    conv_id, headers, _ = paged_room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=2&limit=100",
                      headers=headers).get_json()

    assert len(body["messages"]) == 2


def test_a_full_page_with_nothing_behind_it_does_not_promise_more(client, paged_room):
    conv_id, headers, _ = paged_room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=5",
                      headers=headers).get_json()

    # The probe row is what settles this: 5 rows fetched with
    # limit 5 means SQLite was asked for 6 and found only 5
    assert len(body["messages"]) == 5
    assert body["hasMore"] is False








# ===========================================================
#  GET .../messages — own-message status and readBy
# ===========================================================


def test_an_own_message_with_only_the_senders_own_receipt_is_still_sent(client, db, room):
    conv_id, user, headers, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    _plant_receipt(db, msg_id, user["id"])

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    # The sender's own receipt never counts toward their status
    assert message["status"] == "sent"
    assert message["readBy"] == [user["id"]]


def test_one_foreign_receipt_reads_a_two_person_room(client, db, room):
    conv_id, user, headers, other, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    _plant_receipt(db, msg_id, other["id"])

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    # others_count is 1 here, so "delivered" has no middle ground
    assert message["status"] == "read"


@pytest.mark.contract
def test_an_own_message_in_a_group_walks_sent_then_delivered_then_read(client, db, trio):
    conv_id, user, headers, ona, jonas = trio
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    path = f"/api/chat/conversations/{conv_id}/messages"

    def _status():
        return client.get(path, headers=headers).get_json()["messages"][0]["status"]

    assert _status() == "sent"
    _plant_receipt(db, msg_id, ona["id"])
    assert _status() == "delivered"
    _plant_receipt(db, msg_id, jonas["id"])
    assert _status() == "read"


def test_receipts_from_people_who_are_not_members_still_satisfy_the_threshold(
        client, db, trio, make_user):
    conv_id, user, headers, ona, jonas = trio
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    # Two receipts, neither from a member: the status math counts
    # readers, it never re-checks who is still in the room
    for stranger in (make_user(), make_user()):
        _plant_receipt(db, msg_id, stranger["id"])

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert message["status"] == "read"
    assert len(message["readBy"]) == 2


def test_an_unsent_own_message_keeps_reporting_its_receipts(client, db, room):
    conv_id, user, headers, other, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="mano", image_url="/api/uploads/a.png",
                            deleted_at=_stamp(9))
    _plant_receipt(db, msg_id, other["id"])

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert message["deleted"] is True
    assert message["text"] == ""
    assert message["imageUrl"] is None
    assert message["status"] == "read"
    assert message["readBy"] == [other["id"]]


def test_somebody_elses_unsent_message_is_blanked_and_always_read(client, db, room):
    conv_id, user, headers, other, _ = room
    _plant_message(db, conv_id, other["id"], text="jų", deleted_at=_stamp(9))

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert message["isOwn"] is False
    assert message["status"] == "read"
    assert message["text"] == ""


def test_a_room_of_one_reads_its_own_message_without_any_receipt(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]])
    _plant_message(db, conv_id, user["id"], text="sau pačiam")

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    # others_count is 0 — there is nobody left to read it, so the
    # bubble must not sit on a single tick forever
    assert message["status"] == "read"
    assert message["readBy"] == []


def test_read_by_is_empty_for_a_message_nobody_receipted(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="mano")

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert message["readBy"] == []
    assert message["status"] == "sent"


def test_read_by_lists_every_receipt_holder_including_the_sender(client, db, trio):
    conv_id, user, headers, ona, jonas = trio
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    for uid in (user["id"], ona["id"], jonas["id"]):
        _plant_receipt(db, msg_id, uid)

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert set(message["readBy"]) == {user["id"], ona["id"], jonas["id"]}


def test_receipts_of_one_message_never_leak_onto_another(client, db, room):
    conv_id, user, headers, other, _ = room
    first = _plant_message(db, conv_id, user["id"], text="pirma", created_at=_stamp(1))
    _plant_message(db, conv_id, user["id"], text="antra", created_at=_stamp(2))
    _plant_receipt(db, first, other["id"])

    messages = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=headers).get_json()["messages"]

    assert messages[0]["readBy"] == [other["id"]]
    assert messages[1]["readBy"] == []








# ===========================================================
#  GET .../messages — reactions, quotes and the row shape
# ===========================================================


def test_reactions_are_grouped_per_emoji_with_by_self_for_the_caller(client, db, room):
    conv_id, user, headers, other, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    _plant_reaction(db, msg_id, user["id"], "\U0001F44D")
    _plant_reaction(db, msg_id, other["id"], "\U0001F44D")

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert len(message["reactions"]) == 1
    group = message["reactions"][0]
    assert group["emoji"] == "\U0001F44D"
    assert group["count"] == 2
    assert group["bySelf"] is True
    assert set(group["byUserIds"]) == {user["id"], other["id"]}


def test_by_self_is_false_on_a_reaction_only_somebody_else_holds(client, db, room):
    conv_id, user, headers, other, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    _plant_reaction(db, msg_id, other["id"], "❤️")

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert message["reactions"] == [
        {"emoji": "❤️", "count": 1, "bySelf": False, "byUserIds": [other["id"]]}
    ]


def test_two_emojis_on_one_message_are_two_groups(client, db, room):
    conv_id, user, headers, other, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    _plant_reaction(db, msg_id, user["id"], "\U0001F602")
    _plant_reaction(db, msg_id, other["id"], "\U0001F622")

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    by_emoji = {g["emoji"]: g for g in message["reactions"]}
    assert set(by_emoji) == {"\U0001F602", "\U0001F622"}
    assert by_emoji["\U0001F602"]["bySelf"] is True
    assert by_emoji["\U0001F622"]["bySelf"] is False


def test_a_message_nobody_reacted_to_ships_an_empty_reaction_list(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="mano")

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert message["reactions"] == []


def test_reactions_of_one_message_never_leak_onto_another(client, db, room):
    conv_id, user, headers, other, _ = room
    first = _plant_message(db, conv_id, user["id"], text="pirma", created_at=_stamp(1))
    _plant_message(db, conv_id, user["id"], text="antra", created_at=_stamp(2))
    _plant_reaction(db, first, other["id"], "\U0001F621")

    messages = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=headers).get_json()["messages"]

    assert len(messages[0]["reactions"]) == 1
    assert messages[1]["reactions"] == []


def test_a_quote_whose_sender_row_vanished_keeps_the_id_and_a_null_name(
        client, db, room, make_user):
    conv_id, user, headers, _, _ = room
    ghost = make_user(display_name="Dingęs")
    db.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
               (conv_id, ghost["id"]))
    quoted = _plant_message(db, conv_id, ghost["id"], text="senas", created_at=_stamp(1))
    _plant_message(db, conv_id, user["id"], text="atsakau", created_at=_stamp(2),
                   reply_to_id=quoted)
    db.execute("DELETE FROM users WHERE id = ?", (ghost["id"],))
    db.commit()

    messages = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=headers).get_json()["messages"]

    # The quoted row is still there (the LEFT JOIN found it), only
    # its author is not — senderId survives, senderName does not
    assert len(messages) == 1
    assert messages[0]["replyTo"] == {
        "id": quoted, "senderId": ghost["id"], "senderName": None,
        "text": "senas", "imageUrl": None, "deleted": False,
    }


def test_a_message_whose_sender_row_vanished_drops_out_of_the_page(
        client, db, room, make_user):
    conv_id, user, headers, _, _ = room
    ghost = make_user()
    _plant_message(db, conv_id, ghost["id"], text="dings", created_at=_stamp(1))
    _plant_message(db, conv_id, user["id"], text="lieka", created_at=_stamp(2))
    db.execute("DELETE FROM users WHERE id = ?", (ghost["id"],))
    db.commit()

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    # The sender JOIN is an INNER one — an orphaned message is
    # invisible rather than a row with a null author
    assert [m["text"] for m in body["messages"]] == ["lieka"]


def test_a_reply_chain_quotes_only_its_immediate_parent(client, db, room):
    conv_id, user, headers, _, _ = room
    first = _plant_message(db, conv_id, user["id"], text="pirma", created_at=_stamp(1))
    second = _plant_message(db, conv_id, user["id"], text="antra", created_at=_stamp(2),
                            reply_to_id=first)
    _plant_message(db, conv_id, user["id"], text="trečia", created_at=_stamp(3),
                   reply_to_id=second)

    messages = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=headers).get_json()["messages"]

    assert messages[0]["replyTo"] is None
    assert messages[1]["replyTo"]["id"] == first
    assert messages[2]["replyTo"]["id"] == second
    assert messages[2]["replyTo"]["text"] == "antra"


def test_a_row_with_an_empty_created_at_ships_a_blank_time(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="mano", created_at="")

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    # _format_time fails soft — one corrupt stamp must never 500
    # a whole history page
    assert message["time"] == ""
    assert message["createdAt"] == ""


def test_a_row_without_a_nonce_ships_a_null_client_msg_id(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="mano")

    message = client.get(f"/api/chat/conversations/{conv_id}/messages",
                         headers=headers).get_json()["messages"][0]

    assert message["clientMsgId"] is None


def test_the_own_flag_separates_the_callers_rows_from_the_others(client, db, room):
    conv_id, user, headers, other, _ = room
    _plant_message(db, conv_id, user["id"], text="mano", created_at=_stamp(1))
    _plant_message(db, conv_id, other["id"], text="jų", created_at=_stamp(2))

    messages = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=headers).get_json()["messages"]

    assert [m["isOwn"] for m in messages] == [True, False]
    assert [m["senderName"] for m in messages] == [user["username"].title(), "Ona Onaitė"]








# ===========================================================
#  GET .../messages — the envelope and the gates
# ===========================================================


@pytest.mark.contract
def test_the_envelope_ships_the_conversation_row_beside_the_page(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]], title="Kursiokai", avatar_emoji="📚")

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["conversation"] == {
        "id": conv_id, "type": "group", "title": "Kursiokai", "avatarEmoji": "📚",
    }
    assert set(body) == {"messages", "hasMore", "participants", "conversation"}


def test_a_direct_room_ships_its_null_title_untouched(client, room):
    conv_id, user, headers, _, _ = room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["conversation"]["type"] == "direct"
    assert body["conversation"]["title"] is None


def test_the_participants_include_the_caller_and_their_avatars(client, db, trio):
    conv_id, user, headers, ona, jonas = trio

    participants = client.get(f"/api/chat/conversations/{conv_id}/messages",
                              headers=headers).get_json()["participants"]

    assert {p["id"] for p in participants} == {user["id"], ona["id"], jonas["id"]}
    assert all(p["avatarUrl"] is None for p in participants)
    assert [p["displayName"] for p in participants] == sorted(p["displayName"] for p in participants)


def test_a_page_whose_conversation_row_vanished_ships_a_null_conversation(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]])
    _plant_message(db, conv_id, user["id"], text="našlaitis")
    db.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    db.commit()

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    # The membership row is what gates the read, so the page still
    # answers — with a null header block instead of a 500
    assert body["conversation"] is None
    assert [m["text"] for m in body["messages"]] == ["našlaitis"]


def test_a_member_who_left_can_no_longer_read_the_history(client, room):
    conv_id, user, headers, _, _ = room

    assert client.delete(f"/api/chat/conversations/{conv_id}", headers=headers).status_code == 200
    response = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}


def test_an_outsider_is_refused_before_any_message_is_read(client, db, room, make_user, auth_headers):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="paslaptis")
    outsider = make_user()

    response = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=auth_headers(outsider))

    assert response.status_code == 403
    assert "paslaptis" not in response.get_data(as_text=True)








# ===========================================================
#  POST .../messages — the body validator, arm by arm
# ===========================================================


@pytest.mark.parametrize("bad", [123, 1.5, True, None, [], {}])
def test_a_text_that_is_not_a_string_is_refused(client, room, no_push, bad):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": bad}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Text must be a string"}


def test_an_explicit_null_text_is_refused_even_beside_a_photo(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": None, "imageUrl": "/api/uploads/a.png"},
                           headers=headers)

    # "no text" is spelled by omitting the key — the type check
    # runs before the text-or-image one
    assert response.status_code == 400
    assert response.get_json() == {"error": "Text must be a string"}


def test_an_empty_json_object_is_refused_as_a_missing_body(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON body required"}


def test_a_body_without_a_json_content_type_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           data='{"text": "labas"}', headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON body required"}


def test_malformed_json_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           data="{oops", headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON body required"}


def test_the_five_thousand_character_cap_is_measured_after_stripping(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "   " + "a" * 5000 + "   "}, headers=headers)

    assert response.status_code == 201
    stored = db.execute("SELECT text FROM messages").fetchone()["text"]
    assert len(stored) == 5000


def test_five_thousand_and_one_characters_are_refused(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "a" * 5001}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Message text must not exceed 5000 characters"}
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "\r\n  \t", "\u00a0"])
def test_a_message_that_strips_to_nothing_needs_a_photo(client, room, no_push, blank):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": blank}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Message must have text or image"}


def test_a_zero_width_space_is_text_enough_to_send(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "\u200b"}, headers=headers)

    # str.strip() does not touch U+200B, so this is a one-character
    # message, not a blank one
    assert response.status_code == 201
    assert db.execute("SELECT text FROM messages").fetchone()["text"] == "\u200b"


def test_whitespace_only_text_beside_a_photo_is_stored_blank(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "   ", "imageUrl": "/api/uploads/a.png"},
                           headers=headers)

    assert response.status_code == 201
    assert response.get_json()["message"]["text"] == ""
    assert db.execute("SELECT text FROM messages").fetchone()["text"] == ""


def test_the_stored_text_is_verbatim_while_the_wire_body_is_entity_escaped(
        client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = _post_raw(client, f"/api/chat/conversations/{conv_id}/messages",
                         {"text": '<b>"labas" & co</b>'}, headers)

    assert response.status_code == 201
    assert db.execute("SELECT text FROM messages").fetchone()["text"] == '<b>"labas" & co</b>'
    assert response.get_json()["message"]["text"] == (
        "&lt;b&gt;&quot;labas&quot; &amp; co&lt;/b&gt;"
    )


def test_a_nul_byte_never_reaches_the_stored_text(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = _post_raw(client, f"/api/chat/conversations/{conv_id}/messages",
                         {"text": "la\u0000bas"}, headers)

    assert response.status_code == 201
    assert db.execute("SELECT text FROM messages").fetchone()["text"] == "labas"








# ===========================================================
#  POST .../messages — imageUrl, every value on the wire
# ===========================================================


@pytest.mark.parametrize("url", [
    "/api/uploads/a.png",
    "/api/uploads/",
    "/api/uploads/nested/dir/a.png",
    "http://localhost/api/uploads/a.png",
    "https://localhost/api/uploads/a.png",
    "http://localhost/api/uploads/a.png?v=1",
])
def test_an_own_origin_uploads_url_is_accepted(client, db, room, no_push, url):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"imageUrl": url}, headers=headers)

    assert response.status_code == 201
    assert db.execute("SELECT image_url FROM messages").fetchone()["image_url"] == url


@pytest.mark.parametrize("url", [
    "/api/uploads",
    "/api/uploadsevil/a.png",
    "api/uploads/a.png",
    " /api/uploads/a.png",
    "//localhost/api/uploads/a.png",
    "http://evil.lt/api/uploads/a.png",
    "http://localhost:9999/api/uploads/a.png",
    "http://user@localhost/api/uploads/a.png",
    "HTTP://LOCALHOST/api/uploads/a.png",
    "ftp://localhost/api/uploads/a.png",
    "javascript:/api/uploads/a.png",
    "data:image/png;base64,AAAA",
    "http://localhost/etc/passwd",
    "https://localhost/../api/uploads/a.png",
])
def test_any_other_image_url_is_refused(client, db, room, no_push, url):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"imageUrl": url}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "imageUrl must be an /api/uploads/ path"}
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.parametrize("bad", [7, 1.5, ["/api/uploads/a.png"], {"url": "/api/uploads/a.png"}, True])
def test_a_truthy_non_string_image_url_is_refused(client, room, no_push, bad):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "labas", "imageUrl": bad}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "imageUrl must be a string"}


@pytest.mark.parametrize("bad", [[], {}, 0, False])
def test_a_falsy_non_string_image_url_is_refused_too(client, room, no_push, bad):
    # the type check runs OUTSIDE the truthiness gate, so a
    # falsy non-string is refused like a truthy one instead of
    # reaching sqlite3 as a bind parameter it cannot take
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "labas", "imageUrl": bad}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "imageUrl must be a string"}


def test_an_empty_image_url_beside_text_is_stored_as_given(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "labas", "imageUrl": ""}, headers=headers)

    # Falsy, so the /api/uploads/ check never runs — the empty
    # string is stored and echoed rather than normalised to null
    assert response.status_code == 201
    assert response.get_json()["message"]["imageUrl"] == ""
    assert db.execute("SELECT image_url FROM messages").fetchone()["image_url"] == ""


def test_a_traversing_uploads_path_passes_the_prefix_check(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"imageUrl": "/api/uploads/../../etc/passwd"}, headers=headers)

    # The guard is about the ORIGIN, not the path: resolving the
    # traversal is the uploads route's job
    assert response.status_code == 201
    assert db.execute("SELECT image_url FROM messages").fetchone()["image_url"] == (
        "/api/uploads/../../etc/passwd"
    )








# ===========================================================
#  POST .../messages — the quoted message
# ===========================================================


@pytest.mark.parametrize("bad", [123, 1.5, True, [], {}])
def test_a_reply_id_that_is_not_a_string_is_refused(client, room, no_push, bad):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "atsakau", "replyToId": bad}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "replyToId must be a string"}


@pytest.mark.parametrize("blank", ["", " ", "\t\n", "\u00a0"])
def test_a_blank_reply_id_is_refused_instead_of_silently_unquoted(client, room, no_push, blank):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "atsakau", "replyToId": blank}, headers=headers)

    # "not a reply" is spelled by omitting the key — a blank id is
    # a client bug and says so
    assert response.status_code == 400
    assert response.get_json() == {"error": "replyToId must not be blank"}


@pytest.mark.parametrize("bad", [123, 1.5, True, [], {}])
def test_a_client_msg_id_that_is_not_a_string_is_refused(client, db, room, no_push, bad):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "labas", "client_msg_id": bad}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "client_msg_id must be a string"}
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_quoting_a_message_whose_sender_row_vanished_is_refused(
        client, db, room, make_user, no_push):
    conv_id, user, headers, _, _ = room
    ghost = make_user()
    quoted = _plant_message(db, conv_id, ghost["id"], text="senas")
    db.execute("DELETE FROM users WHERE id = ?", (ghost["id"],))
    db.commit()

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "atsakau", "replyToId": quoted}, headers=headers)

    # The quote lookup INNER JOINs the author, so an orphaned row
    # is "not found" rather than a quote with a null sender
    assert response.status_code == 400
    assert response.get_json() == {"error": "Quoted message not found in this conversation"}


def test_a_reply_id_is_matched_untrimmed(client, db, room, no_push):
    conv_id, user, headers, _, _ = room
    quoted = _plant_message(db, conv_id, user["id"], text="senas")

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "atsakau", "replyToId": f" {quoted} "}, headers=headers)

    # .strip() only decides "is it blank", never what is looked up
    assert response.status_code == 400
    assert response.get_json() == {"error": "Quoted message not found in this conversation"}


def test_replying_to_ones_own_message_is_allowed(client, room, no_push):
    conv_id, user, headers, _, _ = room
    mine = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "pirma"}, headers=headers).get_json()["message"]

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "antra", "replyToId": mine["id"]}, headers=headers)

    assert response.status_code == 201
    quote = response.get_json()["message"]["replyTo"]
    assert quote["id"] == mine["id"]
    assert quote["senderId"] == user["id"]
    assert quote["deleted"] is False


def test_a_reply_to_a_reply_quotes_only_the_parent(client, db, room, no_push):
    conv_id, user, headers, _, _ = room
    first = _plant_message(db, conv_id, user["id"], text="pirma", created_at=_stamp(1))
    second = _plant_message(db, conv_id, user["id"], text="antra", created_at=_stamp(2),
                            reply_to_id=first)

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "trečia", "replyToId": second}, headers=headers)

    assert response.status_code == 201
    assert response.get_json()["message"]["replyTo"]["text"] == "antra"


def test_quoting_a_message_of_a_room_the_caller_also_belongs_to_is_refused(
        client, db, room, no_push):
    conv_id, user, headers, _, _ = room
    elsewhere = _plant_conversation(db, [user["id"]], title="Kita")
    quoted = _plant_message(db, elsewhere, user["id"], text="kitur")

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "atsakau", "replyToId": quoted}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Quoted message not found in this conversation"}








# ===========================================================
#  POST .../messages — the client_msg_id replay
# ===========================================================


def test_a_replay_writes_neither_a_second_row_nor_a_second_receipt(client, db, room, no_push):
    conv_id, user, headers, _, _ = room
    path = f"/api/chat/conversations/{conv_id}/messages"
    first = client.post(path, json={"text": "labas", "client_msg_id": "n-1"}, headers=headers)

    second = client.post(path, json={"text": "labas", "client_msg_id": "n-1"}, headers=headers)

    assert (first.status_code, second.status_code) == (201, 200)
    assert first.get_json()["message"]["id"] == second.get_json()["message"]["id"]
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 1


def test_a_nonce_of_exactly_128_characters_replays(client, db, room, no_push):
    conv_id, _, headers, _, _ = room
    nonce = "n" * 128
    path = f"/api/chat/conversations/{conv_id}/messages"
    client.post(path, json={"text": "labas", "client_msg_id": nonce}, headers=headers)

    response = client.post(path, json={"text": "labas", "client_msg_id": nonce}, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["message"]["clientMsgId"] == nonce
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_a_nonce_of_129_characters_is_refused(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "labas", "client_msg_id": "n" * 129}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "client_msg_id too long"}
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_a_whitespace_nonce_is_a_real_nonce(client, db, room, no_push):
    conv_id, _, headers, _, _ = room
    path = f"/api/chat/conversations/{conv_id}/messages"
    client.post(path, json={"text": "labas", "client_msg_id": " "}, headers=headers)

    response = client.post(path, json={"text": "labas", "client_msg_id": " "}, headers=headers)

    # Only the EMPTY string is "no nonce" — a blank one is stored
    # verbatim and replays like any other
    assert response.status_code == 200
    assert db.execute("SELECT client_msg_id FROM messages").fetchone()["client_msg_id"] == " "


def test_a_null_nonce_is_no_nonce_at_all(client, db, room, no_push):
    conv_id, _, headers, _, _ = room
    path = f"/api/chat/conversations/{conv_id}/messages"

    first = client.post(path, json={"text": "labas", "client_msg_id": None}, headers=headers)
    second = client.post(path, json={"text": "labas", "client_msg_id": None}, headers=headers)

    assert (first.status_code, second.status_code) == (201, 201)
    assert first.get_json()["message"]["clientMsgId"] is None
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_two_sends_without_a_nonce_are_two_messages(client, db, room, no_push):
    conv_id, _, headers, _, _ = room
    path = f"/api/chat/conversations/{conv_id}/messages"

    client.post(path, json={"text": "labas"}, headers=headers)
    client.post(path, json={"text": "labas"}, headers=headers)

    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_a_replay_answers_the_committed_photo_not_the_retried_one(client, room, no_push):
    conv_id, _, headers, _, _ = room
    path = f"/api/chat/conversations/{conv_id}/messages"
    client.post(path, json={"imageUrl": "/api/uploads/pirma.png", "client_msg_id": "n-1"},
                headers=headers)

    response = client.post(path, json={"imageUrl": "/api/uploads/antra.png",
                                       "client_msg_id": "n-1"}, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["message"]["imageUrl"] == "/api/uploads/pirma.png"


def test_a_replay_ignores_a_different_quote_in_the_retry(client, db, room, no_push):
    conv_id, user, headers, _, _ = room
    first = _plant_message(db, conv_id, user["id"], text="pirma", created_at=_stamp(1))
    second = _plant_message(db, conv_id, user["id"], text="antra", created_at=_stamp(2))
    path = f"/api/chat/conversations/{conv_id}/messages"
    client.post(path, json={"text": "atsakau", "replyToId": first, "client_msg_id": "n-1"},
                headers=headers)

    response = client.post(path, json={"text": "atsakau", "replyToId": second,
                                       "client_msg_id": "n-1"}, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["message"]["replyTo"]["id"] == first


# -----------------------------------------------------------
# _blind_first_lookup
# -----------------------------------------------------------
#
# Makes the PRE-INSERT replay lookup miss exactly once, which
# is what a racing twin does to it: the two requests both read
# "no such nonce", both insert, and the v10 unique index picks
# the loser. The second call (the one inside the
# IntegrityError handler) runs the real thing again.
#
# Used by:
#   - the two unique-index race tests below
# -----------------------------------------------------------

def _blind_first_lookup(chat_routes, monkeypatch, blind_all=False):
    real = chat_routes._find_committed_send
    seen = {"calls": 0}

    def _lookup(*args, **kwargs):
        seen["calls"] += 1
        if blind_all or seen["calls"] == 1:
            return None
        return real(*args, **kwargs)

    monkeypatch.setattr(chat_routes, "_find_committed_send", _lookup)
    return seen


def test_a_twin_caught_by_the_unique_index_answers_the_committed_row(
        client, chat_routes, db, room, monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    path = f"/api/chat/conversations/{conv_id}/messages"
    client.post(path, json={"text": "pirma", "client_msg_id": "n-1"}, headers=headers)
    seen = _blind_first_lookup(chat_routes, monkeypatch)

    response = client.post(path, json={"text": "antra", "client_msg_id": "n-1"}, headers=headers)

    # The insert fired the index, the rollback undid it, and the
    # second lookup answered with what is really committed
    assert response.status_code == 200
    assert response.get_json()["message"]["text"] == "pirma"
    assert seen["calls"] == 2
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_an_index_violation_with_no_committed_twin_is_reraised(
        client, chat_routes, db, room, monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    path = f"/api/chat/conversations/{conv_id}/messages"
    client.post(path, json={"text": "pirma", "client_msg_id": "n-1"}, headers=headers)
    _blind_first_lookup(chat_routes, monkeypatch, blind_all=True)

    # Nothing to answer with means the error is real — swallowing
    # it would lose the message silently
    with pytest.raises(sqlite3.IntegrityError):
        client.post(path, json={"text": "antra", "client_msg_id": "n-1"}, headers=headers)

    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_a_replay_is_still_gated_by_membership(client, db, room, make_user, auth_headers, no_push):
    conv_id, user, headers, _, _ = room
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "labas", "client_msg_id": "n-1"}, headers=headers)
    outsider = make_user()

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "labas", "client_msg_id": "n-1"},
                           headers=auth_headers(outsider))

    assert response.status_code == 403








# ===========================================================
#  POST .../messages — the committed row and the fan-out
# ===========================================================


@pytest.mark.contract
def test_the_socket_payload_and_the_201_body_agree(client, chat_events, room, monkeypatch, no_push):
    conv_id, user, headers, _, _ = room
    emitted = []
    monkeypatch.setattr(chat_events, "emit_new_message",
                        lambda sio, cid, payload: emitted.append((cid, payload)))

    body = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "labas"}, headers=headers).get_json()["message"]

    assert len(emitted) == 1
    conv, payload = emitted[0]
    assert conv == conv_id
    # The 201 body is the socket payload plus the three fields
    # only the sender's own client may read
    assert body == {**payload, "isOwn": True, "status": "sent", "readBy": [user["id"]]}


def test_the_sent_row_is_readable_back_through_the_history(client, room, no_push):
    conv_id, user, headers, _, _ = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "labas", "client_msg_id": "n-1"},
                       headers=headers).get_json()["message"]

    page = client.get(f"/api/chat/conversations/{conv_id}/messages",
                      headers=headers).get_json()["messages"]

    assert len(page) == 1
    for key in ("id", "conversationId", "senderId", "senderName", "senderAvatar",
                "text", "imageUrl", "time", "createdAt", "clientMsgId", "isOwn",
                "status", "readBy", "reactions", "replyTo", "deleted"):
        assert page[0][key] == sent[key], key


def test_a_five_thousand_character_message_survives_the_round_trip(client, room, no_push):
    conv_id, _, headers, _, _ = room
    text = "ą" * 5000
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": text}, headers=headers)

    page = client.get(f"/api/chat/conversations/{conv_id}/messages",
                      headers=headers).get_json()["messages"]

    assert page[0]["text"] == text








# ===========================================================
#  POST .../messages — who gets a push and what it says
# ===========================================================


def test_every_other_member_of_a_group_is_a_push_recipient(client, trio, no_push):
    conv_id, user, headers, ona, jonas = trio

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "labas"}, headers=headers)

    assert len(no_push) == 1
    func, args, _ = no_push[0]
    recipients, title, preview, data = args
    assert set(recipients) == {ona["id"], jonas["id"]}
    assert user["id"] not in recipients
    assert data == {"type": "chat_message", "conversationId": conv_id}


def test_a_group_without_a_title_pushes_the_bare_sender_name(client, db, actor, make_user, no_push):
    user, headers = actor
    ona = make_user(display_name="Ona Onaitė")
    conv_id = _plant_conversation(db, [user["id"], ona["id"]], title=None)

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "labas"}, headers=headers)

    _, args, _ = no_push[0]
    assert args[1] == user["username"].title()


def test_a_titled_group_says_where_the_message_landed(client, db, actor, make_user, no_push):
    user, headers = actor
    ona = make_user(display_name="Ona Onaitė")
    conv_id = _plant_conversation(db, [user["id"], ona["id"]], title="Kursiokai")

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "labas"}, headers=headers)

    _, args, _ = no_push[0]
    assert args[1] == f"{user['username'].title()} · Kursiokai"


def test_a_direct_room_pushes_the_plain_sender_name(client, room, no_push):
    conv_id, user, headers, _, _ = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "labas"}, headers=headers)

    _, args, _ = no_push[0]
    assert args[1] == user["username"].title()


def test_a_preview_of_exactly_one_hundred_characters_is_untruncated(client, room, no_push):
    conv_id, _, headers, _, _ = room
    text = "a" * 100

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": text}, headers=headers)

    _, args, _ = no_push[0]
    assert args[2] == text


def test_a_preview_of_one_hundred_and_one_characters_loses_the_last_one(client, room, no_push):
    conv_id, _, headers, _, _ = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "b" * 101}, headers=headers)

    _, args, _ = no_push[0]
    assert args[2] == "b" * 100


def test_a_photo_with_a_caption_previews_the_caption(client, room, no_push):
    conv_id, _, headers, _, _ = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "žiūrėk", "imageUrl": "/api/uploads/a.png"}, headers=headers)

    _, args, _ = no_push[0]
    assert args[2] == "žiūrėk"
    # The photo marker rides only on a message with NO text
    assert "preview" not in args[3]


def test_a_photo_without_a_caption_previews_nuotrauka(client, room, no_push):
    conv_id, _, headers, _, _ = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"imageUrl": "/api/uploads/a.png"}, headers=headers)

    _, args, _ = no_push[0]
    assert args[2] == "Nuotrauka"
    assert args[3]["preview"] == "photo"


def test_a_room_of_one_schedules_no_push_at_all(client, db, actor, no_push):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]])

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "sau pačiam"}, headers=headers)

    assert response.status_code == 201
    assert no_push == []


def test_an_unreadable_socket_room_table_pushes_everybody(
        client, chat_routes, room, monkeypatch, no_push, quiet_socket):
    conv_id, _, headers, other, _ = room

    class _NotAMapping:
        def get(self, *args, **kwargs):
            raise RuntimeError("the room table is not readable")

    monkeypatch.setattr(chat_routes._get_socketio().server.manager, "rooms", _NotAMapping())

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "vis tiek"}, headers=headers)

    # Unreadable means "assume nobody is in the room": a push too
    # many beats a message nobody is told about
    assert response.status_code == 201
    assert no_push[0][1][0] == [other["id"]]


def test_a_broken_presence_table_never_fails_a_committed_send(
        client, db, chat_events, room, monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    # No .items(): STEP 5 raises on the very first line inside its
    # own try, which is exactly what it is there for
    monkeypatch.setattr(chat_events, "_connected_users", object())

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "labas"}, headers=headers)

    assert response.status_code == 201
    assert no_push == []
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_the_push_task_is_the_module_level_fan_out(client, chat_routes, room, no_push):
    conv_id, _, headers, _, _ = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "labas"}, headers=headers)

    func, _, _ = no_push[0]
    assert func is chat_routes._push_chat_message








# ===========================================================
#  _find_committed_send — the replay lookup, on its own
# ===========================================================


def test_the_replay_lookup_answers_none_for_an_unknown_nonce(chat_routes, db, room):
    conv_id, user, _, _, _ = room
    _plant_message(db, conv_id, user["id"], text="labas", client_msg_id="n-1")

    assert chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-2") is None


def test_the_replay_lookup_is_scoped_to_the_conversation(chat_routes, db, room):
    conv_id, user, _, _, _ = room
    elsewhere = _plant_conversation(db, [user["id"]], title="Kita")
    _plant_message(db, elsewhere, user["id"], text="kitur", client_msg_id="n-1")

    assert chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1") is None


def test_the_replay_lookup_is_scoped_to_the_sender(chat_routes, db, room):
    conv_id, user, _, other, _ = room
    _plant_message(db, conv_id, other["id"], text="jų", client_msg_id="n-1")

    assert chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1") is None


@pytest.mark.contract
def test_the_replay_lookup_shapes_the_whole_send_response(chat_routes, db, room):
    conv_id, user, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="labas", created_at=_stamp(0),
                            client_msg_id="n-1")

    result = chat_routes._find_committed_send(
        db, conv_id, user["id"], "Vardas Pavardė", "/api/uploads/av.png", "n-1")

    assert result == {
        "id": msg_id,
        "conversationId": conv_id,
        "senderId": user["id"],
        "senderName": "Vardas Pavardė",
        "senderAvatar": "/api/uploads/av.png",
        "text": "labas",
        "imageUrl": None,
        "time": "10:00",
        "createdAt": _stamp(0),
        "clientMsgId": "n-1",
        "reactions": [],
        "replyTo": None,
        "deleted": False,
        "isOwn": True,
        "status": "sent",
        "readBy": [user["id"]],
    }


def test_the_replay_lookup_echoes_the_caller_supplied_name_and_avatar(chat_routes, db, room):
    conv_id, user, _, _, _ = room
    _plant_message(db, conv_id, user["id"], text="labas", client_msg_id="n-1")

    result = chat_routes._find_committed_send(db, conv_id, user["id"], "Kitas Vardas",
                                              "/api/uploads/kitas.png", "n-1")

    # The name and portrait come off the SESSION user, never off a
    # second users lookup — a renamed sender sees the new name
    assert result["senderName"] == "Kitas Vardas"
    assert result["senderAvatar"] == "/api/uploads/kitas.png"


def test_the_replay_lookup_blanks_an_unsent_row(chat_routes, db, room):
    conv_id, user, _, _, _ = room
    _plant_message(db, conv_id, user["id"], text="dar yra", image_url="/api/uploads/a.png",
                   deleted_at=_stamp(9), client_msg_id="n-1")

    result = chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1")

    # The row may still hold its columns; the wire must not
    assert result["deleted"] is True
    assert result["text"] == ""
    assert result["imageUrl"] is None


def test_the_replay_lookup_keeps_a_live_photo(chat_routes, db, room):
    conv_id, user, _, _, _ = room
    _plant_message(db, conv_id, user["id"], image_url="/api/uploads/a.png", client_msg_id="n-1")

    result = chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1")

    assert result["imageUrl"] == "/api/uploads/a.png"
    assert result["text"] == ""


def test_the_replay_lookup_carries_a_live_quote(chat_routes, db, room):
    conv_id, user, _, other, _ = room
    quoted = _plant_message(db, conv_id, other["id"], text="senas", created_at=_stamp(1))
    _plant_message(db, conv_id, user["id"], text="atsakau", created_at=_stamp(2),
                   reply_to_id=quoted, client_msg_id="n-1")

    result = chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1")

    assert result["replyTo"] == {
        "id": quoted, "senderId": other["id"], "senderName": "Ona Onaitė",
        "text": "senas", "imageUrl": None, "deleted": False,
    }


def test_the_replay_lookup_blanks_a_quote_of_an_unsent_message(chat_routes, db, room):
    conv_id, user, _, other, _ = room
    quoted = _plant_message(db, conv_id, other["id"], text="senas", image_url="/api/uploads/a.png",
                            created_at=_stamp(1), deleted_at=_stamp(9))
    _plant_message(db, conv_id, user["id"], text="atsakau", created_at=_stamp(2),
                   reply_to_id=quoted, client_msg_id="n-1")

    quote = chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1")["replyTo"]

    assert quote["deleted"] is True
    assert quote["senderId"] == other["id"]
    assert quote["text"] == ""
    assert quote["imageUrl"] is None


def test_the_replay_lookup_shapes_a_dangling_quote_as_deleted(chat_routes, db, room):
    conv_id, user, _, _, _ = room
    _plant_message(db, conv_id, user["id"], text="atsakau", reply_to_id="msg-nera",
                   client_msg_id="n-1")

    quote = chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1")["replyTo"]

    # A ghost quote is shaped as unsent, never as a live quote
    # with a null sender
    assert quote == {"id": "msg-nera", "senderId": None, "senderName": None,
                     "text": "", "imageUrl": None, "deleted": True}


def test_the_replay_lookup_leaves_a_quote_whose_author_vanished_unnamed(
        chat_routes, db, room, make_user):
    conv_id, user, _, _, _ = room
    ghost = make_user()
    quoted = _plant_message(db, conv_id, ghost["id"], text="senas", created_at=_stamp(1))
    _plant_message(db, conv_id, user["id"], text="atsakau", created_at=_stamp(2),
                   reply_to_id=quoted, client_msg_id="n-1")
    db.execute("DELETE FROM users WHERE id = ?", (ghost["id"],))
    db.commit()

    quote = chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1")["replyTo"]

    assert quote["senderId"] == ghost["id"]
    assert quote["senderName"] is None
    assert quote["deleted"] is False


def test_the_replay_lookup_reports_a_fixed_status_whatever_the_stores_hold(
        chat_routes, db, room):
    conv_id, user, _, other, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="labas", client_msg_id="n-1")
    _plant_receipt(db, msg_id, other["id"])
    _plant_reaction(db, msg_id, other["id"], "\U0001F44D")

    result = chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1")

    # A replay answers the SEND shape, not the history shape: the
    # client resyncs through GET for the live state
    assert result["status"] == "sent"
    assert result["readBy"] == [user["id"]]
    assert result["reactions"] == []


def test_the_replay_lookup_answers_none_on_an_empty_conversation(chat_routes, db, room):
    conv_id, user, _, _, _ = room

    assert chat_routes._find_committed_send(db, conv_id, user["id"], "V", None, "n-1") is None








# ===========================================================
#  _push_chat_message — the fan-out that owes nobody an error
# ===========================================================


def test_the_batched_notifier_wins_when_the_package_offers_one(chat_routes, push_module,
                                                               monkeypatch):
    calls = []
    monkeypatch.setattr(push_module, "notify_channel_users",
                        lambda *args, **kwargs: calls.append((args, kwargs)), raising=False)

    def _must_not_run(*args, **kwargs):
        raise AssertionError("the fallback ran even though a batched notifier exists")

    monkeypatch.setattr(push_module, "send_push_batch", _must_not_run, raising=False)

    chat_routes._push_chat_message(["u1", "u2"], "Ona", "labas", {"type": "chat_message"})

    assert calls == [(("chat", ["u1", "u2"], "Ona", "labas"),
                      {"data": {"type": "chat_message"}})]


def test_the_batched_notifier_gets_the_data_dict_unchanged(chat_routes, push_module, monkeypatch):
    seen = {}
    monkeypatch.setattr(push_module, "notify_channel_users",
                        lambda *args, **kwargs: seen.update(kwargs), raising=False)
    payload = {"type": "chat_message", "conversationId": "c-1"}

    chat_routes._push_chat_message(["u1"], "Ona", "labas", payload)

    # Stamping the channel is the batched helper's own job
    assert seen["data"] == {"type": "chat_message", "conversationId": "c-1"}
    assert "channel" not in payload


def test_a_raising_batched_notifier_never_escapes(chat_routes, push_module, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("expo down")

    monkeypatch.setattr(push_module, "notify_channel_users", _boom, raising=False)

    chat_routes._push_chat_message(["u1"], "Ona", "labas", {})


def test_a_package_without_either_entry_point_is_swallowed(chat_routes, push_module):
    # Both imports fail: the first inside its own try, the second
    # straight into the outer catch-all
    chat_routes._push_chat_message(["u1"], "Ona", "labas", {})


def test_an_empty_recipient_list_never_raises_out_of_the_task(chat_routes, push_module,
                                                              monkeypatch):
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)

    # An empty IN () is a SQLite syntax error — the fan-out must
    # log it and return, never let it reach the caller
    chat_routes._push_chat_message([], "Ona", "labas", {})

    assert sent == []


def test_the_fallback_batches_every_device_of_every_recipient(chat_routes, push_module, db,
                                                              make_user, monkeypatch):
    ona = make_user()
    jonas = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[aaa]")
    _plant_token(db, ona["id"], "ExponentPushToken[bbb]")
    _plant_token(db, jonas["id"], "ExponentPushToken[ccc]")
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)

    chat_routes._push_chat_message([ona["id"], jonas["id"]], "Ona", "labas", {})

    assert len(sent) == 1
    assert set(sent[0][0]) == {"ExponentPushToken[aaa]", "ExponentPushToken[bbb]",
                               "ExponentPushToken[ccc]"}


def test_the_fallback_ignores_a_deactivated_token(chat_routes, push_module, db, make_user,
                                                  monkeypatch):
    ona = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[old]", active=0)
    _plant_token(db, ona["id"], "ExponentPushToken[new]")
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)

    chat_routes._push_chat_message([ona["id"]], "Ona", "labas", {})

    assert sent[0][0] == ["ExponentPushToken[new]"]


def test_the_fallback_keeps_an_explicitly_enabled_chat_channel(chat_routes, push_module, db,
                                                               make_user, monkeypatch):
    ona = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[aaa]")
    db.execute("INSERT INTO notification_channels (user_id, channel, enabled) VALUES (?, 'chat', 1)",
               (ona["id"],))
    db.commit()
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)

    chat_routes._push_chat_message([ona["id"]], "Ona", "labas", {})

    assert sent[0][0] == ["ExponentPushToken[aaa]"]


def test_an_opt_out_on_another_channel_does_not_silence_chat(chat_routes, push_module, db,
                                                             make_user, monkeypatch):
    ona = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[aaa]")
    db.execute("INSERT INTO notification_channels (user_id, channel, enabled) VALUES (?, 'news', 0)",
               (ona["id"],))
    db.commit()
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)

    chat_routes._push_chat_message([ona["id"]], "Ona", "labas", {})

    assert sent[0][0] == ["ExponentPushToken[aaa]"]


def test_a_recipient_without_any_token_is_simply_absent(chat_routes, push_module, db,
                                                        make_user, monkeypatch):
    ona = make_user()
    silent = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[aaa]")
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)

    chat_routes._push_chat_message([ona["id"], silent["id"]], "Ona", "labas", {})

    assert sent[0][0] == ["ExponentPushToken[aaa]"]


def test_the_fallback_stamps_the_chat_channel_onto_a_null_data(chat_routes, push_module, db,
                                                               make_user, monkeypatch):
    ona = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[aaa]")
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)

    chat_routes._push_chat_message([ona["id"]], "Ona", "labas", None)

    assert sent[0][3] == {"channel": "chat"}


def test_the_fallback_overwrites_a_caller_supplied_channel(chat_routes, push_module, db,
                                                           make_user, monkeypatch):
    ona = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[aaa]")
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)
    payload = {"channel": "news", "type": "chat_message"}

    chat_routes._push_chat_message([ona["id"]], "Ona", "labas", payload)

    assert sent[0][3] == {"channel": "chat", "type": "chat_message"}
    # The caller's dict is copied, never grown
    assert payload == {"channel": "news", "type": "chat_message"}


def test_the_fallback_passes_the_title_and_body_through(chat_routes, push_module, db,
                                                        make_user, monkeypatch):
    ona = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[aaa]")
    sent = []
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args: sent.append(args), raising=False)

    chat_routes._push_chat_message([ona["id"]], "Ona · Kursiokai", "Nuotrauka", {})

    assert sent[0][1] == "Ona · Kursiokai"
    assert sent[0][2] == "Nuotrauka"


def test_a_raising_batch_send_never_escapes(chat_routes, push_module, db, make_user, monkeypatch):
    ona = make_user()
    _plant_token(db, ona["id"], "ExponentPushToken[aaa]")

    def _boom(*args):
        raise RuntimeError("expo down")

    monkeypatch.setattr(push_module, "send_push_batch", _boom, raising=False)

    chat_routes._push_chat_message([ona["id"]], "Ona", "labas", {})
