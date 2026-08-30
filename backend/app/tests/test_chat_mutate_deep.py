# -----------------------------------------------------------
#  [*] Tests — chat mutations, the exhaustive pass
#
#  The three routes in app/chat/routes.py that CHANGE a room
#  without writing a message, driven down every arm the code
#  actually has:
#
#    - delete_message (DELETE .../messages/<mid>) — the gate
#      ORDER (403 before 404 before 403), the ownership rule
#      no role escapes, the idempotency guard that keeps the
#      first deleted_at and stays silent on the wire, the
#      exact blast radius of one unsend (reactions go, the
#      receipts and every other message stay, the room is not
#      bumped, the search index forgets the text) and the
#      four /api/uploads/ prefix arms of the blob cleanup.
#    - toggle_pin (PUT .../pin) — the atomic `1 - pinned`
#      flip, rowcount as the ONLY membership gate (no
#      conversation lookup at all), the per-member privacy of
#      a pin, the untouched neighbours of the row it writes,
#      and the wire shape: a real JSON boolean.
#    - leave_conversation (DELETE /conversations/<id>) — the
#      404-then-403 order, the leaver-scoped deletes, the
#      last-member purge (messages, receipts, reactions,
#      search index) and the best-effort socket eviction that
#      may never fail an already committed leave.
#
#  Quotas are spent by planting monotonic stamps in the shared
#  limiter, never by 100 real calls; every stamp a row carries
#  is fabricated, so nothing here races the wall clock.
# -----------------------------------------------------------


import os
import sqlite3
import time
import uuid

import pytest


CONVERSATIONS = "/api/chat/conversations"




# -----------------------------------------------------------
# chat_routes / chat_events
# -----------------------------------------------------------
#
# The modules under test, imported only after the `app`
# fixture has pinned DB_PATH and friends — the package must
# never be pulled in against a stray environment at collection
# time.
#
# Used by:
#   - the socket-eviction and broadcast tests below
# -----------------------------------------------------------

@pytest.fixture
def chat_routes(app):
    from app.chat import routes
    return routes


@pytest.fixture
def chat_events(app):
    from app.chat import events
    return events




# -----------------------------------------------------------
# clean_buckets
# -----------------------------------------------------------
#
# The rate-limit store is a module-level dict that outlives
# any single test. Every test here starts and ends with an
# empty one, so a quota test can never bleed into the next
# file's actor.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_buckets(app):
    from app.auth import routes as auth_routes

    auth_routes._rate_limit_store.clear()
    yield
    auth_routes._rate_limit_store.clear()




# -----------------------------------------------------------
# fresh_upload_dir
# -----------------------------------------------------------
#
# uploads/routes.py resolves UPLOAD_DIR ONCE per process and
# caches it in a module global, so whichever test calls the
# real delete_upload first pins its own tmp directory for
# every test after it. Cleared on both sides here (a plain
# assignment, not monkeypatch, which would restore the stale
# value) so the unsend tests neither inherit another module's
# directory nor leave this one behind — the same guard
# test_uploads.py carries.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_upload_dir(app):
    from app.uploads import routes as uploads_routes

    uploads_routes._upload_dir = None
    yield
    uploads_routes._upload_dir = None




# -----------------------------------------------------------
# _stamp
# -----------------------------------------------------------
#
# A naive-UTC isoformat stamp in the exact shape the routes
# write and every cursor compares as TEXT. Far in the past and
# zero padded, so a planted row always sorts before anything a
# route stamps with the real clock.
#
# Used by:
#   - _plant_conversation, _plant_message and the ordering
#     assertions below
# -----------------------------------------------------------

def _stamp(index):
    return f"2020-03-01T09:{index // 60:02d}:{index % 60:02d}.000000"




# -----------------------------------------------------------
# _plant_*
# -----------------------------------------------------------
#
# Rows written straight through the test connection, which
# runs with PRAGMA foreign_keys OFF — that is what lets a test
# arrange the states no route will create: a membership row
# whose conversation is gone, an already-unsent message that
# still carries a photo, a pinned flag outside 0/1.
#
# Used by:
#   - nearly every test below
# -----------------------------------------------------------

def _plant_conversation(db, member_ids, conv_type="group", title="Kursiokai",
                        conv_id=None, created_at=None, pinned_for=(), last_read=None):
    conv_id = conv_id or f"conv-{uuid.uuid4().hex[:8]}"
    now = created_at or _stamp(0)
    db.execute(
        "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conv_id, conv_type, None if conv_type == "direct" else title, None,
         member_ids[0] if member_ids else None, now, now),
    )
    for uid in member_ids:
        db.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id, pinned, last_read_at)"
            " VALUES (?, ?, ?, ?)",
            (conv_id, uid, 1 if uid in pinned_for else 0, (last_read or {}).get(uid)),
        )
    db.commit()
    return conv_id


def _plant_message(db, conv_id, sender_id, text="Labas", created_at=None, msg_id=None,
                   image_url=None, deleted_at=None):
    msg_id = msg_id or f"msg-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url, deleted_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, conv_id, sender_id, text, image_url, deleted_at, created_at or _stamp(1)),
    )
    db.commit()
    return msg_id


def _plant_read(db, msg_id, user_id, read_at=_stamp(2)):
    db.execute("INSERT INTO message_reads (message_id, user_id, read_at) VALUES (?, ?, ?)",
               (msg_id, user_id, read_at))
    db.commit()


def _plant_reaction(db, msg_id, user_id, emoji="\U0001F44D"):
    db.execute("INSERT INTO message_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
               (msg_id, user_id, emoji))
    db.commit()




# -----------------------------------------------------------
# row readers
# -----------------------------------------------------------
#
# Used by:
#   - the persistence assertions below
# -----------------------------------------------------------

def _message_row(db, msg_id):
    return db.execute("SELECT text, image_url, deleted_at FROM messages WHERE id = ?",
                      (msg_id,)).fetchone()


def _pin_row(db, conv_id, user_id):
    return db.execute(
        "SELECT pinned, last_read_at FROM conversation_participants"
        " WHERE conversation_id = ? AND user_id = ?", (conv_id, user_id)).fetchone()


def _members_of(db, conv_id):
    return sorted(r[0] for r in db.execute(
        "SELECT user_id FROM conversation_participants WHERE conversation_id = ?", (conv_id,)))


def _updated_at(db, conv_id):
    return db.execute("SELECT updated_at FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0]




# -----------------------------------------------------------
# _fts_hits
# -----------------------------------------------------------
#
# The rowids the FTS5 shadow table still matches for a needle,
# or None when this SQLite has no FTS5 and migration v20 left
# the table uncreated (the search route falls back to LIKE
# there, and the index assertions have nothing to say).
#
# Used by:
#   - the unsend and purge index tests below
# -----------------------------------------------------------

def _fts_hits(db, needle):
    try:
        return [r[0] for r in db.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", (needle,))]
    except sqlite3.OperationalError:
        return None




# -----------------------------------------------------------
# _fill_bucket
# -----------------------------------------------------------
#
# Spends `count` of a scope's budget for one user by planting
# the monotonic stamps the shared limiter keeps in memory —
# 100 real unsends would prove the same thing a hundred times
# slower. `age` pushes the stamps back so a test can plant a
# window that has already aged out.
#
# Used by:
#   - the delete_message quota tests below
# -----------------------------------------------------------

def _fill_bucket(scope, user_id, count, age=0.0):
    from app.auth import routes as auth_routes

    with auth_routes._rate_limit_lock:
        auth_routes._rate_limit_store[f"{scope}:{user_id}"] = [time.monotonic() - age] * count




# -----------------------------------------------------------
# room
# -----------------------------------------------------------
#
# A two-person group with one message from the caller, the
# common arrangement for the unsend tests:
# (conv_id, user, headers, other, other_headers, msg_id).
#
# Used by:
#   - most delete_message tests below
# -----------------------------------------------------------

@pytest.fixture
def room(db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user(display_name="Ona Onaitė")
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    msg_id = _plant_message(db, conv_id, user["id"], text="mano žinutė")
    return conv_id, user, headers, other, auth_headers(other), msg_id




# ===========================================================
# DELETE .../messages/<mid> — the gates and their order
# ===========================================================


def test_unsending_in_a_conversation_that_does_not_exist_is_a_403(client, actor):
    # No conversation lookup runs at all: the membership row is
    # missing, so the outsider answer comes out — not a 404 that
    # would confirm the room is unknown
    _, headers = actor

    response = client.delete(f"{CONVERSATIONS}/nera-tokio/messages/nera-tokios", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"


def test_an_ex_member_cannot_unsend_their_own_message(client, db, room):
    conv_id, _, headers, _, _, msg_id = room
    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200

    response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"
    assert _message_row(db, msg_id)["deleted_at"] is None, "the message the room kept is untouched"


def test_no_role_may_unsend_another_members_message(client, db, actor, make_user, auth_headers):
    # Ownership is not a permission the app grades by role —
    # a curator and an admin are refused exactly like a peer
    user, _ = actor
    roles = ["student", "teacher", "curator", "admin"]
    others = [make_user(role=role) for role in roles]
    conv_id = _plant_conversation(db, [user["id"]] + [o["id"] for o in others])
    msg_id = _plant_message(db, conv_id, user["id"], text="tik mano")

    for role, other in zip(roles, others):
        response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}",
                                 headers=auth_headers(other))
        assert response.status_code == 403, f"{role} was allowed to unsend"
        assert response.get_json()["error"] == "Only the sender can delete a message"

    assert _message_row(db, msg_id)["deleted_at"] is None


def test_a_message_id_full_of_like_wildcards_is_just_not_found(client, room):
    conv_id, _, headers, _, _, _ = room

    response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/%25_%27 OR 1=1--",
                             headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Message not found"


def test_an_encoded_slash_in_the_message_id_is_a_404_not_a_routing_error(client, room):
    conv_id, _, headers, _, _, _ = room

    response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/..%2F..%2Fetc", headers=headers)

    assert response.status_code == 404


def test_a_two_kilobyte_message_id_is_a_404(client, room):
    conv_id, _, headers, _, _, _ = room

    response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{'x' * 2048}", headers=headers)

    assert response.status_code == 404


def test_the_empty_message_id_is_not_a_route_at_all(client, room):
    conv_id, _, headers, _, _, _ = room

    response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/", headers=headers)

    # Werkzeug matches no rule for the trailing-slash form, so
    # the router answers before the blueprint — never a soft
    # delete of nothing
    assert response.status_code == 404




# ===========================================================
# DELETE .../messages/<mid> — the idempotency guard
# ===========================================================


def test_a_repeat_unsend_keeps_the_first_deleted_at(client, db, room):
    conv_id, _, headers, _, _, msg_id = room

    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}",
                         headers=headers).status_code == 200
    first_stamp = _message_row(db, msg_id)["deleted_at"]
    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}",
                         headers=headers).status_code == 200

    assert _message_row(db, msg_id)["deleted_at"] == first_stamp


def test_an_already_unsent_row_is_never_re_blanked(client, db, room):
    # A row unsent by an older build kept its text; the guard
    # must leave that alone rather than quietly rewrite history
    conv_id, user, headers, _, _, _ = room
    legacy = _plant_message(db, conv_id, user["id"], text="senas turinys",
                            deleted_at=_stamp(3), image_url="/api/uploads/senas.jpg")

    response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{legacy}", headers=headers)

    row = _message_row(db, legacy)
    assert response.status_code == 200
    assert row["text"] == "senas turinys"
    assert row["image_url"] == "/api/uploads/senas.jpg"
    assert row["deleted_at"] == _stamp(3)


def test_a_repeat_unsend_never_calls_the_upload_helper_again(client, db, room, monkeypatch):
    conv_id, user, headers, _, _, _ = room
    already = _plant_message(db, conv_id, user["id"], deleted_at=_stamp(3),
                             image_url="/api/uploads/liko.jpg")
    handed = []
    from app.uploads import routes as uploads_routes
    monkeypatch.setattr(uploads_routes, "delete_upload", handed.append)

    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{already}",
                         headers=headers).status_code == 200

    assert handed == []


def test_a_repeat_unsend_keeps_the_reactions_that_landed_after_the_first(client, db, room):
    # Nothing can react to an unsent message through the API,
    # but a planted row proves the guard skips the reaction
    # purge entirely rather than re-running it
    conv_id, user, headers, other, _, msg_id = room
    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)
    _plant_reaction(db, msg_id, other["id"])

    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}",
                         headers=headers).status_code == 200

    assert db.execute("SELECT COUNT(*) FROM message_reactions WHERE message_id = ?",
                      (msg_id,)).fetchone()[0] == 1


def test_a_refused_unsend_broadcasts_nothing(client, room, chat_events, monkeypatch, make_user,
                                             auth_headers):
    conv_id, _, headers, _, other_headers, msg_id = room
    seen = []
    monkeypatch.setattr(chat_events, "emit_message_deleted",
                        lambda sio, cid, mid: seen.append(mid))

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=other_headers)   # 403
    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/nera", headers=headers)             # 404
    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}",
                  headers=auth_headers(make_user()))                                       # 403

    assert seen == []




# ===========================================================
# DELETE .../messages/<mid> — the blast radius of one unsend
# ===========================================================


def test_unsending_keeps_the_read_receipts_of_the_message(client, db, room):
    # Only the reactions are purged: a receipt is evidence the
    # message WAS read, and the status math of the other
    # members still needs it
    conv_id, user, headers, other, _, msg_id = room
    _plant_read(db, msg_id, user["id"])
    _plant_read(db, msg_id, other["id"])

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    readers = sorted(r[0] for r in db.execute(
        "SELECT user_id FROM message_reads WHERE message_id = ?", (msg_id,)))
    assert readers == sorted([user["id"], other["id"]])


def test_unsending_touches_no_other_message_in_the_room(client, db, room):
    conv_id, user, headers, other, _, msg_id = room
    neighbour = _plant_message(db, conv_id, user["id"], text="lieka", created_at=_stamp(5),
                               image_url="/api/uploads/lieka.jpg")
    _plant_reaction(db, neighbour, other["id"])

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    row = _message_row(db, neighbour)
    assert row["text"] == "lieka"
    assert row["image_url"] == "/api/uploads/lieka.jpg"
    assert row["deleted_at"] is None
    assert db.execute("SELECT COUNT(*) FROM message_reactions WHERE message_id = ?",
                      (neighbour,)).fetchone()[0] == 1


def test_unsending_does_not_bump_the_conversation(client, db, room):
    # updated_at is the list ordering: an unsend must not lift
    # a room back to the top of everybody's Messages tab
    conv_id, _, headers, _, _, msg_id = room
    before = _updated_at(db, conv_id)

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert _updated_at(db, conv_id) == before


def test_unsending_does_not_move_anybodys_read_watermark(client, db, room):
    conv_id, user, headers, other, _, msg_id = room
    before = {uid: _pin_row(db, conv_id, uid)["last_read_at"] for uid in (user["id"], other["id"])}

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    after = {uid: _pin_row(db, conv_id, uid)["last_read_at"] for uid in (user["id"], other["id"])}
    assert after == before


def test_unsending_drops_the_text_out_of_the_search_index(client, db, room):
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], text="paslaptingas raktazodis",
                            created_at=_stamp(6))
    if _fts_hits(db, "raktazodis") is None:
        pytest.skip("this SQLite has no FTS5, so migration v20 left no index to check")
    assert _fts_hits(db, "raktazodis"), "the planted row must be indexed to begin with"

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    # The AFTER UPDATE OF text trigger unindexes the old text
    assert _fts_hits(db, "raktazodis") == []


def test_unsending_leaves_the_row_countable_for_the_cursor(client, db, room):
    conv_id, _, headers, _, _, msg_id = room

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 1




# ===========================================================
# DELETE .../messages/<mid> — the /api/uploads/ cleanup arms
# ===========================================================


@pytest.fixture
def captured_uploads(app, monkeypatch):
    from app.uploads import routes as uploads_routes

    handed = []
    monkeypatch.setattr(uploads_routes, "delete_upload", handed.append)
    return handed


def test_a_relative_uploads_path_is_handed_to_the_helper_verbatim(client, db, room,
                                                                  captured_uploads):
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], image_url="/api/uploads/nuotrauka.jpg")

    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}",
                         headers=headers).status_code == 200

    assert captured_uploads == ["/api/uploads/nuotrauka.jpg"]


def test_the_bare_uploads_prefix_still_reaches_the_helper(client, db, room, captured_uploads):
    # The prefix test is startswith, not a filename check —
    # the helper is the one that decides a name is not ours
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], image_url="/api/uploads/")

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert captured_uploads == ["/api/uploads/"]


def test_a_path_one_character_short_of_the_prefix_is_left_alone(client, db, room,
                                                                captured_uploads):
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], image_url="/api/uploads")

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert captured_uploads == []


def test_an_empty_image_url_is_left_alone(client, db, room, captured_uploads):
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], image_url="")

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert captured_uploads == []


def test_a_case_shifted_uploads_prefix_is_left_alone(client, db, room, captured_uploads):
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], image_url="/API/Uploads/nuotrauka.jpg")

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert captured_uploads == []


def test_an_absolute_same_origin_uploads_url_is_cleaned_up_too(client, db, room, captured_uploads):
    # the cleanup matches every form send_message accepts, so
    # the blob of a photo sent as an absolute same-origin url
    # goes with its message too — the helper reads the last
    # path segment, which is why it takes the url verbatim
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"],
                            image_url="http://localhost/api/uploads/nuotrauka.jpg")

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert captured_uploads == ["http://localhost/api/uploads/nuotrauka.jpg"]


def test_a_helper_that_raises_a_lookup_error_cannot_fail_the_unsend(client, db, room, monkeypatch):
    # Only ImportError is swallowed silently; every other
    # exception goes through the logging arm and the unsend
    # still stands
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], image_url="/api/uploads/sprogs.jpg")

    def _boom(path):
        raise LookupError("the uploads index is gone")

    from app.uploads import routes as uploads_routes
    monkeypatch.setattr(uploads_routes, "delete_upload", _boom)

    response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert response.status_code == 200
    assert _message_row(db, msg_id)["deleted_at"] is not None


def test_an_uploads_package_without_the_helper_still_unsends_and_broadcasts(client, db, room,
                                                                           chat_events,
                                                                           monkeypatch):
    # The documented fallback: while the shared helper is
    # missing the from-import is an ImportError, swallowed
    # without a log line — and everything else still runs
    conv_id, user, headers, _, _, _ = room
    msg_id = _plant_message(db, conv_id, user["id"], image_url="/api/uploads/be-pagalbininko.jpg")
    seen = []
    monkeypatch.setattr(chat_events, "emit_message_deleted",
                        lambda sio, cid, mid: seen.append(mid))
    from app.uploads import routes as uploads_routes
    monkeypatch.delattr(uploads_routes, "delete_upload")

    response = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert response.status_code == 200
    assert _message_row(db, msg_id)["image_url"] is None
    assert seen == [msg_id]


def test_the_photo_file_and_its_uploads_row_go_with_the_message(client, app, db, room):
    # The real helper, not a fake: the uploads package owns
    # names of 32 hex characters plus an image extension
    conv_id, user, headers, _, _, _ = room
    name = f"{uuid.uuid4().hex}.jpg"
    stored = os.path.join(app.config["UPLOAD_DIR"], name)
    with open(stored, "wb") as handle:
        handle.write(b"\xff\xd8\xff\xdb")
    db.execute("INSERT INTO uploads (id, filename, user_id, byte_size, created_at)"
               " VALUES (?, ?, ?, ?, ?)",
               (str(uuid.uuid4()), name, user["id"], 4, _stamp(1)))
    db.commit()
    msg_id = _plant_message(db, conv_id, user["id"], image_url=f"/api/uploads/{name}")

    client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)

    assert not os.path.exists(stored)
    assert db.execute("SELECT COUNT(*) FROM uploads WHERE filename = ?",
                      (name,)).fetchone()[0] == 0




# ===========================================================
# DELETE .../messages/<mid> — the 100-per-5-min quota
# ===========================================================


def test_the_hundredth_unsend_lands_and_the_next_one_is_refused(client, db, room):
    conv_id, user, headers, _, _, msg_id = room
    second = _plant_message(db, conv_id, user["id"], text="antra", created_at=_stamp(7))
    _fill_bucket("chat_delete", user["id"], 99)

    hundredth = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", headers=headers)
    over = client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{second}", headers=headers)

    assert hundredth.status_code == 200
    assert over.status_code == 429
    assert over.get_json()["code"] == "rate_limited"
    assert int(over.headers["Retry-After"]) >= 1
    assert _message_row(db, second)["deleted_at"] is None, "the refused call wrote nothing"


def test_a_refused_unsend_still_spends_a_slot_of_the_quota(client, db, room):
    # The limiter sits under require_auth and above every gate,
    # so a 404 costs exactly what a successful unsend costs
    conv_id, user, headers, _, _, msg_id = room
    _fill_bucket("chat_delete", user["id"], 99)

    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/nera-tokios",
                         headers=headers).status_code == 404
    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}",
                         headers=headers).status_code == 429


def test_a_quota_that_has_aged_out_of_the_window_admits_the_unsend(client, db, room):
    # 100 stamps, all older than the 300 s window — pruned on
    # read, so the budget is whole again without any sleeping
    conv_id, user, headers, _, _, msg_id = room
    _fill_bucket("chat_delete", user["id"], 100, age=301)

    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}",
                         headers=headers).status_code == 200


def test_one_members_exhausted_quota_does_not_touch_another(client, db, room):
    conv_id, user, headers, other, other_headers, _ = room
    theirs = _plant_message(db, conv_id, other["id"], text="jų", created_at=_stamp(8))
    _fill_bucket("chat_delete", user["id"], 100)

    assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{theirs}",
                         headers=other_headers).status_code == 200




# ===========================================================
# PUT /conversations/<id>/pin
# ===========================================================


def test_pinning_without_a_token_is_401(client, db, actor):
    user, _ = actor
    conv_id = _plant_conversation(db, [user["id"]])

    response = client.put(f"{CONVERSATIONS}/{conv_id}/pin")

    assert response.status_code == 401
    assert _pin_row(db, conv_id, user["id"])["pinned"] == 0


def test_pinning_a_conversation_that_does_not_exist_is_a_403(client, actor):
    # rowcount 0 is the WHOLE gate — the route never looks at
    # the conversations table, so an unknown id and an
    # outsider's id are the same answer
    _, headers = actor

    response = client.put(f"{CONVERSATIONS}/nera-tokio/pin", headers=headers)

    assert response.status_code == 403
    assert response.get_json()["error"] == "Not a participant"


def test_a_membership_row_whose_conversation_is_gone_can_still_be_pinned(client, db, actor):
    # The contrast with leave_conversation, which starts from
    # the conversations table and answers 404 for this very row
    user, headers = actor
    db.execute("INSERT INTO conversation_participants (conversation_id, user_id, pinned)"
               " VALUES (?, ?, 0)", ("conv-nasle", user["id"]))
    db.commit()

    response = client.put(f"{CONVERSATIONS}/conv-nasle/pin", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"pinned": True}
    assert client.delete(f"{CONVERSATIONS}/conv-nasle", headers=headers).status_code == 404


def test_the_flip_is_written_to_the_membership_row(client, db, actor, make_user):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)
    assert _pin_row(db, conv_id, user["id"])["pinned"] == 1

    client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)
    assert _pin_row(db, conv_id, user["id"])["pinned"] == 0


def test_an_odd_number_of_toggles_ends_pinned(client, db, actor, make_user):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    states = [client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers).get_json()["pinned"]
              for _ in range(5)]

    assert states == [True, False, True, False, True]
    assert _pin_row(db, conv_id, user["id"])["pinned"] == 1


def test_the_pinned_field_is_a_real_json_boolean(client, db, actor, make_user):
    # bool(row["pinned"]) — a 1 on the wire would still compare
    # equal in Python but breaks the mobile ApiConversation type
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    body = client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers).get_json()

    assert isinstance(body["pinned"], bool)
    assert list(body) == ["pinned"]


def test_a_pin_never_moves_the_other_members_row(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]], pinned_for=[other["id"]])

    client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)

    assert _pin_row(db, conv_id, user["id"])["pinned"] == 1
    assert _pin_row(db, conv_id, other["id"])["pinned"] == 1, "the other member's pin is their own"

    client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=auth_headers(other))

    assert _pin_row(db, conv_id, user["id"])["pinned"] == 1
    assert _pin_row(db, conv_id, other["id"])["pinned"] == 0


def test_pinning_leaves_the_rest_of_the_membership_row_intact(client, db, actor, make_user):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]],
                                  last_read={user["id"]: _stamp(4)})

    client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)

    assert _pin_row(db, conv_id, user["id"])["last_read_at"] == _stamp(4)


def test_pinning_does_not_reorder_the_room_for_anybody_else(client, db, actor, make_user):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])
    before = _updated_at(db, conv_id)

    client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)

    assert _updated_at(db, conv_id) == before


def test_pinning_ignores_whatever_body_it_is_given(client, db, actor, make_user):
    # The route reads no body at all: "unpin me" still toggles
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    response = client.put(f"{CONVERSATIONS}/{conv_id}/pin", json={"pinned": False},
                          headers=headers)

    assert response.get_json() == {"pinned": True}


def test_a_non_object_json_body_is_refused_before_the_flip(client, db, actor, make_user):
    # The app-wide validate_json_input hook answers first, so
    # nothing is written — a 400 must not silently pin the room
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    response = client.put(f"{CONVERSATIONS}/{conv_id}/pin", data="[1, 2]",
                          headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "JSON body must be an object"
    assert _pin_row(db, conv_id, user["id"])["pinned"] == 0


def test_a_pin_value_outside_zero_and_one_stays_truthy_on_every_flip(client, db, actor,
                                                                     make_user):
    # `1 - pinned` is arithmetic, not a NOT: only a corrupt row
    # can reach this, and it then oscillates 2 → -1 → 2, both
    # truthy. Pinned rows sort first, so it merely sticks
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])
    db.execute("UPDATE conversation_participants SET pinned = 2 WHERE conversation_id = ?"
               " AND user_id = ?", (conv_id, user["id"]))
    db.commit()

    first = client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers).get_json()
    second = client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers).get_json()

    assert first == {"pinned": True}
    assert second == {"pinned": True}
    assert _pin_row(db, conv_id, user["id"])["pinned"] == 2


def test_pinning_carries_no_quota_of_its_own(client, db, actor, make_user):
    # Unlike its sibling write routes toggle_pin has no
    # @rate_limit — a swipe-happy thumb must never be refused
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    codes = {client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers).status_code
             for _ in range(40)}

    assert codes == {200}


def test_a_pin_survives_the_counterpart_leaving(client, db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    client.put(f"{CONVERSATIONS}/{conv_id}/pin", headers=headers)

    assert client.delete(f"{CONVERSATIONS}/{conv_id}",
                         headers=auth_headers(other)).status_code == 200

    assert _pin_row(db, conv_id, user["id"])["pinned"] == 1
    rows = client.get(CONVERSATIONS, headers=headers).get_json()["conversations"]
    assert [row["pinned"] for row in rows if row["id"] == conv_id] == [True]




# ===========================================================
# DELETE /conversations/<id> — leave
# ===========================================================


def test_leaving_without_a_token_is_401(client, db, actor):
    user, _ = actor
    conv_id = _plant_conversation(db, [user["id"]])

    response = client.delete(f"{CONVERSATIONS}/{conv_id}")

    assert response.status_code == 401
    assert _members_of(db, conv_id) == [user["id"]]


def test_the_unknown_room_answer_comes_before_the_membership_check(client, actor):
    _, headers = actor

    response = client.delete(f"{CONVERSATIONS}/nera-tokio", headers=headers)

    assert response.status_code == 404
    assert response.get_json()["error"] == "Conversation not found"


def test_a_room_with_no_members_left_is_not_purged_by_a_strangers_call(client, db, actor):
    # The purge only ever runs after a leave the caller was
    # entitled to — a 403 must not sweep the room away
    _, headers = actor
    conv_id = _plant_conversation(db, [])

    response = client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    assert response.status_code == 403
    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?",
                      (conv_id,)).fetchone()[0] == 1


def test_leaving_ignores_a_json_body(client, db, actor, make_user):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    response = client.delete(f"{CONVERSATIONS}/{conv_id}", json={"purge": True}, headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_a_non_object_json_body_cannot_leave_the_room(client, db, actor, make_user):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    response = client.delete(f"{CONVERSATIONS}/{conv_id}", data="[1, 2]",
                             headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400
    assert user["id"] in _members_of(db, conv_id)


def test_the_solo_purge_takes_the_messages_receipts_and_reactions_with_it(client, db, actor,
                                                                          make_user):
    # The room is down to one member; everything the departed
    # crowd left behind goes in the same transaction
    user, headers = actor
    ghost = make_user()
    conv_id = _plant_conversation(db, [user["id"]])
    msg_id = _plant_message(db, conv_id, ghost["id"], text="senos kalbos")
    _plant_read(db, msg_id, ghost["id"])
    _plant_reaction(db, msg_id, ghost["id"])

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200

    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?",
                      (conv_id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (msg_id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM message_reads WHERE message_id = ?",
                      (msg_id,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM message_reactions WHERE message_id = ?",
                      (msg_id,)).fetchone()[0] == 0


def test_the_purge_clears_the_search_index_too(client, db, actor):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]])
    _plant_message(db, conv_id, user["id"], text="istrinamas raktazodis")
    if _fts_hits(db, "istrinamas") is None:
        pytest.skip("this SQLite has no FTS5, so migration v20 left no index to check")
    assert _fts_hits(db, "istrinamas")

    client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    assert _fts_hits(db, "istrinamas") == []


def test_the_purge_leaves_the_photo_file_for_the_orphan_sweep(client, app, db, actor):
    # Documented, not accidental: the leave path deletes rows
    # only, and uploads/sweep_orphan_uploads owns the blobs
    user, headers = actor
    name = f"{uuid.uuid4().hex}.jpg"
    stored = os.path.join(app.config["UPLOAD_DIR"], name)
    with open(stored, "wb") as handle:
        handle.write(b"\xff\xd8\xff\xdb")
    conv_id = _plant_conversation(db, [user["id"]])
    _plant_message(db, conv_id, user["id"], image_url=f"/api/uploads/{name}")

    client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    assert os.path.exists(stored)


def test_a_purge_of_two_hundred_messages_leaves_nothing_behind(client, db, actor):
    # No cap, no paging: one DELETE statement carries the whole
    # history out
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"]])
    db.executemany(
        "INSERT INTO messages (id, conversation_id, sender_id, text, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        [(f"msg-{i:04d}", conv_id, user["id"], f"eilute {i}", _stamp(i)) for i in range(200)],
    )
    db.commit()

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200

    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?",
                      (conv_id,)).fetchone()[0] == 0


def test_the_purge_never_reaches_another_rooms_messages(client, db, actor, make_user):
    user, headers = actor
    leaving = _plant_conversation(db, [user["id"]])
    keeping = _plant_conversation(db, [user["id"], make_user()["id"]])
    kept = _plant_message(db, keeping, user["id"], text="lieka")
    _plant_message(db, leaving, user["id"], text="dingsta")

    client.delete(f"{CONVERSATIONS}/{leaving}", headers=headers)

    assert db.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (kept,)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?",
                      (keeping,)).fetchone()[0] == 1


def test_leaving_keeps_the_other_members_receipts_on_the_leavers_messages(client, db, actor,
                                                                          make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    msg_id = _plant_message(db, conv_id, user["id"], text="mano")
    _plant_read(db, msg_id, user["id"])
    _plant_read(db, msg_id, other["id"])

    client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    readers = [r[0] for r in db.execute("SELECT user_id FROM message_reads WHERE message_id = ?",
                                        (msg_id,))]
    assert readers == [other["id"]]


def test_every_socket_of_the_leaver_leaves_the_room_and_nobody_elses_does(client, db, actor,
                                                                          make_user, chat_events,
                                                                          chat_routes, monkeypatch):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    evictions = []
    monkeypatch.setattr(chat_routes, "leave_room",
                        lambda room, sid=None, namespace=None: evictions.append(
                            (room, sid, namespace)))
    monkeypatch.setitem(chat_events._connected_users, "sid-phone", user["id"])
    monkeypatch.setitem(chat_events._connected_users, "sid-laptop", user["id"])
    monkeypatch.setitem(chat_events._connected_users, "sid-theirs", other["id"])

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200

    assert sorted(evictions) == [
        (f"conv:{conv_id}", "sid-laptop", "/"),
        (f"conv:{conv_id}", "sid-phone", "/"),
    ]


def test_an_empty_presence_map_evicts_nothing_and_still_answers_ok(client, db, actor, make_user,
                                                                   chat_events, chat_routes,
                                                                   monkeypatch):
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])
    evictions = []
    monkeypatch.setattr(chat_routes, "leave_room", lambda *a, **k: evictions.append(a))
    monkeypatch.setattr(chat_events, "_connected_users", {})

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    assert evictions == []


def test_a_presence_map_that_cannot_even_be_imported_cannot_fail_the_leave(client, db, actor,
                                                                           make_user, chat_events,
                                                                           monkeypatch):
    # The whole eviction block sits under one try/except: the
    # membership row is already committed when it runs
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])
    monkeypatch.delattr(chat_events, "_connected_users")

    response = client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert _members_of(db, conv_id) != [user["id"]]


def test_a_raising_room_manager_is_swallowed_after_the_commit(client, db, actor, make_user,
                                                              chat_events, chat_routes,
                                                              monkeypatch):
    # leave_room raising aborts the eviction loop — the leave
    # itself is committed either way, which is the contract
    # that matters, and the socket follows on its reconnect
    user, headers = actor
    conv_id = _plant_conversation(db, [user["id"], make_user()["id"]])

    def _boom(room, sid=None, namespace=None):
        raise RuntimeError("room manager is down")

    monkeypatch.setattr(chat_routes, "leave_room", _boom)
    monkeypatch.setitem(chat_events._connected_users, "sid-phone", user["id"])

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    assert _members_of(db, conv_id) != [user["id"]]


def test_leaving_carries_no_quota_of_its_own(client, db, actor, make_user):
    # Twenty rooms, twenty leaves — no @rate_limit on this route
    user, headers = actor
    rooms = [_plant_conversation(db, [user["id"], make_user()["id"]]) for _ in range(20)]

    codes = {client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code
             for conv_id in rooms}

    assert codes == {200}


def test_leaving_the_same_room_twice_in_a_row_cannot_purge_a_neighbour(client, db, actor,
                                                                        make_user):
    user, headers = actor
    other = make_user()
    conv_id = _plant_conversation(db, [user["id"], other["id"]])
    neighbour = _plant_conversation(db, [user["id"], other["id"]])

    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 200
    assert client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers).status_code == 403

    assert _members_of(db, neighbour) == sorted([user["id"], other["id"]])
