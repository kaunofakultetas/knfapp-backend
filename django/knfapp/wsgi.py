############################################################
#  [*] WSGI entry point — what gunicorn serves
#
#  The Django app wrapped in the Socket.IO layer: every
#  /socket.io/* request goes to the chat transport
#  (chat/socket.py — threading mode, polling only),
#  everything else falls through to Django. Caddy proxies both prefixes to this
#  one container, so the mobile app's wire protocol is
#  byte-identical.
#
#  Presence, rooms and the socket rate limiter are
#  in-process state — gunicorn must run ONE worker (gthread;
#  the Dockerfile CMD says so). A DJANGO_DEBUG runserver
#  serves REST only: manage.py does not route through this
#  module, so live chat events need the gunicorn path.
############################################################


import os

import socketio

from django.core.wsgi import get_wsgi_application


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "knfapp.settings")

django_application = get_wsgi_application()

# Imported AFTER the Django app is built: socket.py pulls in
# chat/events.py, whose auth import needs the app registry
from knfapp.chat.socket import sio  # noqa: E402

application = socketio.WSGIApp(sio, django_application, socketio_path="socket.io")

# The capture-to-panorama worker rides the serving process —
# a management command or bare shell never spawns it (see
# wayfind/stitch.py; WAYFIND_STITCH_ENABLED=0 turns it off)
from knfapp.wayfind.stitch import start_stitch_worker  # noqa: E402

start_stitch_worker()
