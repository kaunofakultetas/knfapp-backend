# -----------------------------------------------------------
#  [*] Tests — app/uploads/routes.py
#
#  The one place binary files enter and leave the app, so the
#  claims in its banner are the ones this module proves:
#
#    - the BYTES decide, not the name: a .png full of text is
#      refused, a PNG called "dokumentas.pdf" is stored, and a
#      RIFF container that is not WEBP never passes as one
#    - the 5 MB cap is a boundary (exactly 5 MB passes the
#      size gate), and a body past MAX_CONTENT_LENGTH is cut
#      by Werkzeug with the same file_too_large slug
#    - the Pillow re-encode IS the gate: EXIF/GPS and PNG text
#      chunks never reach disk, 30 MP is enforced with the
#      animation frames counted, still images are downscaled
#      to 2048 px and the stored extension describes the bytes
#    - the client's filename is thrown away entirely, so no
#      traversal, no overwrite and no enumeration by name
#    - every stored file gets an uploads row (owner + size),
#      that row is the per-account quota and the DELETE gate,
#      and delete_upload() clears both file and row
#    - GET is public, cached privately for 24 h, and answers
#      400 for a name this module could never have written and
#      404 for one it could have but did not
#
#  Every rejection is asserted through its machine-readable
#  "code", because that slug — not the prose — is what
#  services/api/uploads.ts branches on.
# -----------------------------------------------------------

import io
import os
import re
import time
import uuid
import zlib

import pytest
import time_machine
from PIL import Image

from app.auth.routes import _rate_limit_store
from app.uploads import routes as uploads_routes
from app.uploads.routes import (
    ALLOWED_EXTENSIONS,
    MAX_EDGE,
    MAX_FILE_SIZE,
    ORPHAN_GRACE_SECONDS,
    UPLOAD_QUOTA_BYTES,
    UPLOAD_RATE_MAX,
    _get_upload_dir,
    _validate_magic_bytes,
    delete_upload,
    sweep_orphan_uploads,
)

UPLOADS = "/api/uploads"

# The exact shape the route hands out and the only shape it
# will ever serve or delete again
STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|gif|webp)$")




# -----------------------------------------------------------
# _isolate_module_state
# -----------------------------------------------------------
#
# uploads/routes.py resolves UPLOAD_DIR once per PROCESS and
# caches it in a module global, but every test gets a fresh
# tmp directory — without this reset the second test in the
# session would write into the first one's directory. The
# rate-limit store is process-global for the same reason: the
# upload quota is per user, but the global per-IP budget in
# create_app spends from the same dict and every test client
# request arrives from 127.0.0.1.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_module_state():
    uploads_routes._upload_dir = None
    _rate_limit_store.clear()
    yield
    uploads_routes._upload_dir = None
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _image_bytes / _animation_bytes
# -----------------------------------------------------------
#
# Real encoded images, built with the same Pillow the route
# decodes with — a hand-written byte literal would prove
# nothing about the re-encode. Every helper returns bytes
# ready to be posted.
# -----------------------------------------------------------

def _image_bytes(fmt="PNG", size=(8, 8), mode="RGB", color=(120, 20, 60), **save_kw):
    fill = {"RGB": color, "RGBA": color + (128,), "LA": (128, 200)}[mode]
    image = Image.new(mode, size, fill)

    buffer = io.BytesIO()
    image.save(buffer, fmt, **save_kw)
    return buffer.getvalue()


def _animation_bytes(fmt="GIF", frames=4, size=(24, 24)):
    # Frames must actually DIFFER — Pillow collapses identical
    # ones and the file would come back single-frame
    images = [Image.new("RGB", size, (index * 60 % 255, 30, 200 - index * 20)) for index in range(frames)]
    if fmt == "GIF":
        images = [image.convert("P") for image in images]

    buffer = io.BytesIO()
    images[0].save(buffer, format=fmt, save_all=True, append_images=images[1:], duration=80)
    return buffer.getvalue()




# -----------------------------------------------------------
# _exif_jpeg_bytes
# -----------------------------------------------------------
#
# A JPEG carrying exactly what a phone camera leaks: a Make
# tag (the marker the test greps the stored file for) and a
# real GPS IFD. Both must be gone after the re-encode.
# -----------------------------------------------------------

def _exif_jpeg_bytes(marker="KNF-KAMERA"):
    image = Image.new("RGB", (16, 16), (200, 10, 90))

    exif = Image.Exif()
    exif[0x010F] = marker
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (54.0, 53.0, 0.0)
    gps[3] = "E"
    gps[4] = (23.0, 54.0, 0.0)

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)
    return buffer.getvalue()




# -----------------------------------------------------------
# _bomb_png
# -----------------------------------------------------------
#
# A PNG whose IHDR CLAIMS a huge canvas and carries no real
# pixel data: Pillow sizes the image from the header alone,
# so this trips the decompression-bomb guard without the test
# ever allocating a hundred megapixels.
# -----------------------------------------------------------

def _bomb_png(width, height):
    def chunk(tag, data):
        return len(data).to_bytes(4, "big") + tag + data + zlib.crc32(tag + data).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"\x00") + chunk(b"IEND", b"")




# -----------------------------------------------------------
# _many_frame_gif
# -----------------------------------------------------------
#
# A GIF whose frames differ by ONE pixel, so it stays a few
# kilobytes on the wire while its decoded pixel budget
# (frames x width x height) blows past the 30 MP ceiling —
# the smuggling attempt a single-frame check would miss.
# -----------------------------------------------------------

def _many_frame_gif(frames=100, size=(600, 600)):
    base = Image.new("P", size, 0)
    base.putpalette([0, 0, 0, 255, 0, 0] + [0] * (768 - 6))

    images = []
    for index in range(frames):
        frame = base.copy()
        frame.putpixel((index, 0), 1)
        images.append(frame)

    buffer = io.BytesIO()
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:])
    return buffer.getvalue()




# -----------------------------------------------------------
# _upload / _stored_path / _stored_names / _row
# -----------------------------------------------------------

def _upload(client, headers, blob, filename="nuotrauka.png"):
    return client.post(
        UPLOADS,
        data={"file": (io.BytesIO(blob), filename)},
        headers=headers,
        content_type="multipart/form-data",
    )


def _stored_path(app, filename):
    return os.path.join(app.config["UPLOAD_DIR"], filename)


def _stored_names(app):
    return sorted(os.listdir(app.config["UPLOAD_DIR"]))


def _row(db, filename):
    return db.execute("SELECT * FROM uploads WHERE filename = ?", (filename,)).fetchone()


def _seed_upload_row(db, user_id, byte_size, filename=None):
    filename = filename or f"{uuid.uuid4().hex}.jpg"
    db.execute(
        "INSERT INTO uploads (id, filename, user_id, byte_size, created_at)"
        " VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (str(uuid.uuid4()), filename, user_id, byte_size),
    )
    db.commit()
    return filename


def _write_stored_file(app, ext="jpg", body=b"ne tikras vaizdas"):
    name = f"{uuid.uuid4().hex}.{ext}"
    with open(_stored_path(app, name), "wb") as handle:
        handle.write(body)
    return name








# -----------------------------------------------------------
# POST /api/uploads — authentication and the multipart body
# -----------------------------------------------------------

def test_an_anonymous_upload_is_refused(client):
    response = _upload(client, {}, _image_bytes())

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required"


def test_a_bogus_bearer_token_cannot_upload(client):
    response = _upload(client, {"Authorization": "Bearer ne-toks-zetonas"}, _image_bytes())

    assert response.status_code == 401


def test_an_upload_with_no_file_part_is_refused(client, actor):
    _, headers = actor

    response = client.post(UPLOADS, data={}, headers=headers, content_type="multipart/form-data")

    assert response.status_code == 400
    assert response.get_json() == {"error": "No file provided", "code": "no_file"}


def test_a_plain_form_field_named_file_is_not_a_file(client, actor):
    _, headers = actor

    response = client.post(UPLOADS, data={"file": "labas"}, headers=headers,
                           content_type="multipart/form-data")

    assert response.status_code == 400
    assert response.get_json()["code"] == "no_file"


def test_a_file_part_with_an_empty_filename_is_refused(client, actor):
    _, headers = actor

    response = _upload(client, headers, _image_bytes(), filename="")

    assert response.status_code == 400
    assert response.get_json() == {"error": "No file selected", "code": "no_file"}








# -----------------------------------------------------------
# POST /api/uploads — the size gates
# -----------------------------------------------------------

def test_an_empty_file_is_refused(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, b"", filename="tuscia.png")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Empty file", "code": "empty_file"}
    assert _stored_names(app) == []


def test_a_file_one_byte_over_the_cap_is_refused(client, actor):
    _, headers = actor

    response = _upload(client, headers, b"\x00" * (MAX_FILE_SIZE + 1), filename="didele.png")

    assert response.status_code == 400
    body = response.get_json()
    assert body["code"] == "file_too_large"
    assert body["error"] == "File too large. Max 5 MB"


def test_exactly_the_cap_gets_past_the_size_gate(client, actor):
    # The gate is "> MAX_FILE_SIZE", so 5 MB on the nose must
    # reach the content check and be refused for its BYTES
    _, headers = actor

    response = _upload(client, headers, b"\x00" * MAX_FILE_SIZE, filename="riba.png")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"


def test_a_body_over_the_werkzeug_ceiling_is_cut_with_a_413(client, actor):
    # MAX_CONTENT_LENGTH (6 MB) refuses the body before the
    # route runs at all — same slug, so the client branches
    # the same way on both
    _, headers = actor

    response = _upload(client, headers, b"\x00" * (6 * 1024 * 1024 + 1024), filename="milzine.png")

    assert response.status_code == 413
    assert response.get_json() == {"error": "File too large", "code": "file_too_large"}








# -----------------------------------------------------------
# POST /api/uploads — the bytes decide, not the name
# -----------------------------------------------------------

def test_a_real_png_is_accepted(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _image_bytes())

    assert response.status_code == 201
    body = response.get_json()
    assert STORED_NAME_RE.match(body["filename"]), body
    assert os.path.isfile(_stored_path(app, body["filename"]))


def test_a_text_file_renamed_to_png_is_refused_on_its_content(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, b"Tai tikrai ne paveiksliukas, o tekstas.", filename="apgaule.png")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "File content does not match an allowed image format",
        "code": "bad_file_content",
    }
    assert _stored_names(app) == []


def test_a_file_with_a_disallowed_extension_is_named_in_the_rejection(client, actor):
    _, headers = actor

    response = _upload(client, headers, b"MZ\x90\x00 kenkeja", filename="virusas.exe")

    assert response.status_code == 400
    body = response.get_json()
    assert body["code"] == "bad_file_type"
    for extension in ALLOWED_EXTENSIONS:
        assert extension in body["error"]


def test_a_blob_with_no_extension_still_reaches_the_content_check(client, actor):
    # The Expo web picker posts parts called "blob"; rejecting
    # those on the name was the old gate's real-world failure
    _, headers = actor

    response = _upload(client, headers, b"visai ne vaizdas", filename="blob")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"


def test_a_png_renamed_to_pdf_is_still_stored(client, actor):
    # The signature is the opinion that counts; the client's
    # extension only shapes the message on a rejection
    _, headers = actor

    response = _upload(client, headers, _image_bytes(), filename="dokumentas.pdf")

    assert response.status_code == 201
    assert response.get_json()["filename"].endswith(".jpg")


def test_a_header_shorter_than_four_bytes_cannot_be_sniffed(client, actor):
    # Three bytes of a real JPEG signature: too short to judge
    _, headers = actor

    response = _upload(client, headers, b"\xff\xd8\xff", filename="trumpa.jpg")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"


def test_a_riff_container_that_is_not_webp_is_not_a_webp(client, actor):
    # A .wav is RIFF too — the signature table checks bytes
    # 8-12 before believing the container
    _, headers = actor
    wave = b"RIFF" + (100).to_bytes(4, "little") + b"WAVEfmt " + b"\x00" * 20

    response = _upload(client, headers, wave, filename="garsas.webp")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"


def test_a_bmp_is_sniffed_and_re_encoded_as_jpeg(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _image_bytes("BMP"), filename="piesinys.bmp")

    assert response.status_code == 201
    name = response.get_json()["filename"]
    assert name.endswith(".jpg")
    assert Image.open(_stored_path(app, name)).format == "JPEG"


def test_a_tiff_is_sniffed_and_re_encoded_as_jpeg(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _image_bytes("TIFF"), filename="skenas.tiff")

    assert response.status_code == 201
    name = response.get_json()["filename"]
    assert name.endswith(".jpg")
    assert Image.open(_stored_path(app, name)).format == "JPEG"


def test_a_big_endian_tiff_header_gets_past_the_extension_filter(client, actor):
    # Pillow writes little-endian, so the big-endian signature
    # is flipped in by hand: the sniff accepts it (no
    # bad_file_type), and the re-encode is what then refuses
    # the mismatched body — magic is only a first opinion
    _, headers = actor
    blob = b"MM\x00*" + _image_bytes("TIFF")[4:]

    response = _upload(client, headers, blob, filename="skenas.tif")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"


def test_a_gif87a_header_is_recognised(client, actor):
    _, headers = actor
    gif89a = _animation_bytes("GIF", frames=3, size=(16, 16))

    response = _upload(client, headers, b"GIF87a" + gif89a[6:], filename="senas.gif")

    assert response.status_code == 201


def test_bytes_that_pass_the_signature_but_do_not_decode_are_refused(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, b"\x89PNG\r\n\x1a\n" + b"sugadinta" * 8, filename="sugadinta.png")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "File content could not be read as an image",
        "code": "bad_file_content",
    }
    assert _stored_names(app) == []


def test_the_signature_table_answers_with_the_format_it_matched():
    assert _validate_magic_bytes(io.BytesIO(b"GIF87a" + b"\x00" * 12)) == "gif"
    assert _validate_magic_bytes(io.BytesIO(b"GIF89a" + b"\x00" * 12)) == "gif"
    assert _validate_magic_bytes(io.BytesIO(b"BM" + b"\x00" * 16)) == "bmp"
    assert _validate_magic_bytes(io.BytesIO(b"II*\x00" + b"\x00" * 12)) == "tiff"
    assert _validate_magic_bytes(io.BytesIO(b"MM\x00*" + b"\x00" * 12)) == "tiff"
    assert _validate_magic_bytes(io.BytesIO(b"RIFF" + b"\x00" * 4 + b"WEBPVP8 ")) == "webp"
    assert _validate_magic_bytes(io.BytesIO(b"RIFF" + b"\x00" * 4 + b"WAVEfmt ")) is None
    assert _validate_magic_bytes(io.BytesIO(b"%PDF-1.7 nera vaizdo")) is None
    assert _validate_magic_bytes(io.BytesIO(b"\xff\xd8")) is None


def test_the_sniff_leaves_the_stream_at_the_start():
    # The route reads the whole body straight after the sniff,
    # so a stream left at offset 16 would truncate the image
    stream = io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"likusi dalis")

    _validate_magic_bytes(stream)

    assert stream.tell() == 0








# -----------------------------------------------------------
# POST /api/uploads — the re-encode is the real gate
# -----------------------------------------------------------

def test_exif_and_gps_never_reach_the_stored_file(client, actor, app):
    _, headers = actor
    original = _exif_jpeg_bytes()
    assert b"KNF-KAMERA" in original, "the fixture must actually carry the marker"

    response = _upload(client, headers, original, filename="foto.jpg")

    assert response.status_code == 201
    path = _stored_path(app, response.get_json()["filename"])
    assert b"KNF-KAMERA" not in open(path, "rb").read()
    stored_exif = Image.open(path).getexif()
    assert dict(stored_exif) == {}
    assert dict(stored_exif.get_ifd(0x8825)) == {}


def test_a_png_text_chunk_does_not_survive_the_re_encode(client, actor, app):
    from PIL.PngImagePlugin import PngInfo

    _, headers = actor
    info = PngInfo()
    info.add_text("Comment", "slaptas-metaduomenu-pedsakas")
    buffer = io.BytesIO()
    Image.new("RGBA", (12, 12), (10, 200, 40, 200)).save(buffer, "PNG", pnginfo=info)

    response = _upload(client, headers, buffer.getvalue(), filename="su-tekstu.png")

    assert response.status_code == 201
    path = _stored_path(app, response.get_json()["filename"])
    assert b"slaptas-metaduomenu-pedsakas" not in open(path, "rb").read()


def test_a_plain_photo_is_stored_as_progressive_jpeg(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _image_bytes(size=(40, 30)))

    name = response.get_json()["filename"]
    assert name.endswith(".jpg")
    stored = Image.open(_stored_path(app, name))
    assert stored.format == "JPEG"
    assert stored.info.get("progression")


@pytest.mark.parametrize("mode", ["RGBA", "LA"])
def test_an_image_with_an_alpha_channel_is_stored_as_png(client, actor, app, mode):
    _, headers = actor

    response = _upload(client, headers, _image_bytes(mode=mode), filename="skaidri.png")

    assert response.status_code == 201
    name = response.get_json()["filename"]
    assert name.endswith(".png")
    assert Image.open(_stored_path(app, name)).format == "PNG"


def test_a_palette_image_with_transparency_is_stored_as_png(client, actor, app):
    _, headers = actor
    buffer = io.BytesIO()
    Image.new("P", (10, 10)).save(buffer, "PNG", transparency=0)

    response = _upload(client, headers, buffer.getvalue(), filename="paletė.png")

    assert response.status_code == 201
    assert response.get_json()["filename"].endswith(".png")


def test_a_still_image_is_downscaled_to_the_longest_edge(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _image_bytes(size=(3000, 1000)), filename="panorama.png")

    assert response.status_code == 201
    stored = Image.open(_stored_path(app, response.get_json()["filename"]))
    assert max(stored.size) == MAX_EDGE
    assert stored.size == (2048, 683), "the aspect ratio must survive the downscale"


def test_a_small_image_is_not_upscaled(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _image_bytes(size=(24, 18)))

    assert Image.open(_stored_path(app, response.get_json()["filename"])).size == (24, 18)


def test_an_animated_gif_keeps_every_frame(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _animation_bytes("GIF", frames=5), filename="animacija.gif")

    assert response.status_code == 201
    name = response.get_json()["filename"]
    assert name.endswith(".gif")
    stored = Image.open(_stored_path(app, name))
    assert stored.format == "GIF"
    assert stored.n_frames == 5


def test_an_animation_keeps_its_original_frame_size(client, actor, app):
    # A per-frame resize desyncs GIF palettes, so animations
    # are deliberately not downscaled
    _, headers = actor

    response = _upload(client, headers, _animation_bytes("GIF", frames=3, size=(320, 120)),
                       filename="plati.gif")

    assert Image.open(_stored_path(app, response.get_json()["filename"])).size == (320, 120)


def test_an_animated_webp_stays_webp(client, actor, app):
    # Re-encoding an animated WebP as GIF can grow it many
    # times over, so the container is kept
    _, headers = actor

    response = _upload(client, headers, _animation_bytes("WEBP", frames=4), filename="animacija.webp")

    assert response.status_code == 201
    name = response.get_json()["filename"]
    assert name.endswith(".webp")
    assert Image.open(_stored_path(app, name)).format == "WEBP"


def test_a_still_webp_becomes_a_jpeg(client, actor):
    _, headers = actor

    response = _upload(client, headers, _image_bytes("WEBP"), filename="viena.webp")

    assert response.status_code == 201
    assert response.get_json()["filename"].endswith(".jpg")


def test_a_declared_bomb_is_refused_before_it_is_decoded(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _bomb_png(10000, 10000), filename="bomba.png")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "Image too large. Max 30 megapixels",
        "code": "image_too_large",
    }
    assert _stored_names(app) == []


def test_the_pixel_budget_counts_animation_frames(client, actor, app):
    # 100 frames of 600x600 is 36 MP decoded but only a few KB
    # on the wire — a single-frame check would wave it through
    _, headers = actor
    blob = _many_frame_gif()
    assert len(blob) < MAX_FILE_SIZE

    response = _upload(client, headers, blob, filename="daug-kadru.gif")

    assert response.status_code == 400
    assert response.get_json()["code"] == "image_too_large"
    assert _stored_names(app) == []


def test_a_re_encode_that_blows_up_is_a_bad_file_not_a_500(client, actor, app, monkeypatch):
    _, headers = actor
    blob = _image_bytes()

    def _boom(self, *args, **kwargs):
        raise OSError("encoder gone")

    monkeypatch.setattr(Image.Image, "save", _boom)

    response = _upload(client, headers, blob)

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"
    assert _stored_names(app) == []








# -----------------------------------------------------------
# POST /api/uploads — the stored name and the ownership row
# -----------------------------------------------------------

def test_the_client_filename_is_thrown_away(client, actor, app):
    _, headers = actor

    response = _upload(client, headers, _image_bytes(), filename="../../../etc/passwd.png")

    assert response.status_code == 201
    assert STORED_NAME_RE.match(response.get_json()["filename"])
    assert _stored_names(app) == [response.get_json()["filename"]]


@pytest.mark.parametrize("filename", [
    "..%2f..%2fknfapp.db",
    "....//pabegimas.png",
    "nuotrauka; rm -rf /.png",
    "ąžuolas-vasarą.jpg",
    "a" * 300 + ".png",
])
def test_no_client_filename_can_steer_where_the_file_lands(client, actor, app, filename):
    _, headers = actor

    response = _upload(client, headers, _image_bytes(), filename=filename)

    assert response.status_code == 201
    assert _stored_names(app) == [response.get_json()["filename"]]
    assert STORED_NAME_RE.match(response.get_json()["filename"])


def test_two_uploads_of_the_same_image_never_collide(client, actor, app):
    _, headers = actor

    first = _upload(client, headers, _image_bytes()).get_json()["filename"]
    second = _upload(client, headers, _image_bytes()).get_json()["filename"]

    assert first != second
    assert _stored_names(app) == sorted([first, second])


def test_the_upload_writes_a_row_naming_its_owner_and_size(client, actor, app, db):
    user, headers = actor

    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    row = _row(db, name)
    assert row is not None
    assert row["user_id"] == user["id"]
    assert row["byte_size"] == os.path.getsize(_stored_path(app, name))
    assert row["created_at"].startswith("20") and row["created_at"].endswith("+00:00")


def test_a_rejected_upload_writes_neither_file_nor_row(client, actor, app, db):
    _, headers = actor

    _upload(client, headers, b"ne vaizdas", filename="niekas.png")

    assert _stored_names(app) == []
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 0


def test_no_part_file_is_left_behind_by_a_successful_upload(client, actor, app):
    _, headers = actor

    _upload(client, headers, _image_bytes())

    assert [name for name in _stored_names(app) if name.endswith(".part")] == []








# -----------------------------------------------------------
# POST /api/uploads — the per-account storage quota
# -----------------------------------------------------------

def test_a_full_account_is_refused_with_a_413(client, actor, app, db):
    user, headers = actor
    _seed_upload_row(db, user["id"], UPLOAD_QUOTA_BYTES)

    response = _upload(client, headers, _image_bytes())

    assert response.status_code == 413
    body = response.get_json()
    assert body["code"] == "quota_exceeded"
    assert body["error"] == "Storage quota reached. Max 100 MB per account"
    assert _stored_names(app) == []
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 1


def test_the_quota_counts_only_the_uploaders_own_files(client, actor, make_user, db):
    user, headers = actor
    stranger = make_user()
    _seed_upload_row(db, stranger["id"], UPLOAD_QUOTA_BYTES * 2)

    response = _upload(client, headers, _image_bytes())

    assert response.status_code == 201


def test_the_quota_admits_the_upload_that_fills_it_exactly(client, actor, db):
    # The gate is "used + new > quota", so the byte that lands
    # ON the limit must still be stored — and the next one must
    # not be. Identical input re-encodes to an identical size.
    user, headers = actor
    blob = _image_bytes(size=(7, 7), color=(9, 9, 9))

    first = _upload(client, headers, blob)
    assert first.status_code == 201
    stored_size = _row(db, first.get_json()["filename"])["byte_size"]

    _seed_upload_row(db, user["id"], UPLOAD_QUOTA_BYTES - 2 * stored_size)

    exact = _upload(client, headers, blob)
    assert exact.status_code == 201, "an upload that exactly fills the quota must be allowed"

    over = _upload(client, headers, blob)
    assert over.status_code == 413
    assert over.get_json()["code"] == "quota_exceeded"


def test_deleting_a_file_frees_the_quota_again(client, actor, db):
    user, headers = actor
    blob = _image_bytes(size=(7, 7), color=(9, 9, 9))
    name = _upload(client, headers, blob).get_json()["filename"]
    stored_size = _row(db, name)["byte_size"]
    filler = _seed_upload_row(db, user["id"], UPLOAD_QUOTA_BYTES - stored_size)

    assert _upload(client, headers, blob).status_code == 413

    db.execute("DELETE FROM uploads WHERE filename = ?", (filler,))
    db.commit()

    assert _upload(client, headers, blob).status_code == 201








# -----------------------------------------------------------
# POST /api/uploads — storage failures
# -----------------------------------------------------------

def test_a_failed_write_answers_507_and_leaves_nothing_behind(client, actor, app, db, monkeypatch):
    _, headers = actor

    def _boom(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", _boom)

    response = _upload(client, headers, _image_bytes())

    assert response.status_code == 507
    assert response.get_json() == {
        "error": "Storage is unavailable, try again later",
        "code": "storage_unavailable",
    }
    assert _stored_names(app) == [], "the .part file must be cleaned up"
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 0


def test_a_failed_cleanup_after_a_failed_write_still_answers_507(client, actor, app, monkeypatch):
    # Both the rename and the .part unlink fail: the second
    # error is swallowed on purpose, the client still gets 507
    _, headers = actor

    def _boom(*args, **kwargs):
        raise OSError("volume gone")

    monkeypatch.setattr(os, "replace", _boom)
    monkeypatch.setattr(os, "unlink", _boom)

    response = _upload(client, headers, _image_bytes())

    assert response.status_code == 507
    assert [name for name in _stored_names(app) if name.endswith(".part")] != []








# -----------------------------------------------------------
# POST /api/uploads — the per-user rate limit
# -----------------------------------------------------------

def test_the_twenty_first_upload_attempt_in_the_window_is_refused(client, actor):
    # Rejected attempts spend budget too — the limiter runs
    # before the route body, so a flood of junk still counts
    _, headers = actor

    for _ in range(UPLOAD_RATE_MAX):
        assert client.post(UPLOADS, data={}, headers=headers,
                           content_type="multipart/form-data").status_code == 400

    response = _upload(client, headers, _image_bytes())

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0


def test_the_upload_window_reopens_after_five_minutes(client, actor):
    _, headers = actor

    for _ in range(UPLOAD_RATE_MAX):
        client.post(UPLOADS, data={}, headers=headers, content_type="multipart/form-data")
    assert _upload(client, headers, _image_bytes()).status_code == 429

    with time_machine.travel(time.time() + 301, tick=False):
        assert _upload(client, headers, _image_bytes()).status_code == 201


def test_one_users_upload_flood_does_not_block_another(client, actor, make_user, auth_headers):
    _, headers = actor
    for _ in range(UPLOAD_RATE_MAX):
        client.post(UPLOADS, data={}, headers=headers, content_type="multipart/form-data")
    assert _upload(client, headers, _image_bytes()).status_code == 429

    other = make_user()

    assert _upload(client, auth_headers(other), _image_bytes()).status_code == 201








# -----------------------------------------------------------
# GET /api/uploads/<filename> — public serving
# -----------------------------------------------------------

def test_a_guest_can_fetch_an_uploaded_image(client, actor, app):
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 200
    assert response.data == open(_stored_path(app, name), "rb").read()
    assert response.headers["Content-Type"] == "image/jpeg"
    # User-supplied bytes must never be MIME-sniffed by a browser
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("ext, content_type", [
    ("jpg", "image/jpeg"),
    ("jpeg", "image/jpeg"),
    ("png", "image/png"),
    ("gif", "image/gif"),
    ("webp", "image/webp"),
])
def test_the_content_type_follows_the_stored_extension(client, app, ext, content_type):
    name = _write_stored_file(app, ext)

    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 200
    assert response.headers["Content-Type"] == content_type


def test_an_image_is_cached_privately_for_a_day(client, app):
    # Chat photos come through this same public route, so a
    # SHARED proxy cache must never hold one
    name = _write_stored_file(app)

    response = client.get(f"{UPLOADS}/{name}")

    cache_control = response.headers["Cache-Control"]
    assert "private" in cache_control
    assert "max-age=86400" in cache_control
    assert "public" not in cache_control
    assert "no-store" not in cache_control, "the app-wide /api/ no-store must not clobber this"


def test_a_conditional_request_gets_a_304(client, app):
    name = _write_stored_file(app)
    first = client.get(f"{UPLOADS}/{name}")

    response = client.get(f"{UPLOADS}/{name}", headers={"If-None-Match": first.headers["ETag"]})

    assert response.status_code == 304


def test_a_well_shaped_name_that_was_never_stored_is_a_404(client):
    response = client.get(f"{UPLOADS}/{uuid.uuid4().hex}.jpg")

    assert response.status_code == 404
    assert response.get_json() == {"error": "File not found"}


@pytest.mark.parametrize("name", [
    "labas.png",                                    # not a uuid hex
    "ABCDEF0123456789ABCDEF0123456789.png",         # uppercase hex
    "0123456789abcdef0123456789abcde.png",          # 31 chars
    "0123456789abcdef0123456789abcdef0.png",        # 33 chars
    "0123456789abcdef0123456789abcdef.bmp",         # never a stored extension
    "0123456789abcdef0123456789abcdef.png.exe",     # double extension
    "0123456789abcdef0123456789abcdef",             # no extension
    "0123456789abcdef0123456789abcdef.PNG",         # uppercase extension
])
def test_a_name_this_module_could_not_have_written_is_a_400(client, name):
    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid filename", "code": "bad_filename"}


@pytest.mark.parametrize("attempt", [
    "../../etc/passwd",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "..%2fknfapp.db",
    "%2e%2e/knfapp.db",
])
def test_traversal_out_of_the_upload_directory_never_serves_a_file(client, attempt):
    response = client.get(f"{UPLOADS}/{attempt}")

    assert response.status_code in (400, 404)
    assert response.is_json, "a filesystem probe must never get bytes back"


def test_serving_does_not_require_a_token(client, actor, app):
    # Avatars and post images render for anonymous viewers too
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    assert client.get(f"{UPLOADS}/{name}").status_code == 200


def test_a_pre_v43_file_with_a_jpeg_extension_is_still_served(client, app):
    # Files written before the uploads table exists have no row
    # and a .jpeg extension the re-encode no longer produces
    name = _write_stored_file(app, "jpeg")

    assert client.get(f"{UPLOADS}/{name}").status_code == 200








# -----------------------------------------------------------
# DELETE /api/uploads/<filename>
# -----------------------------------------------------------

def test_the_owner_can_delete_their_own_upload(client, actor, app, db):
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    response = client.delete(f"{UPLOADS}/{name}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert not os.path.exists(_stored_path(app, name))
    assert _row(db, name) is None


def test_a_deleted_upload_is_no_longer_served(client, actor):
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    client.delete(f"{UPLOADS}/{name}", headers=headers)

    assert client.get(f"{UPLOADS}/{name}").status_code == 404


def test_a_stranger_cannot_delete_someone_elses_upload(client, actor, make_user, auth_headers, app):
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]
    stranger = make_user()

    response = client.delete(f"{UPLOADS}/{name}", headers=auth_headers(stranger))

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only the owner can delete this file"
    assert os.path.isfile(_stored_path(app, name))


def test_an_admin_can_delete_anyones_upload(client, actor, admin, app):
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]
    _, admin_headers = admin

    response = client.delete(f"{UPLOADS}/{name}", headers=admin_headers)

    assert response.status_code == 200
    assert not os.path.exists(_stored_path(app, name))


def test_an_admin_can_delete_an_ownerless_pre_v43_file(client, admin, app):
    # No uploads row exists for files written before v43, so
    # only an admin can ever reach them
    _, admin_headers = admin
    name = _write_stored_file(app)

    response = client.delete(f"{UPLOADS}/{name}", headers=admin_headers)

    assert response.status_code == 200
    assert not os.path.exists(_stored_path(app, name))


def test_an_ownerless_file_is_invisible_to_an_ordinary_user(client, actor, app):
    # 404, not 403: the route must not confirm what exists
    _, headers = actor
    name = _write_stored_file(app)

    response = client.delete(f"{UPLOADS}/{name}", headers=headers)

    assert response.status_code == 404
    assert response.get_json() == {"error": "File not found"}
    assert os.path.isfile(_stored_path(app, name))


def test_a_curator_is_not_an_admin_here(client, actor, make_user, auth_headers, app):
    # Only "admin" bypasses ownership — curator is privileged
    # in the auth blueprint but not on someone else's file
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]
    curator = make_user(role="curator")

    response = client.delete(f"{UPLOADS}/{name}", headers=auth_headers(curator))

    assert response.status_code == 403
    assert os.path.isfile(_stored_path(app, name))


def test_deleting_the_same_upload_twice_stops_finding_it(client, actor):
    # The row is the authorisation record, so once it is gone
    # the owner sees exactly what a stranger sees
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    assert client.delete(f"{UPLOADS}/{name}", headers=headers).status_code == 200
    assert client.delete(f"{UPLOADS}/{name}", headers=headers).status_code == 404


def test_deleting_an_unknown_name_is_a_404_for_a_member(client, actor):
    _, headers = actor

    response = client.delete(f"{UPLOADS}/{uuid.uuid4().hex}.png", headers=headers)

    assert response.status_code == 404


def test_deleting_an_unknown_name_is_harmlessly_ok_for_an_admin(client, admin):
    _, admin_headers = admin

    response = client.delete(f"{UPLOADS}/{uuid.uuid4().hex}.png", headers=admin_headers)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_an_anonymous_delete_is_refused(client, actor, app):
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    response = client.delete(f"{UPLOADS}/{name}")

    assert response.status_code == 401
    assert os.path.isfile(_stored_path(app, name))


@pytest.mark.parametrize("name", [
    "labas.png",
    "0123456789abcdef0123456789abcdef.bmp",
    "0123456789abcdef0123456789abcdef.PNG",
])
def test_deleting_a_name_this_module_could_not_have_written_is_a_400(client, actor, name):
    _, headers = actor

    response = client.delete(f"{UPLOADS}/{name}", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid filename", "code": "bad_filename"}


def test_a_delete_traversal_cannot_reach_the_database_file(client, admin, app):
    _, admin_headers = admin
    db_path = app.config["DB_PATH"]

    for attempt in ("..%2fknfapp-test.db", "%2e%2e%2f%2e%2e%2fetc%2fpasswd", "../../etc/passwd"):
        response = client.delete(f"{UPLOADS}/{attempt}", headers=admin_headers)
        assert response.status_code in (400, 404)

    assert os.path.isfile(db_path)


def test_deleting_one_upload_leaves_the_others_alone(client, actor, app, db):
    _, headers = actor
    first = _upload(client, headers, _image_bytes()).get_json()["filename"]
    second = _upload(client, headers, _image_bytes(size=(9, 9))).get_json()["filename"]

    client.delete(f"{UPLOADS}/{first}", headers=headers)

    assert _stored_names(app) == [second]
    assert _row(db, second) is not None








# -----------------------------------------------------------
# delete_upload() — the helper the other blueprints call
# -----------------------------------------------------------

def test_delete_upload_accepts_the_relative_url_that_was_stored(app, actor, client, db):
    _, headers = actor
    body = _upload(client, headers, _image_bytes()).get_json()

    with app.app_context():
        assert delete_upload(body["url"]) is True

    assert not os.path.exists(_stored_path(app, body["filename"]))
    assert _row(db, body["filename"]) is None


def test_delete_upload_accepts_a_bare_filename(app, actor, client):
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    with app.app_context():
        assert delete_upload(name) is True


def test_delete_upload_reports_false_when_the_file_is_already_gone(app, actor, db):
    user, _ = actor
    name = _seed_upload_row(db, user["id"], 500)

    with app.app_context():
        assert delete_upload(name) is False

    assert _row(db, name) is None, "a missing file still clears its row"


@pytest.mark.parametrize("value", [None, 42, b"baitai", ["/api/uploads/x.png"], {}])
def test_delete_upload_ignores_anything_that_is_not_a_string(app, value):
    with app.app_context():
        assert delete_upload(value) is False


@pytest.mark.parametrize("value", [
    "https://knf.vu.lt/wp-content/uploads/2026/naujiena.jpg",
    "/etc/passwd",
    "../../knfapp.db",
    "..%2f..%2fknfapp.db",
    "/api/uploads/",
    "/api/uploads/..",
    "",
    "0123456789abcdef0123456789abcdef.bmp",
    "0123456789ABCDEF0123456789ABCDEF.png",
])
def test_delete_upload_refuses_anything_it_did_not_write(app, value):
    with app.app_context():
        assert delete_upload(value) is False


def test_delete_upload_never_touches_a_foreign_row(app, actor, db):
    user, _ = actor
    _seed_upload_row(db, user["id"], 100, filename="0123456789abcdef0123456789abcdef.jpg")

    with app.app_context():
        delete_upload("https://kitas.lt/failai/nuotrauka.jpg")

    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 1


def test_delete_upload_survives_an_unlinkable_file(app, actor, client, db, monkeypatch):
    # A read-only volume must not raise into the caller, which
    # is always mid delete-or-replace and has to succeed anyway
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    def _boom(path):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(os, "unlink", _boom)

    with app.app_context():
        assert delete_upload(name) is False

    assert _row(db, name) is None, "the row goes even when the file cannot"








# -----------------------------------------------------------
# _get_upload_dir()
# -----------------------------------------------------------

def test_the_upload_directory_is_resolved_once_per_process(app):
    with app.app_context():
        first = _get_upload_dir()
        app.config["UPLOAD_DIR"] = "/tmp/niekada-nenaudojamas-katalogas"
        assert _get_upload_dir() == first

    assert os.path.isdir(first)


def test_a_missing_upload_directory_is_created_on_demand(app, tmp_path):
    target = tmp_path / "naujas" / "uploads"
    app.config["UPLOAD_DIR"] = str(target)

    with app.app_context():
        resolved = _get_upload_dir()

    assert resolved == str(target)
    assert os.path.isdir(resolved)








# -----------------------------------------------------------
# sweep_orphan_uploads()
# -----------------------------------------------------------

# -----------------------------------------------------------
# _age
# -----------------------------------------------------------
#
# Backdates a file past the sweep's grace period without any
# wall-clock sleeping — the mtime is what the sweep reads.
# -----------------------------------------------------------

def _age(path, seconds=ORPHAN_GRACE_SECONDS + 60):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def test_the_sweep_removes_an_old_unreferenced_file_and_its_row(app, actor, client, db):
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]
    _age(_stored_path(app, name))

    with app.app_context():
        assert sweep_orphan_uploads() == 1

    assert not os.path.exists(_stored_path(app, name))
    assert _row(db, name) is None


def test_the_sweep_spares_a_file_inside_the_grace_period(app, actor, client, db):
    # A file uploaded seconds ago has not been attached to a
    # post or message yet — unreferenced is not orphaned
    _, headers = actor
    name = _upload(client, headers, _image_bytes()).get_json()["filename"]

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert os.path.isfile(_stored_path(app, name))
    assert _row(db, name) is not None


def test_the_sweep_spares_a_file_an_avatar_points_at(app, actor, client, db):
    user, headers = actor
    body = _upload(client, headers, _image_bytes()).get_json()
    _age(_stored_path(app, body["filename"]))
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (body["url"], user["id"]))
    db.commit()

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert os.path.isfile(_stored_path(app, body["filename"]))


def test_the_sweep_spares_a_file_a_post_points_at(app, actor, client, db):
    user, headers = actor
    body = _upload(client, headers, _image_bytes()).get_json()
    _age(_stored_path(app, body["filename"]))
    db.execute(
        "INSERT INTO news_posts (id, title, content, image_url, author_id, source, post_type)"
        " VALUES (?, 'Naujiena', 'Tekstas', ?, ?, 'user', 'social')",
        (str(uuid.uuid4()), body["url"], user["id"]),
    )
    db.commit()

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert os.path.isfile(_stored_path(app, body["filename"]))


def test_the_sweep_spares_a_file_a_chat_message_points_at(app, actor, client, db):
    user, headers = actor
    body = _upload(client, headers, _image_bytes()).get_json()
    _age(_stored_path(app, body["filename"]))
    conversation_id = str(uuid.uuid4())
    db.execute("INSERT INTO conversations (id, type, created_by) VALUES (?, 'direct', ?)",
               (conversation_id, user["id"]))
    db.execute(
        "INSERT INTO messages (id, conversation_id, sender_id, text, image_url)"
        " VALUES (?, ?, ?, '', ?)",
        (str(uuid.uuid4()), conversation_id, user["id"], body["url"]),
    )
    db.commit()

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert os.path.isfile(_stored_path(app, body["filename"]))


def test_the_sweep_collects_a_leftover_part_file(app):
    # An interrupted write leaves <name>.part behind; the same
    # pass has to take it
    leftover = _stored_path(app, f"{uuid.uuid4().hex}.jpg.part")
    with open(leftover, "wb") as handle:
        handle.write(b"pusiau irasyta")
    _age(leftover)

    with app.app_context():
        assert sweep_orphan_uploads() == 1

    assert not os.path.exists(leftover)


def test_the_sweep_walks_past_a_subdirectory(app):
    nested = os.path.join(app.config["UPLOAD_DIR"], "senos-nuotraukos")
    os.makedirs(nested)
    _age(nested)

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert os.path.isdir(nested)


def test_the_sweep_counts_only_what_it_could_remove(app, actor, client, monkeypatch):
    _, headers = actor
    first = _upload(client, headers, _image_bytes()).get_json()["filename"]
    second = _upload(client, headers, _image_bytes(size=(9, 9))).get_json()["filename"]
    _age(_stored_path(app, first))
    _age(_stored_path(app, second))

    real_unlink = os.unlink
    blocked = _stored_path(app, first)

    def _selective_unlink(path):
        if path == blocked:
            raise PermissionError("read-only file system")
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _selective_unlink)

    with app.app_context():
        assert sweep_orphan_uploads() == 1

    assert os.path.isfile(blocked)


def test_the_sweep_on_an_empty_directory_removes_nothing(app):
    with app.app_context():
        assert sweep_orphan_uploads() == 0








# -----------------------------------------------------------
# Wire contracts — what the mobile client actually consumes
# -----------------------------------------------------------

@pytest.mark.contract
def test_a_successful_upload_answers_the_shape_the_client_stores(client, actor):
    # services/api/uploads.ts UploadResponse: { url, filename }
    _, headers = actor

    response = _upload(client, headers, _image_bytes())

    assert response.status_code == 201
    body = response.get_json()
    assert set(body) == {"url", "filename"}
    assert body["url"] == f"/api/uploads/{body['filename']}"
    assert body["url"].startswith("/api/uploads/"), "the stored value must stay RELATIVE"


@pytest.mark.contract
def test_the_url_an_upload_hands_out_is_accepted_as_an_avatar(client, actor):
    # create_app's before_request pins avatar_url to exactly
    # the shape this route produces — the two patterns must
    # never drift apart
    _, headers = actor
    url = _upload(client, headers, _image_bytes()).get_json()["url"]

    response = client.put("/api/auth/me", json={"avatar_url": url}, headers=headers)

    assert response.status_code == 200


@pytest.mark.contract
@pytest.mark.parametrize("blob, filename, status, code", [
    (b"", "tuscia.png", 400, "empty_file"),
    (b"tekstas", "apgaule.png", 400, "bad_file_content"),
    (b"MZ", "virusas.exe", 400, "bad_file_type"),
])
def test_every_rejection_carries_a_machine_readable_code(client, actor, blob, filename, status, code):
    _, headers = actor

    response = _upload(client, headers, blob, filename=filename)

    assert response.status_code == status
    body = response.get_json()
    assert body["code"] == code
    assert isinstance(body["error"], str) and body["error"]
