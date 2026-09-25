############################################################
#  [*] news core — visibility, wire shapes, feed fingerprint
#
#  The pure(ish) helpers every news route shares, kept in
#  one module so the wire contract cannot drift between
#  routes: the lenient ISO parse (query strings eat '+'),
#  the overflow-safe UTC move, the ONE visibility
#  predicate, the post/poll wire shapes and the feed's
#  cheap change fingerprint.
#
#  Split into:
#
#    parse_iso / as_utc / to_utc_iso — timestamp repairs
#    block_set / blocked_pair        — the block relation
#    can_view_post                   — the visibility gate
#    lock_post                       — the writers' lock order
#    wall_visibility_q               — the wall list's slice
#    subtract_blocked_engagement     — the reader's tallies
#    derive_title                    — an untitled post's title
#    post_to_dict                    — the NewsPost wire shape
#    feed_version                    — the ETag seed
#    inactive_authors_stamp          — the seed's liveness term
#    poll_shape / polls_for_posts / poll_to_dict
############################################################


from datetime import datetime, timezone


from django.db import models


from knfapp.news.models import NewsComment, NewsLike, NewsPost, Poll, PollOption, PollVote
from knfapp.social.models import Friendship, UserBlock
from knfapp.users.models import User


STAFF_ROLES = ("admin", "curator", "teacher")

MAX_TITLE_LENGTH = 200
# How long a title derived from an untitled post's body may run
DERIVED_TITLE_LENGTH = 80
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
# validation gate. to_utc_iso normalises a stored stamp to
# explicit-UTC ISO for the wire, handing back an
# unparseable string untouched — a null endDate would read
# as "never closes".
#
# Used by:
#   - api/views.py — get_feed (?before), create_poll,
#     vote_poll, get_comments
#   - poll_shape (below) — endDate on the way out
############################################################

def parse_iso(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
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


def feed_stamp(value):
    # ETag seeds want a stable TEXT token for a stamp column's
    # aggregate — datetime in, canonical ISO out
    return value.isoformat() if isinstance(value, datetime) else (value or "-")








############################################################
# block_set / blocked_pair
############################################################
#
# The block relation in the two shapes the gates need: the
# SET of ids on either side of a block with `user_id` — the
# accounts they blocked and the accounts that blocked them,
# one query, empty for a guest — and the yes/no pair test.
# user_blocks holds one row blocker→blocked; every
# enforcement site treats it as bidirectional, so both
# helpers OR the two directions.
#
# Used by:
#   - can_view_post / wall_visibility_q (below)
#   - api/views.py — get_feed (the exclusion and the ETag
#     seed), get_comments, _can_engage
#   - social/api/views.py — social_feed, get_profile,
#     get_user_posts, send_friend_request
############################################################

def block_set(user_id):
    if not user_id:
        return set()
    rows = UserBlock.objects.filter(
        models.Q(blocker_id=user_id) | models.Q(blocked_id=user_id),
    ).values_list("blocker_id", "blocked_id")
    ids = set()
    for blocker, blocked in rows:
        ids.add(blocked if blocker == user_id else blocker)
    return ids


def blocked_pair(a, b):
    return UserBlock.objects.filter(
        models.Q(blocker_id=a, blocked_id=b) | models.Q(blocker_id=b, blocked_id=a),
    ).exists()








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
# The block rule sits in front of that table and applies to
# WALL rows only: a blocked pair (either direction) never
# sees each other's wall posts, public ones included. It
# deliberately does NOT reach official content — a faculty
# announcement or a scraped article stays readable by an
# account its author blocked, because a teacher blocking a
# student must not cut that student off from the faculty's
# word; the engagement writes carry their own pair test for
# those rows (api/views.py _can_engage). An admin keeps
# their bypass on the block as on everything else. The
# caller may hand in the viewer's block_set when it already
# has one (a page of rows); otherwise the gate fetches it
# for the wall rows that need it.
#
# The liveness rule sits in front of both: a WALL post whose
# author is deactivated (the admin console's moderation
# lever, and erasure's end state) is nobody's but an
# admin's — the social app's own rule (social_feed,
# get_profile, get_user_posts), so a moderated account's
# post cannot stay readable, likeable and shareable through
# /api/news while every social surface calls it gone.
# Official rows ride on as with the block. The row carries
# the author's flag as author__active (both projections
# below fetch it); None — no author joined, or a hand-built
# row — places no liveness restriction.
#
# Callers answer 404 (never 403) on False: a stranger must
# not be able to tell "private" — or "blocked" — from
# "missing". The row needs is_public, author_id, source and
# the author's active flag — GATE_FIELDS is exactly that.
#
# Used by:
#   - api/views.py — every per-post route
############################################################

GATE_FIELDS = ("id", "author_id", "source", "is_public", "author__active")


def can_view_post(row, user, blocked=None):
    author_active = row.get("author__active")
    if (row["source"] == "user" and row["author_id"] and author_active is not None
            and not author_active and not (user and user["role"] == "admin")):
        return False
    if (user and row["source"] == "user" and user["role"] != "admin"
            and row["author_id"] and row["author_id"] != user["id"]):
        if blocked is None:
            blocked = block_set(user["id"])
        if row["author_id"] in blocked:
            return False
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
# lock_post
############################################################
#
# The one lock order of every writer that touches a post's
# children: the news_posts row FIRST (SELECT … FOR UPDATE,
# held to the request's commit under ATOMIC_REQUESTS), the
# likes, comments, poll options and votes after. Two racing
# likes then serialise on the post — the second one's
# correlated recount runs on a snapshot that already holds
# the first one's news_likes row, the window a one-statement
# COUNT alone cannot close (a blocked UPDATE re-checks only
# its target row, never the subquery) — and because the
# deletes, the vote and the comment routes all take the SAME
# row first, no two writers can deadlock by holding a child
# while waiting for the post (erasure, the only writer
# outside the two apps, also moves the post rows before
# their children). Join-free on purpose: FOR UPDATE refuses
# the nullable side of an outer join, and the gate reads
# that follow join users. SQLite ignores the clause — it
# serialises writers anyway.
#
# Used by:
#   - api/views.py — toggle_like, add_comment,
#     delete_comment, delete_post, create_poll, delete_poll,
#     vote_poll
#   - social/api/views.py — delete_post (the wall delete)
############################################################

def lock_post(post_id):
    list(NewsPost.objects.select_for_update().filter(id=post_id).values_list("id", flat=True))








############################################################
# wall_visibility_q
############################################################
#
# can_view_post's rule as ONE Q over an author's wall list
# (the rows a profile lists and counts — source 'user' and
# 'faculty'), so the social routes cannot restate it and
# drift. No filter at all for the subject themselves or an
# admin. Everyone else starts from the public rows;
# friendship unlocks the private WALL rows only (a friend
# of a teacher is not a proof-reader of their private
# faculty draft); a STAFF_ROLES viewer unlocks the private
# non-wall rows (the draft) whoever wrote them. `blocked`
# is the pair test's answer: a block hides every wall row
# from both parties, the official rows stay.
#
# Used by:
#   - social/api/views.py — get_profile (postCount),
#     get_user_posts
############################################################

def wall_visibility_q(viewer, author_id, is_friend, blocked=False):
    if viewer and (viewer["id"] == author_id or viewer["role"] == "admin"):
        return models.Q()
    visible = models.Q(is_public=1)
    if viewer and is_friend:
        visible |= models.Q(source="user")
    if viewer and viewer["role"] in STAFF_ROLES:
        visible |= ~models.Q(source="user")
    if blocked:
        visible &= ~models.Q(source="user")
    return visible








############################################################
# subtract_blocked_engagement
############################################################
#
# The engagement a reader can NOT see, taken out of the
# tallies they are shown: likes and comments on these posts
# by accounts on either side of a block with the reader.
# get_comments already drops those comments (its total
# follows), so the denormalised counter on a card said 5
# where the thread the reader opened showed 4; every route
# that serves post counters subtracts this reader's slice,
# so the numbers agree with what they can open. The stored
# counters stay the global truth. Two grouped queries per
# page — and none at all for a reader with no block, the
# common case. `posts` are wire dicts (likes / comments),
# patched in place.
#
# Used by:
#   - api/views.py — get_feed, get_post, delete_comment
#   - social/api/views.py — social_feed, get_user_posts
############################################################

def subtract_blocked_engagement(posts, blocked):
    if not blocked or not posts:
        return
    ids = [p["id"] for p in posts]


    def hidden(model):
        return dict(
            model.objects.filter(post_id__in=ids, user_id__in=blocked)
            .values("post_id").annotate(c=models.Count("*")).values_list("post_id", "c")
        )

    likes, comments = hidden(NewsLike), hidden(NewsComment)
    for p in posts:
        p["likes"] = max(0, p["likes"] - likes.get(p["id"], 0))
        p["comments"] = max(0, p["comments"] - comments.get(p["id"], 0))








############################################################
# derive_title
############################################################
#
# The title an untitled post gets: its first line, whole
# when it fits DERIVED_TITLE_LENGTH, else cut at the last
# word boundary with an ellipsis — never mid-word ("…geria"),
# never across a line break. A first line with no boundary
# late enough (one long word, a URL) is hard-cut. The client
# recognises a derived title as the head of the body
# (services/newsText titleRepeatsBody) and never prints the
# same words twice — a short post's card shows it once, the
# article shows the body alone.
#
# Used by:
#   - api/views.py — create_post
#   - social/api/views.py — create_post, update_post
############################################################

def derive_title(content):
    first = (content or "").strip().split("\n", 1)[0].strip()
    if len(first) <= DERIVED_TITLE_LENGTH:
        return first
    head = first[:DERIVED_TITLE_LENGTH - 1]
    cut = head.rfind(" ")
    if cut >= DERIVED_TITLE_LENGTH // 2:
        head = head[:cut]
    return head.rstrip(" ,.;:!?–—-") + "…"








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
# truncate=True cuts the body to SUMMARY_LENGTH for list
# pages — a feed card renders the summary, never the body,
# and the article screen fetches the post whole anyway —
# and `truncated` tells a cut body from a genuinely short
# one (the same contract social's _post_row_to_dict keeps
# for the community feed). Python slices by code point, so
# a cut never strands half a surrogate pair.
#
# Takes the values() dict of POST_FIELDS plus the annotated
# live_author_name; POST_FIELDS also carries the author's
# active flag, because get_post runs these rows through
# can_view_post (the liveness rule).
#
# Used by:
#   - api/views.py — get_feed (truncated on ?truncate=1),
#     create_post, get_post
############################################################

POST_FIELDS = (
    "id", "title", "content", "summary", "image_url", "author_id",
    "author_name", "source", "source_url", "post_type",
    "likes_count", "comments_count", "shares_count",
    "published_at", "is_public", "author__active",
)


def post_to_dict(row, truncate=False):
    content = row["content"] or ""
    truncated = truncate and len(content) > SUMMARY_LENGTH
    return {
        "id": row["id"],
        "title": row["title"],
        "content": content[:SUMMARY_LENGTH] if truncated else content,
        "truncated": truncated,
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
# news_posts: row count, newest published_at, newest
# updated_at, and each engagement counter summed on its
# OWN term. The stamp is the term that really moves: every
# write that changes what a feed page shows — a like or
# unlike, a comment added or deleted, a share, a poll
# attached, detached or voted on, a wall edit — bumps the
# post's updated_at (api/views.py, social/api/views.py), so
# Max(updated_at) advances on EVERY mutation. Global sums
# alone cannot see a like moved from one post to another
# (an unlike plus a like leave every sum where it was while
# the bytes and the ranking changed), and a poll write
# touches no counter at all; the sums stay as a cheap
# second witness. The trade-off, accepted on purpose: one
# engagement anywhere invalidates every viewer's tag — it
# already did for a like, and a stale 304 is the worse
# failure. Derived from the DATA, never a body hash.
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
        f"{row['rows_total']}:{feed_stamp(row['newest'])}:{feed_stamp(row['touched'])}"
        f":{row['likes'] or 0}:{row['comments'] or 0}:{row['shares'] or 0}"
    )








############################################################
# inactive_authors_stamp
############################################################
#
# The seed term for the feed's liveness filter: a wall post
# drops out of every page the moment its author is
# deactivated, and deactivation writes the users row, never
# news_posts — so feed_version alone would answer a 304 and
# keep the moderated post on a cached page. Both writers of
# the flag (the admin console's user update and erasure)
# stamp users.updated_at, so the count of inactive accounts
# plus their newest stamp moves on every deactivation and
# every reactivation. One aggregate over the users table.
#
# Used by:
#   - api/views.py — get_feed's ETag seed
############################################################

def inactive_authors_stamp():
    from django.db.models import Count, Max
    row = User.objects.filter(active=False).aggregate(n=Count("id"), touched=Max("updated_at"))
    return f"{row['n']}:{feed_stamp(row['touched'])}"








############################################################
# poll_shape / polls_for_posts / poll_to_dict
############################################################
#
# The ONE producer of the poll wire shape (the mobile
# PollResponse); options ride in creation order (the
# position column). polls_for_posts serves a whole page in
# THREE queries — the polls, all their options, the
# caller's votes — instead of two per card; poll_to_dict
# is the single-poll form the poll routes use.
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
        # Derived, not stored — the options are always in hand,
        # so the total can never drift from what the widget sums
        "totalVotes": sum(o["votes"] for o in option_rows),
        "createdAt": poll_row["created_at"],
        "userVote": user_vote,
        "options": [{"id": o["id"], "text": o["text"], "votes": o["votes"]} for o in option_rows],
    }


def polls_for_posts(post_ids, user_id=None):
    if not post_ids:
        return {}
    poll_rows = list(Poll.objects.filter(post_id__in=post_ids)
                     .values("id", "post_id", "title", "end_date", "created_at"))
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
# the same pair; the feed's callers import them from here.
#
# Used by:
#   - api/views.py — get_feed
############################################################

from knfapp.common.http import etag_for, if_none_match_contains  # noqa: E402,F401
