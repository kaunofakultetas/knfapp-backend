############################################################
#  [*] App — the Flask application factory
#
#  create_app() is the one place the process is assembled:
#  config from env vars, ProxyFix + CORS + the module-level
#  Socket.IO server, the SQLite schema (database.init_db),
#  the ten blueprints, the chat socket events, the scraper
#  scheduler, and the request/response hooks every route
#  inherits. main.py calls it once per process (twice under
#  the APP_DEBUG reloader) and serves the result with
#  socketio.run().
#
#  Config (env var → app.config, dev default in code):
#    DB_PATH          — database.init_db; compose mounts
#                       ./_DATA/backend on /data and sets
#                       /data/knfapp.db
#    UPLOAD_DIR       — uploads/routes.py; compose: /data/uploads
#    ALLOWED_ORIGINS  — env only, not stored: CORS + Socket.IO
#    SECRET_KEY, JWT_SECRET, INVITATION_EXPIRY_HOURS — set and
#                       read by NOTHING: sessions are opaque
#                       uuid4 tokens (auth/routes.py) and
#                       admin/routes.py takes the invitation
#                       expiry from the request body
#
#  Request pipeline, in order:
#    validate_json_input   before_request — NUL-strip JSON
#                          object bodies, whitelist the
#                          avatar_url scheme
#    <the route>
#    add_security_headers  after_request — registered second,
#                          so Flask (reverse order) runs it
#                          first; it only touches headers
#    escape_json_output    after_request — html.escape every
#                          string in a 2xx/3xx JSON body
#
#  XSS model: RAW text in the DB, escaping on OUTPUT only
#  (database migrations v1/v2 are the scar of an earlier
#  escape-on-input attempt). The mobile client
#  (services/api/client.ts) entity-decodes every response.
#  Socket.IO emits from chat/routes.py never pass through
#  Flask's response cycle, so the same message text arrives
#  escaped over REST and raw over the socket.
#
#  Blueprints (prefix → module):
#    /api/auth           auth/routes.py
#    /api/news           news/routes.py
#    /api/schedule       schedule/routes.py
#    /api/admin          admin/routes.py
#    /api/scraper        scraper/routes.py
#    /api/chat           chat/routes.py
#    /api/social         social/routes.py
#    /api/uploads        uploads/routes.py
#    /api/info           info/routes.py
#    /api/notifications  notifications/routes.py
#
#    GET /api/health     — liveness probe, the one route here
############################################################


import html
import json as json_mod
import os
from urllib.parse import urlparse

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

from app.database import init_db

# The one Socket.IO server for the process, created unbound and
# attached to the app in create_app() (init_app). Module-level so
# main.py can hand it to socketio.run() and chat/routes.py can emit
# through it (fetched lazily there via _get_socketio); chat/events.py
# receives it as a parameter instead
socketio = SocketIO()








############################################################
# create_app
############################################################
#
# Assembles and returns the Flask app; the file header has
# the shape. Worth knowing before touching it:
#
#   - blueprint modules are imported INSIDE the factory:
#     every routes module imports app.database and
#     app.auth.routes at its top, so importing them at this
#     module's top would run them while the app package is
#     still half-initialised.
#   - only the DB directory is created here; UPLOAD_DIR is
#     made lazily by uploads/routes.py. The container runs
#     read_only with ./_DATA/backend on /data, so both paths
#     have to live under /data in compose.
#   - CORS covers /api/* only; /socket.io/* is policed by
#     Flask-SocketIO's own cors_allowed_origins, fed the
#     same list. ALLOWED_ORIGINS="*" goes through as the
#     bare string. The dev default admits the Expo dev
#     server (8081) and the screenshot harness (8083);
#     production compose lists https://knfapp.knf-hosting.lt
#     plus the localhost variants — Flask-SocketIO checks
#     Origin on the polling handshake even for same-origin
#     pages.
#   - async_mode="threading" with no simple-websocket in
#     requirements.txt: the WebSocket transport is never
#     available, every client long-polls.
#   - ProxyFix trusts exactly one hop of X-Forwarded-*, and
#     the ingress Caddyfile forwards X-Forwarded-For
#     verbatim, so request.remote_addr (the auth rate-limit
#     key) is whatever the proxy chain put there.
#   - init_db() and start_scraper_scheduler() run on every
#     call — once per process normally, twice under the
#     APP_DEBUG reloader (see main.py).
#
# Used by:
#   - main.py — main(), once per process
############################################################

def create_app():
    # STEP 1: config — every knob is an env var with a dev default
    # ============================================================
    app = Flask(__name__)

    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    app.config["DB_PATH"] = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "knfapp.db"))
    # Read by nothing (see the file header); kept so the compose
    # environment keeps validating
    app.config["JWT_SECRET"] = os.environ.get("JWT_SECRET", "jwt-dev-secret-change-me")
    app.config["INVITATION_EXPIRY_HOURS"] = int(os.environ.get("INVITATION_EXPIRY_HOURS", "168"))
    app.config["UPLOAD_DIR"] = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "uploads"))


    # STEP 2: reverse-proxy awareness, CORS, Socket.IO
    # ================================================
    # One hop of X-Forwarded-For/Proto/Host/Prefix is trusted (Caddy)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # A bare "*" stays a string; anything else becomes a trimmed list
    allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:8081,http://localhost:8083")
    if allowed_origins != "*":
        allowed_origins = [o.strip() for o in allowed_origins.split(",") if o.strip()]
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

    # Same list for the /socket.io/* handshake, which flask-cors never
    # sees; threading mode means long-polling only (no simple-websocket)
    socketio.init_app(app, cors_allowed_origins=allowed_origins, async_mode="threading")


    # STEP 3: data directory and schema
    # =================================
    db_dir = os.path.dirname(app.config["DB_PATH"])
    os.makedirs(db_dir, exist_ok=True)

    # Creates/migrates the schema and pins the path get_db() reads
    with app.app_context():
        init_db(app.config["DB_PATH"])


    # STEP 4: blueprints — imported here, not at module top (each
    # pulls in app.database and app.auth.routes, see the banner)
    # ===========================================================
    from app.auth.routes import auth_bp
    from app.news.routes import news_bp
    from app.schedule.routes import schedule_bp
    from app.admin.routes import admin_bp
    from app.scraper.routes import scraper_bp
    from app.chat.routes import chat_bp
    from app.social.routes import social_bp
    from app.uploads.routes import uploads_bp
    from app.info.routes import info_bp
    from app.notifications.routes import notifications_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(news_bp, url_prefix="/api/news")
    app.register_blueprint(schedule_bp, url_prefix="/api/schedule")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
    app.register_blueprint(scraper_bp, url_prefix="/api/scraper")
    app.register_blueprint(chat_bp, url_prefix="/api/chat")
    app.register_blueprint(social_bp, url_prefix="/api/social")
    app.register_blueprint(uploads_bp, url_prefix="/api/uploads")
    app.register_blueprint(info_bp, url_prefix="/api/info")
    app.register_blueprint(notifications_bp, url_prefix="/api/notifications")


    # STEP 5: realtime chat handlers and the background scrapers
    # ==========================================================
    from app.chat.events import register_socket_events
    register_socket_events(socketio)

    # APScheduler jobs (news 20 min, timetable 6 h, faculty info 24 h)
    # plus one-shot startup timers at 2 s / 30 s / 60 s — in every
    # process that builds an app
    from app.scraper.scheduler import start_scraper_scheduler
    start_scraper_scheduler(app)


    # STEP 6: request/response hooks, the health route and the JSON
    # error handlers — each carries its own banner below
    # =============================================================
    _AVATAR_URL_MAX_LENGTH = 2048
    _ALLOWED_URL_SCHEMES = {"http", "https"}






    ############################################################
    # _validate_avatar_url
    ############################################################
    #
    # (is_valid, error) for one avatar_url value: None and ""
    # pass (that is how a client clears the avatar), a
    # non-string or anything over 2048 chars fails, a relative
    # /api/uploads/ path passes (own uploads), and anything
    # else must urlparse with an http/https scheme — compared
    # lower-cased so "JaVaScRiPt:" and friends are caught.
    # Validation only, no escaping.
    #
    # Used by:
    #   - validate_json_input (below)
    ############################################################

    def _validate_avatar_url(url):
        if url is None or url == "":
            return True, None
        if not isinstance(url, str):
            return False, "avatar_url must be a string"
        if len(url) > _AVATAR_URL_MAX_LENGTH:
            return False, f"avatar_url must be at most {_AVATAR_URL_MAX_LENGTH} characters"
        # Own uploads are stored as relative paths, never absolute URLs
        if url.startswith("/api/uploads/"):
            return True, None
        try:
            parsed = urlparse(url)
            if parsed.scheme.lower() not in _ALLOWED_URL_SCHEMES:
                return False, "avatar_url must use http:// or https:// scheme, or be a relative /api/uploads/ path"
        except Exception:
            return False, "avatar_url is not a valid URL"
        return True, None






    ############################################################
    # _strip_null_bytes
    ############################################################
    #
    # Recursively drops "\x00" from every string in a parsed
    # JSON structure — dicts and lists are walked, every other
    # scalar comes back untouched — so NUL never reaches SQLite
    # text columns. Input side only.
    #
    # Used by:
    #   - validate_json_input (below)
    ############################################################

    def _strip_null_bytes(value):
        if isinstance(value, str):
            return value.replace("\x00", "")
        if isinstance(value, dict):
            return {k: _strip_null_bytes(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_strip_null_bytes(item) for item in value]
        return value






    ############################################################
    # validate_json_input
    ############################################################
    #
    # before_request for every route. Only JSON content types
    # and only OBJECT bodies are touched (a top-level list goes
    # by untouched), and get_json(silent=True) means a
    # malformed body is ignored here and left for the handler's
    # own get_json() to reject. Two jobs: NUL-strip every
    # string, and whitelist the avatar_url scheme through
    # _validate_avatar_url → 400 {"error": ...}. No html.escape
    # — escaping on input double-escaped round-trip edits
    # (database migrations v1/v2).
    #
    # Gotchas:
    #   - the cleaned body is installed by overwriting
    #     Werkzeug's PRIVATE request._cached_json, a
    #     (silent=False, silent=True) pair — both slots are set
    #     so either get_json() flavour sees the clean copy. A
    #     Werkzeug upgrade can break this silently.
    #   - only the snake_case key is checked. auth/routes.py
    #     update_me and social/routes.py update_profile also
    #     accept "avatarUrl", which walks past this hook and is
    #     stored verbatim by both; the mobile app itself sends
    #     avatar_url (services/api/social.ts), so its own
    #     traffic is covered.
    #
    # Used by:
    #   - Flask — @app.before_request; nothing calls it directly
    ############################################################

    @app.before_request
    def validate_json_input():
        if request.content_type and "json" in request.content_type.lower():
            data = request.get_json(silent=True)
            if data and isinstance(data, dict):
                cleaned = _strip_null_bytes(data)
                if cleaned != data:
                    # Both cache slots, or the other get_json() flavour would
                    # still hand the handler the original body
                    request._cached_json = (cleaned, cleaned)
                    data = cleaned

                if "avatar_url" in data:
                    valid, err = _validate_avatar_url(data["avatar_url"])
                    if not valid:
                        return jsonify({"error": err}), 400






    ############################################################
    # _escape_value
    ############################################################
    #
    # Recursive html.escape (quote=True, so " and ' become
    # entities too) over every string in a JSON structure;
    # dict keys are left alone. URLs are escaped like any other
    # string — an "&" in a query string goes out as "&amp;" —
    # which is why the mobile client decodes whole responses,
    # not just display text.
    #
    # Used by:
    #   - escape_json_output (below)
    ############################################################

    def _escape_value(value):
        if isinstance(value, str):
            return html.escape(value, quote=True)
        if isinstance(value, dict):
            return {k: _escape_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_escape_value(item) for item in value]
        return value






    ############################################################
    # escape_json_output
    ############################################################
    #
    # after_request: the single escaping point for REST. JSON
    # responses with status < 400 are re-serialised with every
    # string escaped; ensure_ascii=False keeps Lithuanian text
    # as UTF-8 rather than \uXXXX. Untouched: error bodies
    # (>= 400), the image bytes uploads/routes.py serves,
    # anything that is not JSON — and the Socket.IO emits from
    # chat/routes.py, which never enter Flask's response cycle.
    # Any failure while escaping is swallowed and the raw body
    # ships rather than a 500.
    #
    # Used by:
    #   - Flask — @app.after_request
    #   - mobile services/api/client.ts undoes it on every
    #     response (decodeHtmlEntities)
    ############################################################

    @app.after_request
    def escape_json_output(response):
        if (
            response.content_type
            and "json" in response.content_type.lower()
            and response.status_code < 400
        ):
            try:
                data = response.get_json(silent=True)
                if data is not None:
                    escaped = _escape_value(data)
                    # ensure_ascii=False: Lithuanian text stays UTF-8
                    response.set_data(
                        json_mod.dumps(escaped, ensure_ascii=False)
                    )
            except Exception:
                pass  # the raw body ships rather than a 500
        return response






    ############################################################
    # add_security_headers
    ############################################################
    #
    # after_request: hardening headers on EVERY response, JSON
    # and images included. Registered after escape_json_output,
    # so Flask (reverse order) runs it first — harmless, one
    # touches headers and the other the body. HSTS goes out
    # although this process only ever speaks plain HTTP on
    # :8000 — TLS ends at the hosting proxy in front of
    # knfapp-caddy. X-XSS-Protection is a legacy header modern
    # browsers ignore.
    #
    # Used by:
    #   - Flask — @app.after_request
    ############################################################

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; font-src 'self'"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response






    ############################################################
    # health
    ############################################################
    #
    # GET /api/health
    #
    # Liveness probe: {"status": "ok", "service":
    # "knfapp-backend"}, no DB touch. The one route defined
    # outside a blueprint.
    #
    # Used by:
    #   - nothing calls this at the moment — no compose
    #     healthcheck, nothing in the mobile app; it is only
    #     described in swagger/swagger.yaml
    ############################################################

    @app.route("/api/health")
    def health():
        return {"status": "ok", "service": "knfapp-backend"}






    ############################################################
    # bad_request
    ############################################################
    #
    # 400 → {"error": "Bad request"}. Flask's default error
    # pages are HTML; the mobile client reads an "error" key.
    # No route calls abort(), so this fires for Werkzeug-raised
    # 400s — a handler's get_json() on a malformed JSON body —
    # and never for the routes' own "jsonify(...), 400"
    # replies, which keep their message.
    #
    # Used by:
    #   - Flask — @app.errorhandler(400)
    ############################################################

    @app.errorhandler(400)
    def bad_request(e):
        return {"error": "Bad request"}, 400






    ############################################################
    # not_found
    ############################################################
    #
    # 404 → {"error": "Not found"} for unknown paths. Behind
    # the ingress only /api/* and /socket.io/* reach this
    # process, so in production every hit is an API miss;
    # routes that answer their own 404 (uploads/routes.py
    # serve_file, for one) keep their own message.
    #
    # Used by:
    #   - Flask — @app.errorhandler(404)
    ############################################################

    @app.errorhandler(404)
    def not_found(e):
        return {"error": "Not found"}, 404






    ############################################################
    # method_not_allowed
    ############################################################
    #
    # 405 → {"error": "Method not allowed"}: a known path hit
    # with a verb none of its rules accept.
    #
    # Used by:
    #   - Flask — @app.errorhandler(405)
    ############################################################

    @app.errorhandler(405)
    def method_not_allowed(e):
        return {"error": "Method not allowed"}, 405






    ############################################################
    # unsupported_media_type
    ############################################################
    #
    # 415 → {"error": "Unsupported media type"}: what a
    # handler's request.get_json() raises when the body arrives
    # without a JSON Content-Type — the many routes that call
    # it without silent=True land here.
    #
    # Used by:
    #   - Flask — @app.errorhandler(415)
    ############################################################

    @app.errorhandler(415)
    def unsupported_media_type(e):
        return {"error": "Unsupported media type"}, 415






    ############################################################
    # internal_error
    ############################################################
    #
    # 500 → {"error": "Internal server error"} for unhandled
    # exceptions, deliberately generic — the traceback stays in
    # the process log. With APP_DEBUG=1 Flask propagates the
    # exception to the debugger instead and this never runs.
    #
    # Used by:
    #   - Flask — @app.errorhandler(500)
    ############################################################

    @app.errorhandler(500)
    def internal_error(e):
        return {"error": "Internal server error"}, 500

    return app
