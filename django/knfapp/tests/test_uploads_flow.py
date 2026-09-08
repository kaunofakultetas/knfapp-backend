############################################################
#  [*] Regression tests — the upload routes over the wire
#
#  Store → serve → delete against a throwaway upload
#  directory: the url shape the app persists, the ownership
#  row behind the quota, the owner-or-admin delete rule,
#  and the serve route's refusal to be a filesystem probe.
############################################################


import io
import shutil
import tempfile
import uuid


from PIL import Image
from django.test import Client, TestCase, override_settings


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now_iso
from knfapp.uploads import storage
from knfapp.uploads.models import Upload
from knfapp.users import auth
from .utils import bearer, create_user


def _jpg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (120, 0, 60)).save(buf, format="JPEG")
    buf.seek(0)
    buf.name = "nuotrauka.jpg"
    return buf


class UploadFlowTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.tmp = tempfile.mkdtemp(prefix="knfapp-uploads-")
        # The module caches the resolved dir per process — point it
        # at the throwaway one for the duration of each test
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))

        self.user = create_user()
        self.token = auth.mint_session(self.user.id)
        self.client = Client()

    def _upload(self, token=None, **extra):
        return self.client.post("/api/uploads", data={"file": _jpg_bytes(), **extra},
                                HTTP_AUTHORIZATION=f"Bearer {token or self.token}")


    def test_a_photo_lands_reencoded_under_a_uuid_name(self):
        response = self._upload()
        self.assertEqual(response.status_code, 201)
        body = response.json()
        # The relative url the client persists, and the row the
        # quota counts
        self.assertRegex(body["url"], r"^/api/uploads/[0-9a-f]{32}\.jpg$")
        self.assertEqual(body["mime"], "image/jpeg")
        self.assertEqual((body["width"], body["height"]), (8, 8))
        self.assertTrue(body["preview"].startswith("data:image/jpeg;base64,"))
        row = Upload.objects.get(filename=body["filename"])
        self.assertEqual(row.user_id, self.user.id)
        self.assertEqual(row.byte_size, body["size"])

        served = self.client.get(body["url"])
        self.assertEqual(served.status_code, 200)
        # Browser cache only — chat photos ride this public route
        self.assertEqual(served["Cache-Control"], "private, max-age=86400")

    def test_anonymous_uploads_are_refused(self):
        response = self.client.post("/api/uploads", data={"file": _jpg_bytes()})
        self.assertEqual(response.status_code, 401)

    def test_garbage_bytes_answer_the_slug_not_a_500(self):
        blob = io.BytesIO(b"tikrai ne paveikslas")
        blob.name = "photo.jpg"
        response = self.client.post("/api/uploads", data={"file": blob},
                                    HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "bad_file_content"))

    def test_a_text_document_is_stored_as_sent(self):
        blob = io.BytesIO("Tvarkaraštis rytoj.".encode())
        blob.name = "planas.txt"
        response = self.client.post("/api/uploads", data={"file": blob, "kind": "file"},
                                    HTTP_AUTHORIZATION=f"Bearer {self.token}")
        body = response.json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(body["mime"], "text/plain")
        self.assertEqual(body["name"], "planas.txt")
        served = self.client.get(body["url"])
        self.assertEqual(b"".join(served.streaming_content).decode(), "Tvarkaraštis rytoj.")

    def test_the_quota_answers_413_before_writing(self):
        Upload.objects.create(id=str(uuid.uuid4()), filename=f"{uuid.uuid4().hex}.jpg",
                              user=self.user, byte_size=storage.UPLOAD_QUOTA_BYTES,
                              created_at=utc_now_iso())
        response = self._upload()
        self.assertEqual((response.status_code, response.json()["code"]), (413, "quota_exceeded"))

    def test_serve_refuses_names_it_could_not_have_written(self):
        for name in ("../knfapp.sqlite3", "..%2Fdb", "x.jpg", "a" * 32 + ".exe"):
            response = self.client.get(f"/api/uploads/{name}")
            self.assertIn(response.status_code, (400, 404), name)

    def test_delete_is_owner_or_admin_only(self):
        uploaded = self._upload().json()["filename"]

        stranger = create_user(username="kitas", email="kitas@knf.vu.lt")
        stranger_token = auth.mint_session(stranger.id)
        admin = create_user(username="vadovas", email="vadovas@knf.vu.lt", role="admin")
        admin_token = auth.mint_session(admin.id)

        self.assertEqual(bearer(self.client.delete, f"/api/uploads/{uploaded}", stranger_token).status_code, 403)
        self.assertEqual(bearer(self.client.delete, f"/api/uploads/{uploaded}", self.token).status_code, 200)
        self.assertEqual(Upload.objects.filter(filename=uploaded).count(), 0)

        # An unknown name and someone else's ownerless file look the
        # same from outside — admins get through
        second = self._upload().json()["filename"]
        self.assertEqual(bearer(self.client.delete, f"/api/uploads/{second}", admin_token).status_code, 200)
        self.assertEqual(bearer(self.client.delete, "/api/uploads/" + "0" * 32 + ".jpg", stranger_token).status_code, 404)
