############################################################
#  [*] Tests — schedule_scraper's STORE layer (deep pass)
#
#  The four functions that own everything schedule_scraper
#  writes, driven directly instead of through a scrape:
#
#    _insert_lessons       — the OR IGNORE batch writer
#    _reconcile_partition  — the (group, semester) rewrite
#    _semester_key         — the academic sort key
#    _purge_old_semesters  — retiring finished semesters
#
#  What this module proves:
#
#    - the NATURAL KEY is exactly the eight columns migration
#      v18 indexes: a batch differing in any ONE of them is a
#      second row, an identical batch is absorbed, and a NULL
#      lecturer defeats the index entirely — which is the
#      whole reason the parser writes "" and never None
#    - the COUNTS are set arithmetic taken BEFORE the write,
#      so an unchanged timetable reports (0, 0), never
#      "everything is new", and the numbers describe the
#      SNAPSHOT even when the table moved underneath
#    - the PARTITION is the unit: reconciliation deletes and
#      rewrites its own (group_name, semester) pair and
#      nothing else, case included, and an unchanged one is
#      not deleted and re-inserted behind the scenes
#    - NEITHER writer commits — the caller owns the
#      transaction — except the purge, which commits exactly
#      when it retired something, taking its caller's pending
#      work with it
#    - the PURGE keys on the academic year, not on text:
#      "2025-R" (autumn 2025) is older than "2025-P" (spring
#      2026) although it sorts after it, and a label this
#      scraper never wrote is left for an admin
#    - the UNHAPPY paths: a lesson missing a field, a batch
#      that is not a list, a connection with the wrong row
#      factory, a constraint the OR IGNORE swallows, and a
#      second writer holding the database
#
#  Nothing here touches the network or the clock: these four
#  functions are pure SQL over the `db` fixture's connection.
############################################################


import logging
import sqlite3

import pytest

from app.scraper import schedule_scraper as ss


# The partition nearly every write test rewrites
GROUP = "ISKS-1"
SEMESTER = "2025-P"

# The eight columns migration v18 made a unique index out of.
# Every one of them is part of a lesson's identity, so a batch
# differing in any single one is two rows, not one
IDENTITY_FIELDS = ("title", "teacher", "room", "time_start",
                   "time_end", "day_of_week", "group_name", "semester")




############################################################
# _lesson
############################################################
#
# One scraped lesson dict in exactly the shape
# scrape_group_schedule hands the write phase — every field
# overridable, so a test can vary one column and leave the
# other seven pinned.
#
# Used by:
#   - every write test below
############################################################

def _lesson(**overrides):
    lesson = {"title": "Programavimas", "teacher": "A. Petraitis", "room": "301",
              "time_start": "08:30", "time_end": "10:00", "day_of_week": 0,
              "group_name": GROUP, "semester": SEMESTER}
    lesson.update(overrides)
    return lesson




############################################################
# _rows
############################################################
#
# Every stored lesson as a comparable tuple in the identity
# order above, so a whole table can be asserted in one
# equality. Reads through whichever connection it is given.
#
# Used by:
#   - every write test below
############################################################

def _rows(conn, semester=None):
    sql = ("SELECT title, teacher, room, time_start, time_end, day_of_week, group_name, semester"
           " FROM schedule_lessons")
    params = []
    if semester is not None:
        sql += " WHERE semester = ?"
        params.append(semester)
    sql += " ORDER BY semester, group_name, day_of_week, time_start, title"

    return [tuple(row) for row in conn.execute(sql, params).fetchall()]




############################################################
# _open / _visible
############################################################
#
# A SECOND connection to the same test database. Everything
# these four functions do happens inside the caller's
# transaction, so "did this actually land" can only be asked
# from outside it — _visible opens a connection, reads the
# committed table and closes again.
#
# `timeout` is left at the caller's disposal: 0 turns the
# writer-blocks-writer case into an immediate error instead
# of a five-second wait.
#
# Used by:
#   - the transaction and lock tests
############################################################

def _open(app, timeout=5.0):
    conn = sqlite3.connect(app.config["DB_PATH"], timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def _visible(app):
    conn = _open(app)
    try:
        return _rows(conn)
    finally:
        conn.close()




############################################################
# _RecordingDb
############################################################
#
# A pass-through wrapper over the test connection keeping
# every statement the code under test issued, so a test can
# PROVE the DELETE was skipped rather than infer it from the
# rows that happen to have survived.
#
# Used by:
#   - the reconciliation and purge tests
############################################################

class _RecordingDb:

    def __init__(self, real):
        self.real = real
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(" ".join(sql.split()))
        return self.real.execute(sql, params)

    def commit(self):
        self.sql.append("COMMIT")
        self.real.commit()

    def statements(self, verb):
        return [sql for sql in self.sql if sql.startswith(verb)]




############################################################
# _RaceDb
############################################################
#
# _RecordingDb plus a one-shot side effect fired just BEFORE
# the first statement containing a marker — the deterministic
# stand-in for "another writer got in between the snapshot
# and the delete", with no threads and no sleeping.
#
# Used by:
#   - the race tests
############################################################

class _RaceDb(_RecordingDb):

    def __init__(self, real, marker, action):
        super().__init__(real)
        self.marker = marker
        self.action = action

    def execute(self, sql, params=()):
        if self.action and self.marker in " ".join(sql.split()):
            action, self.action = self.action, None
            action()
        return super().execute(sql, params)




# ==========================================================
# _semester_key — the academic sort key
# ==========================================================


def test_the_key_is_the_academic_year_doubled_with_autumn_before_spring():
    assert ss._semester_key("2025-R") == 2025 * 2
    assert ss._semester_key("2025-P") == 2025 * 2 + 1


def test_autumn_sorts_before_the_spring_of_the_same_academic_year():
    # Plain text sorting is the bug this key exists to fix:
    # "2025-P" is spring 2026 and comes AFTER autumn 2025
    assert "2025-P" < "2025-R"
    assert ss._semester_key("2025-R") < ss._semester_key("2025-P")


def test_the_keys_of_four_consecutive_terms_increase_with_the_calendar():
    labels = ["2024-R", "2024-P", "2025-R", "2025-P", "2026-R", "2026-P"]
    keys = [ss._semester_key(label) for label in labels]

    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_consecutive_terms_are_exactly_one_apart():
    assert ss._semester_key("2025-P") - ss._semester_key("2025-R") == 1
    assert ss._semester_key("2026-R") - ss._semester_key("2025-P") == 1


def test_the_earliest_and_latest_four_digit_labels_still_key():
    assert ss._semester_key("0000-R") == 0
    assert ss._semester_key("0000-P") == 1
    assert ss._semester_key("9999-P") == 9999 * 2 + 1


@pytest.mark.parametrize("label", [
    "2025-pavasaris",   # the legacy seed shape
    "2025-ruduo",
    "2025-r",           # lower case is not a label this scraper writes
    "2025-p",
    "2025-Z",           # a term letter that does not exist
    "2025-",
    "2025",
    "-R",
    "25-R",             # two digits
    "202-R",            # three
    "20255-R",          # five
    "2025-RR",          # fullmatch, so a trailing letter is fatal
    " 2025-R",          # surrounding whitespace is not trimmed
    "2025-R ",
    "2025-R\n",         # fullmatch has no $ leniency for a newline
    "2025/R",
    "2025_R",
    "Rudens semestras",
])
def test_a_label_this_scraper_never_wrote_has_no_key(label):
    assert ss._semester_key(label) is None


@pytest.mark.parametrize("label", [None, "", 0, False, [], {}])
def test_a_falsy_label_collapses_to_the_empty_string_and_keys_to_none(label):
    # `label or ""` is what keeps a None out of re.fullmatch
    assert ss._semester_key(label) is None


@pytest.mark.parametrize("label", [2025, 20250, ["2025-R"], {"2025-R"}, object()])
def test_a_truthy_non_string_label_reaches_the_regex_and_raises(label):
    with pytest.raises(TypeError):
        ss._semester_key(label)


def test_a_very_long_label_is_simply_not_one_of_ours():
    assert ss._semester_key("2025-R" * 5000) is None


def test_the_year_group_is_read_as_a_number_not_as_text():
    # Non-ASCII decimal digits satisfy \d and int() alike. No
    # scraper path can produce one — _get_semester_label
    # formats an int — but it pins that the key is numeric
    assert ss._semester_key("٢٠٢٥-R") == ss._semester_key("2025-R")




# ==========================================================
# _insert_lessons — the OR IGNORE batch writer
# ==========================================================


def test_an_empty_batch_inserts_nothing_and_issues_no_statement(app, db):
    recorder = _RecordingDb(db)

    assert ss._insert_lessons(recorder, []) == 0

    assert recorder.sql == []
    assert _rows(db) == []


def test_one_lesson_is_stored_column_for_column(app, db):
    assert ss._insert_lessons(db, [_lesson()]) == 1
    db.commit()

    assert _rows(db) == [("Programavimas", "A. Petraitis", "301",
                          "08:30", "10:00", 0, GROUP, SEMESTER)]


def test_every_row_gets_its_own_generated_id_and_a_created_at(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(day_of_week=1)])
    db.commit()

    rows = db.execute("SELECT id, created_at FROM schedule_lessons").fetchall()
    ids = {row["id"] for row in rows}
    assert len(ids) == 2
    assert all(len(row["id"]) == 36 for row in rows)
    assert all(row["created_at"] for row in rows)


@pytest.mark.parametrize("field, value", [
    ("title", "Duomenų bazės"),
    ("teacher", "B. Jonaitis"),
    ("room", "402"),
    ("time_start", "10:15"),
    ("time_end", "11:45"),
    ("day_of_week", 3),
    ("group_name", "EV-2"),
    ("semester", "2026-R"),
])
def test_a_batch_differing_in_one_identity_column_is_two_rows(app, db, field, value):
    assert ss._insert_lessons(db, [_lesson(), _lesson(**{field: value})]) == 2
    db.commit()

    assert len(_rows(db)) == 2


def test_the_same_lesson_twice_inside_one_batch_counts_once(app, db):
    assert ss._insert_lessons(db, [_lesson(), _lesson()]) == 1
    db.commit()

    assert len(_rows(db)) == 1


def test_a_repeated_batch_adds_nothing_the_second_time(app, db):
    lessons = [_lesson(), _lesson(day_of_week=1), _lesson(day_of_week=2)]

    assert ss._insert_lessons(db, lessons) == 3
    db.commit()
    assert ss._insert_lessons(db, lessons) == 0
    db.commit()

    assert len(_rows(db)) == 3


def test_a_half_new_batch_reports_only_what_it_added(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(day_of_week=1)])
    db.commit()

    added = ss._insert_lessons(db, [_lesson(), _lesson(day_of_week=1),
                                    _lesson(day_of_week=2), _lesson(day_of_week=3)])
    db.commit()

    assert added == 2
    assert len(_rows(db)) == 4


def test_a_lesson_with_every_string_field_empty_still_stores_and_dedupes(app, db):
    blank = _lesson(title="", teacher="", room="", time_start="", time_end="",
                    group_name="", semester="")

    assert ss._insert_lessons(db, [blank]) == 1
    assert ss._insert_lessons(db, [blank]) == 0
    db.commit()

    assert len(_rows(db)) == 1


def test_a_null_lecturer_defeats_the_unique_index_on_every_run(app, db):
    # SQLite counts NULLs in a unique index as distinct, so a
    # NULL teacher re-inserts a fresh duplicate every six hours
    # — which is exactly why the parser coerces to ""
    lessons = [_lesson(teacher=None)]

    assert ss._insert_lessons(db, lessons) == 1
    assert ss._insert_lessons(db, lessons) == 1
    db.commit()

    assert len(_rows(db)) == 2


def test_a_null_room_defeats_it_the_same_way(app, db):
    lessons = [_lesson(room=None)]

    assert ss._insert_lessons(db, lessons) == 1
    assert ss._insert_lessons(db, lessons) == 1
    db.commit()

    assert len(_rows(db)) == 2


def test_an_empty_lecturer_and_a_null_one_are_two_different_rows(app, db):
    assert ss._insert_lessons(db, [_lesson(teacher=""), _lesson(teacher=None)]) == 2
    db.commit()

    assert len(_rows(db)) == 2


@pytest.mark.parametrize("day", [0, 6])
def test_the_weekday_boundaries_the_check_constraint_allows_are_stored(app, db, day):
    assert ss._insert_lessons(db, [_lesson(day_of_week=day)]) == 1
    db.commit()

    assert _rows(db)[0][5] == day


@pytest.mark.parametrize("day", [-1, 7, 99])
def test_a_weekday_outside_the_check_constraint_is_swallowed_by_or_ignore(app, db, day):
    # OR IGNORE skips a CHECK violation as quietly as a UNIQUE
    # one: the row is dropped, the count says 0, nothing raises
    assert ss._insert_lessons(db, [_lesson(day_of_week=day)]) == 0
    db.commit()

    assert _rows(db) == []


def test_a_weekday_written_as_text_lands_as_the_integer_it_dedupes_against(app, db):
    # The column has INTEGER affinity, so "0" is stored as 0 and
    # collides with the row already there
    assert ss._insert_lessons(db, [_lesson()]) == 1
    db.commit()

    assert ss._insert_lessons(db, [_lesson(day_of_week="0")]) == 0
    db.commit()

    assert len(_rows(db)) == 1


@pytest.mark.parametrize("field", ["title", "time_start", "time_end", "day_of_week"])
def test_a_null_in_a_not_null_column_is_swallowed_by_or_ignore(app, db, field):
    assert ss._insert_lessons(db, [_lesson(**{field: None})]) == 0
    db.commit()

    assert _rows(db) == []


def test_a_dropped_row_does_not_stop_the_rest_of_the_batch(app, db):
    added = ss._insert_lessons(db, [_lesson(day_of_week=9),
                                    _lesson(day_of_week=1),
                                    _lesson(title=None),
                                    _lesson(day_of_week=2)])
    db.commit()

    assert added == 2
    assert sorted(row[5] for row in _rows(db)) == [1, 2]


def test_a_huge_title_is_stored_whole(app, db):
    title = "Ą" * 100_000

    assert ss._insert_lessons(db, [_lesson(title=title)]) == 1
    db.commit()

    assert db.execute("SELECT title FROM schedule_lessons").fetchone()[0] == title


def test_a_three_hundred_lesson_batch_is_counted_row_by_row(app, db):
    lessons = [_lesson(title=f"Dalykas {n}", day_of_week=n % 7) for n in range(300)]

    assert ss._insert_lessons(db, lessons) == 300
    db.commit()

    assert len(_rows(db)) == 300
    assert ss._insert_lessons(db, lessons) == 0


def test_extra_keys_on_a_lesson_dict_are_ignored(app, db):
    lesson = _lesson()
    lesson["colour"] = "#ff899d"
    lesson["id"] = "not-the-id-that-is-used"

    assert ss._insert_lessons(db, [lesson]) == 1
    db.commit()

    assert db.execute("SELECT id FROM schedule_lessons").fetchone()[0] != "not-the-id-that-is-used"


def test_a_lesson_missing_an_identity_field_raises_and_strands_the_batch(app, db):
    broken = {key: value for key, value in _lesson().items() if key != "room"}

    with pytest.raises(KeyError):
        ss._insert_lessons(db, [_lesson(day_of_week=1), broken, _lesson(day_of_week=2)])

    # The rows before the bad one are already in the caller's
    # open transaction — undoing them is the caller's job
    assert len(_rows(db)) == 1
    db.rollback()
    assert _rows(db) == []


def test_a_batch_that_is_not_iterable_raises_before_anything_is_written(app, db):
    with pytest.raises(TypeError):
        ss._insert_lessons(db, None)

    assert _rows(db) == []


def test_the_writer_leaves_the_commit_to_its_caller(app, db):
    assert ss._insert_lessons(db, [_lesson()]) == 1

    assert _visible(app) == []
    db.commit()
    assert len(_visible(app)) == 1


def test_a_rolled_back_batch_leaves_no_trace(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(day_of_week=1)])
    db.rollback()

    assert _rows(db) == []
    assert _visible(app) == []


def test_a_write_blocked_by_another_writer_surfaces_as_an_error(app, db):
    # The deterministic form of "a parallel run holds the
    # database": a zero timeout turns the wait into an immediate
    # OperationalError, which _insert_lessons does not catch
    blocker = _open(app)
    writer = _open(app, timeout=0)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "INSERT INTO schedule_lessons (id, title, time_start, time_end, day_of_week)"
            " VALUES ('blocker', 'T', '08:30', '10:00', 0)")

        with pytest.raises(sqlite3.OperationalError):
            ss._insert_lessons(writer, [_lesson()])
    finally:
        blocker.rollback()
        blocker.close()
        writer.close()

    assert _visible(app) == []




# ==========================================================
# _reconcile_partition — the (group, semester) rewrite
# ==========================================================


def test_reconciling_an_empty_partition_with_nothing_touches_nothing(app, db):
    recorder = _RecordingDb(db)

    assert ss._reconcile_partition(recorder, GROUP, SEMESTER, []) == (0, 0)

    assert recorder.statements("DELETE") == []
    assert recorder.statements("INSERT") == []
    assert _rows(db) == []


def test_a_first_import_reports_every_lesson_as_added(app, db):
    lessons = [_lesson(), _lesson(day_of_week=1), _lesson(day_of_week=2)]

    assert ss._reconcile_partition(db, GROUP, SEMESTER, lessons) == (3, 0)
    db.commit()

    assert len(_rows(db)) == 3


def test_an_unchanged_partition_reports_nothing_and_is_never_deleted(app, db):
    lessons = [_lesson(), _lesson(day_of_week=1)]
    ss._insert_lessons(db, lessons)
    db.commit()
    before = db.execute("SELECT rowid, id, created_at FROM schedule_lessons ORDER BY rowid").fetchall()

    recorder = _RecordingDb(db)
    assert ss._reconcile_partition(recorder, GROUP, SEMESTER, lessons) == (0, 0)
    db.commit()

    assert recorder.statements("DELETE") == []
    assert recorder.statements("INSERT") == []
    after = db.execute("SELECT rowid, id, created_at FROM schedule_lessons ORDER BY rowid").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_the_scraped_order_does_not_make_a_partition_look_changed(app, db):
    lessons = [_lesson(day_of_week=day) for day in range(5)]
    ss._insert_lessons(db, lessons)
    db.commit()

    assert ss._reconcile_partition(db, GROUP, SEMESTER, list(reversed(lessons))) == (0, 0)


def test_a_lesson_that_left_the_feed_is_retired(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(title="Atšaukta", day_of_week=1)])
    db.commit()

    assert ss._reconcile_partition(db, GROUP, SEMESTER, [_lesson()]) == (0, 1)
    db.commit()

    assert [row[0] for row in _rows(db)] == ["Programavimas"]


def test_a_lesson_the_feed_gained_is_added_without_disturbing_the_rest(app, db):
    ss._insert_lessons(db, [_lesson()])
    db.commit()

    assert ss._reconcile_partition(db, GROUP, SEMESTER,
                                   [_lesson(), _lesson(title="Nauja", day_of_week=2)]) == (1, 0)
    db.commit()

    assert sorted(row[0] for row in _rows(db)) == ["Nauja", "Programavimas"]


def test_a_moved_lesson_is_one_added_and_one_removed_not_two_rows(app, db):
    ss._insert_lessons(db, [_lesson(room="301")])
    db.commit()

    assert ss._reconcile_partition(db, GROUP, SEMESTER, [_lesson(room="402")]) == (1, 1)
    db.commit()

    assert _rows(db) == [("Programavimas", "A. Petraitis", "402",
                          "08:30", "10:00", 0, GROUP, SEMESTER)]


def test_an_empty_scrape_empties_only_that_partition(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(day_of_week=1),
                            _lesson(group_name="EV-2"), _lesson(semester="2026-R")])
    db.commit()

    assert ss._reconcile_partition(db, GROUP, SEMESTER, []) == (0, 2)
    db.commit()

    assert sorted((row[6], row[7]) for row in _rows(db)) == [("EV-2", SEMESTER), (GROUP, "2026-R")]


def test_a_partition_whose_name_differs_only_in_case_is_a_different_partition(app, db):
    # The lookup is a plain `=` on TEXT, which SQLite compares
    # byte for byte — no COLLATE NOCASE anywhere
    ss._insert_lessons(db, [_lesson(group_name="isks-1")])
    db.commit()

    assert ss._reconcile_partition(db, GROUP, SEMESTER, []) == (0, 0)
    db.commit()

    assert len(_rows(db)) == 1


def test_duplicate_lessons_in_the_scraped_batch_collapse_into_one_row(app, db):
    assert ss._reconcile_partition(db, GROUP, SEMESTER, [_lesson(), _lesson(), _lesson()]) == (1, 0)
    db.commit()

    assert len(_rows(db)) == 1


def test_the_comparison_key_is_the_full_identity_not_the_time_slot(app, db):
    # Parallel subgroups legitimately share a slot with another
    # teacher and another room
    ss._insert_lessons(db, [_lesson(teacher="A. Petraitis", room="301")])
    db.commit()

    added, removed = ss._reconcile_partition(db, GROUP, SEMESTER, [
        _lesson(teacher="A. Petraitis", room="301"),
        _lesson(teacher="B. Jonaitis", room="402"),
    ])
    db.commit()

    assert (added, removed) == (1, 0)
    assert len(_rows(db)) == 2


def test_a_partition_rewrite_replaces_the_rows_rather_than_updating_them(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(day_of_week=1)])
    db.commit()
    old_ids = {row[0] for row in db.execute("SELECT id FROM schedule_lessons")}

    ss._reconcile_partition(db, GROUP, SEMESTER, [_lesson(), _lesson(day_of_week=3)])
    db.commit()

    new_ids = {row[0] for row in db.execute("SELECT id FROM schedule_lessons")}
    # Even the untouched lesson comes back with a fresh id: the
    # whole partition is deleted and written again
    assert old_ids.isdisjoint(new_ids)


def test_legacy_rows_with_a_null_lecturer_are_rewritten_once_and_then_settle(app, db):
    # A NULL never equals the "" the parser produces, so the
    # first reconciliation rewrites the partition — and the
    # second finds it identical
    ss._insert_lessons(db, [_lesson(teacher=None)])
    db.commit()

    assert ss._reconcile_partition(db, GROUP, SEMESTER, [_lesson(teacher="")]) == (1, 1)
    db.commit()
    assert ss._reconcile_partition(db, GROUP, SEMESTER, [_lesson(teacher="")]) == (0, 0)
    db.commit()

    assert _rows(db) == [("Programavimas", "", "301", "08:30", "10:00", 0, GROUP, SEMESTER)]


def test_two_stored_rows_that_the_index_let_through_count_as_one_removal(app, db):
    # Two NULL-lecturer rows are one entry in the `existing` SET
    # — the removed count is set arithmetic, not a row count
    ss._insert_lessons(db, [_lesson(teacher=None)])
    ss._insert_lessons(db, [_lesson(teacher=None)])
    db.commit()
    assert len(_rows(db)) == 2

    assert ss._reconcile_partition(db, GROUP, SEMESTER, []) == (0, 1)
    db.commit()

    assert _rows(db) == []


def test_a_foreign_lesson_matching_the_six_columns_looks_unchanged_and_is_dropped(app, db):
    # group_name and semester are NOT in the comparison key — the
    # partition supplies them — so a lesson belonging to another
    # group but identical in the other six reads as "no change"
    # and is never written at all
    ss._insert_lessons(db, [_lesson()])
    db.commit()
    recorder = _RecordingDb(db)

    added, removed = ss._reconcile_partition(recorder, GROUP, SEMESTER, [_lesson(group_name="EV-2")])
    db.commit()

    assert (added, removed) == (0, 0)
    assert recorder.statements("INSERT") == []
    assert _rows(db) == [("Programavimas", "A. Petraitis", "301",
                          "08:30", "10:00", 0, GROUP, SEMESTER)]


def test_a_foreign_lesson_that_does_differ_is_written_under_its_own_partition(app, db):
    # Once the six columns differ the rewrite runs, and it
    # deletes the partition it was NAMED while inserting whatever
    # it was HANDED. Only scrape_knf_schedule's per-partition
    # bucketing keeps the two in step
    ss._insert_lessons(db, [_lesson()])
    db.commit()

    added, removed = ss._reconcile_partition(db, GROUP, SEMESTER,
                                             [_lesson(group_name="EV-2", title="Nauja")])
    db.commit()

    assert (added, removed) == (1, 1)
    assert _rows(db) == [("Nauja", "A. Petraitis", "301",
                          "08:30", "10:00", 0, "EV-2", SEMESTER)]


def test_the_counts_ignore_group_and_semester_while_the_insert_does_not(app, db):
    # Two lessons differing ONLY in group_name are one entry in
    # the six-column comparison set but two rows in the table
    added, removed = ss._reconcile_partition(db, GROUP, SEMESTER,
                                             [_lesson(), _lesson(group_name="EV-2")])
    db.commit()

    assert (added, removed) == (1, 0)
    assert len(_rows(db)) == 2


def test_a_null_keyed_partition_cannot_be_reconciled_and_grows_every_run(app, db):
    # `WHERE group_name = NULL` never matches, so the snapshot is
    # empty, the delete removes nothing and the NULLs slip past
    # the unique index. Unreachable from the scraper — both
    # parsers always return a string — and pinned so it stays
    # that way
    for _ in range(2):
        assert ss._reconcile_partition(db, None, None, [_lesson(group_name=None, semester=None)]) == (1, 0)
        db.commit()

    assert len(_rows(db)) == 2


def test_reconciliation_refuses_a_lesson_missing_a_field_before_it_deletes(app, db):
    ss._insert_lessons(db, [_lesson()])
    db.commit()
    broken = {key: value for key, value in _lesson().items() if key != "time_end"}

    with pytest.raises(KeyError):
        ss._reconcile_partition(db, GROUP, SEMESTER, [broken])

    # The scraped set is built before the DELETE, so the stored
    # timetable is untouched
    assert len(_rows(db)) == 1


def test_reconciliation_of_a_batch_that_is_not_iterable_writes_nothing(app, db):
    ss._insert_lessons(db, [_lesson()])
    db.commit()

    with pytest.raises(TypeError):
        ss._reconcile_partition(db, GROUP, SEMESTER, None)

    assert len(_rows(db)) == 1


def test_reconciliation_needs_a_connection_that_returns_named_rows(app):
    # The snapshot reads row["title"] — a connection left on the
    # default tuple factory fails loudly instead of silently
    # comparing the wrong things
    plain = sqlite3.connect(app.config["DB_PATH"])
    try:
        plain.execute(
            "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end,"
            " day_of_week, group_name, semester)"
            " VALUES ('x', 'T', '', '', '08:30', '10:00', 0, ?, ?)", (GROUP, SEMESTER))
        plain.commit()

        with pytest.raises(TypeError):
            ss._reconcile_partition(plain, GROUP, SEMESTER, [])
    finally:
        plain.close()


def test_reconciliation_leaves_the_commit_to_its_caller(app, db):
    ss._insert_lessons(db, [_lesson()])
    db.commit()

    ss._reconcile_partition(db, GROUP, SEMESTER, [_lesson(title="Nauja")])

    # Outside the transaction the old timetable still stands
    assert [row[0] for row in _visible(app)] == ["Programavimas"]
    db.commit()
    assert [row[0] for row in _visible(app)] == ["Nauja"]


def test_rolling_back_a_reconciliation_brings_the_whole_partition_back(app, db):
    ss._insert_lessons(db, [_lesson(), _lesson(day_of_week=1)])
    db.commit()

    ss._reconcile_partition(db, GROUP, SEMESTER, [])
    assert _rows(db) == []

    db.rollback()
    assert len(_rows(db)) == 2


def test_a_two_hundred_lesson_partition_reconciles_against_itself_for_free(app, db):
    lessons = [_lesson(title=f"Dalykas {n}", day_of_week=n % 7) for n in range(200)]
    ss._insert_lessons(db, lessons)
    db.commit()

    recorder = _RecordingDb(db)
    assert ss._reconcile_partition(recorder, GROUP, SEMESTER, lessons) == (0, 0)

    assert recorder.statements("INSERT") == []


def test_a_blocked_delete_leaves_the_partition_exactly_as_it_was(app, db):
    ss._insert_lessons(db, [_lesson()])
    db.commit()

    blocker = _open(app)
    writer = _open(app, timeout=0)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("INSERT INTO schedule_lessons (id, title, time_start, time_end, day_of_week)"
                        " VALUES ('blocker', 'T', '08:30', '10:00', 0)")

        with pytest.raises(sqlite3.OperationalError):
            ss._reconcile_partition(writer, GROUP, SEMESTER, [_lesson(title="Nauja")])
    finally:
        blocker.rollback()
        blocker.close()
        writer.close()

    assert [row[0] for row in _visible(app)] == ["Programavimas"]




# ==========================================================
# Races the write phase can actually lose
# ==========================================================


def test_a_row_that_lands_after_the_snapshot_is_deleted_with_the_rest(app, db):
    ss._insert_lessons(db, [_lesson()])
    db.commit()

    def intruder():
        other = _open(app)
        try:
            ss._insert_lessons(other, [_lesson(title="Tarpinis", day_of_week=4)])
            other.commit()
        finally:
            other.close()

    racing = _RaceDb(db, "DELETE FROM schedule_lessons", intruder)
    added, removed = ss._reconcile_partition(racing, GROUP, SEMESTER, [_lesson(title="Nauja")])
    db.commit()

    # The counts describe the SNAPSHOT — two rows went, one was
    # reported — and the partition still ends as the feed said
    assert (added, removed) == (1, 1)
    assert [row[0] for row in _rows(db)] == ["Nauja"]


def test_a_row_that_lands_in_another_partition_survives_the_rewrite(app, db):
    ss._insert_lessons(db, [_lesson()])
    db.commit()

    def intruder():
        other = _open(app)
        try:
            ss._insert_lessons(other, [_lesson(group_name="EV-2")])
            other.commit()
        finally:
            other.close()

    racing = _RaceDb(db, "DELETE FROM schedule_lessons", intruder)
    ss._reconcile_partition(racing, GROUP, SEMESTER, [_lesson(title="Nauja")])
    db.commit()

    assert sorted((row[0], row[6]) for row in _rows(db)) == [("Nauja", GROUP),
                                                             ("Programavimas", "EV-2")]


def test_a_row_already_present_when_the_insert_runs_is_absorbed(app, db):
    def intruder():
        # Inside the same transaction the DELETE just opened —
        # the OR IGNORE has to absorb it or the write phase dies
        # on the natural-key index
        db.execute(
            "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end,"
            " day_of_week, group_name, semester)"
            " VALUES ('intruder', 'Nauja', 'A. Petraitis', '301', '08:30', '10:00', 0, ?, ?)",
            (GROUP, SEMESTER))

    ss._insert_lessons(db, [_lesson()])
    db.commit()

    racing = _RaceDb(db, "INSERT OR IGNORE", intruder)
    added, removed = ss._reconcile_partition(racing, GROUP, SEMESTER, [_lesson(title="Nauja")])
    db.commit()

    assert (added, removed) == (1, 1)
    assert _rows(db) == [("Nauja", "A. Petraitis", "301", "08:30", "10:00", 0, GROUP, SEMESTER)]




# ==========================================================
# _purge_old_semesters — retiring finished semesters
# ==========================================================


def test_semesters_older_than_the_anchor_are_retired(app, db):
    ss._insert_lessons(db, [_lesson(semester="2023-R"), _lesson(semester="2024-P"),
                            _lesson(semester="2025-R"), _lesson(semester=SEMESTER)])
    db.commit()

    ss._purge_old_semesters(db, SEMESTER)

    assert [row[7] for row in _visible(app)] == [SEMESTER]


def test_the_anchor_and_everything_newer_are_kept(app, db):
    ss._insert_lessons(db, [_lesson(semester=SEMESTER), _lesson(semester="2026-R"),
                            _lesson(semester="2026-P"), _lesson(semester="2030-R")])
    db.commit()
    recorder = _RecordingDb(db)

    ss._purge_old_semesters(recorder, SEMESTER)

    assert len(_visible(app)) == 4
    assert recorder.statements("DELETE") == []
    assert "COMMIT" not in recorder.sql


def test_the_term_immediately_before_the_anchor_is_the_first_to_go(app, db):
    # "2025-R" sorts AFTER "2025-P" as text and is one term older
    # in the calendar — the key is what makes the purge right
    ss._insert_lessons(db, [_lesson(semester="2025-R"), _lesson(semester="2025-P")])
    db.commit()

    ss._purge_old_semesters(db, "2025-P")

    assert [row[7] for row in _visible(app)] == ["2025-P"]


def test_the_anchor_itself_is_never_purged_even_as_the_oldest_label(app, db):
    ss._insert_lessons(db, [_lesson(semester="2025-R"), _lesson(semester="2025-R", day_of_week=1)])
    db.commit()

    ss._purge_old_semesters(db, "2025-R")

    assert len(_visible(app)) == 2


def test_every_group_of_a_finished_semester_goes_together(app, db):
    ss._insert_lessons(db, [_lesson(semester="2024-R", group_name="ISKS-1"),
                            _lesson(semester="2024-R", group_name="EV-2"),
                            _lesson(semester="2024-R", group_name="LFR-1"),
                            _lesson(semester=SEMESTER, group_name="EV-2")])
    db.commit()

    ss._purge_old_semesters(db, SEMESTER)

    assert [(row[6], row[7]) for row in _visible(app)] == [("EV-2", SEMESTER)]


@pytest.mark.parametrize("label", ["2024-pavasaris", "rudens semestras", "2024", ""])
def test_a_label_this_scraper_never_wrote_is_left_for_an_admin(app, db, label):
    ss._insert_lessons(db, [_lesson(semester=label), _lesson(semester=SEMESTER)])
    db.commit()

    ss._purge_old_semesters(db, SEMESTER)

    assert sorted(row[7] for row in _visible(app)) == sorted([label, SEMESTER])


def test_rows_with_no_semester_at_all_are_left_alone(app, db):
    ss._insert_lessons(db, [_lesson(semester=None), _lesson(semester="2020-R")])
    db.commit()

    ss._purge_old_semesters(db, SEMESTER)

    assert [row[7] for row in _visible(app)] == [None]


@pytest.mark.parametrize("anchor", [None, "", "rudens semestras", "2025-r", "25-P"])
def test_an_anchor_this_scraper_never_wrote_purges_nothing(app, db, anchor):
    ss._insert_lessons(db, [_lesson(semester="2019-R")])
    db.commit()
    recorder = _RecordingDb(db)

    ss._purge_old_semesters(recorder, anchor)

    assert len(_visible(app)) == 1
    assert recorder.sql == []


def test_a_non_string_anchor_raises_out_of_the_key(app, db):
    ss._insert_lessons(db, [_lesson(semester="2019-R")])
    db.commit()

    with pytest.raises(TypeError):
        ss._purge_old_semesters(db, 2025)

    assert len(_visible(app)) == 1


def test_the_earliest_possible_anchor_finds_nothing_older(app, db):
    ss._insert_lessons(db, [_lesson(semester="0000-R"), _lesson(semester="2025-R")])
    db.commit()

    ss._purge_old_semesters(db, "0000-R")

    assert len(_visible(app)) == 2


def test_the_latest_possible_anchor_retires_every_label_it_recognises(app, db):
    ss._insert_lessons(db, [_lesson(semester="0000-R"), _lesson(semester="2025-P"),
                            _lesson(semester="9999-R"), _lesson(semester="senas"),
                            _lesson(semester=None)])
    db.commit()

    ss._purge_old_semesters(db, "9999-P")

    assert sorted(str(row[7]) for row in _visible(app)) == ["None", "senas"]


def test_purging_an_empty_table_is_a_no_op(app, db):
    recorder = _RecordingDb(db)

    ss._purge_old_semesters(recorder, SEMESTER)

    assert recorder.statements("DELETE") == []
    assert "COMMIT" not in recorder.sql


def test_a_second_purge_finds_nothing_left_to_retire(app, db):
    ss._insert_lessons(db, [_lesson(semester="2020-R"), _lesson(semester=SEMESTER)])
    db.commit()
    ss._purge_old_semesters(db, SEMESTER)

    recorder = _RecordingDb(db)
    ss._purge_old_semesters(recorder, SEMESTER)

    assert recorder.statements("DELETE") == []
    assert len(_visible(app)) == 1


def test_one_delete_is_issued_per_stale_label_not_per_row(app, db):
    ss._insert_lessons(db, [_lesson(semester="2020-R"), _lesson(semester="2020-R", day_of_week=1),
                            _lesson(semester="2021-P"), _lesson(semester="2021-P", day_of_week=2),
                            _lesson(semester=SEMESTER)])
    db.commit()
    recorder = _RecordingDb(db)

    ss._purge_old_semesters(recorder, SEMESTER)

    assert len(recorder.statements("DELETE")) == 2
    assert len(_visible(app)) == 1


def test_the_purge_logs_what_each_finished_semester_cost(app, db, caplog):
    ss._insert_lessons(db, [_lesson(semester="2020-R"), _lesson(semester="2020-R", day_of_week=1),
                            _lesson(semester="2020-R", day_of_week=2)])
    db.commit()

    with caplog.at_level(logging.INFO, logger="app.scraper.schedule_scraper"):
        ss._purge_old_semesters(db, SEMESTER)

    assert "Retired 3 lesson(s) from the finished semester 2020-R" in caplog.text


def test_a_purge_that_retires_something_commits_it(app, db):
    ss._insert_lessons(db, [_lesson(semester="2020-R"), _lesson(semester=SEMESTER)])
    db.commit()

    ss._purge_old_semesters(db, SEMESTER)

    # No commit of our own: the purge's own commit is what makes
    # the deletion durable
    assert [row[7] for row in _visible(app)] == [SEMESTER]


def test_a_purge_that_retires_something_also_commits_its_callers_pending_work(app, db):
    ss._insert_lessons(db, [_lesson(semester="2020-R")])
    db.commit()
    # Still uncommitted when the purge runs — and the purge's
    # commit takes it along, which is safe only because
    # scrape_knf_schedule commits the write phase first
    ss._insert_lessons(db, [_lesson(semester=SEMESTER, title="Nebaigta")])

    ss._purge_old_semesters(db, SEMESTER)

    assert [row[0] for row in _visible(app)] == ["Nebaigta"]


def test_a_purge_with_nothing_stale_commits_nothing_of_its_callers(app, db):
    ss._insert_lessons(db, [_lesson(semester=SEMESTER, title="Nebaigta")])

    ss._purge_old_semesters(db, SEMESTER)

    assert _visible(app) == []
    db.rollback()
    assert _rows(db) == []


def test_the_purge_needs_a_connection_that_returns_named_rows(app):
    plain = sqlite3.connect(app.config["DB_PATH"])
    try:
        plain.execute(
            "INSERT INTO schedule_lessons (id, title, teacher, room, time_start, time_end,"
            " day_of_week, group_name, semester)"
            " VALUES ('x', 'T', '', '', '08:30', '10:00', 0, ?, '2020-R')", (GROUP,))
        plain.commit()

        with pytest.raises(TypeError):
            ss._purge_old_semesters(plain, SEMESTER)
    finally:
        plain.close()


def test_a_purge_blocked_by_another_writer_retires_nothing(app, db):
    ss._insert_lessons(db, [_lesson(semester="2020-R")])
    db.commit()

    blocker = _open(app)
    writer = _open(app, timeout=0)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("INSERT INTO schedule_lessons (id, title, time_start, time_end, day_of_week)"
                        " VALUES ('blocker', 'T', '08:30', '10:00', 0)")

        with pytest.raises(sqlite3.OperationalError):
            ss._purge_old_semesters(writer, SEMESTER)
    finally:
        blocker.rollback()
        blocker.close()
        writer.close()

    assert [row[7] for row in _visible(app)] == ["2020-R"]




# ==========================================================
# The three of them together — the write phase's shape
# ==========================================================


def test_the_write_phase_is_idempotent_over_a_whole_timetable(app, db):
    autumn = [_lesson(semester="2024-R", title=f"Sena {n}", day_of_week=n) for n in range(3)]
    current = [_lesson(title=f"Dalykas {n}", day_of_week=n) for n in range(5)]
    ss._insert_lessons(db, autumn)
    db.commit()

    first = ss._reconcile_partition(db, GROUP, SEMESTER, current)
    db.commit()
    ss._purge_old_semesters(db, SEMESTER)

    second = ss._reconcile_partition(db, GROUP, SEMESTER, current)
    db.commit()
    ss._purge_old_semesters(db, SEMESTER)

    assert first == (5, 0)
    assert second == (0, 0)
    assert len(_visible(app)) == 5


def test_a_neighbouring_semester_is_added_to_and_never_reconciled_away(app, db):
    # The window clips the next autumn — those partitions go
    # through _insert_lessons, so the anchor's rewrite must not
    # touch them
    ss._insert_lessons(db, [_lesson(semester="2026-R"), _lesson(semester="2026-R", day_of_week=1)])
    db.commit()

    ss._reconcile_partition(db, GROUP, SEMESTER, [_lesson()])
    db.commit()
    ss._purge_old_semesters(db, SEMESTER)

    assert sorted(row[7] for row in _visible(app)) == ["2025-P", "2026-R", "2026-R"]
