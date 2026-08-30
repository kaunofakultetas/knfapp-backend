# -----------------------------------------------------------
#  [*] Tests — the self-profile slice, branch by branch
#
#  A gap-closing companion to test_auth_me.py over exactly
#  five functions of app/auth/routes.py:
#
#    _serialize_user          — the public user shape
#    me                       — GET  /api/auth/me
#    update_me                — PUT  /api/auth/me
#    _delete_replaced_upload  — the avatar disk cleanup
#    change_password          — POST /api/auth/change-password
#
#  Line coverage was already complete; what this module adds
#  is the arms the line count cannot see:
#
#    - _serialize_user as a pure function: the ten-key
#      whitelist, the .get() fallbacks, the invited default
#      that fires ONLY on a missing key, and the dict-not-Row
#      precondition every call site honours with dict(...)
#    - each camelCase/snake_case key pair in BOTH directions —
#      the camel key wins even when its value is the invalid
#      one, so the snake spelling is never a way round a guard
#    - every cap at 49/50/51 and 99/100/101, measured in
#      characters (not bytes) and AFTER strip()
#    - the guard ORDER: display name before avatar before the
#      student-card loop, and the loop's own first-offender
#      rule — plus the promise that a rejected request writes
#      nothing at all, snapshots included
#    - the avatar backstop reached by calling the view inside a
#      request context: create_app's before_request refuses
#      every value that would trip it, so those two lines have
#      no HTTP route to them
#    - _delete_replaced_upload's three exits (no helper, a
#      throwing helper, a working one) and WHERE in update_me
#      it fires: after the commit, never on the vanished-row
#      401
#    - change_password's password policy as the gate that runs
#      BEFORE the bcrypt check, so a weak new password never
#      spends the failure budget, and the 10th failure / 11th
#      request rate-limit boundary
# -----------------------------------------------------------


import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

ME = "/api/auth/me"
CHANGE_PASSWORD = "/api/auth/change-password"
LOGIN = "/api/auth/login"

# The ten camelCase keys _serialize_user hands out, and
# nothing else — mobile's services/api/auth.ts destructures
# exactly these
USER_KEYS = {"id", "username", "email", "displayName", "role", "avatarUrl",
             "invited", "studentNumber", "studyGroup", "studyProgram"}

# Columns PUT /me must never be able to write, whatever the
# body says
FORBIDDEN_COLUMNS = ("id", "username", "email", "role", "invited", "active", "password_hash")




# -----------------------------------------------------------
# clean_rate_limiter
# -----------------------------------------------------------
#
# The limiter's store is a MODULE global keyed on monotonic
# stamps, so it outlives the per-test app and every test in
# the process shares "global:127.0.0.1". Clearing it around
# each test keeps the 429 boundaries below exact and stops
# this file's bursts leaking into a sibling module.
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
# upload_deletes / socket_kicks
# -----------------------------------------------------------
#
# Both side effects run through a lazy import INSIDE the
# helper, so the attribute is resolved when the route fires:
# patching it on the OWNING module is what the route really
# calls. Each fixture hands back the argument list it saw.
#
# Used by:
#   - the avatar-replacement and change-password tests below
# -----------------------------------------------------------

@pytest.fixture
def upload_deletes(monkeypatch):
    import app.uploads.routes as uploads_routes

    seen = []
    monkeypatch.setattr(uploads_routes, "delete_upload", lambda path: seen.append(path) or True)
    return seen


@pytest.fixture
def socket_kicks(monkeypatch):
    import app.chat.events as chat_events

    seen = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", lambda user_id: seen.append(user_id))
    return seen




# -----------------------------------------------------------
# mid_request
# -----------------------------------------------------------
#
#   mid_request("utc_now_iso", lambda conn: conn.execute(...))
#
# Arms a one-shot trap on an auth-module helper: the first
# time the route calls it, the callback runs against its own
# connection to the same database and the rest of the handler
# then works on state that changed under it. utc_now_iso is
# the useful hook — update_me and change_password both call it
# after their guards and before their write, which is exactly
# the window a racing admin action would land in.
#
# Used by:
#   - the vanished-row and stale-copy tests below
# -----------------------------------------------------------

@pytest.fixture
def mid_request(app, monkeypatch):

    def _arm(hook_name, callback):
        import app.auth.routes as auth_routes

        original = getattr(auth_routes, hook_name)
        fired = []

        def _hook(*args, **kwargs):
            if not fired:
                fired.append(True)
                conn = sqlite3.connect(app.config["DB_PATH"], timeout=15)
                try:
                    callback(conn)
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
# The ONE avatar shape create_app's before_request admits: 32
# hex characters plus an extension uploads/routes.py actually
# writes. A hand-written "/api/uploads/a.jpg" never reaches
# the route at all.
#
# Used by:
#   - every avatar test below
# -----------------------------------------------------------

def _upload_path(ext="jpg"):
    return f"/api/uploads/{uuid.uuid4().hex}.{ext}"


def _row(db, user_id):
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _set(db, user_id, column, value):
    db.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (value, user_id))
    db.commit()




# -----------------------------------------------------------
# _put_raw / _post_raw
# -----------------------------------------------------------
#
# TESTPLAN rule 10: a `json=` kwarg is serialised through the
# app's own provider, which html-escapes every string, so the
# bytes on the wire are already escaped and no assertion about
# markup or entities would mean anything. These post the exact
# bytes a real client sends.
#
# Used by:
#   - the escaping / NUL-byte / trailing-newline tests below
# -----------------------------------------------------------

def _put_raw(client, headers, payload):
    return client.put(ME, data=json.dumps(payload),
                      headers={**headers, "Content-Type": "application/json"})


def _post_raw(client, path, headers, payload):
    return client.post(path, data=json.dumps(payload),
                       headers={**headers, "Content-Type": "application/json"})




# -----------------------------------------------------------
# _call_update_me
# -----------------------------------------------------------
#
# update_me's avatar guard is a BACKSTOP: create_app's
# validate_json_input already refuses every value that would
# trip it, in both key spellings, so no HTTP request can reach
# those two lines. Calling the view inside a request context
# runs the decorators but not the before_request hooks, which
# is the only way to prove the second line of defence rather
# than assume it.
#
# Used by:
#   - the backstop tests at the end of the PUT section
# -----------------------------------------------------------

def _call_update_me(app, headers, body):
    from app.auth.routes import update_me

    with app.test_request_context(ME, method="PUT", json=body, headers=headers):
        result = update_me()
        response, status = result if isinstance(result, tuple) else (result, result.status_code)
        return response.get_json(), status




# -----------------------------------------------------------
# _serialize_user — the shape, as a pure function
# -----------------------------------------------------------


def test_serialize_user_maps_every_stored_column_to_its_camel_case_key():
    from app.auth.routes import _serialize_user

    payload = _serialize_user({
        "id": "u1", "username": "ona", "email": "ona@knf.vu.lt",
        "display_name": "Ona O.", "role": "curator", "avatar_url": "/api/uploads/x.png",
        "invited": 1, "student_number": "1911234", "study_group": "PS-1",
        "study_program": "Programų sistemos",
    })

    assert payload == {
        "id": "u1", "username": "ona", "email": "ona@knf.vu.lt",
        "displayName": "Ona O.", "role": "curator", "avatarUrl": "/api/uploads/x.png",
        "invited": True, "studentNumber": "1911234", "studyGroup": "PS-1",
        "studyProgram": "Programų sistemos",
    }


def test_serialize_user_returns_exactly_the_ten_public_keys():
    from app.auth.routes import _serialize_user

    assert set(_serialize_user({}).keys()) == USER_KEYS


def test_serialize_user_drops_every_column_outside_the_whitelist():
    from app.auth.routes import _serialize_user

    payload = _serialize_user({
        "id": "u1", "password_hash": "$2b$12$secret", "active": 1,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-02T00:00:00+00:00",
        "some_future_column": "leak",
    })

    assert set(payload.keys()) == USER_KEYS
    assert "$2b$12$secret" not in json.dumps(payload)


def test_serialize_user_fills_every_missing_column_with_none():
    from app.auth.routes import _serialize_user

    payload = _serialize_user({})

    assert all(payload[key] is None for key in USER_KEYS - {"invited"})


@pytest.mark.parametrize("stored,expected", [
    (1, True), (0, False), (None, False), ("", False), (2, True), ("0", True), (True, True),
])
def test_serialize_user_coerces_invited_to_a_bool(stored, expected):
    from app.auth.routes import _serialize_user

    assert _serialize_user({"invited": stored})["invited"] is expected


def test_serialize_user_defaults_invited_to_true_only_when_the_key_is_absent():
    from app.auth.routes import _serialize_user

    # A partial dict (register builds one) means "not a guest";
    # an explicit 0 is still a guest
    assert _serialize_user({})["invited"] is True
    assert _serialize_user({"invited": 0})["invited"] is False


def test_serialize_user_does_not_mutate_the_dict_it_is_handed():
    from app.auth.routes import _serialize_user

    source = {"id": "u1", "display_name": "Ona"}
    _serialize_user(source)

    assert source == {"id": "u1", "display_name": "Ona"}


def test_serialize_user_needs_a_dict_because_a_row_has_no_get(db, actor):
    from app.auth.routes import _serialize_user

    user, _ = actor
    row = _row(db, user["id"])

    # Every call site wraps the row in dict() for exactly this
    # reason — the banner's promise, pinned
    with pytest.raises(AttributeError):
        _serialize_user(row)
    assert _serialize_user(dict(row))["id"] == user["id"]




# -----------------------------------------------------------
# GET /api/auth/me
# -----------------------------------------------------------


@pytest.mark.parametrize("role", ["student", "teacher", "admin", "curator"])
def test_me_reports_each_role_in_the_whitelist(client, make_user, auth_headers, role):
    user = make_user(role=role)

    response = client.get(ME, headers=auth_headers(user))

    assert response.status_code == 200
    assert response.get_json()["role"] == role


@pytest.mark.contract
def test_me_answers_exactly_the_ten_keys_and_no_more(client, actor):
    _, headers = actor

    body = client.get(ME, headers=headers).get_json()

    assert set(body.keys()) == USER_KEYS


def test_me_returns_a_stored_avatar_verbatim(client, db, actor):
    user, headers = actor
    path = _upload_path("webp")
    _set(db, user["id"], "avatar_url", path)

    assert client.get(ME, headers=headers).get_json()["avatarUrl"] == path


def test_me_returns_null_student_card_fields_on_a_fresh_account(client, actor):
    _, headers = actor

    body = client.get(ME, headers=headers).get_json()

    assert body["studentNumber"] is None
    assert body["studyGroup"] is None
    assert body["studyProgram"] is None


def test_me_reflects_an_edit_made_straight_in_the_database(client, db, actor):
    user, headers = actor
    _set(db, user["id"], "display_name", "Perrašyta")

    # require_auth re-reads the row every request — /me is never
    # answered from a cached copy of the session's user
    assert client.get(ME, headers=headers).get_json()["displayName"] == "Perrašyta"


def test_me_reports_invited_false_for_an_account_registered_without_a_code(client, db, actor):
    user, headers = actor
    _set(db, user["id"], "invited", 0)

    assert client.get(ME, headers=headers).get_json()["invited"] is False


def test_me_ignores_a_json_body_on_the_get(client, actor):
    _, headers = actor

    response = client.get(ME, data=json.dumps({"role": "admin"}),
                          headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 200
    assert response.get_json()["role"] == "student"


@pytest.mark.parametrize("method", ["post", "patch"])
def test_me_serves_only_get_put_and_delete(client, actor, method):
    # DELETE grew a handler (self-service GDPR erasure —
    # test_account_erasure_deep.py owns its behaviour)
    _, headers = actor

    assert getattr(client, method)(ME, headers=headers).status_code == 405


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer    ", "Basic abc", "Token abc", "bearer"])
def test_me_is_401_without_a_usable_bearer_token(client, header):
    response = client.get(ME, headers={"Authorization": header} if header else {})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_me_is_401_once_the_session_expired(client, db, actor):
    user, headers = actor
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (past, user["id"]))
    db.commit()

    assert client.get(ME, headers=headers).status_code == 401




# -----------------------------------------------------------
# PUT /api/auth/me — the body-level guards
# -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["[1, 2]", '"hello"', "42", "true"])
def test_update_me_refuses_a_json_body_that_is_not_an_object(client, actor, raw):
    _, headers = actor

    response = client.put(ME, data=raw, headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("raw", ["{", "", "not json at all", "{'single': 'quotes'}"])
def test_update_me_refuses_a_malformed_json_body(client, actor, raw):
    _, headers = actor

    response = client.put(ME, data=raw, headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_update_me_refuses_a_json_null_body(client, actor):
    _, headers = actor

    response = client.put(ME, data="null", headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_update_me_refuses_a_body_sent_without_a_json_content_type(client, actor):
    _, headers = actor

    response = client.put(ME, data="displayName=Ona", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_update_me_refuses_an_empty_object_before_looking_at_any_field(client, actor):
    _, headers = actor

    # {} is a dict but falsy — it lands on "JSON body required",
    # not on "No fields to update"
    response = client.put(ME, json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_update_me_refuses_a_body_carrying_only_unwritable_columns(client, db, actor):
    user, headers = actor
    before = _row(db, user["id"])

    response = client.put(ME, json={"role": "admin", "username": "root", "email": "r@x.lt",
                                    "active": 0, "invited": 1, "id": "other"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "No fields to update"
    after = _row(db, user["id"])
    assert all(before[column] == after[column] for column in FORBIDDEN_COLUMNS)
    assert before["updated_at"] == after["updated_at"]




# -----------------------------------------------------------
# PUT /api/auth/me — displayName
# -----------------------------------------------------------


def test_update_me_uses_the_snake_case_display_name_only_when_the_camel_case_key_is_absent(client, db, actor):
    user, headers = actor

    # The camel key wins even though the snake value is the one
    # that would fail the type check — the fallback is a lookup,
    # not a second chance
    response = client.put(ME, json={"displayName": "Ona", "display_name": 5}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["display_name"] == "Ona"


def test_update_me_refuses_a_null_camel_case_display_name_even_beside_a_valid_snake_case_one(client, db, actor):
    user, headers = actor
    before = _row(db, user["id"])["display_name"]

    response = client.put(ME, json={"displayName": None, "display_name": "Ona"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "display_name must be a string"
    assert _row(db, user["id"])["display_name"] == before


@pytest.mark.parametrize("value", [None, 5, 5.5, True, False, ["Ona"], {"name": "Ona"}])
def test_update_me_refuses_every_non_string_display_name(client, actor, value):
    _, headers = actor

    response = client.put(ME, json={"displayName": value}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "display_name must be a string"


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n", "\r\n", " ", " \t \n "])
def test_update_me_refuses_a_display_name_that_is_blank_after_stripping(client, actor, blank):
    _, headers = actor

    response = client.put(ME, json={"displayName": blank}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name cannot be empty"


@pytest.mark.parametrize("length,status", [(1, 200), (99, 200), (100, 200), (101, 400)])
def test_update_me_caps_the_display_name_at_one_hundred_characters(client, actor, length, status):
    _, headers = actor

    response = client.put(ME, json={"displayName": "a" * length}, headers=headers)

    assert response.status_code == status


def test_update_me_measures_the_display_name_cap_after_stripping(client, db, actor):
    user, headers = actor
    padded = "   " + "a" * 100 + "   "

    response = client.put(ME, json={"displayName": padded}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["display_name"] == "a" * 100


def test_update_me_counts_display_name_characters_not_bytes(client, db, actor):
    user, headers = actor
    lithuanian = "ą" * 100  # 200 bytes, 100 characters

    response = client.put(ME, json={"displayName": lithuanian}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["display_name"] == lithuanian


def test_update_me_keeps_whitespace_inside_the_display_name(client, db, actor):
    user, headers = actor

    client.put(ME, json={"displayName": "  Ona   Marija  Petraitė  "}, headers=headers)

    assert _row(db, user["id"])["display_name"] == "Ona   Marija  Petraitė"


@pytest.mark.contract
def test_update_me_stores_the_display_name_raw_and_answers_it_escaped(client, db, actor):
    user, headers = actor

    response = _put_raw(client, headers, {"displayName": '<b>Ona</b> & "Co"'})

    assert response.status_code == 200
    assert _row(db, user["id"])["display_name"] == '<b>Ona</b> & "Co"'
    assert response.get_json()["displayName"] == "&lt;b&gt;Ona&lt;/b&gt; &amp; &quot;Co&quot;"


def test_update_me_strips_null_bytes_out_of_the_display_name(client, db, actor):
    user, headers = actor

    response = _put_raw(client, headers, {"displayName": "On\x00a"})

    assert response.status_code == 200
    assert _row(db, user["id"])["display_name"] == "Ona"


def test_update_me_refuses_a_display_name_that_is_only_null_bytes(client, actor):
    _, headers = actor

    response = _put_raw(client, headers, {"displayName": "\x00\x00"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name cannot be empty"




# -----------------------------------------------------------
# PUT /api/auth/me — avatarUrl
# -----------------------------------------------------------


@pytest.mark.parametrize("ext", ["jpg", "jpeg", "png", "gif", "webp"])
def test_update_me_accepts_every_extension_the_uploads_route_hands_out(client, db, actor, ext):
    user, headers = actor
    path = _upload_path(ext)

    response = client.put(ME, json={"avatarUrl": path}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["avatar_url"] == path


def test_update_me_prefers_the_camel_case_avatar_key_when_both_spellings_are_sent(client, db, actor):
    user, headers = actor
    camel, snake = _upload_path(), _upload_path()

    response = client.put(ME, json={"avatarUrl": camel, "avatar_url": snake}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["avatar_url"] == camel


@pytest.mark.parametrize("value", [
    "/api/uploads/../../etc/passwd",
    "/api/uploads/00000000000000000000000000000000.jpg?x=//evil.lt",
    "/api/uploads/00000000000000000000000000000000.bmp",
    "/api/uploads/short.jpg",
    "https://evil.lt/a.jpg",
    "//evil.lt/a.jpg",
    "javascript:alert(1)",
    "data:image/png;base64,AAAA",
    "/uploads/00000000000000000000000000000000.jpg",
])
def test_update_me_refuses_an_avatar_that_is_not_an_own_upload(client, db, actor, value):
    user, headers = actor
    before = _row(db, user["id"])["avatar_url"]

    response = client.put(ME, json={"avatarUrl": value}, headers=headers)

    assert response.status_code == 400
    assert "/api/uploads/" in response.get_json()["error"]
    assert _row(db, user["id"])["avatar_url"] == before


def test_update_me_refuses_an_avatar_with_a_trailing_newline(client, actor):
    _, headers = actor

    # "$" would match before a trailing newline; the validator
    # anchors with \Z, so this shape cannot be stored verbatim
    response = _put_raw(client, headers, {"avatarUrl": _upload_path() + "\n"})

    assert response.status_code == 400


def test_update_me_refuses_a_bad_snake_case_avatar_even_when_the_camel_case_one_would_win(client, db, actor):
    user, headers = actor

    # The before_request hook validates BOTH spellings, so the
    # loser of update_me's key race is still a hard 400
    response = client.put(ME, json={"avatarUrl": _upload_path(), "avatar_url": "https://evil.lt/a.jpg"},
                          headers=headers)

    assert response.status_code == 400
    assert _row(db, user["id"])["avatar_url"] is None


@pytest.mark.parametrize("cleared", [None, ""])
def test_update_me_clears_an_avatar_that_was_never_set_without_deleting_anything(client, db, actor, upload_deletes, cleared):
    user, headers = actor

    response = client.put(ME, json={"avatarUrl": cleared}, headers=headers)

    assert response.status_code == 200
    assert response.get_json()["avatarUrl"] == cleared
    assert _row(db, user["id"])["avatar_url"] == cleared
    assert upload_deletes == []


def test_update_me_replaces_one_own_upload_with_another_and_deletes_the_old(client, db, actor, upload_deletes):
    user, headers = actor
    old, new = _upload_path(), _upload_path("png")
    _set(db, user["id"], "avatar_url", old)

    response = client.put(ME, json={"avatarUrl": new}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["avatar_url"] == new
    assert upload_deletes == [old]


def test_update_me_deletes_nothing_when_the_same_avatar_is_sent_again(client, db, actor, upload_deletes):
    user, headers = actor
    path = _upload_path()
    _set(db, user["id"], "avatar_url", path)

    response = client.put(ME, json={"avatarUrl": path}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["avatar_url"] == path
    assert upload_deletes == []


@pytest.mark.parametrize("foreign", ["https://cdn.knf.vu.lt/a.jpg", "/uploads/a.jpg", "/static/avatar.png"])
def test_update_me_never_deletes_an_avatar_the_uploads_package_does_not_own(client, db, actor, upload_deletes, foreign):
    user, headers = actor
    _set(db, user["id"], "avatar_url", foreign)

    response = client.put(ME, json={"avatarUrl": _upload_path()}, headers=headers)

    assert response.status_code == 200
    assert upload_deletes == []


def test_update_me_deletes_the_replaced_upload_only_after_the_new_one_is_committed(app, client, db, actor, monkeypatch):
    import app.uploads.routes as uploads_routes

    user, headers = actor
    old, new = _upload_path(), _upload_path()
    _set(db, user["id"], "avatar_url", old)
    seen = []

    def _spy(path):
        # A second connection can only see the new value if the
        # route's transaction already committed
        conn = sqlite3.connect(app.config["DB_PATH"], timeout=15)
        try:
            stored = conn.execute("SELECT avatar_url FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
        finally:
            conn.close()
        seen.append((path, stored))
        return True

    monkeypatch.setattr(uploads_routes, "delete_upload", _spy)
    client.put(ME, json={"avatarUrl": new}, headers=headers)

    assert seen == [(old, new)]


def test_update_me_does_not_delete_the_replaced_upload_when_the_row_vanished(client, db, actor, upload_deletes, mid_request):
    user, headers = actor
    _set(db, user["id"], "avatar_url", _upload_path())
    mid_request("utc_now_iso", lambda conn: conn.execute("DELETE FROM users WHERE id = ?", (user["id"],)))

    response = client.put(ME, json={"avatarUrl": _upload_path()}, headers=headers)

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"
    assert upload_deletes == []


def test_update_me_changes_the_name_and_the_avatar_in_one_call(client, db, actor, upload_deletes):
    user, headers = actor
    old = _upload_path()
    _set(db, user["id"], "avatar_url", old)
    new = _upload_path()

    response = client.put(ME, json={"displayName": "Ona", "avatarUrl": new}, headers=headers)

    assert response.status_code == 200
    row = _row(db, user["id"])
    assert (row["display_name"], row["avatar_url"]) == ("Ona", new)
    assert upload_deletes == [old]




# -----------------------------------------------------------
# PUT /api/auth/me — the avatar backstop (no before_request)
# -----------------------------------------------------------


@pytest.mark.parametrize("value", ["/api/uploads/", "/api/uploads/../../etc/passwd",
                                   "/api/uploads/x.jpg?next=//evil.lt"])
def test_the_avatar_backstop_only_checks_the_uploads_prefix(app, actor, value):
    _, headers = actor

    # Defence in depth, not the gate: create_app's validator is
    # what refuses these shapes over HTTP (the test above), and
    # the route's own check is a prefix match by design
    body, status = _call_update_me(app, headers, {"avatarUrl": value})

    assert status == 200
    assert body["avatarUrl"] == value


@pytest.mark.parametrize("value", [0, False, 3.5, [], {}, ["/api/uploads/x.jpg"]])
def test_the_avatar_backstop_refuses_a_non_string_that_is_not_none_or_blank(app, actor, value):
    _, headers = actor

    body, status = _call_update_me(app, headers, {"avatarUrl": value})

    assert status == 400
    assert body["error"] == "avatar_url must be a relative /api/uploads/ path"


def test_the_avatar_backstop_refuses_a_relative_path_outside_uploads(app, actor):
    _, headers = actor

    body, status = _call_update_me(app, headers, {"avatar_url": "/etc/passwd"})

    assert status == 400
    assert body["error"] == "avatar_url must be a relative /api/uploads/ path"




# -----------------------------------------------------------
# PUT /api/auth/me — the student-card fields
# -----------------------------------------------------------


@pytest.mark.parametrize("camel,snake,column", [
    ("studentNumber", "student_number", "student_number"),
    ("studyGroup", "study_group", "study_group"),
    ("studyProgram", "study_program", "study_program"),
])
def test_update_me_prefers_the_camel_case_student_card_key(client, db, actor, camel, snake, column):
    user, headers = actor

    response = client.put(ME, json={camel: "camel", snake: 12345}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])[column] == "camel"


@pytest.mark.parametrize("field", ["studentNumber", "student_number", "studyGroup", "study_group",
                                   "studyProgram", "study_program"])
@pytest.mark.parametrize("value", [5, True, ["a"]])
def test_update_me_names_the_key_the_client_sent_on_a_non_string_student_card_field(client, actor, field, value):
    _, headers = actor

    response = client.put(ME, json={field: value}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == f"{field} must be a string"


def test_update_me_names_the_first_offending_student_card_field(client, actor):
    _, headers = actor

    # The loop order is studentNumber, studyGroup, studyProgram —
    # the earliest bad one is the one the client hears about
    response = client.put(ME, json={"studyProgram": 3, "studyGroup": 2, "studentNumber": 1},
                          headers=headers)

    assert response.get_json()["error"] == "studentNumber must be a string"


@pytest.mark.parametrize("column,field", [
    ("student_number", "studentNumber"), ("study_group", "studyGroup"), ("study_program", "studyProgram"),
])
@pytest.mark.parametrize("length,status", [(1, 200), (49, 200), (50, 200), (51, 400)])
def test_update_me_caps_every_student_card_field_at_fifty_characters(client, actor, column, field, length, status):
    _, headers = actor

    response = client.put(ME, json={field: "a" * length}, headers=headers)

    assert response.status_code == status
    if status == 400:
        assert response.get_json()["error"] == f"{field} must be at most 50 characters"


def test_update_me_measures_the_student_card_cap_after_stripping(client, db, actor):
    user, headers = actor

    response = client.put(ME, json={"studyGroup": "  " + "a" * 50 + "  "}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["study_group"] == "a" * 50


def test_update_me_counts_student_card_characters_not_bytes(client, db, actor):
    user, headers = actor
    lithuanian = "ų" * 50

    response = client.put(ME, json={"studyProgram": lithuanian}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["study_program"] == lithuanian


@pytest.mark.parametrize("blank", ["", "  ", "\t\n"])
def test_update_me_stores_a_blank_student_card_field_as_null(client, db, actor, blank):
    user, headers = actor
    _set(db, user["id"], "student_number", "1911234")

    response = client.put(ME, json={"studentNumber": blank}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["student_number"] is None
    assert response.get_json()["studentNumber"] is None


def test_update_me_stores_an_explicit_null_student_card_field_as_null(client, db, actor):
    user, headers = actor
    _set(db, user["id"], "study_group", "PS-1")

    response = client.put(ME, json={"studyGroup": None}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["study_group"] is None


def test_update_me_writes_all_three_student_card_fields_at_once(client, db, actor):
    user, headers = actor

    response = client.put(ME, json={"studentNumber": "1911234", "studyGroup": "PS-1",
                                    "studyProgram": "Programų sistemos"}, headers=headers)

    assert response.status_code == 200
    row = _row(db, user["id"])
    assert (row["student_number"], row["study_group"], row["study_program"]) == \
        ("1911234", "PS-1", "Programų sistemos")




# -----------------------------------------------------------
# PUT /api/auth/me — guard order, atomicity, ownership
# -----------------------------------------------------------


def test_update_me_checks_the_display_name_before_the_avatar(client, actor):
    _, headers = actor

    response = client.put(ME, json={"displayName": "", "avatarUrl": _upload_path()}, headers=headers)

    assert response.get_json()["error"] == "Display name cannot be empty"


def test_update_me_checks_the_avatar_before_the_student_card_fields(client, actor):
    _, headers = actor

    response = client.put(ME, json={"avatarUrl": "https://evil.lt/a.jpg", "studentNumber": 5},
                          headers=headers)

    assert response.status_code == 400
    assert "/api/uploads/" in response.get_json()["error"]


def test_update_me_writes_nothing_at_all_when_a_later_field_fails(client, db, actor):
    user, headers = actor
    before = _row(db, user["id"])

    response = client.put(ME, json={"displayName": "Nauja", "studyProgram": 42}, headers=headers)

    assert response.status_code == 400
    after = _row(db, user["id"])
    assert after["display_name"] == before["display_name"]
    assert after["updated_at"] == before["updated_at"]


def test_update_me_leaves_the_news_snapshots_alone_when_the_request_is_rejected(client, db, actor):
    user, headers = actor
    db.execute("INSERT INTO news_posts (id, title, content, author_id, author_name)"
               " VALUES (?, 'T', 'C', ?, ?)", (str(uuid.uuid4()), user["id"], "Senas Vardas"))
    db.commit()

    client.put(ME, json={"displayName": "Naujas Vardas", "studyGroup": []}, headers=headers)

    assert db.execute("SELECT author_name FROM news_posts").fetchone()[0] == "Senas Vardas"


def test_update_me_rewrites_only_the_callers_own_news_snapshots(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    for author_id, name in [(user["id"], "Mano"), (other["id"], "Kito"), (None, "Naujienų robotas")]:
        db.execute("INSERT INTO news_posts (id, title, content, author_id, author_name)"
                   " VALUES (?, 'T', 'C', ?, ?)", (str(uuid.uuid4()), author_id, name))
    db.commit()

    client.put(ME, json={"displayName": "Perrašyta"}, headers=headers)

    names = {row[0] for row in db.execute("SELECT author_name FROM news_posts").fetchall()}
    assert names == {"Perrašyta", "Kito", "Naujienų robotas"}


def test_update_me_does_not_touch_the_news_snapshots_when_no_name_was_sent(client, db, actor):
    user, headers = actor
    db.execute("INSERT INTO news_posts (id, title, content, author_id, author_name)"
               " VALUES (?, 'T', 'C', ?, ?)", (str(uuid.uuid4()), user["id"], "Senas Vardas"))
    db.commit()

    client.put(ME, json={"studyGroup": "PS-1"}, headers=headers)

    assert db.execute("SELECT author_name FROM news_posts").fetchone()[0] == "Senas Vardas"


def test_update_me_edits_only_the_callers_own_row(client, db, actor, make_user):
    user, headers = actor
    other = make_user(display_name="Kitas Žmogus")

    client.put(ME, json={"displayName": "Mano Vardas", "studyGroup": "PS-1"}, headers=headers)

    assert _row(db, other["id"])["display_name"] == "Kitas Žmogus"
    assert _row(db, other["id"])["study_group"] is None


def test_update_me_stamps_updated_at_and_leaves_created_at_alone(client, db, actor):
    user, headers = actor
    before = _row(db, user["id"])

    client.put(ME, json={"displayName": "Ona"}, headers=headers)

    after = _row(db, user["id"])
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] != before["updated_at"]
    assert "T" in after["updated_at"] and after["updated_at"].endswith("+00:00")


def test_update_me_is_idempotent_for_the_same_payload(client, actor):
    _, headers = actor
    payload = {"displayName": "Ona", "studentNumber": "1911234", "studyGroup": "PS-1"}

    first = client.put(ME, json=payload, headers=headers).get_json()
    second = client.put(ME, json=payload, headers=headers).get_json()

    assert first == second


def test_update_me_answers_the_row_as_it_stands_after_the_commit(client, db, actor, mid_request):
    user, headers = actor
    mid_request("utc_now_iso",
                lambda conn: conn.execute("UPDATE users SET role = 'teacher' WHERE id = ?", (user["id"],)))

    body = client.put(ME, json={"displayName": "Ona"}, headers=headers).get_json()

    # request.user still said "student"; the answer is the
    # re-read, so a role changed mid-request is not echoed stale
    assert body["role"] == "teacher"


def test_update_me_is_401_for_a_deactivated_account(client, db, actor):
    user, headers = actor
    _set(db, user["id"], "active", 0)

    response = client.put(ME, json={"displayName": "Ona"}, headers=headers)

    assert response.status_code == 401


def test_update_me_is_401_once_the_session_expired(client, db, actor):
    user, headers = actor
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (past, user["id"]))
    db.commit()

    assert client.put(ME, json={"displayName": "Ona"}, headers=headers).status_code == 401


def test_update_me_shows_the_edit_on_the_next_get_me(client, actor):
    _, headers = actor

    put_body = client.put(ME, json={"displayName": "Ona", "studyProgram": "PS"}, headers=headers).get_json()
    get_body = client.get(ME, headers=headers).get_json()

    assert put_body == get_body




# -----------------------------------------------------------
# _delete_replaced_upload — the three exits
# -----------------------------------------------------------


def test_delete_replaced_upload_hands_the_path_to_the_uploads_helper(upload_deletes):
    from app.auth.routes import _delete_replaced_upload

    assert _delete_replaced_upload("/api/uploads/a.jpg") is None
    assert upload_deletes == ["/api/uploads/a.jpg"]


def test_delete_replaced_upload_passes_whatever_it_is_given_straight_through(upload_deletes):
    from app.auth.routes import _delete_replaced_upload

    # No guard of its own — the uploads helper is the one that
    # decides a value is not a name it owns
    _delete_replaced_upload(None)

    assert upload_deletes == [None]


def test_delete_replaced_upload_swallows_a_throwing_helper(monkeypatch):
    import app.uploads.routes as uploads_routes
    from app.auth.routes import _delete_replaced_upload

    def _boom(path):
        raise OSError("disk on fire")

    monkeypatch.setattr(uploads_routes, "delete_upload", _boom)

    assert _delete_replaced_upload("/api/uploads/a.jpg") is None


def test_delete_replaced_upload_is_a_no_op_when_the_uploads_helper_is_missing(monkeypatch):
    import app.uploads.routes as uploads_routes
    from app.auth.routes import _delete_replaced_upload

    monkeypatch.delattr(uploads_routes, "delete_upload")

    assert _delete_replaced_upload("/api/uploads/a.jpg") is None


def test_update_me_still_succeeds_when_the_uploads_helper_is_missing(client, db, actor, monkeypatch):
    import app.uploads.routes as uploads_routes

    user, headers = actor
    _set(db, user["id"], "avatar_url", _upload_path())
    monkeypatch.delattr(uploads_routes, "delete_upload")

    response = client.put(ME, json={"avatarUrl": None}, headers=headers)

    assert response.status_code == 200
    assert _row(db, user["id"])["avatar_url"] is None


def test_update_me_still_succeeds_when_the_uploads_helper_throws(client, db, actor, monkeypatch):
    import app.uploads.routes as uploads_routes

    def _boom(path):
        raise RuntimeError("nope")

    user, headers = actor
    _set(db, user["id"], "avatar_url", _upload_path())
    monkeypatch.setattr(uploads_routes, "delete_upload", _boom)

    response = client.put(ME, json={"avatarUrl": _upload_path()}, headers=headers)

    assert response.status_code == 200




# -----------------------------------------------------------
# POST /api/auth/change-password — the body guards
# -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["[1, 2]", '"x"', "7"])
def test_change_password_refuses_a_body_that_is_not_an_object(client, actor, raw):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD, data=raw,
                           headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("body", [
    {}, {"oldPassword": "slaptazodis123"}, {"newPassword": "naujas-2026"},
    {"old_password": "slaptazodis123"}, {"new_password": "naujas-2026"},
])
def test_change_password_needs_both_passwords(client, actor, body):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD, json=body, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] in ("JSON body required", "Old and new password required")


@pytest.mark.parametrize("falsy", [None, "", 0, False, [], {}])
def test_change_password_treats_a_falsy_password_as_missing_not_as_a_type_error(client, actor, falsy):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": falsy, "newPassword": "naujas-2026"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Old and new password required"


@pytest.mark.parametrize("old,new", [
    (5, "naujas-2026"), ("slaptazodis123", 5), (["a"], ["b"]), ({"a": 1}, "naujas-2026"),
    (True, "naujas-2026"), ("slaptazodis123", 3.5),
])
def test_change_password_refuses_truthy_non_string_passwords(client, actor, old, new):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD, json={"oldPassword": old, "newPassword": new},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Old and new password must be strings"


def test_change_password_reads_the_camel_case_key_even_when_it_is_blank(client, db, actor):
    user, headers = actor
    before = _row(db, user["id"])["password_hash"]

    # "oldPassword" is PRESENT, so the snake spelling is never
    # consulted — a blank camel value is a hard 400
    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": "", "old_password": user["password"],
                                 "newPassword": "naujas-2026"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Old and new password required"
    assert _row(db, user["id"])["password_hash"] == before


def test_change_password_reads_the_camel_case_new_password_even_when_it_is_blank(client, actor):
    _, headers = actor

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": "slaptazodis123", "newPassword": "",
                                 "new_password": "naujas-2026"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Old and new password required"


def test_change_password_prefers_both_camel_case_keys_over_both_snake_case_ones(client, actor):
    user, headers = actor

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "old_password": "neteisingas",
                                 "newPassword": "naujas-2026", "new_password": "x"},
                           headers=headers)

    assert response.status_code == 200


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_change_password_serves_only_post(client, actor, method):
    _, headers = actor

    assert getattr(client, method)(CHANGE_PASSWORD, headers=headers).status_code == 405




# -----------------------------------------------------------
# POST /api/auth/change-password — the shared password policy
# -----------------------------------------------------------


@pytest.mark.parametrize("length,status", [(5, 400), (6, 200), (71, 200), (72, 200), (73, 400)])
def test_change_password_takes_six_to_seventy_two_characters(client, make_user, auth_headers, length, status):
    user = make_user(username=f"u{uuid.uuid4().hex[:8]}")
    headers = auth_headers(user)

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": "N" * length},
                           headers=headers)

    assert response.status_code == status
    if status == 400:
        assert response.get_json()["code"] == "weak_password"


@pytest.mark.parametrize("chars,status", [(36, 200), (37, 400)])
def test_change_password_measures_the_cap_in_bytes_not_characters(client, make_user, auth_headers, chars, status):
    user = make_user(username=f"u{uuid.uuid4().hex[:8]}")
    headers = auth_headers(user)

    # "ą" is two UTF-8 bytes: 36 characters = 72 bytes fits,
    # 37 = 74 bytes is past what bcrypt would even hash
    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": "ą" * chars},
                           headers=headers)

    assert response.status_code == status


@pytest.mark.parametrize("common", ["password123", "ADMIN123", "Labas123", "IlOvEyOu", "slaptazodis"])
def test_change_password_refuses_a_common_password_in_any_case(client, make_user, auth_headers, common):
    user = make_user(username=f"u{uuid.uuid4().hex[:8]}")

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": common},
                           headers=auth_headers(user))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Password is too common"


@pytest.mark.parametrize("template", ["{u}Extra1", "prefix{u}suffix", "{U}12345", "xx{u}"])
def test_change_password_refuses_a_new_password_containing_the_username_in_any_case(
        client, make_user, auth_headers, template):
    user = make_user(username="jonaitis")

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"],
                                 "newPassword": template.format(u="jonaitis", U="JONAITIS")},
                           headers=auth_headers(user))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Password must not contain your username"


@pytest.mark.parametrize("local,status", [("ab", 200), ("abc", 400)])
def test_change_password_screens_the_email_local_part_only_from_three_characters(
        client, db, make_user, auth_headers, local, status):
    user = make_user(username=f"u{uuid.uuid4().hex[:8]}")
    headers = auth_headers(user)
    _set(db, user["id"], "email", f"{local}@knf.vu.lt")

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": f"{local}-Kitas-9"},
                           headers=headers)

    assert response.status_code == status


def test_change_password_screens_an_email_with_no_at_sign_as_its_whole_value(client, db, make_user, auth_headers):
    user = make_user(username=f"u{uuid.uuid4().hex[:8]}")
    headers = auth_headers(user)
    _set(db, user["id"], "email", "legacyvalue")

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": "LegacyValue-9"},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Password must not contain your email"


def test_change_password_screens_against_the_callers_own_username_only(client, make_user, auth_headers):
    user = make_user(username="jonaitis")
    make_user(username="petraitis")

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": "petraitis-2026"},
                           headers=auth_headers(user))

    assert response.status_code == 200


def test_change_password_allows_rotating_to_the_same_password(client, db, actor):
    user, headers = actor
    before = _row(db, user["id"])["password_hash"]

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": user["password"]},
                           headers=headers)

    assert response.status_code == 200
    # A fresh salt, so the hash changes even though the secret did not
    assert _row(db, user["id"])["password_hash"] != before


def test_change_password_strips_null_bytes_out_of_the_new_password(client, actor):
    user, headers = actor

    response = _post_raw(client, CHANGE_PASSWORD, headers,
                         {"oldPassword": user["password"], "newPassword": "naujas\x00-2026"})

    assert response.status_code == 200
    assert client.post(LOGIN, json={"username": user["username"], "password": "naujas-2026"}).status_code == 200




# -----------------------------------------------------------
# POST /api/auth/change-password — verification and effects
# -----------------------------------------------------------


def test_change_password_swaps_the_credential_over(client, actor):
    user, headers = actor

    assert client.post(CHANGE_PASSWORD,
                       json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                       headers=headers).status_code == 200
    assert client.post(LOGIN, json={"username": user["username"],
                                    "password": user["password"]}).status_code == 401
    assert client.post(LOGIN, json={"username": user["username"],
                                    "password": "naujas-2026"}).status_code == 200


def test_change_password_keeps_the_presented_session_usable(client, actor):
    user, headers = actor

    client.post(CHANGE_PASSWORD, json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                headers=headers)

    assert client.get(ME, headers=headers).status_code == 200


def test_change_password_kills_every_other_session_of_the_caller(client, auth_headers, make_user):
    user = make_user(username=f"u{uuid.uuid4().hex[:8]}")
    first, second, third = auth_headers(user), auth_headers(user), auth_headers(user)

    client.post(CHANGE_PASSWORD, json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                headers=third)

    assert client.get(ME, headers=first).status_code == 401
    assert client.get(ME, headers=second).status_code == 401
    assert client.get(ME, headers=third).status_code == 200


def test_change_password_leaves_the_callers_push_tokens_alone(client, db, actor):
    user, headers = actor
    db.execute("INSERT INTO push_tokens (id, user_id, token) VALUES (?, ?, ?)",
               (str(uuid.uuid4()), user["id"], "ExponentPushToken[abc]"))
    db.commit()

    client.post(CHANGE_PASSWORD, json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                headers=headers)

    assert db.execute("SELECT COUNT(*) FROM push_tokens WHERE user_id = ?",
                      (user["id"],)).fetchone()[0] == 1


def test_change_password_touches_no_column_but_the_hash_and_the_stamp(client, db, actor):
    user, headers = actor
    before = dict(_row(db, user["id"]))

    client.post(CHANGE_PASSWORD, json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                headers=headers)

    after = dict(_row(db, user["id"]))
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {"password_hash", "updated_at"}


def test_change_password_kicks_the_callers_sockets_after_the_commit(app, client, db, actor, monkeypatch):
    import app.chat.events as chat_events

    user, headers = actor
    before = _row(db, user["id"])["password_hash"]
    seen = []

    def _spy(user_id):
        conn = sqlite3.connect(app.config["DB_PATH"], timeout=15)
        try:
            stored = conn.execute("SELECT password_hash FROM users WHERE id = ?",
                                  (user_id,)).fetchone()[0]
        finally:
            conn.close()
        seen.append((user_id, stored != before))

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _spy)
    client.post(CHANGE_PASSWORD, json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                headers=headers)

    assert seen == [(user["id"], True)]


def test_change_password_kicks_no_socket_when_the_old_password_is_wrong(client, actor, socket_kicks):
    _, headers = actor

    client.post(CHANGE_PASSWORD, json={"oldPassword": "neteisingas", "newPassword": "naujas-2026"},
                headers=headers)

    assert socket_kicks == []


def test_change_password_kicks_no_socket_when_the_new_password_is_weak(client, actor, socket_kicks):
    user, headers = actor

    client.post(CHANGE_PASSWORD, json={"oldPassword": user["password"], "newPassword": "123"},
                headers=headers)

    assert socket_kicks == []


def test_change_password_leaves_another_users_credential_untouched(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    other_headers = auth_headers(other)
    other_hash = _row(db, other["id"])["password_hash"]

    client.post(CHANGE_PASSWORD, json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                headers=headers)

    assert _row(db, other["id"])["password_hash"] == other_hash
    assert client.get(ME, headers=other_headers).status_code == 200


def test_change_password_is_400_when_the_row_vanished_mid_request(client, actor, mid_request):
    user, headers = actor
    mid_request("_check_rate_limit",
                lambda conn: conn.execute("DELETE FROM users WHERE id = ?", (user["id"],)))

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_credentials"


def test_change_password_is_401_for_a_deactivated_account(client, db, actor):
    user, headers = actor
    _set(db, user["id"], "active", 0)

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                           headers=headers)

    assert response.status_code == 401


def test_change_password_is_401_once_the_session_expired(client, db, actor):
    user, headers = actor
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?", (past, user["id"]))
    db.commit()

    response = client.post(CHANGE_PASSWORD,
                           json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                           headers=headers)

    assert response.status_code == 401




# -----------------------------------------------------------
# POST /api/auth/change-password — the failure budget
# -----------------------------------------------------------


def _wrong_old(client, headers, new="naujas-2026"):
    return client.post(CHANGE_PASSWORD, json={"oldPassword": "neteisingas", "newPassword": new},
                       headers=headers)


def test_change_password_allows_exactly_ten_wrong_old_passwords_before_the_429(client, actor):
    _, headers = actor

    for attempt in range(10):
        assert _wrong_old(client, headers).status_code == 400, f"attempt {attempt} should still be checked"

    response = _wrong_old(client, headers)
    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_change_password_429_carries_a_usable_retry_after(client, actor):
    _, headers = actor
    for _ in range(10):
        _wrong_old(client, headers)

    response = _wrong_old(client, headers)

    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 301


def test_change_password_probes_the_budget_before_it_reads_the_body(client, actor):
    _, headers = actor
    for _ in range(10):
        _wrong_old(client, headers)

    # A body that would otherwise be a 400 still gets the 429 —
    # the probe is the first thing the handler does
    response = client.post(CHANGE_PASSWORD, json={"garbage": True}, headers=headers)

    assert response.status_code == 429


def test_change_password_never_spends_budget_on_a_weak_new_password(client, actor):
    user, headers = actor

    for _ in range(15):
        assert client.post(CHANGE_PASSWORD,
                           json={"oldPassword": "neteisingas", "newPassword": "123"},
                           headers=headers).get_json()["code"] == "weak_password"

    # The policy runs before the bcrypt check, so not one of those
    # counted as a failed authentication
    assert client.post(CHANGE_PASSWORD,
                       json={"oldPassword": user["password"], "newPassword": "naujas-2026"},
                       headers=headers).status_code == 200


def test_change_password_never_spends_budget_on_a_malformed_body(client, actor):
    user, headers = actor

    for _ in range(15):
        client.post(CHANGE_PASSWORD, json={"oldPassword": user["password"]}, headers=headers)

    assert _wrong_old(client, headers).status_code == 400


def test_change_password_successes_never_spend_budget(client, make_user, auth_headers):
    user = make_user(username=f"u{uuid.uuid4().hex[:8]}")
    headers = auth_headers(user)
    secret = user["password"]

    for index in range(12):
        nxt = f"Rotacija-{index}-2026"
        assert client.post(CHANGE_PASSWORD, json={"oldPassword": secret, "newPassword": nxt},
                           headers=headers).status_code == 200
        secret = nxt


def test_change_password_budgets_each_user_separately(client, actor, make_user, auth_headers):
    _, headers = actor
    other = make_user()
    other_headers = auth_headers(other)

    for _ in range(11):
        _wrong_old(client, headers)

    assert _wrong_old(client, headers).status_code == 429
    assert _wrong_old(client, other_headers).status_code == 400


def test_change_password_budget_frees_up_once_the_window_passes(client, actor):
    import time_machine

    _, headers = actor
    for _ in range(10):
        _wrong_old(client, headers)
    assert _wrong_old(client, headers).status_code == 429

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(seconds=301), tick=False):
        assert _wrong_old(client, headers).status_code == 400
