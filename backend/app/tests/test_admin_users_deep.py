# -----------------------------------------------------------
#  [*] Tests — the admin user console, deep pass
#      (app/admin/routes.py: list_users, update_user,
#       admin_stats, _write_audit, _disconnect_user_sockets)
#
#  The gap-closing pass over that slice: the broad suite
#  already walks every line of these five, so everything here
#  is a branch arm, a boundary or an error path that line
#  coverage cannot see.
#
#    - GET /admin/users: what the seven-key body does with
#      values only DbGate can plant (an `active` of 2, of -1,
#      of the TEXT 'yes'), the html-escaping the JSON provider
#      puts on every string it ships, and the FULL ?limit= /
#      ?offset= parser — int() is lenient enough to take
#      " 3 ", "+3", "1_0" and an Arabic-Indic digit, strict
#      enough to refuse "", "3.0" and "0x3", and the limit
#      complaint always wins over the offset one.
#    - PATCH /admin/users/<id>: the ORDER its guards fire in
#      (404 before the body, role before active, "cannot
#      deactivate yourself" before "cannot remove your own
#      admin role", both before "nothing to update"), the
#      app-wide before_request that answers a non-object body
#      BEFORE the 404, and the id-matching that no case flip
#      or percent-escape can walk around.
#    - the last-active-admin backstop from every direction:
#      the demotion arm, the deactivation arm, both at once,
#      an inactive spare, a spare whose `active` is truthy but
#      not 1 — and the window between the count and the write,
#      which is not transactional.
#    - _write_audit as a unit: what it stores for a falsy
#      payload, a None target, Lithuanian text; that it lives
#      inside the caller's transaction (a rollback takes it
#      with it); and that a missing table, a NOT NULL
#      violation, a foreign-key violation and a CLOSED
#      connection are all swallowed while the real write still
#      commits.
#    - _disconnect_user_sockets as a unit: the module missing,
#      the symbol missing, the call exploding — three silent
#      no-ops — plus the real chat/events wiring, and proof
#      that it runs AFTER the commit and only for a
#      deactivation.
#    - GET /admin/stats: the 45 s snapshot on both sides of
#      its boundary, the grouped news_posts pass over all five
#      legal sources, and the string-compared expiry on the
#      second it flips.
# -----------------------------------------------------------

import contextlib
import json
import logging
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from flask import request

from app.admin import routes as admin_routes
from app.database import get_db


USERS = "/api/admin/users"
STATS = "/api/admin/stats"

# The AdminUser wire shape — services/api/admin.ts
USER_FIELDS = {"id", "username", "email", "displayName", "role", "active", "createdAt"}
STATS_FIELDS = {"users", "posts", "scrapedArticles", "comments", "activeInvitations"}

# news_posts.source is a CHECK column: these five and nothing
# else, of which exactly two belong to the scrapers
SCRAPER_SOURCES = ("knf.vu.lt", "vu.lt")
OTHER_SOURCES = ("app", "faculty", "user")




# -----------------------------------------------------------
# _fresh_module_state
# -----------------------------------------------------------
#
# admin/routes.py keeps the 45 s stats snapshot in a
# PROCESS-wide dict and chat/events.py keeps presence in two
# more, none of them tied to an app. Without this a snapshot
# built on one test's database would be served to the next,
# and a planted socket id would outlive the test that planted
# it.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_module_state():
    import app.chat.events as chat_events

    admin_routes._stats_cache.clear()
    chat_events._connected_users.clear()
    chat_events._connected_names.clear()
    yield
    admin_routes._stats_cache.clear()
    chat_events._connected_users.clear()
    chat_events._connected_names.clear()




# -----------------------------------------------------------
# mid_request
# -----------------------------------------------------------
#
#   mid_request(lambda conn: conn.execute("DELETE ..."))
#
# Arms a one-shot trap on admin/routes.py's utc_now_iso: the
# first time update_user calls it — STEP 4, after every guard
# has passed and before the first UPDATE — the callback runs
# on its own connection to the same database and the rest of
# the handler then works on state that moved under it. That
# is the exact window a second admin's concurrent PATCH lands
# in.
# -----------------------------------------------------------

@pytest.fixture
def mid_request(app, monkeypatch):

    def _arm(callback):
        original = admin_routes.utc_now_iso
        fired = []

        def _hook():
            if not fired:
                fired.append(True)
                conn = sqlite3.connect(app.config["DB_PATH"], timeout=15)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                try:
                    callback(conn)
                    conn.commit()
                finally:
                    conn.close()
            return original()

        monkeypatch.setattr(admin_routes, "utc_now_iso", _hook)
        return fired

    return _arm




# -----------------------------------------------------------
# _admin_request
# -----------------------------------------------------------
#
#   with _admin_request(app, actor_id) as conn:
#       admin_routes._write_audit(conn, "x")
#
# _write_audit reads request.user and writes on the caller's
# OPEN connection, so a direct unit test needs both: a request
# context carrying the actor require_role would have planted,
# and a real get_db() connection (foreign keys ON, exactly
# what the routes hand it).
# -----------------------------------------------------------

@contextlib.contextmanager
def _admin_request(app, actor_id, role="admin"):
    with app.test_request_context(USERS):
        request.user = {"id": actor_id, "role": role}
        conn = get_db()
        try:
            yield conn
        finally:
            conn.close()




# -----------------------------------------------------------
# _patch / _patch_raw / _list
# -----------------------------------------------------------
#
# The two routes under test. _patch_raw puts bytes on the
# wire untouched — the app's JSON provider escapes every
# string a `json=` kwarg carries (TESTPLAN rule 10), and a
# malformed or non-object body cannot be expressed as a
# `json=` kwarg at all.
# -----------------------------------------------------------

def _patch(client, headers, user_id, **body):
    return client.patch(f"{USERS}/{user_id}", headers=headers, json=body)


def _patch_raw(client, headers, user_id, raw, content_type="application/json"):
    return client.patch(f"{USERS}/{user_id}", headers=headers, data=raw,
                        content_type=content_type)


def _list(client, headers, **params):
    return client.get(USERS, headers=headers, query_string=params or None)




# -----------------------------------------------------------
# _entry
# -----------------------------------------------------------
#
# One user out of a listing response by id — every assertion
# that says "this row" goes through it, so a missing row
# fails loudly instead of comparing None.
# -----------------------------------------------------------

def _entry(response, user_id):
    assert response.status_code == 200, response.get_json()
    for user in response.get_json()["users"]:
        if user["id"] == user_id:
            return user
    raise AssertionError(f"user {user_id} is not in the listing")




# -----------------------------------------------------------
# _iso / _row / _set
# -----------------------------------------------------------
#
# Row surgery for the states no route can produce: a
# hand-edited `active`, a stamped created_at, a space-form
# timestamp. The column names are literals from this file,
# never anything a request supplied.
# -----------------------------------------------------------

def _iso(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _row(db, user_id):
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _set(db, user_id, column, value):
    db.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (value, user_id))
    db.commit()




# -----------------------------------------------------------
# _audit_rows
# -----------------------------------------------------------
#
# The admin_audit trail (migration v40), oldest first, so a
# combined patch can be checked for one row PER changed field
# and in the order the handler wrote them.
# -----------------------------------------------------------

def _audit_rows(db, action=None):
    if action:
        return db.execute(
            "SELECT * FROM admin_audit WHERE action = ? ORDER BY rowid", (action,)
        ).fetchall()
    return db.execute("SELECT * FROM admin_audit ORDER BY rowid").fetchall()




# -----------------------------------------------------------
# _seed_article / _seed_comment / _seed_code / _seed_token
# -----------------------------------------------------------
#
# Rows the stats tiles only ever COUNT and the deactivation
# only ever DELETES, so a direct insert is the cheapest
# honest way to make them.
# -----------------------------------------------------------

def _seed_article(db, source="knf.vu.lt"):
    post_id = str(uuid.uuid4())
    now = _iso()
    db.execute(
        """INSERT INTO news_posts (id, title, content, source, source_url,
                                   post_type, is_public, published_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'article', 1, ?, ?, ?)""",
        (post_id, "Naujiena", "Turinys", source,
         f"https://example.invalid/{post_id}", now, now, now),
    )
    db.commit()
    return post_id


def _seed_comment(db, post_id, user_id):
    comment_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (comment_id, post_id, user_id, "Komentaras", _iso()),
    )
    db.commit()
    return comment_id


def _seed_code(db, created_by, expires_at, use_count=0, max_uses=5, role="student"):
    code_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO invitation_codes (id, code, role, created_by, max_uses, use_count,
                                         expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (code_id, "D-" + uuid.uuid4().hex[:10].upper(), role, created_by, max_uses,
         use_count, expires_at, _iso()),
    )
    db.commit()
    return code_id


def _seed_token(db, user_id):
    token = f"ExponentPushToken[{uuid.uuid4().hex[:22]}]"
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, token, "ios"),
    )
    db.commit()
    return token


def _sessions(db, user_id):
    return db.execute("SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user_id,)).fetchone()["c"]


def _tokens(db, user_id):
    return db.execute("SELECT COUNT(*) AS c FROM push_tokens WHERE user_id = ?", (user_id,)).fetchone()["c"]




# -----------------------------------------------------------
# _plant_snapshot
# -----------------------------------------------------------
#
# The stats cache aged by hand: `age` seconds of monotonic
# time are subtracted from its stamp, so a test can sit on
# either side of the 45 s window without a clock or a sleep.
# -----------------------------------------------------------

def _plant_snapshot(stats, age=0.0):
    admin_routes._stats_cache["at"] = time.monotonic() - age
    admin_routes._stats_cache["stats"] = stats




# -----------------------------------------------------------
# _uncount
# -----------------------------------------------------------
#
# The last-active-admin guard counts rows with active = 1
# while every auth check only asks whether active is TRUTHY.
# An active of 2 therefore leaves the caller fully
# authenticated and outside that count — the only way an HTTP
# caller reaches the backstop, since the self-guards catch
# every other route to it.
# -----------------------------------------------------------

def _uncount(db, user_id):
    _set(db, user_id, "active", 2)








# ===========================================================
# GET /api/admin/users — the body
# ===========================================================

@pytest.mark.contract
def test_every_row_of_the_listing_carries_exactly_the_seven_contract_keys(client, admin, make_user):
    _, headers = admin
    make_user(role="teacher")
    make_user(role="curator")

    users = _list(client, headers).get_json()["users"]

    assert len(users) == 3
    for user in users:
        assert set(user) == USER_FIELDS


def test_the_listing_carries_one_row_per_role_present(client, admin, make_user):
    _, headers = admin
    for role in ("student", "teacher", "curator"):
        make_user(role=role)

    roles = sorted(u["role"] for u in _list(client, headers).get_json()["users"])

    assert roles == ["admin", "curator", "student", "teacher"]


def test_the_listing_never_ships_a_password_hash_even_as_raw_bytes(client, admin, make_user):
    _, headers = admin
    make_user()

    body = _list(client, headers).get_data(as_text=True)

    assert "password" not in body
    assert "$2b$" not in body


def test_a_hand_edited_active_of_two_still_reads_as_active_true(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "active", 2)

    entry = _entry(_list(client, headers), user["id"])

    assert entry["active"] is True


def test_a_hand_edited_negative_active_still_reads_as_active_true(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "active", -1)

    assert _entry(_list(client, headers), user["id"])["active"] is True


@pytest.mark.parametrize("stored,expected", [("yes", True), ("", False), (0, False), (1, True)])
def test_the_active_flag_is_a_bool_of_whatever_the_column_holds(client, admin, make_user, db,
                                                                stored, expected):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "active", stored)

    assert _entry(_list(client, headers), user["id"])["active"] is expected


def test_a_display_name_carrying_markup_leaves_html_escaped(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "display_name", 'Ona <b>"K"</b> & Co')

    body = _list(client, headers).get_data(as_text=True)

    assert "&lt;b&gt;&quot;K&quot;&lt;/b&gt; &amp; Co" in body
    assert "<b>" not in body


def test_lithuanian_display_names_go_out_as_utf8_not_escape_sequences(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "display_name", "Ąžuolas Šešėlis")

    body = _list(client, headers).get_data(as_text=True)

    assert "Ąžuolas Šešėlis" in body
    assert "\\u" not in body


def test_an_empty_display_name_comes_back_as_an_empty_string(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "display_name", "")

    assert _entry(_list(client, headers), user["id"])["displayName"] == ""


def test_created_at_is_echoed_verbatim_including_a_legacy_space_form(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "created_at", "2020-01-02 03:04:05")

    assert _entry(_list(client, headers), user["id"])["createdAt"] == "2020-01-02 03:04:05"


def test_the_listing_is_strictly_newest_first(client, admin, make_user, db):
    _, headers = admin
    ordered = []
    for seconds in (10, 20, 30, 40):
        user = make_user()
        _set(db, user["id"], "created_at", _iso(seconds))
        ordered.append(user["id"])

    listed = [u["id"] for u in _list(client, headers).get_json()["users"]]

    assert listed[:4] == list(reversed(ordered))


def test_rows_sharing_one_created_at_all_still_appear(client, admin, make_user, db):
    _, headers = admin
    ids = {make_user()["id"] for _ in range(4)}
    for user_id in ids:
        _set(db, user_id, "created_at", "2021-01-01T00:00:00+00:00")

    listed = {u["id"] for u in _list(client, headers).get_json()["users"]}

    assert ids <= listed


def test_the_seeded_admin_is_in_the_listing(client, admin):
    user, headers = admin

    assert _entry(_list(client, headers), user["id"])["role"] == "admin"


def test_posting_to_the_listing_is_method_not_allowed(client, admin):
    _, headers = admin

    response = client.post(USERS, headers=headers, json={})

    assert response.status_code == 405


def test_deleting_a_single_user_erases_the_account(client, admin, make_user):
    # DELETE grew a handler (GDPR erasure — test_account_erasure_deep.py
    # owns its behaviour); here only that the method is routed
    _, headers = admin
    user = make_user()

    assert client.delete(f"{USERS}/{user['id']}", headers=headers).status_code == 200


def test_putting_a_single_user_is_method_not_allowed(client, admin, make_user):
    _, headers = admin
    user = make_user()

    assert client.put(f"{USERS}/{user['id']}", headers=headers, json={}).status_code == 405








# ===========================================================
# GET /api/admin/users — ?limit= and ?offset=
# ===========================================================

def test_unknown_query_parameters_are_ignored_entirely(client, admin, make_user):
    _, headers = admin
    make_user()

    response = _list(client, headers, sort="name", page="2")

    assert response.status_code == 200
    assert len(response.get_json()["users"]) == 2


@pytest.mark.parametrize("value", [" 3 ", "+3", "1_0", "٣"])
def test_int_parses_the_lenient_limit_forms_it_always_has(client, admin, make_user, value):
    _, headers = admin
    for _ in range(3):
        make_user()

    response = _list(client, headers, limit=value)

    assert response.status_code == 200
    assert len(response.get_json()["users"]) <= 10


@pytest.mark.parametrize("value", ["", " ", "3.0", "1e2", "0x3", "abc", "null", "true",
                                   "3,0", "３ ３", "-", "+"])
def test_a_limit_int_cannot_read_is_a_four_hundred(client, admin, value):
    _, headers = admin

    response = _list(client, headers, limit=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be an integer"


@pytest.mark.parametrize("value", ["", "abc", "1.5", "0x0", "+"])
def test_an_offset_int_cannot_read_is_a_four_hundred(client, admin, value):
    _, headers = admin

    response = _list(client, headers, offset=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "offset must be an integer"


@pytest.mark.parametrize("value", ["0", "-1", "-500", "501", "1000"])
def test_a_limit_outside_the_bounds_is_a_four_hundred(client, admin, value):
    _, headers = admin

    response = _list(client, headers, limit=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be between 1 and 500"


def test_a_limit_of_minus_zero_is_still_a_zero_and_refused(client, admin):
    _, headers = admin

    assert _list(client, headers, limit="-0").status_code == 400


def test_an_absurdly_large_limit_parses_and_is_then_refused(client, admin):
    _, headers = admin

    response = _list(client, headers, limit="9" * 40)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be between 1 and 500"


def test_an_offset_of_minus_zero_is_accepted(client, admin):
    _, headers = admin

    assert _list(client, headers, offset="-0").status_code == 200


@pytest.mark.parametrize("value", ["-1", "-500"])
def test_a_negative_offset_is_a_four_hundred(client, admin, value):
    _, headers = admin

    response = _list(client, headers, offset=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "offset must be zero or greater"


def test_the_limit_complaint_wins_when_both_parameters_are_bad(client, admin):
    _, headers = admin

    response = _list(client, headers, limit="abc", offset="abc")

    assert response.get_json()["error"] == "limit must be an integer"


def test_a_good_limit_with_a_bad_offset_reports_the_offset(client, admin):
    _, headers = admin

    response = _list(client, headers, limit="5", offset="abc")

    assert response.get_json()["error"] == "offset must be an integer"


def test_an_out_of_range_limit_is_reported_before_a_bad_offset(client, admin):
    _, headers = admin

    response = _list(client, headers, limit="0", offset="abc")

    assert response.get_json()["error"] == "limit must be between 1 and 500"


def test_a_repeated_limit_parameter_takes_the_first_value(client, admin, make_user):
    _, headers = admin
    for _ in range(3):
        make_user()

    response = client.get(USERS + "?limit=1&limit=abc", headers=headers)

    assert response.status_code == 200
    assert len(response.get_json()["users"]) == 1


def test_a_refused_page_carries_the_error_and_nothing_else(client, admin, make_user):
    _, headers = admin
    make_user()

    body = _list(client, headers, limit="0").get_json()

    assert set(body) == {"error"}


def test_paging_in_slices_of_two_visits_every_user_exactly_once(client, admin, make_user, db):
    _, headers = admin
    for seconds in range(5):
        user = make_user()
        _set(db, user["id"], "created_at", _iso(seconds + 1))

    seen = []
    for offset in (0, 2, 4, 6):
        page = _list(client, headers, limit=2, offset=offset).get_json()["users"]
        seen.extend(u["id"] for u in page)

    assert len(seen) == 6
    assert len(set(seen)) == 6


def test_an_offset_alone_rides_on_sqlites_unlimited_row_count(client, admin, make_user, db):
    _, headers = admin
    for seconds in range(4):
        user = make_user()
        _set(db, user["id"], "created_at", _iso(seconds + 1))

    everything = _list(client, headers).get_json()["users"]
    rest = _list(client, headers, offset=1).get_json()["users"]

    assert [u["id"] for u in rest] == [u["id"] for u in everything[1:]]


def test_an_offset_past_the_last_row_is_an_empty_page_not_an_error(client, admin):
    _, headers = admin

    response = _list(client, headers, offset=10 ** 12)

    assert response.status_code == 200
    assert response.get_json()["users"] == []


def test_the_limit_bounds_themselves_are_both_accepted(client, admin):
    _, headers = admin

    assert _list(client, headers, limit=1).status_code == 200
    assert _list(client, headers, limit=500).status_code == 200


def test_a_paged_listing_still_hides_the_password_hash(client, admin, make_user):
    _, headers = admin
    make_user()

    body = _list(client, headers, limit=1).get_data(as_text=True)

    assert "password" not in body


def test_a_limit_larger_than_the_table_returns_the_whole_table(client, admin, make_user):
    _, headers = admin
    make_user()

    assert len(_list(client, headers, limit=500).get_json()["users"]) == 2








# ===========================================================
# PATCH /api/admin/users/<id> — the 404 gate and the body
# ===========================================================

@pytest.mark.parametrize("bad_id", [
    "00000000-0000-0000-0000-000000000000",
    "%",
    "_",
    "' OR '1'='1",
    "x" * 4000,
])
def test_an_id_that_matches_nobody_is_a_four_hundred_and_four(client, admin, bad_id):
    _, headers = admin

    response = _patch(client, headers, bad_id, role="teacher")

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_a_wildcard_id_never_matches_a_real_row(client, admin, make_user, db):
    _, headers = admin
    user = make_user(role="student")

    _patch(client, headers, "%", role="admin")

    assert _row(db, user["id"])["role"] == "student"


def test_flipping_the_case_of_an_id_finds_nobody(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    response = _patch(client, headers, user["id"].upper(), role="teacher")

    assert response.status_code == 404
    assert _row(db, user["id"])["role"] == "student"


def test_the_self_guard_survives_a_percent_escaped_id(client, admin):
    user, headers = admin
    escaped = user["id"].replace("-", "%2D")

    response = client.patch(f"{USERS}/{escaped}", headers=headers, json={"active": False})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot deactivate your own account"


def test_an_unknown_id_is_answered_before_an_invalid_role(client, admin):
    _, headers = admin

    response = _patch(client, headers, str(uuid.uuid4()), role="emperor")

    assert response.get_json()["error"] == "User not found"


def test_an_unknown_id_is_answered_before_a_non_boolean_active(client, admin):
    _, headers = admin

    response = _patch(client, headers, str(uuid.uuid4()), active="false")

    assert response.get_json()["error"] == "User not found"


def test_an_unknown_id_is_answered_before_an_empty_patch(client, admin):
    _, headers = admin

    response = client.patch(f"{USERS}/{uuid.uuid4()}", headers=headers, json={})

    assert response.status_code == 404


def test_a_non_object_body_is_refused_by_the_app_hook_before_the_id_is_looked_up(client, admin):
    # The before_request in app/__init__.py answers a top-level
    # array first — the route's own 404-before-the-body promise
    # only holds for bodies that hook lets through
    _, headers = admin

    response = _patch_raw(client, headers, str(uuid.uuid4()), b"[1, 2]")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("raw", [b"[1, 2]", b'"student"', b"42", b"true"])
def test_every_non_object_json_body_is_refused(client, admin, make_user, raw):
    _, headers = admin
    user = make_user()

    response = _patch_raw(client, headers, user["id"], raw)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("raw", [b"", b"{", b"{'role': 'teacher'}", b"null", b"nonsense"])
def test_a_body_that_does_not_parse_is_the_routes_own_four_hundred(client, admin, make_user, raw):
    _, headers = admin
    user = make_user()

    response = _patch_raw(client, headers, user["id"], raw)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_json_object_sent_as_plain_text_is_refused(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = _patch_raw(client, headers, user["id"], b'{"role": "teacher"}',
                          content_type="text/plain")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_form_encoded_body_is_refused(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = client.patch(f"{USERS}/{user['id']}", headers=headers, data={"role": "teacher"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_no_body_at_all_is_refused(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = client.patch(f"{USERS}/{user['id']}", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_refused_body_changes_nothing_about_the_user(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    _patch_raw(client, headers, user["id"], b"{")

    row = _row(db, user["id"])
    assert (row["role"], row["active"]) == ("student", 1)








# ===========================================================
# PATCH /api/admin/users/<id> — field typing and guard order
# ===========================================================

@pytest.mark.parametrize("value", ["", "ADMIN", "Admin", "student ", " student", "superadmin",
                                   "students", 5, 0, 1.5, [], {}, ["student"]])
def test_a_role_outside_the_whitelist_is_refused(client, admin, make_user, value):
    _, headers = admin
    user = make_user()

    response = _patch(client, headers, user["id"], role=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"


def test_a_boolean_true_is_not_a_role(client, admin, make_user):
    _, headers = admin
    user = make_user()

    assert _patch(client, headers, user["id"], role=True).get_json()["error"] == "Invalid role"


@pytest.mark.parametrize("role", ["student", "teacher", "curator", "admin"])
def test_each_whitelisted_role_is_accepted(client, admin, make_user, db, role):
    _, headers = admin
    user = make_user()

    response = _patch(client, headers, user["id"], role=role)

    assert response.status_code == 200
    assert _row(db, user["id"])["role"] == role


def test_a_role_laced_with_a_nul_byte_is_cleaned_before_the_whitelist_sees_it(client, admin,
                                                                             make_user, db):
    # The app-wide before_request NUL-strips every string in the
    # body, so this arrives at the whitelist as a plain "teacher"
    _, headers = admin
    user = make_user()

    response = _patch_raw(client, headers, user["id"], b'{"role": "tea\\u0000cher"}')

    assert response.status_code == 200
    assert _row(db, user["id"])["role"] == "teacher"


@pytest.mark.parametrize("value", [0, 1, -1, "true", "false", "", "1", 1.0, 0.0, [], {},
                                   [True], "True"])
def test_an_active_that_is_not_a_json_boolean_is_refused(client, admin, make_user, value):
    _, headers = admin
    user = make_user()

    response = _patch(client, headers, user["id"], active=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "active must be a boolean"


def test_a_bad_role_is_reported_before_a_bad_active(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = _patch(client, headers, user["id"], role="emperor", active=0)

    assert response.get_json()["error"] == "Invalid role"


def test_a_non_boolean_active_is_reported_before_the_self_deactivation_guard(client, admin):
    user, headers = admin

    response = _patch(client, headers, user["id"], active=0)

    assert response.get_json()["error"] == "active must be a boolean"


def test_a_zero_active_never_deactivates_the_calling_admin(client, admin, db):
    user, headers = admin

    _patch(client, headers, user["id"], active=0)

    assert _row(db, user["id"])["active"] == 1


def test_the_self_deactivation_guard_fires_before_the_self_demotion_guard(client, admin):
    user, headers = admin

    response = _patch(client, headers, user["id"], role="student", active=False)

    assert response.get_json()["error"] == "Cannot deactivate your own account"


def test_the_self_deactivation_guard_fires_before_nothing_to_update(client, admin):
    user, headers = admin

    response = _patch(client, headers, user["id"], active=False)

    assert response.get_json()["error"] == "Cannot deactivate your own account"


def test_an_invalid_role_is_reported_before_nothing_to_update(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = _patch(client, headers, user["id"], role="emperor")

    assert response.get_json()["error"] == "Invalid role"


@pytest.mark.parametrize("body", [{}, {"foo": "bar"}, {"role": None}, {"active": None},
                                  {"role": None, "active": None}, {"Role": "admin"},
                                  {"isActive": False}])
def test_a_patch_that_changes_no_known_field_is_nothing_to_update(client, admin, make_user, body):
    _, headers = admin
    user = make_user()

    response = client.patch(f"{USERS}/{user['id']}", headers=headers, json=body)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Nothing to update"


def test_unknown_keys_alongside_a_real_field_are_ignored(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    response = client.patch(f"{USERS}/{user['id']}", headers=headers,
                            json={"role": "teacher", "username": "hacked", "id": "nope"})

    assert response.status_code == 200
    row = _row(db, user["id"])
    assert row["role"] == "teacher"
    assert row["username"] == user["username"]


def test_a_refused_patch_leaves_the_updated_at_stamp_alone(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    before = _row(db, user["id"])["updated_at"]

    _patch(client, headers, user["id"], role="emperor")

    assert _row(db, user["id"])["updated_at"] == before








# ===========================================================
# PATCH /api/admin/users/<id> — admin continuity
# ===========================================================

def test_an_admin_may_promote_themselves_to_admin_again(client, admin, db):
    user, headers = admin

    response = _patch(client, headers, user["id"], role="admin")

    assert response.status_code == 200
    assert _row(db, user["id"])["role"] == "admin"


@pytest.mark.parametrize("role", ["student", "teacher", "curator"])
def test_an_admin_may_never_strip_their_own_admin_role(client, admin, db, role):
    user, headers = admin

    response = _patch(client, headers, user["id"], role=role)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove your own admin role"
    assert _row(db, user["id"])["role"] == "admin"


def test_the_self_demotion_guard_fires_even_with_spare_admins_around(client, admin, make_user):
    user, headers = admin
    make_user(role="admin")
    make_user(role="admin")

    response = _patch(client, headers, user["id"], role="student")

    assert response.get_json()["error"] == "Cannot remove your own admin role"


def test_demoting_a_second_admin_is_allowed_while_the_caller_still_counts(client, admin,
                                                                         make_user, db):
    _, headers = admin
    spare = make_user(role="admin")

    response = _patch(client, headers, spare["id"], role="teacher")

    assert response.status_code == 200
    assert _row(db, spare["id"])["role"] == "teacher"


def test_the_last_active_admin_cannot_be_demoted(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])

    response = _patch(client, headers, target["id"], role="student")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove the last active admin"
    assert _row(db, target["id"])["role"] == "admin"


def test_the_last_active_admin_cannot_be_deactivated(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])

    response = _patch(client, headers, target["id"], active=False)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove the last active admin"
    assert _row(db, target["id"])["active"] == 1


def test_the_last_admin_cannot_be_kept_admin_and_deactivated_in_one_patch(client, admin,
                                                                         make_user, db):
    # role = "admin" is no demotion at all, so only the second
    # arm of the guard's `or` can catch this one
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])

    response = _patch(client, headers, target["id"], role="admin", active=False)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove the last active admin"


def test_the_last_admin_cannot_be_demoted_and_deactivated_in_one_patch(client, admin,
                                                                      make_user, db):
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])

    response = _patch(client, headers, target["id"], role="student", active=False)

    assert response.status_code == 400
    assert _row(db, target["id"])["role"] == "admin"
    assert _row(db, target["id"])["active"] == 1


def test_promoting_the_last_admin_to_admin_is_not_a_demotion(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])

    assert _patch(client, headers, target["id"], role="admin").status_code == 200


def test_reactivating_the_last_admin_never_consults_the_backstop(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="admin", active=0)
    _uncount(db, actor["id"])

    response = _patch(client, headers, target["id"], active=True)

    assert response.status_code == 200
    assert _row(db, target["id"])["active"] == 1


def test_a_deactivated_spare_admin_does_not_save_the_last_one(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="admin")
    make_user(role="admin", active=0)
    _uncount(db, actor["id"])

    response = _patch(client, headers, target["id"], role="student")

    assert response.get_json()["error"] == "Cannot remove the last active admin"


def test_a_spare_admin_whose_active_is_truthy_but_not_one_does_not_count(client, admin,
                                                                        make_user, db):
    # The backstop counts active = 1; every auth check only asks
    # for truthiness, so a hand-edited 2 is a logged-in admin the
    # guard cannot see
    actor, headers = admin
    target = make_user(role="admin")
    spare = make_user(role="admin")
    _uncount(db, actor["id"])
    _uncount(db, spare["id"])

    response = _patch(client, headers, target["id"], role="student")

    assert response.get_json()["error"] == "Cannot remove the last active admin"


def test_one_active_spare_admin_is_enough_to_allow_the_demotion(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="admin")
    make_user(role="admin")
    _uncount(db, actor["id"])

    assert _patch(client, headers, target["id"], role="student").status_code == 200


def test_the_backstop_never_fires_for_a_non_admin_target(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="curator")
    _uncount(db, actor["id"])

    assert _patch(client, headers, target["id"], role="student").status_code == 200
    assert _patch(client, headers, target["id"], active=False).status_code == 200


def test_the_target_never_counts_as_its_own_remaining_admin(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])

    # target is active = 1, and it is the row being demoted —
    # the count excludes it explicitly
    assert _patch(client, headers, target["id"], role="teacher").status_code == 400


def test_a_refused_continuity_guard_writes_no_audit_row(client, admin, make_user, db):
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])

    _patch(client, headers, target["id"], role="student")

    assert _audit_rows(db) == []








# ===========================================================
# PATCH /api/admin/users/<id> — what the write does
# ===========================================================

@pytest.mark.contract
def test_the_patch_answers_the_same_row_the_listing_would(client, admin, make_user):
    _, headers = admin
    user = make_user()

    patched = _patch(client, headers, user["id"], role="teacher").get_json()
    listed = _entry(_list(client, headers), user["id"])

    assert set(patched) == USER_FIELDS
    assert patched == listed


def test_a_role_change_leaves_every_other_column_alone(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    before = _row(db, user["id"])

    _patch(client, headers, user["id"], role="curator")
    after = _row(db, user["id"])

    for column in ("id", "username", "email", "display_name", "password_hash", "active",
                   "invited", "created_at"):
        assert after[column] == before[column]


def test_setting_the_role_a_user_already_has_still_succeeds(client, admin, make_user, db):
    _, headers = admin
    user = make_user(role="teacher")

    response = _patch(client, headers, user["id"], role="teacher")

    assert response.status_code == 200
    assert _row(db, user["id"])["role"] == "teacher"


def test_setting_the_same_role_twice_is_idempotent_on_the_row(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    _patch(client, headers, user["id"], role="curator")
    _patch(client, headers, user["id"], role="curator")

    assert _row(db, user["id"])["role"] == "curator"


def test_a_role_change_stamps_updated_at_in_iso_t_form(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    _patch(client, headers, user["id"], role="teacher")
    stamp = _row(db, user["id"])["updated_at"]

    assert "T" in stamp and stamp.endswith("+00:00")


def test_an_active_only_change_stamps_updated_at_too(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    before = _row(db, user["id"])["updated_at"]

    _patch(client, headers, user["id"], active=False)
    after = _row(db, user["id"])["updated_at"]

    assert after != before
    assert "T" in after


def test_deactivation_writes_a_zero_not_a_json_false(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    _patch(client, headers, user["id"], active=False)

    assert _row(db, user["id"])["active"] == 0


def test_reactivation_writes_a_one(client, admin, make_user, db):
    _, headers = admin
    user = make_user(active=0)

    _patch(client, headers, user["id"], active=True)

    assert _row(db, user["id"])["active"] == 1


def test_reactivating_a_hand_edited_active_normalises_it_to_one(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "active", 7)

    _patch(client, headers, user["id"], active=True)

    assert _row(db, user["id"])["active"] == 1


def test_a_role_only_patch_leaves_a_hand_edited_active_untouched(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "active", 2)

    response = _patch(client, headers, user["id"], role="teacher")

    assert response.get_json()["active"] is True
    assert _row(db, user["id"])["active"] == 2


def test_deactivating_an_already_inactive_user_is_accepted_and_idempotent(client, admin,
                                                                         make_user, db):
    _, headers = admin
    user = make_user(active=0)

    first = _patch(client, headers, user["id"], active=False)
    second = _patch(client, headers, user["id"], active=False)

    assert (first.status_code, second.status_code) == (200, 200)
    assert _row(db, user["id"])["active"] == 0


def test_reactivating_an_already_active_user_deletes_no_session(client, admin, actor, db):
    user, _ = actor
    _, headers = admin

    _patch(client, headers, user["id"], active=True)

    assert _sessions(db, user["id"]) == 1


def test_deactivation_deletes_every_one_of_the_users_sessions(client, admin, make_user,
                                                              auth_headers, db):
    _, headers = admin
    user = make_user()
    auth_headers(user)
    auth_headers(user)
    auth_headers(user)
    assert _sessions(db, user["id"]) == 3

    _patch(client, headers, user["id"], active=False)

    assert _sessions(db, user["id"]) == 0


def test_deactivation_deletes_every_one_of_the_users_push_tokens(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    _seed_token(db, user["id"])
    _seed_token(db, user["id"])

    _patch(client, headers, user["id"], active=False)

    assert _tokens(db, user["id"]) == 0


def test_deactivation_leaves_other_peoples_sessions_and_tokens(client, admin, make_user,
                                                               auth_headers, db):
    _, headers = admin
    target = make_user()
    bystander = make_user()
    auth_headers(target)
    auth_headers(bystander)
    _seed_token(db, target["id"])
    _seed_token(db, bystander["id"])

    _patch(client, headers, target["id"], active=False)

    assert (_sessions(db, bystander["id"]), _tokens(db, bystander["id"])) == (1, 1)


def test_deactivation_touches_neither_posts_nor_comments(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    post_id = _seed_article(db, "app")
    _seed_comment(db, post_id, user["id"])

    _patch(client, headers, user["id"], active=False)

    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 1
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 1


def test_a_role_change_alone_keeps_the_sessions_and_tokens(client, admin, actor, db):
    user, _ = actor
    _, headers = admin
    _seed_token(db, user["id"])

    _patch(client, headers, user["id"], role="teacher")

    assert (_sessions(db, user["id"]), _tokens(db, user["id"])) == (1, 1)


def test_a_role_and_a_deactivation_apply_together(client, admin, actor, db):
    user, _ = actor
    _, headers = admin

    response = _patch(client, headers, user["id"], role="curator", active=False)
    row = _row(db, user["id"])

    assert response.get_json()["role"] == "curator"
    assert response.get_json()["active"] is False
    assert (row["role"], row["active"]) == ("curator", 0)
    assert _sessions(db, user["id"]) == 0


def test_a_role_and_a_reactivation_apply_together_without_deleting_sessions(client, admin,
                                                                           make_user,
                                                                           auth_headers, db):
    _, headers = admin
    user = make_user()
    auth_headers(user)

    response = _patch(client, headers, user["id"], role="teacher", active=True)

    assert response.status_code == 200
    assert _sessions(db, user["id"]) == 1


def test_a_deactivated_user_can_be_brought_back_and_log_in_again(client, admin, make_user,
                                                                 auth_headers):
    _, headers = admin
    user = make_user()

    _patch(client, headers, user["id"], active=False)
    refused = client.post("/api/auth/login", json={"username": user["username"],
                                                   "password": user["password"]})
    _patch(client, headers, user["id"], active=True)

    assert refused.status_code == 403
    assert auth_headers(user)["Authorization"].startswith("Bearer ")


def test_the_response_active_flag_mirrors_the_row_that_was_just_written(client, admin, make_user):
    _, headers = admin
    user = make_user()

    off = _patch(client, headers, user["id"], active=False).get_json()
    on = _patch(client, headers, user["id"], active=True).get_json()

    assert off["active"] is False
    assert on["active"] is True


def test_a_display_name_with_markup_is_escaped_in_the_patch_answer_too(client, admin,
                                                                       make_user, db):
    _, headers = admin
    user = make_user()
    _set(db, user["id"], "display_name", "<i>Rasa</i>")

    body = _patch(client, headers, user["id"], role="teacher").get_data(as_text=True)

    assert "&lt;i&gt;Rasa&lt;/i&gt;" in body








# ===========================================================
# PATCH /api/admin/users/<id> — the audit trail
# ===========================================================

def test_a_role_change_records_the_old_and_the_new_role(client, admin, make_user, db):
    actor, headers = admin
    user = make_user(role="teacher")

    _patch(client, headers, user["id"], role="curator")
    row = _audit_rows(db, "user.role")[0]

    assert row["actor_id"] == actor["id"]
    assert row["target"] == user["id"]
    assert json.loads(row["payload"]) == {"from": "teacher", "to": "curator"}


def test_a_no_op_role_change_still_records_from_equal_to(client, admin, make_user, db):
    _, headers = admin
    user = make_user(role="teacher")

    _patch(client, headers, user["id"], role="teacher")

    assert json.loads(_audit_rows(db, "user.role")[0]["payload"]) == {"from": "teacher",
                                                                     "to": "teacher"}


@pytest.mark.parametrize("flag", [True, False])
def test_an_active_change_records_the_boolean_it_applied(client, admin, make_user, db, flag):
    _, headers = admin
    user = make_user(active=0 if flag else 1)

    _patch(client, headers, user["id"], active=flag)

    assert json.loads(_audit_rows(db, "user.active")[0]["payload"]) == {"active": flag}


def test_a_combined_patch_writes_the_role_row_before_the_active_row(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    _patch(client, headers, user["id"], role="teacher", active=False)

    assert [r["action"] for r in _audit_rows(db)] == ["user.role", "user.active"]


def test_each_repeat_of_the_same_patch_appends_another_audit_row(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    for _ in range(3):
        _patch(client, headers, user["id"], role="teacher")

    assert len(_audit_rows(db, "user.role")) == 3


def test_two_audit_rows_never_share_an_id(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    _patch(client, headers, user["id"], role="teacher", active=False)
    ids = [r["id"] for r in _audit_rows(db)]

    assert len(set(ids)) == 2


def test_audit_rows_are_stamped_in_iso_t_form(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    _patch(client, headers, user["id"], role="teacher")
    stamp = _audit_rows(db)[0]["created_at"]

    assert "T" in stamp and stamp.endswith("+00:00")


@pytest.mark.parametrize("body", [{"role": "emperor"}, {"active": "no"}, {}])
def test_a_body_the_route_refuses_leaves_no_trail(client, admin, make_user, db, body):
    _, headers = admin
    user = make_user()

    client.patch(f"{USERS}/{user['id']}", headers=headers, json=body)

    assert _audit_rows(db) == []


def test_a_four_hundred_and_four_leaves_no_trail(client, admin, db):
    _, headers = admin

    _patch(client, headers, str(uuid.uuid4()), role="teacher")

    assert _audit_rows(db) == []


def test_the_role_change_commits_even_with_the_audit_table_gone(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    db.execute("DROP TABLE admin_audit")
    db.commit()

    response = _patch(client, headers, user["id"], role="teacher")

    assert response.status_code == 200
    assert _row(db, user["id"])["role"] == "teacher"


def test_the_deactivation_commits_even_with_the_audit_table_gone(client, admin, actor, db):
    user, _ = actor
    _, headers = admin
    db.execute("DROP TABLE admin_audit")
    db.commit()

    response = _patch(client, headers, user["id"], active=False)

    assert response.status_code == 200
    assert _row(db, user["id"])["active"] == 0
    assert _sessions(db, user["id"]) == 0








# ===========================================================
# PATCH /api/admin/users/<id> — the socket kill switch
# ===========================================================

@pytest.fixture
def socket_kicks(monkeypatch):
    import app.chat.events as chat_events

    seen = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", lambda user_id: seen.append(user_id))
    return seen


def test_a_deactivation_cuts_exactly_the_targets_sockets(client, admin, make_user, socket_kicks):
    _, headers = admin
    user = make_user()

    _patch(client, headers, user["id"], active=False)

    assert socket_kicks == [user["id"]]


@pytest.mark.parametrize("body", [{"role": "teacher"}, {"active": True},
                                  {"role": "teacher", "active": True}])
def test_nothing_but_a_deactivation_cuts_a_socket(client, admin, make_user, socket_kicks, body):
    _, headers = admin
    user = make_user(active=0)

    client.patch(f"{USERS}/{user['id']}", headers=headers, json=body)

    assert socket_kicks == []


def test_a_refused_patch_cuts_no_socket(client, admin, make_user, socket_kicks, db):
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])

    _patch(client, headers, target["id"], active=False)

    assert socket_kicks == []


def test_a_four_hundred_and_four_cuts_no_socket(client, admin, socket_kicks):
    _, headers = admin

    _patch(client, headers, str(uuid.uuid4()), active=False)

    assert socket_kicks == []


def test_the_sockets_are_cut_only_after_the_deactivation_commits(app, client, admin, make_user,
                                                                 monkeypatch):
    # A second connection can only read active = 0 if the
    # handler's own transaction is already committed
    import app.chat.events as chat_events

    _, headers = admin
    user = make_user()
    seen = []

    def _spy(user_id):
        conn = sqlite3.connect(app.config["DB_PATH"], timeout=15)
        try:
            row = conn.execute("SELECT active FROM users WHERE id = ?", (user_id,)).fetchone()
        finally:
            conn.close()
        seen.append((user_id, row[0]))

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _spy)
    _patch(client, headers, user["id"], active=False)

    assert seen == [(user["id"], 0)]


def test_the_real_chat_layer_drops_the_users_presence_row(client, admin, actor, db):
    import app.chat.events as chat_events

    user, _ = actor
    _, headers = admin
    chat_events._connected_users["sid-1"] = user["id"]
    chat_events._connected_users["sid-2"] = user["id"]
    chat_events._connected_names["sid-1"] = "Vartotojas"
    chat_events._connected_users["sid-other"] = "somebody-else"

    _patch(client, headers, user["id"], active=False)

    assert "sid-1" not in chat_events._connected_users
    assert "sid-2" not in chat_events._connected_users
    assert chat_events._connected_users["sid-other"] == "somebody-else"


def test_an_exploding_socket_layer_never_fails_the_deactivation(client, admin, actor, db,
                                                                monkeypatch):
    import app.chat.events as chat_events

    user, _ = actor
    _, headers = admin

    def _boom(user_id):
        raise RuntimeError("socket layer wedged")

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _boom)
    response = _patch(client, headers, user["id"], active=False)

    assert response.status_code == 200
    assert _row(db, user["id"])["active"] == 0
    assert _sessions(db, user["id"]) == 0


def test_a_missing_socket_layer_never_fails_the_deactivation(client, admin, actor, db,
                                                             monkeypatch):
    user, _ = actor
    _, headers = admin
    monkeypatch.setitem(sys.modules, "app.chat.events", None)

    response = _patch(client, headers, user["id"], active=False)

    assert response.status_code == 200
    assert _row(db, user["id"])["active"] == 0








# ===========================================================
# _disconnect_user_sockets — the helper on its own
# ===========================================================

def test_the_helper_hands_the_user_id_straight_to_the_chat_layer(monkeypatch):
    import app.chat.events as chat_events

    seen = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", lambda uid: seen.append(uid))

    admin_routes._disconnect_user_sockets("naudotojas-1")

    assert seen == ["naudotojas-1"]


def test_the_helper_ignores_whatever_the_chat_layer_returns(monkeypatch):
    import app.chat.events as chat_events

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", lambda uid: 17)

    assert admin_routes._disconnect_user_sockets("naudotojas-1") is None


def test_a_chat_module_that_will_not_import_is_a_silent_no_op(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.chat.events", None)

    assert admin_routes._disconnect_user_sockets("naudotojas-1") is None


def test_a_chat_module_without_the_symbol_is_a_silent_no_op(monkeypatch):
    import app.chat.events as chat_events

    monkeypatch.delattr(chat_events, "disconnect_user_sockets")

    assert admin_routes._disconnect_user_sockets("naudotojas-1") is None


def test_an_exploding_chat_layer_is_logged_and_swallowed(monkeypatch, caplog):
    import app.chat.events as chat_events

    def _boom(uid):
        raise RuntimeError("no socket server")

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _boom)

    with caplog.at_level(logging.WARNING, logger="app.admin.routes"):
        assert admin_routes._disconnect_user_sockets("naudotojas-1") is None

    assert "Could not disconnect sockets for user naudotojas-1" in caplog.text


def test_a_chat_layer_raising_import_error_at_call_time_is_swallowed_too(monkeypatch):
    import app.chat.events as chat_events

    def _boom(uid):
        raise ImportError("half-loaded")

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _boom)

    assert admin_routes._disconnect_user_sockets("naudotojas-1") is None








# ===========================================================
# _write_audit — the helper on its own
# ===========================================================

def test_the_audit_helper_stores_the_actor_action_target_and_payload(app, admin, db):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        admin_routes._write_audit(conn, "user.role", "taikinys", {"from": "student"})
        conn.commit()

    row = _audit_rows(db)[0]
    assert row["actor_id"] == actor["id"]
    assert row["action"] == "user.role"
    assert row["target"] == "taikinys"
    assert json.loads(row["payload"]) == {"from": "student"}


def test_the_audit_helper_defaults_target_and_payload_to_null(app, admin, db):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        admin_routes._write_audit(conn, "invitation.revoke")
        conn.commit()

    row = _audit_rows(db)[0]
    assert row["target"] is None
    assert row["payload"] is None


@pytest.mark.parametrize("payload,stored", [({}, "{}"), ([], "[]"), (0, "0"),
                                            (False, "false"), ("", '""')])
def test_a_falsy_payload_is_still_serialised_because_the_check_is_is_not_none(app, admin, db,
                                                                             payload, stored):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        admin_routes._write_audit(conn, "test.action", None, payload)
        conn.commit()

    assert _audit_rows(db)[0]["payload"] == stored


def test_a_lithuanian_payload_is_stored_as_utf8_not_escape_sequences(app, admin, db):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        admin_routes._write_audit(conn, "test.action", None, {"vardas": "Ąžuolas"})
        conn.commit()

    assert _audit_rows(db)[0]["payload"] == '{"vardas": "Ąžuolas"}'


def test_the_audit_helper_stamps_created_at_in_iso_t_form(app, admin, db):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        admin_routes._write_audit(conn, "test.action")
        conn.commit()

    stamp = _audit_rows(db)[0]["created_at"]
    assert "T" in stamp and stamp.endswith("+00:00")


def test_every_audit_row_gets_its_own_uuid(app, admin, db):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        admin_routes._write_audit(conn, "one")
        admin_routes._write_audit(conn, "two")
        conn.commit()

    ids = [r["id"] for r in _audit_rows(db)]
    assert len(set(ids)) == 2
    assert all(uuid.UUID(i).version == 4 for i in ids)


def test_the_audit_row_is_invisible_to_other_connections_until_the_caller_commits(app, admin, db):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        admin_routes._write_audit(conn, "test.action")
        assert _audit_rows(db) == []
        conn.commit()

    assert len(_audit_rows(db)) == 1


def test_rolling_the_caller_back_takes_the_audit_row_with_it(app, admin, db):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (actor["id"],))
        admin_routes._write_audit(conn, "user.role", actor["id"])
        conn.rollback()

    assert _audit_rows(db) == []
    assert _row(db, actor["id"])["role"] == "admin"


def test_a_missing_audit_table_is_logged_and_swallowed(app, admin, db, caplog):
    actor, _ = admin
    db.execute("DROP TABLE admin_audit")
    db.commit()

    with _admin_request(app, actor["id"]) as conn:
        with caplog.at_level(logging.WARNING, logger="app.admin.routes"):
            admin_routes._write_audit(conn, "user.role", "taikinys")
        conn.commit()

    assert "admin_audit unavailable" in caplog.text
    assert actor["id"] in caplog.text


def test_the_surrounding_write_still_commits_when_the_audit_table_is_gone(app, admin, db):
    actor, _ = admin
    db.execute("DROP TABLE admin_audit")
    db.commit()

    with _admin_request(app, actor["id"]) as conn:
        conn.execute("UPDATE users SET role = 'curator' WHERE id = ?", (actor["id"],))
        admin_routes._write_audit(conn, "user.role", actor["id"])
        conn.commit()

    assert _row(db, actor["id"])["role"] == "curator"


def test_a_not_null_violation_on_the_action_is_swallowed(app, admin, db):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        admin_routes._write_audit(conn, None, "taikinys")
        conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (actor["id"],))
        conn.commit()

    assert _audit_rows(db) == []
    assert _row(db, actor["id"])["role"] == "teacher"


def test_an_actor_who_no_longer_exists_breaks_the_foreign_key_and_is_swallowed(app, db):
    with _admin_request(app, "vaiduoklis") as conn:
        admin_routes._write_audit(conn, "user.role", "taikinys")
        conn.commit()

    assert _audit_rows(db) == []


def test_writing_on_a_closed_connection_is_swallowed(app, admin, caplog):
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        conn.close()
        with caplog.at_level(logging.WARNING, logger="app.admin.routes"):
            admin_routes._write_audit(conn, "user.role", "taikinys")

    assert "admin_audit unavailable" in caplog.text


def test_a_payload_json_cannot_serialise_is_not_swallowed(app, admin):
    # The helper only promises to swallow sqlite errors — a
    # TypeError out of json.dumps escapes it. No route can build
    # one: every payload here is str/int/bool from a whitelist
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        with pytest.raises(TypeError):
            admin_routes._write_audit(conn, "test.action", None, {"obj": object()})


def test_the_helper_writes_on_the_connection_it_was_handed(app, admin, db):
    # Two connections, one commit: only the one that was passed
    # in may carry the row
    actor, _ = admin

    with _admin_request(app, actor["id"]) as conn:
        other = get_db()
        try:
            admin_routes._write_audit(conn, "test.action")
            other.commit()
            assert _audit_rows(db) == []
            conn.commit()
        finally:
            other.close()

    assert len(_audit_rows(db)) == 1








# ===========================================================
# GET /api/admin/stats — the counters
# ===========================================================

@pytest.mark.contract
def test_the_dashboard_answers_exactly_five_integer_counters(client, admin):
    _, headers = admin

    body = client.get(STATS, headers=headers).get_json()

    assert set(body) == STATS_FIELDS
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in body.values())


def test_a_fresh_database_reports_its_bootstrap_state(client, admin):
    _, headers = admin

    body = client.get(STATS, headers=headers).get_json()

    assert body == {"users": 1, "posts": 0, "scrapedArticles": 0, "comments": 0,
                    "activeInvitations": 1}


def test_the_user_counter_counts_inactive_accounts_too(client, admin, make_user):
    _, headers = admin
    make_user(active=0)
    make_user(active=0)

    assert client.get(STATS, headers=headers).get_json()["users"] == 3


@pytest.mark.parametrize("source", SCRAPER_SOURCES)
def test_each_scraper_source_counts_as_a_scraped_article(client, admin, db, source):
    _, headers = admin
    _seed_article(db, source)

    body = client.get(STATS, headers=headers).get_json()

    assert (body["posts"], body["scrapedArticles"]) == (1, 1)


@pytest.mark.parametrize("source", OTHER_SOURCES)
def test_no_other_source_counts_as_scraped(client, admin, db, source):
    _, headers = admin
    _seed_article(db, source)

    body = client.get(STATS, headers=headers).get_json()

    assert (body["posts"], body["scrapedArticles"]) == (1, 0)


def test_the_grouped_pass_adds_every_source_up(client, admin, db):
    _, headers = admin
    for source in SCRAPER_SOURCES + OTHER_SOURCES:
        _seed_article(db, source)
        _seed_article(db, source)

    body = client.get(STATS, headers=headers).get_json()

    assert body["posts"] == 10
    assert body["scrapedArticles"] == 4


def test_an_empty_news_table_leaves_both_post_counters_at_zero(client, admin):
    _, headers = admin

    body = client.get(STATS, headers=headers).get_json()

    assert (body["posts"], body["scrapedArticles"]) == (0, 0)


def test_the_comment_counter_counts_every_comment_on_every_post(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    first = _seed_article(db, "app")
    second = _seed_article(db, "knf.vu.lt")
    _seed_comment(db, first, user["id"])
    _seed_comment(db, second, user["id"])
    _seed_comment(db, second, user["id"])

    assert client.get(STATS, headers=headers).get_json()["comments"] == 3


@pytest.mark.parametrize("use_count,max_uses,active", [
    (0, 1, True), (0, 5, True), (4, 5, True),
    (1, 1, False), (5, 5, False), (6, 5, False), (0, 0, False),
])
def test_the_invitation_counter_reads_use_count_against_max_uses(client, admin, db,
                                                                 use_count, max_uses, active):
    actor, headers = admin
    _seed_code(db, actor["id"], _iso(3600), use_count=use_count, max_uses=max_uses)

    # the bootstrap code is always there and always active
    expected = 2 if active else 1
    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == expected


def test_a_code_that_expired_an_hour_ago_is_not_active(client, admin, db):
    actor, headers = admin
    _seed_code(db, actor["id"], _iso(-3600))

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 1


def test_a_code_expiring_this_very_second_is_not_active(client, admin, db):
    # The comparison is a strict '>', so the second a code names
    # is already too late
    actor, headers = admin
    this_second = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    _seed_code(db, actor["id"], this_second)

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 1


def test_a_nineteen_character_expiry_needs_no_offset_to_count(client, admin, db):
    actor, headers = admin
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    _seed_code(db, actor["id"], future)

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 2


def test_a_legacy_space_form_expiry_is_normalised_before_the_comparison(client, admin, db):
    actor, headers = admin
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    _seed_code(db, actor["id"], past)
    _seed_code(db, actor["id"], future)

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 2


def test_a_fractional_second_expiry_a_day_out_counts_as_active(client, admin, db):
    actor, headers = admin
    _seed_code(db, actor["id"], _iso(86400))

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 2


def test_an_unparsable_expiry_reaches_the_same_verdict_as_the_listing(client, admin, db):
    # A string comparison only means anything on a string that is
    # a date: 'netrukus' sorts above every timestamp the clock can
    # print, so the GLOB gate is what keeps the counter and the
    # listing on the same verdict
    actor, headers = admin
    _seed_code(db, actor["id"], "netrukus")

    listed = client.get("/api/admin/invitations", headers=headers).get_json()["invitations"]
    planted = [i for i in listed if i["expiresAt"] == "netrukus"][0]
    counted = client.get(STATS, headers=headers).get_json()["activeInvitations"]

    assert planted["expired"] is True
    assert counted == 1


def test_the_counters_move_with_the_rows_once_the_snapshot_is_stale(client, admin, make_user, db):
    _, headers = admin

    first = client.get(STATS, headers=headers).get_json()
    make_user()
    _seed_article(db, "vu.lt")
    _plant_snapshot(admin_routes._stats_cache["stats"], age=admin_routes._STATS_CACHE_TTL)
    second = client.get(STATS, headers=headers).get_json()

    assert first["users"] == 1 and first["posts"] == 0
    assert second["users"] == 2
    assert second["posts"] == 1 and second["scrapedArticles"] == 1








# ===========================================================
# GET /api/admin/stats — the 45 second snapshot
# ===========================================================

def test_the_first_call_builds_and_publishes_a_snapshot(client, admin):
    _, headers = admin

    body = client.get(STATS, headers=headers).get_json()

    assert admin_routes._stats_cache["stats"] == body
    assert "at" in admin_routes._stats_cache


def test_a_snapshot_one_second_inside_the_window_is_served_untouched(client, admin, make_user):
    _, headers = admin
    make_user()
    _plant_snapshot({"users": 999, "posts": 0, "scrapedArticles": 0, "comments": 0,
                     "activeInvitations": 0},
                    age=admin_routes._STATS_CACHE_TTL - 1)

    assert client.get(STATS, headers=headers).get_json()["users"] == 999


def test_a_snapshot_exactly_at_the_window_is_rebuilt(client, admin):
    _, headers = admin
    _plant_snapshot({"users": 999, "posts": 0, "scrapedArticles": 0, "comments": 0,
                     "activeInvitations": 0},
                    age=admin_routes._STATS_CACHE_TTL)

    assert client.get(STATS, headers=headers).get_json()["users"] == 1


def test_a_long_stale_snapshot_is_rebuilt(client, admin):
    _, headers = admin
    _plant_snapshot({"users": 999, "posts": 0, "scrapedArticles": 0, "comments": 0,
                     "activeInvitations": 0},
                    age=3600)

    assert client.get(STATS, headers=headers).get_json()["users"] == 1


def test_a_fresh_snapshot_is_handed_back_exactly_as_it_was_planted(client, admin):
    # Whatever sits in the cache is what ships — the route does
    # not reshape or re-key it on the way out
    _, headers = admin
    planted = {"users": 7, "posts": 6, "scrapedArticles": 5, "comments": 4,
               "activeInvitations": 3}
    _plant_snapshot(dict(planted), age=1)

    assert client.get(STATS, headers=headers).get_json() == planted


def test_two_calls_inside_the_window_answer_byte_identical_bodies(client, admin, make_user):
    _, headers = admin

    first = client.get(STATS, headers=headers).get_data()
    make_user()
    second = client.get(STATS, headers=headers).get_data()

    assert first == second


def test_a_rebuild_restamps_the_snapshot_clock(client, admin):
    _, headers = admin
    _plant_snapshot({"users": 999}, age=3600)

    client.get(STATS, headers=headers)

    assert time.monotonic() - admin_routes._stats_cache["at"] < 5


def test_an_empty_cache_dict_is_treated_as_no_snapshot_at_all(client, admin):
    _, headers = admin
    admin_routes._stats_cache.clear()

    response = client.get(STATS, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["users"] == 1


def test_the_dashboard_body_is_never_cacheable_by_the_client(client, admin):
    _, headers = admin

    response = client.get(STATS, headers=headers)

    assert response.headers["Cache-Control"] == "no-store"


def test_the_dashboard_is_admin_only(client, make_user, auth_headers):
    curator = make_user(role="curator")

    response = client.get(STATS, headers=auth_headers(curator))

    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"








# ===========================================================
# The races an admin console really runs into
# ===========================================================

def test_a_role_change_that_lands_on_a_row_deactivated_underneath_it_still_commits(
        client, admin, make_user, db, mid_request):
    _, headers = admin
    user = make_user()
    fired = mid_request(lambda conn: conn.execute(
        "UPDATE users SET active = 0 WHERE id = ?", (user["id"],)))

    response = _patch(client, headers, user["id"], role="teacher")
    row = _row(db, user["id"])

    assert fired == [True]
    assert response.status_code == 200
    assert (row["role"], row["active"]) == ("teacher", 0)
    assert response.get_json()["active"] is False


def test_the_guards_run_on_the_row_as_it_was_read_not_as_it_ends_up(client, admin, make_user,
                                                                    db, mid_request):
    # target was a student when STEP 1 read it, so demotes_admin
    # is False and the backstop is never consulted — the promotion
    # that landed in between is simply overwritten
    actor, headers = admin
    user = make_user()
    _uncount(db, actor["id"])
    mid_request(lambda conn: conn.execute(
        "UPDATE users SET role = 'admin' WHERE id = ?", (user["id"],)))

    response = _patch(client, headers, user["id"], role="student")

    assert response.status_code == 200
    assert _row(db, user["id"])["role"] == "student"


def test_the_admin_count_is_taken_before_the_write_and_never_rechecked(client, admin,
                                                                       make_user, db,
                                                                       mid_request):
    # The backstop is not transactional: a spare admin
    # deactivated between the COUNT and the UPDATE leaves the
    # console with zero active admins
    actor, headers = admin
    target = make_user(role="admin")
    _uncount(db, actor["id"])
    spare = make_user(role="admin")
    mid_request(lambda conn: conn.execute(
        "UPDATE users SET active = 0 WHERE id = ?", (spare["id"],)))

    response = _patch(client, headers, target["id"], role="student")

    assert response.status_code == 200
    assert db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND active = 1"
    ).fetchone()["c"] == 0


def test_an_audit_row_is_skipped_when_the_actor_vanishes_mid_request(client, admin, make_user,
                                                                     db, mid_request):
    # admin_audit.actor_id has a foreign key, so the trail write
    # fails — and _write_audit swallowing it is what lets the
    # user change commit anyway
    actor, headers = admin
    target = make_user()
    make_user(role="admin")
    mid_request(lambda conn: conn.execute("DELETE FROM users WHERE id = ?", (actor["id"],)))

    response = _patch(client, headers, target["id"], role="teacher")

    assert response.status_code == 200
    assert _row(db, target["id"])["role"] == "teacher"
    assert _audit_rows(db) == []


def test_a_target_deleted_mid_request_is_not_an_internal_error(app, client, admin, make_user,
                                                               mid_request):
    # The row is gone by the time STEP 5 re-reads it, so there is
    # nothing to answer with: the same 404 the id would have got
    # one millisecond earlier, never a bare 500
    app.config["PROPAGATE_EXCEPTIONS"] = False
    _, headers = admin
    user = make_user()
    mid_request(lambda conn: conn.execute("DELETE FROM users WHERE id = ?", (user["id"],)))

    response = _patch(client, headers, user["id"], role="teacher")

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_two_deactivations_of_the_same_user_both_purge_and_both_cut(client, admin, make_user,
                                                                    auth_headers, db,
                                                                    socket_kicks):
    _, headers = admin
    user = make_user()
    auth_headers(user)
    _seed_token(db, user["id"])

    first = _patch(client, headers, user["id"], active=False)
    second = _patch(client, headers, user["id"], active=False)

    assert (first.status_code, second.status_code) == (200, 200)
    assert socket_kicks == [user["id"], user["id"]]
    assert (_sessions(db, user["id"]), _tokens(db, user["id"])) == (0, 0)
