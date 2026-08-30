############################################################
#  [*] App — the Flask application factory
#
#  create_app() is the one place the process is assembled:
#  config from env vars, ProxyFix + CORS + the module-level
#  Socket.IO server, the SQLite schema (database.init_db),
#  the ten blueprints, the chat socket events and the
#  request/response hooks every route inherits. main.py
#  calls it once per process, INSIDE its --http branch — so
#  `python main.py` with no flags no longer builds a
#  database — and serves the result with socketio.run(). The
#  scraper scheduler is started by main.py too, not here:
#  merely building an app used to fire live scrapes and real
#  push notifications.
#
#  Config (env var → app.config, dev default in code):
#    DB_PATH          — database.init_db; compose mounts
#                       ./_DATA/backend on /data and sets
#                       /data/knfapp.db
#    UPLOAD_DIR       — uploads/routes.py; compose:
#                       /data/uploads. Created AND probed for
#                       writability here, so a bad path fails
#                       the boot instead of the first upload
#    ALLOWED_ORIGINS  — env only, not stored: CORS + Socket.IO.
#                       Unset, empty or comma-only falls back
#                       to the dev defaults with a warning
#    SECRET_KEY       — no literal fallback any more: unset or
#                       empty becomes a per-process random
#                       secret, so a future signing feature can
#                       never inherit a published dev key
#    JWT_SECRET       — falls back to SECRET_KEY; read by
#                       nothing yet (sessions are opaque uuid4
#                       tokens, auth/routes.py)
#    INVITATION_EXPIRY_HOURS — read by nothing (admin/routes.py
#                       takes the expiry from the request body);
#                       a non-numeric value warns and falls back
#                       instead of crash-looping the container
#    GLOBAL_RATE_LIMIT — requests per client IP per 5-minute
#                       window before throttle_requests answers
#                       429; 0 or less disables the global
#                       budget (the per-route quotas stay)
#    SCRAPER_ENABLED  — read by main.py, not here
#
#  Request pipeline, in order:
#    throttle_requests     before_request — the global per-IP
#                          budget; the per-route quotas are
#                          auth's rate_limit decorator
#    validate_json_input   before_request — OBJECT JSON bodies
#                          only, NUL-stripped under a depth cap,
#                          avatar_url / avatarUrl pinned to own
#                          uploads
#    <the route>
#    add_security_headers  after_request — headers only, plus
#                          Cache-Control: no-store on every
#                          /api/ JSON answer
#
#  XSS model: RAW text in the DB, escaping on OUTPUT only
#  (database migrations v1/v2 are the scar of an earlier
#  escape-on-input attempt). The escaping lives INSIDE the
#  JSON provider (_EscapingJSONProvider) rather than in an
#  after_request hook: every jsonify body — 2xx and error
#  alike — is escaped once during serialisation instead of
#  being dumped, re-parsed and re-dumped, and a body that
#  cannot be escaped fails CLOSED (500) instead of shipping
#  raw. The mobile client
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
#    GET /api/health     — readiness probe (SELECT 1 + an
#                          UPLOAD_DIR writability check), the
#                          one route defined here
############################################################


import html
import logging
import os
import re
import secrets
import sqlite3

from flask import Flask, has_request_context, jsonify, request
from flask.json.provider import DefaultJSONProvider
from flask_cors import CORS
from flask_socketio import SocketIO
from werkzeug.middleware.proxy_fix import ProxyFix

from app.database import get_db, init_db

logger = logging.getLogger(__name__)

# The one Socket.IO server for the process, created unbound and
# attached to the app in create_app() (init_app). Module-level so
# main.py can hand it to socketio.run() and chat/routes.py can emit
# through it (fetched lazily there via _get_socketio); chat/events.py
# receives it as a parameter instead
socketio = SocketIO()

# The dev CORS list: the Expo dev server (8081) and the
# screenshot harness (8083). Production compose overrides it
_DEFAULT_ORIGINS = "http://localhost:8081,http://localhost:8083"

# Hard ceiling on JSON container nesting, both ways. A 100 000
# deep "[[[[...]]]]" body used to blow the recursion limit inside
# json.loads or the walkers below — an unauthenticated 500 on
# EVERY route; past this depth the body is refused with a 400
_MAX_JSON_DEPTH = 32

# Global request budget per client IP inside auth's 5-minute
# rate-limit window (~120 requests/minute). Deliberately generous:
# it is the backstop against a flood, not the per-route quota
# (auth's rate_limit decorator), and a NATed campus network puts
# many honest students behind one ProxyFix-resolved address —
# hence the GLOBAL_RATE_LIMIT override (0 or less turns the
# global budget off and leaves the per-route quotas in place)
_GLOBAL_RATE_LIMIT_MAX = 600

# Cap on a single Socket.IO HTTP payload (256 KB). Without it a
# client can batch unbounded packet bytes into one long-poll POST
_SOCKET_MAX_HTTP_BUFFER = 256 * 1024








############################################################
# _JsonTooDeep
############################################################
#
# Raised by the JSON walkers below when a structure nests
# past _MAX_JSON_DEPTH. On the INPUT side validate_json_input
# turns it into a 400; on the OUTPUT side it escapes out of
# the JSON provider and becomes a 500 — a body that could not
# be escaped must never ship.
#
# Used by:
#   - _strip_null_bytes, _escape_value, validate_json_input
#     (all inside create_app below)
############################################################

class _JsonTooDeep(Exception):
    pass








############################################################
# _env_int
############################################################
#
# An env var read as an int, with the default kept (and a
# warning logged) when the value is missing or not a number.
# A typo'd INVITATION_EXPIRY_HOURS used to raise ValueError
# straight out of create_app — a container that crash-loops
# on a one-character .env mistake, for a key nothing reads.
#
# Used by:
#   - create_app (below) — INVITATION_EXPIRY_HOURS
############################################################

def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer — falling back to %d", name, raw, default)
        return default








############################################################
# _prepare_upload_dir
############################################################
#
# Creates UPLOAD_DIR and proves it is writable, at factory
# time. The container runs read_only with only /data mounted
# writable, so the in-image default path fails here — loudly,
# naming the env var — instead of surfacing as a 500 on the
# first avatar upload months later. Returns the absolute
# path, which create_app writes back into the config so
# uploads/routes.py never has to resolve it again.
#
# Used by:
#   - create_app (below)
############################################################

def _prepare_upload_dir(path):
    upload_dir = os.path.abspath(path)
    probe = os.path.join(upload_dir, ".knfapp-write-probe")

    try:
        os.makedirs(upload_dir, exist_ok=True)
        with open(probe, "w", encoding="utf-8"):
            pass
        os.remove(probe)
    except OSError as exc:
        raise RuntimeError(f"UPLOAD_DIR {upload_dir!r} is not usable: {exc}") from exc

    return upload_dir








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
#   - BOTH data directories are created here: the DB's
#     parent and UPLOAD_DIR (probed for writability, see
#     _prepare_upload_dir). The container runs read_only
#     with ./_DATA/backend on /data, so both paths have to
#     live under /data in compose.
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
#     async_handlers=False serialises the event handlers on
#     the connection's own packet thread, so one client can
#     no longer spawn a thread per event and the chat rate
#     limiter becomes real back-pressure; engineio still
#     runs one packet thread per connection (inherent to
#     threading mode).
#   - ProxyFix trusts exactly one hop of X-Forwarded-*.
#     x_prefix is NOT trusted: the ingress never sets
#     X-Forwarded-Prefix, so honouring a client-supplied one
#     only lets a caller rewrite url_for()/redirect paths.
#     The ingress appends the real peer to X-Forwarded-For
#     (the Caddyfile no longer echoes the client's own
#     header), so request.remote_addr — the rate-limit key —
#     is the true client address.
#   - init_db() runs on every call; the scraper scheduler
#     does not (main.py starts it, once, in its --http
#     branch).
#
# Used by:
#   - main.py — main(), inside the --http branch
############################################################

def create_app():
    # STEP 1: config — every knob is an env var with a dev default
    # ============================================================
    app = Flask(__name__)

    # No published literal fallback: an unset SECRET_KEY (or the
    # empty string a "${SECRET_KEY}" substitution leaves behind when
    # the .env line is missing) becomes a random per-process secret,
    # so nothing can ever sign with a value that is in this repo
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config["DB_PATH"] = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "knfapp.db"))
    # Read by nothing yet (see the file header); falls back to
    # SECRET_KEY so a future JWT feature inherits a real secret
    app.config["JWT_SECRET"] = os.environ.get("JWT_SECRET") or app.config["SECRET_KEY"]
    # Read by nothing; a typo warns instead of killing the container
    app.config["INVITATION_EXPIRY_HOURS"] = _env_int("INVITATION_EXPIRY_HOURS", 168)
    app.config["UPLOAD_DIR"] = os.environ.get("UPLOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "data", "uploads"))
    # Werkzeug answers 413 before buffering anything bigger — uploads
    # cap at 5 MB for photos and documents and 50 MB for videos
    # (uploads/routes.py MAX_FILE_SIZE / VIDEO_MAX_SIZE); the extra
    # MB covers the multipart envelope. Caddy holds every OTHER
    # /api/* route to 6 MB, so only /api/uploads ever spools this
    # much into /tmp
    app.config["MAX_CONTENT_LENGTH"] = 52 * 1024 * 1024


    # STEP 2: reverse-proxy awareness, CORS, Socket.IO
    # ================================================
    # One hop of X-Forwarded-For/Proto/Host is trusted (Caddy). NOT
    # x_prefix: the ingress never sends X-Forwarded-Prefix, so trusting
    # one would only hand a caller control of the app's URL root
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # A bare "*" stays a string; anything else becomes a trimmed list.
    # Unset OR empty falls back to the dev defaults — a set-but-empty
    # ALLOWED_ORIGINS used to become [], which silently rejected every
    # browser origin and every Socket.IO handshake
    raw_origins = os.environ.get("ALLOWED_ORIGINS") or _DEFAULT_ORIGINS
    allowed_origins = raw_origins
    if allowed_origins != "*":
        allowed_origins = [o.strip() for o in allowed_origins.split(",") if o.strip()]
        if not allowed_origins:
            logger.warning("ALLOWED_ORIGINS=%r lists no origin — falling back to the dev defaults", raw_origins)
            allowed_origins = [o.strip() for o in _DEFAULT_ORIGINS.split(",")]
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

    # Same list for the /socket.io/* handshake, which flask-cors never
    # sees; threading mode means long-polling only (no simple-websocket).
    # async_handlers=False keeps a client's events on its own packet
    # thread instead of spawning one per event, and the buffer cap
    # bounds a single long-poll POST
    socketio.init_app(
        app,
        cors_allowed_origins=allowed_origins,
        async_mode="threading",
        async_handlers=False,
        max_http_buffer_size=_SOCKET_MAX_HTTP_BUFFER,
    )


    # STEP 3: data directories and schema
    # ===================================
    # A bare DB_PATH filename has no directory part, and makedirs("")
    # raises FileNotFoundError — the boot must survive "knfapp.db"
    db_dir = os.path.dirname(app.config["DB_PATH"])
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    # Made and probed once, here: uploads/routes.py then reads a path
    # it can trust (and stops calling makedirs on every image GET)
    app.config["UPLOAD_DIR"] = _prepare_upload_dir(app.config["UPLOAD_DIR"])

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


    # STEP 5: realtime chat handlers
    # ==============================
    from app.chat.events import register_socket_events
    register_socket_events(socketio)

    # The scraper scheduler is deliberately NOT started here. Building
    # an app — a test, a shell, `python main.py` with no flags — used
    # to hit knf.vu.lt and push real notifications within 2 seconds;
    # main.py now starts it once, in the serving process only, and
    # only when SCRAPER_ENABLED is not "0"


    # STEP 6: request/response hooks, the health route and the JSON
    # error handlers — each carries its own banner below
    # =============================================================
    # The one in-memory rate limiter lives in auth/routes.py; the
    # global budget below spends from the same store, so there is
    # still exactly one limiter in the process
    from app.auth.routes import _check_rate_limit, _rate_limited_response

    global_rate_limit = _env_int("GLOBAL_RATE_LIMIT", _GLOBAL_RATE_LIMIT_MAX)

    _AVATAR_URL_MAX_LENGTH = 2048
    # Exactly the shape uploads/routes.py stores — uuid4().hex plus one
    # of its ALLOWED_EXTENSIONS. A bare startswith() also admitted
    # "/api/uploads/../../../etc/passwd" and query-string smuggling.
    # Anchored with \Z, NOT $: "$" also matches before a trailing
    # newline, so "...jpg\n" used to validate and be stored verbatim
    _AVATAR_URL_RE = re.compile(r"^/api/uploads/[0-9a-f]{32}\.(?:jpg|jpeg|png|gif|webp)\Z")






    ############################################################
    # throttle_requests
    ############################################################
    #
    # before_request, registered FIRST so a flood is refused
    # before any body is parsed: one global budget per client
    # IP (GLOBAL_RATE_LIMIT, default _GLOBAL_RATE_LIMIT_MAX,
    # inside auth's 5-minute window) on top of the per-route
    # quotas. Until this hook the only throttling in the app
    # was auth's two login endpoints — every feed, chat and
    # upload route was unmetered. Answers auth's house 429
    # ({error, code: rate_limited} + Retry-After). CORS
    # preflights are exempt: a rejected OPTIONS would surface
    # in the browser as a CORS error rather than a rate limit.
    # Socket.IO traffic never reaches here (engineio answers
    # /socket.io/* above Flask's dispatch) — chat/events.py has
    # its own per-user limiter.
    #
    # Used by:
    #   - Flask — @app.before_request; nothing calls it directly
    ############################################################

    @app.before_request
    def throttle_requests():
        if global_rate_limit <= 0 or request.method == "OPTIONS":
            return None

        key = f"global:{request.remote_addr or 'unknown'}"
        if _check_rate_limit(key, max_attempts=global_rate_limit):
            logger.warning("Global rate limit hit: %s (%s %s)", key, request.method, request.path)
            return _rate_limited_response("Too many requests. Please wait a few minutes.", key)

        return None






    ############################################################
    # _validate_avatar_url
    ############################################################
    #
    # (is_valid, error) for one avatar_url value: None and ""
    # pass (that is how a client clears the avatar), a
    # non-string or anything over 2048 chars fails, and
    # everything else must match _AVATAR_URL_RE — the exact
    # shape uploads/routes.py hands out, not merely a
    # "/api/uploads/" prefix, which traversal ("..%2f..") and
    # query-string ("...jpg?x=//evil") payloads walked
    # straight through. The rule before that admitted any
    # http(s) URL, which let a profile avatar beacon every
    # viewer's IP/UA to an attacker-chosen host; absolute
    # URLs are rejected outright, javascript:/data: and
    # friends with them. Write side only — stored avatars are
    # untouched. Validation only, no escaping.
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
        if _AVATAR_URL_RE.match(url):
            return True, None
        return False, "avatar_url must be a relative /api/uploads/ path"






    ############################################################
    # _strip_null_bytes
    ############################################################
    #
    # Drops "\x00" from every string in a parsed JSON structure
    # — dicts and lists are walked, every other scalar comes
    # back untouched — so NUL never reaches SQLite text
    # columns. Input side only.
    #
    # The walk is ITERATIVE (an explicit stack of
    # source/target/depth triples) and capped at
    # _MAX_JSON_DEPTH: the old recursive version turned a
    # 100 000-deep body into a RecursionError, i.e. an
    # unauthenticated 500 on every route in the app. Nesting
    # past the cap raises _JsonTooDeep, which the caller
    # answers with a 400.
    #
    # Used by:
    #   - validate_json_input (below)
    ############################################################

    def _strip_null_bytes(value):
        if isinstance(value, str):
            return value.replace("\x00", "")
        if not isinstance(value, (dict, list)):
            return value

        root = {} if isinstance(value, dict) else []
        stack = [(value, root, 1)]

        while stack:
            src, dst, depth = stack.pop()
            items = src.items() if isinstance(src, dict) else enumerate(src)
            for key, item in items:
                if isinstance(item, str):
                    child = item.replace("\x00", "")
                elif isinstance(item, (dict, list)):
                    if depth >= _MAX_JSON_DEPTH:
                        raise _JsonTooDeep()
                    # The empty container is linked in now and filled
                    # when the stack gets to it — same object either way
                    child = {} if isinstance(item, dict) else []
                    stack.append((item, child, depth + 1))
                else:
                    child = item

                if isinstance(dst, list):
                    dst.append(child)
                else:
                    dst[key] = child

        return root






    ############################################################
    # validate_json_input
    ############################################################
    #
    # before_request for every route. Only JSON content types
    # are touched, and get_json(silent=True) means a malformed
    # body is ignored here and left for the handler's own
    # get_json() to reject. Three jobs: refuse a non-OBJECT
    # body with a 400 (a top-level array or scalar used to sail
    # through to some 27 handlers whose data.get() then raised
    # an unauthenticated AttributeError 500 — auth's
    # get_json_object is the same guard at the call sites),
    # NUL-strip every string, and pin avatar_url to own uploads
    # through _validate_avatar_url → 400 {"error": ...} — BOTH
    # key spellings, since auth/routes.py update_me and
    # social/routes.py update_profile also accept "avatarUrl"
    # (the camelCase key used to walk past this hook and be
    # stored verbatim). No html.escape — escaping on input
    # double-escaped round-trip edits (database migrations
    # v1/v2).
    #
    # Gotchas:
    #   - the cleaned body is installed by overwriting
    #     Werkzeug's PRIVATE request._cached_json, a
    #     (silent=False, silent=True) pair — both slots are set
    #     so either get_json() flavour sees the clean copy. A
    #     Werkzeug upgrade can break this silently.
    #   - the WHOLE body — get_json() included, where json.loads
    #     recurses per nesting level — runs under one
    #     RecursionError/_JsonTooDeep guard, so a deeply nested
    #     body is a 400 and never a 500.
    #
    # Used by:
    #   - Flask — @app.before_request; nothing calls it directly
    ############################################################

    @app.before_request
    def validate_json_input():
        if not (request.content_type and "json" in request.content_type.lower()):
            return None

        try:
            data = request.get_json(silent=True)
            # None = absent or malformed: the handler answers that
            if data is None:
                return None
            if not isinstance(data, dict):
                return jsonify({"error": "JSON body must be an object"}), 400

            cleaned = _strip_null_bytes(data)
            if cleaned != data:
                # Both cache slots, or the other get_json() flavour would
                # still hand the handler the original body
                request._cached_json = (cleaned, cleaned)
                data = cleaned

            # Both spellings — the routes accept either
            for key in ("avatar_url", "avatarUrl"):
                if key in data:
                    valid, err = _validate_avatar_url(data[key])
                    if not valid:
                        return jsonify({"error": err}), 400
        except (RecursionError, _JsonTooDeep):
            logger.warning("Rejected over-nested JSON body on %s %s", request.method, request.path)
            return jsonify({"error": "JSON nesting too deep"}), 400

        return None






    ############################################################
    # _escape_value
    ############################################################
    #
    # html.escape (quote=True, so " and ' become entities too)
    # over every string in a JSON structure; dict keys are left
    # alone. URLs are escaped like any other string — an "&" in
    # a query string goes out as "&amp;" — which is why the
    # mobile client decodes whole responses, not just display
    # text.
    #
    # Iterative and depth-capped for the same reason as
    # _strip_null_bytes: no walk in this file may turn a
    # pathological structure into a RecursionError. Tuples are
    # normalised to lists on the way through, exactly as
    # json.dumps would have serialised them.
    #
    # Used by:
    #   - _EscapingJSONProvider.dumps (below) — every JSON
    #     response the app serialises
    ############################################################

    def _escape_value(value):
        if isinstance(value, str):
            return html.escape(value, quote=True)
        if not isinstance(value, (dict, list, tuple)):
            return value

        root = {} if isinstance(value, dict) else []
        stack = [(value, root, 1)]

        while stack:
            src, dst, depth = stack.pop()
            items = src.items() if isinstance(src, dict) else enumerate(src)
            for key, item in items:
                if isinstance(item, str):
                    child = html.escape(item, quote=True)
                elif isinstance(item, (dict, list, tuple)):
                    if depth >= _MAX_JSON_DEPTH:
                        raise _JsonTooDeep()
                    child = {} if isinstance(item, dict) else []
                    stack.append((item, child, depth + 1))
                else:
                    child = item

                if isinstance(dst, list):
                    dst.append(child)
                else:
                    dst[key] = child

        return root






    ############################################################
    # _EscapingJSONProvider
    ############################################################
    #
    # The single escaping point for REST, moved off the
    # response hook and into serialisation itself: jsonify (and
    # every dict a route or error handler returns) now goes
    # through one dumps that escapes as it writes. The old
    # after_request hook dumped, re-parsed and re-dumped every
    # 2xx body — three serialisations of a full feed page.
    #
    # Two behaviour notes:
    #   - EVERY status is escaped now, not only < 400: the
    #     mobile client entity-decodes error bodies too, so an
    #     unescaped 400 was the one inconsistent path.
    #   - a body that cannot be escaped fails CLOSED. The
    #     exception is logged with the request path and
    #     propagates into the 500 handler instead of the old
    #     silent "ship the raw body".
    #
    # ensure_ascii=False keeps Lithuanian text as UTF-8 rather
    # than \uXXXX — the same bytes the hook used to produce.
    # Untouched: the image bytes uploads/routes.py serves,
    # anything that is not JSON, and the Socket.IO emits from
    # chat/routes.py, which never enter Flask's response cycle
    # (python-socketio serialises those with the stdlib json).
    #
    # Used by:
    #   - Flask — app.json, i.e. jsonify and every dict return
    #   - mobile services/api/client.ts undoes it on every
    #     response (decodeHtmlEntities)
    ############################################################

    class _EscapingJSONProvider(DefaultJSONProvider):
        ensure_ascii = False

        def dumps(self, obj, **kwargs):
            try:
                escaped = _escape_value(obj)
            except Exception:
                path = request.path if has_request_context() else "<no request>"
                logger.exception("Output escaping failed for %s — refusing to ship the raw body", path)
                raise
            return super().dumps(escaped, **kwargs)

    app.json = _EscapingJSONProvider(app)






    ############################################################
    # add_security_headers
    ############################################################
    #
    # after_request: hardening headers on EVERY response, JSON
    # and images included. The only after_request hook left —
    # escaping moved into the JSON provider. HSTS goes out
    # although this process only ever speaks plain HTTP on
    # :8000 — TLS ends at the hosting proxy in front of
    # knfapp-caddy. X-XSS-Protection is a legacy header modern
    # browsers ignore.
    #
    # Cache-Control: no-store on every /api/ answer, so an
    # invitation code, an admin user list or a chat page can no
    # longer sit in a shared browser cache. setdefault, not
    # assignment: the routes that ship their own policy keep it
    # — uploads/routes.py serve_file (max-age=86400), the news
    # feed, /api/info and /api/schedule (ETag + public
    # max-age).
    #
    # Pragma: no-cache is HTTP/1.0's blunt version of the same
    # instruction, so it rides along ONLY where the effective
    # Cache-Control really does forbid storing. Setting it on
    # every /api/ answer told an HTTP/1.0 cache the exact
    # opposite of the 6 h / 24 h public max-age the cacheable
    # reads had just asked for.
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

        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")

            # Only echo the HTTP/1.0 header when HTTP/1.1 is saying
            # "do not keep this" too — never over a route's max-age
            policy = response.headers["Cache-Control"].lower()
            if "no-store" in policy or "no-cache" in policy:
                response.headers.setdefault("Pragma", "no-cache")

        return response






    ############################################################
    # health
    ############################################################
    #
    # GET /api/health
    #
    # READINESS probe, not a liveness literal: a SELECT 1
    # through get_db() and a writability check on UPLOAD_DIR,
    # because a backend whose database file has gone read-only
    # (a full disk, a bad mount) answered "ok" all the way
    # through the outage. The two published keys are unchanged
    # — {"status", "service"} — and the per-check fields are
    # additive; a failure answers 503 with the reason.
    #
    # Used by:
    #   - nothing calls this at the moment — no compose
    #     healthcheck, nothing in the mobile app; it is only
    #     described in swagger/swagger.yaml
    ############################################################

    @app.route("/api/health")
    def health():
        # STEP 1: the database must answer a trivial query
        # ================================================
        checks = {"database": "ok", "uploads": "ok"}
        reason = None

        try:
            db = get_db()
            try:
                db.execute("SELECT 1").fetchone()
            finally:
                db.close()
        except Exception as exc:
            checks["database"] = "error"
            reason = f"database: {exc}"
            logger.exception("Health probe: database check failed")


        # STEP 2: uploads must still be writable — an avatar
        # upload is the first thing a read-only mount breaks
        # ==================================================
        upload_dir = app.config["UPLOAD_DIR"]
        if not os.access(upload_dir, os.W_OK):
            checks["uploads"] = "error"
            reason = reason or f"uploads: {upload_dir} is not writable"
            logger.warning("Health probe: %s is not writable", upload_dir)

        if reason:
            return {"status": "error", "service": "knfapp-backend", "reason": reason, **checks}, 503

        return {"status": "ok", "service": "knfapp-backend", **checks}






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
    # with a verb none of its rules accept, plus the Allow
    # header RFC 9110 makes mandatory on a 405. The verbs live
    # on Werkzeug's MethodNotAllowed — routing knows every
    # method the matching rules carry — and used to be thrown
    # away along with the exception's own response when this
    # handler rebuilt the answer from a bare dict: the client
    # learned it had used the wrong verb and nothing about
    # which one would work. A hand-rolled abort(405) names no
    # methods; then the header is left off rather than guessed.
    #
    # Used by:
    #   - Flask — @app.errorhandler(405)
    ############################################################

    @app.errorhandler(405)
    def method_not_allowed(e):
        response = jsonify({"error": "Method not allowed"})

        valid_methods = getattr(e, "valid_methods", None)
        if valid_methods:
            response.headers["Allow"] = ", ".join(valid_methods)

        return response, 405






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
    # payload_too_large
    ############################################################
    #
    # 413 → {"error": "File too large", "code":
    # "file_too_large"}: what Werkzeug raises when a request
    # body exceeds MAX_CONTENT_LENGTH (STEP 1) before any
    # handler runs — the early exit for uploads far over the
    # 5 MB cap uploads/routes.py enforces with its own 400 for
    # bodies that fit under the ceiling. The additive "code"
    # slug matches auth's pattern and the one uploads/routes.py
    # puts on its 400s, so the client can translate the failure
    # instead of showing one generic toast.
    #
    # Used by:
    #   - Flask — @app.errorhandler(413)
    ############################################################

    @app.errorhandler(413)
    def payload_too_large(e):
        return {"error": "File too large", "code": "file_too_large"}, 413






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






    ############################################################
    # database_unavailable
    ############################################################
    #
    # sqlite3.OperationalError → 503 {"error", "code":
    # "database_busy"} + Retry-After when the message is a lock
    # or busy timeout, 500 for anything else. get_db() waits
    # 30 s on a locked file (PRAGMA busy_timeout), so reaching
    # this handler means a writer held the database longer than
    # that — a retryable condition, and previously an
    # unhandled exception that surfaced as a bare 500 with no
    # hint that retrying would work.
    #
    # Used by:
    #   - Flask — @app.errorhandler(sqlite3.OperationalError),
    #     for the route bodies that do not catch it themselves
    ############################################################

    @app.errorhandler(sqlite3.OperationalError)
    def database_unavailable(e):
        message = str(e).lower()
        if "locked" in message or "busy" in message:
            logger.warning("Database busy on %s %s: %s", request.method, request.path, e)
            response = jsonify({"error": "Database busy, please retry", "code": "database_busy"})
            response.headers["Retry-After"] = "2"
            return response, 503

        logger.exception("Unhandled SQLite error on %s %s", request.method, request.path)
        return jsonify({"error": "Internal server error"}), 500

    return app
