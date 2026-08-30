# -----------------------------------------------------------
#  [*] Tests — login, logout, logout-all and the session token
#
#  The credential half of app/auth/routes.py: the three routes
#  that mint and revoke opaque bearer sessions, plus the
#  machinery every other blueprint leans on to resolve one.
#
#  What this module proves:
#
#    - a correct username OR email (either case) signs in; a
#      wrong password, an unknown account and a deactivated
#      one each get their documented answer, and unknown vs.
#      wrong are byte-identical so login cannot be used to
#      enumerate accounts
#    - the token in the response is NEVER the token in the
#      database: sessions.token holds its sha256, so a DbGate
#      peek or a leaked backup yields nothing replayable
#      (migration v13)
#    - a bearer token resolves through resolve_session_token —
#      the lookup the socket handshake shares — including the
#      scheme parsing, the 30-day expiry with its lazy purge
#      of the session AND the user's push tokens, the
#      users.active backstop and the per-request g cache
#    - logout drops EXACTLY the presented session (and only the
#      named device's push row); logout-all drops every session
#      and push row of that user and nobody else's
#    - the login rate limiter counts FAILURES only, in two
#      buckets (per IP, per identifier), answers the house 429
#      with a counting-down Retry-After, and lets a caller back
#      in once the 5-minute window passes
#
#  No wall-clock sleeping: session expiry travels with
#  time_machine, and the rate-limit window is aged by rewriting
#  the limiter's monotonic stamps (time_machine deliberately
#  does not patch time.monotonic).
# -----------------------------------------------------------

import hashlib
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine
from flask import g, jsonify, request

from app.auth import routes as auth_routes




# -----------------------------------------------------------
# _clean_rate_limit_store
# -----------------------------------------------------------
#
# The limiter store is module-level and outlives a test — the
# app fixture rebuilds the database, never this dict. Cleared
# on both sides so neither a sibling module's failed logins
# leak into these tests nor the LRU flood below leaks out.
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
# _login / _bearer / _me
# -----------------------------------------------------------
#
# One-liners for the three moves every test here makes: post a
# login body (optionally from a chosen client IP, which the
# per-IP bucket keys on), build the Authorization header, and
# probe a require_auth route with it.
#
# Used by:
#   - most tests below
# -----------------------------------------------------------

def _login(client, ip=None, **payload):
    kwargs = {"json": payload}
    if ip is not None:
        kwargs["environ_base"] = {"REMOTE_ADDR": ip}
    return client.post("/api/auth/login", **kwargs)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _me(client, token):
    return client.get("/api/auth/me", headers=_bearer(token))




# -----------------------------------------------------------
# _sha256 / _session_rows / _push_rows
# -----------------------------------------------------------
#
# _sha256 recomputes the at-rest hash INDEPENDENTLY of
# auth's _hash_token — asserting against the module's own
# helper would pass even if both changed together, which is
# exactly the regression the storage rule exists to prevent.
#
# Used by:
#   - the token-storage, session-cap and logout tests
# -----------------------------------------------------------

def _sha256(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _session_rows(db, user_id):
    return db.execute(
        "SELECT id, token, created_at, expires_at FROM sessions WHERE user_id = ?"
        " ORDER BY created_at, id",
        (user_id,),
    ).fetchall()


def _push_rows(db, user_id):
    return [r["token"] for r in db.execute(
        "SELECT token FROM push_tokens WHERE user_id = ? ORDER BY token", (user_id,),
    ).fetchall()]


def _seed_push_token(db, user_id, token):
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform) VALUES (?, ?, ?, 'ios')",
        (str(uuid.uuid4()), user_id, token),
    )
    db.commit()




# -----------------------------------------------------------
# _age_rate_limit_window
# -----------------------------------------------------------
#
# Pushes every recorded attempt `seconds` further into the
# past — what wall-clock passage would do to the limiter,
# without sleeping and without patching time.monotonic (which
# time_machine leaves alone by design). Default: just past the
# 5-minute window, so every bucket is empty on the next check.
#
# Used by:
#   - the rate-limit window and Retry-After tests
# -----------------------------------------------------------

def _age_rate_limit_window(seconds=None):
    shift = auth_routes._RATE_LIMIT_WINDOW + 1 if seconds is None else seconds
    with auth_routes._rate_limit_lock:
        for key in list(auth_routes._rate_limit_store):
            auth_routes._rate_limit_store[key] = [
                stamp - shift for stamp in auth_routes._rate_limit_store[key]
            ]




# -----------------------------------------------------------
# _PurgeBlockedConnection
# -----------------------------------------------------------
#
# A real connection that refuses exactly one statement: the
# expired-session DELETE, with the "database is locked" error
# a concurrent writer would raise. Stands in for a lock window
# so the best-effort purge can be proven to answer 401 instead
# of 500.
#
# Used by:
#   - test_an_expired_session_still_answers_401_when_the_purge_cannot_run
# -----------------------------------------------------------

class _PurgeBlockedConnection:

    def __init__(self, real):
        self._real = real

    def execute(self, sql, params=()):
        if "DELETE FROM sessions" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)




# -----------------------------------------------------------
# _mount
# -----------------------------------------------------------
#
# Registers a throwaway route on the per-test app so the gate
# decorators (require_auth / require_role / rate_limit) can be
# exercised as themselves, not through whichever blueprint
# happens to use them. Flask refuses new routes once the app
# has served a request, so call this BEFORE the first client
# call in the test (and never take the `actor`/`admin`
# fixtures, which log in during setup).
#
# Used by:
#   - the decorator tests at the bottom
# -----------------------------------------------------------

def _mount(app, rule, view):
    app.add_url_rule(rule, rule.strip("/").replace("/", "_"), view, methods=["GET", "POST"])




# -----------------------------------------------------------
# Login — the happy path
# -----------------------------------------------------------

def test_login_with_the_right_password_returns_the_user_and_a_token(client, make_user):
    user = make_user(display_name="Jonas Jonaitis")

    response = _login(client, username=user["username"], password=user["password"])

    assert response.status_code == 200
    body = response.get_json()
    assert body["user"]["id"] == user["id"]
    assert body["user"]["username"] == user["username"]
    assert body["user"]["displayName"] == "Jonas Jonaitis"
    assert body["user"]["role"] == "student"
    assert body["user"]["invited"] is True
    assert isinstance(body["token"], str) and body["token"]


@pytest.mark.contract
def test_the_login_body_is_the_shape_the_mobile_app_consumes(client, make_user):
    # services/api/auth.ts — AuthResponse { user: User, token }
    user = make_user()

    body = _login(client, username=user["username"], password=user["password"]).get_json()

    assert set(body) == {"user", "token"}
    assert set(body["user"]) == {
        "id", "username", "email", "displayName", "role", "avatarUrl",
        "invited", "studentNumber", "studyGroup", "studyProgram",
    }


def test_login_never_hands_back_the_password_hash_or_the_active_flag(client, make_user):
    user = make_user()

    body = _login(client, username=user["username"], password=user["password"]).get_json()

    assert "password_hash" not in body["user"]
    assert "active" not in body["user"]
    assert user["password"] not in repr(body)


def test_the_login_token_authenticates_a_protected_route(client, make_user):
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]

    response = _me(client, token)

    assert response.status_code == 200
    assert response.get_json()["id"] == user["id"]


def test_login_accepts_the_email_address_as_the_identifier(client, make_user):
    user = make_user()

    response = _login(client, email=user["email"], password=user["password"])

    assert response.status_code == 200
    assert response.get_json()["user"]["id"] == user["id"]


def test_login_matches_the_username_case_insensitively(client, make_user):
    user = make_user(username="jonas.jonaitis")

    response = _login(client, username="JoNaS.JoNaItIs", password=user["password"])

    assert response.status_code == 200
    assert response.get_json()["user"]["id"] == user["id"]


def test_login_matches_the_email_case_insensitively(client, make_user):
    user = make_user(username="ona")

    response = _login(client, username=user["email"].upper(), password=user["password"])

    assert response.status_code == 200
    assert response.get_json()["user"]["id"] == user["id"]


def test_the_username_key_wins_when_both_username_and_email_are_sent(client, make_user):
    named = make_user(username="pirmas")
    other = make_user(username="antras")

    response = _login(client, username=named["username"], email=other["email"],
                      password=named["password"])

    assert response.status_code == 200
    assert response.get_json()["user"]["id"] == named["id"]


def test_an_empty_username_falls_back_to_the_email_key(client, make_user):
    user = make_user()

    response = _login(client, username="", email=user["email"], password=user["password"])

    assert response.status_code == 200
    assert response.get_json()["user"]["id"] == user["id"]


def test_an_identifier_padded_with_spaces_is_not_matched(client, make_user):
    # The lookup forgives case, not whitespace — the clients trim
    # before sending, and a padded identifier must not silently
    # resolve to a neighbouring account
    user = make_user(username="tomas")

    response = _login(client, username="  tomas ", password=user["password"])

    assert response.status_code == 401


def test_two_accounts_differing_only_in_case_each_stay_reachable(client, make_user):
    # Legacy rows: the uniqueness pre-check was BINARY once, so a
    # 'Tomas' and a 'tomas' can coexist. Every NOCASE match is
    # tried and the row the password verifies for wins
    upper = make_user(username="Tomas", password="pirmas-slaptazodis")
    lower = make_user(username="tomas", password="antras-slaptazodis")

    first = _login(client, username="tomas", password="pirmas-slaptazodis")
    second = _login(client, username="tomas", password="antras-slaptazodis")

    assert first.get_json()["user"]["id"] == upper["id"]
    assert second.get_json()["user"]["id"] == lower["id"]




# -----------------------------------------------------------
# Login — the session it mints
# -----------------------------------------------------------

def test_the_database_stores_only_the_sha256_of_the_token(client, db, make_user):
    user = make_user()

    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]

    rows = _session_rows(db, user["id"])
    assert len(rows) == 1
    assert rows[0]["token"] != token
    assert rows[0]["token"] == _sha256(token)


def test_a_stolen_session_row_cannot_be_replayed_as_a_bearer_token(client, db, make_user):
    user = make_user()
    _login(client, username=user["username"], password=user["password"])

    stored = _session_rows(db, user["id"])[0]["token"]

    assert _me(client, stored).status_code == 401


def test_every_login_mints_a_session_and_leaves_the_earlier_one_valid(client, db, make_user):
    user = make_user()

    first = _login(client, username=user["username"], password=user["password"]).get_json()["token"]
    second = _login(client, username=user["username"], password=user["password"]).get_json()["token"]

    assert first != second
    assert len(_session_rows(db, user["id"])) == 2
    assert _me(client, first).status_code == 200
    assert _me(client, second).status_code == 200


def test_a_new_session_expires_thirty_days_after_the_login(client, db, make_user):
    user = make_user()

    with time_machine.travel(datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc), tick=False):
        _login(client, username=user["username"], password=user["password"])

    row = _session_rows(db, user["id"])[0]
    assert datetime.fromisoformat(row["expires_at"]) == datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat(row["created_at"]).tzinfo is not None


def test_only_the_ten_newest_sessions_of_a_user_survive_a_login(client, db, make_user):
    user = make_user()
    neighbour = make_user()
    neighbour_token = _login(client, username=neighbour["username"],
                             password=neighbour["password"]).get_json()["token"]

    tokens = [
        _login(client, username=user["username"], password=user["password"]).get_json()["token"]
        for _ in range(auth_routes._SESSIONS_PER_USER + 1)
    ]

    assert len(_session_rows(db, user["id"])) == auth_routes._SESSIONS_PER_USER
    assert _me(client, tokens[0]).status_code == 401
    assert _me(client, tokens[-1]).status_code == 200
    # The cap is per user — a neighbour's session is not collateral
    assert _me(client, neighbour_token).status_code == 200


def test_a_login_does_not_disturb_another_users_session(client, db, make_user):
    one = make_user()
    two = make_user()
    one_token = _login(client, username=one["username"], password=one["password"]).get_json()["token"]

    _login(client, username=two["username"], password=two["password"])

    assert _me(client, one_token).status_code == 200
    assert len(_session_rows(db, one["id"])) == 1




# -----------------------------------------------------------
# Login — refusals
# -----------------------------------------------------------

def test_a_login_without_any_body_is_refused(client):
    response = client.post("/api/auth/login")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_an_empty_json_object_is_refused(client):
    response = client.post("/api/auth/login", json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_malformed_json_body_is_refused(client):
    response = client.post("/api/auth/login", data="{not json",
                           content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_top_level_array_body_is_refused(client):
    response = client.post("/api/auth/login", json=["admin", "slaptazodis"])

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_a_login_without_a_password_is_refused(client, make_user):
    user = make_user()

    response = _login(client, username=user["username"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "Username/email and password required"


def test_a_login_without_an_identifier_is_refused(client):
    response = _login(client, password="slaptazodis123")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Username/email and password required"


def test_a_blank_password_is_refused_before_any_lookup(client, make_user):
    user = make_user()

    response = _login(client, username=user["username"], password="")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Username/email and password required"


def test_a_non_string_identifier_is_refused(client):
    response = _login(client, username=12345, password="slaptazodis123")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Username/email and password must be strings"


def test_a_non_string_password_is_refused(client, make_user):
    user = make_user()

    response = _login(client, username=user["username"], password={"a": 1})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Username/email and password must be strings"


def test_a_wrong_password_is_invalid_credentials(client, make_user):
    user = make_user()

    response = _login(client, username=user["username"], password="ne-tas-slaptazodis")

    assert response.status_code == 401
    assert response.get_json() == {"error": "Invalid credentials", "code": "invalid_credentials"}


def test_an_unknown_account_answers_exactly_what_a_wrong_password_does(client, make_user):
    # Account existence must not be readable off the response
    user = make_user()

    wrong = _login(client, username=user["username"], password="ne-tas-slaptazodis")
    unknown = _login(client, username="niekada-nebuves", password="ne-tas-slaptazodis")

    assert (wrong.status_code, wrong.get_json()) == (unknown.status_code, unknown.get_json())
    assert unknown.status_code == 401


def test_a_failed_login_mints_no_session(client, db, make_user):
    user = make_user()

    _login(client, username=user["username"], password="ne-tas-slaptazodis")

    assert _session_rows(db, user["id"]) == []


def test_an_over_long_password_is_a_401_and_never_a_server_error(client, make_user):
    # bcrypt truncates past 72 bytes; the guess still has to be
    # refused, not blow up the route
    user = make_user()

    response = _login(client, username=user["username"], password="x" * 500)

    assert response.status_code == 401


def test_a_deactivated_account_is_told_that_it_is_deactivated(client, make_user):
    user = make_user(active=0)

    response = _login(client, username=user["username"], password=user["password"])

    assert response.status_code == 403
    assert response.get_json() == {"error": "Account deactivated", "code": "account_deactivated"}


def test_a_deactivated_account_with_a_wrong_password_reveals_nothing(client, make_user):
    # The flag is disclosed only to whoever proved they own the
    # account — a guesser sees the ordinary 401
    user = make_user(active=0)

    response = _login(client, username=user["username"], password="ne-tas-slaptazodis")

    assert response.status_code == 401
    assert response.get_json()["code"] == "invalid_credentials"


def test_a_deactivated_login_mints_no_session(client, db, make_user):
    user = make_user(active=0)

    _login(client, username=user["username"], password=user["password"])

    assert _session_rows(db, user["id"]) == []




# -----------------------------------------------------------
# Registration interop — the accounts login has to resolve
# -----------------------------------------------------------
#
# Not the registration matrix (that lives in its own module) —
# only the part login depends on: register lowercases the
# stored email and pre-checks uniqueness COLLATE NOCASE, so
# every account it creates resolves to exactly one row however
# the user later types their identifier, and the session it
# mints is stored under the same sha256 rule.
# -----------------------------------------------------------

def _register(client, **overrides):
    payload = {"username": "jonas.j", "password": "labas-rytas-77",
               "display_name": "Jonas Jonaitis", "email": "Jonas.J@Knf.Vu.LT"}
    payload.update(overrides)
    return client.post("/api/auth/register", json=payload)


def test_an_account_created_by_register_signs_in_under_any_case(client):
    created = _register(client)
    assert created.status_code == 201

    by_username = _login(client, username="JONAS.J", password="labas-rytas-77")
    by_email = _login(client, email="jonas.j@KNF.VU.LT", password="labas-rytas-77")

    assert by_username.get_json()["user"]["id"] == created.get_json()["user"]["id"]
    assert by_email.get_json()["user"]["id"] == created.get_json()["user"]["id"]


def test_the_session_register_mints_is_hashed_at_rest_too(client, db):
    body = _register(client).get_json()

    rows = _session_rows(db, body["user"]["id"])
    assert len(rows) == 1
    assert rows[0]["token"] == _sha256(body["token"])
    assert rows[0]["token"] != body["token"]


def test_registering_without_a_code_creates_a_guest_who_can_sign_in(client):
    created = _register(client)

    assert created.get_json()["user"]["invited"] is False
    assert created.get_json()["user"]["role"] == "student"

    signed_in = _login(client, username="jonas.j", password="labas-rytas-77")
    assert signed_in.get_json()["user"]["invited"] is False


def test_registering_with_the_bootstrap_code_creates_an_invited_account(client, seeded_code):
    created = _register(client, invitation_code=seeded_code)

    assert created.status_code == 201
    assert created.get_json()["user"]["invited"] is True

    signed_in = _login(client, username="jonas.j", password="labas-rytas-77")
    assert signed_in.get_json()["user"]["invited"] is True


def test_register_refuses_a_username_that_only_differs_in_case(client, make_user):
    # The NOCASE pre-check is what keeps login's two-column
    # lookup pointing at exactly one row for new accounts
    make_user(username="Jonas.J")

    refused = _register(client, username="jonas.j", email="kitas@knf.vu.lt")

    assert refused.status_code == 409
    assert refused.get_json()["code"] == "username_taken"




# -----------------------------------------------------------
# Bearer resolution — the header, the row, the active backstop
# -----------------------------------------------------------

def test_the_bearer_scheme_is_matched_case_insensitively(client, actor):
    user, headers = actor
    token = headers["Authorization"].split(" ", 1)[1]

    for scheme in ("bearer", "BEARER", "BeArEr"):
        response = client.get("/api/auth/me", headers={"Authorization": f"{scheme} {token}"})
        assert response.status_code == 200, scheme


def test_extra_whitespace_around_the_token_is_tolerated(client, actor):
    user, headers = actor
    token = headers["Authorization"].split(" ", 1)[1]

    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer   {token}  "})

    assert response.status_code == 200


def test_another_authorization_scheme_is_not_accepted(client, actor):
    user, headers = actor
    token = headers["Authorization"].split(" ", 1)[1]

    for header in (f"Token {token}", f"Basic {token}", token):
        response = client.get("/api/auth/me", headers={"Authorization": header})
        assert response.status_code == 401, header


def test_an_authorization_header_without_a_token_is_rejected(client):
    for header in ("Bearer", "Bearer ", "Bearer    ", ""):
        response = client.get("/api/auth/me", headers={"Authorization": header})
        assert response.status_code == 401, repr(header)


def test_a_request_with_no_authorization_header_is_rejected(client):
    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_an_unknown_token_is_rejected(client, make_user):
    make_user()

    assert _me(client, str(uuid.uuid4())).status_code == 401


def test_resolve_session_token_answers_the_socket_handshake_without_a_request(app, client, make_user):
    # chat/events.py resolves handshake tokens through this exact
    # call, outside any Flask request context
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]

    resolved = auth_routes.resolve_session_token(token)

    assert resolved["id"] == user["id"]
    assert "password_hash" not in resolved
    assert auth_routes.resolve_session_token(str(uuid.uuid4())) is None


def test_a_session_whose_user_row_vanished_resolves_to_nobody(app, client, db, make_user):
    orphan_token = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions (id, user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), "no-such-user", _sha256(orphan_token),
         datetime.now(timezone.utc).isoformat(),
         (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
    )
    db.commit()

    assert _me(client, orphan_token).status_code == 401


def test_a_deactivated_user_loses_a_session_minted_before_the_flag_flip(client, db, make_user):
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]
    assert _me(client, token).status_code == 200

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert _me(client, token).status_code == 401




# -----------------------------------------------------------
# Bearer resolution — expiry
# -----------------------------------------------------------

def test_a_session_still_works_on_its_last_day(client, make_user):
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    with time_machine.travel(base, tick=False):
        token = _login(client, username=user["username"],
                       password=user["password"]).get_json()["token"]

    with time_machine.travel(base + timedelta(days=29, hours=23), tick=False):
        assert _me(client, token).status_code == 200


def test_a_session_stops_working_once_thirty_days_have_passed(client, db, make_user):
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    with time_machine.travel(base, tick=False):
        token = _login(client, username=user["username"],
                       password=user["password"]).get_json()["token"]

    with time_machine.travel(base + timedelta(days=30, minutes=1), tick=False):
        assert _me(client, token).status_code == 401

    # The expired row is purged on the spot, not left to rot
    assert _session_rows(db, user["id"]) == []


def test_an_expired_session_takes_that_users_push_tokens_with_it(client, db, make_user):
    user = make_user()
    neighbour = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    with time_machine.travel(base, tick=False):
        token = _login(client, username=user["username"],
                       password=user["password"]).get_json()["token"]
    _seed_push_token(db, user["id"], "ExponentPushToken[savas]")
    _seed_push_token(db, neighbour["id"], "ExponentPushToken[kaimyno]")

    with time_machine.travel(base + timedelta(days=31), tick=False):
        assert _me(client, token).status_code == 401

    assert _push_rows(db, user["id"]) == []
    assert _push_rows(db, neighbour["id"]) == ["ExponentPushToken[kaimyno]"]


def test_a_session_with_an_unreadable_expiry_counts_as_expired(client, db, make_user):
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]

    db.execute("UPDATE sessions SET expires_at = 'kada nors' WHERE token = ?", (_sha256(token),))
    db.commit()

    assert _me(client, token).status_code == 401
    assert _session_rows(db, user["id"]) == []


def test_a_naive_expiry_stamp_in_the_future_is_read_as_utc(client, db, make_user):
    # Legacy rows carry no offset; they must be treated as UTC,
    # not as an unparseable value
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]
    naive = (datetime.now(timezone.utc) + timedelta(days=2)).replace(tzinfo=None).isoformat()

    db.execute("UPDATE sessions SET expires_at = ? WHERE token = ?", (naive, _sha256(token)))
    db.commit()

    assert _me(client, token).status_code == 200


def test_a_naive_expiry_stamp_in_the_past_is_expired(client, db, make_user):
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]
    naive = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None).isoformat()

    db.execute("UPDATE sessions SET expires_at = ? WHERE token = ?", (naive, _sha256(token)))
    db.commit()

    assert _me(client, token).status_code == 401


def test_an_expired_session_still_answers_401_when_the_purge_cannot_run(client, db, make_user, monkeypatch):
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]
    db.execute("UPDATE sessions SET expires_at = 'kada nors' WHERE token = ?", (_sha256(token),))
    db.commit()

    real_get_db = auth_routes.get_db
    monkeypatch.setattr(auth_routes, "get_db", lambda: _PurgeBlockedConnection(real_get_db()))

    response = _me(client, token)

    assert response.status_code == 401
    # The purge is best-effort: the row survives a locked window,
    # and the caller still gets a clean 401 rather than a 500
    assert len(_session_rows(db, user["id"])) == 1




# -----------------------------------------------------------
# Bearer resolution — the per-request cache
# -----------------------------------------------------------

def test_the_caller_is_resolved_once_per_request(app, client, make_user, monkeypatch):
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]

    calls = []
    real_resolve = auth_routes.resolve_session_token

    def counting(raw):
        calls.append(raw)
        return real_resolve(raw)

    monkeypatch.setattr(auth_routes, "resolve_session_token", counting)

    with app.test_request_context("/api/auth/me", headers=_bearer(token)):
        first = auth_routes.get_current_user()
        second = auth_routes.get_current_user()

    assert first["id"] == user["id"]
    assert second is first
    assert len(calls) == 1


def test_a_rejected_token_is_cached_as_a_rejection_too(app, client, monkeypatch):
    calls = []
    monkeypatch.setattr(auth_routes, "resolve_session_token",
                        lambda raw: calls.append(raw) or None)

    with app.test_request_context("/api/auth/me", headers=_bearer("nera-tokio")):
        assert auth_routes.get_current_user() is None
        assert auth_routes.get_current_user() is None

    assert len(calls) == 1


def test_a_cache_entry_for_another_token_is_not_reused(app, client, make_user):
    user = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]

    with app.test_request_context("/api/auth/me", headers=_bearer(token)):
        g._auth_cache = ("kitas-tokenas", {"id": "kitas-vartotojas"})

        resolved = auth_routes.get_current_user()

    assert resolved["id"] == user["id"]




# -----------------------------------------------------------
# Logout
# -----------------------------------------------------------

def test_logout_drops_the_presented_session(client, db, actor):
    user, headers = actor

    response = client.post("/api/auth/logout", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"message": "Logged out"}
    assert _session_rows(db, user["id"]) == []
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_logout_invalidates_exactly_one_session(client, db, make_user):
    user = make_user()
    phone = _login(client, username=user["username"], password=user["password"]).get_json()["token"]
    tablet = _login(client, username=user["username"], password=user["password"]).get_json()["token"]

    assert client.post("/api/auth/logout", headers=_bearer(phone)).status_code == 200

    rows = _session_rows(db, user["id"])
    assert len(rows) == 1
    assert rows[0]["token"] == _sha256(tablet)
    assert _me(client, phone).status_code == 401
    assert _me(client, tablet).status_code == 200


def test_logout_leaves_other_users_signed_in(client, db, make_user):
    user = make_user()
    neighbour = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]
    neighbour_token = _login(client, username=neighbour["username"],
                             password=neighbour["password"]).get_json()["token"]

    client.post("/api/auth/logout", headers=_bearer(token))

    assert _me(client, neighbour_token).status_code == 200
    assert len(_session_rows(db, neighbour["id"])) == 1


def test_logout_requires_a_live_session(client):
    response = client.post("/api/auth/logout")

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_logging_out_twice_is_a_401_the_second_time(client, actor):
    user, headers = actor

    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 401


def test_logout_without_a_body_keeps_the_devices_push_tokens(client, db, actor):
    user, headers = actor
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert _push_rows(db, user["id"]) == ["ExponentPushToken[telefonas]"]


def test_logout_with_a_push_token_silences_only_that_device(client, db, make_user, auth_headers):
    user = make_user()
    headers = auth_headers(user)
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")
    _seed_push_token(db, user["id"], "ExponentPushToken[plansete]")

    response = client.post("/api/auth/logout", headers=headers,
                           json={"pushToken": "ExponentPushToken[telefonas]"})

    assert response.status_code == 200
    assert _push_rows(db, user["id"]) == ["ExponentPushToken[plansete]"]


def test_logout_cannot_silence_another_users_device(client, db, actor, make_user):
    user, headers = actor
    neighbour = make_user()
    _seed_push_token(db, neighbour["id"], "ExponentPushToken[kaimyno]")

    response = client.post("/api/auth/logout", headers=headers,
                           json={"pushToken": "ExponentPushToken[kaimyno]"})

    assert response.status_code == 200
    assert _push_rows(db, neighbour["id"]) == ["ExponentPushToken[kaimyno]"]


def test_a_push_token_that_is_not_a_string_is_ignored(client, db, make_user, auth_headers):
    # A fresh session per value: the logout it rides on consumes
    # the one it was presented with
    user = make_user()
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    for value in (12345, None, ""):
        response = client.post("/api/auth/logout", headers=auth_headers(user),
                               json={"pushToken": value})
        assert response.status_code == 200, value

    assert _push_rows(db, user["id"]) == ["ExponentPushToken[telefonas]"]


def test_a_body_that_is_not_json_at_all_is_tolerated(client, db, actor):
    user, headers = actor

    response = client.post("/api/auth/logout", headers=headers,
                           data="labas", content_type="text/plain")

    assert response.status_code == 200
    assert _session_rows(db, user["id"]) == []


def test_logout_kicks_the_users_live_sockets(client, actor, monkeypatch):
    from app.chat import events as chat_events
    user, headers = actor
    kicked = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", kicked.append)

    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert kicked == [user["id"]]


def test_a_failing_socket_kick_never_fails_the_logout(client, db, actor, monkeypatch):
    from app.chat import events as chat_events
    user, headers = actor

    def explode(_user_id):
        raise RuntimeError("socket layer down")

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", explode)

    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert _session_rows(db, user["id"]) == []


def test_logout_works_when_the_socket_layer_is_unavailable(client, db, actor, monkeypatch):
    # The kill switch is a guarded lazy import — no socket module,
    # no logout failure
    user, headers = actor
    monkeypatch.setitem(sys.modules, "app.chat.events", None)

    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert _session_rows(db, user["id"]) == []




# -----------------------------------------------------------
# Logout-all
# -----------------------------------------------------------

def test_logout_all_drops_every_session_of_the_caller(client, db, make_user):
    user = make_user()
    tokens = [
        _login(client, username=user["username"], password=user["password"]).get_json()["token"]
        for _ in range(3)
    ]

    response = client.post("/api/auth/logout-all", headers=_bearer(tokens[-1]))

    assert response.status_code == 200
    assert response.get_json() == {"message": "Logged out everywhere"}
    assert _session_rows(db, user["id"]) == []
    for token in tokens:
        assert _me(client, token).status_code == 401


def test_logout_all_leaves_every_other_user_signed_in(client, db, make_user):
    user = make_user()
    neighbour = make_user()
    token = _login(client, username=user["username"], password=user["password"]).get_json()["token"]
    neighbour_tokens = [
        _login(client, username=neighbour["username"],
               password=neighbour["password"]).get_json()["token"]
        for _ in range(2)
    ]

    client.post("/api/auth/logout-all", headers=_bearer(token))

    assert len(_session_rows(db, neighbour["id"])) == 2
    for other in neighbour_tokens:
        assert _me(client, other).status_code == 200


def test_logout_all_drops_the_callers_push_tokens_and_nobody_elses(client, db, actor, make_user):
    user, headers = actor
    neighbour = make_user()
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")
    _seed_push_token(db, user["id"], "ExponentPushToken[plansete]")
    _seed_push_token(db, neighbour["id"], "ExponentPushToken[kaimyno]")

    assert client.post("/api/auth/logout-all", headers=headers).status_code == 200

    assert _push_rows(db, user["id"]) == []
    assert _push_rows(db, neighbour["id"]) == ["ExponentPushToken[kaimyno]"]


def test_logout_all_requires_a_live_session(client):
    response = client.post("/api/auth/logout-all")

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_logout_all_kicks_the_users_live_sockets(client, actor, monkeypatch):
    from app.chat import events as chat_events
    user, headers = actor
    kicked = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", kicked.append)

    assert client.post("/api/auth/logout-all", headers=headers).status_code == 200

    assert kicked == [user["id"]]




# -----------------------------------------------------------
# The login rate limiter
# -----------------------------------------------------------

def test_ten_failed_logins_for_one_account_earn_a_429(client, make_user):
    user = make_user()

    for _ in range(auth_routes._RATE_LIMIT_MAX):
        assert _login(client, ip="10.0.0.1", username=user["username"],
                      password="ne-tas").status_code == 401

    blocked = _login(client, ip="10.0.0.1", username=user["username"], password="ne-tas")

    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    assert 1 <= int(blocked.headers["Retry-After"]) <= auth_routes._RATE_LIMIT_WINDOW + 1


def test_the_lockout_holds_even_with_the_right_password(client, make_user):
    user = make_user()
    for _ in range(auth_routes._RATE_LIMIT_MAX):
        _login(client, ip="10.0.0.2", username=user["username"], password="ne-tas")

    assert _login(client, ip="10.0.0.2", username=user["username"],
                  password=user["password"]).status_code == 429


def test_the_account_bucket_ignores_the_case_and_padding_of_the_identifier(client, make_user):
    user = make_user(username="jonas")

    for spelling in ("jonas", "JONAS", " Jonas ", "jOnAs", "JoNaS"):
        for _ in range(2):
            _login(client, ip="10.0.0.3", username=spelling, password="ne-tas")

    assert _login(client, ip="10.0.0.3", username="jonas",
                  password=user["password"]).status_code == 429


def test_the_lockout_lifts_once_the_window_passes(client, make_user):
    user = make_user()
    for _ in range(auth_routes._RATE_LIMIT_MAX):
        _login(client, ip="10.0.0.4", username=user["username"], password="ne-tas")
    assert _login(client, ip="10.0.0.4", username=user["username"],
                  password=user["password"]).status_code == 429

    _age_rate_limit_window()

    assert _login(client, ip="10.0.0.4", username=user["username"],
                  password=user["password"]).status_code == 200


def test_successful_sign_ins_never_fill_the_bucket(client, make_user):
    # Ten good logins used to lock the account out — only failures
    # may spend budget
    user = make_user()

    for _ in range(auth_routes._RATE_LIMIT_MAX + 1):
        assert _login(client, ip="10.0.0.5", username=user["username"],
                      password=user["password"]).status_code == 200


def test_a_successful_login_leaves_no_rate_limit_footprint(client, make_user):
    user = make_user(username="ramune")

    _login(client, ip="10.0.0.6", username=user["username"], password=user["password"])

    assert "login:10.0.0.6" not in auth_routes._rate_limit_store
    assert "login:id:ramune" not in auth_routes._rate_limit_store


def test_a_failed_login_fills_both_the_ip_and_the_account_bucket(client, make_user):
    user = make_user(username="gintare")

    _login(client, ip="10.0.0.7", username="GINTARE", password="ne-tas")

    assert len(auth_routes._rate_limit_store["login:10.0.0.7"]) == 1
    assert len(auth_routes._rate_limit_store["login:id:gintare"]) == 1


def test_a_flood_from_one_ip_is_refused_before_the_body_is_read(client):
    for _ in range(auth_routes._LOGIN_IP_MAX):
        auth_routes._record_attempt("login:10.0.0.8")

    blocked = client.post("/api/auth/login", json={}, environ_base={"REMOTE_ADDR": "10.0.0.8"})

    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    assert blocked.headers["Retry-After"]


def test_the_ip_budget_is_three_times_the_account_budget(client, make_user):
    # A NATed campus shares one address: the per-IP bucket must
    # outlast the per-account one
    user = make_user()
    for _ in range(auth_routes._RATE_LIMIT_MAX):
        _login(client, ip="10.0.0.9", username=user["username"], password="ne-tas")

    other = make_user()
    assert _login(client, ip="10.0.0.9", username=other["username"],
                  password=other["password"]).status_code == 200


def test_the_retry_after_header_counts_down_the_window(client, make_user):
    user = make_user()
    for _ in range(auth_routes._RATE_LIMIT_MAX):
        _login(client, ip="10.0.0.10", username=user["username"], password="ne-tas")

    _age_rate_limit_window(100)
    blocked = _login(client, ip="10.0.0.10", username=user["username"], password="ne-tas")

    assert blocked.status_code == 429
    assert 190 <= int(blocked.headers["Retry-After"]) <= 205


def test_the_house_429_never_asks_a_client_to_retry_in_zero_seconds(app):
    with app.test_request_context("/api/auth/login"):
        response, status = auth_routes._rate_limited_response("per daug", "nera:rakto")

    assert status == 429
    assert response.headers["Retry-After"] == "1"


def test_the_limiter_store_never_grows_past_its_lru_ceiling():
    for index in range(auth_routes._RATE_LIMIT_MAX_KEYS + 200):
        auth_routes._check_rate_limit(f"spoofed:{index}")

    assert len(auth_routes._rate_limit_store) <= auth_routes._RATE_LIMIT_MAX_KEYS
    # The oldest keys go first, the newest survive
    assert "spoofed:0" not in auth_routes._rate_limit_store
    assert f"spoofed:{auth_routes._RATE_LIMIT_MAX_KEYS + 199}" in auth_routes._rate_limit_store


def test_recording_a_failure_obeys_the_same_lru_ceiling(client, make_user):
    # The other half of the limiter writes to the same store; a
    # spoofed-IP flood must not grow it through this door either
    for index in range(auth_routes._RATE_LIMIT_MAX_KEYS):
        auth_routes._check_rate_limit(f"spoofed:{index}")

    auth_routes._record_attempt("login:id:paskutinis")

    assert len(auth_routes._rate_limit_store) <= auth_routes._RATE_LIMIT_MAX_KEYS
    assert "login:id:paskutinis" in auth_routes._rate_limit_store
    assert "spoofed:0" not in auth_routes._rate_limit_store




# -----------------------------------------------------------
# The gate decorators the other blueprints import
# -----------------------------------------------------------

def test_require_role_refuses_an_anonymous_caller(app, client):
    _mount(app, "/probe/admins", auth_routes.require_role("admin")(lambda: jsonify({"ok": True})))

    response = client.get("/probe/admins")

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_require_role_refuses_a_role_that_is_not_listed(app, client, make_user, auth_headers):
    _mount(app, "/probe/admins", auth_routes.require_role("admin", "curator")(lambda: jsonify({"ok": True})))
    student = make_user(role="student")

    response = client.get("/probe/admins", headers=auth_headers(student))

    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"


def test_require_role_lets_a_listed_role_through_and_hands_over_the_user(app, client, make_user, auth_headers):
    _mount(app, "/probe/admins",
           auth_routes.require_role("admin", "curator")(lambda: jsonify({"role": request.user["role"]})))
    curator = make_user(role="curator")

    response = client.get("/probe/admins", headers=auth_headers(curator))

    assert response.status_code == 200
    assert response.get_json()["role"] == "curator"


def test_require_auth_hands_the_resolved_user_to_the_handler(app, client, make_user, auth_headers):
    _mount(app, "/probe/self", auth_routes.require_auth(lambda: jsonify({"id": request.user["id"]})))
    user = make_user()

    response = client.get("/probe/self", headers=auth_headers(user))

    assert response.get_json()["id"] == user["id"]


def test_the_shared_rate_limit_decorator_keys_signed_in_callers_by_user(app, client, make_user, auth_headers):
    view = auth_routes.require_auth(auth_routes.rate_limit("probe", max_attempts=2)(lambda: jsonify({"ok": True})))
    _mount(app, "/probe/quota", view)
    one = make_user()
    two = make_user()
    headers_one = auth_headers(one)
    headers_two = auth_headers(two)

    assert client.post("/probe/quota", headers=headers_one).status_code == 200
    assert client.post("/probe/quota", headers=headers_one).status_code == 200
    blocked = client.post("/probe/quota", headers=headers_one)

    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    # A different signed-in caller has their own budget
    assert client.post("/probe/quota", headers=headers_two).status_code == 200


def test_the_shared_rate_limit_decorator_keys_anonymous_callers_by_ip(app, client):
    _mount(app, "/probe/anon", auth_routes.rate_limit("probeanon", max_attempts=2)(lambda: jsonify({"ok": True})))

    for _ in range(2):
        assert client.post("/probe/anon", environ_base={"REMOTE_ADDR": "10.1.0.1"}).status_code == 200
    assert client.post("/probe/anon", environ_base={"REMOTE_ADDR": "10.1.0.1"}).status_code == 429
    # Another address is a different bucket
    assert client.post("/probe/anon", environ_base={"REMOTE_ADDR": "10.1.0.2"}).status_code == 200
