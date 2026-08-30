# -----------------------------------------------------------
#  [*] Tests — info_scraper: the fetch, the front-page block
#      and the two storage helpers, exhaustively
#
#  A deep pass over five functions of
#  scraper/info_scraper.py — _fetch_page,
#  _scrape_general_contact, _stored_section, _store_info and
#  the run that ties them together, scrape_faculty_info.
#  test_scraper_info.py already proves the happy shapes; this
#  file walks the guards, the boundaries and the paths a
#  broken knf.vu.lt can actually produce:
#
#    - _fetch_page as the SSRF gate it is: the allowlist is
#      checked before a packet leaves and again after every
#      redirect, a non-HTML Content-Type is refused, a body
#      over the 2 MB cap is truncated and still parses, and a
#      page's own charset declaration is what decodes it —
#      every failure becoming None, never an exception
#    - _scrape_general_contact's search order: which of the
#      five contact-block selectors wins, that the phone is
#      read from that block ALONE, and the exact address
#      shapes the postcode regex accepts and refuses
#    - the two first-match-only holes that used to follow
#      from it, now fixed and pinned here: a non-faculty
#      number or a private knf@ address standing earlier in
#      the block no longer hides the real one behind it
#    - _stored_section against everything a data_json column
#      can actually hold: a scalar, a list, a bytes blob that
#      does not decode, a row that is missing, a lang that
#      differs only in case
#    - _store_info's change counting and its failure modes —
#      an empty payload, a value JSON cannot express, a NULL
#      stamp, a list instead of a dict, and the UNIQUE(lang,
#      section) race its hand-rolled upsert does not guard
#    - the run itself: a budget that expires mid-way, a page
#      that downloads empty, the lock released after a
#      failure, the three-layer general_contact merge, and a
#      section skipped leaving even the stored STAMP alone
#
#  Every fetch is fed by `responses`; the container has no
#  network. Nothing sleeps.
# -----------------------------------------------------------


import json
import sqlite3
import uuid

import pytest
import requests
import responses
from bs4 import BeautifulSoup

from app.scraper import info_scraper
from app.scraper.info_scraper import (
    GENERAL_CONTACT_DEFAULTS,
    _fetch_page,
    _scrape_general_contact,
    _store_info,
    _stored_section,
    scrape_faculty_info,
)


# A knf.vu.lt URL that is not one of the five INFO_PAGES, so
# these fetch tests can never collide with a run
PAGE = "https://knf.vu.lt/testinis-puslapis"

PAGE_URLS = {page["type"]: page["url"] for page in info_scraper.INFO_PAGES}

STAMP = "2026-08-29T09:30:00"




# -----------------------------------------------------------
# Fixture pages — the smallest markup that still yields one
# item per section, so the counts in the run tests are exact
# -----------------------------------------------------------

MAIN = """<html><body>
<p>Muitinės g. 8, LT-44280 Kaunas</p>
<footer><p>Tel. +370 37 422 523</p><p>knf@knf.vu.lt</p></footer>
</body></html>"""

CONTACTS = """<html><body><div class="article-content">
<h2>Dekanatas</h2>
<p>Studijų skyrius, studijos@knf.vu.lt, 102 kab.</p>
</div></body></html>"""

STRUCTURE = """<html><body><div class="item-page">
<h2>Informatikos katedra</h2>
<p>Prof. dr. Jonas Jonaitis, katedros vedėjas, jonas.jonaitis@knf.vu.lt</p>
</div></body></html>"""

BACHELOR = """<html><body><div class="article-content">
<ul><li><a href="/studijos/informatika">Informatika ir skaitmeninis turinys</a> <span>4 metai</span></li></ul>
</div></body></html>"""

MASTER = """<html><body><article>
<h3>Informacinių sistemų inžinerijos magistrantūros studijos</h3>
</article></body></html>"""




# -----------------------------------------------------------
# http
# -----------------------------------------------------------
#
# The `responses` fake. A URL nobody registered raises a
# transport error inside requests, which is exactly how this
# file spells "that page is dead".
#
# Used by:
#   - every _fetch_page test and every run test
# -----------------------------------------------------------

@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock




# -----------------------------------------------------------
# _html
# -----------------------------------------------------------
#
#   _html(http, PAGE, "<p>x</p>")   — 200 text/html
#
# Used by:
#   - the _fetch_page tests
# -----------------------------------------------------------

def _html(http, url, body, status=200, content_type="text/html; charset=utf-8"):
    http.add(responses.GET, url, body=body, status=status, content_type=content_type)


# -----------------------------------------------------------
# _serve
# -----------------------------------------------------------
#
#   _serve(http, main=MAIN)    — the front page only, the
#                                other four dead
#
# A page left out is simply never registered, so requesting
# it raises inside requests and reaches the scraper as None.
# The front page is registered under both its bare and its
# trailing-slash form, because requests normalises an empty
# path before sending.
#
# Used by:
#   - every scrape_faculty_info test
# -----------------------------------------------------------

def _serve(http, main=None, structure=None, contacts=None, bachelor=None, master=None):
    for page_type, body in (("main", main), ("structure", structure), ("contacts", contacts),
                            ("bachelor", bachelor), ("master", master)):
        if body is None:
            continue
        urls = [PAGE_URLS[page_type]]
        if page_type == "main":
            urls.append(PAGE_URLS[page_type] + "/")
        for url in urls:
            _html(http, url, body)


# -----------------------------------------------------------
# _serve_all — the five healthy pages in one call
# -----------------------------------------------------------

def _serve_all(http):
    _serve(http, main=MAIN, structure=STRUCTURE, contacts=CONTACTS,
           bachelor=BACHELOR, master=MASTER)


# -----------------------------------------------------------
# _gc — _scrape_general_contact over a body fragment
# -----------------------------------------------------------

def _gc(body):
    return _scrape_general_contact(BeautifulSoup(f"<html><body>{body}</body></html>", "lxml"))


# -----------------------------------------------------------
# _sections / _rows / _runs — what a run left in the database
# -----------------------------------------------------------

def _sections(db, lang="lt"):
    rows = db.execute("SELECT section, data_json FROM faculty_info WHERE lang = ?", (lang,)).fetchall()

    return {row["section"]: json.loads(row["data_json"]) for row in rows}


def _rows(db):
    return db.execute("SELECT * FROM faculty_info ORDER BY lang, section").fetchall()


def _runs(db, source="knf.vu.lt/info"):
    return db.execute(
        "SELECT * FROM scraper_runs WHERE source = ? ORDER BY started_at", (source,)
    ).fetchall()


# -----------------------------------------------------------
# _seed — one faculty_info row straight into the table
# -----------------------------------------------------------

def _seed(db, section, blob, lang="lt", scraped_at=STAMP):
    db.execute(
        "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), lang, section, blob, scraped_at),
    )
    db.commit()


# -----------------------------------------------------------
# _conn — the connection the scraper itself would open
# -----------------------------------------------------------
#
# get_db() reads the module-global path init_db() set, so
# every test using this must depend on the `app` fixture.
#
# Used by:
#   - the _stored_section and _store_info tests
# -----------------------------------------------------------

def _conn():
    return info_scraper.get_db()


# -----------------------------------------------------------
# _FakeRowDb
# -----------------------------------------------------------
#
# A stand-in database whose SELECT answers one handed-in row.
# The only way to reach _stored_section's TypeError arm: the
# column is NOT NULL, so a None data_json cannot be arranged
# through SQLite at all.
#
# Used by:
#   - the _stored_section decode tests
# -----------------------------------------------------------

class _FakeRowDb:

    def __init__(self, row):
        self.row = row

    def execute(self, sql, params=()):
        return self

    def fetchone(self):
        return self.row


# -----------------------------------------------------------
# _StaticCursor / _RacingDb
# -----------------------------------------------------------
#
# _store_info reads a (lang, section) row and then INSERTs
# when it saw none. _RacingDb plants a rival row in that gap
# — the SELECT is answered from a snapshot taken BEFORE the
# rival commits, so the INSERT that follows meets the
# UNIQUE(lang, section) index every time.
#
# Used by:
#   - the _store_info race test
# -----------------------------------------------------------

class _StaticCursor:

    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _RacingDb:

    def __init__(self, conn, rival, lang, section):
        self.conn = conn
        self.rival = rival
        self.lang = lang
        self.section = section
        self.raced = False

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            rows = self.conn.execute(sql, params).fetchall()
            if not self.raced:
                self.raced = True
                self.rival.execute(
                    "INSERT INTO faculty_info (id, lang, section, data_json, scraped_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), self.lang, self.section, "{}", STAMP),
                )
                self.rival.commit()
            return _StaticCursor(rows)

        return self.conn.execute(sql, params)

    def commit(self):
        self.conn.commit()








# -----------------------------------------------------------
# _fetch_page — the allowlist and the transport
# -----------------------------------------------------------


def test_a_healthy_page_becomes_a_parsed_soup(http):
    _html(http, PAGE, "<html><body><div class='article-content'><p>Sveiki</p></div></body></html>")

    soup = _fetch_page(PAGE)

    assert soup.select_one(".article-content p").get_text() == "Sveiki"


def test_the_page_own_charset_declaration_decodes_its_bytes(http):
    # The body comes back as BYTES on purpose, so a page served
    # as windows-1257 keeps its Lithuanian letters instead of
    # arriving through the ISO-8859-1 requests would assume
    markup = ('<html><head><meta charset="windows-1257"></head>'
              '<body><p>Muitinės g. 8, LT-44280 Kaunas</p></body></html>')
    _html(http, PAGE, markup.encode("windows-1257"), content_type="text/html")

    soup = _fetch_page(PAGE)

    assert soup.get_text(strip=True) == "Muitinės g. 8, LT-44280 Kaunas"


def test_a_utf8_page_without_a_charset_header_keeps_its_letters(http):
    markup = '<html><head><meta charset="utf-8"></head><body><p>Studijų skyrius</p></body></html>'
    _html(http, PAGE, markup.encode("utf-8"), content_type="text/html")

    assert "Studijų skyrius" in _fetch_page(PAGE).get_text()


def test_a_page_that_downloads_empty_is_still_a_soup(http):
    _html(http, PAGE, b"")

    soup = _fetch_page(PAGE)

    assert isinstance(soup, BeautifulSoup)
    assert soup.get_text() == ""


def test_markup_nobody_closed_is_parsed_rather_than_raised(http):
    _html(http, PAGE, "<html><body><div class='item-page'><p>Nebaigta<div><table><tr><td>x")

    soup = _fetch_page(PAGE)

    assert soup.select_one(".item-page") is not None


def test_a_body_that_is_not_markup_at_all_is_still_a_soup(http):
    _html(http, PAGE, "{\"json\": \"nors ir text/html\"}")

    assert "json" in _fetch_page(PAGE).get_text()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 429, 500, 502, 503])
def test_an_error_status_is_a_missing_page_and_never_an_exception(http, status):
    _html(http, PAGE, "<html><p>klaida</p>", status=status)

    assert _fetch_page(PAGE) is None


def test_a_transport_failure_is_a_missing_page(http):
    http.add(responses.GET, PAGE, body=requests.exceptions.ConnectionError("nutrūko ryšys"))

    assert _fetch_page(PAGE) is None


def test_a_page_nobody_answered_is_a_missing_page(http):
    assert _fetch_page(PAGE) is None


@pytest.mark.parametrize("content_type", [
    "application/pdf",
    "application/json",
    "text/plain",
    "image/png",
    "text/xml",
])
def test_a_body_declared_as_something_unparsable_is_refused(http, content_type):
    _html(http, PAGE, "<html><p>x</p>", content_type=content_type)

    assert _fetch_page(PAGE) is None


@pytest.mark.parametrize("content_type", [
    "text/html",
    "text/html; charset=windows-1257",
    "TEXT/HTML; charset=UTF-8",
    "  text/html ; charset=utf-8",
    "application/xhtml+xml",
])
def test_every_parsable_content_type_spelling_is_accepted(http, content_type):
    _html(http, PAGE, "<html><p id='t'>gerai</p></html>", content_type=content_type)

    assert _fetch_page(PAGE).select_one("#t") is not None


def test_a_server_that_declares_no_type_gets_the_benefit_of_the_doubt(http):
    _html(http, PAGE, "<html><p id='t'>gerai</p></html>", content_type=None)

    assert _fetch_page(PAGE).select_one("#t") is not None


@pytest.mark.parametrize("url", [
    "https://vu.lt/fakultetas",
    "https://tvarkarasciai.vu.lt/x",
    "https://knf.vu.lt.evil.example/kontaktai",
    "https://evil.example/knf.vu.lt",
    "http://169.254.169.254/latest/meta-data/",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "//knf.vu.lt/kontaktai",
    "/fakultetas/kontaktai",
    "",
    None,
])
def test_a_url_off_the_knf_allowlist_is_never_requested(http, url):
    assert _fetch_page(url) is None
    assert len(http.calls) == 0


@pytest.mark.parametrize("url", [
    "https://knf.vu.lt/kontaktai",
    "https://www.knf.vu.lt/kontaktai",
    "http://knf.vu.lt/kontaktai",
    "https://KNF.VU.LT/kontaktai",
])
def test_every_spelling_of_the_faculty_host_is_on_the_allowlist(http, url):
    _html(http, url, "<html><p id='t'>gerai</p></html>")

    assert _fetch_page(url).select_one("#t") is not None


def test_a_redirect_that_stays_on_the_allowlist_is_followed(http):
    http.add(responses.GET, PAGE, status=301,
             headers={"Location": "https://www.knf.vu.lt/perkeltas"}, content_type="text/html")
    _html(http, "https://www.knf.vu.lt/perkeltas", "<html><p id='t'>perkelta</p></html>")

    assert _fetch_page(PAGE).select_one("#t").get_text() == "perkelta"


def test_a_redirect_off_the_allowlist_yields_nothing(http):
    # The allowlist is checked AGAIN on the URL the response came
    # from: a page that redirects to another host must not be
    # parsed just because the first hop was legitimate
    http.add(responses.GET, PAGE, status=302,
             headers={"Location": "https://vu.lt/kitas"}, content_type="text/html")
    _html(http, "https://vu.lt/kitas", "<html><p id='t'>svetima</p></html>")

    assert _fetch_page(PAGE) is None


@pytest.mark.slow
def test_a_body_over_the_two_megabyte_cap_is_truncated_and_still_parses(http):
    body = "<html><body><p id='head'>pradžia</p>" + ("y" * 2_000_100) + "<p id='tail'>galas</p></body></html>"
    _html(http, PAGE, body)

    soup = _fetch_page(PAGE)

    assert soup.select_one("#head") is not None
    assert soup.select_one("#tail") is None


def test_a_non_string_url_is_the_one_shape_the_guard_does_not_absorb(http):
    # Documented boundary: every caller passes an INFO_PAGES
    # string, and host_allowed only absorbs a ValueError
    with pytest.raises(AttributeError):
        _fetch_page(7)

    assert len(http.calls) == 0








# -----------------------------------------------------------
# _scrape_general_contact — the address
# -----------------------------------------------------------


@pytest.mark.parametrize("written, expected", [
    ("Muitinės g. 8, LT-44280 Kaunas", "Muitinės g. 8, LT-44280 Kaunas"),
    ("Muitinės g. 8, 44280 Kaunas", "Muitinės g. 8, 44280 Kaunas"),
    ("Muitinės g 8, LT-44280 Kaunas", "Muitinės g 8, LT-44280 Kaunas"),
    ("Muitinės  g.  8  LT-44280  Kaunas", "Muitinės  g.  8  LT-44280  Kaunas"),
    ("Muitinės g. 8 (naujas korpusas), LT-44280 Kaunas",
     "Muitinės g. 8 (naujas korpusas), LT-44280 Kaunas"),
    ("Muitinės g. 8, LT-44280Kaunas", "Muitinės g. 8, LT-44280Kaunas"),
])
def test_every_accepted_spelling_of_the_faculty_address_is_read(written, expected):
    assert _gc(f"<p>{written}</p>") == {"address": expected}


@pytest.mark.parametrize("written", [
    "Muitinės g. 8, LT-4428 Kaunas",
    "Muitinės g. 8, LT-442800 Kaunas",
    "Muitinės g. 8, LT-44280 Vilnius",
    "Muitinės g. 8, LT-44280 kaunas",
    "muitinės g. 8, LT-44280 Kaunas",
    "Muitinės g., LT-44280 Kaunas",
    "Muitines g. 8, LT-44280 Kaunas",
    "Muitinės g. 8, Kaunas",
])
def test_a_line_that_is_not_the_faculty_address_is_not_read_as_one(written):
    assert _gc(f"<p>{written}</p>") == {}


def test_the_address_is_read_from_anywhere_on_the_page_not_only_the_contact_block():
    body = "<div class='hero'><p>Muitinės g. 8, LT-44280 Kaunas</p></div><footer><p>nieko</p></footer>"

    assert _gc(body) == {"address": "Muitinės g. 8, LT-44280 Kaunas"}


def test_the_first_address_on_the_page_is_the_one_that_is_stored():
    body = ("<p>Muitinės g. 8, LT-44280 Kaunas</p>"
            "<p>Muitinės g. 12, LT-44281 Kaunas</p>")

    assert _gc(body) == {"address": "Muitinės g. 8, LT-44280 Kaunas"}


def test_an_address_split_across_elements_keeps_the_element_boundary():
    # QUIRK worth knowing before it reaches a screen: the page
    # text is newline-joined, and the newline between the street
    # and the postcode is stored inside the address string
    assert _gc("<p>Muitinės g. 8,</p><p>LT-44280 Kaunas</p>") == {
        "address": "Muitinės g. 8,\nLT-44280 Kaunas",
    }


def test_a_comma_inside_the_street_line_stops_the_address_short():
    assert _gc("<p>Muitinės g. 8, II korpusas, LT-44280 Kaunas</p>") == {}








# -----------------------------------------------------------
# _scrape_general_contact — which block the phone comes from
# -----------------------------------------------------------


def test_the_footer_beats_every_other_contact_selector():
    # <footer> is first in the selector list, so its (Vilnius,
    # therefore dropped) number is the only one considered
    body = ("<footer><p>+370 5 268 7000</p></footer>"
            "<div class='footer'><p>+370 37 422 111</p></div>"
            "<div id='footer'><p>+370 37 422 222</p></div>"
            "<div class='contact'><p>+370 37 422 333</p></div>")

    assert _gc(body) == {}


def test_a_footer_class_is_used_when_the_page_has_no_footer_element():
    body = ("<div class='footer'><p>+370 37 422 111</p></div>"
            "<div id='footer'><p>+370 37 422 222</p></div>")

    assert _gc(body) == {"phone": "+370 37 422 111"}


def test_a_footer_id_is_used_when_there_is_no_footer_class():
    body = ("<div id='footer'><p>+370 37 422 111</p></div>"
            "<div class='contact-block'><p>+370 37 422 222</p></div>")

    assert _gc(body) == {"phone": "+370 37 422 111"}


def test_a_class_mentioning_contacts_is_used_before_an_id_mentioning_them():
    body = ("<div class='page-contacts'><p>+370 37 422 111</p></div>"
            "<div id='contact-us'><p>+370 37 422 222</p></div>")

    assert _gc(body) == {"phone": "+370 37 422 111"}


def test_an_id_mentioning_contacts_is_the_last_block_tried():
    assert _gc("<div id='main-contact-area'><p>+370 37 422 523</p></div>") == {
        "phone": "+370 37 422 523",
    }


def test_an_empty_footer_hides_the_contact_block_behind_it():
    # The selector list stops at the FIRST match, so a template
    # that renders an empty <footer> costs the phone entirely
    body = "<footer></footer><div class='contacts'><p>+370 37 422 523</p></div>"

    assert _gc(body) == {}


def test_a_landline_outside_the_contact_block_is_not_the_switchboard():
    body = "<p>Tel. +370 37 422 523</p><footer><p>Sekite mus</p></footer>"

    assert _gc(body) == {}


def test_the_old_national_spelling_is_read_from_the_contact_block():
    assert _gc("<footer><p>Tel. (8-37) 42 25 23</p></footer>") == {"phone": "(8-37) 42 25 23"}


def test_a_bare_number_without_a_plus_or_brackets_is_not_a_phone():
    assert _gc("<footer><p>Tel. 8 37 422 523</p></footer>") == {}


def test_a_page_with_no_contact_block_at_all_yields_no_phone():
    assert _gc("<p>+370 37 422 523</p>") == {}


def test_the_faculty_landline_is_found_even_when_another_number_comes_first():
    body = ("<footer><p>VU: +370 5 268 7000</p>"
            "<p>Kauno fakultetas: +370 37 422 523</p></footer>")

    assert _gc(body) == {"phone": "+370 37 422 523"}








# -----------------------------------------------------------
# _scrape_general_contact — the email
# -----------------------------------------------------------


def test_knf_at_wins_over_info_at_inside_the_same_block():
    assert _gc("<footer><p>info@knf.vu.lt</p><p>knf@knf.vu.lt</p></footer>") == {
        "email": "knf@knf.vu.lt",
    }


def test_the_contact_block_email_wins_over_the_one_in_the_page_body():
    body = "<p>info@knf.vu.lt</p><footer><p>knf@vu.lt</p></footer>"

    assert _gc(body) == {"email": "knf@vu.lt"}


def test_a_private_knf_address_in_the_block_falls_through_to_info_at():
    assert _gc("<footer><p>knf@gmail.com</p><p>info@knf.vu.lt</p></footer>") == {
        "email": "info@knf.vu.lt",
    }


def test_a_lookalike_domain_is_not_an_institutional_address():
    assert _gc("<footer><p>knf@knf.vu.lt.evil.example</p></footer>") == {}


@pytest.mark.parametrize("written, expected", [
    ("knf@knf.vu.lt", "knf@knf.vu.lt"),
    ("knf@vu.lt", "knf@vu.lt"),
    ("knf@stud.vu.lt", "knf@stud.vu.lt"),
    ("knf@knf.vu.lt.", "knf@knf.vu.lt"),
    ("knf@knf.vu.lt...", "knf@knf.vu.lt"),
])
def test_the_institutional_general_addresses_are_republished(written, expected):
    assert _gc(f"<footer><p>{written}</p></footer>") == {"email": expected}


@pytest.mark.parametrize("written", [
    "KNF@KNF.VU.LT",
    "Knf@knf.vu.lt",
    "kontaktai@knf.vu.lt",
    "knf @ knf.vu.lt",
])
def test_an_address_the_pattern_does_not_recognise_is_not_republished(written):
    assert _gc(f"<footer><p>{written}</p></footer>") == {}


def test_a_longer_local_part_ending_in_knf_is_not_the_general_mailbox():
    # The patterns carry a left-hand boundary: a department's
    # own address is not the faculty mailbox, and publishing
    # the truncated form put an address on /api/info that the
    # page never stated
    assert _gc("<footer><p>administracija-knf@knf.vu.lt</p></footer>") == {}


def test_an_institutional_address_later_on_the_page_is_not_shadowed():
    body = "<footer><p>knf@gmail.com</p></footer><p>Rašykite knf@knf.vu.lt</p>"

    assert _gc(body) == {"email": "knf@knf.vu.lt"}








# -----------------------------------------------------------
# _scrape_general_contact — the shape of the answer
# -----------------------------------------------------------


def test_a_front_page_that_matched_nothing_is_an_empty_dict_not_the_defaults():
    # The whole point of the empty dict: the caller's "non-empty
    # sections only" rule is what protects a stored good value
    assert _gc("<p>Sveiki atvykę</p>") == {}


def test_a_page_that_never_downloaded_is_an_empty_dict():
    assert _scrape_general_contact(None) == {}


def test_an_empty_document_is_an_empty_dict():
    assert _scrape_general_contact(BeautifulSoup(b"", "lxml")) == {}


def test_only_the_fields_the_page_stated_are_present():
    assert set(_gc("<footer><p>knf@knf.vu.lt</p></footer>")) == {"email"}


def test_a_front_page_stating_everything_yields_all_three_fields():
    assert _scrape_general_contact(BeautifulSoup(MAIN, "lxml")) == {
        "address": "Muitinės g. 8, LT-44280 Kaunas",
        "phone": "+370 37 422 523",
        "email": "knf@knf.vu.lt",
    }








# -----------------------------------------------------------
# _stored_section — reading the previous blob back
# -----------------------------------------------------------


def test_a_section_that_was_never_stored_reads_back_empty(app):
    conn = _conn()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {}
    finally:
        conn.close()


def test_a_stored_object_reads_back_whole(app, db):
    _seed(db, "general_contact", json.dumps({"email": "knf@knf.vu.lt", "hours": {"I-V": "8-17"}}))

    conn = _conn()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {
            "email": "knf@knf.vu.lt",
            "hours": {"I-V": "8-17"},
        }
    finally:
        conn.close()


def test_a_stored_empty_object_is_indistinguishable_from_no_row(app, db):
    _seed(db, "general_contact", "{}")

    conn = _conn()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {}
    finally:
        conn.close()


@pytest.mark.parametrize("blob", [
    '"tik tekstas"',
    "5",
    "5.5",
    "true",
    "false",
    "null",
    "[1, 2]",
    '[{"category": "X"}]',
])
def test_a_stored_blob_that_is_not_an_object_reads_back_empty(app, db, blob):
    _seed(db, "general_contact", blob)

    conn = _conn()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {}
    finally:
        conn.close()


@pytest.mark.parametrize("blob", [
    "",
    "   ",
    "{nebaigtas",
    "{'viengubos': 'kabutes'}",
    "<html>404</html>",
    '{"a": 1}{"b": 2}',
])
def test_a_stored_blob_that_is_not_json_reads_back_empty(app, db, blob):
    _seed(db, "general_contact", blob)

    conn = _conn()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {}
    finally:
        conn.close()


def test_a_stored_blob_written_as_bytes_still_decodes(app, db):
    _seed(db, "general_contact", '{"email": "knf@knf.vu.lt"}'.encode())

    conn = _conn()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {"email": "knf@knf.vu.lt"}
    finally:
        conn.close()


def test_a_stored_blob_of_undecodable_bytes_reads_back_empty(app, db):
    _seed(db, "general_contact", b"\xff\xfe\x00")

    conn = _conn()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {}
    finally:
        conn.close()


def test_a_row_whose_blob_is_null_reads_back_empty():
    # data_json is NOT NULL, so this arm needs a stand-in row —
    # it is the guard against a schema that ever stops being
    assert _stored_section(_FakeRowDb({"data_json": None}), "lt", "general_contact") == {}


def test_a_missing_row_object_reads_back_empty():
    assert _stored_section(_FakeRowDb(None), "lt", "general_contact") == {}


@pytest.mark.parametrize("lang, section", [
    ("LT", "general_contact"),
    ("Lt", "general_contact"),
    ("en", "general_contact"),
    ("lt", "General_Contact"),
    ("lt", "generalcontact"),
    ("lt", "contacts"),
    ("lt", ""),
])
def test_the_lookup_matches_the_language_and_section_exactly(app, db, lang, section):
    _seed(db, "general_contact", '{"email": "knf@knf.vu.lt"}')

    conn = _conn()
    try:
        assert _stored_section(conn, lang, section) == {}
    finally:
        conn.close()


def test_another_language_row_is_never_read_as_the_lithuanian_one(app, db):
    _seed(db, "general_contact", '{"email": "en@knf.vu.lt"}', lang="en")
    _seed(db, "general_contact", '{"email": "lt@knf.vu.lt"}', lang="lt")

    conn = _conn()
    try:
        assert _stored_section(conn, "lt", "general_contact") == {"email": "lt@knf.vu.lt"}
        assert _stored_section(conn, "en", "general_contact") == {"email": "en@knf.vu.lt"}
    finally:
        conn.close()


def test_a_list_section_such_as_contacts_reads_back_empty(app):
    # Only general_contact is merged this way, and this is why:
    # the contacts and programs blobs are lists, not objects
    conn = _conn()
    try:
        _store_info(conn, "lt", {"contacts": [{"category": "Dekanatas", "items": []}]}, STAMP)
        assert _stored_section(conn, "lt", "contacts") == {}
    finally:
        conn.close()








# -----------------------------------------------------------
# _store_info — what counts as a change
# -----------------------------------------------------------


def test_storing_nothing_writes_no_rows_and_counts_nothing(app, db):
    conn = _conn()
    try:
        assert _store_info(conn, "lt", {}, STAMP) == 0
    finally:
        conn.close()

    assert _rows(db) == []


def test_every_section_of_a_first_run_counts_as_changed(app, db):
    conn = _conn()
    try:
        changed = _store_info(conn, "lt", {"contacts": [], "programs": [],
                                           "general_contact": {"email": "knf@knf.vu.lt"}}, STAMP)
    finally:
        conn.close()

    assert changed == 3
    assert {row["section"] for row in _rows(db)} == {"contacts", "programs", "general_contact"}


def test_only_the_section_whose_blob_differs_is_counted(app, db):
    conn = _conn()
    try:
        _store_info(conn, "lt", {"contacts": [{"category": "A", "items": []}],
                                 "programs": [{"name": "P"}]}, STAMP)
        changed = _store_info(conn, "lt", {"contacts": [{"category": "B", "items": []}],
                                           "programs": [{"name": "P"}]}, "2026-08-30T09:30:00")
    finally:
        conn.close()

    assert changed == 1
    assert _sections(db)["contacts"] == [{"category": "B", "items": []}]


def test_an_unchanged_section_keeps_its_row_id_and_its_bytes(app, db):
    conn = _conn()
    try:
        _store_info(conn, "lt", {"programs": [{"name": "P"}]}, STAMP)
        before = db.execute("SELECT id, data_json FROM faculty_info").fetchone()
        _store_info(conn, "lt", {"programs": [{"name": "P"}]}, "2026-08-30T09:30:00")
    finally:
        conn.close()

    after = db.execute("SELECT id, data_json, scraped_at FROM faculty_info").fetchall()
    assert len(after) == 1
    assert after[0]["id"] == before["id"]
    assert after[0]["data_json"] == before["data_json"]
    assert after[0]["scraped_at"] == "2026-08-30T09:30:00"


def test_the_same_fields_written_in_another_order_count_as_a_change(app):
    # QUIRK: the comparison is on the serialised TEXT, so a
    # reordered dict is "new" even though nothing about the
    # faculty changed. Harmless for the blobs this scraper
    # builds, which are assembled in a fixed order every run
    conn = _conn()
    try:
        _store_info(conn, "lt", {"general_contact": {"phone": "1", "email": "a@vu.lt"}}, STAMP)
        changed = _store_info(conn, "lt", {"general_contact": {"email": "a@vu.lt", "phone": "1"}},
                              STAMP)
    finally:
        conn.close()

    assert changed == 1


def test_lithuanian_letters_are_stored_unescaped(app, db):
    conn = _conn()
    try:
        _store_info(conn, "lt", {"contacts": [{"category": "Studijų skyrius", "items": []}]}, STAMP)
    finally:
        conn.close()

    stored = db.execute("SELECT data_json FROM faculty_info").fetchone()["data_json"]
    assert "Studijų skyrius" in stored
    assert "\\u" not in stored


def test_a_deeply_nested_blob_round_trips_unchanged(app, db):
    blob = {"contacts": [{"category": "Dekanatas",
                          "items": [{"name": "A", "email": "a@knf.vu.lt", "room": "102"}]}]}
    conn = _conn()
    try:
        _store_info(conn, "lt", blob, STAMP)
    finally:
        conn.close()

    assert _sections(db)["contacts"] == blob["contacts"]


def test_a_large_blob_is_stored_whole(app, db):
    items = [{"name": f"Darbuotojas {n}", "email": f"d{n}@knf.vu.lt"} for n in range(4000)]
    conn = _conn()
    try:
        assert _store_info(conn, "lt", {"contacts": [{"category": "Visi", "items": items}]},
                           STAMP) == 1
    finally:
        conn.close()

    assert len(_sections(db)["contacts"][0]["items"]) == 4000


def test_the_two_languages_are_independent_rows(app, db):
    conn = _conn()
    try:
        assert _store_info(conn, "lt", {"general_contact": {"email": "lt@knf.vu.lt"}}, STAMP) == 1
        assert _store_info(conn, "en", {"general_contact": {"email": "en@knf.vu.lt"}}, STAMP) == 1
    finally:
        conn.close()

    assert len(_rows(db)) == 2
    assert _sections(db, "en") == {"general_contact": {"email": "en@knf.vu.lt"}}


@pytest.mark.parametrize("section", ["", "x", "a" * 300, "general contact", "GENERAL_CONTACT"])
def test_a_section_name_is_stored_exactly_as_it_was_handed_over(app, db, section):
    conn = _conn()
    try:
        _store_info(conn, "lt", {section: {"a": 1}}, STAMP)
    finally:
        conn.close()

    assert [row["section"] for row in _rows(db)] == [section]


def test_the_stamp_is_written_verbatim_whatever_shape_it_has(app, db):
    conn = _conn()
    try:
        _store_info(conn, "lt", {"programs": []}, "ne data")
    finally:
        conn.close()

    assert _rows(db)[0]["scraped_at"] == "ne data"








# -----------------------------------------------------------
# _store_info — the failure modes
# -----------------------------------------------------------


def test_a_value_json_cannot_express_stops_the_write(app, db):
    conn = _conn()
    try:
        with pytest.raises(TypeError):
            _store_info(conn, "lt", {"contacts": [{"name": "A"}], "programs": {1, 2}}, STAMP)
    finally:
        conn.close()

    # The earlier section was written but never committed, so
    # the caller's rollback has something to roll back and
    # another connection sees nothing
    assert _rows(db) == []


def test_a_stamp_that_is_none_is_refused_by_the_schema(app, db):
    conn = _conn()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _store_info(conn, "lt", {"programs": []}, None)
    finally:
        conn.close()

    assert _rows(db) == []


def test_a_payload_that_is_not_a_mapping_raises_before_any_sql(app, db):
    conn = _conn()
    try:
        with pytest.raises(AttributeError):
            _store_info(conn, "lt", [("programs", [])], STAMP)
    finally:
        conn.close()

    assert _rows(db) == []


def test_a_row_planted_between_the_select_and_the_insert_breaks_the_upsert(app, db):
    # The upsert is hand-rolled rather than leaning on
    # UNIQUE(lang, section): nothing but the module's own run
    # lock keeps a second writer out of the gap
    conn = _conn()
    rival = _conn()
    try:
        racing = _RacingDb(conn, rival, "lt", "programs")
        with pytest.raises(sqlite3.IntegrityError):
            _store_info(racing, "lt", {"programs": [{"name": "P"}]}, STAMP)
    finally:
        conn.close()
        rival.close()

    assert len(_rows(db)) == 1








# -----------------------------------------------------------
# scrape_faculty_info — the pages it got and the ones it did not
# -----------------------------------------------------------


def test_a_page_that_downloads_empty_still_counts_as_scraped(app, db, http):
    _serve(http, main="")

    result = scrape_faculty_info()

    assert result["pages_scraped"] == 1
    assert _sections(db) == {}
    assert _runs(db)[-1]["status"] == "completed"


def test_a_run_out_of_time_keeps_the_pages_it_already_had(app, db, http, monkeypatch):
    # The budget is checked BETWEEN fetches: main and structure
    # land, the remaining three never leave the container
    calls = {"n": 0}

    def _spent(deadline):
        calls["n"] += 1
        return calls["n"] > 2

    monkeypatch.setattr(info_scraper, "deadline_passed", _spent)
    _serve_all(http)

    result = scrape_faculty_info()

    assert result["pages_scraped"] == 2
    assert result["programs_found"] == 0
    assert set(_sections(db)) == {"contacts", "general_contact"}
    assert _runs(db)[-1]["status"] == "completed"


def test_a_page_redirected_off_the_allowlist_is_a_dead_page(app, db, http):
    _serve(http, structure=STRUCTURE, contacts=CONTACTS, bachelor=BACHELOR, master=MASTER)
    for url in (PAGE_URLS["main"], PAGE_URLS["main"] + "/"):
        http.add(responses.GET, url, status=302,
                 headers={"Location": "https://vu.lt/pradzia"}, content_type="text/html")
    _html(http, "https://vu.lt/pradzia", "<html><footer>knf@knf.vu.lt</footer></html>")

    result = scrape_faculty_info()

    assert result["pages_scraped"] == 4
    assert "general_contact" not in _sections(db)


def test_every_page_dead_writes_no_rows_at_all(app, db, http):
    result = scrape_faculty_info()

    assert result["pages_scraped"] == 0
    assert _rows(db) == []
    row = _runs(db)[-1]
    assert row["status"] == "completed"
    assert row["error_message"] is None


def test_only_the_front_page_alive_writes_the_general_section_alone(app, db, http):
    _serve(http, main=MAIN)

    result = scrape_faculty_info()

    assert set(_sections(db)) == {"general_contact"}
    assert result == {"pages_scraped": 1, "contacts_found": 0, "programs_found": 0,
                      "runId": result["runId"]}
    assert _runs(db)[-1]["articles_new"] == 1








# -----------------------------------------------------------
# scrape_faculty_info — the general_contact merge
# -----------------------------------------------------------


def test_the_general_block_layers_the_scrape_over_the_stored_blob_over_the_defaults(app, db, http):
    _seed(db, "general_contact", json.dumps({
        "phone": "+370 37 422 999",
        "hours": "I-V 8:00-17:00",
    }))
    _serve(http, main="<html><body><p>Muitinės g. 8, LT-44280 Kaunas</p></body></html>")

    scrape_faculty_info()

    assert _sections(db)["general_contact"] == {
        # scraped this run
        "address": "Muitinės g. 8, LT-44280 Kaunas",
        # kept from the stored blob
        "phone": "+370 37 422 999",
        "hours": "I-V 8:00-17:00",
        # nobody ever found one, so the hardcoded floor stands
        "email": GENERAL_CONTACT_DEFAULTS["email"],
    }


def test_a_scraped_field_replaces_the_stored_one(app, db, http):
    _seed(db, "general_contact", json.dumps({"email": "senas@knf.vu.lt"}))
    _serve(http, main="<html><body><footer>knf@knf.vu.lt</footer></body></html>")

    scrape_faculty_info()

    assert _sections(db)["general_contact"]["email"] == "knf@knf.vu.lt"


def test_a_stored_general_block_that_is_not_an_object_is_ignored_by_the_merge(app, db, http):
    _seed(db, "general_contact", '["ne objektas"]')
    _serve(http, main="<html><body><footer>knf@knf.vu.lt</footer></body></html>")

    scrape_faculty_info()

    assert _sections(db)["general_contact"] == {
        "address": GENERAL_CONTACT_DEFAULTS["address"],
        "phone": GENERAL_CONTACT_DEFAULTS["phone"],
        "email": "knf@knf.vu.lt",
    }


def test_a_run_that_found_no_general_contact_leaves_even_the_stamp_alone(app, db, http):
    _seed(db, "general_contact", json.dumps({"email": "knf@knf.vu.lt"}), scraped_at="2020-01-01T00:00:00")
    _serve(http, main="<html><body><p>Sveiki</p></body></html>", contacts=CONTACTS)

    scrape_faculty_info()

    row = db.execute("SELECT data_json, scraped_at FROM faculty_info WHERE section = 'general_contact'").fetchone()
    assert json.loads(row["data_json"]) == {"email": "knf@knf.vu.lt"}
    assert row["scraped_at"] == "2020-01-01T00:00:00"


def test_a_folded_staff_member_carries_only_the_fields_the_page_stated(app, db, http):
    # The fold copies name, email, phone and position one by one,
    # so a line stating a phone and nothing else must arrive as a
    # phone and nothing else — never an empty email or position
    structure = ("<html><body><div class='item-page'>"
                 "<h2>Verslo katedra</h2>"
                 "<p>Doc. dr. Ona Onaitė +370 37 422 604</p>"
                 "</div></body></html>")
    _serve(http, structure=structure)

    scrape_faculty_info()

    katedra = _sections(db)["contacts"][0]
    assert katedra["category"] == "Verslo katedra"
    assert katedra["items"] == [{"name": "Doc. dr. Ona Onaitė +370 37 422 604",
                                 "phone": "+370 37 422 604"}]


def test_a_department_whose_people_all_dropped_out_adds_no_category(app, db, http, monkeypatch):
    monkeypatch.setattr(info_scraper, "_scrape_staff", lambda soup: [
        {"department": "Tuščia katedra", "staff": []},
        {"department": "Informatikos katedra", "staff": [{"name": "Jonas Jonaitis"}]},
    ])
    _serve(http, structure=STRUCTURE)

    result = scrape_faculty_info()

    assert [c["category"] for c in _sections(db)["contacts"]] == ["Informatikos katedra"]
    assert result["contacts_found"] == 1


def test_an_english_row_is_never_touched_by_a_run(app, db, http):
    _seed(db, "general_contact", json.dumps({"email": "en@knf.vu.lt"}), lang="en",
          scraped_at="2020-01-01T00:00:00")
    _serve_all(http)

    scrape_faculty_info()

    row = db.execute("SELECT data_json, scraped_at FROM faculty_info WHERE lang = 'en'").fetchone()
    assert json.loads(row["data_json"]) == {"email": "en@knf.vu.lt"}
    assert row["scraped_at"] == "2020-01-01T00:00:00"








# -----------------------------------------------------------
# scrape_faculty_info — bookkeeping, the lock and the failures
# -----------------------------------------------------------


def test_the_result_carries_exactly_the_four_documented_keys(app, http):
    _serve_all(http)

    result = scrape_faculty_info()

    assert set(result) == {"pages_scraped", "contacts_found", "programs_found", "runId"}
    assert uuid.UUID(result["runId"])


def test_articles_found_counts_items_and_programmes_not_sections(app, db, http):
    _serve_all(http)

    result = scrape_faculty_info()

    # one contacts item + one folded staff member + two programmes
    assert result["contacts_found"] == 2
    assert result["programs_found"] == 2
    row = _runs(db)[-1]
    assert row["articles_found"] == 4
    assert row["articles_new"] == 3


def test_a_skipped_trigger_writes_neither_a_run_row_nor_a_section(app, db, http):
    _serve_all(http)
    assert info_scraper._RUN_LOCK.acquire(blocking=False)
    try:
        result = scrape_faculty_info()
    finally:
        info_scraper._RUN_LOCK.release()

    assert result == {"pages_scraped": 0, "contacts_found": 0, "programs_found": 0, "skipped": True}
    assert "runId" not in result
    assert _runs(db) == []
    assert _rows(db) == []
    assert len(http.calls) == 0


def test_the_lock_is_released_after_a_run_that_failed(app, db, http, monkeypatch):
    def _boom(db_conn, lang, data, scraped_at):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(info_scraper, "_store_info", _boom)
    _serve_all(http)

    assert "error" in scrape_faculty_info()

    assert info_scraper._RUN_LOCK.acquire(blocking=False)
    info_scraper._RUN_LOCK.release()


def test_a_section_json_cannot_express_fails_the_run_and_stores_nothing(app, db, http):
    # The extractors are guarded, but what they RETURN is not:
    # a value json.dumps refuses lands in the run's own except
    def _unserialisable(bachelor_soup, master_soup):
        return [{"name": "Informatika", "degree": {"Bakalauras"}}]

    _serve_all(http)
    original = info_scraper._scrape_programs
    info_scraper._scrape_programs = _unserialisable
    try:
        result = scrape_faculty_info()
    finally:
        info_scraper._scrape_programs = original

    assert "not JSON serializable" in result["error"]
    assert result["pages_scraped"] == 0
    assert _rows(db) == []
    row = _runs(db)[-1]
    assert row["id"] == result["runId"]
    assert row["status"] == "failed"


def test_a_rollback_on_the_connection_that_broke_does_not_hide_the_failure(app, db, http, monkeypatch):
    # The rollback runs on exactly the connection that may have
    # died, so its own failure is swallowed and the run is still
    # closed as failed — on a fresh connection
    def _close_then_fail(db_conn, lang, data, scraped_at):
        db_conn.close()
        raise RuntimeError("ryšys nutrūko")

    monkeypatch.setattr(info_scraper, "_store_info", _close_then_fail)
    _serve_all(http)

    result = scrape_faculty_info()

    assert result["error"] == "ryšys nutrūko"
    assert _runs(db)[-1]["status"] == "failed"
    assert info_scraper._RUN_LOCK.acquire(blocking=False)
    info_scraper._RUN_LOCK.release()


def test_a_failed_run_is_followed_by_a_healthy_one(app, db, http, monkeypatch):
    def _boom(db_conn, lang, data, scraped_at):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(info_scraper, "_store_info", _boom)
    _serve_all(http)
    scrape_faculty_info()

    monkeypatch.undo()
    scrape_faculty_info()

    assert [row["status"] for row in _runs(db)] == ["failed", "completed"]
    assert set(_sections(db)) == {"contacts", "programs", "general_contact"}
