"""Image upload and serving routes."""

import os
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from app.auth.routes import require_auth

uploads_bp = Blueprint("uploads", __name__)

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


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
