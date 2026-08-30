# -----------------------------------------------------------
#  [*] Tests — app/uploads/routes.py, the INTAKE half
#
#  Exhaustive pass over the five functions that decide whether
#  bytes are allowed to become a stored file at all:
#
#    _allowed_file        — the filename pre-filter
#    _validate_magic_bytes— the signature sniff
#    _reencode_image      — the Pillow gate that IS the check
#    _get_upload_dir      — the once-per-process directory
#    upload_file          — POST /api/uploads
#
#  Everything downstream of a stored file (GET, DELETE,
#  delete_upload, sweep_orphan_uploads) belongs to other test
#  modules and is only touched here when it is the cheapest
#  way to observe what the intake actually wrote.
#
#  What this module pins, arm by arm:
#
#    - the ORDER of the gates: rate limit, then the multipart
#      body, then the 5 MB cap, then "empty", then the
#      signature, then the re-encode, then the quota, then the
#      write — an empty file called "x.pdf" is empty_file, not
#      bad_file_type, and an exhausted budget answers 429 even
#      when there is no file part at all
#    - every boundary as a PAIR: 3 header bytes vs 4, exactly
#      5 MB vs one byte over, the pixel ceiling on the nose vs
#      one pixel past it, a quota filled exactly vs overfilled
#      by one byte, 2048 px vs 2049 px
#    - every canonical-output arm of the re-encode, including
#      the ones no client sends on purpose: an APNG comes out
#      a GIF, a still GIF comes out a JPEG, a CMYK JPEG comes
#      out RGB
#    - every error path a caller can actually drive: bytes
#      that do not decode, a declared decompression bomb, a
#      save() that blows up, an open() that fails on the
#      .part file, an fsync that fails, a replace that fails,
#      and a cleanup unlink that fails on top of that
#    - the machine-readable "code" on every rejection, because
#      services/api/uploads.ts branches on the slug, never on
#      the prose
# -----------------------------------------------------------

import builtins
import io
import os
import re
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
    MAX_IMAGE_PIXELS,
    UPLOAD_QUOTA_BYTES,
    UPLOAD_RATE_MAX,
    _allowed_file,
    _get_upload_dir,
    _reencode_image,
    _validate_magic_bytes,
)

UPLOADS = "/api/uploads"

# The only shape the route ever hands out
STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|png|gif|webp)$")

# The full rejection vocabulary of this route, so a test can
# assert a code is one of THESE and not a typo
INTAKE_CODES = {
    "no_file", "file_too_large", "empty_file", "bad_file_type",
    "bad_file_content", "image_too_large", "quota_exceeded",
    "rate_limited", "storage_unavailable",
}




# -----------------------------------------------------------
# _isolate_module_state
# -----------------------------------------------------------
#
# uploads/routes.py caches UPLOAD_DIR in a module global that
# outlives one test, and the rate-limit store is process-wide
# (the global per-IP budget in create_app spends from the same
# dict, and every test request arrives from 127.0.0.1). Both
# are reset around every test so the order tests run in cannot
# change what they prove.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_module_state():
    uploads_routes._upload_dir = None
    _rate_limit_store.clear()
    yield
    uploads_routes._upload_dir = None
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _encode / _animate
# -----------------------------------------------------------
#
# Real encoded images built with the same Pillow the route
# decodes with — a hand-written byte literal would prove
# nothing about the re-encode. _animate's frames must actually
# DIFFER or Pillow collapses them and the file comes back
# single-frame.
# -----------------------------------------------------------

_FILL = {
    "RGB": (120, 20, 60),
    "RGBA": (120, 20, 60, 128),
    "LA": (128, 200),
    "L": 128,
    "P": 3,
    "1": 1,
    "CMYK": (10, 20, 30, 40),
}


def _encode(fmt="PNG", size=(8, 8), mode="RGB", **save_kw):
    buffer = io.BytesIO()
    Image.new(mode, size, _FILL[mode]).save(buffer, fmt, **save_kw)
    return buffer.getvalue()


def _animate(fmt="GIF", frames=4, size=(24, 24)):
    images = [Image.new("RGB", size, (index * 60 % 255, 30, 200 - index * 20)) for index in range(frames)]
    if fmt == "GIF":
        images = [image.convert("P") for image in images]

    buffer = io.BytesIO()
    images[0].save(buffer, format=fmt, save_all=True, append_images=images[1:], duration=80)
    return buffer.getvalue()




# -----------------------------------------------------------
# _bomb_png / _wide_gif
# -----------------------------------------------------------
#
# _bomb_png writes a PNG whose IHDR CLAIMS a huge canvas and
# carries no real pixels: Pillow sizes the image from the
# header alone, so the guard trips without the test ever
# allocating anything.
#
# _wide_gif is the other half of the same attack — frames that
# differ by one pixel, so the file stays kilobytes on the wire
# while its DECODED budget (frames x w x h) blows past 30 MP.
# -----------------------------------------------------------

def _bomb_png(width, height):
    def chunk(tag, data):
        return len(data).to_bytes(4, "big") + tag + data + zlib.crc32(tag + data).to_bytes(4, "big")

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", b"\x00") + chunk(b"IEND", b"")


def _big_endian_tiff(width=2, height=2, pixels=b"\x10\x20\x30\x40"):
    # Pillow only ever WRITES little-endian TIFF, so the
    # "MM\x00*" half of the signature table needs a file
    # assembled by hand: a 9-tag IFD describing an
    # uncompressed 8-bit greyscale strip, motorola byte order
    tags = [
        (256, 3, 1, width),        # ImageWidth
        (257, 3, 1, height),       # ImageLength
        (258, 3, 1, 8),            # BitsPerSample
        (259, 3, 1, 1),            # Compression: none
        (262, 3, 1, 1),            # Photometric: black is zero
        (273, 4, 1, 0),            # StripOffsets, patched below
        (277, 3, 1, 1),            # SamplesPerPixel
        (278, 3, 1, height),       # RowsPerStrip
        (279, 4, 1, len(pixels)),  # StripByteCounts
    ]
    data_offset = 8 + 2 + 12 * len(tags) + 4

    out = bytearray(b"MM\x00*" + (8).to_bytes(4, "big") + len(tags).to_bytes(2, "big"))
    for tag, kind, count, value in tags:
        if tag == 273:
            value = data_offset
        # A SHORT is left-justified inside the 4-byte field
        field = value.to_bytes(2, "big") + b"\x00\x00" if kind == 3 else value.to_bytes(4, "big")
        out += tag.to_bytes(2, "big") + kind.to_bytes(2, "big") + count.to_bytes(4, "big") + field

    return bytes(out + (0).to_bytes(4, "big") + pixels)


def _wide_gif(frames=90, size=(600, 600)):
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
# _post / _stored_names / _row / _seed_row
# -----------------------------------------------------------

def _post(client, headers, blob, filename="nuotrauka.png"):
    return client.post(
        UPLOADS,
        data={"file": (io.BytesIO(blob), filename)},
        headers=headers,
        content_type="multipart/form-data",
    )


def _stored_names(app):
    return sorted(os.listdir(app.config["UPLOAD_DIR"]))


def _stored_path(app, filename):
    return os.path.join(app.config["UPLOAD_DIR"], filename)


def _row(db, filename):
    return db.execute("SELECT * FROM uploads WHERE filename = ?", (filename,)).fetchone()


def _seed_row(db, user_id, byte_size, filename=None):
    filename = filename or f"{uuid.uuid4().hex}.jpg"
    db.execute(
        "INSERT INTO uploads (id, filename, user_id, byte_size, created_at)"
        " VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (str(uuid.uuid4()), filename, user_id, byte_size),
    )
    db.commit()
    return filename








# -----------------------------------------------------------
# _allowed_file — the cheap filename pre-filter
# -----------------------------------------------------------

@pytest.mark.parametrize("extension", list(ALLOWED_EXTENSIONS))
def test_every_advertised_extension_passes_the_pre_filter(extension):
    assert _allowed_file(f"nuotrauka.{extension}") is True


@pytest.mark.parametrize("filename", [
    "NUOTRAUKA.PNG",
    "nuotrauka.PnG",
    "nuotrauka.JPEG",
    "nuotrauka.TiFf",
])
def test_the_pre_filter_lowercases_the_extension_before_matching(filename):
    assert _allowed_file(filename) is True


@pytest.mark.parametrize("filename", ["blob", "image", "upload", "", "png", "a" * 500])
def test_a_name_with_no_extension_at_all_passes(filename):
    # The Expo web picker sends blobs called "blob"/"image" —
    # rejecting those was the old gate's real-world failure
    assert _allowed_file(filename) is True


@pytest.mark.parametrize("filename", [
    "dokumentas.pdf",
    "kenkejas.exe",
    "archyvas.zip",
    "puslapis.html",
    "scenarijus.svg",
    "video.mp4",
    "garsas.wav",
])
def test_a_positively_non_image_extension_is_refused(filename):
    assert _allowed_file(filename) is False


def test_only_the_last_extension_counts():
    assert _allowed_file("nuotrauka.png.exe") is False
    assert _allowed_file("kenkejas.exe.png") is True
    assert _allowed_file("archyvas.tar.gz") is False


def test_a_trailing_dot_leaves_an_empty_extension_which_is_not_an_image():
    assert _allowed_file("nuotrauka.") is False
    assert _allowed_file(".") is False


def test_a_dotfile_is_read_as_extension_only():
    # ".png" rsplits into ("", "png"), so a bare dotfile named
    # after a format passes and ".gitignore" does not
    assert _allowed_file(".png") is True
    assert _allowed_file("..png") is True
    assert _allowed_file(".gitignore") is False


@pytest.mark.parametrize("filename", [
    "nuotrauka.png ",
    "nuotrauka. png",
    "nuotrauka.png\n",
    "nuotrauka.png\x00",
    "nuotrauka.pnģ",
    "nuotrauka.p n g",
])
def test_the_extension_match_is_exact_and_unforgiving(filename):
    assert _allowed_file(filename) is False


def test_a_dot_in_a_directory_part_swallows_the_whole_tail():
    # rsplit takes everything after the LAST dot, path
    # separators included — "png/failas" is not an extension
    assert _allowed_file("katalogas.png/failas") is False
    assert _allowed_file("../../etc.png/passwd") is False


def test_a_traversal_attempt_that_still_ends_in_an_image_extension_passes():
    # The pre-filter only shapes a message; the stored name
    # never comes from here, so this passing is harmless
    assert _allowed_file("../../../etc/passwd.png") is True


def test_the_pre_filter_answers_a_bool_not_a_truthy_value():
    assert _allowed_file("blob") is True
    assert _allowed_file("x.pdf") is False


def test_the_pre_filter_requires_a_string_from_its_one_caller():
    # upload_file guards `if not file.filename` first, so a
    # non-string can never reach here; the contract is pinned
    # so a future caller cannot quietly rely on None working
    with pytest.raises(TypeError):
        _allowed_file(None)

    with pytest.raises(TypeError):
        _allowed_file(b"nuotrauka.png")








# -----------------------------------------------------------
# _validate_magic_bytes — the signature sniff
# -----------------------------------------------------------

@pytest.mark.parametrize("header, expected", [
    (b"\xff\xd8\xff\xe0", "jpg"),
    (b"\xff\xd8\xff\xdb\x00\x43", "jpg"),
    (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", "png"),
    (b"GIF87a\x10\x00", "gif"),
    (b"GIF89a\x10\x00", "gif"),
    (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "webp"),
    (b"BM\x36\x00\x00\x00", "bmp"),
    (b"II*\x00\x08\x00\x00\x00", "tiff"),
    (b"MM\x00*\x00\x00\x00\x08", "tiff"),
])
def test_every_signature_in_the_table_is_recognised(header, expected):
    assert _validate_magic_bytes(io.BytesIO(header)) == expected


@pytest.mark.parametrize("blob, expected", [
    (_encode("PNG"), "png"),
    (_encode("JPEG"), "jpg"),
    (_encode("BMP"), "bmp"),
    (_encode("TIFF"), "tiff"),
    (_encode("WEBP"), "webp"),
    (_animate("GIF"), "gif"),
])
def test_real_encoded_files_are_sniffed_as_their_own_format(blob, expected):
    assert _validate_magic_bytes(io.BytesIO(blob)) == expected


@pytest.mark.parametrize("header", [b"", b"\xff", b"\xff\xd8", b"\xff\xd8\xff", b"BM\x36", b"II*"])
def test_anything_shorter_than_four_bytes_cannot_be_sniffed(header):
    # The JPEG signature is three bytes long, so a stream of
    # EXACTLY the JPEG magic is still too short to answer
    assert _validate_magic_bytes(io.BytesIO(header)) is None


def test_four_bytes_is_the_smallest_header_that_can_match():
    assert _validate_magic_bytes(io.BytesIO(b"\xff\xd8\xff")) is None
    assert _validate_magic_bytes(io.BytesIO(b"\xff\xd8\xff\x00")) == "jpg"


@pytest.mark.parametrize("header", [
    b"%PDF-1.7",
    b"PK\x03\x04\x14\x00",
    b"\x7fELF\x02\x01\x01\x00",
    b"<!DOCTYPE html>",
    b"labas rytas nuo KNF",
    b"\x00" * 16,
    b"GIF88a\x10\x00",
    b"II*\x01\x08\x00\x00\x00",
    b"MM\x00+\x00\x00\x00\x08",
    b"\x89PNG\r\n\x1a\x00\x00\x00\x00\r",
])
def test_a_header_that_matches_nothing_answers_none(header):
    assert _validate_magic_bytes(io.BytesIO(header)) is None


def test_a_truncated_png_signature_is_not_a_png():
    # The PNG signature is eight bytes; four of them is not it
    assert _validate_magic_bytes(io.BytesIO(b"\x89PNG")) is None
    assert _validate_magic_bytes(io.BytesIO(b"\x89PNG\r\n\x1a\n")) == "png"


@pytest.mark.parametrize("header", [
    b"RIFF",
    b"RIFF\x24\x00\x00\x00",
    b"RIFF\x24\x00\x00\x00WEB",
    b"RIFF\x24\x00\x00\x00WAVEfmt ",
    b"RIFF\x24\x00\x00\x00AVI LIST",
    b"RIFF\x24\x00\x00\x00webp",
])
def test_a_riff_container_that_is_not_webp_is_never_a_webp(header):
    # RIFF alone is a container: a .wav and an .avi open with
    # the same four bytes, and the check is case sensitive
    assert _validate_magic_bytes(io.BytesIO(header)) is None


def test_the_shortest_possible_webp_header_is_exactly_twelve_bytes():
    assert _validate_magic_bytes(io.BytesIO(b"RIFF\x00\x00\x00\x00WEBP")) == "webp"
    assert _validate_magic_bytes(io.BytesIO(b"RIFF\x00\x00\x00\x00WEB")) is None


def test_the_sniff_rewinds_the_stream_to_zero():
    stream = io.BytesIO(_encode("PNG"))

    assert _validate_magic_bytes(stream) == "png"
    assert stream.tell() == 0


def test_a_stream_that_matched_nothing_is_still_rewound():
    stream = io.BytesIO(b"visai ne paveiksliukas, tik tekstas")

    assert _validate_magic_bytes(stream) is None
    assert stream.tell() == 0


def test_an_empty_stream_is_still_rewound():
    stream = io.BytesIO(b"")

    assert _validate_magic_bytes(stream) is None
    assert stream.tell() == 0


def test_the_sniff_reads_from_wherever_the_cursor_is_and_rewinds_to_the_start():
    # It does NOT restore the previous position — it always
    # leaves the stream at 0, which is what upload_file needs
    stream = io.BytesIO(b"XYZ" + _encode("PNG"))
    stream.seek(3)

    assert _validate_magic_bytes(stream) == "png"
    assert stream.tell() == 0


def test_a_png_read_from_byte_zero_when_the_cursor_moved_past_it_is_not_seen():
    stream = io.BytesIO(_encode("PNG"))
    stream.seek(2)

    assert _validate_magic_bytes(stream) is None
    assert stream.tell() == 0


# A stream that records how much the sniff asked for, so the
# "first 16 bytes, no more" claim is proven rather than assumed
class _CountingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.reads = []

    def read(self, size=-1):
        self.reads.append(size)
        return super().read(size)


def test_the_sniff_reads_sixteen_bytes_and_no_more():
    stream = _CountingStream(_encode("PNG", size=(200, 200)))

    assert _validate_magic_bytes(stream) == "png"
    assert stream.reads == [16]


def test_a_file_shorter_than_sixteen_bytes_is_sniffed_from_what_there_is():
    assert _validate_magic_bytes(io.BytesIO(b"GIF89a")) == "gif"
    assert _validate_magic_bytes(io.BytesIO(b"BM\x00\x00")) == "bmp"


def test_the_sniff_is_a_first_opinion_only_and_says_nothing_about_decodability():
    # "GIF89a" plus nothing is a gif to the sniff and garbage
    # to Pillow — the re-encode is where that gets settled
    assert _validate_magic_bytes(io.BytesIO(b"GIF89a")) == "gif"
    assert _reencode_image(b"GIF89a")[2][1] == "bad_file_content"








# -----------------------------------------------------------
# _reencode_image — bytes that do not decode
# -----------------------------------------------------------

@pytest.mark.parametrize("raw", [
    b"",
    b"x",
    b"visai ne paveiksliukas",
    b"\xff\xd8\xff\xe0" + b"\x00" * 64,
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    b"GIF89a" + b"\x00" * 64,
    b"BM" + b"\x00" * 64,
    b"RIFF\x24\x00\x00\x00WEBP" + b"\x00" * 64,
    _encode("PNG")[:20],
    _encode("JPEG")[:-40],
])
def test_bytes_that_do_not_decode_are_a_bad_file_content_rejection(raw):
    ext, blob, rejection = _reencode_image(raw)

    assert ext is None
    assert blob is None
    assert rejection == ("File content could not be read as an image", "bad_file_content")


def test_a_declared_decompression_bomb_is_refused_before_anything_is_allocated():
    ext, blob, rejection = _reencode_image(_bomb_png(40000, 40000))

    assert (ext, blob) == (None, None)
    assert rejection == ("Image too large. Max 30 megapixels", "image_too_large")


def test_the_megapixel_message_names_the_real_ceiling():
    _, _, rejection = _reencode_image(_bomb_png(50000, 50000))

    assert rejection[0] == f"Image too large. Max {MAX_IMAGE_PIXELS // (1000 * 1000)} megapixels"


def test_a_claim_under_pillows_own_ceiling_reaches_the_decoder_first():
    # Pillow only RAISES past TWICE Image.MAX_IMAGE_PIXELS, so
    # a header claiming 49 MP is handed to the decoder rather
    # than refused on sight: what a client sees then depends on
    # whether the pixels are really there — bad_file_content
    # for this hollow file, image_too_large for a real one
    _, _, rejection = _reencode_image(_bomb_png(7000, 7000))

    assert rejection == ("File content could not be read as an image", "bad_file_content")


def test_a_save_that_blows_up_is_a_bad_file_not_an_exception(monkeypatch):
    raw = _encode("PNG")

    def _boom(self, *args, **kwargs):
        raise OSError("encoder gone")

    monkeypatch.setattr(Image.Image, "save", _boom)

    ext, blob, rejection = _reencode_image(raw)

    assert (ext, blob) == (None, None)
    assert rejection == ("File content could not be read as an image", "bad_file_content")


@pytest.mark.parametrize("raw", [_encode("PNG"), _encode("PNG", mode="RGBA"), _animate("GIF")])
def test_every_encode_arm_survives_a_failing_save(monkeypatch, raw):
    # The three save() calls sit in three different branches;
    # each has to answer the same rejection, not a 500
    def _boom(self, *args, **kwargs):
        raise ValueError("no encoder for this build")

    monkeypatch.setattr(Image.Image, "save", _boom)

    assert _reencode_image(raw)[2] == ("File content could not be read as an image", "bad_file_content")


def test_the_decoded_image_is_closed_on_the_success_path(monkeypatch):
    closed = []
    real_close = Image.Image.close

    def _spy(self):
        closed.append(id(self))
        real_close(self)

    monkeypatch.setattr(Image.Image, "close", _spy)
    _reencode_image(_encode("PNG"))

    assert closed, "the finally arm must close the decoded image"








# -----------------------------------------------------------
# _reencode_image — the pixel budget
# -----------------------------------------------------------

def test_the_pixel_ceiling_is_exclusive_so_the_exact_ceiling_passes(monkeypatch):
    monkeypatch.setattr(uploads_routes, "MAX_IMAGE_PIXELS", 400)

    ext, blob, rejection = _reencode_image(_encode("PNG", size=(20, 20)))

    assert rejection is None
    assert (ext, blob is not None) == ("jpg", True)


def test_one_pixel_past_the_ceiling_is_refused(monkeypatch):
    monkeypatch.setattr(uploads_routes, "MAX_IMAGE_PIXELS", 400)

    ext, blob, rejection = _reencode_image(_encode("PNG", size=(401, 1)))

    assert (ext, blob) == (None, None)
    assert rejection[1] == "image_too_large"


def test_the_budget_multiplies_the_frame_count_in(monkeypatch):
    monkeypatch.setattr(uploads_routes, "MAX_IMAGE_PIXELS", 4 * 10 * 10)

    assert _reencode_image(_animate("GIF", frames=4, size=(10, 10)))[2] is None
    assert _reencode_image(_animate("GIF", frames=5, size=(10, 10)))[2][1] == "image_too_large"


def test_a_still_image_counts_as_exactly_one_frame(monkeypatch):
    # A JPEG has no n_frames attribute at all, so the getattr
    # default is what keeps the budget from collapsing to zero
    monkeypatch.setattr(uploads_routes, "MAX_IMAGE_PIXELS", 100)

    assert _reencode_image(_encode("JPEG", size=(10, 10)))[2] is None
    assert _reencode_image(_encode("JPEG", size=(10, 11)))[2][1] == "image_too_large"


@pytest.mark.slow
def test_a_many_frame_gif_cannot_smuggle_a_bomb_past_a_single_frame_check():
    # 90 frames of 600x600 is 32.4 MP decoded and a few KB on
    # the wire — the real ceiling, no monkeypatching
    blob = _wide_gif()
    assert len(blob) < MAX_FILE_SIZE

    ext, out, rejection = _reencode_image(blob)

    assert (ext, out) == (None, None)
    assert rejection == ("Image too large. Max 30 megapixels", "image_too_large")








# -----------------------------------------------------------
# _reencode_image — the canonical stored format
# -----------------------------------------------------------

@pytest.mark.parametrize("mode, fmt", [
    ("RGB", "PNG"),
    ("L", "PNG"),
    ("1", "PNG"),
    ("P", "PNG"),
    ("RGB", "BMP"),
    ("RGB", "TIFF"),
    ("RGB", "WEBP"),
    ("RGB", "JPEG"),
    ("CMYK", "JPEG"),
])
def test_an_opaque_still_image_becomes_a_progressive_jpeg(mode, fmt):
    ext, blob, rejection = _reencode_image(_encode(fmt, mode=mode, size=(12, 9)))

    assert rejection is None
    assert ext == "jpg"

    stored = Image.open(io.BytesIO(blob))
    assert stored.format == "JPEG"
    assert stored.mode == "RGB"
    assert stored.info.get("progression")


@pytest.mark.parametrize("mode", ["RGBA", "LA"])
def test_an_alpha_channel_forces_png(mode):
    ext, blob, rejection = _reencode_image(_encode("PNG", mode=mode, size=(12, 9)))

    assert rejection is None
    assert ext == "png"

    stored = Image.open(io.BytesIO(blob))
    assert stored.format == "PNG"
    assert stored.mode == "RGBA"


def test_a_palette_image_with_a_transparency_index_becomes_png():
    raw = _encode("PNG", mode="P", size=(10, 10), transparency=0)

    ext, blob, rejection = _reencode_image(raw)

    assert (ext, rejection) == ("png", None)
    assert Image.open(io.BytesIO(blob)).format == "PNG"


def test_a_palette_image_without_transparency_becomes_a_jpeg():
    ext, blob, rejection = _reencode_image(_encode("PNG", mode="P", size=(10, 10)))

    assert (ext, rejection) == ("jpg", None)
    assert Image.open(io.BytesIO(blob)).format == "JPEG"


def test_an_animated_gif_stays_a_gif_with_every_frame():
    ext, blob, rejection = _reencode_image(_animate("GIF", frames=6))

    assert (ext, rejection) == ("gif", None)
    stored = Image.open(io.BytesIO(blob))
    assert stored.format == "GIF"
    assert stored.n_frames == 6


def test_an_animated_webp_stays_a_webp():
    # Re-encoding an animated WebP as GIF can grow it many
    # times over, so the container is deliberately kept
    ext, blob, rejection = _reencode_image(_animate("WEBP", frames=4))

    assert (ext, rejection) == ("webp", None)
    stored = Image.open(io.BytesIO(blob))
    assert stored.format == "WEBP"
    assert stored.n_frames == 4


def test_an_animated_png_is_re_encoded_as_a_gif():
    # An APNG is multi-frame but not WEBP, so it falls into
    # the GIF arm — the one input no client sends on purpose
    raw = _animate("PNG", frames=3, size=(16, 16))
    assert Image.open(io.BytesIO(raw)).n_frames == 3

    ext, blob, rejection = _reencode_image(raw)

    assert (ext, rejection) == ("gif", None)
    assert Image.open(io.BytesIO(blob)).format == "GIF"


def test_an_animation_with_transparency_stays_an_animation():
    # frames > 1 is tested FIRST, so a transparent palette in
    # an animated GIF never diverts it onto the PNG arm
    images = []
    for index in range(4):
        frame = Image.new("P", (12, 12), 0)
        frame.putpalette([0, 0, 0, 255, 0, 0] + [0] * (768 - 6))
        frame.putpixel((index, 0), 1)
        images.append(frame)

    buffer = io.BytesIO()
    images[0].save(buffer, format="GIF", save_all=True, append_images=images[1:], transparency=0)

    ext, blob, rejection = _reencode_image(buffer.getvalue())

    assert (ext, rejection) == ("gif", None)
    assert Image.open(io.BytesIO(blob)).n_frames == 4


def test_a_still_webp_with_an_alpha_channel_becomes_a_png():
    ext, blob, rejection = _reencode_image(_encode("WEBP", mode="RGBA", size=(20, 20)))

    assert (ext, rejection) == ("png", None)
    assert Image.open(io.BytesIO(blob)).format == "PNG"


def test_a_single_frame_gif_is_not_an_animation_and_becomes_a_jpeg():
    ext, blob, rejection = _reencode_image(_animate("GIF", frames=1, size=(20, 20)))

    assert (ext, rejection) == ("jpg", None)
    assert Image.open(io.BytesIO(blob)).format == "JPEG"


def test_a_single_frame_webp_is_not_an_animation_and_becomes_a_jpeg():
    ext, blob, rejection = _reencode_image(_encode("WEBP", size=(20, 20)))

    assert (ext, rejection) == ("jpg", None)
    assert Image.open(io.BytesIO(blob)).format == "JPEG"


def test_the_returned_extension_is_always_one_the_serving_route_admits():
    for raw in (_encode("PNG"), _encode("PNG", mode="RGBA"), _animate("GIF"), _animate("WEBP")):
        ext, blob, rejection = _reencode_image(raw)
        assert rejection is None
        assert STORED_NAME_RE.match(f"{'0' * 32}.{ext}")
        assert blob








# -----------------------------------------------------------
# _reencode_image — downscaling and metadata
# -----------------------------------------------------------

@pytest.mark.parametrize("size, expected", [
    ((3000, 1000), (2048, 683)),
    ((1000, 3000), (683, 2048)),
    ((4096, 4096), (2048, 2048)),
    ((2049, 2049), (2048, 2048)),
])
def test_a_still_image_is_downscaled_to_the_longest_edge(size, expected):
    _, blob, _ = _reencode_image(_encode("PNG", size=size))

    assert Image.open(io.BytesIO(blob)).size == expected


@pytest.mark.parametrize("size", [(1, 1), (24, 18), (MAX_EDGE, 10), (MAX_EDGE, MAX_EDGE)])
def test_an_image_at_or_under_the_edge_limit_is_left_alone(size):
    _, blob, _ = _reencode_image(_encode("PNG", size=size))

    assert Image.open(io.BytesIO(blob)).size == size


def test_a_transparent_image_is_downscaled_on_the_png_arm_too():
    _, blob, _ = _reencode_image(_encode("PNG", mode="RGBA", size=(3000, 300)))

    stored = Image.open(io.BytesIO(blob))
    assert stored.size == (2048, 205)
    assert stored.format == "PNG"


def test_an_animation_is_never_downscaled():
    # A per-frame resize desyncs GIF palettes, so oversize
    # animations keep their frame size on purpose
    _, blob, _ = _reencode_image(_animate("GIF", frames=3, size=(3000, 60)))

    assert Image.open(io.BytesIO(blob)).size == (3000, 60)


def test_exif_and_gps_never_survive_the_re_encode():
    image = Image.new("RGB", (16, 16), (200, 10, 90))
    exif = Image.Exif()
    exif[0x010F] = "KNF-KAMERA"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (54.0, 53.0, 0.0)

    source = io.BytesIO()
    image.save(source, "JPEG", exif=exif)
    assert b"KNF-KAMERA" in source.getvalue()

    _, blob, _ = _reencode_image(source.getvalue())

    assert b"KNF-KAMERA" not in blob
    assert not Image.open(io.BytesIO(blob)).getexif()


def test_a_png_text_chunk_never_survives_the_re_encode():
    from PIL import PngImagePlugin

    meta = PngImagePlugin.PngInfo()
    meta.add_text("Autorius", "KNF-SLAPTA")
    source = io.BytesIO()
    Image.new("RGBA", (10, 10), (1, 2, 3, 200)).save(source, "PNG", pnginfo=meta)
    assert b"KNF-SLAPTA" in source.getvalue()

    _, blob, _ = _reencode_image(source.getvalue())

    assert b"KNF-SLAPTA" not in blob


def test_the_canonical_bytes_are_a_fresh_container_not_the_original():
    raw = _encode("PNG", size=(30, 30))

    _, blob, _ = _reencode_image(raw)

    assert blob != raw
    assert blob.startswith(b"\xff\xd8\xff")








# -----------------------------------------------------------
# _get_upload_dir — resolved and created once per process
# -----------------------------------------------------------

def test_the_upload_directory_is_absolute_and_exists(app):
    with app.app_context():
        resolved = _get_upload_dir()

    assert os.path.isabs(resolved)
    assert os.path.isdir(resolved)


def test_the_resolution_is_cached_in_the_module_global(app):
    with app.app_context():
        resolved = _get_upload_dir()

    assert uploads_routes._upload_dir == resolved


def test_a_later_config_change_cannot_move_a_resolved_directory(app, tmp_path):
    # Cached once per PROCESS on purpose: the makedirs used to
    # run on every public image GET
    with app.app_context():
        first = _get_upload_dir()
        app.config["UPLOAD_DIR"] = str(tmp_path / "niekada-nenaudojamas")
        assert _get_upload_dir() == first

    assert not os.path.exists(tmp_path / "niekada-nenaudojamas")


def test_the_directory_is_created_only_once_however_often_it_is_asked_for(app, monkeypatch):
    calls = []
    real_makedirs = os.makedirs

    def _spy(path, **kwargs):
        calls.append(path)
        return real_makedirs(path, **kwargs)

    monkeypatch.setattr(os, "makedirs", _spy)

    with app.app_context():
        for _ in range(5):
            _get_upload_dir()

    assert len(calls) == 1


def test_a_missing_directory_is_created_on_demand(app, tmp_path):
    target = tmp_path / "naujas" / "gilus" / "uploads"
    app.config["UPLOAD_DIR"] = str(target)

    with app.app_context():
        resolved = _get_upload_dir()

    assert resolved == str(target)
    assert os.path.isdir(resolved)


def test_an_existing_directory_is_accepted_without_complaint(app, tmp_path):
    target = tmp_path / "jau-yra"
    target.mkdir()
    app.config["UPLOAD_DIR"] = str(target)

    with app.app_context():
        assert _get_upload_dir() == str(target)


def test_a_relative_looking_config_value_is_normalised(app, tmp_path):
    app.config["UPLOAD_DIR"] = str(tmp_path / "a" / ".." / "uploads-normalizuotas")

    with app.app_context():
        resolved = _get_upload_dir()

    assert resolved == str(tmp_path / "uploads-normalizuotas")
    assert ".." not in resolved


def test_a_trailing_separator_is_stripped_by_the_resolution(app, tmp_path):
    target = tmp_path / "su-brukšniu"
    target.mkdir()
    app.config["UPLOAD_DIR"] = f"{target}{os.sep}"

    with app.app_context():
        assert _get_upload_dir() == str(target)


def test_resolving_needs_an_application_context(app):
    with pytest.raises(RuntimeError):
        _get_upload_dir()


def test_a_directory_that_cannot_be_created_raises_and_is_not_cached(app, monkeypatch):
    def _boom(path, **kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(os, "makedirs", _boom)

    with app.app_context():
        with pytest.raises(OSError):
            _get_upload_dir()

    assert uploads_routes._upload_dir is None, "a failed resolution must not poison the cache"


def test_the_resolved_directory_is_the_one_the_route_writes_into(client, actor, app):
    _, headers = actor

    name = _post(client, headers, _encode("PNG")).get_json()["filename"]

    with app.app_context():
        assert os.path.isfile(os.path.join(_get_upload_dir(), name))








# -----------------------------------------------------------
# POST /api/uploads — the authentication gate
# -----------------------------------------------------------

def test_an_anonymous_upload_is_refused_before_the_body_matters(client, app):
    response = client.post(UPLOADS, data={}, content_type="multipart/form-data")

    assert response.status_code == 401
    assert response.get_json() == {"error": "Authentication required"}
    assert _stored_names(app) == []


@pytest.mark.parametrize("header", [
    {"Authorization": "Bearer ne-toks-zetonas"},
    {"Authorization": "Bearer "},
    {"Authorization": "Basic YWRtaW46YWRtaW4="},
    {"Authorization": "3f8f3de8-a851-4ac4-a2e0-476694508600"},
    {"Authorization": "Bearer Bearer 3f8f3de8-a851-4ac4-a2e0-476694508600"},
])
def test_no_malformed_credential_gets_past_the_gate(client, app, header):
    response = _post(client, header, _encode("PNG"))

    assert response.status_code == 401
    assert _stored_names(app) == []


def test_a_deactivated_account_can_no_longer_upload(client, make_user, auth_headers, db, app):
    user = make_user()
    headers = auth_headers(user)
    db.execute("UPDATE users SET active = 0 WHERE id = ?", (user["id"],))
    db.commit()

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 401
    assert _stored_names(app) == []


@pytest.mark.parametrize("role", ["student", "teacher", "curator", "admin"])
def test_every_role_may_upload(client, make_user, auth_headers, role):
    headers = auth_headers(make_user(role=role))

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 201








# -----------------------------------------------------------
# POST /api/uploads — picking the file out of the body
# -----------------------------------------------------------

def test_a_body_with_no_file_part_at_all_is_refused(client, actor):
    _, headers = actor

    response = client.post(UPLOADS, data={}, headers=headers, content_type="multipart/form-data")

    assert response.status_code == 400
    assert response.get_json() == {"error": "No file provided", "code": "no_file"}


@pytest.mark.parametrize("field", ["files", "image", "photo", "File", "FILE", "file "])
def test_the_file_field_name_is_exact_and_case_sensitive(client, actor, field):
    _, headers = actor

    response = client.post(
        UPLOADS,
        data={field: (io.BytesIO(_encode("PNG")), "nuotrauka.png")},
        headers=headers,
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "no_file"


def test_a_plain_text_form_field_named_file_is_not_a_file(client, actor):
    _, headers = actor

    response = client.post(UPLOADS, data={"file": "labas"}, headers=headers,
                           content_type="multipart/form-data")

    assert response.status_code == 400
    assert response.get_json()["code"] == "no_file"


def test_a_json_body_has_no_file_part(client, actor):
    _, headers = actor

    response = client.post(UPLOADS, json={"file": "nuotrauka.png"}, headers=headers)

    assert response.status_code == 400
    assert response.get_json()["code"] == "no_file"


def test_a_urlencoded_body_has_no_file_part(client, actor):
    _, headers = actor

    response = client.post(UPLOADS, data={"file": "labas"}, headers=headers,
                           content_type="application/x-www-form-urlencoded")

    assert response.status_code == 400
    assert response.get_json()["code"] == "no_file"


def test_a_file_part_with_an_empty_filename_is_refused(client, actor, app):
    _, headers = actor

    response = _post(client, headers, _encode("PNG"), filename="")

    assert response.status_code == 400
    assert response.get_json() == {"error": "No file selected", "code": "no_file"}
    assert _stored_names(app) == []


def test_a_whitespace_only_filename_is_still_a_filename(client, actor):
    # Only the BYTES decide; a name of spaces is truthy, so it
    # reaches the content check like any other
    _, headers = actor

    response = _post(client, headers, _encode("PNG"), filename="   ")

    assert response.status_code == 201


def test_only_the_first_part_named_file_is_read(client, actor, app):
    _, headers = actor

    response = client.post(
        UPLOADS,
        data={"file": [
            (io.BytesIO(_encode("PNG")), "pirma.png"),
            (io.BytesIO(_animate("GIF")), "antra.gif"),
        ]},
        headers=headers,
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    assert response.get_json()["filename"].endswith(".jpg"), "the still PNG came first"
    assert len(_stored_names(app)) == 1


def test_extra_form_fields_beside_the_file_are_ignored(client, actor):
    _, headers = actor

    response = client.post(
        UPLOADS,
        data={
            "file": (io.BytesIO(_encode("PNG")), "nuotrauka.png"),
            "filename": "../../knfapp.db",
            "user_id": "kitas-vartotojas",
            "byte_size": "0",
        },
        headers=headers,
        content_type="multipart/form-data",
    )

    assert response.status_code == 201








# -----------------------------------------------------------
# POST /api/uploads — the size gates
# -----------------------------------------------------------

def test_an_empty_file_is_refused(client, actor, app, db):
    _, headers = actor

    response = _post(client, headers, b"", filename="tuscia.png")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Empty file", "code": "empty_file"}
    assert _stored_names(app) == []
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 0


def test_the_size_gate_runs_before_the_type_gate(client, actor):
    # An empty file with a forbidden extension is EMPTY, not
    # bad_file_type — the order the banner promises
    _, headers = actor

    assert _post(client, headers, b"", filename="tuscia.pdf").get_json()["code"] == "empty_file"


def test_one_byte_is_not_an_empty_file(client, actor):
    _, headers = actor

    response = _post(client, headers, b"\xff", filename="vienas-baitas.png")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content", "past the size gate, into the sniff"


def test_exactly_the_five_megabyte_cap_gets_past_the_size_gate(client, actor):
    # The gate is "> MAX_FILE_SIZE", so 5 MB on the nose must
    # reach the sniff — here with a PNG header, so it gets all
    # the way to the re-encode before being refused
    _, headers = actor
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_FILE_SIZE - 8)
    assert len(blob) == MAX_FILE_SIZE

    response = _post(client, headers, blob, filename="riba.png")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"


def test_one_byte_over_the_cap_is_refused_with_the_size_slug(client, actor, app):
    _, headers = actor

    response = _post(client, headers, b"\x00" * (MAX_FILE_SIZE + 1), filename="didele.png")

    assert response.status_code == 400
    assert response.get_json() == {"error": "File too large. Max 5 MB", "code": "file_too_large"}
    assert _stored_names(app) == []


def test_the_size_gate_beats_the_extension_gate(client, actor):
    _, headers = actor

    response = _post(client, headers, b"\x00" * (MAX_FILE_SIZE + 1), filename="didelis.pdf")

    assert response.get_json()["code"] == "file_too_large"


def test_a_valid_image_well_under_the_cap_is_stored(client, actor):
    _, headers = actor
    blob = _encode("PNG", size=(64, 64))
    assert 0 < len(blob) < MAX_FILE_SIZE

    assert _post(client, headers, blob).status_code == 201


def test_a_body_past_the_werkzeug_ceiling_never_reaches_the_route(client, actor, app):
    # MAX_CONTENT_LENGTH is 6 MB — one megabyte of headroom
    # over the route's own cap, and the same slug either way
    _, headers = actor

    response = _post(client, headers, b"\x00" * (6 * 1024 * 1024 + 1024), filename="milzine.png")

    assert response.status_code == 413
    assert response.get_json() == {"error": "File too large", "code": "file_too_large"}
    assert _stored_names(app) == []








# -----------------------------------------------------------
# POST /api/uploads — the signature decides, the name only
# shapes the message
# -----------------------------------------------------------

@pytest.mark.parametrize("filename", [
    "dokumentas.pdf",
    "kenkejas.exe",
    "puslapis.html",
    "scenarijus.svg",
    "video.mp4",
    "archyvas.tar.gz",
])
def test_unsniffable_bytes_under_a_non_image_name_are_a_bad_file_type(client, actor, filename):
    _, headers = actor

    response = _post(client, headers, b"%PDF-1.7 tai visai ne vaizdas", filename=filename)

    assert response.status_code == 400
    body = response.get_json()
    assert body["code"] == "bad_file_type"
    assert body["error"] == f"File type not allowed. Use: {', '.join(ALLOWED_EXTENSIONS)}"


def test_the_rejection_message_lists_every_extension_in_a_stable_order(client, actor):
    _, headers = actor

    error = _post(client, headers, b"ne vaizdas", filename="x.pdf").get_json()["error"]

    assert error.endswith("bmp, gif, jpeg, jpg, png, tif, tiff, webp")


@pytest.mark.parametrize("filename", ["nuotrauka.png", "nuotrauka.JPG", "nuotrauka.webp"])
def test_unsniffable_bytes_under_an_image_name_are_a_bad_file_content(client, actor, filename):
    _, headers = actor

    response = _post(client, headers, b"tekstas apsimetantis vaizdu", filename=filename)

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "File content does not match an allowed image format",
        "code": "bad_file_content",
    }


@pytest.mark.parametrize("filename", ["blob", "image", "upload"])
def test_an_extensionless_blob_is_judged_on_its_content_alone(client, actor, filename):
    # The Expo web picker sends exactly these names
    _, headers = actor

    assert _post(client, headers, b"ne vaizdas", filename=filename).get_json()["code"] == "bad_file_content"
    assert _post(client, headers, _encode("PNG"), filename=filename).status_code == 201


@pytest.mark.parametrize("filename", ["dokumentas.pdf", "kenkejas.exe", "blob", "be-galo.tar.gz"])
def test_a_real_image_under_any_name_at_all_is_stored(client, actor, filename):
    _, headers = actor

    response = _post(client, headers, _encode("PNG"), filename=filename)

    assert response.status_code == 201
    assert response.get_json()["filename"].endswith(".jpg"), "the bytes named the extension"


@pytest.mark.parametrize("blob, ext", [
    (_encode("BMP"), "jpg"),
    (_encode("TIFF"), "jpg"),
    (_encode("WEBP"), "jpg"),
    (_encode("JPEG"), "jpg"),
    (_animate("GIF"), "gif"),
    (_animate("WEBP"), "webp"),
])
def test_every_sniffable_format_reaches_the_re_encode(client, actor, blob, ext):
    _, headers = actor

    response = _post(client, headers, blob, filename="nezinoma")

    assert response.status_code == 201
    assert response.get_json()["filename"].endswith(f".{ext}")


def test_a_big_endian_tiff_is_stored_like_a_little_endian_one(client, actor):
    # Both halves of the TIFF signature table are real files,
    # not just header prefixes: this one decodes and lands
    _, headers = actor
    raw = _big_endian_tiff()
    assert raw.startswith(b"MM\x00*")

    response = _post(client, headers, raw, filename="be-pletinio")

    assert response.status_code == 201
    assert response.get_json()["filename"].endswith(".jpg")


def test_a_riff_container_that_is_not_a_webp_is_refused_on_its_content(client, actor):
    _, headers = actor
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32

    response = _post(client, headers, wav, filename="garsas.png")

    assert response.get_json()["code"] == "bad_file_content"


def test_a_header_that_sniffs_but_does_not_decode_is_a_bad_file_content(client, actor, app, db):
    _, headers = actor

    response = _post(client, headers, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, filename="melagis.png")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"
    assert _stored_names(app) == []
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 0








# -----------------------------------------------------------
# POST /api/uploads — the re-encode rejections
# -----------------------------------------------------------

def test_a_declared_bomb_is_refused_with_the_megapixel_slug(client, actor, app):
    _, headers = actor

    response = _post(client, headers, _bomb_png(40000, 40000), filename="bomba.png")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "Image too large. Max 30 megapixels",
        "code": "image_too_large",
    }
    assert _stored_names(app) == []


def test_a_re_encode_that_blows_up_is_a_400_not_a_500(client, actor, app, monkeypatch):
    _, headers = actor
    raw = _encode("PNG")

    def _boom(self, *args, **kwargs):
        raise OSError("encoder gone")

    monkeypatch.setattr(Image.Image, "save", _boom)

    response = _post(client, headers, raw)

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_file_content"
    assert _stored_names(app) == []


def test_a_re_encode_rejection_leaves_the_quota_untouched(client, actor, db):
    _, headers = actor

    _post(client, headers, _bomb_png(40000, 40000), filename="bomba.png")

    assert db.execute("SELECT COALESCE(SUM(byte_size), 0) AS t FROM uploads").fetchone()["t"] == 0








# -----------------------------------------------------------
# POST /api/uploads — the per-account storage quota
# -----------------------------------------------------------

def test_a_first_upload_on_an_empty_account_is_never_a_quota_problem(client, actor, db):
    user, headers = actor
    assert db.execute("SELECT COUNT(*) AS c FROM uploads WHERE user_id = ?",
                      (user["id"],)).fetchone()["c"] == 0

    assert _post(client, headers, _encode("PNG")).status_code == 201


def test_an_upload_that_fills_the_quota_exactly_is_admitted(client, actor, db):
    # The gate is "used + new > QUOTA", so landing exactly on
    # the ceiling must pass
    user, headers = actor
    _, blob, _ = _reencode_image(_encode("PNG", size=(40, 40)))
    _seed_row(db, user["id"], UPLOAD_QUOTA_BYTES - len(blob))

    response = _post(client, headers, _encode("PNG", size=(40, 40)))

    assert response.status_code == 201


def test_one_byte_past_the_quota_is_refused_with_a_413(client, actor, db, app):
    user, headers = actor
    _, blob, _ = _reencode_image(_encode("PNG", size=(40, 40)))
    _seed_row(db, user["id"], UPLOAD_QUOTA_BYTES - len(blob) + 1)

    response = _post(client, headers, _encode("PNG", size=(40, 40)))

    assert response.status_code == 413
    assert response.get_json() == {
        "error": "Storage quota reached. Max 100 MB per account",
        "code": "quota_exceeded",
    }
    assert _stored_names(app) == []


def test_a_completely_full_account_is_refused(client, actor, db):
    user, headers = actor
    _seed_row(db, user["id"], UPLOAD_QUOTA_BYTES)

    assert _post(client, headers, _encode("PNG")).status_code == 413


def test_a_quota_rejection_writes_neither_file_nor_row(client, actor, db, app):
    user, headers = actor
    _seed_row(db, user["id"], UPLOAD_QUOTA_BYTES)

    _post(client, headers, _encode("PNG"))

    assert _stored_names(app) == []
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 1


def test_the_quota_counts_only_the_uploaders_own_rows(client, actor, make_user, db):
    user, headers = actor
    stranger = make_user()
    _seed_row(db, stranger["id"], UPLOAD_QUOTA_BYTES * 4)

    assert _post(client, headers, _encode("PNG")).status_code == 201


def test_an_ownerless_row_counts_against_nobody(client, actor, db):
    # users.id is ON DELETE SET NULL, so a departed account
    # leaves rows no quota query can ever match
    _, headers = actor
    db.execute(
        "INSERT INTO uploads (id, filename, user_id, byte_size, created_at)"
        " VALUES (?, ?, NULL, ?, '2026-01-01T00:00:00+00:00')",
        (str(uuid.uuid4()), f"{uuid.uuid4().hex}.jpg", UPLOAD_QUOTA_BYTES * 2),
    )
    db.commit()

    assert _post(client, headers, _encode("PNG")).status_code == 201


def test_the_quota_counts_rows_not_files_on_disk(client, actor, db, app):
    # The row is the accounting record: deleting the file by
    # hand does not give the megabytes back
    user, headers = actor
    _seed_row(db, user["id"], UPLOAD_QUOTA_BYTES)
    assert _stored_names(app) == []

    assert _post(client, headers, _encode("PNG")).status_code == 413


def test_many_small_rows_add_up_to_the_same_ceiling(client, actor, db):
    user, headers = actor
    for _ in range(10):
        _seed_row(db, user["id"], UPLOAD_QUOTA_BYTES // 10)

    assert _post(client, headers, _encode("PNG")).status_code == 413


def test_zero_sized_rows_never_push_an_account_over(client, actor, db):
    user, headers = actor
    for _ in range(50):
        _seed_row(db, user["id"], 0)

    assert _post(client, headers, _encode("PNG")).status_code == 201


def test_the_re_encode_is_judged_before_the_quota_is_counted(client, actor, db):
    # A bomb from a full account is a bomb, not a quota
    # problem — the order the STEP numbering promises
    user, headers = actor
    _seed_row(db, user["id"], UPLOAD_QUOTA_BYTES)

    response = _post(client, headers, _bomb_png(40000, 40000), filename="bomba.png")

    assert response.status_code == 400
    assert response.get_json()["code"] == "image_too_large"


def test_the_quota_is_counted_before_a_single_byte_is_written(client, actor, app, db):
    user, headers = actor
    _seed_row(db, user["id"], UPLOAD_QUOTA_BYTES)

    _post(client, headers, _encode("PNG"))

    assert _stored_names(app) == [], "not even a .part file is created"


def test_the_quota_counts_the_re_encoded_size_not_the_uploaded_one(client, actor, app, db):
    # A 3000 px PNG shrinks hard on the way in; what the quota
    # spends is what was STORED, never what was posted
    user, headers = actor
    raw = _encode("PNG", size=(3000, 3000))

    name = _post(client, headers, raw).get_json()["filename"]

    row = _row(db, name)
    assert row["user_id"] == user["id"]
    assert row["byte_size"] == os.path.getsize(_stored_path(app, name))
    assert row["byte_size"] != len(raw)








# -----------------------------------------------------------
# POST /api/uploads — the atomic write
# -----------------------------------------------------------

def test_a_successful_upload_leaves_no_part_file_behind(client, actor, app):
    _, headers = actor

    name = _post(client, headers, _encode("PNG")).get_json()["filename"]

    assert _stored_names(app) == [name]
    assert not any(entry.endswith(".part") for entry in _stored_names(app))


def test_a_failing_rename_answers_507_and_cleans_up(client, actor, app, db, monkeypatch):
    _, headers = actor

    def _boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", _boom)

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 507
    assert response.get_json() == {
        "error": "Storage is unavailable, try again later",
        "code": "storage_unavailable",
    }
    assert _stored_names(app) == []
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 0


def test_a_failing_open_of_the_part_file_answers_507(client, actor, app, monkeypatch):
    # The disk fills before a single byte is written, so the
    # cleanup unlink hits a file that was never created — an
    # OSError the handler swallows on purpose
    _, headers = actor
    real_open = builtins.open

    def _fail_part(path, *args, **kwargs):
        if isinstance(path, str) and path.endswith(".part"):
            raise OSError(28, "No space left on device")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _fail_part)

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 507
    assert response.get_json()["code"] == "storage_unavailable"
    assert _stored_names(app) == []


def test_a_failing_fsync_answers_507_and_removes_the_partial_file(client, actor, app, monkeypatch):
    _, headers = actor

    def _boom(fd):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", _boom)

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 507
    assert _stored_names(app) == [], "the .part file is unlinked by the handler"


def test_a_failing_cleanup_after_a_failing_write_still_answers_507(client, actor, app, monkeypatch):
    _, headers = actor

    def _boom(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(os, "replace", _boom)
    monkeypatch.setattr(os, "unlink", _boom)

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 507
    assert response.get_json()["code"] == "storage_unavailable"
    leftovers = _stored_names(app)
    assert len(leftovers) == 1 and leftovers[0].endswith(".part"), (
        "the .part survives an unlinkable volume — the orphan sweep's problem, not the caller's"
    )


def test_a_storage_failure_leaves_the_account_able_to_try_again(client, actor, app, db, monkeypatch):
    _, headers = actor

    def _boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", _boom)
    assert _post(client, headers, _encode("PNG")).status_code == 507
    monkeypatch.undo()

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 201
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 1








# -----------------------------------------------------------
# POST /api/uploads — the stored name, the row and the answer
# -----------------------------------------------------------

def test_a_successful_upload_answers_201_with_exactly_two_fields(client, actor):
    _, headers = actor

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 201
    assert response.headers["Content-Type"].startswith("application/json")
    body = response.get_json()
    assert set(body) == {"url", "filename"}


def test_the_url_is_the_relative_one_clients_persist(client, actor):
    _, headers = actor

    body = _post(client, headers, _encode("PNG")).get_json()

    assert body["url"] == f"/api/uploads/{body['filename']}"
    assert body["url"].startswith("/api/uploads/")
    assert "://" not in body["url"]


def test_the_stored_name_is_a_uuid4_hex_and_a_canonical_extension(client, actor):
    _, headers = actor

    name = _post(client, headers, _encode("PNG")).get_json()["filename"]

    assert STORED_NAME_RE.match(name)
    assert uuid.UUID(hex=name.split(".")[0]).version == 4


@pytest.mark.parametrize("filename", [
    "../../../etc/passwd.png",
    "....//pabegimas.png",
    "..%2f..%2fknfapp.db",
    "/absoliutus/kelias.png",
    "C:\\Windows\\system32\\nuotrauka.png",
    "nuotrauka; rm -rf /.png",
    "ąžuolas-vasarą.jpg",
    "a" * 400 + ".png",
    "   ",
    "\u202enuotrauka.png",
])
def test_no_client_filename_can_steer_where_the_file_lands(client, actor, app, filename):
    _, headers = actor

    response = _post(client, headers, _encode("PNG"), filename=filename)

    assert response.status_code == 201
    name = response.get_json()["filename"]
    assert STORED_NAME_RE.match(name)
    assert _stored_names(app) == [name]


def test_the_same_bytes_uploaded_twice_are_two_separate_files(client, actor, app, db):
    # Uploads are never de-duplicated: two references must be
    # able to die independently
    _, headers = actor
    blob = _encode("PNG")

    first = _post(client, headers, blob).get_json()["filename"]
    second = _post(client, headers, blob).get_json()["filename"]

    assert first != second
    assert _stored_names(app) == sorted([first, second])
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == 2


def test_the_row_records_the_owner_the_stored_size_and_a_utc_stamp(client, actor, app, db):
    user, headers = actor

    name = _post(client, headers, _encode("PNG", size=(300, 200))).get_json()["filename"]

    row = _row(db, name)
    assert row["user_id"] == user["id"]
    assert row["filename"] == name
    assert row["byte_size"] == os.path.getsize(_stored_path(app, name))
    assert row["created_at"].endswith("+00:00")
    assert uuid.UUID(row["id"]).version == 4


def test_the_bytes_on_disk_are_the_re_encoded_ones_not_the_uploaded_ones(client, actor, app):
    _, headers = actor
    raw = _encode("PNG", size=(64, 64))

    name = _post(client, headers, raw).get_json()["filename"]

    with open(_stored_path(app, name), "rb") as handle:
        stored = handle.read()

    assert stored != raw
    assert stored.startswith(b"\xff\xd8\xff")


def test_the_stored_extension_always_describes_the_stored_bytes(client, actor, app):
    _, headers = actor
    cases = {
        "jpg": _encode("PNG"),
        "png": _encode("PNG", mode="RGBA"),
        "gif": _animate("GIF"),
        "webp": _animate("WEBP"),
    }

    for expected, blob in cases.items():
        name = _post(client, headers, blob, filename="melagis.tiff").get_json()["filename"]
        assert name.endswith(f".{expected}")
        assert Image.open(_stored_path(app, name)).format == {
            "jpg": "JPEG", "png": "PNG", "gif": "GIF", "webp": "WEBP",
        }[expected]


def test_what_was_stored_is_what_the_public_route_serves_back(client, actor, app):
    _, headers = actor

    body = _post(client, headers, _encode("PNG", size=(40, 40))).get_json()

    served = client.get(body["url"])
    assert served.status_code == 200
    with open(_stored_path(app, body["filename"]), "rb") as handle:
        assert served.data == handle.read()








# -----------------------------------------------------------
# POST /api/uploads — twenty per user per five minutes
# -----------------------------------------------------------

def test_the_twenty_first_upload_in_the_window_is_refused(client, actor, app):
    _, headers = actor
    for _ in range(UPLOAD_RATE_MAX):
        assert _post(client, headers, _encode("PNG")).status_code == 201

    response = _post(client, headers, _encode("PNG"))

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"
    assert response.headers.get("Retry-After")
    assert len(_stored_names(app)) == UPLOAD_RATE_MAX


def test_a_refused_attempt_spends_budget_just_like_a_stored_one(client, actor):
    # The limiter sits outside the handler, so twenty
    # malformed attempts close the window for a good one
    _, headers = actor
    for _ in range(UPLOAD_RATE_MAX):
        assert _post(client, headers, b"", filename="tuscia.png").status_code == 400

    assert _post(client, headers, _encode("PNG")).status_code == 429


def test_the_limiter_runs_before_the_body_is_looked_at(client, actor):
    _, headers = actor
    for _ in range(UPLOAD_RATE_MAX):
        _post(client, headers, _encode("PNG"))

    response = client.post(UPLOADS, data={}, headers=headers, content_type="multipart/form-data")

    assert response.status_code == 429, "no body, and still a rate limit rather than no_file"


def test_one_account_flooding_does_not_close_the_window_for_another(client, actor, make_user, auth_headers):
    _, headers = actor
    other = auth_headers(make_user())
    for _ in range(UPLOAD_RATE_MAX + 1):
        _post(client, headers, _encode("PNG"))

    assert _post(client, other, _encode("PNG")).status_code == 201


def test_the_window_reopens_five_minutes_later(client, actor):
    _, headers = actor

    with time_machine.travel("2026-03-01 10:00:00+00:00", tick=False) as traveller:
        for _ in range(UPLOAD_RATE_MAX):
            _post(client, headers, _encode("PNG"))
        assert _post(client, headers, _encode("PNG")).status_code == 429

        traveller.shift(301)
        assert _post(client, headers, _encode("PNG")).status_code == 201


def test_the_window_is_still_shut_one_second_early(client, actor):
    _, headers = actor

    with time_machine.travel("2026-03-01 10:00:00+00:00", tick=False) as traveller:
        for _ in range(UPLOAD_RATE_MAX):
            _post(client, headers, _encode("PNG"))

        traveller.shift(299)
        assert _post(client, headers, _encode("PNG")).status_code == 429


def test_a_rate_limited_attempt_writes_neither_file_nor_row(client, actor, app, db):
    _, headers = actor
    for _ in range(UPLOAD_RATE_MAX):
        _post(client, headers, _encode("PNG"))

    assert _post(client, headers, _encode("PNG")).status_code == 429
    assert len(_stored_names(app)) == UPLOAD_RATE_MAX
    assert db.execute("SELECT COUNT(*) AS c FROM uploads").fetchone()["c"] == UPLOAD_RATE_MAX


def test_two_sessions_of_one_account_share_a_single_budget(client, actor, auth_headers):
    # The limiter keys on the user id, not on the token, so a
    # second login is not a second allowance
    user, headers = actor
    second = auth_headers(user)
    for _ in range(UPLOAD_RATE_MAX):
        _post(client, headers, _encode("PNG"))

    assert _post(client, second, _encode("PNG")).status_code == 429


def test_an_anonymous_flood_never_spends_a_members_budget(client, actor):
    # require_auth wraps the limiter, so a 401 never records
    _, headers = actor
    for _ in range(40):
        _post(client, {}, _encode("PNG"))

    assert _post(client, headers, _encode("PNG")).status_code == 201








# -----------------------------------------------------------
# POST /api/uploads — the rejection vocabulary
# -----------------------------------------------------------

@pytest.mark.parametrize("blob, filename, status, code", [
    (b"", "tuscia.png", 400, "empty_file"),
    (b"\x00" * (MAX_FILE_SIZE + 1), "didele.png", 400, "file_too_large"),
    (b"ne vaizdas", "dokumentas.pdf", 400, "bad_file_type"),
    (b"ne vaizdas", "nuotrauka.png", 400, "bad_file_content"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 40, "melagis.png", 400, "bad_file_content"),
])
def test_every_rejection_carries_a_machine_readable_slug(client, actor, blob, filename, status, code):
    _, headers = actor

    response = _post(client, headers, blob, filename=filename)

    assert response.status_code == status
    body = response.get_json()
    assert body["code"] == code
    assert body["code"] in INTAKE_CODES
    assert isinstance(body["error"], str) and body["error"]


def test_no_rejection_ever_leaks_the_upload_directory(client, actor, app):
    _, headers = actor

    for blob, filename in ((b"", "t.png"), (b"ne vaizdas", "x.pdf"), (_bomb_png(40000, 40000), "b.png")):
        body = _post(client, headers, blob, filename=filename).get_json()
        assert app.config["UPLOAD_DIR"] not in body["error"]
        assert filename not in body["error"]
