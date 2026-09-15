############################################################
#  [*] Regression tests — the assistant admin console API
#
#  The dashboard overview's shape, the review list with its
#  thumbs-down filter and soft-delete flag, the transcript
#  read flattening text and tool parts AND writing the
#  audit row every single time (student chats are personal
#  data), the on-demand re-index running the shared sync
#  through the faked gateway seam, the retrieval test box
#  guard, and the role gate on all of it.
############################################################


import json


from django.test import Client, TestCase, override_settings


from knfapp.admin.models import AdminAudit
from knfapp.assistant import indexing
from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import bearer, create_user


SECRET = "test-internal-secret"


@override_settings(ASSISTANT_INTERNAL_SECRET=SECRET)
class AssistantAdminTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin = create_user(username="vadovas", role="admin")
        self.token = auth.mint_session(self.admin.id)
        self.client = Client()

    def _internal(self, path, body):
        return self.client.post(path, data=json.dumps(body),
                                content_type="application/json",
                                HTTP_X_INTERNAL_SECRET=SECRET)

    def _thread_with_turn(self, question="Kaip gauti stipendija?", rating=None):
        thread_id = self._internal("/internal/assistant/threads", {}).json()["id"]
        reply = {"id": "m2", "role": "assistant",
                 "parts": [{"type": "tool-searchHandbook", "toolCallId": "c1",
                            "input": {"query": "stipendija"}, "output": {"entries": []}},
                           {"type": "text", "text": "Pagal rezultatus."}]}
        self._internal(f"/internal/assistant/threads/{thread_id}/messages",
                       {"messages": [{"id": "m1", "role": "user",
                                      "parts": [{"type": "text", "text": question}]}, reply]})
        if rating is not None:
            self._internal(f"/internal/assistant/threads/{thread_id}/feedback",
                           {"message_id": "m2", "rating": rating})
        return thread_id

    def test_review_list_filters_thumbs_down_and_flags_deleted(self):
        plain = self._thread_with_turn("Pirmas?")
        down = self._thread_with_turn("Antras?", rating=-1)
        self._internal(f"/internal/assistant/threads/{plain}/delete", {})

        everything = bearer(self.client.get, "/api/admin/assistant/threads", self.token).json()
        self.assertEqual(everything["total"], 2)
        by_id = {row["id"]: row for row in everything["threads"]}
        self.assertTrue(by_id[plain]["deleted"])
        self.assertEqual(by_id[down]["down"], 1)
        self.assertEqual(by_id[down]["messages"], 2)
        self.assertEqual(by_id[down]["user"], None)

        complaints = bearer(self.client.get, "/api/admin/assistant/threads?rating=down",
                            self.token).json()
        self.assertEqual([row["id"] for row in complaints["threads"]], [down])

    def test_transcript_flattens_parts_and_audits_every_read(self):
        thread_id = self._thread_with_turn(rating=-1)

        answer = bearer(self.client.get, f"/api/admin/assistant/threads/{thread_id}",
                        self.token).json()
        roles = [(row["role"], row["rating"]) for row in answer["messages"]]
        self.assertEqual(roles, [("user", None), ("assistant", -1)])
        self.assertEqual(answer["messages"][1]["text"], "Pagal rezultatus.")
        self.assertEqual(answer["messages"][1]["tools"], ["searchHandbook"])

        bearer(self.client.get, f"/api/admin/assistant/threads/{thread_id}", self.token)
        views = AdminAudit.objects.filter(action="assistant_thread_view",
                                          target=thread_id).count()
        self.assertEqual(views, 2)

    def test_overview_counts_the_dashboard(self):
        self._thread_with_turn(rating=1)
        answer = bearer(self.client.get, "/api/admin/assistant/overview", self.token).json()
        self.assertEqual(answer["threads"], 1)
        self.assertEqual(answer["ratings"], {"up": 1, "down": 0})
        self.assertIn("knowledge", answer)
        self.assertIn("turns7d", answer)
        self.assertIsNone(answer["activePromptVersion"])

    def test_reindex_runs_the_shared_sync_and_audits(self):
        real = indexing.embed_texts
        indexing.embed_texts = lambda texts, post=None: [[0.5] * 1536 for _ in texts]
        self.addCleanup(lambda: setattr(indexing, "embed_texts", real))

        answer = bearer(self.client.post, "/api/admin/assistant/knowledge/reindex", self.token,
                        data=json.dumps({}), content_type="application/json")
        self.assertEqual(answer.status_code, 200)
        self.assertGreater(answer.json()["embedded"], 0)
        self.assertEqual(AdminAudit.objects.filter(action="assistant_reindex").count(), 1)

    def test_role_gate_and_search_guard(self):
        student = create_user(username="jonas", role="student")
        student_token = auth.mint_session(student.id)
        for path in ("/api/admin/assistant/overview", "/api/admin/assistant/threads"):
            refused = bearer(self.client.get, path, student_token)
            self.assertEqual(refused.status_code, 403)

        empty = bearer(self.client.post, "/api/admin/assistant/knowledge/search", self.token,
                       data=json.dumps({}), content_type="application/json")
        self.assertEqual(empty.status_code, 400)
