"""Schedule/timetable API."""

from flask import Blueprint, jsonify, request

from app.auth.routes import require_role
from app.database import get_db

schedule_bp = Blueprint("schedule", __name__)


@schedule_bp.route("", methods=["GET"])
def get_schedule():
    """
    Get schedule for a given day.

    Query params:
      - day (int, 0=Monday..6=Sunday)
      - group (str, optional filter by group)
      - semester (str, optional filter by semester)
    """
    day_raw = request.args.get("day")
    group = request.args.get("group")
    semester = request.args.get("semester")

    # Validate day param if provided
    day = None
    if day_raw is not None:
        try:
            day = int(day_raw)
        except (ValueError, TypeError):
            return jsonify({"error": "Parameter 'day' must be an integer (0=Monday..6=Sunday)"}), 400
        if day < 0 or day > 6:
            return jsonify({"error": "Parameter 'day' must be between 0 (Monday) and 6 (Sunday)"}), 400

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


@schedule_bp.route("/filters", methods=["GET"])
def get_schedule_filters():
    """
    Get available groups and semesters for the schedule filter UI.

    Returns:
      { "groups": ["ISKS-1", ...], "semesters": ["2025-P", ...] }
    """
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


@schedule_bp.route("/seed", methods=["POST"])
@require_role("admin")
def seed_schedule():
    """Seed the schedule with demo data (for development)."""
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
