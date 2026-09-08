############################################################
#  [*] Regression tests — the admin console
#
#  The decisions that keep the console honest: invitation
#  bounds are REJECTED not clamped and privileged codes stay
#  single-use credentials, a curator's view of the code list
#  is scoped to their own mintable-role rows (404, never a
#  403 oracle), the user editor's continuity guards and the
#  deactivation purge, the stats cache with its GLOB'd
#  date-sanity gate, and the complaint queue's role split.
#  Every mutation leaves its admin_audit row.
############################################################


import json
from datetime import datetime, timedelta, timezone


from django.test import Client, TestCase


from knfapp.admin.api import views
from knfapp.admin.models import AdminAudit
from knfapp.common import ratelimit
from knfapp.notifications.models import PushToken
from knfapp.users import auth
from knfapp.users.models import InvitationCode, Session, User
from .utils import bearer, create_invite, create_post, create_user


def _client_for(role, username):
    user = create_user(username=username, role=role)
    return user, auth.mint_session(user.id)


class InvitationMintTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.curator, self.curator_token = _client_for("curator", "kuratorius")
        self.client = Client()

    def _mint(self, token, **body):
        return bearer(self.client.post, "/api/admin/invitations", token,
                      data=json.dumps(body), content_type="application/json")

    def test_out_of_range_numbers_are_rejected_not_clamped(self):
        for body in ({"max_uses": 10000}, {"max_uses": 0}, {"expires_hours": 0},
                     {"expires_hours": 9000}, {"max_uses": True}, {"expires_hours": "10"}):
            self.assertEqual(self._mint(self.admin_token, role="student", **body).status_code, 400, body)
        self.assertEqual(InvitationCode.objects.count(), 0)

    def test_curators_cannot_mint_their_way_up(self):
        for role in ("admin", "curator"):
            self.assertEqual(self._mint(self.curator_token, role=role).status_code, 403)

    def test_privileged_codes_are_single_use_and_short_lived(self):
        self.assertEqual(self._mint(self.admin_token, role="admin", max_uses=2).status_code, 400)
        self.assertEqual(self._mint(self.admin_token, role="admin", expires_hours=100).status_code, 400)
        # The DEFAULT expiry (168 h) is clamped to 72 silently —
        # only an explicit ask past the cap is refused
        response = self._mint(self.admin_token, role="admin")
        self.assertEqual(response.status_code, 201)
        payload = json.loads(response.content)
        expires = datetime.fromisoformat(payload["expiresAt"])
        self.assertLessEqual(expires - datetime.now(timezone.utc), timedelta(hours=72, minutes=1))

    def test_a_mint_leaves_its_audit_row(self):
        response = self._mint(self.admin_token, role="student", max_uses=5)
        self.assertEqual(response.status_code, 201)
        row = AdminAudit.objects.get(action="invitation.create")
        self.assertEqual(row.actor_id, self.admin.id)
        self.assertEqual(row.target, json.loads(response.content)["id"])
        self.assertEqual(json.loads(row.payload)["maxUses"], 5)


class InvitationScopeTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.curator, self.curator_token = _client_for("curator", "kuratorius")
        self.other_curator, _ = _client_for("curator", "kitas")
        self.mine = create_invite(code="MANO", role="student", created_by=self.curator)
        self.admins = create_invite(code="ADMINO", role="admin", created_by=self.admin)
        self.colleagues = create_invite(code="KOLEGOS", role="student", created_by=self.other_curator)
        self.client = Client()

    def test_a_curator_lists_only_their_own_mintable_codes(self):
        response = bearer(self.client.get, "/api/admin/invitations", self.curator_token)
        codes = [i["code"] for i in json.loads(response.content)["invitations"]]
        self.assertEqual(codes, ["MANO"])
        # The admin sees the whole ledger
        response = bearer(self.client.get, "/api/admin/invitations", self.admin_token)
        self.assertEqual(len(json.loads(response.content)["invitations"]), 3)

    def test_revoking_outside_the_scope_is_the_same_404_as_nonexistence(self):
        for target in (self.admins, self.colleagues):
            response = bearer(self.client.delete, f"/api/admin/invitations/{target.id}", self.curator_token)
            self.assertEqual(response.status_code, 404)
        self.assertEqual(InvitationCode.objects.count(), 3)
        response = bearer(self.client.delete, f"/api/admin/invitations/{self.mine.id}", self.curator_token)
        self.assertEqual(response.status_code, 200)

    def test_the_offset_that_used_to_overflow_is_a_400(self):
        response = bearer(self.client.get, f"/api/admin/invitations?offset={2 ** 63}", self.admin_token)
        self.assertEqual(response.status_code, 400)
        response = bearer(self.client.get, "/api/admin/invitations?limit=abc", self.admin_token)
        self.assertEqual(response.status_code, 400)


class UserEditorTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.target, self.target_token = _client_for("admin", "kolega")
        self.client = Client()

    def _patch(self, user_id, **body):
        return bearer(self.client.patch, f"/api/admin/users/{user_id}", self.admin_token,
                      data=json.dumps(body), content_type="application/json")

    def test_an_unknown_target_is_a_404_before_the_body_is_read(self):
        response = bearer(self.client.patch, "/api/admin/users/nera", self.admin_token,
                          data="not json", content_type="application/json")
        self.assertEqual(response.status_code, 404)

    def test_only_a_real_boolean_reaches_the_guards(self):
        # 0 and "false" would slip past the `is False` self-
        # deactivation guard and lock the calling admin out
        for value in (0, "false"):
            response = self._patch(self.admin.id, active=value)
            self.assertEqual(response.status_code, 400)
            self.assertIn("boolean", json.loads(response.content)["error"])
        self.assertTrue(User.objects.get(id=self.admin.id).active)

    def test_the_console_cannot_lock_itself_out(self):
        self.assertEqual(self._patch(self.admin.id, active=False).status_code, 400)
        self.assertEqual(self._patch(self.admin.id, role="student").status_code, 400)
        self.assertEqual(self._patch(self.admin.id).status_code, 400)  # nothing to update

    def test_deactivation_purges_sessions_and_push_tokens(self):
        import uuid
        PushToken.objects.create(id=str(uuid.uuid4()), user_id=self.target.id, token="ExponentPushToken[x]",
                                 created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00")
        response = self._patch(self.target.id, active=False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(json.loads(response.content)["active"])
        self.assertEqual(Session.objects.filter(user_id=self.target.id).count(), 0)
        self.assertEqual(PushToken.objects.filter(user_id=self.target.id).count(), 0)
        self.assertEqual(bearer(self.client.get, "/api/auth/me", self.target_token).status_code, 401)
        self.assertEqual(AdminAudit.objects.filter(action="user.active").count(), 1)

    def test_a_bare_demotion_keeps_the_session_alive(self):
        # Documented gotcha, pinned on purpose: only DEACTIVATION
        # revokes sessions — a demoted admin keeps a working token,
        # just not the role
        self.assertEqual(self._patch(self.target.id, role="teacher").status_code, 200)
        self.assertEqual(bearer(self.client.get, "/api/auth/me", self.target_token).status_code, 200)
        self.assertEqual(bearer(self.client.get, "/api/admin/users", self.target_token).status_code, 403)


class AdminErasureTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.target, _ = _client_for("student", "studentas")
        self.client = Client()

    def test_the_erasure_anonymises_and_tombstones(self):
        create_post(author=self.target, source="user", post_type="social")
        response = bearer(self.client.delete, f"/api/admin/users/{self.target.id}", self.admin_token)
        self.assertEqual(response.status_code, 200)

        row = User.objects.get(id=self.target.id)
        self.assertEqual(row.username, f"deleted-{self.target.id}")
        self.assertEqual(row.display_name, "Ištrintas naudotojas")
        self.assertEqual(row.active, 0)

        from knfapp.news.models import NewsPost
        post = NewsPost.objects.get(author_id=self.target.id)
        self.assertEqual(post.author_name, "Ištrintas naudotojas")
        self.assertTrue(AdminAudit.objects.filter(action="user.delete", target=self.target.id).exists())

    def test_the_caller_is_pointed_at_the_self_service_path(self):
        response = bearer(self.client.delete, f"/api/admin/users/{self.admin.id}", self.admin_token)
        self.assertEqual(response.status_code, 400)
        self.assertIn("DELETE /api/auth/me", json.loads(response.content)["error"])


class StatsTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        views.reset_stats_cache()
        self.addCleanup(views.reset_stats_cache)
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.client = Client()

    def _stats(self):
        return json.loads(bearer(self.client.get, "/api/admin/stats", self.admin_token).content)

    def test_scraped_articles_count_only_the_two_scraper_sources(self):
        create_post(source="knf.vu.lt")
        create_post(source="vu.lt")
        create_post(author=create_user(username="autorius"), source="user", post_type="social")
        stats = self._stats()
        self.assertEqual(stats["posts"], 3)
        self.assertEqual(stats["scrapedArticles"], 2)
        self.assertEqual(stats["users"], 2)

    def test_the_snapshot_is_served_stale_inside_the_ttl(self):
        first = self._stats()
        create_user(username="naujokas")
        self.assertEqual(self._stats()["users"], first["users"])
        views.reset_stats_cache()
        self.assertEqual(self._stats()["users"], first["users"] + 1)

    def test_garbage_expiry_strings_are_not_active_invitations(self):
        # 'netrukus' sorts above every timestamp and would be
        # COUNTED — the GLOB gate throws out anything that does
        # not open with a date; a lapsed same-day code stays out
        create_invite(code="GYVAS")
        create_invite(code="SUGADINTAS", expires_at="netrukus")
        create_invite(code="KA_TIK_BAIGESI",
                      expires_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        self.assertEqual(self._stats()["activeInvitations"], 1)


class ReportQueueTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.curator, self.curator_token = _client_for("curator", "kuratorius")
        self.reporter, _ = _client_for("student", "skundikas")
        self.suspect, _ = _client_for("student", "itariamasis")

        import uuid
        from knfapp.social.models import Report
        from knfapp.common.timestamps import utc_now_iso
        self.report = Report.objects.create(
            id=str(uuid.uuid4()), reporter=self.reporter, target_type="user",
            target_id=self.suspect.id, reason="Netinkamas elgesys", status="open",
            created_at=utc_now_iso(),
        )
        self.client = Client()

    def test_curators_read_the_queue_but_not_the_directory(self):
        response = bearer(self.client.get, "/api/admin/reports", self.curator_token)
        self.assertEqual(response.status_code, 200)
        row = json.loads(response.content)["reports"][0]
        self.assertEqual(row["reporterName"], self.reporter.display_name)
        self.assertEqual(row["targetUserName"], self.suspect.display_name)
        self.assertEqual(bearer(self.client.get, "/api/admin/users", self.curator_token).status_code, 403)

    def test_the_default_view_is_the_open_queue_only(self):
        response = bearer(self.client.put, f"/api/admin/reports/{self.report.id}", self.admin_token,
                          data=json.dumps({"status": "resolved"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(bearer(self.client.get, "/api/admin/reports", self.admin_token).content)["reports"], [])
        archived = bearer(self.client.get, "/api/admin/reports?status=resolved", self.admin_token)
        self.assertEqual(len(json.loads(archived.content)["reports"]), 1)
        self.assertEqual(bearer(self.client.get, "/api/admin/reports?status=bogus", self.admin_token).status_code, 400)
        # Reopening is allowed — a resolve tapped by mistake must reverse
        response = bearer(self.client.put, f"/api/admin/reports/{self.report.id}", self.admin_token,
                          data=json.dumps({"status": "open"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AdminAudit.objects.filter(action="report.status").count(), 2)
