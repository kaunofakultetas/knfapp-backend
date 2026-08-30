# -----------------------------------------------------------
#  [*] Tests — GET /api/news, the exhaustive pass
#
#  The gap-closing companion to test_news_feed.py. It owns
#  four functions of news/routes.py and drives every branch,
#  guard and boundary each of them still had open:
#
#    get_feed       — the whole WHERE-clause matrix (guest,
#                     student, teacher, curator, admin ×
#                     public / private / wall / draft rows ×
#                     no filter, ?source=user, ?source=faculty,
#                     ?source=app), the friend-set edges
#                     (none, many, self-friendship, a
#                     one-directional row), the hasMore
#                     arithmetic at its boundaries, and the
#                     guards that must answer before anything
#                     is ranked or stamped
#    _feed_version  — every term of the watermark on its own:
#                     the row count, the newest published_at,
#                     the newest updated_at and each of the
#                     three engagement counters, plus the two
#                     blank-stamp fallbacks and the fact that
#                     the watermark is table-wide, not
#                     per-viewer
#    _cacheable     — the weak tag's shape, both scopes on
#                     both the 200 and the 304, "*", the
#                     strong spelling, a list of candidates,
#                     garbage, another viewer's tag, and the
#                     Cache-Control that must survive the
#                     app-wide no-store hook
#    _post_to_dict  — one key per column with nothing shared
#                     or swapped, NULL vs empty string, the
#                     bool() on is_public, both arms of the
#                     author-name COALESCE (including a
#                     dangling author_id), and the fact that
#                     the feed card and the single-post body
#                     come out of this one producer
#
#  Rows are seeded with raw SQL throughout: the feed has to
#  serve rows no route can create — scraped articles, drafts,
#  blank and corrupted stamps, a counter poked behind the
#  routes' back — and only direct INSERTs produce them.
#
#  Nothing here sleeps and nothing reaches the network.
# -----------------------------------------------------------


import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.auth import routes as auth_routes
from app.news import routes as news_routes
from app.news.routes import FEED_CACHE_MAX_AGE, SOURCES


FEED = "/api/news"

# _cacheable's tag: sha256 truncated to 32 hex, weak-marked
ETAG_RE = re.compile(r'^W/"[0-9a-f]{32}"$')

# The mobile NewsPost keys plus the per-viewer flag get_feed
# attaches to every card
CARD_KEYS = {
    "id", "title", "content", "summary", "imageUrl", "author", "authorId",
    "source", "sourceUrl", "postType", "likes", "comments", "shares",
    "date", "isPublic", "liked",
}




# -----------------------------------------------------------
# _clear_rate_limits
# -----------------------------------------------------------
#
# app/__init__.py's throttle_requests meters EVERY request
# against one process-global bucket keyed on the client IP,
# and every test in the suite shares 127.0.0.1. A file this
# request-heavy would start answering 429 halfway through —
# and would push the files after it over the edge too — so
# the bucket is emptied around each test, the same guard
# test_auth_login.py and test_chat_mutate_deep.py use.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_rate_limits():
    auth_routes._rate_limit_store.clear()
    yield
    auth_routes._rate_limit_store.clear()




# -----------------------------------------------------------
# _iso
# -----------------------------------------------------------
#
# A stamp `days` from now in the shape the module writes:
# aware UTC, ISO T-form, no microseconds so the string
# comparison behind ?before stays readable in a failure.
# -----------------------------------------------------------

def _iso(days=0.0, base=None):
    moment = (base or datetime.now(timezone.utc)) + timedelta(days=days)
    return moment.replace(microsecond=0).isoformat()




# -----------------------------------------------------------
# make_post
# -----------------------------------------------------------
#
#   pid = make_post(source="faculty", is_public=0, days=-1)
#
# One news_posts row, inserted straight through sqlite3 so a
# test can produce what POST /api/news never could: a scraped
# article, an unpublished draft, a blank or malformed stamp,
# a counter that no route bumped. published_at and updated_at
# are settable apart, because _feed_version reads them apart.
# -----------------------------------------------------------

@pytest.fixture
def make_post(app):

    def _make(source="app", post_type="article", is_public=1, days=0.0, author_id=None,
              author_name=None, title=None, content="Turinys", summary=None, image_url=None,
              source_url=None, likes=0, comments=0, shares=0,
              published_at=None, updated_at=None, post_id=None):
        post_id = post_id or str(uuid.uuid4())
        stamp = published_at if published_at is not None else _iso(days)
        touched = updated_at if updated_at is not None else stamp

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO news_posts
                   (id, title, content, summary, image_url, author_id, author_name,
                    source, source_url, post_type, is_public,
                    likes_count, comments_count, shares_count,
                    published_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (post_id, title or f"Post {post_id[:8]}", content, summary, image_url,
                 author_id, author_name, source, source_url, post_type, is_public,
                 likes, comments, shares, stamp, stamp, touched),
            )
            conn.commit()
        finally:
            conn.close()

        return post_id

    return _make




# -----------------------------------------------------------
# sql
# -----------------------------------------------------------
#
#   sql("UPDATE news_posts SET updated_at = ?", (stamp,))
#
# Arbitrary write on the test database, for the states only a
# poke behind the routes' back can produce — a counter moved
# without its stamp, a friendship row deleted, a stamp blanked.
# -----------------------------------------------------------

@pytest.fixture
def sql(app):

    def _sql(statement, params=()):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(statement, params)
            conn.commit()
        finally:
            conn.close()

    return _sql




# -----------------------------------------------------------
# link
# -----------------------------------------------------------
#
# The friendships pair social/routes.py writes on accept —
# BOTH directions by default, one direction when a test needs
# to prove the feed reads the caller's own row only.
# -----------------------------------------------------------

@pytest.fixture
def link(app):

    def _link(user_id, friend_id, both=True):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)",
                         (user_id, friend_id))
            if both:
                conn.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)",
                             (friend_id, user_id))
            conn.commit()
        finally:
            conn.close()

    return _link




# -----------------------------------------------------------
# make_poll
# -----------------------------------------------------------
#
# A poll with its options in insertion order, and the post
# flipped to post_type 'poll' the way create_poll flips it.
# -----------------------------------------------------------

@pytest.fixture
def make_poll(app):

    def _make(post_id, options=("Taip", "Ne"), title="Klausimas", end_date=None):
        poll_id = str(uuid.uuid4())
        option_ids = [str(uuid.uuid4()) for _ in options]

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO polls (id, post_id, title, end_date, created_at) VALUES (?, ?, ?, ?, ?)",
                (poll_id, post_id, title, end_date, _iso()),
            )
            for option_id, text in zip(option_ids, options):
                conn.execute(
                    "INSERT INTO poll_options (id, poll_id, text, votes) VALUES (?, ?, ?, 0)",
                    (option_id, poll_id, text),
                )
            conn.execute("UPDATE news_posts SET post_type = 'poll' WHERE id = ?", (post_id,))
            conn.commit()
        finally:
            conn.close()

        return poll_id, option_ids

    return _make




# -----------------------------------------------------------
# world
# -----------------------------------------------------------
#
#   ids = world(viewer)
#
# The twelve-row world every visibility test below is asked
# about, built around ONE viewer: the public rows a guest may
# read, the viewer's own wall posts and own draft, a friend's
# wall posts, a stranger's wall posts, and the two private
# non-wall rows only staff may read. Returns {name: post id},
# so an assertion reads as a set of names instead of uuids.
# -----------------------------------------------------------

@pytest.fixture
def world(make_post, make_user, link):

    def _world(viewer):
        staff = make_user(role="teacher")
        friend = make_user()
        stranger = make_user()
        link(viewer["id"], friend["id"])

        return {
            "public_article": make_post(source="knf.vu.lt", author_name="knf.vu.lt",
                                        source_url="https://knf.vu.lt/a"),
            "public_app": make_post(source="app"),
            "public_faculty": make_post(source="faculty", author_id=staff["id"]),
            "draft_faculty": make_post(source="faculty", is_public=0, author_id=staff["id"]),
            "private_app": make_post(source="app", is_public=0, author_id=stranger["id"]),
            "own_faculty_draft": make_post(source="faculty", is_public=0, author_id=viewer["id"]),
            "own_wall_public": make_post(source="user", post_type="social", author_id=viewer["id"]),
            "own_wall_private": make_post(source="user", post_type="social", is_public=0,
                                          author_id=viewer["id"]),
            "friend_wall_public": make_post(source="user", post_type="social",
                                            author_id=friend["id"]),
            "friend_wall_private": make_post(source="user", post_type="social", is_public=0,
                                             author_id=friend["id"]),
            "stranger_wall_public": make_post(source="user", post_type="social",
                                              author_id=stranger["id"]),
            "stranger_wall_private": make_post(source="user", post_type="social", is_public=0,
                                               author_id=stranger["id"]),
        }

    return _world




# -----------------------------------------------------------
# _names
# -----------------------------------------------------------
#
# The page's rows as the world's names — the readable subject
# of every visibility assertion below.
# -----------------------------------------------------------

def _names(response, ids):
    by_id = {pid: name for name, pid in ids.items()}
    return {by_id[post["id"]] for post in response.get_json()["posts"]}




# -----------------------------------------------------------
# _ids / _tag
# -----------------------------------------------------------
#
# The page's post ids in the order the feed ranked them, and
# the weak ETag _cacheable stamped on the answer.
# -----------------------------------------------------------

def _ids(response):
    return [post["id"] for post in response.get_json()["posts"]]


def _tag(response):
    return response.headers["ETag"]




# -----------------------------------------------------------
# _post_to_dict — one key per column, nothing shared
# -----------------------------------------------------------


def test_every_card_key_carries_its_own_column(client, make_post, make_user):
    author = make_user(display_name="Ona Onaite")
    post_id = make_post(
        source="faculty", post_type="announcement", author_id=author["id"],
        author_name="Snapshot", title="Antraste", content="Tekstas", summary="Santrauka",
        image_url="/api/uploads/abc.jpg", source_url="https://knf.vu.lt/x",
        likes=1, comments=2, shares=3, published_at="2026-05-04T03:02:01+00:00",
    )

    card = client.get(FEED).get_json()["posts"][0]

    assert card == {
        "id": post_id,
        "title": "Antraste",
        "content": "Tekstas",
        "summary": "Santrauka",
        "imageUrl": "/api/uploads/abc.jpg",
        "author": "Ona Onaite",
        "authorId": author["id"],
        "source": "faculty",
        "sourceUrl": "https://knf.vu.lt/x",
        "postType": "announcement",
        "likes": 1,
        "comments": 2,
        "shares": 3,
        "date": "2026-05-04T03:02:01+00:00",
        "isPublic": True,
        "liked": False,
    }


def test_the_three_counters_never_swap_places(client, make_post):
    make_post(source="faculty", likes=7, comments=8, shares=9)

    card = client.get(FEED).get_json()["posts"][0]

    assert (card["likes"], card["comments"], card["shares"]) == (7, 8, 9)


def test_the_counters_travel_as_numbers(client, make_post):
    make_post(source="faculty", likes=4)

    card = client.get(FEED).get_json()["posts"][0]

    for key in ("likes", "comments", "shares"):
        assert isinstance(card[key], int)


def test_a_null_column_travels_as_null_not_as_an_empty_string(client, make_post):
    make_post(source="knf.vu.lt", summary=None, image_url=None, source_url=None,
              author_id=None, author_name=None)

    card = client.get(FEED).get_json()["posts"][0]

    assert card["summary"] is None
    assert card["imageUrl"] is None
    assert card["sourceUrl"] is None
    assert card["authorId"] is None
    assert card["author"] is None


def test_an_empty_column_stays_an_empty_string(client, make_post):
    make_post(source="knf.vu.lt", summary="", image_url="", author_name="")

    card = client.get(FEED).get_json()["posts"][0]

    assert card["summary"] == ""
    assert card["imageUrl"] == ""
    assert card["author"] == ""


@pytest.mark.parametrize("stored, expected", [(1, True), (0, False), (2, True)])
def test_is_public_goes_out_as_a_boolean(client, make_post, actor, stored, expected):
    user, headers = actor
    make_post(source="faculty", is_public=stored, author_id=user["id"])

    card = client.get(FEED, headers=headers).get_json()["posts"][0]

    assert card["isPublic"] is expected


def test_the_live_display_name_wins_over_the_stored_snapshot(client, make_post, make_user, sql):
    author = make_user(display_name="Senas Vardas")
    make_post(source="faculty", author_id=author["id"], author_name="Senas Vardas")
    sql("UPDATE users SET display_name = 'Naujas Vardas' WHERE id = ?", (author["id"],))

    assert client.get(FEED).get_json()["posts"][0]["author"] == "Naujas Vardas"


def test_a_dangling_author_id_falls_back_to_the_snapshot(client, make_post):
    orphan = str(uuid.uuid4())
    make_post(source="faculty", author_id=orphan, author_name="Dinges Autorius")

    card = client.get(FEED).get_json()["posts"][0]

    assert card["author"] == "Dinges Autorius"
    assert card["authorId"] == orphan


def test_a_scraped_row_with_no_author_at_all_serves_both_as_null(client, make_post):
    make_post(source="vu.lt", author_id=None, author_name=None)

    card = client.get(FEED).get_json()["posts"][0]

    assert (card["author"], card["authorId"]) == (None, None)


@pytest.mark.parametrize("stored", ["2026-01-02 03:04:05", "2026-01-02T03:04:05+00:00", "ne-data"])
def test_the_date_is_the_stored_published_at_verbatim(client, make_post, stored):
    # `date` is published_at as stored — unlike a comment's
    # `time`, nothing normalises it, so a legacy space-form row
    # and even an unparseable one reach the client in the shape
    # the row carries
    make_post(source="faculty", published_at=stored)

    assert client.get(FEED).get_json()["posts"][0]["date"] == stored


def test_markup_in_a_card_is_escaped_on_the_way_out(client, make_post):
    make_post(source="faculty", title="<b>Labas</b>", content='Ji pasake "ne" & isejo')

    card = client.get(FEED).get_json()["posts"][0]

    assert card["title"] == "&lt;b&gt;Labas&lt;/b&gt;"
    assert card["content"] == "Ji pasake &quot;ne&quot; &amp; isejo"


def test_lithuanian_letters_survive_the_card(client, make_post):
    make_post(source="faculty", title="Ąžuolas", content="Šįvakar bus ūkanota")

    card = client.get(FEED).get_json()["posts"][0]

    assert card["title"] == "Ąžuolas"
    assert card["content"] == "Šįvakar bus ūkanota"


@pytest.mark.contract
def test_a_card_carries_the_documented_keys_and_nothing_else(client, make_post):
    make_post(source="faculty")

    assert set(client.get(FEED).get_json()["posts"][0]) == CARD_KEYS


@pytest.mark.contract
def test_a_poll_card_adds_the_poll_key_and_only_that(client, make_post, make_poll):
    post_id = make_post(source="faculty")
    make_poll(post_id)

    assert set(client.get(FEED).get_json()["posts"][0]) == CARD_KEYS | {"poll"}


@pytest.mark.contract
def test_the_feed_card_and_the_single_post_body_are_the_same_shape(client, make_post, make_user):
    author = make_user()
    post_id = make_post(source="faculty", author_id=author["id"], summary="Santrauka",
                        image_url="/api/uploads/x.png", likes=2, comments=1, shares=5)

    card = client.get(FEED).get_json()["posts"][0]
    single = client.get(f"{FEED}/{post_id}").get_json()

    assert card == single




# -----------------------------------------------------------
# _feed_version — every term of the watermark on its own
# -----------------------------------------------------------


def test_an_empty_feed_and_a_one_row_feed_do_not_share_a_tag(client, make_post):
    empty = _tag(client.get(FEED))
    make_post(source="faculty")

    assert _tag(client.get(FEED)) != empty


def test_deleting_a_row_moves_the_tag_although_no_stamp_moved(client, make_post, sql):
    stamp = _iso(-1)
    make_post(source="faculty", published_at=stamp)
    doomed = make_post(source="faculty", published_at=stamp)
    tag = _tag(client.get(FEED))

    sql("DELETE FROM news_posts WHERE id = ?", (doomed,))

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


def test_a_moved_published_at_alone_moves_the_tag(client, make_post, sql):
    post_id = make_post(source="faculty", days=-2)
    tag = _tag(client.get(FEED))

    sql("UPDATE news_posts SET published_at = ? WHERE id = ?", (_iso(-1), post_id))

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


def test_a_moved_updated_at_alone_moves_the_tag(client, make_post, sql):
    # Nothing else changes: same row count, same published_at,
    # same counters — only the watermark's `touched` term
    post_id = make_post(source="faculty", days=-2)
    tag = _tag(client.get(FEED))

    sql("UPDATE news_posts SET updated_at = ? WHERE id = ?", (_iso(5), post_id))

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


@pytest.mark.parametrize("column", ["likes_count", "comments_count", "shares_count"])
def test_each_engagement_counter_moves_the_tag_on_its_own(client, make_post, sql, column):
    post_id = make_post(source="faculty", days=-1)
    tag = _tag(client.get(FEED))

    sql(f"UPDATE news_posts SET {column} = {column} + 1 WHERE id = ?", (post_id,))

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


def test_a_share_through_its_own_route_moves_the_tag(client, make_post):
    post_id = make_post(source="faculty", days=-1)
    tag = _tag(client.get(FEED))

    assert client.post(f"{FEED}/{post_id}/share").status_code == 200
    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


def test_a_comment_through_its_own_route_moves_the_tag(client, make_post, actor):
    _, headers = actor
    post_id = make_post(source="faculty", days=-1)
    tag = _tag(client.get(FEED))

    assert client.post(f"{FEED}/{post_id}/comments", json={"text": "Sveiki"},
                       headers=headers).status_code == 201
    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


def test_a_row_the_caller_can_never_see_still_moves_their_tag(client, make_post, make_user):
    # The watermark is the TABLE's, not the page's: an invisible
    # insert costs a needless revalidation, never a stale page
    make_post(source="faculty")
    stranger = make_user()
    tag = _tag(client.get(FEED))

    make_post(source="user", post_type="social", is_public=0, author_id=stranger["id"])

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


def test_a_write_outside_news_posts_leaves_the_tag_alone(client, make_post, make_user):
    make_post(source="faculty")
    tag = _tag(client.get(FEED))

    make_user()

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 304


def test_blank_stamps_still_produce_a_usable_tag(client, make_post):
    # MAX() over blank text is falsy, so both watermark slots
    # fall back to '-' — the page must still be served and still
    # revalidate
    make_post(source="faculty", published_at="", updated_at="")

    first = client.get(FEED)

    assert first.status_code == 200
    assert len(first.get_json()["posts"]) == 1
    assert ETAG_RE.match(_tag(first))
    assert client.get(FEED, headers={"If-None-Match": _tag(first)}).status_code == 304


def test_a_blank_stamped_row_is_not_the_empty_feed(client, make_post):
    empty = _tag(client.get(FEED))
    make_post(source="faculty", published_at="", updated_at="")

    assert _tag(client.get(FEED)) != empty


def test_the_friend_hash_returns_to_its_old_value_when_the_friendship_goes(
        client, make_post, make_user, link, sql, actor):
    user, headers = actor
    friend = make_user()
    make_post(source="faculty")
    alone = _tag(client.get(FEED, headers=headers))

    link(user["id"], friend["id"])
    befriended = _tag(client.get(FEED, headers=headers))
    sql("DELETE FROM friendships WHERE user_id = ? OR friend_id = ?", (user["id"], user["id"]))

    assert befriended != alone
    assert _tag(client.get(FEED, headers=headers)) == alone


def test_two_identical_requests_agree_on_the_tag_and_the_body(client, make_post):
    for index in range(3):
        make_post(source="faculty", days=-index)

    first = client.get(FEED)
    second = client.get(FEED)

    assert _tag(first) == _tag(second)
    assert first.get_json() == second.get_json()


def test_an_unlike_and_a_comment_that_cancel_out_still_bust_the_tag(client, make_post, actor,
                                                                    make_user, auth_headers):
    _, headers = actor
    other = make_user()
    post_id = make_post(source="faculty", days=-1)
    assert client.post(f"{FEED}/{post_id}/like", headers=headers).get_json()["liked"] is True
    tag = _tag(client.get(FEED))

    assert client.post(f"{FEED}/{post_id}/like", headers=headers).get_json()["liked"] is False
    assert client.post(f"{FEED}/{post_id}/comments", json={"text": "Sveiki"},
                       headers=auth_headers(other)).status_code == 201

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200




# -----------------------------------------------------------
# _cacheable — the weak tag and the two scopes
# -----------------------------------------------------------


def test_the_tag_is_weak_and_thirty_two_hex_characters(client, make_post):
    make_post(source="faculty")

    assert ETAG_RE.match(_tag(client.get(FEED)))


def test_an_empty_feed_is_tagged_the_same_way(client):
    assert ETAG_RE.match(_tag(client.get(FEED)))


@pytest.mark.parametrize("logged_in, scope", [(False, "public"), (True, "private")])
def test_the_scope_follows_the_viewer_on_the_page(client, make_post, actor, logged_in, scope):
    _, headers = actor
    make_post(source="faculty")

    response = client.get(FEED, headers=headers if logged_in else {})

    assert response.headers["Cache-Control"] == f"{scope}, max-age={FEED_CACHE_MAX_AGE}"


@pytest.mark.parametrize("logged_in, scope", [(False, "public"), (True, "private")])
def test_the_scope_follows_the_viewer_on_the_304_too(client, make_post, actor, logged_in, scope):
    _, headers = actor
    make_post(source="faculty")
    used = dict(headers) if logged_in else {}
    first = client.get(FEED, headers=used)

    second = client.get(FEED, headers={**used, "If-None-Match": _tag(first)})

    assert second.status_code == 304
    assert second.headers["Cache-Control"] == f"{scope}, max-age={FEED_CACHE_MAX_AGE}"
    assert second.headers["ETag"] == _tag(first)
    assert second.get_data() == b""


def test_the_feed_keeps_its_cache_control_against_the_app_wide_no_store(client, make_post):
    make_post(source="faculty")

    assert "no-store" not in client.get(FEED).headers["Cache-Control"]


@pytest.mark.parametrize("star", ["*", "W/*"])
def test_a_star_if_none_match_answers_304(client, make_post, star):
    make_post(source="faculty")

    response = client.get(FEED, headers={"If-None-Match": star})

    assert response.status_code == 304
    assert response.get_data() == b""


def test_the_strong_spelling_of_the_tag_still_answers_304(client, make_post):
    make_post(source="faculty")
    weak = _tag(client.get(FEED))

    response = client.get(FEED, headers={"If-None-Match": weak[2:]})

    assert response.status_code == 304


def test_one_matching_candidate_among_several_answers_304(client, make_post):
    make_post(source="faculty")
    weak = _tag(client.get(FEED))

    response = client.get(FEED, headers={"If-None-Match": f'W/"deadbeef", {weak}, W/"cafe"'})

    assert response.status_code == 304


@pytest.mark.parametrize("candidate", ["", "   ", "deadbeef", 'W/""', '"deadbeef"',
                                       'W/"deadbeef", W/"cafe"'])
def test_a_tag_that_does_not_match_gets_the_whole_page(client, make_post, candidate):
    make_post(source="faculty")

    response = client.get(FEED, headers={"If-None-Match": candidate})

    assert response.status_code == 200
    assert len(response.get_json()["posts"]) == 1


def test_a_guests_tag_never_shortcuts_a_members_request(client, make_post, actor):
    _, headers = actor
    make_post(source="faculty")
    guest_tag = _tag(client.get(FEED))

    response = client.get(FEED, headers={**headers, "If-None-Match": guest_tag})

    assert response.status_code == 200


def test_a_members_tag_never_shortcuts_a_guests_request(client, make_post, actor):
    _, headers = actor
    make_post(source="faculty")
    member_tag = _tag(client.get(FEED, headers=headers))

    response = client.get(FEED, headers={"If-None-Match": member_tag})

    assert response.status_code == 200


def test_a_refused_page_parameter_is_never_tagged(client, make_post):
    make_post(source="faculty")

    response = client.get(f"{FEED}?page=0", headers={"If-None-Match": "*"})

    assert response.status_code == 400
    assert "ETag" not in response.headers


def test_a_refused_source_parameter_is_never_tagged(client, make_post):
    make_post(source="faculty")

    response = client.get(f"{FEED}?source=twitter", headers={"If-None-Match": "*"})

    assert response.status_code == 400
    assert "ETag" not in response.headers


def test_a_refused_before_parameter_is_never_tagged(client, make_post):
    make_post(source="faculty")

    response = client.get(f"{FEED}?before=vakar", headers={"If-None-Match": "*"})

    assert response.status_code == 400
    assert "ETag" not in response.headers


def test_the_304_is_answered_before_a_single_row_is_ranked(client, make_post, monkeypatch):
    # Breaking the ranking SQL proves where the early return
    # sits: the tagged request never reaches the ORDER BY, while
    # the same request without the tag dies in it
    make_post(source="faculty")
    tag = _tag(client.get(FEED))
    monkeypatch.setattr(news_routes, "FEED_SCORE_SQL", "no_such_column_xyz")

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 304
    assert client.get(FEED).status_code == 500


def test_the_tag_separates_the_page_from_the_per_page(client, make_post):
    # "1|23" and "12|3" must not collapse into one seed
    make_post(source="faculty")

    assert _tag(client.get(f"{FEED}?page=1&per_page=23")) != _tag(
        client.get(f"{FEED}?page=12&per_page=3"))


def test_every_query_parameter_gets_its_own_tag(client, make_post):
    make_post(source="faculty")
    queries = ["", "?page=2", "?per_page=5", "?source=faculty", "?before=2030-01-01"]

    tags = {_tag(client.get(f"{FEED}{query}")) for query in queries}

    assert len(tags) == len(queries)


@pytest.mark.parametrize("spelling", [
    "2030-01-01T00:00:00+00:00",
    "2030-01-01 00:00:00 00:00",
    "2030-01-01T00:00:00Z",
    "2030-01-01T03:00:00+03:00",
])
def test_equivalent_before_spellings_share_one_tag(client, make_post, spelling):
    make_post(source="faculty", days=-1)
    canonical = _tag(client.get(FEED, query_string={"before": "2030-01-01T00:00:00+00:00"}))

    assert _tag(client.get(FEED, query_string={"before": spelling})) == canonical




# -----------------------------------------------------------
# get_feed — the visibility matrix, role by role
# -----------------------------------------------------------


PUBLIC_ROWS = {"public_article", "public_app", "public_faculty"}
MEMBER_ROWS = PUBLIC_ROWS | {"own_wall_public", "own_wall_private", "friend_wall_public",
                             "friend_wall_private", "own_faculty_draft"}
STAFF_ROWS = MEMBER_ROWS | {"draft_faculty", "private_app"}


def test_a_guest_sees_exactly_the_public_non_wall_rows(client, world, make_user):
    ids = world(make_user())

    response = client.get(f"{FEED}?per_page=50")

    assert _names(response, ids) == PUBLIC_ROWS
    assert response.get_json()["total"] == len(PUBLIC_ROWS)


@pytest.mark.parametrize("role, expected", [
    ("student", MEMBER_ROWS),
    ("teacher", STAFF_ROWS),
    ("curator", STAFF_ROWS),
    ("admin", STAFF_ROWS),
])
def test_the_feed_shows_a_role_exactly_the_rows_it_may_see(client, world, make_user,
                                                           auth_headers, role, expected):
    viewer = make_user(role=role)
    ids = world(viewer)

    response = client.get(f"{FEED}?per_page=50", headers=auth_headers(viewer))

    assert _names(response, ids) == expected
    assert response.get_json()["total"] == len(expected)


@pytest.mark.parametrize("role", ["student", "teacher"])
def test_source_user_narrows_to_own_and_friends_walls_whatever_the_role(client, world, make_user,
                                                                        auth_headers, role):
    viewer = make_user(role=role)
    ids = world(viewer)

    response = client.get(f"{FEED}?source=user&per_page=50", headers=auth_headers(viewer))

    assert _names(response, ids) == {"own_wall_public", "own_wall_private",
                                     "friend_wall_public", "friend_wall_private"}


def test_source_user_is_empty_for_a_guest(client, world, make_user):
    world(make_user())

    body = client.get(f"{FEED}?source=user").get_json()

    assert body["posts"] == []
    assert body["total"] == 0


def test_source_faculty_hides_another_staffs_draft_from_a_student(client, world, make_user,
                                                                  auth_headers):
    viewer = make_user(role="student")
    ids = world(viewer)

    response = client.get(f"{FEED}?source=faculty&per_page=50", headers=auth_headers(viewer))

    assert _names(response, ids) == {"public_faculty", "own_faculty_draft"}


def test_source_faculty_shows_staff_every_draft(client, world, make_user, auth_headers):
    # Neither extra clause is appended for staff under a
    # non-user ?source — the whole faculty stream, drafts included
    viewer = make_user(role="curator")
    ids = world(viewer)

    response = client.get(f"{FEED}?source=faculty&per_page=50", headers=auth_headers(viewer))

    assert _names(response, ids) == {"public_faculty", "draft_faculty", "own_faculty_draft"}


def test_source_faculty_shows_a_guest_only_the_published_one(client, world, make_user):
    ids = world(make_user())

    assert _names(client.get(f"{FEED}?source=faculty&per_page=50"), ids) == {"public_faculty"}


def test_source_app_hides_a_private_row_from_a_student(client, world, make_user, auth_headers):
    viewer = make_user(role="student")
    ids = world(viewer)

    response = client.get(f"{FEED}?source=app&per_page=50", headers=auth_headers(viewer))

    assert _names(response, ids) == {"public_app"}


def test_source_app_shows_staff_the_private_row_too(client, world, make_user, auth_headers):
    viewer = make_user(role="teacher")
    ids = world(viewer)

    response = client.get(f"{FEED}?source=app&per_page=50", headers=auth_headers(viewer))

    assert _names(response, ids) == {"public_app", "private_app"}


def test_the_first_source_parameter_wins(client, make_post):
    wanted = make_post(source="app")
    make_post(source="faculty")

    assert _ids(client.get(f"{FEED}?source=app&source=faculty")) == [wanted]


def test_the_source_whitelist_is_exactly_the_modules_tuple(client):
    error = client.get(f"{FEED}?source=twitter").get_json()["error"]

    assert error == f"source must be one of: {', '.join(SOURCES)}"




# -----------------------------------------------------------
# get_feed — the friend set behind the wall posts
# -----------------------------------------------------------


def test_a_member_with_no_friends_still_gets_their_own_wall_post(client, make_post, actor):
    user, headers = actor
    mine = make_post(source="user", post_type="social", is_public=0, author_id=user["id"])

    assert _ids(client.get(FEED, headers=headers)) == [mine]


def test_a_member_with_many_friends_gets_every_wall(client, make_post, make_user, link, actor):
    user, headers = actor
    expected = set()
    for _ in range(5):
        friend = make_user()
        link(user["id"], friend["id"])
        expected.add(make_post(source="user", post_type="social", is_public=0,
                               author_id=friend["id"]))

    assert set(_ids(client.get(f"{FEED}?per_page=50", headers=headers))) == expected


def test_a_friendship_row_pointing_the_other_way_is_not_enough(client, make_post, make_user,
                                                               link, actor):
    # The feed reads the caller's OWN direction, exactly as
    # _can_view_post does; social/routes.py writes both on accept
    user, headers = actor
    friend = make_user()
    link(friend["id"], user["id"], both=False)
    make_post(source="user", post_type="social", author_id=friend["id"])

    assert client.get(FEED, headers=headers).get_json()["posts"] == []


def test_a_self_friendship_row_does_not_duplicate_the_page(client, make_post, link, actor):
    user, headers = actor
    link(user["id"], user["id"], both=False)
    mine = make_post(source="user", post_type="social", author_id=user["id"])

    assert _ids(client.get(FEED, headers=headers)) == [mine]


def test_dropping_a_friendship_drops_their_wall_post(client, make_post, make_user, link, sql,
                                                     actor):
    user, headers = actor
    friend = make_user()
    link(user["id"], friend["id"])
    make_post(source="user", post_type="social", author_id=friend["id"])
    assert len(_ids(client.get(FEED, headers=headers))) == 1

    sql("DELETE FROM friendships WHERE user_id = ?", (user["id"],))

    assert client.get(FEED, headers=headers).get_json()["posts"] == []




# -----------------------------------------------------------
# get_feed — paging arithmetic and the guards around it
# -----------------------------------------------------------


@pytest.mark.parametrize("rows, per_page, page, served, has_more", [
    (0, 20, 1, 0, False),
    (1, 1, 1, 1, False),
    (3, 1, 1, 1, True),
    (3, 1, 3, 1, False),
    (3, 1, 4, 0, False),
    (3, 2, 1, 2, True),
    (3, 2, 2, 1, False),
    (3, 3, 1, 3, False),
    (3, 50, 1, 3, False),
])
def test_the_page_arithmetic_holds_at_every_boundary(client, make_post, rows, per_page, page,
                                                     served, has_more):
    for index in range(rows):
        make_post(source="faculty", days=-index)

    body = client.get(f"{FEED}?page={page}&per_page={per_page}").get_json()

    assert len(body["posts"]) == served
    assert body["total"] == rows
    assert body["hasMore"] is has_more
    assert (body["page"], body["perPage"]) == (page, per_page)


def test_the_smallest_page_walks_every_row_exactly_once(client, make_post):
    seeded = {make_post(source="faculty", days=-index) for index in range(7)}

    walked = [_ids(client.get(f"{FEED}?page={n}&per_page=1"))[0] for n in range(1, 8)]

    assert len(walked) == len(set(walked))
    assert set(walked) == seeded


def test_the_page_cap_and_the_per_page_cap_are_both_accepted(client, make_post):
    make_post(source="faculty")

    body = client.get(f"{FEED}?page=10000&per_page=50").get_json()

    assert body["posts"] == []
    assert body["total"] == 1
    assert body["hasMore"] is False


def test_an_empty_page_for_a_member_still_carries_the_envelope(client, make_post, actor):
    _, headers = actor
    make_post(source="faculty")

    body = client.get(f"{FEED}?page=9&per_page=1", headers=headers).get_json()

    assert body["posts"] == []
    assert body["total"] == 1


def test_a_full_per_page_of_fifty_is_served_whole(client, make_post):
    for index in range(51):
        make_post(source="faculty", days=-index)

    body = client.get(f"{FEED}?per_page=50").get_json()

    assert len(body["posts"]) == 50
    assert body["hasMore"] is True




# -----------------------------------------------------------
# get_feed — the ?before window
# -----------------------------------------------------------


def test_before_narrows_the_total_as_well_as_the_page(client, make_post):
    make_post(source="faculty", days=-3)
    make_post(source="faculty", days=3)

    body = client.get(FEED, query_string={"before": _iso()}).get_json()

    assert len(body["posts"]) == 1
    assert body["total"] == 1
    assert body["hasMore"] is False


def test_before_keeps_a_row_stamped_at_the_pin_and_drops_the_next_second(client, make_post):
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
    kept = make_post(source="faculty", published_at=base.isoformat())
    make_post(source="faculty", published_at=(base + timedelta(seconds=1)).isoformat())

    assert _ids(client.get(FEED, query_string={"before": base.isoformat()})) == [kept]


def test_a_before_far_in_the_past_empties_the_page_but_still_tags_it(client, make_post):
    make_post(source="faculty", days=-1)

    response = client.get(FEED, query_string={"before": "2000-01-01T00:00:00+00:00"})

    assert response.get_json()["total"] == 0
    assert response.get_json()["hasMore"] is False
    assert ETAG_RE.match(_tag(response))


def test_a_before_at_the_end_of_time_keeps_every_row(client, make_post):
    for index in range(3):
        make_post(source="faculty", days=index)

    body = client.get(FEED, query_string={"before": "9999-12-31T23:59:59+00:00"}).get_json()

    assert body["total"] == 3


@pytest.mark.parametrize("spelling", ["2030-01-01", "2030-01-01T00:00:00Z", "20300101"])
def test_before_accepts_the_shapes_a_query_string_can_carry(client, make_post, spelling):
    make_post(source="faculty", days=-1)

    response = client.get(FEED, query_string={"before": spelling})

    assert response.status_code == 200
    assert len(response.get_json()["posts"]) == 1


def test_before_and_a_source_filter_and_a_page_all_apply_at_once(client, make_post):
    for index in range(3):
        make_post(source="faculty", days=-index - 1)
    make_post(source="vu.lt", days=-1)
    make_post(source="faculty", days=5)

    body = client.get(FEED, query_string={"before": _iso(), "source": "faculty",
                                          "per_page": "2"}).get_json()

    assert body["total"] == 3
    assert len(body["posts"]) == 2
    assert body["hasMore"] is True


def test_an_unpinned_run_can_repeat_a_row_and_a_pinned_one_cannot(client, make_post):
    # The regression ?before exists for: a scraper insert between
    # two pages shifts the OFFSET window, so the row on the seam
    # is served twice. Pinning the run keeps the same two pages
    # disjoint and complete
    ordered = [make_post(source="faculty", days=-index - 1) for index in range(4)]
    pin = _iso()
    live_first = _ids(client.get(f"{FEED}?page=1&per_page=2"))
    pinned_first = _ids(client.get(FEED, query_string={"before": pin, "page": "1",
                                                       "per_page": "2"}))
    make_post(source="faculty", days=0.5)

    live_second = _ids(client.get(f"{FEED}?page=2&per_page=2"))
    pinned_second = _ids(client.get(FEED, query_string={"before": pin, "page": "2",
                                                        "per_page": "2"}))

    assert live_first == pinned_first == ordered[:2]
    assert set(live_first) & set(live_second) == {ordered[1]}
    assert pinned_second == ordered[2:]


def test_a_guest_page_stays_public_only_under_a_before_window(client, world, make_user):
    ids = world(make_user())

    response = client.get(FEED, query_string={"before": _iso(1), "per_page": "50"})

    assert _names(response, ids) == PUBLIC_ROWS




# -----------------------------------------------------------
# get_feed — the liked flag and the inline polls
# -----------------------------------------------------------


def test_the_liked_flag_is_set_per_card_not_per_page(client, make_post, sql, actor):
    user, headers = actor
    liked = make_post(source="faculty", days=-1)
    plain = make_post(source="faculty", days=-2)
    sql("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], liked))

    flags = {p["id"]: p["liked"] for p in client.get(FEED, headers=headers).get_json()["posts"]}

    assert flags == {liked: True, plain: False}


def test_another_members_like_never_flags_my_card(client, make_post, sql, make_user, actor):
    _, headers = actor
    other = make_user()
    post_id = make_post(source="faculty")
    sql("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (other["id"], post_id))

    assert client.get(FEED, headers=headers).get_json()["posts"][0]["liked"] is False


def test_a_guest_card_is_unliked_even_when_the_table_is_full(client, make_post, sql, make_user):
    other = make_user()
    post_id = make_post(source="faculty")
    sql("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (other["id"], post_id))

    assert client.get(FEED).get_json()["posts"][0]["liked"] is False


def test_a_private_poll_card_still_carries_its_poll_for_its_author(client, make_post, make_poll,
                                                                   actor):
    user, headers = actor
    post_id = make_post(source="faculty", is_public=0, author_id=user["id"])
    _, option_ids = make_poll(post_id, options=("Taip", "Ne", "Nezinau"))

    card = client.get(FEED, headers=headers).get_json()["posts"][0]

    assert [option["id"] for option in card["poll"]["options"]] == option_ids
    assert card["poll"]["userVote"] is None
    assert card["poll"]["totalVotes"] == 0


def test_only_the_poll_cards_of_a_mixed_page_carry_a_poll(client, make_post, make_poll):
    with_poll = make_post(source="faculty", days=-1)
    make_poll(with_poll)
    plain = make_post(source="faculty", days=-2)
    orphan = make_post(source="faculty", post_type="poll", days=-3)

    carried = {p["id"]: ("poll" in p) for p in client.get(FEED).get_json()["posts"]}

    assert carried == {with_poll: True, plain: False, orphan: False}
