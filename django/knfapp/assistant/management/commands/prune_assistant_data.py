############################################################
#  [*] prune_assistant_data — retention, on cron's clock
#
#  The assistant's stored conversations are personal data
#  with a shelf life, and this command is the whole
#  retention policy in one place:
#
#    - soft-deleted threads (the GUI's swipe) hard-delete
#      after a short grace window, messages cascading
#    - untouched threads retire RETENTION_DAYS after their
#      last message — a support chat is not an archive
#    - assistant_turns telemetry prunes after TURN_DAYS
#
#    python3 manage.py prune_assistant_data
############################################################


from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from knfapp.assistant.models import AssistantThread, AssistantTurn


# Days a soft-deleted thread lingers before the hard prune
# — long enough to notice an accidental swipe, no more
GRACE_DAYS = 7

# Days of silence after which a thread retires outright
RETENTION_DAYS = 180

# Days of per-turn telemetry kept for usage stats
TURN_DAYS = 90


class Command(BaseCommand):
    help = "Prune deleted/stale assistant threads and old turn telemetry"

    def handle(self, *args, **options):
        now = timezone.now()

        deleted, _ = (AssistantThread.objects
                      .filter(deleted_at__lt=now - timedelta(days=GRACE_DAYS))
                      .delete())
        retired, _ = (AssistantThread.objects
                      .filter(last_message_at__lt=now - timedelta(days=RETENTION_DAYS))
                      .delete())
        turns, _ = (AssistantTurn.objects
                    .filter(created_at__lt=now - timedelta(days=TURN_DAYS))
                    .delete())

        self.stdout.write(
            f"pruned {deleted} swiped threads, {retired} stale threads, {turns} old turns"
        )
