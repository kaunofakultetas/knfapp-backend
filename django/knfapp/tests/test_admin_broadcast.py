############################################################
#  [*] Regression tests — the admin broadcast job
#
#  The 202-and-poll contract: the queued record the POST
#  hands out, the forced type marker no custom payload may
#  drop, the byte bound Expo would refuse anyway, the
#  fan-out finishing even with nobody to send to, and the
#  registry's eviction discipline — a job the LRU dropped
#  mid-flight must stay forgotten, not come back as a
#  half-built record.
############################################################


import json


from django.test import Client, TestCase


from knfapp.admin.api import views
from knfapp.admin.models import AdminAudit
from knfapp.common import ratelimit
from knfapp.users import auth
from .utils import bearer, create_user


class BroadcastTests(TestCase):

    def setUp(self):
        ratelimit.reset()
        views._broadcast_jobs.clear()
        self.addCleanup(views._broadcast_jobs.clear)

        # The seam: no real thread in a test — capture the spawn
        self.spawned = []
        self._real_spawn = views._spawn_broadcast
        views._spawn_broadcast = lambda *args: self.spawned.append(args)
        self.addCleanup(lambda: setattr(views, "_spawn_broadcast", self._real_spawn))

        self.admin = create_user(username="vadovas", role="admin")
        self.token = auth.mint_session(self.admin.id)
        self.client = Client()

    def _post(self, **body):
        with self.captureOnCommitCallbacks(execute=True):
            return bearer(self.client.post, "/api/admin/notifications", self.token,
                          data=json.dumps(body), content_type="application/json")

    def test_the_202_hands_out_a_queued_record_and_audits(self):
        response = self._post(title="Dėmesio", body="Rytoj nebus paskaitų")
        self.assertEqual(response.status_code, 202)
        job = json.loads(response.content)
        self.assertEqual(job["status"], "queued")
        self.assertEqual((job["sent"], job["failed"]), (0, 0))

        status = bearer(self.client.get, f"/api/admin/notifications/{job['jobId']}", self.token)
        self.assertEqual(json.loads(status.content)["title"], "Dėmesio")
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(AdminAudit.objects.filter(action="notification.broadcast", target=job["jobId"]).exists())

    def test_the_type_marker_survives_a_custom_payload(self):
        self._post(title="Dėmesio", body="Tekstas", data={"type": "phishing", "deeplink": "/news"})
        extra = self.spawned[0][3]
        self.assertEqual(extra["type"], "admin_announcement")
        self.assertEqual(extra["deeplink"], "/news")

    def test_the_bounds_reject_before_anything_is_registered(self):
        cases = (
            {"title": "", "body": "x"},
            {"title": "x" * 201, "body": "x"},
            {"title": "x", "body": "x" * 1001},
            {"title": "x", "body": "x", "data": ["ne", "objektas"]},
            {"title": "x", "body": "x", "data": {"blob": "x" * 4000}},
            {"title": 5, "body": "x"},
        )
        for body in cases:
            self.assertEqual(self._post(**body).status_code, 400, body)
        self.assertEqual(len(self.spawned), 0)
        self.assertEqual(len(views._broadcast_jobs), 0)

    def test_the_fanout_with_no_devices_lands_done_not_stranded(self):
        # No push_tokens rows: notify_channel targets nothing, Expo
        # is never contacted, and the job still finishes — "done"
        # with zero devices, never a job stranded in "running"
        job = json.loads(self._post(title="Dėmesio", body="Tekstas").content)
        views._run_broadcast(job["jobId"], "Dėmesio", "Tekstas", {"type": "admin_announcement"})
        record = views._broadcast_job(job["jobId"])
        self.assertEqual(record["status"], "done")
        self.assertEqual((record["sent"], record["distinctUsers"]), (0, 0))
        self.assertIsNotNone(record["finishedAt"])

    def test_an_evicted_job_is_never_resurrected(self):
        views._set_broadcast_job("senas", status="running", title="Pirmas")
        for i in range(views.BROADCAST_JOBS_MAX):
            views._set_broadcast_job(f"naujas-{i}", status="queued")
        self.assertIsNone(views._broadcast_job("senas"))
        # The finishing write of the evicted fan-out updates nothing
        self.assertIsNone(views._update_broadcast_job("senas", status="done"))
        self.assertIsNone(views._broadcast_job("senas"))

    def test_fanout_counts_reads_every_sender_shape(self):
        self.assertEqual(views._fanout_counts(7), (7, 0))
        self.assertEqual(views._fanout_counts((5, 2)), (5, 2))
        self.assertEqual(views._fanout_counts({"sent": 3, "failed": 1}), (3, 1))
        self.assertEqual(views._fanout_counts(None), (0, 0))

    def test_an_unknown_job_is_a_404(self):
        response = bearer(self.client.get, "/api/admin/notifications/nezinomas", self.token)
        self.assertEqual(response.status_code, 404)
