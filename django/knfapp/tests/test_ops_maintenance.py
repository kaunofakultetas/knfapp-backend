############################################################
#  [*] Regression tests — the ops commands and wayfind sweeps
#
#  grant_role is the ONE path to the first admin on an empty
#  database (registration mints students, privileged codes
#  need an admin) — it grants case-insensitively, refuses
#  the unknown, and leaves an audit row. The maintenance
#  command's wayfind passes bound the three unbounded
#  stores: abandoned captures (files and rows, past the
#  grace window, never the worker's states), the version
#  history (published + newest window survive), and the op
#  log (past the replay horizon).
############################################################


import io
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone


from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings


from knfapp.admin.models import AdminAudit
from knfapp.common.timestamps import utc_now_iso
from knfapp.users.models import User
from knfapp.wayfind.models import WfBuilding, WfCapture, WfCaptureFrame, WfOp, WfVersion
from .utils import create_user


def _aware_days_ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class GrantRoleTests(TestCase):

    def test_the_first_admin_is_minted_case_insensitively_and_audited(self):
        user = create_user(username="Tomas")
        out = io.StringIO()
        call_command("grant_role", "tomas", "admin", stdout=out)

        self.assertEqual(User.objects.get(id=user.id).role, "admin")
        trail = AdminAudit.objects.get(action="user.role")
        self.assertIsNone(trail.actor_id)   # nobody is signed in at first boot
        self.assertEqual(trail.target, user.id)
        # payload is a JSON column — the dict grant_role wrote
        # comes back as-is
        self.assertEqual(trail.payload["via"], "grant_role")

    def test_unknown_names_and_roles_are_refused(self):
        create_user(username="tomas")
        with self.assertRaises(CommandError):
            call_command("grant_role", "nera-tokio", "admin")
        self.assertEqual(User.objects.get().role, "student")


class WayfindSweepTests(TestCase):

    def setUp(self):
        now = utc_now_iso()
        self.building = WfBuilding.objects.create(id="knf", name="KNF", draft_revision=30,
                                                  created_at=now, updated_at=now)

    def _capture(self, capture_id, status, days_old):
        stamp = _aware_days_ago(days_old)
        capture = WfCapture.objects.create(
            id=f"knf:{capture_id}", building_id="knf", mode="full", frame_hfov_deg=60.0,
            targets=[], expected=8, status=status, created_at=stamp, updated_at=stamp,
        )
        WfCaptureFrame.objects.create(capture_id=capture.id, target_id="t1", yaw_deg=0.0,
                                      pitch_deg=0.0, roll_deg=0.0, bytes=1, width=1, height=1,
                                      updated_at=stamp)
        return capture

    def test_abandoned_captures_sweep_out_with_their_files_after_the_grace_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(UPLOAD_DIR=tmp):
                from knfapp.wayfind.api.captures import frames_dir

                stale = self._capture("senas", "failed", days_old=40)
                fresh = self._capture("naujas", "uploading", days_old=1)
                working = self._capture("dirba", "stitching", days_old=40)

                os.makedirs(frames_dir(stale.id), exist_ok=True)
                open(os.path.join(frames_dir(stale.id), "t1.jpg"), "wb").write(b"x")

                call_command("maintenance", stdout=io.StringIO())

                # The stale one is gone — row, frames, files;
                # the fresh one and the worker's are untouched
                self.assertFalse(WfCapture.objects.filter(id=stale.id).exists())
                self.assertFalse(WfCaptureFrame.objects.filter(capture_id=stale.id).exists())
                self.assertFalse(os.path.exists(frames_dir(stale.id)))
                self.assertTrue(WfCapture.objects.filter(id=fresh.id).exists())
                self.assertTrue(WfCapture.objects.filter(id=working.id).exists())

    def test_version_history_keeps_the_published_row_and_the_newest_window(self):
        for revision in range(1, 31):
            WfVersion.objects.create(building_id="knf", revision=revision, document="{}",
                                     etag=f"e{revision}", published_at=utc_now_iso())
        # The published pointer sits OUTSIDE the newest-20
        # window on purpose — it must survive anyway
        WfBuilding.objects.filter(id="knf").update(published_revision=3)

        call_command("maintenance", stdout=io.StringIO())

        kept = set(WfVersion.objects.filter(building_id="knf").values_list("revision", flat=True))
        self.assertIn(3, kept)                        # the served document
        self.assertEqual(kept - {3}, set(range(11, 31)))   # the newest 20

    def test_old_ops_prune_and_the_replay_window_survives(self):
        for days_old, op_id in ((120, "senas"), (5, "naujas")):
            WfOp.objects.create(id=f"knf:{op_id}", building_id="knf", op="{}",
                                status="applied", created_at=_aware_days_ago(days_old))

        call_command("maintenance", stdout=io.StringIO())

        remaining = list(WfOp.objects.values_list("id", flat=True))
        self.assertEqual(remaining, ["knf:naujas"])








############################################################
# OrphanUploadMaintenanceTests
############################################################
#
# KNF-118 through the daily tick: the sweep only COUNTS by
# default — a student's file is deleted only once the owner
# has read the dry-run counts and passes the flag.
############################################################

class OrphanUploadMaintenanceTests(TestCase):

    def setUp(self):
        import shutil
        import tempfile
        from datetime import timedelta

        from knfapp.common.timestamps import utc_now
        from knfapp.uploads import storage
        from knfapp.uploads.models import Upload
        from .utils import create_user, register_upload

        self.storage = storage
        self.tmp = tempfile.mkdtemp(prefix="knfapp-maint-")
        storage._upload_dir = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: setattr(storage, "_upload_dir", None))
        owner = create_user()
        self.orphan = register_upload(self.tmp, owner, blob=b"x" * 400)
        Upload.objects.filter(filename=self.orphan).update(created_at=utc_now() - timedelta(days=9))

    def test_the_default_run_counts_and_deletes_nothing(self):
        from knfapp.uploads.models import Upload

        out = io.StringIO()
        call_command("maintenance", stdout=out)
        self.assertIn("Would remove (dry run) 1 orphan upload(s), 400 bytes", out.getvalue())
        self.assertTrue(Upload.objects.filter(filename=self.orphan).exists())

    def test_the_flag_deletes_the_orphan(self):
        from knfapp.uploads.models import Upload

        out = io.StringIO()
        call_command("maintenance", "--delete-orphan-uploads", stdout=out)
        self.assertIn("Removed 1 orphan upload(s), 400 bytes", out.getvalue())
        self.assertFalse(Upload.objects.filter(filename=self.orphan).exists())
