# -----------------------------------------------------------
#  [*] Tests — the channel fan-out: _send_by_language,
#      notify_channel_users, notify_channel_user,
#      notify_channel
#
#  The four helpers that stand between a caller ("tell these
#  people this") and Expo. The raw senders below them are
#  someone else's slice; everything here is about the
#  DECISIONS these four make before a message exists:
#
#    - THE GUARD: a channel name outside VALID_CHANNELS has
#      no opt-out rows at all, so sending on it would mean
#      "send to everyone who ever disabled anything". Every
#      near-miss spelling, every wrong type and the empty
#      string are refused, loudly, BEFORE a database
#      connection is opened
#    - THE RECIPIENT SET: deduplicated, sorted, falsy ids
#      dropped, chunked at _ID_CHUNK so a huge fan-out cannot
#      hit SQLite's variable cap — and an empty set costs
#      neither a connection nor a request
#    - THE OPT-OUT: only an explicit enabled=0 row for THAT
#      channel silences a user. A missing row, an enabled=1
#      row, an enabled=2 row and a row for a different
#      channel all still ring
#    - THE COPY: push_tokens.language routes it, and ONLY the
#      exact string 'en' is English — 'EN', 'en-US', a
#      trailing space and everything else ride the Lithuanian
#      batch. An empty title_en or body_en falls back
#    - THE STAMP: data["channel"] is written on a COPY, so a
#      caller's dict never grows a key, a reused dict is safe,
#      and a "channel" the caller set itself is overwritten
#    - THE SNAPSHOT: the rows are read once. What happens to
#      push_tokens after that query — a device retired
#      mid-fan-out, an opt-out saved between two chunks —
#      lands on the NEXT call, deterministically driven here
#    - CONTAINMENT: the connection is closed on every path,
#      including the one where the query raises
#
#  No packet leaves the process: every exp.host call is
#  driven through `responses`, and the container has no
#  network at all.
# -----------------------------------------------------------

import json
import logging
import sqlite3
import uuid

import pytest
import responses

from app.notifications import push


LOGGER_NAME = "app.notifications.push"

# Names that must never reach Expo: near-miss spellings, the
# wrong case, stray whitespace, the wrong type entirely
_NOT_A_CHANNEL = [
    None, "", " ", "News", "NEWS", "news ", " news", "new", "newss",
    "chats", "chat\n", "default", "marketing", "*", "%",
    0, 1, 3.14, True, b"news", ("news",), ["news"], {"news": 1},
]




# -----------------------------------------------------------
# _isolated_push_module
# -----------------------------------------------------------
#
# push.py keeps process-wide state — the paced gate's
# last-slice stamp and the receipt queue. Both are reset
# around every test here and the pace is switched off, so a
# fan-out of two hundred messages costs no wall-clock and no
# test in this file (or the next one) inherits a stamp.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_push_module(monkeypatch):
    _reset_push_state()
    monkeypatch.setattr(push, "_SLICE_INTERVAL", 0.0)
    yield
    _reset_push_state()


def _reset_push_state():
    push._last_slice_at = 0.0
    with push._receipt_lock:
        push._receipt_queue.clear()




# -----------------------------------------------------------
# Row / token helpers
# -----------------------------------------------------------
#
# _expo_token keeps the shape notifications/routes.py accepts
# at intake, so a token in these rows looks like a real one;
# _row builds the two-column shape _send_by_language reads
# when it is called directly, without a database at all.
# -----------------------------------------------------------

def _expo_token(name):
    return f"ExponentPushToken[{name}-aaaaaaaaaa]"


def _row(token, language="lt"):
    return {"token": token, "language": language}


def _seed_token(db, user_id, token, language="lt", active=1, platform="ios"):
    db.execute(
        "INSERT INTO push_tokens (id, user_id, token, platform, language, active)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, token, platform, language, active),
    )
    db.commit()
    return token


def _set_channel(db, user_id, channel, enabled):
    db.execute(
        "INSERT OR REPLACE INTO notification_channels (user_id, channel, enabled) VALUES (?, ?, ?)",
        (user_id, channel, enabled),
    )
    db.commit()


def _token_row(db, token):
    return db.execute("SELECT * FROM push_tokens WHERE token = ?", (token,)).fetchone()




# -----------------------------------------------------------
# Expo fakes
# -----------------------------------------------------------
#
# _expo_accepts_all answers any shape it is handed with a
# matching row of "ok" tickets, so a test can send one message
# or two hundred without arranging a body per slice.
# _expo_replies queues ONE canned answer: registered in order,
# `responses` hands each queued body to the next call and
# repeats the last one after that — which is how the
# "first batch fails, second succeeds" tests below work.
# -----------------------------------------------------------

def _expo_accepts_all():

    def _reply(request):
        payload = json.loads(request.body)
        if isinstance(payload, list):
            data = [{"status": "ok", "id": f"ticket:{msg.get('to')}"} for msg in payload]
        else:
            data = {"status": "ok", "id": f"ticket:{payload.get('to')}"}
        return 200, {}, json.dumps({"data": data})

    responses.add_callback(responses.POST, push.EXPO_PUSH_URL,
                           callback=_reply, content_type="application/json")


def _expo_replies(body=None, status=200):
    responses.add(responses.POST, push.EXPO_PUSH_URL, json=body, status=status)


def _push_bodies():
    return [json.loads(call.request.body) for call in responses.calls
            if call.request.url == push.EXPO_PUSH_URL]


def _all_messages():
    out = []
    for body in _push_bodies():
        out.extend(body if isinstance(body, list) else [body])
    return out


def _sent_tokens():
    return sorted(msg["to"] for msg in _all_messages())




# -----------------------------------------------------------
# _RecordingDb / _record_db
# -----------------------------------------------------------
#
# A see-through wrapper around the real connection: it keeps
# every (sql, params) pair the fan-out issued and remembers
# whether close() ran. That is how the chunking tests count
# QUERIES rather than guessing from the requests, and how the
# containment tests prove the connection is released.
#
# Used by:
#   - the query-shape and connection-hygiene tests below
# -----------------------------------------------------------

class _RecordingDb:

    def __init__(self, real):
        self._real = real
        self.queries = []
        self.closed = False

    def execute(self, sql, params=()):
        self.queries.append((sql, list(params)))
        return self._real.execute(sql, params)

    def close(self):
        self.closed = True
        self._real.close()


def _record_db(monkeypatch):
    opened = []
    real_get_db = push.get_db

    def _fake_get_db():
        conn = _RecordingDb(real_get_db())
        opened.append(conn)
        return conn

    monkeypatch.setattr(push, "get_db", _fake_get_db)
    return opened




# -----------------------------------------------------------
# _HookedDb
# -----------------------------------------------------------
#
# The same wrapper with a callback fired just BEFORE the nth
# query — the only deterministic way to change the database
# in the middle of a chunked fan-out and see which chunk the
# change lands on.
#
# Used by:
#   - the mid-fan-out race tests below
# -----------------------------------------------------------

class _HookedDb(_RecordingDb):

    def __init__(self, real, before_query):
        super().__init__(real)
        self._before = before_query

    def execute(self, sql, params=()):
        self._before(len(self.queries) + 1)
        return super().execute(sql, params)


def _hook_db(monkeypatch, before_query):
    opened = []
    real_get_db = push.get_db

    def _fake_get_db():
        conn = _HookedDb(real_get_db(), before_query)
        opened.append(conn)
        return conn

    monkeypatch.setattr(push, "get_db", _fake_get_db)
    return opened




# -----------------------------------------------------------
# _explode / _AngryDb
# -----------------------------------------------------------
#
# The database being unreachable (get_db itself raising) and
# the database answering a query with an error. Neither of
# these four helpers swallows: containment is the caller's
# job, which is why chat/routes.py wraps its call in a try.
#
# Used by:
#   - the containment tests below
# -----------------------------------------------------------

def _explode(*args, **kwargs):
    raise sqlite3.OperationalError("unable to open database file")


class _AngryDb:

    def __init__(self):
        self.closed = False

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    def close(self):
        self.closed = True




# -----------------------------------------------------------
# _ghosts
# -----------------------------------------------------------
#
# Recipient ids belonging to nobody — enough of them to cross
# the real _ID_CHUNK boundary without inserting a single row,
# which is what makes the chunk-cap test cheap.
# -----------------------------------------------------------

def _ghosts(count):
    return [f"ghost-{i:05d}" for i in range(count)]








# -----------------------------------------------------------
# _send_by_language — the guard and the stamp
# -----------------------------------------------------------

@responses.activate
def test_a_none_row_set_is_a_zero_without_a_request(app):
    _expo_accepts_all()

    assert push._send_by_language("news", None, "T", "B", None, None, None, None) == 0
    assert len(responses.calls) == 0


@responses.activate
def test_an_empty_row_set_never_stamps_the_callers_data(app):
    _expo_accepts_all()
    caller_data = {"type": "news"}

    assert push._send_by_language("news", [], "T", "B", caller_data, None, None, None) == 0
    assert caller_data == {"type": "news"}


@responses.activate
def test_an_empty_row_set_leaves_the_stats_dict_untouched(app):
    _expo_accepts_all()
    stats = {}

    push._send_by_language("news", [], "T", "B", None, None, None, stats)

    assert stats == {}


@responses.activate
def test_a_channel_the_caller_already_stamped_is_overwritten(app):
    _expo_accepts_all()

    push._send_by_language("news", [_row(_expo_token("overwrite"))], "T", "B",
                           {"channel": "gossip"}, None, None, None)

    assert _all_messages()[0]["data"]["channel"] == "news"


@responses.activate
def test_a_reused_data_dict_survives_two_fan_outs_unchanged(app):
    _expo_accepts_all()
    caller_data = {"type": "news"}

    push._send_by_language("news", [_row(_expo_token("reuse1"))], "T", "B",
                           caller_data, None, None, None)
    push._send_by_language("chat", [_row(_expo_token("reuse2"))], "T", "B",
                           caller_data, None, None, None)

    assert caller_data == {"type": "news"}
    assert [msg["data"]["channel"] for msg in _all_messages()] == ["news", "chat"]


@responses.activate
def test_an_empty_data_dict_still_gets_the_channel_stamp(app):
    _expo_accepts_all()

    push._send_by_language("schedule", [_row(_expo_token("emptydata"))], "T", "B",
                           {}, None, None, None)

    assert _all_messages()[0]["data"] == {"channel": "schedule"}


@responses.activate
def test_a_nested_value_in_the_callers_data_rides_along_untouched(app):
    _expo_accepts_all()
    caller_data = {"type": "chat_message", "meta": {"unread": 3}}

    push._send_by_language("chat", [_row(_expo_token("nested"))], "T", "B",
                           caller_data, None, None, None)

    assert _all_messages()[0]["data"]["meta"] == {"unread": 3}


@responses.activate
def test_called_directly_it_stamps_whatever_channel_it_is_handed(app):
    # Validation lives in the public helpers on purpose — this
    # one is private and trusts its caller
    _expo_accepts_all()

    assert push._send_by_language("gossip", [_row(_expo_token("direct"))], "T", "B",
                                  None, None, None, None) == 1
    assert _all_messages()[0]["data"]["channel"] == "gossip"
    assert _all_messages()[0]["ttl"] == 86400








# -----------------------------------------------------------
# _send_by_language — the language split
# -----------------------------------------------------------

@pytest.mark.parametrize("language", ["lt", "LT", "de", "EN", "En", "eN", "en-US", "en_GB",
                                      " en", "en ", "english", "", "e", "n"])
@responses.activate
def test_only_the_exact_en_string_takes_the_english_copy(app, language):
    _expo_accepts_all()

    push._send_by_language("news", [_row(_expo_token("langcase"), language)],
                           "Naujiena", "Tekstas", None, "News", "Text", None)

    message = _all_messages()[0]
    assert message["title"] == "Naujiena"
    assert message["body"] == "Tekstas"


@responses.activate
def test_an_en_device_takes_the_english_copy(app):
    _expo_accepts_all()

    push._send_by_language("news", [_row(_expo_token("exacten"), "en")],
                           "Naujiena", "Tekstas", None, "News", "Text", None)

    message = _all_messages()[0]
    assert message["title"] == "News"
    assert message["body"] == "Text"


@responses.activate
def test_an_all_english_row_set_is_one_request(app):
    _expo_accepts_all()

    sent = push._send_by_language("news", [_row(_expo_token("allen1"), "en"),
                                           _row(_expo_token("allen2"), "en")],
                                  "Naujiena", "Tekstas", None, "News", "Text", None)

    assert sent == 2
    assert len(_push_bodies()) == 1


@responses.activate
def test_an_all_lithuanian_row_set_is_one_request(app):
    _expo_accepts_all()

    sent = push._send_by_language("news", [_row(_expo_token("alllt1")),
                                           _row(_expo_token("alllt2"))],
                                  "Naujiena", "Tekstas", None, "News", "Text", None)

    assert sent == 2
    assert len(_push_bodies()) == 1


@responses.activate
def test_an_empty_english_title_falls_back_to_the_lithuanian_one(app):
    _expo_accepts_all()

    push._send_by_language("news", [_row(_expo_token("emptytitle"), "en")],
                           "Naujiena", "Tekstas", None, "", "Text", None)

    message = _all_messages()[0]
    assert message["title"] == "Naujiena"
    assert message["body"] == "Text"


@responses.activate
def test_an_empty_english_body_falls_back_to_the_lithuanian_one(app):
    _expo_accepts_all()

    push._send_by_language("news", [_row(_expo_token("emptybody"), "en")],
                           "Naujiena", "Tekstas", None, "News", "", None)

    message = _all_messages()[0]
    assert message["title"] == "News"
    assert message["body"] == "Tekstas"


@responses.activate
def test_an_english_body_without_a_title_keeps_the_lithuanian_title(app):
    _expo_accepts_all()

    push._send_by_language("news", [_row(_expo_token("bodyonly"), "en")],
                           "Naujiena", "Tekstas", None, None, "Text", None)

    message = _all_messages()[0]
    assert message["title"] == "Naujiena"
    assert message["body"] == "Text"


@responses.activate
def test_the_lithuanian_batch_goes_out_before_the_english_one(app):
    _expo_accepts_all()

    push._send_by_language("news", [_row(_expo_token("orderen"), "en"),
                                    _row(_expo_token("orderlt"), "lt")],
                           "Naujiena", "Tekstas", None, "News", "Text", None)

    bodies = _push_bodies()
    assert bodies[0][0]["title"] == "Naujiena"
    assert bodies[1][0]["title"] == "News"


@responses.activate
def test_the_row_order_is_preserved_inside_a_language_batch(app):
    _expo_accepts_all()
    tokens = [_expo_token(f"ord{i}") for i in range(5)]

    push._send_by_language("news", [_row(t) for t in tokens], "T", "B", None, None, None, None)

    assert [msg["to"] for msg in _push_bodies()[0]] == tokens


@responses.activate
def test_a_repeated_token_in_the_rows_is_addressed_twice(app):
    # No dedupe at this level — push_tokens.token is UNIQUE, so
    # a repeat can only come from a caller building rows itself
    _expo_accepts_all()
    token = _expo_token("twice")

    assert push._send_by_language("news", [_row(token), _row(token)], "T", "B",
                                  None, None, None, None) == 2
    assert [msg["to"] for msg in _all_messages()] == [token, token]


@responses.activate
def test_a_blank_token_is_still_handed_to_expo(app):
    # Filtering belongs at intake (notifications/routes.py); the
    # fan-out sends what the query gave it
    _expo_accepts_all()

    assert push._send_by_language("news", [_row("")], "T", "B", None, None, None, None) == 1
    assert _all_messages()[0]["to"] == ""


@pytest.mark.slow
@responses.activate
def test_a_hundred_of_each_language_stays_two_requests(app):
    # Expo's cap is 100 PER REQUEST, and the two languages are
    # never merged into one batch of 200
    _expo_accepts_all()
    rows = ([_row(_expo_token(f"bulklt{i:03d}"), "lt") for i in range(100)]
            + [_row(_expo_token(f"bulken{i:03d}"), "en") for i in range(100)])

    assert push._send_by_language("news", rows, "T", "B", None, "TE", "BE", None) == 200
    assert sorted(len(body) for body in _push_bodies()) == [100, 100]








# -----------------------------------------------------------
# _send_by_language — hints, stats and partial failure
# -----------------------------------------------------------

@responses.activate
def test_both_language_batches_carry_the_chat_delivery_hints(app):
    _expo_accepts_all()

    push._send_by_language("chat", [_row(_expo_token("hintlt")), _row(_expo_token("hinten"), "en")],
                           "T", "B", None, "TE", "BE", None)

    for message in _all_messages():
        assert message["priority"] == "high"
        assert message["ttl"] == 3600


@pytest.mark.parametrize("channel", ["news", "schedule", "admin", "gossip"])
@responses.activate
def test_every_other_channel_takes_a_day_of_ttl_and_no_priority(app, channel):
    _expo_accepts_all()

    push._send_by_language(channel, [_row(_expo_token(f"ttl{channel}")), _row(_expo_token(f"ttlen{channel}"), "en")],
                           "T", "B", None, "TE", "BE", None)

    for message in _all_messages():
        assert "priority" not in message
        assert message["ttl"] == 86400


@responses.activate
def test_the_two_language_batches_share_one_stats_dict(app):
    _expo_accepts_all()
    stats = {}

    push._send_by_language("news", [_row(_expo_token("statlt")), _row(_expo_token("staten"), "en")],
                           "T", "B", None, "TE", "BE", stats)

    assert stats["sent"] == 2
    assert stats["failed"] == 0


@responses.activate
def test_a_stats_dict_that_already_holds_counts_is_added_to(app):
    _expo_accepts_all()
    stats = {"sent": 5, "failed": 2, "errors": {"MessageTooBig": 1}}

    push._send_by_language("news", [_row(_expo_token("statsadd"))], "T", "B",
                           None, None, None, stats)

    assert stats["sent"] == 6
    assert stats["failed"] == 2
    assert stats["errors"] == {"MessageTooBig": 1}


@responses.activate
def test_a_failed_lithuanian_batch_still_lets_the_english_one_through(app):
    _expo_replies({"errors": [{"code": "PUSH_TOO_MANY_NOTIFICATIONS"}]})
    _expo_accepts_all()

    sent = push._send_by_language("news", [_row(_expo_token("faillt")), _row(_expo_token("oken"), "en")],
                                  "T", "B", None, "TE", "BE", None)

    assert sent == 1
    assert len(_push_bodies()) == 2


@responses.activate
def test_a_failed_language_batch_is_counted_in_the_stats(app):
    _expo_replies({"errors": [{"code": "PUSH_TOO_MANY_NOTIFICATIONS"}]})
    _expo_accepts_all()
    stats = {}

    push._send_by_language("news", [_row(_expo_token("statfail")), _row(_expo_token("statok"), "en")],
                           "T", "B", None, "TE", "BE", stats)

    assert stats["sent"] == 1
    assert stats["failed"] == 1
    assert stats["errors"] == {"Rejected": 1}


@responses.activate
def test_the_return_value_is_the_sum_of_both_language_batches(app):
    _expo_accepts_all()
    rows = [_row(_expo_token(f"sum{i}")) for i in range(3)] + [_row(_expo_token("sumen"), "en")]

    assert push._send_by_language("news", rows, "T", "B", None, "TE", "BE", None) == 4


@responses.activate
def test_a_none_title_and_body_ride_through_untouched(app):
    # Nothing here validates the copy — an empty push is the
    # caller's mistake to make, not this helper's to hide
    _expo_accepts_all()

    push._send_by_language("news", [_row(_expo_token("nonecopy"))], None, None,
                           None, None, None, None)

    message = _all_messages()[0]
    assert message["title"] is None
    assert message["body"] is None


@responses.activate
def test_a_huge_title_is_not_truncated_on_the_way_out(app):
    _expo_accepts_all()
    huge = "A" * 10000

    push._send_by_language("news", [_row(_expo_token("hugecopy"))], huge, "B",
                           None, None, None, None)

    assert _all_messages()[0]["title"] == huge








# -----------------------------------------------------------
# notify_channel_users — the channel guard
# -----------------------------------------------------------

@pytest.mark.parametrize("channel", _NOT_A_CHANNEL)
@responses.activate
def test_a_fan_out_on_anything_but_the_four_channels_sends_nothing(app, db, channel, caplog):
    _seed_token(db, "guard-owner", _expo_token("guarduser"))
    _expo_accepts_all()

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push.notify_channel_users(channel, ["guard-owner"], "T", "B") == 0

    assert len(responses.calls) == 0
    assert "Refusing push on unknown channel" in caplog.text


@responses.activate
def test_the_refused_channel_is_named_in_the_log_line(app, caplog):
    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        push.notify_channel_users("marketing", ["u-1"], "T", "B")

    assert repr("marketing") in caplog.text


@responses.activate
def test_an_unknown_channel_never_opens_a_database_connection(app, monkeypatch):
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_accepts_all()

    assert push.notify_channel_users("gossip", ["u-1"], "T", "B") == 0


@pytest.mark.parametrize("channel", list(push.VALID_CHANNELS))
@responses.activate
def test_every_valid_channel_reaches_the_fan_out(app, db, make_user, channel):
    user = make_user()
    _seed_token(db, user["id"], _expo_token(f"fanvalid{channel}"))
    _expo_accepts_all()

    assert push.notify_channel_users(channel, [user["id"]], "T", "B") == 1








# -----------------------------------------------------------
# notify_channel_users — the recipient set
# -----------------------------------------------------------

@pytest.mark.parametrize("user_ids", [[], None, (), set(), {}, [None], ["", ""], [None, "", 0, False],
                                      [0], [False]])
@responses.activate
def test_a_recipient_set_with_nothing_usable_in_it_never_opens_a_database(app, monkeypatch, user_ids):
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", user_ids, "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_tuple_of_recipient_ids_is_accepted(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("tupleids"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", (user["id"],), "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_set_of_recipient_ids_is_accepted(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("setids"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", {user["id"]}, "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_generator_of_recipient_ids_is_accepted(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("genids"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", (u for u in [user["id"]]), "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_dict_of_recipient_ids_is_read_as_its_keys(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("dictids"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", {user["id"]: "online"}, "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_bare_string_recipient_is_read_character_by_character(app, db, make_user, monkeypatch):
    # The footgun of an iterable parameter: handing this helper
    # one id instead of a LIST of ids queries its letters and
    # silently reaches nobody. notify_channel_user is the shape
    # a caller with a single recipient is meant to use
    user = make_user()
    _seed_token(db, user["id"], _expo_token("barestring"))
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", "abc", "T", "B") == 0
    assert opened[0].queries[0][1] == ["a", "b", "c", "chat"]
    assert len(responses.calls) == 0


@responses.activate
def test_a_falsy_id_beside_a_real_one_drops_only_itself(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("mixedfalsy"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [None, "", user["id"]], "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_the_recipient_set_is_deduplicated_before_the_query(app, db, make_user, monkeypatch):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("dedupq"))
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel_users("chat", [user["id"]] * 50, "T", "B")

    assert opened[0].queries[0][1] == [user["id"], "chat"]


@responses.activate
def test_a_thousand_repeats_of_one_id_are_one_query_of_one_id(app, db, make_user, monkeypatch):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("thousand"))
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]] * 1000, "T", "B") == 1
    assert len(opened[0].queries) == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_the_recipient_ids_reach_the_query_sorted(app, monkeypatch):
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel_users("chat", ["u-c", "u-a", "u-b"], "T", "B")

    assert opened[0].queries[0][1] == ["u-a", "u-b", "u-c", "chat"]


@responses.activate
def test_exactly_one_chunk_of_ids_is_one_query(app, monkeypatch):
    monkeypatch.setattr(push, "_ID_CHUNK", 3)
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel_users("chat", _ghosts(3), "T", "B")

    assert len(opened[0].queries) == 1


@responses.activate
def test_one_id_past_the_chunk_size_is_a_second_query(app, monkeypatch):
    monkeypatch.setattr(push, "_ID_CHUNK", 3)
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel_users("chat", _ghosts(4), "T", "B")

    assert [len(params) for _, params in opened[0].queries] == [4, 2]


@responses.activate
def test_the_channel_is_the_last_parameter_of_every_chunk_query(app, monkeypatch):
    monkeypatch.setattr(push, "_ID_CHUNK", 2)
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel_users("schedule", _ghosts(5), "T", "B")

    assert len(opened[0].queries) == 3
    assert all(params[-1] == "schedule" for _, params in opened[0].queries)


@responses.activate
def test_one_connection_serves_every_chunk(app, monkeypatch):
    monkeypatch.setattr(push, "_ID_CHUNK", 1)
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel_users("chat", _ghosts(6), "T", "B")

    assert len(opened) == 1
    assert len(opened[0].queries) == 6


@pytest.mark.slow
@responses.activate
def test_a_thousand_recipients_stay_under_sqlites_variable_cap(app, monkeypatch):
    # The real _ID_CHUNK, no rows needed: what is under test is
    # that no single statement carries a thousand placeholders
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", _ghosts(1000), "T", "B") == 0
    assert len(opened[0].queries) == 3
    assert max(len(params) for _, params in opened[0].queries) <= push._ID_CHUNK + 1


@responses.activate
def test_the_rows_of_every_chunk_end_up_in_one_batch_per_language(app, db, make_user, monkeypatch):
    lt_user, en_user = make_user(), make_user()
    _seed_token(db, lt_user["id"], _expo_token("chunklt"), language="lt")
    _seed_token(db, en_user["id"], _expo_token("chunken"), language="en")
    monkeypatch.setattr(push, "_ID_CHUNK", 1)
    _expo_accepts_all()

    assert push.notify_channel_users("news", [lt_user["id"], en_user["id"]], "T", "B",
                                     title_en="TE", body_en="BE") == 2
    assert len(_push_bodies()) == 2








# -----------------------------------------------------------
# notify_channel_users — what the query keeps and drops
# -----------------------------------------------------------

@responses.activate
def test_a_token_of_a_user_outside_the_list_is_never_addressed(app, db, make_user):
    wanted, stranger = make_user(), make_user()
    theirs = _seed_token(db, wanted["id"], _expo_token("wanted"))
    _seed_token(db, stranger["id"], _expo_token("stranger"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [wanted["id"]], "T", "B") == 1
    assert _sent_tokens() == [theirs]


@responses.activate
def test_an_id_belonging_to_nobody_costs_nothing_beside_a_real_one(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("realone"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", ["nobody-at-all", user["id"]], "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_recipient_id_that_looks_like_sql_matches_nobody(app, db, make_user):
    one, two = make_user(), make_user()
    theirs = _seed_token(db, one["id"], _expo_token("sqlone"))
    _seed_token(db, two["id"], _expo_token("sqltwo"))
    _expo_accepts_all()

    sent = push.notify_channel_users("chat", ["' OR 1=1 --", one["id"]], "T", "B")

    assert sent == 1
    assert _sent_tokens() == [theirs]


@responses.activate
def test_integer_recipient_ids_match_no_text_column(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("intids"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [1, 2, 3], "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_an_active_flag_that_is_not_exactly_one_is_not_addressed(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("activetwo"), active=2)
    live = _seed_token(db, user["id"], _expo_token("activeone"), active=1)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == [live]


@responses.activate
def test_a_disabled_switch_silences_every_device_that_user_owns(app, db, make_user):
    quiet = make_user()
    _seed_token(db, quiet["id"], _expo_token("q1"))
    _seed_token(db, quiet["id"], _expo_token("q2"))
    _seed_token(db, quiet["id"], _expo_token("q3"))
    _set_channel(db, quiet["id"], "chat", 0)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [quiet["id"]], "T", "B") == 0
    assert len(responses.calls) == 0


@pytest.mark.parametrize("enabled", [1, 2, -1, 99])
@responses.activate
def test_only_a_zero_switch_silences_a_user(app, db, make_user, enabled):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token(f"enabled{abs(enabled)}"))
    _set_channel(db, user["id"], "chat", enabled)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_switch_stored_as_the_text_zero_still_silences(app, db, make_user):
    # INTEGER affinity converts '0' on the way in, so the SQL
    # comparison still matches — the opt-out holds
    user = make_user()
    _seed_token(db, user["id"], _expo_token("textzero"))
    _set_channel(db, user["id"], "chat", "0")
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 0


@responses.activate
def test_one_users_opt_out_does_not_silence_another(app, db, make_user):
    quiet, loud = make_user(), make_user()
    _seed_token(db, quiet["id"], _expo_token("pairquiet"))
    theirs = _seed_token(db, loud["id"], _expo_token("pairloud"))
    _set_channel(db, quiet["id"], "chat", 0)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [quiet["id"], loud["id"]], "T", "B") == 1
    assert _sent_tokens() == [theirs]


@pytest.mark.parametrize("other", ["news", "schedule", "admin"])
@responses.activate
def test_an_opt_out_on_any_other_channel_leaves_chat_alone(app, db, make_user, other):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token(f"other{other}"))
    _set_channel(db, user["id"], other, 0)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_user_who_disabled_every_channel_hears_none_of_them(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("allmuted"))
    for channel in push.VALID_CHANNELS:
        _set_channel(db, user["id"], channel, 0)
    _expo_accepts_all()

    for channel in push.VALID_CHANNELS:
        assert push.notify_channel_users(channel, [user["id"]], "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_the_language_split_applies_to_the_fan_out_shape_too(app, db, make_user):
    user = make_user()
    lt_token = _seed_token(db, user["id"], _expo_token("fanlt"), language="lt")
    en_token = _seed_token(db, user["id"], _expo_token("fanen"), language="en")
    _expo_accepts_all()

    push.notify_channel_users("chat", [user["id"]], "Vardas", "Labas",
                              title_en="Name", body_en="Hello")

    by_token = {msg["to"]: msg for msg in _all_messages()}
    assert by_token[lt_token]["title"] == "Vardas"
    assert by_token[en_token]["title"] == "Name"


@responses.activate
def test_the_fan_out_stamps_the_channel_on_a_copy_of_the_callers_data(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("fanstamp"))
    _expo_accepts_all()
    caller_data = {"type": "chat_message", "conversationId": "c-1"}

    push.notify_channel_users("chat", [user["id"]], "T", "B", data=caller_data)

    assert caller_data == {"type": "chat_message", "conversationId": "c-1"}
    assert _all_messages()[0]["data"]["channel"] == "chat"








# -----------------------------------------------------------
# notify_channel_users — stats, state and containment
# -----------------------------------------------------------

@responses.activate
def test_the_fan_out_adds_to_a_stats_dict_that_already_has_counts(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("fanstats"))
    _expo_accepts_all()
    stats = {"sent": 3, "failed": 1, "errors": {"DeviceNotRegistered": 2}}

    push.notify_channel_users("chat", [user["id"]], "T", "B", stats=stats)

    assert stats["sent"] == 4
    assert stats["failed"] == 1
    assert stats["errors"] == {"DeviceNotRegistered": 2}


@responses.activate
def test_a_recipient_with_no_device_leaves_the_stats_dict_empty(app, db, make_user):
    user = make_user()
    _expo_accepts_all()
    stats = {}

    assert push.notify_channel_users("chat", [user["id"]], "T", "B", stats=stats) == 0
    assert stats == {}


@responses.activate
def test_two_fan_outs_in_a_row_each_reach_the_same_device(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("twice2"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == [token, token]


@responses.activate
def test_a_device_retired_by_the_first_fan_out_is_gone_from_the_second(app, db, make_user):
    # One of the two devices comes back DeviceNotRegistered —
    # which one is whatever order the query returned, so the
    # test reads the verdict back out of the table
    user = make_user()
    first = _seed_token(db, user["id"], _expo_token("retireone"))
    second = _seed_token(db, user["id"], _expo_token("retiretwo"))
    _expo_replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}},
                            {"status": "ok", "id": "t"}]})
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1

    live = [t for t in (first, second) if _token_row(db, t)["active"] == 1]
    assert len(live) == 1

    responses.calls.reset()
    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1
    assert _sent_tokens() == live


@responses.activate
def test_a_device_registered_between_two_fan_outs_joins_the_second(app, db, make_user):
    user = make_user()
    first = _seed_token(db, user["id"], _expo_token("joinfirst"))
    _expo_accepts_all()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 1

    second = _seed_token(db, user["id"], _expo_token("joinsecond"))
    responses.calls.reset()

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 2
    assert _sent_tokens() == sorted([first, second])


@responses.activate
def test_an_opt_out_saved_between_two_chunks_silences_the_rest(app, db, monkeypatch):
    # The ids are literal so sorted() puts "user-aaa" in the
    # first chunk and "user-zzz" in the second; the opt-out is
    # written after the first query and before the second
    kept = _seed_token(db, "user-aaa", _expo_token("earlychunk"))
    _seed_token(db, "user-zzz", _expo_token("latechunk"))
    monkeypatch.setattr(push, "_ID_CHUNK", 1)

    def _before(nth):
        if nth == 2:
            _set_channel(db, "user-zzz", "chat", 0)

    _hook_db(monkeypatch, _before)
    _expo_accepts_all()

    assert push.notify_channel_users("chat", ["user-zzz", "user-aaa"], "T", "B") == 1
    assert _sent_tokens() == [kept]


@responses.activate
def test_the_connection_is_closed_after_a_fan_out(app, db, make_user, monkeypatch):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("closenormal"))
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel_users("chat", [user["id"]], "T", "B")

    assert opened[0].closed is True


@responses.activate
def test_the_connection_is_closed_when_the_query_raises(app, monkeypatch):
    angry = _AngryDb()
    monkeypatch.setattr(push, "get_db", lambda: angry)
    _expo_accepts_all()

    with pytest.raises(sqlite3.OperationalError):
        push.notify_channel_users("chat", ["u-1"], "T", "B")

    assert angry.closed is True
    assert len(responses.calls) == 0


@responses.activate
def test_a_database_that_cannot_be_opened_reaches_the_caller(app, monkeypatch):
    # Containment is the caller's job here: chat/routes.py wraps
    # its fan-out in a try precisely because this one does not
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_accepts_all()

    with pytest.raises(sqlite3.OperationalError):
        push.notify_channel_users("chat", ["u-1"], "T", "B")


@responses.activate
def test_a_fan_out_survives_expo_answering_an_error_page(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("fanhtml"))
    responses.add(responses.POST, push.EXPO_PUSH_URL, body="<html>nope</html>",
                  status=400, content_type="text/html")

    assert push.notify_channel_users("chat", [user["id"]], "T", "B") == 0








# -----------------------------------------------------------
# notify_channel_user — the single-recipient shape
# -----------------------------------------------------------

@responses.activate
def test_the_single_recipient_shape_forwards_every_argument(app, monkeypatch):
    seen = {}

    def _fake(channel, user_ids, title, body, data=None, title_en=None, body_en=None, stats=None):
        seen.update(channel=channel, user_ids=user_ids, title=title, body=body,
                    data=data, title_en=title_en, body_en=body_en, stats=stats)
        return 7

    monkeypatch.setattr(push, "notify_channel_users", _fake)
    stats = {}

    result = push.notify_channel_user("chat", "u-1", "Vardas", "Labas", data={"type": "chat_message"},
                                      title_en="Name", body_en="Hello", stats=stats)

    assert result == 7
    assert seen == {"channel": "chat", "user_ids": ["u-1"], "title": "Vardas", "body": "Labas",
                    "data": {"type": "chat_message"}, "title_en": "Name", "body_en": "Hello",
                    "stats": stats}


@responses.activate
def test_the_single_recipient_shape_defaults_everything_optional_to_none(app, monkeypatch):
    seen = {}

    def _fake(channel, user_ids, title, body, data=None, title_en=None, body_en=None, stats=None):
        seen.update(data=data, title_en=title_en, body_en=body_en, stats=stats)
        return 0

    monkeypatch.setattr(push, "notify_channel_users", _fake)

    push.notify_channel_user("news", "u-1", "T", "B")

    assert seen == {"data": None, "title_en": None, "body_en": None, "stats": None}


@responses.activate
def test_the_single_recipient_shape_wraps_even_a_falsy_id_in_a_list(app, monkeypatch):
    seen = {}

    def _fake(channel, user_ids, title, body, **kwargs):
        seen["user_ids"] = user_ids
        return 0

    monkeypatch.setattr(push, "notify_channel_users", _fake)

    push.notify_channel_user("news", None, "T", "B")

    assert seen["user_ids"] == [None]


@pytest.mark.parametrize("user_id", [None, "", 0, False])
@responses.activate
def test_the_single_recipient_shape_with_no_real_user_never_opens_a_database(app, monkeypatch, user_id):
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_accepts_all()

    assert push.notify_channel_user("chat", user_id, "T", "B") == 0
    assert len(responses.calls) == 0


@pytest.mark.parametrize("channel", _NOT_A_CHANNEL)
@responses.activate
def test_the_single_recipient_shape_refuses_every_unknown_channel(app, db, channel):
    _seed_token(db, "single-guard-owner", _expo_token("singleguard"))
    _expo_accepts_all()

    assert push.notify_channel_user(channel, "single-guard-owner", "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_the_single_recipient_shape_splits_one_users_two_languages(app, db, make_user):
    user = make_user()
    lt_token = _seed_token(db, user["id"], _expo_token("onelt"), language="lt")
    en_token = _seed_token(db, user["id"], _expo_token("oneen"), language="en")
    _expo_accepts_all()

    assert push.notify_channel_user("news", user["id"], "Naujiena", "Tekstas",
                                    title_en="News", body_en="Text") == 2

    by_token = {msg["to"]: msg for msg in _all_messages()}
    assert by_token[lt_token]["body"] == "Tekstas"
    assert by_token[en_token]["body"] == "Text"


@responses.activate
def test_the_single_recipient_shape_stamps_the_channel(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("singlestamp"))
    _expo_accepts_all()
    caller_data = {"type": "chat_message"}

    push.notify_channel_user("chat", user["id"], "T", "B", data=caller_data)

    assert _all_messages()[0]["data"] == {"type": "chat_message", "channel": "chat"}
    assert caller_data == {"type": "chat_message"}


@responses.activate
def test_the_single_recipient_shape_carries_the_chat_delivery_hints(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("singlehint"))
    _expo_accepts_all()

    push.notify_channel_user("chat", user["id"], "T", "B")

    message = _all_messages()[0]
    assert message["priority"] == "high"
    assert message["ttl"] == 3600


@responses.activate
def test_the_single_recipient_shape_fills_the_stats_dict(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("singlestats"))
    _expo_replies({"data": [{"status": "error", "details": {"error": "MessageTooBig"}}]})
    stats = {}

    assert push.notify_channel_user("news", user["id"], "T", "B", stats=stats) == 0
    assert stats == {"sent": 0, "failed": 1, "errors": {"MessageTooBig": 1}}


@responses.activate
def test_the_single_recipient_shape_retires_a_dead_device(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("singledead"))
    _expo_replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]})

    assert push.notify_channel_user("chat", user["id"], "T", "B") == 0
    assert _token_row(db, token)["active"] == 0


@responses.activate
def test_the_single_recipient_shape_asks_for_exactly_one_id(app, monkeypatch):
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel_user("chat", "u-solo", "T", "B")

    assert opened[0].queries[0][1] == ["u-solo", "chat"]








# -----------------------------------------------------------
# notify_channel — the broadcast guard
# -----------------------------------------------------------

@pytest.mark.parametrize("channel", _NOT_A_CHANNEL)
@responses.activate
def test_a_broadcast_on_anything_but_the_four_channels_sends_nothing(app, db, channel, caplog):
    _seed_token(db, "broadcast-guard-owner", _expo_token("bguard"))
    _expo_accepts_all()

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert push.notify_channel(channel, "T", "B") == 0

    assert len(responses.calls) == 0
    assert "Refusing broadcast on unknown channel" in caplog.text


@responses.activate
def test_a_refused_broadcast_never_opens_a_database_connection(app, monkeypatch):
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_accepts_all()

    assert push.notify_channel("gossip", "T", "B") == 0


@responses.activate
def test_a_refused_broadcast_ignores_its_exclusion_too(app, monkeypatch):
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_accepts_all()

    assert push.notify_channel("gossip", "T", "B", exclude_user_id="u-1") == 0








# -----------------------------------------------------------
# notify_channel — the query and the exclusion
# -----------------------------------------------------------

@responses.activate
def test_a_broadcast_asks_the_database_exactly_once(app, db, make_user, monkeypatch):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("onequery"))
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel("news", "T", "B")

    assert len(opened) == 1
    assert len(opened[0].queries) == 1


@responses.activate
def test_without_an_exclusion_the_query_carries_only_the_channel(app, monkeypatch):
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel("news", "T", "B")

    sql, params = opened[0].queries[0]
    assert params == ["news"]
    assert "pt.user_id != ?" not in sql


@responses.activate
def test_an_exclusion_adds_one_parameter_and_one_clause(app, monkeypatch):
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel("news", "T", "B", exclude_user_id="u-author")

    sql, params = opened[0].queries[0]
    assert params == ["news", "u-author"]
    assert sql.rstrip().endswith("AND pt.user_id != ?")


@pytest.mark.parametrize("excluded", [None, "", 0, False])
@responses.activate
def test_a_falsy_exclusion_excludes_nobody(app, db, make_user, excluded, monkeypatch):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("falsyexcl"))
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B", exclude_user_id=excluded) == 1
    assert _sent_tokens() == [token]
    assert opened[0].queries[0][1] == ["news"]


@responses.activate
def test_an_exclusion_skips_every_device_that_user_owns(app, db, make_user):
    author, reader = make_user(), make_user()
    _seed_token(db, author["id"], _expo_token("authora"))
    _seed_token(db, author["id"], _expo_token("authorb"))
    theirs = _seed_token(db, reader["id"], _expo_token("readerx"))
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B", exclude_user_id=author["id"]) == 1
    assert _sent_tokens() == [theirs]


@responses.activate
def test_the_exclusion_is_parameterised_not_pasted(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("noinject"))
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B", exclude_user_id="' OR 1=1 --") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_the_exclusion_and_the_opt_out_both_apply(app, db, make_user):
    author, quiet, reader = make_user(), make_user(), make_user()
    _seed_token(db, author["id"], _expo_token("bothauthor"))
    _seed_token(db, quiet["id"], _expo_token("bothquiet"))
    theirs = _seed_token(db, reader["id"], _expo_token("bothreader"))
    _set_channel(db, quiet["id"], "news", 0)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B", exclude_user_id=author["id"]) == 1
    assert _sent_tokens() == [theirs]


@responses.activate
def test_excluding_the_only_user_who_had_not_opted_out_sends_nothing(app, db, make_user):
    quiet, author = make_user(), make_user()
    _seed_token(db, quiet["id"], _expo_token("lastquiet"))
    _seed_token(db, author["id"], _expo_token("lastauthor"))
    _set_channel(db, quiet["id"], "news", 0)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B", exclude_user_id=author["id"]) == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_broadcast_ignores_an_active_flag_that_is_not_one(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("bactivetwo"), active=2)
    _seed_token(db, user["id"], _expo_token("bactivezero"), active=0)
    live = _seed_token(db, user["id"], _expo_token("bactiveone"), active=1)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 1
    assert _sent_tokens() == [live]


@pytest.mark.parametrize("enabled", [1, 2, -1])
@responses.activate
def test_only_a_zero_switch_silences_a_broadcast(app, db, make_user, enabled):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token(f"benabled{abs(enabled)}"))
    _set_channel(db, user["id"], "news", enabled)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_broadcast_reaches_a_user_who_muted_a_different_channel(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("bothermute"))
    _set_channel(db, user["id"], "chat", 0)
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 1
    assert _sent_tokens() == [token]


@responses.activate
def test_a_broadcast_addresses_a_token_whose_owner_row_is_gone(app, db):
    # push_tokens is the only table the broadcast consults;
    # production relies on ON DELETE CASCADE to clear these
    _seed_token(db, "user-who-vanished", _expo_token("orphan"))
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 1


@responses.activate
def test_a_broadcast_counts_devices_not_users(app, db, make_user):
    one, two = make_user(), make_user()
    _seed_token(db, one["id"], _expo_token("cnt1a"))
    _seed_token(db, one["id"], _expo_token("cnt1b"))
    _seed_token(db, two["id"], _expo_token("cnt2a"))
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 3








# -----------------------------------------------------------
# notify_channel — delivery, state and containment
# -----------------------------------------------------------

@responses.activate
def test_a_broadcast_splits_the_languages_around_the_excluded_user(app, db, make_user):
    author, lt_user, en_user = make_user(), make_user(), make_user()
    _seed_token(db, author["id"], _expo_token("bxauthor"), language="en")
    lt_token = _seed_token(db, lt_user["id"], _expo_token("bxlt"), language="lt")
    en_token = _seed_token(db, en_user["id"], _expo_token("bxen"), language="en")
    _expo_accepts_all()

    sent = push.notify_channel("news", "Naujiena", "Tekstas", exclude_user_id=author["id"],
                               title_en="News", body_en="Text")

    assert sent == 2
    by_token = {msg["to"]: msg for msg in _all_messages()}
    assert by_token[lt_token]["title"] == "Naujiena"
    assert by_token[en_token]["title"] == "News"


@responses.activate
def test_a_broadcast_to_english_devices_only_is_one_request(app, db, make_user):
    one, two = make_user(), make_user()
    _seed_token(db, one["id"], _expo_token("enonly1"), language="en")
    _seed_token(db, two["id"], _expo_token("enonly2"), language="en")
    _expo_accepts_all()

    assert push.notify_channel("news", "Naujiena", "Tekstas", title_en="News", body_en="Text") == 2
    assert len(_push_bodies()) == 1
    assert {msg["title"] for msg in _all_messages()} == {"News"}


@responses.activate
def test_a_broadcast_counts_only_the_tickets_expo_accepted(app, db, make_user):
    one, two = make_user(), make_user()
    _seed_token(db, one["id"], _expo_token("mixok"))
    _seed_token(db, two["id"], _expo_token("mixerr"))
    _expo_replies({"data": [{"status": "ok", "id": "t1"},
                            {"status": "error", "details": {"error": "MessageTooBig"}}]})

    assert push.notify_channel("news", "T", "B") == 1


@responses.activate
def test_a_broadcast_adds_to_a_stats_dict_that_already_has_counts(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("bstatsadd"))
    _expo_accepts_all()
    stats = {"sent": 10, "failed": 4, "errors": {"Rejected": 1}}

    push.notify_channel("news", "T", "B", stats=stats)

    assert stats["sent"] == 11
    assert stats["failed"] == 4
    assert stats["errors"] == {"Rejected": 1}


@responses.activate
def test_a_broadcast_with_no_devices_leaves_the_stats_dict_empty(app, db):
    _expo_accepts_all()
    stats = {}

    assert push.notify_channel("news", "T", "B", stats=stats) == 0
    assert stats == {}


@responses.activate
def test_two_broadcasts_in_a_row_each_reach_the_same_device(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("btwice"))
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 1
    assert push.notify_channel("news", "T", "B") == 1
    assert _sent_tokens() == [token, token]


@responses.activate
def test_a_device_retired_by_the_first_broadcast_is_gone_from_the_second(app, db, make_user):
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("bdead"))
    _expo_replies({"data": [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]})
    _expo_accepts_all()

    assert push.notify_channel("news", "T", "B") == 0
    assert _token_row(db, token)["active"] == 0

    responses.calls.reset()
    assert push.notify_channel("news", "T", "B") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_a_device_retired_between_the_query_and_the_send_is_still_addressed(app, db, make_user, monkeypatch):
    # The rows are a snapshot: a row flipped to active=0 after
    # the SELECT still gets this message, and drops out of the
    # next broadcast instead
    user = make_user()
    token = _seed_token(db, user["id"], _expo_token("snapshot"))
    _expo_accepts_all()
    real_batch = push.send_push_batch

    def _retire_then_send(tokens, *args, **kwargs):
        db.execute("UPDATE push_tokens SET active = 0 WHERE token = ?", (token,))
        db.commit()
        return real_batch(tokens, *args, **kwargs)

    monkeypatch.setattr(push, "send_push_batch", _retire_then_send)

    assert push.notify_channel("news", "T", "B") == 1
    assert _sent_tokens() == [token]

    monkeypatch.setattr(push, "send_push_batch", real_batch)
    responses.calls.reset()
    assert push.notify_channel("news", "T", "B") == 0


@responses.activate
def test_a_broadcast_closes_its_connection(app, db, make_user, monkeypatch):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("bclose"))
    opened = _record_db(monkeypatch)
    _expo_accepts_all()

    push.notify_channel("news", "T", "B")

    assert opened[0].closed is True


@responses.activate
def test_a_broadcast_closes_its_connection_when_the_query_raises(app, monkeypatch):
    angry = _AngryDb()
    monkeypatch.setattr(push, "get_db", lambda: angry)
    _expo_accepts_all()

    with pytest.raises(sqlite3.OperationalError):
        push.notify_channel("news", "T", "B")

    assert angry.closed is True
    assert len(responses.calls) == 0


@responses.activate
def test_a_broadcast_lets_an_unopenable_database_reach_the_caller(app, monkeypatch):
    monkeypatch.setattr(push, "get_db", _explode)
    _expo_accepts_all()

    with pytest.raises(sqlite3.OperationalError):
        push.notify_channel("news", "T", "B")


@responses.activate
def test_a_broadcast_survives_expo_answering_a_json_array(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("barray"))
    _expo_replies([{"status": "ok"}])

    assert push.notify_channel("news", "T", "B") == 0


@responses.activate
def test_a_broadcast_survives_a_ticket_that_is_not_an_object(app, db, make_user):
    user = make_user()
    _seed_token(db, user["id"], _expo_token("bnotobj"))
    _expo_replies({"data": ["nope"]})

    assert push.notify_channel("news", "T", "B") == 0
