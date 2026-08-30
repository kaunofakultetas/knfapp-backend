# -----------------------------------------------------------
#  [*] Tests — admin users, stats and broadcast (app/admin/routes.py)
#
#  The half of the admin console that edits people rather than
#  codes, proved end to end through the real routes:
#
#    - GET /admin/users answers the exact AdminUser shape the
#      mobile admin-users screen consumes (id, username,
#      email, displayName, role, active, createdAt) and NEVER
#      a password hash — the one body that made app-wide
#      Cache-Control: no-store worth having, since it carries
#      every e-mail address the faculty holds.
#    - the optional ?limit= / ?offset= page it without ever
#      changing the no-params answer, and every bad value is a
#      400 that returns no rows at all.
#    - PATCH /admin/users/<id> types its body BEFORE it acts:
#      an unknown id is a 404 even when the body is also
#      wrong, `active` must be a real JSON boolean (a 0 or a
#      "false" used to slip past the self-deactivation guard
#      and log the calling admin out), and an empty patch is
#      "Nothing to update".
#    - the console cannot lock itself out: an admin may not
#      strip their own admin role nor deactivate themselves,
#      and the last-active-admin backstop fires on the one
#      path an HTTP caller can still reach it by.
#    - deactivation BITES: sessions and push tokens are
#      deleted, live sockets are cut, login answers 403 and
#      the user's existing bearer token stops resolving —
#      while a role change deliberately leaves the session
#      alive (documented gotcha, pinned here so it cannot
#      change silently).
#    - every mutation leaves exactly one admin_audit row per
#      changed field, naming the acting admin, and a refused
#      or 404-ing request leaves none — while a missing audit
#      table never fails the action it was meant to record.
#    - GET /admin/stats counts what it says it counts, treats
#      only the two scraper sources as scraped, agrees with
#      list_invitations about which codes are still active
#      (space-form rows included) and serves a snapshot for
#      45 seconds before rebuilding it.
#    - POST /admin/notifications answers 202 with a queued
#      job, forces the admin_announcement marker back on top
#      of any caller data, bounds the payload, and reports the
#      fan-out result on GET /admin/notifications/<job_id>.
#    - require_role refuses anonymous callers with 401 and
#      students, teachers and (outside the invitation routes)
#      curators with 403, on every route in the blueprint.
# -----------------------------------------------------------

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

from app.admin import routes as admin_routes


# The wire shapes the mobile client is typed against —
# services/api/admin.ts, AdminUser / AdminStats
USER_FIELDS = {"id", "username", "email", "displayName", "role", "active", "createdAt"}
STATS_FIELDS = {"users", "posts", "scrapedArticles", "comments", "activeInvitations"}
JOB_FIELDS = {"jobId", "status", "sent", "failed", "title", "createdAt", "finishedAt", "message"}

# Every route in the blueprint, split by the roles its
# require_role admits. Used by the gate matrix at the bottom
ADMIN_ONLY_ROUTES = [
    ("get", "/api/admin/users"),
    ("patch", "/api/admin/users/00000000-0000-0000-0000-000000000000"),
    ("get", "/api/admin/stats"),
    ("post", "/api/admin/notifications"),
    ("get", "/api/admin/notifications/00000000-0000-0000-0000-000000000000"),
]
INVITATION_ROUTES = [
    ("post", "/api/admin/invitations"),
    ("get", "/api/admin/invitations"),
    ("delete", "/api/admin/invitations/00000000-0000-0000-0000-000000000000"),
]




# -----------------------------------------------------------
# _fresh_module_state
# -----------------------------------------------------------
#
# admin/routes.py keeps two PROCESS-wide registries — the 45 s
# stats snapshot and the broadcast job table. Neither is tied
# to an app, so without this a snapshot built on one test's
# database would be served to the next one, and job ids would
# accumulate across the whole session.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_module_state():
    admin_routes._stats_cache.clear()
    admin_routes._broadcast_jobs.clear()
    yield
    admin_routes._stats_cache.clear()
    admin_routes._broadcast_jobs.clear()




# -----------------------------------------------------------
# _call
# -----------------------------------------------------------
#
# One route of the blueprint by (method, path), with an empty
# JSON object on the writing verbs — the gate matrix has to
# reach require_role, never a body validator.
# -----------------------------------------------------------

def _call(client, method, path, headers=None):
    kwargs = {}
    if headers:
        kwargs["headers"] = headers
    if method in ("post", "patch"):
        kwargs["json"] = {}
    return getattr(client, method)(path, **kwargs)




# -----------------------------------------------------------
# _iso / _stamp_created
# -----------------------------------------------------------
#
# Fixture users are inserted with the column DEFAULT, so they
# all share one whole-second created_at and ORDER BY
# created_at DESC has nothing to sort on. Any test that
# asserts an ORDER stamps its own distinct T-form values.
# -----------------------------------------------------------

def _iso(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _stamp_created(db, user_id, created_at):
    db.execute("UPDATE users SET created_at = ? WHERE id = ?", (created_at, user_id))
    db.commit()




# -----------------------------------------------------------
# _uncount_actor
# -----------------------------------------------------------
#
# The last-active-admin guard counts rows with active = 1,
# while every auth check only asks whether active is TRUTHY.
# A hand-edited active = 2 (DbGate writes back whatever it is
# given) therefore leaves the calling admin fully
# authenticated but outside that count — the one way an HTTP
# caller reaches the backstop, since the self-demotion and
# self-deactivation guards catch every other route to it.
# -----------------------------------------------------------

def _uncount_actor(db, user_id):
    db.execute("UPDATE users SET active = 2 WHERE id = ?", (user_id,))
    db.commit()




# -----------------------------------------------------------
# _audit_rows
# -----------------------------------------------------------
#
# The admin_audit trail (migration v40), oldest first,
# optionally filtered to one action.
# -----------------------------------------------------------

def _audit_rows(db, action=None):
    if action:
        return db.execute(
            "SELECT * FROM admin_audit WHERE action = ? ORDER BY created_at", (action,)
        ).fetchall()
    return db.execute("SELECT * FROM admin_audit ORDER BY created_at").fetchall()




# -----------------------------------------------------------
# _seed_article / _seed_comment / _seed_code / _seed_push_token
# -----------------------------------------------------------
#
# Rows the admin routes only ever COUNT, so the cheapest
# honest way to make them is a direct insert: a news_posts row
# of a chosen source (source_url is UNIQUE, hence the uuid), a
# comment on it, an invitation code with an arbitrary expiry /
# use count, and one Expo device token.
# -----------------------------------------------------------

def _seed_article(db, source="knf.vu.lt", author_id=None):
    post_id = str(uuid.uuid4())
    now = _iso()
    db.execute(
        """INSERT INTO news_posts (id, title, content, summary, author_id, author_name, source,
                                   source_url, post_type, is_public, published_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'article', 1, ?, ?, ?)""",
        (post_id, "Naujiena", "Turinys", "Turinys", author_id, "Autorius", source,
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
        """INSERT INTO invitation_codes (id, code, role, created_by, max_uses, use_count, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (code_id, "T-" + uuid.uuid4().hex[:10].upper(), role, created_by, max_uses,
         use_count, expires_at, _iso()),
    )
    db.commit()
    return code_id


def _seed_push_token(db, user_id):
    token = f"ExponentPushToken[{uuid.uuid4().hex[:22]}]"
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, token, "ios"),
    )
    db.commit()
    return token


def _session_count(db, user_id):
    return db.execute("SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?", (user_id,)).fetchone()["c"]


def _push_token_count(db, user_id):
    return db.execute("SELECT COUNT(*) AS c FROM push_tokens WHERE user_id = ?", (user_id,)).fetchone()["c"]




# -----------------------------------------------------------
# inline_broadcast
# -----------------------------------------------------------
#
# POST /admin/notifications hands the Expo fan-out to
# socketio.start_background_task, which in production is a
# real thread. Swapping the SocketIO lookup for one that runs
# the target INLINE makes the job's whole lifecycle
# observable inside the request that started it, with no
# sleeping and no race — and the 202 body is still built from
# the copy taken before the task ran, which is exactly what
# _set_broadcast_job promises.
# -----------------------------------------------------------

class _InlineSocketIO:
    def __init__(self):
        self.tasks = []

    def start_background_task(self, target, *args, **kwargs):
        self.tasks.append((target, args, kwargs))
        target(*args, **kwargs)


@pytest.fixture
def inline_broadcast(monkeypatch):
    fake = _InlineSocketIO()
    monkeypatch.setattr(admin_routes, "_get_socketio", lambda: fake)
    return fake




# -----------------------------------------------------------
# fake_notify
# -----------------------------------------------------------
#
# notify_channel with no Expo behind it: records the call and
# hands back whatever `.result` is set to — an int, a
# (sent, failed) tuple, a dict or an Exception to raise — so
# _fanout_counts and the failure path can both be driven
# without a network the container does not have.
# -----------------------------------------------------------

@pytest.fixture
def fake_notify(monkeypatch):
    from app.notifications import push as push_module

    def _notify(channel, title, body, data=None, **kwargs):
        _notify.calls.append({"channel": channel, "title": title, "body": body, "data": data})
        if isinstance(_notify.result, Exception):
            raise _notify.result
        return _notify.result

    _notify.calls = []
    _notify.result = 0
    monkeypatch.setattr(push_module, "notify_channel", _notify)
    return _notify




# -----------------------------------------------------------
# broadcast
# -----------------------------------------------------------
#
# POST /admin/notifications with the smallest valid body,
# overridable field by field.
# -----------------------------------------------------------

def _broadcast(client, headers, **body):
    payload = {"title": "Pranešimas", "body": "Turinys"}
    payload.update(body)
    return client.post("/api/admin/notifications", headers=headers, json=payload)




# -----------------------------------------------------------
# _padded_data
# -----------------------------------------------------------
#
# A `data` object whose payload serialises to EXACTLY `size`
# bytes once the route has stamped the type marker on it — the
# only way to sit on the _BROADCAST_DATA_MAX boundary rather
# than somewhere near it.
# -----------------------------------------------------------

def _padded_data(size):
    marker = {"pad": "", "type": "admin_announcement"}
    overhead = len(json.dumps(marker, ensure_ascii=False).encode())
    return {"pad": "x" * (size - overhead)}








# ===========================================================
# GET /api/admin/users — the listing
# ===========================================================

@pytest.mark.contract
def test_user_list_answers_the_mobile_admin_user_shape(client, admin, make_user):
    _, headers = admin
    make_user(username="rasa", role="teacher")

    response = client.get("/api/admin/users", headers=headers)

    assert response.status_code == 200
    users = response.get_json()["users"]
    assert len(users) == 2
    for user in users:
        assert set(user) == USER_FIELDS
        assert isinstance(user["active"], bool)


def test_user_list_carries_every_account(client, admin, make_user):
    _, headers = admin
    ona = make_user(username="ona")
    jonas = make_user(username="jonas", role="teacher")

    users = client.get("/api/admin/users", headers=headers).get_json()["users"]

    assert {u["username"] for u in users} == {"admin", "ona", "jonas"}
    assert {u["id"] for u in users} >= {ona["id"], jonas["id"]}


def test_user_list_never_carries_a_password_hash(client, admin, make_user, db):
    _, headers = admin
    user = make_user(username="slapta")
    stored = db.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()[0]

    response = client.get("/api/admin/users", headers=headers)

    assert stored not in response.get_data(as_text=True)
    assert all("password_hash" not in u and "passwordHash" not in u
               for u in response.get_json()["users"])


def test_user_list_is_newest_first(client, admin, make_user, db):
    admin_user, headers = admin
    first = make_user(username="pirmas")
    second = make_user(username="antras")
    _stamp_created(db, admin_user["id"], _iso(-300))
    _stamp_created(db, first["id"], _iso(-200))
    _stamp_created(db, second["id"], _iso(-100))

    users = client.get("/api/admin/users", headers=headers).get_json()["users"]

    assert [u["username"] for u in users] == ["antras", "pirmas", "admin"]


def test_a_deactivated_user_reads_as_active_false(client, admin, make_user):
    _, headers = admin
    banned = make_user(username="isjungtas", active=0)

    users = client.get("/api/admin/users", headers=headers).get_json()["users"]
    row = next(u for u in users if u["id"] == banned["id"])

    assert row["active"] is False


def test_an_active_user_reads_as_active_true(client, admin, make_user):
    _, headers = admin
    user = make_user(username="ijungtas")

    users = client.get("/api/admin/users", headers=headers).get_json()["users"]

    assert next(u for u in users if u["id"] == user["id"])["active"] is True


def test_user_list_is_never_cached(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users", headers=headers)

    assert "no-store" in response.headers.get("Cache-Control", "")


def test_user_list_display_name_and_email_come_straight_off_the_row(client, admin, make_user):
    _, headers = admin
    user = make_user(username="vardas", display_name="Vardas Pavardė")

    users = client.get("/api/admin/users", headers=headers).get_json()["users"]
    row = next(u for u in users if u["id"] == user["id"])

    assert row["displayName"] == "Vardas Pavardė"
    assert row["email"] == "vardas@knf.vu.lt"




# ===========================================================
# GET /api/admin/users — ?limit= / ?offset=
# ===========================================================

def test_user_list_with_no_paging_params_returns_everything(client, admin, make_user):
    _, headers = admin
    for i in range(4):
        make_user(username=f"vartotojas{i}")

    users = client.get("/api/admin/users", headers=headers).get_json()["users"]

    assert len(users) == 5


def test_user_list_limit_returns_only_that_many(client, admin, make_user):
    _, headers = admin
    for i in range(4):
        make_user(username=f"ribotas{i}")

    users = client.get("/api/admin/users?limit=2", headers=headers).get_json()["users"]

    assert len(users) == 2


def test_user_list_offset_skips_the_first_rows(client, admin, make_user, db):
    admin_user, headers = admin
    first = make_user(username="pirmas")
    second = make_user(username="antras")
    _stamp_created(db, admin_user["id"], _iso(-300))
    _stamp_created(db, first["id"], _iso(-200))
    _stamp_created(db, second["id"], _iso(-100))

    page = client.get("/api/admin/users?limit=1&offset=1", headers=headers).get_json()["users"]

    assert [u["username"] for u in page] == ["pirmas"]


def test_user_list_offset_without_a_limit_returns_the_rest(client, admin, make_user, db):
    admin_user, headers = admin
    first = make_user(username="pirmas")
    second = make_user(username="antras")
    _stamp_created(db, admin_user["id"], _iso(-300))
    _stamp_created(db, first["id"], _iso(-200))
    _stamp_created(db, second["id"], _iso(-100))

    page = client.get("/api/admin/users?offset=1", headers=headers).get_json()["users"]

    assert [u["username"] for u in page] == ["pirmas", "admin"]


def test_user_list_offset_past_the_end_is_an_empty_page(client, admin):
    _, headers = admin

    page = client.get("/api/admin/users?offset=50", headers=headers).get_json()["users"]

    assert page == []


def test_user_list_limit_of_one_is_accepted(client, admin, make_user):
    _, headers = admin
    make_user(username="vienas")

    response = client.get("/api/admin/users?limit=1", headers=headers)

    assert response.status_code == 200
    assert len(response.get_json()["users"]) == 1


def test_user_list_limit_of_five_hundred_is_accepted(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users?limit=500", headers=headers)

    assert response.status_code == 200


def test_user_list_offset_of_zero_is_accepted(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users?limit=10&offset=0", headers=headers)

    assert response.status_code == 200
    assert len(response.get_json()["users"]) == 1


def test_user_list_limit_of_zero_is_refused(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users?limit=0", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be between 1 and 500"


def test_user_list_limit_over_five_hundred_is_refused(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users?limit=501", headers=headers)

    assert response.status_code == 400


def test_user_list_negative_limit_is_refused(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users?limit=-1", headers=headers)

    assert response.status_code == 400


def test_user_list_non_integer_limit_is_refused(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users?limit=daug", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be an integer"


def test_user_list_non_integer_offset_is_refused(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users?offset=nulis", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "offset must be an integer"


def test_user_list_negative_offset_is_refused(client, admin):
    _, headers = admin

    response = client.get("/api/admin/users?limit=5&offset=-1", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "offset must be zero or greater"


def test_a_refused_page_returns_no_rows_at_all(client, admin, make_user):
    _, headers = admin
    make_user(username="paslaptis")

    body = client.get("/api/admin/users?limit=0", headers=headers).get_json()

    assert "users" not in body




# ===========================================================
# PATCH /api/admin/users/<id> — body validation
# ===========================================================

def test_an_unknown_user_is_not_found(client, admin):
    _, headers = admin

    response = client.patch(f"/api/admin/users/{uuid.uuid4()}", headers=headers,
                            json={"role": "teacher"})

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_an_unknown_user_is_not_found_even_when_the_body_is_also_wrong(client, admin):
    _, headers = admin

    response = client.patch(f"/api/admin/users/{uuid.uuid4()}", headers=headers,
                            json={"role": "wizard"})

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_a_list_body_is_refused(client, admin, make_user, db):
    # The app-wide validate_json_input hook answers a non-object
    # body before the blueprint sees it, which is why this reads
    # "JSON body must be an object" and not the route's own
    # message — either way nothing is written
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers, json=[1, 2])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"
    assert _audit_rows(db) == []


def test_a_body_that_is_not_json_is_refused(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            data="ne json", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_an_unknown_role_is_refused(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"role": "superadmin"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"
    assert db.execute("SELECT role FROM users WHERE id = ?", (user["id"],)).fetchone()[0] == "student"


@pytest.mark.parametrize("role", ["ADMIN", "Student", "", "moderator", "root"])
def test_only_the_four_known_roles_are_accepted(client, admin, make_user, role):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": role})

    assert response.status_code == 400


@pytest.mark.parametrize("value", [0, 1, "false", "true", "", [], {}, 0.0])
def test_active_must_be_a_real_json_boolean(client, admin, make_user, value):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"active": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "active must be a boolean"


def test_a_zero_active_cannot_deactivate_the_calling_admin(client, admin, db):
    admin_user, headers = admin

    response = client.patch(f"/api/admin/users/{admin_user['id']}", headers=headers,
                            json={"active": 0})

    assert response.status_code == 400
    assert response.get_json()["error"] == "active must be a boolean"
    assert db.execute("SELECT active FROM users WHERE id = ?", (admin_user["id"],)).fetchone()[0] == 1
    assert client.get("/api/auth/me", headers=headers).status_code == 200


def test_an_empty_patch_is_nothing_to_update(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Nothing to update"


def test_a_patch_of_unknown_keys_only_is_nothing_to_update(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"displayName": "Naujas", "email": "kitas@knf.vu.lt"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Nothing to update"


def test_explicit_nulls_are_nothing_to_update(client, admin, make_user):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"role": None, "active": None})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Nothing to update"




# ===========================================================
# PATCH /api/admin/users/<id> — role changes
# ===========================================================

@pytest.mark.contract
def test_a_role_change_answers_the_fresh_row_in_the_list_shape(client, admin, make_user):
    _, headers = admin
    user = make_user(username="busimas", display_name="Būsimas Dėstytojas")

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"role": "teacher"})

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == USER_FIELDS
    assert body == {
        "id": user["id"],
        "username": "busimas",
        "email": "busimas@knf.vu.lt",
        "displayName": "Būsimas Dėstytojas",
        "role": "teacher",
        "active": True,
        "createdAt": body["createdAt"],
    }


@pytest.mark.parametrize("role", ["student", "teacher", "curator", "admin"])
def test_every_known_role_can_be_granted(client, admin, make_user, db, role):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": role})

    assert response.status_code == 200
    assert response.get_json()["role"] == role
    assert db.execute("SELECT role FROM users WHERE id = ?", (user["id"],)).fetchone()[0] == role


def test_a_promoted_curator_can_reach_the_curator_routes(client, admin, make_user, auth_headers):
    _, headers = admin
    user = make_user(username="kuratorius")
    assert client.get("/api/admin/invitations", headers=auth_headers(user)).status_code == 403

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": "curator"})

    promoted = auth_headers(user)
    assert client.get("/api/admin/invitations", headers=promoted).status_code == 200
    assert client.get("/api/admin/users", headers=promoted).status_code == 403


def test_a_promoted_admin_can_reach_the_admin_routes(client, admin, make_user, auth_headers):
    _, headers = admin
    user = make_user(username="naujas_adminas")

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": "admin"})

    assert client.get("/api/admin/users", headers=auth_headers(user)).status_code == 200


def test_a_role_change_leaves_the_active_flag_alone(client, admin, make_user, db):
    _, headers = admin
    user = make_user(active=0)

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"role": "teacher"})

    assert response.get_json()["active"] is False
    assert db.execute("SELECT active FROM users WHERE id = ?", (user["id"],)).fetchone()[0] == 0


def test_a_role_change_stamps_updated_at_in_iso_t_form(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": "teacher"})

    updated_at = db.execute("SELECT updated_at FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
    assert "T" in updated_at and " " not in updated_at
    assert datetime.fromisoformat(updated_at).tzinfo is not None


def test_a_demoted_admin_keeps_their_token_until_it_expires(client, admin, make_user, auth_headers):
    # Documented gotcha: only DEACTIVATION revokes sessions, so a
    # role change alone leaves the old bearer token alive
    _, headers = admin
    other = make_user(username="buves_adminas", role="admin")
    other_headers = auth_headers(other)

    client.patch(f"/api/admin/users/{other['id']}", headers=headers, json={"role": "student"})

    assert client.get("/api/auth/me", headers=other_headers).status_code == 200
    assert client.get("/api/admin/users", headers=other_headers).status_code == 403


def test_a_role_change_does_not_touch_anybody_else(client, admin, make_user, db):
    _, headers = admin
    target = make_user(username="taikinys")
    bystander = make_user(username="salia")

    client.patch(f"/api/admin/users/{target['id']}", headers=headers, json={"role": "teacher"})

    assert db.execute("SELECT role FROM users WHERE id = ?", (bystander["id"],)).fetchone()[0] == "student"




# ===========================================================
# PATCH /api/admin/users/<id> — admin continuity guards
# ===========================================================

def test_an_admin_cannot_remove_their_own_admin_role(client, admin, db):
    admin_user, headers = admin

    response = client.patch(f"/api/admin/users/{admin_user['id']}", headers=headers,
                            json={"role": "student"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove your own admin role"
    assert db.execute("SELECT role FROM users WHERE id = ?", (admin_user["id"],)).fetchone()[0] == "admin"


def test_an_admin_cannot_remove_their_own_admin_role_even_with_a_spare_admin(client, admin, make_user, db):
    admin_user, headers = admin
    make_user(username="kitas_adminas", role="admin")

    response = client.patch(f"/api/admin/users/{admin_user['id']}", headers=headers,
                            json={"role": "curator"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove your own admin role"


def test_an_admin_may_keep_their_own_admin_role(client, admin):
    admin_user, headers = admin

    response = client.patch(f"/api/admin/users/{admin_user['id']}", headers=headers,
                            json={"role": "admin"})

    assert response.status_code == 200
    assert response.get_json()["role"] == "admin"


def test_an_admin_cannot_deactivate_their_own_account(client, admin, db):
    admin_user, headers = admin

    response = client.patch(f"/api/admin/users/{admin_user['id']}", headers=headers,
                            json={"active": False})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot deactivate your own account"
    assert db.execute("SELECT active FROM users WHERE id = ?", (admin_user["id"],)).fetchone()[0] == 1
    assert _session_count(db, admin_user["id"]) == 1


def test_an_admin_may_reactivate_themselves(client, admin):
    admin_user, headers = admin

    response = client.patch(f"/api/admin/users/{admin_user['id']}", headers=headers,
                            json={"active": True})

    assert response.status_code == 200
    assert response.get_json()["active"] is True


def test_a_second_admin_can_be_demoted_while_another_admin_remains(client, admin, make_user, db):
    _, headers = admin
    other = make_user(username="antras_adminas", role="admin")

    response = client.patch(f"/api/admin/users/{other['id']}", headers=headers,
                            json={"role": "teacher"})

    assert response.status_code == 200
    assert db.execute("SELECT role FROM users WHERE id = ?", (other["id"],)).fetchone()[0] == "teacher"


def test_a_second_admin_can_be_deactivated_while_another_admin_remains(client, admin, make_user, db):
    _, headers = admin
    other = make_user(username="antras_adminas", role="admin")

    response = client.patch(f"/api/admin/users/{other['id']}", headers=headers,
                            json={"active": False})

    assert response.status_code == 200
    assert db.execute("SELECT active FROM users WHERE id = ?", (other["id"],)).fetchone()[0] == 0


def test_the_last_active_admin_cannot_be_demoted(client, admin, make_user, db):
    admin_user, headers = admin
    other = make_user(username="paskutinis", role="admin")
    _uncount_actor(db, admin_user["id"])

    response = client.patch(f"/api/admin/users/{other['id']}", headers=headers,
                            json={"role": "student"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove the last active admin"
    assert db.execute("SELECT role FROM users WHERE id = ?", (other["id"],)).fetchone()[0] == "admin"


def test_the_last_active_admin_cannot_be_deactivated(client, admin, make_user, db):
    admin_user, headers = admin
    other = make_user(username="paskutinis", role="admin")
    _uncount_actor(db, admin_user["id"])

    response = client.patch(f"/api/admin/users/{other['id']}", headers=headers,
                            json={"active": False})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove the last active admin"
    assert db.execute("SELECT active FROM users WHERE id = ?", (other["id"],)).fetchone()[0] == 1


def test_a_deactivated_admin_does_not_count_as_a_remaining_admin(client, admin, make_user, db):
    admin_user, headers = admin
    make_user(username="miegantis_adminas", role="admin", active=0)
    other = make_user(username="paskutinis", role="admin")
    _uncount_actor(db, admin_user["id"])

    response = client.patch(f"/api/admin/users/{other['id']}", headers=headers,
                            json={"role": "student"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove the last active admin"


def test_the_admin_count_is_never_consulted_for_a_non_admin_target(client, admin, make_user, db):
    admin_user, headers = admin
    student = make_user(username="studentas")
    _uncount_actor(db, admin_user["id"])

    response = client.patch(f"/api/admin/users/{student['id']}", headers=headers,
                            json={"role": "teacher", "active": False})

    assert response.status_code == 200
    assert response.get_json()["role"] == "teacher"


def test_promoting_the_last_admin_further_is_not_a_demotion(client, admin, make_user, db):
    admin_user, headers = admin
    other = make_user(username="paskutinis", role="admin")
    _uncount_actor(db, admin_user["id"])

    response = client.patch(f"/api/admin/users/{other['id']}", headers=headers,
                            json={"role": "admin", "active": True})

    assert response.status_code == 200




# ===========================================================
# PATCH /api/admin/users/<id> — the active flag and what it revokes
# ===========================================================

def test_deactivating_a_user_answers_active_false(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"active": False})

    assert response.status_code == 200
    assert response.get_json()["active"] is False
    assert db.execute("SELECT active FROM users WHERE id = ?", (user["id"],)).fetchone()[0] == 0


def test_deactivating_a_user_deletes_their_sessions(client, admin, actor, db):
    _, headers = admin
    user, _ = actor
    assert _session_count(db, user["id"]) == 1

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": False})

    assert _session_count(db, user["id"]) == 0


def test_deactivating_a_user_deletes_their_push_tokens(client, admin, actor, db):
    _, headers = admin
    user, _ = actor
    _seed_push_token(db, user["id"])
    _seed_push_token(db, user["id"])
    assert _push_token_count(db, user["id"]) == 2

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": False})

    assert _push_token_count(db, user["id"]) == 0


def test_only_the_targeted_users_sessions_and_tokens_are_deleted(client, admin, make_user,
                                                                 auth_headers, db):
    admin_user, headers = admin
    target = make_user(username="taikinys")
    bystander = make_user(username="salia")
    auth_headers(target)
    auth_headers(bystander)
    _seed_push_token(db, target["id"])
    _seed_push_token(db, bystander["id"])

    client.patch(f"/api/admin/users/{target['id']}", headers=headers, json={"active": False})

    assert _session_count(db, bystander["id"]) == 1
    assert _push_token_count(db, bystander["id"]) == 1
    assert _session_count(db, admin_user["id"]) == 1


def test_a_deactivated_user_cannot_log_in(client, admin, make_user):
    _, headers = admin
    user = make_user(username="uzdarytas")

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": False})

    response = client.post("/api/auth/login",
                           json={"username": user["username"], "password": user["password"]})
    assert response.status_code == 403
    assert response.get_json()["code"] == "account_deactivated"


def test_a_deactivated_users_existing_token_stops_working(client, admin, actor):
    _, headers = admin
    user, user_headers = actor
    assert client.get("/api/auth/me", headers=user_headers).status_code == 200

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": False})

    assert client.get("/api/auth/me", headers=user_headers).status_code == 401


def test_reactivating_a_user_lets_them_log_in_again(client, admin, make_user, auth_headers):
    _, headers = admin
    user = make_user(username="grazintas")
    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": False})

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": True})

    assert response.status_code == 200
    assert response.get_json()["active"] is True
    assert client.get("/api/auth/me", headers=auth_headers(user)).status_code == 200


def test_reactivating_a_user_keeps_their_new_session(client, admin, make_user, auth_headers, db):
    _, headers = admin
    user = make_user(username="grazintas")
    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": False})
    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": True})
    fresh = auth_headers(user)

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": True})

    assert _session_count(db, user["id"]) == 1
    assert client.get("/api/auth/me", headers=fresh).status_code == 200


def test_deactivating_an_already_inactive_user_is_accepted(client, admin, make_user):
    _, headers = admin
    user = make_user(active=0)

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"active": False})

    assert response.status_code == 200
    assert response.get_json()["active"] is False


def test_a_role_and_an_active_change_apply_together(client, admin, actor, db):
    _, headers = admin
    user, user_headers = actor

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"role": "teacher", "active": False})

    assert response.status_code == 200
    body = response.get_json()
    assert body["role"] == "teacher" and body["active"] is False
    assert _session_count(db, user["id"]) == 0


def test_deactivation_disconnects_the_users_live_sockets(client, admin, actor, monkeypatch):
    from app.chat import events as chat_events
    _, headers = admin
    user, _ = actor
    seen = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", lambda uid: seen.append(uid))

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": False})

    assert seen == [user["id"]]


def test_a_role_change_alone_disconnects_nothing(client, admin, actor, monkeypatch):
    from app.chat import events as chat_events
    _, headers = admin
    user, _ = actor
    seen = []
    monkeypatch.setattr(chat_events, "disconnect_user_sockets", lambda uid: seen.append(uid))

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": "teacher"})

    assert seen == []


def test_a_failing_socket_disconnect_never_fails_the_deactivation(client, admin, actor, monkeypatch, db):
    from app.chat import events as chat_events
    _, headers = admin
    user, _ = actor

    def _boom(_user_id):
        raise RuntimeError("socket layer down")

    monkeypatch.setattr(chat_events, "disconnect_user_sockets", _boom)

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"active": False})

    assert response.status_code == 200
    assert db.execute("SELECT active FROM users WHERE id = ?", (user["id"],)).fetchone()[0] == 0


def test_a_missing_socket_layer_never_fails_the_deactivation(client, admin, actor, monkeypatch, db):
    # sys.modules[name] = None makes the guarded lazy import raise
    # ImportError, which is the pre-chat/events.py state the helper
    # is written to survive
    _, headers = admin
    user, _ = actor
    monkeypatch.setitem(sys.modules, "app.chat.events", None)

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"active": False})

    assert response.status_code == 200
    assert db.execute("SELECT active FROM users WHERE id = ?", (user["id"],)).fetchone()[0] == 0




# ===========================================================
# admin_audit — the trail every mutation leaves
# ===========================================================

def test_a_role_change_writes_one_audit_row(client, admin, make_user, db):
    admin_user, headers = admin
    user = make_user()

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": "teacher"})

    rows = _audit_rows(db, "user.role")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == admin_user["id"]
    assert rows[0]["target"] == user["id"]
    assert json.loads(rows[0]["payload"]) == {"from": "student", "to": "teacher"}


def test_a_deactivation_writes_one_audit_row(client, admin, make_user, db):
    admin_user, headers = admin
    user = make_user()

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": False})

    rows = _audit_rows(db, "user.active")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == admin_user["id"]
    assert json.loads(rows[0]["payload"]) == {"active": False}


def test_a_reactivation_writes_an_audit_row_saying_so(client, admin, make_user, db):
    _, headers = admin
    user = make_user(active=0)

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": True})

    rows = _audit_rows(db, "user.active")
    assert json.loads(rows[0]["payload"]) == {"active": True}


def test_a_combined_patch_writes_one_row_per_changed_field(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                 json={"role": "curator", "active": False})

    assert [r["action"] for r in _audit_rows(db)] == ["user.role", "user.active"]


def test_audit_rows_are_stamped_in_iso_t_form(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": "teacher"})

    created_at = _audit_rows(db)[0]["created_at"]
    assert "T" in created_at and " " not in created_at
    assert datetime.fromisoformat(created_at).tzinfo is not None


def test_a_refused_patch_leaves_no_audit_row(client, admin, make_user, db):
    _, headers = admin
    user = make_user()

    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"role": "wizard"})
    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={})
    client.patch(f"/api/admin/users/{user['id']}", headers=headers, json={"active": "false"})

    assert _audit_rows(db) == []


def test_a_patch_on_an_unknown_user_leaves_no_audit_row(client, admin, db):
    _, headers = admin

    client.patch(f"/api/admin/users/{uuid.uuid4()}", headers=headers, json={"role": "teacher"})

    assert _audit_rows(db) == []


def test_a_guard_refusal_leaves_no_audit_row(client, admin, db):
    admin_user, headers = admin

    client.patch(f"/api/admin/users/{admin_user['id']}", headers=headers, json={"role": "student"})
    client.patch(f"/api/admin/users/{admin_user['id']}", headers=headers, json={"active": False})

    assert _audit_rows(db) == []


def test_the_role_change_still_commits_when_the_audit_table_is_gone(client, admin, make_user, db):
    # A pre-v40 database file has no admin_audit table; the audit
    # write must be swallowed, never fail the admin action
    _, headers = admin
    user = make_user()
    db.execute("DROP TABLE admin_audit")
    db.commit()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"role": "teacher"})

    assert response.status_code == 200
    assert db.execute("SELECT role FROM users WHERE id = ?", (user["id"],)).fetchone()[0] == "teacher"


def test_the_deactivation_still_commits_when_the_audit_table_is_gone(client, admin, actor, db):
    _, headers = admin
    user, _ = actor
    db.execute("DROP TABLE admin_audit")
    db.commit()

    response = client.patch(f"/api/admin/users/{user['id']}", headers=headers,
                            json={"active": False})

    assert response.status_code == 200
    assert _session_count(db, user["id"]) == 0


def test_minting_a_code_writes_an_audit_row(client, admin, db):
    admin_user, headers = admin

    response = client.post("/api/admin/invitations", headers=headers, json={"role": "teacher"})

    assert response.status_code == 201
    rows = _audit_rows(db, "invitation.create")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == admin_user["id"]
    assert rows[0]["target"] == response.get_json()["id"]
    assert json.loads(rows[0]["payload"])["role"] == "teacher"


def test_revoking_a_code_writes_an_audit_row(client, admin, db):
    _, headers = admin
    code_id = client.post("/api/admin/invitations", headers=headers, json={}).get_json()["id"]

    response = client.delete(f"/api/admin/invitations/{code_id}", headers=headers)

    assert response.status_code == 200
    rows = _audit_rows(db, "invitation.revoke")
    assert len(rows) == 1
    assert rows[0]["target"] == code_id
    assert rows[0]["payload"] is None


def test_revoking_an_unknown_code_writes_no_audit_row(client, admin, db):
    _, headers = admin

    response = client.delete(f"/api/admin/invitations/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert _audit_rows(db, "invitation.revoke") == []


def test_a_curator_cannot_mint_a_privileged_code(client, admin, make_user, auth_headers, db):
    _, _ = admin
    curator = make_user(username="kuratorius", role="curator")

    response = client.post("/api/admin/invitations", headers=auth_headers(curator),
                           json={"role": "admin"})

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only admins can create admin/curator invitations"
    assert _audit_rows(db, "invitation.create") == []


def test_a_malformed_mint_body_is_refused_before_anything_is_written(client, admin, db):
    _, headers = admin

    response = client.post("/api/admin/invitations", headers=headers,
                           data="{ne json", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"
    assert _audit_rows(db, "invitation.create") == []


@pytest.mark.parametrize("value", ["5", 5.0, True, None, [5]])
def test_max_uses_must_be_an_integer(client, admin, value):
    _, headers = admin

    response = client.post("/api/admin/invitations", headers=headers, json={"max_uses": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be an integer"


@pytest.mark.parametrize("value", ["24", 24.5, False, None])
def test_expires_hours_must_be_an_integer(client, admin, value):
    _, headers = admin

    response = client.post("/api/admin/invitations", headers=headers,
                           json={"expires_hours": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be an integer"


@pytest.mark.parametrize("value", [0, -1, 1001, 10000])
def test_an_out_of_range_max_uses_is_rejected_never_clamped(client, admin, value):
    _, headers = admin

    response = client.post("/api/admin/invitations", headers=headers, json={"max_uses": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be between 1 and 1000"


@pytest.mark.parametrize("value", [1, 1000])
def test_the_max_uses_bounds_themselves_are_accepted(client, admin, value):
    _, headers = admin

    response = client.post("/api/admin/invitations", headers=headers, json={"max_uses": value})

    assert response.status_code == 201
    assert response.get_json()["maxUses"] == value


@pytest.mark.parametrize("value", [0, -1, 8761, 10 ** 17])
def test_an_out_of_range_expiry_is_rejected_never_clamped(client, admin, value):
    _, headers = admin

    response = client.post("/api/admin/invitations", headers=headers,
                           json={"expires_hours": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be between 1 and 8760"


@pytest.mark.parametrize("value", [1, 8760])
def test_the_expiry_bounds_themselves_are_accepted(client, admin, value):
    _, headers = admin

    response = client.post("/api/admin/invitations", headers=headers,
                           json={"expires_hours": value})

    assert response.status_code == 201


def test_an_unknown_invitation_role_is_refused(client, admin, db):
    _, headers = admin

    response = client.post("/api/admin/invitations", headers=headers, json={"role": "rektorius"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"
    assert _audit_rows(db, "invitation.create") == []


def test_a_nonsense_configured_expiry_falls_back_to_the_default(app, client, admin):
    # A bad INVITATION_EXPIRY_HOURS in the environment must not
    # turn every default mint into a 400
    _, headers = admin
    app.config["INVITATION_EXPIRY_HOURS"] = "septynios paros"

    response = client.post("/api/admin/invitations", headers=headers, json={})

    assert response.status_code == 201
    expires_at = datetime.fromisoformat(response.get_json()["expiresAt"])
    assert timedelta(hours=167) < expires_at - datetime.now(timezone.utc) < timedelta(hours=169)


def test_the_invitation_listing_refuses_a_bad_page(client, admin):
    _, headers = admin

    response = client.get("/api/admin/invitations?limit=nulis", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be an integer"
    assert "invitations" not in response.get_json()


def test_the_broadcast_reaches_the_real_socketio_instance(app):
    # The lookup is deferred to call time as a cycle guard; every
    # other broadcast test swaps it out, so this is the one place
    # that proves it resolves the instance app/__init__.py binds
    from app import socketio

    assert admin_routes._get_socketio() is socketio


def test_an_unparsable_expiry_reads_as_expired_instead_of_breaking_the_listing(client, admin, db):
    admin_user, headers = admin
    broken = _seed_code(db, admin_user["id"], "kada nors")
    naive_future = _seed_code(db, admin_user["id"],
                              (datetime.now(timezone.utc) + timedelta(days=1))
                              .replace(tzinfo=None).isoformat())

    response = client.get("/api/admin/invitations", headers=headers)

    assert response.status_code == 200
    codes = {c["id"]: c for c in response.get_json()["invitations"]}
    assert codes[broken]["expired"] is True
    assert codes[naive_future]["expired"] is False




# ===========================================================
# GET /api/admin/stats
# ===========================================================

@pytest.mark.contract
def test_stats_answers_exactly_the_five_dashboard_counters(client, admin):
    _, headers = admin

    response = client.get("/api/admin/stats", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == STATS_FIELDS
    assert all(isinstance(v, int) for v in body.values())


def test_stats_counts_users(client, admin, make_user):
    _, headers = admin
    make_user()
    make_user()

    body = client.get("/api/admin/stats", headers=headers).get_json()

    assert body["users"] == 3


def test_stats_counts_a_deactivated_user_too(client, admin, make_user):
    _, headers = admin
    make_user(active=0)

    assert client.get("/api/admin/stats", headers=headers).get_json()["users"] == 2


def test_stats_starts_at_zero_posts_and_comments(client, admin):
    _, headers = admin

    body = client.get("/api/admin/stats", headers=headers).get_json()

    assert body["posts"] == 0
    assert body["comments"] == 0


def test_stats_counts_every_post_whatever_its_source(client, admin, db):
    _, headers = admin
    for source in ("app", "user", "faculty", "knf.vu.lt", "vu.lt"):
        _seed_article(db, source=source)

    body = client.get("/api/admin/stats", headers=headers).get_json()

    assert body["posts"] == 5


def test_only_the_two_scraper_sources_count_as_scraped(client, admin, db):
    _, headers = admin
    _seed_article(db, source="knf.vu.lt")
    _seed_article(db, source="knf.vu.lt")
    _seed_article(db, source="vu.lt")
    _seed_article(db, source="app")
    _seed_article(db, source="user")
    _seed_article(db, source="faculty")

    body = client.get("/api/admin/stats", headers=headers).get_json()

    assert body["scrapedArticles"] == 3
    assert body["posts"] == 6


def test_stats_counts_comments(client, admin, make_user, db):
    _, headers = admin
    user = make_user()
    post_id = _seed_article(db, source="app")
    _seed_comment(db, post_id, user["id"])
    _seed_comment(db, post_id, user["id"])

    assert client.get("/api/admin/stats", headers=headers).get_json()["comments"] == 2


def test_the_bootstrap_invitation_counts_as_active(client, admin):
    _, headers = admin

    assert client.get("/api/admin/stats", headers=headers).get_json()["activeInvitations"] == 1


def test_an_expired_code_is_not_active(client, admin, db):
    admin_user, headers = admin
    _seed_code(db, admin_user["id"], _iso(-3600))

    assert client.get("/api/admin/stats", headers=headers).get_json()["activeInvitations"] == 1


def test_a_fully_used_code_is_not_active(client, admin, db):
    admin_user, headers = admin
    _seed_code(db, admin_user["id"], _iso(86400), use_count=5, max_uses=5)

    assert client.get("/api/admin/stats", headers=headers).get_json()["activeInvitations"] == 1


def test_a_partly_used_unexpired_code_is_active(client, admin, db):
    admin_user, headers = admin
    _seed_code(db, admin_user["id"], _iso(86400), use_count=4, max_uses=5)

    assert client.get("/api/admin/stats", headers=headers).get_json()["activeInvitations"] == 2


def test_a_space_form_expiry_reaches_the_same_verdict_as_the_listing(client, admin, db):
    # A hand-edited row can hold SQLite's space-separated form;
    # compared raw, ' ' sorts before 'T' and a same-day expired
    # code would still have counted as active
    admin_user, headers = admin
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    _seed_code(db, admin_user["id"], stale)

    assert client.get("/api/admin/stats", headers=headers).get_json()["activeInvitations"] == 1


def test_a_space_form_future_expiry_still_counts_as_active(client, admin, db):
    admin_user, headers = admin
    ahead = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    _seed_code(db, admin_user["id"], ahead)

    assert client.get("/api/admin/stats", headers=headers).get_json()["activeInvitations"] == 2


def test_a_freshly_minted_code_counts_as_active(client, admin):
    _, headers = admin
    client.post("/api/admin/invitations", headers=headers, json={"expires_hours": 1})

    assert client.get("/api/admin/stats", headers=headers).get_json()["activeInvitations"] == 2


def test_stats_are_served_from_the_snapshot_inside_the_window(client, admin, make_user):
    _, headers = admin

    with time_machine.travel(datetime.now(timezone.utc), tick=False) as traveller:
        first = client.get("/api/admin/stats", headers=headers).get_json()
        make_user()
        traveller.shift(timedelta(seconds=admin_routes._STATS_CACHE_TTL - 1))
        again = client.get("/api/admin/stats", headers=headers).get_json()

    assert first["users"] == 1
    assert again == first


def test_stats_rebuild_once_the_snapshot_ages_out(client, admin, make_user):
    _, headers = admin

    with time_machine.travel(datetime.now(timezone.utc), tick=False) as traveller:
        first = client.get("/api/admin/stats", headers=headers).get_json()
        make_user()
        traveller.shift(timedelta(seconds=admin_routes._STATS_CACHE_TTL + 1))
        later = client.get("/api/admin/stats", headers=headers).get_json()

    assert first["users"] == 1
    assert later["users"] == 2


def test_stats_are_never_cached_by_the_client(client, admin):
    _, headers = admin

    response = client.get("/api/admin/stats", headers=headers)

    assert "no-store" in response.headers.get("Cache-Control", "")




# ===========================================================
# POST /api/admin/notifications — the broadcast
# ===========================================================

@pytest.mark.contract
def test_a_broadcast_is_accepted_as_a_queued_job(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    response = _broadcast(client, headers, title="Dėmesio", body="Rytoj nevyks paskaitos")

    assert response.status_code == 202
    job = response.get_json()
    assert set(job) == JOB_FIELDS
    assert job["status"] == "queued"
    assert job["sent"] == 0 and job["failed"] == 0
    assert job["title"] == "Dėmesio"
    assert job["finishedAt"] is None
    assert uuid.UUID(job["jobId"])


def test_the_accepted_body_is_a_copy_not_the_live_record(client, admin, inline_broadcast, fake_notify):
    # The 202 is built from the copy taken BEFORE the fan-out ran,
    # so an already-finished job still reads as queued to the caller
    _, headers = admin

    job = _broadcast(client, headers).get_json()

    status = client.get(f"/api/admin/notifications/{job['jobId']}", headers=headers).get_json()
    assert job["status"] == "queued"
    assert status["status"] == "done"


def test_a_broadcast_hands_the_fan_out_to_a_background_task(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    _broadcast(client, headers, title="Tema", body="Tekstas")

    assert len(inline_broadcast.tasks) == 1
    target, args, _ = inline_broadcast.tasks[0]
    assert target is admin_routes._run_broadcast
    assert args[1:3] == ("Tema", "Tekstas")


def test_a_broadcast_goes_out_on_the_admin_channel(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    _broadcast(client, headers, title="Tema", body="Tekstas")

    assert fake_notify.calls == [{"channel": "admin", "title": "Tema", "body": "Tekstas",
                                 "data": {"type": "admin_announcement"}}]


def test_a_broadcast_writes_an_audit_row(client, admin, inline_broadcast, fake_notify, db):
    admin_user, headers = admin

    job = _broadcast(client, headers, title="Svarbu").get_json()

    rows = _audit_rows(db, "notification.broadcast")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == admin_user["id"]
    assert rows[0]["target"] == job["jobId"]
    assert json.loads(rows[0]["payload"]) == {"title": "Svarbu"}


def test_the_type_marker_survives_a_caller_supplied_type(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    _broadcast(client, headers, data={"type": "chat_message", "postId": "42"})

    assert fake_notify.calls[0]["data"] == {"type": "admin_announcement", "postId": "42"}


def test_caller_data_rides_along_with_the_marker(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    _broadcast(client, headers, data={"postId": "7", "deep": {"a": 1}})

    assert fake_notify.calls[0]["data"] == {"postId": "7", "deep": {"a": 1},
                                            "type": "admin_announcement"}


def test_an_absent_data_object_still_carries_the_marker(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    _broadcast(client, headers)

    assert fake_notify.calls[0]["data"] == {"type": "admin_announcement"}


def test_a_broadcast_reports_the_accepted_ticket_count(client, admin, inline_broadcast, fake_notify):
    _, headers = admin
    fake_notify.result = 12

    job_id = _broadcast(client, headers).get_json()["jobId"]

    status = client.get(f"/api/admin/notifications/{job_id}", headers=headers).get_json()
    assert status["status"] == "done"
    assert status["sent"] == 12
    assert status["failed"] == 0
    assert status["message"] == "Accepted by Expo for 12 device token(s)"
    assert status["finishedAt"] is not None


def test_a_tuple_result_is_read_as_sent_and_failed(client, admin, inline_broadcast, fake_notify):
    _, headers = admin
    fake_notify.result = (7, 3)

    job_id = _broadcast(client, headers).get_json()["jobId"]

    status = client.get(f"/api/admin/notifications/{job_id}", headers=headers).get_json()
    assert (status["sent"], status["failed"]) == (7, 3)


def test_a_dict_result_is_read_as_sent_and_failed(client, admin, inline_broadcast, fake_notify):
    _, headers = admin
    fake_notify.result = {"sent": 5, "failed": 2}

    job_id = _broadcast(client, headers).get_json()["jobId"]

    status = client.get(f"/api/admin/notifications/{job_id}", headers=headers).get_json()
    assert (status["sent"], status["failed"]) == (5, 2)


def test_an_empty_dict_result_reads_as_zero(client, admin, inline_broadcast, fake_notify):
    _, headers = admin
    fake_notify.result = {}

    job_id = _broadcast(client, headers).get_json()["jobId"]

    status = client.get(f"/api/admin/notifications/{job_id}", headers=headers).get_json()
    assert (status["sent"], status["failed"]) == (0, 0)


def test_a_none_result_reads_as_zero(client, admin, inline_broadcast, fake_notify):
    _, headers = admin
    fake_notify.result = None

    job_id = _broadcast(client, headers).get_json()["jobId"]

    assert client.get(f"/api/admin/notifications/{job_id}",
                      headers=headers).get_json()["sent"] == 0


def test_a_failing_fan_out_marks_the_job_failed(client, admin, inline_broadcast, fake_notify):
    _, headers = admin
    fake_notify.result = RuntimeError("Expo unreachable")

    response = _broadcast(client, headers)

    assert response.status_code == 202
    job_id = response.get_json()["jobId"]
    status = client.get(f"/api/admin/notifications/{job_id}", headers=headers).get_json()
    assert status["status"] == "failed"
    assert status["message"] == "Broadcast failed — see the server log"
    assert status["finishedAt"] is not None


def test_a_broadcast_with_no_devices_registered_sends_to_nobody(client, admin, inline_broadcast):
    # The REAL notify_channel, with no push_tokens rows behind it:
    # it must reach the end without a single HTTP call
    _, headers = admin

    job_id = _broadcast(client, headers).get_json()["jobId"]

    status = client.get(f"/api/admin/notifications/{job_id}", headers=headers).get_json()
    assert status["status"] == "done"
    assert status["sent"] == 0




# ===========================================================
# POST /api/admin/notifications — body validation
# ===========================================================

def test_a_broadcast_needs_an_object_body(client, admin, inline_broadcast):
    # Refused by the app-wide validate_json_input hook — see
    # test_a_list_body_is_refused
    _, headers = admin

    response = client.post("/api/admin/notifications", headers=headers, json=["labas"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"
    assert inline_broadcast.tasks == []


def test_a_malformed_broadcast_body_is_refused_by_the_route(client, admin, inline_broadcast):
    _, headers = admin

    response = client.post("/api/admin/notifications", headers=headers,
                           data="{ne json", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"
    assert inline_broadcast.tasks == []


@pytest.mark.parametrize("field,value", [
    ("title", 5), ("title", None), ("title", ["a"]),
    ("body", 5), ("body", None), ("body", {"a": 1}),
])
def test_a_broadcast_title_and_body_must_be_strings(client, admin, inline_broadcast, field, value):
    _, headers = admin

    response = _broadcast(client, headers, **{field: value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body must be strings"


@pytest.mark.parametrize("title,body", [("", "Turinys"), ("Tema", ""), ("   ", "Turinys"),
                                        ("Tema", "\n\t "), ("", "")])
def test_a_broadcast_needs_both_a_title_and_a_body(client, admin, inline_broadcast, title, body):
    _, headers = admin

    response = _broadcast(client, headers, title=title, body=body)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body are required"


def test_a_broadcast_with_no_title_or_body_at_all_is_refused(client, admin, inline_broadcast):
    _, headers = admin

    response = client.post("/api/admin/notifications", headers=headers, json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body are required"


def test_a_two_hundred_character_title_is_accepted(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    response = _broadcast(client, headers, title="t" * 200)

    assert response.status_code == 202


def test_a_title_over_two_hundred_characters_is_refused(client, admin, inline_broadcast):
    _, headers = admin

    response = _broadcast(client, headers, title="t" * 201)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title must be at most 200 characters"


def test_a_title_is_trimmed_before_the_length_check(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    response = _broadcast(client, headers, title="  " + "t" * 200 + "  ")

    assert response.status_code == 202
    assert response.get_json()["title"] == "t" * 200


def test_a_thousand_character_body_is_accepted(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    response = _broadcast(client, headers, body="b" * 1000)

    assert response.status_code == 202


def test_a_body_over_a_thousand_characters_is_refused(client, admin, inline_broadcast):
    _, headers = admin

    response = _broadcast(client, headers, body="b" * 1001)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Body must be at most 1000 characters"


@pytest.mark.parametrize("value", ["tekstas", 5, ["a"], True])
def test_broadcast_data_must_be_an_object(client, admin, inline_broadcast, value):
    _, headers = admin

    response = _broadcast(client, headers, data=value)

    assert response.status_code == 400
    assert response.get_json()["error"] == "data must be an object"


def test_a_null_data_is_treated_as_absent(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    response = _broadcast(client, headers, data=None)

    assert response.status_code == 202
    assert fake_notify.calls[0]["data"] == {"type": "admin_announcement"}


def test_a_payload_at_the_ceiling_is_accepted(client, admin, inline_broadcast, fake_notify):
    _, headers = admin

    response = _broadcast(client, headers, data=_padded_data(admin_routes._BROADCAST_DATA_MAX))

    assert response.status_code == 202


def test_a_payload_one_byte_over_the_ceiling_is_refused(client, admin, inline_broadcast):
    _, headers = admin

    response = _broadcast(client, headers, data=_padded_data(admin_routes._BROADCAST_DATA_MAX + 1))

    assert response.status_code == 400
    assert response.get_json()["error"] == "data must serialise to at most 3072 bytes"


def test_a_refused_broadcast_starts_no_job_and_writes_no_audit_row(client, admin, inline_broadcast, db):
    _, headers = admin

    _broadcast(client, headers, title="")

    assert inline_broadcast.tasks == []
    assert _audit_rows(db, "notification.broadcast") == []
    assert admin_routes._broadcast_jobs == {}




# ===========================================================
# GET /api/admin/notifications/<job_id>
# ===========================================================

def test_an_unknown_broadcast_job_is_not_found(client, admin):
    _, headers = admin

    response = client.get(f"/api/admin/notifications/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Broadcast job not found"


def test_a_job_id_from_another_broadcast_is_not_confused(client, admin, inline_broadcast, fake_notify):
    _, headers = admin
    fake_notify.result = 4
    first = _broadcast(client, headers, title="Pirmas").get_json()["jobId"]
    fake_notify.result = 9
    second = _broadcast(client, headers, title="Antras").get_json()["jobId"]

    first_status = client.get(f"/api/admin/notifications/{first}", headers=headers).get_json()
    second_status = client.get(f"/api/admin/notifications/{second}", headers=headers).get_json()

    assert (first_status["title"], first_status["sent"]) == ("Pirmas", 4)
    assert (second_status["title"], second_status["sent"]) == ("Antras", 9)


def test_only_the_newest_fifty_jobs_survive_in_the_registry():
    # The registry is in-process and unbounded without this cap —
    # driven straight through the helper, since 51 real broadcasts
    # would prove the same thing 51 times slower
    oldest = "job-0"
    for i in range(admin_routes._BROADCAST_JOBS_MAX + 1):
        admin_routes._set_broadcast_job(f"job-{i}", status="queued")

    assert len(admin_routes._broadcast_jobs) == admin_routes._BROADCAST_JOBS_MAX
    assert admin_routes._broadcast_job(oldest) is None
    assert admin_routes._broadcast_job("job-50") is not None


def test_touching_a_job_moves_it_out_of_the_eviction_line():
    admin_routes._set_broadcast_job("job-a", status="queued")
    for i in range(admin_routes._BROADCAST_JOBS_MAX - 1):
        admin_routes._set_broadcast_job(f"job-{i}", status="queued")
    admin_routes._set_broadcast_job("job-a", status="running")
    admin_routes._set_broadcast_job("job-extra", status="queued")

    assert admin_routes._broadcast_job("job-a")["status"] == "running"
    assert admin_routes._broadcast_job("job-0") is None


def test_a_job_record_handed_out_is_a_copy():
    admin_routes._set_broadcast_job("job-x", status="queued")

    handed_out = admin_routes._broadcast_job("job-x")
    handed_out["status"] = "tampered"

    assert admin_routes._broadcast_job("job-x")["status"] == "queued"




# ===========================================================
# require_role — the gate on every route
# ===========================================================

@pytest.mark.parametrize("method,path", ADMIN_ONLY_ROUTES + INVITATION_ROUTES)
def test_an_anonymous_caller_is_refused_everywhere(client, method, path):
    response = _call(client, method, path)

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


@pytest.mark.parametrize("method,path", ADMIN_ONLY_ROUTES + INVITATION_ROUTES)
def test_a_student_is_refused_everywhere(client, actor, method, path):
    _, headers = actor

    response = _call(client, method, path, headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"


@pytest.mark.parametrize("method,path", ADMIN_ONLY_ROUTES + INVITATION_ROUTES)
def test_a_teacher_is_refused_everywhere(client, make_user, auth_headers, method, path):
    headers = auth_headers(make_user(role="teacher"))

    response = _call(client, method, path, headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"


@pytest.mark.parametrize("method,path", ADMIN_ONLY_ROUTES)
def test_a_curator_is_refused_outside_the_invitation_routes(client, make_user, auth_headers,
                                                            method, path):
    headers = auth_headers(make_user(role="curator"))

    response = _call(client, method, path, headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"


@pytest.mark.parametrize("method,path", INVITATION_ROUTES)
def test_a_curator_passes_the_gate_on_the_invitation_routes(client, make_user, auth_headers,
                                                            method, path):
    headers = auth_headers(make_user(role="curator"))

    response = _call(client, method, path, headers)

    assert response.status_code not in (401, 403)


def test_a_deactivated_admin_is_refused_like_an_anonymous_caller(client, admin, make_user,
                                                                 auth_headers):
    _, headers = admin
    other = make_user(username="buves_adminas", role="admin")
    other_headers = auth_headers(other)

    client.patch(f"/api/admin/users/{other['id']}", headers=headers, json={"active": False})

    assert client.get("/api/admin/users", headers=other_headers).status_code == 401


def test_a_garbage_bearer_token_is_refused(client):
    response = client.get("/api/admin/users", headers={"Authorization": "Bearer nesamone"})

    assert response.status_code == 401


def test_a_student_cannot_change_anybody_s_role(client, actor, make_user, db):
    _, headers = actor
    target = make_user(username="taikinys")

    response = client.patch(f"/api/admin/users/{target['id']}", headers=headers,
                            json={"role": "admin"})

    assert response.status_code == 403
    assert db.execute("SELECT role FROM users WHERE id = ?", (target["id"],)).fetchone()[0] == "student"
    assert _audit_rows(db) == []
