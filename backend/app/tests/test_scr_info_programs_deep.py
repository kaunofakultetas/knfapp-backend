# -----------------------------------------------------------
#  [*] Tests — info_scraper study programmes, the exhaustive
#      pass over _scrape_programs / _card_text /
#      _program_entry / _extract_section
#
#  The broad suite in test_scraper_info.py already proves the
#  happy shapes. This file is the gap-closing pass over the
#  four functions that turn the bachelor and master listing
#  pages into the 'programs' blob /api/info serves: every
#  guard clause, every boundary either side of the constant,
#  every arm of every branch, and the wrong-type paths a
#  caller can actually reach.
#
#  What it pins:
#
#    - CONTAINER SELECTION. The four content selectors are
#      tried in order and the FIRST match wins even when it
#      is empty — an empty .article-content shadows a
#      populated <article> and the page yields nothing. Only
#      "no selector matched at all" skips the page.
#    - THE LINK PASS and its three independent gates: the
#      href substring (case SENSITIVE), more than 8
#      characters of anchor text, and the "daugiau" filter —
#      each tested at its boundary and on both arms.
#    - THE HEADING FALLBACK, which fires per page and is
#      gated on THIS page's link result. A page whose only
#      links were already seen on the other page counts as
#      "no links" and falls back — one of the few genuinely
#      surprising paths in the module.
#    - seen_names is shared across both pages and
#      case-insensitive, so the first page to list a
#      programme also fixes its degree.
#    - _card_text's two constants: at most THREE ancestors
#      are examined, and a candidate must add MORE than
#      three characters. Both tested at n and n+1.
#    - _program_entry's degree precedence (magistr- beats
#      bakalaur-, card beats page) and the whole duration
#      grammar — one digit, an optional decimal, four unit
#      spellings, and the lookbehind that keeps "2026 m."
#      from becoming a six-year programme.
#    - _extract_section as the guard the run hangs on: falsy
#      values pass through, every Exception becomes a named
#      string, and BaseException deliberately does NOT.
#
#  Two defects this pass found are now fixed and pinned as
#  ordinary tests: the fallback read a <li> wrapping a
#  <strong> twice (the outer copy glued), and it published
#  the "daugiau" ("more") navigation label as a programme,
#  which the link pass had always dropped.
#
#  No network and no clock: the parsing functions are pure,
#  and the two end-to-end runs drive `responses` exactly like
#  the rest of the suite.
# -----------------------------------------------------------


import json
import logging

import pytest
import responses
from bs4 import BeautifulSoup

from app.scraper import info_scraper
from app.scraper.info_scraper import (
    _card_text,
    _extract_section,
    _program_entry,
    _scrape_programs,
    scrape_faculty_info,
)


# type -> URL, straight from the module so a page the scraper
# stops fetching breaks these tests loudly
PAGE_URLS = {page["type"]: page["url"] for page in info_scraper.INFO_PAGES}

# The anchor text every structural test reuses: 17 characters,
# comfortably over the link pass's 8-character floor, and a
# known length so _card_text's "+3" boundary can be hit exactly
LINK_TEXT = "Ilgas pavadinimas"




# -----------------------------------------------------------
# _soup — the parser the scraper itself uses
# -----------------------------------------------------------

def _soup(html):
    return BeautifulSoup(html, "lxml")


# -----------------------------------------------------------
# _page — one listing page wrapped in a content container
# -----------------------------------------------------------
#
#   _page("<a href='/studijos/x'>...</a>")
#   _page("<h3>...</h3>", selector="article")
#
# Builds the smallest page _scrape_programs will read, in the
# container the test cares about.
#
# Used by:
#   - most _scrape_programs tests below
# -----------------------------------------------------------

def _page(inner, selector="article-content"):
    if selector == "article":
        return _soup(f"<html><body><article>{inner}</article></body></html>")
    if selector == "content":
        return _soup(f"<html><body><div id='content'>{inner}</div></body></html>")

    return _soup(f"<html><body><div class='{selector}'>{inner}</div></body></html>")


# -----------------------------------------------------------
# _names — just the programme names, in the order returned
# -----------------------------------------------------------

def _names(programs):
    return [program["name"] for program in programs]


# -----------------------------------------------------------
# _link — one programme anchor, wrapped in `levels` plain divs
# -----------------------------------------------------------
#
#   _link(2, label="trukmė 4 metai")
#
# The card label is written into the OUTERMOST wrapper, so
# `levels` is exactly how far _card_text has to walk to find
# it. levels=0 puts the anchor straight in the container with
# no label at all.
#
# Used by:
#   - the _card_text depth tests
# -----------------------------------------------------------

def _link(levels, label="trukmė 4 metai"):
    inner = f"<a href='/studijos/x'>{LINK_TEXT}</a>"

    for level in range(levels):
        if level == levels - 1:
            inner = f"<div>{inner} <span>{label}</span></div>"
        else:
            inner = f"<div>{inner}</div>"

    return _page(inner).find("a")


# -----------------------------------------------------------
# http / _serve — the five pages, faked
# -----------------------------------------------------------
#
# The container has no network, so the two end-to-end runs
# register every INFO_PAGES url up front; a page the test
# leaves out is served as a 404, which the scraper treats as
# a dead page. The front page is registered under both its
# bare and its trailing-slash form because requests
# normalises the empty path before sending.
#
# Used by:
#   - the end-to-end section at the bottom
# -----------------------------------------------------------

@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


def _serve(http, **pages):
    for page_type, url in PAGE_URLS.items():
        html = pages.get(page_type)
        urls = [url, url + "/"] if page_type == "main" else [url]

        for one in urls:
            if html is None:
                http.add(responses.GET, one, status=404)
            else:
                http.add(responses.GET, one, body=html, status=200,
                         content_type="text/html; charset=utf-8")


# A bachelor listing carrying one card per gate the link pass
# applies: a full card, a decimal duration, a card that states
# no duration, a "daugiau" link, an off-path link and a link
# whose text is too short
BACHELOR_PAGE = """
<html><body>
<div class="article-content">
  <h2>Bakalauro studijų programos</h2>
  <ul>
    <li><a href="/studijos/informatika">Informatika ir skaitmeninis turinys</a> <span>Trukmė 4 metai</span></li>
    <li><a href="/studijos/verslas">Verslo ir vadybos studijos</a> <span>3,5 metų</span></li>
    <li><a href="/studijos/socialinis">Socialinio darbo studijos</a></li>
    <li><a href="/studijos/priemimas">Daugiau apie priėmimą</a></li>
    <li><a href="/naujienos/2026">Naujienos apie studijas</a></li>
    <li><a href="/studijos/t">Trumpas</a></li>
  </ul>
</div>
</body></html>
"""

# A master listing with no programme links at all, so its
# heading fallback runs — and whose second heading repeats a
# programme the bachelor page already listed
MASTER_PAGE = """
<html><body>
<article>
  <h3>Informacinių sistemų inžinerijos magistrantūros studijos</h3>
  <p>Trukmė 2 metai</p>
  <h3>Verslo ir vadybos studijos</h3>
</article>
</body></html>
"""








# -----------------------------------------------------------
# _scrape_programs — which pages and which container
# -----------------------------------------------------------


def test_a_master_page_alone_still_yields_its_programmes():
    programs = _scrape_programs(None, _page("<a href='/studijos/x'>Magistro programa</a>"))

    assert programs == [{"name": "Magistro programa", "degree": "Magistras"}]


def test_neither_page_downloading_yields_an_empty_list():
    assert _scrape_programs(None, None) == []


@pytest.mark.parametrize("selector", ["article-content", "item-page", "content", "article"])
def test_a_listing_is_read_from_every_known_container(selector):
    programs = _scrape_programs(_page(f"<a href='/studijos/x'>{LINK_TEXT}</a>", selector), None)

    assert _names(programs) == [LINK_TEXT]


def test_the_first_matching_container_wins_over_a_later_one():
    html = ("<html><body>"
            "<div class='article-content'><a href='/studijos/a'>Is article-content</a></div>"
            "<div class='item-page'><a href='/studijos/b'>Is item-page</a></div>"
            "</body></html>")

    assert _names(_scrape_programs(_soup(html), None)) == ["Is article-content"]


def test_an_empty_first_container_shadows_a_populated_later_one():
    # select_one returns the empty div, which bs4 reports as
    # truthy, so the selector loop stops there and never sees
    # the <article> holding the actual listing
    html = ("<html><body><div class='article-content'></div>"
            f"<article><a href='/studijos/x'>{LINK_TEXT}</a></article></body></html>")

    assert _scrape_programs(_soup(html), None) == []


def test_a_page_matching_no_container_at_all_is_skipped():
    html = f"<html><body><div class='sidebar'><a href='/studijos/x'>{LINK_TEXT}</a></div></body></html>"

    assert _scrape_programs(_soup(html), None) == []


def test_a_master_page_without_a_container_gets_no_fallback_either():
    html = "<html><body><div class='sidebar'><h3>Magistro studijos cia</h3></div></body></html>"

    assert _scrape_programs(None, _soup(html)) == []


def test_a_dead_bachelor_page_does_not_cost_the_master_programmes():
    programs = _scrape_programs(None, _page("<a href='/studijos/x'>Magistro programa</a>"))

    assert len(programs) == 1


def test_a_page_that_is_not_a_soup_raises_for_the_caller_to_catch():
    # scrape_faculty_info only ever hands this function a soup
    # or None; anything else is a programming error and must
    # surface, not be swallowed into an empty programme list
    with pytest.raises(AttributeError):
        _scrape_programs(object(), None)








# -----------------------------------------------------------
# _scrape_programs — the link pass and its three gates
# -----------------------------------------------------------


@pytest.mark.parametrize("href", ["/studijos/x", "/studiju-programos", "/programos/x",
                                  "https://knf.vu.lt/studijos/x", "/en/program/x"])
def test_a_studies_or_program_href_is_a_programme_link(href):
    programs = _scrape_programs(_page(f"<a href='{href}'>{LINK_TEXT}</a>"), None)

    assert _names(programs) == [LINK_TEXT]


@pytest.mark.parametrize("href", ["/naujienos/2026", "/fakultetas/kontaktai", "/STUDIJOS/x",
                                  "/Programos/x", "", "#"])
def test_an_href_outside_the_studies_paths_is_not_a_programme(href):
    # The substring test is case SENSITIVE — an uppercase path
    # is not a programme link
    assert _scrape_programs(_page(f"<a href='{href}'>{LINK_TEXT}</a>"), None) == []


def test_an_anchor_carrying_no_href_is_not_a_programme():
    assert _scrape_programs(_page(f"<a name='x'>{LINK_TEXT}</a>"), None) == []


@pytest.mark.parametrize("text,kept", [("12345678", False), ("123456789", True)])
def test_a_link_needs_more_than_eight_characters_of_text(text, kept):
    programs = _scrape_programs(_page(f"<a href='/studijos/x'>{text}</a>"), None)

    assert _names(programs) == ([text] if kept else [])


def test_a_link_with_no_text_at_all_is_not_a_programme():
    assert _scrape_programs(_page("<a href='/studijos/x'></a>"), None) == []


def test_surrounding_whitespace_does_not_count_towards_the_text_floor():
    # get_text(strip=True) strips before the length test
    assert _scrape_programs(_page("<a href='/studijos/x'>   12345678   </a>"), None) == []


@pytest.mark.parametrize("text", ["Daugiau apie programa", "DAUGIAU APIE PROGRAMA",
                                  "Skaityti daugiau apie", "Programos daugiau"])
def test_a_more_link_is_dropped_whatever_its_case_or_position(text):
    assert _scrape_programs(_page(f"<a href='/studijos/x'>{text}</a>"), None) == []


def test_a_more_link_does_not_reserve_the_name_for_a_real_programme():
    # "daugiau" is filtered in the same condition that records
    # the name, so the label is never added to seen_names
    inner = ("<a href='/studijos/a'>Daugiau apie programa</a>"
             "<a href='/studijos/b'>Daugiau apie programa</a>")

    assert _scrape_programs(_page(inner), None) == []


def test_the_same_name_twice_on_one_page_is_stored_once():
    inner = (f"<a href='/studijos/a'>{LINK_TEXT}</a>"
             f"<a href='/studijos/b'>{LINK_TEXT.upper()}</a>")

    assert _names(_scrape_programs(_page(inner), None)) == [LINK_TEXT]


def test_a_programme_listed_on_both_pages_keeps_the_first_pages_degree():
    same = "<a href='/studijos/a'>Verslo ir vadybos studijos</a>"
    programs = _scrape_programs(_page(same), _page(same))

    assert programs == [{"name": "Verslo ir vadybos studijos", "degree": "Bakalauras"}]


def test_bachelor_entries_come_before_master_entries():
    programs = _scrape_programs(
        _page("<a href='/studijos/a'>Bakalauro programa viena</a>"),
        _page("<a href='/studijos/b'>Magistro programa viena</a>"))

    assert [program["degree"] for program in programs] == ["Bakalauras", "Magistras"]


def test_a_long_listing_is_not_capped():
    inner = "".join(f"<a href='/studijos/p{n}'>Programa numeris {n}</a>" for n in range(200))
    programs = _scrape_programs(_page(inner), None)

    assert len(programs) == 200








# -----------------------------------------------------------
# _scrape_programs — the heading / list fallback
# -----------------------------------------------------------


@pytest.mark.parametrize("tag", ["h3", "h4", "li", "strong"])
def test_the_fallback_reads_every_tag_it_walks(tag):
    programs = _scrape_programs(_page(f"<{tag}>Bakalauro studijos cia</{tag}>"), None)

    assert _names(programs) == ["Bakalauro studijos cia"]


@pytest.mark.parametrize("tag", ["h2", "p", "div", "span", "td"])
def test_the_fallback_ignores_every_other_tag(tag):
    assert _scrape_programs(_page(f"<{tag}>Bakalauro studijos cia</{tag}>"), None) == []


@pytest.mark.parametrize("text,kept", [("studijos12", False), ("studijos123", True)])
def test_a_fallback_candidate_needs_more_than_ten_characters(text, kept):
    programs = _scrape_programs(_page(f"<h3>{text}</h3>"), None)

    assert _names(programs) == ([text] if kept else [])


@pytest.mark.parametrize("text", ["Informatikos studijų programa", "INFORMATIKOS STUDIJŲ PROGRAMA",
                                  "Programa STUDIJoms rengti"])
def test_the_fallback_matches_studij_whatever_its_case(text):
    assert _names(_scrape_programs(_page(f"<h3>{text}</h3>"), None)) == [text]


def test_a_long_heading_that_never_says_studij_is_not_a_programme():
    assert _scrape_programs(_page("<h3>Priėmimo tvarka ir terminai</h3>"), None) == []


def test_the_fallback_only_runs_when_this_page_listed_no_links():
    inner = (f"<a href='/studijos/x'>{LINK_TEXT}</a>"
             "<h3>Informatikos studijų programa</h3>")

    assert _names(_scrape_programs(_page(inner), None)) == [LINK_TEXT]


def test_a_page_whose_only_links_were_already_seen_falls_back_to_its_headings():
    # The fallback is gated on THIS page's harvest, and a link
    # deduplicated away leaves that harvest empty — so the
    # master page's headings are read after all
    same = "<a href='/studijos/a'>Verslo studijos programa</a>"
    master = _page(same + "<h3>Kitos magistro studijos cia</h3>")

    assert _names(_scrape_programs(_page(same), master)) == [
        "Verslo studijos programa", "Kitos magistro studijos cia"]


def test_the_fallback_shares_the_dedupe_with_the_link_pass():
    link = "<a href='/studijos/a'>Verslo ir vadybos studijos</a>"
    master = _page("<h3>Verslo ir vadybos studijos</h3>")

    assert len(_scrape_programs(_page(link), master)) == 1


def test_the_fallback_reads_the_degree_and_duration_out_of_its_own_text():
    programs = _scrape_programs(None, _page("<h3>Magistrantūros studijos, 2 metai</h3>"))

    assert programs == [{"name": "Magistrantūros studijos, 2 metai",
                         "degree": "Magistras", "duration": "2 metai"}]


def test_a_page_with_neither_links_nor_headings_yields_nothing():
    assert _scrape_programs(_page("<p>Priėmimas vyksta kasmet</p>"), None) == []


def test_a_list_item_wrapping_a_strong_is_one_programme_not_two():
    inner = "<ul><li><strong>Informatikos studijų programa</strong> nuolatinės</li></ul>"
    programs = _scrape_programs(_page(inner), None)

    assert _names(programs) == ["Informatikos studijų programa"]


def test_a_more_label_is_not_a_programme_in_the_fallback_either():
    assert _scrape_programs(_page("<li>Daugiau apie studijas</li>"), None) == []








# -----------------------------------------------------------
# _card_text — how far it walks and what counts as a card
# -----------------------------------------------------------


@pytest.mark.parametrize("levels", [1, 2, 3])
def test_a_card_is_found_up_to_three_levels_above_the_link(levels):
    assert _card_text(_link(levels)) == f"{LINK_TEXT} trukmė 4 metai"


def test_a_card_four_levels_up_is_out_of_reach():
    assert _card_text(_link(4)) == LINK_TEXT


@pytest.mark.parametrize("extra,found", [("ab", False), ("abc", True)])
def test_a_card_has_to_add_more_than_three_characters(extra, found):
    # LINK_TEXT is 17 characters and the separator adds one, so
    # "ab" makes exactly +3 — which is not enough
    link = _page(f"<li><a href='/studijos/x'>{LINK_TEXT}</a>{extra}</li>").find("a")

    assert _card_text(link) == (f"{LINK_TEXT} {extra}" if found else LINK_TEXT)


def test_a_second_linked_programme_stops_the_search_at_once():
    inner = (f"<li><a href='/studijos/a'>{LINK_TEXT}</a>"
             "<a href='/studijos/b'>Kita programa cia</a> <span>trukmė 4 metai</span></li>")
    link = _page(inner).find("a")

    assert _card_text(link) == LINK_TEXT


def test_a_listing_two_levels_up_stops_the_search_after_the_card():
    # The <li> is a real card and is accepted before the <ul>
    # holding the other programme is ever examined
    inner = ("<ul>"
             f"<li><a href='/studijos/a'>{LINK_TEXT}</a> <span>trukmė 4 metai</span></li>"
             "<li><a href='/studijos/b'>Kita programa cia</a></li>"
             "</ul>")
    link = _page(inner).find("a")

    assert _card_text(link) == f"{LINK_TEXT} trukmė 4 metai"


def test_an_anchor_without_an_href_does_not_make_a_listing():
    # Only href-carrying anchors count towards the "this is the
    # listing, not a card" test
    inner = (f"<li><a name='x'>Zyme</a><a href='/studijos/a'>{LINK_TEXT}</a>"
             " <span>trukmė 4 metai</span></li>")
    link = _page(inner).find("a", href=True)

    assert _card_text(link) == f"Zyme {LINK_TEXT} trukmė 4 metai"


def test_the_card_text_is_flattened_with_spaces_not_glued():
    inner = f"<li><a href='/studijos/x'>{LINK_TEXT}</a><strong>trukmė 4 metai</strong></li>"
    link = _page(inner).find("a")

    assert _card_text(link) == f"{LINK_TEXT} trukmė 4 metai"


def test_the_search_stops_when_the_tree_runs_out_above_the_link():
    card = _page(f"<div id='card'><a href='/studijos/x'>{LINK_TEXT}</a>ab</div>").select_one("#card")
    card.extract()

    assert _card_text(card.find("a")) == LINK_TEXT


def test_a_link_with_no_parent_at_all_answers_its_own_text():
    link = _page(f"<a href='/studijos/x'>{LINK_TEXT}</a>").find("a")
    link.extract()

    assert link.parent is None
    assert _card_text(link) == LINK_TEXT








# -----------------------------------------------------------
# _program_entry — the degree
# -----------------------------------------------------------


def test_an_entry_always_carries_a_name_and_a_degree():
    assert _program_entry("Informatika", "Bakalauras", "") == {
        "name": "Informatika", "degree": "Bakalauras"}


@pytest.mark.parametrize("card", ["magistrantūros studijos", "magistro laipsnis",
                                  "MAGISTRANTŪROS STUDIJOS", "Magistro programa"])
def test_a_card_naming_a_master_degree_overrides_the_page(card):
    assert _program_entry("X", "Bakalauras", card)["degree"] == "Magistras"


@pytest.mark.parametrize("card", ["bakalauro studijos", "BAKALAURO", "Bakalauras"])
def test_a_card_naming_a_bachelor_degree_overrides_the_page(card):
    assert _program_entry("X", "Magistras", card)["degree"] == "Bakalauras"


def test_a_card_naming_both_degrees_reads_as_a_master():
    # The bachelor test is an elif, so it is never reached
    assert _program_entry("X", "Bakalauras",
                          "magistrantūros studijos po bakalauro")["degree"] == "Magistras"


@pytest.mark.parametrize("page_degree", ["Bakalauras", "Magistras"])
def test_a_card_naming_no_degree_leaves_the_page_degree_standing(page_degree):
    assert _program_entry("X", page_degree, "trukmė 4 metai")["degree"] == page_degree


@pytest.mark.parametrize("card", [None, "", 0, False, [], {}])
def test_a_falsy_card_text_leaves_the_page_degree_standing(card):
    entry = _program_entry("X", "Magistras", card)

    assert entry == {"name": "X", "degree": "Magistras"}


def test_a_non_string_card_text_raises_for_the_section_guard_to_catch():
    with pytest.raises(AttributeError):
        _program_entry("X", "Bakalauras", ["magistro"])








# -----------------------------------------------------------
# _program_entry — the duration grammar
# -----------------------------------------------------------


@pytest.mark.parametrize("card,duration", [
    ("4 metai", "4 metai"),
    ("trukmė 2 metų", "2 metai"),
    ("trukmė 2 metu", "2 metai"),
    ("trukmė 2 m.", "2 metai"),
    ("3,5 metų", "3,5 metai"),
    ("3.5 metų", "3.5 metai"),
    ("1,5 m.", "1,5 metai"),
    ("4metai", "4 metai"),
    ("4    metai", "4 metai"),
    ("5 metais", "5 metai"),
    ("0 metai", "0 metai"),
    ("9 metai", "9 metai"),
    ("v2 metai", "2 metai"),
])
def test_a_duration_the_card_states_is_normalised_to_metai(card, duration):
    assert _program_entry("X", "Bakalauras", card)["duration"] == duration


@pytest.mark.parametrize("card", [
    "",
    "trukmė nenurodyta",
    "metai",
    "4 years",
    "4 mėnesiai",
    "10 metai",
    "44 metai",
    "3,55 metai",
    "2026 m.",
    "priėmimas nuo 2024 m.",
])
def test_a_card_that_states_no_course_length_gets_no_duration(card):
    assert "duration" not in _program_entry("X", "Bakalauras", card)


def test_a_two_digit_number_is_never_a_duration():
    # The lookbehind is what keeps a year out of the field, and
    # it costs any programme longer than nine years — none exist
    assert "duration" not in _program_entry("X", "Bakalauras", "12 metai")


def test_the_first_duration_on_the_card_wins():
    entry = _program_entry("X", "Bakalauras", "2 metai nuolatinių arba 4 metai ištęstinių")

    assert entry["duration"] == "2 metai"


def test_a_year_next_to_a_real_duration_does_not_shadow_it():
    entry = _program_entry("X", "Bakalauras", "Priėmimas 2026 m., trukmė 4 metai")

    assert entry["duration"] == "4 metai"


def test_the_duration_is_read_case_insensitively():
    assert _program_entry("X", "Bakalauras", "TRUKMĖ 4 METAI")["duration"] == "4 metai"








# -----------------------------------------------------------
# _program_entry — what passes through untouched
# -----------------------------------------------------------


def test_the_name_is_stored_exactly_as_given():
    assert _program_entry("  Tarpai  ", "Bakalauras", "")["name"] == "  Tarpai  "


def test_a_very_long_programme_name_is_not_capped():
    # Contact items are capped at 100 characters; programme
    # names are not — pinned so a future cap is a deliberate
    # change and not a silent one
    name = "A" * 500

    assert _program_entry(name, "Bakalauras", "")["name"] == name


def test_an_empty_name_is_still_an_entry():
    assert _program_entry("", "Bakalauras", "")["name"] == ""


def test_the_page_degree_is_passed_through_verbatim():
    assert _program_entry("X", "Doktorantūra", "")["degree"] == "Doktorantūra"


def test_the_entry_never_carries_a_key_the_blob_does_not_expect():
    entry = _program_entry("X", "Bakalauras", "magistro 3,5 metų")

    assert set(entry) == {"name", "degree", "duration"}








# -----------------------------------------------------------
# _extract_section — the guard that keeps one dead page from
# failing the run
# -----------------------------------------------------------


@pytest.mark.parametrize("value", [["ok"], [], {}, {"a": 1}, None, 0, "", False])
def test_a_healthy_extractor_answers_its_value_and_no_error(value):
    # Falsy values matter: an empty list is exactly what a dead
    # page produces, and it must NOT be reported as an error
    assert _extract_section("programs", lambda: value) == (value, None)


def test_an_extractor_returning_none_is_not_reported_as_an_error():
    # The caller cannot tell this apart from a failure by value
    # alone — the error slot is the only signal, and it is None
    assert _extract_section("programs", lambda: None) == (None, None)


@pytest.mark.parametrize("args", [(), (1,), (1, 2), (1, 2, 3)])
def test_every_argument_is_forwarded_to_the_extractor(args):
    seen = []
    value, error = _extract_section("programs", lambda *got: seen.append(got) or "done", *args)

    assert seen == [args]
    assert (value, error) == ("done", None)


def test_the_arguments_reach_the_extractor_unchanged():
    bachelor, master = _page("<p>a</p>"), _page("<p>b</p>")
    value, _ = _extract_section("programs", lambda b, m: (b is bachelor, m is master),
                                bachelor, master)

    assert value == (True, True)


def test_a_crashing_extractor_is_named_with_its_message():
    def _boom(bachelor, master):
        raise ValueError("blogas puslapis")

    assert _extract_section("programs", _boom, None, None) == (None, "programs: blogas puslapis")


@pytest.mark.parametrize("error,message", [
    (ValueError(""), "programs: "),
    (RuntimeError("eilutė su ąžuolu"), "programs: eilutė su ąžuolu"),
    (KeyError("trūksta"), "programs: 'trūksta'"),
    (AttributeError("'NoneType' object has no attribute 'select_one'"),
     "programs: 'NoneType' object has no attribute 'select_one'"),
])
def test_every_exception_is_flattened_into_the_named_string(error, message):
    value, reported = _extract_section("programs", lambda: (_ for _ in ()).throw(error))

    assert value is None
    assert reported == message


def test_a_multi_line_message_is_reported_as_written():
    value, reported = _extract_section(
        "programs", lambda: (_ for _ in ()).throw(ValueError("pirma\nantra")))

    assert reported == "programs: pirma\nantra"


def test_the_section_name_is_whatever_the_caller_passed():
    _, reported = _extract_section("general_contact",
                                   lambda: (_ for _ in ()).throw(ValueError("x")))

    assert reported.startswith("general_contact: ")


def test_a_non_callable_extractor_is_reported_rather_than_raised():
    value, reported = _extract_section("programs", None)

    assert value is None
    assert reported == "programs: 'NoneType' object is not callable"


def test_a_wrong_arity_is_reported_rather_than_raised():
    value, reported = _extract_section("programs", lambda only: only, 1, 2)

    assert value is None
    assert reported.startswith("programs: ")


def test_the_real_scraper_raising_on_a_bad_page_becomes_a_named_error():
    value, reported = _extract_section("programs", _scrape_programs, object(), None)

    assert value is None
    assert reported.startswith("programs: ")
    assert "select_one" in reported


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_a_base_exception_is_deliberately_not_swallowed(error):
    # The guard catches Exception, not BaseException, so a
    # shutdown still stops the run instead of being filed as a
    # broken section
    with pytest.raises(error):
        _extract_section("programs", lambda: (_ for _ in ()).throw(error()))


def test_a_failed_section_is_logged_with_its_traceback(caplog):
    with caplog.at_level(logging.ERROR, logger="app.scraper.info_scraper"):
        _extract_section("programs", lambda: (_ for _ in ()).throw(ValueError("blogas")))

    assert "Faculty info section 'programs' failed" in caplog.text
    assert "ValueError: blogas" in caplog.text


def test_a_healthy_section_logs_nothing(caplog):
    with caplog.at_level(logging.ERROR, logger="app.scraper.info_scraper"):
        _extract_section("programs", lambda: ["ok"])

    assert caplog.text == ""








# -----------------------------------------------------------
# End to end — the four functions through a real run
# -----------------------------------------------------------


def test_a_run_stores_exactly_what_the_two_listing_pages_yielded(app, db, http):
    _serve(http, bachelor=BACHELOR_PAGE, master=MASTER_PAGE)

    result = scrape_faculty_info()

    row = db.execute(
        "SELECT data_json FROM faculty_info WHERE lang = 'lt' AND section = 'programs'").fetchone()
    assert json.loads(row["data_json"]) == [
        {"name": "Informatika ir skaitmeninis turinys", "degree": "Bakalauras",
         "duration": "4 metai"},
        {"name": "Verslo ir vadybos studijos", "degree": "Bakalauras", "duration": "3,5 metai"},
        {"name": "Socialinio darbo studijos", "degree": "Bakalauras"},
        {"name": "Informacinių sistemų inžinerijos magistrantūros studijos",
         "degree": "Magistras"},
    ]
    assert result["programs_found"] == 4


def test_a_broken_programs_section_leaves_the_stored_blob_alone(app, db, http, monkeypatch):
    _serve(http, bachelor=BACHELOR_PAGE, master=MASTER_PAGE)
    scrape_faculty_info()
    before = db.execute(
        "SELECT data_json FROM faculty_info WHERE section = 'programs'").fetchone()["data_json"]

    monkeypatch.setattr(info_scraper, "_scrape_programs",
                        lambda bachelor, master: (_ for _ in ()).throw(ValueError("struktūra")))
    scrape_faculty_info()

    after = db.execute(
        "SELECT data_json FROM faculty_info WHERE section = 'programs'").fetchone()
    run = db.execute(
        "SELECT status, error_message FROM scraper_runs ORDER BY started_at").fetchall()[-1]
    assert after["data_json"] == before
    assert run["status"] == "completed"
    assert run["error_message"] == "programs: struktūra"
