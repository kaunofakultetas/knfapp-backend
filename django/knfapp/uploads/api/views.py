############################################################
#  [*] Uploads API — store, serve, delete
#
#  One POST that accepts a photo (re-encoded), a document,
#  a video or a voice note; a public GET serving the flat
#  directory with browser-only caching; an owner-or-admin
#  DELETE. Every rejection carries the machine `code`
#  beside the human `error`, exactly the slugs the mobile
#  app translates.
#
#  Split into:
#
#    upload_file — POST   /api/uploads
#    delete_file — DELETE /api/uploads/<filename>
#    serve_file  — GET    /api/uploads/<filename>
############################################################


import base64
import io
import logging
import mimetypes
import os
import uuid


from PIL import Image
from django.db import models
from django.http import FileResponse
from django.views.decorators.http import require_POST


from knfapp.common import ratelimit
from knfapp.common.http import json_error, json_response
from knfapp.common.timestamps import utc_now
from knfapp.uploads import gates
from knfapp.uploads.models import Upload
from knfapp.uploads.storage import (
    UPLOAD_QUOTA_BYTES,
    UPLOAD_RATE_MAX,
    atomic_write,
    delete_upload,
    safe_upload_name,
    upload_dir,
)
from knfapp.users.auth import require_auth


logger = logging.getLogger(__name__)








############################################################
# upload_file
############################################################
#
# POST /api/uploads — multipart/form-data with one "file"
# field (+ optional kind=file/video/audio; default image) →
# 201 {"url": "/api/uploads/<uuid4 hex>.<ext>", "filename",
# "name", "size", "mime", "width", "height", "preview"}.
# The client's filename is thrown away entirely — the
# stored extension comes from the byte gates, so the bytes
# on disk always match the name they are served under, and
# names are never enumerable. Photos additionally answer
# their stored pixel size and a ~14px data-URI micro copy
# (the blurry placeholder the chat bubble shows while the
# real bytes download).
#
# Twenty uploads per user per 5 minutes and 100 MB stored
# per account bound what one account can do to the volume.
#
# Used by:
#   - services/api/uploads.ts uploadImageApi (avatar, post
#     image, chat photo/video/voice/file)
############################################################

@require_POST
@require_auth
@ratelimit.per_user("upload", max_attempts=UPLOAD_RATE_MAX)
def upload_file(request):
    # STEP 1: the file out of the multipart body
    # ==========================================
    file = request.FILES.get("file")
    if file is None or not file.name:
        return json_error("No file provided", 400, code="no_file")

    upload_kind = request.POST.get("kind") or request.GET.get("kind") or "image"
    size_cap = gates.VIDEO_MAX_SIZE if upload_kind == "video" else gates.MAX_FILE_SIZE
    if file.size > size_cap:
        return json_error(f"File too large. Max {size_cap // (1024 * 1024)} MB", 400, code="file_too_large")
    if file.size == 0:
        return json_error("Empty file", 400, code="empty_file")

    blob = file.read()


    # STEP 2: the kind picks its gate — documents/videos/audio are
    # stored as sent once their bytes prove the claimed type; a
    # photo goes through the signature sniff and the re-encode
    # ============================================================
    width = height = None
    preview = None
    if upload_kind == "file":
        ext, rejection = gates.accept_document(file.name, blob)
        if rejection:
            return json_error(rejection[0], 400, code=rejection[1])
    elif upload_kind == "video":
        ext, rejection = gates.accept_video(file.name, blob)
        if rejection:
            return json_error(rejection[0], 400, code=rejection[1])
    elif upload_kind == "audio":
        ext, rejection = gates.accept_audio(file.name, blob)
        if rejection:
            return json_error(rejection[0], 400, code=rejection[1])
    else:
        if not gates.sniff_image_format(blob):
            if not gates.allowed_image_name(file.name):
                return json_error(f"File type not allowed. Use: {', '.join(gates.ALLOWED_EXTENSIONS)}",
                                  400, code="bad_file_type")
            return json_error("File content does not match an allowed image format", 400, code="bad_file_content")

        ext, blob, rejection = gates.reencode_image(blob)
        if rejection:
            return json_error(rejection[0], 400, code=rejection[1])

        # The stored pixel size plus the micro copy — the client lays
        # a photo bubble out at its final proportions before the bytes
        # arrive, and upscales the blur while they download
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


    # STEP 3: the per-account storage quota, counted from the rows
    # ============================================================
    user_id = request.user["id"]
    used = Upload.objects.filter(user_id=user_id).aggregate(total=models.Sum("byte_size"))["total"] or 0
    if used + len(blob) > UPLOAD_QUOTA_BYTES:
        logger.warning("Upload quota reached by user %s (%d bytes stored)", user_id, used)
        return json_error(f"Storage quota reached. Max {UPLOAD_QUOTA_BYTES // (1024 * 1024)} MB per account",
                          413, code="quota_exceeded")


    # STEP 4: atomic write, then the ownership row (same request
    # transaction — a failed INSERT cannot leave an unowned file
    # that the quota would never count)
    # ==========================================================
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    if not atomic_write(stored_name, blob):
        return json_error("Storage is unavailable, try again later", 507, code="storage_unavailable")

    Upload.objects.create(
        id=str(uuid.uuid4()), filename=stored_name, user_id=user_id,
        byte_size=len(blob), created_at=utc_now(),
    )

    return json_response({
        "url": f"/api/uploads/{stored_name}",
        "filename": stored_name,
        # What a document message carries as its attachment — the
        # name the sender chose, the byte size, the canonical mime
        "name": os.path.basename(file.name) or stored_name,
        "size": len(blob),
        "mime": gates.MIME_BY_EXT.get(ext) or f"image/{'jpeg' if ext == 'jpg' else ext}",
        # Photos only — None for documents, videos and audio
        "width": width,
        "height": height,
        "preview": preview,
    }, status=201)








############################################################
# delete_file
############################################################
#
# DELETE /api/uploads/<filename> — the uploader or an admin
# drops a stored file. A name with no ownership row is a
# 404 for everyone but an admin — an unknown name and an
# orphaned file look the same from outside. An already-
# missing file is still a 200 — the row goes and the
# caller got what it asked for — but a file that SURVIVES
# the unlink answers 500 delete_failed rather than lying:
# this is the erasure and moderation path.
#
# Used by:
#   - admin/moderation tooling and manual erasure requests
############################################################

@require_auth
def delete_file(request, filename):
    safe_name = safe_upload_name(filename)
    if not safe_name:
        return json_error("Invalid filename", 400, code="bad_filename")

    row = Upload.objects.filter(filename=safe_name).values("user_id").first()
    is_admin = request.user.get("role") == "admin"

    if row is None and not is_admin:
        return json_error("File not found", 404)
    if row is not None and not is_admin and row["user_id"] != request.user["id"]:
        return json_error("Only the owner can delete this file", 403)

    delete_upload(safe_name)

    try:
        survived = os.path.lexists(os.path.join(upload_dir(), safe_name))
    except OSError:
        survived = True
    if survived:
        logger.error("Delete left %s on disk — there is no sweep, collect it by hand", safe_name)
        return json_error("The file could not be removed, try again later", 500, code="delete_failed")

    return json_response({"ok": True})








############################################################
# serve_file
############################################################
#
# GET /api/uploads/<filename> — public on purpose (avatars
# and post/chat images render for anonymous viewers too).
# The layers that keep it inside the flat directory: the
# URL converter refuses slashes, safe_upload_name admits
# ONLY the uuid-hex names this app writes, and a realpath
# check makes sure the bytes actually live where the name
# says — a symlink planted in the volume must not turn this
# unauthenticated route into an arbitrary file read. 24 h
# of BROWSER caching only (Cache-Control: private): chat
# photos share this route, and a shared proxy cache has no
# business holding them.
#
# Used by:
#   - services/api/client.ts getUploadUrl — every rendered
#     avatar and post/chat image
############################################################

def serve_file(request, filename):
    safe_name = safe_upload_name(filename)
    if not safe_name:
        return json_error("Invalid filename", 400, code="bad_filename")

    directory = upload_dir()
    file_path = os.path.join(directory, safe_name)
    if not os.path.isfile(file_path):
        return json_error("File not found", 404)

    if os.path.realpath(file_path) != os.path.join(os.path.realpath(directory), safe_name):
        logger.warning("Refused %s: it resolves outside the upload directory", safe_name)
        return json_error("File not found", 404)

    content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    response = FileResponse(open(file_path, "rb"), content_type=content_type)
    response["Cache-Control"] = "private, max-age=86400"
    return response
