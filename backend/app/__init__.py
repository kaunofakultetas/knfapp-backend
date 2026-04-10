"""knfapp-backend Flask application factory."""

import html
import json as json_mod
import os
from urllib.parse import urlparse

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

from app.database import init_db

# Module-level SocketIO instance so chat routes can import it
socketio = SocketIO()


def create_app():
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DB_PATH"] = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "knfapp.db"))
    app.config["JWT_SECRET"] = os.environ.get("JWT_SECRET", "jwt-dev-secret-change-me")
    app.config["INVITATION_EXPIRY_HOURS"] = int(os.environ.get("INVITATION_EXPIRY_HOURS", "168"))
    app.config["UPLOAD_DIR"] = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "uploads"))

    # ProxyFix for running behind Caddy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # CORS — restrict origins in production via ALLOWED_ORIGINS env var (comma-separated)
    allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:8081,http://localhost:8083")
    if allowed_origins != "*":
        allowed_origins = [o.strip() for o in allowed_origins.split(",") if o.strip()]
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

    # Initialize Socket.IO with same CORS policy
    socketio.init_app(app, cors_allowed_origins=allowed_origins, async_mode="threading")

    # Ensure data directory exists
    db_dir = os.path.dirname(app.config["DB_PATH"])
    os.makedirs(db_dir, exist_ok=True)

    # Initialize database
    with app.app_context():
        init_db(app.config["DB_PATH"])

    # Register blueprints
    from app.auth.routes import auth_bp
    from app.news.routes import news_bp
    from app.schedule.routes import schedule_bp
    from app.admin.routes import admin_bp
    from app.scraper.routes import scraper_bp
    from app.chat.routes import chat_bp
    from app.social.routes import social_bp
    from app.uploads.routes import uploads_bp
    from app.info.routes import info_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(news_bp, url_prefix="/api/news")
    app.register_blueprint(schedule_bp, url_prefix="/api/schedule")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(scraper_bp, url_prefix="/api/scraper")
    app.register_blueprint(chat_bp, url_prefix="/api/chat")
    app.register_blueprint(social_bp, url_prefix="/api/social")
    app.register_blueprint(uploads_bp, url_prefix="/api/uploads")
    app.register_blueprint(info_bp, url_prefix="/api/info")

    # Register Socket.IO events for real-time chat
    from app.chat.events import register_socket_events
    register_socket_events(socketio)

    # Start background scraper
    from app.scraper.scheduler import start_scraper_scheduler
    start_scraper_scheduler(app)

    # ── Global XSS protection middleware ──────────────────────────────────
    # Architecture: store RAW text, escape on OUTPUT.
    #
    # before_request:  validate avatar_url scheme (input validation only,
    #                  NO html.escape — that caused double-escaping).
    # after_request:   html.escape all string values in JSON responses
    #                  so clients always receive safe HTML.

    _AVATAR_URL_MAX_LENGTH = 2048
    _ALLOWED_URL_SCHEMES = {"http", "https"}

    def _validate_avatar_url(url):
        """Return (is_valid, error_message) for avatar_url.
        Whitelist http:// and https:// schemes, OR relative paths
        starting with /api/uploads/ (own uploaded avatars).
        Case-insensitive to block JaVaScRiPt: etc."""
        if url is None or url == "":
            return True, None
        if not isinstance(url, str):
            return False, "avatar_url must be a string"
        if len(url) > _AVATAR_URL_MAX_LENGTH:
            return False, f"avatar_url must be at most {_AVATAR_URL_MAX_LENGTH} characters"
        # Allow relative upload paths (e.g. /api/uploads/abc-123.jpg)
        if url.startswith("/api/uploads/"):
            return True, None
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
                return False, "avatar_url must use http:// or https:// scheme, or be a relative /api/uploads/ path"
        except Exception:
            return False, "avatar_url is not a valid URL"
        return True, None

    def _strip_null_bytes(value):
        """Recursively strip null bytes (\x00) from all strings in a
        JSON-serializable structure.  Applied on INPUT to prevent null
        bytes from reaching the database."""
        if isinstance(value, str):
            return value.replace("\x00", "")
        if isinstance(value, dict):
            return {k: _strip_null_bytes(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_strip_null_bytes(item) for item in value]
        return value

    @app.before_request
    def validate_json_input():
        """Validate and sanitize JSON request bodies:
        1. Strip null bytes from all string fields.
        2. Validate avatar_url scheme (whitelist).
        No html.escape here — escaping is done on OUTPUT to prevent
        double-escaping on round-trip edits."""
        if request.content_type and "json" in request.content_type.lower():
            data = request.get_json(silent=True)
            if data and isinstance(data, dict):
                # Strip null bytes from all string values
                cleaned = _strip_null_bytes(data)
                if cleaned != data:
                    # Replace Werkzeug's internal JSON cache so downstream
                    # get_json() returns the cleaned data.
                    request._cached_json = (cleaned, cleaned)
                    data = cleaned

                if "avatar_url" in data:
                    valid, err = _validate_avatar_url(data["avatar_url"])
                    if not valid:
                        return jsonify({"error": err}), 400

    def _escape_value(value):
        """Recursively HTML-escape every string in a JSON-serializable
        structure.  Used on OUTPUT (responses), never on input."""
        if isinstance(value, str):
            return html.escape(value, quote=True)
        if isinstance(value, dict):
            return {k: _escape_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_escape_value(item) for item in value]
        return value

    @app.after_request
    def escape_json_output(response):
        """HTML-escape all string values in JSON API responses.
        This is the single point of XSS protection — raw text is
        stored in DB and escaped only when sent to clients."""
        if (
            response.content_type
            and "json" in response.content_type.lower()
            and response.status_code < 400
        ):
            try:
                data = response.get_json(silent=True)
                if data is not None:
                    escaped = _escape_value(data)
                    response.set_data(
                        json_mod.dumps(escaped, ensure_ascii=False)
                    )
            except Exception:
                pass  # Don't break responses if escaping fails
        return response

    # Security headers middleware
    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self'"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

    @app.route("/api/health")
    def health():
        return {"status": "ok", "service": "knfapp-backend"}

    # JSON error handlers for API routes (Flask defaults return HTML)
    @app.errorhandler(400)
    def bad_request(e):
        return {"error": "Bad request"}, 400

    @app.errorhandler(404)
    def not_found(e):
        return {"error": "Not found"}, 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return {"error": "Method not allowed"}, 405

    @app.errorhandler(415)
    def unsupported_media_type(e):
        return {"error": "Unsupported media type"}, 415

    @app.errorhandler(500)
    def internal_error(e):
        return {"error": "Internal server error"}, 500

    return app
