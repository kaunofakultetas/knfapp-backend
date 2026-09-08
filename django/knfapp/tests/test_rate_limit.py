############################################################
#  [*] Regression tests — the write quotas
#
#  common/ratelimit.py and the two decisions the auth flows
#  hang on it: budgets are probed before they are spent
#  (honest traffic never eats itself), and login's buckets
#  count FAILURES only. Time is driven through time.time
#  patching — no sleeping suites.
############################################################


import json
from unittest.mock import patch


from django.test import Client, TestCase


from knfapp.common import ratelimit
from .utils import PASSWORD, create_user


class LimiterTests(TestCase):

    def setUp(self):
        ratelimit.reset()

    def test_the_budget_is_per_window_and_ages_out(self):
        with patch("knfapp.common.ratelimit.time") as clock:
            clock.time.return_value = 1000.0
            for _ in range(ratelimit.MAX_ATTEMPTS):
                self.assertFalse(ratelimit.check("k"))
            self.assertTrue(ratelimit.check("k"))

            # One second past the window the oldest attempt ages out
            clock.time.return_value = 1000.0 + ratelimit.WINDOW + 1
            self.assertFalse(ratelimit.check("k"))

    def test_probe_does_not_spend(self):
        for _ in range(ratelimit.MAX_ATTEMPTS * 2):
            self.assertFalse(ratelimit.check("k", record=False))

    def test_the_lru_ceiling_bounds_distinct_keys(self):
        for i in range(ratelimit.MAX_KEYS + 50):
            ratelimit.check(f"k{i}")
        with ratelimit._lock:
            self.assertLessEqual(len(ratelimit._store), ratelimit.MAX_KEYS)

    def test_the_429_carries_retry_after(self):
        with patch("knfapp.common.ratelimit.time") as clock:
            clock.time.return_value = 1000.0
            ratelimit.record("k")
            clock.time.return_value = 1100.0
            response = ratelimit.limited_response("Palaukite.", "k")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.content)["code"], "rate_limited")
        # 300s window − 100s elapsed, +1 rounding — never zero
        self.assertEqual(response["Retry-After"], "201")


class LoginBucketTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()

    def _login(self, password):
        return self.client.post("/api/auth/login", data={"username": "tomas", "password": password},
                                content_type="application/json")

    def test_only_failures_fill_the_identifier_bucket(self):
        create_user(username="tomas")
        for _ in range(ratelimit.MAX_ATTEMPTS + 2):
            self.assertEqual(self._login(PASSWORD).status_code, 200)

        for _ in range(ratelimit.MAX_ATTEMPTS):
            self.assertEqual(self._login("neteisingas").status_code, 401)
        self.assertEqual(self._login("neteisingas").status_code, 429)

        # The lock protects the ACCOUNT identifier, not the password
        self.assertEqual(self._login(PASSWORD).status_code, 429)

    def test_malformed_register_bodies_never_spend_the_budget(self):
        for _ in range(ratelimit.MAX_ATTEMPTS * 2):
            response = self.client.post("/api/auth/register", data={"username": "x"},
                                        content_type="application/json")
            self.assertEqual(response.status_code, 400)
