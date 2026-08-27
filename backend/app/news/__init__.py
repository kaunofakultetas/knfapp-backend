############################################################
#  [*] News — package marker for the feed
#
#  routes.py is the whole module: the ranked feed (scraped
#  faculty articles and user posts in one stream), post
#  creation and deletion, likes, comments and the polls that
#  hang off a post.
#
#  news_bp is NOT re-exported here — create_app() imports it
#  from app.news.routes directly.
#
#  Used by:
#    - app/__init__.py — registers news_bp at /api/news
#    - scraper/knf_scraper.py, vu_scraper.py — write the
#      scraped posts this serves
#    - mobile services/api/news.ts, social.ts — the feed
############################################################
