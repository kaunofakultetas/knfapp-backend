# -----------------------------------------------------------
#  [*] Tests — account erasure, data export, privileged codes
#
#  The GDPR layer end to end:
#
#    - DELETE /api/auth/me — the password confirm (right,
#      wrong, missing, budget), the last-active-admin guard,
#      and the erasure itself: sessions dead, profile
#      anonymised and 404ing, authored posts tombstoned,
#      likes/votes gone WITH their denormalised counters,
#      friendships/requests/blocks/memberships deleted,
#      uploaded files off the disk, login impossible.
#    - GET /api/auth/me/export — the document's sections and
#      that it contains the caller's rows, not anyone else's.
#    - DELETE /api/admin/users/<id> — role gate, self-delete
#      redirect, unknown target, last-admin continuity, the
#      audit row, and that the erasure ran.
#    - POST /api/admin/invitations — admin/curator codes are
#      single-use and ≤72h explicit, with the omitted-expiry
#      default clamped instead of rejected.
#
#  Rule 10 of TESTPLAN.md applies: nothing here asserts raw
#  wire bytes, so plain json= is fine throughout.
# -----------------------------------------------------------

import uuid

import pytest


ME = "/api/auth/me"
EXPORT = "/api/auth/me/export"
ADMIN_USERS = "/api/admin/users"
INVITATIONS = "/api/admin/invitations"

PASSWORD = "slaptazodis123"




# -----------------------------------------------------------
# fixtures / helpers
# -----------------------------------------------------------
#
# populated — one user with a footprint in every table the
# erasure touches: a post with a like from someone else AND
# a like BY them on someone else's post, a comment, a
# message, a friendship, a pending request, a block, an
# upload on disk, a session, a push token, a channel row.
#
# Used by:
#   - the erasure sections below
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_upload_dir():
    # The uploads package caches its directory per process (see
    # test_uploads.py) — reset so THIS test's app resolves its
    # own tmp_path
    from app.uploads import routes as uploads_routes
    uploads_routes._upload_dir = None
    yield
    uploads_routes._upload_dir = None


@pytest.fixture
def populated(app, db, make_user, auth_headers, tmp_path):
    # All three users first: make_user writes on its own
    # connection, which deadlocks against this fixture's later
    # uncommitted inserts on `db` if interleaved
    user = make_user(display_name="Vardenis Pavardenis")
    other = make_user(display_name="Ona Onaitė")
    third = make_user()
    headers = auth_headers(user)
    other_headers = auth_headers(other)

    # An upload on disk, owned by the user — the same directory
    # the app fixture pointed UPLOAD_DIR at
    upload_dir = tmp_path / "uploads"
    filename = f"{uuid.uuid4().hex}.jpg"
    (upload_dir / filename).write_bytes(b"fake-jpeg")
    db.execute(
        "INSERT INTO uploads (id, filename, user_id, byte_size) VALUES (?, ?, ?, 9)",
        (uuid.uuid4().hex, filename, user["id"]),
    )

    # Their post (with their upload as cover), the other's post
    my_post = str(uuid.uuid4())
    their_post = str(uuid.uuid4())
    db.execute(
        "INSERT INTO news_posts (id, title, content, author_id, author_name, source, post_type, image_url)"
        " VALUES (?, 'Mano', 'Tekstas', ?, 'Vardenis Pavardenis', 'user', 'social', ?)",
        (my_post, user["id"], f"/api/uploads/{filename}"),
    )
    db.execute(
        "INSERT INTO news_posts (id, title, content, author_id, author_name, source, post_type, likes_count)"
        " VALUES (?, 'Kito', 'Tekstas', ?, 'Ona Onaitė', 'user', 'social', 1)",
        (their_post, other["id"]),
    )

    # Their like on the other's post (counted), a comment, a poll vote
    db.execute("INSERT INTO news_likes (user_id, post_id) VALUES (?, ?)", (user["id"], their_post))
    db.execute(
        "INSERT INTO news_comments (id, post_id, user_id, text) VALUES (?, ?, ?, 'Puiku')",
        (str(uuid.uuid4()), their_post, user["id"]),
    )
    poll_id = str(uuid.uuid4())
    option_id = str(uuid.uuid4())
    db.execute("INSERT INTO polls (id, post_id, title) VALUES (?, ?, 'Ar?')", (poll_id, their_post))
    db.execute(
        "INSERT INTO poll_options (id, poll_id, text, votes) VALUES (?, ?, 'Taip', 1)",
        (option_id, poll_id),
    )
    db.execute(
        "INSERT INTO poll_votes (user_id, poll_id, option_id) VALUES (?, ?, ?)",
        (user["id"], poll_id, option_id),
    )

    # A conversation with one message from them
    conv_id = str(uuid.uuid4())
    db.execute("INSERT INTO conversations (id, type, created_by) VALUES (?, 'direct', ?)", (conv_id, user["id"]))
    for uid in (user["id"], other["id"]):
        db.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
            (conv_id, uid),
        )
    msg_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text) VALUES (?, ?, ?, 'Labas')",
        (msg_id, conv_id, user["id"]),
    )

    # Social graph rows in both directions
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (user["id"], other["id"]))
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (other["id"], user["id"]))
    db.execute(
        "INSERT INTO friend_requests (id, from_user_id, to_user_id, status) VALUES (?, ?, ?, 'pending')",
        (str(uuid.uuid4()), third["id"], user["id"]),
    )
    db.execute(
        "INSERT INTO user_blocks (blocker_id, blocked_id) VALUES (?, ?)",
        (user["id"], third["id"]),
    )

    # Device rows
    db.execute(
        "INSERT INTO push_tokens (user_id, token) VALUES (?, 'ExponentPushToken[erasure-test-1]')",
        (user["id"],),
    )
    db.execute(
        "INSERT INTO notification_channels (user_id, channel, enabled) VALUES (?, 'chat', 0)",
        (user["id"],),
    )
    db.commit()

    return {
        "user": user, "headers": headers,
        "other": other, "other_headers": other_headers,
        "my_post": my_post, "their_post": their_post,
        "conv_id": conv_id, "msg_id": msg_id,
        "poll_option": option_id,
        "upload_path": upload_dir / filename,
    }


def _delete_me(client, headers, password=PASSWORD):
    return client.delete(ME, json={"password": password}, headers=headers)




# -----------------------------------------------------------
# DELETE /api/auth/me — the confirm and the guards
# -----------------------------------------------------------

class TestDeleteMeGuards:

    def test_requires_auth_and_body(self, client, actor):
        _, headers = actor
        assert client.delete(ME, json={"password": PASSWORD}).status_code == 401
        assert client.delete(ME, json={}, headers=headers).status_code == 400

    def test_wrong_password_is_400_and_erases_nothing(self, client, db, actor):
        user, headers = actor
        resp = _delete_me(client, headers, password="neteisingas000")
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "invalid_credentials"
        row = db.execute("SELECT username, active FROM users WHERE id = ?", (user["id"],)).fetchone()
        assert row["username"] == user["username"]
        assert row["active"] == 1

    def test_wrong_passwords_burn_the_shared_budget(self, client, actor):
        _, headers = actor
        for _ in range(12):
            resp = _delete_me(client, headers, password="neteisingas000")
            if resp.status_code == 429:
                break
        else:
            pytest.fail("the attempt budget never engaged")

    def test_last_active_admin_cannot_erase_themselves(self, client, admin):
        admin_user, admin_headers = admin
        resp = _delete_me(client, admin_headers, password=admin_user["password"])
        assert resp.status_code == 400
        assert "admin" in resp.get_json()["error"].lower()




# -----------------------------------------------------------
# DELETE /api/auth/me — the erasure itself
# -----------------------------------------------------------

class TestErasure:

    def test_the_whole_footprint(self, client, db, populated):
        p = populated
        user_id = p["user"]["id"]

        assert _delete_me(client, p["headers"]).status_code == 200

        # The session died with the erasure — the token is gone
        assert client.get(ME, headers=p["headers"]).status_code == 401

        # Login is impossible: credentials anonymised
        assert client.post("/api/auth/login", json={
            "username": p["user"]["username"], "password": PASSWORD,
        }).status_code in (400, 401)

        # The users row survives, anonymised
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        assert row["username"] == f"deleted-{user_id}"
        assert row["email"] == f"deleted-{user_id}@deleted.invalid"
        assert row["display_name"] == "Ištrintas naudotojas"
        assert row["active"] == 0
        assert row["avatar_url"] is None
        assert row["student_number"] is None

        # The profile reads as gone to everyone (active = 0)
        assert client.get(f"/api/social/profile/{user_id}").status_code == 404

        # Authored post: kept, tombstoned, cover reference nulled
        post = db.execute("SELECT author_name, image_url FROM news_posts WHERE id = ?",
                          (p["my_post"],)).fetchone()
        assert post["author_name"] == "Ištrintas naudotojas"
        assert post["image_url"] is None

        # The upload left the disk and the table
        assert not p["upload_path"].exists()
        assert db.execute("SELECT 1 FROM uploads WHERE user_id = ?", (user_id,)).fetchone() is None

        # Like + vote rows gone AND their counters decremented
        assert db.execute("SELECT 1 FROM news_likes WHERE user_id = ?", (user_id,)).fetchone() is None
        assert db.execute("SELECT likes_count FROM news_posts WHERE id = ?",
                          (p["their_post"],)).fetchone()["likes_count"] == 0
        assert db.execute("SELECT 1 FROM poll_votes WHERE user_id = ?", (user_id,)).fetchone() is None
        assert db.execute("SELECT votes FROM poll_options WHERE id = ?",
                          (p["poll_option"],)).fetchone()["votes"] == 0

        # The message survives for the counterpart, the sender
        # resolves to the marker at read time
        assert db.execute("SELECT text FROM messages WHERE id = ?",
                          (p["msg_id"],)).fetchone()["text"] == "Labas"

        # The graph and device rows are gone, both directions
        for sql in (
            "SELECT 1 FROM friendships WHERE user_id = ? OR friend_id = ?",
            "SELECT 1 FROM friend_requests WHERE from_user_id = ? OR to_user_id = ?",
            "SELECT 1 FROM user_blocks WHERE blocker_id = ? OR blocked_id = ?",
        ):
            assert db.execute(sql, (user_id, user_id)).fetchone() is None
        for sql in (
            "SELECT 1 FROM sessions WHERE user_id = ?",
            "SELECT 1 FROM push_tokens WHERE user_id = ?",
            "SELECT 1 FROM notification_channels WHERE user_id = ?",
            "SELECT 1 FROM conversation_participants WHERE user_id = ?",
        ):
            assert db.execute(sql, (user_id,)).fetchone() is None

    def test_counterpart_still_reads_the_conversation(self, client, populated):
        p = populated
        assert _delete_me(client, p["headers"]).status_code == 200
        resp = client.get(f"/api/chat/conversations/{p['conv_id']}/messages",
                          headers=p["other_headers"])
        assert resp.status_code == 200
        texts = [m["text"] for m in resp.get_json()["messages"]]
        assert "Labas" in texts




# -----------------------------------------------------------
# GET /api/auth/me/export
# -----------------------------------------------------------

class TestExport:

    def test_document_sections_and_ownership(self, client, populated):
        p = populated
        body = client.get(EXPORT, headers=p["headers"]).get_json()

        assert body["profile"]["id"] == p["user"]["id"]
        assert "password_hash" not in body["profile"]
        assert [post["id"] for post in body["posts"]] == [p["my_post"]]
        assert len(body["comments"]) == 1
        assert [m["id"] for m in body["messages"]] == [p["msg_id"]]
        assert [c["id"] for c in body["conversations"]] == [p["conv_id"]]
        assert len(body["likes"]) == 1
        assert len(body["pollVotes"]) == 1
        assert len(body["friends"]) == 1
        assert len(body["friendRequests"]) == 1
        assert len(body["blocks"]) == 1
        assert len(body["uploads"]) == 1
        assert body["notificationChannels"][0]["channel"] == "chat"

        # The OTHER side's export carries none of it
        other_body = client.get(EXPORT, headers=p["other_headers"]).get_json()
        assert other_body["posts"] and other_body["posts"][0]["id"] == p["their_post"]
        assert other_body["messages"] == []

    def test_requires_auth(self, client):
        assert client.get(EXPORT).status_code == 401




# -----------------------------------------------------------
# DELETE /api/admin/users/<id>
# -----------------------------------------------------------

class TestAdminDelete:

    def test_erases_and_audits(self, client, db, populated, admin):
        p = populated
        _, admin_headers = admin
        resp = client.delete(f"{ADMIN_USERS}/{p['user']['id']}", headers=admin_headers)
        assert resp.status_code == 200

        row = db.execute("SELECT username, active FROM users WHERE id = ?",
                         (p["user"]["id"],)).fetchone()
        assert row["username"] == f"deleted-{p['user']['id']}"
        assert row["active"] == 0
        assert db.execute(
            "SELECT 1 FROM admin_audit WHERE action = 'user.delete' AND target = ?",
            (p["user"]["id"],),
        ).fetchone() is not None

    def test_gates(self, client, actor, admin, make_user):
        user, headers = actor
        admin_user, admin_headers = admin
        # Students bounce off the role gate
        assert client.delete(f"{ADMIN_USERS}/{user['id']}", headers=headers).status_code == 403
        # Unknown target
        assert client.delete(f"{ADMIN_USERS}/no-such", headers=admin_headers).status_code == 404
        # Self-deletion goes through /api/auth/me instead
        assert client.delete(f"{ADMIN_USERS}/{admin_user['id']}",
                             headers=admin_headers).status_code == 400

    def test_last_active_admin_survives(self, client, db, admin, make_user, auth_headers):
        admin_user, _ = admin
        second = make_user(role="admin")
        second_headers = auth_headers(second)
        # Deactivate the seeded admin so `second` is the last one standing
        db.execute("UPDATE users SET active = 0 WHERE id = ?", (admin_user["id"],))
        db.commit()
        third = make_user(role="admin")
        # `second` may erase `third`... but then nobody may erase `second`
        assert client.delete(f"{ADMIN_USERS}/{third['id']}", headers=second_headers).status_code == 200
        db.execute("UPDATE users SET active = 1 WHERE id = ?", (admin_user["id"],))
        db.commit()
        admin_headers = auth_headers(admin_user)
        db.execute("UPDATE users SET active = 0 WHERE id = ?", (admin_user["id"],))
        db.commit()
        resp = client.delete(f"{ADMIN_USERS}/{second['id']}", headers=admin_headers)
        assert resp.status_code in (400, 401)




# -----------------------------------------------------------
# Privileged invitation codes
# -----------------------------------------------------------

class TestPrivilegedInvitations:

    def _mint(self, client, headers, **body):
        return client.post(INVITATIONS, json=body, headers=headers)

    @pytest.mark.parametrize("role", ["admin", "curator"])
    def test_multi_use_is_rejected(self, client, admin, role):
        _, headers = admin
        resp = self._mint(client, headers, role=role, max_uses=5)
        assert resp.status_code == 400
        assert "single-use" in resp.get_json()["error"]

    @pytest.mark.parametrize("role", ["admin", "curator"])
    def test_explicit_long_expiry_is_rejected(self, client, admin, role):
        _, headers = admin
        resp = self._mint(client, headers, role=role, expires_hours=720)
        assert resp.status_code == 400
        assert "72" in resp.get_json()["error"]

    def test_omitted_expiry_is_clamped_not_rejected(self, client, db, admin):
        _, headers = admin
        resp = self._mint(client, headers, role="admin")
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["maxUses"] == 1
        # The stored expiry sits within the 72h cap even though
        # the config default (168h) is longer
        from datetime import datetime, timedelta, timezone
        expires = datetime.fromisoformat(body["expiresAt"])
        cap = datetime.now(timezone.utc) + timedelta(hours=72, minutes=5)
        assert expires <= cap

    def test_explicit_short_expiry_still_mints(self, client, admin):
        _, headers = admin
        assert self._mint(client, headers, role="curator",
                          expires_hours=24).status_code == 201

    def test_student_codes_keep_the_general_bounds(self, client, admin):
        _, headers = admin
        assert self._mint(client, headers, role="student", max_uses=100,
                          expires_hours=720).status_code == 201
