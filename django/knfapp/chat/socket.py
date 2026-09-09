############################################################
#  [*] Chat socket — the Socket.IO server instance
#
#  ONE python-socketio Server in threading mode, wrapped
#  around the Django WSGI app by knfapp/wsgi.py: one
#  process, polling transport, an engineio packet thread
#  per connection. Threading mode over ASGI on purpose —
#  the handlers stay synchronous, and polling is the only
#  transport the mobile client uses (it never upgrades to
#  websocket).
#
#  Consequence for serving: gunicorn must run ONE worker
#  (gthread) — presence, rooms and the socket rate limiter
#  are in-process state. The Dockerfile's CMD says so.
#
#  async_handlers=False serialises handler dispatch per
#  connection, which is what makes the per-event rate
#  limiter real back-pressure instead of a thread-spawn
#  bypass (see events.py). CORS is wide open at this layer
#  on purpose — Caddy fronts the service and the REST side
#  trusts bearer tokens, not origins.
#
#  Importing this module never starts a server: emitting on
#  a Server with no clients is a no-op, which is what lets
#  the REST views (and the tests) import the emit path
#  unconditionally.
############################################################


import json
from datetime import date, datetime, timezone

import socketio


from knfapp.chat.events import register_socket_events


# Chat's socket wire is NAIVE UTC like its REST twin: this
# shim strips an aware stamp's offset before serialising,
# so handlers carry ONE stamp kind (aware) and the wire
# keeps its frozen no-offset shape
class _StampJson:
    @staticmethod
    def dumps(obj, **kwargs):
        kwargs.pop("default", None)
        return json.dumps(obj, default=_json_default, **kwargs)

    loads = staticmethod(json.loads)


def _json_default(o):
    if isinstance(o, datetime):
        if o.tzinfo is not None:
            o = o.astimezone(timezone.utc).replace(tzinfo=None)
        return o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


sio = socketio.Server(
    async_mode="threading",
    cors_allowed_origins="*",
    async_handlers=False,
    json=_StampJson,
)

register_socket_events(sio)
