############################################################
#  [*] Regression tests — the error envelope and the
#      control characters the body parser strips
#
#  Two guarantees below the views. (1) Every string in a
#  JSON body reaches the view without NUL or another C0
#  control character: PostgreSQL's text type refuses NUL
#  with a DataError — a 500 from a body no human typed —
#  and SQLite stores it and answers wrong later. (2) What
#  no view answered still speaks JSON: an unknown /api/...
#  path, a PermissionDenied, a request Django itself
#  refuses (the oversized body answered 413 among them)
#  and an exception no view caught all answer the
#  documented {"error", "code"} envelope, never Django's
#  HTML page — and a wrong verb on a news write answers the
#  app's own 405 with Allow, not Django's empty one. The
#  handlers are exercised through the REAL urls.py names
#  (this module re-exports them beside two raising routes).
############################################################


import json


from django.core.exceptions import PermissionDenied, RequestDataTooBig, TooManyFieldsSent
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import path


from knfapp import urls as knfapp_urls
from knfapp.common import ratelimit
from knfapp.common.http import clean_param, get_json_object, handler400 as _handler400


def _boom(request):
    raise RuntimeError("uncaught on purpose")


def _forbidden(request):
    raise PermissionDenied("closed on purpose")


# The production urlconf plus two raising routes, under the
# SAME four handlers urls.py names — an unmatched path, a
# PermissionDenied and an uncaught exception then exercise
# the real wiring
urlpatterns = [path("api/_test/boom", _boom), path("api/_test/forbidden", _forbidden)] + knfapp_urls.urlpatterns
handler400 = knfapp_urls.handler400
handler403 = knfapp_urls.handler403
handler404 = knfapp_urls.handler404
handler500 = knfapp_urls.handler500


class BodyControlCharacterTests(TestCase):

    def _post(self, payload):
        return RequestFactory().post("/api/x", data=json.dumps(payload),
                                     content_type="application/json")

    def test_nul_and_the_c0_controls_are_stripped_from_every_string(self):
        body = get_json_object(self._post({
            "text": "La\x00bas\x07",
            "nested": {"list": ["a\x00", {"deep": "b\x1f"}], "n": 1},
            "keep": "line\nbreak\ttab\r",
        }))
        self.assertEqual(body["text"], "Labas")
        self.assertEqual(body["nested"], {"list": ["a", {"deep": "b"}], "n": 1})
        # The three text controls survive — a message body may carry them
        self.assertEqual(body["keep"], "line\nbreak\ttab\r")

    def test_keys_are_cleaned_too_and_non_objects_stay_none(self):
        self.assertEqual(get_json_object(self._post({"na\x00me": "x"})), {"name": "x"})
        self.assertIsNone(get_json_object(self._post(["a\x00"])))

    def test_clean_param_strips_a_query_value_and_passes_none(self):
        self.assertEqual(clean_param("a\x00b\x01c\td"), "abc\td")
        self.assertIsNone(clean_param(None))


@override_settings(ROOT_URLCONF="knfapp.tests.test_error_envelope")
class ErrorEnvelopeTests(TestCase):

    def setUp(self):
        self.client = Client()
        # The 500 case: hand back the handler's response instead
        # of re-raising the view's exception into the test
        self.client.raise_request_exception = False

    def test_an_unknown_api_path_answers_the_json_envelope(self):
        response = self.client.get("/api/no/such/route")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"error": "Not found", "code": "not_found"})

    def test_an_uncaught_exception_answers_the_json_envelope(self):
        response = self.client.get("/api/_test/boom")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"error": "Internal server error", "code": "server_error"})

    @override_settings(ALLOWED_HOSTS=["knfapp.test"])
    def test_a_request_django_itself_refuses_answers_the_json_envelope(self):
        response = self.client.get("/api/health", HTTP_HOST="evil.example")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"error": "Bad request", "code": "bad_request"})

    def test_a_permission_denied_answers_the_json_envelope(self):
        response = self.client.get("/api/_test/forbidden")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"error": "Forbidden", "code": "forbidden"})

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=100, DEBUG=False)
    def test_an_oversized_body_answers_413_too_large(self):
        # validate-code is the first public route that reads the
        # body, before any auth — request.body raises
        # RequestDataTooBig, Django hands it to handler400, and
        # the handler's 413 is what reaches the wire
        ratelimit.reset()
        response = self.client.post("/api/auth/validate-code", data=json.dumps({"code": "x" * 200}),
                                    content_type="application/json")
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"error": "Request body too large", "code": "too_large"})

    def test_handler400_tells_the_oversized_body_from_the_other_suspicious_operations(self):
        request = RequestFactory().post("/api/x")
        self.assertEqual(_handler400(request, exception=RequestDataTooBig()).status_code, 413)
        self.assertEqual(_handler400(request, exception=TooManyFieldsSent()).status_code, 400)
        self.assertEqual(_handler400(request).status_code, 400)

    def test_a_wrong_verb_on_a_news_write_answers_the_json_405_with_allow(self):
        # Django's bare @require_POST answered an EMPTY text/html
        # 405; the project's guard answers the envelope and lists
        # the verb, before auth is even looked at
        response = self.client.get("/api/news/nera/like")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json(), {"error": "Method not allowed", "code": "method_not_allowed"})

    def test_the_users_and_uploads_writes_answer_the_same_json_405(self):
        # The last routes to wear Django's bare @require_POST —
        # a GET on login or on the upload sink now answers the
        # same envelope and Allow as every other write
        for route in ("/api/auth/login", "/api/auth/register", "/api/uploads"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response["Allow"], "POST")
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertEqual(response.json()["code"], "method_not_allowed")
