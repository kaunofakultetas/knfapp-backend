# -----------------------------------------------------------
#  [*] Tests — admin invitations, the exhaustive pass
#
#  A gap-closing companion to test_admin_invitations.py over
#  five functions of app/admin/routes.py and nothing else:
#  create_invitation, list_invitations, delete_invitation,
#  _invitation_expired and _pagination_clause. The broad file
#  proves the features work; this one walks every arm, guard
#  and boundary they own. What it adds:
#
#    - _pagination_clause as a unit: the four param
#      combinations, both int() failure arms INCLUDING the
#      TypeError one no query string can produce, which error
#      wins when both params are bad, and exactly what int()
#      quietly accepts (" 5 ", "+5", "1_0", "٥") — leniency
#      that is on the wire whether anyone meant it or not.
#    - _invitation_expired as a unit: the TypeError arm (every
#      non-str type), the ValueError arm, the naive-is-UTC
#      rule, a frozen clock on the exact microsecond of the
#      < boundary, and non-UTC offsets where the LOCAL reading
#      and the UTC reading disagree — the aware-to-aware claim
#      is only worth anything there.
#    - create_invitation's validation ORDER: body, then
#      max_uses type, expires_hours type, max_uses range,
#      expires_hours range, role whitelist, privilege — each
#      pinned by a body that trips two rules at once.
#    - the quota is spent by REJECTED mints too (the decorator
#      runs first), the 429 carries Retry-After, and a
#      throttled mint writes nothing.
#    - the uuid4 collision the module documents as negligible:
#      forced, it is a 500 and leaves exactly one row and a
#      usable connection behind.
#    - the full role × ownership matrix on BOTH scoped
#      queries: 8 combinations listed, 8 revoked, as a curator
#      and as an admin, so no cell of either scope is assumed.
#    - the 201 object and the listing object are the SAME
#      object, field for field — the app prepends one to a
#      list of the other.
#    - idempotency and the races a test can drive: revoke
#      twice, revoke a row deleted under us, revoke then
#      register, mint → expire → register.
#
#  Two defects this file found are now fixed and asserted
#  here: ?offset= past SQLite's integer range is a 400 rather
#  than the OverflowError 500 it used to be, and a
#  hand-edited non-numeric use_count / max_uses reads as
#  fully used (_invitation_fully_used) instead of 500-ing the
#  whole listing — the exact failure mode _invitation_expired
#  exists to prevent, through a column nobody had guarded.
# -----------------------------------------------------------

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import time_machine
from flask import request

from app.admin import routes as admin_routes
from app.auth.routes import PRIVILEGED_ROLES, ROLES, _rate_limit_store

INVITATIONS = "/api/admin/invitations"
REGISTER = "/api/auth/register"

PASSWORD = "RudensLapai!77"

# The two halves of the role space this module keeps apart:
# what a curator may mint, see and revoke, and what only an
# admin may touch
MINTABLE_BY_CURATOR = ("student", "teacher")




# -----------------------------------------------------------
# clean_module_state
# -----------------------------------------------------------
#
# auth's rate-limit window is process-wide and outlives the
# per-test `app` fixture — it carries both the 30-per-actor
# invite quota this file deliberately exhausts and the app's
# global per-IP budget. The stats snapshot and the broadcast
# registry are cleared with it so a neighbouring module's
# leftovers cannot reach a test here either.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_module_state():
    _rate_limit_store.clear()
    admin_routes._stats_cache.clear()
    yield
    _rate_limit_store.clear()
    admin_routes._stats_cache.clear()




# -----------------------------------------------------------
# curator / second_curator
# -----------------------------------------------------------
#
# The scoped half of every invitation route is written from a
# curator's point of view, and the ownership matrix needs a
# SECOND one to own the rows the first may not touch.
#
# Used by:
#   - the scope, listing and revoke matrices below
# -----------------------------------------------------------

@pytest.fixture
def curator(make_user, auth_headers):
    user = make_user(role="curator", display_name="Kuratore")
    return user, auth_headers(user)


@pytest.fixture
def second_curator(make_user, auth_headers):
    user = make_user(role="curator", display_name="Antra kuratore")
    return user, auth_headers(user)




# -----------------------------------------------------------
# _fake_args
# -----------------------------------------------------------
#
# Swaps the module's `request` for a stand-in carrying just
# the .args mapping _pagination_clause reads. The point is the
# int() TypeError arm: request.args.get() hands back a str or
# None and nothing else, so no query string on earth can reach
# that except: clause — but the code catches it, so it gets
# tested rather than pragma'd away.
#
# Used by:
#   - the _pagination_clause unit tests below
# -----------------------------------------------------------

def _fake_args(monkeypatch, **args):
    monkeypatch.setattr(admin_routes, "request", SimpleNamespace(args=dict(args)))
    return admin_routes._pagination_clause()




# -----------------------------------------------------------
# _clause
# -----------------------------------------------------------
#
# _pagination_clause under a REAL request context, so the
# query string goes through Werkzeug's own parsing and
# multi-value rules exactly as it does in production.
#
# Used by:
#   - the query-string tests below
# -----------------------------------------------------------

def _clause(app, query):
    with app.test_request_context(INVITATIONS + query):
        return admin_routes._pagination_clause()




# -----------------------------------------------------------
# _plant
# -----------------------------------------------------------
#
# One invitation_codes row in any state a test needs — role,
# owner, counters and timestamps all free — returning its id.
# Every state the routes REFUSE to create (an admin-role code
# owned by a curator, an exhausted code, an unparsable
# expiry) has to arrive this way.
#
# Used by:
#   - the listing, matrix and revoke tests below
# -----------------------------------------------------------

def _plant(db, code, role="student", created_by=None, max_uses=1, use_count=0,
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
# _mint / _mint_raw
# -----------------------------------------------------------
#
# POST /admin/invitations. The raw variant serialises the body
# itself and posts bytes: the app's JSON provider html-escapes
# every string a `json=` kwarg goes out through (TESTPLAN rule
# 10), and the numeric-type tests below care about the exact
# literal on the wire — 1 and 1.0 are the same Python value to
# the test client and two different bodies to the handler.
#
# Used by:
#   - the create_invitation tests below
# -----------------------------------------------------------

def _mint(client, headers, **body):
    return client.post(INVITATIONS, json=body, headers=headers)


def _mint_raw(client, headers, payload, content_type="application/json"):
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    return client.post(INVITATIONS, data=body,
                       headers={**headers, "Content-Type": content_type})




# -----------------------------------------------------------
# _rows / _codes
# -----------------------------------------------------------
#
# The listing's row objects, and just the code strings in it —
# which is what a scoping assertion is actually about.
#
# Used by:
#   - the listing and matrix tests below
# -----------------------------------------------------------

def _rows(client, headers, query=""):
    response = client.get(INVITATIONS + query, headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["invitations"]


def _codes(client, headers, query=""):
    return [row["code"] for row in _rows(client, headers, query)]




# -----------------------------------------------------------
# _audit_rows
# -----------------------------------------------------------
#
# The admin_audit trail for one action, oldest first.
#
# Used by:
#   - the audit assertions below
# -----------------------------------------------------------

def _audit_rows(db, action):
    return db.execute(
        "SELECT * FROM admin_audit WHERE action = ? ORDER BY created_at",
        (action,),
    ).fetchall()




# -----------------------------------------------------------
# _register_with
# -----------------------------------------------------------
#
# A registration attempt carrying an invitation code — the
# other half of every "the listing and register() agree"
# claim in this module.
#
# Used by:
#   - the cross-check tests below
# -----------------------------------------------------------

def _register_with(client, code, username):
    return client.post(REGISTER, json={
        "username": username,
        "password": PASSWORD,
        "display_name": "Naujokas",
        "email": f"{username}@knf.vu.lt",
        "invitation_code": code,
    })








# -----------------------------------------------------------
# _pagination_clause — the four param combinations
# -----------------------------------------------------------

def test_neither_param_returns_the_empty_clause_that_pages_nothing(app):
    assert _clause(app, "") == ("", [], None)


def test_a_limit_alone_pages_from_the_first_row(app):
    assert _clause(app, "?limit=7") == (" LIMIT ? OFFSET ?", [7, 0], None)


def test_an_offset_alone_rides_on_sqlites_unlimited_row_count(app):
    assert _clause(app, "?offset=3") == (" LIMIT ? OFFSET ?", [-1, 3], None)


def test_both_params_together_bind_both_values(app):
    assert _clause(app, "?limit=2&offset=4") == (" LIMIT ? OFFSET ?", [2, 4], None)


def test_an_empty_query_string_is_the_same_as_no_query_string(app):
    assert _clause(app, "?") == ("", [], None)


def test_an_unrelated_query_param_never_starts_paging(app):
    assert _clause(app, "?role=admin&page=2") == ("", [], None)




# -----------------------------------------------------------
# _pagination_clause — the bounds, both ends of both
# -----------------------------------------------------------

@pytest.mark.parametrize("limit", [1, 2, 499, 500])
def test_every_accepted_limit_comes_back_bound_unchanged(app, limit):
    assert _clause(app, f"?limit={limit}") == (" LIMIT ? OFFSET ?", [limit, 0], None)


@pytest.mark.parametrize("limit", [0, -1, 501, 1000, 10 ** 20])
def test_a_limit_outside_the_range_returns_the_range_message_and_no_clause(app, limit):
    assert _clause(app, f"?limit={limit}") == ("", [], "limit must be between 1 and 500")


@pytest.mark.parametrize("offset", [0, 1, 499, 10 ** 9])
def test_every_non_negative_offset_is_accepted(app, offset):
    assert _clause(app, f"?offset={offset}") == (" LIMIT ? OFFSET ?", [-1, offset], None)


@pytest.mark.parametrize("offset", [-1, -2, -10 ** 9])
def test_a_negative_offset_returns_its_own_message(app, offset):
    assert _clause(app, f"?offset={offset}") == ("", [], "offset must be zero or greater")


def test_minus_zero_is_a_zero_offset_not_a_negative_one(app):
    assert _clause(app, "?offset=-0") == (" LIMIT ? OFFSET ?", [-1, 0], None)


def test_minus_zero_is_still_an_out_of_range_limit(app):
    assert _clause(app, "?limit=-0") == ("", [], "limit must be between 1 and 500")




# -----------------------------------------------------------
# _pagination_clause — what int() rejects, and what it quietly
# takes: the leniency is a wire contract whether or not anyone
# chose it
# -----------------------------------------------------------

@pytest.mark.parametrize("raw", ["abc", "", "1.5", "1e2", "0x10", "5px", "null", "true", "NaN", "١٢٣abc"])
def test_an_unparsable_limit_is_the_integer_message(app, raw):
    assert _clause(app, f"?limit={raw}") == ("", [], "limit must be an integer")


@pytest.mark.parametrize("raw", ["abc", "", "2.0", "one", "-"])
def test_an_unparsable_offset_is_the_integer_message(app, raw):
    assert _clause(app, f"?offset={raw}") == ("", [], "offset must be an integer")


@pytest.mark.parametrize("raw,parsed", [("+5", 5), ("005", 5), ("1_0", 10), ("%20%205%20", 5), ("٥", 5)])
def test_int_accepts_more_than_plain_digits_and_the_clause_inherits_it(app, raw, parsed):
    assert _clause(app, f"?limit={raw}") == (" LIMIT ? OFFSET ?", [parsed, 0], None)


def test_a_repeated_limit_takes_the_first_value_werkzeug_parsed(app):
    assert _clause(app, "?limit=5&limit=9") == (" LIMIT ? OFFSET ?", [5, 0], None)




# -----------------------------------------------------------
# _pagination_clause — which complaint wins, and the arm no
# query string can reach
# -----------------------------------------------------------

def test_a_bad_limit_is_reported_before_a_bad_offset(app):
    assert _clause(app, "?limit=abc&offset=abc") == ("", [], "limit must be an integer")


def test_an_out_of_range_limit_is_reported_before_a_negative_offset(app):
    assert _clause(app, "?limit=0&offset=-5") == ("", [], "limit must be between 1 and 500")


def test_a_valid_limit_does_not_rescue_a_bad_offset(app):
    assert _clause(app, "?limit=10&offset=abc") == ("", [], "offset must be an integer")


def test_a_valid_offset_does_not_rescue_a_bad_limit(app):
    assert _clause(app, "?limit=abc&offset=10") == ("", [], "limit must be an integer")


def test_a_non_string_limit_trips_the_type_error_arm(monkeypatch):
    assert _fake_args(monkeypatch, limit=["5"]) == ("", [], "limit must be an integer")


def test_a_non_string_offset_trips_the_type_error_arm(monkeypatch):
    assert _fake_args(monkeypatch, offset={"n": 1}) == ("", [], "offset must be an integer")


def test_the_fake_request_still_reaches_the_happy_path(monkeypatch):
    assert _fake_args(monkeypatch, limit="4", offset="8") == (" LIMIT ? OFFSET ?", [4, 8], None)








# -----------------------------------------------------------
# _invitation_expired — the TypeError arm
# -----------------------------------------------------------

@pytest.mark.parametrize("value", [None, 0, 1, -1, 1.5, True, False, b"2030-01-01",
                                   ["2030-01-01"], {"expires_at": "2030-01-01"}, (2030, 1, 1)])
def test_anything_that_is_not_a_string_reads_as_expired(value):
    assert admin_routes._invitation_expired(value) is True


def test_a_datetime_object_is_not_a_string_either_so_it_reads_as_expired():
    # fromisoformat takes str only — a caller handing it the parsed
    # value gets the conservative verdict, never a crash
    assert admin_routes._invitation_expired(datetime.now(timezone.utc)) is True




# -----------------------------------------------------------
# _invitation_expired — the ValueError arm
# -----------------------------------------------------------

@pytest.mark.parametrize("value", [
    "",
    "labas",
    "2020-13-45",
    "2020-02-30T00:00:00+00:00",
    "  2030-01-01",
    "2030-01-01T00:00:00+99:00",
    "2030/01/01",
    "01-01-2030",
    "9999999999",
    "2030-01-01T25:00:00",
])
def test_an_unparsable_timestamp_reads_as_expired(value):
    assert admin_routes._invitation_expired(value) is True




# -----------------------------------------------------------
# _invitation_expired — the shapes fromisoformat DOES take
# -----------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("2030-01-01T00:00:00+00:00", False),
    ("2030-01-01T00:00:00Z", False),
    ("2030-01-01 00:00:00", False),
    ("2030-01-01", False),
    ("20300101", False),
    ("9999-12-31T23:59:59.999999+00:00", False),
    ("2020-01-01T00:00:00+00:00", True),
    ("2020-01-01", True),
    ("0001-01-01T00:00:00+00:00", True),
])
def test_a_parsable_timestamp_is_judged_on_its_moment(value, expected):
    assert admin_routes._invitation_expired(value) is expected


def test_a_naive_timestamp_is_read_as_utc_not_as_local_time():
    # 30 minutes ahead of a frozen UTC clock, written without an
    # offset: read as UTC it is still in the future
    with time_machine.travel(datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc), tick=False):
        assert admin_routes._invitation_expired("2030-06-01T12:30:00") is False
        assert admin_routes._invitation_expired("2030-06-01T11:30:00") is True




# -----------------------------------------------------------
# _invitation_expired — the < boundary, to the microsecond
# -----------------------------------------------------------

def test_a_timestamp_equal_to_now_is_not_yet_expired():
    frozen = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        assert admin_routes._invitation_expired(frozen.isoformat()) is False


def test_one_microsecond_before_now_is_expired():
    frozen = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        assert admin_routes._invitation_expired((frozen - timedelta(microseconds=1)).isoformat()) is True


def test_one_microsecond_after_now_is_not_expired():
    frozen = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        assert admin_routes._invitation_expired((frozen + timedelta(microseconds=1)).isoformat()) is False




# -----------------------------------------------------------
# _invitation_expired — non-UTC offsets, where "aware-to-aware"
# is the only thing keeping the answer right
# -----------------------------------------------------------

def test_an_offset_timestamp_is_compared_in_utc_not_by_its_wall_clock():
    frozen = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        # 13:00+03:00 IS 10:00 UTC — later on its own wall clock,
        # two hours in the past for the comparison that matters
        assert admin_routes._invitation_expired("2030-06-01T13:00:00+03:00") is True
        # 11:00-03:00 IS 14:00 UTC — the mirror image
        assert admin_routes._invitation_expired("2030-06-01T11:00:00-03:00") is False


def test_the_same_instant_written_three_ways_gets_one_verdict():
    frozen = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    with time_machine.travel(frozen, tick=False):
        spellings = ["2030-06-01T13:00:00+00:00", "2030-06-01T16:00:00+03:00",
                     "2030-06-01T13:00:00Z", "2030-06-01T13:00:00"]
        assert [admin_routes._invitation_expired(s) for s in spellings] == [False] * 4








# -----------------------------------------------------------
# create_invitation — the order the guards fire in, each
# pinned by a body that breaks two rules at once
# -----------------------------------------------------------

def test_a_non_object_body_is_refused_before_any_field_is_read(client, admin):
    response = _mint_raw(client, admin[1], "{not json at all")
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_bodyless_post_with_no_content_type_is_the_same_refusal(client, admin):
    response = client.post(INVITATIONS, headers=admin[1])
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded"])
def test_a_valid_json_body_under_the_wrong_content_type_is_not_a_body(client, admin, content_type):
    response = _mint_raw(client, admin[1], {"role": "teacher"}, content_type=content_type)
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_json_null_body_is_refused_as_no_body_at_all(client, admin):
    response = _mint_raw(client, admin[1], "null")
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_an_uppercase_json_content_type_still_mints(client, admin):
    response = _mint_raw(client, admin[1], {"role": "teacher"}, content_type="APPLICATION/JSON")
    assert response.status_code == 201
    assert response.get_json()["role"] == "teacher"


def test_a_charset_on_the_content_type_still_mints(client, admin):
    response = _mint_raw(client, admin[1], {"role": "teacher"},
                         content_type="application/json; charset=utf-8")
    assert response.status_code == 201


def test_the_max_uses_type_is_checked_before_the_expires_hours_type(client, admin):
    response = _mint(client, admin[1], max_uses="5", expires_hours="24")
    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be an integer"


def test_the_expires_hours_type_is_checked_before_every_range(client, admin):
    response = _mint(client, admin[1], max_uses=1, expires_hours=[24])
    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be an integer"


def test_a_bad_max_uses_type_beats_an_out_of_range_expires_hours(client, admin):
    response = _mint(client, admin[1], max_uses=None, expires_hours=99999)
    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be an integer"


def test_the_max_uses_range_is_checked_before_the_expires_hours_range(client, admin):
    response = _mint(client, admin[1], max_uses=0, expires_hours=0)
    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be between 1 and 1000"


def test_the_expires_hours_range_is_checked_before_the_role(client, admin):
    response = _mint(client, admin[1], expires_hours=0, role="dekanas")
    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be between 1 and 8760"


def test_an_unknown_role_is_refused_before_the_privilege_check(client, curator):
    response = _mint(client, curator[1], role="superadmin")
    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"


def test_the_privilege_check_is_the_last_gate_a_curator_meets(client, curator):
    response = _mint(client, curator[1], role="admin", max_uses=1000, expires_hours=8760)
    assert response.status_code == 403
    assert response.get_json()["error"] == "Only admins can create admin/curator invitations"




# -----------------------------------------------------------
# create_invitation — the numeric types, exactly as they sit
# on the wire
# -----------------------------------------------------------

@pytest.mark.parametrize("literal", ["1.0", "2.5", "1e2", '"1"', "null", "[]", "{}"])
def test_a_max_uses_that_is_not_a_json_integer_is_refused(client, admin, literal):
    response = _mint_raw(client, admin[1], '{"max_uses": %s}' % literal)
    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be an integer"


@pytest.mark.parametrize("literal", ["24.0", "0.5", '"24"', "null", "[]", "{}"])
def test_an_expires_hours_that_is_not_a_json_integer_is_refused(client, admin, literal):
    response = _mint_raw(client, admin[1], '{"expires_hours": %s}' % literal)
    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be an integer"


@pytest.mark.parametrize("literal,expected", [("true", "max_uses must be an integer"),
                                              ("false", "max_uses must be an integer")])
def test_a_json_boolean_is_not_an_integer_even_though_python_says_it_is(client, admin, literal, expected):
    response = _mint_raw(client, admin[1], '{"max_uses": %s}' % literal)
    assert response.status_code == 400
    assert response.get_json()["error"] == expected


def test_an_integer_far_past_what_sqlite_could_store_is_a_range_400_not_a_crash(client, admin):
    response = _mint_raw(client, admin[1], '{"max_uses": %d}' % (10 ** 40))
    assert response.status_code == 400
    assert response.get_json()["error"] == "max_uses must be between 1 and 1000"


def test_an_expires_hours_big_enough_to_overflow_timedelta_never_reaches_it(client, admin):
    response = _mint_raw(client, admin[1], '{"expires_hours": %d}' % (10 ** 18))
    assert response.status_code == 400
    assert response.get_json()["error"] == "expires_hours must be between 1 and 8760"


@pytest.mark.parametrize("max_uses,hours", [(1, 1), (1, 8760), (1000, 1), (1000, 8760)])
def test_the_four_corners_of_the_accepted_ranges_all_mint(client, admin, max_uses, hours):
    response = _mint(client, admin[1], max_uses=max_uses, expires_hours=hours)
    assert response.status_code == 201
    assert response.get_json()["maxUses"] == max_uses




# -----------------------------------------------------------
# create_invitation — the role whitelist and the privilege gate
# -----------------------------------------------------------

@pytest.mark.parametrize("role", ["Student", "STUDENT", "student ", " student", "studentas",
                                  "", "admin;", "student\n", True, 0, 3.5])
def test_a_role_outside_the_whitelist_is_refused_whatever_shape_it_has(client, admin, role):
    response = _mint(client, admin[1], role=role)
    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"


def test_an_explicit_null_role_is_not_the_default_student(client, admin):
    response = _mint_raw(client, admin[1], '{"role": null}')
    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid role"


@pytest.mark.parametrize("role", ROLES)
def test_an_admin_mints_every_role_and_gets_it_back(client, admin, role):
    response = _mint(client, admin[1], role=role)
    assert response.status_code == 201
    assert response.get_json()["role"] == role


@pytest.mark.parametrize("role", MINTABLE_BY_CURATOR)
def test_a_curator_mints_the_two_roles_they_own(client, curator, role):
    response = _mint(client, curator[1], role=role)
    assert response.status_code == 201
    assert response.get_json()["createdBy"] == curator[0]["id"]


@pytest.mark.parametrize("role", PRIVILEGED_ROLES)
def test_a_curators_privileged_mint_is_refused_and_stores_nothing(client, curator, role, db):
    before = db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"]
    response = _mint(client, curator[1], role=role)
    assert response.status_code == 403
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"] == before
    assert _audit_rows(db, "invitation.create") == []




# -----------------------------------------------------------
# create_invitation — the configured default expiry
# -----------------------------------------------------------

def test_a_missing_expiry_config_key_falls_back_to_a_week(client, app, admin):
    app.config.pop("INVITATION_EXPIRY_HOURS", None)
    response = _mint(client, admin[1])
    assert response.status_code == 201

    minted = datetime.fromisoformat(response.get_json()["expiresAt"])
    assert timedelta(hours=167) < minted - datetime.now(timezone.utc) <= timedelta(hours=168)


@pytest.mark.parametrize("configured", ["168", 168.0, None, [168], {"hours": 168}, True, False])
def test_a_configured_expiry_that_is_not_an_integer_falls_back_to_a_week(client, app, admin, configured):
    # A nonsense env value must not turn every default mint into a
    # 400 — and a bool is not an integer here however Python feels
    app.config["INVITATION_EXPIRY_HOURS"] = configured
    response = _mint(client, admin[1])
    assert response.status_code == 201

    minted = datetime.fromisoformat(response.get_json()["expiresAt"])
    assert timedelta(hours=167) < minted - datetime.now(timezone.utc) <= timedelta(hours=168)


@pytest.mark.parametrize("configured,expected_hours", [(1, 1), (8760, 8760), (0, 1), (-5, 1),
                                                       (100000, 8760)])
def test_the_configured_default_is_clamped_into_the_accepted_range(client, app, admin,
                                                                   configured, expected_hours):
    app.config["INVITATION_EXPIRY_HOURS"] = configured
    response = _mint(client, admin[1])
    assert response.status_code == 201

    minted = datetime.fromisoformat(response.get_json()["expiresAt"])
    delta = minted - datetime.now(timezone.utc)
    assert timedelta(hours=expected_hours) - timedelta(minutes=1) < delta <= timedelta(hours=expected_hours)


def test_an_explicit_zero_is_refused_even_when_the_config_would_allow_it(client, app, admin):
    app.config["INVITATION_EXPIRY_HOURS"] = 24
    response = _mint(client, admin[1], expires_hours=0)
    assert response.status_code == 400




# -----------------------------------------------------------
# create_invitation — what lands in the row and in the trail
# -----------------------------------------------------------

def test_the_response_and_the_stored_row_carry_the_identical_timestamps(client, admin, db):
    payload = _mint(client, admin[1], max_uses=3, expires_hours=48).get_json()

    row = db.execute("SELECT * FROM invitation_codes WHERE id = ?", (payload["id"],)).fetchone()
    assert row["expires_at"] == payload["expiresAt"]
    assert row["created_at"] == payload["createdAt"]
    assert row["max_uses"] == 3
    assert row["use_count"] == 0


def test_twenty_mints_produce_twenty_distinct_codes_and_ids(client, admin):
    minted = [_mint(client, admin[1]).get_json() for _ in range(20)]
    assert len({m["code"] for m in minted}) == 20
    assert len({m["id"] for m in minted}) == 20


def test_the_audit_row_names_the_minter_the_code_and_the_terms(client, curator, db):
    payload = _mint(client, curator[1], role="teacher", max_uses=5, expires_hours=12).get_json()

    rows = _audit_rows(db, "invitation.create")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == curator[0]["id"]
    assert rows[0]["target"] == payload["id"]
    assert json.loads(rows[0]["payload"]) == {"role": "teacher", "maxUses": 5,
                                              "expiresAt": payload["expiresAt"]}


def test_unknown_body_fields_are_ignored_rather_than_stored(client, admin, db):
    payload = _mint(client, admin[1], role="student", use_count=99, code="MANO-KODAS",
                    created_by="kitas", id="mano-id").get_json()

    assert payload["id"] != "mano-id"
    assert payload["code"] != "MANO-KODAS"
    row = db.execute("SELECT * FROM invitation_codes WHERE id = ?", (payload["id"],)).fetchone()
    assert row["use_count"] == 0
    assert row["created_by"] == admin[0]["id"]




# -----------------------------------------------------------
# create_invitation — the quota, which the decorator spends
# before the handler ever sees the body
# -----------------------------------------------------------

def test_the_thirtieth_mint_passes_and_the_thirty_first_is_throttled(client, admin):
    for _ in range(30):
        assert _mint(client, admin[1]).status_code == 201

    response = _mint(client, admin[1])
    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_a_throttled_mint_carries_a_usable_retry_after(client, admin):
    for _ in range(30):
        _mint(client, admin[1])

    response = _mint(client, admin[1])
    assert 1 <= int(response.headers["Retry-After"]) <= 301


def test_rejected_mints_spend_the_quota_too(client, admin, db):
    for _ in range(30):
        assert _mint(client, admin[1], max_uses=0).status_code == 400

    response = _mint(client, admin[1])
    assert response.status_code == 429
    # Thirty 400s and one 429 minted nothing: only the bootstrap row
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"] == 1


def test_a_throttled_mint_writes_neither_a_code_nor_an_audit_row(client, admin, db):
    for _ in range(30):
        _mint(client, admin[1])
    before = db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"]

    assert _mint(client, admin[1]).status_code == 429
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"] == before
    assert len(_audit_rows(db, "invitation.create")) == 30


def test_an_exhausted_curator_never_blocks_the_admin(client, admin, curator):
    for _ in range(30):
        _mint(client, curator[1])

    assert _mint(client, curator[1]).status_code == 429
    assert _mint(client, admin[1]).status_code == 201




# -----------------------------------------------------------
# create_invitation — the collision the banner calls negligible
# -----------------------------------------------------------

def test_a_code_collision_is_a_500_that_leaves_one_row_and_a_working_route(client, app, admin, db,
                                                                          monkeypatch):
    # Same first 12 hex chars every time (so `code` collides on its
    # UNIQUE index) with a different tail (so `id` does not) — the
    # collision the module documents, forced
    counter = {"n": 0}

    def fixed_prefix_uuid4():
        counter["n"] += 1
        return uuid.UUID("aaaaaaaaaaaa" + f"{counter['n']:020d}")

    monkeypatch.setattr(admin_routes.uuid, "uuid4", fixed_prefix_uuid4)
    app.config["PROPAGATE_EXCEPTIONS"] = False

    assert _mint(client, admin[1]).status_code == 201
    assert _mint(client, admin[1]).status_code == 500

    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE code = 'AAAAAAAAAAAA'"
                      ).fetchone()["c"] == 1

    # The failed request closed its connection in the finally, so the
    # next caller is not staring at a locked database
    monkeypatch.undo()
    assert _mint(client, admin[1]).status_code == 201








# -----------------------------------------------------------
# list_invitations — the empty and the whole
# -----------------------------------------------------------

def test_an_empty_table_lists_an_empty_array_not_a_404(client, admin, db):
    db.execute("DELETE FROM invitation_codes")
    db.commit()

    response = client.get(INVITATIONS, headers=admin[1])
    assert response.status_code == 200
    assert response.get_json() == {"invitations": []}


def test_a_curator_with_nothing_of_their_own_lists_an_empty_array(client, curator, admin, db):
    _plant(db, "SVETIMAS", role="student", created_by=admin[0]["id"])
    assert _rows(client, curator[1]) == []


def test_sixty_rows_all_come_back_when_no_page_params_are_given(client, admin, db):
    for n in range(60):
        _plant(db, f"KODAS{n:03d}", created_by=admin[0]["id"],
               created_at=f"2030-01-01T00:{n:02d}:00+00:00")

    assert len(_rows(client, admin[1])) == 61  # the sixty plus the bootstrap row


@pytest.mark.contract
def test_every_listed_row_carries_exactly_the_ten_documented_keys(client, admin):
    _mint(client, admin[1])
    for row in _rows(client, admin[1]):
        assert set(row) == {"id", "code", "role", "maxUses", "useCount", "expiresAt",
                            "createdAt", "createdBy", "expired", "fullyUsed"}


@pytest.mark.contract
def test_the_derived_flags_are_real_json_booleans_not_zero_and_one(client, admin, db):
    _plant(db, "PASIBAIGES", created_by=admin[0]["id"], use_count=1, max_uses=1,
           expires_at="2020-01-01T00:00:00+00:00")

    row = next(r for r in _rows(client, admin[1]) if r["code"] == "PASIBAIGES")
    assert row["expired"] is True
    assert row["fullyUsed"] is True
    assert isinstance(row["useCount"], int) and isinstance(row["maxUses"], int)




# -----------------------------------------------------------
# list_invitations — ordering and paging over a known set
# -----------------------------------------------------------

@pytest.fixture
def five_codes(db, admin):
    # Five rows a minute apart, newest last in the loop and therefore
    # FIRST in the listing
    for n in range(5):
        _plant(db, f"EILE{n}", created_by=admin[0]["id"],
               created_at=f"2030-03-0{n + 1}T00:00:00+00:00")
    db.execute("DELETE FROM invitation_codes WHERE code NOT LIKE 'EILE%'")
    db.commit()
    return ["EILE4", "EILE3", "EILE2", "EILE1", "EILE0"]


def test_the_listing_is_newest_first_by_created_at(client, admin, five_codes):
    assert _codes(client, admin[1]) == five_codes


def test_a_limit_takes_the_newest_rows_off_the_top(client, admin, five_codes):
    assert _codes(client, admin[1], "?limit=2") == five_codes[:2]


def test_an_offset_alone_skips_from_the_top_and_keeps_the_rest(client, admin, five_codes):
    assert _codes(client, admin[1], "?offset=2") == five_codes[2:]


def test_paging_two_at_a_time_reconstructs_the_whole_ordered_listing(client, admin, five_codes):
    paged = []
    for offset in (0, 2, 4):
        paged += _codes(client, admin[1], f"?limit=2&offset={offset}")
    assert paged == five_codes


def test_an_offset_past_the_last_row_is_an_empty_page(client, admin, five_codes):
    assert _codes(client, admin[1], "?offset=5") == []
    assert _codes(client, admin[1], "?limit=2&offset=99") == []


def test_a_limit_larger_than_the_table_returns_the_whole_table(client, admin, five_codes):
    assert _codes(client, admin[1], "?limit=500") == five_codes


def test_a_zero_offset_changes_nothing(client, admin, five_codes):
    assert _codes(client, admin[1], "?offset=0") == five_codes


@pytest.mark.parametrize("query,message", [
    ("?limit=0", "limit must be between 1 and 500"),
    ("?limit=501", "limit must be between 1 and 500"),
    ("?limit=abc", "limit must be an integer"),
    ("?offset=-1", "offset must be zero or greater"),
    ("?offset=abc", "offset must be an integer"),
    ("?limit=1&offset=x", "offset must be an integer"),
])
def test_a_bad_page_param_is_a_400_on_the_admin_listing(client, admin, query, message):
    response = client.get(INVITATIONS + query, headers=admin[1])
    assert response.status_code == 400
    assert response.get_json()["error"] == message


@pytest.mark.parametrize("query,message", [
    ("?limit=0", "limit must be between 1 and 500"),
    ("?offset=-1", "offset must be zero or greater"),
])
def test_the_same_400_answers_a_curator_before_the_scoped_query_runs(client, curator, query, message):
    response = client.get(INVITATIONS + query, headers=curator[1])
    assert response.status_code == 400
    assert response.get_json()["error"] == message


def test_the_curator_listing_pages_over_their_own_rows_only(client, curator, admin, db):
    for n in range(4):
        _plant(db, f"MANO{n}", created_by=curator[0]["id"],
               created_at=f"2030-04-0{n + 1}T00:00:00+00:00")
    _plant(db, "SVETIMAS", created_by=admin[0]["id"], created_at="2030-05-01T00:00:00+00:00")

    assert _codes(client, curator[1], "?limit=2") == ["MANO3", "MANO2"]
    assert _codes(client, curator[1], "?limit=2&offset=2") == ["MANO1", "MANO0"]


def test_an_offset_past_the_sqlite_integer_range_is_a_400_not_a_crash(client, app, admin):
    app.config["PROPAGATE_EXCEPTIONS"] = False
    response = client.get(INVITATIONS + "?offset=9223372036854775808", headers=admin[1])
    assert response.status_code == 400
    assert response.get_json()["error"] == "offset must be at most 9223372036854775807"


def test_the_largest_offset_sqlite_can_hold_is_still_an_empty_page(client, admin):
    assert _codes(client, admin[1], "?offset=9223372036854775807") == []




# -----------------------------------------------------------
# list_invitations — the derived flags, row by row
# -----------------------------------------------------------

def test_one_unparsable_row_never_takes_the_listing_down_with_it(client, admin, db):
    _plant(db, "GERAS", created_by=admin[0]["id"],
           expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    _plant(db, "SUGADINTAS", created_by=admin[0]["id"], expires_at="ne data")
    _plant(db, "SENAS", created_by=admin[0]["id"], expires_at="2020-01-01T00:00:00+00:00")

    flags = {r["code"]: r["expired"] for r in _rows(client, admin[1])}
    assert flags["GERAS"] is False
    assert flags["SUGADINTAS"] is True
    assert flags["SENAS"] is True


@pytest.mark.parametrize("use_count,max_uses,expected", [
    (0, 1, False), (1, 1, True), (2, 1, True), (0, 0, True), (4, 5, False), (5, 5, True),
    (-1, 1, False), (0, 1000, False), (1000, 1000, True),
])
def test_the_fully_used_flag_is_a_plain_use_count_comparison(client, admin, db,
                                                             use_count, max_uses, expected):
    _plant(db, "SKAICIAI", created_by=admin[0]["id"], use_count=use_count, max_uses=max_uses)

    row = next(r for r in _rows(client, admin[1]) if r["code"] == "SKAICIAI")
    assert row["fullyUsed"] is expected


def test_a_row_with_no_creator_still_lists_with_a_null_created_by(client, admin, db):
    _plant(db, "NIEKIENO", created_by=None)

    row = next(r for r in _rows(client, admin[1]) if r["code"] == "NIEKIENO")
    assert row["createdBy"] is None


@pytest.mark.parametrize("column", ["use_count", "max_uses"])
def test_a_hand_edited_counter_does_not_take_the_listing_down(client, app, admin, db, column):
    code_id = _plant(db, "RANKINIS", created_by=admin[0]["id"])
    db.execute(f"UPDATE invitation_codes SET {column} = 'daug' WHERE id = ?", (code_id,))
    db.commit()

    app.config["PROPAGATE_EXCEPTIONS"] = False
    response = client.get(INVITATIONS, headers=admin[1])

    assert response.status_code == 200
    # Unreadable counter, conservative verdict: the code reads as
    # spent rather than as one an admin may still hand out
    row = next(r for r in response.get_json()["invitations"] if r["code"] == "RANKINIS")
    assert row["fullyUsed"] is True




# -----------------------------------------------------------
# list_invitations — the whole role × ownership matrix, from
# both sides of the scope
# -----------------------------------------------------------

MATRIX = [(role, owner) for role in ROLES for owner in ("self", "other")]


@pytest.mark.parametrize("role,owner", MATRIX)
def test_a_curator_sees_only_their_own_unprivileged_rows(client, curator, second_curator, db,
                                                         role, owner):
    creator = curator[0]["id"] if owner == "self" else second_curator[0]["id"]
    _plant(db, "MATRICA", role=role, created_by=creator)

    visible = "MATRICA" in _codes(client, curator[1])
    assert visible is (owner == "self" and role in MINTABLE_BY_CURATOR)


@pytest.mark.parametrize("role,owner", MATRIX)
def test_an_admin_sees_every_cell_of_the_same_matrix(client, admin, curator, second_curator, db,
                                                     role, owner):
    creator = curator[0]["id"] if owner == "self" else second_curator[0]["id"]
    _plant(db, "MATRICA", role=role, created_by=creator)

    assert "MATRICA" in _codes(client, admin[1])


def test_a_privileged_code_string_never_appears_anywhere_in_a_curators_body(client, curator, db):
    _plant(db, "SLAPTAADMIN", role="admin", created_by=curator[0]["id"])

    response = client.get(INVITATIONS, headers=curator[1])
    assert b"SLAPTAADMIN" not in response.data








# -----------------------------------------------------------
# delete_invitation — the same matrix, cell by cell
# -----------------------------------------------------------

@pytest.mark.parametrize("role,owner", MATRIX)
def test_a_curator_revokes_only_their_own_unprivileged_rows(client, curator, second_curator, db,
                                                            role, owner):
    creator = curator[0]["id"] if owner == "self" else second_curator[0]["id"]
    code_id = _plant(db, "MATRICA", role=role, created_by=creator)

    response = client.delete(f"{INVITATIONS}/{code_id}", headers=curator[1])
    allowed = owner == "self" and role in MINTABLE_BY_CURATOR

    assert response.status_code == (200 if allowed else 404)
    remaining = db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE id = ?",
                           (code_id,)).fetchone()["c"]
    assert remaining == (0 if allowed else 1)


@pytest.mark.parametrize("role,owner", MATRIX)
def test_an_admin_revokes_every_cell_of_the_same_matrix(client, admin, curator, second_curator, db,
                                                        role, owner):
    creator = curator[0]["id"] if owner == "self" else second_curator[0]["id"]
    code_id = _plant(db, "MATRICA", role=role, created_by=creator)

    response = client.delete(f"{INVITATIONS}/{code_id}", headers=admin[1])
    assert response.status_code == 200
    assert response.get_json() == {"message": "Invitation deleted"}


def test_a_refused_revoke_tells_a_curator_nothing_the_missing_row_would_not(client, curator, admin, db):
    hidden = _plant(db, "SVETIMAS", role="admin", created_by=admin[0]["id"])

    refused = client.delete(f"{INVITATIONS}/{hidden}", headers=curator[1])
    unknown = client.delete(f"{INVITATIONS}/{uuid.uuid4()}", headers=curator[1])

    assert refused.status_code == unknown.status_code == 404
    assert refused.get_json() == unknown.get_json() == {"error": "Invitation not found"}




# -----------------------------------------------------------
# delete_invitation — what counts as an id
# -----------------------------------------------------------

def test_revoking_by_the_code_string_instead_of_the_id_finds_nothing(client, admin, db):
    _plant(db, "KODASNEID", created_by=admin[0]["id"])

    response = client.delete(f"{INVITATIONS}/KODASNEID", headers=admin[1])
    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes WHERE code = 'KODASNEID'"
                      ).fetchone()["c"] == 1


def test_an_id_in_the_wrong_case_matches_nothing(client, admin, db):
    code_id = _plant(db, "DIDZIOSIOS", created_by=admin[0]["id"])

    response = client.delete(f"{INVITATIONS}/{code_id.upper()}", headers=admin[1])
    assert response.status_code == 404


@pytest.mark.parametrize("code_id", ["..", "0", "-1", "%20", "null", "undefined",
                                     "' OR '1'='1", "1 OR 1=1", "a" * 4000])
def test_a_hostile_or_nonsense_id_deletes_nothing_and_answers_404(client, admin, db, code_id):
    before = db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"]

    response = client.delete(f"{INVITATIONS}/{code_id}", headers=admin[1])
    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) AS c FROM invitation_codes").fetchone()["c"] == before


def test_the_collection_path_has_no_delete_verb(client, admin):
    response = client.delete(INVITATIONS, headers=admin[1])
    assert response.status_code == 405
    assert response.get_json() == {"error": "Method not allowed"}




# -----------------------------------------------------------
# delete_invitation — idempotency, isolation and the races a
# test can actually drive
# -----------------------------------------------------------

def test_revoking_twice_is_a_200_then_a_404(client, admin, db):
    code_id = _plant(db, "DUKART", created_by=admin[0]["id"])

    assert client.delete(f"{INVITATIONS}/{code_id}", headers=admin[1]).status_code == 200
    assert client.delete(f"{INVITATIONS}/{code_id}", headers=admin[1]).status_code == 404


def test_a_row_that_vanished_under_the_caller_is_a_404_not_a_crash(client, admin, db):
    code_id = _plant(db, "DINGES", created_by=admin[0]["id"])
    db.execute("DELETE FROM invitation_codes WHERE id = ?", (code_id,))
    db.commit()

    assert client.delete(f"{INVITATIONS}/{code_id}", headers=admin[1]).status_code == 404


def test_two_curators_racing_the_same_row_produce_one_winner(client, curator, second_curator, db,
                                                             admin):
    code_id = _plant(db, "LENKTYNES", created_by=curator[0]["id"])
    # The second curator does not own it, so their attempt is the same
    # 404 whether it arrives first or last
    assert client.delete(f"{INVITATIONS}/{code_id}", headers=second_curator[1]).status_code == 404
    assert client.delete(f"{INVITATIONS}/{code_id}", headers=curator[1]).status_code == 200
    assert client.delete(f"{INVITATIONS}/{code_id}", headers=admin[1]).status_code == 404


def test_revoking_one_row_leaves_its_neighbours_alone(client, admin, db):
    keep_first = _plant(db, "LIKS1", created_by=admin[0]["id"])
    doomed = _plant(db, "DINGS", created_by=admin[0]["id"])
    keep_second = _plant(db, "LIKS2", created_by=admin[0]["id"])

    assert client.delete(f"{INVITATIONS}/{doomed}", headers=admin[1]).status_code == 200

    remaining = {r["id"] for r in _rows(client, admin[1])}
    assert keep_first in remaining and keep_second in remaining
    assert doomed not in remaining


@pytest.mark.parametrize("state", ["expired", "exhausted"])
def test_a_dead_code_is_still_revocable(client, admin, db, state):
    if state == "expired":
        code_id = _plant(db, "NEBEGALIOJA", created_by=admin[0]["id"],
                         expires_at="2020-01-01T00:00:00+00:00")
    else:
        code_id = _plant(db, "NEBEGALIOJA", created_by=admin[0]["id"], use_count=9, max_uses=1)

    assert client.delete(f"{INVITATIONS}/{code_id}", headers=admin[1]).status_code == 200


def test_a_successful_revoke_writes_exactly_one_audit_row_naming_the_actor(client, curator, db):
    minted = _mint(client, curator[1], role="teacher").get_json()

    assert client.delete(f"{INVITATIONS}/{minted['id']}", headers=curator[1]).status_code == 200

    rows = _audit_rows(db, "invitation.revoke")
    assert len(rows) == 1
    assert rows[0]["actor_id"] == curator[0]["id"]
    assert rows[0]["target"] == minted["id"]
    assert rows[0]["payload"] is None


def test_a_404_revoke_writes_no_audit_row_at_all(client, admin, db):
    assert client.delete(f"{INVITATIONS}/{uuid.uuid4()}", headers=admin[1]).status_code == 404
    assert _audit_rows(db, "invitation.revoke") == []








# -----------------------------------------------------------
# The three routes against each other, and against register()
# -----------------------------------------------------------

@pytest.mark.contract
def test_the_minted_object_and_the_listed_object_are_the_same_object(client, admin, db):
    db.execute("DELETE FROM invitation_codes")
    db.commit()

    minted = _mint(client, admin[1], role="teacher", max_uses=4, expires_hours=72).get_json()
    listed = _rows(client, admin[1])

    assert listed == [minted]


def test_a_curator_sees_and_then_revokes_the_code_they_just_minted(client, curator):
    minted = _mint(client, curator[1], role="student").get_json()

    assert minted["code"] in _codes(client, curator[1])
    assert client.delete(f"{INVITATIONS}/{minted['id']}", headers=curator[1]).status_code == 200
    assert minted["code"] not in _codes(client, curator[1])


def test_a_revoked_code_can_no_longer_register_anyone(client, admin):
    minted = _mint(client, admin[1], role="teacher").get_json()
    assert client.delete(f"{INVITATIONS}/{minted['id']}", headers=admin[1]).status_code == 200

    response = _register_with(client, minted["code"], "vejas")
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_invalid"


def test_burning_a_single_use_code_flips_fully_used_on_the_listing(client, admin):
    minted = _mint(client, admin[1], role="teacher", max_uses=1).get_json()
    assert _register_with(client, minted["code"], "ausra").status_code == 201

    listed = next(r for r in _rows(client, admin[1]) if r["id"] == minted["id"])
    assert listed["useCount"] == 1
    assert listed["fullyUsed"] is True
    assert listed["expired"] is False


def test_the_listing_and_register_agree_the_moment_a_code_expires(client, admin):
    minted = _mint(client, admin[1], role="teacher", expires_hours=1).get_json()

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(hours=2), tick=False):
        listed = next(r for r in _rows(client, admin[1]) if r["id"] == minted["id"])
        assert listed["expired"] is True

        response = _register_with(client, minted["code"], "ruta")
        assert response.status_code == 400
        assert response.get_json()["code"] == "invite_expired"


def test_a_code_with_an_unparsable_expiry_is_refused_by_both_halves(client, admin, db):
    _plant(db, "SUGADINTA", role="teacher", created_by=admin[0]["id"], expires_at="ne data")

    listed = next(r for r in _rows(client, admin[1]) if r["code"] == "SUGADINTA")
    assert listed["expired"] is True

    response = _register_with(client, "SUGADINTA", "gintare")
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_expired"


def test_a_minted_code_registers_the_role_it_was_minted_for(client, curator, db):
    minted = _mint(client, curator[1], role="teacher").get_json()

    assert _register_with(client, minted["code"], "jonas").status_code == 201
    row = db.execute("SELECT role, invited FROM users WHERE username = 'jonas'").fetchone()
    assert row["role"] == "teacher"
    assert row["invited"] == 1
