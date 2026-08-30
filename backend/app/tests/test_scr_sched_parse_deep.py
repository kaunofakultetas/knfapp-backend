############################################################
#  [*] Tests — schedule_scraper, the pure parse helpers
#
#  The exhaustive branch-by-branch pass over the ten pure
#  functions the tvarkarasciai.vu.lt import is built out of.
#  Nothing here fetches, writes or sleeps: every one of these
#  is a total function of its arguments, so every arm, guard
#  and boundary is reachable by calling it.
#
#  The slice, and what this module pins about each:
#
#    _get_semester_label     — the Aug/Jul cutoff from both
#                              sides, the year - 1 spring
#                              shift, and the extremes the
#                              calendar allows
#    _strip_diacritics       — all eighteen Lithuanian
#                              letters, and what it leaves
#                              alone (other scripts, already-
#                              decomposed forms)
#    _parse_group_display_name — every programme in the
#                              ORDERED table, both candidate
#                              passes, the -M/-EN suffixes,
#                              the course sources, and the
#                              two slug fallbacks with their
#                              log lines
#    _lesson_hash            — determinism, width, per-field
#                              sensitivity, and the two
#                              collisions its "|" join admits
#    _joined_link_texts      — blanks, duplicates, nesting,
#                              and the codepoint sort that
#                              keeps the natural key stable
#    _extract_teacher_from_html / _extract_room_from_html
#                            — attribute present / absent /
#                              empty, the escaped-HTML lift,
#                              the label strip and its
#                              case-sensitivity, and the
#                              first-attribute-wins regex
#    _normalise_colour       — rgb()/rgba(), #rgb, #rrggbb,
#                              clamping, the near-misses that
#                              fall through, and the
#                              non-string inputs
#    _labelled_retake        — the identity check on the flag,
#                              all six scanned keys, and the
#                              keys that are NOT scanned
#    _extract_title_text     — the markup/plain fork, the
#                              first-link rule, the first-line
#                              fallback, and the empty result
#                              its caller reads as "skip this"
#
#  The one gap this pass found — the banner promised the
#  event title was scanned for the PERLAIKYMAS label and the
#  key tuple left it out — is closed, and its test asserts
#  the fixed behaviour: see the retake-in-the-title case.
############################################################


import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from html import escape

import pytest
from bs4 import BeautifulSoup

from app.scraper import schedule_scraper as ss


LOGGER_NAME = "app.scraper.schedule_scraper"




############################################################
# _academics / _rooms
############################################################
#
# One popover attribute in the shape the extractors read it:
# the value is HTML-ESCAPED HTML, which is why the module
# regex-lifts it, unescapes it and parses it a second time.
# Written as a bare <span> so the surrounding markup can
# never be what a test is actually asserting on.
#
# Used by:
#   - the _extract_teacher_from_html and
#     _extract_room_from_html sections
############################################################

def _academics(value: str) -> str:
    return f'<span data-academics="{escape(value, quote=True)}"></span>'


def _rooms(value: str) -> str:
    return f'<span data-rooms="{escape(value, quote=True)}"></span>'




############################################################
# _soup
############################################################
#
# _joined_link_texts takes a parsed fragment, not markup, so
# its tests build one the same way the extractors do.
#
# Used by:
#   - the _joined_link_texts section
############################################################

def _soup(markup: str):
    return BeautifulSoup(markup, "html.parser")




# ==========================================================
# _get_semester_label
# ==========================================================


@pytest.mark.parametrize("month", [8, 9, 10, 11, 12])
def test_every_autumn_month_keeps_its_own_calendar_year(month):
    assert ss._get_semester_label(datetime(2025, month, 15)) == "2025-R"


@pytest.mark.parametrize("month", [1, 2, 3, 4, 5, 6, 7])
def test_every_spring_month_borrows_the_previous_calendar_year(month):
    # The label carries the academic year's FIRST year, so the
    # whole of 2026's spring half reads "2025-P"
    assert ss._get_semester_label(datetime(2026, month, 15)) == "2025-P"


def test_the_cutoff_sits_exactly_between_the_last_july_second_and_the_first_august_one():
    last_of_spring = datetime(2026, 7, 31, 23, 59, 59, 999999)
    first_of_autumn = datetime(2026, 8, 1, 0, 0, 0)

    assert ss._get_semester_label(last_of_spring) == "2025-P"
    assert ss._get_semester_label(first_of_autumn) == "2026-R"
    # One microsecond apart, two different academic years
    assert first_of_autumn - last_of_spring == timedelta(microseconds=1)


def test_new_years_eve_and_new_years_day_are_the_same_semester():
    # December 31st and January 1st are one academic year apart
    # in the calendar and the SAME semester label — which is the
    # whole reason the spring half subtracts a year
    assert ss._get_semester_label(datetime(2025, 12, 31, 23, 59)) == "2025-R"
    assert ss._get_semester_label(datetime(2026, 1, 1, 0, 0)) == "2025-P"


def test_a_leap_day_is_an_ordinary_spring_date():
    assert ss._get_semester_label(datetime(2024, 2, 29)) == "2023-P"


def test_the_january_exam_session_belongs_to_the_spring_half():
    # January is deliberately spring: the exam session that
    # closes the autumn term must not open a new label
    assert ss._get_semester_label(datetime(2026, 1, 20)) == "2025-P"


def test_a_timezone_aware_date_is_labelled_off_its_own_wall_clock():
    aware = datetime(2026, 2, 9, 9, 0, tzinfo=timezone.utc)

    assert ss._get_semester_label(aware) == "2025-P"


def test_a_plain_date_is_labelled_like_a_datetime():
    # Only .month and .year are read, so a date works — the
    # callers pass datetimes, but nothing here demands one
    assert ss._get_semester_label(date(2026, 3, 1)) == "2025-P"
    assert ss._get_semester_label(date(2026, 9, 1)) == "2026-R"


def test_the_extreme_ends_of_the_calendar_still_produce_a_label():
    # Year 1 January underflows to a "0-P" label rather than
    # raising — it is nonsense, but it is not a crash
    assert ss._get_semester_label(datetime.min) == "0-P"
    assert ss._get_semester_label(datetime.max) == "9999-R"


def test_a_label_is_always_the_year_dash_one_letter_shape_the_purge_parses():
    for dt in (datetime(2025, 8, 1), datetime(2026, 7, 31)):
        label = ss._get_semester_label(dt)
        # _semester_key only understands this shape; a label it
        # cannot parse is left out of the purge forever
        assert ss._semester_key(label) is not None


def test_an_object_without_a_month_is_not_labelled():
    with pytest.raises(AttributeError):
        ss._get_semester_label(None)




# ==========================================================
# _strip_diacritics
# ==========================================================


@pytest.mark.parametrize("letter,folded", [
    ("ą", "a"), ("č", "c"), ("ę", "e"), ("ė", "e"),
    ("į", "i"), ("š", "s"), ("ų", "u"), ("ū", "u"),
    ("ž", "z"),
])
def test_every_lowercase_lithuanian_letter_folds_to_its_ascii_base(letter, folded):
    assert ss._strip_diacritics(letter) == folded


@pytest.mark.parametrize("letter,folded", [
    ("Ą", "A"), ("Č", "C"), ("Ę", "E"), ("Ė", "E"),
    ("Į", "I"), ("Š", "S"), ("Ų", "U"), ("Ū", "U"),
    ("Ž", "Z"),
])
def test_every_uppercase_lithuanian_letter_folds_and_keeps_its_case(letter, folded):
    assert ss._strip_diacritics(letter) == folded


def test_the_whole_alphabet_folds_in_one_pass():
    assert ss._strip_diacritics("ąčęėįšųūž") == "aceeisuuz"
    assert ss._strip_diacritics("ĄČĘĖĮŠŲŪŽ") == "ACEEISUUZ"


def test_an_empty_string_folds_to_an_empty_string():
    assert ss._strip_diacritics("") == ""


def test_ascii_text_survives_untouched():
    text = "Informacijos sistemos ir kibernetine sauga - 1 kursas (2 grupe)"

    assert ss._strip_diacritics(text) == text


def test_folding_is_idempotent():
    once = ss._strip_diacritics("Kūrybiškumo ir skaitmeninės retorikos")

    assert ss._strip_diacritics(once) == once


def test_letters_of_other_scripts_are_left_alone():
    # Only the nine Lithuanian pairs are in the table — a Polish
    # or French letter is not the scraper's problem
    assert ss._strip_diacritics("łéüñ") == "łéüñ"


def test_a_decomposed_letter_keeps_its_combining_mark():
    # "a" + COMBINING OGONEK is a different code point sequence
    # from U+0105 and the table does not see it — the feed sends
    # composed text, so this is documentation, not a wish
    decomposed = "ą"

    assert ss._strip_diacritics(decomposed) == decomposed


def test_case_is_never_changed_by_the_fold():
    assert ss._strip_diacritics("MENO VADYBA") == "MENO VADYBA"


def test_a_very_long_name_folds_without_truncation():
    assert ss._strip_diacritics("ą" * 50000) == "a" * 50000


def test_a_non_string_cannot_be_folded():
    with pytest.raises(AttributeError):
        ss._strip_diacritics(None)
    with pytest.raises(AttributeError):
        ss._strip_diacritics(2025)




# ==========================================================
# _parse_group_display_name — the ordered programme table
# ==========================================================


@pytest.mark.parametrize("display_name,expected", [
    ("Informacijos sistemos ir kibernetinė sauga - 1 kursas", "ISKS-1"),
    ("Lietuvių literatūra ir kūrybinis rašymas - 1 kursas", "LLKR-1"),
    ("Lietuvių filologija ir reklama - 1 kursas", "LFR-1"),
    ("Marketingas ir pardavimų vadyba - 1 kursas", "MPV-1"),
    ("Marketingo technologijos - 1 kursas", "MT-1"),
    ("Tarptautinio verslo vadyba - 1 kursas", "TVV-1"),
    ("Tvariųjų finansų ekonomika - 1 kursas", "TFE-1"),
    ("Finansų analitika - 1 kursas", "FA-1"),
    ("Finansų technologijos - 1 kursas", "FT-1"),
    ("Ekonomika ir vadyba - 1 kursas", "EV-1"),
    ("Audiovizualinis vertimas - 1 kursas", "AV-1"),
    ("Viešojo diskurso lingvistika - 1 kursas", "VDL-1"),
    ("Kalba ir dirbtinio intelekto valdymas - 1 kursas", "KDIV-1"),
    ("Meno vadyba - 1 kursas", "MV-1"),
    ("Art management - 1 kursas", "MV-1"),
    ("Anglų ir kita užsienio kalba - 1 kursas", "AKUK-1"),
])
def test_every_plain_programme_in_the_table_resolves_to_its_abbreviation(display_name, expected):
    assert ss._parse_group_display_name("grupe-1k", display_name) == expected


@pytest.mark.parametrize("display_name,expected", [
    ("Lietuvių filologija ir reklama (turinio kūrimas ir rinkodara) - 1 kursas", "LFR-TKR-1"),
    ("Lietuvių filologija ir reklama (kūrybiškumo ir skaitmeninės retorikos) - 1 kursas", "LFR-KSR-1"),
    ("Viešojo diskurso lingvistika (medijų retorika ir komunikacija) - 1 kursas", "VDL-MRK-1"),
    ("Audiovizualinis vertimas (skaitmeninio turinio prieinamumas) - 1 kursas", "AV-STP-1"),
])
def test_a_specialisation_beats_the_broader_programme_whose_words_it_repeats(display_name, expected):
    # These four names contain BOTH patterns; the table's order
    # is what stops two real timetables merging into one
    assert ss._parse_group_display_name("grupe-1k", display_name) == expected


def test_the_broader_programme_still_wins_when_the_specialisation_is_absent():
    # The mirror of the case above: without the parenthesis the
    # same programme has to fall through to its own abbreviation
    assert ss._parse_group_display_name(
        "grupe-1k", "Viešojo diskurso lingvistika - 1 kursas") == "VDL-1"
    assert ss._parse_group_display_name(
        "grupe-1k", "Audiovizualinis vertimas - 1 kursas") == "AV-1"


def test_a_programme_name_matches_on_its_stem_not_its_full_spelling():
    # The table stores "informacijos sistemos ir kibernetin", so
    # every declension of "sauga" behind it still matches
    assert ss._parse_group_display_name(
        "isks-2k", "Informacijos sistemos ir kibernetinės saugos studijos - 2 kursas") == "ISKS-2"


def test_the_match_is_diacritic_blind_because_the_name_is_folded_first():
    with_marks = ss._parse_group_display_name("g-1k", "Lietuvių filologija ir reklama - 1 kursas")
    without = ss._parse_group_display_name("g-1k", "Lietuviu filologija ir reklama - 1 kursas")

    assert with_marks == without == "LFR-1"


def test_the_match_is_case_blind_because_the_name_is_lowercased_first():
    assert ss._parse_group_display_name("g-1k", "MENO VADYBA - 1 KURSAS") == "MV-1"




# ==========================================================
# _parse_group_display_name — the two candidate passes
# ==========================================================


def test_the_display_name_is_tried_before_the_slug():
    # The name resolves to a LATER table entry than the slug
    # does; candidate order, not table order, decides
    name = ss._parse_group_display_name(
        "skaitmeninio-turinio-prieinamumas-1k", "Audiovizualinis vertimas - 1 kursas")

    assert name == "AV-1"


def test_a_de_hyphenated_slug_carries_the_programme_when_the_name_cannot():
    name = ss._parse_group_display_name(
        "informacijos-sistemos-ir-kibernetine-sauga-1k", "1 Grupė")

    assert name == "ISKS-1"


def test_a_slug_whose_words_are_only_the_abbreviation_matches_nothing():
    # "isks 1k" contains no table pattern, so the short slug
    # itself is the group name — this is the common real case
    assert ss._parse_group_display_name("isks-1k", "Tvarkaraštis") == "isks-1k"




# ==========================================================
# _parse_group_display_name — the course digit
# ==========================================================


@pytest.mark.parametrize("spelling", ["1 kursas", "1kursas", "1   kursas", "1\tkursas"])
def test_the_course_is_read_through_any_spacing_before_kursas(spelling):
    assert ss._parse_group_display_name("g-x", f"Meno vadyba - {spelling}") == "MV-1"


@pytest.mark.parametrize("course", ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"])
def test_every_single_digit_course_is_carried_through(course):
    assert ss._parse_group_display_name(
        "g-x", f"Meno vadyba - {course} kursas") == f"MV-{course}"


def test_a_two_digit_course_keeps_only_the_digit_next_to_kursas():
    # `(\d)\s*kursas` captures ONE digit, so "10 kursas" reads as
    # course 0 rather than 10 — no faculty has a tenth year, but
    # this is what the regex does
    assert ss._parse_group_display_name("g-x", "Meno vadyba - 10 kursas") == "MV-0"


def test_the_slug_supplies_the_course_when_the_name_has_none():
    assert ss._parse_group_display_name("mv-3k", "Meno vadyba") == "MV-3"


def test_the_slug_course_token_also_accepts_the_c_spelling():
    assert ss._parse_group_display_name("mv-3c", "Meno vadyba") == "MV-3"


def test_the_name_beats_the_slug_when_both_carry_a_course():
    assert ss._parse_group_display_name("mv-4k", "Meno vadyba - 2 kursas") == "MV-2"


def test_the_slug_course_token_is_read_anywhere_in_the_slug():
    # `(\d)[kc]` is a plain search, so a year in the slug can be
    # mistaken for a course — "2024c" answers "4"
    assert ss._parse_group_display_name("meno-2024c", "Meno vadyba") == "MV-4"


def test_the_slug_course_is_read_off_the_raw_slug_on_both_passes():
    # The second candidate de-hyphenates the slug, but the course
    # search still runs against the ORIGINAL, so a slug-only
    # match keeps its digit
    assert ss._parse_group_display_name(
        "kalba-ir-dirbtinio-intelekto-valdymas-2k", "") == "KDIV-2"




# ==========================================================
# _parse_group_display_name — the -M and -EN suffixes
# ==========================================================


def test_a_masters_group_is_marked_from_the_display_name():
    assert ss._parse_group_display_name(
        "mv-1k", "Meno vadyba, magistrantūros studijos - 1 kursas") == "MV-M-1"


def test_the_masters_marker_is_the_stem_so_every_declension_counts():
    for spelling in ("magistrantūra", "magistrantūros", "magistrantams"):
        assert ss._parse_group_display_name(
            "mv-1k", f"Meno vadyba {spelling} - 1 kursas") == "MV-M-1"


def test_a_masters_slug_marks_the_group_when_the_slug_is_the_matching_candidate():
    # "magistrant" is looked for in the CURRENT candidate, and on
    # the slug pass that candidate is the de-hyphenated slug
    name = ss._parse_group_display_name("meno-vadyba-magistrantura-1k", "1 Grupė")

    assert name == "MV-M-1"


def test_a_masters_slug_is_ignored_when_the_display_name_already_matched():
    # The name matched on the first pass, so the slug's own
    # "magistrantura" is never consulted for the level suffix
    name = ss._parse_group_display_name("magistrantura-mv-1k", "Meno vadyba - 1 kursas")

    assert name == "MV-1"


@pytest.mark.parametrize("marker", ["anglų kalba", "anglų k.", "in English",
                                    "English-taught", "english taught", "(EN)"])
def test_an_explicit_english_marker_in_the_name_adds_the_suffix(marker):
    assert ss._parse_group_display_name(
        "mv-1k", f"Meno vadyba {marker} - 1 kursas") == "MV-EN-1"


def test_an_en_token_in_the_slug_adds_the_suffix_on_its_own():
    assert ss._parse_group_display_name(
        "tvv-1k-en", "Tarptautinio verslo vadyba - 1 kursas") == "TVV-EN-1"


@pytest.mark.parametrize("slug", ["meno-vadyba-1k", "menotyra-1k", "renginiai-1k"])
def test_an_en_inside_a_word_never_adds_the_suffix(slug):
    # The token needs a boundary either side, or half the
    # Lithuanian slugs on the site would be tagged English
    assert ss._parse_group_display_name(slug, "Meno vadyba - 1 kursas") == "MV-1"


def test_the_lithuanian_english_language_programme_is_never_tagged_english():
    # "Anglų ir kita užsienio kalba" is taught in Lithuanian; the
    # bare "angl" substring used to tag every one of its groups
    assert ss._parse_group_display_name(
        "akuk-2k", "Anglų ir kita užsienio kalba - 2 kursas") == "AKUK-2"


def test_the_english_marker_still_fires_when_that_programme_really_is_in_english():
    # "anglų kalba" as an adjacent pair IS the marker, even
    # inside the programme whose name carries "anglų"
    assert ss._parse_group_display_name(
        "akuk-2k", "Anglų ir kita užsienio kalba, anglų kalba - 2 kursas") == "AKUK-EN-2"


def test_both_suffixes_appear_in_the_documented_order():
    name = ss._parse_group_display_name(
        "mv-1k", "Meno vadyba magistrantūra anglų k. - 1 kursas")

    assert name == "MV-M-EN-1"




# ==========================================================
# _parse_group_display_name — the course-less pools and the
# slug fallbacks
# ==========================================================


def test_the_two_course_less_pools_keep_a_bare_abbreviation():
    assert ss._parse_group_display_name(
        "bus", "Bendrųjų universitetinių studijų dalykai") == "BUS"
    assert ss._parse_group_display_name(
        "isd", "Individualiųjų studijų dalykai") == "ISD"


def test_a_course_less_pool_still_takes_a_course_when_one_is_there():
    assert ss._parse_group_display_name(
        "bus-2k", "Bendrųjų universitetinių studijų dalykai - 2 kursas") == "BUS-2"


def test_a_course_less_pool_still_takes_the_suffixes():
    assert ss._parse_group_display_name(
        "isd-x", "Individualiųjų studijų dalykai anglų kalba") == "ISD-EN"
    assert ss._parse_group_display_name(
        "bus-x", "Bendrųjų universitetinių studijų dalykai magistrantūra") == "BUS-M"


def test_a_course_bearing_programme_with_no_course_keeps_its_unique_slug(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        name = ss._parse_group_display_name("ekonomika-vadyba-grupe", "Ekonomika ir vadyba")

    # "EV" would pool all four years into one timetable
    assert name == "ekonomika-vadyba-grupe"
    assert "No course in the EV group" in caplog.text
    assert "ekonomika-vadyba-grupe" in caplog.text


def test_the_course_less_fallback_is_capped_at_thirty_characters(caplog):
    slug = "ekonomika-ir-vadyba-be-jokio-kurso-numerio"

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        name = ss._parse_group_display_name(slug, "Ekonomika ir vadyba")

    assert name == slug[:30]
    assert len(name) == 30


def test_a_name_matching_nothing_at_all_keeps_the_slug_and_logs_at_info(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        name = ss._parse_group_display_name("kazkokia-grupe", "Visiškai nežinoma programa")

    assert name == "kazkokia-grupe"
    assert "No programme matched" in caplog.text


@pytest.mark.parametrize("length,expected", [(29, 29), (30, 30), (31, 30), (200, 30)])
def test_the_unmatched_slug_fallback_is_capped_at_thirty(length, expected):
    slug = "z" * length

    assert len(ss._parse_group_display_name(slug, "nieko")) == expected


def test_two_empty_strings_produce_an_empty_group_name():
    # Nothing matches and the slug is empty, so the group name is
    # empty too — degenerate, but it does not raise
    assert ss._parse_group_display_name("", "") == ""


def test_a_missing_display_name_is_refused_before_anything_is_matched():
    with pytest.raises(AttributeError):
        ss._parse_group_display_name("mv-1k", None)


def test_a_missing_slug_is_refused_too():
    with pytest.raises(AttributeError):
        ss._parse_group_display_name(None, "Meno vadyba - 1 kursas")


def test_parallel_subgroups_of_one_course_collapse_to_the_same_name():
    names = {
        ss._parse_group_display_name(
            f"isks-1k-{n}gr",
            f"Informacijos sistemos ir kibernetinė sauga - 1 kursas {n} grupė")
        for n in (1, 2, 3)
    }

    assert names == {"ISKS-1"}


def test_two_courses_of_one_programme_never_collapse_together():
    first = ss._parse_group_display_name("isks-1k", "Informacijos sistemos ir kibernetinė sauga - 1 kursas")
    second = ss._parse_group_display_name("isks-2k", "Informacijos sistemos ir kibernetinė sauga - 2 kursas")

    assert first != second




# ==========================================================
# _lesson_hash
# ==========================================================


# The eight identity fields in the order the function takes
# them, as a baseline every mutation test perturbs
_BASE_LESSON = ("Programavimas", "J. Jonaitis", "301", "08:30", "10:00", 0, "ISKS-1", "2025-P")


def test_the_hash_is_sixteen_lowercase_hex_characters():
    digest = ss._lesson_hash(*_BASE_LESSON)

    assert len(digest) == 16
    assert all(character in "0123456789abcdef" for character in digest)


def test_the_hash_is_the_documented_sha256_prefix_of_the_pipe_joined_key():
    key = "Programavimas|J. Jonaitis|301|08:30|10:00|0|ISKS-1|2025-P"

    assert ss._lesson_hash(*_BASE_LESSON) == hashlib.sha256(key.encode()).hexdigest()[:16]


def test_the_same_lesson_hashes_the_same_every_time():
    assert ss._lesson_hash(*_BASE_LESSON) == ss._lesson_hash(*_BASE_LESSON)


@pytest.mark.parametrize("index,replacement", [
    (0, "Duomenų bazės"), (1, "A. Petraitis"), (2, "302"), (3, "08:45"),
    (4, "10:15"), (5, 1), (6, "ISKS-2"), (7, "2025-R"),
])
def test_changing_any_one_identity_field_changes_the_hash(index, replacement):
    mutated = list(_BASE_LESSON)
    mutated[index] = replacement

    assert ss._lesson_hash(*mutated) != ss._lesson_hash(*_BASE_LESSON)


def test_an_empty_teacher_and_room_still_hash():
    digest = ss._lesson_hash("Programavimas", "", "", "08:30", "10:00", 0, "ISKS-1", "2025-P")

    assert len(digest) == 16


def test_none_fields_hash_as_the_string_none_rather_than_raising():
    # Nothing upstream sends None any more, but the f-string
    # would swallow one silently rather than blow up mid-scrape
    assert ss._lesson_hash("t", None, None, "08:30", "10:00", 0, "g", "s") == \
        ss._lesson_hash("t", "None", "None", "08:30", "10:00", 0, "g", "s")


def test_a_weekday_int_and_its_string_hash_alike():
    # The key is built by f-string, so 0 and "0" are one value —
    # harmless while the caller always passes datetime.weekday()
    assert ss._lesson_hash("t", "te", "r", "08:30", "10:00", 0, "g", "s") == \
        ss._lesson_hash("t", "te", "r", "08:30", "10:00", "0", "g", "s")


def test_a_pipe_inside_a_field_can_shift_the_key_across_a_boundary():
    # "a|b" + "c" and "a" + "b|c" join to the same string; the
    # hash is an in-memory dedup key inside ONE group's scrape,
    # so the collision has no path to the database
    with_pipe = ss._lesson_hash("a|b", "c", "r", "08:30", "10:00", 0, "g", "s")
    without = ss._lesson_hash("a", "b|c", "r", "08:30", "10:00", 0, "g", "s")

    assert with_pipe == without


def test_unicode_fields_hash_through_utf_8():
    digest = ss._lesson_hash("Kūrybinis rašymas", "Ž. Žilinskaitė", "Š-301",
                             "08:30", "10:00", 0, "LLKR-1", "2025-P")
    key = "Kūrybinis rašymas|Ž. Žilinskaitė|Š-301|08:30|10:00|0|LLKR-1|2025-P"

    assert digest == hashlib.sha256(key.encode()).hexdigest()[:16]


def test_every_weekday_of_one_slot_is_its_own_lesson():
    digests = {ss._lesson_hash("Programavimas", "J. J.", "301", "08:30", "10:00", day, "ISKS-1", "2025-P")
               for day in range(7)}

    assert len(digests) == 7




# ==========================================================
# _joined_link_texts
# ==========================================================


def test_a_fragment_without_links_joins_to_an_empty_string():
    assert ss._joined_link_texts(_soup("<p>Dėstytojai: J. Jonaitis</p>")) == ""


def test_an_entirely_empty_fragment_joins_to_an_empty_string():
    assert ss._joined_link_texts(_soup("")) == ""


def test_a_single_link_is_returned_bare():
    assert ss._joined_link_texts(_soup('<a href="#">J. Jonaitis</a>')) == "J. Jonaitis"


def test_link_texts_are_sorted_so_the_natural_key_is_stable():
    one_order = ss._joined_link_texts(_soup("<a>B. Jonaitis</a><a>A. Petraitis</a>"))
    other_order = ss._joined_link_texts(_soup("<a>A. Petraitis</a><a>B. Jonaitis</a>"))

    assert one_order == other_order == "A. Petraitis, B. Jonaitis"


def test_duplicate_link_texts_collapse_to_one():
    assert ss._joined_link_texts(_soup("<a>J. Jonaitis</a><a>J. Jonaitis</a>")) == "J. Jonaitis"


def test_an_empty_link_is_dropped_rather_than_joined_as_a_gap():
    assert ss._joined_link_texts(_soup("<a></a><a>305</a>")) == "305"


def test_a_whitespace_only_link_is_dropped_too():
    assert ss._joined_link_texts(_soup("<a>   </a><a>\n\t</a>")) == ""


def test_surrounding_whitespace_is_stripped_from_every_link():
    assert ss._joined_link_texts(_soup("<a>  305  </a><a>\n204\n</a>")) == "204, 305"


def test_links_nested_anywhere_in_the_fragment_are_found():
    markup = "<div><ul><li><a>305</a></li></ul></div><span><a>204</a></span>"

    assert ss._joined_link_texts(_soup(markup)) == "204, 305"


def test_a_links_inner_markup_is_flattened_into_its_text():
    # get_text(strip=True) strips each string BEFORE joining, so
    # the space between the two halves goes with it — the site
    # marks the whole name up as one string, so this never bites
    assert ss._joined_link_texts(_soup("<a><b>J.</b> Jonaitis</a>")) == "J.Jonaitis"
    assert ss._joined_link_texts(_soup("<a>J. Jonaitis</a>")) == "J. Jonaitis"


def test_an_anchor_without_text_contributes_nothing():
    # <a name="..."> targets are anchors too and have no text
    assert ss._joined_link_texts(_soup('<a name="top"></a>')) == ""


def test_the_sort_is_by_code_point_not_by_lithuanian_alphabet():
    # Uppercase sorts before lowercase and "Ž" lands last — the
    # order only has to be STABLE, never linguistically right
    assert ss._joined_link_texts(_soup("<a>Ž</a><a>Z</a><a>a</a><a>A</a>")) == "A, Z, a, Ž"


def test_the_separator_is_a_comma_and_a_space():
    assert ss._joined_link_texts(_soup("<a>A</a><a>B</a><a>C</a>")) == "A, B, C"




# ==========================================================
# _extract_teacher_from_html
# ==========================================================


def test_no_academics_attribute_yields_no_teacher():
    assert ss._extract_teacher_from_html("<a href='#'>Programavimas</a>") == ""


def test_a_title_with_no_markup_at_all_yields_no_teacher():
    assert ss._extract_teacher_from_html("Programavimas") == ""


def test_an_empty_academics_attribute_yields_an_empty_teacher():
    # The attribute matches, the lift succeeds and the parse
    # finds nothing — every branch runs and the answer is ""
    assert ss._extract_teacher_from_html('<span data-academics=""></span>') == ""


def test_a_single_linked_lecturer_is_returned():
    assert ss._extract_teacher_from_html(
        _academics('<a href="#">J. Jonaitis</a>')) == "J. Jonaitis"


def test_co_taught_lecturers_are_deduplicated_and_sorted():
    markup = _academics('<a href="#">B. Jonaitis</a><a href="#">A. Petraitis</a>'
                        '<a href="#">B. Jonaitis</a><a href="#"></a>')

    assert ss._extract_teacher_from_html(markup) == "A. Petraitis, B. Jonaitis"


def test_a_link_less_popover_falls_back_to_its_flattened_text():
    assert ss._extract_teacher_from_html(_academics("J. Jonaitis")) == "J. Jonaitis"


def test_the_lecturer_label_is_stripped_from_the_link_less_form():
    assert ss._extract_teacher_from_html(
        _academics("Dėstytojai: J. Jonaitis")) == "J. Jonaitis"


def test_the_lecturer_label_is_stripped_without_a_space_after_the_colon():
    assert ss._extract_teacher_from_html(
        _academics("Dėstytojai:J. Jonaitis")) == "J. Jonaitis"


def test_a_label_only_popover_leaves_nothing_behind():
    assert ss._extract_teacher_from_html(_academics("Dėstytojai:")) == ""


def test_the_label_strip_is_case_sensitive_and_anchored():
    # Only the site's own capitalised, leading label goes; a
    # lowercase one or one further in survives as data
    assert ss._extract_teacher_from_html(
        _academics("dėstytojai: J. Jonaitis")) == "dėstytojai: J. Jonaitis"
    assert ss._extract_teacher_from_html(
        _academics("Kursas, Dėstytojai: J. Jonaitis")) == "Kursas, Dėstytojai: J. Jonaitis"


def test_the_label_is_never_stripped_from_the_linked_form():
    # With links the label is not part of any <a>, so it is gone
    # already — this pins that the two paths do not interfere
    markup = _academics('Dėstytojai: <a href="#">J. Jonaitis</a>')

    assert ss._extract_teacher_from_html(markup) == "J. Jonaitis"


def test_a_lecturer_name_with_a_trailing_comma_is_handed_back_untrimmed():
    # The comma trim lives in scrape_group_schedule, not here
    assert ss._extract_teacher_from_html(_academics("J. Jonaitis, ")) == "J. Jonaitis,"


def test_the_escaped_markup_inside_the_attribute_really_is_re_parsed():
    # The value arrives as "&lt;a&gt;J. Jonaitis&lt;/a&gt;" — one
    # unescape short and the whole tag would come back as text
    raw = '<span data-academics="&lt;a&gt;J. Jonaitis&lt;/a&gt;"></span>'

    assert ss._extract_teacher_from_html(raw) == "J. Jonaitis"


def test_the_first_academics_attribute_wins_when_the_markup_repeats_it():
    raw = '<span data-academics="A. Petraitis" data-academics="B. Jonaitis"></span>'

    assert ss._extract_teacher_from_html(raw) == "A. Petraitis"


def test_an_unescaped_quote_inside_the_value_truncates_the_lift():
    # The regex reads up to the next double quote, so markup the
    # site never actually emits is cut short rather than mis-read
    raw = '<span data-academics="A. Petraitis" B. Jonaitis"></span>'

    assert ss._extract_teacher_from_html(raw) == "A. Petraitis"


def test_the_rooms_attribute_is_not_mistaken_for_the_lecturers():
    raw = _rooms("Patalpos: 301")

    assert ss._extract_teacher_from_html(raw) == ""


def test_both_attributes_on_one_span_are_read_independently():
    raw = ('<span data-rooms="Patalpos: 301" '
           'data-academics="D&#279;stytojai: A. Petraitis"></span>')

    assert ss._extract_teacher_from_html(raw) == "A. Petraitis"
    assert ss._extract_room_from_html(raw) == "301"


def test_a_multi_element_link_less_popover_flattens_without_separators():
    # get_text(strip=True) concatenates, so two blocks run
    # together — pinned so a future separator is a visible change
    assert ss._extract_teacher_from_html(
        _academics("<div>A. Petraitis</div><div>B. Jonaitis</div>")) == "A. PetraitisB. Jonaitis"


def test_a_non_string_title_cannot_be_searched_for_lecturers():
    with pytest.raises(TypeError):
        ss._extract_teacher_from_html(None)




# ==========================================================
# _extract_room_from_html
# ==========================================================


def test_no_rooms_attribute_yields_no_room():
    assert ss._extract_room_from_html("<a href='#'>Programavimas</a>") == ""


def test_a_plain_text_title_yields_no_room():
    assert ss._extract_room_from_html("Programavimas") == ""


def test_an_empty_rooms_attribute_yields_an_empty_room():
    assert ss._extract_room_from_html('<span data-rooms=""></span>') == ""


def test_a_single_linked_room_is_returned():
    assert ss._extract_room_from_html(_rooms('<a href="#">305</a>')) == "305"


def test_a_lecture_split_across_two_rooms_keeps_both_sorted():
    assert ss._extract_room_from_html(
        _rooms('<a href="#">305</a><a href="#">204</a>')) == "204, 305"


def test_a_repeated_room_link_collapses_to_one():
    assert ss._extract_room_from_html(
        _rooms('<a href="#">305</a><a href="#">305</a>')) == "305"


def test_a_link_less_room_popover_falls_back_to_its_text():
    assert ss._extract_room_from_html(_rooms("301")) == "301"


def test_the_room_label_is_stripped_from_the_link_less_form():
    assert ss._extract_room_from_html(_rooms("Patalpos: 301")) == "301"
    assert ss._extract_room_from_html(_rooms("Patalpos:301")) == "301"


def test_a_room_label_with_nothing_after_it_leaves_nothing():
    assert ss._extract_room_from_html(_rooms("Patalpos:")) == ""


def test_the_room_label_strip_is_case_sensitive_and_anchored():
    assert ss._extract_room_from_html(_rooms("patalpos: 301")) == "patalpos: 301"
    assert ss._extract_room_from_html(_rooms("Rūmai, Patalpos: 301")) == "Rūmai, Patalpos: 301"


def test_the_escaped_room_markup_is_re_parsed_into_links():
    raw = '<span data-rooms="&lt;a&gt;305&lt;/a&gt;&lt;a&gt;204&lt;/a&gt;"></span>'

    assert ss._extract_room_from_html(raw) == "204, 305"


def test_the_first_rooms_attribute_wins_when_the_markup_repeats_it():
    raw = '<span data-rooms="204" data-rooms="305"></span>'

    assert ss._extract_room_from_html(raw) == "204"


def test_the_academics_attribute_is_not_mistaken_for_the_rooms():
    assert ss._extract_room_from_html(_academics("Dėstytojai: J. Jonaitis")) == ""


def test_a_room_name_keeps_its_own_punctuation():
    assert ss._extract_room_from_html(_rooms("Patalpos: 305 a. (Muitinės g. 8)")) == \
        "305 a. (Muitinės g. 8)"


def test_a_non_string_title_cannot_be_searched_for_rooms():
    with pytest.raises(TypeError):
        ss._extract_room_from_html(None)




# ==========================================================
# _normalise_colour
# ==========================================================


@pytest.mark.parametrize("value", ["", "   ", "\t\n", None, 0, False, [], {}])
def test_every_empty_shaped_colour_normalises_to_an_empty_string(value):
    # `str(value or "")` is what folds them all together — the
    # caller reads "" as "this event has no colour"
    assert ss._normalise_colour(value) == ""


def test_an_already_normal_hex_is_returned_unchanged():
    assert ss._normalise_colour("#ff899d") == "#ff899d"


def test_an_uppercase_hex_is_lowercased():
    assert ss._normalise_colour("#FF899D") == "#ff899d"


def test_surrounding_whitespace_is_stripped_before_anything_else():
    assert ss._normalise_colour("  \n#FF899D\t ") == "#ff899d"


def test_a_hex_without_its_hash_gets_one():
    assert ss._normalise_colour("ff899d") == "#ff899d"


def test_repeated_leading_hashes_are_all_stripped():
    # lstrip("#") removes every one of them, so "##ff899d" is the
    # retake colour too
    assert ss._normalise_colour("##ff899d") == "#ff899d"


@pytest.mark.parametrize("short,full", [
    ("#f9d", "#ff99dd"), ("#abc", "#aabbcc"), ("#000", "#000000"),
    ("#fff", "#ffffff"), ("f9d", "#ff99dd"),
])
def test_the_three_digit_shorthand_is_doubled(short, full):
    assert ss._normalise_colour(short) == full


def test_the_shorthand_of_the_retake_colour_is_not_the_retake_colour():
    # "#f9d" doubles to "#ff99dd", which is a DIFFERENT pink —
    # the shorthand is expanded faithfully, not guessed at
    assert ss._normalise_colour("#f9d") not in ss._RETAKE_COLOURS


@pytest.mark.parametrize("value,expected", [
    ("rgb(255, 137, 157)", "#ff899d"),
    ("rgb(255,137,157)", "#ff899d"),
    ("rgb( 255 , 137 , 157 )", "#ff899d"),
    ("RGB(255, 137, 157)", "#ff899d"),
    ("rgba(255, 137, 157, 0.5)", "#ff899d"),
    ("rgba(0, 0, 0, 0)", "#000000"),
    ("rgb(0,0,0)", "#000000"),
    ("rgb(255,255,255)", "#ffffff"),
    ("rgb(7,8,9)", "#070809"),
    ("rgb(007,008,009)", "#070809"),
])
def test_every_rgb_notation_collapses_to_the_hex_the_table_is_written_in(value, expected):
    assert ss._normalise_colour(value) == expected


@pytest.mark.parametrize("channel,expected", [
    ("255", "#ff0000"), ("256", "#ff0000"), ("999", "#ff0000"), ("254", "#fe0000"),
])
def test_an_rgb_channel_is_clamped_at_the_top_of_the_byte(channel, expected):
    assert ss._normalise_colour(f"rgb({channel}, 0, 0)") == expected


def test_an_unterminated_rgb_is_still_read():
    # The pattern stops after the third channel, so the closing
    # bracket is optional — a truncated value still normalises
    assert ss._normalise_colour("rgb(255,137,157") == "#ff899d"


def test_trailing_junk_after_an_rgb_is_ignored():
    assert ss._normalise_colour("rgb(255,137,157) !important") == "#ff899d"


@pytest.mark.parametrize("value", [
    "rgb(1000,0,0)",        # four digits — the channel pattern caps at three
    "rgb(-1,0,0)",          # a sign is not a digit
    "rgb(255,0)",           # only two channels
    "rgb(255 137 157)",     # no commas
    " rgb(255,137,157)",    # leading space is stripped, but see below
])
def test_a_near_miss_rgb_is_handed_back_rather_than_guessed_at(value):
    stripped = value.strip().lower()
    # The one exception is the leading-space case, which strips
    # into a perfectly good rgb() — asserted for what it is
    if stripped.startswith("rgb(255,137,157"):
        assert ss._normalise_colour(value) == "#ff899d"
    else:
        assert ss._normalise_colour(value) == stripped


@pytest.mark.parametrize("value", ["#ff899", "#ff899dd", "#ff899dff", "#gggggg", "#12345"])
def test_a_hex_of_the_wrong_width_or_alphabet_falls_through_unchanged(value):
    assert ss._normalise_colour(value) == value


def test_a_named_colour_comes_back_lowercased_and_never_dropped():
    # The caller counts every colour it sees; dropping one would
    # hide a palette change instead of showing it
    assert ss._normalise_colour("CornflowerBlue") == "cornflowerblue"
    assert ss._normalise_colour("RED") == "red"


def test_a_gradient_is_handed_back_whole():
    assert ss._normalise_colour("linear-gradient(red, blue)") == "linear-gradient(red, blue)"


@pytest.mark.parametrize("value,expected", [
    (True, "true"), (12345, "12345"), (1.5, "1.5"), ([1, 2], "[1, 2]"),
])
def test_a_non_string_colour_is_stringified_rather_than_refused(value, expected):
    assert ss._normalise_colour(value) == expected


def test_every_notation_of_the_retake_pink_lands_inside_the_retake_table():
    for notation in ("#FF899D", "#ff899d", "  #Ff899D ", "ff899d", "##ff899d",
                     "rgb(255, 137, 157)", "rgba(255,137,157,1)"):
        assert ss._normalise_colour(notation) in ss._RETAKE_COLOURS


def test_an_ordinary_lecture_colour_is_outside_the_retake_table():
    for notation in ("#3a87ad", "rgb(58, 135, 173)", "cornflowerblue"):
        assert ss._normalise_colour(notation) not in ss._RETAKE_COLOURS


def test_normalising_is_idempotent():
    for value in ("#FF899D", "rgb(255,137,157)", "#f9d", "CornflowerBlue", "rgb(1000,0,0)"):
        once = ss._normalise_colour(value)
        assert ss._normalise_colour(once) == once




# ==========================================================
# _labelled_retake
# ==========================================================


def test_an_empty_event_is_not_a_retake():
    assert ss._labelled_retake({}) is False


def test_an_ordinary_lecture_is_not_a_retake():
    assert ss._labelled_retake(
        {"subtitle": "Paskaita", "description": "Auditorija 301", "type": "lecture"}) is False


def test_the_boolean_retake_flag_is_honoured():
    assert ss._labelled_retake({"retake": True}) is True


@pytest.mark.parametrize("flag", [1, "true", "yes", [1], {"a": 1}])
def test_a_merely_truthy_retake_flag_is_not_the_boolean_one(flag):
    # The check is deliberately `is True`, so only a real JSON
    # boolean counts — anything else has to be labelled instead
    assert ss._labelled_retake({"retake": flag}) is False


@pytest.mark.parametrize("flag", [False, None, 0, ""])
def test_a_falsey_retake_flag_leaves_the_decision_to_the_labels(flag):
    assert ss._labelled_retake({"retake": flag}) is False
    assert ss._labelled_retake({"retake": flag, "subtitle": "PERLAIKYMAS"}) is True


@pytest.mark.parametrize("key", ["subtitle", "title", "description", "type",
                                 "category", "eventType"])
def test_the_label_is_read_from_every_scanned_field(key):
    assert ss._labelled_retake({key: "Egzaminas (PERLAIKYMAS)"}) is True


@pytest.mark.parametrize("key", ["subtitle", "description", "type", "category", "eventType"])
def test_a_scanned_field_holding_none_is_not_a_retake(key):
    # str(None).upper() is "NONE" — close to nothing, but it is
    # the branch a null field takes
    assert ss._labelled_retake({key: None}) is False


@pytest.mark.parametrize("spelling", [
    "PERLAIKYMAS", "perlaikymas", "Perlaikymas", "perlaikymo egzaminas",
    "PERLAIKYMAI", "Egzaminų perlaikymui", "xxPERLAIKYMxx",
])
def test_the_label_matches_on_its_stem_in_any_case(spelling):
    # The test is the stem "PERLAIKYM" uppercased, so every
    # Lithuanian declension of the word is caught
    assert ss._labelled_retake({"subtitle": spelling}) is True


@pytest.mark.parametrize("near_miss", ["perlaikym", "PERLAIKY", "laikymas", "perlaidos"])
def test_a_word_short_of_the_stem_is_not_a_retake(near_miss):
    assert ss._labelled_retake({"subtitle": near_miss}) is (near_miss == "perlaikym")


@pytest.mark.parametrize("key", ["name", "summary", "notes", "kind", "status"])
def test_an_unscanned_field_carrying_the_label_is_ignored(key):
    # Only the five documented keys are read; a sixth would be a
    # deliberate change, not an accident
    assert ss._labelled_retake({key: "PERLAIKYMAS"}) is False


def test_a_structured_field_is_stringified_before_the_search():
    # str() of a dict or a list still contains the word, so a
    # nested label is caught rather than skipped
    assert ss._labelled_retake({"type": {"name": "perlaikymas"}}) is True
    assert ss._labelled_retake({"category": ["egzaminas", "PERLAIKYMAS"]}) is True


def test_the_first_matching_field_is_enough():
    assert ss._labelled_retake(
        {"subtitle": "Paskaita", "category": "PERLAIKYMAS", "eventType": "lecture"}) is True


def test_the_answer_is_always_a_real_boolean():
    for event in ({}, {"retake": True}, {"subtitle": "PERLAIKYMAS"}, {"subtitle": "Paskaita"}):
        assert isinstance(ss._labelled_retake(event), bool)


def test_a_non_mapping_event_is_refused():
    with pytest.raises(AttributeError):
        ss._labelled_retake(None)


# The title is scanned as written, popover markup and all —
# before it joined the key tuple, a retake named only there
# and painted an ordinary colour was imported as a weekly
# lesson and shown to students every week
def test_a_retake_named_only_in_the_title_is_recognised():
    assert ss._labelled_retake({"title": "Programavimas (PERLAIKYMAS)"}) is True




# ==========================================================
# _extract_title_text
# ==========================================================


def test_a_plain_title_is_returned_stripped():
    assert ss._extract_title_text("  Programavimas  ") == "Programavimas"


def test_an_empty_title_stays_empty():
    # "" is what scrape_group_schedule reads as "skip this event"
    assert ss._extract_title_text("") == ""


def test_a_whitespace_only_title_collapses_to_empty():
    assert ss._extract_title_text("   \n\t ") == ""


def test_a_plain_title_keeps_its_inner_punctuation_and_diacritics():
    assert ss._extract_title_text("Duomenų bazės ir SQL, II d.") == "Duomenų bazės ir SQL, II d."


def test_a_plain_title_keeps_its_ampersand_because_nothing_parses_it():
    # No "<" means no BeautifulSoup, so an entity stays literal
    assert ss._extract_title_text("R &amp; D") == "R &amp; D"


def test_the_first_link_of_a_popover_title_is_the_course():
    markup = '<a href="/dalykas/1/">Duomenų bazės</a><span data-rooms="301"></span>'

    assert ss._extract_title_text(markup) == "Duomenų bazės"


def test_only_the_first_link_is_taken():
    assert ss._extract_title_text("<a>Pirmas</a><a>Antras</a>") == "Pirmas"


def test_the_first_link_wins_wherever_it_is_nested():
    assert ss._extract_title_text("<div><span>x</span><p><a>Gilus</a></p></div>") == "Gilus"


def test_a_links_inner_markup_is_flattened():
    assert ss._extract_title_text("<a><b>Duomenų</b> bazės</a>") == "Duomenųbazės"


def test_an_empty_first_link_makes_the_event_untitled():
    # The link IS found, so the text fallback never runs and the
    # caller counts the event as untitled — a real drop path
    assert ss._extract_title_text("<a></a><a>Antras</a>") == ""


def test_markup_without_links_falls_back_to_its_first_text_line():
    assert ss._extract_title_text("<div>Programavimas\nII kursas</div>") == "Programavimas"


def test_the_first_line_fallback_is_stripped():
    assert ss._extract_title_text("<div>  Programavimas  \nII kursas</div>") == "Programavimas"


def test_a_break_tag_does_not_start_a_new_line_for_the_fallback():
    # get_text() joins strings without separators, so <br> runs
    # the two halves together instead of splitting them
    assert ss._extract_title_text("<div>Programavimas<br/>II kursas</div>") == "ProgramavimasII kursas"


def test_entities_inside_markup_are_decoded_by_the_parse():
    assert ss._extract_title_text("<div>Programavimas &amp; DB</div>") == "Programavimas & DB"


def test_markup_with_no_text_at_all_is_untitled():
    assert ss._extract_title_text('<span data-rooms="301"></span>') == ""


def test_a_bare_less_than_sign_sends_a_plain_title_down_the_markup_path():
    # The fork is the crude `"<" in title_field`, so a title with
    # a maths comparison is parsed — and survives it intact
    assert ss._extract_title_text("a < b") == "a < b"


def test_a_lone_less_than_sign_survives_the_parse():
    assert ss._extract_title_text("<") == "<"


def test_a_comment_only_title_is_untitled():
    assert ss._extract_title_text("<!-- nothing here -->") == ""


def test_a_script_or_style_body_is_not_text_and_leaves_the_event_untitled():
    # bs4 keeps script and stylesheet bodies out of get_text(),
    # so an injected one contributes nothing and the event is
    # dropped as untitled rather than titled with code
    assert ss._extract_title_text("<script>alert('Programavimas')</script>") == ""
    assert ss._extract_title_text("<style>a{content:'x'}</style>") == ""


def test_a_title_that_is_only_a_closing_tag_is_untitled():
    assert ss._extract_title_text("</a>") == ""


def test_a_non_string_title_is_refused_which_is_why_the_caller_guards():
    # scrape_group_schedule does `event.get("title") or ""`
    # precisely because this raises
    with pytest.raises(TypeError):
        ss._extract_title_text(None)


def test_the_popover_and_plain_paths_agree_on_the_same_course_name():
    plain = ss._extract_title_text("Programavimas")
    popover = ss._extract_title_text('<a href="/dalykas/1/">Programavimas</a>')

    assert plain == popover == "Programavimas"
