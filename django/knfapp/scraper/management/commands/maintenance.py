############################################################
#  [*] maintenance — the daily housekeeping tick
#
#  The rows nothing else ever deletes, in one command so
#  the crontab is one line:
#
#    - expired sessions rows (auth's lazy purge only touches
#      the user who shows up; this sweeps the rest)
#    - push tokens whose owner has no live session left — a
#      phone that logged out and never came back must stop
#      receiving message previews
#    - scraper_runs rows a killed process left at 'running'
#      (older than every run budget, so a scrape genuinely
#      in flight is never touched)
#    - expired disappearing messages in rooms nobody
#      reopens (rooms sweep themselves on activity; this
#      backstop finds every room holding an overdue row and
#      runs the same sweep the routes run)
#    - failed and abandoned wayfind captures past the grace
#      window, with their frame files (no retry path exists
#      through the wire, so the frames serve nothing)
#    - wayfind versions beyond the published one plus the
#      newest window (each row holds a full document — the
#      history has no consumer past the admin list)
#    - wayfind op-log rows past the replay horizon (the log
#      is only ever probed by exact id on a retry; a stale
#      replay past the horizon re-applies and its
#      baseRevision conflicts answer as a stale edit should)
#
#  Each pass is guarded on its own — housekeeping never
#  fails housekeeping.
#
#  Run by the cron container:
#    docker exec knfapp-django python3 manage.py maintenance
############################################################


import logging
import shutil
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from knfapp.common.timestamps import utc_now
from knfapp.notifications.push import prune_orphan_push_tokens
from knfapp.scraper.common import reconcile_interrupted_runs
from knfapp.users.models import Session

logger = logging.getLogger(__name__)

# The wayfind retention knobs: a month covers any realistic
# capture retry or offline outbox, twenty versions cover any
# realistic "what did we publish" question
CAPTURE_GRACE_DAYS = 30
WF_VERSION_KEEP = 20
WF_OPS_RETENTION_DAYS = 90


class Command(BaseCommand):
    help = "Sweep expired sessions, orphaned push tokens, abandoned scraper runs and stale wayfind rows"

    def handle(self, *args, **options):
        try:
            swept, _ = Session.objects.filter(expires_at__lt=utc_now()).delete()
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

        try:
            self._sweep_abandoned_captures()
        except Exception:
            logger.exception("Abandoned-capture sweep failed")

        try:
            self._prune_wayfind_versions()
        except Exception:
            logger.exception("Wayfind version prune failed")

        try:
            self._prune_wayfind_ops()
        except Exception:
            logger.exception("Wayfind op-log prune failed")

        self.stdout.write("Maintenance done")

    def _sweep_expired_messages(self):
        from knfapp.chat.api.views import _sweep_expired
        from knfapp.chat.models import Message

        rooms = list(
            Message.objects.filter(expires_at__isnull=False, expires_at__lte=utc_now())
            .values_list("conversation_id", flat=True)
            .distinct()
        )
        for room_id in rooms:
            _sweep_expired(room_id)
        if rooms:
            logger.info("Expiry sweep cleared %d idle room(s)", len(rooms))

    def _sweep_abandoned_captures(self):
        from knfapp.wayfind.api.captures import frames_dir
        from knfapp.wayfind.models import WfCapture, WfCaptureFrame

        # 'queued'/'stitching'/'done' are never touched — the
        # worker owns those; only the two states with no
        # forward path age out
        cutoff = datetime.now(timezone.utc) - timedelta(days=CAPTURE_GRACE_DAYS)
        stale = list(
            WfCapture.objects.filter(status__in=("failed", "uploading"), updated_at__lt=cutoff)
            .values_list("id", flat=True)
        )

        # Files first, then frames, then the capture row, so a
        # crash mid-pass re-enters cleanly — a re-run just
        # finds nothing left to unlink
        for capture_id in stale:
            shutil.rmtree(frames_dir(capture_id), ignore_errors=True)
            WfCaptureFrame.objects.filter(capture_id=capture_id).delete()
            WfCapture.objects.filter(id=capture_id).delete()
        if stale:
            logger.info("Swept %d abandoned capture(s)", len(stale))

    def _prune_wayfind_versions(self):
        from django.db import connection

        from knfapp.wayfind.models import WfBuilding

        pruned = 0
        buildings = list(WfBuilding.objects.values_list("id", "published_revision"))
        with connection.cursor() as cursor:
            for building_id, published_revision in buildings:
                # One statement per building, so the keep-set and
                # the delete share a snapshot. COALESCE(-1): a
                # NULL published_revision must protect nothing —
                # `!= NULL` would protect EVERYTHING and the
                # prune would silently no-op forever
                cursor.execute(
                    """DELETE FROM wf_versions
                       WHERE building_id = %s
                         AND revision != COALESCE(%s, -1)
                         AND revision NOT IN (SELECT revision FROM wf_versions
                                              WHERE building_id = %s
                                              ORDER BY revision DESC LIMIT %s)""",
                    (building_id, published_revision, building_id, WF_VERSION_KEEP),
                )
                pruned += cursor.rowcount
        if pruned:
            logger.info("Pruned %d old wayfind version(s)", pruned)

    def _prune_wayfind_ops(self):
        from knfapp.wayfind.models import WfOp

        # No index on purpose — a daily scan of a 90-day
        # table is cheaper than a fourth b-tree on every op
        cutoff = datetime.now(timezone.utc) - timedelta(days=WF_OPS_RETENTION_DAYS)
        pruned, _ = WfOp.objects.filter(created_at__lt=cutoff).delete()
        if pruned:
            logger.info("Pruned %d old wayfind op(s)", pruned)
