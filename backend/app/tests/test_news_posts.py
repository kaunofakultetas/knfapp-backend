# -----------------------------------------------------------
#  [*] Tests — news post lifecycle
#
#  What this module proves about app/news/routes.py, the five
#  routes a post travels through from creation to deletion:
#
#    POST   /api/news              — the role picks the source
#                                    (STAFF_ROLES publish as
#                                    'faculty', everyone else
#                                    as a 'user' wall post),
#                                    every field is typed and
#                                    bounded, and image_url may
#                                    ONLY be one of our own
#                                    /api/uploads/ paths — an
#                                    absolute URL would turn
#                                    every reader into a beacon
#    GET    /api/news/<id>         — one post plus the viewer's
#                                    `liked` flag, gated by
#                                    _can_view_post: a hidden
#                                    row answers 404, never 403,
#                                    so existence cannot leak
#    DELETE /api/news/<id>         — author or admin only, its
#                                    likes/comments/poll swept,
#                                    a scraped source_url
#                                    tombstoned so the next
#                                    scrape cannot resurrect it,
#                                    and the cover upload
#                                    unlinked after the commit
#    POST   /api/news/<id>/like    — a toggle whose counter is
#                                    RECOMPUTED from news_likes,
#                                    so it heals drift instead
#                                    of compounding it
#    POST   /api/news/<id>/share   — the one write with no auth
#                                    at all, counting up only
#
#  The wire shapes marked `contract` are the ones
#  mobile/app/services/api/news.ts and types/index.ts consume
#  (NewsPost / NewsPostDetail / LikeResponse / ShareResponse) —
#  a failure there breaks the shipped app.
#
#  Note on assertions: app/__init__.py html-escapes every
#  string on the way OUT, so the fixtures here stay on plain
#  ASCII text and never assert on a quote or an angle bracket.
# -----------------------------------------------------------

import os
import uuid
from datetime import datetime, timezone

import pytest

from app.news.routes import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    POST_TYPES,
    STAFF_ROLES,
)

# Exactly the keys _post_to_dict produces, plus the `liked`
# flag the two single-post paths attach. Mirrors the mobile
# NewsPost + NewsPostDetail types.
POST_KEYS = {
    "id", "title", "content", "summary", "imageUrl", "author", "authorId",
    "source", "sourceUrl", "postType", "likes", "comments", "shares",
    "date", "isPublic", "liked",
}




# -----------------------------------------------------------
# _seed_post
# -----------------------------------------------------------
#
# One news_posts row written straight to the database, for the
# states no route can create: a scraped article (author_id
# NULL, a source_url), a private faculty draft, a foreign
# image_url, a drifted counter. Returns the new post id.
#
# published_at/created_at/updated_at share one stamp in the
# house T-form shape, so the feed's julianday() ranking and
# the string cursors keep agreeing with production rows.
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
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    row.update(overrides)

    db.execute(
        """INSERT INTO news_posts
           (id, title, content, summary, image_url, author_id, author_name,
            source, source_url, post_type, is_public, likes_count,
            published_at, created_at, updated_at)
           VALUES (:id, :title, :content, :summary, :image_url, :author_id, :author_name,
                   :source, :source_url, :post_type, :is_public, :likes_count,
                   :published_at, :published_at, :published_at)""",
        row,
    )
    db.commit()
    return row["id"]




# -----------------------------------------------------------
# _seed_poll
# -----------------------------------------------------------
#
# A poll with its options hung on an existing post, for
# get_post's inline `poll` object and delete_post's sweep.
# Returns (poll_id, [option_id, ...]) in creation order, which
# is the rowid order _polls_for_posts serves them in.
# -----------------------------------------------------------

def _seed_poll(db, post_id, options=("Taip", "Ne"), end_date=None):
    poll_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO polls (id, post_id, title, end_date, total_votes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (poll_id, post_id, "Ar ateisi", end_date, 0, datetime.now(timezone.utc).isoformat()),
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
# _befriend
# -----------------------------------------------------------
#
# friendships is written in BOTH directions on accept
# (social/routes.py), and _can_view_post trusts that — so the
# fixture has to write both too, or the private-wall-post gate
# would be tested against a shape production never stores.
# -----------------------------------------------------------

def _befriend(db, first_id, second_id):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (first_id, second_id))
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (second_id, first_id))
    db.commit()




# -----------------------------------------------------------
# _create
# -----------------------------------------------------------
#
# POST /api/news with a body that already carries the one
# required field, so a test names only what it is about.
# -----------------------------------------------------------

def _create(client, headers, **body):
    payload = {"content": "Turinys"}
    payload.update(body)
    return client.post("/api/news", json=payload, headers=headers)








# ===========================================================
#  create_post — the caller's role decides the source
# ===========================================================

def test_a_student_post_is_stored_as_a_user_wall_post(client, actor):
    _, headers = actor

    response = _create(client, headers)

    assert response.status_code == 201
    assert response.get_json()["source"] == "user"


def test_a_student_post_defaults_to_the_social_post_type(client, actor):
    _, headers = actor

    assert _create(client, headers).get_json()["postType"] == "social"


@pytest.mark.parametrize("role", STAFF_ROLES)
def test_every_staff_role_publishes_as_faculty(client, make_user, auth_headers, role):
    staff = make_user(role=role)

    response = _create(client, auth_headers(staff))

    assert response.status_code == 201
    assert response.get_json()["source"] == "faculty"


def test_a_staff_post_defaults_to_an_announcement(client, make_user, auth_headers):
    teacher = make_user(role="teacher")

    assert _create(client, auth_headers(teacher)).get_json()["postType"] == "announcement"


def test_an_explicit_post_type_survives_the_role_default(client, actor):
    _, headers = actor

    body = _create(client, headers, post_type="link").get_json()

    assert body["postType"] == "link"
    assert body["source"] == "user"


def test_a_blank_post_type_falls_back_to_the_role_default(client, actor):
    _, headers = actor

    assert _create(client, headers, post_type="").get_json()["postType"] == "social"


def test_a_students_role_cannot_be_talked_into_a_faculty_source(client, actor):
    # source is never read off the body — only off the role
    _, headers = actor

    assert _create(client, headers, source="faculty").get_json()["source"] == "user"








# ===========================================================
#  create_post — body typing and the post_type allow-list
# ===========================================================

def test_creating_a_post_requires_authentication(client):
    response = client.post("/api/news", json={"content": "Turinys"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_an_empty_body_object_is_refused(client, actor):
    _, headers = actor

    response = client.post("/api/news", json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_content_must_be_a_string(client, actor):
    _, headers = actor

    response = client.post("/api/news", json={"content": 42}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_a_missing_content_is_refused(client, actor):
    _, headers = actor

    response = client.post("/api/news", json={"title": "Antraste"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Content required"


def test_a_whitespace_only_content_is_refused(client, actor):
    _, headers = actor

    response = client.post("/api/news", json={"content": "   \n\t "}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Content required"


def test_title_must_be_a_string(client, actor):
    _, headers = actor

    response = _create(client, headers, title=["Antraste"])

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_an_unknown_post_type_is_refused(client, actor):
    _, headers = actor

    response = _create(client, headers, post_type="video")

    assert response.status_code == 400
    assert "post_type must be one of" in response.get_json()["error"]


def test_a_client_cannot_mint_a_poll_card_with_no_poll_behind_it(client, actor):
    # 'poll' is deliberately missing from POST_TYPES — only
    # create_poll's server-side flip may set it
    _, headers = actor

    response = _create(client, headers, post_type="poll")

    assert response.status_code == 400
    assert "poll" not in POST_TYPES


def test_a_non_string_post_type_is_refused(client, actor):
    _, headers = actor

    assert _create(client, headers, post_type=7).status_code == 400


@pytest.mark.parametrize("post_type", POST_TYPES)
def test_every_allowed_post_type_is_accepted(client, actor, post_type):
    _, headers = actor

    response = _create(client, headers, post_type=post_type)

    assert response.status_code == 201
    assert response.get_json()["postType"] == post_type


def test_is_public_must_be_a_real_boolean(client, actor):
    # The truthy string "false" used to be stored public AND
    # echoed back verbatim as a string
    _, headers = actor

    response = _create(client, headers, is_public="false")

    assert response.status_code == 400
    assert response.get_json()["error"] == "is_public must be a boolean"


def test_is_public_defaults_to_public(client, actor):
    _, headers = actor

    assert _create(client, headers).get_json()["isPublic"] is True


def test_a_post_can_be_created_private(client, actor, db):
    _, headers = actor

    body = _create(client, headers, is_public=False).get_json()

    assert body["isPublic"] is False
    stored = db.execute("SELECT is_public FROM news_posts WHERE id = ?", (body["id"],)).fetchone()
    assert stored["is_public"] == 0








# ===========================================================
#  create_post — the length caps, at the boundary
# ===========================================================

def test_a_title_of_exactly_the_cap_is_accepted(client, actor):
    _, headers = actor

    response = _create(client, headers, title="t" * MAX_TITLE_LENGTH)

    assert response.status_code == 201
    assert len(response.get_json()["title"]) == MAX_TITLE_LENGTH


def test_a_title_one_character_over_the_cap_is_refused(client, actor):
    _, headers = actor

    response = _create(client, headers, title="t" * (MAX_TITLE_LENGTH + 1))

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Title must be at most {MAX_TITLE_LENGTH} characters"


def test_content_of_exactly_the_cap_is_accepted(client, actor):
    _, headers = actor

    response = _create(client, headers, content="c" * MAX_CONTENT_LENGTH)

    assert response.status_code == 201
    assert len(response.get_json()["content"]) == MAX_CONTENT_LENGTH


def test_content_one_character_over_the_cap_is_refused(client, actor):
    _, headers = actor

    response = _create(client, headers, content="c" * (MAX_CONTENT_LENGTH + 1))

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Content must be at most {MAX_CONTENT_LENGTH} characters"


def test_a_missing_title_falls_back_to_the_first_eighty_characters(client, actor):
    _, headers = actor
    content = "z" * 500

    body = _create(client, headers, content=content).get_json()

    assert body["title"] == content[:80]


def test_a_blank_title_falls_back_to_the_content_too(client, actor):
    _, headers = actor

    body = _create(client, headers, content="Trumpas irasas", title="   ").get_json()

    assert body["title"] == "Trumpas irasas"


def test_the_summary_is_the_first_two_hundred_characters(client, actor):
    _, headers = actor
    content = "y" * 900

    body = _create(client, headers, content=content).get_json()

    assert body["summary"] == content[:200]


def test_title_and_content_are_stored_stripped(client, actor):
    _, headers = actor

    body = _create(client, headers, content="  Turinys  ", title="  Antraste  ").get_json()

    assert body["content"] == "Turinys"
    assert body["title"] == "Antraste"








# ===========================================================
#  create_post — image_url may only be one of our uploads
# ===========================================================

def test_an_own_uploads_path_image_is_accepted(client, actor):
    _, headers = actor

    response = _create(client, headers, image_url="/api/uploads/abc123.jpg")

    assert response.status_code == 201
    assert response.get_json()["imageUrl"] == "/api/uploads/abc123.jpg"


@pytest.mark.parametrize("image_url", [
    "https://evil.example/beacon.png",
    "http://evil.example/beacon.png",
    "//evil.example/beacon.png",
    "/uploads/abc123.jpg",
    "api/uploads/abc123.jpg",
    "javascript:alert(1)",
    " /api/uploads/abc123.jpg",
])
def test_an_image_url_outside_our_uploads_is_refused(client, actor, image_url):
    _, headers = actor

    response = _create(client, headers, image_url=image_url)

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


def test_a_non_string_image_url_is_refused(client, actor):
    _, headers = actor

    response = _create(client, headers, image_url=123)

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


def test_a_null_image_url_leaves_the_post_without_a_cover(client, actor):
    _, headers = actor

    response = _create(client, headers, image_url=None)

    assert response.status_code == 201
    assert response.get_json()["imageUrl"] is None


def test_a_refused_image_url_never_reaches_the_database(client, actor, db):
    _, headers = actor

    _create(client, headers, image_url="https://evil.example/beacon.png")

    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 0








# ===========================================================
#  create_post — what lands in the row, and on the wire
# ===========================================================

@pytest.mark.contract
def test_the_create_response_carries_the_whole_post_shape(client, actor):
    # sourceUrl was once missing from the create response alone
    # — the hand-built body predates _post_to_dict owning it
    _, headers = actor

    body = _create(client, headers).get_json()

    assert set(body) == POST_KEYS
    assert body["sourceUrl"] is None
    assert body["liked"] is False
    assert body["likes"] == body["comments"] == body["shares"] == 0


def test_the_created_post_is_persisted_with_its_authors_identity(client, db, make_user, auth_headers):
    author = make_user(display_name="Ona Onaite")

    body = _create(client, auth_headers(author), content="Sveiki").get_json()

    row = db.execute(
        "SELECT author_id, author_name, source, source_url, published_at, created_at, updated_at"
        " FROM news_posts WHERE id = ?", (body["id"],),
    ).fetchone()
    assert row["author_id"] == author["id"]
    assert row["author_name"] == "Ona Onaite"
    assert row["source"] == "user"
    assert row["source_url"] is None
    assert row["published_at"] == row["created_at"] == row["updated_at"]


def test_the_created_stamp_is_explicit_utc_iso(client, actor):
    _, headers = actor

    date = _create(client, headers).get_json()["date"]

    assert date.endswith("+00:00")
    assert date[10] == "T"


def test_a_new_post_is_immediately_readable_through_get_post(client, actor):
    _, headers = actor

    created = _create(client, headers, content="Perskaityk mane").get_json()
    fetched = client.get(f"/api/news/{created['id']}", headers=headers).get_json()

    assert fetched == created








# ===========================================================
#  create_post — the 'news' push channel
# ===========================================================

# -----------------------------------------------------------
# _capture_push
# -----------------------------------------------------------
#
# Replaces notify_channel on the push module create_post
# imports lazily, and records the calls. No network is
# possible here (the container has none), so the recorder is
# about the DECISION to push, not the delivery.
# -----------------------------------------------------------

def _capture_push(monkeypatch, boom=False):
    import app.notifications.push as push_module

    calls = []

    def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        if boom:
            raise RuntimeError("Expo unreachable")
        return 1

    monkeypatch.setattr(push_module, "notify_channel", _fake)
    return calls


def test_a_public_faculty_post_rings_the_news_channel(client, make_user, auth_headers, monkeypatch):
    calls = _capture_push(monkeypatch)
    teacher = make_user(role="teacher")

    body = _create(client, auth_headers(teacher), content="Paskaita nukeliama", title="Skelbimas").get_json()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "news"
    assert args[1] == "Skelbimas"
    assert kwargs["data"] == {"type": "news", "source": "faculty", "postId": body["id"]}
    assert kwargs["exclude_user_id"] == teacher["id"]


def test_a_private_faculty_post_stays_silent(client, make_user, auth_headers, monkeypatch):
    calls = _capture_push(monkeypatch)
    teacher = make_user(role="teacher")

    _create(client, auth_headers(teacher), is_public=False)

    assert calls == []


def test_a_student_wall_post_never_rings_the_news_channel(client, actor, monkeypatch):
    calls = _capture_push(monkeypatch)
    _, headers = actor

    _create(client, headers)

    assert calls == []


def test_a_failing_push_does_not_fail_the_creation(client, db, make_user, auth_headers, monkeypatch):
    calls = _capture_push(monkeypatch, boom=True)
    admin_user = make_user(role="admin")

    response = _create(client, auth_headers(admin_user), content="Vis tiek paskelbta")

    assert response.status_code == 201
    assert len(calls) == 1
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 1








# ===========================================================
#  create_post — the write quota
# ===========================================================

def test_the_twenty_first_post_in_the_window_is_rate_limited(client, make_user, auth_headers):
    # rate_limit("news_post", max_attempts=20), keyed on the
    # caller's id — a fresh user gets a fresh budget
    prolific = make_user()
    headers = auth_headers(prolific)

    for _ in range(20):
        assert _create(client, headers).status_code == 201

    response = _create(client, headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1








# ===========================================================
#  get_post — the row, its author name and the liked flag
# ===========================================================

def test_an_unknown_post_id_is_a_404(client):
    response = client.get(f"/api/news/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


@pytest.mark.contract
def test_a_guest_reads_a_public_post_with_liked_false(client, db):
    post_id = _seed_post(db, title="Viesa naujiena", source="knf.vu.lt",
                         source_url="https://knf.vu.lt/naujiena")

    response = client.get(f"/api/news/{post_id}")

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == POST_KEYS
    assert body["id"] == post_id
    assert body["liked"] is False
    assert body["isPublic"] is True


def test_a_scraped_row_falls_back_to_its_author_name_snapshot(client, db):
    post_id = _seed_post(db, author_id=None, author_name="knf.vu.lt", source="vu.lt")

    assert client.get(f"/api/news/{post_id}").get_json()["author"] == "knf.vu.lt"


def test_the_author_name_follows_the_users_current_display_name(client, db, make_user):
    # The row's author_name is a snapshot; the LEFT JOIN serves
    # the live one, so a rename shows on every old post
    author = make_user(display_name="Senas Vardas")
    post_id = _seed_post(db, author_id=author["id"], author_name="Senas Vardas",
                         source="user", post_type="social")
    db.execute("UPDATE users SET display_name = 'Naujas Vardas' WHERE id = ?", (author["id"],))
    db.commit()

    assert client.get(f"/api/news/{post_id}").get_json()["author"] == "Naujas Vardas"


def test_the_liked_flag_is_true_once_the_viewer_liked_it(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db)

    client.post(f"/api/news/{post_id}/like", headers=headers)

    assert client.get(f"/api/news/{post_id}", headers=headers).get_json()["liked"] is True


def test_the_liked_flag_stays_false_for_another_viewer(client, db, actor, make_user, auth_headers):
    _, headers = actor
    post_id = _seed_post(db)
    client.post(f"/api/news/{post_id}/like", headers=headers)

    other = make_user()

    assert client.get(f"/api/news/{post_id}", headers=auth_headers(other)).get_json()["liked"] is False


def test_a_poll_post_carries_its_poll_inline(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id)

    body = client.get(f"/api/news/{post_id}", headers=headers).get_json()

    assert body["postType"] == "poll"
    assert [o["id"] for o in body["poll"]["options"]] == option_ids
    assert body["poll"]["userVote"] is None
    assert body["poll"]["totalVotes"] == 0


def test_a_poll_post_with_no_poll_row_omits_the_poll_key(client, db):
    post_id = _seed_post(db, post_type="poll")

    assert "poll" not in client.get(f"/api/news/{post_id}").get_json()








# ===========================================================
#  get_post — the visibility gate is a 404, never a 403
# ===========================================================

def test_a_guest_is_refused_a_private_post_with_the_missing_body(client, db):
    post_id = _seed_post(db, is_public=0)

    response = client.get(f"/api/news/{post_id}")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_the_author_reads_their_own_private_post(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=headers).status_code == 200


def test_an_admin_reads_someone_elses_private_post(client, db, admin, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=admin[1]).status_code == 200


@pytest.mark.parametrize("role", ["teacher", "curator"])
def test_staff_proof_read_a_private_faculty_draft(client, db, make_user, auth_headers, role):
    author = make_user(role="teacher")
    post_id = _seed_post(db, author_id=author["id"], source="faculty",
                         post_type="announcement", is_public=0)
    staff = make_user(role=role)

    assert client.get(f"/api/news/{post_id}", headers=auth_headers(staff)).status_code == 200


def test_a_student_is_refused_a_private_faculty_draft(client, db, make_user, actor):
    author = make_user(role="teacher")
    post_id = _seed_post(db, author_id=author["id"], source="faculty",
                         post_type="announcement", is_public=0)
    _, headers = actor

    assert client.get(f"/api/news/{post_id}", headers=headers).status_code == 404


def test_a_friend_reads_a_private_wall_post(client, db, make_user, auth_headers):
    author = make_user()
    friend = make_user()
    _befriend(db, author["id"], friend["id"])
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}", headers=auth_headers(friend)).status_code == 200


def test_a_stranger_is_refused_a_private_wall_post(client, db, make_user, actor):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)
    _, headers = actor

    assert client.get(f"/api/news/{post_id}", headers=headers).status_code == 404


def test_a_teacher_is_refused_a_strangers_private_wall_post(client, db, make_user, auth_headers):
    # STAFF_ROLES proof-read faculty drafts, NOT members' walls
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)
    teacher = make_user(role="teacher")

    assert client.get(f"/api/news/{post_id}", headers=auth_headers(teacher)).status_code == 404








# ===========================================================
#  delete_post — author or admin, and the sweep
# ===========================================================

def test_deleting_a_post_requires_authentication(client, db):
    post_id = _seed_post(db)

    response = client.delete(f"/api/news/{post_id}")

    assert response.status_code == 401
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts").fetchone()["c"] == 1


def test_deleting_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.delete(f"/api/news/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_a_stranger_cannot_delete_someone_elses_post(client, db, make_user, actor):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social")
    _, headers = actor

    response = client.delete(f"/api/news/{post_id}", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only the post author or an admin can delete this post"
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 1


def test_the_author_deletes_their_own_post(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social")

    response = client.delete(f"/api/news/{post_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted"}
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 0


def test_an_admin_deletes_someone_elses_post(client, db, admin, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social")

    assert client.delete(f"/api/news/{post_id}", headers=admin[1]).status_code == 200
    assert db.execute("SELECT COUNT(*) AS c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 0


def test_only_an_admin_can_delete_a_scraped_article(client, db, actor, admin):
    # A scraped row has author_id NULL, so nobody "authored" it
    post_id = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a")
    _, headers = actor

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 403
    assert client.delete(f"/api/news/{post_id}", headers=admin[1]).status_code == 200


def test_deleting_a_post_sweeps_its_likes_and_comments(client, db, actor, make_user, auth_headers):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social")
    other = make_user()
    client.post(f"/api/news/{post_id}/like", headers=auth_headers(other))
    client.post(f"/api/news/{post_id}/comments", json={"text": "Sveiki"}, headers=headers)

    client.delete(f"/api/news/{post_id}", headers=headers)

    assert db.execute("SELECT COUNT(*) AS c FROM news_likes WHERE post_id = ?", (post_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments WHERE post_id = ?", (post_id,)).fetchone()["c"] == 0


def test_deleting_a_post_sweeps_its_poll_options_and_votes(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="poll")
    poll_id, option_ids = _seed_poll(db, post_id)
    db.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], poll_id, option_ids[0], datetime.now(timezone.utc).isoformat()),
    )
    db.commit()

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    assert db.execute("SELECT COUNT(*) AS c FROM polls WHERE id = ?", (poll_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM poll_options WHERE poll_id = ?", (poll_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM poll_votes WHERE poll_id = ?", (poll_id,)).fetchone()["c"] == 0








# ===========================================================
#  delete_post — the deleted_source_urls tombstone
# ===========================================================

def test_deleting_a_scraped_article_tombstones_its_source_url(client, db, admin):
    admin_user, headers = admin
    url = "https://knf.vu.lt/naujienos/istrinta"
    post_id = _seed_post(db, source="knf.vu.lt", source_url=url)

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    row = db.execute("SELECT deleted_by, deleted_at FROM deleted_source_urls WHERE source_url = ?", (url,)).fetchone()
    assert row is not None
    assert row["deleted_by"] == admin_user["id"]
    assert row["deleted_at"].endswith("+00:00")


def test_a_post_without_a_source_url_writes_no_tombstone(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social")

    client.delete(f"/api/news/{post_id}", headers=headers)

    assert db.execute("SELECT COUNT(*) AS c FROM deleted_source_urls").fetchone()["c"] == 0


def test_an_existing_tombstone_is_kept_rather_than_replaced(client, db, admin, make_user):
    # INSERT OR IGNORE: the article came back and was deleted
    # again, and the FIRST deletion still owns the tombstone
    first_remover = make_user(role="admin")
    url = "https://vu.lt/naujienos/vel"
    db.execute(
        "INSERT INTO deleted_source_urls (source_url, deleted_by, deleted_at) VALUES (?, ?, ?)",
        (url, first_remover["id"], "2026-01-01T00:00:00+00:00"),
    )
    db.commit()
    post_id = _seed_post(db, source="vu.lt", source_url=url)

    assert client.delete(f"/api/news/{post_id}", headers=admin[1]).status_code == 200

    rows = db.execute("SELECT deleted_by FROM deleted_source_urls WHERE source_url = ?", (url,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["deleted_by"] == first_remover["id"]








# ===========================================================
#  delete_post — the cover file
# ===========================================================

# -----------------------------------------------------------
# _own_upload
# -----------------------------------------------------------
#
# A real file in this test's UPLOAD_DIR plus its uploads row,
# named the way uploads/routes.py's _FILENAME_RE demands
# (32 hex chars + an image extension) — anything else is
# refused by delete_upload before it touches the disk.
#
# _upload_dir is a MODULE-level cache resolved once per
# process, so it is reset for the duration of the test (and
# restored by monkeypatch afterwards) or the deleter would
# look inside whichever test happened to run first.
# -----------------------------------------------------------

def _own_upload(app, db, monkeypatch, owner_id, name="ab12cd34" * 4 + ".jpg"):
    import app.uploads.routes as uploads_routes

    monkeypatch.setattr(uploads_routes, "_upload_dir", None)

    path = os.path.join(app.config["UPLOAD_DIR"], name)
    with open(path, "wb") as handle:
        handle.write(b"not-really-a-jpeg")

    db.execute(
        "INSERT INTO uploads (id, filename, user_id, byte_size) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), name, owner_id, 17),
    )
    db.commit()
    return f"/api/uploads/{name}", path


def test_deleting_a_post_removes_its_cover_upload(app, client, db, actor, monkeypatch):
    user, headers = actor
    image_url, path = _own_upload(app, db, monkeypatch, user["id"])
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", image_url=image_url)

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    assert not os.path.exists(path)
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 0


def test_a_foreign_cover_url_is_never_handed_to_the_uploads_deleter(client, db, admin, monkeypatch):
    # Only /api/uploads/ paths are ours; a scraped article's
    # image lives on knf.vu.lt and must not be unlinked
    import app.uploads.routes as uploads_routes

    seen = []
    monkeypatch.setattr(uploads_routes, "delete_upload", lambda path: seen.append(path))
    post_id = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a",
                         image_url="https://knf.vu.lt/images/cover.jpg")

    assert client.delete(f"/api/news/{post_id}", headers=admin[1]).status_code == 200
    assert seen == []


def test_a_failing_cover_delete_still_leaves_the_post_deleted(client, db, actor, monkeypatch):
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








# ===========================================================
#  toggle_like — the flip, the gate and the healing counter
# ===========================================================

def test_liking_requires_authentication(client, db):
    post_id = _seed_post(db)

    assert client.post(f"/api/news/{post_id}/like").status_code == 401


def test_liking_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.post(f"/api/news/{uuid.uuid4()}/like", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


@pytest.mark.contract
def test_a_like_and_a_second_tap_toggle_back(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    first = client.post(f"/api/news/{post_id}/like", headers=headers)
    assert first.status_code == 200
    assert first.get_json() == {"liked": True, "likes": 1}

    second = client.post(f"/api/news/{post_id}/like", headers=headers)
    assert second.get_json() == {"liked": False, "likes": 0}


def test_a_repeated_toggle_pair_leaves_no_like_row_behind(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db)

    for _ in range(3):
        client.post(f"/api/news/{post_id}/like", headers=headers)
        client.post(f"/api/news/{post_id}/like", headers=headers)

    assert db.execute(
        "SELECT COUNT(*) AS c FROM news_likes WHERE post_id = ? AND user_id = ?", (post_id, user["id"]),
    ).fetchone()["c"] == 0
    assert db.execute("SELECT likes_count FROM news_posts WHERE id = ?", (post_id,)).fetchone()["likes_count"] == 0


def test_two_users_liking_the_same_post_both_count(client, db, actor, make_user, auth_headers):
    _, headers = actor
    post_id = _seed_post(db)
    other = make_user()

    client.post(f"/api/news/{post_id}/like", headers=headers)
    second = client.post(f"/api/news/{post_id}/like", headers=auth_headers(other))

    assert second.get_json() == {"liked": True, "likes": 2}


def test_a_drifted_like_counter_heals_on_the_next_toggle(client, db, actor):
    # likes_count is RECOMPUTED from news_likes, never nudged
    _, headers = actor
    post_id = _seed_post(db, likes_count=99)

    assert client.post(f"/api/news/{post_id}/like", headers=headers).get_json()["likes"] == 1


def test_unliking_never_drives_the_counter_negative(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, likes_count=0)
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], post_id))
    db.commit()

    response = client.post(f"/api/news/{post_id}/like", headers=headers)

    assert response.get_json() == {"liked": False, "likes": 0}


def test_a_stranger_cannot_like_a_private_post(client, db, make_user, actor):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)
    _, headers = actor

    response = client.post(f"/api/news/{post_id}/like", headers=headers)

    assert response.status_code == 404
    assert db.execute("SELECT likes_count FROM news_posts WHERE id = ?", (post_id,)).fetchone()["likes_count"] == 0


def test_the_author_can_like_their_own_private_post(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social", is_public=0)

    assert client.post(f"/api/news/{post_id}/like", headers=headers).get_json()["liked"] is True


def test_a_friend_can_like_a_private_wall_post(client, db, make_user, auth_headers):
    author = make_user()
    friend = make_user()
    _befriend(db, author["id"], friend["id"])
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.post(f"/api/news/{post_id}/like", headers=auth_headers(friend)).status_code == 200


def test_the_like_shows_up_on_the_post_itself(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    client.post(f"/api/news/{post_id}/like", headers=headers)

    body = client.get(f"/api/news/{post_id}", headers=headers).get_json()
    assert body["likes"] == 1
    assert body["liked"] is True








# ===========================================================
#  share_post — the one write with no auth at all
# ===========================================================

@pytest.mark.contract
def test_a_guest_can_record_a_share(client, db):
    post_id = _seed_post(db)

    response = client.post(f"/api/news/{post_id}/share")

    assert response.status_code == 200
    assert response.get_json() == {"shares": 1}


def test_sharing_an_unknown_post_is_a_404(client):
    response = client.post(f"/api/news/{uuid.uuid4()}/share")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_every_share_bumps_the_counter_again(client, db):
    # Deliberately NOT idempotent — nothing ever decrements it
    post_id = _seed_post(db)

    counts = [client.post(f"/api/news/{post_id}/share").get_json()["shares"] for _ in range(3)]

    assert counts == [1, 2, 3]
    assert db.execute("SELECT shares_count FROM news_posts WHERE id = ?", (post_id,)).fetchone()["shares_count"] == 3


def test_a_share_by_a_member_counts_the_same_as_a_guests(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    client.post(f"/api/news/{post_id}/share")

    assert client.post(f"/api/news/{post_id}/share", headers=headers).get_json()["shares"] == 2


def test_the_share_count_shows_up_on_the_post_itself(client, db):
    post_id = _seed_post(db)

    client.post(f"/api/news/{post_id}/share")

    assert client.get(f"/api/news/{post_id}").get_json()["shares"] == 1


def test_a_stranger_cannot_share_a_private_post(client, db, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.post(f"/api/news/{post_id}/share").status_code == 404








# ===========================================================
#  get_feed — who sees what, and the paging window
# ===========================================================

@pytest.mark.contract
def test_the_feed_page_carries_the_shape_the_app_pages_through(client, db):
    _seed_post(db)

    body = client.get("/api/news").get_json()

    assert set(body) == {"posts", "page", "perPage", "total", "hasMore"}
    assert body["page"] == 1
    assert body["perPage"] == 20
    assert set(body["posts"][0]) == POST_KEYS


def test_a_guest_sees_public_non_wall_rows_only(client, db, make_user):
    author = make_user()
    public_article = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a")
    _seed_post(db, source="user", post_type="social", author_id=author["id"])
    _seed_post(db, source="faculty", post_type="announcement", is_public=0)

    ids = [p["id"] for p in client.get("/api/news").get_json()["posts"]]

    assert ids == [public_article]


def test_a_guest_asking_for_wall_posts_gets_an_empty_page(client, db, make_user):
    author = make_user()
    _seed_post(db, source="user", post_type="social", author_id=author["id"])

    body = client.get("/api/news?source=user").get_json()

    assert body["posts"] == []
    assert body["total"] == 0


def test_an_unknown_source_filter_is_refused(client):
    response = client.get("/api/news?source=twitter")

    assert response.status_code == 400
    assert "source must be one of" in response.get_json()["error"]


def test_a_non_numeric_page_is_refused(client):
    response = client.get("/api/news?page=pirmas")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"


def test_an_over_sized_per_page_is_refused(client):
    assert client.get("/api/news?per_page=51").status_code == 400


def test_the_source_filter_narrows_the_page(client, db):
    _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a")
    vu = _seed_post(db, source="vu.lt", source_url="https://vu.lt/b")

    body = client.get("/api/news?source=vu.lt").get_json()

    assert [p["id"] for p in body["posts"]] == [vu]
    assert body["total"] == 1


def test_a_member_sees_their_own_private_post_in_the_feed(client, db, actor):
    user, headers = actor
    mine = _seed_post(db, author_id=user["id"], source="faculty", post_type="announcement", is_public=0)

    ids = [p["id"] for p in client.get("/api/news", headers=headers).get_json()["posts"]]

    assert mine in ids


def test_a_member_never_sees_a_strangers_private_faculty_draft(client, db, make_user, actor):
    teacher = make_user(role="teacher")
    _seed_post(db, author_id=teacher["id"], source="faculty", post_type="announcement", is_public=0)
    _, headers = actor

    assert client.get("/api/news", headers=headers).get_json()["posts"] == []


def test_staff_see_private_faculty_drafts_in_the_feed(client, db, make_user, auth_headers):
    author = make_user(role="teacher")
    draft = _seed_post(db, author_id=author["id"], source="faculty", post_type="announcement", is_public=0)
    curator = make_user(role="curator")

    ids = [p["id"] for p in client.get("/api/news", headers=auth_headers(curator)).get_json()["posts"]]

    assert ids == [draft]


def test_a_friends_private_wall_post_reaches_the_feed(client, db, make_user, auth_headers):
    author = make_user()
    friend = make_user()
    _befriend(db, author["id"], friend["id"])
    wall = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    ids = [p["id"] for p in client.get("/api/news", headers=auth_headers(friend)).get_json()["posts"]]

    assert ids == [wall]


def test_a_non_friends_public_wall_post_stays_out_of_the_feed(client, db, make_user, actor):
    stranger = make_user()
    _seed_post(db, author_id=stranger["id"], source="user", post_type="social")
    _, headers = actor

    assert client.get("/api/news", headers=headers).get_json()["posts"] == []


def test_a_member_filtering_on_a_faculty_source_still_sees_the_page(client, db, actor):
    _, headers = actor
    announcement = _seed_post(db, source="faculty", post_type="announcement")

    ids = [p["id"] for p in client.get("/api/news?source=faculty", headers=headers).get_json()["posts"]]

    assert ids == [announcement]


def test_before_pins_the_window_and_excludes_later_rows(client, db):
    old = _seed_post(db, published_at="2026-01-01T00:00:00+00:00")
    _seed_post(db, published_at="2027-01-01T00:00:00+00:00")

    body = client.get("/api/news", query_string={"before": "2026-06-01T00:00:00+00:00"}).get_json()

    assert [p["id"] for p in body["posts"]] == [old]
    assert body["total"] == 1


def test_a_before_whose_plus_arrived_as_a_space_is_still_understood(client, db):
    # "?before=…12:00:00+00:00" reaches request.args as
    # "…12:00:00 00:00" — _parse_iso repairs exactly that
    old = _seed_post(db, published_at="2026-01-01T00:00:00+00:00")

    body = client.get("/api/news?before=2026-06-01T00:00:00 00:00").get_json()

    assert [p["id"] for p in body["posts"]] == [old]


def test_a_legacy_space_form_before_is_still_understood(client, db):
    old = _seed_post(db, published_at="2026-01-01T00:00:00+00:00")

    body = client.get("/api/news", query_string={"before": "2026-06-01 00:00:00"}).get_json()

    assert [p["id"] for p in body["posts"]] == [old]


def test_a_garbage_before_is_refused(client):
    response = client.get("/api/news?before=vakar")

    assert response.status_code == 400
    assert response.get_json()["error"] == "before must be an ISO-8601 timestamp"


def test_a_future_stamp_does_not_pin_a_row_above_everything(client, db):
    # MAX(0, …) floors a future published_at at zero age, so
    # the recency term can no longer divide by ~0
    future = _seed_post(db, published_at="2099-01-01T00:00:00+00:00", source="app")
    faculty = _seed_post(db, published_at=datetime.now(timezone.utc).isoformat(), source="faculty")

    ids = [p["id"] for p in client.get("/api/news").get_json()["posts"]]

    assert ids == [faculty, future]


def test_an_unparseable_published_at_still_ranks_the_row(client, db):
    # COALESCE(…, 0) — a NULL score would sort LAST and bury it
    broken = _seed_post(db, published_at="nezinia")

    body = client.get("/api/news").get_json()

    assert [p["id"] for p in body["posts"]] == [broken]


def test_the_pages_walk_to_the_end(client, db):
    for index in range(3):
        _seed_post(db, published_at=f"2026-0{index + 1}-01T00:00:00+00:00")

    first = client.get("/api/news?per_page=2&page=1").get_json()
    second = client.get("/api/news?per_page=2&page=2").get_json()

    assert first["hasMore"] is True
    assert len(first["posts"]) == 2
    assert second["hasMore"] is False
    assert len(second["posts"]) == 1
    assert first["total"] == second["total"] == 3


def test_the_feed_answers_304_when_nothing_changed(client, db):
    _seed_post(db)
    first = client.get("/api/news")
    etag = first.headers["ETag"]

    second = client.get("/api/news", headers={"If-None-Match": etag})

    assert first.status_code == 200
    assert second.status_code == 304
    assert second.get_data() == b""
    assert second.headers["Cache-Control"] == "public, max-age=60"


def test_a_like_busts_the_feed_etag(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)
    etag = client.get("/api/news", headers=headers).headers["ETag"]

    client.post(f"/api/news/{post_id}/like", headers=headers)

    again = client.get("/api/news", headers={**headers, "If-None-Match": etag})
    assert again.status_code == 200


def test_a_members_feed_is_never_publicly_cacheable(client, db, actor):
    _, headers = actor
    _seed_post(db)

    response = client.get("/api/news", headers=headers)

    assert response.headers["Cache-Control"] == "private, max-age=60"


def test_the_feed_marks_the_callers_own_likes(client, db, actor, make_user, auth_headers):
    _, headers = actor
    post_id = _seed_post(db)
    client.post(f"/api/news/{post_id}/like", headers=headers)
    stranger = make_user()

    mine = client.get("/api/news", headers=headers).get_json()["posts"][0]
    theirs = client.get("/api/news", headers=auth_headers(stranger)).get_json()["posts"][0]

    assert mine["liked"] is True
    assert theirs["liked"] is False


def test_a_poll_card_ships_its_poll_inline_in_the_feed(client, db):
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id)

    post = client.get("/api/news").get_json()["posts"][0]

    assert [o["id"] for o in post["poll"]["options"]] == option_ids


def test_an_empty_feed_answers_an_empty_page(client):
    body = client.get("/api/news").get_json()

    assert body == {"posts": [], "page": 1, "perPage": 20, "total": 0, "hasMore": False}








# ===========================================================
#  comments — reading, writing and taking one back
# ===========================================================

@pytest.mark.contract
def test_a_comment_lands_and_bumps_the_counter(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db)

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "Puiku"}, headers=headers)

    assert response.status_code == 201
    body = response.get_json()
    assert set(body) == {"id", "text", "time", "userName", "userAvatar", "userId"}
    assert body["text"] == "Puiku"
    assert body["userId"] == user["id"]
    assert db.execute(
        "SELECT comments_count FROM news_posts WHERE id = ?", (post_id,)
    ).fetchone()["comments_count"] == 1


def test_a_blank_comment_is_refused(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "   "}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_a_non_string_comment_is_refused(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    assert client.post(f"/api/news/{post_id}/comments", json={"text": 5}, headers=headers).status_code == 400


def test_a_comment_of_exactly_the_cap_is_accepted(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "k" * 2000}, headers=headers)

    assert response.status_code == 201


def test_a_comment_one_character_over_the_cap_is_refused(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "k" * 2001}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment must be at most 2000 characters"


def test_commenting_needs_authentication(client, db):
    post_id = _seed_post(db)

    assert client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}).status_code == 401


def test_commenting_on_an_invisible_post_is_a_404(client, db, make_user, actor):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)
    _, headers = actor

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    assert response.status_code == 404


def test_the_comments_page_is_newest_first(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)
    for text in ("Pirmas", "Antras"):
        client.post(f"/api/news/{post_id}/comments", json={"text": text}, headers=headers)

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert [c["text"] for c in body["comments"]] == ["Antras", "Pirmas"]
    assert body["total"] == 2
    assert body["page"] == 1


def test_a_legacy_space_form_comment_stamp_goes_out_as_utc_iso(client, db, actor):
    user, _ = actor
    post_id = _seed_post(db)
    db.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), post_id, user["id"], "Senas", "2026-01-01 12:00:00"),
    )
    db.commit()

    comment = client.get(f"/api/news/{post_id}/comments").get_json()["comments"][0]

    assert comment["time"] == "2026-01-01T12:00:00+00:00"


def test_an_orphaned_comment_still_pages_as_a_deleted_user(client, db, make_user):
    # LEFT JOIN on purpose: the row used to vanish from the page
    # while still counting toward total, so paging never converged
    ghost = make_user()
    post_id = _seed_post(db)
    db.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), post_id, ghost["id"], "Naslaitis", datetime.now(timezone.utc).isoformat()),
    )
    db.execute("DELETE FROM users WHERE id = ?", (ghost["id"],))
    db.commit()

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["total"] == 1
    assert body["comments"][0]["userName"] == "Deleted user"
    assert body["comments"][0]["userAvatar"] is None


def test_reading_the_comments_of_an_unknown_post_is_a_404(client):
    assert client.get(f"/api/news/{uuid.uuid4()}/comments").status_code == 404


def test_a_bad_page_on_the_comments_route_is_refused(client, db):
    post_id = _seed_post(db)

    assert client.get(f"/api/news/{post_id}/comments?page=0").status_code == 400


def test_a_private_posts_comments_are_closed_to_a_stranger(client, db, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)

    assert client.get(f"/api/news/{post_id}/comments").status_code == 404


def test_the_comment_author_takes_their_own_comment_back(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)
    comment_id = client.post(f"/api/news/{post_id}/comments", json={"text": "Atsiprasau"},
                             headers=headers).get_json()["id"]

    response = client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted", "comments": 0}


def test_the_post_author_can_remove_someone_elses_comment(client, db, actor, make_user, auth_headers):
    author, author_headers = actor
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social")
    commenter = make_user()
    comment_id = client.post(f"/api/news/{post_id}/comments", json={"text": "Nemandagu"},
                             headers=auth_headers(commenter)).get_json()["id"]

    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=author_headers).status_code == 200


def test_an_admin_can_remove_any_comment(client, db, admin, actor):
    user, headers = actor
    post_id = _seed_post(db)
    comment_id = client.post(f"/api/news/{post_id}/comments", json={"text": "Bet kas"},
                             headers=headers).get_json()["id"]

    assert client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=admin[1]).status_code == 200


def test_a_bystander_cannot_remove_a_comment(client, db, actor, make_user, auth_headers):
    _, headers = actor
    post_id = _seed_post(db)
    comment_id = client.post(f"/api/news/{post_id}/comments", json={"text": "Mano zodziai"},
                             headers=headers).get_json()["id"]
    bystander = make_user()

    response = client.delete(f"/api/news/{post_id}/comments/{comment_id}", headers=auth_headers(bystander))

    assert response.status_code == 403
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 1


def test_a_comment_from_another_thread_cannot_be_deleted_through_this_post(client, db, actor):
    user, headers = actor
    mine = _seed_post(db, author_id=user["id"], source="user", post_type="social")
    other = _seed_post(db)
    comment_id = client.post(f"/api/news/{other}/comments", json={"text": "Kitur"},
                             headers=headers).get_json()["id"]

    response = client.delete(f"/api/news/{mine}/comments/{comment_id}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Comment not found"


def test_deleting_a_comment_on_an_invisible_post_is_a_404(client, db, make_user, actor):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social", is_public=0)
    _, headers = actor

    assert client.delete(f"/api/news/{post_id}/comments/{uuid.uuid4()}", headers=headers).status_code == 404








# ===========================================================
#  polls — attach, read, vote, detach
# ===========================================================

def test_the_poll_of_a_post_is_served_with_the_callers_vote(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, post_type="poll")
    poll_id, option_ids = _seed_poll(db, post_id)
    db.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], poll_id, option_ids[1], datetime.now(timezone.utc).isoformat()),
    )
    db.commit()

    body = client.get(f"/api/news/{post_id}/poll", headers=headers).get_json()

    assert set(body) == {"id", "postId", "title", "endDate", "totalVotes", "createdAt", "userVote", "options"}
    assert body["userVote"] == option_ids[1]
    assert body["endDate"] is None


def test_a_post_without_a_poll_answers_the_polls_own_404(client, db):
    post_id = _seed_post(db)

    response = client.get(f"/api/news/{post_id}/poll")

    assert response.status_code == 404
    assert response.get_json()["error"] == "No poll found for this post"


def test_an_invisible_post_hides_its_poll_behind_the_same_404(client, db, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="poll", is_public=0)
    _seed_poll(db, post_id)

    response = client.get(f"/api/news/{post_id}/poll")

    assert response.status_code == 404
    assert response.get_json()["error"] == "No poll found for this post"


def test_a_poll_end_date_goes_out_as_explicit_utc(client, db):
    post_id = _seed_post(db, post_type="poll")
    _seed_poll(db, post_id, end_date="2026-12-01 10:00:00")

    assert client.get(f"/api/news/{post_id}/poll").get_json()["endDate"] == "2026-12-01T10:00:00+00:00"


def test_attaching_a_poll_flips_the_post_type(client, db, actor):
    _, headers = actor
    post_id = _create(client, headers).get_json()["id"]

    response = client.post(f"/api/news/{post_id}/poll",
                           json={"title": "Ar ateisi", "options": ["Taip", "Ne"]}, headers=headers)

    assert response.status_code == 201
    assert response.get_json()["userVote"] is None
    assert [o["text"] for o in response.get_json()["options"]] == ["Taip", "Ne"]
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?", (post_id,)).fetchone()["post_type"] == "poll"


def test_an_empty_poll_body_is_refused(client, actor):
    _, headers = actor
    post_id = _create(client, headers).get_json()["id"]

    response = client.post(f"/api/news/{post_id}/poll", json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


@pytest.mark.parametrize("body, message", [
    ({"title": 5, "options": ["a", "b"]}, "title must be a string"),
    ({"title": "  ", "options": ["a", "b"]}, "Poll title required"),
    ({"title": "t" * 201, "options": ["a", "b"]}, "Poll title must be at most 200 characters"),
    ({"title": "Klausimas", "options": "Taip"}, "options must be an array of strings"),
    ({"title": "Klausimas", "options": ["Taip", 2]}, "options must be an array of strings"),
    ({"title": "Klausimas", "options": ["Taip", "   "]}, "At least 2 options required"),
    ({"title": "Klausimas", "options": [str(i) for i in range(11)]}, "Maximum 10 options allowed"),
    ({"title": "Klausimas", "options": ["o" * 101, "Ne"]}, "Each option must be at most 100 characters"),
    ({"title": "Klausimas", "options": ["Taip", "Ne"], "end_date": "rytoj"},
     "end_date must be an ISO-8601 timestamp"),
])
def test_a_malformed_poll_is_refused(client, actor, body, message):
    _, headers = actor
    post_id = _create(client, headers).get_json()["id"]

    response = client.post(f"/api/news/{post_id}/poll", json=body, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == message


def test_a_poll_end_date_is_stored_normalised_to_utc(client, actor):
    _, headers = actor
    post_id = _create(client, headers).get_json()["id"]

    body = client.post(f"/api/news/{post_id}/poll", json={
        "title": "Ar ateisi", "options": ["Taip", "Ne"], "end_date": "2026-12-01T13:00:00+03:00",
    }, headers=headers).get_json()

    assert body["endDate"] == "2026-12-01T10:00:00+00:00"


def test_attaching_a_poll_to_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.post(f"/api/news/{uuid.uuid4()}/poll",
                           json={"title": "Ar ateisi", "options": ["Taip", "Ne"]}, headers=headers)

    assert response.status_code == 404


def test_only_the_author_or_an_admin_may_attach_a_poll(client, db, actor, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social")
    _, headers = actor

    response = client.post(f"/api/news/{post_id}/poll",
                           json={"title": "Ar ateisi", "options": ["Taip", "Ne"]}, headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only the post author or admin can create a poll"


def test_an_admin_may_attach_a_poll_to_someone_elses_post(client, db, admin, make_user):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="social")

    response = client.post(f"/api/news/{post_id}/poll",
                           json={"title": "Ar ateisi", "options": ["Taip", "Ne"]}, headers=admin[1])

    assert response.status_code == 201


def test_a_scraped_article_cannot_carry_a_poll(client, db, admin):
    post_id = _seed_post(db, source="knf.vu.lt", source_url="https://knf.vu.lt/a")

    response = client.post(f"/api/news/{post_id}/poll",
                           json={"title": "Ar ateisi", "options": ["Taip", "Ne"]}, headers=admin[1])

    assert response.status_code == 400
    assert response.get_json()["error"] == "A scraped article cannot carry a poll"


def test_a_second_poll_on_one_post_is_a_409(client, actor):
    _, headers = actor
    post_id = _create(client, headers).get_json()["id"]
    payload = {"title": "Ar ateisi", "options": ["Taip", "Ne"]}
    client.post(f"/api/news/{post_id}/poll", json=payload, headers=headers)

    response = client.post(f"/api/news/{post_id}/poll", json=payload, headers=headers)

    assert response.status_code == 409
    assert response.get_json()["error"] == "Post already has a poll"


def test_detaching_a_poll_restores_a_wall_posts_type(client, db, actor):
    _, headers = actor
    post_id = _create(client, headers).get_json()["id"]
    client.post(f"/api/news/{post_id}/poll", json={"title": "Ar ateisi", "options": ["Taip", "Ne"]},
                headers=headers)

    response = client.delete(f"/api/news/{post_id}/poll", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted", "postType": "social"}
    assert db.execute("SELECT COUNT(*) AS c FROM polls WHERE post_id = ?", (post_id,)).fetchone()["c"] == 0


def test_detaching_a_faculty_polls_type_falls_back_to_announcement(client, make_user, auth_headers):
    teacher = make_user(role="teacher")
    headers = auth_headers(teacher)
    post_id = _create(client, headers).get_json()["id"]
    client.post(f"/api/news/{post_id}/poll", json={"title": "Ar ateisi", "options": ["Taip", "Ne"]},
                headers=headers)

    assert client.delete(f"/api/news/{post_id}/poll", headers=headers).get_json()["postType"] == "announcement"


def test_detaching_a_legacy_scraped_polls_type_falls_back_to_article(client, db, admin):
    # create_poll refuses scraped rows now; the restore path
    # still has to handle the ones attached before it did
    post_id = _seed_post(db, source="vu.lt", source_url="https://vu.lt/a", post_type="poll")
    _seed_poll(db, post_id)

    assert client.delete(f"/api/news/{post_id}/poll", headers=admin[1]).get_json()["postType"] == "article"


def test_detaching_a_missing_poll_is_a_404(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], source="user", post_type="social")

    response = client.delete(f"/api/news/{post_id}/poll", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "No poll found for this post"


def test_a_stranger_cannot_detach_a_poll(client, db, make_user, actor):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="poll")
    _seed_poll(db, post_id)
    _, headers = actor

    response = client.delete(f"/api/news/{post_id}/poll", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only the post author or admin can delete this poll"


def test_detaching_a_poll_from_an_invisible_post_is_a_404(client, db, make_user, actor):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="poll", is_public=0)
    _seed_poll(db, post_id)
    _, headers = actor

    assert client.delete(f"/api/news/{post_id}/poll", headers=headers).status_code == 404


def test_a_vote_lands_and_recomputes_both_tallies(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id)

    body = client.post(f"/api/news/{post_id}/poll/vote",
                       json={"option_id": option_ids[0]}, headers=headers).get_json()

    assert body["userVote"] == option_ids[0]
    assert body["totalVotes"] == 1
    assert [o["votes"] for o in body["options"]] == [1, 0]


def test_moving_a_vote_moves_the_tally_with_it(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id)
    client.post(f"/api/news/{post_id}/poll/vote", json={"option_id": option_ids[0]}, headers=headers)

    body = client.post(f"/api/news/{post_id}/poll/vote",
                       json={"option_id": option_ids[1]}, headers=headers).get_json()

    assert body["userVote"] == option_ids[1]
    assert body["totalVotes"] == 1
    assert [o["votes"] for o in body["options"]] == [0, 1]


def test_voting_the_same_option_twice_is_a_409(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id)
    client.post(f"/api/news/{post_id}/poll/vote", json={"option_id": option_ids[0]}, headers=headers)

    response = client.post(f"/api/news/{post_id}/poll/vote", json={"option_id": option_ids[0]}, headers=headers)

    assert response.status_code == 409
    assert response.get_json()["error"] == "Already voted for this option"


def test_a_foreign_option_id_is_an_invalid_option(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _seed_poll(db, post_id)

    response = client.post(f"/api/news/{post_id}/poll/vote",
                           json={"option_id": str(uuid.uuid4())}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid option"


@pytest.mark.parametrize("body, message", [
    ({}, "JSON object body required"),
    ({"option_id": ""}, "option_id required"),
    ({"option_id": {"id": 1}}, "option_id required"),
    ({"option_id": None}, "option_id required"),
])
def test_a_malformed_vote_is_refused(client, db, actor, body, message):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _seed_poll(db, post_id)

    response = client.post(f"/api/news/{post_id}/poll/vote", json=body, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == message


def test_voting_on_a_post_with_no_poll_is_a_404(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db)

    response = client.post(f"/api/news/{post_id}/poll/vote",
                           json={"option_id": str(uuid.uuid4())}, headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "No poll found for this post"


def test_voting_on_an_invisible_posts_poll_is_a_404(client, db, make_user, actor):
    author = make_user()
    post_id = _seed_post(db, author_id=author["id"], source="user", post_type="poll", is_public=0)
    _, option_ids = _seed_poll(db, post_id)
    _, headers = actor

    assert client.post(f"/api/news/{post_id}/poll/vote",
                       json={"option_id": option_ids[0]}, headers=headers).status_code == 404


def test_an_ended_poll_refuses_a_vote(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id, end_date="2020-01-01T00:00:00+00:00")

    response = client.post(f"/api/news/{post_id}/poll/vote",
                           json={"option_id": option_ids[0]}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Poll has ended"


def test_a_poll_closing_in_the_future_still_takes_votes(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id, end_date="2099-01-01T00:00:00+00:00")

    assert client.post(f"/api/news/{post_id}/poll/vote",
                       json={"option_id": option_ids[0]}, headers=headers).status_code == 200


def test_an_unparseable_end_date_leaves_the_poll_open(client, db, actor):
    _, headers = actor
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id, end_date="kada nors")

    assert client.post(f"/api/news/{post_id}/poll/vote",
                       json={"option_id": option_ids[0]}, headers=headers).status_code == 200


def test_voting_needs_authentication(client, db):
    post_id = _seed_post(db, post_type="poll")
    _, option_ids = _seed_poll(db, post_id)

    assert client.post(f"/api/news/{post_id}/poll/vote", json={"option_id": option_ids[0]}).status_code == 401








# ===========================================================
#  the last corners: the batched poll helper and the poll
#  creation race
# ===========================================================

def test_the_inline_poll_carries_the_viewers_own_vote(client, db, actor):
    # _polls_for_posts batches the votes of a whole page — the
    # feed and the single post both read the caller's own back
    user, headers = actor
    post_id = _seed_post(db, post_type="poll")
    poll_id, option_ids = _seed_poll(db, post_id)
    db.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], poll_id, option_ids[1], datetime.now(timezone.utc).isoformat()),
    )
    db.commit()

    detail = client.get(f"/api/news/{post_id}", headers=headers).get_json()
    feed_card = client.get("/api/news", headers=headers).get_json()["posts"][0]

    assert detail["poll"]["userVote"] == option_ids[1]
    assert feed_card["poll"]["userVote"] == option_ids[1]


def test_the_batched_poll_helper_answers_nothing_for_no_posts(db):
    from app.news.routes import _polls_for_posts

    assert _polls_for_posts(db, []) == {}


def test_a_lost_poll_creation_race_answers_the_same_409(client, db, actor, monkeypatch):
    # The friendly pre-check cannot see a poll another request
    # has not committed yet, so migration v26's unique index is
    # the real guard: its IntegrityError must become the same
    # 409, not a 500. Pinning the generated poll id makes the
    # second INSERT collide exactly as the lost race does.
    import app.news.routes as news_routes

    user, headers = actor
    first = _seed_post(db, author_id=user["id"], source="user", post_type="social")
    second = _seed_post(db, author_id=user["id"], source="user", post_type="social")

    real_uuid4 = uuid.uuid4
    pinned = uuid.UUID("00000000-0000-4000-8000-000000000001")
    state = {"armed": True}

    def _first_call_is_pinned():
        if state["armed"]:
            state["armed"] = False
            return pinned
        return real_uuid4()

    monkeypatch.setattr(news_routes.uuid, "uuid4", _first_call_is_pinned)

    payload = {"title": "Ar ateisi", "options": ["Taip", "Ne"]}
    assert client.post(f"/api/news/{first}/poll", json=payload, headers=headers).status_code == 201

    state["armed"] = True
    response = client.post(f"/api/news/{second}/poll", json=payload, headers=headers)

    assert response.status_code == 409
    assert response.get_json()["error"] == "Post already has a poll"
    assert db.execute("SELECT COUNT(*) AS c FROM polls").fetchone()["c"] == 1
    assert db.execute(
        "SELECT post_type FROM news_posts WHERE id = ?", (second,)
    ).fetchone()["post_type"] == "social"
