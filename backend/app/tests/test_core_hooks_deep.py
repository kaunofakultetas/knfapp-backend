# -----------------------------------------------------------
#  [*] Tests — the factory's request/response hooks, exhaustive
#
#  The gap-closing pass over the seven things in
#  app/__init__.py that EVERY blueprint inherits, and nothing
#  else:
#
#    throttle_requests, validate_json_input,
#    _validate_avatar_url, _escape_value,
#    _EscapingJSONProvider, add_security_headers,
#    and the seven error handlers (400, 404, 405, 413, 415,
#    500, sqlite3.OperationalError)
#
#  The broad suite proves these work; this module goes after
#  every arm, guard and boundary they own:
#
#    - throttle_requests: the disabled budget (0 and negative),
#      the OPTIONS exemption, the "unknown" key an absent
#      REMOTE_ADDR falls back to, the ProxyFix-resolved address
#      the key is really built from, per-IP isolation, the
#      budget shared across routes, the exact max/max+1
#      boundary, the Retry-After it hands back, and the fact
#      that it runs BEFORE any body is parsed
#    - validate_json_input: every content-type shape that opts
#      in or out, every non-object top-level body (and the
#      "null" body that is NOT one), the NUL strip landing in
#      BOTH get_json() cache slots, the untouched body when
#      nothing changed, the 32/33 nesting boundary both ways,
#      and the RecursionError a 100 000-deep body raises inside
#      json.loads
#    - _validate_avatar_url: None/"" clearing, every wrong
#      type, the 2048/2049 length boundary, both key
#      spellings, every extension the regex admits and the
#      traversal / query-string / absolute-URL / trailing-
#      newline payloads it must not
#    - _escape_value + the JSON provider: quote=True, keys left
#      alone, tuples normalised to lists, non-strings
#      untouched, ensure_ascii=False, the depth cap, and the
#      fail-CLOSED contract (a body that cannot be escaped
#      becomes a 500, never a raw body)
#    - add_security_headers: the six headers on every status,
#      the /api/ Cache-Control default, the setdefault that
#      lets a route keep its own policy, and the non-/api path
#      that gets no cache header at all
#    - the error handlers: the JSON shape of each, including
#      the 413 "code" slug and the 503/500 split
#      database_unavailable makes on the message text
# -----------------------------------------------------------

import json
import logging
import sqlite3
import time
import uuid

import pytest
import time_machine
from flask import jsonify, request

import app as app_package
from app.auth.routes import _RATE_LIMIT_WINDOW, _rate_limit_store


# The shape uploads/routes.py hands out: uuid4().hex + one of
# its allowed extensions
_VALID_AVATAR = "/api/uploads/" + "0" * 32 + ".jpg"

# "/api/uploads/" is 13 characters; _AVATAR_URL_MAX_LENGTH is
# 2048, so this pads a URL to exactly the ceiling
_PREFIX_LEN = len("/api/uploads/")

_TOO_LONG_MESSAGE = "avatar_url must be at most 2048 characters"
_NOT_A_STRING_MESSAGE = "avatar_url must be a string"
_BAD_SHAPE_MESSAGE = "avatar_url must be a relative /api/uploads/ path"

# str() of the sqlite3.OperationalError each probe flavour
# raises — the handler branches on this text alone
_SQLITE_MESSAGES = {
    "locked": "database is locked",
    "busy": "database table is busy",
    "shouty": "Database Is LOCKED",
    "shouty-busy": "The Database Is BUSY",
    "other": "no such table: users",
    "empty": "",
}




# -----------------------------------------------------------
# _reset_rate_limits
# -----------------------------------------------------------
#
# The limiter store is ONE module-level dict for the whole
# process, so a test that spends a budget would otherwise
# poison every test after it in the same xdist worker.
#
# Used by:
#   - every test in this module (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_rate_limits():
    _rate_limit_store.clear()
    yield
    _rate_limit_store.clear()




# -----------------------------------------------------------
# build_app
# -----------------------------------------------------------
#
# create_app() on a throwaway database with an environment of
# the caller's choosing. Deliberately NOT importlib.reload'ing
# the package: the module-level _JsonTooDeep this file asserts
# on has to stay the same class object the factory raises.
#
# Used by:
#   - probe_app (below)
# -----------------------------------------------------------

def build_app(tmp_path, monkeypatch, **env):
    tag = uuid.uuid4().hex[:8]
    uploads = tmp_path / f"uploads-{tag}"
    uploads.mkdir()

    monkeypatch.setenv("DB_PATH", str(tmp_path / f"hooks-{tag}.db"))
    monkeypatch.setenv("UPLOAD_DIR", str(uploads))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("SCRAPER_ENABLED", "0")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8081")

    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))

    application = app_package.create_app()
    application.config["TESTING"] = True
    # The error handlers under test — not the debugger — must be
    # what answers an unhandled exception
    application.config["PROPAGATE_EXCEPTIONS"] = False
    return application




# -----------------------------------------------------------
# probe_app
# -----------------------------------------------------------
#
# A real app plus a handful of routes that exist only to make
# a hook's effect observable: an echo that reads the body back
# through BOTH get_json() flavours, a response with its own
# caching policy, and four ways to fail. Registered before the
# first request, so Flask still accepts them.
#
# Every probe lives under /api/ except _probe_plain, which is
# the one path add_security_headers must NOT put a
# Cache-Control on.
#
# Used by:
#   - make_probe / probe_application / probe_client (below)
# -----------------------------------------------------------

def probe_app(tmp_path, monkeypatch, **env):
    application = build_app(tmp_path, monkeypatch, **env)

    @application.route("/api/_probe/echo", methods=["POST", "PUT", "PATCH"])
    def _probe_echo():
        return {"strict": request.get_json(), "silent": request.get_json(silent=True)}

    @application.route("/api/_probe/ok", methods=["GET", "POST"])
    def _probe_ok():
        return {"ok": True}

    @application.route("/api/_probe/cached")
    def _probe_cached():
        response = jsonify({"ok": True})
        response.headers["Cache-Control"] = "public, max-age=60"
        return response

    @application.route("/api/_probe/pragma")
    def _probe_pragma():
        response = jsonify({"ok": True})
        response.headers["Pragma"] = "knfapp-own-token"
        return response

    @application.route("/api/_probe/deep")
    def _probe_deep():
        return jsonify(nested_object(app_package._MAX_JSON_DEPTH + 8))

    @application.route("/api/_probe/boom")
    def _probe_boom():
        raise ValueError("probe explosion")

    @application.route("/api/_probe/sqlite/<flavour>")
    def _probe_sqlite(flavour):
        raise sqlite3.OperationalError(_SQLITE_MESSAGES[flavour])

    @application.route("/_probe/plain")
    def _probe_plain():
        return {"ok": True}

    return application




@pytest.fixture
def make_probe(tmp_path, monkeypatch):

    def _make(**env):
        return probe_app(tmp_path, monkeypatch, **env)

    return _make


@pytest.fixture
def probe_application(make_probe):
    return make_probe()


@pytest.fixture
def probe_client(probe_application):
    return probe_application.test_client()




# -----------------------------------------------------------
# post_raw
# -----------------------------------------------------------
#
# TESTPLAN rule 10: Flask's `json=` kwarg serialises through
# the app's OWN escaping provider, so an assertion about what
# is on the wire is worthless unless the bytes are built here.
# content_type=None sends no Content-Type header at all.
#
# Used by:
#   - every body test below
# -----------------------------------------------------------

def post_raw(client, path, payload, content_type="application/json",
             method="POST", headers=None, **kwargs):
    body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    hdrs = dict(headers or {})
    if content_type is not None:
        hdrs["Content-Type"] = content_type
    return client.open(path, method=method, data=body, headers=hdrs, **kwargs)




# -----------------------------------------------------------
# nested_object / nested_array
# -----------------------------------------------------------
#
# `levels` containers deep, counted the way both walkers count:
# nested_object(1) is {"a": "x"}, one container. The cap
# refuses a container whose PARENT is already at
# _MAX_JSON_DEPTH, so 32 levels pass and 33 do not.
#
# Used by:
#   - the depth-boundary tests below
# -----------------------------------------------------------

def nested_object(levels, leaf="x"):
    node = leaf
    for _ in range(levels):
        node = {"a": node}
    return node


def nested_array(levels, leaf="x"):
    node = leaf
    for _ in range(levels):
        node = [node]
    return node




# -----------------------------------------------------------
# spend_global_budget
# -----------------------------------------------------------
#
# Fills one client's global bucket directly instead of firing
# hundreds of requests: the stamps are the monotonic clock the
# limiter reads, so the window prunes exactly as it would have.
#
# Used by:
#   - the throttle tests below
# -----------------------------------------------------------

def spend_global_budget(remote_addr="127.0.0.1", stamps=64):
    now = time.monotonic()
    _rate_limit_store[f"global:{remote_addr}"] = [now] * stamps




class TestThrottleRequests:

    def test_a_request_under_the_budget_is_let_through(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=5).test_client()
        assert client.get("/api/_probe/ok").status_code == 200

    def test_the_last_request_inside_the_budget_still_passes(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=3).test_client()
        statuses = [client.get("/api/_probe/ok").status_code for _ in range(3)]
        assert statuses == [200, 200, 200]

    def test_one_request_past_the_budget_is_refused(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=3).test_client()
        for _ in range(3):
            client.get("/api/_probe/ok")

        response = client.get("/api/_probe/ok")
        assert response.status_code == 429

    def test_a_budget_of_one_allows_exactly_one_request(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        assert client.get("/api/_probe/ok").status_code == 200
        assert client.get("/api/_probe/ok").status_code == 429

    def test_the_refusal_carries_the_house_429_body(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        client.get("/api/_probe/ok")

        body = client.get("/api/_probe/ok").get_json()
        assert body["code"] == "rate_limited"
        assert "Too many requests" in body["error"]

    def test_the_refusal_carries_a_usable_retry_after(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        client.get("/api/_probe/ok")

        response = client.get("/api/_probe/ok")
        retry_after = int(response.headers["Retry-After"])
        assert 1 <= retry_after <= _RATE_LIMIT_WINDOW + 1

    def test_the_refusal_still_carries_the_hardening_headers(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        client.get("/api/_probe/ok")

        response = client.get("/api/_probe/ok")
        assert response.status_code == 429
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"

    def test_a_zero_budget_disables_the_global_limiter(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=0).test_client()
        # Far past any conceivable ceiling — the guard must return
        # before the store is even consulted
        spend_global_budget(stamps=5000)

        assert client.get("/api/_probe/ok").status_code == 200

    def test_a_negative_budget_disables_the_global_limiter(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=-1).test_client()
        spend_global_budget(stamps=5000)

        assert client.get("/api/_probe/ok").status_code == 200

    def test_a_non_numeric_budget_falls_back_to_the_default(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT="not-a-number").test_client()
        spend_global_budget(stamps=599)
        assert client.get("/api/_probe/ok").status_code == 200

        spend_global_budget(stamps=600)
        assert client.get("/api/_probe/ok").status_code == 429

    def test_an_empty_budget_var_falls_back_to_the_default(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT="").test_client()
        spend_global_budget(stamps=600)
        assert client.get("/api/_probe/ok").status_code == 429

    def test_an_unset_budget_var_falls_back_to_the_default(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=None).test_client()
        spend_global_budget(stamps=600)
        assert client.get("/api/_probe/ok").status_code == 429

    def test_a_cors_preflight_is_exempt_even_over_budget(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        spend_global_budget(stamps=5000)

        # A throttled OPTIONS would surface in the browser as a CORS
        # failure, not as a rate limit
        response = client.options("/api/_probe/ok", headers={
            "Origin": "http://localhost:8081",
            "Access-Control-Request-Method": "GET",
        })
        assert response.status_code != 429

    def test_a_verb_that_is_not_options_is_still_metered(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        spend_global_budget(stamps=5000)

        assert post_raw(client, "/api/_probe/ok", {}).status_code == 429

    def test_each_client_address_gets_its_own_budget(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        spend_global_budget("10.0.0.1", stamps=5000)

        refused = client.get("/api/_probe/ok", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        allowed = client.get("/api/_probe/ok", environ_base={"REMOTE_ADDR": "10.0.0.2"})
        assert (refused.status_code, allowed.status_code) == (429, 200)

    def test_an_absent_client_address_falls_back_to_one_unknown_bucket(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        spend_global_budget("unknown", stamps=5000)

        response = client.get("/api/_probe/ok", environ_base={"REMOTE_ADDR": ""})
        assert response.status_code == 429

    def test_the_bucket_key_follows_the_forwarded_client_address(self, make_probe):
        # ProxyFix trusts one hop, so the rate-limit key is the real
        # peer the ingress appended — not the proxy
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        spend_global_budget("203.0.113.9", stamps=5000)

        response = client.get("/api/_probe/ok",
                              headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"})
        assert response.status_code == 429

    def test_the_budget_is_shared_across_every_route(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=2).test_client()
        assert client.get("/api/_probe/ok").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/_probe/ok").status_code == 429

    def test_even_an_unknown_path_spends_from_the_budget(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        assert client.get("/api/nowhere-at-all").status_code == 404
        assert client.get("/api/_probe/ok").status_code == 429

    def test_the_window_frees_the_budget_up_again(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        with time_machine.travel(0, tick=False) as traveller:
            assert client.get("/api/_probe/ok").status_code == 200
            assert client.get("/api/_probe/ok").status_code == 429

            traveller.shift(_RATE_LIMIT_WINDOW + 1)
            assert client.get("/api/_probe/ok").status_code == 200

    def test_the_retry_after_counts_the_oldest_stamp_out_of_the_window(self, make_probe):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        with time_machine.travel(0, tick=False) as traveller:
            client.get("/api/_probe/ok")
            traveller.shift(100)

            response = client.get("/api/_probe/ok")
            # 300 s window, 100 s already served, +1 so a client never
            # busy-loops on a zero
            assert response.headers["Retry-After"] == str(_RATE_LIMIT_WINDOW - 100 + 1)

    def test_the_refusal_is_logged_with_the_verb_and_the_path(self, make_probe, caplog):
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        client.get("/api/_probe/ok")

        with caplog.at_level(logging.WARNING, logger="app"):
            client.get("/api/_probe/ok")

        hits = [r.getMessage() for r in caplog.records if "Global rate limit hit" in r.getMessage()]
        assert hits and "GET /api/_probe/ok" in hits[0]

    def test_the_throttle_runs_before_the_body_is_ever_parsed(self, make_probe):
        # Registered first on purpose: a flood must be refused
        # without the JSON walkers touching a byte of it
        client = make_probe(GLOBAL_RATE_LIMIT=1).test_client()
        spend_global_budget(stamps=5000)

        response = post_raw(client, "/api/_probe/echo", nested_object(400))
        assert response.status_code == 429




class TestJsonHookOptsIn:

    def test_a_body_less_get_is_untouched(self, probe_client):
        assert probe_client.get("/api/_probe/ok").status_code == 200

    def test_a_body_with_no_content_type_is_left_to_the_handler(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "[1, 2]", content_type=None)
        # The hook never saw it; the handler's strict get_json() did
        assert response.status_code == 415

    def test_an_empty_content_type_is_left_to_the_handler(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "[1, 2]", content_type="")
        assert response.status_code == 415

    def test_a_text_body_is_left_to_the_handler(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "[1, 2]",
                            content_type="text/plain")
        assert response.status_code == 415

    def test_a_multipart_body_is_left_alone(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "[1, 2]",
                            content_type="multipart/form-data; boundary=xyz")
        assert response.status_code == 415

    def test_an_uppercase_json_content_type_still_opts_in(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "[1, 2]",
                            content_type="APPLICATION/JSON")
        assert response.status_code == 400
        assert response.get_json() == {"error": "JSON body must be an object"}

    def test_a_charset_suffix_still_opts_in(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "[1, 2]",
                            content_type="application/json; charset=utf-8")
        assert response.status_code == 400

    def test_a_vendor_json_content_type_still_opts_in(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "[1, 2]",
                            content_type="application/vnd.api+json")
        assert response.status_code == 400




class TestJsonHookBodyShape:

    @pytest.mark.parametrize("raw", ["[]", "[1, 2, 3]", '"a string"', "5", "5.5",
                                     "true", "false", "0", '[{"a": 1}]'])
    def test_a_non_object_top_level_body_is_refused(self, probe_client, raw):
        # Some 27 handlers used to call data.get() on these and raise
        # an unauthenticated AttributeError 500
        response = post_raw(probe_client, "/api/_probe/echo", raw)
        assert response.status_code == 400
        assert response.get_json() == {"error": "JSON body must be an object"}

    def test_a_null_body_is_not_an_object_error(self, probe_client):
        # json "null" parses to None, which the hook reads as "absent
        # or malformed" and hands straight to the handler
        response = post_raw(probe_client, "/api/_probe/echo", "null")
        assert response.status_code == 200
        assert response.get_json() == {"strict": None, "silent": None}

    def test_an_empty_body_is_left_to_the_handler(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "")
        assert response.status_code == 400
        assert response.get_json() == {"error": "Bad request"}

    def test_a_malformed_body_is_left_to_the_handler(self, probe_client):
        # silent=True in the hook, so the 400 below is the handler's
        # own get_json() failing — not the hook's object check
        response = post_raw(probe_client, "/api/_probe/echo", "{not json")
        assert response.status_code == 400
        assert response.get_json() == {"error": "Bad request"}

    def test_an_empty_object_passes(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "{}")
        assert response.status_code == 200
        assert response.get_json()["strict"] == {}

    def test_an_ordinary_object_reaches_the_handler_unchanged(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo",
                            {"a": 1, "b": "labas", "c": None, "d": [1, 2]})
        assert response.get_json()["strict"] == {"a": 1, "b": "labas", "c": None, "d": [1, 2]}

    def test_the_shape_check_applies_to_put_and_patch_too(self, probe_client):
        for method in ("PUT", "PATCH"):
            response = post_raw(probe_client, "/api/_probe/echo", "[1]", method=method)
            assert response.status_code == 400, method

    def test_a_get_with_a_json_body_is_checked_the_same_way(self, probe_client):
        # The hook is keyed on the content type, not the verb
        response = post_raw(probe_client, "/api/_probe/ok", "[1]", method="GET")
        assert response.status_code == 400
        assert response.get_json() == {"error": "JSON body must be an object"}

    def test_the_hook_refuses_before_the_route_is_even_resolved(self, probe_client):
        # before_request runs ahead of dispatch, so a bad body on an
        # unknown path is a 400 — never a 404
        response = post_raw(probe_client, "/api/nowhere-at-all", "[1]")
        assert response.status_code == 400

    def test_the_object_refusal_carries_the_hardening_headers(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "[1]")
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Cache-Control"] == "no-store"




class TestJsonHookNullBytes:

    def test_a_null_byte_is_stripped_from_a_top_level_string(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", {"a": "pri\x00e\x00s"})
        assert response.get_json()["strict"] == {"a": "pries"}

    def test_the_cleaned_body_lands_in_both_get_json_cache_slots(self, probe_client):
        # The hook overwrites Werkzeug's private (strict, silent) pair;
        # a handler reaching for either flavour must see the clean copy
        body = post_raw(probe_client, "/api/_probe/echo", {"a": "x\x00y"}).get_json()
        assert body["strict"] == body["silent"] == {"a": "xy"}

    def test_null_bytes_are_stripped_inside_nested_containers(self, probe_client):
        payload = {"outer": {"inner": ["a\x00b", {"deep": "c\x00d"}]}}
        response = post_raw(probe_client, "/api/_probe/echo", payload)
        assert response.get_json()["strict"] == {"outer": {"inner": ["ab", {"deep": "cd"}]}}

    def test_a_string_that_is_only_null_bytes_becomes_empty(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", {"a": "\x00\x00"})
        assert response.get_json()["strict"] == {"a": ""}

    def test_dict_keys_carrying_a_null_byte_are_left_alone(self, probe_client):
        # Only values are walked — the key keeps its bytes
        response = post_raw(probe_client, "/api/_probe/echo", {"k\x00": "v\x00"})
        assert response.get_json()["strict"] == {"k\x00": "v"}

    def test_non_string_scalars_survive_the_strip_unchanged(self, probe_client):
        payload = {"i": 7, "f": 1.5, "t": True, "f2": False, "n": None}
        response = post_raw(probe_client, "/api/_probe/echo", payload)
        assert response.get_json()["strict"] == payload

    def test_a_body_with_no_null_byte_is_not_rewritten(self, probe_client):
        payload = {"a": ["b", {"c": 1}], "d": None}
        response = post_raw(probe_client, "/api/_probe/echo", payload)
        assert response.get_json()["strict"] == payload

    def test_empty_containers_survive_the_walk(self, probe_client):
        payload = {"d": {}, "l": [], "n": {"x": []}}
        response = post_raw(probe_client, "/api/_probe/echo", payload)
        assert response.get_json()["strict"] == payload




class TestJsonHookDepthCap:

    def test_a_body_at_the_depth_cap_is_accepted(self, probe_client):
        depth = app_package._MAX_JSON_DEPTH
        response = post_raw(probe_client, "/api/_probe/ok", nested_object(depth))
        assert response.status_code == 200

    def test_a_body_one_level_past_the_cap_is_refused(self, probe_client):
        depth = app_package._MAX_JSON_DEPTH + 1
        response = post_raw(probe_client, "/api/_probe/ok", nested_object(depth))
        assert response.status_code == 400
        assert response.get_json() == {"error": "JSON nesting too deep"}

    def test_nested_arrays_are_counted_the_same_way(self, probe_client):
        cap = app_package._MAX_JSON_DEPTH
        # The wrapping object is level 1, so cap-1 arrays sit exactly
        # on the ceiling and cap arrays go one past it
        assert post_raw(probe_client, "/api/_probe/ok",
                        {"root": nested_array(cap - 1)}).status_code == 200
        assert post_raw(probe_client, "/api/_probe/ok",
                        {"root": nested_array(cap)}).status_code == 400

    def test_a_wide_but_shallow_body_is_not_refused(self, probe_client):
        payload = {f"k{i}": [i, {"x": i}] for i in range(500)}
        assert post_raw(probe_client, "/api/_probe/ok", payload).status_code == 200

    def test_a_pathological_body_is_a_400_not_a_recursion_500(self, probe_client):
        # 100 000 nested arrays blow up inside json.loads itself, long
        # before either walker gets a look — an unauthenticated 500 on
        # every route in the app until the guard went in
        raw = "{\"a\": " + "[" * 100000 + "]" * 100000 + "}"
        response = post_raw(probe_client, "/api/_probe/ok", raw)
        assert response.status_code == 400
        assert response.get_json() == {"error": "JSON nesting too deep"}

    def test_the_depth_refusal_carries_the_hardening_headers(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/ok",
                            nested_object(app_package._MAX_JSON_DEPTH + 1))
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_the_depth_refusal_is_logged(self, probe_client, caplog):
        with caplog.at_level(logging.WARNING, logger="app"):
            post_raw(probe_client, "/api/_probe/ok",
                     nested_object(app_package._MAX_JSON_DEPTH + 1))
        assert any("over-nested" in record.getMessage() for record in caplog.records)




class TestAvatarUrlGate:

    @pytest.mark.parametrize("key", ["avatar_url", "avatarUrl"])
    @pytest.mark.parametrize("extension", ["jpg", "jpeg", "png", "gif", "webp"])
    def test_every_extension_uploads_hands_out_is_accepted(self, probe_client, key, extension):
        url = "/api/uploads/" + "a1b2c3d4" * 4 + "." + extension
        response = post_raw(probe_client, "/api/_probe/echo", {key: url})
        assert response.status_code == 200
        assert response.get_json()["strict"][key] == url

    @pytest.mark.parametrize("key", ["avatar_url", "avatarUrl"])
    def test_null_clears_the_avatar(self, probe_client, key):
        response = post_raw(probe_client, "/api/_probe/echo", {key: None})
        assert response.status_code == 200

    @pytest.mark.parametrize("key", ["avatar_url", "avatarUrl"])
    def test_the_empty_string_clears_the_avatar(self, probe_client, key):
        response = post_raw(probe_client, "/api/_probe/echo", {key: ""})
        assert response.status_code == 200

    def test_a_body_without_either_key_is_never_checked(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", {"avatar": "whatever://x"})
        assert response.status_code == 200

    @pytest.mark.parametrize("key", ["avatar_url", "avatarUrl"])
    @pytest.mark.parametrize("raw", ["123", "1.5", "true", "false", "[]", "{}",
                                     '["/api/uploads/x.jpg"]', "0"])
    def test_a_non_string_value_is_refused(self, probe_client, key, raw):
        response = post_raw(probe_client, "/api/_probe/echo", '{"%s": %s}' % (key, raw))
        assert response.status_code == 400
        assert response.get_json() == {"error": _NOT_A_STRING_MESSAGE}

    def test_a_url_at_the_length_ceiling_fails_on_its_shape(self, probe_client):
        # Exactly 2048 characters: past the regex, inside the cap, so
        # the SHAPE message is the one that must come back
        url = "/api/uploads/" + "a" * (2048 - _PREFIX_LEN)
        assert len(url) == 2048

        response = post_raw(probe_client, "/api/_probe/echo", {"avatar_url": url})
        assert response.get_json() == {"error": _BAD_SHAPE_MESSAGE}

    def test_a_url_one_character_past_the_ceiling_fails_on_its_length(self, probe_client):
        url = "/api/uploads/" + "a" * (2049 - _PREFIX_LEN)
        assert len(url) == 2049

        response = post_raw(probe_client, "/api/_probe/echo", {"avatar_url": url})
        assert response.status_code == 400
        assert response.get_json() == {"error": _TOO_LONG_MESSAGE}

    def test_a_hugely_oversized_url_fails_on_its_length(self, probe_client):
        url = "/api/uploads/" + "a" * 50000 + ".jpg"
        response = post_raw(probe_client, "/api/_probe/echo", {"avatar_url": url})
        assert response.get_json() == {"error": _TOO_LONG_MESSAGE}

    @pytest.mark.parametrize("url", [
        "https://evil.example/avatar.jpg",
        "http://evil.example/avatar.jpg",
        "//evil.example/avatar.jpg",
        "javascript:alert(1)",
        "data:image/png;base64,iVBORw0KGgo=",
        "/api/uploads/../../../etc/passwd",
        "/api/uploads/..%2f..%2fetc%2fpasswd",
        "/api/uploads/" + "0" * 32 + ".jpg?x=//evil.example",
        "/api/uploads/" + "0" * 32 + ".jpg#/../..",
        "/api/uploads/" + "0" * 32 + ".jpg\n",
        "/api/uploads/" + "0" * 32 + ".jpg ",
        " /api/uploads/" + "0" * 32 + ".jpg",
        "/api/uploads/" + "0" * 31 + ".jpg",
        "/api/uploads/" + "0" * 33 + ".jpg",
        "/api/uploads/" + "A" * 32 + ".jpg",
        "/api/uploads/" + "0" * 32 + ".JPG",
        "/api/uploads/" + "0" * 32 + ".svg",
        "/api/uploads/" + "0" * 32,
        "/api/uploads/" + "0" * 32 + ".jpg.exe",
        "/api/uploadsX/" + "0" * 32 + ".jpg",
        "api/uploads/" + "0" * 32 + ".jpg",
        "/API/UPLOADS/" + "0" * 32 + ".jpg",
        "/api/uploads//" + "0" * 32 + ".jpg",
        "/api/uploads/sub/" + "0" * 32 + ".jpg",
    ])
    def test_anything_but_an_own_upload_path_is_refused(self, probe_client, url):
        response = post_raw(probe_client, "/api/_probe/echo", {"avatar_url": url})
        assert response.status_code == 400
        assert response.get_json() == {"error": _BAD_SHAPE_MESSAGE}

    def test_a_trailing_newline_cannot_smuggle_a_valid_prefix(self, probe_client):
        # "$" also matches before a trailing newline; the rule is
        # anchored with \Z precisely so this stays refused
        response = post_raw(probe_client, "/api/_probe/echo",
                            {"avatar_url": _VALID_AVATAR + "\n"})
        assert response.status_code == 400

    def test_the_camel_case_spelling_is_checked_too(self, probe_client):
        # avatarUrl used to walk past this hook and be stored verbatim
        response = post_raw(probe_client, "/api/_probe/echo",
                            {"avatarUrl": "https://evil.example/x.jpg"})
        assert response.status_code == 400

    def test_a_bad_camel_case_key_is_caught_beside_a_good_snake_case_one(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", {
            "avatar_url": _VALID_AVATAR,
            "avatarUrl": "https://evil.example/x.jpg",
        })
        assert response.status_code == 400

    def test_both_keys_valid_is_accepted(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", {
            "avatar_url": _VALID_AVATAR,
            "avatarUrl": _VALID_AVATAR,
        })
        assert response.status_code == 200

    def test_a_null_byte_is_stripped_before_the_url_is_judged(self, probe_client):
        # Order matters: the strip runs first, so a NUL cannot be used
        # to hide a bad shape from the regex either way round
        good = post_raw(probe_client, "/api/_probe/echo",
                        {"avatar_url": _VALID_AVATAR[:20] + "\x00" + _VALID_AVATAR[20:]})
        assert good.status_code == 200
        assert good.get_json()["strict"]["avatar_url"] == _VALID_AVATAR

        bad = post_raw(probe_client, "/api/_probe/echo",
                       {"avatar_url": "/api/uploads/\x00../../etc/passwd"})
        assert bad.status_code == 400

    def test_the_refusal_never_reaches_the_handler(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo",
                            {"avatar_url": "javascript:alert(1)"})
        assert "strict" not in response.get_json()

    def test_a_deeply_nested_avatar_key_is_not_the_hooks_business(self, probe_client):
        # Only the TOP level is inspected; a nested key is data
        response = post_raw(probe_client, "/api/_probe/echo",
                            {"profile": {"avatar_url": "javascript:alert(1)"}})
        assert response.status_code == 200




class TestEscapeValue:

    def test_a_bare_string_is_escaped(self, probe_application):
        assert probe_application.json.dumps("a < b") == '"a &lt; b"'

    def test_quotes_are_escaped_too(self, probe_application):
        # quote=True, so an attribute-context break is impossible
        dumped = probe_application.json.dumps("say \"hi\" & 'bye'")
        assert "&quot;" in dumped and "&#x27;" in dumped and "&amp;" in dumped

    def test_an_ampersand_is_escaped_first_so_entities_survive(self, probe_application):
        assert json.loads(probe_application.json.dumps("&lt;")) == "&amp;lt;"

    @pytest.mark.parametrize("value", [None, True, False, 0, -1, 3.5])
    def test_a_bare_non_container_scalar_is_returned_untouched(self, probe_application, value):
        assert json.loads(probe_application.json.dumps(value)) == value

    def test_values_in_a_dict_are_escaped(self, probe_application):
        assert json.loads(probe_application.json.dumps({"k": "<b>"})) == {"k": "&lt;b&gt;"}

    def test_dict_keys_are_left_alone(self, probe_application):
        # Keys are never rendered as markup; escaping them would break
        # every client that looks a field up by name
        assert '"k<b>"' in probe_application.json.dumps({"k<b>": "v"})

    def test_a_non_string_dict_key_still_works(self, probe_application):
        assert json.loads(probe_application.json.dumps({1: "<b>"})) == {"1": "&lt;b&gt;"}

    def test_strings_in_a_list_are_escaped(self, probe_application):
        assert json.loads(probe_application.json.dumps(["<a>", "<b>"])) == ["&lt;a&gt;", "&lt;b&gt;"]

    def test_a_tuple_is_normalised_to_a_list(self, probe_application):
        assert json.loads(probe_application.json.dumps(("<a>", 2))) == ["&lt;a&gt;", 2]

    def test_a_nested_tuple_is_normalised_too(self, probe_application):
        dumped = probe_application.json.dumps({"t": ("<a>", ("<b>",))})
        assert json.loads(dumped) == {"t": ["&lt;a&gt;", ["&lt;b&gt;"]]}

    def test_a_dict_inside_a_tuple_is_walked_too(self, probe_application):
        dumped = probe_application.json.dumps(("<a>", {"k": "<b>"}, [7]))
        assert json.loads(dumped) == ["&lt;a&gt;", {"k": "&lt;b&gt;"}, [7]]

    def test_containers_nest_in_either_direction(self, probe_application):
        value = {"l": ["<a>", {"d": ["<b>"]}], "d": {"l": ["<c>"]}}
        assert json.loads(probe_application.json.dumps(value)) == {
            "l": ["&lt;a&gt;", {"d": ["&lt;b&gt;"]}],
            "d": {"l": ["&lt;c&gt;"]},
        }

    def test_non_strings_inside_containers_are_untouched(self, probe_application):
        value = {"i": 7, "f": 1.5, "t": True, "n": None, "l": [1, None, False]}
        assert json.loads(probe_application.json.dumps(value)) == value

    def test_empty_containers_come_back_empty(self, probe_application):
        dumped = probe_application.json.dumps({"d": {}, "l": [], "t": ()})
        assert json.loads(dumped) == {"d": {}, "l": [], "t": []}

    def test_lithuanian_text_stays_utf8(self, probe_application):
        # ensure_ascii=False — the mobile client shows these bytes
        assert "ąčęėįšųūž" in probe_application.json.dumps({"t": "ąčęėįšųūž"})

    def test_extra_dumps_kwargs_are_forwarded(self, probe_application):
        dumped = probe_application.json.dumps({"b": 1, "a": 2}, sort_keys=False)
        assert dumped.index('"b"') < dumped.index('"a"')

    def test_a_structure_at_the_depth_cap_is_serialised(self, probe_application):
        dumped = probe_application.json.dumps(nested_object(app_package._MAX_JSON_DEPTH))
        assert dumped.count("{") == app_package._MAX_JSON_DEPTH

    def test_a_structure_one_level_past_the_cap_refuses_to_serialise(self, probe_application):
        with pytest.raises(app_package._JsonTooDeep):
            probe_application.json.dumps(nested_object(app_package._MAX_JSON_DEPTH + 1))

    def test_a_deep_list_is_capped_the_same_way(self, probe_application):
        with pytest.raises(app_package._JsonTooDeep):
            probe_application.json.dumps(nested_array(app_package._MAX_JSON_DEPTH + 1))

    def test_a_deep_tuple_is_capped_the_same_way(self, probe_application):
        node = "x"
        for _ in range(app_package._MAX_JSON_DEPTH + 1):
            node = (node,)
        with pytest.raises(app_package._JsonTooDeep):
            probe_application.json.dumps(node)




class TestJsonProviderFailsClosed:

    def test_a_failure_is_logged_with_the_request_path(self, probe_application, caplog):
        with probe_application.test_request_context("/api/_probe/deep"):
            with caplog.at_level(logging.ERROR, logger="app"):
                with pytest.raises(app_package._JsonTooDeep):
                    probe_application.json.dumps(nested_object(40))

        assert any("/api/_probe/deep" in record.getMessage() for record in caplog.records)

    def test_a_failure_outside_a_request_context_names_no_path(self, probe_application, caplog):
        with caplog.at_level(logging.ERROR, logger="app"):
            with pytest.raises(app_package._JsonTooDeep):
                probe_application.json.dumps(nested_object(40))

        assert any("<no request>" in record.getMessage() for record in caplog.records)

    def test_any_escaping_failure_is_reported_not_swallowed(self, probe_application, caplog):

        # A mapping whose items() refuses — the generic arm of the
        # provider's except, not the depth cap
        class HostileMapping(dict):
            def items(self):
                raise ValueError("no items for you")

        with caplog.at_level(logging.ERROR, logger="app"):
            with pytest.raises(ValueError):
                probe_application.json.dumps(HostileMapping(a=1))

        assert any("refusing to ship the raw body" in record.getMessage()
                   for record in caplog.records)

    def test_a_body_that_cannot_be_escaped_becomes_a_500(self, probe_client):
        # Fails CLOSED: the old hook shipped the raw body instead
        response = probe_client.get("/api/_probe/deep")
        assert response.status_code == 500
        assert response.get_json() == {"error": "Internal server error"}

    def test_a_serialisation_error_is_not_blamed_on_the_escaper(self, probe_application, caplog):
        # super().dumps() runs OUTSIDE the try, so a plain unencodable
        # value must not be logged as an escaping failure
        with caplog.at_level(logging.ERROR, logger="app"):
            with pytest.raises(TypeError):
                probe_application.json.dumps({"s": {1, 2}})

        assert not any("Output escaping failed" in record.getMessage()
                       for record in caplog.records)




class TestOutputEscapingOnTheWire:

    def test_markup_in_a_response_body_is_escaped_exactly_once(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo",
                            {"content": "<script>alert(1)</script>"})
        raw = response.get_data(as_text=True)
        assert "<script>" not in raw
        assert "&lt;script&gt;" in raw
        assert "&amp;lt;" not in raw

    def test_an_error_body_is_escaped_on_the_same_path(self, probe_client):
        # Every status is escaped now, not only < 400
        response = post_raw(probe_client, "/api/_probe/echo",
                            {"avatar_url": "javascript:'<b>'"})
        assert response.status_code == 400
        assert "<b>" not in response.get_data(as_text=True)

    def test_numbers_and_flags_keep_their_json_types(self, probe_client):
        body = post_raw(probe_client, "/api/_probe/echo",
                        {"n": 7, "f": 1.5, "b": True, "z": None}).get_json()
        assert body["strict"] == {"n": 7, "f": 1.5, "b": True, "z": None}

    def test_the_health_probe_body_survives_the_provider(self, probe_client):
        body = probe_client.get("/api/health").get_json()
        assert body["status"] == "ok"
        assert body["service"] == "knfapp-backend"




class TestSecurityHeaders:

    @pytest.mark.parametrize("path,expected_status", [
        ("/api/_probe/ok", 200),
        ("/api/nowhere", 404),
        ("/_probe/plain", 200),
        ("/api/_probe/boom", 500),
    ])
    def test_the_six_hardening_headers_are_on_every_status(self, probe_client, path, expected_status):
        response = probe_client.get(path)
        assert response.status_code == expected_status
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-XSS-Protection"] == "1; mode=block"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["Strict-Transport-Security"] == \
            "max-age=31536000; includeSubDomains"
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]

    def test_the_csp_names_every_directive_the_app_relies_on(self, probe_client):
        policy = probe_client.get("/api/_probe/ok").headers["Content-Security-Policy"]
        for directive in ("script-src 'self'", "style-src 'self' 'unsafe-inline'",
                          "img-src 'self' data: https:", "font-src 'self'"):
            assert directive in policy

    def test_an_api_answer_defaults_to_no_store(self, probe_client):
        response = probe_client.get("/api/_probe/ok")
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"

    def test_a_route_with_its_own_cache_policy_keeps_it(self, probe_client):
        # setdefault, not assignment: uploads/routes.py ships
        # max-age=86400 for avatars and must keep it
        response = probe_client.get("/api/_probe/cached")
        assert response.headers["Cache-Control"] == "public, max-age=60"
        # ...and the HTTP/1.0 header does NOT contradict it
        assert "Pragma" not in response.headers

    def test_a_route_with_its_own_pragma_keeps_it(self, probe_client):
        response = probe_client.get("/api/_probe/pragma")
        assert response.headers["Pragma"] == "knfapp-own-token"
        assert response.headers["Cache-Control"] == "no-store"

    def test_a_path_outside_api_gets_no_cache_header(self, probe_client):
        response = probe_client.get("/_probe/plain")
        assert "Cache-Control" not in response.headers
        assert "Pragma" not in response.headers

    def test_a_404_outside_api_gets_no_cache_header_either(self, probe_client):
        response = probe_client.get("/definitely-not-here")
        assert response.status_code == 404
        assert "Cache-Control" not in response.headers

    def test_a_prefix_that_only_looks_like_api_is_not_treated_as_one(self, probe_client):
        # startswith("/api/") — "/apiary" must not match
        response = probe_client.get("/apiary")
        assert response.status_code == 404
        assert "Cache-Control" not in response.headers

    def test_the_headers_ride_along_on_an_options_answer(self, probe_client):
        response = probe_client.options("/api/_probe/ok")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"

    def test_the_headers_ride_along_on_a_head_answer(self, probe_client):
        response = probe_client.head("/api/_probe/ok")
        assert response.headers["X-Frame-Options"] == "DENY"

    def test_the_headers_ride_along_on_a_413(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/ok", "x" * (7 * 1024 * 1024))
        assert response.status_code == 413
        assert response.headers["X-Content-Type-Options"] == "nosniff"




class TestErrorHandlers:

    def test_a_werkzeug_400_answers_the_json_shape(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", "{oops")
        assert response.status_code == 400
        assert response.get_json() == {"error": "Bad request"}
        assert response.is_json

    def test_an_unknown_api_path_answers_the_json_404(self, probe_client):
        response = probe_client.get("/api/nope")
        assert response.status_code == 404
        assert response.get_json() == {"error": "Not found"}

    def test_an_unknown_path_outside_api_answers_the_same_404(self, probe_client):
        response = probe_client.get("/nope")
        assert response.status_code == 404
        assert response.get_json() == {"error": "Not found"}

    def test_a_wrong_verb_on_a_known_path_answers_405(self, probe_client):
        response = probe_client.delete("/api/_probe/ok")
        assert response.status_code == 405
        assert response.get_json() == {"error": "Method not allowed"}

    def test_the_405_names_the_verbs_that_would_work(self, probe_client):
        # RFC 9110 makes Allow mandatory on a 405; the handler
        # copies the verbs off Werkzeug's MethodNotAllowed
        response = probe_client.delete("/api/_probe/ok")
        assert "GET" in response.headers.get("Allow", "")
        assert "POST" in response.headers["Allow"]

    def test_a_body_without_a_json_content_type_answers_415(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/echo", {"a": 1},
                            content_type="text/plain")
        assert response.status_code == 415
        assert response.get_json() == {"error": "Unsupported media type"}

    def test_an_oversized_body_answers_413_with_a_translatable_code(self, probe_client):
        response = post_raw(probe_client, "/api/_probe/ok", "x" * (7 * 1024 * 1024))
        assert response.status_code == 413
        assert response.get_json() == {"error": "File too large", "code": "file_too_large"}

    def test_a_body_just_under_the_ceiling_is_not_refused(self, probe_client):
        # 6 MB cap; a 5 MB upload plus its multipart envelope must fit
        payload = json.dumps({"a": "x" * (5 * 1024 * 1024)})
        assert len(payload) < 6 * 1024 * 1024
        assert post_raw(probe_client, "/api/_probe/ok", payload).status_code == 200

    def test_a_body_at_the_exact_ceiling_is_accepted(self, probe_client):
        ceiling = 6 * 1024 * 1024
        payload = '{"a":"' + "x" * (ceiling - 8) + '"}'
        assert len(payload) == ceiling

        assert post_raw(probe_client, "/api/_probe/ok", payload).status_code == 200

    def test_a_body_one_byte_past_the_ceiling_is_refused(self, probe_client):
        ceiling = 6 * 1024 * 1024
        payload = '{"a":"' + "x" * (ceiling - 7) + '"}'
        assert len(payload) == ceiling + 1

        assert post_raw(probe_client, "/api/_probe/ok", payload).status_code == 413

    def test_an_unhandled_exception_answers_a_generic_500(self, probe_client):
        # The traceback stays in the process log; the client learns
        # nothing about the internals
        response = probe_client.get("/api/_probe/boom")
        assert response.status_code == 500
        assert response.get_json() == {"error": "Internal server error"}
        assert "probe explosion" not in response.get_data(as_text=True)

    def test_every_error_body_is_json_never_an_html_page(self, probe_client):
        cases = [
            probe_client.get("/api/nope"),
            probe_client.delete("/api/_probe/ok"),
            probe_client.get("/api/_probe/boom"),
            post_raw(probe_client, "/api/_probe/echo", "{oops"),
            post_raw(probe_client, "/api/_probe/echo", {"a": 1}, content_type="text/plain"),
        ]
        for response in cases:
            assert response.status_code >= 400
            assert response.is_json, response.get_data(as_text=True)[:80]




class TestDatabaseUnavailable:

    @pytest.mark.parametrize("flavour", ["locked", "busy", "shouty", "shouty-busy"])
    def test_a_lock_or_busy_error_answers_a_retryable_503(self, probe_client, flavour):
        # get_db() already waited 30 s on PRAGMA busy_timeout, so
        # reaching here means retrying is the right advice
        response = probe_client.get(f"/api/_probe/sqlite/{flavour}")
        assert response.status_code == 503
        assert response.get_json() == {"error": "Database busy, please retry",
                                       "code": "database_busy"}

    def test_the_503_carries_a_retry_after(self, probe_client):
        response = probe_client.get("/api/_probe/sqlite/locked")
        assert response.headers["Retry-After"] == "2"

    def test_the_503_still_carries_the_hardening_headers(self, probe_client):
        response = probe_client.get("/api/_probe/sqlite/locked")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.parametrize("flavour", ["other", "empty"])
    def test_any_other_sqlite_error_stays_a_generic_500(self, probe_client, flavour):
        response = probe_client.get(f"/api/_probe/sqlite/{flavour}")
        assert response.status_code == 500
        assert response.get_json() == {"error": "Internal server error"}
        assert "Retry-After" not in response.headers

    def test_the_500_leaks_no_schema_detail(self, probe_client):
        response = probe_client.get("/api/_probe/sqlite/other")
        assert "no such table" not in response.get_data(as_text=True)

    def test_a_busy_error_is_logged_as_a_warning_not_a_traceback(self, probe_client, caplog):
        with caplog.at_level(logging.WARNING, logger="app"):
            probe_client.get("/api/_probe/sqlite/locked")

        busy = [r for r in caplog.records if "Database busy" in r.getMessage()]
        assert busy and busy[0].levelno == logging.WARNING
        assert busy[0].exc_info is None

    def test_an_unexpected_sqlite_error_is_logged_with_its_traceback(self, probe_client, caplog):
        with caplog.at_level(logging.ERROR, logger="app"):
            probe_client.get("/api/_probe/sqlite/other")

        errors = [r for r in caplog.records if "Unhandled SQLite error" in r.getMessage()]
        assert errors and errors[0].exc_info is not None
