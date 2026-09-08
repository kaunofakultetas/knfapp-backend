############################################################
#  [*] Upload gates — the bytes decide, never the name
#
#  Every acceptance rule of the upload route as pure
#  functions over (filename, blob) — no request objects, no
#  disk, no database — so each rule pins down to a byte
#  table in the tests. The rule of every gate: the client's
#  filename only ever shapes an error message or a cheap
#  pre-filter; what gets STORED is what
#  the bytes prove (signatures for documents/videos/audio,
#  the Pillow re-encode for images).
#
#  Split into:
#
#    accept_document / accept_video / accept_audio
#    allowed_image_name    — the message-shaping pre-filter
#    sniff_image_format    — magic-byte first opinion
#    reencode_image        — the real image gate
############################################################


import io
import logging


from PIL import Image


logger = logging.getLogger(__name__)

# Only shapes the "use one of these" rejection message and the
# cheap filename pre-filter — the BYTES decide what is stored
ALLOWED_EXTENSIONS = ("bmp", "gif", "jpeg", "jpg", "png", "tif", "tiff", "webp")
ALLOWED_DOC_EXTENSIONS = ("pdf", "docx", "xlsx", "pptx", "zip", "txt")
ALLOWED_VIDEO_EXTENSIONS = ("mp4", "mov", "m4v", "webm")
ALLOWED_AUDIO_EXTENSIONS = ("m4a", "aac", "mp3")

MAX_FILE_SIZE = 5 * 1024 * 1024          # mirrored by mobile MAX_UPLOAD_BYTES
VIDEO_MAX_SIZE = 50 * 1024 * 1024        # mirrored by mobile MAX_VIDEO_UPLOAD_BYTES
MAX_IMAGE_PIXELS = 30 * 1000 * 1000      # 30 MP decoded, animation frames counted
MAX_EDGE = 2048                          # longest edge kept after downscaling
JPEG_QUALITY = 85

# Pillow's own decompression-bomb guard, set to the same ceiling
# the gate enforces — a hostile header cannot make the decoder
# allocate gigabytes before we look at it
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

MIME_BY_EXT = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "txt": "text/plain",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "m4v": "video/x-m4v",
    "webm": "video/webm",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "mp3": "audio/mpeg",
}

# The signature inventory AND the check itself — sniff walks this
# dict and answers with the matched format: one table, not two
_MAGIC_BYTES = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"RIFF": "webp",   # RIFF container — bytes 8-12 must spell WEBP too
    b"BM": "bmp",
    b"II*\x00": "tiff",
    b"MM\x00*": "tiff",
}


def _ext_of(filename):
    name = filename or ""
    return name.rsplit(".", 1)[1].lower() if "." in name else ""








############################################################
# accept_document / accept_video / accept_audio
############################################################
#
# (ext, None) when the blob matches the extension the name
# claims, else (None, (message, code)). Stored as sent —
# no transcoder here — so the signature is the whole proof:
# %PDF, the PK ZIP container behind docx/xlsx/pptx/zip,
# clean NUL-free UTF-8 for txt; the ISO 'ftyp' box for
# mp4/mov/m4v/m4a, EBML for webm, ADTS sync for aac, ID3 or
# frame sync for mp3.
#
# Used by:
#   - api/views.py upload_file — kind=file/video/audio
############################################################

def accept_document(filename, blob):
    ext = _ext_of(filename)
    if ext not in ALLOWED_DOC_EXTENSIONS:
        return None, (f"File type not allowed. Use: {', '.join(ALLOWED_DOC_EXTENSIONS)}", "bad_file_type")
    head = blob[:4096]
    if ext == "pdf":
        ok = head.startswith(b"%PDF")
    elif ext in ("docx", "xlsx", "pptx", "zip"):
        ok = head.startswith(b"PK\x03\x04")
    else:
        try:
            head.decode("utf-8")
            ok = b"\x00" not in head
        except UnicodeDecodeError:
            ok = False
    if not ok:
        return None, ("File content does not match its extension", "bad_file_content")
    return ext, None


def accept_video(filename, blob):
    ext = _ext_of(filename)
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return None, (f"File type not allowed. Use: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}", "bad_file_type")
    head = blob[:16]
    if ext == "webm":
        ok = head.startswith(b"\x1a\x45\xdf\xa3")
    else:
        ok = len(head) >= 8 and head[4:8] == b"ftyp"
    if not ok:
        return None, ("File content does not match its extension", "bad_file_content")
    return ext, None


def accept_audio(filename, blob):
    ext = _ext_of(filename)
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        return None, (f"File type not allowed. Use: {', '.join(ALLOWED_AUDIO_EXTENSIONS)}", "bad_file_type")
    head = blob[:16]
    if ext == "m4a":
        ok = len(head) >= 8 and head[4:8] == b"ftyp"
    elif ext == "aac":
        ok = len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF0) == 0xF0
    else:
        ok = head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if not ok:
        return None, ("File content does not match its extension", "bad_file_content")
    return ext, None








############################################################
# allowed_image_name / sniff_image_format
############################################################
#
# The name pre-filter exists only to phrase the rejection
# when the bytes already failed to look like an image; a
# name with NO extension passes (web pickers send blobs
# called "blob"/"image" — rejecting those was the old
# gate's real-world failure). sniff reads the first bytes
# and answers the format they announce, or None; WebP keeps
# its RIFF+WEBP double check. A CHEAP first opinion — the
# re-encode decides what is stored.
#
# Used by:
#   - api/views.py upload_file — the image branch
############################################################

def allowed_image_name(filename):
    if "." not in (filename or ""):
        return True
    return _ext_of(filename) in ALLOWED_EXTENSIONS


def sniff_image_format(blob):
    header = blob[:16]
    if len(header) < 4:
        return None
    for signature, fmt in _MAGIC_BYTES.items():
        if not header.startswith(signature):
            continue
        if fmt == "webp" and header[8:12] != b"WEBP":
            continue
        return fmt
    return None








############################################################
# reencode_image
############################################################
#
# The real gate. Decodes with Pillow and answers
# (ext, canonical bytes, None) or (None, None, (message,
# code)). Nothing from the source's metadata reaches
# save(), so EXIF (GPS included), APP1 and XMP are dropped
# by construction; the pixel budget counts animation
# frames, so a 200-frame GIF cannot smuggle a bomb past a
# single-frame check.
#
# Canonical output: animations keep their frames and stay
# WebP when they arrived as WebP (GIF re-encode can grow
# many times over), transparency becomes PNG, everything
# else progressive JPEG; still images are downscaled to
# MAX_EDGE first — what keeps a 30 MP photo off a phone.
#
# Used by:
#   - api/views.py upload_file — the image branch
############################################################

def reencode_image(raw):
    too_many_pixels = (f"Image too large. Max {MAX_IMAGE_PIXELS // (1000 * 1000)} megapixels", "image_too_large")

    # STEP 1: decode — verify() consumes its object, so the image
    # we work on is a second, fresh open; a bomb far past the
    # ceiling never decodes at all (Image.MAX_IMAGE_PIXELS raises)
    # ===========================================================
    try:
        Image.open(io.BytesIO(raw)).verify()
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Image.DecompressionBombError:
        logger.info("Upload rejected: decompression bomb")
        return None, None, too_many_pixels
    except Exception:
        logger.info("Upload rejected: bytes do not decode as an image")
        return None, None, ("File content could not be read as an image", "bad_file_content")

    # STEP 2: pixel budget, frames included
    # =====================================
    frames = getattr(img, "n_frames", 1)
    if img.width * img.height * frames > MAX_IMAGE_PIXELS:
        img.close()
        return None, None, too_many_pixels

    # STEP 3: re-encode into the canonical container — only pixels
    # cross over, never the source's metadata
    # ============================================================
    buffer = io.BytesIO()
    try:
        if frames > 1 and img.format == "WEBP":
            img.save(buffer, format="WEBP", save_all=True)
            ext = "webp"
        elif frames > 1:
            img.save(buffer, format="GIF", save_all=True, optimize=True)
            ext = "gif"
        elif img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            flat = img.convert("RGBA")
            flat.thumbnail((MAX_EDGE, MAX_EDGE))
            flat.save(buffer, format="PNG", optimize=True)
            ext = "png"
        else:
            flat = img.convert("RGB")
            flat.thumbnail((MAX_EDGE, MAX_EDGE))
            flat.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            ext = "jpg"
    except Exception:
        logger.warning("Upload rejected: re-encode failed", exc_info=True)
        return None, None, ("File content could not be read as an image", "bad_file_content")
    finally:
        img.close()

    return ext, buffer.getvalue(), None
