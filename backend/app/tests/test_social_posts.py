# -----------------------------------------------------------
#  [*] Tests — social wall posts and the community feed
#
#  What this module proves about app/social/routes.py:
#
#    - create_post writes ONE shape: source 'user', post_type
#      'social', a snapshotted author name, a summary cut to
#      SUMMARY_LENGTH, and a 201 body produced by the same
#      _post_row_to_dict every read path uses. Every guard in
#      front of that insert is tripped here — non-string
#      content/title, blank content, the title and content
#      length limits, a non-boolean is_public, and the
#      image_url beacon guard that pins images to
#      /api/uploads/.
#    - the feed's visibility rule: a guest sees public wall
#      posts only, a signed-in reader ALSO sees their own and
#      their friends' private ones, and a stranger sees
#      neither. The friendship is read in the VIEWER's
#      direction, so a half-written friendship reveals
#      nothing.
#    - the two floors everybody pays: a deactivated author's
#      posts leave the feed entirely, and only the last
#      _FEED_WINDOW_DAYS days are ranked — while a profile's
#      own list stays unwindowed, so history never becomes
#      unreachable.
#    - the ranking contract (a brand-new post outranks an
#      older, more-liked one; engagement breaks ties), the
#      ?before pin that keeps OFFSET paging stable, and the
#      400 for an unparseable one.
#    - the paging caps: per_page over 50 and page over 200 are
#      400s, not silent clamps, and total/hasMore cover the
#      same visibility set as the page.
#    - delete authorisation: owner-only, someone else's post
#      is a 404 and not a 403, an admin gets no override, the
#      row's likes go with it through ON DELETE CASCADE, and
#      the post's own upload is removed best-effort — a
#      failing or missing upload helper must never fail the
#      route.
#    - the wire shape the mobile app consumes
#      (services/api/social.ts SocialFeedPost /
#      SocialFeedResponse), and that bodies are stored RAW and
#      escaped on output.
#
#  Everything is arranged either through the routes themselves
#  or with direct INSERTs on the `db` fixture, because most of
#  these states (an old post, a deactivated author, a
#  half-written friendship) no route can create.
# -----------------------------------------------------------

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

from app.api import MAX_CONTENT_LENGTH, MAX_TITLE_LENGTH, SUMMARY_LENGTH

FEED = "/api/social/feed"
POSTS = "/api/social/posts"

# A filename in the shape uploads/routes.py hands out — the
# per-route guard only checks the /api/uploads/ prefix, but the
# delete helper further down only unlinks names matching this
IMAGE_URL = "/api/uploads/" + "a" * 31 + "1.jpg"




# -----------------------------------------------------------
# _fresh_rate_limits
# -----------------------------------------------------------
#
# The limiter's store is a module-level dict that outlives the
# app fixture, so without this every request this file makes
# would count against the next test's global per-IP budget
# (600 per 5 minutes) and the suite would start 429ing halfway
# through. Cleared on both sides so neighbouring modules are
# no worse off either.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _seed_post
# -----------------------------------------------------------
#
# One news_posts row, inserted the way the scraper and the
# news blueprint do — the only way to arrange a post that is
# old, has engagement counters, belongs to a deactivated
# author or carries no author at all. Defaults describe a
# fresh public wall post.
# -----------------------------------------------------------

def _seed_post(db, author_id, content="Turinys", title="Antraste", public=True,
               source="user", post_type="social", published_at=None, likes=0,
               comments=0, shares=0, image_url=None, author_name="Autorius"):
    post_id = str(uuid.uuid4())
    stamp = published_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    db.execute(
        """INSERT INTO news_posts
           (id, title, content, summary, image_url, author_id, author_name, source,
            source_url, post_type, is_public, likes_count, comments_count, shares_count,
            published_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, title, content, content[:SUMMARY_LENGTH], image_url, author_id,
         author_name, source, post_type, 1 if public else 0, likes, comments, shares,
         stamp, stamp, stamp),
    )
    db.commit()
    return post_id




# -----------------------------------------------------------
# _befriend
# -----------------------------------------------------------
#
# The accepted-friendship state of record: ONE row per
# direction, which is what accept_friend_request writes. Pass
# both=False to arrange the half-written friendship a crash
# could leave behind.
# -----------------------------------------------------------

def _befriend(db, user_id, friend_id, both=True):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user_id, friend_id))
    if both:
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (friend_id, user_id))
    db.commit()




# -----------------------------------------------------------
# _ids / _days_ago
# -----------------------------------------------------------

def _ids(response):
    return [p["id"] for p in response.get_json()["posts"]]


def _days_ago(days, seconds=0):
    moment = datetime.now(timezone.utc) - timedelta(days=days, seconds=seconds)
    return moment.replace(microsecond=0).isoformat()




# -----------------------------------------------------------
# POST /api/social/posts — the wall-post insert
# -----------------------------------------------------------

def test_creating_a_wall_post_answers_the_stored_shape(client, actor):
    user, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Sveiki visi", "title": "Labas"})

    assert response.status_code == 201
    body = response.get_json()
    assert body["title"] == "Labas"
    assert body["content"] == "Sveiki visi"
    assert body["summary"] == "Sveiki visi"
    assert body["author"] == user["username"].title()
    assert body["authorId"] == user["id"]
    assert body["source"] == "user"
    assert body["sourceUrl"] is None
    assert body["postType"] == "social"
    assert body["isPublic"] is True
    assert body["liked"] is False
    assert body["truncated"] is False
    assert body["likes"] == 0 and body["comments"] == 0 and body["shares"] == 0


def test_a_created_post_is_persisted_as_a_user_wall_post(client, actor, db):
    user, headers = actor

    post_id = client.post(POSTS, headers=headers, json={"content": "Issaugota"}).get_json()["id"]

    row = db.execute("SELECT * FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert row["source"] == "user"
    assert row["post_type"] == "social"
    assert row["author_id"] == user["id"]
    assert row["author_name"] == user["username"].title()
    assert row["is_public"] == 1
    # published_at, created_at and updated_at all get the one stamp
    assert row["published_at"] == row["created_at"] == row["updated_at"]


def test_the_summary_is_cut_to_the_summary_length(client, actor, db):
    _, headers = actor
    long_body = "z" * (SUMMARY_LENGTH + 50)

    post_id = client.post(POSTS, headers=headers, json={"content": long_body}).get_json()["id"]

    row = db.execute("SELECT summary, content FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert row["summary"] == "z" * SUMMARY_LENGTH
    assert row["content"] == long_body


def test_the_created_post_body_is_not_truncated(client, actor):
    _, headers = actor
    long_body = "z" * (SUMMARY_LENGTH + 50)

    body = client.post(POSTS, headers=headers, json={"content": long_body}).get_json()

    assert body["content"] == long_body
    assert body["truncated"] is False


def test_creating_a_post_requires_authentication(client):
    response = client.post(POSTS, json={"content": "Anonimas"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_creating_a_post_without_a_body_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_creating_a_post_with_an_empty_object_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_json_array_body_cannot_reach_the_post_handler(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json=["content"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_non_string_content_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": 42})

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_a_blank_content_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "   \n\t "})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Post content required"


def test_a_missing_content_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"title": "Vien antraste"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Post content required"


def test_content_is_stored_stripped(client, actor, db):
    _, headers = actor

    post_id = client.post(POSTS, headers=headers, json={"content": "  apkarpyta  "}).get_json()["id"]

    assert db.execute("SELECT content FROM news_posts WHERE id = ?", (post_id,)).fetchone()["content"] == "apkarpyta"


def test_a_non_string_title_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "title": {"lt": "Labas"}})

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_a_missing_title_falls_back_to_the_head_of_the_content(client, actor):
    _, headers = actor
    body_text = "a" * 120

    body = client.post(POSTS, headers=headers, json={"content": body_text}).get_json()

    assert body["title"] == "a" * 80


def test_a_blank_title_falls_back_to_the_head_of_the_content(client, actor):
    _, headers = actor

    body = client.post(POSTS, headers=headers, json={"content": "Trumpas", "title": "   "}).get_json()

    assert body["title"] == "Trumpas"


def test_a_title_at_the_limit_is_accepted(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={
        "content": "Turinys",
        "title": "t" * MAX_TITLE_LENGTH,
    })

    assert response.status_code == 201


def test_a_title_over_the_limit_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={
        "content": "Turinys",
        "title": "t" * (MAX_TITLE_LENGTH + 1),
    })

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Title must be at most {MAX_TITLE_LENGTH} characters"


def test_content_at_the_limit_is_accepted(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "c" * MAX_CONTENT_LENGTH})

    assert response.status_code == 201


def test_content_over_the_limit_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "c" * (MAX_CONTENT_LENGTH + 1)})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Content must be at most {MAX_CONTENT_LENGTH} characters"


def test_a_string_is_public_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "is_public": "false"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "is_public must be a boolean"


def test_an_integer_is_public_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "is_public": 1})

    assert response.status_code == 400
    assert response.get_json()["error"] == "is_public must be a boolean"


def test_a_private_post_is_stored_and_echoed_as_private(client, actor, db):
    _, headers = actor

    body = client.post(POSTS, headers=headers, json={"content": "Tik draugams", "is_public": False}).get_json()

    assert body["isPublic"] is False
    assert db.execute("SELECT is_public FROM news_posts WHERE id = ?", (body["id"],)).fetchone()["is_public"] == 0


def test_an_absolute_image_url_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={
        "content": "Turinys",
        "image_url": "https://evil.example/beacon.png",
    })

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


def test_a_non_string_image_url_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "image_url": 7})

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


def test_a_relative_upload_image_url_is_pinned_to_the_post(client, actor, db):
    _, headers = actor

    body = client.post(POSTS, headers=headers, json={"content": "Su nuotrauka", "image_url": IMAGE_URL}).get_json()

    assert body["imageUrl"] == IMAGE_URL
    assert db.execute("SELECT image_url FROM news_posts WHERE id = ?", (body["id"],)).fetchone()["image_url"] == IMAGE_URL


def test_an_absent_image_url_stores_null(client, actor, db):
    _, headers = actor

    body = client.post(POSTS, headers=headers, json={"content": "Be nuotraukos"}).get_json()

    assert body["imageUrl"] is None
    assert db.execute("SELECT image_url FROM news_posts WHERE id = ?", (body["id"],)).fetchone()["image_url"] is None


def test_post_content_is_stored_raw_and_escaped_on_output(client, actor, db):
    _, headers = actor
    payload = '<script>alert("xss")</script>'
    # NOT client.post(json=...): the test client serialises that
    # through the app's OWN escaping JSON provider, so the payload
    # would already arrive escaped and prove nothing
    response = client.post(POSTS, headers=headers, content_type="application/json",
                           data=json.dumps({"content": payload}))

    body = response.get_json()
    stored = db.execute("SELECT content FROM news_posts WHERE id = ?", (body["id"],)).fetchone()["content"]
    assert stored == payload
    assert body["content"] == "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;"


def test_creating_posts_past_the_quota_is_rate_limited(client, actor):
    _, headers = actor

    for _ in range(20):
        assert client.post(POSTS, headers=headers, json={"content": "Turinys"}).status_code == 201

    response = client.post(POSTS, headers=headers, json={"content": "Vienas per daug"})

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1


def test_the_post_quota_reopens_after_the_rate_limit_window(client, actor):
    _, headers = actor
    start = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)

    with time_machine.travel(start, tick=False):
        for _ in range(20):
            client.post(POSTS, headers=headers, json={"content": "Turinys"})
        assert client.post(POSTS, headers=headers, json={"content": "Turinys"}).status_code == 429

    with time_machine.travel(start + timedelta(minutes=6), tick=False):
        assert client.post(POSTS, headers=headers, json={"content": "Vėl galima"}).status_code == 201




# -----------------------------------------------------------
# GET /api/social/feed — the guest / friends visibility rule
# -----------------------------------------------------------

def test_a_guest_sees_public_wall_posts(client, make_user, db):
    author = make_user()
    post_id = _seed_post(db, author["id"])

    response = client.get(FEED)

    assert response.status_code == 200
    assert _ids(response) == [post_id]


def test_a_guest_never_sees_a_private_wall_post(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"], public=False)

    response = client.get(FEED)

    assert _ids(response) == []
    assert response.get_json()["total"] == 0


def test_a_garbage_bearer_token_reads_the_feed_as_a_guest(client, make_user, db):
    author = make_user()
    public_id = _seed_post(db, author["id"])
    _seed_post(db, author["id"], public=False)

    response = client.get(FEED, headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 200
    assert _ids(response) == [public_id]


def test_a_reader_sees_their_own_private_post(client, actor, db):
    user, headers = actor
    private_id = _seed_post(db, user["id"], public=False)

    assert _ids(client.get(FEED, headers=headers)) == [private_id]


def test_a_friends_private_post_is_in_the_feed(client, actor, make_user, db):
    user, headers = actor
    friend = make_user()
    _befriend(db, user["id"], friend["id"])
    private_id = _seed_post(db, friend["id"], public=False)

    assert _ids(client.get(FEED, headers=headers)) == [private_id]


def test_a_strangers_private_post_stays_out_of_the_feed(client, actor, make_user, db):
    _, headers = actor
    stranger = make_user()
    _seed_post(db, stranger["id"], public=False)
    public_id = _seed_post(db, stranger["id"])

    assert _ids(client.get(FEED, headers=headers)) == [public_id]


def test_a_half_written_friendship_reveals_nothing_to_the_wrong_side(client, actor, make_user, db):
    user, headers = actor
    other = make_user()
    # Only the other user's direction was written — the feed reads
    # the VIEWER's, so the viewer must still see nothing private
    _befriend(db, other["id"], user["id"], both=False)
    _seed_post(db, other["id"], public=False)

    assert _ids(client.get(FEED, headers=headers)) == []


def test_a_signed_in_reader_still_sees_public_posts_of_strangers(client, actor, make_user, db):
    _, headers = actor
    stranger = make_user()
    public_id = _seed_post(db, stranger["id"])

    assert _ids(client.get(FEED, headers=headers)) == [public_id]




# -----------------------------------------------------------
# GET /api/social/feed — the floors everybody pays
# -----------------------------------------------------------

def test_only_user_source_posts_reach_the_community_feed(client, make_user, db):
    author = make_user()
    wall_id = _seed_post(db, author["id"])
    _seed_post(db, author["id"], source="knf.vu.lt", post_type="article")
    _seed_post(db, author["id"], source="faculty", post_type="announcement")

    assert _ids(client.get(FEED)) == [wall_id]


def test_a_deactivated_authors_posts_leave_the_feed(client, make_user, db):
    author = make_user(active=0)
    _seed_post(db, author["id"])

    response = client.get(FEED)

    assert _ids(response) == []
    assert response.get_json()["total"] == 0


def test_a_deactivated_friends_posts_leave_the_feed_too(client, actor, make_user, db):
    user, headers = actor
    friend = make_user(active=0)
    _befriend(db, user["id"], friend["id"])
    _seed_post(db, friend["id"], public=False)

    assert _ids(client.get(FEED, headers=headers)) == []


def test_an_authorless_post_survives_the_active_filter(client, db):
    # LEFT JOIN + COALESCE: a hand-inserted row with no author
    # must not be filtered out as "inactive"
    orphan_id = _seed_post(db, None, author_name="Nezinomas")

    body = client.get(FEED).get_json()

    assert [p["id"] for p in body["posts"]] == [orphan_id]
    assert body["posts"][0]["author"] == "Nezinomas"
    assert body["posts"][0]["authorAvatar"] is None


def test_a_post_older_than_the_feed_window_is_not_ranked(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"], published_at=_days_ago(181))

    assert _ids(client.get(FEED)) == []


def test_a_post_inside_the_feed_window_is_ranked(client, make_user, db):
    author = make_user()
    fresh_id = _seed_post(db, author["id"], published_at=_days_ago(179))

    assert _ids(client.get(FEED)) == [fresh_id]


def test_a_post_at_the_feed_window_edge_is_still_ranked(client, make_user, db):
    author = make_user()
    edge_id = _seed_post(db, author["id"], published_at=_days_ago(180))

    assert _ids(client.get(FEED)) == [edge_id]


def test_a_scraper_style_space_timestamp_still_ranks(client, make_user, db):
    # julianday() must cope with both shapes in published_at:
    # create_post's ISO "T" and the scraper's datetime('now')
    author = make_user()
    space_form = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    post_id = _seed_post(db, author["id"], published_at=space_form)

    body = client.get(FEED).get_json()

    assert [p["id"] for p in body["posts"]] == [post_id]




# -----------------------------------------------------------
# GET /api/social/feed — ranking, liked flags, wire shape
# -----------------------------------------------------------

def test_a_brand_new_post_outranks_an_older_more_liked_one(client, make_user, db):
    author = make_user()
    old_id = _seed_post(db, author["id"], published_at=_days_ago(5), likes=500)
    new_id = _seed_post(db, author["id"])

    assert _ids(client.get(FEED)) == [new_id, old_id]


def test_engagement_breaks_the_tie_between_two_equally_old_posts(client, make_user, db):
    author = make_user()
    stamp = _days_ago(1)
    quiet_id = _seed_post(db, author["id"], published_at=stamp)
    busy_id = _seed_post(db, author["id"], published_at=stamp, likes=10, comments=4, shares=2)

    assert _ids(client.get(FEED)) == [busy_id, quiet_id]


def test_the_liked_flag_is_filled_for_the_viewer(client, actor, make_user, db):
    user, headers = actor
    author = make_user()
    liked_id = _seed_post(db, author["id"], published_at=_days_ago(0, 60))
    plain_id = _seed_post(db, author["id"], published_at=_days_ago(1))
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], liked_id))
    db.commit()

    posts = {p["id"]: p["liked"] for p in client.get(FEED, headers=headers).get_json()["posts"]}

    assert posts == {liked_id: True, plain_id: False}


def test_a_guest_always_gets_liked_false(client, make_user, db):
    author = make_user()
    post_id = _seed_post(db, author["id"])
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (author["id"], post_id))
    db.commit()

    assert client.get(FEED).get_json()["posts"][0]["liked"] is False


def test_feed_bodies_are_trimmed_with_a_truncated_flag(client, make_user, db):
    author = make_user()
    long_body = "x" * (SUMMARY_LENGTH + 300)
    _seed_post(db, author["id"], content=long_body)

    post = client.get(FEED).get_json()["posts"][0]

    assert post["content"] == "x" * SUMMARY_LENGTH
    assert post["truncated"] is True
    assert post["summary"] == "x" * SUMMARY_LENGTH


def test_a_short_feed_body_is_not_flagged_as_truncated(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"], content="Trumpas irasas")

    post = client.get(FEED).get_json()["posts"][0]

    assert post["content"] == "Trumpas irasas"
    assert post["truncated"] is False


def test_a_body_exactly_at_the_summary_length_is_not_truncated(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"], content="y" * SUMMARY_LENGTH)

    post = client.get(FEED).get_json()["posts"][0]

    assert post["truncated"] is False
    assert post["content"] == "y" * SUMMARY_LENGTH


def test_the_feed_author_is_the_current_display_name_not_the_snapshot(client, make_user, db):
    author = make_user(display_name="Nauja Pavarde")
    _seed_post(db, author["id"], author_name="Sena Pavarde")

    assert client.get(FEED).get_json()["posts"][0]["author"] == "Nauja Pavarde"


def test_the_feed_carries_the_authors_avatar(client, make_user, db):
    author = make_user()
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (IMAGE_URL, author["id"]))
    db.commit()
    _seed_post(db, author["id"])

    assert client.get(FEED).get_json()["posts"][0]["authorAvatar"] == IMAGE_URL


@pytest.mark.contract
def test_the_feed_page_shape_is_the_one_the_app_consumes(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"])

    body = client.get(FEED).get_json()

    assert set(body) == {"posts", "page", "perPage", "total", "hasMore"}
    assert set(body["posts"][0]) == {
        "id", "title", "content", "summary", "imageUrl", "author", "authorId",
        "authorAvatar", "source", "sourceUrl", "postType", "likes", "comments",
        "shares", "date", "isPublic", "liked", "truncated",
    }


@pytest.mark.contract
def test_an_empty_feed_still_answers_the_full_envelope(client):
    body = client.get(FEED).get_json()

    assert body == {"posts": [], "page": 1, "perPage": 20, "total": 0, "hasMore": False}




# -----------------------------------------------------------
# GET /api/social/feed — paging caps and the ?before pin
# -----------------------------------------------------------

def test_the_feed_pages_and_reports_has_more(client, make_user, db):
    author = make_user()
    for day in range(3):
        _seed_post(db, author["id"], published_at=_days_ago(day))

    first = client.get(f"{FEED}?per_page=2").get_json()
    second = client.get(f"{FEED}?per_page=2&page=2").get_json()

    assert first["total"] == 3 and first["hasMore"] is True and len(first["posts"]) == 2
    assert second["total"] == 3 and second["hasMore"] is False and len(second["posts"]) == 1
    assert second["page"] == 2 and second["perPage"] == 2


def test_the_feed_total_covers_only_the_visible_set(client, actor, make_user, db):
    _, headers = actor
    stranger = make_user()
    _seed_post(db, stranger["id"])
    _seed_post(db, stranger["id"], public=False)

    assert client.get(FEED, headers=headers).get_json()["total"] == 1


def test_a_per_page_over_the_feed_cap_is_refused(client):
    response = client.get(f"{FEED}?per_page=51")

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be at most 50"


def test_a_per_page_at_the_feed_cap_is_accepted(client):
    response = client.get(f"{FEED}?per_page=50")

    assert response.status_code == 200
    assert response.get_json()["perPage"] == 50


def test_a_page_over_the_feed_cap_is_refused(client):
    response = client.get(f"{FEED}?page=201")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 200"


def test_a_page_at_the_feed_cap_is_accepted(client):
    response = client.get(f"{FEED}?page=200")

    assert response.status_code == 200
    assert response.get_json()["posts"] == []


def test_a_zero_page_is_refused(client):
    response = client.get(f"{FEED}?page=0")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"


def test_a_non_numeric_per_page_is_refused(client):
    response = client.get(f"{FEED}?per_page=daug")

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be a positive integer"


def test_before_pins_the_feed_to_an_instant(client, make_user, db):
    author = make_user()
    older_id = _seed_post(db, author["id"], published_at=_days_ago(0, 7200))
    _seed_post(db, author["id"], published_at=_days_ago(0, 60))
    pin = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()

    body = client.get(FEED, query_string={"before": pin}).get_json()

    assert [p["id"] for p in body["posts"]] == [older_id]
    assert body["total"] == 1


def test_a_z_suffixed_before_is_accepted(client, make_user, db):
    author = make_user()
    post_id = _seed_post(db, author["id"], published_at=_days_ago(1))
    pin = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"

    assert _ids(client.get(FEED, query_string={"before": pin})) == [post_id]


def test_a_naive_before_is_read_as_utc(client, make_user, db):
    author = make_user()
    post_id = _seed_post(db, author["id"], published_at=_days_ago(1))
    pin = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat()

    assert _ids(client.get(FEED, query_string={"before": pin})) == [post_id]


def test_a_before_offset_arriving_as_a_space_is_still_accepted(client, make_user, db):
    # An un-encoded "+" in a query string decodes to a space —
    # answering 400 for a correct timestamp would be a trap
    author = make_user()
    post_id = _seed_post(db, author["id"], published_at=_days_ago(1))
    pin = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+", " ")

    assert _ids(client.get(FEED, query_string={"before": pin})) == [post_id]


def test_a_before_in_another_offset_is_converted_to_utc(client, make_user, db):
    author = make_user()
    older_id = _seed_post(db, author["id"], published_at=_days_ago(0, 7200))
    _seed_post(db, author["id"], published_at=_days_ago(0, 60))
    # 01:00 in +03:00 is 22:00 UTC the previous day — an hour ago,
    # so only the two-hour-old post may survive the pin
    pin = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0)
    shifted = pin.astimezone(timezone(timedelta(hours=3))).isoformat()

    assert _ids(client.get(FEED, query_string={"before": shifted})) == [older_id]


def test_an_unparseable_before_is_refused(client):
    response = client.get(FEED, query_string={"before": "vakar"})

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_before"


def test_an_empty_before_is_refused(client):
    response = client.get(FEED, query_string={"before": ""})

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_before"


def test_before_keeps_a_new_post_from_shifting_the_second_page(client, make_user, db):
    author = make_user()
    seeded = [_seed_post(db, author["id"], published_at=_days_ago(0, 600 * (i + 1))) for i in range(3)]
    pin = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    first = client.get(FEED, query_string={"before": pin, "per_page": 2}).get_json()
    # The scraper inserts between the two page requests, stamped
    # after the client's page-1 instant
    intruder = _seed_post(db, author["id"], published_at=_days_ago(0, -60))
    second = client.get(FEED, query_string={"before": pin, "per_page": 2, "page": 2}).get_json()

    assert [p["id"] for p in first["posts"]] == seeded[:2]
    assert [p["id"] for p in second["posts"]] == seeded[2:]
    # ...while the unpinned feed does rank the newcomer first
    assert _ids(client.get(FEED))[0] == intruder




# -----------------------------------------------------------
# GET /api/social/posts — one user's wall
# -----------------------------------------------------------

def test_the_post_list_requires_a_user_id(client):
    response = client.get(POSTS)

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id query param required"


def test_the_post_list_of_an_unknown_user_is_404(client):
    response = client.get(POSTS, query_string={"user_id": "nera-tokio"})

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_the_post_list_of_a_deactivated_user_is_404(client, make_user, db):
    gone = make_user(active=0)
    _seed_post(db, gone["id"])

    response = client.get(POSTS, query_string={"user_id": gone["id"]})

    assert response.status_code == 404


def test_an_admin_still_reads_a_deactivated_users_posts(client, admin, make_user, db):
    _, headers = admin
    gone = make_user(active=0)
    post_id = _seed_post(db, gone["id"])

    response = client.get(POSTS, query_string={"user_id": gone["id"]}, headers=headers)

    assert response.status_code == 200
    assert _ids(response) == [post_id]


def test_a_guest_sees_only_the_public_posts_of_a_user(client, make_user, db):
    author = make_user()
    public_id = _seed_post(db, author["id"])
    _seed_post(db, author["id"], public=False)

    response = client.get(POSTS, query_string={"user_id": author["id"]})

    assert _ids(response) == [public_id]
    assert response.get_json()["total"] == 1


def test_the_author_sees_their_own_private_posts(client, actor, db):
    user, headers = actor
    public_id = _seed_post(db, user["id"], published_at=_days_ago(0, 60))
    private_id = _seed_post(db, user["id"], public=False, published_at=_days_ago(1))

    response = client.get(POSTS, query_string={"user_id": user["id"]}, headers=headers)

    assert _ids(response) == [public_id, private_id]


def test_a_friend_sees_the_private_posts_of_a_user(client, actor, make_user, db):
    user, headers = actor
    friend = make_user()
    _befriend(db, user["id"], friend["id"])
    private_id = _seed_post(db, friend["id"], public=False)

    assert _ids(client.get(POSTS, query_string={"user_id": friend["id"]}, headers=headers)) == [private_id]


def test_a_stranger_does_not_see_the_private_posts_of_a_user(client, actor, make_user, db):
    _, headers = actor
    stranger = make_user()
    _seed_post(db, stranger["id"], public=False)

    response = client.get(POSTS, query_string={"user_id": stranger["id"]}, headers=headers)

    assert _ids(response) == []
    assert response.get_json()["total"] == 0


def test_faculty_announcements_appear_on_their_authors_wall(client, make_user, db):
    author = make_user(role="teacher")
    wall_id = _seed_post(db, author["id"], published_at=_days_ago(0, 60))
    faculty_id = _seed_post(db, author["id"], source="faculty", post_type="announcement",
                            published_at=_days_ago(1))
    _seed_post(db, author["id"], source="knf.vu.lt", post_type="article", published_at=_days_ago(2))

    assert _ids(client.get(POSTS, query_string={"user_id": author["id"]})) == [wall_id, faculty_id]


def test_the_post_list_is_newest_first(client, make_user, db):
    author = make_user()
    oldest = _seed_post(db, author["id"], published_at=_days_ago(3))
    middle = _seed_post(db, author["id"], published_at=_days_ago(2))
    newest = _seed_post(db, author["id"], published_at=_days_ago(1))

    assert _ids(client.get(POSTS, query_string={"user_id": author["id"]})) == [newest, middle, oldest]


def test_the_post_list_has_no_recency_window(client, make_user, db):
    # The feed's window bounds the ranked sort; a profile must
    # still reach its own history
    author = make_user()
    ancient_id = _seed_post(db, author["id"], published_at=_days_ago(400))

    assert _ids(client.get(FEED)) == []
    assert _ids(client.get(POSTS, query_string={"user_id": author["id"]})) == [ancient_id]


def test_the_post_list_fills_the_liked_flag(client, actor, db):
    user, headers = actor
    liked_id = _seed_post(db, user["id"], published_at=_days_ago(0, 60))
    plain_id = _seed_post(db, user["id"], published_at=_days_ago(1))
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], liked_id))
    db.commit()

    posts = {p["id"]: p["liked"] for p in
             client.get(POSTS, query_string={"user_id": user["id"]}, headers=headers).get_json()["posts"]}

    assert posts == {liked_id: True, plain_id: False}


def test_the_post_list_trims_bodies_like_the_feed(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"], content="w" * (SUMMARY_LENGTH + 10))

    post = client.get(POSTS, query_string={"user_id": author["id"]}).get_json()["posts"][0]

    assert post["content"] == "w" * SUMMARY_LENGTH
    assert post["truncated"] is True


def test_the_post_list_pages_and_reports_has_more(client, make_user, db):
    author = make_user()
    for day in range(3):
        _seed_post(db, author["id"], published_at=_days_ago(day))

    first = client.get(POSTS, query_string={"user_id": author["id"], "per_page": 2}).get_json()
    second = client.get(POSTS, query_string={"user_id": author["id"], "per_page": 2, "page": 2}).get_json()

    assert first["hasMore"] is True and first["total"] == 3
    assert second["hasMore"] is False and len(second["posts"]) == 1


def test_a_per_page_over_the_cap_is_refused_on_the_post_list(client, make_user):
    author = make_user()

    response = client.get(POSTS, query_string={"user_id": author["id"], "per_page": 51})

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be at most 50"


def test_a_page_over_the_cap_is_refused_on_the_post_list(client, make_user):
    author = make_user()

    response = client.get(POSTS, query_string={"user_id": author["id"], "page": 201})

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 200"


@pytest.mark.contract
def test_the_post_list_answers_the_same_envelope_as_the_feed(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"])

    body = client.get(POSTS, query_string={"user_id": author["id"]}).get_json()

    assert set(body) == {"posts", "page", "perPage", "total", "hasMore"}
    assert set(body["posts"][0]) == {
        "id", "title", "content", "summary", "imageUrl", "author", "authorId",
        "authorAvatar", "source", "sourceUrl", "postType", "likes", "comments",
        "shares", "date", "isPublic", "liked", "truncated",
    }




# -----------------------------------------------------------
# DELETE /api/social/posts/<id> — owner only
# -----------------------------------------------------------

def test_deleting_an_own_post_removes_the_row(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.delete(f"{POSTS}/{post_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted"}
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 0


def test_deleting_someone_elses_post_is_a_404_and_keeps_the_row(client, actor, make_user, db):
    _, headers = actor
    stranger = make_user()
    post_id = _seed_post(db, stranger["id"])

    response = client.delete(f"{POSTS}/{post_id}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found or not yours"
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 1


def test_an_admin_gets_no_override_on_someone_elses_wall_post(client, admin, make_user, db):
    _, headers = admin
    author = make_user()
    post_id = _seed_post(db, author["id"])

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 404


def test_deleting_an_unknown_post_is_404(client, actor):
    _, headers = actor

    assert client.delete(f"{POSTS}/nera-tokio", headers=headers).status_code == 404


def test_deleting_a_post_requires_authentication(client, make_user, db):
    author = make_user()
    post_id = _seed_post(db, author["id"])

    response = client.delete(f"{POSTS}/{post_id}")

    assert response.status_code == 401


def test_a_scraped_post_cannot_be_deleted_through_the_social_route(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], source="knf.vu.lt", post_type="article")

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 404


def test_an_author_deletes_their_own_faculty_announcement(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], source="faculty", post_type="announcement")

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200


def test_deleting_a_post_cascades_its_likes(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], post_id))
    db.commit()

    client.delete(f"{POSTS}/{post_id}", headers=headers)

    assert db.execute("SELECT COUNT(*) AS c FROM news_likes WHERE post_id = ?", (post_id,)).fetchone()["c"] == 0


def test_a_deleted_post_leaves_the_feed(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    client.delete(f"{POSTS}/{post_id}", headers=headers)

    assert _ids(client.get(FEED)) == []


def test_deleting_a_post_removes_its_uploaded_image(client, app, actor, db, monkeypatch):
    user, headers = actor
    # uploads/routes.py caches the resolved directory in a
    # process global, so it must be re-resolved against THIS
    # test's temp UPLOAD_DIR
    monkeypatch.setattr("app.uploads.routes._upload_dir", None)
    name = "b" * 32 + ".png"
    path = os.path.join(app.config["UPLOAD_DIR"], name)
    with open(path, "wb") as handle:
        handle.write(b"fake-png")
    post_id = _seed_post(db, user["id"], image_url=f"/api/uploads/{name}")

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200
    assert not os.path.exists(path)


def test_a_failing_upload_delete_does_not_fail_the_route(client, actor, db, monkeypatch):
    user, headers = actor
    post_id = _seed_post(db, user["id"], image_url=IMAGE_URL)

    def _boom(path):
        raise OSError("disk on fire")

    monkeypatch.setattr("app.uploads.routes.delete_upload", _boom)

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 0


def test_a_missing_uploads_helper_is_a_silent_no_op(client, actor, db, monkeypatch):
    user, headers = actor
    post_id = _seed_post(db, user["id"], image_url=IMAGE_URL)
    # A None entry in sys.modules makes the guarded lazy import
    # raise ImportError, which is the "uploads has not landed yet"
    # branch
    monkeypatch.setitem(sys.modules, "app.uploads.routes", None)

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200


def test_a_foreign_image_path_is_never_handed_to_the_upload_deleter(client, actor, db, monkeypatch):
    user, headers = actor
    post_id = _seed_post(db, user["id"])
    # Only /api/uploads/ paths belong to us; the row above has
    # none at all, so the helper must not even be reached
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda path: calls.append(path))

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200
    assert calls == []




# -----------------------------------------------------------
# PUT /api/social/posts/<id> — the (unused) edit route
# -----------------------------------------------------------

def test_editing_an_own_post_rewrites_content_and_summary(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], content="Senas")

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Naujas turinys"})

    assert response.status_code == 200
    assert response.get_json() == {"status": "updated"}
    row = db.execute("SELECT content, summary FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert row["content"] == "Naujas turinys"
    assert row["summary"] == "Naujas turinys"


def test_editing_never_re_ranks_the_post(client, actor, db):
    user, headers = actor
    stamp = _days_ago(3)
    post_id = _seed_post(db, user["id"], published_at=stamp)

    client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Pataisyta"})

    assert db.execute("SELECT published_at FROM news_posts WHERE id = ?", (post_id,)).fetchone()["published_at"] == stamp


def test_editing_someone_elses_post_is_a_404(client, actor, make_user, db):
    _, headers = actor
    stranger = make_user()
    post_id = _seed_post(db, stranger["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Isilauziu"})

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found or not yours"


def test_editing_requires_a_json_body(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_editing_requires_at_least_one_known_field(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"nesamone": 1})

    assert response.status_code == 400
    assert response.get_json()["error"] == "No fields to update"


def test_editing_with_a_non_string_content_is_refused(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": []})

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_editing_to_a_blank_content_is_refused(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], content="Senas")

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "  "})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Post content required"
    assert db.execute("SELECT content FROM news_posts WHERE id = ?", (post_id,)).fetchone()["content"] == "Senas"


def test_editing_over_the_content_limit_is_refused(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers,
                          json={"content": "c" * (MAX_CONTENT_LENGTH + 1)})

    assert response.status_code == 400


def test_editing_with_a_non_string_title_is_refused(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"title": 5})

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_editing_over_the_title_limit_is_refused(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers,
                          json={"title": "t" * (MAX_TITLE_LENGTH + 1)})

    assert response.status_code == 400


def test_a_blank_title_falls_back_to_the_head_of_the_new_content(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], content="Senas")
    new_body = "n" * 120

    client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": new_body, "title": " "})

    assert db.execute("SELECT title FROM news_posts WHERE id = ?", (post_id,)).fetchone()["title"] == "n" * 80


def test_a_blank_title_alone_falls_back_to_the_stored_content(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], content="s" * 120)

    client.put(f"{POSTS}/{post_id}", headers=headers, json={"title": ""})

    assert db.execute("SELECT title FROM news_posts WHERE id = ?", (post_id,)).fetchone()["title"] == "s" * 80


def test_editing_an_image_to_an_absolute_url_is_refused(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers,
                          json={"image_url": "http://evil.example/x.png"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


def test_editing_can_clear_the_image(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], image_url=IMAGE_URL)

    assert client.put(f"{POSTS}/{post_id}", headers=headers, json={"image_url": None}).status_code == 200
    assert db.execute("SELECT image_url FROM news_posts WHERE id = ?", (post_id,)).fetchone()["image_url"] is None


def test_editing_stamps_updated_at_without_touching_the_title(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], title="Antraste", published_at=_days_ago(2))

    client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Kitas turinys"})

    row = db.execute("SELECT title, updated_at, created_at FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert row["title"] == "Antraste"
    assert row["updated_at"] > row["created_at"]


def test_editing_a_post_requires_authentication(client, make_user, db):
    author = make_user()
    post_id = _seed_post(db, author["id"])

    assert client.put(f"{POSTS}/{post_id}", json={"content": "Ne mano"}).status_code == 401




# -----------------------------------------------------------
# The post-shaped corners of the profile routes
# -----------------------------------------------------------
#
# postCount applies the SAME visibility split as GET /posts —
# the stat has to match the list the profile screen renders
# next to it — and a rename has to carry the author_name
# snapshot on the user's posts with it. The rest of the
# profile and friendship surface belongs to the sibling
# modules; only the post-facing half is proved here.
# -----------------------------------------------------------

def test_the_profile_post_count_hides_private_posts_from_a_guest(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"])
    _seed_post(db, author["id"], public=False)

    assert client.get(f"/api/social/profile/{author['id']}").get_json()["postCount"] == 1


def test_the_profile_post_count_includes_own_private_posts(client, actor, db):
    user, headers = actor
    _seed_post(db, user["id"])
    _seed_post(db, user["id"], public=False)

    assert client.get(f"/api/social/profile/{user['id']}", headers=headers).get_json()["postCount"] == 2


def test_the_profile_post_count_includes_a_friends_private_posts(client, actor, make_user, db):
    user, headers = actor
    friend = make_user()
    _befriend(db, user["id"], friend["id"])
    _seed_post(db, friend["id"], public=False)

    assert client.get(f"/api/social/profile/{friend['id']}", headers=headers).get_json()["postCount"] == 1


def test_the_profile_post_count_covers_faculty_but_not_scraped_posts(client, make_user, db):
    author = make_user(role="teacher")
    _seed_post(db, author["id"])
    _seed_post(db, author["id"], source="faculty", post_type="announcement")
    _seed_post(db, author["id"], source="knf.vu.lt", post_type="article")

    assert client.get(f"/api/social/profile/{author['id']}").get_json()["postCount"] == 2


def test_the_own_profile_post_count_counts_every_own_post(client, actor, db):
    user, headers = actor
    _seed_post(db, user["id"])
    _seed_post(db, user["id"], public=False)
    _seed_post(db, user["id"], source="faculty", post_type="announcement")

    assert client.get("/api/social/profile", headers=headers).get_json()["postCount"] == 3


def test_a_rename_rewrites_the_author_name_snapshot_on_own_posts(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], author_name="Sena Pavarde")

    assert client.put("/api/social/profile", headers=headers,
                      json={"displayName": "Nauja Pavarde"}).status_code == 200

    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["author_name"] == "Nauja Pavarde"
    assert client.get(FEED).get_json()["posts"][0]["author"] == "Nauja Pavarde"


def test_a_rename_leaves_other_authors_posts_alone(client, actor, make_user, db):
    _, headers = actor
    stranger = make_user()
    post_id = _seed_post(db, stranger["id"], author_name="Svetimas")

    client.put("/api/social/profile", headers=headers, json={"displayName": "Nauja Pavarde"})

    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["author_name"] == "Svetimas"
