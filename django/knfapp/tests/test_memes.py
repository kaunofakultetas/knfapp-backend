############################################################
#  [*] Regression tests — the shared meme library
#
#  The two-treatment rule (a GIF keeps its bytes, a static
#  image clears the uploads re-encode), the folded
#  Lithuanian search, pusher-or-admin removal taking the
#  file with the row, and the public serve gate.
############################################################


import io
import shutil
import tempfile


from PIL import Image
from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.memes.api import views as meme_views
from knfapp.memes.models import Meme
from knfapp.users import auth
from .utils import bearer, create_user


def _gif_bytes(frames=2):
    images = [Image.new("P", (6, 6), i) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=True, append_images=images[1:])
    buf.seek(0)
    buf.name = "juokinga.gif"
    return buf


def _jpg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (6, 6), (10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    buf.name = "nuotrauka.jpg"
    return buf


class MemeLibraryTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.tmp = tempfile.mkdtemp(prefix="knfapp-memes-")
        meme_views._memes_dir_cache = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(meme_views, "_memes_dir_cache", None))

        self.client = Client()
        self.user = create_user()
        self.token = auth.mint_session(self.user.id)

    def _push(self, file, session=None, **form):
        return bearer(self.client.post, "/api/memes", session or self.token,
                      data={"file": file, **form})

    def test_a_gif_keeps_its_bytes_and_reads_animated(self):
        raw = _gif_bytes()
        raw_bytes = raw.getvalue()
        body = self._push(raw, title="Šokis").json()["meme"]
        self.assertTrue(body["animated"])
        served = self.client.get(body["url"])
        self.assertEqual(b"".join(served.streaming_content), raw_bytes)

    def test_a_static_image_clears_the_reencode_gate(self):
        body = self._push(_jpg_bytes()).json()["meme"]
        self.assertFalse(body["animated"])
        self.assertTrue(body["url"].endswith(".jpg"))
        self.assertTrue(body["preview"].startswith("data:image/jpeg;base64,"))
        garbage = io.BytesIO(b"tikrai ne paveikslas")
        garbage.name = "x.jpg"
        self.assertEqual(self._push(garbage).status_code, 400)

    def test_the_title_falls_back_to_the_filename_stem(self):
        raw = _jpg_bytes()
        raw.name = "kavos_puodelis-rytas.jpg"
        body = self._push(raw).json()["meme"]
        self.assertEqual(body["title"], "kavos puodelis rytas")

    def test_the_folded_search_finds_diacritics_both_ways(self):
        self._push(_jpg_bytes(), title="AČIŪ dėstytojau")
        found = bearer(self.client.get, "/api/memes?q=aciu", self.token).json()["memes"]
        self.assertEqual(len(found), 1)
        # Every word must appear — two words, one match set
        both = bearer(self.client.get, "/api/memes?q=ačiū dėstytojau", self.token).json()["memes"]
        self.assertEqual(len(both), 1)
        none = bearer(self.client.get, "/api/memes?q=aciu kava", self.token).json()["memes"]
        self.assertEqual(len(none), 0)

    def test_pusher_or_admin_removes_it_with_the_file(self):
        import os
        body = self._push(_jpg_bytes()).json()["meme"]
        filename = body["url"].rsplit("/", 1)[-1]
        stranger = create_user(username="kitas", email="k@knf.vu.lt")
        self.assertEqual(bearer(self.client.delete, f"/api/memes/{body['id']}",
                                auth.mint_session(stranger.id)).status_code, 403)
        self.assertEqual(bearer(self.client.delete, f"/api/memes/{body['id']}",
                                self.token).status_code, 200)
        self.assertEqual(Meme.objects.count(), 0)
        self.assertFalse(os.path.exists(f"{self.tmp}/{filename}"))

    def test_the_serve_gate_refuses_foreign_names(self):
        for name in ("../knfapp.sqlite3", "x.gif", "a" * 32 + ".exe"):
            self.assertEqual(self.client.get(f"/api/memes/file/{name}").status_code, 404, name)

    def test_the_library_needs_a_login_but_the_files_do_not(self):
        body = self._push(_jpg_bytes()).json()["meme"]
        self.assertEqual(self.client.get("/api/memes").status_code, 401)
        self.assertEqual(self.client.get(body["url"]).status_code, 200)
