# -----------------------------------------------------------
#  [*] Tests — the caller's own account
#
#  Covers the three routes a signed-in user points at itself:
#
#    GET  /api/auth/me              — the profile the mobile
#                                     app hydrates its session
#                                     from
#    PUT  /api/auth/me              — partial self-edit
#    POST /api/auth/change-password — credential rotation
#
#  What this module proves:
#
#    - the exact wire shape of _serialize_user: ten camelCase
#      keys and nothing else, so password_hash, active and the
#      timestamps can never leak into a response
#    - every one of require_auth's ways to say 401 — no header,
#      a foreign scheme, an unknown token, an expired session
#      (its push tokens purged with it) and a deactivated
#      account
#    - PUT /me's guard clauses one by one: blank and non-string
#      display names, the 100- and 50-character caps, blank →
#      NULL on the student-card fields, "no fields to update",
#      and a row that disappears mid-request
#    - avatarUrl is pinned to /api/uploads/ paths — an absolute
#      URL would beacon every avatar render to a host the user
#      picked — and a replaced own upload is handed to the
#      uploads package's delete helper, best-effort
#    - a display-name change rewrites the caller's news_posts
#      author_name snapshots in the same transaction
#    - PUT /me edits nothing but the whitelisted columns: a
#      "role": "admin" in the body is not a promotion
#    - change-password verifies the old password against a
#      fresh hash read, answers a WRONG one with 400 (not the
#      401 the client would tear its session down over), drops
#      every OTHER session, kicks the user's live sockets, and
#      rate-limits failures per user while successes stay free
# -----------------------------------------------------------


import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

ME = "/api/auth/me"
CHANGE_PASSWORD = "/api/auth/change-password"

# The ten keys _serialize_user hands out — mobile's
# services/api/auth.ts destructures exactly these
USER_KEYS = {"id", "username", "email", "displayName", "role", "avatarUrl",
             "invited", "studentNumber", "studyGroup", "studyProgram"}




# -----------------------------------------------------------
# clean_rate_limiter
# -----------------------------------------------------------
#
# The limiter's store is a MODULE global on monotonic stamps,
# so it outlives the per-test app and every test in the
# process shares the "global:127.0.0.1" bucket. Clearing it
# around each test keeps the 429 assertions below honest and
# stops this file's own bursts leaking into a sibling module.
#
# Used by:
#   - autouse: every test here
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_rate_limiter():
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# upload_deletions / socket_disconnects
# -----------------------------------------------------------
#
# Both side effects are reached through a lazy import INSIDE
# the helper (_delete_replaced_upload, _disconnect_user_sockets)
# — the attribute is looked up when the route fires, so
# patching it on the owning module is what the route really
# calls. Each fixture returns the list of arguments it saw.
#
# Used by:
#   - the avatar-replacement and change-password tests below
# -----------------------------------------------------------

@pytest.fixture
def upload_deletions(monkeypatch):
    import app.uploads.routes as uploads_routes

    calls = []
    monkeypatch.setattr(uploads_routes, "delete_upload", lambda path: bool(calls.append(path)))
    return calls


@pytest.fixture
def socket_disconnects(monkeypatch):
    import app.chat.events as chat_events

    calls = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", lambda user_id: calls.append(user_id))
    return calls




# -----------------------------------------------------------
# vanish_user
# -----------------------------------------------------------
#
# Arms a one-shot trap: the named auth-module hook deletes the
# caller's row the first time the route calls it, so the rest
# of the handler runs against a user that no longer exists.
# That is the only way to reach the "row deleted between the
# auth check and the re-read" guards — an admin deletion
# cannot be raced on purpose from outside.
#
# Used by:
#   - the two row-vanished tests below
# -----------------------------------------------------------

@pytest.fixture
def vanish_user(app, monkeypatch):

    def _arm(hook_name, user_id):
        import app.auth.routes as auth_routes

        original = getattr(auth_routes, hook_name)
        fired = []

        def _hook(*args, **kwargs):
            if not fired:
                fired.append(True)
                conn = sqlite3.connect(app.config["DB_PATH"])
                try:
                    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                    conn.commit()
                finally:
                    conn.close()
            return original(*args, **kwargs)

        monkeypatch.setattr(auth_routes, hook_name, _hook)

    return _arm




# -----------------------------------------------------------
# _upload_path
# -----------------------------------------------------------
#
# A path in the ONE shape uploads/routes.py hands out and
# create_app's avatar validator admits: 32 hex characters and
# an image extension. A hand-written "/api/uploads/a.jpg" is
# refused by the before_request hook and would never reach the
# route.
#
# Used by:
#   - every avatar test below
# -----------------------------------------------------------

def _upload_path():
    return f"/api/uploads/{uuid.uuid4().hex}.jpg"


def _user_row(db, user_id):
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _set_column(db, user_id, column, value):
    db.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (value, user_id))
    db.commit()




# -----------------------------------------------------------
# _put_me_directly
# -----------------------------------------------------------
#
# PUT /me's own avatar guard is a BACKSTOP: create_app's
# validate_json_input already refuses every value that would
# trip it (both key spellings), so no HTTP request can reach
# those two lines. This calls the view inside a request
# context — the decorators run, the before_request hooks do
# not — and returns (json, status) the way the test client
# would, so the route's second line of defence is still
# proven rather than assumed.
#
# Used by:
#   - the two backstop tests at the end of the PUT section
# -----------------------------------------------------------

def _put_me_directly(app, headers, body):
    from app.auth.routes import update_me

    with app.test_request_context(ME, method="PUT", json=body, headers=headers):
        result = update_me()
        response, status = result if isinstance(result, tuple) else (result, result.status_code)
        return response.get_json(), status




# -----------------------------------------------------------
# GET /api/auth/me
# -----------------------------------------------------------


@pytest.mark.contract
def test_me_returns_the_callers_own_profile(client, actor):
    user, headers = actor

    response = client.get(ME, headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == USER_KEYS
    assert body["id"] == user["id"]
    assert body["username"] == user["username"]
    assert body["email"] == user["email"]
    assert body["role"] == "student"
    assert body["avatarUrl"] is None
    assert body["invited"] is True


def test_me_never_leaks_the_password_hash_or_the_active_flag(client, actor):
    _, headers = actor

    body = client.get(ME, headers=headers).get_json()

    for leaked in ("password_hash", "passwordHash", "active", "created_at", "updated_at"):
        assert leaked not in body


def test_me_serializes_the_student_card_fields(client, db, actor):
    user, headers = actor
    db.execute("UPDATE users SET student_number = ?, study_group = ?, study_program = ?,"
               " avatar_url = ? WHERE id = ?",
               ("20231234", "IFF-1", "Informatikos sistemos", "/api/uploads/x.jpg", user["id"]))
    db.commit()

    body = client.get(ME, headers=headers).get_json()

    assert body["studentNumber"] == "20231234"
    assert body["studyGroup"] == "IFF-1"
    assert body["studyProgram"] == "Informatikos sistemos"
    assert body["avatarUrl"] == "/api/uploads/x.jpg"


def test_me_reports_a_privileged_role(client, make_user, auth_headers):
    curator = make_user(role="curator")

    body = client.get(ME, headers=auth_headers(curator)).get_json()

    assert body["role"] == "curator"


def test_me_marks_an_account_registered_without_a_code_as_not_invited(client, db, actor):
    user, headers = actor
    _set_column(db, user["id"], "invited", 0)

    assert client.get(ME, headers=headers).get_json()["invited"] is False


def test_me_escapes_the_display_name_on_output(client, db, actor):
    user, headers = actor
    _set_column(db, user["id"], "display_name", "Ona & Co <b>")

    body = client.get(ME, headers=headers).get_json()

    # Raw in the database, escaped on the wire — the mobile
    # client entity-decodes every response
    assert body["displayName"] == "Ona &amp; Co &lt;b&gt;"
    assert _user_row(db, user["id"])["display_name"] == "Ona & Co <b>"


def test_me_is_401_for_a_guest(client):
    response = client.get(ME)

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}


@pytest.mark.parametrize("header", [
    "",
    "Bearer",
    "Bearer    ",
    "Token abc123",
    "Basic dXNlcjpwYXNz",
])
def test_me_is_401_for_a_header_that_is_not_a_bearer_token(client, header):
    assert client.get(ME, headers={"Authorization": header}).status_code == 401


def test_me_accepts_the_bearer_scheme_in_any_case(client, actor):
    _, headers = actor
    token = headers["Authorization"].split(" ", 1)[1]

    assert client.get(ME, headers={"Authorization": f"bEaReR {token}"}).status_code == 200


def test_me_is_401_for_an_unknown_token(client, actor):
    assert client.get(ME, headers={"Authorization": f"Bearer {uuid.uuid4()}"}).status_code == 401


def test_me_is_401_for_a_deactivated_account(client, db, actor):
    user, headers = actor
    assert client.get(ME, headers=headers).status_code == 200

    _set_column(db, user["id"], "active", 0)

    # The session was minted before the flag flip and must die with it
    assert client.get(ME, headers=headers).status_code == 401


def test_me_is_401_once_the_session_expired_and_takes_the_push_tokens_with_it(client, db, actor):
    user, headers = actor
    db.execute("INSERT INTO push_tokens (id, user_id, token) VALUES (?, ?, ?)",
               ("push-1", user["id"], "ExponentPushToken[abc]"))
    db.commit()

    # Sessions last 30 days; day 31 is past every one of them
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=31), tick=False):
        response = client.get(ME, headers=headers)

    assert response.status_code == 401
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user["id"],)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM push_tokens WHERE user_id = ?", (user["id"],)).fetchone()[0] == 0


def test_me_is_401_when_the_session_expiry_is_unparseable(client, db, actor):
    user, headers = actor
    db.execute("UPDATE sessions SET expires_at = 'not-a-date' WHERE user_id = ?", (user["id"],))
    db.commit()

    assert client.get(ME, headers=headers).status_code == 401
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user["id"],)).fetchone()[0] == 0


def test_me_reads_a_legacy_naive_session_expiry_as_utc(client, db, actor):
    user, headers = actor
    naive_future = (datetime.now(timezone.utc) + timedelta(days=5)).replace(tzinfo=None).isoformat()
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (naive_future, user["id"]))
    db.commit()

    # Rows written before the aware stamps landed carry no
    # offset — they are UTC, not a 500 and not an instant 401
    assert client.get(ME, headers=headers).status_code == 200

    naive_past = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None).isoformat()
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (naive_past, user["id"]))
    db.commit()

    assert client.get(ME, headers=headers).status_code == 401


def test_me_is_401_when_the_session_outlives_its_user(client, db, actor):
    user, headers = actor
    # The row is gone but the session survives it — an FK-less
    # deletion through DbGate looks exactly like this
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    db.commit()

    response = client.get(ME, headers=headers)

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}


def test_the_caller_is_resolved_once_per_request(app, actor):
    from app.auth.routes import get_current_user

    user, headers = actor

    with app.test_request_context(ME, headers=headers):
        first = get_current_user()
        second = get_current_user()

    # The second resolution comes off flask.g, not off a second
    # database round trip — the SAME dict, not an equal one
    assert first is second
    assert first["id"] == user["id"]




# -----------------------------------------------------------
# _WriteLockedDb
# -----------------------------------------------------------
#
# A connection whose writes always report the database as
# locked. The real thing needs a second writer holding an
# exclusive transaction, and get_db's busy_timeout would make
# the test wait 30 seconds for it; this reaches the same
# branch instantly. Reads pass straight through.
#
# Used by:
#   - the expired-session purge test below
# -----------------------------------------------------------

class _WriteLockedDb:

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("DELETE"):
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def test_me_is_401_when_the_expired_session_purge_cannot_run(client, db, actor, monkeypatch):
    import app.auth.routes as auth_routes

    real_get_db = auth_routes.get_db
    monkeypatch.setattr(auth_routes, "get_db", lambda: _WriteLockedDb(real_get_db()))
    user, headers = actor

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=31), tick=False):
        response = client.get(ME, headers=headers)

    # The lazy purge is best-effort: a locked database must
    # yield the clean 401, never a 500
    assert response.status_code == 401
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user["id"],)).fetchone()[0] == 1


def test_the_rate_limit_store_cannot_grow_without_bound(app):
    from app.auth.routes import (_RATE_LIMIT_MAX_KEYS, _check_rate_limit,
                                 _rate_limit_store, _record_attempt)

    # No HTTP surface can mint four thousand distinct client
    # IPs inside a test, so the LRU ceiling is exercised at the
    # primitives — it is what keeps a spoofed X-Forwarded-For
    # flood from eating the process's memory
    for index in range(_RATE_LIMIT_MAX_KEYS + 50):
        assert _check_rate_limit(f"probe:{index}") is False

    assert len(_rate_limit_store) == _RATE_LIMIT_MAX_KEYS
    assert "probe:0" not in _rate_limit_store

    for index in range(50):
        _record_attempt(f"record:{index}")

    assert len(_rate_limit_store) == _RATE_LIMIT_MAX_KEYS




# -----------------------------------------------------------
# PUT /api/auth/me — display name
# -----------------------------------------------------------


def test_update_me_is_401_for_a_guest(client):
    response = client.put(ME, json={"displayName": "Guest"})

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}


def test_update_me_is_401_for_a_deactivated_account(client, db, actor):
    user, headers = actor
    _set_column(db, user["id"], "active", 0)

    assert client.put(ME, json={"displayName": "Vardas"}, headers=headers).status_code == 401


def test_update_me_requires_a_json_body(client, actor):
    _, headers = actor

    response = client.put(ME, data="{not json", content_type="application/json", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON body required"}


def test_update_me_rejects_an_empty_object(client, actor):
    _, headers = actor

    response = client.put(ME, json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_update_me_rejects_a_body_that_is_not_an_object(client, actor):
    _, headers = actor

    response = client.put(ME, json=["displayName", "Ona"], headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON body must be an object"}


def test_update_me_changes_the_display_name(client, db, actor):
    user, headers = actor

    response = client.put(ME, json={"displayName": "Ona Onaitiene"}, headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == USER_KEYS
    assert body["displayName"] == "Ona Onaitiene"
    assert _user_row(db, user["id"])["display_name"] == "Ona Onaitiene"


def test_update_me_accepts_the_snake_case_display_name_key(client, db, actor):
    user, headers = actor

    response = client.put(ME, json={"display_name": "Jonas Jonaitis"}, headers=headers)

    assert response.status_code == 200
    assert _user_row(db, user["id"])["display_name"] == "Jonas Jonaitis"


def test_update_me_prefers_the_camel_case_display_name_when_both_are_sent(client, db, actor):
    user, headers = actor

    client.put(ME, json={"displayName": "Camel", "display_name": "Snake"}, headers=headers)

    assert _user_row(db, user["id"])["display_name"] == "Camel"


def test_update_me_stores_the_display_name_stripped(client, db, actor):
    user, headers = actor

    body = client.put(ME, json={"displayName": "   Ona   "}, headers=headers).get_json()

    assert body["displayName"] == "Ona"
    assert _user_row(db, user["id"])["display_name"] == "Ona"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_update_me_rejects_a_blank_display_name(client, db, actor, blank):
    user, headers = actor
    before = _user_row(db, user["id"])["display_name"]

    response = client.put(ME, json={"displayName": blank}, headers=headers)

    # A present-but-blank name is a 400, never a silent skip
    # answering 200 with the old name
    assert response.status_code == 400
    assert response.get_json() == {"error": "Display name cannot be empty"}
    assert _user_row(db, user["id"])["display_name"] == before


@pytest.mark.parametrize("value", [123, None, True, {"lt": "Ona"}, ["Ona"]])
def test_update_me_rejects_a_non_string_display_name(client, actor, value):
    _, headers = actor

    response = client.put(ME, json={"displayName": value}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "display_name must be a string"}


def test_update_me_accepts_a_display_name_of_exactly_one_hundred_characters(client, db, actor):
    user, headers = actor
    name = "a" * 100

    assert client.put(ME, json={"displayName": name}, headers=headers).status_code == 200
    assert _user_row(db, user["id"])["display_name"] == name


def test_update_me_rejects_a_display_name_over_one_hundred_characters(client, actor):
    _, headers = actor

    response = client.put(ME, json={"displayName": "a" * 101}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Display name must be at most 100 characters"}


def test_update_me_rewrites_the_authors_news_snapshots(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    db.execute("INSERT INTO news_posts (id, title, content, author_id, author_name)"
               " VALUES (?, ?, ?, ?, ?)", ("post-mine", "Naujiena", "Turinys", user["id"], "Senas Vardas"))
    db.execute("INSERT INTO news_posts (id, title, content, author_id, author_name)"
               " VALUES (?, ?, ?, ?, ?)", ("post-other", "Kita", "Turinys", other["id"], "Kitas Autorius"))
    db.commit()

    client.put(ME, json={"displayName": "Naujas Vardas"}, headers=headers)

    rows = {r["id"]: r["author_name"] for r in db.execute("SELECT id, author_name FROM news_posts")}
    assert rows["post-mine"] == "Naujas Vardas"
    assert rows["post-other"] == "Kitas Autorius"


def test_update_me_leaves_the_news_snapshots_alone_when_the_name_is_untouched(client, db, actor):
    user, headers = actor
    db.execute("INSERT INTO news_posts (id, title, content, author_id, author_name)"
               " VALUES (?, ?, ?, ?, ?)", ("post-1", "Naujiena", "Turinys", user["id"], "Senas Vardas"))
    db.commit()

    client.put(ME, json={"studyGroup": "IFF-2"}, headers=headers)

    assert db.execute("SELECT author_name FROM news_posts WHERE id = 'post-1'").fetchone()[0] == "Senas Vardas"


def test_update_me_stamps_updated_at_in_the_house_t_form(client, db, actor):
    user, headers = actor
    before = _user_row(db, user["id"])["updated_at"]

    client.put(ME, json={"displayName": "Ona"}, headers=headers)

    after = _user_row(db, user["id"])["updated_at"]
    assert after != before
    # T-form UTC, not the space-form the column DEFAULT writes
    assert "T" in after and after.endswith("+00:00")


def test_update_me_edits_nothing_outside_the_whitelist(client, db, actor):
    user, headers = actor

    response = client.put(ME, json={
        "displayName": "Ona",
        "role": "admin",
        "invited": True,
        "active": 0,
        "username": "root",
        "email": "root@knf.vu.lt",
        "id": "somebody-else",
    }, headers=headers)

    assert response.status_code == 200
    row = _user_row(db, user["id"])
    assert row["role"] == "student"
    assert row["username"] == user["username"]
    assert row["email"] == user["email"]
    assert row["active"] == 1
    assert row["invited"] == 1


def test_update_me_rejects_a_body_with_no_known_field(client, actor):
    _, headers = actor

    response = client.put(ME, json={"nickname": "Ona", "theme": "dark"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "No fields to update"}


def test_update_me_is_401_when_the_row_vanishes_mid_request(client, db, actor, vanish_user):
    user, headers = actor
    vanish_user("utc_now_iso", user["id"])

    response = client.put(ME, json={"displayName": "Ona"}, headers=headers)

    # The session-dead 401 the client already handles — never a
    # TypeError 500 on the re-read
    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}
    assert _user_row(db, user["id"]) is None




# -----------------------------------------------------------
# PUT /api/auth/me — avatar
# -----------------------------------------------------------


def test_update_me_sets_an_own_upload_as_the_avatar(client, db, actor):
    user, headers = actor
    path = _upload_path()

    body = client.put(ME, json={"avatarUrl": path}, headers=headers).get_json()

    assert body["avatarUrl"] == path
    assert _user_row(db, user["id"])["avatar_url"] == path


def test_update_me_accepts_the_snake_case_avatar_key(client, db, actor):
    user, headers = actor
    path = _upload_path()

    assert client.put(ME, json={"avatar_url": path}, headers=headers).status_code == 200
    assert _user_row(db, user["id"])["avatar_url"] == path


@pytest.mark.parametrize("cleared", [None, ""])
def test_update_me_clears_the_avatar(client, db, actor, cleared):
    user, headers = actor
    _set_column(db, user["id"], "avatar_url", _upload_path())

    body = client.put(ME, json={"avatarUrl": cleared}, headers=headers).get_json()

    # Both spellings of "no avatar" read as cleared by the client
    assert body["avatarUrl"] in (None, "")
    assert not _user_row(db, user["id"])["avatar_url"]


@pytest.mark.parametrize("value", [
    "https://evil.example/beacon.png",
    "http://knf.vu.lt/logo.png",
    "//evil.example/beacon.png",
    "javascript:alert(1)",
    "data:image/png;base64,AAAA",
    "/api/uploads/../../etc/passwd",
    "/api/uploads/abc.jpg?x=//evil.example",
    "/uploads/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg",
])
def test_update_me_rejects_an_avatar_that_is_not_an_own_upload(client, db, actor, value):
    user, headers = actor
    _set_column(db, user["id"], "avatar_url", "/api/uploads/keep.jpg")

    response = client.put(ME, json={"avatarUrl": value}, headers=headers)

    # An absolute URL would beacon every avatar render to a
    # host the user picked
    assert response.status_code == 400
    assert response.get_json() == {"error": "avatar_url must be a relative /api/uploads/ path"}
    assert _user_row(db, user["id"])["avatar_url"] == "/api/uploads/keep.jpg"


@pytest.mark.parametrize("value", [12345, True, ["/api/uploads/a.jpg"], {"url": "/api/uploads/a.jpg"}])
def test_update_me_rejects_a_non_string_avatar(client, actor, value):
    _, headers = actor

    response = client.put(ME, json={"avatarUrl": value}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "avatar_url must be a string"}


def test_update_me_rejects_an_absurdly_long_avatar_url(client, actor):
    _, headers = actor

    response = client.put(ME, json={"avatarUrl": "/api/uploads/" + "a" * 2100}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "avatar_url must be at most 2048 characters"


def test_update_me_deletes_the_replaced_upload(client, db, actor, upload_deletions):
    user, headers = actor
    old = _upload_path()
    _set_column(db, user["id"], "avatar_url", old)

    response = client.put(ME, json={"avatarUrl": _upload_path()}, headers=headers)

    assert response.status_code == 200
    assert upload_deletions == [old]


def test_update_me_deletes_the_upload_when_the_avatar_is_cleared(client, db, actor, upload_deletions):
    user, headers = actor
    old = _upload_path()
    _set_column(db, user["id"], "avatar_url", old)

    client.put(ME, json={"avatarUrl": None}, headers=headers)

    assert upload_deletions == [old]


def test_update_me_keeps_the_upload_when_the_avatar_is_unchanged(client, db, actor, upload_deletions):
    user, headers = actor
    path = _upload_path()
    _set_column(db, user["id"], "avatar_url", path)

    client.put(ME, json={"avatarUrl": path}, headers=headers)

    assert upload_deletions == []


def test_update_me_deletes_nothing_when_there_was_no_avatar(client, actor, upload_deletions):
    _, headers = actor

    client.put(ME, json={"avatarUrl": _upload_path()}, headers=headers)

    assert upload_deletions == []


def test_update_me_never_deletes_an_avatar_it_does_not_own(client, db, actor, upload_deletions):
    user, headers = actor
    # A legacy row can still hold an absolute URL; only our own
    # uploads are ours to unlink
    _set_column(db, user["id"], "avatar_url", "https://cdn.example/old.png")

    client.put(ME, json={"avatarUrl": _upload_path()}, headers=headers)

    assert upload_deletions == []


def test_update_me_survives_a_failing_upload_delete(client, db, actor, monkeypatch):
    import app.uploads.routes as uploads_routes

    def _boom(path):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(uploads_routes, "delete_upload", _boom)
    user, headers = actor
    _set_column(db, user["id"], "avatar_url", _upload_path())
    new = _upload_path()

    response = client.put(ME, json={"avatarUrl": new}, headers=headers)

    # Disk cleanup is best-effort — it must never fail the
    # profile update that triggered it
    assert response.status_code == 200
    assert _user_row(db, user["id"])["avatar_url"] == new


def test_update_me_survives_a_missing_uploads_helper(client, db, actor, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.uploads.routes", None)
    user, headers = actor
    _set_column(db, user["id"], "avatar_url", _upload_path())

    response = client.put(ME, json={"avatarUrl": _upload_path()}, headers=headers)

    assert response.status_code == 200


def test_update_me_guard_refuses_an_absolute_avatar_url_without_the_middleware(app, actor):
    _, headers = actor

    body, status = _put_me_directly(app, headers, {"avatarUrl": "https://evil.example/beacon.png"})

    assert status == 400
    assert body == {"error": "avatar_url must be a relative /api/uploads/ path"}


def test_update_me_guard_refuses_a_non_string_avatar_without_the_middleware(app, actor):
    _, headers = actor

    body, status = _put_me_directly(app, headers, {"avatar_url": 42})

    assert status == 400
    assert body == {"error": "avatar_url must be a relative /api/uploads/ path"}




# -----------------------------------------------------------
# PUT /api/auth/me — student-card fields
# -----------------------------------------------------------


def test_update_me_sets_every_student_card_field(client, db, actor):
    user, headers = actor

    body = client.put(ME, json={
        "studentNumber": "20231234",
        "studyGroup": "IFF-1",
        "studyProgram": "Informatikos sistemos",
    }, headers=headers).get_json()

    assert body["studentNumber"] == "20231234"
    assert body["studyGroup"] == "IFF-1"
    assert body["studyProgram"] == "Informatikos sistemos"
    row = _user_row(db, user["id"])
    assert row["student_number"] == "20231234"
    assert row["study_group"] == "IFF-1"
    assert row["study_program"] == "Informatikos sistemos"


def test_update_me_accepts_the_snake_case_student_card_keys(client, db, actor):
    user, headers = actor

    response = client.put(ME, json={
        "student_number": "20239999",
        "study_group": "IFF-9",
        "study_program": "Verslo informatika",
    }, headers=headers)

    assert response.status_code == 200
    row = _user_row(db, user["id"])
    assert row["student_number"] == "20239999"
    assert row["study_group"] == "IFF-9"
    assert row["study_program"] == "Verslo informatika"


def test_update_me_trims_a_student_card_field(client, db, actor):
    user, headers = actor

    body = client.put(ME, json={"studentNumber": "  20231234  "}, headers=headers).get_json()

    assert body["studentNumber"] == "20231234"
    assert _user_row(db, user["id"])["student_number"] == "20231234"


@pytest.mark.parametrize("cleared", [None, "", "   "])
def test_update_me_stores_a_blank_student_card_field_as_null(client, db, actor, cleared):
    user, headers = actor
    _set_column(db, user["id"], "study_group", "IFF-1")

    body = client.put(ME, json={"studyGroup": cleared}, headers=headers).get_json()

    assert body["studyGroup"] is None
    assert _user_row(db, user["id"])["study_group"] is None


def test_update_me_accepts_a_student_card_field_of_exactly_fifty_characters(client, db, actor):
    user, headers = actor
    value = "p" * 50

    assert client.put(ME, json={"studyProgram": value}, headers=headers).status_code == 200
    assert _user_row(db, user["id"])["study_program"] == value


@pytest.mark.parametrize("field", ["studentNumber", "studyGroup", "studyProgram",
                                   "student_number", "study_group", "study_program"])
def test_update_me_rejects_a_student_card_field_over_fifty_characters(client, actor, field):
    _, headers = actor

    response = client.put(ME, json={field: "x" * 51}, headers=headers)

    assert response.status_code == 400
    # The error names the key the CLIENT sent, not the column
    assert response.get_json() == {"error": f"{field} must be at most 50 characters"}


@pytest.mark.parametrize("field,value", [
    ("studentNumber", 20231234),
    ("studyGroup", ["IFF-1"]),
    ("study_program", {"lt": "Informatika"}),
])
def test_update_me_rejects_a_non_string_student_card_field(client, actor, field, value):
    _, headers = actor

    response = client.put(ME, json={field: value}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": f"{field} must be a string"}


def test_update_me_applies_every_accepted_field_in_one_call(client, db, actor):
    user, headers = actor
    path = _upload_path()

    body = client.put(ME, json={
        "displayName": "Ona Onaitiene",
        "avatarUrl": path,
        "studentNumber": "20231234",
        "studyGroup": "IFF-1",
        "studyProgram": "Informatikos sistemos",
    }, headers=headers).get_json()

    assert body["displayName"] == "Ona Onaitiene"
    assert body["avatarUrl"] == path
    row = _user_row(db, user["id"])
    assert row["display_name"] == "Ona Onaitiene"
    assert row["avatar_url"] == path
    assert row["student_number"] == "20231234"


def test_update_me_shows_up_on_the_next_get_me(client, actor):
    _, headers = actor
    client.put(ME, json={"displayName": "Ona", "studyGroup": "IFF-3"}, headers=headers)

    body = client.get(ME, headers=headers).get_json()

    assert body["displayName"] == "Ona"
    assert body["studyGroup"] == "IFF-3"




# -----------------------------------------------------------
# POST /api/auth/change-password
# -----------------------------------------------------------


def test_change_password_is_401_for_a_guest(client):
    response = client.post(CHANGE_PASSWORD, json={"old_password": "a", "new_password": "b"})

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}


def test_change_password_requires_a_json_body(client, actor):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD, data="{nope", content_type="application/json", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON body required"}


@pytest.mark.parametrize("body", [
    {},
    {"old_password": "slaptazodis123"},
    {"new_password": "NaujasSlaptas1"},
    {"old_password": "", "new_password": "NaujasSlaptas1"},
    {"old_password": "slaptazodis123", "new_password": ""},
])
def test_change_password_requires_both_passwords(client, actor, body):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD, json=body, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] in ("JSON body required", "Old and new password required")


@pytest.mark.parametrize("old,new", [
    (12345678, "NaujasSlaptas1"),
    ("slaptazodis123", 87654321),
    (["slaptazodis123"], {"new": 1}),
])
def test_change_password_rejects_non_string_passwords(client, actor, old, new):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD, json={"old_password": old, "new_password": new}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Old and new password must be strings"}


@pytest.mark.parametrize("new_password,reason", [
    ("abc12", "Password must be at least 6 characters"),
    ("x" * 73, "Password must be at most 72 characters"),
    # 37 two-byte characters = 74 BYTES: the cap counts bytes,
    # because bcrypt silently truncates past 72 of them
    ("ą" * 37, "Password must be at most 72 characters"),
    ("password123", "Password is too common"),
    ("ADMIN123", "Password is too common"),
])
def test_change_password_screens_the_new_password(client, actor, new_password, reason):
    user, headers = actor

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"],
        "new_password": new_password,
    }, headers=headers)

    assert response.status_code == 400
    body = response.get_json()
    assert body["code"] == "weak_password"
    assert body["error"] == reason


def test_change_password_accepts_exactly_seventy_two_bytes(client, make_user, auth_headers):
    user = make_user(username="ona.o")
    # 36 two-byte characters = the 72-byte bcrypt ceiling
    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"],
        "new_password": "ą" * 36,
    }, headers=auth_headers(user))

    assert response.status_code == 200


def test_change_password_rejects_a_new_password_containing_the_username(client, make_user, auth_headers):
    user = make_user(username="jonas.petras")

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"],
        "new_password": "Jonas.Petras2026",
    }, headers=auth_headers(user))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Password must not contain your username"


def test_change_password_rejects_a_new_password_containing_the_email_local_part(client, db, make_user, auth_headers):
    user = make_user(username="zyx.qwe")
    headers = auth_headers(user)
    db.execute("UPDATE users SET email = 'kaunas@knf.vu.lt' WHERE id = ?", (user["id"],))
    db.commit()

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"],
        "new_password": "KaunasVU2026",
    }, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Password must not contain your email"


def test_change_password_ignores_an_email_local_part_under_three_characters(client, db, make_user, auth_headers):
    user = make_user(username="zyx.qwe")
    headers = auth_headers(user)
    # A two-character local part is too noisy to screen on
    db.execute("UPDATE users SET email = 'ab@knf.vu.lt' WHERE id = ?", (user["id"],))
    db.commit()

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"],
        "new_password": "ab123456",
    }, headers=headers)

    assert response.status_code == 200


def test_change_password_rejects_a_wrong_old_password_with_400_not_401(client, actor):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": "visiskai-kitas",
        "new_password": "NaujasSlaptas1",
    }, headers=headers)

    # 401 would make the mobile client tear the whole login
    # down over a typo — this must stay a 400
    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid credentials", "code": "invalid_credentials"}


def test_change_password_leaves_the_old_hash_alone_when_the_old_password_is_wrong(client, db, actor):
    user, headers = actor
    before = _user_row(db, user["id"])["password_hash"]

    client.post(CHANGE_PASSWORD, json={
        "old_password": "visiskai-kitas",
        "new_password": "NaujasSlaptas1",
    }, headers=headers)

    assert _user_row(db, user["id"])["password_hash"] == before


def test_change_password_rotates_the_credential(client, db, actor):
    user, headers = actor
    before = _user_row(db, user["id"])["password_hash"]

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"],
        "new_password": "NaujasSlaptas1",
    }, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"message": "Password changed"}
    assert _user_row(db, user["id"])["password_hash"] != before

    assert client.post("/api/auth/login", json={
        "username": user["username"], "password": user["password"]}).status_code == 401
    assert client.post("/api/auth/login", json={
        "username": user["username"], "password": "NaujasSlaptas1"}).status_code == 200


def test_change_password_accepts_the_camel_case_keys(client, actor):
    user, headers = actor

    response = client.post(CHANGE_PASSWORD, json={
        "oldPassword": user["password"],
        "newPassword": "NaujasSlaptas1",
    }, headers=headers)

    assert response.status_code == 200


def test_change_password_stamps_updated_at(client, db, actor):
    user, headers = actor
    before = _user_row(db, user["id"])["updated_at"]

    client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"], "new_password": "NaujasSlaptas1"}, headers=headers)

    after = _user_row(db, user["id"])["updated_at"]
    assert after != before
    assert "T" in after


def test_change_password_drops_every_other_session_but_keeps_the_current_one(client, db, make_user, auth_headers):
    user = make_user()
    phone = auth_headers(user)
    laptop = auth_headers(user)
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user["id"],)).fetchone()[0] == 2

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"], "new_password": "NaujasSlaptas1"}, headers=laptop)

    assert response.status_code == 200
    # A compromised credential dies everywhere except the
    # device doing the rotation
    assert client.get(ME, headers=laptop).status_code == 200
    assert client.get(ME, headers=phone).status_code == 401
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user["id"],)).fetchone()[0] == 1


def test_change_password_leaves_other_users_sessions_alone(client, db, actor, make_user, auth_headers):
    user, headers = actor
    bystander = make_user()
    bystander_headers = auth_headers(bystander)

    client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"], "new_password": "NaujasSlaptas1"}, headers=headers)

    assert client.get(ME, headers=bystander_headers).status_code == 200


def test_change_password_disconnects_the_users_live_sockets(client, actor, socket_disconnects):
    user, headers = actor

    client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"], "new_password": "NaujasSlaptas1"}, headers=headers)

    # A socket authenticates once at handshake — without this
    # a revoked token keeps its realtime feed alive
    assert socket_disconnects == [user["id"]]


def test_change_password_survives_a_failing_socket_disconnect(client, actor, monkeypatch):
    import app.chat.events as chat_events

    def _boom(user_id):
        raise RuntimeError("socket layer down")

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _boom)
    user, headers = actor

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"], "new_password": "NaujasSlaptas1"}, headers=headers)

    assert response.status_code == 200


def test_change_password_survives_a_missing_socket_layer(client, actor, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.chat.events", None)
    user, headers = actor

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"], "new_password": "NaujasSlaptas1"}, headers=headers)

    assert response.status_code == 200


def test_change_password_is_400_when_the_row_vanishes_mid_request(client, actor, vanish_user):
    user, headers = actor
    vanish_user("_validate_new_password", user["id"])

    response = client.post(CHANGE_PASSWORD, json={
        "old_password": user["password"], "new_password": "NaujasSlaptas1"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_credentials"




# -----------------------------------------------------------
# POST /api/auth/change-password — the failure budget
# -----------------------------------------------------------
#
# Verifying an old password is a password oracle, so failures
# are limited per user exactly like login's. Both tests here
# spend real bcrypt work — hence @slow — because the point is
# what the ROUTE counts, not what the store holds.
# -----------------------------------------------------------


@pytest.mark.slow
def test_change_password_rate_limits_repeated_wrong_old_passwords(client, actor):
    _, headers = actor
    wrong = {"old_password": "ne-tas-slaptazodis", "new_password": "NaujasSlaptas1"}

    with time_machine.travel(datetime.now(timezone.utc), tick=False) as traveller:
        for attempt in range(10):
            assert client.post(CHANGE_PASSWORD, json=wrong, headers=headers).status_code == 400, attempt

        blocked = client.post(CHANGE_PASSWORD, json=wrong, headers=headers)
        assert blocked.status_code == 429
        assert blocked.get_json() == {"error": "Too many attempts. Please wait a few minutes.",
                                      "code": "rate_limited"}
        assert 1 <= int(blocked.headers["Retry-After"]) <= 301

        # The window is 5 minutes wide and ages out on its own
        traveller.shift(timedelta(seconds=301))
        assert client.post(CHANGE_PASSWORD, json=wrong, headers=headers).status_code == 400


@pytest.mark.slow
def test_change_password_successes_never_spend_the_budget(client, actor):
    user, headers = actor
    current = user["password"]

    # Eleven honest rotations — one more than the budget a
    # failure-counting limiter would have burned through
    for index in range(11):
        new_password = f"Kaunas{index}Slaptas"
        response = client.post(CHANGE_PASSWORD, json={
            "old_password": current, "new_password": new_password}, headers=headers)
        assert response.status_code == 200, (index, response.get_json())
        current = new_password


def test_change_password_budget_is_per_user(client, actor, make_user, auth_headers):
    _, headers = actor
    neighbour = make_user()
    neighbour_headers = auth_headers(neighbour)
    wrong = {"old_password": "ne-tas-slaptazodis", "new_password": "NaujasSlaptas1"}

    for _ in range(3):
        client.post(CHANGE_PASSWORD, json=wrong, headers=headers)

    # The neighbour's own budget is untouched by our failures
    response = client.post(CHANGE_PASSWORD, json={
        "old_password": neighbour["password"], "new_password": "NaujasSlaptas1"},
        headers=neighbour_headers)
    assert response.status_code == 200
