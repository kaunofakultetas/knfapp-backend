# -----------------------------------------------------------
#  [*] Tests — chat search, the exhaustive slice
#
#  ONE slice of app/chat/routes.py, taken branch by branch:
#
#      search_messages  GET /conversations/<id>/messages/search
#      search_users     GET /users/search
#      _escape_like, _format_time, _epoch_ms, _get_socketio
#
#  What this module proves on top of the broad suite:
#
#    - The two searches guard DIFFERENTLY on purpose. An
#      in-room search 400s on a blank needle and on one past
#      200 characters, before it opens a connection; the
#      people picker answers {users: []} to anything under two
#      characters and carries no upper bound at all.
#    - The order of the gates is q, then membership, then
#      limit: an outsider with a blank q reads 400, an
#      outsider with a good q reads 403, and only a member
#      ever reaches the limit parser.
#    - The FTS5 arm and the escaped-LIKE fallback are NOT the
#      same search. FTS matches token PREFIXES and folds
#      Lithuanian diacritics; LIKE matches raw substrings with
#      ASCII-only folding. Both are pinned here, and the
#      fallback keeps %, _ and \ literal.
#    - search_users RANKS its twenty: an exact username, then
#      a display-name prefix, then the rest — each tier by
#      display name NOCASE with the id as the last tiebreak,
#      so the same query always answers the same people.
#    - The four helpers fail soft exactly as their banners
#      promise: a bad stamp is "" and 0, never an exception,
#      and _escape_like escapes the backslash FIRST so the
#      escape character it introduces is not re-escaped.
#
#  THREE tests cover one sharp edge: python-sqlite3 binds
#  TEXT NUL-terminated, so a NUL byte in q would truncate the
#  bound LIKE pattern down to "%" — the people picker paging
#  the directory the under-two-characters gate exists to
#  protect, and the in-room search answering the whole room.
#  Both routes now turn a NUL-bearing needle away empty.
#
#  Nothing here sleeps and nothing reaches the network; the
#  rate-limit windows move under time_machine.
# -----------------------------------------------------------

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import time_machine

import app as _app_module
from app.chat import routes as chat_routes
from app.chat.routes import _epoch_ms, _escape_like, _format_time, _get_socketio

# The route's own bounds, restated so a boundary test reads as
# a boundary test
_SEARCH_Q_MAX = 200
_SEARCH_TOTAL_CAP = 500
_MSG_SEARCH_BUDGET = 100
_USER_SEARCH_BUDGET = 120

# Every seeded stamp hangs off this base, comfortably in the
# past, so any stamp a route writes itself sorts after it
_BASE = datetime(2021, 5, 4, 9, 0, 0)




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
# the database: POST /conversations spends a rate budget these
# tests need elsewhere and cannot build a room of any shape.
# -----------------------------------------------------------

def _seed_room(db, member_ids, conv_type="group", title="Paieškos kambarys"):
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




# -----------------------------------------------------------
# _seed_message
# -----------------------------------------------------------
#
# One message row. `created_at` takes a raw string so a test
# can plant an unparseable stamp; `deleted` soft-deletes it
# the way delete_message would, `image_url` makes it a photo.
# The v20 sync triggers index the text as it lands, so a row
# seeded here is findable through messages_fts too.
# -----------------------------------------------------------

def _seed_message(db, conv_id, sender_id, text="labas", offset=1,
                  deleted=False, created_at=None, image_url=None):
    msg_id = str(uuid.uuid4())
    stamp = created_at if created_at is not None else _stamp(offset)

    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, created_at, deleted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, image_url, stamp, _stamp(offset) if deleted else None),
    )
    db.commit()

    return msg_id




# -----------------------------------------------------------
# _seed_many
# -----------------------------------------------------------
#
# `count` messages one second apart in ONE transaction — the
# cap and clamp tests want dozens of rows and a commit per row
# would make them minutes long. Returns the ids oldest-first.
# -----------------------------------------------------------

def _seed_many(db, conv_id, sender_id, count, text="rastas", first_offset=1):
    rows = [
        (str(uuid.uuid4()), conv_id, sender_id, f"{text} {i}", _stamp(first_offset + i))
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
# _disable_fts
# -----------------------------------------------------------
#
# Drops the v20 messages_fts shadow table and its sync
# triggers, so the route's MATCH raises OperationalError and
# search_messages takes the escaped-LIKE substring arm — what
# an SQLite build without FTS5 (or a pre-v20 database file)
# really does in production. Seed the messages BEFORE calling
# it: the triggers go with the table.
# -----------------------------------------------------------

def _disable_fts(db):
    for trigger in ("messages_fts_ai", "messages_fts_ad", "messages_fts_au"):
        db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    db.execute("DROP TABLE IF EXISTS messages_fts")
    db.commit()




# -----------------------------------------------------------
# URL builders — the two routes under test
# -----------------------------------------------------------

def _search_url(conv_id):
    return f"/api/chat/conversations/{conv_id}/messages/search"


_USERS_URL = "/api/chat/users/search"




# -----------------------------------------------------------
# fresh_limiter
# -----------------------------------------------------------
#
# The app's rate limiter (auth/routes.py _rate_limit_store) is
# PROCESS state, so the fresh-database fixtures do not reset
# it. Cleared on both sides so this file neither inherits a
# spent budget nor exports one to the modules that run after
# it.
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
# The standing cast: a two-person room with one message from
# bob in it, plus an outsider who belongs to nothing. Tokens
# come from the shared auth_headers fixture, so every one is
# minted by the real login route.
# -----------------------------------------------------------

@pytest.fixture
def room(db, make_user, auth_headers):
    alice = make_user()
    bob = make_user()
    outsider = make_user()

    conv_id = _seed_room(db, [alice["id"], bob["id"]])
    msg_id = _seed_message(db, conv_id, bob["id"], "labas rytas kolega", offset=1)

    return SimpleNamespace(
        alice=alice, alice_h=auth_headers(alice),
        bob=bob, bob_h=auth_headers(bob),
        outsider=outsider, outsider_h=auth_headers(outsider),
        conv=conv_id, msg=msg_id,
    )




# ===========================================================
#  _format_time — HH:MM of a naive UTC stamp, or ""
# ===========================================================


@pytest.mark.parametrize("stamp,expected", [
    ("2021-05-04T09:00:00", "09:00"),
    ("2021-05-04T00:00:00", "00:00"),
    ("2021-05-04T23:59:59", "23:59"),
    ("2021-05-04T09:07:00.123456", "09:07"),
    # the legacy space-separated form CPython 3.11+ parses
    ("2021-05-04 14:30:00", "14:30"),
    # the compact form, also accepted since 3.11
    ("20210504T143000", "14:30"),
    # date only — midnight
    ("2021-05-04", "00:00"),
])
def test_format_time_renders_the_hour_and_minute(stamp, expected):
    assert _format_time(stamp) == expected


def test_format_time_never_converts_an_offset_stamp():
    # the app writes naive UTC; an aware stamp is rendered as
    # written, NOT shifted to UTC — the clients format
    # createdAt themselves for exactly this reason
    assert _format_time("2021-05-04T09:00:00+03:00") == "09:00"


def test_format_time_accepts_the_zulu_suffix():
    assert _format_time("2021-05-04T09:00:00Z") == "09:00"


def test_format_time_pads_a_single_digit_hour():
    assert _format_time("2021-05-04T05:04:00") == "05:04"


@pytest.mark.parametrize("bad", [
    None,
    "",
    "   ",
    "labas",
    "not-a-date",
    "2021-13-45T99:99:99",
    "2021-05-04T09:00:00+99:00",
])
def test_format_time_answers_blank_for_an_unparseable_string(bad):
    # ValueError arm — one bad row must never break a listing
    assert _format_time(bad) == ""


@pytest.mark.parametrize("bad", [0, 1620118800, 3.5, b"2021-05-04T09:00:00", [], {}, object()])
def test_format_time_answers_blank_for_a_non_string(bad):
    # TypeError arm — fromisoformat refuses anything but str
    assert _format_time(bad) == ""


def test_format_time_answers_blank_for_a_datetime_object():
    # the fail-soft contract holds even for the type a caller
    # is most likely to pass by mistake
    assert _format_time(datetime(2021, 5, 4, 9, 0, 0)) == ""




# ===========================================================
#  _epoch_ms — epoch milliseconds of a naive UTC stamp, or 0
# ===========================================================


def test_epoch_ms_of_the_epoch_itself_is_zero():
    assert _epoch_ms("1970-01-01T00:00:00") == 0


def test_epoch_ms_counts_milliseconds_forward():
    assert _epoch_ms("1970-01-01T00:00:01") == 1000


def test_epoch_ms_is_pinned_to_utc_whatever_the_process_timezone_says():
    # the stamp is naive; .replace(tzinfo=utc) is what keeps
    # the number right on a container with a local TZ set
    assert _epoch_ms("2020-03-01T10:00:00") == 1583056800000


def test_epoch_ms_truncates_sub_millisecond_precision():
    assert _epoch_ms("1970-01-01T00:00:00.123456") == 123
    assert _epoch_ms("1970-01-01T00:00:00.999999") == 999


def test_epoch_ms_goes_negative_before_the_epoch():
    assert _epoch_ms("1969-12-31T23:59:59") == -1000


def test_epoch_ms_overrides_an_offset_instead_of_converting_it():
    # .replace(tzinfo=utc) REPLACES the offset — a +03:00
    # stamp reads exactly like the same naive wall time
    assert _epoch_ms("2020-03-01T10:00:00+03:00") == _epoch_ms("2020-03-01T10:00:00")


def test_epoch_ms_handles_the_far_future_without_overflowing():
    assert _epoch_ms("9999-12-31T23:59:59") > _epoch_ms("2020-03-01T10:00:00")


@pytest.mark.parametrize("bad", [None, "", "   ", "labas", "2021-13-45T99:99:99"])
def test_epoch_ms_answers_zero_for_an_unparseable_string(bad):
    # ValueError arm — ONE bad updated_at must not 500 the tab
    assert _epoch_ms(bad) == 0


@pytest.mark.parametrize("bad", [0, 1620118800, 3.5, b"1970-01-01T00:00:00", [], {}, object()])
def test_epoch_ms_answers_zero_for_a_non_string(bad):
    # TypeError arm
    assert _epoch_ms(bad) == 0


def test_epoch_ms_answers_a_falsy_zero_for_both_failure_and_the_epoch():
    # list_conversations reads `_epoch_ms(updated_at) or
    # _epoch_ms(created_at)` — the fallback keys off falsiness,
    # so both an unparseable stamp and a genuine 1970 row take
    # it. Nothing else in the codomain is falsy
    assert not _epoch_ms("visiskai ne data")
    assert not _epoch_ms("1970-01-01T00:00:00")
    assert _epoch_ms("1970-01-01T00:00:00.001")




# ===========================================================
#  _escape_like — \, % and _ made literal for LIKE ... ESCAPE
# ===========================================================


@pytest.mark.parametrize("raw,escaped", [
    ("", ""),
    ("labas", "labas"),
    ("%", "\\%"),
    ("_", "\\_"),
    ("\\", "\\\\"),
    ("%%", "\\%\\%"),
    ("__", "\\_\\_"),
    ("100%", "100\\%"),
    ("a_b%c", "a\\_b\\%c"),
    ("ačiū", "ačiū"),
    ("\U0001F44D", "\U0001F44D"),
])
def test_escape_like_escapes_exactly_the_three_metacharacters(raw, escaped):
    assert _escape_like(raw) == escaped


def test_escape_like_escapes_the_backslash_first():
    # order matters: escaping % first would then re-escape the
    # backslash IT introduced and the pattern would stop
    # matching. "\%" must become "\\\%", never "\\\\%"
    assert _escape_like("\\%") == "\\\\\\%"
    assert _escape_like("\\_") == "\\\\\\_"


def test_escape_like_leaves_a_lone_percent_a_single_escape_deep():
    assert _escape_like("%") == "\\%"
    assert _escape_like("%").count("\\") == 1


def test_escape_like_is_not_idempotent_so_it_is_applied_once():
    # a double pass doubles every escape — the route escapes
    # exactly once, on the way into the pattern
    assert _escape_like(_escape_like("%")) != _escape_like("%")


def test_escape_like_leaves_a_long_needle_the_same_length_when_it_is_clean():
    needle = "a" * _SEARCH_Q_MAX
    assert _escape_like(needle) == needle


def test_an_escaped_pattern_matches_the_metacharacter_literally_in_sqlite(db):
    # the contract the route depends on: pattern + ESCAPE '\'
    # matches % as a character, not as "anything"
    pattern = f"%{_escape_like('%')}%"

    assert db.execute("SELECT 'nuolaida 50% studentams' LIKE ? ESCAPE '\\'", (pattern,)).fetchone()[0] == 1
    assert db.execute("SELECT 'jokio zenklo' LIKE ? ESCAPE '\\'", (pattern,)).fetchone()[0] == 0


def test_an_escaped_underscore_matches_only_an_underscore_in_sqlite(db):
    pattern = f"%{_escape_like('a_b')}%"

    assert db.execute("SELECT 'xa_by' LIKE ? ESCAPE '\\'", (pattern,)).fetchone()[0] == 1
    assert db.execute("SELECT 'xaZby' LIKE ? ESCAPE '\\'", (pattern,)).fetchone()[0] == 0


def test_an_escaped_backslash_matches_only_a_backslash_in_sqlite(db):
    pattern = f"%{_escape_like(chr(92) + 'n')}%"

    assert db.execute("SELECT 'a\\nb' LIKE ? ESCAPE '\\'", (pattern,)).fetchone()[0] == 1
    assert db.execute("SELECT 'anb' LIKE ? ESCAPE '\\'", (pattern,)).fetchone()[0] == 0




# ===========================================================
#  _get_socketio — the deferred lookup of the package singleton
# ===========================================================


def test_get_socketio_returns_the_package_singleton():
    # Looked up through the MODULE, never bound at import time:
    # sibling suites call importlib.reload on app to rebuild the
    # factory, which rebinds app.socketio to a fresh object. A
    # captured reference would then compare an old instance
    # against the deferred lookup's new one and fail for a reason
    # that has nothing to do with _get_socketio
    assert _get_socketio() is _app_module.socketio


def test_get_socketio_answers_the_same_instance_every_call():
    assert _get_socketio() is _get_socketio()


def test_get_socketio_needs_no_app_or_request_context():
    # the whole point of deferring the import: the lookup costs
    # nothing and works wherever it is called from
    assert _get_socketio() is not None


def test_get_socketio_hands_back_a_bound_server_after_create_app(app):
    sio = _get_socketio()

    # send_message reads sio.server.manager.rooms and calls
    # start_background_task off this very object
    assert sio.server is not None
    assert callable(sio.start_background_task)
    assert callable(sio.emit)


def test_get_socketio_keeps_no_module_level_reference():
    # chat/routes.py holds no module-level reference — the
    # attribute is fetched from the package on every call
    assert not hasattr(chat_routes, "socketio")




# ===========================================================
#  search_messages — the q guard (runs before ANY connection)
# ===========================================================


def test_a_missing_q_is_refused(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h)

    assert response.status_code == 400
    assert response.get_json()["error"] == "q parameter is required and must not be empty"


def test_an_empty_q_is_refused(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h, query_string={"q": ""})

    assert response.status_code == 400


@pytest.mark.parametrize("blank", [" ", "   ", "\t", "\n", "\r\n", " \t \n ", "\u00a0"])
def test_a_q_of_nothing_but_whitespace_is_refused(client, room, blank):
    # str.strip() takes unicode whitespace too, so a
    # non-breaking space is just as blank as a plain one
    response = client.get(_search_url(room.conv), headers=room.alice_h, query_string={"q": blank})

    assert response.status_code == 400


def test_a_single_character_q_is_accepted(client, db, room):
    # unlike the people picker, ONE character is a real search
    _seed_message(db, room.conv, room.bob["id"], "x marks", offset=5)

    response = client.get(_search_url(room.conv), headers=room.alice_h, query_string={"q": "x"})

    assert response.status_code == 200
    assert len(response.get_json()["messages"]) == 1


def test_a_q_of_exactly_the_limit_is_accepted(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "a" * _SEARCH_Q_MAX})

    assert response.status_code == 200


def test_a_q_one_character_past_the_limit_is_refused(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "a" * (_SEARCH_Q_MAX + 1)})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"q must be at most {_SEARCH_Q_MAX} characters"


def test_a_huge_q_is_refused_rather_than_searched(client, room):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "a" * 20000})

    assert response.status_code == 400


def test_the_length_limit_is_measured_after_stripping(client, room):
    # 200 real characters wrapped in whitespace still fits
    padded = "   " + "a" * _SEARCH_Q_MAX + "   "

    response = client.get(_search_url(room.conv), headers=room.alice_h, query_string={"q": padded})

    assert response.status_code == 200


def test_the_length_limit_counts_characters_not_bytes(client, room):
    # len() over a str is code points — 200 Lithuanian letters
    # fit even though they are 400 bytes on the wire
    assert client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "ą" * _SEARCH_Q_MAX}).status_code == 200
    assert client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "ą" * (_SEARCH_Q_MAX + 1)}).status_code == 400


def test_the_q_guard_runs_before_the_membership_gate(client, room):
    # STEP 1 is the needle, STEP 2 the membership — an outsider
    # with a blank q reads 400, not 403
    response = client.get(_search_url(room.conv), headers=room.outsider_h, query_string={"q": " "})

    assert response.status_code == 400


def test_the_length_guard_also_runs_before_the_membership_gate(client, room):
    response = client.get(_search_url(room.conv), headers=room.outsider_h,
                          query_string={"q": "a" * (_SEARCH_Q_MAX + 1)})

    assert response.status_code == 400


def test_a_good_needle_from_an_outsider_is_forbidden(client, room):
    response = client.get(_search_url(room.conv), headers=room.outsider_h,
                          query_string={"q": "labas"})

    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"


def test_searching_an_unknown_conversation_is_forbidden(client, room):
    response = client.get(_search_url(str(uuid.uuid4())), headers=room.alice_h,
                          query_string={"q": "labas"})

    assert response.status_code == 403


def test_a_member_who_left_can_no_longer_search_the_room(client, db, room):
    # the gate reads the membership row live, so a leaver loses
    # the history the moment their row goes
    db.execute("DELETE FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
               (room.conv, room.alice["id"]))
    db.commit()

    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas"})

    assert response.status_code == 403


def test_searching_needs_a_token(client, room):
    response = client.get(_search_url(room.conv), query_string={"q": "labas"})

    assert response.status_code == 401


def test_searching_with_a_junk_token_is_unauthorized(client, room):
    response = client.get(_search_url(room.conv), query_string={"q": "labas"},
                          headers={"Authorization": "Bearer ne-tikras"})

    assert response.status_code == 401


def test_the_search_route_is_get_only(client, room):
    response = client.post(_search_url(room.conv), headers=room.alice_h, json={"q": "labas"})

    assert response.status_code == 405




# ===========================================================
#  search_messages — the limit parser and its 1..50 clamp
# ===========================================================


def test_the_default_limit_is_twenty(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 25)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas"}).get_json()

    assert len(body["messages"]) == 20
    # the total ignores the page size
    assert body["total"] == 25


def test_an_explicit_limit_is_honoured(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 7)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": "3"}).get_json()

    assert len(body["messages"]) == 3
    assert body["total"] == 7


def test_a_limit_one_past_the_ceiling_is_clamped_to_fifty(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 55)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": "51"}).get_json()

    assert len(body["messages"]) == 50
    assert body["total"] == 55


def test_an_absurd_limit_is_clamped_to_fifty(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 55)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": "999999999999999999999"}).get_json()

    assert len(body["messages"]) == 50


def test_a_negative_limit_never_reaches_sqlite_as_no_limit(client, db, room):
    # LIMIT -1 means "everything" in SQLite; the clamp is what
    # stops a client from paging a whole conversation
    _seed_many(db, room.conv, room.bob["id"], 30)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": "-1"}).get_json()

    assert len(body["messages"]) == 1


@pytest.mark.parametrize("bad", ["abc", "3.0", "1e3", "0x10", "", " ", "null", "20,", "٥٫٥", "NaN"])
def test_a_non_integer_limit_is_refused(client, room, bad):
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas", "limit": bad})

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be an integer"


def test_a_limit_padded_with_spaces_is_still_an_integer(client, db, room):
    # int() strips surrounding whitespace — pinned so a future
    # rewrite does not silently turn it into a 400
    _seed_many(db, room.conv, room.bob["id"], 9)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": " 4 "}).get_json()

    assert len(body["messages"]) == 4


def test_a_signed_limit_is_accepted(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 9)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": "+4"}).get_json()

    assert len(body["messages"]) == 4


def test_a_non_ascii_decimal_limit_is_accepted_by_int(client, db, room):
    # int() takes any Unicode decimal digit — ٥ is five
    _seed_many(db, room.conv, room.bob["id"], 9)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": "\u0665"}).get_json()

    assert len(body["messages"]) == 5


def test_the_membership_gate_runs_before_the_limit_is_parsed(client, room):
    # the limit is parsed INSIDE the connection, after STEP 2 —
    # an outsider sending garbage still reads 403
    response = client.get(_search_url(room.conv), headers=room.outsider_h,
                          query_string={"q": "labas", "limit": "abc"})

    assert response.status_code == 403




# ===========================================================
#  search_messages — the FTS5 arm (the production path)
# ===========================================================


def test_a_word_in_the_middle_of_a_message_is_found(client, room):
    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rytas"}).get_json()

    assert [m["id"] for m in body["messages"]] == [room.msg]
    assert body["total"] == 1


def test_a_token_prefix_is_found(client, room):
    # "..."* — the needle matches the START of a token
    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "ryt"}).get_json()

    assert len(body["messages"]) == 1


def test_a_mid_token_substring_is_not_found_on_the_fts_arm(client, room):
    # prefix semantics, not substring — "ytas" is inside
    # "rytas" but starts no token, so FTS answers nothing and
    # the route does NOT fall back to LIKE
    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "ytas"}).get_json()

    assert body["messages"] == []
    assert body["total"] == 0


def test_the_fts_arm_folds_lithuanian_diacritics(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "ačiū už pagalbą", offset=6)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "aciu"}).get_json()

    assert len(body["messages"]) == 1


def test_the_fts_arm_ignores_case_including_lithuanian_case(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "ačiū už pagalbą", offset=6)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "AČIŪ"}).get_json()

    assert len(body["messages"]) == 1


def test_a_multi_word_needle_matches_as_a_phrase(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "rytas labas atvirkščiai", offset=7)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas rytas"}).get_json()

    # only the adjacent pair, not the reversed one
    assert [m["id"] for m in body["messages"]] == [room.msg]


def test_a_quote_in_the_needle_is_doubled_instead_of_breaking_the_match(client, room):
    # the fts_query wraps q in quotes and doubles the inner
    # ones — an unbalanced quote would be a syntax error
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": '" OR 1=1'})

    assert response.status_code == 200
    assert response.get_json()["messages"] == []


@pytest.mark.parametrize("needle", [
    "%", "_", "*", "^", "NOT", "AND", "()", '"', "\\", "-", "NEAR(a b)", "\x00", "\U0001F44D",
])
def test_an_fts_operator_in_the_needle_is_text_not_syntax(client, room, needle):
    # everything rides inside one quoted phrase, so no needle a
    # client can type turns into an FTS expression or a 500
    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": needle})

    assert response.status_code == 200


def test_a_null_byte_needle_matches_no_message(client, db, room):
    # the same truncation as the people picker: the quoted FTS
    # phrase would lose its closing quote and the LIKE fallback
    # would then bind a bare '%' and answer the WHOLE room
    _seed_message(db, room.conv, room.bob["id"], "visai kitas tekstas", offset=17)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "\x00"}).get_json()

    assert body == {"messages": [], "total": 0}


def test_a_photo_only_message_matches_nothing(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "", offset=8, image_url="/api/uploads/a.jpg")

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas"}).get_json()

    assert [m["id"] for m in body["messages"]] == [room.msg]




# ===========================================================
#  search_messages — the escaped-LIKE fallback
#
#  Reached when messages_fts is gone: a build without FTS5 or
#  a database file older than migration v20. This arm is a raw
#  substring scan, and the only one where q's LIKE
#  metacharacters are observable.
# ===========================================================


def test_the_fallback_finds_the_mid_token_substring_fts_misses(client, db, room):
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "ytas"}).get_json()

    assert [m["id"] for m in body["messages"]] == [room.msg]
    assert body["total"] == 1


def test_the_fallback_folds_ascii_case_only(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "ačiū už pagalbą", offset=6)
    _disable_fts(db)

    # ASCII folds...
    assert len(client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "LABAS"}).get_json()["messages"]) == 1
    # ...Lithuanian does not, and the diacritic is not stripped
    assert client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "AČIŪ"}).get_json()["messages"] == []
    assert client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "aciu"}).get_json()["messages"] == []
    assert len(client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "ačiū"}).get_json()["messages"]) == 1


def test_a_lone_percent_does_not_match_every_message_on_the_fallback(client, db, room):
    hit = _seed_message(db, room.conv, room.bob["id"], "nuolaida 50% studentams", offset=9)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "%"}).get_json()

    assert [m["id"] for m in body["messages"]] == [hit]
    assert body["total"] == 1


def test_a_lone_underscore_does_not_match_any_single_character(client, db, room):
    hit = _seed_message(db, room.conv, room.bob["id"], "failas_2021 prisegtas", offset=9)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "_"}).get_json()

    assert [m["id"] for m in body["messages"]] == [hit]


def test_a_backslash_needle_matches_the_backslash_literally(client, db, room):
    hit = _seed_message(db, room.conv, room.bob["id"], "kelias c:\\namai\\failas", offset=9)
    _seed_message(db, room.conv, room.bob["id"], "kelias c:namaifailas", offset=10)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "\\namai"}).get_json()

    assert [m["id"] for m in body["messages"]] == [hit]


def test_a_needle_of_only_wildcards_matches_only_that_text(client, db, room):
    hit = _seed_message(db, room.conv, room.bob["id"], "cia yra %_ zenklai", offset=9)
    _seed_message(db, room.conv, room.bob["id"], "cia jokiu zenklu nera", offset=10)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "%_"}).get_json()

    assert [m["id"] for m in body["messages"]] == [hit]


def test_the_fallback_total_is_exact_below_the_cap(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 12)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": "5"}).get_json()

    assert len(body["messages"]) == 5
    assert body["total"] == 12


def test_the_fallback_honours_the_same_clamp(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 55)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "rastas", "limit": "51"}).get_json()

    assert len(body["messages"]) == 50


def test_both_arms_ship_the_same_hit_shape(client, db, room):
    fts_hit = client.get(_search_url(room.conv), headers=room.alice_h,
                         query_string={"q": "labas"}).get_json()["messages"][0]
    _disable_fts(db)
    like_hit = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas"}).get_json()["messages"][0]

    assert fts_hit == like_hit


def test_the_fallback_still_excludes_unsent_messages(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "labas istrintas", offset=11, deleted=True)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas"}).get_json()

    assert [m["id"] for m in body["messages"]] == [room.msg]
    assert body["total"] == 1


def test_the_fallback_never_reaches_another_conversation(client, db, room, make_user):
    stranger = make_user()
    other_conv = _seed_room(db, [room.alice["id"], stranger["id"]])
    _seed_message(db, other_conv, stranger["id"], "labas kitame kambaryje", offset=12)
    _disable_fts(db)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas"}).get_json()

    assert [m["id"] for m in body["messages"]] == [room.msg]
    assert body["total"] == 1




# ===========================================================
#  search_messages — the shaped answer
# ===========================================================


@pytest.mark.contract
def test_a_search_hit_carries_exactly_the_fields_the_app_reads(client, room):
    hit = client.get(_search_url(room.conv), headers=room.alice_h,
                     query_string={"q": "labas"}).get_json()["messages"][0]

    assert set(hit) == {
        "id", "conversationId", "senderId", "senderName", "senderAvatar",
        "text", "imageUrl", "time", "createdAt", "isOwn",
    }


@pytest.mark.contract
def test_the_search_envelope_is_messages_and_total_only(client, room):
    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas"}).get_json()

    assert set(body) == {"messages", "total"}


def test_a_hit_carries_the_senders_name_and_portrait(client, db, room):
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?",
               ("/api/uploads/bob.png", room.bob["id"]))
    db.commit()

    hit = client.get(_search_url(room.conv), headers=room.alice_h,
                     query_string={"q": "labas"}).get_json()["messages"][0]

    assert hit["senderId"] == room.bob["id"]
    assert hit["senderName"] == room.bob["username"].title()
    assert hit["senderAvatar"] == "/api/uploads/bob.png"


def test_a_hit_without_a_portrait_reports_null(client, room):
    hit = client.get(_search_url(room.conv), headers=room.alice_h,
                     query_string={"q": "labas"}).get_json()["messages"][0]

    assert hit["senderAvatar"] is None


def test_is_own_is_true_only_for_the_callers_own_messages(client, room):
    mine = client.get(_search_url(room.conv), headers=room.bob_h,
                      query_string={"q": "labas"}).get_json()["messages"][0]
    theirs = client.get(_search_url(room.conv), headers=room.alice_h,
                        query_string={"q": "labas"}).get_json()["messages"][0]

    assert mine["isOwn"] is True
    assert theirs["isOwn"] is False


def test_a_hit_carries_the_conversation_id_from_the_url(client, room):
    hit = client.get(_search_url(room.conv), headers=room.alice_h,
                     query_string={"q": "labas"}).get_json()["messages"][0]

    assert hit["conversationId"] == room.conv


def test_a_hit_keeps_its_photo_url(client, db, room):
    hit_id = _seed_message(db, room.conv, room.bob["id"], "labas su nuotrauka",
                           offset=13, image_url="/api/uploads/foto.jpg")

    hits = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "nuotrauka"}).get_json()["messages"]

    assert [m["id"] for m in hits] == [hit_id]
    assert hits[0]["imageUrl"] == "/api/uploads/foto.jpg"


def test_the_hit_time_is_utc_hhmm_of_created_at(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "labas vakaras", created_at="2021-05-04T21:45:07")

    hit = client.get(_search_url(room.conv), headers=room.alice_h,
                     query_string={"q": "vakaras"}).get_json()["messages"][0]

    assert hit["createdAt"] == "2021-05-04T21:45:07"
    assert hit["time"] == "21:45"


def test_a_hit_with_an_unparseable_stamp_gets_a_blank_time(client, db, room):
    # _format_time's fail-soft arm reached through the route:
    # one corrupt row must not 500 the search
    _seed_message(db, room.conv, room.bob["id"], "labas sugadintas", created_at="visai ne data")

    hits = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "sugadintas"}).get_json()["messages"]

    assert hits[0]["time"] == ""
    assert hits[0]["createdAt"] == "visai ne data"


def test_search_hits_come_back_oldest_first(client, db, room):
    second = _seed_message(db, room.conv, room.bob["id"], "labas antras", offset=20)
    third = _seed_message(db, room.conv, room.bob["id"], "labas trecias", offset=30)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas"}).get_json()

    assert [m["id"] for m in body["messages"]] == [room.msg, second, third]


def test_the_newest_hits_survive_when_the_limit_bites(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "labas antras", offset=20)
    third = _seed_message(db, room.conv, room.bob["id"], "labas trecias", offset=30)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas", "limit": "1"}).get_json()

    assert [m["id"] for m in body["messages"]] == [third]
    assert body["total"] == 3


def test_a_search_that_matches_nothing_is_an_empty_page(client, room):
    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "niekonerasi"}).get_json()

    assert body == {"messages": [], "total": 0}


def test_an_empty_room_answers_an_empty_page(client, db, make_user, auth_headers):
    loner = make_user()
    friend = make_user()
    conv_id = _seed_room(db, [loner["id"], friend["id"]])

    body = client.get(_search_url(conv_id), headers=auth_headers(loner),
                      query_string={"q": "labas"}).get_json()

    assert body == {"messages": [], "total": 0}


def test_an_unsent_message_is_never_a_hit(client, db, room):
    _seed_message(db, room.conv, room.bob["id"], "labas istrintas", offset=14, deleted=True)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "istrintas"}).get_json()

    assert body == {"messages": [], "total": 0}


def test_a_deactivated_senders_messages_stay_searchable(client, db, room):
    # search_users hides a disabled account, but the history it
    # wrote must not vanish from the room it wrote in
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (room.bob["id"],))
    db.commit()

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas"}).get_json()

    assert [m["id"] for m in body["messages"]] == [room.msg]


def test_search_never_reaches_into_another_conversation(client, db, room, make_user):
    stranger = make_user()
    other_conv = _seed_room(db, [room.alice["id"], stranger["id"]])
    _seed_message(db, other_conv, stranger["id"], "labas kitur", offset=15)

    body = client.get(_search_url(room.conv), headers=room.alice_h,
                      query_string={"q": "labas"}).get_json()

    assert [m["id"] for m in body["messages"]] == [room.msg]
    assert body["total"] == 1


def test_both_members_see_the_same_hits(client, room):
    alice_hits = client.get(_search_url(room.conv), headers=room.alice_h,
                            query_string={"q": "labas"}).get_json()["messages"]
    bob_hits = client.get(_search_url(room.conv), headers=room.bob_h,
                          query_string={"q": "labas"}).get_json()["messages"]

    assert [m["id"] for m in alice_hits] == [m["id"] for m in bob_hits]


def test_repeating_a_search_answers_the_same_page(client, db, room):
    _seed_many(db, room.conv, room.bob["id"], 8)

    first = client.get(_search_url(room.conv), headers=room.alice_h,
                       query_string={"q": "rastas", "limit": "5"}).get_json()
    second = client.get(_search_url(room.conv), headers=room.alice_h,
                        query_string={"q": "rastas", "limit": "5"}).get_json()

    assert first == second


def test_markup_in_a_hit_is_escaped_on_the_wire(client, db, room):
    # every body goes through the app's escaping JSON provider,
    # so a search hit can never ship raw markup to a client
    # that renders HTML
    _seed_message(db, room.conv, room.bob["id"], "labas <script>alert(1)</script>", offset=16)

    response = client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "alert"})

    assert b"<script>" not in response.data
    assert b"&lt;script&gt;" in response.data




# ===========================================================
#  search_messages — the per-user budget
# ===========================================================


@pytest.mark.slow
def test_the_search_budget_is_per_user_and_frees_up_after_its_window(client, room):
    with time_machine.travel(datetime(2026, 4, 1, 8, 0, 0), tick=False) as traveller:
        # a needle too short to search still spends an attempt:
        # the decorator runs before the q guard
        for _ in range(_MSG_SEARCH_BUDGET):
            client.get(_search_url(room.conv), headers=room.alice_h, query_string={"q": ""})

        spent = client.get(_search_url(room.conv), headers=room.alice_h,
                           query_string={"q": "labas"})
        assert spent.status_code == 429
        assert spent.get_json()["code"] == "rate_limited"
        assert int(spent.headers["Retry-After"]) >= 1

        # another member's budget is untouched
        assert client.get(_search_url(room.conv), headers=room.bob_h,
                          query_string={"q": "labas"}).status_code == 200

        # and the five-minute window releases it again
        traveller.shift(301)
        assert client.get(_search_url(room.conv), headers=room.alice_h,
                          query_string={"q": "labas"}).status_code == 200




# ===========================================================
#  search_users — the two-character warm-up gate
# ===========================================================


def test_a_people_search_without_q_answers_empty(client, actor):
    _user, headers = actor

    response = client.get(_USERS_URL, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"users": []}


@pytest.mark.parametrize("short", ["", "a", " ", "  a  ", "\t", "ą", "\u00a0"])
def test_a_needle_under_two_characters_answers_empty_with_200(client, make_user, actor, short):
    # the picker calls this on every keystroke — one letter is
    # a warm-up, not a directory page
    make_user(username="abcdefgh", display_name="Abcdefgh Pavarde")
    _user, headers = actor

    response = client.get(_USERS_URL, headers=headers, query_string={"q": short})

    assert response.status_code == 200
    assert response.get_json() == {"users": []}


def test_exactly_two_characters_is_a_real_search(client, make_user, actor):
    target = make_user(username="zydrunas", display_name="Zydrunas Petraitis")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "zy"}).get_json()

    assert [u["id"] for u in body["users"]] == [target["id"]]


def test_the_two_character_floor_is_measured_after_stripping(client, make_user, actor):
    target = make_user(username="zydrunas", display_name="Zydrunas Petraitis")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "   zy   "}).get_json()

    assert [u["id"] for u in body["users"]] == [target["id"]]


def test_the_people_search_has_no_upper_length_bound(client, actor):
    # unlike the in-room search there is no 200-character 400
    # here — the rate budget is what bounds this route
    _user, headers = actor

    response = client.get(_USERS_URL, headers=headers, query_string={"q": "z" * 5000})

    assert response.status_code == 200
    assert response.get_json() == {"users": []}


def test_the_people_search_needs_a_token(client):
    response = client.get(_USERS_URL, query_string={"q": "zy"})

    assert response.status_code == 401


def test_the_people_search_route_is_get_only(client, actor):
    _user, headers = actor

    response = client.post(_USERS_URL, headers=headers, json={"q": "zy"})

    assert response.status_code == 405




# ===========================================================
#  search_users — who is in the answer
# ===========================================================


def test_a_username_substring_matches(client, make_user, actor):
    target = make_user(username="kregzdute", display_name="Visai Kitas Vardas")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "regzd"}).get_json()

    assert [u["id"] for u in body["users"]] == [target["id"]]


def test_a_display_name_substring_matches(client, make_user, actor):
    target = make_user(username="qqqwww1", display_name="Ona Kregzdiene")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "regzd"}).get_json()

    assert [u["id"] for u in body["users"]] == [target["id"]]


def test_the_caller_is_never_offered_to_themselves(client, make_user, auth_headers):
    me = make_user(username="kregzdute", display_name="Kregzdute Pati")
    headers = auth_headers(me)

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzd"}).get_json()

    assert body == {"users": []}


def test_a_deactivated_account_is_never_offered(client, make_user, actor):
    make_user(username="kregzdute", display_name="Isjungta Paskyra", active=0)
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzd"}).get_json()

    assert body == {"users": []}


def test_a_deactivated_account_is_hidden_even_on_an_exact_username(client, make_user, actor):
    make_user(username="kregzdute", display_name="Isjungta Paskyra", active=0)
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzdute"}).get_json()

    assert body == {"users": []}


@pytest.mark.parametrize("role", ["student", "teacher", "admin", "curator"])
def test_every_role_is_offered_by_the_picker(client, make_user, actor, role):
    target = make_user(username=f"kregzde{role}", display_name=f"Kregzde {role.title()}", role=role)
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert [u["id"] for u in body["users"]] == [target["id"]]
    assert body["users"][0]["role"] == role


def test_a_needle_matching_nobody_answers_empty(client, make_user, actor):
    make_user(username="kregzdute", display_name="Ona Kregzdiene")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "niekonerasi"}).get_json()

    assert body == {"users": []}


def test_an_injection_attempt_is_just_a_needle(client, make_user, actor):
    make_user(username="kregzdute", display_name="Ona Kregzdiene")
    _user, headers = actor

    response = client.get(_USERS_URL, headers=headers,
                          query_string={"q": "' OR 1=1 --"})

    assert response.status_code == 200
    assert response.get_json() == {"users": []}


@pytest.mark.parametrize("needle", ["\U0001F44D\U0001F44D", "..", "--", "%%%", "\x00"])
def test_a_hostile_needle_is_answered_not_crashed(client, make_user, actor, needle):
    # a lone NUL is in here for the crash check only — what it
    # MATCHES is the pair of tests below
    make_user(username="kregzdute", display_name="Ona Kregzdiene")
    _user, headers = actor

    response = client.get(_USERS_URL, headers=headers, query_string={"q": needle})

    assert response.status_code == 200


def test_a_null_byte_needle_does_not_page_the_directory(client, make_user, actor):
    # python-sqlite3 binds TEXT NUL-terminated, so this needle
    # would reach SQLite as a bare '%' — the gate turns a
    # NUL-bearing q away with the under-two-characters ones
    make_user(username="kregzdute", display_name="Ona Kregzdiene")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "\x00\x00"}).get_json()

    assert body == {"users": []}


def test_a_null_byte_does_not_turn_the_needle_into_a_suffix_match(client, make_user, actor):
    # the same truncation from the other side: '%kregzd\x00…%'
    # would bind as '%kregzd' and answer a SUFFIX match nobody
    # asked for
    make_user(username="qqqwwwj", display_name="Ona Kregzd")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers,
                      query_string={"q": "kregzd\x00zzzzz"}).get_json()

    # nothing contains the needle as typed, NUL and all
    assert body == {"users": []}


def test_ascii_case_is_folded_but_lithuanian_case_is_not(client, make_user, actor):
    make_user(username="qqqwww2", display_name="Ona Ąžuolas")
    _user, headers = actor

    # LIKE folds ASCII...
    assert len(client.get(_USERS_URL, headers=headers,
                          query_string={"q": "ONA"}).get_json()["users"]) == 1
    # ...and leaves Lithuanian letters alone, in BOTH
    # directions: only the stored spelling matches
    assert client.get(_USERS_URL, headers=headers,
                      query_string={"q": "ĄŽUOLAS"}).get_json()["users"] == []
    assert client.get(_USERS_URL, headers=headers,
                      query_string={"q": "ąžuolas"}).get_json()["users"] == []
    assert len(client.get(_USERS_URL, headers=headers,
                          query_string={"q": "Ąžuolas"}).get_json()["users"]) == 1




# ===========================================================
#  search_users — the escaped LIKE
# ===========================================================


def test_an_underscore_needle_matches_the_underscore_literally(client, make_user, actor):
    target = make_user(username="ab_cd", display_name="Su Pabraukimu")
    make_user(username="abxcd", display_name="Be Pabraukimo")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "ab_cd"}).get_json()

    assert [u["id"] for u in body["users"]] == [target["id"]]


def test_a_percent_needle_matches_the_percent_literally(client, make_user, actor):
    target = make_user(username="qqqwww3", display_name="Nuolaida 50% Studentams")
    make_user(username="qqqwww4", display_name="Nuolaida 50 Studentams")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "50%"}).get_json()

    assert [u["id"] for u in body["users"]] == [target["id"]]


def test_a_backslash_needle_matches_the_backslash_literally(client, make_user, actor):
    target = make_user(username="qqqwww5", display_name="Kelias c:\\namai")
    make_user(username="qqqwww6", display_name="Kelias c:namai")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "c:\\namai"}).get_json()

    assert [u["id"] for u in body["users"]] == [target["id"]]


def test_a_needle_of_only_wildcards_does_not_list_the_directory(client, make_user, actor):
    make_user(username="qqqwww7", display_name="Pirmas Zmogus")
    make_user(username="qqqwww8", display_name="Antras Zmogus")
    _user, headers = actor

    assert client.get(_USERS_URL, headers=headers, query_string={"q": "%%"}).get_json() == {"users": []}
    assert client.get(_USERS_URL, headers=headers, query_string={"q": "__"}).get_json() == {"users": []}
    assert client.get(_USERS_URL, headers=headers, query_string={"q": "%_"}).get_json() == {"users": []}




# ===========================================================
#  search_users — the ranking, which is the whole point
# ===========================================================


def test_the_three_tiers_outrank_alphabetical_order(client, make_user, actor):
    # display names deliberately sort the other way round, so
    # only the CASE tiers can produce this order
    substring = make_user(username="qqqwww9", display_name="Alfa Kregzde")
    prefix = make_user(username="qqqwwwa", display_name="Kregzde Beta")
    exact = make_user(username="kregzde", display_name="Zeta Zmogus")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert [u["id"] for u in body["users"]] == [exact["id"], prefix["id"], substring["id"]]


def test_an_exact_username_wins_case_insensitively(client, make_user, actor):
    exact = make_user(username="testas", display_name="Zeta Zmogus")
    make_user(username="qqqwwwb", display_name="Testas Alfa")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "TESTAS"}).get_json()

    assert body["users"][0]["id"] == exact["id"]


def test_a_display_name_prefix_outranks_a_mere_substring(client, make_user, actor):
    substring = make_user(username="qqqwwwc", display_name="Alfa Kregzde")
    prefix = make_user(username="qqqwwwd", display_name="Kregzde Zeta")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert [u["id"] for u in body["users"]] == [prefix["id"], substring["id"]]


def test_a_username_only_hit_still_lands_in_the_last_tier(client, make_user, actor):
    by_username = make_user(username="xkregzdex", display_name="Zeta Zmogus")
    by_display = make_user(username="qqqwwwe", display_name="Kregzde Alfa")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert [u["id"] for u in body["users"]] == [by_display["id"], by_username["id"]]


def test_one_tier_is_ordered_by_display_name_ignoring_case(client, make_user, actor):
    # binary collation would put "Beta" (B) before "alfa" (a);
    # NOCASE is what puts alfa first
    lower = make_user(username="qqqwwwf", display_name="alfa kregzde")
    upper = make_user(username="qqqwwwg", display_name="Beta kregzde")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert [u["id"] for u in body["users"]] == [lower["id"], upper["id"]]


def test_identical_display_names_are_broken_by_id(client, make_user, actor):
    twins = [make_user(username=f"qqqwwwh{i}", display_name="Dvyniai Kregzde") for i in range(3)]
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert [u["id"] for u in body["users"]] == sorted(t["id"] for t in twins)


def test_the_same_query_always_answers_the_same_people(client, make_user, actor):
    for i in range(6):
        make_user(username=f"qqqwwwi{i}", display_name="Dvyniai Kregzde")
    _user, headers = actor

    first = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()
    second = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert first == second




# ===========================================================
#  search_users — the fixed page of twenty
# ===========================================================


def test_exactly_twenty_matches_all_come_back(client, make_user, actor):
    for i in range(20):
        make_user(username=f"kregzde{i:02d}", display_name=f"Kregzde {i:02d}")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert len(body["users"]) == 20


def test_the_twenty_first_match_is_cut(client, make_user, actor):
    for i in range(21):
        make_user(username=f"kregzde{i:02d}", display_name=f"Kregzde {i:02d}")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert len(body["users"]) == 20


def test_the_kept_twenty_are_the_ranked_twenty_not_a_random_scan(client, make_user, actor):
    for i in range(25):
        make_user(username=f"kregzde{i:02d}", display_name=f"Kregzde {i:02d}")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzde"}).get_json()

    assert [u["displayName"] for u in body["users"]] == [f"Kregzde {i:02d}" for i in range(20)]




# ===========================================================
#  search_users — the wire shape
# ===========================================================


@pytest.mark.contract
def test_a_people_hit_carries_exactly_five_fields(client, make_user, actor):
    make_user(username="kregzdute", display_name="Ona Kregzdiene", role="teacher")
    _user, headers = actor

    hit = client.get(_USERS_URL, headers=headers,
                     query_string={"q": "kregzd"}).get_json()["users"][0]

    assert set(hit) == {"id", "username", "displayName", "avatarUrl", "role"}


@pytest.mark.contract
def test_a_people_hit_never_leaks_an_email_or_a_hash(client, make_user, actor):
    make_user(username="kregzdute", display_name="Ona Kregzdiene")
    _user, headers = actor

    response = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzd"})

    assert b"@knf.vu.lt" not in response.data
    assert b"password" not in response.data


def test_a_people_hit_carries_the_portrait_when_there_is_one(client, db, make_user, actor):
    target = make_user(username="kregzdute", display_name="Ona Kregzdiene")
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?",
               ("/api/uploads/ona.png", target["id"]))
    db.commit()
    _user, headers = actor

    hit = client.get(_USERS_URL, headers=headers,
                     query_string={"q": "kregzd"}).get_json()["users"][0]

    assert hit["avatarUrl"] == "/api/uploads/ona.png"


def test_a_people_hit_reports_a_missing_portrait_as_null(client, make_user, actor):
    make_user(username="kregzdute", display_name="Ona Kregzdiene")
    _user, headers = actor

    hit = client.get(_USERS_URL, headers=headers,
                     query_string={"q": "kregzd"}).get_json()["users"][0]

    assert hit["avatarUrl"] is None


def test_the_people_envelope_is_users_only(client, make_user, actor):
    make_user(username="kregzdute", display_name="Ona Kregzdiene")
    _user, headers = actor

    body = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzd"}).get_json()

    assert set(body) == {"users"}


def test_markup_in_a_display_name_is_escaped_on_the_wire(client, make_user, actor):
    make_user(username="kregzdute", display_name="Ona <b>Kregzdiene</b>")
    _user, headers = actor

    response = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzd"})

    assert b"<b>" not in response.data
    assert b"&lt;b&gt;" in response.data




# ===========================================================
#  search_users — the per-user budget
# ===========================================================


@pytest.mark.slow
def test_the_people_budget_is_per_user_and_frees_up_after_its_window(
        client, make_user, auth_headers):
    searcher = make_user()
    other = make_user()
    target = make_user(username="kregzdute", display_name="Ona Kregzdiene")
    headers = auth_headers(searcher)

    with time_machine.travel(datetime(2026, 4, 1, 8, 0, 0), tick=False) as traveller:
        # a one-letter warm-up spends an attempt too: the
        # decorator runs before the two-character gate
        for _ in range(_USER_SEARCH_BUDGET):
            client.get(_USERS_URL, headers=headers, query_string={"q": "a"})

        spent = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzd"})
        assert spent.status_code == 429
        assert spent.get_json()["code"] == "rate_limited"

        # a second account searches freely
        assert client.get(_USERS_URL, headers=auth_headers(other),
                          query_string={"q": "kregzd"}).status_code == 200

        traveller.shift(301)
        recovered = client.get(_USERS_URL, headers=headers, query_string={"q": "kregzd"})
        assert recovered.status_code == 200
        assert [u["id"] for u in recovered.get_json()["users"]] == [target["id"]]
