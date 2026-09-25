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
#  The event TYPE is the site's own word — the popover's
#  "Tipas: Egzaminas, Privalomasis" (Paskaita, Pratybos,
#  Egzaminas, …), else the "(EGZAMINAS)" marker printed
#  after the subject link, else the feed's colour legend —
#  and the "Pogrupiai: N" subgroups ride with it. Both live
#  in schedule_events.lecture_type as "Kind|1,2" (see
#  join_lecture_type): the column is already the natural
#  key's fifth part, so no migration was needed, and the
#  read side splits it back into two wire fields. The type
#  is an ATTRIBUTE of the session, never its identity: one
#  physical slot (date, times, title, room) is one event
#  however the programmes' feeds label it ("Paskaita" in
#  one, "Paskaitos ir seminarai" in another — _pick_kind
#  settles it, an exam first). A slot's stored row is
#  relabelled IN PLACE when its type or subgroups move, and
#  a superseded copy on a slot the run confirmed is deleted
#  even in the past, so no typing change can re-create
#  events (new ids, a spurious "new lectures" push) or leave
#  the fortnight of history the window re-reads doubled.
#
#  Every run is logged in scraper_runs (source
#  'tvarkarasciai.vu.lt') with the lesson counts in the
#  articles_found / articles_new columns the news scrapers
#  named, and prunes run rows older than 30 days. A run
#  that completed with group feeds it could not read (a
#  dead feed, a body over the byte cap — refused whole,
#  since half a JSON document is no timetable) names them
#  in error_message, so a feed frozen at its last good copy
#  shows on /api/scraper/status instead of in a log line.
#  The cron command runs it every 6 h, api/views.py exposes
#  an admin trigger, and the rows flow on to the schedule
#  views → the mobile schedule tab.
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
from knfapp.schedule.kinds import is_badge_kind
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
# "rgb(255, 137, 157)" are the same value. A feed whose own
# event_colors legend names a retake colour adds it for that
# feed (see _read_legend)
_RETAKE_COLOURS = frozenset({"#ff899d"})

# The largest live feed body is ~1.6 MB (a 22-week window of
# ~900 events, ~1.9 KB of popover markup each) — the shared
# 2 MB default left it 21% of headroom, and a JSON body cut
# at the cap is unparsable anyway. The cap still exists: it
# stops a runaway body, just not a growing semester
FEED_MAX_BYTES = 8_000_000

# The event_colors legend labels every feed may carry that
# mean "an ordinary event" — no type to learn from them.
# Compared casefolded
_REGULAR_LEGEND_LABELS = frozenset({"įprastas įvykis"})

# The legend labels the scraper understands — the regular
# one plus the event types it has seen painted. Anything
# else is a palette/legend change worth a WARNING, with the
# colour and the label named (KNF-156: a zero retake count
# alone carries no information). Compared casefolded
_KNOWN_LEGEND_LABELS = _REGULAR_LEGEND_LABELS | frozenset({
    "egzaminas", "perlaikymas", "atsiskaitymas", "konsultacija",
    "kolokviumas", "koliokviumas", "įskaita",
})

# The parenthesised type marker the site prints after the
# subject link — "(EGZAMINAS)", "(PERLAIKYMAS)" — upper-case
# letters only, so a room like "(knf)" never reads as one
_KIND_MARKER_RE = re.compile(r"\(\s*([A-ZĄČĘĖĮŠŲŪŽ][A-ZĄČĘĖĮŠŲŪŽ ]{2,}?)\s*\)")

# "Pogrupiai: 1" / "Pogrupiai: 1, 2" — the tokens after the
# label, up to the line's end
_SUBGROUPS_RE = re.compile(r"Pogrupi\w*\s*:\s*([^\n<]*)", re.IGNORECASE)

# The one character a type or subgroup never carries — the
# separator inside lecture_type (see join_lecture_type)
LECTURE_TYPE_SEPARATOR = "|"

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
# would merge into one timetable) and falls back to the WHOLE
# slug, which is unique, with the slug logged for review. The
# fallback used to be capped at 30 characters, and the site's
# newer slugs ("…-angl-29" … "-32") differ only past that
# point — the cap merged four course-years' feeds into one
# name, and group_name is an unconstrained text column, so
# it bought nothing.
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
                return slug

            # An explicit language marker only — plain "angl"
            # also matches the AKUK programme's own name
            lang_suffix = "-EN" if _LANG_EN_RE.search(name_lower) or _LANG_EN_RE.search(slug) else ""

            level_suffix = "-M" if "magistrant" in name_lower else ""

            return f"{abbrev}{level_suffix}{lang_suffix}-{course}" if course else f"{abbrev}{level_suffix}{lang_suffix}"

    # No programme matched — the raw slug, whole
    logger.info("No programme matched the group '%s' (slug %s) — keeping the slug as its name",
                display_name, slug)
    return slug







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
# _parse_title
############################################################
#
# The event's "title" field → (subject, kind, subgroups).
# The field is either plain text or the site's popover
# markup:
#
#   <a data-showed_type="…Tipas: Egzaminas, Privalomasis…"
#      data-subgroups="…Pogrupiai: 1…" …>Subject</a>
#   (EGZAMINAS) Pogrupiai: 1<br>II k. kl. (knf)<br>
#
# The SUBJECT is the first <a>'s text (else the first line
# of the flattened text) — "" means "skip this event". The
# KIND is the first comma part of data-showed_type ("…,
# Privalomasis" is the course status, not the type), else
# the parenthesised marker in the text after the link,
# capitalised ("EGZAMINAS" → "Egzaminas"). The SUBGROUPS
# come from data-subgroups, else the "Pogrupiai:" text
# after the link. Everything after the first <br> is the
# room line and is never read here — a room "(knf)" must
# not pass for a marker.
#
# Used by:
#   - scrape_group_schedule (below)
############################################################

def _parse_title(title_field: str):
    if "<" not in title_field:
        return title_field.strip(), "", []

    soup = BeautifulSoup(title_field, "html.parser")
    link = soup.find("a")
    if not link:
        return soup.get_text(strip=True).split("\n")[0].strip(), "", []

    title = link.get_text(strip=True)


    # STEP 1: the structured attributes — BeautifulSoup hands
    # them back entity-decoded, as markup of their own
    # =======================================================
    kind = _labelled_value(link.get("data-showed_type", ""), "Tipas").split(",")[0].strip()
    subgroups = _split_subgroups(_labelled_value(link.get("data-subgroups", ""), "Pogrupiai"))


    # STEP 2: the visible text between the link and the first
    # <br> — the marker and the "Pogrupiai:" line live there
    # =======================================================
    tail = []
    for node in link.next_siblings:
        name = getattr(node, "name", None)
        if name == "br":
            break
        # A tag contributes its text, a bare string itself
        tail.append(node.get_text(" ") if name else str(node))
    tail_text = " ".join(tail)

    if not kind:
        marker = _KIND_MARKER_RE.search(tail_text)
        kind = marker.group(1).strip().capitalize() if marker else ""
    if not subgroups:
        found = _SUBGROUPS_RE.search(tail_text)
        subgroups = _split_subgroups(found.group(1)) if found else []

    return title, _clean_kind(kind), subgroups








############################################################
# _labelled_value / _split_subgroups / _clean_kind
############################################################
#
# The small text shapers behind _parse_title: a popover
# attribute ("<span><strong>Tipas: </strong>Pratybos,
# Privalomasis</span>") flattened with its "Label:" prefix
# stripped; a subgroup list ("1, 2" / "1;2") split into
# tokens in natural order ("2" before "10"); a kind with
# whitespace collapsed and the lecture_type separator
# replaced, so the stored value always splits back cleanly.
#
# Used by:
#   - _parse_title (above), join_lecture_type (below)
############################################################

def _labelled_value(markup: str, label: str) -> str:
    if not markup:
        return ""
    text = BeautifulSoup(markup, "html.parser").get_text(" ", strip=True) if "<" in markup else markup
    return re.sub(rf"^\s*{label}\w*\s*:\s*", "", text, flags=re.IGNORECASE).strip()


def _split_subgroups(raw) -> list:
    tokens = raw if isinstance(raw, (list, tuple, set, frozenset)) else re.split(r"[,;]", raw or "")
    cleaned = {re.sub(r"\s+", " ", str(token)).strip().replace(LECTURE_TYPE_SEPARATOR, "/")
               for token in tokens}
    return sorted((token for token in cleaned if token),
                  key=lambda token: (not token.isdigit(), int(token) if token.isdigit() else 0, token))


def _clean_kind(kind: str) -> str:
    return re.sub(r"\s+", " ", kind or "").strip().replace(LECTURE_TYPE_SEPARATOR, "/")








############################################################
# join_lecture_type / split_lecture_type
############################################################
#
# The one format schedule_events.lecture_type is written in:
# the event's kind, then — only when the event names its
# subgroups — "|" and the subgroups comma-joined in natural
# order: "Pratybos|1", "Egzaminas|1,2", "Paskaita", "" (no
# type known — every row stored before the scraper read
# types). split_lecture_type is the exact inverse, and
# tolerates anything: a value without "|" is all kind.
#
# Used by:
#   - scrape_group_schedule / _sync_schedule / _upsert_event
#     (below) — writing and merging
#   - schedule/api/views.py — get_schedule_events splits it
#     into the lectureType + subgroups wire fields
############################################################

def join_lecture_type(kind: str, subgroups=()) -> str:
    kind = _clean_kind(kind)
    groups = _split_subgroups(list(subgroups))
    return f"{kind}{LECTURE_TYPE_SEPARATOR}{','.join(groups)}" if groups else kind


def split_lecture_type(value: str):
    kind, separator, rest = (value or "").partition(LECTURE_TYPE_SEPARATOR)
    return kind.strip(), (_split_subgroups(rest) if separator else [])








############################################################
# _pick_kind
############################################################
#
# The one type a slot is stored under when its feeds disagree
# — every programme's feed types the SAME session its own way
# ("Paskaita" for one, "Paskaitos ir seminarai" for another).
# A badge type (an exam, a retake, an assessment, a
# consultation — schedule/kinds.py is_badge_kind) wins
# outright: a student must never see an exam as a lecture
# because a second feed said so. Otherwise
# the type most feeds give, and on a tie the first by text —
# deterministic, so an unchanged timetable never flips its
# rows between two runs. "" when no feed names one.
#
# Used by:
#   - scrape_group_schedule (below) — one feed's slot
#   - _sync_schedule (below) — every feed's view of a slot
############################################################

def _pick_kind(kinds) -> str:
    counts = {}
    for kind in kinds:
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        return ""
    return sorted(counts, key=lambda kind: (not is_badge_kind(kind), -counts[kind], kind.casefold()))[0]








############################################################
# _read_legend
############################################################
#
# The feed's own event_colors legend ({"#FFC2CC":
# "EGZAMINAS", "#F1F1F1": "Įprastas įvykis"}) with every
# colour normalised like the events' own — the authority on
# what a colour MEANS in this feed. A group without exams
# serves only the regular entry, so the legend is per feed
# and never a global palette. Anything that is not a plain
# colour → label mapping reads as an empty legend.
#
# Used by:
#   - scrape_group_schedule (below) — per-feed retake
#     colours and the kind fallback
############################################################

def _read_legend(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    legend = {}
    for colour, label in raw.items():
        normalised = _normalise_colour(colour)
        if normalised and isinstance(label, str) and label.strip():
            legend[normalised] = re.sub(r"\s+", " ", label).strip()
    return legend








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
# lacks "kursas" the course digit is appended — first from
# the "N Kursas" label the page prints beside the anchor
# (a <span> just before the anchor's own; the rows of one
# programme are <br>-separated, so the scan stops at a <br>
# and never borrows the previous course's label), else from
# the slug's "Nk" token. The label matters: the site's newer
# slugs ("…-angl-29") carry no such token, and without the
# label every course-year of those programmes reached
# _parse_group_display_name course-less. Raises on HTTP
# failure — the caller marks the whole run failed.
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

        # STEP 2.1: the course label the page prints beside the
        # anchor — nearest-first through the previous siblings of
        # the anchor's own <span>, stopping at the <br> that ends
        # the row so the label of the course above is never taken.
        # Strings and tags alike go through get_text, so the blank
        # whitespace nodes between the tags simply fall through
        course = ""
        if link.parent is not None:
            for sibling in link.parent.previous_siblings:
                if sibling.name == "br":
                    break
                label_match = re.search(r"(\d)\s*kursas", sibling.get_text(strip=True), re.IGNORECASE)
                if label_match:
                    course = label_match.group(1)
                    break

        # STEP 2.2: nearest preceding heading/bold, up to 5 levels up
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

        # STEP 2.3: no heading found — the link's title attribute
        if not display_name and link.get("title"):
            display_name = link["title"]

        # STEP 2.4: append the course — the page's label first, the
        # slug's "Nk" token as the fallback for a page without one.
        # The suffix is the shape _parse_group_display_name reads
        if display_name and "kursas" not in display_name.lower():
            if not course:
                course_match = re.search(r"(\d)k", slug)
                course = course_match.group(1) if course_match else ""
            if course:
                display_name += f" - {course} kursas"

        # STEP 2.5: still nothing — the de-hyphenated slug
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
# (lessons, stats): the dated lesson dicts
# scrape_knf_schedule inserts, plus a count of everything
# dropped on the way, the colour and kind histograms, and
# the feed's own colour legend. Dropped: all-day events (no
# "T" in start — holidays), retake exams, events whose dates
# don't parse, and events with an empty title. Lecturer and
# room come from the top-level "instructor"/"location"
# fields, falling back to the popover markup only when the
# title carries HTML.
#
# A retake is recognised by its NORMALISED colour (so
# "#FF899D", "#ff899d" and "rgb(255,137,157)" are one
# value) — _RETAKE_COLOURS plus any colour this feed's
# legend labels a retake — by a structured retake/type
# field, or by the PERLAIKYMAS label; a labelled retake
# wearing an unknown colour logs a WARNING, which is what
# makes a palette change visible before it imports exams as
# weekly lessons.
#
# Every lesson carries its KIND and SUBGROUPS (_parse_title,
# the legend label as the kind's last resort) joined into
# lecture_type. The body is fetched under FEED_MAX_BYTES and
# REFUSED when it runs past it: a JSON body cut at the cap
# would only raise a baffling JSONDecodeError further down.
#
# The slug is validated against _SLUG_RE and percent-encoded
# before it is interpolated into the feed URL; a malformed
# one raises and the caller skips the group.
#
# Every event keeps its DATE plus "HH:MM" times — one dict
# per dated occurrence and kind, deduped only against the
# feed serving the same instance twice (the natural key,
# teacher excluded) — two subgroups sitting the same slot
# in the same room are ONE session, their subgroups
# unioned. A one-off room change is simply a different row
# on that one date, which is the whole point of the dated
# model. Raises on HTTP/JSON failure — the caller logs and
# skips the group.
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
    # content types are accepted; the body is JSON either way.
    # A body past the cap comes back as None, never cut short
    result = fetch(url, SCHEDULE_HOSTS,
                   params={"start": start_date, "end": end_date},
                   content_types=JSON_CONTENT_TYPES + HTML_CONTENT_TYPES,
                   max_bytes=FEED_MAX_BYTES, allow_truncated=False)
    if not result:
        raise RuntimeError(f"Could not fetch the event feed for {slug} "
                           f"(refused, unreachable, or over {FEED_MAX_BYTES} bytes)")

    data = json.loads(result[0])
    events = data.get("events", [])

    # The feed's own colour legend: a colour it names a retake
    # joins the retake set for this feed
    legend = _read_legend(data.get("event_colors"))
    retake_colours = _RETAKE_COLOURS | {colour for colour, label in legend.items()
                                        if "PERLAIKYM" in label.upper()}

    group_name = _parse_group_display_name(slug, group_display_name)

    # (date, times, title, room) → the lesson dict, its
    # subgroup set and the types it was served under — a feed
    # serving one event twice, or two subgroups sitting one
    # session, collapses here
    lessons_by_key: dict = {}
    # What the run threw away and why, what it kept by kind,
    # and the legend it read — a filter that silently stops
    # matching is otherwise indistinguishable from a semester
    # without retakes
    stats = {"events": len(events), "all_day": 0, "retakes": 0,
             "unparsable": 0, "untitled": 0, "colours": {}, "kinds": {},
             "legend": legend}


    # STEP 2: one dated lesson per distinct event and kind
    # ====================================================
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
        if colour in retake_colours or labelled_retake:
            stats["retakes"] += 1
            # The label without the colour is the palette change
            # this filter has to survive
            if labelled_retake and colour and colour not in retake_colours:
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

        # Null-safe for the same reason start is: _parse_title
        # tests `"<" in title_field`, which raises on None. The
        # legend names the kind only when the popover and the
        # marker both stayed silent, and never "Įprastas įvykis"
        raw_title = event.get("title") or ""
        title, kind, subgroups = _parse_title(raw_title)
        if not title:
            stats["untitled"] += 1
            continue
        if not kind:
            label = legend.get(colour, "")
            if label and label.casefold() not in _REGULAR_LEGEND_LABELS:
                kind = _clean_kind(label.capitalize())

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

        # The physical SLOT — teacher is deliberately not in it
        # (a teacher swap is the same event), and neither is the
        # type or the subgroup: a second subgroup in the same
        # slot and room joins the session instead of becoming a
        # twin row, and a type is the session's attribute
        key = (start_dt.date(), time_start, time_end, title, room)
        held = lessons_by_key.get(key)
        if held is not None:
            held[1].update(subgroups)
            held[2].append(kind)
            continue

        lessons_by_key[key] = ({
            "title": title,
            "teacher": teacher,
            "room": room,
            "date": start_dt.date(),
            "time_start": time_start,
            "time_end": time_end,
            "group_name": group_name,
            "slug": slug,
            "semester": semester,
        }, set(subgroups), [kind])


    # STEP 3: the slot's type and its unioned subgroups into
    # the one stored lecture_type, in first-seen order
    # ======================================================
    lessons = []
    for lesson, subgroups, kinds in lessons_by_key.values():
        kind = _pick_kind(kinds)
        stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
        lessons.append({**lesson, "lecture_type": join_lecture_type(kind, subgroups)})

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
# failed group LIST fails the run — but a run that completes
# without some feeds (failed, or never reached before the
# deadline) says so in its error_message, naming them: a
# feed failing every run would otherwise freeze its group's
# timetable at the last good copy with nothing on /status.
#
# The per-run log line carries the drop counts, the colour
# and kind histograms and the legend vocabulary; a legend
# label the scraper does not know is a WARNING naming the
# colour and the label — the palette-change alarm, fed by
# the site's own legend rather than a retake counter whose
# healthy value is already zero.
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
        # becomes visible — plus what it kept by kind and every
        # legend entry any feed served
        dropped = {"all_day": 0, "retakes": 0, "unparsable": 0, "untitled": 0}
        colours: dict = {}
        kinds: dict = {}
        legend: dict = {}
        # Slugs whose feed raised — named in the run's note
        failed: list = []
        attempted = 0

        for group in groups:
            if deadline_passed(deadline):
                logger.warning("Schedule scrape out of time after %d group(s)", groups_scraped)
                break

            slug = group["slug"]
            display_name = group["display_name"]
            attempted += 1

            # STEP 4.1: a failing group is logged and skipped, never fatal
            try:
                lessons, stats = scrape_group_schedule(slug, display_name, start_date, end_date)
            except Exception:
                logger.warning("Failed to scrape group %s", slug, exc_info=True)
                failed.append(slug)
                continue

            groups_scraped += 1
            total_lessons += len(lessons)

            # STEP 4.1.1: fold this group's drop counts into the run's
            # (the kind/legend keys are read with .get — a stats dict
            # from before they existed must still fold)
            for key in dropped:
                dropped[key] += stats[key]
            for colour, count in stats["colours"].items():
                colours[colour] = colours.get(colour, 0) + count
            for kind, count in stats.get("kinds", {}).items():
                kinds[kind] = kinds.get(kind, 0) + count
            legend.update(stats.get("legend", {}))

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

        # One line per run with everything the filters removed,
        # the colours and kinds behind it and the legend served.
        # Retakes are 0 on a healthy run too (none published most
        # of the year), so the ALARM is the legend: a label the
        # scraper does not know names its colour, and a legend
        # retake colour missing from _RETAKE_COLOURS says the
        # constant went stale (the feed's own legend still steers
        # the filter meanwhile)
        logger.info("Schedule scrape filters: dropped=%s, colours=%s, kinds=%s, legend=%s",
                    dropped, colours, kinds, legend)
        unknown = {colour: label for colour, label in legend.items()
                   if label.casefold() not in _KNOWN_LEGEND_LABELS}
        if unknown:
            logger.warning("Timetable legend carries label(s) the scraper does not know: %s — "
                           "events painted so import as their popover type", unknown)
        stale_retake = sorted(colour for colour, label in legend.items()
                              if "PERLAIKYM" in label.upper() and colour not in _RETAKE_COLOURS)
        if stale_retake:
            logger.warning("Timetable legend paints retakes %s, not %s — update _RETAKE_COLOURS",
                           stale_retake, sorted(_RETAKE_COLOURS))


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
        # the articles_found / articles_new columns, the feeds it
        # could not read into the note — and prune
        # =======================================================
        close_run(run_id, total_lessons, total_new, _missing_feeds_note(failed, len(groups) - attempted))

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
# _missing_feeds_note
############################################################
#
# The error_message a COMPLETED run carries when it did not
# read every group feed, or None when it did: how many feeds
# failed (the first MAX_NAMED slugs named, so the note stays
# one readable line) and how many the deadline never
# reached. Both keep their stored schedule untouched — the
# note is what tells an admin that "untouched" may by now
# mean "stale".
#
# Used by:
#   - _run (above) — STEP 8
############################################################

def _missing_feeds_note(failed: list, unattempted: int):
    max_named = 8
    parts = []
    if failed:
        named = ", ".join(failed[:max_named]) + (", …" if len(failed) > max_named else "")
        parts.append(f"{len(failed)} group feed(s) failed and kept their last good copy: {named}")
    if unattempted > 0:
        parts.append(f"{unattempted} group feed(s) not reached before the run's deadline")
    return "; ".join(parts) or None








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
# Before the insert, a slot whose EXACT row is missing but
# which holds a twin — same date, times, title and room, not
# already claimed by another event of this run — has that
# twin RELABELLED in place (the untyped legacy row first):
# the rows stored before the scraper read types ('') take
# their type on the first typed run, and a session whose
# type or subgroup set moved keeps its id, instead of being
# re-created (a "new lectures" push for nothing) or
# duplicated (the window's fortnight of past rows is never
# retired). A run holds ONE event per slot (_sync_schedule
# merges on it), so any unclaimed row there is this event's
# own earlier copy. `claimed` holds the ids the run has
# upserted so far.
#
# Used by:
#   - _sync_schedule (below) — once per merged event
############################################################

def _upsert_event(cursor, event: dict, run_stamp, claimed=frozenset()):
    key = (event["date"], event["time_start"], event["time_end"],
           event["title"], event["lecture_type"], event["room"])
    # Raw SQL must store stamps in the exact form the ORM
    # writes and compares — on SQLite a bare datetime binding
    # keeps its "+00:00" suffix and breaks every <=> filter
    run_stamp = connection.ops.adapt_datetimefield_value(run_stamp)


    # STEP 1: relabel an unclaimed twin on the slot when the
    # exact row is missing — the legacy '' row first
    # ======================================================
    cursor.execute(
        """SELECT id, lecture_type FROM schedule_events
           WHERE date = %s AND time_start = %s AND time_end = %s
             AND title = %s AND room = %s""",
        (event["date"], event["time_start"], event["time_end"], event["title"], event["room"]),
    )
    slot_rows = cursor.fetchall()
    if not any(stored == event["lecture_type"] for _id, stored in slot_rows):
        twins = sorted(
            ((row_id, stored) for row_id, stored in slot_rows if row_id not in claimed),
            key=lambda row: (row[1] != "", row[1], row[0]),
        )
        if twins:
            cursor.execute("UPDATE schedule_events SET lecture_type = %s WHERE id = %s",
                           (event["lecture_type"], twins[0][0]))


    # STEP 2: insert-or-confirm on the full natural key
    # =================================================
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
# _superseded_copies
############################################################
#
# The ids of rows sitting on a slot the run confirmed that
# the run did NOT confirm (last_seen_at before its stamp,
# not among `claimed`) — a slot is one physical session, so
# such a row is a superseded copy of the confirmed event.
# Read by DATE, a couple of hundred days per query, then
# matched on the full slot in Python — a handful of queries
# however many slots.
#
# Used by:
#   - _sync_schedule (below) — STEP 3.1
############################################################

def _superseded_copies(slots, claimed, run_stamp) -> list:
    wanted = set(slots)
    dates = sorted({slot[0] for slot in wanted})
    copies = []
    for i in range(0, len(dates), 200):
        rows = ScheduleEvent.objects.filter(
            date__in=dates[i:i + 200], last_seen_at__lt=run_stamp,
        ).values_list("id", "date", "time_start", "time_end", "title", "room")
        for row_id, *slot in rows:
            if tuple(slot) in wanted and row_id not in claimed:
                copies.append(row_id)
    return copies








############################################################
# _sync_schedule
############################################################
#
# The whole write phase, called inside one
# transaction.atomic(). Merges the per-group dicts on the
# physical SLOT (date, times, title, room) — the same
# lecture reached through two feeds becomes ONE event with
# two group links, the union of their subgroups and the type
# _pick_kind settles — inserts-or-confirms events
# (relabelling the slot's earlier copy in place, see
# _upsert_event), teachers and links with the run's stamp,
# deletes a superseded copy left on a slot the run
# confirmed — past or future, as long as every group that
# links it answered this run (a failed feed's claim is never
# judged) — then retires what the healthy feeds stopped
# serving:
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


    # STEP 2: merge the per-group dicts on the physical slot,
    # the subgroups unioned and the feeds' types settled
    # =======================================================
    merged: dict = {}
    for lesson in scraped:
        kind, subgroups = split_lecture_type(lesson["lecture_type"])
        key = (lesson["date"], lesson["time_start"], lesson["time_end"],
               lesson["title"], lesson["room"])
        entry = merged.get(key)
        if entry is None:
            entry = merged[key] = {**lesson, "slugs": set(), "subgroups": set(), "kinds": []}
        entry["slugs"].add(lesson["slug"])
        entry["subgroups"].update(subgroups)
        entry["kinds"].append(kind)
        # The first non-empty teacher wins — feeds rarely disagree,
        # and '' must never overwrite a name
        if not entry["teacher"] and lesson["teacher"]:
            entry["teacher"] = lesson["teacher"]

    for entry in merged.values():
        entry["lecture_type"] = join_lecture_type(_pick_kind(entry["kinds"]), entry["subgroups"])


    # STEP 3: insert-or-confirm events, teachers and links
    # ====================================================
    added = 0
    teacher_ids: dict = {}
    confirmed_ids: list = []
    # The same ids as a set — _upsert_event must never relabel
    # a row an earlier event of this run already claimed
    claimed: set = set()
    with connection.cursor() as cursor:
        for event in merged.values():
            event_id, created = _upsert_event(cursor, event, run_stamp, claimed)
            confirmed_ids.append(event_id)
            claimed.add(event_id)
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


    # STEP 3.1: the superseded copies — a row the run did NOT
    # confirm, on a slot it did, is that slot's older copy (the
    # split an earlier typing left); gone past or future, once
    # every group linking it answered this run
    superseded = _superseded_copies(merged.keys(), claimed, run_stamp)
    guarded = set()
    for i in range(0, len(superseded), 500):
        guarded.update(
            ScheduleEventGroup.objects.filter(event_id__in=superseded[i:i + 500])
            .exclude(group_id__in=groups_ok.keys()).values_list("event_id", flat=True)
        )
    judged = sorted(set(superseded) - guarded)
    for i in range(0, len(judged), 500):
        ScheduleEvent.objects.filter(id__in=judged[i:i + 500]).delete()


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
    removed = by_model.get("schedule.ScheduleEvent", 0) + len(judged)


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
