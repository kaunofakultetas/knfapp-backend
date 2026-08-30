# -----------------------------------------------------------
#  [*] Tests — notifications routes, the exhaustive pass
#      (app/notifications/routes.py — every route in the module)
#
#  The broad suite already walks every line and every branch of
#  the four push routes. This file is the pass over what line
#  coverage cannot see: the corners where two paths execute the
#  same statements and only the OUTCOME differs.
#
#  What it pins:
#
#    - dispatch and transport before the handler ever runs: the
#      method matrix on both paths, the trailing slash that is
#      a different URL, HEAD, the CORS preflight that carries
#      no bearer token, every content type that is and is not
#      JSON, the 6 MB ceiling and the 32-level nesting guard.
#    - authentication permutations for all four routes: guest,
#      every malformed Authorization shape, a case-folded
#      scheme, a deactivated account, and an EXPIRED session,
#      which takes the caller's devices with it on the way out.
#      Every role may register and flip a switch — none of
#      these routes has a role gate.
#    - the token grammar from the wire side: markup, entities
#      and ampersands posted as RAW bytes (TESTPLAN rule 10),
#      NUL smuggled after the closing bracket, unicode
#      whitespace padding, and a body carrying someone else's
#      user id, which is ignored.
#    - the upsert as an IDENTITY: five registrations of one
#      token keep one row and one id, a takeover keeps the row
#      id and created_at while reviving active, and exactly one
#      row exists for a token globally.
#    - the cap as an ORDERING: an over-cap fleet heals to ten in
#      one call, a legacy space-form timestamp sorts oldest and
#      is the first to go (migration v17's gotcha), and a
#      thousand rows are pruned in one statement.
#    - the failure path as a TRANSACTION: when the upsert loses
#      a race the eviction it had already done is rolled back,
#      so a lost race can never cost a device; the caller's own
#      row answers 200 unchanged, the connection is released,
#      and no log line anywhere quotes a raw token.
#    - the switches as a CONTRACT: validation precedence inside
#      one dict, duplicate keys in the raw body, a channel name
#      carrying markup named back html-escaped on the wire, the
#      legacy enabled values the opt-out model still reads, and
#      the composite primary key that keeps one row per switch.
#    - the two write budgets: a refused body still spends one, a
#      guest spends none, a 429 writes nothing, and the read is
#      unmetered.
# -----------------------------------------------------------

import html
import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import pytest
import time_machine

from app.auth import routes as auth_routes
from app.notifications import routes as notif_routes
from app.notifications.push import VALID_CHANNELS


REGISTER = "/api/notifications/register"
CHANNELS = "/api/notifications/channels"

# One legal token for the tests that do not care which
TOKEN = "ExponentPushToken[deepslice01]"

# The four calls this module owns, as (method, path, body) —
# every route-wide permutation (guest, deactivated, method)
# rides this list so no route can quietly escape one
CALLS = [
    ("post", REGISTER, {"token": TOKEN}),
    ("delete", REGISTER, {"token": TOKEN}),
    ("get", CHANNELS, None),
    ("put", CHANNELS, {"channels": {"news": False}}),
]




# -----------------------------------------------------------
# _clean_rate_limit_store
# -----------------------------------------------------------
#
# The limiter dict lives at module scope and outlives the `app`
# fixture's fresh database. Cleared on both sides so a sibling
# module's writes never spend this module's budget and nothing
# seeded here leaks out.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limit_store():
    auth_routes._rate_limit_store.clear()
    yield
    auth_routes._rate_limit_store.clear()




# -----------------------------------------------------------
# _tok
# -----------------------------------------------------------
#
# A distinct legal token per call: the id is padded to the
# grammar's 10-character floor, so a two-letter seed is still
# something Expo could have minted.
# -----------------------------------------------------------

def _tok(seed):
    body = "".join(c for c in str(seed) if c.isalnum() or c in "_-")
    return f"ExponentPushToken[{body.ljust(10, 'z')[:64]}]"




# -----------------------------------------------------------
# _register / _unregister / _get_channels / _put_channels
# -----------------------------------------------------------
#
# The four calls through the test client, returning the raw
# response so an unhappy path can read its status and body.
# -----------------------------------------------------------

def _register(client, headers, token=TOKEN, **extra):
    payload = {"token": token}
    payload.update(extra)
    return client.post(REGISTER, headers=headers, json=payload)


def _unregister(client, headers, token=TOKEN, **extra):
    payload = {"token": token}
    payload.update(extra)
    return client.delete(REGISTER, headers=headers, json=payload)


def _get_channels(client, headers):
    return client.get(CHANNELS, headers=headers)


def _put_channels(client, headers, channels, **extra):
    payload = {"channels": channels}
    payload.update(extra)
    return client.put(CHANNELS, headers=headers, json=payload)




# -----------------------------------------------------------
# _raw
# -----------------------------------------------------------
#
# A body put on the wire BYTE FOR BYTE. TESTPLAN rule 10: the
# test client serialises a `json=` kwarg through the app's own
# html-escaping provider, so a token or a channel name carrying
# markup would arrive pre-escaped and no real client sends
# that. Everything about markup, entities or quotes goes
# through here.
# -----------------------------------------------------------

def _raw(client, method, path, headers, body, content_type="application/json"):
    return getattr(client, method)(
        path, data=body, headers={**headers, "Content-Type": content_type})




# -----------------------------------------------------------
# _msg
# -----------------------------------------------------------
#
# The error prose as it was WRITTEN: the provider escapes every
# string it serialises, so a message naming a channel goes out
# as "&#x27;news&#x27;" and the mobile client decodes it again.
# -----------------------------------------------------------

def _msg(response):
    return html.unescape(response.get_json()["error"])




# -----------------------------------------------------------
# _rows / _row / _tokens_of / _count
# -----------------------------------------------------------
#
# What the routes actually wrote — these tests are about
# persistence at least as often as about the body.
# -----------------------------------------------------------

def _rows(db, user_id):
    return db.execute(
        "SELECT * FROM push_tokens WHERE user_id = ? ORDER BY updated_at", (user_id,)).fetchall()


def _row(db, token):
    return db.execute("SELECT * FROM push_tokens WHERE token = ?", (token,)).fetchone()


def _tokens_of(db, user_id):
    return {r["token"] for r in _rows(db, user_id)}


def _count(db, table):
    return db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]




# -----------------------------------------------------------
# _seed_token
# -----------------------------------------------------------
#
# A push_tokens row the route itself would never write: one
# push.py has already deactivated, one whose updated_at is old
# or in the legacy space form the cap's ORDER BY sorts on, or a
# legacy token that could not pass _TOKEN_RE today.
# -----------------------------------------------------------

def _seed_token(db, user_id, token, platform="ios", language="lt", active=1,
                updated_at="2020-01-01T00:00:00", created_at="2019-01-01T00:00:00"):
    row_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO push_tokens (id, user_id, token, platform, language, active,
                                    created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (row_id, user_id, token, platform, language, active, created_at, updated_at))
    db.commit()
    return row_id




# -----------------------------------------------------------
# _seed_channel
# -----------------------------------------------------------
#
# An explicit notification_channels row — the only thing that
# can turn a switch off, a missing row meaning enabled.
# `enabled` is free-form so a legacy non-binary value can be
# planted the way an older backend left one.
# -----------------------------------------------------------

def _seed_channel(db, user_id, channel, enabled):
    db.execute(
        "INSERT INTO notification_channels (user_id, channel, enabled, updated_at)"
        " VALUES (?, ?, ?, '2020-01-01T00:00:00')", (user_id, channel, enabled))
    db.commit()




# -----------------------------------------------------------
# _fill_bucket
# -----------------------------------------------------------
#
# Fills a per-user rate-limit bucket to the brim without making
# the calls: `scope` is the decorator's scope, the key its
# per-user form.
# -----------------------------------------------------------

def _fill_bucket(monkeypatch, scope, user_id, attempts):
    monkeypatch.setitem(auth_routes._rate_limit_store,
                        f"{scope}:{user_id}", [time.monotonic()] * attempts)




# -----------------------------------------------------------
# _LosingConnection / _lose_the_insert
# -----------------------------------------------------------
#
# A real connection whose push_tokens INSERT always loses the
# race: it raises the IntegrityError SQLite raises on the
# UNIQUE token index and lets every other statement through.
# Two app starts landing at once is the only way to reach the
# route's belt-and-braces rollback, and the point of these
# tests is what the ROLLBACK undoes — the eviction the route
# had already done in the same transaction.
#
# Used by:
#   - the lost-race and vanished-row tests below
# -----------------------------------------------------------

class _LosingConnection:

    def __init__(self, real):
        self._real = real

    def execute(self, sql, params=()):
        if "INSERT INTO push_tokens" in sql:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: push_tokens.token")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _lose_the_insert(monkeypatch):
    real_get_db = notif_routes.get_db
    monkeypatch.setattr(notif_routes, "get_db", lambda: _LosingConnection(real_get_db()))




# ===========================================================
# Dispatch — what answers before the handler does
# ===========================================================


@pytest.mark.parametrize("method", ["get", "put", "patch"])
def test_the_register_path_answers_only_post_and_delete(client, actor, method):
    _, headers = actor

    response = getattr(client, method)(REGISTER, headers=headers, json={"token": TOKEN})

    assert response.status_code == 405
    assert response.get_json()["error"] == "Method not allowed"


@pytest.mark.parametrize("method", ["post", "delete", "patch"])
def test_the_channels_path_answers_only_get_and_put(client, actor, method):
    _, headers = actor

    response = getattr(client, method)(CHANNELS, headers=headers,
                                       json={"channels": {"news": False}})

    assert response.status_code == 405
    assert response.get_json()["error"] == "Method not allowed"


@pytest.mark.parametrize("path", [REGISTER + "/", CHANNELS + "/"])
def test_a_trailing_slash_is_a_different_url_entirely(client, actor, path):
    # The rules are declared without one, so a client that adds
    # a slash gets the house 404, not the route
    _, headers = actor

    response = client.post(path, headers=headers, json={"token": TOKEN})

    assert response.status_code == 404
    assert response.get_json()["error"] == "Not found"


def test_an_unknown_path_under_the_blueprint_is_a_404(client, actor):
    _, headers = actor

    response = client.get("/api/notifications/settings", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Not found"


def test_a_head_request_reads_the_switches_with_no_body(client, actor):
    # Flask answers HEAD off the GET rule; the route still runs,
    # so this proves the read is side-effect free as well
    _, headers = actor

    response = client.head(CHANNELS, headers=headers)

    assert response.status_code == 200
    assert response.data == b""


def test_a_cors_preflight_needs_no_bearer_token(client):
    # A rejected OPTIONS would surface in the browser as a CORS
    # error rather than a 401, so the preflight is exempt from
    # both the auth gate and the global throttle
    response = client.options(REGISTER, headers={
        "Origin": "http://localhost:8081",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })

    assert response.status_code in (200, 204)
    assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:8081"




# ===========================================================
# Transport — which bodies are JSON at all
# ===========================================================


def test_a_json_body_with_a_charset_is_still_json(client, actor, db):
    _, headers = actor

    response = _raw(client, "post", REGISTER, headers, json.dumps({"token": TOKEN}),
                    "application/json; charset=utf-8")

    assert response.status_code == 201
    assert _row(db, TOKEN) is not None


def test_a_vendor_json_mimetype_is_accepted_as_json(client, actor, db):
    _, headers = actor

    response = _raw(client, "post", REGISTER, headers, json.dumps({"token": TOKEN}),
                    "application/vnd.api+json")

    assert response.status_code == 201
    assert _row(db, TOKEN) is not None


@pytest.mark.parametrize("content_type", [
    "text/plain",
    "application/x-www-form-urlencoded",
    "application/octet-stream",
    "application/jsonx",   # merely CONTAINING "json" is not a JSON mimetype to Flask
])
def test_a_body_that_is_not_json_never_reaches_the_token_check(client, actor, db, content_type):
    _, headers = actor

    response = _raw(client, "post", REGISTER, headers, json.dumps({"token": TOKEN}), content_type)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"
    assert _count(db, "push_tokens") == 0


def test_a_body_with_no_content_type_at_all_is_refused(client, actor):
    _, headers = actor

    response = client.post(REGISTER, headers=headers, data=json.dumps({"token": TOKEN}))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"


def test_a_utf8_bom_does_not_hide_the_token(client, actor, db):
    # json.loads sniffs the byte-order mark off a bytes body
    # (utf-8-sig), so a client whose serialiser prefixes one
    # still registers instead of collecting a 400
    _, headers = actor

    response = _raw(client, "post", REGISTER, headers,
                    "﻿" + json.dumps({"token": TOKEN}))

    assert response.status_code == 201
    assert _row(db, TOKEN) is not None


def test_a_body_over_the_six_megabyte_ceiling_never_reaches_the_route(client, actor, db):
    _, headers = actor
    body = b'{"token": "' + b"A" * (7 * 1024 * 1024) + b'"}'

    response = client.post(REGISTER, headers=headers, data=body,
                           content_type="application/json")

    assert response.status_code == 413
    assert response.get_json()["code"] == "file_too_large"
    assert _count(db, "push_tokens") == 0


@pytest.mark.parametrize("method,path", [("post", REGISTER), ("put", CHANNELS)])
def test_an_over_nested_body_is_refused_before_the_route(client, actor, method, path):
    # The app-wide guard folds at 32 levels, so a hostile body
    # is a 400 and never a RecursionError 500. Raw bytes, and
    # not only for rule 10: a `json=` kwarg this deep dies in
    # the test client's own serialiser before it is ever sent
    _, headers = actor
    body = {"token": TOKEN, "channels": {}}
    node = body
    for _ in range(40):
        node["nested"] = {}
        node = node["nested"]

    response = _raw(client, method, path, headers, json.dumps(body))

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON nesting too deep"




# ===========================================================
# Authentication — the same gate on all four routes
# ===========================================================


@pytest.mark.parametrize("method,path,body", CALLS)
def test_every_route_refuses_a_guest(client, method, path, body):
    kwargs = {"json": body} if body is not None else {}

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


@pytest.mark.parametrize("header", [
    "", "Bearer", "Bearer    ", "Basic YWRtaW46YWRtaW4=", "Token abcdef",
    "Bearer 00000000-0000-0000-0000-000000000000",
])
def test_a_header_that_is_not_a_live_bearer_token_is_refused(client, header):
    response = client.post(REGISTER, headers={"Authorization": header},
                           json={"token": TOKEN})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_the_bearer_scheme_is_matched_case_insensitively(client, actor, db):
    # RFC 7235: auth schemes are not case-sensitive, and the
    # detached logout unregister rebuilds the header by hand
    _, headers = actor
    raw_token = headers["Authorization"].split(" ", 1)[1]

    response = client.post(REGISTER, headers={"Authorization": f"bEaReR {raw_token}"},
                           json={"token": TOKEN})

    assert response.status_code == 201
    assert _row(db, TOKEN) is not None


@pytest.mark.parametrize("method,path,body", CALLS)
def test_a_deactivated_account_reaches_none_of_the_routes(client, actor, db, method, path, body):
    user, headers = actor
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()
    kwargs = {"json": body} if body is not None else {}

    response = getattr(client, method)(path, headers=headers, **kwargs)

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_an_expired_session_is_refused_and_takes_the_devices_with_it(client, actor, db):
    # resolve_session_token purges push_tokens with the session:
    # a device that can no longer authenticate must not keep
    # receiving previews
    user, headers = actor
    assert _register(client, headers).status_code == 201
    db.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00+00:00' WHERE user_id = ?",
               (user["id"],))
    db.commit()

    response = _register(client, headers, token=_tok("second"))

    assert response.status_code == 401
    assert _rows(db, user["id"]) == []


@pytest.mark.parametrize("role", ["student", "teacher", "curator", "admin"])
def test_every_role_may_register_a_device_and_flip_a_switch(client, make_user, auth_headers, role):
    # None of these routes has a role gate — push is a setting,
    # not a privilege
    user = make_user(role=role)
    headers = auth_headers(user)

    assert _register(client, headers, token=_tok(role)).status_code == 201
    assert _put_channels(client, headers, {"news": False}).status_code == 200
    assert _get_channels(client, headers).get_json()["channels"]["news"] is False


def test_deleting_the_user_takes_the_devices_and_the_switches_with_them(client, actor, db):
    # ON DELETE CASCADE on both tables — the fan-out can never
    # read a row belonging to a user who no longer exists
    user, headers = actor
    _register(client, headers)
    _put_channels(client, headers, {"chat": False})

    db.execute("PRAGMA foreign_keys=ON")
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    db.commit()

    assert _count(db, "push_tokens") == 0
    assert _count(db, "notification_channels") == 0




# ===========================================================
# POST /register — the body from the wire side
# ===========================================================


@pytest.mark.parametrize("body,expected", [
    (None, "Push token required"),
    ({}, "Push token required"),
    ({"platform": "ios"}, "Push token required"),
    ({"token": ""}, "Push token required"),
    ({"token": 0}, "Push token required"),
    ({"token": False}, "Push token required"),
    ({"token": None}, "Push token required"),
    ({"token": []}, "Push token required"),
    ({"token": {}}, "Push token required"),
    ({"token": 12345}, "Token must be a string"),
    ({"token": True}, "Token must be a string"),
    ({"token": 3.5}, "Token must be a string"),
    ({"token": ["ExponentPushToken[abcdefghij]"]}, "Token must be a string"),
    ({"token": {"token": "x"}}, "Token must be a string"),
])
def test_the_post_guard_clauses_answer_before_any_row_is_written(client, actor, db,
                                                                 body, expected):
    # An empty container is caught by the truthiness check, a
    # non-empty one by the type check — the two guards read the
    # same value and only their order tells them apart
    _, headers = actor
    kwargs = {"json": body} if body is not None else {}

    response = client.post(REGISTER, headers=headers, **kwargs)

    assert response.status_code == 400
    assert response.get_json()["error"] == expected
    assert _count(db, "push_tokens") == 0


def test_the_length_cap_is_measured_after_stripping_on_the_post_too(client, actor, db):
    # 201 characters is refused by length, 200 by grammar, and a
    # legal token padded far past the cap is neither
    _, headers = actor

    over = _register(client, headers, token="X" * 201)
    at_cap = _register(client, headers, token="X" * 200)
    padded = _register(client, headers, token=" " * 400 + TOKEN + " " * 400)

    assert (over.status_code, over.get_json()["error"]) == (400, "Token too long")
    assert (at_cap.status_code, at_cap.get_json()["error"]) == (
        400, "Invalid Expo push token format")
    assert padded.status_code == 201
    assert _tokens_of(db, _row(db, TOKEN)["user_id"]) == {TOKEN}


def test_the_token_field_name_is_case_sensitive(client, actor, db):
    _, headers = actor

    response = client.post(REGISTER, headers=headers, json={"Token": TOKEN})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"
    assert _count(db, "push_tokens") == 0


def test_a_body_naming_another_user_still_registers_to_the_caller(client, db, make_user,
                                                                  auth_headers):
    # Nothing but request.user decides the owner: the extra keys
    # are read by no one
    mine = make_user()
    other = make_user()

    response = client.post(REGISTER, headers=auth_headers(mine), json={
        "token": TOKEN, "user_id": other["id"], "userId": other["id"],
        "active": 0, "id": "chosen-by-the-client",
    })

    assert response.status_code == 201
    row = _row(db, TOKEN)
    assert row["user_id"] == mine["id"]
    assert row["active"] == 1
    assert row["id"] != "chosen-by-the-client"
    assert response.get_json()["tokenId"] == row["id"]


@pytest.mark.parametrize("padded", [
    " " + TOKEN + " ",     # non-breaking spaces
    " " + TOKEN + " ",     # em space / thin space
    "\r\n\t " + TOKEN + " \r\n\t",
    "\x0b\x0c" + TOKEN + "\x0c\x0b",  # vertical tab / form feed
])
def test_every_kind_of_surrounding_whitespace_is_stripped(client, actor, db, padded):
    # str.strip() is unicode-aware, so a client that pads with a
    # non-breaking space still lands on the same single row
    _, headers = actor

    response = _register(client, headers, token=padded)

    assert response.status_code == 201
    assert _tokens_of(db, _row(db, TOKEN)["user_id"]) == {TOKEN}


def test_a_nul_byte_after_the_closing_bracket_is_stripped_and_the_token_lands(client, actor, db):
    # validate_json_input NUL-strips every string before the
    # grammar check, so the smuggled byte leaves a legal token
    _, headers = actor

    response = _register(client, headers, token=TOKEN + "\x00")

    assert response.status_code == 201
    assert _row(db, TOKEN) is not None


@pytest.mark.parametrize("hostile", [
    'ExponentPushToken[abcdefghij]"; DROP TABLE push_tokens; --',
    "ExponentPushToken[<script>alert(1)</script>]",
    "ExponentPushToken[abc&amp;defghij]",
    "ExponentPushToken[abcdefghij]&",
    "<b>ExponentPushToken[abcdefghij]</b>",
    "ExponentPushToken[abcde\"fghij]",
])
def test_markup_and_quotes_on_the_raw_wire_never_reach_the_column(client, actor, db, hostile):
    # Posted byte for byte, NOT through the escaping provider —
    # the regex has to judge what a real client could send
    _, headers = actor

    response = _raw(client, "post", REGISTER, headers, json.dumps({"token": hostile}))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid Expo push token format"
    assert _count(db, "push_tokens") == 0


@pytest.mark.parametrize("lookalike", [
    "ＥxponentPushToken[abcdefghij]",   # full-width E
    "ExponentPushToken〔abcdefghij〕",  # CJK brackets
    "ExponentPushToken[abcdefghij）",
    "ExponentPushToken[абвгдежзий]",   # Cyrillic id
])
def test_a_lookalike_token_is_not_the_expo_grammar(client, actor, db, lookalike):
    _, headers = actor

    response = _register(client, headers, token=lookalike)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid Expo push token format"
    assert _count(db, "push_tokens") == 0


def test_an_id_of_nothing_but_punctuation_from_the_alphabet_is_legal(client, actor, db):
    token = "ExponentPushToken[__--__--__]"

    _, headers = actor

    assert _register(client, headers, token=token).status_code == 201
    assert _row(db, token) is not None


@pytest.mark.parametrize("length,expected", [(9, 400), (10, 201), (64, 201), (65, 400)])
def test_the_id_length_boundaries_are_exact(client, actor, length, expected):
    _, headers = actor

    response = _register(client, headers, token=f"ExponentPushToken[{'a' * length}]")

    assert response.status_code == expected




# ===========================================================
# POST /register — platform and language fall-backs
# ===========================================================


@pytest.mark.parametrize("platform", [{}, [], 0, False, True, 1.5, "IOS ", " android", "Android"])
def test_anything_outside_the_platform_whitelist_is_stored_as_unknown(client, actor, db, platform):
    _, headers = actor

    assert _register(client, headers, platform=platform).status_code == 201
    assert _row(db, TOKEN)["platform"] == "unknown"


@pytest.mark.parametrize("language", [{}, [], 0, False, True, 1.5, "LT", " en", "en-GB"])
def test_anything_outside_the_two_languages_falls_back_to_lithuanian(client, actor, db, language):
    _, headers = actor

    assert _register(client, headers, language=language).status_code == 201
    assert _row(db, TOKEN)["language"] == "lt"


def test_the_platform_and_language_are_refreshed_on_every_registration(client, actor, db):
    # The app re-registers on every start; a device that changed
    # its language must not keep receiving the old copy
    _, headers = actor
    _register(client, headers, platform="ios", language="en")

    _register(client, headers, platform="android", language="lt")

    row = _row(db, TOKEN)
    assert (row["platform"], row["language"]) == ("android", "lt")




# ===========================================================
# POST /register — the upsert as an identity
# ===========================================================


def test_five_registrations_of_one_token_keep_one_row_and_one_id(client, actor, db):
    user, headers = actor

    first = _register(client, headers)
    repeats = [_register(client, headers) for _ in range(4)]

    assert first.status_code == 201
    assert [r.status_code for r in repeats] == [200, 200, 200, 200]
    assert len(_rows(db, user["id"])) == 1
    assert {r.get_json()["tokenId"] for r in repeats} == {first.get_json()["tokenId"]}


def test_the_register_response_carries_exactly_two_keys(client, actor):
    _, headers = actor

    body = _register(client, headers).get_json()

    assert set(body) == {"registered", "tokenId"}
    assert body["registered"] is True
    assert isinstance(body["tokenId"], str)


def test_the_token_id_is_the_row_id_the_table_holds(client, actor, db):
    _, headers = actor

    token_id = _register(client, headers).get_json()["tokenId"]

    assert _row(db, TOKEN)["id"] == token_id
    assert uuid.UUID(token_id).version == 4


def test_two_devices_of_one_user_get_two_different_row_ids(client, actor, db):
    user, headers = actor

    first = _register(client, headers, token=_tok("phone")).get_json()["tokenId"]
    second = _register(client, headers, token=_tok("tablet")).get_json()["tokenId"]

    assert first != second
    assert len(_rows(db, user["id"])) == 2


def test_registering_a_device_never_writes_a_channel_row(client, actor, db):
    # A missing row means enabled; registering must not turn the
    # opt-out model into an opt-in one by planting defaults
    _, headers = actor

    _register(client, headers)

    assert _count(db, "notification_channels") == 0


def test_a_takeover_keeps_the_row_id_and_created_at_and_revives_it(client, db, make_user,
                                                                   auth_headers):
    # A phone changing hands: DO UPDATE keeps the ORIGINAL row,
    # so only the owner, the stamp and active move
    previous = make_user()
    new_owner = make_user()
    row_id = _seed_token(db, previous["id"], TOKEN, active=0,
                         created_at="2019-01-01T00:00:00", updated_at="2019-01-01T00:00:00")

    response = _register(client, auth_headers(new_owner), platform="android")

    assert response.status_code == 201
    row = _row(db, TOKEN)
    assert row["id"] == row_id
    assert response.get_json()["tokenId"] == row_id
    assert row["user_id"] == new_owner["id"]
    assert row["created_at"] == "2019-01-01T00:00:00"
    assert row["updated_at"] > "2019-01-01T00:00:00"
    assert row["active"] == 1
    assert row["platform"] == "android"


def test_the_new_owner_gets_two_hundred_on_the_next_start(client, db, make_user, auth_headers):
    previous = make_user()
    new_owner = make_user()
    _seed_token(db, previous["id"], TOKEN)
    headers = auth_headers(new_owner)
    assert _register(client, headers).status_code == 201

    response = _register(client, headers)

    assert response.status_code == 200


def test_exactly_one_row_exists_for_a_token_globally_after_a_takeover(client, db, make_user,
                                                                      auth_headers):
    previous = make_user()
    new_owner = make_user()
    _seed_token(db, previous["id"], TOKEN)

    _register(client, auth_headers(new_owner))

    assert _count(db, "push_tokens") == 1
    assert _rows(db, previous["id"]) == []


def test_a_takeover_leaves_the_previous_owners_other_devices_alone(client, db, make_user,
                                                                   auth_headers):
    previous = make_user()
    new_owner = make_user()
    _seed_token(db, previous["id"], TOKEN)
    _seed_token(db, previous["id"], _tok("keeper"))

    _register(client, auth_headers(new_owner))

    assert _tokens_of(db, previous["id"]) == {_tok("keeper")}


def test_no_log_line_anywhere_quotes_a_raw_token(client, db, make_user, auth_headers, caplog):
    # Takeover, eviction and the vanished-row error all log; not
    # one of them may put a live token in a log file
    caplog.set_level(logging.DEBUG)
    previous = make_user()
    new_owner = make_user()
    _seed_token(db, previous["id"], TOKEN)
    headers = auth_headers(new_owner)
    for i in range(10):
        _seed_token(db, new_owner["id"], _tok(f"dev{i}"), updated_at=f"2020-01-{i + 1:02d}T00:00:00")

    _register(client, headers)

    assert "ExponentPushToken[" not in caplog.text
    assert "Push token reassigned" in caplog.text
    assert "Dropped" in caplog.text




# ===========================================================
# POST /register — the cap as an ordering
# ===========================================================


def test_an_over_cap_fleet_is_healed_to_the_cap_in_one_call(client, actor, db):
    # Rows seeded past the cap (a fleet from before the limit)
    # are pruned by the next registration, not left forever
    user, headers = actor
    for i in range(15):
        _seed_token(db, user["id"], _tok(f"old{i}"), updated_at=f"2020-01-{i + 1:02d}T00:00:00")

    response = _register(client, headers, token=_tok("fresh"))

    assert response.status_code == 201
    assert len(_rows(db, user["id"])) == 10


def test_the_survivors_are_the_nine_newest_plus_the_one_just_registered(client, actor, db):
    user, headers = actor
    for i in range(1, 13):
        _seed_token(db, user["id"], _tok(f"d{i}"), updated_at=f"2030-01-{i:02d}T00:00:00")

    _register(client, headers, token=_tok("fresh"))

    assert _tokens_of(db, user["id"]) == {_tok(f"d{i}") for i in range(4, 13)} | {_tok("fresh")}


def test_a_legacy_space_form_timestamp_sorts_oldest_and_goes_first(client, actor, db):
    # Migration v17's gotcha, still live in the cap's ORDER BY:
    # " " sorts before "T", so the same instant written the old
    # way looks older than every T-form row
    user, headers = actor
    for i in range(9):
        _seed_token(db, user["id"], _tok(f"iso{i}"), updated_at="2030-01-01T00:00:00")
    _seed_token(db, user["id"], _tok("legacy"), updated_at="2030-01-01 00:00:00")

    _register(client, headers, token=_tok("fresh"))

    assert _tok("legacy") not in _tokens_of(db, user["id"])
    assert len(_rows(db, user["id"])) == 10


def test_the_cap_counts_the_fleet_before_the_upsert_so_a_takeover_still_fits(client, db,
                                                                             make_user,
                                                                             auth_headers):
    # The surplus query excludes the token being registered, so
    # taking over a device at the cap evicts exactly one row
    previous = make_user()
    new_owner = make_user()
    _seed_token(db, previous["id"], TOKEN)
    for i in range(10):
        _seed_token(db, new_owner["id"], _tok(f"own{i}"),
                    updated_at=f"2030-01-{i + 1:02d}T00:00:00")

    response = _register(client, auth_headers(new_owner))

    assert response.status_code == 201
    assert len(_rows(db, new_owner["id"])) == 10
    assert TOKEN in _tokens_of(db, new_owner["id"])


def test_nothing_is_evicted_while_the_fleet_is_under_the_cap(client, actor, db, caplog):
    caplog.set_level(logging.INFO)
    user, headers = actor
    for i in range(9):
        _seed_token(db, user["id"], _tok(f"dev{i}"), updated_at=f"2030-01-{i + 1:02d}T00:00:00")

    _register(client, headers, token=_tok("tenth"))

    assert len(_rows(db, user["id"])) == 10
    assert "Dropped" not in caplog.text


def test_a_thousand_rows_are_pruned_in_one_statement(client, actor, db):
    # The eviction builds one placeholder per surplus row; a
    # fleet far past the cap must not blow the statement up
    user, headers = actor
    db.executemany(
        """INSERT INTO push_tokens (id, user_id, token, platform, language, active,
                                    created_at, updated_at)
           VALUES (?, ?, ?, 'ios', 'lt', 1, '2020-01-01T00:00:00', ?)""",
        [(str(uuid.uuid4()), user["id"], _tok(f"bulk{i}"), f"2020-01-01T00:00:{i % 60:02d}")
         for i in range(1005)])
    db.commit()

    response = _register(client, headers, token=_tok("fresh"))

    assert response.status_code == 201
    assert len(_rows(db, user["id"])) == 10


def test_the_eviction_never_reaches_another_users_fleet(client, db, make_user, auth_headers):
    mine = make_user()
    other = make_user()
    for i in range(10):
        _seed_token(db, mine["id"], _tok(f"mine{i}"), updated_at=f"2030-01-{i + 1:02d}T00:00:00")
        _seed_token(db, other["id"], _tok(f"other{i}"), updated_at="2019-01-01T00:00:00")

    _register(client, auth_headers(mine), token=_tok("fresh"))

    assert len(_rows(db, mine["id"])) == 10
    assert len(_rows(db, other["id"])) == 10




# ===========================================================
# POST /register — the failure path as a transaction
# ===========================================================


def test_a_lost_race_on_our_own_device_answers_two_hundred_without_changing_it(client, actor, db,
                                                                               monkeypatch):
    # The row is already ours and already there: the rollback
    # leaves it exactly as it was and the id goes out of the
    # TABLE, so the caller still gets a usable answer
    user, headers = actor
    row_id = _seed_token(db, user["id"], TOKEN, platform="ios", language="lt", active=0)
    _lose_the_insert(monkeypatch)

    response = _register(client, headers, platform="android", language="en")

    assert response.status_code == 200
    assert response.get_json() == {"registered": True, "tokenId": row_id}
    row = _row(db, TOKEN)
    assert (row["platform"], row["language"], row["active"]) == ("ios", "lt", 0)


def test_a_lost_race_rolls_the_eviction_back_so_no_device_is_lost(client, actor, db, monkeypatch):
    # The eviction and the upsert share one transaction: if the
    # insert dies, the rows it had already dropped come back
    user, headers = actor
    for i in range(1, 13):
        _seed_token(db, user["id"], _tok(f"d{i}"), updated_at=f"2030-01-{i:02d}T00:00:00")
    _lose_the_insert(monkeypatch)

    response = _register(client, headers, token=_tok("fresh"))

    assert response.status_code == 500
    assert response.get_json()["error"] == "Could not register push token"
    assert _tokens_of(db, user["id"]) == {_tok(f"d{i}") for i in range(1, 13)}


def test_the_lost_race_and_the_vanished_row_are_logged_by_digest_only(client, actor, db,
                                                                      monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _, headers = actor
    _lose_the_insert(monkeypatch)

    _register(client, headers)

    assert "Push token registration raced" in caplog.text
    assert "Push token vanished during registration" in caplog.text
    assert TOKEN not in caplog.text


def test_the_connection_is_released_even_when_the_registration_fails(client, actor, db,
                                                                     monkeypatch):
    # finally: db.close() — a leaked connection would leave the
    # database locked and the next write would time out
    _, headers = actor
    _lose_the_insert(monkeypatch)
    assert _register(client, headers).status_code == 500

    monkeypatch.undo()

    assert _register(client, headers).status_code == 201
    assert _row(db, TOKEN) is not None




# ===========================================================
# DELETE /register — owner-scoped removal
# ===========================================================


def test_a_token_of_nothing_but_spaces_is_present_then_deleted_as_empty(client, actor, db):
    # "   " is truthy, so it passes the required check, and
    # strip() then leaves an empty string no row can match
    user, headers = actor
    _register(client, headers)

    response = _unregister(client, headers, token="   ")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Token not found"
    assert len(_rows(db, user["id"])) == 1


def test_the_delete_matches_the_token_case_sensitively(client, actor, db):
    user, headers = actor
    _register(client, headers)

    response = _unregister(client, headers, token=TOKEN.upper())

    assert response.status_code == 404
    assert len(_rows(db, user["id"])) == 1


def test_a_nul_bearing_legacy_row_cannot_be_named_from_the_wire(client, actor, db):
    # validate_json_input strips the NUL out of the body, so the
    # string that reaches the DELETE can never equal the stored
    # one — such rows were removed by migration v47 instead
    user, headers = actor
    legacy = "ExponentPushToken[abcde\x00fghij]"
    _seed_token(db, user["id"], legacy)

    response = _unregister(client, headers, token=legacy)

    assert response.status_code == 404
    assert len(_rows(db, user["id"])) == 1


def test_a_legacy_row_past_the_post_length_cap_is_still_removable_by_its_owner(client, actor, db):
    # The POST's 200-character guard does not apply here: the
    # grammar caps a real token at 83, so only a pre-v47 row can
    # be this long — and its owner is the one person entitled to
    # remove it
    user, headers = actor
    long_token = "ExponentPushToken[" + "a" * 250 + "]"
    _seed_token(db, user["id"], long_token)

    response = _unregister(client, headers, token=long_token)

    assert response.status_code == 200
    assert response.get_json() == {"unregistered": True}
    assert _rows(db, user["id"]) == []


def test_a_token_longer_than_any_row_reaches_the_delete_and_is_simply_not_found(client, actor):
    # No length gate and no grammar gate on this path, so an
    # over-long string is judged by the table alone
    _, headers = actor

    at_cap = _unregister(client, headers, token="A" * 200)
    past_cap = _unregister(client, headers, token="A" * 201)

    assert (at_cap.status_code, at_cap.get_json()["error"]) == (404, "Token not found")
    assert (past_cap.status_code, past_cap.get_json()["error"]) == (404, "Token not found")


def test_a_device_push_deactivated_can_still_be_removed(client, actor, db):
    user, headers = actor
    _seed_token(db, user["id"], TOKEN, active=0)

    response = _unregister(client, headers)

    assert response.status_code == 200
    assert response.get_json() == {"unregistered": True}
    assert _rows(db, user["id"]) == []


def test_the_delete_ignores_every_field_but_the_token(client, db, make_user, auth_headers):
    mine = make_user()
    other = make_user()
    _seed_token(db, other["id"], _tok("theirs"))
    _seed_token(db, mine["id"], TOKEN)

    response = client.delete(REGISTER, headers=auth_headers(mine), json={
        "token": TOKEN, "user_id": other["id"], "all": True})

    assert response.status_code == 200
    assert _tokens_of(db, other["id"]) == {_tok("theirs")}


def test_removing_a_device_leaves_the_switches_alone(client, actor, db):
    # Two independent records: turning the phone off must not
    # silently re-enable the topics the user muted
    user, headers = actor
    _register(client, headers)
    _put_channels(client, headers, {"news": False, "chat": False})

    assert _unregister(client, headers).status_code == 200

    assert _get_channels(client, headers).get_json()["channels"] == {
        "news": False, "chat": False, "schedule": True, "admin": True}


@pytest.mark.parametrize("body,expected", [
    (None, "Push token required"),
    ({}, "Push token required"),
    ({"token": ""}, "Push token required"),
    ({"token": 0}, "Push token required"),
    ({"token": False}, "Push token required"),
    ({"token": None}, "Push token required"),
    ({"token": 12}, "Token must be a string"),
    ({"token": True}, "Token must be a string"),
    ({"token": ["x"]}, "Token must be a string"),
    ({"token": {"a": 1}}, "Token must be a string"),
    ({"token": 1.5}, "Token must be a string"),
])
def test_the_delete_guard_clauses_answer_exactly_like_the_post(client, actor, body, expected):
    _, headers = actor
    kwargs = {"json": body} if body is not None else {}

    response = client.delete(REGISTER, headers=headers, **kwargs)

    assert response.status_code == 400
    assert response.get_json()["error"] == expected


def test_a_delete_with_a_top_level_array_body_never_reaches_the_route(client, actor):
    _, headers = actor

    response = client.delete(REGISTER, headers=headers, json=[{"token": TOKEN}])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_the_delete_response_carries_exactly_one_key(client, actor):
    _, headers = actor
    _register(client, headers)

    body = _unregister(client, headers).get_json()

    assert body == {"unregistered": True}




# ===========================================================
# GET /channels — the opt-out read
# ===========================================================


def test_the_switch_map_always_carries_exactly_the_four_names(client, actor):
    _, headers = actor

    body = _get_channels(client, headers).get_json()

    assert set(body) == {"channels"}
    assert set(body["channels"]) == set(VALID_CHANNELS)


def test_every_switch_is_a_json_boolean_not_a_number(client, actor, db):
    user, headers = actor
    _seed_channel(db, user["id"], "news", 0)
    _seed_channel(db, user["id"], "chat", 1)

    channels = _get_channels(client, headers).get_json()["channels"]

    assert channels["news"] is False
    assert channels["chat"] is True


@pytest.mark.parametrize("stored,expected", [
    (1, True), (0, False), (2, True), (-1, True), (99, True),
    ("1", True), ("0", False), ("", False),
])
def test_a_legacy_enabled_value_is_read_through_bool(client, actor, db, stored, expected):
    # Only an explicit falsy value silences a topic; everything
    # else the column ever held still reads as on
    user, headers = actor
    _seed_channel(db, user["id"], "schedule", stored)

    channels = _get_channels(client, headers).get_json()["channels"]

    assert channels["schedule"] is expected


def test_a_fifth_channel_name_cannot_exist_to_be_read(client, actor, db):
    # The CHECK on notification_channels.channel is what keeps
    # the read's key set equal to VALID_CHANNELS
    user, headers = actor

    with pytest.raises(sqlite3.IntegrityError):
        _seed_channel(db, user["id"], "email", 0)

    assert set(_get_channels(client, headers).get_json()["channels"]) == set(VALID_CHANNELS)


def test_the_read_never_writes_a_row(client, actor, db):
    _, headers = actor

    for _ in range(3):
        assert _get_channels(client, headers).status_code == 200

    assert _count(db, "notification_channels") == 0


def test_query_parameters_are_ignored_by_the_read(client, actor, db):
    user, headers = actor
    _seed_channel(db, user["id"], "admin", 0)

    body = client.get(CHANNELS + "?channel=news&enabled=1", headers=headers).get_json()

    assert body["channels"] == {"news": True, "chat": True, "schedule": True, "admin": False}


def test_another_users_switches_never_leak_into_the_read(client, db, make_user, auth_headers):
    mine = make_user()
    other = make_user()
    for channel in VALID_CHANNELS:
        _seed_channel(db, other["id"], channel, 0)

    channels = _get_channels(client, auth_headers(mine)).get_json()["channels"]

    assert channels == {"news": True, "chat": True, "schedule": True, "admin": True}




# ===========================================================
# PUT /channels — validation precedence and the wire
# ===========================================================


@pytest.mark.parametrize("body", [
    None, {}, {"switches": {"news": False}},
    {"channels": None}, {"channels": []}, {"channels": "news"}, {"channels": 5},
    {"channels": True}, {"channels": 0}, {"channels": ""},
])
def test_the_channels_field_must_be_a_dict_before_anything_is_written(client, actor, db, body):
    # One guard for a missing body, a missing field and a field
    # of the wrong type alike — the app sends a map or nothing
    _, headers = actor
    kwargs = {"json": body} if body is not None else {}

    response = client.put(CHANNELS, headers=headers, **kwargs)

    assert response.status_code == 400
    assert response.get_json()["error"] == "channels dict required"
    assert _count(db, "notification_channels") == 0


def test_an_unknown_name_is_refused_before_its_value_is_judged(client, actor, db):
    _, headers = actor

    response = _put_channels(client, headers, {"bogus": 1})

    assert response.status_code == 400
    assert _msg(response) == "Unknown channel 'bogus'"
    assert _count(db, "notification_channels") == 0


def test_the_first_offending_entry_in_the_dict_decides_the_error(client, actor, db):
    # Wire order is what the loop walks, so this goes out raw:
    # the app's own provider SORTS keys, which would silently
    # rearrange a `json=` body and pin the wrong entry
    _, headers = actor

    response = _raw(client, "put", CHANNELS, headers,
                    '{"channels": {"news": true, "bogus": true, "chat": 1}}')

    assert response.status_code == 400
    assert _msg(response) == "Unknown channel 'bogus'"
    assert _count(db, "notification_channels") == 0


def test_a_bad_value_before_an_unknown_name_decides_instead(client, actor):
    _, headers = actor

    response = _raw(client, "put", CHANNELS, headers,
                    '{"channels": {"news": 1, "bogus": true}}')

    assert response.status_code == 400
    assert _msg(response) == "Channel 'news' value must be a boolean (true/false), got int"


def test_a_channel_name_carrying_markup_is_named_back_escaped_on_the_wire(client, actor):
    # Rule 10 in both directions: the name goes out raw and the
    # provider escapes the message it comes back in
    _, headers = actor
    name = "<script>news</script>"

    response = _raw(client, "put", CHANNELS, headers,
                    json.dumps({"channels": {name: False}}))

    assert response.status_code == 400
    assert "&lt;script&gt;" in response.get_data(as_text=True)
    assert _msg(response) == f"Unknown channel '{name}'"


@pytest.mark.parametrize("value,type_name", [
    (-1, "int"), (0.5, "float"), ("false", "str"), ("news", "str"),
])
def test_a_non_boolean_value_names_its_python_type(client, actor, value, type_name):
    _, headers = actor

    response = _put_channels(client, headers, {"admin": value})

    assert response.status_code == 400
    assert _msg(response) == (
        f"Channel 'admin' value must be a boolean (true/false), got {type_name}")


def test_a_nested_dict_value_is_refused_as_a_dict(client, actor):
    _, headers = actor

    response = _put_channels(client, headers, {"news": {"enabled": True}})

    assert response.status_code == 400
    assert _msg(response) == "Channel 'news' value must be a boolean (true/false), got dict"


def test_a_thousand_unknown_names_are_refused_by_the_first_one(client, actor, db):
    # Validation walks the whole dict before any write, so a
    # flood of names costs one refusal and no rows
    _, headers = actor
    channels = {f"bogus{i}": False for i in range(1000)}

    response = _raw(client, "put", CHANNELS, headers, json.dumps({"channels": channels}))

    assert response.status_code == 400
    assert _msg(response) == "Unknown channel 'bogus0'"
    assert _count(db, "notification_channels") == 0


def test_a_duplicate_key_in_the_raw_body_takes_the_last_value(client, actor, db):
    # Two toggles of the same switch inside one debounce window
    # would serialise like this; JSON says the last one wins
    user, headers = actor

    response = _raw(client, "put", CHANNELS, headers,
                    '{"channels": {"news": true, "news": false}}')

    assert response.status_code == 200
    assert response.get_json()["channels"]["news"] is False
    assert db.execute("SELECT enabled FROM notification_channels WHERE user_id = ?",
                      (user["id"],)).fetchone()["enabled"] == 0


def test_a_duplicate_channels_field_takes_the_last_object(client, actor):
    _, headers = actor

    response = _raw(client, "put", CHANNELS, headers,
                    '{"channels": {"news": false}, "channels": {"chat": false}}')

    assert response.status_code == 200
    assert response.get_json()["channels"] == {
        "news": True, "chat": False, "schedule": True, "admin": True}


def test_extra_top_level_fields_are_ignored_by_the_update(client, db, make_user, auth_headers):
    mine = make_user()
    other = make_user()
    _seed_channel(db, other["id"], "news", 1)

    response = client.put(CHANNELS, headers=auth_headers(mine), json={
        "channels": {"news": False}, "user_id": other["id"], "all_users": True})

    assert response.status_code == 200
    assert db.execute("SELECT enabled FROM notification_channels WHERE user_id = ?",
                      (other["id"],)).fetchone()["enabled"] == 1


def test_a_refused_batch_leaves_the_state_that_was_already_stored(client, actor, db):
    user, headers = actor
    _seed_channel(db, user["id"], "news", 0)

    response = _put_channels(client, headers, {"news": True, "bogus": True})

    assert response.status_code == 400
    assert _get_channels(client, headers).get_json()["channels"]["news"] is False




# ===========================================================
# PUT /channels — what the write leaves behind
# ===========================================================


def test_the_whole_batch_shares_one_timestamp(client, actor, db):
    # One `now` for the loop and one commit around it: the four
    # rows are written as a single state, not four
    user, headers = actor

    _put_channels(client, headers, {ch: False for ch in VALID_CHANNELS})

    stamps = {r["updated_at"] for r in db.execute(
        "SELECT updated_at FROM notification_channels WHERE user_id = ?", (user["id"],))}
    assert len(stamps) == 1
    stamp = stamps.pop()
    assert "T" in stamp and " " not in stamp and "+" not in stamp


def test_switching_everything_off_and_on_again_keeps_four_rows(client, actor, db):
    # ON CONFLICT(user_id, channel) rides the composite primary
    # key — a switch is one row for its whole life
    user, headers = actor

    _put_channels(client, headers, {ch: False for ch in VALID_CHANNELS})
    body = _put_channels(client, headers, {ch: True for ch in VALID_CHANNELS}).get_json()

    assert body["channels"] == {ch: True for ch in VALID_CHANNELS}
    assert _count(db, "notification_channels") == 4


def test_writing_the_same_value_twice_only_moves_the_stamp(client, actor, db):
    user, headers = actor
    _put_channels(client, headers, {"chat": False})
    first = db.execute("SELECT updated_at FROM notification_channels"
                       " WHERE user_id = ? AND channel = 'chat'", (user["id"],)).fetchone()[0]

    with time_machine.travel(datetime.now(timezone.utc).timestamp() + 60, tick=False):
        _put_channels(client, headers, {"chat": False})

    row = db.execute("SELECT enabled, updated_at FROM notification_channels"
                     " WHERE user_id = ? AND channel = 'chat'", (user["id"],)).fetchone()
    assert row["enabled"] == 0
    assert row["updated_at"] > first
    assert _count(db, "notification_channels") == 1


def test_an_empty_batch_answers_the_stored_state_untouched(client, actor, db):
    user, headers = actor
    _put_channels(client, headers, {"news": False})

    response = _put_channels(client, headers, {})

    assert response.status_code == 200
    assert response.get_json()["channels"]["news"] is False
    assert _count(db, "notification_channels") == 1


def test_a_partial_batch_writes_only_the_names_it_carries(client, actor, db):
    user, headers = actor

    _put_channels(client, headers, {"schedule": False})

    rows = db.execute("SELECT channel FROM notification_channels WHERE user_id = ?",
                      (user["id"],)).fetchall()
    assert [r["channel"] for r in rows] == ["schedule"]


@pytest.mark.contract
def test_the_update_answers_in_exactly_the_read_shape(client, actor):
    # services/api/notifications.ts — both calls are typed
    # NotificationChannelsResponse: { channels: Record<name, boolean> }
    _, headers = actor

    written = _put_channels(client, headers, {"news": False, "admin": True}).get_json()
    read = _get_channels(client, headers).get_json()

    assert written == read
    assert set(written) == {"channels"}
    assert set(written["channels"]) == set(VALID_CHANNELS)
    assert all(isinstance(v, bool) for v in written["channels"].values())




# ===========================================================
# The two write budgets
# ===========================================================


def test_a_refused_body_still_spends_a_registration(client, actor, db):
    # The limiter sits under require_auth and above the handler,
    # so a malformed body costs exactly what a good one costs
    _, headers = actor
    for _ in range(20):
        assert _register(client, headers, token="not-a-token").status_code == 400

    response = _register(client, headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert _count(db, "push_tokens") == 0


def test_twenty_registrations_fit_in_one_window(client, actor, db):
    _, headers = actor

    accepted = [_register(client, headers, token=_tok(f"dev{i}")).status_code
                for i in range(20)]

    assert all(code in (200, 201) for code in accepted)
    assert _register(client, headers, token=_tok("late")).status_code == 429
    assert len(_rows(db, _row(db, _tok("dev19"))["user_id"])) == 10


def test_a_guest_spends_none_of_the_callers_budget(client, actor):
    # The key is the user id, and an anonymous caller never gets
    # one: 401 comes from the decorator above the limiter
    user, headers = actor
    for _ in range(30):
        assert client.post(REGISTER, json={"token": TOKEN}).status_code == 401

    assert _register(client, headers).status_code == 201


def test_a_refused_registration_writes_nothing(client, actor, db, monkeypatch):
    user, headers = actor
    _fill_bucket(monkeypatch, "push_register", user["id"], 20)

    response = _register(client, headers)

    assert response.status_code == 429
    assert _count(db, "push_tokens") == 0


@pytest.mark.parametrize("method", ["post", "delete"])
def test_the_two_register_verbs_share_one_budget_and_one_shape(client, actor, monkeypatch, method):
    user, headers = actor
    _fill_bucket(monkeypatch, "push_register", user["id"], 20)

    response = getattr(client, method)(REGISTER, headers=headers, json={"token": TOKEN})

    assert response.status_code == 429
    assert response.get_json() == {"error": "Too many requests. Please wait a few minutes.",
                                   "code": "rate_limited"}
    assert 1 <= int(response.headers["Retry-After"]) <= 301


def test_a_spent_registration_budget_leaves_the_switches_alone(client, actor, monkeypatch):
    user, headers = actor
    _fill_bucket(monkeypatch, "push_register", user["id"], 20)

    assert _register(client, headers).status_code == 429
    assert _put_channels(client, headers, {"news": False}).status_code == 200
    assert _get_channels(client, headers).status_code == 200


def test_the_read_stays_open_when_the_write_budget_is_gone(client, actor, monkeypatch):
    # GET carries no limiter at all — settings must still render
    user, headers = actor
    _fill_bucket(monkeypatch, "push_channels", user["id"], 60)

    assert _put_channels(client, headers, {"news": False}).status_code == 429
    assert all(_get_channels(client, headers).status_code == 200 for _ in range(10))


def test_the_registration_budget_recovers_after_the_window(client, actor, monkeypatch):
    user, headers = actor
    _fill_bucket(monkeypatch, "push_register", user["id"], 20)
    assert _register(client, headers).status_code == 429

    with time_machine.travel(datetime.now(timezone.utc).timestamp() + 601, tick=False):
        assert _register(client, headers).status_code == 201


def test_one_users_flood_never_reaches_another(client, make_user, auth_headers, monkeypatch):
    flooder = make_user()
    quiet = make_user()
    _fill_bucket(monkeypatch, "push_register", flooder["id"], 20)

    assert _register(client, auth_headers(flooder)).status_code == 429
    assert _register(client, auth_headers(quiet), token=_tok("quiet")).status_code == 201
