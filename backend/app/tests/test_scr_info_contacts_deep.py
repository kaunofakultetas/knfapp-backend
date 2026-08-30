# -----------------------------------------------------------
#  [*] Tests — info_scraper's contacts slice, exhaustively
#
#  The gap-closing pass over seven pure functions of
#  scraper/info_scraper.py:
#
#    _scrape_contacts      the contacts page -> categories
#    _scrape_staff         the structure page -> departments
#    _extract_email        first address in a text
#    _institutional_email  the *.vu.lt privacy gate
#    _faculty_phone        the Kaunas-landline privacy gate
#    _extract_phone        first Lithuanian number in a text
#    _name_shaped          "does this read like a person"
#
#  Line and branch coverage of the module was already 100%
#  before this file, so what it adds is the arm-by-arm and
#  boundary-by-boundary behaviour those numbers cannot see:
#
#    - the PRIVACY GATE both scrapers hang on, since every
#      item here is republished on an unauthenticated
#      /api/info: which domains count as the university's,
#      which numbers count as the switchboard's, and what a
#      lookalike ("evil-vu.lt", "vu.lt.evil.com", a mobile
#      spelled as a landline) is answered
#    - every guard clause of the two walks: no soup, an empty
#      document, no known container, a container with no
#      child tags, wrapper elements, one-cell rows, rows with
#      no name, lines too short and lines too long
#    - the boundaries either side of every threshold — name
#      >= 2 chars, entry >= 5 chars, staff line >= 3 and
#      < 200 chars, the 100-char name cap, the 60-char name
#      fallback, a room of 2, 3 and 4 digits, the five words
#      _name_shaped looks at
#    - the ordering rules: which content selector wins, which
#      phone spelling wins, which room spelling wins, knf@
#      over info@, and the (name, email) dedupe key that
#      contacts have and staff do not
#    - two defects this pass found, now FIXED and pinned as
#      ordinary tests: a heading with inline markup lost the
#      space between its words, and a lecturer's MOBILE
#      number — dropped from the phone field by the privacy
#      gate on purpose — was republished verbatim as their
#      position
#
#  Everything here is a pure function over parsed HTML: no
#  network, no database, no clock. The soups are built with
#  the same lxml parser _fetch_page uses, so what the tests
#  see is what a real fetch would hand the scraper.
# -----------------------------------------------------------


import pytest
from bs4 import BeautifulSoup

from app.scraper.info_scraper import (
    _extract_email,
    _extract_phone,
    _faculty_phone,
    _institutional_email,
    _name_shaped,
    _scrape_contacts,
    _scrape_staff,
)




# -----------------------------------------------------------
# _soup — the parser the scraper itself uses
# -----------------------------------------------------------
#
# lxml, exactly as _fetch_page builds it, so the tree the
# tests walk has the same tag fixups (implied <html>, closed
# <p>, <tbody> around rows) the live pages get.
# -----------------------------------------------------------

def _soup(html):
    return BeautifulSoup(html, "lxml")


# -----------------------------------------------------------
# _contacts — the contacts of one page fragment
# -----------------------------------------------------------
#
# Wraps the fragment in a known content container so a test
# only has to write the markup it is actually about.
# -----------------------------------------------------------

def _contacts(fragment, container='<div id="content">', closing="</div>"):
    return _scrape_contacts(_soup(f"{container}{fragment}{closing}"))


# -----------------------------------------------------------
# _items — every item of every category, flattened
# -----------------------------------------------------------

def _items(contacts):
    return [item for category in contacts for item in category["items"]]


# -----------------------------------------------------------
# _one_item — the single item a fragment was expected to yield
# -----------------------------------------------------------

def _one_item(fragment):
    items = _items(_contacts(fragment))
    assert len(items) == 1, f"expected exactly one item, got {items}"

    return items[0]


# -----------------------------------------------------------
# _staff — the departments of one structure fragment
# -----------------------------------------------------------

def _staff(fragment, container='<div id="content">', closing="</div>"):
    return _scrape_staff(_soup(f"{container}{fragment}{closing}"))


# -----------------------------------------------------------
# _people — every person of every department, flattened
# -----------------------------------------------------------

def _people(departments):
    return [person for department in departments for person in department["staff"]]


# -----------------------------------------------------------
# _one_person — the single staff entry a fragment yields
# -----------------------------------------------------------
#
# Every staff fragment needs a heading above it: a person
# found before the first h2/h3/h4 is never flushed, which is
# itself pinned further down.
# -----------------------------------------------------------

def _one_person(line):
    people = _people(_staff(f"<h2>Katedra</h2>{line}"))
    assert len(people) == 1, f"expected exactly one person, got {people}"

    return people[0]






# -----------------------------------------------------------
# _extract_email — the first address in a text
# -----------------------------------------------------------


def test_an_address_is_found_in_the_middle_of_a_sentence():
    assert _extract_email("Rašykite adresu knf@knf.vu.lt darbo valandomis") == "knf@knf.vu.lt"


def test_a_domain_without_a_dot_is_not_an_address():
    assert _extract_email("parašykite jonui@localhost") is None


def test_the_shortest_thing_shaped_like_an_address_is_one():
    assert _extract_email("a@b.c") == "a@b.c"


def test_every_trailing_dot_is_trimmed_off_the_address():
    assert _extract_email("Rašykite knf@knf.vu.lt...") == "knf@knf.vu.lt"


def test_a_mailto_prefix_is_not_part_of_the_address():
    assert _extract_email("mailto:knf@knf.vu.lt") == "knf@knf.vu.lt"


def test_angle_brackets_around_an_address_are_not_part_of_it():
    assert _extract_email("Jonas <jonas@knf.vu.lt>") == "jonas@knf.vu.lt"


def test_a_punctuation_character_cuts_the_local_part_short():
    # "!" is outside the local-part class, so the match starts
    # after it — the address harvested is not the one written
    assert _extract_email("kreiptis!jonas@knf.vu.lt") == "jonas@knf.vu.lt"


def test_an_address_glued_to_the_word_before_it_keeps_that_word():
    assert _extract_email("rašykiteknf@knf.vu.lt") == "rašykiteknf@knf.vu.lt"


def test_an_address_with_no_local_part_is_not_an_address():
    assert _extract_email("Rašykite @vu.lt") is None


def test_the_case_the_page_wrote_is_preserved():
    assert _extract_email("KNF@KNF.VU.LT") == "KNF@KNF.VU.LT"


def test_dots_a_plus_tag_and_an_underscore_are_all_local_part_characters():
    assert _extract_email("vardas.pavarde+naujienos@knf.vu.lt") == "vardas.pavarde+naujienos@knf.vu.lt"
    assert _extract_email("vardas_pavarde@knf.vu.lt") == "vardas_pavarde@knf.vu.lt"


def test_a_lithuanian_letter_is_a_word_character_to_the_address_regex():
    assert _extract_email("žinios@vu.lt") == "žinios@vu.lt"


def test_an_empty_text_has_no_address():
    assert _extract_email("") is None


def test_a_text_without_an_at_sign_has_no_address():
    assert _extract_email("Kontaktai: Muitinės g. 8, Kaunas") is None


def test_a_missing_text_is_not_an_empty_text():
    # None-safety lives in _institutional_email, not here — the
    # callers always hand this one a real string
    with pytest.raises(TypeError):
        _extract_email(None)






# -----------------------------------------------------------
# _institutional_email — the *.vu.lt gate
# -----------------------------------------------------------


@pytest.mark.parametrize("email", [
    "knf@vu.lt",
    "knf@knf.vu.lt",
    "jonas@mif.vu.lt",
    "jonas@a.b.knf.vu.lt",
])
def test_the_university_and_all_its_subdomains_pass_the_gate(email):
    assert _institutional_email(email) == email


def test_the_domain_is_compared_in_lower_case_and_the_address_comes_back_as_written():
    assert _institutional_email("KNF@KNF.VU.LT") == "KNF@KNF.VU.LT"
    assert _institutional_email("knf@Knf.Vu.Lt") == "knf@Knf.Vu.Lt"


@pytest.mark.parametrize("email", [
    "jonas@gmail.com",
    "jonas@ktu.lt",
    "jonas@evil-vu.lt",
    "jonas@notvu.lt",
    "jonas@vu.lt.evil.com",
    "jonas@vu.lt.",
    "jonas@vu.lt ",
    "jonas@",
])
def test_a_domain_that_only_looks_like_the_university_is_dropped(email):
    assert _institutional_email(email) is None


def test_nothing_at_all_is_dropped():
    assert _institutional_email(None) is None
    assert _institutional_email("") is None


def test_a_token_without_an_at_sign_is_dropped():
    assert _institutional_email("knf.vu.lt") is None


def test_only_the_last_at_sign_separates_the_domain():
    assert _institutional_email("jonas@knf@vu.lt") == "jonas@knf@vu.lt"


def test_a_bare_domain_behind_an_at_sign_passes_the_gate():
    # A curiosity rather than a hole: _extract_email requires a
    # local part, so no caller can reach the gate with this
    assert _institutional_email("@vu.lt") == "@vu.lt"






# -----------------------------------------------------------
# _faculty_phone — the Kaunas-landline gate
# -----------------------------------------------------------


def test_the_number_comes_back_byte_for_byte_as_the_page_wrote_it():
    assert _faculty_phone("+370-37-422-523") == "+370-37-422-523"
    assert _faculty_phone("(8-37)\n 42 25 23") == "(8-37)\n 42 25 23"


@pytest.mark.parametrize("phone", [
    "+370 612 34567",
    "8 612 34567",
    "(8-687) 12 34 56",
    "+370 5 219 5000",
])
def test_a_mobile_or_another_citys_landline_never_passes_the_gate(phone):
    assert _faculty_phone(phone) is None


def test_the_country_code_is_stripped_before_the_area_code_is_read():
    assert _faculty_phone("+370 37 422 523") == "+370 37 422 523"
    assert _faculty_phone("+370 8 12 345") is None


def test_the_national_leading_eight_is_stripped_before_the_area_code_is_read():
    assert _faculty_phone("(8-37) 42 25 23") == "(8-37) 42 25 23"
    assert _faculty_phone("8-370-37-422-523") == "8-370-37-422-523"


def test_a_number_written_with_no_prefix_at_all_is_kept_when_it_starts_with_the_area_code():
    assert _faculty_phone("37 422 523") == "37 422 523"


def test_the_area_code_alone_is_the_smallest_number_that_passes():
    assert _faculty_phone("37") == "37"
    assert _faculty_phone("8-37") == "8-37"


def test_a_country_code_with_nothing_behind_it_is_dropped():
    assert _faculty_phone("+370") is None
    assert _faculty_phone("8") is None


def test_a_string_with_no_digits_in_it_is_dropped():
    assert _faculty_phone("telefonas nenurodytas") is None
    assert _faculty_phone("   ") is None


def test_no_number_at_all_is_dropped():
    assert _faculty_phone(None) is None
    assert _faculty_phone("") is None


def test_a_number_that_is_not_text_is_not_a_number():
    with pytest.raises(TypeError):
        _faculty_phone(37037422523)






# -----------------------------------------------------------
# _extract_phone — the first Lithuanian number in a text
# -----------------------------------------------------------


def test_the_international_form_is_read_without_any_separators():
    assert _extract_phone("tel. +37037422523") == "+37037422523"


def test_dashes_written_between_the_groups_are_kept():
    assert _extract_phone("tel. +370-37-422-523") == "+370-37-422-523"


def test_a_dash_surrounded_by_spaces_keeps_the_dash_and_collapses_the_spaces():
    assert _extract_phone("tel. +370  -  37 422 523") == "+370 - 37 422 523"


def test_a_four_digit_last_group_is_taken_whole():
    assert _extract_phone("+370 37 422 5234") == "+370 37 422 5234"


def test_a_fifth_digit_is_left_behind_rather_than_failing_the_match():
    assert _extract_phone("+370 37 422 52345") == "+370 37 422 5234"


def test_too_few_digits_after_the_country_code_is_not_a_number():
    assert _extract_phone("skambinkite +370 37 42 52") is None


def test_the_national_form_is_read_with_and_without_its_inner_separator():
    assert _extract_phone("(8-37) 42 25 23") == "(8-37) 42 25 23"
    assert _extract_phone("(8 37) 42 25 23") == "(8 37) 42 25 23"
    assert _extract_phone("(837) 422523") == "(837) 422523"


def test_a_doubled_separator_breaks_the_national_form():
    # [\s\-]? is a single character, so a page that padded the
    # groups loses its number entirely
    assert _extract_phone("(8-37) 42  25  23") is None


def test_the_national_form_keeps_the_newline_it_was_written_with():
    # Only the international branch collapses whitespace runs;
    # the national one is returned stripped but not normalised
    assert _extract_phone("(8-37)\n 42 25 23") == "(8-37)\n 42 25 23"


def test_a_three_digit_area_code_in_brackets_is_still_a_number_here():
    # Reading it is one job, republishing it another: the
    # privacy gate is what drops a mobile
    assert _extract_phone("(8-687) 12 34 56") == "(8-687) 12 34 56"
    assert _faculty_phone(_extract_phone("(8-687) 12 34 56")) is None


def test_the_national_form_without_its_brackets_is_not_a_number():
    assert _extract_phone("8-37 42 25 23") is None
    assert _extract_phone("8 37 422 523") is None


def test_the_international_form_without_its_plus_is_not_a_number():
    assert _extract_phone("370 37 422 523") is None


def test_the_international_form_wins_even_when_the_national_one_is_written_first():
    text = "(8-37) 42 25 23 arba +370 37 422 523"

    assert _extract_phone(text) == "+370 37 422 523"


def test_surrounding_text_is_trimmed_off_the_number():
    assert _extract_phone("   Telefonas: +370 37 422 523.   ") == "+370 37 422 523"


def test_a_number_at_the_end_of_a_very_long_page_is_still_found():
    assert _extract_phone("žinios " * 2000 + "+370 37 422 523") == "+370 37 422 523"


def test_a_text_with_no_number_in_it_has_no_number():
    assert _extract_phone("") is None
    assert _extract_phone("Muitinės g. 8, Kaunas") is None


def test_a_missing_text_has_no_number_to_look_in():
    with pytest.raises(TypeError):
        _extract_phone(None)






# -----------------------------------------------------------
# _name_shaped — does this line read like a person
# -----------------------------------------------------------


def test_two_capitalised_words_is_the_least_that_reads_like_a_name():
    assert _name_shaped("Jonas Jonaitis") is True
    assert _name_shaped("Jonas jonaitis") is False


def test_only_the_first_five_words_are_looked_at():
    assert _name_shaped("apie mus ir Jonas Jonaitis") is True
    assert _name_shaped("apie mus ir mūsų Jonas Jonaitis") is False


def test_a_leading_separator_does_not_use_up_one_of_the_five_words():
    assert _name_shaped("  Jonas Jonaitis") is True
    assert _name_shaped(",,,Jonas,Jonaitis") is True


@pytest.mark.parametrize("text", [
    "Jonas Jonaitis",
    "Jonas\tJonaitis",
    "Jonas\nJonaitis",
    "Jonaitis,Jonas",
    "Jonas\xa0Jonaitis",
])
def test_words_are_split_on_any_whitespace_or_comma(text):
    assert _name_shaped(text) is True


def test_a_bracket_hides_the_capital_behind_it():
    assert _name_shaped("(Jonas) Jonaitis") is False


def test_digits_are_not_capital_letters():
    assert _name_shaped("123 456") is False


def test_lithuanian_capitals_count_as_capitals():
    assert _name_shaped("Ąžuolas Šimkus") is True


def test_an_academic_title_counts_towards_the_two_capitals():
    assert _name_shaped("Prof. dr. Jonas") is True


def test_an_all_capitals_line_reads_as_a_name():
    # Which is why the title test does the real filtering — this
    # one only asks about shape
    assert _name_shaped("STUDIJŲ DALYKŲ APRAŠAI") is True


def test_nothing_at_all_does_not_read_like_a_name():
    assert _name_shaped("") is False
    assert _name_shaped("   ") is False
    assert _name_shaped(",") is False






# -----------------------------------------------------------
# _scrape_contacts — picking the content container
# -----------------------------------------------------------


def test_the_contacts_selector_list_is_tried_in_order_not_in_document_order():
    html = ('<div class="item-page"><p>Antra, antra@knf.vu.lt</p></div>'
            '<div class="article-content"><p>Pirma, pirma@knf.vu.lt</p></div>')
    items = _items(_scrape_contacts(_soup(html)))

    assert [item["name"] for item in items] == ["Pirma"]


def test_a_later_selector_is_used_only_when_the_earlier_ones_miss():
    html = ('<article><p>Antra, antra@knf.vu.lt</p></article>'
            '<div id="content"><p>Pirma, pirma@knf.vu.lt</p></div>')
    items = _items(_scrape_contacts(_soup(html)))

    assert [item["name"] for item in items] == ["Pirma"]


def test_a_page_that_never_downloaded_yields_no_contacts():
    assert _scrape_contacts(None) == []


def test_an_empty_document_yields_no_contacts():
    # Not through the None guard — an empty soup is a real
    # object, it simply matches no content selector
    assert _scrape_contacts(_soup("")) == []


def test_a_container_holding_nothing_but_text_yields_no_contacts():
    assert _scrape_contacts(_soup("<article>Priimamasis knf@knf.vu.lt</article>")) == []


def test_a_page_of_headings_and_nothing_else_yields_no_contacts():
    assert _contacts("<h2>Dekanatas</h2><h3>Paslaugos</h3>") == []






# -----------------------------------------------------------
# _scrape_contacts — headings and categories
# -----------------------------------------------------------


def test_a_padded_heading_names_its_category_stripped():
    contacts = _contacts("<h2>  Dekanatas  </h2><p>Jonas, jonas@knf.vu.lt</p>")

    assert contacts[0]["category"] == "Dekanatas"


def test_a_heading_with_inline_markup_keeps_the_space_between_its_words():
    contacts = _contacts("<h2>Dekanatas ir <strong>administracija</strong></h2>"
                         "<p>Jonas, jonas@knf.vu.lt</p>")

    assert contacts[0]["category"] == "Dekanatas ir administracija"


def test_a_whitespace_only_heading_swallows_everything_written_under_it():
    # The category name goes falsy, and a falsy category is
    # never flushed — the same fate an empty heading deals
    assert _contacts("<h2>   </h2><p>Jonas, jonas@knf.vu.lt</p>") == []


def test_two_headings_in_a_row_do_not_flush_an_empty_category():
    contacts = _contacts("<h2>Tuščia</h2><h2>Dekanatas</h2><p>Jonas, jonas@knf.vu.lt</p>")

    assert [category["category"] for category in contacts] == ["Dekanatas"]


def test_a_heading_with_nobody_under_it_at_the_end_adds_no_category():
    contacts = _contacts("<p>Jonas, jonas@knf.vu.lt</p><h2>Tuščia</h2>")

    assert [category["category"] for category in contacts] == ["Kontaktai"]


def test_an_h4_opens_a_category_the_way_an_h2_does():
    contacts = _contacts("<h4>Skyrius</h4><p>Jonas, jonas@knf.vu.lt</p>")

    assert contacts[0]["category"] == "Skyrius"


def test_an_h1_is_invisible_to_the_walk():
    contacts = _contacts("<h1>Kontaktinė informacija</h1><p>Jonas, jonas@knf.vu.lt</p>")

    assert [category["category"] for category in contacts] == ["Kontaktai"]


def test_the_implicit_category_is_flushed_before_the_first_real_one():
    contacts = _contacts("<p>Priimamasis, knf@knf.vu.lt</p>"
                         "<h2>Dekanatas</h2><p>Jonas, jonas@knf.vu.lt</p>")

    assert [category["category"] for category in contacts] == ["Kontaktai", "Dekanatas"]






# -----------------------------------------------------------
# _scrape_contacts — wrappers and leaves
# -----------------------------------------------------------


def test_a_div_wrapping_a_table_is_a_wrapper_and_its_row_is_read_instead():
    item = _one_item("<div><table><tr><td>Biblioteka</td>"
                     "<td>biblioteka@knf.vu.lt</td></tr></table></div>")

    assert item == {"name": "Biblioteka", "email": "biblioteka@knf.vu.lt"}


def test_a_row_whose_cell_wraps_its_text_in_a_paragraph_is_read_as_free_text():
    # The <p> inside makes the <tr> a wrapper, so the row branch
    # never runs — only that one paragraph is read, and the
    # details standing in the OTHER cells are lost
    item = _one_item("<table><tr><td><p>Biblioteka, biblioteka@knf.vu.lt</p></td>"
                     "<td>(8-37) 42 25 35</td></tr></table>")

    assert item == {"name": "Biblioteka", "email": "biblioteka@knf.vu.lt"}


def test_a_paragraph_holding_only_inline_markup_is_a_leaf():
    item = _one_item("<p><b>Jonas</b> <b>Jonaitis</b>, jonas@knf.vu.lt</p>")

    assert item["name"] == "Jonas Jonaitis"


@pytest.mark.parametrize("tag", ["p", "div", "li"])
def test_every_free_text_tag_carries_an_entry(tag):
    item = _one_item(f"<{tag}>Jonas, jonas@knf.vu.lt</{tag}>")

    assert item == {"name": "Jonas", "email": "jonas@knf.vu.lt"}






# -----------------------------------------------------------
# _scrape_contacts — table rows
# -----------------------------------------------------------


def test_header_cells_count_as_cells():
    item = _one_item("<table><tr><th>Padalinys</th><th>knf@knf.vu.lt</th></tr></table>")

    assert item == {"name": "Padalinys", "email": "knf@knf.vu.lt"}


def test_every_cell_after_the_first_is_searched_for_details():
    item = _one_item("<table><tr><td>Biblioteka</td><td>tel. (8-37) 42 25 35</td>"
                     "<td>biblioteka@knf.vu.lt</td></tr></table>")

    assert item == {"name": "Biblioteka", "phone": "(8-37) 42 25 35",
                    "email": "biblioteka@knf.vu.lt"}


def test_the_first_address_among_the_searched_cells_wins():
    item = _one_item("<table><tr><td>Biblioteka</td><td>pirma@knf.vu.lt</td>"
                     "<td>antra@knf.vu.lt</td></tr></table>")

    assert item["email"] == "pirma@knf.vu.lt"


def test_an_address_in_the_name_cell_is_only_a_name():
    assert _contacts("<table><tr><td>jonas@knf.vu.lt</td><td>Jonas</td></tr></table>") == []


def test_a_row_never_carries_a_room_even_when_a_cell_names_one():
    item = _one_item("<table><tr><td>Biblioteka</td>"
                     "<td>biblioteka@knf.vu.lt, 301 kab.</td></tr></table>")

    assert "room" not in item


def test_a_row_of_two_empty_cells_is_skipped():
    assert _contacts("<table><tr><td></td><td></td></tr></table>") == []


def test_two_cells_is_the_least_a_row_can_be_read_from():
    assert _contacts("<table><tr><td>Biblioteka knf@knf.vu.lt</td></tr></table>") == []
    assert _one_item("<table><tr><td>Biblioteka</td>"
                     "<td>knf@knf.vu.lt</td></tr></table>")["name"] == "Biblioteka"


def test_a_row_carrying_only_a_number_is_an_entry_without_an_address():
    item = _one_item("<table><tr><td>Budėtojas</td><td>(8-37) 42 25 00</td></tr></table>")

    assert item == {"name": "Budėtojas", "phone": "(8-37) 42 25 00"}


def test_a_row_name_is_capped_at_a_hundred_characters():
    item = _one_item(f"<table><tr><td>{'A' * 130}</td>"
                     "<td>knf@knf.vu.lt</td></tr></table>")

    assert item["name"] == "A" * 100


def test_a_row_puts_its_keys_in_the_order_the_app_reads_them():
    item = _one_item("<table><tr><td>Biblioteka</td>"
                     "<td>biblioteka@knf.vu.lt +370 37 422 535</td></tr></table>")

    assert list(item) == ["name", "phone", "email"]






# -----------------------------------------------------------
# _scrape_contacts — the free-text entry
# -----------------------------------------------------------


def test_a_line_of_under_five_characters_is_never_an_entry():
    assert _contacts("<p>Ne</p><p></p><p>abcd</p>") == []


def test_an_entry_with_a_number_and_no_address_keeps_the_whole_line_as_its_name():
    # With no address to split on, the name is the text as
    # written — the number included
    item = _one_item("<p>Budėtojas, +370 37 422 500, 100 kab.</p>")

    assert item == {"name": "Budėtojas, +370 37 422 500, 100 kab.",
                    "phone": "+370 37 422 500", "room": "100"}


def test_an_entry_that_is_nothing_but_an_address_takes_it_as_its_name_too():
    item = _one_item("<p>studijos@knf.vu.lt</p>")

    assert item == {"name": "studijos@knf.vu.lt", "email": "studijos@knf.vu.lt"}


def test_a_private_address_still_cuts_the_name_off_before_it():
    # The name split runs on the RAW address, so a dropped
    # gmail still does its job of ending the name
    item = _one_item("<p>Jonas Jonaitis, jonas@gmail.com, +370 37 422 523</p>")

    assert item == {"name": "Jonas Jonaitis", "phone": "+370 37 422 523"}


def test_a_name_of_exactly_two_characters_is_kept():
    item = _one_item("<p>Ab, jonas@knf.vu.lt</p>")

    assert item["name"] == "Ab"


def test_a_name_of_one_character_falls_back_to_the_whole_text():
    item = _one_item("<p>A, jonas@knf.vu.lt</p>")

    assert item["name"] == "A, jonas@knf.vu.lt"


def test_the_fallback_name_is_the_first_sixty_characters_of_the_text():
    item = _one_item(f"<p>- jonas@knf.vu.lt {'z' * 80}</p>")

    assert item["name"] == f"- jonas@knf.vu.lt {'z' * 80}"[:60]
    assert len(item["name"]) == 60


def test_a_trailing_colon_behind_a_comma_is_trimmed_off_the_name():
    assert _one_item("<p>Dekanatas:, knf@knf.vu.lt</p>")["name"] == "Dekanatas"


def test_a_trailing_comma_behind_a_colon_survives():
    # rstrip(",") runs first, so a "," AFTER the ":" is the only
    # one it can reach
    assert _one_item("<p>Dekanatas,: knf@knf.vu.lt</p>")["name"] == "Dekanatas,"


def test_repeated_trailing_commas_are_all_trimmed():
    assert _one_item("<p>Dekanatas,,, knf@knf.vu.lt</p>")["name"] == "Dekanatas"


def test_a_name_is_capped_at_a_hundred_characters_after_it_is_stripped():
    item = _one_item(f"<p>   {'A' * 130}   , knf@knf.vu.lt</p>")

    assert item["name"] == "A" * 100


def test_an_entry_keeps_a_non_breaking_space_inside_its_name():
    item = _one_item("<p>Jonas&nbsp;Jonaitis, jonas@knf.vu.lt</p>")

    assert item["name"] == "Jonas\xa0Jonaitis"


def test_a_phone_written_before_the_name_ends_up_inside_it():
    # The name is whatever precedes the ADDRESS, phone included
    item = _one_item("<p>+370 37 422 523, Jonas, jonas@knf.vu.lt</p>")

    assert item["name"] == "+370 37 422 523, Jonas"


def test_an_entry_starting_with_its_address_falls_back_to_the_phone_split():
    item = _one_item("<p>knf@knf.vu.lt +370 37 422 523</p>")

    assert item["name"] == "knf@knf.vu.lt"


def test_a_phone_the_reader_normalised_away_from_the_text_does_not_break_the_name():
    # _extract_phone collapses the whitespace runs, so the value
    # it returns is not in the text any more and the split finds
    # nothing to cut — the whole line becomes the name
    item = _one_item("<p>knf@knf.vu.lt +370\n37 422 523</p>")

    assert item["name"] == "knf@knf.vu.lt +370\n37 422 523"
    assert item["phone"] == "+370 37 422 523"


def test_an_institutional_address_beside_a_mobile_keeps_only_the_address():
    item = _one_item("<p>Jonas, jonas@knf.vu.lt, +370 612 34567</p>")

    assert item == {"name": "Jonas", "email": "jonas@knf.vu.lt"}


def test_an_entry_puts_its_keys_in_the_order_the_app_reads_them():
    item = _one_item("<p>Jonas, jonas@knf.vu.lt, +370 37 422 523, 305 kab.</p>")

    assert list(item) == ["name", "phone", "email", "room"]






# -----------------------------------------------------------
# _scrape_contacts — the room number
# -----------------------------------------------------------


@pytest.mark.parametrize("written", ["305 kab.", "305 kabinetas", "305 room",
                                     "kab. 305", "kabinetas 305", "room 305", "kab305"])
def test_a_room_is_read_in_either_order_and_in_either_language(written):
    item = _one_item(f"<p>Registratūra, registratura@knf.vu.lt, {written}</p>")

    assert item["room"] == "305"


def test_the_room_word_is_matched_whatever_its_case():
    assert _one_item("<p>Registratūra, registratura@knf.vu.lt, ROOM 305</p>")["room"] == "305"


def test_a_four_digit_number_yields_only_its_last_three_digits_as_a_room():
    item = _one_item("<p>Registratūra, registratura@knf.vu.lt, 1305 kab.</p>")

    assert item["room"] == "305"


def test_a_two_digit_room_leaves_the_entry_without_one():
    item = _one_item("<p>Sandėlis, sandelis@knf.vu.lt, 12 kab.</p>")

    assert "room" not in item


def test_the_number_before_the_word_wins_over_the_number_after_it():
    item = _one_item("<p>Registratūra, registratura@knf.vu.lt, 205 kab. kab. 301</p>")

    assert item["room"] == "205"


def test_a_room_without_a_publishable_contact_detail_is_not_an_entry():
    assert _contacts("<p>Sandėlis, 305 kab.</p>") == []






# -----------------------------------------------------------
# _scrape_contacts — deduplication and volume
# -----------------------------------------------------------


def test_the_same_name_with_two_different_addresses_is_two_entries():
    items = _items(_contacts("<p>Jonas, pirma@knf.vu.lt</p><p>Jonas, antra@knf.vu.lt</p>"))

    assert [item["email"] for item in items] == ["pirma@knf.vu.lt", "antra@knf.vu.lt"]


def test_the_same_name_with_no_address_collapses_to_one_entry_and_loses_a_phone():
    # The dedupe key is (name, email) — two desks that share a
    # name and publish no address are one item, whatever their
    # numbers
    items = _items(_contacts("<p>Budėtojas, budetojas@gmail.com, +370 37 422 500</p>"
                             "<p>Budėtojas, budetojas@gmail.com, +370 37 422 501</p>"))

    assert items == [{"name": "Budėtojas", "phone": "+370 37 422 500"}]


def test_the_dedupe_set_starts_empty_again_at_every_heading():
    contacts = _contacts("<p>Jonas, jonas@knf.vu.lt</p>"
                         "<h2>Dekanatas</h2><p>Jonas, jonas@knf.vu.lt</p>")

    assert [category["category"] for category in contacts] == ["Kontaktai", "Dekanatas"]
    assert len(_items(contacts)) == 2


def test_a_category_holds_as_many_entries_as_the_page_lists():
    fragment = "".join(f"<p>Vardas{i}, vardas{i}@knf.vu.lt</p>" for i in range(100))

    assert len(_items(_contacts(fragment))) == 100






# -----------------------------------------------------------
# _scrape_staff — picking the content container
# -----------------------------------------------------------


def test_the_staff_selector_list_is_tried_in_order_not_in_document_order():
    html = ('<article><h2>Antra</h2><p>Prof. dr. Ona Onaitė</p></article>'
            '<div class="item-page"><h2>Pirma</h2><p>Prof. dr. Jonas Jonaitis</p></div>')
    departments = _scrape_staff(_soup(html))

    assert [department["department"] for department in departments] == ["Pirma"]


def test_a_page_that_never_downloaded_yields_no_departments():
    assert _scrape_staff(None) == []


def test_an_empty_document_yields_no_departments():
    assert _scrape_staff(_soup("")) == []


def test_a_container_with_no_headings_or_paragraphs_yields_no_departments():
    assert _staff("<span>Prof. dr. Jonas Jonaitis</span>") == []






# -----------------------------------------------------------
# _scrape_staff — departments
# -----------------------------------------------------------


def test_people_listed_before_the_first_heading_are_dropped():
    # Unlike the contacts walk there is no implicit department,
    # so anything above the first heading is never flushed
    departments = _staff("<p>Prof. dr. Jonas Jonaitis, dekanas</p>"
                         "<h2>Katedra</h2><p>Doc. dr. Ona Onaitė, vedėja</p>")

    assert departments == [{
        "department": "Katedra",
        "staff": [{"name": "Doc. dr. Ona Onaitė", "position": "vedėja"}],
    }]


def test_a_page_with_people_and_no_heading_at_all_yields_nothing():
    assert _staff("<p>Prof. dr. Jonas Jonaitis, dekanas</p>") == []


def test_a_whitespace_only_heading_does_not_close_the_department():
    departments = _staff("<h2>Katedra</h2><h3>   </h3><p>Prof. dr. Jonas Jonaitis</p>")

    assert [department["department"] for department in departments] == ["Katedra"]


def test_a_department_heading_with_inline_markup_keeps_its_spaces():
    departments = _staff("<h2>Informatikos <em>katedra</em></h2><p>Prof. dr. Jonas Jonaitis</p>")

    assert departments[0]["department"] == "Informatikos katedra"


def test_a_trailing_department_with_nobody_in_it_is_dropped():
    departments = _staff("<h2>Katedra</h2><p>Prof. dr. Jonas Jonaitis</p><h2>Tuščias skyrius</h2>")

    assert [department["department"] for department in departments] == ["Katedra"]


def test_an_h1_is_invisible_to_the_staff_walk():
    assert _staff("<h1>Struktūra</h1><p>Prof. dr. Jonas Jonaitis</p>") == []


def test_a_heading_that_happens_to_be_an_address_is_still_a_department_name():
    departments = _staff("<h2>knf@knf.vu.lt</h2><p>Prof. dr. Jonas Jonaitis</p>")

    assert departments[0]["department"] == "knf@knf.vu.lt"






# -----------------------------------------------------------
# _scrape_staff — what counts as a person
# -----------------------------------------------------------


@pytest.mark.parametrize("line, name", [
    ("Prof. dr. Jonas Jonaitis", "Prof. dr. Jonas Jonaitis"),
    ("Doc. Ona Onaitė", "Doc. Ona Onaitė"),
    ("Dr. Jonas Jonaitis", "Dr. Jonas Jonaitis"),
    ("Lekt. Ona Onaitė", "Lekt. Ona Onaitė"),
    ("Asist. Jonas Jonaitis", "Asist. Jonas Jonaitis"),
    ("Ona Onaitė, katedros vedėja", "Ona Onaitė"),
    ("PROF. DR. JONAS JONAITIS", "PROF. DR. JONAS JONAITIS"),
    ("Moksl. dr. Jonas Jonaitis", "Moksl. dr. Jonas Jonaitis"),
])
def test_every_academic_title_spelling_opens_a_staff_entry(line, name):
    assert _one_person(f"<p>{line}</p>")["name"] == name


def test_a_title_needs_the_dot_the_page_usually_writes():
    assert _staff("<h2>Katedra</h2><p>dr Jonas Jonaitis</p>") == []
    assert _staff("<h2>Katedra</h2><p>Profesorius Jonas Jonaitis</p>") == []


def test_the_vedej_title_needs_no_dot_but_still_needs_its_word_boundary():
    assert _one_person("<p>Ona Onaitė, katedros vedėja</p>")["name"] == "Ona Onaitė"
    assert _staff("<h2>Katedra</h2><p>Pavedėjas Jonas Jonaitis</p>") == []


def test_an_institutional_address_alone_makes_a_person_without_any_title():
    person = _one_person("<p>studijos@knf.vu.lt</p>")

    assert person == {"name": "studijos@knf.vu.lt", "email": "studijos@knf.vu.lt"}


def test_a_name_shaped_line_without_a_title_or_an_address_is_not_a_person():
    assert _staff("<h2>Katedra</h2><p>Jonas Jonaitis</p>") == []


def test_a_titled_line_with_a_private_address_is_a_person_without_the_address():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis, jonas@gmail.com</p>")

    assert person == {"name": "Prof. dr. Jonas Jonaitis"}


def test_a_line_of_one_hundred_and_ninety_nine_characters_is_still_a_person():
    line = "Prof. dr. Jonas Jonaitis, dekanas".ljust(199, "x")
    person = _one_person(f"<p>{line}</p>")

    assert person["name"] == "Prof. dr. Jonas Jonaitis"


def test_a_line_of_two_hundred_characters_is_prose():
    line = "Prof. dr. Jonas Jonaitis, dekanas".ljust(200, "x")

    assert _staff(f"<h2>Katedra</h2><p>{line}</p>") == []


def test_a_line_shorter_than_three_characters_is_skipped():
    assert _staff("<h2>Katedra</h2><p>dr</p><p></p><p>  </p>") == []


def test_a_non_breaking_space_between_the_title_and_the_name_still_reads_as_a_person():
    person = _one_person("<p>Prof.&nbsp;dr.&nbsp;Jonas&nbsp;Jonaitis</p>")

    assert person["name"] == "Prof.\xa0dr.\xa0Jonas\xa0Jonaitis"


def test_a_paragraph_nested_in_a_list_item_is_read_once():
    people = _people(_staff("<h2>Katedra</h2><ul><li><p>Prof. dr. Jonas Jonaitis</p></li></ul>"))

    assert [person["name"] for person in people] == ["Prof. dr. Jonas Jonaitis"]


def test_a_list_item_nested_in_a_list_item_is_read_once():
    people = _people(_staff("<h2>Katedra</h2>"
                            "<ul><li><ul><li>Prof. dr. Jonas Jonaitis</li></ul></li></ul>"))

    assert [person["name"] for person in people] == ["Prof. dr. Jonas Jonaitis"]


def test_the_same_person_written_twice_is_stored_twice():
    # There is no dedupe on this walk, unlike the contacts one
    people = _people(_staff("<h2>Katedra</h2><p>Prof. dr. Jonas Jonaitis</p>"
                            "<p>Prof. dr. Jonas Jonaitis</p>"))

    assert len(people) == 2


def test_a_department_holds_as_many_people_as_the_page_lists():
    fragment = "<h2>Katedra</h2>" + "".join(
        f"<p>Prof. dr. Jonas Jonaitis{i}</p>" for i in range(200))

    assert len(_people(_staff(fragment))) == 200






# -----------------------------------------------------------
# _scrape_staff — the fields of one person
# -----------------------------------------------------------


def test_the_name_is_everything_before_the_first_comma_capped_at_a_hundred():
    person = _one_person(f"<p>Prof. dr. {'A' * 130}, dekanas</p>")

    assert person["name"] == f"Prof. dr. {'A' * 130}"[:100]


def test_the_position_is_capped_at_a_hundred_characters_too():
    person = _one_person(f"<p>Prof. dr. Jonas Jonaitis, {'B' * 130}</p>")

    assert person["position"] == "B" * 100


def test_an_empty_second_part_yields_no_position_even_when_a_third_one_reads_like_a_role():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis,, dekanas</p>")

    assert "position" not in person


def test_a_blank_second_part_yields_no_position():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis,   , dekanas</p>")

    assert "position" not in person


def test_a_line_ending_in_a_comma_yields_no_position():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis,</p>")

    assert person == {"name": "Prof. dr. Jonas Jonaitis"}


def test_a_faculty_landline_and_the_role_before_it_both_land():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis, dekanas, +370 37 422 523</p>")

    assert person == {"name": "Prof. dr. Jonas Jonaitis", "phone": "+370 37 422 523",
                      "position": "dekanas"}


def test_a_mobile_number_never_reaches_the_phone_field():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis, dekanas, +370 612 34567</p>")

    assert "phone" not in person


def test_an_address_written_in_capitals_still_passes_the_gate():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis, JONAS@KNF.VU.LT</p>")

    assert person["email"] == "JONAS@KNF.VU.LT"


def test_a_person_puts_their_keys_in_the_order_the_app_reads_them():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis, dekanas, "
                         "jonas@knf.vu.lt, +370 37 422 523</p>")

    assert list(person) == ["name", "email", "phone", "position"]


def test_a_line_starting_with_a_comma_is_filed_under_an_empty_name():
    # Garbage in, garbage out: nothing drops an entry whose name
    # came out empty, and the person's name becomes their role
    person = _one_person("<p>, Jonas Jonaitis, dr.</p>")

    assert person == {"name": "", "position": "Jonas Jonaitis"}


def test_a_mobile_number_after_the_comma_is_not_republished_as_a_position():
    person = _one_person("<p>Prof. dr. Jonas Jonaitis, +370 612 34567</p>")

    assert "+370 612 34567" not in person.get("position", "")






# -----------------------------------------------------------
# The privacy gate, end to end through both walks
# -----------------------------------------------------------


def test_no_private_address_or_mobile_survives_the_contacts_walk():
    contacts = _contacts(
        "<h2>Dekanatas</h2>"
        "<p>Rėmėjas Petras, petras@gmail.com, +370 612 34567</p>"
        "<p>Studijų skyrius, studijos@knf.vu.lt, +370 37 422 604</p>"
        "<table><tr><td>Biblioteka</td>"
        "<td>biblioteka@yahoo.com (8-687) 12 34 56</td></tr></table>")
    blob = repr(contacts)

    assert "gmail.com" not in blob
    assert "yahoo.com" not in blob
    assert "612 34567" not in blob
    assert "8-687" not in blob
    assert _items(contacts) == [{"name": "Studijų skyrius", "phone": "+370 37 422 604",
                                 "email": "studijos@knf.vu.lt"}]


def test_no_private_address_survives_the_staff_walk():
    departments = _staff(
        "<h2>Katedra</h2>"
        "<p>Prof. dr. Jonas Jonaitis, dekanas, jonas.jonaitis@gmail.com</p>"
        "<p>Doc. dr. Ona Onaitė, vedėja, ona.onaite@knf.vu.lt</p>")
    blob = repr(departments)

    assert "gmail.com" not in blob
    assert "ona.onaite@knf.vu.lt" in blob
