############################################################
#  [*] ops — operational commands
#
#  No models, no routes: the home of the management
#  commands an operator runs by hand (the data cutover).
#  The scheduled ticks live with the scraper app instead —
#  the cron container owns those.
############################################################


from django.apps import AppConfig


class OpsConfig(AppConfig):
    name = "knfapp.ops"
    label = "ops"
