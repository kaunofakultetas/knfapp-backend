# -----------------------------------------------------------
#  [*] Tests — scraper/knf_scraper.py, knf.vu.lt into the feed
#
#  What this module proves about scrape_knf_news and the two
#  helpers it owns (_listing_links, _fetch_article):
#
#    - the listing walk: all three template generations, the
#      sidebar/menu links it must leave behind, an off-
#      allowlist href that must never become an outbound
#      probe, and the run-wide dedupe on the canonical URL
#    - the article parse, field by field: the og:title site
#      prefix (hyphen AND en-dash), the h1 fallback, the
#      listing link text as the title of last resort, the
#      three content selectors plus the broad fallback that
#      sheds navigation crumbs, the 200-char summary cut back
#      to a word boundary, og:image against the first content
#      <img>, and the four date sources tried in order
#    - published_at: the source offset APPLIED rather than
#      dropped, and a future or ancient stamp clamped to now
#    - dedupe and idempotency: a second run inserts nothing
#      and re-fetches nothing, a story republished under a
#      second URL is skipped by title, and a row a racing run
#      wrote first costs one INSERT OR IGNORE, not the run
#    - the tombstone: an article an admin deleted (through
#      DELETE /api/news/<id>, admin-only) must NOT come back
#    - resilience: a missing page, an empty page, malformed
#      markup, an article whose download fails — and the one
#      shape that MUST fail the run, a listing that downloaded
#      and yielded not one recognisable link
#    - the caps and the budget: MAX_ARTICLE_FETCHES,
#      MAX_LISTING_PAGES, `pages` as a MINIMUM, and a spent
#      wall-clock budget
#    - the bookkeeping: scraper_runs running -> completed /
#      failed, the 30-day prune that keeps each source's
#      newest row, the yield-drop ERROR, and the lock that
#      turns an overlapping trigger into "skipped" (409)
#    - the push: one notification per run with declined
#      Lithuanian copy plus its English variant, silent for a
#      backfill, silent with notify=False, and a push that
#      blows up does not fail the run
#
#  EVERY fetch goes through `responses` and fixture HTML
#  written inline below. The test container runs with
#  --network none, so a test that reached knf.vu.lt would
#  fail by construction — the `site` fixture answers only
#  knf.vu.lt URLs it was given and raises ConnectionError for
#  the rest, which is exactly how "the page is missing" is
#  modelled. Time-dependent behaviour uses time_machine;
#  nothing sleeps.
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

from app.database import get_db as real_get_db
from app.scraper import common, knf_scraper
from app.scraper.knf_scraper import scrape_knf_news


BASE = "https://knf.vu.lt"
NEWS = f"{BASE}/aktualijos"

# Every knf.vu.lt URL lands on the one callback the `site`
# fixture registers; www. is in the pattern because a listing
# may link its own articles that way
KNF_PATTERN = re.compile(r"https://(?:www\.)?knf\.vu\.lt/.*")

# A frozen instant used by the clamp tests — a plain wall
# clock so the expected published_at is readable
FROZEN = "2026-06-15 12:00:00 +0000"
FROZEN_ISO = "2026-06-15T12:00:00"




# -----------------------------------------------------------
# _listing_url / _article_url
# -----------------------------------------------------------
#
# The exact URLs the scraper builds: Joomla pages 5 per page
# through ?start=<offset>, and an article under /aktualijos/.
# The fixture map is keyed by these strings, so a typo shows
# up as "the page is missing" instead of a silent pass.
# -----------------------------------------------------------

def _listing_url(offset=0):
    return NEWS if offset == 0 else f"{NEWS}?start={offset}"


def _article_url(slug):
    return f"{BASE}/aktualijos/{slug}"




# -----------------------------------------------------------
# _page
# -----------------------------------------------------------
#
# One HTML document in the shape the parser meets: a declared
# utf-8 charset (the body is served as BYTES, so the sniffed
# charset is what makes the Lithuanian diacritics survive)
# and the site chrome every real page carries.
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
#   _listing([("/aktualijos/pirma", "Pirma")])
#   _listing([...], template="h4")      — the older Joomla
#   _listing([...], template="class")   — the last resort
#
# One listing page carrying the given (href, link text) pairs
# in one of the three template generations _listing_links
# knows. The sidebar block is always present: its links are
# the ones that must be left behind.
# -----------------------------------------------------------

def _listing(items, template="h2"):
    blocks = []
    for href, text in items:
        if template == "h2":
            blocks.append(f"<div class=\"item\"><h2 class=\"article-title\">"
                          f"<a href=\"{href}\">{text}</a></h2></div>")
        elif template == "h4":
            blocks.append(f"<div class=\"item\"><h4><a href=\"{href}\">{text}</a></h4></div>")
        else:
            blocks.append(f"<div class=\"item\"><div class=\"blog-article-title\">"
                          f"<a href=\"{href}\">{text}</a></div></div>")

    sidebar = ("<aside><h2 class=\"article-title\"><a href=\"/studijos\">Studijos</a></h2>"
               "<h4><a href=\"/apie-mus\">Apie mus</a></h4></aside>")

    return _page(f"<div class=\"blog\">{''.join(blocks)}</div>{sidebar}")


# The end of the listing: it downloads fine and holds no
# article links, which is what stops the walk once the
# caller's minimum number of pages has been read
EMPTY_LISTING = _page("<div class=\"blog\"><p>Daugiau naujienų nėra.</p></div>")




# -----------------------------------------------------------
# _article
# -----------------------------------------------------------
#
# One article page in the current template: an .item-page
# wrapper around an .article-content body. Every knob maps to
# a fallback chain in _fetch_article, so a test names the ONE
# thing it varies:
#
#   og_title            — the first title source
#   content_class       — ".article-content" (current) or
#                         ".item-content" (older)
#   time_attr/time_text — the <time> in the article body
#   published/updated   — the two metas, tried after <time>
#   og_image / images   — the two image sources
#   author/author_class — the four author selectors
#
# The shapes with no recognisable container at all are built
# from _page directly, right in the test that needs them.
# -----------------------------------------------------------

def _article(og_title="VU Kauno fakultetas - Pirma naujiena",
             content="Fakultetas kviečia studentus į rugsėjo pirmosios šventę.",
             content_class="article-content",
             time_attr=None,
             time_text=None,
             og_image=None,
             published=None,
             updated=None,
             images=(),
             author=None,
             author_class="article-author"):
    head = ""
    if og_title:
        head += f"<meta property=\"og:title\" content=\"{og_title}\">"
    if og_image:
        head += f"<meta property=\"og:image\" content=\"{og_image}\">"
    if published:
        head += f"<meta property=\"article:published_time\" content=\"{published}\">"
    if updated:
        head += f"<meta property=\"og:updated_time\" content=\"{updated}\">"

    inner = ""
    if time_attr is not None:
        inner += f"<time datetime=\"{time_attr}\">{time_text or ''}</time>"
    elif time_text is not None:
        inner += f"<time>{time_text}</time>"
    inner += f"<p>{content}</p><script>var reklama = 1;</script>"
    inner += "".join(f"<img src=\"{src}\">" for src in images)

    body = f"<div class=\"{content_class}\">{inner}</div>"
    if author:
        body += f"<span class=\"{author_class}\">{author}</span>"

    return _page(f"<div class=\"item-page\">{body}</div>", head=head)




# -----------------------------------------------------------
# _source_time
# -----------------------------------------------------------
#
#   _source_time(days_ago=2)  -> "2026-08-27T09:00:00+03:00"
#
# A Vilnius wall-clock stamp in the shape the template emits,
# always safely in the past so only the tests that mean to
# exercise the clamp do.
# -----------------------------------------------------------

def _source_time(days_ago=2, offset="+03:00"):
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.replace(hour=9, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S") + offset




# -----------------------------------------------------------
# _Site
# -----------------------------------------------------------
#
# The fake knf.vu.lt. `serve` adds pages to the map, the
# callback answers them and records every request; a URL that
# was never served raises the ConnectionError requests would
# raise for a host that is not answering, so "the page is
# missing" needs no extra machinery.
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
        # A callable page is served for its side effect as well
        # as its body — that is how a run racing this one is
        # staged, right in the middle of an article download
        if callable(page):
            page = page(request)
        if isinstance(page, tuple):
            status, body = page
        else:
            status, body = 200, page
        return status, {}, body.encode("utf-8")

    def hits_on(self, url):
        return self.hits.count(url)




# -----------------------------------------------------------
# site
# -----------------------------------------------------------
#
# Used by:
#   - every test in this module; without it a fetch would
#     leave the container, which --network none forbids
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
# common._LAST_PUSH is a PROCESS-global "this source pushed N
# seconds ago" map, so without this one test's push would
# silence the next one's. Cleared on both sides so the order
# tests run in cannot matter.
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
#   stored()            — every scraped row, oldest title first
#   stored(url)         — the row for one source_url, or None
# -----------------------------------------------------------

@pytest.fixture
def stored(db):

    def _stored(url=None):
        if url is not None:
            return db.execute("SELECT * FROM news_posts WHERE source_url = ?", (url,)).fetchone()
        return db.execute("SELECT * FROM news_posts ORDER BY title").fetchall()

    return _stored




# -----------------------------------------------------------
# runs
# -----------------------------------------------------------
#
#   runs()              — every scraper_runs row, newest first
#   runs(run_id)        — one run row
# -----------------------------------------------------------

@pytest.fixture
def runs(db):

    def _runs(run_id=None):
        if run_id is not None:
            return db.execute("SELECT * FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()
        return db.execute("SELECT * FROM scraper_runs ORDER BY started_at DESC").fetchall()

    return _runs




# -----------------------------------------------------------
# seed_run
# -----------------------------------------------------------
#
# A finished scraper_runs row. push_allowed treats a source
# with no earlier COMPLETED run as a first-boot backfill and
# stays silent, so every push test needs one of these first;
# the prune tests need an ancient one.
# -----------------------------------------------------------

@pytest.fixture
def seed_run(app):

    def _seed(source="knf.vu.lt", status="completed", days_ago=1, found=1, new=1):
        run_id = str(uuid.uuid4())
        started = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO scraper_runs
                   (id, source, status, articles_found, articles_new, started_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, source, status, found, new, started, started),
            )
            conn.commit()
        finally:
            conn.close()
        return run_id

    return _seed




# -----------------------------------------------------------
# seed_article
# -----------------------------------------------------------
#
# A scraped news_posts row written straight to the database —
# what an earlier run of the scraper would have left behind.
# -----------------------------------------------------------

@pytest.fixture
def seed_article(app):

    def _seed(source_url, title="Sena naujiena", source="knf.vu.lt"):
        post_id = str(uuid.uuid4())
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT INTO news_posts
                   (id, title, content, summary, source, source_url, post_type, published_at)
                   VALUES (?, ?, 'Turinys', 'Santrauka', ?, ?, 'article', ?)""",
                (post_id, title, source, source_url, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        return post_id

    return _seed




# -----------------------------------------------------------
# The happy path
# -----------------------------------------------------------


def test_a_listing_page_becomes_news_rows(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma naujiena"),
                                  ("/aktualijos/antra", "Antra naujiena")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="VU Kauno fakultetas - Pirma naujiena",
                                        content="Fakultetas kviečia studentus į šventę.",
                                        time_attr=_source_time(3),
                                        og_image="https://newshub.vu.lt/nuotraukos/pirma.jpg",
                                        author="Komunikacijos skyrius"),
        _article_url("antra"): _article(og_title="VU Kauno fakultetas - Antra naujiena",
                                        content="Antrosios naujienos turinys.",
                                        time_attr=_source_time(1)),
    })

    result = scrape_knf_news()

    assert result["found"] == 2
    assert result["new"] == 2

    row = stored(_article_url("pirma"))
    assert row["title"] == "Pirma naujiena"
    assert "kviečia studentus" in row["content"]
    assert row["summary"] == "Fakultetas kviečia studentus į šventę."
    assert row["image_url"] == "https://newshub.vu.lt/nuotraukos/pirma.jpg"
    assert row["author_name"] == "Komunikacijos skyrius"
    assert row["source"] == "knf.vu.lt"
    assert row["post_type"] == "article"
    assert row["author_id"] is None
    assert row["is_public"] == 1


def test_the_run_row_opens_running_and_closes_completed_with_the_counts(app, site, runs):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })

    result = scrape_knf_news()

    row = runs(result["runId"])
    assert row["source"] == "knf.vu.lt"
    assert row["status"] == "completed"
    assert row["articles_found"] == 1
    assert row["articles_new"] == 1
    assert row["started_at"] and row["finished_at"]
    assert row["error_message"] is None


def test_the_script_tag_inside_the_article_body_is_not_stored(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(content="Tikras tekstas apie fakultetą."),
    })

    scrape_knf_news()

    assert "reklama" not in stored(_article_url("pirma"))["content"]


def test_lithuanian_diacritics_survive_the_byte_body(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="VU Kauno fakultetas - Šventė ąžuolyne"),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["title"] == "Šventė ąžuolyne"




# -----------------------------------------------------------
# _listing_links — the three template generations
# -----------------------------------------------------------


def test_the_older_h4_template_is_still_understood(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena naujiena")], template="h4"),
        _listing_url(5): EMPTY_LISTING,
        _article_url("sena"): _article(),
    })

    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (1, 1)
    assert site.hits_on(_article_url("sena")) == 1


def test_the_article_title_class_is_the_last_resort_generation(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/paskutine", "Paskutinė")], template="class"),
        _listing_url(5): EMPTY_LISTING,
        _article_url("paskutine"): _article(),
    })

    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (1, 1)


def test_sidebar_and_menu_links_are_left_behind(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })

    result = scrape_knf_news()

    # The sidebar every _listing carries points at /studijos and
    # /apie-mus — neither holds "/aktualijos/"
    assert result["found"] == 1
    assert [r["source_url"] for r in stored()] == [_article_url("pirma")]


def test_a_heading_without_an_anchor_is_skipped(app, site):
    listing = _page("<div class=\"blog\">"
                    "<h2 class=\"article-title\">Be nuorodos</h2>"
                    "<h2 class=\"article-title\"><a href=\"/aktualijos/su-nuoroda\">Su nuoroda</a></h2>"
                    "</div>")
    site.serve({
        _listing_url(): listing,
        _listing_url(5): EMPTY_LISTING,
        _article_url("su-nuoroda"): _article(),
    })

    assert scrape_knf_news()["found"] == 1


def test_an_h4_without_an_anchor_does_not_stop_the_older_template(app, site):
    listing = _page("<div class=\"blog\"><h4>Skyrius</h4>"
                    "<h4><a href=\"/aktualijos/sena\">Sena</a></h4></div>")
    site.serve({
        _listing_url(): listing,
        _listing_url(5): EMPTY_LISTING,
        _article_url("sena"): _article(),
    })

    assert scrape_knf_news()["found"] == 1


def test_an_article_title_class_anchor_without_an_href_is_ignored(app, site):
    listing = _page("<div class=\"blog\">"
                    "<div class=\"blog-article-title\"><a>Be href</a></div>"
                    "<div class=\"blog-article-title\"><a href=\"/kontaktai\">Kontaktai</a></div>"
                    "<div class=\"blog-article-title\"><a href=\"/aktualijos/gera\">Gera</a></div>"
                    "</div>")
    site.serve({
        _listing_url(): listing,
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article(),
    })

    assert scrape_knf_news()["found"] == 1




# -----------------------------------------------------------
# The listing walk — dedupe, allowlist, empty text
# -----------------------------------------------------------


def test_the_same_article_linked_four_ways_is_fetched_once(app, site, stored):
    site.serve({
        _listing_url(): _listing([
            ("/aktualijos/viena", "Viena"),
            ("/aktualijos/viena/", "Viena su brūkšniu"),
            ("/aktualijos/viena?utm_source=facebook", "Viena su žyme"),
            ("https://www.knf.vu.lt/aktualijos/viena#turinys", "Viena su www"),
        ]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("viena"): _article(),
    })

    result = scrape_knf_news()

    assert result["found"] == 1
    assert site.hits_on(_article_url("viena")) == 1
    assert len(stored()) == 1
    assert stored()[0]["source_url"] == _article_url("viena")


def test_an_off_allowlist_article_link_is_never_fetched(app, site, stored):
    site.serve({
        _listing_url(): _listing([
            ("https://evil.example.com/aktualijos/ssrf", "Piktybinė"),
            ("http://169.254.169.254/aktualijos/metadata", "Metaduomenys"),
            ("/aktualijos/gera", "Gera"),
        ]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article(),
    })

    result = scrape_knf_news()

    assert result["found"] == 1
    assert [r["source_url"] for r in stored()] == [_article_url("gera")]
    assert not any("evil.example.com" in hit or "169.254" in hit for hit in site.hits)


def test_a_link_with_no_text_is_not_counted_as_found(app, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/tuscia", ""),
                                  ("/aktualijos/gera", "Gera")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("gera"): _article(),
    })

    result = scrape_knf_news()

    assert result["found"] == 1
    assert site.hits_on(_article_url("tuscia")) == 0




# -----------------------------------------------------------
# _fetch_article — title
# -----------------------------------------------------------


def test_the_site_prefix_is_stripped_from_the_og_title(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Nuorodos tekstas")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="VU Kauno fakultetas - Tikras pavadinimas"),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["title"] == "Tikras pavadinimas"


def test_the_en_dash_form_of_the_site_prefix_is_stripped_too(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Nuorodos tekstas")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="VU Kauno fakultetas – Kitas pavadinimas"),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["title"] == "Kitas pavadinimas"


def test_a_title_without_the_prefix_is_kept_whole(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Nuorodos tekstas")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="Konferencija Kaune"),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["title"] == "Konferencija Kaune"


def test_without_an_og_title_the_first_real_h1_wins(app, site, stored):
    page = _page("<div class=\"item-page\"><h1>Aktualijos</h1><h1></h1>"
                 "<h1>Studentų šventė</h1>"
                 "<div class=\"article-content\"><p>Turinys.</p></div></div>")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Nuorodos tekstas")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): page,
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["title"] == "Studentų šventė"


def test_an_empty_og_title_content_falls_through_to_the_h1(app, site, stored):
    page = _page("<div class=\"item-page\"><h1>Antraštė iš h1</h1>"
                 "<div class=\"article-content\"><p>Turinys.</p></div></div>",
                 head="<meta property=\"og:title\" content=\"\">")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Nuorodos tekstas")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): page,
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["title"] == "Antraštė iš h1"


def test_an_unparsable_article_page_is_stored_under_the_listing_link_text(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/keista", "Tekstas iš sąrašo")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("keista"): _page("<div class=\"kazkas\"><p>Nieko atpažįstamo.</p></div>"),
    })

    scrape_knf_news()

    row = stored(_article_url("keista"))
    assert row["title"] == "Tekstas iš sąrašo"


def test_a_very_long_title_is_cut_to_the_column_limit(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/ilga", "Ilga")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("ilga"): _article(og_title="A" * 260),
    })

    scrape_knf_news()

    assert len(stored(_article_url("ilga"))["title"]) == common.MAX_TITLE_LENGTH




# -----------------------------------------------------------
# _fetch_article — content and summary
# -----------------------------------------------------------


def test_the_older_item_content_selector_is_still_read(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(content="Senojo šablono turinys.",
                                        content_class="item-content"),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["content"] == "Senojo šablono turinys."


def test_the_item_page_article_body_selector_is_still_read(app, site, stored):
    page = _page("<div class=\"item-page\"><div class=\"article-body\">"
                 "<p>Vidurinio šablono turinys.</p></div></div>",
                 head="<meta property=\"og:title\" content=\"Vidurinis\">")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): page,
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["content"] == "Vidurinio šablono turinys."


def test_the_broad_fallback_drops_the_leading_navigation_crumbs(app, site, stored):
    page = _page("<div class=\"item-page\"><p>Pradžia</p><p>Aktualijos</p>"
                 "<nav><a href=\"/studijos\">Meniu punktas kuris yra pakankamai ilgas</a></nav>"
                 "<p>Ilgas sakinys apie fakulteto naujienas, kuris tikrai ilgesnis.</p>"
                 "<script>var sekiklis = 2;</script><footer>Poraštė</footer></div>",
                 head="<meta property=\"og:title\" content=\"Platus\">")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): page,
    })

    scrape_knf_news()

    content = stored(_article_url("pirma"))["content"]
    assert content == "Ilgas sakinys apie fakulteto naujienas, kuris tikrai ilgesnis."
    # The nav is stripped BEFORE the crumb skip, so its long
    # line cannot become the first line the article starts at
    assert "Pradžia" not in content
    assert "Meniu punktas" not in content
    assert "sekiklis" not in content
    assert "Poraštė" not in content


def test_the_broad_fallback_keeps_everything_when_no_line_is_long_enough(app, site, stored):
    page = _page("<div class=\"item-page\"><p>Trumpa</p><p>Dar trumpesnė</p></div>",
                 head="<meta property=\"og:title\" content=\"Trumpas\">")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): page,
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["content"] == "Trumpa\nDar trumpesnė"


def test_a_long_content_is_cut_to_the_column_limit(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/ilga", "Ilga")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("ilga"): _article(content="Kaunas " * 2000),
    })

    scrape_knf_news()

    assert len(stored(_article_url("ilga"))["content"]) == common.MAX_CONTENT_LENGTH


def test_the_summary_is_cut_back_to_a_word_boundary(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/ilga", "Ilga")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("ilga"): _article(content="Kaunas " * 40),
    })

    scrape_knf_news()

    summary = stored(_article_url("ilga"))["summary"]
    assert summary.endswith("Kaunas...")
    assert summary.count("Kaunas") == 28
    assert len(summary) == 198


def test_a_short_content_is_its_own_summary(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/trumpa", "Trumpa")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("trumpa"): _article(content="Trumpa žinutė."),
    })

    scrape_knf_news()

    row = stored(_article_url("trumpa"))
    assert row["summary"] == row["content"] == "Trumpa žinutė."




# -----------------------------------------------------------
# _fetch_article — image
# -----------------------------------------------------------


def test_the_og_image_is_stored(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_image="https://newshub.vu.lt/nuotraukos/a.jpg"),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["image_url"] == "https://newshub.vu.lt/nuotraukos/a.jpg"


def test_an_off_allowlist_og_image_is_dropped_and_the_body_image_wins(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_image="https://tracker.example.com/pixel.jpg",
                                        images=["/nuotraukos/tikra.jpg"]),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["image_url"] == f"{BASE}/nuotraukos/tikra.jpg"


def test_chrome_images_are_skipped_before_the_first_real_one(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        # The page chrome _page always renders carries
        # /images/logo.png, and these add the rest of the list
        _article_url("pirma"): _article(images=["/images/icon-share.png",
                                                "/images/banner-top.jpg",
                                                "/images/tracking-pixel.gif",
                                                "https://tracker.example.com/a.jpg",
                                                "nuotraukos/straipsnis.jpg"]),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["image_url"] == f"{BASE}/aktualijos/nuotraukos/straipsnis.jpg"


def test_a_page_with_no_usable_image_stores_none(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(images=["https://tracker.example.com/a.jpg"]),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["image_url"] is None




# -----------------------------------------------------------
# _fetch_article — the four date sources, in order
# -----------------------------------------------------------


def test_the_time_inside_the_article_body_beats_the_page_time(app, site, stored):
    body_time = _source_time(2)
    page = _page("<div class=\"item-page\">"
                 f"<aside><time datetime=\"{_source_time(200)}\">Sena</time></aside>"
                 f"<div class=\"article-content\"><time datetime=\"{body_time}\">Data</time>"
                 "<p>Turinys.</p></div></div>",
                 head="<meta property=\"og:title\" content=\"Data\">")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): page,
    })

    scrape_knf_news()

    expected = datetime.fromisoformat(body_time).astimezone(timezone.utc).replace(tzinfo=None)
    assert stored(_article_url("pirma"))["published_at"] == expected.isoformat()


def test_the_source_offset_is_applied_and_not_dropped(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        site.serve({
            _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
            _listing_url(5): EMPTY_LISTING,
            # 14:00 Vilnius is 11:00 UTC; dropping the offset
            # would put the article an hour in the FUTURE and the
            # clamp would then stamp it "now"
            _article_url("pirma"): _article(time_attr="2026-06-15T14:00:00+03:00"),
        })

        scrape_knf_news()

    assert stored(_article_url("pirma"))["published_at"] == "2026-06-15T11:00:00"


def test_an_unparsable_time_attribute_falls_through_to_the_meta(app, site, stored):
    published = _source_time(4)
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(time_attr="vakar", published=published),
    })

    scrape_knf_news()

    expected = datetime.fromisoformat(published).astimezone(timezone.utc).replace(tzinfo=None)
    assert stored(_article_url("pirma"))["published_at"] == expected.isoformat()


def test_a_time_element_without_a_datetime_attribute_uses_its_text(app, site, stored):
    day = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(time_text=day),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["published_at"] == f"{day}T00:00:00"


def test_a_time_outside_the_article_body_is_the_second_source(app, site, stored):
    day = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    page = _page(f"<div class=\"kazkas\"><time datetime=\"{day}\">Data</time>"
                 "<p>Turinys be atpažįstamo konteinerio.</p></div>",
                 head="<meta property=\"og:title\" content=\"Antra data\">")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): page,
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["published_at"] == f"{day}T00:00:00"


def test_the_published_time_meta_is_the_third_source(app, site, stored):
    published = _source_time(7, offset="+00:00")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(published=published),
    })

    scrape_knf_news()

    expected = datetime.fromisoformat(published).astimezone(timezone.utc).replace(tzinfo=None)
    assert stored(_article_url("pirma"))["published_at"] == expected.isoformat()


def test_the_updated_time_meta_is_the_fourth_source(app, site, stored):
    updated = _source_time(8, offset="+00:00")
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(published="ne data", updated=updated),
    })

    scrape_knf_news()

    expected = datetime.fromisoformat(updated).astimezone(timezone.utc).replace(tzinfo=None)
    assert stored(_article_url("pirma"))["published_at"] == expected.isoformat()


def test_a_page_with_no_date_at_all_is_stamped_now(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        site.serve({
            _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
            _listing_url(5): EMPTY_LISTING,
            _article_url("pirma"): _article(),
        })

        scrape_knf_news()

    assert stored(_article_url("pirma"))["published_at"] == FROZEN_ISO


def test_a_future_published_at_is_clamped_to_now(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        site.serve({
            _listing_url(): _listing([("/aktualijos/ateitis", "Ateitis")]),
            _listing_url(5): EMPTY_LISTING,
            _article_url("ateitis"): _article(time_attr="2030-01-01T00:00:00+03:00"),
        })

        scrape_knf_news()

    # Left alone, a future stamp divides the feed's recency term
    # by ~0 and pins the article to the top of the feed forever
    assert stored(_article_url("ateitis"))["published_at"] == FROZEN_ISO


def test_a_published_at_older_than_five_years_is_clamped_to_now(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        site.serve({
            _listing_url(): _listing([("/aktualijos/senove", "Senovė")]),
            _listing_url(5): EMPTY_LISTING,
            _article_url("senove"): _article(time_attr="2015-01-01T00:00:00+02:00"),
        })

        scrape_knf_news()

    assert stored(_article_url("senove"))["published_at"] == FROZEN_ISO


def test_a_date_five_years_minus_a_day_old_is_kept(app, site, stored):
    with time_machine.travel(FROZEN, tick=False):
        inside = (datetime(2026, 6, 15, 12, 0, 0) - timedelta(days=5 * 365 - 1)).isoformat()
        site.serve({
            _listing_url(): _listing([("/aktualijos/riba", "Riba")]),
            _listing_url(5): EMPTY_LISTING,
            _article_url("riba"): _article(time_attr=inside),
        })

        scrape_knf_news()

    assert stored(_article_url("riba"))["published_at"] == inside




# -----------------------------------------------------------
# _fetch_article — author
# -----------------------------------------------------------


def test_the_author_defaults_to_the_faculty(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["author_name"] == "VU Kauno fakultetas"


@pytest.mark.parametrize("css_class", ["article-author", "author", "createdby"])
def test_every_author_selector_generation_is_read(app, site, stored, css_class):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(author="Dekanatas", author_class=css_class),
    })

    scrape_knf_news()

    assert stored(_article_url("pirma"))["author_name"] == "Dekanatas"




# -----------------------------------------------------------
# Dedupe and idempotency
# -----------------------------------------------------------


def test_running_the_scrape_twice_inserts_nothing_the_second_time(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma"),
                                  ("/aktualijos/antra", "Antra")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="Pirma"),
        _article_url("antra"): _article(og_title="Antra"),
    })

    first = scrape_knf_news()
    fetches_after_first = site.hits_on(_article_url("pirma"))
    second = scrape_knf_news()

    assert (first["found"], first["new"]) == (2, 2)
    assert (second["found"], second["new"]) == (2, 0)
    assert len(stored()) == 2
    # The stored row is the fetch-avoidance path, not just an
    # insert guard: the article page is never downloaded again
    assert site.hits_on(_article_url("pirma")) == fetches_after_first == 1


def test_an_already_stored_article_is_counted_but_never_re_fetched(app, site, seed_article):
    seed_article(_article_url("sena"), title="Sena naujiena")
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena naujiena")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("sena"): _article(),
    })

    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (1, 0)
    assert site.hits_on(_article_url("sena")) == 0


def test_the_same_story_under_a_second_url_is_skipped_by_title(app, site, stored, seed_article):
    seed_article(_article_url("pirmas-adresas"), title="Ta pati istorija")
    site.serve({
        _listing_url(): _listing([("/aktualijos/antras-adresas", "Nuoroda")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("antras-adresas"): _article(og_title="Ta pati istorija"),
    })

    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (1, 0)
    assert len(stored()) == 1


def test_a_row_a_racing_run_wrote_first_costs_one_row_not_the_run(app, site, stored):
    # The article download is where the race is staged: another
    # run inserts the very source_url this one is about to,
    # between the SELECT that found nothing and the INSERT
    def _race(_request):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                """INSERT OR IGNORE INTO news_posts
                   (id, title, content, source, source_url, post_type, published_at)
                   VALUES (?, 'Kito bėgimo eilutė', 'Turinys', 'knf.vu.lt', ?, 'article', ?)""",
                (str(uuid.uuid4()), _article_url("lenktynes"),
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
        return _article(og_title="Mano pavadinimas")

    site.serve({
        _listing_url(): _listing([("/aktualijos/lenktynes", "Lenktynės")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("lenktynes"): _race,
    })

    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (1, 0)
    rows = stored()
    assert len(rows) == 1
    assert rows[0]["title"] == "Kito bėgimo eilutė"




# -----------------------------------------------------------
# Tombstones — a deleted article must not come back
# -----------------------------------------------------------


def test_a_tombstoned_url_is_counted_but_never_re_inserted(app, site, db, stored):
    db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)",
               (_article_url("istrinta"),))
    db.commit()

    site.serve({
        _listing_url(): _listing([("/aktualijos/istrinta", "Ištrinta")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("istrinta"): _article(),
    })

    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (1, 0)
    assert stored() == []
    assert site.hits_on(_article_url("istrinta")) == 0


def test_a_tombstone_stored_in_another_url_shape_still_matches(app, site, db, stored):
    # The tombstone table can hold whatever shape an older row
    # carried; load_deleted_urls normalises both sides
    db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)",
               ("http://www.knf.vu.lt/aktualijos/istrinta/?utm_source=fb",))
    db.commit()

    site.serve({
        _listing_url(): _listing([("/aktualijos/istrinta", "Ištrinta")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("istrinta"): _article(),
    })

    assert scrape_knf_news()["new"] == 0
    assert stored() == []


def test_an_article_an_admin_deleted_does_not_come_back(app, client, admin, site, stored):
    _user, headers = admin
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })

    assert scrape_knf_news()["new"] == 1
    post_id = stored()[0]["id"]

    deleted = client.delete(f"/api/news/{post_id}", headers=headers)
    assert deleted.status_code == 200
    assert stored() == []

    again = scrape_knf_news()

    assert (again["found"], again["new"]) == (1, 0)
    assert stored() == []


def test_only_an_admin_can_tombstone_a_scraped_article(app, client, actor, site, stored):
    _user, headers = actor
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })
    scrape_knf_news()
    post_id = stored()[0]["id"]

    # A scraped row has no author, so a student is neither its
    # author nor an admin
    refused = client.delete(f"/api/news/{post_id}", headers=headers)

    assert refused.status_code == 403
    assert len(stored()) == 1




# -----------------------------------------------------------
# Missing, empty and malformed pages
# -----------------------------------------------------------


def test_a_listing_page_that_fails_to_download_is_skipped_not_fatal(app, site, stored):
    # Page one is never served; page two carries the articles
    site.serve({
        _listing_url(5): _listing([("/aktualijos/antra", "Antra")]),
        _listing_url(10): EMPTY_LISTING,
        _article_url("antra"): _article(),
    })

    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (1, 1)
    assert len(stored()) == 1


def test_a_listing_page_answering_404_is_skipped(app, site):
    site.serve({
        _listing_url(): (404, "<html><body>Nerasta</body></html>"),
        _listing_url(5): _listing([("/aktualijos/antra", "Antra")]),
        _listing_url(10): EMPTY_LISTING,
        _article_url("antra"): _article(),
    })

    assert scrape_knf_news()["new"] == 1


def test_a_site_that_is_entirely_down_completes_with_zero(app, site, runs):
    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (0, 0)
    assert runs(result["runId"])["status"] == "completed"


def test_a_listing_that_downloaded_and_yielded_nothing_fails_the_run(app, site, runs):
    site.serve({_listing_url(): EMPTY_LISTING})

    result = scrape_knf_news(pages=1)

    assert result["new"] == 0
    assert "template" in result["error"]
    row = runs(result["runId"])
    assert row["status"] == "failed"
    assert "template" in row["error_message"]


def test_a_listing_whose_links_all_point_elsewhere_fails_the_run(app, site, runs):
    site.serve({_listing_url(): _listing([("/kontaktai", "Kontaktai"),
                                          ("/studijos/bakalauras", "Bakalauras")])})

    result = scrape_knf_news(pages=1)

    assert result["error"]
    assert runs(result["runId"])["status"] == "failed"


def test_an_empty_response_body_fails_the_run_rather_than_completing_at_zero(app, site, runs):
    site.serve({_listing_url(): ""})

    result = scrape_knf_news(pages=1)

    assert result["error"]
    assert runs(result["runId"])["status"] == "failed"


def test_malformed_listing_markup_still_yields_its_links(app, site, stored):
    # Unclosed tags, a stray comment and a broken attribute —
    # lxml recovers, and the anchors are still found
    broken = ("<!doctype html><html><head><meta charset=\"utf-8\"><body><div class=blog>"
              "<h2 class=\"article-title\"><a href=\"/aktualijos/laužyta\">Laužyta"
              "<!-- nebaigtas komentaras --><p><h2 class=\"article-title\">"
              "<a href=\"/aktualijos/kita\">Kita</a>")
    site.serve({
        _listing_url(): broken,
        _listing_url(5): EMPTY_LISTING,
        f"{BASE}/aktualijos/lau%C5%BEyta": _article(og_title="Laužyta"),
        _article_url("kita"): _article(og_title="Kita"),
    })

    result = scrape_knf_news()

    assert result["found"] == 2
    assert {r["title"] for r in stored()} == {"Laužyta", "Kita"}


def test_an_article_page_that_fails_to_download_is_retried_next_run(app, site, stored):
    site.serve({
        _listing_url(): _listing([("/aktualijos/veliau", "Vėliau")]),
        _listing_url(5): EMPTY_LISTING,
    })

    first = scrape_knf_news()

    assert (first["found"], first["new"]) == (1, 0)
    assert stored() == []

    site.serve({_article_url("veliau"): _article(og_title="Vėliau")})
    second = scrape_knf_news()

    assert (second["found"], second["new"]) == (1, 1)
    assert len(stored()) == 1




# -----------------------------------------------------------
# Paging, caps and the wall-clock budget
# -----------------------------------------------------------


def test_pages_is_the_minimum_number_of_listing_pages_walked(app, site, seed_article):
    seed_article(_article_url("sena"), title="Sena")
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena")]),
        _listing_url(5): EMPTY_LISTING,
    })

    scrape_knf_news(pages=1)

    # Page one held nothing unseen and pages=1 was satisfied, so
    # page two is never even requested
    assert site.hits == [_listing_url()]


def test_the_default_minimum_walks_a_second_listing_page(app, site, seed_article):
    seed_article(_article_url("sena"), title="Sena")
    site.serve({
        _listing_url(): _listing([("/aktualijos/sena", "Sena")]),
        _listing_url(5): EMPTY_LISTING,
    })

    scrape_knf_news(pages=2)

    assert site.hits == [_listing_url(), _listing_url(5)]


def test_paging_continues_past_the_minimum_while_pages_keep_yielding(app, site, seed_article):
    seed_article(_article_url("sena"), title="Sena")
    site.serve({
        _listing_url(): _listing([("/aktualijos/a", "A")]),
        _listing_url(5): _listing([("/aktualijos/b", "B")]),
        _listing_url(10): _listing([("/aktualijos/sena", "Sena")]),
        _listing_url(15): EMPTY_LISTING,
        _article_url("a"): _article(og_title="A"),
        _article_url("b"): _article(og_title="B"),
    })

    result = scrape_knf_news(pages=1)

    # A publishing burst pushed the unseen articles onto pages
    # one AND two; page three held nothing new, so the walk ends
    assert (result["found"], result["new"]) == (3, 2)
    assert site.hits_on(_listing_url(10)) == 1
    assert site.hits_on(_listing_url(15)) == 0


def test_one_run_never_fetches_more_than_the_article_cap(app, site, stored):
    pages = {}
    for page in range(4):
        items = [(f"/aktualijos/a{page}-{i}", f"Straipsnis {page}-{i}") for i in range(6)]
        pages[_listing_url(page * 5)] = _listing(items)
        for href, title in items:
            pages[f"{BASE}{href}"] = _article(og_title=title)
    site.serve(pages)

    result = scrape_knf_news()

    assert result["new"] == knf_scraper.MAX_ARTICLE_FETCHES == 20
    # The 21st link is still COUNTED — found is what the listing
    # offered, new is what the run could take
    assert result["found"] == 21
    assert len(stored()) == 20
    assert site.hits_on(_listing_url(20)) == 0


def test_the_walk_stops_at_the_listing_page_cap(app, site):
    pages = {}
    for page in range(12):
        pages[_listing_url(page * 5)] = _listing([(f"/aktualijos/p{page}", f"Puslapis {page}")])
        pages[_article_url(f"p{page}")] = _article(og_title=f"Puslapis {page}")
    site.serve(pages)

    result = scrape_knf_news()

    assert result["found"] == knf_scraper.MAX_LISTING_PAGES == 10
    # Offsets 0..45 are the ten pages the cap allows; the
    # eleventh is never requested even though it holds articles
    assert site.hits_on(_listing_url(45)) == 1
    assert site.hits_on(_listing_url(50)) == 0


def test_a_spent_budget_stops_the_run_before_the_first_fetch(app, site, monkeypatch, runs):
    monkeypatch.setattr(knf_scraper, "RUN_BUDGET_SECONDS", -1)
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _article_url("pirma"): _article(),
    })

    result = scrape_knf_news()

    assert (result["found"], result["new"]) == (0, 0)
    assert site.hits == []
    # Nothing downloaded, so this is "the site is slow", not
    # "the template changed" — the run completes
    assert runs(result["runId"])["status"] == "completed"


def test_a_budget_spent_mid_walk_stops_the_paging(app, site, monkeypatch, stored):
    # The budget is spent the moment the first listing page has
    # been read, which is what a slow-drip source looks like
    real_passed = knf_scraper.deadline_passed
    state = {"calls": 0}

    def _passed(deadline):
        state["calls"] += 1
        return state["calls"] > 3 or real_passed(deadline)

    monkeypatch.setattr(knf_scraper, "deadline_passed", _passed)
    site.serve({
        _listing_url(): _listing([("/aktualijos/a", "A")]),
        _listing_url(5): _listing([("/aktualijos/b", "B")]),
        _article_url("a"): _article(og_title="A"),
        _article_url("b"): _article(og_title="B"),
    })

    result = scrape_knf_news()

    assert result["new"] == 1
    assert [r["title"] for r in stored()] == ["A"]
    assert site.hits_on(_listing_url(5)) == 0




# -----------------------------------------------------------
# The run lock and the failure path
# -----------------------------------------------------------


def test_a_trigger_that_overlaps_a_running_scrape_steps_aside(app, site, runs):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })

    knf_scraper._RUN_LOCK.acquire()
    try:
        result = scrape_knf_news()
    finally:
        knf_scraper._RUN_LOCK.release()

    assert result == {"found": 0, "new": 0, "skipped": True}
    assert site.hits == []
    assert runs() == []


def test_the_lock_is_released_again_after_a_run(app, site):
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})

    scrape_knf_news()

    assert knf_scraper._RUN_LOCK.acquire(blocking=False)
    knf_scraper._RUN_LOCK.release()


def test_an_unexpected_error_fails_the_run_and_reports_it(app, site, monkeypatch, runs, stored):
    def _boom(_url):
        raise RuntimeError("normalise sprogo")

    monkeypatch.setattr(knf_scraper, "normalise_url", _boom)
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })

    result = scrape_knf_news()

    assert result["error"] == "normalise sprogo"
    assert (result["found"], result["new"]) == (0, 0)
    row = runs(result["runId"])
    assert row["status"] == "failed"
    assert row["error_message"] == "normalise sprogo"
    assert stored() == []
    # The lock must survive the exception or every later run is
    # answered "skipped" forever
    assert knf_scraper._RUN_LOCK.acquire(blocking=False)
    knf_scraper._RUN_LOCK.release()


def test_a_rollback_that_itself_fails_does_not_escape_the_scraper(app, site, monkeypatch, runs):
    # The connection the run was using is exactly the thing that
    # broke, so its rollback breaks too; mark_run_failed's own
    # fresh connection is what still closes the row
    class _BrokenConnection:

        def __init__(self, real, healthy_calls):
            self._real = real
            self._left = healthy_calls

        def execute(self, *args, **kwargs):
            if self._left <= 0:
                raise sqlite3.OperationalError("disk I/O error")
            self._left -= 1
            return self._real.execute(*args, **kwargs)

        def commit(self):
            return self._real.commit()

        def rollback(self):
            raise sqlite3.OperationalError("connection is gone")

        def close(self):
            return self._real.close()

    monkeypatch.setattr(knf_scraper, "get_db", lambda: _BrokenConnection(real_get_db(), 2))
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(),
    })

    result = scrape_knf_news()

    assert "disk I/O error" in result["error"]
    assert runs(result["runId"])["status"] == "failed"




# -----------------------------------------------------------
# Run bookkeeping — retention and the yield-drop alarm
# -----------------------------------------------------------


def test_run_rows_older_than_the_retention_window_are_pruned(app, site, seed_run, runs):
    old_knf = seed_run(source="knf.vu.lt", days_ago=40)
    older_knf = seed_run(source="knf.vu.lt", days_ago=90)
    old_vu = seed_run(source="vu.lt", days_ago=40)
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})

    result = scrape_knf_news()

    surviving = {row["id"] for row in runs()}
    assert old_knf not in surviving
    assert older_knf not in surviving
    # The newest run of EVERY source survives whatever its age,
    # so /api/scraper/status never loses a stalled scraper
    assert old_vu in surviving
    assert result["runId"] in surviving


def test_a_recent_run_row_is_not_pruned(app, site, seed_run, runs):
    recent = seed_run(source="knf.vu.lt", days_ago=2)
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})

    scrape_knf_news()

    assert recent in {row["id"] for row in runs()}


def test_a_collapsed_yield_is_logged_as_an_error(app, site, seed_run, caplog):
    seed_run(source="knf.vu.lt", days_ago=1, found=40)
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})

    with caplog.at_level(logging.ERROR, logger="app.scraper.common"):
        result = scrape_knf_news()

    assert result["found"] == 1
    assert "yield collapsed" in caplog.text




# -----------------------------------------------------------
# The push
# -----------------------------------------------------------


# -----------------------------------------------------------
# push_recorder
# -----------------------------------------------------------
#
# knf_scraper imports notify_channel INSIDE the run (a lazy
# import, not a cycle guard), so replacing the attribute on
# the push module is what the scraper picks up.
# -----------------------------------------------------------

@pytest.fixture
def push_recorder(monkeypatch):
    from app.notifications import push

    sent = []

    def _notify(channel, title, body, data=None, **kwargs):
        sent.append({"channel": channel, "title": title, "body": body,
                     "data": data, **kwargs})
        return len(sent)

    monkeypatch.setattr(push, "notify_channel", _notify)
    return sent


def test_one_new_article_pushes_singular_lithuanian_copy(app, site, seed_run, push_recorder):
    seed_run()
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})

    scrape_knf_news(notify=True)

    assert len(push_recorder) == 1
    sent = push_recorder[0]
    assert sent["channel"] == "news"
    assert sent["title"] == "KNF naujienos"
    assert sent["body"] == "Naujas straipsnis iš knf.vu.lt"
    assert sent["title_en"] == "KNF news"
    assert sent["body_en"] == "New article from knf.vu.lt"
    assert sent["data"] == {"type": "news", "source": "knf.vu.lt"}


def test_three_new_articles_decline_the_lithuanian_plural(app, site, seed_run, push_recorder):
    seed_run()
    items = [(f"/aktualijos/n{i}", f"Naujiena {i}") for i in range(3)]
    pages = {_listing_url(): _listing(items), _listing_url(5): EMPTY_LISTING}
    for href, title in items:
        pages[f"{BASE}{href}"] = _article(og_title=title)
    site.serve(pages)

    scrape_knf_news(notify=True)

    assert push_recorder[0]["title"] == "KNF naujienos (3)"
    assert push_recorder[0]["body"] == "3 nauji straipsniai iš knf.vu.lt"
    assert push_recorder[0]["body_en"] == "3 new articles from knf.vu.lt"


def test_the_admin_trigger_never_wakes_the_faculty(app, site, seed_run, push_recorder):
    seed_run()
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})

    scrape_knf_news(notify=False)

    assert push_recorder == []


def test_the_first_completed_run_is_a_backfill_and_stays_silent(app, site, push_recorder):
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})

    result = scrape_knf_news(notify=True)

    assert result["new"] == 1
    assert push_recorder == []


def test_a_second_push_within_the_hour_is_suppressed(app, site, seed_run, push_recorder):
    seed_run()
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article(og_title="Pirma")})
    scrape_knf_news(notify=True)

    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma"),
                                          ("/aktualijos/antra", "Antra")]),
                _article_url("antra"): _article(og_title="Antra")})
    second = scrape_knf_news(notify=True)

    assert second["new"] == 1
    assert len(push_recorder) == 1


def test_a_run_with_nothing_new_never_pushes(app, site, seed_run, push_recorder, seed_article):
    seed_run()
    seed_article(_article_url("sena"), title="Sena")
    site.serve({_listing_url(): _listing([("/aktualijos/sena", "Sena")]),
                _listing_url(5): EMPTY_LISTING})

    result = scrape_knf_news(notify=True)

    assert result["new"] == 0
    assert push_recorder == []


def test_a_push_that_blows_up_does_not_fail_the_run(app, site, seed_run, monkeypatch, runs):
    from app.notifications import push

    def _explode(*args, **kwargs):
        raise RuntimeError("Expo neatsako")

    monkeypatch.setattr(push, "notify_channel", _explode)
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})
    seed_run()

    result = scrape_knf_news(notify=True)

    assert result["new"] == 1
    assert "error" not in result
    assert runs(result["runId"])["status"] == "completed"




# -----------------------------------------------------------
# The admin routes that drive this scraper
# -----------------------------------------------------------


# -----------------------------------------------------------
# quiet_vu_scraper
# -----------------------------------------------------------
#
# POST /api/scraper/trigger runs the knf AND the vu scrape
# back to back, and _trigger_status lets either one decide the
# status code. vu.lt is another module's target and its host
# is not served here, so its run is stubbed to a clean zero —
# otherwise every status assertion below would be reading
# vu.lt's failure instead of knf.vu.lt's result.
# -----------------------------------------------------------

@pytest.fixture
def quiet_vu_scraper(monkeypatch):
    from app.scraper import routes

    monkeypatch.setattr(routes, "scrape_vu_news",
                        lambda **kwargs: {"found": 0, "new": 0, "runId": "vu-stub"})


def test_a_guest_cannot_trigger_the_scraper(client, site):
    assert client.post("/api/scraper/trigger").status_code == 401
    assert client.get("/api/scraper/status").status_code == 401
    assert site.hits == []


def test_a_student_cannot_trigger_the_scraper(client, actor, site):
    _user, headers = actor

    assert client.post("/api/scraper/trigger", headers=headers).status_code == 403
    assert client.get("/api/scraper/status", headers=headers).status_code == 403
    assert site.hits == []


def test_an_admin_trigger_runs_the_knf_scrape(client, admin, site, stored, quiet_vu_scraper):
    _user, headers = admin
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})

    response = client.post("/api/scraper/trigger", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body["knf"]["found"] == 1
    assert body["knf"]["new"] == 1
    assert len(stored()) == 1


def test_the_trigger_answers_409_while_a_run_holds_the_lock(client, admin, site, quiet_vu_scraper):
    _user, headers = admin

    knf_scraper._RUN_LOCK.acquire()
    try:
        response = client.post("/api/scraper/trigger", headers=headers)
    finally:
        knf_scraper._RUN_LOCK.release()

    assert response.status_code == 409
    assert response.get_json()["knf"]["skipped"] is True


def test_the_trigger_answers_502_when_the_template_changed(client, admin, site, quiet_vu_scraper):
    _user, headers = admin
    site.serve({_listing_url(): EMPTY_LISTING, _listing_url(5): EMPTY_LISTING})

    response = client.post("/api/scraper/trigger", headers=headers)

    assert response.status_code == 502
    # The exception text stays in the log and the run row; the
    # body carries a stable slug
    assert response.get_json()["knf"]["error"] == "scrape_failed"


def test_a_scraped_article_reaches_a_guest_through_the_feed(app, client, site):
    site.serve({
        _listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
        _listing_url(5): EMPTY_LISTING,
        _article_url("pirma"): _article(og_title="Renginys fakultete",
                                        og_image="https://newshub.vu.lt/nuotraukos/a.jpg"),
    })
    scrape_knf_news()

    # No login: the app must work without one, and the news
    # tab's 'knf.vu.lt' chip filters on exactly this source
    body = client.get("/api/news?source=knf.vu.lt").get_json()

    assert [post["title"] for post in body["posts"]] == ["Renginys fakultete"]
    post = body["posts"][0]
    assert post["source"] == "knf.vu.lt"
    assert post["postType"] == "article"
    assert post["sourceUrl"] == _article_url("pirma")
    assert post["imageUrl"] == "https://newshub.vu.lt/nuotraukos/a.jpg"


def test_the_status_route_shows_the_finished_run(client, admin, site):
    _user, headers = admin
    site.serve({_listing_url(): _listing([("/aktualijos/pirma", "Pirma")]),
                _listing_url(5): EMPTY_LISTING,
                _article_url("pirma"): _article()})
    result = scrape_knf_news()

    body = client.get("/api/scraper/status?source=knf.vu.lt", headers=headers).get_json()

    ids = [run["id"] for run in body["runs"]]
    assert result["runId"] in ids
    assert body["runs"][0]["articlesFound"] == 1
    assert body["runs"][0]["itemsNew"] == 1
    assert body["sources"][0]["source"] == "knf.vu.lt"
    assert body["sources"][0]["lastSuccess"]["id"] == result["runId"]
