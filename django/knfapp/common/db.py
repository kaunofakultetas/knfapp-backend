############################################################
#  [*] Raw-SQL helpers — dict rows over the one connection
#
#  The idiom the raw-SQL handlers share: execute with %s
#  params, read rows back by column name.
#  q() answers a list of dicts, q1() one dict or None,
#  execute() the rowcount. Nothing here manages
#  transactions — the caller's ATOMIC_REQUESTS block or
#  its own transaction.atomic() owns that.
#
#  Used by:
#    - wayfind/api/*, wayfind/stitch.py (chat/api carries
#      an older private copy of the same helpers)
############################################################


from django.db import connection


def _dict_rows(cursor):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


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
        return dict(zip(columns, row))


def execute(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.rowcount
