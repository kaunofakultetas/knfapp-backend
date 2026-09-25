############################################################
#  [*] Regression tests — HTTP verb enforcement
#
#  Every routed view wears @require_methods with the verb
#  urls.py documents, and every shared-path dispatcher names
#  its branches and answers the rest with the same JSON 405
#  — so a wrong verb can no longer fall INTO a write
#  handler. The table below is the proven-live list: a GET
#  that cleared the unread badge, a GET that ran a full
#  scrape, a GET/HEAD that left (and purged) a room, a GET
#  that deleted a meme or an invitation, a PATCH that edited
#  a wall post, a GET that silently unpinned. Each wrong
#  verb answers {"error", "code": "method_not_allowed"} with
#  an Allow header, before auth and before the rate limiter
#  (no bearer is needed to be told the verb is wrong, no
#  budget is spent), the row or state it would have touched
#  survives, and the right verb still answers as before.
############################################################


import uuid


from django.test import Client, TestCase


from knfapp.chat import events
from knfapp.chat.api import views as chat_views
from knfapp.chat.models import Conversation, ConversationParticipant, Message
from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now
from knfapp.memes.models import Meme
from knfapp.scraper.api import views as scraper_views
from knfapp.social.activity import record_activity
from knfapp.social.models import Activity
from knfapp.users import auth
from knfapp.users.models import InvitationCode
from .utils import bearer, create_invite, create_message, create_post, create_room, create_user


ENVELOPE = {"error": "Method not allowed", "code": "method_not_allowed"}


class VerbGateTestCase(TestCase):

    def setUp(self):
        ratelimit.reset()
        events.reset_socket_state()
        self.client = Client()
        self.student = create_user(username="tomas")
        self.admin = create_user(username="vadovas", email="vadovas@knf.vu.lt", role="admin")
        self.token = auth.mint_session(self.student.id)
        self.admin_token = auth.mint_session(self.admin.id)

    def assert_refused(self, response, allow):
        self.assertEqual(response.status_code, 405, response.content)
        self.assertEqual(response["Allow"], allow)
        if response.request["REQUEST_METHOD"] != "HEAD":
            self.assertEqual(response.json(), ENVELOPE)


class WriteFallthroughTests(VerbGateTestCase):

    def test_get_on_activity_read_leaves_the_badge_alone(self):
        record_activity(self.student.id, "connect_request", self.admin.id, "req-1")
        self.assert_refused(bearer(self.client.get, "/api/social/activity/read", self.token), "POST")
        self.assertEqual(Activity.objects.filter(user=self.student, read=0).count(), 1)
        # The right verb still flips it
        self.assertEqual(bearer(self.client.post, "/api/social/activity/read", self.token).status_code, 200)
        self.assertEqual(Activity.objects.filter(user=self.student, read=0).count(), 0)

    def test_get_on_the_scraper_triggers_runs_nothing(self):
        calls = []
        real = scraper_views.scrape_knf_schedule
        scraper_views.scrape_knf_schedule = lambda **kw: calls.append(kw) or {"found": 0, "new": 0}
        self.addCleanup(lambda: setattr(scraper_views, "scrape_knf_schedule", real))

        self.assert_refused(bearer(self.client.get, "/api/scraper/schedule", self.admin_token), "POST")
        self.assertEqual(calls, [])
        for path in ("/api/scraper/trigger", "/api/scraper/run", "/api/scraper/info"):
            self.assert_refused(bearer(self.client.get, path, self.admin_token), "POST")
        # POST still scrapes
        self.assertEqual(bearer(self.client.post, "/api/scraper/schedule", self.admin_token).status_code, 200)
        self.assertEqual(calls, [{"notify": False}])

    def test_get_and_head_on_a_conversation_do_not_leave_it(self):
        room = create_room([self.student, self.admin])
        path = f"/api/chat/conversations/{room.id}"
        self.assert_refused(bearer(self.client.get, path, self.token), "DELETE")
        self.assert_refused(bearer(self.client.head, path, self.token), "DELETE")
        self.assertTrue(Conversation.objects.filter(id=room.id).exists())
        self.assertEqual(ConversationParticipant.objects.filter(conversation=room).count(), 2)
        # DELETE still leaves
        self.assertEqual(bearer(self.client.delete, path, self.token).status_code, 200)
        self.assertFalse(ConversationParticipant.objects.filter(conversation=room, user=self.student).exists())

    def test_get_on_a_meme_or_an_invitation_deletes_nothing(self):
        meme = Meme.objects.create(id=str(uuid.uuid4()), filename="a" * 32 + ".jpg", title="Memas",
                                   added_by_id=self.student.id, byte_size=1, created_at=utc_now())
        self.assert_refused(bearer(self.client.get, f"/api/memes/{meme.id}", self.token), "DELETE")
        self.assertTrue(Meme.objects.filter(id=meme.id).exists())
        self.assertEqual(bearer(self.client.delete, f"/api/memes/{meme.id}", self.token).status_code, 200)
        self.assertFalse(Meme.objects.filter(id=meme.id).exists())

        invite = create_invite(code="KODAS1")
        self.assert_refused(bearer(self.client.get, f"/api/admin/invitations/{invite.id}", self.admin_token),
                            "DELETE")
        self.assertTrue(InvitationCode.objects.filter(id=invite.id).exists())
        self.assertEqual(bearer(self.client.delete, f"/api/admin/invitations/{invite.id}",
                                self.admin_token).status_code, 200)
        self.assertFalse(InvitationCode.objects.filter(id=invite.id).exists())

    def test_the_other_write_fallthroughs_are_405s(self):
        # register_token, update_wall_post, update_user, edit_message
        # and react_to_message were the "else" of their dispatchers
        self.assert_refused(bearer(self.client.get, "/api/notifications/register", self.token), "POST, DELETE")
        post = create_post(author=self.student)
        self.assert_refused(bearer(self.client.patch, f"/api/social/posts/{post.id}", self.token), "PUT, DELETE")
        self.assert_refused(bearer(self.client.post, f"/api/admin/users/{self.student.id}", self.admin_token),
                            "PATCH, DELETE")
        room = create_room([self.student, self.admin])
        msg = create_message(room, self.admin)
        base = f"/api/chat/conversations/{room.id}/messages/{msg.id}"
        self.assert_refused(bearer(self.client.get, base, self.token), "PUT, DELETE")
        self.assert_refused(bearer(self.client.put, f"{base}/react", self.token), "POST, DELETE")
        self.assertEqual(Message.objects.get(id=msg.id).text, "Labas")

    def test_the_pin_route_pins_on_put_unpins_on_delete_and_nothing_else(self):
        room = create_room([self.student, self.admin])
        msg = create_message(room, self.admin)
        path = f"/api/chat/conversations/{room.id}/messages/{msg.id}/pin"
        self.assertEqual(bearer(self.client.put, path, self.token).status_code, 200)
        self.assertIsNotNone(Message.objects.get(id=msg.id).pinned_at)
        # GET and POST used to unpin silently — the pin survives both
        self.assert_refused(bearer(self.client.get, path, self.token), "PUT, DELETE")
        self.assert_refused(bearer(self.client.post, path, self.token), "PUT, DELETE")
        self.assertIsNotNone(Message.objects.get(id=msg.id).pinned_at)
        self.assertEqual(bearer(self.client.delete, path, self.token).status_code, 200)
        self.assertIsNone(Message.objects.get(id=msg.id).pinned_at)


class GuardPlacementTests(VerbGateTestCase):

    def test_the_guard_answers_before_auth(self):
        # No bearer at all: 405, never 401 — the verb is judged
        # before any session is resolved
        self.assert_refused(self.client.get("/api/social/activity/read"), "POST")
        self.assert_refused(self.client.post("/api/schedule/filters"), "GET, HEAD")
        self.assert_refused(self.client.post("/api/health"), "GET, HEAD")
        self.assert_refused(self.client.delete("/api/info"), "GET, HEAD")
        self.assert_refused(self.client.get("/api/scraper/trigger"), "POST")
        self.assert_refused(self.client.put("/api/wayfind/buildings/b1/panoramas"), "POST")
        self.assert_refused(self.client.get("/internal/assistant/turn-log"), "POST")
        self.assert_refused(self.client.delete("/api/admin/assistant/overview"), "GET, HEAD")

    def test_the_guard_spends_no_rate_limit_budget(self):
        # 61 wrong-verb hits on a route limited to 60 per window,
        # then the right verb still answers — the limiter never saw them
        for _ in range(61):
            self.assert_refused(bearer(self.client.get, f"/api/social/blocks/{self.admin.id}", self.token), "DELETE")
        self.assertEqual(bearer(self.client.delete, f"/api/social/blocks/{self.admin.id}", self.token).status_code, 200)

    def test_head_rides_with_get_and_the_dispatchers_name_their_verbs(self):
        self.assertEqual(self.client.head("/api/schedule/filters").status_code, 200)
        self.assertEqual(self.client.head("/api/wayfind/buildings").status_code, 200)
        # The users / news / uploads dispatchers answer their unmatched
        # branch the same way, Allow listing every named verb
        self.assert_refused(self.client.patch("/api/news"), "GET, HEAD, POST")
        self.assert_refused(self.client.post("/api/uploads/x.jpg"), "GET, HEAD, DELETE")
        self.assert_refused(self.client.put("/api/news/nera/poll"), "GET, HEAD, POST, DELETE")
        self.assert_refused(bearer(self.client.patch, "/api/social/profile", self.token), "GET, HEAD, PUT")
        self.assert_refused(bearer(self.client.get, "/api/notifications/register", self.token), "POST, DELETE")

    def test_the_guard_keeps_the_chat_views_non_atomic_mark(self):
        # @require_methods sits above @transaction.non_atomic_requests;
        # functools.wraps must carry the mark or Django would wrap the
        # chat writes back into one request transaction
        for view in (chat_views.leave_conversation, chat_views.pin_message, chat_views.search_users):
            self.assertTrue(getattr(view, "_non_atomic_requests", None), view.__name__)
