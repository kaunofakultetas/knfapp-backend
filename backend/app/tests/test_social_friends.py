# -----------------------------------------------------------
#  [*] Tests — social friendships
#
#  Everything the friend state machine in app/social/routes.py
#  promises, proved through the wire the mobile app uses:
#
#    - send: 201 "pending", and the 200 "accepted" auto-accept
#      when the target had already asked us — the two shapes
#      services/api/social.ts branches on
#    - every guard on the way there: self-request, a non-string
#      user_id, an unknown or deactivated target, an existing
#      friendship, an already-pending pair, and the lost INSERT
#      race that migration v21's unique index must turn into a
#      409 rather than a 500
#    - the two brakes on request spam: 20 sends per 5-minute
#      window and the 7-day post-rejection cooldown (with its
#      boundary and its opportunistic purge), both driven with
#      time_machine — never the wall clock
#    - accept / decline / cancel, including the 404s that keep
#      other people's requests invisible and the OR IGNORE
#      paths a mutual-send race leaves behind
#    - the friends and requests lists — deactivated accounts
#      dropping out, the case-insensitive order, paging and its
#      limits — and unfriend, which must clear a half-written
#      friendship from either side
#
#  Two backend fixes are pinned here as regressions:
#  cancelling your OWN request no longer locks the pair out for
#  a week, and a new request row is stamped in the house T-form
#  so the request list cannot mis-sort against a row migration
#  v17 normalised.
# -----------------------------------------------------------

from datetime import datetime, timedelta, timezone

import pytest
import time_machine


_SEND = "/api/social/friends/request"
_REQUESTS = "/api/social/friends/requests"
_FRIENDS = "/api/social/friends"

# The instant every clock-travelling test starts from. It is
# in the FUTURE on purpose: a session minted inside the
# traveller lives 30 days from there, so a test may move on a
# week and still be authenticated, and no login has to happen
# outside the frozen clock.
_T0 = datetime(2027, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

# The route's own quotas, restated so a test names the number
# it is standing on
_MAX_SENDS_PER_WINDOW = 20
_COOLDOWN = timedelta(days=7)




# -----------------------------------------------------------
# fresh_rate_limits
# -----------------------------------------------------------
#
# auth/routes.py keeps its rate-limit windows in a module-level
# dict, so they outlive the per-test database and leak from one
# test into the next — including the global per-IP budget that
# EVERY request in the suite spends. Clearing it around each
# test is what makes the 429 assertions below mean the quota
# under test, and stops this module from spending another
# module's budget. time_machine also moves time.monotonic, so
# a stamp recorded inside a traveller would otherwise look
# eternally fresh to the tests that follow.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_rate_limits(app):
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# pair
# -----------------------------------------------------------
#
# Two signed-in students, ((user, headers), (user, headers)) —
# the arrangement almost every test here starts from.
# -----------------------------------------------------------

@pytest.fixture
def pair(make_user, auth_headers):
    ana = make_user(username="ana", display_name="Ana")
    benas = make_user(username="benas", display_name="Benas")
    return (ana, auth_headers(ana)), (benas, auth_headers(benas))




# -----------------------------------------------------------
# Wire helpers — the four friend calls the app makes
# -----------------------------------------------------------

def _send(client, headers, target_id):
    return client.post(_SEND, json={"user_id": target_id}, headers=headers)


def _accept(client, headers, request_id):
    return client.post(f"{_REQUESTS}/{request_id}/accept", headers=headers)


def _reject(client, headers, request_id):
    return client.post(f"{_REQUESTS}/{request_id}/reject", headers=headers)


def _unfriend(client, headers, user_id):
    return client.delete(f"{_FRIENDS}/{user_id}", headers=headers)


def _friend_ids(client, headers):
    return [f["id"] for f in client.get(_FRIENDS, headers=headers).get_json()["friends"]]


def _request_user_ids(client, headers, direction="received"):
    response = client.get(_REQUESTS, query_string={"direction": direction}, headers=headers)
    return [r["userId"] for r in response.get_json()["requests"]]




# -----------------------------------------------------------
# _friendship_pairs / _seed_friendship
# -----------------------------------------------------------
#
# friendships holds ONE ROW PER DIRECTION and every reader
# (feed, counts, lists) only ever looks up its own direction,
# so "are they friends" is a question about a SET of pairs —
# asserting on the set is what catches a route that writes
# only half of one. _seed_friendship plants a single direction
# on purpose: that is the half-written state unfriend and the
# auto-accept have to survive.
# -----------------------------------------------------------

def _friendship_pairs(db):
    return {
        (r["user_id"], r["friend_id"])
        for r in db.execute("SELECT user_id, friend_id FROM friendships").fetchall()
    }


def _seed_friendship(db, user_id, friend_id):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user_id, friend_id))
    db.commit()


def _request_rows(db):
    return db.execute(
        "SELECT id, from_user_id, to_user_id, status, created_at, updated_at FROM friend_requests"
    ).fetchall()




# -----------------------------------------------------------
# _arm_pending_row_race
# -----------------------------------------------------------
#
# Plays the second tab that wins the race: a BEFORE INSERT
# trigger that plants the competing pending row for the very
# pair the route is about to insert — landing exactly in the
# window between the route's duplicate check and its own
# INSERT. Migration v21's partial unique index then rejects
# the route's row with a real IntegrityError, so the 409 path
# is proved against the actual database constraint rather than
# a patched-out sqlite3.
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
# Sending a request — the 201 shape
# -----------------------------------------------------------

def test_sending_a_request_creates_one_pending_row(client, db, pair):
    (ana, ana_headers), (benas, _) = pair

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201
    body = response.get_json()
    assert body["status"] == "pending"
    row = db.execute(
        "SELECT from_user_id, to_user_id, status FROM friend_requests WHERE id = ?",
        (body["id"],),
    ).fetchone()
    assert (row["from_user_id"], row["to_user_id"], row["status"]) == (ana["id"], benas["id"], "pending")
    # A request is not a friendship until somebody accepts it
    assert _friendship_pairs(db) == set()


@pytest.mark.contract
def test_the_send_response_carries_the_id_and_status_the_app_switches_on(client, pair):
    (_, ana_headers), (benas, _) = pair

    body = _send(client, ana_headers, benas["id"]).get_json()

    assert isinstance(body["id"], str) and body["id"]
    assert body["status"] == "pending"


def test_a_request_shows_up_on_both_sides_of_the_request_lists(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair

    _send(client, ana_headers, benas["id"])

    assert _request_user_ids(client, benas_headers, "received") == [ana["id"]]
    assert _request_user_ids(client, ana_headers, "sent") == [benas["id"]]
    assert _request_user_ids(client, ana_headers, "received") == []
    assert _request_user_ids(client, benas_headers, "sent") == []


def test_a_request_may_be_sent_to_any_role(client, admin, actor):
    _, actor_headers = actor
    admin_user, _ = admin

    response = _send(client, actor_headers, admin_user["id"])

    assert response.status_code == 201


def test_a_new_request_is_stamped_in_the_house_T_form(client, db, pair):
    (_, ana_headers), (benas, _) = pair

    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    row = db.execute(
        "SELECT created_at, updated_at FROM friend_requests WHERE id = ?", (request_id,)
    ).fetchone()
    # Space-form text sorts below every T-form value of the same
    # day, and migration v17 left T-form rows in this very column
    assert "T" in row["created_at"], f"space-form created_at: {row['created_at']!r}"
    assert "T" in row["updated_at"], f"space-form updated_at: {row['updated_at']!r}"


def test_requests_made_the_same_day_are_listed_newest_first(client, db, make_user, auth_headers):
    ana = make_user(username="ana", display_name="Ana")
    senas = make_user(username="senas", display_name="Senas")
    naujas = make_user(username="naujas", display_name="Naujas")
    ana_headers = auth_headers(ana)

    fresh_id = _send(client, auth_headers(naujas), ana["id"]).get_json()["id"]

    # The row shape migration v17 leaves behind (T-form, no
    # offset), one second OLDER than the row the route just
    # wrote — so a route that still writes space-form text puts
    # this legacy row on top of a newer one
    fresh_at = db.execute(
        "SELECT created_at FROM friend_requests WHERE id = ?", (fresh_id,)
    ).fetchone()["created_at"]
    legacy_at = (datetime.fromisoformat(fresh_at) - timedelta(seconds=1)).isoformat()
    db.execute(
        "INSERT INTO friend_requests (id, from_user_id, to_user_id, created_at, updated_at)"
        " VALUES ('legacy-row', ?, ?, ?, ?)",
        (senas["id"], ana["id"], legacy_at, legacy_at),
    )
    db.commit()

    assert _request_user_ids(client, ana_headers) == [naujas["id"], senas["id"]]




# -----------------------------------------------------------
# Sending a request — the 200 auto-accept
# -----------------------------------------------------------

def test_requesting_someone_who_already_asked_you_auto_accepts(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _send(client, benas_headers, ana["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert response.get_json()["status"] == "accepted"
    assert _friendship_pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}
    # The handshake row is gone — the friendships rows carry it
    assert _request_rows(db) == []


def test_the_auto_accept_leaves_both_friends_lists_populated(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _send(client, benas_headers, ana["id"])

    _send(client, ana_headers, benas["id"])

    assert _friend_ids(client, ana_headers) == [benas["id"]]
    assert _friend_ids(client, benas_headers) == [ana["id"]]
    assert _request_user_ids(client, ana_headers, "received") == []


def test_the_auto_accept_repairs_a_half_written_friendship(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _send(client, benas_headers, ana["id"])
    # Only the far direction of the friendship exists — a crash
    # between the two inserts of an earlier accept
    _seed_friendship(db, benas["id"], ana["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert _friendship_pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}




# -----------------------------------------------------------
# Sending a request — every guard
# -----------------------------------------------------------

def test_requesting_yourself_is_refused(client, db, actor):
    user, headers = actor

    response = _send(client, headers, user["id"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "Cannot friend yourself"
    assert _request_rows(db) == []


def test_a_missing_user_id_is_refused(client, actor):
    _, headers = actor

    response = client.post(_SEND, json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id required"


def test_a_blank_user_id_is_refused(client, actor):
    _, headers = actor

    response = _send(client, headers, "")

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id required"


def test_a_body_that_is_not_json_is_refused(client, actor):
    _, headers = actor

    response = client.post(_SEND, data="not json at all",
                           content_type="application/json", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id required"


def test_a_numeric_user_id_is_refused_instead_of_reaching_sqlite(client, actor):
    _, headers = actor

    response = _send(client, headers, 12345)

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id must be a string"


def test_a_user_id_that_is_a_list_is_refused(client, actor):
    _, headers = actor

    response = _send(client, headers, ["a", "b"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id must be a string"


def test_requesting_an_unknown_user_is_a_404(client, actor):
    _, headers = actor

    response = _send(client, headers, "no-such-user")

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_requesting_a_deactivated_user_is_a_404(client, db, make_user, actor):
    _, headers = actor
    gone = make_user(username="isjunges", active=0)

    response = _send(client, headers, gone["id"])

    assert response.status_code == 404
    assert _request_rows(db) == []


def test_requesting_an_existing_friend_is_a_409(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _seed_friendship(db, ana["id"], benas["id"])
    _seed_friendship(db, benas["id"], ana["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 409
    assert response.get_json()["error"] == "Already friends"


def test_a_second_request_to_the_same_person_is_a_409(client, db, pair):
    (_, ana_headers), (benas, _) = pair
    _send(client, ana_headers, benas["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 409
    assert response.get_json()["error"] == "Friend request already pending"
    assert len(_request_rows(db)) == 1


def test_a_lost_insert_race_is_a_409_not_a_500(client, db, pair):
    (_, ana_headers), (benas, _) = pair
    _arm_pending_row_race(db)

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 409
    assert response.get_json()["error"] == "Friend request already pending"


def test_sending_without_a_token_is_refused(client, actor):
    user, _ = actor

    response = _send(client, {}, user["id"])

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_a_deactivated_sender_loses_the_friend_routes(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (ana["id"],))
    db.commit()

    assert _send(client, ana_headers, benas["id"]).status_code == 401
    assert client.get(_FRIENDS, headers=ana_headers).status_code == 401
    assert client.get(_REQUESTS, headers=ana_headers).status_code == 401




# -----------------------------------------------------------
# The per-window send cap
# -----------------------------------------------------------
#
# The rate_limit decorator's budget is spent before the route
# body runs and keyed on the SENDER, so it is the one brake
# that a carpet-bombing account cannot dodge by varying the
# target. The window is 5 minutes of monotonic time —
# time_machine moves that too, which is why the recovery half
# of the test never sleeps.
# -----------------------------------------------------------

def test_the_twenty_first_send_in_a_window_is_rate_limited(client, make_user, auth_headers):
    sender = make_user(username="ana")
    targets = [make_user(username=f"target{i}") for i in range(_MAX_SENDS_PER_WINDOW + 1)]

    with time_machine.travel(_T0, tick=False):
        headers = auth_headers(sender)
        for target in targets[:_MAX_SENDS_PER_WINDOW]:
            assert _send(client, headers, target["id"]).status_code == 201

        response = _send(client, headers, targets[-1]["id"])

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


def test_the_send_budget_comes_back_when_the_window_moves_on(client, make_user, auth_headers):
    sender = make_user(username="ana")
    targets = [make_user(username=f"target{i}") for i in range(_MAX_SENDS_PER_WINDOW + 1)]

    with time_machine.travel(_T0, tick=False) as traveller:
        headers = auth_headers(sender)
        for target in targets[:_MAX_SENDS_PER_WINDOW]:
            _send(client, headers, target["id"])
        assert _send(client, headers, targets[-1]["id"]).status_code == 429

        traveller.move_to(_T0 + timedelta(minutes=6))
        response = _send(client, headers, targets[-1]["id"])

    assert response.status_code == 201


def test_a_refused_send_still_costs_a_send(client, make_user, auth_headers):
    sender = make_user(username="ana")
    target = make_user(username="benas")

    with time_machine.travel(_T0, tick=False):
        headers = auth_headers(sender)
        # Every one of these is a 404 — the budget is spent by the
        # decorator, above the route, so spam cannot be free
        for _ in range(_MAX_SENDS_PER_WINDOW):
            assert _send(client, headers, "no-such-user").status_code == 404

        response = _send(client, headers, target["id"])

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"




# -----------------------------------------------------------
# The post-rejection cooldown
# -----------------------------------------------------------

def test_a_request_inside_the_rejection_cooldown_is_refused(client, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
        request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
        assert _reject(client, benas_headers, request_id).status_code == 200

        traveller.move_to(_T0 + _COOLDOWN - timedelta(seconds=1))
        response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 429
    assert response.get_json()["code"] == "friend_request_cooldown"


def test_a_rejection_exactly_a_cooldown_old_no_longer_blocks(client, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
        request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
        _reject(client, benas_headers, request_id)

        # The window is "> cutoff", so the instant it ages to
        # exactly _FRIEND_REQUEST_COOLDOWN_DAYS it is over
        traveller.move_to(_T0 + _COOLDOWN)
        response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201


def test_the_cooldown_expires_and_stale_rejections_are_purged(client, db, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")
    carl = make_user(username="carlas")
    dana = make_user(username="dana")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
        dana_headers = auth_headers(dana)
        first = _send(client, ana_headers, benas["id"]).get_json()["id"]
        _reject(client, benas_headers, first)
        # An unrelated pair's rejection, which the opportunistic
        # purge must also drop once it has outlived the window
        other = _send(client, auth_headers(carl), dana["id"]).get_json()["id"]
        _reject(client, dana_headers, other)

        traveller.move_to(_T0 + _COOLDOWN + timedelta(days=1))
        response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201
    statuses = [r["status"] for r in _request_rows(db)]
    assert statuses == ["pending"], "friend_requests kept a record of who declined whom"


def test_the_cooldown_only_binds_the_sender_who_was_declined(client, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
        request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
        _reject(client, benas_headers, request_id)

        traveller.move_to(_T0 + timedelta(days=1))
        # The person who declined may still ask the other way round
        response = _send(client, benas_headers, ana["id"])

    assert response.status_code == 201


def test_the_cooldown_does_not_leak_to_a_third_party(client, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")
    carl = make_user(username="carlas")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers, benas_headers = auth_headers(ana), auth_headers(benas)
        request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
        _reject(client, benas_headers, request_id)

        traveller.move_to(_T0 + timedelta(days=1))
        response = _send(client, ana_headers, carl["id"])

    assert response.status_code == 201


def test_cancelling_your_own_request_does_not_lock_the_pair_out(client, make_user, auth_headers):
    ana = make_user(username="ana")
    benas = make_user(username="benas")

    with time_machine.travel(_T0, tick=False) as traveller:
        ana_headers = auth_headers(ana)
        request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
        # The app's "cancel sent request" button is this route,
        # called by the SENDER — nobody declined anything
        assert _reject(client, ana_headers, request_id).status_code == 200

        traveller.move_to(_T0 + timedelta(minutes=1))
        response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201, response.get_json()




# -----------------------------------------------------------
# Accepting
# -----------------------------------------------------------

def test_accepting_makes_both_directions_friends(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _accept(client, benas_headers, request_id)

    assert response.status_code == 200
    assert response.get_json()["status"] == "accepted"
    assert _friendship_pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}
    # The settled handshake row is deleted, not archived
    assert _request_rows(db) == []


def test_an_accepted_request_leaves_both_request_lists(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    _accept(client, benas_headers, request_id)

    assert _request_user_ids(client, benas_headers, "received") == []
    assert _request_user_ids(client, ana_headers, "sent") == []
    assert _friend_ids(client, ana_headers) == [benas["id"]]


def test_the_sender_cannot_accept_their_own_request(client, db, pair):
    (_, ana_headers), (benas, _) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _accept(client, ana_headers, request_id)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Friend request not found"
    assert _friendship_pairs(db) == set()


def test_a_third_party_cannot_accept_someone_elses_request(client, db, pair, make_user, auth_headers):
    (_, ana_headers), (benas, _) = pair
    nosy = make_user(username="smalsute")
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _accept(client, auth_headers(nosy), request_id)

    assert response.status_code == 404
    assert _friendship_pairs(db) == set()


def test_accepting_an_unknown_request_is_a_404(client, actor):
    _, headers = actor

    response = _accept(client, headers, "no-such-request")

    assert response.status_code == 404


def test_accepting_a_request_twice_is_a_404(client, pair):
    (_, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _accept(client, benas_headers, request_id)

    response = _accept(client, benas_headers, request_id)

    assert response.status_code == 404


def test_accepting_a_declined_request_is_a_404(client, db, pair):
    (_, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _reject(client, benas_headers, request_id)

    response = _accept(client, benas_headers, request_id)

    assert response.status_code == 404
    assert _friendship_pairs(db) == set()


def test_accepting_a_leftover_request_between_friends_settles_it(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    # What a mutual-send race leaves: the pair is already
    # friends and a second pending row is still lying around
    _seed_friendship(db, ana["id"], benas["id"])
    _seed_friendship(db, benas["id"], ana["id"])
    db.execute(
        "INSERT INTO friend_requests (id, from_user_id, to_user_id) VALUES ('leftover', ?, ?)",
        (benas["id"], ana["id"]),
    )
    db.commit()

    response = _accept(client, ana_headers, "leftover")

    assert response.status_code == 200
    assert _friendship_pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}
    assert _request_rows(db) == []


def test_accepting_without_a_token_is_refused(client, pair):
    (_, ana_headers), (benas, _) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _accept(client, {}, request_id)

    assert response.status_code == 401




# -----------------------------------------------------------
# Declining and cancelling
# -----------------------------------------------------------

def test_the_recipient_can_decline_and_the_record_is_kept(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _reject(client, benas_headers, request_id)

    assert response.status_code == 200
    assert response.get_json()["status"] == "rejected"
    row = db.execute(
        "SELECT status, updated_at FROM friend_requests WHERE id = ?", (request_id,)
    ).fetchone()
    # Kept — it is the cooldown's only record — and stamped in
    # the T-form the cooldown comparison relies on
    assert row["status"] == "rejected"
    assert "T" in row["updated_at"]
    assert _friendship_pairs(db) == set()


def test_a_declined_request_leaves_both_request_lists(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    _reject(client, benas_headers, request_id)

    assert _request_user_ids(client, benas_headers, "received") == []
    assert _request_user_ids(client, ana_headers, "sent") == []


def test_the_sender_can_cancel_their_own_request(client, db, pair):
    (_, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _reject(client, ana_headers, request_id)

    assert response.status_code == 200
    assert response.get_json()["status"] == "rejected"
    assert _request_user_ids(client, benas_headers, "received") == []
    # A withdrawal is not a rejection, so it leaves no record to
    # hold the pair in the cooldown
    assert _request_rows(db) == []


def test_a_third_party_cannot_reject_someone_elses_request(client, db, pair, make_user, auth_headers):
    (_, ana_headers), (benas, _) = pair
    nosy = make_user(username="smalsute")
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _reject(client, auth_headers(nosy), request_id)

    assert response.status_code == 404
    assert _request_rows(db)[0]["status"] == "pending"


def test_rejecting_an_unknown_request_is_a_404(client, actor):
    _, headers = actor

    response = _reject(client, headers, "no-such-request")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Friend request not found"


def test_rejecting_the_same_request_twice_is_a_404(client, pair):
    (_, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _reject(client, benas_headers, request_id)

    response = _reject(client, benas_headers, request_id)

    assert response.status_code == 404


def test_rejecting_an_accepted_request_is_a_404(client, pair):
    (_, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _accept(client, benas_headers, request_id)

    response = _reject(client, benas_headers, request_id)

    assert response.status_code == 404


def test_rejecting_without_a_token_is_refused(client, pair):
    (_, ana_headers), (benas, _) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]

    response = _reject(client, {}, request_id)

    assert response.status_code == 401


def test_a_declined_pair_can_be_asked_again_from_the_other_side(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _reject(client, benas_headers, request_id)

    response = _send(client, benas_headers, ana["id"])

    assert response.status_code == 201
    assert _request_user_ids(client, ana_headers, "received") == [benas["id"]]




# -----------------------------------------------------------
# The requests list
# -----------------------------------------------------------

@pytest.mark.contract
def test_a_request_row_carries_the_fields_the_request_screen_renders(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _send(client, ana_headers, benas["id"])

    row = client.get(_REQUESTS, headers=benas_headers).get_json()["requests"][0]

    assert set(row) == {"id", "userId", "displayName", "username", "avatarUrl", "role", "createdAt"}
    assert row["userId"] == ana["id"]
    assert row["username"] == ana["username"]
    assert row["displayName"] == "Ana"
    assert row["role"] == "student"
    assert row["avatarUrl"] is None
    assert row["createdAt"]


def test_the_default_direction_is_received(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _send(client, ana_headers, benas["id"])

    body = client.get(_REQUESTS, headers=benas_headers).get_json()

    assert [r["userId"] for r in body["requests"]] == [ana["id"]]
    assert body["total"] == 1
    assert body["hasMore"] is False


def test_an_unknown_direction_is_refused(client, actor):
    _, headers = actor

    response = client.get(_REQUESTS, query_string={"direction": "Sent"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_direction"


def test_an_empty_direction_is_refused(client, actor):
    _, headers = actor

    response = client.get(_REQUESTS, query_string={"direction": ""}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_direction"


def test_requests_from_deactivated_accounts_are_hidden(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _send(client, ana_headers, benas["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (ana["id"],))
    db.commit()

    body = client.get(_REQUESTS, headers=benas_headers).get_json()

    assert body["requests"] == []
    assert body["total"] == 0


def test_a_request_to_a_deactivated_account_leaves_the_sent_list(client, db, pair):
    (_, ana_headers), (benas, _) = pair
    _send(client, ana_headers, benas["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (benas["id"],))
    db.commit()

    body = client.get(_REQUESTS, query_string={"direction": "sent"}, headers=ana_headers).get_json()

    assert body["requests"] == []
    assert body["total"] == 0


def test_the_request_list_pages(client, make_user, auth_headers):
    ana = make_user(username="ana")
    senders = [make_user(username=f"sender{i}") for i in range(3)]
    for sender in senders:
        _send(client, auth_headers(sender), ana["id"])
    ana_headers = auth_headers(ana)

    first = client.get(_REQUESTS, query_string={"per_page": 2}, headers=ana_headers).get_json()
    second = client.get(_REQUESTS, query_string={"per_page": 2, "page": 2}, headers=ana_headers).get_json()

    assert first["total"] == second["total"] == 3
    assert first["hasMore"] is True
    assert second["hasMore"] is False
    assert len(first["requests"]) == 2 and len(second["requests"]) == 1
    seen = {r["userId"] for r in first["requests"]} | {r["userId"] for r in second["requests"]}
    assert seen == {s["id"] for s in senders}


def test_an_over_cap_page_size_on_the_request_list_is_refused(client, actor):
    _, headers = actor

    response = client.get(_REQUESTS, query_string={"per_page": 201}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be at most 200"


def test_a_page_beyond_the_request_list_cap_is_refused(client, actor):
    _, headers = actor

    response = client.get(_REQUESTS, query_string={"page": 201}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 200"


def test_the_request_list_needs_a_token(client):
    assert client.get(_REQUESTS).status_code == 401




# -----------------------------------------------------------
# The friends list
# -----------------------------------------------------------

@pytest.mark.contract
def test_a_friend_row_carries_the_fields_the_friends_screen_renders(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _accept(client, benas_headers, request_id)

    row = client.get(_FRIENDS, headers=ana_headers).get_json()["friends"][0]

    assert set(row) == {"id", "username", "displayName", "avatarUrl", "role", "friendsSince"}
    assert row["id"] == benas["id"]
    assert row["username"] == benas["username"]
    assert row["displayName"] == "Benas"
    assert row["role"] == "student"
    assert row["avatarUrl"] is None
    assert row["friendsSince"]


def test_the_friends_list_is_empty_for_a_new_account(client, actor):
    _, headers = actor

    body = client.get(_FRIENDS, headers=headers).get_json()

    assert body == {"friends": [], "total": 0, "hasMore": False}


def test_the_friends_list_sorts_case_insensitively(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    names = {"Zita": None, "aldona": None, "Barbora": None, "Ąžuolas": None}
    for name in list(names):
        friend = make_user(username=name.lower(), display_name=name)
        names[name] = friend["id"]
        _seed_friendship(db, me["id"], friend["id"])
        _seed_friendship(db, friend["id"], me["id"])

    rows = client.get(_FRIENDS, headers=auth_headers(me)).get_json()["friends"]

    # NOCASE folds ASCII case only — the Lithuanian letter still
    # lands after Z, which is what the client re-sorts with Intl
    assert [r["displayName"] for r in rows] == ["aldona", "Barbora", "Zita", "Ąžuolas"]


def test_deactivated_friends_drop_out_of_the_list(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _seed_friendship(db, ana["id"], benas["id"])
    _seed_friendship(db, benas["id"], ana["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (benas["id"],))
    db.commit()

    body = client.get(_FRIENDS, headers=ana_headers).get_json()

    assert body["friends"] == []
    assert body["total"] == 0


def test_the_friends_list_only_reads_the_callers_own_direction(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    # Only the far half of the friendship exists
    _seed_friendship(db, benas["id"], ana["id"])

    assert _friend_ids(client, ana_headers) == []


def test_the_friends_list_pages(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    friends = []
    for name in ("Aldona", "Barbora", "Cecilija"):
        friend = make_user(username=name.lower(), display_name=name)
        friends.append(friend)
        _seed_friendship(db, me["id"], friend["id"])
    headers = auth_headers(me)

    first = client.get(_FRIENDS, query_string={"per_page": 2}, headers=headers).get_json()
    second = client.get(_FRIENDS, query_string={"per_page": 2, "page": 2}, headers=headers).get_json()

    assert [r["displayName"] for r in first["friends"]] == ["Aldona", "Barbora"]
    assert [r["displayName"] for r in second["friends"]] == ["Cecilija"]
    assert first["total"] == second["total"] == 3
    assert first["hasMore"] is True
    assert second["hasMore"] is False


def test_an_over_cap_page_size_on_the_friends_list_is_refused(client, actor):
    _, headers = actor

    response = client.get(_FRIENDS, query_string={"per_page": 201}, headers=headers)

    assert response.status_code == 400


def test_a_page_beyond_the_friends_list_cap_is_refused(client, actor):
    _, headers = actor

    response = client.get(_FRIENDS, query_string={"page": 201}, headers=headers)

    assert response.status_code == 400


def test_a_junk_page_on_the_friends_list_is_refused(client, actor):
    _, headers = actor

    response = client.get(_FRIENDS, query_string={"page": "abc"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"


def test_the_friends_list_needs_a_token(client):
    assert client.get(_FRIENDS).status_code == 401




# -----------------------------------------------------------
# Unfriending
# -----------------------------------------------------------

def test_unfriending_removes_both_directions(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _accept(client, benas_headers, request_id)

    response = _unfriend(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert response.get_json()["status"] == "unfriended"
    assert _friendship_pairs(db) == set()
    assert _friend_ids(client, benas_headers) == []


def test_unfriending_a_stranger_is_a_404(client, pair):
    (_, ana_headers), (benas, _) = pair

    response = _unfriend(client, ana_headers, benas["id"])

    assert response.status_code == 404
    assert response.get_json()["error"] == "Not friends"


def test_unfriending_an_unknown_user_is_a_404(client, actor):
    _, headers = actor

    assert _unfriend(client, headers, "no-such-user").status_code == 404


def test_unfriending_yourself_is_a_404(client, actor):
    user, headers = actor

    assert _unfriend(client, headers, user["id"]).status_code == 404


def test_a_half_written_friendship_can_be_cleared_from_the_missing_side(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    # Only the OTHER direction exists: the old pre-check read
    # just (me, them) and left this pair stuck forever
    _seed_friendship(db, benas["id"], ana["id"])

    response = _unfriend(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert _friendship_pairs(db) == set()


def test_a_deactivated_friend_can_still_be_unfriended(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _seed_friendship(db, ana["id"], benas["id"])
    _seed_friendship(db, benas["id"], ana["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (benas["id"],))
    db.commit()

    # The row is invisible in every list, but it still counts —
    # the DELETE must not start caring about active
    response = _unfriend(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert _friendship_pairs(db) == set()


def test_unfriending_frees_the_pair_to_start_over(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _accept(client, benas_headers, request_id)
    _unfriend(client, ana_headers, benas["id"])

    response = _send(client, ana_headers, benas["id"])

    assert response.status_code == 201
    assert _request_user_ids(client, benas_headers, "received") == [ana["id"]]


def test_unfriending_without_a_token_is_refused(client, pair):
    (_, _), (benas, _) = pair

    assert _unfriend(client, {}, benas["id"]).status_code == 401




# -----------------------------------------------------------
# The friendship status the profile screen reads
# -----------------------------------------------------------
#
# get_profile's friendshipStatus is the same state machine seen
# from the viewer's side — it decides which button the profile
# screen shows, so every transition above has to land here too.
# -----------------------------------------------------------

def _status(client, headers, user_id):
    return client.get(f"/api/social/profile/{user_id}", headers=headers).get_json()["friendshipStatus"]


def test_a_fresh_pair_sees_no_friendship(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair

    assert _status(client, ana_headers, benas["id"]) == "none"
    assert _status(client, benas_headers, ana["id"]) == "none"


def test_a_pending_request_reads_as_sent_and_received(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _send(client, ana_headers, benas["id"])

    assert _status(client, ana_headers, benas["id"]) == "request_sent"
    assert _status(client, benas_headers, ana["id"]) == "request_received"


def test_an_accepted_request_reads_as_friends_on_both_sides(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _accept(client, benas_headers, request_id)

    assert _status(client, ana_headers, benas["id"]) == "friends"
    assert _status(client, benas_headers, ana["id"]) == "friends"


def test_a_declined_request_reads_as_none_again(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _reject(client, benas_headers, request_id)

    assert _status(client, ana_headers, benas["id"]) == "none"
    assert _status(client, benas_headers, ana["id"]) == "none"


def test_your_own_profile_and_a_guests_view_have_no_friendship(client, pair):
    (ana, ana_headers), (benas, _) = pair
    _send(client, ana_headers, benas["id"])

    assert _status(client, ana_headers, ana["id"]) == "none"
    assert client.get(f"/api/social/profile/{benas['id']}").get_json()["friendshipStatus"] == "none"


def test_the_profile_friend_count_leaves_out_deactivated_friends(client, db, pair, make_user):
    (ana, ana_headers), (benas, _) = pair
    gone = make_user(username="isjunges")
    for other in (benas, gone):
        _seed_friendship(db, ana["id"], other["id"])
        _seed_friendship(db, other["id"], ana["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (gone["id"],))
    db.commit()

    profile = client.get(f"/api/social/profile/{ana['id']}", headers=ana_headers).get_json()

    # The count has to agree with the list, the same rule
    # postCount follows — a profile must not claim friends the
    # friends screen refuses to show
    assert profile["friendCount"] == len(_friend_ids(client, ana_headers)) == 1


def test_the_profile_friend_count_follows_the_friendship(client, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = _send(client, ana_headers, benas["id"]).get_json()["id"]
    _accept(client, benas_headers, request_id)

    assert client.get(f"/api/social/profile/{benas['id']}").get_json()["friendCount"] == 1

    _unfriend(client, ana_headers, benas["id"])

    assert client.get(f"/api/social/profile/{benas['id']}").get_json()["friendCount"] == 0
