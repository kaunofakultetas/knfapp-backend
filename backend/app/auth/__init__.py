############################################################
#  [*] Auth — package marker for accounts and sessions
#
#  routes.py is the whole module: invitation-based
#  registration, login, the caller's own profile and logout,
#  all on opaque uuid4 bearer sessions (30 days) rather than
#  JWTs. It also owns require_auth / require_role, the gate
#  decorators every other blueprint imports, and
#  get_current_user, which both enforce users.active.
#
#  auth_bp is NOT re-exported here — create_app() and the
#  other blueprints import from app.auth.routes directly.
#
#  Used by:
#    - app/__init__.py — registers auth_bp at /api/auth
#    - every other routes.py — require_auth / require_role
#    - mobile services/api/auth.ts — AuthContext
############################################################
