############################################################
#  [*] Regression tests — invitation-code lifecycle
#
#  The canonical rejection order (unknown → exhausted →
#  expired), the aware/naive/malformed expiry reads, and
#  the atomic burn's rowcount guard — the piece that keeps
#  two registrations from sharing the last use.
############################################################


from datetime import datetime, timedelta, timezone


from django.db import models
from django.test import TestCase


from knfapp.users.api.auth_views import _invite_rejection
from knfapp.users.models import InvitationCode
from .utils import create_invite


class InviteRejectionTests(TestCase):

    def test_unknown_beats_everything(self):
        self.assertEqual(_invite_rejection(None)[2], "unknown")

    def test_exhausted_beats_expired(self):
        invite = create_invite(max_uses=1, use_count=1,
                               expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        self.assertEqual(_invite_rejection(invite)[2], "exhausted")

    def test_naive_expiry_reads_as_utc_and_malformed_as_expired(self):
        live_naive = create_invite(code="A", expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).replace(tzinfo=None).isoformat())
        self.assertIsNone(_invite_rejection(live_naive))
        broken = create_invite(code="B", expires_at="rytoj")
        self.assertEqual(_invite_rejection(broken)[2], "expired")


class AtomicBurnTests(TestCase):

    def test_the_conditional_update_cannot_overshoot_max_uses(self):
        create_invite(code="RIBA", max_uses=2)
        burn = lambda: InvitationCode.objects.filter(
            code="RIBA", use_count__lt=models.F("max_uses"),
        ).update(use_count=models.F("use_count") + 1)
        self.assertEqual(burn(), 1)
        self.assertEqual(burn(), 1)
        # The racer that lost gets rowcount 0 — never a third use
        self.assertEqual(burn(), 0)
        self.assertEqual(InvitationCode.objects.get(code="RIBA").use_count, 2)
