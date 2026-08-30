# -----------------------------------------------------------
#  [*] Tests — chat messages (send, page, unsend)
#
#  What this module proves about app/chat/routes.py, the half
#  of the chat blueprint the room screen lives on:
#
#    - POST .../messages writes ONE row and answers the shape
#      hooks/chat/useChatComposer.ts renders optimistically:
#      isOwn/status/readBy on top of the socket payload, the
#      sender's own receipt already written, the conversation
#      bumped to the top of the list.
#    - The client_msg_id contract: a retried POST carrying a
#      nonce already committed answers 200 with the EXISTING
#      row — never a second message, never a second fan-out —
#      including when the pre-insert lookup misses and the v10
#      unique index is what catches the twin.
#    - GET .../messages pages backwards through the composite
#      (?before, ?before_id) cursor without ever skipping an
#      equal-stamp sibling, reports hasMore exactly, clamps
#      ?limit into 1..100 and refuses garbage.
#    - DELETE .../messages/<mid> is an UNSEND: the row stays so
#      quotes and cursors survive, text/image are blanked,
#      reactions dropped, the photo blob handed to the uploads
#      helper, and the socket event fires only on the call that
#      actually unsent it.
#    - Every route's gates: 401 without a token, 403 for a
#      non-member (an outsider cannot even tell a message
#      exists), 404 for an id outside this conversation, 403
#      for somebody else's message, 400 for every body the
#      validator rejects.
#    - The push fan-out picks the members WITHOUT a socket in
#      this very room, titles the banner per conversation type,
#      and can never fail a send that is already committed.
#
#  Arrangement is planted straight through the `db` fixture
#  (fabricated, ordered stamps) wherever the test is about
#  ordering or a state no route can create; everything else
#  goes through the real routes.
# -----------------------------------------------------------


import os
import sqlite3
import uuid

import pytest


# -----------------------------------------------------------
# chat_routes / chat_events
# -----------------------------------------------------------
#
# The modules under test, imported only once the `app` fixture
# has pinned DB_PATH and friends — the package must never be
# pulled in against a stray environment at collection time.
#
# Used by:
#   - the monkeypatching tests (push fan-out, socket emits,
#     the idempotency race)
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
# A naive-UTC isoformat stamp in the fixed shape the routes
# write and every cursor compares as TEXT. Deliberately far in
# the past so a planted row always sorts before anything a
# route stamps with the real clock, and zero-padded so string
# order equals chronological order.
#
# Used by:
#   - _plant_message, _plant_conversation and the paging tests
# -----------------------------------------------------------

def _stamp(index, micro=0):
    return f"2020-01-01T10:{index // 60:02d}:{index % 60:02d}.{micro:06d}"




# -----------------------------------------------------------
# _plant_conversation / _plant_message
# -----------------------------------------------------------
#
# Rows written straight through the test connection, which
# runs with PRAGMA foreign_keys OFF (sqlite3's default) —
# that is what lets a test plant the states a route refuses
# to create: a membership row for a vanished conversation, a
# reply pointing at a message that no longer exists, a
# corrupt created_at.
#
# Used by:
#   - most tests here; anything about ordering or a broken row
# -----------------------------------------------------------

def _plant_conversation(db, member_ids, conv_type="group", title="Kursiokai",
                        conv_id=None, last_read_at=None, created_at=None):
    conv_id = conv_id or f"conv-{uuid.uuid4().hex[:8]}"
    now = created_at or _stamp(0)
    db.execute(
        "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conv_id, conv_type, None if conv_type == "direct" else title, None,
         member_ids[0] if member_ids else None, now, now),
    )
    for uid in member_ids:
        db.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id, last_read_at)"
            " VALUES (?, ?, ?)",
            (conv_id, uid, last_read_at),
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
         deleted_at, created_at or _stamp(0)),
    )
    db.commit()
    return msg_id




# -----------------------------------------------------------
# room
# -----------------------------------------------------------
#
# The common arrangement: a two-person direct chat between the
# signed-in `actor` and a second member, both real users.
# Returns (conv_id, actor, actor_headers, other, other_headers).
#
# Used by:
#   - the send / page / unsend tests that need no third member
# -----------------------------------------------------------

@pytest.fixture
def room(db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user(display_name="Ona Onaitė")
    other_headers = auth_headers(other)
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="direct")
    return conv_id, user, headers, other, other_headers




# -----------------------------------------------------------
# no_push
# -----------------------------------------------------------
#
# Swallows the background push task so a send test never
# spawns a thread that outlives it. Returns the list of
# (args, kwargs) the route handed to start_background_task,
# so the fan-out tests read their assertions off it.
#
# Used by:
#   - every test that POSTs a message
# -----------------------------------------------------------

@pytest.fixture
def no_push(chat_routes, monkeypatch):
    calls = []

    def _capture(func, *args, **kwargs):
        calls.append((func, args, kwargs))

    monkeypatch.setattr(chat_routes._get_socketio(), "start_background_task", _capture)
    return calls




# -----------------------------------------------------------
# quiet_socket
# -----------------------------------------------------------
#
# Silences the live 'new_message' emit. The tests that fake
# socketio's room table need it: the real emit walks that very
# table and would choke on a hand-built one, while the code
# under test here is the push-recipient selection that reads
# it afterwards.
#
# Used by:
#   - the in-room / elsewhere / broken-manager push tests
# -----------------------------------------------------------

@pytest.fixture
def quiet_socket(chat_events, monkeypatch):
    monkeypatch.setattr(chat_events, "emit_new_message", lambda *args, **kwargs: None)








# ===========================================================
#  POST /api/chat/conversations/<id>/messages — sending
# ===========================================================


@pytest.mark.contract
def test_sending_text_answers_the_full_message_shape(client, room, no_push):
    conv_id, user, headers, other, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "Labas!"}, headers=headers)

    assert response.status_code == 201
    message = response.get_json()["message"]
    assert set(message) == {
        "id", "conversationId", "senderId", "senderName", "senderAvatar", "text",
        "imageUrl", "time", "createdAt", "clientMsgId", "reactions", "replyTo",
        "deleted", "isOwn", "status", "readBy",
    }
    assert message["conversationId"] == conv_id
    assert message["senderId"] == user["id"]
    assert message["text"] == "Labas!"
    assert message["imageUrl"] is None
    assert message["clientMsgId"] is None
    assert message["reactions"] == []
    assert message["replyTo"] is None
    assert message["deleted"] is False
    assert message["isOwn"] is True
    assert message["status"] == "sent"
    assert message["readBy"] == [user["id"]]


def test_sent_text_is_stripped_and_persisted_once(client, db, room, no_push):
    conv_id, user, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "  su tarpais  "}, headers=headers)

    assert response.status_code == 201
    rows = db.execute("SELECT text, sender_id, deleted_at FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["text"] == "su tarpais"
    assert rows[0]["sender_id"] == user["id"]
    assert rows[0]["deleted_at"] is None


def test_send_time_is_utc_hh_mm_of_created_at(client, room, no_push):
    conv_id, _, headers, _, _ = room

    message = client.post(f"/api/chat/conversations/{conv_id}/messages",
                          json={"text": "laikas"}, headers=headers).get_json()["message"]

    assert message["time"] == message["createdAt"][11:16]


def test_send_bumps_the_conversation_and_the_senders_read_state(client, db, room, no_push):
    conv_id, user, headers, _, _ = room

    message = client.post(f"/api/chat/conversations/{conv_id}/messages",
                          json={"text": "pirmas"}, headers=headers).get_json()["message"]

    conv = db.execute("SELECT updated_at FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    assert conv["updated_at"] == message["createdAt"]

    member = db.execute(
        "SELECT last_read_at FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user["id"]),
    ).fetchone()
    assert member["last_read_at"] == message["createdAt"]

    receipt = db.execute(
        "SELECT 1 FROM message_reads WHERE message_id = ? AND user_id = ?",
        (message["id"], user["id"]),
    ).fetchone()
    assert receipt is not None


def test_sender_never_counts_their_own_message_as_unread(client, room, no_push):
    conv_id, _, headers, _, other_headers = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "neskaitytas kitiems"}, headers=headers)

    assert client.get("/api/chat/unread-count", headers=headers).get_json()["unreadCount"] == 0
    assert client.get("/api/chat/unread-count", headers=other_headers).get_json()["unreadCount"] == 1


def test_photo_only_message_is_accepted(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"imageUrl": "/api/uploads/nuotrauka.jpg"}, headers=headers)

    assert response.status_code == 201
    message = response.get_json()["message"]
    assert message["text"] == ""
    assert message["imageUrl"] == "/api/uploads/nuotrauka.jpg"
    assert db.execute("SELECT image_url FROM messages WHERE id = ?",
                      (message["id"],)).fetchone()["image_url"] == "/api/uploads/nuotrauka.jpg"


def test_same_origin_absolute_image_url_is_accepted(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"imageUrl": "http://localhost/api/uploads/a.png"}, headers=headers)

    assert response.status_code == 201
    assert response.get_json()["message"]["imageUrl"] == "http://localhost/api/uploads/a.png"


@pytest.mark.parametrize("bad_url", [
    "https://evil.example/api/uploads/a.png",
    "//evil.example/api/uploads/a.png",
    "http://localhost/static/a.png",
    "/etc/passwd",
    "data:image/png;base64,AAAA",
    "http://localhost:9999/api/uploads/a.png",
])
def test_foreign_image_url_is_refused(client, db, room, no_push, bad_url):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"imageUrl": bad_url}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "imageUrl must be an /api/uploads/ path"
    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 0


def test_non_string_image_url_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"imageUrl": {"path": "/api/uploads/a.png"}}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "imageUrl must be a string"


def test_missing_body_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_non_object_body_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json=["labas"], headers=headers)

    assert response.status_code == 400
    # The app-wide body guard answers before the route does
    assert response.get_json()["error"] == "JSON body must be an object"


def test_non_string_text_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": 42}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Text must be a string"


def test_whitespace_only_message_without_image_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "   \n\t "}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Message must have text or image"


def test_text_of_exactly_5000_characters_is_accepted(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "a" * 5000}, headers=headers)

    assert response.status_code == 201
    assert len(response.get_json()["message"]["text"]) == 5000


def test_text_over_5000_characters_is_refused(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "a" * 5001}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Message text must not exceed 5000 characters"
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_outsider_cannot_send_into_a_room(client, db, room, make_user, auth_headers, no_push):
    conv_id, _, _, _, _ = room
    stranger = make_user()

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "įsibroviau"}, headers=auth_headers(stranger))

    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"
    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 0


def test_sending_into_an_unknown_conversation_is_403(client, actor, no_push):
    _, headers = actor

    response = client.post("/api/chat/conversations/nera-tokio/messages",
                           json={"text": "labas"}, headers=headers)

    assert response.status_code == 403


def test_sending_without_a_token_is_401(client, room, no_push):
    conv_id, _, _, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages", json={"text": "labas"})

    assert response.status_code == 401




# ===========================================================
#  replyToId — the quoted bubble
# ===========================================================


@pytest.mark.contract
def test_reply_carries_the_quoted_message(client, room, no_push):
    conv_id, _, headers, other, other_headers = room
    quoted = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "originalas"}, headers=other_headers).get_json()["message"]

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "atsakymas", "replyToId": quoted["id"]}, headers=headers)

    assert response.status_code == 201
    reply_to = response.get_json()["message"]["replyTo"]
    assert reply_to == {
        "id": quoted["id"],
        "senderId": other["id"],
        "senderName": "Ona Onaitė",
        "text": "originalas",
        "imageUrl": None,
        "deleted": False,
    }


def test_quoting_a_photo_keeps_its_image_url(client, room, no_push):
    conv_id, _, headers, _, other_headers = room
    quoted = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"imageUrl": "/api/uploads/foto.jpg"},
                         headers=other_headers).get_json()["message"]

    reply = client.post(f"/api/chat/conversations/{conv_id}/messages",
                        json={"text": "graži", "replyToId": quoted["id"]},
                        headers=headers).get_json()["message"]

    assert reply["replyTo"]["imageUrl"] == "/api/uploads/foto.jpg"
    assert reply["replyTo"]["text"] == ""


def test_quoting_an_unsent_message_keeps_the_sender_but_drops_the_content(client, room, no_push):
    conv_id, _, headers, other, other_headers = room
    quoted = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "atšauksiu"}, headers=other_headers).get_json()["message"]
    client.delete(f"/api/chat/conversations/{conv_id}/messages/{quoted['id']}", headers=other_headers)

    reply = client.post(f"/api/chat/conversations/{conv_id}/messages",
                        json={"text": "vėlu", "replyToId": quoted["id"]},
                        headers=headers).get_json()["message"]

    assert reply["replyTo"]["deleted"] is True
    assert reply["replyTo"]["text"] == ""
    assert reply["replyTo"]["imageUrl"] is None
    assert reply["replyTo"]["senderId"] == other["id"]


def test_quoting_a_message_from_another_conversation_is_refused(client, db, room, actor,
                                                               make_user, no_push):
    conv_id, user, headers, _, _ = room
    elsewhere = _plant_conversation(db, [user["id"], make_user()["id"]])
    foreign_msg = _plant_message(db, elsewhere, user["id"], text="kitur")

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "citata", "replyToId": foreign_msg}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Quoted message not found in this conversation"


def test_quoting_an_unknown_message_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "citata", "replyToId": "nera-tokio"}, headers=headers)

    assert response.status_code == 400


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_reply_id_is_refused_instead_of_silently_unquoted(client, room, no_push, blank):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "citata", "replyToId": blank}, headers=headers)

    assert response.status_code == 400
    # "" is falsy, "   " is not — both must reach the same answer
    assert response.get_json()["error"] == "replyToId must not be blank"


def test_non_string_reply_id_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "citata", "replyToId": 7}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "replyToId must be a string"


def test_omitting_reply_to_id_is_the_way_to_send_an_unquoted_message(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    message = client.post(f"/api/chat/conversations/{conv_id}/messages",
                          json={"text": "be citatos"}, headers=headers).get_json()["message"]

    assert message["replyTo"] is None
    assert db.execute("SELECT reply_to_id FROM messages WHERE id = ?",
                      (message["id"],)).fetchone()["reply_to_id"] is None




# ===========================================================
#  client_msg_id — the idempotency contract
# ===========================================================


def test_retried_send_returns_the_existing_row_and_writes_nothing(client, db, room, no_push):
    conv_id, user, headers, _, _ = room
    nonce = "nonce-abc-1"

    first = client.post(f"/api/chat/conversations/{conv_id}/messages",
                        json={"text": "vienintelis", "client_msg_id": nonce}, headers=headers)
    second = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "vienintelis", "client_msg_id": nonce}, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json()["message"]["id"] == first.get_json()["message"]["id"]
    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 1


def test_replay_answers_the_committed_text_not_the_retried_body(client, db, room, no_push):
    conv_id, _, headers, _, _ = room
    nonce = "nonce-abc-2"
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "originalus", "client_msg_id": nonce}, headers=headers)

    replay = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "PAKEISTAS", "client_msg_id": nonce}, headers=headers)

    assert replay.get_json()["message"]["text"] == "originalus"
    assert db.execute("SELECT text FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()["text"] == "originalus"


@pytest.mark.contract
def test_replay_answers_the_same_shape_as_the_original_send(client, room, no_push):
    conv_id, user, headers, _, _ = room
    nonce = "nonce-abc-3"
    first = client.post(f"/api/chat/conversations/{conv_id}/messages",
                        json={"text": "kartojuosi", "client_msg_id": nonce},
                        headers=headers).get_json()["message"]

    replay = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "kartojuosi", "client_msg_id": nonce},
                         headers=headers).get_json()["message"]

    assert set(replay) == set(first)
    assert replay["clientMsgId"] == nonce
    assert replay["senderName"] == first["senderName"]
    assert replay["senderAvatar"] == first["senderAvatar"]
    assert replay["createdAt"] == first["createdAt"]
    assert replay["time"] == first["time"]
    assert replay["status"] == "sent"
    assert replay["readBy"] == [user["id"]]
    assert replay["isOwn"] is True
    assert replay["deleted"] is False


def test_replay_of_an_unsent_message_comes_back_blanked(client, room, no_push):
    conv_id, _, headers, _, _ = room
    nonce = "nonce-abc-4"
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "atšauksiu", "imageUrl": "/api/uploads/x.jpg",
                             "client_msg_id": nonce},
                       headers=headers).get_json()["message"]
    client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}", headers=headers)

    replay = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "atšauksiu", "client_msg_id": nonce},
                         headers=headers).get_json()["message"]

    assert replay["deleted"] is True
    assert replay["text"] == ""
    assert replay["imageUrl"] is None


def test_replay_carries_the_reply_quote(client, room, no_push):
    conv_id, _, headers, other, other_headers = room
    quoted = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "originalas"}, headers=other_headers).get_json()["message"]
    nonce = "nonce-abc-5"
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "atsakymas", "replyToId": quoted["id"], "client_msg_id": nonce},
                headers=headers)

    replay = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "atsakymas", "replyToId": quoted["id"],
                               "client_msg_id": nonce},
                         headers=headers)

    assert replay.status_code == 200
    assert replay.get_json()["message"]["replyTo"]["id"] == quoted["id"]
    assert replay.get_json()["message"]["replyTo"]["senderId"] == other["id"]


def test_replay_of_a_dangling_quote_is_shaped_as_deleted(client, db, room, no_push):
    conv_id, _, headers, _, other_headers = room
    quoted = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "dings"}, headers=other_headers).get_json()["message"]
    nonce = "nonce-abc-6"
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "citata", "replyToId": quoted["id"], "client_msg_id": nonce},
                headers=headers)
    # The quoted row vanishes under an FK-off write, so the
    # LEFT JOIN in the replay lookup finds nothing
    db.execute("DELETE FROM messages WHERE id = ?", (quoted["id"],))
    db.commit()

    replay = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "citata", "replyToId": quoted["id"],
                               "client_msg_id": nonce},
                         headers=headers)

    # replyToId no longer names a live row, so the send-side
    # guard fires first: the replay is never reached
    assert replay.status_code == 400
    assert replay.get_json()["error"] == "Quoted message not found in this conversation"


def test_the_same_nonce_from_another_sender_is_its_own_message(client, db, room, no_push):
    conv_id, user, headers, other, other_headers = room
    nonce = "shared-nonce"

    mine = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "mano", "client_msg_id": nonce}, headers=headers)
    theirs = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "jų", "client_msg_id": nonce}, headers=other_headers)

    assert mine.status_code == 201
    assert theirs.status_code == 201
    assert mine.get_json()["message"]["id"] != theirs.get_json()["message"]["id"]
    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 2


def test_the_same_nonce_in_another_conversation_is_its_own_message(client, db, room,
                                                                   make_user, no_push):
    conv_id, user, headers, _, _ = room
    elsewhere = _plant_conversation(db, [user["id"], make_user()["id"]])
    nonce = "cross-room-nonce"

    here = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "čia", "client_msg_id": nonce}, headers=headers)
    there = client.post(f"/api/chat/conversations/{elsewhere}/messages",
                        json={"text": "ten", "client_msg_id": nonce}, headers=headers)

    assert here.status_code == 201
    assert there.status_code == 201
    assert here.get_json()["message"]["id"] != there.get_json()["message"]["id"]


def test_replay_does_not_fan_out_a_second_time(client, chat_events, room, monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    emitted = []
    monkeypatch.setattr(chat_events, "emit_new_message",
                        lambda sio, cid, payload: emitted.append(payload))
    nonce = "nonce-fanout"

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "vieną kartą", "client_msg_id": nonce}, headers=headers)
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "vieną kartą", "client_msg_id": nonce}, headers=headers)

    assert len(emitted) == 1
    assert len(no_push) == 1


def test_replay_does_not_move_the_conversation_back_up_the_list(client, db, room, no_push):
    conv_id, _, headers, _, _ = room
    nonce = "nonce-bump"
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "pirmas", "client_msg_id": nonce}, headers=headers)
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "antras"}, headers=headers)
    before = db.execute("SELECT updated_at FROM conversations WHERE id = ?",
                        (conv_id,)).fetchone()["updated_at"]

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "pirmas", "client_msg_id": nonce}, headers=headers)

    after = db.execute("SELECT updated_at FROM conversations WHERE id = ?",
                       (conv_id,)).fetchone()["updated_at"]
    assert after == before


def test_a_racing_twin_is_caught_by_the_unique_index(client, db, chat_routes, room,
                                                     monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    nonce = "race-nonce"
    real_lookup = chat_routes._find_committed_send
    misses = {"n": 0}

    # The pre-insert lookup misses exactly once — the window a
    # double-submit slips through; the INSERT then trips the v10
    # unique index and the catch answers with the committed twin
    def _blind_first(*args, **kwargs):
        misses["n"] += 1
        if misses["n"] == 1:
            return None
        return real_lookup(*args, **kwargs)

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "dvynys", "client_msg_id": nonce}, headers=headers)
    monkeypatch.setattr(chat_routes, "_find_committed_send", _blind_first)

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "dvynys", "client_msg_id": nonce}, headers=headers)

    assert response.status_code == 200
    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 1


def test_an_integrity_error_with_no_committed_twin_is_reraised(client, chat_routes, room,
                                                               monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    nonce = "blind-nonce"
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "pirmas", "client_msg_id": nonce}, headers=headers)
    # Never finds the twin — the route must not swallow the
    # constraint failure into a false 200
    monkeypatch.setattr(chat_routes, "_find_committed_send", lambda *a, **k: None)

    with pytest.raises(sqlite3.IntegrityError):
        client.post(f"/api/chat/conversations/{conv_id}/messages",
                    json={"text": "pirmas", "client_msg_id": nonce}, headers=headers)


def test_client_msg_id_of_exactly_128_characters_is_accepted(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "ilgas nonce", "client_msg_id": "n" * 128},
                           headers=headers)

    assert response.status_code == 201
    assert response.get_json()["message"]["clientMsgId"] == "n" * 128


def test_client_msg_id_over_128_characters_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "per ilgas", "client_msg_id": "n" * 129},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "client_msg_id too long"


def test_non_string_client_msg_id_is_refused(client, room, no_push):
    conv_id, _, headers, _, _ = room

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "labas", "client_msg_id": 12345}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "client_msg_id must be a string"


def test_empty_client_msg_id_is_stored_as_no_nonce(client, db, room, no_push):
    conv_id, _, headers, _, _ = room

    first = client.post(f"/api/chat/conversations/{conv_id}/messages",
                        json={"text": "vienas", "client_msg_id": ""}, headers=headers)
    second = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "du", "client_msg_id": ""}, headers=headers)

    # "" is not a nonce: no idempotency, and NULLs never collide
    # in the unique index
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.get_json()["message"]["clientMsgId"] is None
    assert db.execute("SELECT COUNT(*) FROM messages WHERE client_msg_id IS NULL").fetchone()[0] == 2




# ===========================================================
#  GET /api/chat/conversations/<id>/messages — the history page
# ===========================================================


@pytest.mark.contract
def test_history_answers_the_page_envelope(client, db, room, actor):
    conv_id, user, headers, other, _ = room
    _plant_message(db, conv_id, user["id"], text="pirmas", created_at=_stamp(1))

    response = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"messages", "hasMore", "participants", "conversation"}
    assert body["hasMore"] is False
    assert body["conversation"] == {"id": conv_id, "type": "direct", "title": None,
                                    "avatarEmoji": None}
    assert {p["id"] for p in body["participants"]} == {user["id"], other["id"]}
    assert set(body["participants"][0]) == {"id", "displayName", "avatarUrl"}
    assert set(body["messages"][0]) == {
        "id", "conversationId", "senderId", "senderName", "senderAvatar", "text",
        "imageUrl", "time", "createdAt", "clientMsgId", "isOwn", "status", "readBy",
        "reactions", "replyTo", "deleted",
    }


def test_history_is_chronological_oldest_first(client, db, room):
    conv_id, user, headers, _, _ = room
    for i in range(1, 4):
        _plant_message(db, conv_id, user["id"], text=f"žinutė {i}",
                       msg_id=f"m{i}", created_at=_stamp(i))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert [m["id"] for m in body["messages"]] == ["m1", "m2", "m3"]


def test_participants_are_sorted_by_display_name(client, db, actor, make_user, auth_headers):
    user, headers = actor
    zita = make_user(display_name="Zita")
    aldona = make_user(display_name="Aldona")
    conv_id = _plant_conversation(db, [user["id"], zita["id"], aldona["id"]])

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    names = [p["displayName"] for p in body["participants"]]
    assert names == sorted(names)
    assert names[0] == "Aldona"


def test_an_empty_room_answers_an_empty_page(client, room):
    conv_id, _, headers, _, _ = room

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["messages"] == []
    assert body["hasMore"] is False


def test_limit_ships_the_newest_page_and_reports_more(client, db, room):
    conv_id, user, headers, _, _ = room
    for i in range(1, 6):
        _plant_message(db, conv_id, user["id"], text=f"ž{i}", msg_id=f"m{i}", created_at=_stamp(i))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=2",
                      headers=headers).get_json()

    assert [m["id"] for m in body["messages"]] == ["m4", "m5"]
    assert body["hasMore"] is True


def test_an_exactly_full_last_page_does_not_promise_more(client, db, room):
    conv_id, user, headers, _, _ = room
    for i in range(1, 3):
        _plant_message(db, conv_id, user["id"], text=f"ž{i}", msg_id=f"m{i}", created_at=_stamp(i))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=2",
                      headers=headers).get_json()

    assert len(body["messages"]) == 2
    assert body["hasMore"] is False


def test_before_cursor_pages_strictly_backwards(client, db, room):
    conv_id, user, headers, _, _ = room
    for i in range(1, 6):
        _plant_message(db, conv_id, user["id"], text=f"ž{i}", msg_id=f"m{i}", created_at=_stamp(i))

    older = client.get(
        f"/api/chat/conversations/{conv_id}/messages?limit=2&before={_stamp(4)}",
        headers=headers,
    ).get_json()

    assert [m["id"] for m in older["messages"]] == ["m2", "m3"]
    assert older["hasMore"] is True


def test_paging_to_the_beginning_ends_with_has_more_false(client, db, room):
    conv_id, user, headers, _, _ = room
    for i in range(1, 4):
        _plant_message(db, conv_id, user["id"], text=f"ž{i}", msg_id=f"m{i}", created_at=_stamp(i))

    first_page = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=2",
                            headers=headers).get_json()
    cursor = first_page["messages"][0]
    second_page = client.get(
        f"/api/chat/conversations/{conv_id}/messages?limit=2"
        f"&before={cursor['createdAt']}&before_id={cursor['id']}",
        headers=headers,
    ).get_json()

    assert [m["id"] for m in second_page["messages"]] == ["m1"]
    assert second_page["hasMore"] is False


def test_equal_stamp_siblings_are_not_skipped_across_a_page_boundary(client, db, room):
    conv_id, user, headers, _, _ = room
    # Three messages sharing a stamp to the microsecond — the id
    # is the only thing that can order them
    for msg_id in ("m-a", "m-b", "m-c"):
        _plant_message(db, conv_id, user["id"], text=msg_id, msg_id=msg_id, created_at=_stamp(1))

    page_one = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=2",
                          headers=headers).get_json()
    cursor = page_one["messages"][0]
    page_two = client.get(
        f"/api/chat/conversations/{conv_id}/messages?limit=2"
        f"&before={cursor['createdAt']}&before_id={cursor['id']}",
        headers=headers,
    ).get_json()

    assert [m["id"] for m in page_one["messages"]] == ["m-b", "m-c"]
    assert [m["id"] for m in page_two["messages"]] == ["m-a"]
    seen = [m["id"] for m in page_two["messages"]] + [m["id"] for m in page_one["messages"]]
    assert sorted(seen) == ["m-a", "m-b", "m-c"]


def test_a_bare_before_cursor_keeps_the_old_strictly_older_stamp_behaviour(client, db, room):
    conv_id, user, headers, _, _ = room
    for msg_id in ("m-a", "m-b"):
        _plant_message(db, conv_id, user["id"], text=msg_id, msg_id=msg_id, created_at=_stamp(1))
    _plant_message(db, conv_id, user["id"], text="senesnė", msg_id="m-0", created_at=_stamp(0))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?before={_stamp(1)}",
                      headers=headers).get_json()

    assert [m["id"] for m in body["messages"]] == ["m-0"]


def test_before_id_without_before_is_ignored(client, db, room):
    conv_id, user, headers, _, _ = room
    for i in range(1, 3):
        _plant_message(db, conv_id, user["id"], text=f"ž{i}", msg_id=f"m{i}", created_at=_stamp(i))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?before_id=m2",
                      headers=headers).get_json()

    assert [m["id"] for m in body["messages"]] == ["m1", "m2"]


def test_a_cursor_before_the_beginning_answers_an_empty_page(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="vienintelė", created_at=_stamp(5))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?before={_stamp(1)}",
                      headers=headers).get_json()

    assert body["messages"] == []
    assert body["hasMore"] is False


@pytest.mark.parametrize("limit", ["0", "-5"])
def test_a_non_positive_limit_is_clamped_to_one_row(client, db, room, limit):
    conv_id, user, headers, _, _ = room
    for i in range(1, 4):
        _plant_message(db, conv_id, user["id"], text=f"ž{i}", msg_id=f"m{i}", created_at=_stamp(i))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit={limit}",
                      headers=headers).get_json()

    # A negative reaching SQLite as LIMIT -n would mean "no limit"
    assert [m["id"] for m in body["messages"]] == ["m3"]
    assert body["hasMore"] is True


def test_limit_is_capped_at_one_hundred(client, db, room):
    conv_id, user, headers, _, _ = room
    rows = [(f"m{i:03d}", conv_id, user["id"], f"ž{i}", _stamp(0, micro=i)) for i in range(101)]
    db.executemany(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at)"
        " VALUES (?, ?, ?, ?, ?)", rows)
    db.commit()

    body = client.get(f"/api/chat/conversations/{conv_id}/messages?limit=1000",
                      headers=headers).get_json()

    assert len(body["messages"]) == 100
    assert body["hasMore"] is True


@pytest.mark.parametrize("limit", ["abc", "10.5", ""])
def test_a_non_integer_limit_is_a_400(client, room, limit):
    conv_id, _, headers, _, _ = room

    response = client.get(f"/api/chat/conversations/{conv_id}/messages?limit={limit}",
                          headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be an integer"


def test_an_unsent_message_keeps_its_slot_but_ships_no_content(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="dingo", msg_id="m1",
                   image_url="/api/uploads/x.jpg", created_at=_stamp(1),
                   deleted_at=_stamp(2))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert len(body["messages"]) == 1
    assert body["messages"][0]["deleted"] is True
    assert body["messages"][0]["text"] == ""
    assert body["messages"][0]["imageUrl"] is None


def test_an_unparseable_created_at_answers_a_blank_time_instead_of_500(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="sugadinta", created_at="ne data")

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["messages"][0]["time"] == ""
    assert body["messages"][0]["createdAt"] == "ne data"


def test_history_carries_the_reply_quote(client, db, room):
    conv_id, user, headers, other, _ = room
    quoted = _plant_message(db, conv_id, other["id"], text="originalas", created_at=_stamp(1))
    _plant_message(db, conv_id, user["id"], text="atsakymas", created_at=_stamp(2),
                   reply_to_id=quoted)

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["messages"][0]["replyTo"] is None
    assert body["messages"][1]["replyTo"] == {
        "id": quoted,
        "senderId": other["id"],
        "senderName": "Ona Onaitė",
        "text": "originalas",
        "imageUrl": None,
        "deleted": False,
    }


def test_a_dangling_quote_is_shaped_as_deleted_not_as_a_live_quote(client, db, room):
    conv_id, user, headers, _, _ = room
    _plant_message(db, conv_id, user["id"], text="citata", created_at=_stamp(2),
                   reply_to_id="dingusi-zinute")

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["messages"][0]["replyTo"] == {
        "id": "dingusi-zinute",
        "senderId": None,
        "senderName": None,
        "text": "",
        "imageUrl": None,
        "deleted": True,
    }


def test_a_quote_of_an_unsent_message_loses_its_content_in_history(client, db, room):
    conv_id, user, headers, other, _ = room
    quoted = _plant_message(db, conv_id, other["id"], text="buvo", created_at=_stamp(1),
                            image_url="/api/uploads/x.jpg", deleted_at=_stamp(3))
    _plant_message(db, conv_id, user["id"], text="atsakymas", created_at=_stamp(2),
                   reply_to_id=quoted)

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    reply_to = body["messages"][1]["replyTo"]
    assert reply_to["deleted"] is True
    assert reply_to["text"] == ""
    assert reply_to["imageUrl"] is None
    assert reply_to["senderId"] == other["id"]


def test_history_carries_the_client_msg_id_of_own_rows(client, db, room, no_push):
    conv_id, _, headers, _, _ = room
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "su nonce", "client_msg_id": "nonce-history"}, headers=headers)

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["messages"][0]["clientMsgId"] == "nonce-history"


def test_reactions_ride_along_with_by_self(client, db, room):
    conv_id, user, headers, other, other_headers = room
    msg_id = _plant_message(db, conv_id, other["id"], text="reaguok", created_at=_stamp(1))
    client.post(f"/api/chat/conversations/{conv_id}/messages/{msg_id}/react",
                json={"emoji": "\U0001F44D"}, headers=headers)
    client.post(f"/api/chat/conversations/{conv_id}/messages/{msg_id}/react",
                json={"emoji": "\U0001F44D"}, headers=other_headers)

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    groups = body["messages"][0]["reactions"]
    assert len(groups) == 1
    assert groups[0]["emoji"] == "\U0001F44D"
    assert groups[0]["count"] == 2
    assert groups[0]["bySelf"] is True
    assert set(groups[0]["byUserIds"]) == {user["id"], other["id"]}


def test_read_by_lists_the_receipt_holders(client, db, room, no_push):
    conv_id, user, headers, other, other_headers = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "perskaityk"}, headers=headers).get_json()["message"]
    client.put(f"/api/chat/conversations/{conv_id}/read", headers=other_headers)

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert set(body["messages"][0]["readBy"]) == {user["id"], other["id"]}
    assert body["messages"][0]["id"] == sent["id"]


def test_own_message_status_walks_sent_delivered_read(client, db, actor, make_user,
                                                      auth_headers, no_push):
    user, headers = actor
    b = make_user(display_name="Bronė")
    c = make_user(display_name="Cezaris")
    b_headers, c_headers = auth_headers(b), auth_headers(c)
    conv_id = _plant_conversation(db, [user["id"], b["id"], c["id"]])
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "trise"}, headers=headers)

    def _status():
        body = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=headers).get_json()
        return body["messages"][0]["status"]

    assert _status() == "sent"
    client.put(f"/api/chat/conversations/{conv_id}/read", headers=b_headers)
    assert _status() == "delivered"
    client.put(f"/api/chat/conversations/{conv_id}/read", headers=c_headers)
    assert _status() == "read"


def test_other_peoples_messages_are_always_read(client, db, room):
    conv_id, user, headers, other, _ = room
    _plant_message(db, conv_id, other["id"], text="jų žinutė", created_at=_stamp(1))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["messages"][0]["isOwn"] is False
    assert body["messages"][0]["status"] == "read"
    assert body["messages"][0]["readBy"] == []


def test_a_message_in_a_room_of_one_is_trivially_read(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]])
    _plant_message(db, conv_id, user["id"], text="sau", created_at=_stamp(1))

    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()

    assert body["messages"][0]["status"] == "read"


def test_the_conversation_block_is_null_when_the_row_vanished(client, db, actor):
    user, headers = actor
    # A membership row surviving its conversation — only an
    # FK-off write can make it, and the header must not 500
    db.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
               ("conv-vaiduoklis", user["id"]))
    db.commit()

    response = client.get("/api/chat/conversations/conv-vaiduoklis/messages", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["conversation"] is None


def test_outsider_cannot_read_a_rooms_history(client, db, room, make_user, auth_headers):
    conv_id, user, _, _, _ = room
    _plant_message(db, conv_id, user["id"], text="privatu", created_at=_stamp(1))
    stranger = make_user()

    response = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=auth_headers(stranger))

    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"


def test_reading_an_unknown_conversation_is_403(client, actor):
    _, headers = actor

    response = client.get("/api/chat/conversations/nera-tokio/messages", headers=headers)

    assert response.status_code == 403


def test_reading_history_without_a_token_is_401(client, room):
    conv_id, _, _, _, _ = room

    assert client.get(f"/api/chat/conversations/{conv_id}/messages").status_code == 401




# ===========================================================
#  DELETE /api/chat/conversations/<id>/messages/<mid> — unsend
# ===========================================================


def test_sender_can_unsend_their_own_message(client, db, room, no_push):
    conv_id, user, headers, _, _ = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "klaida", "imageUrl": "/api/uploads/x.jpg"},
                       headers=headers).get_json()["message"]

    response = client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}",
                             headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    row = db.execute("SELECT text, image_url, deleted_at FROM messages WHERE id = ?",
                     (sent["id"],)).fetchone()
    assert row["text"] == ""
    assert row["image_url"] is None
    assert row["deleted_at"] is not None


def test_the_unsent_row_survives_so_cursors_and_quotes_hold(client, db, room, no_push):
    conv_id, user, headers, _, _ = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "atšaukiama"}, headers=headers).get_json()["message"]

    client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}", headers=headers)

    assert db.execute("SELECT COUNT(*) FROM messages WHERE id = ?",
                      (sent["id"],)).fetchone()[0] == 1
    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()
    assert [m["id"] for m in body["messages"]] == [sent["id"]]
    assert body["messages"][0]["deleted"] is True


def test_unsending_drops_the_reactions(client, db, room, no_push):
    conv_id, user, headers, _, other_headers = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "reakcijos"}, headers=headers).get_json()["message"]
    client.post(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}/react",
                json={"emoji": "❤️"}, headers=other_headers)

    client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}", headers=headers)

    assert db.execute("SELECT COUNT(*) FROM message_reactions WHERE message_id = ?",
                      (sent["id"],)).fetchone()[0] == 0
    body = client.get(f"/api/chat/conversations/{conv_id}/messages", headers=headers).get_json()
    assert body["messages"][0]["reactions"] == []


def test_an_unsent_message_stops_counting_as_unread(client, db, room, no_push):
    conv_id, _, headers, _, other_headers = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "atšauksiu"}, headers=headers).get_json()["message"]
    assert client.get("/api/chat/unread-count", headers=other_headers).get_json()["unreadCount"] == 1

    client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}", headers=headers)

    assert client.get("/api/chat/unread-count", headers=other_headers).get_json()["unreadCount"] == 0


def test_unsending_twice_stays_a_silent_200(client, chat_events, room, monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "du kartus"}, headers=headers).get_json()["message"]
    broadcasts = []
    monkeypatch.setattr(chat_events, "emit_message_deleted",
                        lambda sio, cid, mid: broadcasts.append(mid))

    first = client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}",
                          headers=headers)
    second = client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}",
                           headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert broadcasts == [sent["id"]]


def test_the_unsend_is_broadcast_to_the_room(client, chat_events, room, monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "transliuok"}, headers=headers).get_json()["message"]
    seen = []
    monkeypatch.setattr(chat_events, "emit_message_deleted",
                        lambda sio, cid, mid: seen.append((cid, mid)))

    client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}", headers=headers)

    assert seen == [(conv_id, sent["id"])]


def test_only_the_sender_can_unsend(client, db, room, no_push):
    conv_id, _, headers, _, other_headers = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "mano žinutė"}, headers=headers).get_json()["message"]

    response = client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}",
                             headers=other_headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only the sender can delete a message"
    assert db.execute("SELECT deleted_at FROM messages WHERE id = ?",
                      (sent["id"],)).fetchone()["deleted_at"] is None


def test_unsending_an_unknown_message_is_404(client, room):
    conv_id, _, headers, _, _ = room

    response = client.delete(f"/api/chat/conversations/{conv_id}/messages/nera-tokios",
                             headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Message not found"


def test_unsending_a_message_of_another_conversation_is_404(client, db, room, make_user, no_push):
    conv_id, user, headers, _, _ = room
    elsewhere = _plant_conversation(db, [user["id"], make_user()["id"]])
    foreign_msg = _plant_message(db, elsewhere, user["id"], text="kitur", created_at=_stamp(1))

    response = client.delete(f"/api/chat/conversations/{conv_id}/messages/{foreign_msg}",
                             headers=headers)

    assert response.status_code == 404
    assert db.execute("SELECT deleted_at FROM messages WHERE id = ?",
                      (foreign_msg,)).fetchone()["deleted_at"] is None


def test_an_outsider_learns_nothing_about_a_rooms_messages(client, db, room,
                                                           make_user, auth_headers):
    conv_id, user, _, _, _ = room
    real_msg = _plant_message(db, conv_id, user["id"], text="yra", created_at=_stamp(1))
    stranger_headers = auth_headers(make_user())

    existing = client.delete(f"/api/chat/conversations/{conv_id}/messages/{real_msg}",
                             headers=stranger_headers)
    missing = client.delete(f"/api/chat/conversations/{conv_id}/messages/nera-tokios",
                            headers=stranger_headers)

    # The same answer either way — 403 before any message row is read
    assert existing.status_code == 403
    assert missing.status_code == 403
    assert existing.get_json() == missing.get_json()


def test_unsending_without_a_token_is_401(client, room):
    conv_id, _, _, _, _ = room

    assert client.delete(f"/api/chat/conversations/{conv_id}/messages/x").status_code == 401


def test_unsending_a_photo_removes_the_stored_file(client, app, db, room, no_push):
    conv_id, user, headers, _, _ = room
    # The uploads package only owns names of its own shape:
    # 32 hex characters plus an image extension
    name = f"{uuid.uuid4().hex}.jpg"
    stored = os.path.join(app.config["UPLOAD_DIR"], name)
    with open(stored, "wb") as handle:
        handle.write(b"\xff\xd8\xff\xdb")
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"imageUrl": f"/api/uploads/{name}"},
                       headers=headers).get_json()["message"]

    client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}", headers=headers)

    assert not os.path.exists(stored)


def test_a_missing_uploads_helper_leaves_the_unsend_intact(client, room, monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"imageUrl": "/api/uploads/nera.jpg"},
                       headers=headers).get_json()["message"]
    # The documented fallback: until the uploads package ships
    # the helper the from-import is an ImportError, not a 500
    from app.uploads import routes as uploads_routes
    monkeypatch.delattr(uploads_routes, "delete_upload")

    response = client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}",
                             headers=headers)

    assert response.status_code == 200


def test_a_failing_upload_cleanup_does_not_fail_the_unsend(client, db, room, monkeypatch, no_push):
    conv_id, _, headers, _, _ = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"imageUrl": "/api/uploads/sprogs.jpg"},
                       headers=headers).get_json()["message"]

    def _boom(path):
        raise OSError("disk on fire")

    from app.uploads import routes as uploads_routes
    monkeypatch.setattr(uploads_routes, "delete_upload", _boom)

    response = client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}",
                             headers=headers)

    assert response.status_code == 200
    assert db.execute("SELECT deleted_at FROM messages WHERE id = ?",
                      (sent["id"],)).fetchone()["deleted_at"] is not None


def test_a_foreign_image_url_is_never_handed_to_the_uploads_helper(client, db, room,
                                                                   monkeypatch, no_push):
    conv_id, user, headers, _, _ = room
    # Only a planted row can carry a non-uploads url — the send
    # route refuses one
    msg_id = _plant_message(db, conv_id, user["id"], text="", created_at=_stamp(1),
                            image_url="https://knf.vu.lt/logo.png")
    handed = []
    from app.uploads import routes as uploads_routes
    monkeypatch.setattr(uploads_routes, "delete_upload", lambda path: handed.append(path))

    response = client.delete(f"/api/chat/conversations/{conv_id}/messages/{msg_id}",
                             headers=headers)

    assert response.status_code == 200
    assert handed == []




# ===========================================================
#  Push fan-out — who gets a banner, and what it says
# ===========================================================


def test_push_goes_to_the_members_without_a_socket_in_the_room(client, room, no_push):
    conv_id, user, headers, other, _ = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "labas vakaras"}, headers=headers)

    assert len(no_push) == 1
    _, args, _ = no_push[0]
    recipients, title, body, data = args
    assert recipients == [other["id"]]
    assert title == user["username"].title()
    assert body == "labas vakaras"
    assert data == {"type": "chat_message", "conversationId": conv_id}


def test_a_group_push_says_which_room_it_came_from(client, db, actor, make_user, no_push):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]],
                                  conv_type="group", title="Kursiokai")

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "sveiki"}, headers=headers)

    _, args, _ = no_push[0]
    assert args[1] == f"{user['username'].title()} · Kursiokai"


def test_a_photo_only_push_says_nuotrauka_and_marks_the_preview(client, room, no_push):
    conv_id, _, headers, _, _ = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"imageUrl": "/api/uploads/a.jpg"}, headers=headers)

    _, args, _ = no_push[0]
    assert args[2] == "Nuotrauka"
    assert args[3]["preview"] == "photo"


def test_the_push_preview_is_capped_at_one_hundred_characters(client, room, no_push):
    conv_id, _, headers, _, _ = room

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "a" * 300}, headers=headers)

    _, args, _ = no_push[0]
    assert args[2] == "a" * 100
    assert "preview" not in args[3]


def test_no_push_for_a_member_whose_socket_is_in_this_room(client, chat_routes, chat_events,
                                                           room, monkeypatch, no_push,
                                                           quiet_socket):
    conv_id, _, headers, other_user, _ = room
    sio = chat_routes._get_socketio()
    monkeypatch.setitem(chat_events._connected_users, "sid-room", other_user["id"])
    monkeypatch.setattr(sio.server.manager, "rooms",
                        {"/": {f"conv:{conv_id}": {"sid-room": None}}})

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "jis jau čia"}, headers=headers)

    assert no_push == []


def test_a_member_online_elsewhere_still_gets_the_push(client, chat_routes, chat_events,
                                                       room, monkeypatch, no_push,
                                                       quiet_socket):
    conv_id, _, headers, other_user, _ = room
    sio = chat_routes._get_socketio()
    # Connected, but its sid is in another room's set
    monkeypatch.setitem(chat_events._connected_users, "sid-elsewhere", other_user["id"])
    monkeypatch.setattr(sio.server.manager, "rooms",
                        {"/": {"conv:kitas": {"sid-elsewhere": None}}})

    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "kitame lange"}, headers=headers)

    assert len(no_push) == 1
    assert no_push[0][1][0] == [other_user["id"]]


def test_a_broken_room_manager_still_pushes(client, chat_routes, room, monkeypatch, no_push,
                                            quiet_socket):
    conv_id, _, headers, other_user, _ = room

    class _Exploding:
        def get(self, *args, **kwargs):
            raise RuntimeError("room manager is confused")

    monkeypatch.setattr(chat_routes._get_socketio().server.manager, "rooms", _Exploding())

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "vis tiek"}, headers=headers)

    assert response.status_code == 201
    assert no_push[0][1][0] == [other_user["id"]]


def test_a_failing_push_never_fails_a_committed_send(client, chat_routes, db, room, monkeypatch):
    conv_id, _, headers, _, _ = room

    def _boom(*args, **kwargs):
        raise RuntimeError("expo unreachable")

    monkeypatch.setattr(chat_routes._get_socketio(), "start_background_task", _boom)

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "vis tiek išsiųsta"}, headers=headers)

    assert response.status_code == 201
    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 1


def test_a_room_of_one_pushes_nothing(client, db, actor, no_push):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]])

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "sau pačiam"}, headers=headers)

    assert response.status_code == 201
    assert no_push == []


def test_the_chat_push_task_prefers_the_batched_notifier(chat_routes, monkeypatch):
    calls = []
    from app.notifications import push as push_module
    monkeypatch.setattr(push_module, "notify_channel_users",
                        lambda *args, **kwargs: calls.append((args, kwargs)))

    chat_routes._push_chat_message(["u1", "u2"], "Ona", "labas", {"type": "chat_message"})

    assert calls == [(("chat", ["u1", "u2"], "Ona", "labas"),
                      {"data": {"type": "chat_message"}})]


def test_the_push_fallback_skips_the_chat_opt_outs(chat_routes, db, make_user, monkeypatch):
    wants = make_user()
    opted_out = make_user()
    for user, token in ((wants, "ExponentPushToken[aaa]"), (opted_out, "ExponentPushToken[bbb]")):
        db.execute("INSERT INTO push_tokens (id, user_id, token, platform) VALUES (?, ?, ?, ?)",
                   (str(uuid.uuid4()), user["id"], token, "ios"))
    db.execute("INSERT INTO notification_channels (user_id, channel, enabled) VALUES (?, ?, 0)",
               (opted_out["id"], "chat"))
    db.commit()

    sent = []
    from app.notifications import push as push_module
    # No batched helper yet — the standalone fallback must do
    # the same shape itself
    monkeypatch.delattr(push_module, "notify_channel_users")
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda tokens, title, body, data: sent.append((tokens, title, body, data)))

    payload = {"type": "chat_message"}
    chat_routes._push_chat_message([wants["id"], opted_out["id"]], "Ona", "labas", payload)

    assert len(sent) == 1
    assert sent[0][0] == ["ExponentPushToken[aaa]"]
    assert sent[0][3] == {"type": "chat_message", "channel": "chat"}
    # The caller's dict must not grow a channel key
    assert payload == {"type": "chat_message"}


def test_the_push_fallback_sends_nothing_without_tokens(chat_routes, make_user, monkeypatch):
    sent = []
    from app.notifications import push as push_module
    monkeypatch.delattr(push_module, "notify_channel_users")
    monkeypatch.setattr(push_module, "send_push_batch",
                        lambda *args, **kwargs: sent.append(args))

    chat_routes._push_chat_message([make_user()["id"]], "Ona", "labas", None)

    assert sent == []


def test_a_raising_notifier_never_escapes_the_push_task(chat_routes, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("expo down")

    from app.notifications import push as push_module
    monkeypatch.setattr(push_module, "notify_channel_users", _boom)

    # No exception: push never owes anybody an error
    chat_routes._push_chat_message(["u1"], "Ona", "labas", {})




# ===========================================================
#  PUT .../read — the receipt store behind status and readBy
# ===========================================================


def test_marking_read_receipts_foreign_messages_only(client, db, room, no_push):
    conv_id, user, headers, other, other_headers = room
    mine = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "mano"}, headers=headers).get_json()["message"]
    theirs = client.post(f"/api/chat/conversations/{conv_id}/messages",
                         json={"text": "jų"}, headers=other_headers).get_json()["message"]

    response = client.put(f"/api/chat/conversations/{conv_id}/read", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "readCount": 1}
    readers = {r["user_id"] for r in db.execute(
        "SELECT user_id FROM message_reads WHERE message_id = ?", (theirs["id"],)).fetchall()}
    # Their own receipt from send time, plus the one this call wrote
    assert readers == {user["id"], other["id"]}
    # The sender's own receipt was written at send time and is
    # not re-counted here
    assert db.execute("SELECT COUNT(*) FROM message_reads WHERE message_id = ?",
                      (mine["id"],)).fetchone()[0] == 1


def test_marking_read_again_reports_nothing_new(client, room, chat_events, monkeypatch, no_push):
    conv_id, _, headers, _, other_headers = room
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "skaityk"}, headers=other_headers)
    receipts = []
    monkeypatch.setattr(chat_events, "emit_read_receipt",
                        lambda sio, cid, reader, ids: receipts.append(ids))

    first = client.put(f"/api/chat/conversations/{conv_id}/read", headers=headers)
    second = client.put(f"/api/chat/conversations/{conv_id}/read", headers=headers)

    assert first.get_json()["readCount"] == 1
    assert second.get_json()["readCount"] == 0
    # Nothing new — the watermark bounds the second scan and no
    # 'messages_read' goes out
    assert len(receipts) == 1


def test_an_outsider_cannot_mark_a_room_read(client, db, room, make_user, auth_headers, no_push):
    conv_id, _, headers, _, other_headers = room
    client.post(f"/api/chat/conversations/{conv_id}/messages",
                json={"text": "privatu"}, headers=other_headers)

    response = client.put(f"/api/chat/conversations/{conv_id}/read",
                          headers=auth_headers(make_user()))

    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"
    assert db.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 1


def test_marking_read_too_often_is_a_429(client, room):
    conv_id, _, headers, _, _ = room

    codes = [client.put(f"/api/chat/conversations/{conv_id}/read", headers=headers).status_code
             for _ in range(12)]

    # One budget across both transports — 10 per 10 s per user
    assert codes[:10] == [200] * 10
    assert codes[-1] == 429


# ===========================================================
#  Rate limits — the decorators that must stay on
# ===========================================================


# -----------------------------------------------------------
# _fill_bucket
# -----------------------------------------------------------
#
# Spends a scope's whole budget for one user by planting the
# attempt stamps the shared limiter keeps in memory — 150
# real sends would prove the same thing a hundred times
# slower. Monotonic stamps, so nothing here depends on the
# wall clock.
#
# Used by:
#   - the two rate-limit regression tests below
# -----------------------------------------------------------

def _fill_bucket(scope, user_id, count):
    import time

    from app.auth import routes as auth_routes

    with auth_routes._rate_limit_lock:
        auth_routes._rate_limit_store[f"{scope}:{user_id}"] = [time.monotonic()] * count


def test_sending_past_the_quota_is_a_429(client, room, no_push):
    conv_id, user, headers, _, _ = room
    _fill_bucket("chat_send", user["id"], 150)

    response = client.post(f"/api/chat/conversations/{conv_id}/messages",
                           json={"text": "per daug"}, headers=headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert response.headers["Retry-After"]


def test_unsending_past_the_quota_is_a_429(client, room, no_push):
    conv_id, user, headers, _, _ = room
    sent = client.post(f"/api/chat/conversations/{conv_id}/messages",
                       json={"text": "žinutė"}, headers=headers).get_json()["message"]
    _fill_bucket("chat_delete", user["id"], 100)

    response = client.delete(f"/api/chat/conversations/{conv_id}/messages/{sent['id']}",
                             headers=headers)

    assert response.status_code == 429
