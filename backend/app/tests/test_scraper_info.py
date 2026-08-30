# -----------------------------------------------------------
#  [*] Tests — scraper/info_scraper.py, the faculty handbook
#
#  What this module proves about the daily knf.vu.lt info
#  scrape (contacts, structure, bachelor and master pages into
#  the faculty_info blobs /api/info serves):
#
#    - PARTIAL FAILURE IS ISOLATED. This is the register
#      blocker: one dead page used to fail the whole run, so
#      62 runs out of 62 stored nothing. Every extractor is
#      None-safe and guarded, so a dead contacts page costs
#      the contacts section and nothing else — programs and
#      general_contact still land, the run still closes as
#      'completed', and the failed section is only NAMED in
#      error_message
#    - a section is written only when the run produced
#      something for it, so a bad scrape leaves the previous
#      good blob alone instead of replacing it with defaults
#    - the LANGUAGE contract: the scraper writes lang 'lt'
#      and only 'lt'. GET /api/info?lang=en therefore serves
#      the English handbook with the SAME scraped rows laid
#      over it — never a second, silently different dataset
#    - idempotency: re-running the scrape updates the three
#      rows in place (ids survive), counts zero changed
#      sections and never duplicates a row
#    - the privacy filter the public endpoint depends on:
#      only *.vu.lt emails and Kaunas landlines are
#      republished, so a lecturer's gmail or mobile found in
#      a paragraph is dropped before anything is stored
#    - malformed-page resilience: no container, empty
#      headings, glued wrappers, one-cell table rows, a page
#      served as a PDF, a transport error, unclosed markup
#    - the bookkeeping: one scraper_runs row per run,
#      articles_found = contacts + programs, articles_new =
#      SECTIONS changed, the 30-day prune, and a failure that
#      rolls the pending section writes back
#    - authorisation on POST /api/scraper/info (admin only)
#      and its 200/409/502 mapping
#
#  Every fetch goes through `responses` with the fixture HTML
#  below — the test container has no network, so a test that
#  reached knf.vu.lt would fail by construction. Time-dependent
#  assertions (scraped_at, retention, staleness) use
#  time_machine; nothing sleeps.
# -----------------------------------------------------------


import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses
import time_machine
from bs4 import BeautifulSoup

from app.scraper import info_scraper
from app.scraper.info_scraper import (
    GENERAL_CONTACT_DEFAULTS,
    _card_text,
    _extract_email,
    _extract_phone,
    _extract_section,
    _faculty_phone,
    _institutional_email,
    _name_shaped,
    _program_entry,
    _scrape_contacts,
    _scrape_general_contact,
    _scrape_programs,
    _scrape_staff,
    _store_info,
    _stored_section,
    scrape_faculty_info,
)


TRIGGER = "/api/scraper/info"
INFO = "/api/info"

# type -> URL, straight from the module so a page the scraper
# stops fetching breaks these tests loudly
PAGE_URLS = {page["type"]: page["url"] for page in info_scraper.INFO_PAGES}




# -----------------------------------------------------------
# Fixture pages
# -----------------------------------------------------------
#
# Small but structurally faithful copies of the five knf.vu.lt
# pages: a .article-content / .item-page / article container,
# h2/h3 headings opening a group, entries as <p>, <li> and
# table rows, programme links inside cards, and a <footer>
# carrying the switchboard number. Each one also carries the
# junk the parser has to survive — a private email, a mobile
# number, a wrapper div, a one-cell row, an empty heading.
# -----------------------------------------------------------

CONTACTS_HTML = """
<html><body>
<div class="article-content">
  <p>Bendra informacija, knf@knf.vu.lt, 101 kab.</p>
  <h2>Dekanatas</h2>
  <div>
    <p>Studijų skyrius, studijos@knf.vu.lt, +370 37 422 604, 102 kab.</p>
    <p>Prodekanė studijoms, prodekane@knf.vu.lt, kab. 103</p>
  </div>
  <p>Studijų skyrius, studijos@knf.vu.lt, +370 37 422 604, 102 kab.</p>
  <p>Rėmėjas Petras, petras.remejas@gmail.com, +370 612 34567</p>
  <p>Ne</p>
  <h3>Paslaugos</h3>
  <table>
    <tr><th>Padalinys</th><th>Kontaktai</th></tr>
    <tr><td>Biblioteka</td><td>biblioteka@knf.vu.lt (8-37) 42 25 35</td></tr>
    <tr><td>Vieną ląstelę turinti eilutė</td></tr>
    <tr><td>Archyvas</td><td>joks kontaktas</td></tr>
  </table>
  <ul>
    <li>IT pagalba, it@knf.vu.lt, kab. 315</li>
    <li>Skelbimai</li>
  </ul>
  <h4></h4>
  <p>Pamestas įrašas, pamestas@knf.vu.lt</p>
</div>
</body></html>
"""

STRUCTURE_HTML = """
<html><body>
<div class="item-page">
  <h2>Informatikos katedra</h2>
  <p>Prof. dr. Jonas Jonaitis, katedros vedėjas, jonas.jonaitis@knf.vu.lt</p>
  <ul>
    <li><p>Doc. dr. Ona Onaitė, prodekanė, +370 37 422 604</p></li>
    <li>Lekt. Petras Petraitis, asistentas</li>
  </ul>
  <p>Studijų dalykų aprašai skelbiami dr. tinklalapyje</p>
  <p>Buveinės adr. Muitinės g. 8, Kaunas</p>
  <h3>Tuščias skyrius</h3>
  <p>Šiame skyriuje darbuotojų sąrašas dar nepaskelbtas</p>
  <h3>Verslo katedra</h3>
  <p>Prof. dr. Rasa Rasaitė, verslas@knf.vu.lt</p>
</div>
</body></html>
"""

BACHELOR_HTML = """
<html><body>
<div class="article-content">
  <ul>
    <li><a href="/studijos/informatika">Informatika ir skaitmeninis turinys</a> <span>4 metai</span></li>
    <li><a href="/studijos/verslas">Verslo ir vadybos studijos</a> <span>3,5 metų</span></li>
    <li><a href="/studijos/socialinis-darbas">Socialinio darbo studijos</a></li>
    <li><a href="/studijos/informatika">Daugiau informacijos apie studijas</a></li>
    <li><a href="/studijos/tr">Trumpas</a></li>
    <li><a href="/naujienos/2026">Nauja programa nuo 2026 m.</a></li>
  </ul>
</div>
</body></html>
"""

MASTER_HTML = """
<html><body>
<article>
  <h3>Informacinių sistemų inžinerijos magistrantūros studijos</h3>
  <p>Trukmė 2 metai</p>
  <h3>Verslo ir vadybos studijos</h3>
  <h4>Trumpa</h4>
</article>
</body></html>
"""

MAIN_HTML = """
<html><body>
<div class="hero"><p>Muitinės g. 8, LT-44280 Kaunas</p></div>
<p>Skambinkite mobiliuoju +370 612 34567</p>
<footer>
  <p>Telefonas: +370 37 422 523</p>
  <p>El. paštas knf@knf.vu.lt.</p>
</footer>
</body></html>
"""




# -----------------------------------------------------------
# http
# -----------------------------------------------------------
#
# The `responses` fake every scraping test drives its pages
# through. Registrations are reusable (responses replays the
# last match), so two runs in one test see the same pages
# unless the test resets it.
#
# Used by:
#   - every test that calls scrape_faculty_info or the admin
#     trigger route
# -----------------------------------------------------------

@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock




# -----------------------------------------------------------
# _serve
# -----------------------------------------------------------
#
#   _serve(http, contacts=CONTACTS_HTML)   — that page only,
#                                            the other four dead
#
# Registers one response per INFO_PAGES entry: the given HTML
# as text/html, or a 404 for a page the test wants dead. The
# front page is registered under both its bare and its
# trailing-slash form because requests normalises the empty
# path before sending.
#
# Used by:
#   - every scraping test
# -----------------------------------------------------------

def _serve(http, main=None, structure=None, contacts=None, bachelor=None, master=None,
           content_type="text/html; charset=utf-8"):
    pages = {"main": main, "structure": structure, "contacts": contacts,
             "bachelor": bachelor, "master": master}

    for page_type, html in pages.items():
        urls = [PAGE_URLS[page_type]]
        if page_type == "main":
            urls.append(PAGE_URLS[page_type] + "/")

        for url in urls:
            if html is None:
                http.add(responses.GET, url, status=404)
            else:
                http.add(responses.GET, url, body=html, status=200, content_type=content_type)


# -----------------------------------------------------------
# _serve_all — the five healthy pages in one call
# -----------------------------------------------------------

def _serve_all(http):
    _serve(http, main=MAIN_HTML, structure=STRUCTURE_HTML, contacts=CONTACTS_HTML,
           bachelor=BACHELOR_HTML, master=MASTER_HTML)


# -----------------------------------------------------------
# _soup — the parser the scraper itself uses
# -----------------------------------------------------------

def _soup(html):
    return BeautifulSoup(html, "lxml")


# -----------------------------------------------------------
# _sections — {section: decoded blob} for one language
# -----------------------------------------------------------

def _sections(db, lang="lt"):
    rows = db.execute("SELECT section, data_json FROM faculty_info WHERE lang = ?", (lang,)).fetchall()

    return {row["section"]: json.loads(row["data_json"]) for row in rows}


# -----------------------------------------------------------
# _runs — the scraper_runs rows this source wrote, newest last
# -----------------------------------------------------------

def _runs(db, source="knf.vu.lt/info"):
    return db.execute(
        "SELECT * FROM scraper_runs WHERE source = ? ORDER BY started_at", (source,)
    ).fetchall()


# -----------------------------------------------------------
# _items — every contact item in a contacts blob, flattened
# -----------------------------------------------------------

def _items(contacts):
    return [item for category in contacts for item in category["items"]]


# -----------------------------------------------------------
# _category — one named category out of a contacts blob
# -----------------------------------------------------------

def _category(contacts, name):
    for category in contacts:
        if category["category"] == name:
            return category

    return None








# -----------------------------------------------------------
# The field guards — what may be republished
# -----------------------------------------------------------


def test_extract_email_finds_the_first_address_in_the_text():
    assert _extract_email("Rašykite studijos@knf.vu.lt arba knf@knf.vu.lt") == "studijos@knf.vu.lt"


def test_extract_email_trims_a_sentence_ending_dot():
    assert _extract_email("Rašykite knf@knf.vu.lt.") == "knf@knf.vu.lt"


def test_extract_email_answers_none_when_there_is_no_address():
    assert _extract_email("Muitinės g. 8, Kaunas") is None


def test_institutional_email_keeps_vu_lt_and_its_subdomains():
    assert _institutional_email("knf@knf.vu.lt") == "knf@knf.vu.lt"
    assert _institutional_email("rektorius@vu.lt") == "rektorius@vu.lt"
    assert _institutional_email("VARDAS@STUD.VU.LT") == "VARDAS@STUD.VU.LT"


def test_institutional_email_drops_a_private_address():
    assert _institutional_email("destytojas@gmail.com") is None


def test_institutional_email_drops_a_lookalike_domain():
    assert _institutional_email("x@knf.vu.lt.evil.com") is None
    assert _institutional_email("x@notvu.lt") is None


def test_institutional_email_drops_none_and_a_token_without_an_at():
    assert _institutional_email(None) is None
    assert _institutional_email("") is None
    assert _institutional_email("knf.vu.lt") is None


def test_faculty_phone_keeps_both_kaunas_spellings():
    assert _faculty_phone("+370 37 422 523") == "+370 37 422 523"
    assert _faculty_phone("(8-37) 42 25 23") == "(8-37) 42 25 23"


def test_faculty_phone_drops_a_lecturers_mobile():
    assert _faculty_phone("+370 612 34567") is None


def test_faculty_phone_drops_none_and_an_empty_string():
    assert _faculty_phone(None) is None
    assert _faculty_phone("") is None


def test_faculty_phone_drops_another_citys_landline():
    assert _faculty_phone("+370 5 268 7000") is None


def test_faculty_phone_keeps_a_number_written_without_a_country_code():
    assert _faculty_phone("37 422 523") == "37 422 523"


def test_extract_phone_reads_the_international_form():
    assert _extract_phone("Tel. +370 37 422 523, kab. 101") == "+370 37 422 523"


def test_extract_phone_collapses_inner_whitespace():
    assert _extract_phone("Tel. +370\t37 422 523") == "+370 37 422 523"


def test_a_number_split_across_elements_is_still_a_phone():
    # The front page's contact block is read WITHOUT stripping, so
    # a number spread over two <span>s arrives with " \n " in its
    # gaps — a one-character separator class dropped it entirely
    html = "<html><body><footer><span>+370</span> <span>37 422 523</span></footer></body></html>"

    assert _extract_phone("Tel. +370  37   422  523") == "+370 37 422 523"
    assert _scrape_general_contact(_soup(html)) == {"phone": "+370 37 422 523"}


def test_extract_phone_reads_the_old_national_form():
    assert _extract_phone("Tel. (8-37) 42 25 23") == "(8-37) 42 25 23"


def test_extract_phone_ignores_a_bare_number_without_a_plus_or_brackets():
    assert _extract_phone("Tel. 8 37 422 523") is None


def test_extract_phone_answers_none_when_there_is_no_number():
    assert _extract_phone("Muitinės g. 8, Kaunas") is None








# -----------------------------------------------------------
# _scrape_contacts — the contacts page
# -----------------------------------------------------------


def test_contacts_are_grouped_under_their_headings():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))

    assert [c["category"] for c in contacts] == ["Kontaktai", "Dekanatas", "Paslaugos"]


def test_an_entry_above_the_first_heading_lands_under_the_implicit_category():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))

    assert _category(contacts, "Kontaktai")["items"] == [
        {"name": "Bendra informacija", "email": "knf@knf.vu.lt", "room": "101"},
    ]


def test_a_contact_entry_carries_its_name_phone_email_and_room():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))

    assert _category(contacts, "Dekanatas")["items"][0] == {
        "name": "Studijų skyrius",
        "phone": "+370 37 422 604",
        "email": "studijos@knf.vu.lt",
        "room": "102",
    }


def test_a_wrapper_div_does_not_glue_the_entries_it_holds_into_one_item():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))
    names = [item["name"] for item in _items(contacts)]

    assert "Prodekanė studijoms" in names
    assert not any("Prodekanė" in name and "Studijų skyrius" in name for name in names)


def test_the_same_entry_twice_in_one_category_is_stored_once():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))
    dekanatas = [item["name"] for item in _category(contacts, "Dekanatas")["items"]]

    assert dekanatas.count("Studijų skyrius") == 1


def test_a_private_email_and_a_mobile_number_are_never_republished():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))
    blob = json.dumps(contacts, ensure_ascii=False)

    assert "gmail.com" not in blob
    assert "612 34567" not in blob
    assert not any("Rėmėjas" in item["name"] for item in _items(contacts))


def test_a_table_row_becomes_a_contact_item():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))

    assert _category(contacts, "Paslaugos")["items"][0] == {
        "name": "Biblioteka",
        "phone": "(8-37) 42 25 35",
        "email": "biblioteka@knf.vu.lt",
    }


def test_a_table_row_carrying_only_one_of_the_two_details_is_still_an_item():
    html = """<div class="article-content"><table>
        <tr><td>Archyvas</td><td>archyvas@knf.vu.lt</td></tr>
        <tr><td>Budėtojas</td><td>(8-37) 42 25 00</td></tr>
    </table></div>"""
    items = _items(_scrape_contacts(_soup(html)))

    assert items == [
        {"name": "Archyvas", "email": "archyvas@knf.vu.lt"},
        {"name": "Budėtojas", "phone": "(8-37) 42 25 00"},
    ]


def test_an_entry_with_a_phone_and_no_email_keeps_its_name_and_room():
    html = """<div class="article-content"><p>Budėtojas, +370 37 422 500, 100 kab.</p></div>"""
    item = _items(_scrape_contacts(_soup(html)))[0]

    assert item == {
        "name": "Budėtojas, +370 37 422 500, 100 kab.",
        "phone": "+370 37 422 500",
        "room": "100",
    }


def test_a_table_row_with_a_single_cell_is_skipped():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))
    names = [item["name"] for item in _items(contacts)]

    assert "Vieną ląstelę turinti eilutė" not in names


def test_a_table_row_without_contact_details_is_skipped():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))
    names = [item["name"] for item in _items(contacts)]

    assert "Archyvas" not in names


def test_a_table_row_without_a_name_is_skipped():
    html = """<div id="content"><table>
        <tr><td></td><td>knf@knf.vu.lt</td></tr>
    </table></div>"""

    assert _scrape_contacts(_soup(html)) == []


def test_a_room_number_is_read_in_either_order():
    html = """<div class="article-content">
        <p>Registratūra, registratura@knf.vu.lt, 305 kab.</p>
        <p>Skaitykla, skaitykla@knf.vu.lt, kab. 306</p>
    </div>"""
    items = _items(_scrape_contacts(_soup(html)))

    assert [item["room"] for item in items] == ["305", "306"]


def test_a_two_digit_room_is_not_read_as_a_room():
    html = """<div class="article-content"><p>Sandėlis, sandelis@knf.vu.lt, 12 kab.</p></div>"""
    item = _items(_scrape_contacts(_soup(html)))[0]

    assert "room" not in item


def test_a_line_too_short_to_be_an_entry_is_skipped():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))
    names = [item["name"] for item in _items(contacts)]

    assert "Ne" not in names


def test_a_list_item_without_contact_details_is_skipped():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))
    names = [item["name"] for item in _items(contacts)]

    assert "Skelbimai" not in names


def test_an_entry_with_no_name_before_its_email_falls_back_to_the_text():
    html = """<div class="article-content"><p>- studijos@knf.vu.lt Studijų skyrius</p></div>"""
    item = _items(_scrape_contacts(_soup(html)))[0]

    assert item["name"] == "- studijos@knf.vu.lt Studijų skyrius"
    assert item["email"] == "studijos@knf.vu.lt"


def test_an_entry_starting_with_its_email_still_gets_a_name():
    html = """<div class="article-content"><p>knf@knf.vu.lt +370 37 422 523</p></div>"""
    item = _items(_scrape_contacts(_soup(html)))[0]

    assert item == {"name": "knf@knf.vu.lt", "phone": "+370 37 422 523", "email": "knf@knf.vu.lt"}


def test_a_very_long_name_is_capped_at_a_hundred_characters():
    html = f"""<div class="article-content"><p>{"A" * 130}, knf@knf.vu.lt</p></div>"""
    item = _items(_scrape_contacts(_soup(html)))[0]

    assert item["name"] == "A" * 100


def test_a_category_with_no_publishable_items_is_dropped():
    html = """<div class="article-content">
        <h2>Tuščia kategorija</h2>
        <p>Šiame skyriuje kontaktų kol kas nėra</p>
        <h2>Dekanatas</h2>
        <p>Priimamasis, knf@knf.vu.lt</p>
    </div>"""
    contacts = _scrape_contacts(_soup(html))

    assert [c["category"] for c in contacts] == ["Dekanatas"]


def test_an_entry_under_an_empty_heading_is_dropped():
    contacts = _scrape_contacts(_soup(CONTACTS_HTML))
    names = [item["name"] for item in _items(contacts)]

    assert "Pamestas įrašas" not in names


def test_contacts_of_a_page_that_never_downloaded_are_empty():
    assert _scrape_contacts(None) == []


def test_contacts_of_a_page_without_a_known_container_are_empty():
    html = "<html><body><div class='sidebar'><p>Priimamasis, knf@knf.vu.lt</p></div></body></html>"

    assert _scrape_contacts(_soup(html)) == []


@pytest.mark.parametrize("container", [
    '<div class="article-content">',
    '<div class="item-page">',
    '<div id="content">',
    "<article>",
])
def test_contacts_are_read_from_every_known_container(container):
    closing = "</article>" if container == "<article>" else "</div>"
    html = f"{container}<p>Priimamasis, knf@knf.vu.lt</p>{closing}"
    contacts = _scrape_contacts(_soup(html))

    assert _items(contacts)[0]["email"] == "knf@knf.vu.lt"


def test_a_malformed_contacts_page_does_not_raise():
    html = "<div class='article-content'><p>Priimamasis, knf@knf.vu.lt<div><tr><td>"

    assert isinstance(_scrape_contacts(_soup(html)), list)








# -----------------------------------------------------------
# _scrape_programs / _card_text / _program_entry
# -----------------------------------------------------------


def test_programme_links_become_entries_with_their_page_degree():
    programs = _scrape_programs(_soup(BACHELOR_HTML), None)

    assert programs[0] == {
        "name": "Informatika ir skaitmeninis turinys",
        "degree": "Bakalauras",
        "duration": "4 metai",
    }


def test_a_comma_decimal_duration_is_kept_as_written():
    programs = _scrape_programs(_soup(BACHELOR_HTML), None)

    assert programs[1]["duration"] == "3,5 metai"


def test_a_duration_the_page_never_states_is_omitted():
    programs = _scrape_programs(_soup(BACHELOR_HTML), None)
    socialinis = [p for p in programs if p["name"] == "Socialinio darbo studijos"][0]

    assert "duration" not in socialinis


def test_a_more_link_is_not_a_programme():
    programs = _scrape_programs(_soup(BACHELOR_HTML), None)

    assert not any("Daugiau" in p["name"] for p in programs)


def test_a_link_with_too_little_text_is_not_a_programme():
    programs = _scrape_programs(_soup(BACHELOR_HTML), None)

    assert not any(p["name"] == "Trumpas" for p in programs)


def test_a_link_outside_the_studies_path_is_not_a_programme():
    programs = _scrape_programs(_soup(BACHELOR_HTML), None)

    assert not any("naujienos" in p["name"].lower() for p in programs)
    assert not any(p["name"].startswith("Nauja programa") for p in programs)


def test_a_programme_listed_on_both_pages_is_stored_once():
    programs = _scrape_programs(_soup(BACHELOR_HTML), _soup(MASTER_HTML))
    names = [p["name"] for p in programs]

    assert names.count("Verslo ir vadybos studijos") == 1


def test_the_master_page_falls_back_to_headings_when_it_lists_no_links():
    programs = _scrape_programs(_soup(BACHELOR_HTML), _soup(MASTER_HTML))

    assert programs[-1] == {
        "name": "Informacinių sistemų inžinerijos magistrantūros studijos",
        "degree": "Magistras",
    }


def test_the_fallback_of_one_page_is_not_silenced_by_the_other_pages_links():
    # The bachelor page yields four links; the master page yields
    # none, and its heading fallback still has to run
    programs = _scrape_programs(_soup(BACHELOR_HTML), _soup(MASTER_HTML))

    assert any(p["degree"] == "Magistras" for p in programs)
    assert any(p["degree"] == "Bakalauras" for p in programs)


def test_programs_of_two_pages_that_never_downloaded_are_empty():
    assert _scrape_programs(None, None) == []


def test_a_programme_page_without_a_known_container_is_skipped():
    html = "<html><body><div class='sidebar'><a href='/studijos/x'>Ilgas pavadinimas</a></div></body></html>"

    assert _scrape_programs(_soup(html), None) == []


def test_a_page_with_neither_links_nor_headings_yields_nothing():
    html = "<div class='article-content'><p>Priėmimas vyksta kasmet</p></div>"

    assert _scrape_programs(_soup(html), None) == []


def test_the_card_wording_overrides_the_page_it_was_listed_on():
    assert _program_entry("X", "Bakalauras", "magistrantūros studijos")["degree"] == "Magistras"
    assert _program_entry("X", "Bakalauras", "magistro laipsnis")["degree"] == "Magistras"
    assert _program_entry("X", "Magistras", "bakalauro studijos")["degree"] == "Bakalauras"


def test_the_page_degree_stands_when_the_card_says_nothing():
    assert _program_entry("X", "Magistras", "")["degree"] == "Magistras"
    assert _program_entry("X", "Bakalauras", None)["degree"] == "Bakalauras"


def test_a_year_in_the_card_is_not_a_course_length():
    entry = _program_entry("X", "Bakalauras", "Priėmimas 2026 m. rudenį")

    assert "duration" not in entry


def test_a_short_duration_spelling_is_understood():
    assert _program_entry("X", "Magistras", "trukmė 2 m.")["duration"] == "2 metai"
    assert _program_entry("X", "Bakalauras", "4 metų trukmės")["duration"] == "4 metai"


def test_card_text_prefers_the_surrounding_card():
    html = "<div class='article-content'><li><a href='/studijos/x'>Ilgas pavadinimas</a> <span>4 metai</span></li></div>"
    link = _soup(html).find("a")

    assert _card_text(link) == "Ilgas pavadinimas 4 metai"


def test_card_text_stops_at_a_listing_holding_several_programmes():
    html = """<div class='article-content'><ul>
        <li><a href='/studijos/x'>Pirma programa</a></li>
        <li><a href='/studijos/y'>Antra programa</a> <span>3 metai</span></li>
    </ul></div>"""
    link = _soup(html).find("a")

    assert _card_text(link) == "Pirma programa"


def test_card_text_falls_back_to_the_link_when_no_ancestor_adds_anything():
    html = "<div class='article-content'><div><span><em><a href='/studijos/x'>Ilgas pavadinimas</a></em></span></div></div>"
    link = _soup(html).find("a")

    assert _card_text(link) == "Ilgas pavadinimas"


def test_card_text_survives_a_link_with_no_parent_left():
    link = _soup("<div class='article-content'><a href='/studijos/x'>Ilgas pavadinimas</a></div>").find("a")
    link.extract()

    assert _card_text(link) == "Ilgas pavadinimas"








# -----------------------------------------------------------
# _scrape_staff — the structure page
# -----------------------------------------------------------


def test_staff_are_grouped_under_their_department():
    departments = _scrape_staff(_soup(STRUCTURE_HTML))

    assert [d["department"] for d in departments] == ["Informatikos katedra", "Verslo katedra"]


def test_a_staff_entry_carries_the_name_email_and_the_role_after_the_comma():
    departments = _scrape_staff(_soup(STRUCTURE_HTML))

    assert departments[0]["staff"][0] == {
        "name": "Prof. dr. Jonas Jonaitis",
        "email": "jonas.jonaitis@knf.vu.lt",
        "position": "katedros vedėjas",
    }


def test_a_faculty_landline_on_a_staff_line_is_kept():
    departments = _scrape_staff(_soup(STRUCTURE_HTML))

    assert departments[0]["staff"][1] == {
        "name": "Doc. dr. Ona Onaitė",
        "phone": "+370 37 422 604",
        "position": "prodekanė",
    }


def test_a_paragraph_inside_a_list_item_is_read_once():
    departments = _scrape_staff(_soup(STRUCTURE_HTML))
    names = [person["name"] for person in departments[0]["staff"]]

    assert names.count("Doc. dr. Ona Onaitė") == 1


def test_a_department_without_people_is_dropped():
    departments = _scrape_staff(_soup(STRUCTURE_HTML))

    assert "Tuščias skyrius" not in [d["department"] for d in departments]


def test_a_title_hiding_inside_another_word_is_not_a_staff_entry():
    departments = _scrape_staff(_soup(STRUCTURE_HTML))
    names = [person["name"] for d in departments for person in d["staff"]]

    assert not any(name.startswith("Buveinės") for name in names)


def test_a_sentence_mentioning_a_title_without_a_name_is_not_a_staff_entry():
    departments = _scrape_staff(_soup(STRUCTURE_HTML))
    names = [person["name"] for d in departments for person in d["staff"]]

    assert not any(name.startswith("Studijų dalykų") for name in names)


def test_contact_details_after_the_comma_are_not_read_as_a_position():
    departments = _scrape_staff(_soup(STRUCTURE_HTML))
    rasa = departments[1]["staff"][0]

    assert rasa["email"] == "verslas@knf.vu.lt"
    assert "position" not in rasa


def test_a_paragraph_of_prose_is_not_a_staff_entry():
    html = f"<div class='item-page'><h2>Katedra</h2><p>Prof. dr. Jonas Jonaitis {'labai ilgas tekstas ' * 15}</p></div>"

    assert _scrape_staff(_soup(html)) == []


def test_a_line_too_short_to_name_anybody_is_skipped():
    html = "<div class='item-page'><h2>Katedra</h2><p>dr</p><p></p></div>"

    assert _scrape_staff(_soup(html)) == []


def test_an_empty_heading_does_not_open_a_department():
    html = """<div class='item-page'>
        <h2>Informatikos katedra</h2>
        <h3></h3>
        <p>Prof. dr. Jonas Jonaitis, dekanas</p>
    </div>"""
    departments = _scrape_staff(_soup(html))

    assert [d["department"] for d in departments] == ["Informatikos katedra"]


def test_staff_of_a_page_that_never_downloaded_are_empty():
    assert _scrape_staff(None) == []


def test_staff_of_a_page_without_a_known_container_are_empty():
    html = "<html><body><div class='sidebar'><p>Prof. dr. Jonas Jonaitis, dekanas</p></div></body></html>"

    assert _scrape_staff(_soup(html)) == []


def test_a_name_needs_two_capitalised_words():
    assert _name_shaped("Prof. dr. Jonas Jonaitis") is True
    assert _name_shaped("Studijų dalykų aprašai") is False
    assert _name_shaped("") is False








# -----------------------------------------------------------
# _scrape_general_contact — the front page footer block
# -----------------------------------------------------------


def test_the_front_page_yields_the_address_phone_and_email():
    assert _scrape_general_contact(_soup(MAIN_HTML)) == {
        "address": "Muitinės g. 8, LT-44280 Kaunas",
        "phone": "+370 37 422 523",
        "email": "knf@knf.vu.lt",
    }


def test_the_phone_comes_from_the_contact_block_not_the_first_number_on_the_page():
    general = _scrape_general_contact(_soup(MAIN_HTML))

    assert general["phone"] != "+370 612 34567"


def test_an_address_split_across_elements_is_still_read():
    html = "<html><body><p>Muitinės g. 8</p><p>LT-44280 Kaunas</p></body></html>"

    assert _scrape_general_contact(_soup(html))["address"] == "Muitinės g. 8\nLT-44280 Kaunas"


def test_info_at_is_used_when_the_page_carries_no_knf_at():
    html = "<html><body><footer><p>info@knf.vu.lt</p></footer></body></html>"

    assert _scrape_general_contact(_soup(html)) == {"email": "info@knf.vu.lt"}


def test_a_page_without_a_footer_still_yields_an_email_from_its_text():
    html = "<html><body><p>Rašykite knf@knf.vu.lt</p></body></html>"

    assert _scrape_general_contact(_soup(html)) == {"email": "knf@knf.vu.lt"}


def test_the_contact_block_is_found_by_a_class_that_mentions_contacts():
    html = "<html><body><div class='page-contacts'><p>Tel. +370 37 422 523</p></div></body></html>"

    assert _scrape_general_contact(_soup(html)) == {"phone": "+370 37 422 523"}


def test_a_private_general_email_is_not_republished():
    html = "<html><body><footer><p>knf@gmail.com</p></footer></body></html>"

    assert _scrape_general_contact(_soup(html)) == {}


def test_a_mobile_number_in_the_footer_is_not_the_switchboard():
    html = "<html><body><footer><p>Tel. +370 612 34567</p></footer></body></html>"

    assert _scrape_general_contact(_soup(html)) == {}


def test_general_contact_of_a_page_that_never_downloaded_is_empty():
    assert _scrape_general_contact(None) == {}


def test_a_front_page_with_nothing_matching_yields_an_empty_dict():
    assert _scrape_general_contact(_soup("<html><body><p>Sveiki</p></body></html>")) == {}








# -----------------------------------------------------------
# A full run — what lands in faculty_info and scraper_runs
# -----------------------------------------------------------


def test_a_full_run_reports_the_pages_contacts_and_programmes_it_found(app, http):
    _serve_all(http)

    result = scrape_faculty_info()

    assert result["pages_scraped"] == 5
    assert result["programs_found"] == 4
    # 5 from the contacts page + 4 people folded in from the structure page
    assert result["contacts_found"] == 9
    assert "error" not in result
    assert uuid.UUID(result["runId"])


def test_a_full_run_stores_the_three_lithuanian_sections(app, db, http):
    _serve_all(http)

    scrape_faculty_info()

    sections = _sections(db)
    assert set(sections) == {"contacts", "programs", "general_contact"}
    assert [c["category"] for c in sections["contacts"]][:3] == ["Kontaktai", "Dekanatas", "Paslaugos"]
    assert sections["programs"][0]["name"] == "Informatika ir skaitmeninis turinys"
    assert sections["general_contact"]["email"] == "knf@knf.vu.lt"


def test_the_departments_are_folded_into_the_contacts_blob(app, db, http):
    _serve_all(http)

    scrape_faculty_info()

    contacts = _sections(db)["contacts"]
    katedra = _category(contacts, "Informatikos katedra")
    assert katedra is not None
    assert katedra["items"][0] == {
        "name": "Prof. dr. Jonas Jonaitis",
        "email": "jonas.jonaitis@knf.vu.lt",
        "position": "katedros vedėjas",
    }


def test_a_folded_staff_entry_never_carries_a_room(app, db, http):
    _serve_all(http)

    scrape_faculty_info()

    katedra = _category(_sections(db)["contacts"], "Verslo katedra")
    assert all("room" not in item for item in katedra["items"])


def test_a_department_whose_people_all_dropped_out_adds_no_category(app, db, http, monkeypatch):
    monkeypatch.setattr(info_scraper, "_scrape_staff",
                        lambda soup: [{"department": "Tuščia katedra", "staff": []}])
    _serve_all(http)

    scrape_faculty_info()

    assert _category(_sections(db)["contacts"], "Tuščia katedra") is None


def test_the_run_row_is_completed_with_what_it_found(app, db, http):
    _serve_all(http)

    result = scrape_faculty_info()

    row = _runs(db)[-1]
    assert row["id"] == result["runId"]
    assert row["status"] == "completed"
    assert row["articles_found"] == 13
    assert row["articles_new"] == 3
    assert row["error_message"] is None
    assert row["finished_at"] is not None


def test_the_scrape_writes_lithuanian_rows_and_only_lithuanian_rows(app, db, http):
    _serve_all(http)

    scrape_faculty_info()

    langs = [row["lang"] for row in db.execute("SELECT lang FROM faculty_info").fetchall()]
    assert set(langs) == {"lt"}


def test_lithuanian_letters_are_stored_unescaped(app, db, http):
    _serve_all(http)

    scrape_faculty_info()

    stored = db.execute(
        "SELECT data_json FROM faculty_info WHERE lang = 'lt' AND section = 'contacts'"
    ).fetchone()["data_json"]
    assert "Studijų skyrius" in stored
    assert "\\u" not in stored


def test_the_scraped_stamp_is_naive_utc(app, db, http):
    _serve_all(http)

    with time_machine.travel(datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc), tick=False):
        scrape_faculty_info()

    stamps = {row["scraped_at"] for row in db.execute("SELECT scraped_at FROM faculty_info").fetchall()}
    assert stamps == {"2026-08-29T09:30:00"}








# -----------------------------------------------------------
# Idempotency — a second run must not duplicate anything
# -----------------------------------------------------------


def test_re_running_the_scrape_does_not_duplicate_a_section(app, db, http):
    _serve_all(http)

    scrape_faculty_info()
    first_ids = {row["section"]: row["id"]
                 for row in db.execute("SELECT id, section FROM faculty_info").fetchall()}
    scrape_faculty_info()

    rows = db.execute("SELECT id, section FROM faculty_info").fetchall()
    assert len(rows) == 3
    assert {row["section"]: row["id"] for row in rows} == first_ids


def test_an_unchanged_second_run_counts_no_changed_sections(app, db, http):
    _serve_all(http)

    scrape_faculty_info()
    scrape_faculty_info()

    runs = _runs(db)
    assert [row["articles_new"] for row in runs] == [3, 0]
    assert [row["status"] for row in runs] == ["completed", "completed"]


def test_an_unchanged_section_still_has_its_stamp_refreshed(app, db, http):
    _serve_all(http)
    monday = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)

    with time_machine.travel(monday, tick=False):
        scrape_faculty_info()
    with time_machine.travel(monday + timedelta(days=1), tick=False):
        scrape_faculty_info()

    stamps = {row["scraped_at"] for row in db.execute("SELECT scraped_at FROM faculty_info").fetchall()}
    assert stamps == {"2026-08-25T06:00:00"}


def test_only_the_section_that_changed_counts_as_new(app, db, http):
    _serve_all(http)
    scrape_faculty_info()

    http.reset()
    _serve(http, main=MAIN_HTML, structure=STRUCTURE_HTML,
           contacts=CONTACTS_HTML.replace("Bendra informacija", "Bendras priimamasis"),
           bachelor=BACHELOR_HTML, master=MASTER_HTML)
    scrape_faculty_info()

    assert [row["articles_new"] for row in _runs(db)] == [3, 1]
    assert "Bendras priimamasis" in json.dumps(_sections(db)["contacts"], ensure_ascii=False)








# -----------------------------------------------------------
# Partial failure — the register blocker
# -----------------------------------------------------------


def test_a_dead_contacts_page_does_not_cost_the_other_sections(app, db, http):
    # The blocker in one test: before the per-section guards a
    # single 404 here failed the whole run and stored nothing
    _serve(http, main=MAIN_HTML, structure=STRUCTURE_HTML, contacts=None,
           bachelor=BACHELOR_HTML, master=MASTER_HTML)

    result = scrape_faculty_info()

    sections = _sections(db)
    assert result["pages_scraped"] == 4
    assert "error" not in result
    assert sections["programs"][0]["name"] == "Informatika ir skaitmeninis turinys"
    assert sections["general_contact"]["email"] == "knf@knf.vu.lt"
    # The contacts blob survives on the folded departments alone
    assert [c["category"] for c in sections["contacts"]] == ["Informatikos katedra", "Verslo katedra"]
    assert _runs(db)[-1]["status"] == "completed"


def test_a_dead_structure_page_costs_only_the_departments(app, db, http):
    _serve(http, main=MAIN_HTML, structure=None, contacts=CONTACTS_HTML,
           bachelor=BACHELOR_HTML, master=MASTER_HTML)

    scrape_faculty_info()

    contacts = _sections(db)["contacts"]
    assert [c["category"] for c in contacts] == ["Kontaktai", "Dekanatas", "Paslaugos"]
    assert _runs(db)[-1]["status"] == "completed"


def test_dead_programme_pages_cost_only_the_programmes(app, db, http):
    _serve(http, main=MAIN_HTML, structure=STRUCTURE_HTML, contacts=CONTACTS_HTML,
           bachelor=None, master=None)

    result = scrape_faculty_info()

    assert result["programs_found"] == 0
    assert "programs" not in _sections(db)
    assert "contacts" in _sections(db)


def test_a_dead_front_page_costs_only_the_general_contact(app, db, http):
    _serve(http, main=None, structure=STRUCTURE_HTML, contacts=CONTACTS_HTML,
           bachelor=BACHELOR_HTML, master=MASTER_HTML)

    scrape_faculty_info()

    assert "general_contact" not in _sections(db)
    assert "contacts" in _sections(db)
    assert "programs" in _sections(db)


def test_a_page_served_as_a_pdf_is_treated_as_a_dead_page(app, db, http):
    _serve(http, main=MAIN_HTML, structure=STRUCTURE_HTML, bachelor=BACHELOR_HTML, master=MASTER_HTML)
    http.add(responses.GET, PAGE_URLS["contacts"], body=b"%PDF-1.4", status=200,
             content_type="application/pdf")

    result = scrape_faculty_info()

    assert result["pages_scraped"] == 4
    assert _runs(db)[-1]["status"] == "completed"
    assert "programs" in _sections(db)


def test_a_transport_failure_on_one_page_does_not_fail_the_run(app, db, http):
    _serve(http, main=MAIN_HTML, structure=STRUCTURE_HTML, bachelor=BACHELOR_HTML, master=MASTER_HTML)
    http.add(responses.GET, PAGE_URLS["contacts"],
             body=requests.exceptions.ConnectionError("nutrūko ryšys"))

    result = scrape_faculty_info()

    assert result["pages_scraped"] == 4
    assert _runs(db)[-1]["status"] == "completed"


def test_every_page_dead_stores_nothing_and_still_completes(app, db, http):
    _serve(http)

    result = scrape_faculty_info()

    assert result == {"pages_scraped": 0, "contacts_found": 0, "programs_found": 0,
                      "runId": result["runId"]}
    assert _sections(db) == {}
    row = _runs(db)[-1]
    assert row["status"] == "completed"
    assert row["articles_found"] == 0
    assert row["articles_new"] == 0


def test_a_bad_run_leaves_the_previous_good_blobs_in_place(app, db, http):
    _serve_all(http)
    scrape_faculty_info()
    before = _sections(db)

    http.reset()
    _serve(http)
    scrape_faculty_info()

    assert _sections(db) == before
    assert [row["status"] for row in _runs(db)] == ["completed", "completed"]


def test_a_section_that_throws_is_named_in_the_run_error_without_failing_it(app, db, http, monkeypatch):
    def _boom(soup):
        raise ValueError("kontaktų puslapio struktūra pasikeitė")

    monkeypatch.setattr(info_scraper, "_scrape_contacts", _boom)
    _serve_all(http)

    result = scrape_faculty_info()

    row = _runs(db)[-1]
    assert row["status"] == "completed"
    assert "contacts:" in row["error_message"]
    assert "struktūra pasikeitė" in row["error_message"]
    assert "error" not in result
    # The sections that did not throw still stored
    assert "programs" in _sections(db)
    assert "general_contact" in _sections(db)


def test_two_broken_sections_are_both_named(app, db, http, monkeypatch):
    monkeypatch.setattr(info_scraper, "_scrape_contacts",
                        lambda soup: (_ for _ in ()).throw(ValueError("a")))
    monkeypatch.setattr(info_scraper, "_scrape_programs",
                        lambda b, m: (_ for _ in ()).throw(ValueError("b")))
    _serve_all(http)

    scrape_faculty_info()

    message = _runs(db)[-1]["error_message"]
    assert message.startswith("contacts: a; programs: b")


def test_a_broken_general_contact_extractor_keeps_the_stored_block(app, db, http, monkeypatch):
    _serve_all(http)
    scrape_faculty_info()

    monkeypatch.setattr(info_scraper, "_scrape_general_contact",
                        lambda soup: (_ for _ in ()).throw(RuntimeError("pagrindinis puslapis")))
    scrape_faculty_info()

    assert _sections(db)["general_contact"]["email"] == "knf@knf.vu.lt"
    assert "general_contact:" in _runs(db)[-1]["error_message"]








# -----------------------------------------------------------
# general_contact — scraped over stored over defaults
# -----------------------------------------------------------


def test_the_general_block_is_filled_out_with_the_hardcoded_defaults(app, db, http):
    _serve(http, main="<html><body><footer><p>knf@knf.vu.lt</p></footer></body></html>",
           structure=STRUCTURE_HTML, contacts=CONTACTS_HTML,
           bachelor=BACHELOR_HTML, master=MASTER_HTML)

    scrape_faculty_info()

    assert _sections(db)["general_contact"] == {
        "address": GENERAL_CONTACT_DEFAULTS["address"],
        "phone": GENERAL_CONTACT_DEFAULTS["phone"],
        "email": "knf@knf.vu.lt",
    }


def test_a_stored_field_survives_a_run_that_did_not_find_it(app, db, http):
    _serve_all(http)
    scrape_faculty_info()

    http.reset()
    _serve(http, main="<html><body><p>Muitinės g. 8, LT-44280 Kaunas</p></body></html>",
           structure=STRUCTURE_HTML, contacts=CONTACTS_HTML,
           bachelor=BACHELOR_HTML, master=MASTER_HTML)
    scrape_faculty_info()

    general = _sections(db)["general_contact"]
    assert general["phone"] == "+370 37 422 523"
    assert general["email"] == "knf@knf.vu.lt"


def test_a_scraped_field_wins_over_the_stored_one(app, db, http):
    _serve_all(http)
    scrape_faculty_info()

    http.reset()
    _serve(http, main=MAIN_HTML.replace("knf@knf.vu.lt", "info@knf.vu.lt"),
           structure=STRUCTURE_HTML, contacts=CONTACTS_HTML,
           bachelor=BACHELOR_HTML, master=MASTER_HTML)
    scrape_faculty_info()

    assert _sections(db)["general_contact"]["email"] == "info@knf.vu.lt"








# -----------------------------------------------------------
# Run bookkeeping — the lock, the budget, failures, retention
# -----------------------------------------------------------


def test_a_second_run_steps_aside_while_one_is_going(app, db, http):
    assert info_scraper._RUN_LOCK.acquire(blocking=False)
    try:
        result = scrape_faculty_info()
    finally:
        info_scraper._RUN_LOCK.release()

    assert result == {"pages_scraped": 0, "contacts_found": 0, "programs_found": 0, "skipped": True}
    assert _runs(db) == []
    assert len(http.calls) == 0


def test_the_lock_is_released_again_after_a_run(app, http):
    _serve_all(http)

    scrape_faculty_info()

    assert info_scraper._RUN_LOCK.acquire(blocking=False)
    info_scraper._RUN_LOCK.release()


def test_a_run_out_of_budget_fetches_nothing_and_still_closes(app, db, http, monkeypatch):
    monkeypatch.setattr(info_scraper, "RUN_BUDGET_SECONDS", -1)
    _serve_all(http)

    result = scrape_faculty_info()

    assert result["pages_scraped"] == 0
    assert len(http.calls) == 0
    assert _runs(db)[-1]["status"] == "completed"


def test_a_storage_failure_marks_the_run_failed(app, db, http, monkeypatch):
    def _boom(db_conn, lang, data, scraped_at):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(info_scraper, "_store_info", _boom)
    _serve_all(http)

    result = scrape_faculty_info()

    assert result["error"] == "database is locked"
    assert result["pages_scraped"] == 0
    row = _runs(db)[-1]
    assert row["id"] == result["runId"]
    assert row["status"] == "failed"
    assert row["error_message"] == "database is locked"


def test_a_failure_rolls_the_pending_section_writes_back(app, db, http, monkeypatch):
    # _store_info commits once at the end, so a crash after some
    # sections were written must leave NOTHING behind
    def _half_written(db_conn, lang, data, scraped_at):
        db_conn.execute(
            "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), lang, "contacts", "[]", scraped_at),
        )
        raise RuntimeError("saugykla neatsakė")

    monkeypatch.setattr(info_scraper, "_store_info", _half_written)
    _serve_all(http)

    scrape_faculty_info()

    assert _sections(db) == {}


def test_a_rollback_on_a_broken_connection_still_records_the_failure(app, db, http, monkeypatch):
    # The connection in hand is exactly what may have broken, so
    # the failure is recorded on a fresh one
    def _close_then_fail(db_conn, lang, data, scraped_at):
        db_conn.close()
        raise RuntimeError("ryšys su duomenų baze nutrūko")

    monkeypatch.setattr(info_scraper, "_store_info", _close_then_fail)
    _serve_all(http)

    result = scrape_faculty_info()

    assert result["error"] == "ryšys su duomenų baze nutrūko"
    assert _runs(db)[-1]["status"] == "failed"


def test_a_run_prunes_run_rows_older_than_thirty_days(app, db, http):
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    for started, source in ((old, "knf.vu.lt/info"), (older, "knf.vu.lt/info"), (old, "vu.lt")):
        db.execute(
            "INSERT INTO scraper_runs (id, source, status, started_at) VALUES (?, ?, 'completed', ?)",
            (str(uuid.uuid4()), source, started),
        )
    db.commit()
    _serve_all(http)

    scrape_faculty_info()

    assert len(_runs(db)) == 1
    # The newest row of every OTHER source survives whatever its age
    assert len(_runs(db, "vu.lt")) == 1








# -----------------------------------------------------------
# _extract_section / _stored_section / _store_info
# -----------------------------------------------------------


def test_extract_section_passes_a_healthy_value_through():
    assert _extract_section("contacts", lambda soup: ["ok"], None) == (["ok"], None)


def test_extract_section_turns_a_crash_into_a_named_error():
    def _boom(a, b):
        raise ValueError("blogas puslapis")

    value, error = _extract_section("programs", _boom, None, None)

    assert value is None
    assert error == "programs: blogas puslapis"


def test_a_section_never_stored_reads_back_as_an_empty_dict(app):
    conn = info_scraper.get_db()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {}
    finally:
        conn.close()


def test_a_stored_section_reads_back_as_its_dict(app):
    conn = info_scraper.get_db()
    try:
        _store_info(conn, "lt", {"general_contact": {"email": "knf@knf.vu.lt"}}, "2026-08-29T09:30:00")
        assert _stored_section(conn, "lt", "general_contact") == {"email": "knf@knf.vu.lt"}
    finally:
        conn.close()


def test_a_stored_blob_that_is_not_json_reads_back_as_an_empty_dict(app, db):
    db.execute(
        "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at) VALUES (?, 'lt', ?, ?, ?)",
        (str(uuid.uuid4()), "general_contact", "{ne json", "2026-08-29T09:30:00"),
    )
    db.commit()

    conn = info_scraper.get_db()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {}
    finally:
        conn.close()


def test_a_stored_blob_that_is_not_an_object_reads_back_as_an_empty_dict(app, db):
    db.execute(
        "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at) VALUES (?, 'lt', ?, ?, ?)",
        (str(uuid.uuid4()), "general_contact", "[1, 2]", "2026-08-29T09:30:00"),
    )
    db.commit()

    conn = info_scraper.get_db()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {}
    finally:
        conn.close()


def test_a_corrupt_stored_general_block_does_not_stop_the_next_run(app, db, http):
    db.execute(
        "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at) VALUES (?, 'lt', ?, ?, ?)",
        (str(uuid.uuid4()), "general_contact", "{ne json", "2026-08-29T09:30:00"),
    )
    db.commit()
    _serve_all(http)

    scrape_faculty_info()

    assert _sections(db)["general_contact"]["email"] == "knf@knf.vu.lt"


def test_store_info_counts_a_new_section_as_changed(app):
    conn = info_scraper.get_db()
    try:
        changed = _store_info(conn, "lt", {"contacts": [{"category": "X", "items": []}]},
                              "2026-08-29T09:30:00")
        assert changed == 1
    finally:
        conn.close()


def test_store_info_updates_a_section_in_place(app, db):
    conn = info_scraper.get_db()
    try:
        _store_info(conn, "lt", {"programs": [{"name": "A"}]}, "2026-08-29T09:30:00")
        first = db.execute("SELECT id FROM faculty_info WHERE section = 'programs'").fetchone()["id"]
        changed = _store_info(conn, "lt", {"programs": [{"name": "B"}]}, "2026-08-30T09:30:00")
    finally:
        conn.close()

    rows = db.execute("SELECT id, data_json, scraped_at FROM faculty_info WHERE section = 'programs'").fetchall()
    assert changed == 1
    assert len(rows) == 1
    assert rows[0]["id"] == first
    assert json.loads(rows[0]["data_json"]) == [{"name": "B"}]


def test_store_info_refreshes_an_unchanged_section_without_counting_it(app, db):
    conn = info_scraper.get_db()
    try:
        _store_info(conn, "lt", {"programs": [{"name": "A"}]}, "2026-08-29T09:30:00")
        changed = _store_info(conn, "lt", {"programs": [{"name": "A"}]}, "2026-08-30T09:30:00")
    finally:
        conn.close()

    row = db.execute("SELECT scraped_at FROM faculty_info WHERE section = 'programs'").fetchone()
    assert changed == 0
    assert row["scraped_at"] == "2026-08-30T09:30:00"


def test_store_info_writes_one_row_per_section(app, db):
    conn = info_scraper.get_db()
    try:
        changed = _store_info(conn, "lt", {"contacts": [], "programs": [], "general_contact": {}},
                              "2026-08-29T09:30:00")
    finally:
        conn.close()

    assert changed == 3
    assert db.execute("SELECT COUNT(*) FROM faculty_info").fetchone()[0] == 3








# -----------------------------------------------------------
# POST /api/scraper/info — the admin trigger
# -----------------------------------------------------------


def test_a_guest_cannot_trigger_the_info_scrape(client, http):
    response = client.post(TRIGGER)

    assert response.status_code == 401
    assert len(http.calls) == 0


def test_a_student_cannot_trigger_the_info_scrape(client, actor, http):
    _user, headers = actor

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 403
    assert len(http.calls) == 0


def test_an_admin_trigger_runs_the_scrape(client, admin, db, http):
    _user, headers = admin
    _serve_all(http)

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body["pages_scraped"] == 5
    assert body["contacts_found"] == 9
    assert body["programs_found"] == 4
    assert "contacts" in _sections(db)


def test_a_trigger_that_finds_the_lock_held_answers_409(client, admin, http):
    _user, headers = admin
    assert info_scraper._RUN_LOCK.acquire(blocking=False)
    try:
        response = client.post(TRIGGER, headers=headers)
    finally:
        info_scraper._RUN_LOCK.release()

    assert response.status_code == 409
    assert response.get_json()["skipped"] is True


def test_a_failing_scrape_answers_502_with_a_stable_slug(client, admin, http, monkeypatch):
    def _boom(db_conn, lang, data, scraped_at):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(info_scraper, "_store_info", _boom)
    _user, headers = admin
    _serve_all(http)

    response = client.post(TRIGGER, headers=headers)

    assert response.status_code == 502
    body = response.get_json()
    assert body["error"] == "scrape_failed"
    # The exception text belongs in the log and the run row, not
    # in an HTTP body
    assert "database is locked" not in json.dumps(body)








# -----------------------------------------------------------
# GET /api/info — what the mobile Info screen actually receives
# -----------------------------------------------------------


@pytest.mark.contract
def test_the_scraped_contacts_reach_the_info_screen(client, http):
    _serve_all(http)
    scrape_faculty_info()

    payload = client.get(f"{INFO}?lang=lt").get_json()

    categories = [c["category"] for c in payload["contacts"]]
    assert "Dekanatas" in categories
    assert "Informatikos katedra" in categories
    # The wire shape the mobile FacultyInfoResponse maps over
    for category in payload["contacts"]:
        assert set(category) == {"category", "items"}
        for item in category["items"]:
            assert "name" in item
            assert set(item) <= {"name", "phone", "email", "room", "position"}
    assert payload["updatedAt"]


@pytest.mark.contract
def test_an_english_request_never_serves_a_different_scraped_dataset(client, db, http):
    _serve_all(http)
    scrape_faculty_info()

    lt = client.get(f"{INFO}?lang=lt").get_json()
    en = client.get(f"{INFO}?lang=en").get_json()

    # Nothing ever writes lang='en', so English borrows the very
    # same rows instead of quietly serving a second dataset
    assert db.execute("SELECT COUNT(*) FROM faculty_info WHERE lang = 'en'").fetchone()[0] == 0
    assert en["lang"] == "en"
    assert en["contacts"] == lt["contacts"]
    assert en["programs"] == lt["programs"]
    assert en["general_contact"] == lt["general_contact"]
    assert en["updatedAt"] == lt["updatedAt"]
    # ...while the curated English half stays English
    assert en["faq"] != lt["faq"]
    assert en["links"] != lt["links"]
    assert en["faq"][0]["q"] == "How do I get my student ID card?"


def test_the_english_answer_no_longer_shows_the_hardcoded_english_contacts(client, http):
    _serve_all(http)
    scrape_faculty_info()

    payload = client.get(f"{INFO}?lang=en").get_json()

    assert "Dean's Office" not in [c["category"] for c in payload["contacts"]]


def test_a_month_old_scrape_is_ignored_by_the_info_screen(client, http):
    _serve_all(http)
    with time_machine.travel(datetime.now(timezone.utc) - timedelta(days=40), tick=False):
        scrape_faculty_info()

    payload = client.get(f"{INFO}?lang=lt").get_json()

    assert [c["category"] for c in payload["contacts"]] == ["Dekanatas", "Katedros", "Paslaugos"]
    assert "updatedAt" not in payload


def test_a_run_that_stored_nothing_leaves_the_curated_handbook_standing(client, http):
    _serve(http)
    scrape_faculty_info()

    payload = client.get(f"{INFO}?lang=lt").get_json()

    assert [c["category"] for c in payload["contacts"]] == ["Dekanatas", "Katedros", "Paslaugos"]
