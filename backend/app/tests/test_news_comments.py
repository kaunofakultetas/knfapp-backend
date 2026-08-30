# -----------------------------------------------------------
#  [*] Tests — news comments (app/news/routes.py)
#
#  The comment half of the news blueprint, end to end:
#
#    POST   /api/news/<id>/comments         — add
#    GET    /api/news/<id>/comments         — paged list
#    DELETE /api/news/<id>/comments/<c_id>  — remove
#
#  What this module proves:
#
#    - the write guards: an object body, `text` a non-blank
#      STRING, at most MAX_COMMENT_LENGTH after stripping —
#      and that the body checks run BEFORE the post lookup
#    - the 404 for an unknown post AND the identical 404 for a
#      post the caller may not see, so existence never leaks
#      (_can_view_post through every one of its branches:
#      author, admin, staff, friend, stranger, guest)
#    - the ORDER BY tiebreaker: comments sharing a created_at
#      to the microsecond still page without a single row
#      repeating or vanishing across an OFFSET boundary
#    - comments_count accuracy — recomputed from the rows on
#      every add and every delete, so a drifted counter heals
#      instead of compounding
#    - who may delete a comment: its author, the post's author
#      or an admin, nobody else, and never through the wrong
#      post
#    - the wire shape the mobile app consumes
#      (services/api/news.ts CommentResponse /
#      CommentsListResponse), the explicit-UTC `time` a legacy
#      row is repaired to, and the HTML escaping the client
#      decodes on arrival
# -----------------------------------------------------------

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

MAX_COMMENT_LENGTH = 2000
COMMENT_BUDGET = 60




# -----------------------------------------------------------
# _make_post
# -----------------------------------------------------------
#
# One news_posts row planted straight through the `db`
# fixture, so a test can own a post of any source/visibility
# without going through create_post (which never mints a
# private faculty row for a plain student, and never a
# scraped one at all).
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
# A comment row written behind the route's back — the only
# way to test a stamp shape add_comment cannot produce
# (legacy space-form, an offset, garbage) or an ORPHAN whose
# author no longer exists: the `db` fixture connection has no
# foreign_keys pragma, so it can plant what the CASCADE would
# otherwise have swept.
#
# Seeding does NOT move comments_count, which is exactly what
# the drift tests want.
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
# _befriend
# -----------------------------------------------------------
#
# friendships is written in BOTH directions on accept, so the
# fixture writes both too — _can_view_post reads one
# direction only and a half-written pair would prove nothing.
# -----------------------------------------------------------

def _befriend(db, one_id, other_id):
    db.execute("INSERT OR IGNORE INTO friendships (user_id, friend_id) VALUES (?, ?)", (one_id, other_id))
    db.execute("INSERT OR IGNORE INTO friendships (user_id, friend_id) VALUES (?, ?)", (other_id, one_id))
    db.commit()




# -----------------------------------------------------------
# _counter
# -----------------------------------------------------------

def _counter(db, post_id):
    return db.execute(
        "SELECT comments_count FROM news_posts WHERE id = ?", (post_id,)
    ).fetchone()["comments_count"]




# -----------------------------------------------------------
# _post_json
# -----------------------------------------------------------
#
# A POST whose body the STDLIB json serialises. Flask's test
# client dumps its json= argument through app.json — which
# here IS the escaping provider — so a "<" in a payload would
# leave the harness ALREADY html-escaped and no test could
# tell escape-on-input from escape-on-output. Every test that
# cares about the exact characters on the wire goes through
# this; the rest use json= freely.
# -----------------------------------------------------------

def _post_json(client, url, payload, headers=None):
    return client.post(url, data=json.dumps(payload),
                       content_type="application/json", headers=headers or {})




# -----------------------------------------------------------
# _walk_pages
# -----------------------------------------------------------
#
# Pages a thread from page 1 until an empty page and returns
# (ids in the order the client saw them, the last total).
# Walking beats asking for one big page: an ORDER BY without
# a deterministic tiebreaker only misbehaves AT the OFFSET
# boundary, where it repeats one row and drops another.
# -----------------------------------------------------------

def _walk_pages(client, post_id, per_page, headers=None):
    seen = []
    total = 0
    page = 1

    while True:
        response = client.get(
            f"/api/news/{post_id}/comments?page={page}&per_page={per_page}",
            headers=headers or {},
        )
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        total = body["total"]

        if not body["comments"]:
            return seen, total

        seen.extend(c["id"] for c in body["comments"])
        page += 1
        assert page < 100, "paging never reached an empty page"




# -----------------------------------------------------------
# Adding a comment — the happy path and the wire shape
# -----------------------------------------------------------


def test_a_member_can_comment_on_a_public_post(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    assert response.status_code == 201
    assert response.get_json()["text"] == "Labas"


def test_a_member_can_comment_on_someone_elses_public_post(client, db, actor, make_user):
    author = make_user()
    _, headers = actor
    post_id = _make_post(db, author_id=author["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "Sveiki"}, headers=headers)

    assert response.status_code == 201


@pytest.mark.contract
def test_the_created_comment_carries_the_mobile_comment_shape(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    body = client.post(
        f"/api/news/{post_id}/comments", json={"text": "Komentaras"}, headers=headers
    ).get_json()

    # services/api/news.ts CommentResponse
    assert set(body) == {"id", "text", "time", "userName", "userAvatar", "userId"}
    assert body["userId"] == user["id"]
    assert body["userName"] == user["username"].title()
    assert body["userAvatar"] is None
    assert body["time"].endswith("+00:00")


def test_the_comment_row_holds_exactly_the_time_the_reply_reported(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    body = client.post(f"/api/news/{post_id}/comments", json={"text": "Vienas"}, headers=headers).get_json()

    row = db.execute("SELECT created_at, text, user_id, post_id FROM news_comments WHERE id = ?",
                     (body["id"],)).fetchone()
    assert row["created_at"] == body["time"]
    assert row["text"] == "Vienas"
    assert row["user_id"] == user["id"]
    assert row["post_id"] == post_id


def test_the_reported_time_is_the_one_the_list_serves_back(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    created = client.post(f"/api/news/{post_id}/comments", json={"text": "Du"}, headers=headers).get_json()
    listed = client.get(f"/api/news/{post_id}/comments", headers=headers).get_json()["comments"][0]

    assert listed["time"] == created["time"]
    assert listed["id"] == created["id"]


def test_surrounding_whitespace_is_stripped_before_the_comment_is_stored(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    body = client.post(
        f"/api/news/{post_id}/comments", json={"text": "   Labas rytas \n "}, headers=headers
    ).get_json()

    assert body["text"] == "Labas rytas"
    assert db.execute("SELECT text FROM news_comments WHERE id = ?",
                      (body["id"],)).fetchone()["text"] == "Labas rytas"


def test_lithuanian_text_survives_the_round_trip(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = _post_json(client, f"/api/news/{post_id}/comments", {"text": "Ačiū už įrašą"}, headers)

    assert response.get_json()["text"] == "Ačiū už įrašą"
    # ensure_ascii=False — the bytes carry UTF-8, not \uXXXX
    assert "Ačiū" in response.get_data(as_text=True)


def test_html_in_a_comment_ships_escaped_but_is_stored_raw(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    raw = '<script>alert("x")</script>'

    body = _post_json(client, f"/api/news/{post_id}/comments", {"text": raw}, headers).get_json()

    # Escaped ONCE on the way out — the mobile client decodes
    # exactly once, so a second pass would surface entities
    assert body["text"] == "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"
    assert db.execute("SELECT text FROM news_comments WHERE id = ?", (body["id"],)).fetchone()["text"] == raw


def test_a_listed_comment_is_escaped_the_same_way_the_created_one_is(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    _seed_comment(db, post_id, user["id"], text="a < b & c")

    listed = client.get(f"/api/news/{post_id}/comments", headers=headers).get_json()["comments"][0]

    assert listed["text"] == "a &lt; b &amp; c"


def test_null_bytes_never_reach_the_comment_column(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    body = _post_json(client, f"/api/news/{post_id}/comments", {"text": "la\x00bas"}, headers).get_json()

    assert db.execute("SELECT text FROM news_comments WHERE id = ?", (body["id"],)).fetchone()["text"] == "labas"




# -----------------------------------------------------------
# Adding a comment — every guard clause
# -----------------------------------------------------------


def test_a_guest_cannot_comment(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 0


def test_a_comment_without_any_body_is_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_a_malformed_json_body_is_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", data="{not json",
                           content_type="application/json", headers=headers)

    assert response.status_code == 400


def test_a_non_object_json_body_is_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", json=["labas"], headers=headers)

    assert response.status_code == 400


def test_a_body_without_text_is_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={"tekstas": "Labas"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


@pytest.mark.parametrize("text", [123, 12.5, None, True, {"a": 1}, ["a"]])
def test_a_text_that_is_not_a_string_is_refused(client, db, actor, text):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={"text": text}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 0


@pytest.mark.parametrize("text", ["", "   ", "\n\t  \n"])
def test_a_blank_comment_is_refused(client, db, actor, text):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments", json={"text": text}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_a_comment_exactly_at_the_length_cap_is_accepted(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments",
                           json={"text": "a" * MAX_COMMENT_LENGTH}, headers=headers)

    assert response.status_code == 201
    assert len(db.execute("SELECT text FROM news_comments WHERE id = ?",
                          (response.get_json()["id"],)).fetchone()["text"]) == MAX_COMMENT_LENGTH


def test_a_comment_one_character_over_the_cap_is_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments",
                           json={"text": "a" * (MAX_COMMENT_LENGTH + 1)}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == f"Comment must be at most {MAX_COMMENT_LENGTH} characters"
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 0


def test_whitespace_padding_does_not_count_towards_the_cap(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.post(f"/api/news/{post_id}/comments",
                           json={"text": "  " + "a" * MAX_COMMENT_LENGTH + "  "}, headers=headers)

    assert response.status_code == 201


def test_a_comment_on_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.post(f"/api/news/{uuid.uuid4()}/comments", json={"text": "Labas"}, headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_the_body_is_validated_before_the_post_is_looked_up(client, actor):
    _, headers = actor

    response = client.post(f"/api/news/{uuid.uuid4()}/comments", json={"text": "  "}, headers=headers)

    # The 400 must not depend on the post existing — the post id
    # is a stranger's guess and its existence must not leak
    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"




# -----------------------------------------------------------
# comments_count — recomputed from the rows, never nudged
# -----------------------------------------------------------


def test_the_counter_grows_with_every_comment(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    for i in range(3):
        assert client.post(f"/api/news/{post_id}/comments",
                           json={"text": f"nr {i}"}, headers=headers).status_code == 201

    assert _counter(db, post_id) == 3


def test_the_post_route_reports_the_fresh_comment_count(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    client.post(f"/api/news/{post_id}/comments", json={"text": "Vienas"}, headers=headers)
    client.post(f"/api/news/{post_id}/comments", json={"text": "Du"}, headers=headers)

    assert client.get(f"/api/news/{post_id}", headers=headers).get_json()["comments"] == 2


def test_a_drifted_counter_heals_on_the_next_comment(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    db.execute("UPDATE news_posts SET comments_count = 99 WHERE id = ?", (post_id,))
    db.commit()

    client.post(f"/api/news/{post_id}/comments", json={"text": "Vienintelis"}, headers=headers)

    # Recomputed from news_comments, not 99 + 1
    assert _counter(db, post_id) == 1


def test_a_counter_that_drifted_low_heals_too(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    _seed_comment(db, post_id, user["id"])
    _seed_comment(db, post_id, user["id"])

    client.post(f"/api/news/{post_id}/comments", json={"text": "Trečias"}, headers=headers)

    assert _counter(db, post_id) == 3


def test_one_posts_comments_never_touch_another_posts_counter(client, db, actor):
    user, headers = actor
    mine = _make_post(db, author_id=user["id"])
    other = _make_post(db, author_id=user["id"])

    client.post(f"/api/news/{mine}/comments", json={"text": "Labas"}, headers=headers)

    assert _counter(db, mine) == 1
    assert _counter(db, other) == 0


def test_deleting_a_comment_lowers_the_counter(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    first = client.post(f"/api/news/{post_id}/comments", json={"text": "Vienas"}, headers=headers).get_json()
    client.post(f"/api/news/{post_id}/comments", json={"text": "Du"}, headers=headers)

    response = client.delete(f"/api/news/{post_id}/comments/{first['id']}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted", "comments": 1}
    assert _counter(db, post_id) == 1


def test_the_counter_reaches_zero_when_the_last_comment_goes(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    only = client.post(f"/api/news/{post_id}/comments", json={"text": "Vienas"}, headers=headers).get_json()

    response = client.delete(f"/api/news/{post_id}/comments/{only['id']}", headers=headers)

    assert response.get_json()["comments"] == 0
    assert _counter(db, post_id) == 0


def test_the_feed_carries_the_fresh_comment_count(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    client.post(f"/api/news/{post_id}/comments", json={"text": "Vienas"}, headers=headers)
    client.post(f"/api/news/{post_id}/comments", json={"text": "Du"}, headers=headers)

    feed = client.get("/api/news", headers=headers).get_json()
    assert [p["comments"] for p in feed["posts"] if p["id"] == post_id] == [2]


def test_a_new_comment_invalidates_the_feeds_etag(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    tag = client.get("/api/news", headers=headers).headers["ETag"]
    # Nothing moved yet — the same page is a 304
    assert client.get("/api/news", headers={**headers, "If-None-Match": tag}).status_code == 304

    client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    # A comment moves no timestamp, only comments_count — the
    # watermark has to notice, or a relaunch keeps a stale page
    assert client.get("/api/news", headers={**headers, "If-None-Match": tag}).status_code == 200


def test_a_drifted_counter_heals_on_a_delete(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    doomed = _seed_comment(db, post_id, user["id"])
    _seed_comment(db, post_id, user["id"])
    db.execute("UPDATE news_posts SET comments_count = 42 WHERE id = ?", (post_id,))
    db.commit()

    response = client.delete(f"/api/news/{post_id}/comments/{doomed}", headers=headers)

    assert response.get_json()["comments"] == 1
    assert _counter(db, post_id) == 1




# -----------------------------------------------------------
# Listing comments — order, paging, the tiebreaker
# -----------------------------------------------------------


@pytest.mark.contract
def test_the_comments_page_carries_the_mobile_list_shape(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    _seed_comment(db, post_id, user["id"])

    body = client.get(f"/api/news/{post_id}/comments", headers=headers).get_json()

    # services/api/news.ts CommentsListResponse
    assert set(body) == {"comments", "total", "page", "perPage"}
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["perPage"] == 20
    assert set(body["comments"][0]) == {"id", "text", "time", "userName", "userAvatar", "userId"}


def test_a_guest_may_read_a_public_posts_comments(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], text="Viešas")

    response = client.get(f"/api/news/{post_id}/comments")

    assert response.status_code == 200
    assert response.get_json()["comments"][0]["text"] == "Viešas"


def test_an_empty_thread_answers_an_empty_page(client, db, make_user):
    post_id = _make_post(db, author_id=make_user()["id"])

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"] == []
    assert body["total"] == 0


def test_comments_come_back_newest_first(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    base = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
    for minute, text in enumerate(["seniausias", "vidurinis", "naujausias"]):
        _seed_comment(db, post_id, author["id"], text=text,
                      created_at=(base + timedelta(minutes=minute)).isoformat())

    texts = [c["text"] for c in client.get(f"/api/news/{post_id}/comments").get_json()["comments"]]

    assert texts == ["naujausias", "vidurinis", "seniausias"]


def test_the_default_page_holds_twenty_comments(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    for i in range(25):
        _seed_comment(db, post_id, author["id"], text=f"nr {i}")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert len(body["comments"]) == 20
    assert body["total"] == 25


def test_paging_walks_every_comment_exactly_once(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    base = datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc)
    planted = [_seed_comment(db, post_id, author["id"], text=f"nr {i}",
                             created_at=(base + timedelta(seconds=i)).isoformat())
               for i in range(7)]

    seen, total = _walk_pages(client, post_id, per_page=2)

    assert total == 7
    assert len(seen) == len(set(seen)) == 7
    assert set(seen) == set(planted)


def test_comments_sharing_a_timestamp_do_not_repeat_across_page_boundaries(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    # The exact regression the "ORDER BY … , c.id DESC" tiebreaker
    # exists for: six rows the created_at cannot tell apart
    same_instant = "2026-08-29T10:00:00+00:00"
    planted = [_seed_comment(db, post_id, author["id"], text=f"nr {i}", created_at=same_instant)
               for i in range(6)]

    seen, total = _walk_pages(client, post_id, per_page=2)

    assert total == 6
    assert len(seen) == len(set(seen)) == 6, "a page boundary repeated or dropped a comment"
    assert seen == sorted(planted, reverse=True)


def test_the_tiebreaker_holds_for_an_odd_page_size_too(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    same_instant = "2026-08-29T10:00:00+00:00"
    planted = [_seed_comment(db, post_id, author["id"], text=f"nr {i}", created_at=same_instant)
               for i in range(7)]

    seen, _ = _walk_pages(client, post_id, per_page=3)

    assert seen == sorted(planted, reverse=True)


def test_repeating_the_same_page_returns_the_same_rows(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    same_instant = "2026-08-29T10:00:00+00:00"
    for i in range(5):
        _seed_comment(db, post_id, author["id"], text=f"nr {i}", created_at=same_instant)

    first = client.get(f"/api/news/{post_id}/comments?page=2&per_page=2").get_json()["comments"]
    again = client.get(f"/api/news/{post_id}/comments?page=2&per_page=2").get_json()["comments"]

    assert [c["id"] for c in first] == [c["id"] for c in again]


def test_a_page_past_the_end_is_empty_but_still_reports_the_total(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"])

    body = client.get(f"/api/news/{post_id}/comments?page=9&per_page=20").get_json()

    assert body["comments"] == []
    assert body["total"] == 1
    assert body["page"] == 9


@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=abc", "page=1.5",
                                   "per_page=0", "per_page=-5", "per_page=abc", "per_page=51",
                                   "page=10001"])
def test_bad_pagination_parameters_are_refused(client, db, make_user, query):
    post_id = _make_post(db, author_id=make_user()["id"])

    response = client.get(f"/api/news/{post_id}/comments?{query}")

    assert response.status_code == 400
    assert "error" in response.get_json()


def test_the_largest_allowed_page_size_is_accepted(client, db, make_user):
    post_id = _make_post(db, author_id=make_user()["id"])

    body = client.get(f"/api/news/{post_id}/comments?per_page=50").get_json()

    assert body["perPage"] == 50


def test_comments_of_an_unknown_post_are_a_404(client):
    response = client.get(f"/api/news/{uuid.uuid4()}/comments")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_pagination_is_validated_before_the_post_lookup(client):
    response = client.get(f"/api/news/{uuid.uuid4()}/comments?page=0")

    assert response.status_code == 400


def test_a_thread_lists_only_its_own_comments(client, db, make_user):
    author = make_user()
    mine = _make_post(db, author_id=author["id"])
    other = _make_post(db, author_id=author["id"])
    _seed_comment(db, mine, author["id"], text="mano")
    _seed_comment(db, other, author["id"], text="svetimas")

    body = client.get(f"/api/news/{mine}/comments").get_json()

    assert body["total"] == 1
    assert [c["text"] for c in body["comments"]] == ["mano"]




# -----------------------------------------------------------
# Listing comments — the author join and the stamp repair
# -----------------------------------------------------------


def test_the_author_name_is_the_users_current_display_name(client, db, make_user):
    author = make_user(display_name="Senas Vardas")
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"])
    db.execute("UPDATE users SET display_name = 'Naujas Vardas' WHERE id = ?", (author["id"],))
    db.commit()

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["userName"] == "Naujas Vardas"


def test_the_authors_avatar_travels_with_the_comment(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"])
    db.execute("UPDATE users SET avatar_url = '/api/uploads/a.jpg' WHERE id = ?", (author["id"],))
    db.commit()

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["userAvatar"] == "/api/uploads/a.jpg"


def test_an_orphaned_comment_still_appears_and_still_counts(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    # The user row the CASCADE missed: the page and the total must
    # agree, or paging never converges
    _seed_comment(db, post_id, str(uuid.uuid4()), text="našlaitis")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["total"] == 1
    assert len(body["comments"]) == 1
    assert body["comments"][0]["userName"] == "Deleted user"
    assert body["comments"][0]["userAvatar"] is None


def test_a_legacy_space_form_stamp_goes_out_as_explicit_utc(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="2026-08-29 09:30:00")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "2026-08-29T09:30:00+00:00"


def test_a_zoneless_stamp_is_read_as_utc(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="2026-08-29T09:30:00")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    # Everything this app stores is UTC — the client used to read
    # a zoneless stamp as LOCAL time
    assert body["comments"][0]["time"] == "2026-08-29T09:30:00+00:00"


def test_a_date_only_stamp_becomes_midnight_utc(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="2026-08-29")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "2026-08-29T00:00:00+00:00"


def test_an_offset_bearing_stamp_is_converted_to_utc(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="2026-08-29T12:00:00+03:00")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "2026-08-29T09:00:00+00:00"


def test_an_unparseable_stamp_is_handed_back_untouched(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="vakar")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "vakar"


def test_a_blank_stamp_becomes_null(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    _seed_comment(db, post_id, author["id"], created_at="   ")

    body = client.get(f"/api/news/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] is None




# -----------------------------------------------------------
# The visibility gate — every _can_view_post branch, always
# a 404 and never a 403
# -----------------------------------------------------------


def test_a_guest_cannot_read_a_private_posts_comments(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"], is_public=0)
    _seed_comment(db, post_id, author["id"], text="slaptas")

    response = client.get(f"/api/news/{post_id}/comments")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_a_hidden_post_and_a_missing_post_answer_the_same_404(client, db, make_user):
    author = make_user()
    hidden = _make_post(db, author_id=author["id"], is_public=0)

    hidden_response = client.get(f"/api/news/{hidden}/comments")
    missing_response = client.get(f"/api/news/{uuid.uuid4()}/comments")

    assert hidden_response.status_code == missing_response.status_code == 404
    assert hidden_response.get_json() == missing_response.get_json()


def test_the_author_reads_their_own_private_posts_comments(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"], is_public=0)
    _seed_comment(db, post_id, user["id"], text="mano slaptas")

    response = client.get(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["comments"][0]["text"] == "mano slaptas"


def test_a_stranger_cannot_read_a_private_wall_posts_comments(client, db, actor, make_user):
    author = make_user()
    _, headers = actor
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)
    _seed_comment(db, post_id, author["id"])

    response = client.get(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 404


def test_a_friend_reads_a_private_wall_posts_comments(client, db, actor, make_user):
    author = make_user()
    user, headers = actor
    _befriend(db, user["id"], author["id"])
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)
    _seed_comment(db, post_id, author["id"], text="draugams")

    response = client.get(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["comments"][0]["text"] == "draugams"


def test_an_admin_reads_any_private_posts_comments(client, db, admin, make_user):
    author = make_user()
    _, headers = admin
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)
    _seed_comment(db, post_id, author["id"], text="moderacijai")

    response = client.get(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["comments"][0]["text"] == "moderacijai"


@pytest.mark.parametrize("role", ["teacher", "curator"])
def test_staff_read_a_private_faculty_posts_comments(client, db, make_user, auth_headers, role):
    staff = make_user(role=role)
    post_id = _make_post(db, author_id=None, source="faculty", is_public=0, post_type="announcement")
    _seed_comment(db, post_id, staff["id"], text="juodraštis")

    response = client.get(f"/api/news/{post_id}/comments", headers=auth_headers(staff))

    assert response.status_code == 200
    assert response.get_json()["comments"][0]["text"] == "juodraštis"


def test_a_student_cannot_read_a_private_faculty_posts_comments(client, db, actor):
    _, headers = actor
    post_id = _make_post(db, author_id=None, source="faculty", is_public=0, post_type="announcement")

    response = client.get(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 404


def test_a_teacher_is_no_closer_to_a_private_wall_post_than_anyone_else(client, db, make_user, auth_headers):
    author = make_user()
    teacher = make_user(role="teacher")
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)

    response = client.get(f"/api/news/{post_id}/comments", headers=auth_headers(teacher))

    # source 'user' takes the friendship path, staff role or not
    assert response.status_code == 404


def test_a_public_wall_post_is_open_to_a_stranger(client, db, actor, make_user):
    author = make_user()
    _, headers = actor
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=1)
    _seed_comment(db, post_id, author["id"], text="viešas įrašas")

    response = client.get(f"/api/news/{post_id}/comments", headers=headers)

    assert response.status_code == 200


def test_a_stranger_cannot_comment_on_a_private_post(client, db, actor, make_user):
    author = make_user()
    _, headers = actor
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "įsibroviau"}, headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 0
    assert _counter(db, post_id) == 0


def test_a_friend_may_comment_on_a_private_wall_post(client, db, actor, make_user):
    author = make_user()
    user, headers = actor
    _befriend(db, user["id"], author["id"])
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)

    response = client.post(f"/api/news/{post_id}/comments", json={"text": "sveikinu"}, headers=headers)

    assert response.status_code == 201
    assert _counter(db, post_id) == 1


def test_a_one_sided_friendship_row_is_enough(client, db, actor, make_user):
    author = make_user()
    user, headers = actor
    # Only the caller → author direction, the one the gate reads
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user["id"], author["id"]))
    db.commit()
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)

    assert client.get(f"/api/news/{post_id}/comments", headers=headers).status_code == 200


def test_the_wrong_direction_friendship_row_is_not(client, db, actor, make_user):
    author = make_user()
    user, headers = actor
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (author["id"], user["id"]))
    db.commit()
    post_id = _make_post(db, author_id=author["id"], source="user", is_public=0)

    assert client.get(f"/api/news/{post_id}/comments", headers=headers).status_code == 404


def test_a_private_scraped_article_is_open_to_staff_only(client, db, actor, make_user, auth_headers):
    _, student_headers = actor
    teacher = make_user(role="teacher")
    post_id = _make_post(db, author_id=None, source="knf.vu.lt", is_public=0, post_type="article")

    assert client.get(f"/api/news/{post_id}/comments", headers=student_headers).status_code == 404
    assert client.get(f"/api/news/{post_id}/comments", headers=auth_headers(teacher)).status_code == 200




# -----------------------------------------------------------
# Deleting a comment
# -----------------------------------------------------------


def test_the_comment_author_can_delete_their_own_comment(client, db, actor, make_user):
    post_author = make_user()
    user, headers = actor
    post_id = _make_post(db, author_id=post_author["id"])
    comment = client.post(f"/api/news/{post_id}/comments", json={"text": "Apsigalvojau"},
                          headers=headers).get_json()

    response = client.delete(f"/api/news/{post_id}/comments/{comment['id']}", headers=headers)

    assert response.status_code == 200
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments WHERE id = ?",
                      (comment["id"],)).fetchone()["c"] == 0


def test_the_post_author_can_delete_someone_elses_comment(client, db, actor, make_user, auth_headers):
    user, headers = actor
    commenter = make_user()
    post_id = _make_post(db, author_id=user["id"])
    comment = client.post(f"/api/news/{post_id}/comments", json={"text": "Nemandagu"},
                          headers=auth_headers(commenter)).get_json()

    response = client.delete(f"/api/news/{post_id}/comments/{comment['id']}", headers=headers)

    assert response.status_code == 200
    assert _counter(db, post_id) == 0


def test_an_admin_can_delete_any_comment(client, db, admin, make_user, auth_headers):
    _, admin_headers = admin
    post_author = make_user()
    commenter = make_user()
    post_id = _make_post(db, author_id=post_author["id"])
    comment = client.post(f"/api/news/{post_id}/comments", json={"text": "Šalintinas"},
                          headers=auth_headers(commenter)).get_json()

    response = client.delete(f"/api/news/{post_id}/comments/{comment['id']}", headers=admin_headers)

    assert response.status_code == 200


def test_an_unrelated_member_cannot_delete_a_comment(client, db, actor, make_user, auth_headers):
    post_author = make_user()
    commenter = make_user()
    _, stranger_headers = actor
    post_id = _make_post(db, author_id=post_author["id"])
    comment = client.post(f"/api/news/{post_id}/comments", json={"text": "Mano žodžiai"},
                          headers=auth_headers(commenter)).get_json()

    response = client.delete(f"/api/news/{post_id}/comments/{comment['id']}", headers=stranger_headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == (
        "Only the comment author, the post author or an admin can delete this comment"
    )
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments WHERE id = ?",
                      (comment["id"],)).fetchone()["c"] == 1
    assert _counter(db, post_id) == 1


def test_deleting_a_comment_requires_auth(client, db, make_user):
    author = make_user()
    post_id = _make_post(db, author_id=author["id"])
    comment_id = _seed_comment(db, post_id, author["id"])

    response = client.delete(f"/api/news/{post_id}/comments/{comment_id}")

    assert response.status_code == 401
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments").fetchone()["c"] == 1


def test_deleting_an_unknown_comment_is_a_404(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    response = client.delete(f"/api/news/{post_id}/comments/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Comment not found"


def test_a_comment_cannot_be_deleted_through_another_post(client, db, actor):
    user, headers = actor
    mine = _make_post(db, author_id=user["id"])
    other = _make_post(db, author_id=user["id"])
    comment = client.post(f"/api/news/{other}/comments", json={"text": "Kitoje gijoje"},
                          headers=headers).get_json()

    response = client.delete(f"/api/news/{mine}/comments/{comment['id']}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Comment not found"
    assert _counter(db, other) == 1


def test_deleting_a_comment_twice_is_a_404_the_second_time(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment = client.post(f"/api/news/{post_id}/comments", json={"text": "Vienas"},
                          headers=headers).get_json()

    assert client.delete(f"/api/news/{post_id}/comments/{comment['id']}", headers=headers).status_code == 200
    second = client.delete(f"/api/news/{post_id}/comments/{comment['id']}", headers=headers)

    assert second.status_code == 404
    assert _counter(db, post_id) == 0


def test_deleting_through_an_unknown_post_is_a_404(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    comment_id = _seed_comment(db, post_id, user["id"])

    response = client.delete(f"/api/news/{uuid.uuid4()}/comments/{comment_id}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_the_post_gate_runs_before_the_comment_lookup(client, db, actor, make_user):
    author = make_user()
    _, headers = actor
    hidden = _make_post(db, author_id=author["id"], source="user", is_public=0)

    response = client.delete(f"/api/news/{hidden}/comments/{uuid.uuid4()}", headers=headers)

    # "Post not found", never "Comment not found" — the hidden
    # post's thread must not be probeable
    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_a_stranger_cannot_delete_a_comment_on_a_hidden_post(client, db, actor, make_user):
    author = make_user()
    _, headers = actor
    hidden = _make_post(db, author_id=author["id"], source="user", is_public=0)
    comment_id = _seed_comment(db, hidden, author["id"])

    response = client.delete(f"/api/news/{hidden}/comments/{comment_id}", headers=headers)

    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) AS c FROM news_comments WHERE id = ?",
                      (comment_id,)).fetchone()["c"] == 1


def test_a_deleted_comment_leaves_the_thread(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    kept = client.post(f"/api/news/{post_id}/comments", json={"text": "lieka"}, headers=headers).get_json()
    doomed = client.post(f"/api/news/{post_id}/comments", json={"text": "šalinamas"},
                         headers=headers).get_json()

    client.delete(f"/api/news/{post_id}/comments/{doomed['id']}", headers=headers)
    body = client.get(f"/api/news/{post_id}/comments", headers=headers).get_json()

    assert [c["id"] for c in body["comments"]] == [kept["id"]]
    assert body["total"] == 1


def test_deleting_the_post_takes_its_comments_with_it(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])
    client.post(f"/api/news/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    assert db.execute("SELECT COUNT(*) AS c FROM news_comments WHERE post_id = ?",
                      (post_id,)).fetchone()["c"] == 0
    assert client.get(f"/api/news/{post_id}/comments", headers=headers).status_code == 404




# -----------------------------------------------------------
# The write budget
# -----------------------------------------------------------
#
# rate_limit("news_comment", max_attempts=60) keys on the
# caller's id, and its window is measured with
# time.monotonic — so the clock has to be frozen BEFORE the
# budget is spent, or the recorded stamps and the shifted
# "now" would not share a timeline.
# -----------------------------------------------------------


@pytest.mark.slow
def test_the_sixty_first_comment_in_a_window_is_rate_limited(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    with time_machine.travel(datetime.now(timezone.utc), tick=False):
        for i in range(COMMENT_BUDGET):
            assert client.post(f"/api/news/{post_id}/comments",
                               json={"text": f"nr {i}"}, headers=headers).status_code == 201

        refused = client.post(f"/api/news/{post_id}/comments", json={"text": "per daug"}, headers=headers)

    assert refused.status_code == 429
    assert refused.get_json()["code"] == "rate_limited"
    assert int(refused.headers["Retry-After"]) >= 1
    assert _counter(db, post_id) == COMMENT_BUDGET


@pytest.mark.slow
def test_the_comment_budget_frees_up_once_the_window_passes(client, db, actor):
    user, headers = actor
    post_id = _make_post(db, author_id=user["id"])

    with time_machine.travel(datetime.now(timezone.utc), tick=False) as traveller:
        for i in range(COMMENT_BUDGET):
            client.post(f"/api/news/{post_id}/comments", json={"text": f"nr {i}"}, headers=headers)
        assert client.post(f"/api/news/{post_id}/comments",
                           json={"text": "per daug"}, headers=headers).status_code == 429

        traveller.shift(timedelta(seconds=301))
        allowed = client.post(f"/api/news/{post_id}/comments", json={"text": "vėl galima"}, headers=headers)

    assert allowed.status_code == 201
    assert _counter(db, post_id) == COMMENT_BUDGET + 1


def test_one_members_spent_budget_does_not_gag_another(client, db, actor, make_user, auth_headers):
    user, headers = actor
    neighbour = make_user()
    post_id = _make_post(db, author_id=user["id"])

    for i in range(5):
        client.post(f"/api/news/{post_id}/comments", json={"text": f"nr {i}"}, headers=headers)

    assert client.post(f"/api/news/{post_id}/comments", json={"text": "ir aš"},
                       headers=auth_headers(neighbour)).status_code == 201
