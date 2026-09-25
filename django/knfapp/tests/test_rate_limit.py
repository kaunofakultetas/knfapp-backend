############################################################
#  [*] Regression tests — the write quotas
#
#  common/ratelimit.py and the decisions the auth flows
#  hang on it: a slot is RESERVED in one lock hold before
#  the bcrypt work (24 threads at one 10-budget key get
#  exactly ten through — a probe that records afterwards
#  lets all 24 in), a right password refunds it and a wrong
#  one keeps it, and a malformed body never spends. Time is
#  driven by patching the monotonic clock — no sleeping
#  suites.
############################################################


import json
import threading
from unittest.mock import patch


import bcrypt
from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import PASSWORD, bearer, create_user


def _stamps(key):
    with ratelimit._lock:
        return len(ratelimit._store.get(key, []))


class LimiterTests(TestCase):

    def setUp(self):
        ratelimit.reset()

    def test_the_budget_is_per_window_and_ages_out(self):
        with patch("knfapp.common.ratelimit.time") as clock:
            clock.monotonic.return_value = 1000.0
            for _ in range(ratelimit.MAX_ATTEMPTS):
                self.assertFalse(ratelimit.check("k"))
            self.assertTrue(ratelimit.check("k"))

            # One second past the window the oldest attempt ages out
            clock.monotonic.return_value = 1000.0 + ratelimit.WINDOW + 1
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
            clock.monotonic.return_value = 1000.0
            ratelimit.record("k")
            clock.monotonic.return_value = 1100.0
            response = ratelimit.limited_response("Palaukite.", "k")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.content)["code"], "rate_limited")
        # 300s window − 100s elapsed, +1 rounding — never zero
        self.assertEqual(response["Retry-After"], "201")

    def test_the_window_runs_on_the_monotonic_clock(self):
        # A wall-clock step (NTP, a DST-confused host) must neither
        # empty nor freeze a window — the stamps never touch time.time
        with patch("knfapp.common.ratelimit.time") as clock:
            clock.monotonic.return_value = 5000.0
            self.assertTrue(ratelimit.reserve("k"))
            clock.time.assert_not_called()
        with ratelimit._lock:
            self.assertEqual(ratelimit._store["k"], [5000.0])


class ReserveTests(TestCase):

    def setUp(self):
        ratelimit.reset()

    def test_a_burst_gets_exactly_the_budget_through(self):
        # 24 threads released together at one empty 10-budget key —
        # the gap between a probe and a later record would let all
        # 24 through; reserve judges and appends under one lock hold
        threads = 24
        gate = threading.Barrier(threads)
        verdicts = []
        verdict_lock = threading.Lock()

        def attempt():
            gate.wait()
            verdict = ratelimit.reserve("k", 10)
            with verdict_lock:
                verdicts.append(verdict)

        workers = [threading.Thread(target=attempt) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual(verdicts.count(True), 10)
        self.assertEqual(verdicts.count(False), 14)
        # A refusal appended nothing — the ten stamps ARE the window
        self.assertEqual(_stamps("k"), 10)

    def test_refund_hands_one_slot_back_and_never_goes_negative(self):
        for _ in range(3):
            self.assertTrue(ratelimit.reserve("k", 3))
        self.assertFalse(ratelimit.reserve("k", 3))

        ratelimit.refund("k")
        self.assertTrue(ratelimit.reserve("k", 3))
        self.assertFalse(ratelimit.reserve("k", 3))

        # An unknown key and an emptied one are no-ops — never an
        # error, never a credit beyond the budget
        ratelimit.refund("nera")
        for _ in range(5):
            ratelimit.refund("k")
        self.assertEqual(_stamps("k"), 0)
        self.assertTrue(ratelimit.reserve("k", 1))
        self.assertFalse(ratelimit.reserve("k", 1))


class ReservedSlotTests(TestCase):
    # The slot is HELD while bcrypt runs: a spy on checkpw reads the
    # bucket at the moment of the check. A probe-then-record flow
    # holds nothing there — that is the whole 24-thread gap — and
    # records only after the verdict

    def setUp(self):
        ratelimit.reset()
        self.client = Client()
        self.user = create_user(username="tomas")
        self.token = auth.mint_session(self.user.id)

    def _spy(self, key, seen):
        real = bcrypt.checkpw

        def checkpw(password, hashed):
            seen.append(_stamps(key))
            return real(password, hashed)
        return checkpw

    def _login(self, password):
        return self.client.post("/api/auth/login", data={"username": "tomas", "password": password},
                                content_type="application/json")

    def test_login_holds_both_slots_during_bcrypt_and_refunds_a_match(self):
        seen = []
        with patch("knfapp.users.api.auth_views.bcrypt.checkpw", new=self._spy("login:id:tomas", seen)):
            wrong = self._login("neteisingas")
            right = self._login(PASSWORD)
        self.assertEqual((wrong.status_code, right.status_code), (401, 200))
        # Reserved before the check both times — the second sees the
        # first's kept slot plus its own…
        self.assertEqual(seen, [1, 2])
        # …and a right password hands its slot back, in both buckets
        self.assertEqual(_stamps("login:id:tomas"), 1)
        self.assertEqual(_stamps("login:127.0.0.1"), 1)

    def test_change_password_and_delete_me_hold_the_shared_slot_during_bcrypt(self):
        seen = []
        key = f"chpass:{self.user.id}"
        with patch("knfapp.users.api.auth_views.bcrypt.checkpw", new=self._spy(key, seen)):
            wrong = bearer(self.client.post, "/api/auth/change-password", self.token,
                           data={"old_password": "neteisingas", "new_password": "naujas-geras-2026"},
                           content_type="application/json")
            wrong_delete = bearer(self.client.delete, "/api/auth/me", self.token,
                                  data=json.dumps({"password": "neteisingas"}), content_type="application/json")
            right = bearer(self.client.post, "/api/auth/change-password", self.token,
                           data={"old_password": PASSWORD, "new_password": "naujas-geras-2026"},
                           content_type="application/json")
        self.assertEqual((wrong.status_code, wrong_delete.status_code, right.status_code), (400, 400, 200))
        # One pooled bucket: each check sees every kept slot plus its
        # own reservation
        self.assertEqual(seen, [1, 2, 3])
        # The two wrong guesses kept theirs, the match gave its back
        self.assertEqual(_stamps(key), 2)

    def test_register_takes_its_slot_only_after_the_body_validates(self):
        # Over budget, a malformed body is still the 400 it always
        # was — the gate sits AFTER validation, so a slot is only ever
        # taken for a body that could have made an account; and a
        # 201 keeps its slot (the budget caps account creation)
        for _ in range(ratelimit.MAX_ATTEMPTS):
            ratelimit.record("register:127.0.0.1")
        malformed = self.client.post("/api/auth/register", data={"username": "x"},
                                     content_type="application/json")
        self.assertEqual(malformed.status_code, 400)
        whole = {"username": "jonas", "password": "saugus-2026", "display_name": "Jonas", "email": "jonas@knf.vu.lt"}
        limited = self.client.post("/api/auth/register", data=whole, content_type="application/json")
        self.assertEqual(limited.status_code, 429)

        ratelimit.reset()
        created = self.client.post("/api/auth/register", data=whole, content_type="application/json")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(_stamps("register:127.0.0.1"), 1)


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

    def test_a_refused_identifier_hands_the_ip_slot_back(self):
        # The 429 spends nothing: the IP bucket must not fill up with
        # attempts the identifier bucket already refused
        create_user(username="tomas")
        for _ in range(ratelimit.MAX_ATTEMPTS):
            self._login("neteisingas")
        self.assertEqual(_stamps("login:127.0.0.1"), ratelimit.MAX_ATTEMPTS)
        self.assertEqual(self._login("neteisingas").status_code, 429)
        self.assertEqual(_stamps("login:127.0.0.1"), ratelimit.MAX_ATTEMPTS)

    def test_malformed_register_bodies_never_spend_the_budget(self):
        for _ in range(ratelimit.MAX_ATTEMPTS * 2):
            response = self.client.post("/api/auth/register", data={"username": "x"},
                                        content_type="application/json")
            self.assertEqual(response.status_code, 400)
