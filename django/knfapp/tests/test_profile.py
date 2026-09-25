############################################################
#  [*] Regression tests — PUT /me and change-password
#
#  The profile update's whitelist rules (avatar paths AND
#  their owner — somebody else's registered upload is 400
#  upload_not_owned, the current avatar sent back unchanged
#  a no-op; camelCase precedence, blank-vs-absent), the
#  replaced avatar's cleanup on the commit, the PUT's own
#  rate limit (the polled GET is never limited), the parity
#  of the two profile routes (one shared routine), and the
#  password rotation's two security decisions: the
#  wrong-old answer is a 400 (never the session-killing
#  401) and every OTHER session dies with the rotation.
############################################################


import os
import shutil
import tempfile


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth
from knfapp.users.models import Session, User
from .utils import PASSWORD, bearer, create_user, register_upload


class UpdateMeTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _put(self, body, token=None):
        return bearer(self.client.put, "/api/auth/me", token or self.token,
                      data=body, content_type="application/json")

    def _throwaway_upload_dir(self):
        tmp = tempfile.mkdtemp(prefix="knfapp-av-")
        storage._upload_dir = tmp
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))
        return tmp

    def test_camel_case_wins_and_the_answer_is_the_reread_row(self):
        response = self._put({"displayName": "  Tomas V.  ", "display_name": "ignored"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["displayName"], "Tomas V.")
        self.assertEqual(User.objects.get(id=self.user.id).display_name, "Tomas V.")

    def test_avatar_accepts_own_uploads_or_clearing_only(self):
        bad = self._put({"avatarUrl": "https://evil.example/x.jpg"})
        self.assertEqual(bad.status_code, 400)
        cleared = self._put({"avatarUrl": None})
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.json()["avatarUrl"])

    def test_the_avatar_must_be_the_callers_own_registered_upload(self):
        tmp = self._throwaway_upload_dir()
        other = create_user(username="kitas", email="kitas@knf.vu.lt")
        foreign = register_upload(tmp, other)
        own = register_upload(tmp, self.user)

        refused = self._put({"avatarUrl": f"/api/uploads/{foreign}"})
        self.assertEqual((refused.status_code, refused.json()["code"]), (400, "upload_not_owned"))
        self.assertIsNone(User.objects.get(id=self.user.id).avatar_url)

        accepted = self._put({"avatarUrl": f"/api/uploads/{own}"})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["avatarUrl"], f"/api/uploads/{own}")

        # The whole profile PUT back: the current avatar, unchanged,
        # is a no-op — even once its ledger row is gone (SET_NULL
        # after an erasure, or a pre-ledger file)
        Upload.objects.filter(filename=own).delete()
        again = self._put({"avatarUrl": f"/api/uploads/{own}", "displayName": "Tomas"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["avatarUrl"], f"/api/uploads/{own}")

    def test_a_replaced_own_avatar_is_deleted_from_disk(self):
        tmp = self._throwaway_upload_dir()
        # Both registered to the user: the sink refuses a rowless
        # old file, and the acceptance refuses a rowless new one
        old_name = register_upload(tmp, self.user)
        new_name = register_upload(tmp, self.user)
        User.objects.filter(id=self.user.id).update(avatar_url=f"/api/uploads/{old_name}")

        # The unlink rides the commit
        with self.captureOnCommitCallbacks(execute=True):
            response = self._put({"avatarUrl": f"/api/uploads/{new_name}"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(tmp, old_name)))
        self.assertFalse(Upload.objects.filter(filename=old_name).exists())
        self.assertTrue(os.path.exists(os.path.join(tmp, new_name)))

    def test_student_fields_blank_or_null_store_null(self):
        response = self._put({"studentNumber": "  ", "studyGroup": "IS-3", "studyProgram": None})
        body = response.json()
        self.assertIsNone(body["studentNumber"])
        self.assertEqual(body["studyGroup"], "IS-3")
        self.assertIsNone(body["studyProgram"])

    def test_an_empty_update_is_a_400(self):
        self.assertEqual(self._put({}).status_code, 400)
        self.assertEqual(self._put({"unknownField": "x"}).status_code, 400)

    def test_the_31st_update_is_a_429_while_the_polled_get_is_not(self):
        for i in range(30):
            self.assertEqual(self._put({"displayName": f"Tomas {i}"}).status_code, 200, i)
        limited = self._put({"displayName": "Tomas 31"})
        self.assertEqual((limited.status_code, limited.json()["code"]), (429, "rate_limited"))
        self.assertEqual(bearer(self.client.get, "/api/auth/me", self.token).status_code, 200)

    def test_both_profile_routes_answer_the_same_shape(self):
        # One routine behind PUT /api/auth/me and PUT
        # /api/social/profile — the same patch must come back with
        # the same keys and values from either
        patch = {"displayName": "Tomas V.", "studyGroup": "IS-3", "studentNumber": None}
        via_auth = self._put(patch)
        via_social = bearer(self.client.put, "/api/social/profile", self.token,
                            data=patch, content_type="application/json")
        self.assertEqual((via_auth.status_code, via_social.status_code), (200, 200))
        self.assertEqual(via_auth.json(), via_social.json())
        self.assertEqual(set(via_auth.json()), {
            "id", "username", "email", "displayName", "role", "avatarUrl",
            "invited", "studentNumber", "studyGroup", "studyProgram",
        })


class ChangePasswordTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _change(self, old, new, token=None):
        return bearer(self.client.post, "/api/auth/change-password", token or self.token,
                      data={"old_password": old, "new_password": new},
                      content_type="application/json")

    def test_wrong_old_password_is_a_400_never_the_session_killing_401(self):
        response = self._change("neteisingas", "naujas-geras-2026")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_credentials"))

    def test_rotation_keeps_only_the_presented_session(self):
        other = auth.mint_session(self.user.id)
        response = self._change(PASSWORD, "naujas-geras-2026")
        self.assertEqual(response.status_code, 200)
        # The rotating device survives, the other dies
        self.assertEqual(bearer(self.client.get, "/api/auth/me", self.token).status_code, 200)
        self.assertEqual(bearer(self.client.get, "/api/auth/me", other).status_code, 401)
        self.assertEqual(Session.objects.filter(user_id=self.user.id).count(), 1)
        # And the new password is live
        login = self.client.post("/api/auth/login",
                                 data={"username": self.user.username, "password": "naujas-geras-2026"},
                                 content_type="application/json")
        self.assertEqual(login.status_code, 200)

    def test_the_new_password_passes_the_register_policy(self):
        response = self._change(PASSWORD, "123456")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "password_too_common"))

    def test_failures_only_fill_the_budget(self):
        for _ in range(ratelimit.MAX_ATTEMPTS):
            self._change("neteisingas", "naujas-geras-2026")
        self.assertEqual(self._change("neteisingas", "naujas-geras-2026").status_code, 429)
