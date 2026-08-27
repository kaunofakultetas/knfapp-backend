############################################################
#  [*] Social — package marker for profiles and friends
#
#  routes.py is the whole module: the user-post feed, public
#  and own profiles, the friend request lifecycle (send,
#  list, accept, reject/cancel, unfriend) and a wall-post
#  endpoint the app never calls. Several routes here are
#  duplicates of /api/auth and /api/news equivalents the app
#  actually uses — see the module header for which.
#
#  social_bp is NOT re-exported here — create_app() imports
#  it from app.social.routes directly.
#
#  Used by:
#    - app/__init__.py — registers social_bp at /api/social
#    - mobile services/api/social.ts — friends and profile
#      screens
############################################################
