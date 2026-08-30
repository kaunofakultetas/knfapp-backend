# -----------------------------------------------------------
#  [*] Tests — social/routes.py list_friends + unfriend, deep
#
#  The exhaustive pass over exactly two routes:
#
#    GET    /api/social/friends          — list_friends
#    DELETE /api/social/friends/<user_id> — unfriend
#
#  test_social_friends.py already proves the happy paths and
#  the obvious guards; this file walks the rest of the grid:
#
#    - every parse_pagination arm as list_friends configures
#      it (default 200, cap 200, max_page 200): 0, 1, cap,
#      cap+1, negatives, junk, "3.0", padded and signed ints,
#      a repeated parameter, a page past the end
#    - hasMore on both sides of its boundary, and a full walk
#      of a paged list proving the u.id tie-break never drops
#      or duplicates a row
#    - the JOIN's own filters: deactivated friends (with no
#      admin override), an orphan friendship row pointing at
#      no user, a self-friendship row, and other people's
#      friendships
#    - the wire shape: exactly six keys per row, every role,
#      an avatar path, an empty display name, and the
#      html-escaping the after_request provider applies on
#      the way out
#    - unfriend as a DELETE-and-count: both rows, near-only,
#      far-only, an orphan row, a second call, the other
#      side's call, an admin with no override, a third pair
#      left untouched, an injection-shaped id, and the
#      friend_requests table left alone
#    - the shared "friendaction" budget (60 per window) that
#      unfriend spends before its body runs, its recovery,
#      and its per-user keying
#    - the auth grid on both routes: no header, a malformed
#      one, a lowercase scheme, an unknown token, an expired
#      session, a deactivated caller
#
#  friendsSince is checked for the house T-form UTC too:
#  both accept paths stamp created_at themselves, so the
#  friendships DEFAULT (space-form, which the app would read
#  as local time) never fires behind them.
# -----------------------------------------------------------

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine


_FRIENDS = "/api/social/friends"
_REQUESTS = "/api/social/friends/requests"

# The route's own numbers, restated so a test names the
# boundary it is standing on
_LIST_PER_PAGE = 200
_LIST_MAX_PAGE = 200
_FRIENDACTION_MAX = 60

# Same trick test_social_friends.py uses: an instant in the
# future, so a session minted inside the traveller is still
# valid after the test moves days forward
_T0 = datetime(2027, 3, 1, 12, 0, 0, tzinfo=timezone.utc)




# -----------------------------------------------------------
# fresh_rate_limits
# -----------------------------------------------------------
#
# The rate-limit windows live in a module-level dict in
# auth/routes.py, so they outlive the per-test database and
# leak between tests — including the global per-IP budget every
# request in the suite spends. Clearing it around each test is
# what makes the 429 assertions below mean the friendaction
# quota and nothing else.
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
# Two signed-in students, ((user, headers), (user, headers)).
# -----------------------------------------------------------

@pytest.fixture
def pair(make_user, auth_headers):
    ana = make_user(username="ana", display_name="Ana")
    benas = make_user(username="benas", display_name="Benas")
    return (ana, auth_headers(ana)), (benas, auth_headers(benas))




# -----------------------------------------------------------
# Wire helpers
# -----------------------------------------------------------

def _list(client, headers, **query):
    return client.get(_FRIENDS, query_string=query, headers=headers)


def _body(client, headers, **query):
    return _list(client, headers, **query).get_json()


def _names(client, headers, **query):
    return [f["displayName"] for f in _body(client, headers, **query)["friends"]]


def _ids(client, headers, **query):
    return [f["id"] for f in _body(client, headers, **query)["friends"]]


def _unfriend(client, headers, user_id):
    return client.delete(f"{_FRIENDS}/{user_id}", headers=headers)




# -----------------------------------------------------------
# _link / _befriend / _pairs
# -----------------------------------------------------------
#
# friendships holds ONE ROW PER DIRECTION and every reader only
# looks up its own direction, so the seeding helpers keep the
# two apart: _link writes a single direction (the half-written
# state unfriend has to survive), _befriend writes both, and
# _pairs asserts on the whole table as a set.
# -----------------------------------------------------------

def _link(db, user_id, friend_id, created_at=None):
    if created_at is None:
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user_id, friend_id))
    else:
        db.execute(
            "INSERT INTO friendships (user_id, friend_id, created_at) VALUES (?, ?, ?)",
            (user_id, friend_id, created_at),
        )
    db.commit()


def _befriend(db, a_id, b_id):
    _link(db, a_id, b_id)
    _link(db, b_id, a_id)


def _pairs(db):
    return {
        (r["user_id"], r["friend_id"])
        for r in db.execute("SELECT user_id, friend_id FROM friendships").fetchall()
    }




# -----------------------------------------------------------
# _bulk_friends
# -----------------------------------------------------------
#
# N friends for one owner, inserted straight through the test
# connection — no bcrypt, no login — because the paging tests
# only ever read these rows through the owner's own token. The
# display names are zero-padded so the route's
# COLLATE NOCASE order and the insertion order agree, which is
# what lets a page walk assert on an exact slice.
#
# The conftest `db` connection leaves PRAGMA foreign_keys off,
# so this is also how an ORPHAN friendship row gets planted.
# -----------------------------------------------------------

def _bulk_friends(db, owner_id, count, prefix="Draugas"):
    ids = []
    for i in range(count):
        user_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO users (id, username, email, display_name, password_hash, role, active, invited)"
            " VALUES (?, ?, ?, ?, 'x', 'student', 1, 1)",
            (user_id, f"bulk{i:04d}", f"bulk{i:04d}@knf.vu.lt", f"{prefix} {i:04d}"),
        )
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (owner_id, user_id))
        ids.append(user_id)
    db.commit()
    return ids




# ===========================================================
# list_friends — the envelope and the row shape
# ===========================================================

@pytest.mark.contract
def test_the_friends_envelope_carries_exactly_three_keys(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    body = _body(client, ana_headers)

    assert set(body) == {"friends", "total", "hasMore"}
    assert body["total"] == 1
    assert body["hasMore"] is False


@pytest.mark.contract
def test_a_friend_row_carries_the_avatar_and_role_the_screen_renders(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    friend = make_user(username="dest", display_name="Destytoja", role="teacher")
    db.execute(
        "UPDATE users SET avatar_url = '/api/uploads/00112233445566778899aabbccddeeff.jpg' WHERE id = ?",
        (friend["id"],),
    )
    db.commit()
    _befriend(db, me["id"], friend["id"])

    row = _body(client, auth_headers(me))["friends"][0]

    assert set(row) == {"id", "username", "displayName", "avatarUrl", "role", "friendsSince"}
    assert row["id"] == friend["id"]
    assert row["username"] == "dest"
    assert row["displayName"] == "Destytoja"
    assert row["role"] == "teacher"
    assert row["avatarUrl"] == "/api/uploads/00112233445566778899aabbccddeeff.jpg"


@pytest.mark.parametrize("role", ["student", "teacher", "admin", "curator"])
def test_every_role_survives_the_friends_list(client, db, make_user, auth_headers, role):
    me = make_user(username="ana")
    friend = make_user(username="kitas", role=role)
    _befriend(db, me["id"], friend["id"])

    assert _body(client, auth_headers(me))["friends"][0]["role"] == role


def test_a_friends_display_name_is_html_escaped_on_the_way_out(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    # Stored RAW through the test connection — a json= body would
    # have been escaped by the app's own provider on the way IN
    # and would prove nothing about the output hook (TESTPLAN 10)
    friend = make_user(username="kenkejas")
    db.execute("UPDATE users SET display_name = ? WHERE id = ?", ('Ona <b>"&', friend["id"]))
    db.commit()
    _befriend(db, me["id"], friend["id"])

    response = _list(client, auth_headers(me))

    assert b"Ona &lt;b&gt;&quot;&amp;" in response.data
    assert b"<b>" not in response.data


def test_an_empty_display_name_comes_back_empty_and_sorts_first(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    blank = make_user(username="tuscias")
    named = make_user(username="zita", display_name="Zita")
    db.execute("UPDATE users SET display_name = '' WHERE id = ?", (blank["id"],))
    db.commit()
    _befriend(db, me["id"], blank["id"])
    _befriend(db, me["id"], named["id"])

    assert _names(client, auth_headers(me)) == ["", "Zita"]


def test_friends_since_echoes_the_friendship_rows_created_at(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _link(db, ana["id"], benas["id"], created_at="2026-01-02T03:04:05+00:00")

    assert _body(client, ana_headers)["friends"][0]["friendsSince"] == "2026-01-02T03:04:05+00:00"


def test_friends_since_is_stamped_in_the_house_T_form(client, pair):
    # accept stamps created_at with utc_now_iso() instead of
    # letting the column's space-form DEFAULT fire — a naive
    # space-form string is read as LOCAL time by the app
    (ana, ana_headers), (benas, benas_headers) = pair
    request_id = client.post(
        "/api/social/friends/request", json={"user_id": benas["id"]}, headers=ana_headers
    ).get_json()["id"]
    client.post(f"{_REQUESTS}/{request_id}/accept", headers=benas_headers)

    since = _body(client, ana_headers)["friends"][0]["friendsSince"]

    assert "T" in since, f"space-form friendsSince: {since!r}"




# ===========================================================
# list_friends — which rows the JOIN keeps
# ===========================================================

def test_a_new_account_gets_an_empty_page_not_a_404(client, actor):
    _, headers = actor

    response = _list(client, headers)

    assert response.status_code == 200
    assert response.get_json() == {"friends": [], "total": 0, "hasMore": False}


def test_two_other_peoples_friendship_is_invisible(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    one = make_user(username="benas")
    two = make_user(username="carlas")
    _befriend(db, one["id"], two["id"])

    assert _body(client, auth_headers(me)) == {"friends": [], "total": 0, "hasMore": False}


def test_only_the_callers_own_direction_is_listed(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    # The far half only — the state a crash between the accept
    # path's two inserts leaves behind
    _link(db, benas["id"], ana["id"])

    body = _body(client, ana_headers)

    assert body["friends"] == []
    assert body["total"] == 0


def test_reactivating_a_friend_brings_them_back_into_the_list(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (benas["id"],))
    db.commit()
    assert _body(client, ana_headers)["total"] == 0

    db.execute("UPDATE users SET active = 1 WHERE id = ?", (benas["id"],))
    db.commit()

    assert _ids(client, ana_headers) == [benas["id"]]


def test_an_admin_gets_no_override_on_deactivated_friends(client, db, admin, make_user):
    admin_user, admin_headers = admin
    gone = make_user(username="isjunges", active=0)
    _befriend(db, admin_user["id"], gone["id"])

    body = _body(client, admin_headers)

    # get_profile lets an admin see a deactivated account; this
    # list has no such branch — active = 1 is unconditional
    assert body["friends"] == []
    assert body["total"] == 0


def test_an_orphan_friendship_row_is_dropped_by_the_join(client, db, actor):
    user, headers = actor
    # No users row for this id — the conftest connection leaves
    # foreign_keys off, so the dangling row can be planted
    _link(db, user["id"], "vaiduoklis")

    body = _body(client, headers)

    assert body["friends"] == []
    assert body["total"] == 0


def test_a_self_friendship_row_lists_the_caller_as_their_own_friend(client, db, actor):
    user, headers = actor
    # Not reachable through any route — the friendships primary
    # key has no user_id <> friend_id check, so this pins what a
    # hand-edited row does rather than a route's behaviour
    _link(db, user["id"], user["id"])

    assert _ids(client, headers) == [user["id"]]


def test_the_total_counts_the_same_set_the_page_lists(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    live = make_user(username="gyvas")
    gone = make_user(username="isjunges")
    _befriend(db, me["id"], live["id"])
    _befriend(db, me["id"], gone["id"])
    _link(db, me["id"], "vaiduoklis")
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (gone["id"],))
    db.commit()

    body = _body(client, auth_headers(me))

    assert len(body["friends"]) == body["total"] == 1




# ===========================================================
# list_friends — ordering
# ===========================================================

def test_names_differing_only_in_case_are_ordered_by_id(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    lower = make_user(username="maza", display_name="rita")
    upper = make_user(username="didele", display_name="RITA")
    _befriend(db, me["id"], lower["id"])
    _befriend(db, me["id"], upper["id"])

    ids = _ids(client, auth_headers(me))

    # NOCASE makes the two names equal, so u.id is the whole
    # order — deterministic is the only promise
    assert ids == sorted([lower["id"], upper["id"]])


def test_a_paged_walk_of_a_tied_list_loses_and_repeats_nothing(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    twins = [make_user(username=f"dvynys{i}", display_name="Dvynys") for i in range(5)]
    for twin in twins:
        _befriend(db, me["id"], twin["id"])
    headers = auth_headers(me)

    walked = []
    for page in range(1, 6):
        walked.extend(_ids(client, headers, per_page=1, page=page))

    assert walked == sorted(t["id"] for t in twins)


def test_the_lithuanian_letter_still_lands_after_z(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    for name in ("Zita", "aldona", "Ąžuolas"):
        friend = make_user(username=name.lower(), display_name=name)
        _befriend(db, me["id"], friend["id"])

    # NOCASE folds ASCII case only; the client re-sorts with
    # Intl.Collator, so this is the documented server floor
    assert _names(client, auth_headers(me)) == ["aldona", "Zita", "Ąžuolas"]




# ===========================================================
# list_friends — paging boundaries
# ===========================================================

def test_the_first_page_of_one_reports_more_to_come(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    _bulk_friends(db, me["id"], 3)

    body = _body(client, auth_headers(me), per_page=1)

    assert len(body["friends"]) == 1
    assert body["total"] == 3
    assert body["hasMore"] is True


def test_the_last_page_reports_no_more(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    _bulk_friends(db, me["id"], 3)

    body = _body(client, auth_headers(me), per_page=2, page=2)

    assert [f["displayName"] for f in body["friends"]] == ["Draugas 0002"]
    assert body["hasMore"] is False


def test_a_full_page_with_nothing_after_it_reports_no_more(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    _bulk_friends(db, me["id"], 2)

    body = _body(client, auth_headers(me), per_page=2)

    # offset + per_page < total is 0 + 2 < 2 — the exact boundary
    assert len(body["friends"]) == 2
    assert body["hasMore"] is False


def test_one_more_row_than_the_page_flips_hasmore(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    _bulk_friends(db, me["id"], 3)

    assert _body(client, auth_headers(me), per_page=2)["hasMore"] is True


def test_a_page_past_the_end_is_empty_but_keeps_the_total(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    _bulk_friends(db, me["id"], 2)

    body = _body(client, auth_headers(me), per_page=2, page=50)

    assert body["friends"] == []
    assert body["total"] == 2
    assert body["hasMore"] is False


@pytest.mark.slow
def test_the_default_page_holds_two_hundred_friends_and_flags_the_rest(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    _bulk_friends(db, me["id"], _LIST_PER_PAGE + 1)

    # The app sends no pagination at all, so the default per_page
    # IS the cap — friend 201 is only reachable through page 2
    body = _body(client, auth_headers(me))

    assert len(body["friends"]) == _LIST_PER_PAGE
    assert body["total"] == _LIST_PER_PAGE + 1
    assert body["hasMore"] is True
    assert len(_body(client, auth_headers(me), page=2)["friends"]) == 1


def test_a_page_size_at_the_cap_is_accepted(client, actor):
    _, headers = actor

    assert _list(client, headers, per_page=_LIST_PER_PAGE).status_code == 200


def test_a_page_size_one_over_the_cap_is_refused(client, actor):
    _, headers = actor

    response = _list(client, headers, per_page=_LIST_PER_PAGE + 1)

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be at most 200"


@pytest.mark.parametrize("per_page", ["0", "-1", "abc", "2.0", "", " ", "1e2", "null"])
def test_a_bad_page_size_is_refused(client, actor, per_page):
    _, headers = actor

    response = _list(client, headers, per_page=per_page)

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be a positive integer"


@pytest.mark.parametrize("page", ["0", "-1", "abc", "1.0", "", "+", "０"])
def test_a_bad_page_number_is_refused(client, actor, page):
    _, headers = actor

    response = _list(client, headers, page=page)

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"


def test_the_last_allowed_page_is_accepted(client, actor):
    _, headers = actor

    response = _list(client, headers, page=_LIST_MAX_PAGE)

    assert response.status_code == 200
    assert response.get_json()["friends"] == []


def test_one_page_beyond_the_cap_is_refused(client, actor):
    _, headers = actor

    response = _list(client, headers, page=_LIST_MAX_PAGE + 1)

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 200"


def test_an_enormous_page_number_is_refused_rather_than_scanned(client, actor):
    _, headers = actor

    response = _list(client, headers, page=10 ** 9)

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 200"


def test_a_padded_or_signed_page_is_accepted_the_way_int_accepts_it(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    _bulk_friends(db, me["id"], 2)
    headers = auth_headers(me)

    # int(" 2 ") and int("+1") both parse; int("2.0") does not —
    # the quirk parse_pagination's banner names
    assert len(_body(client, headers, page=" 2 ", per_page="+1")["friends"]) == 1


def test_the_first_of_a_repeated_query_parameter_wins(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    _bulk_friends(db, me["id"], 3)

    body = client.get(
        f"{_FRIENDS}?per_page=1&per_page=200", headers=auth_headers(me)
    ).get_json()

    assert len(body["friends"]) == 1
    assert body["total"] == 3




# ===========================================================
# list_friends — the auth grid
# ===========================================================

def test_the_friends_list_without_any_header_is_refused(client):
    response = client.get(_FRIENDS)

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Token abcdef", "abcdef", "Basic YWRtaW46eA=="])
def test_a_malformed_authorization_header_is_refused(client, header):
    response = client.get(_FRIENDS, headers={"Authorization": header})

    assert response.status_code == 401


def test_a_lowercase_bearer_scheme_is_accepted(client, actor):
    _, headers = actor
    token = headers["Authorization"].split(" ", 1)[1]

    response = client.get(_FRIENDS, headers={"Authorization": f"bearer {token}"})

    assert response.status_code == 200


def test_an_unknown_token_is_refused(client, actor):
    response = client.get(_FRIENDS, headers={"Authorization": f"Bearer {uuid.uuid4()}"})

    assert response.status_code == 401


def test_an_expired_session_loses_the_friends_list(client, make_user, auth_headers):
    me = make_user(username="ana")

    with time_machine.travel(_T0, tick=False) as traveller:
        headers = auth_headers(me)
        assert client.get(_FRIENDS, headers=headers).status_code == 200

        # Sessions live 30 days from the login instant
        traveller.move_to(_T0 + timedelta(days=31))
        response = client.get(_FRIENDS, headers=headers)

    assert response.status_code == 401


def test_a_deactivated_caller_loses_the_friends_list(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (ana["id"],))
    db.commit()

    assert client.get(_FRIENDS, headers=ana_headers).status_code == 401


def test_a_write_method_on_the_friends_collection_is_refused(client, actor):
    _, headers = actor

    assert client.post(_FRIENDS, headers=headers).status_code == 405
    assert client.put(_FRIENDS, headers=headers).status_code == 405
    assert client.delete(_FRIENDS, headers=headers).status_code == 405




# ===========================================================
# unfriend — the delete-and-count
# ===========================================================

@pytest.mark.contract
def test_unfriending_answers_exactly_the_status_the_app_reads(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    response = _unfriend(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert response.get_json() == {"status": "unfriended"}


def test_unfriending_clears_both_rows_and_is_committed(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _befriend(db, ana["id"], benas["id"])

    _unfriend(client, ana_headers, benas["id"])

    # Read back on a connection the route never touched
    assert _pairs(db) == set()
    assert _body(client, benas_headers)["total"] == 0


def test_the_near_direction_alone_is_enough_to_unfriend(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _link(db, ana["id"], benas["id"])

    response = _unfriend(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert _pairs(db) == set()


def test_the_far_direction_alone_is_enough_to_unfriend(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _link(db, benas["id"], ana["id"])

    response = _unfriend(client, ana_headers, benas["id"])

    assert response.status_code == 200
    assert _pairs(db) == set()


def test_a_second_unfriend_is_a_404(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])
    assert _unfriend(client, ana_headers, benas["id"]).status_code == 200

    response = _unfriend(client, ana_headers, benas["id"])

    assert response.status_code == 404
    assert response.get_json()["error"] == "Not friends"


def test_the_other_side_gets_a_404_after_the_first_unfriend(client, db, pair):
    (ana, ana_headers), (benas, benas_headers) = pair
    _befriend(db, ana["id"], benas["id"])
    _unfriend(client, ana_headers, benas["id"])

    # Both rows went in one DELETE, so the other side has nothing
    # left to remove
    assert _unfriend(client, benas_headers, ana["id"]).status_code == 404


def test_an_orphan_friendship_row_can_still_be_unfriended(client, db, actor):
    user, headers = actor
    _link(db, user["id"], "vaiduoklis")

    response = _unfriend(client, headers, "vaiduoklis")

    # The DELETE never joins users, so a row the LIST hides is
    # still removable — otherwise it would be stuck forever
    assert response.status_code == 200
    assert _pairs(db) == set()


def test_a_self_friendship_row_can_be_unfriended(client, db, actor):
    user, headers = actor
    _link(db, user["id"], user["id"])

    response = _unfriend(client, headers, user["id"])

    assert response.status_code == 200
    assert _pairs(db) == set()


def test_an_admin_cannot_unfriend_two_other_people(client, db, admin, pair):
    _, admin_headers = admin
    (ana, _), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    response = _unfriend(client, admin_headers, benas["id"])

    # No admin override on this route — the DELETE is anchored on
    # the caller's own id in both halves of the OR
    assert response.status_code == 404
    assert _pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}


def test_unfriending_leaves_every_other_pair_alone(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    friend = make_user(username="benas")
    others = (make_user(username="carlas"), make_user(username="dana"))
    _befriend(db, me["id"], friend["id"])
    _befriend(db, others[0]["id"], others[1]["id"])
    # A friendship of the caller with a third party must survive
    third = make_user(username="egle")
    _befriend(db, me["id"], third["id"])

    _unfriend(client, auth_headers(me), friend["id"])

    assert _pairs(db) == {
        (others[0]["id"], others[1]["id"]), (others[1]["id"], others[0]["id"]),
        (me["id"], third["id"]), (third["id"], me["id"]),
    }


def test_a_failed_unfriend_deletes_nothing(client, db, pair, make_user, auth_headers):
    (ana, ana_headers), (benas, _) = pair
    stranger = make_user(username="svetimas")
    _befriend(db, ana["id"], benas["id"])

    assert _unfriend(client, ana_headers, stranger["id"]).status_code == 404

    assert _pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}


def test_an_injection_shaped_user_id_deletes_nothing(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    response = _unfriend(client, ana_headers, "x' OR '1'='1")

    assert response.status_code == 404
    assert _pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}


def test_a_unicode_user_id_is_a_404(client, actor):
    _, headers = actor

    assert _unfriend(client, headers, "Ąžuolas-💥").status_code == 404


def test_a_very_long_user_id_is_a_404(client, actor):
    _, headers = actor

    assert _unfriend(client, headers, "a" * 4000).status_code == 404


def test_the_static_friend_routes_are_matched_before_the_wildcard(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    # /friends/request and /friends/requests are POST/GET rules,
    # so a DELETE on them falls through to this route with the
    # segment as a user id — and must find no friendship
    assert _unfriend(client, ana_headers, "request").status_code == 404
    assert _unfriend(client, ana_headers, "requests").status_code == 404
    assert _pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}


def test_an_encoded_slash_in_the_user_id_never_reaches_the_route(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    # Werkzeug decodes the path before matching, so this becomes a
    # two-segment path no rule owns — the app's own 404 handler,
    # never a 500
    response = client.delete(f"{_FRIENDS}/..%2F..%2Fetc", headers=ana_headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Not found"
    assert len(_pairs(db)) == 2


def test_the_bare_friend_path_with_a_trailing_slash_is_a_404(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    # <user_id> needs at least one character, so an empty segment
    # matches no rule at all
    response = client.delete(f"{_FRIENDS}/", headers=ana_headers)

    assert response.status_code == 404
    assert len(_pairs(db)) == 2


def test_unfriending_does_not_disturb_the_friend_requests_table(client, db, pair, make_user):
    (ana, ana_headers), (benas, _) = pair
    carlas = make_user(username="carlas")
    _befriend(db, ana["id"], benas["id"])
    db.execute(
        "INSERT INTO friend_requests (id, from_user_id, to_user_id, status) VALUES ('r1', ?, ?, 'rejected')",
        (ana["id"], carlas["id"]),
    )
    db.commit()

    _unfriend(client, ana_headers, benas["id"])

    rows = db.execute("SELECT id, status FROM friend_requests").fetchall()
    assert [(r["id"], r["status"]) for r in rows] == [("r1", "rejected")]


def test_a_json_body_on_the_delete_is_ignored(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    response = client.delete(f"{_FRIENDS}/{benas['id']}", json={"user_id": "kitas"}, headers=ana_headers)

    assert response.status_code == 200
    assert _pairs(db) == set()


def test_a_non_object_json_body_never_reaches_the_route(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    response = client.delete(f"{_FRIENDS}/{benas['id']}", json=[1, 2], headers=ana_headers)

    # The before_request body guard answers first, so the
    # friendship is untouched
    assert response.status_code == 400
    assert _pairs(db) == {(ana["id"], benas["id"]), (benas["id"], ana["id"])}


def test_a_read_method_on_one_friend_is_refused(client, actor):
    user, headers = actor

    assert client.get(f"{_FRIENDS}/{user['id']}", headers=headers).status_code == 405
    assert client.put(f"{_FRIENDS}/{user['id']}", headers=headers).status_code == 405




# ===========================================================
# unfriend — the auth grid
# ===========================================================

def test_unfriending_without_a_token_changes_nothing(client, db, pair):
    (ana, _), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    response = _unfriend(client, {}, benas["id"])

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"
    assert len(_pairs(db)) == 2


def test_an_unknown_token_cannot_unfriend(client, db, pair):
    (ana, _), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])

    response = _unfriend(client, {"Authorization": f"Bearer {uuid.uuid4()}"}, benas["id"])

    assert response.status_code == 401
    assert len(_pairs(db)) == 2


def test_a_deactivated_caller_cannot_unfriend(client, db, pair):
    (ana, ana_headers), (benas, _) = pair
    _befriend(db, ana["id"], benas["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (ana["id"],))
    db.commit()

    assert _unfriend(client, ana_headers, benas["id"]).status_code == 401
    assert len(_pairs(db)) == 2


def test_an_expired_session_cannot_unfriend(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    friend = make_user(username="benas")
    _befriend(db, me["id"], friend["id"])

    with time_machine.travel(_T0, tick=False) as traveller:
        headers = auth_headers(me)
        traveller.move_to(_T0 + timedelta(days=31))
        response = _unfriend(client, headers, friend["id"])

    assert response.status_code == 401
    assert len(_pairs(db)) == 2




# ===========================================================
# unfriend — the shared friendaction budget
# ===========================================================
#
# rate_limit("friendaction", max_attempts=60) sits UNDER
# require_auth, so the key is the caller's user id and the
# budget is spent before the route body runs — a 404 costs as
# much as a real unfriend.
# ===========================================================

def test_the_sixty_first_friend_action_in_a_window_is_rate_limited(client, make_user, auth_headers):
    me = make_user(username="ana")

    with time_machine.travel(_T0, tick=False):
        headers = auth_headers(me)
        for _ in range(_FRIENDACTION_MAX):
            assert _unfriend(client, headers, "no-such-user").status_code == 404

        response = _unfriend(client, headers, "no-such-user")

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


def test_a_rate_limited_unfriend_never_reaches_the_delete(client, db, make_user, auth_headers):
    me = make_user(username="ana")
    friend = make_user(username="benas")
    _befriend(db, me["id"], friend["id"])

    with time_machine.travel(_T0, tick=False):
        headers = auth_headers(me)
        for _ in range(_FRIENDACTION_MAX):
            _unfriend(client, headers, "no-such-user")

        response = _unfriend(client, headers, friend["id"])

    assert response.status_code == 429
    assert len(_pairs(db)) == 2


def test_the_friend_action_budget_comes_back_when_the_window_moves_on(client, make_user, auth_headers):
    me = make_user(username="ana")

    with time_machine.travel(_T0, tick=False) as traveller:
        headers = auth_headers(me)
        for _ in range(_FRIENDACTION_MAX):
            _unfriend(client, headers, "no-such-user")
        assert _unfriend(client, headers, "no-such-user").status_code == 429

        traveller.move_to(_T0 + timedelta(minutes=6))
        response = _unfriend(client, headers, "no-such-user")

    assert response.status_code == 404


def test_the_friend_action_budget_is_keyed_on_the_caller(client, make_user, auth_headers):
    hungry = make_user(username="ana")
    other = make_user(username="benas")

    with time_machine.travel(_T0, tick=False):
        hungry_headers, other_headers = auth_headers(hungry), auth_headers(other)
        for _ in range(_FRIENDACTION_MAX):
            _unfriend(client, hungry_headers, "no-such-user")
        assert _unfriend(client, hungry_headers, "no-such-user").status_code == 429

        response = _unfriend(client, other_headers, "no-such-user")

    assert response.status_code == 404


def test_unfriend_shares_its_budget_with_the_other_friend_actions(client, make_user, auth_headers):
    me = make_user(username="ana")

    with time_machine.travel(_T0, tick=False):
        headers = auth_headers(me)
        # accept / reject / unfriend all key on "friendaction"
        for _ in range(_FRIENDACTION_MAX):
            client.post(f"{_REQUESTS}/no-such-request/reject", headers=headers)

        response = _unfriend(client, headers, "no-such-user")

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_the_friends_list_costs_no_friend_action_budget(client, make_user, auth_headers):
    me = make_user(username="ana")

    with time_machine.travel(_T0, tick=False):
        headers = auth_headers(me)
        # A read route carries no rate_limit decorator at all
        for _ in range(_FRIENDACTION_MAX + 5):
            assert client.get(_FRIENDS, headers=headers).status_code == 200

        assert _unfriend(client, headers, "no-such-user").status_code == 404
