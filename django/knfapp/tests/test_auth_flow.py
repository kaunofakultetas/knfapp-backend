############################################################
#  [*] Regression tests — the account flows over the wire
#
#  register → me → login → logout through the real URLconf
#  and middleware, pinning the CONTRACT the mobile app
#  depends on: paths, bodies, status codes and the machine
#  `code` slugs it translates. Not a re-test of every input
#  permutation — each case pins one decision that must
#  survive rewrites.
############################################################


from unittest import mock


from django.db import IntegrityError
from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.users.api import auth_views
from knfapp.users.models import InvitationCode, Session, User
from .utils import PASSWORD, bearer, create_invite, create_user


def _register_body(**overrides):
    body = {
        "username": "jonas",
        "password": "saugus-2026",
        "display_name": "Jonas",
        "email": "Jonas@KNF.vu.lt",
    }
    body.update(overrides)
    return body


class AuthFlowTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.client = Client()

    def _register(self, **overrides):
        return self.client.post("/api/auth/register", data=_register_body(**overrides),
                                content_type="application/json")


    # ---- register --------------------------------------

    def test_register_signs_the_user_in_and_lowercases_the_email(self):
        response = self._register()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        # The canonical email shape — login matches case-insensitively
        self.assertEqual(body["user"]["email"], "jonas@knf.vu.lt")
        self.assertEqual(body["user"]["role"], "student")
        self.assertFalse(body["user"]["invited"])
        # The token works immediately
        me = bearer(self.client.get, "/api/auth/me", body["token"])
        self.assertEqual(me.status_code, 200)

    def test_register_slugs_reach_the_client(self):
        cases = [
            (_register_body(username="x"), "invalid_username"),
            (_register_body(email="ne-pastas"), "invalid_email"),
            (_register_body(password="12345"), "password_too_short"),
            (_register_body(password="Jonas-slaptas"), "password_contains_username"),
        ]
        for body, slug in cases:
            response = self.client.post("/api/auth/register", data=body, content_type="application/json")
            self.assertEqual((response.status_code, response.json().get("code")), (400, slug), slug)

    def test_taken_name_is_one_409_case_insensitively(self):
        create_user(username="Tomas", email="tomas@knf.vu.lt")
        response = self._register(username="tomas", email="kitas@knf.vu.lt")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "username_taken"))

    def test_a_given_bad_code_is_a_400_never_a_silent_downgrade(self):
        response = self._register(invitation_code="NETIKRAS")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invite_invalid"))
        self.assertEqual(User.objects.count(), 0)

    def test_a_valid_code_grants_its_role_and_burns_one_use(self):
        create_invite(code="MOKYTOJUI", role="teacher", max_uses=2)
        response = self._register(invitation_code="MOKYTOJUI")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["user"]["role"], "teacher")
        self.assertTrue(response.json()["user"]["invited"])
        self.assertEqual(InvitationCode.objects.get(code="MOKYTOJUI").use_count, 1)

    def test_the_last_use_cannot_be_taken_twice(self):
        create_invite(code="PASKUTINIS", max_uses=1, use_count=1)
        response = self._register(invitation_code="PASKUTINIS")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invite_exhausted"))

    def test_a_taken_username_does_not_burn_the_invitation(self):
        # A 4xx return commits under ATOMIC_REQUESTS — a typo in the
        # username must not spend a single-use curator code
        create_user(username="Tomas", email="tomas@knf.vu.lt")
        create_invite(code="VIENKARTINIS", role="curator", max_uses=1)
        response = self._register(username="tomas", email="kitas@knf.vu.lt", invitation_code="VIENKARTINIS")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "username_taken"))
        self.assertEqual(InvitationCode.objects.get(code="VIENKARTINIS").use_count, 0)

        # …and the honest retry still gets its one use
        response = self._register(username="tomas2", email="kitas@knf.vu.lt", invitation_code="VIENKARTINIS")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["user"]["role"], "curator")
        self.assertEqual(InvitationCode.objects.get(code="VIENKARTINIS").use_count, 1)

    def test_the_race_behind_the_pre_check_discards_the_burn_too(self):
        # The INSERT's IntegrityError is the pre-check's race. SQLite
        # keeps the transaction usable after it, so without an
        # explicit rollback mark the burn would commit behind the 409
        create_invite(code="LENKTYNES", role="curator", max_uses=1)
        with mock.patch.object(auth_views.User.objects, "create", side_effect=IntegrityError("users.username")):
            response = self._register(invitation_code="LENKTYNES")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "username_taken"))
        self.assertEqual(InvitationCode.objects.get(code="LENKTYNES").use_count, 0)
        self.assertEqual(User.objects.count(), 0)


    # ---- validate-code ---------------------------------

    def test_validate_code_answers_200_either_way_with_the_reason(self):
        create_invite(code="GALIOJA", role="curator", max_uses=3, use_count=1)
        ok = self.client.post("/api/auth/validate-code", data={"code": "GALIOJA"},
                              content_type="application/json").json()
        self.assertEqual(ok, {"valid": True, "role": "curator", "remainingUses": 2})

        bad = self.client.post("/api/auth/validate-code", data={"code": "NERA"},
                               content_type="application/json")
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(bad.json()["reason"], "unknown")


    # ---- login -----------------------------------------

    def test_login_accepts_username_or_email_any_case(self):
        create_user(username="Migle", email="migle@knf.vu.lt")
        for identifier in ("migle", "MIGLE", "Migle@KNF.vu.lt"):
            response = self.client.post("/api/auth/login", data={"username": identifier, "password": PASSWORD},
                                        content_type="application/json")
            self.assertEqual(response.status_code, 200, identifier)

    def test_unknown_user_and_wrong_password_share_one_401(self):
        create_user(username="tomas")
        wrong = self.client.post("/api/auth/login", data={"username": "tomas", "password": "neteisingas"},
                                 content_type="application/json")
        unknown = self.client.post("/api/auth/login", data={"username": "nera-tokio", "password": "neteisingas"},
                                   content_type="application/json")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertEqual(wrong.json(), unknown.json())

    def test_deactivation_is_disclosed_only_after_the_password_matches(self):
        create_user(username="tomas", active=0)
        right = self.client.post("/api/auth/login", data={"username": "tomas", "password": PASSWORD},
                                 content_type="application/json")
        wrong = self.client.post("/api/auth/login", data={"username": "tomas", "password": "neteisingas"},
                                 content_type="application/json")
        self.assertEqual((right.status_code, right.json()["code"]), (403, "account_deactivated"))
        self.assertEqual(wrong.status_code, 401)

    def test_an_unusable_stored_hash_is_a_401_never_a_500(self):
        create_user(username="tomas", password_hash="ne-bcrypt")
        response = self.client.post("/api/auth/login", data={"username": "tomas", "password": PASSWORD},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 401)


    # ---- logout ----------------------------------------

    def test_logout_kills_only_the_presented_session(self):
        body = self._register().json()
        token = body["token"]
        second = self.client.post("/api/auth/login", data={"username": "jonas", "password": "saugus-2026"},
                                  content_type="application/json").json()["token"]

        response = bearer(self.client.post, "/api/auth/logout", token, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(bearer(self.client.get, "/api/auth/me", token).status_code, 401)
        self.assertEqual(bearer(self.client.get, "/api/auth/me", second).status_code, 200)

    def test_logout_all_leaves_no_session_behind(self):
        body = self._register().json()
        self.client.post("/api/auth/login", data={"username": "jonas", "password": "saugus-2026"},
                         content_type="application/json")
        response = bearer(self.client.post, "/api/auth/logout-all", body["token"], content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Session.objects.count(), 0)
