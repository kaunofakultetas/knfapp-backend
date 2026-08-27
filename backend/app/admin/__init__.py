############################################################
#  [*] Admin — package marker for the admin console API
#
#  routes.py is the whole module: invitation codes (mint,
#  list, revoke), the user table (list, role change,
#  activate/deactivate), the dashboard counters and the
#  push broadcast. Every route is require_role("admin"),
#  so the blueprint is invisible to students.
#
#  admin_bp is NOT re-exported here — create_app() imports
#  it from app.admin.routes directly, so importing this
#  package pulls in nothing.
#
#  Used by:
#    - app/__init__.py — registers admin_bp at /api/admin
#      (via app.admin.routes)
#    - mobile services/api/admin.ts — the admin screens
############################################################
