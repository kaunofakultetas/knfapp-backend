############################################################
#  [*] Raw-SQL helpers — dict rows over the one connection
#
#  The idiom the raw-SQL statements share: execute with %s
#  params, read rows back by column name.
#  q() answers a list of dicts, q1() one dict or None,
#  execute() the rowcount. Nothing here manages
#  transactions — the caller's ATOMIC_REQUESTS block or
#  its own transaction.atomic() owns that.
#
#  Used by:
#    - wayfind/api/views.py — the buildings LEFT JOIN listing
#    - chat/api/views.py — the deliberate raw core (the
#      mark_read hand transaction, the last-message seek,
#      the unread aggregates, the no-cascade purges)
#    - tests/test_wayfind_stitch.py — reading a pano row
#      back
#    (chat/api carries its own private copy of the same
#    helpers)
############################################################


from datetime import datetime, timezone


from django.db import connection


def _aware(value):
    # SQLite's decltype converter hands back NAIVE datetimes
    # where the ORM would hand aware ones — one kind (aware
    # UTC) leaves this module, whatever the driver did
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _dict_rows(cursor):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, (_aware(v) for v in row))) for row in cursor.fetchall()]


def q(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return _dict_rows(cursor)


def q1(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
        if row is None:
            return None
        columns = [col[0] for col in cursor.description]
        return dict(zip(columns, (_aware(v) for v in row)))


def execute(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount
