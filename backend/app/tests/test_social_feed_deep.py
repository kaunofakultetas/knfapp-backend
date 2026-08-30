# -----------------------------------------------------------
#  [*] Tests — the exhaustive pass over social_feed,
#      _post_row_to_dict and _parse_before
#
#  test_social_posts.py already walks the happy paths of
#  app/social/routes.py. This module is the gap-closing pass
#  over three functions and nothing else: every arm of every
#  branch in them, every guard, every boundary either side of
#  a limit, and the unhappy paths a caller can actually
#  reach.
#
#  What it proves that the broad suite does not:
#
#    - _parse_before as a UNIT: the "no pin" arm, the
#      first-attempt parse (Z suffix, offsets, naive, date
#      only, basic form, ISO weeks, fractions), the
#      second-attempt repair of a "+" that decoded to a
#      space, the AttributeError arm a non-string reaches,
#      and the exact 400 tuple both failures share. Plus the
#      asymmetries the implementation actually has: a
#      lowercase "z" is NOT a Z, and a whitespace-only pin
#      is a 400 by way of the repair attempt.
#    - the datetime.min / datetime.max corners, where a far
#      offset makes the final astimezone() overflow — the
#      conversion is inside the guard, so they answer the
#      same 400 as any other bad pin.
#    - _post_row_to_dict as a UNIT against real joined rows:
#      the exact 18-key wire shape, the truncate flag either
#      side of SUMMARY_LENGTH (199 / 200 / 201), truncate
#      OFF, an empty body, the author fallback chain
#      (current display name → snapshot → None) including a
#      BLANK current name, a non-boolean is_public column
#      coming out as a JSON boolean, and a character-counted
#      (not byte-counted) trim.
#    - social_feed's remaining branches: every pagination
#      refusal and the two accepted-but-odd int() spellings,
#      both cap boundaries, hasMore either side of equality,
#      the empty-page-past-the-end case, a signed-in reader
#      with an EMPTY feed (the liked query is skipped), the
#      visible-ids splice with zero, one and many friends,
#      a self-friendship row, role permutations, a
#      deactivated or expired session degrading to the guest
#      view, the ORDER BY tie-breakers reached through
#      unrankable timestamps, the MAX(0, …) clamp on a
#      future post, the engagement cap, the recency window's
#      exact date boundary, and the ?before pin moving the
#      SCORE clock and not only the filter.
#
#  Every state is arranged with direct INSERTs on the `db`
#  fixture — old posts, unrankable timestamps, blank display
#  names and half-written friendships are all states no route
#  can create.
# -----------------------------------------------------------

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import time_machine

from app.api import SUMMARY_LENGTH
from app.social.routes import (
    _FEED_MAX_PAGE,
    _FEED_PER_PAGE_MAX,
    _FEED_WINDOW_DAYS,
    _parse_before,
    _post_row_to_dict,
)

FEED = "/api/social/feed"

# The 18 keys _post_row_to_dict answers with — the mobile
# card reads summary || content and refetches the full body,
# so both halves of the truncation pair must always ship
WIRE_KEYS = {
    "id", "title", "content", "summary", "imageUrl", "author", "authorId",
    "authorAvatar", "source", "sourceUrl", "postType", "likes", "comments",
    "shares", "date", "isPublic", "liked", "truncated",
}

ENVELOPE_KEYS = {"posts", "page", "perPage", "total", "hasMore"}




# -----------------------------------------------------------
# _fresh_rate_limits
# -----------------------------------------------------------
#
# The limiter's store is a module-level dict that outlives the
# app fixture, so without this the global per-IP budget (600
# requests per 5 minutes) would carry across tests and this
# file would start 429ing partway through. Cleared on both
# sides so neighbouring modules are no worse off either.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    from app.auth.routes import _rate_limit_store

    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _seed
# -----------------------------------------------------------
#
# One news_posts row, straight in. Every column the feed
# ranks, filters or serialises is a parameter, because most
# of the states under test here (an unrankable published_at,
# an is_public of 2, a NULL author_name) no route will ever
# write.
# -----------------------------------------------------------

def _seed(db, author_id, content="Turinys", title="Antraste", is_public=1,
          source="user", post_type="social", published_at=None, likes=0,
          comments=0, shares=0, image_url=None, author_name="Momentine kopija",
          summary="Santrauka", source_url=None):
    post_id = str(uuid.uuid4())
    stamp = published_at if published_at is not None else _stamp()

    db.execute(
        """INSERT INTO news_posts
           (id, title, content, summary, image_url, author_id, author_name, source,
            source_url, post_type, is_public, likes_count, comments_count, shares_count,
            published_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, title, content, summary, image_url, author_id, author_name, source,
         source_url, post_type, is_public, likes, comments, shares,
         stamp, stamp, stamp),
    )
    db.commit()
    return post_id




# -----------------------------------------------------------
# _befriend
# -----------------------------------------------------------
#
# The accepted-friendship state of record: one row PER
# direction. both=False arranges the half-written pair a
# crash can leave behind — the feed only ever reads the
# viewer's own direction.
# -----------------------------------------------------------

def _befriend(db, user_id, friend_id, both=True):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user_id, friend_id))
    if both:
        db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (friend_id, user_id))
    db.commit()




# -----------------------------------------------------------
# _stamp / _ids / _joined / _cutoff / _pin
# -----------------------------------------------------------
#
# _stamp   — a T-form UTC published_at n days / seconds back
# _ids     — the post ids of a feed response, in order
# _joined  — one row with the au.* aliases _post_row_to_dict
#            requires, fetched exactly as the routes fetch it
# _cutoff  — the recency floor computed by SQLite itself, so
#            the boundary tests cannot drift against the
#            clock Python happens to read
# _pin     — a ?before value safe to put in a query string
#            (an un-encoded "+" would arrive as a space)
# -----------------------------------------------------------

def _stamp(days=0, seconds=0):
    moment = datetime.now(timezone.utc) - timedelta(days=days, seconds=seconds)
    return moment.replace(microsecond=0).isoformat()


def _ids(response):
    return [p["id"] for p in response.get_json()["posts"]]


def _joined(db, post_id):
    return db.execute(
        """SELECT p.*,
                  au.avatar_url AS author_avatar,
                  au.display_name AS author_display_name
           FROM news_posts p
           LEFT JOIN users au ON au.id = p.author_id
           WHERE p.id = ?""",
        (post_id,),
    ).fetchone()


def _cutoff(db):
    return db.execute(f"SELECT date('now', '-{_FEED_WINDOW_DAYS} day') AS d").fetchone()["d"]


# _post_row_to_dict only ever subscripts its argument, so a
# plain mapping stands in for a sqlite3.Row wherever the state
# under test is one the schema itself forbids (a NULL
# display_name, a NULL content)
def _row(**overrides):
    row = {
        "id": "p1", "title": "Antraste", "content": "Turinys", "summary": "Santrauka",
        "image_url": None, "author_id": "u1", "author_name": "Momentine kopija",
        "author_display_name": "Dabartinis", "author_avatar": None, "source": "user",
        "source_url": None, "post_type": "social", "likes_count": 0,
        "comments_count": 0, "shares_count": 0, "published_at": "2026-05-01T09:00:00+00:00",
        "is_public": 1,
    }
    row.update(overrides)
    return row


def _pin(iso):
    return iso.replace("+00:00", "Z")




# -----------------------------------------------------------
# _before
# -----------------------------------------------------------
#
# _parse_before under a real request context, so the unit
# tests read the same request.args the route does. `raw` is
# percent-encoded whole, which is how a correct client sends
# a "+00:00" offset in the first place.
# -----------------------------------------------------------

def _before(app, raw):
    with app.test_request_context("/api/social/feed?before=" + quote(raw, safe="")):
        return _parse_before()




# -----------------------------------------------------------
# _parse_before — the "no pin" arm
# -----------------------------------------------------------

def test_an_absent_before_pins_nothing_and_raises_nothing(app):
    with app.test_request_context("/api/social/feed"):
        assert _parse_before() == (None, None)


def test_an_absent_before_is_not_confused_with_another_parameter(app):
    with app.test_request_context("/api/social/feed?page=2&per_page=5"):
        assert _parse_before() == (None, None)




# -----------------------------------------------------------
# _parse_before — the first-attempt parse
# -----------------------------------------------------------

def test_a_z_suffix_becomes_an_explicit_utc_offset(app):
    value, err = _before(app, "2026-01-01T10:00:00Z")

    assert err is None
    assert value == "2026-01-01T10:00:00+00:00"


def test_an_already_explicit_utc_offset_survives_unchanged(app):
    value, err = _before(app, "2026-01-01T10:00:00+00:00")

    assert err is None
    assert value == "2026-01-01T10:00:00+00:00"


def test_a_naive_pin_is_read_as_utc_without_moving_the_clock(app):
    value, err = _before(app, "2026-01-01T10:00:00")

    assert err is None
    assert value == "2026-01-01T10:00:00+00:00"


def test_an_eastern_offset_is_shifted_back_to_utc(app):
    value, _ = _before(app, "2026-01-01T10:00:00+03:00")

    assert value == "2026-01-01T07:00:00+00:00"


def test_a_western_offset_is_shifted_forward_to_utc(app):
    value, _ = _before(app, "2026-01-01T10:00:00-05:30")

    assert value == "2026-01-01T15:30:00+00:00"


def test_an_offset_that_crosses_midnight_moves_the_date_too(app):
    value, _ = _before(app, "2026-01-01T01:00:00+03:00")

    assert value == "2025-12-31T22:00:00+00:00"


def test_a_date_only_pin_is_read_as_midnight_utc(app):
    value, err = _before(app, "2026-01-01")

    assert err is None
    assert value == "2026-01-01T00:00:00+00:00"


def test_a_space_separated_pin_parses_on_the_first_attempt(app):
    # The repair attempt would turn this into "2026-01-01+10:00:00"
    # and fail, so reaching it at all would be a bug
    value, err = _before(app, "2026-01-01 10:00:00")

    assert err is None
    assert value == "2026-01-01T10:00:00+00:00"


def test_a_basic_format_pin_is_accepted(app):
    value, _ = _before(app, "20260101T100000")

    assert value == "2026-01-01T10:00:00+00:00"


def test_an_iso_week_date_pin_is_accepted(app):
    value, _ = _before(app, "2026-W01-1")

    assert value == "2025-12-29T00:00:00+00:00"


def test_fractional_seconds_survive_the_round_trip(app):
    value, _ = _before(app, "2026-01-01T10:00:00.123456Z")

    assert value == "2026-01-01T10:00:00.123456+00:00"


def test_the_first_january_of_year_one_is_a_valid_pin(app):
    value, err = _before(app, "0001-01-01T00:00:00Z")

    assert err is None
    assert value == "0001-01-01T00:00:00+00:00"


def test_the_last_second_of_year_nine_thousand_nine_hundred_ninety_nine_is_a_valid_pin(app):
    value, err = _before(app, "9999-12-31T23:59:59Z")

    assert err is None
    assert value == "9999-12-31T23:59:59+00:00"




# -----------------------------------------------------------
# _parse_before — the second attempt, for a "+" that decoded
# to a space
# -----------------------------------------------------------

def test_an_offset_that_arrived_as_a_space_is_repaired(app):
    value, err = _before(app, "2026-01-01T10:00:00 03:00")

    assert err is None
    assert value == "2026-01-01T07:00:00+00:00"


def test_a_z_and_a_spaced_offset_never_meet(app):
    # The Z replacement runs on the FIRST attempt only, so a
    # value carrying both spellings is simply unparseable
    _, err = _before(app, "2026-01-01T10:00:00Z 03:00")

    assert err[1] == 400


def test_a_lone_space_is_refused_by_both_attempts(app):
    _, err = _before(app, " ")

    assert err is not None
    assert err[1] == 400


def test_the_repair_replaces_every_space_not_just_the_offsets(app):
    # "2026-01-01 10:00:00 03:00" fails the first attempt, and
    # the repair turns BOTH spaces into pluses — the date/time
    # separator may be any single character since Python 3.11,
    # so "2026-01-01+10:00:00+03:00" parses anyway
    value, err = _before(app, "2026-01-01 10:00:00 03:00")

    assert err is None
    assert value == "2026-01-01T07:00:00+00:00"




# -----------------------------------------------------------
# _parse_before — the refusals
# -----------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "",
    "vakar",
    "not-a-date",
    "2026-13-01T00:00:00Z",
    "2026-01-32",
    "2026-01-01T25:00:00Z",
    "2026-01-01T10:00:00z",
    "ZZZ",
    "0",
    "1767261600",
    "2026/01/01",
    "01-01-2026",
])
def test_an_unparseable_pin_is_a_ready_made_four_hundred(app, raw):
    value, err = _before(app, raw)

    assert value is None
    assert err[1] == 400
    assert err[0].get_json() == {
        "error": "before must be an ISO-8601 timestamp",
        "code": "invalid_before",
    }


def test_a_lowercase_z_is_not_a_utc_suffix(app):
    # Only "Z" is replaced, and datetime.fromisoformat rejects
    # the lowercase spelling — an asymmetry worth pinning
    _, err = _before(app, "2026-01-01T10:00:00z")

    assert err[1] == 400


def test_a_non_string_pin_is_refused_instead_of_crashing(app, monkeypatch):
    # request.args always hands back str, so this is the
    # AttributeError arm's only reachable driver — and it must
    # answer the same stable 400 as any other bad pin
    import app.social.routes as social_routes

    with app.test_request_context("/api/social/feed"):
        monkeypatch.setattr(social_routes, "request", SimpleNamespace(args={"before": 1767261600}))
        value, err = _parse_before()

    assert value is None
    assert err[1] == 400
    assert err[0].get_json()["code"] == "invalid_before"


@pytest.mark.parametrize("raw", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
def test_a_pin_at_the_edge_of_the_datetime_range_is_refused_not_crashed(app, raw):
    # Both parse, and both overflow on the shift to UTC — the
    # conversion is inside the guard, so they leave by the same
    # 400 as any other bad pin instead of 500ing the feed
    value, err = _before(app, raw)

    assert value is None
    assert err is not None
    assert err[1] == 400
    assert err[0].get_json()["code"] == "invalid_before"




# -----------------------------------------------------------
# _parse_before — through the route
# -----------------------------------------------------------

def test_the_route_answers_the_invalid_before_code(client):
    response = client.get(FEED, query_string={"before": "rytoj"})

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_before"


def test_the_pagination_guard_runs_before_the_pin_guard(client):
    response = client.get(FEED, query_string={"page": 0, "before": "rytoj"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"


def test_only_the_first_before_value_is_read(client, make_user, db):
    author = make_user()
    post_id = _seed(db, author["id"], published_at=_stamp(days=1))

    response = client.get(FEED + "?before=" + quote(_pin(_stamp()), safe="") + "&before=rytoj")

    assert response.status_code == 200
    assert _ids(response) == [post_id]




# -----------------------------------------------------------
# _post_row_to_dict — the wire shape
# -----------------------------------------------------------

def test_the_wire_shape_carries_exactly_the_documented_keys(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"]))

    assert set(_post_row_to_dict(row)) == WIRE_KEYS


def test_the_wire_shape_is_the_same_with_and_without_truncation(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], content="x" * 500))

    assert set(_post_row_to_dict(row)) == set(_post_row_to_dict(row, truncate=True))


def test_every_stored_column_rides_through_untouched(make_user, db):
    author = make_user()
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?", ("/api/uploads/a.jpg", author["id"]))
    db.commit()
    post_id = _seed(db, author["id"], title="Pavadinimas", content="Kunas",
                    summary="Trumpai", image_url="/api/uploads/b.jpg",
                    source_url=None, post_type="social", likes=3, comments=2, shares=1,
                    published_at="2026-05-01T09:00:00+00:00")

    shape = _post_row_to_dict(_joined(db, post_id))

    assert shape["id"] == post_id
    assert shape["title"] == "Pavadinimas"
    assert shape["content"] == "Kunas"
    assert shape["summary"] == "Trumpai"
    assert shape["imageUrl"] == "/api/uploads/b.jpg"
    assert shape["authorId"] == author["id"]
    assert shape["authorAvatar"] == "/api/uploads/a.jpg"
    assert shape["source"] == "user"
    assert shape["sourceUrl"] is None
    assert shape["postType"] == "social"
    assert shape["likes"] == 3
    assert shape["comments"] == 2
    assert shape["shares"] == 1
    assert shape["date"] == "2026-05-01T09:00:00+00:00"


def test_the_date_is_the_published_at_column_verbatim(make_user, db):
    # The scraper's space form is NOT normalised on the way out
    author = make_user()
    row = _joined(db, _seed(db, author["id"], published_at="2026-05-01 09:00:00"))

    assert _post_row_to_dict(row)["date"] == "2026-05-01 09:00:00"


def test_liked_always_starts_false(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"]))

    assert _post_row_to_dict(row)["liked"] is False




# -----------------------------------------------------------
# _post_row_to_dict — the truncation boundary
# -----------------------------------------------------------

def test_a_body_one_character_over_the_summary_length_is_trimmed(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], content="x" * (SUMMARY_LENGTH + 1)))

    shape = _post_row_to_dict(row, truncate=True)

    assert shape["truncated"] is True
    assert shape["content"] == "x" * SUMMARY_LENGTH


def test_a_body_exactly_at_the_summary_length_is_kept_whole(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], content="x" * SUMMARY_LENGTH))

    shape = _post_row_to_dict(row, truncate=True)

    assert shape["truncated"] is False
    assert shape["content"] == "x" * SUMMARY_LENGTH


def test_a_body_one_character_under_the_summary_length_is_kept_whole(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], content="x" * (SUMMARY_LENGTH - 1)))

    assert _post_row_to_dict(row, truncate=True)["truncated"] is False


def test_truncation_is_off_unless_the_caller_asks_for_it(make_user, db):
    author = make_user()
    body = "x" * (SUMMARY_LENGTH * 10)
    row = _joined(db, _seed(db, author["id"], content=body))

    shape = _post_row_to_dict(row)

    assert shape["truncated"] is False
    assert shape["content"] == body


def test_an_empty_body_is_an_empty_string_and_never_truncated(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], content=""))

    shape = _post_row_to_dict(row, truncate=True)

    assert shape["content"] == ""
    assert shape["truncated"] is False


def test_a_null_body_is_normalised_to_an_empty_string(db):
    # news_posts.content is NOT NULL, so the "or ''" guard is
    # only ever reachable off-schema — it still must not blow
    # up on len(None)
    shape = _post_row_to_dict(_row(content=None), truncate=True)

    assert shape["content"] == ""
    assert shape["truncated"] is False


def test_a_null_is_public_answers_a_json_false(db):
    assert _post_row_to_dict(_row(is_public=None))["isPublic"] is False


def test_the_trim_counts_characters_not_bytes(make_user, db):
    # Every one of these is two bytes in UTF-8; a byte-counted
    # cut would return 100 of them, or split one in half
    author = make_user()
    row = _joined(db, _seed(db, author["id"], content="ą" * (SUMMARY_LENGTH + 1)))

    assert _post_row_to_dict(row, truncate=True)["content"] == "ą" * SUMMARY_LENGTH


def test_the_stored_summary_is_untouched_by_the_body_trim(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], content="x" * 500, summary="Rankine santrauka"))

    shape = _post_row_to_dict(row, truncate=True)

    assert shape["truncated"] is True
    assert shape["summary"] == "Rankine santrauka"


def test_a_null_summary_column_stays_null(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], summary=None))

    assert _post_row_to_dict(row)["summary"] is None




# -----------------------------------------------------------
# _post_row_to_dict — the author fallback chain
# -----------------------------------------------------------

def test_the_current_display_name_wins_over_the_snapshot(make_user, db):
    author = make_user(display_name="Dabartinis")
    row = _joined(db, _seed(db, author["id"], author_name="Senas"))

    assert _post_row_to_dict(row)["author"] == "Dabartinis"


def test_an_authorless_row_falls_back_to_the_snapshot(db):
    row = _joined(db, _seed(db, None, author_name="Naujienu tarnyba"))

    shape = _post_row_to_dict(row)

    assert shape["author"] == "Naujienu tarnyba"
    assert shape["authorId"] is None
    assert shape["authorAvatar"] is None


def test_a_blank_current_display_name_falls_back_to_the_snapshot(make_user, db):
    author = make_user()
    db.execute("UPDATE users SET display_name = '' WHERE id = ?", (author["id"],))
    db.commit()
    row = _joined(db, _seed(db, author["id"], author_name="Senas"))

    assert _post_row_to_dict(row)["author"] == "Senas"


@pytest.mark.parametrize("current, snapshot, expected", [
    ("Dabartinis", "Senas", "Dabartinis"),
    ("Dabartinis", None, "Dabartinis"),
    ("", "Senas", "Senas"),
    (None, "Senas", "Senas"),
    ("", "", ""),
    ("", None, None),
    (None, None, None),
])
def test_the_author_fallback_matrix(current, snapshot, expected):
    # _post_row_to_dict only ever subscripts its row, so a plain
    # mapping drives the whole "or" chain — including the two
    # NULL combinations users.display_name (NOT NULL) forbids
    row = _row(author_display_name=current, author_name=snapshot)

    assert _post_row_to_dict(row)["author"] == expected


def test_a_row_with_neither_name_answers_a_null_author(db):
    row = _joined(db, _seed(db, None, author_name=None))

    assert _post_row_to_dict(row)["author"] is None


def test_a_blank_snapshot_with_no_author_answers_an_empty_author(db):
    row = _joined(db, _seed(db, None, author_name=""))

    assert _post_row_to_dict(row)["author"] == ""




# -----------------------------------------------------------
# _post_row_to_dict — is_public
# -----------------------------------------------------------

def test_a_public_row_answers_a_json_true(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], is_public=1))

    assert _post_row_to_dict(row)["isPublic"] is True


def test_a_private_row_answers_a_json_false(make_user, db):
    author = make_user()
    row = _joined(db, _seed(db, author["id"], is_public=0))

    assert _post_row_to_dict(row)["isPublic"] is False


def test_an_out_of_range_is_public_column_still_answers_a_boolean(make_user, db):
    # The column carries no CHECK, so a hand-edit CAN put a 2
    # there — the wire contract stays a JSON boolean
    author = make_user()
    row = _joined(db, _seed(db, author["id"], is_public=2))

    assert _post_row_to_dict(row)["isPublic"] is True




# -----------------------------------------------------------
# social_feed — the pagination guards
# -----------------------------------------------------------

@pytest.mark.parametrize("page", [0, -1, -1000])
def test_a_non_positive_page_is_refused(client, page):
    response = client.get(FEED, query_string={"page": page})

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"


@pytest.mark.parametrize("page", ["abc", "3.0", "0x1", "", "1e3", "!"])
def test_a_non_integer_page_is_refused(client, page):
    response = client.get(FEED, query_string={"page": page})

    assert response.status_code == 400
    assert response.get_json()["error"] == "page must be a positive integer"


@pytest.mark.parametrize("per_page", [0, -1])
def test_a_non_positive_per_page_is_refused(client, per_page):
    response = client.get(FEED, query_string={"per_page": per_page})

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be a positive integer"


@pytest.mark.parametrize("per_page", ["abc", "50.0", ""])
def test_a_non_integer_per_page_is_refused(client, per_page):
    response = client.get(FEED, query_string={"per_page": per_page})

    assert response.status_code == 400
    assert response.get_json()["error"] == "per_page must be a positive integer"


def test_a_page_one_over_the_feed_cap_is_refused(client):
    response = client.get(FEED, query_string={"page": _FEED_MAX_PAGE + 1})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"page must be at most {_FEED_MAX_PAGE}"


def test_a_per_page_one_over_the_feed_cap_is_refused(client):
    response = client.get(FEED, query_string={"per_page": _FEED_PER_PAGE_MAX + 1})

    assert response.status_code == 400
    assert response.get_json()["error"] == f"per_page must be at most {_FEED_PER_PAGE_MAX}"


def test_the_feed_caps_are_its_own_not_the_shared_defaults(client):
    page_error = client.get(FEED, query_string={"page": 10_001}).get_json()["error"]

    assert "200" in page_error
    assert "10000" not in page_error


def test_a_page_exactly_at_the_cap_is_accepted(client):
    response = client.get(FEED, query_string={"page": _FEED_MAX_PAGE})

    assert response.status_code == 200
    assert response.get_json()["page"] == _FEED_MAX_PAGE


def test_a_per_page_exactly_at_the_cap_is_accepted(client):
    response = client.get(FEED, query_string={"per_page": _FEED_PER_PAGE_MAX})

    assert response.status_code == 200
    assert response.get_json()["perPage"] == _FEED_PER_PAGE_MAX


def test_a_per_page_of_one_returns_exactly_one_row(client, make_user, db):
    author = make_user()
    for _ in range(3):
        _seed(db, author["id"])

    body = client.get(FEED, query_string={"per_page": 1}).get_json()

    assert len(body["posts"]) == 1
    assert body["total"] == 3


def test_a_plus_signed_page_is_accepted(client):
    # int() takes "+2"; the plus has to be percent-encoded or
    # the query string would decode it to a space
    response = client.get(FEED + "?page=%2B2")

    assert response.status_code == 200
    assert response.get_json()["page"] == 2


def test_a_space_padded_per_page_is_accepted(client):
    response = client.get(FEED + "?per_page=%205%20")

    assert response.status_code == 200
    assert response.get_json()["perPage"] == 5


def test_the_defaults_are_page_one_and_twenty_per_page(client):
    body = client.get(FEED).get_json()

    assert body["page"] == 1
    assert body["perPage"] == 20




# -----------------------------------------------------------
# social_feed — the envelope, total and hasMore
# -----------------------------------------------------------

def test_the_envelope_carries_exactly_the_documented_keys(client):
    assert set(client.get(FEED).get_json()) == ENVELOPE_KEYS


def test_page_and_per_page_are_echoed_as_integers(client):
    body = client.get(FEED, query_string={"page": "2", "per_page": "7"}).get_json()

    assert body["page"] == 2 and isinstance(body["page"], int)
    assert body["perPage"] == 7 and isinstance(body["perPage"], int)


def test_has_more_is_true_while_rows_remain(client, make_user, db):
    author = make_user()
    for _ in range(3):
        _seed(db, author["id"])

    body = client.get(FEED, query_string={"per_page": 2}).get_json()

    assert body["total"] == 3
    assert body["hasMore"] is True


def test_has_more_is_false_when_the_page_exactly_exhausts_the_total(client, make_user, db):
    author = make_user()
    for _ in range(3):
        _seed(db, author["id"])

    body = client.get(FEED, query_string={"per_page": 3}).get_json()

    assert len(body["posts"]) == 3
    assert body["hasMore"] is False


def test_has_more_is_false_on_the_last_partial_page(client, make_user, db):
    author = make_user()
    for _ in range(3):
        _seed(db, author["id"])

    body = client.get(FEED, query_string={"per_page": 2, "page": 2}).get_json()

    assert len(body["posts"]) == 1
    assert body["hasMore"] is False


def test_a_per_page_larger_than_the_total_returns_everything_once(client, make_user, db):
    author = make_user()
    seeded = {_seed(db, author["id"]) for _ in range(4)}

    body = client.get(FEED, query_string={"per_page": 50}).get_json()

    assert set(p["id"] for p in body["posts"]) == seeded
    assert body["hasMore"] is False


def test_a_page_past_the_end_is_empty_but_still_reports_the_total(client, make_user, db):
    author = make_user()
    for _ in range(2):
        _seed(db, author["id"])

    body = client.get(FEED, query_string={"page": 9, "per_page": 10}).get_json()

    assert body["posts"] == []
    assert body["total"] == 2
    assert body["hasMore"] is False


def test_the_highest_reachable_offset_is_an_empty_page(client, make_user, db):
    author = make_user()
    _seed(db, author["id"])

    body = client.get(FEED, query_string={"page": _FEED_MAX_PAGE, "per_page": _FEED_PER_PAGE_MAX}).get_json()

    assert body["posts"] == []
    assert body["total"] == 1


def test_paging_never_repeats_or_drops_a_row(client, make_user, db):
    author = make_user()
    seeded = [_seed(db, author["id"], published_at=_stamp(seconds=600 * (i + 1))) for i in range(5)]

    first = _ids(client.get(FEED, query_string={"per_page": 2}))
    second = _ids(client.get(FEED, query_string={"per_page": 2, "page": 2}))
    third = _ids(client.get(FEED, query_string={"per_page": 2, "page": 3}))

    assert first + second + third == seeded


def test_unknown_query_parameters_are_ignored(client, make_user, db):
    author = make_user()
    post_id = _seed(db, author["id"])

    assert _ids(client.get(FEED, query_string={"user_id": "kazkas", "sort": "hot"})) == [post_id]


def test_the_feed_refuses_a_post(client):
    assert client.post(FEED, json={}).status_code == 405


def test_the_feed_needs_no_authentication_at_all(client):
    assert client.get(FEED).status_code == 200




# -----------------------------------------------------------
# social_feed — the visible-ids splice
# -----------------------------------------------------------

def test_a_reader_with_no_friends_still_sees_their_own_private_post(client, actor, db):
    user, headers = actor
    own_id = _seed(db, user["id"], is_public=0)

    assert _ids(client.get(FEED, headers=headers)) == [own_id]


def test_many_friends_all_widen_the_visible_set(client, actor, make_user, db):
    user, headers = actor
    expected = set()
    for _ in range(6):
        friend = make_user()
        _befriend(db, user["id"], friend["id"])
        expected.add(_seed(db, friend["id"], is_public=0))

    body = client.get(FEED, query_string={"per_page": 50}, headers=headers).get_json()

    assert set(p["id"] for p in body["posts"]) == expected
    assert body["total"] == 6


def test_a_self_friendship_row_does_not_duplicate_own_posts(client, actor, db):
    # visible_ids becomes [me, me] — two placeholders, one row
    user, headers = actor
    own_id = _seed(db, user["id"], is_public=0)
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user["id"], user["id"]))
    db.commit()

    body = client.get(FEED, headers=headers).get_json()

    assert [p["id"] for p in body["posts"]] == [own_id]
    assert body["total"] == 1


def test_a_friends_public_post_is_listed_once_not_twice(client, actor, make_user, db):
    # The OR arms both match — the WHERE must not double the row
    user, headers = actor
    friend = make_user()
    _befriend(db, user["id"], friend["id"])
    post_id = _seed(db, friend["id"], is_public=1)

    body = client.get(FEED, headers=headers).get_json()

    assert [p["id"] for p in body["posts"]] == [post_id]
    assert body["total"] == 1


def test_the_friendship_is_read_in_the_readers_direction_only(client, actor, make_user, db):
    user, headers = actor
    friend = make_user()
    # Only their row exists: they call us a friend, we do not
    _befriend(db, friend["id"], user["id"], both=False)
    _seed(db, friend["id"], is_public=0)

    body = client.get(FEED, headers=headers).get_json()

    assert body["posts"] == []
    assert body["total"] == 0


def test_an_admin_reader_gets_no_extra_visibility(client, admin, make_user, db):
    _, headers = admin
    stranger = make_user()
    private_id = _seed(db, stranger["id"], is_public=0)
    public_id = _seed(db, stranger["id"], is_public=1)

    ids = _ids(client.get(FEED, headers=headers))

    assert public_id in ids
    assert private_id not in ids


def test_a_teacher_reader_gets_no_extra_visibility(client, make_user, auth_headers, db):
    teacher = make_user(role="teacher")
    headers = auth_headers(teacher)
    stranger = make_user()
    private_id = _seed(db, stranger["id"], is_public=0)

    assert private_id not in _ids(client.get(FEED, headers=headers))


def test_the_total_covers_the_same_visibility_set_as_the_page(client, actor, make_user, db):
    user, headers = actor
    stranger = make_user()
    _seed(db, user["id"], is_public=0)
    _seed(db, stranger["id"], is_public=1)
    _seed(db, stranger["id"], is_public=0)

    body = client.get(FEED, headers=headers).get_json()

    assert body["total"] == 2
    assert len(body["posts"]) == 2


def test_a_deactivated_author_is_left_out_of_the_total_too(client, make_user, db):
    live = make_user()
    gone = make_user()
    _seed(db, live["id"])
    _seed(db, gone["id"])
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (gone["id"],))
    db.commit()

    assert client.get(FEED).get_json()["total"] == 1


def test_a_windowed_out_post_is_left_out_of_the_total_too(client, make_user, db):
    author = make_user()
    _seed(db, author["id"])
    _seed(db, author["id"], published_at=_stamp(days=_FEED_WINDOW_DAYS + 5))

    assert client.get(FEED).get_json()["total"] == 1




# -----------------------------------------------------------
# social_feed — who counts as the viewer
# -----------------------------------------------------------

def test_a_bearer_header_with_no_token_reads_as_a_guest(client, actor, db):
    user, _ = actor
    _seed(db, user["id"], is_public=0)

    body = client.get(FEED, headers={"Authorization": "Bearer"}).get_json()

    assert body["posts"] == []


def test_a_non_bearer_authorization_scheme_reads_as_a_guest(client, actor, db):
    user, _ = actor
    _seed(db, user["id"], is_public=0)

    body = client.get(FEED, headers={"Authorization": "Basic YWRtaW46YWRtaW4="}).get_json()

    assert body["posts"] == []


def test_a_deactivated_reader_falls_back_to_the_guest_view(client, actor, make_user, db):
    user, headers = actor
    friend = make_user()
    _befriend(db, user["id"], friend["id"])
    private_id = _seed(db, friend["id"], is_public=0)
    public_id = _seed(db, friend["id"], is_public=1)

    assert set(_ids(client.get(FEED, headers=headers))) == {private_id, public_id}

    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    assert _ids(client.get(FEED, headers=headers)) == [public_id]


def test_an_expired_session_reads_the_feed_as_a_guest(client, actor, make_user, db):
    user, headers = actor
    friend = make_user()
    _befriend(db, user["id"], friend["id"])
    private_id = _seed(db, friend["id"], is_public=0)
    public_id = _seed(db, friend["id"], is_public=1)

    assert set(_ids(client.get(FEED, headers=headers))) == {private_id, public_id}

    # Sessions live 30 days; day 31 is nobody
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=31), tick=False):
        assert _ids(client.get(FEED, headers=headers)) == [public_id]




# -----------------------------------------------------------
# social_feed — the liked flags
# -----------------------------------------------------------

def test_a_signed_in_reader_with_an_empty_feed_skips_the_liked_query(client, actor):
    _, headers = actor

    body = client.get(FEED, headers=headers).get_json()

    assert body == {"posts": [], "page": 1, "perPage": 20, "total": 0, "hasMore": False}


def test_only_the_posts_the_reader_liked_are_flagged(client, actor, make_user, db):
    user, headers = actor
    author = make_user()
    liked_id = _seed(db, author["id"], published_at=_stamp(seconds=60))
    plain_id = _seed(db, author["id"], published_at=_stamp(seconds=120))
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], liked_id))
    db.commit()

    flags = {p["id"]: p["liked"] for p in client.get(FEED, headers=headers).get_json()["posts"]}

    assert flags == {liked_id: True, plain_id: False}


def test_someone_elses_like_never_flags_the_readers_row(client, actor, make_user, db):
    _, headers = actor
    author = make_user()
    post_id = _seed(db, author["id"])
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (author["id"], post_id))
    db.commit()

    assert client.get(FEED, headers=headers).get_json()["posts"][0]["liked"] is False


def test_a_like_on_a_row_outside_the_page_does_not_leak_onto_it(client, actor, make_user, db):
    user, headers = actor
    author = make_user()
    newest_id = _seed(db, author["id"], published_at=_stamp(seconds=60))
    older_id = _seed(db, author["id"], published_at=_stamp(seconds=600))
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], older_id))
    db.commit()

    page = client.get(FEED, query_string={"per_page": 1}, headers=headers).get_json()["posts"]

    assert [p["id"] for p in page] == [newest_id]
    assert page[0]["liked"] is False


def test_the_liked_flag_survives_a_truncated_body(client, actor, db):
    user, headers = actor
    post_id = _seed(db, user["id"], content="x" * 500)
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], post_id))
    db.commit()

    post = client.get(FEED, headers=headers).get_json()["posts"][0]

    assert post["liked"] is True
    assert post["truncated"] is True


def test_a_guest_gets_a_false_liked_flag_even_on_a_liked_post(client, make_user, db):
    author = make_user()
    post_id = _seed(db, author["id"])
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (author["id"], post_id))
    db.commit()

    assert client.get(FEED).get_json()["posts"][0]["liked"] is False




# -----------------------------------------------------------
# social_feed — the recency window boundary
# -----------------------------------------------------------

def test_a_post_stamped_exactly_at_the_window_date_is_dropped(client, make_user, db):
    # The floor is a strict ">" against a bare date, so a bare
    # date equal to it does not clear it
    author = make_user()
    _seed(db, author["id"], published_at=_cutoff(db))

    assert client.get(FEED).get_json()["posts"] == []


def test_a_post_at_midnight_of_the_window_date_is_ranked(client, make_user, db):
    author = make_user()
    post_id = _seed(db, author["id"], published_at=_cutoff(db) + "T00:00:00+00:00")

    assert _ids(client.get(FEED)) == [post_id]


def test_a_post_in_the_space_form_at_the_window_date_is_ranked(client, make_user, db):
    author = make_user()
    post_id = _seed(db, author["id"], published_at=_cutoff(db) + " 00:00:00")

    assert _ids(client.get(FEED)) == [post_id]


def test_the_last_second_of_the_day_before_the_window_is_dropped(client, make_user, db):
    author = make_user()
    day_before = (datetime.fromisoformat(_cutoff(db)) - timedelta(days=1)).date().isoformat()
    _seed(db, author["id"], published_at=day_before + "T23:59:59+00:00")

    assert client.get(FEED).get_json()["posts"] == []


def test_a_post_one_day_past_the_window_is_dropped(client, make_user, db):
    author = make_user()
    _seed(db, author["id"], published_at=_stamp(days=_FEED_WINDOW_DAYS + 1))

    assert client.get(FEED).get_json()["posts"] == []


def test_the_window_floor_is_paid_by_the_author_too(client, actor, db):
    user, headers = actor
    _seed(db, user["id"], is_public=0, published_at=_stamp(days=_FEED_WINDOW_DAYS + 1))

    assert client.get(FEED, headers=headers).get_json()["posts"] == []


def test_a_faculty_post_by_the_reader_is_still_out_of_the_community_feed(client, actor, db):
    user, headers = actor
    _seed(db, user["id"], source="faculty", post_type="announcement")

    assert client.get(FEED, headers=headers).get_json()["posts"] == []




# -----------------------------------------------------------
# social_feed — the ranking formula and its tie-breakers
# -----------------------------------------------------------

def test_an_unrankable_timestamp_scores_no_recency_at_all(client, make_user, db):
    # julianday() gives NULL, the COALESCE turns the whole
    # recency term into 0 — the row still ships, last
    author = make_user()
    fresh_id = _seed(db, author["id"])
    broken_id = _seed(db, author["id"], published_at="zzzz-neaisku")

    assert _ids(client.get(FEED)) == [fresh_id, broken_id]


def test_two_unrankable_timestamps_fall_back_to_published_at_descending(client, make_user, db):
    # Both score 0, so the second ORDER BY term is the only
    # thing left to separate them
    author = make_user()
    low_id = _seed(db, author["id"], published_at="zzzz-a")
    high_id = _seed(db, author["id"], published_at="zzzz-b")

    assert _ids(client.get(FEED)) == [high_id, low_id]


def test_an_exact_tie_is_broken_by_the_id_descending(client, make_user, db):
    author = make_user()
    stamp = _stamp(days=1)
    ids = [_seed(db, author["id"], published_at=stamp) for _ in range(4)]

    assert _ids(client.get(FEED)) == sorted(ids, reverse=True)


def test_the_engagement_term_is_capped_so_two_wild_posts_tie(client, make_user, db):
    # min(likes + 2*comments + 3*shares, 100): 200 and 5000 both
    # clamp to 100, so only the id can separate them
    author = make_user()
    stamp = _stamp(days=1)
    loud_id = _seed(db, author["id"], published_at=stamp, likes=5000)
    louder_id = _seed(db, author["id"], published_at=stamp, likes=200)

    assert _ids(client.get(FEED)) == sorted([loud_id, louder_id], reverse=True)


def test_a_comment_weighs_twice_a_like(client, make_user, db):
    author = make_user()
    stamp = _stamp(days=1)
    liked_id = _seed(db, author["id"], published_at=stamp, likes=10)
    discussed_id = _seed(db, author["id"], published_at=stamp, comments=10)

    assert _ids(client.get(FEED)) == [discussed_id, liked_id]


def test_a_share_weighs_three_times_a_like(client, make_user, db):
    author = make_user()
    stamp = _stamp(days=1)
    liked_id = _seed(db, author["id"], published_at=stamp, likes=10)
    shared_id = _seed(db, author["id"], published_at=stamp, shares=10)

    assert _ids(client.get(FEED)) == [shared_id, liked_id]


def test_a_future_post_is_clamped_to_the_top_instead_of_scoring_negative(client, make_user, db):
    # Without MAX(0, …) a ten-day-future post would score -11
    # and sink below everything
    author = make_user()
    future_id = _seed(db, author["id"], published_at=_stamp(days=-10))
    old_id = _seed(db, author["id"], published_at=_stamp(days=100))

    assert _ids(client.get(FEED)) == [future_id, old_id]


def test_two_future_posts_share_the_clamped_recency_and_tie_on_engagement(client, make_user, db):
    author = make_user()
    quiet_id = _seed(db, author["id"], published_at=_stamp(days=-5))
    busy_id = _seed(db, author["id"], published_at=_stamp(days=-20), likes=40)

    assert _ids(client.get(FEED)) == [busy_id, quiet_id]




# -----------------------------------------------------------
# social_feed — the ?before pin
# -----------------------------------------------------------

def test_a_post_stamped_exactly_at_the_pin_is_included(client, make_user, db):
    author = make_user()
    stamp = _stamp(days=1)
    post_id = _seed(db, author["id"], published_at=stamp)

    assert _ids(client.get(FEED, query_string={"before": _pin(stamp)})) == [post_id]


def test_a_post_one_second_after_the_pin_is_excluded(client, make_user, db):
    author = make_user()
    pinned = _stamp(days=1)
    _seed(db, author["id"], published_at=_stamp(days=1, seconds=-1))

    assert client.get(FEED, query_string={"before": _pin(pinned)}).get_json()["posts"] == []


def test_a_pin_before_everything_answers_an_empty_page_and_a_zero_total(client, make_user, db):
    author = make_user()
    _seed(db, author["id"])

    body = client.get(FEED, query_string={"before": _pin(_stamp(days=10))}).get_json()

    assert body["posts"] == []
    assert body["total"] == 0
    assert body["hasMore"] is False


def test_a_pin_in_the_future_keeps_every_post(client, make_user, db):
    author = make_user()
    seeded = {_seed(db, author["id"]) for _ in range(3)}

    body = client.get(FEED, query_string={"before": _pin(_stamp(days=-365))}).get_json()

    assert set(p["id"] for p in body["posts"]) == seeded
    assert body["total"] == 3


def test_the_pin_narrows_the_total_as_well_as_the_page(client, make_user, db):
    author = make_user()
    _seed(db, author["id"], published_at=_stamp(days=2))
    _seed(db, author["id"], published_at=_stamp(days=1))
    _seed(db, author["id"])

    body = client.get(FEED, query_string={"before": _pin(_stamp(days=1))}).get_json()

    assert body["total"] == 2


def test_the_pin_moves_the_score_clock_and_not_only_the_filter(client, make_user, db):
    # Live, the fresher post wins on recency. Pinned a thousand
    # days out both recency terms collapse towards zero and the
    # engagement term decides — which only happens if the pin
    # is bound into the score as well as the WHERE
    author = make_user()
    fresh_id = _seed(db, author["id"], published_at=_stamp(seconds=43200))
    engaged_id = _seed(db, author["id"], published_at=_stamp(days=2), likes=20)

    assert _ids(client.get(FEED)) == [fresh_id, engaged_id]
    assert _ids(client.get(FEED, query_string={"before": _pin(_stamp(days=-1000))})) == [engaged_id, fresh_id]


def test_the_pin_and_the_friend_visibility_apply_together(client, actor, make_user, db):
    user, headers = actor
    friend = make_user()
    stranger = make_user()
    _befriend(db, user["id"], friend["id"])
    old_friend_post = _seed(db, friend["id"], is_public=0, published_at=_stamp(days=3))
    _seed(db, friend["id"], is_public=0)
    _seed(db, stranger["id"], is_public=0, published_at=_stamp(days=3))

    body = client.get(FEED, query_string={"before": _pin(_stamp(days=2))}, headers=headers).get_json()

    assert [p["id"] for p in body["posts"]] == [old_friend_post]
    assert body["total"] == 1


def test_the_pin_binds_correctly_for_a_guest(client, make_user, db):
    # No friend ids in front of it, so the pin is the only
    # WHERE parameter there is
    author = make_user()
    old_id = _seed(db, author["id"], published_at=_stamp(days=3))
    _seed(db, author["id"])

    assert _ids(client.get(FEED, query_string={"before": _pin(_stamp(days=1))})) == [old_id]


def test_the_pin_survives_paging(client, make_user, db):
    author = make_user()
    seeded = [_seed(db, author["id"], published_at=_stamp(days=1, seconds=600 * (i + 1))) for i in range(3)]
    pin = _pin(_stamp(days=1))

    first = _ids(client.get(FEED, query_string={"before": pin, "per_page": 2}))
    second = _ids(client.get(FEED, query_string={"before": pin, "per_page": 2, "page": 2}))

    assert first + second == seeded


def test_the_pin_does_not_reopen_the_recency_window(client, make_user, db):
    # A pin far in the past cannot resurrect a post the
    # _FEED_WINDOW_DAYS floor already dropped
    author = make_user()
    _seed(db, author["id"], published_at=_stamp(days=_FEED_WINDOW_DAYS + 30))

    body = client.get(FEED, query_string={"before": _pin(_stamp(days=_FEED_WINDOW_DAYS + 10))}).get_json()

    assert body["posts"] == []
    assert body["total"] == 0




# -----------------------------------------------------------
# social_feed — the body on the wire
# -----------------------------------------------------------

def test_the_feed_always_trims_bodies(client, make_user, db):
    author = make_user()
    _seed(db, author["id"], content="x" * (SUMMARY_LENGTH + 50))

    post = client.get(FEED).get_json()["posts"][0]

    assert post["truncated"] is True
    assert len(post["content"]) == SUMMARY_LENGTH


def test_a_body_is_trimmed_before_it_is_escaped(client, make_user, db):
    # 201 raw "<" trim to 200, and only then become entities —
    # escaping first would leave 50 characters of body
    author = make_user()
    _seed(db, author["id"], content="<" * (SUMMARY_LENGTH + 1))

    post = client.get(FEED).get_json()["posts"][0]

    assert post["content"] == "&lt;" * SUMMARY_LENGTH
    assert post["truncated"] is True


def test_a_feed_body_is_stored_raw_and_escaped_on_output(client, make_user, db):
    author = make_user()
    _seed(db, author["id"], content="<b>Sveiki</b> & Co")

    assert client.get(FEED).get_json()["posts"][0]["content"] == "&lt;b&gt;Sveiki&lt;/b&gt; &amp; Co"
    assert db.execute("SELECT content FROM news_posts").fetchone()["content"] == "<b>Sveiki</b> & Co"


def test_the_feed_answers_the_full_wire_shape_per_post(client, make_user, db):
    author = make_user()
    _seed(db, author["id"])

    assert set(client.get(FEED).get_json()["posts"][0]) == WIRE_KEYS


def test_the_feed_carries_the_current_display_name_and_avatar(client, make_user, db):
    author = make_user(display_name="Ona Onaite")
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?", ("/api/uploads/o.jpg", author["id"]))
    db.commit()
    _seed(db, author["id"], author_name="Senas Vardas")

    post = client.get(FEED).get_json()["posts"][0]

    assert post["author"] == "Ona Onaite"
    assert post["authorAvatar"] == "/api/uploads/o.jpg"


def test_an_authorless_post_keeps_its_snapshot_in_the_feed(client, db):
    post_id = _seed(db, None, author_name="Redakcija")

    post = client.get(FEED).get_json()["posts"][0]

    assert post["id"] == post_id
    assert post["author"] == "Redakcija"
    assert post["authorId"] is None
