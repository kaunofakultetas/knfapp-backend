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
#  instead of erroring. A replay never rewrites a stored
#  row — only this turn's reply may grow (KNF-068); a guest
#  answer whose thread a login claimed mid-turn still lands;
#  a rating only ever lands on an assistant message; and the
#  transcript's order is total — a tied pair reads question
#  first whatever its ids (KNF-149).
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

    def test_an_unknown_user_id_is_a_400_before_the_insert(self):
        # A guest thread needs no user; an id the users table does
        # not know is refused by a lookup — NOT by the FK, which
        # PostgreSQL checks only at commit, after the view returned
        response = self._post("/internal/assistant/threads",
                              {"user_id": str(uuid.uuid4()), "language": "lt"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "unknown user_id")
        self.assertEqual(AssistantThread.objects.count(), 0)

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

    def test_a_nul_byte_in_a_turn_is_stripped_before_storage(self):
        # PostgreSQL's text type refuses NUL (DataError → 500) and
        # SQLite would store it into the title — the body parser
        # strips it before the view, so both engines store the text
        thread_id = self._create_thread()
        response = self._post(f"/internal/assistant/threads/{thread_id}/messages",
                              {"messages": [_user_message(text="Kaip\x00 gauti?")]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(AssistantThread.objects.get(id=thread_id).title, "Kaip gauti?")

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


    def test_a_replay_never_rewrites_a_stored_row_only_this_turns_reply_grows(self):
        thread_id = self._create_thread()
        answer = {"id": "srv-a", "role": "assistant", "parts": [{"type": "text", "text": "Tikras atsakymas."}]}
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message(), answer], "reply_id": "srv-a"})

        # The phone's copy of both, "edited", replayed as the tail
        # of the next turn — nothing already stored may change
        forged = {"id": "srv-a", "role": "assistant", "parts": [{"type": "text", "text": "Suklastota."}]}
        edited_question = _user_message(text="Kitas klausimas")
        reply = {"id": "srv-b", "role": "assistant", "parts": [{"type": "text", "text": "Antras."}]}
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [edited_question, forged, _user_message("m3", "O dar?"), reply],
                    "reply_id": "srv-b"})
        stored = {row.id: row.content for row in AssistantMessage.objects.filter(thread_id=thread_id)}
        self.assertEqual(stored["srv-a"]["parts"][0]["text"], "Tikras atsakymas.")
        self.assertEqual(stored["m1"]["parts"][0]["text"], "Kaip gauti stipendija?")
        self.assertIn("m3", stored)

        # A tool round's continuation grows ITS reply in place
        grown = {"id": "srv-b", "role": "assistant",
                 "parts": [{"type": "text", "text": "Antras."}, {"type": "text", "text": " Ir daugiau."}]}
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [grown], "reply_id": "srv-b"})
        self.assertEqual(len(AssistantMessage.objects.get(thread_id=thread_id, id="srv-b").content["parts"]), 2)

    def test_a_regenerated_answer_replaces_the_old_one_in_the_transcript(self):
        thread_id = self._create_thread()
        old = {"id": "srv-old", "role": "assistant", "parts": [{"type": "text", "text": "Senas atsakymas."}]}
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message(), old], "reply_id": "srv-old"})

        new = {"id": "srv-new", "role": "assistant", "parts": [{"type": "text", "text": "Naujas atsakymas."}]}
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message(), new], "reply_id": "srv-new", "replaced_id": "srv-old"})
        replay = self._get(f"/internal/assistant/threads/{thread_id}/messages").json()
        self.assertEqual([row["id"] for row in replay["messages"]], ["m1", "srv-new"])

        # A rated answer is evidence and stays; a question is never
        # "replaced"; and nothing goes before the new answer exists
        self._post(f"/internal/assistant/threads/{thread_id}/feedback", {"message_id": "srv-new", "rating": 1})
        again = {"id": "srv-3", "role": "assistant", "parts": [{"type": "text", "text": "Trečias."}]}
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message(), again], "reply_id": "srv-3", "replaced_id": "srv-new"})
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message("m5", "Kitas?")], "reply_id": "srv-missing", "replaced_id": "srv-3"})
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [{"id": "srv-4", "role": "assistant", "parts": [{"type": "text", "text": "4"}]}],
                    "reply_id": "srv-4", "replaced_id": "m1"})
        stored = set(AssistantMessage.objects.filter(thread_id=thread_id).values_list("id", flat=True))
        self.assertEqual(stored, {"m1", "srv-new", "srv-3", "m5", "srv-4"})

    def test_a_rated_reply_is_evidence_even_for_its_own_turn(self):
        thread_id = self._create_thread()
        reply = {"id": "srv-r", "role": "assistant", "parts": [{"type": "text", "text": "Įvertintas."}]}
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message(), reply], "reply_id": "srv-r"})
        self._post(f"/internal/assistant/threads/{thread_id}/feedback", {"message_id": "srv-r", "rating": -1})
        rewrite = {"id": "srv-r", "role": "assistant", "parts": [{"type": "text", "text": "Perrašyta."}]}
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [rewrite], "reply_id": "srv-r"})
        self.assertEqual(AssistantMessage.objects.get(thread_id=thread_id, id="srv-r").content["parts"][0]["text"],
                         "Įvertintas.")

    def test_a_guest_answer_lands_in_a_thread_claimed_mid_turn(self):
        owner = create_user(username="ona")
        thread_id = self._create_thread()
        # The login claims the thread while its guest turn streams
        self._post("/internal/assistant/threads/claim", {"ids": [thread_id], "user_id": owner.id})
        batch = {"messages": [_user_message(), {"id": "srv-g", "role": "assistant",
                                                "parts": [{"type": "text", "text": "Atsakymas."}]}],
                 "reply_id": "srv-g"}

        # Without the container's word it is a stranger's write
        plain = self._post(f"/internal/assistant/threads/{thread_id}/messages", batch)
        self.assertEqual(plain.status_code, 404)
        # With it — a turn that BEGAN on the ownerless thread — it lands
        claimed = self._post(f"/internal/assistant/threads/{thread_id}/messages",
                             {**batch, "began_ownerless": True})
        self.assertEqual(claimed.status_code, 200)
        self.assertTrue(AssistantMessage.objects.filter(thread_id=thread_id, id="srv-g").exists())
        # ...but a deleted thread stays closed, flag or not
        self._post(f"/internal/assistant/threads/{thread_id}/delete", {"user_id": owner.id})
        gone = self._post(f"/internal/assistant/threads/{thread_id}/messages",
                          {**batch, "began_ownerless": True})
        self.assertEqual(gone.status_code, 404)

    def test_a_rating_lands_only_on_an_assistant_message(self):
        thread_id = self._create_thread()
        self._post(f"/internal/assistant/threads/{thread_id}/messages",
                   {"messages": [_user_message(), {"id": "m2", "role": "assistant",
                                                   "parts": [{"type": "text", "text": "Atsakymas"}]}]})
        on_question = self._post(f"/internal/assistant/threads/{thread_id}/feedback",
                                 {"message_id": "m1", "rating": -1})
        self.assertEqual(on_question.status_code, 404)
        self.assertIsNone(AssistantMessage.objects.get(thread_id=thread_id, id="m1").rating)

    def test_a_tied_pair_reads_question_first_whatever_its_ids(self):
        thread_id = self._create_thread()
        # A legacy batch: both rows share ONE stamp, and the
        # answer's id sorts BEFORE the question's
        from django.utils import timezone
        stamp = timezone.now()
        thread = AssistantThread.objects.get(id=thread_id)
        AssistantMessage.objects.create(thread=thread, id="zzz-question", format="aisdk-v7", created_at=stamp,
                                        content={"id": "zzz-question", "role": "user",
                                                 "parts": [{"type": "text", "text": "Klausimas?"}]})
        AssistantMessage.objects.create(thread=thread, id="aaa-answer", format="aisdk-v7", created_at=stamp,
                                        content={"id": "aaa-answer", "role": "assistant",
                                                 "parts": [{"type": "text", "text": "Atsakymas."}]})
        replay = self._get(f"/internal/assistant/threads/{thread_id}/messages").json()
        self.assertEqual([row["id"] for row in replay["messages"]], ["zzz-question", "aaa-answer"])


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
