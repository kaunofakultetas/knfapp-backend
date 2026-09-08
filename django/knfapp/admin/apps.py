############################################################
#  [*] admin — app registration
#
#  The label 'admin' is free on purpose: this project
#  installs no contrib admin site (settings.py) — the admin
#  console is the mobile app's own screens over /api/admin.
############################################################


from django.apps import AppConfig


class AdminConfig(AppConfig):
    name = "knfapp.admin"
    label = "admin"
