############################################################
#  [*] Regression tests — the friend-request state machine
#
#  The handshake's every settle path: send, the mutual-send
#  auto-accept, accept writing BOTH rows and clearing stale
#  declines, the two reject meanings (a recipient's decline
#  feeds the cooldown, a sender's cancel leaves no record),
#  the blocked-pair 404, and unfriend clearing a
#  half-present friendship from either side. Plus the
#  crossed mutual send the per-DIRECTION index never stops:
#  accept and the auto-accept clear the pending row in BOTH
#  directions, a decline against a current friend writes no
#  cooldown, and a decline older than a later friendship
#  does not brake a new send.
############################################################


from datetime import datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now, utc_now_iso
from knfapp.social.activity import record_activity
from knfapp.social.models import Activity, FriendRequest, Friendship
from knfapp.users import auth
from .utils import bearer, befriend, create_user


class FriendRequestTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.a = create_user(username="tomas")
        self.b = create_user(username="migle", email="migle@knf.vu.lt")
        self.token_a = auth.mint_session(self.a.id)
        self.token_b = auth.mint_session(self.b.id)

    def _send(self, token, target):
        return bearer(self.client.post, "/api/social/friends/request", token,
                      data={"user_id": target}, content_type="application/json")

    def test_send_then_accept_writes_both_directions(self):
        req_id = self._send(self.token_a, self.b.id).json()["id"]
        # The recipient hears about the ask
        self.assertEqual(Activity.objects.filter(user=self.b, kind="connect_request").count(), 1)

        response = bearer(self.client.post, f"/api/social/friends/requests/{req_id}/accept", self.token_b,
                          content_type="application/json")
        self.assertEqual(response.json()["status"], "accepted")
        self.assertTrue(Friendship.objects.filter(user_id=self.a.id, friend_id=self.b.id).exists())
        self.assertTrue(Friendship.objects.filter(user_id=self.b.id, friend_id=self.a.id).exists())
        # The handshake row is gone — the friendships rows carry it
        self.assertEqual(FriendRequest.objects.count(), 0)
        # The asker hears the accept; the pending row left the list
        self.assertEqual(Activity.objects.filter(user=self.a, kind="connect_accept").count(), 1)
        self.assertEqual(Activity.objects.filter(kind="connect_request").count(), 0)

    def test_a_mutual_send_auto_accepts(self):
        self._send(self.token_a, self.b.id)
        response = self._send(self.token_b, self.a.id)
        self.assertEqual((response.status_code, response.json()["status"]), (200, "accepted"))
        self.assertEqual(Friendship.objects.count(), 2)

    def test_only_the_recipient_may_accept(self):
        req_id = self._send(self.token_a, self.b.id).json()["id"]
        self.assertEqual(bearer(self.client.post, f"/api/social/friends/requests/{req_id}/accept",
                                self.token_a, content_type="application/json").status_code, 404)

    def test_the_two_meanings_of_reject(self):
        # The recipient declining leaves the cooldown record
        req_id = self._send(self.token_a, self.b.id).json()["id"]
        bearer(self.client.post, f"/api/social/friends/requests/{req_id}/reject", self.token_b,
               content_type="application/json")
        self.assertEqual(FriendRequest.objects.get(id=req_id).status, "rejected")
        # ... which blocks a re-ask with the stable slug
        again = self._send(self.token_a, self.b.id)
        self.assertEqual((again.status_code, again.json()["code"]), (429, "friend_request_cooldown"))

        # The sender cancelling leaves NO record and no cooldown
        req2 = self._send(self.token_b, self.a.id).json()["id"]
        bearer(self.client.post, f"/api/social/friends/requests/{req2}/reject", self.token_b,
               content_type="application/json")
        self.assertEqual(FriendRequest.objects.filter(id=req2).count(), 0)
        self.assertEqual(self._send(self.token_b, self.a.id).status_code, 201)

    def test_an_expired_decline_stops_binding(self):
        req_id = self._send(self.token_a, self.b.id).json()["id"]
        bearer(self.client.post, f"/api/social/friends/requests/{req_id}/reject", self.token_b,
               content_type="application/json")
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        FriendRequest.objects.filter(id=req_id).update(updated_at=stale, created_at=stale)
        self.assertEqual(self._send(self.token_a, self.b.id).status_code, 201)
        # The opportunistic purge dropped the old decline
        self.assertEqual(FriendRequest.objects.filter(status="rejected").count(), 0)

    def test_accept_clears_stale_declines_between_the_pair(self):
        req_id = self._send(self.token_a, self.b.id).json()["id"]
        bearer(self.client.post, f"/api/social/friends/requests/{req_id}/reject", self.token_b,
               content_type="application/json")
        # b changes their mind and asks a — the pair settles
        req2 = self._send(self.token_b, self.a.id).json()["id"]
        bearer(self.client.post, f"/api/social/friends/requests/{req2}/accept", self.token_a,
               content_type="application/json")
        self.assertEqual(FriendRequest.objects.count(), 0)

    def _plant_pending(self, request_id, sender, recipient):
        # A pending row written directly — the crossed send the
        # per-direction index admits — with the ask the recipient
        # would have heard about
        now = utc_now()
        FriendRequest.objects.create(id=request_id, from_user=sender, to_user=recipient,
                                     created_at=now, updated_at=now)
        record_activity(recipient.id, "connect_request", sender.id, request_id)

    def test_accept_clears_a_crossed_mutual_send_in_both_directions(self):
        self._plant_pending("0-ab", self.a, self.b)
        self._plant_pending("1-ba", self.b, self.a)
        response = bearer(self.client.post, "/api/social/friends/requests/0-ab/accept", self.token_b,
                          content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Friendship.objects.count(), 2)
        # No pending row survives in EITHER direction, and neither
        # side keeps a stale "wants to connect" row
        self.assertEqual(FriendRequest.objects.filter(status="pending").count(), 0)
        self.assertEqual(Activity.objects.filter(kind="connect_request").count(), 0)

    def test_the_auto_accept_clears_the_crossed_row_too(self):
        # Both rows planted; ids sort so the lookup lands on THEIR
        # row and a's send takes the auto-accept branch — a's own
        # crossed row must go with it
        self._plant_pending("0-ba", self.b, self.a)
        self._plant_pending("1-ab", self.a, self.b)
        response = self._send(self.token_a, self.b.id)
        self.assertEqual((response.status_code, response.json()["status"]), (200, "accepted"))
        self.assertEqual(FriendRequest.objects.filter(status="pending").count(), 0)
        self.assertEqual(Activity.objects.filter(kind="connect_request").count(), 0)

    def test_reject_after_the_handshake_writes_no_cooldown(self):
        # The leftover reverse row of a crossed send, declined once
        # the pair is already friends: dropped, never a decline
        befriend(self.a, self.b)
        self._plant_pending("ba", self.b, self.a)
        response = bearer(self.client.post, "/api/social/friends/requests/ba/reject", self.token_a,
                          content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(FriendRequest.objects.count(), 0)
        # ... so after an unfriend b may ask again at once
        bearer(self.client.delete, f"/api/social/friends/{self.a.id}", self.token_b)
        self.assertEqual(self._send(self.token_b, self.a.id).status_code, 201)

    def test_a_decline_older_than_a_later_friendship_does_not_brake(self):
        req_id = self._send(self.token_a, self.b.id).json()["id"]
        bearer(self.client.post, f"/api/social/friends/requests/{req_id}/reject", self.token_b,
               content_type="application/json")
        # The decline is a day old — well inside the cooldown — but a
        # friendship came after it, of which one direction survives
        # (a half-present row lets the send through to the brake)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        FriendRequest.objects.filter(id=req_id).update(updated_at=yesterday, created_at=yesterday)
        Friendship.objects.create(user=self.b, friend=self.a, created_at=utc_now_iso())
        self.assertEqual(self._send(self.token_a, self.b.id).status_code, 201)

    def test_a_blocked_pair_reads_as_missing(self):
        bearer(self.client.post, "/api/social/blocks", self.token_b,
               data={"user_id": self.a.id}, content_type="application/json")
        # The BLOCKED side asking gets the indistinguishable 404
        response = self._send(self.token_a, self.b.id)
        self.assertEqual((response.status_code, response.json()["error"]), (404, "User not found"))

    def test_guards_self_duplicates_and_deactivated(self):
        self.assertEqual(self._send(self.token_a, self.a.id).status_code, 400)
        self._send(self.token_a, self.b.id)
        self.assertEqual(self._send(self.token_a, self.b.id).status_code, 409)
        ghost = create_user(username="dinges", email="d@knf.vu.lt", active=0)
        self.assertEqual(self._send(self.token_a, ghost.id).status_code, 404)


class UnfriendTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.a = create_user(username="tomas")
        self.b = create_user(username="migle", email="migle@knf.vu.lt")
        self.token = auth.mint_session(self.a.id)

    def test_unfriend_clears_both_directions(self):
        befriend(self.a, self.b)
        response = bearer(self.client.delete, f"/api/social/friends/{self.b.id}", self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Friendship.objects.count(), 0)

    def test_a_half_present_friendship_clears_from_either_side(self):
        # One direction lost to a crash or a hand-edit — unfriend
        # is a both-directions DELETE, so either side clears it
        Friendship.objects.create(user=self.b, friend=self.a, created_at=utc_now_iso())
        response = bearer(self.client.delete, f"/api/social/friends/{self.b.id}", self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Friendship.objects.count(), 0)

    def test_nothing_matched_is_the_only_404(self):
        self.assertEqual(bearer(self.client.delete, f"/api/social/friends/{self.b.id}",
                                self.token).status_code, 404)
