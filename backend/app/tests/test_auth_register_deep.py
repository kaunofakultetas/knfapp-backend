# -----------------------------------------------------------
#  [*] Tests — the registration slice, exhaustively
#
#  A gap-closing pass over exactly three functions of
#  app/auth/routes.py:
#
#    validate_invitation_code  — POST /api/auth/validate-code
#    register                  — POST /api/auth/register
#    _validate_new_password    — the shared password policy
#
#  test_auth_register.py already proves the headline
#  behaviours; this module goes after what a line-coverage
#  report cannot see — the ORDER the guards fire in, the
#  falsy-versus-mistyped split, the exact instants either
#  side of a boundary, the arms of each `except`, and the
#  values a caller can actually put on the wire. What it
#  proves:
#
#    - the password policy as a UNIT, including the argument
#      shapes only change_password can hand it (a None
#      username, a None email) and every entry of the
#      embedded common-password list.
#    - the policy's decision order: length, then username,
#      then email, then the common list — a password failing
#      two rules is named by the first.
#    - the common-password screen is an EXACT match, not a
#      substring: "password1234" is allowed and
#      "slaptazodis123" (the suite's own fixture password)
#      is not screened out.
#    - validate-code's boundaries: a zero-use code, the last
#      remaining use, an expiry landing on this very instant,
#      an expiry stored as a NUMBER (the TypeError arm of the
#      parse, where the sibling file drives the ValueError
#      arm) and expiries carrying a non-UTC offset.
#    - validate-code answers 200 without writing ANYTHING —
#      no burn, no row, on any of its four outcomes.
#    - register's guard order across every pair of
#      simultaneous faults, and that a falsy value of any
#      type is reported MISSING while a truthy one of the
#      wrong type is reported MISTYPED — except
#      invitation_code, where the same falsy value is a
#      type error.
#    - what a body CANNOT do: an extra "role"/"invited"/
#      "active"/"id" key never reaches the INSERT.
#    - the wire itself (TESTPLAN rule 10): NUL bytes are
#      stripped before the guards run, markup is stored raw
#      and escaped only on the way out, and Lithuanian
#      letters survive untouched.
#    - the invitation burn is discarded by the INSERT's own
#      IntegrityError, not only by the uniqueness pre-check.
#    - the per-IP buckets: probed before the body is read,
#      never extended by a call they reject, keyed off the
#      ProxyFix-resolved client, and one bucket per route.
#    - where validate-code and register must AGREE. The
#      register burn reads expires_at through julianday()
#      rather than as text, so a code stored with a non-UTC
#      offset means the same instant to both routes: what
#      the register screen ticks green, the submit takes.
# -----------------------------------------------------------

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
import time_machine

from app.auth.routes import (
    _COMMON_PASSWORDS,
    _PASSWORD_MAX_BYTES,
    ROLES,
    _rate_limit_store,
    _record_attempt,
    _validate_new_password,
)

REGISTER = "/api/auth/register"
VALIDATE = "/api/auth/validate-code"
ME = "/api/auth/me"

# The two buckets this module fills by hand; the test client's
# ProxyFix-resolved address is 127.0.0.1
REGISTER_KEY = "register:127.0.0.1"
VALIDATE_KEY = "validate:127.0.0.1"




# -----------------------------------------------------------
# clean_rate_limits
# -----------------------------------------------------------
#
# The limiter store is MODULE state — it outlives the `app`
# fixture, so one test's spent attempts (and the app's global
# 600-per-IP budget) would leak into the next. Cleared on both
# sides of every test.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_rate_limits():
    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _body
# -----------------------------------------------------------
#
# A registration body that clears every guard, tagged unique
# so two calls in one test never collide. An override of None
# is how a test sends an explicit JSON null.
#
# Used by:
#   - the register tests below
# -----------------------------------------------------------

def _body(**overrides):
    tag = uuid.uuid4().hex[:8]
    body = {
        "username": f"gilus_{tag}",
        "password": "ZaliaPieva42",
        "display_name": "Gilus Testas",
        "email": f"gilus_{tag}@knf.vu.lt",
    }
    body.update(overrides)
    return body




# -----------------------------------------------------------
# _raw
# -----------------------------------------------------------
#
# TESTPLAN rule 10: `json=` is serialised through the app's
# OWN escaping JSON provider, so a body containing markup,
# quotes or NUL reaches the route ALREADY escaped — which no
# real client sends. Every test whose point is what sits on
# the wire posts through here instead.
#
# Used by:
#   - the escaping, NUL and SQL-metacharacter tests below
# -----------------------------------------------------------

def _raw(client, path, payload, **kwargs):
    return client.post(path, data=json.dumps(payload),
                       headers={"Content-Type": "application/json"}, **kwargs)




# -----------------------------------------------------------
# _insert_code / _use_count
# -----------------------------------------------------------
#
# Plants one invitation_codes row in whatever state a test
# needs. created_by stays NULL (nullable FK), so no admin has
# to exist first, and expires_at is written VERBATIM — the
# expiry-parsing tests depend on that.
#
# Used by:
#   - the invitation-code tests below
# -----------------------------------------------------------

def _insert_code(db, code, role="student", max_uses=1, use_count=0, expires_at=None):
    if expires_at is None:
        expires_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    db.execute(
        "INSERT INTO invitation_codes (id, code, role, max_uses, use_count, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), code, role, max_uses, use_count, expires_at),
    )
    db.commit()
    return code


def _use_count(db, code):
    row = db.execute("SELECT use_count FROM invitation_codes WHERE code = ?", (code,)).fetchone()
    return row["use_count"]


def _fill(key, attempts):
    for _ in range(attempts):
        _record_attempt(key)




# -----------------------------------------------------------
# _race_before_the_burn
# -----------------------------------------------------------
#
# Fires one statement from a SECOND connection in the hairline
# between register's invitation pre-checks and its conditional
# UPDATE. The route stamps that UPDATE with utc_now_iso() and
# that is the request's FIRST call to it, so wrapping the name
# register imported gives a deterministic racer where real
# threads would not — and the racer commits before the route
# takes any write lock of its own, so nothing deadlocks.
#
# Used by:
#   - the rowcount-0 tests below
# -----------------------------------------------------------

def _race_before_the_burn(monkeypatch, app, sql, params=()):
    import app.auth.routes as routes

    real_stamp = routes.utc_now_iso
    fired = []

    def _stamp():
        if not fired:
            fired.append(True)
            racer = sqlite3.connect(app.config["DB_PATH"], timeout=15)
            try:
                racer.execute(sql, params)
                racer.commit()
            finally:
                racer.close()
        return real_stamp()

    monkeypatch.setattr(routes, "utc_now_iso", _stamp)




# -----------------------------------------------------------
# the password policy, as a unit
#
# register only ever hands it three non-empty strings;
# change_password can hand it a None username or email, and
# nothing but a direct call can hand it the empty password
# the route's own presence check swallows first.
# -----------------------------------------------------------


@pytest.mark.parametrize("password", ["", "a", "ab", "abcde"])
def test_a_password_under_six_characters_is_refused(password):
    assert _validate_new_password(password, "jonas", "kitas@knf.vu.lt") == \
        "Password must be at least 6 characters"


def test_six_characters_is_the_floor_not_the_first_rejection():
    assert _validate_new_password("Zx9qPl", "jonas", "kitas@knf.vu.lt") is None


def test_the_byte_ceiling_is_exactly_seventy_two():
    assert _PASSWORD_MAX_BYTES == 72
    assert _validate_new_password("Q" * 72, "jonas", "kitas@knf.vu.lt") is None
    assert _validate_new_password("Q" * 73, "jonas", "kitas@knf.vu.lt") == \
        "Password must be at most 72 characters"


def test_the_ceiling_counts_bytes_even_for_four_byte_characters():
    # bcrypt truncates at 72 BYTES; 19 emoji are 76 of them
    at_cap = "\N{GRINNING FACE}" * 18
    assert len(at_cap) == 18 and len(at_cap.encode()) == 72
    assert _validate_new_password(at_cap, "jonas", "kitas@knf.vu.lt") is None

    over_cap = "\N{GRINNING FACE}" * 19
    assert len(over_cap.encode()) == 76
    assert _validate_new_password(over_cap, "jonas", "kitas@knf.vu.lt") == \
        "Password must be at most 72 characters"


def test_a_falsy_username_skips_the_containment_screen():
    # change_password reads the username off request.user, which a
    # partial row can leave absent — the screen must simply not run
    assert _validate_new_password("PerkunasIrLietus", None, "kitas@knf.vu.lt") is None
    assert _validate_new_password("PerkunasIrLietus", "", "kitas@knf.vu.lt") is None


def test_a_falsy_email_never_raises_on_the_local_part():
    assert _validate_new_password("PerkunasIrLietus", "jonas", None) is None
    assert _validate_new_password("PerkunasIrLietus", "jonas", "") is None


def test_both_identity_screens_can_be_absent_at_once():
    assert _validate_new_password("PerkunasIrLietus", None, None) is None


@pytest.mark.parametrize("password", [
    "jonas123456",        # prefix
    "123456jonas",        # suffix
    "xxJONASxx9",         # infix, upper case
    "JoNaS-diena",        # mixed case
])
def test_the_username_screen_is_case_insensitive_and_positional(password):
    assert _validate_new_password(password, "Jonas", "kitas@knf.vu.lt") == \
        "Password must not contain your username"


def test_a_username_longer_than_the_password_cannot_be_contained():
    assert _validate_new_password("Perkun", "Perkunasvyras", "kitas@knf.vu.lt") is None


def test_the_email_local_part_screen_starts_at_three_characters():
    assert _validate_new_password("xxABCxx9", "jonas", "abc@knf.vu.lt") == \
        "Password must not contain your email"
    # two characters is far too noisy a screen to enforce
    assert _validate_new_password("xxABxx99", "jonas", "ab@knf.vu.lt") is None


def test_the_email_screen_is_case_insensitive_on_both_sides():
    assert _validate_new_password("xxstudentasxx", "jonas", "STUDENTAS@knf.vu.lt") == \
        "Password must not contain your email"


def test_only_the_local_part_is_screened_never_the_domain():
    assert _validate_new_password("xxKNFVUxx9", "jonas", "kitas@knf.vu.lt") is None


def test_an_email_without_an_at_sign_screens_its_whole_value():
    # split("@", 1)[0] of a domainless value is the value itself —
    # register cannot produce one, change_password reads whatever
    # the row holds
    assert _validate_new_password("xxSLAPTUKASxx", "jonas", "slaptukas") == \
        "Password must not contain your email"


def test_the_length_checks_run_before_the_containment_screens():
    assert _validate_new_password("jonas", "jonas", "jonas@knf.vu.lt") == \
        "Password must be at least 6 characters"
    assert _validate_new_password("jonas" + "Q" * 68, "jonas", "jonas@knf.vu.lt") == \
        "Password must be at most 72 characters"


def test_the_username_screen_runs_before_the_email_screen():
    assert _validate_new_password("jonasabcdef", "jonas", "abcdef@knf.vu.lt") == \
        "Password must not contain your username"


def test_the_identity_screens_run_before_the_common_list():
    assert _validate_new_password("password", "password", "kitas@knf.vu.lt") == \
        "Password must not contain your username"
    assert _validate_new_password("iloveyou", "jonas", "iloveyou@knf.vu.lt") == \
        "Password must not contain your email"


def test_every_embedded_common_password_is_refused():
    for common in sorted(_COMMON_PASSWORDS):
        assert _validate_new_password(common, "jonas", "kitas@knf.vu.lt") == \
            "Password is too common", common


def test_no_entry_of_the_common_list_is_shadowed_by_the_length_floor():
    # An entry under 6 characters could never be reached — the
    # floor answers first — so the list would be quietly lying
    assert all(len(entry) >= 6 for entry in _COMMON_PASSWORDS)
    assert all(len(entry.encode()) <= _PASSWORD_MAX_BYTES for entry in _COMMON_PASSWORDS)
    assert all(entry == entry.lower() for entry in _COMMON_PASSWORDS)


@pytest.mark.parametrize("password", ["PASSWORD", "PaSsWoRd", "AbC123", "LetMeIn"])
def test_the_common_list_is_matched_lowercased(password):
    assert _validate_new_password(password, "jonas", "kitas@knf.vu.lt") == "Password is too common"


def test_the_common_list_is_an_exact_match_not_a_substring():
    assert _validate_new_password("password1234", "jonas", "kitas@knf.vu.lt") is None
    # the suite's own fixture password — "slaptazodis" IS listed
    assert _validate_new_password("slaptazodis123", "jonas", "kitas@knf.vu.lt") is None


def test_the_policy_has_no_charset_rule_at_all():
    # Documented weak spot: six spaces clear every screen. Raising
    # the bar is a human call (it would lock live habits out)
    assert _validate_new_password("      ", "jonas", "kitas@knf.vu.lt") is None
    assert _validate_new_password("\N{GRINNING FACE}" * 6, "jonas", "kitas@knf.vu.lt") is None




# -----------------------------------------------------------
# validate-code — the boundaries either side of each guard
# -----------------------------------------------------------


def test_a_code_with_no_uses_at_all_is_exhausted_from_birth(client, db):
    _insert_code(db, "NULINIS", max_uses=0)

    body = client.post(VALIDATE, json={"code": "NULINIS"}).get_json()
    assert body["valid"] is False
    assert body["reason"] == "exhausted"


def test_the_last_remaining_use_still_validates(client, db):
    _insert_code(db, "PASKUTINE", max_uses=3, use_count=2)

    body = client.post(VALIDATE, json={"code": "PASKUTINE"}).get_json()
    assert body["valid"] is True
    assert body["remainingUses"] == 1


def test_one_use_past_the_cap_is_exhausted_not_negative(client, db):
    _insert_code(db, "PERSPAUSTA", max_uses=3, use_count=3)

    assert client.post(VALIDATE, json={"code": "PERSPAUSTA"}).get_json()["reason"] == "exhausted"


def test_an_expiry_landing_on_this_very_instant_still_validates(client, db):
    # `invite_expires < now` is strict, so equality is still valid —
    # the hairline register's `expires_at > ?` burn disagrees on
    frozen = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _insert_code(db, "RIBINE", expires_at=frozen.isoformat())

    with time_machine.travel(frozen, tick=False):
        assert client.post(VALIDATE, json={"code": "RIBINE"}).get_json()["valid"] is True


def test_one_microsecond_past_the_expiry_is_expired(client, db):
    frozen = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _insert_code(db, "MIKRO", expires_at=(frozen - timedelta(microseconds=1)).isoformat())

    with time_machine.travel(frozen, tick=False):
        assert client.post(VALIDATE, json={"code": "MIKRO"}).get_json()["reason"] == "expired"


def test_an_expiry_stored_as_a_number_counts_as_expired(client, db):
    # SQLite's dynamic typing lets an INTEGER into the TEXT column;
    # fromisoformat raises TypeError there, not ValueError — the
    # other arm of the same except
    _insert_code(db, "SKAICIUS", expires_at=1767225600)

    response = client.post(VALIDATE, json={"code": "SKAICIUS"})
    assert response.status_code == 200
    assert response.get_json()["reason"] == "expired"


def test_an_offset_less_expiry_is_read_as_utc_not_as_local_time(client, db):
    # A legacy row carries no offset at all; assuming UTC is what
    # keeps the answer the same wherever the container runs
    frozen = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    _insert_code(db, "NAIVI-BUVUSI", expires_at="2026-06-15T11:59:59")
    _insert_code(db, "NAIVI-BUSIMA", expires_at="2026-06-15T12:00:01")

    with time_machine.travel(frozen, tick=False):
        assert client.post(VALIDATE, json={"code": "NAIVI-BUVUSI"}).get_json()["reason"] == "expired"
        assert client.post(VALIDATE, json={"code": "NAIVI-BUSIMA"}).get_json()["valid"] is True


def test_a_future_expiry_written_in_a_foreign_offset_is_valid(client, db):
    # 09:00-05:00 is 14:00 UTC — two hours away, however the text sorts
    frozen = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    _insert_code(db, "OFSETAS-PLIUS", expires_at="2026-06-15T09:00:00-05:00")

    with time_machine.travel(frozen, tick=False):
        assert client.post(VALIDATE, json={"code": "OFSETAS-PLIUS"}).get_json()["valid"] is True


def test_a_past_expiry_written_in_a_foreign_offset_is_expired(client, db):
    # 14:00+03:00 is 11:00 UTC — an hour GONE, though the text reads later
    frozen = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    _insert_code(db, "OFSETAS-MINUS", expires_at="2026-06-15T14:00:00+03:00")

    with time_machine.travel(frozen, tick=False):
        assert client.post(VALIDATE, json={"code": "OFSETAS-MINUS"}).get_json()["reason"] == "expired"


@pytest.mark.parametrize("role", ROLES)
def test_every_role_a_code_can_carry_is_reported_back(client, db, role):
    _insert_code(db, f"ROLE-{role.upper()}", role=role)

    body = client.post(VALIDATE, json={"code": f"ROLE-{role.upper()}"}).get_json()
    assert body["valid"] is True
    assert body["role"] == role


def test_the_valid_answer_carries_exactly_three_keys(client, seeded_code):
    body = client.post(VALIDATE, json={"code": seeded_code}).get_json()

    assert set(body) == {"valid", "role", "remainingUses"}


@pytest.mark.parametrize("code,reason", [("NEZINOMAS", "unknown"),
                                         ("ISNAUDOTAS", "exhausted"),
                                         ("PASIBAIGES", "expired")])
def test_every_invalid_answer_carries_exactly_four_keys(client, db, code, reason):
    _insert_code(db, "ISNAUDOTAS", max_uses=1, use_count=1)
    _insert_code(db, "PASIBAIGES",
                 expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())

    body = client.post(VALIDATE, json={"code": code}).get_json()
    assert set(body) == {"valid", "error", "code", "reason"}
    assert body["valid"] is False
    assert body["reason"] == reason


def test_validate_code_never_writes_anything_on_any_outcome(client, db, seeded_code):
    _insert_code(db, "SUNAUDOTAS", max_uses=2, use_count=2)
    _insert_code(db, "SENAS", expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    before = db.execute("SELECT code, use_count, expires_at FROM invitation_codes ORDER BY code").fetchall()

    for code in (seeded_code, "SUNAUDOTAS", "SENAS", "VISAI-NEZINOMAS"):
        assert client.post(VALIDATE, json={"code": code}).status_code == 200

    after = db.execute("SELECT code, use_count, expires_at FROM invitation_codes ORDER BY code").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1  # the seeded admin alone


def test_a_ten_thousand_character_code_is_simply_unknown(client):
    response = client.post(VALIDATE, json={"code": "A" * 10_000})

    assert response.status_code == 200
    assert response.get_json()["reason"] == "unknown"


def test_a_code_of_sql_metacharacters_is_parameterised_not_interpolated(client, db, seeded_code):
    # Raw bytes: the escaping provider would turn the quote into an
    # entity and the payload would never reach the query as typed
    response = _raw(client, VALIDATE, {"code": "' OR 1=1 --"})

    assert response.status_code == 200
    assert response.get_json()["reason"] == "unknown"
    assert db.execute("SELECT COUNT(*) c FROM invitation_codes").fetchone()["c"] == 1
    assert client.post(VALIDATE, json={"code": seeded_code}).get_json()["valid"] is True


def test_a_unicode_code_is_unknown_and_never_a_crash(client):
    assert client.post(VALIDATE, json={"code": "KODAS-ĄŽUOLAS-\N{GRINNING FACE}"}).get_json()["reason"] == "unknown"


def test_nul_bytes_are_stripped_out_of_the_code_before_the_lookup(client, db):
    _insert_code(db, "SVARUS")

    response = _raw(client, VALIDATE, {"code": "SVA\x00RUS"})
    assert response.status_code == 200
    assert response.get_json()["valid"] is True




# -----------------------------------------------------------
# validate-code — body and transport guards
# -----------------------------------------------------------


@pytest.mark.parametrize("code", [0, 0.0, False, [], {}])
def test_a_falsy_non_string_code_is_reported_missing_not_mistyped(client, code):
    # `not data.get("code")` fires before the isinstance check, so
    # the type error is unreachable for any falsy value
    response = client.post(VALIDATE, json={"code": code})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Code required"


@pytest.mark.parametrize("code", [1.5, -1, [""], {"a": 1}])
def test_a_truthy_non_string_code_is_refused_by_type(client, code):
    response = client.post(VALIDATE, json={"code": code})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Code must be a string"


def test_a_form_encoded_body_reads_as_no_code_at_all(client, seeded_code):
    response = client.post(VALIDATE, data={"code": seeded_code})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Code required"


def test_a_body_less_post_reads_as_no_code_at_all(client):
    response = client.post(VALIDATE)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Code required"


def test_validate_code_answers_only_to_post(client, seeded_code):
    assert client.get(VALIDATE).status_code == 405
    assert client.put(VALIDATE, json={"code": seeded_code}).status_code == 405
    assert client.delete(VALIDATE).status_code == 405




# -----------------------------------------------------------
# validate-code — the per-IP budget
# -----------------------------------------------------------


def test_the_validate_budget_is_probed_before_the_body_is_read(client):
    _fill(VALIDATE_KEY, 30)

    response = client.post(VALIDATE, json={"nothing": "at all"})
    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_the_validate_429_carries_a_retry_after_inside_the_window(client):
    _fill(VALIDATE_KEY, 30)

    response = client.post(VALIDATE, json={"code": "BETKAS"})
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 301


def test_a_rejected_validate_call_never_extends_its_own_window(client):
    _fill(VALIDATE_KEY, 30)

    for _ in range(5):
        assert client.post(VALIDATE, json={"code": "BETKAS"}).status_code == 429

    assert len(_rate_limit_store[VALIDATE_KEY]) == 30


def test_the_validate_bucket_follows_the_proxy_resolved_client(client, seeded_code):
    _fill("validate:10.0.0.7", 30)

    blocked = client.post(VALIDATE, json={"code": seeded_code},
                          headers={"X-Forwarded-For": "10.0.0.7"})
    assert blocked.status_code == 429

    neighbour = client.post(VALIDATE, json={"code": seeded_code},
                            headers={"X-Forwarded-For": "10.0.0.8"})
    assert neighbour.status_code == 200
    assert neighbour.get_json()["valid"] is True


def test_an_unresolvable_client_shares_one_fallback_bucket(client, seeded_code):
    response = client.post(VALIDATE, json={"code": seeded_code}, environ_base={"REMOTE_ADDR": ""})

    assert response.status_code == 200
    assert "validate:unknown" in _rate_limit_store


def test_the_validate_bucket_is_not_the_register_bucket(client, seeded_code):
    _fill(REGISTER_KEY, 10)

    # register is spent, validate untouched
    assert client.post(REGISTER, json=_body()).status_code == 429
    assert client.post(VALIDATE, json={"code": seeded_code}).status_code == 200




# -----------------------------------------------------------
# register — which guard answers first
#
# Every test here sends a body with TWO faults and pins which
# one is named. The order is the contract the register screen
# highlights fields by.
# -----------------------------------------------------------


def test_an_empty_object_body_is_reported_as_no_body_at_all(client):
    # `if not data` catches {} before the missing-field list runs
    response = client.post(REGISTER, json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_body_that_is_not_json_at_all_is_reported_as_no_body(client):
    response = client.post(REGISTER, data="naujokas", content_type="text/plain")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_body_less_register_is_reported_as_no_body(client):
    assert client.post(REGISTER).get_json()["error"] == "JSON body required"


def test_every_missing_field_is_named_in_the_required_order(client):
    response = client.post(REGISTER, json={"unrelated": "x"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Missing fields: username, password, display_name, email"


@pytest.mark.parametrize("value", ["", 0, 0.0, False, [], {}])
@pytest.mark.parametrize("field", ["username", "password", "display_name", "email"])
def test_a_falsy_value_of_any_type_is_reported_missing(client, field, value):
    response = client.post(REGISTER, json=_body(**{field: value}))

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Missing fields: {field}"


def test_an_explicit_null_is_reported_missing_too(client):
    response = client.post(REGISTER, json=_body(email=None))

    assert response.get_json()["error"] == "Missing fields: email"


def test_presence_is_checked_before_type(client):
    response = client.post(REGISTER, json=_body(username="", password=12345))

    assert response.get_json()["error"] == "Missing fields: username"


def test_the_type_check_names_the_first_bad_field_in_the_required_order(client):
    response = client.post(REGISTER, json=_body(password=1, email=2))

    assert response.status_code == 400
    assert response.get_json()["error"] == "password must be a string"


def test_the_username_charset_is_checked_before_the_email_shape(client):
    response = client.post(REGISTER, json=_body(username="per trumpas!", email="ne-elpastas"))

    assert response.get_json()["code"] == "invalid_username"


def test_the_email_shape_is_checked_before_the_password_policy(client):
    response = client.post(REGISTER, json=_body(email="ne-elpastas", password="123"))

    assert response.get_json()["code"] == "invalid_email"


def test_the_password_policy_is_checked_before_the_display_name(client):
    response = client.post(REGISTER, json=_body(password="12345", display_name="   "))

    assert response.get_json()["code"] == "weak_password"


def test_the_display_name_guards_run_before_the_invitation_code(client):
    response = client.post(REGISTER, json=_body(display_name="n" * 101,
                                                invitation_code="NERA-TOKIO"))

    assert response.get_json()["error"] == "Display name must be at most 100 characters"


def test_the_invitation_code_type_is_checked_before_the_lookup(client, make_user):
    # ...and before the uniqueness pre-check, so a duplicate
    # username with a mistyped code still names the code
    taken = make_user(username="uzimtas")
    response = client.post(REGISTER, json=_body(username=taken["username"], invitation_code=7))

    assert response.status_code == 400
    assert response.get_json()["error"] == "invitation_code must be a string"


@pytest.mark.parametrize("code", [0, False, [], {}, 1.5, ["A"]])
def test_a_falsy_invitation_code_of_the_wrong_type_is_still_a_type_error(client, code):
    # The mirror image of the required fields: here `is not None`
    # decides, so 0 and False are type errors, not "absent"
    response = client.post(REGISTER, json=_body(invitation_code=code))

    assert response.status_code == 400
    assert response.get_json()["error"] == "invitation_code must be a string"


def test_register_answers_only_to_post(client):
    assert client.get(REGISTER).status_code == 405
    assert client.put(REGISTER, json=_body()).status_code == 405




# -----------------------------------------------------------
# register — caps, canonicalisation and what the body cannot do
# -----------------------------------------------------------


def test_the_email_cap_is_measured_after_the_trim(client, db):
    at_cap = "a" * (254 - len("@knf.vu.lt")) + "@knf.vu.lt"
    assert len(at_cap) == 254

    response = client.post(REGISTER, json=_body(email=f"   {at_cap}   "))
    assert response.status_code == 201
    assert response.get_json()["user"]["email"] == at_cap


def test_an_email_is_canonicalised_before_it_is_shape_checked(client):
    # Upper case would still match the regex, but the stored value
    # is the lowered one and that is what the cap measured
    response = client.post(REGISTER, json=_body(email="  NAUJAS.Vardas@KNF.VU.LT "))

    assert response.status_code == 201
    assert response.get_json()["user"]["email"] == "naujas.vardas@knf.vu.lt"


@pytest.mark.parametrize("email", ["a@b.c", "a.b-c_d+e@sub.knf.vu.lt", "ĄŽ@knf.vu.lt"])
def test_the_email_shape_check_is_deliberately_permissive(client, email):
    # Everything without an @, a space or a dotless domain passes —
    # the app must not out-guess RFC 5322 on a live sign-up form
    assert client.post(REGISTER, json=_body(email=email)).status_code == 201


def test_a_display_name_stripped_to_one_character_is_accepted(client, db):
    response = client.post(REGISTER, json=_body(display_name="  Ą  "))

    assert response.status_code == 201
    assert response.get_json()["user"]["displayName"] == "Ą"


@pytest.mark.parametrize("blank", ["\n", "\t\t", "\r\n ", " " * 3])
def test_a_display_name_of_only_whitespace_is_refused_whatever_the_whitespace(client, blank):
    response = client.post(REGISTER, json=_body(display_name=blank))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name cannot be empty"


def test_a_hundred_thousand_character_password_is_a_policy_400(client):
    response = client.post(REGISTER, json=_body(password="Q" * 100_000))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Password must be at most 72 characters"


def test_a_hundred_thousand_character_username_is_a_charset_400(client):
    response = client.post(REGISTER, json=_body(username="q" * 100_000))

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_username"


def test_an_extra_body_key_can_never_set_the_role_or_the_trust_flag(client, db):
    body = _body()
    body.update({"role": "admin", "invited": 1, "active": 0, "id": "pasirinktas-id"})

    response = client.post(REGISTER, json=body)
    assert response.status_code == 201
    assert response.get_json()["user"]["role"] == "student"
    assert response.get_json()["user"]["invited"] is False
    assert response.get_json()["user"]["id"] != "pasirinktas-id"

    row = db.execute("SELECT role, invited, active FROM users WHERE username = ?",
                     (body["username"],)).fetchone()
    assert (row["role"], row["invited"], row["active"]) == ("student", 0, 1)


def test_a_privileged_invitation_code_really_does_mint_a_privileged_account(client, db):
    _insert_code(db, "KURATORIUS", role="curator")

    response = client.post(REGISTER, json=_body(invitation_code="KURATORIUS"))
    assert response.status_code == 201
    assert response.get_json()["user"]["role"] == "curator"

    headers = {"Authorization": f"Bearer {response.get_json()['token']}"}
    assert client.get(ME, headers=headers).get_json()["role"] == "curator"




# -----------------------------------------------------------
# register — what actually goes on the wire
#
# TESTPLAN rule 10: these post raw bytes, because `json=`
# would hand the route an already-escaped string.
# -----------------------------------------------------------


def test_markup_in_the_display_name_is_stored_raw_and_escaped_only_on_the_way_out(client, db):
    body = _body(display_name="Jonas <b>Vanagas</b>")

    response = _raw(client, REGISTER, body)
    assert response.status_code == 201
    assert response.get_json()["user"]["displayName"] == "Jonas &lt;b&gt;Vanagas&lt;/b&gt;"

    stored = db.execute("SELECT display_name FROM users WHERE username = ?",
                        (body["username"],)).fetchone()["display_name"]
    assert stored == "Jonas <b>Vanagas</b>", "escaping on input would double-escape every edit"


def test_lithuanian_letters_survive_registration_untouched(client, db):
    body = _body(display_name="Ąžuolas Šešėlis Ūkininkas")

    response = _raw(client, REGISTER, body)
    assert response.status_code == 201
    assert response.get_json()["user"]["displayName"] == "Ąžuolas Šešėlis Ūkininkas"

    stored = db.execute("SELECT display_name FROM users WHERE username = ?",
                        (body["username"],)).fetchone()["display_name"]
    assert stored == "Ąžuolas Šešėlis Ūkininkas"


def test_nul_bytes_are_stripped_before_the_username_charset_check(client, db):
    # Without the input scrub the NUL would fail the charset (and
    # would reach a TEXT column if it ever did not)
    body = _body(username="nu\x00linis_vardas")

    response = _raw(client, REGISTER, body)
    assert response.status_code == 201
    assert response.get_json()["user"]["username"] == "nulinis_vardas"
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = ?",
                      ("nulinis_vardas",)).fetchone()["c"] == 1


def test_a_password_may_carry_quotes_and_still_authenticate(client, db):
    # The escaping provider never touches what goes INTO bcrypt —
    # a password with an apostrophe must keep working on login
    body = _body(password="Ne'sakyk \"nieko\"")

    assert _raw(client, REGISTER, body).status_code == 201
    login = _raw(client, "/api/auth/login",
                 {"username": body["username"], "password": body["password"]})
    assert login.status_code == 200




# -----------------------------------------------------------
# register — the invitation code, boundary by boundary
# -----------------------------------------------------------


def test_a_zero_use_code_is_refused_as_exhausted(client, db):
    _insert_code(db, "NULIS-NAUDOJIMU", max_uses=0)

    response = client.post(REGISTER, json=_body(invitation_code="NULIS-NAUDOJIMU"))
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_exhausted"
    assert _use_count(db, "NULIS-NAUDOJIMU") == 0


def test_an_expiry_stored_as_a_number_is_refused_as_expired(client, db):
    _insert_code(db, "SKAITINE", expires_at=1767225600)

    response = client.post(REGISTER, json=_body(invitation_code="SKAITINE"))
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_expired"


def test_a_multi_use_code_serves_every_seat_and_then_stops(client, db):
    _insert_code(db, "TRYS-VIETOS", max_uses=3)

    for seat in range(3):
        assert client.post(REGISTER, json=_body(invitation_code="TRYS-VIETOS")).status_code == 201, seat
    assert _use_count(db, "TRYS-VIETOS") == 3

    spent = client.post(REGISTER, json=_body(invitation_code="TRYS-VIETOS"))
    assert spent.status_code == 400
    assert spent.get_json()["code"] == "invite_exhausted"
    assert _use_count(db, "TRYS-VIETOS") == 3


def test_a_stripped_code_burns_the_row_it_matched(client, db):
    _insert_code(db, "TARPAI", max_uses=2)

    assert client.post(REGISTER, json=_body(invitation_code="  TARPAI\t")).status_code == 201
    assert _use_count(db, "TARPAI") == 1


def test_a_code_expired_by_a_racer_between_the_check_and_the_burn(client, db, app, monkeypatch):
    # The pre-checks pass, then the world changes: only the
    # conditional UPDATE can still catch it, and the re-read names it
    _insert_code(db, "PASENO", max_uses=5)
    _race_before_the_burn(monkeypatch, app,
                          "UPDATE invitation_codes SET expires_at = ? WHERE code = ?",
                          ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "PASENO"))

    response = client.post(REGISTER, json=_body(invitation_code="PASENO"))
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_expired"
    assert _use_count(db, "PASENO") == 0


def test_a_code_deleted_by_a_racer_between_the_check_and_the_burn(client, db, app, monkeypatch):
    # rowcount 0 with the row GONE — the re-read finds nothing and
    # the answer falls all the way back to "unknown"
    _insert_code(db, "DINGO", max_uses=5)
    _race_before_the_burn(monkeypatch, app,
                          "DELETE FROM invitation_codes WHERE code = ?", ("DINGO",))
    body = _body(invitation_code="DINGO")

    response = client.post(REGISTER, json=body)
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_invalid"
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["c"] == 0


def test_a_last_seat_taken_by_a_racer_between_the_check_and_the_burn(client, db, app, monkeypatch):
    # rowcount 0 with the row still there and full — two callers
    # racing for one seat can never both be served
    _insert_code(db, "UZIMTA-VIETA", max_uses=2, use_count=1)
    _race_before_the_burn(monkeypatch, app,
                          "UPDATE invitation_codes SET use_count = max_uses WHERE code = ?",
                          ("UZIMTA-VIETA",))

    response = client.post(REGISTER, json=_body(invitation_code="UZIMTA-VIETA"))
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_exhausted"
    assert _use_count(db, "UZIMTA-VIETA") == 2, "the racer's use stands, ours never happened"


def test_a_naive_invite_expiry_is_read_as_utc_by_register(client, db):
    # No offset in the text: the parse assumes UTC rather than the
    # container's local zone, in both directions
    frozen = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    _insert_code(db, "NAIVI-SENA", expires_at="2026-06-15T11:00:00")
    _insert_code(db, "NAIVI-NAUJA", expires_at="2026-06-15T13:00:00")

    with time_machine.travel(frozen, tick=False):
        stale = client.post(REGISTER, json=_body(invitation_code="NAIVI-SENA"))
        fresh = client.post(REGISTER, json=_body(invitation_code="NAIVI-NAUJA"))

    assert stale.status_code == 400
    assert stale.get_json()["code"] == "invite_expired"
    assert fresh.status_code == 201
    assert _use_count(db, "NAIVI-NAUJA") == 1


def test_an_insert_race_discards_the_invitation_burn(client, db, make_user, monkeypatch):
    # The uniqueness pre-check is only the fast path; when the
    # INSERT itself raises, the burn must roll back with it
    twin = make_user(username="dvynys-gilus")
    _insert_code(db, "NESUDEGA", max_uses=1)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(twin["id"]))

    response = client.post(REGISTER, json=_body(invitation_code="NESUDEGA"))
    assert response.status_code == 409
    assert response.get_json()["code"] == "username_taken"

    monkeypatch.undo()
    assert _use_count(db, "NESUDEGA") == 0
    assert client.post(VALIDATE, json={"code": "NESUDEGA"}).get_json()["valid"] is True


def test_a_guest_registration_touches_no_invitation_row_at_all(client, db, seeded_code):
    assert client.post(REGISTER, json=_body()).status_code == 201

    assert _use_count(db, seeded_code) == 0




# -----------------------------------------------------------
# register — uniqueness scope, idempotency, persistence
# -----------------------------------------------------------


def test_a_repeated_registration_leaves_one_row_and_one_session(client, db):
    body = _body()
    assert client.post(REGISTER, json=body).status_code == 201

    repeat = client.post(REGISTER, json=body)
    assert repeat.status_code == 409
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["c"] == 1
    assert db.execute(
        "SELECT COUNT(*) c FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.username = ?",
        (body["username"],)).fetchone()["c"] == 1


def test_the_uniqueness_pre_check_never_crosses_the_two_columns(client, db, make_user):
    # It compares username-to-username and email-to-email only, so a
    # new EMAIL may equal a legacy email-shaped USERNAME. login
    # matches across both columns and tries each candidate's
    # password, so neither account becomes unreachable
    legacy = make_user(username="jonas@knf.vu.lt", password="SenasVartotojas9")
    body = _body(email="jonas@knf.vu.lt", password="NaujasVartotojas9")

    assert client.post(REGISTER, json=body).status_code == 201

    old = client.post("/api/auth/login", json={"username": "jonas@knf.vu.lt",
                                               "password": legacy["password"]})
    new = client.post("/api/auth/login", json={"username": "jonas@knf.vu.lt",
                                               "password": body["password"]})
    assert old.status_code == 200 and old.get_json()["user"]["id"] == legacy["id"]
    assert new.status_code == 200 and new.get_json()["user"]["username"] == body["username"]


def test_the_response_carries_exactly_a_user_and_a_token(client):
    payload = client.post(REGISTER, json=_body()).get_json()

    assert set(payload) == {"user", "token"}
    assert set(payload["user"]) == {"id", "username", "email", "displayName", "role",
                                    "avatarUrl", "invited", "studentNumber", "studyGroup",
                                    "studyProgram"}


def test_a_fresh_account_starts_with_no_avatar_and_no_student_card(client, db):
    body = _body()
    user = client.post(REGISTER, json=body).get_json()["user"]

    assert user["avatarUrl"] is None
    assert user["studentNumber"] is None
    assert user["studyGroup"] is None
    assert user["studyProgram"] is None

    row = db.execute("SELECT avatar_url, student_number, study_group, study_program, active"
                     " FROM users WHERE username = ?", (body["username"],)).fetchone()
    assert tuple(row) == (None, None, None, None, 1)


def test_the_returned_id_is_the_stored_row_and_a_uuid4(client, db):
    body = _body()
    user = client.post(REGISTER, json=body).get_json()["user"]

    assert uuid.UUID(user["id"]).version == 4
    stored = db.execute("SELECT id FROM users WHERE username = ?", (body["username"],)).fetchone()
    assert stored["id"] == user["id"]


def test_the_user_and_session_stamps_are_the_house_t_form(client, db):
    body = _body()
    client.post(REGISTER, json=body)

    row = db.execute(
        "SELECT u.created_at, u.updated_at, s.created_at AS s_created, s.expires_at"
        " FROM users u JOIN sessions s ON s.user_id = u.id WHERE u.username = ?",
        (body["username"],)).fetchone()

    for stamp in (row["created_at"], row["updated_at"], row["s_created"], row["expires_at"]):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00", stamp), stamp
        assert datetime.fromisoformat(stamp).tzinfo is not None
    assert row["created_at"] == row["updated_at"]


def test_the_session_id_is_a_uuid_the_token_hash_is_not(client, db):
    payload = client.post(REGISTER, json=_body()).get_json()

    row = db.execute("SELECT id, token FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()
    assert uuid.UUID(row["id"]).version == 4
    assert row["token"] != payload["token"]
    assert len(row["token"]) == 64


def test_the_stored_hash_verifies_the_password_that_was_sent(client, db):
    body = _body()
    client.post(REGISTER, json=body)

    stored = db.execute("SELECT password_hash FROM users WHERE username = ?",
                        (body["username"],)).fetchone()["password_hash"]
    assert bcrypt.checkpw(body["password"].encode(), stored.encode())
    assert not bcrypt.checkpw(b"kitas-slaptazodis", stored.encode())


def test_a_seventy_two_byte_password_survives_the_round_trip_to_login(client):
    body = _body(password="Q" * 71 + "z")

    assert client.post(REGISTER, json=body).status_code == 201
    login = client.post("/api/auth/login", json={"username": body["username"],
                                                 "password": body["password"]})
    assert login.status_code == 200




# -----------------------------------------------------------
# register — the per-IP budget
# -----------------------------------------------------------


def test_the_register_budget_is_probed_before_the_body_is_read(client):
    _fill(REGISTER_KEY, 10)

    response = client.post(REGISTER, json={"visiskai": "netinkamas"})
    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_a_rejected_registration_never_extends_its_own_window(client):
    _fill(REGISTER_KEY, 10)

    for _ in range(4):
        assert client.post(REGISTER, json=_body()).status_code == 429

    assert len(_rate_limit_store[REGISTER_KEY]) == 10


def test_a_throttled_registration_creates_nothing(client, db):
    _fill(REGISTER_KEY, 10)
    body = _body()

    assert client.post(REGISTER, json=body).status_code == 429
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0


def test_the_register_429_carries_a_retry_after_inside_the_window(client):
    _fill(REGISTER_KEY, 10)

    response = client.post(REGISTER, json=_body())
    assert 1 <= int(response.headers["Retry-After"]) <= 301


def test_a_body_that_validated_spends_budget_however_it_ends(client, db, make_user):
    # The attempt is recorded the moment the SHAPE is good — an
    # unknown code and a taken username both cost the caller a slot
    taken = make_user(username="jau-yra")

    assert client.post(REGISTER, json=_body(invitation_code="NERA-TOKIO")).status_code == 400
    assert client.post(REGISTER, json=_body(username=taken["username"])).status_code == 409

    assert len(_rate_limit_store[REGISTER_KEY]) == 2


def test_a_mistyped_invitation_code_spends_budget_the_missing_field_did_not(client):
    # invitation_code's type check sits AFTER the record point,
    # unlike every other type check in the handler
    assert client.post(REGISTER, json=_body(username="")).status_code == 400
    assert REGISTER_KEY not in _rate_limit_store

    assert client.post(REGISTER, json=_body(invitation_code=7)).status_code == 400
    assert len(_rate_limit_store[REGISTER_KEY]) == 1


def test_the_register_bucket_follows_the_proxy_resolved_client(client):
    _fill("register:203.0.113.9", 10)

    blocked = client.post(REGISTER, json=_body(), headers={"X-Forwarded-For": "203.0.113.9"})
    assert blocked.status_code == 429

    neighbour = client.post(REGISTER, json=_body(), headers={"X-Forwarded-For": "203.0.113.10"})
    assert neighbour.status_code == 201


def test_an_unresolvable_client_registers_under_the_fallback_bucket(client):
    response = client.post(REGISTER, json=_body(), environ_base={"REMOTE_ADDR": ""})

    assert response.status_code == 201
    assert "register:unknown" in _rate_limit_store


def test_the_budget_reopens_once_the_window_has_passed(client):
    _fill(REGISTER_KEY, 10)
    assert client.post(REGISTER, json=_body()).status_code == 429

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(seconds=301), tick=False):
        assert client.post(REGISTER, json=_body()).status_code == 201




# -----------------------------------------------------------
# the two routes, side by side
#
# The register screen validates a code and then submits it —
# an answer that flips between the two calls is a user staring
# at a green tick and a red error for one code.
# -----------------------------------------------------------


@pytest.mark.parametrize("code,use_count,expires,slug", [
    ("SUTAMPA-NEZINOMAS", None, None, "invite_invalid"),
    ("SUTAMPA-ISNAUDOTAS", 1, None, "invite_exhausted"),
    ("SUTAMPA-PASIBAIGES", 0, "past", "invite_expired"),
])
def test_both_routes_pick_the_same_slug_for_the_same_row(client, db, code, use_count, expires, slug):
    if use_count is not None:
        expires_at = ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                      if expires == "past" else None)
        _insert_code(db, code, max_uses=1, use_count=use_count, expires_at=expires_at)

    validated = client.post(VALIDATE, json={"code": code}).get_json()
    registered = client.post(REGISTER, json=_body(invitation_code=code))

    assert validated["valid"] is False
    assert validated["code"] == slug
    assert registered.status_code == 400
    assert registered.get_json()["code"] == slug


def test_a_code_validate_calls_valid_registers_on_the_next_call(client, db):
    _insert_code(db, "SUTARIMAS", role="teacher", max_uses=2)

    validated = client.post(VALIDATE, json={"code": "SUTARIMAS"}).get_json()
    assert validated == {"valid": True, "role": "teacher", "remainingUses": 2}

    registered = client.post(REGISTER, json=_body(invitation_code="SUTARIMAS"))
    assert registered.status_code == 201
    assert registered.get_json()["user"]["role"] == "teacher"
    assert client.post(VALIDATE, json={"code": "SUTARIMAS"}).get_json()["remainingUses"] == 1


def test_a_foreign_offset_expiry_is_read_the_same_way_by_both_routes(client, db):
    # 09:00-05:00 is 14:00 UTC — two hours in the future — and the
    # burn's julianday() applies that offset instead of sorting "09"
    # under "12" as text, so both routes call the row valid
    frozen = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    _insert_code(db, "OFSETAS-ABIEM", expires_at="2026-06-15T09:00:00-05:00")

    with time_machine.travel(frozen, tick=False):
        assert client.post(VALIDATE, json={"code": "OFSETAS-ABIEM"}).get_json()["valid"] is True
        registered = client.post(REGISTER, json=_body(invitation_code="OFSETAS-ABIEM"))

    assert registered.status_code == 201
