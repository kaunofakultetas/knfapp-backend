# -----------------------------------------------------------
#  [*] Tests — blocks, reports and push-preview privacy (v56)
#
#  The abuse-handling layer end to end:
#
#    - POST/DELETE/GET /api/social/blocks — the block pair
#      itself: validation, idempotency, the severing of an
#      existing friendship and pending requests, the list.
#    - The enforcement sites: friend requests answer 404 for
#      a blocked pair (either direction), chat conversation
#      creation answers 403 (direct AND group), an existing
#      direct room refuses sends both ways, the chat user
#      search hides both halves of a pair from each other,
#      and the push fan-out keeps a blocked pair's phones
#      quiet inside a group without touching the room.
#    - GET /api/social/profile/<id> carries blockedByMe so
#      the client can offer "unblock".
#    - GET/PUT /api/notifications/chat-preview and the
#      fan-out split it drives: preview-off recipients get
#      the content-free body, preview-on recipients the
#      text, in two separate pushes.
#    - POST /api/social/reports + the admin list/resolve
#      pair, including the audit row.
#
#  Rule 10 of TESTPLAN.md applies: nothing here asserts raw
#  wire bytes, so plain json= is fine throughout.
# -----------------------------------------------------------

import uuid

import pytest


BLOCKS = "/api/social/blocks"
REPORTS = "/api/social/reports"
ADMIN_REPORTS = "/api/admin/reports"
CHAT_PREVIEW = "/api/notifications/chat-preview"
CONVERSATIONS = "/api/chat/conversations"




# -----------------------------------------------------------
# fixtures
# -----------------------------------------------------------
#
# pair — two strangers (A, B) with headers for both.
# chat_routes / no_push — the same push-capture pattern the
# chat suites use: start_background_task is swallowed and its
# calls handed back for assertions.
#
# Used by:
#   - every section below
# -----------------------------------------------------------

@pytest.fixture
def chat_routes(app):
    from app.chat import routes
    return routes


@pytest.fixture
def no_push(chat_routes, monkeypatch):
    calls = []
    monkeypatch.setattr(chat_routes._get_socketio(), "start_background_task",
                        lambda func, *args, **kwargs: calls.append((func, args, kwargs)))
    return calls


@pytest.fixture
def pair(make_user, auth_headers):
    a = make_user(display_name="Asta Astaitė")
    b = make_user(display_name="Benas Benaitis")
    return a, auth_headers(a), b, auth_headers(b)


def _block(client, headers, target_id):
    return client.post(BLOCKS, json={"user_id": target_id}, headers=headers)


def _plant_friendship(db, a_id, b_id):
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (a_id, b_id))
    db.execute("INSERT INTO friendships (user_id, friend_id) VALUES (?, ?)", (b_id, a_id))
    db.commit()


def _plant_direct(db, a_id, b_id):
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"
    db.execute(
        "INSERT INTO conversations (id, type, created_by) VALUES (?, 'direct', ?)",
        (conv_id, a_id),
    )
    for uid in (a_id, b_id):
        db.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id) VALUES (?, ?)",
            (conv_id, uid),
        )
    db.commit()
    return conv_id




# -----------------------------------------------------------
# The block endpoint itself
# -----------------------------------------------------------

class TestBlockEndpoint:

    def test_block_creates_row_and_answers_blocked(self, client, db, pair):
        a, ha, b, _ = pair
        resp = _block(client, ha, b["id"])
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "blocked"
        row = db.execute(
            "SELECT 1 FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?",
            (a["id"], b["id"]),
        ).fetchone()
        assert row is not None

    def test_repeat_block_is_idempotent_200(self, client, pair):
        _, ha, b, _ = pair
        assert _block(client, ha, b["id"]).status_code == 200
        assert _block(client, ha, b["id"]).status_code == 200

    def test_self_block_is_400(self, client, pair):
        a, ha, _, _ = pair
        assert _block(client, ha, a["id"]).status_code == 400

    def test_unknown_target_is_404(self, client, pair):
        _, ha, _, _ = pair
        assert _block(client, ha, "no-such-user").status_code == 404

    def test_deactivated_target_is_404(self, client, make_user, pair):
        _, ha, _, _ = pair
        ghost = make_user(active=0)
        assert _block(client, ha, ghost["id"]).status_code == 404

    def test_missing_and_nonstring_user_id_are_400(self, client, pair):
        _, ha, _, _ = pair
        assert client.post(BLOCKS, json={}, headers=ha).status_code == 400
        assert client.post(BLOCKS, json={"user_id": 7}, headers=ha).status_code == 400

    def test_requires_auth(self, client, pair):
        _, _, b, _ = pair
        assert client.post(BLOCKS, json={"user_id": b["id"]}).status_code == 401

    def test_block_severs_friendship_both_directions(self, client, db, pair):
        a, ha, b, _ = pair
        _plant_friendship(db, a["id"], b["id"])
        _block(client, ha, b["id"])
        rows = db.execute(
            "SELECT 1 FROM friendships WHERE (user_id = ? AND friend_id = ?)"
            " OR (user_id = ? AND friend_id = ?)",
            (a["id"], b["id"], b["id"], a["id"]),
        ).fetchall()
        assert rows == []

    def test_block_clears_pending_requests_both_directions(self, client, db, pair):
        a, ha, b, _ = pair
        db.execute(
            "INSERT INTO friend_requests (id, from_user_id, to_user_id, status)"
            " VALUES ('fr-1', ?, ?, 'pending')",
            (b["id"], a["id"]),
        )
        db.commit()
        _block(client, ha, b["id"])
        left = db.execute(
            "SELECT 1 FROM friend_requests WHERE status = 'pending' AND"
            " ((from_user_id = ? AND to_user_id = ?) OR (from_user_id = ? AND to_user_id = ?))",
            (a["id"], b["id"], b["id"], a["id"]),
        ).fetchall()
        assert left == []




# -----------------------------------------------------------
# Unblock + the list
# -----------------------------------------------------------

class TestUnblockAndList:

    def test_unblock_removes_row_and_repeat_is_200(self, client, db, pair):
        a, ha, b, _ = pair
        _block(client, ha, b["id"])
        resp = client.delete(f"{BLOCKS}/{b['id']}", headers=ha)
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "unblocked"
        assert db.execute(
            "SELECT 1 FROM user_blocks WHERE blocker_id = ?", (a["id"],)
        ).fetchone() is None
        # Idempotent: an id that is (no longer) blocked is the same 200
        assert client.delete(f"{BLOCKS}/{b['id']}", headers=ha).status_code == 200

    def test_list_shows_own_blocks_only_with_profile_fields(self, client, pair, make_user, auth_headers):
        a, ha, b, hb = pair
        c = make_user(display_name="Cilė Cilaitė")
        _block(client, ha, b["id"])
        _block(client, auth_headers(c), b["id"])

        listing = client.get(BLOCKS, headers=ha).get_json()["blocked"]
        assert [r["id"] for r in listing] == [b["id"]]
        row = listing[0]
        assert row["displayName"] == "Benas Benaitis"
        assert set(row) == {"id", "username", "displayName", "avatarUrl", "role", "blockedAt"}

        # The blocked side's own list is empty — a block is invisible to its target
        assert client.get(BLOCKS, headers=hb).get_json()["blocked"] == []

    def test_list_requires_auth(self, client):
        assert client.get(BLOCKS).status_code == 401




# -----------------------------------------------------------
# Enforcement: friend requests
# -----------------------------------------------------------

class TestFriendRequestGate:

    def test_blocked_pair_cannot_request_either_direction(self, client, pair):
        _, ha, b, hb = pair
        a = pair[0]
        _block(client, ha, b["id"])
        # The blocker asking their own blockee — same flat 404
        assert client.post("/api/social/friends/request",
                           json={"user_id": b["id"]}, headers=ha).status_code == 404
        # The blocked side asking back — 404, not a hint that a block exists
        assert client.post("/api/social/friends/request",
                           json={"user_id": a["id"]}, headers=hb).status_code == 404

    def test_unblock_lets_requests_flow_again(self, client, pair):
        _, ha, b, _ = pair
        _block(client, ha, b["id"])
        client.delete(f"{BLOCKS}/{b['id']}", headers=ha)
        resp = client.post("/api/social/friends/request",
                           json={"user_id": b["id"]}, headers=ha)
        assert resp.status_code in (200, 201)




# -----------------------------------------------------------
# Enforcement: the profile flag
# -----------------------------------------------------------

class TestProfileFlag:

    def test_blocked_by_me_true_for_blocker_false_for_target(self, client, pair):
        a, ha, b, hb = pair
        _block(client, ha, b["id"])
        assert client.get(f"/api/social/profile/{b['id']}", headers=ha).get_json()["blockedByMe"] is True
        # The target sees nothing unusual on the blocker's profile
        assert client.get(f"/api/social/profile/{a['id']}", headers=hb).get_json()["blockedByMe"] is False

    def test_guest_view_is_false(self, client, pair):
        _, ha, b, _ = pair
        _block(client, ha, b["id"])
        assert client.get(f"/api/social/profile/{b['id']}").get_json()["blockedByMe"] is False




# -----------------------------------------------------------
# Enforcement: conversation creation
# -----------------------------------------------------------

class TestConversationGate:

    def test_direct_refused_both_directions(self, client, pair):
        a, ha, b, hb = pair
        _block(client, ha, b["id"])
        assert client.post(CONVERSATIONS, headers=ha,
                           json={"participantIds": [b["id"]], "type": "direct"}).status_code == 403
        assert client.post(CONVERSATIONS, headers=hb,
                           json={"participantIds": [a["id"]], "type": "direct"}).status_code == 403

    def test_group_with_a_blocked_member_refused(self, client, pair, make_user):
        _, ha, b, _ = pair
        c = make_user()
        _block(client, ha, b["id"])
        resp = client.post(CONVERSATIONS, headers=ha,
                           json={"participantIds": [b["id"], c["id"]],
                                 "type": "group", "title": "Grupė"})
        assert resp.status_code == 403
        # The flat message names nobody
        assert b["id"] not in resp.get_json()["error"]

    def test_unrelated_pair_unaffected(self, client, pair, make_user, auth_headers):
        _, ha, b, _ = pair
        c = make_user()
        _block(client, ha, b["id"])
        resp = client.post(CONVERSATIONS, headers=auth_headers(c),
                           json={"participantIds": [b["id"]], "type": "direct"})
        assert resp.status_code == 201

    def test_unblock_reopens_creation(self, client, pair):
        _, ha, b, _ = pair
        _block(client, ha, b["id"])
        client.delete(f"{BLOCKS}/{b['id']}", headers=ha)
        assert client.post(CONVERSATIONS, headers=ha,
                           json={"participantIds": [b["id"]], "type": "direct"}).status_code == 201




# -----------------------------------------------------------
# Enforcement: sends in an existing direct room
# -----------------------------------------------------------

class TestExistingRoomGate:

    def test_direct_send_refused_both_ways_after_block(self, client, db, pair, no_push):
        a, ha, b, hb = pair
        conv_id = _plant_direct(db, a["id"], b["id"])
        _block(client, ha, b["id"])
        for headers in (ha, hb):
            resp = client.post(f"{CONVERSATIONS}/{conv_id}/messages",
                               json={"text": "labas"}, headers=headers)
            assert resp.status_code == 403

    def test_group_send_still_delivers(self, client, db, pair, make_user, no_push):
        a, ha, b, _ = pair
        c = make_user()
        conv_id = f"conv-{uuid.uuid4().hex[:8]}"
        db.execute("INSERT INTO conversations (id, type, title, created_by)"
                   " VALUES (?, 'group', 'Grupė', ?)", (conv_id, a["id"]))
        for uid in (a["id"], b["id"], c["id"]):
            db.execute("INSERT INTO conversation_participants (conversation_id, user_id)"
                       " VALUES (?, ?)", (conv_id, uid))
        db.commit()
        _block(client, ha, b["id"])
        resp = client.post(f"{CONVERSATIONS}/{conv_id}/messages",
                           json={"text": "visiems labas"}, headers=ha)
        assert resp.status_code == 201

        # ...but the push fan-out skipped the blocked half: only C's
        # phone is on the recipient list
        assert len(no_push) == 1
        recipients = no_push[0][1][0]
        assert recipients == [c["id"]]




# -----------------------------------------------------------
# Enforcement: the user search
# -----------------------------------------------------------

class TestSearchGate:

    def test_pair_hidden_from_each_other_but_not_from_others(self, client, pair, make_user, auth_headers):
        a, ha, b, hb = pair
        c = make_user()
        _block(client, ha, b["id"])

        found_by_a = [u["id"] for u in client.get(
            f"/api/chat/users/search?q={b['username']}", headers=ha).get_json()["users"]]
        assert b["id"] not in found_by_a

        found_by_b = [u["id"] for u in client.get(
            f"/api/chat/users/search?q={a['username']}", headers=hb).get_json()["users"]]
        assert a["id"] not in found_by_b

        found_by_c = [u["id"] for u in client.get(
            f"/api/chat/users/search?q={b['username']}", headers=auth_headers(c)).get_json()["users"]]
        assert b["id"] in found_by_c




# -----------------------------------------------------------
# The push-preview setting and its fan-out split
# -----------------------------------------------------------

class TestChatPreviewSetting:

    def test_defaults_on_and_round_trips(self, client, actor):
        _, headers = actor
        assert client.get(CHAT_PREVIEW, headers=headers).get_json() == {"enabled": True}
        resp = client.put(CHAT_PREVIEW, json={"enabled": False}, headers=headers)
        assert resp.status_code == 200 and resp.get_json() == {"enabled": False}
        assert client.get(CHAT_PREVIEW, headers=headers).get_json() == {"enabled": False}

    def test_non_boolean_is_400_and_auth_required(self, client, actor):
        _, headers = actor
        assert client.put(CHAT_PREVIEW, json={"enabled": "taip"}, headers=headers).status_code == 400
        assert client.put(CHAT_PREVIEW, json={}, headers=headers).status_code == 400
        assert client.get(CHAT_PREVIEW).status_code == 401

    def test_fanout_splits_preview_on_and_off_recipients(self, client, db, pair, make_user, no_push):
        a, ha, b, _ = pair
        c = make_user()
        conv_id = f"conv-{uuid.uuid4().hex[:8]}"
        db.execute("INSERT INTO conversations (id, type, title, created_by)"
                   " VALUES (?, 'group', 'Grupė', ?)", (conv_id, a["id"]))
        for uid in (a["id"], b["id"], c["id"]):
            db.execute("INSERT INTO conversation_participants (conversation_id, user_id)"
                       " VALUES (?, ?)", (conv_id, uid))
        db.execute("UPDATE users SET chat_push_preview = 0 WHERE id = ?", (b["id"],))
        db.commit()

        resp = client.post(f"{CONVERSATIONS}/{conv_id}/messages",
                           json={"text": "slapta žinia tik mums"}, headers=ha)
        assert resp.status_code == 201

        # Two pushes: the text to C, the content-free body to B —
        # B's message text never left for Expo at all
        assert len(no_push) == 2
        by_recipients = {tuple(call[1][0]): call for call in no_push}
        full = by_recipients[(c["id"],)]
        quiet = by_recipients[(b["id"],)]
        assert full[1][2] == "slapta žinia tik mums"
        assert quiet[1][2] == "Nauja žinutė"
        assert quiet[1][3]["preview"] == "hidden"

    def test_direct_send_preview_off_gets_content_free_body(self, client, db, pair, no_push):
        a, ha, b, _ = pair
        conv_id = _plant_direct(db, a["id"], b["id"])
        db.execute("UPDATE users SET chat_push_preview = 0 WHERE id = ?", (b["id"],))
        db.commit()
        assert client.post(f"{CONVERSATIONS}/{conv_id}/messages",
                           json={"text": "labas vakaras"}, headers=ha).status_code == 201
        assert len(no_push) == 1
        assert no_push[0][1][2] == "Nauja žinutė"




# -----------------------------------------------------------
# Reports: filing
# -----------------------------------------------------------

class TestCreateReport:

    def test_report_user_happy_path(self, client, db, pair):
        a, ha, b, _ = pair
        resp = client.post(REPORTS, headers=ha, json={
            "target_type": "user", "target_id": b["id"], "reason": "Įžeidinėja žinutėmis",
        })
        assert resp.status_code == 201
        body = resp.get_json()
        assert body["status"] == "submitted"
        row = db.execute("SELECT * FROM reports WHERE id = ?", (body["id"],)).fetchone()
        assert row["reporter_id"] == a["id"]
        assert row["target_type"] == "user"
        assert row["status"] == "open"

    def test_post_and_message_targets_must_exist(self, client, db, pair, make_user):
        a, ha, b, _ = pair
        post_id = f"post-{uuid.uuid4().hex[:8]}"
        db.execute("INSERT INTO news_posts (id, title, content, source, author_id)"
                   " VALUES (?, 'T', 'C', 'user', ?)", (post_id, b["id"]))
        conv_id = _plant_direct(db, a["id"], b["id"])
        msg_id = f"msg-{uuid.uuid4().hex[:8]}"
        db.execute("INSERT INTO messages (id, conversation_id, sender_id, text)"
                   " VALUES (?, ?, ?, 'bjauru')", (msg_id, conv_id, b["id"]))
        db.commit()

        for target_type, target_id in (("post", post_id), ("message", msg_id)):
            assert client.post(REPORTS, headers=ha, json={
                "target_type": target_type, "target_id": target_id, "reason": "netinkamas turinys",
            }).status_code == 201

        assert client.post(REPORTS, headers=ha, json={
            "target_type": "post", "target_id": "no-such", "reason": "x y z",
        }).status_code == 404

    def test_validation_gates(self, client, pair):
        _, ha, b, _ = pair
        assert client.post(REPORTS, headers=ha, json={
            "target_type": "tweet", "target_id": b["id"], "reason": "r",
        }).status_code == 400
        assert client.post(REPORTS, headers=ha, json={
            "target_type": "user", "target_id": "  ", "reason": "r",
        }).status_code == 400
        assert client.post(REPORTS, headers=ha, json={
            "target_type": "user", "target_id": b["id"], "reason": "   ",
        }).status_code == 400
        assert client.post(REPORTS, headers=ha, json={
            "target_type": "user", "target_id": b["id"], "reason": "x" * 1001,
        }).status_code == 400
        assert client.post(REPORTS, json={
            "target_type": "user", "target_id": b["id"], "reason": "r",
        }).status_code == 401




# -----------------------------------------------------------
# Reports: the admin panel side
# -----------------------------------------------------------

class TestAdminReports:

    def _file_report(self, client, ha, target_id):
        return client.post(REPORTS, headers=ha, json={
            "target_type": "user", "target_id": target_id, "reason": "Įžeidinėja",
        }).get_json()["id"]

    def test_open_list_carries_reporter_and_target_names(self, client, pair, admin):
        a, ha, b, _ = pair
        _, admin_headers = admin
        report_id = self._file_report(client, ha, b["id"])

        listing = client.get(ADMIN_REPORTS, headers=admin_headers).get_json()["reports"]
        mine = next(r for r in listing if r["id"] == report_id)
        assert mine["reporterName"] == "Asta Astaitė"
        assert mine["targetUserName"] == "Benas Benaitis"
        assert mine["status"] == "open"

    def test_resolve_moves_between_lists_and_audits(self, client, db, pair, admin):
        _, ha, b, _ = pair
        _, admin_headers = admin
        report_id = self._file_report(client, ha, b["id"])

        resp = client.put(f"{ADMIN_REPORTS}/{report_id}",
                          json={"status": "resolved"}, headers=admin_headers)
        assert resp.status_code == 200

        open_ids = [r["id"] for r in client.get(
            ADMIN_REPORTS, headers=admin_headers).get_json()["reports"]]
        assert report_id not in open_ids
        resolved_ids = [r["id"] for r in client.get(
            f"{ADMIN_REPORTS}?status=resolved", headers=admin_headers).get_json()["reports"]]
        assert report_id in resolved_ids

        audit = db.execute(
            "SELECT 1 FROM admin_audit WHERE action = 'report.status' AND target = ?",
            (report_id,),
        ).fetchone()
        assert audit is not None

        # Reopening is allowed — a mistaken resolve must be reversible
        assert client.put(f"{ADMIN_REPORTS}/{report_id}",
                          json={"status": "open"}, headers=admin_headers).status_code == 200

    def test_gates(self, client, pair, admin):
        _, ha, b, _ = pair
        _, admin_headers = admin
        # Students cannot read the ledger
        assert client.get(ADMIN_REPORTS, headers=ha).status_code == 403
        # Unknown filter and unknown id are named errors, not 500s
        assert client.get(f"{ADMIN_REPORTS}?status=weird",
                          headers=admin_headers).status_code == 400
        assert client.put(f"{ADMIN_REPORTS}/no-such",
                          json={"status": "resolved"}, headers=admin_headers).status_code == 404
        assert client.put(f"{ADMIN_REPORTS}/no-such",
                          json={"status": "maybe"}, headers=admin_headers).status_code == 400
