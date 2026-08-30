# -----------------------------------------------------------
#  [*] Tests — knf_scraper, the article half: fields, dedupe,
#      tombstones and the published_at clamp
#
#  The exhaustive pass over the SECOND half of
#  scraper/knf_scraper.py — _fetch_article's six field
#  chains and everything scrape_knf_news does with the parsed
#  result once every fetch of the run is done. The listing
#  walk, the caps, the budget, the run lock and the push are
#  another agent's slice and are only touched here where a
#  field or a dedupe decision cannot be reached without them.
#
#  What this module proves, chain by chain:
#
#    - title: og:title minus BOTH site prefixes, only the
#      FIRST prefix stripped, an og:title that is nothing but
#      the prefix falling through to the <h1> chain, the
#      section-name filter (case-folded, empty h1 included),
#      and the 200-character column limit at exactly 200,
#      201 and on the listing link text of last resort
#    - content: the three clean selectors in order, an
#      .article-content that MATCHED but held no text still
#      falling through to the broad chain, all three broad
#      selectors, the >30-character crumb cut at 30 and 31,
#      every one of the five decomposed tags, and the 10000-
#      character limit at 10000 and 10001
#    - summary: the cut at exactly 200 and 201 characters,
#      a 201-character run with no space in it at all, a
#      space landing exactly on the cut, and empty content
#    - image: og:image against the first content <img>, all
#      five chrome words (upper case too), a blank src, a
#      missing src, an off-allowlist src, an over-length
#      src — each one falling through to the NEXT candidate
#      rather than ending the search — and resolution
#      against the POST-REDIRECT page URL
#    - date: the four sources in order, a container that
#      exists but holds no <time>, a <time> whose datetime
#      attribute is empty falling back to its text, a broken
#      first <time> and a broken first meta, the offset
#      APPLIED (positive and negative) and the clamp at
#      exactly now, one second future, exactly five years and
#      one second past five years
#    - author: the selector chain, the whole-document reach
#      of select_one, and the default
#    - storage: the canonical source_url, the row shape a
#      guest is served, dedupe by URL and by CUT title, the
#      title dedupe scoped to this source and case-sensitive,
#      two same-titled articles inside ONE run, a row a
#      racing run wrote first costing one INSERT OR IGNORE,
#      and the found/new arithmetic under each of those
#    - tombstones: matched in normalise_url shape, loaded
#      ONCE per run, NULL and empty rows ignored, counted in
#      "found" and never fetched
#
#  Every fetch goes through `responses`; the container runs
#  --network none, so a test that reached knf.vu.lt fails by
#  construction. Nothing sleeps — the clamp boundaries are
#  driven with time_machine.
# -----------------------------------------------------------


import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses
import time_machine

from app.scraper import common
from app.scraper.knf_scraper import scrape_knf_news


BASE = "https://knf.vu.lt"
NEWS = f"{BASE}/aktualijos"

# Both host spellings answer: a listing may link its own
# articles through www., and normalise_url collapses them
KNF_PATTERN = re.compile(r"https?://(?:www\.)?knf\.vu\.lt/.*")

# A wall clock chosen for readability — every clamp boundary
# below is expressed against it
FROZEN = "2026-06-15 12:00:00 +0000"
FROZEN_ISO = "2026-06-15T12:00:00"
FROZEN_DT = datetime(2026, 6, 15, 12, 0, 0)

DEFAULT_CONTENT = "Fakultetas kviecia studentus i rugsejo pirmosios svente ir i atviru duru diena."




# -----------------------------------------------------------
# _listing_url / _article_url
# -----------------------------------------------------------
#
# The exact strings the scraper builds — Joomla pages five at
# a time through ?start=<offset>. Keying the fixture map by
# these means a typo shows up as "the page is missing", never
# as a silent pass.
# -----------------------------------------------------------

def _listing_url(offset=0):
    return NEWS if offset == 0 else f"{NEWS}?start={offset}"


def _article_url(slug):
    return f"{BASE}/aktualijos/{slug}"




# -----------------------------------------------------------
# _page
# -----------------------------------------------------------
#
# One document in the shape the parser meets: a declared
# charset (the body arrives as BYTES, so the sniffed charset
# is what saves the diacritics) and the site chrome every
# real page carries. The header logo is deliberate — it is
# the <img> the chrome filter has to reject before it can
# reach a real one.
# -----------------------------------------------------------

def _page(body, head=""):
    return (
        "<!doctype html><html lang=\"lt\"><head><meta charset=\"utf-8\">"
        f"<title>VU Kauno fakultetas</title>{head}</head>"
        "<body><header><nav><a href=\"/kontaktai\">Kontaktai</a>"
        "<img src=\"/images/logo.png\"></nav></header>"
        f"{body}</body></html>"
    )




# -----------------------------------------------------------
# _listing
# -----------------------------------------------------------
#
# A current-template listing page carrying the given
# (href, link text) pairs. Only the current generation is
# built here: the older two belong to the listing agent's
# slice, and every test below is about what happens AFTER a
# link is recognised.
# -----------------------------------------------------------

def _listing(items):
    blocks = "".join(
        f"<div class=\"item\"><h2 class=\"article-title\"><a href=\"{href}\">{text}</a></h2></div>"
        for href, text in items
    )
    return _page(f"<div class=\"blog\">{blocks}</div>")


# The end of the listing: downloads fine, holds no article
# link, which is what stops the walk after the minimum pages
EMPTY_LISTING = _page("<div class=\"blog\"><p>Daugiau naujienu nera.</p></div>")




# -----------------------------------------------------------
# _article
# -----------------------------------------------------------
#
#   _article()                       — the current template
#   _article(og_title=None)          — no og:title at all
#   _article(content_class="item-content")
#   _article(head_extra="<meta ...>")
#   _article(inner_extra="<time ...>")  — inside the body
#   _article(body_extra="<span ...>")   — inside .item-page
#
# One article page: an .item-page wrapper around a content
# div. Every knob maps to exactly one fallback chain in
# _fetch_article, so a test names the single thing it varies
# and the shapes with no container at all are built from
# _page directly in the test that needs them.
# -----------------------------------------------------------

def _article(og_title="VU Kauno fakultetas - Pirma naujiena",
             content=DEFAULT_CONTENT,
             content_class="article-content",
             head_extra="",
             inner_extra="",
             body_extra=""):
    head = ""
    if og_title:
        head += f"<meta property=\"og:title\" content=\"{og_title}\">"
    head += head_extra

    inner = inner_extra + (f"<p>{content}</p>" if content else "")
    body = f"<div class=\"item-page\"><div class=\"{content_class}\">{inner}</div>{body_extra}</div>"

    return _page(body, head=head)




# -----------------------------------------------------------
# _Site
# -----------------------------------------------------------
#
# The fake knf.vu.lt. `serve` adds pages, the callback
# answers them and records every request. A URL that was
# never served raises the ConnectionError requests would
# raise for a host that is not answering, so "the page is
# missing" needs no extra machinery. A served page may be:
#
#   str                      — a 200 text/html body
#   (status, body)           — any status
#   (status, headers, body)  — a redirect, for the post-
#                              redirect image resolution
#   callable(request)        — served for its SIDE EFFECT as
#                              well, which is how a racing
#                              writer is staged mid-run
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
            return requests.exceptions.ConnectionError(f"knf.vu.lt unreachable: {request.url}")

        if callable(page):
            page = page(request)

        if isinstance(page, tuple) and len(page) == 3:
            status, headers, body = page
        elif isinstance(page, tuple):
            status, body = page
            headers = {}
        else:
            status, headers, body = 200, {}, page

        return status, headers, body.encode("utf-8")

    def hits_on(self, url):
        return self.hits.count(url)




# -----------------------------------------------------------
# site
# -----------------------------------------------------------
#
# Used by:
#   - every test here; without it a fetch would leave the
#     container, which --network none forbids
# -----------------------------------------------------------

@pytest.fixture
def site():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        fake = _Site()
        mock.add_callback(responses.GET, KNF_PATTERN, callback=fake.answer,
                          content_type="text/html; charset=utf-8")
        yield fake




# -----------------------------------------------------------
# forget_push_history
# -----------------------------------------------------------
#
# common._LAST_PUSH is a PROCESS-global map. Nothing here
# asks for a push, but the module is shared with the suite's
# push tests and a leaked entry must not depend on run order.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def forget_push_history():
    common._LAST_PUSH.clear()
    yield
    common._LAST_PUSH.clear()




# -----------------------------------------------------------
# stored
# -----------------------------------------------------------
#
#   stored()      — every scraped row, title order
#   stored(url)   — the row for one source_url, or None
# -----------------------------------------------------------

@pytest.fixture
def stored(db):

    def _stored(url=None):
        if url is not None:
            return db.execute("SELECT * FROM news_posts WHERE source_url = ?", (url,)).fetchone()
        return db.execute("SELECT * FROM news_posts ORDER BY title").fetchall()

    return _stored




# -----------------------------------------------------------
# plant_post
# -----------------------------------------------------------
#
# A news_posts row written straight to the database — what an
# earlier run, another source, or a run racing this one would
# have left behind.
# -----------------------------------------------------------

@pytest.fixture
def plant_post(app):

    def _plant(source_url=None, title="Sena naujiena", source="knf.vu.lt"):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO news_posts
                   (id, title, content, summary, source, source_url, post_type, published_at)
                   VALUES (?, ?, 'Turinys', 'Santrauka', ?, ?, 'article', '2026-01-01T00:00:00')""",
                (str(uuid.uuid4()), title, source, source_url),
            )
            conn.commit()
        finally:
            conn.close()

    return _plant




# -----------------------------------------------------------
# tombstone
# -----------------------------------------------------------
#
# A deleted_source_urls row in whatever shape the caller
# wants to prove is still matched — the scraper normalises
# both sides before comparing.
# -----------------------------------------------------------

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
# _one
# -----------------------------------------------------------
#
# The whole arrangement most tests need: one listing page
# holding one link, one article page, an empty second listing
# page so the default two-page minimum terminates. Returns
# the scrape result.
# -----------------------------------------------------------

def _one(site, article_html, slug="pirma", link_text="Pirma"):
    site.serve({
        _listing_url(): _listing([(f"/aktualijos/{slug}", link_text)]),
        _listing_url(5): EMPTY_LISTING,
        _article_url(slug): article_html,
    })
    return scrape_knf_news(notify=False)




# ###########################################################
# _fetch_article — title
# ###########################################################


def test_the_og_title_is_stored_with_the_hyphen_site_prefix_removed(app, site, stored):
    _one(site, _article(og_title="VU Kauno fakultetas - Rugsejo pirmoji"))

    assert stored(_article_url("pirma"))["title"] == "Rugsejo pirmoji"


def test_the_en_dash_site_prefix_is_removed_too(app, site, stored):
    _one(site, _article(og_title="VU Kauno fakultetas – Rugsejo pirmoji"))

    assert stored(_article_url("pirma"))["title"] == "Rugsejo pirmoji"


def test_only_the_first_matching_prefix_is_stripped(app, site, stored):
    # The strip loop breaks on its first hit, so a doubled
    # prefix keeps its second copy — one pass, not a while
    _one(site, _article(og_title="VU Kauno fakultetas - VU Kauno fakultetas - Dvigubas"))

    assert stored(_article_url("pirma"))["title"] == "VU Kauno fakultetas - Dvigubas"


def test_a_title_that_only_looks_like_the_prefix_is_kept_whole(app, site, stored):
    _one(site, _article(og_title="VU Kauno fakulteto naujiena"))

    assert stored(_article_url("pirma"))["title"] == "VU Kauno fakulteto naujiena"


def test_an_og_title_that_is_nothing_but_the_prefix_falls_through_to_the_h1(app, site, stored):
    # Stripping the prefix can empty the title outright, and
    # the h1 chain has to run afterwards for that to be caught
    html = _page(
        "<div class=\"item-page\"><h1>Tikras pavadinimas</h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>",
        head="<meta property=\"og:title\" content=\"VU Kauno fakultetas - \">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Tikras pavadinimas"


def test_an_og_title_meta_with_no_content_attribute_falls_through_to_the_h1(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><h1>Antrastes pavadinimas</h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>",
        head="<meta property=\"og:title\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Antrastes pavadinimas"


def test_an_empty_og_title_content_falls_through_to_the_h1(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><h1>Is antrastes</h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>",
        head="<meta property=\"og:title\" content=\"\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Is antrastes"


@pytest.mark.parametrize("section", ["Aktualijos", "Naujienos", "Renginiai"])
def test_a_section_name_h1_is_never_the_title(app, site, stored, section):
    html = _page(
        f"<div class=\"item-page\"><h1>{section}</h1><h1>Straipsnio pavadinimas</h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>"
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Straipsnio pavadinimas"


def test_the_section_name_filter_is_case_folded(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><h1>AKTUALIJOS</h1><h1>Tikras</h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>"
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Tikras"


def test_an_h1_holding_only_whitespace_is_skipped(app, site, stored):
    # get_text(strip=True) leaves "", which the filter tuple
    # carries explicitly — an empty heading is not a title
    html = _page(
        "<div class=\"item-page\"><h1>   </h1><h1>Gera antraste</h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>"
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Gera antraste"


def test_the_first_usable_h1_wins_over_every_later_one(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><h1>Pirmoji</h1><h1>Antroji</h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>"
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Pirmoji"


def test_the_og_title_beats_an_h1(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><h1>Is antrastes</h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>",
        head="<meta property=\"og:title\" content=\"Is og\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Is og"


def test_a_page_with_no_title_source_at_all_is_stored_under_the_listing_link_text(app, site, stored):
    # No og:title and no h1: _fetch_article hands back an
    # EMPTY title on purpose so the caller can fall back
    html = _page(f"<div class=\"item-page\"><div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>")
    _one(site, html, link_text="Nuorodos tekstas")

    assert stored(_article_url("pirma"))["title"] == "Nuorodos tekstas"


def test_a_page_whose_only_h1s_are_section_names_falls_back_to_the_listing_text(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><h1>Aktualijos</h1><h1>Naujienos</h1><h1>Renginiai</h1><h1></h1>"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>"
    )
    _one(site, html, link_text="Nuoroda")

    assert stored(_article_url("pirma"))["title"] == "Nuoroda"


def test_a_title_of_exactly_two_hundred_characters_is_stored_whole(app, site, stored):
    exact = "T" * 200
    _one(site, _article(og_title=exact))

    assert stored(_article_url("pirma"))["title"] == exact


def test_a_title_one_character_over_the_limit_is_cut_to_two_hundred(app, site, stored):
    _one(site, _article(og_title="T" * 201))

    row = stored(_article_url("pirma"))
    assert len(row["title"]) == 200
    assert row["title"] == "T" * 200


def test_the_site_prefix_is_stripped_before_the_length_limit_bites(app, site, stored):
    # Cutting first would leave the prefix inside the stored
    # title and lose 22 characters of the real one
    _one(site, _article(og_title="VU Kauno fakultetas - " + "T" * 210))

    assert stored(_article_url("pirma"))["title"] == "T" * 200


def test_a_listing_link_text_over_the_limit_is_cut_by_the_caller(app, site, stored):
    html = _page(f"<div class=\"item-page\"><div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>")
    _one(site, html, link_text="N" * 250)

    assert stored(_article_url("pirma"))["title"] == "N" * 200




def test_an_html_entity_in_the_og_title_is_unescaped_before_storage(app, site, stored):
    _one(site, _article(og_title="Studentai &amp; destytojai"))

    assert stored(_article_url("pirma"))["title"] == "Studentai & destytojai"


def test_the_first_og_title_meta_wins_when_the_template_emits_two(app, site, stored):
    html = _article(og_title="Pirmoji meta",
                    head_extra="<meta property=\"og:title\" content=\"Antroji meta\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["title"] == "Pirmoji meta"




# ###########################################################
# _fetch_article — the download that never parses
# ###########################################################


def test_an_article_whose_download_fails_writes_no_row_and_is_retried_next_run(app, site, stored):
    # _fetch_article answers None, the caller skips it, and
    # with NO row written the next run tries the same article
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
    })
    first = scrape_knf_news(notify=False)

    assert first["found"] == 1
    assert first["new"] == 0
    assert stored() == []

    site.serve({_article_url("pirma"): _article()})
    second = scrape_knf_news(notify=False)

    assert second["new"] == 1
    assert stored(_article_url("pirma")) is not None


def test_an_article_answering_404_is_skipped_without_failing_the_run(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/dingo", "Dingo"), ("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("dingo"): (404, _page("<p>Nerasta</p>")),
        _article_url("pirma"): _article(),
    })
    result = scrape_knf_news(notify=False)

    assert "error" not in result
    assert result["found"] == 2
    assert result["new"] == 1
    assert stored(_article_url("dingo")) is None


def test_a_listing_anchor_with_no_text_is_never_fetched_or_stored(app, site, stored):
    # This is the guard that makes the title of last resort
    # non-empty: every candidate that survives it carries link
    # text, so a stored row can never end up with no title
    site.serve({
        _listing_url(): _listing([("/aktualijos/tuscia", ""), ("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("tuscia"): _article(og_title=None, content=""),
        _article_url("pirma"): _article(),
    })
    result = scrape_knf_news(notify=False)

    assert result["found"] == 1
    assert result["new"] == 1
    assert site.hits_on(_article_url("tuscia")) == 0
    assert all(row["title"] for row in stored())




# ###########################################################
# _fetch_article — content
# ###########################################################


def test_the_article_content_selector_beats_the_two_older_ones(app, site, stored):
    html = _page(
        "<div class=\"item-page\">"
        "<div class=\"article-content\"><p>Dabartinis</p></div>"
        "<div class=\"article-body\"><p>Senesnis</p></div>"
        "<div class=\"item-content\"><p>Seniausias</p></div>"
        "</div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == "Dabartinis"


def test_the_item_page_article_body_selector_is_the_second_generation(app, site, stored):
    html = _page(
        "<div class=\"item-page\">"
        "<div class=\"article-body\"><p>Senesnis</p></div>"
        "<div class=\"item-content\"><p>Seniausias</p></div>"
        "</div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == "Senesnis"


def test_the_item_content_selector_is_the_third_generation(app, site, stored):
    _one(site, _article(content_class="item-content", content="Seniausias turinys"))

    assert stored(_article_url("pirma"))["content"] == "Seniausias turinys"


def test_an_article_content_that_matched_but_held_no_text_still_falls_to_the_broad_chain(app, site, stored):
    # The clean loop BREAKS on a matching element, but content
    # is still empty afterwards, so the broad chain has to run
    # or a template that renamed its inner wrapper stores ""
    html = _page(
        "<div class=\"item-page\"><div class=\"article-content\"></div>"
        "<p>Placiaji atsargini pasirinkima reikia paleisti ir po tuscios atitikties.</p></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert "atsargini" in stored(_article_url("pirma"))["content"]


def test_the_broad_item_page_selector_is_the_first_fallback(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><p>Tekstas kuris tikrai virsija trisdesimt simboliu riba.</p></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == \
        "Tekstas kuris tikrai virsija trisdesimt simboliu riba."


def test_the_broad_article_element_is_the_second_fallback(app, site, stored):
    html = _page(
        "<article><p>Tekstas kuris tikrai virsija trisdesimt simboliu riba.</p></article>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == \
        "Tekstas kuris tikrai virsija trisdesimt simboliu riba."


def test_the_broad_content_content_selector_is_the_last_fallback(app, site, stored):
    html = _page(
        "<div id=\"content\"><div class=\"content\">"
        "<p>Tekstas kuris tikrai virsija trisdesimt simboliu riba.</p></div></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == \
        "Tekstas kuris tikrai virsija trisdesimt simboliu riba."


def test_a_leading_line_of_exactly_thirty_characters_is_shed_as_a_crumb(app, site, stored):
    # The cut is "> 30", so 30 is still a navigation crumb
    crumb = "a" * 30
    body = "b" * 31
    html = _page(
        f"<div class=\"item-page\"><p>{crumb}</p><p>{body}</p></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == body


def test_a_leading_line_of_thirty_one_characters_is_kept_as_real_text(app, site, stored):
    first = "a" * 31
    second = "b" * 40
    html = _page(
        f"<div class=\"item-page\"><p>{first}</p><p>{second}</p></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == f"{first}\n{second}"


def test_when_no_line_is_long_enough_the_whole_text_including_crumbs_is_kept(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><p>Pradzia</p><p>Trumpa</p></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == "Pradzia\nTrumpa"


@pytest.mark.parametrize("tag", ["script", "style", "nav", "header", "footer"])
def test_every_decomposed_tag_is_stripped_from_the_article_body(app, site, stored, tag):
    html = _page(
        f"<div class=\"item-page\"><div class=\"article-content\">"
        f"<{tag}>SLAPTA</{tag}><p>{DEFAULT_CONTENT}</p></div></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    row = stored(_article_url("pirma"))
    assert "SLAPTA" not in row["content"]
    assert DEFAULT_CONTENT in row["content"]


@pytest.mark.parametrize("tag", ["script", "style", "nav", "header", "footer"])
def test_the_broad_fallback_decomposes_the_same_five_tags(app, site, stored, tag):
    html = _page(
        f"<div class=\"item-page\"><{tag}>SLAPTA</{tag}>"
        f"<p>{DEFAULT_CONTENT}</p></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert "SLAPTA" not in stored(_article_url("pirma"))["content"]


def test_an_image_inside_a_decomposed_tag_is_no_longer_an_image_candidate(app, site, stored):
    # decompose() mutates the SHARED soup: the <img> lookup
    # that runs later cannot see what the content step removed
    html = _page(
        "<div class=\"item-page\"><div class=\"article-content\">"
        "<nav><img src=\"/images/naujiena.jpg\"></nav>"
        f"<p>{DEFAULT_CONTENT}</p></div></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] is None


def test_a_content_of_exactly_ten_thousand_characters_is_stored_whole(app, site, stored):
    exact = "c" * 10000
    _one(site, _article(content=exact))

    assert len(stored(_article_url("pirma"))["content"]) == 10000


def test_a_content_one_character_over_the_limit_is_cut(app, site, stored):
    _one(site, _article(content="c" * 10001))

    row = stored(_article_url("pirma"))
    assert len(row["content"]) == 10000
    assert row["content"] == "c" * 10000


def test_the_separator_between_block_elements_is_a_newline(app, site, stored):
    html = _page(
        "<div class=\"item-page\"><div class=\"article-content\">"
        "<p>Pirma pastraipa</p><p>Antra pastraipa</p></div></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["content"] == "Pirma pastraipa\nAntra pastraipa"




# ###########################################################
# _fetch_article — summary
# ###########################################################


def test_a_content_of_exactly_two_hundred_characters_is_its_own_summary(app, site, stored):
    exact = "s" * 200
    _one(site, _article(content=exact))

    row = stored(_article_url("pirma"))
    assert row["summary"] == exact
    assert not row["summary"].endswith("...")


def test_a_content_one_character_over_two_hundred_is_cut_and_ellipsised(app, site, stored):
    words = ("zodis " * 60).strip()
    assert len(words) > 201
    _one(site, _article(content=words))

    summary = stored(_article_url("pirma"))["summary"]
    assert summary.endswith("...")
    assert summary[:-3] == words[:200].rsplit(" ", 1)[0]


def test_a_two_hundred_and_one_character_run_with_no_space_keeps_the_whole_cut(app, site, stored):
    # rsplit finds nothing to cut back to, so the summary is
    # the full 200-character slice plus the ellipsis — 203
    unbroken = "u" * 201
    _one(site, _article(content=unbroken))

    summary = stored(_article_url("pirma"))["summary"]
    assert summary == "u" * 200 + "..."
    assert len(summary) == 203


def test_a_space_landing_exactly_on_the_cut_drops_the_trailing_word_boundary(app, site, stored):
    content = "w" * 199 + " " + "x" * 50
    _one(site, _article(content=content))

    assert stored(_article_url("pirma"))["summary"] == "w" * 199 + "..."


def test_an_empty_content_gives_an_empty_summary_and_the_row_is_still_written(app, site, stored):
    # Title comes from the listing link, so "parsed to
    # nothing" never actually costs the article its row
    html = _page("<div class=\"article-content\"></div>")
    result = _one(site, html, link_text="Vien antraste")

    row = stored(_article_url("pirma"))
    assert result["new"] == 1
    assert row["title"] == "Vien antraste"
    assert row["content"] == ""
    assert row["summary"] == ""


def test_the_summary_never_reaches_its_own_column_limit(app, site, stored):
    # MAX_SUMMARY_LENGTH is 500 but the cut above it is 200
    # (+3 for the ellipsis), so the second limit is a backstop
    # that the current arithmetic can never make bite
    _one(site, _article(content="y" * 5000))

    assert len(stored(_article_url("pirma"))["summary"]) == 203




# ###########################################################
# _fetch_article — image
# ###########################################################


def test_the_og_image_beats_the_body_image(app, site, stored):
    html = _article(
        head_extra="<meta property=\"og:image\" content=\"https://knf.vu.lt/images/og.jpg\">",
        inner_extra="<img src=\"/images/kunas.jpg\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/og.jpg"


def test_a_protocol_relative_og_image_becomes_absolute(app, site, stored):
    html = _article(head_extra="<meta property=\"og:image\" content=\"//newshub.vu.lt/x.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://newshub.vu.lt/x.jpg"


def test_a_root_relative_og_image_is_resolved_against_the_page_host(app, site, stored):
    html = _article(head_extra="<meta property=\"og:image\" content=\"/images/x.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/x.jpg"


def test_a_bare_relative_og_image_is_resolved_against_the_article_path(app, site, stored):
    html = _article(head_extra="<meta property=\"og:image\" content=\"paveikslai/x.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/aktualijos/paveikslai/x.jpg"


def test_an_image_is_resolved_against_the_post_redirect_url(app, site, stored):
    # fetch hands back resp.url, so a template that moved the
    # article resolves its relative images at the NEW path
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): (301, {"Location": _article_url("naujoje/vietoje")}, ""),
        _article_url("naujoje/vietoje"): _article(
            head_extra="<meta property=\"og:image\" content=\"x.jpg\">"),
    })
    scrape_knf_news(notify=False)

    row = stored(_article_url("pirma"))
    assert row["image_url"] == "https://knf.vu.lt/aktualijos/naujoje/x.jpg"


def test_an_og_image_meta_with_no_content_attribute_falls_to_the_body_image(app, site, stored):
    html = _article(
        head_extra="<meta property=\"og:image\">",
        inner_extra="<img src=\"/images/kunas.jpg\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/kunas.jpg"


def test_a_whitespace_only_og_image_falls_to_the_body_image(app, site, stored):
    # A blank-but-present src resolves to the ARTICLE PAGE
    # under a naive urljoin, which would store an HTML
    # document as image_url
    html = _article(
        head_extra="<meta property=\"og:image\" content=\"   \">",
        inner_extra="<img src=\"/images/kunas.jpg\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/kunas.jpg"


def test_a_data_uri_og_image_is_refused(app, site, stored):
    html = _article(head_extra="<meta property=\"og:image\" content=\"data:image/gif;base64,R0lGOD\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] is None


def test_an_off_allowlist_og_image_is_refused(app, site, stored):
    html = _article(head_extra="<meta property=\"og:image\" content=\"https://tracker.example.com/p.gif\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] is None


def test_an_og_image_on_the_shared_newshub_host_is_kept(app, site, stored):
    # IMAGE_HOSTS is deliberately wider than the page
    # allowlist: both faculty sites serve from newshub
    html = _article(head_extra="<meta property=\"og:image\" content=\"https://newshub.vu.lt/a.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://newshub.vu.lt/a.jpg"


@pytest.mark.parametrize("chrome", ["logo", "icon", "banner", "pixel", "tracking"])
def test_every_chrome_word_is_skipped_before_a_real_image(app, site, stored, chrome):
    html = _article(inner_extra=f"<img src=\"/images/{chrome}-1.png\"><img src=\"/images/tikras.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/tikras.jpg"


def test_the_chrome_filter_is_case_folded(app, site, stored):
    html = _article(inner_extra="<img src=\"/images/LOGO.PNG\"><img src=\"/images/tikras.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/tikras.jpg"


def test_a_blank_src_does_not_end_the_image_search(app, site, stored):
    html = _article(inner_extra="<img src=\" \"><img src=\"/images/tikras.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/tikras.jpg"


def test_an_img_with_no_src_attribute_is_not_a_candidate(app, site, stored):
    html = _article(inner_extra="<img alt=\"be saltinio\"><img src=\"/images/tikras.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/tikras.jpg"


def test_an_off_allowlist_body_image_does_not_end_the_image_search(app, site, stored):
    html = _article(inner_extra="<img src=\"https://evil.example.com/a.jpg\"><img src=\"/images/tikras.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/tikras.jpg"


def test_an_over_length_body_image_does_not_end_the_image_search(app, site, stored):
    html = _article(inner_extra=f"<img src=\"/images/{'a' * 2100}.jpg\"><img src=\"/images/tikras.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/tikras.jpg"


def test_a_page_whose_only_images_are_chrome_stores_no_image(app, site, stored):
    html = _article(inner_extra="<img src=\"/images/icon-a.png\"><img src=\"/images/banner-b.png\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] is None


def test_the_first_og_image_meta_wins_when_the_template_emits_two(app, site, stored):
    html = _article(head_extra=(
        "<meta property=\"og:image\" content=\"https://knf.vu.lt/images/pirmas.jpg\">"
        "<meta property=\"og:image\" content=\"https://knf.vu.lt/images/antras.jpg\">"
    ))
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/pirmas.jpg"


def test_the_first_usable_body_image_wins_over_every_later_one(app, site, stored):
    html = _article(inner_extra="<img src=\"/images/pirmas.jpg\"><img src=\"/images/antras.jpg\">")
    _one(site, html)

    assert stored(_article_url("pirma"))["image_url"] == "https://knf.vu.lt/images/pirmas.jpg"




# ###########################################################
# _fetch_article — date
# ###########################################################


def test_a_time_in_the_article_content_is_the_first_date_source(app, site, stored):
    html = _article(
        inner_extra="<time datetime=\"2026-06-13T09:00:00+03:00\">2026 06 13</time>",
        head_extra="<meta property=\"article:published_time\" content=\"2020-01-01T00:00:00+00:00\">",
    )
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-13T06:00:00"


def test_a_container_that_holds_no_time_falls_through_to_the_next_container(app, site, stored):
    html = _page(
        "<div class=\"item-page\">"
        f"<div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div>"
        "<div class=\"article-body\"><time datetime=\"2026-06-13T09:00:00+00:00\"></time></div>"
        "</div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-13T09:00:00"


def test_a_time_in_the_item_content_container_is_read(app, site, stored):
    html = _article(content_class="item-content",
                    inner_extra="<time datetime=\"2026-06-12T08:00:00+00:00\"></time>")
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-12T08:00:00"


def test_a_time_in_the_bare_article_element_is_the_fourth_container(app, site, stored):
    html = _page(
        "<article><time datetime=\"2026-06-11T07:00:00+00:00\"></time>"
        f"<p>{DEFAULT_CONTENT}</p></article>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-11T07:00:00"


def test_a_broken_first_time_falls_through_to_the_second_one_beside_it(app, site, stored):
    html = _article(
        inner_extra="<time datetime=\"nesamone\">taip pat nesamone</time>"
                    "<time datetime=\"2026-06-10T06:00:00+00:00\"></time>",
    )
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-10T06:00:00"


def test_an_empty_datetime_attribute_falls_back_to_the_time_element_text(app, site, stored):
    html = _article(inner_extra="<time datetime=\"\">2026-06-09</time>")
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-09T00:00:00"


def test_a_time_outside_every_container_is_the_second_date_source(app, site, stored):
    # No container matches at all, so the page-wide sweep is
    # the only thing that can find the sidebar stamp
    html = _page(
        "<aside><time datetime=\"2026-06-08T05:00:00+00:00\"></time></aside>"
        "<div id=\"content\"><div class=\"content\">"
        f"<p>{DEFAULT_CONTENT}</p></div></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-08T05:00:00"


def test_an_unparsable_time_falls_all_the_way_through_to_the_published_meta(app, site, stored):
    html = _article(
        inner_extra="<time datetime=\"vakar\">vakar</time>",
        head_extra="<meta property=\"article:published_time\" content=\"2026-06-07T04:00:00+00:00\">",
    )
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-07T04:00:00"


def test_a_broken_published_meta_falls_through_to_a_second_one(app, site, stored):
    html = _article(head_extra=(
        "<meta property=\"article:published_time\" content=\"nesamone\">"
        "<meta property=\"article:published_time\" content=\"2026-06-06T03:00:00+00:00\">"
    ))
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-06T03:00:00"


def test_a_published_meta_with_no_content_attribute_is_skipped(app, site, stored):
    html = _article(head_extra=(
        "<meta property=\"article:published_time\">"
        "<meta property=\"og:updated_time\" content=\"2026-06-05T02:00:00+00:00\">"
    ))
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-05T02:00:00"


def test_the_updated_meta_is_the_fourth_and_last_date_source(app, site, stored):
    html = _article(head_extra=(
        "<meta property=\"article:published_time\" content=\"visai ne data\">"
        "<meta property=\"og:updated_time\" content=\"2026-06-04T01:00:00+00:00\">"
    ))
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-04T01:00:00"


def test_the_published_meta_beats_the_updated_meta(app, site, stored):
    html = _article(head_extra=(
        "<meta property=\"og:updated_time\" content=\"2026-06-04T01:00:00+00:00\">"
        "<meta property=\"article:published_time\" content=\"2026-06-03T00:00:00+00:00\">"
    ))
    with time_machine.travel(FROZEN, tick=False):
        _one(site, html)

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-03T00:00:00"


def test_a_page_with_no_date_source_at_all_is_stamped_now(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article())

    assert stored(_article_url("pirma"))["published_at"] == FROZEN_ISO


@pytest.mark.parametrize("stamp,expected", [
    ("2026-06-13T09:00:00+03:00", "2026-06-13T06:00:00"),
    ("2026-06-13T09:00:00-05:00", "2026-06-13T14:00:00"),
    ("2026-06-13T09:00:00Z", "2026-06-13T09:00:00"),
    ("2026-06-13T09:00:00+00:00", "2026-06-13T09:00:00"),
    ("2026-06-13", "2026-06-13T00:00:00"),
    ("2026-06-13 09:00:00", "2026-06-13T09:00:00"),
])
def test_every_source_timestamp_shape_lands_as_naive_utc(app, site, stored, stamp, expected):
    # The offset is APPLIED, never dropped — including a
    # NEGATIVE one, which the old split("+") surgery broke
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article(inner_extra=f"<time datetime=\"{stamp}\"></time>"))

    assert stored(_article_url("pirma"))["published_at"] == expected


def test_a_stamp_exactly_at_now_is_kept(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article(inner_extra=f"<time datetime=\"{FROZEN_ISO}+00:00\"></time>"))

    assert stored(_article_url("pirma"))["published_at"] == FROZEN_ISO


def test_a_stamp_one_second_in_the_future_is_clamped_to_now(app, site, stored):
    ahead = (FROZEN_DT + timedelta(seconds=1)).isoformat()
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article(inner_extra=f"<time datetime=\"{ahead}+00:00\"></time>"))

    # Left alone, a future stamp divides the feed's recency
    # term by ~0 and pins the article to the top forever
    assert stored(_article_url("pirma"))["published_at"] == FROZEN_ISO


def test_a_stamp_exactly_five_years_old_is_kept(app, site, stored):
    edge = (FROZEN_DT - timedelta(days=5 * 365)).isoformat()
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article(inner_extra=f"<time datetime=\"{edge}+00:00\"></time>"))

    assert stored(_article_url("pirma"))["published_at"] == edge


def test_a_stamp_one_second_past_five_years_is_clamped_to_now(app, site, stored):
    past = (FROZEN_DT - timedelta(days=5 * 365, seconds=1)).isoformat()
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article(inner_extra=f"<time datetime=\"{past}+00:00\"></time>"))

    assert stored(_article_url("pirma"))["published_at"] == FROZEN_ISO


def test_the_offset_is_applied_before_the_future_clamp_decides(app, site, stored):
    # 14:00 Vilnius is 11:00 UTC — an hour in the PAST. Drop
    # the offset instead of applying it and this article is
    # clamped for being two hours in the future
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article(inner_extra="<time datetime=\"2026-06-15T14:00:00+03:00\"></time>"))

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-15T11:00:00"


def test_a_stamp_that_is_future_only_after_the_offset_is_applied_is_clamped(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article(inner_extra="<time datetime=\"2026-06-15T13:00:00-01:00\"></time>"))

    assert stored(_article_url("pirma"))["published_at"] == FROZEN_ISO


def test_a_mis_parsed_far_future_year_is_clamped_rather_than_stored(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        _one(site, _article(inner_extra="<time datetime=\"9999-01-01T00:00:00+00:00\"></time>"))

    assert stored(_article_url("pirma"))["published_at"] == FROZEN_ISO




# ###########################################################
# _fetch_article — author
# ###########################################################


def test_the_author_defaults_to_the_faculty_itself(app, site, stored):
    _one(site, _article())

    assert stored(_article_url("pirma"))["author_name"] == "VU Kauno fakultetas"


def test_the_article_author_class_beats_every_later_generation(app, site, stored):
    html = _article(body_extra=(
        "<span class=\"article-author\">Pirmoji karta</span>"
        "<span class=\"author\">Antroji karta</span>"
        "<span class=\"createdby\">Trecioji karta</span>"
    ))
    _one(site, html)

    assert stored(_article_url("pirma"))["author_name"] == "Pirmoji karta"


def test_the_author_class_is_the_second_generation(app, site, stored):
    html = _article(body_extra=(
        "<span class=\"author\">Antroji karta</span>"
        "<span class=\"createdby\">Trecioji karta</span>"
    ))
    _one(site, html)

    assert stored(_article_url("pirma"))["author_name"] == "Antroji karta"


def test_the_createdby_class_is_the_third_generation(app, site, stored):
    _one(site, _article(body_extra="<span class=\"createdby\">Trecioji karta</span>"))

    assert stored(_article_url("pirma"))["author_name"] == "Trecioji karta"


def test_a_span_author_is_already_matched_by_the_second_generation(app, site, stored):
    # The fourth selector, span.author, is a strict subset of
    # ".author" — nothing can reach it, and this is what
    # proves the <span class="author"> case is still served
    _one(site, _article(body_extra="<span class=\"author\">Ketvirtoji karta</span>"))

    assert stored(_article_url("pirma"))["author_name"] == "Ketvirtoji karta"


def test_an_author_element_anywhere_on_the_page_is_read(app, site, stored):
    # select_one searches the WHOLE document, not the article
    # body — a sidebar byline is taken as the author
    html = _page(
        "<aside><span class=\"author\">Sonine juosta</span></aside>"
        f"<div class=\"item-page\"><div class=\"article-content\"><p>{DEFAULT_CONTENT}</p></div></div>",
        head="<meta property=\"og:title\" content=\"Pirma\">",
    )
    _one(site, html)

    assert stored(_article_url("pirma"))["author_name"] == "Sonine juosta"


def test_the_author_text_is_stripped_of_surrounding_whitespace(app, site, stored):
    _one(site, _article(body_extra="<span class=\"article-author\">  Jonas Jonaitis  </span>"))

    assert stored(_article_url("pirma"))["author_name"] == "Jonas Jonaitis"


def test_an_empty_author_element_leaves_the_faculty_default_in_place(app, site, stored):
    # The TEXT ends the ladder, not the selector match — a bs4
    # tag is truthy whatever it holds
    _one(site, _article(body_extra="<span class=\"article-author\"></span>"))

    assert stored(_article_url("pirma"))["author_name"] == "VU Kauno fakultetas"


def test_a_very_long_author_is_cut_to_the_cap_without_failing_the_run(app, site, stored):
    # author_name is capped like title, content and summary —
    # a source page cannot write an unbounded byline into the
    # row every guest is served, and the row still lands
    result = _one(site, _article(body_extra=f"<span class=\"article-author\">{'A' * 5000}</span>"))

    assert result["new"] == 1
    assert stored(_article_url("pirma"))["author_name"] == "A" * common.MAX_AUTHOR_LENGTH




# ###########################################################
# Storage — the row a guest is served
# ###########################################################


def test_a_scraped_row_carries_the_source_marks_and_no_author_id(app, site, stored):
    _one(site, _article())

    row = stored(_article_url("pirma"))
    assert row["source"] == "knf.vu.lt"
    assert row["post_type"] == "article"
    assert row["author_id"] is None
    assert row["is_public"] == 1


def test_the_source_url_is_stored_in_its_canonical_shape(app, site, stored):
    # www, a trailing slash, a campaign tag and a fragment all
    # collapse to the one key that news_posts.source_url holds
    site.serve({
        _listing_url(): _listing([
            ("https://www.knf.vu.lt/aktualijos/pirma/?utm_source=fb&ref=x#virsus", "Pirma"),
        ]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })
    scrape_knf_news(notify=False)

    assert stored(_article_url("pirma")) is not None


def test_a_non_tracking_query_parameter_stays_part_of_the_key(app, site, stored):
    url = f"{BASE}/aktualijos/pirma?lang=lt"
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma?lang=lt", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        url: _article(),
    })
    scrape_knf_news(notify=False)

    assert stored(url) is not None


def test_the_counts_of_a_plain_run_are_one_found_and_one_new(app, site):
    result = _one(site, _article())

    assert result["found"] == 1
    assert result["new"] == 1
    assert "error" not in result




# ###########################################################
# Storage — dedupe by source_url
# ###########################################################


def test_an_article_already_stored_is_counted_but_never_re_fetched(app, site, plant_post):
    plant_post(source_url=_article_url("pirma"))
    result = _one(site, _article())

    assert result["found"] == 1
    assert result["new"] == 0
    assert site.hits_on(_article_url("pirma")) == 0


def test_a_second_run_over_the_same_listing_writes_nothing_and_fetches_nothing(app, site, stored):
    _one(site, _article())
    first_hits = site.hits_on(_article_url("pirma"))

    second = scrape_knf_news(notify=False)

    assert first_hits == 1
    assert site.hits_on(_article_url("pirma")) == 1
    assert second["found"] == 1
    assert second["new"] == 0
    assert len(stored()) == 1


def test_a_row_planted_between_the_check_and_the_insert_costs_one_row_not_the_run(app, site, stored):
    # The SELECT is fetch avoidance; INSERT OR IGNORE is the
    # backstop. A lost race must cost the row, not the run
    def race(request):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO news_posts
                   (id, title, content, source, source_url, post_type, published_at)
                   VALUES (?, 'Kito bego pavadinimas', 'x', 'knf.vu.lt', ?, 'article',
                           '2026-01-01T00:00:00')""",
                (str(uuid.uuid4()), _article_url("antra")),
            )
            conn.commit()
        finally:
            conn.close()
        return _article(og_title="Antra naujiena")

    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma"), ("/aktualijos/antra", "Antra")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="Pirma naujiena"),
        _article_url("antra"): race,
    })
    result = scrape_knf_news(notify=False)

    assert result["found"] == 2
    assert result["new"] == 1
    assert "error" not in result
    assert stored(_article_url("antra"))["title"] == "Kito bego pavadinimas"




# ###########################################################
# Storage — dedupe by title
# ###########################################################


def test_the_same_story_under_a_second_url_is_skipped_by_title(app, site, stored, plant_post):
    plant_post(source_url=_article_url("sena"), title="Ta pati istorija")
    result = _one(site, _article(og_title="Ta pati istorija"), slug="nauja")

    assert result["found"] == 1
    assert result["new"] == 0
    assert stored(_article_url("nauja")) is None


def test_the_title_dedupe_is_scoped_to_this_source(app, site, stored, plant_post):
    # vu.lt covering the same story must not stop knf.vu.lt's
    # own copy from being stored
    plant_post(source_url="https://vu.lt/naujienos/ta-pati", title="Ta pati istorija", source="vu.lt")
    result = _one(site, _article(og_title="Ta pati istorija"))

    assert result["new"] == 1
    assert stored(_article_url("pirma")) is not None


def test_the_title_dedupe_is_case_sensitive(app, site, stored, plant_post):
    plant_post(source_url=_article_url("sena"), title="Ta pati istorija")
    result = _one(site, _article(og_title="TA PATI ISTORIJA"), slug="nauja")

    assert result["new"] == 1
    assert len(stored()) == 2


def test_titles_differing_only_in_whitespace_are_not_duplicates(app, site, stored, plant_post):
    plant_post(source_url=_article_url("sena"), title="Ta pati istorija")
    result = _one(site, _article(og_title="Ta  pati istorija"), slug="nauja")

    assert result["new"] == 1
    assert len(stored()) == 2


def test_the_dedupe_compares_the_CUT_title_not_the_raw_one(app, site, stored, plant_post):
    # Two titles identical for 200 characters collapse to one
    # stored title, so the second is a duplicate of the first
    plant_post(source_url=_article_url("sena"), title="N" * 200)
    result = _one(site, _article(og_title="N" * 200 + " kitas galas"), slug="nauja")

    assert result["found"] == 1
    assert result["new"] == 0
    assert len(stored()) == 1


def test_two_same_titled_articles_inside_one_run_yield_a_single_row(app, site, stored):
    # The duplicate lookup runs on the same connection as the
    # inserts, so the second candidate of a run sees the first
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma"), ("/aktualijos/antra", "Antra")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="Vienoda antraste"),
        _article_url("antra"): _article(og_title="Vienoda antraste"),
    })
    result = scrape_knf_news(notify=False)

    assert result["found"] == 2
    assert result["new"] == 1
    assert len(stored()) == 1
    assert stored(_article_url("pirma")) is not None
    assert stored(_article_url("antra")) is None


def test_a_duplicate_by_title_is_still_counted_in_found(app, site, plant_post):
    plant_post(source_url=_article_url("sena"), title="Ta pati istorija")
    result = _one(site, _article(og_title="Ta pati istorija"), slug="nauja")

    assert result["found"] == 1


def test_a_duplicate_title_article_is_fetched_because_its_url_is_unknown(app, site, plant_post):
    # The title check can only happen after the parse, so the
    # download is the price of catching a republished story
    plant_post(source_url=_article_url("sena"), title="Ta pati istorija")
    _one(site, _article(og_title="Ta pati istorija"), slug="nauja")

    assert site.hits_on(_article_url("nauja")) == 1


def test_two_genuinely_different_articles_both_land(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma"), ("/aktualijos/antra", "Antra")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="Pirma naujiena"),
        _article_url("antra"): _article(og_title="Antra naujiena"),
    })
    result = scrape_knf_news(notify=False)

    assert result["found"] == 2
    assert result["new"] == 2
    assert [row["title"] for row in stored()] == ["Antra naujiena", "Pirma naujiena"]




# ###########################################################
# Storage — the newest-published log line
# ###########################################################


def test_the_newest_article_of_the_run_is_the_one_logged(app, site, caplog):
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena"), ("/aktualijos/nauja", "Nauja")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("sena"): _article(og_title="Sena",
                                       inner_extra="<time datetime=\"2026-06-01T00:00:00+00:00\"></time>"),
        _article_url("nauja"): _article(og_title="Nauja",
                                        inner_extra="<time datetime=\"2026-06-10T00:00:00+00:00\"></time>"),
    })
    with caplog.at_level(logging.INFO, logger="app.scraper.knf_scraper"):
        with time_machine.travel(FROZEN, tick=False):
            scrape_knf_news(notify=False)

    complete = [r for r in caplog.records if "scrape complete" in r.getMessage()]
    assert complete[-1].args[2] == "2026-06-10T00:00:00"


def test_an_older_article_seen_second_does_not_become_the_newest(app, site, caplog):
    site.serve({
        _listing_url(): _listing([("/aktualijos/nauja", "Nauja"), ("/aktualijos/sena", "Sena")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("nauja"): _article(og_title="Nauja",
                                        inner_extra="<time datetime=\"2026-06-10T00:00:00+00:00\"></time>"),
        _article_url("sena"): _article(og_title="Sena",
                                       inner_extra="<time datetime=\"2026-06-01T00:00:00+00:00\"></time>"),
    })
    with caplog.at_level(logging.INFO, logger="app.scraper.knf_scraper"):
        with time_machine.travel(FROZEN, tick=False):
            scrape_knf_news(notify=False)

    complete = [r for r in caplog.records if "scrape complete" in r.getMessage()]
    assert complete[-1].args[2] == "2026-06-10T00:00:00"




# ###########################################################
# Tombstones
# ###########################################################


def test_a_tombstoned_article_is_counted_but_never_inserted(app, site, stored, tombstone):
    tombstone(_article_url("pirma"))
    result = _one(site, _article())

    assert result["found"] == 1
    assert result["new"] == 0
    assert stored(_article_url("pirma")) is None


def test_a_tombstoned_article_is_never_even_fetched(app, site, tombstone):
    tombstone(_article_url("pirma"))
    _one(site, _article())

    assert site.hits_on(_article_url("pirma")) == 0


@pytest.mark.parametrize("shape", [
    "https://www.knf.vu.lt/aktualijos/pirma",
    "http://knf.vu.lt/aktualijos/pirma",
    "https://knf.vu.lt/aktualijos/pirma/",
    "https://knf.vu.lt/aktualijos/pirma?utm_source=naujienlaiskis",
    "https://knf.vu.lt/aktualijos/pirma#turinys",
    "  https://knf.vu.lt/aktualijos/pirma  ",
])
def test_a_tombstone_written_in_any_url_shape_still_matches(app, site, stored, tombstone, shape):
    # Both sides go through normalise_url, so the shape an
    # admin's client happened to send cannot resurrect an
    # article the admin deleted
    tombstone(shape)
    _one(site, _article())

    assert stored(_article_url("pirma")) is None


def test_a_tombstone_for_another_article_does_not_block_this_one(app, site, stored, tombstone):
    tombstone(_article_url("kita"))
    result = _one(site, _article())

    assert result["new"] == 1
    assert stored(_article_url("pirma")) is not None


def test_a_null_tombstone_row_is_ignored_and_does_not_fail_the_run(app, site, stored, db):
    db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (NULL)")
    db.commit()

    result = _one(site, _article())

    assert "error" not in result
    assert result["new"] == 1


def test_an_empty_string_tombstone_row_is_ignored(app, site, stored, tombstone):
    tombstone("")
    result = _one(site, _article())

    assert result["new"] == 1
    assert stored(_article_url("pirma")) is not None


def test_a_tombstone_on_another_host_never_matches(app, site, stored, tombstone):
    tombstone("https://vu.lt/naujienos/pirma")
    result = _one(site, _article())

    assert result["new"] == 1


def test_tombstones_are_loaded_once_per_run(app, site, stored):
    # The set is read before any network I/O, so a tombstone
    # planted while the run is fetching cannot take effect
    # until the NEXT tick — which is what keeps the whole run
    # reading one consistent view
    def plant_tombstone(request):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)",
                         (_article_url("antra"),))
            conn.commit()
        finally:
            conn.close()
        return _article(og_title="Pirma naujiena")

    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma"), ("/aktualijos/antra", "Antra")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): plant_tombstone,
        _article_url("antra"): _article(og_title="Antra naujiena"),
    })
    result = scrape_knf_news(notify=False)

    assert result["new"] == 2
    assert stored(_article_url("antra")) is not None


def test_an_admin_deleting_a_scraped_article_keeps_it_out_on_the_next_run(app, client, admin, site, stored):
    _one(site, _article())
    post_id = stored(_article_url("pirma"))["id"]

    _user, headers = admin
    assert client.delete(f"/api/news/{post_id}", headers=headers).status_code == 200

    second = scrape_knf_news(notify=False)

    assert second["found"] == 1
    assert second["new"] == 0
    assert stored(_article_url("pirma")) is None


def test_found_counts_the_new_the_stored_and_the_tombstoned_alike(app, site, stored, plant_post, tombstone):
    # "found" is every distinct listing link with a title,
    # whatever the run then decides to do with it; "new" is
    # only the rows INSERT OR IGNORE actually wrote
    plant_post(source_url=_article_url("sena"), title="Sena naujiena")
    tombstone(_article_url("istrinta"))
    site.serve({
        _listing_url(): _listing([
            ("/aktualijos/sena", "Sena"),
            ("/aktualijos/istrinta", "Istrinta"),
            ("/aktualijos/nauja", "Nauja"),
        ]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("nauja"): _article(og_title="Nauja naujiena"),
    })
    result = scrape_knf_news(notify=False)

    assert result["found"] == 3
    assert result["new"] == 1
    assert site.hits_on(_article_url("sena")) == 0
    assert site.hits_on(_article_url("istrinta")) == 0
    assert len(stored()) == 2


def test_a_tombstoned_article_no_longer_blocks_a_differently_titled_one(app, site, stored, tombstone):
    tombstone(_article_url("pirma"))
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma"), ("/aktualijos/antra", "Antra")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="Pirma naujiena"),
        _article_url("antra"): _article(og_title="Antra naujiena"),
    })
    result = scrape_knf_news(notify=False)

    assert result["found"] == 2
    assert result["new"] == 1
    assert stored(_article_url("antra")) is not None
