############################################################
#  [*] Smoke test — the WSGI entry point loads
#
#  knfapp/wsgi.py is what gunicorn serves, and nothing in
#  the suite ever imported it: the Socket.IO wrapping and
#  the stitch worker's start ran for the first time at a
#  deploy. Importing it at module scope builds the Django
#  app, wraps it in socketio.WSGIApp around chat/socket.py's
#  Server and calls start_stitch_worker() — a REAL daemon
#  thread plus a bookkeeping UPDATE — so that call is
#  patched out here and the import is asserted to reach it.
############################################################


import importlib
import sys
from io import BytesIO
from unittest.mock import patch
from wsgiref.util import setup_testing_defaults


import socketio
from django.core.signals import request_finished, request_started
from django.db import close_old_connections
from django.test import TestCase


from knfapp.chat import socket as socket_layer


class WsgiSmokeTests(TestCase):

    def _import_fresh(self):
        # A fresh import every time — a previous test in the same
        # process may have cached the module — with the worker
        # start intercepted (wsgi.py binds it by `from` import,
        # so the patch must be in place BEFORE the import)
        sys.modules.pop("knfapp.wsgi", None)
        self.addCleanup(sys.modules.pop, "knfapp.wsgi", None)
        with patch("knfapp.wayfind.stitch.start_stitch_worker", return_value=True) as started:
            wsgi = importlib.import_module("knfapp.wsgi")
        return wsgi, started

    def test_the_module_wraps_django_in_socketio_and_starts_the_stitch_worker(self):
        wsgi, started = self._import_fresh()

        started.assert_called_once_with()
        self.assertIsInstance(wsgi.application, socketio.WSGIApp)
        # The one Server socket.py built, with Django underneath
        # and the polling wire on the path Caddy proxies
        self.assertIs(wsgi.application.engineio_app, socket_layer.sio)
        self.assertIs(wsgi.application.wsgi_app, wsgi.django_application)
        self.assertEqual(wsgi.application.engineio_path, "/socket.io/")

    def test_a_rest_path_falls_through_to_django(self):
        wsgi, _ = self._import_fresh()

        environ = {}
        setup_testing_defaults(environ)
        environ.update(PATH_INFO="/api/schedule/filters", REQUEST_METHOD="GET",
                       HTTP_HOST="127.0.0.1", **{"wsgi.input": BytesIO(b"")})
        seen = {}

        def start_response(status, headers, exc_info=None):
            seen["status"] = status
            seen["headers"] = dict(headers)

        # Django's test Client does exactly this around its own
        # call: the request_started / request_finished receivers
        # recycle the connection, which under this test's
        # transaction is the very close chat/events.py guards
        # against — on PostgreSQL the view would answer 500
        request_started.disconnect(close_old_connections)
        request_finished.disconnect(close_old_connections)
        try:
            response = wsgi.application(environ, start_response)
            body = b"".join(response)
            response.close()
        finally:
            request_started.connect(close_old_connections)
            request_finished.connect(close_old_connections)
        self.assertEqual(seen["status"], "200 OK")
        self.assertIn("application/json", seen["headers"]["Content-Type"])
        self.assertIn(b"semesters", body)
