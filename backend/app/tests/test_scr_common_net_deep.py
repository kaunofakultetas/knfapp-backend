# -----------------------------------------------------------
#  [*] Tests — scraper/common.py, the network half, exhaustive
#
#  The gap-closing pass over six functions of the shared
#  scraper plumbing: get_session, host_allowed, normalise_url,
#  fetch, validate_image_url and push_allowed. The broad suite
#  next door already walks their happy paths; this file walks
#  the arms it does not:
#
#    - get_session: the guard is `is not None` and not
#      truthiness, ONE adapter object serves both schemes, the
#      Retry only ever retries a GET, and eight threads racing
#      the first call still leave one session behind
#    - host_allowed: every shape an injected href uses to look
#      like the faculty — userinfo before an evil host, a
#      trailing dot, a percent-encoded dot, a homograph that
#      only NFKC-normalises to the real name, a decimal IP,
#      IPv6 loopback — plus the arms reached by None, bytes and
#      an empty allowlist
#    - normalise_url: what the dedup key silently DROPS (port,
#      credentials, ;params, fragment) and what it does NOT
#      collapse (a reordered query), the trailing-slash and
#      blank-value boundaries, and idempotency as a property
#      over every odd shape at once
#    - fetch: the guards in the order they run (nothing is read
#      off the wire before the type is judged), the byte budget
#      at 0 / 1 / one overshooting chunk, the response closed on
#      EVERY exit including one that escapes, and the redirect
#      re-check that only ever looks at the FINAL hop
#    - validate_image_url: the resolve/cap/allowlist arms from
#      both sides (a bad page URL and a bad src), and the two
#      srcs that still resolve to the article page itself
#    - push_allowed: which suppressions spend the hourly slot
#      and which do not, the window boundary at exactly 3600 s,
#      a backwards clock, and two threads racing one source
#
#  Nothing here touches the network: the wire is `responses`,
#  the clock is time_machine, and the two tests that need to
#  see BELOW requests use the local stubs.
# -----------------------------------------------------------


import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
import requests
import responses
import time_machine

from app.scraper import common


LOGGER_NAME = "app.scraper.common"

PAGE_URL = "https://knf.vu.lt/naujienos/studiju-pradzia"

# Aware UTC so time_machine cannot read the destination as
# local time; every push-window test reasons from this instant
FROZEN = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)




# -----------------------------------------------------------
# reset_module_globals
# -----------------------------------------------------------
#
# common.py keeps two globals that outlive a test — the pooled
# session and the per-source push clock. Rebinding both per
# test through monkeypatch is what stops the ORDER tests run in
# deciding whether a push is allowed or which session a fetch
# went through.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_module_globals(monkeypatch):
    monkeypatch.setattr(common, "_SESSION", None)
    monkeypatch.setattr(common, "_LAST_PUSH", {})




# -----------------------------------------------------------
# completed_run
# -----------------------------------------------------------
#
#   completed_run("knf")                 — a predecessor
#   completed_run("knf", status="running")
#
# One scraper_runs row written straight through the `db`
# connection: push_allowed asks about run history, and no route
# can put a run into an arbitrary state. The status CHECK
# constraint allows only running/completed/failed.
# -----------------------------------------------------------

@pytest.fixture
def completed_run(db):

    def _seed(source, status="completed", found=0, new=0, days_ago=0.0):
        run_id = str(uuid.uuid4())
        stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        db.execute(
            """INSERT INTO scraper_runs
               (id, source, status, articles_found, articles_new, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, source, status, found, new, stamp, stamp),
        )
        db.commit()
        return run_id

    return _seed




# -----------------------------------------------------------
# _FakeResponse / _FakeWire
# -----------------------------------------------------------
#
# `responses` fakes the wire, which is the right level for
# almost everything — except the handful of things that happen
# BELOW it: the keyword arguments fetch hands requests, a body
# that breaks halfway through iteration, one chunk that
# overshoots the budget on its own, and proving no body is read
# at all when a guard has already answered. An Exception placed
# among the chunks is raised at that point in the stream.
# -----------------------------------------------------------

class _FakeResponse:

    def __init__(self, url, chunks=(), content_type="text/html"):
        self.url = url
        self.headers = {"Content-Type": content_type} if content_type is not None else {}
        self._chunks = list(chunks)
        self.closed = False
        self.reads = 0

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1):
        self.reads += 1
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class _FakeWire:

    def __init__(self, response):
        self.response = response
        self.seen = {}

    def get(self, url, **kwargs):
        self.seen = dict(kwargs, url=url)
        return self.response




# -----------------------------------------------------------
# _CountingDb
# -----------------------------------------------------------
#
# A thread-safe stand-in for the run-history query push_allowed
# opens with: the two race tests need many threads hitting it
# at once, and a real sqlite3 connection refuses to be used
# from a thread other than the one that made it.
# -----------------------------------------------------------

class _CountingDb:

    def __init__(self, count=1, error=None):
        self._count = count
        self._error = error

    def execute(self, sql, params=()):
        if self._error is not None:
            raise self._error
        return self

    def fetchone(self):
        return (self._count,)




# ===========================================================
# get_session — the one pooled session
# ===========================================================


def test_get_session_stores_the_session_it_built_in_the_module_global():
    assert common._SESSION is None

    session = common.get_session()

    assert common._SESSION is session
    assert isinstance(session, requests.Session)


def test_get_session_hands_back_a_falsy_session_rather_than_rebuilding_one(monkeypatch):
    # The guard is `is not None`, not truthiness — a session
    # that answered False would otherwise be rebuilt per call
    class _Falsy:
        def __bool__(self):
            return False

    planted = _Falsy()
    monkeypatch.setattr(common, "_SESSION", planted)

    assert common.get_session() is planted


def test_get_session_never_rebuilds_once_the_global_is_set(monkeypatch):
    planted = object()
    monkeypatch.setattr(common, "_SESSION", planted)

    assert common.get_session() is planted is common.get_session()


def test_get_session_serves_both_schemes_from_one_adapter_object():
    session = common.get_session()

    # One adapter means one connection pool: mounting two would
    # halve the reuse the pooling exists for
    assert session.adapters["https://"] is session.adapters["http://"]


def test_get_session_replaces_the_two_default_adapters_and_adds_none():
    session = common.get_session()

    assert set(session.adapters) == {"https://", "http://"}
    assert session.adapters["https://"].max_retries.total == 2


@pytest.mark.parametrize("attribute", ["total", "connect", "read", "status"])
def test_get_session_gives_every_retry_counter_the_same_budget(attribute):
    retry = common.get_session().adapters["https://"].max_retries

    assert getattr(retry, attribute) == 2


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_get_session_retries_a_transient_status_on_a_get(status):
    retry = common.get_session().adapters["https://"].max_retries

    assert retry.is_retry("GET", status) is True


@pytest.mark.parametrize("status", [200, 301, 400, 401, 403, 404, 410, 418, 501])
def test_get_session_does_not_retry_a_status_that_will_not_change(status):
    retry = common.get_session().adapters["https://"].max_retries

    assert retry.is_retry("GET", status) is False


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
def test_get_session_retries_nothing_but_a_get(method):
    # A retried POST double-submits; the scrapers only ever GET
    retry = common.get_session().adapters["https://"].max_retries

    assert retry.is_retry(method, 503) is False


def test_get_session_backs_off_between_retries_instead_of_hammering():
    retry = common.get_session().adapters["https://"].max_retries

    assert retry.backoff_factor == 0.5
    assert retry.raise_on_status is False


def test_get_session_starts_with_no_cookies_and_no_authorization():
    session = common.get_session()

    assert len(session.cookies) == 0
    assert "Authorization" not in session.headers


def test_eight_threads_racing_the_first_call_still_leave_one_session_behind():
    # Two threads CAN build two sessions — the loser is garbage.
    # What must hold is that the global settles on exactly one
    # and every later caller gets it
    gate = threading.Barrier(8)
    built = []

    def race():
        gate.wait()
        built.append(common.get_session())

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(built) == 8
    assert all(isinstance(session, requests.Session) for session in built)
    assert common._SESSION in built
    assert common.get_session() is common._SESSION is common.get_session()




# ===========================================================
# host_allowed — the allowlist gate
# ===========================================================


@pytest.mark.parametrize("url", [
    # The userinfo trick: everything before the @ is a credential
    "https://knf.vu.lt@evil.example.org/x",
    "https://knf.vu.lt:slaptazodis@evil.example.org/x",
    "https://www.knf.vu.lt@169.254.169.254/latest/meta-data/",
])
def test_host_allowed_reads_the_host_after_the_at_sign_not_before_it(url):
    assert common.host_allowed(url, common.KNF_HOSTS) is False


@pytest.mark.parametrize("url", [
    "HTTPS://knf.vu.lt/naujienos",
    "HtTp://knf.vu.lt/naujienos",
    "hTTps://KNF.VU.LT/naujienos",
])
def test_host_allowed_reads_the_scheme_whatever_its_case(url):
    assert common.host_allowed(url, common.KNF_HOSTS) is True


def test_host_allowed_ignores_whitespace_around_the_url():
    assert common.host_allowed("  https://knf.vu.lt/naujienos  ", common.KNF_HOSTS) is True


def test_host_allowed_ignores_a_newline_smuggled_into_the_url():
    # urlsplit strips tabs and newlines outright, so the verdict
    # is taken on the same host the request would go to
    assert common.host_allowed("https://knf.vu.lt\n/naujienos", common.KNF_HOSTS) is True


def test_host_allowed_refuses_a_newline_that_extends_the_host():
    assert common.host_allowed("https://knf.vu.lt\n.evil.example.org/x", common.KNF_HOSTS) is False


def test_host_allowed_refuses_a_none_url_instead_of_crashing():
    assert common.host_allowed(None, common.KNF_HOSTS) is False


def test_host_allowed_refuses_a_bytes_url():
    # A bytes URL parses to a bytes scheme, which matches
    # neither "http" nor "https" — it fails closed
    assert common.host_allowed(b"https://knf.vu.lt/naujienos", common.KNF_HOSTS) is False


@pytest.mark.parametrize("url", ["", "   ", "/naujienos", "naujienos/x", "?q=1", "#frag"])
def test_host_allowed_refuses_a_url_with_no_scheme_at_all(url):
    assert common.host_allowed(url, common.KNF_HOSTS) is False


def test_host_allowed_refuses_everything_when_the_allowlist_is_empty():
    assert common.host_allowed("https://knf.vu.lt/naujienos", frozenset()) is False


@pytest.mark.parametrize("hosts", [
    ["knf.vu.lt"], ("knf.vu.lt",), {"knf.vu.lt"}, {"knf.vu.lt": True},
])
def test_host_allowed_accepts_any_container_as_the_allowlist(hosts):
    assert common.host_allowed("https://knf.vu.lt/x", hosts) is True


def test_host_allowed_refuses_a_trailing_dot_host():
    # "knf.vu.lt." resolves to the same site but is not the same
    # string; the allowlist fails CLOSED, which is the safe way
    assert common.host_allowed("https://knf.vu.lt./naujienos", common.KNF_HOSTS) is False


def test_host_allowed_refuses_a_percent_encoded_host():
    assert common.host_allowed("https://knf%2Evu%2Elt/naujienos", common.KNF_HOSTS) is False


def test_host_allowed_refuses_a_homograph_that_only_normalises_to_the_real_host():
    # A fullwidth full stop NFKC-normalises to "." — urlsplit
    # raises ValueError rather than hand it on, and the except
    # arm answers False
    assert common.host_allowed("https://knf.vu.lt．/x", common.KNF_HOSTS) is False


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x",
    "http://[::1]/x",
    "http://[::ffff:169.254.169.254]/x",
    "http://2130706433/x",
    "http://0177.0.0.1/x",
    "http://localhost/x",
    "http://knfapp-backend/x",
])
def test_host_allowed_refuses_every_shape_of_an_internal_address(url):
    assert common.host_allowed(url, common.KNF_HOSTS) is False


def test_host_allowed_refuses_an_unparsable_ipv6_literal():
    assert common.host_allowed("https://[::1/x", common.KNF_HOSTS) is False


@pytest.mark.parametrize("url", [
    "https://knf.vu.lt:0/x", "https://knf.vu.lt:99999/x", "https://knf.vu.lt:/x",
])
def test_host_allowed_ignores_the_port_however_absurd(url):
    # Both sites serve on the defaults; the port is not part of
    # the identity the allowlist is about
    assert common.host_allowed(url, common.KNF_HOSTS) is True


def test_host_allowed_keeps_the_three_page_allowlists_apart():
    assert common.host_allowed("https://vu.lt/x", common.KNF_HOSTS) is False
    assert common.host_allowed("https://knf.vu.lt/x", common.VU_HOSTS) is False
    assert common.host_allowed("https://knf.vu.lt/x", common.SCHEDULE_HOSTS) is False
    assert common.host_allowed("https://tvarkarasciai.vu.lt/x", common.VU_HOSTS) is False


def test_host_allowed_does_not_treat_the_image_allowlist_as_a_page_allowlist():
    # newshub serves images for both sites and pages for neither
    assert common.host_allowed("https://newshub.vu.lt/x.jpg", common.IMAGE_HOSTS) is True
    assert common.host_allowed("https://newshub.vu.lt/x.jpg", common.KNF_HOSTS) is False
    assert common.host_allowed("https://newshub.vu.lt/x.jpg", common.VU_HOSTS) is False




# ===========================================================
# normalise_url — the dedup key
# ===========================================================


def test_normalise_url_drops_the_port():
    assert common.normalise_url("https://knf.vu.lt:8443/naujienos/x") == \
        "https://knf.vu.lt/naujienos/x"


def test_normalise_url_drops_credentials_instead_of_storing_them():
    assert common.normalise_url("https://vardas:slaptazodis@knf.vu.lt/n/x") == \
        "https://knf.vu.lt/n/x"


def test_normalise_url_drops_a_path_parameter():
    # urlunparse is handed "" for params, so ";sessionid=…" on
    # the last segment never reaches the stored source_url
    assert common.normalise_url("https://knf.vu.lt/n/x;sessionid=abc") == "https://knf.vu.lt/n/x"


@pytest.mark.parametrize("scheme", ["http", "ftp", "gopher", "HTTPS"])
def test_normalise_url_forces_https_whatever_the_scheme_was(scheme):
    assert common.normalise_url(f"{scheme}://knf.vu.lt/n/x") == "https://knf.vu.lt/n/x"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "mailto:info@knf.vu.lt", "data:text/html,x"])
def test_normalise_url_leaves_a_scheme_only_reference_alone(url):
    # No netloc means no host to canonicalise — it comes back
    # stripped, never turned into an https URL that looks real
    assert common.normalise_url(url) == url


def test_normalise_url_canonicalises_a_protocol_relative_link():
    assert common.normalise_url("//www.knf.vu.lt/naujienos/x/") == "https://knf.vu.lt/naujienos/x"


def test_normalise_url_strips_only_one_www_label():
    assert common.normalise_url("https://www.www.knf.vu.lt/x") == "https://www.knf.vu.lt/x"


def test_normalise_url_keeps_a_host_that_merely_starts_with_www():
    assert common.normalise_url("https://wwwknf.vu.lt/x") == "https://wwwknf.vu.lt/x"


def test_normalise_url_leaves_no_host_behind_when_www_was_the_whole_host():
    # Degenerate, but it must not produce something that looks
    # like a real faculty link
    result = common.normalise_url("http://www./x")

    assert (urlparse(result).hostname or "") == ""
    assert common.host_allowed(result, common.KNF_HOSTS) is False


def test_normalise_url_never_turns_an_ip_literal_into_an_allowlisted_host():
    for url in ("https://[::1]/x", "http://169.254.169.254/x", "http://127.0.0.1/x"):
        assert common.host_allowed(common.normalise_url(url), common.KNF_HOSTS) is False


@pytest.mark.parametrize("url, expected", [
    ("https://knf.vu.lt/n/x/", "https://knf.vu.lt/n/x"),
    ("https://knf.vu.lt/n/x//", "https://knf.vu.lt/n/x"),
    ("https://knf.vu.lt//", "https://knf.vu.lt/"),
    ("https://knf.vu.lt/n//x/", "https://knf.vu.lt/n//x"),
])
def test_normalise_url_strips_trailing_slashes_without_losing_the_root(url, expected):
    assert common.normalise_url(url) == expected


def test_normalise_url_drops_a_bare_hash():
    assert common.normalise_url("https://knf.vu.lt/n/x#") == "https://knf.vu.lt/n/x"


def test_normalise_url_drops_a_fragment_that_looks_like_a_query():
    assert common.normalise_url("https://knf.vu.lt/n/x#?utm_source=fb") == "https://knf.vu.lt/n/x"


def test_normalise_url_re_encodes_a_space_the_same_way_whichever_form_it_arrived_in():
    plus = common.normalise_url("https://knf.vu.lt/paieska?q=studiju pradzia")
    encoded = common.normalise_url("https://knf.vu.lt/paieska?q=studiju%20pradzia")

    assert plus == encoded == "https://knf.vu.lt/paieska?q=studiju+pradzia"


def test_normalise_url_percent_encodes_a_lithuanian_query_but_leaves_the_path_alone():
    assert common.normalise_url("https://knf.vu.lt/naujienos/ąžuolas?q=ą") == \
        "https://knf.vu.lt/naujienos/ąžuolas?q=%C4%85"


def test_normalise_url_keeps_every_copy_of_a_repeated_parameter():
    assert common.normalise_url("https://knf.vu.lt/n?tag=a&tag=b") == \
        "https://knf.vu.lt/n?tag=a&tag=b"


def test_normalise_url_does_not_sort_the_query_so_a_reordered_link_is_a_second_key():
    # Worth knowing: the dedup key collapses campaign tags and
    # www, but two links whose parameters arrive in a different
    # ORDER are two rows
    assert common.normalise_url("https://knf.vu.lt/n?a=1&b=2") != \
        common.normalise_url("https://knf.vu.lt/n?b=2&a=1")


@pytest.mark.parametrize("query, expected", [
    ("utm=1", "https://knf.vu.lt/n?utm=1"),
    ("utmsource=fb", "https://knf.vu.lt/n?utmsource=fb"),
    ("referrer=fb", "https://knf.vu.lt/n?referrer=fb"),
    ("reference=7", "https://knf.vu.lt/n?reference=7"),
    ("prefix_ref=7", "https://knf.vu.lt/n?prefix_ref=7"),
])
def test_normalise_url_keeps_a_parameter_that_merely_looks_like_a_tracker(query, expected):
    assert common.normalise_url(f"https://knf.vu.lt/n?{query}") == expected


@pytest.mark.parametrize("query", ["utm_source", "FBCLID", "_ga", "Ref"])
def test_normalise_url_drops_a_tracking_parameter_that_carries_no_value(query):
    assert common.normalise_url(f"https://knf.vu.lt/n?{query}") == "https://knf.vu.lt/n"


def test_normalise_url_keeps_a_real_parameter_that_carries_no_value():
    # keep_blank_values is what stops "?q" becoming a different
    # page from "?q="
    assert common.normalise_url("https://knf.vu.lt/paieska?q") == "https://knf.vu.lt/paieska?q="


def test_normalise_url_drops_every_tracker_and_leaves_an_empty_query_behind():
    assert common.normalise_url("https://knf.vu.lt/n/x?utm_source=fb&fbclid=1&gclid=2&ref=3") == \
        "https://knf.vu.lt/n/x"


def test_normalise_url_collapses_a_whitespace_only_value_to_the_empty_string():
    assert common.normalise_url("   ") == ""
    assert common.normalise_url("\t\n ") == ""


@pytest.mark.parametrize("value", [None, "", 0, [], {}])
def test_normalise_url_hands_back_any_falsy_value_untouched(value):
    assert common.normalise_url(value) == value


@pytest.mark.parametrize("url", [
    "http://www.knf.vu.lt/naujienos/x/?utm_source=fb#top",
    "https://knf.vu.lt",
    "https://knf.vu.lt///",
    "https://vardas:pw@knf.vu.lt:8443/n/x/",
    "https://knf.vu.lt/n?q=studiju pradzia",
    "https://knf.vu.lt/n?q=%20",
    "https://knf.vu.lt/n/x;sessionid=abc",
    "//www.knf.vu.lt/n/x/",
    "/naujienos/x",
    "naujienos/x",
    "https://knf.vu.lt/naujienos/ąžuolas?q=ą",
    "https://knf.vu.lt/n?tag=a&tag=b",
    "  http://[::1  ",
    "",
])
def test_normalise_url_is_idempotent_on_every_shape(url):
    # The migration re-ran this over existing rows, and every
    # run re-normalises URLs it already stored: a second pass
    # must never produce a third key
    once = common.normalise_url(url)

    assert common.normalise_url(once) == once


def test_a_listing_link_and_its_post_redirect_share_the_key_but_a_sibling_does_not():
    same = {
        common.normalise_url("https://knf.vu.lt/naujienos/studiju-pradzia/"),
        common.normalise_url("https://www.knf.vu.lt/naujienos/studiju-pradzia?fbclid=1"),
        common.normalise_url("HTTP://KNF.VU.LT/naujienos/studiju-pradzia#turinys"),
    }
    other = common.normalise_url("https://knf.vu.lt/naujienos/studiju-pabaiga")

    assert len(same) == 1
    assert other not in same




# ===========================================================
# fetch — the guarded GET
# ===========================================================


@responses.activate
def test_fetch_refuses_a_none_url_without_sending_a_packet(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert common.fetch(None, common.KNF_HOSTS) is None
    assert len(responses.calls) == 0
    assert "Refusing to fetch off-allowlist URL" in caplog.text


@responses.activate
def test_fetch_refuses_an_empty_url_without_sending_a_packet():
    assert common.fetch("", common.KNF_HOSTS) is None
    assert len(responses.calls) == 0


@responses.activate
def test_fetch_refuses_every_url_when_the_allowlist_is_empty():
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", content_type="text/html")

    assert common.fetch(PAGE_URL, frozenset()) is None
    assert len(responses.calls) == 0


@responses.activate
def test_fetch_logs_the_refused_url_exactly_once(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    common.fetch("http://169.254.169.254/latest/meta-data/", common.KNF_HOSTS)

    refusals = [r for r in caplog.records if "off-allowlist URL" in r.getMessage()]
    assert len(refusals) == 1


@responses.activate
def test_fetch_hands_back_a_tuple_of_bytes_and_a_string():
    responses.add(responses.GET, PAGE_URL, body=b"<html>lt</html>", content_type="text/html")

    result = common.fetch(PAGE_URL, common.KNF_HOSTS)

    assert isinstance(result, tuple) and len(result) == 2
    assert isinstance(result[0], bytes)
    assert isinstance(result[1], str)


@responses.activate
def test_fetch_sends_a_get_with_no_body():
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", content_type="text/html")

    common.fetch(PAGE_URL, common.KNF_HOSTS)

    assert responses.calls[0].request.method == "GET"
    assert responses.calls[0].request.body is None


@responses.activate
def test_fetch_merges_params_into_a_url_that_already_carries_a_query():
    responses.add(responses.GET, "https://knf.vu.lt/naujienos",
                  body=b"<html></html>", content_type="text/html")

    _, final_url = common.fetch("https://knf.vu.lt/naujienos?kalba=lt", common.KNF_HOSTS,
                                params={"page": 2})

    sent = responses.calls[0].request.url
    assert "kalba=lt" in sent and "page=2" in sent
    # The caller stores the URL it actually landed on, query included
    assert "page=2" in final_url


@responses.activate
def test_fetch_lets_an_extra_header_override_the_user_agent():
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", content_type="text/html")

    common.fetch(PAGE_URL, common.KNF_HOSTS, extra_headers={"User-Agent": "kitas/9"})

    assert responses.calls[0].request.headers["User-Agent"] == "kitas/9"


@responses.activate
def test_fetch_ignores_an_empty_extra_headers_mapping():
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", content_type="text/html")

    common.fetch(PAGE_URL, common.KNF_HOSTS, extra_headers={})

    assert responses.calls[0].request.headers["User-Agent"] == common.USER_AGENT


@responses.activate
def test_fetch_advertises_a_content_type_the_caller_invented():
    responses.add(responses.GET, PAGE_URL, body=b"<rss/>", content_type="application/rss+xml")

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS, content_types=("application/rss+xml",))

    assert body == b"<rss/>"
    assert responses.calls[0].request.headers["Accept"] == "application/rss+xml"


@responses.activate
def test_fetch_with_no_acceptable_types_refuses_anything_declared(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", content_type="text/html")

    assert common.fetch(PAGE_URL, common.KNF_HOSTS, content_types=()) is None
    assert "Unexpected Content-Type" in caplog.text


@responses.activate
def test_fetch_with_no_acceptable_types_still_takes_an_undeclared_body():
    # The check only fires on what the server DECLARED; nothing
    # declared is still the benefit of the doubt
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", content_type=None)

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS, content_types=())

    assert body == b"<html></html>"


@responses.activate
def test_fetch_treats_a_header_that_is_only_parameters_as_undeclared():
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>",
                  content_type="; charset=utf-8")

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS)

    assert body == b"<html></html>"


@responses.activate
def test_fetch_accepts_the_second_type_on_the_list_too():
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>",
                  content_type="application/xhtml+xml")

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS)

    assert body == b"<html></html>"


@responses.activate
def test_fetch_refuses_a_type_that_only_starts_like_an_allowed_one(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    responses.add(responses.GET, PAGE_URL, body=b"x", content_type="text/htmlx")

    assert common.fetch(PAGE_URL, common.KNF_HOSTS) is None
    assert "Unexpected Content-Type text/htmlx" in caplog.text


@responses.activate
def test_fetch_reads_nothing_at_all_when_the_type_is_wrong(monkeypatch):
    # The type check runs BEFORE the body budget: an unexpected
    # 30 MB PDF must never be pulled down first
    response = _FakeResponse(PAGE_URL, [b"%PDF-1.4"], content_type="application/pdf")
    monkeypatch.setattr(common, "get_session", lambda: _FakeWire(response))

    assert common.fetch(PAGE_URL, common.KNF_HOSTS) is None
    assert response.reads == 0
    assert response.closed is True


def test_fetch_reads_nothing_when_the_redirect_left_the_allowlist(monkeypatch):
    response = _FakeResponse("https://evil.example.org/collect", [b"<html></html>"])
    monkeypatch.setattr(common, "get_session", lambda: _FakeWire(response))

    assert common.fetch(PAGE_URL, common.KNF_HOSTS) is None
    assert response.reads == 0
    assert response.closed is True


@responses.activate
def test_fetch_refuses_a_redirect_onto_the_metadata_service(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    responses.add(responses.GET, "https://knf.vu.lt/go", status=302,
                  headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    responses.add(responses.GET, "http://169.254.169.254/latest/meta-data/",
                  body=b"iam-role", content_type="text/html")

    assert common.fetch("https://knf.vu.lt/go", common.KNF_HOSTS) is None
    assert "Refusing redirect off the allowlist" in caplog.text


@responses.activate
def test_fetch_judges_only_the_final_hop_of_a_redirect_chain():
    # Worth knowing: the re-check is on resp.url, so a chain
    # that BOUNCES through a host off the allowlist and lands
    # back on it is accepted — the intermediate host was still
    # contacted
    responses.add(responses.GET, "https://knf.vu.lt/go", status=302,
                  headers={"Location": "https://tracker.example.org/hop"})
    responses.add(responses.GET, "https://tracker.example.org/hop", status=302,
                  headers={"Location": "https://knf.vu.lt/naujienos/x"})
    responses.add(responses.GET, "https://knf.vu.lt/naujienos/x",
                  body=b"<html>lt</html>", content_type="text/html")

    body, final_url = common.fetch("https://knf.vu.lt/go", common.KNF_HOSTS)

    assert body == b"<html>lt</html>"
    assert final_url == "https://knf.vu.lt/naujienos/x"
    assert len(responses.calls) == 3


@responses.activate
def test_fetch_accepts_a_redirect_that_only_swaps_www_for_the_bare_host():
    responses.add(responses.GET, "https://www.knf.vu.lt/n/x", status=301,
                  headers={"Location": "https://knf.vu.lt/n/x"})
    responses.add(responses.GET, "https://knf.vu.lt/n/x", body=b"<html></html>",
                  content_type="text/html")

    body, final_url = common.fetch("https://www.knf.vu.lt/n/x", common.KNF_HOSTS)

    assert body == b"<html></html>"
    assert final_url == "https://knf.vu.lt/n/x"


@responses.activate
def test_fetch_returns_an_empty_body_when_the_budget_is_zero(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    responses.add(responses.GET, PAGE_URL, body=b"<html>daug baitu</html>",
                  content_type="text/html")

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS, max_bytes=0)

    assert body == b""
    assert "truncated" in caplog.text


@responses.activate
def test_fetch_keeps_exactly_one_byte_when_that_is_the_budget():
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", content_type="text/html")

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS, max_bytes=1)

    assert body == b"<"


@responses.activate
def test_fetch_keeps_a_body_one_byte_under_the_budget_whole():
    responses.add(responses.GET, PAGE_URL, body=b"x" * 63, content_type="text/html")

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS, max_bytes=64)

    assert body == b"x" * 63


def test_fetch_cuts_a_single_chunk_that_overshoots_the_budget_on_its_own(monkeypatch):
    # The break happens AFTER the chunk is appended, so the
    # slice is the only thing keeping the promise
    response = _FakeResponse(PAGE_URL, [b"y" * 5000])
    monkeypatch.setattr(common, "get_session", lambda: _FakeWire(response))

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS, max_bytes=10)

    assert body == b"y" * 10


def test_fetch_skips_an_empty_keepalive_chunk_without_spending_the_budget(monkeypatch):
    # A chunked server sends empty keep-alive chunks between real
    # ones: counted, they would cut a page short of its budget
    response = _FakeResponse(PAGE_URL, [b"", b"abc", None, b"", b"def", b""])
    monkeypatch.setattr(common, "get_session", lambda: _FakeWire(response))

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS, max_bytes=6)

    assert body == b"abcdef"


def test_fetch_stops_reading_the_moment_the_budget_is_spent(monkeypatch):
    # The chunks after the cap are never pulled: an endless body
    # must not swallow the run
    response = _FakeResponse(PAGE_URL, [b"a" * 8, b"b" * 8, ValueError("read past the cap")])
    monkeypatch.setattr(common, "get_session", lambda: _FakeWire(response))

    body, _ = common.fetch(PAGE_URL, common.KNF_HOSTS, max_bytes=16)

    assert body == b"a" * 8 + b"b" * 8


def test_fetch_loses_the_partial_body_when_the_stream_breaks_midway(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    broken = requests.exceptions.ChunkedEncodingError("connection reset")
    response = _FakeResponse(PAGE_URL, [b"<html><body>pradzia", broken])
    monkeypatch.setattr(common, "get_session", lambda: _FakeWire(response))

    assert common.fetch(PAGE_URL, common.KNF_HOSTS) is None
    assert response.closed is True
    assert "Failed to fetch" in caplog.text


def test_fetch_closes_the_response_even_when_the_failure_escapes(monkeypatch):
    # Only requests' own exceptions are swallowed; anything else
    # belongs to the scraper's handler — but the socket is still
    # given back on the way out
    response = _FakeResponse(PAGE_URL, [b"<html>", MemoryError("out of memory")])
    monkeypatch.setattr(common, "get_session", lambda: _FakeWire(response))

    with pytest.raises(MemoryError):
        common.fetch(PAGE_URL, common.KNF_HOSTS)

    assert response.closed is True


def test_fetch_closes_a_response_it_read_to_the_end(monkeypatch):
    response = _FakeResponse(PAGE_URL, [b"<html></html>"])
    monkeypatch.setattr(common, "get_session", lambda: _FakeWire(response))

    common.fetch(PAGE_URL, common.KNF_HOSTS)

    assert response.closed is True


def test_fetch_never_touches_the_session_for_an_off_allowlist_url(monkeypatch):
    def _explode():  # pragma: no cover - the point is that it is never called
        raise AssertionError("fetch built a session for a refused URL")

    monkeypatch.setattr(common, "get_session", _explode)

    assert common.fetch("https://evil.example.org/x", common.KNF_HOSTS) is None


def test_fetch_hands_the_callers_own_timeout_straight_through(monkeypatch):
    wire = _FakeWire(_FakeResponse(PAGE_URL, [b"<html></html>"]))
    monkeypatch.setattr(common, "get_session", lambda: wire)

    common.fetch(PAGE_URL, common.KNF_HOSTS, timeout=(1, 2))

    assert wire.seen["timeout"] == (1, 2)
    assert wire.seen["stream"] is True
    assert wire.seen["url"] == PAGE_URL


def test_fetch_passes_params_of_none_through_untouched(monkeypatch):
    wire = _FakeWire(_FakeResponse(PAGE_URL, [b"<html></html>"]))
    monkeypatch.setattr(common, "get_session", lambda: wire)

    common.fetch(PAGE_URL, common.KNF_HOSTS)

    assert wire.seen["params"] is None


@responses.activate
def test_the_pooled_session_carries_a_cookie_from_one_page_to_the_next():
    # One session for the whole run means the second page is
    # fetched as the same visitor — a property of pooling worth
    # knowing about, since it outlives a single scrape
    responses.add(responses.GET, "https://knf.vu.lt/naujienos", body=b"<html></html>",
                  content_type="text/html",
                  headers={"Set-Cookie": "SSESS=abc; Path=/"})
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", content_type="text/html")

    common.fetch("https://knf.vu.lt/naujienos", common.KNF_HOSTS)
    common.fetch(PAGE_URL, common.KNF_HOSTS)

    assert "SSESS=abc" in responses.calls[1].request.headers.get("Cookie", "")


@responses.activate
@pytest.mark.parametrize("status", [402, 405, 418, 429, 451, 500, 504])
def test_fetch_answers_none_for_any_status_requests_calls_an_error(status):
    responses.add(responses.GET, PAGE_URL, body=b"<html></html>", status=status,
                  content_type="text/html")

    assert common.fetch(PAGE_URL, common.KNF_HOSTS) is None




# ===========================================================
# validate_image_url — the scraped <img> gate
# ===========================================================


@pytest.mark.parametrize("src", [
    None, "", " ", "\t", "\n", "\r\n", "   \t  ",
    "\u00a0",  # the non-breaking space a CMS editor pastes
    "\u2003",
])
def test_an_image_src_that_is_only_whitespace_is_dropped(src):
    # urljoin would resolve a blank src to the ARTICLE PAGE and
    # store an HTML document as image_url
    assert common.validate_image_url(PAGE_URL, src) is None


# The blank-src guard exists because urljoin resolves "" to the
# ARTICLE PAGE, which would store an HTML document as image_url
# and stop the caller reaching its next <img> candidate. These
# five srcs — every one of them a real lazy-load placeholder —
# resolve to exactly the same place and are guarded with it:
# neither a host nor a path is an empty src by another spelling.
@pytest.mark.parametrize("src", ["#", "?v=2", "//", "https://", "https:"])
def test_an_empty_shaped_image_src_must_not_resolve_to_the_article_page(src):
    resolved = common.validate_image_url(PAGE_URL, src)

    assert resolved is None or not resolved.startswith(PAGE_URL)


def test_a_relative_src_resolves_against_the_directory_of_the_page():
    assert common.validate_image_url("https://knf.vu.lt/naujienos/x", "kitas.jpg") == \
        "https://knf.vu.lt/naujienos/kitas.jpg"


def test_a_dot_segment_src_resolves_the_way_a_browser_would():
    assert common.validate_image_url("https://knf.vu.lt/naujienos/2026/x", "../paveiksliukas.jpg") == \
        "https://knf.vu.lt/naujienos/paveiksliukas.jpg"


def test_a_traversal_src_cannot_climb_above_the_host():
    assert common.validate_image_url(PAGE_URL, "/../../../etc/passwd.jpg") == \
        "https://knf.vu.lt/etc/passwd.jpg"


def test_a_percent_encoded_traversal_is_left_encoded():
    assert common.validate_image_url("https://knf.vu.lt/n/a", "..%2F..%2Fx.jpg") == \
        "https://knf.vu.lt/n/..%2F..%2Fx.jpg"


def test_an_absolute_src_ignores_the_page_it_was_found_on():
    assert common.validate_image_url("https://vu.lt/naujienos/y", "https://newshub.vu.lt/x.jpg") == \
        "https://newshub.vu.lt/x.jpg"


def test_a_protocol_relative_src_inherits_the_pages_scheme():
    assert common.validate_image_url("https://knf.vu.lt/n/a", "//newshub.vu.lt/x.jpg") == \
        "https://newshub.vu.lt/x.jpg"
    # …including http, which is left as it was found
    assert common.validate_image_url("http://knf.vu.lt/n/a", "//newshub.vu.lt/x.jpg") == \
        "http://newshub.vu.lt/x.jpg"


def test_a_page_url_of_none_still_lets_an_absolute_src_through():
    assert common.validate_image_url(None, "https://knf.vu.lt/x.jpg") == "https://knf.vu.lt/x.jpg"


def test_a_page_url_of_none_drops_a_relative_src():
    assert common.validate_image_url(None, "x.jpg") is None


def test_an_empty_page_url_still_lets_an_absolute_src_through():
    assert common.validate_image_url("", "https://knf.vu.lt/x.jpg") == "https://knf.vu.lt/x.jpg"


def test_an_unparsable_page_url_drops_the_image(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert common.validate_image_url("https://[::1/n/a", "x.jpg") is None
    assert "Unparsable image src" in caplog.text


def test_an_unparsable_src_drops_the_image(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert common.validate_image_url(PAGE_URL, "https://[oops/x.jpg") is None
    assert "Unparsable image src" in caplog.text


def test_an_image_url_one_byte_over_the_cap_is_dropped(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    prefix = "https://knf.vu.lt/"
    src = prefix + "a" * (common.MAX_IMAGE_URL_LENGTH - len(prefix) + 1)

    assert len(src) == common.MAX_IMAGE_URL_LENGTH + 1
    assert common.validate_image_url(PAGE_URL, src) is None
    assert "over 2048 chars" in caplog.text


def test_the_cap_counts_the_resolved_url_and_not_the_src():
    # A five-character src can still blow the cap once it is
    # resolved against a very long article path
    long_page = "https://knf.vu.lt/" + "a" * 2100 + "/"

    assert common.validate_image_url(long_page, "x.jpg") is None


def test_the_cap_is_checked_before_the_allowlist(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    src = "https://evil.example.org/" + "a" * common.MAX_IMAGE_URL_LENGTH

    assert common.validate_image_url(PAGE_URL, src) is None
    assert "over 2048 chars" in caplog.text
    assert "off-allowlist" not in caplog.text


@pytest.mark.parametrize("src", [
    "data:image/png;base64,iVBORw0KGgo=",
    "DATA:image/png;base64,iVBORw0KGgo=",
    "  data:image/gif;base64,R0lGOD  ",
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "file:///etc/passwd",
    "ftp://knf.vu.lt/x.jpg",
    "about:blank",
])
def test_an_image_src_with_a_scheme_that_is_not_http_is_dropped(src, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert common.validate_image_url(PAGE_URL, src) is None
    assert "Dropping off-allowlist image URL" in caplog.text


@pytest.mark.parametrize("src", ["http://", "http:x.jpg", "http:"])
def test_an_image_src_that_names_a_scheme_but_no_host_is_dropped(src):
    # A scheme that differs from the page's makes urljoin keep
    # the src as-is, and there is no host on it to allow
    assert common.validate_image_url(PAGE_URL, src) is None


def test_a_triple_slash_src_resolves_to_the_root_of_the_page_host():
    # Empty authority, absolute path: the host comes from the page
    assert common.validate_image_url(PAGE_URL, "///x.jpg") == "https://knf.vu.lt/x.jpg"


@pytest.mark.parametrize("src", [
    "https://evil.example.org/x.jpg",
    "https://knf.vu.lt.evil.example.org/x.jpg",
    "https://evil.example.org/x.jpg?knf.vu.lt",
    "https://knf.vu.lt@evil.example.org/x.jpg",
    "http://169.254.169.254/latest/meta-data/x.jpg",
    "https://tvarkarasciai.vu.lt/x.jpg",
    "https://knf.vu.lt./x.jpg",
])
def test_an_image_src_off_the_image_allowlist_is_dropped(src):
    assert common.validate_image_url(PAGE_URL, src) is None


@pytest.mark.parametrize("host", [
    "knf.vu.lt", "www.knf.vu.lt", "vu.lt", "www.vu.lt", "newshub.vu.lt", "www.newshub.vu.lt",
])
def test_every_host_on_the_image_allowlist_survives(host):
    assert common.validate_image_url(PAGE_URL, f"https://{host}/sites/x.jpg") == \
        f"https://{host}/sites/x.jpg"


def test_the_resolved_url_is_stored_exactly_as_resolved():
    # validate_image_url is a gate, not a canonicaliser — the
    # host case and the query survive into news_posts.image_url
    assert common.validate_image_url(PAGE_URL, "https://KNF.VU.LT/X.JPG?v=2#a") == \
        "https://KNF.VU.LT/X.JPG?v=2#a"


def test_only_the_ends_of_an_image_src_are_trimmed():
    assert common.validate_image_url(PAGE_URL, "  https://knf.vu.lt/x.jpg\n") == \
        "https://knf.vu.lt/x.jpg"
    # An inner space is left where the source put it
    assert common.validate_image_url(PAGE_URL, "https://knf.vu.lt/a b.jpg") == \
        "https://knf.vu.lt/a b.jpg"


def test_an_image_url_exactly_on_the_cap_is_kept():
    prefix = "https://knf.vu.lt/"
    src = prefix + "a" * (common.MAX_IMAGE_URL_LENGTH - len(prefix))

    assert len(src) == common.MAX_IMAGE_URL_LENGTH
    assert common.validate_image_url(PAGE_URL, src) == src


def test_a_falsy_non_string_src_is_dropped_before_anything_is_stripped():
    assert common.validate_image_url(PAGE_URL, 0) is None
    assert common.validate_image_url(PAGE_URL, False) is None
    assert common.validate_image_url(PAGE_URL, []) is None




# ===========================================================
# push_allowed — the run-shape guard on notifications
# ===========================================================


def test_a_run_with_no_new_rows_still_spends_the_hourly_slot(db, completed_run):
    # Worth knowing: the guard is about the SHAPE of the run, so
    # a zero-row run that asks is charged for the hour
    completed_run("knf")

    assert common.push_allowed(db, "knf", 0, "run-1") is True
    assert common.push_allowed(db, "knf", 1, "run-2") is False


def test_a_negative_new_count_is_not_a_burst(db, completed_run):
    completed_run("knf")

    assert common.push_allowed(db, "knf", -1, "run-1") is True


def test_one_row_under_the_burst_threshold_still_pushes(db, completed_run):
    completed_run("knf")

    assert common.push_allowed(db, "knf", common.PUSH_BURST_THRESHOLD - 1, "run-1") is True


@pytest.mark.parametrize("new_count", [26, 100, 10 ** 6, 2 ** 62])
def test_any_count_over_the_burst_threshold_is_suppressed(db, completed_run, new_count):
    completed_run("knf")

    assert common.push_allowed(db, "knf", new_count, "run-1") is False


def test_the_backfill_suppression_does_not_spend_the_hourly_slot(db, completed_run):
    # The first run says no because it is history; the very next
    # run must not then be told it already pushed
    assert common.push_allowed(db, "knf", 3, "run-1") is False

    completed_run("knf")

    assert common.push_allowed(db, "knf", 3, "run-2") is True


def test_an_ancient_completed_run_is_still_a_predecessor(db, completed_run):
    completed_run("knf", days_ago=400)

    assert common.push_allowed(db, "knf", 3, "run-now") is True


def test_a_running_run_of_the_same_source_is_not_a_predecessor(db, completed_run):
    completed_run("knf", status="running")

    assert common.push_allowed(db, "knf", 3, "run-now") is False


def test_a_source_name_that_differs_only_by_case_is_a_different_source(db, completed_run):
    completed_run("knf")

    assert common.push_allowed(db, "KNF", 3, "run-1") is False
    assert common.push_allowed(db, "knf", 3, "run-2") is True


def test_a_source_of_none_never_pushes(db, completed_run):
    completed_run("knf")

    assert common.push_allowed(db, None, 3, "run-1") is False


def test_a_run_id_of_none_hides_every_earlier_run(db, completed_run):
    # "id != NULL" is never true in SQL, so the predecessor count
    # comes back 0 and the run reads as a first boot. No caller
    # passes None — every scraper opens its run row with a uuid —
    # but the failure mode is the safe one: a push is suppressed,
    # never sent twice
    completed_run("knf")

    assert common.push_allowed(db, "knf", 3, None) is False


def test_push_allowed_answers_a_real_boolean(db, completed_run):
    completed_run("knf")

    assert common.push_allowed(db, "knf", 3, "run-1") is True
    assert common.push_allowed(db, "knf", 3, "run-2") is False


def test_a_push_exactly_on_the_hour_boundary_is_allowed(db, completed_run):
    completed_run("knf")

    with time_machine.travel(FROZEN, tick=False):
        assert common.push_allowed(db, "knf", 1, "run-1") is True

    with time_machine.travel(FROZEN + timedelta(seconds=common.PUSH_MIN_INTERVAL_SECONDS),
                             tick=False):
        assert common.push_allowed(db, "knf", 1, "run-2") is True


def test_a_push_a_hair_under_the_hour_is_suppressed(db, completed_run):
    completed_run("knf")

    with time_machine.travel(FROZEN, tick=False):
        assert common.push_allowed(db, "knf", 1, "run-1") is True

    with time_machine.travel(FROZEN + timedelta(seconds=3599.999), tick=False):
        assert common.push_allowed(db, "knf", 1, "run-2") is False


def test_a_suppressed_attempt_does_not_slide_the_hourly_window(db, completed_run):
    # The window is measured from the last ALLOWED push, so an
    # attempt halfway through cannot postpone the next one
    completed_run("knf")

    with time_machine.travel(FROZEN, tick=False):
        assert common.push_allowed(db, "knf", 1, "run-1") is True

    with time_machine.travel(FROZEN + timedelta(seconds=1800), tick=False):
        assert common.push_allowed(db, "knf", 1, "run-2") is False

    with time_machine.travel(FROZEN + timedelta(seconds=3600), tick=False):
        assert common.push_allowed(db, "knf", 1, "run-3") is True


def test_a_clock_that_steps_backwards_never_opens_the_gate(db, completed_run):
    completed_run("knf")

    with time_machine.travel(FROZEN, tick=False):
        assert common.push_allowed(db, "knf", 1, "run-1") is True

    with time_machine.travel(FROZEN - timedelta(days=1), tick=False):
        assert common.push_allowed(db, "knf", 1, "run-2") is False


def test_a_burst_suppressed_inside_the_hour_leaves_the_window_where_it_was(db, completed_run):
    completed_run("knf")

    with time_machine.travel(FROZEN, tick=False):
        assert common.push_allowed(db, "knf", 1, "run-1") is True
        assert common.push_allowed(db, "knf", 500, "run-2") is False

    with time_machine.travel(FROZEN + timedelta(seconds=3600), tick=False):
        assert common.push_allowed(db, "knf", 1, "run-3") is True


def test_a_closed_connection_fails_open_but_still_obeys_the_other_two_guards(db, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    db.close()

    # Fail open on the count, but a burst is still a burst…
    assert common.push_allowed(db, "knf", 500, "run-1") is False
    # …and the hour still holds
    assert common.push_allowed(db, "knf", 1, "run-2") is True
    assert common.push_allowed(db, "knf", 1, "run-3") is False


def test_a_database_error_is_logged_with_its_traceback(db, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    db.close()

    common.push_allowed(db, "knf", 1, "run-1")

    warnings = [r for r in caplog.records if "Could not count earlier" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("no such table: scraper_runs"),
    sqlite3.DatabaseError("database disk image is malformed"),
    sqlite3.ProgrammingError("Cannot operate on a closed database."),
])
def test_every_sqlite_error_fails_open(error):
    assert common.push_allowed(_CountingDb(error=error), "knf", 1, "run-1") is True


def test_an_error_that_is_not_a_sqlite_error_is_left_to_the_caller():
    # The except arm is narrow on purpose: a None connection is
    # a programming mistake, not a broken database
    with pytest.raises(AttributeError):
        common.push_allowed(None, "knf", 1, "run-1")


def test_only_one_of_eight_threads_racing_one_source_may_push():
    gate = threading.Barrier(8)
    verdicts = []
    fake_db = _CountingDb(count=1)

    def race():
        gate.wait()
        verdicts.append(common.push_allowed(fake_db, "knf", 1, str(uuid.uuid4())))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(verdicts) == 8
    assert verdicts.count(True) == 1


def test_two_threads_pushing_different_sources_both_win():
    gate = threading.Barrier(2)
    verdicts = {}
    fake_db = _CountingDb(count=1)

    def race(source):
        gate.wait()
        verdicts[source] = common.push_allowed(fake_db, source, 1, "run-1")

    threads = [threading.Thread(target=race, args=(source,)) for source in ("knf", "vu")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert verdicts == {"knf": True, "vu": True}


def test_the_hourly_clock_is_per_process_so_a_restart_forgives_it(db, completed_run, monkeypatch):
    completed_run("knf")

    assert common.push_allowed(db, "knf", 1, "run-1") is True
    assert common.push_allowed(db, "knf", 1, "run-2") is False

    # What a restart looks like from here: the module global is
    # empty again and the source gets its slot back
    monkeypatch.setattr(common, "_LAST_PUSH", {})

    assert common.push_allowed(db, "knf", 1, "run-3") is True


def test_the_burst_threshold_and_the_interval_are_the_documented_ones():
    assert common.PUSH_BURST_THRESHOLD == 25
    assert common.PUSH_MIN_INTERVAL_SECONDS == 3600
