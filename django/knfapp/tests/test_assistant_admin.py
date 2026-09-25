############################################################
#  [*] Regression tests — the assistant admin console API
#
#  The dashboard overview's shape, the review list with its
#  thumbs-down filter and soft-delete flag — its reads
#  audited too, titles and previews are students' words —
#  the transcript read flattening text and tool parts in the
#  app's own total order AND writing the audit row every
#  single time (student chats are personal data), the on-demand re-index running the shared sync
#  through the faked gateway seam, the retrieval test box
#  guard, the role gate on all of it — and the console's
#  CURATED ANSWERS: embedded on save straight into
#  support_chunks, re-read by the nightly sync as unchanged,
#  edited into a new id, deleted outright, validated, and
#  refused whole (nothing written) when the gateway is down.
############################################################


import json
import uuid


from django.test import Client, TestCase, override_settings


from knfapp.admin.models import AdminAudit
from knfapp.assistant import curated, indexing
from knfapp.assistant.gateway import GatewayError
from knfapp.assistant.models import SupportChunk
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

        # Both page reads left a trace — who read whose words
        listed = AdminAudit.objects.filter(action="assistant_threads_list").order_by("created_at")
        self.assertEqual(listed.count(), 2)
        self.assertEqual(listed.last().payload["rating"], "down")

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

    def test_transcript_reads_a_tied_pair_question_first_like_the_app(self):
        from django.utils import timezone
        from knfapp.assistant.models import AssistantMessage, AssistantThread
        thread_id = self._internal("/internal/assistant/threads", {}).json()["id"]
        thread = AssistantThread.objects.get(id=thread_id)
        stamp = timezone.now()
        AssistantMessage.objects.create(thread=thread, id="zzz-q", format="aisdk-v7", created_at=stamp,
                                        content={"role": "user", "parts": [{"type": "text", "text": "Q"}]})
        AssistantMessage.objects.create(thread=thread, id="aaa-a", format="aisdk-v7", created_at=stamp,
                                        content={"role": "assistant", "parts": [{"type": "text", "text": "A"}]})
        answer = bearer(self.client.get, f"/api/admin/assistant/threads/{thread_id}", self.token).json()
        self.assertEqual([row["id"] for row in answer["messages"]], ["zzz-q", "aaa-a"])

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


@override_settings(ASSISTANT_INTERNAL_SECRET=SECRET)
class CuratedAnswerTests(TestCase):

    PATH = "/api/admin/assistant/knowledge/curated"
    VALID = {"question": "Kada egzaminų sesija?", "answer": "Sausio ir birželio mėnesiais.",
             "language": "lt"}

    def setUp(self):
        ratelimit.reset()
        self.admin = create_user(username="vadovas", role="admin")
        self.token = auth.mint_session(self.admin.id)
        self.client = Client()

        # Both the console's save and the indexer embed through
        # the gateway module — fake both seams, logging the texts
        self.embedded = []

        def fake_embed(texts, post=None):
            self.embedded.append(list(texts))
            return [[0.25] * 1536 for _ in texts]

        for module in (curated, indexing):
            real = module.embed_texts
            module.embed_texts = fake_embed
            self.addCleanup(lambda module=module, real=real: setattr(module, "embed_texts", real))

    def _post(self, path, body):
        return bearer(self.client.post, path, self.token, data=json.dumps(body),
                      content_type="application/json")

    def test_create_embeds_into_support_chunks_and_the_nightly_sync_keeps_it(self):
        answer = self._post(self.PATH, self.VALID)
        self.assertEqual(answer.status_code, 201)
        entry = answer.json()
        row = SupportChunk.objects.get(source="curated", source_id=f"admin:{entry['id']}")
        self.assertEqual(row.title, "Kada egzaminų sesija?")
        self.assertEqual(row.text, "Kada egzaminų sesija?\nSausio ir birželio mėnesiais.")
        self.assertEqual((row.language, row.section), ("lt", "faq"))
        self.assertEqual(self.embedded, [[row.text]])

        listed = bearer(self.client.get, self.PATH, self.token).json()["entries"]
        self.assertEqual([(e["id"], e["question"], e["answer"], e["language"]) for e in listed],
                         [(entry["id"], "Kada egzaminų sesija?", "Sausio ir birželio mėnesiais.", "lt")])

        # The nightly sync re-reads the row from its own fields:
        # same id, unchanged — not re-embedded, not retired
        self.embedded.clear()
        indexing.run_index()
        self.assertTrue(SupportChunk.objects.filter(id=row.id).exists())
        self.assertNotIn(row.text, [text for batch in self.embedded for text in batch])
        self.assertEqual(AdminAudit.objects.filter(action="assistant_curated_create",
                                                   target=entry["id"]).count(), 1)

    def test_update_moves_the_entry_to_a_new_id_and_delete_drops_it(self):
        entry = self._post(self.PATH, self.VALID).json()
        source_id = f"admin:{entry['id']}"
        old_id = SupportChunk.objects.get(source_id=source_id).id

        updated = self._post(f"{self.PATH}/{entry['id']}/update",
                             {"question": "Kada sesija?", "answer": "Sausį ir birželį.",
                              "language": "lt"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["answer"], "Sausį ir birželį.")
        rows = list(SupportChunk.objects.filter(source_id=source_id))
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0].id, old_id)
        self.assertEqual(rows[0].text, "Kada sesija?\nSausį ir birželį.")

        gone = self._post(f"{self.PATH}/{entry['id']}/delete", {})
        self.assertEqual(gone.status_code, 200)
        self.assertFalse(SupportChunk.objects.filter(source_id=source_id).exists())
        self.assertEqual(self._post(f"{self.PATH}/{entry['id']}/delete", {}).status_code, 404)
        self.assertEqual(self._post(f"{self.PATH}/{uuid.uuid4()}/update", self.VALID).status_code, 404)

        actions = AdminAudit.objects.filter(action__startswith="assistant_curated_")
        self.assertEqual(sorted(actions.values_list("action", flat=True)),
                         ["assistant_curated_create", "assistant_curated_delete",
                          "assistant_curated_update"])

    def test_validation_the_dead_gateway_and_the_role_gate(self):
        for body in ({"answer": "x", "language": "lt"},
                     {"question": "x", "language": "lt"},
                     {"question": "x", "answer": "y", "language": "de"},
                     {"question": "x" * 201, "answer": "y", "language": "lt"},
                     {"question": "x", "answer": "y" * 1201, "language": "en"}):
            self.assertEqual(self._post(self.PATH, body).status_code, 400, body)

        def dead(texts, post=None):
            raise GatewayError("down")
        curated.embed_texts = dead
        refused = self._post(self.PATH, self.VALID)
        self.assertEqual(refused.status_code, 502)
        self.assertFalse(SupportChunk.objects.filter(source="curated",
                                                     source_id__startswith="admin:").exists())

        student = create_user(username="jonas", role="student")
        student_token = auth.mint_session(student.id)
        self.assertEqual(bearer(self.client.get, self.PATH, student_token).status_code, 403)
