############################################################
#  [*] Schedule scraper — tvarkarasciai.vu.lt timetable
#
#  Pulls every KNF group's FullCalendar event feed from
#  tvarkarasciai.vu.lt and folds the dated events into
#  schedule_lessons as WEEKLY patterns: a lesson's identity
#  is title|teacher|room|time|weekday|group|semester, so the
#  same lecture week after week becomes ONE row, and a
#  one-off room change would become a second row the app
#  then shows every week. That is why the import is a
#  RECONCILIATION, not an append: for the semester the run
#  is about, each (group_name, semester) partition is
#  rewritten from the freshly scraped set, so a moved
#  lecture, a cancelled one and last week's phantom room all
#  disappear on the next run. Neighbouring semesters caught
#  by the window are only ever added to (INSERT OR IGNORE) —
#  the window covers a fortnight of them, which is not
#  enough to rebuild them from.
#
#  The window is rolling — [today - 2 weeks, today + 20
#  weeks] — instead of "this semester from its first day":
#  a January run used to re-import the finished autumn
#  semester and miss the spring one for a month, and the
#  16-week cutoff dropped the January and June exam
#  sessions entirely.
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
#  become a semester option in the mobile picker, and
#  semesters older than the run's anchor are purged once the
#  anchor itself has rows.
#
#  Every run is logged in scraper_runs (source
#  'tvarkarasciai.vu.lt') with the lesson counts in the
#  articles_found / articles_new columns the news scrapers
#  named, and prunes run rows older than 30 days.
#  scheduler.py calls in 30 s after boot and every 6 h,
#  scraper/routes.py exposes an admin trigger, and the rows
#  flow on to schedule/routes.py → the mobile schedule tab
#  (services/api/schedule.ts).
############################################################


import hashlib
import html
import json
import logging
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

from bs4 import BeautifulSoup

from app.database import get_db, utc_now_iso
from app.scraper.common import (
    HTML_CONTENT_TYPES,
    JSON_CONTENT_TYPES,
    SCHEDULE_HOSTS,
    check_yield_drop,
    deadline_passed,
    fetch,
    mark_run_failed,
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

# Dead constant: nothing reads it — _get_semester_label
# hard-codes `month >= 8` as the autumn cutoff instead
_SEMESTER_MONTH_CUTOFF = 7  # Aug-Dec = autumn, Jan-Jul = spring

# The rolling window, in weeks either side of today: two
# back so a just-finished week can still be corrected,
# twenty forward to cover the rest of the semester and the
# exam session behind it
WINDOW_BACK_WEEKS = 2
WINDOW_FORWARD_WEEKS = 20

# A semester label the whole run saw fewer times than this
# is a stray (a single misdated event) and is not stored —
# it would otherwise show up as an option in the mobile
# semester picker
MIN_SEMESTER_LESSONS = 5

# Wall-clock budget: ~100 group feeds fit comfortably, and
# the 6 h tick is never at risk of being skipped
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

# An EXPLICIT English-taught marker. The old test was the
# bare substring "angl", which also fires on the Lithuanian
# programme "Anglų ir kita užsienio kalba" — every one of its
# groups was tagged "-EN"
_LANG_EN_RE = re.compile(
    r"angl\w*\s+(?:kalb\w*|k\.)|in\s+english|english[-\s]taught|(?:^|[-\s(])en(?:[-\s)]|$)",
    re.IGNORECASE,
)

# One timetable run at a time, whoever asked for it
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
# "LFR" it used to collapse into — that collision silently
# merged two real timetables.
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
# _lesson_hash
############################################################
#
# 16 hex chars of SHA-256 over the eight identity fields
# joined with "|". Only an in-memory dedup key inside one
# group's scrape — it is never stored; the DB-side "already
# exists" check in scrape_knf_schedule compares the same
# eight columns directly.
#
# Used by:
#   - scrape_group_schedule (below)
############################################################

def _lesson_hash(title: str, teacher: str, room: str, time_start: str,
                 time_end: str, day_of_week: int, group_name: str, semester: str) -> str:
    key = f"{title}|{teacher}|{room}|{time_start}|{time_end}|{day_of_week}|{group_name}|{semester}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]








############################################################
# _extract_teacher_from_html
############################################################
#
# The lecturers from an event title that carries the site's
# popover markup: the data-academics attribute holds
# HTML-escaped HTML, so it is regex-lifted, unescaped and
# parsed again. EVERY <a> is kept (sorted, joined with
# ", ") — keeping only the first is how a co-taught lecture
# used to lose a name, while the feed's own top-level
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
# value, so a stylesheet that swaps notation no longer turns
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
# ("Programavimas (PERLAIKYMAS)") used to be imported as a
# WEEKLY lesson and shown to students every week unless it
# also wore the retake colour — and it defeated the
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
# Every event collapses to its weekday + "HH:MM" times, so
# the dated occurrences of one lecture dedupe to a single
# dict via _lesson_hash, while a one-week room change
# survives as a separate dict. day_of_week is
# datetime.weekday(): 0 = Monday … 6 = Sunday, the same
# convention /api/schedule and the mobile app use. Raises
# on HTTP/JSON failure — the caller logs and skips the
# group.
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
    # The feed has been served as text/html before now, so both
    # content types are accepted; the body is JSON either way
    result = fetch(url, SCHEDULE_HOSTS,
                   params={"start": start_date, "end": end_date},
                   content_types=JSON_CONTENT_TYPES + HTML_CONTENT_TYPES)
    if not result:
        raise RuntimeError(f"Could not fetch the event feed for {slug}")

    data = json.loads(result[0])
    events = data.get("events", [])

    group_name = _parse_group_display_name(slug, group_display_name)

    lessons_seen = set()  # _lesson_hash keys — weekly recurrences collapse here
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
        # in None` raises TypeError, and that escaped the whole
        # function — one malformed event dropped every lesson the
        # group had. A null collapses to "" and is dropped alone
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

        day_of_week = start_dt.weekday()  # 0=Mon, 6=Sun -- matches our API
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
        # idx_schedule_lessons_natural, and SQLite counts NULLs
        # in a unique index as distinct — a lesson with no
        # lecturer used to be re-inserted on every single run
        teacher = event.get("instructor") or ""
        if not teacher and "<" in raw_title:
            teacher = _extract_teacher_from_html(raw_title)

        room = event.get("location") or ""
        if not room and "<" in raw_title:
            room = _extract_room_from_html(raw_title)

        # Only surrounding whitespace and a trailing comma are
        # trimmed — no academic titles are removed, whatever the
        # original comment promised
        if teacher:
            teacher = teacher.strip().rstrip(",").strip()

        # Same weekday/time/group/semester = the same weekly lesson
        h = _lesson_hash(title, teacher, room, time_start, time_end,
                         day_of_week, group_name, semester)
        if h in lessons_seen:
            continue
        lessons_seen.add(h)

        lessons.append({
            "title": title,
            "teacher": teacher,
            "room": room,
            "time_start": time_start,
            "time_end": time_end,
            "day_of_week": day_of_week,
            "group_name": group_name,
            "semester": semester,
        })

    return lessons, stats








############################################################
# scrape_knf_schedule
############################################################
#
# The full import: takes the source lock, opens a
# scraper_runs row, fetches the group list, scrapes every
# group over the network, then RECONCILES the result into
# schedule_lessons, purges semesters older than the run's
# anchor, closes the run and pushes a "schedule" channel
# notification when something actually changed. Returns
# {"groups_scraped", "lessons_found", "lessons_new",
# "dropped"} — plus an "error" key and zero counts on
# failure (which is what lets the admin trigger answer a
# non-2xx), {"skipped": True} when another run holds the
# lock, and it never raises (the run row is marked 'failed'
# with a message and a finish time instead).
#
# Two structural preconditions FAIL the run instead of
# completing it with zeros: an empty group list, and every
# group answering with no lesson at all. Both used to look
# exactly like a quiet semester.
#
# Date window: rolling, [today - 2 weeks, today +
# forward_weeks]. Every event keeps the label
# _get_semester_label gives its own date; the run's ANCHOR
# is today's label, and only anchor partitions are
# rewritten. A label seen fewer than MIN_SEMESTER_LESSONS
# times across the run is dropped as a stray instead of
# becoming a semester option.
#
# Writes happen once, after every fetch: for each anchor
# (group_name, semester) partition the old rows are deleted
# and the scraped set inserted, so a moved lecture or a
# one-week room change stops accumulating phantom weekly
# rows; other semesters get INSERT OR IGNORE against
# idx_schedule_lessons_natural. lessons_found sums every
# group's post-dedup dicts; lessons_new counts rows that
# were NOT already there, so an unchanged timetable pushes
# nothing.
#
# A group that fails to scrape is logged and skipped, and
# its partition is left alone rather than emptied. Only a
# failed group LIST fails the run. The push's data payload
# (type "schedule_update") is not routed by the mobile tap
# listener at the moment — a tap just opens the app.
#
# Used by:
#   - scraper/scheduler.py — 30 s after start-up, then
#     every 6 h
#   - scraper/routes.py — POST /api/scraper/schedule
#     (admin trigger)
############################################################

def scrape_knf_schedule(forward_weeks: int = WINDOW_FORWARD_WEEKS, notify: bool = True) -> dict:
    # STEP 1: one timetable run at a time — the admin trigger
    # steps aside when the 6 h job is still going
    # =======================================================
    if not _RUN_LOCK.acquire(blocking=False):
        logger.info("Schedule scrape already running — this trigger is skipped")
        return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0, "skipped": True}

    run_id = str(uuid.uuid4())
    db = get_db()
    deadline = run_deadline(RUN_BUDGET_SECONDS)

    try:
        db.execute(
            "INSERT INTO scraper_runs (id, source, status, started_at) VALUES (?, 'tvarkarasciai.vu.lt', 'running', ?)",
            (run_id, utc_now_iso()),
        )
        db.commit()


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
        scraped: dict = {}
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

            # STEP 4.2: file each lesson under its (group, semester)
            # partition — the unit the write phase reconciles
            for lesson in lessons:
                partition = (lesson["group_name"], lesson["semester"])
                scraped.setdefault(partition, []).append(lesson)


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
        for (_group_name, semester), lessons in scraped.items():
            per_semester[semester] = per_semester.get(semester, 0) + len(lessons)

        kept_semesters = {
            semester for semester, count in per_semester.items()
            if count >= MIN_SEMESTER_LESSONS or semester == anchor_semester
        }
        for semester, count in per_semester.items():
            if semester not in kept_semesters:
                logger.info("Dropping stray semester label %s (%d event(s) this run)", semester, count)


        # STEP 6: the write phase — anchor partitions are
        # rewritten from the scraped set, the neighbouring
        # semesters the window clipped are only added to
        # ===============================================
        total_new = 0
        total_removed = 0

        for (group_name, semester), lessons in scraped.items():
            if semester not in kept_semesters:
                continue

            if semester == anchor_semester:
                added, removed = _reconcile_partition(db, group_name, semester, lessons)
                total_new += added
                total_removed += removed
            else:
                total_new += _insert_lessons(db, lessons)

        db.commit()


        # STEP 7: retire semesters older than the anchor, but only
        # once the anchor itself actually has rows — an empty
        # scrape must never empty the app
        # ========================================================
        if anchor_semester in kept_semesters:
            _purge_old_semesters(db, anchor_semester)


        # STEP 8: close the run row — the lesson counts go into
        # the articles_found / articles_new columns — and prune
        # =====================================================
        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?,
                   finished_at = ?
               WHERE id = ?""",
            (total_lessons, total_new, utc_now_iso(), run_id),
        )
        db.commit()

        # A run that still 'completed' but harvested a tenth of
        # what the last one did gets its own ERROR line
        check_yield_drop(db, "tvarkarasciai.vu.lt", total_lessons, run_id)

        prune_scraper_runs(db)

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
        if total_new > 0 and notify and push_allowed(db, "tvarkarasciai.vu.lt", total_new, run_id):
            try:
                # Imported lazily, as the news scrapers do
                from app.notifications.push import notify_channel
                from app.scraper.plurals import lt_plural
                title = "Tvarkara\u0161\u010dio pakeitimai"
                # lt_plural picks the declined form — 21 is
                # singular again, 10 takes the genitive
                phrase = lt_plural(total_new, ("naujas \u012fra\u0161as", "nauji \u012fra\u0161ai", "nauj\u0173 \u012fra\u0161\u0173"))
                body = "Naujas \u012fra\u0161as tvarkara\u0161tyje" if total_new == 1 else f"{total_new} {phrase} tvarkara\u0161tyje"
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
        # Anything past the group-list fetch: roll the half-written
        # reconciliation back, mark the run failed WITH a finish
        # time on a FRESH connection, and return zeros — never
        # raise to the scheduler or the admin route
        logger.exception("Schedule scrape failed")
        try:
            db.rollback()
        except sqlite3.Error:
            logger.warning("Rollback after the schedule failure did not take", exc_info=True)
        mark_run_failed(run_id, str(e))
        return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0,
                "error": str(e), "runId": run_id}
    finally:
        db.close()
        _RUN_LOCK.release()








############################################################
# _reconcile_partition
############################################################
#
# Rewrites one (group_name, semester) partition to exactly
# the lessons just scraped and answers (added, removed).
# DELETE + INSERT rather than an upsert because the natural
# key IS the whole row: there is nothing to update, only
# rows to gain and rows to lose. The counts are taken by
# comparing the two sets BEFORE the write, so an unchanged
# timetable reports zero and sends no push — a plain "rows
# inserted" count would report the whole partition as new on
# every run.
#
# Parallel subgroups legitimately share a slot with a
# different teacher or room, so the comparison key is the
# full six-column identity, never the time slot alone.
#
# Used by:
#   - scrape_knf_schedule (above) — the anchor semester's
#     partitions
############################################################

def _reconcile_partition(db, group_name: str, semester: str, lessons: list[dict]):
    # STEP 1: what the table holds for this partition today
    # =====================================================
    existing = {
        (row["title"], row["teacher"], row["room"],
         row["time_start"], row["time_end"], row["day_of_week"])
        for row in db.execute(
            """SELECT title, teacher, room, time_start, time_end, day_of_week
               FROM schedule_lessons WHERE group_name = ? AND semester = ?""",
            (group_name, semester),
        ).fetchall()
    }

    scraped = {
        (lesson["title"], lesson["teacher"], lesson["room"],
         lesson["time_start"], lesson["time_end"], lesson["day_of_week"])
        for lesson in lessons
    }


    # STEP 2: replace the partition — the delete is what makes a
    # one-week room change stop haunting every following week
    # ==========================================================
    if existing != scraped:
        db.execute(
            "DELETE FROM schedule_lessons WHERE group_name = ? AND semester = ?",
            (group_name, semester),
        )
        _insert_lessons(db, lessons)

    return len(scraped - existing), len(existing - scraped)








############################################################
# _insert_lessons
############################################################
#
# Inserts a batch of scraped lesson dicts and returns how
# many rows were actually added. INSERT OR IGNORE leans on
# idx_schedule_lessons_natural (migration v18) instead of a
# SELECT per candidate: the old check-then-insert was a full
# table scan each time and still lost to a parallel run.
#
# Used by:
#   - _reconcile_partition (above) — after the delete
#   - scrape_knf_schedule (above) — the semesters the window
#     only clipped, which are added to and never rewritten
############################################################

def _insert_lessons(db, lessons: list[dict]) -> int:
    added = 0

    for lesson in lessons:
        cursor = db.execute(
            """INSERT OR IGNORE INTO schedule_lessons
               (id, title, teacher, room, time_start, time_end,
                day_of_week, group_name, semester)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), lesson["title"], lesson["teacher"],
             lesson["room"], lesson["time_start"], lesson["time_end"],
             lesson["day_of_week"], lesson["group_name"], lesson["semester"]),
        )
        added += cursor.rowcount

    return added








############################################################
# _semester_key
############################################################
#
# Sort key for a semester label, or None when the label is
# not one this scraper writes. Plain text sorting is WRONG
# here: "2025-P" (spring 2026) sorts before "2025-R"
# (autumn 2025) although it comes after it in the academic
# year, so autumn is 0 and spring 1 within the year. The
# legacy "2025-pavasaris" seed shape and anything hand-typed
# return None and are left alone by the purge.
#
# Used by:
#   - _purge_old_semesters (below)
############################################################

def _semester_key(label: str):
    match = re.fullmatch(r"(\d{4})-([RP])", label or "")
    if not match:
        return None

    return int(match.group(1)) * 2 + (0 if match.group(2) == "R" else 1)








############################################################
# _purge_old_semesters
############################################################
#
# Deletes schedule_lessons rows whose semester is older than
# the run's anchor, so the picker does not grow a longer
# list of dead semesters every year. Only labels this
# scraper wrote are eligible (_semester_key) — anything else
# is left for an admin. Runs only after the anchor semester
# itself has rows, so a scrape that fetched nothing can
# never empty the timetable.
#
# Used by:
#   - scrape_knf_schedule (above) — after the write phase
############################################################

def _purge_old_semesters(db, anchor_semester: str):
    anchor_key = _semester_key(anchor_semester)
    if anchor_key is None:
        return

    stale = []
    for row in db.execute(
        "SELECT DISTINCT semester FROM schedule_lessons WHERE semester IS NOT NULL"
    ).fetchall():
        key = _semester_key(row["semester"])
        if key is not None and key < anchor_key:
            stale.append(row["semester"])

    for semester in stale:
        removed = db.execute(
            "DELETE FROM schedule_lessons WHERE semester = ?", (semester,)
        ).rowcount
        logger.info("Retired %d lesson(s) from the finished semester %s", removed, semester)

    if stale:
        db.commit()
