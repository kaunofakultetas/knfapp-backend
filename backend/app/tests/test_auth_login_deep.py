# -----------------------------------------------------------
#  [*] Tests — login / logout / logout-all, the exhaustive pass
#
#  The gap-closing companion to test_auth_login.py. That module
#  proves the documented behaviour of the credential routes;
#  this one walks the four functions it owns branch by branch —
#  app/auth/routes.py: login, logout, logout_all and
#  _disconnect_user_sockets — and pins the arms, guards and
#  boundaries a happy-path suite never reaches.
#
#  What this module proves:
#
#    - login's identifier resolution in full: the "username or
#      email" fallback with FALSY non-string keys, whitespace-
#      only and 10 000-character identifiers, an identifier
#      that is one account's username and another's email, two
#      case-variant rows sharing one password, and the ASCII-
#      only reach of SQLite's NOCASE (a Lithuanian address does
#      NOT fold)
#    - the password boundary bcrypt actually enforces: 72 bytes
#      of truncation, so a longer guess signs in on its prefix —
#      which is exactly why registration caps at 72 — and the
#      NUL bytes the app strips before bcrypt ever sees them
#    - the session cap as an ORDERING, not a count: ties break
#      by id, expired rows are trimmed first, and a login can
#      evict the very session it just minted
#    - the two failure buckets at their edges: the 10th failure
#      is still a 401, a rejected attempt does not extend its
#      own lockout, malformed bodies and deactivated accounts
#      fill nothing, and the window lifts at 300 seconds — not
#      at 299
#    - logout's optional push cleanup for every wrong shape,
#      and that the socket kick runs AFTER the commit, kicks
#      every device of the user, and cannot fail the route it
#      rides on — import missing, helper missing, helper
#      exploding
#    - logout-all as the revoke-everywhere switch: it ignores
#      its body, takes expired rows with it, and leaves the
#      account able to sign in again at once
#
#  No wall-clock sleeping: the limiter's monotonic stamps are
#  rewritten by _age_rate_limit_window, and expiry is arranged
#  by seeding the stamp rather than travelling to it.
#
#  Wire care (TESTPLAN rule 10): a `json=` kwarg is serialised
#  through the app's html-escaping JSON provider, so every test
#  whose point is the exact bytes of a password posts raw.
# -----------------------------------------------------------

import json
import sqlite3
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest

from app.auth import routes as auth_routes




# -----------------------------------------------------------
# _clean_rate_limit_store
# -----------------------------------------------------------
#
# The limiter is a module-level dict that outlives the `app`
# fixture's fresh database, so a sibling module's failed logins
# would otherwise leak in — and this module's deliberate floods
# would leak out.
#
# Used by:
#   - every test here (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limit_store():
    auth_routes._rate_limit_store.clear()
    yield
    auth_routes._rate_limit_store.clear()




# -----------------------------------------------------------
# _login / _login_raw / _bearer / _me / _token
# -----------------------------------------------------------
#
# _login posts a body the ordinary way (fine for plain ASCII);
# _login_raw puts EXACTLY the given bytes on the wire, which is
# the only way to test a password containing characters the
# app's JSON provider would escape on the way out.
#
# Used by:
#   - most tests below
# -----------------------------------------------------------

def _login(client, ip=None, **payload):
    kwargs = {"json": payload}
    if ip is not None:
        kwargs["environ_base"] = {"REMOTE_ADDR": ip}
    return client.post("/api/auth/login", **kwargs)


def _login_raw(client, payload):
    return client.post("/api/auth/login", data=json.dumps(payload),
                       content_type="application/json")


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _me(client, token):
    return client.get("/api/auth/me", headers=_bearer(token))


def _token(client, user):
    return _login(client, username=user["username"], password=user["password"]).get_json()["token"]




# -----------------------------------------------------------
# _insert_user
# -----------------------------------------------------------
#
# The shared make_user fixture derives the email from the
# username and always writes a live bcrypt hash. Several tests
# here need a row make_user cannot express: a chosen email (so
# one identifier can match two different accounts), an `active`
# value other than 0/1, or a password_hash that is not a bcrypt
# hash at all.
#
# Used by:
#   - the identifier-resolution and password-boundary tests
# -----------------------------------------------------------

def _insert_user(app, username, password, email=None, role="student",
                 active=1, display_name=None, password_hash=None):
    user_id = str(uuid.uuid4())
    email = email if email is not None else f"{username}@knf.vu.lt"
    stored = password_hash if password_hash is not None else \
        bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "INSERT INTO users (id, username, email, display_name, password_hash, role, active, invited)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (user_id, username, email, display_name or username, stored, role, active),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": user_id, "username": username, "password": password,
            "email": email, "role": role}




# -----------------------------------------------------------
# _session_rows / _push_rows / _seed_push_token / _seed_session
# -----------------------------------------------------------
#
# _seed_session writes a sessions row with stamps of the
# caller's choosing — the only way to arrange the cap's
# ORDERING (equal stamps, a longer expiry, an already-expired
# row) without travelling in time.
#
# Used by:
#   - the session-cap, logout and logout-all tests
# -----------------------------------------------------------

def _session_rows(db, user_id):
    return db.execute(
        "SELECT id, token, created_at, expires_at FROM sessions WHERE user_id = ? ORDER BY id",
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


def _seed_session(db, user_id, session_id, expires_at, created_at="2026-01-01T00:00:00+00:00"):
    db.execute(
        "INSERT INTO sessions (id, user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (session_id, user_id, f"hash-{session_id}", created_at, expires_at),
    )
    db.commit()


def _in_days(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()




# -----------------------------------------------------------
# _bucket / _fill_bucket / _age_rate_limit_window
# -----------------------------------------------------------
#
# The limiter keys on time.monotonic, which time_machine
# deliberately leaves alone, so a window is aged by rewriting
# the recorded stamps — what wall-clock passage would do,
# without sleeping. _fill_bucket spends a budget through the
# module's own recording primitive instead of paying for N real
# bcrypt logins.
#
# Used by:
#   - the rate-limit tests
# -----------------------------------------------------------

def _bucket(key):
    return auth_routes._rate_limit_store.get(key, [])


def _fill_bucket(key, count):
    for _ in range(count):
        auth_routes._record_attempt(key)


def _age_rate_limit_window(seconds=None):
    shift = auth_routes._RATE_LIMIT_WINDOW + 1 if seconds is None else seconds
    with auth_routes._rate_limit_lock:
        for key in list(auth_routes._rate_limit_store):
            auth_routes._rate_limit_store[key] = [
                stamp - shift for stamp in auth_routes._rate_limit_store[key]
            ]




# -----------------------------------------------------------
# Login — resolving the identifier
# -----------------------------------------------------------

def test_a_login_with_no_client_address_falls_back_to_the_unknown_ip_bucket(client, make_user):
    # remote_addr is None behind a misconfigured proxy; the key
    # must still be a string or the limiter itself would blow up
    user = make_user()

    response = client.post("/api/auth/login",
                           json={"username": user["username"], "password": "blogas"},
                           environ_base={"REMOTE_ADDR": None})

    assert response.status_code == 401
    assert len(_bucket("login:unknown")) == 1


def test_an_addressless_flood_can_still_be_locked_out(client, make_user):
    user = make_user()
    _fill_bucket("login:unknown", auth_routes._LOGIN_IP_MAX)

    response = client.post("/api/auth/login",
                           json={"username": user["username"], "password": user["password"]},
                           environ_base={"REMOTE_ADDR": None})

    assert response.status_code == 429


def test_a_truthy_non_string_username_is_refused_before_the_email_key_is_read(client, make_user):
    # 12345 is truthy, so it WINS the fallback and then fails the
    # type gate — the email beside it is never consulted
    user = make_user()

    response = _login(client, username=12345, email=user["email"], password=user["password"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "Username/email and password must be strings"


def test_a_falsy_non_string_username_falls_through_to_the_email_key(client, make_user):
    # 0 is falsy, so `data.get("username") or data.get("email")`
    # hands the email through and the sign-in succeeds
    user = make_user()

    response = _login(client, username=0, email=user["email"], password=user["password"])

    assert response.status_code == 200
    assert response.get_json()["user"]["id"] == user["id"]


def test_a_password_that_is_a_truthy_number_is_a_type_error_not_a_missing_field(client, make_user):
    user = make_user()

    response = _login(client, username=user["username"], password=1234)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Username/email and password must be strings"


def test_a_password_that_is_a_falsy_number_is_reported_as_missing(client, make_user):
    user = make_user()

    response = _login(client, username=user["username"], password=0)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Username/email and password required"


def test_a_whitespace_only_identifier_finds_nobody(client, make_user):
    make_user(username="tomas")

    response = _login(client, username="   ", password="slaptazodis123")

    assert response.status_code == 401
    assert response.get_json()["code"] == "invalid_credentials"


def test_every_whitespace_only_identifier_shares_one_failure_bucket(client):
    # The bucket key is the identifier stripped and lowercased, so
    # blanks of every width collapse onto the same empty key
    _login(client, username="   ", password="slaptazodis123")
    _login(client, username=chr(9) + chr(9), password="slaptazodis123")

    assert len(_bucket("login:id:")) == 2


def test_an_identifier_matching_one_accounts_username_and_anothers_email_is_decided_by_the_password(client, app):
    # Legacy rows only — the username charset blocks '@' now, but
    # the OR still reaches both columns, and both rows must stay
    # reachable by their own password
    by_name = _insert_user(app, "jonas.k", "pirmas-slaptazodis")
    by_mail = _insert_user(app, "kitas.vardas", "antras-slaptazodis", email="jonas.k")

    first = _login(client, username="jonas.k", password="pirmas-slaptazodis")
    second = _login(client, username="jonas.k", password="antras-slaptazodis")

    assert first.get_json()["user"]["id"] == by_name["id"]
    assert second.get_json()["user"]["id"] == by_mail["id"]


def test_the_first_matching_row_wins_when_two_case_variants_share_a_password(client, app):
    # Nothing distinguishes the candidates, so the scan order does:
    # the older row answers and the newer account is unreachable
    # under that identifier
    older = _insert_user(app, "Tomas", "bendras-slaptazodis", email="tomas1@knf.vu.lt")
    newer = _insert_user(app, "tomas", "bendras-slaptazodis", email="tomas2@knf.vu.lt")

    response = _login(client, username="TOMAS", password="bendras-slaptazodis")

    assert response.get_json()["user"]["id"] == older["id"]
    assert response.get_json()["user"]["id"] != newer["id"]


def test_the_nocase_match_folds_ascii_only(client, app):
    # SQLite's NOCASE is ASCII — a Lithuanian address signs in as
    # typed and NOT in upper case, unlike an ASCII one
    user = _insert_user(app, "azuolas", "slaptazodis123", email="ąžuolas@knf.vu.lt")

    folded = _login(client, email="ĄŽUOLAS@knf.vu.lt", password=user["password"])
    exact = _login(client, email="ąžuolas@knf.vu.lt", password=user["password"])

    assert folded.status_code == 401
    assert exact.status_code == 200


def test_a_ten_thousand_character_identifier_is_refused_without_a_crash(client, make_user):
    make_user()
    identifier = "a" * 10000

    response = _login(client, username=identifier, password="slaptazodis123")

    assert response.status_code == 401
    assert len(_bucket(f"login:id:{identifier}")) == 1


def test_an_injection_shaped_identifier_is_just_an_unknown_account(client, db, make_user):
    user = make_user()

    response = _login(client, username="' OR 1=1 --", password=user["password"])

    assert response.status_code == 401
    assert db.execute("SELECT count(*) c FROM users").fetchone()["c"] == 2


@pytest.mark.parametrize("role", auth_routes.ROLES)
def test_every_role_can_sign_in_out_and_out_everywhere(client, db, app, role):
    user = _insert_user(app, f"{role}.vartotojas", "slaptazodis123", role=role)

    body = _login(client, username=user["username"], password=user["password"]).get_json()
    assert body["user"]["role"] == role

    assert client.post("/api/auth/logout", headers=_bearer(body["token"])).status_code == 200

    second = _login(client, username=user["username"], password=user["password"]).get_json()
    assert client.post("/api/auth/logout-all", headers=_bearer(second["token"])).status_code == 200
    assert _session_rows(db, user["id"]) == []


def test_any_non_zero_active_value_signs_in(client, app):
    # The guard is `if not user["active"]`, not `active == 1`
    user = _insert_user(app, "keistas", "slaptazodis123", active=2)

    assert _login(client, username=user["username"], password=user["password"]).status_code == 200


def test_a_negative_active_value_signs_in_too(client, app):
    user = _insert_user(app, "neigiamas", "slaptazodis123", active=-1)

    assert _login(client, username=user["username"], password=user["password"]).status_code == 200




# -----------------------------------------------------------
# Login — the exact bytes bcrypt sees
# -----------------------------------------------------------

def test_a_password_longer_than_seventy_two_bytes_matches_its_first_seventy_two(client, app):
    # bcrypt truncates at 72 bytes — the reason registration caps
    # there. A longer guess sharing the prefix DOES sign in
    user = _insert_user(app, "ilgas", "a" * 72)

    response = _login_raw(client, {"username": user["username"], "password": "a" * 72 + "b" * 40})

    assert response.status_code == 200


def test_a_seventy_two_byte_password_differing_in_its_last_byte_is_refused(client, app):
    user = _insert_user(app, "ribinis", "a" * 72)

    response = _login_raw(client, {"username": user["username"], "password": "a" * 71 + "b"})

    assert response.status_code == 401


def test_a_multibyte_password_is_measured_in_bytes_not_characters(client, app):
    # 36 Lithuanian characters are 72 bytes — the cap's real edge
    _insert_user(app, "lietuviskas", "ą" * 36)

    assert _login_raw(client, {"username": "lietuviskas", "password": "ą" * 36}).status_code == 200
    assert _login_raw(client, {"username": "lietuviskas", "password": "ą" * 35}).status_code == 401


def test_a_nul_byte_in_the_password_is_stripped_before_bcrypt(client, make_user):
    # validate_json_input NUL-strips every string in the body, so
    # two different passwords collapse onto one at the bcrypt call
    user = make_user()
    laced = "slaptazodis" + chr(0) + "123"

    response = _login_raw(client, {"username": user["username"], "password": laced})

    assert response.status_code == 200


def test_a_password_of_html_characters_matches_the_bytes_on_the_wire(client, app):
    # Posted raw, the account's own password authenticates; posted
    # through the escaping serialiser the same literal does not —
    # TESTPLAN rule 10 made visible
    secret = "sla<pta>&" + chr(34) + "zo'dis"
    user = _insert_user(app, "entitetas", secret)

    raw = _login_raw(client, {"username": user["username"], "password": secret})
    escaped = _login(client, username=user["username"], password=secret)

    assert raw.status_code == 200
    assert escaped.status_code == 401


def test_the_login_response_escapes_the_display_name_it_returns(client, app):
    # Raw in the database, escaped on output — the mobile client
    # decodes entities on every response
    user = _insert_user(app, "zymetas", "slaptazodis123", display_name="Jonas <b>Bold</b>")

    body = _login(client, username=user["username"], password=user["password"]).get_json()

    assert body["user"]["displayName"] == "Jonas &lt;b&gt;Bold&lt;/b&gt;"


def test_a_corrupted_password_hash_is_refused_instead_of_crashing(client, app):
    _insert_user(app, "sugadintas", "slaptazodis123", password_hash="ne-bcrypt-reiksme")

    response = _login(client, username="sugadintas", password="slaptazodis123")

    assert response.status_code == 401




# -----------------------------------------------------------
# Login — bodies the route never gets to see
# -----------------------------------------------------------

def test_a_form_encoded_login_is_refused_as_a_missing_json_body(client, make_user):
    user = make_user()

    response = client.post("/api/auth/login",
                           data={"username": user["username"], "password": user["password"]})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_json_null_body_is_refused(client):
    response = client.post("/api/auth/login", data="null", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_json_string_body_is_stopped_by_the_object_guard_before_the_route(client):
    response = client.post("/api/auth/login", data='"labas"', content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_getting_the_login_route_is_a_method_not_allowed(client):
    response = client.get("/api/auth/login")

    assert response.status_code == 405
    assert response.get_json() == {"error": "Method not allowed"}




# -----------------------------------------------------------
# Login — the session it mints, and the cap that trims it
# -----------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {},
    {"username": "kazkas"},
    {"password": "slaptazodis123"},
    {"username": 5, "password": "slaptazodis123"},
])
def test_no_refused_body_shape_ever_mints_a_session(client, db, make_user, payload):
    make_user()

    response = client.post("/api/auth/login", json=payload)

    assert response.status_code == 400
    assert db.execute("SELECT count(*) c FROM sessions").fetchone()["c"] == 0


def test_a_wrong_password_and_a_deactivated_account_both_mint_nothing(client, db, make_user):
    live = make_user()
    dead = make_user(active=0)

    _login(client, username=live["username"], password="blogas")
    _login(client, username=dead["username"], password=dead["password"])

    assert db.execute("SELECT count(*) c FROM sessions").fetchone()["c"] == 0


def test_a_login_leaves_the_users_push_tokens_registered(client, db, make_user):
    # Only logout-all and the expiry purge clear push rows
    user = make_user()
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    assert _login(client, username=user["username"], password=user["password"]).status_code == 200

    assert _push_rows(db, user["id"]) == ["ExponentPushToken[telefonas]"]


def test_the_token_bearing_login_response_is_never_cached(client, make_user):
    # A bearer token sitting in a shared cache is the whole
    # session; /api/ answers carry no-store
    user = make_user()

    response = _login(client, username=user["username"], password=user["password"])

    assert response.headers["Cache-Control"] == "no-store"


def test_presenting_a_live_token_to_login_still_mints_a_new_session(client, db, make_user, auth_headers):
    # login ignores the Authorization header entirely — it is the
    # one route that authenticates by password, never by session
    user = make_user()
    headers = auth_headers(user)

    response = client.post("/api/auth/login", headers=headers,
                           json={"username": user["username"], "password": user["password"]})

    assert response.status_code == 200
    assert len(_session_rows(db, user["id"])) == 2


def test_the_session_cap_keeps_the_ten_highest_ids_when_the_stamps_tie(client, db, make_user):
    # expires_at and created_at are identical across the seeded
    # rows, so the id DESC tie-break alone decides the survivors
    user = make_user()
    far = _in_days(60)
    for i in range(12):
        _seed_session(db, user["id"], f"s{i:02d}", far)

    assert _login(client, username=user["username"], password=user["password"]).status_code == 200

    assert [row["id"] for row in _session_rows(db, user["id"])] == [f"s{i:02d}" for i in range(2, 12)]


def test_the_cap_can_evict_the_session_the_login_just_minted(client, db, make_user):
    # Ten sessions that outlive the fresh one sort ahead of it, so
    # the trim deletes the row the very same request created and
    # the token handed to the client is dead on arrival
    user = make_user()
    far = _in_days(60)
    for i in range(10):
        _seed_session(db, user["id"], f"s{i:02d}", far)

    body = _login(client, username=user["username"], password=user["password"]).get_json()

    assert len(_session_rows(db, user["id"])) == 10
    assert _me(client, body["token"]).status_code == 401


def test_an_expired_session_is_the_first_the_cap_trims(client, db, make_user):
    # expires_at DESC puts the dead rows last, so the fresh session
    # always survives a full table
    user = make_user()
    past = _in_days(-5)
    for i in range(10):
        _seed_session(db, user["id"], f"s{i:02d}", past)

    body = _login(client, username=user["username"], password=user["password"]).get_json()

    assert len(_session_rows(db, user["id"])) == 10
    assert _me(client, body["token"]).status_code == 200


def test_every_token_still_authenticates_at_exactly_the_session_cap(client, make_user):
    user = make_user()

    tokens = [_token(client, user) for _ in range(auth_routes._SESSIONS_PER_USER)]

    assert all(_me(client, token).status_code == 200 for token in tokens)


def test_one_login_past_the_cap_kills_exactly_the_oldest_token(client, db, make_user):
    user = make_user()
    tokens = [_token(client, user) for _ in range(auth_routes._SESSIONS_PER_USER)]

    newest = _token(client, user)

    assert _me(client, tokens[0]).status_code == 401
    assert all(_me(client, token).status_code == 200 for token in tokens[1:])
    assert _me(client, newest).status_code == 200
    assert len(_session_rows(db, user["id"])) == auth_routes._SESSIONS_PER_USER


def test_the_cap_counts_only_the_signing_in_users_rows(client, db, make_user):
    user = make_user()
    neighbour = make_user()
    far = _in_days(60)
    for i in range(12):
        _seed_session(db, neighbour["id"], f"n{i:02d}", far)

    _login(client, username=user["username"], password=user["password"])

    assert len(_session_rows(db, neighbour["id"])) == 12




# -----------------------------------------------------------
# Login — the two failure buckets at their edges
# -----------------------------------------------------------

def test_the_tenth_failure_is_still_a_401_and_the_eleventh_is_a_429(client, make_user):
    user = make_user()
    _fill_bucket(f"login:id:{user['username']}", auth_routes._RATE_LIMIT_MAX - 1)

    tenth = _login(client, username=user["username"], password="blogas")
    eleventh = _login(client, username=user["username"], password="blogas")

    assert tenth.status_code == 401
    assert eleventh.status_code == 429
    assert eleventh.get_json()["code"] == "rate_limited"


def test_a_rejected_login_does_not_extend_its_own_lockout(client, make_user):
    # The probe is record=False, so hammering a locked bucket can
    # never push the window forward and keep the caller out longer
    user = make_user()
    key = f"login:id:{user['username']}"
    _fill_bucket(key, auth_routes._RATE_LIMIT_MAX)

    for _ in range(3):
        assert _login(client, username=user["username"], password="blogas").status_code == 429

    assert len(_bucket(key)) == auth_routes._RATE_LIMIT_MAX


def test_the_username_and_the_email_of_one_account_have_separate_budgets(client, make_user):
    # The bucket keys the TYPED identifier, not the resolved
    # account — a lockout on one spelling leaves the other open
    user = make_user()
    _fill_bucket(f"login:id:{user['username']}", auth_routes._RATE_LIMIT_MAX)

    by_name = _login(client, username=user["username"], password="blogas")
    by_mail = _login(client, email=user["email"], password="blogas")

    assert by_name.status_code == 429
    assert by_mail.status_code == 401


def test_a_malformed_body_never_touches_either_bucket(client, make_user):
    user = make_user()

    for _ in range(15):
        assert client.post("/api/auth/login", json={"username": user["username"]}).status_code == 400

    assert _bucket("login:127.0.0.1") == []
    assert not [key for key in auth_routes._rate_limit_store if key.startswith("login:id:")]


def test_a_deactivated_account_never_fills_a_bucket(client, make_user):
    # The 403 returns before _record_attempt, so the holder of a
    # disabled account is never additionally locked out
    user = make_user(active=0)

    for _ in range(12):
        assert _login(client, username=user["username"], password=user["password"]).status_code == 403

    assert _bucket(f"login:id:{user['username']}") == []
    assert _bucket("login:127.0.0.1") == []


def test_a_successful_login_leaves_both_buckets_empty(client, make_user):
    user = make_user()

    assert _login(client, username=user["username"], password=user["password"]).status_code == 200

    assert _bucket(f"login:id:{user['username']}") == []
    assert _bucket("login:127.0.0.1") == []


def test_a_flood_from_one_address_locks_the_address_and_not_the_accounts(client, make_user):
    # The per-IP budget is spent by failures against OTHER names,
    # so the honest owner is refused from that address and let in
    # from any other
    victim = make_user()
    _fill_bucket("login:10.0.0.9", auth_routes._LOGIN_IP_MAX)

    blocked = _login(client, ip="10.0.0.9", username=victim["username"], password=victim["password"])
    elsewhere = _login(client, ip="10.0.0.10", username=victim["username"], password=victim["password"])

    assert blocked.status_code == 429
    assert elsewhere.status_code == 200


def test_the_lockout_lifts_at_the_window_edge_and_not_a_second_earlier(client, make_user):
    user = make_user()
    key = f"login:id:{user['username']}"
    _fill_bucket(key, auth_routes._RATE_LIMIT_MAX)

    _age_rate_limit_window(auth_routes._RATE_LIMIT_WINDOW - 1)
    still_locked = _login(client, username=user["username"], password=user["password"])

    _age_rate_limit_window(1)
    reopened = _login(client, username=user["username"], password=user["password"])

    assert still_locked.status_code == 429
    assert reopened.status_code == 200


def test_the_identifier_429_carries_the_house_shape_and_a_retry_after(client, make_user):
    user = make_user()
    _fill_bucket(f"login:id:{user['username']}", auth_routes._RATE_LIMIT_MAX)

    response = _login(client, username=user["username"], password="blogas")

    assert response.status_code == 429
    assert set(response.get_json()) == {"error", "code"}
    assert response.get_json()["code"] == "rate_limited"
    assert 1 <= int(response.headers["Retry-After"]) <= auth_routes._RATE_LIMIT_WINDOW + 1


def test_a_rate_limited_login_mints_nothing_and_never_reads_the_body(client, db, make_user):
    make_user()
    _fill_bucket("login:127.0.0.1", auth_routes._LOGIN_IP_MAX)

    response = client.post("/api/auth/login", data="ne-json", content_type="application/json")

    assert response.status_code == 429
    assert db.execute("SELECT count(*) c FROM sessions").fetchone()["c"] == 0




# -----------------------------------------------------------
# Logout — the optional push cleanup
# -----------------------------------------------------------

def test_logout_ignores_a_snake_case_push_token_key(client, db, actor):
    # Only the camelCase key the mobile client sends is honoured
    user, headers = actor
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    response = client.post("/api/auth/logout", headers=headers,
                           json={"push_token": "ExponentPushToken[telefonas]"})

    assert response.status_code == 200
    assert _push_rows(db, user["id"]) == ["ExponentPushToken[telefonas]"]


@pytest.mark.parametrize("value", [True, ["ExponentPushToken[a]"], {"token": "a"}, 0, 1.5, None])
def test_logout_ignores_a_push_token_of_the_wrong_type(client, db, make_user, auth_headers, value):
    user = make_user()
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    response = client.post("/api/auth/logout", headers=auth_headers(user), json={"pushToken": value})

    assert response.status_code == 200
    assert _push_rows(db, user["id"]) == ["ExponentPushToken[telefonas]"]


def test_logout_ignores_an_empty_push_token(client, db, actor):
    # A string of the right type but no length — the second half of
    # `isinstance(push_token, str) and push_token`
    user, headers = actor
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    response = client.post("/api/auth/logout", headers=headers, json={"pushToken": ""})

    assert response.status_code == 200
    assert _push_rows(db, user["id"]) == ["ExponentPushToken[telefonas]"]


def test_logout_does_not_trim_a_padded_push_token(client, db, actor):
    user, headers = actor
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    response = client.post("/api/auth/logout", headers=headers,
                           json={"pushToken": " ExponentPushToken[telefonas] "})

    assert response.status_code == 200
    assert _push_rows(db, user["id"]) == ["ExponentPushToken[telefonas]"]


def test_logout_with_an_unregistered_push_token_is_a_no_op(client, db, actor):
    user, headers = actor
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    response = client.post("/api/auth/logout", headers=headers,
                           json={"pushToken": "ExponentPushToken[niekada-nebuvo]"})

    assert response.status_code == 200
    assert _push_rows(db, user["id"]) == ["ExponentPushToken[telefonas]"]


def test_a_json_null_logout_body_is_tolerated(client, db, actor):
    user, headers = actor

    response = client.post("/api/auth/logout", headers=headers,
                           data="null", content_type="application/json")

    assert response.status_code == 200
    assert _session_rows(db, user["id"]) == []


def test_an_empty_json_object_logout_body_is_tolerated(client, db, actor):
    user, headers = actor

    response = client.post("/api/auth/logout", headers=headers, json={})

    assert response.status_code == 200
    assert _session_rows(db, user["id"]) == []


def test_a_top_level_array_logout_body_is_refused_before_the_route_runs(client, db, actor):
    # The route itself would tolerate it; the app-wide object guard
    # does not — and because that guard runs first, the session
    # SURVIVES a logout the client believes it made
    user, headers = actor

    response = client.post("/api/auth/logout", headers=headers, json=[1, 2])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"
    assert len(_session_rows(db, user["id"])) == 1


def test_logout_answers_exactly_the_documented_body(client, actor):
    _, headers = actor

    response = client.post("/api/auth/logout", headers=headers)

    assert response.get_json() == {"message": "Logged out"}


def test_logout_is_a_401_once_another_device_revoked_everything(client, make_user, auth_headers):
    # The idempotency path a client actually hits: logout-all on
    # the tablet, then the phone's own logout arrives late
    user = make_user()
    phone = auth_headers(user)
    tablet = auth_headers(user)

    assert client.post("/api/auth/logout-all", headers=tablet).status_code == 200
    assert client.post("/api/auth/logout", headers=phone).status_code == 401


def test_logout_ignores_the_keys_it_does_not_know(client, db, actor):
    user, headers = actor

    response = client.post("/api/auth/logout", headers=headers,
                           json={"deviceId": "abc", "reason": "user tapped sign out"})

    assert response.status_code == 200
    assert _session_rows(db, user["id"]) == []


def test_getting_the_logout_route_is_a_method_not_allowed(client):
    assert client.get("/api/auth/logout").status_code == 405




# -----------------------------------------------------------
# Logout — what it must NOT touch
# -----------------------------------------------------------

def test_logout_leaves_the_account_itself_untouched(client, db, actor):
    user, headers = actor

    client.post("/api/auth/logout", headers=headers)

    row = db.execute("SELECT active, role FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert row["active"] == 1
    assert row["role"] == "student"


def test_logout_from_one_device_leaves_the_other_devices_push_rows(client, db, make_user, auth_headers):
    user = make_user()
    phone = auth_headers(user)
    auth_headers(user)
    _seed_push_token(db, user["id"], "ExponentPushToken[plansete]")

    client.post("/api/auth/logout", headers=phone, json={"pushToken": "ExponentPushToken[telefonas]"})

    assert _push_rows(db, user["id"]) == ["ExponentPushToken[plansete]"]




# -----------------------------------------------------------
# _session_count_seen_by
# -----------------------------------------------------------
#
# A socket-kick stand-in that opens its OWN connection and
# records how many session rows are visible from outside the
# route's transaction — the only way to prove the kick happens
# after the commit and not before it.
#
# Used by:
#   - the two ordering tests below
# -----------------------------------------------------------

def _session_count_seen_by(app, seen):

    def spy(user_id):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            seen.append(conn.execute("SELECT count(*) FROM sessions WHERE user_id = ?",
                                     (user_id,)).fetchone()[0])
        finally:
            conn.close()

    return spy


def test_the_sockets_are_kicked_only_after_the_session_row_is_gone(client, app, actor, monkeypatch):
    from app.chat import events as chat_events
    user, headers = actor
    seen = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _session_count_seen_by(app, seen))

    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert seen == [0]


def test_logout_all_kicks_the_sockets_only_after_every_row_is_gone(client, app, make_user,
                                                                   auth_headers, monkeypatch):
    from app.chat import events as chat_events
    user = make_user()
    headers = auth_headers(user)
    auth_headers(user)
    seen = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _session_count_seen_by(app, seen))

    assert client.post("/api/auth/logout-all", headers=headers).status_code == 200

    assert seen == [0]


def test_logout_kicks_every_socket_of_the_user_even_though_other_sessions_survive(client, make_user,
                                                                                 auth_headers,
                                                                                 monkeypatch):
    # Deliberate: the kick is per USER, not per session. A device
    # whose session is still valid simply reconnects
    from app.chat import events as chat_events
    user = make_user()
    phone = auth_headers(user)
    tablet = auth_headers(user)
    kicked = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", kicked.append)

    client.post("/api/auth/logout", headers=phone)

    assert kicked == [user["id"]]
    assert client.get("/api/auth/me", headers=tablet).status_code == 200


def test_logout_clears_the_users_presence_entry(client, actor, monkeypatch):
    # End to end through the REAL chat helper: the socket id is
    # dropped from the presence map even when no server can be
    # reached to close the connection
    from app.chat import events as chat_events
    user, headers = actor
    monkeypatch.setitem(chat_events._connected_users, "sid-testinis", user["id"])

    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert "sid-testinis" not in chat_events._connected_users




# -----------------------------------------------------------
# Logout-all
# -----------------------------------------------------------

def test_logout_all_ignores_the_body_it_is_given(client, db, actor):
    user, headers = actor
    _seed_push_token(db, user["id"], "ExponentPushToken[telefonas]")

    response = client.post("/api/auth/logout-all", headers=headers,
                           json={"pushToken": "ExponentPushToken[kitas]", "keepSessions": True})

    assert response.status_code == 200
    assert _push_rows(db, user["id"]) == []
    assert _session_rows(db, user["id"]) == []


def test_logout_all_drops_expired_sessions_too(client, db, actor):
    user, headers = actor
    _seed_session(db, user["id"], "senas", _in_days(-9))

    assert client.post("/api/auth/logout-all", headers=headers).status_code == 200

    assert _session_rows(db, user["id"]) == []


def test_logout_all_without_any_push_tokens_still_answers_200(client, db, actor):
    user, headers = actor

    response = client.post("/api/auth/logout-all", headers=headers)

    assert response.status_code == 200
    assert _push_rows(db, user["id"]) == []


def test_logout_all_twice_is_a_401_the_second_time(client, actor):
    _, headers = actor

    assert client.post("/api/auth/logout-all", headers=headers).status_code == 200
    assert client.post("/api/auth/logout-all", headers=headers).status_code == 401


def test_logout_all_lets_the_user_sign_in_again_immediately(client, db, make_user, auth_headers):
    user = make_user()
    headers = auth_headers(user)

    client.post("/api/auth/logout-all", headers=headers)
    fresh = _login(client, username=user["username"], password=user["password"])

    assert fresh.status_code == 200
    assert _me(client, fresh.get_json()["token"]).status_code == 200
    assert len(_session_rows(db, user["id"])) == 1


def test_logout_all_survives_a_failing_socket_kick(client, db, actor, monkeypatch):
    from app.chat import events as chat_events
    user, headers = actor

    def explode(_user_id):
        raise RuntimeError("socket layer down")

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", explode)

    assert client.post("/api/auth/logout-all", headers=headers).status_code == 200
    assert _session_rows(db, user["id"]) == []


def test_logout_all_survives_a_missing_socket_layer(client, db, actor, monkeypatch):
    user, headers = actor
    monkeypatch.setitem(sys.modules, "app.chat.events", None)

    assert client.post("/api/auth/logout-all", headers=headers).status_code == 200
    assert _session_rows(db, user["id"]) == []


def test_logout_all_leaves_another_users_push_rows_and_sessions_alone(client, db, actor,
                                                                     make_user, auth_headers):
    user, headers = actor
    neighbour = make_user()
    neighbour_headers = auth_headers(neighbour)
    _seed_push_token(db, neighbour["id"], "ExponentPushToken[kaimyno]")

    client.post("/api/auth/logout-all", headers=headers)

    assert _push_rows(db, neighbour["id"]) == ["ExponentPushToken[kaimyno]"]
    assert client.get("/api/auth/me", headers=neighbour_headers).status_code == 200


def test_getting_the_logout_all_route_is_a_method_not_allowed(client):
    assert client.get("/api/auth/logout-all").status_code == 405




# -----------------------------------------------------------
# _disconnect_user_sockets — the guarded kill switch itself
# -----------------------------------------------------------

def test_the_socket_kick_hands_the_user_id_to_the_chat_helper(monkeypatch):
    from app.chat import events as chat_events
    kicked = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", kicked.append)

    result = auth_routes._disconnect_user_sockets("naudotojo-id")

    assert kicked == ["naudotojo-id"]
    assert result is None


def test_the_socket_kick_swallows_any_exception_from_the_helper(monkeypatch):
    from app.chat import events as chat_events

    def explode(_user_id):
        raise RuntimeError("socket layer down")

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", explode)

    assert auth_routes._disconnect_user_sockets("naudotojo-id") is None


def test_the_socket_kick_is_a_no_op_when_the_chat_module_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.chat.events", None)

    assert auth_routes._disconnect_user_sockets("naudotojo-id") is None


def test_the_socket_kick_is_a_no_op_when_the_helper_is_missing_from_the_module(monkeypatch):
    # A from-import of an absent name raises ImportError too — the
    # branch that covers the helper not having landed yet
    monkeypatch.setitem(sys.modules, "app.chat.events", types.ModuleType("app.chat.events"))

    assert auth_routes._disconnect_user_sockets("naudotojo-id") is None


def test_the_socket_kick_lets_a_base_exception_through(monkeypatch):
    # `except Exception` is deliberate: a shutdown signal must not
    # be swallowed by a best-effort cleanup
    from app.chat import events as chat_events

    def interrupt(_user_id):
        raise KeyboardInterrupt

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", interrupt)

    with pytest.raises(KeyboardInterrupt):
        auth_routes._disconnect_user_sockets("naudotojo-id")


def test_the_socket_kick_reaches_the_real_chat_helper(app):
    # No monkeypatch: the guarded import resolves in the shape
    # production runs, and an unknown user is simply a no-op
    assert auth_routes._disconnect_user_sockets("niekada-neprisijunges") is None
