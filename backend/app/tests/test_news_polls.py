# -----------------------------------------------------------
#  [*] Tests — news polls (app/news/routes.py)
#
#  Everything the poll half of the news blueprint promises,
#  proved end to end through the real routes:
#
#    - a poll hangs only off a post the caller can SEE and
#      OWNS (or an admin's), never off a scraped article, and
#      never twice — the friendly pre-check AND migration
#      v26's UNIQUE polls(post_id) both answer 409, and the
#      loser of the race leaves no second poll and no second
#      option set behind.
#    - the body is typed and bounded: options a LIST of
#      strings, blanks stripped BEFORE the 2..10 count, a
#      100-character cap per option, a 200-character title —
#      so a poll can never exist with fewer options than the
#      client declared.
#    - one vote per user per poll: the first cast counts once,
#      the same option again is a 409 that moves no counter, a
#      different option MOVES the vote instead of adding one,
#      and every tally is recomputed from poll_votes, so a
#      drifted counter heals instead of compounding.
#    - a poll closes at the instant its end_date MEANS, offset
#      and all (a "+03:00" poll used to close three hours
#      late); an unparseable end_date leaves it open.
#    - GET answers the mobile PollResponse shape exactly,
#      userVote included, and answers 404 — the SAME body
#      every time — for a missing post, a hidden post and a
#      post with no poll, because "no poll" is the only thing
#      a caller may learn. The mobile fetchPoll turns exactly
#      that status into null.
# -----------------------------------------------------------

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import time_machine


POLL_FIELDS = {"id", "postId", "title", "endDate", "totalVotes", "createdAt", "userVote", "options"}
OPTION_FIELDS = {"id", "text", "votes"}




# -----------------------------------------------------------
# _soon
# -----------------------------------------------------------
#
# A whole-second instant a few hours ahead of the REAL clock —
# the anchor every end-date test travels around. An absolute
# far-future date would not do: the sessions the fixtures mint
# live 30 days, so a test that jumped months ahead would only
# prove the bearer token expired.
# -----------------------------------------------------------

def _soon(hours=1):
    return datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=hours)




# -----------------------------------------------------------
# _make_post
# -----------------------------------------------------------
#
# One post through the real POST /api/news — the poll routes
# all hang off a post, and creating it through the route
# leaves source/author/post_type exactly as production writes
# them (source 'user' for a student, 'faculty' for staff).
# -----------------------------------------------------------

def _make_post(client, headers, **body):
    payload = {"content": "Apklausos irasas"}
    payload.update(body)
    response = client.post("/api/news", headers=headers, json=payload)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["id"]




# -----------------------------------------------------------
# _seed_post
# -----------------------------------------------------------
#
# A news_posts row the routes cannot create: a scraped
# article (author_id NULL, source knf.vu.lt), a private wall
# post owned by somebody else, a post whose post_type already
# says 'poll' with no poll behind it.
# -----------------------------------------------------------

def _seed_post(db, author_id=None, source="user", is_public=1, post_type="social", source_url=None):
    post_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO news_posts
           (id, title, content, summary, author_id, author_name, source, source_url,
            post_type, is_public, published_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (post_id, "Įrašas", "Turinys", "Turinys", author_id, "Autorius",
         source, source_url, post_type, is_public, now, now, now),
    )
    db.commit()
    return post_id




# -----------------------------------------------------------
# _attach_poll / _fetch_poll / _vote / _detach_poll
# -----------------------------------------------------------
#
# The four poll calls, with the defaults every happy-path test
# would otherwise repeat. They return the raw response so an
# unhappy path can assert its status and body.
# -----------------------------------------------------------

def _attach_poll(client, headers, post_id, title="Kada rinktis?", options=None, **extra):
    payload = {"title": title, "options": ["Pirmadieni", "Antradieni"] if options is None else options}
    payload.update(extra)
    return client.post(f"/api/news/{post_id}/poll", headers=headers, json=payload)


def _fetch_poll(client, post_id, headers=None):
    return client.get(f"/api/news/{post_id}/poll", headers=headers or {})


def _vote(client, headers, post_id, option_id):
    return client.post(f"/api/news/{post_id}/poll/vote", headers=headers, json={"option_id": option_id})


def _detach_poll(client, headers, post_id):
    return client.delete(f"/api/news/{post_id}/poll", headers=headers)




# -----------------------------------------------------------
# _poll_with_options
# -----------------------------------------------------------
#
# The common arrangement: a post owned by `headers`, a poll on
# it, and the option ids in the order the creator sent them.
# -----------------------------------------------------------

def _poll_with_options(client, headers, options=("Taip", "Ne"), **extra):
    post_id = _make_post(client, headers)
    response = _attach_poll(client, headers, post_id, options=list(options), **extra)
    assert response.status_code == 201, response.get_json()
    poll = response.get_json()
    return post_id, poll, [o["id"] for o in poll["options"]]




# -----------------------------------------------------------
# _befriend
# -----------------------------------------------------------
#
# friendships is written in BOTH directions on accept, which
# is what _can_view_post's single-direction lookup relies on.
# -----------------------------------------------------------

def _befriend(db, one, other):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (one, other))
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (other, one))
    db.commit()




# -----------------------------------------------------------
# GET /api/news/<post_id>/poll — the 404-means-no-poll contract
# -----------------------------------------------------------

def test_an_unknown_post_answers_no_poll_found(client):
    response = _fetch_poll(client, "no-such-post")

    assert response.status_code == 404
    assert response.get_json() == {"error": "No poll found for this post"}


def test_a_post_without_a_poll_answers_the_same_404_as_a_missing_post(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    missing = _fetch_poll(client, "no-such-post", headers)
    pollless = _fetch_poll(client, post_id, headers)

    assert pollless.status_code == missing.status_code == 404
    assert pollless.get_json() == missing.get_json()


def test_a_post_that_only_claims_to_be_a_poll_still_answers_404(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], post_type="poll")

    assert _fetch_poll(client, post_id, headers).status_code == 404


def test_a_private_post_hides_its_poll_from_a_guest(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers, is_public=False)
    assert _attach_poll(client, headers, post_id).status_code == 201

    response = _fetch_poll(client, post_id)

    assert response.status_code == 404
    assert response.get_json() == {"error": "No poll found for this post"}


def test_a_private_post_hides_its_poll_from_a_stranger(client, make_user, auth_headers, actor):
    owner, owner_headers = actor
    post_id = _make_post(client, owner_headers, is_public=False)
    _attach_poll(client, owner_headers, post_id)

    stranger_headers = auth_headers(make_user())

    assert _fetch_poll(client, post_id, stranger_headers).status_code == 404


def test_a_private_posts_poll_reaches_its_author(client, actor):
    owner, headers = actor
    post_id = _make_post(client, headers, is_public=False)
    _attach_poll(client, headers, post_id)

    assert _fetch_poll(client, post_id, headers).status_code == 200


def test_a_private_posts_poll_reaches_an_admin(client, admin, actor):
    owner, owner_headers = actor
    post_id = _make_post(client, owner_headers, is_public=False)
    _attach_poll(client, owner_headers, post_id)

    admin_user, admin_headers = admin

    assert _fetch_poll(client, post_id, admin_headers).status_code == 200


def test_a_private_wall_posts_poll_reaches_a_friend(client, db, actor, make_user, auth_headers):
    owner, owner_headers = actor
    friend = make_user()
    _befriend(db, owner["id"], friend["id"])

    post_id = _make_post(client, owner_headers, is_public=False)
    _attach_poll(client, owner_headers, post_id)

    response = _fetch_poll(client, post_id, auth_headers(friend))

    assert response.status_code == 200
    assert response.get_json()["userVote"] is None


def test_a_private_scraped_articles_poll_is_hidden_from_a_student(client, db, actor, admin):
    admin_user, admin_headers = admin
    post_id = _seed_post(db, source="knf.vu.lt", is_public=0, post_type="article",
                         source_url="https://knf.vu.lt/private")
    db.execute("INSERT INTO polls (id, post_id, title, created_at) VALUES ('legacy-poll', ?, 'Sena', ?)",
               (post_id, datetime.now(timezone.utc).isoformat()))
    db.commit()

    student, student_headers = actor

    assert _fetch_poll(client, post_id, student_headers).status_code == 404
    assert _fetch_poll(client, post_id, admin_headers).status_code == 200




# -----------------------------------------------------------
# GET /api/news/<post_id>/poll — the wire shape
# -----------------------------------------------------------

@pytest.mark.contract
def test_the_poll_shape_is_exactly_what_the_mobile_client_reads(client, actor):
    user, headers = actor
    post_id, created, option_ids = _poll_with_options(client, headers, options=["Taip", "Ne", "Nezinau"])

    body = _fetch_poll(client, post_id, headers).get_json()

    assert set(body) == POLL_FIELDS
    assert body["id"] == created["id"]
    assert body["postId"] == post_id
    assert body["title"] == "Kada rinktis?"
    assert body["endDate"] is None
    assert body["totalVotes"] == 0
    assert body["createdAt"]
    assert body["userVote"] is None
    assert [o["text"] for o in body["options"]] == ["Taip", "Ne", "Nezinau"]
    for option in body["options"]:
        assert set(option) == OPTION_FIELDS
        assert option["votes"] == 0


def test_a_guest_may_read_a_public_polls_tally_but_never_a_uservote(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    assert _vote(client, headers, post_id, option_ids[0]).status_code == 200

    body = _fetch_poll(client, post_id).get_json()

    assert body["totalVotes"] == 1
    assert body["userVote"] is None
    assert {o["id"]: o["votes"] for o in body["options"]} == {option_ids[0]: 1, option_ids[1]: 0}


def test_a_voter_sees_their_own_choice_as_uservote(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[1])

    assert _fetch_poll(client, post_id, headers).get_json()["userVote"] == option_ids[1]


def test_another_member_sees_the_same_tally_but_no_uservote(client, actor, make_user, auth_headers):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[0])

    other = _fetch_poll(client, post_id, auth_headers(make_user())).get_json()

    assert other["totalVotes"] == 1
    assert other["userVote"] is None


def test_options_come_back_in_the_order_they_were_sent(client, actor):
    user, headers = actor
    wanted = ["Ziema", "Pavasaris", "Vasara", "Ruduo"]
    post_id, created, _ = _poll_with_options(client, headers, options=wanted)

    assert [o["text"] for o in created["options"]] == wanted
    assert [o["text"] for o in _fetch_poll(client, post_id, headers).get_json()["options"]] == wanted


def test_a_legacy_space_form_end_date_goes_out_as_explicit_utc(client, db, actor):
    user, headers = actor
    post_id, poll, _ = _poll_with_options(client, headers)
    db.execute("UPDATE polls SET end_date = '2026-12-01 09:00:00' WHERE id = ?", (poll["id"],))
    db.commit()

    assert _fetch_poll(client, post_id, headers).get_json()["endDate"] == "2026-12-01T09:00:00+00:00"


def test_an_unparseable_end_date_is_handed_back_untouched_never_dropped(client, db, actor):
    user, headers = actor
    post_id, poll, _ = _poll_with_options(client, headers)
    db.execute("UPDATE polls SET end_date = 'kada nors' WHERE id = ?", (poll["id"],))
    db.commit()

    # null would read as "never closes" on the client, so the
    # junk travels instead of vanishing
    assert _fetch_poll(client, post_id, headers).get_json()["endDate"] == "kada nors"


def test_poll_text_is_stored_raw_and_escaped_only_on_the_way_out(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    # json.dumps by hand: the test client would otherwise
    # serialise the body through the app's OWN escaping provider
    # and the markup would arrive pre-escaped
    body = json.dumps({"title": "Ar saugu?", "options": ["<script>alert(1)</script>", "Ne"]})
    created = client.post(f"/api/news/{post_id}/poll", headers=headers,
                          data=body, content_type="application/json").get_json()

    assert created["options"][0]["text"] == "&lt;script&gt;alert(1)&lt;/script&gt;"
    stored = db.execute("SELECT text FROM poll_options WHERE poll_id = ? ORDER BY rowid",
                        (created["id"],)).fetchone()[0]
    assert stored == "<script>alert(1)</script>"




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — body validation
# -----------------------------------------------------------

def test_attaching_a_poll_requires_authentication(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, {}, post_id)

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}


def test_a_missing_body_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = client.post(f"/api/news/{post_id}/poll", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object body required"}


def test_a_malformed_json_body_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = client.post(f"/api/news/{post_id}/poll", headers=headers,
                           data="{not json", content_type="application/json")

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object body required"}


def test_a_top_level_array_body_never_reaches_the_route(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = client.post(f"/api/news/{post_id}/poll", headers=headers, json=["Taip", "Ne"])

    assert response.status_code == 400
    assert db.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 0


def test_a_non_string_title_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, title=42)

    assert response.status_code == 400
    assert response.get_json() == {"error": "title must be a string"}


def test_a_blank_title_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, title="   ")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll title required"}


def test_a_missing_title_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = client.post(f"/api/news/{post_id}/poll", headers=headers,
                           json={"options": ["Taip", "Ne"]})

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll title required"}


def test_a_title_of_exactly_two_hundred_characters_is_accepted(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, title="k" * 200)

    assert response.status_code == 201
    assert response.get_json()["title"] == "k" * 200


def test_a_title_one_character_over_the_cap_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, title="k" * 201)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll title must be at most 200 characters"}


def test_options_must_be_a_list_not_a_bare_string(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    # A bare string used to pass the len() check and be iterated
    # character by character
    response = _attach_poll(client, headers, post_id, options="Taip")

    assert response.status_code == 400
    assert response.get_json() == {"error": "options must be an array of strings"}


def test_options_must_all_be_strings(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, options=["Taip", 7])

    assert response.status_code == 400
    assert response.get_json() == {"error": "options must be an array of strings"}


def test_missing_options_are_refused_as_too_few(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = client.post(f"/api/news/{post_id}/poll", headers=headers, json={"title": "Kada?"})

    assert response.status_code == 400
    assert response.get_json() == {"error": "At least 2 options required"}


def test_blank_options_are_stripped_before_the_count_check(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    # Declared three, meant one — the poll must not be born with
    # fewer options than it advertised
    response = _attach_poll(client, headers, post_id, options=["Taip", "   ", ""])

    assert response.status_code == 400
    assert response.get_json() == {"error": "At least 2 options required"}
    assert db.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 0


def test_a_blank_option_is_dropped_from_an_otherwise_valid_poll(client, actor):
    user, headers = actor
    post_id, created, _ = _poll_with_options(client, headers, options=["Taip", "  ", "Ne"])

    assert [o["text"] for o in created["options"]] == ["Taip", "Ne"]


def test_two_options_are_the_accepted_minimum(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, options=["Taip", "Ne"])

    assert response.status_code == 201
    assert len(response.get_json()["options"]) == 2


def test_ten_options_are_the_accepted_maximum(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, options=[f"Variantas {i}" for i in range(10)])

    assert response.status_code == 201
    assert len(response.get_json()["options"]) == 10


def test_eleven_options_are_refused(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, options=[f"Variantas {i}" for i in range(11)])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Maximum 10 options allowed"}
    assert db.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == 0


def test_an_option_of_exactly_one_hundred_characters_is_accepted(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, options=["o" * 100, "Ne"])

    assert response.status_code == 201
    assert response.get_json()["options"][0]["text"] == "o" * 100


def test_an_option_one_character_over_the_cap_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, options=["o" * 101, "Ne"])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Each option must be at most 100 characters"}


def test_an_unparseable_end_date_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, end_date="rytoj")

    assert response.status_code == 400
    assert response.get_json() == {"error": "end_date must be an ISO-8601 timestamp"}


def test_a_non_string_end_date_is_refused(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, end_date=1764579600)

    assert response.status_code == 400
    assert response.get_json() == {"error": "end_date must be an ISO-8601 timestamp"}


def test_an_explicit_null_end_date_means_the_poll_never_closes(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, end_date=None)

    assert response.status_code == 201
    assert response.get_json()["endDate"] is None
    assert db.execute("SELECT end_date FROM polls WHERE post_id = ?", (post_id,)).fetchone()[0] is None


def test_an_offset_end_date_is_stored_normalised_to_utc(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, end_date="2026-12-01T12:00:00+03:00")

    assert response.status_code == 201
    assert response.get_json()["endDate"] == "2026-12-01T09:00:00+00:00"
    stored = db.execute("SELECT end_date FROM polls WHERE post_id = ?", (post_id,)).fetchone()[0]
    assert stored == "2026-12-01T09:00:00+00:00"


def test_a_zoneless_end_date_is_read_as_utc(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, end_date="2026-12-01T09:00:00")

    assert response.status_code == 201
    assert response.get_json()["endDate"] == "2026-12-01T09:00:00+00:00"


def test_an_end_date_whose_plus_arrived_as_a_space_is_still_understood(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    # What a query string does to "+00:00" on the way in
    response = _attach_poll(client, headers, post_id, end_date="2026-12-01T09:00:00 00:00")

    assert response.status_code == 201
    assert response.get_json()["endDate"] == "2026-12-01T09:00:00+00:00"




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — the gates
# -----------------------------------------------------------

def test_a_poll_on_an_unknown_post_is_a_404(client, actor):
    user, headers = actor

    response = _attach_poll(client, headers, "no-such-post")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_a_hidden_post_answers_404_not_403(client, actor, make_user, auth_headers):
    owner, owner_headers = actor
    post_id = _make_post(client, owner_headers, is_public=False)

    # A stranger must not be able to tell "private" from "missing"
    response = _attach_poll(client, auth_headers(make_user()), post_id)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_only_the_author_may_attach_a_poll(client, actor, make_user, auth_headers):
    owner, owner_headers = actor
    post_id = _make_post(client, owner_headers)

    response = _attach_poll(client, auth_headers(make_user()), post_id)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Only the post author or admin can create a poll"}


def test_a_friend_may_see_the_private_post_and_still_not_attach_a_poll(client, db, actor, make_user, auth_headers):
    owner, owner_headers = actor
    friend = make_user()
    _befriend(db, owner["id"], friend["id"])
    post_id = _make_post(client, owner_headers, is_public=False)

    response = _attach_poll(client, auth_headers(friend), post_id)

    assert response.status_code == 403


def test_a_curator_may_see_a_teachers_private_post_and_still_not_attach_a_poll(
        client, make_user, auth_headers):
    teacher = make_user(role="teacher")
    post_id = _make_post(client, auth_headers(teacher), is_public=False)

    response = _attach_poll(client, auth_headers(make_user(role="curator")), post_id)

    assert response.status_code == 403


def test_an_admin_may_attach_a_poll_to_someone_elses_post(client, actor, admin):
    owner, owner_headers = actor
    post_id = _make_post(client, owner_headers)
    admin_user, admin_headers = admin

    response = _attach_poll(client, admin_headers, post_id)

    assert response.status_code == 201
    assert response.get_json()["postId"] == post_id


def test_a_scraped_article_cannot_carry_a_poll(client, db, admin):
    admin_user, admin_headers = admin
    post_id = _seed_post(db, source="knf.vu.lt", post_type="article",
                         source_url="https://knf.vu.lt/naujiena")

    response = _attach_poll(client, admin_headers, post_id)

    assert response.status_code == 400
    assert response.get_json() == {"error": "A scraped article cannot carry a poll"}
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?", (post_id,)).fetchone()[0] == "article"


def test_a_vu_lt_article_cannot_carry_a_poll_either(client, db, admin):
    admin_user, admin_headers = admin
    post_id = _seed_post(db, source="vu.lt", post_type="article", source_url="https://vu.lt/naujiena")

    assert _attach_poll(client, admin_headers, post_id).status_code == 400




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — one poll per post
# -----------------------------------------------------------

def test_a_second_poll_on_the_same_post_is_refused(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_with_options(client, headers)

    response = _attach_poll(client, headers, post_id, title="Antra apklausa")

    assert response.status_code == 409
    assert response.get_json() == {"error": "Post already has a poll"}


def test_the_refused_second_poll_duplicates_nothing(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers, options=["Taip", "Ne"])

    _attach_poll(client, headers, post_id, title="Antra apklausa", options=["A", "B", "C"])

    assert db.execute("SELECT COUNT(*) FROM polls WHERE post_id = ?", (post_id,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == 2
    assert db.execute("SELECT title FROM polls WHERE post_id = ?", (post_id,)).fetchone()[0] == "Kada rinktis?"


def test_an_admin_cannot_add_a_second_poll_either(client, actor, admin):
    owner, owner_headers = actor
    post_id, poll, _ = _poll_with_options(client, owner_headers)
    admin_user, admin_headers = admin

    assert _attach_poll(client, admin_headers, post_id).status_code == 409


def test_a_poll_created_after_the_precheck_still_answers_409(client, db, actor, monkeypatch):
    user, headers = actor
    post_id = _make_post(client, headers)

    # The race the UNIQUE index exists for: a competing poll
    # lands between the friendly pre-check and the INSERT.
    # utc_now_iso() is read at exactly that point, so hooking it
    # reproduces the interleaving without threads
    import sqlite3

    from app.news import routes as news_routes

    real_now = news_routes.utc_now_iso
    landed = []

    def _land_a_competing_poll():
        if not landed:
            landed.append(True)
            rival = sqlite3.connect(client.application.config["DB_PATH"], timeout=15)
            try:
                rival.execute(
                    "INSERT INTO polls (id, post_id, title, created_at) VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), post_id, "Varzovo apklausa", real_now()),
                )
                rival.commit()
            finally:
                rival.close()
        return real_now()

    monkeypatch.setattr(news_routes, "utc_now_iso", _land_a_competing_poll)

    response = _attach_poll(client, headers, post_id)

    assert response.status_code == 409
    assert response.get_json() == {"error": "Post already has a poll"}
    assert db.execute("SELECT COUNT(*) FROM polls WHERE post_id = ?", (post_id,)).fetchone()[0] == 1
    # The loser's options must not survive its rolled-back insert
    assert db.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == 0




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll — what a successful attach does
# -----------------------------------------------------------

@pytest.mark.contract
def test_the_created_poll_is_returned_in_the_poll_shape(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _attach_poll(client, headers, post_id, options=["Taip", "Ne"])
    body = response.get_json()

    assert response.status_code == 201
    assert set(body) == POLL_FIELDS
    assert body["postId"] == post_id
    assert body["totalVotes"] == 0
    assert body["userVote"] is None
    assert all(o["votes"] == 0 for o in body["options"])


def test_attaching_a_poll_flips_the_posts_type(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?", (post_id,)).fetchone()[0] == "social"

    _attach_poll(client, headers, post_id)

    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?", (post_id,)).fetchone()[0] == "poll"
    assert client.get(f"/api/news/{post_id}", headers=headers).get_json()["postType"] == "poll"


def test_the_options_are_persisted_in_order_with_zero_votes(client, db, actor):
    user, headers = actor
    post_id, poll, _ = _poll_with_options(client, headers, options=["Taip", "Ne", "Gal"])

    rows = db.execute(
        "SELECT text, votes FROM poll_options WHERE poll_id = ? ORDER BY rowid", (poll["id"],)
    ).fetchall()

    assert [r["text"] for r in rows] == ["Taip", "Ne", "Gal"]
    assert [r["votes"] for r in rows] == [0, 0, 0]


def test_the_poll_routes_stop_taking_calls_once_the_budget_is_spent(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    # A rejected call spends an attempt exactly like a good one
    for _ in range(20):
        assert _attach_poll(client, headers, post_id, title="").status_code == 400

    response = _attach_poll(client, headers, post_id)

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) >= 1




# -----------------------------------------------------------
# POST /api/news/<post_id>/poll/vote
# -----------------------------------------------------------

def test_voting_requires_authentication(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)

    response = _vote(client, {}, post_id, option_ids[0])

    assert response.status_code == 401


def test_a_vote_with_no_body_is_refused(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)

    response = client.post(f"/api/news/{post_id}/poll/vote", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object body required"}


@pytest.mark.parametrize("bad", [None, 7, ["a"], {"id": "a"}, "", "   "])
def test_an_option_id_that_is_not_a_non_blank_string_is_refused(client, actor, bad):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)

    response = client.post(f"/api/news/{post_id}/poll/vote", headers=headers, json={"option_id": bad})

    assert response.status_code == 400
    assert response.get_json() == {"error": "option_id required"}


def test_a_vote_on_an_unknown_post_is_a_404(client, actor):
    user, headers = actor

    response = _vote(client, headers, "no-such-post", "no-such-option")

    assert response.status_code == 404
    assert response.get_json() == {"error": "No poll found for this post"}


def test_a_vote_on_a_post_without_a_poll_is_a_404(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _vote(client, headers, post_id, "no-such-option")

    assert response.status_code == 404
    assert response.get_json() == {"error": "No poll found for this post"}


def test_a_stranger_cannot_vote_on_a_private_posts_poll(client, actor, make_user, auth_headers, db):
    owner, owner_headers = actor
    post_id = _make_post(client, owner_headers, is_public=False)
    poll = _attach_poll(client, owner_headers, post_id).get_json()

    response = _vote(client, auth_headers(make_user()), post_id, poll["options"][0]["id"])

    assert response.status_code == 404
    assert response.get_json() == {"error": "No poll found for this post"}
    assert db.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 0


def test_an_option_from_another_poll_is_refused(client, actor):
    user, headers = actor
    first_post, first_poll, first_options = _poll_with_options(client, headers)
    second_post, second_poll, second_options = _poll_with_options(client, headers)

    response = _vote(client, headers, first_post, second_options[0])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid option"}


def test_an_option_id_that_exists_nowhere_is_refused(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)

    response = _vote(client, headers, post_id, str(uuid.uuid4()))

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid option"}
    assert db.execute("SELECT total_votes FROM polls WHERE id = ?", (poll["id"],)).fetchone()[0] == 0


@pytest.mark.contract
def test_a_first_vote_counts_once_and_answers_with_the_fresh_poll(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)

    response = _vote(client, headers, post_id, option_ids[0])
    body = response.get_json()

    assert response.status_code == 200
    assert set(body) == POLL_FIELDS
    assert body["userVote"] == option_ids[0]
    assert body["totalVotes"] == 1
    assert {o["id"]: o["votes"] for o in body["options"]} == {option_ids[0]: 1, option_ids[1]: 0}
    assert db.execute(
        "SELECT option_id FROM poll_votes WHERE user_id = ? AND poll_id = ?", (user["id"], poll["id"])
    ).fetchone()[0] == option_ids[0]


def test_voting_the_same_option_twice_is_a_409(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[0])

    response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 409
    assert response.get_json() == {"error": "Already voted for this option"}


def test_the_refused_repeat_moves_no_counter(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[0])

    for _ in range(3):
        assert _vote(client, headers, post_id, option_ids[0]).status_code == 409

    after = _fetch_poll(client, post_id, headers).get_json()
    assert after["totalVotes"] == 1
    assert {o["id"]: o["votes"] for o in after["options"]} == {option_ids[0]: 1, option_ids[1]: 0}
    assert db.execute("SELECT COUNT(*) FROM poll_votes WHERE poll_id = ?", (poll["id"],)).fetchone()[0] == 1


def test_a_whitespace_padded_option_id_still_matches_the_held_vote(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[0])

    response = client.post(f"/api/news/{post_id}/poll/vote", headers=headers,
                           json={"option_id": f"  {option_ids[0]}  "})

    assert response.status_code == 409


def test_moving_the_vote_shifts_the_tally_without_growing_the_total(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers, options=["Taip", "Ne", "Gal"])
    _vote(client, headers, post_id, option_ids[0])

    moved = _vote(client, headers, post_id, option_ids[2])
    body = moved.get_json()

    assert moved.status_code == 200
    assert body["userVote"] == option_ids[2]
    assert body["totalVotes"] == 1
    assert {o["id"]: o["votes"] for o in body["options"]} == {
        option_ids[0]: 0, option_ids[1]: 0, option_ids[2]: 1,
    }
    assert db.execute("SELECT COUNT(*) FROM poll_votes WHERE poll_id = ?", (poll["id"],)).fetchone()[0] == 1


def test_every_voter_counts_exactly_once(client, db, actor, make_user, auth_headers):
    owner, owner_headers = actor
    post_id, poll, option_ids = _poll_with_options(client, owner_headers)

    _vote(client, owner_headers, post_id, option_ids[0])
    for _ in range(3):
        _vote(client, auth_headers(make_user()), post_id, option_ids[1])

    body = _fetch_poll(client, post_id, owner_headers).get_json()

    assert body["totalVotes"] == 4
    assert {o["id"]: o["votes"] for o in body["options"]} == {option_ids[0]: 1, option_ids[1]: 3}
    assert db.execute("SELECT COUNT(*) FROM poll_votes WHERE poll_id = ?", (poll["id"],)).fetchone()[0] == 4


def test_a_drifted_tally_heals_on_the_next_vote(client, db, actor, make_user, auth_headers):
    owner, owner_headers = actor
    post_id, poll, option_ids = _poll_with_options(client, owner_headers)
    _vote(client, owner_headers, post_id, option_ids[0])

    # The drift migration v14 had to reset in production
    db.execute("UPDATE polls SET total_votes = 99 WHERE id = ?", (poll["id"],))
    db.execute("UPDATE poll_options SET votes = 77 WHERE poll_id = ?", (poll["id"],))
    db.commit()

    body = _vote(client, auth_headers(make_user()), post_id, option_ids[1]).get_json()

    assert body["totalVotes"] == 2
    assert {o["id"]: o["votes"] for o in body["options"]} == {option_ids[0]: 1, option_ids[1]: 1}


def test_a_guest_may_not_vote(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)

    assert _vote(client, {}, post_id, option_ids[0]).status_code == 401


def test_a_friend_may_vote_on_a_private_wall_posts_poll(client, db, actor, make_user, auth_headers):
    owner, owner_headers = actor
    friend = make_user()
    _befriend(db, owner["id"], friend["id"])
    post_id = _make_post(client, owner_headers, is_public=False)
    poll = _attach_poll(client, owner_headers, post_id).get_json()

    response = _vote(client, auth_headers(friend), post_id, poll["options"][0]["id"])

    assert response.status_code == 200
    assert response.get_json()["totalVotes"] == 1




# -----------------------------------------------------------
# The end-date gate
# -----------------------------------------------------------

def test_a_poll_past_its_end_date_refuses_the_vote(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)
    end = _soon(1)
    poll = _attach_poll(client, headers, post_id, end_date=end.isoformat()).get_json()

    with time_machine.travel(end + timedelta(seconds=1), tick=False):
        response = _vote(client, headers, post_id, poll["options"][0]["id"])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll has ended"}


def test_a_poll_before_its_end_date_takes_the_vote(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)
    end = _soon(2)
    poll = _attach_poll(client, headers, post_id, end_date=end.isoformat()).get_json()

    with time_machine.travel(end - timedelta(seconds=1), tick=False):
        response = _vote(client, headers, post_id, poll["options"][0]["id"])

    assert response.status_code == 200
    assert response.get_json()["totalVotes"] == 1


def test_a_poll_is_still_open_at_the_exact_instant_it_ends(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)
    end = _soon(1)
    poll = _attach_poll(client, headers, post_id, end_date=end.isoformat()).get_json()

    # "> end", not ">=" — the closing instant itself still counts
    with time_machine.travel(end, tick=False):
        response = _vote(client, headers, post_id, poll["options"][0]["id"])

    assert response.status_code == 200


def test_a_poll_closes_at_the_instant_its_offset_end_date_means(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)
    # The same instant written in a +03:00 zone: reading the
    # stamp zone-less used to close this poll three hours late
    end = _soon(6)
    response = _attach_poll(client, headers, post_id,
                            end_date=end.astimezone(timezone(timedelta(hours=3))).isoformat())
    option_id = response.get_json()["options"][0]["id"]

    assert db.execute("SELECT end_date FROM polls WHERE post_id = ?", (post_id,)).fetchone()[0] == end.isoformat()

    with time_machine.travel(end + timedelta(minutes=30), tick=False):
        late = _vote(client, headers, post_id, option_id)

    with time_machine.travel(end - timedelta(minutes=30), tick=False):
        early = _vote(client, headers, post_id, option_id)

    assert late.status_code == 400
    assert early.status_code == 200


def test_a_closed_poll_refuses_the_vote_before_it_checks_the_option(client, db, actor):
    user, headers = actor
    post_id = _make_post(client, headers)
    end = _soon(1)
    poll = _attach_poll(client, headers, post_id, end_date=end.isoformat()).get_json()

    with time_machine.travel(end + timedelta(days=7), tick=False):
        response = _vote(client, headers, post_id, "no-such-option")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll has ended"}
    assert db.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 0


def test_a_poll_created_with_a_past_end_date_is_born_closed(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    poll = _attach_poll(client, headers, post_id, end_date=past.isoformat()).get_json()

    response = _vote(client, headers, post_id, poll["options"][0]["id"])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Poll has ended"}


def test_an_unparseable_end_date_keeps_the_poll_open(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    db.execute("UPDATE polls SET end_date = 'per Jonines' WHERE id = ?", (poll["id"],))
    db.commit()

    response = _vote(client, headers, post_id, option_ids[0])

    assert response.status_code == 200
    assert response.get_json()["totalVotes"] == 1




# -----------------------------------------------------------
# DELETE /api/news/<post_id>/poll
# -----------------------------------------------------------

def test_detaching_a_poll_requires_authentication(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_with_options(client, headers)

    assert _detach_poll(client, {}, post_id).status_code == 401


def test_detaching_from_an_unknown_post_is_a_404(client, actor):
    user, headers = actor

    response = _detach_poll(client, headers, "no-such-post")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_detaching_from_a_hidden_post_is_a_404(client, actor, make_user, auth_headers):
    owner, owner_headers = actor
    post_id = _make_post(client, owner_headers, is_public=False)
    _attach_poll(client, owner_headers, post_id)

    response = _detach_poll(client, auth_headers(make_user()), post_id)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_a_stranger_may_not_detach_a_poll(client, actor, make_user, auth_headers):
    owner, owner_headers = actor
    post_id, poll, _ = _poll_with_options(client, owner_headers)

    response = _detach_poll(client, auth_headers(make_user()), post_id)

    assert response.status_code == 403
    assert response.get_json() == {"error": "Only the post author or admin can delete this poll"}


def test_detaching_when_there_is_no_poll_is_a_404(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    response = _detach_poll(client, headers, post_id)

    assert response.status_code == 404
    assert response.get_json() == {"error": "No poll found for this post"}


def test_the_author_detaches_the_poll_and_the_wall_post_goes_back_to_social(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[0])

    response = _detach_poll(client, headers, post_id)

    assert response.status_code == 200
    assert response.get_json() == {"status": "deleted", "postType": "social"}
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?", (post_id,)).fetchone()[0] == "social"
    assert _fetch_poll(client, post_id, headers).status_code == 404


def test_detaching_takes_the_options_and_votes_with_it(client, db, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[1])

    _detach_poll(client, headers, post_id)

    assert db.execute("SELECT COUNT(*) FROM polls").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM poll_options").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM poll_votes").fetchone()[0] == 0


def test_an_admin_detaches_a_faculty_poll_back_to_announcement(client, db, admin):
    admin_user, admin_headers = admin
    post_id, poll, _ = _poll_with_options(client, admin_headers)

    response = _detach_poll(client, admin_headers, post_id)

    assert response.get_json()["postType"] == "announcement"
    assert db.execute("SELECT post_type FROM news_posts WHERE id = ?", (post_id,)).fetchone()[0] == "announcement"


def test_a_legacy_poll_on_a_scraped_row_restores_the_article_type(client, db, admin):
    admin_user, admin_headers = admin
    post_id = _seed_post(db, source="knf.vu.lt", post_type="poll", source_url="https://knf.vu.lt/sena")
    db.execute("INSERT INTO polls (id, post_id, title, created_at) VALUES ('legacy', ?, 'Sena', ?)",
               (post_id, datetime.now(timezone.utc).isoformat()))
    db.commit()

    response = _detach_poll(client, admin_headers, post_id)

    assert response.get_json() == {"status": "deleted", "postType": "article"}


def test_a_post_may_carry_a_new_poll_once_the_old_one_is_detached(client, actor):
    user, headers = actor
    post_id, poll, _ = _poll_with_options(client, headers)
    _detach_poll(client, headers, post_id)

    response = _attach_poll(client, headers, post_id, title="Nauja apklausa")

    assert response.status_code == 201
    assert response.get_json()["title"] == "Nauja apklausa"




# -----------------------------------------------------------
# The poll travelling with its post — feed and detail
# -----------------------------------------------------------

def test_the_post_detail_carries_the_poll_inline_with_the_viewers_vote(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[1])

    body = client.get(f"/api/news/{post_id}", headers=headers).get_json()

    assert body["poll"]["id"] == poll["id"]
    assert body["poll"]["userVote"] == option_ids[1]
    assert body["poll"]["totalVotes"] == 1


def test_an_unknown_posts_detail_is_a_404(client, actor):
    user, headers = actor

    response = client.get("/api/news/no-such-post", headers=headers)

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_a_hidden_poll_posts_detail_is_the_same_404(client, actor, make_user, auth_headers):
    owner, owner_headers = actor
    post_id = _make_post(client, owner_headers, is_public=False)
    _attach_poll(client, owner_headers, post_id)

    response = client.get(f"/api/news/{post_id}", headers=auth_headers(make_user()))

    assert response.status_code == 404
    assert response.get_json() == {"error": "Post not found"}


def test_a_post_without_a_poll_carries_no_poll_key(client, actor):
    user, headers = actor
    post_id = _make_post(client, headers)

    assert "poll" not in client.get(f"/api/news/{post_id}", headers=headers).get_json()


def test_a_post_that_only_claims_to_be_a_poll_carries_no_poll_key(client, db, actor):
    user, headers = actor
    post_id = _seed_post(db, author_id=user["id"], post_type="poll")

    body = client.get(f"/api/news/{post_id}", headers=headers).get_json()

    assert body["postType"] == "poll"
    assert "poll" not in body


def test_the_feed_ships_a_poll_card_with_its_poll(client, actor):
    user, headers = actor
    post_id, poll, option_ids = _poll_with_options(client, headers)
    _vote(client, headers, post_id, option_ids[0])

    page = client.get("/api/news", headers=headers).get_json()
    card = next(p for p in page["posts"] if p["id"] == post_id)

    assert set(card["poll"]) == POLL_FIELDS
    assert card["poll"]["userVote"] == option_ids[0]
    assert card["poll"]["totalVotes"] == 1


def test_a_guests_feed_ships_the_poll_without_a_uservote(client, admin):
    admin_user, admin_headers = admin
    post_id, poll, option_ids = _poll_with_options(client, admin_headers)
    _vote(client, admin_headers, post_id, option_ids[0])

    page = client.get("/api/news").get_json()
    card = next(p for p in page["posts"] if p["id"] == post_id)

    assert card["poll"]["userVote"] is None
    assert card["poll"]["totalVotes"] == 1


def test_a_member_who_has_not_voted_sees_no_uservote_in_the_feed(client, admin, make_user, auth_headers):
    admin_user, admin_headers = admin
    post_id, poll, option_ids = _poll_with_options(client, admin_headers)
    _vote(client, admin_headers, post_id, option_ids[0])

    page = client.get("/api/news", headers=auth_headers(make_user())).get_json()
    card = next(p for p in page["posts"] if p["id"] == post_id)

    assert card["poll"]["userVote"] is None


def test_the_poll_batch_answers_nothing_for_an_empty_page(app):
    from app.database import get_db
    from app.news.routes import _polls_for_posts

    with app.app_context():
        db = get_db()
        try:
            assert _polls_for_posts(db, [], None) == {}
        finally:
            db.close()
