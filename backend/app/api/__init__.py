############################################################
#  [*] API — helpers shared by the JSON route modules
#
#  One helper so far: parse_pagination, the page/per_page
#  query-string parser the feed-style GET routes share. It
#  answers a ready-made (response, 400) tuple instead of
#  raising, so a route does
#
#    page, per_page, err = parse_pagination()
#    if err:
#        return err
#
#  and Flask accepts the tuple as the return value as-is.
############################################################


from flask import jsonify, request








############################################################
# parse_pagination
############################################################
#
# Reads ?page= and ?per_page= from request.args and returns
# (page, per_page, None) or (None, None, (response, 400)).
# page must be an int >= 1 and is capped at 10 000 so the
# OFFSET the callers build from it stays sane; per_page must
# be an int >= 1 and is silently CLAMPED to max_per_page
# rather than rejected. Absent params fall back to page 1
# and default_per_page. int() accepts "+3" and " 3 " but not
# "3.0"; the TypeError branches are unreachable, args values
# are always str. Every caller today runs with the defaults
# (max 100, default 20).
#
# Used by:
#   - news/routes.py — get_feed, get_comments
#   - social/routes.py — social_feed, get_user_posts
############################################################

def parse_pagination(max_per_page=100, default_per_page=20):
    raw_page = request.args.get("page")
    raw_per_page = request.args.get("per_page")


    # STEP 1: page — positive int, capped so OFFSET stays sane
    # ========================================================
    # Re-bound on every call; a module constant would do the same job
    _MAX_PAGE = 10_000
    if raw_page is not None:
        try:
            page = int(raw_page)
        except (ValueError, TypeError):
            return None, None, (jsonify({"error": "page must be a positive integer"}), 400)
        if page < 1:
            return None, None, (jsonify({"error": "page must be a positive integer"}), 400)
        if page > _MAX_PAGE:
            return None, None, (jsonify({"error": f"page must be at most {_MAX_PAGE}"}), 400)
    else:
        page = 1


    # STEP 2: per_page — positive int, clamped (not rejected) above
    # max_per_page
    # =============================================================
    if raw_per_page is not None:
        try:
            per_page = int(raw_per_page)
        except (ValueError, TypeError):
            return None, None, (jsonify({"error": "per_page must be a positive integer"}), 400)
        if per_page < 1:
            return None, None, (jsonify({"error": "per_page must be a positive integer"}), 400)
        per_page = min(per_page, max_per_page)
    else:
        per_page = default_per_page

    return page, per_page, None
