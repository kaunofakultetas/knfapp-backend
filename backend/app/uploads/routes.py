############################################################
#  [*] Uploads — image upload, ownership and public serving
#
#  The one place binary files enter and leave the app: an
#  authenticated multipart POST re-encodes an image into a
#  canonical, metadata-free file stored under a fresh uuid4
#  name, an unauthenticated GET serves it back, and the
#  owner (or an admin) can DELETE it again. The returned url
#  is the RELATIVE "/api/uploads/<name>" and that exact
#  string is what clients persist as avatar_url, image_url
#  and chat imageUrl (create_app's before_request whitelists
#  the prefix for avatar_url); it becomes an absolute URL
#  only at render time, in services/api/client.ts
#  getUploadUrl.
#
#  Files live in UPLOAD_DIR (env var; docker-compose sets
#  /data/uploads = ./_DATA/backend/uploads on the host) and
#  every stored file gets an uploads row (migration v43)
#  naming its owner and byte size: that row is what the
#  per-user quota counts, what the DELETE route authorises
#  against and what sweep_orphan_uploads() ages out.
#  delete_upload() is the shared cleanup helper the other
#  blueprints call when the value they stored goes away — a
#  replaced avatar, an unsent message, a deleted post.
#
#  Validation order: rate limit, 5 MB cap (Werkzeug already
#  refused anything over MAX_CONTENT_LENGTH = 6 MB, and the
#  ingress Caddyfile cuts the body even earlier), magic
#  bytes, then the Pillow re-encode that IS the real gate —
#  it caps pixels, drops EXIF/GPS/XMP, downscales and picks
#  the stored format, so the extension and the served
#  Content-Type describe the bytes rather than the
#  uploader's claim. Every rejection carries a
#  machine-readable "code" beside the human "error".
#
#  Backups: these files are the app's only state outside
#  knfapp.db, so a DB-only snapshot restores rows pointing at
#  images that no longer exist. Tarring /data/uploads next to
#  the DB snapshot is the backup sidecar's job (backup/), and
#  wiring that service in is deploy-local.
#  All three routes are documented in swagger/swagger.yaml.
#
#    POST   /api/uploads            — store an image (auth)
#    GET    /api/uploads/<filename> — serve it (public, 24 h private cache)
#    DELETE /api/uploads/<filename> — drop it (owner or admin)
############################################################


import base64
import io
import logging
import os
import re
import time
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from PIL import Image
from werkzeug.utils import secure_filename

from app.auth.routes import rate_limit, require_auth
from app.database import get_db, utc_now_iso

uploads_bp = Blueprint("uploads", __name__)
logger = logging.getLogger(__name__)

# Only shapes the "use one of these" rejection message and the
# cheap filename pre-filter — the BYTES decide what is stored
# (kept sorted so the message reads the same in every process)
ALLOWED_EXTENSIONS = ("bmp", "gif", "jpeg", "jpg", "png", "tif", "tiff", "webp")

# Documents (form field kind=file): stored as sent, never
# re-encoded, so the bytes must prove their type — PDF and the
# ZIP container behind docx/xlsx/pptx/zip by signature, plain
# text by decoding cleanly. Same size cap as photos
ALLOWED_DOC_EXTENSIONS = ("pdf", "docx", "xlsx", "pptx", "zip", "txt")

# Videos (form field kind=video): stored as sent, proven by
# container signature — the ISO base-media 'ftyp' box for
# mp4/mov/m4v, the EBML header for webm. Their own, larger cap
# (a phone's minute of 1080p is tens of MB); the multipart
# ceiling in create_app and Caddy's /api/uploads body limit
# are sized to it
ALLOWED_VIDEO_EXTENSIONS = ("mp4", "mov", "m4v", "webm")

# Voice notes (form field kind=audio): stored as sent, proven
# by container signature — the ISO 'ftyp' box for m4a, the
# ADTS sync for raw aac, an ID3 tag or MPEG frame sync for mp3
ALLOWED_AUDIO_EXTENSIONS = ("m4a", "aac", "mp3")
VIDEO_MAX_SIZE = 50 * 1024 * 1024        # mirrored by mobile MAX_VIDEO_UPLOAD_BYTES
_VIDEO_MIME = {
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "m4v": "video/x-m4v",
    "webm": "video/webm",
}
_AUDIO_MIME = {
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "mp3": "audio/mpeg",
}
_DOC_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "zip": "application/zip",
    "txt": "text/plain",
}

MAX_FILE_SIZE = 5 * 1024 * 1024          # mirrored by mobile MAX_UPLOAD_BYTES
MAX_IMAGE_PIXELS = 30 * 1000 * 1000      # 30 MP decoded, animation frames counted
MAX_EDGE = 2048                          # longest edge kept after downscaling
JPEG_QUALITY = 85
UPLOAD_QUOTA_BYTES = 100 * 1024 * 1024   # stored bytes one account may hold
UPLOAD_RATE_MAX = 20                     # uploads per user per 5 min window
ORPHAN_GRACE_SECONDS = 24 * 60 * 60      # how long an unreferenced file survives

# Pillow's own decompression-bomb guard, set to the same
# ceiling the route enforces: a hostile header can no longer
# make the decoder allocate gigabytes before we look at it
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

# The signature inventory AND the check itself —
# _validate_magic_bytes walks this dict and answers with the
# matched format, so there is one table, not two lists
_MAGIC_BYTES = {
    b"\xff\xd8\xff": "jpg",        # JPEG
    b"\x89PNG\r\n\x1a\n": "png",   # PNG
    b"GIF87a": "gif",              # GIF87a
    b"GIF89a": "gif",              # GIF89a
    b"RIFF": "webp",               # WebP (RIFF container, bytes 8-12 checked too)
    b"BM": "bmp",                  # BMP
    b"II*\x00": "tiff",            # TIFF, little-endian
    b"MM\x00*": "tiff",            # TIFF, big-endian
}

# Stored names are a uuid4 hex plus a canonical extension, and
# nothing else is ever served or deleted; the files written
# before migration v43 match this shape too
_FILENAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp|pdf|docx|xlsx|pptx|zip|txt|mp4|mov|m4v|webm)$")

# Resolved (and created) once per process by _get_upload_dir
_upload_dir = None








############################################################
# _accept_document
############################################################
#
# (ext, bytes, None) for a document whose bytes match its
# claimed extension, else (None, None, (message, code)). PDF
# opens with %PDF; docx/xlsx/pptx/zip are ZIP containers
# (PK\x03\x04); txt must decode as UTF-8 with no NUL byte in
# its first 4 KB — enough to keep an executable in a .txt
# coat from being stored. Nothing else is accepted.
#
# Used by:
#   - upload_file (below) — the kind=file branch
############################################################

def _accept_document(file_obj):
    name = file_obj.filename or ""
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    if ext not in ALLOWED_DOC_EXTENSIONS:
        return None, None, (
            f"File type not allowed. Use: {', '.join(ALLOWED_DOC_EXTENSIONS)}",
            "bad_file_type",
        )
    file_obj.seek(0)
    blob = file_obj.read()
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
        return None, None, ("File content does not match its extension", "bad_file_content")
    return ext, blob, None







############################################################
# _accept_video
############################################################
#
# (ext, bytes, None) for a video whose bytes match its claimed
# container, else (None, None, (message, code)). mp4/mov/m4v
# carry the ISO base-media 'ftyp' box at offset 4; webm opens
# with the EBML magic. The bytes are stored as sent — there is
# no transcoder in this container — so the signature is the
# whole proof, the same bar documents clear.
#
# Used by:
#   - upload_file (below) — the kind=video branch
############################################################

def _accept_video(file_obj):
    name = file_obj.filename or ""
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return None, None, (
            f"File type not allowed. Use: {', '.join(ALLOWED_VIDEO_EXTENSIONS)}",
            "bad_file_type",
        )
    file_obj.seek(0)
    blob = file_obj.read()
    head = blob[:16]
    if ext == "webm":
        ok = head.startswith(b"\x1a\x45\xdf\xa3")
    else:
        ok = len(head) >= 8 and head[4:8] == b"ftyp"
    if not ok:
        return None, None, ("File content does not match its extension", "bad_file_content")
    return ext, blob, None







############################################################
# _accept_audio
############################################################
#
# (ext, bytes, None) for a voice note whose bytes match the
# container its name claims, else (None, None, (message,
# code)). m4a is ISO base-media like mp4 ('ftyp' at offset
# 4); raw aac opens with the ADTS sync (0xFFF); mp3 with an
# ID3 tag or an MPEG frame sync. Stored as sent — the same
# bar documents and videos clear, no transcoder here.
#
# Used by:
#   - upload_file (below) — the kind=audio branch
############################################################

def _accept_audio(file_obj):
    name = file_obj.filename or ""
    ext = name.rsplit(".", 1)[1].lower() if "." in name else ""
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        return None, None, (
            f"File type not allowed. Use: {', '.join(ALLOWED_AUDIO_EXTENSIONS)}",
            "bad_file_type",
        )
    file_obj.seek(0)
    blob = file_obj.read()
    head = blob[:16]
    if ext == "m4a":
        ok = len(head) >= 8 and head[4:8] == b"ftyp"
    elif ext == "aac":
        ok = len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF0) == 0xF0
    else:
        ok = head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if not ok:
        return None, None, ("File content does not match its extension", "bad_file_content")
    return ext, blob, None







############################################################
# _allowed_file
############################################################
#
# Cheap pre-filter on the CLIENT-SUPPLIED filename, used
# only to phrase the rejection when the bytes already failed
# to look like an image: a name whose extension is
# positively NOT an image gets the "use one of these"
# message. A name with NO extension passes — the Expo web
# picker sends blobs called "blob"/"image", and rejecting
# those was the old gate's real-world failure. The stored
# extension never comes from here any more; the re-encode
# decides it.
#
# Used by:
#   - upload_file (below)
############################################################

def _allowed_file(filename: str) -> bool:
    if "." not in filename:
        return True
    return filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS








############################################################
# _validate_magic_bytes
############################################################
#
# Reads the first 16 bytes of the stream, seeks back to 0,
# and answers with the format those bytes announce — the
# _MAGIC_BYTES key they start with — or None when nothing
# matches. WebP keeps its extra condition (a RIFF container
# whose bytes 8-12 spell WEBP; a bare "RIFF", e.g. a .wav,
# is not a match), and anything shorter than 4 bytes fails.
# The answer is a CHEAP first opinion: the Pillow re-encode
# in _reencode_image is what actually decides the format
# that gets stored.
#
# Used by:
#   - upload_file (below)
############################################################

def _validate_magic_bytes(file_obj):
    header = file_obj.read(16)
    file_obj.seek(0)

    if len(header) < 4:
        return None

    for signature, fmt in _MAGIC_BYTES.items():
        if not header.startswith(signature):
            continue
        # RIFF alone is a container, not a format
        if fmt == "webp" and header[8:12] != b"WEBP":
            continue
        return fmt

    return None








############################################################
# _reencode_image
############################################################
#
# The real gate. Decodes the uploaded bytes with Pillow and
# hands back (extension, canonical bytes, None) or
# (None, None, (message, code)) — a rejection the caller
# turns into a 400. Nothing from the source's metadata is
# passed to save(), so EXIF (GPS included), APP1 and XMP are
# dropped by construction; the pixel budget counts animation
# frames, so a 200-frame GIF cannot smuggle a decompression
# bomb past a single-frame check.
#
# Canonical output:
#   - animations keep every frame at their original size (a
#     per-frame resize desyncs GIF palettes) and stay WebP
#     when they arrived as WebP, because a WebP re-encoded
#     as GIF can grow many times over
#   - anything with transparency becomes PNG
#   - everything else becomes progressive JPEG
# Still images are downscaled to MAX_EDGE first, which is
# what keeps a 30 MP photo from reaching a phone.
#
# Used by:
#   - upload_file (below)
############################################################

def _reencode_image(raw: bytes):
    # STEP 1: decode — verify() consumes the object, so the
    # image we work on is a second, fresh open of the bytes.
    # A bomb far past the ceiling never decodes at all:
    # Image.MAX_IMAGE_PIXELS makes Pillow raise instead
    # ======================================================
    too_many_pixels = (
        f"Image too large. Max {MAX_IMAGE_PIXELS // (1000 * 1000)} megapixels",
        "image_too_large",
    )

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


    # STEP 3: re-encode into the canonical container — only
    # pixels cross over, never the source's metadata
    # =====================================================
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








############################################################
# _get_upload_dir
############################################################
#
# Absolute UPLOAD_DIR, resolved and created ONCE per process
# and cached in _upload_dir — the makedirs used to run on
# every call, so every public image GET paid a syscall for a
# directory that has existed since boot. create_app makes
# the same directory at startup (fail fast on a read-only
# volume); the exist_ok here still lets a bare `flask run`
# and the scheduler's sweep work on an empty /data. Reads
# current_app, so it needs a request or app context.
#
# Used by:
#   - upload_file, serve_file, delete_upload,
#     sweep_orphan_uploads (below)
############################################################

def _get_upload_dir() -> str:
    global _upload_dir

    if _upload_dir is None:
        resolved = os.path.abspath(current_app.config["UPLOAD_DIR"])
        os.makedirs(resolved, exist_ok=True)
        _upload_dir = resolved

    return _upload_dir








############################################################
# upload_file
############################################################
#
# POST /api/uploads
#
# multipart/form-data with one "file" field → 201 {"url":
# "/api/uploads/<uuid4 hex>.<ext>", "filename"} — the shape
# the app has always consumed. Every rejection now carries a
# machine-readable "code" beside the human "error" (the auth
# blueprint's slug pattern), so clients stop parsing prose:
# no_file / file_too_large / empty_file / bad_file_type /
# bad_file_content / image_too_large on a 400,
# quota_exceeded on a 413, rate_limited on a 429 and
# storage_unavailable on a 507.
#
# The client's filename is thrown away entirely — the stored
# extension comes from _reencode_image, so the bytes on disk
# always match the name they are served under, and files are
# never enumerable or overwritable by name (secure_filename
# on a uuid hex is a no-op kept as belt and braces). The
# save is atomic: bytes go to <name>.part, get fsynced and
# are os.replace-d into place, so a full disk can never
# leave a truncated file being served as a valid image.
#
# Twenty uploads per user per 5 minutes and 100 MB of stored
# files per account (counted from the uploads rows, which
# this route writes) bound what one account can do to the
# volume that also holds knfapp.db.
#
# Used by:
#   - services/api/uploads.ts uploadImageApi, called from
#     app/(main)/tabs/id.tsx and
#     app/(main)/profile/index.tsx (avatar),
#     app/(main)/create-post/index.tsx (post image) and
#     hooks/chat/useChatComposer.ts (chat image)
############################################################

@uploads_bp.route("", methods=["POST"])
@require_auth
@rate_limit("upload", max_attempts=UPLOAD_RATE_MAX)
def upload_file():
    # STEP 1: pick the file out of the multipart body
    # ===============================================
    if "file" not in request.files:
        return jsonify({"error": "No file provided", "code": "no_file"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected", "code": "no_file"}), 400


    # STEP 2: size gates — the stream is already spooled, so
    # seek-to-end is the exact byte count (Werkzeug refused
    # anything over MAX_CONTENT_LENGTH long before this)
    # ======================================================
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)

    # The kind decides the cap: a video may be ten times a photo
    upload_kind = request.form.get("kind") or request.args.get("kind") or "image"
    size_cap = VIDEO_MAX_SIZE if upload_kind == "video" else MAX_FILE_SIZE
    if size > size_cap:
        return jsonify({
            "error": f"File too large. Max {size_cap // (1024 * 1024)} MB",
            "code": "file_too_large",
        }), 400

    if size == 0:
        return jsonify({"error": "Empty file", "code": "empty_file"}), 400


    # STEP 3: a DOCUMENT (kind=file) or a VIDEO (kind=video) is
    # stored as sent once its bytes prove the type it claims; a
    # photo goes through the signature check and the re-encode
    # ========================================================
    width = height = None
    preview = None
    if upload_kind == "file":
        ext, blob, rejection = _accept_document(file)
        if rejection:
            message, code = rejection
            return jsonify({"error": message, "code": code}), 400
    elif upload_kind == "video":
        ext, blob, rejection = _accept_video(file)
        if rejection:
            message, code = rejection
            return jsonify({"error": message, "code": code}), 400
    elif upload_kind == "audio":
        ext, blob, rejection = _accept_audio(file)
        if rejection:
            message, code = rejection
            return jsonify({"error": message, "code": code}), 400
    else:
        # The bytes decide — the signature is the first opinion,
        # the client's filename only shapes the message when that
        # opinion is "not an image at all"
        if not _validate_magic_bytes(file):
            if not _allowed_file(file.filename):
                return jsonify({
                    "error": f"File type not allowed. Use: {', '.join(ALLOWED_EXTENSIONS)}",
                    "code": "bad_file_type",
                }), 400
            return jsonify({
                "error": "File content does not match an allowed image format",
                "code": "bad_file_content",
            }), 400


        # STEP 4: re-encode — strips metadata, caps pixels,
        # downscales and picks the canonical stored format
        # =================================================
        ext, blob, rejection = _reencode_image(file.read())
        if rejection:
            message, code = rejection
            return jsonify({"error": message, "code": code}), 400
        # The stored pixel size, read back off the header — the
        # client lays a photo bubble out at its final proportions
        # before the bytes arrive — and a ~14px micro copy as a
        # data URI, the blurry placeholder shown while the real
        # bytes download (bilinear upscale does the blurring)
        try:
            with Image.open(io.BytesIO(blob)) as stored:
                width, height = stored.size
                tiny = stored.convert("RGB")
                tiny.thumbnail((14, 14))
                tiny_buf = io.BytesIO()
                tiny.save(tiny_buf, format="JPEG", quality=60)
                preview = "data:image/jpeg;base64," + base64.b64encode(tiny_buf.getvalue()).decode("ascii")
        except Exception:
            width = height = None
            preview = None


    # STEP 5: the per-account storage quota, counted from
    # this user's own uploads rows
    # ===================================================
    user_id = request.user["id"]
    db = get_db()
    try:
        used = db.execute(
            "SELECT COALESCE(SUM(byte_size), 0) AS total FROM uploads WHERE user_id = ?",
            (user_id,),
        ).fetchone()["total"]

        if used + len(blob) > UPLOAD_QUOTA_BYTES:
            logger.warning("Upload quota reached by user %s (%d bytes stored)", user_id, used)
            return jsonify({
                "error": f"Storage quota reached. Max {UPLOAD_QUOTA_BYTES // (1024 * 1024)} MB per account",
                "code": "quota_exceeded",
            }), 413


        # STEP 6: write atomically — .part, fsync, rename — so
        # a full disk leaves nothing half-written behind
        # ====================================================
        safe_name = secure_filename(f"{uuid.uuid4().hex}.{ext}")
        final_path = os.path.join(_get_upload_dir(), safe_name)
        part_path = f"{final_path}.part"

        try:
            with open(part_path, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(part_path, final_path)
        except OSError:
            logger.error("Upload write failed for %s", safe_name, exc_info=True)
            try:
                os.unlink(part_path)
            except OSError:
                pass
            return jsonify({
                "error": "Storage is unavailable, try again later",
                "code": "storage_unavailable",
            }), 507


        # STEP 7: record the owner, then answer with the
        # RELATIVE url the client persists
        # ==============================================
        db.execute(
            "INSERT INTO uploads (id, filename, user_id, byte_size, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), safe_name, user_id, len(blob), utc_now_iso()),
        )
        db.commit()
    finally:
        db.close()

    return jsonify({
        "url": f"/api/uploads/{safe_name}",
        "filename": safe_name,
        # Additive: what a document message carries as its
        # attachment — the name the sender chose, the byte size,
        # the canonical mime for the stored extension
        "name": secure_filename(file.filename) or safe_name,
        "size": len(blob),
        "mime": _DOC_MIME.get(ext) or _VIDEO_MIME.get(ext) or _AUDIO_MIME.get(ext) or f"image/{'jpeg' if ext == 'jpg' else ext}",
        # Photos only — None for documents, videos and audio
        "width": width,
        "height": height,
        "preview": preview,
    }), 201








############################################################
# delete_upload
############################################################
#
# The shared cleanup helper: takes the value the OTHER
# blueprints stored — "/api/uploads/<name>", or the bare
# name — and removes both the file and its uploads row.
# True when a file was actually unlinked, False when the
# value is not a stored upload (a scraped http(s) image
# url, a foreign path) or the file was already gone. Never
# raises on a filesystem error: the caller is always in the
# middle of a delete/replace that must succeed regardless,
# and a file left behind is the orphan sweep's problem.
#
# _FILENAME_RE is the gate, so no caller can walk out of
# UPLOAD_DIR with a crafted stored value.
#
# Used by:
#   - delete_file, sweep_orphan_uploads (below)
#   - auth/routes.py — _delete_replaced_upload (the avatar
#     a profile update replaced)
#   - chat/routes.py — delete_message (an unsent message's
#     photo)
#   - news/routes.py, social/routes.py — post deletion, once
#     those packages call it
############################################################

def delete_upload(path) -> bool:
    # STEP 1: reduce whatever was stored to a name we own
    # ===================================================
    if not isinstance(path, str):
        return False

    safe_name = secure_filename(path.rsplit("/", 1)[-1])
    if not safe_name or not _FILENAME_RE.match(safe_name):
        return False


    # STEP 2: the file, best-effort — an already-missing one
    # still clears its row
    # ======================================================
    removed = False
    try:
        os.unlink(os.path.join(_get_upload_dir(), safe_name))
        removed = True
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not unlink upload %s", safe_name, exc_info=True)


    # STEP 3: the ownership row, so the quota frees up too
    # ====================================================
    db = get_db()
    try:
        db.execute("DELETE FROM uploads WHERE filename = ?", (safe_name,))
        db.commit()
    finally:
        db.close()

    return removed








############################################################
# delete_file
############################################################
#
# DELETE /api/uploads/<filename>
#
# Additive route (no client calls it yet): the uploader —
# or an admin — drops a stored file, answering the house
# {"ok": True}. Authorisation is the uploads row migration
# v43 added, so the files written before it have no owner
# and only an admin can reach them; an unknown name and a
# someone-else's pre-v43 file look the same from outside
# (404), which keeps the route from confirming what exists.
#
# An already-missing file is still a 200 — the row goes and
# the caller got what it asked for — but a file that SURVIVES
# the unlink (a read-only volume, a directory wearing a
# stored name) answers 500 "delete_failed" rather than lying:
# this is the erasure and moderation path, and the bytes stay
# readable through the public GET until the sweep collects
# them. The ownership row is cleared either way, which is
# what turns the survivor into an orphan the sweep can reach.
# Deleting a file still referenced by a post, message or
# avatar is allowed on purpose — that is the moderation and
# erasure path — and leaves the reference to render as a
# broken image, exactly as a swept orphan would.
#
# Used by:
#   - nothing in mobile yet; admin/moderation tooling and
#     manual erasure requests
############################################################

@uploads_bp.route("/<filename>", methods=["DELETE"])
@require_auth
def delete_file(filename):
    # STEP 1: the name must be one we could have written
    # ==================================================
    safe_name = secure_filename(filename)
    if not safe_name or not _FILENAME_RE.match(safe_name):
        return jsonify({"error": "Invalid filename", "code": "bad_filename"}), 400


    # STEP 2: owner or admin only
    # ===========================
    db = get_db()
    try:
        row = db.execute(
            "SELECT user_id FROM uploads WHERE filename = ?",
            (safe_name,),
        ).fetchone()
    finally:
        db.close()

    is_admin = request.user.get("role") == "admin"

    if row is None and not is_admin:
        return jsonify({"error": "File not found"}), 404

    if row is not None and not is_admin and row["user_id"] != request.user["id"]:
        return jsonify({"error": "Only the owner can delete this file"}), 403


    # STEP 3: file plus row, through the shared helper — which
    # swallows every filesystem error and clears the row
    # regardless, so the disk is what says whether this worked
    # ========================================================
    delete_upload(safe_name)

    try:
        survived = os.path.lexists(os.path.join(_get_upload_dir(), safe_name))
    except OSError:
        # A broken mount: _get_upload_dir cannot even resolve
        # the directory, so nothing was removed
        survived = True

    if survived:
        logger.error("Delete left %s on disk; the sweep will have to collect it", safe_name)
        return jsonify({
            "error": "The file could not be removed, try again later",
            "code": "delete_failed",
        }), 500

    return jsonify({"ok": True})








############################################################
# sweep_orphan_uploads
############################################################
#
# Deletes every file in UPLOAD_DIR that no row references —
# users.avatar_url, news_posts.image_url (news AND social
# posts live in that one table) and messages.image_url are
# the only places an upload url is ever stored — and that is
# older than ORPHAN_GRACE_SECONDS. The grace period is what
# makes the sweep safe against a race: a file uploaded
# seconds ago has not been sent to the create-post or
# send-message call yet, so it is unreferenced and NOT an
# orphan. Leftover <name>.part files from an interrupted
# write are collected by the same pass.
#
# The pass then clears the MIRROR image: uploads rows whose
# file is no longer on disk (a DB-only backup restored over
# an empty volume, a manual rm, an unlink delete_file could
# not do). The directory walk cannot see those by
# definition, and their byte_size would otherwise count
# against the owner's 100 MB quota for ever. A name this
# pass SAW on disk is spared even if it has since vanished,
# so a delete racing the sweep leaves the row to whoever
# won it.
#
# Returns the number of FILES removed — the row pass is
# reported in the log only; needs an app context for
# UPLOAD_DIR.
#
# Used by:
#   - not scheduled yet — app/__init__.py owns the
#     APScheduler wiring (a daily job calling this inside
#     app.app_context()); until then it is a manual /
#     REPL-invoked sweep
############################################################

def sweep_orphan_uploads() -> int:
    # STEP 1: every upload url a row still points at, reduced
    # to bare filenames
    # =======================================================
    upload_dir = _get_upload_dir()
    cutoff = time.time() - ORPHAN_GRACE_SECONDS
    removed = 0
    forgotten = []

    db = get_db()
    try:
        referenced = set()
        for sql in (
            "SELECT avatar_url AS url FROM users WHERE avatar_url IS NOT NULL",
            "SELECT image_url AS url FROM news_posts WHERE image_url IS NOT NULL",
            "SELECT image_url AS url FROM messages WHERE image_url IS NOT NULL",
        ):
            for row in db.execute(sql):
                referenced.add(str(row["url"]).rsplit("/", 1)[-1])


        # STEP 2: walk the directory once (snapshotted, since we
        # unlink while iterating) and drop what nothing claims
        # ======================================================
        entries = list(os.scandir(upload_dir))
        seen = {entry.name for entry in entries}

        for entry in entries:
            if not entry.is_file() or entry.name in referenced:
                continue

            try:
                if entry.stat().st_mtime > cutoff:
                    continue
                os.unlink(entry.path)
            except OSError:
                logger.warning("Orphan sweep could not remove %s", entry.name, exc_info=True)
                continue

            db.execute("DELETE FROM uploads WHERE filename = ?", (entry.name,))
            removed += 1


        # STEP 3: the other half of the litter — rows the walk
        # can never see, because their file is already gone.
        # lexists as well as the snapshot: a row written AFTER
        # the scandir belongs to a file that is on disk right
        # now, and losing it would cost that upload its owner
        # ====================================================
        forgotten = [
            row["filename"]
            for row in db.execute("SELECT filename FROM uploads").fetchall()
            if row["filename"] not in seen
            and not os.path.lexists(os.path.join(upload_dir, row["filename"]))
        ]

        for name in forgotten:
            db.execute("DELETE FROM uploads WHERE filename = ?", (name,))

        db.commit()
    finally:
        db.close()

    if removed:
        logger.info("Swept %d orphaned upload file(s)", removed)

    if forgotten:
        logger.info("Cleared %d uploads row(s) whose file was already gone", len(forgotten))

    return removed








############################################################
# serve_file
############################################################
#
# GET /api/uploads/<filename>
#
# Public on purpose — avatars and post/chat images are
# rendered for anonymous viewers too. Five layers keep it
# inside UPLOAD_DIR: the route converter refuses slashes,
# _FILENAME_RE admits ONLY the uuid-hex names this module
# writes (so the served path is a pure function of the
# request, and anything else is a 400 rather than a
# filesystem probe), secure_filename strips anything
# path-like as belt and braces, send_from_directory
# safe-joins once more, and a realpath check makes sure the
# bytes actually live where the name says they do. That last
# layer is the only one that looks past the NAME: the other
# four all follow a symlink planted in the volume, which
# would turn this unauthenticated route into an arbitrary
# file read (knfapp.db included). A name that resolves
# elsewhere is a 404, exactly like a missing file. The
# explicit isfile check answers {"error": "File not found"}
# rather than falling through to the app-wide 404 handler's
# {"error": "Not found"}.
#
# max_age=86400 gives 24 h of caching plus ETag /
# conditional 304s, but the header is flipped to
# Cache-Control: private: chat photos are shared through
# this same public route, and a shared proxy cache has no
# business holding them. Browsers still cache. Content-Type
# is guessed from the stored extension, which since the
# re-encode landed IS the format of the bytes.
#
# Used by:
#   - services/api/client.ts getUploadUrl builds these URLs;
#     rendered by components/ui/Avatar.tsx,
#     components/news/NewsCard.tsx,
#     app/(main)/news-post/index.tsx,
#     chatkit/MessageBubble.tsx,
#     components/chat/ConversationRow.tsx and
#     app/(main)/chat-room/index.tsx
############################################################

@uploads_bp.route("/<filename>", methods=["GET"])
def serve_file(filename):
    # STEP 1: only names this module could have written
    # =================================================
    safe_name = secure_filename(filename)
    if not safe_name or not _FILENAME_RE.match(safe_name):
        return jsonify({"error": "Invalid filename", "code": "bad_filename"}), 400

    upload_dir = _get_upload_dir()
    file_path = os.path.join(upload_dir, safe_name)

    if not os.path.isfile(file_path):
        return jsonify({"error": "File not found"}), 404


    # STEP 2: containment — every check so far judged the NAME,
    # and a symlink sitting in the upload volume resolves
    # wherever it likes. The stored files are flat, so the only
    # honest answer is <real UPLOAD_DIR>/<name>; anything else
    # is treated as absent rather than confirmed
    # =========================================================
    if os.path.realpath(file_path) != os.path.join(os.path.realpath(upload_dir), safe_name):
        logger.warning("Refused %s: it resolves outside the upload directory", safe_name)
        return jsonify({"error": "File not found"}), 404


    # STEP 3: 24 h in the BROWSER's cache, never in a shared
    # one — private chat photos come through this route too
    # ======================================================
    rv = send_from_directory(upload_dir, safe_name, max_age=86400)
    rv.cache_control.public = False
    rv.cache_control.private = True

    return rv
