############################################################
#  [*] Timestamps — the one stamp policy
#
#  The API stores and publishes ISO-8601 text with an
#  explicit UTC offset, byte-compatible with what the
#  production tables already hold ("2026-09-06T12:34:56.789012+00:00"), so a
#  data migration never has to rewrite stamps and the
#  mobile app's parsers keep working.
#
#    utc_now_iso()        — now, in that exact shape
#    parse_stored(value)  — a stored stamp back to an AWARE
#      datetime. A naive legacy value is assumed UTC; a
#      malformed one returns None, which every caller
#      treats as "expired" — a 401/400, never a 500.
############################################################


from datetime import datetime, timezone


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_stored(value):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp
