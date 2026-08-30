# -----------------------------------------------------------
#  [*] Tests — the application factory and its middleware
#
#  Everything in app/__init__.py that every other blueprint
#  inherits: how create_app() reads its configuration, the
#  before_request gates (global throttle, JSON body shape),
#  the after_request hardening headers, the output escaping
#  that keeps stored markup out of a client's renderer, the
#  readiness probe, and the error handlers whose JSON shapes
#  the mobile client parses.
#
#  These are the seams where a regression is invisible in any
#  single blueprint's own tests but breaks every route at once.
# -----------------------------------------------------------

import importlib
import json
import os
import sqlite3

import pytest


# -----------------------------------------------------------
# fresh_app
# -----------------------------------------------------------
#
# create_app() with an environment of the caller's choosing.
# The module is re-imported so module-level state (rate-limit
# buckets above all) never leaks between cases.
#
# Used by:
#   - the configuration and throttle tests below
# -----------------------------------------------------------

def fresh_app(tmp_path, monkeypatch, **env):

    db_path = tmp_path / f"core-{len(str(tmp_path))}-{env.get('tag', 'x')}.db"
    uploads = tmp_path / "uploads"
    uploads.mkdir(exist_ok=True)

    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("SCRAPER_ENABLED", "0")
    for key, value in env.items():
        if key == "tag":
            continue
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))

    import app as app_module

    importlib.reload(app_module)
    application = app_module.create_app()
    application.config["TESTING"] = True
    return application




class TestConfiguration:

    def test_an_unset_secret_key_becomes_a_random_per_process_value(self, tmp_path, monkeypatch):
        first = fresh_app(tmp_path, monkeypatch, SECRET_KEY=None, tag="a")
        second = fresh_app(tmp_path, monkeypatch, SECRET_KEY=None, tag="b")

        # Never a literal from the repo, and never shared
        assert first.config["SECRET_KEY"]
        assert first.config["SECRET_KEY"] != second.config["SECRET_KEY"]

    def test_an_empty_secret_key_is_treated_as_unset(self, tmp_path, monkeypatch):
        application = fresh_app(tmp_path, monkeypatch, SECRET_KEY="", tag="c")
        assert application.config["SECRET_KEY"]

    def test_jwt_secret_falls_back_to_the_secret_key(self, tmp_path, monkeypatch):
        application = fresh_app(tmp_path, monkeypatch, SECRET_KEY="pinned", JWT_SECRET=None, tag="d")
        assert application.config["JWT_SECRET"] == "pinned"

    def test_the_body_cap_leaves_room_for_the_multipart_envelope(self, app):
        # uploads cap at 5 MB; Werkzeug must refuse before buffering
        assert app.config["MAX_CONTENT_LENGTH"] == 6 * 1024 * 1024

    def test_a_bad_integer_env_var_falls_back_instead_of_killing_the_boot(self, tmp_path, monkeypatch):
        application = fresh_app(tmp_path, monkeypatch, INVITATION_EXPIRY_HOURS="not-a-number", tag="e")
        assert application.config["INVITATION_EXPIRY_HOURS"] == 168

    def test_an_empty_allowed_origins_falls_back_to_the_dev_defaults(self, tmp_path, monkeypatch):
        # A set-but-empty value used to become [], which rejected
        # every browser origin and every Socket.IO handshake
        application = fresh_app(tmp_path, monkeypatch, ALLOWED_ORIGINS="   ", tag="f")
        assert application is not None




class TestSecurityHeaders:

    def test_every_response_carries_the_hardening_headers(self, client):
        response = client.get("/api/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "Content-Security-Policy" in response.headers
        assert "Strict-Transport-Security" in response.headers
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_api_responses_default_to_no_store(self, client):
        # setdefault, so a route with its OWN caching policy (the
        # news feed's ETag + max-age) keeps it; everything else
        # must not be cached, since /api/ answers carry PII
        response = client.get("/api/health")
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"

    def test_a_route_with_its_own_cache_policy_keeps_it(self, client):
        response = client.get("/api/news?page=1")
        assert "max-age" in response.headers["Cache-Control"]

    def test_headers_are_present_on_an_error_response_too(self, client):
        response = client.get("/api/definitely-not-a-route")
        assert response.status_code == 404
        assert response.headers["X-Content-Type-Options"] == "nosniff"




class TestErrorHandlers:

    def test_an_unknown_path_answers_the_json_404_shape(self, client):
        response = client.get("/api/nope")
        assert response.status_code == 404
        assert response.get_json() == {"error": "Not found"}

    def test_a_wrong_method_answers_the_json_405_shape(self, client):
        response = client.delete("/api/health")
        assert response.status_code == 405
        assert "error" in response.get_json()

    def test_an_oversized_body_answers_413_before_buffering(self, client, actor):
        _user, headers = actor
        payload = "x" * (7 * 1024 * 1024)
        response = client.post("/api/news", data=payload,
                               headers={**headers, "Content-Type": "application/json"})
        assert response.status_code == 413
        assert "error" in response.get_json()

    def test_malformed_json_is_a_400_not_a_500(self, client, actor):
        _user, headers = actor
        response = client.post("/api/news", data="{not json",
                               headers={**headers, "Content-Type": "application/json"})
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_a_json_array_body_is_refused_where_an_object_is_required(self, client, actor):
        _user, headers = actor
        response = client.post("/api/news", json=["not", "an", "object"], headers=headers)
        assert response.status_code == 400

    def test_every_error_body_is_json_never_html(self, client):
        # The mobile client parses JSON unconditionally; an HTML
        # error page reaches it as an unhelpful parse failure
        for path in ("/api/nope", "/api/news/does-not-exist"):
            response = client.get(path)
            assert response.status_code >= 400
            assert response.is_json, f"{path} answered non-JSON"




class TestOutputEscaping:

    def test_stored_markup_comes_back_escaped_exactly_once(self, client, actor):
        # Posted as RAW bytes: Flask's test client would otherwise
        # serialise `json=` through the app's own escaping provider
        # and put an already-escaped string on the wire, which no
        # real client sends — and the round trip would then look
        # double-escaped for a reason that does not exist in
        # production (migrations v1/v2 are the scar from that)
        _user, headers = actor
        client.post("/api/social/posts",
                    data=json.dumps({"content": "<script>alert(1)</script> I <3 knf"}),
                    headers={**headers, "Content-Type": "application/json"})

        body = client.get("/api/social/feed", headers=headers).get_data(as_text=True)
        assert "<script>" not in body, "raw markup reached the client"
        assert "&lt;script&gt;" in body, "content was not escaped on output"
        assert "&amp;lt;" not in body, "content was escaped twice"

    def test_the_input_hook_stores_text_verbatim(self, app, client, actor, db):
        # Escaping on INPUT double-escaped round-trip edits, so the
        # hook only strips NULs — the escape belongs on output alone
        _user, headers = actor
        client.post("/api/social/posts", data=json.dumps({"content": "I <3 knf & co"}),
                    headers={**headers, "Content-Type": "application/json"})

        stored = db.execute(
            "SELECT content FROM news_posts ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert stored == "I <3 knf & co"

    def test_non_string_values_survive_escaping_unchanged(self, client):
        payload = client.get("/api/news?page=1").get_json()
        # Counters must stay numbers and flags stay booleans — the
        # app does arithmetic and branching on them
        assert isinstance(payload["page"], int)
        assert isinstance(payload["hasMore"], bool)

    def test_a_null_byte_in_a_body_is_stripped(self, client, actor, db):
        # A NUL in text breaks SQLite comparisons and C string
        # handling downstream, so the hook strips them on input
        _user, headers = actor
        payload = json.dumps({"content": "prie\u0161" + chr(0) + " po"})
        client.post("/api/social/posts", data=payload,
                    headers={**headers, "Content-Type": "application/json"})

        stored = db.execute(
            "SELECT content FROM news_posts ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert chr(0) not in stored
