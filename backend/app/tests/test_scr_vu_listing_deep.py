# -----------------------------------------------------------
#  [*] Tests — vu_scraper, the LISTING-PARSE half, exhaustively
#
#  The gap-closing pass over the half of scraper/vu_scraper.py
#  that decides WHAT to fetch, before a single article page is
#  parsed:
#
#    - _is_article_href — the path shape alone: the news
#      category plus a slug, under any leading segments, case
#      folded; the category root, a paging number, a
#      navigation slug, a lookalike category, a dot-segment
#      path, a doubled slash and a non-http scheme are all
#      refused, and an unparsable href comes back False
#      instead of raising
#    - _listing_links — one card is two anchors: the 10/11
#      character floor on a usable label, the title /
#      aria-label fallback chain and its shadowing, the
#      longest-label-wins rule and the two ways a later
#      anchor LOSES (shorter, equal), first-sighting page
#      order, and the raw href as the key
#    - scrape_vu_news STEPS 1-3.3 — the lock, the 'running'
#      row committed before the fetches, the ?page=N walk and
#      every way it ends (nothing unseen, the caller's
#      minimum, MAX_LISTING_PAGES, MAX_ARTICLE_FETCHES, the
#      wall-clock budget mid-page / end-of-page / next-page),
#      the allowlist refusal, the tombstone, the seen-set,
#      the already-stored skip, and the two failure exits —
#      a dead FIRST page and a page that downloaded but held
#      no recognisable link
#
#  The article-parse half (_fetch_vu_article, _article_summary,
#  _looks_like_chrome) is another agent's slice and is only
#  ever used here as the far end of a listing walk.
#
#  Every fetch goes through `responses` against HTML authored
#  in this file — the container runs --network none, so a test
#  that reaches vu.lt fails by construction.
# -----------------------------------------------------------


import logging
import sqlite3
import uuid

import pytest
import responses
import time_machine
from bs4 import BeautifulSoup
from responses import matchers

from app.scraper import common
from app.scraper.vu_scraper import (
    MAX_ARTICLE_FETCHES,
    MAX_LISTING_PAGES,
    NEWS_URL,
    _is_article_href,
    _listing_links,
    scrape_vu_news,
)


# Every article URL the scraper builds has been through
# normalise_url, which drops the "www." — so article pages are
# registered on the bare host whatever the listing linked
ARTICLE_HOST = "https://vu.lt"

# Long enough to clear _looks_like_chrome's 40-character floor
PARAGRAPH = ("Vilniaus universiteto Kauno fakultetas atidaro nauja duomenu mokslo laboratorija, "
             "kurioje dirbs dvidesimt tyreju ir keturiasdesimt studentu.")




# -----------------------------------------------------------
# _links
# -----------------------------------------------------------
#
# _listing_links over an HTML fragment. Fragments are parsed
# with lxml exactly as the scraper parses a downloaded page.
# -----------------------------------------------------------

def _links(html):
    return _listing_links(BeautifulSoup(html, "lxml"))




# -----------------------------------------------------------
# _card
# -----------------------------------------------------------
#
# One listing card the way vu.lt renders it: the image anchor
# FIRST and the headline anchor second, both on the same href.
# -----------------------------------------------------------

def _card(href, title):
    return (f'<div class="card"><a href="{href}"><img src="/t.jpg" alt=""></a>'
            f'<a href="{href}"><h3>{title}</h3></a></div>')




# -----------------------------------------------------------
# _listing_page
# -----------------------------------------------------------
#
# A listing document around the given cards. The chrome is the
# category root, a paging link and an off-category link — the
# three things the harvest must never take for a story.
# -----------------------------------------------------------

def _listing_page(*cards, chrome=True):
    nav = ('<nav><a href="/naujienos">Visos naujienos</a>'
           '<a href="/naujienos/?page=2">Kitas puslapis</a>'
           '<a href="/kontaktai">Kontaktai ir rekvizitai</a></nav>') if chrome else ""
    return ("<!doctype html><html lang='lt'><body>"
            f"<header>{nav}</header><main>{''.join(cards)}</main></body></html>")




# -----------------------------------------------------------
# _article_page
# -----------------------------------------------------------
#
# The far end of a listing link. `region=None` produces a page
# the content ladder recognises nothing in, which is how an
# article comes back with an empty title AND empty content.
# -----------------------------------------------------------

def _article_page(title="Nauja duomenu mokslo laboratorija Kaune",
                  published="2026-08-20T09:30:00+03:00",
                  body=PARAGRAPH,
                  region="article"):
    head = "<meta charset='utf-8'>"
    if published:
        head += f"<meta property='article:published_time' content='{published}'>"

    inner = (f"<h1>{title}</h1>" if title else "") + (f"<p>{body}</p>" if body else "")
    if region == "article":
        markup = f"<article>{inner}</article>"
    else:
        markup = f"<div class='wrap'>{inner}</div>"

    return f"<!doctype html><html lang='lt'><head>{head}</head><body>{markup}</body></html>"




# -----------------------------------------------------------
# _listing / _article / _article_at
# -----------------------------------------------------------
#
# Register one fake page. Listing page 1 is the bare URL and
# every later one carries ?page=N, which is exactly how the
# scraper walks them; a page nobody registers answers with a
# ConnectionError, this suite's "the site is down".
# -----------------------------------------------------------

def _listing(page, html, status=200, content_type="text/html"):
    match = ([matchers.query_string_matcher("")] if page == 1
             else [matchers.query_param_matcher({"page": str(page)})])
    responses.add(responses.GET, NEWS_URL, body=html, status=status,
                  content_type=content_type, match=match)


def _article_at(url, html=None, status=200):
    responses.add(responses.GET, url, body=html if html is not None else _article_page(),
                  status=status, content_type="text/html")


def _article(slug, html=None, status=200):
    _article_at(f"{ARTICLE_HOST}/naujienos/{slug}", html, status)




# -----------------------------------------------------------
# _rows / _run / _listing_calls
# -----------------------------------------------------------

def _rows(db):
    return db.execute(
        "SELECT * FROM news_posts WHERE source = 'vu.lt' ORDER BY source_url"
    ).fetchall()


def _run(db, run_id):
    return db.execute("SELECT * FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()


def _listing_calls():
    return [c.request.url for c in responses.calls if c.request.url.startswith(NEWS_URL)]




# -----------------------------------------------------------
# _store / _tombstone
# -----------------------------------------------------------
#
# A vu.lt row the scraper must treat as already seen, and a
# tombstone an admin left behind. Both are arranged on the
# test's own connection because no route creates them in the
# shape a listing walk has to skip.
# -----------------------------------------------------------

def _store(db, url, title="Jau issaugotas straipsnis apie moksla"):
    db.execute(
        "INSERT INTO news_posts (id, title, content, source, source_url, post_type, published_at)"
        " VALUES (?, ?, 'turinys', 'vu.lt', ?, 'article', '2026-08-01T09:00:00')",
        (str(uuid.uuid4()), title, url),
    )
    db.commit()


def _tombstone(db, url):
    db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)", (url,))
    db.commit()




# -----------------------------------------------------------
# _budget_spent_on_call
# -----------------------------------------------------------
#
# Makes deadline_passed answer True from its Nth call on, so
# the wall-clock budget can be spent at a chosen point of the
# walk without a single second of real time. Per listing page
# the scraper asks once at the top, once per candidate link
# and once at the bottom.
# -----------------------------------------------------------

def _budget_spent_on_call(monkeypatch, nth):
    calls = {"n": 0}

    def _fake(_deadline):
        calls["n"] += 1
        return calls["n"] >= nth

    monkeypatch.setattr("app.scraper.vu_scraper.deadline_passed", _fake)
    return calls




# -----------------------------------------------------------
# _no_push
# -----------------------------------------------------------
#
# Nothing in this file is about the push, but a second run
# inside one test would reach notify_channel for real. The
# per-PROCESS hourly cap is cleared on both sides so this file
# can never silence another one.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_push(monkeypatch):
    common._LAST_PUSH.clear()
    monkeypatch.setattr("app.notifications.push.notify_channel",
                        lambda *a, **k: 0)
    yield
    common._LAST_PUSH.clear()








# -----------------------------------------------------------
# _is_article_href — the shape of a story path
# -----------------------------------------------------------

def test_the_news_category_plus_a_slug_is_an_article():
    assert _is_article_href("/naujienos/nauja-laboratorija")
    assert _is_article_href("/naujienos/nauja-laboratorija/")
    assert _is_article_href("/visos-naujienos/nauja-laboratorija")
    assert _is_article_href("https://www.vu.lt/naujienos/nauja-laboratorija")


def test_segments_before_and_after_the_category_are_free():
    assert _is_article_href("/lt/naujienos/straipsnis")
    assert _is_article_href("/lt/visos-naujienos/mokslas/straipsnis")
    assert _is_article_href("/a/b/c/naujienos/d/e/f/straipsnis")


def test_the_category_root_alone_is_a_listing():
    assert not _is_article_href("/naujienos")
    assert not _is_article_href("/naujienos/")
    assert not _is_article_href("/visos-naujienos")
    assert not _is_article_href("/visos-naujienos/")
    assert not _is_article_href("/lt/naujienos/")


def test_a_lookalike_category_is_not_the_news_category():
    assert not _is_article_href("/senos-naujienos/straipsnis")
    assert not _is_article_href("/naujienos-archyvas/straipsnis")
    assert not _is_article_href("/naujiena/straipsnis")
    assert not _is_article_href("/xnaujienos/straipsnis")


def test_the_category_is_matched_case_insensitively():
    assert _is_article_href("/NAUJIENOS/Straipsnis")
    assert _is_article_href("/Visos-Naujienos/Straipsnis")
    assert _is_article_href("/LT/NaUjIeNoS/Straipsnis")


def test_every_navigation_slug_is_refused_whatever_its_case():
    for slug in ("naujienos", "visos-naujienos", "page", "puslapis"):
        assert not _is_article_href(f"/naujienos/{slug}"), slug
        assert not _is_article_href(f"/naujienos/{slug.upper()}"), slug
        assert not _is_article_href(f"/visos-naujienos/{slug}/"), slug


def test_a_numeric_slug_is_paging_not_a_story():
    assert not _is_article_href("/naujienos/0")
    assert not _is_article_href("/naujienos/1")
    assert not _is_article_href("/naujienos/2/")
    assert not _is_article_href("/naujienos/007")
    assert not _is_article_href("/naujienos/" + "9" * 200)
    assert not _is_article_href("/naujienos/tema/17")


# str.isdigit() is true for the superscript and the
# Arabic-Indic forms too, so those paging links die here as well
def test_a_unicode_digit_slug_is_paging_too():
    assert not _is_article_href("/naujienos/²")
    assert not _is_article_href("/naujienos/٢")


def test_a_slug_that_merely_starts_with_a_digit_is_a_story():
    assert _is_article_href("/naujienos/2a")
    assert _is_article_href("/naujienos/3d-spausdinimas")
    assert _is_article_href("/naujienos/2026-metu-apzvalga")


def test_a_number_inside_the_path_is_not_the_slug():
    assert _is_article_href("/naujienos/tema/2/straipsnis")
    assert _is_article_href("/naujienos/2026/rugpjutis/straipsnis")


def test_the_query_and_the_fragment_never_change_the_verdict():
    assert not _is_article_href("/naujienos/?page=2")
    assert not _is_article_href("/naujienos/#turinys")
    assert not _is_article_href("/naujienos?page=2")
    assert _is_article_href("/naujienos/straipsnis?utm_source=facebook")
    assert _is_article_href("/naujienos/straipsnis#turinys")
    assert _is_article_href("/naujienos/straipsnis/?page=3")


def test_a_relative_href_is_resolved_against_the_base():
    assert _is_article_href("naujienos/straipsnis")
    assert _is_article_href("./naujienos/straipsnis")


def test_a_protocol_relative_href_is_article_shaped():
    assert _is_article_href("//vu.lt/naujienos/straipsnis")
    assert not _is_article_href("//vu.lt/naujienos/")


# The host is host_allowed's business in the caller — this
# function answers about the PATH and nothing else
def test_the_host_is_not_this_functions_business():
    assert _is_article_href("https://evil.example.com/naujienos/straipsnis")
    assert _is_article_href("http://169.254.169.254/naujienos/straipsnis")


def test_dot_segments_collapse_back_onto_the_listing():
    assert not _is_article_href("/naujienos/straipsnis/..")
    assert not _is_article_href("/naujienos/straipsnis/../")
    assert not _is_article_href("/naujienos/.")


def test_a_doubled_slash_breaks_the_shape():
    assert not _is_article_href("/naujienos//straipsnis")
    assert not _is_article_href("//naujienos/straipsnis")


def test_a_non_http_scheme_has_no_article_path():
    assert not _is_article_href("mailto:info@vu.lt")
    assert not _is_article_href("tel:+37037000000")
    assert not _is_article_href("javascript:void(0)")
    assert not _is_article_href("data:text/html,naujienos/x")


def test_an_empty_or_missing_href_is_not_an_article():
    assert not _is_article_href("")
    assert not _is_article_href(None)
    assert not _is_article_href("   ")
    assert not _is_article_href("#")
    assert not _is_article_href("/")


# urljoin raises ValueError("Invalid IPv6 URL") on these — a
# malformed href is skipped, never allowed to crash the harvest
def test_an_unparsable_href_comes_back_false_instead_of_raising():
    assert not _is_article_href("http://[")
    assert not _is_article_href("https://[::1")
    assert not _is_article_href("http://[oops]:80/naujienos/straipsnis")


def test_an_enormous_slug_is_still_an_article():
    assert _is_article_href("/naujienos/" + "a" * 5000)


def test_a_percent_encoded_slug_is_still_an_article():
    assert _is_article_href("/naujienos/%20")
    assert _is_article_href("/naujienos/straipsnis%2Fkitas")


def test_a_path_outside_the_news_category_is_refused():
    assert not _is_article_href("/kontaktai/adresas")
    assert not _is_article_href("/studijos")
    assert not _is_article_href("/en/about-us/history")








# -----------------------------------------------------------
# _listing_links — which anchor of a card carries the headline
# -----------------------------------------------------------

def test_an_empty_document_yields_no_links():
    assert _links("") == []
    assert _links("<!doctype html><html><body></body></html>") == []


def test_an_anchor_without_an_href_attribute_is_ignored():
    assert _links("<a>Antraste be jokios nuorodos</a>") == []


def test_an_empty_href_attribute_is_ignored():
    assert _links('<a href="">Antraste be jokios nuorodos</a>') == []


# find_all("a", ...) — an <area> or a <link> carrying the same
# path is chrome, not a card
def test_a_non_anchor_element_with_an_href_is_ignored():
    html = ('<area href="/naujienos/aa" title="Antraste is atributo ilga">'
            '<link href="/naujienos/bb" title="Antraste is atributo ilga">')

    assert _links(html) == []


def test_listing_and_paging_chrome_is_never_harvested():
    assert _links(_listing_page()) == []


def test_an_image_first_card_keeps_the_headline_of_its_text_anchor():
    links = _links(_listing_page(_card("/naujienos/aa", "Naujas mokslo centras Kaune")))

    assert links == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


def test_a_label_of_exactly_ten_characters_is_unusable():
    assert len("Skaitykite") == 10
    assert _links('<a href="/naujienos/aa">Skaitykite</a>') == []


def test_a_label_of_eleven_characters_is_usable():
    assert len("Skaitykite!") == 11
    assert _links('<a href="/naujienos/aa">Skaitykite!</a>') == [
        {"href": "/naujienos/aa", "title": "Skaitykite!"}]


def test_a_short_label_is_replaced_by_a_longer_title_attribute():
    html = '<a href="/naujienos/aa" title="Naujas mokslo centras Kaune">Daugiau</a>'

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


def test_a_title_attribute_no_longer_than_the_text_does_not_win():
    html = '<a href="/naujienos/aa" title="Trumpas">Skaitykite</a>'

    # Both are under the floor, so the anchor is dropped either
    # way — what this pins is that the shorter attribute never
    # replaces the text
    assert _links(html) == []
    assert _links('<a href="/naujienos/aa" title="Trumpas">Skaitykite placiau</a>') == [
        {"href": "/naujienos/aa", "title": "Skaitykite placiau"}]


# The attribute wins the comparison and the anchor is STILL
# dropped: the floor is applied a second time afterwards
def test_a_title_attribute_of_exactly_ten_characters_leaves_the_anchor_unusable():
    html = '<a href="/naujienos/aa" title="Skaitykite">Daugiau</a>'

    assert _links(html) == []


def test_an_eleven_character_text_never_consults_the_attributes():
    html = ('<a href="/naujienos/aa" title="Kur kas ilgesne antraste is atributo">'
            'Skaitykite!</a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Skaitykite!"}]


def test_the_title_attribute_shadows_a_longer_aria_label():
    html = ('<a href="/naujienos/aa" title="Antraste is title"'
            ' aria-label="Kur kas ilgesne antraste is aria-label">'
            '<img src="/t.jpg" alt=""></a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Antraste is title"}]


def test_an_empty_title_attribute_falls_through_to_the_aria_label():
    html = ('<a href="/naujienos/aa" title="" aria-label="Naujas mokslo centras Kaune">'
            '<img src="/t.jpg" alt=""></a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


# A blank-but-PRESENT title attribute is truthy, so it used to
# shadow the usable aria-label beside it and the card was lost
# for the whole run. Each candidate is stripped before it is
# judged, so the aria-label is reached
def test_a_whitespace_only_title_attribute_falls_through_to_the_aria_label():
    html = ('<a href="/naujienos/aa" title="   " aria-label="Naujas mokslo centras Kaune">'
            '<img src="/t.jpg" alt=""></a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


def test_the_attribute_label_is_stripped():
    html = '<a href="/naujienos/aa" title="  Naujas mokslo centras  "><img src="/t.jpg"></a>'

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras"}]


def test_an_anchor_with_no_label_at_all_is_dropped():
    html = ('<a href="/naujienos/aa"><img src="/t.jpg" alt=""></a>'
            '<a href="/naujienos/aa">Daugiau</a>')

    assert _links(html) == []


def test_a_second_anchor_with_a_longer_headline_wins():
    html = ('<a href="/naujienos/aa">Trumpesne antraste</a>'
            '<a href="/naujienos/aa">Ilgesne ir tikslesne antraste</a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Ilgesne ir tikslesne antraste"}]


def test_a_second_anchor_with_a_shorter_headline_loses():
    html = ('<a href="/naujienos/aa">Ilgesne ir tikslesne antraste</a>'
            '<a href="/naujienos/aa">Trumpesne antraste</a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Ilgesne ir tikslesne antraste"}]


# Equal length is NOT longer — the first sighting keeps the
# headline, so the harvest is stable whichever anchor comes first
def test_a_second_anchor_of_equal_length_loses_to_the_first():
    first, second = "Antraste 111", "Antraste 222"
    assert len(first) == len(second)

    assert _links(f'<a href="/naujienos/aa">{first}</a>'
                  f'<a href="/naujienos/aa">{second}</a>') == [
        {"href": "/naujienos/aa", "title": first}]


def test_a_third_anchor_can_still_take_the_headline():
    html = ('<a href="/naujienos/aa">Antraste vienas</a>'
            '<a href="/naujienos/aa">Antraste du</a>'
            '<a href="/naujienos/aa">Pati ilgiausia antraste is triju</a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Pati ilgiausia antraste is triju"}]


def test_page_order_follows_the_first_sighting_of_each_href():
    html = ('<a href="/naujienos/bb">Antraste apie sporta</a>'
            '<a href="/naujienos/aa">Antraste apie moksla</a>'
            '<a href="/naujienos/bb">Ilgesne antraste apie sporta</a>'
            '<a href="/naujienos/cc">Antraste apie studijas</a>')

    assert [link["href"] for link in _links(html)] == [
        "/naujienos/bb", "/naujienos/aa", "/naujienos/cc"]


# The key is the RAW href, so three spellings of one article
# are three entries here — normalise_url in the caller is what
# collapses them
def test_the_raw_href_is_the_key_so_two_spellings_are_two_entries():
    html = ('<a href="/naujienos/aa">Antraste apie moksla</a>'
            '<a href="/naujienos/aa/">Antraste apie moksla</a>'
            '<a href="https://www.vu.lt/naujienos/aa">Antraste apie moksla</a>')

    assert [link["href"] for link in _links(html)] == [
        "/naujienos/aa", "/naujienos/aa/", "https://www.vu.lt/naujienos/aa"]


# The strings are joined with a SPACE: a headline broken across
# inline tags used to harvest as "Naujasmokslocentras", and this
# text is the title of last resort actually stored for an
# article page that carries no <h1>
def test_nested_markup_in_a_headline_keeps_its_spaces():
    html = '<a href="/naujienos/aa"><span>Naujas</span> <b>mokslo</b> centras</a>'

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras"}]


def test_whitespace_around_a_headline_is_stripped():
    html = '<a href="/naujienos/aa">\n   Naujas mokslo centras Kaune   \n</a>'

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


def test_a_headline_made_only_of_whitespace_falls_back_to_the_attribute():
    html = '<a href="/naujienos/aa" aria-label="Naujas mokslo centras Kaune">   </a>'

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


def test_a_listing_href_is_dropped_however_long_its_headline():
    html = ('<a href="/naujienos/">Visos fakulteto naujienos vienoje vietoje</a>'
            '<a href="/naujienos/page">Kitas naujienu puslapis su daug teksto</a>')

    assert _links(html) == []


def test_a_hundred_cards_all_come_back_in_page_order():
    cards = [_card(f"/naujienos/a{i:03d}", f"Antraste numeris {i:03d} apie moksla")
             for i in range(100)]

    links = _links(_listing_page(*cards))

    assert len(links) == 100
    assert [link["href"] for link in links] == [f"/naujienos/a{i:03d}" for i in range(100)]








# -----------------------------------------------------------
# scrape_vu_news — the run row and the lock, before any fetch
# -----------------------------------------------------------

@responses.activate
def test_a_run_is_visible_as_running_while_its_fetches_are_in_flight(app, db):
    seen = []

    # The status page reads the run row on its OWN connection
    # while the article fetch is still outstanding
    def _peek(request):
        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            seen.extend(row[0] for row in
                        conn.execute("SELECT status FROM scraper_runs WHERE source = 'vu.lt'"))
        finally:
            conn.close()
        return 200, {}, _article_page()

    _listing(1, _listing_page(_card("/naujienos/aa", "Naujas mokslo centras Kaune")))
    responses.add_callback(responses.GET, f"{ARTICLE_HOST}/naujienos/aa",
                           callback=_peek, content_type="text/html")

    result = scrape_vu_news()

    assert seen == ["running"]
    assert _run(db, result["runId"])["status"] == "completed"


@responses.activate
def test_the_run_row_is_stamped_with_the_moment_the_walk_opened(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Naujas mokslo centras Kaune")))
    _article("aa")

    with time_machine.travel("2026-08-29T12:00:00+00:00", tick=False):
        result = scrape_vu_news()

    run = _run(db, result["runId"])
    assert run["started_at"] == "2026-08-29T12:00:00+00:00"
    assert run["finished_at"] == "2026-08-29T12:00:00+00:00"


@responses.activate
def test_a_skipped_run_writes_no_run_row_at_all(app, db):
    from app.scraper.vu_scraper import _RUN_LOCK

    _listing(1, _listing_page(_card("/naujienos/aa", "Naujas mokslo centras Kaune")))
    assert _RUN_LOCK.acquire(blocking=False)
    try:
        result = scrape_vu_news()
    finally:
        _RUN_LOCK.release()

    assert result == {"found": 0, "new": 0, "skipped": True}
    assert "runId" not in result
    assert db.execute("SELECT COUNT(*) FROM scraper_runs").fetchone()[0] == 0
    assert _listing_calls() == []


@responses.activate
def test_the_lock_is_free_again_after_a_completed_run(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Naujas mokslo centras Kaune")))
    _article("aa")

    first = scrape_vu_news()
    second = scrape_vu_news()

    assert first["new"] == 1
    assert "skipped" not in second
    assert second["found"] == 1
    assert second["new"] == 0








# -----------------------------------------------------------
# scrape_vu_news — how the ?page=N walk is driven
# -----------------------------------------------------------

@responses.activate
def test_the_first_listing_page_is_requested_without_a_page_parameter(app, db):
    _store(db, f"{ARTICLE_HOST}/naujienos/aa")
    _listing(1, _listing_page(_card("/naujienos/aa", "Naujas mokslo centras Kaune")))

    scrape_vu_news()

    assert _listing_calls() == [NEWS_URL]


@responses.activate
def test_later_listing_pages_carry_their_own_page_number(app, db):
    for page in (1, 2, 3):
        _listing(page, _listing_page(
            _card(f"/naujienos/a{page}", f"Antraste numeris {page} apie moksla")))
        _article(f"a{page}")

    scrape_vu_news()

    assert _listing_calls()[:3] == [NEWS_URL, f"{NEWS_URL}?page=2", f"{NEWS_URL}?page=3"]


@responses.activate
def test_a_page_holding_nothing_unseen_ends_the_walk(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(2, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(3, _listing_page(_card("/naujienos/bb", "Antraste apie sporta")))
    _article("aa")

    result = scrape_vu_news()

    assert result["found"] == 1
    assert len(_listing_calls()) == 2
    assert not any("page=3" in url for url in _listing_calls())


@responses.activate
def test_a_minimum_of_zero_pages_still_walks_the_first_one(app, db):
    _store(db, f"{ARTICLE_HOST}/naujienos/aa")
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(2, _listing_page(_card("/naujienos/bb", "Antraste apie sporta")))

    result = scrape_vu_news(pages=0)

    assert result["found"] == 1
    assert result["new"] == 0
    assert len(_listing_calls()) == 1


@responses.activate
def test_a_negative_minimum_behaves_like_zero(app, db):
    _store(db, f"{ARTICLE_HOST}/naujienos/aa")
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(2, _listing_page(_card("/naujienos/bb", "Antraste apie sporta")))

    result = scrape_vu_news(pages=-5)

    assert result["found"] == 1
    assert len(_listing_calls()) == 1


@responses.activate
def test_the_minimum_page_count_keeps_walking_pages_that_hold_nothing_unseen(app, db):
    for page in (1, 2, 3, 4):
        _store(db, f"{ARTICLE_HOST}/naujienos/a{page}")
        _listing(page, _listing_page(
            _card(f"/naujienos/a{page}", f"Antraste numeris {page} apie moksla")))

    result = scrape_vu_news(pages=3)

    assert result["found"] == 3
    assert result["new"] == 0
    assert len(_listing_calls()) == 3


@responses.activate
def test_a_minimum_beyond_the_hard_cap_still_stops_at_the_cap(app, db):
    for page in range(1, MAX_LISTING_PAGES + 3):
        _store(db, f"{ARTICLE_HOST}/naujienos/a{page}")
        _listing(page, _listing_page(
            _card(f"/naujienos/a{page}", f"Antraste numeris {page} apie moksla")))

    result = scrape_vu_news(pages=99)

    assert result["found"] == MAX_LISTING_PAGES
    assert len(_listing_calls()) == MAX_LISTING_PAGES
    assert not any(f"page={MAX_LISTING_PAGES + 1}" in url for url in _listing_calls())


@responses.activate
def test_a_later_page_that_answers_500_only_ends_the_walk(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(2, "<html></html>", status=500)
    _article("aa")

    result = scrape_vu_news(pages=4)

    assert result["found"] == 1
    assert result["new"] == 1
    assert _run(db, result["runId"])["status"] == "completed"


@responses.activate
def test_a_later_page_that_never_answers_only_ends_the_walk(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _article("aa")

    result = scrape_vu_news(pages=5)

    assert result["found"] == 1
    assert _run(db, result["runId"])["status"] == "completed"








# -----------------------------------------------------------
# scrape_vu_news — the allowlist, the seen set and the tombstone
# -----------------------------------------------------------

@responses.activate
def test_an_off_allowlist_link_is_warned_about_and_never_counted(app, db, caplog):
    _listing(1, _listing_page(
        _card("https://evil.example.com/naujienos/xx", "Iterpta nuoroda i svetima serveri"),
        _card("/naujienos/aa", "Naujas mokslo centras Kaune"),
    ))
    _article("aa")

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 1
    assert "Skipping off-allowlist article link https://evil.example.com/naujienos/xx" in caplog.text
    assert not any("evil.example.com" in c.request.url for c in responses.calls)


@responses.activate
def test_a_protocol_relative_link_to_another_host_is_refused(app, db):
    _listing(1, _listing_page(
        _card("//evil.example.com/naujienos/xx", "Iterpta nuoroda i svetima serveri"),
        _card("/naujienos/aa", "Naujas mokslo centras Kaune"),
    ))
    _article("aa")

    result = scrape_vu_news()

    assert result["found"] == 1
    assert not any("evil.example.com" in c.request.url for c in responses.calls)


# Off-allowlist links are refused BEFORE they are counted, so a
# listing made of nothing else reads as a markup change
@responses.activate
def test_a_listing_of_nothing_but_off_allowlist_links_fails_the_run(app, db):
    _listing(1, _listing_page(
        _card("https://evil.example.com/naujienos/xx", "Iterpta nuoroda i svetima serveri"),
        _card("https://evil.example.com/naujienos/yy", "Kita iterpta svetima nuoroda"),
    ))

    result = scrape_vu_news()

    assert result["found"] == 0
    assert "markup has probably changed" in result["error"]
    assert _run(db, result["runId"])["status"] == "failed"


# normalise_url canonicalises the scheme, so an odd one on an
# ALLOWED host is laundered onto https and fetched — the host
# allowlist, not the scheme of the href, is the defence
@responses.activate
def test_an_odd_scheme_on_the_allowed_host_is_canonicalised_onto_https(app, db):
    _listing(1, _listing_page(_card("ftp://vu.lt/naujienos/aa", "Naujas mokslo centras Kaune")))
    _article("aa")

    result = scrape_vu_news()

    assert result["new"] == 1
    assert _rows(db)[0]["source_url"] == f"{ARTICLE_HOST}/naujienos/aa"
    assert not any(c.request.url.startswith("ftp") for c in responses.calls)


@responses.activate
def test_a_link_under_another_scheme_that_leaves_the_allowlist_is_still_refused(app, db):
    _listing(1, _listing_page(
        _card("ftp://evil.example.com/naujienos/xx", "Iterpta nuoroda i svetima serveri"),
        _card("file:///etc/naujienos/passwd", "Nuoroda i konteinerio faila"),
        _card("/naujienos/aa", "Naujas mokslo centras Kaune"),
    ))
    _article("aa")

    result = scrape_vu_news()

    assert result["found"] == 1
    assert not any("evil.example.com" in c.request.url for c in responses.calls)
    assert not any(c.request.url.startswith("file") for c in responses.calls)


@responses.activate
def test_four_spellings_of_one_link_are_found_once_and_fetched_once(app, db):
    _listing(1, _listing_page(
        _card("/naujienos/aa", "Naujas mokslo centras Kaune"),
        _card("/naujienos/aa/", "Naujas mokslo centras Kaune"),
        _card("https://www.vu.lt/naujienos/aa?utm_source=facebook", "Naujas mokslo centras Kaune"),
        _card("//vu.lt/naujienos/aa#turinys", "Naujas mokslo centras Kaune"),
    ))
    _article("aa")

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 1
    assert len([c for c in responses.calls if "/naujienos/aa" in c.request.url]) == 1


@responses.activate
def test_one_article_linked_from_two_pages_is_counted_once(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla"),
                              _card("/naujienos/bb", "Antraste apie sporta")))
    _listing(2, _listing_page(_card("/naujienos/aa", "Antraste apie moksla"),
                              _card("/naujienos/cc", "Antraste apie studijas")))
    for slug in ("aa", "bb", "cc"):
        _article(slug, _article_page(title=f"Straipsnis {slug} apie viska"))

    result = scrape_vu_news()

    assert result["found"] == 3
    assert result["new"] == 3


# The URL is marked seen on its FIRST sighting, so a longer
# headline further down the walk arrives too late to be used
@responses.activate
def test_a_better_headline_on_a_later_page_cannot_replace_the_first_sighting(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(2, _listing_page(_card("/naujienos/aa", "Kur kas ilgesne ir tikslesne antraste")))
    _article("aa", _article_page(title=""))

    result = scrape_vu_news()

    assert result["new"] == 1
    assert _rows(db)[0]["title"] == "Antraste apie moksla"


# The markup guard weighs the WHOLE run: one page that yielded
# a link is enough, however empty the pages behind it
@responses.activate
def test_the_markup_guard_weighs_the_whole_run_not_one_page(app, db):
    _store(db, f"{ARTICLE_HOST}/naujienos/aa")
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(2, _listing_page())

    result = scrape_vu_news(pages=2)

    assert result["found"] == 1
    assert "error" not in result
    assert _run(db, result["runId"])["status"] == "completed"


@responses.activate
def test_every_link_being_tombstoned_still_completes_the_run(app, db):
    for slug in ("aa", "bb"):
        _tombstone(db, f"{ARTICLE_HOST}/naujienos/{slug}")
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla"),
                              _card("/naujienos/bb", "Antraste apie sporta")))

    result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 0
    assert _run(db, result["runId"])["status"] == "completed"
    assert len(responses.calls) == 1


# The tombstone is stored the way an admin's delete left it and
# matched in normalise_url shape, so a campaign-tagged link
# cannot walk around it
@responses.activate
def test_a_tombstone_is_matched_in_normalised_shape(app, db):
    _tombstone(db, "https://www.vu.lt/naujienos/aa/?utm_source=facebook")
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert not any("/naujienos/aa" in c.request.url for c in responses.calls)


@responses.activate
def test_an_already_stored_article_does_not_extend_the_walk(app, db):
    _store(db, f"{ARTICLE_HOST}/naujienos/aa")
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(2, _listing_page(_card("/naujienos/bb", "Antraste apie sporta")))

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert len(_listing_calls()) == 1


@responses.activate
def test_a_stored_article_is_skipped_even_when_its_link_carries_a_campaign_tag(app, db):
    _store(db, f"{ARTICLE_HOST}/naujienos/aa")
    _listing(1, _listing_page(
        _card("https://www.vu.lt/naujienos/aa/?utm_medium=email", "Antraste apie moksla")))

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert len(_rows(db)) == 1








# -----------------------------------------------------------
# scrape_vu_news — the fetch cap and the wall-clock budget
# -----------------------------------------------------------

@responses.activate
def test_exactly_the_fetch_cap_is_stored_and_the_next_page_is_never_asked_for(app, db, caplog):
    cards = []
    for i in range(MAX_ARTICLE_FETCHES):
        slug = f"a{i:02d}"
        cards.append(_card(f"/naujienos/{slug}", f"Antraste numeris {i:02d} apie moksla"))
        _article(slug, _article_page(title=f"Antraste numeris {i:02d} apie moksla"))
    _listing(1, _listing_page(*cards))
    _listing(2, _listing_page(_card("/naujienos/zz", "Antraste apie sporta")))

    with caplog.at_level(logging.INFO, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == MAX_ARTICLE_FETCHES
    assert result["new"] == MAX_ARTICLE_FETCHES
    assert "stopped at listing page 1 (fetch cap or budget)" in caplog.text
    assert len(_listing_calls()) == 1


@responses.activate
def test_the_link_past_the_fetch_cap_is_counted_but_never_downloaded(app, db):
    cards = []
    for i in range(MAX_ARTICLE_FETCHES + 3):
        slug = f"a{i:02d}"
        cards.append(_card(f"/naujienos/{slug}", f"Antraste numeris {i:02d} apie moksla"))
        _article(slug, _article_page(title=f"Antraste numeris {i:02d} apie moksla"))
    _listing(1, _listing_page(*cards))

    result = scrape_vu_news()

    # The link that tripped the cap is counted found; the two
    # behind it never reach the loop at all
    assert result["found"] == MAX_ARTICLE_FETCHES + 1
    assert result["new"] == MAX_ARTICLE_FETCHES
    assert len(_rows(db)) == MAX_ARTICLE_FETCHES
    for i in (MAX_ARTICLE_FETCHES, MAX_ARTICLE_FETCHES + 1, MAX_ARTICLE_FETCHES + 2):
        assert not any(c.request.url.endswith(f"a{i:02d}") for c in responses.calls)


@responses.activate
def test_a_budget_spent_before_the_first_page_completes_an_empty_run(app, db, monkeypatch, caplog):
    _budget_spent_on_call(monkeypatch, 1)
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _article("aa")

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 0
    assert result["new"] == 0
    assert "out of time after 0 listing page(s)" in caplog.text
    run = _run(db, result["runId"])
    assert run["status"] == "completed"
    assert (run["articles_found"], run["articles_new"]) == (0, 0)


@responses.activate
def test_a_budget_spent_mid_page_leaves_the_rest_counted_but_unfetched(app, db, monkeypatch):
    # Calls: 1 top of page, 2 first link, 3 second link
    _budget_spent_on_call(monkeypatch, 3)
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla"),
                              _card("/naujienos/bb", "Antraste apie sporta")))
    _article("aa")
    _article("bb")

    result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 1
    assert [row["source_url"] for row in _rows(db)] == [f"{ARTICLE_HOST}/naujienos/aa"]
    assert not any(c.request.url.endswith("/naujienos/bb") for c in responses.calls)


@responses.activate
def test_a_budget_spent_at_the_end_of_a_page_stops_the_walk(app, db, monkeypatch, caplog):
    # Calls: 1 top, 2 first link, 3 second link, 4 bottom of page
    _budget_spent_on_call(monkeypatch, 4)
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla"),
                              _card("/naujienos/bb", "Antraste apie sporta")))
    _listing(2, _listing_page(_card("/naujienos/cc", "Antraste apie studijas")))
    for slug in ("aa", "bb", "cc"):
        _article(slug, _article_page(title=f"Straipsnis {slug} apie viska"))

    with caplog.at_level(logging.INFO, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 2
    assert "stopped at listing page 1 (fetch cap or budget)" in caplog.text
    assert len(_listing_calls()) == 1


@responses.activate
def test_a_budget_spent_before_a_later_page_logs_the_pages_already_walked(app, db, monkeypatch, caplog):
    # Calls: 1 top, 2 link, 3 bottom, 4 top of page two
    _budget_spent_on_call(monkeypatch, 4)
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _listing(2, _listing_page(_card("/naujienos/bb", "Antraste apie sporta")))
    _article("aa")
    _article("bb")

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 1
    assert "out of time after 1 listing page(s)" in caplog.text
    assert len(_listing_calls()) == 1








# -----------------------------------------------------------
# scrape_vu_news — the two ways the listing walk fails the run
# -----------------------------------------------------------

@responses.activate
def test_a_dead_first_page_fails_the_run_however_many_pages_were_asked_for(app, db):
    result = scrape_vu_news(pages=5)

    assert result == {"found": 0, "new": 0,
                      "error": f"Failed to fetch {NEWS_URL}", "runId": result["runId"]}
    run = _run(db, result["runId"])
    assert run["status"] == "failed"
    assert run["error_message"] == f"Failed to fetch {NEWS_URL}"
    assert run["finished_at"] is not None
    assert len(_listing_calls()) == 1


@responses.activate
def test_a_first_page_that_answers_500_fails_the_run(app, db):
    _listing(1, "<html></html>", status=500)

    result = scrape_vu_news()

    assert result["error"] == f"Failed to fetch {NEWS_URL}"
    assert _run(db, result["runId"])["status"] == "failed"


@responses.activate
def test_a_first_page_that_answers_json_fails_the_run(app, db):
    _listing(1, '{"items": []}', content_type="application/json")

    result = scrape_vu_news()

    assert result["error"] == f"Failed to fetch {NEWS_URL}"
    assert _run(db, result["runId"])["status"] == "failed"


@responses.activate
def test_an_empty_listing_body_fails_the_run_as_a_markup_change(app, db, caplog):
    _listing(1, "")

    with caplog.at_level(logging.ERROR, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 0
    assert "markup has probably changed" in result["error"]
    assert "found nothing on 1 downloaded listing page(s)" in caplog.text
    run = _run(db, result["runId"])
    assert run["status"] == "failed"
    assert run["articles_found"] == 0


@responses.activate
def test_a_listing_of_nothing_but_chrome_fails_the_run(app, db):
    _listing(1, _listing_page())

    result = scrape_vu_news()

    assert "markup has probably changed" in result["error"]
    assert _run(db, result["runId"])["status"] == "failed"


# Two pages downloaded and neither held a link: the count in
# the ERROR line is the number that actually came down the wire
@responses.activate
def test_the_markup_failure_counts_every_page_that_downloaded(app, db, caplog):
    _listing(1, _listing_page())
    _listing(2, _listing_page())

    with caplog.at_level(logging.ERROR, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news(pages=2)

    assert "found nothing on 2 downloaded listing page(s)" in caplog.text
    assert _run(db, result["runId"])["status"] == "failed"


# Nothing downloaded at all is a DIFFERENT failure — the budget
# ended the walk, and a zero found is honest rather than a
# markup change
@responses.activate
def test_a_run_that_downloaded_no_page_is_not_a_markup_failure(app, db, monkeypatch):
    _budget_spent_on_call(monkeypatch, 1)
    _listing(1, _listing_page())

    result = scrape_vu_news()

    assert "error" not in result
    assert _run(db, result["runId"])["status"] == "completed"


@responses.activate
def test_malformed_markup_is_still_harvested(app, db):
    _listing(1, "<html><body><ul><li><a href='/naujienos/aa'>Naujas mokslo centras Kaune</a>"
                "<li><a href='/naujienos/bb'>Antraste apie sporta ir studijas</a></ul>")
    _article("aa")
    _article("bb", _article_page(title="Antraste apie sporta ir studijas"))

    result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 2








# -----------------------------------------------------------
# scrape_vu_news — what the listing headline is worth
# -----------------------------------------------------------

@responses.activate
def test_the_listing_headline_is_the_title_of_last_resort(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste tik is saraso puslapio")))
    _article("aa", _article_page(title="", body=PARAGRAPH))

    result = scrape_vu_news()

    assert result["new"] == 1
    assert _rows(db)[0]["title"] == "Antraste tik is saraso puslapio"


@responses.activate
def test_an_article_whose_page_is_dead_is_counted_but_stores_no_row(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Straipsnis kurio puslapis netyla"),
                              _card("/naujienos/bb", "Antraste apie sporta")))
    _article("bb", _article_page(title="Antraste apie sporta"))

    result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 1
    assert [row["source_url"] for row in _rows(db)] == [f"{ARTICLE_HOST}/naujienos/bb"]


@responses.activate
def test_the_newest_article_of_the_run_is_the_one_logged(app, db, caplog):
    dates = {"aa": "2026-08-01T09:00:00+00:00",
             "bb": "2026-08-20T09:00:00+00:00",
             "cc": "2026-08-10T09:00:00+00:00"}
    cards = []
    for slug, published in dates.items():
        cards.append(_card(f"/naujienos/{slug}", f"Antraste {slug} apie viska ir dar daugiau"))
        _article(slug, _article_page(title=f"Antraste {slug} apie viska ir dar daugiau",
                                     published=published))
    _listing(1, _listing_page(*cards))

    with caplog.at_level(logging.INFO, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 3
    assert "newest=2026-08-20T09:00:00" in caplog.text


# The listing headline is the LAST resort and _listing_links
# can never hand over an empty one — so this drives the guard
# the way a future listing parser could break it
@responses.activate
def test_a_link_with_no_headline_and_an_empty_page_stores_no_row(app, db, monkeypatch, caplog):
    monkeypatch.setattr("app.scraper.vu_scraper._listing_links",
                        lambda soup: [{"href": "/naujienos/aa", "title": ""}])
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _article("aa", _article_page(title="", body="", region=None))

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert "parsed to nothing — not stored" in caplog.text
    assert _rows(db) == []
    assert _run(db, result["runId"])["status"] == "completed"


# A title-less row skips the republished-title lookup entirely:
# an empty title would otherwise match every other title-less
# row of this source
@responses.activate
def test_a_title_less_article_with_content_is_stored_without_a_duplicate_check(app, db, monkeypatch):
    monkeypatch.setattr("app.scraper.vu_scraper._listing_links",
                        lambda soup: [{"href": "/naujienos/aa", "title": ""}])
    _store(db, f"{ARTICLE_HOST}/naujienos/senas", title="")
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla")))
    _article("aa", _article_page(title="", body=PARAGRAPH))

    result = scrape_vu_news()

    assert result["new"] == 1
    stored = db.execute("SELECT title, content FROM news_posts WHERE source_url = ?",
                        (f"{ARTICLE_HOST}/naujienos/aa",)).fetchone()
    assert stored["title"] == ""
    assert PARAGRAPH in stored["content"]








# -----------------------------------------------------------
# scrape_vu_news — the counts the run row carries away
# -----------------------------------------------------------

@responses.activate
def test_the_run_row_carries_the_counts_of_the_listing_walk(app, db):
    _store(db, f"{ARTICLE_HOST}/naujienos/aa")
    _tombstone(db, f"{ARTICLE_HOST}/naujienos/bb")
    _listing(1, _listing_page(
        _card("/naujienos/aa", "Antraste jau issaugota anksciau"),
        _card("/naujienos/bb", "Antraste kuria administratorius istryne"),
        _card("/naujienos/cc", "Antraste apie visai nauja straipsni"),
        _card("https://evil.example.com/naujienos/dd", "Iterpta svetima nuoroda"),
    ))
    _article("cc", _article_page(title="Antraste apie visai nauja straipsni"))

    result = scrape_vu_news()

    run = _run(db, result["runId"])
    assert (run["articles_found"], run["articles_new"]) == (3, 1)
    assert run["status"] == "completed"
    assert run["finished_at"] is not None
    assert run["error_message"] is None
    assert result == {"found": 3, "new": 1, "runId": result["runId"]}


@responses.activate
def test_a_second_walk_over_the_same_listing_finds_everything_and_inserts_nothing(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraste apie moksla"),
                              _card("/naujienos/bb", "Antraste apie sporta")))
    for slug in ("aa", "bb"):
        _article(slug, _article_page(title=f"Straipsnis {slug} apie viska"))

    first = scrape_vu_news()
    second = scrape_vu_news()

    assert (first["found"], first["new"]) == (2, 2)
    assert (second["found"], second["new"]) == (2, 0)
    assert len(_rows(db)) == 2
    assert db.execute("SELECT COUNT(*) FROM scraper_runs WHERE status = 'completed'"
                      ).fetchone()[0] == 2
