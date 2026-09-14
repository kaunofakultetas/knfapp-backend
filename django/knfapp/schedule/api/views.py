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
#    GET /api/schedule/filters   — groups + semesters + days
#
#  A row is always a (event × group) view: an event shared
#  by two groups answers under each group_name, exactly as
#  the per-group table used to, so group filtering stays a
#  plain equality on the wire.
#
#  Split into:
#
#    get_schedule         — the folded weekly page
#    get_schedule_events  — one capped page of dated events
#    get_schedule_filters — the filter-sheet values
############################################################


import hashlib
import re
from datetime import date as date_type, datetime, timedelta


from django.db.models import Count, Max
from django.http import HttpResponse


from knfapp.common.http import etag_for, if_none_match_contains, json_error, json_response
from knfapp.schedule.models import ScheduleEvent, ScheduleEventGroup
from knfapp.scraper.schedule_scraper import _get_semester_label, _semester_key


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








############################################################
# _parse_count / _semester_options / _table_version /
# _conditional_json / _rows_for
############################################################
#
# The shared pieces: a clamped non-negative integer param
# (garbage is a 400, an out-of-range number takes the cap),
# the semester labels past the stray-row threshold in
# SEASONAL order (the scraper's _semester_key — text order
# would rank an autumn label above its own spring), the
# cheap table fingerprint behind the ETags, the public
# conditional response, and the one query both read routes
# share: (event × group) rows through the link table,
# filtered and ordered.
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


def _semester_options():
    rows = (
        ScheduleEvent.objects.exclude(semester=None).exclude(semester="")
        .values("semester").annotate(c=Count("id")).filter(c__gte=MIN_SEMESTER_LESSONS)
        .values_list("semester", flat=True)
    )
    # Seasonal order, newest first: the scraper's key ranks a
    # label year's spring ABOVE its autumn (plain text would
    # cling to "-R" all spring); off-grammar labels sort after
    # the real ones, by text
    return sorted(rows,
                  key=lambda s: (_semester_key(s) is not None, _semester_key(s) or 0, s.casefold()),
                  reverse=True)


def _table_version():
    row = ScheduleEvent.objects.aggregate(rows_total=Count("id"), newest=Max("last_seen_at"))
    return f"{row['rows_total']}:{row['newest'] or '-'}"


def _conditional_json(request, payload, seed):
    tag = etag_for(seed)
    if if_none_match_contains(request.headers.get("If-None-Match"), tag):
        response = HttpResponse(status=304)
    else:
        response = json_response(payload)
    response["ETag"] = f'W/"{tag}"'
    response["Cache-Control"] = f"public, max-age={CACHE_MAX_AGE}"
    return response


def _rows_for(group=None, semester=None, date_from=None, date_to=None):
    query = ScheduleEventGroup.objects.select_related("event", "group")
    if group:
        query = query.filter(group__group_name=group)
    if semester:
        query = query.filter(event__semester=semester)
    if date_from:
        query = query.filter(event__date__gte=date_from)
    if date_to:
        query = query.filter(event__date__lte=date_to)
    return query.order_by("event__date", "event__time_start", "group__group_name", "event__id")








############################################################
# get_schedule
############################################################
#
# GET /api/schedule?day=&group=&semester=&limit=&offset= —
# the LEGACY weekly shape, folded on the fly: the semester's
# dated events collapse to distinct (weekday, times, title,
# teacher, room, group) patterns, so an old app build keeps
# seeing exactly the rows the retired schedule_lessons table
# used to hold. day must be ASCII digits in 0..6;
# group/semester are exact matches with empty meaning "no
# filter". No ?semester means the CURRENT term (today's
# label; the newest one only when today's has no rows) —
# never every year interleaved — and ?semester=all is the
# explicit opt-out. The answer is ONE page ordered day,
# time, group, so days never interleave; the id is a stable
# hash of the pattern.
#
# Used by:
#   - services/api/schedule.ts fetchScheduleWeek — the
#     schedule tab (the 'all' opt-out is part of the mobile
#     wire contract)
############################################################

def get_schedule(request):
    # STEP 1: validate every filter before any DB work
    # ================================================
    day_raw = request.GET.get("day")
    group = request.GET.get("group")
    semester = request.GET.get("semester")

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


    # STEP 2: default the semester to the CURRENT one — the
    # rolling window imports the next term's exam session weeks
    # early, and its label must not steal the default while this
    # term is still running. "all" opts back into every semester
    # ==========================================================
    if semester and semester.strip().lower() == "all":
        semester = None
    elif not semester:
        options = _semester_options()
        current = _get_semester_label(datetime.now())
        semester = current if current in options else (options[0] if options else None)


    # STEP 3: fold the dated rows to weekly patterns — small
    # enough to do in Python (one semester × one faculty), and
    # weekday() spares a per-backend EXTRACT dialect
    # ========================================================
    patterns = {}
    for row in _rows_for(group=group, semester=semester).values(
        "event__title", "event__teacher", "event__room", "event__time_start",
        "event__time_end", "event__date", "event__semester", "group__group_name",
    ):
        weekday = row["event__date"].weekday()
        if day is not None and weekday != day:
            continue
        key = (weekday, row["event__time_start"], row["event__time_end"],
               row["event__title"], row["event__teacher"], row["event__room"],
               row["group__group_name"], row["event__semester"])
        patterns.setdefault(key, None)

    ordered = sorted(patterns, key=lambda k: (k[0], k[1], k[6]))

    lessons = [
        {
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
        }
        for key in ordered[offset:offset + limit]
    ]


    # STEP 4: the ETag — the filters ride as a repr'd tuple, so
    # a "|" inside a group name cannot alias two seeds
    # =========================================================
    seed = f"schedule|{_table_version()}|{(day, group, semester, limit, offset)!r}"
    return _conditional_json(request, {"lessons": lessons}, seed)








############################################################
# get_schedule_events
############################################################
#
# GET /api/schedule/events?group=&from=&to=&semester=&limit=
# &offset= — dated events, the real timetable: one row per
# (event × group) with the calendar date on it, ordered
# date, time, group. from/to are inclusive ISO dates; the
# default range is [today - 7, today + 28] and a requested
# one is capped at MAX_RANGE_DAYS. dayOfWeek rides along
# (0=Monday) so clients never re-derive it differently.
#
# Used by:
#   - services/api/schedule.ts fetchScheduleEvents — the
#     schedule tab's dated feed
############################################################

def get_schedule_events(request):
    # STEP 1: validate filters — dates strictly YYYY-MM-DD
    # ====================================================
    group = request.GET.get("group")
    semester = request.GET.get("semester")

    bounds = {}
    for name, fallback in (("from", date_type.today() - timedelta(days=7)),
                           ("to", date_type.today() + timedelta(days=28))):
        raw = request.GET.get(name)
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


    # STEP 2: one page of dated (event × group) rows
    # ==============================================
    rows = _rows_for(group=group, semester=semester,
                     date_from=bounds["from"], date_to=bounds["to"]).values(
        "event__id", "event__title", "event__teacher", "event__room",
        "event__lecture_type", "event__date", "event__time_start",
        "event__time_end", "event__semester", "group__group_name",
    )[offset:offset + limit]

    events = [
        {
            "id": r["event__id"],
            "title": r["event__title"],
            "teacher": r["event__teacher"],
            "room": r["event__room"],
            "lectureType": r["event__lecture_type"],
            "date": r["event__date"].isoformat(),
            "timeStart": r["event__time_start"],
            "timeEnd": r["event__time_end"],
            "dayOfWeek": r["event__date"].weekday(),
            "group": r["group__group_name"],
            "semester": r["event__semester"],
        }
        for r in rows
    ]


    # STEP 3: the conditional answer
    # ==============================
    seed = (f"events|{_table_version()}|"
            f"{(group, semester, bounds['from'], bounds['to'], limit, offset)!r}")
    return _conditional_json(request, {"events": events}, seed)








############################################################
# get_schedule_filters
############################################################
#
# GET /api/schedule/filters — the filter sheet in one call:
# groups, semesters (past the threshold, newest first), the
# DISTINCT days, and semesterGroups correlating which groups
# really exist in which semester. ?semester= scopes groups
# and days to one label; semesters and semesterGroups always
# describe the whole table.
#
# Used by:
#   - services/api/schedule.ts fetchScheduleFilters — the
#     group/semester pickers
############################################################

def get_schedule_filters(request):
    semester = request.GET.get("semester") or None

    # STEP 1: the semester options past the stray-label threshold
    # ===========================================================
    semesters = _semester_options()


    # STEP 2: groups and days, scoped when a label is given
    # =====================================================
    scoped = ScheduleEventGroup.objects.all()
    if semester:
        scoped = scoped.filter(event__semester=semester)

    groups = sorted(set(scoped.values_list("group__group_name", flat=True)))
    days = sorted({d.weekday() for d in scoped.values_list("event__date", flat=True).distinct()})


    # STEP 3: which groups really exist in which semester —
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
        "days": days,
        "semesterGroups": [{"semester": s, "groups": sorted(set(by_semester.get(s, [])))}
                           for s in semesters],
    }
    seed = f"filters|{_table_version()}|{semester}"
    return _conditional_json(request, payload, seed)
