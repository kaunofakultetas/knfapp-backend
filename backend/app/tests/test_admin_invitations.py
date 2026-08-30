# -----------------------------------------------------------
#  [*] Tests — /api/admin (invitations, users, stats, broadcast)
#
#  The admin console's whole surface, with the invitation
#  routes under the brightest light: a code string IS a role
#  grant, so who may mint one, who may SEE one and who may
#  take one back are three separate authorisation questions
#  and this module answers all three. What it proves:
#
#    - THE BLOCKER: GET /admin/invitations is SCOPED. An
#      admin sees every row; a curator sees only the
#      student/teacher codes THEY created. A planted
#      admin-role code never appears in a curator's listing —
#      not its object, not even its code string anywhere in
#      the body — because the screen's copy/QR actions would
#      hand a curator a straight path to an admin account.
#    - the same scope on DELETE: a curator aiming at someone
#      else's row, or at an admin-role row, gets the SAME 404
#      as a nonexistent id and learns nothing about it.
#    - role rules on create: curators mint student/teacher,
#      admin/curator codes are 403 for them and mint nothing.
#    - the numeric bounds are REJECTED, never clamped —
#      max_uses 1..1000, expires_hours 1..8760 — with both
#      ends of both ranges pinned, bools excluded from "int",
#      and a nonsense configured default falling back / being
#      clamped instead of turning every mint into a 400.
#    - the 201 body is the exact camelCase shape the mobile
#      app prepends to its list, createdAt included, with
#      expired/fullyUsed both false; createdAt is the ISO
#      T-form, so a new code sorts to the TOP of the listing
#      rather than halfway down it (migration v17's scar).
#    - the derived flags: expired is aware-to-aware and an
#      unparsable stored value reads as expired instead of
#      500-ing the whole listing; fullyUsed trips at max_uses.
#    - the role gate on every route in the module, the
#      curator's 403 on the admin-only half, and an
#      inactive admin refused outright.
#    - the ?limit=/?offset= bounds on both listings, and that
#      neither param means "everything" — the frozen contract.
#    - every mutation writes its admin_audit row, and a
#      database without that table still performs the action.
#    - user management: the 404-before-body rule, the
#      self-deactivation and self-demotion guards, session +
#      push-token purge on deactivation, and the socket kill
#      switch that can never fail the request.
#    - the dashboard counters, their 45 s snapshot, and the
#      same-day expiry comparison that used to count an
#      already-dead code as active.
#    - the broadcast: 202 + job id, the background fan-out,
#      the forced announcement type marker and the payload
#      ceiling.
# -----------------------------------------------------------

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine
from flask import request

from app.admin import routes as admin_routes
from app.auth.routes import PRIVILEGED_ROLES, ROLES, _rate_limit_store

INVITATIONS = "/api/admin/invitations"
USERS = "/api/admin/users"
STATS = "/api/admin/stats"
NOTIFICATIONS = "/api/admin/notifications"

PASSWORD = "GiedraDiena!42"




# -----------------------------------------------------------
# clean_module_state
# -----------------------------------------------------------
#
# Four process-wide stores outlive the per-test `app` fixture:
# auth's rate-limit window (the invite quota AND the app's
# global 600-per-IP budget), the dashboard's 45 s stats
# snapshot, and the broadcast job registry. Left alone they
# carry one test's state into the next — a cached counter is
# especially vicious, since it makes a correct COUNT(*) look
# wrong for 45 seconds. Cleared around every test.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_module_state():
    _rate_limit_store.clear()
    admin_routes._stats_cache.clear()
    admin_routes._broadcast_jobs.clear()
    yield
    _rate_limit_store.clear()
    admin_routes._stats_cache.clear()
    admin_routes._broadcast_jobs.clear()




# -----------------------------------------------------------
# curator / teacher
# -----------------------------------------------------------
#
# The two extra actors this module needs beside conftest's
# `actor` (student) and `admin`. The curator is the whole
# point of the scoping tests, so it gets its own fixture
# rather than a make_user call in twenty places.
#
# Used by:
#   - the scoping, minting and revoking tests below
# -----------------------------------------------------------

@pytest.fixture
def curator(make_user, auth_headers):
    user = make_user(role="curator", display_name="Kuratore")
    return user, auth_headers(user)


@pytest.fixture
def teacher(make_user, auth_headers):
    user = make_user(role="teacher")
    return user, auth_headers(user)




# -----------------------------------------------------------
# _plant_code
# -----------------------------------------------------------
#
# Writes one invitation_codes row in whatever state a test
# needs — any role, any owner, expired, exhausted, or with a
# deliberately unparsable timestamp — and returns its id.
# Planting beats minting through the route wherever the point
# is a state the route REFUSES to create (an admin code owned
# by a curator is exactly that).
#
# Used by:
#   - the listing, revoke and stats tests below
# -----------------------------------------------------------

def _plant_code(db, code, role="student", created_by=None, max_uses=1, use_count=0,
                expires_at=None, created_at=None):
    code_id = str(uuid.uuid4())
    if expires_at is None:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()

    db.execute(
        "INSERT INTO invitation_codes (id, code, role, created_by, max_uses, use_count, expires_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (code_id, code, role, created_by, max_uses, use_count, expires_at, created_at),
    )
    db.commit()
    return code_id




# -----------------------------------------------------------
# _listing
# -----------------------------------------------------------
#
# (payload, codes) for one GET /admin/invitations — the raw
# body plus the set of code STRINGS in it, which is what the
# scoping assertions are really about.
#
# Used by:
#   - the listing tests below
# -----------------------------------------------------------

def _listing(client, headers, query=""):
    response = client.get(INVITATIONS + query, headers=headers)
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    return payload, {row["code"] for row in payload["invitations"]}




# -----------------------------------------------------------
# _audit
# -----------------------------------------------------------
#
# The admin_audit rows for one action, oldest first.
#
# Used by:
#   - the audit-trail assertions below
# -----------------------------------------------------------

def _audit(db, action):
    return db.execute(
        "SELECT * FROM admin_audit WHERE action = ? ORDER BY created_at",
        (action,),
    ).fetchall()




# -----------------------------------------------------------
# _admin_id
# -----------------------------------------------------------
#
# The seeded admin's id straight from the database — the one
# foreign key a planted comment needs, without dragging the
# whole `admin` fixture (and a login round trip) into it.
#
# Used by:
#   - the stats tests below
# -----------------------------------------------------------

def _admin_id(db):
    return db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]




# -----------------------------------------------------------
# _mint
# -----------------------------------------------------------
#
# POST /admin/invitations with a body, returning the response.
# Trivial, but it keeps forty call sites from repeating the
# path and the headers keyword.
#
# Used by:
#   - the create_invitation tests below
# -----------------------------------------------------------

def _mint(client, headers, **body):
    return client.post(INVITATIONS, json=body, headers=headers)








# -----------------------------------------------------------
# Role gates — require_role on every route in the module
# -----------------------------------------------------------

ALL_ROUTES = [
    ("POST", INVITATIONS),
    ("GET", INVITATIONS),
    ("DELETE", INVITATIONS + "/whatever"),
    ("GET", USERS),
    ("PATCH", USERS + "/whatever"),
    ("GET", STATS),
    ("POST", NOTIFICATIONS),
    ("GET", NOTIFICATIONS + "/whatever"),
]

ADMIN_ONLY_ROUTES = [
    ("GET", USERS),
    ("PATCH", USERS + "/whatever"),
    ("GET", STATS),
    ("POST", NOTIFICATIONS),
    ("GET", NOTIFICATIONS + "/whatever"),
]


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_every_admin_route_refuses_an_anonymous_caller(client, method, path):
    response = client.open(path, method=method, json={})
    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_every_admin_route_refuses_a_student(client, actor, method, path):
    _, headers = actor
    response = client.open(path, method=method, json={}, headers=headers)
    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_every_admin_route_refuses_a_teacher(client, teacher, method, path):
    _, headers = teacher
    response = client.open(path, method=method, json={}, headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("method,path", ADMIN_ONLY_ROUTES)
def test_a_curator_is_refused_on_the_admin_only_routes(client, curator, method, path):
    _, headers = curator
    response = client.open(path, method=method, json={}, headers=headers)
    assert response.status_code == 403
    assert response.get_json()["error"] == "Insufficient permissions"


def test_a_curator_reaches_the_three_invitation_routes(client, curator):
    _, headers = curator

    assert client.get(INVITATIONS, headers=headers).status_code == 200
    assert _mint(client, headers).status_code == 201
    # Not found, not forbidden — the decorator let the curator in
    assert client.delete(INVITATIONS + "/nera-tokio", headers=headers).status_code == 404


def test_a_deactivated_admin_is_refused_like_an_anonymous_caller(client, admin, db):
    user, headers = admin
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    response = client.get(INVITATIONS, headers=headers)
    assert response.status_code == 401


def test_admin_bodies_are_never_cached(client, admin):
    _, headers = admin
    response = client.get(INVITATIONS, headers=headers)
    assert "no-store" in response.headers["Cache-Control"]








# -----------------------------------------------------------
# POST /admin/invitations — the 201 body
# -----------------------------------------------------------

@pytest.mark.contract
def test_minting_answers_the_camelcase_body_the_app_prepends(client, admin):
    user, headers = admin
    response = _mint(client, headers, role="teacher", max_uses=5, expires_hours=48)

    assert response.status_code == 201
    body = response.get_json()
    assert set(body) == {
        "id", "code", "role", "maxUses", "useCount", "expiresAt",
        "createdAt", "createdBy", "expired", "fullyUsed",
    }
    assert body["role"] == "teacher"
    assert body["maxUses"] == 5
    assert body["useCount"] == 0
    assert body["createdBy"] == user["id"]


def test_a_fresh_code_is_neither_expired_nor_fully_used(client, admin):
    _, headers = admin
    body = _mint(client, headers).get_json()

    assert body["expired"] is False
    assert body["fullyUsed"] is False


def test_the_minted_code_is_twelve_uppercase_hex_characters(client, admin):
    _, headers = admin
    code = _mint(client, headers).get_json()["code"]

    assert len(code) == 12
    assert code == code.upper()
    assert all(character in "0123456789ABCDEF" for character in code)


def test_the_default_role_is_student(client, admin):
    _, headers = admin
    assert _mint(client, headers).get_json()["role"] == "student"


def test_the_default_max_uses_is_one(client, admin):
    _, headers = admin
    assert _mint(client, headers).get_json()["maxUses"] == 1


def test_the_minted_row_is_persisted_with_the_actor_as_creator(client, admin, db):
    user, headers = admin
    body = _mint(client, headers, role="teacher", max_uses=3).get_json()

    row = db.execute("SELECT * FROM invitation_codes WHERE id = ?", (body["id"],)).fetchone()
    assert row["code"] == body["code"]
    assert row["role"] == "teacher"
    assert row["max_uses"] == 3
    assert row["use_count"] == 0
    assert row["created_by"] == user["id"]
    assert row["expires_at"] == body["expiresAt"]
    assert row["created_at"] == body["createdAt"]


def test_created_at_is_the_iso_t_form_so_a_new_code_sorts_to_the_top(client, admin, db):
    _, headers = admin
    # A row stamped by the column DEFAULT (migration v17's
    # space-separated form) sorts BEFORE every T-form row of the
    # SAME day, which used to file a brand-new code halfway down
    # the ORDER BY created_at DESC listing
    _plant_code(db, "SENAS-TARPAS",
                created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    body = _mint(client, headers).get_json()
    assert "T" in body["createdAt"]
    assert body["createdAt"].endswith("+00:00")
    # Parses as an aware datetime, not just as a string that looks like one
    assert datetime.fromisoformat(body["createdAt"]).tzinfo is not None

    payload, codes = _listing(client, headers)
    assert body["code"] in codes
    assert payload["invitations"][0]["code"] == body["code"], \
        "a freshly minted code must be first in the listing"


def test_the_expiry_is_an_aware_utc_timestamp_hours_from_now(client, admin):
    _, headers = admin
    body = _mint(client, headers, expires_hours=6).get_json()

    expires = datetime.fromisoformat(body["expiresAt"])
    assert expires.tzinfo is not None
    delta = expires - datetime.now(timezone.utc)
    assert timedelta(hours=5, minutes=59) < delta <= timedelta(hours=6)








# -----------------------------------------------------------
# POST /admin/invitations — the expiry default and its config
# -----------------------------------------------------------

def test_the_default_expiry_comes_from_the_configured_hours(client, app, admin):
    _, headers = admin
    app.config["INVITATION_EXPIRY_HOURS"] = 72

    frozen = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=1)
    with time_machine.travel(frozen, tick=False):
        body = _mint(client, headers).get_json()

    assert datetime.fromisoformat(body["expiresAt"]) == frozen + timedelta(hours=72)
    assert datetime.fromisoformat(body["createdAt"]) == frozen


def test_an_explicit_expires_hours_beats_the_configured_default(client, app, admin):
    _, headers = admin
    app.config["INVITATION_EXPIRY_HOURS"] = 72

    frozen = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=1)
    with time_machine.travel(frozen, tick=False):
        body = _mint(client, headers, expires_hours=1).get_json()

    assert datetime.fromisoformat(body["expiresAt"]) == frozen + timedelta(hours=1)


@pytest.mark.parametrize("configured", ["ne-skaicius", None, 3.5, True, False])
def test_a_non_integer_configured_expiry_falls_back_to_a_week(client, app, admin, configured):
    _, headers = admin
    app.config["INVITATION_EXPIRY_HOURS"] = configured

    frozen = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=1)
    with time_machine.travel(frozen, tick=False):
        body = _mint(client, headers).get_json()

    assert datetime.fromisoformat(body["expiresAt"]) == frozen + timedelta(hours=168)


def test_an_absurd_configured_expiry_is_clamped_to_a_year_not_rejected(client, app, admin):
    _, headers = admin
    app.config["INVITATION_EXPIRY_HOURS"] = 10 ** 9

    frozen = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=1)
    with time_machine.travel(frozen, tick=False):
        response = _mint(client, headers)

    assert response.status_code == 201
    assert datetime.fromisoformat(response.get_json()["expiresAt"]) == frozen + timedelta(hours=8760)


def test_a_zero_configured_expiry_is_clamped_up_to_an_hour(client, app, admin):
    _, headers = admin
    app.config["INVITATION_EXPIRY_HOURS"] = 0

    frozen = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=1)
    with time_machine.travel(frozen, tick=False):
        response = _mint(client, headers)

    assert response.status_code == 201
    assert datetime.fromisoformat(response.get_json()["expiresAt"]) == frozen + timedelta(hours=1)








# -----------------------------------------------------------
# POST /admin/invitations — the numeric bounds
# -----------------------------------------------------------

@pytest.mark.parametrize("max_uses", [1, 2, 999, 1000])
def test_max_uses_accepts_both_ends_of_its_range(client, admin, max_uses):
    _, headers = admin
    response = _mint(client, headers, max_uses=max_uses)

    assert response.status_code == 201
    assert response.get_json()["maxUses"] == max_uses


@pytest.mark.parametrize("max_uses", [0, -1, -1000, 1001, 10000, 10 ** 12])
def test_max_uses_outside_its_range_is_rejected_never_clamped(client, admin, max_uses, db):
    _, headers = admin
    response = _mint(client, headers, max_uses=max_uses)

    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be between 1 and 1000"
    # The 10000 that used to come back as a quietly rewritten 201
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE max_uses = ?",
                      (max_uses,)).fetchone()["c"] == 0


@pytest.mark.parametrize("max_uses", ["5", 1.0, 2.5, None, [1], {"n": 1}])
def test_a_non_integer_max_uses_is_rejected(client, admin, max_uses):
    _, headers = admin
    response = _mint(client, headers, max_uses=max_uses)

    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be an integer"


@pytest.mark.parametrize("max_uses", [True, False])
def test_a_boolean_max_uses_is_not_an_integer(client, admin, max_uses):
    _, headers = admin
    response = _mint(client, headers, max_uses=max_uses)

    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be an integer"


@pytest.mark.parametrize("hours", [1, 2, 8759, 8760])
def test_expires_hours_accepts_both_ends_of_its_range(client, admin, hours):
    _, headers = admin
    assert _mint(client, headers, expires_hours=hours).status_code == 201


@pytest.mark.parametrize("hours", [0, -1, -168, 8761, 10 ** 17])
def test_expires_hours_outside_its_range_is_rejected(client, admin, hours):
    _, headers = admin
    response = _mint(client, headers, expires_hours=hours)

    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be between 1 and 8760"


def test_a_zero_expiry_cannot_mint_an_already_expired_code(client, admin, db):
    _, headers = admin
    assert _mint(client, headers, expires_hours=0).status_code == 400
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"] == 1


@pytest.mark.parametrize("hours", ["24", 1.5, None, [], {}])
def test_a_non_integer_expires_hours_is_rejected(client, admin, hours):
    _, headers = admin
    response = _mint(client, headers, expires_hours=hours)

    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be an integer"


@pytest.mark.parametrize("hours", [True, False])
def test_a_boolean_expires_hours_is_not_an_integer(client, admin, hours):
    _, headers = admin
    response = _mint(client, headers, expires_hours=hours)

    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be an integer"


def test_max_uses_is_checked_before_the_role(client, admin):
    _, headers = admin
    response = _mint(client, headers, role="nesamone", max_uses=0)

    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be between 1 and 1000"








# -----------------------------------------------------------
# POST /admin/invitations — the role rules
# -----------------------------------------------------------

@pytest.mark.parametrize("role", ROLES)
def test_an_admin_may_mint_a_code_of_every_role(client, admin, role):
    _, headers = admin
    response = _mint(client, headers, role=role)

    assert response.status_code == 201
    assert response.get_json()["role"] == role


@pytest.mark.parametrize("role", ["student", "teacher"])
def test_a_curator_may_mint_the_unprivileged_roles(client, curator, role):
    _, headers = curator
    response = _mint(client, headers, role=role)

    assert response.status_code == 201
    assert response.get_json()["role"] == role


@pytest.mark.parametrize("role", PRIVILEGED_ROLES)
def test_a_curator_cannot_mint_a_privileged_code(client, curator, role, db):
    _, headers = curator
    response = _mint(client, headers, role=role)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only admins can create admin/curator invitations"
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE role = ?",
                      (role,)).fetchone()["c"] == 0


@pytest.mark.parametrize("role", ["superadmin", "ADMIN", "", "dekanas", "students"])
def test_an_unknown_role_is_rejected(client, admin, role):
    _, headers = admin
    response = _mint(client, headers, role=role)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"


@pytest.mark.parametrize("role", [1, None, ["admin"], {"role": "admin"}])
def test_a_non_string_role_is_rejected(client, admin, role):
    _, headers = admin
    response = _mint(client, headers, role=role)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"


def test_a_curator_cannot_smuggle_a_privileged_role_through_the_default(client, curator, db):
    _, headers = curator
    # No role key at all — the default must be student, never
    # something inherited from the caller
    body = _mint(client, headers).get_json()
    assert body["role"] == "student"








# -----------------------------------------------------------
# POST /admin/invitations — body, audit and rate limit
# -----------------------------------------------------------

def test_a_malformed_json_body_is_rejected(client, admin):
    _, headers = admin
    response = client.post(INVITATIONS, data="{ne json", content_type="application/json",
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_body_less_post_is_rejected(client, admin):
    _, headers = admin
    response = client.post(INVITATIONS, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_an_array_body_never_reaches_the_handler(client, admin):
    _, headers = admin
    response = client.post(INVITATIONS, json=[{"role": "admin"}], headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_an_empty_object_body_mints_the_defaults(client, admin):
    _, headers = admin
    response = client.post(INVITATIONS, json={}, headers=headers)

    assert response.status_code == 201
    assert response.get_json()["role"] == "student"


def test_minting_writes_one_audit_row(client, admin, db):
    user, headers = admin
    body = _mint(client, headers, role="teacher", max_uses=4).get_json()

    rows = _audit(db, "invitation.create")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == user["id"]
    assert rows[0]["target"] == body["id"]
    assert json.loads(rows[0]["payload"]) == {
        "role": "teacher", "maxUses": 4, "expiresAt": body["expiresAt"],
    }


def test_a_rejected_mint_writes_no_audit_row(client, admin, db):
    _, headers = admin
    assert _mint(client, headers, max_uses=0).status_code == 400
    assert _audit(db, "invitation.create") == []


def test_minting_still_works_on_a_database_without_the_audit_table(client, admin, db):
    # A pre-v40 file: the audit INSERT fails, is swallowed, and
    # the code itself must still commit
    _, headers = admin
    db.execute("DROP TABLE admin_audit")
    db.commit()

    response = _mint(client, headers)
    assert response.status_code == 201
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE id = ?",
                      (response.get_json()["id"],)).fetchone()["c"] == 1


def test_minting_is_rate_limited_per_actor(client, admin, curator):
    _, admin_headers = admin
    _, curator_headers = curator

    for _ in range(30):
        assert _mint(client, admin_headers).status_code == 201

    refused = _mint(client, admin_headers)
    assert refused.status_code == 429
    assert refused.get_json()["code"] == "rate_limited"
    assert int(refused.headers["Retry-After"]) >= 1

    # The budget is keyed per actor, not per route
    assert _mint(client, curator_headers).status_code == 201


def test_the_rate_limit_budget_reopens_after_the_window(client, admin):
    _, headers = admin
    for _ in range(30):
        assert _mint(client, headers).status_code == 201
    assert _mint(client, headers).status_code == 429

    # Age every recorded attempt past the 5-minute window without
    # spending five real minutes on it
    for key in list(_rate_limit_store):
        _rate_limit_store[key] = [stamp - 301 for stamp in _rate_limit_store[key]]

    assert _mint(client, headers).status_code == 201








# -----------------------------------------------------------
# GET /admin/invitations — THE BLOCKER: curator scoping
# -----------------------------------------------------------

def test_an_admin_sees_every_code_whoever_minted_it(client, admin, curator, db):
    admin_user, admin_headers = admin
    curator_user, _ = curator

    _plant_code(db, "ADMINO-KODAS", role="admin", created_by=admin_user["id"])
    _plant_code(db, "KURATORES-KODAS", role="student", created_by=curator_user["id"])
    _plant_code(db, "NIEKIENO-KODAS", role="teacher", created_by=None)

    _, codes = _listing(client, admin_headers)
    assert {"ADMINO-KODAS", "KURATORES-KODAS", "NIEKIENO-KODAS"} <= codes


def test_a_curator_never_sees_an_admin_role_code(client, curator, admin, db):
    admin_user, _ = admin
    curator_user, curator_headers = curator

    # Both flavours of the escalation: one an admin minted, and one
    # planted onto the curator's own id (a hand-edited row, or a
    # grant an admin later made) — neither may reach their screen
    _plant_code(db, "ESKALACIJA-SVETIMA", role="admin", created_by=admin_user["id"])
    _plant_code(db, "ESKALACIJA-SAVA", role="admin", created_by=curator_user["id"])
    _plant_code(db, "LEISTINAS", role="student", created_by=curator_user["id"])

    payload, codes = _listing(client, curator_headers)

    assert "ESKALACIJA-SVETIMA" not in codes
    assert "ESKALACIJA-SAVA" not in codes
    assert "LEISTINAS" in codes
    assert {row["role"] for row in payload["invitations"]} <= {"student", "teacher"}
    # Not merely absent as an object — the string itself must not
    # appear anywhere in the body a curator's client receives
    body = client.get(INVITATIONS, headers=curator_headers).get_data(as_text=True)
    assert "ESKALACIJA-SVETIMA" not in body
    assert "ESKALACIJA-SAVA" not in body


def test_a_curator_never_sees_a_curator_role_code(client, curator, db):
    curator_user, curator_headers = curator
    _plant_code(db, "KURATORIAUS-GRANTAS", role="curator", created_by=curator_user["id"])

    _, codes = _listing(client, curator_headers)
    assert "KURATORIAUS-GRANTAS" not in codes


def test_a_curator_sees_only_the_codes_they_created(client, curator, make_user, admin, db):
    admin_user, _ = admin
    curator_user, curator_headers = curator
    other_curator = make_user(role="curator")

    _plant_code(db, "SAVAS", role="student", created_by=curator_user["id"])
    _plant_code(db, "KITOS-KURATORES", role="student", created_by=other_curator["id"])
    _plant_code(db, "ADMINO-STUDENTO", role="student", created_by=admin_user["id"])
    _plant_code(db, "BE-SAVININKO", role="student", created_by=None)

    _, codes = _listing(client, curator_headers)
    assert codes == {"SAVAS"}


def test_a_curator_sees_their_own_student_and_teacher_codes(client, curator, db):
    curator_user, headers = curator
    _plant_code(db, "STUDENTO", role="student", created_by=curator_user["id"])
    _plant_code(db, "DESTYTOJO", role="teacher", created_by=curator_user["id"])

    _, codes = _listing(client, headers)
    assert codes == {"STUDENTO", "DESTYTOJO"}


def test_a_curator_sees_the_code_they_just_minted(client, curator):
    _, headers = curator
    minted = _mint(client, headers, role="teacher").get_json()

    _, codes = _listing(client, headers)
    assert minted["code"] in codes


def test_the_bootstrap_code_is_invisible_to_a_curator(client, curator, seeded_code):
    _, headers = curator
    _, codes = _listing(client, headers)
    assert seeded_code not in codes


def test_the_bootstrap_code_is_visible_to_an_admin(client, admin, seeded_code):
    _, headers = admin
    _, codes = _listing(client, headers)
    assert seeded_code in codes








# -----------------------------------------------------------
# GET /admin/invitations — shape, order and derived flags
# -----------------------------------------------------------

@pytest.mark.contract
def test_the_listing_is_the_shape_the_app_consumes(client, admin, db):
    user, headers = admin
    _plant_code(db, "FORMA", role="teacher", created_by=user["id"], max_uses=3, use_count=1)

    payload, _ = _listing(client, headers)
    row = next(r for r in payload["invitations"] if r["code"] == "FORMA")

    assert set(payload) == {"invitations"}
    assert set(row) == {
        "id", "code", "role", "maxUses", "useCount", "expiresAt",
        "createdAt", "createdBy", "expired", "fullyUsed",
    }
    assert row["maxUses"] == 3
    assert row["useCount"] == 1
    assert row["createdBy"] == user["id"]
    assert row["expired"] is False
    assert row["fullyUsed"] is False


def test_the_listing_is_newest_first(client, admin, db):
    _, headers = admin
    _plant_code(db, "SENIAUSIAS", created_at="2020-01-01T00:00:00+00:00")
    _plant_code(db, "VIDURINYS", created_at="2023-06-01T00:00:00+00:00")
    _plant_code(db, "NAUJAUSIAS", created_at="2030-01-01T00:00:00+00:00")

    planted = {"SENIAUSIAS", "VIDURINYS", "NAUJAUSIAS"}
    payload, _ = _listing(client, headers)
    order = [r["code"] for r in payload["invitations"] if r["code"] in planted]
    assert order == ["NAUJAUSIAS", "VIDURINYS", "SENIAUSIAS"]


@pytest.mark.parametrize("offset_days,expected", [(-1, True), (1, False)])
def test_the_expired_flag_follows_the_stored_timestamp(client, admin, db, offset_days, expected):
    _, headers = admin
    stamp = (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat()
    _plant_code(db, "LAIKAS", expires_at=stamp)

    payload, _ = _listing(client, headers)
    row = next(r for r in payload["invitations"] if r["code"] == "LAIKAS")
    assert row["expired"] is expected


def test_a_naive_stored_timestamp_is_read_as_utc(client, admin, db):
    _, headers = admin
    naive_past = (datetime.now(timezone.utc) - timedelta(days=2)).replace(tzinfo=None).isoformat()
    naive_future = (datetime.now(timezone.utc) + timedelta(days=2)).replace(tzinfo=None).isoformat()
    _plant_code(db, "NAIVUS-SENAS", expires_at=naive_past)
    _plant_code(db, "NAIVUS-NAUJAS", expires_at=naive_future)

    payload, _ = _listing(client, headers)
    flags = {r["code"]: r["expired"] for r in payload["invitations"]}
    assert flags["NAIVUS-SENAS"] is True
    assert flags["NAIVUS-NAUJAS"] is False


@pytest.mark.parametrize("stored", ["", "labas", "2020-13-45", "netikra data", "0000"])
def test_an_unparsable_timestamp_reads_as_expired_without_500ing_the_listing(client, admin, db, stored):
    _, headers = admin
    _plant_code(db, "SUGADINTAS", expires_at=stored)
    _plant_code(db, "SVEIKAS")

    payload, codes = _listing(client, headers)
    flags = {r["code"]: r["expired"] for r in payload["invitations"]}
    assert flags["SUGADINTAS"] is True
    # One bad row used to raise inside the comprehension and take
    # every other code down with it
    assert flags["SVEIKAS"] is False
    assert "SVEIKAS" in codes


def test_invitation_expired_reads_a_missing_timestamp_as_expired():
    # The column is NOT NULL, so this branch is only reachable
    # directly — a None still has to answer "expired", never raise
    assert admin_routes._invitation_expired(None) is True


@pytest.mark.parametrize("use_count,max_uses,expected", [(0, 1, False), (1, 1, True), (2, 3, False),
                                                        (3, 3, True), (5, 3, True)])
def test_the_fully_used_flag_trips_at_max_uses(client, admin, db, use_count, max_uses, expected):
    _, headers = admin
    _plant_code(db, "NAUDOJIMAI", max_uses=max_uses, use_count=use_count)

    payload, _ = _listing(client, headers)
    row = next(r for r in payload["invitations"] if r["code"] == "NAUDOJIMAI")
    assert row["fullyUsed"] is expected








# -----------------------------------------------------------
# GET /admin/invitations — ?limit= / ?offset=
# -----------------------------------------------------------

def test_the_listing_returns_everything_when_no_page_params_are_given(client, admin, db):
    _, headers = admin
    for index in range(5):
        _plant_code(db, f"PUSLAPIS-{index}")

    payload, _ = _listing(client, headers)
    # Five planted plus the bootstrap code
    assert len(payload["invitations"]) == 6


def test_limit_pages_the_listing(client, admin, db):
    _, headers = admin
    for index in range(5):
        _plant_code(db, f"PUSLAPIS-{index}")

    payload, _ = _listing(client, headers, "?limit=2")
    assert len(payload["invitations"]) == 2


def test_offset_without_a_limit_skips_from_the_top(client, admin, db):
    _, headers = admin
    _plant_code(db, "PIRMAS", created_at="2030-01-01T00:00:00+00:00")
    _plant_code(db, "ANTRAS", created_at="2029-01-01T00:00:00+00:00")

    everything, _ = _listing(client, headers)
    skipped, _ = _listing(client, headers, "?offset=1")

    assert everything["invitations"][0]["code"] == "PIRMAS"
    assert skipped["invitations"][0]["code"] == "ANTRAS"
    assert len(skipped["invitations"]) == len(everything["invitations"]) - 1


def test_limit_and_offset_together_select_one_row(client, admin, db):
    _, headers = admin
    _plant_code(db, "PIRMAS", created_at="2030-01-01T00:00:00+00:00")
    _plant_code(db, "ANTRAS", created_at="2029-01-01T00:00:00+00:00")
    _plant_code(db, "TRECIAS", created_at="2028-01-01T00:00:00+00:00")

    payload, codes = _listing(client, headers, "?limit=1&offset=1")
    assert codes == {"ANTRAS"}


@pytest.mark.parametrize("limit", [1, 500])
def test_limit_accepts_both_ends_of_its_range(client, admin, limit):
    _, headers = admin
    assert client.get(f"{INVITATIONS}?limit={limit}", headers=headers).status_code == 200


@pytest.mark.parametrize("limit", [0, -1, 501, 100000])
def test_a_limit_outside_its_range_is_rejected(client, admin, limit):
    _, headers = admin
    response = client.get(f"{INVITATIONS}?limit={limit}", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be between 1 and 500"


@pytest.mark.parametrize("limit", ["abc", "", "1.5", "10;DROP"])
def test_a_non_integer_limit_is_rejected(client, admin, limit):
    _, headers = admin
    response = client.get(f"{INVITATIONS}?limit={limit}", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be an integer"


def test_a_negative_offset_is_rejected(client, admin):
    _, headers = admin
    response = client.get(f"{INVITATIONS}?offset=-1", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "offset must be zero or greater"


def test_a_non_integer_offset_is_rejected(client, admin):
    _, headers = admin
    response = client.get(f"{INVITATIONS}?offset=rytoj", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "offset must be an integer"


def test_a_zero_offset_is_accepted(client, admin):
    _, headers = admin
    assert client.get(f"{INVITATIONS}?offset=0", headers=headers).status_code == 200


def test_the_curator_listing_pages_too(client, curator, db):
    curator_user, headers = curator
    for index in range(4):
        _plant_code(db, f"KURATORES-{index}", created_by=curator_user["id"])

    payload, _ = _listing(client, headers, "?limit=2")
    assert len(payload["invitations"]) == 2


def test_a_bad_page_param_is_rejected_before_the_curator_query_runs(client, curator):
    _, headers = curator
    response = client.get(f"{INVITATIONS}?limit=0", headers=headers)
    assert response.status_code == 400








# -----------------------------------------------------------
# DELETE /admin/invitations/<code_id>
# -----------------------------------------------------------

def test_an_admin_revokes_any_code(client, admin, curator, db):
    _, admin_headers = admin
    curator_user, _ = curator
    code_id = _plant_code(db, "ATSAUKIAMAS", created_by=curator_user["id"])

    response = client.delete(f"{INVITATIONS}/{code_id}", headers=admin_headers)
    assert response.status_code == 200
    assert response.get_json()["message"] == "Invitation deleted"
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE id = ?",
                      (code_id,)).fetchone()["c"] == 0


def test_a_revoked_code_is_gone_from_the_listing(client, admin, db):
    _, headers = admin
    code_id = _plant_code(db, "DINGES")

    assert client.delete(f"{INVITATIONS}/{code_id}", headers=headers).status_code == 200
    _, codes = _listing(client, headers)
    assert "DINGES" not in codes


def test_revoking_an_unknown_id_is_404(client, admin):
    _, headers = admin
    response = client.delete(f"{INVITATIONS}/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Invitation not found"


def test_a_curator_revokes_their_own_unprivileged_code(client, curator, db):
    curator_user, headers = curator
    student_id = _plant_code(db, "SAVAS-STUDENTO", role="student", created_by=curator_user["id"])
    teacher_id = _plant_code(db, "SAVAS-DESTYTOJO", role="teacher", created_by=curator_user["id"])

    assert client.delete(f"{INVITATIONS}/{student_id}", headers=headers).status_code == 200
    assert client.delete(f"{INVITATIONS}/{teacher_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE created_by = ?",
                      (curator_user["id"],)).fetchone()["c"] == 0


def test_a_curator_cannot_revoke_a_privileged_code_they_own(client, curator, db):
    curator_user, headers = curator
    code_id = _plant_code(db, "SAVAS-ADMINO", role="admin", created_by=curator_user["id"])

    response = client.delete(f"{INVITATIONS}/{code_id}", headers=headers)
    # The same 404 a nonexistent id gets — nothing is disclosed
    assert response.status_code == 404
    assert response.get_json()["error"] == "Invitation not found"
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE id = ?",
                      (code_id,)).fetchone()["c"] == 1


def test_a_curator_cannot_revoke_someone_elses_code(client, curator, make_user, admin, db):
    admin_user, _ = admin
    _, headers = curator
    other_curator = make_user(role="curator")

    admin_owned = _plant_code(db, "ADMINO-STUDENTO", role="student", created_by=admin_user["id"])
    other_owned = _plant_code(db, "KITOS-STUDENTO", role="student", created_by=other_curator["id"])
    orphan = _plant_code(db, "BE-SAVININKO", role="student", created_by=None)

    for code_id in (admin_owned, other_owned, orphan):
        assert client.delete(f"{INVITATIONS}/{code_id}", headers=headers).status_code == 404

    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"] == 4


def test_a_curator_can_revoke_the_code_they_just_minted(client, curator):
    _, headers = curator
    minted = _mint(client, headers).get_json()

    assert client.delete(f"{INVITATIONS}/{minted['id']}", headers=headers).status_code == 200


def test_revoking_writes_one_audit_row(client, admin, db):
    user, headers = admin
    code_id = _plant_code(db, "AUDITUOJAMAS")

    assert client.delete(f"{INVITATIONS}/{code_id}", headers=headers).status_code == 200
    rows = _audit(db, "invitation.revoke")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == user["id"]
    assert rows[0]["target"] == code_id
    assert rows[0]["payload"] is None


def test_a_refused_revoke_writes_no_audit_row(client, curator, admin, db):
    admin_user, _ = admin
    _, headers = curator
    code_id = _plant_code(db, "SVETIMAS", created_by=admin_user["id"])

    assert client.delete(f"{INVITATIONS}/{code_id}", headers=headers).status_code == 404
    assert _audit(db, "invitation.revoke") == []


def test_revoking_still_works_on_a_database_without_the_audit_table(client, admin, db):
    _, headers = admin
    code_id = _plant_code(db, "BE-AUDITO")
    db.execute("DROP TABLE admin_audit")
    db.commit()

    assert client.delete(f"{INVITATIONS}/{code_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE id = ?",
                      (code_id,)).fetchone()["c"] == 0


def test_revoking_leaves_the_account_that_already_used_the_code(client, admin, db, seeded_code):
    _, headers = admin
    registration = client.post("/api/auth/register", json={
        "username": "panaudojo",
        "password": PASSWORD,
        "display_name": "Panaudojo Kodą",
        "email": "panaudojo@knf.vu.lt",
        "invitation_code": seeded_code,
    })
    assert registration.status_code == 201, registration.get_json()

    code_id = db.execute("SELECT id FROM invitation_codes WHERE code = ?",
                         (seeded_code,)).fetchone()["id"]
    assert client.delete(f"{INVITATIONS}/{code_id}", headers=headers).status_code == 200

    login = client.post("/api/auth/login", json={"username": "panaudojo", "password": PASSWORD})
    assert login.status_code == 200








# -----------------------------------------------------------
# GET /admin/users
# -----------------------------------------------------------

@pytest.mark.contract
def test_the_user_list_is_the_shape_the_app_consumes(client, admin, make_user):
    _, headers = admin
    make_user(username="rasa", role="teacher")

    response = client.get(USERS, headers=headers)
    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {"users"}

    row = next(r for r in payload["users"] if r["username"] == "rasa")
    assert set(row) == {"id", "username", "email", "displayName", "role", "active", "createdAt"}
    assert row["role"] == "teacher"
    assert row["active"] is True


def test_the_user_list_never_carries_a_password_hash(client, admin, make_user):
    _, headers = admin
    make_user(username="slaptas")

    body = client.get(USERS, headers=headers).get_data(as_text=True)
    assert "password" not in body.lower()
    assert "$2b$" not in body


def test_an_inactive_user_comes_back_as_a_real_boolean_false(client, admin, make_user):
    _, headers = admin
    make_user(username="uzblokuotas", active=0)

    payload = client.get(USERS, headers=headers).get_json()
    row = next(r for r in payload["users"] if r["username"] == "uzblokuotas")
    assert row["active"] is False


def test_the_user_list_pages(client, admin, make_user):
    _, headers = admin
    for index in range(4):
        make_user(username=f"eile{index}")

    payload = client.get(f"{USERS}?limit=2", headers=headers).get_json()
    assert len(payload["users"]) == 2


def test_the_user_list_rejects_a_bad_page_param(client, admin):
    _, headers = admin
    response = client.get(f"{USERS}?limit=501", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit must be between 1 and 500"


def test_the_user_list_rejects_a_bad_offset(client, admin):
    _, headers = admin
    response = client.get(f"{USERS}?offset=-5", headers=headers)
    assert response.status_code == 400








# -----------------------------------------------------------
# PATCH /admin/users/<user_id>
# -----------------------------------------------------------

def test_an_unknown_user_is_404_before_the_body_is_looked_at(client, admin):
    _, headers = admin
    # A bad id AND a bad body — the id must win, or the caller
    # believes the account exists
    response = client.patch(f"{USERS}/{uuid.uuid4()}", json={"role": "nesamone"}, headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_a_malformed_patch_body_is_rejected(client, admin, make_user):
    _, headers = admin
    target = make_user()
    response = client.patch(f"{USERS}/{target['id']}", data="{ne json",
                            content_type="application/json", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_an_empty_patch_is_rejected(client, admin, make_user):
    _, headers = admin
    target = make_user()
    response = client.patch(f"{USERS}/{target['id']}", json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Nothing to update"


def test_an_unknown_role_is_refused_on_a_patch(client, admin, make_user):
    _, headers = admin
    target = make_user()
    response = client.patch(f"{USERS}/{target['id']}", json={"role": "dekanas"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"


@pytest.mark.parametrize("active", [0, 1, "false", "true", [], {"active": False}])
def test_a_non_boolean_active_is_refused(client, admin, make_user, active):
    _, headers = admin
    target = make_user()
    response = client.patch(f"{USERS}/{target['id']}", json={"active": active}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "active must be a boolean"


def test_a_zero_active_cannot_deactivate_the_calling_admin(client, admin, db):
    user, headers = admin
    # 0 used to slip past the `is False` guard and log the console out
    response = client.patch(f"{USERS}/{user['id']}", json={"active": 0}, headers=headers)

    assert response.status_code == 400
    assert db.execute("SELECT active FROM users WHERE id = ?", (user["id"],)).fetchone()["active"] == 1


def test_an_admin_cannot_deactivate_their_own_account(client, admin, db):
    user, headers = admin
    response = client.patch(f"{USERS}/{user['id']}", json={"active": False}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot deactivate your own account"
    assert db.execute("SELECT active FROM users WHERE id = ?", (user["id"],)).fetchone()["active"] == 1


def test_an_admin_cannot_remove_their_own_admin_role(client, admin, db):
    user, headers = admin
    response = client.patch(f"{USERS}/{user['id']}", json={"role": "student"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot remove your own admin role"
    assert db.execute("SELECT role FROM users WHERE id = ?", (user["id"],)).fetchone()["role"] == "admin"


def test_an_admin_may_patch_their_own_row_without_changing_their_role(client, admin):
    user, headers = admin
    response = client.patch(f"{USERS}/{user['id']}", json={"role": "admin", "active": True},
                            headers=headers)
    assert response.status_code == 200


@pytest.mark.contract
def test_a_role_change_answers_the_fresh_row(client, admin, make_user):
    _, headers = admin
    target = make_user(username="paaukstintas")

    response = client.patch(f"{USERS}/{target['id']}", json={"role": "teacher"}, headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"id", "username", "email", "displayName", "role", "active", "createdAt"}
    assert body["role"] == "teacher"
    assert body["active"] is True


def test_a_role_change_is_persisted_and_audited(client, admin, make_user, db):
    user, headers = admin
    target = make_user(username="kuratore2")

    assert client.patch(f"{USERS}/{target['id']}", json={"role": "curator"},
                        headers=headers).status_code == 200
    assert db.execute("SELECT role FROM users WHERE id = ?",
                      (target["id"],)).fetchone()["role"] == "curator"

    rows = _audit(db, "user.role")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == user["id"]
    assert rows[0]["target"] == target["id"]
    assert json.loads(rows[0]["payload"]) == {"from": "student", "to": "curator"}


def test_deactivating_a_user_drops_their_sessions_and_push_tokens(client, admin, make_user,
                                                                  auth_headers, db):
    _, headers = admin
    target = make_user(username="atjungiamas")
    auth_headers(target)
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform) VALUES (?, ?, ?, 'ios')",
        (str(uuid.uuid4()), target["id"], "ExponentPushToken[abcdefghijklmnopqrstuv]"),
    )
    db.commit()

    assert db.execute("SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?",
                      (target["id"],)).fetchone()["c"] == 1

    response = client.patch(f"{USERS}/{target['id']}", json={"active": False}, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["active"] is False
    assert db.execute("SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?",
                      (target["id"],)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM push_tokens WHERE user_id = ?",
                      (target["id"],)).fetchone()["c"] == 0


def test_deactivating_a_user_locks_their_live_token_out(client, admin, make_user, auth_headers):
    _, headers = admin
    target = make_user(username="istremtas")
    target_headers = auth_headers(target)
    assert client.get("/api/auth/me", headers=target_headers).status_code == 200

    assert client.patch(f"{USERS}/{target['id']}", json={"active": False},
                        headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=target_headers).status_code == 401


def test_reactivating_a_user_lets_them_log_in_again(client, admin, make_user):
    _, headers = admin
    target = make_user(username="grazintas", active=0)

    refused = client.post("/api/auth/login", json={"username": "grazintas",
                                                   "password": target["password"]})
    assert refused.status_code == 403

    assert client.patch(f"{USERS}/{target['id']}", json={"active": True},
                        headers=headers).status_code == 200
    allowed = client.post("/api/auth/login", json={"username": "grazintas",
                                                   "password": target["password"]})
    assert allowed.status_code == 200


def test_deactivation_is_audited(client, admin, make_user, db):
    _, headers = admin
    target = make_user()

    client.patch(f"{USERS}/{target['id']}", json={"active": False}, headers=headers)
    rows = _audit(db, "user.active")
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"]) == {"active": False}


def test_a_role_and_active_change_in_one_patch_audits_both(client, admin, make_user, db):
    _, headers = admin
    target = make_user()

    response = client.patch(f"{USERS}/{target['id']}", json={"role": "teacher", "active": False},
                            headers=headers)
    assert response.status_code == 200
    assert response.get_json()["role"] == "teacher"
    assert response.get_json()["active"] is False
    assert len(_audit(db, "user.role")) == 1
    assert len(_audit(db, "user.active")) == 1


def test_another_admin_may_be_demoted_while_one_admin_remains(client, admin, make_user, db):
    _, headers = admin
    other_admin = make_user(username="antras_adminas", role="admin")

    response = client.patch(f"{USERS}/{other_admin['id']}", json={"role": "student"},
                            headers=headers)
    assert response.status_code == 200
    assert db.execute("SELECT role FROM users WHERE id = ?",
                      (other_admin["id"],)).fetchone()["role"] == "student"


def test_another_admin_may_be_deactivated_while_one_admin_remains(client, admin, make_user, db):
    _, headers = admin
    other_admin = make_user(username="trecias_adminas", role="admin")

    response = client.patch(f"{USERS}/{other_admin['id']}", json={"active": False},
                            headers=headers)
    assert response.status_code == 200
    assert db.execute("SELECT active FROM users WHERE id = ?",
                      (other_admin["id"],)).fetchone()["active"] == 0


def test_the_last_active_admin_backstop_refuses_the_change(app, db, make_user):
    # The one guard in this module the ROUTE cannot reach:
    # require_role guarantees an active admin actor, so a count of
    # zero other active admins implies the actor IS the target —
    # and both self-changes are already refused above it. The
    # handler is therefore driven directly, standing in for "the
    # path a future caller opens" the banner keeps it for; the
    # point is that the count excludes the target and ignores
    # deactivated admins.
    # __wrapped__ is require_role's undecorated handler — the
    # decorator is exactly what makes the guard unreachable
    update_user = admin_routes.update_user.__wrapped__

    lonely_admin = make_user(username="paskutinis_adminas", role="admin")
    sleeping_admin = make_user(username="uzmiges_adminas", role="admin", active=0)
    db.execute("UPDATE users SET active = 0 WHERE username = 'admin'")
    db.commit()

    for patch in ({"role": "student"}, {"active": False}):
        with app.test_request_context(f"/api/admin/users/{lonely_admin['id']}",
                                      method="PATCH", json=patch):
            request.user = {"id": "nebeegzistuojantis-adminas", "role": "admin"}
            response, status = update_user(lonely_admin["id"])

        assert status == 400
        assert response.get_json()["error"] == "Cannot remove the last active admin"

    row = db.execute("SELECT role, active FROM users WHERE id = ?",
                     (lonely_admin["id"],)).fetchone()
    assert row["role"] == "admin"
    assert row["active"] == 1
    assert sleeping_admin["id"] != lonely_admin["id"]


def test_an_already_inactive_admin_may_still_be_demoted(client, admin, make_user):
    _, headers = admin
    sleeping_admin = make_user(username="miegantis_adminas", role="admin", active=0)

    response = client.patch(f"{USERS}/{sleeping_admin['id']}", json={"role": "teacher"},
                            headers=headers)
    assert response.status_code == 200


def test_the_update_survives_a_database_without_the_audit_table(client, admin, make_user, db):
    _, headers = admin
    target = make_user()
    db.execute("DROP TABLE admin_audit")
    db.commit()

    assert client.patch(f"{USERS}/{target['id']}", json={"role": "teacher"},
                        headers=headers).status_code == 200
    assert db.execute("SELECT role FROM users WHERE id = ?",
                      (target["id"],)).fetchone()["role"] == "teacher"


def test_a_failing_socket_kill_switch_never_fails_the_patch(client, admin, make_user, monkeypatch):
    _, headers = admin
    target = make_user()

    def _explode(user_id):
        raise RuntimeError("socket layer down")

    monkeypatch.setattr("app.chat.events.disconnect_user_sockets", _explode)
    assert client.patch(f"{USERS}/{target['id']}", json={"active": False},
                        headers=headers).status_code == 200


def test_a_missing_socket_layer_never_fails_the_patch(client, admin, make_user, monkeypatch):
    _, headers = admin
    target = make_user()

    # A None entry makes `from app.chat.events import ...` raise
    # ImportError — the state before the helper landed there
    monkeypatch.setitem(sys.modules, "app.chat.events", None)
    assert client.patch(f"{USERS}/{target['id']}", json={"active": False},
                        headers=headers).status_code == 200


def test_the_kill_switch_is_called_for_the_deactivated_user(client, admin, make_user, monkeypatch):
    _, headers = admin
    target = make_user()
    called = []

    monkeypatch.setattr("app.chat.events.disconnect_user_sockets", called.append)
    client.patch(f"{USERS}/{target['id']}", json={"active": False}, headers=headers)
    assert called == [target["id"]]


def test_the_kill_switch_is_not_called_on_a_plain_role_change(client, admin, make_user, monkeypatch):
    _, headers = admin
    target = make_user()
    called = []

    monkeypatch.setattr("app.chat.events.disconnect_user_sockets", called.append)
    client.patch(f"{USERS}/{target['id']}", json={"role": "teacher"}, headers=headers)
    assert called == []








# -----------------------------------------------------------
# GET /admin/stats
# -----------------------------------------------------------

# -----------------------------------------------------------
# _plant_post
# -----------------------------------------------------------
#
# One news_posts row of a given source — the column the
# scrapedArticles counter groups on.
#
# Used by:
#   - the stats tests below
# -----------------------------------------------------------

def _plant_post(db, source, post_id=None):
    post_id = post_id or str(uuid.uuid4())
    db.execute(
        "INSERT INTO news_posts (id, title, content, source) VALUES (?, ?, ?, ?)",
        (post_id, f"Naujiena {post_id[:6]}", "Turinys", source),
    )
    db.commit()
    return post_id


@pytest.mark.contract
def test_the_dashboard_answers_the_five_counters(client, admin, db, make_user):
    _, headers = admin
    make_user(username="statistas")
    post_id = _plant_post(db, "knf.vu.lt")
    db.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text) VALUES (?, ?, ?, 'Komentaras')",
        (str(uuid.uuid4()), post_id, _admin_id(db)),
    )
    db.commit()

    response = client.get(STATS, headers=headers)
    assert response.status_code == 200
    stats = response.get_json()
    assert set(stats) == {"users", "posts", "scrapedArticles", "comments", "activeInvitations"}
    assert stats["users"] == 2
    assert stats["posts"] == 1
    assert stats["scrapedArticles"] == 1
    assert stats["comments"] == 1
    assert stats["activeInvitations"] == 1


def test_only_the_scraper_sources_count_as_scraped_articles(client, admin, db):
    _, headers = admin
    _plant_post(db, "knf.vu.lt")
    _plant_post(db, "vu.lt")
    _plant_post(db, "app")
    _plant_post(db, "user")
    _plant_post(db, "faculty")

    stats = client.get(STATS, headers=headers).get_json()
    assert stats["posts"] == 5
    assert stats["scrapedArticles"] == 2


def test_an_empty_database_answers_zeroes(client, admin, db):
    _, headers = admin
    db.execute("DELETE FROM invitation_codes")
    db.commit()

    stats = client.get(STATS, headers=headers).get_json()
    assert stats["posts"] == 0
    assert stats["scrapedArticles"] == 0
    assert stats["comments"] == 0
    assert stats["activeInvitations"] == 0


def test_an_exhausted_code_is_not_an_active_invitation(client, admin, db):
    _, headers = admin
    db.execute("DELETE FROM invitation_codes")
    db.commit()
    _plant_code(db, "ISNAUDOTAS", max_uses=2, use_count=2)

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 0


def test_an_expired_code_is_not_an_active_invitation(client, admin, db):
    _, headers = admin
    db.execute("DELETE FROM invitation_codes")
    db.commit()
    _plant_code(db, "PASIBAIGES",
                expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 0


def test_a_same_day_expired_code_does_not_count_as_active(client, admin, db):
    # The 'T' vs ' ' separator bug: compared raw, a space-form
    # timestamp sorts BEFORE every T-form one, so an hour-old dead
    # code from today still read as active
    _, headers = admin
    db.execute("DELETE FROM invitation_codes")
    db.commit()
    space_form = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    _plant_code(db, "SIANDIEN-MIRES", expires_at=space_form)

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 0


def test_a_live_multi_use_code_counts_as_active(client, admin, db):
    _, headers = admin
    db.execute("DELETE FROM invitation_codes")
    db.commit()
    _plant_code(db, "GYVAS", max_uses=5, use_count=2)

    assert client.get(STATS, headers=headers).get_json()["activeInvitations"] == 1


def test_the_counters_are_served_from_the_snapshot_inside_the_ttl(client, admin, db):
    _, headers = admin
    first = client.get(STATS, headers=headers).get_json()
    _plant_post(db, "knf.vu.lt")

    second = client.get(STATS, headers=headers).get_json()
    assert second == first, "a second call inside the TTL must not re-scan the tables"


def test_the_counters_rebuild_once_the_snapshot_lapses(client, admin, db, monkeypatch):
    _, headers = admin
    first = client.get(STATS, headers=headers).get_json()
    _plant_post(db, "knf.vu.lt")

    monkeypatch.setattr(admin_routes, "_STATS_CACHE_TTL", 0)
    second = client.get(STATS, headers=headers).get_json()

    assert first["posts"] == 0
    assert second["posts"] == 1








# -----------------------------------------------------------
# POST /admin/notifications and its background fan-out
# -----------------------------------------------------------

# -----------------------------------------------------------
# fake_socketio
# -----------------------------------------------------------
#
# The route hands the Expo round-trips to
# socketio.start_background_task; letting the real one run
# would spawn a thread that reaches the network (blocked by
# design) inside an unrelated test. This records the call
# instead, so the tests can assert WHAT was scheduled and
# drive the task themselves.
#
# Used by:
#   - the broadcast tests below
# -----------------------------------------------------------

@pytest.fixture
def fake_socketio(monkeypatch):

    class _Recorder:
        def __init__(self):
            self.calls = []

        def start_background_task(self, target, *args, **kwargs):
            self.calls.append((target, args, kwargs))
            return None

    recorder = _Recorder()
    monkeypatch.setattr(admin_routes, "_get_socketio", lambda: recorder)
    return recorder


def test_a_broadcast_answers_202_with_a_queued_job(client, admin, fake_socketio):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json={"title": "Dėmesio", "body": "Rytoj nevyks paskaitos"},
                           headers=headers)

    assert response.status_code == 202
    job = response.get_json()
    assert job["status"] == "queued"
    assert job["sent"] == 0
    assert job["failed"] == 0
    assert job["finishedAt"] is None
    assert uuid.UUID(job["jobId"])


def test_the_fanout_is_handed_to_a_background_task(client, admin, fake_socketio):
    _, headers = admin
    job = client.post(NOTIFICATIONS, json={"title": "T", "body": "B", "data": {"postId": "abc"}},
                      headers=headers).get_json()

    assert len(fake_socketio.calls) == 1
    target, args, _ = fake_socketio.calls[0]
    assert target is admin_routes._run_broadcast
    assert args[0] == job["jobId"]
    assert args[1] == "T"
    assert args[2] == "B"
    assert args[3] == {"postId": "abc", "type": "admin_announcement"}


def test_the_announcement_marker_survives_a_caller_supplied_type(client, admin, fake_socketio):
    _, headers = admin
    client.post(NOTIFICATIONS, json={"title": "T", "body": "B", "data": {"type": "kitas"}},
                headers=headers)

    _, args, _ = fake_socketio.calls[0]
    assert args[3]["type"] == "admin_announcement"


def test_the_broadcast_job_can_be_read_back(client, admin, fake_socketio):
    _, headers = admin
    job = client.post(NOTIFICATIONS, json={"title": "T", "body": "B"}, headers=headers).get_json()

    response = client.get(f"{NOTIFICATIONS}/{job['jobId']}", headers=headers)
    assert response.status_code == 200
    assert response.get_json() == job


def test_an_unknown_broadcast_job_is_404(client, admin):
    _, headers = admin
    response = client.get(f"{NOTIFICATIONS}/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Broadcast job not found"


def test_a_broadcast_writes_one_audit_row(client, admin, db, fake_socketio):
    user, headers = admin
    job = client.post(NOTIFICATIONS, json={"title": "Svarbu", "body": "B"},
                      headers=headers).get_json()

    rows = _audit(db, "notification.broadcast")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == user["id"]
    assert rows[0]["target"] == job["jobId"]
    assert json.loads(rows[0]["payload"]) == {"title": "Svarbu"}


def test_a_malformed_broadcast_body_is_rejected(client, admin, fake_socketio):
    _, headers = admin
    response = client.post(NOTIFICATIONS, data="{ne json", content_type="application/json",
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


@pytest.mark.parametrize("title,body", [(1, "B"), ("T", 2), (None, "B"), ("T", ["B"]), ({}, {})])
def test_a_non_string_title_or_body_is_rejected(client, admin, fake_socketio, title, body):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json={"title": title, "body": body}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body must be strings"


@pytest.mark.parametrize("payload", [{}, {"title": "T"}, {"body": "B"}, {"title": "  ", "body": "B"},
                                     {"title": "T", "body": "\n\t "}, {"title": "", "body": ""}])
def test_an_empty_title_or_body_is_rejected(client, admin, fake_socketio, payload):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json=payload, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title and body are required"


def test_the_title_and_body_are_trimmed_before_the_length_checks(client, admin, fake_socketio):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json={"title": "  " + "a" * 200 + "  ", "body": " B "},
                           headers=headers)

    assert response.status_code == 202
    assert response.get_json()["title"] == "a" * 200


def test_a_title_over_two_hundred_characters_is_rejected(client, admin, fake_socketio):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json={"title": "a" * 201, "body": "B"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Title must be at most 200 characters"


def test_a_body_at_the_thousand_character_limit_is_accepted(client, admin, fake_socketio):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json={"title": "T", "body": "b" * 1000}, headers=headers)
    assert response.status_code == 202


def test_a_body_over_a_thousand_characters_is_rejected(client, admin, fake_socketio):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json={"title": "T", "body": "b" * 1001}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Body must be at most 1000 characters"


@pytest.mark.parametrize("extra", ["duomenys", 5, ["a"], True])
def test_a_non_object_data_payload_is_rejected(client, admin, fake_socketio, extra):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json={"title": "T", "body": "B", "data": extra},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "data must be an object"


def test_an_oversized_data_payload_is_rejected(client, admin, fake_socketio):
    _, headers = admin
    response = client.post(NOTIFICATIONS,
                           json={"title": "T", "body": "B", "data": {"x": "y" * 4000}},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "data must serialise to at most 3072 bytes"
    assert fake_socketio.calls == []


def test_a_null_data_payload_still_carries_the_type_marker(client, admin, fake_socketio):
    _, headers = admin
    response = client.post(NOTIFICATIONS, json={"title": "T", "body": "B", "data": None},
                           headers=headers)

    assert response.status_code == 202
    _, args, _ = fake_socketio.calls[0]
    assert args[3] == {"type": "admin_announcement"}








# -----------------------------------------------------------
# The broadcast helpers, driven directly
# -----------------------------------------------------------

@pytest.mark.parametrize("result,expected", [
    (7, (7, 0)),
    (0, (0, 0)),
    (None, (0, 0)),
    ((4, 2), (4, 2)),
    ({"sent": 9, "failed": 1}, (9, 1)),
    ({"sent": 3}, (3, 0)),
    ({}, (0, 0)),
])
def test_the_fanout_counts_read_every_shape_push_may_return(result, expected):
    assert admin_routes._fanout_counts(result) == expected


def test_a_finished_broadcast_records_the_accepted_ticket_count(monkeypatch):
    monkeypatch.setattr("app.notifications.push.notify_channel", lambda *a, **k: 12)

    admin_routes._run_broadcast("darbas-1", "T", "B", {"type": "admin_announcement"})
    job = admin_routes._broadcast_job("darbas-1")

    assert job["status"] == "done"
    assert job["sent"] == 12
    assert job["failed"] == 0
    assert job["finishedAt"] is not None
    assert "12 device token(s)" in job["message"]


def test_a_broadcast_passes_the_admin_channel_and_the_payload_through(monkeypatch):
    seen = {}

    def _capture(channel, title, body, data=None):
        seen.update({"channel": channel, "title": title, "body": body, "data": data})
        return (2, 1)

    monkeypatch.setattr("app.notifications.push.notify_channel", _capture)
    admin_routes._run_broadcast("darbas-2", "Antraste", "Tekstas", {"type": "admin_announcement"})

    assert seen["channel"] == "admin"
    assert seen["title"] == "Antraste"
    assert seen["body"] == "Tekstas"
    assert seen["data"] == {"type": "admin_announcement"}
    assert admin_routes._broadcast_job("darbas-2")["failed"] == 1


def test_a_failing_fanout_is_recorded_as_failed_and_never_raises(monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("Expo unreachable")

    monkeypatch.setattr("app.notifications.push.notify_channel", _explode)
    admin_routes._run_broadcast("darbas-3", "T", "B", {})

    job = admin_routes._broadcast_job("darbas-3")
    assert job["status"] == "failed"
    assert job["finishedAt"] is not None
    assert "see the server log" in job["message"]


def test_an_unknown_job_id_resolves_to_none():
    assert admin_routes._broadcast_job("nera-tokio-darbo") is None


def test_the_job_registry_keeps_only_the_newest_records():
    for index in range(admin_routes._BROADCAST_JOBS_MAX + 10):
        admin_routes._set_broadcast_job(f"darbas-{index}", status="queued")

    assert len(admin_routes._broadcast_jobs) == admin_routes._BROADCAST_JOBS_MAX
    assert admin_routes._broadcast_job("darbas-0") is None
    assert admin_routes._broadcast_job(f"darbas-{admin_routes._BROADCAST_JOBS_MAX + 9}") is not None


def test_a_job_update_returns_a_copy_not_the_live_record():
    handed_out = admin_routes._set_broadcast_job("darbas-kopija", status="queued")
    handed_out["status"] = "sugadinta"

    assert admin_routes._broadcast_job("darbas-kopija")["status"] == "queued"


def test_the_socketio_lookup_returns_the_bound_instance(app):
    from app import socketio

    assert admin_routes._get_socketio() is socketio








# -----------------------------------------------------------
# Cross-route regressions
# -----------------------------------------------------------

def test_a_minted_code_registers_a_user_with_the_role_it_grants(client, admin):
    _, headers = admin
    minted = _mint(client, headers, role="teacher").get_json()

    response = client.post("/api/auth/register", json={
        "username": "naujas_destytojas",
        "password": PASSWORD,
        "display_name": "Naujas Dėstytojas",
        "email": "destytojas@knf.vu.lt",
        "invitation_code": minted["code"],
    })

    assert response.status_code == 201, response.get_json()
    assert response.get_json()["user"]["role"] == "teacher"


def test_the_listing_and_registration_agree_that_a_code_is_expired(client, admin, db):
    _, headers = admin
    _plant_code(db, "MIRES-KODAS",
                expires_at=(datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat())

    payload, _ = _listing(client, headers)
    row = next(r for r in payload["invitations"] if r["code"] == "MIRES-KODAS")
    assert row["expired"] is True

    response = client.post("/api/auth/register", json={
        "username": "veluojantis",
        "password": PASSWORD,
        "display_name": "Vėluojantis",
        "email": "veluojantis@knf.vu.lt",
        "invitation_code": "MIRES-KODAS",
    })
    assert response.status_code == 400


def test_a_revoked_code_can_no_longer_register_anybody(client, admin, db):
    _, headers = admin
    minted = _mint(client, headers, max_uses=10).get_json()

    assert client.delete(f"{INVITATIONS}/{minted['id']}", headers=headers).status_code == 200

    response = client.post("/api/auth/register", json={
        "username": "pavelavo",
        "password": PASSWORD,
        "display_name": "Pavėlavo",
        "email": "pavelavo@knf.vu.lt",
        "invitation_code": minted["code"],
    })
    assert response.status_code == 400


def test_a_curator_cannot_reach_an_admin_code_through_any_invitation_route(client, curator, admin, db):
    admin_user, admin_headers = admin
    curator_user, curator_headers = curator

    planted = _plant_code(db, "GALUTINIS-TESTAS", role="admin", created_by=admin_user["id"])

    # Minting one: refused. Listing it: absent. Revoking it: 404.
    assert _mint(client, curator_headers, role="admin").status_code == 403
    _, codes = _listing(client, curator_headers)
    assert "GALUTINIS-TESTAS" not in codes
    assert client.delete(f"{INVITATIONS}/{planted}", headers=curator_headers).status_code == 404

    # And the admin still has it
    _, admin_codes = _listing(client, admin_headers)
    assert "GALUTINIS-TESTAS" in admin_codes
