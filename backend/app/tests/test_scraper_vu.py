# -----------------------------------------------------------
#  [*] Tests — scraper/vu_scraper.py, the vu.lt newsroom
#
#  What this module proves about scrape_vu_news and the four
#  helpers it owns (_listing_links, _is_article_href,
#  _fetch_vu_article, _article_summary, _looks_like_chrome):
#
#    - link harvesting: an article href is the news category
#      PLUS a slug, so "/naujienos", "/naujienos/" and
#      "/naujienos/2" are listings and paging, never stories;
#      an image-first card keeps the headline its second
#      anchor carries, and an image-only anchor falls back to
#      title/aria-label
#    - article parsing: <h1>, the <article>/[class*=content]/
#      <main> content ladder with its chrome decomposed, the
#      og:description teaser and the body-minus-preamble one
#    - images: og:image wins, newshub.vu.lt covers are kept,
#      a protocol-relative src is resolved against the page,
#      logo/icon/pixel/tracking/avatar are skipped and an
#      off-allowlist host is dropped rather than published to
#      every guest
#    - dates: the source's UTC offset is APPLIED, a future or
#      pre-historic stamp is clamped to now, and <time> is the
#      fallback the meta leaves
#    - dedupe: normalise_url collapses a campaign-tagged link,
#      a redirect stores the POST-REDIRECT URL, a republished
#      title is refused, INSERT OR IGNORE is the backstop, and
#      re-running the same scrape inserts nothing
#    - the tombstone: a URL an admin deleted is counted found
#      and never fetched again
#    - the run row: 'running' -> 'completed' with its counts,
#      'failed' on a dead first listing page and on a listing
#      that downloaded but yielded nothing, retention pruning,
#      and the yield-collapse ERROR line
#    - the bounds: MAX_LISTING_PAGES, MAX_ARTICLE_FETCHES and
#      the wall-clock budget
#    - the push: one per run, declined Lithuanian copy, no
#      push for a hand-fired run or a first backfill, and a
#      push failure that does not fail the run
#    - the admin routes that fire it: 401/403/200/409/502
#
#  Every fetch goes through `responses` against HTML authored
#  in this file — the container runs --network none, so a test
#  that reaches vu.lt fails by construction.
# -----------------------------------------------------------


import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

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
    _article_summary,
    _fetch_vu_article,
    _is_article_href,
    _listing_links,
    _looks_like_chrome,
    scrape_vu_news,
)


# The listing lives on www, but every article URL the scraper
# builds has already been through normalise_url — which strips
# the "www." — so article pages are registered on the bare host
ARTICLE_HOST = "https://vu.lt"

# A real vu.lt cover lives on the shared newshub host, which is
# in IMAGE_HOSTS but in neither page allowlist
COVER = "https://newshub.vu.lt/media/cover.jpg"

# Two paragraphs long enough to survive _looks_like_chrome's
# 40-character floor, so a body-derived summary has something
# to be made of
BODY = (
    "Vilniaus universiteto Kauno fakultetas atidaro naują duomenų mokslo laboratoriją.",
    "Laboratorijoje dirbs dvidešimt tyrėjų ir keturiasdešimt studentų iš keturių programų.",
)




# -----------------------------------------------------------
# _card
# -----------------------------------------------------------
#
# One listing card in the shape vu.lt renders: an image anchor
# FIRST and the headline anchor second, both pointing at the
# same href. That order is the regression _listing_links exists
# for — marking the href seen on the image anchor dropped every
# card on the page.
# -----------------------------------------------------------

def _card(href, title):
    cover = f'<a href="{href}"><img src="/media/thumb.jpg" alt=""></a>'
    return f'<div class="news-card">{cover}<a href="{href}"><h3>{title}</h3></a></div>'




# -----------------------------------------------------------
# _listing_page
# -----------------------------------------------------------
#
# A listing page around the given cards, with the site chrome
# that must NOT be harvested: the category root, a paging link
# and an off-category link.
# -----------------------------------------------------------

def _listing_page(*cards, chrome=True):
    nav = ""
    if chrome:
        nav = ('<nav><a href="/naujienos">Visos naujienos</a>'
               '<a href="/naujienos/?page=2">Kitas puslapis</a>'
               '<a href="/kontaktai">Kontaktai ir rekvizitai</a></nav>')
    return ("<!doctype html><html lang='lt'><body>"
            f"<header>{nav}</header><main>{''.join(cards)}</main>"
            "</body></html>")




# -----------------------------------------------------------
# _article_page
# -----------------------------------------------------------
#
# One article page. `region` picks which container the parser's
# content ladder has to land on ("article", "content", "main",
# or "none" for a page it recognises nothing in), and
# og_image=None omits the meta entirely while og_image=""
# leaves it there empty — two different branches.
# -----------------------------------------------------------

def _article_page(title="Nauja duomenų mokslo laboratorija Kaune",
                  paragraphs=BODY,
                  published="2026-08-20T09:30:00+03:00",
                  og_image=COVER,
                  og_description=None,
                  meta_description=None,
                  images=(),
                  time_datetime=None,
                  time_text=None,
                  region="article",
                  chrome=True):

    head = "<meta charset='utf-8'>"
    if published is not None:
        head += f"<meta property='article:published_time' content='{published}'>"
    if og_image is not None:
        head += f"<meta property='og:image' content='{og_image}'>"
    if og_description is not None:
        head += f"<meta property='og:description' content='{og_description}'>"
    if meta_description is not None:
        head += f"<meta name='description' content='{meta_description}'>"

    stamp = ""
    if time_datetime is not None:
        stamp = f"<time datetime='{time_datetime}'>{time_text or ''}</time>"
    elif time_text is not None:
        stamp = f"<time>{time_text}</time>"

    inner = (f"<h1>{title}</h1>" if title else "") + stamp
    inner += "".join(f'<img src="{src}" alt="">' for src in images)
    inner += "".join(f"<p>{p}</p>" for p in paragraphs)

    junk = ("<script>window.__NEXT_DATA__={}</script>"
            "<nav>Pradžia / Naujienos</nav>"
            "<aside>Susiję straipsniai</aside>") if chrome else ""

    if region == "article":
        body = f"<article>{junk}{inner}</article>"
    elif region == "content":
        body = f"<div class='article-content'>{junk}{inner}</div>"
    elif region == "main":
        body = f"<main>{junk}{inner}</main>"
    else:
        body = f"<div class='wrap'>{junk}{inner}</div>"

    return f"<!doctype html><html lang='lt'><head>{head}</head><body>{body}</body></html>"




# -----------------------------------------------------------
# _listing / _article
# -----------------------------------------------------------
#
# Register one fake page. Listing page 1 is the bare URL and
# every later one carries ?page=N, which is exactly how the
# scraper walks them; a page nobody registers answers with a
# ConnectionError, which is this suite's "the site is down".
# -----------------------------------------------------------

def _listing(page, html, status=200):
    match = ([matchers.query_string_matcher("")] if page == 1
             else [matchers.query_param_matcher({"page": str(page)})])
    responses.add(responses.GET, NEWS_URL, body=html, status=status,
                  content_type="text/html", match=match)


def _article(slug, html, status=200, headers=None):
    responses.add(responses.GET, f"{ARTICLE_HOST}/naujienos/{slug}", body=html,
                  status=status, content_type="text/html", headers=headers)




# -----------------------------------------------------------
# _one_article_site
# -----------------------------------------------------------
#
# The smallest complete site: one listing page holding one card
# and the article behind it. Returns the canonical URL the row
# is expected to carry.
# -----------------------------------------------------------

def _one_article_site(slug="laboratorija", title="Nauja laboratorija Kauno fakultete", **article):
    _listing(1, _listing_page(_card(f"/naujienos/{slug}", title)))
    _article(slug, _article_page(title=title, **article))
    return f"{ARTICLE_HOST}/naujienos/{slug}"




# -----------------------------------------------------------
# _rows / _run
# -----------------------------------------------------------
#
# The stored vu.lt articles and the scraper_runs row of a run,
# read back on the test's own connection.
# -----------------------------------------------------------

def _rows(db):
    return db.execute(
        "SELECT * FROM news_posts WHERE source = 'vu.lt' ORDER BY source_url"
    ).fetchall()


def _run(db, run_id):
    return db.execute("SELECT * FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()




# -----------------------------------------------------------
# _seed_run
# -----------------------------------------------------------
#
# An earlier scraper_runs row. push_allowed refuses to wake a
# device on a source's FIRST completed run, so every push test
# needs one of these, and the retention and yield-drop tests
# need one with a chosen age.
# -----------------------------------------------------------

def _seed_run(db, source="vu.lt", status="completed", found=5, days_ago=1):
    started = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    run_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO scraper_runs (id, source, status, articles_found, articles_new, started_at, finished_at)"
        " VALUES (?, ?, ?, ?, 0, ?, ?)",
        (run_id, source, status, found, started, started),
    )
    db.commit()
    return run_id




# -----------------------------------------------------------
# _forget_recent_pushes
# -----------------------------------------------------------
#
# common._LAST_PUSH is per PROCESS, so one test that pushed
# would silence every later one through the hourly cap.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _forget_recent_pushes():
    common._LAST_PUSH.clear()
    yield
    common._LAST_PUSH.clear()




# -----------------------------------------------------------
# push_spy
# -----------------------------------------------------------
#
# Records every notify_channel call instead of fanning out to
# Expo. The scraper imports the function lazily inside the run,
# so patching the module attribute is what the call resolves.
# -----------------------------------------------------------

@pytest.fixture
def push_spy(monkeypatch):
    sent = []

    def _fake(channel, title, body, data=None, title_en=None, body_en=None, stats=None):
        sent.append({"channel": channel, "title": title, "body": body, "data": data,
                     "title_en": title_en, "body_en": body_en})
        return len(sent)

    monkeypatch.setattr("app.notifications.push.notify_channel", _fake)
    return sent








# -----------------------------------------------------------
# _is_article_href — what counts as a story
# -----------------------------------------------------------

def test_a_news_path_with_a_slug_is_an_article():
    assert _is_article_href("/naujienos/nauja-laboratorija")
    assert _is_article_href("https://www.vu.lt/naujienos/nauja-laboratorija")
    assert _is_article_href("/lt/visos-naujienos/mokslas/nauja-laboratorija")
    assert _is_article_href("/en/naujienos/tema/story/")


def test_the_news_category_root_is_a_listing_not_an_article():
    assert not _is_article_href("/naujienos")
    assert not _is_article_href("/naujienos/")
    assert not _is_article_href("/lt/naujienos/")
    assert not _is_article_href("/visos-naujienos")


def test_a_query_string_cannot_make_a_listing_look_like_an_article():
    assert not _is_article_href("/naujienos/?page=2")
    assert not _is_article_href("/naujienos/#turinys")


def test_a_navigation_slug_is_not_an_article():
    assert not _is_article_href("/naujienos/page")
    assert not _is_article_href("/naujienos/puslapis")
    assert not _is_article_href("/naujienos/naujienos")
    assert not _is_article_href("/visos-naujienos/visos-naujienos")


def test_a_numeric_last_segment_is_paging_not_an_article():
    assert not _is_article_href("/naujienos/2")
    assert not _is_article_href("/naujienos/tema/17")


def test_a_path_outside_the_news_category_is_not_an_article():
    assert not _is_article_href("/kontaktai/adresas")
    assert not _is_article_href("/studijos")


def test_an_empty_href_is_not_an_article():
    assert not _is_article_href("")
    assert not _is_article_href(None)


def test_an_unparsable_href_is_not_an_article():
    # urljoin raises ValueError("Invalid IPv6 URL") on this one —
    # a malformed href must be skipped, never crash the harvest
    assert not _is_article_href("http://[")








# -----------------------------------------------------------
# _listing_links — one card, two anchors
# -----------------------------------------------------------

def _links(html):
    return _listing_links(BeautifulSoup(html, "lxml"))


def test_an_image_first_card_keeps_the_headline_of_its_text_anchor():
    links = _links(_listing_page(_card("/naujienos/aa", "Naujas mokslo centras Kaune")))

    assert links == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


def test_an_image_only_anchor_falls_back_to_its_title_attribute():
    html = ('<a href="/naujienos/aa" title="Naujas mokslo centras Kaune">'
            '<img src="/t.jpg" alt=""></a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


def test_an_image_only_anchor_falls_back_to_its_aria_label():
    html = ('<a href="/naujienos/aa" aria-label="Naujas mokslo centras Kaune">'
            '<img src="/t.jpg" alt=""></a>')

    assert _links(html) == [{"href": "/naujienos/aa", "title": "Naujas mokslo centras Kaune"}]


def test_an_anchor_with_no_usable_label_at_all_is_dropped():
    html = '<a href="/naujienos/aa"><img src="/t.jpg" alt=""></a><a href="/naujienos/aa">Daugiau</a>'

    assert _links(html) == []


def test_the_longest_label_wins_and_page_order_is_kept():
    html = ('<a href="/naujienos/bb">Trumpas antraštės variantas</a>'
            '<a href="/naujienos/aa">Pirmas straipsnis apie mokslą</a>'
            '<a href="/naujienos/bb">Ilgesnis ir tikslesnis antraštės variantas</a>')

    assert _links(html) == [
        {"href": "/naujienos/bb", "title": "Ilgesnis ir tikslesnis antraštės variantas"},
        {"href": "/naujienos/aa", "title": "Pirmas straipsnis apie mokslą"},
    ]


def test_listing_chrome_is_never_harvested_as_an_article():
    assert _links(_listing_page()) == []








# -----------------------------------------------------------
# _looks_like_chrome / _article_summary — the teaser
# -----------------------------------------------------------

def test_a_short_line_is_chrome():
    assert _looks_like_chrome("", "Antraštė")
    assert _looks_like_chrome("Dalintis", "Antraštė")


def test_the_repeated_title_is_chrome():
    title = "Nauja duomenų mokslo laboratorija Kauno fakultete"

    assert _looks_like_chrome(title, title)
    assert _looks_like_chrome(title.upper(), title)


def test_a_short_date_line_is_chrome():
    assert _looks_like_chrome("2026-08-29 paskelbta Vilniaus universiteto tinklalapyje", "T")
    assert _looks_like_chrome("Paskelbta 2026 m. rugpjūčio 29 d. Kauno fakultete", "T")


def test_a_real_paragraph_is_not_chrome():
    line = ("Laboratorijoje dirbs dvidešimt tyrėjų ir keturiasdešimt studentų iš keturių "
            "skirtingų studijų programų, o pirmieji projektai startuos rudenį.")

    assert not _looks_like_chrome(line, "Antraštė")
    # Over 80 characters the date shapes stop counting as chrome
    assert len(line) >= 80


def test_the_og_description_wins_over_the_body():
    described = "Kauno fakultete atidaroma duomenų mokslo laboratorija su dvidešimt darbo vietų."
    soup = BeautifulSoup(_article_page(og_description=described), "lxml")

    assert _article_summary(soup, "\n".join(BODY), "Antraštė") == described


def test_the_plain_meta_description_is_the_second_choice():
    described = "Kauno fakultete atidaroma duomenų mokslo laboratorija su dvidešimt darbo vietų."
    soup = BeautifulSoup(_article_page(meta_description=described), "lxml")

    assert _article_summary(soup, "\n".join(BODY), "Antraštė") == described


def test_a_too_short_description_is_ignored_and_the_body_is_used():
    soup = BeautifulSoup(_article_page(og_description="Trumpai"), "lxml")

    summary = _article_summary(soup, "\n".join(BODY), "Antraštė")

    assert summary.startswith("Vilniaus universiteto Kauno fakultetas")


def test_the_body_summary_drops_its_leading_chrome():
    title = "Nauja duomenų mokslo laboratorija Kauno fakultete"
    content = "\n".join(["Pradžia / Naujienos", title,
                         "2026-08-29 paskelbta Vilniaus universiteto tinklalapyje", BODY[0]])
    soup = BeautifulSoup(_article_page(og_description=None), "lxml")

    assert _article_summary(soup, content, title) == BODY[0]


def test_a_body_that_is_nothing_but_chrome_still_yields_its_text():
    soup = BeautifulSoup("<html><body></body></html>", "lxml")

    assert _article_summary(soup, "Dalintis\nSpausdinti", "Antraštė") == "Dalintis\nSpausdinti"


def test_a_long_summary_is_cut_back_to_a_word_boundary():
    content = " ".join(["labai"] * 100)
    soup = BeautifulSoup("<html><body></body></html>", "lxml")

    summary = _article_summary(soup, content, "Antraštė")

    assert summary.endswith("...")
    assert len(summary) <= 203
    assert "lab..." not in summary








# -----------------------------------------------------------
# _fetch_vu_article — one page to one row's worth of fields
# -----------------------------------------------------------

@responses.activate
def test_an_article_that_cannot_be_fetched_comes_back_as_nothing(app):
    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/nera") is None


@responses.activate
def test_an_off_allowlist_article_url_is_never_requested(app):
    assert _fetch_vu_article("https://evil.example.com/naujienos/x") is None
    assert len(responses.calls) == 0


@responses.activate
def test_an_article_page_yields_every_stored_field(app):
    _article("aa", _article_page())

    data = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")

    assert data["url"] == f"{ARTICLE_HOST}/naujienos/aa"
    assert data["title"] == "Nauja duomenų mokslo laboratorija Kaune"
    assert BODY[0] in data["content"]
    assert data["summary"].startswith(BODY[0])
    assert data["image_url"] == COVER
    assert data["author"] == "Vilniaus universitetas"
    # 09:30 +03:00 is 06:30 UTC — the offset is APPLIED, not dropped
    assert data["date"] == "2026-08-20T06:30:00"


@responses.activate
def test_the_page_chrome_is_decomposed_out_of_the_content(app):
    _article("aa", _article_page())

    content = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]

    assert "__NEXT_DATA__" not in content
    assert "Susiję straipsniai" not in content
    assert "Pradžia / Naujienos" not in content


@responses.activate
def test_a_page_with_no_h1_comes_back_with_an_empty_title(app):
    _article("aa", _article_page(title=""))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"] == ""


@responses.activate
def test_the_content_class_region_is_used_when_there_is_no_article_element(app):
    _article("aa", _article_page(region="content"))

    assert BODY[1] in _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]


@responses.activate
def test_main_is_the_last_content_region_tried(app):
    _article("aa", _article_page(region="main"))

    assert BODY[1] in _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]


@responses.activate
def test_a_page_with_no_recognisable_region_yields_empty_content(app):
    _article("aa", _article_page(region="none"))

    data = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")

    assert data["content"] == ""
    assert data["summary"] == ""
    assert data["title"] == "Nauja duomenų mokslo laboratorija Kaune"


@responses.activate
def test_title_content_and_summary_are_cut_to_the_stored_limits(app):
    _article("aa", _article_page(title="A" * 400,
                                 paragraphs=("B" * 12000,),
                                 og_description="C" * 900))

    data = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")

    assert len(data["title"]) == common.MAX_TITLE_LENGTH
    assert len(data["content"]) == common.MAX_CONTENT_LENGTH
    assert len(data["summary"]) == common.MAX_SUMMARY_LENGTH








# -----------------------------------------------------------
# _fetch_vu_article — the image ladder
# -----------------------------------------------------------

@responses.activate
def test_the_newshub_cover_from_og_image_wins_over_a_body_image(app):
    _article("aa", _article_page(images=("/media/inline.jpg",)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == COVER


@responses.activate
def test_an_empty_og_image_falls_through_to_the_body_image(app):
    _article("aa", _article_page(og_image="", images=("/media/inline.jpg",)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == \
        f"{ARTICLE_HOST}/media/inline.jpg"


@responses.activate
def test_an_off_allowlist_og_image_falls_through_to_the_body_image(app):
    _article("aa", _article_page(og_image="https://tracker.example.com/beacon.png",
                                 images=("/media/inline.jpg",)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == \
        f"{ARTICLE_HOST}/media/inline.jpg"


@responses.activate
def test_a_protocol_relative_cover_is_resolved_against_the_page(app):
    _article("aa", _article_page(og_image=None, images=("//newshub.vu.lt/media/x.jpg",)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == \
        "https://newshub.vu.lt/media/x.jpg"


@responses.activate
def test_logos_icons_pixels_trackers_and_avatars_are_skipped(app):
    _article("aa", _article_page(og_image=None, images=(
        "/assets/logo.svg", "/assets/icon-share.png", "/pixel.gif",
        "/tracking/beacon.png", "/users/avatar.jpg", "/media/real.jpg",
    )))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == \
        f"{ARTICLE_HOST}/media/real.jpg"


@responses.activate
def test_an_off_allowlist_body_image_is_dropped_and_the_next_one_tried(app):
    _article("aa", _article_page(og_image=None, images=(
        "https://cdn.example.com/photo.jpg", "/media/real.jpg",
    )))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == \
        f"{ARTICLE_HOST}/media/real.jpg"


@responses.activate
def test_a_page_with_no_usable_image_stores_none(app):
    _article("aa", _article_page(og_image=None, images=("https://cdn.example.com/photo.jpg",)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] is None


@responses.activate
def test_an_absurdly_long_image_url_is_dropped(app):
    _article("aa", _article_page(og_image=COVER + "?q=" + "x" * 2100, images=()))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] is None








# -----------------------------------------------------------
# _fetch_vu_article — the date ladder and its clamp
# -----------------------------------------------------------

@responses.activate
def test_a_time_element_datetime_supplies_the_date_when_the_meta_is_missing(app):
    _article("aa", _article_page(published=None, time_datetime="2026-08-18T12:00:00+00:00"))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-18T12:00:00"


@responses.activate
def test_a_bare_time_element_text_supplies_the_date(app):
    _article("aa", _article_page(published=None, time_text="2026-08-17"))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-17T00:00:00"


@responses.activate
def test_a_negative_offset_is_applied_rather_than_failing_to_parse(app):
    _article("aa", _article_page(published="2026-08-19T22:00:00-05:00"))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-20T03:00:00"


@responses.activate
def test_a_future_published_time_is_clamped_to_now(app):
    _article("aa", _article_page(published="2030-01-01T00:00:00+00:00"))

    with time_machine.travel("2026-08-29 12:00:00 +0000", tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-29T12:00:00"


@responses.activate
def test_a_prehistoric_published_time_is_clamped_to_now(app):
    _article("aa", _article_page(published="1999-01-01T00:00:00+00:00"))

    with time_machine.travel("2026-08-29 12:00:00 +0000", tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-29T12:00:00"


@responses.activate
def test_an_unparsable_date_falls_back_to_now(app):
    _article("aa", _article_page(published=None, time_text="vakar"))

    with time_machine.travel("2026-08-29 12:00:00 +0000", tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-29T12:00:00"


@responses.activate
def test_a_page_with_no_date_at_all_falls_back_to_now(app):
    _article("aa", _article_page(published=None))

    with time_machine.travel("2026-08-29 12:00:00 +0000", tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-29T12:00:00"








# -----------------------------------------------------------
# scrape_vu_news — the happy run
# -----------------------------------------------------------

@responses.activate
def test_a_run_stores_the_articles_it_finds(app, db):
    _listing(1, _listing_page(
        _card("/naujienos/aa", "Nauja laboratorija Kauno fakultete"),
        _card("/naujienos/bb", "Studentai laimėjo tarptautinį konkursą"),
    ))
    _article("aa", _article_page(title="Nauja laboratorija Kauno fakultete"))
    _article("bb", _article_page(title="Studentai laimėjo tarptautinį konkursą"))

    result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 2
    rows = _rows(db)
    assert [r["source_url"] for r in rows] == [
        f"{ARTICLE_HOST}/naujienos/aa", f"{ARTICLE_HOST}/naujienos/bb"]
    assert rows[0]["source"] == "vu.lt"
    assert rows[0]["post_type"] == "article"
    assert rows[0]["author_name"] == "Vilniaus universitetas"
    assert rows[0]["image_url"] == COVER
    assert rows[0]["author_id"] is None


@responses.activate
def test_the_run_row_is_completed_with_its_counts(app, db):
    _one_article_site()

    result = scrape_vu_news()

    run = _run(db, result["runId"])
    assert run["source"] == "vu.lt"
    assert run["status"] == "completed"
    assert run["articles_found"] == 1
    assert run["articles_new"] == 1
    assert run["finished_at"] is not None
    assert run["error_message"] is None


@responses.activate
def test_re_running_the_same_scrape_inserts_nothing(app, db):
    _one_article_site()

    first = scrape_vu_news()
    second = scrape_vu_news()

    assert first["new"] == 1
    assert second["found"] == 1
    assert second["new"] == 0
    assert len(_rows(db)) == 1


@responses.activate
def test_an_already_stored_article_is_counted_found_but_never_refetched(app, db):
    _one_article_site()
    scrape_vu_news()
    before = len(responses.calls)

    result = scrape_vu_news()

    assert result["found"] == 1
    # Only the listing page was fetched the second time round
    assert len(responses.calls) == before + 1


@responses.activate
def test_the_listing_headline_is_the_title_of_last_resort(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraštė iš sąrašo puslapio")))
    _article("aa", _article_page(title=""))

    scrape_vu_news()

    assert _rows(db)[0]["title"] == "Antraštė iš sąrašo puslapio"


@responses.activate
def test_an_article_that_parses_to_nothing_is_stored_under_its_listing_headline(app, db):
    # The listing link text is ALWAYS longer than ten characters
    # (_listing_links refuses anything shorter), so the "parsed to
    # nothing — not stored" branch below it never fires: a page the
    # parser recognises nothing in still becomes a row, headline and
    # URL intact, instead of the "Untitled" it used to be
    _listing(1, _listing_page(_card("/naujienos/aa", "Antraštė iš sąrašo puslapio")))
    _article("aa", _article_page(title="", region="none", og_image=None, published=None))

    result = scrape_vu_news()

    assert result["new"] == 1
    row = _rows(db)[0]
    assert row["title"] == "Antraštė iš sąrašo puslapio"
    assert row["content"] == ""
    assert row["image_url"] is None








# -----------------------------------------------------------
# scrape_vu_news — dedupe, tombstones and the URL key
# -----------------------------------------------------------

@responses.activate
def test_a_campaign_tagged_link_collapses_onto_the_clean_one(app, db):
    _listing(1, _listing_page(
        _card("/naujienos/aa", "Nauja laboratorija Kauno fakultete"),
        _card("/naujienos/aa?utm_source=facebook", "Nauja laboratorija Kauno fakultete"),
    ))
    _article("aa", _article_page(title="Nauja laboratorija Kauno fakultete"))

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 1
    assert [r["source_url"] for r in _rows(db)] == [f"{ARTICLE_HOST}/naujienos/aa"]


@responses.activate
def test_the_post_redirect_url_is_what_gets_stored(app, db):
    _listing(1, _listing_page(_card("/naujienos/senas", "Straipsnis persikėlė kitu adresu")))
    _article("senas", "", status=301,
             headers={"Location": f"{ARTICLE_HOST}/naujienos/naujas"})
    _article("naujas", _article_page(title="Straipsnis persikėlė kitu adresu"))

    scrape_vu_news()

    assert [r["source_url"] for r in _rows(db)] == [f"{ARTICLE_HOST}/naujienos/naujas"]


@responses.activate
def test_two_links_redirecting_to_one_article_insert_a_single_row(app, db):
    # The INSERT OR IGNORE backstop: both links resolve to the
    # same canonical URL, and the second page answers with a
    # different headline so the title dedupe cannot catch it
    _listing(1, _listing_page(
        _card("/naujienos/aa", "Pirmoji nuoroda į tą patį straipsnį"),
        _card("/naujienos/bb", "Antroji nuoroda į tą patį straipsnį"),
    ))
    _article("aa", "", status=302, headers={"Location": f"{ARTICLE_HOST}/naujienos/cc"})
    _article("bb", "", status=302, headers={"Location": f"{ARTICLE_HOST}/naujienos/cc"})
    _article("cc", _article_page(title="Pirmoji nuoroda į tą patį straipsnį"))
    _article("cc", _article_page(title="Antroji nuoroda į tą patį straipsnį"))

    result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 1
    assert [r["source_url"] for r in _rows(db)] == [f"{ARTICLE_HOST}/naujienos/cc"]


@responses.activate
def test_the_same_story_republished_under_a_second_url_is_not_stored_twice(app, db):
    db.execute(
        "INSERT INTO news_posts (id, title, content, source, source_url, published_at)"
        " VALUES (?, 'Nauja laboratorija Kauno fakultete', 'x', 'vu.lt', ?, '2026-08-01T00:00:00')",
        (str(uuid.uuid4()), f"{ARTICLE_HOST}/naujienos/senas"),
    )
    db.commit()
    _one_article_site(slug="naujas", title="Nauja laboratorija Kauno fakultete")

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert len(_rows(db)) == 1


@responses.activate
def test_a_tombstoned_article_is_counted_but_never_fetched_again(app, db):
    db.execute("INSERT INTO deleted_source_urls (source_url) VALUES (?)",
               (f"{ARTICLE_HOST}/naujienos/aa",))
    db.commit()
    _one_article_site(slug="aa")

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert _rows(db) == []
    assert [c.request.url for c in responses.calls] == [NEWS_URL]


@responses.activate
def test_an_off_allowlist_article_link_is_refused(app, db, caplog):
    _listing(1, _listing_page(
        _card("https://evil.example.com/naujienos/xx", "Įterpta nuoroda į svetimą serverį"),
        _card("/naujienos/aa", "Nauja laboratorija Kauno fakultete"),
    ))
    _article("aa", _article_page(title="Nauja laboratorija Kauno fakultete"))

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 1
    assert "Skipping off-allowlist article link" in caplog.text
    assert not any("evil.example.com" in c.request.url for c in responses.calls)


@responses.activate
def test_an_article_whose_page_is_dead_is_counted_but_not_stored(app, db):
    _listing(1, _listing_page(
        _card("/naujienos/aa", "Straipsnis kurio puslapis neatsako"),
        _card("/naujienos/bb", "Studentai laimėjo tarptautinį konkursą"),
    ))
    _article("bb", _article_page(title="Studentai laimėjo tarptautinį konkursą"))

    result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 1
    assert [r["source_url"] for r in _rows(db)] == [f"{ARTICLE_HOST}/naujienos/bb"]








# -----------------------------------------------------------
# scrape_vu_news — paging and the caps
# -----------------------------------------------------------

@responses.activate
def test_paging_continues_while_pages_still_hold_unseen_articles(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Pirmas straipsnis apie mokslą")))
    _listing(2, _listing_page(_card("/naujienos/bb", "Antras straipsnis apie studijas")))
    # Page three repeats page two — nothing unseen, so paging ends
    _listing(3, _listing_page(_card("/naujienos/bb", "Antras straipsnis apie studijas")))
    _listing(4, _listing_page(_card("/naujienos/cc", "Trečias straipsnis apie sportą")))
    _article("aa", _article_page(title="Pirmas straipsnis apie mokslą"))
    _article("bb", _article_page(title="Antras straipsnis apie studijas"))

    result = scrape_vu_news()

    assert result["found"] == 2
    assert result["new"] == 2
    assert sum(1 for c in responses.calls if c.request.url.startswith(NEWS_URL)) == 3
    assert not any("page=4" in c.request.url for c in responses.calls)


@responses.activate
def test_a_minimum_page_count_is_walked_even_with_nothing_unseen(app, db):
    _listing(1, _listing_page())
    _listing(2, _listing_page(_card("/naujienos/bb", "Antras straipsnis apie studijas")))
    _article("bb", _article_page(title="Antras straipsnis apie studijas"))

    result = scrape_vu_news(pages=2)

    assert result["found"] == 1
    assert result["new"] == 1


@responses.activate
def test_paging_stops_at_the_hard_listing_page_cap(app, db):
    for page in range(1, MAX_LISTING_PAGES + 2):
        slug = f"a{page}"
        _listing(page, _listing_page(_card(f"/naujienos/{slug}", f"Straipsnis numeris {page} apie mokslą")))
        _article(slug, _article_page(title=f"Straipsnis numeris {page} apie mokslą"))

    result = scrape_vu_news()

    assert result["found"] == MAX_LISTING_PAGES
    assert not any(f"page={MAX_LISTING_PAGES + 1}" in c.request.url for c in responses.calls)


@responses.activate
def test_a_later_listing_page_that_fails_only_ends_the_paging(app, db):
    _listing(1, _listing_page(_card("/naujienos/aa", "Pirmas straipsnis apie mokslą")))
    _article("aa", _article_page(title="Pirmas straipsnis apie mokslą"))

    result = scrape_vu_news(pages=3)

    assert result["found"] == 1
    assert result["new"] == 1
    assert _run(db, result["runId"])["status"] == "completed"


@responses.activate
def test_the_run_stops_at_the_article_fetch_cap(app, db):
    cards = []
    for i in range(MAX_ARTICLE_FETCHES + 1):
        slug = f"a{i:02d}"
        cards.append(_card(f"/naujienos/{slug}", f"Straipsnis numeris {i:02d} apie mokslą"))
        _article(slug, _article_page(title=f"Straipsnis numeris {i:02d} apie mokslą"))
    _listing(1, _listing_page(*cards))

    result = scrape_vu_news()

    assert result["found"] == MAX_ARTICLE_FETCHES + 1
    assert result["new"] == MAX_ARTICLE_FETCHES
    assert len(_rows(db)) == MAX_ARTICLE_FETCHES
    # The link past the cap was counted, never downloaded
    assert not any(c.request.url.endswith(f"a{MAX_ARTICLE_FETCHES:02d}") for c in responses.calls)


@responses.activate
def test_a_spent_wall_clock_budget_ends_the_run_before_the_first_page(app, db, monkeypatch, caplog):
    monkeypatch.setattr("app.scraper.vu_scraper.RUN_BUDGET_SECONDS", -1)
    _one_article_site()

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result == {"found": 0, "new": 0, "runId": result["runId"]}
    assert len(responses.calls) == 0
    assert "out of time" in caplog.text
    assert _run(db, result["runId"])["status"] == "completed"








# -----------------------------------------------------------
# scrape_vu_news — the failure paths
# -----------------------------------------------------------

@responses.activate
def test_a_dead_first_listing_page_fails_the_run(app, db):
    result = scrape_vu_news()

    assert result["found"] == 0
    assert result["new"] == 0
    assert result["error"] == f"Failed to fetch {NEWS_URL}"
    run = _run(db, result["runId"])
    assert run["status"] == "failed"
    assert run["error_message"] == result["error"]
    assert run["finished_at"] is not None


@responses.activate
def test_a_listing_that_downloads_but_holds_no_article_fails_the_run(app, db, caplog):
    _listing(1, _listing_page())

    with caplog.at_level(logging.ERROR, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert "markup has probably changed" in result["error"]
    assert _run(db, result["runId"])["status"] == "failed"
    assert "found nothing on 1 downloaded listing page(s)" in caplog.text


@responses.activate
def test_a_non_html_listing_response_is_refused(app, db):
    responses.add(responses.GET, NEWS_URL, body=b"%PDF-1.7", content_type="application/pdf",
                  match=[matchers.query_string_matcher("")])

    result = scrape_vu_news()

    assert result["error"] == f"Failed to fetch {NEWS_URL}"


@responses.activate
def test_a_second_run_steps_aside_while_one_holds_the_lock(app, db):
    from app.scraper.vu_scraper import _RUN_LOCK

    assert _RUN_LOCK.acquire(blocking=False)
    try:
        result = scrape_vu_news()
    finally:
        _RUN_LOCK.release()

    assert result == {"found": 0, "new": 0, "skipped": True}
    assert len(responses.calls) == 0


@responses.activate
def test_an_unexpected_error_rolls_back_and_marks_the_run_failed(app, db, monkeypatch):
    def _boom(_db):
        raise RuntimeError("tombstone table exploded")

    monkeypatch.setattr("app.scraper.vu_scraper.load_deleted_urls", _boom)
    _one_article_site()

    result = scrape_vu_news()

    assert result["error"] == "tombstone table exploded"
    run = _run(db, result["runId"])
    assert run["status"] == "failed"
    assert run["error_message"] == "tombstone table exploded"
    assert _rows(db) == []


@responses.activate
def test_the_lock_is_released_after_a_failed_run(app, db, monkeypatch):
    def _boom(_db):
        raise RuntimeError("tombstone table exploded")

    monkeypatch.setattr("app.scraper.vu_scraper.load_deleted_urls", _boom)
    scrape_vu_news()

    monkeypatch.undo()
    _one_article_site()

    assert scrape_vu_news()["new"] == 1


@responses.activate
def test_a_rollback_that_itself_fails_does_not_escape_the_scraper(app, db, monkeypatch, caplog):
    # The connection the run was using is exactly the thing that
    # broke: its rollback raises too, and mark_run_failed still
    # has to close the row on a fresh connection
    real_get_db = common.get_db

    class _BrokenRollback:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def rollback(self):
            raise sqlite3.OperationalError("rollback refused")

    monkeypatch.setattr("app.scraper.vu_scraper.get_db", lambda: _BrokenRollback(real_get_db()))
    monkeypatch.setattr("app.scraper.vu_scraper.load_deleted_urls",
                        lambda _db: (_ for _ in ()).throw(RuntimeError("boom")))

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["error"] == "boom"
    assert "Rollback after the vu.lt failure did not take" in caplog.text
    assert _run(db, result["runId"])["status"] == "failed"








# -----------------------------------------------------------
# scrape_vu_news — run bookkeeping
# -----------------------------------------------------------

@responses.activate
def test_run_rows_older_than_the_retention_window_are_pruned(app, db):
    with time_machine.travel("2026-08-29 12:00:00 +0000", tick=False):
        stale_vu = _seed_run(db, "vu.lt", days_ago=40)
        stale_knf = _seed_run(db, "knf.vu.lt", days_ago=40)
        _one_article_site()

        result = scrape_vu_news()

    assert _run(db, stale_vu) is None
    # The newest row of every source survives whatever its age —
    # a scraper that stopped months ago must stay on /status
    assert _run(db, stale_knf) is not None
    assert _run(db, result["runId"]) is not None


@responses.activate
def test_a_collapsed_yield_gets_its_own_error_line(app, db, caplog):
    _seed_run(db, "vu.lt", found=100)
    _one_article_site()

    with caplog.at_level(logging.ERROR, logger="app.scraper.common"):
        result = scrape_vu_news()

    assert result["new"] == 1
    assert "yield collapsed" in caplog.text


@responses.activate
def test_a_steady_yield_is_not_reported_as_a_collapse(app, db, caplog):
    _seed_run(db, "vu.lt", found=2)
    _one_article_site()

    with caplog.at_level(logging.ERROR, logger="app.scraper.common"):
        scrape_vu_news()

    assert "yield collapsed" not in caplog.text








# -----------------------------------------------------------
# scrape_vu_news — the one push per run
# -----------------------------------------------------------

@responses.activate
def test_a_single_new_article_pushes_the_singular_copy(app, db, push_spy):
    _seed_run(db)
    _one_article_site()

    scrape_vu_news()

    assert len(push_spy) == 1
    assert push_spy[0]["channel"] == "news"
    assert push_spy[0]["title"] == "VU naujienos"
    assert push_spy[0]["body"] == "Naujas straipsnis iš vu.lt"
    assert push_spy[0]["title_en"] == "VU news"
    assert push_spy[0]["body_en"] == "New article from vu.lt"
    assert push_spy[0]["data"] == {"type": "news", "source": "vu.lt"}


@responses.activate
def test_a_batch_pushes_the_declined_lithuanian_plural(app, db, push_spy):
    _seed_run(db)
    cards = []
    for i in range(3):
        slug = f"a{i}"
        cards.append(_card(f"/naujienos/{slug}", f"Straipsnis numeris {i} apie mokslą"))
        _article(slug, _article_page(title=f"Straipsnis numeris {i} apie mokslą"))
    _listing(1, _listing_page(*cards))

    scrape_vu_news()

    assert len(push_spy) == 1
    assert push_spy[0]["title"] == "VU naujienos (3)"
    assert push_spy[0]["body"] == "3 nauji straipsniai iš vu.lt"
    assert push_spy[0]["body_en"] == "3 new articles from vu.lt"


@responses.activate
def test_a_hand_fired_run_never_wakes_a_device(app, db, push_spy):
    _seed_run(db)
    _one_article_site()

    scrape_vu_news(notify=False)

    assert push_spy == []


@responses.activate
def test_the_first_completed_run_of_the_source_is_a_backfill_and_does_not_push(app, db, push_spy):
    _one_article_site()

    scrape_vu_news()

    assert push_spy == []


@responses.activate
def test_a_run_that_inserted_nothing_does_not_push(app, db, push_spy):
    _seed_run(db)
    _one_article_site()
    scrape_vu_news(notify=False)

    scrape_vu_news()

    assert push_spy == []


@responses.activate
def test_a_second_run_within_the_hour_does_not_push_again(app, db, push_spy):
    _seed_run(db)
    _listing(1, _listing_page(_card("/naujienos/aa", "Pirmas straipsnis apie mokslą")))
    _article("aa", _article_page(title="Pirmas straipsnis apie mokslą"))
    scrape_vu_news()

    responses.reset()
    _listing(1, _listing_page(_card("/naujienos/bb", "Antras straipsnis apie studijas")))
    _article("bb", _article_page(title="Antras straipsnis apie studijas"))
    scrape_vu_news()

    assert len(push_spy) == 1


@responses.activate
def test_a_push_failure_does_not_fail_the_run(app, db, monkeypatch, caplog):
    def _explode(*args, **kwargs):
        raise RuntimeError("expo is down")

    monkeypatch.setattr("app.notifications.push.notify_channel", _explode)
    _seed_run(db)
    _one_article_site()

    with caplog.at_level(logging.ERROR, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["new"] == 1
    assert "error" not in result
    assert _run(db, result["runId"])["status"] == "completed"
    assert "Failed to send push notification" in caplog.text








# -----------------------------------------------------------
# The admin routes that fire and report the scrape
# -----------------------------------------------------------

def test_the_status_route_refuses_a_guest(client):
    assert client.get("/api/scraper/status").status_code == 401


def test_the_status_route_refuses_a_student(client, actor):
    _user, headers = actor

    assert client.get("/api/scraper/status", headers=headers).status_code == 403


def test_the_trigger_route_refuses_a_guest_and_a_student(client, actor):
    _user, headers = actor

    assert client.post("/api/scraper/trigger").status_code == 401
    assert client.post("/api/scraper/run", headers=headers).status_code == 403


@responses.activate
def test_an_admin_sees_the_finished_vu_run_on_the_status_page(client, admin, db):
    _one_article_site()
    scrape_vu_news()
    _user, headers = admin

    response = client.get("/api/scraper/status?source=vu.lt", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert [r["source"] for r in body["runs"]] == ["vu.lt"]
    assert body["runs"][0]["itemsNew"] == 1
    assert body["sources"][0]["lastSuccess"]["articlesFound"] == 1
    assert body["sources"][0]["lastFailure"] is None


@responses.activate
@pytest.mark.contract
def test_the_admin_trigger_runs_the_vu_scrape_without_pushing(client, admin, db, monkeypatch, push_spy):
    monkeypatch.setattr("app.scraper.routes.scrape_knf_news",
                        lambda pages=2, notify=True: {"found": 0, "new": 0, "runId": "knf-run"})
    _seed_run(db)
    _one_article_site()
    _user, headers = admin

    response = client.post("/api/scraper/trigger", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"knf", "vu"}
    assert body["vu"]["found"] == 1
    assert body["vu"]["new"] == 1
    assert body["vu"]["runId"]
    assert push_spy == []


@responses.activate
def test_a_failed_vu_scrape_answers_502_with_a_stable_slug(client, admin, monkeypatch):
    monkeypatch.setattr("app.scraper.routes.scrape_knf_news",
                        lambda pages=2, notify=True: {"found": 0, "new": 0})
    _user, headers = admin

    response = client.post("/api/scraper/run", headers=headers)

    assert response.status_code == 502
    assert response.get_json()["vu"]["error"] == "scrape_failed"


@responses.activate
def test_a_vu_scrape_that_is_already_running_answers_409(client, admin, monkeypatch):
    from app.scraper.vu_scraper import _RUN_LOCK

    monkeypatch.setattr("app.scraper.routes.scrape_knf_news",
                        lambda pages=2, notify=True: {"found": 0, "new": 0})
    _user, headers = admin

    assert _RUN_LOCK.acquire(blocking=False)
    try:
        response = client.post("/api/scraper/trigger", headers=headers)
    finally:
        _RUN_LOCK.release()

    assert response.status_code == 409
    assert response.get_json()["vu"]["skipped"] is True
