############################################################
#  [*] Regression tests — the versioned prompt store
#
#  Jauka's discipline, pinned: versions mint monotonically
#  and are never edited (a fix is a new version), at most
#  one row is active with activation switching atomically
#  and deactivation a valid state, the internal endpoint
#  serves the active appendix (empty when none) behind the
#  shared-secret gate, the admin routes demand the admin
#  role and audit every mutation, the turn log stores the
#  prompt_version the container stamps, and the size cap
#  refuses a novel.
############################################################


import json


from django.test import Client, TestCase, override_settings


from knfapp.admin.models import AdminAudit
from knfapp.assistant.models import AssistantPrompt, AssistantTurn
from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import bearer, create_user


SECRET = "test-internal-secret"


@override_settings(ASSISTANT_INTERNAL_SECRET=SECRET)
class AssistantPromptTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin = create_user(username="vadovas", role="admin")
        self.token = auth.mint_session(self.admin.id)
        self.client = Client()

    def _post(self, path, body):
        return bearer(self.client.post, path, self.token,
                      data=json.dumps(body), content_type="application/json")

    def _active(self):
        return self.client.get("/internal/assistant/prompt",
                               HTTP_X_INTERNAL_SECRET=SECRET).json()


    def test_versions_mint_monotonically_and_activation_switches_one_winner(self):
        first = self._post("/api/admin/assistant/prompts",
                           {"text": "Visada pasisveikink.", "activate": True})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json()["version"], 1)
        self.assertEqual(self._active(), {"version": 1, "text": "Visada pasisveikink."})

        second = self._post("/api/admin/assistant/prompts",
                            {"text": "Nauja gairė.", "notes": "sezonui"})
        self.assertEqual(second.json()["version"], 2)
        # Saving without activate leaves v1 live
        self.assertEqual(self._active()["version"], 1)

        self._post("/api/admin/assistant/prompts/2/activate", {})
        self.assertEqual(self._active(), {"version": 2, "text": "Nauja gairė."})
        self.assertEqual(AssistantPrompt.objects.filter(active=True).count(), 1)

        self._post("/api/admin/assistant/prompts/deactivate", {})
        self.assertEqual(self._active(), {"version": None, "text": ""})

    def test_history_lists_newest_first_and_mutations_audit(self):
        self._post("/api/admin/assistant/prompts", {"text": "a", "activate": True})
        self._post("/api/admin/assistant/prompts", {"text": "b", "activate": True})

        listing = bearer(self.client.get, "/api/admin/assistant/prompts", self.token).json()
        self.assertEqual([row["version"] for row in listing["prompts"]], [2, 1])
        self.assertEqual([row["active"] for row in listing["prompts"]], [True, False])

        actions = list(AdminAudit.objects.values_list("action", flat=True))
        self.assertEqual(actions.count("assistant_prompt_create"), 2)

    def test_guards_role_size_and_missing_version(self):
        student = create_user(username="jonas", role="student")
        student_token = auth.mint_session(student.id)
        refused = bearer(self.client.post, "/api/admin/assistant/prompts", student_token,
                         data=json.dumps({"text": "x"}), content_type="application/json")
        self.assertEqual(refused.status_code, 403)

        too_big = self._post("/api/admin/assistant/prompts", {"text": "x" * 9000})
        self.assertEqual(too_big.status_code, 400)
        missing = self._post("/api/admin/assistant/prompts/9/activate", {})
        self.assertEqual(missing.status_code, 404)

        naked = self.client.get("/internal/assistant/prompt")
        self.assertEqual(naked.status_code, 403)

    def test_turn_log_stores_the_stamped_prompt_version(self):
        self.client.post("/internal/assistant/turn-log",
                         data=json.dumps({"model": "m", "outcome": "ok", "prompt_version": 3}),
                         content_type="application/json",
                         HTTP_X_INTERNAL_SECRET=SECRET)
        self.client.post("/internal/assistant/turn-log",
                         data=json.dumps({"model": "m", "outcome": "ok", "prompt_version": "bad"}),
                         content_type="application/json",
                         HTTP_X_INTERNAL_SECRET=SECRET)
        stamps = list(AssistantTurn.objects.order_by("id").values_list("prompt_version", flat=True))
        self.assertEqual(stamps, [3, None])
