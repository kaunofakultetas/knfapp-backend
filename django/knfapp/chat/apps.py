############################################################
#  [*] chat — app registration
#
#  Conversations, messages, reactions, receipts — the REST
#  half in api/, the socket.io half in socket.py/events.py
#  (wired around the WSGI app, threading mode, polling
#  transport — see socket.py).
############################################################


from django.apps import AppConfig


class ChatConfig(AppConfig):
    name = "knfapp.chat"
    label = "chat"
