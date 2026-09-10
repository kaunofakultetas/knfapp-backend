############################################################
#  [*] Regression tests — PUT /me and change-password
#
#  The profile update's whitelist rules (avatar paths,
#  camelCase precedence, blank-vs-absent) and the password
#  rotation's two security decisions: the wrong-old answer
#  is a 400 (never the session-killing 401) and every OTHER
#  session dies with the rotation.
############################################################


import shutil
import tempfile


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.uploads import storage
from knfapp.users import auth
from knfapp.users.models import Session, User
from .utils import PASSWORD, bearer, create_user


class UpdateMeTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _put(self, body, token=None):
        return bearer(self.client.put, "/api/auth/me", token or self.token,
                      data=body, content_type="application/json")

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

    def test_a_replaced_own_avatar_is_deleted_from_disk(self):
        tmp = tempfile.mkdtemp(prefix="knfapp-av-")
        storage._upload_dir = tmp
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))

        old_name = "a" * 32 + ".jpg"
        open(f"{tmp}/{old_name}", "wb").write(b"x")
        User.objects.filter(id=self.user.id).update(avatar_url=f"/api/uploads/{old_name}")
        # Re-mint so request.user carries the old avatar
        token = auth.mint_session(self.user.id)

        response = self._put({"avatarUrl": "/api/uploads/" + "b" * 32 + ".jpg"}, token=token)
        self.assertEqual(response.status_code, 200)
        import os
        self.assertFalse(os.path.exists(f"{tmp}/{old_name}"))

    def test_student_fields_blank_or_null_store_null(self):
        response = self._put({"studentNumber": "  ", "studyGroup": "IS-3", "studyProgram": None})
        body = response.json()
        self.assertIsNone(body["studentNumber"])
        self.assertEqual(body["studyGroup"], "IS-3")
        self.assertIsNone(body["studyProgram"])

    def test_an_empty_update_is_a_400(self):
        self.assertEqual(self._put({}).status_code, 400)
        self.assertEqual(self._put({"unknownField": "x"}).status_code, 400)


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
