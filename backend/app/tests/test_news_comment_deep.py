# -----------------------------------------------------------
#  [*] Tests — news comments, the exhaustive pass
#      (app/news/routes.py: get_comments, add_comment,
#       delete_comment)
#
#  test_news_comments.py already walks these three routes'
#  happy paths and their headline guards. This file is the
#  gap-closing pass over what is LEFT of them — the arms of
#  every branch that file does not reach:
#
#    - the OPTIONAL caller of get_comments going wrong in the
#      three ways a real client manages it: an unknown token,
#      an expired session and a deactivated account. All three
#      make the reader a GUEST, so a private thread they used
#      to read answers 404 without any 401 in sight.
#    - _can_view_post's remaining corners: a private WALL post
#      whose author_id is NULL (the friendship probe runs
#      against NULL and matches nobody), a private 'app' /
#      'vu.lt' row for staff vs a student, and a student who
#      AUTHORED a private faculty row outranking their own role
#    - parse_pagination's edges as seen through this route:
#      per_page 1, the last allowed page (10 000), an empty
#      ?page=, a float per_page, "+1" and " 1 ", a repeated
#      parameter, and the offset window walking a thread in
#      page-sized steps
#    - _to_utc_iso's two stamp repairs applying to ONE value
#      (the legacy space separator AND the '+' a query string
#      turned into a space), microseconds, and an empty
#      display_name that COALESCE must NOT read as 'Deleted
#      user'
#    - add_comment's body cap where it actually bites: NUL
#      bytes stripped BEFORE the cap is counted, 2 000
#      multi-byte characters (the cap counts characters, not
#      bytes), U+00A0 blank vs U+200B non-blank, a duplicate
#      "text" key, extra fields ignored, a wrong content type,
#      and a body over the server's own 6 MB ceiling
#    - who may DELETE: the full ownership x role matrix, the
#      order of the three checks (post gate, then the comment,
#      then permission — so a stranger probing a missing
#      comment gets 404 and never 403), and the reply's own
#      {status, comments} shape
#    - the two write budgets, driven by planting a spent window
#      instead of hammering 60 requests: news_comment and
#      news_comment_delete are SEPARATE scopes, and the read
#      route has no budget at all
# -----------------------------------------------------------

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

MAX_COMMENT_LENGTH = 2000
COMMENT_BUDGET = 60
DELETE_BUDGET = 60
MAX_PAGE = 10_000
SERVER_BODY_CEILING = 6 * 1024 * 1024




# -----------------------------------------------------------
# _make_post
# -----------------------------------------------------------
#
# One news_posts row planted through the `db` fixture, so a
# test can own a post of any source / visibility / authorship
# — including the two create_post can never mint: a scraped
# row and a row with NO author at all.
# -----------------------------------------------------------

def _make_post(db, author_id=None, source="user", is_public=1, post_type="social"):
    post_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        """INSERT INTO news_posts
           (id, title, content, summary, author_id, author_name, source,
            post_type, is_public, published_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, "Antraštė", "Turinys", "Turinys", author_id, "Autorius",
         source, post_type, is_public, now, now, now),
    )
    db.commit()
    return post_id




# -----------------------------------------------------------
# _seed_comment
# -----------------------------------------------------------
#
# A comment row written behind the route's back — the only way
# to plant a created_at shape add_comment cannot produce, or an
# author the users table no longer has. The `db` fixture's
# connection has no foreign_keys pragma, so the orphan sticks.
#
# Seeding never moves comments_count; the tests that care set
# it themselves.
# -----------------------------------------------------------

def _seed_comment(db, post_id, user_id, text="Komentaras", created_at=None):
    comment_id = str(uuid.uuid4())

    db.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (comment_id, post_id, user_id, text, created_at or datetime.now(timezone.utc).isoformat()),
    )
    db.commit()
    return comment_id




# -----------------------------------------------------------
# _sync_counter
# -----------------------------------------------------------
#
# Brings comments_count in line with the rows a test seeded
# directly, for the delete tests whose point is the counter the
# REPLY carries — a seeded thread starts at 0 and would make
# every count assertion meaningless.
# -----------------------------------------------------------

def _sync_counter(db, post_id):
    db.execute(
        "UPDATE news_posts SET comments_count = (SELECT COUNT(*) FROM news_comments WHERE post_id = ?)"
        " WHERE id = ?",
        (post_id, post_id),
    )
    db.commit()




# -----------------------------------------------------------
# _counter
# -----------------------------------------------------------

def _counter(db, post_id):
    return db.execute(
        "SELECT comments_count FROM news_posts WHERE id = ?", (post_id,)
    ).fetchone()["comments_count"]




# -----------------------------------------------------------
# _befriend
# -----------------------------------------------------------
#
# friendships is written in BOTH directions on accept, so the
# helper writes both — _can_view_post reads one direction only
# and a half-written pair would prove nothing.
# -----------------------------------------------------------

def _befriend(db, one_id, other_id):
    db.execute("INSERT OR IGNORE INTO friendships (user_id, friend_id) VALUES (?, ?)", (one_id, other_id))
    db.execute("INSERT OR IGNORE INTO friendships (user_id, friend_id) VALUES (?, ?)", (other_id, one_id))
    db.commit()




# -----------------------------------------------------------
# _post_raw
# -----------------------------------------------------------
#
# A POST whose body the STDLIB json serialises (or that the
# caller hands over byte for byte). Flask's test client dumps a
# json= kwarg through app.json — which HERE is the escaping
# provider — so a payload with "<" or a quote would leave the
# harness already escaped and no test could tell escape-on-
# input from escape-on-output. Everything that cares about the
# exact bytes on the wire goes through this (TESTPLAN rule 10).
# -----------------------------------------------------------

def _post_raw(client, url, payload, headers=None, content_type="application/json"):
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    return client.post(url, data=body, content_type=content_type, headers=headers or {})




# -----------------------------------------------------------
# spend_budget
# -----------------------------------------------------------
#
#   spend_budget("news_comment", user_id, 60)
#
# Plants a spent rate-limit window for one scope+caller
# directly in auth's module-level store, so the 429 arm is
# reached in ONE request instead of sixty — and, at 59, so is
# the "last call still allowed" boundary next to it. The store
# outlives the app fixture (it is module state, not app state),
# so every key this fixture touches is removed again on
# teardown; the stamps are time.monotonic()-based exactly as
# _check_rate_limit records them.
# -----------------------------------------------------------

@pytest.fixture
def spend_budget():
    from app.auth.routes import _rate_limit_lock, _rate_limit_store

    planted = []

    def _spend(scope, user_id, attempts):
        key = f"{scope}:{user_id}"
        with _rate_limit_lock:
            _rate_limit_store[key] = [time.monotonic()] * attempts
        planted.append(key)
        return key

    yield _spend

    with _rate_limit_lock:
        for key in planted:
            _rate_limit_store.pop(key, None)




# -----------------------------------------------------------
# Reading a thread — when the OPTIONAL caller stops resolving
# -----------------------------------------------------------


def test_an_unknown_bearer_token_reads_a_public_thread_all_the_same(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"])

    response = client.get(f"/api/news/{post_id}/comments",
                          headers={"Authorization": f"Bearer {uuid.uuid4()}"})

    # get_current_user() is optional here: an unresolvable token
    # is a guest, never a 401
    assert response.status_code == 200
    assert response.get_json()["total"] == 1


def test_an_unknown_bearer_token_cannot_open_a_private_thread(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"], is_public=0)
    _seed_comment(db, post_id, author["id"])

    response = client.get(f"/api/news/{post_id}/comments",
                          headers={"Authorization": f"Bearer {uuid.uuid4()}"})

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer    ", "Basic abc", "Token abc"])
def test_a_malformed_authorization_header_reads_as_a_guest(client, db, make_user, header):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"], is_public=0)

    response = client.get(f"/api/news/{post_id}/comments", headers={"Authorization": header})

    assert response.status_code == 404


def test_a_deactivated_member_loses_the_thread_they_could_read(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"], is_public=0)
    assert client.get(f"/api/news/{post_id}/comments", headers=headers).status_code == 200

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert client.get(f"/api/news/{post_id}/comments", headers=headers).status_code == 404


def test_a_deactivated_member_cannot_comment_at_all(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    # The write route is behind require_auth, so the same lost
    # session is a 401 here and a 404 on the read above
    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_an_expired_session_reads_the_thread_as_a_guest(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"], is_public=0)
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?",
               ("2020-01-01T00:00:00+00:00", user["id"]))
    db.commit()

    response = client.get(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 404


def test_an_expired_session_cannot_delete_a_comment(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])
    db.execute("UPDATE sessions SET expires_at = ? WHERE user_id = ?",
               ("2020-01-01T00:00:00+00:00", user["id"]))
    db.commit()

    response = client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=headers)

    assert response.status_code == 401
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 1




# -----------------------------------------------------------
# Reading a thread — the last corners of _can_view_post
# -----------------------------------------------------------


def test_a_private_wall_post_without_an_author_is_closed_to_every_member(client, db, actor):
    _, headers = actor
    # author_id NULL sends the gate past the "is it mine" branch
    # into the friendship probe, which then runs against NULL
    orphaned = _make_post(db, author_id=None, source="user", is_public=0)

    assert client.get(f"/api/news/{orphaned}/comments", headers=headers).status_code == 404


def test_a_private_wall_post_without_an_author_is_closed_to_a_guest(client, db):
    orphaned = _make_post(db, author_id=None, source="user", is_public=0)

    assert client.get(f"/api/news/{orphaned}/comments").status_code == 404


def test_an_admin_still_opens_an_authorless_private_wall_post(client, db, admin):
    _, admin_headers = admin
    orphaned = _make_post(db, author_id=None, source="user", is_public=0)
    _seed_comment(db, orphaned, str(uuid.uuid4()))

    response = client.get(f"/api/news/{orphaned}/comments", headers=admin_headers)

    assert response.status_code == 200
    assert response.get_json()["total"] == 1


def test_a_friendship_with_someone_else_does_not_open_an_authorless_wall_post(client, db, actor, make_user):
    user, headers = actor
    neighbour = make_user()
    _befriend(db, user["id"], neighbour["id"])
    orphaned = _make_post(db, author_id=None, source="user", is_public=0)

    assert client.get(f"/api/news/{orphaned}/comments", headers=headers).status_code == 404


@pytest.mark.parametrize("source", ["app", "vu.lt", "knf.vu.lt", "faculty"])
@pytest.mark.parametrize("role", ["teacher", "curator"])
def test_staff_open_every_private_non_wall_thread(client, db, make_user, auth_headers, source, role):
    staff = make_user(role=role)
    post_id = _make_post(db, author_id=None, source=source, is_public=0)
    _seed_comment(db, post_id, staff["id"])

    response = client.get(f"/api/news/{post_id}/comments", headers=auth_headers(staff))

    assert response.status_code == 200
    assert response.get_json()["total"] == 1


@pytest.mark.parametrize("source", ["app", "vu.lt", "knf.vu.lt", "faculty"])
def test_a_student_opens_no_private_non_wall_thread(client, db, actor, source):
    _, headers = actor
    post_id = _make_post(db, author_id=None, source=source, is_public=0)

    assert client.get(f"/api/news/{post_id}/comments", headers=headers).status_code == 404


def test_authorship_outranks_a_role_on_a_private_faculty_thread(client, db, actor):
    user, headers = actor
    # A student's id on a 'faculty' row: the author branch fires
    # before the STAFF_ROLES one, so the role never matters
    post_id = _make_post(db, author_id=user["id"], source="faculty", is_public=0)
    _seed_comment(db, post_id, user["id"])

    response = client.get(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["total"] == 1


def test_a_public_wall_post_of_a_stranger_is_readable_by_a_guest(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=1)
    _seed_comment(db, post_id, author["id"])

    # is_public short-circuits the whole gate — no friendship, no
    # role, no caller at all
    assert client.get(f"/api/news/{post_id}/comments").status_code == 200




# -----------------------------------------------------------
# Reading a thread — the paging window at its boundaries
# -----------------------------------------------------------


def test_the_smallest_page_size_returns_exactly_one_comment(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    for _ in range(3):
        _seed_comment(db, post_id, author["id"])

    body = client.get(f"/api/news/{post_id}/comments?per_page=1").get_json()

    assert len(body["comments"]) == 1
    assert body["perPage"] == 1
    assert body["total"] == 3


def test_the_last_allowed_page_is_accepted_and_simply_empty(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"])

    response = client.get(f"/api/news/{post_id}/comments?page={MAX_PAGE}")

    assert response.status_code == 200
    assert response.get_json()["comments"] == []
    assert response.get_json()["page"] == MAX_PAGE
    assert response.get_json()["total"] == 1


@pytest.mark.parametrize("query", ["page=", "per_page=", "page=1.0", "per_page=2.0",
                                   "page=%20", "per_page=1e2", "page=0x2", "per_page=%2B%2B1"])
def test_a_page_parameter_that_is_not_an_integer_is_refused(client, db, make_user, query):
    post_id = _make_post(db, author_id=make_user()["id"])

    response = client.get(f"/api/news/{post_id}/comments?{query}")

    assert response.status_code == 400
    assert "error" in response.get_json()


@pytest.mark.parametrize("query, expected_page", [("page=%2B2", 2), ("page=%201%20", 1), ("page=007", 7)])
def test_the_page_number_is_read_the_way_int_reads_it(client, db, make_user, query, expected_page):
    post_id = _make_post(db, author_id=make_user()["id"])

    body = client.get(f"/api/news/{post_id}/comments?{query}").get_json()

    assert body["page"] == expected_page


def test_a_repeated_page_parameter_takes_the_first_value(client, db, make_user):
    post_id = _make_post(db, author_id=make_user()["id"])

    body = client.get(f"/api/news/{post_id}/comments?page=1&page=9").get_json()

    assert body["page"] == 1


def test_unknown_query_parameters_are_ignored(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"])

    body = client.get(f"/api/news/{post_id}/comments?source=user&limit=1&before=x&q=%3B").get_json()

    assert body["page"] == 1
    assert body["perPage"] == 20
    assert body["total"] == 1


def test_the_offset_walks_the_thread_in_page_sized_steps(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    base = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    for i in range(5):
        _seed_comment(db, post_id, author["id"], text=f"nr {i}",
                      created_at=(base + timedelta(minutes=i)).isoformat())

    second = client.get(f"/api/news/{post_id}/comments?page=2&per_page=2").get_json()

    # Newest first: nr 4, nr 3 | nr 2, nr 1 | nr 0
    assert [c["text"] for c in second["comments"]] == ["nr 2", "nr 1"]
    assert second["total"] == 5


def test_the_total_ignores_the_page_window(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    for i in range(5):
        _seed_comment(db, post_id, author["id"], text=f"nr {i}")

    totals = [client.get(f"/api/news/{post_id}/comments?page={p}&per_page=2").get_json()["total"]
              for p in (1, 2, 3, 4)]

    assert totals == [5, 5, 5, 5]


def test_an_empty_thread_reports_a_zero_total_and_echoes_the_page(client, db, make_user):
    post_id = _make_post(db, author_id=make_user()["id"])

    body = client.get(f"/api/news/{post_id}/comments?page=3&per_page=7").get_json()

    assert body == {"comments": [], "total": 0, "page": 3, "perPage": 7}




# -----------------------------------------------------------
# Reading a thread — the row shape at its edges
# -----------------------------------------------------------


def test_both_stamp_repairs_can_apply_to_one_value(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    # Legacy space separator AND an offset whose '+' a query
    # string turned into a space — the banner says both repairs
    # may land on one value, and here they do
    _seed_comment(db, post_id, author["id"], created_at="2026-08-29 12:00:00 03:00")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "2026-08-29T09:00:00+00:00"


def test_an_offset_that_arrived_as_a_space_is_still_an_offset(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="2026-08-29T12:00:00 03:00")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "2026-08-29T09:00:00+00:00"


def test_microseconds_survive_the_stamp_repair(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="2026-08-29T09:30:00.123456+00:00")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "2026-08-29T09:30:00.123456+00:00"


def test_a_negative_offset_stamp_is_converted_to_utc(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="2026-08-29T06:00:00-03:00")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "2026-08-29T09:00:00+00:00"


def test_a_blank_stamp_leaves_the_comment_without_a_time(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="   ")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    # Nothing parseable and nothing worth handing back either —
    # the field goes out null rather than as whitespace
    assert body["comments"][0]["time"] is None


def test_an_unparseable_stamp_is_handed_back_verbatim(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="vakar vakare")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    # Dropping it would read as "no timestamp" on the client, so
    # the original string survives instead
    assert body["comments"][0]["time"] == "vakar vakare"


def test_a_numeric_stamp_is_handed_back_verbatim(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    # TEXT affinity turns the epoch integer into a string on the
    # way in; ISO parsing then fails on it like any other garbage
    _seed_comment(db, post_id, author["id"], created_at=1700000000)

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "1700000000"


def test_an_empty_display_name_is_not_mistaken_for_a_deleted_user(client, db, make_user):
    author = make_user()
    db.execute("UPDATE users SET display_name = '' WHERE id = ?", (author["id"],))
    db.commit()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"])

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    # COALESCE replaces NULL, never a blank string — a live user
    # with no name is not a deleted one
    assert body["comments"][0]["userName"] == ""


def test_every_row_carries_its_own_author(client, db, make_user):
    author = make_user(display_name="Pirmas")
    other = make_user(display_name="Antras")
    post_id = _make_post(db, author_id=author["id"])
    base = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    _seed_comment(db, post_id, author["id"], text="a", created_at=base.isoformat())
    _seed_comment(db, post_id, other["id"], text="b",
                  created_at=(base + timedelta(minutes=1)).isoformat())

    rows = client.get(f"/api/news/{post_id}/comments").get_json()["comments"]

    assert [(r["userName"], r["userId"]) for r in rows] == [
        ("Antras", other["id"]), ("Pirmas", author["id"]),
    ]


def test_quotes_in_a_stored_comment_go_out_as_entities(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], text="Jis sakė \"labas\" & 'iki'")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    # The provider escapes with quote=True, which is why the
    # mobile client decodes whole responses
    assert body["comments"][0]["text"] == "Jis sakė &quot;labas&quot; &amp; &#x27;iki&#x27;"


def test_only_the_thread_of_the_post_in_the_path_is_served(client, db, make_user):
    author = make_user()
    mine = _make_post(db, author_id=author["id"])
    other = _make_post(db, author_id=author["id"])
    _seed_comment(db, mine, author["id"], text="mano")
    for _ in range(3):
        _seed_comment(db, other, author["id"], text="svetimas")

    body = client.get(f"/api/news/{mine}/comments").get_json()

    assert body["total"] == 1
    assert [c["text"] for c in body["comments"]] == ["mano"]




# -----------------------------------------------------------
# Adding a comment — the body guard where it actually bites
# -----------------------------------------------------------


def test_an_empty_json_object_is_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_a_comment_of_nothing_but_null_bytes_is_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_raw(client, f"/api/news/{post_id}/comments", {"text": "\x00\x00"}, headers)

    # The NUL strip runs in before_request, so what reaches the
    # blank guard is an empty string
    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 0


def test_null_bytes_do_not_count_towards_the_length_cap(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    padded = "a" * MAX_COMMENT_LENGTH + "\x00" * 50

    response = _post_raw(client, f"/api/news/{post_id}/comments", {"text": padded}, headers)

    assert response.status_code == 201
    assert len(db.execute("SELECT text FROM news_comments WHERE id = ?",
                          (response.get_json()["id"],)).fetchone()["text"]) == MAX_COMMENT_LENGTH


def test_the_cap_counts_characters_and_not_bytes(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_raw(client, f"/api/news/{post_id}/comments",
                         {"text": "ė" * MAX_COMMENT_LENGTH}, headers)

    # 2 000 characters, 4 000 bytes — the cap is on len(), so this
    # one fits
    assert response.status_code == 201


def test_one_multibyte_character_over_the_cap_is_still_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_raw(client, f"/api/news/{post_id}/comments",
                         {"text": "ė" * (MAX_COMMENT_LENGTH + 1)}, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Comment must be at most {MAX_COMMENT_LENGTH} characters"


@pytest.mark.parametrize("blank", [" ", " ", "\r\n", "\x0b\x0c"])
def test_a_unicode_blank_is_still_a_blank_comment(client, db, actor, blank):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_raw(client, f"/api/news/{post_id}/comments", {"text": blank}, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_a_zero_width_space_is_a_real_comment(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_raw(client, f"/api/news/{post_id}/comments", {"text": "​"}, headers)

    # U+200B is not whitespace to str.strip(), so it survives the
    # blank guard — a "blank-looking" comment the moderator sees
    assert response.status_code == 201
    assert db.execute("SELECT text FROM news_comments WHERE id = ?",
                      (response.get_json()["id"],)).fetchone()["text"] == "​"


def test_a_comment_that_is_only_the_digit_zero_is_accepted(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "0"}, headers=headers)

    # "0" is falsy nowhere here: the guard tests the STRIPPED
    # string, not the value
    assert response.status_code == 201
    assert response.get_json()["text"] == "0"


def test_the_length_cap_is_checked_before_the_post_is_looked_up(client, actor):
    _, headers = actor

    response = client.post(f"/api/news/{uuid.uuid4()}/comments",
                           json={"text": "a" * (MAX_COMMENT_LENGTH + 1)}, headers=headers)

    # 400, not 404 — the body is judged before any post id is
    # touched, so an over-long comment never probes existence
    assert response.status_code == 400
    assert response.get_json()["error"] == f"Comment must be at most {MAX_COMMENT_LENGTH} characters"


def test_a_body_over_the_servers_own_ceiling_is_a_413(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    oversized = b'{"text": "' + b"a" * (SERVER_BODY_CEILING + 1024) + b'"}'

    response = _post_raw(client, f"/api/news/{post_id}/comments", oversized, headers)

    # Werkzeug refuses the body before any view runs — a 413 with
    # the house code, never a 500
    assert response.status_code == 413
    assert response.get_json()["code"] == "file_too_large"
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 0


def test_a_json_body_sent_as_plain_text_is_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_raw(client, f"/api/news/{post_id}/comments", {"text": "Labas"},
                         headers, content_type="text/plain")

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_a_charset_annotated_json_content_type_is_accepted(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_raw(client, f"/api/news/{post_id}/comments", {"text": "Labas"},
                         headers, content_type="application/json; charset=utf-8")

    assert response.status_code == 201


def test_the_text_key_is_case_sensitive(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={"Text": "Labas"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_a_repeated_text_key_keeps_the_last_one(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_raw(client, f"/api/news/{post_id}/comments",
                         b'{"text": "pirmas", "text": "antras"}', headers)

    assert response.status_code == 201
    assert response.get_json()["text"] == "antras"


def test_extra_body_fields_are_ignored(client, db, actor):
    user, headers = actor
    other = str(uuid.uuid4())
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", headers=headers, json={
        "text": "Labas", "id": "pasirinktas", "userId": other,
        "time": "1999-01-01T00:00:00+00:00", "post_id": str(uuid.uuid4()),
    })

    # Nothing but `text` is read off the body — no client picks
    # its own comment id, author or timestamp
    body = response.get_json()
    assert response.status_code == 201
    assert body["id"] != "pasirinktas"
    assert body["userId"] == user["id"]
    assert body["time"] != "1999-01-01T00:00:00+00:00"
    row = db.execute("SELECT post_id, user_id FROM news_comments WHERE id = ?", (body["id"],)).fetchone()
    assert row["post_id"] == post_id
    assert row["user_id"] == user["id"]




# -----------------------------------------------------------
# Adding a comment — the gate, role by role
# -----------------------------------------------------------


@pytest.mark.parametrize("role", ["teacher", "curator"])
def test_staff_may_comment_on_a_private_faculty_post(client, db, make_user, auth_headers, role):
    staff = make_user(role=role)
    post_id = _make_post(db, author_id=None, source="faculty", is_public=0, post_type="announcement")

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "Peržiūrėta"},
                           headers=auth_headers(staff))

    assert response.status_code == 201
    assert _counter(db, post_id) == 1


def test_a_student_may_not_comment_on_a_private_faculty_post(client, db, actor):
    _, headers = actor
    post_id = _make_post(db, author_id=None, source="faculty", is_public=0, post_type="announcement")

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"
    assert _counter(db, post_id) == 0


def test_an_admin_may_comment_on_any_private_post(client, db, admin, make_user):
    _, admin_headers = admin
    post_id = _make_post(db, author_id=make_user()["id"], source="user", is_public=0)

    assert client.post(f"/api/news/{post_id}/comments", json={"text": "Moderacija"},
                       headers=admin_headers).status_code == 201


def test_a_member_may_not_comment_on_an_authorless_private_wall_post(client, db, actor):
    _, headers = actor
    orphaned = _make_post(db, author_id=None, source="user", is_public=0)

    assert client.post(f"/api/news/{orphaned}/comments", json={"text": "Labas"},
                       headers=headers).status_code == 404


def test_a_teacher_is_no_closer_to_a_private_wall_post_than_a_student(client, db, make_user, auth_headers):
    author = make_user()
    teacher = make_user(role="teacher")
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)

    assert client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"},
                       headers=auth_headers(teacher)).status_code == 404


def test_the_author_may_comment_on_their_own_private_post(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"], is_public=0)

    assert client.post(f"/api/news/{post_id}/comments", json={"text": "Sau"},
                       headers=headers).status_code == 201


@pytest.mark.parametrize("source", ["knf.vu.lt", "vu.lt", "app", "faculty"])
def test_any_member_may_comment_on_a_public_authorless_post(client, db, actor, source):
    _, headers = actor
    post_id = _make_post(db, author_id=None, source=source, is_public=1, post_type="article")

    assert client.post(f"/api/news/{post_id}/comments", json={"text": "Ačiū"},
                       headers=headers).status_code == 201


def test_a_poll_card_takes_comments_like_any_other_post(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"], post_type="poll")

    assert client.post(f"/api/news/{post_id}/comments", json={"text": "Balsavau"},
                       headers=headers).status_code == 201
    assert _counter(db, post_id) == 1




# -----------------------------------------------------------
# Adding a comment — what actually lands
# -----------------------------------------------------------


def test_two_identical_comments_both_land(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    first = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}, headers=headers)
    second = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    # Nothing about this route is idempotent — a double tap is
    # two comments, and the counter says so
    assert first.get_json()["id"] != second.get_json()["id"]
    assert _counter(db, post_id) == 2


def test_the_created_comment_carries_the_authors_avatar(client, db, actor):
    user, headers = actor
    db.execute("UPDATE users SET avatar_url = '/api/uploads/a.jpg' WHERE id = ?", (user["id"],))
    db.commit()
    post_id = _make_post(db, author_id=user["id"])

    body = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"},
                       headers=headers).get_json()

    assert body["userAvatar"] == "/api/uploads/a.jpg"


def test_the_created_comment_reports_no_avatar_when_there_is_none(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    body = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"},
                       headers=headers).get_json()

    assert body["userAvatar"] is None


def test_the_reply_names_the_author_as_they_are_called_right_now(client, db, actor):
    user, headers = actor
    db.execute("UPDATE users SET display_name = 'Senas Vardas' WHERE id = ?", (user["id"],))
    db.commit()
    post_id = _make_post(db, author_id=user["id"])

    created = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"},
                          headers=headers).get_json()
    db.execute("UPDATE users SET display_name = 'Naujas Vardas' WHERE id = ?", (user["id"],))
    db.commit()
    listed = client.get(f"/api/news/{post_id}/comments", headers=headers).get_json()["comments"][0]

    # The 201 is a snapshot of the moment; the list JOINs the live
    # name, so a rename shows up there and only there
    assert created["userName"] == "Senas Vardas"
    assert listed["userName"] == "Naujas Vardas"


def test_the_comment_belongs_to_the_caller_and_not_to_the_post_author(client, db, actor, make_user):
    user, headers = actor
    post_author = make_user()
    post_id = _make_post(db, author_id=post_author["id"])

    body = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"},
                       headers=headers).get_json()

    assert body["userId"] == user["id"]
    assert db.execute("SELECT user_id FROM news_comments WHERE id = ?",
                      (body["id"],)).fetchone()["user_id"] == user["id"]


def test_a_comment_heals_a_counter_that_drifted_on_another_post_alone(client, db, actor):
    user, headers = actor
    mine = _make_post(db, author_id=user["id"])
    other = _make_post(db, author_id=user["id"])
    db.execute("UPDATE news_posts SET comments_count = 99 WHERE id = ?", (other,))
    db.commit()

    client.post(f"/api/news/{mine}/comments", json={"text": "Labas"}, headers=headers)

    # The recompute is scoped to the post in the path — a
    # neighbour's drift is not this route's to heal
    assert _counter(db, mine) == 1
    assert _counter(db, other) == 99




# -----------------------------------------------------------
# Deleting a comment — the order of the three checks
# -----------------------------------------------------------


def test_a_missing_comment_is_a_404_even_for_a_caller_who_could_never_delete_it(client, db, actor, make_user):
    _, stranger_headers = actor
    post_id = _make_post(db, author_id=make_user()["id"])

    response = client.delete(f"/api/news/{post_id}/comments/{uuid.uuid4()}", headers=stranger_headers)

    # The comment lookup precedes the permission check, so the
    # answer is 404 and never the 403 the caller would earn
    assert response.status_code == 404
    assert response.get_json()["error"] == "Comment not found"


def test_a_comment_id_that_is_not_a_uuid_is_simply_not_found(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.delete(f"/api/news/{post_id}/comments/ne-uuid", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Comment not found"


def test_a_post_id_that_is_not_a_uuid_is_simply_not_found(client, actor):
    _, headers = actor

    response = client.delete(f"/api/news/ne-uuid/comments/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_an_admin_cannot_reach_a_comment_through_the_wrong_post(client, db, admin, make_user):
    _, admin_headers = admin
    author = make_user()
    mine = _make_post(db, author_id=author["id"])
    other = _make_post(db, author_id=author["id"])
    comment_id = _seed_comment(db, other, author["id"])
    _sync_counter(db, other)

    response = client.delete(f"/api/news/{mine}/comments/{comment_id}", headers=admin_headers)

    # The AND post_id clause is not a permission check — even an
    # admin has to name the right thread
    assert response.status_code == 404
    assert _counter(db, other) == 1




# -----------------------------------------------------------
# Deleting a comment — the whole ownership x role matrix
# -----------------------------------------------------------


@pytest.mark.parametrize("who, expected", [
    ("comment_author", 200),
    ("post_author", 200),
    ("admin", 200),
    ("teacher", 403),
    ("curator", 403),
    ("stranger", 403),
    ("friend_of_post_author", 403),
])
def test_only_the_two_owners_and_an_admin_may_delete_a_comment(
        client, db, make_user, auth_headers, admin, who, expected):
    post_author = make_user()
    commenter = make_user()
    post_id = _make_post(db, author_id=post_author["id"])
    comment_id = _seed_comment(db, post_id, commenter["id"])
    _sync_counter(db, post_id)

    if who == "comment_author":
        headers = auth_headers(commenter)
    elif who == "post_author":
        headers = auth_headers(post_author)
    elif who == "admin":
        headers = admin[1]
    elif who == "friend_of_post_author":
        friend = make_user()
        _befriend(db, friend["id"], post_author["id"])
        headers = auth_headers(friend)
    else:
        headers = auth_headers(make_user(role="student" if who == "stranger" else who))

    response = client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=headers)

    assert response.status_code == expected
    assert _counter(db, post_id) == (0 if expected == 200 else 1)


def test_the_author_of_both_the_post_and_the_comment_may_delete_it(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])
    _sync_counter(db, post_id)

    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}",
                         headers=headers).status_code == 200


def test_a_curator_deleting_their_own_comment_needs_no_role_at_all(client, db, make_user, auth_headers):
    post_author = make_user()
    curator = make_user(role="curator")
    post_id = _make_post(db, author_id=post_author["id"])
    comment_id = _seed_comment(db, post_id, curator["id"])
    _sync_counter(db, post_id)

    # The ownership arm fires first, so the 403 the same curator
    # earns on someone else's comment never comes up
    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}",
                         headers=auth_headers(curator)).status_code == 200


def test_the_comment_author_may_delete_from_a_scraped_article(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=None, source="knf.vu.lt", post_type="article")
    comment_id = _seed_comment(db, post_id, user["id"])
    _sync_counter(db, post_id)

    # author_id is NULL on a scraped row, so the post-author arm
    # can never fire — only the comment's own author is left
    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}",
                         headers=headers).status_code == 200


def test_a_stranger_may_not_delete_from_a_scraped_article(client, db, actor, make_user, auth_headers):
    _, stranger_headers = actor
    commenter = make_user()
    post_id = _make_post(db, author_id=None, source="knf.vu.lt", post_type="article")
    comment_id = _seed_comment(db, post_id, commenter["id"])
    _sync_counter(db, post_id)

    response = client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=stranger_headers)

    assert response.status_code == 403
    assert _counter(db, post_id) == 1


def test_the_post_author_may_delete_a_comment_on_their_private_post(client, db, actor, make_user):
    user, headers = actor
    friend = make_user()
    _befriend(db, user["id"], friend["id"])
    post_id = _make_post(db, author_id=user["id"], source="user", is_public=0)
    comment_id = _seed_comment(db, post_id, friend["id"])
    _sync_counter(db, post_id)

    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}",
                         headers=headers).status_code == 200


def test_a_friend_may_read_a_private_thread_but_not_prune_it(client, db, actor, make_user, auth_headers):
    user, headers = actor
    friend = make_user()
    _befriend(db, user["id"], friend["id"])
    post_id = _make_post(db, author_id=user["id"], source="user", is_public=0)
    comment_id = _seed_comment(db, post_id, user["id"])
    _sync_counter(db, post_id)
    friend_headers = auth_headers(friend)

    assert client.get(f"/api/news/{post_id}/comments", headers=friend_headers).status_code == 200
    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}",
                         headers=friend_headers).status_code == 403


def test_an_orphaned_comment_can_still_be_pruned_by_the_post_author(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, str(uuid.uuid4()), text="našlaitis")
    _sync_counter(db, post_id)

    response = client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=headers)

    assert response.status_code == 200
    assert client.get(f"/api/news/{post_id}/comments", headers=headers).get_json()["total"] == 0




# -----------------------------------------------------------
# Deleting a comment — the reply and the counter
# -----------------------------------------------------------


def test_the_delete_reply_carries_the_remaining_count(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    ids = [_seed_comment(db, post_id, user["id"], text=f"nr {i}") for i in range(3)]
    _sync_counter(db, post_id)

    body = client.delete(f"/api/news/{post_id}/comments/{ids[0]}", headers=headers).get_json()

    assert body == {"status": "deleted", "comments": 2}
    assert _counter(db, post_id) == 2


def test_the_reply_count_walks_down_to_zero(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    ids = [_seed_comment(db, post_id, user["id"], text=f"nr {i}") for i in range(3)]
    _sync_counter(db, post_id)

    counts = [client.delete(f"/api/news/{post_id}/comments/{cid}",
                            headers=headers).get_json()["comments"] for cid in ids]

    assert counts == [2, 1, 0]


def test_the_reply_count_is_the_counter_the_row_now_holds(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])
    _seed_comment(db, post_id, user["id"])
    # A counter that drifted high heals on the way out, and the
    # reply carries the healed value, not the stale one
    db.execute("UPDATE news_posts SET comments_count = 42 WHERE id = ?", (post_id,))
    db.commit()

    body = client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=headers).get_json()

    assert body["comments"] == 1
    assert _counter(db, post_id) == 1


def test_a_delete_never_touches_another_posts_counter(client, db, actor):
    user, headers = actor
    mine = _make_post(db, author_id=user["id"])
    other = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, mine, user["id"])
    _seed_comment(db, other, user["id"])
    _sync_counter(db, mine)
    _sync_counter(db, other)

    client.delete(f"/api/news/{mine}/comments/{comment_id}", headers=headers)

    assert _counter(db, mine) == 0
    assert _counter(db, other) == 1




# -----------------------------------------------------------
# The two write budgets
# -----------------------------------------------------------
#
# rate_limit keys on "<scope>:<caller id>" and prunes a 5-minute
# window, so a spent window can be PLANTED instead of spent by
# sixty real requests — the 429 arm and the boundary right below
# it then cost one call each. The keys are removed again on
# teardown (see the fixture).
# -----------------------------------------------------------


def test_a_spent_comment_budget_refuses_the_next_comment(client, db, actor, spend_budget):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    spend_budget("news_comment", user["id"], COMMENT_BUDGET)

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "per daug"}, headers=headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 0
    assert _counter(db, post_id) == 0


def test_the_last_comment_inside_the_budget_still_lands(client, db, actor, spend_budget):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    spend_budget("news_comment", user["id"], COMMENT_BUDGET - 1)

    assert client.post(f"/api/news/{post_id}/comments", json={"text": "paskutinis"},
                       headers=headers).status_code == 201


def test_a_spent_comment_budget_does_not_block_a_delete(client, db, actor, spend_budget):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])
    _sync_counter(db, post_id)
    spend_budget("news_comment", user["id"], COMMENT_BUDGET)

    # Separate scopes: news_comment and news_comment_delete keep
    # their own windows, so moderation survives a chatty session
    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}",
                         headers=headers).status_code == 200


def test_a_spent_delete_budget_refuses_the_next_delete(client, db, actor, spend_budget):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])
    _sync_counter(db, post_id)
    spend_budget("news_comment_delete", user["id"], DELETE_BUDGET)

    response = client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments WHERE id = ?",
                      (comment_id,)).fetchone()["c"] == 1
    assert _counter(db, post_id) == 1


def test_the_last_delete_inside_the_budget_still_lands(client, db, actor, spend_budget):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])
    _sync_counter(db, post_id)
    spend_budget("news_comment_delete", user["id"], DELETE_BUDGET - 1)

    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}",
                         headers=headers).status_code == 200


def test_a_spent_delete_budget_gags_only_its_owner(client, db, actor, make_user, auth_headers, spend_budget):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])
    _sync_counter(db, post_id)
    spend_budget("news_comment_delete", user["id"], DELETE_BUDGET)
    moderator = make_user(role="admin")

    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}",
                         headers=auth_headers(moderator)).status_code == 200


def test_reading_a_thread_is_never_rate_limited(client, db, actor, spend_budget):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    _seed_comment(db, post_id, user["id"])
    spend_budget("news_comment", user["id"], COMMENT_BUDGET)
    spend_budget("news_comment_delete", user["id"], DELETE_BUDGET)

    # get_comments carries no budget at all — a member who has
    # written too much may still read
    assert client.get(f"/api/news/{post_id}/comments", headers=headers).status_code == 200




# -----------------------------------------------------------
# The two URLs and the methods they answer to
# -----------------------------------------------------------


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_the_thread_url_answers_only_get_and_post(client, db, actor, method):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = getattr(client, method)(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 405


@pytest.mark.parametrize("method", ["get", "post", "put"])
def test_the_single_comment_url_answers_only_delete(client, db, actor, method):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])

    response = getattr(client, method)(f"/api/news/{post_id}/comments/{comment_id}", headers=headers)

    assert response.status_code == 405
