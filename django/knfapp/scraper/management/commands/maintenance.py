############################################################
#  [*] maintenance — the daily housekeeping tick
#
#  The rows nothing else ever deletes, in one command so the
#  crontab stays one line:
#
#    - expired sessions rows (auth's lazy purge only touches
#      the user who shows up; this sweeps the rest)
#    - push tokens whose owner has no live session left — a
#      phone that logged out and never came back must stop
#      receiving message previews
#    - scraper_runs rows a killed process left at 'running'
#      (older than every run budget, so a scrape genuinely
#      in flight is never touched)
#
#    - expired disappearing messages in rooms nobody
#      reopens (rooms sweep themselves on activity; this
#      backstop finds every room holding an overdue row and
#      runs the same sweep the routes run)
#
#  Each pass is guarded on its own — housekeeping never
#  fails housekeeping.
#
#  Run by the cron container:
#    docker exec knfapp-django python3 manage.py maintenance
############################################################


import logging

from django.core.management.base import BaseCommand

from knfapp.common.timestamps import utc_now_iso
from knfapp.notifications.push import prune_orphan_push_tokens
from knfapp.scraper.common import reconcile_interrupted_runs
from knfapp.users.models import Session

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sweep expired sessions, orphaned push tokens and abandoned scraper runs"

    def handle(self, *args, **options):
        try:
            swept, _ = Session.objects.filter(expires_at__lt=utc_now_iso()).delete()
            if swept:
                logger.info("Swept %d expired session row(s)", swept)
        except Exception:
            logger.exception("Expired-session sweep failed")

        try:
            prune_orphan_push_tokens()
        except Exception:
            logger.exception("Push-token prune failed")

        try:
            reconcile_interrupted_runs()
        except Exception:
            logger.exception("Scraper-run reconciliation failed")

        try:
            self._sweep_expired_messages()
        except Exception:
            logger.exception("Expiry sweep failed")

        self.stdout.write("Maintenance done")

    def _sweep_expired_messages(self):
        from django.db import connection

        from knfapp.chat.api.views import _now_naive, _sweep_expired

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT conversation_id FROM messages WHERE expires_at IS NOT NULL AND expires_at <= %s",
                (_now_naive(),),
            )
            rooms = [row[0] for row in cursor.fetchall()]
        for room_id in rooms:
            _sweep_expired(room_id)
        if rooms:
            logger.info("Expiry sweep cleared %d idle room(s)", len(rooms))
