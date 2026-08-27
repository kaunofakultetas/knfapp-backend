############################################################
#  [*] Schedule scraper — tvarkarasciai.vu.lt timetable
#
#  Pulls every KNF group's FullCalendar event feed from
#  tvarkarasciai.vu.lt and folds the dated events into
#  schedule_lessons as WEEKLY patterns: a lesson's identity
#  is title|teacher|room|time|weekday|group|semester, so the
#  same lecture week after week becomes ONE row, and a
#  one-off room change becomes a second row the app then
#  shows every week. Rows are only ever inserted — nothing
#  here updates or deletes — so a lesson that vanishes from
#  the source stays in the app until someone clears the
#  table by hand.
#
#  Group names collapse to programme abbreviation + course
#  ("ISKS-1"); the "1 grupė / 2 grupė" split is dropped, so
#  parallel groups share one group_name. Semester labels are
#  "<year>-R" (ruduo) / "<year>-P" (pavasaris), keyed on the
#  academic year's FIRST calendar year — spring 2026 is
#  "2025-P". The first-boot seed (database/__init__.py)
#  writes "2025-pavasaris" instead, so the filter list can
#  show both shapes side by side.
#
#  Every run is logged in scraper_runs (source
#  'tvarkarasciai.vu.lt') with the lesson counts in the
#  articles_found / articles_new columns the news scrapers
#  named. scheduler.py calls in 30 s after boot and every
#  6 h, scraper/routes.py exposes an admin trigger, and the
#  rows flow on to schedule/routes.py → the mobile schedule
#  tab (services/api/schedule.ts).
############################################################


import hashlib
import html
import logging
import re
import uuid
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from app.database import get_db

logger = logging.getLogger(__name__)

BASE_URL = "https://tvarkarasciai.vu.lt"
GROUP_LIST_URL = f"{BASE_URL}/knf/list/"
# The JSON feed behind a group's timetable page; the
# "/group/255/" segment is fixed and the date window goes in
# as start/end query params (see scrape_group_schedule)
EVENT_URL_TEMPLATE = f"{BASE_URL}/knf/ajax_fullcalendar_events/{{slug}}/group/255/"

# Dead constant: nothing reads it — _get_semester_label and
# scrape_knf_schedule both hard-code `month >= 8` as the
# autumn cutoff instead
_SEMESTER_MONTH_CUTOFF = 7  # Aug-Dec = autumn, Jan-Jul = spring

USER_AGENT = "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)"
REQUEST_TIMEOUT = 20  # seconds, per request








############################################################
# _get_semester_label
############################################################
#
# Semester label for one event date: August–December →
# "<year>-R", January–July → "<year-1>-P", so the label
# always carries the academic year's first calendar year
# (2026-02-09 → "2025-P"). January is filed as SPRING here
# while scrape_knf_schedule's date window files it under
# autumn; with the default 16-week window (ending 22 Dec)
# no January event is ever fetched, so the two never
# disagree in practice — widen the window past 17 weeks and
# January lectures get the spring label.
#
# Used by:
#   - scrape_group_schedule (below) — per event
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
# Matching is a plain substring scan over the table in
# insertion order, first against the diacritic-stripped
# lowercase display name, then the de-hyphenated slug; the
# first hit wins, so a name containing both "lietuviu
# filologija ir reklama" and "turinio kurimas ir rinkodara"
# resolves to the earlier "LFR", never "LFR-TKR". "angl"
# anywhere in the name or slug adds "-EN" — which also tags
# the "Anglų ir kita užsienio kalba" programme itself as
# English-taught ("AKUK-EN-…"). The course digit comes from
# "N kursas" in the name or "Nk"/"Nc" in the slug. No
# programme matched → the first 30 characters of the slug.
# The table is rebuilt on every call.
#
# Used by:
#   - scrape_group_schedule (below) — once per group
############################################################

def _parse_group_display_name(slug: str, display_name: str) -> str:
    # All ASCII on purpose — matched after _strip_diacritics
    _PROGRAM_ABBREVS = {
        "informacijos sistemos ir kibernetin": "ISKS",
        "ekonomika ir vadyba": "EV",
        "finansu analitika": "FA",
        "finansu technologijos": "FT",
        "marketingo technologijos": "MT",
        "audiovizualinis vertimas": "AV",
        "lietuviu filologija ir reklama": "LFR",
        "lietuviu literatura ir kurybinis rasymas": "LLKR",
        "marketingas ir pardavimu vadyba": "MPV",
        "meno vadyba": "MV",
        "tarptautinio verslo vadyba": "TVV",
        "tvariuju finansu ekonomika": "TFE",
        "viesojo diskurso lingvistika": "VDL",
        "kalba ir dirbtinio intelekto valdymas": "KDIV",
        "bendruju universitetiniu studiju": "BUS",
        "individualiuju studiju dalykai": "ISD",
        "anglu ir kita uzsienio kalba": "AKUK",
        "art management": "MV",
        "turinio kurimas ir rinkodara": "LFR-TKR",
        "kurybiskumo ir skaitmenines retorikos": "LFR-KSR",
        "mediju retorika ir komunikacija": "VDL-MRK",
        "skaitmeninio turinio prieinamumas": "AV-STP",
    }

    # Slugs are ASCII already, so only the name is folded
    candidates = [
        _strip_diacritics(display_name).lower(),
        slug.replace("-", " "),
    ]

    for name_lower in candidates:
        for pattern, abbrev in _PROGRAM_ABBREVS.items():
            if pattern in name_lower:
                # "N kursas" only exists in the display name; the
                # slug spells it "1k" (the regex also takes "1c")
                course_match = re.search(r"(\d)\s*kursas", name_lower)
                if not course_match:
                    course_match = re.search(r"(\d)[kc]", slug)
                course = course_match.group(1) if course_match else ""

                # "angl" also matches the AKUK programme name itself,
                # so every "Anglų ir kita užsienio kalba" group is
                # tagged -EN
                lang_suffix = ""
                if "angl" in name_lower or "angl" in slug:
                    lang_suffix = "-EN"

                level_suffix = ""
                if "magistrant" in name_lower:
                    level_suffix = "-M"

                group_name = f"{abbrev}{level_suffix}{lang_suffix}-{course}" if course else f"{abbrev}{level_suffix}{lang_suffix}"
                return group_name

    # No programme matched — the raw slug, capped at 30 chars
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
# The lecturer from an event title that carries the site's
# popover markup: the data-academics attribute holds
# HTML-escaped HTML, so it is regex-lifted, unescaped and
# parsed again. The FIRST <a> wins — a lesson with two
# lecturers keeps only one. Without links the flattened
# text is used with its "Dėstytojai: " label stripped. ""
# when the attribute is absent.
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
        links = soup.find_all("a")
        if links:
            return links[0].get_text(strip=True)
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
# first <a> wins, else the flattened text minus its
# "Patalpos: " label, "" when the attribute is absent.
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
        links = soup.find_all("a")
        if links:
            return links[0].get_text(strip=True)
        text = soup.get_text(strip=True)
        text = re.sub(r"^Patalpos:\s*", "", text)
        return text
    return ""








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
    # STEP 1: fetch the faculty's group list page
    # ===========================================
    resp = requests.get(GROUP_LIST_URL, timeout=REQUEST_TIMEOUT, headers={
        "User-Agent": USER_AGENT,
    })
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
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
# [start_date, end_date] (ISO dates), flattened to the
# weekly lesson dicts scrape_knf_schedule inserts. Dropped
# on the way: all-day events (no "T" in start — holidays),
# retake exams (colour #FF899D or "PERLAIKYMAS" in the
# subtitle), events whose dates don't parse, and events
# with an empty title. Lecturer and room come from the
# top-level "instructor"/"location" fields, falling back to
# the popover markup only when the title carries HTML.
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
                          start_date: str, end_date: str) -> list[dict]:
    url = EVENT_URL_TEMPLATE.format(slug=slug)
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
        "User-Agent": USER_AGENT,
    }, params={"start": start_date, "end": end_date})
    resp.raise_for_status()

    data = resp.json()
    events = data.get("events", [])

    group_name = _parse_group_display_name(slug, group_display_name)

    lessons_seen = set()  # _lesson_hash keys — weekly recurrences collapse here
    lessons = []

    for event in events:
        start_str = event.get("start", "")
        end_str = event.get("end", "")

        # All-day events (holidays) come without a time component
        if "T" not in start_str:
            continue

        # Retake exams: the site colours them #FF899D and/or labels
        # the subtitle PERLAIKYMAS
        color = event.get("color", "")
        if color == "#FF899D":
            continue
        subtitle = event.get("subtitle", "")
        if "PERLAIKYMAS" in subtitle.upper():
            continue

        # An absent end ("" → ValueError) or a null one (TypeError)
        # drops the event as well
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
        except (ValueError, TypeError):
            continue

        day_of_week = start_dt.weekday()  # 0=Mon, 6=Sun -- matches our API
        time_start = start_dt.strftime("%H:%M")
        time_end = end_dt.strftime("%H:%M")
        # Labelled per event, not per run — see _get_semester_label
        semester = _get_semester_label(start_dt)

        raw_title = event.get("title", "")
        title = _extract_title_text(raw_title)
        if not title:
            continue

        # Top-level fields first; the popover markup is only
        # consulted when the title actually carries HTML
        teacher = event.get("instructor", "")
        if not teacher and "<" in raw_title:
            teacher = _extract_teacher_from_html(raw_title)

        room = event.get("location", "")
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

    return lessons








############################################################
# scrape_knf_schedule
############################################################
#
# The full import: opens a scraper_runs row, fetches the
# group list, scrapes every group and inserts the lessons
# not already in schedule_lessons, then closes the run and
# pushes a "schedule" channel notification when anything
# new landed. Returns {"groups_scraped", "lessons_found",
# "lessons_new"} — all zeros on failure, never raises (the
# run row is marked 'failed' instead).
#
# Date window: August–January → 1 Sept of the academic
# year, otherwise 1 Feb; end = start + semester_weeks
# (default 16 → 22 Dec / ~24 May). A January run therefore
# re-imports the finished autumn semester, not the coming
# spring one. lessons_found sums every group's post-dedup
# dicts (parallel groups sharing a group_name are each
# counted); lessons_new counts actual inserts.
#
# The per-lesson "already exists" check is an eight-column
# equality SELECT — schedule_lessons has no UNIQUE index —
# with one commit per group. A group that fails to scrape
# is logged and skipped; only a failed group LIST fails the
# run, and that branch leaves finished_at NULL, unlike the
# outer except. The push's data payload (type
# "schedule_update") is not routed by the mobile tap
# listener at the moment — a tap just opens the app.
#
# Used by:
#   - scraper/scheduler.py — 30 s after start-up, then
#     every 6 h
#   - scraper/routes.py — POST /api/scraper/schedule
#     (admin trigger)
############################################################

def scrape_knf_schedule(semester_weeks: int = 16) -> dict:
    # STEP 1: open a 'running' scraper_runs row for this run
    # ======================================================
    run_id = str(uuid.uuid4())
    db = get_db()

    try:
        db.execute(
            "INSERT INTO scraper_runs (id, source, status) VALUES (?, 'tvarkarasciai.vu.lt', 'running')",
            (run_id,),
        )
        db.commit()


        # STEP 2: pick the semester window — 1 Sept for Aug-Jan,
        # 1 Feb otherwise, semester_weeks long
        # ======================================================
        now = datetime.utcnow()
        if now.month >= 8:
            start = datetime(now.year, 9, 1)
        elif now.month <= 1:
            # January re-imports the autumn semester that started
            # last September — NOT the upcoming spring one
            start = datetime(now.year - 1, 9, 1)
        else:
            start = datetime(now.year, 2, 1)

        # 16 weeks → 22 Dec / ~24 May; 18+ weeks would reach
        # January, where _get_semester_label switches to "-P"
        end = start + timedelta(weeks=semester_weeks)
        start_date = start.strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")

        logger.info("Schedule scrape: %s to %s", start_date, end_date)


        # STEP 3: fetch the group list — without it the run is
        # marked 'failed' (finished_at stays NULL on this path,
        # unlike the outer except) and zeros are returned
        # =====================================================
        try:
            groups = scrape_group_list()
        except Exception:
            logger.exception("Failed to fetch group list")
            db.execute(
                "UPDATE scraper_runs SET status = 'failed' WHERE id = ?", (run_id,),
            )
            db.commit()
            return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0}

        logger.info("Found %d groups to scrape", len(groups))

        total_lessons = 0
        total_new = 0
        groups_scraped = 0


        # STEP 4: scrape every group and insert the lessons that
        # are not in schedule_lessons yet, one commit per group
        # ======================================================
        for group in groups:
            slug = group["slug"]
            display_name = group["display_name"]

            # STEP 4.1: a failing group is logged and skipped, never fatal
            try:
                lessons = scrape_group_schedule(slug, display_name, start_date, end_date)
            except Exception:
                logger.warning("Failed to scrape group %s", slug, exc_info=True)
                continue

            groups_scraped += 1
            total_lessons += len(lessons)

            # STEP 4.2: insert the unseen ones — full-row equality, no UNIQUE index
            for lesson in lessons:
                existing = db.execute(
                    """SELECT 1 FROM schedule_lessons
                       WHERE title = ? AND teacher = ? AND room = ?
                       AND time_start = ? AND time_end = ?
                       AND day_of_week = ? AND group_name = ? AND semester = ?""",
                    (lesson["title"], lesson["teacher"], lesson["room"],
                     lesson["time_start"], lesson["time_end"],
                     lesson["day_of_week"], lesson["group_name"], lesson["semester"]),
                ).fetchone()

                if not existing:
                    db.execute(
                        """INSERT INTO schedule_lessons
                           (id, title, teacher, room, time_start, time_end,
                            day_of_week, group_name, semester)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(uuid.uuid4()), lesson["title"], lesson["teacher"],
                         lesson["room"], lesson["time_start"], lesson["time_end"],
                         lesson["day_of_week"], lesson["group_name"], lesson["semester"]),
                    )
                    total_new += 1

            db.commit()


        # STEP 5: close the run row — the lesson counts go into
        # the articles_found / articles_new columns
        # =====================================================
        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?,
                   finished_at = datetime('now')
               WHERE id = ?""",
            (total_lessons, total_new, run_id),
        )
        db.commit()

        result = {
            "groups_scraped": groups_scraped,
            "lessons_found": total_lessons,
            "lessons_new": total_new,
        }
        logger.info("Schedule scrape complete: %s", result)


        # STEP 6: push to the "schedule" channel when anything new
        # landed — per-user opt-out lives in notification_channels
        # ========================================================
        if total_new > 0:
            try:
                # Imported lazily, as the news scrapers do
                from app.notifications.push import notify_channel
                title = "Tvarkara\u0161\u010dio pakeitimai"
                # Lithuanian plural is approximated: 21 gets the
                # "nauji įrašai" form too
                body = f"{total_new} nauji \u012fra\u0161ai tvarkara\u0161tyje" if total_new > 1 else "Naujas \u012fra\u0161as tvarkara\u0161tyje"
                notify_channel("schedule", title, body, data={"type": "schedule_update", "newLessons": total_new})
            except Exception:
                # A push failure never fails the (already completed)
                # run
                logger.exception("Failed to send push notification for schedule changes")

        return result

    except Exception:
        # Anything past the group-list fetch: mark failed WITH a
        # finish time and return zeros — never raise to the
        # scheduler or the admin route
        logger.exception("Schedule scrape failed")
        db.execute(
            "UPDATE scraper_runs SET status = 'failed', finished_at = datetime('now') WHERE id = ?", (run_id,),
        )
        db.commit()
        return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0}
    finally:
        db.close()
