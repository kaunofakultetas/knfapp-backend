"""Scraper for VU KNF schedule data from tvarkarasciai.vu.lt."""

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
EVENT_URL_TEMPLATE = f"{BASE_URL}/knf/ajax_fullcalendar_events/{{slug}}/group/255/"

# Map semester suffixes used in the DB
# tvarkarasciai.vu.lt uses academic year like "2025/2026"
# We store as e.g. "2025-R" (ruduo=autumn) or "2026-P" (pavasaris=spring)
_SEMESTER_MONTH_CUTOFF = 7  # Aug-Dec = autumn, Jan-Jul = spring

USER_AGENT = "KNFAPP/1.0 (Vilnius University Kaunas Faculty Mobile App)"
REQUEST_TIMEOUT = 20


def _get_semester_label(dt: datetime) -> str:
    """Derive semester label from a date. E.g. 2026-02-09 -> '2025-P' (spring of 2025/2026 year)."""
    if dt.month >= 8:
        # Autumn semester: starts in Sept of year X -> "X-R"
        return f"{dt.year}-R"
    else:
        # Spring semester: Jan-Jul of year X -> previous academic year spring
        return f"{dt.year - 1}-P"


def _parse_group_display_name(slug: str, display_name: str) -> str:
    """Extract a short group name for storage.
    E.g. 'Informacijos sistemos ir kibernetin\u0117 sauga - 1 kursas 1 grup\u0117' -> 'ISKS-1'
    Falls back to slug if parsing fails.
    """
    # Known program abbreviations
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
    }

    name_lower = display_name.lower()

    # Try to match known programs
    for pattern, abbrev in _PROGRAM_ABBREVS.items():
        if pattern in name_lower:
            # Extract course number
            course_match = re.search(r"(\d)\s*kursas", name_lower)
            course = course_match.group(1) if course_match else ""

            # Check for English language variant
            lang_suffix = ""
            if "angl" in name_lower:
                lang_suffix = "-EN"

            # Check for master's
            level_suffix = ""
            if "magistrant" in name_lower:
                level_suffix = "-M"

            group_name = f"{abbrev}{level_suffix}{lang_suffix}-{course}" if course else f"{abbrev}{level_suffix}{lang_suffix}"
            return group_name

    # Fallback: use slug
    return slug[:30]


def _lesson_hash(title: str, teacher: str, room: str, time_start: str,
                 time_end: str, day_of_week: int, group_name: str, semester: str) -> str:
    """Create a deterministic hash for deduplication."""
    key = f"{title}|{teacher}|{room}|{time_start}|{time_end}|{day_of_week}|{group_name}|{semester}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _extract_teacher_from_html(title_html: str) -> str:
    """Extract teacher name from HTML popover data attributes."""
    # Try data-academics attribute
    match = re.search(r'data-academics="([^"]*)"', title_html)
    if match:
        raw = html.unescape(match.group(1))
        soup = BeautifulSoup(raw, "html.parser")
        links = soup.find_all("a")
        if links:
            return links[0].get_text(strip=True)
        text = soup.get_text(strip=True)
        # Remove "D\u0117stytojai: " prefix
        text = re.sub(r"^D\u0117stytojai:\s*", "", text)
        return text
    return ""


def _extract_room_from_html(title_html: str) -> str:
    """Extract room from HTML popover data attributes."""
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


def _extract_title_text(title_field: str) -> str:
    """Extract clean title text from potentially HTML-laden title."""
    if "<" in title_field:
        soup = BeautifulSoup(title_field, "html.parser")
        # Get first link or first text
        link = soup.find("a")
        if link:
            return link.get_text(strip=True)
        return soup.get_text(strip=True).split("\n")[0].strip()
    return title_field.strip()


def scrape_group_list() -> list[dict]:
    """Fetch the list of all groups from tvarkarasciai.vu.lt/knf/list/.

    Returns list of dicts: [{"slug": "...", "display_name": "..."}]
    """
    resp = requests.get(GROUP_LIST_URL, timeout=REQUEST_TIMEOUT, headers={
        "User-Agent": USER_AGENT,
    })
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    groups = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        match = re.match(r"^/knf/groups/([^/]+)/$", href)
        if match:
            slug = match.group(1)
            display_name = link.get_text(strip=True)
            groups.append({"slug": slug, "display_name": display_name})

    return groups


def scrape_group_schedule(slug: str, group_display_name: str,
                          start_date: str, end_date: str) -> list[dict]:
    """Fetch schedule events for a single group in a date range.

    Args:
        slug: Group URL slug (e.g. 'ekonomika-ir-vadyba-1k-1gr-2025')
        group_display_name: Human-readable group name
        start_date: ISO date string YYYY-MM-DD
        end_date: ISO date string YYYY-MM-DD

    Returns:
        List of parsed lesson dicts ready for DB insertion.
    """
    url = EVENT_URL_TEMPLATE.format(slug=slug)
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={
        "User-Agent": USER_AGENT,
    }, params={"start": start_date, "end": end_date})
    resp.raise_for_status()

    data = resp.json()
    events = data.get("events", [])

    group_name = _parse_group_display_name(slug, group_display_name)

    lessons_seen = set()  # dedup by hash
    lessons = []

    for event in events:
        start_str = event.get("start", "")
        end_str = event.get("end", "")

        # Skip all-day events (holidays) -- they lack time component
        if "T" not in start_str:
            continue

        # Skip retake exams (PERLAIKYMAS)
        color = event.get("color", "")
        if color == "#FF899D":
            continue
        subtitle = event.get("subtitle", "")
        if "PERLAIKYMAS" in subtitle.upper():
            continue

        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
        except (ValueError, TypeError):
            continue

        day_of_week = start_dt.weekday()  # 0=Mon, 6=Sun -- matches our API
        time_start = start_dt.strftime("%H:%M")
        time_end = end_dt.strftime("%H:%M")
        semester = _get_semester_label(start_dt)

        # Extract title -- handle both clean and HTML formats
        raw_title = event.get("title", "")
        title = _extract_title_text(raw_title)
        if not title:
            continue

        # Extract teacher -- try top-level field first, then HTML
        teacher = event.get("instructor", "")
        if not teacher and "<" in raw_title:
            teacher = _extract_teacher_from_html(raw_title)

        # Extract room -- try top-level field first, then HTML
        room = event.get("location", "")
        if not room and "<" in raw_title:
            room = _extract_room_from_html(raw_title)

        # Clean up teacher name: remove trailing academic titles for brevity
        if teacher:
            teacher = teacher.strip().rstrip(",").strip()

        # Deduplicate: same lesson on the same weekday/time/group = one entry
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


def scrape_knf_schedule(semester_weeks: int = 16) -> dict:
    """Full scrape: fetch all groups and their schedules, update DB.

    Args:
        semester_weeks: How many weeks of data to request (covers a semester).

    Returns:
        Dict with 'groups_scraped', 'lessons_found', 'lessons_new' counts.
    """
    run_id = str(uuid.uuid4())
    db = get_db()

    try:
        db.execute(
            "INSERT INTO scraper_runs (id, source, status) VALUES (?, 'tvarkarasciai.vu.lt', 'running')",
            (run_id,),
        )
        db.commit()

        # Determine date range: current semester
        now = datetime.utcnow()
        if now.month >= 8:
            # Autumn: Sept 1 to Jan 31
            start = datetime(now.year, 9, 1)
        elif now.month <= 1:
            # Still autumn semester
            start = datetime(now.year - 1, 9, 1)
        else:
            # Spring: Feb 1 to Jun 30
            start = datetime(now.year, 2, 1)

        end = start + timedelta(weeks=semester_weeks)
        start_date = start.strftime("%Y-%m-%d")
        end_date = end.strftime("%Y-%m-%d")

        logger.info("Schedule scrape: %s to %s", start_date, end_date)

        # Step 1: Get all groups
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

        for group in groups:
            slug = group["slug"]
            display_name = group["display_name"]

            try:
                lessons = scrape_group_schedule(slug, display_name, start_date, end_date)
            except Exception:
                logger.warning("Failed to scrape group %s", slug, exc_info=True)
                continue

            groups_scraped += 1
            total_lessons += len(lessons)

            for lesson in lessons:
                # Check if this exact lesson already exists
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

        db.execute(
            """UPDATE scraper_runs
               SET status = 'completed', articles_found = ?, articles_new = ?
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
        return result

    except Exception:
        logger.exception("Schedule scrape failed")
        db.execute(
            "UPDATE scraper_runs SET status = 'failed' WHERE id = ?", (run_id,),
        )
        db.commit()
        return {"groups_scraped": 0, "lessons_found": 0, "lessons_new": 0}
    finally:
        db.close()
