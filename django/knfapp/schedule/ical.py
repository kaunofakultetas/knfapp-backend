############################################################
#  [*] Schedule — the timetable as an iCalendar feed
#
#  RFC 5545 rendering behind GET /api/schedule/calendar.ics:
#  a student subscribes once (Apple Calendar through webcal://,
#  Google Calendar by URL) and their lectures, rooms and exams
#  keep themselves current. Pure functions over plain dicts —
#  the view queries, this module only writes text.
#
#  The rules a calendar client actually checks: CRLF line
#  ends; every content line folded at 75 OCTETS (not
#  characters — "š" is two) with the break never inside a
#  UTF-8 sequence, continuation lines led by one space; TEXT
#  values with backslash, semicolon, comma and newline
#  escaped; DTSTART/DTEND as UTC "…Z" stamps computed from the
#  Europe/Vilnius wall clock the timetable is published in —
#  daylight saving included, so a 09:45 lecture is 06:45Z in
#  October's first weeks and 07:45Z after the clocks go back.
#  The UID is the event's own id, which the scraper keeps
#  stable across relabels, so a type or subgroup change edits
#  an event in the student's calendar instead of duplicating
#  it. The body is deterministic for one table state (DTSTAMP
#  is the scrape stamp, never the request time) — what makes
#  the view's STRONG ETag honest.
#
#  Split into:
#
#    escape_text     — a TEXT value, escaped
#    fold_line       — one content line, folded at 75 octets
#    utc_stamp       — a Vilnius wall time → "YYYYMMDDTHHMMSSZ"
#    render_calendar — the whole VCALENDAR body
############################################################


import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


from knfapp.schedule.kinds import english_kind, is_badge_kind


# The wall clock the faculty publishes its timetable in
VILNIUS = ZoneInfo("Europe/Vilnius")

# The domain the UIDs live under — a stable namespace, not a URL
UID_DOMAIN = "knfapp.knf-hosting.lt"

PRODID = "-//VU Kauno fakultetas//KNFAPP tvarkarastis//LT"

# How often a subscribed client should come back — the scraper
# ticks every 6 h, so sooner buys nothing
REFRESH = "PT6H"

# A strict "HH:MM" — anything else is not a time this feed
# can place, and its event is left out rather than guessed
_CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# The feed's own words, per language
_WORDS = {
    "lt": {"calendar": "KNF tvarkaraštis", "type": "Tipas", "teacher": "Dėstytojas",
           "subgroups": "Pogrupiai", "groups": "Grupės"},
    "en": {"calendar": "KNF timetable", "type": "Type", "teacher": "Teacher",
           "subgroups": "Subgroups", "groups": "Groups"},
}








############################################################
# escape_text
############################################################
#
# One TEXT property value per RFC 5545 3.3.11: backslash
# first (or the other escapes would be doubled), then ";",
# "," and every newline form as the two characters "\n".
#
# Used by:
#   - render_calendar (below) — every TEXT property
############################################################

def escape_text(value: str) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )








############################################################
# fold_line
############################################################
#
# One content line folded per RFC 5545 3.1: at most 75
# octets per physical line, the break falling BETWEEN
# characters (a Lithuanian letter is two octets, a dash
# three), each continuation line opened by one space — which
# counts against its 75, so continuations carry 74. Joined
# with CRLF, no trailing break.
#
# Used by:
#   - render_calendar (below) — every line it writes
############################################################

def fold_line(line: str) -> str:
    parts = []
    current = bytearray()
    limit = 75
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > limit:
            parts.append(current.decode("utf-8"))
            current = bytearray()
            limit = 74
        current += encoded
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)








############################################################
# utc_stamp
############################################################
#
# A calendar date plus an "HH:MM" Vilnius wall time → the
# UTC form-2 DATE-TIME ("20261023T064500Z"), or None when the
# time is not a strict clock. The zone does the daylight-
# saving arithmetic; a datetime is an ordinary timestamp to
# every caller.
#
# Used by:
#   - render_calendar (below) — DTSTART / DTEND
############################################################

def utc_stamp(day, clock: str):
    match = _CLOCK_RE.match((clock or "").strip())
    if not match:
        return None
    local = datetime.combine(day, time(int(match.group(1)), int(match.group(2))), tzinfo=VILNIUS)
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")








############################################################
# render_calendar
############################################################
#
# The whole feed. `events` are dicts in display order:
#   {"id", "title", "kind", "subgroups", "teacher", "room",
#    "date", "time_start", "time_end", "groups"}
# (`kind` and `subgroups` already split out of lecture_type,
# `groups` every group the event links). `name` titles the
# calendar ("ISKS-2" / a teacher), `lang` is "lt" or "en",
# `stamp` the aware datetime every DTSTAMP carries — the
# table's last confirmation, so the bytes change only when
# the data does. An exam-like type leads its SUMMARY
# ("Egzaminas: …" / "Exam: …"); an event whose times do not
# parse, or end before they start, is left out.
#
# Used by:
#   - schedule/api/views.py — get_schedule_calendar
############################################################

def render_calendar(events, *, name: str, lang: str, stamp) -> str:
    words = _WORDS["en" if lang == "en" else "lt"]
    kind_name = english_kind if lang == "en" else (lambda word: word)
    dtstamp = (stamp or datetime(1970, 1, 1, tzinfo=timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    title = f"{words['calendar']} — {name}"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{escape_text(title)}",
        f"NAME:{escape_text(title)}",
        "X-WR-TIMEZONE:Europe/Vilnius",
        f"REFRESH-INTERVAL;VALUE=DURATION:{REFRESH}",
        f"X-PUBLISHED-TTL:{REFRESH}",
    ]

    for event in events:
        start = utc_stamp(event["date"], event["time_start"])
        end = utc_stamp(event["date"], event["time_end"])
        if not start or not end or end <= start:
            continue

        kind = (event.get("kind") or "").strip()
        label = kind_name(kind) if kind else ""
        summary = f"{label}: {event['title']}" if kind and is_badge_kind(kind) else event["title"]
        subgroups = event.get("subgroups") or []
        groups = event.get("groups") or []
        description = "\n".join(part for part in (
            f"{words['type']}: {label}" if label else "",
            f"{words['teacher']}: {event['teacher']}" if event.get("teacher") else "",
            f"{words['subgroups']}: {', '.join(subgroups)}" if subgroups else "",
            f"{words['groups']}: {', '.join(groups)}" if groups else "",
        ) if part)

        lines += [
            "BEGIN:VEVENT",
            f"UID:{event['id']}@{UID_DOMAIN}",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"SUMMARY:{escape_text(summary)}",
        ]
        if event.get("room"):
            lines.append(f"LOCATION:{escape_text(event['room'])}")
        if description:
            lines.append(f"DESCRIPTION:{escape_text(description)}")
        if label:
            lines.append(f"CATEGORIES:{escape_text(label)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"
