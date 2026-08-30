# -----------------------------------------------------------
#  [*] Tests — chat reactions, the exhaustive pass
#
#  The gap-closing sweep over FOUR functions of
#  app/chat/routes.py and nothing else:
#
#    react_to_message  — POST   .../messages/<mid>/react
#    remove_reaction   — DELETE .../messages/<mid>/react
#    _reactions_for    — the one reaction shaper both
#                        transports read from
#    _reply_payload    — the quoted-message block
#
#  What it proves, branch by branch:
#
#    - The react body ladder in ITS OWN ORDER: the falsy
#      "emoji required" check fires before the isinstance
#      check (so 0 / False / [] are "required", 42 / True are
#      "must be a string"), and both fire before the DB is
#      even opened — a non-member with a bad emoji gets a 400,
#      not a 403.
#    - The allowlist is byte-exact: the bare heart, a skin-
#      toned thumb, a padded or doubled emoji and a 5 000-char
#      string are all refused; the six the mobile picker
#      offers are accepted and echoed back unchanged when the
#      request is posted as RAW UTF-8 (TESTPLAN rule 10).
#    - react and unreact differ on exactly ONE gate: react
#      refuses an unsent message (404), unreact still serves
#      it — a chip must not resurrect on a placeholder bubble,
#      but clearing one must always be possible.
#    - Both routes snapshot INSIDE the write transaction and
#      broadcast AFTER the commit: the fake emit reads the
#      database on a second connection and already sees the
#      finished write.
#    - _reactions_for: the empty-id short circuit, both wire
#      shapes (bySelf only when a current user is passed, and
#      passed "" still counts — the check is `is not None`),
#      duplicate ids, a full 100-id page, and the documented
#      non-guarantee that it checks neither membership nor
#      whether the message is still live.
#    - _reply_payload: not-a-reply, a BLANK reply id, the
#      dangling-quote ghost shape, an unsent quote, a quote
#      whose sender row vanished, a NULL quote text, and that
#      `deleted` is `is not None` rather than truthiness.
#
#  Nothing here sleeps and nothing reaches the network; the
#  429 tests arrange the limiter's process state directly
#  instead of firing 300 real requests.
# -----------------------------------------------------------

import importlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

# The six the mobile picker offers (REACTION_OPTIONS in
# hooks/chat/useChatReactions.ts). Spelled with escapes so a
# reviewer can see the heart is U+2764 PLUS U+FE0F — the bare
# heart is a different string and the server refuses it
THUMBS = "\U0001F44D"
HEART = "❤️"
JOY = "\U0001F602"
WOW = "\U0001F62E"
SAD = "\U0001F622"
ANGRY = "\U0001F621"
ALL_REACTIONS = (THUMBS, HEART, JOY, WOW, SAD, ANGRY)

# Every seeded stamp hangs off this base, comfortably in the
# past, so a route's own datetime.now() always sorts after it
_BASE = datetime(2021, 5, 4, 9, 0, 0)

# The route's own budget, restated so a boundary test reads as
# a boundary test
_REACT_BUDGET = 300




# -----------------------------------------------------------
# _stamp
# -----------------------------------------------------------
#
# A naive-UTC isoformat stamp `offset` seconds after _BASE —
# the exact string shape chat/routes.py writes and compares as
# text.
# -----------------------------------------------------------

def _stamp(offset=0):
    return (_BASE + timedelta(seconds=offset)).isoformat()




# -----------------------------------------------------------
# _seed_room / _seed_message / _seed_reaction
# -----------------------------------------------------------
#
# Rows written straight to the database: POST /conversations
# cannot build a group of arbitrary size, cannot plant an
# unsent message and spends a rate budget these tests need for
# something else. The conftest `db` connection does NOT set
# PRAGMA foreign_keys, which is what lets the shaper tests
# plant reactions under invented user ids and delete a quoted
# row out from under a reply.
# -----------------------------------------------------------

def _seed_room(db, member_ids, conv_type="group", title="Reakcijų kambarys"):
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
        [(conv_id, uid, None) for uid in member_ids],
    )
    db.commit()

    return conv_id


def _seed_message(db, conv_id, sender_id, text="labas", offset=1, deleted=False,
                  image_url=None, reply_to_id=None, msg_id=None):
    msg_id = msg_id or str(uuid.uuid4())

    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, reply_to_id,"
        " created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, image_url, reply_to_id,
         _stamp(offset), _stamp(offset) if deleted else None),
    )
    db.commit()

    return msg_id


def _seed_reaction(db, msg_id, user_id, emoji):
    db.execute(
        "INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
        (msg_id, user_id, emoji),
    )
    db.commit()




# -----------------------------------------------------------
# URL builders
# -----------------------------------------------------------

def _react_url(conv_id, msg_id):
    return f"/api/chat/conversations/{conv_id}/messages/{msg_id}/react"


def _messages_url(conv_id):
    return f"/api/chat/conversations/{conv_id}/messages"




# -----------------------------------------------------------
# _post_raw
# -----------------------------------------------------------
#
# TESTPLAN rule 10: a `json=` kwarg is serialised through the
# app's own html-escaping provider, so what lands on the wire
# is not what a real client sends. Every test that cares about
# the exact BYTES of an emoji posts through here instead.
# -----------------------------------------------------------

def _post_raw(client, path, payload, headers):
    return client.post(
        path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
    )




# -----------------------------------------------------------
# _reaction_rows / _by_emoji
# -----------------------------------------------------------
#
# Small readers so a persistence assertion is one line and a
# group assertion does not depend on the order SQLite happens
# to return the reaction rows in.
# -----------------------------------------------------------

def _reaction_rows(db, msg_id):
    return db.execute(
        "SELECT user_id, emoji FROM message_reactions WHERE message_id = ? ORDER BY user_id",
        (msg_id,),
    ).fetchall()


def _by_emoji(groups):
    return {g["emoji"]: g for g in groups}




# -----------------------------------------------------------
# _shaper_row
# -----------------------------------------------------------
#
# A REAL sqlite3.Row carrying the six reply_* columns
# get_messages selects — _reply_payload indexes its argument
# by name and raises IndexError on a missing one, so the
# stand-in has to be a genuine Row with every column present,
# not a dict that happens to answer the keys the branch reads.
# -----------------------------------------------------------

def _shaper_row(db, **overrides):
    cols = {
        "reply_to_id": None,
        "reply_sender_id": None,
        "reply_sender_name": None,
        "reply_text": None,
        "reply_image_url": None,
        "reply_deleted_at": None,
    }
    cols.update(overrides)
    keys = list(cols)
    sql = "SELECT " + ", ".join(f"? AS {k}" for k in keys)

    return db.execute(sql, [cols[k] for k in keys]).fetchone()




# -----------------------------------------------------------
# _spend_budget
# -----------------------------------------------------------
#
# Fills a rate-limit key's window with `count` stamps, so the
# 300-per-5-min ceiling can be walked up to and over without
# firing 300 real requests. The store is auth/routes.py
# PROCESS state and the stamps are time.monotonic() floats —
# the same clock _check_rate_limit prunes against.
# -----------------------------------------------------------

def _spend_budget(user_id, count, scope="chat_react"):
    from app.auth.routes import _rate_limit_lock, _rate_limit_store

    now = time.monotonic()
    with _rate_limit_lock:
        _rate_limit_store[f"{scope}:{user_id}"] = [now] * count




# -----------------------------------------------------------
# fresh_limiter
# -----------------------------------------------------------
#
# The limiter lives in module state, not in the database, so
# the fresh-database fixtures do not reset it. Cleared on both
# sides so this file neither inherits a spent budget nor
# exports one to the modules that run after it.
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
# routes
# -----------------------------------------------------------
#
# The module under test, imported through importlib so the
# package name does not collide with the `app` fixture this
# depends on — the dependency is what guarantees create_app
# has already run against the throwaway database.
# -----------------------------------------------------------

@pytest.fixture
def routes(app):
    return importlib.import_module("app.chat.routes")




# -----------------------------------------------------------
# emits
# -----------------------------------------------------------
#
# Captures every reaction_update broadcast. Both routes import
# emit_reaction_update INSIDE the request, so patching the
# attribute on chat.events is enough to intercept it.
# -----------------------------------------------------------

@pytest.fixture
def emits(app, monkeypatch):
    events = importlib.import_module("app.chat.events")
    calls = []

    def _fake(socketio, conv_id, msg_id, reactions):
        calls.append(SimpleNamespace(conv=conv_id, msg=msg_id, reactions=reactions))

    monkeypatch.setattr(events, "emit_reaction_update", _fake)

    return calls




# -----------------------------------------------------------
# room
# -----------------------------------------------------------
#
# The standing cast: a three-person room (alice, bob, carol),
# one message in it from bob, plus an outsider who belongs to
# nothing and an admin who is likewise not a member — the role
# must buy no access. Tokens all come from the real login
# route via the shared auth_headers fixture.
# -----------------------------------------------------------

@pytest.fixture
def room(db, make_user, auth_headers, admin):
    alice = make_user()
    bob = make_user()
    carol = make_user()
    outsider = make_user()
    admin_user, admin_h = admin

    conv_id = _seed_room(db, [alice["id"], bob["id"], carol["id"]])
    msg_id = _seed_message(db, conv_id, bob["id"], "Sveiki, kaip sekasi?")

    return SimpleNamespace(
        alice=alice, alice_h=auth_headers(alice),
        bob=bob, bob_h=auth_headers(bob),
        carol=carol, carol_h=auth_headers(carol),
        outsider=outsider, outsider_h=auth_headers(outsider),
        admin=admin_user, admin_h=admin_h,
        conv=conv_id, msg=msg_id,
    )




# ===========================================================
#  react_to_message — the body ladder, in its own order
# ===========================================================


def test_reacting_with_no_body_at_all_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 400
    assert response.get_json() == {"error": "emoji required"}


def test_a_json_null_body_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg),
                           data=b"null", headers={**room.alice_h,
                                                 "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_an_empty_json_object_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_a_malformed_json_body_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg),
                           data=b'{"emoji": ', headers={**room.alice_h,
                                                       "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_a_form_encoded_body_is_not_read_as_json(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           data={"emoji": THUMBS})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_a_json_body_sent_as_plain_text_is_not_read(client, room):
    # get_json only parses a json content type, so the body is
    # invisible to the route however well-formed it is
    response = client.post(_react_url(room.conv, room.msg),
                           data=json.dumps({"emoji": THUMBS}),
                           headers={**room.alice_h, "Content-Type": "text/plain"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_a_missing_emoji_key_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"reaction": THUMBS})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_a_null_emoji_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": None})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


def test_an_empty_emoji_string_is_refused(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": ""})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


@pytest.mark.parametrize("falsy", [0, False, [], {}, 0.0])
def test_a_falsy_emoji_is_required_before_it_is_typed(client, room, falsy):
    # the ladder's first rung is `not data.get("emoji")`, so a
    # falsy NON-string never reaches the isinstance check
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": falsy})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji required"


@pytest.mark.parametrize("truthy", [42, True, 1.5, [THUMBS], {"emoji": THUMBS}])
def test_a_truthy_non_string_emoji_is_refused_as_a_non_string(client, room, truthy):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": truthy})

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji must be a string"




# ===========================================================
#  react_to_message — the allowlist, byte for byte
# ===========================================================


@pytest.mark.parametrize("emoji", ALL_REACTIONS)
def test_every_emoji_the_picker_offers_is_accepted_on_the_wire(client, room, emoji):
    # raw bytes, not the `json=` kwarg: this asserts what a real
    # client puts on the wire and gets echoed back
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": emoji}, room.alice_h)

    assert response.status_code == 200
    body = response.get_json()
    assert body["emoji"] == emoji
    assert body["reactions"] == [
        {"emoji": emoji, "count": 1, "byUserIds": [room.alice["id"]]}
    ]


def test_the_allowlist_is_exactly_the_six_the_picker_offers(routes):
    assert routes._ALLOWED_REACTIONS == frozenset(ALL_REACTIONS)
    assert len(routes._ALLOWED_REACTIONS) == 6


def test_the_bare_heart_without_its_variation_selector_is_refused(client, room):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": "❤"}, room.alice_h)

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji must be one of the supported reactions"


def test_a_heart_with_a_doubled_variation_selector_is_refused(client, room):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": "❤️️"}, room.alice_h)

    assert response.status_code == 400


def test_a_skin_toned_thumbs_up_is_refused(client, room):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": "\U0001F44D\U0001F3FD"}, room.alice_h)

    assert response.status_code == 400


def test_a_thumbs_up_with_a_zero_width_joiner_is_refused(client, room):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": "\U0001F44D‍"}, room.alice_h)

    assert response.status_code == 400


@pytest.mark.parametrize("padded", [" \U0001F44D", "\U0001F44D ", "\n\U0001F44D",
                                    "\t\U0001F44D\t", "\U0001F44D "])
def test_a_padded_emoji_is_refused_rather_than_stripped(client, room, padded):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": padded}, room.alice_h)

    assert response.status_code == 400
    assert response.get_json()["error"] == "emoji must be one of the supported reactions"


def test_a_doubled_emoji_is_refused(client, room):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": THUMBS * 2}, room.alice_h)

    assert response.status_code == 400


@pytest.mark.parametrize("text", ["thumbsup", "+1", ":+1:", "like", "<3", "👍👎"])
def test_a_plain_text_reaction_is_refused(client, room, text):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": text}, room.alice_h)

    assert response.status_code == 400


def test_an_unlisted_emoji_is_refused(client, room):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": "\U0001F389"}, room.alice_h)

    assert response.status_code == 400
    assert "reactions" not in response.get_json()


def test_a_five_thousand_character_reaction_is_refused(client, db, room):
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": "a" * 5000}, room.alice_h)

    assert response.status_code == 400
    assert _reaction_rows(db, room.msg) == []


def test_a_nul_padded_emoji_is_accepted_once_the_input_guard_strips_it(client, room):
    # the app's validate_json_input hook drops \x00 from every
    # string BEFORE the route sees the body, so the allowlist
    # compares the cleaned value — documented here because the
    # two guards only make sense read together
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": "\x00" + THUMBS}, room.alice_h)

    assert response.status_code == 200
    assert response.get_json()["emoji"] == THUMBS




# ===========================================================
#  react_to_message — guard order and the access gates
# ===========================================================


def test_reacting_requires_authentication(client, room):
    response = client.post(_react_url(room.conv, room.msg), json={"emoji": THUMBS})

    assert response.status_code == 401


def test_a_bogus_bearer_token_is_unauthorized(client, room):
    response = client.post(_react_url(room.conv, room.msg),
                           headers={"Authorization": "Bearer nera-tokio-zetono"},
                           json={"emoji": THUMBS})

    assert response.status_code == 401


def test_authentication_is_checked_before_the_emoji(client, room):
    # require_auth wraps the body ladder, so an anonymous caller
    # cannot even learn that the emoji was the problem
    response = client.post(_react_url(room.conv, room.msg), json={"emoji": "\U0001F389"})

    assert response.status_code == 401


def test_the_emoji_is_checked_before_membership(client, room):
    # the whole body ladder runs before get_db(), so an outsider
    # gets 400 — not the 403 they would get with a good emoji
    response = _post_raw(client, _react_url(room.conv, room.msg),
                         {"emoji": "\U0001F389"}, room.outsider_h)

    assert response.status_code == 400
    assert client.post(_react_url(room.conv, room.msg), headers=room.outsider_h,
                       json={"emoji": THUMBS}).status_code == 403


def test_the_emoji_is_checked_before_the_conversation_is_looked_up(client, room):
    response = _post_raw(client, _react_url(str(uuid.uuid4()), room.msg),
                         {"emoji": "\U0001F389"}, room.alice_h)

    assert response.status_code == 400


def test_a_non_member_cannot_react(client, db, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.outsider_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}
    assert _reaction_rows(db, room.msg) == []


def test_the_forbidden_body_never_leaks_the_reactions(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": HEART})

    response = client.post(_react_url(room.conv, room.msg), headers=room.outsider_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 403
    assert "reactions" not in response.get_json()


def test_an_admin_who_is_not_a_member_cannot_react(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.admin_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 403


def test_a_member_who_left_can_no_longer_react(client, db, room):
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (room.conv, room.alice["id"]))
    db.commit()

    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 403


def test_reacting_in_an_unknown_conversation_is_forbidden(client, room):
    response = client.post(_react_url(str(uuid.uuid4()), room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 403


def test_reacting_to_an_unknown_message_is_not_found(client, room):
    response = client.post(_react_url(room.conv, str(uuid.uuid4())), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 404
    assert response.get_json() == {"error": "Message not found"}


def test_reacting_to_a_hard_deleted_message_is_not_found(client, db, room):
    db.execute("DELETE FROM messages WHERE id = ?", (room.msg,))
    db.commit()

    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 404


def test_reacting_to_a_message_of_another_room_the_caller_also_belongs_to_is_not_found(
        client, db, room):
    # membership passes on BOTH rooms — only the conversation id
    # in the URL decides where the message must live
    other_conv = _seed_room(db, [room.alice["id"], room.bob["id"]])
    other_msg = _seed_message(db, other_conv, room.bob["id"], "kitas kambarys", offset=3)

    response = client.post(_react_url(room.conv, other_msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 404
    assert _reaction_rows(db, other_msg) == []


def test_reacting_to_an_unsent_message_is_not_found(client, db, room):
    unsent = _seed_message(db, room.conv, room.bob["id"], "", offset=4, deleted=True)

    response = client.post(_react_url(room.conv, unsent), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 404
    assert _reaction_rows(db, unsent) == []


def test_a_huge_message_id_is_simply_not_found(client, room):
    response = client.post(_react_url(room.conv, "z" * 4000), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 404


def test_a_markup_shaped_message_id_is_just_not_found(client, room):
    # the id reaches the route decoded and is only ever a query
    # parameter — no lookup, no reflection, a plain 404
    response = client.post(_react_url(room.conv, "%3Cscript%3Ealert(1)%3B"),
                           headers=room.alice_h, json={"emoji": THUMBS})

    assert response.status_code == 404
    assert response.get_json() == {"error": "Message not found"}


def test_a_huge_conversation_id_is_forbidden(client, room):
    response = client.post(_react_url("c" * 4000, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 403




# ===========================================================
#  react_to_message — what the write actually does
# ===========================================================


def test_reacting_to_your_own_message_is_allowed(client, db, room):
    own = _seed_message(db, room.conv, room.alice["id"], "mano žinutė", offset=5)

    response = client.post(_react_url(room.conv, own), headers=room.alice_h,
                           json={"emoji": JOY})

    assert response.status_code == 200
    assert [tuple(r) for r in _reaction_rows(db, own)] == [(room.alice["id"], JOY)]


@pytest.mark.contract
def test_the_react_response_carries_exactly_ok_emoji_and_reactions(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    body = response.get_json()
    assert set(body) == {"ok", "emoji", "reactions"}
    assert body["ok"] is True


@pytest.mark.contract
def test_a_react_group_carries_no_by_self(client, room):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    group = response.get_json()["reactions"][0]
    assert set(group) == {"emoji", "count", "byUserIds"}


def test_the_same_emoji_twice_leaves_one_row(client, db, room):
    for _ in range(3):
        assert client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS}).status_code == 200

    assert [tuple(r) for r in _reaction_rows(db, room.msg)] == [(room.alice["id"], THUMBS)]


def test_switching_emoji_replaces_the_row_instead_of_adding_one(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": ANGRY})

    assert response.get_json()["reactions"] == [
        {"emoji": ANGRY, "count": 1, "byUserIds": [room.alice["id"]]}
    ]
    assert [tuple(r) for r in _reaction_rows(db, room.msg)] == [(room.alice["id"], ANGRY)]


def test_all_six_emoji_from_one_user_collapse_to_the_last(client, db, room):
    for emoji in ALL_REACTIONS:
        client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": emoji})

    assert [tuple(r) for r in _reaction_rows(db, room.msg)] == [(room.alice["id"], ANGRY)]


def test_two_members_on_one_emoji_share_a_single_group(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": WOW})
    response = client.post(_react_url(room.conv, room.msg), headers=room.bob_h,
                           json={"emoji": WOW})

    groups = response.get_json()["reactions"]
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert set(groups[0]["byUserIds"]) == {room.alice["id"], room.bob["id"]}


def test_the_response_lists_the_other_members_reaction_too(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": SAD})

    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": JOY})

    groups = _by_emoji(response.get_json()["reactions"])
    assert set(groups) == {SAD, JOY}
    assert groups[SAD]["byUserIds"] == [room.bob["id"]]
    assert groups[JOY]["byUserIds"] == [room.alice["id"]]


def test_six_members_on_six_emoji_produce_six_groups(client, db, make_user, auth_headers):
    members = [make_user() for _ in range(6)]
    conv_id = _seed_room(db, [m["id"] for m in members])
    msg_id = _seed_message(db, conv_id, members[0]["id"], "šešios reakcijos")

    for member, emoji in zip(members, ALL_REACTIONS):
        response = client.post(_react_url(conv_id, msg_id), headers=auth_headers(member),
                               json={"emoji": emoji})
        assert response.status_code == 200

    groups = _by_emoji(response.get_json()["reactions"])
    assert set(groups) == set(ALL_REACTIONS)
    assert all(g["count"] == 1 for g in groups.values())


def test_reacting_leaves_a_sibling_message_untouched(client, db, room):
    other = _seed_message(db, room.conv, room.bob["id"], "kita žinutė", offset=6)

    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    assert _reaction_rows(db, other) == []


def test_reacting_writes_nothing_but_the_reaction_row(client, db, room):
    before = db.execute("SELECT updated_at FROM conversations WHERE id = ?",
                        (room.conv,)).fetchone()["updated_at"]

    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    after = db.execute("SELECT updated_at FROM conversations WHERE id = ?",
                       (room.conv,)).fetchone()["updated_at"]
    assert after == before
    assert db.execute("SELECT COUNT(*) AS n FROM message_reads").fetchone()["n"] == 0


def test_the_react_list_matches_what_the_history_page_reports(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": HEART})
    react = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                        json={"emoji": HEART}).get_json()["reactions"]

    page = client.get(_messages_url(room.conv), headers=room.alice_h).get_json()
    page_groups = page["messages"][0]["reactions"]

    # same shaper, one extra key: get_messages passes a current
    # user, so its groups also carry bySelf
    assert [{k: v for k, v in g.items() if k != "bySelf"} for g in page_groups] == react
    assert all(g["bySelf"] is True for g in page_groups)




# ===========================================================
#  react_to_message — the broadcast
# ===========================================================


def test_the_react_broadcast_carries_the_response_list(client, room, emits):
    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert len(emits) == 1
    assert emits[0].conv == room.conv
    assert emits[0].msg == room.msg
    assert emits[0].reactions == response.get_json()["reactions"]


def test_the_react_broadcast_carries_no_by_self(client, room, emits):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    assert set(emits[0].reactions[0]) == {"emoji", "count", "byUserIds"}


def test_a_refused_react_broadcasts_nothing(client, room, emits):
    client.post(_react_url(room.conv, room.msg), headers=room.outsider_h, json={"emoji": THUMBS})
    client.post(_react_url(room.conv, str(uuid.uuid4())), headers=room.alice_h,
                json={"emoji": THUMBS})
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": "x"})

    assert emits == []


def test_the_react_is_committed_before_it_is_broadcast(client, app, room, monkeypatch):
    # a SECOND connection reads the row from inside the fake
    # emit: seeing it there proves the commit already happened,
    # which is what makes the broadcast snapshot authoritative
    events = importlib.import_module("app.chat.events")
    seen = []

    def _fake(socketio, conv_id, msg_id, reactions):
        probe = sqlite3.connect(app.config["DB_PATH"])
        try:
            seen.append(probe.execute(
                "SELECT emoji FROM message_reactions WHERE message_id = ?", (msg_id,)
            ).fetchall())
        finally:
            probe.close()

    monkeypatch.setattr(events, "emit_reaction_update", _fake)

    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    assert seen == [[(THUMBS,)]]




# ===========================================================
#  react_to_message — the shared 300-per-5-min budget
# ===========================================================


def test_the_last_call_inside_the_budget_still_reacts(client, room):
    _spend_budget(room.alice["id"], _REACT_BUDGET - 1)

    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 200


def test_the_call_past_the_budget_is_rate_limited(client, room):
    _spend_budget(room.alice["id"], _REACT_BUDGET)

    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                           json={"emoji": THUMBS})

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


def test_a_spent_react_budget_also_blocks_the_unreact_verb(client, room):
    _spend_budget(room.alice["id"], _REACT_BUDGET)

    assert client.delete(_react_url(room.conv, room.msg),
                         headers=room.alice_h).status_code == 429


def test_a_spent_budget_answers_before_the_body_is_parsed(client, room):
    _spend_budget(room.alice["id"], _REACT_BUDGET)

    response = client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={})

    assert response.status_code == 429


def test_a_refused_emoji_still_spends_one_call_of_the_budget(client, room):
    _spend_budget(room.alice["id"], _REACT_BUDGET - 1)

    assert client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                       json={"emoji": "\U0001F389"}).status_code == 400
    # the rejected call burned the last one — a perfectly good
    # react now finds the budget gone
    assert client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                       json={"emoji": THUMBS}).status_code == 429


def test_a_forbidden_react_still_spends_one_call_of_the_budget(client, room):
    _spend_budget(room.outsider["id"], _REACT_BUDGET - 1)

    assert client.post(_react_url(room.conv, room.msg), headers=room.outsider_h,
                       json={"emoji": THUMBS}).status_code == 403
    assert client.post(_react_url(room.conv, room.msg), headers=room.outsider_h,
                       json={"emoji": THUMBS}).status_code == 429


def test_one_members_spent_budget_does_not_bind_another(client, room):
    _spend_budget(room.alice["id"], _REACT_BUDGET)

    assert client.post(_react_url(room.conv, room.msg), headers=room.bob_h,
                       json={"emoji": THUMBS}).status_code == 200




# ===========================================================
#  remove_reaction
# ===========================================================


def test_removing_a_reaction_clears_it(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 200
    assert response.get_json()["reactions"] == []
    assert _reaction_rows(db, room.msg) == []


@pytest.mark.contract
def test_the_unreact_response_carries_exactly_ok_and_reactions(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    body = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h).get_json()

    assert set(body) == {"ok", "reactions"}
    assert body["ok"] is True


@pytest.mark.contract
def test_an_unreact_group_carries_no_by_self(client, room):
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": HEART})
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": HEART})

    body = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h).get_json()

    assert set(body["reactions"][0]) == {"emoji", "count", "byUserIds"}


def test_removing_a_reaction_that_was_never_set_is_a_silent_ok(client, room):
    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "reactions": []}


def test_removing_twice_is_idempotent(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 200
    assert response.get_json()["reactions"] == []
    assert _reaction_rows(db, room.msg) == []


def test_removing_clears_only_the_callers_own_row(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": JOY})
    client.post(_react_url(room.conv, room.msg), headers=room.carol_h, json={"emoji": JOY})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    groups = response.get_json()["reactions"]
    assert len(groups) == 1
    assert groups[0]["emoji"] == JOY
    assert set(groups[0]["byUserIds"]) == {room.bob["id"], room.carol["id"]}
    assert {r["user_id"] for r in _reaction_rows(db, room.msg)} == {room.bob["id"], room.carol["id"]}


def test_removing_when_only_others_reacted_returns_their_groups_untouched(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": WOW})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.get_json()["reactions"] == [
        {"emoji": WOW, "count": 1, "byUserIds": [room.bob["id"]]}
    ]
    assert len(_reaction_rows(db, room.msg)) == 1


def test_removing_clears_whichever_emoji_the_caller_last_switched_to(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": SAD})

    client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert _reaction_rows(db, room.msg) == []


def test_removing_a_reaction_from_an_unsent_message_still_works(client, db, room):
    # the ONE gate react and unreact do not share: clearing a
    # chip must stay possible after the bubble became a
    # placeholder, even though setting one must not
    unsent = _seed_message(db, room.conv, room.bob["id"], "", offset=7, deleted=True)
    _seed_reaction(db, unsent, room.alice["id"], THUMBS)

    response = client.delete(_react_url(room.conv, unsent), headers=room.alice_h)

    assert response.status_code == 200
    assert response.get_json()["reactions"] == []
    assert _reaction_rows(db, unsent) == []
    # and the same message refuses a fresh react
    assert client.post(_react_url(room.conv, unsent), headers=room.alice_h,
                       json={"emoji": THUMBS}).status_code == 404


def test_removing_a_reaction_requires_authentication(client, room):
    response = client.delete(_react_url(room.conv, room.msg))

    assert response.status_code == 401


def test_a_non_member_cannot_remove_a_reaction(client, db, room):
    _seed_reaction(db, room.msg, room.bob["id"], THUMBS)

    response = client.delete(_react_url(room.conv, room.msg), headers=room.outsider_h)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Not a participant"}
    assert len(_reaction_rows(db, room.msg)) == 1


def test_the_unreact_forbidden_body_never_leaks_the_reactions(client, db, room):
    _seed_reaction(db, room.msg, room.bob["id"], HEART)

    response = client.delete(_react_url(room.conv, room.msg), headers=room.outsider_h)

    assert "reactions" not in response.get_json()


def test_an_admin_who_is_not_a_member_cannot_remove_a_reaction(client, room):
    response = client.delete(_react_url(room.conv, room.msg), headers=room.admin_h)

    assert response.status_code == 403


def test_a_member_who_left_can_no_longer_unreact(client, db, room):
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (room.conv, room.alice["id"]))
    db.commit()

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert response.status_code == 403
    assert len(_reaction_rows(db, room.msg)) == 1


def test_unreact_membership_is_checked_before_the_message_exists(client, room):
    # an outsider gets the same 403 whether the id names a real
    # message or nothing at all
    real = client.delete(_react_url(room.conv, room.msg), headers=room.outsider_h)
    fake = client.delete(_react_url(room.conv, str(uuid.uuid4())), headers=room.outsider_h)

    assert real.status_code == fake.status_code == 403
    assert real.get_json() == fake.get_json()


def test_removing_a_reaction_from_an_unknown_message_is_not_found(client, room):
    response = client.delete(_react_url(room.conv, str(uuid.uuid4())), headers=room.alice_h)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Message not found"}


def test_removing_a_reaction_from_another_rooms_message_is_not_found(client, db, room):
    other_conv = _seed_room(db, [room.alice["id"], room.bob["id"]])
    other_msg = _seed_message(db, other_conv, room.bob["id"], "kitas kambarys", offset=8)
    _seed_reaction(db, other_msg, room.alice["id"], THUMBS)

    response = client.delete(_react_url(room.conv, other_msg), headers=room.alice_h)

    assert response.status_code == 404
    assert len(_reaction_rows(db, other_msg)) == 1


def test_removing_a_reaction_in_an_unknown_conversation_is_forbidden(client, room):
    response = client.delete(_react_url(str(uuid.uuid4()), room.msg), headers=room.alice_h)

    assert response.status_code == 403


def test_a_json_body_on_the_delete_is_ignored(client, db, room):
    client.post(_react_url(room.conv, room.msg), headers=room.alice_h, json={"emoji": THUMBS})

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h,
                             json={"emoji": "\U0001F389"})

    assert response.status_code == 200
    assert _reaction_rows(db, room.msg) == []


def test_removing_leaves_a_sibling_message_untouched(client, db, room):
    other = _seed_message(db, room.conv, room.bob["id"], "kita žinutė", offset=9)
    _seed_reaction(db, other, room.alice["id"], THUMBS)
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)

    client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert len(_reaction_rows(db, other)) == 1


def test_the_unreact_broadcast_carries_the_response_list(client, room, emits):
    client.post(_react_url(room.conv, room.msg), headers=room.bob_h, json={"emoji": HEART})
    emits.clear()

    response = client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert len(emits) == 1
    assert emits[0].conv == room.conv
    assert emits[0].msg == room.msg
    assert emits[0].reactions == response.get_json()["reactions"]


def test_a_no_op_unreact_still_broadcasts(client, room, emits):
    # the broadcast lives outside any "did anything change"
    # guard — a retry re-announces the unchanged list
    client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert len(emits) == 1
    assert emits[0].reactions == []


def test_a_refused_unreact_broadcasts_nothing(client, room, emits):
    client.delete(_react_url(room.conv, room.msg), headers=room.outsider_h)
    client.delete(_react_url(room.conv, str(uuid.uuid4())), headers=room.alice_h)

    assert emits == []


def test_the_unreact_is_committed_before_it_is_broadcast(client, app, room, db, monkeypatch):
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)
    events = importlib.import_module("app.chat.events")
    seen = []

    def _fake(socketio, conv_id, msg_id, reactions):
        probe = sqlite3.connect(app.config["DB_PATH"])
        try:
            seen.append(probe.execute(
                "SELECT COUNT(*) FROM message_reactions WHERE message_id = ?", (msg_id,)
            ).fetchone()[0])
        finally:
            probe.close()

    monkeypatch.setattr(events, "emit_reaction_update", _fake)

    client.delete(_react_url(room.conv, room.msg), headers=room.alice_h)

    assert seen == [0]


def test_the_last_unreact_inside_the_budget_still_runs(client, room):
    _spend_budget(room.alice["id"], _REACT_BUDGET - 1)

    assert client.delete(_react_url(room.conv, room.msg),
                         headers=room.alice_h).status_code == 200


def test_a_spent_unreact_budget_also_blocks_the_react_verb(client, room):
    _spend_budget(room.alice["id"], _REACT_BUDGET - 1)

    assert client.delete(_react_url(room.conv, room.msg),
                         headers=room.alice_h).status_code == 200
    assert client.post(_react_url(room.conv, room.msg), headers=room.alice_h,
                       json={"emoji": THUMBS}).status_code == 429




# ===========================================================
#  _reactions_for — the one shaper, called directly
# ===========================================================


@pytest.mark.parametrize("empty", [[], (), set(), None, ""])
def test_an_empty_id_batch_short_circuits_to_an_empty_map(routes, db, empty):
    assert routes._reactions_for(db, empty) == {}
    assert routes._reactions_for(db, empty, "kas-nors") == {}


def test_ids_without_reactions_are_absent_rather_than_empty(routes, db, room):
    other = _seed_message(db, room.conv, room.bob["id"], "be reakcijų", offset=10)

    assert routes._reactions_for(db, [room.msg, other]) == {}


def test_one_reaction_shapes_one_group(routes, db, room):
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)

    assert routes._reactions_for(db, [room.msg]) == {
        room.msg: [{"emoji": THUMBS, "count": 1, "byUserIds": [room.alice["id"]]}]
    }


def test_a_current_user_adds_by_self_true_when_they_hold_the_emoji(routes, db, room):
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)

    group = routes._reactions_for(db, [room.msg], room.alice["id"])[room.msg][0]

    assert set(group) == {"emoji", "count", "bySelf", "byUserIds"}
    assert group["bySelf"] is True


def test_a_current_user_who_did_not_react_gets_by_self_false(routes, db, room):
    _seed_reaction(db, room.msg, room.bob["id"], THUMBS)

    group = routes._reactions_for(db, [room.msg], room.alice["id"])[room.msg][0]

    assert group["bySelf"] is False


def test_by_self_is_decided_per_group(routes, db, room):
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)
    _seed_reaction(db, room.msg, room.bob["id"], ANGRY)

    groups = _by_emoji(routes._reactions_for(db, [room.msg], room.alice["id"])[room.msg])

    assert groups[THUMBS]["bySelf"] is True
    assert groups[ANGRY]["bySelf"] is False


def test_an_empty_string_current_user_still_gets_a_by_self_flag(routes, db, room):
    # the guard is `is not None`, not truthiness — an empty
    # identity is still "one caller", so the key ships
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)

    group = routes._reactions_for(db, [room.msg], "")[room.msg][0]

    assert group["bySelf"] is False
    assert "bySelf" in group


def test_no_current_user_leaves_every_group_without_by_self(routes, db, room):
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)
    _seed_reaction(db, room.msg, room.bob["id"], JOY)

    for group in routes._reactions_for(db, [room.msg])[room.msg]:
        assert "bySelf" not in group


def test_the_count_always_matches_the_user_id_list(routes, db, room):
    for i in range(5):
        _seed_reaction(db, room.msg, f"naudotojas-{i}", HEART)

    group = routes._reactions_for(db, [room.msg])[room.msg][0]

    assert group["count"] == 5 == len(group["byUserIds"])
    assert len(set(group["byUserIds"])) == 5


def test_every_emoji_appears_in_exactly_one_group(routes, db, room):
    for i, emoji in enumerate(ALL_REACTIONS):
        _seed_reaction(db, room.msg, f"a-{i}", emoji)
        _seed_reaction(db, room.msg, f"b-{i}", emoji)

    groups = routes._reactions_for(db, [room.msg])[room.msg]

    assert len(groups) == 6
    assert sorted(g["emoji"] for g in groups) == sorted(ALL_REACTIONS)
    assert all(g["count"] == 2 for g in groups)


def test_two_messages_come_back_under_their_own_keys(routes, db, room):
    second = _seed_message(db, room.conv, room.bob["id"], "antra", offset=11)
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)
    _seed_reaction(db, second, room.alice["id"], SAD)

    shaped = routes._reactions_for(db, [room.msg, second])

    assert set(shaped) == {room.msg, second}
    assert shaped[room.msg][0]["emoji"] == THUMBS
    assert shaped[second][0]["emoji"] == SAD


def test_a_duplicated_id_in_the_batch_yields_one_entry(routes, db, room):
    _seed_reaction(db, room.msg, room.alice["id"], THUMBS)

    shaped = routes._reactions_for(db, [room.msg, room.msg, room.msg])

    assert list(shaped) == [room.msg]
    # the id repeats in the IN (...) list, the row does not
    assert shaped[room.msg][0]["count"] == 1


def test_a_full_hundred_id_page_is_one_batch(routes, db, room):
    # 100 is get_messages' hard limit, so this is the widest
    # IN (...) the shaper ever sees in production
    msg_ids = [str(uuid.uuid4()) for _ in range(100)]
    db.executemany(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(mid, room.conv, room.bob["id"], "labas", _stamp(20 + i))
         for i, mid in enumerate(msg_ids)],
    )
    db.executemany(
        "INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
        [(mid, room.alice["id"], THUMBS) for mid in msg_ids],
    )
    db.commit()

    shaped = routes._reactions_for(db, msg_ids, room.alice["id"])

    assert len(shaped) == 100
    assert all(groups[0]["bySelf"] is True for groups in shaped.values())


def test_the_shaper_checks_neither_membership_nor_the_room(routes, db, make_user, room):
    # the documented non-guarantee: gating is the ROUTES' job,
    # and this is what makes the 403-before-404 order load-bearing
    stranger = make_user()
    far_conv = _seed_room(db, [stranger["id"]])
    far_msg = _seed_message(db, far_conv, stranger["id"], "svetima", offset=12)
    _seed_reaction(db, far_msg, stranger["id"], ANGRY)

    shaped = routes._reactions_for(db, [far_msg], room.alice["id"])

    assert shaped[far_msg][0]["byUserIds"] == [stranger["id"]]


def test_an_unsent_messages_reactions_are_still_shaped(routes, db, room):
    unsent = _seed_message(db, room.conv, room.bob["id"], "", offset=13, deleted=True)
    _seed_reaction(db, unsent, room.alice["id"], THUMBS)

    assert routes._reactions_for(db, [unsent])[unsent][0]["count"] == 1


def test_an_emoji_outside_the_allowlist_is_shaped_as_stored(routes, db, room):
    # legacy rows predate the allowlist; the shaper reports what
    # is in the table rather than filtering it
    _seed_reaction(db, room.msg, room.alice["id"], "\U0001F389")

    assert routes._reactions_for(db, [room.msg])[room.msg][0]["emoji"] == "\U0001F389"


def test_a_reaction_by_a_vanished_user_still_ships_its_id(routes, db, room):
    # no users join — the shaper never learned display names
    _seed_reaction(db, room.msg, "nebeegzistuoja", THUMBS)

    assert routes._reactions_for(db, [room.msg])[room.msg][0]["byUserIds"] == ["nebeegzistuoja"]




# ===========================================================
#  _reply_payload — the quoted block, called directly
# ===========================================================


def test_a_message_that_is_not_a_reply_has_no_payload(routes, db):
    assert routes._reply_payload(_shaper_row(db, reply_to_id=None)) is None


def test_a_blank_reply_id_counts_as_not_a_reply(routes, db):
    # the guard is `not row[...]`, so "" is "no quote" and takes
    # the same single code path as NULL
    assert routes._reply_payload(_shaper_row(db, reply_to_id="")) is None


@pytest.mark.contract
def test_a_dangling_quote_is_shaped_as_deleted(routes, db):
    payload = routes._reply_payload(_shaper_row(db, reply_to_id="nera-tokios-zinutes"))

    assert payload == {
        "id": "nera-tokios-zinutes",
        "senderId": None,
        "senderName": None,
        "text": "",
        "imageUrl": None,
        "deleted": True,
    }


def test_a_dangling_quote_ignores_whatever_the_other_columns_hold(routes, db):
    # the join missed, so nothing but reply_to_id is trustworthy
    payload = routes._reply_payload(_shaper_row(
        db, reply_to_id="vaiduoklis", reply_sender_id=None, reply_sender_name="Ona",
        reply_text="senas tekstas", reply_image_url="/api/uploads/x.jpg",
        reply_deleted_at=None,
    ))

    assert payload["senderName"] is None
    assert payload["text"] == ""
    assert payload["imageUrl"] is None
    assert payload["deleted"] is True


@pytest.mark.contract
def test_a_live_quote_carries_its_sender_text_and_image(routes, db):
    payload = routes._reply_payload(_shaper_row(
        db, reply_to_id="m1", reply_sender_id="u1", reply_sender_name="Ona",
        reply_text="Labas rytas", reply_image_url="/api/uploads/a.jpg",
    ))

    assert payload == {
        "id": "m1",
        "senderId": "u1",
        "senderName": "Ona",
        "text": "Labas rytas",
        "imageUrl": "/api/uploads/a.jpg",
        "deleted": False,
    }


def test_a_quote_with_a_null_text_becomes_an_empty_string(routes, db):
    payload = routes._reply_payload(_shaper_row(
        db, reply_to_id="m1", reply_sender_id="u1", reply_sender_name="Ona",
        reply_text=None, reply_image_url="/api/uploads/a.jpg",
    ))

    assert payload["text"] == ""
    assert payload["imageUrl"] == "/api/uploads/a.jpg"


def test_a_photo_only_quote_keeps_its_image_and_an_empty_text(routes, db):
    payload = routes._reply_payload(_shaper_row(
        db, reply_to_id="m1", reply_sender_id="u1", reply_sender_name="Ona",
        reply_text="", reply_image_url="/api/uploads/a.jpg",
    ))

    assert payload["text"] == ""
    assert payload["deleted"] is False


def test_an_unsent_quote_loses_its_text_and_image_but_keeps_its_sender(routes, db):
    payload = routes._reply_payload(_shaper_row(
        db, reply_to_id="m1", reply_sender_id="u1", reply_sender_name="Ona",
        reply_text="slaptas tekstas", reply_image_url="/api/uploads/a.jpg",
        reply_deleted_at="2021-05-04T09:00:00",
    ))

    assert payload == {
        "id": "m1",
        "senderId": "u1",
        "senderName": "Ona",
        "text": "",
        "imageUrl": None,
        "deleted": True,
    }


def test_an_empty_deleted_stamp_still_counts_as_unsent(routes, db):
    # `is not None`, not truthiness — a blank stamp is still a
    # stamp, and the quote blanks
    payload = routes._reply_payload(_shaper_row(
        db, reply_to_id="m1", reply_sender_id="u1", reply_sender_name="Ona",
        reply_text="tekstas", reply_deleted_at="",
    ))

    assert payload["deleted"] is True
    assert payload["text"] == ""


def test_a_quote_whose_sender_row_vanished_keeps_a_null_name(routes, db):
    # sender_id came off the messages row, the name off a LEFT
    # JOIN that missed — a live quote with an unnamed author
    payload = routes._reply_payload(_shaper_row(
        db, reply_to_id="m1", reply_sender_id="u1", reply_sender_name=None,
        reply_text="tekstas",
    ))

    assert payload["senderId"] == "u1"
    assert payload["senderName"] is None
    assert payload["deleted"] is False


def test_the_payload_carries_exactly_six_keys(routes, db):
    live = routes._reply_payload(_shaper_row(
        db, reply_to_id="m1", reply_sender_id="u1", reply_sender_name="Ona", reply_text="x"))
    ghost = routes._reply_payload(_shaper_row(db, reply_to_id="m1"))

    expected = {"id", "senderId", "senderName", "text", "imageUrl", "deleted"}
    assert set(live) == expected
    assert set(ghost) == expected




# ===========================================================
#  _reply_payload — through the routes that ship it
# ===========================================================


def test_a_history_page_carries_the_quote_of_a_reply(client, db, room):
    quoted = _seed_message(db, room.conv, room.bob["id"], "Klausimas?", offset=14)
    _seed_message(db, room.conv, room.alice["id"], "Atsakymas", offset=15, reply_to_id=quoted)

    page = client.get(_messages_url(room.conv), headers=room.alice_h).get_json()
    reply = [m for m in page["messages"] if m["text"] == "Atsakymas"][0]

    assert reply["replyTo"] == {
        "id": quoted,
        "senderId": room.bob["id"],
        "senderName": room.bob["username"].title(),
        "text": "Klausimas?",
        "imageUrl": None,
        "deleted": False,
    }


def test_a_plain_message_in_the_history_page_has_a_null_quote(client, room):
    page = client.get(_messages_url(room.conv), headers=room.alice_h).get_json()

    assert page["messages"][0]["replyTo"] is None


def test_a_quote_of_an_unsent_message_ships_the_placeholder(client, db, room):
    quoted = _seed_message(db, room.conv, room.bob["id"], "", offset=16, deleted=True)
    _seed_message(db, room.conv, room.alice["id"], "Atsakymas", offset=17, reply_to_id=quoted)

    page = client.get(_messages_url(room.conv), headers=room.alice_h).get_json()
    reply = [m for m in page["messages"] if m["text"] == "Atsakymas"][0]

    assert reply["replyTo"]["deleted"] is True
    assert reply["replyTo"]["text"] == ""
    assert reply["replyTo"]["senderId"] == room.bob["id"]


def test_a_dangling_quote_in_a_history_page_is_shaped_as_deleted(client, db, room):
    quoted = _seed_message(db, room.conv, room.bob["id"], "Dings", offset=18)
    _seed_message(db, room.conv, room.alice["id"], "Atsakymas", offset=19, reply_to_id=quoted)
    # the conftest connection has no FK enforcement, which is
    # exactly the FK-off write that leaves a ghost quote behind
    db.execute("DELETE FROM messages WHERE id = ?", (quoted,))
    db.commit()

    page = client.get(_messages_url(room.conv), headers=room.alice_h).get_json()
    reply = [m for m in page["messages"] if m["text"] == "Atsakymas"][0]

    assert reply["replyTo"] == {
        "id": quoted,
        "senderId": None,
        "senderName": None,
        "text": "",
        "imageUrl": None,
        "deleted": True,
    }


def test_a_quote_by_a_deleted_account_keeps_its_id_and_loses_its_name(
        client, db, make_user, room):
    ghost = make_user()
    db.execute("INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
               (room.conv, ghost["id"]))
    db.commit()
    quoted = _seed_message(db, room.conv, ghost["id"], "Palieku", offset=20)
    _seed_message(db, room.conv, room.alice["id"], "Atsakymas", offset=21, reply_to_id=quoted)
    db.execute("DELETE FROM users WHERE id = ?", (ghost["id"],))
    db.commit()

    page = client.get(_messages_url(room.conv), headers=room.alice_h).get_json()
    reply = [m for m in page["messages"] if m["text"] == "Atsakymas"][0]

    assert reply["replyTo"]["senderId"] == ghost["id"]
    assert reply["replyTo"]["senderName"] is None
    assert reply["replyTo"]["deleted"] is False


def test_a_quote_with_an_image_survives_the_round_trip(client, db, room):
    quoted = _seed_message(db, room.conv, room.bob["id"], "", offset=22,
                           image_url="/api/uploads/abc.jpg")
    _seed_message(db, room.conv, room.alice["id"], "Graži", offset=23, reply_to_id=quoted)

    page = client.get(_messages_url(room.conv), headers=room.alice_h).get_json()
    reply = [m for m in page["messages"] if m["text"] == "Graži"][0]

    assert reply["replyTo"]["imageUrl"] == "/api/uploads/abc.jpg"
    assert reply["replyTo"]["text"] == ""


def test_a_freshly_sent_reply_answers_with_its_quote(client, db, room):
    quoted = _seed_message(db, room.conv, room.bob["id"], "Klausimas?", offset=24)

    response = client.post(_messages_url(room.conv), headers=room.alice_h,
                           json={"text": "Atsakymas", "replyToId": quoted})

    assert response.status_code == 201
    assert response.get_json()["message"]["replyTo"]["id"] == quoted
    assert response.get_json()["message"]["replyTo"]["deleted"] is False


def test_a_replayed_send_still_carries_its_quote(client, db, room):
    # the idempotent-replay lookup shapes the quote through the
    # very same helper, off a LEFT JOIN instead of an inner one
    quoted = _seed_message(db, room.conv, room.bob["id"], "Klausimas?", offset=25)
    nonce = str(uuid.uuid4())
    first = client.post(_messages_url(room.conv), headers=room.alice_h,
                        json={"text": "Atsakymas", "replyToId": quoted,
                              "client_msg_id": nonce})

    replay = client.post(_messages_url(room.conv), headers=room.alice_h,
                         json={"text": "Atsakymas", "replyToId": quoted,
                               "client_msg_id": nonce})

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.get_json()["message"]["replyTo"] == first.get_json()["message"]["replyTo"]


def test_a_replayed_plain_send_has_a_null_quote(client, room):
    nonce = str(uuid.uuid4())
    client.post(_messages_url(room.conv), headers=room.alice_h,
                json={"text": "Vien tekstas", "client_msg_id": nonce})

    replay = client.post(_messages_url(room.conv), headers=room.alice_h,
                         json={"text": "Vien tekstas", "client_msg_id": nonce})

    assert replay.status_code == 200
    assert replay.get_json()["message"]["replyTo"] is None


def test_a_replayed_send_whose_quote_was_since_unsent_ships_the_placeholder(client, db, room):
    quoted = _seed_message(db, room.conv, room.bob["id"], "Klausimas?", offset=26)
    nonce = str(uuid.uuid4())
    client.post(_messages_url(room.conv), headers=room.alice_h,
                json={"text": "Atsakymas", "replyToId": quoted, "client_msg_id": nonce})
    client.delete(f"{_messages_url(room.conv)}/{quoted}", headers=room.bob_h)

    replay = client.post(_messages_url(room.conv), headers=room.alice_h,
                         json={"text": "Atsakymas", "replyToId": quoted,
                               "client_msg_id": nonce})

    assert replay.status_code == 200
    quote = replay.get_json()["message"]["replyTo"]
    assert quote["deleted"] is True
    assert quote["text"] == ""
    assert quote["senderId"] == room.bob["id"]
