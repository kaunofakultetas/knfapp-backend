############################################################
#  [*] Regression tests — the assistant's internal door
#
#  The shared-secret gate (fails closed when unconfigured),
#  the thread access rule in every route that carries it —
#  an owned thread answers only its owner, a guest thread
#  answers any presenter of the uuid, a deleted one
#  answers nobody, and "not yours" is indistinguishable
#  from "not there" — the turn upsert's idempotence and
#  auto-title, the claim flow adopting only ownerless
#  threads, the search route with retrieval faked (the
#  cosine SQL is postgres-only; sqlite exercises the
#  callers), and the telemetry route folding bad labels
#  instead of erroring.
############################################################


import json
import uuid


from django.test import Client, TestCase, override_settings


from knfapp.assistant.api import internal_views
from knfapp.assistant.gateway import GatewayError
from knfapp.assistant.models import AssistantMessage, AssistantThread, AssistantTurn
from .utils import create_user


SECRET = "test-internal-secret"


def _user_message(message_id="m1", text="Kaip gauti stipendija?"):
    return {"id": message_id, "role": "user", "parts": [{"type": "text", "text": text}]}


@override_settings(ASSISTANT_INTERNAL_SECRET=SECRET)
class AssistantInternalTests(TestCase):

    def setUp(self):
        self.client = Client()

    def _post(self, path, body, secret=SECRET):
        headers = {"HTTP_X_INTERNAL_SECRET": secret} if secret else {}
        return self.client.post(path, data=json.dumps(body),
                                content_type="application/json", **headers)

    def _get(self, path, secret=SECRET):
        headers = {"HTTP_X_INTERNAL_SECRET": secret} if secret else {}
        return self.client.get(path, **headers)

    def _create_thread(self, user_id=None, language="lt"):
        response = self._post("/internal/assistant/threads",
                              {"user_id": user_id, "language": language})
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]


    # ----- the gate -----

    def test_missing_and_wrong_secret_are_refused(self):
        for secret in (None, "wrong"):
            response = self._post("/internal/assistant/threads", {}, secret=secret)
            self.assertEqual(response.status_code, 403)

    def test_unconfigured_secret_fails_closed(self):
        with override_settings(ASSISTANT_INTERNAL_SECRET=""):
            response = self._post("/internal/assistant/threads", {}, secret="")
            self.assertEqual(response.status_code, 403)


    # ----- threads -----

    def test_created_thread_lists_for_its_owner_only(self):
        owner = create_user(username="ona")
        stranger = create_user(username="petras")
        thread_id = self._create_thread(user_id=owner.id)

        mine = self._get(f"/internal/assistant/threads/list?user_id={owner.id}").json()
        self.assertEqual([row["id"] for row in mine["threads"]], [thread_id])
        theirs = self._get(f"/internal/assistant/threads/list?user_id={stranger.id}").json()
        self.assertEqual(theirs["threads"], [])

    def test_guest_lookup_serves_ownerless_and_own_but_never_foreign(self):
        owner = create_user(username="ona")
        guest_thread = self._create_thread()
        owned_thread = self._create_thread(user_id=owner.id)

        body = {"ids": [guest_thread, owned_thread, str(uuid.uuid4()), "garbage"]}
        anonymous = self._post("/internal/assistant/threads/lookup", body).json()
        self.assertEqual([row["id"] for row in anonymous["threads"]], [guest_thread])

        as_owner = self._post("/internal/assistant/threads/lookup",
                              {**body, "user_id": owner.id}).json()
        self.assertEqual({row["id"] for row in as_owner["threads"]},
                         {guest_thread, owned_thread})

    def test_claim_adopts_only_ownerless_threads(self):
        owner = create_user(username="ona")
        other = create_user(username="petras")
        guest_thread = self._create_thread()
        foreign_thread = self._create_thread(user_id=other.id)

        response = self._post("/internal/assistant/threads/claim",
                              {"ids": [guest_thread, foreign_thread], "user_id": owner.id})
        self.assertEqual(response.json()["claimed"], 1)
        self.assertEqual(str(AssistantThread.objects.get(id=guest_thread).user_id), owner.id)
        self.assertEqual(str(AssistantThread.objects.get(id=foreign_thread).user_id), other.id)

    def test_delete_soft_deletes_and_hides_everywhere(self):
        thread_id = self._create_thread()
        response = self._post(f"/internal/assistant/threads/{thread_id}/delete", {})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(AssistantThread.objects.get(id=thread_id).deleted_at)

        lookup = self._post("/internal/assistant/threads/lookup", {"ids": [thread_id]}).json()
        self.assertEqual(lookup["threads"], [])
        messages = self._get(f"/internal/assistant/threads/{thread_id}/messages")
        self.assertEqual(messages.status_code, 404)


    # ----- the transcript -----

    def test_turn_upsert_stores_titles_and_replays(self):
        thread_id = self._create_thread()
        reply = {"id": "m2", "role": "assistant",
                 "parts": [{"type": "text", "text": "Stipendijos skiriamos pagal rezultatus."}]}
        response = self._post(f"/internal/assistant/threads/{thread_id}/messages",
                              {"messages": [_user_message(), reply]})
        self.assertEqual(response.json()["stored"], 2)

        thread = AssistantThread.objects.get(id=thread_id)
        self.assertEqual(thread.title, "Kaip gauti stipendija?")

        replay = self._get(f"/internal/assistant/threads/{thread_id}/messages").json()
        self.assertEqual([row["id"] for row in replay["messages"]], ["m1", "m2"])
        self.assertEqual(replay["messages"][0]["content"]["parts"][0]["text"],
                         "Kaip gauti stipendija?")

        # The thread list's second line: the newest ANSWER
        listed = self._post("/internal/assistant/threads/lookup", {"ids": [thread_id]}).json()
        self.assertEqual(listed["threads"][0]["preview"],
                         "Stipendijos skiriamos pagal rezultatus.")

    def test_turn_upsert_is_idempotent_and_title_sticks(self):
        thread_id = self._create_thread()
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message()]})
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message(), _user_message("m9", "Kitas klausimas")]})

        self.assertEqual(AssistantMessage.objects.filter(thread_id=thread_id).count(), 2)
        self.assertEqual(AssistantThread.objects.get(id=thread_id).title,
                         "Kaip gauti stipendija?")

    def test_owned_transcript_refuses_strangers_as_not_found(self):
        owner = create_user(username="ona")
        thread_id = self._create_thread(user_id=owner.id)
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"user_id": owner.id, "messages": [_user_message()]})

        stranger = self._get(f"/internal/assistant/threads/{thread_id}/messages?user_id=kitas")
        self.assertEqual(stranger.status_code, 404)
        anonymous = self._get(f"/internal/assistant/threads/{thread_id}/messages")
        self.assertEqual(anonymous.status_code, 404)
        owned = self._get(f"/internal/assistant/threads/{thread_id}/messages?user_id={owner.id}")
        self.assertEqual(owned.status_code, 200)


    def test_feedback_rates_clears_and_cloaks(self):
        owner = create_user(username="ona")
        thread_id = self._create_thread(user_id=owner.id)
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"user_id": owner.id,
                    "messages": [_user_message(), {"id": "m2", "role": "assistant",
                                                   "parts": [{"type": "text", "text": "Atsakymas"}]}]})

        rate = self._post(f"/internal/assistant/threads/{thread_id}/feedback",
                          {"user_id": owner.id, "message_id": "m2", "rating": 1})
        self.assertEqual(rate.status_code, 200)
        self.assertEqual(AssistantMessage.objects.get(thread_id=thread_id, id="m2").rating, 1)

        # 0 clears; a stranger and a missing message read as 404
        self._post(f"/internal/assistant/threads/{thread_id}/feedback",
                   {"user_id": owner.id, "message_id": "m2", "rating": 0})
        self.assertIsNone(AssistantMessage.objects.get(thread_id=thread_id, id="m2").rating)
        stranger = self._post(f"/internal/assistant/threads/{thread_id}/feedback",
                              {"user_id": "kitas", "message_id": "m2", "rating": -1})
        self.assertEqual(stranger.status_code, 404)
        missing = self._post(f"/internal/assistant/threads/{thread_id}/feedback",
                             {"user_id": owner.id, "message_id": "nope", "rating": -1})
        self.assertEqual(missing.status_code, 404)
        bad = self._post(f"/internal/assistant/threads/{thread_id}/feedback",
                         {"user_id": owner.id, "message_id": "m2", "rating": 5})
        self.assertEqual(bad.status_code, 400)


    # ----- search (retrieval faked — the SQL is postgres-only) -----

    def test_search_answers_the_faked_retrieval(self):
        rows = [{"id": "handbook:lt-faq:0-0", "title": "Kaip gauti stipendija?",
                 "excerpt": "Stipendijos skiriamos...", "section": "faq", "language": "lt"}]
        calls = []

        def fake_search(query, limit=5, language=None, embed=None):
            calls.append((query, limit, language))
            return rows

        real = internal_views.search_chunks
        internal_views.search_chunks = fake_search
        self.addCleanup(lambda: setattr(internal_views, "search_chunks", real))

        response = self._post("/internal/assistant/search",
                              {"query": "stipendija", "limit": 3, "language": "lt"})
        self.assertEqual(response.json()["results"], rows)
        self.assertEqual(calls, [("stipendija", 3, "lt")])

    def test_search_requires_a_query_and_maps_gateway_failure_to_502(self):
        self.assertEqual(self._post("/internal/assistant/search", {}).status_code, 400)

        def broken_search(*args, **kwargs):
            raise GatewayError("down")

        real = internal_views.search_chunks
        internal_views.search_chunks = broken_search
        self.addCleanup(lambda: setattr(internal_views, "search_chunks", real))
        response = self._post("/internal/assistant/search", {"query": "x"})
        self.assertEqual(response.status_code, 502)


    # ----- telemetry -----

    def test_turn_log_folds_bad_labels_and_unknown_refs_to_null(self):
        response = self._post("/internal/assistant/turn-log", {
            "thread_id": "not-a-uuid", "user_id": "nobody",
            "model": "gpt-4o", "outcome": "weird",
            "input_tokens": "many", "output_tokens": 12,
            "tool_calls": [{"name": "searchHandbook", "ms": 120, "ok": True}],
        })
        self.assertEqual(response.status_code, 201)
        turn = AssistantTurn.objects.get()
        self.assertIsNone(turn.thread_id)
        self.assertIsNone(turn.user_id)
        self.assertEqual(turn.outcome, "error")
        self.assertEqual(turn.input_tokens, 0)
        self.assertEqual(turn.output_tokens, 12)
        self.assertEqual(turn.tool_calls[0]["name"], "searchHandbook")
