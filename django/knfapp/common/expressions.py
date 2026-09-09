############################################################
#  [*] Shared ORM expressions — the vendor-split functions
#
#  The one place engine dialects are allowed to differ:
#  everything else in the codebase is either plain ORM or a
#  single SQL spelling both engines parse. These classes use
#  the ORM's own dispatch (as_sqlite / as_postgresql), so a
#  call site writes ONE expression and the compiler renders
#  the engine's spelling — exactly how Django's built-in
#  functions cross engines.
#
#  Both render the same NUMBER — the astronomical Julian day
#  as a float — so score formulas built on them rank
#  identically on either engine. The postgres arm casts its
#  argument with ::timestamptz — a no-op on the timestamptz
#  stamp columns, and the coercion that lets a bound Python
#  datetime ride the same expression; SQLite's JULIANDAY
#  parses the engine's own stored text form directly.
#
#  Split into:
#
#    JulianDay    — julianday(<stamp column or value>)
#    JulianDayNow — julianday('now')
############################################################


from django.db.models import FloatField, Func








############################################################
# JulianDay
############################################################
#
# The Julian day number of one stamp — a datetime column
# or a bound datetime value.
#
# Used by:
#   - news/api/views.py — the feed score's recency term
#   - social/api/views.py — the wall score's recency term
############################################################

class JulianDay(Func):
    function = "JULIANDAY"
    arity = 1
    output_field = FloatField()

    def as_postgresql(self, compiler, connection, **extra_context):
        return self.as_sql(
            compiler, connection,
            template="(EXTRACT(EPOCH FROM (%(expressions)s)::timestamptz) / 86400.0 + 2440587.5)",
            **extra_context,
        )








############################################################
# JulianDayNow
############################################################
#
# The Julian day number of the current instant — the `ref`
# a feed page with no ?before pin decays against.
#
# Used by:
#   - news/api/views.py — get_feed without ?before
#   - social/api/views.py — social_feed without ?before
############################################################

class JulianDayNow(Func):
    template = "JULIANDAY('now')"
    arity = 0
    output_field = FloatField()

    def as_postgresql(self, compiler, connection, **extra_context):
        return self.as_sql(
            compiler, connection,
            template="(EXTRACT(EPOCH FROM NOW()) / 86400.0 + 2440587.5)",
            **extra_context,
        )
