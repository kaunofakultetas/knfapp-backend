############################################################
#  [*] Schedule API — the lecture timetable reads
#
#  Read side of the DATED schedule tables, no login on any
#  route — the timetable is the app's "works without an
#  account" screen. Three routes behind weak ETags
#  (identical bytes between two scraper ticks cost a 304):
#
#    GET /api/schedule           — the WEEKLY shape, folded
#        on the fly from the dated events (legacy wire
#        contract; old app builds keep working)
#    GET /api/schedule/events    — dated events in a date
#        range, the shape the current app consumes
#    GET /api/schedule/filters   — groups + semesters (with
#        each term's first and last date) + days + teachers
#    GET /api/schedule/calendar.ics — one group's or one
#        teacher's timetable as an iCalendar subscription
#
#  A row is always a (event × group) view: an event shared
#  by two groups answers under each group_name, exactly as
#  the per-group table used to, so group filtering stays a
#  plain equality on the wire.
#
#  Every route decides its 304 BEFORE any body work: the
#  ETag seed needs only the parsed parameters and one
#  aggregate (_table_version), so a revalidation costs one
#  query instead of the whole answer it discards.
#
#  Split into:
#
#    get_schedule          — the folded weekly page
#    get_schedule_events   — one capped page of dated events
#    get_schedule_filters  — the filter-sheet values
#    get_schedule_calendar — the iCalendar feed
############################################################


import hashlib
import re
from datetime import date as date_type, datetime, timedelta


from django.db.models import Count, Max, Min
from django.db.models.functions import ExtractIsoWeekDay
from django.http import HttpResponse


from knfapp.common.http import (
    clean_param, etag_for, if_none_match_contains, json_error, json_response, require_methods,
)
from knfapp.schedule.ical import render_calendar
from knfapp.schedule.models import ScheduleEvent, ScheduleEventGroup, ScheduleGroup, ScheduleTeacher
from knfapp.scraper.schedule_scraper import _get_semester_label, _semester_key, split_lecture_type


# One page of rows — ?limit/?offset page through and
# MAX_LESSONS is the hard ceiling
MAX_LESSONS = 500
MAX_OFFSET = 100000

# Rows a semester label needs before it is offered as a filter
# value or picked as the default — the scraper labels per
# event, so a single stray row would mint a picker option
MIN_SEMESTER_LESSONS = 5

# The timetable scraper ticks every 6 h; a client copy may
# live exactly that long
CACHE_MAX_AGE = 6 * 3600

# The widest date range /api/schedule/events serves in one
# call — a whole semester with both exam sessions fits
MAX_RANGE_DAYS = 220

# ASCII digits only: int() also accepts Unicode digits,
# underscores and surrounding whitespace
DIGITS_RE = re.compile(r"[0-9]{1,9}")

# Strict wire dates — date.fromisoformat also accepts
# compact and week-number forms this API never means
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# Bumped whenever a route's BODY SHAPE changes, and mixed
# into every ETag seed: a client revalidating a copy of the
# old shape must get the new body, not a 304 that pins the
# old one on an unchanged table
WIRE_SHAPE = "2"

# The iCalendar feed's window and caching: a fortnight back
# (last week's room change still reads right), forward as far
# as the table holds (the scraper's horizon is ~20 weeks, so
# the cap only fences bad data), and an hour's client cache —
# calendar apps poll on their own schedule anyway
CALENDAR_BACK_DAYS = 14
CALENDAR_AHEAD_DAYS = 400
CALENDAR_MAX_AGE = 3600








############################################################
# _parse_count / _term_rows / _semester_options /
# _table_state / _table_version / _not_modified /
# _cacheable / _rows_for
############################################################
#
# The shared pieces: a clamped non-negative integer param
# (garbage is a 400, an out-of-range number takes the cap),
# the semester labels past the stray-row threshold in
# SEASONAL order (the scraper's _semester_key — text order
# would rank an autumn label above its own spring) together
# with each label's first and last event date, the cheap
# table fingerprint behind the ETags (with its newest stamp,
# which the calendar prints), the conditional-GET pair —
# _not_modified answers the 304 (or None) from a tag alone,
# before the caller builds anything, and _cacheable stamps
# the ETag (weak, or strong for a byte-stable body) and the
# public caching headers on either answer (Vary on
# Authorization all the same — the body is nobody's, but
# every API answer keys a cache on the credential so the
# rule has no exception to remember) — and the one query
# every read route shares: (event × group) rows through the
# link table, filtered and ordered.
#
# Used by:
#   - get_schedule, get_schedule_events,
#     get_schedule_filters (below)
############################################################

def _parse_count(raw, name, default, minimum, maximum):
    if raw is None:
        return default, None
    if not DIGITS_RE.fullmatch(raw):
        return None, json_error(f"Parameter '{name}' must be a non-negative integer", 400)
    return min(max(int(raw), minimum), maximum), None


def _term_rows():
    rows = (
        ScheduleEvent.objects.exclude(semester=None).exclude(semester="")
        .values("semester")
        .annotate(c=Count("id"), first=Min("date"), last=Max("date"))
        .filter(c__gte=MIN_SEMESTER_LESSONS)
    )
    # Seasonal order, newest first: the scraper's key ranks a
    # label year's spring ABOVE its autumn (plain text would
    # cling to "-R" all spring); off-grammar labels sort after
    # the real ones, by text
    return sorted(rows,
                  key=lambda r: (_semester_key(r["semester"]) is not None,
                                 _semester_key(r["semester"]) or 0, r["semester"].casefold()),
                  reverse=True)


def _semester_options():
    return [row["semester"] for row in _term_rows()]


def _table_state():
    # The fingerprint AND its newest confirmation stamp — the
    # calendar feed prints the stamp, so both come from ONE
    # aggregate
    row = ScheduleEvent.objects.aggregate(rows_total=Count("id"), newest=Max("last_seen_at"))
    return f"{row['rows_total']}:{row['newest'] or '-'}", row["newest"]


def _table_version():
    return _table_state()[0]


def _not_modified(request, tag, *, strong=False, max_age=CACHE_MAX_AGE):
    if not if_none_match_contains(request.headers.get("If-None-Match"), tag):
        return None
    return _cacheable(HttpResponse(status=304), tag, strong=strong, max_age=max_age)


def _cacheable(response, tag, *, strong=False, max_age=CACHE_MAX_AGE):
    # Weak by default: the JSON routes promise the same MEANING,
    # not the same bytes; the calendar feed's body is a pure
    # function of its seed and earns the strong form
    response["ETag"] = f'"{tag}"' if strong else f'W/"{tag}"'
    response["Vary"] = "Authorization, Accept-Encoding"
    response["Cache-Control"] = f"public, max-age={max_age}"
    return response


def _rows_for(group=None, semester=None, teacher=None, date_from=None, date_to=None):
    # No select_related: every caller ends in .values(), which
    # makes Django drop it anyway — the joins come from the
    # named fields
    query = ScheduleEventGroup.objects.all()
    if group:
        query = query.filter(group__group_name=group)
    if semester:
        query = query.filter(event__semester=semester)
    if teacher:
        # Exact match on the display string — the roster in
        # /schedule/filters serves these exact values
        query = query.filter(event__teacher=teacher)
    if date_from:
        query = query.filter(event__date__gte=date_from)
    if date_to:
        query = query.filter(event__date__lte=date_to)
    return query.order_by("event__date", "event__time_start", "group__group_name", "event__id")








############################################################
# get_schedule
############################################################
#
# GET /api/schedule?day=&group=&semester=&limit=&offset=
#
# The LEGACY weekly shape, folded on the fly: the
# semester's dated events collapse to distinct (weekday,
# times, title, teacher, room, group) patterns, so an old
# app build keeps seeing exactly the rows the retired
# schedule_lessons table used to hold. day must be ASCII
# digits in 0..6; group/semester are exact matches with
# empty meaning "no filter". No ?semester means the CURRENT
# term (today's label; the newest one only when today's has
# no rows) — never every year interleaved — and
# ?semester=all is the explicit opt-out.
#
# The fold is ONE SQL statement: SELECT DISTINCT over the
# pattern columns with the ISO weekday computed by the
# database, the day filter a WHERE, ORDER BY day, time,
# group (then the rest of the pattern, so the page order is
# total) and the page cut by LIMIT/OFFSET — the database
# returns one page, not the semester. One row past the page
# answers hasMore. The id is a stable hash of the pattern.
#
# Used by:
#   - no shipped client — the current app reads the dated
#     GET /api/schedule/events (services/api/schedule.ts
#     fetchScheduleEvents); this shape stays for the old
#     app builds that still call it
############################################################

@require_methods("GET")
def get_schedule(request):
    # STEP 1: validate every filter before any DB work
    # ================================================
    day_raw = clean_param(request.GET.get("day"))
    group = clean_param(request.GET.get("group"))
    semester = clean_param(request.GET.get("semester"))

    day = None
    if day_raw is not None:
        if not DIGITS_RE.fullmatch(day_raw):
            return json_error("Parameter 'day' must be an integer (0=Monday..6=Sunday)", 400)
        day = int(day_raw)
        if day < 0 or day > 6:
            return json_error("Parameter 'day' must be between 0 (Monday) and 6 (Sunday)", 400)

    limit, err = _parse_count(request.GET.get("limit"), "limit", MAX_LESSONS, 1, MAX_LESSONS)
    if err:
        return err
    offset, err = _parse_count(request.GET.get("offset"), "offset", 0, 0, MAX_OFFSET)
    if err:
        return err


    # STEP 2: the conditional answer, before any body work —
    # the default semester below is a pure function of the
    # table (its version) and today's label, so the seed
    # carries those instead of the resolved value. The filters
    # ride as a repr'd tuple, so a "|" inside a group name
    # cannot alias two seeds
    # ========================================================
    current = _get_semester_label(datetime.now())
    tag = etag_for(f"schedule|{WIRE_SHAPE}|{_table_version()}|{current}|"
                   f"{(day, group, semester, limit, offset)!r}")
    cached = _not_modified(request, tag)
    if cached:
        return cached


    # STEP 3: default the semester to the CURRENT one — the
    # rolling window imports the next term's exam session weeks
    # early, and its label must not steal the default while this
    # term is still running. "all" opts back into every semester
    # ==========================================================
    if semester and semester.strip().lower() == "all":
        semester = None
    elif not semester:
        options = _semester_options()
        semester = current if current in options else (options[0] if options else None)


    # STEP 4: fold the dated rows to weekly patterns IN SQL —
    # one page plus one row, the weekday from the database's
    # own ISO extract (1 = Monday), portable across engines
    # =======================================================
    pattern = ("iso_day", "event__time_start", "event__time_end", "event__title",
               "event__teacher", "event__room", "group__group_name", "event__semester")
    rows = _rows_for(group=group, semester=semester).annotate(iso_day=ExtractIsoWeekDay("event__date"))
    if day is not None:
        rows = rows.filter(iso_day=day + 1)
    page = list(
        rows.values(*pattern).distinct()
        .order_by("iso_day", "event__time_start", "group__group_name", "event__time_end",
                  "event__title", "event__teacher", "event__room", "event__semester")
        [offset:offset + limit + 1]
    )

    lessons = []
    for row in page[:limit]:
        key = (row["iso_day"] - 1, row["event__time_start"], row["event__time_end"],
               row["event__title"], row["event__teacher"], row["event__room"],
               row["group__group_name"], row["event__semester"])
        lessons.append({
            # Stable across runs — old clients key rows on it
            "id": hashlib.sha256("|".join(map(str, key)).encode()).hexdigest()[:16],
            "title": key[3],
            "teacher": key[4],
            "room": key[5],
            "timeStart": key[1],
            "timeEnd": key[2],
            "dayOfWeek": key[0],
            "group": key[6],
            "semester": key[7],
        })

    return _cacheable(json_response({"lessons": lessons, "hasMore": len(page) > limit}), tag)








############################################################
# get_schedule_events
############################################################
#
# GET /api/schedule/events?group=&teacher=&from=&to=&semester=&limit=&offset=
#
# Dated events, the real timetable: one row per (event ×
# group) with the calendar date on it, ordered date, time,
# group. group and teacher are exact matches (the teacher
# string as the filters roster serves it — the mobile
# teacher perspective's feed, where the FOLDED shape showed
# an alternating biweekly lecture twice per week). from/to
# are inclusive ISO dates; the default range is [today - 7,
# today + 28] and a requested one is capped at
# MAX_RANGE_DAYS. dayOfWeek rides along (0=Monday) so
# clients never re-derive it.
#
# lectureType is the event's kind as the site names it
# ("Paskaita", "Pratybos", "Egzaminas", "" when unknown) and
# subgroups the "Pogrupiai" it names (["1"], [] for the
# whole group) — both split out of the stored lecture_type
# (schedule_scraper.split_lecture_type).
#
# Used by:
#   - services/api/schedule.ts fetchScheduleEvents — the
#     schedule tab's dated feed
############################################################

@require_methods("GET")
def get_schedule_events(request):
    # STEP 1: validate filters — dates strictly YYYY-MM-DD
    # ====================================================
    group = clean_param(request.GET.get("group"))
    teacher = clean_param(request.GET.get("teacher"))
    semester = clean_param(request.GET.get("semester"))

    bounds = {}
    for name, fallback in (("from", date_type.today() - timedelta(days=7)),
                           ("to", date_type.today() + timedelta(days=28))):
        raw = clean_param(request.GET.get(name))
        if raw is None:
            bounds[name] = fallback
            continue
        if not ISO_DATE_RE.fullmatch(raw):
            return json_error(f"Parameter '{name}' must be an ISO date (YYYY-MM-DD)", 400)
        try:
            bounds[name] = date_type.fromisoformat(raw)
        except ValueError:
            return json_error(f"Parameter '{name}' is not a valid date", 400)

    if bounds["to"] < bounds["from"]:
        return json_error("Parameter 'to' must not precede 'from'", 400)
    if (bounds["to"] - bounds["from"]).days > MAX_RANGE_DAYS:
        return json_error(f"The date range is capped at {MAX_RANGE_DAYS} days", 400)

    limit, err = _parse_count(request.GET.get("limit"), "limit", MAX_LESSONS, 1, MAX_LESSONS)
    if err:
        return err
    offset, err = _parse_count(request.GET.get("offset"), "offset", 0, 0, MAX_OFFSET)
    if err:
        return err


    # STEP 2: the conditional answer, before the page query —
    # every seed input is parsed by now
    # =======================================================
    tag = etag_for(f"events|{WIRE_SHAPE}|{_table_version()}|"
                   f"{(group, teacher, semester, bounds['from'], bounds['to'], limit, offset)!r}")
    cached = _not_modified(request, tag)
    if cached:
        return cached


    # STEP 3: one page of dated (event × group) rows
    # ==============================================
    # DISTINCT: two slugs folding to one group_name ("1 grupė"/
    # "2 grupė" subgroups) can both link the same event — one
    # (event × group_name) row must reach the wire once, or the
    # client renders duplicate keys
    rows = _rows_for(group=group, semester=semester, teacher=teacher,
                     date_from=bounds["from"], date_to=bounds["to"]).values(
        "event__id", "event__title", "event__teacher", "event__room",
        "event__lecture_type", "event__date", "event__time_start",
        "event__time_end", "event__semester", "group__group_name",
    ).distinct()[offset:offset + limit]

    events = []
    for r in rows:
        kind, subgroups = split_lecture_type(r["event__lecture_type"])
        events.append({
            "id": r["event__id"],
            "title": r["event__title"],
            "teacher": r["event__teacher"],
            "room": r["event__room"],
            "lectureType": kind,
            "subgroups": subgroups,
            "date": r["event__date"].isoformat(),
            "timeStart": r["event__time_start"],
            "timeEnd": r["event__time_end"],
            "dayOfWeek": r["event__date"].weekday(),
            "group": r["group__group_name"],
            "semester": r["event__semester"],
        })

    return _cacheable(json_response({"events": events}), tag)








############################################################
# get_schedule_filters
############################################################
#
# GET /api/schedule/filters?semester=
#
# The filter sheet in one call: groups, semesters (past the
# threshold, newest first) with `terms` naming each one's
# first and last event date, the DISTINCT days, teachers
# (the roster the teacher-perspective picker searches —
# exact strings ?teacher= matches), and semesterGroups
# correlating which groups really exist in which semester.
# ?semester= scopes groups and days to one label;
# semesters, terms, teachers and semesterGroups always
# describe the whole table. The terms are what lands the
# app's semester jump on a term's first REAL week — the
# nominal September/February Mondays miss both a term that
# opens mid-week in August and a January exam session.
#
# Used by:
#   - services/api/schedule.ts fetchScheduleFilters — the
#     group/teacher pickers and the semester time-jump
############################################################

@require_methods("GET")
def get_schedule_filters(request):
    semester = clean_param(request.GET.get("semester")) or None


    # STEP 1: the conditional answer — the seed is the table
    # version and the one parameter, both known already
    # ======================================================
    tag = etag_for(f"filters|{WIRE_SHAPE}|{_table_version()}|{semester}")
    cached = _not_modified(request, tag)
    if cached:
        return cached


    # STEP 2: the semester options past the stray-label
    # threshold, with their date spans
    # =================================================
    terms = _term_rows()
    semesters = [row["semester"] for row in terms]


    # STEP 3: groups and days, scoped when a label is given —
    # DISTINCT in the database: the link table holds one row
    # per (event × group), the answer is a few dozen names
    # =======================================================
    scoped = ScheduleEventGroup.objects.all()
    if semester:
        scoped = scoped.filter(event__semester=semester)

    groups = sorted(scoped.values_list("group__group_name", flat=True).distinct())
    days = sorted(iso_day - 1 for iso_day in
                  scoped.annotate(iso_day=ExtractIsoWeekDay("event__date"))
                  .values_list("iso_day", flat=True).distinct())


    # STEP 4: which groups really exist in which semester —
    # labels below the threshold are dropped here too
    # =====================================================
    known = set(semesters)
    by_semester = {}
    pairs = (
        ScheduleEventGroup.objects.values("event__semester", "group__group_name")
        .distinct().order_by("group__group_name")
    )
    for r in pairs:
        if r["event__semester"] in known:
            by_semester.setdefault(r["event__semester"], []).append(r["group__group_name"])

    payload = {
        "groups": groups,
        "semesters": semesters,
        "terms": [{"semester": row["semester"], "from": row["first"].isoformat(),
                   "to": row["last"].isoformat()} for row in terms],
        "days": days,
        # Every known teacher, retention-pruned with the events —
        # the strings are exactly what ?teacher= matches
        "teachers": sorted(ScheduleTeacher.objects.values_list("name", flat=True),
                           key=str.casefold),
        "semesterGroups": [{"semester": s, "groups": sorted(set(by_semester.get(s, [])))}
                           for s in semesters],
    }
    return _cacheable(json_response(payload), tag)








############################################################
# get_schedule_calendar
############################################################
#
# GET /api/schedule/calendar.ics?group=|teacher=&lang=
#
# One group's (group_name, exact) or one teacher's (the
# roster's exact string) timetable as an RFC 5545 feed a
# phone calendar subscribes to: every event from a fortnight
# back to the end of the last published term, one VEVENT
# each (schedule/ical.py renders), UTC times from the
# Vilnius wall clock, exams leading their SUMMARY. ?lang=en
# words the calendar name and the event details in English;
# anything else is Lithuanian.
#
# Exactly one of group/teacher, or 400; a name the timetable
# does not know is a 404 — a feed of nothing would look like
# a subscription that silently broke. The ETag is STRONG (the
# body is a pure function of the table state, today's date
# and the parameters) and answers a 304 from one aggregate
# before any other query; a 200 costs that aggregate, the
# existence probe and ONE data query — every (event × group)
# row of the scope's events, so each VEVENT can name all its
# groups. Clients may cache for an hour.
#
# Used by:
#   - components/schedule/CalendarSubscribeSheet.tsx (via
#     services/api/schedule.ts scheduleCalendarLinks) — the
#     "Prenumeruoti kalendoriuje" webcal / Google Calendar /
#     copy-link targets; then the subscribed calendar apps
############################################################

@require_methods("GET")
def get_schedule_calendar(request):
    # STEP 1: exactly one scope, and the feed's language
    # ==================================================
    # A blank value is no value — "?group= " names nothing
    group = (clean_param(request.GET.get("group")) or "").strip() or None
    teacher = (clean_param(request.GET.get("teacher")) or "").strip() or None
    if bool(group) == bool(teacher):
        return json_error("Pass exactly one of 'group' or 'teacher'", 400, code="calendar_scope")
    lang = "en" if (clean_param(request.GET.get("lang")) or "").strip().lower().startswith("en") else "lt"


    # STEP 2: the conditional answer — the window moves with
    # today, so today rides the seed
    # ======================================================
    today = date_type.today()
    version, newest = _table_state()
    tag = etag_for(f"calendar|{WIRE_SHAPE}|{version}|{today}|{(group, teacher, lang)!r}")
    cached = _not_modified(request, tag, strong=True, max_age=CALENDAR_MAX_AGE)
    if cached:
        return cached


    # STEP 3: a scope the timetable never heard of is a 404
    # =====================================================
    known = (ScheduleGroup.objects.filter(group_name=group) if group
             else ScheduleTeacher.objects.filter(name=teacher)).exists()
    if not known:
        return json_error("No such group or teacher in the timetable", 404, code="calendar_scope_unknown")


    # STEP 4: every (event × group) row of the scope's events
    # in the window — ONE query, the scope as a subquery
    # =======================================================
    scope = (ScheduleEventGroup.objects.filter(group__group_name=group).values("event_id") if group
             else ScheduleEvent.objects.filter(teacher=teacher).values("id"))
    rows = (
        ScheduleEventGroup.objects
        .filter(event_id__in=scope,
                event__date__gte=today - timedelta(days=CALENDAR_BACK_DAYS),
                event__date__lte=today + timedelta(days=CALENDAR_AHEAD_DAYS))
        .values("event__id", "event__title", "event__teacher", "event__room",
                "event__lecture_type", "event__date", "event__time_start",
                "event__time_end", "group__group_name")
        .order_by("event__date", "event__time_start", "event__id", "group__group_name")
    )

    events = {}
    for row in rows:
        event = events.get(row["event__id"])
        if event is None:
            kind, subgroups = split_lecture_type(row["event__lecture_type"])
            event = events[row["event__id"]] = {
                "id": row["event__id"], "title": row["event__title"], "kind": kind,
                "subgroups": subgroups, "teacher": row["event__teacher"], "room": row["event__room"],
                "date": row["event__date"], "time_start": row["event__time_start"],
                "time_end": row["event__time_end"], "groups": [],
            }
        if row["group__group_name"] not in event["groups"]:
            event["groups"].append(row["group__group_name"])


    # STEP 5: the feed — UTF-8 text/calendar, the strong tag
    # ======================================================
    body = render_calendar(events.values(), name=group or teacher, lang=lang, stamp=newest)
    response = HttpResponse(body.encode("utf-8"), content_type="text/calendar; charset=utf-8")
    ascii_name = re.sub(r"[^A-Za-z0-9-]+", "-", group or "destytojas").strip("-") or "tvarkarastis"
    response["Content-Disposition"] = f'inline; filename="knf-{ascii_name}.ics"'
    return _cacheable(response, tag, strong=True, max_age=CALENDAR_MAX_AGE)
