"""Schedule/timetable API."""

from flask import Blueprint, jsonify, request

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
    day = request.args.get("day", type=int)
    group = request.args.get("group")
    semester = request.args.get("semester")

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


@schedule_bp.route("/seed", methods=["POST"])
def seed_schedule():
    """Seed the schedule with demo data (for development)."""
    import uuid

    demo_lessons = [
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
