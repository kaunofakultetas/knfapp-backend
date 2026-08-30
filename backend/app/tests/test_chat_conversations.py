# -----------------------------------------------------------
#  [*] Tests — chat conversation lifecycle
#
#  What this module proves about app/chat/routes.py, the half
#  of the Messages tab that has nothing to do with typing a
#  message:
#
#    - POST /chat/conversations validates EVERY field before
#      it writes: the participant list (1..50 non-empty
#      strings), the type enum, the bounded title and
#      avatarEmoji, the group's mandatory title, the member
#      set that must not reduce to the caller alone, and the
#      "exactly two people" rule for a direct chat.
#    - Direct dedup answers 200 with the EXISTING room — but
#      only for a room that really holds two people. A
#      planted multi-member 'direct' row (the shape migration
#      v49 demoted) must never swallow two people's DM, and
#      neither must a group, nor a DM with somebody else.
#      The under-the-write-lock re-check is exercised too:
#      the loser of a double-submit answers 200 instead of
#      inserting a second DM.
#    - A direct chat stores NULL title/avatarEmoji whatever
#      the body says (an attacker-chosen title would
#      impersonate the counterpart in every list row) and is
#      named after the other member on read.
#    - GET /chat/conversations ships the exact wire shape the
#      mobile ApiConversation type declares, pinned first
#      then newest lastUpdatedMs, with an unread count that
#      excludes the caller's own and every soft-deleted
#      message — the badge must agree with what is still
#      readable.
#    - DELETE /chat/conversations/<id> is a LEAVE: the
#      caller's membership, receipts and reactions go, the
#      remaining members keep the history, and the last one
#      out purges the room.
#    - Every conversation-scoped route refuses a non-member,
#      and an ex-member becomes a non-member immediately.
# -----------------------------------------------------------

import time
import uuid
from datetime import datetime, timezone

import pytest
import time_machine


CONVERSATIONS = "/api/chat/conversations"




# -----------------------------------------------------------
# _plant_conversation
# -----------------------------------------------------------
#
# A conversation and its membership rows written straight to
# the database — the only way to arrange states the API
# deliberately refuses to create: a 'direct' row with three
# members (the v49 shape), a room whose updated_at is
# unparseable, a member whose last_read_at is still NULL.
#
# `last_read` maps a member id to its watermark (absent =
# NULL), `pinned_for` lists the members whose row is pinned.
#
# Used by:
#   - the dedup, list-ordering, unread and leave tests below
# -----------------------------------------------------------

def _plant_conversation(db, members, conv_type="direct", title=None, avatar_emoji=None,
                        created_at="2026-01-01T09:00:00", updated_at=None,
                        last_read=None, pinned_for=(), conv_id=None):
    conv_id = conv_id or f"conv-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conv_id, conv_type, title, avatar_emoji, members[0] if members else None,
         created_at, created_at if updated_at is None else updated_at),
    )
    for uid in members:
        db.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id, pinned, last_read_at)"
            " VALUES (?, ?, ?, ?)",
            (conv_id, uid, 1 if uid in pinned_for else 0, (last_read or {}).get(uid)),
        )
    db.commit()
    return conv_id




# -----------------------------------------------------------
# _plant_message
# -----------------------------------------------------------
#
# One message row with a stamp the caller chooses — the
# unread count and the list ordering compare those stamps as
# plain strings, so the tests need to pin them rather than
# race the clock. deleted_at set makes it an unsent message.
#
# Used by:
#   - the unread-count, lastMessage and purge tests below
# -----------------------------------------------------------

def _plant_message(db, conv_id, sender_id, text="Labas", created_at="2026-01-01T10:00:00",
                   deleted_at=None, image_url=None, msg_id=None):
    msg_id = msg_id or f"msg-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, deleted_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, image_url, deleted_at, created_at),
    )
    db.commit()
    return msg_id




# -----------------------------------------------------------
# request shorthands
# -----------------------------------------------------------
#
# _create_direct / _create_group post the two bodies the
# people picker sends; _conversations returns the caller's
# list rows and _row_for picks one out of it.
#
# Used by:
#   - nearly every test below
# -----------------------------------------------------------

def _create_direct(client, headers, other_id):
    return client.post(CONVERSATIONS, json={"participantIds": [other_id], "type": "direct"},
                       headers=headers)


def _create_group(client, headers, member_ids, title="Grupė", avatar_emoji=None):
    body = {"participantIds": list(member_ids), "type": "group", "title": title}
    if avatar_emoji is not None:
        body["avatarEmoji"] = avatar_emoji
    return client.post(CONVERSATIONS, json=body, headers=headers)


def _conversations(client, headers):
    response = client.get(CONVERSATIONS, headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["conversations"]


def _row_for(rows, conv_id):
    matches = [row for row in rows if row["id"] == conv_id]
    assert matches, f"conversation {conv_id} missing from the list"
    return matches[0]


def _members_of(db, conv_id):
    return sorted(
        r[0] for r in db.execute(
            "SELECT user_id FROM conversation_participants WHERE conversation_id = ?", (conv_id,)
        )
    )




# ===========================================================
# POST /chat/conversations — body validation
# ===========================================================


def test_creating_a_conversation_requires_authentication(client, make_user):
    other = make_user()
    response = client.post(CONVERSATIONS, json={"participantIds": [other["id"]]})
    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_creating_a_conversation_without_a_body_is_refused(client, actor):
    _, headers = actor
    response = client.post(CONVERSATIONS, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_top_level_array_body_is_refused_instead_of_crashing(client, db, actor):
    # Stopped by the app-wide validate_json_input hook, one
    # layer above the route — either way no data.get() ever runs
    # on a list and nothing is written
    _, headers = actor
    response = client.post(CONVERSATIONS, json=["not", "an", "object"], headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_an_empty_json_object_is_refused_by_the_route(client, actor):
    _, headers = actor
    response = client.post(CONVERSATIONS, json={}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_participant_ids_must_be_present(client, actor):
    _, headers = actor
    response = client.post(CONVERSATIONS, json={"type": "direct"}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_participant_ids_must_be_a_list(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = client.post(CONVERSATIONS, json={"participantIds": other["id"]}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_an_empty_participant_list_is_refused(client, actor):
    _, headers = actor
    response = client.post(CONVERSATIONS, json={"participantIds": []}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_more_than_fifty_participant_ids_are_refused(client, actor):
    _, headers = actor
    response = client.post(
        CONVERSATIONS,
        json={"participantIds": [f"ghost-{n}" for n in range(51)], "type": "group", "title": "T"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain at most 50 ids"


def test_exactly_fifty_participant_ids_pass_the_length_gate(client, actor):
    # 50 is the boundary: this body dies on the users lookup,
    # NOT on the length rule — which is how we know the cap is
    # 51 and not 50
    _, headers = actor
    response = client.post(
        CONVERSATIONS,
        json={"participantIds": [f"ghost-{n}" for n in range(50)], "type": "group", "title": "T"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "One or more participant IDs are invalid"


def test_non_string_participant_ids_are_refused(client, actor):
    _, headers = actor
    response = client.post(CONVERSATIONS, json={"participantIds": [12345]}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain non-empty strings"


def test_a_blank_participant_id_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = client.post(CONVERSATIONS, json={"participantIds": [other["id"], ""]},
                           headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain non-empty strings"


def test_an_unknown_conversation_type_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = client.post(CONVERSATIONS,
                           json={"participantIds": [other["id"]], "type": "channel"},
                           headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "type must be one of: direct, group"


def test_a_non_string_title_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = client.post(CONVERSATIONS,
                           json={"participantIds": [other["id"]], "type": "group", "title": 42},
                           headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_a_title_over_a_hundred_characters_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = _create_group(client, headers, [other["id"]], title="Ą" * 101)
    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be at most 100 characters"


def test_a_title_of_exactly_a_hundred_characters_is_accepted(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    response = _create_group(client, headers, [other["id"]], title="Ą" * 100)
    assert response.status_code == 201
    stored = db.execute("SELECT title FROM conversations WHERE id = ?",
                        (response.get_json()["conversationId"],)).fetchone()["title"]
    assert stored == "Ą" * 100


def test_a_non_string_avatar_emoji_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = client.post(
        CONVERSATIONS,
        json={"participantIds": [other["id"]], "type": "group", "title": "T", "avatarEmoji": 7},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "avatarEmoji must be a string"


def test_an_avatar_emoji_over_sixteen_characters_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = _create_group(client, headers, [other["id"]], avatar_emoji="x" * 17)
    assert response.status_code == 400
    assert response.get_json()["error"] == "avatarEmoji must be at most 16 characters"


def test_an_avatar_emoji_of_exactly_sixteen_characters_is_accepted(client, db, actor, make_user):
    _, headers = actor
    other = make_user()
    response = _create_group(client, headers, [other["id"]], avatar_emoji="x" * 16)
    assert response.status_code == 201
    stored = db.execute("SELECT avatar_emoji FROM conversations WHERE id = ?",
                        (response.get_json()["conversationId"],)).fetchone()["avatar_emoji"]
    assert stored == "x" * 16


def test_a_group_without_a_title_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = client.post(CONVERSATIONS,
                           json={"participantIds": [other["id"]], "type": "group"},
                           headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Group conversations require a title"


def test_a_group_with_a_whitespace_only_title_is_refused(client, actor, make_user):
    # A null-ish group title crashes the mobile Messages tab
    # search, so blank is refused as hard as missing
    _, headers = actor
    other = make_user()
    response = _create_group(client, headers, [other["id"]], title="   ")
    assert response.status_code == 400
    assert response.get_json()["error"] == "Group conversations require a title"


def test_a_conversation_with_only_the_caller_is_refused(client, actor):
    user, headers = actor
    response = client.post(CONVERSATIONS, json={"participantIds": [user["id"]]}, headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "A conversation needs at least one other participant"


def test_a_direct_chat_with_three_people_is_refused(client, actor, make_user):
    _, headers = actor
    one, two = make_user(), make_user()
    response = client.post(CONVERSATIONS,
                           json={"participantIds": [one["id"], two["id"]], "type": "direct"},
                           headers=headers)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Direct conversations must have exactly 2 participants"


def test_an_unknown_participant_id_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()
    response = client.post(
        CONVERSATIONS,
        json={"participantIds": [other["id"], "no-such-user"], "type": "group", "title": "T"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "One or more participant IDs are invalid"


def test_a_deactivated_user_cannot_be_dragged_into_a_new_room(client, actor, make_user):
    _, headers = actor
    disabled = make_user(active=0)
    response = _create_direct(client, headers, disabled["id"])
    assert response.status_code == 400
    assert response.get_json()["error"] == "One or more participant IDs are invalid"


def test_nothing_is_written_when_a_participant_id_is_invalid(client, db, actor, make_user):
    _, headers = actor
    other = make_user()
    before = db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    client.post(CONVERSATIONS,
                json={"participantIds": [other["id"], "ghost"], "type": "group", "title": "T"},
                headers=headers)
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == before




# ===========================================================
# POST /chat/conversations — the member set
# ===========================================================


def test_the_creator_is_added_even_when_absent_from_the_body(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    response = _create_direct(client, headers, other["id"])
    assert response.status_code == 201
    assert _members_of(db, response.get_json()["conversationId"]) == sorted(
        [user["id"], other["id"]])


def test_duplicate_participant_ids_collapse_into_one_membership(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    response = client.post(
        CONVERSATIONS,
        json={"participantIds": [other["id"], other["id"], other["id"]], "type": "direct"},
        headers=headers,
    )
    assert response.status_code == 201
    assert _members_of(db, response.get_json()["conversationId"]) == sorted(
        [user["id"], other["id"]])


def test_the_caller_listing_themselves_does_not_make_a_direct_chat_a_trio(client, db, actor,
                                                                         make_user):
    user, headers = actor
    other = make_user()
    response = client.post(
        CONVERSATIONS,
        json={"participantIds": [user["id"], other["id"]], "type": "direct"},
        headers=headers,
    )
    assert response.status_code == 201
    assert _members_of(db, response.get_json()["conversationId"]) == sorted(
        [user["id"], other["id"]])


def test_a_group_stores_its_title_emoji_creator_and_every_member(client, db, actor, make_user):
    user, headers = actor
    one, two = make_user(), make_user()
    response = _create_group(client, headers, [one["id"], two["id"]],
                             title="Kursiokai", avatar_emoji="🎓")
    assert response.status_code == 201

    conv_id = response.get_json()["conversationId"]
    row = db.execute("SELECT type, title, avatar_emoji, created_by FROM conversations WHERE id = ?",
                     (conv_id,)).fetchone()
    assert row["type"] == "group"
    assert row["title"] == "Kursiokai"
    assert row["avatar_emoji"] == "🎓"
    assert row["created_by"] == user["id"]
    assert _members_of(db, conv_id) == sorted([user["id"], one["id"], two["id"]])


def test_a_direct_chat_never_stores_a_body_supplied_title_or_emoji(client, db, actor, make_user):
    # A body-chosen title would name the counterpart in every
    # list row — impersonation. Direct rooms store NULL for both
    _, headers = actor
    other = make_user()
    response = client.post(
        CONVERSATIONS,
        json={"participantIds": [other["id"]], "type": "direct",
              "title": "Rektorius", "avatarEmoji": "👑"},
        headers=headers,
    )
    assert response.status_code == 201

    row = db.execute("SELECT title, avatar_emoji FROM conversations WHERE id = ?",
                     (response.get_json()["conversationId"],)).fetchone()
    assert row["title"] is None
    assert row["avatar_emoji"] is None


def test_the_type_defaults_to_direct_when_the_body_omits_it(client, db, actor, make_user):
    _, headers = actor
    other = make_user()
    response = client.post(CONVERSATIONS, json={"participantIds": [other["id"]]}, headers=headers)
    assert response.status_code == 201
    assert db.execute("SELECT type FROM conversations WHERE id = ?",
                      (response.get_json()["conversationId"],)).fetchone()["type"] == "direct"


def test_a_new_room_opens_with_zero_unread_for_everyone(client, db, actor, make_user,
                                                        auth_headers):
    user, headers = actor
    other = make_user()
    other_headers = auth_headers(other)

    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    watermarks = [r[0] for r in db.execute(
        "SELECT last_read_at FROM conversation_participants WHERE conversation_id = ?", (conv_id,))]
    assert all(mark is not None for mark in watermarks)
    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 0
    assert _row_for(_conversations(client, other_headers), conv_id)["unreadCount"] == 0




# ===========================================================
# POST /chat/conversations — direct dedup
# ===========================================================


def test_a_second_direct_create_reuses_the_existing_room(client, db, actor, make_user):
    _, headers = actor
    other = make_user()

    first = _create_direct(client, headers, other["id"])
    second = _create_direct(client, headers, other["id"])

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json()["conversationId"] == first.get_json()["conversationId"]
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


def test_the_other_side_creating_the_same_direct_chat_reuses_it_too(client, db, actor, make_user,
                                                                    auth_headers):
    user, headers = actor
    other = make_user()
    other_headers = auth_headers(other)

    mine = _create_direct(client, headers, other["id"])
    theirs = _create_direct(client, other_headers, user["id"])

    assert theirs.status_code == 200
    assert theirs.get_json()["conversationId"] == mine.get_json()["conversationId"]
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


def test_a_planted_multi_member_direct_room_is_never_reused_as_a_dm(client, db, actor, make_user):
    # The v49 shape: a 'direct' row with three members. Reusing
    # it would drop two people's private chat into a third
    # person's lap
    user, headers = actor
    other, third = make_user(), make_user()
    planted = _plant_conversation(db, [user["id"], other["id"], third["id"]], conv_type="direct")

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    conv_id = response.get_json()["conversationId"]
    assert conv_id != planted
    assert _members_of(db, conv_id) == sorted([user["id"], other["id"]])


def test_a_group_with_the_same_two_people_is_not_reused_as_a_dm(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    group_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group",
                                   title="Projektas")

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    assert response.get_json()["conversationId"] != group_id


def test_a_direct_chat_with_somebody_else_is_not_reused(client, db, actor, make_user):
    user, headers = actor
    other, stranger = make_user(), make_user()
    with_stranger = _plant_conversation(db, [user["id"], stranger["id"]])

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    assert response.get_json()["conversationId"] != with_stranger
    assert _members_of(db, response.get_json()["conversationId"]) == sorted(
        [user["id"], other["id"]])


def test_a_direct_room_the_caller_already_left_is_not_reused(client, db, actor, make_user):
    # Only the caller's OWN memberships drive the lookup, and a
    # one-member leftover fails the count = 2 arm anyway
    user, headers = actor
    other = make_user()
    abandoned = _plant_conversation(db, [other["id"]], conv_type="direct")

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    assert response.get_json()["conversationId"] != abandoned


def test_two_groups_with_the_same_members_are_both_created(client, db, actor, make_user):
    # Only direct chats dedup — a class group and a party group
    # of the same people are two different rooms
    _, headers = actor
    other = make_user()

    first = _create_group(client, headers, [other["id"]], title="Paskaitos")
    second = _create_group(client, headers, [other["id"]], title="Vakarėlis")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.get_json()["conversationId"] != second.get_json()["conversationId"]
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2


def test_a_racing_double_submit_finds_its_twin_under_the_write_lock(client, db, actor, make_user,
                                                                    monkeypatch):
    # The pre-lock fast path misses exactly as it would when a
    # twin request commits right after it; the re-check inside
    # BEGIN IMMEDIATE must then answer 200 with the twin's id
    # instead of inserting a second DM
    user, headers = actor
    other = make_user()
    twin = _plant_conversation(db, [user["id"], other["id"]])

    from app.chat import routes as chat_routes
    real_lookup = chat_routes._find_direct_conversation
    calls = {"n": 0}

    def _miss_the_fast_path(conn, uid, oid):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_lookup(conn, uid, oid)

    monkeypatch.setattr(chat_routes, "_find_direct_conversation", _miss_the_fast_path)

    response = _create_direct(client, headers, other["id"])

    assert calls["n"] == 2, "the dedup must run again under the write lock"
    assert response.status_code == 200
    assert response.get_json()["conversationId"] == twin
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1




# ===========================================================
# POST /chat/conversations — presence plumbing and quota
# ===========================================================


def test_a_departed_socket_cannot_fail_an_already_committed_create(client, db, actor, make_user,
                                                                   monkeypatch):
    # The sid snapshot is stale by construction: the room join
    # is best effort and a create that already committed must
    # still answer 201
    user, headers = actor
    other, bystander = make_user(), make_user()

    from app.chat import events as chat_events
    monkeypatch.setitem(chat_events._connected_users, "sid-ghost", other["id"])
    monkeypatch.setitem(chat_events._connected_users, "sid-bystander", bystander["id"])

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    assert _members_of(db, response.get_json()["conversationId"]) == sorted(
        [user["id"], other["id"]])


def test_the_fifty_first_create_in_the_window_is_rate_limited(client, actor, make_user,
                                                              monkeypatch):
    user, headers = actor
    other = make_user()

    from app.auth.routes import _rate_limit_store
    monkeypatch.setitem(_rate_limit_store, f"chat_create:{user['id']}",
                        [time.monotonic()] * 50)

    response = _create_direct(client, headers, other["id"])
    assert response.status_code == 429
    assert response.get_json()["error"]


def test_the_create_quota_recovers_after_the_five_minute_window(client, actor, make_user,
                                                                monkeypatch):
    user, headers = actor
    other = make_user()

    from app.auth.routes import _rate_limit_store
    monkeypatch.setitem(_rate_limit_store, f"chat_create:{user['id']}",
                        [time.monotonic()] * 50)

    assert _create_direct(client, headers, other["id"]).status_code == 429

    with time_machine.travel(datetime.now(timezone.utc).timestamp() + 601, tick=False):
        assert _create_direct(client, headers, other["id"]).status_code == 201




# ===========================================================
# GET /chat/conversations
# ===========================================================


def test_listing_conversations_requires_authentication(client):
    assert client.get(CONVERSATIONS).status_code == 401


def test_a_new_user_has_an_empty_conversation_list(client, actor):
    _, headers = actor
    assert _conversations(client, headers) == []


def test_the_list_never_leaks_a_room_the_caller_is_not_in(client, db, actor, make_user):
    _, headers = actor
    one, two = make_user(), make_user()
    _plant_conversation(db, [one["id"], two["id"]])

    assert _conversations(client, headers) == []


@pytest.mark.contract
def test_a_conversation_row_carries_the_mobile_wire_shape(client, db, actor, make_user):
    user, headers = actor
    other = make_user(display_name="Ona Onaitė")
    conv_id = _create_group(client, headers, [other["id"]],
                            title="Kursas", avatar_emoji="📚").get_json()["conversationId"]
    _plant_message(db, conv_id, other["id"], text="Sveiki", created_at="2026-03-01T08:30:00")

    row = _row_for(_conversations(client, headers), conv_id)

    assert set(row) == {"id", "type", "title", "avatarEmoji", "pinned", "unreadCount",
                        "lastUpdatedMs", "participants", "lastMessage"}
    assert row["type"] == "group"
    assert row["title"] == "Kursas"
    assert row["avatarEmoji"] == "📚"
    assert row["pinned"] is False
    assert isinstance(row["lastUpdatedMs"], int)
    assert set(row["participants"][0]) == {"id", "displayName", "avatarUrl"}
    assert sorted(p["id"] for p in row["participants"]) == sorted([user["id"], other["id"]])
    assert set(row["lastMessage"]) == {"id", "text", "imageUrl", "time", "senderId",
                                       "senderName", "deleted"}
    assert row["lastMessage"]["text"] == "Sveiki"
    assert row["lastMessage"]["senderId"] == other["id"]
    assert row["lastMessage"]["senderName"] == "Ona Onaitė"
    assert row["lastMessage"]["time"] == "08:30"
    assert row["lastMessage"]["deleted"] is False


def test_a_conversation_without_messages_ships_no_last_message(client, actor, make_user):
    _, headers = actor
    other = make_user()
    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    assert "lastMessage" not in _row_for(_conversations(client, headers), conv_id)


def test_the_last_message_is_the_newest_row_of_the_room(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], text="senas", created_at="2026-01-01T10:00:00")
    newest = _plant_message(db, conv_id, other["id"], text="naujas",
                            created_at="2026-01-02T10:00:00")

    row = _row_for(_conversations(client, headers), conv_id)
    assert row["lastMessage"]["id"] == newest
    assert row["lastMessage"]["text"] == "naujas"


def test_an_unsent_last_message_is_flagged_and_blank(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], text="", created_at="2026-01-02T10:00:00",
                   deleted_at="2026-01-02T10:05:00")

    row = _row_for(_conversations(client, headers), conv_id)
    assert row["lastMessage"]["deleted"] is True
    assert row["lastMessage"]["text"] == ""


def test_a_photo_only_last_message_reports_its_upload_path(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], text="", image_url="/api/uploads/nuotrauka.jpg",
                   created_at="2026-01-02T10:00:00")

    row = _row_for(_conversations(client, headers), conv_id)
    assert row["lastMessage"]["imageUrl"] == "/api/uploads/nuotrauka.jpg"
    assert row["lastMessage"]["text"] == ""


def test_an_unparseable_message_stamp_leaves_the_time_field_blank(client, db, actor, make_user):
    # One bad row must not break the whole listing — `time` is
    # a convenience field, and the client formats createdAt anyway
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")
    _plant_message(db, conv_id, other["id"], text="bloga data", created_at="visai ne data")

    row = _row_for(_conversations(client, headers), conv_id)
    assert row["lastMessage"]["time"] == ""
    assert row["lastMessage"]["text"] == "bloga data"


def test_conversations_are_ordered_by_last_activity(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    oldest = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="A",
                                 updated_at="2026-01-01T10:00:00")
    newest = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="C",
                                 updated_at="2026-03-01T10:00:00")
    middle = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="B",
                                 updated_at="2026-02-01T10:00:00")

    rows = _conversations(client, headers)
    assert [row["id"] for row in rows] == [newest, middle, oldest]
    assert rows[0]["lastUpdatedMs"] > rows[1]["lastUpdatedMs"] > rows[2]["lastUpdatedMs"]


def test_a_pinned_conversation_sorts_before_a_newer_unpinned_one(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    pinned = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="Pinned",
                                 updated_at="2026-01-01T10:00:00", pinned_for={user["id"]})
    newer = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="Newer",
                                updated_at="2026-05-01T10:00:00")

    rows = _conversations(client, headers)
    assert [row["id"] for row in rows] == [pinned, newer]
    assert rows[0]["pinned"] is True
    assert rows[1]["pinned"] is False


def test_a_pin_is_private_to_the_member_who_set_it(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    other_headers = auth_headers(other)
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G",
                                  pinned_for={user["id"]})

    assert _row_for(_conversations(client, headers), conv_id)["pinned"] is True
    assert _row_for(_conversations(client, other_headers), conv_id)["pinned"] is False


@pytest.mark.contract
def test_last_updated_ms_is_epoch_milliseconds_of_the_utc_stamp(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G",
                                  updated_at="2026-03-01T12:00:00")

    expected = int(datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _row_for(_conversations(client, headers), conv_id)["lastUpdatedMs"] == expected


def test_an_unparseable_updated_at_falls_back_to_created_at(client, db, actor, make_user):
    # ONE bad stamp used to 500 the whole Messages tab
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G",
                                  created_at="2026-03-01T12:00:00", updated_at="ne data")

    expected = int(datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _row_for(_conversations(client, headers), conv_id)["lastUpdatedMs"] == expected


def test_a_row_with_two_bad_stamps_sorts_last_instead_of_failing_the_tab(client, db, actor,
                                                                        make_user):
    user, headers = actor
    other = make_user()
    broken = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G",
                                 created_at="visai ne data", updated_at="ne data")

    rows = _conversations(client, headers)
    assert _row_for(rows, broken)["lastUpdatedMs"] == 0


def test_a_direct_chat_is_named_after_the_other_participant(client, actor, make_user):
    _, headers = actor
    other = make_user(display_name="Jonas Jonaitis")
    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    row = _row_for(_conversations(client, headers), conv_id)
    assert row["type"] == "direct"
    assert row["title"] == "Jonas Jonaitis"


def test_a_direct_chat_with_nobody_else_left_has_a_null_title(client, db, actor):
    # The client renders messages.conversationFallback for this
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]], conv_type="direct")

    assert _row_for(_conversations(client, headers), conv_id)["title"] is None


def test_a_legacy_direct_chat_keeps_a_title_it_already_has(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="direct",
                                  title="Senas pavadinimas")

    assert _row_for(_conversations(client, headers), conv_id)["title"] == "Senas pavadinimas"


def test_a_group_keeps_its_own_title_and_every_participant(client, db, actor, make_user):
    user, headers = actor
    one, two = make_user(), make_user()
    conv_id = _create_group(client, headers, [one["id"], two["id"]],
                            title="Studentai").get_json()["conversationId"]

    row = _row_for(_conversations(client, headers), conv_id)
    assert row["title"] == "Studentai"
    assert sorted(p["id"] for p in row["participants"]) == sorted(
        [user["id"], one["id"], two["id"]])




# ===========================================================
# Unread counts — the row badge and the tab badge
# ===========================================================


def test_unread_counts_other_peoples_messages_after_the_watermark(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00"})
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T09:00:00")
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T12:00:00")

    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 2


def test_a_message_stamped_exactly_at_the_watermark_is_already_read(client, db, actor, make_user):
    # The comparison is strictly greater-than on the ISO string
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00"})
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T10:00:00")

    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 0


def test_the_callers_own_messages_are_never_unread_for_them(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00"})
    _plant_message(db, conv_id, user["id"], created_at="2026-01-01T11:00:00")
    _plant_message(db, conv_id, user["id"], created_at="2026-01-01T12:00:00")

    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 0


def test_unsent_messages_never_count_as_unread(client, db, actor, make_user):
    # The badge must agree with what the reader can still read
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00"})
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")
    _plant_message(db, conv_id, other["id"], text="", created_at="2026-01-01T12:00:00",
                   deleted_at="2026-01-01T12:01:00")

    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 1


def test_a_null_watermark_counts_the_whole_history(client, db, actor, make_user):
    # Membership rows older than the last_read_at column
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], created_at="1999-01-01T10:00:00")
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")

    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 2


def test_unread_is_counted_per_member_not_per_room(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    other_headers = auth_headers(other)
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00",
                                             other["id"]: "2026-01-01T10:00:00"})
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")

    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 1
    assert _row_for(_conversations(client, other_headers), conv_id)["unreadCount"] == 0


def test_the_tab_badge_requires_authentication(client):
    assert client.get("/api/chat/unread-count").status_code == 401


def test_the_tab_badge_sums_every_room_the_caller_belongs_to(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    one = _plant_conversation(db, [user["id"], other["id"]],
                              last_read={user["id"]: "2026-01-01T10:00:00"})
    two = _plant_conversation(db, [user["id"], other["id"]],
                              last_read={user["id"]: "2026-01-01T10:00:00"})
    _plant_message(db, one, other["id"], created_at="2026-01-01T11:00:00")
    _plant_message(db, two, other["id"], created_at="2026-01-01T11:00:00")
    _plant_message(db, two, other["id"], created_at="2026-01-01T12:00:00")

    response = client.get("/api/chat/unread-count", headers=headers)
    assert response.status_code == 200
    assert response.get_json()["unreadCount"] == 3


def test_the_tab_badge_agrees_with_the_row_badges(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00"})
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")
    _plant_message(db, conv_id, other["id"], text="", created_at="2026-01-01T12:00:00",
                   deleted_at="2026-01-01T12:01:00")
    _plant_message(db, conv_id, user["id"], created_at="2026-01-01T13:00:00")

    rows = _conversations(client, headers)
    total = client.get("/api/chat/unread-count", headers=headers).get_json()["unreadCount"]
    assert total == sum(row["unreadCount"] for row in rows) == 1


def test_a_stranger_has_no_unread_of_other_peoples_rooms(client, db, actor, make_user):
    _, headers = actor
    one, two = make_user(), make_user()
    conv_id = _plant_conversation(db, [one["id"], two["id"]])
    _plant_message(db, conv_id, one["id"], created_at="2026-01-01T11:00:00")

    assert client.get("/api/chat/unread-count", headers=headers).get_json()["unreadCount"] == 0


def test_marking_a_conversation_read_clears_its_badge(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00"})
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")

    response = client.put(f"{CONVERSATIONS}/{conv_id}/read", headers=headers)
    assert response.status_code == 200
    assert response.get_json()["readCount"] == 1
    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 0


def test_marking_read_from_a_null_watermark_receipts_the_whole_history(client, db, actor,
                                                                       make_user):
    # No prior watermark means the receipt scan is unbounded
    # below — every foreign message up to `now` is receipted
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")
    _plant_message(db, conv_id, other["id"], created_at="1999-01-01T10:00:00")
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")
    _plant_message(db, conv_id, user["id"], created_at="2026-01-01T12:00:00")

    response = client.put(f"{CONVERSATIONS}/{conv_id}/read", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["readCount"] == 2, "the caller's own message needs no receipt"
    assert client.put(f"{CONVERSATIONS}/{conv_id}/read",
                      headers=headers).get_json()["readCount"] == 0
    assert _row_for(_conversations(client, headers), conv_id)["unreadCount"] == 0


def test_the_rest_mark_read_spends_the_socket_quota(client, db, actor, make_user, monkeypatch):
    # REST must not be the free way around the socket twin's
    # 10-per-10-s budget
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")

    from app.chat import events as chat_events
    monkeypatch.setitem(chat_events._socket_rate, (user["id"], "mark_read"),
                        [time.monotonic()] * 10)

    response = client.put(f"{CONVERSATIONS}/{conv_id}/read", headers=headers)
    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"




# ===========================================================
# DELETE /chat/conversations/<id> — leave semantics
# ===========================================================


def test_leaving_a_conversation_requires_authentication(client, db, make_user):
    one, two = make_user(), make_user()
    conv_id = _plant_conversation(db, [one["id"], two["id"]])
    assert client.delete(f"{CONVERSATIONS}/{conv_id}").status_code == 401


def test_leaving_an_unknown_conversation_is_a_404(client, actor):
    _, headers = actor
    response = client.delete(f"{CONVERSATIONS}/no-such-room", headers=headers)
    assert response.status_code == 404
    assert response.get_json()["error"] == "Conversation not found"


def test_leaving_a_room_the_caller_never_joined_is_a_403(client, db, actor, make_user):
    _, headers = actor
    one, two = make_user(), make_user()
    conv_id = _plant_conversation(db, [one["id"], two["id"]])

    response = client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)
    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"
    assert _members_of(db, conv_id) == sorted([one["id"], two["id"]])


def test_leaving_removes_only_the_callers_membership(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    other_headers = auth_headers(other)
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")

    response = client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert _members_of(db, conv_id) == [other["id"]]
    assert _conversations(client, headers) == []
    assert _row_for(_conversations(client, other_headers), conv_id)["id"] == conv_id


def test_the_remaining_members_keep_the_history(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    other_headers = auth_headers(other)
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")
    msg_id = _plant_message(db, conv_id, user["id"], text="paskutinis",
                            created_at="2026-01-01T11:00:00")

    client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    messages = client.get(f"{CONVERSATIONS}/{conv_id}/messages",
                          headers=other_headers).get_json()["messages"]
    assert [m["id"] for m in messages] == [msg_id]
    assert messages[0]["senderId"] == user["id"], "the leaver's messages stay attributed to them"


def test_leaving_drops_the_leavers_receipts_and_reactions_only(client, db, actor, make_user):
    # A ghost reader would keep the remaining members' status
    # chips stuck on "delivered" forever
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")
    msg_id = _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")
    db.execute("INSERT INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
               (msg_id, user["id"], "2026-01-01T11:05:00"))
    db.execute("INSERT INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
               (msg_id, other["id"], "2026-01-01T11:05:00"))
    db.execute("INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
               (msg_id, user["id"], "\U0001F44D"))
    db.execute("INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
               (msg_id, other["id"], "\U0001F44D"))
    db.commit()

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200

    readers = [r[0] for r in db.execute("SELECT user_id FROM message_reads WHERE message_id = ?",
                                        (msg_id,))]
    reactors = [r[0] for r in db.execute(
        "SELECT user_id FROM message_reactions WHERE message_id = ?", (msg_id,))]
    assert readers == [other["id"]]
    assert reactors == [other["id"]]


def test_a_leavers_rows_in_other_rooms_survive(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    leaving = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="A")
    staying = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="B")
    kept_msg = _plant_message(db, staying, other["id"], created_at="2026-01-01T11:00:00")
    db.execute("INSERT INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
               (kept_msg, user["id"], "2026-01-01T11:05:00"))
    db.commit()

    client.delete(f"{CONVERSATIONS}/{leaving}", headers=headers)

    assert db.execute("SELECT COUNT(*) FROM message_reads WHERE message_id = ? AND user_id = ?",
                      (kept_msg, user["id"])).fetchone()[0] == 1
    assert [row["id"] for row in _conversations(client, headers)] == [staying]


def test_the_last_member_out_purges_the_room_and_its_messages(client, db, actor, make_user,
                                                              auth_headers):
    user, headers = actor
    other = make_user()
    other_headers = auth_headers(other)
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")
    msg_id = _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")
    db.execute("INSERT INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
               (msg_id, user["id"], "2026-01-01T11:05:00"))
    db.execute("INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
               (msg_id, user["id"], "\U0001F44D"))
    db.commit()

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?",
                      (conv_id,)).fetchone()[0] == 1, "the room survives while one member is left"

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=other_headers).status_code == 200

    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?",
                      (conv_id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM message_reads WHERE message_id = ?",
                      (msg_id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM message_reactions WHERE message_id = ?",
                      (msg_id,)).fetchone()[0] == 0


def test_leaving_a_solo_room_purges_it_immediately(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]], conv_type="direct")

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?",
                      (conv_id,)).fetchone()[0] == 0


def test_leaving_twice_is_a_403_the_second_time(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    second = client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)
    assert second.status_code == 403
    assert second.get_json()["error"] == "Not a participant"


def test_leaving_a_purged_room_answers_404(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]], conv_type="direct")

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 404


def test_leaving_drops_the_rooms_unread_from_the_tab_badge(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")
    assert client.get("/api/chat/unread-count", headers=headers).get_json()["unreadCount"] == 1

    client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    assert client.get("/api/chat/unread-count", headers=headers).get_json()["unreadCount"] == 0


def test_leaving_a_direct_chat_lets_it_be_created_again(client, db, actor, make_user):
    # The counterpart's own membership row survives, so the
    # count = 2 arm no longer matches — a fresh room is right
    user, headers = actor
    other = make_user()
    first = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    client.delete(f"{CONVERSATIONS}/{first}", headers=headers)
    again = _create_direct(client, headers, other["id"])

    assert again.status_code == 201
    assert again.get_json()["conversationId"] != first


def test_a_departed_socket_cannot_fail_an_already_committed_leave(client, db, actor, make_user,
                                                                  monkeypatch):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")

    from app.chat import events as chat_events
    monkeypatch.setitem(chat_events._connected_users, "sid-leaver", user["id"])
    monkeypatch.setitem(chat_events._connected_users, "sid-other", other["id"])

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    assert _members_of(db, conv_id) == [other["id"]]


def test_a_broken_socket_eviction_cannot_fail_the_leave(client, db, actor, make_user, monkeypatch):
    # The membership row is already committed when the eviction
    # runs — a raising room manager must be logged, not returned
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")

    from app.chat import events as chat_events
    from app.chat import routes as chat_routes

    def _boom(*args, **kwargs):
        raise RuntimeError("room manager is down")

    monkeypatch.setitem(chat_events._connected_users, "sid-leaver", user["id"])
    monkeypatch.setattr(chat_routes, "leave_room", _boom)

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    assert _members_of(db, conv_id) == [other["id"]]




# ===========================================================
# Non-member refusals across the conversation routes
# ===========================================================


def test_a_non_member_cannot_read_the_history(client, db, actor, make_user):
    _, headers = actor
    one, two = make_user(), make_user()
    conv_id = _plant_conversation(db, [one["id"], two["id"]])

    response = client.get(f"{CONVERSATIONS}/{conv_id}/messages", headers=headers)
    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"


def test_a_non_member_cannot_pin_a_room(client, db, actor, make_user):
    _, headers = actor
    one, two = make_user(), make_user()
    conv_id = _plant_conversation(db, [one["id"], two["id"]])

    response = client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)
    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"


def test_a_non_member_cannot_mark_a_room_read(client, db, actor, make_user):
    _, headers = actor
    one, two = make_user(), make_user()
    conv_id = _plant_conversation(db, [one["id"], two["id"]])

    response = client.put(f"{CONVERSATIONS}/{conv_id}/read", headers=headers)
    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"


def test_an_ex_member_is_refused_like_any_stranger(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T11:00:00")

    client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    assert client.get(f"{CONVERSATIONS}/{conv_id}/messages", headers=headers).status_code == 403
    assert client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers).status_code == 403
    assert client.put(f"{CONVERSATIONS}/{conv_id}/read", headers=headers).status_code == 403


def test_pinning_toggles_the_flag_and_shows_up_in_the_list(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="G")

    first = client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)
    assert first.status_code == 200
    assert first.get_json() == {"pinned": True}
    assert _row_for(_conversations(client, headers), conv_id)["pinned"] is True

    second = client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)
    assert second.get_json() == {"pinned": False}
    assert _row_for(_conversations(client, headers), conv_id)["pinned"] is False
