# -----------------------------------------------------------
#  [*] Tests — scraper/common.py, the shared scraper plumbing
#
#  What this module proves about the half of the scrapers that
#  every source shares (get_session, host_allowed, fetch,
#  normalise_url, run_deadline/deadline_passed, utc_now_naive,
#  parse_source_datetime, sanitise_published_at,
#  mark_run_failed, prune_scraper_runs, load_deleted_urls,
#  validate_image_url, push_allowed, check_yield_drop):
#
#    - fetch() is the SSRF gate, not a convenience wrapper: an
#      off-allowlist URL never leaves the container, and a
#      redirect that lands off the allowlist is dropped even
#      though the request already went out
#    - the four other guards it grew — (connect, read) tuple
#      timeouts, the Content-Type check, the hard byte cap,
#      and bytes-not-text so BeautifulSoup sniffs the charset
#    - every failure path answers None instead of raising:
#      connect timeout, read timeout, connection error, 404,
#      500, a PDF where a page was promised
#    - normalise_url is the dedup key: the listing link, the
#      post-redirect URL and the campaign-tagged share of one
#      article collapse to ONE string, so a second run
#      re-inserts nothing — and a tombstone written in any of
#      those shapes still matches
#    - timestamps: the offset is APPLIED and not dropped
#      (Vilnius news used to land hours in the future), a
#      negative offset parses, and the [now - 5 years, now]
#      clamp keeps a mis-parsed year off the top of the feed
#    - the run bookkeeping: mark_run_failed on its own
#      connection while the caller's is broken, retention that
#      never deletes a source's newest run, and the two
#      run-shape guards (no push for a backfill, a burst or a
#      second push inside the hour; one ERROR line when a
#      run's yield collapses)
#
#  Nothing here touches the network: every response is
#  registered with `responses`, and the clock moves with
#  time_machine instead of sleeping.
# -----------------------------------------------------------


import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses
import time_machine
from bs4 import BeautifulSoup

from app.scraper import common


LOGGER_NAME = "app.scraper.common"

ARTICLE_URL = "https://knf.vu.lt/naujienos/studiju-pradzia"

# A frozen instant every timestamp test reasons from — aware
# UTC so time_machine cannot read it as local time
FROZEN = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
FROZEN_NAIVE = FROZEN.replace(tzinfo=None)

# Small but structurally faithful: the meta charset, the
# og:image and the <time> are the three things the article
# parsers reach for, and the Lithuanian text is what proves
# the body came back as bytes rather than mis-decoded latin-1
ARTICLE_HTML = (
    "<!DOCTYPE html><html lang='lt'><head>"
    "<meta charset='utf-8'>"
    "<meta property='og:image' content='/sites/default/files/naujiena.jpg'>"
    "<title>Studijų pradžia</title></head>"
    "<body><article><h1>Studijų pradžia</h1>"
    "<time datetime='2026-08-29T10:00:00+03:00'>2026-08-29</time>"
    "<p>Rugsėjo 1-ąją į Kauno fakultetą sugrįžta studentai.</p>"
    "</article></body></html>"
).encode("utf-8")




# -----------------------------------------------------------
# clean_module_state
# -----------------------------------------------------------
#
# common.py keeps two module globals that outlive a test: the
# pooled session and the per-source push clock. Both are
# rebound per test through monkeypatch, so the order tests run
# in can never decide whether a push is allowed or which
# session a fetch went through.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_module_state(monkeypatch):
    monkeypatch.setattr(common, "_SESSION", None)
    monkeypatch.setattr(common, "_LAST_PUSH", {})




# -----------------------------------------------------------
# seed_run
# -----------------------------------------------------------
#
#   run_id = seed_run("knf", status="completed", found=40)
#
# One scraper_runs row, written straight through the `db`
# connection because the scrapers' own bookkeeping is what is
# under test — there is no route that can create a run in an
# arbitrary state. `days_ago` places started_at relative to
# now (or to whatever instant time_machine is holding).
# -----------------------------------------------------------

@pytest.fixture
def seed_run(db):

    def _seed(source, status="completed", found=0, new=0, days_ago=0.0, run_id=None,
              started_at=None, finished=True):
        run_id = run_id or str(uuid.uuid4())
        stamp = started_at or (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        db.execute(
            """INSERT INTO scraper_runs
               (id, source, status, articles_found, articles_new, started_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, source, status, found, new, stamp, stamp if finished else None),
        )
        db.commit()
        return run_id

    return _seed




# -----------------------------------------------------------
# _StubResponse / _StubSession
# -----------------------------------------------------------
#
# `responses` fakes the wire, which is the right level for
# everything except the two things that happen BELOW it: the
# keyword arguments fetch hands requests (the tuple timeout
# and stream=True, whose loss would turn the read budget into
# a whole-request one), and a body that arrives as empty
# keep-alive chunks. These two stubs stand in for the pooled
# session in exactly those tests.
# -----------------------------------------------------------

class _StubResponse:

    def __init__(self, url, chunks, content_type="text/html"):
        self.url = url
        self.headers = {"Content-Type": content_type} if content_type is not None else {}
        self._chunks = chunks
        self.closed = False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class _StubSession:

    def __init__(self, response):
        self.response = response
        self.seen = {}

    def get(self, url, **kwargs):
        self.seen = dict(kwargs, url=url)
        return self.response




# ===========================================================
# get_session — the one pooled session
# ===========================================================


def test_get_session_hands_back_the_same_session_every_time():
    first = common.get_session()
    second = common.get_session()
    assert first is second


def test_get_session_mounts_a_retrying_adapter_on_both_schemes():
    session = common.get_session()

    for scheme in ("https://", "http://"):
        adapter = session.adapters[scheme]
        retry = adapter.max_retries
        assert retry.total == 2
        assert retry.backoff_factor == 0.5
        assert set(retry.status_forcelist) == {429, 500, 502, 503, 504}
        # A retried POST would double-submit; the scrapers only GET
        assert set(retry.allowed_methods) == {"GET"}
        # The status is handed back for fetch to judge, never raised
        assert retry.raise_on_status is False


def test_get_session_pools_connections_instead_of_opening_one_per_page():
    adapter = common.get_session().adapters["https://"]
    assert adapter._pool_connections == 4
    assert adapter._pool_maxsize == 8




# ===========================================================
# host_allowed — the allowlist
# ===========================================================


@pytest.mark.parametrize("url, hosts", [
    ("https://knf.vu.lt/naujienos", common.KNF_HOSTS),
    ("https://www.knf.vu.lt/naujienos", common.KNF_HOSTS),
    ("http://knf.vu.lt/naujienos", common.KNF_HOSTS),
    ("https://vu.lt/naujienos", common.VU_HOSTS),
    ("https://www.vu.lt/naujienos", common.VU_HOSTS),
    ("https://tvarkarasciai.vu.lt/api/groups", common.SCHEDULE_HOSTS),
    ("https://newshub.vu.lt/x.jpg", common.IMAGE_HOSTS),
])
def test_host_allowed_accepts_a_host_on_its_own_allowlist(url, hosts):
    assert common.host_allowed(url, hosts) is True


def test_host_allowed_ignores_the_port_and_the_case():
    assert common.host_allowed("https://KNF.VU.LT/naujienos", common.KNF_HOSTS) is True
    assert common.host_allowed("https://knf.vu.lt:8443/naujienos", common.KNF_HOSTS) is True


@pytest.mark.parametrize("url", [
    "https://evil.example.org/naujienos",
    # The two shapes an injected href uses to look like the real host
    "https://knf.vu.lt.evil.example.org/x",
    "https://evil.example.org/?knf.vu.lt",
    "https://notknf.vu.lt/x",
    # A cross-allowlist link: vu.lt is not knf.vu.lt
    "https://vu.lt/naujienos",
    # The classic cloud metadata probe
    "http://169.254.169.254/latest/meta-data/",
])
def test_host_allowed_refuses_a_host_off_the_list(url):
    assert common.host_allowed(url, common.KNF_HOSTS) is False


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "data:text/html,<h1>hi</h1>",
    "javascript:alert(1)",
    "ftp://knf.vu.lt/x",
    "//knf.vu.lt/x",
])
def test_host_allowed_refuses_a_non_http_scheme(url):
    assert common.host_allowed(url, common.KNF_HOSTS) is False


def test_host_allowed_refuses_an_unparsable_url():
    assert common.host_allowed("http://[::1", common.KNF_HOSTS) is False


def test_host_allowed_refuses_a_url_with_no_host_at_all():
    assert common.host_allowed("https:///naujienos", common.KNF_HOSTS) is False


def test_the_image_allowlist_covers_both_page_allowlists():
    # A page host must never fail its own article's image
    assert common.KNF_HOSTS <= common.IMAGE_HOSTS
    assert common.VU_HOSTS <= common.IMAGE_HOSTS
    # …and never a wildcard: the value reaches every guest's <Image>
    assert "*" not in common.IMAGE_HOSTS




# ===========================================================
# normalise_url — the dedup key
# ===========================================================


@pytest.mark.parametrize("value", ["", None])
def test_normalise_url_hands_back_an_empty_value_untouched(value):
    assert common.normalise_url(value) == value


def test_normalise_url_upgrades_the_scheme_and_drops_www():
    assert common.normalise_url("http://www.knf.vu.lt/naujienos/x") == "https://knf.vu.lt/naujienos/x"


def test_normalise_url_lowercases_the_host_but_not_the_path():
    assert common.normalise_url("https://KNF.VU.LT/Naujienos/X") == "https://knf.vu.lt/Naujienos/X"


def test_normalise_url_drops_the_fragment():
    assert common.normalise_url("https://knf.vu.lt/n/x#turinys") == "https://knf.vu.lt/n/x"


def test_normalise_url_drops_the_trailing_slash_but_keeps_the_root():
    assert common.normalise_url("https://knf.vu.lt/naujienos/") == "https://knf.vu.lt/naujienos"
    assert common.normalise_url("https://knf.vu.lt/") == "https://knf.vu.lt/"
    assert common.normalise_url("https://knf.vu.lt") == "https://knf.vu.lt/"
    # Nothing but slashes still has to leave a path behind
    assert common.normalise_url("https://knf.vu.lt///") == "https://knf.vu.lt/"


@pytest.mark.parametrize("tracker", [
    "utm_source=facebook", "utm_medium=social", "utm_campaign=rugsejis",
    "UTM_SOURCE=facebook", "fbclid=IwAR123", "gclid=abc", "mc_cid=1", "mc_eid=2",
    "_ga=GA1.2.3", "ref=newsletter", "REF=newsletter",
])
def test_normalise_url_drops_a_tracking_parameter(tracker):
    assert common.normalise_url(f"https://knf.vu.lt/n/x?{tracker}") == "https://knf.vu.lt/n/x"


def test_normalise_url_keeps_the_parameters_that_identify_the_page():
    assert common.normalise_url("https://knf.vu.lt/naujienos?page=2&utm_source=fb&id=7") == \
        "https://knf.vu.lt/naujienos?page=2&id=7"


def test_normalise_url_keeps_a_blank_but_real_parameter():
    assert common.normalise_url("https://knf.vu.lt/paieska?q=") == "https://knf.vu.lt/paieska?q="


def test_every_shape_of_one_article_link_collapses_to_one_key():
    # The listing link, the share with a campaign tag, the
    # post-redirect www URL and the anchored copy are the same
    # row — this is what stops a second run re-inserting it
    variants = [
        "https://knf.vu.lt/naujienos/studiju-pradzia",
        "http://knf.vu.lt/naujienos/studiju-pradzia/",
        "https://www.knf.vu.lt/naujienos/studiju-pradzia?utm_source=fb&fbclid=IwAR1",
        "https://WWW.KNF.VU.LT/naujienos/studiju-pradzia#pradzia",
        "  https://knf.vu.lt/naujienos/studiju-pradzia  ",
    ]
    assert len({common.normalise_url(v) for v in variants}) == 1


def test_normalise_url_is_idempotent():
    once = common.normalise_url("http://www.knf.vu.lt/naujienos/x/?utm_source=fb#top")
    assert common.normalise_url(once) == once


def test_normalise_url_leaves_a_relative_reference_alone():
    # No host to canonicalise; the caller resolves it first
    assert common.normalise_url("/naujienos/studiju-pradzia") == "/naujienos/studiju-pradzia"
    assert common.normalise_url("  naujienos/x  ") == "naujienos/x"


def test_normalise_url_hands_back_an_unparsable_url_stripped():
    assert common.normalise_url("  http://[::1  ") == "http://[::1"




# ===========================================================
# fetch — the guarded GET
# ===========================================================


@responses.activate
def test_fetch_returns_the_body_bytes_and_the_final_url():
    responses.add(responses.GET, ARTICLE_URL, body=ARTICLE_HTML,
                  content_type="text/html; charset=utf-8")

    body, final_url = common.fetch(ARTICLE_URL, common.KNF_HOSTS)

    assert isinstance(body, bytes)
    assert body == ARTICLE_HTML
    assert final_url == ARTICLE_URL


@responses.activate
def test_fetch_hands_back_bytes_so_beautifulsoup_can_sniff_the_charset():
    # No charset in the header: decoding here would give
    # requests' ISO-8859-1 fallback and mojibake in the feed
    responses.add(responses.GET, ARTICLE_URL, body=ARTICLE_HTML, content_type="text/html")

    body, _ = common.fetch(ARTICLE_URL, common.KNF_HOSTS)
    soup = BeautifulSoup(body, "lxml")

    assert soup.find("h1").get_text() == "Studijų pradžia"


@responses.activate
def test_fetch_sends_the_knfapp_user_agent_and_asks_for_lithuanian():
    responses.add(responses.GET, ARTICLE_URL, body=ARTICLE_HTML, content_type="text/html")

    common.fetch(ARTICLE_URL, common.KNF_HOSTS)

    sent = responses.calls[0].request.headers
    assert sent["User-Agent"] == common.USER_AGENT
    assert "KNFAPP" in sent["User-Agent"]
    assert sent["Accept-Language"] == "lt"
    # The Accept header advertises exactly what the caller can parse
    assert sent["Accept"] == "text/html, application/xhtml+xml"


@responses.activate
def test_fetch_advertises_the_content_types_the_caller_asked_for():
    url = "https://tvarkarasciai.vu.lt/api/groups"
    responses.add(responses.GET, url, body=b"[]", content_type="application/json")

    body, _ = common.fetch(url, common.SCHEDULE_HOSTS, content_types=common.JSON_CONTENT_TYPES)

    assert body == b"[]"
    assert responses.calls[0].request.headers["Accept"] == "application/json, text/json"


@responses.activate
def test_fetch_merges_extra_headers_over_the_defaults():
    responses.add(responses.GET, ARTICLE_URL, body=ARTICLE_HTML, content_type="text/html")

    common.fetch(ARTICLE_URL, common.KNF_HOSTS,
                 extra_headers={"Referer": "https://knf.vu.lt/naujienos", "Accept-Language": "en"})

    sent = responses.calls[0].request.headers
    assert sent["Referer"] == "https://knf.vu.lt/naujienos"
    assert sent["Accept-Language"] == "en"
    assert sent["User-Agent"] == common.USER_AGENT


@responses.activate
def test_fetch_passes_query_parameters_through():
    listing = "https://knf.vu.lt/naujienos"
    responses.add(responses.GET, listing, body=b"<html></html>", content_type="text/html")

    common.fetch(listing, common.KNF_HOSTS, params={"page": 2})

    assert responses.calls[0].request.url == "https://knf.vu.lt/naujienos?page=2"


@responses.activate
def test_fetch_refuses_an_off_allowlist_url_without_sending_a_packet(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert common.fetch("http://169.254.169.254/latest/meta-data/", common.KNF_HOSTS) is None

    assert len(responses.calls) == 0
    assert "Refusing to fetch off-allowlist URL" in caplog.text


@responses.activate
def test_fetch_follows_a_redirect_inside_the_allowlist_and_reports_where_it_landed():
    responses.add(responses.GET, "https://knf.vu.lt/n/x", status=301,
                  headers={"Location": "https://www.knf.vu.lt/n/x"})
    responses.add(responses.GET, "https://www.knf.vu.lt/n/x", body=ARTICLE_HTML,
                  content_type="text/html")

    body, final_url = common.fetch("https://knf.vu.lt/n/x", common.KNF_HOSTS)

    assert body == ARTICLE_HTML
    # The caller stores the POST-redirect URL as the dedup key
    assert final_url == "https://www.knf.vu.lt/n/x"


@responses.activate
def test_fetch_refuses_a_redirect_that_leaves_the_allowlist(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    responses.add(responses.GET, "https://knf.vu.lt/go", status=302,
                  headers={"Location": "https://evil.example.org/collect"})
    responses.add(responses.GET, "https://evil.example.org/collect", body=b"<html></html>",
                  content_type="text/html")

    assert common.fetch("https://knf.vu.lt/go", common.KNF_HOSTS) is None

    assert "Refusing redirect off the allowlist" in caplog.text


@responses.activate
@pytest.mark.parametrize("status", [204, 301, 400, 401, 403, 404, 410, 500, 502, 503])
def test_fetch_gives_up_on_a_status_it_cannot_use(status):
    # 204/301 have no usable body either: a bare redirect with
    # no Location leaves requests with nothing to follow
    responses.add(responses.GET, ARTICLE_URL, body=b"", status=status, content_type="text/html")

    result = common.fetch(ARTICLE_URL, common.KNF_HOSTS)

    if status >= 400:
        assert result is None
    else:
        assert result[0] == b""


@responses.activate
@pytest.mark.parametrize("content_type", [
    "application/pdf", "image/jpeg", "text/plain", "application/json",
    "text/html-ish", "application/octet-stream",
])
def test_fetch_rejects_a_content_type_it_cannot_parse(content_type, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    responses.add(responses.GET, ARTICLE_URL, body=b"%PDF-1.4", content_type=content_type)

    assert common.fetch(ARTICLE_URL, common.KNF_HOSTS) is None
    assert "Unexpected Content-Type" in caplog.text


@responses.activate
def test_fetch_accepts_a_declared_type_whatever_its_charset_and_case():
    responses.add(responses.GET, ARTICLE_URL, body=ARTICLE_HTML,
                  content_type="TEXT/HTML; charset=UTF-8")

    body, _ = common.fetch(ARTICLE_URL, common.KNF_HOSTS)

    assert body == ARTICLE_HTML


@responses.activate
def test_fetch_gives_a_server_that_declares_nothing_the_benefit_of_the_doubt():
    responses.add(responses.GET, ARTICLE_URL, body=ARTICLE_HTML, content_type=None)

    body, _ = common.fetch(ARTICLE_URL, common.KNF_HOSTS)

    assert body == ARTICLE_HTML


@responses.activate
def test_fetch_returns_an_empty_body_rather_than_none():
    # An empty page is not a failure: the caller parses it,
    # finds no articles and says so
    responses.add(responses.GET, ARTICLE_URL, body=b"", content_type="text/html")

    body, final_url = common.fetch(ARTICLE_URL, common.KNF_HOSTS)

    assert body == b""
    assert final_url == ARTICLE_URL


@responses.activate
def test_fetch_truncates_a_body_over_the_cap_and_the_rest_still_parses(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    giant = b"<html><head><title>Naujienos</title></head><body>" + b"x" * 4096
    responses.add(responses.GET, ARTICLE_URL, body=giant, content_type="text/html")

    body, _ = common.fetch(ARTICLE_URL, common.KNF_HOSTS, max_bytes=64)

    assert len(body) == 64
    assert "truncated" in caplog.text
    # The cap is on bytes KEPT — a cut page is still parseable
    assert BeautifulSoup(body, "lxml").find("title").get_text() == "Naujienos"


@responses.activate
def test_fetch_keeps_a_body_that_sits_exactly_on_the_cap():
    responses.add(responses.GET, ARTICLE_URL, body=b"x" * 64, content_type="text/html")

    body, _ = common.fetch(ARTICLE_URL, common.KNF_HOSTS, max_bytes=64)

    assert body == b"x" * 64


@responses.activate
@pytest.mark.parametrize("failure", [
    requests.exceptions.ConnectTimeout("connect timed out"),
    requests.exceptions.ReadTimeout("read timed out"),
    requests.exceptions.ConnectionError("name resolution failed"),
    requests.exceptions.TooManyRedirects("redirect loop"),
    requests.exceptions.ChunkedEncodingError("truncated chunk"),
    requests.exceptions.RequestException("something else entirely"),
])
def test_fetch_answers_none_instead_of_raising_on_a_transport_failure(failure, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    responses.add(responses.GET, ARTICLE_URL, body=failure)

    assert common.fetch(ARTICLE_URL, common.KNF_HOSTS) is None
    assert "Failed to fetch" in caplog.text


def test_fetch_asks_requests_for_a_tuple_timeout_and_a_streamed_body(monkeypatch):
    # A scalar timeout bounds the quiet time between bytes, never
    # the whole request — losing the tuple is how a run wedges
    stub = _StubSession(_StubResponse(ARTICLE_URL, [ARTICLE_HTML]))
    monkeypatch.setattr(common, "get_session", lambda: stub)

    body, final_url = common.fetch(ARTICLE_URL, common.KNF_HOSTS)

    assert stub.seen["timeout"] == (5, 20)
    assert common.DEFAULT_TIMEOUT == (5, 20)
    assert stub.seen["stream"] is True
    assert body == ARTICLE_HTML and final_url == ARTICLE_URL


def test_fetch_skips_empty_keepalive_chunks_and_closes_the_response(monkeypatch):
    response = _StubResponse(ARTICLE_URL, [b"", b"<html>", None, b"</html>"])
    monkeypatch.setattr(common, "get_session", lambda: _StubSession(response))

    body, _ = common.fetch(ARTICLE_URL, common.KNF_HOSTS)

    assert body == b"<html></html>"
    assert response.closed is True


def test_fetch_closes_the_response_even_when_the_type_is_wrong(monkeypatch):
    response = _StubResponse(ARTICLE_URL, [b"%PDF"], content_type="application/pdf")
    monkeypatch.setattr(common, "get_session", lambda: _StubSession(response))

    assert common.fetch(ARTICLE_URL, common.KNF_HOSTS) is None
    assert response.closed is True




# ===========================================================
# run_deadline / deadline_passed — the wall-clock budget
# ===========================================================


def test_a_fresh_budget_has_not_been_spent():
    assert common.deadline_passed(common.run_deadline(60)) is False


def test_a_zero_second_budget_is_already_spent():
    assert common.deadline_passed(common.run_deadline(0)) is True


def test_the_budget_is_measured_on_the_monotonic_clock():
    before = time.monotonic()
    deadline = common.run_deadline(30)
    assert 30 <= deadline - before <= 31


def test_the_budget_expires_once_its_seconds_have_passed():
    with time_machine.travel(FROZEN, tick=False):
        deadline = common.run_deadline(120)
        assert common.deadline_passed(deadline) is False

        with time_machine.travel(FROZEN + timedelta(seconds=119), tick=False):
            assert common.deadline_passed(deadline) is False

        with time_machine.travel(FROZEN + timedelta(seconds=121), tick=False):
            assert common.deadline_passed(deadline) is True




# ===========================================================
# utc_now_naive
# ===========================================================


def test_utc_now_naive_is_the_current_utc_instant_without_a_tzinfo():
    with time_machine.travel(FROZEN, tick=False):
        now = common.utc_now_naive()

    assert now.tzinfo is None
    assert now == FROZEN_NAIVE




# ===========================================================
# parse_source_datetime — the shapes the pages emit
# ===========================================================


@pytest.mark.parametrize("value, expected", [
    # The <time datetime> attribute both faculty templates emit
    ("2026-08-29T10:00:00+03:00", datetime(2026, 8, 29, 10, 0, tzinfo=timezone(timedelta(hours=3)))),
    ("2026-08-29T07:00:00Z", datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)),
    ("2026-08-29T07:00:00+00:00", datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)),
    # The regression: split("+") surgery turned a negative
    # offset into a parse failure
    ("2026-08-29T07:00:00-05:00", datetime(2026, 8, 29, 7, 0, tzinfo=timezone(timedelta(hours=-5)))),
    # article:published_time with fractional seconds
    ("2026-08-29T07:00:00.123456+00:00",
     datetime(2026, 8, 29, 7, 0, 0, 123456, tzinfo=timezone.utc)),
    # Date-only, the listing shape
    ("2026-08-29", datetime(2026, 8, 29, 0, 0)),
    # The older space-form template
    ("2026-08-29 10:00:00", datetime(2026, 8, 29, 10, 0)),
    # Unpadded month/day — fromisoformat refuses these, strptime saves them
    ("2026-8-29", datetime(2026, 8, 29, 0, 0)),
    ("2026-8-29 10:00:00", datetime(2026, 8, 29, 10, 0)),
    # Surrounding whitespace is what a scraped attribute usually carries
    ("  2026-08-29T10:00:00+03:00\n", datetime(2026, 8, 29, 10, 0, tzinfo=timezone(timedelta(hours=3)))),
])
def test_parse_source_datetime_reads_the_format(value, expected):
    assert common.parse_source_datetime(value) == expected


def test_parse_source_datetime_keeps_a_negative_offset_instead_of_failing():
    parsed = common.parse_source_datetime("2026-08-29T07:00:00-05:00")
    assert parsed.utcoffset() == timedelta(hours=-5)


@pytest.mark.parametrize("value", [
    None, "", "   ", "\n\t",
    # What the Lithuanian templates print for humans — not a
    # timestamp, so the caller falls back to now
    "2026 m. rugpjūčio 29 d.",
    "rugpjūčio 29",
    "29/08/2026", "29.08.2026", "August 29, 2026",
    "2026-13-45", "2026-02-30", "vakar", "0",
])
def test_parse_source_datetime_answers_none_for_anything_else(value):
    assert common.parse_source_datetime(value) is None




# ===========================================================
# sanitise_published_at — the offset and the clamp
# ===========================================================


def test_a_missing_timestamp_is_stamped_now():
    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(None) == FROZEN_NAIVE.isoformat()


def test_an_offset_is_applied_and_not_dropped():
    # 10:00 Vilnius is 07:00 UTC; dropping the offset used to
    # put Vilnius news three hours in the future
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(
            datetime(2026, 8, 29, 10, 0, tzinfo=timezone(timedelta(hours=3))))

    assert stamped == "2026-08-29T07:00:00"


def test_a_negative_offset_is_applied_the_same_way():
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(
            datetime(2026, 8, 29, 5, 0, tzinfo=timezone(timedelta(hours=-5))))

    assert stamped == "2026-08-29T10:00:00"


def test_a_naive_timestamp_is_taken_as_utc_already():
    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(datetime(2026, 8, 28, 9, 30)) == "2026-08-28T09:30:00"


def test_the_stored_stamp_carries_no_offset():
    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(
            datetime(2026, 8, 29, 10, 0, tzinfo=timezone(timedelta(hours=3))))

    assert "+" not in stamped and not stamped.endswith("Z")


@pytest.mark.parametrize("ahead", [timedelta(seconds=1), timedelta(hours=3), timedelta(days=400)])
def test_a_future_timestamp_falls_back_to_now(ahead, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        stamped = common.sanitise_published_at(FROZEN_NAIVE + ahead)

    assert stamped == FROZEN_NAIVE.isoformat()
    assert "out of range" in caplog.text


def test_a_timestamp_older_than_five_years_falls_back_to_now(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    ancient = FROZEN_NAIVE - timedelta(days=common.MAX_ARTICLE_AGE_DAYS + 1)

    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(ancient) == FROZEN_NAIVE.isoformat()

    assert "out of range" in caplog.text


def test_a_timestamp_just_inside_the_five_year_window_is_kept():
    inside = FROZEN_NAIVE - timedelta(days=common.MAX_ARTICLE_AGE_DAYS) + timedelta(seconds=1)

    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(inside) == inside.isoformat()


def test_a_timestamp_of_exactly_now_is_kept():
    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(FROZEN_NAIVE) == FROZEN_NAIVE.isoformat()


def test_a_mis_parsed_year_cannot_pin_an_article_to_the_top_of_the_feed():
    # Year 1970 or year 2126 both divide the feed's recency
    # term by something absurd; both land on now instead
    with time_machine.travel(FROZEN, tick=False):
        assert common.sanitise_published_at(datetime(1970, 1, 1)) == FROZEN_NAIVE.isoformat()
        assert common.sanitise_published_at(datetime(2126, 1, 1)) == FROZEN_NAIVE.isoformat()




# ===========================================================
# mark_run_failed — closing a run on a fresh connection
# ===========================================================


def test_mark_run_failed_closes_the_running_row(app, db, seed_run):
    run_id = seed_run("knf", status="running", finished=False)

    common.mark_run_failed(run_id, "HTTP 503 from knf.vu.lt")

    row = db.execute("SELECT status, error_message, finished_at FROM scraper_runs WHERE id = ?",
                     (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "HTTP 503 from knf.vu.lt"
    assert row["finished_at"] is not None


def test_mark_run_failed_works_while_the_callers_connection_is_broken(app, db, seed_run):
    # The whole reason it opens its own connection: the scrapers
    # call it from the except handler where the connection they
    # were using is the thing that died
    run_id = seed_run("vu", status="running", finished=False)
    db.close()

    common.mark_run_failed(run_id, "database is locked")

    check = sqlite3.connect(app.config["DB_PATH"])
    try:
        status = check.execute("SELECT status FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()[0]
    finally:
        check.close()
    assert status == "failed"


def test_mark_run_failed_truncates_a_giant_error_message(app, db, seed_run):
    run_id = seed_run("knf", status="running", finished=False)

    common.mark_run_failed(run_id, "x" * 5000)

    stored = db.execute("SELECT error_message FROM scraper_runs WHERE id = ?",
                        (run_id,)).fetchone()[0]
    assert len(stored) == 1000


def test_mark_run_failed_stringifies_an_exception(app, db, seed_run):
    run_id = seed_run("knf", status="running", finished=False)

    common.mark_run_failed(run_id, requests.exceptions.ReadTimeout("read timed out"))

    stored = db.execute("SELECT error_message FROM scraper_runs WHERE id = ?",
                        (run_id,)).fetchone()[0]
    assert "read timed out" in stored


def test_mark_run_failed_on_an_unknown_run_is_a_no_op(app, db):
    common.mark_run_failed("no-such-run", "boom")

    assert db.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 0


def test_mark_run_failed_swallows_a_database_failure(app, monkeypatch, caplog):
    # It runs in an except handler; raising here would replace
    # the scraper's real error with a database one and escape
    # into the admin route as a 500
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)

    def _broken():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(common, "get_db", _broken)

    common.mark_run_failed("any-run", "boom")

    assert "Failed to close scraper run" in caplog.text




# ===========================================================
# prune_scraper_runs — retention
# ===========================================================


def test_prune_deletes_runs_older_than_the_retention_window(db, seed_run, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    with time_machine.travel(FROZEN, tick=False):
        seed_run("knf", days_ago=0)                                       # kept: newest
        old = seed_run("knf", days_ago=common.RUN_RETENTION_DAYS + 1)     # pruned
        older = seed_run("knf", days_ago=365)                             # pruned

        common.prune_scraper_runs(db)

    remaining = {r[0] for r in db.execute("SELECT id FROM scraper_runs")}
    assert old not in remaining and older not in remaining
    assert len(remaining) == 1
    assert "Pruned 2 scraper_runs row(s)" in caplog.text


def test_prune_keeps_a_run_inside_the_window(db, seed_run):
    with time_machine.travel(FROZEN, tick=False):
        recent = seed_run("knf", days_ago=common.RUN_RETENTION_DAYS - 1)
        seed_run("knf", days_ago=0)

        common.prune_scraper_runs(db)

    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE id = ?", (recent,)).fetchone()[0] == 1


def test_prune_keeps_the_newest_run_of_every_source_whatever_its_age(db, seed_run):
    # /api/scraper/status has to keep showing the last run of a
    # source that stopped succeeding months ago
    with time_machine.travel(FROZEN, tick=False):
        stale_knf = seed_run("knf", days_ago=200)
        stale_vu = seed_run("vu", days_ago=400, status="failed")
        older_vu = seed_run("vu", days_ago=500, status="failed")

        common.prune_scraper_runs(db)

    remaining = {r[0] for r in db.execute("SELECT id FROM scraper_runs")}
    assert remaining == {stale_knf, stale_vu}
    assert older_vu not in remaining


def test_prune_commits_so_another_connection_sees_it(app, db, seed_run):
    with time_machine.travel(FROZEN, tick=False):
        seed_run("knf", days_ago=0)
        seed_run("knf", days_ago=90)

        common.prune_scraper_runs(db)

    check = sqlite3.connect(app.config["DB_PATH"])
    try:
        assert check.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 1
    finally:
        check.close()


def test_prune_on_an_empty_table_says_nothing(db, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    common.prune_scraper_runs(db)

    assert "Pruned" not in caplog.text


def test_prune_never_turns_a_completed_run_into_a_failed_one(app, db, caplog):
    # Retention is housekeeping: a broken connection is a
    # warning, never an exception the scraper has to catch
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    db.close()

    common.prune_scraper_runs(db)

    assert "Could not prune scraper_runs" in caplog.text




# ===========================================================
# load_deleted_urls — the tombstones
# ===========================================================


def _tombstone(db, url):
    db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)", (url,))
    db.commit()


def test_no_tombstones_is_an_empty_set(db):
    assert common.load_deleted_urls(db) == set()


def test_tombstones_come_back_in_normalised_shape(db):
    _tombstone(db, "http://www.knf.vu.lt/naujienos/x/?utm_source=fb#top")

    assert common.load_deleted_urls(db) == {"https://knf.vu.lt/naujienos/x"}


def test_a_deleted_article_stays_deleted_however_the_next_run_writes_its_link(db):
    # The tombstone and the freshly scraped link only match
    # because BOTH go through normalise_url
    _tombstone(db, "https://knf.vu.lt/naujienos/studiju-pradzia")
    deleted = common.load_deleted_urls(db)

    scraped = "https://www.knf.vu.lt/naujienos/studiju-pradzia/?utm_campaign=rugsejis"
    assert common.normalise_url(scraped) in deleted


def test_a_different_article_is_not_matched_by_a_tombstone(db):
    _tombstone(db, "https://knf.vu.lt/naujienos/x")

    assert common.normalise_url("https://knf.vu.lt/naujienos/y") not in common.load_deleted_urls(db)


def test_an_empty_tombstone_row_is_ignored(db):
    _tombstone(db, "")
    _tombstone(db, "https://knf.vu.lt/naujienos/x")

    assert common.load_deleted_urls(db) == {"https://knf.vu.lt/naujienos/x"}


def test_a_missing_tombstone_table_is_an_empty_set_not_a_failure(db, caplog):
    # The table is owned by the news package; a scraper running
    # before that migration must behave exactly as before
    caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)
    db.execute("DROP TABLE deleted_source_urls")
    db.commit()

    assert common.load_deleted_urls(db) == set()
    assert "No deleted_source_urls table yet" in caplog.text




# ===========================================================
# validate_image_url — what a news row may store
# ===========================================================


@pytest.mark.parametrize("src", ["", None])
def test_an_absent_image_src_is_none(src):
    assert common.validate_image_url(ARTICLE_URL, src) is None


def test_a_whitespace_only_image_src_is_none():
    # A blank src must not resolve to the ARTICLE PAGE — an
    # HTML document stored as image_url reaches every guest's
    # <Image> and also blocks the caller's <img> fallback
    assert common.validate_image_url(ARTICLE_URL, "   \n") is None


def test_a_relative_src_is_resolved_against_the_article_page():
    assert common.validate_image_url("https://knf.vu.lt/naujienos/x", "images/foto.jpg") == \
        "https://knf.vu.lt/naujienos/images/foto.jpg"


def test_a_root_relative_src_is_resolved_against_the_host():
    assert common.validate_image_url(ARTICLE_URL, "/sites/default/files/foto.jpg") == \
        "https://knf.vu.lt/sites/default/files/foto.jpg"


def test_a_protocol_relative_src_keeps_the_page_scheme():
    # Glued onto BASE_URL this used to become a broken URL
    assert common.validate_image_url(ARTICLE_URL, "//newshub.vu.lt/foto.jpg") == \
        "https://newshub.vu.lt/foto.jpg"


@pytest.mark.parametrize("src", [
    "https://newshub.vu.lt/foto.jpg",
    "https://www.newshub.vu.lt/foto.jpg",
    "https://vu.lt/foto.jpg",
    "https://www.knf.vu.lt/foto.jpg",
    "http://knf.vu.lt/foto.jpg",
])
def test_an_absolute_src_on_the_image_allowlist_survives(src):
    assert common.validate_image_url(ARTICLE_URL, src) == src


def test_a_surrounding_whitespace_src_is_trimmed():
    assert common.validate_image_url(ARTICLE_URL, "  https://newshub.vu.lt/foto.jpg  ") == \
        "https://newshub.vu.lt/foto.jpg"


@pytest.mark.parametrize("src", [
    "data:image/png;base64,iVBORw0KGgo=",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "https://evil.example.org/beacon.gif",
    "https://cdn.example.com/foto.jpg",
    "https://knf.vu.lt.evil.example.org/foto.jpg",
    "https://tvarkarasciai.vu.lt/foto.jpg",
])
def test_an_image_src_off_the_allowlist_is_dropped(src, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert common.validate_image_url(ARTICLE_URL, src) is None
    assert "image" in caplog.text.lower()


def test_an_image_url_over_the_length_cap_is_dropped(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    src = "https://newshub.vu.lt/" + "a" * (common.MAX_IMAGE_URL_LENGTH - 25) + ".jpg"
    assert len(src) == common.MAX_IMAGE_URL_LENGTH + 1

    assert common.validate_image_url(ARTICLE_URL, src) is None
    assert "Image URL over" in caplog.text


def test_an_image_url_exactly_on_the_cap_is_kept():
    src = "https://newshub.vu.lt/" + "a" * (common.MAX_IMAGE_URL_LENGTH - 26) + ".jpg"
    assert len(src) == common.MAX_IMAGE_URL_LENGTH

    assert common.validate_image_url(ARTICLE_URL, src) == src


def test_an_unparsable_page_url_drops_the_image(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    assert common.validate_image_url("http://[", "foto.jpg") is None
    assert "Unparsable image src" in caplog.text




# ===========================================================
# push_allowed — the run-shape guard on notifications
# ===========================================================


def test_the_first_completed_run_of_a_source_never_pushes(db, caplog):
    # 247 new articles on a first boot is history, not news
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    assert common.push_allowed(db, "knf", 247, "run-1") is False
    assert "first completed run" in caplog.text


def test_the_run_being_closed_does_not_count_as_its_own_predecessor(db, seed_run):
    run_id = seed_run("knf", status="completed")

    assert common.push_allowed(db, "knf", 3, run_id) is False


def test_an_earlier_failed_run_is_not_a_predecessor(db, seed_run):
    seed_run("knf", status="failed")
    seed_run("knf", status="running", finished=False)

    assert common.push_allowed(db, "knf", 3, "run-now") is False


def test_an_earlier_run_of_another_source_is_not_a_predecessor(db, seed_run):
    seed_run("vu", status="completed")

    assert common.push_allowed(db, "knf", 3, "run-now") is False


def test_a_normal_run_after_an_earlier_completed_one_pushes(db, seed_run):
    seed_run("knf", status="completed")

    assert common.push_allowed(db, "knf", 3, "run-now") is True


def test_a_burst_over_the_threshold_is_suppressed(db, seed_run, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    seed_run("knf", status="completed")

    assert common.push_allowed(db, "knf", common.PUSH_BURST_THRESHOLD + 1, "run-now") is False
    assert "burst threshold" in caplog.text


def test_a_run_exactly_on_the_burst_threshold_still_pushes(db, seed_run):
    seed_run("knf", status="completed")

    assert common.push_allowed(db, "knf", common.PUSH_BURST_THRESHOLD, "run-now") is True


def test_a_suppressed_burst_does_not_spend_the_hourly_slot(db, seed_run):
    seed_run("knf", status="completed")

    assert common.push_allowed(db, "knf", 100, "run-1") is False
    assert common.push_allowed(db, "knf", 2, "run-2") is True


def test_a_second_push_inside_the_hour_is_suppressed(db, seed_run, caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    seed_run("knf", status="completed")

    assert common.push_allowed(db, "knf", 1, "run-1") is True
    assert common.push_allowed(db, "knf", 1, "run-2") is False
    assert "was sent" in caplog.text


def test_each_source_keeps_its_own_hourly_budget(db, seed_run):
    seed_run("knf", status="completed")
    seed_run("vu", status="completed")

    assert common.push_allowed(db, "knf", 1, "run-1") is True
    assert common.push_allowed(db, "vu", 1, "run-2") is True


def test_a_push_is_allowed_again_once_the_hour_is_over(db, seed_run):
    seed_run("knf", status="completed")

    with time_machine.travel(FROZEN, tick=False):
        assert common.push_allowed(db, "knf", 1, "run-1") is True

        with time_machine.travel(FROZEN + timedelta(seconds=common.PUSH_MIN_INTERVAL_SECONDS - 1),
                                 tick=False):
            assert common.push_allowed(db, "knf", 1, "run-2") is False

        with time_machine.travel(FROZEN + timedelta(seconds=common.PUSH_MIN_INTERVAL_SECONDS + 1),
                                 tick=False):
            assert common.push_allowed(db, "knf", 1, "run-3") is True


def test_a_database_error_fails_open_rather_than_silencing_the_push(app, db, caplog):
    # A broken count must not silence the feature that works
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    db.close()

    assert common.push_allowed(db, "knf", 3, "run-now") is True
    assert "Could not count earlier knf runs" in caplog.text




# ===========================================================
# check_yield_drop — the selector-rot alarm
# ===========================================================


def test_a_collapsed_yield_logs_one_error(db, seed_run, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    seed_run("knf", status="completed", found=40)

    common.check_yield_drop(db, "knf", 3, "run-now")

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "yield collapsed" in errors[0].getMessage()
    assert "3 item(s) this run against 40 last run" in errors[0].getMessage()


def test_a_healthy_run_says_nothing(db, seed_run, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    seed_run("knf", status="completed", found=40)

    common.check_yield_drop(db, "knf", 30, "run-now")

    assert caplog.records == []


def test_a_yield_exactly_a_tenth_of_the_last_run_is_not_a_collapse(db, seed_run, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    seed_run("knf", status="completed", found=40)

    common.check_yield_drop(db, "knf", 4, "run-now")

    assert caplog.records == []


def test_a_source_that_always_yields_a_handful_is_never_collapsing(db, seed_run, caplog):
    # Under ten there is no order of magnitude to lose
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    seed_run("knf", status="completed", found=9)

    common.check_yield_drop(db, "knf", 0, "run-now")

    assert caplog.records == []


def test_ten_down_to_zero_is_a_collapse(db, seed_run, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    seed_run("knf", status="completed", found=10)

    common.check_yield_drop(db, "knf", 0, "run-now")

    assert "yield collapsed" in caplog.text


def test_the_first_run_of_a_source_is_never_a_collapse(db, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)

    common.check_yield_drop(db, "knf", 0, "run-now")

    assert caplog.records == []


def test_only_the_latest_completed_run_of_the_same_source_is_the_baseline(db, seed_run, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    seed_run("knf", status="completed", found=500, days_ago=10)   # older, ignored
    seed_run("knf", status="failed", found=400, days_ago=1)       # not completed, ignored
    seed_run("vu", status="completed", found=300, days_ago=0)     # other source, ignored
    seed_run("knf", status="completed", found=20, days_ago=2)     # the baseline

    common.check_yield_drop(db, "knf", 5, "run-now")

    assert caplog.records == []


def test_the_run_being_closed_is_not_its_own_baseline(db, seed_run, caplog):
    caplog.set_level(logging.ERROR, logger=LOGGER_NAME)
    run_id = seed_run("knf", status="completed", found=40)

    common.check_yield_drop(db, "knf", 1, run_id)

    assert caplog.records == []


def test_a_database_error_is_only_a_warning(app, db, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    db.close()

    common.check_yield_drop(db, "knf", 1, "run-now")

    assert "Could not read the previous knf run" in caplog.text
