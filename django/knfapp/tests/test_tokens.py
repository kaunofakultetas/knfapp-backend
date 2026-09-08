############################################################
#  [*] Regression tests — the bearer-session scheme
#
#  users/auth.py top to bottom: what the sessions table
#  stores, how a bearer resolves, when it stops resolving,
#  and what dies with it. These pin the security decisions
#  a rewrite must not lose — sha256 at rest, lazy expiry
#  purge taking push tokens with it, the deactivation
#  backstop, the per-user session cap.
############################################################


import uuid
from datetime import datetime, timedelta, timezone


from django.test import RequestFactory, TestCase


from knfapp.common.timestamps import utc_now_iso
from knfapp.notifications.models import PushToken
from knfapp.users import auth
from knfapp.users.models import Session
from .utils import bearer, create_user


def _session_for(user, token, expires_in_days=30, expires_at=None):
    return Session.objects.create(
        id=str(uuid.uuid4()),
        user=user,
        token=auth.hash_token(token),
        created_at=utc_now_iso(),
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat(),
    )


def _push_for(user):
    return PushToken.objects.create(
        id=str(uuid.uuid4()), user=user, token=f"ExponentPushToken[{uuid.uuid4().hex[:12]}]",
        created_at=utc_now_iso(), updated_at=utc_now_iso(),
    )








############################################################
# What the table stores
############################################################

class TokenAtRestTests(TestCase):

    def test_mint_session_stores_the_sha256_never_the_raw_token(self):
        user = create_user()
        token = auth.mint_session(user.id)
        row = Session.objects.get(user=user)
        self.assertEqual(row.token, auth.hash_token(token))
        self.assertNotIn(token, row.token)

    def test_login_trims_to_the_newest_sessions_per_user(self):
        user = create_user()
        for _ in range(auth.SESSIONS_PER_USER + 3):
            auth.mint_session(user.id)
        self.assertEqual(Session.objects.filter(user=user).count(), auth.SESSIONS_PER_USER)








############################################################
# How a bearer resolves
############################################################

class ResolveTests(TestCase):

    def test_live_token_resolves_to_the_public_columns_only(self):
        user = create_user()
        _session_for(user, "raw-token")
        resolved = auth.resolve_session_token("raw-token")
        self.assertEqual(resolved["id"], user.id)
        self.assertNotIn("password_hash", resolved)

    def test_unknown_token_is_none(self):
        self.assertIsNone(auth.resolve_session_token("no-such-token"))

    def test_expired_token_is_purged_with_the_owners_push_tokens(self):
        user = create_user()
        _session_for(user, "old-token", expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        _push_for(user)
        self.assertIsNone(auth.resolve_session_token("old-token"))
        self.assertEqual(Session.objects.count(), 0)
        self.assertEqual(PushToken.objects.count(), 0)

    def test_naive_legacy_expiry_is_read_as_utc(self):
        user = create_user()
        # Naive text a legacy row could carry — one hour in the future
        naive = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        _session_for(user, "legacy-token", expires_at=naive)
        self.assertIsNotNone(auth.resolve_session_token("legacy-token"))

    def test_malformed_expiry_counts_as_expired_never_a_500(self):
        user = create_user()
        _session_for(user, "broken-token", expires_at="pirmadienis")
        self.assertIsNone(auth.resolve_session_token("broken-token"))

    def test_deactivated_account_is_locked_out_on_a_live_session(self):
        user = create_user(active=0)
        _session_for(user, "raw-token")
        self.assertIsNone(auth.resolve_session_token("raw-token"))








############################################################
# The header and the request cache
############################################################

class BearerHeaderTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()

    def _request_with(self, header):
        return self.factory.get("/api/auth/me", HTTP_AUTHORIZATION=header)

    def test_scheme_is_case_insensitive_and_token_is_trimmed(self):
        self.assertEqual(auth.bearer_token(self._request_with("BEARER  abc ")), "abc")
        self.assertEqual(auth.bearer_token(self._request_with("bearer abc")), "abc")

    def test_other_schemes_and_empty_tokens_are_none(self):
        self.assertIsNone(auth.bearer_token(self._request_with("Basic abc")))
        self.assertIsNone(auth.bearer_token(self._request_with("Bearer ")))
        self.assertIsNone(auth.bearer_token(self.factory.get("/api/auth/me")))

    def test_one_lookup_per_request_negative_results_included(self):
        request = self._request_with("Bearer no-such-token")
        self.assertIsNone(auth.get_current_user(request))
        # The cache holds the negative — a second call must not hit the DB
        with self.assertNumQueries(0):
            self.assertIsNone(auth.get_current_user(request))








############################################################
# The route gates, through the real URLconf
############################################################

class GateTests(TestCase):

    def test_me_is_401_without_a_token_and_200_with_one(self):
        user = create_user()
        _session_for(user, "raw-token")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
        response = bearer(self.client.get, "/api/auth/me", "raw-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], user.username)

    def test_serialize_user_is_the_whitelist(self):
        user = create_user()
        _session_for(user, "raw-token")
        body = bearer(self.client.get, "/api/auth/me", "raw-token").json()
        self.assertEqual(
            set(body),
            {"id", "username", "email", "displayName", "role", "avatarUrl",
             "invited", "studentNumber", "studyGroup", "studyProgram"},
        )
