############################################################
#  [*] Schedule — the lecture timetable API
#
#  Read side of schedule_lessons: the filtered lesson list
#  and the distinct group/semester values behind the mobile
#  filter sheet. No login on the reads — the timetable is
#  the app's "works without an account" screen; only the
#  demo seed is admin-gated.
#
#  Who writes the table: scraper/schedule_scraper.py
#  (scrape_knf_schedule — 30 s after boot and every 6 h via
#  scraper/scheduler.py, or POST /api/scraper/schedule),
#  database/__init__.py _seed_defaults (11 first-boot rows
#  for ISKS-1 under the odd label "2025-pavasaris"), and
#  seed_schedule below. Nothing ever deletes rows: stale
#  semesters accumulate, and the /seed fixtures stack up
#  again on every call.
#
#  Times are "HH:MM" wall-clock strings, day_of_week is
#  0 = Monday … 6 = Sunday (a CHECK on the table), and
#  semester labels follow the scraper's "YYYY-P" (spring)
#  / "YYYY-R" (autumn) shape. The JSON is camelCased:
#  group_name → group, time_start → timeStart.
#
#    GET  /api/schedule         — lessons, optionally filtered
#    GET  /api/schedule/filters — distinct groups + semesters
#    POST /api/schedule/seed    — 31 demo lessons (admin)
############################################################


from flask import Blueprint, jsonify, request

from app.auth.routes import require_role
from app.database import get_db

schedule_bp = Blueprint("schedule", __name__)








############################################################
# get_schedule
############################################################
#
# GET /api/schedule
#
# Query ?day=0..6&group=ISKS-1&semester=2025-P — every one
# optional, so no params returns the WHOLE table (all days,
# all groups). day must parse as an int in 0..6 or it is a
# 400; group/semester are exact string matches, and an
# empty value counts as "no filter". The WHERE clause is
# assembled with an f-string, but only from fixed
# "column = ?" fragments — every user value is bound, and
# "1=1" stands in when nothing is filtered.
#
# Rows come ORDER BY time_start alone: the "HH:MM" strings
# are zero-padded, so the text sort is chronological, but
# an unfiltered call has no day ordering — the client
# groups by dayOfWeek itself.
#
# Used by:
#   - services/api/schedule.ts — fetchSchedule, called from
#     app/(main)/tabs/schedule.tsx with the selected day and
#     the group/semester picks
#   - swagger/swagger.yaml documents it
############################################################

@schedule_bp.route("", methods=["GET"])
def get_schedule():
    # STEP 1: read the filters — only day needs parsing, and
    # a bad one is refused before any DB work
    # ======================================================
    day_raw = request.args.get("day")
    group = request.args.get("group")
    semester = request.args.get("semester")

    day = None
    if day_raw is not None:
        try:
            day = int(day_raw)
        except (ValueError, TypeError):
            return jsonify({"error": "Parameter 'day' must be an integer (0=Monday..6=Sunday)"}), 400
        if day < 0 or day > 6:
            return jsonify({"error": "Parameter 'day' must be between 0 (Monday) and 6 (Sunday)"}), 400


    # STEP 2: assemble the WHERE from fixed fragments — the
    # values themselves are always bound parameters
    # =====================================================
    db = get_db()
    try:
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


        # STEP 3: fetch and camelCase — time_start sorts as
        # text, chronological only because every writer
        # zero-pads the hour
        # =================================================
        rows = db.execute(
            f"""SELECT * FROM schedule_lessons
                WHERE {where_sql}
                ORDER BY time_start""",
            params,
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

        return jsonify({"lessons": lessons})
    finally:
        db.close()








############################################################
# get_schedule_filters
############################################################
#
# GET /api/schedule/filters
#
# {"groups": [...], "semesters": [...]} — DISTINCT non-NULL
# values straight off schedule_lessons, groups ascending
# and semesters descending. Both are plain text sorts under
# SQLite's BINARY collation, so newest-first holds inside
# the "YYYY-P"/"YYYY-R" family, but the first-boot label
# "2025-pavasaris" (lowercase p outranks every capital)
# sorts ABOVE "2025-R" and "2025-P" for as long as those
# seed rows exist.
#
# Used by:
#   - services/api/schedule.ts — fetchScheduleFilters, the
#     group/semester pickers in app/(main)/tabs/schedule.tsx
#   - swagger/swagger.yaml documents it
############################################################

@schedule_bp.route("/filters", methods=["GET"])
def get_schedule_filters():
    db = get_db()
    try:
        groups = [
            r["group_name"]
            for r in db.execute(
                "SELECT DISTINCT group_name FROM schedule_lessons WHERE group_name IS NOT NULL ORDER BY group_name"
            ).fetchall()
        ]
        semesters = [
            r["semester"]
            for r in db.execute(
                "SELECT DISTINCT semester FROM schedule_lessons WHERE semester IS NOT NULL ORDER BY semester DESC"
            ).fetchall()
        ]
        return jsonify({"groups": groups, "semesters": semesters})
    finally:
        db.close()








############################################################
# seed_schedule
############################################################
#
# POST /api/schedule/seed
#
# Admin-only development fixture: 31 hard-coded lessons —
# ISKS-1 (11), ISKS-2 (8) and VVB-1 (7) for spring "2025-P"
# plus 5 ISKS-1 rows for autumn "2025-R" — each inserted
# under a fresh uuid4. Answers {"message": "Seeded 31
# lessons"}.
#
# Gotchas:
#   - INSERT OR IGNORE only guards the primary key, and the
#     key is minted per call, so nothing is ever ignored:
#     every POST appends all 31 rows again, duplicates and
#     all, and no route deletes them.
#   - the labels DO match the scraper's "YYYY-P"/"YYYY-R"
#     format, so these rows blend into the real filter
#     values; the first-boot _seed_defaults set (semester
#     "2025-pavasaris") is a separate, unrelated fixture.
#   - the scraper's own dedupe compares all eight columns,
#     so it neither collides with nor cleans up these rows.
#
# Used by:
#   - nothing calls this at the moment — not the mobile app,
#     not swagger, no main.py flag; it is a manual curl with
#     an admin bearer token
############################################################

@schedule_bp.route("/seed", methods=["POST"])
@require_role("admin")
def seed_schedule():
    # STEP 1: the fixture — tuple order is (title, teacher,
    # room, time_start, time_end, day_of_week, group,
    # semester); uuid is a local import, nothing else in the
    # module needs it
    # ======================================================
    import uuid

    demo_lessons = [
        # ISKS-1, 2025-P (Spring)
        ("Programavimo pagrindai", "Doc. J. Kazlauskas", "207", "08:30", "10:00", 0, "ISKS-1", "2025-P"),
        ("Duomenų bazės", "Lekt. I. Petrauskaitė", "105", "10:15", "11:45", 0, "ISKS-1", "2025-P"),
        ("Tinklų pagrindai", "Asist. K. Jonaitis", "Lab-3", "12:00", "13:30", 0, "ISKS-1", "2025-P"),
        ("Diskrečioji matematika", "Prof. V. Matulis", "Aula", "14:00", "15:30", 0, "ISKS-1", "2025-P"),
        ("Objektinis programavimas", "Doc. J. Kazlauskas", "207", "08:30", "10:00", 1, "ISKS-1", "2025-P"),
        ("Kompiuterių architektūra", "Doc. A. Rimkus", "Lab-2", "10:15", "11:45", 1, "ISKS-1", "2025-P"),
        ("Anglų kalba", "Lekt. S. Brown", "301", "12:00", "13:30", 2, "ISKS-1", "2025-P"),
        ("Statistika", "Prof. V. Matulis", "Aula", "08:30", "10:00", 2, "ISKS-1", "2025-P"),
        ("Programavimo pagrindai (Lab)", "Doc. J. Kazlauskas", "Lab-1", "10:15", "11:45", 3, "ISKS-1", "2025-P"),
        ("Web technologijos", "Asist. K. Jonaitis", "Lab-3", "12:00", "13:30", 3, "ISKS-1", "2025-P"),
        ("Duomenų bazės (Lab)", "Lekt. I. Petrauskaitė", "Lab-2", "14:00", "15:30", 4, "ISKS-1", "2025-P"),
        # ISKS-2, 2025-P (Spring)
        ("Operacinės sistemos", "Doc. A. Rimkus", "207", "08:30", "10:00", 0, "ISKS-2", "2025-P"),
        ("Algoritmų analizė", "Prof. V. Matulis", "Aula", "10:15", "11:45", 0, "ISKS-2", "2025-P"),
        ("Programų inžinerija", "Doc. J. Kazlauskas", "105", "12:00", "13:30", 1, "ISKS-2", "2025-P"),
        ("Duomenų struktūros", "Lekt. I. Petrauskaitė", "Lab-2", "08:30", "10:00", 1, "ISKS-2", "2025-P"),
        ("Tinklų saugumas", "Asist. K. Jonaitis", "Lab-3", "10:15", "11:45", 2, "ISKS-2", "2025-P"),
        ("Anglų kalba B2", "Lekt. S. Brown", "301", "12:00", "13:30", 2, "ISKS-2", "2025-P"),
        ("Programų inžinerija (Lab)", "Doc. J. Kazlauskas", "Lab-1", "08:30", "10:00", 3, "ISKS-2", "2025-P"),
        ("Operacinės sistemos (Lab)", "Doc. A. Rimkus", "Lab-2", "10:15", "11:45", 4, "ISKS-2", "2025-P"),
        # VVB-1, 2025-P (Spring) — Business management group
        ("Mikroekonomika", "Prof. R. Jankauskienė", "Aula", "08:30", "10:00", 0, "VVB-1", "2025-P"),
        ("Verslo teisė", "Lekt. D. Stankevičius", "105", "10:15", "11:45", 0, "VVB-1", "2025-P"),
        ("Apskaita ir finansai", "Doc. L. Navickienė", "207", "08:30", "10:00", 1, "VVB-1", "2025-P"),
        ("Rinkodaros pagrindai", "Lekt. M. Žukauskaitė", "301", "10:15", "11:45", 2, "VVB-1", "2025-P"),
        ("Vadyba", "Prof. R. Jankauskienė", "Aula", "12:00", "13:30", 2, "VVB-1", "2025-P"),
        ("Verslo teisė (Sem.)", "Lekt. D. Stankevičius", "105", "08:30", "10:00", 3, "VVB-1", "2025-P"),
        ("Statistika versle", "Prof. V. Matulis", "207", "10:15", "11:45", 4, "VVB-1", "2025-P"),
        # ISKS-1, 2025-R (Autumn — previous semester for testing)
        ("Informacinės technologijos", "Asist. K. Jonaitis", "Lab-3", "08:30", "10:00", 0, "ISKS-1", "2025-R"),
        ("Matematinė analizė", "Prof. V. Matulis", "Aula", "10:15", "11:45", 0, "ISKS-1", "2025-R"),
        ("Fizika", "Doc. P. Lapinskienė", "207", "12:00", "13:30", 1, "ISKS-1", "2025-R"),
        ("Informacinės technologijos (Lab)", "Asist. K. Jonaitis", "Lab-1", "08:30", "10:00", 2, "ISKS-1", "2025-R"),
        ("Matematinė analizė (Prat.)", "Prof. V. Matulis", "105", "10:15", "11:45", 3, "ISKS-1", "2025-R"),
    ]


    # STEP 2: insert under per-row uuid4 ids and one commit —
    # see the banner on why OR IGNORE never ignores anything
    # =======================================================
    db = get_db()
    try:
        for lesson in demo_lessons:
            db.execute(
                """INSERT OR IGNORE INTO schedule_lessons
                   (id, title, teacher, room, time_start, time_end, day_of_week, group_name, semester)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), *lesson),
            )
        db.commit()
        return jsonify({"message": f"Seeded {len(demo_lessons)} lessons"})
    finally:
        db.close()
