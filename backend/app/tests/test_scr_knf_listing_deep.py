# -----------------------------------------------------------
#  [*] Tests — knf_scraper, the LISTING half, branch by branch
#
#  One slice of app/scraper/knf_scraper.py: _listing_links and
#  everything scrape_knf_news does BEFORE an article page is
#  parsed — link extraction, filtering and the paging walk.
#  The article parse (_fetch_article) and the write
#  transaction are other files' business; every article page
#  served here is the plainest one that stores.
#
#  What this module proves:
#
#    - _listing_links as a unit: the three template
#      generations, which one wins and when the next is even
#      consulted, the one-anchor-per-heading rule of the
#      current template against the every-anchor rule of the
#      last resort, an anchor carrying the class itself,
#      nested matching ancestors, and the "/aktualijos/"
#      substring filter with its case sensitivity and its
#      missing trailing slash
#    - the allowlist: a third-party host, a javascript: href,
#      an IP literal, a protocol-relative link and — the one
#      that looks safest — "https://knf.vu.lt@evil.example.
#      com/…", whose HOSTNAME is the attacker's. None of them
#      may produce a single request, and site.hits records
#      EVERY request the run makes, not only the ones to the
#      faculty
#    - canonicalisation before the dedupe key: userinfo, port,
#      scheme, host case, "www.", the fragment and every
#      tracking parameter collapse; a meaningful query
#      parameter and a path's letter case do not
#    - the found counter: distinct links with non-empty text,
#      tombstoned and already-stored ones included, an empty,
#      whitespace-only, image-only or comment-only link text
#      excluded
#    - paging: the ?start= offsets, `pages` as a MINIMUM (0,
#      negative, fractional, past the cap, and the wrong type
#      that must fail the run instead of escaping it), what
#      counts as "unseen" on a page, the article and listing
#      caps at their exact boundaries, and a listing page that
#      fails to download, answers 404/500/503, arrives as JSON
#      or arrives with no Content-Type at all
#    - the wall-clock budget at all three points it is tested:
#      before the first fetch, between the links of a page and
#      at the top of the loop after a page that failed to
#      download (the only path that reaches it)
#    - the template-change verdict: pages_fetched against
#      articles_found, run-wide rather than per page
#
#  Every request goes through `responses`; the container runs
#  --network none, so a test that reached knf.vu.lt would fail
#  by construction. Clock-dependent behaviour is driven by
#  time_machine — including shifting the clock INSIDE a
#  response callback, which is how a budget is spent mid-run
#  without a single sleep.
# -----------------------------------------------------------


import logging
import re
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest
import requests
import responses
import time_machine
from bs4 import BeautifulSoup

from app.scraper import common, knf_scraper
from app.scraper.knf_scraper import _listing_links, scrape_knf_news


BASE = "https://knf.vu.lt"
NEWS = f"{BASE}/aktualijos"

# The fake site answers EVERYTHING, not just knf.vu.lt: a link
# the scraper must refuse would otherwise leave no trace to
# assert on
ANY_URL = re.compile(r".*")

HTML_TYPE = "text/html; charset=utf-8"

# A plain wall clock for the budget tests; nothing here reads
# a stored timestamp back, so the instant only has to be fixed
FROZEN = "2026-06-15 12:00:00 +0000"




# -----------------------------------------------------------
# _listing_url / _article_url
# -----------------------------------------------------------
#
# The exact URLs the walk builds — Joomla pages five at a
# time through ?start=<offset>, and the canonical article URL
# normalise_url hands to fetch(). The fixture map is keyed by
# these strings, so a typo shows up as "the page is missing".
# -----------------------------------------------------------

def _listing_url(offset=0):
    return NEWS if offset == 0 else f"{NEWS}?start={offset}"


def _article_url(slug):
    return f"{BASE}/aktualijos/{slug}"




# -----------------------------------------------------------
# _page / _listing / _article
# -----------------------------------------------------------
#
#   _listing([("/aktualijos/a", "A")])            — current
#   _listing([...], template="h4")                — older
#   _listing([...], template="class")             — last resort
#
# A listing page carrying (href, link text) pairs — the text
# is inserted raw, so a test can put markup, an <img> or a
# comment where the title should be. _article is the plainest
# page that stores: an og:title and a body.
# -----------------------------------------------------------

def _page(body, head=""):
    return ("<!doctype html><html lang=\"lt\"><head><meta charset=\"utf-8\">"
            f"<title>VU Kauno fakultetas</title>{head}</head>"
            f"<body>{body}</body></html>")


def _listing(items, template="h2"):
    blocks = []
    for href, text in items:
        if template == "h2":
            blocks.append(f"<h2 class=\"article-title\"><a href=\"{href}\">{text}</a></h2>")
        elif template == "h4":
            blocks.append(f"<h4><a href=\"{href}\">{text}</a></h4>")
        else:
            blocks.append(f"<div class=\"blog-article-title\"><a href=\"{href}\">{text}</a></div>")

    return _page(f"<div class=\"blog\">{''.join(blocks)}</div>")


def _article(title="Naujiena", published=None):
    head = f"<meta property=\"og:title\" content=\"{title}\">"
    if published:
        head += f"<meta property=\"article:published_time\" content=\"{published}\">"

    return _page("<div class=\"item-page\"><div class=\"article-content\">"
                 "<p>Fakultetas kviecia studentus i rugsejo pirmosios svente.</p>"
                 "</div></div>",
                 head=head)


# A listing page that downloads perfectly and holds no article
# link at all — the end of the paginated list
EMPTY_LISTING = _page("<div class=\"blog\"><p>Daugiau naujienu nera.</p></div>")




# -----------------------------------------------------------
# _links
# -----------------------------------------------------------
#
# _listing_links driven straight off a markup fragment, so the
# generation rules can be pinned without a run around them.
# -----------------------------------------------------------

def _links(markup):
    return [a.get("href") for a in _listing_links(BeautifulSoup(_page(markup), "lxml"))]




# -----------------------------------------------------------
# _many
# -----------------------------------------------------------
#
#   items, pages = _many(21)
#
# N listing items plus the article page each one needs, for
# the two caps that only show at their exact boundary.
# -----------------------------------------------------------

def _many(count, prefix="a"):
    # The titles carry the prefix too: a second page repeating a
    # title of the first would be dropped as the same story
    # republished, which is the write half's business, not this
    # file's
    items = [(f"/aktualijos/{prefix}{i}", f"Straipsnis {prefix}{i}") for i in range(count)]
    pages = {_article_url(f"{prefix}{i}"): _article(f"Straipsnis {prefix}{i}") for i in range(count)}
    return items, pages




# -----------------------------------------------------------
# _Served
# -----------------------------------------------------------
#
# One response with a status and a Content-Type of its own —
# what a plain string page cannot express: a 500, a listing
# served as JSON, a listing served with no type at all.
# -----------------------------------------------------------

class _Served:

    def __init__(self, body, status=200, content_type=HTML_TYPE):
        self.body = body
        self.status = status
        self.content_type = content_type


def _as_response(page):
    if isinstance(page, _Served):
        return page
    if isinstance(page, tuple):
        return _Served(page[1], status=page[0])
    return _Served(page)




# -----------------------------------------------------------
# _Site
# -----------------------------------------------------------
#
# The fake internet. `serve` adds pages; the callback answers
# them and records EVERY request, whatever the host, so a test
# can prove the scraper never asked for something. A URL that
# was never served raises the ConnectionError requests would
# raise for a host that is not answering. A callable page is
# served for its side effect as well as its body — that is how
# the clock is moved in the middle of a download.
# -----------------------------------------------------------

class _Site:

    def __init__(self):
        self.pages = {}
        self.hits = []

    def serve(self, pages):
        self.pages.update(pages)
        return self

    def answer(self, request):
        self.hits.append(request.url)
        page = self.pages.get(request.url)
        if page is None:
            return requests.exceptions.ConnectionError(f"unreachable: {request.url}")
        if callable(page):
            page = page(request)
        served = _as_response(page)
        return served.status, {"Content-Type": served.content_type}, served.body.encode("utf-8")

    def hits_on(self, url):
        return self.hits.count(url)

    def listing_hits(self):
        return [h for h in self.hits if h == NEWS or h.startswith(f"{NEWS}?start=")]

    def article_hits(self):
        return [h for h in self.hits if h not in self.listing_hits()]




# -----------------------------------------------------------
# site
# -----------------------------------------------------------
#
# Used by:
#   - every run-level test below; without it a fetch would
#     leave the container, which --network none forbids
# -----------------------------------------------------------

@pytest.fixture
def site():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        fake = _Site()
        mock.add_callback(responses.GET, ANY_URL, callback=fake.answer, content_type=HTML_TYPE)
        yield fake




# -----------------------------------------------------------
# forget_push_history
# -----------------------------------------------------------
#
# common._LAST_PUSH is a PROCESS-global map, so one module's
# run could silence another's. Cleared on both sides.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def forget_push_history():
    common._LAST_PUSH.clear()
    yield
    common._LAST_PUSH.clear()




# -----------------------------------------------------------
# stored / runs / seed_article / tombstone
# -----------------------------------------------------------
#
#   stored()               — every scraped row
#   runs(run_id)           — one scraper_runs row
#   seed_article(url)      — what an earlier run left behind
#   tombstone(url)         — what an admin deleted
# -----------------------------------------------------------

@pytest.fixture
def stored(db):

    def _stored():
        return db.execute("SELECT * FROM news_posts ORDER BY title").fetchall()

    return _stored


@pytest.fixture
def runs(db):

    def _runs(run_id):
        return db.execute("SELECT * FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()

    return _runs


@pytest.fixture
def seed_article(app):

    def _seed(source_url, title=None):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO news_posts
                   (id, title, content, summary, source, source_url, post_type, published_at)
                   VALUES (?, ?, 'Turinys', 'Santrauka', 'knf.vu.lt', ?, 'article', ?)""",
                (str(uuid.uuid4()), title or f"Sena {uuid.uuid4().hex[:6]}", source_url,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    return _seed


@pytest.fixture
def tombstone(app):

    def _tombstone(source_url):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)", (source_url,))
            conn.commit()
        finally:
            conn.close()

    return _tombstone




# -----------------------------------------------------------
# _listing_links — the current template (h2.article-title)
# -----------------------------------------------------------


def test_an_empty_document_yields_no_links():
    assert _links("") == []


def test_a_page_of_ordinary_anchors_yields_no_links():
    assert _links("<p><a href=\"/aktualijos/a\">Ne antrasteje</a></p>") == []


def test_the_current_template_yields_its_anchors_in_document_order():
    markup = ("<h2 class=\"article-title\"><a href=\"/aktualijos/pirma\">Pirma</a></h2>"
              "<h2 class=\"article-title\"><a href=\"/aktualijos/antra\">Antra</a></h2>"
              "<h2 class=\"article-title\"><a href=\"/aktualijos/trecia\">Trecia</a></h2>")

    assert _links(markup) == ["/aktualijos/pirma", "/aktualijos/antra", "/aktualijos/trecia"]


def test_a_heading_class_list_that_merely_contains_the_class_still_matches():
    assert _links("<h2 class=\"featured article-title big\">"
                  "<a href=\"/aktualijos/x\">X</a></h2>") == ["/aktualijos/x"]


def test_only_the_first_anchor_of_a_current_template_heading_is_taken():
    # find() stops at the first anchor — the "share" link a
    # heading sometimes carries beside the title is never a
    # second article
    markup = ("<h2 class=\"article-title\"><a href=\"/aktualijos/a\">A</a>"
              "<a href=\"/aktualijos/b\">B</a></h2>")

    assert _links(markup) == ["/aktualijos/a"]


def test_an_anchor_without_an_href_does_not_hide_the_one_behind_it():
    # href=True is part of the search, so the anchor with no
    # href is stepped over rather than ending the heading
    markup = "<h2 class=\"article-title\"><a>Vardas</a><a href=\"/aktualijos/b\">B</a></h2>"

    assert _links(markup) == ["/aktualijos/b"]


def test_a_deeply_nested_anchor_inside_the_heading_is_still_found():
    markup = ("<h2 class=\"article-title\"><span><em>"
              "<a href=\"/aktualijos/gilus\">Gilus</a></em></span></h2>")

    assert _links(markup) == ["/aktualijos/gilus"]


def test_an_uppercased_heading_and_attribute_still_match():
    # lxml lowercases tag and attribute names; a template that
    # shouts must parse the same way
    assert _links("<H2 CLASS=\"article-title\"><A HREF=\"/aktualijos/didz\">D</A></H2>") \
        == ["/aktualijos/didz"]


def test_a_heading_without_any_anchor_contributes_nothing():
    markup = ("<h2 class=\"article-title\">Be nuorodos</h2>"
              "<h2 class=\"article-title\"><a href=\"/aktualijos/su\">Su</a></h2>")

    assert _links(markup) == ["/aktualijos/su"]


def test_duplicate_anchors_are_all_returned_for_the_caller_to_dedupe():
    markup = ("<h2 class=\"article-title\"><a href=\"/aktualijos/ta-pati\">Ta pati</a></h2>"
              "<h2 class=\"article-title\"><a href=\"/aktualijos/ta-pati\">Ta pati</a></h2>")

    assert _links(markup) == ["/aktualijos/ta-pati", "/aktualijos/ta-pati"]




# -----------------------------------------------------------
# _listing_links — the older template (h4) and the last resort
# -----------------------------------------------------------


def test_the_older_template_is_read_only_when_the_current_one_matched_nothing():
    markup = ("<h2 class=\"article-title\"><a href=\"/aktualijos/naujas\">Naujas</a></h2>"
              "<h4><a href=\"/aktualijos/senas\">Senas</a></h4>")

    # The current generation matched, so the h4 is never looked at
    assert _links(markup) == ["/aktualijos/naujas"]


def test_the_older_template_takes_over_when_the_current_one_matched_nothing_useful():
    # The h2 headings are there but point at the menu, so the
    # generation yields nothing and the next one is consulted
    markup = ("<h2 class=\"article-title\"><a href=\"/studijos\">Studijos</a></h2>"
              "<h4><a href=\"/aktualijos/senas\">Senas</a></h4>")

    assert _links(markup) == ["/aktualijos/senas"]


def test_the_older_template_takes_headings_anywhere_on_the_page():
    # find_all("h4") does not care where the heading sits — a
    # sidebar teaser is an article link too
    markup = "<aside><h4><a href=\"/aktualijos/salia\">Salia</a></h4></aside>"

    assert _links(markup) == ["/aktualijos/salia"]


def test_an_h4_without_an_anchor_does_not_end_the_older_generation():
    markup = ("<h4>Skyrius</h4><h4><a href=\"/aktualijos/senas\">Senas</a></h4>")

    assert _links(markup) == ["/aktualijos/senas"]


def test_the_last_resort_generation_is_read_only_when_the_older_one_matched_nothing():
    markup = ("<h4><a href=\"/aktualijos/senas\">Senas</a></h4>"
              "<div class=\"blog-article-title\"><a href=\"/aktualijos/kitas\">Kitas</a></div>")

    assert _links(markup) == ["/aktualijos/senas"]


def test_the_last_resort_generation_matches_any_heading_level():
    assert _links("<h3 class=\"article-title\"><a href=\"/aktualijos/h3\">H3</a></h3>") \
        == ["/aktualijos/h3"]


def test_the_last_resort_generation_matches_a_class_that_only_contains_the_word():
    # [class*='article-title'] is a substring match, so the
    # plural — which h2.article-title does NOT match — is caught
    assert _links("<h2 class=\"article-titles\"><a href=\"/aktualijos/x\">X</a></h2>") \
        == ["/aktualijos/x"]


def test_the_last_resort_generation_takes_every_anchor_under_the_heading():
    # Unlike the current generation's one-per-heading rule
    markup = ("<div class=\"blog-article-title\"><a href=\"/aktualijos/a\">A</a>"
              "<a href=\"/aktualijos/b\">B</a></div>")

    assert _links(markup) == ["/aktualijos/a", "/aktualijos/b"]


def test_an_anchor_carrying_the_class_itself_is_not_a_descendant_of_it():
    # The selector is "[class*='article-title'] a" — an element
    # is not its own descendant, so this anchor is invisible to
    # all three generations
    assert _links("<a class=\"article-title\" href=\"/aktualijos/x\">X</a>") == []


def test_nested_matching_ancestors_yield_the_anchor_once():
    markup = ("<div class=\"article-title-wrap\"><span class=\"article-title\">"
              "<a href=\"/aktualijos/x\">X</a></span></div>")

    assert _links(markup) == ["/aktualijos/x"]


def test_the_last_resort_generation_ignores_an_anchor_without_an_href():
    markup = ("<div class=\"blog-article-title\"><a>Be href</a></div>"
              "<div class=\"blog-article-title\"><a href=\"/aktualijos/gera\">Gera</a></div>")

    assert _links(markup) == ["/aktualijos/gera"]


def test_a_current_heading_whose_first_anchor_is_not_an_article_falls_to_the_last_resort():
    # The current generation looks at the FIRST anchor only and
    # so yields nothing; the last resort looks at every anchor
    # under the heading and rescues the article link
    markup = ("<h2 class=\"article-title\"><a href=\"/kontaktai\">Kontaktai</a>"
              "<a href=\"/aktualijos/b\">B</a></h2>")

    assert _links(markup) == ["/aktualijos/b"]


def test_the_three_generations_never_mix():
    markup = ("<h2 class=\"article-title\"><a href=\"/aktualijos/dabartinis\">Dabartinis</a></h2>"
              "<h4><a href=\"/aktualijos/senas\">Senas</a></h4>"
              "<div class=\"blog-article-title\"><a href=\"/aktualijos/kitas\">Kitas</a></div>")

    assert _links(markup) == ["/aktualijos/dabartinis"]




# -----------------------------------------------------------
# _listing_links — the "/aktualijos/" filter
# -----------------------------------------------------------


@pytest.mark.parametrize("href", [
    "/kontaktai",
    "/studijos/bakalauras",
    # The section index without its trailing slash — the "back
    # to Aktualijos" link every article page carries
    "/aktualijos",
    # The check is case-sensitive
    "/AKTUALIJOS/x",
    "/Aktualijos/x",
    # No leading slash, so the segment never appears whole
    "aktualijos/x",
    "#",
    "",
    "mailto:info@knf.vu.lt",
])
def test_a_link_without_the_aktualijos_segment_is_left_behind(href):
    assert _links(f"<h2 class=\"article-title\"><a href=\"{href}\">Nuoroda</a></h2>") == []


@pytest.mark.parametrize("href", [
    "/aktualijos/straipsnis",
    "/en/aktualijos/article",
    "https://knf.vu.lt/aktualijos/straipsnis",
    "https://www.knf.vu.lt/aktualijos/straipsnis",
    "../aktualijos/straipsnis",
    "/aktualijos/straipsnis?id=7",
    "/aktualijos/straipsnis#turinys",
    # The segment is looked for anywhere in the href, not only
    # in the path — a search link carrying it in its query
    # counts too
    "/paieska?q=/aktualijos/",
])
def test_a_link_carrying_the_segment_anywhere_is_taken(href):
    assert _links(f"<h2 class=\"article-title\"><a href=\"{href}\">Nuoroda</a></h2>") == [href]


def test_the_href_is_handed_over_with_its_whitespace_intact():
    # Trimming is normalise_url's job, not the extractor's
    assert _links("<h2 class=\"article-title\">"
                  "<a href=\"  /aktualijos/tarpai  \">T</a></h2>") == ["  /aktualijos/tarpai  "]




# -----------------------------------------------------------
# The walk — the host allowlist
# -----------------------------------------------------------


def test_a_link_to_a_third_party_host_is_neither_counted_nor_fetched(app, site, stored, caplog):
    site.serve({
        _listing_url(): _listing([("https://evil.example.com/aktualijos/ssrf", "Piktybine"),
                                  ("/aktualijos/gera", "Gera")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article("Gera"),
    })

    with caplog.at_level(logging.WARNING, logger="app.scraper.knf_scraper"):
        result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)
    assert [row["source_url"] for row in stored()] == [_article_url("gera")]
    assert not any("evil.example.com" in hit for hit in site.hits)
    assert "off-allowlist article link" in caplog.text


def test_a_listing_of_only_third_party_links_fails_the_run(app, site, runs, stored):
    site.serve({_listing_url(): _listing([("https://evil.example.com/aktualijos/a", "A"),
                                          ("https://knf.vu.lt.evil.example.com/aktualijos/b", "B")])})

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (0, 0)
    assert "template" in result["error"]
    assert runs(result["runId"])["status"] == "failed"
    assert stored() == []


def test_a_javascript_href_never_becomes_a_request(app, site):
    site.serve({
        _listing_url(): _listing([("javascript:/aktualijos/x", "Kenkejiska"),
                                  ("/aktualijos/gera", "Gera")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article("Gera"),
    })

    result = scrape_knf_news(pages=1)

    assert result["found"] == 1
    assert not any(hit.startswith("javascript") for hit in site.hits)


@pytest.mark.parametrize("href, marker", [
    ("http://127.0.0.1/aktualijos/x", "127.0.0.1"),
    ("http://169.254.169.254/aktualijos/metadata", "169.254"),
    ("http://[::1]/aktualijos/x", "::1"),
    ("http://localhost/aktualijos/x", "localhost"),
])
def test_a_link_to_an_internal_address_is_refused(app, site, href, marker):
    site.serve({
        _listing_url(): _listing([(href, "Vidine"), ("/aktualijos/gera", "Gera")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article("Gera"),
    })

    result = scrape_knf_news(pages=1)

    assert result["found"] == 1
    assert not any(marker in hit for hit in site.hits)


def test_a_host_that_only_looks_like_the_faculty_in_its_userinfo_is_refused(app, site):
    # "https://knf.vu.lt@evil.example.com/…" — the hostname is
    # the attacker's; everything before the @ is a username
    site.serve({
        _listing_url(): _listing([("https://knf.vu.lt@evil.example.com/aktualijos/x", "Apgaule"),
                                  ("/aktualijos/gera", "Gera")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article("Gera"),
    })

    result = scrape_knf_news(pages=1)

    assert result["found"] == 1
    assert not any("evil.example.com" in hit for hit in site.hits)


def test_credentials_on_the_faculty_host_are_dropped_from_the_fetched_url(app, site, stored):
    site.serve({
        _listing_url(): _listing([("https://redaktorius@knf.vu.lt/aktualijos/x", "Su vardu")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("x"): _article("Su vardu"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)
    assert site.hits_on(_article_url("x")) == 1
    assert [row["source_url"] for row in stored()] == [_article_url("x")]


def test_a_link_on_an_unusual_port_is_fetched_on_the_default_one(app, site):
    site.serve({
        _listing_url(): _listing([("https://knf.vu.lt:8443/aktualijos/portas", "Portas")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("portas"): _article("Portas"),
    })

    assert scrape_knf_news(pages=1)["new"] == 1
    assert site.hits_on(_article_url("portas")) == 1
    assert not any(":8443" in hit for hit in site.hits)


def test_a_protocol_relative_link_to_the_faculty_is_fetched(app, site):
    site.serve({
        _listing_url(): _listing([("//knf.vu.lt/aktualijos/be-schemos", "Be schemos")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("be-schemos"): _article("Be schemos"),
    })

    assert scrape_knf_news(pages=1)["new"] == 1
    assert site.hits_on(_article_url("be-schemos")) == 1


def test_a_protocol_relative_link_to_another_host_is_refused(app, site, runs):
    site.serve({_listing_url(): _listing([("//evil.example.com/aktualijos/x", "Piktybine")])})

    result = scrape_knf_news(pages=1)

    # Nothing recognisable was left after the filter, so this is
    # the template verdict — and no request went out
    assert runs(result["runId"])["status"] == "failed"
    assert not any("evil.example.com" in hit for hit in site.hits)


def test_an_http_link_is_canonicalised_to_https(app, site, stored):
    site.serve({
        _listing_url(): _listing([("http://knf.vu.lt/aktualijos/http", "Http")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("http"): _article("Http"),
    })

    assert scrape_knf_news(pages=1)["new"] == 1
    assert not any(hit.startswith("http://") for hit in site.hits)
    assert [row["source_url"] for row in stored()] == [_article_url("http")]


def test_an_uppercase_scheme_and_host_still_resolve_to_the_faculty(app, site):
    site.serve({
        _listing_url(): _listing([("HTTPS://KNF.VU.LT/aktualijos/didz", "Didziosiomis")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("didz"): _article("Didziosiomis"),
    })

    assert scrape_knf_news(pages=1)["new"] == 1
    assert site.hits_on(_article_url("didz")) == 1


def test_a_relative_link_is_resolved_against_the_site_root(app, site):
    site.serve({
        _listing_url(): _listing([("../aktualijos/santykine", "Santykine")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("santykine"): _article("Santykine"),
    })

    assert scrape_knf_news(pages=1)["new"] == 1
    assert site.hits_on(_article_url("santykine")) == 1


def test_a_lithuanian_slug_is_fetched_percent_encoded(app, site, stored):
    slug = "rugsejo-1-osios-šventė"
    site.serve({
        _listing_url(): _listing([(f"/aktualijos/{slug}", "Šventė")]),
        _listing_url(5): EMPTY_LISTING,
        requests.utils.requote_uri(_article_url(slug)): _article("Šventė"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)
    # The wire carries the escaped form; the row keeps the
    # canonical one the dedupe key is built from
    assert any("%C5%A1vent%C4%97" in hit for hit in site.hits)
    assert [row["source_url"] for row in stored()] == [_article_url(slug)]


def test_a_link_with_a_space_in_its_path_is_still_followed(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/dvi dalys", "Dvi dalys")]),
        _listing_url(5): EMPTY_LISTING,
        f"{BASE}/aktualijos/dvi%20dalys": _article("Dvi dalys"),
    })

    assert scrape_knf_news(pages=1)["new"] == 1


def test_an_absurdly_long_link_is_followed_like_any_other(app, site):
    slug = "i" * 3000
    site.serve({
        _listing_url(): _listing([(f"/aktualijos/{slug}", "Ilga nuoroda")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url(slug): _article("Ilga nuoroda"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)
    assert site.hits_on(_article_url(slug)) == 1




# -----------------------------------------------------------
# The walk — the canonical dedupe key
# -----------------------------------------------------------


@pytest.mark.parametrize("param", [
    "utm_source=facebook",
    "utm_medium=email",
    "utm_campaign=rugsejis",
    "fbclid=IwAR0",
    "gclid=abc123",
    "mc_cid=1a2b",
    "mc_eid=3c4d",
    "_ga=GA1.2.3",
    "ref=naujienlaiskis",
])
def test_a_tracking_parameter_collapses_into_the_same_article(app, site, param, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/viena", "Viena"),
                                  (f"/aktualijos/viena?{param}", "Viena su zyme")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("viena"): _article("Viena"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)
    assert site.hits_on(_article_url("viena")) == 1
    assert len(stored()) == 1


@pytest.mark.parametrize("href", [
    "/aktualijos/viena/",
    "/aktualijos/viena#turinys",
    "https://www.knf.vu.lt/aktualijos/viena",
    "http://knf.vu.lt/aktualijos/viena",
    "https://knf.vu.lt:443/aktualijos/viena",
])
def test_another_shape_of_the_same_url_is_the_same_article(app, site, href, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/viena", "Viena"), (href, "Ta pati kitaip")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("viena"): _article("Viena"),
    })

    result = scrape_knf_news(pages=1)

    assert result["found"] == 1
    assert site.hits_on(_article_url("viena")) == 1
    assert len(stored()) == 1


def test_a_meaningful_query_parameter_keeps_two_links_apart(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/x?id=1", "Pirma"), ("/aktualijos/x?id=2", "Antra")]),
        _listing_url(5): EMPTY_LISTING,
        f"{_article_url('x')}?id=1": _article("Pirma"),
        f"{_article_url('x')}?id=2": _article("Antra"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (2, 2)
    assert sorted(row["source_url"] for row in stored()) == [f"{_article_url('x')}?id=1",
                                                             f"{_article_url('x')}?id=2"]


def test_two_paths_differing_only_in_case_are_two_articles(app, site):
    # The host is case-insensitive, the path is not
    site.serve({
        _listing_url(): _listing([("/aktualijos/svente", "Mazoji"),
                                  ("/aktualijos/Svente", "Didzioji")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("svente"): _article("Mazoji"),
        _article_url("Svente"): _article("Didzioji"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (2, 2)


def test_the_same_article_on_two_listing_pages_is_fetched_once_and_ends_the_walk(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/viena", "Viena")]),
        _listing_url(5): _listing([("/aktualijos/viena/", "Viena vel")]),
        _listing_url(10): EMPTY_LISTING,
        _article_url("viena"): _article("Viena"),
    })

    result = scrape_knf_news(pages=2)

    # Page two held nothing the run had not already seen, so the
    # walk stopped there
    assert (result["found"], result["new"]) == (1, 1)
    assert site.hits_on(_article_url("viena")) == 1
    assert site.hits_on(_listing_url(10)) == 0




# -----------------------------------------------------------
# The walk — the found counter
# -----------------------------------------------------------


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "\n\t ",
    "<img src=\"/images/nuotrauka.jpg\">",
    "<!-- redakcijos komentaras -->",
])
def test_a_link_with_no_readable_text_is_neither_counted_nor_fetched(app, site, text):
    site.serve({
        _listing_url(): _listing([("/aktualijos/tuscia", text), ("/aktualijos/gera", "Gera")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article("Gera"),
        _article_url("tuscia"): _article("Tuscia"),
    })

    result = scrape_knf_news(pages=1)

    assert result["found"] == 1
    assert site.hits_on(_article_url("tuscia")) == 0


def test_two_titled_anchors_to_the_same_article_in_one_card_are_one_article(app, site):
    card = _page("<div class=\"blog-article-title\">"
                 "<a href=\"/aktualijos/x\">Pavadinimas</a>"
                 "<a href=\"/aktualijos/x\">Skaityti daugiau</a></div>")
    site.serve({
        _listing_url(): card,
        _listing_url(5): EMPTY_LISTING,
        _article_url("x"): _article("Pavadinimas"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)
    assert site.hits_on(_article_url("x")) == 1


def test_a_thumbnail_anchor_does_not_swallow_the_titled_link_beside_it(app, site):
    # The Joomla blog card every generation can produce: the
    # image and the title are two anchors to the SAME article
    card = _page("<div class=\"blog-article-title\">"
                 "<a href=\"/aktualijos/x\"><img src=\"/images/thumb.jpg\"></a>"
                 "<a href=\"/aktualijos/x\">Tikras pavadinimas</a></div>")
    site.serve({
        _listing_url(): card,
        _listing_url(5): EMPTY_LISTING,
        _article_url("x"): _article("Tikras pavadinimas"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)
    assert site.hits_on(_article_url("x")) == 1


def test_a_listing_whose_links_all_lack_text_fails_the_run(app, site, runs):
    site.serve({_listing_url(): _listing([("/aktualijos/a", " "), ("/aktualijos/b", "")])})

    result = scrape_knf_news(pages=1)

    assert result["found"] == 0
    assert "template" in result["error"]
    assert runs(result["runId"])["status"] == "failed"


def test_the_link_text_is_read_through_its_markup(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/gera", "<span> <b>Tikra</b> </span>")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article("Tikra"),
    })

    assert scrape_knf_news(pages=1)["found"] == 1


def test_an_already_stored_link_is_counted_but_holds_nothing_unseen(app, site, seed_article):
    seed_article(_article_url("sena"))
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena")]),
        _article_url("sena"): _article("Sena"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 0)
    # Nothing unseen and the minimum was satisfied, so page two
    # is never requested and the article is never re-fetched
    assert site.hits == [_listing_url()]


def test_a_tombstoned_link_is_counted_but_holds_nothing_unseen(app, site, tombstone, stored):
    tombstone(_article_url("istrinta"))
    site.serve({
        _listing_url(): _listing([("/aktualijos/istrinta", "Istrinta")]),
        _article_url("istrinta"): _article("Istrinta"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 0)
    assert stored() == []
    assert site.hits == [_listing_url()]


def test_a_page_of_refused_links_holds_nothing_unseen(app, site, seed_article):
    seed_article(_article_url("sena"))
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena"),
                                  ("https://evil.example.com/aktualijos/x", "Piktybine")]),
        _article_url("sena"): _article("Sena"),
    })

    result = scrape_knf_news(pages=1)

    assert result["found"] == 1
    assert site.hits == [_listing_url()]




# -----------------------------------------------------------
# Paging — the offsets and `pages` as a MINIMUM
# -----------------------------------------------------------


def test_the_first_listing_page_carries_no_start_parameter(app, site, seed_article):
    seed_article(_article_url("sena"))
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena")]),
        _listing_url(5): EMPTY_LISTING,
    })

    scrape_knf_news(pages=2)

    assert site.hits == [NEWS, f"{NEWS}?start=5"]


def test_the_listing_offsets_step_five_at_a_time(app, site, seed_article):
    served = {}
    for page in range(4):
        seed_article(_article_url(f"sena{page}"))
        served[_listing_url(page * 5)] = _listing([(f"/aktualijos/sena{page}", f"Sena {page}")])
    site.serve(served)

    scrape_knf_news(pages=4)

    assert site.hits == [_listing_url(0), _listing_url(5), _listing_url(10), _listing_url(15)]


@pytest.mark.parametrize("pages", [0, -1, -100, 1])
def test_a_minimum_of_one_page_or_less_stops_after_the_first(app, site, seed_article, pages):
    seed_article(_article_url("sena"))
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena")]),
        _listing_url(5): EMPTY_LISTING,
    })

    scrape_knf_news(pages=pages)

    assert site.hits == [_listing_url()]


@pytest.mark.parametrize("pages, walked", [(2.5, 3), (3.0, 3), (2.0, 2)])
def test_a_fractional_minimum_stops_at_the_next_whole_page(app, site, seed_article, pages, walked):
    served = {}
    for page in range(4):
        seed_article(_article_url(f"sena{page}"))
        served[_listing_url(page * 5)] = _listing([(f"/aktualijos/sena{page}", f"Sena {page}")])
    site.serve(served)

    scrape_knf_news(pages=pages)

    assert len(site.hits) == walked


@pytest.mark.parametrize("pages", [10, 11, 1000, 10 ** 9])
def test_a_minimum_the_page_cap_cannot_reach_stops_at_ten_pages(app, site, seed_article, pages, runs):
    served = {}
    for page in range(12):
        seed_article(_article_url(f"sena{page}"))
        served[_listing_url(page * 5)] = _listing([(f"/aktualijos/sena{page}", f"Sena {page}")])
    site.serve(served)

    result = scrape_knf_news(pages=pages)

    assert site.hits == [_listing_url(offset * 5) for offset in range(knf_scraper.MAX_LISTING_PAGES)]
    assert result["found"] == knf_scraper.MAX_LISTING_PAGES == 10
    assert runs(result["runId"])["status"] == "completed"


@pytest.mark.parametrize("pages", [None, "2", [2]])
def test_a_minimum_that_is_not_a_number_fails_the_run_instead_of_escaping(app, site, seed_article,
                                                                          pages, runs, stored):
    seed_article(_article_url("sena"))
    site.serve({_listing_url(): _listing([("/aktualijos/sena", "Sena")])})

    result = scrape_knf_news(pages=pages)

    assert (result["found"], result["new"]) == (0, 0)
    assert result["error"]
    assert runs(result["runId"])["status"] == "failed"
    # The row an earlier run left is all there is: the failure
    # wrote nothing of its own
    assert [row["source_url"] for row in stored()] == [_article_url("sena")]


def test_paging_continues_past_the_minimum_while_a_page_keeps_yielding(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/a", "A")]),
        _listing_url(5): _listing([("/aktualijos/b", "B")]),
        _listing_url(10): EMPTY_LISTING,
        _article_url("a"): _article("A"),
        _article_url("b"): _article("B"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (2, 2)
    assert site.listing_hits() == [_listing_url(), _listing_url(5), _listing_url(10)]


def test_an_article_that_failed_to_download_still_counts_as_unseen(app, site):
    # No row is written for it, so the next run retries it — and
    # the page it sat on counts as one that yielded something
    site.serve({
        _listing_url(): _listing([("/aktualijos/dingusi", "Dingusi")]),
        _listing_url(5): EMPTY_LISTING,
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 0)
    assert site.hits_on(_listing_url(5)) == 1


def test_pages_of_articles_that_all_fail_to_download_keep_the_walk_going_to_the_cap(app, site, runs):
    # Every page yields something unseen, so no page ever ends
    # the walk — the listing cap is the only thing that does
    site.serve({_listing_url(page * 5): _listing([(f"/aktualijos/p{page}", f"Puslapis {page}")])
                for page in range(knf_scraper.MAX_LISTING_PAGES + 2)})

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (knf_scraper.MAX_LISTING_PAGES, 0)
    assert len(site.listing_hits()) == knf_scraper.MAX_LISTING_PAGES
    assert runs(result["runId"])["status"] == "completed"




# -----------------------------------------------------------
# Paging — listing pages that do not arrive
# -----------------------------------------------------------


def test_a_listing_page_that_never_answers_does_not_end_the_walk(app, site, seed_article, runs):
    # A page that fails to download skips the stop conditions
    # entirely, so the walk runs on to the page cap
    seed_article(_article_url("sena"))
    site.serve({_listing_url(): _listing([("/aktualijos/sena", "Sena")])})

    result = scrape_knf_news(pages=2)

    assert len(site.listing_hits()) == knf_scraper.MAX_LISTING_PAGES
    assert site.hits[-1] == _listing_url(45)
    assert (result["found"], result["new"]) == (1, 0)
    assert runs(result["runId"])["status"] == "completed"


@pytest.mark.parametrize("status", [404, 410, 500, 502, 503])
def test_a_listing_page_answering_an_error_status_is_skipped(app, site, status):
    site.serve({
        _listing_url(): _Served("<html><body>Klaida</body></html>", status=status),
        _listing_url(5): _listing([("/aktualijos/antra", "Antra")]),
        _listing_url(10): EMPTY_LISTING,
        _article_url("antra"): _article("Antra"),
    })

    result = scrape_knf_news(pages=2)

    assert (result["found"], result["new"]) == (1, 1)


def test_a_listing_served_as_json_is_skipped_and_the_run_still_completes(app, site, runs):
    site.serve({_listing_url(): _Served("{\"naujienos\": []}", content_type="application/json")})

    result = scrape_knf_news(pages=1)

    # Nothing was parsed, so nothing "downloaded" — this is the
    # site misbehaving, not the template changing
    assert (result["found"], result["new"]) == (0, 0)
    assert "error" not in result
    assert runs(result["runId"])["status"] == "completed"


@pytest.mark.parametrize("content_type", [
    "text/html",
    "text/html; charset=utf-8",
    "TEXT/HTML; charset=UTF-8",
    "text/html;charset=windows-1257",
    "application/xhtml+xml",
])
def test_every_html_content_type_the_source_may_declare_is_parsed(app, site, content_type):
    site.serve({
        _listing_url(): _Served(_listing([("/aktualijos/gera", "Gera")]), content_type=content_type),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article("Gera"),
    })

    assert scrape_knf_news(pages=1)["new"] == 1


@pytest.mark.parametrize("content_type", [
    "application/json",
    "application/pdf",
    "image/png",
    "text/plain",
])
def test_a_listing_declaring_a_type_we_cannot_parse_is_skipped(app, site, content_type, runs):
    site.serve({_listing_url(): _Served(_listing([("/aktualijos/gera", "Gera")]),
                                        content_type=content_type)})

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (0, 0)
    assert runs(result["runId"])["status"] == "completed"
    assert site.article_hits() == []


@pytest.mark.parametrize("body", [
    "",
    "   ",
    "kazkoks tekstas be zymu",
    "{\"straipsniai\": []}",
    "<!doctype html><html><body></body></html>",
])
def test_a_listing_body_that_holds_no_article_link_fails_the_run(app, site, body, runs):
    site.serve({_listing_url(): body})

    result = scrape_knf_news(pages=1)

    assert "template" in result["error"]
    assert runs(result["runId"])["status"] == "failed"


def test_a_listing_served_without_a_content_type_is_parsed_anyway(app, site):
    site.serve({
        _listing_url(): _Served(_listing([("/aktualijos/gera", "Gera")]), content_type=""),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article("Gera"),
    })

    assert scrape_knf_news(pages=1)["new"] == 1


def test_a_site_that_never_answers_completes_rather_than_blaming_the_template(app, site, runs):
    result = scrape_knf_news(pages=2)

    assert (result["found"], result["new"]) == (0, 0)
    assert "error" not in result
    assert runs(result["runId"])["status"] == "completed"
    assert len(site.listing_hits()) == knf_scraper.MAX_LISTING_PAGES


@pytest.mark.slow
def test_a_listing_body_over_the_byte_cap_keeps_the_links_before_the_cut(app, site):
    padding = "<!-- " + ("x" * (common.MAX_RESPONSE_BYTES + 1000))
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]) + padding,
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article("Pirma"),
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)




# -----------------------------------------------------------
# Paging — the two caps at their exact boundary
# -----------------------------------------------------------


def test_exactly_the_article_cap_on_one_page_is_all_fetched_and_ends_the_walk(app, site, stored,
                                                                              caplog):
    items, pages = _many(knf_scraper.MAX_ARTICLE_FETCHES)
    site.serve({_listing_url(): _listing(items), **pages})

    with caplog.at_level(logging.INFO, logger="app.scraper.knf_scraper"):
        result = scrape_knf_news(pages=2)

    assert (result["found"], result["new"]) == (20, 20)
    assert len(stored()) == 20
    assert site.hits_on(_listing_url(5)) == 0
    assert "fetch cap or budget" in caplog.text


def test_the_link_after_the_article_cap_is_counted_but_never_fetched(app, site, stored):
    items, pages = _many(knf_scraper.MAX_ARTICLE_FETCHES + 1)
    site.serve({_listing_url(): _listing(items), **pages})

    result = scrape_knf_news(pages=2)

    # found is what the listing offered, new is what the run
    # could take
    assert (result["found"], result["new"]) == (21, 20)
    assert len(stored()) == 20
    assert site.hits_on(_article_url("a20")) == 0


def test_a_huge_listing_page_never_fetches_more_than_the_cap(app, site):
    items, pages = _many(300)
    site.serve({_listing_url(): _listing(items), **pages})

    result = scrape_knf_news(pages=2)

    assert (result["found"], result["new"]) == (21, 20)
    assert len(site.article_hits()) == knf_scraper.MAX_ARTICLE_FETCHES == 20


def test_the_cap_is_counted_across_pages_not_per_page(app, site):
    served = {}
    for page in range(5):
        items, pages = _many(6, prefix=f"p{page}-")
        served[_listing_url(page * 5)] = _listing(items)
        served.update(pages)
    site.serve(served)

    result = scrape_knf_news(pages=2)

    # Six per page: the cap falls in the middle of the fourth
    assert (result["found"], result["new"]) == (21, 20)
    assert site.hits_on(_listing_url(20)) == 0


def test_the_walk_never_asks_for_an_eleventh_listing_page(app, site):
    served = {}
    for page in range(12):
        served[_listing_url(page * 5)] = _listing([(f"/aktualijos/p{page}", f"Puslapis {page}")])
        served[_article_url(f"p{page}")] = _article(f"Puslapis {page}")
    site.serve(served)

    result = scrape_knf_news(pages=2)

    assert result["found"] == knf_scraper.MAX_LISTING_PAGES == 10
    assert site.hits_on(_listing_url(45)) == 1
    assert site.hits_on(_listing_url(50)) == 0


def test_the_newest_article_of_the_walk_is_the_one_logged(app, site, caplog):
    # The listing is newest-first, so every article after the
    # first one leaves the running maximum alone
    site.serve({
        _listing_url(): _listing([("/aktualijos/nauja", "Nauja"), ("/aktualijos/sena", "Sena")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("nauja"): _article("Nauja", published="2026-06-12T09:00:00+03:00"),
        _article_url("sena"): _article("Sena", published="2026-06-10T09:00:00+03:00"),
    })

    with time_machine.travel(FROZEN, tick=False):
        with caplog.at_level(logging.INFO, logger="app.scraper.knf_scraper"):
            result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (2, 2)
    assert "newest=2026-06-12T06:00:00" in caplog.text


def test_the_articles_are_fetched_in_listing_order(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/a", "A"), ("/aktualijos/b", "B"),
                                  ("/aktualijos/c", "C")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("a"): _article("A"),
        _article_url("b"): _article("B"),
        _article_url("c"): _article("C"),
    })

    scrape_knf_news(pages=1)

    assert site.article_hits() == [_article_url("a"), _article_url("b"), _article_url("c")]




# -----------------------------------------------------------
# Paging — the wall-clock budget
# -----------------------------------------------------------


def test_a_budget_of_zero_seconds_stops_before_the_first_listing_page(app, site, monkeypatch,
                                                                      runs, caplog):
    monkeypatch.setattr(knf_scraper, "RUN_BUDGET_SECONDS", 0)
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _article_url("pirma"): _article("Pirma")})

    with time_machine.travel(FROZEN, tick=False):
        with caplog.at_level(logging.WARNING, logger="app.scraper.knf_scraper"):
            result = scrape_knf_news(pages=2)

    assert (result["found"], result["new"]) == (0, 0)
    assert site.hits == []
    # Nothing downloaded, so this is "the site is slow", never
    # "the template changed"
    assert runs(result["runId"])["status"] == "completed"
    assert "out of time after 0 listing page(s)" in caplog.text


def test_a_budget_spent_while_the_listing_downloads_stops_before_any_article(app, site):
    with time_machine.travel(FROZEN, tick=False) as clock:

        def _slow_listing(request):
            clock.shift(knf_scraper.RUN_BUDGET_SECONDS + 1)
            return _listing([("/aktualijos/pirma", "Pirma")])

        site.serve({_listing_url(): _slow_listing,
                    _listing_url(5): EMPTY_LISTING,
                    _article_url("pirma"): _article("Pirma")})

        result = scrape_knf_news(pages=2)

    # The link was counted before the budget was tested, but no
    # article page was ever asked for
    assert (result["found"], result["new"]) == (1, 0)
    assert site.hits == [_listing_url()]


def test_a_budget_spent_during_an_article_download_leaves_the_rest_of_the_page(app, site, stored):
    with time_machine.travel(FROZEN, tick=False) as clock:

        def _slow_article(request):
            clock.shift(knf_scraper.RUN_BUDGET_SECONDS + 1)
            return _article("Pirma")

        site.serve({
            _listing_url(): _listing([("/aktualijos/pirma", "Pirma"), ("/aktualijos/antra", "Antra")]),
            _listing_url(5): EMPTY_LISTING,
            _article_url("pirma"): _slow_article,
            _article_url("antra"): _article("Antra"),
        })

        result = scrape_knf_news(pages=2)

    assert (result["found"], result["new"]) == (2, 1)
    assert site.hits_on(_article_url("antra")) == 0
    assert [row["title"] for row in stored()] == ["Pirma"]


def test_a_budget_spent_on_a_page_that_failed_to_download_is_caught_at_the_top_of_the_walk(
        app, site, caplog):
    # A page that fails to download skips both stop conditions,
    # so the check at the TOP of the loop is the only one left
    with time_machine.travel(FROZEN, tick=False) as clock:

        def _dead_page(request):
            clock.shift(knf_scraper.RUN_BUDGET_SECONDS + 1)
            return _Served("<html><body>Klaida</body></html>", status=500)

        site.serve({
            _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
            _listing_url(5): _dead_page,
            _article_url("pirma"): _article("Pirma"),
        })

        with caplog.at_level(logging.WARNING, logger="app.scraper.knf_scraper"):
            result = scrape_knf_news(pages=2)

    assert (result["found"], result["new"]) == (1, 1)
    assert "out of time after 2 listing page(s)" in caplog.text
    assert site.hits_on(_listing_url(10)) == 0




# -----------------------------------------------------------
# The template-change verdict
# -----------------------------------------------------------


def test_the_verdict_writes_no_rows_and_closes_the_run_as_failed(app, site, runs, stored):
    site.serve({_listing_url(): EMPTY_LISTING})

    result = scrape_knf_news(pages=1)

    assert result == {"found": 0, "new": 0, "error": result["error"], "runId": result["runId"]}
    assert "template has probably changed" in result["error"]
    row = runs(result["runId"])
    assert row["status"] == "failed"
    assert "template" in row["error_message"]
    assert row["finished_at"]
    assert stored() == []


def test_a_later_page_that_yields_links_saves_the_run_from_the_verdict(app, site, runs):
    # The verdict is run-wide, not per page: page one downloaded
    # and held nothing, page two held an article
    site.serve({
        _listing_url(): EMPTY_LISTING,
        _listing_url(5): _listing([("/aktualijos/antra", "Antra")]),
        _listing_url(10): EMPTY_LISTING,
        _article_url("antra"): _article("Antra"),
    })

    result = scrape_knf_news(pages=2)

    assert (result["found"], result["new"]) == (1, 1)
    assert runs(result["runId"])["status"] == "completed"


def test_ten_downloaded_listing_pages_that_yield_nothing_fail_the_run(app, site, runs, caplog):
    site.serve({_listing_url(page * 5): EMPTY_LISTING for page in range(knf_scraper.MAX_LISTING_PAGES)})

    with caplog.at_level(logging.ERROR, logger="app.scraper.knf_scraper"):
        result = scrape_knf_news(pages=1000)

    assert len(site.listing_hits()) == knf_scraper.MAX_LISTING_PAGES
    assert "template" in result["error"]
    assert runs(result["runId"])["status"] == "failed"
    assert "10 downloaded listing page(s)" in caplog.text


def test_an_unrecognised_markup_generation_fails_the_run(app, site, runs):
    # Every selector generation missing at once is what a
    # template rewrite looks like from here
    site.serve({_listing_url(): _page("<div class=\"blog\"><article>"
                                      "<p><a href=\"/aktualijos/nauja\">Nauja</a></p>"
                                      "</article></div>")})

    result = scrape_knf_news(pages=1)

    assert runs(result["runId"])["status"] == "failed"
    assert site.hits == [_listing_url()]




# -----------------------------------------------------------
# Quirks of the walk worth pinning
# -----------------------------------------------------------


def test_the_section_index_link_is_followed_like_any_other_article(app, site, stored):
    # "/aktualijos/" carries the segment, so the listing page
    # itself becomes a candidate — it is fetched a second time
    # and stored under the link text
    site.serve({
        _listing_url(): _listing([("/aktualijos/", "Aktualijos")]),
        _listing_url(5): EMPTY_LISTING,
    })

    result = scrape_knf_news(pages=1)

    assert (result["found"], result["new"]) == (1, 1)
    assert site.hits_on(_listing_url()) == 2
    assert [row["source_url"] for row in stored()] == [NEWS]


def test_a_trigger_fired_in_the_middle_of_the_walk_steps_aside(app, site):
    # The overlapping run is staged from inside the download of
    # listing page one, which is the narrowest window there is
    inner = []

    def _listing_and_a_second_trigger(request):
        inner.append(scrape_knf_news(pages=1))
        return _listing([("/aktualijos/pirma", "Pirma")])

    site.serve({
        _listing_url(): _listing_and_a_second_trigger,
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article("Pirma"),
    })

    result = scrape_knf_news(pages=1)

    assert inner == [{"found": 0, "new": 0, "skipped": True}]
    # The run that holds the lock is untouched by the one that
    # stepped aside
    assert (result["found"], result["new"]) == (1, 1)
    assert "runId" in result


def test_a_run_over_a_listing_it_has_already_read_asks_for_no_article_page(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article("Pirma"),
    })

    assert scrape_knf_news(pages=1, notify=False)["new"] == 1
    first_pass = len(site.hits)

    second = scrape_knf_news(pages=1, notify=False)

    assert (second["found"], second["new"]) == (1, 0)
    # One listing page and nothing else: the stored row is the
    # fetch-avoidance path
    assert site.hits[first_pass:] == [_listing_url()]


def test_the_sidebar_of_the_current_template_is_left_behind(app, site, stored):
    listing = _page("<div class=\"blog\">"
                    "<h2 class=\"article-title\"><a href=\"/aktualijos/pirma\">Pirma</a></h2>"
                    "</div>"
                    "<aside><h2 class=\"article-title\"><a href=\"/studijos\">Studijos</a></h2>"
                    "<h4><a href=\"/apie-mus\">Apie mus</a></h4></aside>")
    site.serve({
        _listing_url(): listing,
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article("Pirma"),
    })

    result = scrape_knf_news(pages=1)

    assert result["found"] == 1
    assert [row["source_url"] for row in stored()] == [_article_url("pirma")]
