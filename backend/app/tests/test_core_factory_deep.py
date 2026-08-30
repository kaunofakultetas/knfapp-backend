# -----------------------------------------------------------
#  [*] Tests — create_app(), exhaustively
#
#  One slice of app/__init__.py, driven down every branch:
#
#    STEP 1  configuration — SECRET_KEY / JWT_SECRET fallbacks,
#            _env_int on INVITATION_EXPIRY_HOURS, the body cap,
#            the DB_PATH and UPLOAD_DIR defaults
#    STEP 2  ProxyFix (one hop, x_prefix NOT trusted), CORS
#            (/api/* only, "*" as a bare string, the
#            empty/comma-only fallback) and the Socket.IO
#            server's options
#    STEP 3  the data directories — a nested DB_PATH, a bare
#            filename with no directory part, and
#            _prepare_upload_dir's write probe and its loud
#            RuntimeError
#    STEP 4  the ten blueprints and their prefixes
#    STEP 5  the chat socket handlers
#    GET /api/health — the readiness probe: both checks, both
#            failures, the precedence between them, and the
#            connection that closes either way
#
#  Everything here boots a REAL app: the factory is the one
#  piece of the backend every other test inherits, so a
#  regression in it is invisible in any single blueprint's own
#  suite and breaks all ten at once.
#
#  NOT this file's slice (other suites own them): the
#  before_request throttle, validate_json_input, the escaping
#  JSON provider, add_security_headers and the error handlers.
#  They are touched here only where the factory's own wiring
#  is what is being proven.
# -----------------------------------------------------------

import itertools
import os
import sqlite3

import pytest
import responses
from flask import Flask, request

from app.auth.routes import _rate_limit_store


# The global per-IP budget is module state shared by every test
# in this worker process, and this file builds a lot of apps and
# fires a lot of requests through them; house pattern is to wipe
# the store around each test so no neighbour inherits our spend
@pytest.fixture(autouse=True)
def _clean_rate_limit_store():
    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()


# Every _build_app call gets its own directory under tmp_path so
# a single test can boot several apps without them sharing a
# database file or an upload directory
_SLOTS = itertools.count()




# -----------------------------------------------------------
# _build_app
# -----------------------------------------------------------
#
#   application = _build_app(tmp_path, monkeypatch,
#                            ALLOWED_ORIGINS="*")
#
# create_app() with an environment of the caller's choosing:
# the shared `app` fixture pins one configuration, and most of
# this file is about the OTHER configurations. A keyword whose
# value is None deletes the variable instead of setting it, so
# the "unset" branches are reachable.
#
# The app module is NOT reloaded — importlib.reload() would
# hand every later test a different module-level socketio
# object; create_app() is re-entrant by design and rebinds
# what it needs.
#
# A /probe/request route is added before the app has served
# anything (Flask refuses new routes afterwards), because the
# ProxyFix results — remote_addr, scheme, host, script_root —
# are otherwise invisible from outside a request.
#
# Used by:
#   - almost every test below
# -----------------------------------------------------------

def _build_app(tmp_path, monkeypatch, **env):
    import app as app_module

    slot = tmp_path / f"factory-{next(_SLOTS)}"
    slot.mkdir()

    monkeypatch.setenv("DB_PATH", str(slot / "knfapp.db"))
    monkeypatch.setenv("UPLOAD_DIR", str(slot / "uploads"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("SCRAPER_ENABLED", "0")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8081")

    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    application = app_module.create_app()
    application.config["TESTING"] = True

    @application.route("/probe/request")
    def _probe_request():
        return {
            "remote_addr": request.remote_addr or "",
            "scheme": request.scheme,
            "host": request.host,
            "script_root": request.script_root,
            "url": request.url,
        }

    return application




# -----------------------------------------------------------
# _acao
# -----------------------------------------------------------
#
# The Access-Control-Allow-Origin a given Origin gets back, or
# None when flask-cors refused it. None is exactly what a
# browser turns into a CORS error, so it is the assertion the
# CORS tests want.
#
# Used by:
#   - TestCorsPolicy
# -----------------------------------------------------------

def _acao(test_client, origin, path="/api/health"):
    response = test_client.get(path, headers={"Origin": origin})
    return response.headers.get("Access-Control-Allow-Origin")




# -----------------------------------------------------------
# _handshake
# -----------------------------------------------------------
#
# One engine.io polling handshake. threading mode ships no
# WebSocket transport, so this GET is how every real client
# opens a socket — and the only place ALLOWED_ORIGINS is
# enforced for /socket.io/*, which flask-cors never sees.
#
# Used by:
#   - TestSocketIoServer
# -----------------------------------------------------------

def _handshake(test_client, origin=None):
    headers = {"Origin": origin} if origin else {}
    return test_client.get("/socket.io/?EIO=4&transport=polling", headers=headers)




# -----------------------------------------------------------
# _FakeConnection
# -----------------------------------------------------------
#
# Stands in for the sqlite3 connection get_db() hands the
# health route, so the probe's failure arm and its
# close-in-finally can both be driven without corrupting a
# real database file. `error` is raised from execute().
#
# Used by:
#   - TestHealthRoute
# -----------------------------------------------------------

class _FakeCursor:

    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:

    def __init__(self, error=None):
        self.error = error
        self.queries = []
        self.closed = False

    def execute(self, sql, *args):
        self.queries.append(sql)
        if self.error is not None:
            raise self.error
        return _FakeCursor((1,))

    def close(self):
        self.closed = True




# -----------------------------------------------------------
# _patch_get_db
# -----------------------------------------------------------
#
# The health route calls the get_db name that app/__init__.py
# imported, and resolves it at call time — so patching the
# attribute on the app package is what the route actually
# sees. Returns nothing; the caller keeps its own handle on
# whatever it installed.
#
# Used by:
#   - TestHealthRoute
# -----------------------------------------------------------

def _patch_get_db(monkeypatch, replacement):
    import app as app_module

    monkeypatch.setattr(app_module, "get_db", replacement)




class TestSecretConfiguration:

    def test_a_provided_secret_key_is_used_verbatim(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, SECRET_KEY="pinned-secret")
        assert application.config["SECRET_KEY"] == "pinned-secret"

    def test_an_unset_secret_key_becomes_a_random_per_process_secret(self, tmp_path, monkeypatch):
        first = _build_app(tmp_path, monkeypatch, SECRET_KEY=None)
        second = _build_app(tmp_path, monkeypatch, SECRET_KEY=None)

        # 32 random bytes as hex — never a literal from this repo
        assert len(first.config["SECRET_KEY"]) == 64
        assert int(first.config["SECRET_KEY"], 16) >= 0
        assert first.config["SECRET_KEY"] != second.config["SECRET_KEY"]

    def test_an_empty_secret_key_is_treated_as_unset(self, tmp_path, monkeypatch):
        # "${SECRET_KEY}" with no .env line substitutes to ""
        application = _build_app(tmp_path, monkeypatch, SECRET_KEY="")
        assert len(application.config["SECRET_KEY"]) == 64

    def test_a_whitespace_secret_key_is_truthy_and_kept(self, tmp_path, monkeypatch):
        # The guard is `or`, not a strip(): a one-space value is a
        # bad secret but an intentional one, and it survives
        application = _build_app(tmp_path, monkeypatch, SECRET_KEY=" ")
        assert application.config["SECRET_KEY"] == " "

    def test_jwt_secret_falls_back_to_the_secret_key(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, SECRET_KEY="pinned", JWT_SECRET=None)
        assert application.config["JWT_SECRET"] == "pinned"

    def test_an_empty_jwt_secret_also_falls_back(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, SECRET_KEY="pinned", JWT_SECRET="")
        assert application.config["JWT_SECRET"] == "pinned"

    def test_a_provided_jwt_secret_is_kept_apart_from_the_secret_key(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, SECRET_KEY="one", JWT_SECRET="two")
        assert application.config["JWT_SECRET"] == "two"
        assert application.config["SECRET_KEY"] == "one"

    def test_a_random_secret_key_is_inherited_by_the_jwt_secret(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, SECRET_KEY=None, JWT_SECRET=None)
        assert application.config["JWT_SECRET"] == application.config["SECRET_KEY"]
        assert len(application.config["JWT_SECRET"]) == 64




class TestIntegerConfiguration:

    @pytest.mark.parametrize("raw, expected", [
        ("0", 0),
        ("1", 1),
        ("168", 168),
        ("-5", -5),
        ("  24  ", 24),
        ("+7", 7),
        ("3\n", 3),
        ("999999999999999999999", 999999999999999999999),
        # int() accepts any Unicode decimal digits, Arabic-Indic included
        ("١٢٣", 123),
    ])
    def test_a_numeric_expiry_is_read_as_written(self, tmp_path, monkeypatch, raw, expected):
        application = _build_app(tmp_path, monkeypatch, INVITATION_EXPIRY_HOURS=raw)
        assert application.config["INVITATION_EXPIRY_HOURS"] == expected

    @pytest.mark.parametrize("raw", [
        "not-a-number", "24.5", "0x10", "1e3", "12abc", "--3", "true", "168 hours",
    ])
    def test_a_non_integer_expiry_warns_and_keeps_the_default(self, tmp_path, monkeypatch, raw):
        # A one-character .env typo used to crash-loop the container
        # on a key nothing reads
        application = _build_app(tmp_path, monkeypatch, INVITATION_EXPIRY_HOURS=raw)
        assert application.config["INVITATION_EXPIRY_HOURS"] == 168

    @pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
    def test_a_blank_expiry_is_the_default_without_a_warning_path(self, tmp_path, monkeypatch, raw):
        application = _build_app(tmp_path, monkeypatch, INVITATION_EXPIRY_HOURS=raw)
        assert application.config["INVITATION_EXPIRY_HOURS"] == 168

    def test_an_unset_expiry_is_the_default(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, INVITATION_EXPIRY_HOURS=None)
        assert application.config["INVITATION_EXPIRY_HOURS"] == 168

    def test_the_bad_value_is_named_in_the_warning(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level("WARNING", logger="app"):
            _build_app(tmp_path, monkeypatch, INVITATION_EXPIRY_HOURS="kas-nors")

        assert any("INVITATION_EXPIRY_HOURS" in r.getMessage() and "kas-nors" in r.getMessage()
                   for r in caplog.records)




class TestBodySizeCap:

    def test_the_body_cap_leaves_room_for_the_multipart_envelope(self, tmp_path, monkeypatch):
        # uploads/routes.py caps a file at 5 MB; the extra MB is the
        # multipart envelope Werkzeug must still be able to buffer
        application = _build_app(tmp_path, monkeypatch)
        assert application.config["MAX_CONTENT_LENGTH"] == 6 * 1024 * 1024

    def test_a_body_of_exactly_the_cap_is_not_refused(self, client, app):
        cap = app.config["MAX_CONTENT_LENGTH"]
        response = client.post("/api/news", data=b"x" * cap,
                               headers={"Content-Type": "application/json"})
        assert response.status_code != 413

    def test_one_byte_over_the_cap_is_refused(self, client, app):
        cap = app.config["MAX_CONTENT_LENGTH"]
        response = client.post("/api/news", data=b"x" * (cap + 1),
                               headers={"Content-Type": "application/json"})
        assert response.status_code == 413




class TestDataDirectories:

    def test_a_nested_database_directory_is_created(self, tmp_path, monkeypatch):
        target = tmp_path / "deep" / "deeper" / "deepest" / "knfapp.db"
        application = _build_app(tmp_path, monkeypatch, DB_PATH=str(target))

        assert application.config["DB_PATH"] == str(target)
        assert target.exists()

    def test_a_bare_database_filename_boots_without_a_makedirs(self, tmp_path, monkeypatch):
        # os.path.dirname("knfapp.db") is "", and makedirs("") raises
        # FileNotFoundError — the `if db_dir:` guard is what stops it
        monkeypatch.chdir(tmp_path)
        application = _build_app(tmp_path, monkeypatch, DB_PATH="bare-name.db")

        assert application.config["DB_PATH"] == "bare-name.db"
        assert (tmp_path / "bare-name.db").exists()

    def test_the_schema_is_created_and_seeded_by_the_factory(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)

        conn = sqlite3.connect(application.config["DB_PATH"])
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'admin'").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM invitation_codes").fetchone()[0] >= 1
            assert conn.execute("SELECT COUNT(*) FROM _migrations").fetchone()[0] >= 1
        finally:
            conn.close()

    def test_booting_twice_on_one_database_seeds_once(self, tmp_path, monkeypatch):
        # init_db() runs on EVERY create_app; only a brand-new file
        # is seeded, so a restart must not plant a second admin
        shared = tmp_path / "shared.db"
        _build_app(tmp_path, monkeypatch, DB_PATH=str(shared))
        _build_app(tmp_path, monkeypatch, DB_PATH=str(shared))

        conn = sqlite3.connect(str(shared))
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'admin'").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM invitation_codes").fetchone()[0] == 1
        finally:
            conn.close()

    def test_the_second_boot_on_one_database_still_serves(self, tmp_path, monkeypatch):
        shared = tmp_path / "shared.db"
        _build_app(tmp_path, monkeypatch, DB_PATH=str(shared))
        second = _build_app(tmp_path, monkeypatch, DB_PATH=str(shared))

        assert second.test_client().get("/api/health").status_code == 200

    def test_the_dev_defaults_point_beside_the_package(self, tmp_path, monkeypatch):
        # Booting on the in-image defaults would write into the repo,
        # so the two side effects are stubbed and only the resolved
        # paths are asserted
        import app as app_module

        seen = []
        monkeypatch.setattr(app_module, "init_db", lambda path: seen.append(path))
        monkeypatch.setattr(app_module, "_prepare_upload_dir", os.path.abspath)

        application = _build_app(tmp_path, monkeypatch, DB_PATH=None, UPLOAD_DIR=None)

        package_data = os.path.abspath(
            os.path.join(os.path.dirname(app_module.__file__), "..", "data"))
        assert os.path.abspath(application.config["DB_PATH"]) == os.path.join(package_data, "knfapp.db")
        assert application.config["UPLOAD_DIR"] == os.path.join(package_data, "uploads")
        assert seen == [application.config["DB_PATH"]]




class TestUploadDirectory:

    def test_the_upload_directory_is_created_when_missing(self, tmp_path, monkeypatch):
        target = tmp_path / "brand" / "new" / "uploads"
        application = _build_app(tmp_path, monkeypatch, UPLOAD_DIR=str(target))

        assert os.path.isdir(str(target))
        assert application.config["UPLOAD_DIR"] == str(target)

    def test_the_config_holds_the_absolute_path(self, tmp_path, monkeypatch):
        # uploads/routes.py trusts this value and never resolves it
        # again, so a relative UPLOAD_DIR must be absolute by now
        monkeypatch.chdir(tmp_path)
        application = _build_app(tmp_path, monkeypatch, UPLOAD_DIR="relative-uploads")

        assert os.path.isabs(application.config["UPLOAD_DIR"])
        assert application.config["UPLOAD_DIR"] == os.path.join(os.getcwd(), "relative-uploads")
        assert os.path.isdir(application.config["UPLOAD_DIR"])

    def test_a_dotted_path_is_normalised(self, tmp_path, monkeypatch):
        (tmp_path / "one").mkdir()
        target = tmp_path / "one" / ".." / "two"
        application = _build_app(tmp_path, monkeypatch, UPLOAD_DIR=str(target))

        assert application.config["UPLOAD_DIR"] == str(tmp_path / "two")

    def test_an_empty_upload_dir_resolves_to_the_working_directory(self, tmp_path, monkeypatch):
        # Deliberate asymmetry with SECRET_KEY/ALLOWED_ORIGINS: those
        # use `or`, this one uses a default argument, so a set-but-
        # empty value is a real (empty) path. abspath("") is the cwd,
        # which under the read_only container fails the boot loudly
        monkeypatch.chdir(tmp_path)
        application = _build_app(tmp_path, monkeypatch, UPLOAD_DIR="")

        assert application.config["UPLOAD_DIR"] == os.getcwd()

    def test_the_write_probe_leaves_nothing_behind(self, tmp_path, monkeypatch):
        target = tmp_path / "probe-check"
        _build_app(tmp_path, monkeypatch, UPLOAD_DIR=str(target))

        assert os.listdir(str(target)) == []

    def test_an_existing_upload_directory_keeps_its_files(self, tmp_path, monkeypatch):
        target = tmp_path / "already-there"
        target.mkdir()
        (target / "avatar.png").write_bytes(b"not-really-a-png")

        _build_app(tmp_path, monkeypatch, UPLOAD_DIR=str(target))

        assert (target / "avatar.png").read_bytes() == b"not-really-a-png"

    def test_an_unusable_upload_dir_fails_the_boot_naming_the_variable(self, tmp_path, monkeypatch):
        # The parent is a FILE, so makedirs cannot make the directory
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file")

        with pytest.raises(RuntimeError) as exc_info:
            _build_app(tmp_path, monkeypatch, UPLOAD_DIR=str(blocker / "uploads"))

        assert "UPLOAD_DIR" in str(exc_info.value)
        assert str(blocker / "uploads") in str(exc_info.value)

    def test_an_upload_dir_that_is_a_file_fails_the_boot(self, tmp_path, monkeypatch):
        blocker = tmp_path / "regular-file"
        blocker.write_text("i am a file")

        with pytest.raises(RuntimeError) as exc_info:
            _build_app(tmp_path, monkeypatch, UPLOAD_DIR=str(blocker))

        assert "UPLOAD_DIR" in str(exc_info.value)

    def test_the_underlying_os_error_is_kept_as_the_cause(self, tmp_path, monkeypatch):
        # `raise ... from exc` — the operator needs the errno, not
        # just the app's own sentence
        blocker = tmp_path / "blocker-two"
        blocker.write_text("x")

        with pytest.raises(RuntimeError) as exc_info:
            _build_app(tmp_path, monkeypatch, UPLOAD_DIR=str(blocker / "nope"))

        assert isinstance(exc_info.value.__cause__, OSError)




class TestProxyFix:

    def test_one_forwarded_hop_becomes_the_remote_address(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get(
            "/probe/request", headers={"X-Forwarded-For": "203.0.113.9"}).get_json()

        assert body["remote_addr"] == "203.0.113.9"

    def test_only_the_last_hop_of_a_chain_is_trusted(self, tmp_path, monkeypatch):
        # x_for=1: the ingress appends the real peer, so the RIGHTMOST
        # entry is the one Caddy wrote and the rest are client-supplied
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get(
            "/probe/request",
            headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2, 198.51.100.7"}).get_json()

        assert body["remote_addr"] == "198.51.100.7"

    def test_a_padded_forwarded_address_is_trimmed(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get(
            "/probe/request", headers={"X-Forwarded-For": "   198.51.100.8   "}).get_json()

        assert body["remote_addr"] == "198.51.100.8"

    def test_an_empty_forwarded_header_leaves_the_peer_address(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get(
            "/probe/request", headers={"X-Forwarded-For": ""}).get_json()

        assert body["remote_addr"] == "127.0.0.1"

    def test_without_forwarded_headers_nothing_is_rewritten(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get("/probe/request").get_json()

        assert body["remote_addr"] == "127.0.0.1"
        assert body["scheme"] == "http"
        assert body["host"] == "localhost"

    def test_the_forwarded_scheme_is_honoured(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get(
            "/probe/request", headers={"X-Forwarded-Proto": "https"}).get_json()

        assert body["scheme"] == "https"
        assert body["url"].startswith("https://")

    def test_the_forwarded_host_is_honoured(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get(
            "/probe/request", headers={"X-Forwarded-Host": "knfapp.knf-hosting.lt"}).get_json()

        assert body["host"] == "knfapp.knf-hosting.lt"
        assert body["url"] == "http://knfapp.knf-hosting.lt/probe/request"

    def test_a_client_supplied_prefix_is_ignored(self, tmp_path, monkeypatch):
        # x_prefix is NOT trusted: the ingress never sends one, so
        # honouring it would only let a caller rewrite url_for()
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get(
            "/probe/request",
            headers={"X-Forwarded-Prefix": "/evil"}).get_json()

        assert body["script_root"] == ""
        assert body["url"].endswith("/probe/request")

    def test_all_three_trusted_headers_apply_together(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        body = application.test_client().get("/probe/request", headers={
            "X-Forwarded-For": "198.51.100.4",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "knfapp.knf-hosting.lt",
            "X-Forwarded-Prefix": "/nope",
        }).get_json()

        assert body == {
            "remote_addr": "198.51.100.4",
            "scheme": "https",
            "host": "knfapp.knf-hosting.lt",
            "script_root": "",
            "url": "https://knfapp.knf-hosting.lt/probe/request",
        }




class TestCorsPolicy:

    def test_a_listed_origin_is_allowed(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="http://localhost:8081")
        assert _acao(application.test_client(), "http://localhost:8081") == "http://localhost:8081"

    def test_an_unlisted_origin_gets_no_allow_header(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="http://localhost:8081")
        assert _acao(application.test_client(), "http://evil.example") is None

    def test_the_answer_varies_on_origin(self, tmp_path, monkeypatch):
        # Without Vary a shared cache would serve one origin's
        # allow-header to the next origin. flask-cors only needs it
        # once the list holds more than one origin — which is exactly
        # what production compose configures
        application = _build_app(tmp_path, monkeypatch,
                                 ALLOWED_ORIGINS="http://a.example,http://b.example")
        response = application.test_client().get(
            "/api/health", headers={"Origin": "http://a.example"})

        assert "Origin" in response.headers.get("Vary", "")

    def test_a_preflight_is_answered_with_the_allowed_methods(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        response = application.test_client().open("/api/auth/login", method="OPTIONS", headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "POST",
        })

        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:8081"
        assert "POST" in response.headers["Access-Control-Allow-Methods"]

    def test_a_whitespace_padded_list_is_trimmed_entry_by_entry(self, tmp_path, monkeypatch):
        application = _build_app(
            tmp_path, monkeypatch,
            ALLOWED_ORIGINS="  http://a.example ,http://b.example  ,")
        test_client = application.test_client()

        assert _acao(test_client, "http://a.example") == "http://a.example"
        assert _acao(test_client, "http://b.example") == "http://b.example"
        assert _acao(test_client, "http://c.example") is None

    def test_a_duplicated_origin_is_harmless(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch,
                                 ALLOWED_ORIGINS="http://dup.example,http://dup.example")
        assert _acao(application.test_client(), "http://dup.example") == "http://dup.example"

    def test_a_bare_star_admits_every_origin(self, tmp_path, monkeypatch):
        # "*" must stay the STRING flask-cors understands as a
        # wildcard, not be split into a one-entry list
        application = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="*")
        allowed = _acao(application.test_client(), "http://anything.example")

        assert allowed in ("*", "http://anything.example")

    def test_a_star_inside_a_list_still_admits_every_origin(self, tmp_path, monkeypatch):
        # "*, http://a" is NOT the bare string, so it becomes a list —
        # and flask-cors treats a "*" entry as the wildcard anyway
        application = _build_app(tmp_path, monkeypatch,
                                 ALLOWED_ORIGINS="*, http://a.example")
        allowed = _acao(application.test_client(), "http://zzz.example")

        assert allowed in ("*", "http://zzz.example")

    @pytest.mark.parametrize("raw", ["", "   ", ",", ",,,", " , , "])
    def test_an_originless_value_falls_back_to_the_dev_defaults(self, tmp_path, monkeypatch, raw):
        # A set-but-empty ALLOWED_ORIGINS used to become [], which
        # silently rejected every browser origin AND every socket
        application = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS=raw)
        test_client = application.test_client()

        assert _acao(test_client, "http://localhost:8081") == "http://localhost:8081"
        assert _acao(test_client, "http://localhost:8083") == "http://localhost:8083"
        assert _acao(test_client, "http://evil.example") is None

    def test_an_unset_allowed_origins_falls_back_to_the_dev_defaults(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS=None)
        test_client = application.test_client()

        assert _acao(test_client, "http://localhost:8081") == "http://localhost:8081"
        assert _acao(test_client, "http://localhost:8083") == "http://localhost:8083"

    def test_the_comma_only_fallback_is_logged(self, tmp_path, monkeypatch, caplog):
        with caplog.at_level("WARNING", logger="app"):
            _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS=" , , ")

        assert any("ALLOWED_ORIGINS" in r.getMessage() for r in caplog.records)

    def test_cors_covers_the_api_only(self, tmp_path, monkeypatch):
        # resources={r"/api/*": ...} — a browser has no business
        # reading anything else off this origin
        application = _build_app(tmp_path, monkeypatch)
        test_client = application.test_client()

        assert _acao(test_client, "http://localhost:8081", "/api/health") == "http://localhost:8081"
        assert _acao(test_client, "http://localhost:8081", "/probe/request") is None
        assert _acao(test_client, "http://localhost:8081", "/") is None

    def test_an_api_error_still_carries_the_allow_header(self, tmp_path, monkeypatch):
        # Without it the browser reports a CORS failure instead of
        # the 404 the mobile client wants to read
        application = _build_app(tmp_path, monkeypatch)
        assert _acao(application.test_client(), "http://localhost:8081",
                     "/api/definitely-not-a-route") == "http://localhost:8081"

    def test_each_app_keeps_its_own_origin_list(self, tmp_path, monkeypatch):
        # create_app() is called once per process in production, but
        # a factory that leaked its last configuration into an
        # earlier app would make every test here meaningless
        first = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="http://first.example")
        second = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="http://second.example")

        assert _acao(first.test_client(), "http://first.example") == "http://first.example"
        assert _acao(first.test_client(), "http://second.example") is None
        assert _acao(second.test_client(), "http://second.example") == "http://second.example"




class TestSocketIoServer:

    def test_the_server_is_bound_to_the_app(self, tmp_path, monkeypatch):
        import app as app_module

        application = _build_app(tmp_path, monkeypatch)
        assert application.extensions["socketio"] is app_module.socketio
        assert app_module.socketio.server is not None

    def test_the_transport_is_threading_only(self, tmp_path, monkeypatch):
        # No simple-websocket in requirements.txt, so every client
        # long-polls; async_mode is what decides that
        import app as app_module

        _build_app(tmp_path, monkeypatch)
        assert app_module.socketio.server.async_mode == "threading"

    def test_handlers_are_not_run_on_their_own_threads(self, tmp_path, monkeypatch):
        # async_handlers=False keeps a client's events on its own
        # packet thread, which is what makes chat's rate limiter
        # real back-pressure instead of a thread-per-event fan-out
        import app as app_module

        _build_app(tmp_path, monkeypatch)
        assert app_module.socketio.server.async_handlers is False

    def test_one_long_poll_payload_is_capped(self, tmp_path, monkeypatch):
        import app as app_module

        _build_app(tmp_path, monkeypatch)
        assert app_module.socketio.server.eio.max_http_buffer_size == 256 * 1024
        assert app_module.socketio.server.eio.max_http_buffer_size == app_module._SOCKET_MAX_HTTP_BUFFER

    def test_the_socket_server_gets_the_same_origin_list_as_cors(self, tmp_path, monkeypatch):
        import app as app_module

        _build_app(tmp_path, monkeypatch,
                   ALLOWED_ORIGINS="http://a.example, http://b.example")
        assert app_module.socketio.server.eio.cors_allowed_origins == [
            "http://a.example", "http://b.example"]

    def test_the_socket_server_gets_the_dev_defaults_too(self, tmp_path, monkeypatch):
        import app as app_module

        _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="   ")
        assert app_module.socketio.server.eio.cors_allowed_origins == [
            "http://localhost:8081", "http://localhost:8083"]

    def test_a_bare_star_reaches_the_socket_server_as_a_string(self, tmp_path, monkeypatch):
        import app as app_module

        _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="*")
        assert app_module.socketio.server.eio.cors_allowed_origins == "*"

    def test_a_listed_origin_completes_the_handshake(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="http://localhost:8081")
        response = _handshake(application.test_client(), "http://localhost:8081")

        assert response.status_code == 200
        # engine.io OPEN packet, "0" followed by the session JSON
        assert response.get_data().startswith(b'0{"sid"')

    def test_an_unlisted_origin_is_refused_at_the_handshake(self, tmp_path, monkeypatch):
        # flask-cors never sees /socket.io/*, so this is the only
        # place the origin list is enforced for the socket
        application = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="http://localhost:8081")
        response = _handshake(application.test_client(), "http://evil.example")

        assert response.status_code == 400

    def test_a_handshake_without_an_origin_is_allowed(self, tmp_path, monkeypatch):
        # A native mobile client sends no Origin at all
        application = _build_app(tmp_path, monkeypatch)
        assert _handshake(application.test_client()).status_code == 200

    def test_a_star_origin_list_admits_any_handshake(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="*")
        assert _handshake(application.test_client(), "http://anything.example").status_code == 200

    def test_the_socket_endpoint_answers_above_flasks_dispatch(self, tmp_path, monkeypatch):
        # engineio's middleware answers /socket.io/* before Flask
        # runs, so the after_request hardening headers are absent
        # there — chat/events.py has to do its own policing
        application = _build_app(tmp_path, monkeypatch)
        response = _handshake(application.test_client(), "http://localhost:8081")

        assert "X-Frame-Options" not in response.headers
        assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:8081"

    def test_each_app_keeps_its_own_socket_origin_policy(self, tmp_path, monkeypatch):
        first = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="http://first.example")
        second = _build_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="http://second.example")

        assert _handshake(first.test_client(), "http://first.example").status_code == 200
        assert _handshake(first.test_client(), "http://second.example").status_code == 400
        assert _handshake(second.test_client(), "http://second.example").status_code == 200

    def test_the_chat_events_are_registered_on_the_default_namespace(self, tmp_path, monkeypatch):
        import app as app_module

        _build_app(tmp_path, monkeypatch)
        registered = set(app_module.socketio.server.handlers.get("/", {}))

        assert registered == {"connect", "disconnect", "join_conversation",
                              "leave_conversation", "typing", "stop_typing", "mark_read"}

    def test_the_namespace_wide_error_handler_is_registered(self, tmp_path, monkeypatch):
        import app as app_module

        _build_app(tmp_path, monkeypatch)
        assert app_module.socketio.default_exception_handler is not None

    def test_the_handlers_survive_a_second_factory_call(self, tmp_path, monkeypatch):
        import app as app_module

        _build_app(tmp_path, monkeypatch)
        _build_app(tmp_path, monkeypatch)

        assert "connect" in app_module.socketio.server.handlers.get("/", {})




class TestBlueprintRegistration:

    def test_exactly_the_ten_documented_blueprints_are_registered(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        assert set(application.blueprints) == {
            "auth", "news", "schedule", "admin", "scraper",
            "chat", "social", "uploads", "info", "notifications",
        }

    @pytest.mark.parametrize("name", [
        "auth", "news", "schedule", "admin", "scraper",
        "chat", "social", "uploads", "info", "notifications",
    ])
    def test_every_blueprint_sits_under_its_own_api_prefix(self, tmp_path, monkeypatch, name):
        application = _build_app(tmp_path, monkeypatch)
        rules = [r for r in application.url_map.iter_rules()
                 if r.endpoint.startswith(f"{name}.")]

        assert rules, f"{name} registered no routes"
        for rule in rules:
            assert rule.rule == f"/api/{name}" or rule.rule.startswith(f"/api/{name}/"), \
                f"{rule.rule} is not under /api/{name}"

    def test_health_is_the_only_route_the_factory_defines_itself(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        own = {r.endpoint for r in application.url_map.iter_rules()
               if r.rule.startswith("/api/") and "." not in r.endpoint}

        assert own == {"health"}

    @pytest.mark.parametrize("path", [
        "/api/auth/me", "/api/news", "/api/schedule", "/api/admin/users",
        "/api/scraper/status", "/api/chat/conversations", "/api/social/feed",
        "/api/info", "/api/notifications/channels",
    ])
    def test_a_route_from_each_blueprint_is_actually_mounted(self, tmp_path, monkeypatch, path):
        # 401/403 is a mounted route refusing a guest; 404 would mean
        # the blueprint never made it onto the URL map
        application = _build_app(tmp_path, monkeypatch)
        assert application.test_client().get(path).status_code != 404

    @pytest.mark.parametrize("path", ["/auth/me", "/api/me", "/news", "/api/login", "/login"])
    def test_nothing_is_reachable_off_its_prefix(self, tmp_path, monkeypatch, path):
        application = _build_app(tmp_path, monkeypatch)
        assert application.test_client().get(path).status_code == 404

    def test_the_factory_returns_a_fresh_flask_app_each_call(self, tmp_path, monkeypatch):
        first = _build_app(tmp_path, monkeypatch)
        second = _build_app(tmp_path, monkeypatch)

        assert isinstance(first, Flask) and isinstance(second, Flask)
        assert first is not second
        first.config["SOME_MARKER"] = "only-here"
        assert "SOME_MARKER" not in second.config

    def test_the_factory_leaves_debug_off(self, tmp_path, monkeypatch):
        # APP_DEBUG is main.py's business; a factory that turned the
        # debugger on would ship tracebacks to every client
        application = _build_app(tmp_path, monkeypatch)
        assert application.debug is False

    def test_building_an_app_starts_no_scraper_and_makes_no_request(self, tmp_path, monkeypatch):
        # Merely building an app used to hit knf.vu.lt and push real
        # notifications within two seconds; main.py owns the
        # scheduler now, and only in its --http branch
        from app.scraper import scheduler

        with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
            _build_app(tmp_path, monkeypatch, SCRAPER_ENABLED="1")
            assert len(mocked.calls) == 0

        assert scheduler._scheduler is None




class TestHealthRoute:

    def test_a_healthy_backend_answers_the_published_shape(self, client):
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.is_json
        assert response.get_json() == {
            "status": "ok",
            "service": "knfapp-backend",
            "database": "ok",
            "uploads": "ok",
        }

    def test_the_probe_needs_no_login(self, client):
        # The app must work without login, and a readiness probe
        # that needed a token could never be wired to a healthcheck
        assert client.get("/api/health").status_code == 200

    def test_the_probe_answers_on_a_freshly_built_app(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        assert application.test_client().get("/api/health").status_code == 200

    def test_the_probe_really_queries_the_database(self, client, monkeypatch):
        connection = _FakeConnection()
        _patch_get_db(monkeypatch, lambda: connection)

        assert client.get("/api/health").status_code == 200
        assert connection.queries == ["SELECT 1"]

    def test_the_connection_is_closed_on_the_happy_path(self, client, monkeypatch):
        connection = _FakeConnection()
        _patch_get_db(monkeypatch, lambda: connection)

        client.get("/api/health")
        assert connection.closed is True

    def test_the_connection_is_closed_when_the_query_fails(self, client, monkeypatch):
        # The close() lives in a finally — a probe that leaked a
        # handle per poll would eventually be the outage
        connection = _FakeConnection(error=sqlite3.OperationalError("no such table: users"))
        _patch_get_db(monkeypatch, lambda: connection)

        assert client.get("/api/health").status_code == 503
        assert connection.closed is True

    def test_an_unopenable_database_answers_503_with_a_reason(self, client, monkeypatch):
        def _refuse():
            raise sqlite3.OperationalError("unable to open database file")

        _patch_get_db(monkeypatch, _refuse)
        response = client.get("/api/health")
        body = response.get_json()

        assert response.status_code == 503
        assert body["status"] == "error"
        assert body["service"] == "knfapp-backend"
        assert body["database"] == "error"
        assert body["uploads"] == "ok"
        assert body["reason"].startswith("database: ")
        assert "unable to open database file" in body["reason"]

    def test_a_database_error_is_handled_inside_the_probe(self, client, monkeypatch):
        # sqlite3.OperationalError has an app-wide error handler that
        # answers 503 {"code": "database_busy"}; the probe catches
        # its own first, so the reason survives
        def _locked():
            raise sqlite3.OperationalError("database is locked")

        _patch_get_db(monkeypatch, _locked)
        body = client.get("/api/health").get_json()

        assert body.get("code") != "database_busy"
        assert body["reason"] == "database: database is locked"

    def test_any_exception_from_the_database_is_caught(self, client, monkeypatch):
        # `except Exception`, not `except sqlite3.Error`: get_db()
        # raises RuntimeError when init_db never ran
        def _never_initialised():
            raise RuntimeError("init_db() has not been called")

        _patch_get_db(monkeypatch, _never_initialised)
        response = client.get("/api/health")

        assert response.status_code == 503
        assert response.get_json()["reason"] == "database: init_db() has not been called"

    def test_the_failure_reason_is_escaped_like_every_other_body(self, client, monkeypatch):
        # The reason carries a driver message; it goes out through
        # the same escaping provider as the rest of the API
        def _markup():
            raise RuntimeError("no <b>db</b>")

        _patch_get_db(monkeypatch, _markup)
        raw = client.get("/api/health").get_data(as_text=True)

        assert "<b>" not in raw
        assert "&lt;b&gt;" in raw

    def test_a_vanished_upload_directory_is_a_503(self, client, app, tmp_path):
        # An avatar upload is the first thing a read-only mount
        # breaks, and the probe used to answer "ok" right through it
        missing = str(tmp_path / "not-there")
        app.config["UPLOAD_DIR"] = missing

        response = client.get("/api/health")
        body = response.get_json()

        assert response.status_code == 503
        assert body["status"] == "error"
        assert body["database"] == "ok"
        assert body["uploads"] == "error"
        assert body["reason"] == f"uploads: {missing} is not writable"

    def test_an_empty_upload_path_is_a_503(self, client, app):
        app.config["UPLOAD_DIR"] = ""
        response = client.get("/api/health")

        assert response.status_code == 503
        assert response.get_json()["uploads"] == "error"

    def test_the_database_reason_wins_when_both_checks_fail(self, client, app, monkeypatch, tmp_path):
        # `reason = reason or ...` — the database is reported first
        # because it is the check that ran first
        def _refuse():
            raise RuntimeError("dingo")

        _patch_get_db(monkeypatch, _refuse)
        app.config["UPLOAD_DIR"] = str(tmp_path / "gone")

        body = client.get("/api/health").get_json()

        assert body["database"] == "error"
        assert body["uploads"] == "error"
        assert body["reason"] == "database: dingo"

    def test_the_probe_recovers_once_the_directory_is_back(self, client, app, tmp_path):
        restored = tmp_path / "restored"
        app.config["UPLOAD_DIR"] = str(restored)
        assert client.get("/api/health").status_code == 503

        restored.mkdir()
        assert client.get("/api/health").status_code == 200

    def test_the_probe_is_a_get_only_route(self, tmp_path, monkeypatch):
        application = _build_app(tmp_path, monkeypatch)
        rule = next(r for r in application.url_map.iter_rules() if r.endpoint == "health")

        assert rule.rule == "/api/health"
        assert rule.methods == {"GET", "HEAD", "OPTIONS"}

    def test_a_head_probe_answers_without_a_body(self, client):
        response = client.head("/api/health")

        assert response.status_code == 200
        assert response.get_data() == b""

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_a_write_verb_on_the_probe_is_refused(self, client, method):
        response = client.open("/api/health", method=method)

        assert response.status_code == 405
        assert response.get_json() == {"error": "Method not allowed"}
