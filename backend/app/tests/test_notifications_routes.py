# -----------------------------------------------------------
#  [*] Tests — notifications (app/notifications/routes.py)
#
#  The four client-facing push routes, proved through the real
#  blueprint on a real database:
#
#    - POST /register takes the WHOLE Expo grammar, not a
#      prefix: a control character, a quote, a tag, an id
#      shorter than 10 or longer than 64, anything trailing
#      the closing bracket — all 400, and the 200-character
#      cap is measured AFTER stripping, so a padded but legal
#      token still registers.
#    - the row is an upsert, not a delete-and-insert: the
#      caller's own token answers 200 with the SAME row id,
#      revives what push.py deactivated and refreshes
#      platform, language and updated_at every time (a live
#      device's row used to age as if it were dead), while
#      created_at never moves.
#    - a token that changed hands answers 201, keeps the row
#      id, moves user_id, and is LOGGED with both user ids and
#      the token's digest — never the token itself; the
#      previous owner then gets 404 on unregister and cannot
#      touch the row.
#    - the fleet is capped at MAX_TOKENS_PER_USER: the row
#      that went longest without re-registering is the one
#      that goes, only the caller's own rows count, and
#      re-registering at the cap evicts nothing.
#    - language is the app language or 'lt' — the value
#      push.py splits its batches on, proved end to end: an
#      'en' device gets the English copy.
#    - DELETE is owner-scoped real removal (not active=0),
#      deliberately WITHOUT the grammar check and WITHOUT the
#      length cap so a legacy row of any shape can still be
#      dropped by its owner, and it authenticates off the
#      forwarded bearer header the detached logout hands it —
#      which is a 401 once that session is gone.
#    - channels are OPT-OUT: all four read True with no rows
#      at all, only an explicit enabled=0 silences one, a
#      typo'd name is a 400 (it used to answer 200 and change
#      nothing), a non-boolean value is a 400 naming the
#      Python type, and a bad entry anywhere leaves NO
#      half-applied batch. A switch turned off through the
#      route really does drop the device from push.py's
#      fan-out.
#    - every route is 401 for a guest, both writes share the
#      "push_register" budget, and the channel read is not
#      rate limited at all.
# -----------------------------------------------------------

import html
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone

import pytest
import responses
import time_machine

from app.auth import routes as auth_routes
from app.notifications import push as push_module


REGISTER = "/api/notifications/register"
CHANNELS = "/api/notifications/channels"

# Base for every generated token: 18 characters of prefix, an
# id of [A-Za-z0-9_-]{10,64}, one closing bracket
GOOD_TOKEN = "ExponentPushToken[xxxxxxxxxx]"




# -----------------------------------------------------------
# _clean_rate_limit_store
# -----------------------------------------------------------
#
# The limiter dict is module-level and outlives a test — the
# app fixture rebuilds the database, never this. Cleared on
# both sides so no sibling module's writes spend this module's
# budget and no seeded bucket here leaks out.
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
# _token
# -----------------------------------------------------------
#
# A distinct but legal Expo token per call — the id is padded
# to the 10-character minimum so a short seed still matches
# the grammar.
# -----------------------------------------------------------

def _token(seed="device"):
    body = "".join(c for c in seed if c.isalnum() or c in "_-")
    return f"ExponentPushToken[{body.ljust(10, 'x')[:64]}]"




# -----------------------------------------------------------
# _register / _unregister / _read_channels / _write_channels
# -----------------------------------------------------------
#
# The four calls, returning the raw response so an unhappy
# path can assert its status and body. `_register` sends the
# payload the mobile registerPushToken builds (token, platform,
# language) and lets a test override or drop any of it.
# -----------------------------------------------------------

def _register(client, headers, token=GOOD_TOKEN, **extra):
    payload = {"token": token}
    payload.update(extra)
    return client.post(REGISTER, headers=headers, json=payload)


def _unregister(client, headers, token=GOOD_TOKEN):
    return client.delete(REGISTER, headers=headers, json={"token": token})


def _read_channels(client, headers):
    return client.get(CHANNELS, headers=headers)


def _write_channels(client, headers, channels):
    return client.put(CHANNELS, headers=headers, json={"channels": channels})




# -----------------------------------------------------------
# _rows / _row
# -----------------------------------------------------------
#
# What the route actually wrote — the register tests are about
# persistence (active, language, updated_at), so they assert
# against the table, not only the body.
# -----------------------------------------------------------

def _rows(db, user_id):
    return db.execute(
        "SELECT * FROM push_tokens WHERE user_id = ? ORDER BY updated_at",
        (user_id,),
    ).fetchall()


def _row(db, token):
    return db.execute("SELECT * FROM push_tokens WHERE token = ?", (token,)).fetchone()




# -----------------------------------------------------------
# _seed_token
# -----------------------------------------------------------
#
# A push_tokens row the route would never write: one that
# push.py has already deactivated, one whose updated_at is
# years old (the cap orders on it), or a legacy token that
# predates _TOKEN_RE and could not be registered today.
# -----------------------------------------------------------

def _seed_token(db, user_id, token, platform="ios", language="lt", active=1,
                updated_at="2020-01-01T00:00:00", created_at="2020-01-01T00:00:00"):
    row_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO push_tokens (id, user_id, token, platform, language, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (row_id, user_id, token, platform, language, active, created_at, updated_at),
    )
    db.commit()
    return row_id




# -----------------------------------------------------------
# _seed_channel
# -----------------------------------------------------------
#
# An explicit notification_channels row — the only thing that
# can turn a channel off, since a missing row means enabled.
# `enabled` is deliberately free-form so a legacy non-binary
# value can be planted.
# -----------------------------------------------------------

def _seed_channel(db, user_id, channel, enabled):
    db.execute(
        "INSERT INTO notification_channels (user_id, channel, enabled, updated_at)"
        " VALUES (?, ?, ?, '2020-01-01T00:00:00')",
        (user_id, channel, enabled),
    )
    db.commit()




# -----------------------------------------------------------
# _spend_budget
# -----------------------------------------------------------
#
# Fills a rate-limit bucket to the brim without making the
# calls — `scope` is the decorator's scope, the key its
# per-user form.
# -----------------------------------------------------------

def _spend_budget(monkeypatch, scope, user_id, attempts):
    monkeypatch.setitem(auth_routes._rate_limit_store,
                        f"{scope}:{user_id}", [time.monotonic()] * attempts)




# -----------------------------------------------------------
# _error
# -----------------------------------------------------------
#
# The error prose as it was WRITTEN, not as it goes out: the
# app's JSON provider html-escapes every string it serialises,
# so a message naming a channel arrives as "&#x27;news&#x27;"
# and the mobile client decodes it again. Entity spellings do
# not belong in an assertion about the route's wording.
# -----------------------------------------------------------

def _error(response):
    return html.unescape(response.get_json()["error"])




# -----------------------------------------------------------
# _RacingConnection
# -----------------------------------------------------------
#
# A real connection whose push_tokens INSERT always loses:
# it optionally commits the row a racing writer would have
# written (through a SEPARATE connection, exactly as another
# request would) and then raises the IntegrityError SQLite
# would raise on the UNIQUE index. Stands in for two app
# starts landing at once, which is the only way to reach the
# route's belt-and-braces rollback.
#
# Used by:
#   - the racing-registration and vanished-row tests
# -----------------------------------------------------------

class _RacingConnection:

    def __init__(self, real, db_path, winner=None):
        self._real = real
        self._db_path = db_path
        self._winner = winner
        self.winner_id = None

    def execute(self, sql, params=()):
        if "INSERT INTO push_tokens" in sql:
            if self._winner:
                self.winner_id = str(uuid.uuid4())
                other = sqlite3.connect(self._db_path)
                try:
                    other.execute(
                        """INSERT INTO push_tokens (id, user_id, token, platform, language, active,
                                                    created_at, updated_at)
                           VALUES (?, ?, ?, 'android', 'lt', 1, ?, ?)""",
                        (self.winner_id, self._winner[0], self._winner[1],
                         "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
                    )
                    other.commit()
                finally:
                    other.close()
            raise sqlite3.IntegrityError("UNIQUE constraint failed: push_tokens.token")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)




# ===========================================================
# POST /notifications/register — the guard clauses
# ===========================================================


def test_registering_a_token_requires_authentication(client):
    response = client.post(REGISTER, json={"token": GOOD_TOKEN})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_registering_with_a_dead_bearer_token_is_refused(client):
    response = client.post(REGISTER, headers={"Authorization": "Bearer nera-tokeno"},
                           json={"token": GOOD_TOKEN})

    assert response.status_code == 401


def test_a_deactivated_account_can_no_longer_register_a_device(client, actor, db):
    # An admin flipping users.active must cut push off at once,
    # even on a session minted before the flip
    user, headers = actor
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert _register(client, headers).status_code == 401
    assert _unregister(client, headers).status_code == 401
    assert _read_channels(client, headers).status_code == 401
    assert _write_channels(client, headers, {"news": False}).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("get", REGISTER), ("put", REGISTER), ("patch", REGISTER),
    ("post", CHANNELS), ("delete", CHANNELS),
])
def test_the_blueprint_exposes_no_other_method(client, actor, method, path):
    _, headers = actor

    response = getattr(client, method)(path, headers=headers, json={"token": GOOD_TOKEN})

    assert response.status_code == 405


def test_registering_with_no_body_at_all_is_refused(client, actor):
    _, headers = actor

    response = client.post(REGISTER, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"


def test_registering_with_a_malformed_json_body_is_refused(client, actor):
    _, headers = actor

    response = client.post(REGISTER, headers=headers, data="{not json",
                           content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"


def test_registering_with_a_top_level_array_body_is_refused(client, actor):
    # The app-wide validate_json_input hook folds a non-object
    # body first, so the route's own get_json_object guard never
    # even sees it — either way nothing reaches push_tokens
    _, headers = actor

    response = client.post(REGISTER, headers=headers, json=[{"token": GOOD_TOKEN}])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_registering_without_a_token_field_is_refused(client, actor):
    _, headers = actor

    response = client.post(REGISTER, headers=headers, json={"platform": "ios"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"


@pytest.mark.parametrize("empty", ["", None, False, 0])
def test_an_empty_token_is_refused_as_missing(client, actor, empty):
    _, headers = actor

    response = _register(client, headers, token=empty)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"


@pytest.mark.parametrize("wrong", [12345, True, ["ExponentPushToken[abcdefghij]"],
                                   {"token": "x"}, 3.5])
def test_a_non_string_token_is_refused_by_type(client, actor, wrong):
    _, headers = actor

    response = _register(client, headers, token=wrong)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Token must be a string"


def test_a_token_past_the_two_hundred_character_cap_is_refused(client, actor):
    _, headers = actor

    response = _register(client, headers, token="A" * 201)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Token too long"


def test_a_token_of_exactly_two_hundred_characters_fails_on_format_not_length(client, actor):
    # The boundary: 200 passes the length gate and is then
    # judged by the grammar, so the error must be the format one
    _, headers = actor

    response = _register(client, headers, token="A" * 200)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid Expo push token format"


def test_a_legal_token_padded_past_the_cap_still_registers(client, actor, db):
    # The length is measured AFTER strip(), so 300 spaces of
    # padding is whitespace, not a 300-character token
    user, headers = actor

    response = _register(client, headers, token=" " * 300 + GOOD_TOKEN + " " * 300)

    assert response.status_code == 201
    assert _row(db, GOOD_TOKEN) is not None


@pytest.mark.parametrize("bad", [
    "ExponentPushToken[",                          # prefix only
    "ExponentPushToken[]",                         # empty id
    "ExponentPushToken[abc]",                      # id under the 10-character floor
    "ExponentPushToken[" + "a" * 9 + "]",          # one short of the floor
    "ExponentPushToken[" + "a" * 65 + "]",         # one past the 64-character ceiling
    "ExpoPushToken[abcdefghij]",                   # the other Expo prefix
    "exponentpushtoken[abcdefghij]",               # case matters
    "ExponentPushToken[abcdefghij] ; DROP TABLE push_tokens",
    "ExponentPushToken[abcdefghij]<script>alert(1)</script>",
    "prefix ExponentPushToken[abcdefghij]",        # anything leading
    "ExponentPushToken[abcdef ghij]",              # a space inside the id
    "ExponentPushToken[abcde\tfghij]",             # a control character
    "ExponentPushToken[abcde'fghij]",              # a quote
    "ExponentPushToken[abcde\nfghij]",             # a newline
    "   ",                                         # whitespace only
    "fcm-token-not-expo-at-all",
])
def test_only_the_whole_expo_grammar_is_accepted(client, actor, db, bad):
    user, headers = actor

    response = _register(client, headers, token=bad)

    assert response.status_code == 400, bad
    assert response.get_json()["error"] == "Invalid Expo push token format"
    assert _rows(db, user["id"]) == []


def test_a_nul_byte_never_reaches_the_token_column(client, actor, db):
    # The app-wide validate_json_input hook strips NUL from every
    # string BEFORE the grammar check, so a smuggled \x00 either
    # leaves a legal token or leaves none — never a stored one
    # with a control character in it
    user, headers = actor

    _register(client, headers, token="ExponentPushToken[abcde\x00fghij]")

    stored = _rows(db, user["id"])
    assert all("\x00" not in row["token"] for row in stored)


@pytest.mark.parametrize("length", [10, 11, 63, 64])
def test_the_legal_token_id_lengths_are_accepted(client, actor, length):
    _, headers = actor

    response = _register(client, headers, token=f"ExponentPushToken[{'a' * length}]")

    assert response.status_code == 201


def test_the_whole_expo_alphabet_is_accepted(client, actor, db):
    # Underscores and hyphens are part of the grammar — an
    # opaque id that carries them must not be turned away
    user, headers = actor
    token = "ExponentPushToken[xxxxx-YYYYY_00000]"

    assert _register(client, headers, token=token).status_code == 201
    assert _row(db, token)["user_id"] == user["id"]


def test_surrounding_whitespace_is_stripped_before_the_token_is_stored(client, actor, db):
    user, headers = actor

    assert _register(client, headers, token=f"\n\t {GOOD_TOKEN} \n").status_code == 201

    stored = _rows(db, user["id"])
    assert len(stored) == 1
    assert stored[0]["token"] == GOOD_TOKEN




# ===========================================================
# POST /notifications/register — platform and language
# ===========================================================


@pytest.mark.parametrize("platform", ["ios", "android", "web", "unknown"])
def test_a_whitelisted_platform_is_stored_as_sent(client, actor, db, platform):
    _, headers = actor

    _register(client, headers, platform=platform)

    assert _row(db, GOOD_TOKEN)["platform"] == platform


@pytest.mark.parametrize("platform", ["windows", "IOS", "", None, 7, ["ios"]])
def test_an_unexpected_platform_is_quietly_stored_as_unknown(client, actor, db, platform):
    _, headers = actor

    assert _register(client, headers, platform=platform).status_code == 201
    assert _row(db, GOOD_TOKEN)["platform"] == "unknown"


def test_a_missing_platform_is_stored_as_unknown(client, actor, db):
    _, headers = actor

    client.post(REGISTER, headers=headers, json={"token": GOOD_TOKEN})

    assert _row(db, GOOD_TOKEN)["platform"] == "unknown"


def test_the_app_language_is_stored_on_the_row(client, actor, db):
    _, headers = actor

    assert _register(client, headers, language="en").status_code == 201
    assert _row(db, GOOD_TOKEN)["language"] == "en"


@pytest.mark.parametrize("language", ["de", "EN", "lt-LT", "", None, 5, ["en"]])
def test_an_unexpected_language_falls_back_to_lithuanian(client, actor, db, language):
    _, headers = actor

    assert _register(client, headers, language=language).status_code == 201
    assert _row(db, GOOD_TOKEN)["language"] == "lt"


def test_a_missing_language_falls_back_to_lithuanian(client, actor, db):
    _, headers = actor

    client.post(REGISTER, headers=headers, json={"token": GOOD_TOKEN})

    assert _row(db, GOOD_TOKEN)["language"] == "lt"


def test_re_registering_after_a_language_switch_updates_the_stored_row(client, actor, db):
    # The app re-registers on every language switch; the row
    # has to follow, or push copy keeps arriving in the old one
    _, headers = actor
    _register(client, headers, platform="ios", language="lt")

    response = _register(client, headers, platform="android", language="en")

    assert response.status_code == 200
    stored = _row(db, GOOD_TOKEN)
    assert stored["language"] == "en"
    assert stored["platform"] == "android"




# ===========================================================
# POST /notifications/register — the upsert
# ===========================================================


def test_a_new_token_is_registered_with_201(client, actor, db):
    user, headers = actor

    response = _register(client, headers, platform="ios", language="lt")

    assert response.status_code == 201
    body = response.get_json()
    assert body["registered"] is True

    stored = _rows(db, user["id"])
    assert len(stored) == 1
    assert stored[0]["id"] == body["tokenId"]
    assert stored[0]["token"] == GOOD_TOKEN
    assert stored[0]["active"] == 1


@pytest.mark.contract
def test_the_register_body_is_the_shape_the_mobile_app_consumes(client, actor):
    # services/api/notifications.ts — { registered: boolean, tokenId: string }
    _, headers = actor

    body = _register(client, headers).get_json()

    assert set(body) == {"registered", "tokenId"}
    assert body["registered"] is True
    assert isinstance(body["tokenId"], str) and body["tokenId"]


def test_re_registering_the_same_token_answers_200_with_the_same_row_id(client, actor, db):
    user, headers = actor
    first = _register(client, headers)

    second = _register(client, headers)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json()["tokenId"] == first.get_json()["tokenId"]
    assert len(_rows(db, user["id"])) == 1


def test_re_registering_revives_a_token_push_deactivated(client, actor, db):
    # push.py flips active to 0 on DeviceNotRegistered; the next
    # app start is what brings a reinstalled device back
    user, headers = actor
    seeded = _seed_token(db, user["id"], GOOD_TOKEN, active=0)

    response = _register(client, headers)

    assert response.status_code == 200
    assert response.get_json()["tokenId"] == seeded
    assert _row(db, GOOD_TOKEN)["active"] == 1


def test_re_registering_refreshes_updated_at_and_keeps_created_at(client, actor, db):
    # The old code skipped the UPDATE when nothing had changed,
    # so a live device's row aged exactly like a dead one
    user, headers = actor
    _seed_token(db, user["id"], GOOD_TOKEN,
                created_at="2020-01-01T00:00:00", updated_at="2020-01-01T00:00:00")

    assert _register(client, headers).status_code == 200

    stored = _row(db, GOOD_TOKEN)
    assert stored["created_at"] == "2020-01-01T00:00:00"
    assert stored["updated_at"] > "2020-01-01T00:00:00"


def test_the_stored_timestamps_are_naive_utc_in_t_form(client, actor, db):
    # Space-form text sorts wrong against T-form under SQLite's
    # string comparison, and the cap orders on updated_at
    _, headers = actor

    _register(client, headers)

    stored = _row(db, GOOD_TOKEN)
    for value in (stored["created_at"], stored["updated_at"]):
        assert "T" in value
        assert " " not in value
        assert "+" not in value and not value.endswith("Z")


def test_two_devices_of_one_user_both_keep_their_row(client, actor, db):
    user, headers = actor

    _register(client, headers, token=_token("phone"), platform="ios")
    _register(client, headers, token=_token("tablet"), platform="android")

    assert {r["platform"] for r in _rows(db, user["id"])} == {"ios", "android"}


def test_a_token_that_changed_hands_is_reassigned_with_201(client, db, make_user,
                                                           auth_headers, caplog):
    caplog.set_level(logging.WARNING, logger="app.notifications.routes")
    first = make_user()
    second = make_user()
    original = _register(client, auth_headers(first)).get_json()["tokenId"]

    response = _register(client, auth_headers(second))

    assert response.status_code == 201
    # DO UPDATE keeps the original row id — tokenId comes out of
    # the table, never out of the INSERT
    assert response.get_json()["tokenId"] == original
    assert _row(db, GOOD_TOKEN)["user_id"] == second["id"]
    assert _rows(db, first["id"]) == []


def test_a_takeover_is_logged_with_both_users_and_the_token_digest(client, make_user,
                                                                   auth_headers, caplog):
    caplog.set_level(logging.WARNING, logger="app.notifications.routes")
    first = make_user()
    second = make_user()
    _register(client, auth_headers(first))
    caplog.clear()

    _register(client, auth_headers(second))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "reassigned" in logged
    assert first["id"] in logged and second["id"] in logged
    assert push_module.token_digest(GOOD_TOKEN) in logged
    assert GOOD_TOKEN not in logged


def test_the_previous_owner_cannot_remove_a_token_that_changed_hands(client, db, make_user,
                                                                     auth_headers):
    first = make_user()
    second = make_user()
    first_headers = auth_headers(first)
    _register(client, first_headers)
    _register(client, auth_headers(second))

    response = _unregister(client, first_headers)

    assert response.status_code == 404
    assert _row(db, GOOD_TOKEN)["user_id"] == second["id"]


def test_one_users_registration_never_disturbs_anothers(client, db, make_user, auth_headers):
    first = make_user()
    second = make_user()
    _register(client, auth_headers(first), token=_token("first"))

    _register(client, auth_headers(second), token=_token("second"))

    assert len(_rows(db, first["id"])) == 1
    assert len(_rows(db, second["id"])) == 1




# ===========================================================
# POST /notifications/register — the per-user cap
# ===========================================================


def test_the_eleventh_token_evicts_the_least_recently_registered(client, actor, db, caplog):
    caplog.set_level(logging.INFO, logger="app.notifications.routes")
    user, headers = actor
    for day in range(1, 11):
        _seed_token(db, user["id"], _token(f"old{day}"), updated_at=f"2020-01-{day:02d}T00:00:00")

    assert _register(client, headers, token=_token("new")).status_code == 201

    stored = _rows(db, user["id"])
    assert len(stored) == 10
    tokens = {r["token"] for r in stored}
    assert _token("old1") not in tokens
    assert _token("old10") in tokens and _token("new") in tokens
    assert any("Dropped 1 push token(s) over the cap" in r.getMessage() for r in caplog.records)


def test_exactly_ten_tokens_fit_without_evicting_anything(client, actor, db):
    user, headers = actor
    for day in range(1, 10):
        _seed_token(db, user["id"], _token(f"old{day}"), updated_at=f"2020-01-{day:02d}T00:00:00")

    _register(client, headers, token=_token("new"))

    assert len(_rows(db, user["id"])) == 10
    assert _row(db, _token("old1")) is not None


def test_re_registering_at_the_cap_evicts_nothing(client, actor, db):
    # The caller's own token is excluded from the surplus scan,
    # so an app start on a tenth device must not drop a ninth
    user, headers = actor
    for day in range(1, 11):
        _seed_token(db, user["id"], _token(f"old{day}"), updated_at=f"2020-01-{day:02d}T00:00:00")

    response = _register(client, headers, token=_token("old1"))

    assert response.status_code == 200
    assert len(_rows(db, user["id"])) == 10
    assert _row(db, _token("old1"))["active"] == 1


def test_a_bulk_registration_run_never_grows_past_the_cap(client, actor, db):
    user, headers = actor

    for index in range(15):
        assert _register(client, headers, token=_token(f"burner{index}")).status_code == 201

    assert len(_rows(db, user["id"])) == 10


def test_the_cap_counts_only_the_callers_own_tokens(client, db, make_user, auth_headers):
    other = make_user()
    for day in range(1, 11):
        _seed_token(db, other["id"], _token(f"other{day}"), updated_at=f"2020-01-{day:02d}T00:00:00")
    mine = make_user()

    _register(client, auth_headers(mine), token=_token("mine"))

    assert len(_rows(db, other["id"])) == 10
    assert len(_rows(db, mine["id"])) == 1




# ===========================================================
# POST /notifications/register — the racing writer
# ===========================================================


def test_a_racing_registration_answers_with_the_winners_row(client, app, actor, db,
                                                            monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="app.notifications.routes")
    user, headers = actor
    from app.notifications import routes as notifications_routes
    real_get_db = notifications_routes.get_db
    holder = {}

    def _racing():
        holder["conn"] = _RacingConnection(real_get_db(), app.config["DB_PATH"],
                                           winner=(user["id"], GOOD_TOKEN))
        return holder["conn"]

    monkeypatch.setattr(notifications_routes, "get_db", _racing)

    response = _register(client, headers)

    assert response.status_code == 201
    # The winner's row is the answer — never a 500
    assert response.get_json()["tokenId"] == holder["conn"].winner_id
    assert _row(db, GOOD_TOKEN)["id"] == holder["conn"].winner_id
    assert any("raced" in r.getMessage() for r in caplog.records)


def test_a_token_that_never_lands_answers_five_hundred(client, app, actor, monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger="app.notifications.routes")
    _, headers = actor
    from app.notifications import routes as notifications_routes
    real_get_db = notifications_routes.get_db
    monkeypatch.setattr(notifications_routes, "get_db",
                        lambda: _RacingConnection(real_get_db(), app.config["DB_PATH"]))

    response = _register(client, headers)

    assert response.status_code == 500
    assert response.get_json()["error"] == "Could not register push token"
    assert any("vanished" in r.getMessage() for r in caplog.records)




# ===========================================================
# POST /notifications/register — the rate limit
# ===========================================================


def test_the_twenty_first_registration_in_the_window_is_refused(client, actor):
    _, headers = actor

    for index in range(20):
        assert _register(client, headers, token=_token(f"dev{index}")).status_code == 201

    response = _register(client, headers, token=_token("onemore"))

    assert response.status_code == 429
    body = response.get_json()
    assert body["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


def test_the_registration_quota_recovers_after_the_five_minute_window(client, actor, monkeypatch):
    user, headers = actor
    _spend_budget(monkeypatch, "push_register", user["id"], 20)

    assert _register(client, headers).status_code == 429

    with time_machine.travel(datetime.now(timezone.utc).timestamp() + 601, tick=False):
        assert _register(client, headers).status_code == 201


def test_register_and_unregister_share_one_budget(client, actor, monkeypatch):
    # Both routes are keyed "push_register:<user>" — a device
    # that re-registered twenty times cannot then unregister
    user, headers = actor
    _spend_budget(monkeypatch, "push_register", user["id"], 20)

    response = _unregister(client, headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_the_registration_budget_is_per_user_not_global(client, make_user, auth_headers,
                                                        monkeypatch):
    first = make_user()
    second = make_user()
    _spend_budget(monkeypatch, "push_register", first["id"], 20)

    assert _register(client, auth_headers(first)).status_code == 429
    assert _register(client, auth_headers(second), token=_token("second")).status_code == 201




# ===========================================================
# DELETE /notifications/register
# ===========================================================


def test_unregistering_requires_authentication(client):
    response = client.delete(REGISTER, json={"token": GOOD_TOKEN})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_unregistering_with_no_body_is_refused(client, actor):
    _, headers = actor

    response = client.delete(REGISTER, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"


def test_unregistering_with_a_top_level_array_body_is_refused(client, actor):
    _, headers = actor

    response = client.delete(REGISTER, headers=headers, json=["token"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_unregistering_an_empty_token_is_refused(client, actor):
    _, headers = actor

    response = _unregister(client, headers, token="")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Push token required"


def test_unregistering_a_non_string_token_is_refused(client, actor):
    _, headers = actor

    response = _unregister(client, headers, token=12345)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Token must be a string"


def test_unregistering_a_token_past_the_post_length_cap_is_not_refused(client, actor):
    # The POST's length cap is not shared with the DELETE — an
    # owner must be able to name a legacy row of any length
    _, headers = actor

    response = _unregister(client, headers, token="A" * 201)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Token not found"


def test_unregistering_removes_the_row_entirely(client, actor, db):
    # Real removal, unlike the active=0 push.py uses for a dead
    # device — nothing may be left to send to
    user, headers = actor
    _register(client, headers)

    response = _unregister(client, headers)

    assert response.status_code == 200
    assert _rows(db, user["id"]) == []


@pytest.mark.contract
def test_the_unregister_body_is_the_shape_the_mobile_app_consumes(client, actor):
    # services/api/notifications.ts — unregisterPushToken ignores
    # the body, but the route's success shape is pinned anyway
    _, headers = actor
    _register(client, headers)

    body = _unregister(client, headers).get_json()

    assert body == {"unregistered": True}


def test_unregistering_a_token_nobody_registered_is_a_404(client, actor):
    _, headers = actor

    response = _unregister(client, headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Token not found"


def test_unregistering_the_same_token_twice_is_a_404_the_second_time(client, actor):
    _, headers = actor
    _register(client, headers)

    assert _unregister(client, headers).status_code == 200
    assert _unregister(client, headers).status_code == 404


def test_unregistering_another_users_token_is_a_404_and_leaves_it_alone(client, db, make_user,
                                                                        auth_headers):
    owner = make_user()
    stranger = make_user()
    _register(client, auth_headers(owner))

    response = _unregister(client, auth_headers(stranger))

    assert response.status_code == 404
    assert _row(db, GOOD_TOKEN)["user_id"] == owner["id"]


def test_a_legacy_token_that_fails_the_expo_grammar_can_still_be_removed(client, actor, db):
    # DELETE deliberately skips _TOKEN_RE: an owner must be able
    # to drop a row that predates the grammar check
    user, headers = actor
    _seed_token(db, user["id"], "legacy-fcm-token-nonsense")

    response = _unregister(client, headers, token="legacy-fcm-token-nonsense")

    assert response.status_code == 200
    assert _rows(db, user["id"]) == []


def test_unregistering_strips_surrounding_whitespace(client, actor, db):
    user, headers = actor
    _register(client, headers)

    response = _unregister(client, headers, token=f"  {GOOD_TOKEN}\n")

    assert response.status_code == 200
    assert _rows(db, user["id"]) == []


def test_unregistering_one_device_leaves_the_others_registered(client, actor, db):
    user, headers = actor
    _register(client, headers, token=_token("phone"))
    _register(client, headers, token=_token("tablet"))

    _unregister(client, headers, token=_token("phone"))

    remaining = _rows(db, user["id"])
    assert [r["token"] for r in remaining] == [_token("tablet")]


def test_a_detached_logout_unregisters_with_the_forwarded_bearer_token(client, actor, db):
    # AuthContext tears down locally FIRST and then fires this
    # detached with the token it captured — the explicit header
    # is the only thing authenticating the call
    user, headers = actor
    _register(client, headers)
    forwarded = {"Authorization": headers["Authorization"]}

    response = client.delete(REGISTER, headers=forwarded, json={"token": GOOD_TOKEN})

    assert response.status_code == 200
    assert _rows(db, user["id"]) == []


def test_a_forwarded_token_from_a_finished_session_is_refused(client, actor, db):
    # The other order: logout landed first, so the captured token
    # is already dead. The mobile wrapper swallows this 401
    user, headers = actor
    _register(client, headers)
    forwarded = {"Authorization": headers["Authorization"]}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    response = client.delete(REGISTER, headers=forwarded, json={"token": GOOD_TOKEN})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"




# ===========================================================
# GET /notifications/channels
# ===========================================================


def test_reading_the_channels_requires_authentication(client):
    response = client.get(CHANNELS)

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_a_user_who_never_touched_settings_hears_every_channel(client, actor):
    # Opt-out model: no rows at all still means all four on
    _, headers = actor

    response = _read_channels(client, headers)

    assert response.status_code == 200
    assert response.get_json()["channels"] == {
        "news": True, "chat": True, "schedule": True, "admin": True}


@pytest.mark.contract
def test_the_channels_body_is_the_shape_the_mobile_app_consumes(client, actor):
    # services/api/notifications.ts — { channels: Record<NotificationChannel, boolean> }
    _, headers = actor

    body = _read_channels(client, headers).get_json()

    assert set(body) == {"channels"}
    assert set(body["channels"]) == {"news", "chat", "schedule", "admin"}
    assert all(isinstance(v, bool) for v in body["channels"].values())


def test_an_explicit_opt_out_row_reads_as_off(client, actor, db):
    user, headers = actor
    _seed_channel(db, user["id"], "news", 0)

    channels = _read_channels(client, headers).get_json()["channels"]

    assert channels["news"] is False
    assert channels["chat"] is True


def test_a_legacy_non_binary_enabled_value_still_reads_as_on(client, actor, db):
    # bool(2) is True — a hand-edited row must not silence a topic
    user, headers = actor
    _seed_channel(db, user["id"], "chat", 2)

    assert _read_channels(client, headers).get_json()["channels"]["chat"] is True


def test_every_channel_can_be_off_at_once(client, actor, db):
    user, headers = actor
    for channel in ("news", "chat", "schedule", "admin"):
        _seed_channel(db, user["id"], channel, 0)

    assert _read_channels(client, headers).get_json()["channels"] == {
        "news": False, "chat": False, "schedule": False, "admin": False}


def test_channel_switches_are_per_user(client, db, make_user, auth_headers):
    quiet = make_user()
    other = make_user()
    _seed_channel(db, quiet["id"], "news", 0)

    assert _read_channels(client, auth_headers(other)).get_json()["channels"]["news"] is True


def test_reading_the_channels_is_not_rate_limited(client, actor, monkeypatch):
    # Only the writes carry the decorator — the settings screen
    # must always be able to load its switches
    user, headers = actor
    _spend_budget(monkeypatch, "push_channels", user["id"], 500)

    assert _read_channels(client, headers).status_code == 200




# ===========================================================
# PUT /notifications/channels
# ===========================================================


def test_updating_the_channels_requires_authentication(client):
    response = client.put(CHANNELS, json={"channels": {"news": False}})

    assert response.status_code == 401


def test_updating_with_no_body_is_refused(client, actor):
    _, headers = actor

    response = client.put(CHANNELS, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "channels dict required"


def test_updating_with_a_malformed_json_body_is_refused(client, actor):
    _, headers = actor

    response = client.put(CHANNELS, headers=headers, data="{{", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "channels dict required"


def test_updating_with_a_top_level_array_body_is_refused(client, actor):
    _, headers = actor

    response = client.put(CHANNELS, headers=headers, json=[{"news": False}])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("channels", ["news", ["news"], 5, None, True])
def test_the_channels_field_must_be_a_dict(client, actor, channels):
    _, headers = actor

    response = _write_channels(client, headers, channels)

    assert response.status_code == 400
    assert response.get_json()["error"] == "channels dict required"


def test_a_body_without_a_channels_field_is_refused(client, actor):
    _, headers = actor

    response = client.put(CHANNELS, headers=headers, json={"news": False})

    assert response.status_code == 400
    assert response.get_json()["error"] == "channels dict required"


@pytest.mark.parametrize("name", ["newz", "News", "", "email", "schedule ", "admin\n"])
def test_an_unknown_channel_name_is_refused_and_named(client, actor, db, name):
    # It used to be skipped in validation AND in the write loop,
    # so a typo answered 200 and changed nothing
    user, headers = actor

    response = _write_channels(client, headers, {name: False})

    assert response.status_code == 400
    assert _error(response) == f"Unknown channel '{name}'"
    assert db.execute("SELECT COUNT(*) c FROM notification_channels").fetchone()["c"] == 0


@pytest.mark.parametrize("value,type_name", [
    (1, "int"), (0, "int"), ("true", "str"), ("", "str"),
    (None, "NoneType"), ([], "list"), ({}, "dict"), (1.0, "float"),
])
def test_a_non_boolean_value_is_refused_naming_the_channel_and_type(client, actor, value, type_name):
    _, headers = actor

    response = _write_channels(client, headers, {"news": value})

    assert response.status_code == 400
    assert _error(response) == (
        f"Channel 'news' value must be a boolean (true/false), got {type_name}")


def test_a_bad_entry_leaves_no_half_applied_batch(client, actor, db):
    # Validation runs over the WHOLE dict before the first write
    user, headers = actor

    response = _write_channels(client, headers, {"news": False, "bogus": True})

    assert response.status_code == 400
    assert db.execute("SELECT COUNT(*) c FROM notification_channels").fetchone()["c"] == 0
    assert _read_channels(client, headers).get_json()["channels"]["news"] is True


def test_a_bad_value_after_a_good_one_writes_nothing(client, actor, db):
    user, headers = actor

    response = _write_channels(client, headers, {"news": False, "chat": 1})

    assert response.status_code == 400
    assert db.execute("SELECT COUNT(*) c FROM notification_channels").fetchone()["c"] == 0


def test_an_empty_channels_dict_is_a_no_op(client, actor, db):
    _, headers = actor

    response = _write_channels(client, headers, {})

    assert response.status_code == 200
    assert response.get_json()["channels"] == {
        "news": True, "chat": True, "schedule": True, "admin": True}
    assert db.execute("SELECT COUNT(*) c FROM notification_channels").fetchone()["c"] == 0


def test_turning_one_channel_off_leaves_the_others_on(client, actor, db):
    user, headers = actor

    response = _write_channels(client, headers, {"chat": False})

    assert response.status_code == 200
    assert response.get_json()["channels"] == {
        "news": True, "chat": False, "schedule": True, "admin": True}
    rows = db.execute("SELECT channel, enabled FROM notification_channels WHERE user_id = ?",
                      (user["id"],)).fetchall()
    assert [(r["channel"], r["enabled"]) for r in rows] == [("chat", 0)]


def test_the_update_answers_the_full_state_not_just_the_delta(client, actor):
    # settings.tsx takes the response as the confirmed truth
    # after a debounced batch of toggles
    _, headers = actor
    _write_channels(client, headers, {"news": False})

    body = _write_channels(client, headers, {"admin": False}).get_json()

    assert body["channels"] == {"news": False, "chat": True, "schedule": True, "admin": False}


def test_a_channel_can_be_switched_back_on_through_the_same_row(client, actor, db):
    user, headers = actor
    _write_channels(client, headers, {"news": False})

    response = _write_channels(client, headers, {"news": True})

    assert response.get_json()["channels"]["news"] is True
    rows = db.execute("SELECT enabled FROM notification_channels WHERE user_id = ? AND channel = 'news'",
                      (user["id"],)).fetchall()
    assert len(rows) == 1 and rows[0]["enabled"] == 1


def test_all_four_channels_can_be_switched_in_one_call(client, actor, db):
    user, headers = actor

    body = _write_channels(client, headers, {
        "news": False, "chat": False, "schedule": True, "admin": False}).get_json()

    assert body["channels"] == {"news": False, "chat": False, "schedule": True, "admin": False}
    assert db.execute("SELECT COUNT(*) c FROM notification_channels WHERE user_id = ?",
                      (user["id"],)).fetchone()["c"] == 4


def test_the_update_and_the_read_agree(client, actor):
    _, headers = actor

    written = _write_channels(client, headers, {"schedule": False}).get_json()

    assert _read_channels(client, headers).get_json() == written


def test_a_second_write_refreshes_updated_at_in_t_form(client, actor, db):
    user, headers = actor
    _seed_channel(db, user["id"], "news", 1)

    _write_channels(client, headers, {"news": False})

    stamp = db.execute("SELECT updated_at FROM notification_channels"
                       " WHERE user_id = ? AND channel = 'news'", (user["id"],)).fetchone()[0]
    assert stamp > "2020-01-01T00:00:00"
    assert "T" in stamp and " " not in stamp and "+" not in stamp


def test_channel_updates_never_touch_another_user(client, db, make_user, auth_headers):
    mine = make_user()
    other = make_user()
    _seed_channel(db, other["id"], "news", 0)

    _write_channels(client, auth_headers(mine), {"news": True})

    assert db.execute("SELECT enabled FROM notification_channels WHERE user_id = ?",
                      (other["id"],)).fetchone()["enabled"] == 0


def test_the_sixty_first_channel_update_in_the_window_is_refused(client, actor, monkeypatch):
    user, headers = actor
    _spend_budget(monkeypatch, "push_channels", user["id"], 60)

    response = _write_channels(client, headers, {"news": False})

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


def test_the_channel_quota_recovers_after_the_five_minute_window(client, actor, monkeypatch):
    user, headers = actor
    _spend_budget(monkeypatch, "push_channels", user["id"], 60)

    assert _write_channels(client, headers, {"news": False}).status_code == 429

    with time_machine.travel(datetime.now(timezone.utc).timestamp() + 601, tick=False):
        assert _write_channels(client, headers, {"news": False}).status_code == 200


def test_the_two_write_budgets_are_separate(client, actor, monkeypatch):
    user, headers = actor
    _spend_budget(monkeypatch, "push_channels", user["id"], 60)

    assert _write_channels(client, headers, {"news": False}).status_code == 429
    assert _register(client, headers).status_code == 201




# ===========================================================
# The switches and the sender — one contract, end to end
# ===========================================================


# -----------------------------------------------------------
# _expo_ok
# -----------------------------------------------------------
#
# Expo's accepted-batch answer: one ok ticket per message. The
# container has no network, so every send below rides this.
# -----------------------------------------------------------

def _expo_ok(count=1):
    responses.add(responses.POST, push_module.EXPO_PUSH_URL,
                  json={"data": [{"status": "ok", "id": f"ticket-{i}"} for i in range(count)]},
                  status=200)


@responses.activate
def test_a_device_registered_through_the_route_is_reachable_by_the_sender(client, actor,
                                                                          monkeypatch):
    # The opt-out model, proved from both ends: a user who never
    # opened settings still gets the broadcast
    monkeypatch.setattr(push_module, "_SLICE_INTERVAL", 0)
    user, headers = actor
    _register(client, headers, language="lt")
    _expo_ok()

    sent = push_module.notify_channel_users("news", [user["id"]], "Naujiena", "Turinys")

    assert sent == 1
    assert len(responses.calls) == 1


@responses.activate
def test_a_channel_switched_off_through_the_route_drops_the_device(client, actor, monkeypatch):
    monkeypatch.setattr(push_module, "_SLICE_INTERVAL", 0)
    user, headers = actor
    _register(client, headers)
    assert _write_channels(client, headers, {"news": False}).status_code == 200
    _expo_ok()

    sent = push_module.notify_channel_users("news", [user["id"]], "Naujiena", "Turinys")

    assert sent == 0
    assert len(responses.calls) == 0


@responses.activate
def test_switching_a_channel_back_on_makes_the_device_reachable_again(client, actor, monkeypatch):
    monkeypatch.setattr(push_module, "_SLICE_INTERVAL", 0)
    user, headers = actor
    _register(client, headers)
    _write_channels(client, headers, {"chat": False})
    _write_channels(client, headers, {"chat": True})
    _expo_ok()

    assert push_module.notify_channel_users("chat", [user["id"]], "Zinute", "Labas") == 1


@responses.activate
def test_an_opt_out_silences_only_the_channel_it_names(client, actor, monkeypatch):
    monkeypatch.setattr(push_module, "_SLICE_INTERVAL", 0)
    user, headers = actor
    _register(client, headers)
    _write_channels(client, headers, {"news": False})
    _expo_ok()

    assert push_module.notify_channel_users("chat", [user["id"]], "Zinute", "Labas") == 1


@responses.activate
def test_the_registered_language_picks_the_english_copy(client, actor, monkeypatch):
    # What push_tokens.language is FOR — the app re-registers on
    # a language switch so this follows the setting
    monkeypatch.setattr(push_module, "_SLICE_INTERVAL", 0)
    user, headers = actor
    _register(client, headers, language="en")
    _expo_ok()

    push_module.notify_channel_users("news", [user["id"]], "Naujiena", "Turinys",
                                     title_en="News", body_en="Content")

    body = responses.calls[0].request.body
    payload = body.decode() if isinstance(body, bytes) else body
    assert "News" in payload and "Naujiena" not in payload


@responses.activate
def test_an_unregistered_device_is_reachable_by_nobody(client, actor, monkeypatch):
    monkeypatch.setattr(push_module, "_SLICE_INTERVAL", 0)
    user, headers = actor
    _register(client, headers)
    _unregister(client, headers)
    _expo_ok()

    assert push_module.notify_channel_users("news", [user["id"]], "Naujiena", "Turinys") == 0
    assert len(responses.calls) == 0
