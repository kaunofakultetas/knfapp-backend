############################################################
#  [*] news core — visibility, wire shapes, cache watermark
#
#  The pure(ish) helpers every news route shares, kept in
#  one module so the wire contract cannot drift between
#  routes: the lenient ISO parse (query
#  strings eat '+'), the overflow-safe UTC move, the ONE
#  visibility predicate, the post/poll wire shapes and the
#  feed's cheap change fingerprint.
#
#  Split into:
#
#    parse_iso / as_utc / to_utc_iso — timestamp repairs
#    can_view_post                   — the visibility gate
#    post_to_dict                    — the NewsPost wire shape
#    feed_version                    — the ETag watermark
#    poll_shape / polls_for_posts / poll_to_dict
############################################################


from datetime import datetime, timezone


from knfapp.news.models import NewsPost, Poll, PollOption, PollVote
from knfapp.social.models import Friendship


STAFF_ROLES = ("admin", "curator", "teacher")

MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 10000
SUMMARY_LENGTH = 200
MAX_COMMENT_LENGTH = 2000
MAX_POLL_OPTION_LENGTH = 100
MIN_POLL_OPTIONS = 2
MAX_POLL_OPTIONS = 10
FEED_CACHE_MAX_AGE = 60








############################################################
# parse_iso / as_utc / to_utc_iso
############################################################
#
# parse_iso forgives the two mutilations a timestamp
# suffers in a query string — a space where the 'T' was and
# a '+' that arrived as a space — and answers an AWARE
# datetime (naive reads as UTC) or None. as_utc moves one
# aware stamp onto UTC, answering None when UTC cannot hold
# it: "0001-01-01T00:00:00+14:00" parses happily and then
# overflows astimezone(), which would be a 500 PAST every
# validation gate. to_utc_iso normalises a STORED string to
# explicit-UTC T-form for the wire, handing back an
# unparseable one untouched — a null endDate would read as
# "never closes".
#
# Used by:
#   - api/views.py — get_feed (?before), create_poll,
#     vote_poll, get_comments
#   - poll_shape (below) — endDate on the way out
############################################################

def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) > 10 and text[10] == " ":
        text = text[:10] + "T" + text[11:]
    text = text.replace(" ", "+")
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def as_utc(parsed):
    if parsed is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        return None


def to_utc_iso(value):
    parsed = as_utc(parse_iso(value))
    if parsed is None:
        return value if isinstance(value, str) and value.strip() else None
    return parsed.isoformat()








############################################################
# can_view_post
############################################################
#
# The ONE visibility predicate: True when `user` (None for
# a guest) may see this news_posts row. Public rows are
# open to everyone. A private row is served to its author,
# to an admin, to the other STAFF_ROLES when it is not a
# wall post, and — for wall posts, source 'user' — to the
# author's friends (friendships is written in BOTH
# directions on accept, so one direction suffices).
#
# Callers answer 404 (never 403) on False: a stranger must
# not be able to tell "private" from "missing". The row
# needs is_public, author_id and source — GATE_FIELDS is
# exactly that.
#
# Used by:
#   - api/views.py — every per-post route
############################################################

GATE_FIELDS = ("id", "author_id", "source", "is_public")


def can_view_post(row, user):
    if row["is_public"]:
        return True
    if not user:
        return False
    if row["author_id"] and row["author_id"] == user["id"]:
        return True
    if user["role"] == "admin":
        return True
    if row["source"] != "user":
        return user["role"] in STAFF_ROLES
    return Friendship.objects.filter(user_id=user["id"], friend_id=row["author_id"]).exists()








############################################################
# post_to_dict
############################################################
#
# The wire shape of one post (camelCase — the mobile
# NewsPost type) and the ONE producer of it. NOT included:
# the viewer's `liked` flag and the additive `poll` object,
# attached per page / per row by the callers. `author` is
# the JOINed live display name with the row's snapshot as
# fallback; the counters are the denormalised columns.
#
# Takes the values() dict of POST_FIELDS plus the annotated
# live_author_name.
#
# Used by:
#   - api/views.py — get_feed, create_post, get_post
############################################################

POST_FIELDS = (
    "id", "title", "content", "summary", "image_url", "author_id",
    "author_name", "source", "source_url", "post_type",
    "likes_count", "comments_count", "shares_count",
    "published_at", "is_public",
)


def post_to_dict(row):
    return {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "summary": row["summary"],
        "imageUrl": row["image_url"],
        "author": row.get("live_author_name") or row["author_name"],
        "authorId": row["author_id"],
        "source": row["source"],
        "sourceUrl": row["source_url"],
        "postType": row["post_type"],
        "likes": row["likes_count"],
        "comments": row["comments_count"],
        "shares": row["shares_count"],
        "date": row["published_at"],
        "isPublic": bool(row["is_public"]),
    }








############################################################
# feed_version
############################################################
#
# The cheap "has anything changed" fingerprint of
# news_posts: row count, newest published_at/updated_at,
# and each engagement counter summed on its OWN term — a
# like or a comment changes the feed's bytes without moving
# a stamp, and an unlike plus a new comment cancel out
# inside one summed term. Derived from the DATA, never a
# body hash.
#
# Used by:
#   - api/views.py — get_feed's ETag seed
############################################################

def feed_version():
    from django.db.models import Count, Max, Sum
    row = NewsPost.objects.aggregate(
        rows_total=Count("id"), newest=Max("published_at"), touched=Max("updated_at"),
        likes=Sum("likes_count"), comments=Sum("comments_count"), shares=Sum("shares_count"),
    )
    return (
        f"{row['rows_total']}:{row['newest'] or '-'}:{row['touched'] or '-'}"
        f":{row['likes'] or 0}:{row['comments'] or 0}:{row['shares'] or 0}"
    )








############################################################
# poll_shape / polls_for_posts / poll_to_dict
############################################################
#
# The ONE producer of the poll wire shape (the mobile
# PollResponse); options ride in creation order (the
# explicit position column — the live table's rowid order).
# polls_for_posts serves a whole page in THREE queries —
# the polls, all their options, the caller's votes —
# instead of two per card; poll_to_dict is the single-poll
# form the poll routes use.
#
# Used by:
#   - api/views.py — get_feed, get_post, get_poll,
#     create_poll, vote_poll
############################################################

def poll_shape(poll_row, option_rows, user_vote):
    return {
        "id": poll_row["id"],
        "postId": poll_row["post_id"],
        "title": poll_row["title"],
        "endDate": to_utc_iso(poll_row["end_date"]),
        "totalVotes": poll_row["total_votes"],
        "createdAt": poll_row["created_at"],
        "userVote": user_vote,
        "options": [{"id": o["id"], "text": o["text"], "votes": o["votes"]} for o in option_rows],
    }


def polls_for_posts(post_ids, user_id=None):
    if not post_ids:
        return {}
    poll_rows = list(Poll.objects.filter(post_id__in=post_ids)
                     .values("id", "post_id", "title", "end_date", "total_votes", "created_at"))
    if not poll_rows:
        return {}

    poll_ids = [r["id"] for r in poll_rows]
    options = {}
    for opt in PollOption.objects.filter(poll_id__in=poll_ids).order_by("position", "id") \
                                 .values("id", "poll_id", "text", "votes"):
        options.setdefault(opt["poll_id"], []).append(opt)

    votes = {}
    if user_id:
        for vote in PollVote.objects.filter(user_id=user_id, poll_id__in=poll_ids).values("poll_id", "option_id"):
            votes[vote["poll_id"]] = vote["option_id"]

    return {
        row["post_id"]: poll_shape(row, options.get(row["id"], []), votes.get(row["id"]))
        for row in poll_rows
    }


def poll_to_dict(poll_row, user_id=None):
    options = list(PollOption.objects.filter(poll_id=poll_row["id"]).order_by("position", "id")
                   .values("id", "text", "votes"))
    user_vote = None
    if user_id:
        vote = PollVote.objects.filter(poll_id=poll_row["id"], user_id=user_id).values("option_id").first()
        if vote:
            user_vote = vote["option_id"]
    return poll_shape(poll_row, options, user_vote)








############################################################
# etag_for / if_none_match_contains
############################################################
#
# Re-exported from common/http.py — schedule and info share
# the same pair; the names stay here for the feed's callers.
#
# Used by:
#   - api/views.py — get_feed
############################################################

from knfapp.common.http import etag_for, if_none_match_contains  # noqa: E402,F401
