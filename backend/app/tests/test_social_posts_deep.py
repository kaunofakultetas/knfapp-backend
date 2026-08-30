# -----------------------------------------------------------
#  [*] Tests — wall posts, the exhaustive pass
#
#  The gap-closing sweep over ONE slice of
#  app/social/routes.py — four routes and the image reaper
#  they share:
#
#    create_post          POST   /api/social/posts
#    get_user_posts       GET    /api/social/posts?user_id=
#    update_post          PUT    /api/social/posts/<id>
#    delete_post          DELETE /api/social/posts/<id>
#    _delete_upload_file  the best-effort upload reaper
#
#  test_social_posts.py already walks the happy paths and the
#  headline guards; this module takes every remaining arm:
#
#    - the body gate in three layers — the before_request hook
#      (a top-level array or scalar never reaches a handler),
#      get_json_object (malformed, form-encoded and JSON null
#      all read as "no body"), and the handlers' own falsy
#      check, which makes {} a "JSON body required" and not a
#      "No fields to update".
#    - every type and boundary a client can put in a field:
#      bool/int/None/list where a string belongs, blank and
#      unicode-whitespace bodies, NUL bytes, the title and
#      content limits at exactly max and max+1, and the order
#      the guards fire in (title before content, ownership
#      before field validation, body before ownership).
#    - the image_url beacon guard on both sides: both refuse
#      every non-string, falsy ones included, null and ""
#      still clear it, and the prefix match is exact — case,
#      leading slash and all.
#    - the wall's visibility matrix: guest, invalid token,
#      author, friend, reverse-only friendship, stranger,
#      admin, and admin-on-a-deactivated-account, crossed with
#      public / private and source 'user' / 'faculty' /
#      scraped.
#    - paging arithmetic: the caps at 200 pages and 50 per
#      page, int()'s "+1" and " 2 " quirks, a page past the
#      end, hasMore exactly on the boundary, and that a paged
#      walk neither drops nor repeats a row.
#    - the two quotas: create and edit share one "post"
#      budget of 20 per user, delete has its own 40, and
#      neither leaks into the other or across users.
#    - the reaper's contract: only /api/uploads/ paths, a
#      missing uploads package is a no-op, a raising helper is
#      logged and swallowed, and a traversal path cannot
#      unlink a file outside the upload directory.
#
#  Bodies that carry markup are posted as RAW bytes: the app's
#  JSON provider html-escapes everything it serialises, so a
#  `json=` kwarg would put an already-escaped string on the
#  wire (TESTPLAN rule 10).
# -----------------------------------------------------------

import json
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

from app.api import MAX_CONTENT_LENGTH, MAX_TITLE_LENGTH, SUMMARY_LENGTH
from app.social.routes import _delete_upload_file

POSTS = "/api/social/posts"

# A name shaped the way uploads/routes.py hands them out — 32
# characters plus an extension, the only shape its delete
# helper will unlink
IMAGE_URL = "/api/uploads/" + "c" * 31 + "7.jpg"




# -----------------------------------------------------------
# _fresh_rate_limits
# -----------------------------------------------------------
#
# The limiter's store is a module-level dict that outlives the
# app fixture, so the quota tests below would otherwise spend
# the next test's budget (and the global 600-per-IP one).
# Cleared on both sides so neighbouring modules are no worse
# off either.
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
# One news_posts row inserted the way the scraper and the news
# blueprint do — the only way to arrange a post that is old,
# private, scraped, engagement-laden or authored by a
# deactivated account. Defaults describe a fresh public wall
# post.
# -----------------------------------------------------------

def _seed_post(db, author_id, content="Turinys", title="Antraste", public=True,
               source="user", post_type="social", published_at=None, likes=0,
               comments=0, shares=0, image_url=None, author_name="Autorius",
               post_id=None):
    post_id = post_id or str(uuid.uuid4())
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
# both=False for the half-written friendship a crash leaves
# behind — every reader here checks the VIEWER's direction
# only.
# -----------------------------------------------------------

def _befriend(db, user_id, friend_id, both=True):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user_id, friend_id))
    if both:
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (friend_id, user_id))
    db.commit()




# -----------------------------------------------------------
# _post_raw / _put_raw
# -----------------------------------------------------------
#
# The body exactly as a real client sends it. Flask's test
# client serialises a `json=` kwarg through app.json.dumps,
# which html-escapes every string on the way out, so anything
# asserting about markup, entities or round-tripped content
# has to put the bytes on the wire itself (TESTPLAN rule 10).
# -----------------------------------------------------------

def _post_raw(client, payload, headers=None, path=POSTS):
    return client.post(path, data=json.dumps(payload),
                       headers={**(headers or {}), "Content-Type": "application/json"})


def _put_raw(client, path, payload, headers=None):
    return client.put(path, data=json.dumps(payload),
                      headers={**(headers or {}), "Content-Type": "application/json"})




# -----------------------------------------------------------
# _wall / _ids
# -----------------------------------------------------------

def _wall(client, user_id, headers=None, **params):
    query = "&".join([f"user_id={user_id}"] + [f"{k}={v}" for k, v in params.items()])
    return client.get(f"{POSTS}?{query}", headers=headers or {})


def _ids(response):
    return [p["id"] for p in response.get_json()["posts"]]




# -----------------------------------------------------------
# POST /api/social/posts — the three-layer body gate
# -----------------------------------------------------------

def test_a_top_level_json_array_never_reaches_the_create_handler(client, actor):
    _, headers = actor

    response = client.post(POSTS, data="[1, 2]", headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_top_level_json_string_never_reaches_the_create_handler(client, actor):
    _, headers = actor

    response = client.post(POSTS, data='"labas"', headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_top_level_json_number_never_reaches_the_create_handler(client, actor):
    _, headers = actor

    response = client.post(POSTS, data="42", headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_json_null_body_reads_as_a_missing_body(client, actor):
    _, headers = actor

    response = client.post(POSTS, data="null", headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_malformed_json_body_reads_as_a_missing_body(client, actor):
    _, headers = actor

    response = client.post(POSTS, data="{oi", headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_a_form_encoded_body_reads_as_a_missing_body(client, actor):
    _, headers = actor

    response = client.post(POSTS, data={"content": "Formos laukas"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"




# -----------------------------------------------------------
# POST /api/social/posts — content and title types
# -----------------------------------------------------------

def test_a_boolean_content_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": True})

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_a_numeric_zero_content_is_refused_as_a_non_string(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": 0})

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_an_explicit_null_content_is_refused_as_missing_not_as_a_type(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": None})

    assert response.status_code == 400
    assert response.get_json()["error"] == "Post content required"


def test_a_content_of_unicode_whitespace_only_is_refused(client, actor):
    _, headers = actor

    response = _post_raw(client, {"content": " \t\n\r "}, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Post content required"


def test_a_content_of_null_bytes_only_is_refused_after_the_hook_strips_them(client, actor):
    _, headers = actor

    response = _post_raw(client, {"content": "\x00\x00"}, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Post content required"


def test_null_bytes_are_stripped_out_of_a_stored_body(client, actor, db):
    _, headers = actor

    post_id = _post_raw(client, {"content": "a\x00b"}, headers).get_json()["id"]

    assert db.execute("SELECT content FROM news_posts WHERE id = ?", (post_id,)).fetchone()["content"] == "ab"


def test_a_boolean_title_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "title": False})

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_a_numeric_title_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "title": 7})

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_an_explicit_null_title_falls_back_to_the_head_of_the_content(client, actor):
    _, headers = actor

    body = client.post(POSTS, headers=headers, json={"content": "Turinys", "title": None}).get_json()

    assert body["title"] == "Turinys"




# -----------------------------------------------------------
# POST /api/social/posts — the length boundaries
# -----------------------------------------------------------

def test_a_content_of_exactly_eighty_characters_becomes_the_whole_title(client, actor):
    _, headers = actor
    content = "a" * 80

    body = client.post(POSTS, headers=headers, json={"content": content}).get_json()

    assert body["title"] == content


def test_a_content_of_eighty_one_characters_gives_an_eighty_character_title(client, actor):
    _, headers = actor
    content = "a" * 81

    body = client.post(POSTS, headers=headers, json={"content": content}).get_json()

    assert body["title"] == "a" * 80
    assert body["content"] == content


def test_a_title_is_measured_after_stripping(client, actor):
    _, headers = actor
    # Padding pushes the raw string over the limit; the stored
    # value is exactly at it
    padded = "   " + "t" * MAX_TITLE_LENGTH + "   "

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "title": padded})

    assert response.status_code == 201
    assert response.get_json()["title"] == "t" * MAX_TITLE_LENGTH


def test_a_title_one_character_over_the_limit_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers,
                           json={"content": "Turinys", "title": "t" * (MAX_TITLE_LENGTH + 1)})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Title must be at most {MAX_TITLE_LENGTH} characters"


def test_a_content_is_measured_after_stripping(client, actor, db):
    _, headers = actor
    padded = "  " + "c" * MAX_CONTENT_LENGTH + "  "

    response = client.post(POSTS, headers=headers, json={"content": padded})

    assert response.status_code == 201
    assert len(response.get_json()["content"]) == MAX_CONTENT_LENGTH


def test_a_content_one_character_over_the_limit_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "c" * (MAX_CONTENT_LENGTH + 1)})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Content must be at most {MAX_CONTENT_LENGTH} characters"


def test_the_title_limit_is_checked_before_the_content_limit(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={
        "content": "c" * (MAX_CONTENT_LENGTH + 1),
        "title": "t" * (MAX_TITLE_LENGTH + 1),
    })

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Title must be at most {MAX_TITLE_LENGTH} characters"


def test_a_body_at_the_summary_length_is_its_own_summary(client, actor, db):
    _, headers = actor
    content = "s" * SUMMARY_LENGTH

    post_id = client.post(POSTS, headers=headers, json={"content": content}).get_json()["id"]

    row = db.execute("SELECT summary, content FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert row["summary"] == row["content"] == content




# -----------------------------------------------------------
# POST /api/social/posts — is_public
# -----------------------------------------------------------

def test_an_omitted_is_public_defaults_to_a_public_post(client, actor, db):
    _, headers = actor

    body = client.post(POSTS, headers=headers, json={"content": "Vieša"}).get_json()

    assert body["isPublic"] is True
    assert db.execute("SELECT is_public FROM news_posts WHERE id = ?", (body["id"],)).fetchone()["is_public"] == 1


def test_a_null_is_public_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "is_public": None})

    assert response.status_code == 400
    assert response.get_json()["error"] == "is_public must be a boolean"


def test_a_list_is_public_is_refused(client, actor):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "is_public": []})

    assert response.status_code == 400
    assert response.get_json()["error"] == "is_public must be a boolean"


def test_a_private_post_never_reaches_a_guests_wall_read(client, actor, db):
    user, headers = actor

    client.post(POSTS, headers=headers, json={"content": "Slapta", "is_public": False})

    guest = _wall(client, user["id"]).get_json()
    assert guest["posts"] == []
    assert guest["total"] == 0




# -----------------------------------------------------------
# POST /api/social/posts — the image beacon guard
# -----------------------------------------------------------

@pytest.mark.parametrize("value", [
    "/api/uploads",                       # the prefix without its slash
    "/API/uploads/a.jpg",                 # the match is case-sensitive
    "api/uploads/a.jpg",                  # no leading slash
    "//evil.example/api/uploads/a.jpg",   # protocol-relative
    "https://evil.example/api/uploads/a.jpg",
    " /api/uploads/a.jpg",                # leading space
])
def test_an_image_url_outside_the_upload_prefix_is_refused(client, actor, value):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "image_url": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


def test_the_bare_upload_prefix_is_accepted_by_the_create_guard(client, actor, db):
    _, headers = actor

    response = client.post(POSTS, headers=headers, json={"content": "Turinys", "image_url": "/api/uploads/"})

    assert response.status_code == 201
    assert response.get_json()["imageUrl"] == "/api/uploads/"


def test_an_empty_image_url_never_becomes_a_beacon(client, actor, db):
    _, headers = actor

    body = client.post(POSTS, headers=headers, json={"content": "Turinys", "image_url": ""}).get_json()

    # Stored as NULL or as "" — either way there is nothing for a
    # reader's browser to fetch
    assert body["imageUrl"] in (None, "")


def test_a_falsy_non_string_image_url_is_refused(client, actor):
    # The guard tests `not in (None, "")`, not truthiness: [] and
    # {} used to reach the INSERT as an unbindable type (a 500)
    # and False / 0 used to be stored and echoed as imageUrl 0
    _, headers = actor

    for value in ([], {}, False, 0):
        response = client.post(POSTS, headers=headers, json={"content": "Turinys", "image_url": value})
        assert response.status_code == 400, value




# -----------------------------------------------------------
# POST /api/social/posts — what the insert snapshots
# -----------------------------------------------------------

def test_an_author_with_a_blank_display_name_answers_a_blank_author(client, actor, db):
    # display_name is NOT NULL, so "" is as empty as an author can
    # get — and it makes BOTH halves of the "current name or the
    # snapshot" fallback falsy
    user, headers = actor
    db.execute("UPDATE users SET display_name = '' WHERE id = ?", (user["id"],))
    db.commit()

    body = client.post(POSTS, headers=headers, json={"content": "Bevardis"}).get_json()

    assert body["author"] == ""
    assert body["authorId"] == user["id"]
    assert db.execute("SELECT author_name FROM news_posts WHERE id = ?",
                      (body["id"],)).fetchone()["author_name"] == ""


def test_a_curator_wall_post_is_still_a_plain_user_post(client, make_user, auth_headers, db):
    curator = make_user(role="curator")

    body = client.post(POSTS, headers=auth_headers(curator), json={"content": "Kuratoriaus siena"}).get_json()

    assert body["source"] == "user"
    assert body["postType"] == "social"
    row = db.execute("SELECT source, post_type FROM news_posts WHERE id = ?", (body["id"],)).fetchone()
    assert row["source"] == "user" and row["post_type"] == "social"


def test_the_created_stamps_are_one_t_form_utc_instant(client, actor, db):
    _, headers = actor

    post_id = client.post(POSTS, headers=headers, json={"content": "Laikas"}).get_json()["id"]

    row = db.execute("SELECT published_at, created_at, updated_at FROM news_posts WHERE id = ?",
                     (post_id,)).fetchone()
    assert row["published_at"] == row["created_at"] == row["updated_at"]
    assert "T" in row["published_at"] and row["published_at"].endswith("+00:00")


def test_a_created_post_is_immediately_on_its_authors_wall(client, actor):
    user, headers = actor

    post_id = client.post(POSTS, headers=headers, json={"content": "Naujiena"}).get_json()["id"]

    assert _ids(_wall(client, user["id"], headers)) == [post_id]


def test_a_created_body_is_stored_raw_and_escaped_on_the_wall(client, actor, db):
    user, headers = actor

    post_id = _post_raw(client, {"content": '<b>Bold</b> & "cit"'}, headers).get_json()["id"]

    assert db.execute("SELECT content FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["content"] == '<b>Bold</b> & "cit"'
    listed = _wall(client, user["id"], headers).get_json()["posts"][0]["content"]
    assert "&lt;b&gt;" in listed and "&amp;" in listed and "<b>" not in listed


@pytest.mark.contract
def test_a_created_post_carries_exactly_the_keys_the_app_reads(client, actor):
    _, headers = actor

    body = client.post(POSTS, headers=headers, json={"content": "Kontraktas"}).get_json()

    assert set(body) == {
        "id", "title", "content", "summary", "imageUrl", "author", "authorId",
        "authorAvatar", "source", "sourceUrl", "postType", "likes", "comments",
        "shares", "date", "isPublic", "liked", "truncated",
    }




# -----------------------------------------------------------
# The "post" quota — one budget for creating AND editing
# -----------------------------------------------------------

def test_edits_and_creates_share_one_post_budget(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    for _ in range(20):
        assert client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Vėl"}).status_code == 200

    response = client.post(POSTS, headers=headers, json={"content": "Per daug"})

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_the_post_budget_is_charged_per_user(client, actor, make_user, auth_headers):
    _, headers = actor
    other = make_user()

    for _ in range(20):
        client.post(POSTS, headers=headers, json={"content": "Mano"})

    assert client.post(POSTS, headers=headers, json={"content": "Dar"}).status_code == 429
    assert client.post(POSTS, headers=auth_headers(other), json={"content": "Kito"}).status_code == 201




# -----------------------------------------------------------
# GET /api/social/posts — the user_id and paging gate
# -----------------------------------------------------------

def test_an_empty_user_id_is_refused(client):
    response = client.get(f"{POSTS}?user_id=")

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id query param required"


def test_a_valueless_user_id_is_refused(client):
    response = client.get(f"{POSTS}?user_id")

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id query param required"


def test_the_user_id_check_runs_before_the_paging_check(client):
    response = client.get(f"{POSTS}?page=0")

    assert response.status_code == 400
    assert response.get_json()["error"] == "user_id query param required"


def test_the_paging_check_runs_before_the_user_lookup(client):
    response = client.get(f"{POSTS}?user_id=nera-tokio&page=0")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"


def test_a_negative_page_is_refused(client, make_user):
    user = make_user()

    assert _wall(client, user["id"], page=-1).status_code == 400


def test_a_page_at_the_cap_is_accepted_and_empty(client, make_user, db):
    user = make_user()
    _seed_post(db, user["id"])

    response = _wall(client, user["id"], page=200)

    assert response.status_code == 200
    assert response.get_json()["posts"] == []


def test_a_page_one_over_the_cap_is_refused(client, make_user):
    user = make_user()

    response = _wall(client, user["id"], page=201)

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 200"


def test_a_per_page_at_the_cap_is_accepted(client, make_user):
    user = make_user()

    assert _wall(client, user["id"], per_page=50).status_code == 200


def test_a_zero_per_page_is_refused(client, make_user):
    user = make_user()

    response = _wall(client, user["id"], per_page=0)

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be a positive integer"


def test_a_float_shaped_per_page_is_refused(client, make_user):
    user = make_user()

    response = _wall(client, user["id"], per_page="2.0")

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be a positive integer"


def test_a_signed_page_and_a_padded_per_page_are_accepted(client, make_user, db):
    # int() takes "+1" and " 2 " — the parser inherits that and
    # a client sending either must not get a 400
    user = make_user()
    _seed_post(db, user["id"])

    response = client.get(f"{POSTS}?user_id={user['id']}&page=%2B1&per_page=%202%20")

    assert response.status_code == 200
    assert response.get_json()["perPage"] == 2


def test_the_first_user_id_wins_when_the_param_repeats(client, make_user, db):
    first = make_user()
    second = make_user()
    mine = _seed_post(db, first["id"])
    _seed_post(db, second["id"])

    response = client.get(f"{POSTS}?user_id={first['id']}&user_id={second['id']}")

    assert _ids(response) == [mine]


def test_an_injection_shaped_user_id_is_just_an_unknown_user(client, make_user, db):
    user = make_user()
    _seed_post(db, user["id"])

    response = client.get(f"{POSTS}?user_id=%27%20OR%201%3D1%20--")

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"




# -----------------------------------------------------------
# GET /api/social/posts — the visibility matrix
# -----------------------------------------------------------

def test_a_garbage_bearer_token_reads_a_wall_as_a_guest(client, make_user, db):
    author = make_user()
    public = _seed_post(db, author["id"])
    _seed_post(db, author["id"], public=False)

    response = _wall(client, author["id"], {"Authorization": "Bearer nesamone"})

    assert _ids(response) == [public]


def test_a_deactivated_users_wall_is_a_404_for_a_guest(client, make_user, db):
    gone = make_user(active=0)
    _seed_post(db, gone["id"])

    response = _wall(client, gone["id"])

    assert response.status_code == 404
    assert response.get_json()["error"] == "User not found"


def test_a_deactivated_users_wall_is_a_404_for_an_ordinary_reader(client, actor, make_user, db):
    _, headers = actor
    gone = make_user(active=0)
    _seed_post(db, gone["id"])

    assert _wall(client, gone["id"], headers).status_code == 404


def test_an_admin_sees_only_the_public_posts_of_a_deactivated_user(client, admin, make_user, db):
    _, headers = admin
    gone = make_user(active=0)
    public = _seed_post(db, gone["id"])
    _seed_post(db, gone["id"], public=False)

    response = _wall(client, gone["id"], headers)

    assert response.status_code == 200
    assert _ids(response) == [public]


def test_an_admin_who_is_a_friend_still_sees_the_private_posts(client, admin, make_user, db):
    admin_user, headers = admin
    gone = make_user(active=0)
    _befriend(db, admin_user["id"], gone["id"])
    private = _seed_post(db, gone["id"], public=False)

    assert _ids(_wall(client, gone["id"], headers)) == [private]


def test_a_reverse_only_friendship_hides_the_private_posts(client, actor, make_user, db):
    me, headers = actor
    author = make_user()
    # They friended me, my direction was never written — every
    # reader here checks the VIEWER's row
    _befriend(db, author["id"], me["id"], both=False)
    public = _seed_post(db, author["id"])
    _seed_post(db, author["id"], public=False)

    assert _ids(_wall(client, author["id"], headers)) == [public]


def test_the_author_sees_their_own_private_faculty_announcement(client, actor, db):
    user, headers = actor
    hidden = _seed_post(db, user["id"], source="faculty", post_type="announcement", public=False)

    assert _ids(_wall(client, user["id"], headers)) == [hidden]


def test_a_scraped_post_is_never_on_a_wall_not_even_the_authors(client, actor, db):
    user, headers = actor
    _seed_post(db, user["id"], source="knf.vu.lt", post_type="article")
    _seed_post(db, user["id"], source="app", post_type="article")

    response = _wall(client, user["id"], headers)

    assert response.get_json()["posts"] == []
    assert response.get_json()["total"] == 0


def test_another_authors_post_stays_off_this_wall(client, actor, make_user, db):
    me, headers = actor
    other = make_user()
    mine = _seed_post(db, me["id"])
    _seed_post(db, other["id"])

    assert _ids(_wall(client, me["id"], headers)) == [mine]


def test_a_user_without_posts_answers_the_empty_envelope(client, actor):
    user, headers = actor

    body = _wall(client, user["id"], headers).get_json()

    assert body == {"posts": [], "page": 1, "perPage": 20, "total": 0, "hasMore": False}




# -----------------------------------------------------------
# GET /api/social/posts — the liked flag
# -----------------------------------------------------------

def test_a_guest_page_always_reports_liked_false(client, make_user, db):
    author = make_user()
    post_id = _seed_post(db, author["id"])
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (author["id"], post_id))
    db.commit()

    assert _wall(client, author["id"]).get_json()["posts"][0]["liked"] is False


def test_only_the_viewers_own_like_lights_the_flag(client, actor, make_user, db):
    me, headers = actor
    author = make_user()
    post_id = _seed_post(db, author["id"])
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (author["id"], post_id))
    db.commit()

    assert _wall(client, author["id"], headers).get_json()["posts"][0]["liked"] is False


def test_the_liked_flag_is_filled_per_row_not_per_page(client, actor, db):
    user, headers = actor
    older = _seed_post(db, user["id"], published_at="2026-01-01T10:00:00+00:00")
    newer = _seed_post(db, user["id"], published_at="2026-02-01T10:00:00+00:00")
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], newer))
    db.commit()

    posts = _wall(client, user["id"], headers).get_json()["posts"]

    assert {p["id"]: p["liked"] for p in posts} == {newer: True, older: False}




# -----------------------------------------------------------
# GET /api/social/posts — trimming, order and paging maths
# -----------------------------------------------------------

def test_a_body_one_character_over_the_summary_length_is_trimmed(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"], content="x" * (SUMMARY_LENGTH + 1))

    post = _wall(client, author["id"]).get_json()["posts"][0]

    assert post["truncated"] is True
    assert len(post["content"]) == SUMMARY_LENGTH


def test_a_body_exactly_at_the_summary_length_is_not_trimmed(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"], content="x" * SUMMARY_LENGTH)

    post = _wall(client, author["id"]).get_json()["posts"][0]

    assert post["truncated"] is False
    assert len(post["content"]) == SUMMARY_LENGTH


def test_the_wall_falls_back_to_the_id_when_the_timestamps_tie(client, make_user, db):
    author = make_user()
    stamp = "2026-03-03T12:00:00+00:00"
    ids = [_seed_post(db, author["id"], published_at=stamp, post_id=f"tie-{n}") for n in range(3)]

    assert _ids(_wall(client, author["id"])) == sorted(ids, reverse=True)


def test_a_paged_walk_of_a_wall_neither_drops_nor_repeats(client, make_user, db):
    author = make_user()
    seeded = {
        _seed_post(db, author["id"], published_at=f"2026-04-0{n}T10:00:00+00:00")
        for n in range(1, 6)
    }

    first = _ids(_wall(client, author["id"], per_page=2, page=1))
    second = _ids(_wall(client, author["id"], per_page=2, page=2))
    third = _ids(_wall(client, author["id"], per_page=2, page=3))

    assert len(first) == len(second) == 2 and len(third) == 1
    assert set(first + second + third) == seeded


def test_has_more_is_false_when_the_page_ends_exactly_on_the_total(client, make_user, db):
    author = make_user()
    for n in range(1, 5):
        _seed_post(db, author["id"], published_at=f"2026-05-0{n}T10:00:00+00:00")

    body = _wall(client, author["id"], per_page=2, page=2).get_json()

    assert body["total"] == 4
    assert body["hasMore"] is False


def test_a_page_past_the_end_is_empty_but_keeps_the_total(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"])

    body = _wall(client, author["id"], per_page=5, page=4).get_json()

    assert body["posts"] == []
    assert body["total"] == 1
    assert body["hasMore"] is False


def test_the_total_follows_the_same_visibility_split_as_the_page(client, actor, db):
    user, headers = actor
    _seed_post(db, user["id"])
    _seed_post(db, user["id"], public=False)

    assert _wall(client, user["id"], headers).get_json()["total"] == 2
    assert _wall(client, user["id"]).get_json()["total"] == 1


def test_the_wall_shows_the_current_name_and_avatar_not_the_snapshot(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"], author_name="Senas Vardas")
    db.execute("UPDATE users SET display_name = ?, avatar_url = ? WHERE id = ?",
               ("Naujas Vardas", IMAGE_URL, author["id"]))
    db.commit()

    post = _wall(client, author["id"]).get_json()["posts"][0]

    assert post["author"] == "Naujas Vardas"
    assert post["authorAvatar"] == IMAGE_URL


@pytest.mark.contract
def test_a_wall_page_carries_exactly_the_envelope_the_app_reads(client, make_user, db):
    author = make_user()
    _seed_post(db, author["id"])

    body = _wall(client, author["id"]).get_json()

    assert set(body) == {"posts", "page", "perPage", "total", "hasMore"}
    assert set(body["posts"][0]) == {
        "id", "title", "content", "summary", "imageUrl", "author", "authorId",
        "authorAvatar", "source", "sourceUrl", "postType", "likes", "comments",
        "shares", "date", "isPublic", "liked", "truncated",
    }




# -----------------------------------------------------------
# PUT /api/social/posts/<id> — the gates, in the order they fire
# -----------------------------------------------------------

def test_editing_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.put(f"{POSTS}/nera-tokio", headers=headers, json={"content": "Kaskas"})

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found or not yours"


def test_the_body_gate_fires_before_the_ownership_lookup(client, actor):
    _, headers = actor

    response = client.put(f"{POSTS}/nera-tokio", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_the_ownership_lookup_fires_before_the_field_validation(client, actor, make_user, db):
    _, headers = actor
    stranger = make_user()
    post_id = _seed_post(db, stranger["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": 12345})

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found or not yours"


def test_an_empty_object_is_a_missing_body_not_an_empty_update(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body required"


def test_an_array_body_never_reaches_the_edit_handler(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", data="[]",
                          headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("source", ["knf.vu.lt", "vu.lt", "app"])
def test_editing_a_non_wall_post_of_your_own_is_a_404(client, actor, db, source):
    user, headers = actor
    post_id = _seed_post(db, user["id"], source=source, post_type="article")

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Nepataisysi"})

    assert response.status_code == 404


def test_editing_an_own_faculty_announcement_is_allowed(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], source="faculty", post_type="announcement")

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Pataisytas skelbimas"})

    assert response.status_code == 200
    assert db.execute("SELECT content FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["content"] == "Pataisytas skelbimas"


def test_an_admin_gets_no_override_on_someone_elses_post_edit(client, admin, make_user, db):
    _, headers = admin
    author = make_user()
    post_id = _seed_post(db, author["id"], content="Ne admino")

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Admino ranka"})

    assert response.status_code == 404
    assert db.execute("SELECT content FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["content"] == "Ne admino"




# -----------------------------------------------------------
# PUT /api/social/posts/<id> — field types and boundaries
# -----------------------------------------------------------

def test_a_null_content_is_refused_by_the_editor(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": None})

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_a_numeric_content_is_refused_by_the_editor(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": 5})

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_an_edit_to_unicode_whitespace_only_is_refused(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], content="Senas")

    response = _put_raw(client, f"{POSTS}/{post_id}", {"content": "\t\n\r "}, headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Post content required"
    assert db.execute("SELECT content FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["content"] == "Senas"


def test_an_edit_one_character_over_the_content_limit_is_refused(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], content="Senas")

    response = client.put(f"{POSTS}/{post_id}", headers=headers,
                          json={"content": "c" * (MAX_CONTENT_LENGTH + 1)})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Content must be at most {MAX_CONTENT_LENGTH} characters"
    assert db.execute("SELECT content FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["content"] == "Senas"


def test_a_content_at_the_limit_is_accepted_and_re_summarised(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers,
                          json={"content": "c" * MAX_CONTENT_LENGTH})

    assert response.status_code == 200
    row = db.execute("SELECT content, summary FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert len(row["content"]) == MAX_CONTENT_LENGTH
    assert row["summary"] == "c" * SUMMARY_LENGTH


def test_a_null_title_is_refused_by_the_editor(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"title": None})

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_a_title_at_the_limit_is_accepted_by_the_editor(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers,
                          json={"title": "t" * MAX_TITLE_LENGTH})

    assert response.status_code == 200
    assert db.execute("SELECT title FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["title"] == "t" * MAX_TITLE_LENGTH


def test_a_refused_title_rolls_back_the_whole_edit(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], content="Senas", title="Sena antraste")

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={
        "content": "Naujas turinys",
        "title": "t" * (MAX_TITLE_LENGTH + 1),
    })

    assert response.status_code == 400
    row = db.execute("SELECT content, title FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert row["content"] == "Senas" and row["title"] == "Sena antraste"


def test_a_blank_title_on_an_empty_stored_body_stores_an_empty_title(client, actor, db):
    user, headers = actor
    # Only a direct insert can leave content empty — create_post
    # refuses it — and this is the arm where every fallback in
    # the chain is falsy
    post_id = _seed_post(db, user["id"], content="", title="Buvo")

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"title": "   "})

    assert response.status_code == 200
    assert db.execute("SELECT title FROM news_posts WHERE id = ?", (post_id,)).fetchone()["title"] == ""




# -----------------------------------------------------------
# PUT /api/social/posts/<id> — which keys the editor knows
# -----------------------------------------------------------

def test_the_editor_does_not_understand_camel_case_keys(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], image_url=IMAGE_URL)

    response = client.put(f"{POSTS}/{post_id}", headers=headers,
                          json={"imageUrl": "/api/uploads/kitas.jpg"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "No fields to update"
    assert db.execute("SELECT image_url FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["image_url"] == IMAGE_URL


def test_visibility_cannot_be_flipped_through_the_editor(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], public=True)

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"is_public": False})

    assert response.status_code == 400
    assert response.get_json()["error"] == "No fields to update"
    assert db.execute("SELECT is_public FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["is_public"] == 1


def test_an_unknown_key_beside_a_known_one_is_ignored(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers,
                          json={"content": "Naujas", "nesamone": 1, "summary": "apeinam"})

    assert response.status_code == 200
    row = db.execute("SELECT content, summary FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert row["content"] == "Naujas" and row["summary"] == "Naujas"


def test_all_three_editable_fields_change_in_one_call(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={
        "content": "Visai kitas turinys",
        "title": "Kita antraste",
        "image_url": IMAGE_URL,
    })

    assert response.status_code == 200
    row = db.execute("SELECT content, title, image_url, summary FROM news_posts WHERE id = ?",
                     (post_id,)).fetchone()
    assert row["content"] == "Visai kitas turinys"
    assert row["title"] == "Kita antraste"
    assert row["image_url"] == IMAGE_URL
    assert row["summary"] == "Visai kitas turinys"


@pytest.mark.parametrize("value", [False, 0, [], {}, 3.5])
def test_the_editor_refuses_every_non_string_image_url(client, actor, db, value):
    # The tight guard create_post's `if image_url` wants: a
    # falsy non-string is a 400 here, never an INSERT parameter
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.put(f"{POSTS}/{post_id}", headers=headers, json={"image_url": value})

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


def test_an_empty_image_url_clears_the_image(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], image_url=IMAGE_URL)

    assert client.put(f"{POSTS}/{post_id}", headers=headers, json={"image_url": ""}).status_code == 200

    assert db.execute("SELECT image_url FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["image_url"] in (None, "")


def test_the_bare_upload_prefix_is_accepted_by_the_editor(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    assert client.put(f"{POSTS}/{post_id}", headers=headers,
                      json={"image_url": "/api/uploads/"}).status_code == 200




# -----------------------------------------------------------
# PUT /api/social/posts/<id> — what an edit must NOT touch
# -----------------------------------------------------------

def test_an_edit_leaves_the_snapshot_the_source_and_the_counters_alone(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], author_name="Snapsotas", likes=4, comments=2, shares=1)
    before = db.execute("SELECT published_at, created_at FROM news_posts WHERE id = ?",
                        (post_id,)).fetchone()

    client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Pakeista"})

    row = db.execute("SELECT * FROM news_posts WHERE id = ?", (post_id,)).fetchone()
    assert row["author_name"] == "Snapsotas"
    assert row["author_id"] == user["id"]
    assert row["source"] == "user" and row["post_type"] == "social"
    assert (row["likes_count"], row["comments_count"], row["shares_count"]) == (4, 2, 1)
    assert row["published_at"] == before["published_at"]
    assert row["created_at"] == before["created_at"]
    assert row["updated_at"] != before["created_at"]


def test_an_edit_stamps_updated_at_in_the_t_form_utc_shape(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], published_at="2026-01-05T08:00:00+00:00")

    client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "Nauja"})

    stamp = db.execute("SELECT updated_at FROM news_posts WHERE id = ?", (post_id,)).fetchone()["updated_at"]
    assert "T" in stamp and stamp.endswith("+00:00")


def test_an_edited_body_is_stored_raw_and_escaped_on_the_wall(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    _put_raw(client, f"{POSTS}/{post_id}", {"content": '<i>Kursyvas</i> & co'}, headers)

    assert db.execute("SELECT content FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["content"] == '<i>Kursyvas</i> & co'
    assert "&lt;i&gt;" in _wall(client, user["id"], headers).get_json()["posts"][0]["content"]


def test_null_bytes_are_stripped_out_of_an_edited_body(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    _put_raw(client, f"{POSTS}/{post_id}", {"content": "prie\x00š"}, headers)

    assert db.execute("SELECT content FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["content"] == "prieš"


def test_an_edit_re_truncates_the_wall_body(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], content="Trumpas")

    client.put(f"{POSTS}/{post_id}", headers=headers, json={"content": "y" * (SUMMARY_LENGTH + 10)})

    post = _wall(client, user["id"], headers).get_json()["posts"][0]
    assert post["truncated"] is True
    assert len(post["content"]) == SUMMARY_LENGTH




# -----------------------------------------------------------
# DELETE /api/social/posts/<id>
# -----------------------------------------------------------

def test_deleting_answers_the_status_shape(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    response = client.delete(f"{POSTS}/{post_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted"}


def test_deleting_the_same_post_twice_is_a_404_the_second_time(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200
    second = client.delete(f"{POSTS}/{post_id}", headers=headers)

    assert second.status_code == 404
    assert second.get_json()["error"] == "Post not found or not yours"


@pytest.mark.parametrize("source", ["app", "vu.lt"])
def test_deleting_a_non_wall_post_of_your_own_is_a_404(client, actor, db, source):
    user, headers = actor
    post_id = _seed_post(db, user["id"], source=source, post_type="article")

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 404
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 1


def test_a_deleted_post_leaves_its_authors_wall_and_its_total(client, actor, db):
    user, headers = actor
    kept = _seed_post(db, user["id"], published_at="2026-06-01T10:00:00+00:00")
    doomed = _seed_post(db, user["id"], published_at="2026-06-02T10:00:00+00:00")

    client.delete(f"{POSTS}/{doomed}", headers=headers)

    body = _wall(client, user["id"], headers).get_json()
    assert [p["id"] for p in body["posts"]] == [kept]
    assert body["total"] == 1


def test_deleting_cascades_the_comments(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])
    db.execute("INSERT INTO news_comments (id, post_id, user_id, text) VALUES (?, ?, ?, ?)",
               (str(uuid.uuid4()), post_id, user["id"], "Komentaras"))
    db.commit()

    client.delete(f"{POSTS}/{post_id}", headers=headers)

    assert db.execute("SELECT COUNT(*) AS c FROM news_comments WHERE post_id = ?",
                      (post_id,)).fetchone()["c"] == 0


def test_deleting_cascades_the_poll_its_options_and_its_votes(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"], post_type="poll")
    poll_id, option_id = str(uuid.uuid4()), str(uuid.uuid4())
    db.execute("INSERT INTO polls (id, post_id, title) VALUES (?, ?, ?)", (poll_id, post_id, "Klausimas"))
    db.execute("INSERT INTO poll_options (id, poll_id, text) VALUES (?, ?, ?)", (option_id, poll_id, "Taip"))
    db.execute("INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
               (user["id"], poll_id, option_id))
    db.commit()

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200

    assert db.execute("SELECT COUNT(*) AS c FROM polls WHERE id = ?", (poll_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM poll_options WHERE poll_id = ?", (poll_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM poll_votes WHERE poll_id = ?", (poll_id,)).fetchone()["c"] == 0


def test_deleting_leaves_the_other_posts_of_the_same_author_alone(client, actor, db):
    user, headers = actor
    keep = _seed_post(db, user["id"])
    drop = _seed_post(db, user["id"])

    client.delete(f"{POSTS}/{drop}", headers=headers)

    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (keep,)).fetchone()["c"] == 1


def test_the_delete_budget_is_forty_and_separate_from_the_create_budget(client, actor):
    _, headers = actor

    for _ in range(40):
        assert client.delete(f"{POSTS}/{uuid.uuid4()}", headers=headers).status_code == 404

    blocked = client.delete(f"{POSTS}/{uuid.uuid4()}", headers=headers)

    assert blocked.status_code == 429
    assert blocked.get_json()["code"] == "rate_limited"
    # The "post" scope is a different bucket — creating still works
    assert client.post(POSTS, headers=headers, json={"content": "Vis dar galiu"}).status_code == 201


def test_the_delete_budget_reopens_after_the_rate_limit_window(client, actor, db):
    user, headers = actor
    post_id = _seed_post(db, user["id"])
    start = datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)

    with time_machine.travel(start, tick=False):
        for _ in range(40):
            client.delete(f"{POSTS}/{uuid.uuid4()}", headers=headers)
        assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 429

    with time_machine.travel(start + timedelta(minutes=6), tick=False):
        assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200




# -----------------------------------------------------------
# DELETE — the image reaper, driven through the route
# -----------------------------------------------------------

def test_a_stored_empty_image_url_never_reaches_the_reaper(client, actor, db, monkeypatch):
    user, headers = actor
    post_id = _seed_post(db, user["id"], image_url="")
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda path: calls.append(path))

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200
    assert calls == []


def test_the_stored_path_reaches_the_reaper_verbatim(client, actor, db, monkeypatch):
    user, headers = actor
    post_id = _seed_post(db, user["id"], image_url=IMAGE_URL)
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda path: calls.append(path))

    client.delete(f"{POSTS}/{post_id}", headers=headers)

    assert calls == [IMAGE_URL]


def test_a_traversal_path_cannot_unlink_a_file_outside_the_upload_dir(client, app, actor, db, monkeypatch, tmp_path):
    user, headers = actor
    # uploads/routes.py caches the resolved directory in a process
    # global, so it has to be re-resolved against THIS test's dir
    monkeypatch.setattr("app.uploads.routes._upload_dir", None)
    outsider = tmp_path / "secret.png"
    outsider.write_bytes(b"not-yours")
    post_id = _seed_post(db, user["id"], image_url="/api/uploads/../secret.png")

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200

    assert outsider.exists()
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 0


def test_a_reaper_that_reports_nothing_removed_still_answers_deleted(client, actor, db, monkeypatch):
    user, headers = actor
    post_id = _seed_post(db, user["id"], image_url=IMAGE_URL)
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda path: False)

    assert client.delete(f"{POSTS}/{post_id}", headers=headers).status_code == 200




# -----------------------------------------------------------
# _delete_upload_file — the helper on its own
# -----------------------------------------------------------
#
# Called directly: it is a plain function with a plain
# contract, and the paths a route can hand it are only a
# subset of the ones it has to survive.
# -----------------------------------------------------------

@pytest.mark.parametrize("path", [None, "", 0])
def test_the_reaper_ignores_an_empty_path(monkeypatch, path):
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda p: calls.append(p))

    _delete_upload_file(path)

    assert calls == []


@pytest.mark.parametrize("path", [
    "/uploads/a.jpg",
    "api/uploads/a.jpg",
    "/api/upload/a.jpg",
    "/api/uploads",
    "http://evil.example/api/uploads/a.jpg",
    "/API/uploads/a.jpg",
])
def test_the_reaper_ignores_a_path_outside_the_upload_prefix(monkeypatch, path):
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda p: calls.append(p))

    _delete_upload_file(path)

    assert calls == []


def test_the_reaper_forwards_an_upload_path_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda p: calls.append(p))

    _delete_upload_file("/api/uploads/../nesamone/a.jpg")

    # The prefix is all this helper checks — sanitising the name
    # is the uploads package's job
    assert calls == ["/api/uploads/../nesamone/a.jpg"]


def test_the_bare_prefix_still_reaches_the_reaper(monkeypatch):
    calls = []
    monkeypatch.setattr("app.uploads.routes.delete_upload", lambda p: calls.append(p))

    _delete_upload_file("/api/uploads/")

    assert calls == ["/api/uploads/"]


def test_a_missing_uploads_package_is_a_silent_no_op(monkeypatch):
    # A None entry in sys.modules is what makes the guarded lazy
    # import raise ImportError — the "uploads has not landed yet"
    # branch
    monkeypatch.setitem(sys.modules, "app.uploads.routes", None)

    assert _delete_upload_file("/api/uploads/a.jpg") is None


def test_a_raising_reaper_is_logged_and_swallowed(monkeypatch, caplog):
    def _boom(path):
        raise OSError("disk on fire")

    monkeypatch.setattr("app.uploads.routes.delete_upload", _boom)

    with caplog.at_level(logging.WARNING, logger="app.social.routes"):
        assert _delete_upload_file("/api/uploads/a.jpg") is None

    assert "Could not delete upload" in caplog.text


def test_a_reaper_raising_a_bare_exception_is_swallowed_too(monkeypatch):
    def _boom(path):
        raise Exception("kazkas")

    monkeypatch.setattr("app.uploads.routes.delete_upload", _boom)

    assert _delete_upload_file("/api/uploads/a.jpg") is None
