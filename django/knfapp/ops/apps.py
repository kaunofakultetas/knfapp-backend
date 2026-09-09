############################################################
#  [*] ops — operational commands
#
#  The home of the health probe and the management
#  commands an operator runs by hand (grant_role). The
#  scheduled ticks live with the scraper app instead —
#  the cron container owns those.
############################################################


from django.apps import AppConfig


class OpsConfig(AppConfig):
    name = "knfapp.ops"
    label = "ops"
