# -----------------------------------------------------------
#  [*] Social — profiles (read and edit)
#
#  What this module proves about app/social/routes.py:
#
#    - GET /api/social/profile/<id> works WITHOUT a token (the
#      app must work without login), never leaks the email, and
#      answers 404 for an unknown user and for a deactivated
#      one — to everyone except an admin.
#    - postCount is the same number the post list actually
#      returns: source 'user' AND 'faculty', with the private
#      posts counted only for the author and their accepted
#      friends. Every viewer class is checked against the
#      "total" of GET /api/social/posts, so the stat and the
#      list can never drift apart again.
#    - friendshipStatus is computed from the VIEWER's side —
#      'friends', 'request_sent', 'request_received' or 'none'
#      — including the one-directional friendships row and the
#      settled (rejected/accepted) request rows that must NOT
#      register.
#    - PUT /api/social/profile validates exactly like its
#      unused twin PUT /api/auth/me: every guard is asserted
#      against BOTH routes at once, which is the only thing
#      stopping the two parallel implementations from drifting.
#      The rename's author_name snapshot rewrite, the replaced
#      avatar's disk cleanup and the 30-per-window rate limit
#      are covered here too.
#
#  Everything is driven through the routes; the direct `db`
#  connection is only used to arrange state the API cannot
#  create (friendship rows in one direction, deactivated
#  accounts, scraped posts) and to assert what was persisted.
# -----------------------------------------------------------

import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

from app.api import SUMMARY_LENGTH


# The exact wire shapes the mobile client reads
# (services/api/social.ts fetchUserProfile / updateProfile)
_PROFILE_KEYS = {"id", "username", "displayName", "avatarUrl", "role", "createdAt",
                 "postCount", "friendCount", "friendshipStatus", "blockedByMe"}
_OWN_PROFILE_KEYS = {"id", "username", "email", "displayName", "avatarUrl", "role", "createdAt",
                     "postCount", "friendCount", "studentNumber", "studyGroup", "studyProgram"}
_UPDATE_KEYS = {"id", "username", "email", "displayName", "avatarUrl", "role", "invited",
                "studentNumber", "studyGroup", "studyProgram"}

# A path shaped exactly like one uploads/routes.py hands out —
# the before_request hook in app/__init__.py matches the full
# uuid4().hex + extension, not merely the /api/uploads/ prefix
_AVATAR_A = "/api/uploads/" + "a" * 32 + ".png"
_AVATAR_B = "/api/uploads/" + "b" * 32 + ".jpg"




# -----------------------------------------------------------
# _clean_rate_limits
# -----------------------------------------------------------
#
# The limiter's store is a MODULE-level dict, so it outlives
# the per-test app and is shared with every other test file in
# the process: without this, the global per-IP budget (600 in
# five minutes, and every test client request has the same
# remote_addr) would eventually 429 whichever test happened to
# run 601st. Cleared around each test so the quota assertions
# below measure only their own calls.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limits():
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _seed_post
# -----------------------------------------------------------
#
# One news_posts row for an author, inserted directly because
# the visibility combinations under test (a private post, a
# 'faculty' announcement, a scraped article credited to a user)
# are not all reachable through the write routes.
# -----------------------------------------------------------

def _seed_post(db, author_id, public=True, source="user", post_type="social", content="Sienos irasas"):
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




# -----------------------------------------------------------
# _befriend / _one_way_friendship / _seed_request
# -----------------------------------------------------------
#
# friendships is one row PER DIRECTION and every reader looks
# up only its own direction, so the half-written case is a
# state the routes must handle — hence the separate helper.
# _seed_request plants a friend_requests row in any status,
# which is how the settled-row cases get tested without
# driving the whole accept/reject dance.
# -----------------------------------------------------------

def _befriend(db, one, other):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (one, other))
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (other, one))
    db.commit()


def _one_way_friendship(db, owner, friend):
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
# _profile / _post_total
# -----------------------------------------------------------
#
# The two numbers this module keeps comparing: the profile's
# postCount and the "total" GET /api/social/posts reports for
# the same viewer. They are read through the routes so the
# comparison covers the real visibility rules, not a re-run of
# the SQL.
# -----------------------------------------------------------

def _profile(client, user_id, headers=None):
    return client.get(f"/api/social/profile/{user_id}", headers=headers or {})


def _post_total(client, user_id, headers=None):
    response = client.get(f"/api/social/posts?user_id={user_id}", headers=headers or {})
    assert response.status_code == 200, response.get_json()
    return response.get_json()["total"]




# -----------------------------------------------------------
# _pin_upload_dir / _write_upload
# -----------------------------------------------------------
#
# uploads/routes.py caches its resolved upload directory in a
# MODULE global, so whichever test resolved it first pins it
# for the whole process. Both are needed by the avatar-cleanup
# tests, which want to watch a real file disappear.
# -----------------------------------------------------------

def _pin_upload_dir(monkeypatch):
    import app.uploads.routes as uploads_routes

    monkeypatch.setattr(uploads_routes, "_upload_dir", None)


def _write_upload(app, avatar_path):
    target = pathlib.Path(app.config["UPLOAD_DIR"]) / avatar_path.rsplit("/", 1)[-1]
    target.write_bytes(b"not really a png")
    return target








# -----------------------------------------------------------
# GET /api/social/profile/<user_id> — reads and gates
# -----------------------------------------------------------

def test_a_guest_can_read_a_profile_without_a_token(client, make_user):
    owner = make_user(display_name="Ona Onaityte")

    response = _profile(client, owner["id"])

    assert response.status_code == 200
    body = response.get_json()
    assert body["id"] == owner["id"]
    assert body["username"] == owner["username"]
    assert body["displayName"] == "Ona Onaityte"


@pytest.mark.contract
def test_the_profile_body_carries_exactly_the_documented_keys(client, make_user):
    owner = make_user()

    body = _profile(client, owner["id"]).get_json()

    assert set(body) == _PROFILE_KEYS


def test_the_public_profile_never_exposes_the_email(client, make_user, actor):
    owner = make_user()
    _, headers = actor

    for viewer_headers in (None, headers):
        body = _profile(client, owner["id"], viewer_headers).get_json()
        assert "email" not in body


def test_an_unknown_user_id_is_404(client):
    response = _profile(client, "no-such-user")

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_a_profile_read_reports_the_role_and_created_at(client, make_user, db):
    owner = make_user(role="teacher")

    body = _profile(client, owner["id"]).get_json()

    assert body["role"] == "teacher"
    stored = db.execute("SELECT created_at FROM users WHERE id = ?", (owner["id"],)).fetchone()
    assert body["createdAt"] == stored["created_at"]


def test_a_profile_avatar_comes_back_as_stored(client, make_user, db):
    owner = make_user()
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (_AVATAR_A, owner["id"]))
    db.commit()

    assert _profile(client, owner["id"]).get_json()["avatarUrl"] == _AVATAR_A


def test_a_display_name_is_html_escaped_on_the_way_out(client, make_user, db):
    owner = make_user()
    db.execute("UPDATE users SET display_name = ? WHERE id = ?", ("<script>x</script>", owner["id"]))
    db.commit()

    body = _profile(client, owner["id"]).get_json()

    assert "<script>" not in body["displayName"]
    assert body["displayName"] == "&lt;script&gt;x&lt;/script&gt;"




# -----------------------------------------------------------
# Deactivated accounts drop out of the profile route
# -----------------------------------------------------------

def test_a_deactivated_profile_is_404_for_a_guest(client, make_user, db):
    owner = make_user()
    _deactivate(db, owner["id"])

    response = _profile(client, owner["id"])

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_a_deactivated_profile_is_404_for_an_ordinary_member(client, make_user, db, actor):
    owner = make_user()
    _deactivate(db, owner["id"])
    _, headers = actor

    assert _profile(client, owner["id"], headers).status_code == 404


def test_a_deactivated_profile_is_404_even_for_their_own_friend(client, make_user, db, actor):
    viewer, headers = actor
    owner = make_user()
    _befriend(db, viewer["id"], owner["id"])
    _deactivate(db, owner["id"])

    assert _profile(client, owner["id"], headers).status_code == 404


def test_an_admin_still_reads_a_deactivated_profile(client, make_user, db, admin):
    owner = make_user(display_name="Deaktyvuotas")
    _deactivate(db, owner["id"])
    _, admin_headers = admin

    response = _profile(client, owner["id"], admin_headers)

    assert response.status_code == 200
    assert response.get_json()["displayName"] == "Deaktyvuotas"


def test_a_deactivated_user_cannot_read_their_own_profile(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    _deactivate(db, owner["id"])

    # The session no longer resolves, so the request is a guest's
    assert _profile(client, owner["id"], headers).status_code == 404


def test_a_deactivated_viewer_still_sees_an_active_profile_as_a_guest(client, make_user, auth_headers, db):
    viewer = make_user()
    owner = make_user()
    headers = auth_headers(viewer)
    _befriend(db, viewer["id"], owner["id"])
    _seed_post(db, owner["id"], public=False)
    _deactivate(db, viewer["id"])

    body = _profile(client, owner["id"], headers).get_json()

    assert body["friendshipStatus"] == "none"
    assert body["postCount"] == 0




# -----------------------------------------------------------
# friendshipStatus — always from the viewer's side
# -----------------------------------------------------------

def test_a_bogus_bearer_token_reads_the_profile_as_a_guest(client, make_user, db):
    owner = make_user()
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], public=True)
    _seed_request(db, owner["id"], make_user()["id"])

    body = _profile(client, owner["id"],
                    {"Authorization": "Bearer not-a-real-token"}).get_json()

    assert body["postCount"] == 1
    assert body["friendshipStatus"] == "none"


def test_friendship_status_is_none_for_a_guest(client, make_user):
    owner = make_user()

    assert _profile(client, owner["id"]).get_json()["friendshipStatus"] == "none"


def test_friendship_status_is_none_for_a_guest_even_with_a_pending_request(client, make_user, db):
    sender = make_user()
    owner = make_user()
    _seed_request(db, sender["id"], owner["id"])

    assert _profile(client, owner["id"]).get_json()["friendshipStatus"] == "none"


def test_friendship_status_is_none_on_your_own_profile(client, actor):
    viewer, headers = actor

    assert _profile(client, viewer["id"], headers).get_json()["friendshipStatus"] == "none"


def test_your_own_profile_stays_none_even_with_a_self_friendship_row(client, actor, db):
    viewer, headers = actor
    _one_way_friendship(db, viewer["id"], viewer["id"])

    assert _profile(client, viewer["id"], headers).get_json()["friendshipStatus"] == "none"


def test_friendship_status_is_friends_when_the_viewer_has_the_row(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _befriend(db, viewer["id"], owner["id"])

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "friends"


def test_friendship_status_is_request_sent_when_the_viewer_asked(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _seed_request(db, viewer["id"], owner["id"])

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "request_sent"


def test_friendship_status_is_request_received_when_the_owner_asked(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _seed_request(db, owner["id"], viewer["id"])

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "request_received"


def test_a_rejected_request_leaves_the_status_none(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _seed_request(db, viewer["id"], owner["id"], status="rejected")

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "none"


def test_an_accepted_request_row_alone_does_not_read_as_friends(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _seed_request(db, viewer["id"], owner["id"], status="accepted")

    # The friendships rows are the state of record, not the handshake row
    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "none"


def test_a_pending_request_with_a_third_party_does_not_leak_into_the_status(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    stranger = make_user()
    _seed_request(db, viewer["id"], stranger["id"])
    _seed_request(db, stranger["id"], owner["id"])

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "none"


def test_friends_wins_over_a_leftover_pending_request(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _befriend(db, viewer["id"], owner["id"])
    _seed_request(db, owner["id"], viewer["id"])

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "friends"


def test_a_friendship_row_the_viewer_does_not_own_is_not_friends(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    # Only the owner's direction was written — the viewer has no row,
    # so their side must not claim a friendship
    _one_way_friendship(db, owner["id"], viewer["id"])

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "none"


def test_the_viewers_own_half_of_a_friendship_is_enough(client, actor, make_user, db):
    viewer, headers = actor
    owner = make_user()
    _one_way_friendship(db, viewer["id"], owner["id"])

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "friends"


def test_the_status_follows_the_real_request_and_accept_routes(client, make_user, auth_headers):
    sender = make_user()
    receiver = make_user()
    sender_headers = auth_headers(sender)
    receiver_headers = auth_headers(receiver)

    sent = client.post("/api/social/friends/request", headers=sender_headers,
                       json={"user_id": receiver["id"]})
    assert sent.status_code == 201

    assert _profile(client, receiver["id"], sender_headers).get_json()["friendshipStatus"] == "request_sent"
    assert _profile(client, sender["id"], receiver_headers).get_json()["friendshipStatus"] == "request_received"

    accepted = client.post(f"/api/social/friends/requests/{sent.get_json()['id']}/accept",
                           headers=receiver_headers)
    assert accepted.status_code == 200

    assert _profile(client, receiver["id"], sender_headers).get_json()["friendshipStatus"] == "friends"
    assert _profile(client, sender["id"], receiver_headers).get_json()["friendshipStatus"] == "friends"


def test_the_status_is_none_again_after_an_unfriend(client, make_user, auth_headers, db):
    viewer = make_user()
    owner = make_user()
    headers = auth_headers(viewer)
    _befriend(db, viewer["id"], owner["id"])

    assert client.delete(f"/api/social/friends/{owner['id']}", headers=headers).status_code == 200

    assert _profile(client, owner["id"], headers).get_json()["friendshipStatus"] == "none"


def test_the_status_is_none_again_after_a_rejection(client, make_user, auth_headers, db):
    sender = make_user()
    receiver = make_user()
    sender_headers = auth_headers(sender)
    receiver_headers = auth_headers(receiver)
    request_id = _seed_request(db, sender["id"], receiver["id"])

    assert client.post(f"/api/social/friends/requests/{request_id}/reject",
                       headers=receiver_headers).status_code == 200

    assert _profile(client, receiver["id"], sender_headers).get_json()["friendshipStatus"] == "none"




# -----------------------------------------------------------
# friendCount
# -----------------------------------------------------------

def test_friend_count_is_zero_for_a_fresh_account(client, make_user):
    owner = make_user()

    assert _profile(client, owner["id"]).get_json()["friendCount"] == 0


def test_friend_count_counts_the_owners_own_direction(client, make_user, db):
    owner = make_user()
    for _ in range(3):
        _befriend(db, owner["id"], make_user()["id"])

    assert _profile(client, owner["id"]).get_json()["friendCount"] == 3


def test_friend_count_ignores_rows_that_only_point_at_the_owner(client, make_user, db):
    owner = make_user()
    admirer = make_user()
    _one_way_friendship(db, admirer["id"], owner["id"])

    assert _profile(client, owner["id"]).get_json()["friendCount"] == 0


def test_friend_count_skips_deactivated_friends(client, make_user, db, actor):
    viewer, headers = actor
    owner = make_user()
    gone = make_user()
    _befriend(db, owner["id"], viewer["id"])
    _befriend(db, owner["id"], gone["id"])
    _deactivate(db, gone["id"])

    # A deactivated account drops out of everything social — the
    # friends list already hides it, so the count must agree
    assert _profile(client, owner["id"], headers).get_json()["friendCount"] == 1


def test_the_owners_friend_count_matches_their_own_friends_list(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    gone = make_user()
    for _ in range(2):
        _befriend(db, owner["id"], make_user()["id"])
    _befriend(db, owner["id"], gone["id"])
    _deactivate(db, gone["id"])

    listed = client.get("/api/social/friends", headers=headers).get_json()

    assert _profile(client, owner["id"], headers).get_json()["friendCount"] == listed["total"]
    assert listed["total"] == len(listed["friends"])




# -----------------------------------------------------------
# postCount — and the list it must agree with
# -----------------------------------------------------------

def test_post_count_is_zero_for_an_author_with_nothing(client, make_user):
    owner = make_user()

    assert _profile(client, owner["id"]).get_json()["postCount"] == 0


def test_post_count_counts_only_public_posts_for_a_guest(client, make_user, db):
    owner = make_user()
    _seed_post(db, owner["id"], public=True)
    _seed_post(db, owner["id"], public=False)

    assert _profile(client, owner["id"]).get_json()["postCount"] == 1


def test_post_count_counts_only_public_posts_for_a_stranger(client, make_user, db, actor):
    _, headers = actor
    owner = make_user()
    _seed_post(db, owner["id"], public=True)
    _seed_post(db, owner["id"], public=False)

    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 1


def test_post_count_includes_private_posts_for_the_author_themselves(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    _seed_post(db, owner["id"], public=True)
    _seed_post(db, owner["id"], public=False)

    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 2


def test_post_count_includes_private_posts_for_an_accepted_friend(client, make_user, actor, db):
    viewer, headers = actor
    owner = make_user()
    _befriend(db, viewer["id"], owner["id"])
    _seed_post(db, owner["id"], public=True)
    _seed_post(db, owner["id"], public=False)

    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 2


def test_post_count_hides_private_posts_from_a_one_directional_friend(client, make_user, actor, db):
    viewer, headers = actor
    owner = make_user()
    # Only the owner claims the friendship; the viewer's own row is
    # missing, and that is the direction the check reads
    _one_way_friendship(db, owner["id"], viewer["id"])
    _seed_post(db, owner["id"], public=True)
    _seed_post(db, owner["id"], public=False)

    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 1


def test_post_count_counts_faculty_announcements_alongside_wall_posts(client, make_user, db):
    owner = make_user(role="teacher")
    _seed_post(db, owner["id"], source="user")
    _seed_post(db, owner["id"], source="faculty", post_type="announcement")

    assert _profile(client, owner["id"]).get_json()["postCount"] == 2


@pytest.mark.parametrize("source", ["app", "knf.vu.lt", "vu.lt"])
def test_post_count_ignores_scraped_and_app_sources(client, make_user, db, source):
    owner = make_user()
    _seed_post(db, owner["id"], source=source, post_type="article")

    assert _profile(client, owner["id"]).get_json()["postCount"] == 0


def test_post_count_ignores_other_peoples_posts(client, make_user, db):
    owner = make_user()
    other = make_user()
    _seed_post(db, other["id"])

    assert _profile(client, owner["id"]).get_json()["postCount"] == 0


def test_post_count_matches_the_post_list_total_for_a_guest(client, make_user, db):
    owner = make_user()
    _seed_post(db, owner["id"], public=True)
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], source="faculty", post_type="announcement")
    _seed_post(db, owner["id"], source="knf.vu.lt", post_type="article")

    assert _profile(client, owner["id"]).get_json()["postCount"] == _post_total(client, owner["id"])


def test_post_count_matches_the_post_list_total_for_the_author(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    _seed_post(db, owner["id"], public=True)
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], source="faculty", post_type="announcement", content="Skelbimas")

    body = _profile(client, owner["id"], headers).get_json()

    assert body["postCount"] == _post_total(client, owner["id"], headers) == 3


def test_post_count_matches_the_post_list_total_for_a_friend(client, make_user, actor, db):
    viewer, headers = actor
    owner = make_user()
    _befriend(db, viewer["id"], owner["id"])
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], public=True)

    body = _profile(client, owner["id"], headers).get_json()

    assert body["postCount"] == _post_total(client, owner["id"], headers) == 3


def test_post_count_matches_the_post_list_total_for_a_stranger(client, make_user, actor, db):
    _, headers = actor
    owner = make_user()
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], public=True)

    body = _profile(client, owner["id"], headers).get_json()

    assert body["postCount"] == _post_total(client, owner["id"], headers) == 1


def test_post_count_matches_the_post_list_total_for_an_admin_on_a_deactivated_user(client, make_user, admin, db):
    _, admin_headers = admin
    owner = make_user()
    _seed_post(db, owner["id"], public=True)
    _seed_post(db, owner["id"], public=False)
    _deactivate(db, owner["id"])

    body = _profile(client, owner["id"], admin_headers).get_json()

    # The admin is neither the author nor a friend: public only,
    # exactly like the list they see next to the number
    assert body["postCount"] == _post_total(client, owner["id"], admin_headers) == 1


def test_post_count_drops_when_the_author_deletes_a_post(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    post_id = _seed_post(db, owner["id"])

    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 1
    assert client.delete(f"/api/social/posts/{post_id}", headers=headers).status_code == 200
    assert _profile(client, owner["id"], headers).get_json()["postCount"] == 0




# -----------------------------------------------------------
# GET /api/social/profile — the private twin
# -----------------------------------------------------------

def test_reading_your_own_profile_requires_a_token(client):
    response = client.get("/api/social/profile")

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


@pytest.mark.contract
def test_your_own_profile_carries_exactly_the_documented_keys(client, actor):
    _, headers = actor

    body = client.get("/api/social/profile", headers=headers).get_json()

    assert set(body) == _OWN_PROFILE_KEYS
    assert "friendshipStatus" not in body


def test_your_own_profile_carries_the_email_and_student_card_fields(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    db.execute(
        "UPDATE users SET student_number = ?, study_group = ?, study_program = ? WHERE id = ?",
        ("S12345", "IFF-1", "Informatika", owner["id"]),
    )
    db.commit()

    body = client.get("/api/social/profile", headers=headers).get_json()

    assert body["email"] == owner["email"]
    assert body["studentNumber"] == "S12345"
    assert body["studyGroup"] == "IFF-1"
    assert body["studyProgram"] == "Informatika"


def test_your_own_profile_counts_private_and_faculty_posts(client, make_user, auth_headers, db):
    owner = make_user(role="teacher")
    headers = auth_headers(owner)
    _seed_post(db, owner["id"], public=False)
    _seed_post(db, owner["id"], source="faculty", post_type="announcement")
    _seed_post(db, owner["id"], source="knf.vu.lt", post_type="article")

    body = client.get("/api/social/profile", headers=headers).get_json()

    assert body["postCount"] == 2


def test_your_own_profile_counts_your_friends(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)
    _befriend(db, owner["id"], make_user()["id"])

    assert client.get("/api/social/profile", headers=headers).get_json()["friendCount"] == 1


def test_your_own_profile_created_at_matches_the_stored_row(client, make_user, auth_headers, db):
    owner = make_user()
    headers = auth_headers(owner)

    body = client.get("/api/social/profile", headers=headers).get_json()

    stored = db.execute("SELECT created_at FROM users WHERE id = ?", (owner["id"],)).fetchone()
    assert body["createdAt"] == stored["created_at"]




# -----------------------------------------------------------
# PUT /api/social/profile — the body itself
# -----------------------------------------------------------

def test_editing_a_profile_requires_a_token(client):
    response = client.put("/api/social/profile", json={"displayName": "Kas nors"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_an_empty_object_body_is_400(client, actor):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_json_null_body_is_400(client, actor):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers,
                          data="null", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_body_of_only_unknown_keys_is_400(client, actor):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"nickname": "x"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "No fields to update"




# -----------------------------------------------------------
# PUT /api/social/profile — display name
# -----------------------------------------------------------

def test_the_display_name_is_updated_and_persisted(client, actor, db):
    user, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"displayName": "Jonas Jonaitis"})

    assert response.status_code == 200
    assert response.get_json()["displayName"] == "Jonas Jonaitis"
    stored = db.execute("SELECT display_name FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["display_name"] == "Jonas Jonaitis"


@pytest.mark.parametrize("value", [5, None, True, ["Jonas"], {"a": 1}])
def test_a_non_string_display_name_is_400(client, actor, value):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"displayName": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "display_name must be a string"


@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_a_blank_display_name_is_400_not_a_silent_skip(client, actor, value, db):
    user, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"displayName": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name cannot be empty"
    stored = db.execute("SELECT display_name FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["display_name"] == user["username"].title()


def test_a_display_name_of_exactly_100_characters_is_accepted(client, actor):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"displayName": "a" * 100})

    assert response.status_code == 200
    assert response.get_json()["displayName"] == "a" * 100


def test_a_display_name_of_101_characters_is_400(client, actor):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"displayName": "a" * 101})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Display name must be at most 100 characters"


def test_a_display_name_is_stored_stripped(client, actor, db):
    user, headers = actor

    client.put("/api/social/profile", headers=headers, json={"displayName": "  Ona  "})

    stored = db.execute("SELECT display_name FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["display_name"] == "Ona"


def test_a_hundred_characters_of_padding_still_pass_the_limit(client, actor):
    _, headers = actor

    # The length gate runs on the STRIPPED value, so surrounding
    # whitespace can never push a legal name over the limit
    response = client.put("/api/social/profile", headers=headers,
                          json={"displayName": "   " + "a" * 100 + "   "})

    assert response.status_code == 200


def test_a_snake_case_display_name_is_accepted(client, actor):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"display_name": "Snake"})

    assert response.status_code == 200
    assert response.get_json()["displayName"] == "Snake"


def test_camel_case_wins_when_both_display_name_spellings_are_sent(client, actor):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers,
                          json={"displayName": "Camel", "display_name": "Snake"})

    assert response.get_json()["displayName"] == "Camel"


def test_a_rename_rewrites_the_author_name_snapshot_on_own_posts(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    client.put("/api/social/profile", headers=headers, json={"displayName": "Nauja Pavarde"})

    stored = db.execute("SELECT author_name FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert stored["author_name"] == "Nauja Pavarde"


def test_a_rename_leaves_another_authors_snapshot_alone(client, actor, make_user, db):
    user, headers = actor
    other = make_user()
    other_post = _seed_post(db, other["id"])

    client.put("/api/social/profile", headers=headers, json={"displayName": "Nauja Pavarde"})

    stored = db.execute("SELECT author_name FROM news_posts WHERE id = ?", (other_post,)).fetchone()
    assert stored["author_name"] == "Snapshot"


def test_a_rename_shows_up_on_the_public_profile(client, actor):
    user, headers = actor

    client.put("/api/social/profile", headers=headers, json={"displayName": "Vieso Vardas"})

    assert _profile(client, user["id"]).get_json()["displayName"] == "Vieso Vardas"


def test_an_update_without_a_rename_leaves_the_snapshots_alone(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    client.put("/api/social/profile", headers=headers, json={"studyGroup": "IFF-2"})

    stored = db.execute("SELECT author_name FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert stored["author_name"] == "Snapshot"




# -----------------------------------------------------------
# PUT /api/social/profile — avatar
# -----------------------------------------------------------

def test_an_uploads_path_avatar_is_stored(client, actor, db):
    user, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})

    assert response.status_code == 200
    assert response.get_json()["avatarUrl"] == _AVATAR_A
    stored = db.execute("SELECT avatar_url FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["avatar_url"] == _AVATAR_A


def test_a_snake_case_avatar_key_is_accepted(client, actor):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"avatar_url": _AVATAR_A})

    assert response.status_code == 200
    assert response.get_json()["avatarUrl"] == _AVATAR_A


@pytest.mark.parametrize("cleared", [None, ""])
def test_null_and_blank_clear_the_avatar(client, actor, db, cleared):
    user, headers = actor
    client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})

    response = client.put("/api/social/profile", headers=headers, json={"avatarUrl": cleared})

    assert response.status_code == 200
    assert response.get_json()["avatarUrl"] in (None, "")
    stored = db.execute("SELECT avatar_url FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["avatar_url"] in (None, "")


@pytest.mark.parametrize("value", [
    "https://evil.example/avatar.png",
    "http://knf.vu.lt/avatar.png",
    "//evil.example/avatar.png",
    "/api/uploads/../../../etc/passwd",
    "/api/uploads/" + "a" * 32 + ".png?x=//evil",
    "/uploads/" + "a" * 32 + ".png",
    "/api/uploads/notahash.png",
    42,
])
def test_an_avatar_outside_our_own_uploads_is_refused(client, actor, value, db):
    user, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={"avatarUrl": value})

    assert response.status_code == 400
    stored = db.execute("SELECT avatar_url FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["avatar_url"] is None


def test_replacing_an_avatar_deletes_the_replaced_file(client, actor, db, app, monkeypatch):
    user, headers = actor
    _pin_upload_dir(monkeypatch)
    old_file = _write_upload(app, _AVATAR_A)
    client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})

    response = client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_B})

    assert response.status_code == 200
    assert not old_file.exists()


def test_clearing_an_avatar_deletes_the_replaced_file(client, actor, app, monkeypatch):
    _, headers = actor
    _pin_upload_dir(monkeypatch)
    old_file = _write_upload(app, _AVATAR_A)
    client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})

    client.put("/api/social/profile", headers=headers, json={"avatarUrl": None})

    assert not old_file.exists()


def test_re_sending_the_same_avatar_keeps_the_file(client, actor, app, monkeypatch):
    _, headers = actor
    _pin_upload_dir(monkeypatch)
    same_file = _write_upload(app, _AVATAR_A)
    client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})

    client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})

    assert same_file.exists()


def test_a_stored_absolute_avatar_is_never_handed_to_the_delete_helper(client, actor, db, monkeypatch):
    user, headers = actor
    # A value the routes would refuse today, but older rows carry it
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?",
               ("https://legacy.example/a.png", user["id"]))
    db.commit()
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda path: calls.append(path))

    response = client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})

    assert response.status_code == 200
    assert calls == []


def test_the_routes_own_avatar_belt_refuses_an_absolute_url(app, actor):
    # The app-wide before_request hook normally answers this first,
    # so the route's own guard is only reachable with the hooks out
    # of the way — it is the belt for the day that hook changes
    from app.social.routes import update_profile

    _, headers = actor

    with app.test_request_context("/api/social/profile", method="PUT", headers=headers,
                                  json={"avatarUrl": "https://evil.example/a.png"}):
        body, status = update_profile()

    assert status == 400
    assert body.get_json()["error"] == "avatar_url must be a relative /api/uploads/ path"


def test_a_row_that_vanishes_mid_update_answers_401_not_500(client, actor, db, monkeypatch):
    import app.social.routes as social_routes

    user, headers = actor
    real_now = social_routes.utc_now_iso

    # utc_now_iso is called between the validation and the UPDATE, so
    # this is the seam where the account can disappear under the
    # request (an admin deleting it, in production)
    def _delete_the_user_first():
        db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        db.commit()
        return real_now()

    monkeypatch.setattr(social_routes, "utc_now_iso", _delete_the_user_first)

    response = client.put("/api/social/profile", headers=headers, json={"displayName": "Ona"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_a_failing_upload_delete_does_not_fail_the_update(client, actor, monkeypatch):
    _, headers = actor
    client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})

    def _boom(path):
        raise OSError("disk on fire")

    monkeypatch.setattr("app.uploads.routes.delete_upload", _boom)

    response = client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_B})

    assert response.status_code == 200
    assert response.get_json()["avatarUrl"] == _AVATAR_B


def test_a_missing_uploads_helper_is_a_silent_no_op(client, actor, monkeypatch):
    _, headers = actor
    client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_A})
    import app.uploads.routes as uploads_routes

    # The guarded lazy import: until delete_upload exists there,
    # the cleanup is skipped instead of exploding the route
    monkeypatch.delattr(uploads_routes, "delete_upload")

    response = client.put("/api/social/profile", headers=headers, json={"avatarUrl": _AVATAR_B})

    assert response.status_code == 200



# -----------------------------------------------------------
# PUT /api/social/profile — the student-card fields
# -----------------------------------------------------------

@pytest.mark.parametrize("camel, snake, column", [
    ("studentNumber", "student_number", "student_number"),
    ("studyGroup", "study_group", "study_group"),
    ("studyProgram", "study_program", "study_program"),
])
def test_a_student_field_is_stored_under_both_spellings(client, actor, db, camel, snake, column):
    user, headers = actor

    assert client.put("/api/social/profile", headers=headers,
                      json={camel: "camel"}).status_code == 200
    stored = db.execute(f"SELECT {column} AS v FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["v"] == "camel"

    assert client.put("/api/social/profile", headers=headers,
                      json={snake: "snake"}).status_code == 200
    stored = db.execute(f"SELECT {column} AS v FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["v"] == "snake"


@pytest.mark.parametrize("key", ["studentNumber", "student_number", "studyGroup", "study_group",
                                 "studyProgram", "study_program"])
def test_a_non_string_student_field_is_400_naming_the_key_that_was_sent(client, actor, key):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={key: 12345})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"{key} must be a string"


@pytest.mark.parametrize("key", ["studentNumber", "studyGroup", "studyProgram"])
def test_a_student_field_of_exactly_50_characters_is_accepted(client, actor, key):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={key: "x" * 50})

    assert response.status_code == 200


@pytest.mark.parametrize("key", ["studentNumber", "studyGroup", "studyProgram"])
def test_a_student_field_of_51_characters_is_400(client, actor, key):
    _, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={key: "x" * 51})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"{key} must be at most 50 characters"


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_student_field_becomes_null(client, actor, db, value):
    user, headers = actor
    client.put("/api/social/profile", headers=headers, json={"studentNumber": "S1"})

    response = client.put("/api/social/profile", headers=headers, json={"studentNumber": value})

    assert response.status_code == 200
    assert response.get_json()["studentNumber"] is None
    stored = db.execute("SELECT student_number FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["student_number"] is None


def test_an_explicit_null_student_field_clears_it(client, actor, db):
    user, headers = actor
    client.put("/api/social/profile", headers=headers, json={"studyGroup": "IFF-1"})

    response = client.put("/api/social/profile", headers=headers, json={"studyGroup": None})

    assert response.status_code == 200
    assert response.get_json()["studyGroup"] is None


def test_student_fields_are_stored_stripped(client, actor, db):
    user, headers = actor

    client.put("/api/social/profile", headers=headers, json={"studyProgram": "  Informatika  "})

    stored = db.execute("SELECT study_program FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["study_program"] == "Informatika"


def test_every_editable_field_can_be_sent_at_once(client, actor, db):
    user, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={
        "displayName": "Visi Laukai",
        "avatarUrl": _AVATAR_A,
        "studentNumber": "S99",
        "studyGroup": "IFF-3",
        "studyProgram": "Programu sistemos",
    })

    assert response.status_code == 200
    body = response.get_json()
    assert body["displayName"] == "Visi Laukai"
    assert body["avatarUrl"] == _AVATAR_A
    assert body["studentNumber"] == "S99"
    assert body["studyGroup"] == "IFF-3"
    assert body["studyProgram"] == "Programu sistemos"


def test_a_partial_update_leaves_the_untouched_fields_alone(client, actor):
    _, headers = actor
    client.put("/api/social/profile", headers=headers,
               json={"studentNumber": "S1", "studyGroup": "IFF-1"})

    response = client.put("/api/social/profile", headers=headers, json={"studyGroup": "IFF-2"})

    body = response.get_json()
    assert body["studentNumber"] == "S1"
    assert body["studyGroup"] == "IFF-2"




# -----------------------------------------------------------
# PUT /api/social/profile — the answer and what it persists
# -----------------------------------------------------------

@pytest.mark.contract
def test_the_update_answer_carries_exactly_the_documented_keys(client, actor):
    _, headers = actor

    body = client.put("/api/social/profile", headers=headers, json={"displayName": "Ona"}).get_json()

    assert set(body) == _UPDATE_KEYS
    # Deliberately absent: the app MERGES this into its cached user
    assert "createdAt" not in body
    assert "active" not in body


def test_the_update_answer_carries_the_invited_flag(client, make_user, auth_headers, db):
    user = make_user()
    headers = auth_headers(user)

    body = client.put("/api/social/profile", headers=headers, json={"displayName": "Ona"}).get_json()

    assert body["invited"] is True
    db.execute("UPDATE users SET invited = 0 WHERE id = ?", (user["id"],))
    db.commit()
    body = client.put("/api/social/profile", headers=headers, json={"displayName": "Ona"}).get_json()
    assert body["invited"] is False


def test_updated_at_is_stamped_in_the_house_t_form(client, actor, db):
    user, headers = actor

    client.put("/api/social/profile", headers=headers, json={"displayName": "Ona"})

    stored = db.execute("SELECT updated_at FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert "T" in stored["updated_at"], "the space-form column DEFAULT must never fire here"
    parsed = datetime.fromisoformat(stored["updated_at"])
    assert parsed.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 120


def test_a_rejected_update_stamps_nothing(client, actor, db):
    user, headers = actor
    before = db.execute("SELECT updated_at FROM users WHERE id = ?", (user["id"],)).fetchone()["updated_at"]

    client.put("/api/social/profile", headers=headers, json={"displayName": ""})

    after = db.execute("SELECT updated_at FROM users WHERE id = ?", (user["id"],)).fetchone()["updated_at"]
    assert after == before


def test_the_role_cannot_be_escalated_through_the_profile_editor(client, actor, db):
    user, headers = actor

    response = client.put("/api/social/profile", headers=headers,
                          json={"displayName": "Ona", "role": "admin", "active": 0, "invited": 1})

    assert response.status_code == 200
    assert response.get_json()["role"] == "student"
    stored = db.execute("SELECT role, active FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert stored["role"] == "student"
    assert stored["active"] == 1


def test_the_username_and_email_cannot_be_changed_here(client, actor, db):
    user, headers = actor

    response = client.put("/api/social/profile", headers=headers, json={
        "displayName": "Ona", "username": "kitas", "email": "kitas@example.com",
    })

    body = response.get_json()
    assert body["username"] == user["username"]
    assert body["email"] == user["email"]




# -----------------------------------------------------------
# The two parallel profile editors must not drift
# -----------------------------------------------------------
#
# PUT /api/social/profile (what the app calls) and
# PUT /api/auth/me (the unused twin) are two implementations of
# the same validation. Both routes get every body below and
# must answer identically — the whole point of the banner's
# "change both".
# -----------------------------------------------------------

_TWIN_ROUTES = ["/api/social/profile", "/api/auth/me"]

_TWIN_BAD_BODIES = [
    {},
    {"nothing": "usable"},
    {"displayName": 5},
    {"displayName": "   "},
    {"displayName": "a" * 101},
    {"display_name": None},
    {"studentNumber": 7},
    {"student_number": 7},
    {"studyGroup": ["IFF"]},
    {"studyProgram": {"name": "x"}},
    {"studentNumber": "x" * 51},
    {"studyGroup": "x" * 51},
    {"studyProgram": "x" * 51},
    {"avatarUrl": "https://evil.example/a.png"},
    {"avatar_url": "/api/uploads/../../etc/passwd"},
]


@pytest.mark.parametrize("body", _TWIN_BAD_BODIES, ids=lambda b: "-".join(sorted(b)) or "empty")
def test_both_profile_editors_reject_the_same_bodies_the_same_way(client, actor, body):
    _, headers = actor

    answers = [client.put(route, headers=headers, json=body) for route in _TWIN_ROUTES]

    assert [a.status_code for a in answers] == [400, 400], [a.get_json() for a in answers]
    assert answers[0].get_json() == answers[1].get_json()


def test_both_profile_editors_answer_the_same_success_shape(client, actor):
    _, headers = actor
    body = {"displayName": "Dvyniai", "studentNumber": "S1",
            "studyGroup": "IFF-1", "studyProgram": "Informatika"}

    social = client.put("/api/social/profile", headers=headers, json=body)
    auth = client.put("/api/auth/me", headers=headers, json=body)

    assert social.status_code == auth.status_code == 200
    assert social.get_json() == auth.get_json()


def test_both_profile_editors_rewrite_the_author_name_snapshot(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    client.put("/api/auth/me", headers=headers, json={"displayName": "Per Auth"})
    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["author_name"] == "Per Auth"

    client.put("/api/social/profile", headers=headers, json={"displayName": "Per Social"})
    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["author_name"] == "Per Social"




# -----------------------------------------------------------
# PUT /api/social/profile — the rate limit
# -----------------------------------------------------------

def test_the_thirty_first_profile_edit_in_a_window_is_rate_limited(client, actor):
    _, headers = actor

    for attempt in range(30):
        response = client.put("/api/social/profile", headers=headers,
                              json={"displayName": f"Vardas {attempt}"})
        assert response.status_code == 200, f"edit {attempt} was refused: {response.get_json()}"

    response = client.put("/api/social/profile", headers=headers, json={"displayName": "Per daug"})

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


def test_a_rejected_edit_still_spends_its_attempt(client, actor):
    _, headers = actor

    for _ in range(30):
        assert client.put("/api/social/profile", headers=headers,
                          json={"displayName": ""}).status_code == 400

    # The decorator runs before the handler, so a bad body is not
    # a free retry — the budget is gone
    assert client.put("/api/social/profile", headers=headers,
                      json={"displayName": "Ona"}).status_code == 429


def test_the_profile_edit_budget_frees_up_after_the_window(client, actor):
    _, headers = actor
    start = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

    with time_machine.travel(start, tick=False) as traveller:
        for attempt in range(30):
            assert client.put("/api/social/profile", headers=headers,
                              json={"displayName": f"Vardas {attempt}"}).status_code == 200
        assert client.put("/api/social/profile", headers=headers,
                          json={"displayName": "Per daug"}).status_code == 429

        traveller.shift(timedelta(seconds=301))

        assert client.put("/api/social/profile", headers=headers,
                          json={"displayName": "Vel galima"}).status_code == 200


def test_one_users_profile_budget_does_not_touch_anothers(client, actor, make_user, auth_headers):
    _, headers = actor
    other = make_user()
    other_headers = auth_headers(other)

    for attempt in range(30):
        assert client.put("/api/social/profile", headers=headers,
                          json={"displayName": f"Vardas {attempt}"}).status_code == 200
    assert client.put("/api/social/profile", headers=headers,
                      json={"displayName": "Per daug"}).status_code == 429

    # The key is "profile:<user id>", not the shared IP
    assert client.put("/api/social/profile", headers=other_headers,
                      json={"displayName": "Kitas"}).status_code == 200
