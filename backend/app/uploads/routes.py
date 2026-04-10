"""Image upload and serving routes."""

import os
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from app.auth.routes import require_auth

uploads_bp = Blueprint("uploads", __name__)

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB

# Magic bytes for allowed image formats
_MAGIC_BYTES = {
    b"\xff\xd8\xff": "jpg",       # JPEG
    b"\x89PNG\r\n\x1a\n": "png",  # PNG
    b"GIF87a": "gif",              # GIF87a
    b"GIF89a": "gif",              # GIF89a
    b"RIFF": "webp",               # WebP (RIFF container, further checked below)
}


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _validate_magic_bytes(file_obj) -> bool:
    """Check that the file's magic bytes match an allowed image format.

    Reads the first 16 bytes, then seeks back to start.
    """
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


def _get_upload_dir() -> str:
    upload_dir = os.path.abspath(current_app.config["UPLOAD_DIR"])
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


@uploads_bp.route("", methods=["POST"])
@require_auth
def upload_file():
    """
    Upload an image file.

    Accepts multipart/form-data with a 'file' field.
    Returns the URL path to access the uploaded file.
    Max size: 5 MB. Allowed: jpg, jpeg, png, gif, webp.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    if not _allowed_file(file.filename):
        return jsonify({"error": f"File type not allowed. Use: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    # Check file size by reading content
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)

    if size > MAX_FILE_SIZE:
        return jsonify({"error": f"File too large. Max {MAX_FILE_SIZE // (1024*1024)} MB"}), 400

    if size == 0:
        return jsonify({"error": "Empty file"}), 400

    # Validate magic bytes match an allowed image format
    if not _validate_magic_bytes(file):
        return jsonify({"error": "File content does not match an allowed image format"}), 400

    # Generate unique filename
    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    safe_name = secure_filename(filename)

    upload_dir = _get_upload_dir()
    file.save(os.path.join(upload_dir, safe_name))

    # Return the URL path (relative to API base)
    url = f"/api/uploads/{safe_name}"

    return jsonify({"url": url, "filename": safe_name}), 201


@uploads_bp.route("/<filename>", methods=["GET"])
def serve_file(filename):
    """Serve an uploaded file. No auth required so images can be displayed to anyone."""
    safe_name = secure_filename(filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400

    upload_dir = _get_upload_dir()
    file_path = os.path.join(upload_dir, safe_name)

    if not os.path.isfile(file_path):
        return jsonify({"error": "File not found"}), 404

    return send_from_directory(upload_dir, safe_name, max_age=86400)
