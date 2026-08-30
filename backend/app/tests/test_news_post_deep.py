# -----------------------------------------------------------
#  [*] Tests — news posts, the exhaustive branch pass
#
#  The gap-closing companion to test_news_posts.py. That file
#  proves the lifecycle works; this one walks EVERY arm of the
#  four functions it owns in app/news/routes.py:
#
#    create_post    — the guard ladder in the order the route
#                     runs it (body shape → content typing →
#                     title typing → the two length caps →
#                     post_type whitelist → is_public typing →
#                     image_url origin), the role → source
#                     fork, both post_type defaults, the one
#                     clock read behind published/created/
#                     updated_at, the 'news' push fork and the
#                     write quota (which a REJECTED body also
#                     spends)
#    get_post       — the missing row, the gated row, the
#                     liked flag for guest / liker / stranger,
#                     the author-name COALESCE over a NULL, a
#                     dangling and a live author_id, the
#                     counters served straight off the row and
#                     the inline poll fork in both directions
#    delete_post    — 401 / 404 / 403 / 200 for every role and
#                     ownership pairing, the source_url
#                     tombstone and its falsy skip, the child
#                     sweep with and without a poll, the cover
#                     unlink for a path we own and its skip
#                     for one we do not, idempotency and the
#                     delete quota
#    _can_view_post — called directly on real rows so the
#                     states no route can create are reachable
#                     too: a truthy-but-not-1 is_public, a
#                     wall post with NO author, and a
#                     friendship written in ONE direction only
#
#  Escaping note (TESTPLAN rule 10): app/__init__.py
#  html-escapes every string on the way OUT, and the test
#  client escapes a `json=` body on the way IN. Where markup
#  matters the payload goes out as raw bytes through
#  _post_raw, so what is asserted is what a real client sends.
# -----------------------------------------------------------

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
import time_machine

from app.news.routes import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    POST_TYPES,
    SOURCES,
    STAFF_ROLES,
    _can_view_post,
    _POST_GATE_SELECT,
)

# Every key _post_to_dict emits plus the `liked` flag both
# single-post paths attach — the mobile NewsPost shape
POST_KEYS = {
    "id", "title", "content", "summary", "imageUrl", "author", "authorId",
    "source", "sourceUrl", "postType", "likes", "comments", "shares",
    "date", "isPublic", "liked",
}

# The two roles that are staff but not admin: they proof-read
# a private faculty draft yet may NOT delete someone else's
# post — the two gates in this file that look alike and are not
NON_ADMIN_STAFF = tuple(r for r in STAFF_ROLES if r != "admin")




# -----------------------------------------------------------
# _seed_post
# -----------------------------------------------------------
#
# One news_posts row written straight to the database, for the
# states POST /api/news refuses to create: a scraped article
# (author_id NULL + a source_url), a private draft, a foreign
# or non-string image_url, a drifted counter, an is_public
# that is neither 0 nor 1. Returns the new post id.
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
            source, source_url, post_type, is_public, likes_count,
            comments_count, shares_count, published_at, created_at, updated_at)
           VALUES (:id, :title, :content, :summary, :image_url, :author_id, :author_name,
                   :source, :source_url, :post_type, :is_public, :likes_count,
                   :comments_count, :shares_count, :published_at, :published_at,
                   :published_at)""",
        row,
    )
    db.commit()
    return row["id"]




# -----------------------------------------------------------
# _seed_poll
# -----------------------------------------------------------
#
# A poll plus its options on an existing post. Returns
# (poll_id, [option_id, ...]) in creation order, which is the
# rowid order the poll shape serves them in.
# -----------------------------------------------------------

def _seed_poll(db, post_id, options=("Taip", "Ne")):
    poll_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO polls (id, post_id, title, end_date, total_votes, created_at)"
        " VALUES (?, ?, ?, NULL, 0, ?)",
        (poll_id, post_id, "Ar ateisi", datetime.now(timezone.utc).isoformat()),
    )

    option_ids = []
    for text in options:
        option_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO poll_options (id, poll_id, text, votes) VALUES (?, ?, ?, 0)",
            (option_id, poll_id, text),
        )
        option_ids.append(option_id)

    db.commit()
    return poll_id, option_ids




# -----------------------------------------------------------
# _befriend / _follow_one_way
# -----------------------------------------------------------
#
# friendships is written in BOTH directions on accept
# (social/routes.py) and _can_view_post trusts exactly that,
# so the happy fixture writes both rows. _follow_one_way
# writes the single row the gate does NOT read, which is how
# the "one direction is enough" claim gets tested rather than
# assumed.
# -----------------------------------------------------------

def _befriend(db, first_id, second_id):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (first_id, second_id))
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (second_id, first_id))
    db.commit()


def _follow_one_way(db, viewer_id, author_id):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (viewer_id, author_id))
    db.commit()




# -----------------------------------------------------------
# _create / _post_raw
# -----------------------------------------------------------
#
# _create fills in the one required field so a test names only
# what it is about. _post_raw is TESTPLAN rule 10: the test
# client serialises a `json=` kwarg through the app's own
# html-escaping provider, so anything asserting on markup,
# entities or round-tripped text has to put the bytes on the
# wire itself.
# -----------------------------------------------------------

def _create(client, headers, **body):
    payload = {"content": "Turinys"}
    payload.update(body)
    return client.post("/api/news", json=payload, headers=headers)


def _post_raw(client, path, payload, headers):
    return client.post(
        path,
        data=json.dumps(payload),
        headers={**headers, "Content-Type": "application/json"},
    )




# -----------------------------------------------------------
# _gate_row / _user_row
# -----------------------------------------------------------
#
# The two arguments _can_view_post takes, read the way its
# callers read them: the row through _POST_GATE_SELECT (the
# column list its banner names as the contract) and the user
# through the same narrow projection resolve_session_token
# hands the routes.
# -----------------------------------------------------------

def _gate_row(db, post_id):
    return db.execute(
        f"SELECT {_POST_GATE_SELECT} FROM news_posts WHERE id = ?", (post_id,)
    ).fetchone()


def _user_row(db, user_id):
    return db.execute("SELECT id, role, display_name FROM users WHERE id = ?", (user_id,)).fetchone()








# ===========================================================
#  create_post — the body has to be a JSON object at all
# ===========================================================

def test_a_request_with_no_body_at_all_is_refused(client, actor):
    response = client.post("/api/news", headers=actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_an_empty_json_object_is_refused_like_a_missing_body(client, actor):
    # {} is falsy, so it takes the SAME branch as no body — the
    # message a client sees must not depend on which one it sent
    response = _post_raw(client, "/api/news", {}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


@pytest.mark.parametrize("body", ["[1, 2]", '"turinys"', "5", "true"])
def test_a_json_body_that_is_not_an_object_never_reaches_the_route(client, actor, body):
    # app/__init__.py's validate_json_input before_request stops
    # a non-dict body first, so create_post's own guard only ever
    # sees the FALSY shapes (below) — the route can no longer be
    # the place a "[1,2]" body turns into an AttributeError
    response = client.post(
        "/api/news", data=body,
        headers={**actor[1], "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


def test_a_literal_null_body_falls_through_to_the_routes_own_guard(client, actor):
    # get_json(silent=True) reads "null" as None, which the
    # before_request hook lets past on purpose
    response = client.post(
        "/api/news", data="null",
        headers={**actor[1], "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_malformed_json_is_a_400_and_not_a_500(client, actor):
    response = client.post(
        "/api/news", data="{not json at all",
        headers={**actor[1], "Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_a_form_encoded_body_is_refused_rather_than_parsed(client, actor):
    # get_json_object parses with silent=True, so a wrong
    # Content-Type comes back None and lands on the 400 instead
    # of the app's 415 handler
    response = client.post("/api/news", data="content=Turinys", headers=actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_creating_a_post_without_a_token_is_a_401(client):
    response = client.post("/api/news", json={"content": "Turinys"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_a_bogus_bearer_token_cannot_create_a_post(client):
    response = client.post(
        "/api/news", json={"content": "Turinys"},
        headers={"Authorization": f"Bearer {uuid.uuid4()}"},
    )

    assert response.status_code == 401








# ===========================================================
#  create_post — content typing and the non-blank rule
# ===========================================================

@pytest.mark.parametrize("bad", [5, 1.5, True, False, ["a"], {"a": 1}])
def test_a_non_string_content_is_refused_by_type(client, actor, bad):
    response = _post_raw(client, "/api/news", {"content": bad}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_an_explicit_null_content_reads_as_missing_not_as_a_type_error(client, actor):
    # `is not None` guards the type check, so null falls through
    # to the emptiness check and gets the other message
    response = _post_raw(client, "/api/news", {"content": None}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "Content required"


def test_an_absent_content_key_is_refused(client, actor):
    response = _post_raw(client, "/api/news", {"title": "Antraste"}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "Content required"


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n", " \t\r\n "])
def test_a_blank_content_is_refused_however_it_is_spelled(client, actor, blank):
    response = _post_raw(client, "/api/news", {"content": blank}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "Content required"


def test_a_single_character_content_is_enough(client, actor):
    response = _post_raw(client, "/api/news", {"content": "x"}, actor[1])

    assert response.status_code == 201
    assert response.get_json()["content"] == "x"


def test_content_is_stored_stripped_of_its_surrounding_whitespace(client, db, actor):
    response = _post_raw(client, "/api/news", {"content": "   Turinys   "}, actor[1])

    stored = db.execute(
        "SELECT content FROM news_posts WHERE id = ?", (response.get_json()["id"],)
    ).fetchone()["content"]
    assert stored == "Turinys"








# ===========================================================
#  create_post — title typing, the fallback and the cap
# ===========================================================

@pytest.mark.parametrize("bad", [7, 2.5, True, ["a"], {"a": 1}])
def test_a_non_string_title_is_refused_by_type(client, actor, bad):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "title": bad}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_content_typing_is_checked_before_title_typing(client, actor):
    response = _post_raw(client, "/api/news", {"content": 1, "title": 1}, actor[1])

    assert response.get_json()["error"] == "content must be a string"


def test_an_explicit_null_title_falls_back_to_the_content(client, actor):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "title": None}, actor[1])

    assert response.status_code == 201
    assert response.get_json()["title"] == "Turinys"


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_a_blank_title_falls_back_to_the_content(client, actor, blank):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "title": blank}, actor[1])

    assert response.status_code == 201
    assert response.get_json()["title"] == "Turinys"


def test_the_title_fallback_stops_at_eighty_characters(client, actor):
    content = "z" * 200

    response = _post_raw(client, "/api/news", {"content": content}, actor[1])

    assert response.get_json()["title"] == "z" * 80


def test_a_content_of_exactly_eighty_characters_becomes_the_whole_title(client, actor):
    content = "y" * 80

    response = _post_raw(client, "/api/news", {"content": content}, actor[1])

    assert response.get_json()["title"] == content


def test_a_title_of_exactly_the_cap_is_accepted(client, actor):
    title = "T" * MAX_TITLE_LENGTH

    response = _post_raw(client, "/api/news", {"content": "Turinys", "title": title}, actor[1])

    assert response.status_code == 201
    assert response.get_json()["title"] == title


def test_a_title_one_over_the_cap_is_refused(client, actor):
    response = _post_raw(
        client, "/api/news",
        {"content": "Turinys", "title": "T" * (MAX_TITLE_LENGTH + 1)}, actor[1],
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Title must be at most {MAX_TITLE_LENGTH} characters"


def test_the_title_cap_measures_the_stripped_value(client, actor):
    # 200 characters wrapped in whitespace still fits: the strip
    # happens before the len()
    title = "  " + "T" * MAX_TITLE_LENGTH + "  "

    response = _post_raw(client, "/api/news", {"content": "Turinys", "title": title}, actor[1])

    assert response.status_code == 201








# ===========================================================
#  create_post — the content cap and the order of the two
# ===========================================================

def test_content_of_exactly_the_cap_is_accepted(client, actor):
    content = "c" * MAX_CONTENT_LENGTH

    response = _post_raw(client, "/api/news", {"content": content}, actor[1])

    assert response.status_code == 201
    assert len(response.get_json()["content"]) == MAX_CONTENT_LENGTH


def test_content_one_over_the_cap_is_refused(client, actor):
    response = _post_raw(client, "/api/news", {"content": "c" * (MAX_CONTENT_LENGTH + 1)}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Content must be at most {MAX_CONTENT_LENGTH} characters"


def test_the_title_cap_is_reported_before_the_content_cap(client, actor):
    # Both are over; the route checks the title first, and a
    # client showing one message must be told about that one
    response = _post_raw(client, "/api/news", {
        "content": "c" * (MAX_CONTENT_LENGTH + 1),
        "title": "T" * (MAX_TITLE_LENGTH + 1),
    }, actor[1])

    assert response.get_json()["error"].startswith("Title must be")


def test_an_over_long_content_with_no_title_still_reports_the_content(client, actor):
    # The fallback title is content[:80] and can never breach
    # the 200 cap, so the content message is the one that fires
    response = _post_raw(client, "/api/news", {"content": "c" * (MAX_CONTENT_LENGTH + 1)}, actor[1])

    assert response.get_json()["error"].startswith("Content must be")


def test_a_refused_length_never_reaches_the_database(client, db, actor):
    _post_raw(client, "/api/news", {"content": "c" * (MAX_CONTENT_LENGTH + 1)}, actor[1])

    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 0


def test_the_caps_count_characters_and_not_bytes(client, actor):
    # 10000 three-byte characters is 30 kB on the wire and still
    # exactly at the cap
    response = _post_raw(client, "/api/news", {"content": "ą" * MAX_CONTENT_LENGTH}, actor[1])

    assert response.status_code == 201








# ===========================================================
#  create_post — the post_type whitelist
# ===========================================================

@pytest.mark.parametrize("post_type", POST_TYPES)
def test_every_whitelisted_post_type_survives_the_role_default(client, actor, post_type):
    response = _post_raw(
        client, "/api/news", {"content": "Turinys", "post_type": post_type}, actor[1]
    )

    assert response.status_code == 201
    assert response.get_json()["postType"] == post_type


def test_the_poll_post_type_is_not_mintable_by_a_client(client, actor):
    # Only create_poll's server-side flip may set 'poll', or a
    # client could paint a poll card with no poll behind it
    response = _post_raw(client, "/api/news", {"content": "Turinys", "post_type": "poll"}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == f"post_type must be one of: {', '.join(POST_TYPES)}"


@pytest.mark.parametrize("bad", ["Article", " article", "article ", "ARTICLE", "news", 5, True, ["article"], {"a": 1}])
def test_a_post_type_outside_the_whitelist_is_refused(client, actor, bad):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "post_type": bad}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"].startswith("post_type must be one of")


@pytest.mark.parametrize("falsy", [None, "", 0, False, [], {}])
def test_a_falsy_post_type_takes_the_role_default_instead_of_a_400(client, actor, falsy):
    # `if post_type and ...` short-circuits, then `if not
    # post_type` fills in the default — both halves of the same
    # truthiness test have to agree
    response = _post_raw(client, "/api/news", {"content": "Turinys", "post_type": falsy}, actor[1])

    assert response.status_code == 201
    assert response.get_json()["postType"] == "social"








# ===========================================================
#  create_post — is_public must be a real boolean
# ===========================================================

@pytest.mark.parametrize("bad", ["true", "false", "", 0, 1, 1.0, None, [], {}, "yes"])
def test_is_public_refuses_everything_that_is_not_a_boolean(client, actor, bad):
    # The string "false" used to be truthy AND echoed back
    # verbatim, so a post the author meant to hide went public
    response = _post_raw(client, "/api/news", {"content": "Turinys", "is_public": bad}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "is_public must be a boolean"


def test_is_public_true_stores_a_public_row(client, db, actor):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "is_public": True}, actor[1])

    assert response.get_json()["isPublic"] is True
    assert db.execute(
        "SELECT is_public FROM news_posts WHERE id = ?", (response.get_json()["id"],)
    ).fetchone()["is_public"] == 1


def test_is_public_false_stores_a_private_row(client, db, actor):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "is_public": False}, actor[1])

    assert response.get_json()["isPublic"] is False
    assert db.execute(
        "SELECT is_public FROM news_posts WHERE id = ?", (response.get_json()["id"],)
    ).fetchone()["is_public"] == 0


def test_an_omitted_is_public_defaults_to_public(client, actor):
    response = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1])

    assert response.get_json()["isPublic"] is True


def test_the_post_type_whitelist_is_checked_before_is_public(client, actor):
    response = _post_raw(
        client, "/api/news",
        {"content": "Turinys", "post_type": "poll", "is_public": "yes"}, actor[1],
    )

    assert response.get_json()["error"].startswith("post_type must be one of")








# ===========================================================
#  create_post — image_url may only be one of our uploads
# ===========================================================

def test_our_own_uploads_path_is_accepted_as_a_cover(client, actor):
    response = _post_raw(
        client, "/api/news",
        {"content": "Turinys", "image_url": "/api/uploads/abc.jpg"}, actor[1],
    )

    assert response.status_code == 201
    assert response.get_json()["imageUrl"] == "/api/uploads/abc.jpg"


def test_the_bare_uploads_prefix_is_the_accepted_boundary(client, actor):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "image_url": "/api/uploads/"}, actor[1])

    assert response.status_code == 201


def test_the_prefix_without_its_trailing_slash_is_refused(client, actor):
    # "/api/uploads" is one character short of the prefix, so a
    # host named "/api/uploadsevil/..." cannot sneak through
    response = _post_raw(client, "/api/news", {"content": "Turinys", "image_url": "/api/uploads"}, actor[1])

    assert response.status_code == 400


@pytest.mark.parametrize("image_url", [
    "https://evil.example/beacon.gif",
    "http://knf.vu.lt/images/a.jpg",
    "//evil.example/a.jpg",
    "api/uploads/a.jpg",
    " /api/uploads/a.jpg",
    "/API/UPLOADS/a.jpg",
    "/api/uploadsevil/a.jpg",
    "javascript:alert(1)",
    "data:image/png;base64,AAAA",
    "/uploads/a.jpg",
])
def test_an_image_url_outside_our_uploads_is_refused(client, actor, image_url):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "image_url": image_url}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


@pytest.mark.parametrize("bad", [5, 1.5, True, ["/api/uploads/a.jpg"], {"url": "/api/uploads/a.jpg"}])
def test_a_truthy_non_string_image_url_is_refused(client, actor, bad):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "image_url": bad}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


def test_a_null_image_url_leaves_the_post_without_a_cover(client, actor):
    response = _post_raw(client, "/api/news", {"content": "Turinys", "image_url": None}, actor[1])

    assert response.status_code == 201
    assert response.get_json()["imageUrl"] is None


def test_an_empty_image_url_string_is_stored_as_no_cover(client, actor):
    # "" is falsy, so it skips the origin gate entirely rather
    # than being rejected as a bad path
    response = _post_raw(client, "/api/news", {"content": "Turinys", "image_url": ""}, actor[1])

    assert response.status_code == 201
    assert not response.get_json()["imageUrl"]


@pytest.mark.parametrize("falsy", [0, False])
def test_a_falsy_non_string_image_url_never_crashes_the_route(client, db, actor, falsy):
    # 0/False used to slip past a truthiness-guarded isinstance
    # and reach the TEXT column as the string "0", so the card
    # went out with imageUrl "0" and the app tried to load /0.
    # The type is checked on its own now, like every neighbour
    response = _post_raw(client, "/api/news", {"content": "Turinys", "image_url": falsy}, actor[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 0


def test_the_is_public_type_is_checked_before_the_image_url(client, actor):
    response = _post_raw(client, "/api/news", {
        "content": "Turinys", "is_public": "yes", "image_url": "https://evil.example/a.gif",
    }, actor[1])

    assert response.get_json()["error"] == "is_public must be a boolean"


def test_a_refused_image_url_never_reaches_the_database(client, db, actor):
    _post_raw(client, "/api/news", {"content": "Turinys", "image_url": "https://evil.example/a.gif"}, actor[1])

    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 0








# ===========================================================
#  create_post — the role picks the source and the default
# ===========================================================

@pytest.mark.parametrize("role", STAFF_ROLES)
def test_staff_publish_as_faculty_and_default_to_an_announcement(client, make_user, auth_headers, role):
    headers = auth_headers(make_user(role=role))

    body = _post_raw(client, "/api/news", {"content": "Turinys"}, headers).get_json()

    assert body["source"] == "faculty"
    assert body["postType"] == "announcement"


def test_a_student_publishes_as_a_user_wall_post_defaulting_to_social(client, actor):
    body = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()

    assert body["source"] == "user"
    assert body["postType"] == "social"


@pytest.mark.parametrize("role", STAFF_ROLES)
def test_an_explicit_post_type_beats_the_staff_default(client, make_user, auth_headers, role):
    headers = auth_headers(make_user(role=role))

    body = _post_raw(client, "/api/news", {"content": "Turinys", "post_type": "link"}, headers).get_json()

    assert body["source"] == "faculty"
    assert body["postType"] == "link"


def test_a_client_supplied_source_is_ignored_entirely(client, actor):
    # The role decides; a student naming 'faculty' still writes
    # a wall post, or the boost in the feed ranking would be
    # for sale
    body = _post_raw(client, "/api/news", {"content": "Turinys", "source": "faculty"}, actor[1]).get_json()

    assert body["source"] == "user"


def test_the_source_a_post_gets_is_always_one_the_schema_allows(client, make_user, auth_headers):
    for role in ("student",) + STAFF_ROLES:
        headers = auth_headers(make_user(role=role))
        assert _post_raw(client, "/api/news", {"content": "Turinys"}, headers).get_json()["source"] in SOURCES








# ===========================================================
#  create_post — the row, the wire shape and the one clock read
# ===========================================================

@pytest.mark.contract
def test_the_201_carries_exactly_the_post_shape_and_nothing_more(client, actor):
    body = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()

    assert set(body) == POST_KEYS


def test_a_fresh_post_is_never_already_liked(client, actor):
    assert _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()["liked"] is False


def test_a_created_post_carries_a_null_source_url(client, actor):
    # The hand-built 201 this route used to assemble left
    # sourceUrl out altogether; re-reading through _post_to_dict
    # is what put it back
    body = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()

    assert "sourceUrl" in body
    assert body["sourceUrl"] is None


def test_the_three_counters_all_start_at_zero(client, actor):
    body = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()

    assert (body["likes"], body["comments"], body["shares"]) == (0, 0, 0)


def test_the_summary_is_the_first_two_hundred_characters_of_the_content(client, actor):
    content = "s" * 250

    body = _post_raw(client, "/api/news", {"content": content}, actor[1]).get_json()

    assert body["summary"] == "s" * 200


def test_a_short_content_is_its_own_summary(client, actor):
    body = _post_raw(client, "/api/news", {"content": "Trumpai"}, actor[1]).get_json()

    assert body["summary"] == "Trumpai"


def test_the_summary_is_cut_from_the_stripped_content(client, db, actor):
    response = _post_raw(client, "/api/news", {"content": "   Turinys   "}, actor[1])

    stored = db.execute(
        "SELECT summary FROM news_posts WHERE id = ?", (response.get_json()["id"],)
    ).fetchone()["summary"]
    assert stored == "Turinys"


def test_one_clock_read_serves_published_created_and_updated_at(client, db, actor):
    with time_machine.travel("2026-03-01 12:00:00 +0000", tick=False):
        response = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1])

    row = db.execute(
        "SELECT published_at, created_at, updated_at FROM news_posts WHERE id = ?",
        (response.get_json()["id"],),
    ).fetchone()
    assert row["published_at"] == row["created_at"] == row["updated_at"]
    assert row["published_at"] == "2026-03-01T12:00:00+00:00"
    assert response.get_json()["date"] == "2026-03-01T12:00:00+00:00"


def test_the_stamp_is_explicit_utc_and_parses_back_aware(client, actor):
    body = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()

    parsed = datetime.fromisoformat(body["date"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_the_row_keeps_the_authors_identity_and_display_name_snapshot(client, db, make_user, auth_headers):
    author = make_user(display_name="Ona Onaite")

    response = _post_raw(client, "/api/news", {"content": "Turinys"}, auth_headers(author))

    row = db.execute(
        "SELECT author_id, author_name FROM news_posts WHERE id = ?",
        (response.get_json()["id"],),
    ).fetchone()
    assert row["author_id"] == author["id"]
    assert row["author_name"] == "Ona Onaite"


def test_the_201_serves_the_authors_live_name_through_the_join(client, db, make_user, auth_headers):
    author = make_user(display_name="Senas Vardas")
    headers = auth_headers(author)
    db.execute("UPDATE users SET display_name = 'Naujas Vardas' WHERE id = ?", (author["id"],))
    db.commit()

    body = _post_raw(client, "/api/news", {"content": "Turinys"}, headers).get_json()

    # The JOIN wins over the snapshot the INSERT just wrote
    assert body["author"] == "Naujas Vardas"
    assert body["authorId"] == author["id"]


def test_the_content_is_stored_raw_and_escaped_only_on_the_way_out(client, db, actor):
    # Nothing is escaped on the way IN; the after_request hook
    # in app/__init__.py escapes on the way out, so the DB keeps
    # what the author typed
    response = _post_raw(client, "/api/news", {"content": "Kaina < 10 & daugiau"}, actor[1])

    assert response.get_json()["content"] == "Kaina &lt; 10 &amp; daugiau"
    stored = db.execute(
        "SELECT content FROM news_posts WHERE id = ?", (response.get_json()["id"],)
    ).fetchone()["content"]
    assert stored == "Kaina < 10 & daugiau"


def test_the_fallback_title_is_cut_from_the_raw_content(client, db, actor):
    response = _post_raw(client, "/api/news", {"content": "<b>Labas</b>"}, actor[1])

    stored = db.execute(
        "SELECT title FROM news_posts WHERE id = ?", (response.get_json()["id"],)
    ).fetchone()["title"]
    assert stored == "<b>Labas</b>"


def test_a_created_post_is_immediately_readable_through_get_post(client, actor):
    created = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()

    fetched = client.get(f"/api/news/{created['id']}", headers=actor[1]).get_json()

    assert fetched == created


def test_each_created_post_gets_its_own_id(client, actor):
    first = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()["id"]
    second = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()["id"]

    assert first != second
    assert uuid.UUID(first).version == 4








# ===========================================================
#  create_post — the 'news' push channel fork
# ===========================================================

# -----------------------------------------------------------
# _capture_push
# -----------------------------------------------------------
#
# notify_channel is imported INSIDE create_post, so the patch
# has to land on app.notifications.push — the module the lazy
# import resolves against — not on a name in news.routes.
# -----------------------------------------------------------

def _capture_push(monkeypatch):
    import app.notifications.push as push

    calls = []
    monkeypatch.setattr(push, "notify_channel", lambda *a, **kw: calls.append((a, kw)))
    return calls


def test_a_public_faculty_post_rings_the_news_channel_with_its_own_summary(client, make_user, auth_headers, monkeypatch):
    calls = _capture_push(monkeypatch)
    author = make_user(role="curator")
    headers = auth_headers(author)

    response = _post_raw(client, "/api/news", {"content": "s" * 250, "title": "Antraste"}, headers)

    assert response.status_code == 201
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "news"
    assert args[1] == "Antraste"
    assert args[2] == "s" * 200
    assert kwargs["data"] == {
        "type": "news", "source": "faculty", "postId": response.get_json()["id"],
    }
    assert kwargs["exclude_user_id"] == author["id"]


def test_a_private_faculty_post_stays_off_the_channel(client, make_user, auth_headers, monkeypatch):
    calls = _capture_push(monkeypatch)
    headers = auth_headers(make_user(role="admin"))

    _post_raw(client, "/api/news", {"content": "Turinys", "is_public": False}, headers)

    assert calls == []


def test_a_public_student_wall_post_stays_off_the_channel(client, actor, monkeypatch):
    calls = _capture_push(monkeypatch)

    _post_raw(client, "/api/news", {"content": "Turinys", "is_public": True}, actor[1])

    assert calls == []


def test_a_private_student_wall_post_stays_off_the_channel(client, actor, monkeypatch):
    calls = _capture_push(monkeypatch)

    _post_raw(client, "/api/news", {"content": "Turinys", "is_public": False}, actor[1])

    assert calls == []


def test_a_failing_push_is_swallowed_and_the_post_still_lands(client, db, make_user, auth_headers, monkeypatch):
    import app.notifications.push as push

    def _boom(*args, **kwargs):
        raise RuntimeError("expo is down")

    monkeypatch.setattr(push, "notify_channel", _boom)
    headers = auth_headers(make_user(role="teacher"))

    response = _post_raw(client, "/api/news", {"content": "Turinys"}, headers)

    assert response.status_code == 201
    assert db.execute(
        "SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (response.get_json()["id"],)
    ).fetchone()["c"] == 1


def test_a_failing_push_is_logged_rather_than_silently_dropped(client, make_user, auth_headers, monkeypatch, caplog):
    import app.notifications.push as push

    def _boom(*args, **kwargs):
        raise RuntimeError("expo is down")

    monkeypatch.setattr(push, "notify_channel", _boom)
    headers = auth_headers(make_user(role="teacher"))

    with caplog.at_level("ERROR", logger="app.news.routes"):
        _post_raw(client, "/api/news", {"content": "Turinys"}, headers)

    assert any("Failed to push" in record.message for record in caplog.records)








# ===========================================================
#  create_post — the write quota
# ===========================================================

def test_a_rejected_body_still_spends_a_slot_of_the_write_quota(client, make_user, auth_headers):
    # The decorator runs BEFORE the handler, so twenty 400s are
    # twenty attempts and the first good post is already too late
    headers = auth_headers(make_user())

    for _ in range(20):
        assert _post_raw(client, "/api/news", {"content": ""}, headers).status_code == 400

    response = _post_raw(client, "/api/news", {"content": "Turinys"}, headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_the_write_quota_is_counted_per_caller(client, make_user, auth_headers):
    spent = auth_headers(make_user())
    fresh = auth_headers(make_user())
    for _ in range(20):
        _post_raw(client, "/api/news", {"content": "Turinys"}, spent)

    assert _post_raw(client, "/api/news", {"content": "Turinys"}, spent).status_code == 429
    assert _post_raw(client, "/api/news", {"content": "Turinys"}, fresh).status_code == 201


def test_the_write_quota_refills_once_its_window_has_passed(client, make_user, auth_headers):
    headers = auth_headers(make_user())

    with time_machine.travel("2026-03-01 12:00:00 +0000", tick=False) as traveller:
        for _ in range(20):
            assert _post_raw(client, "/api/news", {"content": "Turinys"}, headers).status_code == 201
        assert _post_raw(client, "/api/news", {"content": "Turinys"}, headers).status_code == 429

        # The window is 300 s; one second past it the bucket has
        # pruned itself empty
        traveller.shift(301)

        assert _post_raw(client, "/api/news", {"content": "Turinys"}, headers).status_code == 201








# ===========================================================
#  get_post — the missing row and the gated row
# ===========================================================

def test_an_unknown_post_id_answers_the_routes_own_404_body(client):
    response = client.get(f"/api/news/{uuid.uuid4()}")

    assert response.status_code == 404
    # Not the app-wide {"error": "Not found"} — the route owns
    # this message, and the gate below reuses it verbatim
    assert response.get_json()["error"] == "Post not found"


@pytest.mark.parametrize("weird_id", [
    "not-a-uuid", "0", "%20", "a" * 500, "..", "null", "1 OR 1=1",
])
def test_a_nonsense_post_id_is_a_plain_404(client, weird_id):
    response = client.get(f"/api/news/{weird_id}")

    assert response.status_code == 404


def test_a_post_id_is_matched_case_sensitively(client, db):
    post_id = _seed_post(db, id="AbCdEf")

    assert client.get(f"/api/news/{post_id}").status_code == 200
    assert client.get("/api/news/abcdef").status_code == 404


def test_a_hidden_post_is_indistinguishable_from_a_missing_one(client, db, make_user):
    author = make_user()
    hidden = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    hidden_response = client.get(f"/api/news/{hidden}")
    missing_response = client.get(f"/api/news/{uuid.uuid4()}")

    assert hidden_response.status_code == missing_response.status_code == 404
    assert hidden_response.get_json() == missing_response.get_json()


def test_an_invalid_token_reads_a_private_post_as_a_guest_does(client, db, make_user):
    author = make_user()
    hidden = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    response = client.get(
        f"/api/news/{hidden}", headers={"Authorization": f"Bearer {uuid.uuid4()}"}
    )

    assert response.status_code == 404








# ===========================================================
#  get_post — the author name COALESCE
# ===========================================================

def test_a_scraped_row_falls_back_to_its_author_name_snapshot(client, db):
    post_id = _seed_post(db, author_id=None, author_name="knf.vu.lt", source="knf.vu.lt",
                         source_url="https://knf.vu.lt/a")

    body = client.get(f"/api/news/{post_id}").get_json()

    assert body["author"] == "knf.vu.lt"
    assert body["authorId"] is None


def test_a_row_with_neither_a_join_nor_a_snapshot_serves_a_null_author(client, db):
    post_id = _seed_post(db, author_id=None, author_name=None)

    assert client.get(f"/api/news/{post_id}").get_json()["author"] is None


def test_a_dangling_author_id_falls_back_to_the_snapshot(client, db):
    # The LEFT JOIN finds nothing, so COALESCE reaches the
    # author_name column the INSERT froze
    post_id = _seed_post(db, author_id=str(uuid.uuid4()), author_name="Dinges Autorius")

    body = client.get(f"/api/news/{post_id}").get_json()

    assert body["author"] == "Dinges Autorius"


def test_a_live_author_id_wins_over_the_stale_snapshot(client, db, make_user):
    author = make_user(display_name="Dabartinis Vardas")
    post_id = _seed_post(db, author_id=author["id"], author_name="Senas Vardas")

    assert client.get(f"/api/news/{post_id}").get_json()["author"] == "Dabartinis Vardas"








# ===========================================================
#  get_post — the counters and the isPublic coercion
# ===========================================================

def test_the_counters_are_served_off_the_row_not_recounted(client, db, actor):
    # _post_to_dict reads the denormalised columns; the writes in
    # this module are what heal them, and a read must not paper
    # over a drift a test is looking for
    post_id = _seed_post(db, likes_count=7, comments_count=3, shares_count=11)

    body = client.get(f"/api/news/{post_id}", headers=actor[1]).get_json()

    assert (body["likes"], body["comments"], body["shares"]) == (7, 3, 11)


def test_a_public_row_reports_is_public_as_a_real_boolean(client, db):
    assert client.get(f"/api/news/{_seed_post(db, is_public=1)}").get_json()["isPublic"] is True


def test_a_truthy_but_not_one_is_public_still_reads_as_public(client, db):
    # bool() over the raw column, so any non-zero legacy value is
    # public — and visible to a guest
    post_id = _seed_post(db, is_public=2)

    response = client.get(f"/api/news/{post_id}")

    assert response.status_code == 200
    assert response.get_json()["isPublic"] is True


def test_a_private_row_reports_is_public_false_to_its_author(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=headers).get_json()["isPublic"] is False








# ===========================================================
#  get_post — the viewer's liked flag
# ===========================================================

def test_a_guest_is_never_liked(client, db):
    post_id = _seed_post(db)

    assert client.get(f"/api/news/{post_id}").get_json()["liked"] is False


def test_a_signed_in_viewer_who_has_not_liked_reads_false(client, db, actor):
    post_id = _seed_post(db)

    assert client.get(f"/api/news/{post_id}", headers=actor[1]).get_json()["liked"] is False


def test_the_viewers_own_like_turns_the_flag_true(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db)
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], post_id))
    db.commit()

    assert client.get(f"/api/news/{post_id}", headers=headers).get_json()["liked"] is True


def test_somebody_elses_like_leaves_the_flag_false(client, db, actor, make_user):
    stranger = make_user()
    post_id = _seed_post(db)
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (stranger["id"], post_id))
    db.commit()

    assert client.get(f"/api/news/{post_id}", headers=actor[1]).get_json()["liked"] is False


def test_a_like_on_another_post_does_not_leak_into_this_one(client, db, actor):
    user, headers = actor
    liked = _seed_post(db)
    other = _seed_post(db)
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], liked))
    db.commit()

    assert client.get(f"/api/news/{other}", headers=headers).get_json()["liked"] is False








# ===========================================================
#  get_post — the inline poll, both arms
# ===========================================================

def test_a_poll_post_carries_its_poll_inline(client, db, actor):
    post_id = _seed_post(db, post_type="poll")
    poll_id, option_ids = _seed_poll(db, post_id)

    body = client.get(f"/api/news/{post_id}", headers=actor[1]).get_json()

    assert body["poll"]["id"] == poll_id
    assert body["poll"]["postId"] == post_id
    assert [o["id"] for o in body["poll"]["options"]] == option_ids
    assert body["poll"]["userVote"] is None


def test_a_poll_post_with_no_poll_row_simply_omits_the_key(client, db):
    post_id = _seed_post(db, post_type="poll")

    assert "poll" not in client.get(f"/api/news/{post_id}").get_json()


def test_a_non_poll_post_never_carries_a_poll_even_when_one_is_attached(client, db):
    # The block is gated on post_type, not on the polls table, so
    # an orphan poll row stays invisible
    post_id = _seed_post(db, post_type="article")
    _seed_poll(db, post_id)

    assert "poll" not in client.get(f"/api/news/{post_id}").get_json()


def test_a_guest_reading_a_poll_post_gets_no_user_vote(client, db):
    post_id = _seed_post(db, post_type="poll")
    _seed_poll(db, post_id)

    assert client.get(f"/api/news/{post_id}").get_json()["poll"]["userVote"] is None


def test_the_viewers_own_vote_travels_with_the_post(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, post_type="poll")
    poll_id, option_ids = _seed_poll(db, post_id)
    db.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
        (user["id"], poll_id, option_ids[1]),
    )
    db.commit()

    body = client.get(f"/api/news/{post_id}", headers=headers).get_json()

    assert body["poll"]["userVote"] == option_ids[1]








# ===========================================================
#  get_post — the visibility gate, every arm through the route
# ===========================================================

def test_a_guest_reads_a_public_post(client, db):
    assert client.get(f"/api/news/{_seed_post(db)}").status_code == 200


def test_a_guest_is_refused_a_private_faculty_draft(client, db, make_user):
    author = make_user(role="teacher")
    post_id = _seed_post(db, author_id=author["id"], source="faculty",
                         post_type="announcement", is_public=0)

    assert client.get(f"/api/news/{post_id}").status_code == 404


def test_the_author_reads_their_own_private_post(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=headers).status_code == 200


def test_an_admin_reads_any_private_wall_post(client, db, admin, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=admin[1]).status_code == 200


def test_an_admin_reads_a_private_scraped_row_with_no_author(client, db, admin):
    post_id = _seed_post(db, author_id=None, source="knf.vu.lt",
                         source_url="https://knf.vu.lt/a", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=admin[1]).status_code == 200


@pytest.mark.parametrize("role", NON_ADMIN_STAFF)
def test_staff_proof_read_a_private_non_wall_row(client, db, make_user, auth_headers, role):
    author = make_user(role="admin")
    post_id = _seed_post(db, author_id=author["id"], source="faculty",
                         post_type="announcement", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=auth_headers(make_user(role=role))).status_code == 200


def test_a_student_is_refused_a_private_faculty_draft(client, db, actor, make_user):
    author = make_user(role="teacher")
    post_id = _seed_post(db, author_id=author["id"], source="faculty",
                         post_type="announcement", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=actor[1]).status_code == 404


def test_a_friend_reads_a_private_wall_post(client, db, make_user, auth_headers):
    author = make_user()
    viewer = make_user()
    _befriend(db, author["id"], viewer["id"])
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=auth_headers(viewer)).status_code == 200


def test_a_stranger_is_refused_a_private_wall_post(client, db, actor, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=actor[1]).status_code == 404


@pytest.mark.parametrize("role", NON_ADMIN_STAFF)
def test_staff_get_no_privilege_over_a_private_wall_post(client, db, make_user, auth_headers, role):
    # source 'user' takes the friendship branch, and being staff
    # is not a friendship
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=auth_headers(make_user(role=role))).status_code == 404


def test_a_public_wall_post_of_a_stranger_is_still_readable_by_id(client, db, actor, make_user):
    # get_post is not the feed: the friends-only narrowing lives
    # in get_feed's WHERE, and a public row is public here
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=1)

    assert client.get(f"/api/news/{post_id}", headers=actor[1]).status_code == 200








# ===========================================================
#  _can_view_post — the arms no route can reach
# ===========================================================

def test_the_gate_opens_on_any_truthy_is_public_before_looking_at_the_user(app, db, make_user):
    stranger = _user_row(db, make_user()["id"])
    post_id = _seed_post(db, is_public=2, source="user", author_id=make_user()["id"])

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), stranger) is True


def test_the_gate_shuts_on_a_guest_before_any_query_runs(app, db, make_user):
    post_id = _seed_post(db, is_public=0, source="user", author_id=make_user()["id"])

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), None) is False


def test_a_private_wall_post_with_no_author_is_closed_to_a_member(app, db, make_user):
    # author_id NULL is falsy, so the ownership arm short-circuits
    # and the friendship lookup runs with a NULL friend_id, which
    # can never match
    viewer = _user_row(db, make_user()["id"])
    post_id = _seed_post(db, is_public=0, source="user", author_id=None)

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), viewer) is False


def test_a_private_wall_post_with_no_author_is_still_open_to_an_admin(app, db, make_user):
    admin_row = _user_row(db, make_user(role="admin")["id"])
    post_id = _seed_post(db, is_public=0, source="user", author_id=None)

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), admin_row) is True


def test_an_empty_string_author_id_never_matches_a_real_viewer(app, db, make_user):
    # "" is falsy, so the ownership arm is skipped even though
    # the comparison would also have failed
    viewer = _user_row(db, make_user()["id"])
    post_id = _seed_post(db, is_public=0, source="faculty", author_id="")

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), viewer) is False


def test_one_friendship_direction_is_enough_for_the_gate(app, db, make_user):
    author = make_user()
    viewer = make_user()
    _follow_one_way(db, viewer["id"], author["id"])
    post_id = _seed_post(db, is_public=0, source="user", author_id=author["id"])

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), _user_row(db, viewer["id"])) is True


def test_the_gate_reads_the_viewer_to_author_direction_only(app, db, make_user):
    # The row the query does NOT read: author→viewer alone must
    # not open the post, or a one-sided leftover would
    author = make_user()
    viewer = make_user()
    _follow_one_way(db, author["id"], viewer["id"])
    post_id = _seed_post(db, is_public=0, source="user", author_id=author["id"])

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), _user_row(db, viewer["id"])) is False


def test_the_author_arm_wins_before_the_role_and_friendship_arms(app, db, make_user):
    author = make_user()
    post_id = _seed_post(db, is_public=0, source="user", author_id=author["id"])

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), _user_row(db, author["id"])) is True


@pytest.mark.parametrize("source", ["app", "knf.vu.lt", "vu.lt", "faculty"])
def test_every_non_wall_source_is_gated_on_the_staff_roles(app, db, make_user, source):
    post_id = _seed_post(db, is_public=0, source=source,
                         source_url=None if source in ("app", "faculty") else f"https://x/{source}",
                         author_id=make_user()["id"])

    with app.app_context():
        row = _gate_row(db, post_id)
        for role in NON_ADMIN_STAFF:
            assert _can_view_post(db, row, _user_row(db, make_user(role=role)["id"])) is True
        assert _can_view_post(db, row, _user_row(db, make_user(role="student")["id"])) is False


def test_the_gate_returns_a_real_boolean_on_the_friendship_arm(app, db, make_user):
    # bool() around the fetchone(), so a caller can compare with
    # `is False` rather than against a row object
    author = make_user()
    viewer = make_user()
    post_id = _seed_post(db, is_public=0, source="user", author_id=author["id"])

    with app.app_context():
        assert _can_view_post(db, _gate_row(db, post_id), _user_row(db, viewer["id"])) is False
        _befriend(db, author["id"], viewer["id"])
        assert _can_view_post(db, _gate_row(db, post_id), _user_row(db, viewer["id"])) is True








# ===========================================================
#  delete_post — authentication and the ownership gate
# ===========================================================

def test_deleting_without_a_token_is_a_401(client, db):
    post_id = _seed_post(db)

    response = client.delete(f"/api/news/{post_id}")

    assert response.status_code == 401
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 1


def test_deleting_with_a_bogus_token_is_a_401(client, db):
    post_id = _seed_post(db)

    response = client.delete(
        f"/api/news/{post_id}", headers={"Authorization": f"Bearer {uuid.uuid4()}"}
    )

    assert response.status_code == 401


def test_deleting_an_unknown_post_is_a_404(client, actor):
    response = client.delete(f"/api/news/{uuid.uuid4()}", headers=actor[1])

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_a_stranger_is_told_403_and_the_post_survives(client, db, actor, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social")

    response = client.delete(f"/api/news/{post_id}", headers=actor[1])

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only the post author or an admin can delete this post"
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 1


@pytest.mark.parametrize("role", NON_ADMIN_STAFF)
def test_staff_short_of_admin_cannot_delete_someone_elses_post(client, db, make_user, auth_headers, role):
    # Staff proof-READ a private draft; deleting is author-or-
    # admin only, and the two gates are deliberately different
    author = make_user(role="admin")
    post_id = _seed_post(db, author_id=author["id"], source="faculty", post_type="announcement")

    assert client.delete(
        f"/api/news/{post_id}", headers=auth_headers(make_user(role=role))
    ).status_code == 403


def test_a_non_admin_cannot_delete_an_authorless_scraped_article(client, db, actor):
    post_id = _seed_post(db, author_id=None, source="knf.vu.lt", source_url="https://knf.vu.lt/a")

    assert client.delete(f"/api/news/{post_id}", headers=actor[1]).status_code == 403


def test_the_author_deletes_their_own_post(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social")

    response = client.delete(f"/api/news/{post_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted"}
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 0


def test_the_author_deletes_their_own_private_post_too(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", is_public=0)

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200


def test_an_admin_deletes_someone_elses_post(client, db, admin, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social")

    assert client.delete(f"/api/news/{post_id}", headers=admin[1]).status_code == 200


def test_an_admin_deletes_an_authorless_scraped_article(client, db, admin):
    post_id = _seed_post(db, author_id=None, source="knf.vu.lt", source_url="https://knf.vu.lt/a")

    assert client.delete(f"/api/news/{post_id}", headers=admin[1]).status_code == 200


def test_an_admin_deleting_their_own_post_takes_the_author_branch(client, db, admin, caplog):
    user, headers = admin
    post_id = _seed_post(db, author_id=user["id"], source="faculty", post_type="announcement")

    with caplog.at_level("INFO", logger="app.news.routes"):
        assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    assert any("deleted by its author" in record.message for record in caplog.records)


def test_an_admin_deleting_a_foreign_post_is_logged_as_a_warning(client, db, admin, make_user, caplog):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social")

    with caplog.at_level("WARNING", logger="app.news.routes"):
        client.delete(f"/api/news/{post_id}", headers=admin[1])

    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_a_created_post_can_be_deleted_by_its_creator_end_to_end(client, actor):
    post_id = _post_raw(client, "/api/news", {"content": "Turinys"}, actor[1]).get_json()["id"]

    assert client.delete(f"/api/news/{post_id}", headers=actor[1]).status_code == 200
    assert client.get(f"/api/news/{post_id}").status_code == 404


def test_deleting_the_same_post_twice_is_a_404_the_second_time(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social")

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200
    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 404








# ===========================================================
#  delete_post — the deleted_source_urls tombstone
# ===========================================================

def test_deleting_a_scraped_article_tombstones_its_url(client, db, admin):
    user, headers = admin
    post_id = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/naujiena")

    with time_machine.travel("2026-03-01 12:00:00 +0000", tick=False):
        assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    row = db.execute(
        "SELECT deleted_by, deleted_at FROM deleted_source_urls WHERE source_url = ?",
        ("https://knf.vu.lt/naujiena",),
    ).fetchone()
    assert row["deleted_by"] == user["id"]
    assert row["deleted_at"] == "2026-03-01T12:00:00+00:00"


def test_a_post_without_a_source_url_writes_no_tombstone(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", source_url=None)

    client.delete(f"/api/news/{post_id}", headers=headers)

    assert db.execute("SELECT COUNT(*) AS c FROM deleted_source_urls").fetchone()["c"] == 0


def test_an_empty_source_url_writes_no_tombstone_either(client, db, actor):
    # "" is falsy, so the tombstone branch is skipped — a blank
    # URL is nothing for a scraper to dedupe against anyway
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", source_url="")

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) AS c FROM deleted_source_urls").fetchone()["c"] == 0


def test_an_existing_tombstone_keeps_its_first_deleter(client, db, admin, make_user):
    other = make_user(role="admin")
    db.execute(
        "INSERT INTO deleted_source_urls (source_url, deleted_by, deleted_at) VALUES (?, ?, ?)",
        ("https://knf.vu.lt/a", other["id"], "2020-01-01T00:00:00+00:00"),
    )
    db.commit()
    post_id = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a")

    assert client.delete(f"/api/news/{post_id}", headers=admin[1]).status_code == 200

    rows = db.execute("SELECT deleted_by, deleted_at FROM deleted_source_urls").fetchall()
    assert len(rows) == 1
    assert rows[0]["deleted_by"] == other["id"]
    assert rows[0]["deleted_at"] == "2020-01-01T00:00:00+00:00"


def test_two_scraped_articles_tombstone_two_separate_urls(client, db, admin):
    first = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a")
    second = _seed_post(db, source="vu.lt", source_url="https://vu.lt/b")

    client.delete(f"/api/news/{first}", headers=admin[1])
    client.delete(f"/api/news/{second}", headers=admin[1])

    assert db.execute("SELECT COUNT(*) AS c FROM deleted_source_urls").fetchone()["c"] == 2








# ===========================================================
#  delete_post — the dependant sweep
# ===========================================================

def test_deleting_a_post_sweeps_its_likes_and_comments(client, db, actor, make_user):
    user, headers = actor
    other = make_user()
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social")
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (other["id"], post_id))
    db.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text) VALUES (?, ?, ?, 'labas')",
        (str(uuid.uuid4()), post_id, other["id"]),
    )
    db.commit()

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    assert db.execute("SELECT COUNT(*) AS c FROM news_likes").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 0


def test_a_neighbouring_posts_children_are_left_alone(client, db, actor):
    user, headers = actor
    doomed = _seed_post(db, author_id=user["id"], source="user", post_type="social")
    keeper = _seed_post(db, author_id=user["id"], source="user", post_type="social")
    for post_id in (doomed, keeper):
        db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], post_id))
    db.commit()

    client.delete(f"/api/news/{doomed}", headers=headers)

    assert db.execute(
        "SELECT COUNT(*) AS c FROM news_likes WHERE post_id = ?", (keeper,)
    ).fetchone()["c"] == 1


def test_deleting_a_poll_post_sweeps_the_poll_its_options_and_its_votes(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="poll")
    poll_id, option_ids = _seed_poll(db, post_id)
    db.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
        (user["id"], poll_id, option_ids[0]),
    )
    db.commit()

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    assert db.execute("SELECT COUNT(*) AS c FROM polls").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM poll_options").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM poll_votes").fetchone()["c"] == 0


def test_a_poll_with_no_options_and_no_votes_still_deletes(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="poll")
    _seed_poll(db, post_id, options=())

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) AS c FROM polls").fetchone()["c"] == 0


def test_a_post_with_no_poll_takes_the_empty_sweep_loop(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social")

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200


def test_another_posts_poll_survives_the_sweep(client, db, actor):
    user, headers = actor
    doomed = _seed_post(db, author_id=user["id"], source="user", post_type="poll")
    keeper = _seed_post(db, author_id=user["id"], source="user", post_type="poll")
    _seed_poll(db, doomed)
    keeper_poll, _ = _seed_poll(db, keeper)

    client.delete(f"/api/news/{doomed}", headers=headers)

    assert db.execute("SELECT COUNT(*) AS c FROM polls WHERE id = ?", (keeper_poll,)).fetchone()["c"] == 1








# ===========================================================
#  delete_post — the cover file, after the commit
# ===========================================================

# -----------------------------------------------------------
# _watch_upload_deleter
# -----------------------------------------------------------
#
# delete_upload is imported INSIDE delete_post, so the patch
# goes on app.uploads.routes. Returns the list of paths the
# route actually handed over, which is the whole point: a
# foreign URL must never reach it.
# -----------------------------------------------------------

def _watch_upload_deleter(monkeypatch):
    import app.uploads.routes as uploads_routes

    seen = []
    monkeypatch.setattr(uploads_routes, "delete_upload", lambda path: seen.append(path) or True)
    return seen


def test_our_own_cover_path_is_handed_to_the_uploads_deleter(client, db, actor, monkeypatch):
    seen = _watch_upload_deleter(monkeypatch)
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social",
                         image_url="/api/uploads/" + "ab12cd34" * 4 + ".jpg")

    client.delete(f"/api/news/{post_id}", headers=headers)

    assert seen == ["/api/uploads/" + "ab12cd34" * 4 + ".jpg"]


def test_a_post_with_no_cover_never_calls_the_deleter(client, db, actor, monkeypatch):
    seen = _watch_upload_deleter(monkeypatch)
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", image_url=None)

    client.delete(f"/api/news/{post_id}", headers=headers)

    assert seen == []


def test_a_foreign_cover_url_never_calls_the_deleter(client, db, admin, monkeypatch):
    seen = _watch_upload_deleter(monkeypatch)
    post_id = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a",
                         image_url="https://knf.vu.lt/images/cover.jpg")

    client.delete(f"/api/news/{post_id}", headers=admin[1])

    assert seen == []


def test_a_non_string_cover_value_never_calls_the_deleter(client, db, admin, monkeypatch):
    # isinstance(..., str) guards the call, so a legacy numeric
    # column value cannot raise inside the delete
    seen = _watch_upload_deleter(monkeypatch)
    post_id = _seed_post(db, source="app", image_url=None)
    db.execute("UPDATE news_posts SET image_url = 5 WHERE id = ?", (post_id,))
    db.commit()

    assert client.delete(f"/api/news/{post_id}", headers=admin[1]).status_code == 200
    assert seen == []


def test_a_failing_cover_delete_still_reports_the_post_deleted(client, db, actor, monkeypatch):
    import app.uploads.routes as uploads_routes

    def _boom(path):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(uploads_routes, "delete_upload", _boom)
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social",
                         image_url="/api/uploads/" + "deadbeef" * 4 + ".jpg")

    response = client.delete(f"/api/news/{post_id}", headers=headers)

    assert response.status_code == 200
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 0


def test_a_failing_cover_delete_is_logged_as_a_warning(client, db, actor, monkeypatch, caplog):
    import app.uploads.routes as uploads_routes

    monkeypatch.setattr(uploads_routes, "delete_upload", lambda path: (_ for _ in ()).throw(OSError("nope")))
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social",
                         image_url="/api/uploads/" + "deadbeef" * 4 + ".jpg")

    with caplog.at_level("WARNING", logger="app.news.routes"):
        client.delete(f"/api/news/{post_id}", headers=headers)

    assert any("Could not delete upload" in record.message for record in caplog.records)


def test_the_real_upload_file_and_its_row_go_with_the_post(app, client, db, actor, monkeypatch):
    import app.uploads.routes as uploads_routes

    # _upload_dir is a module-level cache resolved once per
    # process, so it is reset for this test or the deleter would
    # look inside whichever test ran first
    monkeypatch.setattr(uploads_routes, "_upload_dir", None)
    user, headers = actor
    name = "ab12cd34" * 4 + ".jpg"
    path = os.path.join(app.config["UPLOAD_DIR"], name)
    with open(path, "wb") as handle:
        handle.write(b"not-really-a-jpeg")
    db.execute(
        "INSERT INTO uploads (id, filename, user_id, byte_size) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), name, user["id"], 17),
    )
    db.commit()
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social",
                         image_url=f"/api/uploads/{name}")

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    assert not os.path.exists(path)
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 0








# ===========================================================
#  delete_post — the delete quota
# ===========================================================

def test_the_sixty_first_delete_in_the_window_is_refused(client, actor):
    # A 404 still spends a slot: the decorator runs before the
    # handler, which is what stops an id-enumeration sweep
    _, headers = actor

    for _ in range(60):
        assert client.delete(f"/api/news/{uuid.uuid4()}", headers=headers).status_code == 404

    response = client.delete(f"/api/news/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_a_spent_delete_quota_does_not_touch_another_caller(client, db, make_user, auth_headers):
    spender = make_user()
    spent = auth_headers(spender)
    for _ in range(60):
        client.delete(f"/api/news/{uuid.uuid4()}", headers=spent)
    assert client.delete(f"/api/news/{uuid.uuid4()}", headers=spent).status_code == 429

    other = make_user()
    post_id = _seed_post(db, author_id=other["id"], source="user", post_type="social")

    assert client.delete(f"/api/news/{post_id}", headers=auth_headers(other)).status_code == 200
