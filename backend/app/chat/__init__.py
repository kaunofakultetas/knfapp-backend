############################################################
#  [*] Chat — package marker for messaging
#
#  Two modules live here:
#
#    routes.py  — the REST surface: conversations, the
#                 history page (with participants and the
#                 conversation row), send with an optional
#                 replyToId, unsend, reactions, pin, read,
#                 unread count, message search, presence and
#                 the people picker
#    events.py  — the Socket.IO side: connect-time room
#                 joins, typing, mark-read, and the
#                 emit_* helpers routes.py calls to push
#                 new_message / reaction_update /
#                 message_deleted / messages_read
#
#  chat_bp is NOT re-exported here — create_app() imports it
#  from app.chat.routes directly.
#
#  Used by:
#    - app/__init__.py — registers chat_bp at /api/chat and
#      calls register_socket_events(socketio)
#    - mobile services/api/chat.ts, services/socket.ts and
#      the chatkit-based room screen
############################################################
