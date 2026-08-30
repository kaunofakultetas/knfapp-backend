# -----------------------------------------------------------
#  [*] Tests — chat conversations, the exhaustive pass
#
#  The gap-closing slice over app/chat/routes.py:
#  _find_direct_conversation, list_conversations and
#  create_conversation. A broad suite already proves the happy
#  paths; this module walks the arms the happy paths never
#  enter:
#
#    - _find_direct_conversation as a UNIT: the type gate, the
#      EXISTS arm, the COUNT(*) = 2 arm and the LIMIT 1, each
#      failed on its own so no single clause can quietly stop
#      mattering. Including the two shapes only a direct call
#      can build — the same id passed twice, and a membership
#      row pointing at a user that no longer exists.
#    - create_conversation's validation ORDER (which 400 wins
#      when a body breaks two rules at once), every wrong type
#      a JSON body can carry in each field, the NUL-stripping
#      the input hook does before the route ever sees the
#      body, and the boundaries: 0/1/50/51 ids, 100/101-char
#      titles, 16/17-char emoji, 2-member and 51-member rooms.
#    - The dedup's ordering against the active-user check (a
#      deactivated partner is a 400, never a 200 reuse), the
#      re-check under BEGIN IMMEDIATE rolling back cleanly,
#      and the presence loop: who gets joined, who does not,
#      and both departed-socket exceptions.
#    - list_conversations against rows the API cannot create:
#      a membership whose conversation is gone, a participant
#      whose user row is gone, a message whose sender is gone,
#      an epoch-zero updated_at, a pre-epoch stamp, a pinned
#      flag of 2 — plus the string-comparison edges of the
#      unread count (the epoch floor, the microsecond above
#      the watermark, the legacy space-form stamp).
#
#  Rule 10 of TESTPLAN.md applies throughout: anything that
#  asserts what is on the wire posts raw bytes, because the
#  app's JSON provider html-escapes a `json=` kwarg on the way
#  out.
# -----------------------------------------------------------

import json
import re
import time
import uuid
from datetime import datetime, timezone

import pytest
import time_machine


CONVERSATIONS = "/api/chat/conversations"

EPOCH_FLOOR = "1970-01-01T00:00:00"




# -----------------------------------------------------------
# _plant_user / _plant_users
# -----------------------------------------------------------
#
# Users written straight in, with a junk password hash: these
# accounts are only ever ids in a participant list, never
# callers, so they must not pay for a bcrypt round. _plant_user
# also reaches the two states make_user cannot: an empty
# display_name and a chosen avatar_url.
#
# Used by:
#   - the 50-id boundary, the list-shaping and the
#     active-check tests below
# -----------------------------------------------------------

def _plant_user(db, display_name=None, active=1, avatar_url=None):
    uid = str(uuid.uuid4())
    username = f"deep_{uuid.uuid4().hex[:10]}"
    db.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, role, active,"
        " invited, avatar_url) VALUES (?, ?, ?, ?, 'not-a-real-hash', 'student', ?, 1, ?)",
        (uid, username, f"{username}@knf.vu.lt",
         username.title() if display_name is None else display_name, active, avatar_url),
    )
    db.commit()
    return uid


def _plant_users(db, count):
    return [_plant_user(db) for _ in range(count)]




# -----------------------------------------------------------
# _plant_conversation / _plant_message
# -----------------------------------------------------------
#
# The only way to arrange what the routes refuse to write: a
# 'direct' row with one or three members, a membership whose
# conversation never existed, a message whose sender was hard
# deleted, an unparseable or epoch-zero stamp. The fixture
# connection has foreign keys OFF (get_db turns them on, a
# plain sqlite3.connect does not), which is exactly what makes
# the dangling rows plantable.
#
# `last_read` maps a member id to its watermark (absent =
# NULL), `pinned_for` maps a member id to its pinned value.
#
# Used by:
#   - nearly every test below
# -----------------------------------------------------------

def _plant_conversation(db, members, conv_type="direct", title=None, avatar_emoji=None,
                        created_at="2026-01-01T09:00:00", updated_at=None,
                        last_read=None, pinned_for=None, conv_id=None, created_by=None):
    conv_id = conv_id or f"conv-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conv_id, conv_type, title, avatar_emoji,
         created_by if created_by is not None else (members[0] if members else None),
         created_at, created_at if updated_at is None else updated_at),
    )
    for uid in members:
        db.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id, pinned, last_read_at)"
            " VALUES (?, ?, ?, ?)",
            (conv_id, uid, (pinned_for or {}).get(uid, 0), (last_read or {}).get(uid)),
        )
    db.commit()
    return conv_id


def _plant_message(db, conv_id, sender_id, text="Labas", created_at="2026-01-01T10:00:00",
                   deleted_at=None, image_url=None, msg_id=None):
    msg_id = msg_id or f"msg-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, deleted_at,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, image_url, deleted_at, created_at),
    )
    db.commit()
    return msg_id




# -----------------------------------------------------------
# request shorthands
# -----------------------------------------------------------
#
# _create posts a body as the mobile picker would; _post_raw
# puts EXACT bytes on the wire (TESTPLAN rule 10 — `json=`
# would arrive pre-escaped and falsify every assertion about
# markup); _rows / _row_for read the Messages tab back.
#
# Used by:
#   - nearly every test below
# -----------------------------------------------------------

def _create(client, headers, **body):
    return client.post(CONVERSATIONS, json=body, headers=headers)


def _create_direct(client, headers, other_id):
    return _create(client, headers, participantIds=[other_id], type="direct")


def _post_raw(client, headers, raw, content_type="application/json"):
    return client.post(CONVERSATIONS, data=raw,
                       headers={**headers, "Content-Type": content_type})


def _rows(client, headers):
    response = client.get(CONVERSATIONS, headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["conversations"]


def _row_for(client, headers, conv_id):
    matches = [row for row in _rows(client, headers) if row["id"] == conv_id]
    assert matches, f"conversation {conv_id} missing from the list"
    return matches[0]


def _lookup(db, user_id, other_id):
    from app.chat.routes import _find_direct_conversation
    return _find_direct_conversation(db, user_id, other_id)


def _count(db, sql, *params):
    return db.execute(sql, params).fetchone()[0]


def _spend_create_quota(monkeypatch, user_id, attempts):
    from app.auth.routes import _rate_limit_store
    monkeypatch.setitem(_rate_limit_store, f"chat_create:{user_id}",
                        [time.monotonic()] * attempts)




# ===========================================================
# _find_direct_conversation — the four clauses, one at a time
# ===========================================================


def test_the_direct_lookup_answers_none_when_the_pair_has_no_room(db, actor, make_user):
    user, _ = actor
    other = make_user()

    assert _lookup(db, user["id"], other["id"]) is None


def test_the_direct_lookup_finds_a_two_member_direct_room(db, actor, make_user):
    user, _ = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])

    assert _lookup(db, user["id"], other["id"]) == conv_id


def test_the_direct_lookup_is_symmetric_in_its_two_arguments(db, actor, make_user):
    user, _ = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])

    assert _lookup(db, other["id"], user["id"]) == conv_id


def test_the_direct_lookup_ignores_a_group_holding_the_same_two_people(db, actor, make_user):
    # The JOIN's `c.type = 'direct'` arm, failed on its own
    user, _ = actor
    other = make_user()
    _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="Grupė")

    assert _lookup(db, user["id"], other["id"]) is None


def test_the_direct_lookup_ignores_a_three_member_direct_row(db, actor, make_user):
    # The COUNT(*) = 2 arm — the v49 shape that must never be
    # reused as two people's DM
    user, _ = actor
    other, third = make_user(), make_user()
    _plant_conversation(db, [user["id"], other["id"], third["id"]])

    assert _lookup(db, user["id"], other["id"]) is None


def test_the_direct_lookup_ignores_a_solo_direct_room(db, actor):
    # COUNT(*) = 1: the EXISTS arm passes (the caller is in it)
    # only when both ids are the caller's, so this fails on the
    # count alone
    user, _ = actor
    _plant_conversation(db, [user["id"]])

    assert _lookup(db, user["id"], user["id"]) is None


def test_the_direct_lookup_ignores_a_room_the_other_person_is_not_in(db, actor, make_user):
    # The EXISTS arm: two members, direct, but the wrong pair
    user, _ = actor
    bystander, other = make_user(), make_user()
    _plant_conversation(db, [user["id"], bystander["id"]])

    assert _lookup(db, user["id"], other["id"]) is None


def test_the_direct_lookup_ignores_a_room_the_caller_has_left(db, actor, make_user):
    user, _ = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (conv_id, user["id"]))
    db.commit()

    assert _lookup(db, user["id"], other["id"]) is None


def test_the_direct_lookup_ignores_a_room_a_third_member_has_joined(db, actor, make_user):
    # A real DM stops being reusable the moment it grows a
    # third membership row — the count is re-evaluated per call
    user, _ = actor
    other, third = make_user(), make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    assert _lookup(db, user["id"], other["id"]) == conv_id

    db.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
               (conv_id, third["id"]))
    db.commit()

    assert _lookup(db, user["id"], other["id"]) is None


def test_the_direct_lookup_answers_none_for_ids_nobody_holds(db, actor):
    assert _lookup(db, "no-such-user", "no-such-other") is None


def test_the_direct_lookup_answers_none_on_an_empty_database(db, actor, make_user):
    user, _ = actor
    other = make_user()
    db.execute("DELETE FROM conversation_participants")
    db.execute("DELETE FROM conversations")
    db.commit()

    assert _lookup(db, user["id"], other["id"]) is None


def test_the_direct_lookup_picks_one_of_two_planted_twins(db, actor, make_user):
    # LIMIT 1 with no ORDER BY: either id is a correct answer,
    # and the route only ever needs "an" existing DM
    user, _ = actor
    other = make_user()
    first = _plant_conversation(db, [user["id"], other["id"]])
    second = _plant_conversation(db, [user["id"], other["id"]])

    assert _lookup(db, user["id"], other["id"]) in (first, second)


def test_the_direct_lookup_counts_rows_not_live_users(db, actor):
    # A membership row pointing at a hard-deleted account still
    # counts toward the two — the helper never joins users. The
    # route cannot reach this: STEP 3 rejects the unknown id
    # long before the dedup runs
    user, _ = actor
    conv_id = _plant_conversation(db, [user["id"], "ghost-account"])

    assert _lookup(db, user["id"], "ghost-account") == conv_id


def test_the_direct_lookup_matches_any_two_member_room_when_both_ids_are_the_same(db, actor,
                                                                                  make_user):
    # Passing one id twice satisfies the EXISTS arm with the
    # caller's own row, so ANY two-member DM of theirs matches.
    # The guard against a self-chat lives in create_conversation
    # (STEP 2), never here
    user, _ = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])

    assert _lookup(db, user["id"], user["id"]) == conv_id




# ===========================================================
# POST /chat/conversations — the body the route refuses
# ===========================================================


def test_a_body_that_is_not_json_at_all_is_refused(client, actor):
    _, headers = actor

    response = _post_raw(client, headers, "labas", content_type="text/plain")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_malformed_json_body_is_refused(client, actor):
    # The input hook ignores unparseable bytes (silent=True) —
    # the route's own get_json_object answers the 400
    _, headers = actor

    response = _post_raw(client, headers, '{"participantIds": [')

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_json_null_body_is_refused(client, actor):
    _, headers = actor

    response = _post_raw(client, headers, "null")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_top_level_number_body_is_refused_before_the_route_runs(client, actor):
    # The before_request hook owns non-object bodies; the route
    # never sees them
    _, headers = actor

    response = _post_raw(client, headers, "5")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_body_of_only_unknown_keys_is_refused_as_a_missing_participant_list(client, actor):
    _, headers = actor

    response = _create(client, headers, nonsense=True)

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_participant_ids_given_as_a_string_are_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=other["id"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_participant_ids_given_as_an_object_are_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds={"0": other["id"]})

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_participant_ids_given_as_null_are_refused(client, actor):
    _, headers = actor

    response = _create(client, headers, participantIds=None)

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_participant_ids_given_as_a_number_are_refused(client, actor):
    _, headers = actor

    response = _create(client, headers, participantIds=1)

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_fifty_one_ids_report_the_length_before_their_type(client, actor):
    # Validation ORDER: the length gate runs before the
    # element-type gate, so an oversized list of integers is a
    # length complaint
    _, headers = actor

    response = _create(client, headers, participantIds=list(range(51)))

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain at most 50 ids"


def test_a_numeric_participant_id_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"], 7])

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain non-empty strings"


def test_a_boolean_participant_id_is_refused(client, actor):
    # True is not a str — the isinstance arm, not the falsy one
    _, headers = actor

    response = _create(client, headers, participantIds=[True])

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain non-empty strings"


def test_a_null_participant_id_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"], None])

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain non-empty strings"


def test_a_nested_list_participant_id_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[[other["id"]]])

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain non-empty strings"


def test_a_participant_id_of_only_nul_bytes_is_stripped_to_blank_and_refused(client, actor):
    # The input hook strips NUL from every string in the body
    # first, so "\x00\x00" reaches the route as "" and fails
    # the non-empty arm
    _, headers = actor

    response = _post_raw(client, headers, json.dumps({"participantIds": ["\x00\x00"]}))

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain non-empty strings"


def test_a_participant_list_of_one_blank_string_is_refused(client, actor):
    _, headers = actor

    response = _create(client, headers, participantIds=[""])

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must contain non-empty strings"


def test_an_explicit_null_type_is_refused(client, actor, make_user):
    # data.get("type", "direct") hands back the NULL, not the
    # default — "type": null is a 400, not a direct chat
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type=None)

    assert response.status_code == 400
    assert response.get_json()["error"] == "type must be one of: direct, group"


def test_an_uppercase_type_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="DIRECT")

    assert response.status_code == 400
    assert response.get_json()["error"] == "type must be one of: direct, group"


def test_an_empty_type_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="")

    assert response.status_code == 400
    assert response.get_json()["error"] == "type must be one of: direct, group"


def test_a_numeric_type_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type=0)

    assert response.status_code == 400
    assert response.get_json()["error"] == "type must be one of: direct, group"


def test_the_type_is_validated_before_the_title(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="chat", title=5)

    assert response.status_code == 400
    assert response.get_json()["error"] == "type must be one of: direct, group"


def test_the_participant_list_is_validated_before_the_type(client, actor):
    _, headers = actor

    response = _create(client, headers, participantIds=[], type="chat")

    assert response.status_code == 400
    assert response.get_json()["error"] == "participantIds must be a non-empty array"


def test_a_numeric_title_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group", title=7)

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_a_list_title_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group", title=["a"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_a_boolean_title_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group", title=False)

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_a_title_of_a_hundred_and_one_characters_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group", title="a" * 101)

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be at most 100 characters"


def test_a_title_of_a_hundred_astral_emoji_is_accepted(client, db, actor, make_user):
    # len() counts code points, not UTF-8 bytes — 100 four-byte
    # emoji are exactly at the limit
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group",
                       title="\U0001F600" * 100)

    assert response.status_code == 201
    stored = db.execute("SELECT title FROM conversations WHERE id = ?",
                        (response.get_json()["conversationId"],)).fetchone()["title"]
    assert stored == "\U0001F600" * 100


def test_a_title_is_validated_even_when_a_direct_chat_would_discard_it(client, actor, make_user):
    # STEP 1 validates before STEP 1's direct-chat blanking —
    # an over-long title is a 400 whatever the type
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="direct",
                       title="a" * 101)

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be at most 100 characters"


def test_a_numeric_avatar_emoji_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group",
                       title="Grupė", avatarEmoji=1)

    assert response.status_code == 400
    assert response.get_json()["error"] == "avatarEmoji must be a string"


def test_an_object_avatar_emoji_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group",
                       title="Grupė", avatarEmoji={"emoji": "x"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "avatarEmoji must be a string"


def test_an_avatar_emoji_of_seventeen_characters_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group",
                       title="Grupė", avatarEmoji="x" * 17)

    assert response.status_code == 400
    assert response.get_json()["error"] == "avatarEmoji must be at most 16 characters"


def test_an_avatar_emoji_of_sixteen_astral_code_points_is_accepted(client, db, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group",
                       title="Grupė", avatarEmoji="\U0001F600" * 16)

    assert response.status_code == 201
    stored = db.execute("SELECT avatar_emoji FROM conversations WHERE id = ?",
                        (response.get_json()["conversationId"],)).fetchone()["avatar_emoji"]
    assert stored == "\U0001F600" * 16


def test_an_empty_avatar_emoji_is_stored_as_given(client, db, actor, make_user):
    # "" passes both emoji gates (it is a string, and 0 <= 16)
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group",
                       title="Grupė", avatarEmoji="")

    assert response.status_code == 201
    stored = db.execute("SELECT avatar_emoji FROM conversations WHERE id = ?",
                        (response.get_json()["conversationId"],)).fetchone()["avatar_emoji"]
    assert stored == ""


def test_an_avatar_emoji_is_validated_even_when_a_direct_chat_would_discard_it(client, actor,
                                                                               make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="direct",
                       avatarEmoji="x" * 17)

    assert response.status_code == 400
    assert response.get_json()["error"] == "avatarEmoji must be at most 16 characters"


def test_a_group_title_of_a_single_character_is_accepted(client, db, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group", title="A")

    assert response.status_code == 201
    assert db.execute("SELECT title FROM conversations WHERE id = ?",
                      (response.get_json()["conversationId"],)).fetchone()["title"] == "A"


def test_a_group_title_of_only_a_non_breaking_space_is_refused(client, actor, make_user):
    # str.strip() drops \xa0 too — a "titled" group that renders
    # blank is still a 400
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group",
                       title="\xa0\xa0")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Group conversations require a title"


def test_a_group_title_of_only_nul_bytes_is_refused(client, actor, make_user):
    # The input hook strips the NULs first, leaving "" for the
    # blank-title gate
    _, headers = actor
    other = make_user()

    response = _post_raw(client, headers, json.dumps({
        "participantIds": [other["id"]], "type": "group", "title": "\x00\x00",
    }))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Group conversations require a title"


def test_a_group_title_of_a_newline_is_refused(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group", title="\n\t ")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Group conversations require a title"


def test_a_group_title_keeps_the_whitespace_around_it(client, db, actor, make_user):
    # .strip() is used to TEST the title, never to rewrite it —
    # what the client sent is what is stored
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="group",
                       title="  Grupė  ")

    assert response.status_code == 201
    assert db.execute("SELECT title FROM conversations WHERE id = ?",
                      (response.get_json()["conversationId"],)).fetchone()["title"] == "  Grupė  "


def test_a_direct_chat_accepts_a_blank_title_a_group_would_reject(client, db, actor, make_user):
    # The group gate is skipped for a direct chat, and the
    # blanking then throws the title away anyway
    _, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]], type="direct", title="   ")

    assert response.status_code == 201
    row = db.execute("SELECT title, avatar_emoji FROM conversations WHERE id = ?",
                     (response.get_json()["conversationId"],)).fetchone()
    assert row["title"] is None and row["avatar_emoji"] is None




# ===========================================================
# POST /chat/conversations — the member set
# ===========================================================


def test_a_participant_list_of_the_caller_twice_is_refused(client, actor):
    # set() collapses the duplicates, leaving one member
    user, headers = actor

    response = _create(client, headers, participantIds=[user["id"], user["id"]])

    assert response.status_code == 400
    assert response.get_json()["error"] == "A conversation needs at least one other participant"


def test_a_group_of_the_caller_alone_is_refused(client, actor):
    user, headers = actor

    response = _create(client, headers, participantIds=[user["id"]], type="group", title="Aš")

    assert response.status_code == 400
    assert response.get_json()["error"] == "A conversation needs at least one other participant"


def test_fifty_copies_of_one_id_collapse_to_a_two_member_room(client, db, actor, make_user):
    user, headers = actor
    other = make_user()

    response = _create(client, headers, participantIds=[other["id"]] * 50, type="direct")

    assert response.status_code == 201
    conv_id = response.get_json()["conversationId"]
    assert _count(db, "SELECT COUNT(*) FROM conversation_participants WHERE conversation_id = ?",
                  conv_id) == 2


def test_a_direct_chat_with_fifty_distinct_people_is_refused(client, db, actor):
    _, headers = actor
    others = _plant_users(db, 50)

    response = _create(client, headers, participantIds=others, type="direct")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Direct conversations must have exactly 2 participants"


def test_fifty_real_participants_create_a_fifty_one_member_group(client, db, actor):
    # The maximum room the route will build: 50 ids plus the
    # creator
    _, headers = actor
    others = _plant_users(db, 50)

    response = _create(client, headers, participantIds=others, type="group", title="Kursas")

    assert response.status_code == 201
    conv_id = response.get_json()["conversationId"]
    assert _count(db, "SELECT COUNT(*) FROM conversation_participants WHERE conversation_id = ?",
                  conv_id) == 51


def test_a_group_of_exactly_two_people_is_not_a_direct_chat(client, db, actor, make_user):
    # A two-person GROUP must never be recycled as the pair's
    # DM — the type gate in the dedup lookup
    user, headers = actor
    other = make_user()

    group = _create(client, headers, participantIds=[other["id"]], type="group", title="Duetas")
    direct = _create_direct(client, headers, other["id"])

    assert group.status_code == 201
    assert direct.status_code == 201
    assert group.get_json()["conversationId"] != direct.get_json()["conversationId"]
    assert _count(db, "SELECT COUNT(*) FROM conversations") == 2




# ===========================================================
# POST /chat/conversations — the users must exist and be live
# ===========================================================


def test_one_unknown_id_among_valid_ones_fails_the_whole_request(client, db, actor, make_user):
    _, headers = actor
    first, second = make_user(), make_user()

    response = _create(client, headers, participantIds=[first["id"], second["id"], "ghost"],
                       type="group", title="Kursas")

    assert response.status_code == 400
    assert response.get_json()["error"] == "One or more participant IDs are invalid"
    assert _count(db, "SELECT COUNT(*) FROM conversations") == 0


def test_one_deactivated_id_among_valid_ones_fails_the_whole_request(client, db, actor, make_user):
    _, headers = actor
    live = make_user()
    frozen = _plant_user(db, active=0)

    response = _create(client, headers, participantIds=[live["id"], frozen],
                       type="group", title="Kursas")

    assert response.status_code == 400
    assert response.get_json()["error"] == "One or more participant IDs are invalid"
    assert _count(db, "SELECT COUNT(*) FROM conversations") == 0


def test_a_partner_deactivated_since_the_picker_loaded_is_refused(client, db, actor, make_user):
    _, headers = actor
    other = make_user()
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (other["id"],))
    db.commit()

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "One or more participant IDs are invalid"


def test_the_active_check_runs_before_the_direct_dedup(client, db, actor, make_user):
    # STEP 3 sits above STEP 4: once the partner is frozen even
    # the EXISTING room is unreachable through create — the
    # caller gets the 400, never a 200 reuse
    user, headers = actor
    other = make_user()
    first = _create_direct(client, headers, other["id"])
    assert first.status_code == 201

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (other["id"],))
    db.commit()

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "One or more participant IDs are invalid"




# ===========================================================
# POST /chat/conversations — the direct dedup
# ===========================================================


def test_the_reuse_answer_carries_only_the_conversation_id(client, actor, make_user):
    _, headers = actor
    other = make_user()
    created = _create_direct(client, headers, other["id"])

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 200
    assert response.get_json() == {"conversationId": created.get_json()["conversationId"]}


def test_the_create_answer_carries_only_the_conversation_id(client, actor, make_user):
    _, headers = actor
    other = make_user()

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    assert list(response.get_json().keys()) == ["conversationId"]


def test_reusing_a_direct_room_writes_nothing_at_all(client, db, actor, make_user):
    # The 200 path must not bump updated_at, add a member or
    # move anybody's watermark
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  created_at="2026-01-01T09:00:00",
                                  updated_at="2026-01-02T09:00:00",
                                  last_read={user["id"]: "2026-01-01T09:30:00"})

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 200
    row = db.execute("SELECT updated_at FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    assert row["updated_at"] == "2026-01-02T09:00:00"
    assert _count(db, "SELECT COUNT(*) FROM conversation_participants WHERE conversation_id = ?",
                  conv_id) == 2
    assert db.execute(
        "SELECT last_read_at FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user["id"]),
    ).fetchone()["last_read_at"] == "2026-01-01T09:30:00"


def test_a_direct_room_that_grew_a_third_member_is_not_reused(client, db, actor, make_user):
    # The COUNT(*) = 2 arm, reached through the route: the old
    # room no longer qualifies, so a SECOND room is created
    user, headers = actor
    other, third = make_user(), make_user()
    first = _create_direct(client, headers, other["id"])
    conv_id = first.get_json()["conversationId"]
    db.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
               (conv_id, third["id"]))
    db.commit()

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    assert response.get_json()["conversationId"] != conv_id
    assert _count(db, "SELECT COUNT(*) FROM conversations") == 2


def test_a_direct_room_between_two_other_people_is_not_reused(client, db, actor, make_user):
    user, headers = actor
    other, stranger = make_user(), make_user()
    _plant_conversation(db, [other["id"], stranger["id"]])

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    assert _count(db, "SELECT COUNT(*) FROM conversations") == 2


def test_a_group_create_never_consults_the_direct_dedup(client, actor, make_user, monkeypatch):
    # Both dedup calls sit behind `if conv_type == "direct"` —
    # a lookup that explodes proves a group never reaches either
    _, headers = actor
    other = make_user()

    from app.chat import routes as chat_routes

    def _must_not_run(*args, **kwargs):
        raise AssertionError("a group create must not run the direct dedup")

    monkeypatch.setattr(chat_routes, "_find_direct_conversation", _must_not_run)

    response = _create(client, headers, participantIds=[other["id"]], type="group", title="Grupė")

    assert response.status_code == 201


def test_the_recheck_under_the_write_lock_rolls_its_transaction_back(client, db, actor, make_user,
                                                                     monkeypatch):
    # The loser of a double-submit takes BEGIN IMMEDIATE, finds
    # the twin and must leave the database exactly as it found
    # it — no half-written conversation, no orphan membership
    user, headers = actor
    other = make_user()
    twin = _plant_conversation(db, [user["id"], other["id"]])

    from app.chat import routes as chat_routes
    real_lookup = chat_routes._find_direct_conversation
    calls = {"n": 0}

    def _miss_the_fast_path(conn, uid, oid):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_lookup(conn, uid, oid)

    monkeypatch.setattr(chat_routes, "_find_direct_conversation", _miss_the_fast_path)

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 200
    assert response.get_json()["conversationId"] == twin
    assert _count(db, "SELECT COUNT(*) FROM conversations") == 1
    assert _count(db, "SELECT COUNT(*) FROM conversation_participants") == 2
    # and the connection recovered: the next call still answers
    assert _create_direct(client, headers, other["id"]).status_code == 200




# ===========================================================
# POST /chat/conversations — what lands in the database
# ===========================================================


def test_a_created_room_stamps_naive_utc_isoformat_everywhere(client, db, actor, make_user):
    # created_at, updated_at and every member's last_read_at are
    # the SAME naive-UTC string — cursors and unread counts
    # compare these as plain text
    user, headers = actor
    other = make_user()

    with time_machine.travel(datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc), tick=False):
        response = _create_direct(client, headers, other["id"])

    conv_id = response.get_json()["conversationId"]
    row = db.execute("SELECT created_at, updated_at FROM conversations WHERE id = ?",
                     (conv_id,)).fetchone()
    watermarks = [r["last_read_at"] for r in db.execute(
        "SELECT last_read_at FROM conversation_participants WHERE conversation_id = ?", (conv_id,))]

    assert row["created_at"] == "2026-03-04T05:06:07"
    assert row["updated_at"] == row["created_at"]
    assert watermarks == [row["created_at"], row["created_at"]]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?", row["created_at"])


def test_a_created_room_records_the_caller_as_its_creator(client, db, actor, make_user):
    user, headers = actor
    other = make_user()

    response = _create_direct(client, headers, other["id"])

    assert db.execute("SELECT created_by FROM conversations WHERE id = ?",
                      (response.get_json()["conversationId"],)).fetchone()["created_by"] == user["id"]


def test_a_created_room_gets_a_uuid_for_an_id(client, actor, make_user):
    _, headers = actor
    other = make_user()

    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    assert uuid.UUID(conv_id).version == 4


def test_two_creates_never_share_an_id(client, actor, make_user):
    _, headers = actor
    first, second = make_user(), make_user()

    one = _create_direct(client, headers, first["id"]).get_json()["conversationId"]
    two = _create_direct(client, headers, second["id"]).get_json()["conversationId"]

    assert one != two


def test_every_member_starts_unpinned(client, db, actor, make_user):
    _, headers = actor
    other = make_user()

    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    pins = {r["pinned"] for r in db.execute(
        "SELECT pinned FROM conversation_participants WHERE conversation_id = ?", (conv_id,))}
    assert pins == {0}




# ===========================================================
# POST /chat/conversations — the presence plumbing
# ===========================================================


def test_only_connected_members_are_joined_to_the_new_room(client, actor, make_user, monkeypatch):
    user, headers = actor
    other, stranger = make_user(), make_user()

    from app.chat import events as chat_events
    from app.chat import routes as chat_routes
    monkeypatch.setattr(chat_events, "_connected_users",
                        {"sid-other": other["id"], "sid-stranger": stranger["id"]})
    joined = []
    monkeypatch.setattr(chat_routes, "join_room",
                        lambda room, sid=None, namespace=None: joined.append((room, sid, namespace)))

    response = _create_direct(client, headers, other["id"])

    conv_id = response.get_json()["conversationId"]
    assert joined == [(f"conv:{conv_id}", "sid-other", "/")]


def test_the_creators_own_socket_is_joined_too(client, actor, make_user, monkeypatch):
    # The loop tests membership of all_ids, and the creator is
    # in it — their socket is joined server-side even though
    # their client also emits join_conversation
    user, headers = actor
    other = make_user()

    from app.chat import events as chat_events
    from app.chat import routes as chat_routes
    monkeypatch.setattr(chat_events, "_connected_users", {"sid-me": user["id"]})
    joined = []
    monkeypatch.setattr(chat_routes, "join_room",
                        lambda room, sid=None, namespace=None: joined.append(sid))

    _create_direct(client, headers, other["id"])

    assert joined == ["sid-me"]


def test_every_socket_of_a_member_with_two_devices_is_joined(client, actor, make_user, monkeypatch):
    _, headers = actor
    other = make_user()

    from app.chat import events as chat_events
    from app.chat import routes as chat_routes
    monkeypatch.setattr(chat_events, "_connected_users",
                        {"sid-phone": other["id"], "sid-web": other["id"]})
    joined = []
    monkeypatch.setattr(chat_routes, "join_room",
                        lambda room, sid=None, namespace=None: joined.append(sid))

    _create_direct(client, headers, other["id"])

    assert sorted(joined) == ["sid-phone", "sid-web"]


def test_nobody_is_joined_when_no_member_is_online(client, actor, make_user, monkeypatch):
    _, headers = actor
    other, stranger = make_user(), make_user()

    from app.chat import events as chat_events
    from app.chat import routes as chat_routes
    monkeypatch.setattr(chat_events, "_connected_users", {"sid-stranger": stranger["id"]})
    joined = []
    monkeypatch.setattr(chat_routes, "join_room",
                        lambda room, sid=None, namespace=None: joined.append(sid))

    assert _create_direct(client, headers, other["id"]).status_code == 201
    assert joined == []


@pytest.mark.parametrize("boom", [KeyError("sid"), ValueError("unknown session")])
def test_both_departed_socket_errors_are_swallowed_after_the_commit(client, db, actor, make_user,
                                                                     monkeypatch, boom):
    # Both exceptions python-socketio raises for a sid that
    # vanished between the snapshot and the join — the room is
    # committed, so the caller must still get its 201
    user, headers = actor
    other = make_user()

    from app.chat import events as chat_events
    from app.chat import routes as chat_routes
    monkeypatch.setattr(chat_events, "_connected_users", {"sid-ghost": other["id"]})

    def _explode(room, sid=None, namespace=None):
        raise boom

    monkeypatch.setattr(chat_routes, "join_room", _explode)

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 201
    assert _count(db, "SELECT COUNT(*) FROM conversation_participants WHERE conversation_id = ?",
                  response.get_json()["conversationId"]) == 2




# ===========================================================
# POST /chat/conversations — the quota
# ===========================================================


def test_the_fiftieth_create_in_the_window_still_succeeds(client, actor, make_user, monkeypatch):
    user, headers = actor
    other = make_user()
    _spend_create_quota(monkeypatch, user["id"], 49)

    assert _create_direct(client, headers, other["id"]).status_code == 201


def test_a_refused_body_still_burns_create_quota(client, actor, make_user, monkeypatch):
    # The decorator records the attempt before the handler
    # validates anything — 49 spent plus one 400 exhausts it
    user, headers = actor
    other = make_user()
    _spend_create_quota(monkeypatch, user["id"], 49)

    assert _create(client, headers, participantIds=[]).status_code == 400
    assert _create_direct(client, headers, other["id"]).status_code == 429


def test_the_create_quota_is_counted_per_user(client, actor, make_user, auth_headers, monkeypatch):
    user, headers = actor
    other = make_user()
    _spend_create_quota(monkeypatch, user["id"], 50)

    assert _create_direct(client, headers, other["id"]).status_code == 429
    assert _create_direct(client, auth_headers(other), user["id"]).status_code == 201


def test_an_exhausted_quota_answers_the_rate_limited_code(client, actor, make_user, monkeypatch):
    user, headers = actor
    other = make_user()
    _spend_create_quota(monkeypatch, user["id"], 50)

    response = _create_direct(client, headers, other["id"])

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_an_anonymous_create_is_refused_with_a_401(client, make_user):
    other = make_user()

    response = client.post(CONVERSATIONS, json={"participantIds": [other["id"]]})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_a_bogus_bearer_token_cannot_create_a_conversation(client, make_user):
    other = make_user()

    response = client.post(CONVERSATIONS, json={"participantIds": [other["id"]]},
                           headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401




# ===========================================================
# GET /chat/conversations — the rows it refuses to show
# ===========================================================


def test_the_list_requires_a_session(client):
    assert client.get(CONVERSATIONS).status_code == 401


def test_an_empty_list_ships_an_empty_array_not_null(client, actor):
    _, headers = actor

    assert client.get(CONVERSATIONS, headers=headers).get_json() == {"conversations": []}


def test_a_membership_whose_conversation_vanished_is_not_listed(client, db, actor, make_user):
    # The INNER JOIN on conversations — a dangling membership
    # row (only writable with foreign keys off) is dropped
    # instead of 500ing the whole tab
    user, headers = actor
    other = make_user()
    real = _plant_conversation(db, [user["id"], other["id"]])
    db.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
               ("conv-that-never-existed", user["id"]))
    db.commit()

    rows = _rows(client, headers)

    assert [row["id"] for row in rows] == [real]


def test_a_participant_whose_user_row_vanished_is_dropped_from_the_row(client, db, actor,
                                                                       make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"], "ghost-account"],
                                  conv_type="group", title="Kursas")

    row = _row_for(client, headers, conv_id)

    assert sorted(p["id"] for p in row["participants"]) == sorted([user["id"], other["id"]])


def test_a_direct_chat_whose_partner_vanished_falls_back_to_a_null_title(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], "ghost-account"])

    row = _row_for(client, headers, conv_id)

    assert row["title"] is None
    assert [p["id"] for p in row["participants"]] == [user["id"]]


def test_a_partner_without_a_display_name_gives_an_empty_title(client, db, actor):
    # other[0]["display_name"] is used verbatim — an empty name
    # is an empty title, NOT the null that triggers the client's
    # localized fallback
    user, headers = actor
    nameless = _plant_user(db, display_name="")
    conv_id = _plant_conversation(db, [user["id"], nameless])

    assert _row_for(client, headers, conv_id)["title"] == ""


def test_a_direct_chat_with_an_empty_title_is_named_after_the_partner(client, db, actor, make_user):
    # `not title` catches "" as well as NULL
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], title="")

    assert _row_for(client, headers, conv_id)["title"] == other["username"].title()


def test_a_group_without_a_title_keeps_a_null_title(client, db, actor, make_user):
    # The partner-name fallback is for direct rows only
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group")

    assert _row_for(client, headers, conv_id)["title"] is None


def test_a_direct_chat_keeps_a_title_it_already_carries(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], title="Senas pavadinimas")

    assert _row_for(client, headers, conv_id)["title"] == "Senas pavadinimas"


def test_a_group_title_is_html_escaped_on_the_wire(client, db, actor, make_user):
    # The app's JSON provider escapes every string it
    # serialises, so the title the mobile list renders arrives
    # entity-encoded. Posted as raw bytes — a `json=` body would
    # already be escaped going IN (TESTPLAN rule 10)
    _, headers = actor
    other = make_user()

    created = _post_raw(client, headers, json.dumps({
        "participantIds": [other["id"]], "type": "group", "title": "Kava & Arbata <3",
    }))
    assert created.status_code == 201
    conv_id = created.get_json()["conversationId"]

    assert db.execute("SELECT title FROM conversations WHERE id = ?",
                      (conv_id,)).fetchone()["title"] == "Kava & Arbata <3"
    assert _row_for(client, headers, conv_id)["title"] == "Kava &amp; Arbata &lt;3"


def test_a_partner_name_is_html_escaped_in_a_direct_title(client, db, actor):
    user, headers = actor
    other = _plant_user(db, display_name="Ona & Co")
    conv_id = _plant_conversation(db, [user["id"], other])

    assert _row_for(client, headers, conv_id)["title"] == "Ona &amp; Co"




# ===========================================================
# GET /chat/conversations — the row shape
# ===========================================================


def test_a_row_without_messages_ships_exactly_the_documented_keys(client, actor, make_user):
    _, headers = actor
    other = make_user()
    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    row = _row_for(client, headers, conv_id)

    assert set(row) == {"id", "type", "title", "avatarEmoji", "pinned", "unreadCount",
                        "lastUpdatedMs", "participants"}


def test_a_row_with_messages_adds_exactly_the_last_message_key(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"])

    row = _row_for(client, headers, conv_id)

    assert set(row) == {"id", "type", "title", "avatarEmoji", "pinned", "unreadCount",
                        "lastUpdatedMs", "participants", "lastMessage"}
    assert set(row["lastMessage"]) == {"id", "text", "imageUrl", "time", "senderId",
                                       "senderName", "deleted"}


def test_the_participant_block_carries_the_avatar_url(client, db, actor):
    user, headers = actor
    other = _plant_user(db, avatar_url="/api/uploads/avatars/ona.jpg")
    conv_id = _plant_conversation(db, [user["id"], other])

    row = _row_for(client, headers, conv_id)
    portraits = {p["id"]: p["avatarUrl"] for p in row["participants"]}

    assert portraits[other] == "/api/uploads/avatars/ona.jpg"
    assert portraits[user["id"]] is None


def test_the_caller_is_listed_among_the_participants(client, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    row = _row_for(client, headers, conv_id)

    assert sorted(p["id"] for p in row["participants"]) == sorted([user["id"], other["id"]])


def test_two_rooms_never_borrow_each_others_participants(client, db, actor, make_user):
    # The participants_map grouping keyed by conversation_id
    user, headers = actor
    left, right = make_user(), make_user()
    a = _plant_conversation(db, [user["id"], left["id"]])
    b = _plant_conversation(db, [user["id"], right["id"]])

    rows = {row["id"]: row for row in _rows(client, headers)}

    assert sorted(p["id"] for p in rows[a]["participants"]) == sorted([user["id"], left["id"]])
    assert sorted(p["id"] for p in rows[b]["participants"]) == sorted([user["id"], right["id"]])


def test_a_fifty_one_member_room_lists_every_member(client, db, actor):
    _, headers = actor
    others = _plant_users(db, 50)

    conv_id = _create(client, headers, participantIds=others, type="group",
                      title="Kursas").get_json()["conversationId"]

    assert len(_row_for(client, headers, conv_id)["participants"]) == 51


def test_the_avatar_emoji_of_a_group_rides_along(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group",
                                  title="Grupė", avatar_emoji="\U0001F4DA")

    assert _row_for(client, headers, conv_id)["avatarEmoji"] == "\U0001F4DA"


def test_the_type_is_reported_verbatim(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    group = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="Grupė")
    direct = _plant_conversation(db, [user["id"], other["id"]])

    rows = {row["id"]: row["type"] for row in _rows(client, headers)}

    assert rows[group] == "group" and rows[direct] == "direct"




# ===========================================================
# GET /chat/conversations — the last message
# ===========================================================


def test_the_newest_message_wins_the_last_message_slot(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], text="Pirma", created_at="2026-01-01T10:00:00")
    _plant_message(db, conv_id, user["id"], text="Antra", created_at="2026-01-01T11:00:00")

    assert _row_for(client, headers, conv_id)["lastMessage"]["text"] == "Antra"


def test_two_messages_sharing_a_stamp_are_broken_by_the_higher_id(client, db, actor, make_user):
    # ROW_NUMBER orders created_at DESC, id DESC — the tiebreak
    # that keeps the pick deterministic
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], text="A", msg_id="msg-aaa",
                   created_at="2026-01-01T10:00:00")
    _plant_message(db, conv_id, other["id"], text="Z", msg_id="msg-zzz",
                   created_at="2026-01-01T10:00:00")

    assert _row_for(client, headers, conv_id)["lastMessage"]["id"] == "msg-zzz"


def test_a_message_whose_sender_vanished_never_becomes_the_last_message(client, db, actor,
                                                                        make_user):
    # The last-message subquery INNER JOINs users, so an orphan
    # row is skipped and the newest LIVE message shows instead
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], text="Matoma", created_at="2026-01-01T10:00:00")
    _plant_message(db, conv_id, "ghost-account", text="Našlaitė",
                   created_at="2026-01-01T11:00:00")

    assert _row_for(client, headers, conv_id)["lastMessage"]["text"] == "Matoma"


def test_a_room_whose_only_message_lost_its_sender_ships_no_last_message(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], "ghost-account"])
    _plant_message(db, conv_id, "ghost-account")

    assert "lastMessage" not in _row_for(client, headers, conv_id)


def test_an_empty_last_message_text_ships_as_an_empty_string(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], text="", image_url="/api/uploads/chat/x.jpg")

    last = _row_for(client, headers, conv_id)["lastMessage"]

    assert last["text"] == ""
    assert last["imageUrl"] == "/api/uploads/chat/x.jpg"


def test_an_unsent_last_message_keeps_its_slot_and_is_flagged(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], text="", deleted_at="2026-01-01T12:00:00")

    last = _row_for(client, headers, conv_id)["lastMessage"]

    assert last["deleted"] is True
    assert last["text"] == ""


def test_a_live_last_message_is_not_flagged_as_deleted(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"])

    assert _row_for(client, headers, conv_id)["lastMessage"]["deleted"] is False


def test_the_last_message_time_is_utc_hh_mm(client, db, actor, make_user):
    # Documented contract: `time` is UTC, not Lithuanian time —
    # clients format createdAt themselves
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], created_at="2026-06-01T21:07:00")

    assert _row_for(client, headers, conv_id)["lastMessage"]["time"] == "21:07"


def test_an_unparseable_last_message_stamp_leaves_the_time_blank(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], created_at="vakar")

    assert _row_for(client, headers, conv_id)["lastMessage"]["time"] == ""


def test_the_last_message_names_its_sender(client, db, actor, make_user):
    user, headers = actor
    other = _plant_user(db, display_name="Ona Onaitė")
    conv_id = _plant_conversation(db, [user["id"], other])
    _plant_message(db, conv_id, other)

    last = _row_for(client, headers, conv_id)["lastMessage"]

    assert last["senderId"] == other
    assert last["senderName"] == "Ona Onaitė"


def test_a_message_in_another_room_never_becomes_this_rooms_last(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    quiet = _plant_conversation(db, [user["id"], other["id"]])
    loud = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="Grupė")
    _plant_message(db, loud, other["id"], text="Grupės žinutė")

    assert "lastMessage" not in _row_for(client, headers, quiet)
    assert _row_for(client, headers, loud)["lastMessage"]["text"] == "Grupės žinutė"




# ===========================================================
# GET /chat/conversations — the unread count
# ===========================================================


def test_a_message_exactly_at_the_epoch_floor_is_not_unread(client, db, actor, make_user):
    # A NULL watermark compares against '1970-01-01T00:00:00'
    # STRICTLY — a message stamped at the floor itself does not
    # count
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], created_at=EPOCH_FLOOR)

    assert _row_for(client, headers, conv_id)["unreadCount"] == 0


def test_a_message_one_second_above_the_epoch_floor_is_unread(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], created_at="1970-01-01T00:00:01")

    assert _row_for(client, headers, conv_id)["unreadCount"] == 1


def test_a_message_one_microsecond_past_the_watermark_is_unread(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00.000000"})
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T10:00:00.000001")

    assert _row_for(client, headers, conv_id)["unreadCount"] == 1


def test_a_message_exactly_at_the_watermark_is_already_read(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2026-01-01T10:00:00.000000"})
    _plant_message(db, conv_id, other["id"], created_at="2026-01-01T10:00:00.000000")

    assert _row_for(client, headers, conv_id)["unreadCount"] == 0


def test_a_watermark_in_the_future_hides_every_message(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={user["id"]: "2099-01-01T00:00:00"})
    _plant_message(db, conv_id, other["id"])

    assert _row_for(client, headers, conv_id)["unreadCount"] == 0


def test_the_unread_count_is_a_plain_string_comparison_of_the_stamps(client, db, actor, make_user):
    # Documented behaviour, not an accident: the space-form
    # stamp SQLite's datetime('now') default writes sorts BELOW
    # the 'T' of an ISO stamp, so a legacy row can hide from the
    # badge (migration v17 normalised the historic rows away).
    # Both directions of the quirk in one place
    user, headers = actor
    other = make_user()
    hidden = _plant_conversation(db, [user["id"], other["id"]],
                                 last_read={user["id"]: "2026-01-01T00:00:00"})
    _plant_message(db, hidden, other["id"], created_at="2026-01-01 23:00:00")

    surfaced = _plant_conversation(db, [user["id"], other["id"]],
                                   last_read={user["id"]: "2026-01-01 23:00:00"})
    _plant_message(db, surfaced, other["id"], created_at="2026-01-01T00:00:00")

    rows = {row["id"]: row["unreadCount"] for row in _rows(client, headers)}

    assert rows[hidden] == 0, "a later space-form stamp sorts below the 'T' and stays unseen"
    assert rows[surfaced] == 1, "an earlier T-form stamp sorts above a space-form watermark"


def test_the_callers_own_messages_never_count_as_unread(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    for _ in range(3):
        _plant_message(db, conv_id, user["id"], created_at="2026-05-01T10:00:00")

    assert _row_for(client, headers, conv_id)["unreadCount"] == 0


def test_an_unsent_message_never_counts_as_unread(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    _plant_message(db, conv_id, other["id"], created_at="2026-05-01T10:00:00",
                   deleted_at="2026-05-01T10:05:00")

    assert _row_for(client, headers, conv_id)["unreadCount"] == 0


def test_an_ex_members_messages_still_count_as_unread(client, db, actor, make_user):
    # The count keys off sender_id, not on current membership —
    # what is still readable is still unread
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="Grupė")
    _plant_message(db, conv_id, other["id"], created_at="2026-05-01T10:00:00")
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (conv_id, other["id"]))
    db.commit()

    assert _row_for(client, headers, conv_id)["unreadCount"] == 1


def test_a_room_with_nothing_unread_reports_zero_rather_than_a_missing_key(client, actor,
                                                                           make_user):
    # The GROUP BY only emits rooms with a hit; the shaper
    # supplies the 0
    _, headers = actor
    other = make_user()
    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    assert _row_for(client, headers, conv_id)["unreadCount"] == 0


def test_unread_counts_are_reported_per_room(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    quiet = _plant_conversation(db, [user["id"], other["id"]])
    busy = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="Grupė")
    _plant_message(db, quiet, other["id"], created_at="2026-05-01T10:00:00")
    for hour in range(3):
        _plant_message(db, busy, other["id"], created_at=f"2026-05-01T1{hour}:00:00")

    rows = {row["id"]: row["unreadCount"] for row in _rows(client, headers)}

    assert rows[quiet] == 1 and rows[busy] == 3


def test_the_unread_count_is_private_to_each_member(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  last_read={other["id"]: "2099-01-01T00:00:00"})
    _plant_message(db, conv_id, other["id"], created_at="2026-05-01T10:00:00")
    _plant_message(db, conv_id, user["id"], created_at="2026-05-01T11:00:00")

    assert _row_for(client, headers, conv_id)["unreadCount"] == 1
    assert _row_for(client, auth_headers(other), conv_id)["unreadCount"] == 0


def test_a_null_watermark_counts_the_whole_history(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    for hour in range(5):
        _plant_message(db, conv_id, other["id"], created_at=f"2026-05-01T1{hour}:00:00")

    assert _row_for(client, headers, conv_id)["unreadCount"] == 5




# ===========================================================
# GET /chat/conversations — ordering and lastUpdatedMs
# ===========================================================


def test_pinned_rooms_sort_above_newer_unpinned_ones(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    pinned = _plant_conversation(db, [user["id"], other["id"]],
                                 updated_at="2026-01-01T09:00:00",
                                 pinned_for={user["id"]: 1})
    newer = _plant_conversation(db, [user["id"], other["id"]],
                                updated_at="2026-12-31T09:00:00")

    assert [row["id"] for row in _rows(client, headers)] == [pinned, newer]


def test_a_pinned_flag_of_two_still_reads_as_pinned_and_sorts_first(client, db, actor, make_user):
    # ORDER BY pinned DESC and bool(pinned) both cope with a
    # non-boolean integer in the column
    user, headers = actor
    other = make_user()
    odd = _plant_conversation(db, [user["id"], other["id"]],
                              updated_at="2026-01-01T09:00:00", pinned_for={user["id"]: 2})
    ordinary = _plant_conversation(db, [user["id"], other["id"]],
                                   updated_at="2026-06-01T09:00:00",
                                   pinned_for={user["id"]: 1})

    rows = _rows(client, headers)

    assert [row["id"] for row in rows] == [odd, ordinary]
    assert rows[0]["pinned"] is True


def test_an_unpinned_row_reports_pinned_false(client, actor, make_user):
    _, headers = actor
    other = make_user()
    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    assert _row_for(client, headers, conv_id)["pinned"] is False


def test_last_updated_ms_is_epoch_milliseconds_of_the_naive_utc_stamp(client, db, actor,
                                                                      make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  updated_at="2026-01-01T10:00:00")

    expected = int(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _row_for(client, headers, conv_id)["lastUpdatedMs"] == expected


def test_microseconds_in_the_stamp_truncate_to_milliseconds(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    plain = _plant_conversation(db, [user["id"], other["id"]],
                                updated_at="2026-01-01T10:00:00.000000")
    precise = _plant_conversation(db, [user["id"], other["id"]],
                                  updated_at="2026-01-01T10:00:00.123999")

    rows = {row["id"]: row["lastUpdatedMs"] for row in _rows(client, headers)}

    assert rows[precise] - rows[plain] == 123


def test_an_offset_in_the_stamp_is_overwritten_with_utc(client, db, actor, make_user):
    # _epoch_ms pins the parsed value to timezone.utc, so a
    # stamp that carries +03:00 is read as if it were UTC — the
    # column is contracted to be naive UTC
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  updated_at="2026-01-01T10:00:00+03:00")

    expected = int(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _row_for(client, headers, conv_id)["lastUpdatedMs"] == expected


def test_an_unparseable_updated_at_falls_back_to_created_at(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  created_at="2026-02-02T08:00:00", updated_at="netikras")

    expected = int(datetime(2026, 2, 2, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _row_for(client, headers, conv_id)["lastUpdatedMs"] == expected


def test_two_unparseable_stamps_report_zero_instead_of_failing_the_tab(client, db, actor,
                                                                       make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  created_at="vakar", updated_at="užvakar")

    assert _row_for(client, headers, conv_id)["lastUpdatedMs"] == 0


def test_an_epoch_zero_updated_at_falls_back_to_created_at(client, db, actor, make_user):
    # `_epoch_ms(updated_at) or _epoch_ms(created_at)` is an OR
    # on the VALUE, so a stamp that really is the epoch reads as
    # "unparseable" and created_at answers instead
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  created_at="2026-02-02T08:00:00", updated_at=EPOCH_FLOOR)

    expected = int(datetime(2026, 2, 2, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert _row_for(client, headers, conv_id)["lastUpdatedMs"] == expected


def test_a_pre_epoch_stamp_reports_a_negative_millisecond_count(client, db, actor, make_user):
    # Only an exact zero falls through the OR — a negative one
    # is truthy and used as it is
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]],
                                  created_at="2026-02-02T08:00:00",
                                  updated_at="1960-01-01T00:00:00")

    assert _row_for(client, headers, conv_id)["lastUpdatedMs"] < 0


def test_a_freshly_created_room_reports_a_recent_last_updated_ms(client, actor, make_user):
    _, headers = actor
    other = make_user()
    before = int(datetime.now(timezone.utc).timestamp() * 1000)

    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    stamp = _row_for(client, headers, conv_id)["lastUpdatedMs"]
    assert before - 5000 <= stamp <= before + 60000


def test_a_hundred_and_twenty_rooms_all_ship_in_one_page(client, db, actor, make_user):
    # No paging, no cap: the whole tab arrives at once, newest
    # first, and the IN (...) lists carry every id
    user, headers = actor
    other = make_user()
    planted = [
        _plant_conversation(db, [user["id"], other["id"]],
                            updated_at=f"2026-01-01T{n // 60:02d}:{n % 60:02d}:00")
        for n in range(120)
    ]

    rows = _rows(client, headers)

    assert len(rows) == 120
    assert [row["id"] for row in rows] == list(reversed(planted))


def test_the_list_never_shows_a_room_the_caller_left(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="group", title="Grupė")
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (conv_id, user["id"]))
    db.commit()

    assert _rows(client, headers) == []
    assert [row["id"] for row in _rows(client, auth_headers(other))] == [conv_id]


def test_a_created_room_shows_up_in_the_creators_list_immediately(client, actor, make_user,
                                                                  auth_headers):
    # The end-to-end pin across both routes: what create wrote,
    # list reads back for BOTH members
    user, headers = actor
    other = make_user()
    conv_id = _create_direct(client, headers, other["id"]).get_json()["conversationId"]

    mine = _row_for(client, headers, conv_id)
    theirs = _row_for(client, auth_headers(other), conv_id)

    assert mine["title"] == other["username"].title()
    assert theirs["title"] == user["username"].title()
    assert mine["unreadCount"] == theirs["unreadCount"] == 0
    assert "lastMessage" not in mine
