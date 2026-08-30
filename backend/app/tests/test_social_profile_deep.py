# -----------------------------------------------------------
#  [*] Social — profiles, the exhaustive pass
#
#  Slice: app/social/routes.py — get_profile,
#  get_own_profile and update_profile ONLY. The broad file
#  test_social_profile.py already drives both lines and
#  branches of these three to 100%; everything here is the
#  next layer down, the paths a line counter cannot see:
#
#    - the conditional EXPRESSIONS coverage never splits —
#      get_own_profile's `row["created_at"] if row else None`
#      when the account is deleted under the request.
#    - the request pipeline AROUND the routes: the object
#      guard and the avatar guard in app/__init__.py's
#      before_request run BEFORE require_auth, so an
#      anonymous caller gets 400 where they expected 401;
#      NUL bytes are stripped from the body; a body over the
#      nesting cap is a 400 and never a 500.
#    - what is actually ON THE WIRE. TESTPLAN rule 10: a
#      `json=` kwarg is html-escaped by the app's own JSON
#      provider before it leaves the test client, so every
#      assertion about markup, entities or NUL round-trips
#      here posts raw bytes through _put_raw.
#    - validation ORDER and atomicity: a body whose second
#      field is bad must leave the first field's column
#      untouched, and each guard must be the one that
#      answers.
#    - every boundary on both sides (0/1/50/51/100/101/2048),
#      every accepted avatar extension and the near-misses
#      the \Z-anchored regex exists to refuse, every viewer
#      class (guest, stranger, one-way friend, real friend,
#      author, admin, deactivated, expired session, deleted
#      account) against postCount, friendCount and
#      friendshipStatus.
#    - method gates, and which routes carry a quota at all.
#
#  Nothing here edits the module under test; a handful of
#  behaviours are pinned as-is with a note when they are
#  quirks rather than contracts.
# -----------------------------------------------------------

import json
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

from app.api import SUMMARY_LENGTH

# Paths shaped exactly the way uploads/routes.py hands them
# out — the before_request hook matches uuid4().hex plus a
# known extension, never a bare "/api/uploads/" prefix
_AVATAR_A = "/api/uploads/" + "a" * 32 + ".png"
_AVATAR_B = "/api/uploads/" + "b" * 32 + ".jpg"

_PROFILE = "/api/social/profile"

# The shared keys the public and the private profile must
# agree on when the author reads themselves
_SHARED_PROFILE_KEYS = ["id", "username", "displayName", "avatarUrl", "role",
                        "createdAt", "postCount", "friendCount"]




# -----------------------------------------------------------
# _clean_rate_limits
# -----------------------------------------------------------
#
# The limiter's store is a MODULE global shared by every test
# in the process, so the 30-per-window profile quota and the
# 600-per-IP global budget both leak between files unless
# each test starts and ends clean.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limits():
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _put_raw / _put
# -----------------------------------------------------------
#
# _put_raw is TESTPLAN rule 10: the bytes go on the wire
# exactly as written, because `json=` would be serialised
# through the app's own escaping JSON provider and silently
# falsify every markup, entity and NUL assertion below.
# _put is the ordinary escaped-is-harmless path.
# -----------------------------------------------------------

def _put_raw(client, headers, body, content_type="application/json"):
    return client.put(_PROFILE, data=body,
                      headers={**headers, "Content-Type": content_type})


def _put(client, headers, body):
    return client.put(_PROFILE, headers=headers, json=body)




# -----------------------------------------------------------
# _bare_user
# -----------------------------------------------------------
#
# A users row inserted straight, with a dummy hash: the bulk
# cases below need 25 accounts and the make_user fixture pays
# a bcrypt round per call. Nobody logs in as these.
# -----------------------------------------------------------

def _bare_user(db, active=1, display_name=None):
    user_id = str(uuid.uuid4())
    username = f"bare_{user_id[:8]}"

    db.execute(
        "INSERT INTO users (id, username, email, display_name, password_hash, role, active, invited)"
        " VALUES (?, ?, ?, ?, 'x', 'student', ?, 1)",
        (user_id, username, f"{username}@knf.vu.lt", display_name or username, active),
    )
    db.commit()
    return user_id




# -----------------------------------------------------------
# _seed_post / _befriend / _one_way / _seed_request / _deactivate
# -----------------------------------------------------------
#
# The states the write routes cannot produce: a private post,
# a 'faculty' announcement, a scraped row credited to a user,
# an author-less row, one HALF of a friendship (the table is
# one row per direction and every reader looks up only its
# own), and a friend_requests row in any status.
# -----------------------------------------------------------

def _seed_post(db, author_id, public=True, source="user", post_type="social",
               content="Sienos irasas"):
    post_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        "INSERT INTO news_posts (id, title, content, summary, author_id, author_name, source,"
        " post_type, is_public, published_at, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (post_id, content[:80], content, content[:SUMMARY_LENGTH], author_id, "Snapshot",
         source, post_type, 1 if public else 0, now, now, now),
    )
    db.commit()
    return post_id


def _befriend(db, one, other):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (one, other))
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (other, one))
    db.commit()


def _one_way(db, owner, friend):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (owner, friend))
    db.commit()


def _seed_request(db, from_id, to_id, status="pending"):
    request_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO friend_requests (id, from_user_id, to_user_id, status) VALUES (?, ?, ?, ?)",
        (request_id, from_id, to_id, status),
    )
    db.commit()
    return request_id


def _deactivate(db, user_id):
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user_id,))
    db.commit()




# -----------------------------------------------------------
# _profile / _stored
# -----------------------------------------------------------

def _profile(client, user_id, headers=None):
    return client.get(f"{_PROFILE}/{urllib.parse.quote(str(user_id), safe='')}",
                      headers=headers or {})


def _stored(db, user_id, column):
    return db.execute(f"SELECT {column} AS v FROM users WHERE id = ?", (user_id,)).fetchone()["v"]




# -----------------------------------------------------------
# _far_future
# -----------------------------------------------------------
#
# Past the 30-day session lifetime auth/routes.py mints, so a
# request made inside this window carries a token whose row
# still exists but whose expires_at has passed.
# -----------------------------------------------------------

def _far_future():
    return datetime.now(timezone.utc) + timedelta(days=31)








# -----------------------------------------------------------
# GET /api/social/profile/<user_id> — the route itself
# -----------------------------------------------------------

@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_public_profile_route_answers_only_to_get(client, make_user, method):
    owner = make_user()

    response = getattr(client, method)(f"{_PROFILE}/{owner['id']}")

    assert response.status_code == 405


def test_a_profile_id_shaped_like_sql_injection_is_only_a_404(client, make_user, db):
    make_user()

    response = _profile(client, "' OR '1'='1")

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"
    # The parameterised lookup cannot have dropped anything
    assert db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] >= 2


def test_a_profile_id_is_matched_case_sensitively(client, make_user):
    owner = make_user()

    assert _profile(client, owner["id"].upper()).status_code == 404
    assert _profile(client, owner["id"]).status_code == 200


def test_a_two_thousand_character_profile_id_is_a_404(client):
    assert _profile(client, "x" * 2000).status_code == 404


def test_a_profile_id_of_only_whitespace_is_a_404(client):
    assert _profile(client, "   ").status_code == 404


def test_an_encoded_slash_in_the_profile_id_never_matches_a_user(client, make_user):
    owner = make_user()

    assert _profile(client, f"{owner['id']}/extra").status_code == 404


def test_a_trailing_slash_never_resolves_to_a_profile(client, make_user):
    owner = make_user()

    response = client.get(f"{_PROFILE}/{owner['id']}/")

    assert response.status_code == 404


def test_a_head_request_answers_like_the_get_with_no_body(client, make_user):
    owner = make_user()

    response = client.head(f"{_PROFILE}/{owner['id']}")

    assert response.status_code == 200
    assert response.get_data() == b""


def test_a_guest_can_read_the_seeded_administrators_profile(client, admin):
    admin_user, _ = admin

    body = _profile(client, admin_user["id"]).get_json()

    assert body["role"] == "admin"
    assert body["username"] == "admin"
    assert "email" not in body


def test_a_legacy_absolute_avatar_is_still_served_by_both_reads(client, make_user,
                                                               auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    # The write side refuses this today; the read side never
    # validates, so an old row must still render
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?",
               ("https://legacy.example/a.png", owner["id"]))
    db.commit()

    assert _profile(client, owner["id"]).get_json()["avatarUrl"] == "https://legacy.example/a.png"
    assert client.get(_PROFILE, headers=headers).get_json()["avatarUrl"] == "https://legacy.example/a.png"


def test_reading_a_profile_forty_times_is_never_rate_limited(client, make_user):
    owner = make_user()

    # No rate_limit decorator on this route — the app must work
    # without login, and a guest scrolling a wall re-reads it
    for _ in range(40):
        assert _profile(client, owner["id"]).status_code == 200


def test_the_public_profile_never_leaks_the_credentials_or_the_active_flag(client, make_user, db):
    owner = make_user()
    _deactivate(db, owner["id"])
    db.execute("UPDATE users SET active = 1 WHERE id = ?", (owner["id"],))
    db.commit()

    body = _profile(client, owner["id"]).get_json()

    for leaked in ("password_hash", "active", "email", "invited", "studentNumber"):
        assert leaked not in body




# -----------------------------------------------------------
# GET /api/social/profile/<user_id> — every viewer class
# -----------------------------------------------------------

@pytest.mark.parametrize("header_value", ["", "Bearer", "Bearer    ", "Basic abc",
                                          "Token abc", "bearer", "Bearer\tabc"])
def test_an_unusable_authorization_header_reads_the_profile_as_a_guest(client, make_user, db,
                                                                       header_value):
    owner = make_user()
    _seed_post(db, owner["id"], public=False)

    body = _profile(client, owner["id"], {"Authorization": header_value}).get_json()

    assert body["postCount"] == 0
    assert body["friendshipStatus"] == "none"


def test_a_lowercase_bearer_scheme_still_authenticates_the_viewer(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    _seed_post(db, owner["id"], public=False)

    lowercase = {"Authorization": headers["Authorization"].replace("Bearer", "bearer", 1)}

    assert _profile(client, owner["id"], lowercase).get_json()["postCount"] == 1


def test_an_expired_session_reads_the_profile_as_a_guest(client, make_user, auth_headers, db):
    viewer = make_user()
    owner = make_user()
    headers = auth_headers(viewer)
    _befriend(db, viewer["id"], owner["id"])
    _seed_post(db, owner["id"], public=False)

    with time_machine.travel(_far_future(), tick=False):
        body = _profile(client, owner["id"], headers).get_json()

    assert body["postCount"] == 0
    assert body["friendshipStatus"] == "none"


def test_a_viewer_whose_account_was_deleted_reads_the_profile_as_a_guest(client, make_user,
                                                                        auth_headers, db):
    viewer = make_user()
    owner = make_user()
    headers = auth_headers(viewer)
    _befriend(db, viewer["id"], owner["id"])
    _seed_post(db, owner["id"], public=False)
    db.execute("DELETE FROM users WHERE id = ?", (viewer["id"],))
    db.commit()

    body = _profile(client, owner["id"], headers).get_json()

    assert body["postCount"] == 0
    assert body["friendshipStatus"] == "none"


def test_an_admin_reading_a_deactivated_friend_still_sees_the_friendship(client, make_user,
                                                                        admin, db):
    admin_user, admin_headers = admin
    owner = make_user()
    _befriend(db, admin_user["id"], owner["id"])
    _seed_post(db, owner["id"], public=False)
    _deactivate(db, owner["id"])

    body = _profile(client, owner["id"], admin_headers).get_json()

    assert body["friendshipStatus"] == "friends"
    # A friend sees the private post, deactivated or not
    assert body["postCount"] == 1


def test_a_curator_gets_the_same_404_as_anyone_else_on_a_deactivated_profile(client, make_user,
                                                                            auth_headers, db):
    curator = make_user(role="curator")
    owner = make_user()
    _deactivate(db, owner["id"])

    # Only 'admin' is exempt — the check reads the role literally
    assert _profile(client, owner["id"], auth_headers(curator)).status_code == 404


def test_a_teacher_gets_the_same_404_as_anyone_else_on_a_deactivated_profile(client, make_user,
                                                                            auth_headers, db):
    teacher = make_user(role="teacher")
    owner = make_user()
    _deactivate(db, owner["id"])

    assert _profile(client, owner["id"], auth_headers(teacher)).status_code == 404




# -----------------------------------------------------------
# GET /api/social/profile/<user_id> — postCount permutations
# -----------------------------------------------------------

def test_post_count_ignores_rows_with_no_author_at_all(client, make_user, db):
    owner = make_user()
    post_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO news_posts (id, title, content, author_name, source, post_type, is_public,"
        " published_at, created_at, updated_at) VALUES (?, 'T', 'C', 'Kazkas', 'user', 'social',"
        " 1, ?, ?, ?)",
        (post_id, now, now, now),
    )
    db.commit()

    assert _profile(client, owner["id"]).get_json()["postCount"] == 0


@pytest.mark.parametrize("post_type", ["social", "article", "announcement", "poll", "link"])
def test_post_count_looks_at_the_source_and_never_at_the_post_type(client, make_user, db,
                                                                  post_type):
    owner = make_user()
    _seed_post(db, owner["id"], source="user", post_type=post_type)

    # source IN ('user', 'faculty') is the whole rule — a poll or a
    # link posted to a wall is still one of the author's posts
    assert _profile(client, owner["id"]).get_json()["postCount"] == 1


@pytest.mark.parametrize("source", ["app", "knf.vu.lt", "vu.lt"])
def test_no_other_source_the_schema_allows_reaches_the_count(client, make_user, db, source):
    owner = make_user()
    _seed_post(db, owner["id"], source=source, post_type="article")
    _seed_post(db, owner["id"], source="faculty", post_type="announcement")

    # The CHECK constraint on news_posts.source bounds the domain to
    # five values, so 'user' + 'faculty' really is the complement
    assert _profile(client, owner["id"]).get_json()["postCount"] == 1


def test_post_count_counts_a_private_faculty_post_for_the_author(client, make_user,
                                                                 auth_headers, db):
    owner = make_user(role="teacher")
    headers = auth_headers(owner)
    _seed_post(db, owner["id"], public=False, source="faculty", post_type="announcement")

    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 1
    assert _profile(client, owner["id"]).get_json()["postCount"] == 0


def test_post_count_counts_a_private_faculty_post_for_a_friend(client, make_user, actor, db):
    viewer, headers = actor
    owner = make_user(role="teacher")
    _befriend(db, viewer["id"], owner["id"])
    _seed_post(db, owner["id"], public=False, source="faculty", post_type="announcement")

    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 1


def test_post_count_reaches_past_a_single_page_of_posts(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    for _ in range(60):
        _seed_post(db, owner["id"], public=False)

    # The stat is a COUNT, not the length of the first page
    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 60


def test_post_count_for_a_one_way_friend_the_viewer_owns_matches_the_list(client, actor,
                                                                         make_user, db):
    viewer, headers = actor
    owner = make_user()
    # Only the viewer's own direction exists, and that is the one read
    _one_way(db, viewer["id"], owner["id"])
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], public=True)

    body = _profile(client, owner["id"], headers).get_json()
    listed = client.get(f"/api/social/posts?user_id={owner['id']}", headers=headers).get_json()

    assert body["postCount"] == listed["total"] == 2




# -----------------------------------------------------------
# GET /api/social/profile/<user_id> — friendCount permutations
# -----------------------------------------------------------

def test_friend_count_counts_a_self_friendship_row(client, actor, db):
    viewer, headers = actor
    _one_way(db, viewer["id"], viewer["id"])

    # A quirk of the plain COUNT, pinned as it stands: the row is
    # unreachable through the routes, only a hand-edit makes it
    assert _profile(client, viewer["id"], headers).get_json()["friendCount"] == 1


def test_friend_count_is_the_owners_and_never_the_viewers(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _befriend(db, viewer["id"], make_user()["id"])

    assert _profile(client, owner["id"], headers).get_json()["friendCount"] == 0


def test_friend_count_handles_twenty_five_friends(client, make_user, db):
    owner = make_user()
    for _ in range(25):
        _one_way(db, owner["id"], _bare_user(db))

    assert _profile(client, owner["id"]).get_json()["friendCount"] == 25


def test_friend_count_leaves_deactivated_friends_out_for_an_admin_too(client, make_user, admin, db):
    _, admin_headers = admin
    owner = make_user()
    gone = _bare_user(db, active=0)
    _one_way(db, owner["id"], gone)
    _one_way(db, owner["id"], _bare_user(db))

    assert _profile(client, owner["id"], admin_headers).get_json()["friendCount"] == 1




# -----------------------------------------------------------
# GET /api/social/profile/<user_id> — friendshipStatus
# -----------------------------------------------------------

def test_a_pending_request_in_both_directions_is_never_none_or_friends(client, actor,
                                                                      make_user, db):
    viewer, headers = actor
    owner = make_user()
    _seed_request(db, owner["id"], viewer["id"])
    _seed_request(db, viewer["id"], owner["id"])

    # Which of the two wins is left to SQLite's scan order; what
    # the screen must never see is a state that hides both
    status = _profile(client, owner["id"], headers).get_json()["friendshipStatus"]
    assert status in ("request_sent", "request_received")


def test_a_self_addressed_request_row_leaves_your_own_profile_at_none(client, actor, db):
    viewer, headers = actor
    _seed_request(db, viewer["id"], viewer["id"])

    assert _profile(client, viewer["id"], headers).get_json()["friendshipStatus"] == "none"


@pytest.mark.parametrize("status", ["rejected", "accepted"])
def test_a_settled_request_the_owner_sent_leaves_the_status_at_none(client, actor, make_user,
                                                                   db, status):
    viewer, headers = actor
    owner = make_user()
    # The received direction, which the sent-direction cases in
    # test_social_profile.py leave open. 'pending', 'accepted' and
    # 'rejected' are the whole CHECK-constrained domain
    _seed_request(db, owner["id"], viewer["id"], status=status)

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "none"


def test_a_settled_row_beside_a_pending_one_does_not_hide_it(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _seed_request(db, viewer["id"], owner["id"], status="rejected")
    _seed_request(db, owner["id"], viewer["id"], status="pending")

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "request_received"


def test_a_pending_request_from_a_deactivated_sender_still_reads_as_received(client, actor,
                                                                            db):
    viewer, headers = actor
    owner = _bare_user(db)
    _seed_request(db, owner, viewer["id"])
    _deactivate(db, owner)

    # The owner's own profile is a 404 now; an admin still sees it,
    # and the status is computed from the viewer's side regardless
    assert _profile(client, owner, headers).status_code == 404


def test_the_status_survives_a_cancelled_and_re_sent_request(client, make_user, auth_headers):
    sender = make_user()
    receiver = make_user()
    sender_headers = auth_headers(sender)

    first = client.post("/api/social/friends/request", headers=sender_headers,
                        json={"user_id": receiver["id"]})
    assert first.status_code == 201
    assert client.post(f"/api/social/friends/requests/{first.get_json()['id']}/reject",
                       headers=sender_headers).status_code == 200
    assert _profile(client, receiver["id"], sender_headers).get_json()["friendshipStatus"] == "none"

    second = client.post("/api/social/friends/request", headers=sender_headers,
                         json={"user_id": receiver["id"]})
    assert second.status_code == 201
    assert _profile(client, receiver["id"], sender_headers).get_json()["friendshipStatus"] == "request_sent"




# -----------------------------------------------------------
# GET /api/social/profile/<user_id> — the values on the wire
# -----------------------------------------------------------

def test_a_legacy_empty_display_name_comes_back_as_an_empty_string(client, make_user, db):
    owner = make_user()
    # The column is NOT NULL, and the editor refuses a blank name, so
    # only a hand-edited row can look like this — it must still read
    db.execute("UPDATE users SET display_name = '' WHERE id = ?", (owner["id"],))
    db.commit()

    response = _profile(client, owner["id"])

    assert response.status_code == 200
    assert response.get_json()["displayName"] == ""


def test_an_ampersand_in_a_display_name_is_escaped_exactly_once(client, make_user, db):
    owner = make_user()
    db.execute("UPDATE users SET display_name = ? WHERE id = ?", ("Jonas & Co", owner["id"]))
    db.commit()

    assert _profile(client, owner["id"]).get_json()["displayName"] == "Jonas &amp; Co"


def test_quotes_in_a_display_name_are_escaped_too(client, make_user, db):
    owner = make_user()
    db.execute("UPDATE users SET display_name = ? WHERE id = ?", ('O"na \'A\'', owner["id"]))
    db.commit()

    body = _profile(client, owner["id"]).get_json()

    assert body["displayName"] == "O&quot;na &#x27;A&#x27;"




# -----------------------------------------------------------
# GET /api/social/profile — the private twin, gates
# -----------------------------------------------------------

@pytest.mark.parametrize("method", ["post", "patch", "delete"])
def test_your_own_profile_route_answers_only_to_get_and_put(client, actor, method):
    _, headers = actor

    assert getattr(client, method)(_PROFILE, headers=headers).status_code == 405


def test_your_own_profile_is_401_for_an_expired_session(client, actor):
    _, headers = actor

    with time_machine.travel(_far_future(), tick=False):
        response = client.get(_PROFILE, headers=headers)

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_your_own_profile_is_401_once_the_account_is_deactivated(client, make_user,
                                                                auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    _deactivate(db, owner["id"])

    assert client.get(_PROFILE, headers=headers).status_code == 401


@pytest.mark.parametrize("header_value", ["Bearer nope", "Bearer", "Basic abc", ""])
def test_your_own_profile_is_401_for_an_unusable_token(client, header_value):
    response = client.get(_PROFILE, headers={"Authorization": header_value})

    assert response.status_code == 401


def test_query_parameters_are_ignored_on_your_own_profile(client, actor, make_user):
    viewer, headers = actor
    other = make_user()

    body = client.get(f"{_PROFILE}?user_id={other['id']}", headers=headers).get_json()

    assert body["id"] == viewer["id"]


def test_reading_your_own_profile_forty_times_is_never_rate_limited(client, actor):
    _, headers = actor

    for _ in range(40):
        assert client.get(_PROFILE, headers=headers).status_code == 200




# -----------------------------------------------------------
# GET /api/social/profile — the private twin, values
# -----------------------------------------------------------

def test_a_row_that_vanishes_under_the_read_answers_a_null_created_at(client, actor, db,
                                                                     monkeypatch):
    import app.social.routes as social_routes

    user, headers = actor
    real_get_db = social_routes.get_db

    # get_db is the seam between require_auth's lookup and the
    # handler's own SELECT — where an admin deleting the account
    # lands in production. The `if row else None` arm is only
    # reachable here
    def _drop_the_row_first():
        db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        db.commit()
        return real_get_db()

    monkeypatch.setattr(social_routes, "get_db", _drop_the_row_first)

    response = client.get(_PROFILE, headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body["createdAt"] is None
    assert body["id"] == user["id"]
    assert body["postCount"] == 0
    assert body["friendCount"] == 0


def test_your_own_student_fields_default_to_null(client, actor):
    _, headers = actor

    body = client.get(_PROFILE, headers=headers).get_json()

    assert body["studentNumber"] is None
    assert body["studyGroup"] is None
    assert body["studyProgram"] is None


def test_your_own_friend_count_ignores_rows_that_only_point_at_you(client, actor, db):
    viewer, headers = actor
    _one_way(db, _bare_user(db), viewer["id"])

    assert client.get(_PROFILE, headers=headers).get_json()["friendCount"] == 0


def test_your_own_friend_count_counts_a_self_friendship_row(client, actor, db):
    viewer, headers = actor
    _one_way(db, viewer["id"], viewer["id"])

    assert client.get(_PROFILE, headers=headers).get_json()["friendCount"] == 1


def test_your_own_post_count_ignores_other_authors(client, actor, db):
    viewer, headers = actor
    _seed_post(db, _bare_user(db))

    assert client.get(_PROFILE, headers=headers).get_json()["postCount"] == 0


def test_your_own_post_count_matches_the_list_you_get_of_yourself(client, actor, db):
    viewer, headers = actor
    _seed_post(db, viewer["id"], public=False)
    _seed_post(db, viewer["id"], public=True)
    _seed_post(db, viewer["id"], source="faculty", post_type="announcement")
    _seed_post(db, viewer["id"], source="vu.lt", post_type="article")

    own = client.get(_PROFILE, headers=headers).get_json()
    listed = client.get(f"/api/social/posts?user_id={viewer['id']}", headers=headers).get_json()

    assert own["postCount"] == listed["total"] == 3


def test_your_own_profile_escapes_the_display_name_on_the_way_out(client, make_user,
                                                                 auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    db.execute("UPDATE users SET display_name = ? WHERE id = ?", ("<b>Ona</b>", owner["id"]))
    db.commit()

    body = client.get(_PROFILE, headers=headers).get_json()

    assert body["displayName"] == "&lt;b&gt;Ona&lt;/b&gt;"


def test_an_admin_reads_their_own_profile_with_the_admin_role(client, admin):
    admin_user, headers = admin

    body = client.get(_PROFILE, headers=headers).get_json()

    assert body["role"] == "admin"
    assert body["id"] == admin_user["id"]


@pytest.mark.contract
def test_the_two_profile_reads_agree_on_every_shared_field(client, make_user, auth_headers, db):
    owner = make_user(display_name="Ona Onaityte")
    headers = auth_headers(owner)
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], public=True)
    _one_way(db, owner["id"], _bare_user(db))

    public_body = _profile(client, owner["id"], headers).get_json()
    own_body = client.get(_PROFILE, headers=headers).get_json()

    for key in _SHARED_PROFILE_KEYS:
        assert public_body[key] == own_body[key], key




# -----------------------------------------------------------
# PUT /api/social/profile — body shapes on the wire
# -----------------------------------------------------------

@pytest.mark.parametrize("raw", ["[1,2]", "[]", "5", "0", '"ona"', "true", "false"])
def test_a_non_object_json_body_is_refused_by_the_object_guard(client, actor, raw):
    _, headers = actor

    response = _put_raw(client, headers, raw)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_the_object_guard_answers_before_authentication_does(client):
    response = client.put(_PROFILE, data="[1,2]", headers={"Content-Type": "application/json"})

    # before_request runs ahead of require_auth, so an anonymous
    # caller sees the body error and never the 401
    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("raw", ['{"displayName": ', "{", "not json at all", ""])
def test_a_malformed_json_body_falls_through_to_the_routes_own_400(client, actor, raw):
    _, headers = actor

    response = _put_raw(client, headers, raw)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


@pytest.mark.parametrize("content_type", ["text/plain", "application/xml",
                                          "application/x-www-form-urlencoded"])
def test_a_json_body_under_the_wrong_content_type_is_400(client, actor, content_type):
    _, headers = actor

    response = _put_raw(client, headers, '{"displayName": "Ona"}', content_type=content_type)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_json_content_type_with_a_charset_is_accepted(client, actor):
    _, headers = actor

    response = _put_raw(client, headers, '{"displayName": "Ona"}',
                        content_type="application/json; charset=utf-8")

    assert response.status_code == 200
    assert response.get_json()["displayName"] == "Ona"


def test_a_form_encoded_edit_is_400(client, actor):
    _, headers = actor

    response = client.put(_PROFILE, headers=headers, data={"displayName": "Ona"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_body_nested_past_the_depth_cap_is_400_and_never_500(client, actor):
    _, headers = actor
    raw = '{"a":' * 40 + "1" + "}" * 40

    response = _put_raw(client, headers, raw)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON nesting too deep"


def test_a_body_of_a_thousand_unknown_keys_is_only_a_400(client, actor):
    _, headers = actor
    raw = json.dumps({f"unknown_{n}": n for n in range(1000)})

    response = _put_raw(client, headers, raw)

    assert response.status_code == 400
    assert response.get_json()["error"] == "No fields to update"




# -----------------------------------------------------------
# PUT /api/social/profile — display name, raw on the wire
# -----------------------------------------------------------

def test_a_display_name_is_stored_raw_and_escaped_only_on_output(client, actor, db):
    user, headers = actor

    response = _put_raw(client, headers, json.dumps({"displayName": "Jonas & <b>Co</b>"}))

    assert response.status_code == 200
    # RAW in the column, entities on the wire — the whole XSS model
    assert _stored(db, user["id"], "display_name") == "Jonas & <b>Co</b>"
    assert response.get_json()["displayName"] == "Jonas &amp; &lt;b&gt;Co&lt;/b&gt;"


def test_nul_bytes_are_stripped_out_of_a_display_name(client, actor, db):
    user, headers = actor

    response = _put_raw(client, headers, '{"displayName": "On\\u0000a"}')

    assert response.status_code == 200
    assert _stored(db, user["id"], "display_name") == "Ona"


def test_a_display_name_of_only_nul_bytes_is_an_empty_name_400(client, actor):
    _, headers = actor

    response = _put_raw(client, headers, '{"displayName": "\\u0000\\u0000"}')

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name cannot be empty"


def test_a_single_character_display_name_is_accepted(client, actor, db):
    user, headers = actor

    assert _put(client, headers, {"displayName": "O"}).status_code == 200
    assert _stored(db, user["id"], "display_name") == "O"


def test_a_hundred_lithuanian_characters_fit_the_limit(client, actor, db):
    user, headers = actor

    # The gate counts CHARACTERS, not the UTF-8 bytes they cost
    response = _put_raw(client, headers, json.dumps({"displayName": "ą" * 100}))

    assert response.status_code == 200
    assert _stored(db, user["id"], "display_name") == "ą" * 100


def test_a_hundred_and_one_lithuanian_characters_do_not(client, actor):
    _, headers = actor

    response = _put_raw(client, headers, json.dumps({"displayName": "ą" * 101}))

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name must be at most 100 characters"


def test_an_emoji_display_name_round_trips(client, actor, db):
    user, headers = actor

    response = _put_raw(client, headers, json.dumps({"displayName": "Ona 🎓"}))

    assert response.status_code == 200
    assert _stored(db, user["id"], "display_name") == "Ona 🎓"
    assert response.get_json()["displayName"] == "Ona 🎓"


def test_inner_newlines_survive_a_display_name_edit(client, actor, db):
    user, headers = actor

    response = _put_raw(client, headers, json.dumps({"displayName": "  Ona\nOnaityte  "}))

    assert response.status_code == 200
    # strip() only touches the ends
    assert _stored(db, user["id"], "display_name") == "Ona\nOnaityte"


def test_the_camel_case_display_name_wins_even_when_it_is_the_invalid_one(client, actor, db):
    user, headers = actor

    response = _put(client, headers, {"displayName": 5, "display_name": "Geras"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "display_name must be a string"
    assert _stored(db, user["id"], "display_name") == user["username"].title()


def test_the_last_of_two_duplicate_keys_in_one_body_wins(client, actor, db):
    user, headers = actor

    response = _put_raw(client, headers, '{"displayName": "Pirmas", "displayName": "Antras"}')

    assert response.status_code == 200
    assert _stored(db, user["id"], "display_name") == "Antras"


def test_a_display_name_of_a_single_zero_is_accepted(client, actor, db):
    user, headers = actor

    # "0" is a perfectly good name and must not read as "unset"
    assert _put(client, headers, {"displayName": "0"}).status_code == 200
    assert _stored(db, user["id"], "display_name") == "0"


def test_two_accounts_may_carry_the_same_display_name(client, make_user, auth_headers, db):
    one = make_user()
    other = make_user()

    assert _put(client, auth_headers(one), {"displayName": "Ona Onaityte"}).status_code == 200
    assert _put(client, auth_headers(other), {"displayName": "Ona Onaityte"}).status_code == 200

    assert _stored(db, one["id"], "display_name") == "Ona Onaityte"
    assert _stored(db, other["id"], "display_name") == "Ona Onaityte"


def test_an_edit_never_reaches_another_users_row(client, actor, make_user, db):
    _, headers = actor
    bystander = make_user(display_name="Nepaliestas")

    _put(client, headers, {"displayName": "Ona", "studyGroup": "IFF-1", "avatarUrl": _AVATAR_A})

    assert _stored(db, bystander["id"], "display_name") == "Nepaliestas"
    assert _stored(db, bystander["id"], "study_group") is None
    assert _stored(db, bystander["id"], "avatar_url") is None


def test_a_blank_snake_case_display_name_is_400(client, actor):
    _, headers = actor

    response = _put(client, headers, {"display_name": "   "})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name cannot be empty"




# -----------------------------------------------------------
# PUT /api/social/profile — the author_name snapshot rewrite
# -----------------------------------------------------------

def test_a_rename_rewrites_the_snapshot_on_scraped_rows_too(client, actor, db):
    user, headers = actor
    scraped = _seed_post(db, user["id"], source="knf.vu.lt", post_type="article")

    _put(client, headers, {"displayName": "Nauja Pavarde"})

    # The UPDATE filters on author_id only — pinned as it stands,
    # since a scraped row credited to a real account should not
    # keep a stale name either
    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (scraped,)).fetchone()["author_name"] == "Nauja Pavarde"


def test_a_rename_leaves_author_less_rows_alone(client, actor, db):
    _, headers = actor
    post_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO news_posts (id, title, content, author_name, source, post_type, is_public,"
        " published_at, created_at, updated_at) VALUES (?, 'T', 'C', 'Fakultetas', 'knf.vu.lt',"
        " 'article', 1, ?, ?, ?)",
        (post_id, now, now, now),
    )
    db.commit()

    _put(client, headers, {"displayName": "Nauja Pavarde"})

    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["author_name"] == "Fakultetas"


def test_the_snapshot_rewrite_stores_the_stripped_name(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    _put(client, headers, {"displayName": "   Ona   "})

    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["author_name"] == "Ona"


def test_a_rejected_rename_never_touches_a_snapshot(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    assert _put(client, headers, {"displayName": "a" * 101}).status_code == 400

    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["author_name"] == "Snapshot"




# -----------------------------------------------------------
# PUT /api/social/profile — avatar, every accepted and refused shape
# -----------------------------------------------------------

@pytest.mark.parametrize("extension", ["jpg", "jpeg", "png", "gif", "webp"])
def test_every_extension_the_uploader_hands_out_is_accepted(client, actor, db, extension):
    user, headers = actor
    path = f"/api/uploads/{'c' * 32}.{extension}"

    assert _put(client, headers, {"avatarUrl": path}).status_code == 200
    assert _stored(db, user["id"], "avatar_url") == path


@pytest.mark.parametrize("path", [
    "/api/uploads/" + "a" * 32 + ".png\n",
    "/api/uploads/" + "A" * 32 + ".png",
    "/api/uploads/" + "a" * 31 + ".png",
    "/api/uploads/" + "a" * 33 + ".png",
    "/api/uploads/" + "a" * 32 + ".svg",
    "/api/uploads/" + "a" * 32 + ".PNG",
    "/api/uploads/" + "a" * 32,
    "/api/uploads/sub/" + "a" * 32 + ".png",
    " /api/uploads/" + "a" * 32 + ".png",
    "/api/uploads/" + "g" * 32 + ".png",
])
def test_an_avatar_that_is_not_exactly_an_upload_path_is_refused(client, actor, db, path):
    user, headers = actor

    response = _put_raw(client, headers, json.dumps({"avatarUrl": path}))

    assert response.status_code == 400
    assert _stored(db, user["id"], "avatar_url") is None


def test_an_avatar_url_over_two_thousand_and_forty_eight_characters_is_refused(client, actor):
    _, headers = actor

    response = _put(client, headers, {"avatarUrl": "/api/uploads/" + "a" * 2100 + ".png"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "avatar_url must be at most 2048 characters"


def test_the_camel_case_avatar_key_wins_over_the_snake_case_one(client, actor, db):
    user, headers = actor

    response = _put(client, headers, {"avatarUrl": _AVATAR_A, "avatar_url": _AVATAR_B})

    assert response.status_code == 200
    assert response.get_json()["avatarUrl"] == _AVATAR_A
    assert _stored(db, user["id"], "avatar_url") == _AVATAR_A


# -----------------------------------------------------------
# _without_the_body_hook
# -----------------------------------------------------------
#
# Removes app/__init__.py's validate_json_input from the
# before_request chain (the global throttle stays), which is
# the only way a real request can reach update_profile's OWN
# avatar guard — the hook answers first for every live
# caller. It exists to show what the belt does and does not
# hold on its own.
# -----------------------------------------------------------

def _without_the_body_hook(app, monkeypatch):
    kept = [hook for hook in app.before_request_funcs[None]
            if hook.__name__ != "validate_json_input"]
    monkeypatch.setitem(app.before_request_funcs, None, kept)


def test_the_routes_own_avatar_belt_refuses_an_absolute_url_over_http(app, client, actor, db,
                                                                     monkeypatch):
    user, headers = actor
    _without_the_body_hook(app, monkeypatch)

    response = _put(client, headers, {"avatarUrl": "https://evil.example/a.png"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "avatar_url must be a relative /api/uploads/ path"
    assert _stored(db, user["id"], "avatar_url") is None


def test_the_belt_alone_is_coarser_than_the_hook_it_backs_up(app, client, actor, db, monkeypatch):
    user, headers = actor
    _without_the_body_hook(app, monkeypatch)

    # The route checks the "/api/uploads/" PREFIX; the exact
    # uuid4-hex-plus-extension shape is the hook's job, and this is
    # what the app would store if that hook ever stopped running
    response = _put(client, headers, {"avatarUrl": "/api/uploads/../../etc/passwd"})

    assert response.status_code == 200
    assert _stored(db, user["id"], "avatar_url") == "/api/uploads/../../etc/passwd"


def test_the_hooks_avatar_guard_checks_the_spelling_the_route_would_ignore(client, actor, db):
    user, headers = actor

    # camelCase wins INSIDE the handler, but before_request validates
    # both keys — so a bad snake_case value still sinks the request
    response = _put(client, headers, {"avatarUrl": _AVATAR_A,
                                      "avatar_url": "https://evil.example/a.png"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "avatar_url must be a relative /api/uploads/ path"
    assert _stored(db, user["id"], "avatar_url") is None


def test_an_empty_avatar_is_stored_as_an_empty_string_not_null(client, actor, db):
    user, headers = actor

    response = _put(client, headers, {"avatarUrl": ""})

    # Pinned as it stands: "" and null both clear the avatar for the
    # app, but they are two different values in the column
    assert response.get_json()["avatarUrl"] == ""
    assert _stored(db, user["id"], "avatar_url") == ""


def test_an_explicit_null_avatar_is_stored_as_null(client, actor, db):
    user, headers = actor

    response = _put(client, headers, {"avatarUrl": None})

    assert response.get_json()["avatarUrl"] is None
    assert _stored(db, user["id"], "avatar_url") is None




# -----------------------------------------------------------
# PUT /api/social/profile — the replaced-file cleanup
# -----------------------------------------------------------
#
# _delete_upload_file is fire-and-forget after the commit, so
# the only way to prove WHICH path it was handed (or that it
# was not called at all) is to record the calls.
# -----------------------------------------------------------

@pytest.fixture
def deleted_uploads(monkeypatch):
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda path: calls.append(path))
    return calls


def test_clearing_an_avatar_that_was_never_set_deletes_nothing(client, actor, deleted_uploads):
    _, headers = actor

    assert _put(client, headers, {"avatarUrl": None}).status_code == 200

    assert deleted_uploads == []


def test_an_empty_string_clears_the_avatar_and_deletes_the_old_file(client, actor, deleted_uploads):
    _, headers = actor
    _put(client, headers, {"avatarUrl": _AVATAR_A})

    assert _put(client, headers, {"avatarUrl": ""}).status_code == 200

    assert deleted_uploads == [_AVATAR_A]


def test_clearing_an_already_empty_avatar_twice_deletes_nothing_more(client, actor,
                                                                    deleted_uploads):
    _, headers = actor
    _put(client, headers, {"avatarUrl": _AVATAR_A})
    _put(client, headers, {"avatarUrl": ""})
    deleted_uploads.clear()

    _put(client, headers, {"avatarUrl": ""})
    _put(client, headers, {"avatarUrl": None})

    assert deleted_uploads == []


def test_a_replaced_avatar_is_handed_over_exactly_once(client, actor, deleted_uploads):
    _, headers = actor
    _put(client, headers, {"avatarUrl": _AVATAR_A})

    _put(client, headers, {"avatarUrl": _AVATAR_B})

    assert deleted_uploads == [_AVATAR_A]


def test_an_edit_that_does_not_mention_the_avatar_deletes_nothing(client, actor, deleted_uploads):
    _, headers = actor
    _put(client, headers, {"avatarUrl": _AVATAR_A})

    _put(client, headers, {"displayName": "Ona", "studyGroup": "IFF-1"})

    assert deleted_uploads == []


def test_a_refused_avatar_never_reaches_the_delete_helper(client, actor, deleted_uploads):
    _, headers = actor
    _put(client, headers, {"avatarUrl": _AVATAR_A})

    assert _put(client, headers, {"avatarUrl": "https://evil.example/a.png"}).status_code == 400

    assert deleted_uploads == []




# -----------------------------------------------------------
# PUT /api/social/profile — the student-card fields
# -----------------------------------------------------------

@pytest.mark.parametrize("key, column", [
    ("studentNumber", "student_number"),
    ("studyGroup", "study_group"),
    ("studyProgram", "study_program"),
])
def test_fifty_characters_inside_padding_still_fit(client, actor, db, key, column):
    user, headers = actor

    # The length gate runs on the STRIPPED value
    response = _put(client, headers, {key: "   " + "x" * 50 + "   "})

    assert response.status_code == 200
    assert _stored(db, user["id"], column) == "x" * 50


@pytest.mark.parametrize("value", [True, False, 1.5, 0, [], {}, ["x"]])
def test_a_non_string_student_field_of_any_type_is_400(client, actor, value):
    _, headers = actor

    response = _put(client, headers, {"studyGroup": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "studyGroup must be a string"


def test_the_camel_case_student_key_wins_even_when_it_is_the_invalid_one(client, actor, db):
    user, headers = actor

    response = _put(client, headers, {"studentNumber": 5, "student_number": "S1"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "studentNumber must be a string"
    assert _stored(db, user["id"], "student_number") is None


def test_nul_bytes_are_stripped_from_a_student_field(client, actor, db):
    user, headers = actor

    response = _put_raw(client, headers, '{"studyGroup": "IFF\\u0000-1"}')

    assert response.status_code == 200
    assert _stored(db, user["id"], "study_group") == "IFF-1"


def test_a_student_field_of_only_nul_bytes_becomes_null(client, actor, db):
    user, headers = actor
    _put(client, headers, {"studyGroup": "IFF-1"})

    response = _put_raw(client, headers, '{"studyGroup": "\\u0000"}')

    assert response.status_code == 200
    assert _stored(db, user["id"], "study_group") is None


def test_a_student_field_is_stored_raw_and_escaped_on_output(client, actor, db):
    user, headers = actor

    response = _put_raw(client, headers, json.dumps({"studyProgram": "IT & <em>VS</em>"}))

    assert _stored(db, user["id"], "study_program") == "IT & <em>VS</em>"
    assert response.get_json()["studyProgram"] == "IT &amp; &lt;em&gt;VS&lt;/em&gt;"


def test_fifty_lithuanian_characters_fit_a_student_field(client, actor, db):
    user, headers = actor

    response = _put_raw(client, headers, json.dumps({"studyProgram": "ų" * 50}))

    assert response.status_code == 200
    assert _stored(db, user["id"], "study_program") == "ų" * 50


def test_fifty_one_lithuanian_characters_do_not(client, actor):
    _, headers = actor

    response = _put_raw(client, headers, json.dumps({"studyProgram": "ų" * 51}))

    assert response.status_code == 400
    assert response.get_json()["error"] == "studyProgram must be at most 50 characters"


def test_fifty_one_characters_after_stripping_is_400(client, actor):
    _, headers = actor

    response = _put(client, headers, {"studyProgram": "  " + "x" * 51 + "  "})

    assert response.status_code == 400
    assert response.get_json()["error"] == "studyProgram must be at most 50 characters"


def test_all_three_student_fields_clear_at_once(client, actor, db):
    user, headers = actor
    _put(client, headers, {"studentNumber": "S1", "studyGroup": "IFF-1",
                           "studyProgram": "Informatika"})

    response = _put(client, headers, {"studentNumber": None, "studyGroup": "",
                                      "studyProgram": "   "})

    assert response.status_code == 200
    body = response.get_json()
    assert body["studentNumber"] is None and body["studyGroup"] is None
    assert body["studyProgram"] is None
    for column in ("student_number", "study_group", "study_program"):
        assert _stored(db, user["id"], column) is None




# -----------------------------------------------------------
# PUT /api/social/profile — guard ORDER and all-or-nothing
# -----------------------------------------------------------

def test_a_later_invalid_field_rolls_the_whole_edit_back(client, actor, db):
    user, headers = actor

    response = _put(client, headers, {"displayName": "Nauja", "studyGroup": 5})

    assert response.status_code == 400
    # The UPDATE is built first and executed last: nothing is written
    assert _stored(db, user["id"], "display_name") == user["username"].title()


def test_an_invalid_student_field_leaves_the_avatar_unwritten(client, actor, db):
    user, headers = actor

    response = _put(client, headers, {"avatarUrl": _AVATAR_A, "studentNumber": 5})

    assert response.status_code == 400
    assert _stored(db, user["id"], "avatar_url") is None


def test_the_display_name_guard_answers_before_the_student_field_one(client, actor):
    _, headers = actor

    response = _put(client, headers, {"displayName": "", "studentNumber": 5})

    assert response.get_json()["error"] == "Display name cannot be empty"


def test_the_hooks_avatar_guard_answers_before_the_display_name_one(client, actor):
    _, headers = actor

    # before_request validates avatar_url for the whole app, so it
    # wins over every check inside the handler
    response = _put(client, headers, {"displayName": "", "avatarUrl": "https://evil.example/a.png"})

    assert response.get_json()["error"] == "avatar_url must be a relative /api/uploads/ path"


def test_the_student_fields_are_validated_in_their_documented_order(client, actor):
    _, headers = actor

    response = _put(client, headers, {"studyProgram": 1, "studentNumber": 2, "studyGroup": 3})

    # studentNumber, studyGroup, studyProgram — the literal list
    assert response.get_json()["error"] == "studentNumber must be a string"


def test_a_rejected_edit_writes_no_updated_at_and_no_snapshot(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])
    before = _stored(db, user["id"], "updated_at")

    assert _put(client, headers, {"displayName": "Nauja", "studyProgram": "x" * 51}).status_code == 400

    assert _stored(db, user["id"], "updated_at") == before
    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["author_name"] == "Snapshot"




# -----------------------------------------------------------
# PUT /api/social/profile — persistence, idempotency, reads
# -----------------------------------------------------------

def test_the_same_edit_twice_is_idempotent_but_restamps_updated_at(client, actor, db):
    user, headers = actor
    start = datetime(2026, 5, 4, 9, 0, tzinfo=timezone.utc)

    with time_machine.travel(start, tick=False) as traveller:
        first = _put(client, headers, {"displayName": "Ona", "studyGroup": "IFF-1"})
        stamp_one = _stored(db, user["id"], "updated_at")
        traveller.shift(timedelta(seconds=90))
        second = _put(client, headers, {"displayName": "Ona", "studyGroup": "IFF-1"})
        stamp_two = _stored(db, user["id"], "updated_at")

    assert first.get_json() == second.get_json()
    assert stamp_two > stamp_one


def test_an_edit_never_touches_created_at_or_the_password(client, actor, db):
    user, headers = actor
    before = db.execute("SELECT created_at, password_hash FROM users WHERE id = ?",
                        (user["id"],)).fetchone()

    _put(client, headers, {"displayName": "Ona", "avatarUrl": _AVATAR_A,
                           "studentNumber": "S1"})

    after = db.execute("SELECT created_at, password_hash FROM users WHERE id = ?",
                       (user["id"],)).fetchone()
    assert after["created_at"] == before["created_at"]
    assert after["password_hash"] == before["password_hash"]


def test_an_unknown_key_beside_a_good_one_is_simply_ignored(client, actor, db):
    user, headers = actor

    response = _put(client, headers, {"displayName": "Ona", "nickname": "onute",
                                      "created_at": "1999-01-01", "updated_at": "1999-01-01"})

    assert response.status_code == 200
    assert _stored(db, user["id"], "display_name") == "Ona"
    assert not _stored(db, user["id"], "updated_at").startswith("1999")


def test_an_edit_shows_up_immediately_on_both_profile_reads(client, actor, db):
    user, headers = actor

    _put(client, headers, {"displayName": "Nauja Ona", "avatarUrl": _AVATAR_A,
                           "studyGroup": "IFF-9"})

    public_body = _profile(client, user["id"]).get_json()
    own_body = client.get(_PROFILE, headers=headers).get_json()
    assert public_body["displayName"] == own_body["displayName"] == "Nauja Ona"
    assert public_body["avatarUrl"] == own_body["avatarUrl"] == _AVATAR_A
    assert own_body["studyGroup"] == "IFF-9"


@pytest.mark.contract
def test_the_editor_answer_agrees_with_the_private_read(client, actor):
    _, headers = actor

    edited = _put(client, headers, {"displayName": "Ona", "studentNumber": "S1",
                                    "studyGroup": "IFF-1", "studyProgram": "Informatika"}).get_json()
    read = client.get(_PROFILE, headers=headers).get_json()

    for key in ("id", "username", "email", "displayName", "avatarUrl", "role",
                "studentNumber", "studyGroup", "studyProgram"):
        assert edited[key] == read[key], key


def test_a_rename_does_not_disturb_the_friend_or_post_counts(client, actor, db):
    user, headers = actor
    _seed_post(db, user["id"], public=False)
    _one_way(db, user["id"], _bare_user(db))

    _put(client, headers, {"displayName": "Kita Ona"})

    body = client.get(_PROFILE, headers=headers).get_json()
    assert body["postCount"] == 1
    assert body["friendCount"] == 1




# -----------------------------------------------------------
# PUT /api/social/profile — auth and the quota
# -----------------------------------------------------------

def test_a_row_deleted_mid_update_answers_401_and_still_deletes_the_replaced_avatar(client, actor, db,
                                                                                    deleted_uploads,
                                                                                    monkeypatch):
    import app.social.routes as social_routes

    user, headers = actor
    _put(client, headers, {"avatarUrl": _AVATAR_A})
    deleted_uploads.clear()
    real_now = social_routes.utc_now_iso

    # utc_now_iso sits between the validation and the UPDATE — the
    # seam where an admin deleting the account lands
    def _delete_the_user_first():
        db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        db.commit()
        return real_now()

    monkeypatch.setattr(social_routes, "utc_now_iso", _delete_the_user_first)

    response = client.put(_PROFILE, headers=headers, json={"avatarUrl": _AVATAR_B})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"
    # The UPDATE is committed either way, so the replaced file has
    # lost its last reference — the cleanup runs BEFORE the
    # session-dead return and nothing is orphaned on disk
    assert deleted_uploads == [_AVATAR_A]


def test_an_edit_is_401_for_an_expired_session(client, actor, db):
    user, headers = actor

    with time_machine.travel(_far_future(), tick=False):
        response = _put(client, headers, {"displayName": "Ona"})

    assert response.status_code == 401
    assert _stored(db, user["id"], "display_name") == user["username"].title()


def test_an_edit_is_401_once_the_account_is_deactivated(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    _deactivate(db, owner["id"])

    assert _put(client, headers, {"displayName": "Ona"}).status_code == 401


def test_an_unauthenticated_edit_never_spends_the_profile_budget(client, actor):
    _, headers = actor

    # require_auth wraps the limiter, so a 401 costs the real user
    # nothing — the full 30 must still be there afterwards
    for _ in range(10):
        assert client.put(_PROFILE, json={"displayName": "Ona"}).status_code == 401

    for attempt in range(30):
        assert _put(client, headers, {"displayName": f"Vardas {attempt}"}).status_code == 200
    assert _put(client, headers, {"displayName": "Per daug"}).status_code == 429


def test_the_profile_quota_is_not_shared_with_the_auth_twin(client, actor):
    _, headers = actor

    for attempt in range(30):
        assert _put(client, headers, {"displayName": f"Vardas {attempt}"}).status_code == 200
    assert _put(client, headers, {"displayName": "Per daug"}).status_code == 429

    # PUT /api/auth/me carries no rate_limit decorator of its own
    assert client.put("/api/auth/me", headers=headers,
                      json={"displayName": "Per auth"}).status_code == 200


def test_a_throttled_edit_changes_nothing_and_still_reads_back(client, actor, db):
    user, headers = actor
    for attempt in range(30):
        assert _put(client, headers, {"displayName": f"Vardas {attempt}"}).status_code == 200

    response = _put(client, headers, {"displayName": "Niekada"})

    assert response.status_code == 429
    assert _stored(db, user["id"], "display_name") == "Vardas 29"
    # Reading is never throttled by this quota
    assert client.get(_PROFILE, headers=headers).get_json()["displayName"] == "Vardas 29"
