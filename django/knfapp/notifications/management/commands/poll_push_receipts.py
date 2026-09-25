############################################################
#  [*] poll_push_receipts — the receipt pass for cron
#
#  Trades every push ticket that has waited its 15 minutes
#  for Expo's receipt — the tickets of EVERY process, since
#  the queue is the shared store in notifications/push.py —
#  retiring uninstalled devices and logging the operator-
#  level failures (InvalidCredentials, MessageRateExceeded)
#  that are otherwise invisible. The server's own watcher
#  thread runs the same pass while it is alive; this command
#  is the clock that does not depend on the server having
#  sent anything lately, and the one that reaches tickets a
#  cron fan-out left behind when its process exited.
#
#  Meant for the cron container, every 15 minutes:
#    */15 * * * *  docker exec knfapp-django python3 manage.py poll_push_receipts
############################################################


import logging

from django.core.management.base import BaseCommand

from knfapp.notifications.push import pending_push_receipts, poll_push_receipts

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Trade due Expo push tickets for their receipts and retire uninstalled devices"

    def handle(self, *args, **options):
        checked = 0
        try:
            checked = poll_push_receipts()
        except Exception:
            logger.exception("Push receipt pass failed")

        self.stdout.write(f"Push receipts: {checked} checked, {pending_push_receipts()} pending")
