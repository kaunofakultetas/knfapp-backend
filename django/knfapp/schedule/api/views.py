############################################################
#  [*] Schedule API — the lecture timetable reads
#
#  Read side of schedule_lessons, no login on either route —
#  the timetable is the app's "works without an account"
#  screen. One capped page of lessons plus the filter-sheet
#  values, each behind a weak ETag (identical bytes between
#  two scraper ticks cost a 304).
#
#  Split into:
#
#    get_schedule         — one capped page of lessons
#    get_schedule_filters — groups + semesters + days
############################################################


import re


from django.db.models import Count, Max
from django.http import HttpResponse


from knfapp.common.http import etag_for, if_none_match_contains, json_error, json_response
from knfapp.schedule.models import ScheduleLesson


# One page of lessons — ?limit/?offset page through the table
# and MAX_LESSONS is the hard ceiling
MAX_LESSONS = 500
MAX_OFFSET = 100000

# Rows a semester label needs before it is offered as a filter
# value or picked as the default — the scraper labels per
# event, so a single stray row would mint a picker option
MIN_SEMESTER_LESSONS = 5

# The timetable scraper ticks every 6 h; a client copy may
# live exactly that long
CACHE_MAX_AGE = 6 * 3600

# ASCII digits only: int() also accepts Unicode digits,
# underscores and surrounding whitespace
DIGITS_RE = re.compile(r"[0-9]{1,9}")








############################################################
# _parse_count / _semester_options / _table_version /
# _conditional_json
############################################################
#
# The four shared pieces: a clamped non-negative integer
# param (garbage is a 400, an out-of-range number takes the
# cap), the semester labels past the stray-row threshold
# (NOCASE sort — inside the "YYYY-P"/"YYYY-R" family the
# text order IS chronological), the cheap table fingerprint
# behind the ETags, and the public conditional response.
#
# Used by:
#   - get_schedule, get_schedule_filters (below)
############################################################

def _parse_count(raw, name, default, minimum, maximum):
    if raw is None:
        return default, None
    if not DIGITS_RE.fullmatch(raw):
        return None, json_error(f"Parameter '{name}' must be a non-negative integer", 400)
    return min(max(int(raw), minimum), maximum), None


def _semester_options():
    rows = (
        ScheduleLesson.objects.exclude(semester=None).exclude(semester="")
        .values("semester").annotate(c=Count("id")).filter(c__gte=MIN_SEMESTER_LESSONS)
        .values_list("semester", flat=True)
    )
    return sorted(rows, key=str.casefold, reverse=True)


def _table_version():
    row = ScheduleLesson.objects.aggregate(rows_total=Count("id"), newest=Max("created_at"))
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








############################################################
# get_schedule
############################################################
#
# GET /api/schedule?day=&group=&semester=&limit=&offset= —
# every filter optional. day must be ASCII digits in 0..6;
# group/semester are exact matches with empty meaning "no
# filter". No ?semester means the NEWEST label past the
# threshold — never every year interleaved — and
# ?semester=all is the explicit opt-out. The answer is ONE
# page ordered day, time, group, id, so days never
# interleave.
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


    # STEP 2: default the semester to the newest real one;
    # "all" opts back into every semester at once
    # ====================================================
    if semester and semester.strip().lower() == "all":
        semester = None
    elif not semester:
        options = _semester_options()
        semester = options[0] if options else None


    # STEP 3: the page — a total order so days never interleave
    # =========================================================
    query = ScheduleLesson.objects.all()
    if day is not None:
        query = query.filter(day_of_week=day)
    if group:
        query = query.filter(group_name=group)
    if semester:
        query = query.filter(semester=semester)

    rows = query.order_by("day_of_week", "time_start", "group_name", "id").values(
        "id", "title", "teacher", "room", "time_start", "time_end",
        "day_of_week", "group_name", "semester",
    )[offset:offset + limit]

    lessons = [
        {
            "id": r["id"],
            "title": r["title"],
            "teacher": r["teacher"],
            "room": r["room"],
            "timeStart": r["time_start"],
            "timeEnd": r["time_end"],
            "dayOfWeek": r["day_of_week"],
            "group": r["group_name"],
            "semester": r["semester"],
        }
        for r in rows
    ]


    # STEP 4: the ETag — the filters ride as a repr'd tuple, so
    # a "|" inside a group name cannot alias two seeds
    # =========================================================
    seed = f"schedule|{_table_version()}|{(day, group, semester, limit, offset)!r}"
    return _conditional_json(request, {"lessons": lessons}, seed)








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
    scoped = ScheduleLesson.objects.all()
    if semester:
        scoped = scoped.filter(semester=semester)

    groups = sorted(scoped.exclude(group_name=None).values_list("group_name", flat=True).distinct())
    days = sorted(scoped.exclude(day_of_week=None).values_list("day_of_week", flat=True).distinct())


    # STEP 3: which groups really exist in which semester —
    # labels below the threshold are dropped here too
    # =====================================================
    known = set(semesters)
    by_semester = {}
    pairs = (
        ScheduleLesson.objects.exclude(semester=None).exclude(group_name=None)
        .values("semester", "group_name").distinct().order_by("group_name")
    )
    for r in pairs:
        if r["semester"] in known:
            by_semester.setdefault(r["semester"], []).append(r["group_name"])

    payload = {
        "groups": groups,
        "semesters": semesters,
        "days": days,
        "semesterGroups": [{"semester": s, "groups": by_semester.get(s, [])} for s in semesters],
    }
    seed = f"filters|{_table_version()}|{semester}"
    return _conditional_json(request, payload, seed)
