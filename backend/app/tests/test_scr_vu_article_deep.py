# -----------------------------------------------------------
#  [*] Tests — vu_scraper, the article-parse and storage half
#
#  The gap-closing pass over ONE slice of
#  scraper/vu_scraper.py: everything that happens AFTER a
#  listing link has been harvested. The link-harvesting half
#  (_listing_links, _is_article_href, the page walk) belongs
#  to another file; what this one owns is
#
#    - _fetch_vu_article — the one None exit, the <h1>
#      ladder, the content-region ladder and the decompose()
#      that MUTATES the shared soup out from under the image
#      and date lookups, og:image against the body <img>
#      loop, the article:published_time / <time> ladder, and
#      the three stored-length caps
#    - _article_summary — og:description, the plain meta
#      description, the 40-character floor that rejects
#      both, the body-minus-leading-chrome fallback and the
#      200-character cut back to a word boundary
#    - _looks_like_chrome — the length floor, the repeated
#      title and the two date shapes, at their boundaries
#    - scrape_vu_news STEP 4-6 — the ONE short write
#      transaction: the title ladder, the two defensive
#      guards over an untitled article, the source-scoped
#      title dedupe, the INSERT OR IGNORE backstop, the run
#      row that closes with the counts, the push copy in
#      both languages, and the failure path that rolls a
#      half-written batch back before recording the run as
#      failed
#
#  Boundaries are pinned on both sides wherever the module
#  has one (39/40 characters of chrome, 79/80 of a date line,
#  200/201 of a summary, the five-year published_at clamp,
#  MAX_TITLE_LENGTH/MAX_CONTENT_LENGTH/MAX_SUMMARY_LENGTH).
#
#  Every fetch is faked with `responses` against HTML
#  authored here — the container runs --network none, so a
#  test that reaches vu.lt fails by construction — and every
#  clock is pinned with time_machine.
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
from app.scraper.common import (
    MAX_ARTICLE_AGE_DAYS,
    MAX_CONTENT_LENGTH,
    MAX_IMAGE_URL_LENGTH,
    MAX_SUMMARY_LENGTH,
    MAX_TITLE_LENGTH,
)
from app.scraper.vu_scraper import (
    NEWS_URL,
    _article_summary,
    _fetch_vu_article,
    _looks_like_chrome,
    scrape_vu_news,
)


# Article URLs are fetched in normalise_url shape, which has
# already dropped the "www." the listing links carry
ARTICLE_HOST = "https://vu.lt"

# The shared cover host: in IMAGE_HOSTS, in neither page
# allowlist — exactly where a real vu.lt cover lives
COVER = "https://newshub.vu.lt/media/cover.jpg"

NOW = "2026-08-29 12:00:00 +0000"
NOW_NAIVE = datetime(2026, 8, 29, 12, 0, 0)




# -----------------------------------------------------------
# _line
# -----------------------------------------------------------
#
# A plain line of EXACTLY n characters: real words separated
# by spaces, no digits, no trailing space and never equal to
# a title under test — so the only thing that can decide
# _looks_like_chrome about it is its length.
# -----------------------------------------------------------

def _line(n, word="zodis"):
    out = word
    while len(out) < n:
        out += " " + word
    return out[:n].rstrip().ljust(n, "z")




# -----------------------------------------------------------
# _soup
# -----------------------------------------------------------
#
# _article_summary reads only the two description metas off
# the soup it is handed, so its unit tests build the smallest
# document that carries them.
# -----------------------------------------------------------

def _soup(head=""):
    return BeautifulSoup(f"<!doctype html><html><head>{head}</head><body></body></html>", "lxml")




# -----------------------------------------------------------
# _page
# -----------------------------------------------------------
#
# One article page, every part of it optional so a single
# helper can produce the page each branch needs. `head_extra`
# and `body` take raw markup for the awkward shapes (two
# metas, a decomposed region, a container the ladder must
# miss) that no keyword could express.
# -----------------------------------------------------------

def _page(title="Nauja laboratorija Kauno fakultete",
          paragraphs=("Vilniaus universiteto Kauno fakultetas atidaro nauja duomenu laboratorija.",),
          published="2026-08-20T09:30:00+03:00",
          og_image=None,
          og_description=None,
          meta_description=None,
          images=(),
          time_datetime=None,
          time_text=None,
          region="article",
          head_extra="",
          inner_extra=""):

    head = "<meta charset='utf-8'>"
    if published is not None:
        head += f"<meta property='article:published_time' content='{published}'>"
    if og_image is not None:
        head += f"<meta property='og:image' content='{og_image}'>"
    if og_description is not None:
        head += f"<meta property='og:description' content='{og_description}'>"
    if meta_description is not None:
        head += f"<meta name='description' content='{meta_description}'>"
    head += head_extra

    stamp = ""
    if time_datetime is not None:
        stamp = f"<time datetime='{time_datetime}'>{time_text or ''}</time>"
    elif time_text is not None:
        stamp = f"<time>{time_text}</time>"

    inner = (f"<h1>{title}</h1>" if title is not None else "") + stamp
    inner += "".join(f'<img src="{src}" alt="">' for src in images)
    inner += "".join(f"<p>{p}</p>" for p in paragraphs)
    inner += inner_extra

    if region == "article":
        markup = f"<article>{inner}</article>"
    elif region == "content":
        markup = f"<div class='page-content'>{inner}</div>"
    elif region == "main":
        markup = f"<main>{inner}</main>"
    elif region == "raw":
        markup = inner
    else:
        markup = f"<div class='wrap'>{inner}</div>"

    return f"<!doctype html><html lang='lt'><head>{head}</head><body>{markup}</body></html>"




# -----------------------------------------------------------
# _card / _listing_html / _listing / _article
# -----------------------------------------------------------
#
# Registering the fake site. Listing page 1 is the bare URL
# and later pages carry ?page=N, which is how the scraper
# walks them; a page nobody registers answers with a
# ConnectionError, this suite's "the site is down".
# -----------------------------------------------------------

def _card(href, title):
    return f'<div class="card"><a href="{href}"><img src="/media/thumb.jpg" alt=""></a><a href="{href}">{title}</a></div>'


def _listing_html(*cards):
    return f"<!doctype html><html lang='lt'><body><main>{''.join(cards)}</main></body></html>"


def _listing(page, html, status=200):
    match = ([matchers.query_string_matcher("")] if page == 1
             else [matchers.query_param_matcher({"page": str(page)})])
    responses.add(responses.GET, NEWS_URL, body=html, status=status,
                  content_type="text/html", match=match)


def _article(slug, html, status=200, headers=None, content_type="text/html"):
    responses.add(responses.GET, f"{ARTICLE_HOST}/naujienos/{slug}", body=html,
                  status=status, content_type=content_type, headers=headers)




# -----------------------------------------------------------
# _site
# -----------------------------------------------------------
#
# The smallest complete site: one listing card and the
# article behind it. Returns the canonical URL the stored row
# is expected to carry.
# -----------------------------------------------------------

def _site(slug="laboratorija", title="Nauja laboratorija Kauno fakultete", **page):
    _listing(1, _listing_html(_card(f"/naujienos/{slug}", title)))
    _article(slug, _page(title=title, **page))
    return f"{ARTICLE_HOST}/naujienos/{slug}"




# -----------------------------------------------------------
# _rows / _run
# -----------------------------------------------------------

def _rows(db):
    return db.execute(
        "SELECT * FROM news_posts WHERE source = 'vu.lt' ORDER BY source_url"
    ).fetchall()


def _run(db, run_id):
    return db.execute("SELECT * FROM scraper_runs WHERE id = ?", (run_id,)).fetchone()




# -----------------------------------------------------------
# _earlier_run
# -----------------------------------------------------------
#
# push_allowed calls a source's FIRST completed run a
# backfill and refuses to wake a device for it, so every push
# test needs one of these behind it.
# -----------------------------------------------------------

def _earlier_run(db, source="vu.lt", found=5, days_ago=1):
    started = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    run_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO scraper_runs (id, source, status, articles_found, articles_new, started_at, finished_at)"
        " VALUES (?, ?, 'completed', ?, 0, ?, ?)",
        (run_id, source, found, started, started),
    )
    db.commit()
    return run_id




# -----------------------------------------------------------
# _untitled_link
# -----------------------------------------------------------
#
# The two storage guards over an EMPTY title are defensive:
# _listing_links refuses any anchor whose label is 10
# characters or shorter, so no listing page can hand the
# writer a title-less link today. Stubbing the harvester is
# the only way to prove the guards still hold if that floor
# ever moves — the writer is what is under test here, not the
# harvester.
# -----------------------------------------------------------

def _untitled_link(monkeypatch, slug="be-pavadinimo"):
    monkeypatch.setattr(
        "app.scraper.vu_scraper._listing_links",
        lambda soup: [{"href": f"/naujienos/{slug}", "title": ""}],
    )
    _listing(1, _listing_html("<p>nesvarbu</p>"))
    return f"{ARTICLE_HOST}/naujienos/{slug}"




# -----------------------------------------------------------
# _forget_recent_pushes
# -----------------------------------------------------------
#
# common._LAST_PUSH is per PROCESS: one test that pushed
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
# Records notify_channel calls instead of fanning out to
# Expo. The scraper imports it lazily inside the run, so
# patching the module attribute is what the call resolves to.
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
# _looks_like_chrome — the length floor
# -----------------------------------------------------------

def test_an_empty_line_is_chrome():
    assert _looks_like_chrome("", "Pavadinimas") is True


def test_a_whitespace_only_line_is_chrome():
    assert _looks_like_chrome("        ", "Pavadinimas") is True


def test_a_thirty_nine_character_line_is_chrome():
    line = _line(39)
    assert len(line) == 39
    assert _looks_like_chrome(line, "Pavadinimas") is True


def test_a_forty_character_line_clears_the_floor():
    line = _line(40)
    assert len(line) == 40
    assert _looks_like_chrome(line, "Pavadinimas") is False


def test_a_very_long_paragraph_is_not_chrome():
    assert _looks_like_chrome(_line(400), "Pavadinimas") is False




# -----------------------------------------------------------
# _looks_like_chrome — the repeated title
# -----------------------------------------------------------

def test_the_title_repeated_verbatim_is_chrome():
    title = _line(50)
    assert _looks_like_chrome(title, title) is True


def test_the_title_repeated_in_another_case_is_chrome():
    title = _line(50)
    assert _looks_like_chrome(title.upper(), title) is True


def test_the_title_rule_folds_lithuanian_case_too():
    title = "Sveikiname Kauno fakulteto absolventus su diplomais šiandien"
    assert _looks_like_chrome(title.casefold(), title.upper()) is True


def test_a_line_the_title_only_prefixes_is_not_chrome():
    title = _line(50)
    assert _looks_like_chrome(title + " ir dar sakinio galas", title) is False


def test_an_empty_title_never_makes_a_line_chrome():
    assert _looks_like_chrome(_line(50), "") is False


def test_a_none_title_never_makes_a_line_chrome():
    assert _looks_like_chrome(_line(50), None) is False




# -----------------------------------------------------------
# _looks_like_chrome — the two date shapes
# -----------------------------------------------------------

@pytest.mark.parametrize("separator", ["-", ".", "/", " "])
def test_every_leading_iso_date_separator_makes_a_short_line_chrome(separator):
    line = f"2026{separator}08{separator}29 " + _line(30)
    assert 40 <= len(line) < 80
    assert _looks_like_chrome(line, "Pavadinimas") is True


def test_a_lithuanian_date_line_anywhere_in_a_short_line_is_chrome():
    line = "Paskelbta 2026 m. rugpjucio 29 d. Kauno fakultete"
    assert 40 <= len(line) < 80
    assert _looks_like_chrome(line, "Pavadinimas") is True


def test_the_year_and_m_need_no_space_between_them():
    line = "Paskelbta 2026m. rugpjucio 29 d. Kauno fakultetas"
    assert 40 <= len(line) < 80
    assert _looks_like_chrome(line, "Pavadinimas") is True


def test_a_seventy_nine_character_date_line_is_still_chrome():
    line = ("2026-08-29 " + _line(80))[:79]
    assert len(line) == 79
    assert _looks_like_chrome(line, "Pavadinimas") is True


def test_an_eighty_character_date_line_is_a_paragraph_again():
    line = ("2026-08-29 " + _line(120))[:80]
    assert len(line) == 80
    assert _looks_like_chrome(line, "Pavadinimas") is False


def test_a_year_mentioned_mid_sentence_is_not_a_date_line():
    line = "Universitetas 2026 metais atidarys nauja laboratorija"
    assert 40 <= len(line) < 80
    assert _looks_like_chrome(line, "Pavadinimas") is False


def test_a_leading_number_that_is_not_four_digits_is_not_a_date_line():
    line = "202-08-29 diena kai laboratorija buvo atidaryta"
    assert 40 <= len(line) < 80
    assert _looks_like_chrome(line, "Pavadinimas") is False


def test_five_leading_digits_are_not_a_date_line():
    line = "20260829 diena kai laboratorija buvo atidaryta xy"
    assert 40 <= len(line) < 80
    assert _looks_like_chrome(line, "Pavadinimas") is False


def test_a_date_line_that_is_also_the_title_is_chrome_by_either_rule():
    line = "2026-08-29 " + _line(40)
    assert _looks_like_chrome(line, line) is True








# -----------------------------------------------------------
# _article_summary — the page's own description
# -----------------------------------------------------------

def test_the_og_description_is_returned_verbatim():
    described = _line(90)
    soup = _soup(f"<meta property='og:description' content='{described}'>")

    assert _article_summary(soup, _line(300), "Pavadinimas") == described


def test_a_description_of_exactly_forty_characters_is_accepted():
    described = _line(40)
    soup = _soup(f"<meta property='og:description' content='{described}'>")

    assert _article_summary(soup, _line(300), "Pavadinimas") == described


def test_a_description_of_thirty_nine_characters_is_refused():
    described = _line(39)
    body = _line(120)
    soup = _soup(f"<meta property='og:description' content='{described}'>")

    assert _article_summary(soup, body, "Pavadinimas") == body


def test_a_short_og_description_falls_through_to_the_plain_meta_one():
    plain = _line(60)
    soup = _soup(f"<meta property='og:description' content='{_line(20)}'>"
                 f"<meta name='description' content='{plain}'>")

    assert _article_summary(soup, _line(300), "Pavadinimas") == plain


def test_the_og_description_wins_when_both_metas_qualify():
    og = _line(45)
    soup = _soup(f"<meta property='og:description' content='{og}'>"
                 f"<meta name='description' content='{_line(90)}'>")

    assert _article_summary(soup, _line(300), "Pavadinimas") == og


def test_the_plain_meta_description_is_used_when_there_is_no_og_one():
    plain = _line(70)
    soup = _soup(f"<meta name='description' content='{plain}'>")

    assert _article_summary(soup, _line(300), "Pavadinimas") == plain


def test_a_description_meta_with_no_content_attribute_is_ignored():
    body = _line(120)
    soup = _soup("<meta property='og:description'><meta name='description'>")

    assert _article_summary(soup, body, "Pavadinimas") == body


def test_an_empty_description_meta_is_ignored():
    body = _line(120)
    soup = _soup("<meta property='og:description' content=''>"
                 "<meta name='description' content=''>")

    assert _article_summary(soup, body, "Pavadinimas") == body


def test_a_description_is_stripped_before_the_length_test():
    described = _line(45)
    soup = _soup(f"<meta property='og:description' content='   {described}   '>")

    assert _article_summary(soup, _line(300), "Pavadinimas") == described


def test_padding_cannot_lift_a_short_description_over_the_floor():
    body = _line(120)
    soup = _soup(f"<meta property='og:description' content='{'  ' * 20}{_line(30)}'>")

    assert _article_summary(soup, body, "Pavadinimas") == body


def test_a_long_description_is_not_cut_here():
    described = _line(400)
    soup = _soup(f"<meta property='og:description' content='{described}'>")

    # The 500-character cap is the caller's; this helper hands
    # back whatever the editor wrote
    assert _article_summary(soup, "", "Pavadinimas") == described




# -----------------------------------------------------------
# _article_summary — the body minus its leading chrome
# -----------------------------------------------------------

def test_the_leading_chrome_lines_are_dropped():
    first = _line(60)
    content = "\n".join(["Pradzia", "Naujienos", "Dalintis", first])

    assert _article_summary(_soup(), content, "Pavadinimas") == first


def test_the_repeated_title_is_dropped_from_the_front_of_the_body():
    title = _line(60)
    first = _line(70, word="tekstas")
    content = "\n".join([title, first])

    assert _article_summary(_soup(), content, title) == first


def test_a_leading_date_line_is_dropped_from_the_body():
    first = _line(70, word="tekstas")
    content = "\n".join(["2026-08-29 " + _line(35), first])

    assert _article_summary(_soup(), content, "Pavadinimas") == first


def test_a_short_line_after_the_body_started_is_kept():
    first = _line(50)
    content = "\n".join([first, "Dalintis", _line(45, word="pabaiga")])
    summary = _article_summary(_soup(), content, "Pavadinimas")

    assert "Dalintis" in summary
    assert summary.startswith(first)


def test_an_empty_line_after_the_body_started_is_skipped_without_doubling_a_space():
    first = _line(50)
    last = _line(60, word="pabaiga")
    content = "\n".join([first, "", "   ", last])
    summary = _article_summary(_soup(), content, "Pavadinimas")

    assert summary == f"{first} {last}"
    assert "  " not in summary


def test_carriage_returns_are_stripped_off_the_body_lines():
    first = _line(50)
    last = _line(60, word="pabaiga")
    summary = _article_summary(_soup(), f"{first}\r\n{last}\r\n", "Pavadinimas")

    assert summary == f"{first} {last}"


def test_a_body_that_is_nothing_but_chrome_falls_back_to_its_raw_text():
    content = "Pradzia\nNaujienos\nDalintis"

    # Every line was skipped, so the join is empty and the raw
    # body — newlines and all — is what the reader gets
    assert _article_summary(_soup(), content, "Pavadinimas") == content


def test_an_empty_body_yields_an_empty_summary():
    assert _article_summary(_soup(), "", "Pavadinimas") == ""


def test_a_whitespace_only_body_yields_an_empty_summary():
    assert _article_summary(_soup(), "   \n  \n ", "Pavadinimas") == ""


def test_a_body_summary_ignores_the_title_rule_when_there_is_no_title():
    first = _line(60)
    assert _article_summary(_soup(), first, "") == first




# -----------------------------------------------------------
# _article_summary — the 200-character cut
# -----------------------------------------------------------

def test_a_two_hundred_character_summary_is_returned_whole():
    content = _line(200)
    summary = _article_summary(_soup(), content, "Pavadinimas")

    assert summary == content
    assert not summary.endswith("...")


def test_a_two_hundred_and_one_character_summary_is_cut_back_to_a_word():
    content = _line(201)
    summary = _article_summary(_soup(), content, "Pavadinimas")

    assert summary.endswith("...")
    assert len(summary) <= 203
    assert content.startswith(summary[:-3])
    assert not summary[:-3].endswith(" ")


def test_a_single_word_longer_than_the_cut_is_taken_whole_to_the_cut():
    content = "a" * 250
    summary = _article_summary(_soup(), content, "Pavadinimas")

    # rsplit finds no space to cut back to, so the whole first
    # 200 characters survive
    assert summary == "a" * 200 + "..."


def test_a_cut_landing_exactly_on_a_space_drops_the_trailing_blank():
    content = "b" * 199 + " " + "c" * 60
    summary = _article_summary(_soup(), content, "Pavadinimas")

    assert summary == "b" * 199 + "..."


def test_the_cut_is_applied_to_the_joined_body_not_to_one_line():
    content = "\n".join([_line(120), _line(120, word="antras")])
    summary = _article_summary(_soup(), content, "Pavadinimas")

    assert summary.endswith("...")
    assert summary.startswith(_line(120)[:50])








# -----------------------------------------------------------
# _fetch_vu_article — the one None exit
# -----------------------------------------------------------

@responses.activate
def test_a_dead_article_page_comes_back_as_none(app):
    _article("aa", "", status=404)

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa") is None


@responses.activate
def test_an_unreachable_article_page_comes_back_as_none(app):
    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa") is None


@responses.activate
def test_an_off_allowlist_article_url_is_never_even_requested(app):
    assert _fetch_vu_article("https://evil.example/naujienos/aa") is None
    assert len(responses.calls) == 0


@responses.activate
def test_a_non_http_article_url_is_refused(app):
    assert _fetch_vu_article("file:///etc/passwd") is None
    assert len(responses.calls) == 0


@responses.activate
def test_an_empty_article_url_is_refused(app):
    assert _fetch_vu_article("") is None
    assert len(responses.calls) == 0


@responses.activate
def test_a_pdf_behind_an_article_link_is_not_parsed_as_a_page(app):
    _article("aa", b"%PDF-1.7", content_type="application/pdf")

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa") is None


@responses.activate
def test_a_redirect_off_the_allowlist_comes_back_as_none(app):
    _article("aa", "", status=302, headers={"Location": "https://evil.example/x"})
    responses.add(responses.GET, "https://evil.example/x", body="<html></html>",
                  content_type="text/html")

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa") is None


@responses.activate
def test_a_page_that_parses_to_nothing_is_still_not_none(app):
    # The contract the caller leans on: only a FAILED fetch is
    # None, so an unparsable page can still fall back to the
    # listing headline instead of being skipped forever
    _article("aa", "<html><body></body></html>")

    data = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")

    assert data is not None
    assert data["title"] == ""
    assert data["content"] == ""




# -----------------------------------------------------------
# _fetch_vu_article — the title ladder
# -----------------------------------------------------------

@responses.activate
def test_the_first_h1_on_the_page_is_the_title(app):
    _article("aa", _page(title="Pirmas", inner_extra="<h1>Antras</h1>"))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"] == "Pirmas"


@responses.activate
def test_an_h1_outside_every_content_region_still_supplies_the_title(app):
    _article("aa", "<html><body><h1>Antraste</h1><div>tekstas</div></body></html>")

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"] == "Antraste"


@responses.activate
def test_an_empty_h1_hands_the_ladder_on_to_the_real_headline(app):
    # bs4 tags are truthy whatever they contain, so the empty
    # <h1> used to BREAK the loop; the text is what ends it now,
    # which is the only way the three narrower selectors below
    # "h1" ever get a turn
    _article("aa", "<html><body><h1></h1><article><h1>Tikroji antraste</h1></article></body></html>")

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"] == "Tikroji antraste"


@responses.activate
def test_an_h1_of_only_whitespace_yields_an_empty_title(app):
    _article("aa", "<html><body><h1>   </h1></body></html>")

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"] == ""


@responses.activate
def test_a_page_with_no_h1_at_all_yields_an_empty_title(app):
    _article("aa", _page(title=None))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"] == ""


@responses.activate
def test_the_title_is_joined_out_of_the_h1_children(app):
    _article("aa", "<html><body><h1>Nauja <em>laboratorija</em></h1></body></html>")

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"] == "Naujalaboratorija"


@responses.activate
def test_a_title_over_the_stored_cap_is_cut(app):
    _article("aa", _page(title="T" * (MAX_TITLE_LENGTH + 60)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"] == "T" * MAX_TITLE_LENGTH


@responses.activate
def test_a_title_exactly_at_the_cap_survives_whole(app):
    _article("aa", _page(title="T" * MAX_TITLE_LENGTH))

    assert len(_fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["title"]) == MAX_TITLE_LENGTH




# -----------------------------------------------------------
# _fetch_vu_article — the content-region ladder
# -----------------------------------------------------------

@responses.activate
def test_the_article_element_is_the_first_content_region(app):
    _article("aa", _page(paragraphs=("Pirma pastraipa.",), region="article"))

    assert "Pirma pastraipa." in _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]


@responses.activate
def test_a_class_named_content_is_the_next_region(app):
    _article("aa", _page(paragraphs=("Antra pastraipa.",), region="content"))

    assert "Antra pastraipa." in _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]


@responses.activate
def test_main_is_the_last_region_tried(app):
    _article("aa", _page(paragraphs=("Trecia pastraipa.",), region="main"))

    assert "Trecia pastraipa." in _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]


@responses.activate
def test_a_page_with_no_recognisable_region_yields_no_content(app):
    _article("aa", _page(paragraphs=("Niekur nerasta.",), region="none"))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"] == ""


@responses.activate
def test_an_empty_article_element_hands_the_ladder_on_to_main(app):
    # The same bs4 truthiness as the <h1> ladder: an empty
    # <article> matched and the <main> below it never got a
    # turn, so the row was stored with an empty body
    _article("aa", "<html><body><article></article><main><p>Tekstas kuris bus rastas.</p></main></body></html>")

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"] == "Tekstas kuris bus rastas."


@responses.activate
def test_the_earlier_region_wins_when_the_page_has_several(app):
    html = ("<html><body><article><p>Is article elemento.</p></article>"
            "<div class='page-content'><p>Is content divo.</p></div>"
            "<main><p>Is main.</p></main></body></html>")
    _article("aa", html)

    content = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]

    assert content == "Is article elemento."


@responses.activate
@pytest.mark.parametrize("tag", ["script", "style", "nav", "header", "footer", "aside"])
def test_every_chrome_tag_is_decomposed_out_of_the_content(app, tag):
    html = (f"<html><body><article><{tag}>SIUKSLES</{tag}>"
            "<p>Tikras tekstas apie laboratorija.</p></article></body></html>")
    _article("aa", html)

    content = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]

    assert "SIUKSLES" not in content
    assert "Tikras tekstas apie laboratorija." in content


@responses.activate
def test_chrome_outside_the_content_region_is_left_alone(app):
    html = ("<html><body><nav>MENIU</nav>"
            "<article><p>Tikras tekstas apie laboratorija.</p></article></body></html>")
    _article("aa", html)

    # The decompose only ever reaches inside the chosen region,
    # so the page's own navigation survives in the soup — it is
    # simply not part of the content
    assert "MENIU" not in _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]


@responses.activate
def test_the_content_lines_are_separated_by_newlines(app):
    _article("aa", _page(paragraphs=("Pirma.", "Antra.")))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"].endswith("Pirma.\nAntra.")


@responses.activate
def test_content_over_the_stored_cap_is_cut(app):
    _article("aa", _page(title=None, paragraphs=("c" * (MAX_CONTENT_LENGTH + 500),)))

    assert len(_fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"]) == MAX_CONTENT_LENGTH


@responses.activate
def test_content_exactly_at_the_cap_survives_whole(app):
    _article("aa", _page(title=None, paragraphs=("c" * MAX_CONTENT_LENGTH,)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["content"] == "c" * MAX_CONTENT_LENGTH




# -----------------------------------------------------------
# _fetch_vu_article — the decompose mutates the shared soup
# -----------------------------------------------------------

@responses.activate
def test_an_image_inside_the_decomposed_chrome_is_gone_by_the_image_step(app):
    html = ("<html><body><article>"
            '<aside><img src="/media/pirmas.jpg"></aside>'
            '<p>Tekstas.</p><img src="/media/antras.jpg">'
            "</article></body></html>")
    _article("aa", html)

    # The aside was decompose()d for the content, so its <img>
    # is no longer in the soup the image step walks
    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/antras.jpg"


@responses.activate
def test_the_only_image_being_chrome_leaves_the_row_without_one(app):
    html = ('<html><body><article><nav><img src="/media/vienintele.jpg"></nav>'
            "<p>Tekstas.</p></article></body></html>")
    _article("aa", html)

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] is None


@responses.activate
def test_a_time_inside_the_decomposed_chrome_is_gone_by_the_date_step(app):
    html = ('<html><body><article><footer><time datetime="2020-01-01T00:00:00+00:00">senas</time></footer>'
            "<p>Tekstas.</p></article></body></html>")
    _article("aa", html)

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == NOW_NAIVE.isoformat()




# -----------------------------------------------------------
# _fetch_vu_article — the image ladder
# -----------------------------------------------------------

@responses.activate
def test_the_og_image_wins_over_every_body_image(app):
    _article("aa", _page(og_image=COVER, images=["/media/kita.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == COVER


@responses.activate
def test_an_og_image_meta_without_a_content_attribute_falls_through(app):
    _article("aa", _page(head_extra="<meta property='og:image'>", images=["/media/kita.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/kita.jpg"


@responses.activate
def test_an_empty_og_image_falls_through(app):
    _article("aa", _page(og_image="", images=["/media/kita.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/kita.jpg"


@responses.activate
def test_a_blank_og_image_falls_through(app):
    _article("aa", _page(og_image="   ", images=["/media/kita.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/kita.jpg"


@responses.activate
def test_an_off_allowlist_og_image_falls_through(app):
    _article("aa", _page(og_image="https://evil.example/beacon.jpg", images=["/media/kita.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/kita.jpg"


@responses.activate
def test_a_data_uri_og_image_falls_through(app):
    _article("aa", _page(og_image="data:image/gif;base64,R0lGODlhAQ", images=["/media/kita.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/kita.jpg"


@responses.activate
def test_a_protocol_relative_og_image_is_resolved_against_the_page(app):
    _article("aa", _page(og_image="//newshub.vu.lt/media/cover.jpg"))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == COVER


@responses.activate
def test_an_og_image_over_the_length_cap_falls_through(app):
    huge = f"{ARTICLE_HOST}/media/" + "a" * MAX_IMAGE_URL_LENGTH + ".jpg"
    _article("aa", _page(og_image=huge, images=["/media/kita.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/kita.jpg"


@responses.activate
@pytest.mark.parametrize("src", [
    "/media/logo.png", "/media/LOGO.png", "/media/icon-24.svg", "/media/ICON.svg",
    "/media/pixel.gif", "/media/tracking-beacon.gif", "/media/avatar-of-author.jpg",
])
def test_every_chrome_image_keyword_is_skipped(app, src):
    _article("aa", _page(images=[src, "/media/nuotrauka.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/nuotrauka.jpg"


@responses.activate
def test_an_off_allowlist_body_image_is_skipped_and_the_next_one_taken(app):
    _article("aa", _page(images=["https://evil.example/beacon.jpg", "/media/nuotrauka.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/nuotrauka.jpg"


@responses.activate
def test_a_blank_body_image_src_is_skipped_and_the_next_one_taken(app):
    _article("aa", _page(images=[" ", "/media/nuotrauka.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/nuotrauka.jpg"


@responses.activate
def test_an_image_with_no_src_attribute_is_never_considered(app):
    _article("aa", _page(inner_extra='<img alt="be src"><img src="/media/nuotrauka.jpg">'))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/nuotrauka.jpg"


@responses.activate
def test_every_body_image_being_unusable_leaves_the_row_without_one(app):
    _article("aa", _page(images=["/media/logo.png", "https://evil.example/x.jpg", "data:image/gif;base64,AA"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] is None


@responses.activate
def test_a_page_with_no_images_at_all_leaves_the_row_without_one(app):
    _article("aa", _page())

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] is None


@responses.activate
def test_the_first_usable_body_image_wins(app):
    _article("aa", _page(images=["/media/pirma.jpg", "/media/antra.jpg"]))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["image_url"] == f"{ARTICLE_HOST}/media/pirma.jpg"


@responses.activate
def test_a_relative_image_is_resolved_against_the_post_redirect_url(app):
    _article("senas", "", status=301,
             headers={"Location": f"{ARTICLE_HOST}/naujienos/skyrius/naujas"})
    responses.add(responses.GET, f"{ARTICLE_HOST}/naujienos/skyrius/naujas",
                  body=_page(images=["nuotrauka.jpg"]), content_type="text/html")

    data = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/senas")

    # Resolved against where the page ACTUALLY came from, not
    # against BASE_URL
    assert data["image_url"] == f"{ARTICLE_HOST}/naujienos/skyrius/nuotrauka.jpg"




# -----------------------------------------------------------
# _fetch_vu_article — the date ladder
# -----------------------------------------------------------

@responses.activate
@pytest.mark.parametrize("published,expected", [
    ("2026-08-20T09:30:00+03:00", "2026-08-20T06:30:00"),
    ("2026-08-20T09:30:00-05:00", "2026-08-20T14:30:00"),
    ("2026-08-20T09:30:00Z", "2026-08-20T09:30:00"),
    ("2026-08-20T09:30:00", "2026-08-20T09:30:00"),
    ("2026-08-20", "2026-08-20T00:00:00"),
    ("2026-08-20 09:30:00", "2026-08-20T09:30:00"),
])
def test_the_published_time_meta_is_parsed_with_its_offset_applied(app, published, expected):
    _article("aa", _page(published=published))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == expected


@responses.activate
def test_the_first_published_time_meta_wins(app):
    _article("aa", _page(published="2026-08-20T09:30:00+00:00",
                         head_extra="<meta property='article:published_time' content='2026-08-01T00:00:00+00:00'>"))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-20T09:30:00"


@responses.activate
def test_a_first_published_time_meta_without_content_falls_to_the_second(app):
    # The loop used to break on the FIRST meta whatever it held,
    # discarding a perfectly good date below it; only a meta
    # CARRYING one ends the ladder now
    _article("aa", _page(published=None,
                         head_extra="<meta property='article:published_time'>"
                                    "<meta property='article:published_time' content='2026-08-01T00:00:00+00:00'>",
                         time_datetime="2026-08-20T09:30:00+00:00"))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-01T00:00:00"


@responses.activate
def test_an_empty_published_time_meta_falls_to_the_time_element(app):
    _article("aa", _page(published="", time_datetime="2026-08-20T09:30:00+00:00"))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-20T09:30:00"


@responses.activate
def test_the_time_datetime_attribute_is_preferred_over_its_text(app):
    _article("aa", _page(published=None, time_datetime="2026-08-20T09:30:00+00:00",
                         time_text="vakar"))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-20T09:30:00"


@responses.activate
def test_the_time_text_supplies_the_date_when_there_is_no_attribute(app):
    _article("aa", _page(published=None, time_text="2026-08-20"))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-20T00:00:00"


@responses.activate
def test_an_empty_time_element_falls_back_to_now(app):
    _article("aa", _page(published=None, time_datetime="", time_text=""))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == NOW_NAIVE.isoformat()


@responses.activate
def test_the_first_time_element_on_the_page_is_the_one_read(app):
    _article("aa", _page(published=None, time_datetime="2026-08-20T09:30:00+00:00",
                         inner_extra="<time datetime='2020-01-01T00:00:00+00:00'>senas</time>"))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == "2026-08-20T09:30:00"


@responses.activate
@pytest.mark.parametrize("value", ["vakar", "not-a-date", "2026-13-45", "0000-00-00", "??"])
def test_an_unparsable_date_falls_back_to_now(app, value):
    _article("aa", _page(published=value))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == NOW_NAIVE.isoformat()


@responses.activate
def test_a_page_with_no_date_anywhere_falls_back_to_now(app):
    _article("aa", _page(published=None))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == NOW_NAIVE.isoformat()


@responses.activate
def test_a_published_time_one_second_in_the_future_is_clamped_to_now(app):
    _article("aa", _page(published=(NOW_NAIVE + timedelta(seconds=1)).isoformat()))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == NOW_NAIVE.isoformat()


@responses.activate
def test_a_published_time_exactly_now_is_kept(app):
    _article("aa", _page(published=NOW_NAIVE.isoformat()))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == NOW_NAIVE.isoformat()


@responses.activate
def test_a_published_time_exactly_five_years_old_is_kept(app):
    edge = NOW_NAIVE - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    _article("aa", _page(published=edge.isoformat()))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == edge.isoformat()


@responses.activate
def test_a_published_time_a_second_past_five_years_is_clamped_to_now(app):
    stale = NOW_NAIVE - timedelta(days=MAX_ARTICLE_AGE_DAYS, seconds=1)
    _article("aa", _page(published=stale.isoformat()))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == NOW_NAIVE.isoformat()


@responses.activate
def test_a_timezone_offset_that_pushes_the_stamp_into_the_future_is_clamped(app):
    # 13:30 at -05:00 is 18:30 UTC, six hours after the frozen
    # now — the offset is applied first and the clamp catches it
    _article("aa", _page(published="2026-08-29T13:30:00-05:00"))

    with time_machine.travel(NOW, tick=False):
        assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["date"] == NOW_NAIVE.isoformat()




# -----------------------------------------------------------
# _fetch_vu_article — the returned shape
# -----------------------------------------------------------

@responses.activate
def test_the_parsed_article_carries_exactly_the_seven_stored_fields(app):
    _article("aa", _page())

    assert set(_fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")) == {
        "url", "title", "content", "summary", "image_url", "date", "author"}


@responses.activate
def test_the_author_is_always_the_university(app):
    _article("aa", _page(title=None, paragraphs=()))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["author"] == "Vilniaus universitetas"


@responses.activate
def test_the_returned_url_is_the_canonical_post_redirect_one(app):
    _article("senas", "", status=301,
             headers={"Location": "https://www.vu.lt/naujienos/naujas/"})
    responses.add(responses.GET, "https://www.vu.lt/naujienos/naujas/",
                  body=_page(), content_type="text/html")

    # www stripped, trailing slash dropped, https forced — the
    # exact key the caller stores and dedupes on
    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/senas")["url"] == f"{ARTICLE_HOST}/naujienos/naujas"


@responses.activate
def test_a_campaign_tag_is_stripped_off_the_returned_url(app):
    responses.add(responses.GET, f"{ARTICLE_HOST}/naujienos/aa", body=_page(),
                  content_type="text/html")

    data = _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa?utm_source=facebook&id=7")

    assert data["url"] == f"{ARTICLE_HOST}/naujienos/aa?id=7"


@responses.activate
def test_the_summary_is_cut_to_the_stored_limit(app):
    _article("aa", _page(og_description="s" * (MAX_SUMMARY_LENGTH + 300)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["summary"] == "s" * MAX_SUMMARY_LENGTH


@responses.activate
def test_the_summary_comes_from_the_body_when_the_page_has_no_description(app):
    paragraph = _line(120)
    _article("aa", _page(title="Antraste", paragraphs=(paragraph,)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["summary"] == paragraph


@responses.activate
def test_the_h1_repeated_at_the_top_of_the_body_is_kept_out_of_the_summary(app):
    title = _line(60)
    paragraph = _line(120, word="tekstas")
    _article("aa", _page(title=title, paragraphs=(title, paragraph)))

    assert _fetch_vu_article(f"{ARTICLE_HOST}/naujienos/aa")["summary"] == paragraph








# -----------------------------------------------------------
# scrape_vu_news — STEP 4, the title ladder in storage
# -----------------------------------------------------------

@responses.activate
def test_the_article_h1_is_what_gets_stored(app, db):
    _listing(1, _listing_html(_card("/naujienos/aa", "Nuorodos antraste is saraso")))
    _article("aa", _page(title="Straipsnio antraste is h1"))

    scrape_vu_news()

    assert [r["title"] for r in _rows(db)] == ["Straipsnio antraste is h1"]


@responses.activate
def test_the_listing_headline_is_the_title_of_last_resort(app, db):
    _listing(1, _listing_html(_card("/naujienos/aa", "Nuorodos antraste is saraso")))
    _article("aa", _page(title=None))

    scrape_vu_news()

    assert [r["title"] for r in _rows(db)] == ["Nuorodos antraste is saraso"]


@responses.activate
def test_an_h1_of_whitespace_still_falls_back_to_the_listing_headline(app, db):
    _listing(1, _listing_html(_card("/naujienos/aa", "Nuorodos antraste is saraso")))
    _article("aa", "<html><body><h1>  </h1><article><p>Tekstas apie laboratorija.</p></article></body></html>")

    scrape_vu_news()

    assert [r["title"] for r in _rows(db)] == ["Nuorodos antraste is saraso"]


@responses.activate
def test_a_listing_headline_over_the_cap_is_cut_in_storage(app, db):
    long_title = "N" * (MAX_TITLE_LENGTH + 40)
    _listing(1, _listing_html(_card("/naujienos/aa", long_title)))
    _article("aa", _page(title=None))

    scrape_vu_news()

    assert [r["title"] for r in _rows(db)] == ["N" * MAX_TITLE_LENGTH]


@responses.activate
def test_an_article_h1_over_the_cap_is_cut_in_storage(app, db):
    _listing(1, _listing_html(_card("/naujienos/aa", "Nuorodos antraste is saraso")))
    _article("aa", _page(title="H" * (MAX_TITLE_LENGTH + 40)))

    scrape_vu_news()

    assert [r["title"] for r in _rows(db)] == ["H" * MAX_TITLE_LENGTH]




# -----------------------------------------------------------
# scrape_vu_news — STEP 4, the two untitled-article guards
# -----------------------------------------------------------

@responses.activate
def test_an_article_with_neither_a_title_nor_content_is_not_stored_at_all(app, db, monkeypatch, caplog):
    url = _untitled_link(monkeypatch)
    _article("be-pavadinimo", "<html><body><div class='wrap'><p>Nerandama.</p></div></body></html>")

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    # No row at all, so the next run simply tries the article
    # again instead of leaving an empty one in the feed forever
    assert result["found"] == 1
    assert result["new"] == 0
    assert _rows(db) == []
    assert "parsed to nothing — not stored" in caplog.text
    assert url in caplog.text


@responses.activate
def test_an_untitled_article_that_still_has_content_is_stored(app, db, monkeypatch):
    url = _untitled_link(monkeypatch)
    _article("be-pavadinimo", "<html><body><article><p>Tekstas be antrastes.</p></article></body></html>")

    result = scrape_vu_news()
    rows = _rows(db)

    # An empty title skips the republished-title lookup, which
    # would otherwise match every other title-less vu.lt row
    assert result["new"] == 1
    assert len(rows) == 1
    assert rows[0]["title"] == ""
    assert rows[0]["content"] == "Tekstas be antrastes."
    assert rows[0]["source_url"] == url


@responses.activate
def test_two_untitled_articles_are_both_stored(app, db, monkeypatch):
    # The empty-title short circuit proved from the other side:
    # the second row would be refused as a republished title if
    # the lookup ran on an empty string
    links = [{"href": "/naujienos/aa", "title": ""}, {"href": "/naujienos/bb", "title": ""}]
    monkeypatch.setattr("app.scraper.vu_scraper._listing_links", lambda soup: links)
    _listing(1, _listing_html("<p>nesvarbu</p>"))
    _article("aa", "<html><body><article><p>Pirmas tekstas.</p></article></body></html>")
    _article("bb", "<html><body><article><p>Antras tekstas.</p></article></body></html>")

    result = scrape_vu_news()

    assert result["new"] == 2
    assert sorted(r["content"] for r in _rows(db)) == ["Antras tekstas.", "Pirmas tekstas."]




# -----------------------------------------------------------
# scrape_vu_news — STEP 4, the dedupe guards
# -----------------------------------------------------------

@responses.activate
def test_a_title_already_stored_for_this_source_is_refused(app, db, caplog):
    db.execute(
        "INSERT INTO news_posts (id, title, content, source, source_url, published_at)"
        " VALUES (?, 'Nauja laboratorija Kauno fakultete', 'x', 'vu.lt', ?, '2026-08-01T00:00:00')",
        (str(uuid.uuid4()), f"{ARTICLE_HOST}/naujienos/senas"),
    )
    db.commit()
    _site(slug="naujas")

    with caplog.at_level(logging.INFO, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert len(_rows(db)) == 1
    assert "already stored under another URL" in caplog.text


@responses.activate
def test_the_title_dedupe_is_scoped_to_this_source(app, db):
    db.execute(
        "INSERT INTO news_posts (id, title, content, source, source_url, published_at)"
        " VALUES (?, 'Nauja laboratorija Kauno fakultete', 'x', 'knf.vu.lt', ?, '2026-08-01T00:00:00')",
        (str(uuid.uuid4()), "https://knf.vu.lt/naujienos/senas"),
    )
    db.commit()
    _site(slug="naujas")

    # The faculty newsroom republishing a central story is two
    # rows on purpose — the feed ranks them differently
    assert scrape_vu_news()["new"] == 1
    assert len(_rows(db)) == 1


@responses.activate
def test_the_title_dedupe_is_case_sensitive(app, db):
    db.execute(
        "INSERT INTO news_posts (id, title, content, source, source_url, published_at)"
        " VALUES (?, 'NAUJA LABORATORIJA KAUNO FAKULTETE', 'x', 'vu.lt', ?, '2026-08-01T00:00:00')",
        (str(uuid.uuid4()), f"{ARTICLE_HOST}/naujienos/senas"),
    )
    db.commit()
    _site(slug="naujas")

    assert scrape_vu_news()["new"] == 1


@responses.activate
def test_a_title_repeated_inside_one_batch_is_stored_once(app, db):
    _listing(1, _listing_html(
        _card("/naujienos/aa", "Pirmoji nuoroda i ta pati straipsni"),
        _card("/naujienos/bb", "Antroji nuoroda i ta pati straipsni"),
    ))
    _article("aa", _page(title="Ta pati istorija dviem adresais"))
    _article("bb", _page(title="Ta pati istorija dviem adresais"))

    result = scrape_vu_news()

    # The guard reads the row the same uncommitted transaction
    # inserted a moment ago
    assert result["found"] == 2
    assert result["new"] == 1
    assert len(_rows(db)) == 1


@responses.activate
def test_two_links_that_redirect_onto_one_url_insert_a_single_row(app, db):
    _listing(1, _listing_html(
        _card("/naujienos/aa", "Pirmoji nuoroda i ta pati straipsni"),
        _card("/naujienos/bb", "Antroji nuoroda i ta pati straipsni"),
    ))
    _article("aa", "", status=302, headers={"Location": f"{ARTICLE_HOST}/naujienos/cc"})
    _article("bb", "", status=302, headers={"Location": f"{ARTICLE_HOST}/naujienos/cc"})
    _article("cc", _page(title="Pirmas variantas antrastes"))
    _article("cc", _page(title="Antras variantas antrastes"))

    result = scrape_vu_news()

    # Different titles, so only INSERT OR IGNORE against the
    # UNIQUE source_url can be what stops the second row
    assert result["found"] == 2
    assert result["new"] == 1
    assert [r["source_url"] for r in _rows(db)] == [f"{ARTICLE_HOST}/naujienos/cc"]


@responses.activate
def test_a_second_identical_run_stores_nothing_new(app, db):
    _site()
    assert scrape_vu_news()["new"] == 1

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert len(_rows(db)) == 1




# -----------------------------------------------------------
# scrape_vu_news — STEP 4, what the row actually holds
# -----------------------------------------------------------

@responses.activate
def test_every_parsed_field_lands_in_the_stored_row(app, db):
    url = _site(slug="aa", title="Nauja laboratorija Kauno fakultete",
                paragraphs=(_line(120),), og_image=COVER,
                published="2026-08-20T09:30:00+03:00")

    with time_machine.travel(NOW, tick=False):
        scrape_vu_news()

    row = _rows(db)[0]

    assert row["title"] == "Nauja laboratorija Kauno fakultete"
    # The <h1> is inside <article>, so it heads the content too —
    # and is dropped from the teaser as the repeated title
    assert row["content"] == "Nauja laboratorija Kauno fakultete\n" + _line(120)
    assert row["summary"] == _line(120)
    assert row["image_url"] == COVER
    assert row["author_name"] == "Vilniaus universitetas"
    assert row["source"] == "vu.lt"
    assert row["source_url"] == url
    assert row["post_type"] == "article"
    # The offset is APPLIED, not dropped: 09:30 +03:00 is 06:30 UTC
    assert row["published_at"] == "2026-08-20T06:30:00"


@responses.activate
def test_the_stored_row_gets_a_fresh_uuid(app, db):
    _site()
    scrape_vu_news()

    assert uuid.UUID(_rows(db)[0]["id"]).version == 4


@responses.activate
def test_the_stored_summary_is_cut_to_the_column_limit(app, db):
    _site(og_description="s" * (MAX_SUMMARY_LENGTH + 400))
    scrape_vu_news()

    assert _rows(db)[0]["summary"] == "s" * MAX_SUMMARY_LENGTH


@responses.activate
def test_the_stored_content_is_cut_to_the_column_limit(app, db):
    _site(paragraphs=("c" * (MAX_CONTENT_LENGTH + 400),))
    scrape_vu_news()

    assert len(_rows(db)[0]["content"]) == MAX_CONTENT_LENGTH


@responses.activate
def test_an_article_with_no_usable_image_stores_a_null_image_url(app, db):
    _site(images=["/media/logo.png"])
    scrape_vu_news()

    assert _rows(db)[0]["image_url"] is None


@responses.activate
def test_an_article_whose_page_is_dead_is_counted_but_never_stored(app, db):
    _listing(1, _listing_html(_card("/naujienos/aa", "Straipsnis kurio nebera")))
    _article("aa", "", status=404)

    result = scrape_vu_news()

    assert result["found"] == 1
    assert result["new"] == 0
    assert _rows(db) == []




# -----------------------------------------------------------
# scrape_vu_news — STEP 5, closing the run row
# -----------------------------------------------------------

@responses.activate
def test_the_run_row_closes_with_the_counts_it_harvested(app, db):
    _listing(1, _listing_html(
        _card("/naujienos/aa", "Pirmas straipsnis apie laboratorija"),
        _card("/naujienos/bb", "Antras straipsnis apie studentus"),
    ))
    _article("aa", _page(title="Pirmas straipsnis apie laboratorija"))
    _article("bb", "", status=404)

    result = scrape_vu_news()
    run = _run(db, result["runId"])

    assert run["status"] == "completed"
    assert run["source"] == "vu.lt"
    assert run["articles_found"] == 2
    assert run["articles_new"] == 1
    assert run["finished_at"] is not None
    assert run["error_message"] is None


@responses.activate
def test_a_run_that_stored_nothing_still_completes(app, db):
    _site()
    scrape_vu_news()

    result = scrape_vu_news()

    assert _run(db, result["runId"])["status"] == "completed"
    assert _run(db, result["runId"])["articles_new"] == 0


@responses.activate
def test_a_collapsed_yield_is_reported_after_the_run_is_closed(app, db, caplog):
    _earlier_run(db, "vu.lt", found=200)
    _site()

    with caplog.at_level(logging.ERROR, logger="app.scraper.common"):
        result = scrape_vu_news()

    assert _run(db, result["runId"])["status"] == "completed"
    assert "yield collapsed" in caplog.text




# -----------------------------------------------------------
# scrape_vu_news — STEP 6, the push
# -----------------------------------------------------------

# -----------------------------------------------------------
# _many_site
# -----------------------------------------------------------
#
# n distinct cards and the n article pages behind them, so a
# push test can drive the count that picks a Lithuanian form.
# -----------------------------------------------------------

def _many_site(n):
    titles = [f"Kauno fakulteto naujiena numeris {i}" for i in range(n)]
    _listing(1, _listing_html(*[_card(f"/naujienos/a{i}", titles[i]) for i in range(n)]))
    for i in range(n):
        _article(f"a{i}", _page(title=titles[i]))
    return titles


@responses.activate
def test_one_new_article_pushes_the_singular_copy(app, db, push_spy):
    _earlier_run(db)
    _site()

    scrape_vu_news()

    assert len(push_spy) == 1
    assert push_spy[0] == {
        "channel": "news",
        "title": "VU naujienos",
        "body": "Naujas straipsnis iš vu.lt",
        "data": {"type": "news", "source": "vu.lt"},
        "title_en": "VU news",
        "body_en": "New article from vu.lt",
    }


@responses.activate
def test_two_new_articles_push_the_nominative_plural(app, db, push_spy):
    _earlier_run(db)
    _many_site(2)

    scrape_vu_news()

    assert push_spy[0]["title"] == "VU naujienos (2)"
    assert push_spy[0]["body"] == "2 nauji straipsniai iš vu.lt"
    assert push_spy[0]["title_en"] == "VU news (2)"
    assert push_spy[0]["body_en"] == "2 new articles from vu.lt"


@responses.activate
def test_ten_new_articles_push_the_genitive_plural(app, db, push_spy):
    _earlier_run(db)
    _many_site(10)

    scrape_vu_news()

    # Lithuanian declines 10 into the genitive, which a bare
    # "if n > 1" could never have picked
    assert push_spy[0]["body"] == "10 naujų straipsnių iš vu.lt"
    assert push_spy[0]["body_en"] == "10 new articles from vu.lt"


@responses.activate
def test_a_hand_fired_run_never_wakes_a_device(app, db, push_spy):
    _earlier_run(db)
    _site()

    result = scrape_vu_news(notify=False)

    assert result["new"] == 1
    assert push_spy == []


@responses.activate
def test_a_run_that_inserted_nothing_does_not_push(app, db, push_spy):
    _earlier_run(db)
    _site()
    scrape_vu_news(notify=False)

    result = scrape_vu_news()

    assert result["new"] == 0
    assert push_spy == []


@responses.activate
def test_the_first_completed_run_of_the_source_is_a_backfill(app, db, push_spy):
    _site()

    assert scrape_vu_news()["new"] == 1
    assert push_spy == []


@responses.activate
def test_a_second_run_inside_the_hour_does_not_push_again(app, db, push_spy):
    # Two listings registered for the same URL: responses serves
    # them to the two runs in order, so the second run finds an
    # article the first one never saw
    _earlier_run(db)
    _listing(1, _listing_html(_card("/naujienos/aa", "Pirmas straipsnis apie laboratorija")))
    _listing(1, _listing_html(_card("/naujienos/bb", "Antras straipsnis apie studentus")))
    _article("aa", _page(title="Pirmas straipsnis apie laboratorija"))
    _article("bb", _page(title="Antras straipsnis apie studentus"))

    first = scrape_vu_news()
    second = scrape_vu_news()

    assert (first["new"], second["new"]) == (1, 1)
    assert len(push_spy) == 1


@responses.activate
def test_a_push_that_raises_does_not_fail_the_run(app, db, monkeypatch, caplog):
    _earlier_run(db)

    def _boom(*args, **kwargs):
        raise RuntimeError("expo is down")

    monkeypatch.setattr("app.notifications.push.notify_channel", _boom)
    _site()

    with caplog.at_level(logging.ERROR, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["new"] == 1
    assert "Failed to send push notification" in caplog.text
    assert _run(db, result["runId"])["status"] == "completed"


@responses.activate
def test_a_missing_push_module_does_not_fail_the_run(app, db, monkeypatch):
    # The import itself is inside the try, so even a broken
    # notifications package leaves the harvest committed
    import app.notifications.push as push_module

    monkeypatch.delattr(push_module, "notify_channel")
    _earlier_run(db)
    _site()

    result = scrape_vu_news()

    assert result["new"] == 1
    assert len(_rows(db)) == 1




# -----------------------------------------------------------
# scrape_vu_news — the storage failure path
# -----------------------------------------------------------

# -----------------------------------------------------------
# _refuse_inserts
# -----------------------------------------------------------
#
# A trigger that aborts INSERTs into news_posts — optionally
# only the ones matching a title. Failing the write from
# INSIDE SQLite is what makes the storage half raise while
# every SELECT before it still works, which no monkeypatch of
# the module could reproduce.
# -----------------------------------------------------------

def _refuse_inserts(db, only_title=None):
    when = f"WHEN NEW.title = '{only_title}' " if only_title else ""
    db.execute(
        f"CREATE TRIGGER refuse_news_insert BEFORE INSERT ON news_posts {when}"
        "BEGIN SELECT RAISE(ABORT, 'insert refused'); END"
    )
    db.commit()


@responses.activate
def test_a_refused_insert_fails_the_run_and_stores_nothing(app, db):
    _refuse_inserts(db)
    _site()

    result = scrape_vu_news()

    assert result["found"] == 0
    assert result["new"] == 0
    assert result["error"] == "insert refused"
    assert _rows(db) == []


@responses.activate
def test_a_refused_insert_closes_the_run_row_as_failed(app, db):
    _refuse_inserts(db)
    _site()

    result = scrape_vu_news()
    run = _run(db, result["runId"])

    assert run["status"] == "failed"
    assert run["error_message"] == "insert refused"
    assert run["finished_at"] is not None


@responses.activate
def test_the_half_written_batch_is_rolled_back_before_the_run_is_failed(app, db):
    # The first article inserts, the second aborts — and the
    # rollback that comes first is what keeps a partial harvest
    # from persisting under a 'failed' row
    _refuse_inserts(db, only_title="Antras straipsnis apie studentus")
    _listing(1, _listing_html(
        _card("/naujienos/aa", "Pirmas straipsnis apie laboratorija"),
        _card("/naujienos/bb", "Antras straipsnis apie studentus"),
    ))
    _article("aa", _page(title="Pirmas straipsnis apie laboratorija"))
    _article("bb", _page(title="Antras straipsnis apie studentus"))

    result = scrape_vu_news()

    assert result["error"] == "insert refused"
    assert _rows(db) == []


@responses.activate
def test_an_article_a_failed_run_could_not_store_is_retried_next_run(app, db):
    _refuse_inserts(db)
    _site()
    assert scrape_vu_news()["new"] == 0

    db.execute("DROP TRIGGER refuse_news_insert")
    db.commit()

    assert scrape_vu_news()["new"] == 1


@responses.activate
def test_a_failed_storage_run_still_releases_the_source_lock(app, db):
    _refuse_inserts(db)
    _site()
    scrape_vu_news()

    db.execute("DROP TRIGGER refuse_news_insert")
    db.commit()

    result = scrape_vu_news()

    assert "skipped" not in result
    assert result["new"] == 1


@responses.activate
def test_a_rollback_that_itself_fails_is_logged_and_swallowed(app, db, monkeypatch, caplog):
    # The connection the batch was written on is exactly the
    # thing that may have broken, so its rollback raising must
    # not escape the scraper into the admin route as a 500
    real_get_db = common.get_db

    class _BrokenRollback:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def rollback(self):
            # The real rollback still runs, because the aborted
            # INSERT left a write lock that mark_run_failed's own
            # connection would otherwise wait thirty seconds for;
            # what is under test is the exception AFTER it
            self._conn.rollback()
            raise sqlite3.OperationalError("rollback refused")

    monkeypatch.setattr("app.scraper.vu_scraper.get_db", lambda: _BrokenRollback(real_get_db()))
    _refuse_inserts(db)
    _site()

    with caplog.at_level(logging.WARNING, logger="app.scraper.vu_scraper"):
        result = scrape_vu_news()

    assert result["error"] == "insert refused"
    assert "Rollback after the vu.lt failure did not take" in caplog.text
    # mark_run_failed opens its OWN connection, so the row still
    # closes even though the run's connection is unusable
    assert _run(db, result["runId"])["status"] == "failed"
    assert _rows(db) == []


@responses.activate
def test_a_failed_storage_run_leaves_the_push_unsent(app, db, push_spy):
    _earlier_run(db)
    _refuse_inserts(db)
    _site()

    result = scrape_vu_news()

    assert "error" in result
    assert push_spy == []
