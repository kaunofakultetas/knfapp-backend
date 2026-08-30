# -----------------------------------------------------------
#  [*] Tests — GET /api/news, the unified feed
#
#  What this module proves about news/routes.py get_feed and
#  the helpers it leans on (_parse_iso, _can_view_post,
#  _post_to_dict, _feed_version, _cacheable, _polls_for_posts,
#  _poll_shape):
#
#    - the app works WITHOUT login: a guest reads the feed and
#      sees exactly the public non-wall rows, never a private
#      one, never somebody's wall, and never liked=True
#    - the member view: own rows whatever their state, friends'
#      wall posts (private ones included), staff-only drafts,
#      and NOT a stranger's wall — not even for an admin
#    - the ranking formula: recency, the capped engagement
#      term and the per-source boost, plus the two regressions
#      the COALESCE and the MAX(0, …) exist for — a future
#      stamp must not pin a row to the top and an unparseable
#      one must sort last instead of burying the whole page
#    - ?source: every whitelisted value, a 400 for anything
#      else, and the empty page a logged-out "user" chip yields
#    - pagination bounds: page 0 / negative / garbage / past
#      the cap, per_page 0 and 51, the 1-and-50 boundaries,
#      and pages that neither overlap nor drop a row
#    - ?before: the paging pin excludes later rows AND moves
#      the recency reference, and the three timestamp shapes a
#      query string can mangle still parse
#    - the wire contract the mobile NewsFeedResponse/NewsPost
#      types consume, down to the key set
#    - the ETag: one per viewer, per friend set and per query,
#      moved by a like as well as by a post, and the 304 that
#      ends the request before any ranking work
#
#  Nothing here sleeps or reaches the network: timestamps are
#  computed relative to now and written straight into the row.
# -----------------------------------------------------------


import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest


FEED = "/api/news"

# The mobile NewsPost type (mobile/app/types/index.ts) plus the
# two additive fields the feed attaches per row
POST_KEYS = {
    "id", "title", "content", "summary", "imageUrl", "author", "authorId",
    "source", "sourceUrl", "postType", "likes", "comments", "shares",
    "date", "isPublic", "liked",
}

# NewsFeedResponse (mobile/app/services/api/news.ts)
ENVELOPE_KEYS = {"posts", "page", "perPage", "total", "hasMore"}




# -----------------------------------------------------------
# _iso
# -----------------------------------------------------------
#
# A stamp `days` away from now in exactly the shape the module
# writes — aware UTC, ISO T-form, microseconds dropped so the
# string comparisons behind ?before stay readable in a failure
# message. Negative days is the past, positive the future.
# -----------------------------------------------------------

def _iso(days=0.0, base=None):
    moment = (base or datetime.now(timezone.utc)) + timedelta(days=days)
    return moment.replace(microsecond=0).isoformat()




# -----------------------------------------------------------
# seed_post
# -----------------------------------------------------------
#
#   pid = seed_post(source="faculty", is_public=0, days=-1)
#
# Inserts one news_posts row directly and hands back its id.
# Direct SQL on purpose: the feed has to serve rows POST
# /api/news can never create — scraped articles (author_id
# NULL, a source_url), unpublished faculty drafts, future and
# corrupted timestamps — and a route-built fixture could not
# produce any of them.
#
# `days` is an offset from now; `published_at` overrides it
# outright for the malformed-timestamp cases.
# -----------------------------------------------------------

@pytest.fixture
def seed_post(app):

    def _seed(source="app", post_type="article", is_public=1, days=0.0, author_id=None,
              author_name=None, title=None, content="Turinys", summary=None, image_url=None,
              source_url=None, likes=0, comments=0, shares=0, published_at=None, post_id=None):
        post_id = post_id or str(uuid.uuid4())
        stamp = published_at if published_at is not None else _iso(days)

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
                 likes, comments, shares, stamp, stamp, stamp),
            )
            conn.commit()
        finally:
            conn.close()

        return post_id

    return _seed




# -----------------------------------------------------------
# befriend
# -----------------------------------------------------------
#
# Writes the friendships pair the way social/routes.py writes
# it on accept — BOTH directions, which is what makes one
# direction enough for the feed's IN-clause.
# -----------------------------------------------------------

@pytest.fixture
def befriend(app):

    def _befriend(a_id, b_id, both=True):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (a_id, b_id))
            if both:
                conn.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (b_id, a_id))
            conn.commit()
        finally:
            conn.close()

    return _befriend




# -----------------------------------------------------------
# add_like
# -----------------------------------------------------------
#
# A news_likes row plus the denormalised counter, the pair
# toggle_like keeps in step — seeded directly so a liked-flag
# test does not depend on the like route passing.
# -----------------------------------------------------------

@pytest.fixture
def add_like(app):

    def _add(post_id, user_id):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user_id, post_id))
            conn.execute(
                "UPDATE news_posts SET likes_count = (SELECT COUNT(*) FROM news_likes WHERE post_id = ?)"
                " WHERE id = ?",
                (post_id, post_id),
            )
            conn.commit()
        finally:
            conn.close()

    return _add




# -----------------------------------------------------------
# seed_poll
# -----------------------------------------------------------
#
# A poll with its options in insertion order (the order the
# feed must preserve) and, optionally, one caller's vote.
# Returns (poll_id, [option ids]).
# -----------------------------------------------------------

@pytest.fixture
def seed_poll(app):

    def _seed(post_id, options=("Taip", "Ne"), title="Klausimas", end_date=None, voter=None,
              voted_index=0):
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
            if voter:
                conn.execute(
                    "INSERT INTO poll_votes (user_id, poll_id, option_id, created_at) VALUES (?, ?, ?, ?)",
                    (voter, poll_id, option_ids[voted_index], _iso()),
                )
                conn.execute("UPDATE poll_options SET votes = 1 WHERE id = ?", (option_ids[voted_index],))
                conn.execute("UPDATE polls SET total_votes = 1 WHERE id = ?", (poll_id,))
            conn.execute("UPDATE news_posts SET post_type = 'poll' WHERE id = ?", (post_id,))
            conn.commit()
        finally:
            conn.close()

        return poll_id, option_ids

    return _seed




# -----------------------------------------------------------
# _ids
# -----------------------------------------------------------
#
# The page's post ids in the order the feed ranked them — the
# assertion subject of every ordering test below.
# -----------------------------------------------------------

def _ids(response):
    return [p["id"] for p in response.get_json()["posts"]]




# -----------------------------------------------------------
# Guest access — the app must work without login
# -----------------------------------------------------------


def test_a_guest_reads_the_feed_without_any_token(client, seed_post):
    seed_post(source="knf.vu.lt")

    response = client.get(FEED)

    assert response.status_code == 200
    assert len(response.get_json()["posts"]) == 1


def test_a_guest_sees_public_scraped_and_faculty_posts(client, seed_post):
    knf = seed_post(source="knf.vu.lt")
    vu = seed_post(source="vu.lt")
    faculty = seed_post(source="faculty")
    app_row = seed_post(source="app")

    served = set(_ids(client.get(FEED)))

    assert served == {knf, vu, faculty, app_row}


def test_a_guest_never_sees_a_wall_post(client, seed_post, make_user):
    author = make_user()
    seed_post(source="user", post_type="social", author_id=author["id"])
    public = seed_post(source="faculty")

    assert _ids(client.get(FEED)) == [public]


def test_a_guest_never_sees_a_private_post(client, seed_post):
    seed_post(source="faculty", is_public=0)
    public = seed_post(source="faculty")

    body = client.get(FEED).get_json()

    assert _ids(client.get(FEED)) == [public]
    assert body["total"] == 1


def test_a_guest_asking_for_source_user_gets_an_empty_page(client, seed_post, make_user):
    author = make_user()
    seed_post(source="user", post_type="social", author_id=author["id"])

    body = client.get(f"{FEED}?source=user").get_json()

    assert body["posts"] == []
    assert body["total"] == 0
    assert body["hasMore"] is False


def test_a_guest_card_is_never_marked_liked(client, seed_post, make_user, add_like):
    fan = make_user()
    post_id = seed_post(source="faculty")
    add_like(post_id, fan["id"])

    card = client.get(FEED).get_json()["posts"][0]

    assert card["liked"] is False
    assert card["likes"] == 1


def test_a_garbage_bearer_token_is_served_as_a_guest(client, seed_post, make_user):
    author = make_user()
    seed_post(source="user", post_type="social", author_id=author["id"])
    public = seed_post(source="faculty")

    response = client.get(FEED, headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 200
    assert _ids(response) == [public]




# -----------------------------------------------------------
# Empty state
# -----------------------------------------------------------


def test_an_empty_database_answers_an_empty_page(client):
    body = client.get(FEED).get_json()

    assert body == {"posts": [], "page": 1, "perPage": 20, "total": 0, "hasMore": False}


def test_a_page_past_the_end_is_empty_but_keeps_the_total(client, seed_post):
    for _ in range(3):
        seed_post(source="faculty")

    body = client.get(f"{FEED}?page=9&per_page=5").get_json()

    assert body["posts"] == []
    assert body["total"] == 3
    assert body["hasMore"] is False


def test_a_member_with_nothing_visible_gets_an_empty_page(client, seed_post, make_user, actor):
    stranger = make_user()
    seed_post(source="user", post_type="social", author_id=stranger["id"])
    _, headers = actor

    body = client.get(FEED, headers=headers).get_json()

    assert body["posts"] == []
    assert body["total"] == 0




# -----------------------------------------------------------
# The wire contract the mobile app consumes
# -----------------------------------------------------------


@pytest.mark.contract
def test_the_envelope_carries_exactly_the_documented_keys(client, seed_post):
    seed_post(source="faculty")

    body = client.get(FEED).get_json()

    assert set(body) == ENVELOPE_KEYS
    assert body["page"] == 1
    assert body["perPage"] == 20


@pytest.mark.contract
def test_a_card_carries_exactly_the_mobile_newspost_keys(client, seed_post, make_user):
    author = make_user(display_name="Ona Onaityte")
    seed_post(source="faculty", post_type="announcement", author_id=author["id"],
              author_name="Sena Pavarde", title="Antraste", content="Turinys",
              summary="Santrauka", image_url="/api/uploads/a.jpg",
              source_url="https://knf.vu.lt/a", likes=2, comments=3, shares=4)

    card = client.get(FEED).get_json()["posts"][0]

    assert set(card) == POST_KEYS


@pytest.mark.contract
def test_a_card_carries_the_row_verbatim(client, seed_post, make_user):
    author = make_user(display_name="Ona Onaityte")
    post_id = seed_post(source="faculty", post_type="announcement", author_id=author["id"],
                        title="Antraste", content="Turinys", summary="Santrauka",
                        image_url="/api/uploads/a.jpg", source_url="https://knf.vu.lt/a",
                        likes=2, comments=3, shares=4)

    card = client.get(FEED).get_json()["posts"][0]

    assert card["id"] == post_id
    assert card["title"] == "Antraste"
    assert card["content"] == "Turinys"
    assert card["summary"] == "Santrauka"
    assert card["imageUrl"] == "/api/uploads/a.jpg"
    assert card["author"] == "Ona Onaityte"
    assert card["authorId"] == author["id"]
    assert card["source"] == "faculty"
    assert card["sourceUrl"] == "https://knf.vu.lt/a"
    assert card["postType"] == "announcement"
    assert (card["likes"], card["comments"], card["shares"]) == (2, 3, 4)
    assert card["isPublic"] is True


def test_ispublic_is_a_real_boolean_not_the_stored_integer(client, seed_post, actor):
    user, headers = actor
    seed_post(source="user", post_type="social", author_id=user["id"], is_public=0)

    card = client.get(FEED, headers=headers).get_json()["posts"][0]

    assert card["isPublic"] is False


def test_a_cards_empty_columns_travel_as_null(client, seed_post):
    seed_post(source="knf.vu.lt")

    card = client.get(FEED).get_json()["posts"][0]

    assert card["summary"] is None
    assert card["imageUrl"] is None
    assert card["sourceUrl"] is None
    assert card["authorId"] is None
    assert card["author"] is None


def test_the_author_name_is_the_live_display_name_not_the_snapshot(client, seed_post, make_user, db):
    author = make_user(display_name="Senas Vardas")
    seed_post(source="faculty", author_id=author["id"], author_name="Senas Vardas")

    db.execute("UPDATE users SET display_name = 'Naujas Vardas' WHERE id = ?", (author["id"],))
    db.commit()

    assert client.get(FEED).get_json()["posts"][0]["author"] == "Naujas Vardas"


def test_a_scraped_row_falls_back_to_its_author_name_snapshot(client, seed_post):
    seed_post(source="knf.vu.lt", author_id=None, author_name="KNF")

    assert client.get(FEED).get_json()["posts"][0]["author"] == "KNF"




# -----------------------------------------------------------
# ?source — the mobile chips map straight onto it
# -----------------------------------------------------------


@pytest.mark.parametrize("source", ["app", "knf.vu.lt", "vu.lt", "faculty"])
def test_a_source_filter_returns_only_that_source(client, seed_post, source):
    wanted = seed_post(source=source)
    for other in ("app", "knf.vu.lt", "vu.lt", "faculty"):
        if other != source:
            seed_post(source=other)

    body = client.get(f"{FEED}?source={source}").get_json()

    assert _ids(client.get(f"{FEED}?source={source}")) == [wanted]
    assert body["total"] == 1


@pytest.mark.parametrize("bad", ["", "twitter", "USER", "knf.vu.lt ", "1"])
def test_a_source_outside_the_whitelist_is_refused(client, bad):
    response = client.get(FEED, query_string={"source": bad})

    assert response.status_code == 400
    assert "source must be one of" in response.get_json()["error"]


def test_the_source_error_lists_every_accepted_value(client):
    error = client.get(f"{FEED}?source=twitter").get_json()["error"]

    for source in ("app", "knf.vu.lt", "vu.lt", "faculty", "user"):
        assert source in error


def test_no_source_filter_mixes_every_source(client, seed_post):
    for source in ("app", "knf.vu.lt", "vu.lt", "faculty"):
        seed_post(source=source)

    assert client.get(FEED).get_json()["total"] == 4


def test_source_user_for_a_member_returns_own_and_friends_only(client, seed_post, make_user,
                                                               befriend, actor):
    user, headers = actor
    friend = make_user()
    stranger = make_user()
    befriend(user["id"], friend["id"])

    mine = seed_post(source="user", post_type="social", author_id=user["id"])
    theirs = seed_post(source="user", post_type="social", author_id=friend["id"], is_public=0)
    seed_post(source="user", post_type="social", author_id=stranger["id"])
    seed_post(source="faculty")

    assert set(_ids(client.get(f"{FEED}?source=user", headers=headers))) == {mine, theirs}


def test_a_source_filter_narrows_the_total_too(client, seed_post):
    seed_post(source="faculty")
    for _ in range(3):
        seed_post(source="vu.lt")

    body = client.get(f"{FEED}?source=vu.lt&per_page=1").get_json()

    assert body["total"] == 3
    assert body["hasMore"] is True




# -----------------------------------------------------------
# Pagination bounds
# -----------------------------------------------------------


def test_page_and_per_page_default_to_one_and_twenty(client, seed_post):
    for _ in range(21):
        seed_post(source="faculty")

    body = client.get(FEED).get_json()

    assert body["page"] == 1
    assert body["perPage"] == 20
    assert len(body["posts"]) == 20
    assert body["hasMore"] is True


@pytest.mark.parametrize("page", ["0", "-1", "abc", "3.0", "", "10001", " "])
def test_a_page_outside_the_bounds_is_refused(client, page):
    response = client.get(FEED, query_string={"page": page})

    assert response.status_code == 400
    assert "page must be" in response.get_json()["error"]


def test_the_page_cap_itself_is_accepted(client):
    response = client.get(f"{FEED}?page=10000")

    assert response.status_code == 200
    assert response.get_json()["page"] == 10000


def test_a_page_one_past_the_cap_is_refused(client):
    response = client.get(f"{FEED}?page=10001")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be at most 10000"


@pytest.mark.parametrize("per_page", ["0", "-5", "abc", "20.0", "", "51", "100"])
def test_a_per_page_outside_the_bounds_is_refused(client, per_page):
    response = client.get(FEED, query_string={"per_page": per_page})

    assert response.status_code == 400
    assert "per_page must be" in response.get_json()["error"]


@pytest.mark.parametrize("per_page", [1, 50])
def test_the_per_page_boundaries_are_accepted(client, seed_post, per_page):
    seed_post(source="faculty")

    body = client.get(f"{FEED}?per_page={per_page}").get_json()

    assert body["perPage"] == per_page


def test_a_padded_integer_page_is_accepted(client):
    response = client.get(FEED, query_string={"page": " 2 ", "per_page": "+5"})

    assert response.status_code == 200
    assert (response.get_json()["page"], response.get_json()["perPage"]) == (2, 5)


def test_paging_neither_drops_nor_duplicates_a_row(client, seed_post):
    for index in range(25):
        seed_post(source="faculty", days=-index)

    first = _ids(client.get(f"{FEED}?page=1&per_page=20"))
    second = _ids(client.get(f"{FEED}?page=2&per_page=20"))

    assert len(first) == 20
    assert len(second) == 5
    assert set(first).isdisjoint(second)
    assert len(set(first) | set(second)) == 25


def test_hasmore_flips_off_on_the_last_page(client, seed_post):
    for index in range(25):
        seed_post(source="faculty", days=-index)

    assert client.get(f"{FEED}?page=1&per_page=20").get_json()["hasMore"] is True
    assert client.get(f"{FEED}?page=2&per_page=20").get_json()["hasMore"] is False


def test_hasmore_is_false_when_the_page_exactly_empties_the_feed(client, seed_post):
    for index in range(5):
        seed_post(source="faculty", days=-index)

    body = client.get(f"{FEED}?page=1&per_page=5").get_json()

    assert len(body["posts"]) == 5
    assert body["hasMore"] is False


def test_per_page_one_walks_the_whole_feed_in_order(client, seed_post):
    for index in range(4):
        seed_post(source="faculty", days=-index)

    whole = _ids(client.get(f"{FEED}?per_page=20"))
    walked = [_ids(client.get(f"{FEED}?page={n}&per_page=1"))[0] for n in range(1, 5)]

    assert walked == whole




# -----------------------------------------------------------
# Ranking — FEED_SCORE_SQL, one term at a time
# -----------------------------------------------------------


def test_a_newer_post_outranks_an_older_one(client, seed_post):
    old = seed_post(source="app", days=-30)
    fresh = seed_post(source="app", days=-0.01)

    assert _ids(client.get(FEED)) == [fresh, old]


def test_the_source_boost_orders_equally_old_posts(client, seed_post):
    stamp = _iso(-2)
    app_row = seed_post(source="app", published_at=stamp)
    vu = seed_post(source="vu.lt", published_at=stamp)
    knf = seed_post(source="knf.vu.lt", published_at=stamp)
    faculty = seed_post(source="faculty", published_at=stamp)

    assert _ids(client.get(FEED)) == [faculty, knf, vu, app_row]


def test_engagement_lifts_an_equally_old_post(client, seed_post):
    stamp = _iso(-10)
    quiet = seed_post(source="app", published_at=stamp)
    busy = seed_post(source="app", published_at=stamp, likes=10, comments=10, shares=10)

    assert _ids(client.get(FEED)) == [busy, quiet]


def test_comments_and_shares_weigh_more_than_likes(client, seed_post):
    stamp = _iso(-10)
    liked = seed_post(source="app", published_at=stamp, likes=9)
    shared = seed_post(source="app", published_at=stamp, shares=4)

    assert _ids(client.get(FEED)) == [shared, liked]


def test_engagement_is_capped_so_a_fresh_post_still_wins(client, seed_post):
    viral = seed_post(source="app", days=-10, likes=100000, comments=100000, shares=100000)
    fresh = seed_post(source="app", days=-0.01)

    assert _ids(client.get(FEED)) == [fresh, viral]


def test_a_future_stamp_no_longer_pins_a_post_to_the_top(client, seed_post):
    # Regression: without MAX(0, …) the recency term of a row
    # half a day ahead is 1/(1-0.5) = 200, twice what any real
    # post can score, and the row sat on top of the feed
    future = seed_post(source="app", days=0.5)
    engaged = seed_post(source="app", days=-0.01, likes=500)

    assert _ids(client.get(FEED)) == [engaged, future]


def test_an_unparseable_published_at_sorts_last_but_is_still_served(client, seed_post):
    # Regression: julianday() of a corrupted stamp is NULL, and
    # without the COALESCE the whole score is NULL — which sorts
    # LAST under DESC anyway, but the row must not vanish
    broken = seed_post(source="faculty", published_at="ne data")
    normal = seed_post(source="app", days=-40)

    body = client.get(FEED).get_json()

    assert _ids(client.get(FEED)) == [normal, broken]
    assert body["total"] == 2


def test_ties_break_by_id_descending(client, seed_post):
    stamp = _iso(-3)
    low = seed_post(source="app", published_at=stamp, post_id="aaaa-1111")
    high = seed_post(source="app", published_at=stamp, post_id="zzzz-9999")

    assert _ids(client.get(FEED)) == [high, low]


def test_the_ranked_order_survives_the_id_join(client, seed_post):
    # STEP 4 ranks ids only and STEP 5 re-fetches them; the page
    # must come back in the ranked order, not the join's
    stamp = _iso(-1)
    faculty = seed_post(source="faculty", published_at=stamp, post_id="aaaa")
    app_row = seed_post(source="app", published_at=stamp, post_id="zzzz")

    assert _ids(client.get(FEED)) == [faculty, app_row]




# -----------------------------------------------------------
# ?before — the paging pin
# -----------------------------------------------------------


def test_before_excludes_rows_published_after_it(client, seed_post):
    old = seed_post(source="faculty", days=-2)
    seed_post(source="faculty", days=-0.1)
    pin = _iso(-1)

    body = client.get(FEED, query_string={"before": pin}).get_json()

    assert _ids(client.get(FEED, query_string={"before": pin})) == [old]
    assert body["total"] == 1


def test_before_includes_a_row_stamped_exactly_at_the_pin(client, seed_post):
    pin = _iso(-1)
    exact = seed_post(source="faculty", published_at=pin)

    assert _ids(client.get(FEED, query_string={"before": pin})) == [exact]


def test_before_accepts_the_legacy_space_form(client, seed_post):
    seed_post(source="faculty", days=-2)
    stamp = datetime.now(timezone.utc) - timedelta(days=1)

    response = client.get(FEED, query_string={"before": stamp.strftime("%Y-%m-%d %H:%M:%S")})

    assert response.status_code == 200
    assert len(response.get_json()["posts"]) == 1


def test_before_accepts_a_plus_that_arrived_as_a_space(client, seed_post):
    # A query string turns "+00:00" into " 00:00"; _parse_iso
    # puts the sign back
    seed_post(source="faculty", days=-2)
    stamp = _iso(-1).replace("+", " ")

    response = client.get(FEED, query_string={"before": stamp})

    assert response.status_code == 200
    assert len(response.get_json()["posts"]) == 1


def test_before_repairs_the_separator_and_the_sign_at_once(client, seed_post):
    seed_post(source="faculty", days=-2)
    stamp = _iso(-1).replace("T", " ").replace("+", " ")

    response = client.get(FEED, query_string={"before": stamp})

    assert response.status_code == 200
    assert len(response.get_json()["posts"]) == 1


def test_before_converts_a_non_utc_offset_instead_of_dropping_it(client, seed_post):
    # 03:00 in a +03:00 zone is midnight UTC: the row stamped an
    # hour later must stay out, the one an hour earlier must come
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=2)
    earlier = seed_post(source="faculty", published_at=(base - timedelta(hours=1)).isoformat())
    seed_post(source="faculty", published_at=(base + timedelta(hours=1)).isoformat())

    pinned = base.astimezone(timezone(timedelta(hours=3))).isoformat()

    assert _ids(client.get(FEED, query_string={"before": pinned})) == [earlier]


@pytest.mark.parametrize("bad", ["", "   ", "vakar", "2026-13-45T99:00:00", "1756400000"])
def test_an_unparseable_before_is_refused(client, bad):
    response = client.get(FEED, query_string={"before": bad})

    assert response.status_code == 400
    assert response.get_json()["error"] == "before must be an ISO-8601 timestamp"


def test_a_date_only_before_is_accepted(client, seed_post):
    seed_post(source="faculty", days=-400)
    seed_post(source="faculty", days=-0.1)

    response = client.get(FEED, query_string={"before": "2000-01-01"})

    assert response.status_code == 200
    assert response.get_json()["posts"] == []


def test_before_pins_the_recency_reference_not_just_the_ceiling(client, seed_post):
    # Both rows predate the pin, so the ceiling alone cannot
    # reorder them: measured from NOW they are near-equally
    # ancient and engagement decides, measured from the PIN the
    # day-old one is fresh and recency decides
    pin_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=365)
    recent = seed_post(source="app", published_at=(pin_at - timedelta(days=1)).isoformat())
    engaged = seed_post(source="app", published_at=(pin_at - timedelta(days=100)).isoformat(),
                        likes=10)

    assert _ids(client.get(FEED)) == [engaged, recent]
    assert _ids(client.get(FEED, query_string={"before": pin_at.isoformat()})) == [recent, engaged]


def test_before_and_a_source_filter_combine(client, seed_post):
    wanted = seed_post(source="vu.lt", days=-2)
    seed_post(source="vu.lt", days=-0.1)
    seed_post(source="faculty", days=-2)

    body = client.get(FEED, query_string={"before": _iso(-1), "source": "vu.lt"}).get_json()

    assert _ids(client.get(FEED, query_string={"before": _iso(-1), "source": "vu.lt"})) == [wanted]
    assert body["total"] == 1


def test_before_keeps_a_mid_paging_insert_out_of_every_page(client, seed_post):
    for index in range(4):
        seed_post(source="faculty", days=-index - 1)
    pin = _iso(-0.5)

    first = _ids(client.get(FEED, query_string={"before": pin, "page": 1, "per_page": 2}))
    latecomer = seed_post(source="faculty", days=0)
    second = _ids(client.get(FEED, query_string={"before": pin, "page": 2, "per_page": 2}))

    assert len(first) == 2
    assert len(second) == 2
    assert set(first).isdisjoint(second)
    assert latecomer not in first + second




# -----------------------------------------------------------
# The liked flag
# -----------------------------------------------------------


def test_liked_is_true_only_for_the_viewer_who_liked(client, seed_post, actor, add_like):
    user, headers = actor
    mine = seed_post(source="faculty")
    other = seed_post(source="faculty")
    add_like(mine, user["id"])

    cards = {p["id"]: p["liked"] for p in client.get(FEED, headers=headers).get_json()["posts"]}

    assert cards[mine] is True
    assert cards[other] is False


def test_another_members_like_does_not_flag_my_card(client, seed_post, actor, make_user, add_like):
    _, headers = actor
    somebody = make_user()
    post_id = seed_post(source="faculty")
    add_like(post_id, somebody["id"])

    card = client.get(FEED, headers=headers).get_json()["posts"][0]

    assert card["liked"] is False
    assert card["likes"] == 1


def test_the_like_route_flips_the_flag_on_the_next_page(client, seed_post, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")

    assert client.get(FEED, headers=headers).get_json()["posts"][0]["liked"] is False

    toggled = client.post(f"{FEED}/{post_id}/like", headers=headers)
    assert toggled.status_code == 200

    card = client.get(FEED, headers=headers).get_json()["posts"][0]
    assert card["liked"] is True
    assert card["likes"] == 1


def test_every_card_of_an_empty_like_table_is_unliked(client, seed_post, actor):
    _, headers = actor
    for _ in range(3):
        seed_post(source="faculty")

    assert all(p["liked"] is False for p in client.get(FEED, headers=headers).get_json()["posts"])




# -----------------------------------------------------------
# Member visibility — who sees which row
# -----------------------------------------------------------


def test_a_member_sees_their_own_private_wall_post(client, seed_post, actor):
    user, headers = actor
    mine = seed_post(source="user", post_type="social", author_id=user["id"], is_public=0)

    assert _ids(client.get(FEED, headers=headers)) == [mine]


def test_a_member_sees_a_friends_private_wall_post(client, seed_post, actor, make_user, befriend):
    user, headers = actor
    friend = make_user()
    befriend(user["id"], friend["id"])
    theirs = seed_post(source="user", post_type="social", author_id=friend["id"], is_public=0)

    assert _ids(client.get(FEED, headers=headers)) == [theirs]


def test_a_member_does_not_see_a_strangers_public_wall_post(client, seed_post, actor, make_user):
    _, headers = actor
    stranger = make_user()
    seed_post(source="user", post_type="social", author_id=stranger["id"])

    assert client.get(FEED, headers=headers).get_json()["total"] == 0


def test_a_student_does_not_see_a_private_faculty_draft(client, seed_post, actor, make_user):
    _, headers = actor
    teacher = make_user(role="teacher")
    seed_post(source="faculty", is_public=0, author_id=teacher["id"])
    published = seed_post(source="faculty")

    assert _ids(client.get(FEED, headers=headers)) == [published]


@pytest.mark.parametrize("role", ["admin", "curator", "teacher"])
def test_staff_see_a_private_faculty_draft(client, seed_post, make_user, auth_headers, role):
    staff = make_user(role=role)
    author = make_user(role="teacher")
    draft = seed_post(source="faculty", is_public=0, author_id=author["id"])

    assert _ids(client.get(FEED, headers=auth_headers(staff))) == [draft]


def test_a_student_sees_their_own_private_non_wall_post(client, seed_post, actor):
    user, headers = actor
    mine = seed_post(source="app", author_id=user["id"], is_public=0)

    assert _ids(client.get(FEED, headers=headers)) == [mine]


def test_even_an_admin_does_not_get_a_strangers_wall_post_in_the_feed(client, seed_post,
                                                                     make_user, admin):
    _, headers = admin
    stranger = make_user()
    seed_post(source="user", post_type="social", author_id=stranger["id"])
    faculty = seed_post(source="faculty", is_public=0)

    assert _ids(client.get(FEED, headers=headers)) == [faculty]


def test_a_friendship_written_in_both_directions_is_enough(client, seed_post, actor,
                                                           make_user, befriend):
    user, headers = actor
    friend = make_user()
    befriend(friend["id"], user["id"])
    theirs = seed_post(source="user", post_type="social", author_id=friend["id"])

    assert _ids(client.get(FEED, headers=headers)) == [theirs]


def test_the_member_total_counts_only_visible_rows(client, seed_post, actor, make_user):
    _, headers = actor
    stranger = make_user()
    seed_post(source="user", post_type="social", author_id=stranger["id"])
    seed_post(source="faculty", is_public=0)
    seed_post(source="faculty")

    assert client.get(FEED, headers=headers).get_json()["total"] == 1


def test_a_staff_source_filter_still_hides_other_peoples_walls(client, seed_post, make_user,
                                                               auth_headers):
    teacher = make_user(role="teacher")
    stranger = make_user()
    seed_post(source="user", post_type="social", author_id=stranger["id"])

    body = client.get(f"{FEED}?source=user", headers=auth_headers(teacher)).get_json()

    assert body["posts"] == []




# -----------------------------------------------------------
# Caching — the weak ETag and its 304
# -----------------------------------------------------------


def test_a_guest_feed_is_publicly_cacheable(client, seed_post):
    seed_post(source="faculty")

    response = client.get(FEED)

    assert response.headers["Cache-Control"] == "public, max-age=60"
    assert response.headers["ETag"].startswith('W/"')


def test_a_member_feed_is_privately_cacheable(client, seed_post, actor):
    _, headers = actor
    seed_post(source="faculty")

    response = client.get(FEED, headers=headers)

    assert response.headers["Cache-Control"] == "private, max-age=60"


def test_a_matching_if_none_match_answers_304_without_a_body(client, seed_post):
    seed_post(source="faculty")
    first = client.get(FEED)

    second = client.get(FEED, headers={"If-None-Match": first.headers["ETag"]})

    assert second.status_code == 304
    assert second.get_data() == b""
    assert second.headers["ETag"] == first.headers["ETag"]
    assert second.headers["Cache-Control"] == "public, max-age=60"


def test_a_stale_etag_gets_a_full_page(client, seed_post):
    seed_post(source="faculty")

    response = client.get(FEED, headers={"If-None-Match": 'W/"deadbeef"'})

    assert response.status_code == 200
    assert len(response.get_json()["posts"]) == 1


def test_a_new_post_invalidates_the_etag(client, seed_post):
    seed_post(source="faculty")
    tag = client.get(FEED).headers["ETag"]

    seed_post(source="faculty")

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


def test_a_like_invalidates_the_etag_even_though_no_stamp_moved(client, seed_post, actor, add_like):
    user, _ = actor
    post_id = seed_post(source="faculty")
    tag = client.get(FEED).headers["ETag"]

    add_like(post_id, user["id"])

    assert client.get(FEED, headers={"If-None-Match": tag}).status_code == 200


def test_the_etag_is_per_viewer(client, seed_post, actor):
    _, headers = actor
    seed_post(source="faculty")

    assert client.get(FEED).headers["ETag"] != client.get(FEED, headers=headers).headers["ETag"]


def test_the_etag_follows_the_friend_set(client, seed_post, actor, make_user, befriend):
    user, headers = actor
    friend = make_user()
    seed_post(source="faculty")
    tag = client.get(FEED, headers=headers).headers["ETag"]

    befriend(user["id"], friend["id"])

    assert client.get(FEED, headers=headers).headers["ETag"] != tag


@pytest.mark.parametrize("query", ["page=2", "per_page=5", "source=faculty", "before=2030-01-01"])
def test_the_etag_is_per_query(client, seed_post, query):
    seed_post(source="faculty")
    plain = client.get(FEED).headers["ETag"]

    assert client.get(f"{FEED}?{query}").headers["ETag"] != plain


def test_a_members_304_is_still_private(client, seed_post, actor):
    _, headers = actor
    seed_post(source="faculty")
    first = client.get(FEED, headers=headers)

    second = client.get(FEED, headers={**headers, "If-None-Match": first.headers["ETag"]})

    assert second.status_code == 304
    assert second.headers["Cache-Control"] == "private, max-age=60"


def test_an_empty_feed_still_carries_an_etag(client):
    response = client.get(FEED)

    assert response.status_code == 200
    assert response.headers["ETag"]
    assert client.get(FEED, headers={"If-None-Match": response.headers["ETag"]}).status_code == 304




# -----------------------------------------------------------
# Poll cards travel with the page
# -----------------------------------------------------------


@pytest.mark.contract
def test_a_poll_card_ships_its_poll_inline(client, seed_post, seed_poll, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])
    poll_id, option_ids = seed_poll(post_id, options=("Taip", "Ne", "Nezinau"), voter=user["id"],
                                    voted_index=1)

    card = client.get(FEED, headers=headers).get_json()["posts"][0]

    assert card["postType"] == "poll"
    assert set(card["poll"]) == {"id", "postId", "title", "endDate", "totalVotes",
                                 "createdAt", "userVote", "options"}
    assert card["poll"]["id"] == poll_id
    assert card["poll"]["postId"] == post_id
    assert card["poll"]["userVote"] == option_ids[1]
    assert card["poll"]["totalVotes"] == 1
    assert [o["text"] for o in card["poll"]["options"]] == ["Taip", "Ne", "Nezinau"]
    assert [o["id"] for o in card["poll"]["options"]] == option_ids
    assert [o["votes"] for o in card["poll"]["options"]] == [0, 1, 0]


def test_a_guest_sees_the_poll_without_a_vote_of_their_own(client, seed_post, seed_poll,
                                                           make_user, add_like):
    voter = make_user()
    post_id = seed_post(source="faculty")
    seed_poll(post_id, voter=voter["id"])

    card = client.get(FEED).get_json()["posts"][0]

    assert card["poll"]["userVote"] is None
    assert card["poll"]["totalVotes"] == 1


def test_a_non_voter_sees_the_tally_but_no_own_vote(client, seed_post, seed_poll, actor, make_user):
    _, headers = actor
    voter = make_user()
    post_id = seed_post(source="faculty")
    seed_poll(post_id, voter=voter["id"])

    assert client.get(FEED, headers=headers).get_json()["posts"][0]["poll"]["userVote"] is None


def test_a_polls_end_date_goes_out_as_explicit_utc(client, seed_post, seed_poll):
    post_id = seed_post(source="faculty")
    seed_poll(post_id, end_date="2030-01-01 12:00:00")

    assert client.get(FEED).get_json()["posts"][0]["poll"]["endDate"] == "2030-01-01T12:00:00+00:00"


def test_a_poll_post_with_no_poll_row_carries_no_poll_key(client, seed_post):
    seed_post(source="faculty", post_type="poll")

    assert "poll" not in client.get(FEED).get_json()["posts"][0]


def test_an_ordinary_card_carries_no_poll_key(client, seed_post):
    seed_post(source="faculty")

    assert "poll" not in client.get(FEED).get_json()["posts"][0]


def test_two_poll_cards_get_their_own_polls(client, seed_post, seed_poll):
    first = seed_post(source="faculty", days=-1)
    second = seed_post(source="faculty", days=-2)
    seed_poll(first, options=("A", "B"), title="Pirmas")
    seed_poll(second, options=("C", "D"), title="Antras")

    cards = {p["id"]: p["poll"] for p in client.get(FEED).get_json()["posts"]}

    assert cards[first]["title"] == "Pirmas"
    assert cards[second]["title"] == "Antras"
    assert [o["text"] for o in cards[second]["options"]] == ["C", "D"]




# -----------------------------------------------------------
# The rest of the blueprint
# -----------------------------------------------------------
#
# The feed is the focus above; everything below drives the
# same module's remaining routes so the visibility predicate,
# the write guards and the poll paths are exercised too. They
# share the feed's fixtures — no new database shapes.
# -----------------------------------------------------------




# -----------------------------------------------------------
# POST /api/news — create_post
# -----------------------------------------------------------


def test_creating_a_post_without_a_token_is_refused(client):
    response = client.post(FEED, json={"content": "Labas"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


# A non-OBJECT JSON body never reaches the handler: the shared
# validate_json_input hook in app/__init__.py answers first.
# The route's own guard is what a body-LESS or malformed
# request trips, and both tests below pin one of the two.
@pytest.mark.parametrize("body", [[1, 2], "tekstas", 7])
def test_a_non_object_body_is_refused_before_the_handler(client, actor, body):
    _, headers = actor

    response = client.post(FEED, json=body, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("kwargs", [{}, {"json": None}])
def test_a_post_with_no_usable_body_is_refused(client, actor, kwargs):
    _, headers = actor

    response = client.post(FEED, headers=headers, **kwargs)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_malformed_json_post_body_is_refused(client, actor):
    _, headers = actor

    response = client.post(FEED, data="{netikras", content_type="application/json",
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


@pytest.mark.parametrize("content", [None, "", "   "])
def test_a_post_without_content_is_refused(client, actor, content):
    _, headers = actor

    response = client.post(FEED, json={"content": content}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Content required"


def test_a_non_string_content_is_refused(client, actor):
    _, headers = actor

    response = client.post(FEED, json={"content": 42}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "content must be a string"


def test_a_non_string_title_is_refused(client, actor):
    _, headers = actor

    response = client.post(FEED, json={"content": "Labas", "title": ["a"]}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "title must be a string"


def test_an_overlong_title_is_refused(client, actor):
    _, headers = actor

    response = client.post(FEED, json={"content": "Labas", "title": "x" * 201}, headers=headers)

    assert response.status_code == 400
    assert "at most 200" in response.get_json()["error"]


def test_a_title_at_the_cap_is_accepted(client, actor):
    _, headers = actor

    response = client.post(FEED, json={"content": "Labas", "title": "x" * 200}, headers=headers)

    assert response.status_code == 201


def test_an_overlong_content_is_refused(client, actor):
    _, headers = actor

    response = client.post(FEED, json={"content": "x" * 10001}, headers=headers)

    assert response.status_code == 400
    assert "at most 10000" in response.get_json()["error"]


@pytest.mark.parametrize("post_type", ["poll", "tweet", "ARTICLE"])
def test_a_post_type_outside_the_whitelist_is_refused(client, actor, post_type):
    _, headers = actor

    response = client.post(FEED, json={"content": "Labas", "post_type": post_type}, headers=headers)

    assert response.status_code == 400
    assert "post_type must be one of" in response.get_json()["error"]


@pytest.mark.parametrize("is_public", ["false", 0, 1, None])
def test_a_non_boolean_is_public_is_refused(client, actor, is_public):
    _, headers = actor

    response = client.post(FEED, json={"content": "Labas", "is_public": is_public}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "is_public must be a boolean"


@pytest.mark.parametrize("image_url", ["https://evil.example/pixel.png", "/static/a.png", 5])
def test_a_foreign_image_url_is_refused(client, actor, image_url):
    _, headers = actor

    response = client.post(FEED, json={"content": "Labas", "image_url": image_url}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "image_url must be a relative /api/uploads/ path"


@pytest.mark.contract
def test_a_student_post_lands_as_a_wall_post(client, actor, db):
    user, headers = actor

    response = client.post(FEED, json={"content": "Labas visiems"}, headers=headers)

    body = response.get_json()
    assert response.status_code == 201
    assert set(body) == POST_KEYS
    assert body["source"] == "user"
    assert body["postType"] == "social"
    assert body["title"] == "Labas visiems"
    assert body["summary"] == "Labas visiems"
    assert body["sourceUrl"] is None
    assert body["authorId"] == user["id"]
    assert body["liked"] is False
    assert body["isPublic"] is True
    assert (body["likes"], body["comments"], body["shares"]) == (0, 0, 0)

    row = db.execute("SELECT source, post_type, is_public FROM news_posts WHERE id = ?",
                     (body["id"],)).fetchone()
    assert (row["source"], row["post_type"], row["is_public"]) == ("user", "social", 1)


def test_a_staff_post_lands_as_a_faculty_announcement(client, make_user, auth_headers):
    teacher = make_user(role="teacher")

    body = client.post(FEED, json={"content": "Paskaita nukeliama"},
                       headers=auth_headers(teacher)).get_json()

    assert body["source"] == "faculty"
    assert body["postType"] == "announcement"


def test_a_long_content_is_trimmed_into_the_title_and_summary(client, actor):
    _, headers = actor
    content = "a" * 300

    body = client.post(FEED, json={"content": content}, headers=headers).get_json()

    assert body["title"] == "a" * 80
    assert body["summary"] == "a" * 200
    assert body["content"] == content


def test_a_blank_post_type_still_takes_the_default(client, actor):
    _, headers = actor

    body = client.post(FEED, json={"content": "Labas", "post_type": ""}, headers=headers).get_json()

    assert body["postType"] == "social"


def test_an_explicit_post_type_is_kept(client, actor):
    _, headers = actor

    body = client.post(FEED, json={"content": "Labas", "post_type": "link"},
                       headers=headers).get_json()

    assert body["postType"] == "link"


def test_an_own_upload_is_accepted_as_the_cover(client, actor):
    _, headers = actor

    body = client.post(FEED, json={"content": "Labas", "image_url": "/api/uploads/a.jpg"},
                       headers=headers).get_json()

    assert body["imageUrl"] == "/api/uploads/a.jpg"


def test_a_private_post_is_stored_private(client, actor, db):
    _, headers = actor

    body = client.post(FEED, json={"content": "Labas", "is_public": False},
                       headers=headers).get_json()

    assert body["isPublic"] is False
    assert db.execute("SELECT is_public FROM news_posts WHERE id = ?",
                      (body["id"],)).fetchone()["is_public"] == 0


def test_a_fresh_post_shows_up_in_its_authors_own_feed(client, actor):
    user, headers = actor
    created = client.post(FEED, json={"content": "Labas"}, headers=headers).get_json()

    assert _ids(client.get(FEED, headers=headers)) == [created["id"]]


def test_a_failing_push_never_fails_the_201(client, make_user, auth_headers, monkeypatch):
    # A public faculty post rings the 'news' channel after the
    # commit; the post is already written, so a push blowing up
    # must not turn the 201 into a 500
    teacher = make_user(role="teacher")

    def _boom(*args, **kwargs):
        raise RuntimeError("Expo unreachable")

    monkeypatch.setattr("app.notifications.push.notify_channel", _boom)

    response = client.post(FEED, json={"content": "Paskaita nukeliama"},
                           headers=auth_headers(teacher))

    assert response.status_code == 201
    assert response.get_json()["source"] == "faculty"


def test_a_private_faculty_post_rings_no_channel(client, make_user, auth_headers, monkeypatch):
    teacher = make_user(role="teacher")
    calls = []
    monkeypatch.setattr("app.notifications.push.notify_channel",
                        lambda *a, **kw: calls.append(a))

    response = client.post(FEED, json={"content": "Juodrastis", "is_public": False},
                           headers=auth_headers(teacher))

    assert response.status_code == 201
    assert calls == []


def test_the_create_route_runs_out_of_budget_at_twenty_one(client, actor):
    _, headers = actor
    for _ in range(20):
        assert client.post(FEED, json={"content": "Labas"}, headers=headers).status_code == 201

    response = client.post(FEED, json={"content": "Labas"}, headers=headers)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1




# -----------------------------------------------------------
# GET /api/news/<id> — get_post and the visibility predicate
# -----------------------------------------------------------


def test_a_guest_reads_one_public_post(client, seed_post):
    post_id = seed_post(source="knf.vu.lt", author_name="KNF")

    response = client.get(f"{FEED}/{post_id}")

    assert response.status_code == 200
    assert response.get_json()["id"] == post_id
    assert response.get_json()["liked"] is False


@pytest.mark.contract
def test_one_post_carries_the_same_keys_as_a_feed_card(client, seed_post):
    post_id = seed_post(source="faculty")

    assert set(client.get(f"{FEED}/{post_id}").get_json()) == POST_KEYS


def test_an_unknown_post_id_is_a_404(client):
    response = client.get(f"{FEED}/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_a_guest_gets_the_same_404_for_a_private_post(client, seed_post):
    post_id = seed_post(source="faculty", is_public=0)

    response = client.get(f"{FEED}/{post_id}")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_the_author_reads_their_own_private_post(client, seed_post, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"], is_public=0)

    assert client.get(f"{FEED}/{post_id}", headers=headers).status_code == 200


def test_an_admin_reads_any_private_post(client, seed_post, make_user, admin):
    _, headers = admin
    stranger = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=stranger["id"], is_public=0)

    assert client.get(f"{FEED}/{post_id}", headers=headers).status_code == 200


def test_staff_read_a_private_non_wall_post(client, seed_post, make_user, auth_headers):
    curator = make_user(role="curator")
    post_id = seed_post(source="faculty", is_public=0)

    assert client.get(f"{FEED}/{post_id}", headers=auth_headers(curator)).status_code == 200


def test_a_student_cannot_read_a_private_faculty_draft(client, seed_post, actor):
    _, headers = actor
    post_id = seed_post(source="faculty", is_public=0)

    assert client.get(f"{FEED}/{post_id}", headers=headers).status_code == 404


def test_a_friend_reads_a_private_wall_post(client, seed_post, actor, make_user, befriend):
    user, headers = actor
    friend = make_user()
    befriend(user["id"], friend["id"])
    post_id = seed_post(source="user", post_type="social", author_id=friend["id"], is_public=0)

    assert client.get(f"{FEED}/{post_id}", headers=headers).status_code == 200


def test_a_stranger_cannot_read_a_private_wall_post(client, seed_post, actor, make_user):
    _, headers = actor
    stranger = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=stranger["id"], is_public=0)

    assert client.get(f"{FEED}/{post_id}", headers=headers).status_code == 404


def test_one_post_carries_the_viewers_like(client, seed_post, actor, add_like):
    user, headers = actor
    post_id = seed_post(source="faculty")
    add_like(post_id, user["id"])

    assert client.get(f"{FEED}/{post_id}", headers=headers).get_json()["liked"] is True


def test_one_post_carries_its_poll(client, seed_post, seed_poll, actor):
    user, headers = actor
    post_id = seed_post(source="faculty")
    _, option_ids = seed_poll(post_id, voter=user["id"])

    body = client.get(f"{FEED}/{post_id}", headers=headers).get_json()

    assert body["poll"]["userVote"] == option_ids[0]


def test_a_poll_post_without_a_poll_row_is_served_without_one(client, seed_post):
    post_id = seed_post(source="faculty", post_type="poll")

    body = client.get(f"{FEED}/{post_id}").get_json()

    assert "poll" not in body




# -----------------------------------------------------------
# DELETE /api/news/<id> — delete_post
# -----------------------------------------------------------


def test_deleting_a_post_without_a_token_is_refused(client, seed_post):
    post_id = seed_post(source="faculty")

    assert client.delete(f"{FEED}/{post_id}").status_code == 401


def test_deleting_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    assert client.delete(f"{FEED}/{uuid.uuid4()}", headers=headers).status_code == 404


def test_a_stranger_cannot_delete_a_post(client, seed_post, actor, make_user):
    _, headers = actor
    author = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=author["id"])

    response = client.delete(f"{FEED}/{post_id}", headers=headers)

    assert response.status_code == 403
    assert "author or an admin" in response.get_json()["error"]


def test_the_author_deletes_their_post_with_its_children(client, seed_post, seed_poll, actor,
                                                         add_like, db):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])
    add_like(post_id, user["id"])
    db.execute("INSERT INTO news_comments (id, post_id, user_id, text, created_at)"
               " VALUES ('c-1', ?, ?, 'Labas', ?)", (post_id, user["id"], _iso()))
    db.commit()
    seed_poll(post_id)

    response = client.delete(f"{FEED}/{post_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted"}
    assert db.execute("SELECT COUNT(*) c FROM news_posts WHERE id = ?", (post_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM news_likes WHERE post_id = ?", (post_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM news_comments WHERE post_id = ?", (post_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM polls WHERE post_id = ?", (post_id,)).fetchone()["c"] == 0


def test_an_admin_deleting_a_scraped_article_tombstones_its_url(client, seed_post, admin, db):
    _, headers = admin
    post_id = seed_post(source="knf.vu.lt", source_url="https://knf.vu.lt/naujiena")

    assert client.delete(f"{FEED}/{post_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) c FROM deleted_source_urls WHERE source_url = ?",
                      ("https://knf.vu.lt/naujiena",)).fetchone()["c"] == 1


def test_deleting_a_post_takes_its_cover_upload_with_it(client, seed_post, actor, app, monkeypatch):
    import os

    # uploads/routes.py resolves UPLOAD_DIR once per PROCESS and
    # caches it in a module global; every test gets a new tmp dir,
    # so the cache has to be dropped or the unlink would go to the
    # previous test's directory
    monkeypatch.setattr("app.uploads.routes._upload_dir", None)

    user, headers = actor
    cover = os.path.join(app.config["UPLOAD_DIR"], "0123456789abcdef0123456789abcdef.jpg")
    with open(cover, "wb") as handle:
        handle.write(b"jpg")
    post_id = seed_post(source="user", post_type="social", author_id=user["id"],
                        image_url="/api/uploads/0123456789abcdef0123456789abcdef.jpg")

    assert client.delete(f"{FEED}/{post_id}", headers=headers).status_code == 200
    assert not os.path.exists(cover)


def test_a_failing_upload_delete_never_fails_the_post_delete(client, seed_post, actor,
                                                             monkeypatch, db):
    # The unlink happens AFTER the commit on purpose: the row is
    # already gone, so a broken filesystem may not resurrect it
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"],
                        image_url="/api/uploads/0123456789abcdef0123456789abcdef.jpg")

    def _boom(path):
        raise OSError("disk on fire")

    monkeypatch.setattr("app.uploads.routes.delete_upload", _boom)

    assert client.delete(f"{FEED}/{post_id}", headers=headers).status_code == 200
    assert db.execute("SELECT COUNT(*) c FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["c"] == 0




# -----------------------------------------------------------
# POST /api/news/<id>/like — toggle_like
# -----------------------------------------------------------


def test_liking_without_a_token_is_refused(client, seed_post):
    post_id = seed_post(source="faculty")

    assert client.post(f"{FEED}/{post_id}/like").status_code == 401


def test_liking_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    assert client.post(f"{FEED}/{uuid.uuid4()}/like", headers=headers).status_code == 404


def test_a_stranger_cannot_like_a_private_post(client, seed_post, actor, make_user):
    _, headers = actor
    stranger = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=stranger["id"], is_public=0)

    response = client.post(f"{FEED}/{post_id}/like", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_a_second_tap_takes_the_like_back(client, seed_post, actor, db):
    _, headers = actor
    post_id = seed_post(source="faculty")

    first = client.post(f"{FEED}/{post_id}/like", headers=headers).get_json()
    second = client.post(f"{FEED}/{post_id}/like", headers=headers).get_json()

    assert first == {"liked": True, "likes": 1}
    assert second == {"liked": False, "likes": 0}
    assert db.execute("SELECT likes_count FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["likes_count"] == 0


def test_the_like_counter_heals_from_a_drifted_value(client, seed_post, actor, db):
    _, headers = actor
    post_id = seed_post(source="faculty", likes=99)

    body = client.post(f"{FEED}/{post_id}/like", headers=headers).get_json()

    assert body == {"liked": True, "likes": 1}




# -----------------------------------------------------------
# POST /api/news/<id>/share — share_post
# -----------------------------------------------------------


def test_a_guest_can_count_a_share(client, seed_post):
    post_id = seed_post(source="faculty")

    assert client.post(f"{FEED}/{post_id}/share").get_json() == {"shares": 1}
    assert client.post(f"{FEED}/{post_id}/share").get_json() == {"shares": 2}


def test_sharing_an_unknown_post_is_a_404(client):
    response = client.post(f"{FEED}/{uuid.uuid4()}/share")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"




# -----------------------------------------------------------
# GET /api/news/<id>/comments — get_comments
# -----------------------------------------------------------


@pytest.fixture
def seed_comment(app):

    def _seed(post_id, user_id, text="Labas", created_at=None, comment_id=None):
        comment_id = comment_id or str(uuid.uuid4())
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO news_comments (id, post_id, user_id, text, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (comment_id, post_id, user_id, text, created_at or _iso()),
            )
            conn.execute(
                "UPDATE news_posts SET comments_count ="
                " (SELECT COUNT(*) FROM news_comments WHERE post_id = ?) WHERE id = ?",
                (post_id, post_id),
            )
            conn.commit()
        finally:
            conn.close()
        return comment_id

    return _seed


def test_a_guest_reads_the_comments_of_a_public_post(client, seed_post, seed_comment, make_user):
    author = make_user(display_name="Ona")
    post_id = seed_post(source="faculty")
    seed_comment(post_id, author["id"], text="Puiku")

    body = client.get(f"{FEED}/{post_id}/comments").get_json()

    assert set(body) == {"comments", "total", "page", "perPage"}
    assert body["total"] == 1
    assert body["comments"][0]["text"] == "Puiku"
    assert body["comments"][0]["userName"] == "Ona"
    assert body["comments"][0]["userId"] == author["id"]


def test_the_comments_of_an_unknown_post_are_a_404(client):
    response = client.get(f"{FEED}/{uuid.uuid4()}/comments")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_a_private_posts_comments_need_the_same_rights_as_the_post(client, seed_post, actor,
                                                                   make_user):
    _, headers = actor
    stranger = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=stranger["id"], is_public=0)

    assert client.get(f"{FEED}/{post_id}/comments").status_code == 404
    assert client.get(f"{FEED}/{post_id}/comments", headers=headers).status_code == 404


def test_a_post_without_comments_answers_an_empty_page(client, seed_post):
    post_id = seed_post(source="faculty")

    body = client.get(f"{FEED}/{post_id}/comments").get_json()

    assert body == {"comments": [], "total": 0, "page": 1, "perPage": 20}


def test_comments_come_newest_first_and_page(client, seed_post, seed_comment, actor):
    user, _ = actor
    post_id = seed_post(source="faculty")
    for index in range(5):
        seed_comment(post_id, user["id"], text=f"c{index}", created_at=_iso(-index))

    first = client.get(f"{FEED}/{post_id}/comments?per_page=2").get_json()
    second = client.get(f"{FEED}/{post_id}/comments?per_page=2&page=2").get_json()

    assert [c["text"] for c in first["comments"]] == ["c0", "c1"]
    assert [c["text"] for c in second["comments"]] == ["c2", "c3"]
    assert first["total"] == 5


def test_comments_in_the_same_second_break_the_tie_by_id(client, seed_post, seed_comment, actor):
    user, _ = actor
    post_id = seed_post(source="faculty")
    stamp = _iso(-1)
    seed_comment(post_id, user["id"], text="pirmas", created_at=stamp, comment_id="aaa")
    seed_comment(post_id, user["id"], text="antras", created_at=stamp, comment_id="zzz")

    body = client.get(f"{FEED}/{post_id}/comments").get_json()

    assert [c["text"] for c in body["comments"]] == ["antras", "pirmas"]


def test_an_orphaned_comment_still_pages_and_counts(client, seed_post, seed_comment):
    post_id = seed_post(source="faculty")
    seed_comment(post_id, "vaiduoklis", text="Naslaitis")

    body = client.get(f"{FEED}/{post_id}/comments").get_json()

    assert body["total"] == 1
    assert len(body["comments"]) == 1
    assert body["comments"][0]["userName"] == "Deleted user"
    assert body["comments"][0]["userAvatar"] is None


def test_a_legacy_comment_stamp_goes_out_as_explicit_utc(client, seed_post, seed_comment, actor):
    user, _ = actor
    post_id = seed_post(source="faculty")
    seed_comment(post_id, user["id"], created_at="2026-01-02 03:04:05")

    body = client.get(f"{FEED}/{post_id}/comments").get_json()

    assert body["comments"][0]["time"] == "2026-01-02T03:04:05+00:00"


def test_an_unparseable_comment_stamp_travels_untouched(client, seed_post, seed_comment, actor):
    user, _ = actor
    post_id = seed_post(source="faculty")
    seed_comment(post_id, user["id"], created_at="nezinia")

    assert client.get(f"{FEED}/{post_id}/comments").get_json()["comments"][0]["time"] == "nezinia"


def test_the_comments_page_validates_its_pagination(client, seed_post):
    post_id = seed_post(source="faculty")

    assert client.get(f"{FEED}/{post_id}/comments?page=0").status_code == 400
    assert client.get(f"{FEED}/{post_id}/comments?per_page=51").status_code == 400




# -----------------------------------------------------------
# POST /api/news/<id>/comments — add_comment
# -----------------------------------------------------------


def test_commenting_without_a_token_is_refused(client, seed_post):
    post_id = seed_post(source="faculty")

    assert client.post(f"{FEED}/{post_id}/comments", json={"text": "Labas"}).status_code == 401


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": None}])
def test_a_comment_without_usable_text_is_refused(client, seed_post, actor, body):
    _, headers = actor
    post_id = seed_post(source="faculty")

    response = client.post(f"{FEED}/{post_id}/comments", json=body, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_a_comment_with_no_body_at_all_is_refused(client, seed_post, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")

    response = client.post(f"{FEED}/{post_id}/comments", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Comment text required"


def test_an_overlong_comment_is_refused(client, seed_post, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")

    response = client.post(f"{FEED}/{post_id}/comments", json={"text": "x" * 2001}, headers=headers)

    assert response.status_code == 400
    assert "at most 2000" in response.get_json()["error"]


def test_commenting_on_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.post(f"{FEED}/{uuid.uuid4()}/comments", json={"text": "Labas"},
                           headers=headers)

    assert response.status_code == 404


def test_a_stranger_cannot_comment_on_a_private_post(client, seed_post, actor, make_user):
    _, headers = actor
    stranger = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=stranger["id"], is_public=0)

    response = client.post(f"{FEED}/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    assert response.status_code == 404


@pytest.mark.contract
def test_a_comment_is_stored_and_counted(client, seed_post, actor, db):
    user, headers = actor
    post_id = seed_post(source="faculty")

    response = client.post(f"{FEED}/{post_id}/comments", json={"text": "  Labas  "},
                           headers=headers)

    body = response.get_json()
    assert response.status_code == 201
    assert set(body) == {"id", "text", "time", "userName", "userAvatar", "userId"}
    assert body["text"] == "Labas"
    assert body["userId"] == user["id"]
    assert body["time"].endswith("+00:00")
    assert db.execute("SELECT comments_count FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["comments_count"] == 1


def test_the_comment_counter_is_recomputed_not_bumped(client, seed_post, actor, db):
    _, headers = actor
    post_id = seed_post(source="faculty", comments=41)

    client.post(f"{FEED}/{post_id}/comments", json={"text": "Labas"}, headers=headers)

    assert db.execute("SELECT comments_count FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["comments_count"] == 1




# -----------------------------------------------------------
# DELETE /api/news/<id>/comments/<id> — delete_comment
# -----------------------------------------------------------


def test_deleting_a_comment_without_a_token_is_refused(client, seed_post, seed_comment, actor):
    user, _ = actor
    post_id = seed_post(source="faculty")
    comment_id = seed_comment(post_id, user["id"])

    assert client.delete(f"{FEED}/{post_id}/comments/{comment_id}").status_code == 401


def test_deleting_a_comment_of_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.delete(f"{FEED}/{uuid.uuid4()}/comments/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Post not found"


def test_a_comment_id_from_another_thread_is_a_404(client, seed_post, seed_comment, actor):
    user, headers = actor
    mine = seed_post(source="user", post_type="social", author_id=user["id"])
    other = seed_post(source="faculty")
    comment_id = seed_comment(other, user["id"])

    response = client.delete(f"{FEED}/{mine}/comments/{comment_id}", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Comment not found"


def test_a_stranger_cannot_delete_someone_elses_comment(client, seed_post, seed_comment,
                                                        actor, make_user):
    _, headers = actor
    author = make_user()
    post_id = seed_post(source="faculty")
    comment_id = seed_comment(post_id, author["id"])

    response = client.delete(f"{FEED}/{post_id}/comments/{comment_id}", headers=headers)

    assert response.status_code == 403
    assert "comment author" in response.get_json()["error"]


def test_the_comment_author_deletes_their_own(client, seed_post, seed_comment, actor, db):
    user, headers = actor
    post_id = seed_post(source="faculty")
    comment_id = seed_comment(post_id, user["id"])

    response = client.delete(f"{FEED}/{post_id}/comments/{comment_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted", "comments": 0}
    assert db.execute("SELECT COUNT(*) c FROM news_comments WHERE id = ?",
                      (comment_id,)).fetchone()["c"] == 0


def test_the_post_author_deletes_a_comment_on_their_post(client, seed_post, seed_comment,
                                                         actor, make_user):
    user, headers = actor
    guest = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])
    comment_id = seed_comment(post_id, guest["id"])

    assert client.delete(f"{FEED}/{post_id}/comments/{comment_id}", headers=headers).status_code == 200


def test_an_admin_deletes_any_comment(client, seed_post, seed_comment, admin, make_user):
    _, headers = admin
    author = make_user()
    post_id = seed_post(source="faculty")
    comment_id = seed_comment(post_id, author["id"])

    assert client.delete(f"{FEED}/{post_id}/comments/{comment_id}", headers=headers).status_code == 200




# -----------------------------------------------------------
# The poll routes
# -----------------------------------------------------------


def test_the_poll_of_a_post_without_one_is_a_404(client, seed_post):
    post_id = seed_post(source="faculty")

    response = client.get(f"{FEED}/{post_id}/poll")

    assert response.status_code == 404
    assert response.get_json()["error"] == "No poll found for this post"


def test_the_poll_of_an_unknown_post_is_the_same_404(client):
    response = client.get(f"{FEED}/{uuid.uuid4()}/poll")

    assert response.status_code == 404
    assert response.get_json()["error"] == "No poll found for this post"


def test_a_private_posts_poll_is_not_readable_by_a_stranger(client, seed_post, make_user,
                                                            seed_poll, actor):
    _, headers = actor
    stranger = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=stranger["id"], is_public=0)
    seed_poll(post_id)

    assert client.get(f"{FEED}/{post_id}/poll").status_code == 404
    assert client.get(f"{FEED}/{post_id}/poll", headers=headers).status_code == 404


@pytest.mark.contract
def test_the_poll_route_serves_the_poll_shape(client, seed_post, seed_poll, actor):
    user, headers = actor
    post_id = seed_post(source="faculty")
    poll_id, option_ids = seed_poll(post_id, options=("Taip", "Ne"), voter=user["id"])

    body = client.get(f"{FEED}/{post_id}/poll", headers=headers).get_json()

    assert set(body) == {"id", "postId", "title", "endDate", "totalVotes", "createdAt",
                         "userVote", "options"}
    assert body["id"] == poll_id
    assert body["userVote"] == option_ids[0]
    assert [o["id"] for o in body["options"]] == option_ids


def test_a_guest_reads_a_poll_without_a_vote(client, seed_post, seed_poll):
    post_id = seed_post(source="faculty")
    seed_poll(post_id)

    assert client.get(f"{FEED}/{post_id}/poll").get_json()["userVote"] is None


def test_creating_a_poll_without_a_token_is_refused(client, seed_post):
    post_id = seed_post(source="faculty")

    assert client.post(f"{FEED}/{post_id}/poll", json={"title": "Kas?"}).status_code == 401


def test_a_poll_needs_a_body(client, seed_post, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_poll_body_that_is_not_an_object_is_refused(client, seed_post, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll", json=["a", "b"], headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("title,error", [
    (None, "Poll title required"),
    ("   ", "Poll title required"),
    (5, "title must be a string"),
])
def test_a_poll_title_is_required_and_typed(client, seed_post, actor, title, error):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": title, "options": ["a", "b"]}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == error


def test_an_overlong_poll_title_is_refused(client, seed_post, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "x" * 201, "options": ["a", "b"]}, headers=headers)

    assert response.status_code == 400
    assert "at most 200" in response.get_json()["error"]


@pytest.mark.parametrize("options", ["ab", {"a": 1}, [1, 2], 5])
def test_poll_options_must_be_a_list_of_strings(client, seed_post, actor, options):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": options}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "options must be an array of strings"


@pytest.mark.parametrize("options", [[], ["a"], ["a", "   "]])
def test_a_poll_needs_two_real_options(client, seed_post, actor, options):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": options}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "At least 2 options required"


def test_more_than_ten_options_are_refused(client, seed_post, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": [f"o{n}" for n in range(11)]},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Maximum 10 options allowed"


def test_an_overlong_option_is_refused(client, seed_post, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": ["a", "x" * 101]}, headers=headers)

    assert response.status_code == 400
    assert "at most 100" in response.get_json()["error"]


def test_an_unparseable_end_date_is_refused(client, seed_post, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": ["a", "b"], "end_date": "rytoj"},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "end_date must be an ISO-8601 timestamp"


def test_a_poll_on_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.post(f"{FEED}/{uuid.uuid4()}/poll",
                           json={"title": "Kas?", "options": ["a", "b"]}, headers=headers)

    assert response.status_code == 404


def test_only_the_author_or_an_admin_may_attach_a_poll(client, seed_post, actor, make_user):
    _, headers = actor
    author = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=author["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": ["a", "b"]}, headers=headers)

    assert response.status_code == 403
    assert "author or admin" in response.get_json()["error"]


def test_a_scraped_article_cannot_carry_a_poll(client, seed_post, admin):
    _, headers = admin
    post_id = seed_post(source="knf.vu.lt", source_url="https://knf.vu.lt/a")

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": ["a", "b"]}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "A scraped article cannot carry a poll"


def test_a_second_poll_on_one_post_is_a_409(client, seed_post, seed_poll, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])
    seed_poll(post_id)

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": ["a", "b"]}, headers=headers)

    assert response.status_code == 409
    assert response.get_json()["error"] == "Post already has a poll"


def test_attaching_a_poll_flips_the_post_type(client, seed_post, actor, db):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.post(f"{FEED}/{post_id}/poll",
                           json={"title": "Kas?", "options": [" Taip ", "Ne", ""],
                                 "end_date": "2030-01-01 12:00:00"},
                           headers=headers)

    body = response.get_json()
    assert response.status_code == 201
    assert body["title"] == "Kas?"
    assert body["userVote"] is None
    assert body["totalVotes"] == 0
    assert [o["text"] for o in body["options"]] == ["Taip", "Ne"]
    assert body["endDate"] == "2030-01-01T12:00:00+00:00"
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?",
                      (post_id,)).fetchone()["post_type"] == "poll"


def test_an_integrity_error_on_the_poll_insert_answers_the_same_409(client, seed_post, seed_poll,
                                                                   actor, monkeypatch, db):
    # The friendly pre-check cannot see a poll another request is
    # inserting right now; migration v26's unique index is the
    # real guard, and its IntegrityError must answer 409, not
    # 500. Colliding the polls PK reproduces that write exactly
    import types

    user, headers = actor
    taken = seed_post(source="faculty", days=-1)
    poll_id, _ = seed_poll(taken)
    mine = seed_post(source="user", post_type="social", author_id=user["id"], days=-2)

    monkeypatch.setattr("app.news.routes.uuid", types.SimpleNamespace(uuid4=lambda: poll_id))

    response = client.post(f"{FEED}/{mine}/poll",
                           json={"title": "Kas?", "options": ["a", "b"]}, headers=headers)

    assert response.status_code == 409
    assert response.get_json()["error"] == "Post already has a poll"
    assert db.execute("SELECT COUNT(*) c FROM polls WHERE post_id = ?", (mine,)).fetchone()["c"] == 0
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?",
                      (mine,)).fetchone()["post_type"] == "social"


def test_the_poll_batch_of_an_empty_page_costs_no_query(app, db):
    # The helper's own guard: get_feed and get_post both skip it
    # on an empty page, so nothing else can reach this branch
    from app.news.routes import _polls_for_posts

    assert _polls_for_posts(db, []) == {}


def test_deleting_a_poll_without_a_token_is_refused(client, seed_post, seed_poll):
    post_id = seed_post(source="faculty")
    seed_poll(post_id)

    assert client.delete(f"{FEED}/{post_id}/poll").status_code == 401


def test_deleting_the_poll_of_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    assert client.delete(f"{FEED}/{uuid.uuid4()}/poll", headers=headers).status_code == 404


def test_only_the_author_or_an_admin_may_detach_a_poll(client, seed_post, seed_poll, actor,
                                                       make_user):
    _, headers = actor
    author = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=author["id"])
    seed_poll(post_id)

    response = client.delete(f"{FEED}/{post_id}/poll", headers=headers)

    assert response.status_code == 403
    assert "author or admin" in response.get_json()["error"]


def test_detaching_a_poll_that_is_not_there_is_a_404(client, seed_post, actor):
    user, headers = actor
    post_id = seed_post(source="user", post_type="social", author_id=user["id"])

    response = client.delete(f"{FEED}/{post_id}/poll", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "No poll found for this post"


@pytest.mark.parametrize("source,restored", [
    ("user", "social"),
    ("faculty", "announcement"),
    ("knf.vu.lt", "article"),
])
def test_detaching_a_poll_restores_the_post_type(client, seed_post, seed_poll, admin, db,
                                                 source, restored):
    _, headers = admin
    post_id = seed_post(source=source, source_url=None if source == "user" else None)
    poll_id, option_ids = seed_poll(post_id)

    response = client.delete(f"{FEED}/{post_id}/poll", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted", "postType": restored}
    assert db.execute("SELECT COUNT(*) c FROM polls WHERE id = ?", (poll_id,)).fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM poll_options WHERE poll_id = ?",
                      (poll_id,)).fetchone()["c"] == 0




# -----------------------------------------------------------
# POST /api/news/<id>/poll/vote — vote_poll
# -----------------------------------------------------------


def test_voting_without_a_token_is_refused(client, seed_post, seed_poll):
    post_id = seed_post(source="faculty")
    _, option_ids = seed_poll(post_id)

    assert client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_ids[0]}).status_code == 401


def test_a_vote_needs_a_body(client, seed_post, seed_poll, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")
    seed_poll(post_id)

    response = client.post(f"{FEED}/{post_id}/poll/vote", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON object body required"


def test_a_vote_body_that_is_not_an_object_is_refused(client, seed_post, seed_poll, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")
    seed_poll(post_id)

    response = client.post(f"{FEED}/{post_id}/poll/vote", json=[1], headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"


@pytest.mark.parametrize("option_id", [None, "", "   ", 5, {"id": "x"}, ["x"]])
def test_a_vote_needs_a_non_blank_option_id(client, seed_post, seed_poll, actor, option_id):
    _, headers = actor
    post_id = seed_post(source="faculty")
    seed_poll(post_id)

    response = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_id},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "option_id required"


def test_voting_on_an_unknown_post_is_a_404(client, actor):
    _, headers = actor

    response = client.post(f"{FEED}/{uuid.uuid4()}/poll/vote", json={"option_id": "x"},
                           headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "No poll found for this post"


def test_voting_on_a_post_without_a_poll_is_a_404(client, seed_post, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")

    response = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": "x"}, headers=headers)

    assert response.status_code == 404


def test_a_stranger_cannot_vote_on_a_private_posts_poll(client, seed_post, seed_poll, actor,
                                                        make_user):
    _, headers = actor
    stranger = make_user()
    post_id = seed_post(source="user", post_type="social", author_id=stranger["id"], is_public=0)
    _, option_ids = seed_poll(post_id)

    response = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_ids[0]},
                           headers=headers)

    assert response.status_code == 404


def test_a_closed_poll_refuses_a_vote(client, seed_post, seed_poll, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")
    _, option_ids = seed_poll(post_id, end_date=_iso(-1))

    response = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_ids[0]},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Poll has ended"


def test_an_unparseable_end_date_leaves_the_poll_open(client, seed_post, seed_poll, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")
    _, option_ids = seed_poll(post_id, end_date="niekada")

    response = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_ids[0]},
                           headers=headers)

    assert response.status_code == 200


def test_an_option_from_another_poll_is_refused(client, seed_post, seed_poll, actor):
    _, headers = actor
    mine = seed_post(source="faculty", days=-1)
    other = seed_post(source="faculty", days=-2)
    seed_poll(mine)
    _, foreign = seed_poll(other)

    response = client.post(f"{FEED}/{mine}/poll/vote", json={"option_id": foreign[0]},
                           headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid option"


def test_a_vote_is_counted_on_its_option_and_the_poll(client, seed_post, seed_poll, actor):
    _, headers = actor
    post_id = seed_post(source="faculty")
    _, option_ids = seed_poll(post_id, options=("Taip", "Ne"))

    body = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_ids[1]},
                       headers=headers).get_json()

    assert body["userVote"] == option_ids[1]
    assert body["totalVotes"] == 1
    assert [o["votes"] for o in body["options"]] == [0, 1]


def test_moving_a_vote_recomputes_both_options(client, seed_post, seed_poll, actor):
    user, headers = actor
    post_id = seed_post(source="faculty")
    _, option_ids = seed_poll(post_id, options=("Taip", "Ne"), voter=user["id"], voted_index=0)

    body = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_ids[1]},
                       headers=headers).get_json()

    assert body["userVote"] == option_ids[1]
    assert body["totalVotes"] == 1
    assert [o["votes"] for o in body["options"]] == [0, 1]


def test_voting_for_the_option_already_held_is_a_409(client, seed_post, seed_poll, actor):
    user, headers = actor
    post_id = seed_post(source="faculty")
    _, option_ids = seed_poll(post_id, voter=user["id"], voted_index=0)

    response = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_ids[0]},
                           headers=headers)

    assert response.status_code == 409
    assert response.get_json()["error"] == "Already voted for this option"


def test_a_drifted_tally_heals_on_the_next_vote(client, seed_post, seed_poll, actor, db):
    _, headers = actor
    post_id = seed_post(source="faculty")
    poll_id, option_ids = seed_poll(post_id, options=("Taip", "Ne"))
    db.execute("UPDATE polls SET total_votes = 99 WHERE id = ?", (poll_id,))
    db.execute("UPDATE poll_options SET votes = 42 WHERE poll_id = ?", (poll_id,))
    db.commit()

    body = client.post(f"{FEED}/{post_id}/poll/vote", json={"option_id": option_ids[0]},
                       headers=headers).get_json()

    assert body["totalVotes"] == 1
    assert [o["votes"] for o in body["options"]] == [1, 0]
