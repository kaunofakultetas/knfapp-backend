############################################################
#  [*] Regression tests — the admin oversight reads
#
#  The four windows the web panel runs on: the audit trail
#  (admin-only, actor names joined, payload parsed back from
#  its stored JSON), the stored-file ledger (ownerless rows
#  answer a null owner, not a 500), the reported-message
#  window (admin AND curator — the report-queue pair — and
#  an unsent message still answers, stamp included), and the
#  tombstone list with its audited restore (second restore =
#  the same 404 as never-tombstoned). Role gates pinned
#  throughout: 403 for the wrong role, never an empty 200.
############################################################


import json
import uuid


from django.test import Client, TestCase


from knfapp.admin.models import AdminAudit
from knfapp.common.timestamps import utc_now_iso
from knfapp.news.models import DeletedSourceUrl
from knfapp.uploads.models import Upload
from knfapp.users import auth
from .utils import bearer, create_message, create_room, create_user


def _client_for(role, username):
    user = create_user(username=username, role=role)
    return user, auth.mint_session(user.id)


class AuditTrailTests(TestCase):

    def setUp(self):
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.curator, self.curator_token = _client_for("curator", "kuratorius")
        self.client = Client()

    def test_a_mutation_lands_in_the_trail_with_actor_name_and_parsed_payload(self):
        # A real mutation through the console, not a hand-written row
        minted = bearer(self.client.post, "/api/admin/invitations", self.admin_token,
                        data=json.dumps({"role": "student"}), content_type="application/json")
        self.assertEqual(minted.status_code, 201)

        listed = bearer(self.client.get, "/api/admin/audit", self.admin_token)
        self.assertEqual(listed.status_code, 200)

        entry = listed.json()["audit"][0]
        self.assertEqual(entry["action"], "invitation.create")
        self.assertEqual(entry["actorId"], self.admin.id)
        self.assertEqual(entry["actorName"], "Vadovas")
        self.assertEqual(entry["payload"]["role"], "student")   # parsed, not a JSON string
        self.assertEqual(entry["target"], minted.json()["id"])

    def test_the_trail_is_admin_only_and_reading_it_writes_nothing(self):
        self.assertEqual(bearer(self.client.get, "/api/admin/audit", self.curator_token).status_code, 403)

        before = AdminAudit.objects.count()
        bearer(self.client.get, "/api/admin/audit", self.admin_token)
        self.assertEqual(AdminAudit.objects.count(), before)

    def test_limit_pages_and_garbage_payload_rows_do_not_500(self):
        for n in range(3):
            AdminAudit.objects.create(id=str(uuid.uuid4()), actor=self.admin, action="user.role",
                                      target=f"t{n}", payload="ne-json{", created_at=utc_now_iso())

        listed = bearer(self.client.get, "/api/admin/audit?limit=2", self.admin_token)
        self.assertEqual(len(listed.json()["audit"]), 2)
        # The unparsable payload comes back as the raw string
        self.assertEqual(listed.json()["audit"][0]["payload"], "ne-json{")


class UploadLedgerTests(TestCase):

    def setUp(self):
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.curator, self.curator_token = _client_for("curator", "kuratorius")
        self.owner = create_user(username="ona")
        self.client = Client()

        Upload.objects.create(id=str(uuid.uuid4()), filename="a1b2.jpg", user=self.owner,
                              byte_size=1234, created_at=utc_now_iso())
        # The row an erasure leaves behind — owner gone, file kept
        Upload.objects.create(id=str(uuid.uuid4()), filename="c3d4.pdf", user=None,
                              byte_size=999, created_at=utc_now_iso())

    def test_the_ledger_lists_owners_and_survives_ownerless_rows(self):
        listed = bearer(self.client.get, "/api/admin/uploads", self.admin_token)
        self.assertEqual(listed.status_code, 200)

        by_name = {u["filename"]: u for u in listed.json()["uploads"]}
        self.assertEqual(by_name["a1b2.jpg"]["userName"], "Ona")
        self.assertEqual(by_name["a1b2.jpg"]["url"], "/api/uploads/a1b2.jpg")
        self.assertEqual(by_name["a1b2.jpg"]["size"], 1234)
        self.assertIsNone(by_name["c3d4.pdf"]["userName"])
        self.assertIsNone(by_name["c3d4.pdf"]["userId"])

    def test_the_ledger_is_admin_only(self):
        self.assertEqual(bearer(self.client.get, "/api/admin/uploads", self.curator_token).status_code, 403)


class ReportedMessageTests(TestCase):

    def setUp(self):
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.curator, self.curator_token = _client_for("curator", "kuratorius")
        self.student, self.student_token = _client_for("student", "tomas")
        self.other = create_user(username="ona")
        self.client = Client()

        # A room the staff readers are NOT members of — that is
        # the whole point of the moderation window
        room = create_room([self.student, self.other])
        self.message = create_message(room, self.student, text="Įžeidžiantis tekstas")

    def test_staff_read_a_message_from_a_room_they_are_not_in(self):
        for token in (self.admin_token, self.curator_token):
            fetched = bearer(self.client.get, f"/api/admin/messages/{self.message.id}", token)
            self.assertEqual(fetched.status_code, 200)

        body = fetched.json()
        self.assertEqual(body["text"], "Įžeidžiantis tekstas")
        self.assertEqual(body["senderName"], "Tomas")
        self.assertEqual(body["conversationType"], "direct")

    def test_an_unsent_message_still_answers_with_its_stamp(self):
        stamp = utc_now_iso()
        type(self.message).objects.filter(id=self.message.id).update(deleted_at=stamp)

        fetched = bearer(self.client.get, f"/api/admin/messages/{self.message.id}", self.admin_token)
        self.assertEqual(fetched.json()["deletedAt"], stamp)

    def test_students_get_403_and_unknown_ids_404(self):
        self.assertEqual(
            bearer(self.client.get, f"/api/admin/messages/{self.message.id}", self.student_token).status_code, 403)
        self.assertEqual(
            bearer(self.client.get, "/api/admin/messages/nera-tokios", self.admin_token).status_code, 404)


class TombstoneTests(TestCase):

    def setUp(self):
        self.admin, self.admin_token = _client_for("admin", "vadovas")
        self.curator, self.curator_token = _client_for("curator", "kuratorius")
        self.client = Client()

        DeletedSourceUrl.objects.create(source_url="https://knf.vu.lt/naujiena-1",
                                        deleted_by=self.admin, deleted_at=utc_now_iso())

    def _restore(self, token, url):
        return bearer(self.client.post, "/api/admin/tombstones/restore", token,
                      data=json.dumps({"source_url": url}), content_type="application/json")

    def test_the_skip_list_is_readable_and_admin_only(self):
        listed = bearer(self.client.get, "/api/admin/tombstones", self.admin_token)
        entry = listed.json()["tombstones"][0]
        self.assertEqual(entry["sourceUrl"], "https://knf.vu.lt/naujiena-1")
        self.assertEqual(entry["deletedByName"], "Vadovas")

        self.assertEqual(bearer(self.client.get, "/api/admin/tombstones", self.curator_token).status_code, 403)

    def test_restore_lifts_the_tombstone_once_and_is_audited(self):
        first = self._restore(self.admin_token, "https://knf.vu.lt/naujiena-1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(DeletedSourceUrl.objects.count(), 0)
        self.assertTrue(AdminAudit.objects.filter(action="tombstone.restore",
                                                  target="https://knf.vu.lt/naujiena-1").exists())

        # The second restore answers exactly like never-tombstoned
        self.assertEqual(self._restore(self.admin_token, "https://knf.vu.lt/naujiena-1").status_code, 404)

    def test_restore_rejects_blank_urls_and_curators(self):
        self.assertEqual(self._restore(self.admin_token, "  ").status_code, 400)
        self.assertEqual(self._restore(self.curator_token, "https://knf.vu.lt/naujiena-1").status_code, 403)
