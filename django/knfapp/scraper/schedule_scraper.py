############################################################
#  [*] Schedule scraper — tvarkarasciai.vu.lt timetable
#
#  Pulls every KNF group's FullCalendar event feed from
#  tvarkarasciai.vu.lt and stores the events DATED, one
#  schedule_events row per (date, times, title, type, room)
#  — the tracer system's proven event identity. Teacher and
#  group are NOT identity: a teacher swap updates the row,
#  and every group feed serving the same lecture confirms
#  ONE row through schedule_event_groups. Irregular and
#  one-off lectures need no folding: a lecture that happens
#  on three scattered dates is three rows on those dates.
#
#  Sync is the tracer confirmation-stamp pattern: each run
#  inserts-or-confirms events and link rows with the run's
#  stamp, then deletes FUTURE events whose stamp went stale
#  (the site stopped serving them) — except events whose
#  every group feed FAILED this run, which are preserved
#  (nothing was fetched, so nothing was cancelled). A stale
#  group link on a surviving event is retired alone: the
#  lecture moved out of that one group's feed. Past events
#  are history: kept as scraped until RETENTION_DAYS, then
#  dropped with their links.
#
#  The window is rolling — [today - 2 weeks, today + 20
#  weeks] — instead of "this semester from its first day":
#  a from-the-first-day window would make a January run
#  re-import the finished autumn semester and miss the
#  spring one for a month, and a 16-week cutoff would drop
#  the January and June exam sessions entirely.
#
#  Group names collapse to programme abbreviation + course
#  ("ISKS-1"); the "1 grupė / 2 grupė" split is dropped, so
#  parallel groups share one group_name. The programme table
#  is ORDERED, most specific pattern first, so a
#  specialisation is not swallowed by the programme whose
#  words it repeats; a group whose course cannot be parsed
#  keeps its unique slug instead of merging every year of
#  the programme into one timetable. Semester labels are
#  "<year>-R" (ruduo) / "<year>-P" (pavasaris), keyed on the
#  academic year's FIRST calendar year — spring 2026 is
#  "2025-P". A label carrying fewer than
#  MIN_SEMESTER_LESSONS events across the whole run is
#  dropped rather than stored, so one stray event can never
#  become a semester option in the mobile picker; old
#  semesters leave through retention, not a purge, so last
#  year's matching term stays browsable.
#
#  Every run is logged in scraper_runs (source
#  'tvarkarasciai.vu.lt') with the lesson counts in the
#  articles_found / articles_new columns the news scrapers
#  named, and prunes run rows older than 30 days. The cron
#  command runs it every 6 h, api/views.py exposes an admin
#  trigger, and the rows flow on to the schedule views → the
#  mobile schedule tab.
############################################################



import html
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

from bs4 import BeautifulSoup

from django.db import connection, transaction

from knfapp.common.timestamps import utc_now
from knfapp.schedule.models import (
    ScheduleEvent,
    ScheduleEventGroup,
    ScheduleEventTeacher,
    ScheduleGroup,
    ScheduleTeacher,
)
from knfapp.scraper.common import (
    HTML_CONTENT_TYPES,
    JSON_CONTENT_TYPES,
    SCHEDULE_HOSTS,
    check_yield_drop,
    close_run,
    deadline_passed,
    fetch,
    mark_run_failed,
    open_run,
    prune_scraper_runs,
    push_allowed,
    run_deadline,
    utc_now_naive,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://tvarkarasciai.vu.lt"
GROUP_LIST_URL = f"{BASE_URL}/knf/list/"
# The JSON feed behind a group's timetable page; the
# "/group/255/" segment is fixed and the date window goes in
# as start/end query params (see scrape_group_schedule)
EVENT_URL_TEMPLATE = f"{BASE_URL}/knf/ajax_fullcalendar_events/{{slug}}/group/255/"

# The rolling window, in weeks either side of today: two
# back so a just-finished week can still be corrected,
# twenty forward to cover the rest of the semester and the
# exam session behind it
WINDOW_BACK_WEEKS = 2
WINDOW_FORWARD_WEEKS = 20

# How long a PAST event stays as history before the run
# drops it (with its links) — ~13 months keeps the previous
# academic year's matching semester browsable, mirroring the
# tracer system's retention idea at app scale
RETENTION_DAYS = 400

# A semester label the whole run saw fewer times than this
# is a stray (a single misdated event) and is not stored —
# it would otherwise show up as an option in the mobile
# semester picker
MIN_SEMESTER_LESSONS = 5

# Wall-clock budget: ~100 group feeds fit comfortably, and
# open_run's staleness horizon for this source
RUN_BUDGET_SECONDS = 3000

# A group slug is a path segment of an URL we build — it
# comes off a scraped page, so it is validated (and still
# percent-encoded) before it is interpolated
_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# Retake exams, by the colour the site paints them. Compared
# after _normalise_colour, so "#FF899D", "#ff899d" and
# "rgb(255, 137, 157)" are the same value
_RETAKE_COLOURS = frozenset({"#ff899d"})

# The programme table, ORDER SIGNIFICANT: the first pattern
# found in the name wins, so every compound programme has to
# stand before the broader one whose words it contains
# ("turinio kurimas ir rinkodara" is a Lietuvių filologija ir
# reklama specialisation and its display name carries both).
# All ASCII on purpose — matched after _strip_diacritics
_PROGRAM_ABBREVS = (
    ("turinio kurimas ir rinkodara", "LFR-TKR"),
    ("kurybiskumo ir skaitmenines retorikos", "LFR-KSR"),
    ("mediju retorika ir komunikacija", "VDL-MRK"),
    ("skaitmeninio turinio prieinamumas", "AV-STP"),
    ("informacijos sistemos ir kibernetin", "ISKS"),
    ("lietuviu literatura ir kurybinis rasymas", "LLKR"),
    ("lietuviu filologija ir reklama", "LFR"),
    ("marketingas ir pardavimu vadyba", "MPV"),
    ("marketingo technologijos", "MT"),
    ("tarptautinio verslo vadyba", "TVV"),
    ("tvariuju finansu ekonomika", "TFE"),
    ("finansu analitika", "FA"),
    ("finansu technologijos", "FT"),
    ("ekonomika ir vadyba", "EV"),
    ("audiovizualinis vertimas", "AV"),
    ("viesojo diskurso lingvistika", "VDL"),
    ("kalba ir dirbtinio intelekto valdymas", "KDIV"),
    ("meno vadyba", "MV"),
    ("art management", "MV"),
    ("anglu ir kita uzsienio kalba", "AKUK"),
    ("bendruju universitetiniu studiju", "BUS"),
    ("individualiuju studiju dalykai", "ISD"),
)

# The two subject pools that legitimately have no course —
# every other programme without one is an unparsed name
_COURSELESS_ABBREVS = frozenset({"BUS", "ISD"})

# An EXPLICIT English-taught marker. A bare "angl" substring
# also fires on the Lithuanian programme "Anglų ir kita
# užsienio kalba" — and would tag all of its groups "-EN"
_LANG_EN_RE = re.compile(
    r"angl\w*\s+(?:kalb\w*|k\.)|in\s+english|english[-\s]taught|(?:^|[-\s(])en(?:[-\s)]|$)",
    re.IGNORECASE,
)

# One timetable run at a time in this process; open_run
# guards the other processes
_RUN_LOCK = threading.Lock()








############################################################
# _get_semester_label
############################################################
#
# Semester label for one event date: August–December →
# "<year>-R", January–July → "<year-1>-P", so the label
# always carries the academic year's first calendar year
# (2026-02-09 → "2025-P"). January counts as spring, which
# is where the January exam session belongs. The rolling
# window straddles the boundary twice a year and events on
# either side keep their own label; the run's ANCHOR label
# (today's) decides which partitions get reconciled and
# which are only added to.
#
# Used by:
#   - scrape_group_schedule (below) — per event
#   - scrape_knf_schedule (below) — the run's anchor label
############################################################

def _get_semester_label(dt: datetime) -> str:
    if dt.month >= 8:
        return f"{dt.year}-R"
    else:
        # Jan-Jul belongs to the academic year that started the
        # previous autumn, hence year - 1
        return f"{dt.year - 1}-P"








############################################################
# _strip_diacritics
############################################################
#
# Folds the nine Lithuanian letters (both cases) to ASCII so
# the programme table in _parse_group_display_name can be
# written and matched without diacritics. The translation
# table is rebuilt on every call — cheap, but it could be a
# module constant.
#
# Used by:
#   - _parse_group_display_name (below)
############################################################

def _strip_diacritics(text: str) -> str:
    _MAP = str.maketrans(
        "\u0105\u010d\u0119\u0117\u012f\u0161\u0173\u016b\u017e\u0104\u010c\u0118\u0116\u012e\u0160\u0172\u016a\u017d",
        "aceeisuuzACEEISUUZ",
    )
    return text.translate(_MAP)








############################################################
# _parse_group_display_name
############################################################
#
# Collapses a group's display name (or, failing that, its
# slug) to the short group_name the app filters on:
# programme abbreviation + optional "-M" (magistrantūra) +
# optional "-EN" + course digit, e.g. "Informacijos sistemos
# ir kibernetinė sauga - 1 kursas 1 grupė" → "ISKS-1". The
# "N grupė" part is dropped on purpose — every parallel
# group of a course shares one group_name.
#
# Matching walks _PROGRAM_ABBREVS in order, first against
# the diacritic-stripped lowercase display name, then the
# de-hyphenated slug. The order is the whole point: the
# SPECIFIC programmes come first, so a name carrying both
# "turinio kurimas ir rinkodara" and "lietuviu filologija ir
# reklama" resolves to "LFR-TKR" and not to the broader
# "LFR" — that collision would silently merge two real
# timetables.
#
# "-EN" needs an explicit language marker ("anglų kalba",
# "in english", an "-en" token in the slug), never the bare
# "angl" substring, which also fires on the Lithuanian-taught
# "Anglų ir kita užsienio kalba" programme itself. The course
# digit comes from "N kursas" in the name or "Nk"/"Nc" in the
# slug; a programme that normally HAS courses and produced
# none refuses to emit the course-less name (all its courses
# would merge into one timetable) and falls back to the slug,
# which is unique, with the slug logged for review.
#
# Used by:
#   - scrape_group_schedule (below) — once per group
############################################################

def _parse_group_display_name(slug: str, display_name: str) -> str:
    # Slugs are ASCII already, so only the name is folded
    candidates = [
        _strip_diacritics(display_name).lower(),
        slug.replace("-", " "),
    ]

    for name_lower in candidates:
        for pattern, abbrev in _PROGRAM_ABBREVS:
            if pattern not in name_lower:
                continue

            # "N kursas" only exists in the display name; the
            # slug spells it "1k" (the regex also takes "1c")
            course_match = re.search(r"(\d)\s*kursas", name_lower)
            if not course_match:
                course_match = re.search(r"(\d)[kc]", slug)
            course = course_match.group(1) if course_match else ""

            # A course-bearing programme with no course parsed:
            # emitting "EV" would pool all four years into one
            # timetable, so the unique slug is used instead
            if not course and abbrev not in _COURSELESS_ABBREVS:
                logger.warning("No course in the %s group '%s' (slug %s) — keeping the slug as its name",
                               abbrev, display_name, slug)
                return slug[:30]

            # An explicit language marker only — plain "angl"
            # also matches the AKUK programme's own name
            lang_suffix = "-EN" if _LANG_EN_RE.search(name_lower) or _LANG_EN_RE.search(slug) else ""

            level_suffix = "-M" if "magistrant" in name_lower else ""

            return f"{abbrev}{level_suffix}{lang_suffix}-{course}" if course else f"{abbrev}{level_suffix}{lang_suffix}"

    # No programme matched — the raw slug, capped at 30 chars
    logger.info("No programme matched the group '%s' (slug %s) — keeping the slug as its name",
                display_name, slug)
    return slug[:30]







############################################################
# _extract_teacher_from_html
############################################################
#
# The lecturers from an event title that carries the site's
# popover markup: the data-academics attribute holds
# HTML-escaped HTML, so it is regex-lifted, unescaped and
# parsed again. EVERY <a> is kept (sorted, joined with
# ", ") — keeping only the first would cost a co-taught
# lecture a name, while the feed's own top-level
# "instructor" field already carries them all. Without links
# the flattened text is used with its "Dėstytojai: " label
# stripped. "" when the attribute is absent.
#
# Used by:
#   - scrape_group_schedule (below) — only when the event
#     has no top-level "instructor" field
############################################################

def _extract_teacher_from_html(title_html: str) -> str:
    match = re.search(r'data-academics="([^"]*)"', title_html)
    if match:
        raw = html.unescape(match.group(1))
        soup = BeautifulSoup(raw, "html.parser")
        names = _joined_link_texts(soup)
        if names:
            return names
        text = soup.get_text(strip=True)
        # The label only survives in the link-less form
        text = re.sub(r"^D\u0117stytojai:\s*", "", text)
        return text
    return ""








############################################################
# _extract_room_from_html
############################################################
#
# Same lift-unescape-parse dance as
# _extract_teacher_from_html, on the data-rooms attribute:
# every <a> sorted and joined with ", " (a lecture split
# across two rooms keeps both), else the flattened text minus
# its "Patalpos: " label, "" when the attribute is absent.
#
# Used by:
#   - scrape_group_schedule (below) — only when the event
#     has no top-level "location" field
############################################################

def _extract_room_from_html(title_html: str) -> str:
    match = re.search(r'data-rooms="([^"]*)"', title_html)
    if match:
        raw = html.unescape(match.group(1))
        soup = BeautifulSoup(raw, "html.parser")
        rooms = _joined_link_texts(soup)
        if rooms:
            return rooms
        text = soup.get_text(strip=True)
        text = re.sub(r"^Patalpos:\s*", "", text)
        return text
    return ""








############################################################
# _joined_link_texts
############################################################
#
# Every <a> text in one popover fragment as a single stable
# string: blanks dropped, duplicates collapsed, sorted,
# joined with ", ". Sorted because the result lands in a
# column that is part of the lesson's natural key — "A.
# Petraitis, B. Jonaitis" and "B. Jonaitis, A. Petraitis"
# would otherwise be two different lessons on two runs.
#
# Used by:
#   - _extract_teacher_from_html (above)
#   - _extract_room_from_html (above)
############################################################

def _joined_link_texts(soup) -> str:
    texts = {link.get_text(strip=True) for link in soup.find_all("a")}

    return ", ".join(sorted(text for text in texts if text))








############################################################
# _normalise_colour
############################################################
#
# One event colour in the single shape the retake table is
# written in: lowercase "#rrggbb". "#FF899D", "#ff899d",
# "#f9d" and "rgb(255, 137, 157)" all collapse to the same
# value, so a stylesheet that swaps notation cannot turn
# the exam filter off. Anything else (a named colour, a
# gradient, something new) comes back stripped and lowercased
# — never dropped, because the caller counts what it sees.
#
# Used by:
#   - scrape_group_schedule (below) — once per event
############################################################

def _normalise_colour(value) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""

    # STEP 1: rgb()/rgba() to the hex the table is written in
    # =======================================================
    rgb = re.match(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", text)
    if rgb:
        channels = [min(int(part), 255) for part in rgb.groups()]
        return "#{:02x}{:02x}{:02x}".format(*channels)


    # STEP 2: #rgb shorthand doubled, #rrggbb lowercased
    # ==================================================
    digits = text.lstrip("#")
    if re.fullmatch(r"[0-9a-f]{3}", digits):
        return "#" + "".join(digit * 2 for digit in digits)
    if re.fullmatch(r"[0-9a-f]{6}", digits):
        return "#" + digits

    # A named colour or something new — handed back as-is so it
    # still shows up in the histogram
    return text








############################################################
# _labelled_retake
############################################################
#
# True when the event SAYS it is a retake, whatever it is
# painted: the PERLAIKYMAS label in the subtitle, title or
# description, a structured type/category carrying the same
# word, or a boolean retake flag. Colour alone is a styling
# decision the site can change on any deploy — this is the
# half of the test that survives it.
#
# The title is scanned as it arrives, popover markup and
# all: an exam whose only marker is in its title
# ("Programavimas (PERLAIKYMAS)") would otherwise import as
# a WEEKLY lesson and show to students every week unless it
# also wore the retake colour — and it would defeat the
# palette-change warning, which only fires for a LABELLED
# retake.
#
# Used by:
#   - scrape_group_schedule (below) — once per event
############################################################

def _labelled_retake(event: dict) -> bool:
    if event.get("retake") is True:
        return True

    for key in ("subtitle", "title", "description", "type", "category", "eventType"):
        if "PERLAIKYM" in str(event.get(key, "")).upper():
            return True

    return False








############################################################
# _extract_title_text
############################################################
#
# The course title from the event's "title" field, which is
# either plain text or the popover markup: with markup the
# first <a>'s text wins, else the first line of the
# flattened text. Returns "" for an empty title, which
# scrape_group_schedule treats as "skip this event".
#
# Used by:
#   - scrape_group_schedule (below)
############################################################

def _extract_title_text(title_field: str) -> str:
    if "<" in title_field:
        soup = BeautifulSoup(title_field, "html.parser")
        link = soup.find("a")
        if link:
            return link.get_text(strip=True)
        return soup.get_text(strip=True).split("\n")[0].strip()
    return title_field.strip()








############################################################
# scrape_group_list
############################################################
#
# One GET of /knf/list/ → [{"slug", "display_name"}] for
# every distinct "/knf/groups/<slug>/" link on the page.
# The link text itself is useless ("1 Grupė",
# "Tvarkaraštis"), so the display name is reconstructed:
# climb up to five ancestors and take the nearest preceding
# h2/h3/h4/strong/b sibling, else the link's title
# attribute, else the de-hyphenated slug. When the name
# lacks "kursas" the course digit is appended from the
# slug's "Nk" token. Raises on HTTP failure — the caller
# marks the whole run failed.
#
# Used by:
#   - scrape_knf_schedule (below)
############################################################

def scrape_group_list() -> list[dict]:
    # STEP 1: fetch the faculty's group list page — a refused or
    # failed fetch raises, and the caller fails the whole run
    # ==========================================================
    result = fetch(GROUP_LIST_URL, SCHEDULE_HOSTS)
    if not result:
        raise RuntimeError(f"Could not fetch the group list at {GROUP_LIST_URL}")

    soup = BeautifulSoup(result[0], "html.parser")
    groups = []
    seen_slugs = set()


    # STEP 2: one entry per distinct /knf/groups/<slug>/ link,
    # with a display name reconstructed from the page context
    # ========================================================
    for link in soup.find_all("a", href=True):
        href = link["href"]
        match = re.match(r"^/knf/groups/([^/]+)/$", href)
        if not match:
            continue

        slug = match.group(1)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        # The slug is interpolated into the event feed URL, so a
        # scraped one that is not a plain path token is dropped
        # here rather than encoded and requested
        if not _SLUG_RE.match(slug):
            logger.warning("Skipping the malformed group slug %.80r", slug)
            continue

        # STEP 2.1: nearest preceding heading/bold, up to 5 levels up
        display_name = ""
        parent = link.parent
        for _ in range(5):
            if parent is None:
                break
            # previous_siblings walks nearest-first, so the closest
            # heading wins; NavigableStrings have name None and fall
            # through the tag-name check
            for sibling in parent.previous_siblings:
                if hasattr(sibling, 'name') and sibling.name in ('h2', 'h3', 'h4', 'strong', 'b'):
                    display_name = sibling.get_text(strip=True)
                    break
            if display_name:
                break
            parent = parent.parent

        # STEP 2.2: no heading found — the link's title attribute
        if not display_name and link.get("title"):
            display_name = link["title"]

        # STEP 2.3: append the course from the slug's "Nk" token
        # link_text is computed but never read (dead variable)
        link_text = link.get_text(strip=True)
        if display_name and "kursas" not in display_name.lower():
            course_match = re.search(r"(\d)k", slug)
            if course_match:
                display_name += f" - {course_match.group(1)} kursas"

        # STEP 2.4: still nothing — the de-hyphenated slug
        # _parse_group_display_name can parse a slug-shaped name
        # too, so this is a usable fallback, not a placeholder
        if not display_name:
            display_name = slug.replace("-", " ")

        groups.append({"slug": slug, "display_name": display_name})

    return groups








############################################################
# scrape_group_schedule
############################################################
#
# One GET of the group's FullCalendar feed for
# [start_date, end_date] (ISO dates), flattened to
# (lessons, stats): the weekly lesson dicts
# scrape_knf_schedule inserts, plus a count of everything
# dropped on the way and the colour histogram behind it.
# Dropped: all-day events (no "T" in start — holidays),
# retake exams, events whose dates don't parse, and events
# with an empty title. Lecturer and room come from the
# top-level "instructor"/"location" fields, falling back to
# the popover markup only when the title carries HTML.
#
# A retake is recognised by its NORMALISED colour (so
# "#FF899D", "#ff899d" and "rgb(255,137,157)" are one
# value), by a structured retake/type field, or by the
# PERLAIKYMAS label — and a labelled retake wearing an
# unknown colour logs a WARNING, which is what makes a
# palette change visible before it imports exams as weekly
# lessons.
#
# The slug is validated against _SLUG_RE and percent-encoded
# before it is interpolated into the feed URL; a malformed
# one raises and the caller skips the group.
#
# Every event keeps its DATE plus "HH:MM" times — one dict
# per dated occurrence, deduped only against the feed
# serving the same instance twice (the natural key, teacher
# excluded). A one-off room change is simply a different
# row on that one date, which is the whole point of the
# dated model. Raises on HTTP/JSON failure — the caller
# logs and skips the group.
#
# Used by:
#   - scrape_knf_schedule (below) — once per group
############################################################

def scrape_group_schedule(slug: str, group_display_name: str,
                          start_date: str, end_date: str):
    # STEP 1: the slug goes into a URL path — validated, then
    # percent-encoded anyway, belt and braces
    # =======================================================
    if not _SLUG_RE.match(slug or ""):
        raise ValueError(f"Refusing the malformed group slug {slug!r}")

    url = EVENT_URL_TEMPLATE.format(slug=quote(slug, safe=""))
    # The feed sometimes arrives declared as text/html, so both
    # content types are accepted; the body is JSON either way
    result = fetch(url, SCHEDULE_HOSTS,
                   params={"start": start_date, "end": end_date},
                   content_types=JSON_CONTENT_TYPES + HTML_CONTENT_TYPES)
    if not result:
        raise RuntimeError(f"Could not fetch the event feed for {slug}")

    data = json.loads(result[0])
    events = data.get("events", [])

    group_name = _parse_group_display_name(slug, group_display_name)

    lessons_seen = set()  # natural keys — a feed serving one event twice collapses here
    lessons = []
    # What the run threw away and why — a filter that silently
    # stops matching is otherwise indistinguishable from a
    # semester without retakes
    stats = {"events": len(events), "all_day": 0, "retakes": 0,
             "unparsable": 0, "untitled": 0, "colours": {}}


    # STEP 2: one weekly lesson dict per distinct event shape
    # =======================================================
    for event in events:
        # A NULL start is not the same as an absent one: `"T" not
        # in None` raises TypeError, which would escape the whole
        # function and cost the group every lesson it has. A null
        # collapses to "" and is dropped alone
        start_str = event.get("start") or ""
        end_str = event.get("end", "")

        # All-day events (holidays) come without a time component
        if "T" not in start_str:
            stats["all_day"] += 1
            continue

        # STEP 2.1: retake exams — the colour is normalised first
        # (case, #rgb shorthand and rgb() forms all collapse), and
        # the PERLAIKYMAS label is honoured whatever the palette
        colour = _normalise_colour(event.get("color", ""))
        if colour:
            stats["colours"][colour] = stats["colours"].get(colour, 0) + 1

        labelled_retake = _labelled_retake(event)
        if colour in _RETAKE_COLOURS or labelled_retake:
            stats["retakes"] += 1
            # The label without the colour is the palette change
            # this filter has to survive
            if labelled_retake and colour and colour not in _RETAKE_COLOURS:
                logger.warning("Retake event painted %s, not a known retake colour — "
                               "the palette has probably changed", colour)
            continue

        # An absent end ("" → ValueError) or a null one (TypeError)
        # drops the event as well
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
        except (ValueError, TypeError):
            stats["unparsable"] += 1
            continue

        time_start = start_dt.strftime("%H:%M")
        time_end = end_dt.strftime("%H:%M")
        # Labelled per event, not per run — see _get_semester_label
        semester = _get_semester_label(start_dt)

        # Null-safe for the same reason start is: _extract_title_
        # text tests `"<" in title_field`, which raises on None
        raw_title = event.get("title") or ""
        title = _extract_title_text(raw_title)
        if not title:
            stats["untitled"] += 1
            continue

        # Top-level fields first; the popover markup is only
        # consulted when the title actually carries HTML. A null
        # one becomes "" and NEVER None: both columns are part of
        # idx_schedule_events_natural, and SQLite counts NULLs
        # in a unique index as distinct — a NULL would re-insert
        # a lecturerless lesson on every single run
        teacher = event.get("instructor") or ""
        if not teacher and "<" in raw_title:
            teacher = _extract_teacher_from_html(raw_title)

        room = event.get("location") or ""
        if not room and "<" in raw_title:
            room = _extract_room_from_html(raw_title)

        # Only surrounding whitespace and a trailing comma are
        # trimmed — academic titles are kept
        if teacher:
            teacher = teacher.strip().rstrip(",").strip()

        # The DATED natural key — teacher is deliberately not in
        # it, matching idx_schedule_events_natural: a teacher swap
        # is the same event
        key = (start_dt.date(), time_start, time_end, title, "", room)
        if key in lessons_seen:
            continue
        lessons_seen.add(key)

        lessons.append({
            "title": title,
            "teacher": teacher,
            "room": room,
            "lecture_type": "",
            "date": start_dt.date(),
            "time_start": time_start,
            "time_end": time_end,
            "group_name": group_name,
            "slug": slug,
            "semester": semester,
        })

    return lessons, stats








############################################################
# scrape_knf_schedule
############################################################
#
# The full import: takes both halves of the source lock,
# opens a scraper_runs row, fetches the group list, scrapes
# every group over the network, then hands the dated dicts
# to _sync_schedule (insert-or-confirm plus the stale-
# future retire and retention), closes the run and pushes a
# "schedule" channel notification when something actually
# changed.
# Returns {"groups_scraped", "lessons_found", "lessons_new",
# "dropped"} — plus an "error" key and zero counts on
# failure (which is what lets the admin trigger answer a
# non-2xx), {"skipped": True} when another run holds the
# source, and it never raises (the run row is marked
# 'failed' with a message and a finish time instead).
#
# Two structural preconditions FAIL the run instead of
# completing it with zeros: an empty group list, and every
# group answering with no lesson at all. Both would
# otherwise look exactly like a quiet semester.
#
# Date window: rolling, [today - 2 weeks, today +
# forward_weeks]. Every event keeps the label
# _get_semester_label gives its own date; the run's ANCHOR
# (today's label) is always kept, and any other label seen
# fewer than MIN_SEMESTER_LESSONS times across the run is
# dropped as a stray instead of becoming a semester option.
#
# Writes happen once, after every fetch, inside ONE
# transaction.atomic() block — the insert-or-confirm pass
# and the stale-future retire land together or not at all
# (_sync_schedule). lessons_found sums every group's
# post-dedup dicts; lessons_new counts events that were NOT
# already there, so an unchanged timetable pushes nothing.
#
# A group that fails to scrape is logged and skipped, and
# because only groups in groups_ok may retire anything, its
# stored schedule is left alone rather than emptied. Only a
# failed group LIST fails the run.
#
# Used by:
#   - management/commands/scrape_schedule.py — every 6 h
#   - api/views.py — POST /api/scraper/schedule (admin)
############################################################

def scrape_knf_schedule(forward_weeks: int = WINDOW_FORWARD_WEEKS, notify: bool = True) -> dict:
    # STEP 1: one timetable run at a time — the admin trigger
    # steps aside when the 6 h job is still going, wherever
    # that job runs
    # =======================================================
    if not _RUN_LOCK.acquire(blocking=False):
        logger.info("Schedule scrape already running — this trigger is skipped")
        return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0, "skipped": True}

    try:
        run_id = open_run("tvarkarasciai.vu.lt", RUN_BUDGET_SECONDS)
        if run_id is None:
            return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0, "skipped": True}

        deadline = run_deadline(RUN_BUDGET_SECONDS)
        return _run(run_id, forward_weeks, notify, deadline)
    finally:
        _RUN_LOCK.release()


def _run(run_id, forward_weeks, notify, deadline):
    try:
        # STEP 2: the rolling window and the run's anchor semester
        # — no calendar guessing, so January sees the spring term
        # and the exam sessions are inside the window
        # ========================================================
        now = utc_now_naive()
        start = now - timedelta(weeks=WINDOW_BACK_WEEKS)
        end = now + timedelta(weeks=forward_weeks)
        start_date = start.strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")
        anchor_semester = _get_semester_label(now)

        logger.info("Schedule scrape: %s to %s (anchor %s)", start_date, end_date, anchor_semester)


        # STEP 3: fetch the group list — without it the run is
        # marked 'failed' and zeros are returned
        # =====================================================
        try:
            groups = scrape_group_list()
        except Exception as e:
            logger.exception("Failed to fetch group list")
            mark_run_failed(run_id, str(e))
            return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0,
                    "error": str(e), "runId": run_id}

        # A list page that downloaded and held no group link at
        # all is a template change, not an empty faculty — the
        # run fails instead of reporting a tidy zero
        if not groups:
            message = "no groups on the tvarkarasciai.vu.lt list page — the markup has probably changed"
            logger.error("Schedule scrape found no groups to scrape")
            mark_run_failed(run_id, message)
            return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0,
                    "error": message, "runId": run_id}

        logger.info("Found %d groups to scrape", len(groups))


        # STEP 4: scrape every group — network only, nothing is
        # written while a fetch is outstanding
        # =====================================================
        scraped: list = []
        groups_ok: dict = {}  # slug → {display_name, group_name} of successful fetches
        total_lessons = 0
        groups_scraped = 0
        # What every group's feed threw away, summed — the only
        # place a broken retake filter or a dead title selector
        # becomes visible
        dropped = {"all_day": 0, "retakes": 0, "unparsable": 0, "untitled": 0}
        colours: dict = {}

        for group in groups:
            if deadline_passed(deadline):
                logger.warning("Schedule scrape out of time after %d group(s)", groups_scraped)
                break

            slug = group["slug"]
            display_name = group["display_name"]

            # STEP 4.1: a failing group is logged and skipped, never fatal
            try:
                lessons, stats = scrape_group_schedule(slug, display_name, start_date, end_date)
            except Exception:
                logger.warning("Failed to scrape group %s", slug, exc_info=True)
                continue

            groups_scraped += 1
            total_lessons += len(lessons)

            # STEP 4.1.1: fold this group's drop counts into the run's
            for key in dropped:
                dropped[key] += stats[key]
            for colour, count in stats["colours"].items():
                colours[colour] = colours.get(colour, 0) + count

            # STEP 4.2: remember the healthy fetch — only feeds in
            # groups_ok may retire events and links in the write
            # phase; a failed or unfetched group's stored schedule
            # is preserved untouched
            groups_ok[slug] = {
                "display_name": display_name,
                "group_name": _parse_group_display_name(slug, display_name),
            }
            scraped.extend(lessons)


        # STEP 4.3: every group answered and not one lesson came
        # out of any of them — the feed shape changed. Fail the
        # run BEFORE the write phase, so nothing is reconciled
        # against an empty scrape
        # ======================================================
        if groups_scraped and total_lessons == 0:
            message = ("no lessons in any of %d group feed(s) — the feed shape has probably changed"
                       % groups_scraped)
            logger.error("Schedule scrape harvested nothing from %d group(s); dropped=%s",
                         groups_scraped, dropped)
            mark_run_failed(run_id, message)
            return {"groups_scraped": groups_scraped, "lessons_found": 0, "lessons_new": 0,
                    "error": message, "runId": run_id}

        # One line per run with everything the filters removed and
        # the colours behind it — a retake filter that stops
        # matching shows up as retakes dropping to zero
        logger.info("Schedule scrape filters: dropped=%s, colours=%s", dropped, colours)


        # STEP 5: drop stray semester labels — a handful of
        # misdated events must never become a picker option
        # =================================================
        per_semester = {}
        for lesson in scraped:
            per_semester[lesson["semester"]] = per_semester.get(lesson["semester"], 0) + 1

        kept_semesters = {
            semester for semester, count in per_semester.items()
            if count >= MIN_SEMESTER_LESSONS or semester == anchor_semester
        }
        for semester, count in per_semester.items():
            if semester not in kept_semesters:
                logger.info("Dropping stray semester label %s (%d event(s) this run)", semester, count)

        scraped = [lesson for lesson in scraped if lesson["semester"] in kept_semesters]


        # STEP 6: the write phase — insert-or-confirm every dated
        # event with the run's stamp, then retire what the healthy
        # feeds stopped serving; one transaction covers everything
        # ========================================================
        run_stamp = utc_now()

        with transaction.atomic():
            total_new, total_removed = _sync_schedule(scraped, groups_ok, run_stamp)


        # STEP 8: close the run row — the lesson counts go into
        # the articles_found / articles_new columns — and prune
        # =====================================================
        close_run(run_id, total_lessons, total_new)

        # A run that still 'completed' but harvested a tenth of
        # what the last one did gets its own ERROR line
        check_yield_drop("tvarkarasciai.vu.lt", total_lessons, run_id)

        prune_scraper_runs()

        result = {
            "groups_scraped": groups_scraped,
            "lessons_found": total_lessons,
            "lessons_new": total_new,
            # Additive, admin-only: what the filters removed
            "dropped": dropped,
        }
        logger.info("Schedule scrape complete: %s (%d row(s) retired)", result, total_removed)


        # STEP 9: push to the "schedule" channel when anything new
        # landed — per-user opt-out lives in notification_channels,
        # the admin trigger passes notify=False, and push_allowed
        # refuses a first import, a burst and an hourly repeat
        # =========================================================
        if total_new > 0 and notify and push_allowed("tvarkarasciai.vu.lt", total_new, run_id):
            try:
                from knfapp.notifications.push import notify_channel
                from knfapp.scraper.plurals import lt_plural
                title = "Tvarkaraščio pakeitimai"
                # lt_plural picks the declined form — 21 is
                # singular again, 10 takes the genitive
                phrase = lt_plural(total_new, ("naujas įrašas", "nauji įrašai", "naujų įrašų"))
                body = "Naujas įrašas tvarkaraštyje" if total_new == 1 else f"{total_new} {phrase} tvarkaraštyje"
                title_en = "Timetable changes"
                body_en = "New timetable entry" if total_new == 1 else f"{total_new} new timetable entries"
                notify_channel("schedule", title, body, data={"type": "schedule_update", "newLessons": total_new},
                               title_en=title_en, body_en=body_en)
            except Exception:
                # A push failure never fails the (already completed)
                # run
                logger.exception("Failed to send push notification for schedule changes")

        return result

    except Exception as e:
        # Anything past the group-list fetch: the atomic block has
        # already rolled the half-written reconciliation back; the
        # run row is closed through a freshened connection — never
        # raise to the command or the admin route
        logger.exception("Schedule scrape failed")
        mark_run_failed(run_id, str(e))
        return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0,
                "error": str(e), "runId": run_id}






############################################################
# _upsert_event
############################################################
#
# Insert-or-confirm ONE dated event: a conflict-ignoring
# INSERT on idx_schedule_events_natural (the spelling runs
# on SQLite and Postgres alike), then an unconditional
# confirmation UPDATE — teacher and semester are refreshed
# and last_seen_at takes the run's stamp whether the row is
# new or a year old. Answers (event_id, created); the
# SELECT instead of RETURNING keeps the SQL portable.
#
# Used by:
#   - _sync_schedule (below) — once per merged event
############################################################

def _upsert_event(cursor, event: dict, run_stamp):
    key = (event["date"], event["time_start"], event["time_end"],
           event["title"], event["lecture_type"], event["room"])
    # Raw SQL must store stamps in the exact form the ORM
    # writes and compares — on SQLite a bare datetime binding
    # keeps its "+00:00" suffix and breaks every <=> filter
    run_stamp = connection.ops.adapt_datetimefield_value(run_stamp)

    cursor.execute(
        """INSERT INTO schedule_events
           (id, title, lecture_type, teacher, room, date, time_start,
            time_end, semester, last_seen_at, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (date, time_start, time_end, title, lecture_type, room)
           DO NOTHING""",
        (str(uuid.uuid4()), event["title"], event["lecture_type"], event["teacher"],
         event["room"], event["date"], event["time_start"], event["time_end"],
         event["semester"], run_stamp, run_stamp),
    )
    created = cursor.rowcount > 0

    cursor.execute(
        """UPDATE schedule_events SET teacher = %s, semester = %s, last_seen_at = %s
           WHERE date = %s AND time_start = %s AND time_end = %s
             AND title = %s AND lecture_type = %s AND room = %s""",
        (event["teacher"], event["semester"], run_stamp, *key),
    )
    cursor.execute(
        """SELECT id FROM schedule_events
           WHERE date = %s AND time_start = %s AND time_end = %s
             AND title = %s AND lecture_type = %s AND room = %s""",
        key,
    )
    return cursor.fetchone()[0], created








############################################################
# _sync_schedule
############################################################
#
# The whole write phase, called inside one
# transaction.atomic(). Merges the per-group dicts on the
# natural key (the same lecture reached through two feeds
# becomes ONE event with two group links), inserts-or-
# confirms events, teachers and links with the run's stamp,
# then retires what the healthy feeds stopped serving:
#
#   - a stale link whose GROUP answered this run is deleted
#     alone — the lecture moved out of that feed; a failed
#     or unfetched group's links (and through them its
#     events) are never touched
#   - a FUTURE event left with no group links is deleted —
#     every feed that could claim it dropped it
#   - the past is history: never retired by staleness, only
#     by RETENTION_DAYS, together with link-less teachers
#     and groups nothing references any more
#
# Teacher links are regenerated from the event's teacher
# string on every confirmation, so a swap replaces the link
# instead of accumulating both names. Answers (added,
# removed): rows inserted and future events retired — an
# unchanged timetable answers (0, 0) and pushes nothing.
#
# Used by:
#   - scrape_knf_schedule (above) — STEP 6
############################################################

def _sync_schedule(scraped: list, groups_ok: dict, run_stamp):
    today = run_stamp.date()

    # STEP 1: upsert the groups whose feeds answered
    # ==============================================
    for slug, info in groups_ok.items():
        ScheduleGroup.objects.update_or_create(
            slug=slug,
            defaults={"display_name": info["display_name"],
                      "group_name": info["group_name"],
                      "last_seen_at": run_stamp},
        )


    # STEP 2: merge the per-group dicts on the natural key
    # ====================================================
    merged: dict = {}
    for lesson in scraped:
        key = (lesson["date"], lesson["time_start"], lesson["time_end"],
               lesson["title"], lesson["lecture_type"], lesson["room"])
        entry = merged.get(key)
        if entry is None:
            entry = merged[key] = {**lesson, "slugs": set()}
        entry["slugs"].add(lesson["slug"])
        # The first non-empty teacher wins — feeds rarely disagree,
        # and '' must never overwrite a name
        if not entry["teacher"] and lesson["teacher"]:
            entry["teacher"] = lesson["teacher"]


    # STEP 3: insert-or-confirm events, teachers and links
    # ====================================================
    added = 0
    teacher_ids: dict = {}
    confirmed_ids: list = []
    with connection.cursor() as cursor:
        for event in merged.values():
            event_id, created = _upsert_event(cursor, event, run_stamp)
            confirmed_ids.append(event_id)
            if created:
                added += 1

            for slug in event["slugs"]:
                ScheduleEventGroup.objects.update_or_create(
                    event_id=event_id, group_id=slug,
                    defaults={"last_seen_at": run_stamp},
                )

            name = event["teacher"]
            if name:
                teacher_id = teacher_ids.get(name)
                if teacher_id is None:
                    teacher, _ = ScheduleTeacher.objects.get_or_create(
                        name=name,
                        defaults={"id": str(uuid.uuid4()), "last_seen_at": run_stamp},
                    )
                    ScheduleTeacher.objects.filter(pk=teacher.pk).update(last_seen_at=run_stamp)
                    teacher_id = teacher_ids[name] = teacher.pk
                ScheduleEventTeacher.objects.update_or_create(
                    event_id=event_id, teacher_id=teacher_id,
                    defaults={"last_seen_at": run_stamp},
                )


    # STEP 4: retire what the healthy feeds stopped serving —
    # future only; the past is outside every feed's window and
    # its links are history, not staleness. Confirmed ids are
    # chunked: SQLite caps bound parameters per statement
    # ========================================================
    for i in range(0, len(confirmed_ids), 500):
        ScheduleEventTeacher.objects.filter(
            event_id__in=confirmed_ids[i:i + 500], last_seen_at__lt=run_stamp,
        ).delete()

    ScheduleEventGroup.objects.filter(
        event__date__gte=today, last_seen_at__lt=run_stamp,
        group_id__in=groups_ok.keys(),
    ).delete()

    _, by_model = ScheduleEvent.objects.filter(
        date__gte=today, last_seen_at__lt=run_stamp,
        scheduleeventgroup__isnull=True,
    ).delete()
    removed = by_model.get("schedule.ScheduleEvent", 0)


    # STEP 5: retention — the past goes at RETENTION_DAYS, and
    # entities nothing links to any more go with it
    # ========================================================
    horizon = run_stamp - timedelta(days=RETENTION_DAYS)
    ScheduleEvent.objects.filter(date__lt=today - timedelta(days=RETENTION_DAYS)).delete()
    ScheduleTeacher.objects.filter(scheduleeventteacher__isnull=True,
                                   last_seen_at__lt=horizon).delete()
    ScheduleGroup.objects.filter(scheduleeventgroup__isnull=True,
                                 last_seen_at__lt=horizon).delete()

    return added, removed




############################################################
# _semester_key
############################################################
#
# Sort key for a semester label, or None when the label is
# not one this scraper writes. Plain text sorting is WRONG
# here: "2025-P" (spring 2026) sorts before "2025-R"
# (autumn 2025) although it comes after it in the academic
# year, so autumn is 0 and spring 1 within the label year.
# A label in any other shape ("2025-pavasaris", anything
# hand-typed) returns None and is left to text order.
#
# Used by:
#   - schedule/api/views.py — _semester_options, so the
#     newest-semester default flips to spring in January
#     instead of clinging to the autumn label all year
############################################################

def _semester_key(label: str):
    match = re.fullmatch(r"(\d{4})-([RP])", label or "")
    if not match:
        return None

    return int(match.group(1)) * 2 + (0 if match.group(2) == "R" else 1)
