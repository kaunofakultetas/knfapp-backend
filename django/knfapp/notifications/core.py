############################################################
#  [*] notifications core — the channel list and the digest
#
#  The two facts the routes and the sender share:
#  VALID_CHANNELS is the one list of topic switches — the
#  routes validate against it and push.py honours it on
#  send; token_digest is the 8-hex handle log lines carry
#  instead of a raw push token.
#
#  Used by:
#    - api/views.py — the channel routes
#    - push.py — the sender
############################################################


import hashlib


VALID_CHANNELS = ("news", "chat", "schedule", "admin")


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()[:8]
