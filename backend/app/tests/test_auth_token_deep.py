# -----------------------------------------------------------
#  [*] Tests — the bearer-token spine of app/auth/routes.py
#
#  The exhaustive pass over the six functions every other
#  blueprint leans on, one branch at a time:
#
#    _hash_token           raw token → the sha256 stored at rest
#    _bearer_token         "Authorization: Bearer <t>" → <t>
#    resolve_session_token token → the public user dict
#    get_current_user      the header + the per-request g cache
#    require_auth          the 401 gate
#    require_role          the 401/403 gate
#
#  What this module proves, beyond the happy paths the login
#  and /me suites already pin:
#
#    - the hash is a plain, one-way sha256 of the token's UTF-8
#      bytes: case-sensitive, whitespace-sensitive, stable, and
#      never itself a usable token (the stored value replays to
#      nobody)
#    - the header parser splits on the FIRST space only, is
#      case-insensitive on the scheme alone, and answers None
#      for every other shape — including the comma-joined value
#      two Authorization headers arrive as
#    - the session lookup compares expiry as an INSTANT (offset
#      stamps, naive stamps, date-only stamps, "Z" stamps), is
#      inclusive at the exact expiry microsecond, treats every
#      unreadable value — blob included — as expired, purges on
#      the spot, and closes its connection on every exit
#    - the g cache is keyed by the token, keeps negative
#      results, is shared by the decorator and the handler, and
#      dies with the request
#    - the two gates: their exact bodies, their argument
#      pass-through, the order of the anonymous and the role
#      check, and the full role × gate matrix
#
#  No wall-clock sleeping (time_machine for every expiry) and
#  no network. Sessions a route cannot mint — an orphan row, a
#  blob expiry, a stamp landing exactly on `now` — are seeded
#  straight through the `db` fixture; every token a client
#  presents comes from the shared login fixtures.
# -----------------------------------------------------------

import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine
from flask import Flask, g, jsonify, request

from app.auth import routes as auth_routes


# The public columns resolve_session_token narrows to — the
# whitelist that keeps password_hash and the timestamps inside
# the auth module
_PUBLIC_COLUMNS = {
    "id", "username", "email", "display_name", "role", "avatar_url",
    "invited", "active", "student_number", "study_group", "study_program",
}

# Sentinel for "send no Authorization header at all", which is
# a different case from sending an empty one
_ABSENT = object()




# -----------------------------------------------------------
# _clean_rate_limit_store
# -----------------------------------------------------------
#
# The limiter dict is module-level and outlives a test — and
# create_app's global per-IP throttle writes to the very same
# store, so a sibling module's flood could otherwise turn these
# tests' requests into 429s. Cleared on both sides.
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
# _sha256 / _bearer / _token_for
# -----------------------------------------------------------
#
# _sha256 recomputes the at-rest digest INDEPENDENTLY of auth's
# _hash_token: asserting the module against itself would pass
# even if both changed together, which is the exact regression
# the storage rule exists to prevent.
#
# Used by:
#   - the hash tests and every seeded session below
# -----------------------------------------------------------

def _sha256(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _token_for(auth_headers, user):
    return auth_headers(user)["Authorization"].split(" ", 1)[1]




# -----------------------------------------------------------
# _seed_session
# -----------------------------------------------------------
#
# A sessions row with an expiry no route would ever mint — a
# stamp landing exactly on `now`, a foreign offset, a blob —
# stored the way production stores it (sha256, never the raw
# token) and handing back the raw token a client would present.
#
# Used by:
#   - the expiry-boundary and malformed-stamp tests
# -----------------------------------------------------------

def _seed_session(db, user_id, expires_at, token=None):
    token = token or str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions (id, user_id, token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, _sha256(token),
         datetime.now(timezone.utc).isoformat(), expires_at),
    )
    db.commit()
    return token


def _session_count(db, user_id):
    return db.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (user_id,),
    ).fetchone()["n"]


def _push_tokens(db, user_id):
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
# _mount
# -----------------------------------------------------------
#
# Registers a throwaway route so the two gate decorators can be
# exercised as THEMSELVES rather than through whichever
# blueprint happens to wear them. Flask refuses new routes once
# the app has served a request, so every test calls this before
# its first client call — which is also why these tests take
# `make_user` + `auth_headers` (the login happens on demand)
# and never the `actor` / `admin` fixtures, which sign in
# during setup.
#
# Used by:
#   - the require_auth and require_role sections
# -----------------------------------------------------------

def _mount(app, rule, view):
    app.add_url_rule(rule, f"probe_{len(app.view_functions)}", view, methods=["GET", "POST"])




# -----------------------------------------------------------
# header_probe
# -----------------------------------------------------------
#
# _bearer_token reads exactly one thing — request.headers — so
# it is probed on a BARE Flask app: no database, no blueprints,
# no middleware that could pre-chew the header. Module-scoped,
# because the parser has no state to reset between cases.
#
# Used by:
#   - the header-parsing section
# -----------------------------------------------------------

@pytest.fixture(scope="module")
def header_probe():
    application = Flask("bearer-probe")

    def _probe(value=_ABSENT):
        headers = {} if value is _ABSENT else {"Authorization": value}
        with application.test_request_context("/", headers=headers):
            return auth_routes._bearer_token()

    return _probe




# -----------------------------------------------------------
# _TrackedConnection / _BlockedConnection
# -----------------------------------------------------------
#
# Real connections with one statement rewritten. _Tracked
# records every close() so the `finally` in
# resolve_session_token can be proven to run on EVERY exit;
# _Blocked additionally raises the "database is locked" error a
# concurrent writer would raise, for whichever statement the
# caller names — the sessions DELETE, the push DELETE or the
# very first SELECT.
#
# Used by:
#   - the purge and connection-lifetime tests
# -----------------------------------------------------------

class _TrackedConnection:

    def __init__(self, real, log):
        self._real = real
        self._log = log

    def close(self):
        self._log.append("close")
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _BlockedConnection(_TrackedConnection):

    def __init__(self, real, log, blocked):
        super().__init__(real, log)
        self._blocked = blocked

    def execute(self, sql, params=()):
        if self._blocked in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, params)




# -----------------------------------------------------------
# _hash_token — the one-way digest sessions.token stores
# -----------------------------------------------------------

@pytest.mark.parametrize("token", [
    "a",
    "00000000-0000-0000-0000-000000000000",
    # Fixed, never uuid4(): a collection-time random value gives
    # each xdist worker a different test id and aborts the run
    "b3f1c2a4-5d6e-4f70-8a91-2c3d4e5f6a7b",
    "Bearer",
    "slaptažodis-ąžuolas-į",
    " ",
    "\n",
    "x" * 4096,
])
def test_the_token_hash_is_the_sha256_of_the_tokens_utf_8_bytes(token):
    assert auth_routes._hash_token(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_the_token_hash_is_sixty_four_lowercase_hex_characters():
    digest = auth_routes._hash_token(str(uuid.uuid4()))

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_hashing_the_same_token_twice_gives_the_same_digest():
    token = str(uuid.uuid4())

    assert auth_routes._hash_token(token) == auth_routes._hash_token(token)


def test_two_tokens_differing_in_one_character_hash_apart():
    assert auth_routes._hash_token("tokenas-a") != auth_routes._hash_token("tokenas-b")


def test_the_token_hash_is_case_sensitive():
    # uuid4 tokens are lowercase; an uppercased copy must not
    # reach the same sessions row
    assert auth_routes._hash_token("ABCDEF") != auth_routes._hash_token("abcdef")


def test_the_empty_token_hashes_to_the_empty_sha256():
    # logout and change_password feed `_bearer_token() or ""`
    # through here, so the empty string must hash, not raise
    assert auth_routes._hash_token("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


@pytest.mark.parametrize("token", [" abc", "abc ", "\tabc", "abc\t", " abc "])
def test_whitespace_around_a_token_makes_it_a_different_token(token):
    assert auth_routes._hash_token(token) != auth_routes._hash_token("abc")


def test_a_megabyte_long_token_still_hashes():
    huge = "a" * 1_000_000

    assert auth_routes._hash_token(huge) == hashlib.sha256(huge.encode()).hexdigest()


@pytest.mark.parametrize("value", [None, b"baitai", 42, ["a"]])
def test_hashing_anything_but_a_string_raises(value):
    # Neither caller can reach this: get_current_user only ever
    # passes _bearer_token's stripped str, logout and
    # change_password default it to "", and the socket handshake
    # type-checks before calling resolve_session_token
    with pytest.raises(AttributeError):
        auth_routes._hash_token(value)


def test_the_hash_is_exactly_what_the_login_route_wrote_to_the_row(client, db, make_user, auth_headers):
    user = make_user()
    token = _token_for(auth_headers, user)

    stored = db.execute(
        "SELECT token FROM sessions WHERE user_id = ?", (user["id"],),
    ).fetchone()["token"]

    assert stored == _sha256(token)
    assert stored == auth_routes._hash_token(token)
    assert token not in stored




# -----------------------------------------------------------
# _bearer_token — the header parser
# -----------------------------------------------------------

@pytest.mark.parametrize("header, expected", [
    ("Bearer abc", "abc"),
    ("bearer abc", "abc"),
    ("BEARER abc", "abc"),
    ("BeArEr abc", "abc"),
    ("Bearer   abc", "abc"),
    ("Bearer abc   ", "abc"),
    ("Bearer \tabc\t", "abc"),
    ("Bearer  \t abc \t ", "abc"),
])
def test_a_bearer_header_yields_its_token(header_probe, header, expected):
    assert header_probe(header) == expected


@pytest.mark.parametrize("header", [
    _ABSENT,
    "",
    " ",
    "Bearer",
    "Bearer ",
    "Bearer      ",
    "Bearer \t ",
    "bearer",
    "Bearer\tabc",
    "Bearerabc",
    "BearerX abc",
    "Token abc",
    "Basic YWJjOmRlZg==",
    "abc",
    " Bearer abc",
    "  Bearer abc",
    "\tBearer abc",
])
def test_every_other_header_shape_yields_no_token(header_probe, header):
    assert header_probe(header) is None


def test_only_the_first_space_splits_the_header(header_probe):
    # partition, not split — the remainder is the token, spaces
    # and all, so a token is never silently truncated
    assert header_probe("Bearer abc def") == "abc def"
    assert header_probe("Bearer Bearer abc") == "Bearer abc"


def test_the_scheme_match_ignores_case_but_not_stray_characters(header_probe):
    assert header_probe("bEaReR abc") == "abc"
    assert header_probe("Bearer. abc") is None
    assert header_probe("'Bearer' abc") is None


def test_a_token_keeps_every_character_a_uuid_or_a_base64_value_can_carry(header_probe):
    token = "AbC-123_x.y~z=="

    assert header_probe(f"Bearer {token}") == token


def test_a_uuid_token_survives_the_parser_unchanged(header_probe):
    token = str(uuid.uuid4())

    assert header_probe(f"Bearer {token}") == token


def test_an_eight_kilobyte_token_comes_back_whole(header_probe):
    token = "t" * 8192

    assert header_probe(f"Bearer {token}") == token


def test_a_non_ascii_token_is_returned_as_typed(header_probe):
    # Nothing mints one, but the parser must not mangle or
    # refuse it — resolve_session_token answers the 401
    assert header_probe("Bearer žetonas-ąč") == "žetonas-ąč"


def test_two_authorization_headers_cannot_smuggle_a_second_token():
    # WSGI joins repeated headers with ", ", so the parser sees
    # ONE value: the first token plus the rest, which resolves
    # to nobody rather than to either session
    application = Flask("bearer-probe-dup")

    with application.test_request_context("/", headers=[
        ("Authorization", "Bearer pirmas"),
        ("Authorization", "Bearer antras"),
    ]):
        assert auth_routes._bearer_token() == "pirmas, Bearer antras"


def test_the_parser_reads_the_header_and_nothing_else(header_probe):
    # No database, no app config, no blueprints on the probe app
    assert header_probe("Bearer abc") == "abc"


def test_the_padded_header_the_parser_accepts_is_the_token_a_route_resolves(client, make_user, auth_headers):
    user = make_user()
    token = _token_for(auth_headers, user)

    response = client.get("/api/auth/me", headers={"Authorization": f"bearer  \t{token}\t "})

    assert response.status_code == 200
    assert response.get_json()["id"] == user["id"]




# -----------------------------------------------------------
# resolve_session_token — the row, the shape
# -----------------------------------------------------------

def test_the_resolved_dict_carries_exactly_the_public_columns(app, client, make_user, auth_headers):
    user = make_user()
    token = _token_for(auth_headers, user)

    resolved = auth_routes.resolve_session_token(token)

    assert set(resolved) == _PUBLIC_COLUMNS
    assert "password_hash" not in resolved
    assert "created_at" not in resolved and "updated_at" not in resolved


def test_the_resolved_value_is_a_plain_dict_a_serializer_can_get_from(app, client, make_user, auth_headers):
    # _serialize_user needs .get(), which sqlite3.Row lacks
    user = make_user()
    token = _token_for(auth_headers, user)

    resolved = auth_routes.resolve_session_token(token)

    assert type(resolved) is dict
    assert resolved.get("nera-tokio-stulpelio") is None


def test_the_resolved_dict_mirrors_the_row_including_the_student_card(app, client, db, make_user, auth_headers):
    user = make_user(role="teacher", display_name="Ona Onaitė")
    db.execute(
        "UPDATE users SET student_number = ?, study_group = ?, study_program = ?, avatar_url = ?"
        " WHERE id = ?",
        ("S-1", "IF-1", "Informatika", "/api/uploads/a.jpg", user["id"]),
    )
    db.commit()
    token = _token_for(auth_headers, user)

    resolved = auth_routes.resolve_session_token(token)

    assert resolved["id"] == user["id"]
    assert resolved["username"] == user["username"]
    assert resolved["email"] == user["email"]
    assert resolved["display_name"] == "Ona Onaitė"
    assert resolved["role"] == "teacher"
    assert resolved["avatar_url"] == "/api/uploads/a.jpg"
    assert resolved["invited"] == 1
    assert resolved["active"] == 1
    assert resolved["student_number"] == "S-1"
    assert resolved["study_group"] == "IF-1"
    assert resolved["study_program"] == "Informatika"


def test_every_resolution_hands_back_a_fresh_dict(app, client, make_user, auth_headers):
    # Handlers park it on request.user and read it back; one
    # request must not be able to poison the next
    user = make_user()
    token = _token_for(auth_headers, user)

    first = auth_routes.resolve_session_token(token)
    first["role"] = "admin"
    second = auth_routes.resolve_session_token(token)

    assert second is not first
    assert second["role"] == "student"


@pytest.mark.parametrize("token", [
    "",
    " ",
    # A FIXED uuid, never uuid4(): a value generated at collection
    # time gives every xdist worker a different test id, and the
    # run aborts with "different tests were collected"
    "e57d8c6d-b0e8-4fdf-b8d7-28583e6d8ddf",
    "nera-tokio-tokeno",
    "x" * 100_000,
    "' OR 1=1 --",
    "\x00",
])
def test_a_token_with_no_session_row_resolves_to_nobody(app, client, db, make_user, auth_headers, token):
    user = make_user()
    _token_for(auth_headers, user)

    assert auth_routes.resolve_session_token(token) is None
    # The lookup is parameterised: the live session is untouched
    assert _session_count(db, user["id"]) == 1


def test_the_stored_hash_cannot_be_replayed_as_a_token(app, client, db, make_user, auth_headers):
    user = make_user()
    token = _token_for(auth_headers, user)
    stored = db.execute(
        "SELECT token FROM sessions WHERE user_id = ?", (user["id"],),
    ).fetchone()["token"]

    assert auth_routes.resolve_session_token(token)["id"] == user["id"]
    assert auth_routes.resolve_session_token(stored) is None


def test_two_users_tokens_never_cross(app, client, make_user, auth_headers):
    one = make_user()
    two = make_user()
    token_one = _token_for(auth_headers, one)
    token_two = _token_for(auth_headers, two)

    assert auth_routes.resolve_session_token(token_one)["id"] == one["id"]
    assert auth_routes.resolve_session_token(token_two)["id"] == two["id"]


def test_a_session_whose_user_row_is_gone_resolves_to_nobody_and_is_left_alone(app, db, make_user):
    # Only expiry purges; an orphan row is the admin's problem,
    # not this lookup's
    orphan = _seed_session(db, "nera-tokio-vartotojo",
                           (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())

    assert auth_routes.resolve_session_token(orphan) is None
    assert _session_count(db, "nera-tokio-vartotojo") == 1




# -----------------------------------------------------------
# resolve_session_token — the users.active backstop
# -----------------------------------------------------------

def test_a_deactivated_account_resolves_to_nobody_but_keeps_its_session_row(app, client, db, make_user, auth_headers):
    user = make_user()
    token = _token_for(auth_headers, user)
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert auth_routes.resolve_session_token(token) is None
    assert _session_count(db, user["id"]) == 1


def test_reactivating_an_account_makes_the_old_token_work_again(app, client, db, make_user, auth_headers):
    # Nothing is cached at rest — the flag is read on every
    # single resolution
    user = make_user()
    token = _token_for(auth_headers, user)

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()
    assert auth_routes.resolve_session_token(token) is None

    db.execute("UPDATE users SET active = 1 WHERE id = ?", (user["id"],))
    db.commit()
    assert auth_routes.resolve_session_token(token)["id"] == user["id"]


def test_the_active_check_is_a_truth_test_not_an_equality_test(app, client, db, make_user, auth_headers):
    # users.active carries no CHECK constraint, so DbGate can
    # write any integer; only a falsy one locks the account out
    user = make_user()
    token = _token_for(auth_headers, user)

    db.execute("UPDATE users SET active = 2 WHERE id = ?", (user["id"],))
    db.commit()

    assert auth_routes.resolve_session_token(token)["active"] == 2


def test_a_deactivated_account_loses_the_socket_handshake_too(app, client, db, make_user, auth_headers):
    # chat/events.py shares this exact lookup, so the flag gates
    # realtime the same way it gates REST
    user = make_user()
    token = _token_for(auth_headers, user)
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    from app.chat.events import _authenticate_socket

    with app.test_request_context("/socket.io/"):
        assert _authenticate_socket({"token": token}) is None




# -----------------------------------------------------------
# resolve_session_token — expiry as an instant
# -----------------------------------------------------------

def test_a_session_expiring_at_this_exact_microsecond_is_still_valid(app, db, make_user):
    # The comparison is strict `<`, so the expiry instant itself
    # still belongs to the session
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    token = _seed_session(db, user["id"], base.isoformat())

    with time_machine.travel(base, tick=False):
        assert auth_routes.resolve_session_token(token)["id"] == user["id"]

    assert _session_count(db, user["id"]) == 1


def test_a_session_one_microsecond_past_its_expiry_is_gone(app, db, make_user):
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    token = _seed_session(db, user["id"], (base - timedelta(microseconds=1)).isoformat())

    with time_machine.travel(base, tick=False):
        assert auth_routes.resolve_session_token(token) is None

    assert _session_count(db, user["id"]) == 0


def test_an_expiry_in_another_offset_is_compared_as_an_instant(app, db, make_user):
    # 13:00+05:00 is 08:00Z — text order would call it future,
    # instant order calls it past
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    already_past = _seed_session(db, user["id"], "2026-03-01T13:00:00+05:00")
    still_future = _seed_session(db, user["id"], "2026-03-01T11:00:00-05:00")

    with time_machine.travel(base, tick=False):
        assert auth_routes.resolve_session_token(already_past) is None
        assert auth_routes.resolve_session_token(still_future)["id"] == user["id"]


def test_a_z_suffixed_expiry_is_read_as_utc(app, db, make_user):
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    token = _seed_session(db, user["id"], "2026-03-02T00:00:00Z")

    with time_machine.travel(base, tick=False):
        assert auth_routes.resolve_session_token(token)["id"] == user["id"]

    with time_machine.travel(base + timedelta(days=2), tick=False):
        assert auth_routes.resolve_session_token(token) is None


def test_a_date_only_expiry_is_read_as_midnight_utc(app, db, make_user):
    user = make_user()
    token = _seed_session(db, user["id"], "2026-03-02")

    with time_machine.travel(datetime(2026, 3, 1, 23, 59, tzinfo=timezone.utc), tick=False):
        assert auth_routes.resolve_session_token(token)["id"] == user["id"]

    with time_machine.travel(datetime(2026, 3, 2, 0, 1, tzinfo=timezone.utc), tick=False):
        assert auth_routes.resolve_session_token(token) is None


def test_a_naive_expiry_is_assumed_utc_on_both_sides_of_the_line(app, db, make_user):
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
    future = _seed_session(db, user["id"], "2026-03-01T12:00:01")
    past = _seed_session(db, user["id"], "2026-03-01T11:59:59")

    with time_machine.travel(base, tick=False):
        assert auth_routes.resolve_session_token(future)["id"] == user["id"]
        assert auth_routes.resolve_session_token(past) is None


@pytest.mark.parametrize("expires_at", [
    "",
    " ",
    "kada nors",
    "2026-13-01T00:00:00+00:00",
    "2026-02-30T00:00:00+00:00",
    "1772370000",
    "2026/03/01 12:00:00",
    "T12:00:00",
])
def test_an_unparseable_expiry_counts_as_expired(app, db, make_user, expires_at):
    user = make_user()
    token = _seed_session(db, user["id"], expires_at)

    assert auth_routes.resolve_session_token(token) is None
    assert _session_count(db, user["id"]) == 0


def test_an_expiry_stored_as_a_blob_counts_as_expired(app, db, make_user):
    # TEXT affinity leaves a BLOB a BLOB, so fromisoformat gets
    # bytes and raises TypeError, not ValueError — the other arm
    # of the same except clause
    user = make_user()
    token = _seed_session(db, user["id"], b"2026-03-01T12:00:00+00:00")

    assert auth_routes.resolve_session_token(token) is None
    assert _session_count(db, user["id"]) == 0




# -----------------------------------------------------------
# resolve_session_token — the lazy purge
# -----------------------------------------------------------

def test_the_purge_drops_only_the_expired_row_of_that_one_user(app, client, db, make_user, auth_headers):
    user = make_user()
    neighbour = make_user()
    live = _token_for(auth_headers, user)
    neighbour_token = _token_for(auth_headers, neighbour)
    dead = _seed_session(db, user["id"], "2020-01-01T00:00:00+00:00")

    assert auth_routes.resolve_session_token(dead) is None

    assert _session_count(db, user["id"]) == 1
    assert auth_routes.resolve_session_token(live)["id"] == user["id"]
    assert auth_routes.resolve_session_token(neighbour_token)["id"] == neighbour["id"]


def test_the_purge_takes_every_push_row_of_that_user_and_nobody_elses(app, db, make_user):
    # Documented, and deliberately wider than the one session:
    # a still-logged-in device simply re-registers on its next
    # app start
    user = make_user()
    neighbour = make_user()
    dead = _seed_session(db, user["id"], "2020-01-01T00:00:00+00:00")
    _seed_push_token(db, user["id"], "ExponentPushToken[savas]")
    _seed_push_token(db, neighbour["id"], "ExponentPushToken[kaimyno]")

    assert auth_routes.resolve_session_token(dead) is None

    assert _push_tokens(db, user["id"]) == []
    assert _push_tokens(db, neighbour["id"]) == ["ExponentPushToken[kaimyno]"]


def test_purging_a_token_twice_is_idempotent(app, db, make_user):
    user = make_user()
    dead = _seed_session(db, user["id"], "2020-01-01T00:00:00+00:00")

    assert auth_routes.resolve_session_token(dead) is None
    # Second time the row is already gone: the unknown-token
    # early return, no error
    assert auth_routes.resolve_session_token(dead) is None
    assert _session_count(db, user["id"]) == 0


def test_a_locked_session_delete_still_answers_nobody(app, db, make_user, monkeypatch):
    user = make_user()
    dead = _seed_session(db, user["id"], "2020-01-01T00:00:00+00:00")
    _seed_push_token(db, user["id"], "ExponentPushToken[savas]")

    log = []
    real_get_db = auth_routes.get_db
    monkeypatch.setattr(auth_routes, "get_db",
                        lambda: _BlockedConnection(real_get_db(), log, "DELETE FROM sessions"))

    assert auth_routes.resolve_session_token(dead) is None
    # Best effort: nothing committed, so both rows survive the
    # lock window and the caller still gets a clean 401
    assert _session_count(db, user["id"]) == 1
    assert _push_tokens(db, user["id"]) == ["ExponentPushToken[savas]"]
    assert log == ["close"]


def test_a_locked_push_delete_rolls_the_session_delete_back_too(app, db, make_user, monkeypatch):
    # The sessions DELETE ran but never committed; closing the
    # connection discards it, so the purge is all-or-nothing
    user = make_user()
    dead = _seed_session(db, user["id"], "2020-01-01T00:00:00+00:00")
    _seed_push_token(db, user["id"], "ExponentPushToken[savas]")

    log = []
    real_get_db = auth_routes.get_db
    monkeypatch.setattr(auth_routes, "get_db",
                        lambda: _BlockedConnection(real_get_db(), log, "DELETE FROM push_tokens"))

    assert auth_routes.resolve_session_token(dead) is None
    assert _session_count(db, user["id"]) == 1
    assert _push_tokens(db, user["id"]) == ["ExponentPushToken[savas]"]
    assert log == ["close"]


def test_an_expired_session_answers_401_over_http_rather_than_500(app, client, db, make_user, auth_headers, monkeypatch):
    user = make_user()
    token = _token_for(auth_headers, user)
    db.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00+00:00' WHERE token = ?",
               (_sha256(token),))
    db.commit()

    real_get_db = auth_routes.get_db
    monkeypatch.setattr(auth_routes, "get_db",
                        lambda: _BlockedConnection(real_get_db(), [], "DELETE FROM sessions"))

    response = client.get("/api/auth/me", headers=_bearer(token))

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}




# -----------------------------------------------------------
# resolve_session_token — the connection is always closed
# -----------------------------------------------------------

@pytest.mark.parametrize("case", ["unknown", "expired", "orphan", "deactivated", "resolved"])
def test_the_connection_is_closed_on_every_exit(app, client, db, make_user, auth_headers, monkeypatch, case):
    user = make_user()
    token = _token_for(auth_headers, user)

    if case == "unknown":
        token = str(uuid.uuid4())
    elif case == "expired":
        token = _seed_session(db, user["id"], "2020-01-01T00:00:00+00:00")
    elif case == "orphan":
        token = _seed_session(db, "nera-tokio-vartotojo",
                              (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    elif case == "deactivated":
        db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
        db.commit()

    log = []
    real_get_db = auth_routes.get_db
    monkeypatch.setattr(auth_routes, "get_db", lambda: _TrackedConnection(real_get_db(), log))

    auth_routes.resolve_session_token(token)

    assert log == ["close"]


def test_the_connection_is_closed_even_when_the_lookup_itself_fails(app, make_user, monkeypatch):
    log = []
    real_get_db = auth_routes.get_db
    monkeypatch.setattr(auth_routes, "get_db",
                        lambda: _BlockedConnection(real_get_db(), log, "SELECT s.user_id"))

    with pytest.raises(sqlite3.OperationalError):
        auth_routes.resolve_session_token(str(uuid.uuid4()))

    assert log == ["close"]




# -----------------------------------------------------------
# get_current_user — the header and the per-request cache
# -----------------------------------------------------------

@pytest.mark.parametrize("header", [_ABSENT, "", "Bearer", "Bearer   ", "Token abc", "abc"])
def test_a_headerless_caller_is_nobody_and_costs_no_lookup(app, monkeypatch, header):
    calls = []
    monkeypatch.setattr(auth_routes, "resolve_session_token",
                        lambda raw: calls.append(raw) or None)
    headers = {} if header is _ABSENT else {"Authorization": header}

    with app.test_request_context("/api/auth/me", headers=headers):
        assert auth_routes.get_current_user() is None
        # The early return also leaves the cache alone
        assert getattr(g, "_auth_cache", None) is None

    assert calls == []


def test_the_resolved_caller_is_cached_under_the_stripped_token(app, client, make_user, auth_headers):
    user = make_user()
    token = _token_for(auth_headers, user)

    with app.test_request_context("/api/auth/me",
                                  headers={"Authorization": f"Bearer  {token}  "}):
        resolved = auth_routes.get_current_user()

        assert g._auth_cache[0] == token
        assert g._auth_cache[1] is resolved


def test_a_padded_header_does_not_split_the_cache(app, client, make_user, auth_headers, monkeypatch):
    user = make_user()
    token = _token_for(auth_headers, user)

    calls = []
    real_resolve = auth_routes.resolve_session_token
    monkeypatch.setattr(auth_routes, "resolve_session_token",
                        lambda raw: calls.append(raw) or real_resolve(raw))

    with app.test_request_context("/api/auth/me",
                                  headers={"Authorization": f"Bearer \t{token}\t"}):
        first = auth_routes.get_current_user()
        second = auth_routes.get_current_user()

    assert first is second
    assert calls == [token]


def test_a_stale_cache_entry_for_another_token_is_replaced(app, client, make_user, auth_headers):
    user = make_user()
    token = _token_for(auth_headers, user)

    with app.test_request_context("/api/auth/me", headers=_bearer(token)):
        g._auth_cache = ("kitas-tokenas", None)

        resolved = auth_routes.get_current_user()

        assert resolved["id"] == user["id"]
        # The stale entry is not merely bypassed, it is overwritten
        assert g._auth_cache[0] == token
        assert g._auth_cache[1] is resolved


def test_a_cached_rejection_is_not_looked_up_again(app, monkeypatch):
    calls = []
    monkeypatch.setattr(auth_routes, "resolve_session_token",
                        lambda raw: calls.append(raw) or None)

    with app.test_request_context("/api/auth/me", headers=_bearer("nera-tokio")):
        assert auth_routes.get_current_user() is None
        assert auth_routes.get_current_user() is None
        assert g._auth_cache == ("nera-tokio", None)

    assert calls == ["nera-tokio"]


def test_the_cache_outlives_the_session_row_it_resolved(app, client, db, make_user, auth_headers):
    # One resolution per request, by design: a session deleted
    # mid-request does not change the answer the handler already
    # got — the NEXT request pays the price
    user = make_user()
    token = _token_for(auth_headers, user)

    with app.test_request_context("/api/auth/me", headers=_bearer(token)):
        first = auth_routes.get_current_user()
        db.execute("DELETE FROM sessions WHERE token = ?", (_sha256(token),))
        db.commit()

        assert auth_routes.get_current_user() is first

    assert auth_routes.resolve_session_token(token) is None


def test_every_request_starts_with_an_empty_cache(app, client, make_user, auth_headers, monkeypatch):
    user = make_user()
    headers = auth_headers(user)

    calls = []
    real_resolve = auth_routes.resolve_session_token
    monkeypatch.setattr(auth_routes, "resolve_session_token",
                        lambda raw: calls.append(raw) or real_resolve(raw))

    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    assert len(calls) == 2


def test_the_decorator_and_the_handler_share_one_resolution(app, client, make_user, auth_headers, monkeypatch):
    _mount(app, "/probe/twice",
           auth_routes.require_auth(
               lambda: jsonify({"same": auth_routes.get_current_user() is request.user})))
    user = make_user()
    headers = auth_headers(user)

    calls = []
    real_resolve = auth_routes.resolve_session_token
    monkeypatch.setattr(auth_routes, "resolve_session_token",
                        lambda raw: calls.append(raw) or real_resolve(raw))

    response = client.get("/probe/twice", headers=headers)

    assert response.get_json() == {"same": True}
    assert len(calls) == 1


def test_an_expired_token_resolves_to_nobody_through_the_header(app, client, make_user, auth_headers):
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    with time_machine.travel(base, tick=False):
        token = _token_for(auth_headers, user)

    with time_machine.travel(base + timedelta(days=31), tick=False):
        with app.test_request_context("/api/auth/me", headers=_bearer(token)):
            assert auth_routes.get_current_user() is None




# -----------------------------------------------------------
# require_auth — the 401 gate
# -----------------------------------------------------------

def test_require_auth_answers_the_exact_house_401(app, client):
    _mount(app, "/probe/gated", auth_routes.require_auth(lambda: jsonify({"ok": True})))

    response = client.get("/probe/gated")

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}
    assert response.mimetype == "application/json"


def test_require_auth_never_reaches_the_handler_when_the_caller_is_nobody(app, client):
    reached = []
    _mount(app, "/probe/gated",
           auth_routes.require_auth(lambda: reached.append(1) or jsonify({"ok": True})))

    assert client.get("/probe/gated").status_code == 401
    assert reached == []


@pytest.mark.parametrize("header", [
    _ABSENT, "", "Bearer", "Bearer ", "Basic YWJjOmRlZg==",
    "Token 4e0c0b16-0000-4000-8000-000000000000",
    "Bearer 4e0c0b16-0000-4000-8000-000000000000",
])
def test_require_auth_refuses_every_unusable_header(app, client, header):
    _mount(app, "/probe/gated", auth_routes.require_auth(lambda: jsonify({"ok": True})))
    headers = {} if header is _ABSENT else {"Authorization": header}

    assert client.get("/probe/gated", headers=headers).status_code == 401


def test_require_auth_hands_the_public_user_dict_to_the_handler(app, client, make_user, auth_headers):
    _mount(app, "/probe/self", auth_routes.require_auth(lambda: jsonify(sorted(request.user))))
    user = make_user()

    response = client.get("/probe/self", headers=auth_headers(user))

    assert response.status_code == 200
    assert set(response.get_json()) == _PUBLIC_COLUMNS


@pytest.mark.parametrize("role", list(auth_routes.ROLES))
def test_require_auth_is_blind_to_the_callers_role(app, client, make_user, auth_headers, role):
    _mount(app, "/probe/self", auth_routes.require_auth(lambda: jsonify({"role": request.user["role"]})))
    user = make_user(role=role)

    response = client.get("/probe/self", headers=auth_headers(user))

    assert response.status_code == 200
    assert response.get_json()["role"] == role


def test_require_auth_passes_the_url_arguments_through(app, client, make_user, auth_headers):
    _mount(app, "/probe/echo/<slug>",
           auth_routes.require_auth(lambda slug: jsonify({"slug": slug})))
    user = make_user()

    response = client.get("/probe/echo/labas", headers=auth_headers(user))

    assert response.get_json() == {"slug": "labas"}


def test_require_auth_returns_the_handlers_own_status_and_headers(app, client, make_user, auth_headers):
    _mount(app, "/probe/created",
           auth_routes.require_auth(lambda: (jsonify({"ok": True}), 202, {"X-Probe": "taip"})))
    user = make_user()

    response = client.post("/probe/created", headers=auth_headers(user))

    assert response.status_code == 202
    assert response.headers["X-Probe"] == "taip"


def test_require_auth_keeps_the_wrapped_functions_identity():
    def named_view():
        return None

    decorated = auth_routes.require_auth(named_view)

    assert decorated.__name__ == "named_view"
    assert decorated.__wrapped__ is named_view


def test_require_auth_refuses_a_deactivated_account(app, client, db, make_user, auth_headers):
    _mount(app, "/probe/gated", auth_routes.require_auth(lambda: jsonify({"ok": True})))
    user = make_user()
    headers = auth_headers(user)
    assert client.get("/probe/gated", headers=headers).status_code == 200

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert client.get("/probe/gated", headers=headers).status_code == 401


def test_require_auth_refuses_an_expired_session(app, client, make_user, auth_headers):
    _mount(app, "/probe/gated", auth_routes.require_auth(lambda: jsonify({"ok": True})))
    user = make_user()
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    with time_machine.travel(base, tick=False):
        headers = auth_headers(user)

    with time_machine.travel(base + timedelta(days=30, minutes=1), tick=False):
        assert client.get("/probe/gated", headers=headers).status_code == 401


def test_require_auth_refuses_a_logged_out_token(app, client, make_user, auth_headers):
    _mount(app, "/probe/gated", auth_routes.require_auth(lambda: jsonify({"ok": True})))
    user = make_user()
    headers = auth_headers(user)

    assert client.post("/api/auth/logout", headers=headers).status_code == 200

    assert client.get("/probe/gated", headers=headers).status_code == 401




# -----------------------------------------------------------
# require_role — the 401/403 gate and the full role matrix
# -----------------------------------------------------------

@pytest.mark.parametrize("gate, role, expected", [
    (("admin",), "admin", 200),
    (("admin",), "curator", 403),
    (("admin",), "teacher", 403),
    (("admin",), "student", 403),
    (("admin", "curator"), "admin", 200),
    (("admin", "curator"), "curator", 200),
    (("admin", "curator"), "teacher", 403),
    (("admin", "curator"), "student", 403),
    (auth_routes.ROLES, "admin", 200),
    (auth_routes.ROLES, "curator", 200),
    (auth_routes.ROLES, "teacher", 200),
    (auth_routes.ROLES, "student", 200),
    ((), "admin", 403),
])
def test_the_role_gate_admits_exactly_the_roles_it_lists(app, client, make_user, auth_headers, gate, role, expected):
    _mount(app, "/probe/gated", auth_routes.require_role(*gate)(lambda: jsonify({"ok": True})))
    user = make_user(role=role)

    response = client.get("/probe/gated", headers=auth_headers(user))

    assert response.status_code == expected


def test_the_role_gate_answers_the_exact_house_403(app, client, make_user, auth_headers):
    _mount(app, "/probe/gated", auth_routes.require_role("admin")(lambda: jsonify({"ok": True})))
    user = make_user(role="student")

    response = client.get("/probe/gated", headers=auth_headers(user))

    assert response.status_code == 403
    assert response.get_json() == {"error": "Insufficient permissions"}
    assert response.mimetype == "application/json"


def test_the_role_gate_answers_401_before_it_ever_looks_at_the_roles(app, client):
    _mount(app, "/probe/gated", auth_routes.require_role()(lambda: jsonify({"ok": True})))

    response = client.get("/probe/gated")

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}


def test_a_deactivated_admin_is_a_401_not_a_403(app, client, db, make_user, auth_headers):
    # The anonymous check runs first, so a revoked admin looks
    # like a dead session to the client, which is what it is
    _mount(app, "/probe/gated", auth_routes.require_role("admin")(lambda: jsonify({"ok": True})))
    user = make_user(role="admin")
    headers = auth_headers(user)
    assert client.get("/probe/gated", headers=headers).status_code == 200

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert client.get("/probe/gated", headers=headers).status_code == 401


def test_an_expired_admin_session_is_a_401_not_a_403(app, client, make_user, auth_headers):
    _mount(app, "/probe/gated", auth_routes.require_role("admin")(lambda: jsonify({"ok": True})))
    user = make_user(role="admin")
    base = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    with time_machine.travel(base, tick=False):
        headers = auth_headers(user)

    with time_machine.travel(base + timedelta(days=31), tick=False):
        assert client.get("/probe/gated", headers=headers).status_code == 401


@pytest.mark.parametrize("listed", ["Admin", "ADMIN", "admin ", " admin", "admins"])
def test_the_role_match_is_exact_and_case_sensitive(app, client, make_user, auth_headers, listed):
    _mount(app, "/probe/gated", auth_routes.require_role(listed)(lambda: jsonify({"ok": True})))
    user = make_user(role="admin")

    assert client.get("/probe/gated", headers=auth_headers(user)).status_code == 403


def test_a_role_listed_twice_still_admits_it_once(app, client, make_user, auth_headers):
    _mount(app, "/probe/gated",
           auth_routes.require_role("admin", "admin")(lambda: jsonify({"ok": True})))
    user = make_user(role="admin")

    assert client.get("/probe/gated", headers=auth_headers(user)).status_code == 200


def test_the_role_gate_never_reaches_the_handler_on_a_refusal(app, client, make_user, auth_headers):
    reached = []
    _mount(app, "/probe/gated",
           auth_routes.require_role("admin")(lambda: reached.append(1) or jsonify({"ok": True})))
    user = make_user(role="student")

    assert client.get("/probe/gated", headers=auth_headers(user)).status_code == 403
    assert reached == []


def test_the_role_gate_hands_the_public_user_dict_to_the_handler(app, client, make_user, auth_headers):
    _mount(app, "/probe/gated",
           auth_routes.require_role("curator")(lambda: jsonify(sorted(request.user))))
    user = make_user(role="curator")

    response = client.get("/probe/gated", headers=auth_headers(user))

    assert set(response.get_json()) == _PUBLIC_COLUMNS


def test_the_role_gate_passes_the_url_arguments_through(app, client, make_user, auth_headers):
    _mount(app, "/probe/echo/<slug>",
           auth_routes.require_role("admin")(lambda slug: jsonify({"slug": slug})))
    user = make_user(role="admin")

    response = client.get("/probe/echo/labas", headers=auth_headers(user))

    assert response.get_json() == {"slug": "labas"}


def test_the_role_gate_returns_the_handlers_own_status(app, client, make_user, auth_headers):
    _mount(app, "/probe/created",
           auth_routes.require_role("admin")(lambda: (jsonify({"ok": True}), 201)))
    user = make_user(role="admin")

    assert client.post("/probe/created", headers=auth_headers(user)).status_code == 201


def test_the_role_gate_keeps_the_wrapped_functions_identity():
    def named_view():
        return None

    decorated = auth_routes.require_role("admin")(named_view)

    assert decorated.__name__ == "named_view"
    assert decorated.__wrapped__ is named_view


def test_the_privileged_roles_are_a_subset_of_the_one_role_whitelist():
    # require_role's contract: the names it is handed are the
    # users.role CHECK set, nothing else
    assert set(auth_routes.PRIVILEGED_ROLES) <= set(auth_routes.ROLES)
    assert set(auth_routes.ROLES) == {"student", "teacher", "admin", "curator"}


def test_an_unlisted_role_cannot_be_planted_in_the_users_table(db, make_user):
    # The gate compares against whatever users.role holds, and
    # the CHECK constraint is what keeps that inside ROLES
    user = make_user()

    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE users SET role = 'superadmin' WHERE id = ?", (user["id"],))
