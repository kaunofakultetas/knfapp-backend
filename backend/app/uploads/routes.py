############################################################
#  [*] Uploads — image upload and public serving
#
#  The one place binary files enter and leave the app: an
#  authenticated multipart POST stores an image under a
#  fresh uuid4 name, an unauthenticated GET serves it back.
#  The returned url is the RELATIVE "/api/uploads/<name>"
#  and that exact string is what clients persist as
#  avatar_url, image_url and chat imageUrl (create_app's
#  before_request whitelists the prefix for avatar_url);
#  it becomes an absolute URL only at render time, in
#  services/api/client.ts getUploadUrl.
#
#  Files live in UPLOAD_DIR (env var; docker-compose sets
#  /data/uploads = ./_DATA/backend/uploads on the host).
#  knfapp-backup snapshots only knfapp.db, so uploads are
#  NOT backed up — and nothing ever deletes them: a
#  replaced avatar or a deleted post/message leaves its
#  file behind for good.
#
#  Validation is extension whitelist + 5 MB cap + magic
#  bytes, all applied AFTER Flask has buffered the whole
#  body: MAX_CONTENT_LENGTH is not configured and Caddy sets
#  no request_body limit, so an oversized upload is spooled
#  in full (the container's /tmp tmpfs) before the 400.
#  Both routes are documented in swagger/swagger.yaml.
#
#    POST /api/uploads            — store an image (auth)
#    GET  /api/uploads/<filename> — serve it (public, 24 h cache)
############################################################


import os
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from app.auth.routes import require_auth

uploads_bp = Blueprint("uploads", __name__)

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

# Dead code: nothing reads this dict — _validate_magic_bytes
# hard-codes the same signatures. It survives as the readable
# inventory of what the content check accepts.
_MAGIC_BYTES = {
    b"\xff\xd8\xff": "jpg",       # JPEG
    b"\x89PNG\r\n\x1a\n": "png",  # PNG
    b"GIF87a": "gif",              # GIF87a
    b"GIF89a": "gif",              # GIF89a
    b"RIFF": "webp",               # WebP (RIFF container, further checked below)
}








############################################################
# _allowed_file
############################################################
#
# Extension whitelist on the CLIENT-SUPPLIED filename (the
# text after the last dot, lowercased). It only decides
# whether the request is looked at further — the content
# check is _validate_magic_bytes — but the same extension
# is what the stored file gets, so a PNG uploaded as
# "x.jpg" is stored and later served as .jpg.
#
# Used by:
#   - upload_file (below)
############################################################

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS








############################################################
# _validate_magic_bytes
############################################################
#
# Reads the first 16 bytes of the stream, seeks back to 0,
# and answers whether they open like ANY allowed format:
# JPEG (FF D8 FF), PNG (8-byte signature), GIF87a/GIF89a, or
# WebP (a RIFF container whose bytes 8–12 spell WEBP — bare
# "RIFF", e.g. a .wav, is rejected). Anything shorter than
# 4 bytes fails. It does NOT check that the format matches
# the extension _allowed_file accepted.
#
# Used by:
#   - upload_file (below)
############################################################

def _validate_magic_bytes(file_obj) -> bool:
    header = file_obj.read(16)
    file_obj.seek(0)

    if len(header) < 4:
        return False

    # JPEG: starts with FF D8 FF
    if header[:3] == b"\xff\xd8\xff":
        return True
    # PNG: 8-byte signature
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    # GIF: GIF87a or GIF89a
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return True
    # WebP: RIFF....WEBP
    if header[:4] == b"RIFF" and len(header) >= 12 and header[8:12] == b"WEBP":
        return True

    return False








############################################################
# _get_upload_dir
############################################################
#
# Absolute UPLOAD_DIR, created on demand (exist_ok) on EVERY
# call — each request pays one makedirs, which is also what
# lets a fresh container start on an empty /data volume.
# Reads current_app, so it needs a request or app context.
#
# Used by:
#   - upload_file, serve_file (below)
############################################################

def _get_upload_dir() -> str:
    upload_dir = os.path.abspath(current_app.config["UPLOAD_DIR"])
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir








############################################################
# upload_file
############################################################
#
# POST /api/uploads
#
# multipart/form-data with one "file" field → 201 {"url":
# "/api/uploads/<uuid4 hex>.<ext>", "filename"}. Every
# rejection is a 400 {"error"}, checked in this order: no
# field, empty filename, extension not allowed (the message
# lists ALLOWED_EXTENSIONS in set order, which varies per
# process), over 5 MB, zero bytes, magic bytes not an
# allowed image. The original filename is thrown away —
# only its extension survives — so stored files are never
# enumerable or overwritable by name, and secure_filename
# on a uuid hex is a no-op kept as belt and braces. The
# size comes from seeking the spooled stream, i.e. after
# the body has already been read in full (see the header).
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
def upload_file():
    # STEP 1: pick the file out of the multipart body
    # ===============================================
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400


    # STEP 2: cheap gates first — extension, then size (the
    # stream is already spooled, so seek-to-end is the exact
    # byte count)
    # ======================================================
    if not _allowed_file(file.filename):
        return jsonify({"error": f"File type not allowed. Use: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)

    if size > MAX_FILE_SIZE:
        return jsonify({"error": f"File too large. Max {MAX_FILE_SIZE // (1024*1024)} MB"}), 400

    if size == 0:
        return jsonify({"error": "Empty file"}), 400


    # STEP 3: content gate — the bytes must open like an
    # allowed image (not necessarily the one the extension
    # claims)
    # ====================================================
    if not _validate_magic_bytes(file):
        return jsonify({"error": "File content does not match an allowed image format"}), 400


    # STEP 4: store under a fresh uuid name and answer with
    # the RELATIVE url the client persists
    # =====================================================
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    safe_name = secure_filename(filename)

    upload_dir = _get_upload_dir()
    file.save(os.path.join(upload_dir, safe_name))

    url = f"/api/uploads/{safe_name}"

    return jsonify({"url": url, "filename": safe_name}), 201








############################################################
# serve_file
############################################################
#
# GET /api/uploads/<filename>
#
# Public on purpose — avatars and post/chat images are
# rendered for anonymous viewers too. Three layers keep it
# inside UPLOAD_DIR: the route converter refuses slashes,
# secure_filename strips anything path-like (and may
# quietly rewrite a non-ASCII name into a different one,
# which then 404s), and send_from_directory safe-joins once
# more. The explicit isfile check answers {"error": "File
# not found"} rather than falling through to the app-wide
# 404 handler's {"error": "Not found"}. max_age=86400 gives
# Cache-Control: public, max-age=86400 (24 h) plus ETag /
# conditional 304s; Content-Type is guessed from the stored
# extension — the uploader's claim, not the bytes' (see
# _allowed_file).
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
    safe_name = secure_filename(filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    upload_dir = _get_upload_dir()
    file_path = os.path.join(upload_dir, safe_name)

    if not os.path.isfile(file_path):
        return jsonify({"error": "File not found"}), 404

    return send_from_directory(upload_dir, safe_name, max_age=86400)
