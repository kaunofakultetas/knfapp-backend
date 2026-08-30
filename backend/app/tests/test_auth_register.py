# -----------------------------------------------------------
#  [*] Tests — POST /auth/validate-code and POST /auth/register
#
#  The two routes that turn a stranger into an account, and
#  the only ones an unauthenticated caller may spend database
#  writes on. What this module proves:
#
#    - validate-code answers 200 either way and keys its
#      three rejections off `reason` / `code` WITHOUT
#      consuming a use — the register screen polls it on a
#      typing debounce, so a check that burned a use would
#      empty every code.
#    - register's invitation code is optional (guest =
#      student + invited 0) but a code that IS given must be
#      valid: unknown / exhausted / expired are 400s, never a
#      silent downgrade to guest.
#    - the use is burned ATOMICALLY. The pre-checks only pick
#      the error slug; the conditional UPDATE decides. Two
#      registrations racing for the last use cannot both win,
#      and a code exhausted, deleted or expired in the
#      microseconds after the pre-check still loses.
#    - a 409 later in the flow DISCARDS the burn — nothing
#      commits until the session mint.
#    - uniqueness is COLLATE NOCASE on both columns, so
#      'Tomas' blocks a new 'tomas' (the BINARY UNIQUE
#      constraints alone would let the case-variant in and
#      login would then reach only one of the two rows).
#    - the email is stored trimmed + lowercased, the display
#      name stripped, the password bcrypt-hashed and the
#      session token sha256-hashed at rest.
#    - every validation guard: the username charset, the
#      254-char email cap, the 6-char password floor, the
#      72-BYTE bcrypt cap (bytes, not characters), the
#      username/email containment screens, the common-password
#      list and the 1–100 display name.
#    - the stable machine `code` slugs the mobile app
#      translates off (it never shows the English prose), and
#      that validate-code and register agree on them.
#    - rate limiting: malformed retries do not spend an
#      honest user's budget, a validated attempt does, the
#      budget is per IP, and it reopens after the window.
# -----------------------------------------------------------

import hashlib
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import pytest
import time_machine
from flask import jsonify

from app.auth.routes import (
    _RATE_LIMIT_MAX_KEYS,
    ROLES,
    _check_rate_limit,
    _rate_limit_store,
    _rate_limited_response,
    _record_attempt,
    rate_limit,
    require_auth,
)

REGISTER = "/api/auth/register"
VALIDATE = "/api/auth/validate-code"




# -----------------------------------------------------------
# clean_rate_limits
# -----------------------------------------------------------
#
# The limiter store is module state, NOT request state: it
# outlives the `app` fixture and would otherwise carry one
# test's spent attempts into the next (and the app's global
# 600-per-IP budget into the whole file). Cleared around every
# test so each one starts with a full budget.
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
# A registration body that passes every guard, with unique
# username/email so two calls in one test never collide.
# Overrides replace a key; passing None for one is how a test
# sends an explicit JSON null.
#
# Used by:
#   - most register tests below
# -----------------------------------------------------------

def _body(**overrides):
    tag = uuid.uuid4().hex[:8]
    body = {
        "username": f"naujokas_{tag}",
        "password": "GiedraDiena!42",
        "display_name": "Naujokas Testinis",
        "email": f"paskyra_{tag}@knf.vu.lt",
    }
    body.update(overrides)
    return body




# -----------------------------------------------------------
# _insert_code
# -----------------------------------------------------------
#
# Plants one invitation_codes row in whatever state a test
# needs — exhausted, expired, multi-use, privileged role.
# created_by stays NULL (the column is a nullable FK), so no
# admin has to exist first.
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
    return db.execute("SELECT use_count FROM invitation_codes WHERE code = ?", (code,)).fetchone()["use_count"]


def _past():
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()




# -----------------------------------------------------------
# _race_during_burn
# -----------------------------------------------------------
#
# Fires one statement from a SECOND connection at the exact
# moment register reaches its conditional UPDATE: the route
# stamps that statement with utc_now_iso(), and that is its
# first call to it, so wrapping the name register imported
# lets a racing twin commit in the hairline between the
# pre-checks and the burn. Deterministic where real threads
# are not — this is how the rowcount-0 branches are reached
# on purpose.
#
# Used by:
#   - the atomic-burn tests below
# -----------------------------------------------------------

def _race_during_burn(monkeypatch, app, sql, params=()):
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
# validate-code — the happy answers
# -----------------------------------------------------------


def test_the_seeded_bootstrap_code_validates(client, seeded_code):
    response = client.post(VALIDATE, json={"code": seeded_code})

    assert response.status_code == 200
    body = response.get_json()
    assert body["valid"] is True
    assert body["role"] == "student"
    assert body["remainingUses"] == 100


@pytest.mark.contract
def test_validate_code_answers_the_shape_the_register_screen_reads(client, seeded_code, db):
    good = client.post(VALIDATE, json={"code": seeded_code}).get_json()
    assert set(good) == {"valid", "role", "remainingUses"}
    assert isinstance(good["remainingUses"], int)

    _insert_code(db, "SHAPE-BAD", max_uses=1, use_count=1)
    bad = client.post(VALIDATE, json={"code": "SHAPE-BAD"})
    assert bad.status_code == 200, "the client branches on `valid`, not on the status"
    assert set(bad.get_json()) == {"valid", "error", "code", "reason"}
    assert bad.get_json()["valid"] is False
    assert bad.get_json()["reason"] in ("unknown", "exhausted", "expired")


def test_remaining_uses_counts_down_from_the_uses_already_spent(client, db):
    _insert_code(db, "LIKUTIS", max_uses=5, use_count=2)

    assert client.post(VALIDATE, json={"code": "LIKUTIS"}).get_json()["remainingUses"] == 3


def test_validating_a_code_never_consumes_a_use(client, db, seeded_code):
    for _ in range(5):
        assert client.post(VALIDATE, json={"code": seeded_code}).get_json()["valid"] is True

    assert _use_count(db, seeded_code) == 0


def test_validate_code_reports_the_role_the_code_hands_out(client, db):
    _insert_code(db, "DESTYTOJO", role="teacher")

    assert client.post(VALIDATE, json={"code": "DESTYTOJO"}).get_json()["role"] == "teacher"




# -----------------------------------------------------------
# validate-code — the three rejections
# -----------------------------------------------------------


def test_an_unknown_code_is_reported_invalid_with_reason_unknown(client):
    body = client.post(VALIDATE, json={"code": "NERA-TOKIO"}).get_json()

    assert body["valid"] is False
    assert body["code"] == "invite_invalid"
    assert body["reason"] == "unknown"


def test_a_fully_used_code_is_reported_with_reason_exhausted(client, db):
    _insert_code(db, "ISNAUDOTAS", max_uses=3, use_count=3)
    body = client.post(VALIDATE, json={"code": "ISNAUDOTAS"}).get_json()

    assert body["valid"] is False
    assert body["code"] == "invite_exhausted"
    assert body["reason"] == "exhausted"


def test_an_overspent_code_is_exhausted_too(client, db):
    _insert_code(db, "PERSPAUSTAS", max_uses=1, use_count=7)

    assert client.post(VALIDATE, json={"code": "PERSPAUSTAS"}).get_json()["reason"] == "exhausted"


def test_an_expired_code_is_reported_with_reason_expired(client, db):
    _insert_code(db, "PASIBAIGES", expires_at=_past())
    body = client.post(VALIDATE, json={"code": "PASIBAIGES"}).get_json()

    assert body["valid"] is False
    assert body["code"] == "invite_expired"
    assert body["reason"] == "expired"


def test_a_code_valid_today_is_expired_a_year_later(client, db):
    _insert_code(db, "METAI", expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat())
    assert client.post(VALIDATE, json={"code": "METAI"}).get_json()["valid"] is True

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=365), tick=False):
        assert client.post(VALIDATE, json={"code": "METAI"}).get_json()["reason"] == "expired"


def test_a_malformed_expiry_counts_as_expired_instead_of_a_500(client, db):
    _insert_code(db, "SUGADINTA", expires_at="ne data")
    response = client.post(VALIDATE, json={"code": "SUGADINTA"})

    assert response.status_code == 200
    assert response.get_json()["reason"] == "expired"


def test_a_naive_expiry_is_read_as_utc(client, db):
    _insert_code(db, "NAIVI", expires_at=(datetime.now(timezone.utc) + timedelta(days=2))
                 .replace(tzinfo=None).isoformat())

    assert client.post(VALIDATE, json={"code": "NAIVI"}).get_json()["valid"] is True


def test_exhausted_is_reported_before_expired(client, db):
    _insert_code(db, "ABU", max_uses=1, use_count=1, expires_at=_past())

    assert client.post(VALIDATE, json={"code": "ABU"}).get_json()["reason"] == "exhausted"


def test_code_lookup_is_exact_about_case_and_whitespace(client, seeded_code):
    assert client.post(VALIDATE, json={"code": seeded_code.lower()}).get_json()["reason"] == "unknown"
    assert client.post(VALIDATE, json={"code": f"  {seeded_code} "}).get_json()["reason"] == "unknown"




# -----------------------------------------------------------
# validate-code — body guards
# -----------------------------------------------------------


@pytest.mark.parametrize("payload", [None, {}, {"code": ""}, {"code": None}, {"other": "x"}])
def test_validate_code_requires_a_code(client, payload):
    response = client.post(VALIDATE, json=payload)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Code required"


def test_validate_code_refuses_a_non_dict_body(client):
    array_body = client.post(VALIDATE, json=[1, 2])
    assert array_body.status_code == 400
    assert array_body.get_json()["error"] == "JSON body must be an object"

    malformed = client.post(VALIDATE, data="ne json", content_type="application/json")
    assert malformed.status_code == 400
    assert malformed.get_json()["error"] == "Code required"


@pytest.mark.parametrize("code", [12345, True, {"code": "x"}, ["KNF"]])
def test_validate_code_refuses_a_non_string_code(client, code):
    response = client.post(VALIDATE, json={"code": code})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Code must be a string"




# -----------------------------------------------------------
# register — the happy paths
# -----------------------------------------------------------


def test_registering_with_the_seeded_code_creates_an_invited_student(client, db, seeded_code):
    body = _body(invitation_code=seeded_code)
    response = client.post(REGISTER, json=body)

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["user"]["username"] == body["username"]
    assert payload["user"]["role"] == "student"
    assert payload["user"]["invited"] is True

    row = db.execute("SELECT * FROM users WHERE username = ?", (body["username"],)).fetchone()
    assert row["invited"] == 1
    assert row["role"] == "student"
    assert row["active"] == 1


def test_registering_without_a_code_creates_an_uninvited_student(client, db):
    body = _body()
    response = client.post(REGISTER, json=body)

    assert response.status_code == 201
    assert response.get_json()["user"]["invited"] is False
    assert response.get_json()["user"]["role"] == "student"
    assert db.execute("SELECT invited FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["invited"] == 0


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_invitation_code_takes_the_guest_path(client, blank):
    response = client.post(REGISTER, json=_body(invitation_code=blank))

    assert response.status_code == 201
    assert response.get_json()["user"]["invited"] is False


@pytest.mark.contract
def test_register_answers_the_user_shape_the_mobile_app_consumes(client, seeded_code):
    payload = client.post(REGISTER, json=_body(invitation_code=seeded_code)).get_json()

    assert set(payload) == {"user", "token"}
    assert set(payload["user"]) == {
        "id", "username", "email", "displayName", "role",
        "avatarUrl", "invited", "studentNumber", "studyGroup", "studyProgram",
    }
    assert isinstance(payload["token"], str) and payload["token"]
    assert payload["user"]["avatarUrl"] is None
    assert payload["user"]["studentNumber"] is None
    assert isinstance(payload["user"]["invited"], bool)


def test_the_returned_token_authenticates_the_new_account(client):
    body = _body()
    token = client.post(REGISTER, json=body).get_json()["token"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.get_json()["username"] == body["username"]


def test_the_session_token_is_stored_hashed_never_in_the_clear(client, db):
    token = client.post(REGISTER, json=_body()).get_json()["token"]

    stored = [r["token"] for r in db.execute("SELECT token FROM sessions")]
    assert token not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored


def test_the_new_session_lasts_thirty_days(client, db):
    client.post(REGISTER, json=_body())
    expires = db.execute("SELECT expires_at FROM sessions ORDER BY created_at DESC LIMIT 1").fetchone()["expires_at"]

    remaining = datetime.fromisoformat(expires) - datetime.now(timezone.utc)
    assert timedelta(days=29) < remaining <= timedelta(days=30)


def test_the_password_is_stored_bcrypt_hashed(client, db):
    body = _body(password="ViskasGerai!7")
    client.post(REGISTER, json=body)

    stored = db.execute("SELECT password_hash FROM users WHERE username = ?",
                        (body["username"],)).fetchone()["password_hash"]
    assert stored != body["password"]
    assert stored.startswith("$2")
    assert bcrypt.checkpw(body["password"].encode(), stored.encode())


def test_the_new_account_can_log_in_straight_away(client):
    body = _body()
    client.post(REGISTER, json=body)

    response = client.post("/api/auth/login", json={"username": body["username"], "password": body["password"]})
    assert response.status_code == 200


def test_the_account_is_reachable_by_the_email_whatever_case_is_typed(client):
    body = _body(email="Jonas.Jonaitis@KNF.VU.LT")
    client.post(REGISTER, json=body)

    response = client.post("/api/auth/login", json={"email": "JONAS.JONAITIS@knf.vu.lt",
                                                    "password": body["password"]})
    assert response.status_code == 200


def test_the_email_is_stored_trimmed_and_lowercased(client, db):
    body = _body(email="  Ona.Onaite@KNF.VU.LT  ")
    payload = client.post(REGISTER, json=body).get_json()

    assert payload["user"]["email"] == "ona.onaite@knf.vu.lt"
    assert db.execute("SELECT email FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["email"] == "ona.onaite@knf.vu.lt"


def test_the_display_name_is_stored_stripped(client, db):
    body = _body(display_name="   Ona Onaitė   ")
    payload = client.post(REGISTER, json=body).get_json()

    assert payload["user"]["displayName"] == "Ona Onaitė"
    assert db.execute("SELECT display_name FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["display_name"] == "Ona Onaitė"


def test_the_username_keeps_the_case_it_was_typed_in(client, db):
    body = _body(username="Tomas.Vanagas")
    assert client.post(REGISTER, json=body).get_json()["user"]["username"] == "Tomas.Vanagas"
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = 'Tomas.Vanagas'").fetchone()["c"] == 1




# -----------------------------------------------------------
# register — the role comes from the code
# -----------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_the_code_decides_the_new_accounts_role(client, db, role):
    _insert_code(db, f"ROLE-{role.upper()}", role=role)
    body = _body(invitation_code=f"ROLE-{role.upper()}")

    payload = client.post(REGISTER, json=body).get_json()
    assert payload["user"]["role"] == role
    assert payload["user"]["invited"] is True
    assert db.execute("SELECT role FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["role"] == role


def test_a_code_can_never_carry_a_role_outside_the_whitelist(db):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_code(db, "ROLE-SUPERUSER", role="superuser")


def test_registering_burns_exactly_one_use(client, db, seeded_code):
    client.post(REGISTER, json=_body(invitation_code=seeded_code))

    assert _use_count(db, seeded_code) == 1
    assert client.post(VALIDATE, json={"code": seeded_code}).get_json()["remainingUses"] == 99


def test_the_last_use_of_a_code_still_registers_and_then_the_code_is_spent(client, db):
    _insert_code(db, "PASKUTINIS", max_uses=2, use_count=1)

    assert client.post(REGISTER, json=_body(invitation_code="PASKUTINIS")).status_code == 201
    assert _use_count(db, "PASKUTINIS") == 2

    spent = client.post(REGISTER, json=_body(invitation_code="PASKUTINIS"))
    assert spent.status_code == 400
    assert spent.get_json()["code"] == "invite_exhausted"




# -----------------------------------------------------------
# register — body guards
# -----------------------------------------------------------


def test_register_requires_a_json_body(client):
    for response in (client.post(REGISTER),
                     client.post(REGISTER, json={}),
                     client.post(REGISTER, data="ne json", content_type="application/json")):
        assert response.status_code == 400
        assert response.get_json()["error"] == "JSON body required"


def test_a_top_level_array_body_is_refused_before_the_handler(client):
    # The app-wide guard catches it first — the handler's own
    # data.get() must never meet a non-dict
    response = client.post(REGISTER, json=[1, 2])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_register_names_every_missing_field(client):
    response = client.post(REGISTER, json={"kitas": "laukas"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Missing fields: username, password, display_name, email"


def test_register_names_the_one_missing_field(client):
    body = _body()
    del body["email"]

    assert client.post(REGISTER, json=body).get_json()["error"] == "Missing fields: email"


@pytest.mark.parametrize("field", ["username", "password", "display_name", "email"])
def test_a_blank_field_is_reported_missing(client, field):
    response = client.post(REGISTER, json=_body(**{field: ""}))

    assert response.get_json()["error"] == f"Missing fields: {field}"


@pytest.mark.parametrize("field", ["username", "password", "display_name", "email"])
def test_a_non_string_field_is_refused_by_name(client, field):
    response = client.post(REGISTER, json=_body(**{field: 12345}))

    assert response.status_code == 400
    assert response.get_json()["error"] == f"{field} must be a string"


def test_the_invitation_code_must_be_a_string_when_sent(client):
    response = client.post(REGISTER, json=_body(invitation_code=12345))

    assert response.status_code == 400
    assert response.get_json()["error"] == "invitation_code must be a string"




# -----------------------------------------------------------
# register — username, email, display name
# -----------------------------------------------------------


@pytest.mark.parametrize("username", [
    "ab",                     # under the 3-char floor
    "a" * 33,                 # over the 32-char ceiling
    "jonas@knf.vu.lt",        # email-shaped: login's two-column match must stay unambiguous
    "jonas vanagas",
    "jonas!",
    "jonas/../admin",
    "jonaitė",                # non-ASCII is outside the charset
    "jonas\n",
])
def test_an_out_of_charset_username_is_refused(client, username):
    response = client.post(REGISTER, json=_body(username=username))

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_username"


@pytest.mark.parametrize("username", ["abc", "a" * 32, "a.b-c_1"])
def test_the_username_charset_accepts_its_boundaries(client, username):
    assert client.post(REGISTER, json=_body(username=username)).status_code == 201


@pytest.mark.parametrize("email", [
    "ne-elpastas",
    "jonas@knf",
    "jonas@knf.",
    "@knf.vu.lt",
    "jonas@@knf.vu.lt",
    "jo nas@knf.vu.lt",
    "jonas@knf vu.lt",
])
def test_a_malformed_email_is_refused(client, email):
    response = client.post(REGISTER, json=_body(email=email))

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_email"


def test_the_email_length_cap_is_254_characters(client):
    at_cap = "a" * (254 - len("@knf.vu.lt")) + "@knf.vu.lt"
    assert len(at_cap) == 254
    assert client.post(REGISTER, json=_body(email=at_cap)).status_code == 201

    over_cap = "a" * (255 - len("@knf.vu.lt")) + "@knf.vu.lt"
    response = client.post(REGISTER, json=_body(email=over_cap))
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_email"


def test_a_display_name_of_only_whitespace_is_refused(client):
    response = client.post(REGISTER, json=_body(display_name="   "))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name cannot be empty"


def test_the_display_name_cap_is_100_characters_after_stripping(client):
    assert client.post(REGISTER, json=_body(display_name="  " + "n" * 100 + "  ")).status_code == 201

    response = client.post(REGISTER, json=_body(display_name="n" * 101))
    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name must be at most 100 characters"




# -----------------------------------------------------------
# register — the password policy
# -----------------------------------------------------------


def test_the_password_floor_is_six_characters(client):
    weak = client.post(REGISTER, json=_body(password="abc12"))
    assert weak.status_code == 400
    assert weak.get_json()["code"] == "weak_password"
    assert weak.get_json()["error"] == "Password must be at least 6 characters"

    assert client.post(REGISTER, json=_body(password="Zx9!qP")).status_code == 201


def test_the_password_ceiling_is_seventy_two_bytes(client):
    assert client.post(REGISTER, json=_body(password="Q" * 71 + "z")).status_code == 201

    response = client.post(REGISTER, json=_body(password="Q" * 72 + "z"))
    assert response.status_code == 400
    assert response.get_json()["code"] == "weak_password"
    assert response.get_json()["error"] == "Password must be at most 72 characters"


def test_the_password_ceiling_counts_bytes_not_characters(client):
    # bcrypt truncates at 72 BYTES, so 37 two-byte characters
    # would silently equal their own 36-character prefix
    at_cap = "ą" * 36
    assert len(at_cap.encode()) == 72
    assert client.post(REGISTER, json=_body(password=at_cap)).status_code == 201

    over_cap = "ą" * 37
    assert len(over_cap) == 37 and len(over_cap.encode()) == 74
    response = client.post(REGISTER, json=_body(password=over_cap))
    assert response.status_code == 400
    assert response.get_json()["error"] == "Password must be at most 72 characters"


@pytest.mark.parametrize("password", ["Vardenis123", "xx-vardenis-xx", "VARDENIS!9"])
def test_a_password_containing_the_username_is_refused(client, password):
    response = client.post(REGISTER, json=_body(username="vardenis", password=password))

    assert response.status_code == 400
    assert response.get_json()["code"] == "weak_password"
    assert response.get_json()["error"] == "Password must not contain your username"


def test_a_password_containing_the_email_local_part_is_refused(client):
    response = client.post(REGISTER, json=_body(username="kitoks", email="Studentas@knf.vu.lt",
                                                password="xxSTUDENTASxx"))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Password must not contain your email"


def test_a_local_part_under_three_characters_is_not_screened(client):
    # "ab" inside a password is far too noisy a screen to enforce
    response = client.post(REGISTER, json=_body(username="kitoks2", email="ab@knf.vu.lt",
                                                password="Zabaduoti!9"))

    assert response.status_code == 201


@pytest.mark.parametrize("password", ["password123", "slaptazodis", "labas123", "admin123", "QWERTY123"])
def test_a_common_password_is_refused(client, password):
    response = client.post(REGISTER, json=_body(username="kitoks3", email="paskyra9@knf.vu.lt",
                                                password=password))

    assert response.status_code == 400
    assert response.get_json()["code"] == "weak_password"
    assert response.get_json()["error"] == "Password is too common"


def test_a_weak_password_creates_no_account(client, db):
    body = _body(password="12345")
    client.post(REGISTER, json=body)

    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["c"] == 0




# -----------------------------------------------------------
# register — the invitation code must be valid
# -----------------------------------------------------------


def test_an_unknown_code_is_refused_and_creates_no_account(client, db):
    before = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    body = _body(invitation_code="NERA-TOKIO")

    response = client.post(REGISTER, json=body)
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_invalid"
    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == before


def test_an_exhausted_code_is_refused(client, db):
    _insert_code(db, "ISNAUDOTAS", max_uses=2, use_count=2)
    response = client.post(REGISTER, json=_body(invitation_code="ISNAUDOTAS"))

    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_exhausted"


def test_an_expired_code_is_refused(client, db):
    _insert_code(db, "PASIBAIGES", expires_at=_past())
    response = client.post(REGISTER, json=_body(invitation_code="PASIBAIGES"))

    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_expired"


def test_a_naive_invite_expiry_is_read_as_utc(client, db):
    # A legacy row without an offset must not read as expired
    _insert_code(db, "NAIVI-REG", expires_at=(datetime.now(timezone.utc) + timedelta(days=2))
                 .replace(tzinfo=None).isoformat())

    assert client.post(REGISTER, json=_body(invitation_code="NAIVI-REG")).status_code == 201
    assert _use_count(db, "NAIVI-REG") == 1


def test_a_malformed_invite_expiry_is_refused_as_expired(client, db):
    _insert_code(db, "SUGADINTA", expires_at="ne data")
    response = client.post(REGISTER, json=_body(invitation_code="SUGADINTA"))

    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_expired"


def test_a_code_that_expired_since_it_was_validated_is_refused(client, db):
    _insert_code(db, "PER-VELU", expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    assert client.post(VALIDATE, json={"code": "PER-VELU"}).get_json()["valid"] is True

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(hours=2), tick=False):
        response = client.post(REGISTER, json=_body(invitation_code="PER-VELU"))

    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_expired"


def test_a_surrounding_whitespace_in_the_code_is_forgiven(client, db, seeded_code):
    assert client.post(REGISTER, json=_body(invitation_code=f"  {seeded_code}  ")).status_code == 201
    assert _use_count(db, seeded_code) == 1


@pytest.mark.parametrize("kind,slug", [
    ("unknown", "invite_invalid"),
    ("exhausted", "invite_exhausted"),
    ("expired", "invite_expired"),
])
def test_validate_code_and_register_agree_on_the_slug(client, db, kind, slug):
    code = f"SLUG-{kind.upper()}"
    if kind == "exhausted":
        _insert_code(db, code, max_uses=2, use_count=2)
    elif kind == "expired":
        _insert_code(db, code, expires_at=_past())

    validated = client.post(VALIDATE, json={"code": code})
    registered = client.post(REGISTER, json=_body(invitation_code=code))

    assert validated.status_code == 200 and validated.get_json()["code"] == slug
    assert registered.status_code == 400 and registered.get_json()["code"] == slug




# -----------------------------------------------------------
# register — the atomic burn
#
# The pre-checks only pick the error slug; the conditional
# UPDATE is the real guard. Each test here makes the world
# change AFTER the pre-checks passed.
# -----------------------------------------------------------


def test_a_racing_twin_taking_the_last_use_loses_the_burn(client, db, app, monkeypatch):
    _insert_code(db, "LENKTYNES", max_uses=1)
    _race_during_burn(monkeypatch, app,
                      "UPDATE invitation_codes SET use_count = max_uses WHERE code = ?", ("LENKTYNES",))
    body = _body(invitation_code="LENKTYNES")

    response = client.post(REGISTER, json=body)
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_exhausted"
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["c"] == 0
    assert _use_count(db, "LENKTYNES") == 1


def test_a_code_revoked_mid_registration_is_refused_as_unknown(client, db, app, monkeypatch):
    _insert_code(db, "ATSAUKTAS", max_uses=5)
    _race_during_burn(monkeypatch, app,
                      "DELETE FROM invitation_codes WHERE code = ?", ("ATSAUKTAS",))
    body = _body(invitation_code="ATSAUKTAS")

    response = client.post(REGISTER, json=body)
    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_invalid"
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["c"] == 0


def test_a_code_expiring_on_this_very_instant_loses_the_burn(client, db):
    # The pre-check passes on equality (`expires < now` is false)
    # and the burn requires strictly-greater — the hairline the
    # conditional UPDATE exists to close
    frozen = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    _insert_code(db, "RIBA", expires_at=frozen.isoformat())

    with time_machine.travel(frozen, tick=False):
        response = client.post(REGISTER, json=_body(invitation_code="RIBA"))

    assert response.status_code == 400
    assert response.get_json()["code"] == "invite_expired"
    assert _use_count(db, "RIBA") == 0


def test_concurrent_registrations_cannot_share_one_use(app, db):
    _insert_code(db, "VIENU-METU", max_uses=2)
    bodies = [_body(username=f"lenktyne{n}", email=f"lenktyne{n}@knf.vu.lt",
                    invitation_code="VIENU-METU") for n in range(4)]
    barrier = threading.Barrier(len(bodies))
    results = []
    guard = threading.Lock()

    def _attempt(body):
        worker = app.test_client()
        barrier.wait()
        response = worker.post(REGISTER, json=body)
        with guard:
            results.append((response.status_code, response.get_json()))

    threads = [threading.Thread(target=_attempt, args=(b,)) for b in bodies]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == len(bodies), f"a worker never answered: {results}"
    successes = [payload for status, payload in results if status == 201]
    # A loser either gets the invite slug or SQLite's own busy 503
    # (WAL refuses a stale-snapshot write upgrade outright) — what
    # must NEVER happen is a third winner or a use spent on nobody
    assert 1 <= len(successes) <= 2, f"burn was not atomic: {results}"
    assert all(status in (201, 400, 503) for status, _ in results), results
    assert all(payload["code"] == "invite_exhausted" for status, payload in results if status == 400)
    assert _use_count(db, "VIENU-METU") == len(successes), "a use was burned without an account"
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username LIKE 'lenktyne%'"
                      ).fetchone()["c"] == len(successes)




# -----------------------------------------------------------
# register — uniqueness, COLLATE NOCASE
# -----------------------------------------------------------


def test_a_taken_username_is_refused_with_409(client, make_user):
    taken = make_user(username="tomas")
    response = client.post(REGISTER, json=_body(username=taken["username"]))

    assert response.status_code == 409
    assert response.get_json()["code"] == "username_taken"
    assert response.get_json()["error"] == "Username or email already exists"


def test_a_case_variant_username_is_refused_too(client, make_user):
    make_user(username="Tomas")

    response = client.post(REGISTER, json=_body(username="tomas"))
    assert response.status_code == 409
    assert response.get_json()["code"] == "username_taken"


def test_a_case_variant_of_the_seeded_admin_is_refused(client):
    assert client.post(REGISTER, json=_body(username="ADMIN")).status_code == 409


def test_a_taken_email_is_refused_with_409(client, make_user):
    taken = make_user(username="ona")

    response = client.post(REGISTER, json=_body(email=taken["email"]))
    assert response.status_code == 409
    assert response.get_json()["code"] == "username_taken"


def test_a_case_variant_email_is_refused_too(client, make_user):
    make_user(username="Ona")   # stored as Ona@knf.vu.lt

    response = client.post(REGISTER, json=_body(email="ONA@KNF.VU.LT"))
    assert response.status_code == 409


def test_a_refused_duplicate_never_burns_the_invitation_code(client, db, make_user):
    make_user(username="dubliuotas")
    _insert_code(db, "NESUDEGES", max_uses=1)

    response = client.post(REGISTER, json=_body(username="Dubliuotas", invitation_code="NESUDEGES"))
    assert response.status_code == 409
    assert _use_count(db, "NESUDEGES") == 0, "the burn must roll back with the failed registration"
    assert client.post(VALIDATE, json={"code": "NESUDEGES"}).get_json()["valid"] is True


def test_an_insert_race_answers_409_instead_of_a_500(client, db, make_user, monkeypatch):
    # The NOCASE pre-check is only the fast path — the INSERT's
    # own IntegrityError must answer the same 409, so a racer
    # that lands between the two never sees a 500
    twin = make_user(username="dvynys")
    body = _body()
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(twin["id"]))

    response = client.post(REGISTER, json=body)
    assert response.status_code == 409
    assert response.get_json()["code"] == "username_taken"
    assert db.execute("SELECT COUNT(*) c FROM users WHERE username = ?",
                      (body["username"],)).fetchone()["c"] == 0




# -----------------------------------------------------------
# rate limiting
# -----------------------------------------------------------


def test_malformed_registrations_do_not_spend_the_budget(client):
    # The attempt is recorded only once the body validates, so a
    # user fixing typos cannot lock themselves out
    for _ in range(15):
        assert client.post(REGISTER, json={"username": "x"}).status_code == 400

    assert client.post(REGISTER, json=_body()).status_code == 201


def test_ten_validated_attempts_exhaust_the_register_budget(client):
    for _ in range(10):
        response = client.post(REGISTER, json=_body(invitation_code="NERA-TOKIO"))
        assert response.status_code == 400

    blocked = client.post(REGISTER, json=_body())
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    assert 1 <= int(blocked.headers["Retry-After"]) <= 301


def test_the_register_budget_is_per_client_ip(client):
    for _ in range(10):
        client.post(REGISTER, json=_body(invitation_code="NERA-TOKIO"),
                    environ_base={"REMOTE_ADDR": "10.0.0.1"})

    assert client.post(REGISTER, json=_body(),
                       environ_base={"REMOTE_ADDR": "10.0.0.1"}).status_code == 429
    assert client.post(REGISTER, json=_body(),
                       environ_base={"REMOTE_ADDR": "10.0.0.2"}).status_code == 201


def test_the_register_budget_reopens_after_the_window(client):
    with time_machine.travel(datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(10):
            client.post(REGISTER, json=_body(invitation_code="NERA-TOKIO"))
        assert client.post(REGISTER, json=_body()).status_code == 429

        traveller.shift(timedelta(seconds=301))
        assert client.post(REGISTER, json=_body()).status_code == 201


def test_validate_code_allows_thirty_attempts_before_it_bites(client, seeded_code):
    # The register screen re-validates on a 600 ms typing
    # debounce, so one honest code entry is a dozen calls
    for _ in range(30):
        assert client.post(VALIDATE, json={"code": seeded_code}).status_code == 200

    blocked = client.post(VALIDATE, json={"code": seeded_code})
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1


def test_the_validate_code_budget_is_per_client_ip(client, seeded_code):
    for _ in range(30):
        client.post(VALIDATE, json={"code": seeded_code}, environ_base={"REMOTE_ADDR": "10.0.0.3"})

    assert client.post(VALIDATE, json={"code": seeded_code},
                       environ_base={"REMOTE_ADDR": "10.0.0.3"}).status_code == 429
    assert client.post(VALIDATE, json={"code": seeded_code},
                       environ_base={"REMOTE_ADDR": "10.0.0.4"}).status_code == 200


def test_the_validate_code_budget_reopens_after_the_window(client, seeded_code):
    with time_machine.travel(datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc), tick=False) as traveller:
        for _ in range(30):
            client.post(VALIDATE, json={"code": seeded_code})
        assert client.post(VALIDATE, json={"code": seeded_code}).status_code == 429

        traveller.shift(timedelta(seconds=301))
        assert client.post(VALIDATE, json={"code": seeded_code}).status_code == 200


def test_a_rejected_probe_does_not_grow_the_limiter_store(app):
    assert _check_rate_limit("zondas:naujas", record=False) is False
    assert "zondas:naujas" not in _rate_limit_store, "a pure probe must not plant a key"


def test_the_limiter_store_is_capped_by_its_lru_ceiling(app):
    # Spoofed X-Forwarded-For addresses must not grow the store
    # without bound — the least recently touched key is dropped
    for n in range(_RATE_LIMIT_MAX_KEYS + 100):
        _check_rate_limit(f"lru:{n}")

    assert len(_rate_limit_store) == _RATE_LIMIT_MAX_KEYS
    assert "lru:0" not in _rate_limit_store
    assert f"lru:{_RATE_LIMIT_MAX_KEYS + 99}" in _rate_limit_store

    _record_attempt("lru:irasytas")
    assert len(_rate_limit_store) == _RATE_LIMIT_MAX_KEYS
    assert "lru:irasytas" in _rate_limit_store


def test_a_recorded_attempt_keeps_the_key_at_the_fresh_end(app):
    _record_attempt("tvarka:a")
    _record_attempt("tvarka:b")
    _record_attempt("tvarka:a")

    assert list(_rate_limit_store)[-1] == "tvarka:a"


def test_the_shared_rate_limit_decorator_meters_anonymous_callers_by_ip(app):
    # The decorator the other blueprints put on their write
    # routes: no user resolved, so the key is the client IP
    @app.route("/api/_zondas", methods=["POST"])
    @rate_limit("zondas", max_attempts=2)
    def _zondas():
        return jsonify({"ok": True})

    caller = app.test_client()
    assert caller.post("/api/_zondas").status_code == 200
    assert caller.post("/api/_zondas").status_code == 200

    blocked = caller.post("/api/_zondas")
    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1

    assert caller.post("/api/_zondas", environ_base={"REMOTE_ADDR": "10.9.9.9"}).status_code == 200


def test_the_shared_rate_limit_decorator_meters_signed_in_callers_by_user(app, make_user, auth_headers):
    # Stacked UNDER require_auth, so request.user is already
    # resolved and one flooding account cannot spend another's
    @app.route("/api/_zondas_auth", methods=["POST"])
    @require_auth
    @rate_limit("zondas_auth", max_attempts=1)
    def _zondas_auth():
        return jsonify({"ok": True})

    first = make_user()
    second = make_user()
    first_headers = auth_headers(first)
    second_headers = auth_headers(second)

    assert app.test_client().post("/api/_zondas_auth", headers=first_headers).status_code == 200
    assert app.test_client().post("/api/_zondas_auth", headers=first_headers).status_code == 429
    assert app.test_client().post("/api/_zondas_auth", headers=second_headers).status_code == 200


def test_retry_after_never_tells_a_client_to_retry_immediately(app):
    with app.app_context():
        response, status = _rate_limited_response("Too many", "tuscias:raktas")

    assert status == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) == 1
