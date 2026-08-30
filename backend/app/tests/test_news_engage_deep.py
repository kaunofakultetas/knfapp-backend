# -----------------------------------------------------------
#  [*] Tests — news engagement, the exhaustive pass
#
#  Slice: app/news/routes.py `toggle_like` and `share_post` —
#  the two counter routes, taken down every branch a caller
#  can actually reach.
#
#    POST /api/news/<id>/like   — require_auth, then the
#                                 news_like quota, then
#                                 _can_view_post, then an
#                                 INSERT OR IGNORE whose
#                                 rowcount decides like vs
#                                 unlike, then a counter
#                                 RECOMPUTED from news_likes
#                                 and re-read after the commit
#    POST /api/news/<id>/share  — no auth and no route quota,
#                                 but the same _can_view_post
#                                 gate: existence, +1, and the
#                                 fresh count
#
#  What this module adds on top of the two suites that already
#  touch these routes (test_news_posts.py, test_news_feed.py):
#
#    - the whole auth chain in front of the like: no header,
#      every malformed header shape, an unknown token, a
#      session aged past its 30 days (time_machine) and an
#      account deactivated after the token was minted
#    - the quota boundary itself — max-1 lands, max is a 429
#      with Retry-After, and one user's spent budget leaves
#      every other user free
#    - EVERY arm of _can_view_post as the like sees it: public
#      short-circuit, author, admin, the three STAFF_ROLES on a
#      non-wall row, the friendship lookup on a wall row (and
#      its ONE direction), and the author_id-is-NULL scraped
#      row
#    - the ids that must not resolve: unknown, wrong case, a
#      quoted SQL fragment, 5 kB of junk, non-ASCII, a trailing
#      slash — none of them may write, crash or leak
#    - the two documented races, driven deterministically by
#      hooking the route's own connection: a foreign write that
#      lands during the commit (the reply must carry it) and a
#      duplicate insert that lands before ours (rowcount 0, an
#      unlike, never an IntegrityError 500)
#    - share_post's boundaries: a negative counter, the int64
#      ceiling, no route quota, and the visibility gate that
#      keeps a private post's counter (and its existence) out
#      of a stranger's reach
#
#  Assertions stay on booleans, integers and status codes: the
#  app html-escapes every string it serialises, so text
#  assertions here would describe the escaper, not the route.
# -----------------------------------------------------------

import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
import time_machine

from app import _GLOBAL_RATE_LIMIT_MAX
from app.auth import routes as auth_routes
from app.news import routes as news_routes

# The quota toggle_like is decorated with —
# @rate_limit("news_like", max_attempts=300). The decorator
# keeps it in a closure, so it is mirrored here and the
# boundary tests below prove the mirror still matches
LIKE_QUOTA = 300




# -----------------------------------------------------------
# clean_buckets
# -----------------------------------------------------------
#
# The rate-limit store is one module-level dict for the whole
# process, and create_app's global per-IP budget spends from
# it on EVERY test-client request (they all arrive from
# 127.0.0.1). Starting and ending empty keeps a quota test in
# this file from bleeding into the next one — or into another
# agent's file.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_buckets(app):
    auth_routes._rate_limit_store.clear()
    yield
    auth_routes._rate_limit_store.clear()




# -----------------------------------------------------------
# spend_budget
# -----------------------------------------------------------
#
#   spend_budget("news_like", user["id"], LIKE_QUOTA)
#
# Plants N attempt stamps under one rate-limit key, which is
# what N real requests would leave behind — without making N
# requests. The window is 5 minutes of time.monotonic(), so a
# single `now` for all of them sits well inside it.
#
# Used by:
#   - the quota tests in both sections
# -----------------------------------------------------------

@pytest.fixture
def spend_budget():

    def _spend(scope, actor, count):
        key = f"{scope}:{actor}"
        with auth_routes._rate_limit_lock:
            auth_routes._rate_limit_store[key] = [time.monotonic()] * count
        return key

    return _spend




# -----------------------------------------------------------
# _seed_post
# -----------------------------------------------------------
#
# One news_posts row written straight to the database, for the
# states no route can create: a private faculty draft, a
# scraped article (author_id NULL), a drifted or absurd
# counter. Returns the new post id.
#
# The three stamps share one house-shape T-form value, so a
# seeded row ranks and pages exactly like a real one.
# -----------------------------------------------------------

def _seed_post(db, **overrides):
    row = {
        "id": str(uuid.uuid4()),
        "title": "Seeded title",
        "content": "Seeded content",
        "summary": "Seeded content",
        "image_url": None,
        "author_id": None,
        "author_name": "Seed Author",
        "source": "app",
        "source_url": None,
        "post_type": "article",
        "is_public": 1,
        "likes_count": 0,
        "comments_count": 0,
        "shares_count": 0,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    row.update(overrides)

    db.execute(
        """INSERT INTO news_posts
           (id, title, content, summary, image_url, author_id, author_name,
            source, source_url, post_type, is_public, likes_count, comments_count,
            shares_count, published_at, created_at, updated_at)
           VALUES (:id, :title, :content, :summary, :image_url, :author_id, :author_name,
                   :source, :source_url, :post_type, :is_public, :likes_count, :comments_count,
                   :shares_count, :published_at, :published_at, :published_at)""",
        row,
    )
    db.commit()
    return row["id"]




# -----------------------------------------------------------
# _friendship / _befriend
# -----------------------------------------------------------
#
# social/routes.py writes friendships in BOTH directions when
# a request is accepted, and _can_view_post reads exactly ONE
# of them (viewer -> author). _friendship writes a single row
# so the direction can be tested on its own; _befriend writes
# the pair production stores.
# -----------------------------------------------------------

def _friendship(db, user_id, friend_id):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user_id, friend_id))
    db.commit()


def _befriend(db, first_id, second_id):
    _friendship(db, first_id, second_id)
    _friendship(db, second_id, first_id)




# -----------------------------------------------------------
# _counters / _like_rows
# -----------------------------------------------------------
#
# The three denormalised counters on a post row, and the
# news_likes rows behind the first of them — the like toggle's
# whole contract is that these two never disagree.
# -----------------------------------------------------------

def _counters(db, post_id):
    row = db.execute(
        "SELECT likes_count, comments_count, shares_count FROM news_posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    return dict(row) if row else None


def _like_rows(db, post_id):
    return db.execute(
        "SELECT user_id, created_at FROM news_likes WHERE post_id = ? ORDER BY user_id",
        (post_id,),
    ).fetchall()




# -----------------------------------------------------------
# _HookedConn
# -----------------------------------------------------------
#
# A stand-in for the connection get_db() hands the route, with
# one callback fired ONCE either just before a statement whose
# SQL contains a marker or just after the commit. That is how
# this file drives the two races the routes' banners claim to
# survive — a foreign write landing mid-request — without
# threads and without sleeping.
#
# Only execute/commit/close are forwarded, because those are
# the only three members toggle_like, share_post and
# _can_view_post touch.
#
# Used by:
#   - hook_news_db (below)
# -----------------------------------------------------------

class _HookedConn:

    def __init__(self, conn, marker=None, before=None, after_commit=None):
        self._conn = conn
        self._marker = marker
        self._before = before
        self._after_commit = after_commit
        self._fired = set()

    def _fire(self, name, callback):
        if name in self._fired or callback is None:
            return
        self._fired.add(name)
        callback()

    def execute(self, sql, *args, **kwargs):
        if self._marker and self._marker in sql:
            self._fire("before", self._before)
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self):
        self._conn.commit()
        self._fire("after", self._after_commit)

    def close(self):
        self._conn.close()




# -----------------------------------------------------------
# hook_news_db
# -----------------------------------------------------------
#
#   hook_news_db(after_commit=lambda: ...)
#   hook_news_db(marker="INSERT OR IGNORE", before=lambda: ...)
#
# Wraps ONLY the get_db name the news blueprint resolves, so
# the session lookup in auth/routes.py keeps its own plain
# connection and the callback is the only other writer.
#
# Used by:
#   - the race tests in both sections
# -----------------------------------------------------------

@pytest.fixture
def hook_news_db(monkeypatch):
    real_get_db = news_routes.get_db

    def _install(marker=None, before=None, after_commit=None):
        def _get_db():
            return _HookedConn(real_get_db(), marker=marker, before=before, after_commit=after_commit)

        monkeypatch.setattr(news_routes, "get_db", _get_db)

    return _install




# -----------------------------------------------------------
# _writer
# -----------------------------------------------------------
#
# A second connection to the very database under test, for the
# "another client got there first" callbacks. Deliberately NOT
# the route's connection and NOT the `db` fixture's, so what it
# writes is a genuinely foreign committed transaction.
# -----------------------------------------------------------

def _writer(app, statements):
    conn = sqlite3.connect(app.config["DB_PATH"], timeout=15)
    try:
        for sql, params in statements:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()




def _like(client, post_id, headers=None, **kwargs):
    return client.post(f"/api/news/{post_id}/like", headers=headers or {}, **kwargs)


def _share(client, post_id, **kwargs):
    return client.post(f"/api/news/{post_id}/share", **kwargs)








# ===========================================================
#  toggle_like — the auth chain in front of the counter
# ===========================================================

def test_a_like_without_any_token_is_401_and_writes_nothing(client, db):
    post_id = _seed_post(db)

    response = _like(client, post_id)

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"
    assert _like_rows(db, post_id) == []
    assert _counters(db, post_id)["likes_count"] == 0


@pytest.mark.parametrize("header", [
    "",
    "Bearer",
    "Bearer ",
    "Basic abcdef",
    "Token 11111111-1111-1111-1111-111111111111",
    "Bearer 11111111-1111-1111-1111-111111111111",
])
def test_a_header_that_resolves_to_no_session_cannot_like(client, db, header):
    post_id = _seed_post(db)

    response = _like(client, post_id, headers={"Authorization": header})

    assert response.status_code == 401
    assert _like_rows(db, post_id) == []


def test_a_lowercase_bearer_scheme_is_still_a_valid_token(client, db, actor):
    # RFC 7235: the scheme is case-insensitive, and _bearer_token
    # matches it that way — the like must land
    _, headers = actor
    post_id = _seed_post(db)
    raw = headers["Authorization"].split(" ", 1)[1]

    response = _like(client, post_id, headers={"Authorization": f"bearer {raw}"})

    assert response.status_code == 200
    assert response.get_json()["liked"] is True


def test_a_session_older_than_thirty_days_cannot_like(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db)

    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=31), tick=False):
        response = _like(client, post_id, headers=headers)

    assert response.status_code == 401
    assert _like_rows(db, post_id) == []
    # the expired row is purged on the way out, so the token is
    # dead for good rather than merely refused once
    assert db.execute("SELECT COUNT(*) AS c FROM sessions WHERE user_id = ?",
                      (user["id"],)).fetchone()["c"] == 0


def test_an_account_deactivated_after_login_cannot_like(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db)
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    response = _like(client, post_id, headers=headers)

    assert response.status_code == 401
    assert _like_rows(db, post_id) == []


@pytest.mark.parametrize("method", ["get", "put", "delete", "patch"])
def test_the_like_route_takes_nothing_but_post(client, db, actor, method):
    _, headers = actor
    post_id = _seed_post(db)

    response = getattr(client, method)(f"/api/news/{post_id}/like", headers=headers)

    assert response.status_code == 405
    assert response.get_json()["error"] == "Method not allowed"








# ===========================================================
#  toggle_like — the per-user quota boundary
# ===========================================================

def test_the_last_like_inside_the_budget_still_lands(client, db, actor, spend_budget):
    _, headers = actor
    user, _ = actor
    post_id = _seed_post(db)
    spend_budget("news_like", user["id"], LIKE_QUOTA - 1)

    response = _like(client, post_id, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"liked": True, "likes": 1}


def test_a_spent_like_budget_is_a_429_that_writes_nothing(client, db, actor, spend_budget):
    user, headers = actor
    post_id = _seed_post(db, likes_count=7)
    spend_budget("news_like", user["id"], LIKE_QUOTA)

    response = _like(client, post_id, headers=headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1
    # the quota sits ABOVE the route, so not even the drifted
    # counter is touched
    assert _like_rows(db, post_id) == []
    assert _counters(db, post_id)["likes_count"] == 7


def test_one_spent_like_budget_leaves_every_other_user_free(client, db, actor, make_user,
                                                            auth_headers, spend_budget):
    blocked, blocked_headers = actor
    post_id = _seed_post(db)
    spend_budget("news_like", blocked["id"], LIKE_QUOTA)
    other = make_user()

    assert _like(client, post_id, headers=blocked_headers).status_code == 429
    assert _like(client, post_id, headers=auth_headers(other)).status_code == 200








# ===========================================================
#  toggle_like — the ids that must never resolve
# ===========================================================

def test_liking_an_unknown_id_is_a_404_and_writes_nothing(client, db, actor):
    _, headers = actor
    seeded = _seed_post(db)

    response = _like(client, uuid.uuid4(), headers=headers)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}
    assert db.execute("SELECT COUNT(*) AS c FROM news_likes").fetchone()["c"] == 0
    assert _counters(db, seeded)["likes_count"] == 0


def test_a_post_id_in_the_wrong_case_does_not_resolve(client, db, actor):
    # SQLite TEXT compares byte-exact, and the mobile client
    # never re-cases an id — an upper-cased uuid is a miss
    _, headers = actor
    post_id = _seed_post(db)

    assert _like(client, post_id.upper(), headers=headers).status_code == 404
    assert _like_rows(db, post_id) == []


@pytest.mark.parametrize("raw_id", [
    "' OR '1'='1",
    "'; DELETE FROM news_posts; --",
    "../../etc/passwd",
    "naujiena-ąčę",
    " ",
    "%00",
    "z" * 5000,
])
def test_a_hostile_or_absurd_post_id_is_just_a_miss(client, db, actor, raw_id):
    _, headers = actor
    post_id = _seed_post(db)

    response = _like(client, quote(raw_id, safe=""), headers=headers)

    assert response.status_code == 404
    # the parameterised query left the table exactly as it was
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 1
    assert _like_rows(db, post_id) == []


def test_a_trailing_slash_is_not_the_like_route(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    response = client.post(f"/api/news/{post_id}/like/", headers=headers)

    assert response.status_code == 404
    assert _like_rows(db, post_id) == []








# ===========================================================
#  toggle_like — every arm of _can_view_post
# ===========================================================

def test_a_public_post_is_likeable_by_any_signed_in_member(client, db, actor):
    # the is_public short-circuit: no author, no role, no
    # friendship lookup
    _, headers = actor
    post_id = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a")

    assert _like(client, post_id, headers=headers).status_code == 200


def test_a_public_wall_post_needs_no_friendship(client, db, make_user, actor):
    stranger = make_user()
    _, headers = actor
    post_id = _seed_post(db, author_id=stranger["id"], source="user", post_type="social")

    assert _like(client, post_id, headers=headers).status_code == 200


def test_the_author_can_like_their_own_private_wall_post(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", is_public=0)

    body = _like(client, post_id, headers=headers).get_json()

    assert body == {"liked": True, "likes": 1}


def test_the_author_can_like_their_own_private_faculty_draft(client, db, make_user, auth_headers):
    author = make_user(role="teacher")
    post_id = _seed_post(db, author_id=author["id"], source="faculty",
                         post_type="announcement", is_public=0)

    assert _like(client, post_id, headers=auth_headers(author)).status_code == 200


@pytest.mark.parametrize("role,expected", [
    ("student", 404),
    ("teacher", 200),
    ("curator", 200),
    ("admin", 200),
])
def test_a_private_faculty_draft_opens_only_to_staff(client, db, make_user, auth_headers,
                                                     role, expected):
    author = make_user(role="teacher")
    reader = make_user(role=role)
    post_id = _seed_post(db, author_id=author["id"], source="faculty",
                         post_type="announcement", is_public=0)

    assert _like(client, post_id, headers=auth_headers(reader)).status_code == expected


@pytest.mark.parametrize("role,expected", [
    ("student", 404),
    ("teacher", 200),
    ("curator", 200),
    ("admin", 200),
])
def test_a_private_scraped_row_with_no_author_opens_only_to_staff(client, db, make_user,
                                                                  auth_headers, role, expected):
    # author_id NULL makes the ownership arm falsy, so the row
    # falls through to the role arms
    reader = make_user(role=role)
    post_id = _seed_post(db, author_id=None, source="knf.vu.lt",
                         source_url="https://knf.vu.lt/private", is_public=0)

    assert _like(client, post_id, headers=auth_headers(reader)).status_code == expected


@pytest.mark.parametrize("role,expected", [
    ("student", 404),
    ("teacher", 404),
    ("curator", 404),
    ("admin", 200),
])
def test_a_private_wall_post_ignores_staff_and_opens_only_to_admins_and_friends(
        client, db, make_user, auth_headers, role, expected):
    # source 'user' takes the friendship branch, so a teacher's
    # proof-reading powers stop at the wall
    author = make_user()
    reader = make_user(role=role)
    post_id = _seed_post(db, author_id=author["id"], source="user",
                         post_type="social", is_public=0)

    assert _like(client, post_id, headers=auth_headers(reader)).status_code == expected


def test_a_friend_can_like_a_private_wall_post(client, db, make_user, auth_headers):
    author = make_user()
    friend = make_user()
    _befriend(db, author["id"], friend["id"])
    post_id = _seed_post(db, author_id=author["id"], source="user",
                         post_type="social", is_public=0)

    body = _like(client, post_id, headers=auth_headers(friend)).get_json()

    assert body == {"liked": True, "likes": 1}


def test_only_the_viewer_to_author_friendship_row_opens_a_private_wall_post(
        client, db, make_user, auth_headers):
    # the gate reads friendships(user_id=viewer, friend_id=author)
    # — the mirror row alone is not enough
    author = make_user()
    reader = make_user()
    _friendship(db, author["id"], reader["id"])
    post_id = _seed_post(db, author_id=author["id"], source="user",
                         post_type="social", is_public=0)

    assert _like(client, post_id, headers=auth_headers(reader)).status_code == 404

    _friendship(db, reader["id"], author["id"])

    assert _like(client, post_id, headers=auth_headers(reader)).status_code == 200


def test_a_hidden_post_answers_the_same_404_as_a_missing_one(client, db, make_user, actor):
    # existence must not leak: same status, same body
    author = make_user()
    hidden = _seed_post(db, author_id=author["id"], source="user",
                        post_type="social", is_public=0)
    _, headers = actor

    missing_body = _like(client, uuid.uuid4(), headers=headers).get_json()
    hidden_response = _like(client, hidden, headers=headers)

    assert hidden_response.status_code == 404
    assert hidden_response.get_json() == missing_body
    assert _counters(db, hidden)["likes_count"] == 0








# ===========================================================
#  toggle_like — the flip, the row and the healing counter
# ===========================================================

@pytest.mark.contract
def test_the_like_reply_carries_exactly_liked_and_likes(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    body = _like(client, post_id, headers=headers).get_json()

    assert set(body) == {"liked", "likes"}
    assert isinstance(body["liked"], bool)
    assert isinstance(body["likes"], int)


def test_the_first_tap_stores_one_stamped_like_row(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db)

    _like(client, post_id, headers=headers)

    rows = _like_rows(db, post_id)
    assert [r["user_id"] for r in rows] == [user["id"]]
    assert rows[0]["created_at"]


def test_the_second_tap_removes_exactly_that_row(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    first = _like(client, post_id, headers=headers).get_json()
    second = _like(client, post_id, headers=headers).get_json()

    assert first == {"liked": True, "likes": 1}
    assert second == {"liked": False, "likes": 0}
    assert _like_rows(db, post_id) == []


def test_a_like_row_planted_out_of_band_makes_the_next_tap_an_unlike(client, db, actor):
    # the INSERT OR IGNORE sees rowcount 0 and takes the delete
    # arm — no SELECT window, no IntegrityError
    user, headers = actor
    post_id = _seed_post(db, likes_count=0)
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], post_id))
    db.commit()

    body = _like(client, post_id, headers=headers).get_json()

    assert body == {"liked": False, "likes": 0}
    assert _like_rows(db, post_id) == []


def test_the_counter_is_recomputed_from_every_users_rows(client, db, actor, make_user):
    # a wildly drifted stored value is replaced, not nudged
    user, headers = actor
    post_id = _seed_post(db, likes_count=99)
    for other in (make_user(), make_user()):
        db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (other["id"], post_id))
    db.commit()

    body = _like(client, post_id, headers=headers).get_json()

    assert body == {"liked": True, "likes": 3}
    assert _counters(db, post_id)["likes_count"] == 3


def test_unliking_leaves_the_other_likers_counted(client, db, actor, make_user):
    user, headers = actor
    post_id = _seed_post(db, likes_count=0)
    keeper = make_user()
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (keeper["id"], post_id))
    db.commit()

    _like(client, post_id, headers=headers)
    body = _like(client, post_id, headers=headers).get_json()

    assert body == {"liked": False, "likes": 1}
    assert [r["user_id"] for r in _like_rows(db, post_id)] == sorted([keeper["id"]])


def test_unliking_never_drives_a_drifted_counter_negative(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, likes_count=0)
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], post_id))
    db.commit()

    assert _like(client, post_id, headers=headers).get_json() == {"liked": False, "likes": 0}
    assert _counters(db, post_id)["likes_count"] == 0


def test_a_like_touches_only_its_own_post(client, db, actor):
    _, headers = actor
    liked_id = _seed_post(db)
    other_id = _seed_post(db, likes_count=4)

    _like(client, liked_id, headers=headers)

    assert _counters(db, liked_id)["likes_count"] == 1
    assert _counters(db, other_id)["likes_count"] == 4


def test_a_like_leaves_the_comment_and_share_counters_alone(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, comments_count=3, shares_count=5)

    _like(client, post_id, headers=headers)
    _like(client, post_id, headers=headers)

    assert _counters(db, post_id) == {"likes_count": 0, "comments_count": 3, "shares_count": 5}


def test_five_members_all_count_and_all_undo(client, db, make_user, auth_headers):
    post_id = _seed_post(db)
    headers = [auth_headers(make_user()) for _ in range(5)]

    counts = [_like(client, post_id, headers=h).get_json()["likes"] for h in headers]
    assert counts == [1, 2, 3, 4, 5]

    counts = [_like(client, post_id, headers=h).get_json()["likes"] for h in headers]
    assert counts == [4, 3, 2, 1, 0]
    assert _like_rows(db, post_id) == []


def test_the_like_belongs_to_the_user_not_the_session(client, db, make_user, auth_headers):
    user = make_user()
    post_id = _seed_post(db)
    first_session = auth_headers(user)
    second_session = auth_headers(user)

    assert _like(client, post_id, headers=first_session).get_json()["liked"] is True
    assert _like(client, post_id, headers=second_session).get_json()["liked"] is False


def test_a_poll_card_is_likeable_like_any_other_post(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll", source="faculty")

    assert _like(client, post_id, headers=headers).get_json() == {"liked": True, "likes": 1}


@pytest.mark.parametrize("kwargs", [
    {"json": {"liked": False}},
    {"data": b"{not json", "content_type": "application/json"},
    {"data": "liked=false", "content_type": "application/x-www-form-urlencoded"},
    {"data": b"", "content_type": "application/json"},
])
def test_the_like_route_ignores_whatever_body_it_is_sent(client, db, actor, kwargs):
    # the toggle takes its input from the URL and the token
    # alone — nothing here parses a body
    _, headers = actor
    post_id = _seed_post(db)

    response = _like(client, post_id, headers=headers, **kwargs)

    assert response.status_code == 200
    assert response.get_json()["liked"] is True


def test_a_non_object_json_body_never_reaches_the_like_at_all(client, db, actor):
    # create_app's validate_json_input hook refuses a top-level
    # array for EVERY route, so the like is a 400 before the
    # blueprint runs — and nothing is written
    _, headers = actor
    post_id = _seed_post(db)

    response = _like(client, post_id, headers=headers, json=["not", "an", "object"])

    assert response.status_code == 400
    assert _like_rows(db, post_id) == []


def test_query_parameters_on_the_like_route_are_ignored(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    response = client.post(f"/api/news/{post_id}/like?liked=false&page=9", headers=headers)

    assert response.get_json() == {"liked": True, "likes": 1}








# ===========================================================
#  toggle_like — the races, driven through the connection
# ===========================================================

def test_a_like_landing_during_the_commit_is_in_the_reply(client, db, app, actor,
                                                          make_user, hook_news_db):
    # the banner's promise: the count is RE-READ after the
    # commit, so it carries likes other users landed meanwhile
    _, headers = actor
    other = make_user()
    post_id = _seed_post(db)

    hook_news_db(after_commit=lambda: _writer(app, [
        ("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (other["id"], post_id)),
        ("UPDATE news_posts SET likes_count = (SELECT COUNT(*) FROM news_likes WHERE post_id = ?)"
         " WHERE id = ?", (post_id, post_id)),
    ]))

    body = _like(client, post_id, headers=headers).get_json()

    assert body == {"liked": True, "likes": 2}


def test_the_same_like_arriving_first_ends_as_an_unlike_not_a_500(client, db, app, actor,
                                                                  hook_news_db):
    # two taps of one user racing: the duplicate row lands
    # between the gate and our INSERT OR IGNORE, so rowcount 0
    # takes the delete arm instead of raising an IntegrityError
    user, headers = actor
    post_id = _seed_post(db)

    hook_news_db(marker="INSERT OR IGNORE", before=lambda: _writer(app, [
        ("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], post_id)),
    ]))

    response = _like(client, post_id, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"liked": False, "likes": 0}
    assert _like_rows(db, post_id) == []


def test_a_post_deleted_during_the_like_commit_does_not_crash(client, db, app, actor,
                                                              hook_news_db):
    _, headers = actor
    post_id = _seed_post(db)

    hook_news_db(after_commit=lambda: _writer(app, [
        ("DELETE FROM news_likes WHERE post_id = ?", (post_id,)),
        ("DELETE FROM news_posts WHERE id = ?", (post_id,)),
    ]))

    assert _like(client, post_id, headers=headers).status_code == 404








# ===========================================================
#  share_post — the one write with no auth, gate and all
# ===========================================================

@pytest.mark.contract
def test_the_share_reply_carries_exactly_a_share_count(client, db):
    post_id = _seed_post(db)

    response = _share(client, post_id)

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"shares"}
    assert body["shares"] == 1
    assert isinstance(body["shares"], int)


def test_a_share_needs_no_auth_even_with_a_broken_token(client, db):
    # the route resolves the caller OPTIONALLY (get_current_user,
    # for the visibility gate), so a garbage header is not an
    # error — it just leaves the caller a guest, and a guest may
    # share a public post
    post_id = _seed_post(db)

    response = _share(client, post_id, headers={"Authorization": "Bearer not-a-token"})

    assert response.status_code == 200
    assert response.get_json() == {"shares": 1}


def test_a_member_share_counts_the_same_as_a_guests(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    assert _share(client, post_id).get_json() == {"shares": 1}
    assert _share(client, post_id, headers=headers).get_json() == {"shares": 2}


def test_every_share_counts_again_and_none_is_ever_taken_back(client, db):
    post_id = _seed_post(db)

    counts = [_share(client, post_id).get_json()["shares"] for _ in range(6)]

    assert counts == [1, 2, 3, 4, 5, 6]
    assert _counters(db, post_id)["shares_count"] == 6


def test_a_share_stores_no_per_user_state(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    _share(client, post_id, headers=headers)

    assert db.execute("SELECT COUNT(*) AS c FROM news_likes").fetchone()["c"] == 0


def test_a_share_leaves_the_like_and_comment_counters_alone(client, db):
    post_id = _seed_post(db, likes_count=2, comments_count=3)

    _share(client, post_id)

    assert _counters(db, post_id) == {"likes_count": 2, "comments_count": 3, "shares_count": 1}


def test_two_posts_keep_independent_share_counters(client, db):
    shared_id = _seed_post(db)
    other_id = _seed_post(db, shares_count=8)

    _share(client, shared_id)

    assert _counters(db, shared_id)["shares_count"] == 1
    assert _counters(db, other_id)["shares_count"] == 8


def test_sharing_an_unknown_post_is_a_404(client, db):
    _seed_post(db)

    response = _share(client, uuid.uuid4())

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


@pytest.mark.parametrize("raw_id", [
    "' OR '1'='1",
    "'; UPDATE news_posts SET shares_count = 999; --",
    "naujiena-ąčę",
    "z" * 5000,
])
def test_a_hostile_post_id_cannot_move_a_share_counter(client, db, raw_id):
    post_id = _seed_post(db)

    assert _share(client, quote(raw_id, safe="")).status_code == 404
    assert _counters(db, post_id)["shares_count"] == 0


def test_a_post_id_in_the_wrong_case_does_not_resolve_for_a_share(client, db):
    post_id = _seed_post(db)

    assert _share(client, post_id.upper()).status_code == 404
    assert _counters(db, post_id)["shares_count"] == 0


@pytest.mark.parametrize("method", ["get", "put", "delete", "patch"])
def test_the_share_route_takes_nothing_but_post(client, db, method):
    post_id = _seed_post(db)

    response = getattr(client, method)(f"/api/news/{post_id}/share")

    assert response.status_code == 405
    assert _counters(db, post_id)["shares_count"] == 0


@pytest.mark.parametrize("kwargs", [
    {"json": {"shares": 100}},
    {"data": b"{not json", "content_type": "application/json"},
    {"data": "shares=100", "content_type": "application/x-www-form-urlencoded"},
])
def test_the_share_route_ignores_whatever_body_it_is_sent(client, db, kwargs):
    post_id = _seed_post(db)

    response = _share(client, post_id, **kwargs)

    assert response.status_code == 200
    assert response.get_json() == {"shares": 1}


def test_a_non_object_json_body_never_reaches_the_share_at_all(client, db):
    # the same global validate_json_input hook, on the one route
    # here that has no auth in front of it either
    post_id = _seed_post(db)

    response = _share(client, post_id, json=[1, 2, 3])

    assert response.status_code == 400
    assert _counters(db, post_id)["shares_count"] == 0








# ===========================================================
#  share_post — counter boundaries and the missing quota
# ===========================================================

def test_a_share_counts_up_from_a_negative_stored_value(client, db):
    # nothing floors this counter; a drifted row simply keeps
    # counting from wherever it is
    post_id = _seed_post(db, shares_count=-5)

    assert _share(client, post_id).get_json() == {"shares": -4}


def test_a_share_at_the_int64_ceiling_promotes_the_counter_to_a_float(client, db):
    # SQLite answers an integer overflow with a REAL rather than
    # an error, so the wire value stops being an int — pinned
    # here because a client parsing it as an int would break
    post_id = _seed_post(db, shares_count=9223372036854775807)

    body = _share(client, post_id).get_json()

    assert body["shares"] > 9.2e18
    assert db.execute("SELECT typeof(shares_count) AS t FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["t"] == "real"


def test_the_share_route_carries_no_per_route_quota(client, db, spend_budget):
    # unlike every other write in this module, share_post has no
    # rate_limit decorator: a filled news_share bucket changes
    # nothing
    post_id = _seed_post(db)
    spend_budget("news_share", "127.0.0.1", 1000)

    assert _share(client, post_id).status_code == 200


def test_the_global_ip_budget_is_the_only_thing_metering_shares(client, db, spend_budget):
    post_id = _seed_post(db)
    spend_budget("global", "127.0.0.1", _GLOBAL_RATE_LIMIT_MAX)

    response = _share(client, post_id)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert _counters(db, post_id)["shares_count"] == 0


def test_a_share_landing_during_the_commit_is_in_the_reply(client, db, app, hook_news_db):
    post_id = _seed_post(db)

    hook_news_db(after_commit=lambda: _writer(app, [
        ("UPDATE news_posts SET shares_count = shares_count + 1 WHERE id = ?", (post_id,)),
    ]))

    assert _share(client, post_id).get_json() == {"shares": 2}


def test_a_post_deleted_during_the_share_commit_does_not_crash(client, db, app, hook_news_db):
    post_id = _seed_post(db)

    hook_news_db(after_commit=lambda: _writer(app, [
        ("DELETE FROM news_posts WHERE id = ?", (post_id,)),
    ]))

    assert _share(client, post_id).status_code == 404


def test_a_stranger_cannot_share_a_private_wall_post(client, db, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user",
                         post_type="social", is_public=0)

    assert _share(client, post_id).status_code == 404


def test_a_guest_cannot_share_a_private_faculty_draft(client, db, make_user):
    author = make_user(role="teacher")
    post_id = _seed_post(db, author_id=author["id"], source="faculty",
                         post_type="announcement", is_public=0)

    _share(client, post_id)

    assert _counters(db, post_id)["shares_count"] == 0
