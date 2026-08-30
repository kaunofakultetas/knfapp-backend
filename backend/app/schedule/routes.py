############################################################
#  [*] Schedule — the lecture timetable API
#
#  Read side of schedule_lessons: one capped page of lessons
#  and the group/semester/day values behind the mobile filter
#  sheet. No login on either read — the timetable is the
#  app's "works without an account" screen.
#
#  Who writes the table: scraper/schedule_scraper.py
#  (scrape_knf_schedule — 30 s after boot and every 6 h via
#  scraper/scheduler.py, or POST /api/scraper/schedule), and
#  nothing else any more. The admin demo-seed route is gone:
#  nothing ever called it and every call appended its 31
#  fixtures again; database/__init__.py dropped its own 11
#  first-boot "2025-pavasaris" rows in the same wave.
#
#  Times are "HH:MM" wall-clock strings, day_of_week is
#  0 = Monday … 6 = Sunday (a CHECK on the table), and
#  semester labels follow the scraper's "YYYY-P" (spring)
#  / "YYYY-R" (autumn) shape. The JSON is camelCased:
#  group_name → group, time_start → timeStart.
#
#  Two habits both reads share: a semester label only counts
#  once it carries MIN_SEMESTER_LESSONS rows (the scraper
#  labels per event, so a single stray row used to mint a
#  whole picker option), and every answer carries a weak
#  ETag over the table's own version plus Cache-Control — a
#  relaunch costs a 304 and no body.
#
#    GET /api/schedule         — one capped page of lessons
#    GET /api/schedule/filters — groups + semesters + days
############################################################


import hashlib
import re

from flask import Blueprint, jsonify, make_response, request

from app.database import get_db

# One page of lessons. An unfiltered call used to stream the
# whole growing table to anonymous callers; ?limit and ?offset
# page through it and MAX_LESSONS is the hard ceiling.
MAX_LESSONS = 500
MAX_OFFSET = 100000

# How many rows a semester label needs before it is offered as
# a filter value or picked as the default semester.
MIN_SEMESTER_LESSONS = 5

# The timetable scraper ticks every 6 h (scraper/scheduler.py),
# so a client copy may live exactly that long — the mobile app
# keeps its own week-long copy on top (services/cache.ts).
CACHE_MAX_AGE = 6 * 3600

# ASCII digits only: int() also accepts Unicode digits ("٣"),
# underscores ("3_0") and surrounding whitespace.
_DIGITS_RE = re.compile(r"[0-9]{1,9}")

schedule_bp = Blueprint("schedule", __name__)








############################################################
# _parse_count
############################################################
#
# One non-negative integer query param → (value, None) or
# (None, (response, 400)), the same "return the error as a
# ready tuple" shape api.parse_pagination uses. An absent
# param takes `default`; anything that is not a run of ASCII
# digits is a 400 (bare int() would swallow "3_0", " 3" and
# Unicode digits); a number outside [minimum, maximum] is
# CLAMPED rather than rejected, so a client asking for
# 10 000 lessons simply gets the cap.
#
# Used by:
#   - get_schedule (below) — ?limit and ?offset
############################################################

def _parse_count(raw, name, default, minimum, maximum):
    if raw is None:
        return default, None
    if not _DIGITS_RE.fullmatch(raw):
        return None, (jsonify({"error": f"Parameter '{name}' must be a non-negative integer"}), 400)
    return min(max(int(raw), minimum), maximum), None








############################################################
# _semester_options
############################################################
#
# The semester labels worth showing, newest first: only the
# ones carrying at least MIN_SEMESTER_LESSONS rows, so a
# single mislabelled lesson never becomes a picker entry.
# COLLATE NOCASE on the ORDER BY, because the plain BINARY
# sort put any lowercase label (the retired "2025-pavasaris"
# seed) above every "2025-R"/"2025-P". Inside the
# "YYYY-P"/"YYYY-R" family the text sort IS chronological —
# R (autumn) follows P (spring) in the same year.
#
# Used by:
#   - get_schedule (below) — [0] is the default semester
#   - get_schedule_filters (below) — the picker values
############################################################

def _semester_options(db):
    rows = db.execute(
        """SELECT semester FROM schedule_lessons
           WHERE semester IS NOT NULL AND semester != ''
           GROUP BY semester
           HAVING COUNT(*) >= ?
           ORDER BY semester COLLATE NOCASE DESC""",
        (MIN_SEMESTER_LESSONS,),
    ).fetchall()

    return [r["semester"] for r in rows]








############################################################
# _table_version
############################################################
#
# A cheap "has anything changed" fingerprint of
# schedule_lessons — row count plus the newest created_at —
# for the ETag seeds. Derived from the DATA, never from the
# response body: app/__init__.py's escape_json_output hook
# rewrites the body after the view returns, so a body hash
# would describe bytes this function never sees.
#
# Used by:
#   - get_schedule, get_schedule_filters (below)
############################################################

def _table_version(db):
    row = db.execute(
        "SELECT COUNT(*) AS rows_total, MAX(created_at) AS newest FROM schedule_lessons"
    ).fetchone()

    return f"{row['rows_total']}:{row['newest'] or '-'}"








############################################################
# _conditional_json
############################################################
#
# Wraps a payload in a cacheable response: a weak ETag over
# `seed` (the data version plus the query that shaped the
# answer) and Cache-Control: public, max-age=CACHE_MAX_AGE.
# A matching If-None-Match answers 304 with no body — the
# timetable is identical bytes between two 6 h scrapes.
# Weak on purpose: escape_json_output re-serialises the body
# afterwards, so the tag identifies the DATA, not an
# octet-exact entity.
#
# Used by:
#   - get_schedule, get_schedule_filters (below)
############################################################

def _conditional_json(payload, seed):
    tag = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]

    if request.if_none_match.contains_weak(tag):
        response = make_response("", 304)
    else:
        response = make_response(jsonify(payload))

    response.set_etag(tag, weak=True)
    response.headers["Cache-Control"] = f"public, max-age={CACHE_MAX_AGE}"

    return response








############################################################
# get_schedule
############################################################
#
# GET /api/schedule
#
# Query ?day=0..6&group=ISKS-1&semester=2025-P&limit=&offset=
# — every one optional. day must be ASCII digits inside
# 0..6 or it is a 400; group/semester are exact string
# matches, and an empty value counts as "no filter"; limit
# and offset must be ASCII digits too (400 otherwise) and
# are clamped into 1..MAX_LESSONS / 0..MAX_OFFSET. The WHERE
# clause is assembled with an f-string, but only from fixed
# "column = ?" fragments — every user value is bound, and
# "1=1" stands in when nothing is filtered.
#
# Two behaviours worth knowing:
#   - no ?semester means the NEWEST semester carrying
#     MIN_SEMESTER_LESSONS rows, not "everything ever
#     scraped"; an unfiltered call used to interleave years.
#     ?semester=all is the explicit opt-out for a client that
#     really does want every semester at once
#   - the answer is ONE page of at most MAX_LESSONS rows
#     ordered day_of_week, time_start, group_name, id, so
#     days no longer interleave while the client keeps
#     grouping by dayOfWeek itself
#
# Used by:
#   - services/api/schedule.ts — fetchSchedule, called from
#     app/(main)/tabs/schedule.tsx with the selected day and
#     the group/semester picks (it sends neither limit nor
#     offset, so only the cap applies)
#   - swagger/swagger.yaml documents it
############################################################

@schedule_bp.route("", methods=["GET"])
def get_schedule():
    # STEP 1: read and validate the filters — every bad one
    # is refused before any DB work
    # ======================================================
    day_raw = request.args.get("day")
    group = request.args.get("group")
    semester = request.args.get("semester")

    day = None
    if day_raw is not None:
        if not _DIGITS_RE.fullmatch(day_raw):
            return jsonify({"error": "Parameter 'day' must be an integer (0=Monday..6=Sunday)"}), 400
        day = int(day_raw)
        if day < 0 or day > 6:
            return jsonify({"error": "Parameter 'day' must be between 0 (Monday) and 6 (Sunday)"}), 400

    limit, err = _parse_count(request.args.get("limit"), "limit", MAX_LESSONS, 1, MAX_LESSONS)
    if err:
        return err

    offset, err = _parse_count(request.args.get("offset"), "offset", 0, 0, MAX_OFFSET)
    if err:
        return err


    # STEP 2: default the semester to the newest real one —
    # the app sends none and used to get every year at once;
    # "all" is the way back to the old every-semester answer
    # ======================================================
    db = get_db()
    try:
        if semester and semester.strip().lower() == "all":
            semester = None
        elif not semester:
            options = _semester_options(db)
            semester = options[0] if options else None


        # STEP 3: assemble the WHERE from fixed fragments —
        # the values themselves are always bound parameters
        # =================================================
        where = []
        params = []

        if day is not None:
            where.append("day_of_week = ?")
            params.append(day)
        if group:
            where.append("group_name = ?")
            params.append(group)
        if semester:
            where.append("semester = ?")
            params.append(semester)

        where_sql = " AND ".join(where) if where else "1=1"


        # STEP 4: fetch one capped page and camelCase it —
        # the nine columns the wire shape actually uses, and
        # a total order so days never interleave (time_start
        # sorts as text, chronological only because every
        # writer zero-pads the hour)
        # ==================================================
        rows = db.execute(
            f"""SELECT id, title, teacher, room, time_start, time_end,
                       day_of_week, group_name, semester
                FROM schedule_lessons
                WHERE {where_sql}
                ORDER BY day_of_week, time_start, group_name, id
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()

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


        # STEP 5: answer through the ETag — the same query
        # against an unchanged table costs a 304. The filters
        # go in as a repr'd tuple, not pipe-joined text: a
        # "|" inside a group name used to let
        # ?group=a|b&semester=c and ?group=a&semester=b|c
        # hash the same seed, so one of them answered 304 to
        # the other one's cached copy
        # ====================================================
        seed = f"schedule|{_table_version(db)}|{(day, group, semester, limit, offset)!r}"

        return _conditional_json({"lessons": lessons}, seed)
    finally:
        db.close()








############################################################
# get_schedule_filters
############################################################
#
# GET /api/schedule/filters
#
# {"groups": [...], "semesters": [...], "days": [...],
# "semesterGroups": [{semester, groups}]} — the filter sheet
# in one call. groups and semesters keep their old meaning
# (the mobile contract), days is the DISTINCT day_of_week
# list the client needs to know Saturday lectures exist at
# all, and semesterGroups correlates the two lists that used
# to be independent global DISTINCTs — most hand-made
# group×semester pairs match nothing.
#
# An optional ?semester= scopes groups and days to that one
# label; semesters and semesterGroups always describe the
# whole table. Only semesters that clear
# MIN_SEMESTER_LESSONS are listed (and only those appear in
# semesterGroups), so a stray mislabelled lesson no longer
# invents a semester.
#
# Used by:
#   - services/api/schedule.ts — fetchScheduleFilters, the
#     group/semester pickers in app/(main)/tabs/schedule.tsx
#     (it sends no ?semester; days/semesterGroups are the
#     additive half a mobile-side change can pick up)
#   - swagger/swagger.yaml documents it
############################################################

@schedule_bp.route("/filters", methods=["GET"])
def get_schedule_filters():
    semester = request.args.get("semester") or None

    db = get_db()
    try:
        # STEP 1: the semester options, newest first and past
        # the stray-label threshold
        # ===================================================
        semesters = _semester_options(db)


        # STEP 2: groups and days — scoped to ?semester when
        # one is given; the fragment is fixed text, the label
        # itself is bound
        # ==================================================
        scope_sql = " AND semester = ?" if semester else ""
        scope_params = (semester,) if semester else ()

        groups = [
            r["group_name"]
            for r in db.execute(
                "SELECT DISTINCT group_name FROM schedule_lessons "
                "WHERE group_name IS NOT NULL" + scope_sql + " ORDER BY group_name",
                scope_params,
            ).fetchall()
        ]
        days = [
            r["day_of_week"]
            for r in db.execute(
                "SELECT DISTINCT day_of_week FROM schedule_lessons "
                "WHERE day_of_week IS NOT NULL" + scope_sql + " ORDER BY day_of_week",
                scope_params,
            ).fetchall()
        ]


        # STEP 3: which groups really exist in which semester;
        # labels below the threshold are dropped here too
        # ====================================================
        known = set(semesters)
        by_semester = {}

        for r in db.execute(
            """SELECT semester, group_name FROM schedule_lessons
               WHERE semester IS NOT NULL AND group_name IS NOT NULL
               GROUP BY semester, group_name
               ORDER BY group_name"""
        ).fetchall():
            if r["semester"] in known:
                by_semester.setdefault(r["semester"], []).append(r["group_name"])

        semester_groups = [
            {"semester": s, "groups": by_semester.get(s, [])}
            for s in semesters
        ]


        # STEP 4: answer through the ETag — these values only
        # move when the scraper writes
        # ===================================================
        payload = {
            "groups": groups,
            "semesters": semesters,
            "days": days,
            "semesterGroups": semester_groups,
        }
        seed = f"filters|{_table_version(db)}|{semester}"

        return _conditional_json(payload, seed)
    finally:
        db.close()
