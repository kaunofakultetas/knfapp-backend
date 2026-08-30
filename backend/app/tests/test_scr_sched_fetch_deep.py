############################################################
#  [*] Tests — schedule_scraper, the three FETCHING functions
#
#  The exhaustive branch pass over exactly three functions of
#  app/scraper/schedule_scraper.py:
#
#    scrape_group_list      — /knf/list/ → [{slug, display_name}]
#    scrape_group_schedule  — one group's FullCalendar feed →
#                             (weekly lesson dicts, drop stats)
#    scrape_knf_schedule    — the whole import, lock to push
#
#  The broad suite already drives every LINE of these three;
#  this module goes after the arms it does not take. Chiefly:
#
#    - the display-name climb, sibling by sibling: a
#      non-heading sibling STEPPED OVER to reach the heading
#      behind it, a heading tag whose text is empty, the
#      nearest of two headings, a heading exactly five
#      ancestors up against six, a heading BELOW the link
#    - the "Nk" course suffix NOT appended, because the slug
#      carries no course token at all — the arm that falls
#      straight through to the append
#    - each drop filter's ORDER against the others: an
#      all-day retake counts as all-day and never reaches the
#      colour histogram, a retake with an unparsable end is a
#      retake, an untitled event with broken dates is
#      unparsable
#    - the instructor/location × popover matrix, the retake
#      colour × label matrix, and every boundary of the slug
#      regex (1, 80, 81 characters)
#    - the run's own arms: every group failing against every
#      group answering empty, MIN_SEMESTER_LESSONS at 4/5/1,
#      the anchor partition rewritten against a neighbour
#      only added to, the purge skipped when the anchor
#      harvested nothing, and each of the four ways the push
#      gate says no
#
#  Every fetch goes through `responses` — the container runs
#  --network none — and every clock through time_machine.
############################################################


import json
import logging
import sqlite3
import uuid
from html import escape
from urllib.parse import parse_qsl, urlsplit

import pytest
import responses
import time_machine

import app.notifications.push as push_module
from app.scraper import common as scraper_common
from app.scraper import schedule_scraper as ss


# A Tuesday inside the spring term: the anchor label is
# "2025-P" and the rolling window runs 2026-01-27 .. 2026-06-30
SPRING_RUN = "2026-02-10 09:00:00"
SPRING_ANCHOR = "2025-P"

# A Monday in July. The anchor is STILL "2025-P", but the
# events in the window's forward half already carry "2026-R" —
# a NEWER neighbour, so the purge must leave it alone
SUMMER_RUN = "2026-07-20 09:00:00"

# Mid-September, where the anchor has flipped to the autumn label
AUTUMN_RUN = "2026-09-15 09:00:00"
AUTUMN_ANCHOR = "2026-R"

# The one colour _RETAKE_COLOURS knows, and one it does not
RETAKE_COLOUR = "#ff899d"
UNKNOWN_COLOUR = "#3366cc"




############################################################
# http
############################################################
#
# The `responses` mock every fetching test runs inside.
# assert_all_requests_are_fired is off on purpose: several
# tests register a feed precisely to prove it is NEVER asked
# for (a refused slug, a spent time budget, a dropped group).
#
# Used by:
#   - every test below that reaches the network layer
############################################################

@pytest.fixture
def http():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps




############################################################
# _scraper_state
############################################################
#
# The two module globals a run mutates and would otherwise
# hand to the next test: schedule_scraper's one-run-at-a-time
# lock and common.py's per-process push clock.
#
# Used by:
#   - autouse, every test in this module
############################################################

@pytest.fixture(autouse=True)
def _scraper_state():
    scraper_common._LAST_PUSH.pop("tvarkarasciai.vu.lt", None)

    yield

    if ss._RUN_LOCK.locked():
        ss._RUN_LOCK.release()
    scraper_common._LAST_PUSH.pop("tvarkarasciai.vu.lt", None)




############################################################
# _page / _anchor / _block
############################################################
#
# The /knf/list/ markup. _block is the shape the real page
# has and every whole-run test wants: the heading is a
# previous sibling of the link's PARENT, which is where the
# climb starts looking. _anchor is the bare link, for the
# tests that shape the ancestor chain themselves.
#
# Used by:
#   - every scrape_group_list test and every whole-run test
############################################################

def _page(*fragments) -> str:
    return "<html><body><main>" + "".join(fragments) + "</main></body></html>"


def _anchor(slug, text="1 Grupė", title_attr=None) -> str:
    title = f' title="{escape(title_attr, quote=True)}"' if title_attr is not None else ""

    return f'<a href="/knf/groups/{slug}/"{title}>{text}</a>'


def _block(heading, slug, text="1 Grupė", title_attr=None, tag="h3") -> str:
    head = f"<{tag}>{heading}</{tag}>" if heading is not None else ""

    return f"<div>{head}<ul><li>{_anchor(slug, text, title_attr)}</li></ul></div>"




############################################################
# _serve_list / _serve_feed / _serve_one_group
############################################################
#
# The two URLs this scraper knows. The feed is registered
# without a query string so `responses` matches whatever
# window the run computed; the window itself is asserted off
# the recorded call instead.
#
# Used by:
#   - every test that fetches
############################################################

def _serve_list(http, body, status=200, content_type="text/html; charset=utf-8"):
    http.add(responses.GET, ss.GROUP_LIST_URL, body=body, status=status,
             content_type=content_type)


def _serve_feed(http, slug, events=None, body=None, status=200,
                content_type="application/json"):
    http.add(responses.GET, ss.EVENT_URL_TEMPLATE.format(slug=slug),
             body=body if body is not None else _feed(events or []),
             status=status, content_type=content_type)


def _serve_one_group(http, events, slug="isks-1k",
                     heading="Informacijos sistemos ir kibernetinė sauga - 1 kursas"):
    _serve_list(http, _page(_block(heading, slug)))
    _serve_feed(http, slug, events)




############################################################
# _event / _feed / _popover
############################################################
#
# One FullCalendar event, the {"events": [...]} envelope, and
# the site's popover title shape — the course as the first
# <a>, the lecturers and rooms in data-academics/data-rooms
# attributes holding HTML-ESCAPED HTML.
#
# Used by:
#   - every scrape_group_schedule and whole-run test
############################################################

def _event(start, end, title="Programavimas", **extra) -> dict:
    event = {"start": start, "end": end, "title": title}
    event.update(extra)

    return event


def _feed(events) -> str:
    return json.dumps({"events": events})


def _popover(title, academics=None, rooms=None) -> str:
    markup = f'<a href="/dalykas/1/">{title}</a><span'
    if academics is not None:
        markup += f' data-academics="{escape(academics, quote=True)}"'
    if rooms is not None:
        markup += f' data-rooms="{escape(rooms, quote=True)}"'

    return markup + "></span>"




############################################################
# _many_events
############################################################
#
# A batch of DISTINCT lessons — each a different quarter hour
# of the same Monday — so a test that needs exactly n new
# rows (the push burst threshold, the semester threshold)
# gets n and not n deduped into one.
#
# Used by:
#   - the threshold and push-gate tests
############################################################

def _many_events(count, day="2026-02-09", title="Dalykas"):
    events = []
    for index in range(count):
        hour = 6 + index // 4
        minute = (index % 4) * 15

        events.append(_event(f"{day}T{hour:02d}:{minute:02d}:00",
                             f"{day}T{hour:02d}:{minute + 10:02d}:00",
                             title=f"{title} {index}"))

    return events




############################################################
# _autumn_events
############################################################
#
# Distinct events in the NEXT academic year — the neighbour
# semester the rolling window clips, which is only ever added
# to and which a run has to see MIN_SEMESTER_LESSONS times
# before it is stored at all.
#
# Used by:
#   - the stray-label and neighbour-semester tests
############################################################

def _autumn_events(count):
    return [_event(f"2026-09-{14 + index:02d}T12:00:00",
                   f"2026-09-{14 + index:02d}T13:00:00",
                   title=f"Ruduo {14 + index}")
            for index in range(count)]




############################################################
# _seed_lesson / _seed_completed_run
############################################################
#
# State no scrape can arrange for itself: rows left over from
# a finished semester, and an EARLIER completed run — without
# one push_allowed calls the import a first backfill and
# suppresses every notification.
#
# Used by:
#   - the reconciliation, purge and push tests
############################################################

def _seed_lesson(db, semester, group_name="ISKS-1", title="Senas dalykas",
                 teacher="A. Petraitis", room="301", time_start="08:30",
                 time_end="10:00", day_of_week=0):
    db.execute(
        """INSERT INTO schedule_lessons
           (id, title, teacher, room, time_start, time_end, day_of_week, group_name, semester)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), title, teacher, room, time_start, time_end,
         day_of_week, group_name, semester),
    )
    db.commit()


def _seed_completed_run(db, found=8, new=8, started_at="2026-02-10T06:00:00+00:00"):
    db.execute(
        """INSERT INTO scraper_runs
           (id, source, status, articles_found, articles_new, started_at, finished_at)
           VALUES (?, 'tvarkarasciai.vu.lt', 'completed', ?, ?, ?, ?)""",
        (str(uuid.uuid4()), found, new, started_at, started_at),
    )
    db.commit()




############################################################
# _lesson_rows / _run_rows / _params_of
############################################################
#
# Readers: the whole timetable as comparable tuples, the run
# rows this source wrote, and one recorded request's query
# string as a dict.
#
# Used by:
#   - the whole-run tests
############################################################

def _lesson_rows(db, semester=None):
    sql = ("SELECT title, teacher, room, time_start, time_end, day_of_week, group_name, semester"
           " FROM schedule_lessons")
    params = []
    if semester:
        sql += " WHERE semester = ?"
        params.append(semester)
    sql += " ORDER BY semester, group_name, day_of_week, time_start, title"

    return [tuple(row) for row in db.execute(sql, params).fetchall()]


def _run_rows(db):
    return db.execute(
        "SELECT status, articles_found, articles_new, error_message, finished_at"
        " FROM scraper_runs WHERE source = 'tvarkarasciai.vu.lt' ORDER BY rowid"
    ).fetchall()


def _params_of(call):
    return dict(parse_qsl(urlsplit(call.request.url).query))




# ==========================================================
# scrape_group_list — the request and its two outcomes
# ==========================================================


def test_the_group_list_is_fetched_from_the_faculty_list_url_without_parameters(http):
    _serve_list(http, _page())

    ss.scrape_group_list()

    assert len(http.calls) == 1
    assert http.calls[0].request.url == ss.GROUP_LIST_URL
    assert _params_of(http.calls[0]) == {}


def test_a_group_list_page_that_downloads_but_holds_no_link_is_an_empty_list(http):
    _serve_list(http, _page())

    assert ss.scrape_group_list() == []


def test_a_group_list_page_of_nothing_but_foreign_links_is_an_empty_list(http):
    _serve_list(http, _page(
        '<a href="/knf/list/">Atgal</a>',
        '<a href="https://vu.lt/">VU</a>',
        '<a href="mailto:info@knf.vu.lt">Rašykite</a>',
    ))

    assert ss.scrape_group_list() == []


@pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
def test_every_failing_status_on_the_list_page_raises(http, status):
    _serve_list(http, "", status=status)

    with pytest.raises(RuntimeError, match="Could not fetch the group list"):
        ss.scrape_group_list()


def test_the_raised_group_list_error_names_the_url_it_could_not_reach(http):
    _serve_list(http, "", status=500)

    with pytest.raises(RuntimeError) as excinfo:
        ss.scrape_group_list()

    assert ss.GROUP_LIST_URL in str(excinfo.value)


def test_a_group_list_served_as_the_wrong_content_type_raises(http):
    # fetch() refuses to parse a PDF as a page, so the caller
    # sees the same "could not fetch" it sees for a 500
    _serve_list(http, _page(_anchor("ev-2k")), content_type="application/pdf")

    with pytest.raises(RuntimeError, match="Could not fetch the group list"):
        ss.scrape_group_list()


def test_a_group_list_redirected_off_the_allowlist_raises(http):
    http.add(responses.GET, ss.GROUP_LIST_URL, status=302,
             headers={"Location": "https://vu.lt/knf/list/"})
    http.add(responses.GET, "https://vu.lt/knf/list/", body=_page(_anchor("ev-2k")),
             status=200, content_type="text/html")

    with pytest.raises(RuntimeError, match="Could not fetch the group list"):
        ss.scrape_group_list()


def test_every_group_entry_is_a_dict_of_exactly_slug_and_display_name(http):
    _serve_list(http, _page(_block("Ekonomika ir vadyba - 2 kursas", "ev-2k")))

    groups = ss.scrape_group_list()

    assert len(groups) == 1
    assert set(groups[0]) == {"slug", "display_name"}


def test_the_groups_come_back_in_the_order_the_page_lists_them(http):
    _serve_list(http, _page(*[
        _block(f"Programa {index} - 1 kursas", f"grupe-{index}") for index in range(6)
    ]))

    assert [group["slug"] for group in ss.scrape_group_list()] == [
        f"grupe-{index}" for index in range(6)
    ]


@pytest.mark.slow
def test_a_list_page_of_two_hundred_groups_yields_all_two_hundred(http):
    _serve_list(http, _page(*[
        _block(f"Programa {index} - 1 kursas", f"grupe-{index}") for index in range(200)
    ]))

    assert len(ss.scrape_group_list()) == 200




# ==========================================================
# scrape_group_list — which hrefs count as a group page
# ==========================================================


@pytest.mark.parametrize("href", [
    "/knf/groups/ev-2k",                # no trailing slash
    "/knf/groups/",                     # no slug at all
    "/knf/groups//",                    # empty slug segment
    "knf/groups/ev-2k/",                # relative, not anchored
    "/knf/groups/ev-2k/tvarkarastis/",  # a page below the group
    "/knf/groups/ev-2k/?savaite=3",     # a query after the slash
    "/KNF/groups/ev-2k/",               # the path is case sensitive
    "/knf/group/ev-2k/",                # singular
    "https://tvarkarasciai.vu.lt/knf/groups/ev-2k/",  # absolute
    "#",
    "",
])
def test_an_href_that_is_not_a_bare_group_page_is_ignored(http, href):
    _serve_list(http, _page(f'<a href="{href}">Grupė</a>'))

    assert ss.scrape_group_list() == []


def test_a_group_href_is_recognised_wherever_it_sits_on_the_page(http):
    _serve_list(http, _page(
        '<a href="/knf/list/">Atgal</a>',
        _block("Ekonomika ir vadyba - 2 kursas", "ev-2k"),
        '<a href="/knf/groups/ev-2k/rooms/">Patalpos</a>',
    ))

    assert [group["slug"] for group in ss.scrape_group_list()] == ["ev-2k"]


def test_a_repeated_slug_keeps_the_first_links_display_name(http):
    _serve_list(http, _page(
        _block("Pirmoji antraštė - 1 kursas", "ev-1k"),
        _block("Antroji antraštė - 2 kursas", "ev-1k"),
    ))

    assert ss.scrape_group_list() == [
        {"slug": "ev-1k", "display_name": "Pirmoji antraštė - 1 kursas"},
    ]


def test_three_links_to_the_same_group_still_produce_one_entry(http):
    _serve_list(http, _page(
        "<div><h3>Ekonomika ir vadyba - 2 kursas</h3><ul><li>"
        f"{_anchor('ev-2k')}{_anchor('ev-2k', text='Tvarkaraštis')}"
        f"{_anchor('ev-2k', text='PDF')}</li></ul></div>"))

    assert len(ss.scrape_group_list()) == 1




# ==========================================================
# scrape_group_list — slug hygiene, on the boundary
# ==========================================================


@pytest.mark.parametrize("slug", [
    "blogas.slug",
    "grupė",
    "ev 2k",
    "ev+2k",
    "..",
    "ev%2f2k",
    "ev:2k",
    "ev@2k",
    "ev#2k",
    "a" * 81,
])
def test_a_slug_that_is_not_a_plain_path_token_is_dropped_with_a_warning(http, caplog, slug):
    _serve_list(http, _page(f'<a href="/knf/groups/{slug}/">Grupė</a>'))

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        groups = ss.scrape_group_list()

    assert groups == []
    assert "malformed group slug" in caplog.text


@pytest.mark.parametrize("slug", ["a", "A", "1", "_", "-", "a" * 80, "ISKS_1k-2gr", "ev-2k"])
def test_every_slug_the_regex_allows_is_kept(http, slug):
    _serve_list(http, _page(f'<a href="/knf/groups/{slug}/">Grupė</a>'))

    assert [group["slug"] for group in ss.scrape_group_list()] == [slug]


def test_the_slug_length_boundary_falls_between_eighty_and_eighty_one(http):
    _serve_list(http, _page(
        f'<a href="/knf/groups/{"a" * 80}/">Ilga</a>',
        f'<a href="/knf/groups/{"b" * 81}/">Per ilga</a>',
    ))

    assert [group["slug"] for group in ss.scrape_group_list()] == ["a" * 80]


def test_a_dropped_slug_never_stops_the_valid_groups_around_it(http):
    _serve_list(http, _page(
        _block("Pirma - 1 kursas", "ev-1k"),
        '<a href="/knf/groups/blogas.slug/">Bloga</a>',
        _block("Antra - 2 kursas", "ev-2k"),
    ))

    assert [group["slug"] for group in ss.scrape_group_list()] == ["ev-1k", "ev-2k"]




# ==========================================================
# scrape_group_list — reconstructing the display name
# ==========================================================


@pytest.mark.parametrize("tag", ["h2", "h3", "h4", "strong", "b"])
def test_each_accepted_tag_above_the_link_becomes_the_display_name(http, tag):
    _serve_list(http, _page(_block("Meno vadyba - 1 kursas", "mv-1k", tag=tag)))

    assert ss.scrape_group_list() == [
        {"slug": "mv-1k", "display_name": "Meno vadyba - 1 kursas"},
    ]


@pytest.mark.parametrize("tag", ["h1", "h5", "h6", "p", "div", "em", "span"])
def test_a_tag_outside_the_accepted_set_is_not_a_display_name(http, tag):
    _serve_list(http, _page(_block("Meno vadyba - 1 kursas", "mv-1k", tag=tag)))

    # Nothing above the link qualifies, so the de-hyphenated
    # slug is the name
    assert ss.scrape_group_list() == [{"slug": "mv-1k", "display_name": "mv 1k"}]


def test_a_non_heading_sibling_is_stepped_over_to_reach_the_heading_behind_it(http):
    # The inner walk has to KEEP GOING past the <hr> and the
    # <p>; stopping at the first sibling loses every heading on
    # a page that puts anything under it
    _serve_list(http, _page(
        "<div><h3>Ekonomika ir vadyba - 2 kursas</h3><p>Aprašymas</p><hr/>"
        f"<ul><li>{_anchor('ev-2k')}</li></ul></div>"))

    assert ss.scrape_group_list() == [
        {"slug": "ev-2k", "display_name": "Ekonomika ir vadyba - 2 kursas"},
    ]


def test_whitespace_between_the_tags_is_not_mistaken_for_a_heading(http):
    # NavigableStrings have a name of None and have to fall
    # through the tag-name test rather than end the walk
    _serve_list(http, _page(
        "<div>\n  <h3>Ekonomika ir vadyba - 2 kursas</h3>\n  <p>Tekstas</p>\n  "
        f"<ul>\n <li>{_anchor('ev-2k')}</li>\n </ul>\n</div>"))

    assert ss.scrape_group_list() == [
        {"slug": "ev-2k", "display_name": "Ekonomika ir vadyba - 2 kursas"},
    ]


def test_an_html_comment_between_the_tags_is_not_mistaken_for_a_heading(http):
    _serve_list(http, _page(
        "<div><h3>Ekonomika ir vadyba - 2 kursas</h3><!-- senas blokas -->"
        f"<ul><li>{_anchor('ev-2k')}</li></ul></div>"))

    assert ss.scrape_group_list()[0]["display_name"] == "Ekonomika ir vadyba - 2 kursas"


def test_the_nearest_of_two_headings_wins(http):
    _serve_list(http, _page(
        "<div><h3>Tolimesnė - 9 kursas</h3><h3>Artimiausia - 2 kursas</h3>"
        f"<ul><li>{_anchor('ev-2k')}</li></ul></div>"))

    assert ss.scrape_group_list()[0]["display_name"] == "Artimiausia - 2 kursas"


def test_a_heading_that_follows_the_link_is_never_used(http):
    _serve_list(http, _page(
        f"<div><ul><li>{_anchor('mv-1k')}</li></ul><h3>Po nuorodos - 1 kursas</h3></div>"))

    assert ss.scrape_group_list() == [{"slug": "mv-1k", "display_name": "mv 1k"}]


def test_an_empty_heading_is_stepped_past_and_the_next_ancestors_wins(http):
    # A heading tag with no text ends the inner walk with an
    # EMPTY name — the climb has to carry on to the next
    # ancestor instead of settling for it
    _serve_list(http, _page(
        "<div><h3>Meno vadyba - 1 kursas</h3><div><h3></h3>"
        f"<ul><li>{_anchor('mv-1k')}</li></ul></div></div>"))

    assert ss.scrape_group_list()[0]["display_name"] == "Meno vadyba - 1 kursas"


def test_a_heading_is_read_with_its_surrounding_whitespace_stripped(http):
    _serve_list(http, _page(
        f"<div><h3>\n  Meno vadyba - 1 kursas\n</h3><ul><li>{_anchor('mv-1k')}</li></ul></div>"))

    assert ss.scrape_group_list()[0]["display_name"] == "Meno vadyba - 1 kursas"


def test_a_heading_five_ancestors_above_the_link_is_still_found(http):
    # The climb visits li, ul and three divs — the heading sits
    # beside the fifth and last of them
    _serve_list(http, _page(
        "<div><h3>Meno vadyba - 1 kursas</h3>"
        f"<div><div><div><ul><li>{_anchor('mv-1k')}</li></ul></div></div></div></div>"))

    assert ss.scrape_group_list()[0]["display_name"] == "Meno vadyba - 1 kursas"


def test_a_heading_six_ancestors_above_the_link_is_out_of_reach(http):
    _serve_list(http, _page(
        "<div><h3>Meno vadyba - 1 kursas</h3>"
        f"<div><div><div><div><ul><li>{_anchor('mv-1k')}</li></ul></div></div></div></div></div>"))

    assert ss.scrape_group_list()[0]["display_name"] == "mv 1k"


def test_a_link_at_the_top_of_the_document_runs_out_of_ancestors(http):
    # The climb hits a None parent long before its five turns
    # are up, and has to stop rather than raise
    _serve_list(http, _anchor("finansu-analitika-1k", text="Grupė"))

    assert ss.scrape_group_list() == [
        {"slug": "finansu-analitika-1k", "display_name": "finansu analitika 1k"},
    ]




# ==========================================================
# scrape_group_list — the title attribute and the slug
# ==========================================================


def test_the_title_attribute_is_used_when_no_heading_is_above_the_link(http):
    _serve_list(http, _page(_anchor("tvv-1k", title_attr="Tarptautinio verslo vadyba - 1 kursas")))

    assert ss.scrape_group_list() == [
        {"slug": "tvv-1k", "display_name": "Tarptautinio verslo vadyba - 1 kursas"},
    ]


def test_a_heading_beats_the_links_own_title_attribute(http):
    _serve_list(http, _page(
        _block("Iš antraštės - 1 kursas", "tvv-1k", title_attr="Iš atributo - 2 kursas")))

    assert ss.scrape_group_list()[0]["display_name"] == "Iš antraštės - 1 kursas"


def test_an_empty_title_attribute_falls_through_to_the_slug(http):
    _serve_list(http, _page(_anchor("tvv-1k", title_attr="")))

    assert ss.scrape_group_list() == [{"slug": "tvv-1k", "display_name": "tvv 1k"}]


def test_the_link_text_is_never_the_display_name(http):
    # The real page's link text is "1 Grupė" / "Tvarkaraštis",
    # which is why the name is reconstructed at all
    _serve_list(http, _page(_anchor("mv-1k", text="Tvarkaraštis")))

    assert ss.scrape_group_list()[0]["display_name"] == "mv 1k"


def test_the_slug_fallback_de_hyphenates_the_slug(http):
    _serve_list(http, _page(_anchor("lietuviu-filologija-ir-reklama-2k")))

    assert ss.scrape_group_list()[0]["display_name"] == "lietuviu filologija ir reklama 2k"




# ==========================================================
# scrape_group_list — appending the course from the slug
# ==========================================================


def test_the_course_is_appended_to_a_heading_that_does_not_carry_one(http):
    _serve_list(http, _page(_block("Ekonomika ir vadyba", "ev-3k-1gr")))

    assert ss.scrape_group_list()[0]["display_name"] == "Ekonomika ir vadyba - 3 kursas"


def test_the_course_suffix_is_skipped_when_the_slug_carries_no_course_token(http):
    # The heading has no "kursas" AND the slug has no "Nk", so
    # the name is used exactly as the page wrote it
    _serve_list(http, _page(_block("Bendrųjų universitetinių studijų dalykai", "bus-dalykai")))

    assert ss.scrape_group_list() == [
        {"slug": "bus-dalykai", "display_name": "Bendrųjų universitetinių studijų dalykai"},
    ]


def test_a_heading_that_already_says_kursas_is_left_alone(http):
    _serve_list(http, _page(_block("Ekonomika ir vadyba - 2 kursas", "ev-3k-1gr")))

    # The slug says 3 and the heading says 2 — the heading wins
    # and nothing is appended twice
    assert ss.scrape_group_list()[0]["display_name"] == "Ekonomika ir vadyba - 2 kursas"


def test_the_kursas_test_ignores_the_headings_case(http):
    _serve_list(http, _page(_block("EKONOMIKA IR VADYBA - 2 KURSAS", "ev-3k")))

    assert ss.scrape_group_list()[0]["display_name"] == "EKONOMIKA IR VADYBA - 2 KURSAS"


def test_the_first_course_token_in_the_slug_wins(http):
    _serve_list(http, _page(_block("Ekonomika ir vadyba", "ev-2k-4k")))

    assert ss.scrape_group_list()[0]["display_name"] == "Ekonomika ir vadyba - 2 kursas"


def test_an_uppercase_course_token_is_not_a_course_token(http):
    # The search is for a lowercase "k"; an uppercase one is
    # left alone rather than guessed at
    _serve_list(http, _page(_block("Ekonomika ir vadyba", "ev-2K")))

    assert ss.scrape_group_list()[0]["display_name"] == "Ekonomika ir vadyba"


def test_a_slug_derived_name_never_gets_a_course_suffix_appended(http):
    # The suffix block only runs for a name that was actually
    # found; the slug fallback is left exactly as the slug
    _serve_list(http, _page(_anchor("ev-3k-1gr")))

    assert ss.scrape_group_list()[0]["display_name"] == "ev 3k 1gr"


def test_a_title_attribute_name_does_get_the_course_appended(http):
    _serve_list(http, _page(_anchor("ev-3k", title_attr="Ekonomika ir vadyba")))

    assert ss.scrape_group_list()[0]["display_name"] == "Ekonomika ir vadyba - 3 kursas"




# ==========================================================
# scrape_group_schedule — the request it makes
# ==========================================================


def test_the_feed_url_carries_the_slug_and_the_window_as_query_parameters(http):
    _serve_feed(http, "isks-1k", [])

    ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert len(http.calls) == 1
    assert http.calls[0].request.url.startswith(ss.EVENT_URL_TEMPLATE.format(slug="isks-1k"))
    assert _params_of(http.calls[0]) == {"start": "2026-01-27", "end": "2026-06-30"}


def test_a_slug_of_only_regex_safe_characters_is_interpolated_verbatim(http):
    # _SLUG_RE lets nothing through that quote() would encode,
    # so the belt-and-braces encoding is a no-op
    _serve_feed(http, "ISKS_1k-2gr", [])

    ss.scrape_group_schedule("ISKS_1k-2gr", "Grupė", "2026-01-27", "2026-06-30")

    assert "/ISKS_1k-2gr/group/255/" in http.calls[0].request.url


@pytest.mark.parametrize("slug", [None, "", "blogas.slug", "ev 2k", "../../etc",
                                  "ev/2k", "a" * 81, "grupė", "ev%2f2k"])
def test_a_malformed_slug_is_refused_before_a_single_request(http, slug):
    _serve_feed(http, "isks-1k", [])

    with pytest.raises(ValueError, match="Refusing the malformed group slug"):
        ss.scrape_group_schedule(slug, "Grupė", "2026-01-27", "2026-06-30")

    assert len(http.calls) == 0


def test_the_refusal_names_the_slug_it_refused(http):
    with pytest.raises(ValueError) as excinfo:
        ss.scrape_group_schedule("blogas.slug", "Grupė", "2026-01-27", "2026-06-30")

    assert "'blogas.slug'" in str(excinfo.value)


@pytest.mark.parametrize("slug", ["a", "a" * 80])
def test_the_slug_length_boundary_is_the_same_one_the_list_enforces(http, slug):
    _serve_feed(http, slug, [])

    lessons, stats = ss.scrape_group_schedule(slug, "Grupė", "2026-01-27", "2026-06-30")

    assert (lessons, stats["events"]) == ([], 0)


@pytest.mark.parametrize("status", [403, 404, 500, 503])
def test_a_failing_feed_status_raises_and_names_the_group(http, status):
    _serve_feed(http, "isks-1k", body="", status=status)

    with pytest.raises(RuntimeError, match="Could not fetch the event feed for isks-1k"):
        ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")


def test_a_feed_served_as_html_is_still_parsed_as_json(http):
    _serve_feed(http, "isks-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")],
                content_type="text/html; charset=utf-8")

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert len(lessons) == 1


def test_a_feed_served_as_an_unaccepted_content_type_raises(http):
    _serve_feed(http, "isks-1k", [], content_type="application/pdf")

    with pytest.raises(RuntimeError, match="Could not fetch the event feed"):
        ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")


@pytest.mark.parametrize("body", ["", "ne json", "<html></html>", "{", "null,"])
def test_a_body_that_is_not_json_raises(http, body):
    _serve_feed(http, "isks-1k", body=body)

    with pytest.raises(json.JSONDecodeError):
        ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")


def test_a_feed_without_an_events_key_yields_nothing(http):
    _serve_feed(http, "isks-1k", body=json.dumps({"kitas": 1}))

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats == {"events": 0, "all_day": 0, "retakes": 0,
                     "unparsable": 0, "untitled": 0, "colours": {}}


def test_an_empty_event_list_yields_an_all_zero_stat_block(http):
    _serve_feed(http, "isks-1k", [])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats == {"events": 0, "all_day": 0, "retakes": 0,
                     "unparsable": 0, "untitled": 0, "colours": {}}


@pytest.mark.parametrize("body,expected", [
    (json.dumps({"events": None}), TypeError),          # len(None)
    (json.dumps([]), AttributeError),                   # a bare list has no .get
    (json.dumps({"events": ["ne objektas"]}), AttributeError),
    (json.dumps({"events": [None]}), AttributeError),
])
def test_a_structurally_wrong_feed_raises_so_the_caller_skips_the_group(http, body, expected):
    _serve_feed(http, "isks-1k", body=body)

    with pytest.raises(expected):
        ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")




# ==========================================================
# scrape_group_schedule — the four drop filters and their order
# ==========================================================


@pytest.mark.parametrize("start", [None, "", "2026-02-09", "visa diena", 0])
def test_an_event_without_a_time_component_is_dropped_as_all_day(http, start):
    _serve_feed(http, "isks-1k", [{"start": start, "end": "2026-02-09T10:00:00",
                                   "title": "Šventė"}])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats["all_day"] == 1


def test_an_event_with_no_start_key_at_all_is_dropped_as_all_day(http):
    _serve_feed(http, "isks-1k", [{"end": "2026-02-09T10:00:00", "title": "Šventė"}])

    _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert stats["all_day"] == 1


def test_each_all_day_event_is_counted_separately(http):
    _serve_feed(http, "isks-1k", [
        {"start": "2026-02-09", "title": "Šventė"},
        {"start": None, "title": "Šventė"},
        {"start": "", "title": "Šventė"},
    ])

    _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert (stats["all_day"], stats["events"]) == (3, 3)


def test_an_all_day_event_never_reaches_the_colour_histogram(http):
    # The all-day test runs BEFORE the colour is read, so a
    # holiday cannot pad the palette diagnostics
    _serve_feed(http, "isks-1k", [
        {"start": "2026-02-09", "title": "Šventė", "color": RETAKE_COLOUR},
    ])

    _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert stats["colours"] == {}
    assert (stats["all_day"], stats["retakes"]) == (1, 0)


def test_a_retake_colour_alone_drops_the_event_without_a_warning(http, caplog):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", color=RETAKE_COLOUR),
    ])

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė",
                                                  "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats["retakes"] == 1
    assert "palette" not in caplog.text


@pytest.mark.parametrize("colour", ["#FF899D", "#ff899d", "rgb(255, 137, 157)",
                                    "rgba(255,137,157,0.5)", "  #FF899D  "])
def test_every_notation_of_the_retake_colour_drops_the_event(http, colour):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", color=colour),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert lessons == []
    assert (stats["retakes"], stats["colours"]) == (1, {RETAKE_COLOUR: 1})


def test_a_labelled_retake_in_an_unknown_colour_warns_that_the_palette_moved(http, caplog):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               color=UNKNOWN_COLOUR, subtitle="PERLAIKYMAS"),
    ])

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė",
                                                  "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats["retakes"] == 1
    assert "palette has probably changed" in caplog.text


def test_a_labelled_retake_in_the_known_colour_warns_about_nothing(http, caplog):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               color=RETAKE_COLOUR, subtitle="PERLAIKYMAS"),
    ])

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert stats["retakes"] == 1
    assert "palette" not in caplog.text


def test_a_labelled_retake_with_no_colour_at_all_warns_about_nothing(http, caplog):
    # There is no palette to have changed, so the warning would
    # be noise on every uncoloured exam
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", description="PERLAIKYMAS"),
    ])

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert (stats["retakes"], stats["colours"]) == (1, {})
    assert "palette" not in caplog.text


@pytest.mark.parametrize("field", ["subtitle", "description", "type", "category", "eventType"])
def test_the_perlaikymas_label_drops_the_event_from_any_of_its_fields(http, field):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", **{field: "Egzaminas (perlaikymas)"}),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats["retakes"] == 1


def test_a_structured_retake_flag_drops_the_event(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", retake=True),
    ])

    _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert stats["retakes"] == 1


@pytest.mark.parametrize("flag", [False, None, 1, "true", "taip"])
def test_anything_but_a_true_retake_flag_leaves_the_lesson_alone(http, flag):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", retake=flag),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert len(lessons) == 1
    assert stats["retakes"] == 0


def test_a_retake_is_dropped_before_its_dates_are_even_parsed(http):
    # Order matters: a broken exam is a retake, not an
    # "unparsable" event, or the two counters trade places
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", None, color=RETAKE_COLOUR),
    ])

    _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert (stats["retakes"], stats["unparsable"]) == (1, 0)


@pytest.mark.parametrize("start,end", [
    ("2026-02-09T08:30:00", None),                     # TypeError
    ("2026-02-09T08:30:00", ""),                       # ValueError
    ("2026-02-09T08:30:00", "rytoj"),
    ("2026-02-09T08:30:00", 12345),
    ("2026-02-30T08:30:00", "2026-02-30T10:00:00"),    # no such day
    ("2026-02-09T25:30:00", "2026-02-09T26:00:00"),    # no such hour
    ("2026-02-09Txx", "2026-02-09T10:00:00"),
])
def test_an_event_whose_dates_do_not_parse_is_dropped_and_counted(http, start, end):
    _serve_feed(http, "isks-1k", [_event(start, end)])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats["unparsable"] == 1


def test_an_event_with_no_end_key_is_dropped_as_unparsable(http):
    _serve_feed(http, "isks-1k", [{"start": "2026-02-09T08:30:00", "title": "Programavimas"}])

    _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert stats["unparsable"] == 1


@pytest.mark.parametrize("title", [None, "", "   ", "\n\t", "<span></span>", "<div><p></p></div>"])
def test_an_event_that_yields_no_title_text_is_dropped_and_counted(http, title):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title=title),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert lessons == []
    assert stats["untitled"] == 1


def test_an_event_with_no_title_key_at_all_is_dropped_as_untitled(http):
    _serve_feed(http, "isks-1k", [{"start": "2026-02-09T08:30:00",
                                   "end": "2026-02-09T10:00:00"}])

    _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert stats["untitled"] == 1


def test_an_untitled_event_with_broken_dates_counts_only_as_unparsable(http):
    # The date parse comes first, so the two counters cannot
    # both claim the same event
    _serve_feed(http, "isks-1k", [_event("2026-02-09T08:30:00", None, title="")])

    _, stats = ss.scrape_group_schedule("isks-1k", "Grupė", "2026-01-27", "2026-06-30")

    assert (stats["unparsable"], stats["untitled"]) == (1, 0)


def test_one_malformed_event_never_costs_the_group_its_other_lessons(http):
    _serve_feed(http, "isks-1k", [
        {"start": None, "end": None, "title": None},
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00"),
        _event("2026-02-10T10:15:00", None, title="Sugedęs"),
        _event("2026-02-11T12:00:00", "2026-02-11T13:30:00", title="Duomenų bazės"),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert [lesson["title"] for lesson in lessons] == ["Programavimas", "Duomenų bazės"]
    assert stats == {"events": 4, "all_day": 1, "retakes": 0,
                     "unparsable": 1, "untitled": 0, "colours": {}}


def test_the_event_count_is_the_raw_feed_length_before_any_filter(http):
    _serve_feed(http, "isks-1k", [
        {"start": "2026-02-09", "title": "Šventė"},
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", color=RETAKE_COLOUR),
        _event("2026-02-10T08:30:00", None),
        _event("2026-02-11T08:30:00", "2026-02-11T10:00:00", title=""),
        _event("2026-02-12T08:30:00", "2026-02-12T10:00:00"),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert stats["events"] == 5
    assert len(lessons) == 1




# ==========================================================
# scrape_group_schedule — the colour histogram
# ==========================================================


def test_the_colour_of_an_ordinary_lesson_is_counted_too(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", color="#123ABC"),
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert len(lessons) == 1
    assert stats["colours"] == {"#123abc": 1}


def test_different_notations_of_one_colour_land_in_one_bucket(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:00:00", "2026-02-09T09:00:00", title="A", color="#ABC"),
        _event("2026-02-09T09:00:00", "2026-02-09T10:00:00", title="B", color="#AABBCC"),
        _event("2026-02-09T10:00:00", "2026-02-09T11:00:00", title="C",
               color="rgb(170, 187, 204)"),
    ])

    _, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                        "2026-01-27", "2026-06-30")

    assert stats["colours"] == {"#aabbcc": 3}


@pytest.mark.parametrize("colour", [None, "", "   ", 0])
def test_a_missing_or_blank_colour_is_not_in_the_histogram(http, colour):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", color=colour),
    ])

    _, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                        "2026-01-27", "2026-06-30")

    assert stats["colours"] == {}


def test_a_named_colour_is_kept_in_the_histogram_lowercased(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", color="ReBeCcAPuRpLe"),
    ])

    _, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                        "2026-01-27", "2026-06-30")

    assert stats["colours"] == {"rebeccapurple": 1}


def test_several_colours_each_keep_their_own_count(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:00:00", "2026-02-09T09:00:00", title="A", color="#111111"),
        _event("2026-02-09T09:00:00", "2026-02-09T10:00:00", title="B", color="#111111"),
        _event("2026-02-09T10:00:00", "2026-02-09T11:00:00", title="C", color="#222222"),
    ])

    _, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                        "2026-01-27", "2026-06-30")

    assert stats["colours"] == {"#111111": 2, "#222222": 1}




# ==========================================================
# scrape_group_schedule — the lesson a kept event becomes
# ==========================================================


def test_a_kept_event_carries_exactly_the_eight_lesson_fields(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               instructor="A. Petraitis", location="301"),
    ])

    lessons, _ = ss.scrape_group_schedule(
        "isks-1k", "Informacijos sistemos ir kibernetinė sauga - 1 kursas",
        "2026-01-27", "2026-06-30")

    assert lessons == [{
        "title": "Programavimas", "teacher": "A. Petraitis", "room": "301",
        "time_start": "08:30", "time_end": "10:00", "day_of_week": 0,
        "group_name": "ISKS-1", "semester": "2025-P",
    }]


@pytest.mark.parametrize("date,weekday", [
    ("2026-02-09", 0), ("2026-02-10", 1), ("2026-02-11", 2), ("2026-02-12", 3),
    ("2026-02-13", 4), ("2026-02-14", 5), ("2026-02-15", 6),
])
def test_every_weekday_maps_to_the_python_convention_the_api_uses(http, date, weekday):
    _serve_feed(http, "isks-1k", [_event(f"{date}T08:30:00", f"{date}T10:00:00")])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert lessons[0]["day_of_week"] == weekday


@pytest.mark.parametrize("start,expected", [
    ("2026-02-09T00:00:00", "00:00"),
    ("2026-02-09T08:05:00", "08:05"),
    ("2026-02-09T23:59:59", "23:59"),
    ("2026-02-09T08:30:00.123456", "08:30"),
])
def test_the_times_are_zero_padded_to_hours_and_minutes(http, start, expected):
    _serve_feed(http, "isks-1k", [_event(start, "2026-02-09T23:59:59")])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert lessons[0]["time_start"] == expected


def test_an_offset_on_the_timestamp_does_not_shift_the_sites_wall_clock(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00+02:00", "2026-02-09T10:00:00+02:00"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["time_start"], lessons[0]["time_end"]) == ("08:30", "10:00")


def test_a_lesson_that_ends_after_midnight_keeps_the_weekday_it_started_on(http):
    _serve_feed(http, "isks-1k", [_event("2026-02-09T22:00:00", "2026-02-10T00:30:00")])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["day_of_week"], lessons[0]["time_end"]) == (0, "00:30")


def test_an_end_before_its_start_is_still_recorded_as_the_feed_wrote_it(http):
    _serve_feed(http, "isks-1k", [_event("2026-02-09T10:00:00", "2026-02-09T08:30:00")])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["time_start"], lessons[0]["time_end"]) == ("10:00", "08:30")


def test_each_event_is_labelled_by_its_own_date_not_the_runs(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title="Pavasaris"),
        _event("2026-09-14T08:30:00", "2026-09-14T10:00:00", title="Ruduo"),
        _event("2025-12-08T08:30:00", "2025-12-08T10:00:00", title="Praeitas"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert {lesson["title"]: lesson["semester"] for lesson in lessons} == {
        "Pavasaris": "2025-P", "Ruduo": "2026-R", "Praeitas": "2025-R",
    }


def test_the_group_name_is_the_same_on_every_lesson_of_one_feed(http):
    _serve_feed(http, "isks-1k-2gr", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title="A"),
        _event("2026-02-10T08:30:00", "2026-02-10T10:00:00", title="B"),
    ])

    lessons, _ = ss.scrape_group_schedule(
        "isks-1k-2gr", "Informacijos sistemos ir kibernetinė sauga - 1 kursas 2 grupė",
        "2026-01-27", "2026-06-30")

    assert {lesson["group_name"] for lesson in lessons} == {"ISKS-1"}


def test_an_unparsable_display_name_falls_back_to_the_slug_as_the_group_name(http):
    _serve_feed(http, "nezinoma-grupe", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    lessons, _ = ss.scrape_group_schedule("nezinoma-grupe", "Nežinoma programa",
                                          "2026-01-27", "2026-06-30")

    assert lessons[0]["group_name"] == "nezinoma-grupe"




# ==========================================================
# scrape_group_schedule — lecturer and room, all four ways
# ==========================================================


def test_the_top_level_instructor_and_location_win_over_the_popover(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               title=_popover("Programavimas", academics="<a>Iš popover</a>", rooms="<a>999</a>"),
               instructor="A. Petraitis", location="301"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["teacher"], lessons[0]["room"]) == ("A. Petraitis", "301")


def test_the_popover_fills_in_only_what_the_top_level_fields_leave_empty(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               title=_popover("Programavimas",
                              academics='<a href="/d/1">B. Jonaitis</a>',
                              rooms='<a href="/p/1">204</a>'),
               instructor="", location=None),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["teacher"], lessons[0]["room"]) == ("B. Jonaitis", "204")


def test_a_plain_title_is_never_searched_for_popover_markup(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title="Programavimas"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["teacher"], lessons[0]["room"]) == ("", "")


def test_a_popover_with_neither_attribute_leaves_both_columns_empty(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title=_popover("Programavimas")),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["teacher"], lessons[0]["room"]) == ("", "")


@pytest.mark.parametrize("value", [None, "", 0, False])
def test_a_falsy_instructor_or_location_becomes_an_empty_string_never_none(http, value):
    # Both columns are part of the natural key, and SQLite
    # counts NULLs in a unique index as distinct
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
               instructor=value, location=value),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert (lessons[0]["teacher"], lessons[0]["room"]) == ("", "")


@pytest.mark.parametrize("raw,expected", [
    ("  A. Petraitis  ", "A. Petraitis"),
    ("A. Petraitis,", "A. Petraitis"),
    ("A. Petraitis, ", "A. Petraitis"),
    ("A. Petraitis,,", "A. Petraitis"),
    (" , ", ""),
    ("prof. dr. A. Petraitis", "prof. dr. A. Petraitis"),
])
def test_the_lecturer_loses_only_its_edges_and_a_trailing_comma(http, raw, expected):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", instructor=raw),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert lessons[0]["teacher"] == expected


def test_the_room_is_never_trimmed_the_way_the_lecturer_is(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="  301, "),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert lessons[0]["room"] == "  301, "




# ==========================================================
# scrape_group_schedule — the weekly dedupe
# ==========================================================


def test_the_same_lecture_on_fifteen_dates_collapses_to_one_lesson(http):
    _serve_feed(http, "isks-1k", [
        _event(f"2026-{month:02d}-{day:02d}T08:30:00", f"2026-{month:02d}-{day:02d}T10:00:00",
               instructor="A. Petraitis", location="301")
        for month, day in [(2, 9), (2, 16), (2, 23), (3, 2), (3, 9), (3, 16), (3, 23),
                           (3, 30), (4, 6), (4, 13), (4, 20), (4, 27), (5, 4), (5, 11), (5, 18)]
    ])

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert len(lessons) == 1
    assert stats["events"] == 15


@pytest.mark.parametrize("second", [
    {"title": "Kitas dalykas"},
    {"instructor": "B. Jonaitis"},
    {"location": "204"},
    {"start": "2026-02-09T08:45:00"},
    {"end": "2026-02-09T10:30:00"},
    {"start": "2026-02-10T08:30:00", "end": "2026-02-10T10:00:00"},
])
def test_a_difference_in_any_identity_field_makes_a_second_lesson(http, second):
    base = _event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
                  instructor="A. Petraitis", location="301")
    other = dict(base)
    other.update(second)
    _serve_feed(http, "isks-1k", [base, other])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert len(lessons) == 2


def test_the_same_slot_in_two_semesters_stays_two_lessons(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="301"),
        _event("2026-09-14T08:30:00", "2026-09-14T10:00:00", location="301"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert {lesson["semester"] for lesson in lessons} == {"2025-P", "2026-R"}


def test_the_lessons_keep_the_order_their_first_occurrence_had(http):
    _serve_feed(http, "isks-1k", [
        _event("2026-02-11T12:00:00", "2026-02-11T13:00:00", title="Trečias"),
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", title="Pirmas"),
        _event("2026-02-18T12:00:00", "2026-02-18T13:00:00", title="Trečias"),
        _event("2026-02-10T10:00:00", "2026-02-10T11:00:00", title="Antras"),
    ])

    lessons, _ = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                          "2026-01-27", "2026-06-30")

    assert [lesson["title"] for lesson in lessons] == ["Trečias", "Pirmas", "Antras"]


@pytest.mark.slow
def test_a_feed_of_a_thousand_repeats_still_yields_one_lesson(http):
    _serve_feed(http, "isks-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00",
                                         instructor="A. Petraitis", location="301")] * 1000)

    lessons, stats = ss.scrape_group_schedule("isks-1k", "Ekonomika ir vadyba - 1 kursas",
                                              "2026-01-27", "2026-06-30")

    assert len(lessons) == 1
    assert stats["events"] == 1000




# ==========================================================
# scrape_knf_schedule — the window, the anchor and the lock
# ==========================================================


def test_the_window_asked_for_is_two_weeks_back_and_twenty_forward(app, http):
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert _params_of(http.calls[1]) == {"start": "2026-01-27", "end": "2026-06-30"}


@pytest.mark.parametrize("forward_weeks,expected_end", [
    (0, "2026-02-10"), (1, "2026-02-17"), (20, "2026-06-30"), (52, "2027-02-09"),
])
def test_the_forward_half_of_the_window_is_the_callers_to_choose(app, http,
                                                                forward_weeks, expected_end):
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(forward_weeks=forward_weeks, notify=False)

    assert _params_of(http.calls[1]) == {"start": "2026-01-27", "end": expected_end}


def test_a_forward_window_of_the_wrong_type_fails_the_run_instead_of_raising(app, db, http):
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(forward_weeks="20", notify=False)

    assert result["lessons_found"] == 0
    assert "error" in result
    assert _run_rows(db)[0]["status"] == "failed"
    # It broke before the group list was even asked for
    assert len(http.calls) == 0


def test_an_autumn_run_anchors_on_the_autumn_label(app, db, http):
    _serve_one_group(http, [_event("2026-09-14T08:30:00", "2026-09-14T10:00:00")])

    with time_machine.travel(AUTUMN_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert [row[7] for row in _lesson_rows(db)] == [AUTUMN_ANCHOR]


def test_a_second_run_finding_the_lock_held_is_skipped_without_a_run_row(app, db, http):
    ss._RUN_LOCK.acquire()

    result = ss.scrape_knf_schedule(notify=False)

    assert result == {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0, "skipped": True}
    assert _run_rows(db) == []
    assert len(http.calls) == 0


def test_a_skipped_run_leaves_the_lock_with_whoever_holds_it(app, http):
    ss._RUN_LOCK.acquire()

    ss.scrape_knf_schedule(notify=False)

    assert ss._RUN_LOCK.locked()


def test_a_completed_run_hands_the_lock_back(app, http):
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert not ss._RUN_LOCK.locked()


def test_a_failed_run_hands_the_lock_back_too(app, http):
    _serve_list(http, "", status=500)

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert not ss._RUN_LOCK.locked()


def test_a_completed_run_closes_the_connection_it_opened(app, http, monkeypatch):
    closed = []
    real_get_db = ss.get_db

    class _CountsCloses:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def close(self):
            closed.append(1)
            self._conn.close()

    monkeypatch.setattr(ss, "get_db", lambda: _CountsCloses(real_get_db()))
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert closed == [1]




# ==========================================================
# scrape_knf_schedule — how many groups it gets through
# ==========================================================


def test_every_group_on_the_list_is_fetched_once(app, http):
    _serve_list(http, _page(
        _block("Ekonomika ir vadyba - 1 kursas", "ev-1k"),
        _block("Ekonomika ir vadyba - 2 kursas", "ev-2k"),
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    for slug in ("ev-1k", "ev-2k", "mv-1k"):
        _serve_feed(http, slug, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["groups_scraped"] == 3
    assert len(http.calls) == 4


def test_one_failing_group_never_costs_the_run_the_others(app, db, http):
    _serve_list(http, _page(
        _block("Ekonomika ir vadyba - 1 kursas", "ev-1k"),
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    _serve_feed(http, "ev-1k", body="", status=503)
    _serve_feed(http, "mv-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["groups_scraped"] == 1
    assert [row[6] for row in _lesson_rows(db)] == ["MV-1"]


def test_a_failing_group_leaves_the_rows_it_already_had_alone(app, db, http):
    _seed_lesson(db, SPRING_ANCHOR, group_name="EV-1", title="Sena paskaita")
    _serve_list(http, _page(
        _block("Ekonomika ir vadyba - 1 kursas", "ev-1k"),
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    _serve_feed(http, "ev-1k", body="ne json")
    _serve_feed(http, "mv-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert "Sena paskaita" in [row[0] for row in _lesson_rows(db)]


def test_a_group_the_list_dropped_is_never_fetched(app, http):
    _serve_list(http, _page(
        '<a href="/knf/groups/blogas.slug/">Bloga</a>',
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    _serve_feed(http, "mv-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])
    _serve_feed(http, "blogas.slug", [])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["groups_scraped"] == 1
    assert len(http.calls) == 2
    assert "blogas.slug" not in http.calls[1].request.url


def test_the_time_budget_stops_the_run_between_two_groups(app, db, http, monkeypatch):
    # False for the first group, True from the second on — the
    # "out of time after N group(s)" break with N above zero
    answers = iter([False, True, True])
    monkeypatch.setattr(ss, "deadline_passed", lambda _deadline: next(answers))

    _serve_list(http, _page(
        _block("Ekonomika ir vadyba - 1 kursas", "ev-1k"),
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    _serve_feed(http, "ev-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])
    _serve_feed(http, "mv-1k", [_event("2026-02-10T08:30:00", "2026-02-10T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["groups_scraped"] == 1
    assert [row[6] for row in _lesson_rows(db)] == ["EV-1"]
    assert _run_rows(db)[0]["status"] == "completed"


def test_a_run_out_of_time_before_the_first_group_still_completes(app, db, http, monkeypatch):
    monkeypatch.setattr(ss, "RUN_BUDGET_SECONDS", -1)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    # groups_scraped is zero, so the "no lessons anywhere" guard
    # deliberately does NOT fire
    assert (result["groups_scraped"], result["lessons_found"]) == (0, 0)
    assert "error" not in result
    assert _run_rows(db)[0]["status"] == "completed"


def test_a_run_where_every_group_failed_completes_with_zeros(app, db, http):
    # Only a failed group LIST fails the run — failing feeds are
    # skipped, and with nothing scraped there is nothing to
    # reconcile against
    _serve_list(http, _page(
        _block("Ekonomika ir vadyba - 1 kursas", "ev-1k"),
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    _serve_feed(http, "ev-1k", body="", status=500)
    _serve_feed(http, "mv-1k", body="", status=500)

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert (result["groups_scraped"], result["lessons_found"]) == (0, 0)
    assert "error" not in result
    assert _run_rows(db)[0]["status"] == "completed"




# ==========================================================
# scrape_knf_schedule — semester keeping and the write phase
# ==========================================================


def test_a_semester_label_seen_four_times_is_dropped_as_a_stray(app, db, http, caplog):
    _serve_one_group(http, _many_events(6) + _autumn_events(4))

    with caplog.at_level(logging.INFO, logger="app.scraper.schedule_scraper"):
        with time_machine.travel(SPRING_RUN, tick=False):
            ss.scrape_knf_schedule(notify=False)

    assert _lesson_rows(db, "2026-R") == []
    assert "Dropping stray semester label 2026-R (4 event(s) this run)" in caplog.text


def test_a_semester_label_seen_five_times_is_kept(app, db, http):
    _serve_one_group(http, _many_events(6) + _autumn_events(5))

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert len(_lesson_rows(db, "2026-R")) == 5


def test_the_stray_threshold_counts_across_the_whole_run_not_one_group(app, db, http):
    _serve_list(http, _page(
        _block("Ekonomika ir vadyba - 1 kursas", "ev-1k"),
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    _serve_feed(http, "ev-1k", _many_events(3) + _autumn_events(3))
    _serve_feed(http, "mv-1k", _many_events(3) + _autumn_events(3))

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    # Three per group, six across the run — over the threshold
    assert len(_lesson_rows(db, "2026-R")) == 6


def test_the_anchor_semester_is_kept_on_a_single_lesson(app, db, http):
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert len(_lesson_rows(db, SPRING_ANCHOR)) == 1


def test_the_anchor_partition_is_rewritten_from_the_scraped_set(app, db, http):
    _seed_lesson(db, SPRING_ANCHOR, group_name="ISKS-1", title="Fantomas",
                 teacher="", room="999")
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="301"),
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert [row[0] for row in _lesson_rows(db, SPRING_ANCHOR)] == ["Programavimas"]


def test_a_neighbouring_semester_is_only_ever_added_to(app, db, http):
    # The window clips a fortnight of the next semester, which
    # is nowhere near enough to rebuild it from
    _seed_lesson(db, "2026-R", group_name="ISKS-1", title="Rudens paskaita")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")]
                     + _autumn_events(5))

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    titles = [row[0] for row in _lesson_rows(db, "2026-R")]
    assert "Rudens paskaita" in titles
    assert len(titles) == 6


def test_a_neighbouring_semester_row_that_is_already_there_is_not_doubled(app, db, http):
    events = [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")] + _autumn_events(5)
    for _ in range(2):
        _serve_one_group(http, events)

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)
        second = ss.scrape_knf_schedule(notify=False)

    assert second["lessons_new"] == 0
    assert len(_lesson_rows(db, "2026-R")) == 5


def test_a_dropped_stray_label_writes_no_row_at_all(app, db, http):
    _serve_one_group(http, _many_events(6) + _autumn_events(1))

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    # It is counted as found — it WAS scraped — but never stored
    assert (result["lessons_found"], result["lessons_new"]) == (7, 6)
    assert _lesson_rows(db, "2026-R") == []


def test_two_groups_of_the_same_semester_are_separate_partitions(app, db, http):
    _serve_list(http, _page(
        _block("Ekonomika ir vadyba - 1 kursas", "ev-1k"),
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    _serve_feed(http, "ev-1k", [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])
    _serve_feed(http, "mv-1k", [_event("2026-02-10T08:30:00", "2026-02-10T10:00:00")])
    _seed_lesson(db, SPRING_ANCHOR, group_name="TVV-1", title="Kitos grupės paskaita")

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert sorted(row[6] for row in _lesson_rows(db)) == ["EV-1", "MV-1", "TVV-1"]


def test_running_the_same_timetable_three_times_changes_nothing_after_the_first(app, db, http):
    events = [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", location="301"),
        _event("2026-02-10T10:15:00", "2026-02-10T11:45:00", title="Duomenų bazės"),
    ]
    for _ in range(3):
        _serve_one_group(http, events)

    with time_machine.travel(SPRING_RUN, tick=False):
        results = [ss.scrape_knf_schedule(notify=False) for _ in range(3)]

    assert [result["lessons_new"] for result in results] == [2, 0, 0]
    assert len(_lesson_rows(db)) == 2


def test_the_result_of_a_completed_run_has_exactly_four_keys(app, http):
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert set(result) == {"groups_scraped", "lessons_found", "lessons_new", "dropped"}


def test_the_dropped_block_sums_every_groups_filters(app, http, caplog):
    _serve_list(http, _page(
        _block("Ekonomika ir vadyba - 1 kursas", "ev-1k"),
        _block("Meno vadyba - 1 kursas", "mv-1k"),
    ))
    noise = [
        {"start": "2026-02-09", "title": "Šventė"},
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00", color=RETAKE_COLOUR,
               title="Perlaikymas"),
        _event("2026-02-10T08:30:00", None, title="Sugedęs"),
        _event("2026-02-11T08:30:00", "2026-02-11T10:00:00", title=""),
    ]
    _serve_feed(http, "ev-1k", noise + [_event("2026-02-12T08:30:00", "2026-02-12T10:00:00")])
    _serve_feed(http, "mv-1k", noise + [_event("2026-02-13T08:30:00", "2026-02-13T10:00:00")])

    with caplog.at_level(logging.INFO, logger="app.scraper.schedule_scraper"):
        with time_machine.travel(SPRING_RUN, tick=False):
            result = ss.scrape_knf_schedule(notify=False)

    assert result["dropped"] == {"all_day": 2, "retakes": 2, "unparsable": 2, "untitled": 2}
    assert "'#ff899d': 2" in caplog.text




# ==========================================================
# scrape_knf_schedule — retiring finished semesters
# ==========================================================


def test_a_finished_semester_is_retired_once_the_anchor_has_rows(app, db, http):
    _seed_lesson(db, "2024-R", title="Labai sena")
    _seed_lesson(db, "2024-P", title="Dar senesnė")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert {row[7] for row in _lesson_rows(db)} == {SPRING_ANCHOR}


def test_the_semester_just_before_the_anchor_is_retired_too(app, db, http):
    _seed_lesson(db, "2025-R", title="Praėjęs ruduo")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert _lesson_rows(db, "2025-R") == []


def test_a_semester_newer_than_the_anchor_survives_the_purge(app, db, http):
    _seed_lesson(db, "2026-R", title="Būsimas ruduo")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert [row[0] for row in _lesson_rows(db, "2026-R")] == ["Būsimas ruduo"]


def test_a_label_this_scraper_never_wrote_is_left_for_an_admin(app, db, http):
    _seed_lesson(db, "2019-pavasaris", title="Senas seed")
    _seed_lesson(db, "rankinis", title="Ranka įvesta")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert sorted(row[0] for row in _lesson_rows(db) if row[7] != SPRING_ANCHOR) == [
        "Ranka įvesta", "Senas seed"]


def test_nothing_is_retired_when_the_anchor_semester_harvested_nothing(app, db, http):
    # A July run whose feed holds only next autumn: the anchor
    # 2025-P is not in the kept set, so the purge never runs
    _seed_lesson(db, "2024-R", title="Labai sena")
    _serve_one_group(http, _autumn_events(5))

    with time_machine.travel(SUMMER_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert [row[0] for row in _lesson_rows(db, "2024-R")] == ["Labai sena"]


def test_the_purge_logs_how_many_rows_each_finished_semester_lost(app, db, http, caplog):
    _seed_lesson(db, "2024-R", title="Sena A")
    _seed_lesson(db, "2024-R", title="Sena B", time_start="10:00", time_end="11:30")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with caplog.at_level(logging.INFO, logger="app.scraper.schedule_scraper"):
        with time_machine.travel(SPRING_RUN, tick=False):
            ss.scrape_knf_schedule(notify=False)

    assert "Retired 2 lesson(s) from the finished semester 2024-R" in caplog.text




# ==========================================================
# scrape_knf_schedule — the run row and its neighbours
# ==========================================================


def test_a_completed_run_row_carries_the_lesson_counts_and_a_finish_time(app, db, http):
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00"),
        _event("2026-02-10T10:15:00", "2026-02-10T11:45:00", title="Duomenų bazės"),
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    row = _run_rows(db)[0]
    assert row["status"] == "completed"
    assert (row["articles_found"], row["articles_new"]) == (2, 2)
    assert row["error_message"] is None
    assert row["finished_at"] is not None


def test_a_second_run_over_the_same_timetable_records_zero_new(app, db, http):
    events = [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")]
    for _ in range(2):
        _serve_one_group(http, events)

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)
        ss.scrape_knf_schedule(notify=False)

    assert [(row["articles_found"], row["articles_new"])
            for row in _run_rows(db)] == [(1, 1), (1, 0)]


def test_an_empty_group_list_fails_the_run_instead_of_reporting_a_tidy_zero(app, db, http):
    _serve_list(http, _page('<a href="/knf/list/">Atgal</a>'))

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["error"] == ("no groups on the tvarkarasciai.vu.lt list page"
                               " — the markup has probably changed")
    assert result["groups_scraped"] == 0
    row = _run_rows(db)[0]
    assert (row["status"], row["error_message"]) == ("failed", result["error"])
    assert row["finished_at"] is not None


def test_a_failed_group_list_fetch_fails_the_run_and_returns_its_id(app, db, http):
    _serve_list(http, "", status=503)

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert "Could not fetch the group list" in result["error"]
    assert result["runId"]
    row = _run_rows(db)[0]
    assert (row["status"], row["articles_found"]) == ("failed", 0)
    assert row["finished_at"] is not None


def test_group_feeds_without_a_single_lesson_fail_the_run_before_the_write(app, db, http):
    _seed_lesson(db, SPRING_ANCHOR, group_name="ISKS-1", title="Turi išlikti")
    _serve_one_group(http, [{"start": "2026-02-09", "title": "Šventė"}])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["error"] == ("no lessons in any of 1 group feed(s)"
                               " — the feed shape has probably changed")
    assert (result["groups_scraped"], result["lessons_found"]) == (1, 0)
    assert _run_rows(db)[0]["status"] == "failed"
    # Nothing was reconciled against the empty scrape
    assert [row[0] for row in _lesson_rows(db)] == ["Turi išlikti"]


def test_a_write_phase_failure_rolls_back_and_marks_the_run_failed(app, db, http, monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("disko klaida")

    monkeypatch.setattr(ss, "_reconcile_partition", _explode)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["error"] == "disko klaida"
    assert (result["groups_scraped"], result["lessons_found"], result["lessons_new"]) == (0, 0, 0)
    assert _lesson_rows(db) == []
    assert _run_rows(db)[0]["error_message"] == "disko klaida"


def test_a_purge_failure_after_a_successful_write_still_fails_the_run(app, db, http, monkeypatch):
    def _explode(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ss, "_purge_old_semesters", _explode)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule(notify=False)

    assert result["error"] == "database is locked"
    assert _run_rows(db)[0]["status"] == "failed"


def test_a_rollback_that_itself_fails_still_closes_the_run_row(app, db, http, monkeypatch, caplog):
    # The connection is exactly what may have broken, so its
    # rollback is allowed to fail without swallowing the run
    # row — mark_run_failed opens its own connection for that
    real_get_db = ss.get_db

    class _RollbackBreaks:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def rollback(self):
            raise sqlite3.OperationalError("cannot rollback")

    def _explode(*args, **kwargs):
        raise RuntimeError("valymo klaida")

    monkeypatch.setattr(ss, "get_db", lambda: _RollbackBreaks(real_get_db()))
    # The purge blows up AFTER the write phase committed, so the
    # broken rollback is the only thing standing in the way
    monkeypatch.setattr(ss, "_purge_old_semesters", _explode)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with caplog.at_level(logging.WARNING, logger="app.scraper.schedule_scraper"):
        with time_machine.travel(SPRING_RUN, tick=False):
            result = ss.scrape_knf_schedule(notify=False)

    assert result["error"] == "valymo klaida"
    assert _run_rows(db)[0]["status"] == "failed"
    assert "Rollback after the schedule failure did not take" in caplog.text


def test_a_run_older_than_the_retention_window_is_pruned_at_the_end(app, db, http):
    _seed_completed_run(db, started_at="2025-11-01T06:00:00+00:00")
    _seed_completed_run(db, started_at="2025-11-02T06:00:00+00:00")
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    # Both seeds are months old and neither is the newest row of
    # the source any more, so only this run survives
    assert len(_run_rows(db)) == 1


def test_a_collapsed_yield_against_the_last_run_is_logged_as_an_error(app, db, http, caplog):
    _seed_completed_run(db, found=200, new=200)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with caplog.at_level(logging.ERROR, logger="app.scraper.common"):
        with time_machine.travel(SPRING_RUN, tick=False):
            ss.scrape_knf_schedule(notify=False)

    assert "yield collapsed" in caplog.text


def test_a_steady_yield_against_the_last_run_logs_no_error(app, db, http, caplog):
    _seed_completed_run(db, found=2, new=2)
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00"),
        _event("2026-02-10T10:15:00", "2026-02-10T11:45:00", title="Duomenų bazės"),
    ])

    with caplog.at_level(logging.ERROR, logger="app.scraper.common"):
        with time_machine.travel(SPRING_RUN, tick=False):
            ss.scrape_knf_schedule(notify=False)

    assert "yield collapsed" not in caplog.text




# ==========================================================
# scrape_knf_schedule — the push gate
# ==========================================================


############################################################
# _recorded_push
############################################################
#
# notify_channel replaced by a recorder. The scraper imports
# it lazily inside the function, so patching the module
# attribute is enough — and `boom` proves a push that raises
# never turns a completed run into a failed one.
#
# Used by:
#   - every push-gate test below
############################################################

def _recorded_push(monkeypatch, boom=False):
    calls = []

    def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        if boom:
            raise RuntimeError("push neveikia")

    monkeypatch.setattr(push_module, "notify_channel", _fake)

    return calls


def test_new_lessons_push_the_full_lithuanian_and_english_copy(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00"),
        _event("2026-02-10T10:15:00", "2026-02-10T11:45:00", title="Duomenų bazės"),
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("schedule", "Tvarkaraščio pakeitimai", "2 nauji įrašai tvarkaraštyje")
    assert kwargs == {
        "data": {"type": "schedule_update", "newLessons": 2},
        "title_en": "Timetable changes",
        "body_en": "2 new timetable entries",
    }


def test_a_single_new_lesson_gets_the_singular_bodies(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()

    args, kwargs = calls[0]
    assert args[2] == "Naujas įrašas tvarkaraštyje"
    assert kwargs["body_en"] == "New timetable entry"


@pytest.mark.parametrize("count,body", [
    (2, "2 nauji įrašai tvarkaraštyje"),
    (10, "10 naujų įrašų tvarkaraštyje"),
    (11, "11 naujų įrašų tvarkaraštyje"),
    (21, "21 naujas įrašas tvarkaraštyje"),
])
def test_the_lithuanian_body_declines_with_the_count(app, db, http, monkeypatch, count, body):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_one_group(http, _many_events(count))

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()

    assert calls[0][0][2] == body


def test_the_admin_trigger_never_pushes_and_never_asks_whether_it_may(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    asked = []
    monkeypatch.setattr(ss, "push_allowed", lambda *args: asked.append(1) or True)
    _seed_completed_run(db)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)

    assert (calls, asked) == ([], [])


def test_an_unchanged_timetable_never_asks_whether_it_may_push(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    asked = []
    monkeypatch.setattr(ss, "push_allowed", lambda *args: asked.append(1) or True)
    _seed_completed_run(db)
    events = [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")]
    for _ in range(2):
        _serve_one_group(http, events)

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule(notify=False)
        second = ss.scrape_knf_schedule()

    assert second["lessons_new"] == 0
    assert (calls, asked) == ([], [])


def test_a_first_import_is_a_backfill_and_does_not_push(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()

    assert calls == []


def test_a_burst_over_the_threshold_does_not_push(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_one_group(http, _many_events(26))

    with time_machine.travel(SPRING_RUN, tick=False):
        result = ss.scrape_knf_schedule()

    assert result["lessons_new"] == 26
    assert calls == []


def test_a_second_push_within_the_hour_is_suppressed(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])
    _serve_one_group(http, [
        _event("2026-02-09T08:30:00", "2026-02-09T10:00:00"),
        _event("2026-02-10T10:15:00", "2026-02-10T11:45:00", title="Duomenų bazės"),
    ])

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()
        second = ss.scrape_knf_schedule()

    assert second["lessons_new"] == 1
    assert len(calls) == 1


def test_a_push_that_raises_leaves_the_run_completed(app, db, http, monkeypatch, caplog):
    _recorded_push(monkeypatch, boom=True)
    _seed_completed_run(db)
    _serve_one_group(http, [_event("2026-02-09T08:30:00", "2026-02-09T10:00:00")])

    with caplog.at_level(logging.ERROR, logger="app.scraper.schedule_scraper"):
        with time_machine.travel(SPRING_RUN, tick=False):
            result = ss.scrape_knf_schedule()

    assert result["lessons_new"] == 1
    assert "error" not in result
    assert _run_rows(db)[-1]["status"] == "completed"
    assert "Failed to send push notification" in caplog.text


def test_a_failed_run_pushes_nothing(app, db, http, monkeypatch):
    calls = _recorded_push(monkeypatch)
    _seed_completed_run(db)
    _serve_list(http, "", status=500)

    with time_machine.travel(SPRING_RUN, tick=False):
        ss.scrape_knf_schedule()

    assert calls == []
