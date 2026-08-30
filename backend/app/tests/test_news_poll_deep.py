# -----------------------------------------------------------
#  [*] Tests — the poll slice of app/news/routes.py, branch by
#      branch
#
#  The exhaustive companion to test_news_polls.py. Nine
#  functions are in scope and every arm of each one is driven
#  from this file alone:
#
#    _parse_iso        every shape it repairs (legacy space
#                      separator, a '+' that arrived as a
#                      space, both at once), every shape it
#                      refuses (non-strings, blanks, garbage),
#                      and the zoneless-means-UTC rule.
#    _to_utc_iso       normalise / hand back untouched / None,
#                      and the OverflowError an edge-of-the-
#                      calendar offset still produces.
#    _poll_shape       the exact wire keys, an empty option
#                      list, the endDate normalisation and the
#                      createdAt that is deliberately NOT
#                      normalised.
#    _polls_for_posts  the two early returns, the option
#                      grouping, the guest/member vote arms and
#                      the poll_id fence around the vote query.
#    _poll_to_dict     the same three vote arms on the single-
#                      poll path, plus option ordering.
#    get_poll          all three routes to the identical 404,
#                      every _can_view_post arm (public,
#                      author, admin, staff, friend, stranger)
#                      and the tally a guest may read.
#    create_poll       every 400/403/404/409 in the order the
#                      route checks them — body BEFORE post,
#                      visibility BEFORE ownership, ownership
#                      BEFORE the scrape rule, the scrape rule
#                      BEFORE the one-poll rule — plus the lost
#                      race the UNIQUE index turns into a 409.
#    delete_poll       every restored post_type, the ownership
#                      check that runs before the "no poll"
#                      check, and the second call's 404.
#    vote_poll         the body guards, the three end_date arms
#                      (absent / blank / unparseable / passed),
#                      the foreign option, the 409 on a repeat
#                      and the move that never grows the total.
#
#  Where a body's exact bytes matter the payload is posted as
#  raw JSON (TESTPLAN rule 10) — the test client would
#  otherwise serialise it through the app's own html-escaping
#  provider and put a string on the wire no real client sends.
# -----------------------------------------------------------

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine

from app.news import routes as news


POLL_FIELDS = {"id", "postId", "title", "endDate", "totalVotes", "createdAt", "userVote", "options"}
OPTION_FIELDS = {"id", "text", "votes"}

# The one 404 body every poll read answers, whatever the real
# reason — "no poll" is the only thing a caller may learn
NO_POLL = {"error": "No poll found for this post"}




# -----------------------------------------------------------
# _now / _ahead
# -----------------------------------------------------------
#
# Whole-second instants around the REAL clock. Absolute dates
# would not do: the sessions the fixtures mint live 30 days, so
# a test that travelled months would only prove the bearer
# token expired.
# -----------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ahead(hours=1):
    return _now() + timedelta(hours=hours)




# -----------------------------------------------------------
# _new_post
# -----------------------------------------------------------
#
# One post through the real POST /api/news, so source, author
# and post_type are exactly what production writes (source
# 'user' for a student, 'faculty' for staff).
# -----------------------------------------------------------

def _new_post(client, headers, **body):
    payload = {"content": "Apklausos irasas"}
    payload.update(body)
    response = client.post("/api/news", headers=headers, json=payload)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["id"]




# -----------------------------------------------------------
# _plant_post
# -----------------------------------------------------------
#
# A news_posts row the routes refuse to create: a scraped
# article (author_id NULL), a source 'app' row, a post whose
# post_type already claims 'poll' with nothing behind it.
# -----------------------------------------------------------

def _plant_post(db, author_id=None, source="user", is_public=1, post_type="social", source_url=None):
    post_id = str(uuid.uuid4())
    stamp = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO news_posts
           (id, title, content, summary, author_id, author_name, source, source_url,
            post_type, is_public, published_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, "Įrašas", "Turinys", "Turinys", author_id, "Autorius",
         source, source_url, post_type, is_public, stamp, stamp, stamp),
    )
    db.commit()
    return post_id




# -----------------------------------------------------------
# _plant_poll / _plant_vote / _poll_row
# -----------------------------------------------------------
#
# The poll rows the helper tests need without going through
# create_poll: an optionless poll, a legacy end_date shape, a
# vote by somebody the route would never let vote. Option ids
# come back in the order they were inserted, which is the rowid
# order _poll_shape's callers rely on.
# -----------------------------------------------------------

def _plant_poll(db, post_id, title="Klausimas", end_date=None, options=("Taip", "Ne"),
                total_votes=0, created_at=None):
    poll_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO polls (id, post_id, title, end_date, total_votes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (poll_id, post_id, title, end_date, total_votes,
         created_at or datetime.now(timezone.utc).isoformat()),
    )
    option_ids = []
    for text in options:
        option_id = str(uuid.uuid4())
        db.execute("INSERT INTO poll_options (id, poll_id, text, votes) VALUES (?, ?, ?, 0)",
                   (option_id, poll_id, text))
        option_ids.append(option_id)
    db.commit()
    return poll_id, option_ids


def _plant_vote(db, user_id, poll_id, option_id):
    db.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id, created_at) VALUES (?, ?, ?, ?)",
        (user_id, poll_id, option_id, datetime.now(timezone.utc).isoformat()),
    )
    db.commit()


def _poll_row(db, poll_id):
    return db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()




# -----------------------------------------------------------
# _attach / _read / _vote / _detach
# -----------------------------------------------------------
#
# The four poll calls with the defaults every arrangement would
# otherwise repeat. Each returns the raw response so an unhappy
# path can assert its status AND its body.
# -----------------------------------------------------------

def _attach(client, headers, post_id, title="Kada rinktis?", options=None, **extra):
    payload = {"title": title, "options": ["Pirmadieni", "Antradieni"] if options is None else options}
    payload.update(extra)
    return client.post(f"/api/news/{post_id}/poll", headers=headers, json=payload)


def _read(client, post_id, headers=None):
    return client.get(f"/api/news/{post_id}/poll", headers=headers or {})


def _vote(client, headers, post_id, option_id):
    return client.post(f"/api/news/{post_id}/poll/vote", headers=headers, json={"option_id": option_id})


def _detach(client, headers, post_id):
    return client.delete(f"/api/news/{post_id}/poll", headers=headers)




# -----------------------------------------------------------
# _post_raw
# -----------------------------------------------------------
#
# TESTPLAN rule 10: `json=` is serialised through the app's own
# html-escaping provider, so a test that cares what is ON THE
# WIRE hands over bytes it built itself.
# -----------------------------------------------------------

def _post_raw(client, path, payload, headers=None):
    merged = {"Content-Type": "application/json", **(headers or {})}
    return client.post(path, data=json.dumps(payload), headers=merged)




# -----------------------------------------------------------
# _poll_on
# -----------------------------------------------------------
#
# The common arrangement: a post owned by `headers`, a poll on
# it, and the option ids in creation order.
# -----------------------------------------------------------

def _poll_on(client, headers, options=("Taip", "Ne"), **extra):
    post_id = _new_post(client, headers)
    response = _attach(client, headers, post_id, options=list(options), **extra)
    assert response.status_code == 201, response.get_json()
    poll = response.get_json()
    return post_id, poll, [o["id"] for o in poll["options"]]




# -----------------------------------------------------------
# _befriend
# -----------------------------------------------------------
#
# friendships is written in BOTH directions on accept, which is
# what _can_view_post's single-direction lookup relies on.
# -----------------------------------------------------------

def _befriend(db, one, other):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (one, other))
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (other, one))
    db.commit()




# -----------------------------------------------------------
# _parse_iso — what it refuses
# -----------------------------------------------------------

@pytest.mark.parametrize("value", [None, 0, 1, 1.5, True, False, [], {}, (), b"2026-08-29",
                                   ["2026-08-29"], {"date": "2026-08-29"}])
def test_parse_iso_answers_none_for_anything_that_is_not_a_string(value):
    assert news._parse_iso(value) is None


def test_parse_iso_answers_none_for_a_real_datetime_object():
    assert news._parse_iso(datetime.now(timezone.utc)) is None


@pytest.mark.parametrize("value", ["", " ", "   ", "\t", "\n", " \t\n "])
def test_parse_iso_answers_none_for_a_blank_string(value):
    assert news._parse_iso(value) is None


@pytest.mark.parametrize("value", ["kada nors", "2026-13-01T00:00:00", "2026-08-32T00:00:00",
                                   "2026-08-29T25:00:00", "29/08/2026", "2026", "-", "T"])
def test_parse_iso_answers_none_for_a_string_it_cannot_read(value):
    assert news._parse_iso(value) is None


def test_parse_iso_answers_none_when_the_separator_repair_leaves_garbage():
    # index 10 IS a space, so the repair fires and still yields
    # something fromisoformat refuses
    assert news._parse_iso("2026-08-29 nesamone") is None


def test_parse_iso_answers_none_when_the_lost_plus_repair_leaves_garbage():
    assert news._parse_iso("2026-08-29T12:00:00 rytoj") is None




# -----------------------------------------------------------
# _parse_iso — what it accepts, and what it repairs first
# -----------------------------------------------------------

def test_parse_iso_reads_a_zoneless_stamp_as_utc():
    assert news._parse_iso("2026-08-29T12:00:00") == datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def test_parse_iso_keeps_an_offset_it_was_given():
    parsed = news._parse_iso("2026-08-29T12:00:00+03:00")

    assert parsed.utcoffset() == timedelta(hours=3)
    assert parsed == datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


def test_parse_iso_keeps_a_negative_offset_too():
    assert news._parse_iso("2026-08-29T12:00:00-05:30") == datetime(2026, 8, 29, 17, 30, tzinfo=timezone.utc)


def test_parse_iso_repairs_the_legacy_space_separator():
    assert news._parse_iso("2026-08-29 12:00:00") == news._parse_iso("2026-08-29T12:00:00")


def test_parse_iso_repairs_a_plus_that_arrived_as_a_space():
    # what a query string does to "+00:00"
    assert news._parse_iso("2026-08-29T12:00:00 03:00") == datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


def test_parse_iso_repairs_the_separator_and_the_lost_plus_in_one_value():
    assert news._parse_iso("2026-08-29 12:00:00 03:00") == datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)


def test_parse_iso_accepts_a_date_with_no_time_at_all():
    # exactly ten characters, so the len > 10 separator guard
    # never fires
    assert news._parse_iso("2026-08-29") == datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_keeps_microseconds():
    assert news._parse_iso("2026-08-29T12:00:00.123456").microsecond == 123456


def test_parse_iso_reads_a_trailing_z_as_utc():
    assert news._parse_iso("2026-08-29T12:00:00Z") == datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def test_parse_iso_strips_the_whitespace_around_a_value():
    assert news._parse_iso("\t 2026-08-29T12:00:00 \n") == datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def test_parse_iso_accepts_the_first_instant_the_calendar_holds():
    assert news._parse_iso("0001-01-01T00:00:00+00:00").year == 1


def test_parse_iso_accepts_the_last_instant_the_calendar_holds():
    assert news._parse_iso("9999-12-31T23:59:59+00:00").year == 9999




# -----------------------------------------------------------
# _to_utc_iso
# -----------------------------------------------------------

def test_to_utc_iso_normalises_an_offset_stamp_to_utc():
    assert news._to_utc_iso("2026-08-29T12:00:00+03:00") == "2026-08-29T09:00:00+00:00"


def test_to_utc_iso_stamps_a_zoneless_value_as_utc():
    assert news._to_utc_iso("2026-08-29T12:00:00") == "2026-08-29T12:00:00+00:00"


def test_to_utc_iso_turns_the_legacy_space_form_into_t_form():
    assert news._to_utc_iso("2026-08-29 12:00:00") == "2026-08-29T12:00:00+00:00"


def test_to_utc_iso_is_idempotent():
    once = news._to_utc_iso("2026-08-29 12:00:00")

    assert news._to_utc_iso(once) == once


def test_to_utc_iso_hands_an_unparseable_string_back_untouched():
    # a null endDate would read as "never closes" on the client,
    # so the original string survives instead
    assert news._to_utc_iso("per Jonines") == "per Jonines"


def test_to_utc_iso_keeps_the_whitespace_of_the_string_it_hands_back():
    assert news._to_utc_iso("  per Jonines  ") == "  per Jonines  "


@pytest.mark.parametrize("value", [None, "", " ", "\t\n", 0, 42, 1.5, True, False, [], {}, ["x"]])
def test_to_utc_iso_answers_none_for_anything_that_is_not_a_usable_string(value):
    assert news._to_utc_iso(value) is None


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
def test_to_utc_iso_survives_a_stamp_utc_cannot_hold(value):
    assert isinstance(news._to_utc_iso(value), str)




# -----------------------------------------------------------
# _poll_shape — the ONE producer of the wire shape
# -----------------------------------------------------------

def _shape_row(**overrides):
    row = {"id": "poll-1", "post_id": "post-1", "title": "Klausimas",
           "end_date": None, "total_votes": 0, "created_at": "2026-08-29T12:00:00+00:00"}
    row.update(overrides)
    return row


def test_the_poll_shape_names_exactly_the_fields_the_mobile_client_reads():
    assert set(news._poll_shape(_shape_row(), [], None)) == POLL_FIELDS


def test_the_poll_shape_carries_an_empty_option_list_when_it_is_given_none():
    assert news._poll_shape(_shape_row(), [], None)["options"] == []


def test_the_poll_shape_names_exactly_three_fields_per_option():
    shape = news._poll_shape(_shape_row(), [{"id": "o1", "text": "Taip", "votes": 3}], None)

    assert set(shape["options"][0]) == OPTION_FIELDS
    assert shape["options"][0] == {"id": "o1", "text": "Taip", "votes": 3}


def test_the_poll_shape_keeps_the_option_rows_in_the_order_it_was_handed():
    rows = [{"id": "z", "text": "Zebras", "votes": 0},
            {"id": "a", "text": "Antis", "votes": 0}]

    assert [o["id"] for o in news._poll_shape(_shape_row(), rows, None)["options"]] == ["z", "a"]


def test_the_poll_shape_normalises_the_end_date_to_explicit_utc():
    shape = news._poll_shape(_shape_row(end_date="2026-08-29 12:00:00"), [], None)

    assert shape["endDate"] == "2026-08-29T12:00:00+00:00"


def test_the_poll_shape_leaves_a_null_end_date_null():
    assert news._poll_shape(_shape_row(end_date=None), [], None)["endDate"] is None


def test_the_poll_shape_turns_a_blank_end_date_into_null():
    assert news._poll_shape(_shape_row(end_date="   "), [], None)["endDate"] is None


def test_the_poll_shape_hands_an_unparseable_end_date_through_untouched():
    assert news._poll_shape(_shape_row(end_date="per Jonines"), [], None)["endDate"] == "per Jonines"


def test_the_poll_shape_passes_created_at_through_without_normalising_it():
    # endDate is normalised, createdAt deliberately is not — only
    # the end date drives a client-side countdown
    shape = news._poll_shape(_shape_row(created_at="2026-08-29 12:00:00"), [], None)

    assert shape["createdAt"] == "2026-08-29 12:00:00"


def test_the_poll_shape_echoes_the_user_vote_it_is_handed():
    assert news._poll_shape(_shape_row(), [], "option-7")["userVote"] == "option-7"


def test_the_poll_shape_leaves_user_vote_none_when_there_is_none():
    assert news._poll_shape(_shape_row(), [], None)["userVote"] is None


def test_the_poll_shape_passes_the_total_through_verbatim():
    assert news._poll_shape(_shape_row(total_votes=41), [], None)["totalVotes"] == 41




# -----------------------------------------------------------
# _polls_for_posts — the batched page path
# -----------------------------------------------------------

@pytest.mark.parametrize("empty", [[], (), set()])
def test_the_poll_batch_answers_nothing_for_an_empty_id_collection(db, empty):
    assert news._polls_for_posts(db, empty) == {}


def test_the_poll_batch_answers_nothing_when_no_post_carries_a_poll(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])

    assert news._polls_for_posts(db, [post_id]) == {}


def test_the_poll_batch_answers_nothing_for_ids_that_exist_nowhere(db):
    assert news._polls_for_posts(db, ["nope-1", "nope-2"]) == {}


def test_the_poll_batch_keys_each_poll_by_its_post(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, _ = _plant_poll(db, post_id)

    batch = news._polls_for_posts(db, [post_id])

    assert list(batch) == [post_id]
    assert batch[post_id]["id"] == poll_id
    assert batch[post_id]["postId"] == post_id


def test_the_poll_batch_serves_two_posts_from_one_call(db, actor):
    user, _ = actor
    first = _plant_post(db, author_id=user["id"])
    second = _plant_post(db, author_id=user["id"])
    _plant_poll(db, first, title="Pirma")
    _plant_poll(db, second, title="Antra")

    batch = news._polls_for_posts(db, [first, second])

    assert {batch[first]["title"], batch[second]["title"]} == {"Pirma", "Antra"}


def test_the_poll_batch_skips_a_post_of_the_page_that_has_no_poll(db, actor):
    user, _ = actor
    with_poll = _plant_post(db, author_id=user["id"])
    without = _plant_post(db, author_id=user["id"])
    _plant_poll(db, with_poll)

    assert set(news._polls_for_posts(db, [with_poll, without])) == {with_poll}


def test_the_poll_batch_carries_an_empty_option_list_for_an_optionless_poll(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    _plant_poll(db, post_id, options=())

    assert news._polls_for_posts(db, [post_id])[post_id]["options"] == []


def test_the_poll_batch_orders_options_by_insertion_not_by_text(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    _plant_poll(db, post_id, options=("Zebras", "Antis", "Meska"))

    texts = [o["text"] for o in news._polls_for_posts(db, [post_id])[post_id]["options"]]

    assert texts == ["Zebras", "Antis", "Meska"]


def test_the_poll_batch_leaves_user_vote_none_for_a_guest(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id)
    _plant_vote(db, user["id"], poll_id, option_ids[0])

    assert news._polls_for_posts(db, [post_id])[post_id]["userVote"] is None


def test_the_poll_batch_leaves_user_vote_none_for_a_member_who_never_voted(db, actor, make_user):
    user, _ = actor
    other = make_user()
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id)
    _plant_vote(db, user["id"], poll_id, option_ids[1])

    assert news._polls_for_posts(db, [post_id], other["id"])[post_id]["userVote"] is None


def test_the_poll_batch_names_the_option_the_caller_holds(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id)
    _plant_vote(db, user["id"], poll_id, option_ids[1])

    assert news._polls_for_posts(db, [post_id], user["id"])[post_id]["userVote"] == option_ids[1]


def test_the_poll_batch_never_leaks_another_members_vote(db, actor, make_user):
    user, _ = actor
    other = make_user()
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id)
    _plant_vote(db, user["id"], poll_id, option_ids[0])
    _plant_vote(db, other["id"], poll_id, option_ids[1])

    assert news._polls_for_posts(db, [post_id], other["id"])[post_id]["userVote"] == option_ids[1]


def test_the_poll_batch_ignores_a_vote_on_a_poll_outside_the_page(db, actor):
    user, _ = actor
    inside = _plant_post(db, author_id=user["id"])
    outside = _plant_post(db, author_id=user["id"])
    _plant_poll(db, inside)
    other_poll, other_options = _plant_poll(db, outside)
    _plant_vote(db, user["id"], other_poll, other_options[0])

    batch = news._polls_for_posts(db, [inside], user["id"])

    assert batch[inside]["userVote"] is None


def test_the_poll_batch_serves_the_stored_tally_verbatim(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id, total_votes=9)
    db.execute("UPDATE poll_options SET votes = 4 WHERE id = ?", (option_ids[0],))
    db.commit()

    batch = news._polls_for_posts(db, [post_id])[post_id]

    assert batch["totalVotes"] == 9
    assert batch["options"][0]["votes"] == 4




# -----------------------------------------------------------
# _poll_to_dict — the single-poll path
# -----------------------------------------------------------

def test_one_poll_to_dict_answers_the_full_wire_shape(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, _ = _plant_poll(db, post_id)

    assert set(news._poll_to_dict(db, _poll_row(db, poll_id))) == POLL_FIELDS


def test_one_poll_to_dict_leaves_user_vote_none_without_a_user(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id)
    _plant_vote(db, user["id"], poll_id, option_ids[0])

    assert news._poll_to_dict(db, _poll_row(db, poll_id))["userVote"] is None


def test_one_poll_to_dict_leaves_user_vote_none_when_that_user_never_voted(db, actor, make_user):
    user, _ = actor
    other = make_user()
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id)
    _plant_vote(db, user["id"], poll_id, option_ids[0])

    assert news._poll_to_dict(db, _poll_row(db, poll_id), other["id"])["userVote"] is None


def test_one_poll_to_dict_names_the_option_that_user_holds(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id, options=("A", "B", "C"))
    _plant_vote(db, user["id"], poll_id, option_ids[2])

    assert news._poll_to_dict(db, _poll_row(db, poll_id), user["id"])["userVote"] == option_ids[2]


def test_one_poll_to_dict_answers_an_empty_option_list_for_an_optionless_poll(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, _ = _plant_poll(db, post_id, options=())

    assert news._poll_to_dict(db, _poll_row(db, poll_id), user["id"])["options"] == []


def test_one_poll_to_dict_orders_options_by_insertion_not_by_text(db, actor):
    user, _ = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, _ = _plant_poll(db, post_id, options=("Zebras", "Antis"))

    texts = [o["text"] for o in news._poll_to_dict(db, _poll_row(db, poll_id))["options"]]

    assert texts == ["Zebras", "Antis"]


def test_one_poll_to_dict_reads_a_vote_only_from_its_own_poll(db, actor):
    user, _ = actor
    first = _plant_post(db, author_id=user["id"])
    second = _plant_post(db, author_id=user["id"])
    quiet_poll, _ = _plant_poll(db, first)
    loud_poll, loud_options = _plant_poll(db, second)
    _plant_vote(db, user["id"], loud_poll, loud_options[0])

    assert news._poll_to_dict(db, _poll_row(db, quiet_poll), user["id"])["userVote"] is None




# -----------------------------------------------------------
# GET /api/news/<post_id>/poll — the identical 404
# -----------------------------------------------------------

def test_all_three_reasons_for_no_poll_answer_one_identical_body(client, db, actor, make_user, auth_headers):
    user, headers = actor
    stranger = make_user()
    stranger_headers = auth_headers(stranger)

    without_poll = _new_post(client, headers)
    hidden = _new_post(client, headers, is_public=False)
    _attach(client, headers, hidden)

    missing = _read(client, str(uuid.uuid4()), stranger_headers)
    empty = _read(client, without_poll, stranger_headers)
    forbidden = _read(client, hidden, stranger_headers)

    assert [missing.status_code, empty.status_code, forbidden.status_code] == [404, 404, 404]
    assert missing.get_json() == empty.get_json() == forbidden.get_json() == NO_POLL


def test_a_post_id_that_is_only_whitespace_is_a_404(client):
    assert _read(client, "%20%20").status_code == 404




# -----------------------------------------------------------
# GET /api/news/<post_id>/poll — every visibility arm
# -----------------------------------------------------------

def test_a_public_wall_posts_poll_is_open_to_a_guest(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)

    response = _read(client, post_id)

    assert response.status_code == 200
    assert response.get_json()["id"] == poll["id"]
    assert response.get_json()["userVote"] is None


def test_a_guest_reads_the_running_tally_of_other_peoples_votes(client, actor, make_user, auth_headers):
    user, headers = actor
    voter_headers = auth_headers(make_user())
    post_id, poll, option_ids = _poll_on(client, headers)
    assert _vote(client, voter_headers, post_id, option_ids[0]).status_code == 200

    body = _read(client, post_id).get_json()

    assert body["totalVotes"] == 1
    assert body["options"][0]["votes"] == 1
    assert body["userVote"] is None


def test_a_private_posts_poll_is_hidden_from_a_guest(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers, is_public=False)
    _attach(client, headers, post_id)

    response = _read(client, post_id)

    assert response.status_code == 404
    assert response.get_json() == NO_POLL


def test_a_private_faculty_posts_poll_reaches_another_teacher(client, make_user, auth_headers):
    author_headers = auth_headers(make_user(role="teacher"))
    reader_headers = auth_headers(make_user(role="teacher"))
    post_id = _new_post(client, author_headers, is_public=False)
    _attach(client, author_headers, post_id)

    assert _read(client, post_id, reader_headers).status_code == 200


def test_a_private_faculty_posts_poll_reaches_a_curator(client, make_user, auth_headers):
    author_headers = auth_headers(make_user(role="teacher"))
    curator_headers = auth_headers(make_user(role="curator"))
    post_id = _new_post(client, author_headers, is_public=False)
    _attach(client, author_headers, post_id)

    assert _read(client, post_id, curator_headers).status_code == 200


def test_a_private_faculty_posts_poll_is_hidden_from_a_student(client, actor, make_user, auth_headers):
    student, student_headers = actor
    author_headers = auth_headers(make_user(role="teacher"))
    post_id = _new_post(client, author_headers, is_public=False)
    _attach(client, author_headers, post_id)

    response = _read(client, post_id, student_headers)

    assert response.status_code == 404
    assert response.get_json() == NO_POLL


def test_a_private_wall_posts_poll_is_hidden_from_a_teacher_who_is_no_friend(
        client, actor, make_user, auth_headers):
    user, headers = actor
    teacher_headers = auth_headers(make_user(role="teacher"))
    post_id = _new_post(client, headers, is_public=False)
    _attach(client, headers, post_id)

    assert _read(client, post_id, teacher_headers).status_code == 404


def test_a_one_directional_friendship_row_is_enough_to_open_a_private_wall_poll(
        client, db, actor, make_user, auth_headers):
    user, headers = actor
    friend = make_user()
    friend_headers = auth_headers(friend)
    post_id = _new_post(client, headers, is_public=False)
    _attach(client, headers, post_id)

    # only the reader → author direction, which is the one
    # _can_view_post looks up
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (friend["id"], user["id"]))
    db.commit()

    assert _read(client, post_id, friend_headers).status_code == 200


def test_the_wrong_friendship_direction_alone_does_not_open_a_private_wall_poll(
        client, db, actor, make_user, auth_headers):
    user, headers = actor
    friend = make_user()
    friend_headers = auth_headers(friend)
    post_id = _new_post(client, headers, is_public=False)
    _attach(client, headers, post_id)

    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user["id"], friend["id"]))
    db.commit()

    assert _read(client, post_id, friend_headers).status_code == 404




# -----------------------------------------------------------
# GET /api/news/<post_id>/poll — what it serves
# -----------------------------------------------------------

def test_a_poll_hung_on_a_post_that_never_became_a_poll_card_is_still_served(client, db, actor):
    user, headers = actor
    post_id = _plant_post(db, author_id=user["id"], post_type="social")
    poll_id, _ = _plant_poll(db, post_id)

    response = _read(client, post_id, headers)

    assert response.status_code == 200
    assert response.get_json()["id"] == poll_id


def test_an_optionless_poll_still_answers_two_hundred(client, db, actor):
    user, headers = actor
    post_id = _plant_post(db, author_id=user["id"])
    _plant_poll(db, post_id, options=())

    response = _read(client, post_id, headers)

    assert response.status_code == 200
    assert response.get_json()["options"] == []


def test_an_ended_poll_is_still_readable(client, actor):
    user, headers = actor
    past = (_now() - timedelta(days=1)).isoformat()
    post_id, poll, _ = _poll_on(client, headers, end_date=past)

    response = _read(client, post_id, headers)

    assert response.status_code == 200
    assert response.get_json()["endDate"] == past


def test_the_stored_legacy_end_date_reaches_the_client_as_explicit_utc(client, db, actor):
    user, headers = actor
    post_id = _plant_post(db, author_id=user["id"])
    _plant_poll(db, post_id, end_date="2026-08-29 12:00:00")

    assert _read(client, post_id, headers).get_json()["endDate"] == "2026-08-29T12:00:00+00:00"


def test_a_stored_edge_of_calendar_end_date_still_serves_the_poll(client, db, actor):
    user, headers = actor
    post_id = _plant_post(db, author_id=user["id"])
    _plant_poll(db, post_id, end_date="9999-12-31T23:59:59-14:00")

    assert _read(client, post_id, headers).status_code == 200




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — the body, before anything else
# -----------------------------------------------------------

def test_an_empty_object_body_is_refused_as_a_missing_body(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = client.post(f"/api/news/{post_id}/poll", headers=headers, json={})

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object body required"}


def test_a_body_of_json_null_is_refused(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _post_raw(client, f"/api/news/{post_id}/poll", None, headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object body required"}


@pytest.mark.parametrize("bad", [0, 1, 1.5, True, False, [], ["a"], {}, {"lt": "Klausimas"}])
def test_a_title_that_is_not_a_string_is_refused(client, actor, bad):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, title=bad)

    assert response.status_code == 400
    assert response.get_json() == {"error": "title must be a string"}


@pytest.mark.parametrize("blank", ["", " ", "\t\n", "     "])
def test_a_title_that_is_only_whitespace_is_refused(client, actor, blank):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, title=blank)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll title required"}


def test_an_explicit_null_title_is_refused_as_missing(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _post_raw(client, f"/api/news/{post_id}/poll",
                         {"title": None, "options": ["A", "B"]}, headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll title required"}


def test_a_title_padded_past_the_cap_is_measured_after_stripping(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, title="   " + "a" * news.MAX_TITLE_LENGTH + "   ")

    assert response.status_code == 201
    assert response.get_json()["title"] == "a" * news.MAX_TITLE_LENGTH


def test_a_title_one_character_over_the_cap_after_stripping_is_refused(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, title="  " + "a" * (news.MAX_TITLE_LENGTH + 1) + "  ")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": f"Poll title must be at most {news.MAX_TITLE_LENGTH} characters"}




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — the options
# -----------------------------------------------------------

@pytest.mark.parametrize("bad", ["Taip, Ne", 2, 2.5, True, {"a": "Taip", "b": "Ne"}])
def test_options_that_are_not_a_list_are_refused(client, actor, bad):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, options=bad)

    assert response.status_code == 400
    assert response.get_json() == {"error": "options must be an array of strings"}


def test_an_explicitly_null_options_value_is_refused_as_a_non_list(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    # NOT the same as leaving the key out: an absent key defaults
    # to [] and is refused for being too FEW
    response = _post_raw(client, f"/api/news/{post_id}/poll",
                         {"title": "Klausimas", "options": None}, headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "options must be an array of strings"}


@pytest.mark.parametrize("bad", [["Taip", 2], ["Taip", None], ["Taip", True], ["Taip", ["Ne"]],
                                 ["Taip", {"text": "Ne"}], [1, 2]])
def test_an_options_list_holding_a_non_string_is_refused(client, actor, bad):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, options=bad)

    assert response.status_code == 400
    assert response.get_json() == {"error": "options must be an array of strings"}


@pytest.mark.parametrize("thin", [[], ["Taip"], ["", "  "], ["Taip", "   "]])
def test_fewer_than_two_usable_options_are_refused(client, actor, thin):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, options=thin)

    assert response.status_code == 400
    assert response.get_json() == {"error": f"At least {news.MIN_POLL_OPTIONS} options required"}


def test_an_absent_options_key_is_refused_as_too_few(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = client.post(f"/api/news/{post_id}/poll", headers=headers, json={"title": "Klausimas"})

    assert response.status_code == 400
    assert response.get_json() == {"error": f"At least {news.MIN_POLL_OPTIONS} options required"}


def test_blanks_are_stripped_before_the_count_so_eleven_can_still_be_ten(client, db, actor):
    user, headers = actor
    post_id = _new_post(client, headers)
    options = [f"O{i}" for i in range(news.MAX_POLL_OPTIONS)] + ["   "]

    response = _attach(client, headers, post_id, options=options)

    assert response.status_code == 201
    assert len(response.get_json()["options"]) == news.MAX_POLL_OPTIONS
    assert db.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == news.MAX_POLL_OPTIONS


def test_twelve_options_of_which_one_is_blank_are_still_refused(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)
    options = [f"O{i}" for i in range(news.MAX_POLL_OPTIONS + 1)] + [""]

    response = _attach(client, headers, post_id, options=options)

    assert response.status_code == 400
    assert response.get_json() == {"error": f"Maximum {news.MAX_POLL_OPTIONS} options allowed"}


def test_an_option_padded_past_the_cap_is_measured_after_stripping(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)
    padded = "  " + "b" * news.MAX_POLL_OPTION_LENGTH + "  "

    response = _attach(client, headers, post_id, options=[padded, "Ne"])

    assert response.status_code == 201
    assert response.get_json()["options"][0]["text"] == "b" * news.MAX_POLL_OPTION_LENGTH


def test_the_option_cap_is_measured_per_option_not_over_the_whole_list(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)
    long_but_legal = ["c" * news.MAX_POLL_OPTION_LENGTH] * news.MIN_POLL_OPTIONS

    assert _attach(client, headers, post_id, options=long_but_legal).status_code == 201


def test_one_over_long_option_among_valid_ones_refuses_the_whole_poll(client, db, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id,
                       options=["Taip", "d" * (news.MAX_POLL_OPTION_LENGTH + 1), "Ne"])

    assert response.status_code == 400
    assert response.get_json() == {
        "error": f"Each option must be at most {news.MAX_POLL_OPTION_LENGTH} characters"}
    assert db.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 0


def test_duplicate_option_texts_are_stored_as_two_separate_options(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, options=["Taip", "Taip"])
    body = response.get_json()

    assert response.status_code == 201
    assert [o["text"] for o in body["options"]] == ["Taip", "Taip"]
    assert body["options"][0]["id"] != body["options"][1]["id"]


def test_a_nul_byte_in_an_option_is_stripped_before_the_poll_is_stored(client, db, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _post_raw(client, f"/api/news/{post_id}/poll",
                         {"title": "Klausimas", "options": ["Ta" + chr(0) + "ip", "Ne"]}, headers)

    assert response.status_code == 201
    stored = db.execute("SELECT text FROM poll_options ORDER BY rowid").fetchone()[0]
    assert stored == "Taip"




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — the end date
# -----------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "kada nors", "2026-13-01T00:00:00", 0, 1, 1.5,
                                 True, False, [], {}, ["2026-08-29"]])
def test_an_end_date_that_is_not_a_parseable_string_is_refused(client, actor, bad):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, end_date=bad)

    assert response.status_code == 400
    assert response.get_json() == {"error": "end_date must be an ISO-8601 timestamp"}


def test_a_date_only_end_date_is_stored_as_midnight_utc(client, db, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, end_date="2026-12-31")

    assert response.status_code == 201
    assert response.get_json()["endDate"] == "2026-12-31T00:00:00+00:00"
    assert db.execute("SELECT end_date FROM polls").fetchone()[0] == "2026-12-31T00:00:00+00:00"


def test_a_legacy_space_form_end_date_is_normalised_on_the_way_in(client, db, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, end_date="2026-12-31 18:30:00")

    assert response.status_code == 201
    assert db.execute("SELECT end_date FROM polls").fetchone()[0] == "2026-12-31T18:30:00+00:00"


def test_the_microseconds_of_an_end_date_survive_the_round_trip(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _attach(client, headers, post_id, end_date="2026-12-31T18:30:00.123456+00:00")

    assert response.get_json()["endDate"] == "2026-12-31T18:30:00.123456+00:00"


def test_an_edge_of_calendar_end_date_is_answered_not_crashed(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    assert _attach(client, headers, post_id, end_date="0001-01-01T00:00:00+14:00").status_code == 400




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — the order the checks run in
# -----------------------------------------------------------

def test_the_title_is_checked_before_the_post_is_even_looked_up(client, actor):
    user, headers = actor

    response = _attach(client, headers, str(uuid.uuid4()), title="")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll title required"}


def test_the_options_are_checked_before_the_post_is_even_looked_up(client, actor):
    user, headers = actor

    response = _attach(client, headers, str(uuid.uuid4()), options=["Vienas"])

    assert response.status_code == 400
    assert response.get_json() == {"error": f"At least {news.MIN_POLL_OPTIONS} options required"}


def test_the_end_date_is_checked_before_the_post_is_even_looked_up(client, actor):
    user, headers = actor

    response = _attach(client, headers, str(uuid.uuid4()), end_date="kada nors")

    assert response.status_code == 400
    assert response.get_json() == {"error": "end_date must be an ISO-8601 timestamp"}


def test_a_hidden_post_is_a_404_before_the_ownership_check_can_say_403(client, actor, make_user, auth_headers):
    user, headers = actor
    stranger_headers = auth_headers(make_user())
    post_id = _new_post(client, headers, is_public=False)

    response = _attach(client, stranger_headers, post_id)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_a_stranger_on_a_scraped_article_is_refused_as_a_non_author_not_as_a_scrape(client, db, actor):
    user, headers = actor
    post_id = _plant_post(db, author_id=None, source="knf.vu.lt", post_type="article",
                          source_url="https://knf.vu.lt/naujiena")

    response = _attach(client, headers, post_id)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Only the post author or admin can create a poll"}


def test_a_scraped_article_that_already_carries_a_poll_is_still_refused_as_scraped(client, db, admin):
    user, headers = admin
    post_id = _plant_post(db, author_id=None, source="vu.lt", post_type="article",
                          source_url="https://vu.lt/naujiena")
    _plant_poll(db, post_id)

    response = _attach(client, headers, post_id)

    assert response.status_code == 400
    assert response.get_json() == {"error": "A scraped article cannot carry a poll"}




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — who may attach one
# -----------------------------------------------------------

def test_the_author_may_attach_a_poll_to_their_own_private_post(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers, is_public=False)

    assert _attach(client, headers, post_id).status_code == 201


def test_a_teacher_attaches_a_poll_to_their_own_faculty_post(client, db, make_user, auth_headers):
    teacher_headers = auth_headers(make_user(role="teacher"))
    post_id = _new_post(client, teacher_headers)

    assert _attach(client, teacher_headers, post_id).status_code == 201
    assert db.execute("SELECT source FROM news_posts WHERE id = ?", (post_id,)).fetchone()[0] == "faculty"


def test_a_teacher_who_can_see_another_teachers_private_post_still_may_not_attach_a_poll(
        client, make_user, auth_headers):
    author_headers = auth_headers(make_user(role="teacher"))
    other_headers = auth_headers(make_user(role="teacher"))
    post_id = _new_post(client, author_headers, is_public=False)

    response = _attach(client, other_headers, post_id)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Only the post author or admin can create a poll"}


def test_a_poll_hangs_on_a_source_app_row_that_no_route_could_have_written(client, db, admin):
    user, headers = admin
    post_id = _plant_post(db, author_id=user["id"], source="app", post_type="article")

    assert _attach(client, headers, post_id).status_code == 201




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — the one-poll rule
# -----------------------------------------------------------

def test_a_poll_planted_after_the_precheck_still_answers_409(client, db, actor, monkeypatch):
    user, headers = actor
    post_id = _new_post(client, headers)

    # The interleaving migration v26's UNIQUE index exists for.
    # utc_now_iso() is read between the pre-check and the INSERT,
    # so hooking it reproduces the race without threads
    import sqlite3

    real_now = news.utc_now_iso
    landed = []

    def _land_a_rival():
        if not landed:
            landed.append(True)
            rival = sqlite3.connect(client.application.config["DB_PATH"], timeout=15)
            try:
                rival.execute(
                    "INSERT INTO polls (id, post_id, title, created_at) VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), post_id, "Varzovas", real_now()),
                )
                rival.commit()
            finally:
                rival.close()
        return real_now()

    monkeypatch.setattr(news, "utc_now_iso", _land_a_rival)

    response = _attach(client, headers, post_id)

    assert response.status_code == 409
    assert response.get_json() == {"error": "Post already has a poll"}
    # the loser rolled back: no second poll, no options, and the
    # post_type flip never ran
    assert db.execute("SELECT COUNT(*) FROM polls WHERE post_id = ?", (post_id,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == 0
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?", (post_id,)).fetchone()[0] == "social"


def test_a_poll_planted_by_hand_makes_the_friendly_precheck_answer_409(client, db, actor):
    user, headers = actor
    post_id = _plant_post(db, author_id=user["id"])
    _plant_poll(db, post_id)

    response = _attach(client, headers, post_id)

    assert response.status_code == 409
    assert response.get_json() == {"error": "Post already has a poll"}




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — what a successful attach leaves
# -----------------------------------------------------------

def test_the_created_poll_starts_empty_and_unvoted(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    body = _attach(client, headers, post_id, options=["Taip", "Ne"]).get_json()

    assert body["totalVotes"] == 0
    assert body["userVote"] is None
    assert [o["votes"] for o in body["options"]] == [0, 0]
    assert body["postId"] == post_id


def test_the_created_poll_carries_the_normalised_end_date_back(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    body = _attach(client, headers, post_id, end_date="2026-12-31T21:30:00+03:00").get_json()

    assert body["endDate"] == "2026-12-31T18:30:00+00:00"


def test_a_created_poll_carries_a_null_end_date_when_none_was_sent(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    assert _attach(client, headers, post_id).get_json()["endDate"] is None


def test_an_explicit_null_end_date_means_the_poll_never_closes(client, db, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _post_raw(client, f"/api/news/{post_id}/poll",
                         {"title": "Klausimas", "options": ["A", "B"], "end_date": None}, headers)

    assert response.status_code == 201
    assert db.execute("SELECT end_date FROM polls").fetchone()[0] is None


def test_the_attached_poll_travels_with_the_post_detail(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)

    body = client.get(f"/api/news/{post_id}", headers=headers).get_json()

    assert body["postType"] == "poll"
    assert body["poll"]["id"] == poll["id"]




# -----------------------------------------------------------
# DELETE /api/news/<post_id>/poll
# -----------------------------------------------------------

def test_detaching_from_an_unknown_post_is_a_404(client, actor):
    user, headers = actor

    response = _detach(client, headers, str(uuid.uuid4()))

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_the_ownership_check_runs_before_the_no_poll_check(client, actor, make_user, auth_headers):
    user, headers = actor
    stranger_headers = auth_headers(make_user())
    post_id = _new_post(client, headers)

    response = _detach(client, stranger_headers, post_id)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Only the post author or admin can delete this poll"}


def test_the_author_of_a_poll_less_post_gets_the_no_poll_404(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _detach(client, headers, post_id)

    assert response.status_code == 404
    assert response.get_json() == NO_POLL


def test_a_friend_who_can_see_the_private_post_still_may_not_detach_its_poll(
        client, db, actor, make_user, auth_headers):
    user, headers = actor
    friend = make_user()
    friend_headers = auth_headers(friend)
    post_id = _new_post(client, headers, is_public=False)
    _attach(client, headers, post_id)
    _befriend(db, user["id"], friend["id"])

    assert _read(client, post_id, friend_headers).status_code == 200
    assert _detach(client, friend_headers, post_id).status_code == 403


def test_detaching_a_wall_polls_post_restores_social(client, db, actor):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)

    response = _detach(client, headers, post_id)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted", "postType": "social"}
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?", (post_id,)).fetchone()[0] == "social"


def test_detaching_a_faculty_polls_post_restores_announcement(client, db, make_user, auth_headers):
    teacher_headers = auth_headers(make_user(role="teacher"))
    post_id = _new_post(client, teacher_headers)
    _attach(client, teacher_headers, post_id)

    assert _detach(client, teacher_headers, post_id).get_json()["postType"] == "announcement"


def test_detaching_from_a_source_app_row_also_restores_announcement(client, db, admin):
    user, headers = admin
    post_id = _plant_post(db, author_id=user["id"], source="app", post_type="poll")
    _plant_poll(db, post_id)

    assert _detach(client, headers, post_id).get_json()["postType"] == "announcement"


def test_a_legacy_poll_on_a_scraped_row_restores_the_article_type(client, db, admin):
    user, headers = admin
    post_id = _plant_post(db, author_id=None, source="knf.vu.lt", post_type="poll",
                          source_url="https://knf.vu.lt/sena")
    _plant_poll(db, post_id)

    assert _detach(client, headers, post_id).get_json()["postType"] == "article"


def test_an_admin_may_detach_a_students_wall_poll(client, db, actor, admin):
    user, headers = actor
    admin_user, admin_headers = admin
    post_id, poll, _ = _poll_on(client, headers)

    assert _detach(client, admin_headers, post_id).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 0


def test_detaching_takes_the_options_and_the_votes_with_it(client, db, actor, make_user, auth_headers):
    user, headers = actor
    voter_headers = auth_headers(make_user())
    post_id, poll, option_ids = _poll_on(client, headers)
    _vote(client, voter_headers, post_id, option_ids[0])

    _detach(client, headers, post_id)

    assert db.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 0


def test_detaching_leaves_the_post_and_its_own_counters_alone(client, db, actor):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)
    client.post(f"/api/news/{post_id}/like", headers=headers)

    _detach(client, headers, post_id)
    row = db.execute("SELECT likes_count, content FROM news_posts WHERE id = ?", (post_id,)).fetchone()

    assert row["likes_count"] == 1
    assert row["content"] == "Apklausos irasas"


def test_detaching_a_poll_whose_post_never_claimed_one_still_rewrites_the_type(client, db, actor):
    user, headers = actor
    post_id = _plant_post(db, author_id=user["id"], post_type="social")
    _plant_poll(db, post_id)

    assert _detach(client, headers, post_id).get_json()["postType"] == "social"


def test_detaching_twice_answers_the_no_poll_404_the_second_time(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)

    assert _detach(client, headers, post_id).status_code == 200
    second = _detach(client, headers, post_id)

    assert second.status_code == 404
    assert second.get_json() == NO_POLL


def test_the_detached_polls_post_answers_the_no_poll_404_on_a_read(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)
    _detach(client, headers, post_id)

    assert _read(client, post_id, headers).get_json() == NO_POLL




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll/vote — the body
# -----------------------------------------------------------

def test_an_empty_object_vote_body_is_refused_as_a_missing_body(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)

    response = client.post(f"/api/news/{post_id}/poll/vote", headers=headers, json={})

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object body required"}


def test_a_vote_body_of_json_null_is_refused(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)

    response = _post_raw(client, f"/api/news/{post_id}/poll/vote", None, headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object body required"}


@pytest.mark.parametrize("bad", [None, "", " ", "\t\n", 0, 1, 1.5, True, False, [], ["o1"], {},
                                 {"id": "o1"}])
def test_an_option_id_that_is_not_a_non_blank_string_is_refused(client, actor, bad):
    user, headers = actor
    post_id, poll, _ = _poll_on(client, headers)

    response = _post_raw(client, f"/api/news/{post_id}/poll/vote", {"option_id": bad}, headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "option_id required"}


def test_the_vote_body_is_checked_before_the_post_is_even_looked_up(client, actor):
    user, headers = actor

    response = _vote(client, headers, str(uuid.uuid4()), "   ")

    assert response.status_code == 400
    assert response.get_json() == {"error": "option_id required"}




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll/vote — the gates
# -----------------------------------------------------------

def test_a_vote_on_a_hidden_post_answers_the_no_poll_404(client, actor, make_user, auth_headers):
    user, headers = actor
    stranger_headers = auth_headers(make_user())
    post_id = _new_post(client, headers, is_public=False)
    option_id = _attach(client, headers, post_id).get_json()["options"][0]["id"]

    response = _vote(client, stranger_headers, post_id, option_id)

    assert response.status_code == 404
    assert response.get_json() == NO_POLL


def test_a_vote_on_a_visible_post_with_no_poll_is_the_same_404(client, actor):
    user, headers = actor
    post_id = _new_post(client, headers)

    response = _vote(client, headers, post_id, str(uuid.uuid4()))

    assert response.status_code == 404
    assert response.get_json() == NO_POLL


def test_an_option_that_belongs_to_another_poll_is_refused(client, actor):
    user, headers = actor
    first, _, first_options = _poll_on(client, headers)
    second, _, _ = _poll_on(client, headers)

    response = _vote(client, headers, second, first_options[0])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid option"}


def test_an_option_id_of_a_poll_that_has_been_detached_is_refused(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)
    _detach(client, headers, post_id)
    _attach(client, headers, post_id, options=["Nauja A", "Nauja B"])

    response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid option"}


def test_a_teacher_may_vote_on_another_teachers_private_faculty_poll(client, make_user, auth_headers):
    author_headers = auth_headers(make_user(role="teacher"))
    other_headers = auth_headers(make_user(role="teacher"))
    post_id = _new_post(client, author_headers, is_public=False)
    option_id = _attach(client, author_headers, post_id).get_json()["options"][0]["id"]

    assert _vote(client, other_headers, post_id, option_id).status_code == 200


def test_a_student_may_not_vote_on_a_private_faculty_poll(client, actor, make_user, auth_headers):
    student, student_headers = actor
    author_headers = auth_headers(make_user(role="teacher"))
    post_id = _new_post(client, author_headers, is_public=False)
    option_id = _attach(client, author_headers, post_id).get_json()["options"][0]["id"]

    assert _vote(client, student_headers, post_id, option_id).status_code == 404


def test_an_admin_may_vote_on_a_private_wall_poll_of_a_stranger(client, actor, admin):
    user, headers = actor
    admin_user, admin_headers = admin
    post_id = _new_post(client, headers, is_public=False)
    option_id = _attach(client, headers, post_id).get_json()["options"][0]["id"]

    response = _vote(client, admin_headers, post_id, option_id)

    assert response.status_code == 200
    assert response.get_json()["userVote"] == option_id




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll/vote — the end-date gate
# -----------------------------------------------------------

def test_a_poll_with_no_end_date_never_closes(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)

    # 20 days, not 20 years: the bearer token the fixtures mint
    # lives 30, so a longer jump would only prove it expired
    with time_machine.travel(_now() + timedelta(days=20), tick=False):
        response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 200


def test_a_blank_end_date_string_is_falsy_and_leaves_the_poll_open(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)
    db.execute("UPDATE polls SET end_date = '' WHERE id = ?", (poll["id"],))
    db.commit()

    response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 200
    assert response.get_json()["totalVotes"] == 1


def test_a_whitespace_end_date_is_unparseable_and_still_leaves_the_poll_open(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)
    db.execute("UPDATE polls SET end_date = '   ' WHERE id = ?", (poll["id"],))
    db.commit()

    response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 200
    assert response.get_json()["totalVotes"] == 1


def test_a_poll_that_ended_one_second_ago_refuses_the_vote(client, actor):
    user, headers = actor
    end = _ahead(1)
    post_id, poll, option_ids = _poll_on(client, headers, end_date=end.isoformat())

    with time_machine.travel(end + timedelta(seconds=1), tick=False):
        response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll has ended"}


def test_a_poll_one_microsecond_past_its_end_is_already_closed(client, actor):
    user, headers = actor
    end = _ahead(1)
    post_id, poll, option_ids = _poll_on(client, headers, end_date=end.isoformat())

    with time_machine.travel(end + timedelta(microseconds=1), tick=False):
        response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 400


def test_the_refused_ended_vote_writes_no_row_and_moves_no_counter(client, db, actor):
    user, headers = actor
    end = _ahead(1)
    post_id, poll, option_ids = _poll_on(client, headers, end_date=end.isoformat())

    with time_machine.travel(end + timedelta(hours=1), tick=False):
        _vote(client, headers, post_id, option_ids[0])

    assert db.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 0
    assert db.execute("SELECT total_votes FROM polls WHERE id = ?", (poll["id"],)).fetchone()[0] == 0


def test_a_vote_already_held_survives_the_poll_closing(client, db, actor):
    user, headers = actor
    end = _ahead(1)
    post_id, poll, option_ids = _poll_on(client, headers, end_date=end.isoformat())
    assert _vote(client, headers, post_id, option_ids[0]).status_code == 200

    with time_machine.travel(end + timedelta(hours=1), tick=False):
        late = _vote(client, headers, post_id, option_ids[1])

    assert late.status_code == 400
    assert db.execute("SELECT option_id FROM poll_votes").fetchone()[0] == option_ids[0]




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll/vote — casting and moving
# -----------------------------------------------------------

def test_the_reply_to_a_first_vote_carries_the_callers_own_choice(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)

    body = _vote(client, headers, post_id, option_ids[0]).get_json()

    assert body["userVote"] == option_ids[0]
    assert body["totalVotes"] == 1
    assert [o["votes"] for o in body["options"]] == [1, 0]


def test_a_whitespace_padded_option_id_still_casts_a_first_vote(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)

    response = _vote(client, headers, post_id, f"  {option_ids[0]}  ")

    assert response.status_code == 200
    assert db.execute("SELECT option_id FROM poll_votes").fetchone()[0] == option_ids[0]


def test_moving_the_vote_back_to_the_first_option_is_accepted(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)

    _vote(client, headers, post_id, option_ids[0])
    _vote(client, headers, post_id, option_ids[1])
    back = _vote(client, headers, post_id, option_ids[0])

    assert back.status_code == 200
    assert back.get_json()["userVote"] == option_ids[0]
    assert back.get_json()["totalVotes"] == 1
    assert db.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 1


def test_walking_every_option_leaves_exactly_one_vote_row(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers, options=("A", "B", "C"))

    for option_id in option_ids:
        assert _vote(client, headers, post_id, option_id).status_code == 200

    assert db.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 1
    tallies = [r[0] for r in db.execute(
        "SELECT votes FROM poll_options WHERE poll_id = ? ORDER BY rowid", (poll["id"],)).fetchall()]
    assert tallies == [0, 0, 1]


def test_the_repeat_vote_409_names_the_option_the_caller_already_holds(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)
    _vote(client, headers, post_id, option_ids[0])

    response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 409
    assert response.get_json() == {"error": "Already voted for this option"}
    assert db.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 1


def test_the_reply_counts_the_votes_other_members_landed_meanwhile(client, actor, make_user, auth_headers):
    user, headers = actor
    other_headers = auth_headers(make_user())
    post_id, poll, option_ids = _poll_on(client, headers)
    _vote(client, other_headers, post_id, option_ids[1])

    body = _vote(client, headers, post_id, option_ids[0]).get_json()

    assert body["totalVotes"] == 2
    assert [o["votes"] for o in body["options"]] == [1, 1]
    assert body["userVote"] == option_ids[0]


def test_voting_leaves_the_polls_own_fields_untouched(client, actor):
    user, headers = actor
    end = _ahead(4).isoformat()
    post_id, poll, option_ids = _poll_on(client, headers, end_date=end)

    body = _vote(client, headers, post_id, option_ids[0]).get_json()

    assert body["title"] == poll["title"]
    assert body["endDate"] == poll["endDate"]
    assert body["createdAt"] == poll["createdAt"]
    assert body["id"] == poll["id"]


def test_a_vote_on_a_poll_hung_on_a_plain_social_post_still_lands(client, db, actor):
    user, headers = actor
    post_id = _plant_post(db, author_id=user["id"], post_type="social")
    poll_id, option_ids = _plant_poll(db, post_id)

    response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 200
    assert response.get_json()["totalVotes"] == 1


def test_a_drifted_option_tally_heals_on_the_next_vote(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_on(client, headers)
    db.execute("UPDATE poll_options SET votes = 99 WHERE id = ?", (option_ids[1],))
    db.execute("UPDATE polls SET total_votes = 99 WHERE id = ?", (poll["id"],))
    db.commit()

    body = _vote(client, headers, post_id, option_ids[0]).get_json()

    assert body["totalVotes"] == 1
    assert [o["votes"] for o in body["options"]] == [1, 0]


def test_a_vote_on_a_poll_with_an_edge_of_calendar_end_date_still_answers(client, db, actor):
    # The far END of the calendar, so the end-date gate reads the
    # poll as OPEN and the vote really does commit — the reply is
    # then assembled through _poll_shape → _to_utc_iso, which used
    # to overflow there and hand the caller a 500 for a write that
    # had already landed
    user, headers = actor
    post_id = _plant_post(db, author_id=user["id"])
    poll_id, option_ids = _plant_poll(db, post_id, end_date="9999-12-31T23:59:59-14:00")

    response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 200
    assert response.get_json()["totalVotes"] == 1
