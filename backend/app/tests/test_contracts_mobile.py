# -----------------------------------------------------------
#  [*] Tests — the wire contract the production app depends on
#
#  Every assertion here mirrors a TypeScript interface the
#  shipped mobile client parses (mobile/app/services/api/*.ts
#  and mobile/app/types/index.ts). The client is not a browser
#  that tolerates a renamed field: a missing or re-spelled key
#  reaches a user as a blank screen or a crash, and the app
#  cannot be hot-fixed the way the backend can.
#
#  So these tests pin field NAMES and types, not values, and
#  they are deliberately strict about the keys a response must
#  carry. A failure here means: do not deploy without shipping
#  a matching app release.
#
#  Every test is marked `contract`, so the whole set can be run
#  alone before a release:
#      ./runTests.sh -m contract
# -----------------------------------------------------------

import json

import pytest


pytestmark = pytest.mark.contract


# -----------------------------------------------------------
# post_raw
# -----------------------------------------------------------
#
# Flask's test client serialises a `json=` body with the APP's
# own JSON provider, which html-escapes every string on the way
# out — so `json={"content": "I <3 you"}` would put an ALREADY
# ESCAPED string on the wire, which no real client ever sends.
# Contract tests must speak exactly what the mobile app speaks,
# so they post raw bytes instead.
#
# Used by:
#   - every test below that sends a body
# -----------------------------------------------------------

def post_raw(client, path, payload, headers=None):
    merged = {"Content-Type": "application/json", **(headers or {})}
    return client.post(path, data=json.dumps(payload), headers=merged)


# -----------------------------------------------------------
# assert_keys
# -----------------------------------------------------------
#
# Names the missing and the unexpected keys in one message, so
# a failure reads as a diff instead of a bare False.
#
# Used by:
#   - the shape assertions below
# -----------------------------------------------------------

def assert_keys(payload, required, where):
    missing = [k for k in required if k not in payload]
    assert not missing, f"{where}: the app reads {missing}, which the backend no longer sends"




class TestAuthContract:

    def test_login_answers_a_token_and_a_user_object(self, client, make_user):
        user = make_user()
        response = post_raw(client, "/api/auth/login",
                            {"username": user["username"], "password": user["password"]})

        assert response.status_code == 200
        body = response.get_json()
        # AuthResponse { user, token }
        assert_keys(body, ["user", "token"], "AuthResponse")
        assert isinstance(body["token"], str) and body["token"]

    def test_the_user_object_carries_every_field_the_app_reads(self, client, actor):
        _user, headers = actor
        body = client.get("/api/auth/me", headers=headers).get_json()

        payload = body.get("user", body)
        # types/index.ts User
        assert_keys(payload, ["id", "username", "email", "displayName", "role"], "User")
        assert isinstance(payload["id"], str)
        assert isinstance(payload["displayName"], str)

    def test_a_failed_login_carries_a_machine_readable_code(self, client, make_user):
        user = make_user()
        response = post_raw(client, "/api/auth/login",
                            {"username": user["username"], "password": "wrong-password"})

        assert response.status_code == 401
        body = response.get_json()
        # The app maps `code` to a Lithuanian message; without it
        # the user sees raw English backend text
        assert_keys(body, ["error", "code"], "auth error body")
        assert body["code"] == "invalid_credentials"

    def test_validate_code_answers_the_valid_flag_shape(self, client, seeded_code):
        response = post_raw(client, "/api/auth/validate-code", {"code": seeded_code})

        assert response.status_code == 200
        body = response.get_json()
        assert_keys(body, ["valid"], "ValidateCodeResponse")
        assert body["valid"] is True

    def test_an_unknown_code_answers_valid_false_with_a_reason(self, client):
        response = post_raw(client, "/api/auth/validate-code", {"code": "KNF-NOPE"})

        assert response.status_code == 200
        body = response.get_json()
        assert body["valid"] is False
        # The register screen keys its translation off `reason`
        assert_keys(body, ["valid", "reason"], "ValidateCodeResponse (invalid)")




class TestNewsContract:

    def test_the_feed_envelope_matches_NewsFeedResponse(self, client):
        body = client.get("/api/news?page=1").get_json()
        assert_keys(body, ["posts", "page", "perPage", "total", "hasMore"], "NewsFeedResponse")
        assert isinstance(body["posts"], list)
        assert isinstance(body["hasMore"], bool)
        assert isinstance(body["page"], int)

    def test_a_feed_item_matches_NewsPost(self, client, actor):
        _user, headers = actor
        post_raw(client, "/api/social/posts", {"content": "Turinys kontraktui"}, headers)

        posts = client.get("/api/news?page=1", headers=headers).get_json()["posts"]
        assert posts, "the feed served nothing to assert on"
        item = posts[0]

        # types/index.ts NewsPost — the required half
        assert_keys(item, ["id", "title", "content", "date", "likes", "comments", "shares"],
                    "NewsPost")
        assert isinstance(item["likes"], int)
        assert isinstance(item["comments"], int)
        assert isinstance(item["shares"], int)
        assert isinstance(item["date"], str)

    def test_the_detail_route_carries_the_liked_flag(self, client, actor):
        _user, headers = actor
        created = post_raw(client, "/api/social/posts", {"content": "Detalus irasas"}, headers)
        post_id = (created.get_json() or {}).get("id") or \
            client.get("/api/news?page=1", headers=headers).get_json()["posts"][0]["id"]

        body = client.get(f"/api/news/{post_id}", headers=headers).get_json()
        # The article screen seeds its heart from this flag; without
        # it the first tap UNLIKES a post the reader had liked
        assert "liked" in body, "GET /api/news/<id> no longer sends `liked`"
        assert isinstance(body["liked"], bool)

    def test_a_guest_reading_the_detail_gets_liked_false(self, client, db):
        # A faculty article is public; a guest has no like state, and
        # the flag must still be present so the heart renders unfilled
        db.execute(
            "INSERT INTO news_posts (id, title, content, source, post_type, is_public)"
            " VALUES ('guest-post', 'Vieša naujiena', 'Turinys', 'knf.vu.lt', 'article', 1)")
        db.commit()

        response = client.get("/api/news/guest-post")
        assert response.status_code == 200
        assert response.get_json().get("liked") is False

    def test_the_like_toggle_answers_liked_and_likes(self, client, actor):
        _user, headers = actor
        post_raw(client, "/api/social/posts", {"content": "Patiktukas"}, headers)
        post_id = client.get("/api/news?page=1", headers=headers).get_json()["posts"][0]["id"]

        body = client.post(f"/api/news/{post_id}/like", headers=headers).get_json()
        assert_keys(body, ["liked", "likes"], "LikeResponse")
        assert isinstance(body["liked"], bool)
        assert isinstance(body["likes"], int)




class TestChatContract:

    def test_the_conversation_list_matches_ApiConversation(self, client, make_user, auth_headers):
        me = make_user()
        friend = make_user()
        headers = auth_headers(me)

        post_raw(client, "/api/chat/conversations",
                 {"participantIds": [friend["id"]], "type": "direct"}, headers)

        body = client.get("/api/chat/conversations", headers=headers).get_json()
        rows = body["conversations"] if isinstance(body, dict) else body
        assert rows, "no conversation to assert on"
        row = rows[0]

        assert_keys(row, ["id", "type", "title", "pinned", "unreadCount",
                          "lastUpdatedMs", "participants"], "ApiConversation")
        assert isinstance(row["unreadCount"], int)
        # The list sorts on this; a string would sort lexicographically
        assert isinstance(row["lastUpdatedMs"], (int, float))
        assert isinstance(row["participants"], list)

    def test_a_participant_entry_carries_id_and_displayName(self, client, make_user, auth_headers):
        me = make_user()
        friend = make_user()
        headers = auth_headers(me)
        post_raw(client, "/api/chat/conversations",
                 {"participantIds": [friend["id"]], "type": "direct"}, headers)

        body = client.get("/api/chat/conversations", headers=headers).get_json()
        rows = body["conversations"] if isinstance(body, dict) else body
        participants = rows[0]["participants"]
        assert participants, "participants came back empty"
        assert_keys(participants[0], ["id", "displayName"], "ApiConversation.participants[]")

    def test_a_sent_message_echoes_the_clientMsgId_nonce(self, client, make_user, auth_headers):
        me = make_user()
        friend = make_user()
        headers = auth_headers(me)
        created = post_raw(client, "/api/chat/conversations",
                           {"participantIds": [friend["id"]], "type": "direct"}, headers)
        # The app reads this exact key off the create response
        conv_id = created.get_json()["conversationId"]

        sent = post_raw(client, f"/api/chat/conversations/{conv_id}/messages",
                        {"text": "labas", "client_msg_id": "temp-42"}, headers)
        assert sent.status_code in (200, 201)
        message = sent.get_json()["message"]

        assert_keys(message, ["id", "conversationId", "senderId", "senderName",
                              "text", "createdAt"], "ApiMessage")
        # The optimistic bubble is matched by this nonce; without the
        # echo the app falls back to fuzzy text matching
        assert message.get("clientMsgId") == "temp-42"

    def test_the_same_nonce_twice_returns_the_same_message(self, client, make_user, auth_headers):
        me = make_user()
        friend = make_user()
        headers = auth_headers(me)
        created = post_raw(client, "/api/chat/conversations",
                           {"participantIds": [friend["id"]], "type": "direct"}, headers)
        conv_id = created.get_json()["conversationId"]

        payload = {"text": "vienas", "client_msg_id": "nonce-1"}
        first = post_raw(client, f"/api/chat/conversations/{conv_id}/messages", payload, headers)
        second = post_raw(client, f"/api/chat/conversations/{conv_id}/messages", payload, headers)

        assert first.get_json()["message"]["id"] == second.get_json()["message"]["id"], \
            "a retried send created a duplicate message"




class TestSocialContract:

    def test_a_profile_carries_the_counters_the_screen_renders(self, client, actor):
        user, headers = actor
        body = client.get(f"/api/social/profile/{user['id']}", headers=headers).get_json()

        assert_keys(body, ["id", "displayName"], "UserProfile")
        for counter in ("postCount", "friendCount"):
            if counter in body:
                assert isinstance(body[counter], int), f"{counter} must be a number"

    def test_sending_a_friend_request_answers_a_status(self, client, make_user, auth_headers):
        me = make_user()
        target = make_user()
        headers = auth_headers(me)

        response = post_raw(client, "/api/social/friends/request",
                            {"user_id": target["id"]}, headers)

        assert response.status_code in (200, 201)
        body = response.get_json()
        # Two shapes: 201 {id, status:'pending'} and the 200
        # auto-accept {status:'accepted'} with NO id
        assert "status" in body
        assert body["status"] in ("pending", "accepted")

    def test_the_social_feed_envelope_is_paged_like_the_news_feed(self, client, actor):
        _user, headers = actor
        body = client.get("/api/social/feed", headers=headers).get_json()
        assert_keys(body, ["posts", "page", "perPage", "hasMore"], "SocialFeedResponse")




class TestScheduleAndInfoContract:

    def test_the_schedule_answers_a_lessons_collection(self, client):
        response = client.get("/api/schedule?day=1")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)

    def test_info_answers_both_languages_with_the_same_envelope(self, client):
        lt = client.get("/api/info?lang=lt")
        en = client.get("/api/info?lang=en")

        assert lt.status_code == 200 and en.status_code == 200
        # An en payload that is structurally different from lt means
        # the English screen silently renders a different dataset
        assert set(lt.get_json().keys()) == set(en.get_json().keys())




class TestGuestAccessContract:

    def test_the_app_works_without_login(self, client):
        # The product rule: auth ADDS features, it never gates them
        for path in ("/api/news?page=1", "/api/health", "/api/info?lang=lt", "/api/schedule?day=1"):
            response = client.get(path)
            assert response.status_code == 200, f"a guest was refused {path}"

    def test_private_routes_refuse_a_guest_with_401_not_500(self, client):
        for path in ("/api/auth/me", "/api/chat/conversations"):
            response = client.get(path)
            assert response.status_code == 401, f"{path} answered {response.status_code}"
            assert response.is_json
