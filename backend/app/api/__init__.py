############################################################
#  [*] API — helpers shared by the JSON route modules
#
#  The pieces more than one blueprint needs:
#
#    MAX_TITLE_LENGTH / MAX_CONTENT_LENGTH / SUMMARY_LENGTH
#                     — the post-body limits, one home for
#                       what news and social hand-synced
#    FEED_SCORE_SQL   — the feed-ranking SQL fragment
#    parse_pagination — the page/per_page query-string parser
#                       the feed-style GET routes share
#
#  parse_pagination answers a ready-made (response, 400)
#  tuple instead of raising, so a route does
#
#    page, per_page, err = parse_pagination()
#    if err:
#        return err
#
#  and Flask accepts the tuple as the return value as-is.
############################################################


from flask import jsonify, request

# The post-body limits the writing routes enforce. They
# lived twice — one hand-synced copy in news/routes.py and
# one in social/routes.py — and drifting apart was a matter
# of time; BOTH read them from here now. SUMMARY_LENGTH is
# both the summary create_post stores and the length the
# list endpoints trim "content" to.
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 10000
SUMMARY_LENGTH = 200

# The one feed-ranking formula: 100 / (1 + age in days) for
# recency plus min(likes + 2*comments + 3*shares, 100) / 2
# for engagement — engagement tops out at 50, so a brand new
# post always outranks a day-old one. Interpolate the "now"
# expression before use: .format(now="'now'") for the live
# window, .format(now="?") when the caller pins it to a
# ?before timestamp. The column names stay unqualified,
# which is unambiguous even when a feed joins users (that
# table has none of them). MAX(0, ...) keeps a future
# published_at from exploding the recency term and the two
# COALESCEs keep an unparseable timestamp or a NULL counter
# from ranking as NULL.
#
# Used by:
#   - social/routes.py — social_feed
#   - news/routes.py keeps its OWN copy for now: the news
#     feed mixes sources and adds a per-source boost term to
#     the same two factors. The two want to become one
#     fragment plus an optional boost.
FEED_SCORE_SQL = (
    "COALESCE((1.0 / (1.0 + MAX(0, julianday({now}) - julianday(published_at)))) * 100, 0)"
    " + COALESCE(MIN(likes_count + comments_count * 2 + shares_count * 3, 100) * 0.5, 0)"
)








############################################################
# parse_pagination
############################################################
#
# Reads ?page= and ?per_page= from request.args and returns
# (page, per_page, None) or (None, None, (response, 400)).
# page must be an int >= 1 and is capped at max_page so the
# OFFSET the callers build from it stays sane; per_page must
# be an int >= 1 and at most max_per_page — 50, the maximum
# swagger has always published, and over it is a 400 like
# every other out-of-range parameter here. It used to default
# to 100 AND clamp silently, so a client asking for 100 got
# 100 rows back while the contract said 50. Absent params
# fall back to page 1 and default_per_page. int() accepts
# "+3" and " 3 " but not "3.0"; the TypeError branches are
# unreachable, args values are always str.
#
# The 10 000 default page cap still let a caller ask for
# OFFSET 999 900 — a full scan of the table per request —
# so the list routes pass a much lower max_page of their
# own; nothing about the answer shape changes.
#
# Used by:
#   - news/routes.py — get_feed, get_comments
#   - social/routes.py — social_feed, get_user_posts,
#     list_friends, list_friend_requests (their own
#     max_page / max_per_page)
############################################################

def parse_pagination(max_per_page=50, default_per_page=20, max_page=10_000):
    raw_page = request.args.get("page")
    raw_per_page = request.args.get("per_page")


    # STEP 1: page — positive int, capped so OFFSET stays sane
    # ========================================================
    if raw_page is not None:
        try:
            page = int(raw_page)
        except (ValueError, TypeError):
            return None, None, (jsonify({"error": "page must be a positive integer"}), 400)
        if page < 1:
            return None, None, (jsonify({"error": "page must be a positive integer"}), 400)
        if page > max_page:
            return None, None, (jsonify({"error": f"page must be at most {max_page}"}), 400)
    else:
        page = 1


    # STEP 2: per_page — positive int, REJECTED above max_per_page
    # (the page cap in STEP 1 already works that way)
    # ============================================================
    if raw_per_page is not None:
        try:
            per_page = int(raw_per_page)
        except (ValueError, TypeError):
            return None, None, (jsonify({"error": "per_page must be a positive integer"}), 400)
        if per_page < 1:
            return None, None, (jsonify({"error": "per_page must be a positive integer"}), 400)
        if per_page > max_per_page:
            return None, None, (jsonify({"error": f"per_page must be at most {max_per_page}"}), 400)
    else:
        per_page = default_per_page

    return page, per_page, None
