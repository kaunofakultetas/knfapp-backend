# -----------------------------------------------------------
#  [*] Tests — social friend requests, the exhaustive pass
#
#  The gap-closing sweep over four functions of
#  app/social/routes.py:
#
#    send_friend_request     POST   /friends/request
#    list_friend_requests    GET    /friends/requests
#    accept_friend_request   POST   /friends/requests/<id>/accept
#    reject_friend_request   POST   /friends/requests/<id>/reject
#
#  test_social_friends.py already walks the happy paths and
#  the headline guards. This file takes every arm those left
#  standing:
#
#    - the body funnel BEFORE the route: create_app's
#      validate_json_input refuses a non-object body, so a
#      top-level array/scalar never reaches get_json_object,
#      while a JSON `null`, an empty body and a non-JSON
#      content type do reach it and fall out of the same
#      "user_id required" 400
#    - every falsy user_id (None, 0, 0.0, False, "", [], {})
#      landing on "user_id required" and every truthy
#      non-string (True, ints, floats, dicts, lists) landing
#      on "user_id must be a string" — bool being the trap,
#      since it is truthy and is not a str
#    - target resolution boundaries: whitespace, NUL bytes,
#      quote-shaped injection payloads, 10 000-char ids,
#      Lithuanian text, and the case sensitivity that keeps an
#      upper-cased uuid a stranger
#    - every ownership/state permutation of the pending and
#      friendship pre-checks, including the ONE-DIRECTION
#      friendship read the banner admits to and the settled
#      'accepted' row that must not block a new send
#    - the cooldown's COALESCE(updated_at, created_at) both
#      ways round, and the opportunistic purge's reach: it
#      runs only on the path that gets that far, and an
#      IntegrityError rollback takes it back with it
#    - the quotas as budgets: friendreq (20) spent by refusals
#      too, friendaction (60) SHARED by accept and reject, and
#      neither spent by a 401
#    - list paging to the last integer on both parameters,
#      the direction whitelist, the created_at/id DESC
#      tie-break, and the pending-only + active-only filters
#      on both the page and its total
#    - accept/reject as state machines: what each leaves in
#      friend_requests, what it leaves in friendships, what it
#      refuses with a 404, and what a hand-seeded self-
#      addressed row does to both
#
#  Nothing here sleeps (time_machine moves the clock) and
#  nothing here touches the network.
# -----------------------------------------------------------

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine


_SEND = "/api/social/friends/request"
_REQUESTS = "/api/social/friends/requests"
_FRIENDS = "/api/social/friends"

# Far enough in the future that a session minted inside a
# traveller survives every jump a test below makes
_T0 = datetime(2027, 6, 1, 9, 0, 0, tzinfo=timezone.utc)

# The route's own numbers, restated so a test can name the
# boundary it stands on
_SEND_BUDGET = 20
_ACTION_BUDGET = 60
_LIST_CAP = 200
_COOLDOWN = timedelta(days=7)




# -----------------------------------------------------------
# fresh_rate_limits
# -----------------------------------------------------------
#
# auth/routes.py keeps every window — the per-route quotas AND
# create_app's global per-IP budget — in one module-level
# dict that outlives the per-test database. Clearing it either
# side of each test is what makes a 429 assertion below mean
# the quota under test, and stops a 60-call loop here from
# bankrupting the next test's IP budget.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_rate_limits(app):
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# duo / trio
# -----------------------------------------------------------
#
# Signed-in students, ((user, headers), ...). Logging in costs
# a real bcrypt round each, so a test asks for the smaller
# fixture whenever two people are enough.
# -----------------------------------------------------------

@pytest.fixture
def duo(make_user, auth_headers):
    ana = make_user(username="ana", display_name="Ana")
    benas = make_user(username="benas", display_name="Benas")
    return (ana, auth_headers(ana)), (benas, auth_headers(benas))


@pytest.fixture
def trio(make_user, auth_headers):
    ana = make_user(username="ana", display_name="Ana")
    benas = make_user(username="benas", display_name="Benas")
    ceslovas = make_user(username="ceslovas", display_name="Ceslovas")
    return ((ana, auth_headers(ana)), (benas, auth_headers(benas)),
            (ceslovas, auth_headers(ceslovas)))




# -----------------------------------------------------------
# Wire helpers
# -----------------------------------------------------------
#
# _post_raw is TESTPLAN rule 10: the test client serialises a
# `json=` kwarg through the app's OWN provider, which
# html-escapes every string on the way out, so a payload whose
# exact bytes matter (quotes, angle brackets, NUL, Lithuanian
# text) has to be dumped here and posted as bytes instead.
# -----------------------------------------------------------

def _post_raw(client, path, payload, headers):
    return client.post(path, data=json.dumps(payload),
                       headers={**headers, "Content-Type": "application/json"})


def _send(client, headers, target_id):
    return _post_raw(client, _SEND, {"user_id": target_id}, headers)


def _accept(client, headers, request_id):
    return client.post(f"{_REQUESTS}/{request_id}/accept", headers=headers)


def _reject(client, headers, request_id):
    return client.post(f"{_REQUESTS}/{request_id}/reject", headers=headers)


def _requests_of(client, headers, direction=None, **params):
    query = dict(params)
    if direction is not None:
        query["direction"] = direction
    return client.get(_REQUESTS, query_string=query, headers=headers)




# -----------------------------------------------------------
# _seed_request / _row / _rows / _friendship_pairs
# -----------------------------------------------------------
#
# The route can only ever create a PENDING row, so the settled
# states it has to refuse ('accepted', an aged 'rejected') are
# planted straight into the table. Timestamps go in as the
# house T-form UTC string, which is what the cooldown's string
# comparison assumes.
# -----------------------------------------------------------

def _stamp(delta=None):
    moment = datetime.now(timezone.utc)
    return (moment + delta if delta else moment).isoformat()


def _seed_request(db, from_id, to_id, status="pending", created_at=None,
                  updated_at=None, request_id=None):
    request_id = request_id or str(uuid.uuid4())
    created_at = created_at or _stamp()
    db.execute(
        "INSERT INTO friend_requests (id, from_user_id, to_user_id, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (request_id, from_id, to_id, status, created_at, updated_at or created_at),
    )
    db.commit()
    return request_id


def _row(db, request_id):
    return db.execute("SELECT * FROM friend_requests WHERE id = ?", (request_id,)).fetchone()


def _rows(db):
    return db.execute(
        "SELECT id, from_user_id, to_user_id, status FROM friend_requests"
    ).fetchall()


def _friendship_pairs(db):
    return {
        (r["user_id"], r["friend_id"])
        for r in db.execute("SELECT user_id, friend_id FROM friendships").fetchall()
    }


def _seed_friendship(db, user_id, friend_id, created_at=None):
    db.execute(
        "INSERT INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
        (user_id, friend_id, created_at or _stamp()),
    )
    db.commit()




# -----------------------------------------------------------
# _seed_user
# -----------------------------------------------------------
#
# A users row for somebody who only ever appears at the OTHER
# end of a request — a sender in a hundred-row list, a target
# in a twenty-send budget loop. make_user is the fixture for
# anyone who has to log in; it bcrypt-hashes a password, which
# a hundred of costs half a minute of wall clock for a
# password nothing in these tests ever presents.
# -----------------------------------------------------------

def _seed_user(db, username, role="student", display_name=None, active=1):
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, role, active, invited)"
        " VALUES (?, ?, ?, ?, 'not-a-usable-hash', ?, ?, 1)",
        (user_id, username, f"{username}@knf.vu.lt", display_name or username.title(), role, active),
    )
    db.commit()
    return {"id": user_id, "username": username, "role": role}




# -----------------------------------------------------------
# _arm_pending_row_race
# -----------------------------------------------------------
#
# The second tab that wins: a BEFORE INSERT trigger dropping
# the competing pending row for the very pair the route is
# about to write, i.e. exactly in the window between its
# duplicate check and its own INSERT. Migration v21's partial
# unique index then raises the real IntegrityError, so STEP
# 4's except arm is proved against the database rather than a
# patched sqlite3.
# -----------------------------------------------------------

def _arm_pending_row_race(db):
    db.executescript(
        """
        CREATE TRIGGER race_the_pending_insert BEFORE INSERT ON friend_requests
        BEGIN
            INSERT INTO friend_requests (id, from_user_id, to_user_id, status)
            VALUES ('raced-' || NEW.id, NEW.from_user_id, NEW.to_user_id, 'pending');
        END;
        """
    )
    db.commit()




# -----------------------------------------------------------
# send — the body funnel, before and inside the route
# -----------------------------------------------------------

def test_a_top_level_array_body_never_reaches_the_route(client, actor):
    _, headers = actor

    response = _post_raw(client, _SEND, [{"user_id": "x"}], headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_top_level_string_body_never_reaches_the_route(client, actor):
    _, headers = actor

    response = _post_raw(client, _SEND, "just-a-string", headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_top_level_number_body_never_reaches_the_route(client, actor):
    _, headers = actor

    response = _post_raw(client, _SEND, 12345, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_top_level_false_body_never_reaches_the_route(client, actor):
    _, headers = actor

    response = _post_raw(client, _SEND, False, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_json_null_body_falls_out_of_the_routes_own_guard(client, actor):
    _, headers = actor

    # `null` parses to None, which the before_request hook lets
    # through — get_json_object turns it into the route's 400
    response = _post_raw(client, _SEND, None, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id required"


def test_an_empty_body_with_a_json_content_type_is_refused(client, actor):
    _, headers = actor

    response = client.post(_SEND, data=b"", headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id required"


def test_truncated_json_is_refused_rather_than_raising(client, actor):
    _, headers = actor

    response = client.post(_SEND, data='{"user_id": ',
                           headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id required"


def test_a_json_body_sent_without_a_json_content_type_is_refused(client, duo):
    (_, ana_headers), (benas, _) = duo

    response = client.post(_SEND, data=json.dumps({"user_id": benas["id"]}), headers=ana_headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id required"


def test_a_form_encoded_body_is_refused(client, duo):
    (_, ana_headers), (benas, _) = duo

    response = client.post(_SEND, data={"user_id": benas["id"]}, headers=ana_headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id required"


def test_an_over_nested_body_is_a_400_not_a_500(client, actor):
    _, headers = actor
    payload = {"user_id": "x"}
    for _ in range(80):
        payload = {"nested": payload}

    response = _post_raw(client, _SEND, payload, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON nesting too deep"


def test_unknown_keys_alongside_user_id_are_ignored(client, db, duo):
    (ana, ana_headers), (benas, _) = duo

    response = _post_raw(client, _SEND, {
        "user_id": benas["id"], "status": "accepted", "id": "hand-picked",
        "from_user_id": benas["id"], "message": "labas",
    }, ana_headers)

    assert response.status_code == 201
    row = _row(db, response.get_json()["id"])
    assert (row["from_user_id"], row["to_user_id"], row["status"]) == (ana["id"], benas["id"], "pending")


def test_a_repeated_user_id_key_takes_the_last_value(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, _) = trio

    # Two "user_id" members in one object — json.loads keeps the
    # last, and the route must act on exactly that one
    body = '{"user_id": "%s", "user_id": "%s"}' % (benas["id"], ceslovas["id"])
    response = client.post(_SEND, data=body,
                           headers={**ana_headers, "Content-Type": "application/json"})

    assert response.status_code == 201
    assert _row(db, response.get_json()["id"])["to_user_id"] == ceslovas["id"]




# -----------------------------------------------------------
# send — the user_id type ladder
# -----------------------------------------------------------
#
# STEP 1 asks two questions in order: is the value truthy, and
# is it a str. A falsy value never reaches the type check, so
# 0 and False are "user_id required" while True — truthy, and
# not a str — is "user_id must be a string".
# -----------------------------------------------------------

def test_every_falsy_user_id_reads_as_missing(client, actor):
    _, headers = actor

    for value in (None, 0, 0.0, False, "", [], {}):
        response = _post_raw(client, _SEND, {"user_id": value}, headers)
        assert response.status_code == 400, value
        assert response.get_json()["error"] == "user_id required", value


def test_every_truthy_non_string_user_id_is_refused_by_type(client, actor):
    _, headers = actor

    for value in (True, 1, -1, 1.5, {"id": "x"}, ["x"]):
        response = _post_raw(client, _SEND, {"user_id": value}, headers)
        assert response.status_code == 400, value
        assert response.get_json()["error"] == "user_id must be a string", value


def test_the_type_guard_runs_before_the_database_is_touched(client, db, actor):
    _, headers = actor

    _post_raw(client, _SEND, {"user_id": 7}, headers)

    assert _rows(db) == []




# -----------------------------------------------------------
# send — resolving the target
# -----------------------------------------------------------

def test_asking_exactly_yourself_is_refused_before_any_lookup(client, db, actor):
    user, headers = actor

    response = _send(client, headers, user["id"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot friend yourself"
    assert _rows(db) == []


def test_a_whitespace_only_user_id_is_a_404(client, db, actor):
    _, headers = actor

    response = _send(client, headers, "   ")

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"
    assert _rows(db) == []


def test_a_quote_shaped_user_id_is_a_404_not_an_injection(client, db, actor):
    _, headers = actor
    _seed_user(db, "taikinys")

    response = _send(client, headers, "' OR 1=1 --")

    assert response.status_code == 404
    assert _rows(db) == []


def test_a_ten_thousand_character_user_id_is_a_404(client, actor):
    _, headers = actor

    response = _send(client, headers, "x" * 10_000)

    assert response.status_code == 404


def test_a_lithuanian_user_id_is_a_404(client, actor):
    _, headers = actor

    response = _send(client, headers, "ąčęėįšųūž-nėra-tokio")

    assert response.status_code == 404


def test_a_user_id_carrying_nul_bytes_is_stripped_and_missed(client, duo):
    (_, ana_headers), (benas, _) = duo

    # The before_request hook drops NUL before SQLite sees it,
    # so the id no longer matches anything
    response = _send(client, ana_headers, benas["id"] + "\x00tail")

    assert response.status_code == 404


def test_the_target_lookup_is_case_sensitive(client, duo):
    (_, ana_headers), (benas, _) = duo

    response = _send(client, ana_headers, benas["id"].upper())

    assert response.status_code == 404


def test_the_self_check_is_case_sensitive_too(client, actor):
    user, headers = actor

    # Upper-cased, so it is neither "me" nor a row that exists
    response = _send(client, headers, user["id"].upper())

    assert response.status_code == 404


def test_a_padded_copy_of_my_own_id_is_a_404_not_a_self_request(client, actor):
    user, headers = actor

    response = _send(client, headers, f" {user['id']} ")

    assert response.status_code == 404


def test_a_deactivated_target_is_a_404_even_for_an_admin_sender(client, db, make_user, admin):
    _, admin_headers = admin
    gone = make_user(username="isjunges", active=0)

    response = _send(client, admin_headers, gone["id"])

    assert response.status_code == 404
    assert _rows(db) == []


def test_a_request_may_be_sent_to_every_role(client, db, actor):
    _, headers = actor
    targets = [_seed_user(db, f"role_{role}", role=role)
               for role in ("student", "teacher", "curator", "admin")]

    for target in targets:
        assert _send(client, headers, target["id"]).status_code == 201

    assert {r["to_user_id"] for r in _rows(db)} == {t["id"] for t in targets}


def test_a_deactivated_target_beats_the_auto_accept(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    _send(client, benas_headers, ana["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (benas["id"],))
    db.commit()

    # STEP 2 runs before STEP 3, so a switched-off account cannot
    # be friended by asking it back
    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 404
    assert _friendship_pairs(db) == set()


def test_a_deactivated_target_beats_the_already_friends_check(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    _seed_friendship(db, ana["id"], benas["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (benas["id"],))
    db.commit()

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_a_deactivated_sender_cannot_send_accept_or_reject(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, benas_headers, ana["id"]).get_json()["id"]
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (ana["id"],))
    db.commit()

    assert _send(client, ana_headers, benas["id"]).status_code == 401
    assert _accept(client, ana_headers, request_id).status_code == 401
    assert _reject(client, ana_headers, request_id).status_code == 401




# -----------------------------------------------------------
# send — the pre-checks, every permutation
# -----------------------------------------------------------

def test_a_forward_friendship_row_alone_is_already_friends(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    _seed_friendship(db, ana["id"], benas["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 409
    assert response.get_json()["error"] == "Already friends"


def test_a_reverse_only_friendship_does_not_block_a_new_request(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    # Only THEIR direction exists — the check reads (me, them),
    # so a half-written friendship still lets a request through
    _seed_friendship(db, benas["id"], ana["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201
    assert _row(db, response.get_json()["id"])["status"] == "pending"


def test_my_own_pending_row_is_a_409_that_changes_nothing(client, db, duo):
    (_, ana_headers), (benas, _) = duo
    first = _send(client, ana_headers, benas["id"]).get_json()["id"]
    before = dict(_row(db, first))

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 409
    assert response.get_json()["error"] == "Friend request already pending"
    assert dict(_row(db, first)) == before
    assert len(_rows(db)) == 1


def test_a_pending_row_to_a_third_party_does_not_block(client, trio):
    (_, ana_headers), (benas, _), (ceslovas, _) = trio
    _send(client, ana_headers, benas["id"])

    response = _send(client, ana_headers, ceslovas["id"])

    assert response.status_code == 201


def test_a_pending_row_between_two_other_people_does_not_block(client, db, trio):
    (ana, ana_headers), (benas, benas_headers), (ceslovas, _) = trio
    _send(client, benas_headers, ceslovas["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201
    assert len(_rows(db)) == 2


def test_a_settled_accepted_row_does_not_block_a_new_request(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    _seed_request(db, ana["id"], benas["id"], status="accepted")

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201
    assert sorted(r["status"] for r in _rows(db)) == ["accepted", "pending"]


def test_their_rejected_row_does_not_block_my_request(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    # THEY were declined by ME — the cooldown reads from_user_id,
    # so it binds them, never me
    _seed_request(db, benas["id"], ana["id"], status="rejected")

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201




# -----------------------------------------------------------
# send — the auto-accept arm
# -----------------------------------------------------------

def test_the_auto_accept_answers_two_hundred_with_its_own_message(client, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    _send(client, benas_headers, ana["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "accepted"
    assert "auto-accepted" in body["message"]
    assert "id" not in body


def test_the_auto_accept_deletes_only_the_handshake_row_it_settled(client, db, trio):
    (ana, ana_headers), (benas, benas_headers), (ceslovas, ceslovas_headers) = trio
    _send(client, benas_headers, ana["id"])
    survivor = _send(client, ceslovas_headers, ana["id"]).get_json()["id"]

    _send(client, ana_headers, benas["id"])

    assert [r["id"] for r in _rows(db)] == [survivor]


def test_the_auto_accept_writes_both_friendship_directions(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    _send(client, benas_headers, ana["id"])

    _send(client, ana_headers, benas["id"])

    assert _friendship_pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}


def test_a_send_after_the_auto_accept_is_already_friends(client, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    _send(client, benas_headers, ana["id"])
    _send(client, ana_headers, benas["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 409
    assert response.get_json()["error"] == "Already friends"


def test_the_auto_accept_keeps_an_existing_friendship_stamp(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    _send(client, benas_headers, ana["id"])
    # Their direction already written by an earlier half-race —
    # OR IGNORE must leave the original created_at alone
    _seed_friendship(db, benas["id"], ana["id"], created_at="2020-01-01T00:00:00+00:00")

    _send(client, ana_headers, benas["id"])

    kept = db.execute(
        "SELECT created_at FROM friendships WHERE user_id = ? AND friend_id = ?",
        (benas["id"], ana["id"]),
    ).fetchone()["created_at"]
    assert kept == "2020-01-01T00:00:00+00:00"


def test_the_auto_accept_beats_a_cooldown_the_sender_is_inside(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    # Ana was declined yesterday, but Benas has since asked HER:
    # STEP 3 settles that before STEP 3.1 ever runs
    _seed_request(db, ana["id"], benas["id"], status="rejected",
                  created_at=_stamp(-timedelta(days=1)))
    _send(client, benas_headers, ana["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert response.get_json()["status"] == "accepted"




# -----------------------------------------------------------
# send — the cooldown and the purge it carries
# -----------------------------------------------------------

def test_the_cooldown_body_carries_the_stable_code_and_a_message(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    _seed_request(db, ana["id"], benas["id"], status="rejected")

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 429
    body = response.get_json()
    assert body["code"] == "friend_request_cooldown"
    assert body["error"].startswith("This person declined")


def test_the_cooldown_reads_updated_at_over_created_at(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    # Asked a month ago, declined yesterday — the decline is what
    # the window is measured from
    _seed_request(db, ana["id"], benas["id"], status="rejected",
                  created_at=_stamp(-timedelta(days=30)),
                  updated_at=_stamp(-timedelta(days=1)))

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 429
    assert response.get_json()["code"] == "friend_request_cooldown"


def test_an_old_updated_at_over_a_fresh_created_at_no_longer_blocks(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    _seed_request(db, ana["id"], benas["id"], status="rejected",
                  created_at=_stamp(-timedelta(minutes=1)),
                  updated_at=_stamp(-timedelta(days=30)))

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201


def test_a_stale_rejection_is_purged_by_the_send_that_outlived_it(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    stale = _seed_request(db, ana["id"], benas["id"], status="rejected",
                          created_at=_stamp(-timedelta(days=30)))

    assert _send(client, ana_headers, benas["id"]).status_code == 201

    assert _row(db, stale) is None
    assert [r["status"] for r in _rows(db)] == ["pending"]


def test_the_purge_sweeps_rejections_belonging_to_other_pairs(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, _) = trio
    foreign = _seed_request(db, benas["id"], ceslovas["id"], status="rejected",
                            created_at=_stamp(-timedelta(days=30)))

    assert _send(client, ana_headers, benas["id"]).status_code == 201

    assert _row(db, foreign) is None


def test_the_purge_leaves_rejections_still_inside_the_window(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, _) = trio
    fresh = _seed_request(db, benas["id"], ceslovas["id"], status="rejected",
                          created_at=_stamp(-timedelta(days=1)))

    assert _send(client, ana_headers, benas["id"]).status_code == 201

    assert _row(db, fresh) is not None


def test_the_purge_leaves_pending_and_accepted_rows_alone(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, _) = trio
    old_pending = _seed_request(db, benas["id"], ceslovas["id"],
                                created_at=_stamp(-timedelta(days=400)))
    old_accepted = _seed_request(db, ceslovas["id"], benas["id"], status="accepted",
                                 created_at=_stamp(-timedelta(days=400)))

    assert _send(client, ana_headers, benas["id"]).status_code == 201

    assert _row(db, old_pending) is not None
    assert _row(db, old_accepted) is not None


def test_the_purge_never_runs_on_a_send_refused_before_it(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, _) = trio
    stale = _seed_request(db, benas["id"], ceslovas["id"], status="rejected",
                          created_at=_stamp(-timedelta(days=30)))
    _seed_friendship(db, ana["id"], benas["id"])

    assert _send(client, ana_headers, benas["id"]).status_code == 409

    assert _row(db, stale) is not None


def test_the_purge_never_runs_on_a_send_the_cooldown_refuses(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, _) = trio
    stale = _seed_request(db, benas["id"], ceslovas["id"], status="rejected",
                          created_at=_stamp(-timedelta(days=30)))
    _seed_request(db, ana["id"], benas["id"], status="rejected")

    assert _send(client, ana_headers, benas["id"]).status_code == 429

    assert _row(db, stale) is not None


def test_a_lost_insert_race_rolls_the_purge_back_with_it(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, _) = trio
    stale = _seed_request(db, benas["id"], ceslovas["id"], status="rejected",
                          created_at=_stamp(-timedelta(days=30)))
    _arm_pending_row_race(db)

    response = _send(client, ana_headers, benas["id"])

    # The purge and the INSERT share one transaction, so the
    # IntegrityError's rollback takes the sweep back too
    assert response.status_code == 409
    assert _row(db, stale) is not None


def test_a_lost_insert_race_writes_no_row_of_its_own(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    _arm_pending_row_race(db)

    _send(client, ana_headers, benas["id"])

    # SQLite's ABORT backs the failed statement out — the
    # trigger's row went in inside it, so it goes with it — and
    # the route's rollback finishes the job: the losing request
    # leaves the table exactly as it found it. In production the
    # winner is another connection whose row is already
    # committed, so only THIS side's write is lost
    assert _rows(db) == []


def test_a_cooldown_refusal_still_spends_a_send_from_the_budget(client, db, duo):
    (ana, ana_headers), (benas, _) = duo
    _seed_request(db, ana["id"], benas["id"], status="rejected")

    for _ in range(_SEND_BUDGET):
        assert _send(client, ana_headers, benas["id"]).get_json()["code"] == "friend_request_cooldown"

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_becoming_friends_clears_the_cooldown_the_decline_left(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    declined = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _reject(client, benas_headers, declined)
    # They became friends the other way round, which settles the
    # decline — the accept takes the rejected row with it, so
    # unfriending leaves Ana free to ask again
    second = _send(client, benas_headers, ana["id"]).get_json()["id"]
    _accept(client, ana_headers, second)
    assert _rows(db) == []
    client.delete(f"{_FRIENDS}/{benas['id']}", headers=ana_headers)

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201
    assert response.get_json()["status"] == "pending"




# -----------------------------------------------------------
# send — the 201 and its row
# -----------------------------------------------------------

@pytest.mark.contract
def test_the_created_request_answers_exactly_id_and_status(client, duo):
    (_, ana_headers), (benas, _) = duo

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201
    assert response.headers["Content-Type"].startswith("application/json")
    assert set(response.get_json()) == {"id", "status"}
    assert response.get_json()["status"] == "pending"


def test_the_returned_id_is_a_uuid_that_names_the_row(client, db, duo):
    (_, ana_headers), (benas, _) = duo

    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    assert uuid.UUID(request_id).version == 4
    assert _row(db, request_id) is not None


def test_a_new_row_stamps_created_at_and_updated_at_identically(client, db, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")

    # The login has to happen INSIDE the traveller: a session
    # minted at the real clock is 30 days dead by _T0
    with time_machine.travel(_T0, tick=False):
        request_id = _send(client, auth_headers(ana), benas["id"]).get_json()["id"]

    row = _row(db, request_id)
    assert row["created_at"] == row["updated_at"] == _T0.isoformat()


def test_an_unauthenticated_send_never_spends_the_send_budget(client, duo):
    (_, ana_headers), (benas, _) = duo

    for _ in range(_SEND_BUDGET + 5):
        assert _send(client, {}, benas["id"]).status_code == 401

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201


def test_the_send_budget_is_kept_per_sender(client, db, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")
    targets = [_seed_user(db, f"target{i}") for i in range(_SEND_BUDGET)]

    ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
    for target in targets:
        assert _send(client, ana_headers, target["id"]).status_code == 201
    assert _send(client, ana_headers, benas["id"]).status_code == 429

    # Benas has not spent a thing
    response = _send(client, benas_headers, targets[0]["id"])

    assert response.status_code == 201




# -----------------------------------------------------------
# list — the direction whitelist
# -----------------------------------------------------------

def test_every_near_miss_direction_is_refused(client, actor):
    _, headers = actor

    for value in ("Sent", "RECEIVED", "sent ", " received", "all", "both",
                  "0", "1", "true", "send", "recieved", "sent,received"):
        response = _requests_of(client, headers, direction=value)
        assert response.status_code == 400, value
        assert response.get_json()["code"] == "invalid_direction", value
        assert response.get_json()["error"].startswith("direction must be")


def test_a_repeated_direction_parameter_uses_the_first_one(client, duo):
    (ana, ana_headers), (benas, _) = duo
    _send(client, ana_headers, benas["id"])

    response = client.get(_REQUESTS, query_string="direction=sent&direction=received",
                          headers=ana_headers)

    # request.args.get takes the first value, so this is the
    # SENT list — Ana's own outgoing request
    assert response.status_code == 200
    assert [r["userId"] for r in response.get_json()["requests"]] == [benas["id"]]


def test_the_direction_check_runs_before_pagination(client, actor):
    _, headers = actor

    response = _requests_of(client, headers, direction="nonsense", per_page=999)

    assert response.get_json()["code"] == "invalid_direction"




# -----------------------------------------------------------
# list — the envelope and both directions
# -----------------------------------------------------------

@pytest.mark.contract
def test_the_requests_envelope_carries_exactly_three_keys(client, actor):
    _, headers = actor

    body = _requests_of(client, headers).get_json()

    assert set(body) == {"requests", "total", "hasMore"}
    assert body == {"requests": [], "total": 0, "hasMore": False}


def test_the_sent_list_names_the_recipient_and_the_received_list_the_sender(client, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    _send(client, ana_headers, benas["id"])

    sent = _requests_of(client, ana_headers, direction="sent").get_json()
    received = _requests_of(client, benas_headers, direction="received").get_json()

    assert [r["userId"] for r in sent["requests"]] == [benas["id"]]
    assert [r["userId"] for r in received["requests"]] == [ana["id"]]
    assert sent["requests"][0]["id"] == received["requests"][0]["id"]


def test_a_sent_request_is_absent_from_the_senders_received_list(client, duo):
    (_, ana_headers), (benas, _) = duo
    _send(client, ana_headers, benas["id"])

    body = _requests_of(client, ana_headers, direction="received").get_json()

    assert body["requests"] == [] and body["total"] == 0


def test_the_list_never_shows_a_third_partys_requests(client, trio):
    (_, ana_headers), (benas, benas_headers), (ceslovas, _) = trio
    _send(client, benas_headers, ceslovas["id"])

    received = _requests_of(client, ana_headers, direction="received").get_json()
    sent = _requests_of(client, ana_headers, direction="sent").get_json()

    assert received["total"] == 0 and sent["total"] == 0


def test_only_pending_rows_are_listed_in_either_direction(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    _seed_request(db, ana["id"], benas["id"], status="rejected")
    _seed_request(db, benas["id"], ana["id"], status="accepted")

    sent = _requests_of(client, ana_headers, direction="sent").get_json()
    received = _requests_of(client, ana_headers, direction="received").get_json()

    assert sent["total"] == 0 and sent["requests"] == []
    assert received["total"] == 0 and received["requests"] == []


def test_the_row_carries_the_other_partys_avatar_and_role(client, db, make_user, auth_headers, actor):
    me, headers = actor
    dean = make_user(username="dekanas", display_name="Dekanas", role="curator")
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?",
               ("/api/uploads/dekanas.jpg", dean["id"]))
    db.commit()
    _send(client, auth_headers(dean), me["id"])

    row = _requests_of(client, headers).get_json()["requests"][0]

    assert row["role"] == "curator"
    assert row["avatarUrl"] == "/api/uploads/dekanas.jpg"
    assert row["displayName"] == "Dekanas"


def test_markup_in_a_display_name_leaves_the_list_escaped(client, make_user, auth_headers, actor):
    me, headers = actor
    joker = make_user(username="jokeris", display_name="<b>Jokeris</b>")
    _send(client, auth_headers(joker), me["id"])

    row = _requests_of(client, headers).get_json()["requests"][0]

    assert row["displayName"] == "&lt;b&gt;Jokeris&lt;/b&gt;"


def test_a_deactivated_counterparty_drops_out_of_the_total_too(client, db, trio):
    (me, headers), (benas, benas_headers), (ceslovas, ceslovas_headers) = trio
    _send(client, benas_headers, me["id"])
    _send(client, ceslovas_headers, me["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (benas["id"],))
    db.commit()

    body = _requests_of(client, headers).get_json()

    assert [r["userId"] for r in body["requests"]] == [ceslovas["id"]]
    assert body["total"] == 1


def test_the_listed_created_at_is_the_stored_sort_key(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    row = _requests_of(client, benas_headers).get_json()["requests"][0]

    assert row["createdAt"] == _row(db, request_id)["created_at"]


def test_the_listed_id_is_the_one_accept_takes(client, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    _send(client, ana_headers, benas["id"])

    # The profile screen's real flow: read the list purely to
    # find the id, then settle it
    listed = _requests_of(client, benas_headers).get_json()["requests"][0]["id"]

    assert _accept(client, benas_headers, listed).status_code == 200


def test_the_listed_id_is_the_one_reject_takes(client, duo):
    (_, ana_headers), (benas, _) = duo
    _send(client, ana_headers, benas["id"])

    listed = _requests_of(client, ana_headers, direction="sent").get_json()["requests"][0]["id"]

    assert _reject(client, ana_headers, listed).status_code == 200


def test_a_reactivated_counterparty_comes_back_to_the_list(client, db, duo):
    (me, headers), (benas, benas_headers) = duo
    _send(client, benas_headers, me["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (benas["id"],))
    db.commit()
    assert _requests_of(client, headers).get_json()["total"] == 0

    db.execute("UPDATE users SET active = 1 WHERE id = ?", (benas["id"],))
    db.commit()

    body = _requests_of(client, headers).get_json()
    assert [r["userId"] for r in body["requests"]] == [benas["id"]]
    assert body["total"] == 1


def test_unknown_query_parameters_are_ignored_by_the_list(client, duo):
    (ana, ana_headers), (benas, _) = duo
    _send(client, ana_headers, benas["id"])

    body = _requests_of(client, ana_headers, direction="sent",
                        status="rejected", user_id="whoever", sort="oldest").get_json()

    assert [r["userId"] for r in body["requests"]] == [benas["id"]]


def test_the_list_is_not_rate_limited(client, actor):
    _, headers = actor

    # No @rate_limit on this route: the badge polls it, and 15
    # reads in a window must all be 200
    for _ in range(15):
        assert _requests_of(client, headers).status_code == 200




# -----------------------------------------------------------
# list — ordering and paging to the last integer
# -----------------------------------------------------------

def test_a_created_at_tie_is_broken_by_id_descending(client, db, actor):
    me, headers = actor
    first = _seed_user(db, "pirmas")
    second = _seed_user(db, "antras")
    same_moment = _stamp()
    _seed_request(db, first["id"], me["id"], created_at=same_moment, request_id="aaa-request")
    _seed_request(db, second["id"], me["id"], created_at=same_moment, request_id="bbb-request")

    ids = [r["id"] for r in _requests_of(client, headers).get_json()["requests"]]

    assert ids == ["bbb-request", "aaa-request"]


def test_the_page_size_boundaries_are_one_and_the_cap(client, duo):
    (ana, ana_headers), (benas, _) = duo
    _send(client, ana_headers, benas["id"])

    for size in (1, _LIST_CAP):
        response = _requests_of(client, ana_headers, direction="sent", per_page=size)
        assert response.status_code == 200, size
        assert len(response.get_json()["requests"]) == 1


def test_every_out_of_range_page_size_is_refused(client, actor):
    _, headers = actor

    for size in (0, -1, _LIST_CAP + 1, 10_000):
        response = _requests_of(client, headers, per_page=size)
        assert response.status_code == 400, size
    assert _requests_of(client, headers, per_page=_LIST_CAP + 1).get_json()["error"] == \
        "per_page must be at most 200"
    assert _requests_of(client, headers, per_page=0).get_json()["error"] == \
        "per_page must be a positive integer"


def test_every_unparseable_page_size_is_refused(client, actor):
    _, headers = actor

    for size in ("", "abc", "1.0", "2e1", "0x10", "  ", "1,2"):
        response = _requests_of(client, headers, per_page=size)
        assert response.status_code == 400, size
        assert response.get_json()["error"] == "per_page must be a positive integer", size


def test_a_padded_or_signed_page_size_is_accepted(client, actor):
    _, headers = actor

    # int() is the whole parser, so it brings its own dialect:
    # surrounding whitespace, an explicit "+" and — the one that
    # surprises — non-ASCII decimal digits all parse
    for size in (" 3 ", "+3", "3\n", "١٢٣"):
        assert _requests_of(client, headers, per_page=size).status_code == 200, size


def test_the_page_boundaries_are_one_and_the_cap(client, actor):
    _, headers = actor

    for page in (1, _LIST_CAP):
        response = _requests_of(client, headers, page=page)
        assert response.status_code == 200, page
        assert response.get_json()["requests"] == []


def test_every_out_of_range_page_is_refused(client, actor):
    _, headers = actor

    assert _requests_of(client, headers, page=0).get_json()["error"] == \
        "page must be a positive integer"
    assert _requests_of(client, headers, page=-1).get_json()["error"] == \
        "page must be a positive integer"
    assert _requests_of(client, headers, page=_LIST_CAP + 1).get_json()["error"] == \
        "page must be at most 200"
    assert _requests_of(client, headers, page="abc").status_code == 400


def test_paging_walks_the_whole_set_without_repeating_a_row(client, db, actor):
    me, headers = actor
    senders = [_seed_user(db, f"siuntejas{i}") for i in range(5)]
    for index, sender in enumerate(senders):
        _seed_request(db, sender["id"], me["id"],
                      created_at=_stamp(-timedelta(minutes=index)))

    seen = []
    for page in (1, 2, 3):
        body = _requests_of(client, headers, page=page, per_page=2).get_json()
        seen.extend(r["userId"] for r in body["requests"])
        assert body["total"] == 5
        assert body["hasMore"] is (page < 3)

    assert seen == [s["id"] for s in senders]


def test_a_page_past_the_end_is_empty_but_keeps_the_total(client, duo):
    (ana, ana_headers), (benas, _) = duo
    _send(client, ana_headers, benas["id"])

    body = _requests_of(client, ana_headers, direction="sent", page=50, per_page=1).get_json()

    assert body["requests"] == []
    assert body["total"] == 1
    assert body["hasMore"] is False


def test_hasmore_is_false_when_the_page_exactly_covers_the_set(client, db, actor):
    me, headers = actor
    for index in range(3):
        _seed_request(db, _seed_user(db, f"lygiai{index}")["id"], me["id"])

    body = _requests_of(client, headers, per_page=3).get_json()

    assert len(body["requests"]) == 3
    assert body["hasMore"] is False


def test_the_default_page_size_swallows_a_hundred_requests(client, db, actor):
    me, headers = actor
    for index in range(100):
        _seed_request(db, _seed_user(db, f"masinis{index}")["id"], me["id"])

    body = _requests_of(client, headers).get_json()

    # _LIST_PER_PAGE is the DEFAULT as well as the cap, so the
    # app's un-paged call still gets every row
    assert len(body["requests"]) == 100
    assert body["total"] == 100
    assert body["hasMore"] is False




# -----------------------------------------------------------
# accept — who may, and what it leaves behind
# -----------------------------------------------------------

@pytest.mark.contract
def test_accepting_answers_exactly_the_status_key(client, duo):
    (_, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _accept(client, benas_headers, request_id)

    assert response.status_code == 200
    assert response.get_json() == {"status": "accepted"}


def test_accepting_keeps_an_existing_friendship_stamp(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _seed_friendship(db, ana["id"], benas["id"], created_at="2019-05-05T05:05:05+00:00")

    assert _accept(client, benas_headers, request_id).status_code == 200

    kept = db.execute(
        "SELECT created_at FROM friendships WHERE user_id = ? AND friend_id = ?",
        (ana["id"], benas["id"]),
    ).fetchone()["created_at"]
    assert kept == "2019-05-05T05:05:05+00:00"
    assert _friendship_pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}


def test_accepting_a_leftover_row_between_friends_settles_it(client, db, duo):
    (ana, _), (benas, benas_headers) = duo
    _seed_friendship(db, ana["id"], benas["id"])
    _seed_friendship(db, benas["id"], ana["id"])
    leftover = _seed_request(db, ana["id"], benas["id"])

    response = _accept(client, benas_headers, leftover)

    assert response.status_code == 200
    assert _rows(db) == []
    assert len(_friendship_pairs(db)) == 2


def test_accepting_touches_only_the_named_request(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, ceslovas_headers) = trio
    mine = _send(client, ana_headers, ceslovas["id"]).get_json()["id"]
    other = _seed_request(db, benas["id"], ceslovas["id"])

    _accept(client, ceslovas_headers, mine)

    assert [r["id"] for r in _rows(db)] == [other]
    assert _friendship_pairs(db) == {(ana["id"], ceslovas["id"]), (ceslovas["id"], ana["id"])}


def test_accepting_an_accepted_status_row_is_a_404(client, db, duo):
    (ana, _), (benas, benas_headers) = duo
    settled = _seed_request(db, ana["id"], benas["id"], status="accepted")

    response = _accept(client, benas_headers, settled)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Friend request not found"
    assert _friendship_pairs(db) == set()


def test_accepting_a_rejected_status_row_is_a_404(client, db, duo):
    (ana, _), (benas, benas_headers) = duo
    declined = _seed_request(db, ana["id"], benas["id"], status="rejected")

    assert _accept(client, benas_headers, declined).status_code == 404
    assert _row(db, declined)["status"] == "rejected"


def test_the_sender_may_not_accept_and_the_row_survives(client, db, duo):
    (_, ana_headers), (benas, _) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    assert _accept(client, ana_headers, request_id).status_code == 404
    assert _row(db, request_id)["status"] == "pending"
    assert _friendship_pairs(db) == set()


def test_an_admin_may_not_accept_someone_elses_request(client, db, duo, admin):
    (ana, ana_headers), (benas, _) = duo
    _, admin_headers = admin
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    # Ownership, not role: there is no admin override here
    assert _accept(client, admin_headers, request_id).status_code == 404
    assert _row(db, request_id)["status"] == "pending"


def test_accepting_a_request_from_a_deactivated_sender_still_works(client, db, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (ana["id"],))
    db.commit()

    # The list hides the row, but the id still settles it — the
    # friendship simply stays invisible while the account is off
    assert _accept(client, benas_headers, request_id).status_code == 200
    assert len(_friendship_pairs(db)) == 2
    assert client.get(_FRIENDS, headers=benas_headers).get_json()["friends"] == []


def test_accepting_a_self_addressed_row_makes_a_self_friendship(client, db, actor):
    me, headers = actor
    # send_friend_request can never create this; a hand-edited or
    # migrated row can, and accept settles it into ONE row
    loop = _seed_request(db, me["id"], me["id"])

    assert _accept(client, headers, loop).status_code == 200
    assert _friendship_pairs(db) == {(me["id"], me["id"])}


def test_every_shape_of_unknown_request_id_is_a_404(client, actor):
    _, headers = actor

    for request_id in ("no-such-request", str(uuid.uuid4()), "x" * 500, "%20", "null", "0"):
        assert _accept(client, headers, request_id).status_code == 404, request_id


def test_an_empty_or_slashed_request_id_never_matches_the_route(client, actor):
    _, headers = actor

    assert client.post(f"{_REQUESTS}//accept", headers=headers).status_code == 404
    assert client.post(f"{_REQUESTS}/a/b/accept", headers=headers).status_code == 404


def test_accepting_twice_is_a_404_the_second_time(client, db, duo):
    (_, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    assert _accept(client, benas_headers, request_id).status_code == 200
    assert _accept(client, benas_headers, request_id).status_code == 404
    assert len(_friendship_pairs(db)) == 2


def test_accepting_after_the_other_side_auto_accepted_is_a_404(client, duo):
    (ana, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    # Benas asks back, which auto-accepts and deletes the row
    _send(client, benas_headers, ana["id"])

    assert _accept(client, benas_headers, request_id).status_code == 404




# -----------------------------------------------------------
# reject — the two settlements behind one wire shape
# -----------------------------------------------------------

@pytest.mark.contract
def test_rejecting_answers_exactly_the_status_key(client, duo):
    (_, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _reject(client, benas_headers, request_id)

    assert response.status_code == 200
    assert response.get_json() == {"status": "rejected"}


def test_the_recipients_decline_keeps_the_row_and_restamps_it(client, db, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
        request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
        created_at = _row(db, request_id)["created_at"]
        traveller.move_to(_T0 + timedelta(hours=3))
        _reject(client, benas_headers, request_id)

    row = _row(db, request_id)
    assert row["status"] == "rejected"
    assert row["created_at"] == created_at
    assert row["updated_at"] == (_T0 + timedelta(hours=3)).isoformat()


def test_the_senders_cancel_deletes_the_row_outright(client, db, duo):
    (_, ana_headers), (benas, _) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    assert _reject(client, ana_headers, request_id).status_code == 200
    assert _row(db, request_id) is None
    assert _rows(db) == []


def test_a_cancelled_request_may_be_sent_again_at_once(client, duo):
    (_, ana_headers), (benas, _) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _reject(client, ana_headers, request_id)

    assert _send(client, ana_headers, benas["id"]).status_code == 201


def test_a_self_addressed_row_is_settled_as_a_cancel(client, db, actor):
    me, headers = actor
    loop = _seed_request(db, me["id"], me["id"])

    # from_user_id == the caller wins the branch, so the row is
    # deleted rather than kept as a rejection against himself
    assert _reject(client, headers, loop).status_code == 200
    assert _row(db, loop) is None


def test_rejecting_touches_no_other_row(client, db, trio):
    (ana, ana_headers), (benas, _), (ceslovas, ceslovas_headers) = trio
    mine = _send(client, ana_headers, ceslovas["id"]).get_json()["id"]
    other = _seed_request(db, benas["id"], ceslovas["id"])

    _reject(client, ceslovas_headers, mine)

    assert _row(db, other)["status"] == "pending"
    assert _row(db, mine)["status"] == "rejected"


def test_a_third_party_may_not_reject_and_the_row_survives(client, db, trio):
    (_, ana_headers), (benas, _), (_, ceslovas_headers) = trio
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    assert _reject(client, ceslovas_headers, request_id).status_code == 404
    assert _row(db, request_id)["status"] == "pending"


def test_an_admin_may_not_reject_someone_elses_request(client, db, duo, admin):
    (_, ana_headers), (benas, _) = duo
    _, admin_headers = admin
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    assert _reject(client, admin_headers, request_id).status_code == 404
    assert _row(db, request_id)["status"] == "pending"


def test_rejecting_an_accepted_status_row_is_a_404(client, db, duo):
    (ana, _), (benas, benas_headers) = duo
    settled = _seed_request(db, ana["id"], benas["id"], status="accepted")

    assert _reject(client, benas_headers, settled).status_code == 404
    assert _row(db, settled)["status"] == "accepted"


def test_declining_twice_is_a_404_the_second_time(client, db, duo):
    (_, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    assert _reject(client, benas_headers, request_id).status_code == 200
    assert _reject(client, benas_headers, request_id).status_code == 404
    assert _row(db, request_id)["status"] == "rejected"


def test_the_sender_may_not_cancel_a_request_already_declined(client, db, duo):
    (_, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _reject(client, benas_headers, request_id)

    assert _reject(client, ana_headers, request_id).status_code == 404
    assert _row(db, request_id)["status"] == "rejected"


def test_every_shape_of_unknown_request_id_is_a_404_on_reject(client, actor):
    _, headers = actor

    for request_id in ("no-such-request", str(uuid.uuid4()), "x" * 500, "null"):
        assert _reject(client, headers, request_id).status_code == 404, request_id


def test_the_decline_stamp_is_what_the_cooldown_counts_from(client, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
        request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
        # Declined three days AFTER it was sent — the window runs
        # from the decline, not from the ask
        traveller.move_to(_T0 + timedelta(days=3))
        _reject(client, benas_headers, request_id)

        traveller.move_to(_T0 + timedelta(days=3) + _COOLDOWN - timedelta(seconds=1))
        blocked = _send(client, ana_headers, benas["id"])
        traveller.move_to(_T0 + timedelta(days=3) + _COOLDOWN)
        allowed = _send(client, ana_headers, benas["id"])

    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "friend_request_cooldown"
    assert allowed.status_code == 201




# -----------------------------------------------------------
# The four routes answer their own verb and nothing else
# -----------------------------------------------------------

def test_the_send_route_answers_only_to_post(client, actor):
    _, headers = actor

    for call in (client.get, client.put, client.patch):
        assert call(_SEND, headers=headers).status_code == 405


def test_the_requests_list_answers_only_to_get(client, actor):
    _, headers = actor

    for call in (client.post, client.put, client.patch):
        assert call(_REQUESTS, headers=headers).status_code == 405


def test_accept_and_reject_answer_only_to_post(client, actor):
    _, headers = actor

    for path in (f"{_REQUESTS}/whatever/accept", f"{_REQUESTS}/whatever/reject"):
        assert client.get(path, headers=headers).status_code == 405
        assert client.delete(path, headers=headers).status_code == 405




# -----------------------------------------------------------
# accept / reject — the shared action budget
# -----------------------------------------------------------
#
# Both routes carry @rate_limit("friendaction", 60), which is
# ONE bucket per user: sixty calls of either kind exhaust it,
# and a 404 costs the same as a settled request because the
# decorator spends before the handler runs.
# -----------------------------------------------------------

def test_the_sixty_first_friend_action_in_a_window_is_refused(client, actor):
    _, headers = actor

    for _ in range(_ACTION_BUDGET):
        assert _accept(client, headers, "no-such-request").status_code == 404

    response = _accept(client, headers, "no-such-request")

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


def test_accept_and_reject_draw_on_the_same_budget(client, duo):
    (_, ana_headers), (benas, benas_headers) = duo

    for _ in range(_ACTION_BUDGET // 2):
        assert _accept(client, benas_headers, "nope").status_code == 404
    for _ in range(_ACTION_BUDGET // 2):
        assert _reject(client, benas_headers, "nope").status_code == 404

    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    response = _accept(client, benas_headers, request_id)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_the_action_budget_comes_back_with_the_next_window(client, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
        request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
        for _ in range(_ACTION_BUDGET):
            _reject(client, benas_headers, "nope")
        assert _accept(client, benas_headers, request_id).status_code == 429

        # The window is monotonic seconds, which time_machine
        # moves too — no test here ever sleeps
        traveller.move_to(_T0 + timedelta(minutes=6))
        response = _accept(client, benas_headers, request_id)

    assert response.status_code == 200


def test_an_unauthenticated_action_never_spends_the_budget(client, duo):
    (_, ana_headers), (benas, benas_headers) = duo
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    for _ in range(_ACTION_BUDGET + 5):
        assert _accept(client, {}, request_id).status_code == 401

    response = _accept(client, benas_headers, request_id)

    assert response.status_code == 200


def test_the_action_budget_is_kept_per_caller(client, trio):
    (ana, ana_headers), (benas, benas_headers), (ceslovas, ceslovas_headers) = trio
    request_id = _send(client, ana_headers, ceslovas["id"]).get_json()["id"]

    for _ in range(_ACTION_BUDGET):
        _reject(client, benas_headers, "nope")
    assert _reject(client, benas_headers, "nope").status_code == 429

    response = _accept(client, ceslovas_headers, request_id)

    assert response.status_code == 200
