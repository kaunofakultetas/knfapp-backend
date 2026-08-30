# -----------------------------------------------------------
#  [*] Tests — chat reactions, in-room search and mark-read
#
#  What this module proves about app/chat/routes.py:
#
#    - React / unreact check MEMBERSHIP before the message
#      even exists. An outsider gets the same 403 whether the
#      id names a real message or nothing at all, and the 403
#      body carries no `reactions` — so the route can never be
#      used to learn WHO reacted to a message the caller
#      cannot see.
#    - One emoji per user per message: a second react replaces
#      the first instead of accumulating, and the six allowed
#      emoji are the mobile picker's set byte for byte (the
#      heart keeps its VS-16 selector).
#    - The react/unreact wire shape is ApiReactionGroup —
#      {emoji, count, byUserIds} and NO bySelf; bySelf exists
#      only on the single-identity get_messages shape.
#    - In-room search: the q and limit guards, the 1..50 clamp,
#      the chronological order, the saturating `total`, and —
#      on the escaped-LIKE fallback a build without FTS5 takes
#      — that a query of "%" or "_" matches LITERALLY instead
#      of matching the whole room.
#    - mark_read (REST) writes both read stores, honours the
#      500-receipt cap and the `<= now` bound, and spends the
#      SAME 10-per-10 s budget as its socket twin, so the REST
#      path is not a free way around the socket quota.
#
#  Every clock-dependent assertion runs under time_machine;
#  nothing here sleeps and nothing reaches the network.
# -----------------------------------------------------------

import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import time_machine

# The six the mobile picker offers (REACTION_OPTIONS in
# hooks/chat/useChatReactions.ts). Spelled with escapes so a
# reviewer can see the heart is U+2764 PLUS U+FE0F — the bare
# heart is a different string and the server rejects it
THUMBS = "\U0001F44D"
HEART = "\u2764\uFE0F"
JOY = "\U0001F602"
WOW = "\U0001F62E"
SAD = "\U0001F622"
ANGRY = "\U0001F621"
ALL_REACTIONS = (THUMBS, HEART, JOY, WOW, SAD, ANGRY)

# Every seeded stamp hangs off this base, comfortably in the
# past, so a route's own datetime.now() always sorts after it
# whatever day the suite runs
_BASE = datetime(2020, 3, 1, 10, 0, 0)

# The route's own bounds, restated here so a test that trips a
# boundary reads as a boundary test
_MARK_READ_CAP = 500
_SEARCH_Q_MAX = 200
_SEARCH_TOTAL_CAP = 500
_SOCKET_MARK_READ_BUDGET = 10




# -----------------------------------------------------------
# _stamp
# -----------------------------------------------------------
#
# A naive-UTC isoformat stamp `offset` seconds after _BASE —
# the exact string shape chat/routes.py writes and compares as
# text. Ordering seeded rows is then just ordering integers.
# -----------------------------------------------------------

def _stamp(offset=0):
    return (_BASE + timedelta(seconds=offset)).isoformat()




# -----------------------------------------------------------
# _seed_room
# -----------------------------------------------------------
#
# A conversation plus its membership rows, written straight to
# the database: POST /conversations cannot make a group of
# arbitrary size, cannot set last_read_at and spends a rate
# budget these tests need for something else.
# -----------------------------------------------------------

def _seed_room(db, member_ids, conv_type="group", title="Testų kambarys", last_read_at=None):
    conv_id = str(uuid.uuid4())
    created = _stamp(0)

    db.execute(
        "INSERT INTO conversations (id, type, title, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (conv_id, conv_type, title, member_ids[0], created, created),
    )
    db.executemany(
        "INSERT INTO conversation_participants (conversation_id, user_id, last_read_at)"
        " VALUES (?, ?, ?)",
        [(conv_id, uid, last_read_at) for uid in member_ids],
    )
    db.commit()

    return conv_id




# -----------------------------------------------------------
# _seed_message
# -----------------------------------------------------------
#
# One message row. `created_at` takes a raw string so a test
# can plant a future stamp or an unparseable one; `deleted`
# soft-deletes it the way delete_message would.
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
# cap tests need 500+ rows and a commit per row would make
# them minutes long. Returns the ids oldest-first.
# -----------------------------------------------------------

def _seed_many(db, conv_id, sender_id, count, text="labas", first_offset=1):
    rows = []
    for i in range(count):
        rows.append((str(uuid.uuid4()), conv_id, sender_id, f"{text} {i}", _stamp(first_offset + i)))

    db.executemany(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()

    return [r[0] for r in rows]




# -----------------------------------------------------------
# _disable_fts
# -----------------------------------------------------------
#
# Drops the v20 messages_fts shadow table and its sync
# triggers, so search_messages' MATCH raises OperationalError
# and the route falls back to the escaped-LIKE substring scan
# — the path an SQLite build without FTS5 (or a database file
# older than v20) really takes in production, and the only one
# where the % / _ escaping is observable. Seed the messages
# BEFORE calling this: the triggers go with the table.
# -----------------------------------------------------------

def _disable_fts(db):
    for trigger in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    db.execute("DROP TABLE IF EXISTS messages_fts")
    db.commit()




# -----------------------------------------------------------
# URL builders — the three routes under test
# -----------------------------------------------------------

def _react_url(conv_id, msg_id):
    return f"/api/chat/conversations/{conv_id}/messages/{msg_id}/react"


def _search_url(conv_id):
    return f"/api/chat/conversations/{conv_id}/messages/search"


def _read_url(conv_id):
    return f"/api/chat/conversations/{conv_id}/read"




# -----------------------------------------------------------
# fresh_limiter
# -----------------------------------------------------------
#
# The app's in-memory rate limiter (auth/routes.py
# _rate_limit_store) is PROCESS state, not database state, so
# the fresh-database fixtures do not reset it: without this
# the budget tests below would leave create_app's global
# 600-per-IP throttle spent for every later test in the run,
# and the 429s would land on innocent modules. Cleared on both
# sides so this file neither inherits nor exports a spent
# budget.
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
# room
# -----------------------------------------------------------
#
# The standing cast for most tests: a two-person room, one
# message in it from bob, and an outsider who belongs to
# nothing. Headers come from the shared auth_headers fixture,
# so every token is minted by the real login route.
# -----------------------------------------------------------

@pytest.fixture
def room(db, make_user, auth_headers):
    alice = make_user()
    bob = make_user()
    outsider = make_user()

    conv_id = _seed_room(db, [alice["id"], bob["id"]])
    msg_id = _seed_message(db, conv_id, bob["id"], "Sveiki, kaip sekasi?")

    return SimpleNamespace(
        alice=alice, alice_h=auth_headers(alice),
        bob=bob, bob_h=auth_headers(bob),
        outsider=outsider, outsider_h=auth_headers(outsider),
        conv=conv_id, msg=msg_id,
    )




# ===========================================================
#  react_to_message — POST .../react
# ===========================================================


def test_reacting_to_a_message_returns_the_authoritative_list(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["emoji"] == THUMBS
    assert body["reactions"] == [
        {"emoji": THUMBS, "count": 1, "byUserIds": [room.alice["id"]]}
    ]


def test_a_reaction_is_persisted(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": HEART})

    rows = db.execute(
        "SELECT user_id, emoji FROM message_reactions WHERE message_id = ?", (room.msg,)
    ).fetchall()
    assert [(r["user_id"], r["emoji"]) for r in rows] == [(room.alice["id"], HEART)]


@pytest.mark.contract
def test_the_reaction_group_shape_carries_no_by_self(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": JOY})

    group = response.get_json()["reactions"][0]
    # mobile ApiReactionGroup is exactly these three keys and
    # derives bySelf from byUserIds — a stray bySelf here would
    # be a second, contradictory source of truth
    assert set(group.keys()) == {"emoji", "count", "byUserIds"}
    assert "bySelf" not in group


def test_a_second_emoji_from_the_same_user_replaces_the_first(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": ANGRY})

    assert response.status_code == 200
    assert response.get_json()["reactions"] == [
        {"emoji": ANGRY, "count": 1, "byUserIds": [room.alice["id"]]}
    ]
    # one emoji per user per message — the replaced row is gone
    assert db.execute(
        "SELECT COUNT(*) FROM message_reactions WHERE message_id = ? AND user_id = ?",
        (room.msg, room.alice["id"]),
    ).fetchone()[0] == 1


def test_reacting_twice_with_the_same_emoji_does_not_double_the_count(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": SAD})
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": SAD})

    assert response.get_json()["reactions"] == [
        {"emoji": SAD, "count": 1, "byUserIds": [room.alice["id"]]}
    ]


def test_two_users_on_the_same_emoji_share_one_group(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": WOW})
    response = client.post(_react_url(room.conv, room.msg), headers=room.bob_h,
                           json={"emoji": WOW})

    groups = response.get_json()["reactions"]
    assert len(groups) == 1
    assert groups[0]["emoji"] == WOW
    assert groups[0]["count"] == 2
    assert sorted(groups[0]["byUserIds"]) == sorted([room.alice["id"], room.bob["id"]])


def test_two_users_on_different_emoji_get_two_groups(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    response = client.post(_react_url(room.conv, room.msg), headers=room.bob_h,
                           json={"emoji": HEART})

    groups = response.get_json()["reactions"]
    assert {g["emoji"] for g in groups} == {THUMBS, HEART}
    assert all(g["count"] == 1 for g in groups)


def test_a_users_reaction_on_one_message_does_not_leak_into_another(client, db, room):
    other_msg = _seed_message(db, room.conv, room.alice["id"], "kitas", offset=2)

    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    response = client.post(_react_url(room.conv, other_msg), headers=room.alice_h,
                           json={"emoji": JOY})

    assert response.get_json()["reactions"] == [
        {"emoji": JOY, "count": 1, "byUserIds": [room.alice["id"]]}
    ]


@pytest.mark.parametrize("emoji", ALL_REACTIONS)
def test_every_emoji_the_mobile_picker_offers_is_accepted(client, room, emoji):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": emoji})

    assert response.status_code == 200
    assert response.get_json()["emoji"] == emoji


def test_the_bare_heart_without_its_variation_selector_is_refused(client, room):
    # U+2764 alone is a DIFFERENT string from the picker's
    # U+2764 U+FE0F — the allowlist must stay byte-exact
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": "\u2764"})

    assert response.status_code == 400
    assert "supported reactions" in response.get_json()["error"]


def test_an_arbitrary_emoji_is_refused(client, db, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": "\U0001F984"})

    assert response.status_code == 400
    assert db.execute("SELECT COUNT(*) FROM message_reactions").fetchone()[0] == 0


def test_an_arbitrary_long_string_is_refused_as_a_reaction(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": "x" * 32})

    assert response.status_code == 400


def test_reacting_without_a_json_body_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_reacting_with_a_top_level_array_body_is_refused(client, db, room):
    # create_app's validate_json_input hook catches this one
    # before the route ever runs — the point is that a non-dict
    # body cannot reach data.get() and 500
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json=["\U0001F44D"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"
    assert db.execute("SELECT COUNT(*) FROM message_reactions").fetchone()[0] == 0


def test_reacting_with_a_malformed_json_body_is_refused(client, room):
    # malformed JSON parses to None, so this is the route's own
    # `if not data` arm rather than the app-wide hook
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           data="{ neuzdaryta", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_reacting_with_an_empty_emoji_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": ""})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_reacting_with_a_non_string_emoji_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": 42})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji must be a string"


def test_reacting_requires_authentication(client, room):
    response = client.post(_react_url(room.conv, room.msg), json={"emoji": THUMBS})

    assert response.status_code == 401


def test_a_non_member_cannot_react(client, db, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.outsider_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}
    assert db.execute("SELECT COUNT(*) FROM message_reactions").fetchone()[0] == 0


def test_a_non_member_never_learns_who_reacted(client, room):
    # alice's identity is on the message; the outsider's 403
    # must not carry the reactions array that would reveal it
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    response = client.post(_react_url(room.conv, room.msg), headers=room.outsider_h,
                           json={"emoji": HEART})

    assert response.status_code == 403
    body = response.get_json()
    assert "reactions" not in body
    assert room.alice["id"] not in response.get_data(as_text=True)


def test_membership_is_checked_before_the_message_exists(client, room):
    # Same answer for a real message and for a fabricated id —
    # otherwise the pair of statuses is a message-existence
    # oracle for a room the caller cannot see
    real = client.post(_react_url(room.conv, room.msg), headers=room.outsider_h,
                       json={"emoji": THUMBS})
    fake = client.post(_react_url(room.conv, str(uuid.uuid4())), headers=room.outsider_h,
                       json={"emoji": THUMBS})

    assert real.status_code == fake.status_code == 403
    assert real.get_json() == fake.get_json()


def test_reacting_to_an_unknown_message_is_not_found(client, room):
    response = client.post(_react_url(room.conv, str(uuid.uuid4())), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 404
    assert response.get_json()["error"] == "Message not found"


def test_reacting_to_a_message_from_another_room_is_not_found(client, db, room):
    # alice is in BOTH rooms, so only the conversation_id arm
    # of the message lookup can reject this
    other_conv = _seed_room(db, [room.alice["id"], room.bob["id"]])
    other_msg = _seed_message(db, other_conv, room.bob["id"], "kitur")

    response = client.post(_react_url(room.conv, other_msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 404


def test_reacting_to_an_unsent_message_is_not_found(client, db, room):
    unsent = _seed_message(db, room.conv, room.bob["id"], "", offset=3, deleted=True)

    response = client.post(_react_url(room.conv, unsent), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) FROM message_reactions").fetchone()[0] == 0


def test_reacting_to_a_message_in_an_unknown_conversation_is_forbidden(client, room):
    response = client.post(_react_url(str(uuid.uuid4()), room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 403




# ===========================================================
#  remove_reaction — DELETE .../react
# ===========================================================


def test_removing_a_reaction_clears_it(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "reactions": []}
    assert db.execute("SELECT COUNT(*) FROM message_reactions").fetchone()[0] == 0


def test_removing_a_reaction_that_was_never_set_is_a_silent_ok(client, room):
    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 200
    assert response.get_json()["reactions"] == []


def test_removing_only_clears_the_callers_own_reaction(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": THUMBS})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.get_json()["reactions"] == [
        {"emoji": THUMBS, "count": 1, "byUserIds": [room.bob["id"]]}
    ]


@pytest.mark.contract
def test_the_unreact_list_also_carries_no_by_self(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": HEART})
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": HEART})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    group = response.get_json()["reactions"][0]
    assert set(group.keys()) == {"emoji", "count", "byUserIds"}


def test_removing_a_reaction_from_an_unsent_message_still_works(client, db, room):
    # react refuses a placeholder bubble, unreact must not:
    # a chip set before the unsend still has to be clearable
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    db.execute("UPDATE messages SET deleted_at = ? WHERE id = ?", (_stamp(9), room.msg))
    db.commit()

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 200
    assert response.get_json()["reactions"] == []


def test_removing_a_reaction_requires_authentication(client, room):
    response = client.delete(_react_url(room.conv, room.msg))

    assert response.status_code == 401


def test_a_non_member_cannot_remove_a_reaction(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.outsider_h)

    assert response.status_code == 403
    assert db.execute("SELECT COUNT(*) FROM message_reactions").fetchone()[0] == 1


def test_a_non_member_unreacting_never_learns_who_reacted(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.outsider_h)

    assert response.status_code == 403
    assert "reactions" not in response.get_json()
    assert room.alice["id"] not in response.get_data(as_text=True)


def test_unreact_membership_is_checked_before_the_message_exists(client, room):
    real = client.delete(_react_url(room.conv, room.msg), headers=room.outsider_h)
    fake = client.delete(_react_url(room.conv, str(uuid.uuid4())), headers=room.outsider_h)

    assert real.status_code == fake.status_code == 403
    assert real.get_json() == fake.get_json()


def test_removing_a_reaction_from_an_unknown_message_is_not_found(client, room):
    response = client.delete(_react_url(room.conv, str(uuid.uuid4())), headers=room.alice_h)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Message not found"


def test_removing_a_reaction_from_another_rooms_message_is_not_found(client, db, room):
    other_conv = _seed_room(db, [room.alice["id"], room.bob["id"]])
    other_msg = _seed_message(db, other_conv, room.bob["id"], "kitur")

    response = client.delete(_react_url(room.conv, other_msg), headers=room.alice_h)

    assert response.status_code == 404




# ===========================================================
#  bySelf — the OTHER reaction shape, on get_messages
# ===========================================================


@pytest.mark.contract
def test_get_messages_reactions_carry_by_self_for_the_caller(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": THUMBS})
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": HEART})

    response = client.get(f"/api/chat/conversations/{room.conv}/messages", headers=room.alice_h)

    groups = {g["emoji"]: g for g in response.get_json()["messages"][0]["reactions"]}
    assert groups[HEART]["bySelf"] is True
    assert groups[THUMBS]["bySelf"] is False
    assert set(groups[HEART].keys()) == {"emoji", "count", "bySelf", "byUserIds"}


def test_a_message_without_reactions_reports_an_empty_list(client, room):
    response = client.get(f"/api/chat/conversations/{room.conv}/messages", headers=room.alice_h)

    assert response.get_json()["messages"][0]["reactions"] == []


def test_reading_a_page_of_messages_needs_membership(client, room):
    response = client.get(f"/api/chat/conversations/{room.conv}/messages",
                          headers=room.outsider_h)

    assert response.status_code == 403


def test_a_non_numeric_page_limit_is_refused(client, room):
    response = client.get(f"/api/chat/conversations/{room.conv}/messages",
                          headers=room.alice_h, query_string={"limit": "daug"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be an integer"


def test_the_before_cursor_returns_only_older_messages(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "antra", offset=2)
    _seed_message(db, room.conv, room.bob["id"], "trecia", offset=3)

    body = client.get(f"/api/chat/conversations/{room.conv}/messages",
                      headers=room.alice_h,
                      query_string={"before": _stamp(3)}).get_json()

    assert [m["text"] for m in body["messages"]] == ["Sveiki, kaip sekasi?", "antra"]


def test_an_own_message_nobody_read_is_only_sent(client, room):
    own = client.get(f"/api/chat/conversations/{room.conv}/messages",
                     headers=room.bob_h).get_json()["messages"][0]

    assert own["isOwn"] is True
    assert own["status"] == "sent"
    assert own["readBy"] == []


def test_an_own_message_some_members_read_is_delivered(client, db, make_user, auth_headers):
    writer = make_user()
    first = make_user()
    second = make_user()
    conv_id = _seed_room(db, [writer["id"], first["id"], second["id"]])
    _seed_message(db, conv_id, writer["id"], "visiems")

    client.put(_read_url(conv_id), headers=auth_headers(first))

    own = client.get(f"/api/chat/conversations/{conv_id}/messages",
                     headers=auth_headers(writer)).get_json()["messages"][0]
    # one of the two others has a receipt — not read yet
    assert own["status"] == "delivered"


def test_a_reply_carries_the_quoted_message(client, db, room):
    quoted = _seed_message(db, room.conv, room.bob["id"], "originalas", offset=2)
    reply_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, reply_to_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (reply_id, room.conv, room.alice["id"], "atsakymas", quoted, _stamp(3)),
    )
    db.commit()

    reply = client.get(f"/api/chat/conversations/{room.conv}/messages",
                       headers=room.alice_h).get_json()["messages"][-1]

    assert reply["replyTo"] == {
        "id": quoted,
        "senderId": room.bob["id"],
        "senderName": room.bob["username"].title(),
        "text": "originalas",
        "imageUrl": None,
        "deleted": False,
    }


def test_a_dangling_reply_quote_is_shaped_as_deleted(client, db, room):
    # the quoted row vanished under an FK-off write, so the
    # LEFT JOIN misses — the client must get a placeholder,
    # never a live quote with a null sender
    reply_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, reply_to_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (reply_id, room.conv, room.alice["id"], "atsakymas i niekur",
         "nera-tokios-zinutes", _stamp(3)),
    )
    db.commit()

    reply = client.get(f"/api/chat/conversations/{room.conv}/messages",
                       headers=room.alice_h).get_json()["messages"][-1]

    assert reply["replyTo"] == {
        "id": "nera-tokios-zinutes",
        "senderId": None,
        "senderName": None,
        "text": "",
        "imageUrl": None,
        "deleted": True,
    }


def test_an_empty_room_needs_no_reaction_lookup(client, db, make_user, auth_headers):
    # the batch shaper's empty-id short circuit: no page, no
    # IN () query, no crash
    reader = make_user()
    conv_id = _seed_room(db, [reader["id"]])

    response = client.get(f"/api/chat/conversations/{conv_id}/messages",
                          headers=auth_headers(reader))

    assert response.status_code == 200
    assert response.get_json()["messages"] == []




# ===========================================================
#  Reaction rate budget — shared by react and unreact
# ===========================================================


@pytest.mark.slow
def test_react_and_unreact_spend_one_shared_budget(client, room):
    # 300 writes per 5 min per user, whichever verb spends them
    for _ in range(150):
        assert client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS}).status_code == 200
        assert client.delete(_react_url(room.conv, room.msg),
                             headers=room.alice_h).status_code == 200

    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1
    # the other verb is out of budget too — one bucket, not two
    assert client.delete(_react_url(room.conv, room.msg),
                         headers=room.alice_h).status_code == 429


def test_one_users_reaction_budget_does_not_bind_another(client, room):
    for _ in range(5):
        client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    assert client.post(_react_url(room.conv, room.msg), headers=room.bob_h,
                       json={"emoji": THUMBS}).status_code == 200




# ===========================================================
#  search_messages — GET .../messages/search
# ===========================================================


def test_search_finds_a_message_by_a_word(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "Rytoj egzaminas auditorijoje", offset=5)

    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "egzaminas"})

    assert response.status_code == 200
    body = response.get_json()
    assert [m["text"] for m in body["messages"]] == ["Rytoj egzaminas auditorijoje"]
    assert body["total"] == 1


@pytest.mark.contract
def test_a_search_hit_carries_the_fields_the_app_renders(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "Paskaita persikelia", offset=5)

    hit = client.get(_search_url(room.conv), headers=room.alice_h,
                     query_string={"q": "paskaita"}).get_json()["messages"][0]

    assert set(hit.keys()) == {
        "id", "conversationId", "senderId", "senderName", "senderAvatar",
        "text", "imageUrl", "time", "createdAt", "isOwn",
    }
    assert hit["conversationId"] == room.conv
    assert hit["senderId"] == room.bob["id"]
    assert hit["isOwn"] is False
    # `time` is UTC HH:MM off the naive stamp, never local time
    assert hit["time"] == "10:00"
    assert hit["createdAt"] == _stamp(5)


def test_a_search_hit_on_the_callers_own_message_is_flagged_own(client, db, room):
    _seed_message(db, room.conv, room.alice["id"], "Mano zinute", offset=5)

    hit = client.get(_search_url(room.conv), headers=room.alice_h,
                     query_string={"q": "zinute"}).get_json()["messages"][0]

    assert hit["isOwn"] is True


def test_a_search_hit_with_an_unparseable_stamp_gets_a_blank_time(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "sugadinta", created_at="ne data")

    hit = client.get(_search_url(room.conv), headers=room.alice_h,
                     query_string={"q": "sugadinta"}).get_json()["messages"][0]

    assert hit["time"] == ""
    assert hit["createdAt"] == "ne data"


def test_search_results_come_back_in_chronological_order(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "labas pirmas", offset=10)
    _seed_message(db, room.conv, room.bob["id"], "labas antras", offset=20)
    _seed_message(db, room.conv, room.bob["id"], "labas trecias", offset=30)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas"}).get_json()

    assert [m["text"] for m in body["messages"]] == [
        "labas pirmas", "labas antras", "labas trecias",
    ]


def test_search_returns_the_newest_hits_when_the_limit_bites(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 5, text="rastas", first_offset=10)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": 2}).get_json()

    # newest two, then reversed into chronological order
    assert [m["text"] for m in body["messages"]] == ["rastas 3", "rastas 4"]
    assert body["total"] == 5


def test_search_skips_unsent_messages(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "istrinta", offset=6, deleted=True)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "istrinta"}).get_json()

    assert body["messages"] == []
    assert body["total"] == 0


def test_search_never_reaches_into_another_conversation(client, db, room):
    other_conv = _seed_room(db, [room.alice["id"], room.bob["id"]])
    _seed_message(db, other_conv, room.bob["id"], "slaptas obuolys", offset=7)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "obuolys"}).get_json()

    assert body["messages"] == []
    assert body["total"] == 0


def test_search_requires_authentication(client, room):
    response = client.get(_search_url(room.conv), query_string={"q": "labas"})

    assert response.status_code == 401


def test_a_non_member_cannot_search_a_room(client, room):
    response = client.get(_search_url(room.conv), headers=room.outsider_h,
                          query_string={"q": "kaip"})

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}


def test_searching_an_unknown_conversation_is_forbidden(client, room):
    response = client.get(_search_url(str(uuid.uuid4())), headers=room.alice_h,
                          query_string={"q": "labas"})

    assert response.status_code == 403


def test_search_without_q_is_refused(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h)

    assert response.status_code == 400
    assert "q parameter is required" in response.get_json()["error"]


def test_search_with_a_whitespace_only_q_is_refused(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "   "})

    assert response.status_code == 400


def test_a_q_at_the_length_limit_is_accepted(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "a" * _SEARCH_Q_MAX})

    assert response.status_code == 200
    assert response.get_json()["messages"] == []


def test_a_q_one_character_past_the_limit_is_refused(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "a" * (_SEARCH_Q_MAX + 1)})

    assert response.status_code == 400
    assert str(_SEARCH_Q_MAX) in response.get_json()["error"]


def test_a_long_q_is_measured_after_stripping(client, room):
    # 200 real characters wrapped in whitespace is still a
    # legal needle — the strip runs before the length check
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "  " + "a" * _SEARCH_Q_MAX + "  "})

    assert response.status_code == 200


def test_a_non_numeric_limit_is_refused(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas", "limit": "daug"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be an integer"


def test_an_empty_limit_is_refused(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas", "limit": ""})

    assert response.status_code == 400


def test_the_membership_gate_runs_before_the_limit_is_parsed(client, room):
    # an outsider must not be able to tell a bad limit from a
    # good one — the 403 has to win
    response = client.get(_search_url(room.conv), headers=room.outsider_h,
                          query_string={"q": "labas", "limit": "daug"})

    assert response.status_code == 403


def test_the_limit_is_clamped_to_the_ceiling(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 55, text="rastas", first_offset=10)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": 999}).get_json()

    assert len(body["messages"]) == 50
    assert body["total"] == 55


def test_a_limit_of_exactly_fifty_is_honoured(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 55, text="rastas", first_offset=10)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": 50}).get_json()

    assert len(body["messages"]) == 50


@pytest.mark.parametrize("limit", [0, -1, -999])
def test_a_non_positive_limit_is_clamped_to_one(client, db, room, limit):
    # a negative reaching SQLite as LIMIT -n means "no limit" —
    # the clamp is what stops a whole room coming back
    _seed_many(db, room.conv, room.bob["id"], 5, text="rastas", first_offset=10)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": limit}).get_json()

    assert len(body["messages"]) == 1


@pytest.mark.slow
def test_the_search_total_saturates_at_the_cap(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], _SEARCH_TOTAL_CAP + 5, text="rastas")

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas"}).get_json()

    # "that many or more" — never the true 505
    assert body["total"] == _SEARCH_TOTAL_CAP
    assert len(body["messages"]) == 20


@pytest.mark.slow
def test_search_spends_a_hundred_calls_per_window(client, room):
    for _ in range(100):
        assert client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas"}).status_code == 200

    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas"})

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_the_search_budget_is_spent_even_by_a_rejected_query(client, room):
    # the decorator runs before the q guard, so a script
    # hammering blank searches still exhausts its own budget
    for _ in range(100):
        client.get(_search_url(room.conv), headers=room.alice_h, query_string={"q": ""})

    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas"})

    assert response.status_code == 429




# ===========================================================
#  search_messages — the escaped-LIKE fallback
#
#  Reached when messages_fts is missing (a build without FTS5
#  or a database file older than migration v20). This is the
#  only arm where q's LIKE metacharacters are observable, and
#  the arm that must treat them as literal text.
# ===========================================================


def test_a_percent_query_matches_literally_on_the_like_fallback(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "nuolaida 50% studentams", offset=10)
    _seed_message(db, room.conv, room.bob["id"], "jokio zenklo cia nera", offset=11)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "%"}).get_json()

    # an unescaped % would be the LIKE wildcard and match BOTH
    assert [m["text"] for m in body["messages"]] == ["nuolaida 50% studentams"]
    assert body["total"] == 1


def test_an_underscore_query_matches_literally_on_the_like_fallback(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "failas snake_case.txt", offset=10)
    _seed_message(db, room.conv, room.bob["id"], "abc", offset=11)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "_"}).get_json()

    # an unescaped _ matches any single character, so "abc"
    # would come back too
    assert [m["text"] for m in body["messages"]] == ["failas snake_case.txt"]
    assert body["total"] == 1


def test_a_backslash_query_matches_literally_on_the_like_fallback(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "kelias C:\\temp\\failas", offset=10)
    _seed_message(db, room.conv, room.bob["id"], "be jokio bruksnio", offset=11)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "\\"}).get_json()

    assert [m["text"] for m in body["messages"]] == ["kelias C:\\temp\\failas"]


def test_a_mixed_wildcard_query_matches_literally_on_the_like_fallback(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "kodas a_b%c yra cia", offset=10)
    _seed_message(db, room.conv, room.bob["id"], "axbyc taip pat", offset=11)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "a_b%c"}).get_json()

    assert [m["text"] for m in body["messages"]] == ["kodas a_b%c yra cia"]


def test_the_like_fallback_finds_a_plain_substring(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "Rytoj egzaminas", offset=10)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "gzamin"}).get_json()

    # substring, not token-prefix — this is the fallback's own
    # semantics and the reason it is worth keeping
    assert [m["text"] for m in body["messages"]] == ["Rytoj egzaminas"]


def test_the_like_fallback_still_excludes_unsent_and_foreign_rooms(client, db, room):
    other_conv = _seed_room(db, [room.alice["id"], room.bob["id"]])
    _seed_message(db, other_conv, room.bob["id"], "obuolys kitur", offset=10)
    _seed_message(db, room.conv, room.bob["id"], "obuolys istrintas", offset=11, deleted=True)
    kept = _seed_message(db, room.conv, room.bob["id"], "obuolys liko", offset=12)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "obuolys"}).get_json()

    assert [m["id"] for m in body["messages"]] == [kept]
    assert body["total"] == 1


@pytest.mark.slow
def test_the_like_fallback_total_also_saturates(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], _SEARCH_TOTAL_CAP + 5, text="rastas")
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas"}).get_json()

    assert body["total"] == _SEARCH_TOTAL_CAP




# ===========================================================
#  search_users — the OTHER _escape_like call site
#
#  The people picker builds its LIKE from the same helper, so
#  the literal-metacharacter contract has to hold here too.
#  Only that contract and its guards are pinned here.
# ===========================================================


def test_the_people_search_escapes_an_underscore(client, make_user, actor):
    _, headers = actor
    make_user(username="ona_petraite", display_name="Ona Petraite")
    # the control: "a P" is 'a', any character, 'p' — an
    # unescaped _ wildcard would drag this one in too
    make_user(username="jonas", display_name="Kava Puode")

    body = client.get("/api/chat/users/search", headers=headers,
                      query_string={"q": "a_p"}).get_json()

    assert [u["username"] for u in body["users"]] == ["ona_petraite"]


def test_the_people_search_escapes_a_percent_sign(client, make_user, actor):
    _, headers = actor
    make_user(username="nuolaida", display_name="100% Studentas")
    # the control carries a 0 but no "0%" — only an unescaped
    # % (LIKE's match-anything) would return it
    make_user(username="kitas", display_name="Kitas 0 Zmogus")

    body = client.get("/api/chat/users/search", headers=headers,
                      query_string={"q": "0%"}).get_json()

    assert [u["username"] for u in body["users"]] == ["nuolaida"]


def test_a_one_character_people_query_answers_empty(client, make_user, actor):
    _, headers = actor
    make_user(username="ona")

    response = client.get("/api/chat/users/search", headers=headers,
                          query_string={"q": "o"})

    # the keystroke warm-up never touches the directory
    assert response.status_code == 200
    assert response.get_json()["users"] == []


def test_the_people_search_hides_the_caller_and_deactivated_accounts(
        client, make_user, auth_headers):
    me = make_user(username="testuotojas_a")
    make_user(username="testuotojas_b", active=0)
    alive = make_user(username="testuotojas_c")

    body = client.get("/api/chat/users/search", headers=auth_headers(me),
                      query_string={"q": "testuotojas"}).get_json()

    assert [u["id"] for u in body["users"]] == [alive["id"]]


@pytest.mark.contract
def test_a_people_search_hit_carries_no_email(client, make_user, actor):
    _, headers = actor
    make_user(username="rasta_paskyra", display_name="Rasta Paskyra")

    hit = client.get("/api/chat/users/search", headers=headers,
                     query_string={"q": "rasta"}).get_json()["users"][0]

    assert set(hit.keys()) == {"id", "username", "displayName", "avatarUrl", "role"}


def test_an_exact_username_outranks_a_mere_substring(client, make_user, actor):
    _, headers = actor
    make_user(username="aaa_ona", display_name="Aaa Ona")
    exact = make_user(username="ona", display_name="Zzz Ona")

    body = client.get("/api/chat/users/search", headers=headers,
                      query_string={"q": "ona"}).get_json()

    # ranked, not scan order: the exact username wins even
    # though its display name sorts last
    assert body["users"][0]["id"] == exact["id"]




# ===========================================================
#  mark_read — PUT .../read
# ===========================================================


def test_marking_read_writes_a_receipt_for_every_foreign_message(client, db, room):
    second = _seed_message(db, room.conv, room.bob["id"], "antra", offset=2)

    response = client.put(_read_url(room.conv), headers=room.alice_h)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "readCount": 2}
    receipts = db.execute(
        "SELECT message_id FROM message_reads WHERE user_id = ?", (room.alice["id"],)
    ).fetchall()
    assert {r["message_id"] for r in receipts} == {room.msg, second}


def test_marking_read_moves_the_unread_watermark(client, db, room):
    client.put(_read_url(room.conv), headers=room.alice_h)

    watermark = db.execute(
        "SELECT last_read_at FROM conversation_participants"
        " WHERE conversation_id = ? AND user_id = ?",
        (room.conv, room.alice["id"]),
    ).fetchone()["last_read_at"]
    assert watermark is not None
    assert client.get("/api/chat/unread-count",
                      headers=room.alice_h).get_json()["unreadCount"] == 0


def test_marking_read_twice_reports_nothing_new_the_second_time(client, room):
    first = client.put(_read_url(room.conv), headers=room.alice_h).get_json()
    second = client.put(_read_url(room.conv), headers=room.alice_h).get_json()

    assert first["readCount"] == 1
    assert second == {"ok": True, "readCount": 0}


def test_marking_read_never_receipts_the_callers_own_messages(client, db, room):
    _seed_message(db, room.conv, room.alice["id"], "mano", offset=2)

    response = client.put(_read_url(room.conv), headers=room.alice_h)

    assert response.get_json()["readCount"] == 1
    senders = db.execute(
        "SELECT DISTINCT m.sender_id FROM message_reads mr"
        " JOIN messages m ON m.id = mr.message_id WHERE mr.user_id = ?",
        (room.alice["id"],),
    ).fetchall()
    assert [s["sender_id"] for s in senders] == [room.bob["id"]]


def test_marking_read_ignores_messages_older_than_the_watermark(client, db, make_user, auth_headers):
    reader = make_user()
    writer = make_user()
    conv_id = _seed_room(db, [reader["id"], writer["id"]], last_read_at=_stamp(100))
    _seed_message(db, conv_id, writer["id"], "sena", offset=50)
    fresh = _seed_message(db, conv_id, writer["id"], "nauja", offset=150)

    response = client.put(_read_url(conv_id), headers=auth_headers(reader))

    assert response.get_json()["readCount"] == 1
    receipts = db.execute(
        "SELECT message_id FROM message_reads WHERE user_id = ?", (reader["id"],)
    ).fetchall()
    assert [r["message_id"] for r in receipts] == [fresh]


def test_a_null_watermark_receipts_the_whole_history(client, db, make_user, auth_headers):
    reader = make_user()
    writer = make_user()
    conv_id = _seed_room(db, [reader["id"], writer["id"]], last_read_at=None)
    _seed_many(db, conv_id, writer["id"], 3, text="sena", first_offset=1)

    response = client.put(_read_url(conv_id), headers=auth_headers(reader))

    assert response.get_json()["readCount"] == 3


def test_marking_read_ignores_a_message_stamped_after_the_call(client, db, room):
    # the `<= now` bound: a row that lands mid-call must stay
    # out of BOTH read stores, or its sender's bubble would
    # flip to read before the reader ever saw it
    _seed_message(db, room.conv, room.bob["id"], "is ateities", created_at="2099-01-01T00:00:00")

    response = client.put(_read_url(room.conv), headers=room.alice_h)

    assert response.get_json()["readCount"] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM message_reads WHERE user_id = ?", (room.alice["id"],)
    ).fetchone()[0] == 1


def test_marking_read_requires_authentication(client, room):
    response = client.put(_read_url(room.conv))

    assert response.status_code == 401


def test_a_non_member_cannot_mark_a_room_read(client, db, room):
    response = client.put(_read_url(room.conv), headers=room.outsider_h)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}
    assert db.execute("SELECT COUNT(*) FROM message_reads").fetchone()[0] == 0


def test_marking_an_unknown_conversation_read_is_forbidden(client, room):
    response = client.put(_read_url(str(uuid.uuid4())), headers=room.alice_h)

    assert response.status_code == 403


def test_marking_read_flips_the_senders_bubble_to_read(client, room):
    client.put(_read_url(room.conv), headers=room.alice_h)

    own = client.get(f"/api/chat/conversations/{room.conv}/messages",
                     headers=room.bob_h).get_json()["messages"][0]

    assert own["isOwn"] is True
    assert own["status"] == "read"
    assert room.alice["id"] in own["readBy"]


@pytest.mark.slow
def test_one_call_receipts_at_most_the_cap(client, db, make_user, auth_headers):
    reader = make_user()
    writer = make_user()
    conv_id = _seed_room(db, [reader["id"], writer["id"]])
    ids = _seed_many(db, conv_id, writer["id"], _MARK_READ_CAP + 5)

    response = client.put(_read_url(conv_id), headers=auth_headers(reader))

    assert response.get_json()["readCount"] == _MARK_READ_CAP
    # the cap takes the NEWEST rows — the oldest five are the
    # ones left without a receipt
    receipted = {r["message_id"] for r in db.execute(
        "SELECT message_id FROM message_reads WHERE user_id = ?", (reader["id"],)
    ).fetchall()}
    assert receipted == set(ids[5:])


@pytest.mark.slow
def test_the_watermark_still_moves_past_the_capped_receipts(client, db, make_user, auth_headers):
    reader = make_user()
    writer = make_user()
    conv_id = _seed_room(db, [reader["id"], writer["id"]])
    _seed_many(db, conv_id, writer["id"], _MARK_READ_CAP + 5)

    client.put(_read_url(conv_id), headers=auth_headers(reader))

    # the badge clears even though five ancient rows never got
    # a receipt — the two stores are deliberately independent
    assert client.get("/api/chat/unread-count",
                      headers=auth_headers(reader)).get_json()["unreadCount"] == 0




# ===========================================================
#  mark_read — the socket-shared rate budget
# ===========================================================


def test_rest_mark_read_spends_the_socket_budget(client, db, make_user, auth_headers):
    reader = make_user()
    writer = make_user()
    conv_id = _seed_room(db, [reader["id"], writer["id"]])
    _seed_message(db, conv_id, writer["id"], "labas")
    headers = auth_headers(reader)

    with time_machine.travel(datetime(2026, 1, 1, 12, 0, 0), tick=False):
        for _ in range(_SOCKET_MARK_READ_BUDGET):
            assert client.put(_read_url(conv_id), headers=headers).status_code == 200

        response = client.put(_read_url(conv_id), headers=headers)

    assert response.status_code == 429
    body = response.get_json()
    assert body["code"] == "rate_limited"
    assert body["error"] == "Too many requests. Please slow down."


def test_the_mark_read_budget_frees_up_after_its_ten_second_window(
        client, db, make_user, auth_headers):
    reader = make_user()
    writer = make_user()
    conv_id = _seed_room(db, [reader["id"], writer["id"]])
    _seed_message(db, conv_id, writer["id"], "labas")
    headers = auth_headers(reader)

    with time_machine.travel(datetime(2026, 1, 1, 12, 0, 0), tick=False) as traveller:
        for _ in range(_SOCKET_MARK_READ_BUDGET):
            client.put(_read_url(conv_id), headers=headers)
        assert client.put(_read_url(conv_id), headers=headers).status_code == 429

        traveller.shift(11)
        assert client.put(_read_url(conv_id), headers=headers).status_code == 200


def test_a_rejected_mark_read_writes_nothing(client, db, make_user, auth_headers):
    reader = make_user()
    writer = make_user()
    conv_id = _seed_room(db, [reader["id"], writer["id"]], last_read_at=_stamp(0))
    headers = auth_headers(reader)

    with time_machine.travel(datetime(2026, 1, 1, 12, 0, 0), tick=False) as traveller:
        for _ in range(_SOCKET_MARK_READ_BUDGET):
            client.put(_read_url(conv_id), headers=headers)

        # one second on: still inside the 10 s window, so the
        # next call is refused — and a message that arrived in
        # the meantime must stay unreceipted, not get a receipt
        # the reader never earned
        traveller.shift(1)
        late = _seed_message(db, conv_id, writer["id"], "velyva",
                             created_at="2026-01-01T12:00:00.500000")

        assert client.put(_read_url(conv_id), headers=headers).status_code == 429

    assert db.execute(
        "SELECT COUNT(*) FROM message_reads WHERE message_id = ?", (late,)
    ).fetchone()[0] == 0


def test_one_users_mark_read_budget_does_not_bind_another(client, db, make_user, auth_headers):
    reader = make_user()
    other = make_user()
    conv_id = _seed_room(db, [reader["id"], other["id"]])
    _seed_message(db, conv_id, other["id"], "labas")
    reader_h = auth_headers(reader)
    other_h = auth_headers(other)

    with time_machine.travel(datetime(2026, 1, 1, 12, 0, 0), tick=False):
        for _ in range(_SOCKET_MARK_READ_BUDGET):
            client.put(_read_url(conv_id), headers=reader_h)

        assert client.put(_read_url(conv_id), headers=other_h).status_code == 200
