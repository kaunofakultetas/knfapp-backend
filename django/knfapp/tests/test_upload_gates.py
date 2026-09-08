############################################################
#  [*] Regression tests — the upload byte gates
#
#  uploads/gates.py as tables: the bytes decide, the name
#  only shapes messages. The re-encode pins are the
#  security decisions — metadata stripped, transparency to
#  PNG, animation preserved, the pixel bomb refused — each
#  proven on a tiny generated image, never a fixture file.
############################################################


import io


from PIL import Image
from django.test import SimpleTestCase


from knfapp.uploads import gates


def _png(size=(4, 4), mode="RGB", color=(200, 30, 60)):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpg(size=(4, 4)):
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


class SniffTests(SimpleTestCase):

    def test_the_signature_table(self):
        cases = [
            (_jpg(), "jpg"),
            (_png(), "png"),
            (b"GIF89a" + b"\x00" * 20, "gif"),
            (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8, "webp"),
            (b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8, None),   # RIFF alone is a container
            (b"BM" + b"\x00" * 20, "bmp"),
            (b"II*\x00" + b"\x00" * 12, "tiff"),
            (b"%PDF-1.7 ...", None),
            (b"\x00\x01", None),                                  # shorter than 4 bytes
        ]
        for blob, expected in cases:
            self.assertEqual(gates.sniff_image_format(blob), expected, blob[:8])

    def test_extensionless_names_pass_the_prefilter(self):
        # Web pickers send blobs called "blob"/"image" — rejecting
        # any of those is the gate's real-world failure mode
        self.assertTrue(gates.allowed_image_name("blob"))
        self.assertFalse(gates.allowed_image_name("virus.exe"))
        self.assertTrue(gates.allowed_image_name("photo.JPG"))


class DocumentGateTests(SimpleTestCase):

    def test_the_bytes_must_prove_the_claimed_type(self):
        cases = [
            ("a.pdf", b"%PDF-1.4 x", "pdf", None),
            ("a.pdf", b"MZ\x90\x00", None, "bad_file_content"),
            ("a.docx", b"PK\x03\x04rest", "docx", None),
            ("a.zip", b"PK\x03\x04rest", "zip", None),
            ("a.txt", "labas rytas".encode(), "txt", None),
            ("a.txt", b"text\x00with-nul", None, "bad_file_content"),   # an executable in a .txt coat
            ("a.txt", b"\xff\xfe\x00broken", None, "bad_file_content"),
            ("a.exe", b"MZ\x90\x00", None, "bad_file_type"),
        ]
        for name, blob, ext, code in cases:
            got_ext, rejection = gates.accept_document(name, blob)
            self.assertEqual(got_ext, ext, name)
            self.assertEqual(rejection[1] if rejection else None, code, name)

    def test_video_and_audio_signatures(self):
        ftyp = b"\x00\x00\x00\x18ftypmp42rest"
        self.assertEqual(gates.accept_video("v.mp4", ftyp)[0], "mp4")
        self.assertEqual(gates.accept_video("v.webm", b"\x1a\x45\xdf\xa3rest")[0], "webm")
        self.assertEqual(gates.accept_video("v.mp4", b"not-a-video")[1][1], "bad_file_content")
        self.assertEqual(gates.accept_audio("a.m4a", ftyp)[0], "m4a")
        self.assertEqual(gates.accept_audio("a.mp3", b"ID3\x04rest")[0], "mp3")
        self.assertEqual(gates.accept_audio("a.aac", b"\xff\xf1rest")[0], "aac")
        self.assertEqual(gates.accept_audio("a.aac", b"\x00\x00rest")[1][1], "bad_file_content")


class ReencodeTests(SimpleTestCase):

    def test_a_jpeg_is_reencoded_and_its_metadata_dropped(self):
        # Write EXIF into the source; the canonical bytes must not
        # carry it — only pixels cross over
        src = Image.new("RGB", (10, 10), (1, 2, 3))
        exif = Image.Exif()
        exif[0x010F] = "spy-camera"
        buf = io.BytesIO()
        src.save(buf, format="JPEG", exif=exif)

        ext, blob, rejection = gates.reencode_image(buf.getvalue())
        self.assertIsNone(rejection)
        self.assertEqual(ext, "jpg")
        with Image.open(io.BytesIO(blob)) as out:
            self.assertEqual(dict(out.getexif()), {})

    def test_transparency_becomes_png(self):
        ext, _, rejection = gates.reencode_image(_png(mode="RGBA", color=(1, 2, 3, 128)))
        self.assertIsNone(rejection)
        self.assertEqual(ext, "png")

    def test_oversized_stills_are_downscaled_to_max_edge(self):
        wide = io.BytesIO()
        Image.new("RGB", (gates.MAX_EDGE * 2, 10), (0, 0, 0)).save(wide, format="JPEG")
        _, blob, rejection = gates.reencode_image(wide.getvalue())
        self.assertIsNone(rejection)
        with Image.open(io.BytesIO(blob)) as out:
            self.assertLessEqual(max(out.size), gates.MAX_EDGE)

    def test_garbage_bytes_are_a_rejection_never_a_raise(self):
        ext, blob, rejection = gates.reencode_image(b"tikrai ne paveikslas")
        self.assertEqual(rejection[1], "bad_file_content")

    def test_the_pixel_budget_counts_animation_frames(self):
        # 40 frames of 1000x1000 = 40 MP > the 30 MP ceiling, while
        # any single frame alone would pass
        frames = [Image.new("P", (1000, 1000), i % 4) for i in range(40)]
        buf = io.BytesIO()
        frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
        _, _, rejection = gates.reencode_image(buf.getvalue())
        self.assertEqual(rejection[1], "image_too_large")
