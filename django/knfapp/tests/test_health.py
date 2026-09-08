############################################################
#  [*] Regression tests — the readiness probe
#
#  GET /api/health is the one swagger-documented route with
#  no auth: the ok answer keeps its two published keys plus
#  the additive per-check fields, and a probe that cannot
#  write the upload directory answers 503 with the reason —
#  the whole point of the route is refusing to say "ok"
#  through a read-only mount.
############################################################


import tempfile


from django.test import Client, TestCase, override_settings


class HealthProbeTests(TestCase):

    @override_settings(UPLOAD_DIR=tempfile.gettempdir())
    def test_a_healthy_service_answers_ok_with_both_checks(self):
        answer = Client().get("/api/health")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json(), {
            "status": "ok",
            "service": "knfapp-backend",
            "database": "ok",
            "uploads": "ok",
        })

    @override_settings(UPLOAD_DIR="/nera/tokio/katalogo")
    def test_an_unwritable_upload_dir_is_a_503_with_the_reason(self):
        answer = Client().get("/api/health")
        self.assertEqual(answer.status_code, 503)

        body = answer.json()
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["uploads"], "error")
        self.assertEqual(body["database"], "ok")
        self.assertIn("/nera/tokio/katalogo", body["reason"])
