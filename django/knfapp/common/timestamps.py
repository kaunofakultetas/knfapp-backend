############################################################
#  [*] Timestamps — the one stamp policy
#
#  STORAGE is real datetimes: every stamp column is a
#  DateTimeField holding an aware UTC instant (timestamptz
#  on postgres; SQLite stores Django's naive-UTC text and
#  the ORM converts back), and CODE carries that one aware
#  kind end to end. The WIRE keeps its frozen ISO shapes —
#  aware "+00:00" on most endpoints, naive UTC on the
#  chat/scraper-status/info surfaces — decided per RESPONSE
#  by json_response's naive_stamps flag (common/http.py),
#  never by the value's kind. The GDPR export is the one
#  mixed response; it shapes its chat sections per field
#  with as_naive_utc.
#
#    utc_now()            — the current aware UTC instant;
#      what every writer stamps
#    utc_now_iso()        — the same instant as the aware
#      wire string, for the few places that need text
#      (ETag seeds, log lines)
#    as_aware(value)      — stamp (datetime or ISO text,
#      raw-cursor space form included) to an AWARE UTC
#      datetime; naive input is assumed UTC; malformed
#      answers None, which every caller treats as
#      "expired" — a 401/400, never a 500
#    as_naive_utc(value)  — the same, minus tzinfo: what a
#      naive-wire serializer hands the encoder
#    parse_stored(value)  — alias of as_aware (the auth and
#      admin callers read like prose with it)
############################################################


from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def as_aware(value):
    if isinstance(value, datetime):
        stamp = value
    else:
        try:
            stamp = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def as_naive_utc(value):
    stamp = as_aware(value)
    if stamp is None:
        return None
    return stamp.replace(tzinfo=None)


def parse_stored(value):
    return as_aware(value)
