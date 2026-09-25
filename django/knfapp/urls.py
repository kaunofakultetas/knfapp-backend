############################################################
#  [*] URL routing — the /api/* surface, one file
#
#  Every route of the service lives here, grouped by app
#  with each group's imports next to its paths — the file
#  reads as a table of contents for the whole API, exactly
#  the paths swagger/swagger.yaml documents. Caddy proxies
#  /api/* to this container and the rest to the mobile web
#  build, which is why every path carries the api/ prefix
#  explicitly. No trailing slashes anywhere (APPEND_SLASH
#  is off).
#
#  Who may call what is not decided here — @require_auth /
#  @require_role on the views own that. WHICH VERB a path
#  takes is decided twice over: the trailing comments below
#  are the contract, every view wears @require_methods with
#  its verb, and each shared-path dispatcher names its verbs
#  explicitly and answers anything else with the JSON 405 —
#  a verb no branch claims never falls into a handler.
############################################################


from django.urls import path


from knfapp.common.http import method_not_allowed


urlpatterns = []








############################################################
# Auth — accounts and bearer sessions
############################################################
#
# Invitation-code validation and self-registration (the two
# public writes), login, the caller's profile (with the
# password-confirmed erasure and the GDPR export), and the
# two logout shapes.
#
# Views live in knfapp/users/api/.
############################################################

from knfapp.users.api.auth_views import (
    change_password, export_me, login, logout, logout_all, me, register, validate_code,
)

urlpatterns += [
    path("api/auth/validate-code", validate_code),     # POST — check a code without consuming it
    path("api/auth/register", register),               # POST — account + 30-day session in one transaction
    path("api/auth/login", login),                     # POST — password → fresh session token
    path("api/auth/me", me),                           # GET profile / PUT partial update / DELETE erasure
    path("api/auth/me/export", export_me),             # GET — everything stored about the caller, one JSON
    path("api/auth/change-password", change_password), # POST — rotate; every OTHER session dies
    path("api/auth/logout", logout),                   # POST — kill the presented session
    path("api/auth/logout-all", logout_all),           # POST — kill every session + push token
]








############################################################
# Uploads — stored files
############################################################
#
# One authenticated POST that stores a photo (re-encoded),
# document, video or voice note; a public GET serving the
# flat directory; an owner-or-admin DELETE. The GET is
# public on purpose — avatars and post images render for
# anonymous viewers too.
#
# Views live in knfapp/uploads/api/.
############################################################

from knfapp.uploads.api.views import delete_file, serve_file, upload_file


def _upload_dispatch(request, filename):
    # One path, two verbs — DELETE is owner-or-admin, GET public
    if request.method == "DELETE":
        return delete_file(request, filename)
    if request.method in ("GET", "HEAD"):
        return serve_file(request, filename)
    return method_not_allowed(["GET", "DELETE"])


urlpatterns += [
    path("api/uploads", upload_file),                  # POST — multipart {file, kind?} → the stored url
    path("api/uploads/<str:filename>", _upload_dispatch),  # GET serves / DELETE removes
]








############################################################
# News — the unified feed and everything hanging off it
############################################################
#
# The ranked feed (guests welcome), member posts, likes,
# shares, comment threads and the poll lifecycle. Reads are
# public where the mobile app reads logged-out; every
# per-post route answers the same 404 for missing and
# hidden. Method dispatch happens here — one path, its
# verbs named side by side.
#
# Views live in knfapp/news/api/.
############################################################

from knfapp.news.api.views import (
    add_comment,
    create_poll,
    create_post,
    delete_comment,
    delete_poll,
    delete_post,
    get_comments,
    get_feed,
    get_poll,
    get_post,
    share_post,
    toggle_like,
    vote_poll,
)


def _news_dispatch(request):
    if request.method == "POST":
        return create_post(request)
    if request.method in ("GET", "HEAD"):
        return get_feed(request)
    return method_not_allowed(["GET", "POST"])


def _news_post_dispatch(request, post_id):
    if request.method == "DELETE":
        return delete_post(request, post_id)
    if request.method in ("GET", "HEAD"):
        return get_post(request, post_id)
    return method_not_allowed(["GET", "DELETE"])


def _news_comments_dispatch(request, post_id):
    if request.method == "POST":
        return add_comment(request, post_id)
    if request.method in ("GET", "HEAD"):
        return get_comments(request, post_id)
    return method_not_allowed(["GET", "POST"])


def _news_poll_dispatch(request, post_id):
    if request.method == "POST":
        return create_poll(request, post_id)
    if request.method == "DELETE":
        return delete_poll(request, post_id)
    if request.method in ("GET", "HEAD"):
        return get_poll(request, post_id)
    return method_not_allowed(["GET", "POST", "DELETE"])


urlpatterns += [
    path("api/news", _news_dispatch),                                  # GET the ranked feed / POST a member or faculty post
    path("api/news/<str:post_id>", _news_post_dispatch),               # GET one post / DELETE author-or-admin
    path("api/news/<str:post_id>/like", toggle_like),                  # POST — flip the caller's like
    path("api/news/<str:post_id>/share", share_post),                  # POST — count a completed share (guests too)
    path("api/news/<str:post_id>/comments", _news_comments_dispatch),  # GET the thread / POST a comment
    path("api/news/<str:post_id>/comments/<str:comment_id>", delete_comment),  # DELETE — author/owner/admin
    path("api/news/<str:post_id>/poll", _news_poll_dispatch),          # GET / POST attach / DELETE detach
    path("api/news/<str:post_id>/poll/vote", vote_poll),               # POST — cast or move the caller's vote
]








############################################################
# Social — profiles, friendships, walls, blocks, activity
############################################################
#
# The community feed and the profile reads are public (a
# guest gets the public-only view); everything that writes
# sits behind the auth decorators in the views. Wall posts
# live in news_posts — likes/comments/polls stay under
# /api/news.
#
# Views live in knfapp/social/api/.
############################################################

from knfapp.social.api.views import (
    accept_friend_request,
    activity_unread_count,
    block_user,
    create_post as create_wall_post,
    create_report,
    delete_post as delete_wall_post,
    get_own_profile,
    get_profile,
    get_user_posts,
    list_activity,
    list_blocks,
    list_friend_requests,
    list_friends,
    mark_activity_read,
    reject_friend_request,
    send_friend_request,
    social_feed,
    unblock_user,
    unfriend,
    update_post as update_wall_post,
    update_profile,
)


def _social_profile_dispatch(request):
    if request.method == "PUT":
        return update_profile(request)
    if request.method in ("GET", "HEAD"):
        return get_own_profile(request)
    return method_not_allowed(["GET", "PUT"])


def _social_posts_dispatch(request):
    if request.method == "POST":
        return create_wall_post(request)
    if request.method in ("GET", "HEAD"):
        return get_user_posts(request)
    return method_not_allowed(["GET", "POST"])


def _social_post_dispatch(request, post_id):
    if request.method == "PUT":
        return update_wall_post(request, post_id)
    if request.method == "DELETE":
        return delete_wall_post(request, post_id)
    return method_not_allowed(["PUT", "DELETE"])


def _social_blocks_dispatch(request):
    if request.method == "POST":
        return block_user(request)
    if request.method in ("GET", "HEAD"):
        return list_blocks(request)
    return method_not_allowed(["GET", "POST"])


urlpatterns += [
    path("api/social/feed", social_feed),                                        # GET  — the community feed (guests welcome)
    path("api/social/profile/<str:user_id>", get_profile),                       # GET  — anyone's public profile
    path("api/social/profile", _social_profile_dispatch),                        # GET own / PUT edit own
    path("api/social/friends/request", send_friend_request),                     # POST — send (or auto-accept) a request
    path("api/social/friends/requests", list_friend_requests),                   # GET  — pending, ?direction=sent|received
    path("api/social/friends/requests/<str:request_id>/accept", accept_friend_request),  # POST — recipient only
    path("api/social/friends/requests/<str:request_id>/reject", reject_friend_request),  # POST — decline or cancel
    path("api/social/friends", list_friends),                                    # GET  — the friends list
    path("api/social/friends/<str:user_id>", unfriend),                          # DELETE — both directions
    path("api/social/posts", _social_posts_dispatch),                            # GET ?user_id / POST a wall post
    path("api/social/posts/<str:post_id>", _social_post_dispatch),               # PUT edit own / DELETE own
    path("api/social/blocks", _social_blocks_dispatch),                          # GET the list / POST a block
    path("api/social/blocks/<str:user_id>", unblock_user),                       # DELETE — idempotent unblock
    path("api/social/reports", create_report),                                   # POST — the complaint ledger
    path("api/social/activity", list_activity),                                  # GET  — keyset-paged notifications
    path("api/social/activity/read", mark_activity_read),                        # POST — flip every unread row
    path("api/social/activity/unread", activity_unread_count),                   # GET  — the badge count
]








############################################################
# Notifications — push tokens and channel switches
############################################################
#
# The client half of Expo push: register/drop a device
# token, flip the four topic switches, and the chat-preview
# privacy flag. Delivery lives in the sender module
# (knfapp/notifications/push.py).
#
# Views live in knfapp/notifications/api/.
############################################################

from knfapp.notifications.api.views import (
    get_channels,
    get_chat_preview,
    register_token,
    unregister_token,
    update_channels,
    update_chat_preview,
)


def _push_register_dispatch(request):
    if request.method == "POST":
        return register_token(request)
    if request.method == "DELETE":
        return unregister_token(request)
    return method_not_allowed(["POST", "DELETE"])


def _channels_dispatch(request):
    if request.method == "PUT":
        return update_channels(request)
    if request.method in ("GET", "HEAD"):
        return get_channels(request)
    return method_not_allowed(["GET", "PUT"])


def _chat_preview_dispatch(request):
    if request.method == "PUT":
        return update_chat_preview(request)
    if request.method in ("GET", "HEAD"):
        return get_chat_preview(request)
    return method_not_allowed(["GET", "PUT"])


urlpatterns += [
    path("api/notifications/register", _push_register_dispatch),   # POST add-or-reactivate / DELETE own row
    path("api/notifications/channels", _channels_dispatch),        # GET the four switches / PUT a partial flip
    path("api/notifications/chat-preview", _chat_preview_dispatch),  # GET / PUT the privacy flag
]








############################################################
# Schedule / Info / Memes — the small public-read groups
############################################################
#
# The timetable and the handbook are the app's works-
# without-an-account screens (public, ETag-cached); the
# meme library is signed-in except its public file serve.
############################################################

from knfapp.schedule.api.views import get_schedule, get_schedule_events, get_schedule_filters
from knfapp.info.api.views import get_faculty_info
from knfapp.memes.api.views import delete_meme, list_memes, push_meme, serve_meme


def _memes_dispatch(request):
    if request.method == "POST":
        return push_meme(request)
    if request.method in ("GET", "HEAD"):
        return list_memes(request)
    return method_not_allowed(["GET", "POST"])


urlpatterns += [
    path("api/schedule", get_schedule),                    # GET — the folded weekly page (legacy)
    path("api/schedule/events", get_schedule_events),      # GET — dated events in a range
    path("api/schedule/filters", get_schedule_filters),    # GET — groups + semesters + days
    path("api/info", get_faculty_info),                    # GET — the bilingual handbook
    path("api/memes", _memes_dispatch),                    # GET the library / POST a push
    path("api/memes/<str:meme_id>", delete_meme),          # DELETE — pusher or admin
    path("api/memes/file/<str:name>", serve_meme),         # GET — the bytes, public
]








############################################################
# Admin — the console behind the app's admin screens
############################################################
#
# Invitation codes (curators scoped to their own mintable-
# role ones), the user directory with the continuity-guarded
# role/active editor and the GDPR erasure, the dashboard
# counters, the broadcast job pair, the complaint queue, and
# the oversight reads the web panel runs on: the audit
# trail, the stored-file ledger, the reported-message
# window, and the scraper skip-list with its restore. Every
# route is role-gated in the views; every mutation writes an
# admin_audit row.
############################################################

from knfapp.admin.api.views import (
    admin_stats,
    broadcast_job_status,
    create_invitation,
    delete_invitation,
    delete_user,
    get_reported_message,
    list_audit,
    list_invitations,
    list_reports,
    list_tombstones,
    list_uploads,
    list_users,
    resolve_report,
    restore_tombstone,
    send_admin_notification,
    update_user,
)


def _admin_invitations_dispatch(request):
    if request.method == "POST":
        return create_invitation(request)
    if request.method in ("GET", "HEAD"):
        return list_invitations(request)
    return method_not_allowed(["GET", "POST"])


def _admin_user_dispatch(request, user_id):
    if request.method == "PATCH":
        return update_user(request, user_id)
    if request.method == "DELETE":
        return delete_user(request, user_id)
    return method_not_allowed(["PATCH", "DELETE"])


urlpatterns += [
    path("api/admin/invitations", _admin_invitations_dispatch),        # GET the scoped list / POST a mint
    path("api/admin/invitations/<str:code_id>", delete_invitation),    # DELETE — revoke, curator-scoped
    path("api/admin/users", list_users),                               # GET — the whole directory
    path("api/admin/users/<str:user_id>", _admin_user_dispatch),       # PATCH role/active / DELETE erasure
    path("api/admin/stats", admin_stats),                              # GET — five cached dashboard counters
    path("api/admin/notifications", send_admin_notification),          # POST — 202 + broadcast job id
    path("api/admin/notifications/<str:job_id>", broadcast_job_status),# GET — the job record, or 404
    path("api/admin/reports", list_reports),                           # GET — the complaint queue
    path("api/admin/reports/<str:report_id>", resolve_report),         # PUT — open <-> resolved
    path("api/admin/audit", list_audit),                               # GET — the audit trail, admin-only
    path("api/admin/uploads", list_uploads),                           # GET — the stored-file ledger
    path("api/admin/messages/<str:message_id>", get_reported_message), # GET — a reported message, any room
    path("api/admin/tombstones", list_tombstones),                     # GET — the scraper skip-list
    path("api/admin/tombstones/restore", restore_tombstone),           # POST — lift one tombstone
]








############################################################
# Scraper — run history and manual triggers
############################################################
#
# Admin-only Swagger/curl surface over the four scrapers the
# cron container otherwise runs. The triggers scrape
# SYNCHRONOUSLY (tens of seconds to minutes) and are
# non-atomic — the scrapers draw their own transaction
# boundaries so /status can watch a run while it goes.
############################################################

from knfapp.scraper.api.views import (
    scraper_status,
    trigger_info_scrape,
    trigger_schedule_scrape,
    trigger_scrape,
)

urlpatterns += [
    path("api/scraper/status", scraper_status),            # GET — last 20 runs + per-source summary
    path("api/scraper/trigger", trigger_scrape),           # POST — knf (2 pages) + vu (1 page), no push
    path("api/scraper/run", trigger_scrape),               # POST — alias of /trigger
    path("api/scraper/schedule", trigger_schedule_scrape), # POST — the full timetable import
    path("api/scraper/info", trigger_info_scrape),         # POST — contacts/programs/structure
]








############################################################
# Chat — conversations, messages, reactions, presence
############################################################
#
# The messaging REST surface; live delivery rides the
# socket.io layer knfapp/wsgi.py wraps around this app
# (chat/socket.py). Every view is non-atomic — the chat
# handlers draw their own commit boundaries so the socket
# fan-out fires strictly after them.
############################################################

from knfapp.chat.api.views import (
    create_conversation,
    delete_message,
    edit_message,
    get_changes,
    get_messages,
    get_pins,
    leave_conversation,
    list_conversations,
    mark_read,
    online_status,
    pin_message,
    react_to_message,
    remove_reaction,
    search_messages,
    search_users,
    send_message,
    set_message_ttl,
    toggle_pin,
    total_unread_count,
)


# The non-atomic mark must sit on the callback the resolver
# hands Django — a dispatcher without it would silently wrap
# the chat writes back into one request transaction
from django.db import transaction as _tx


@_tx.non_atomic_requests
def _chat_conversations_dispatch(request):
    if request.method == "POST":
        return create_conversation(request)
    if request.method in ("GET", "HEAD"):
        return list_conversations(request)
    return method_not_allowed(["GET", "POST"])


@_tx.non_atomic_requests
def _chat_messages_dispatch(request, conv_id):
    if request.method == "POST":
        return send_message(request, conv_id)
    if request.method in ("GET", "HEAD"):
        return get_messages(request, conv_id)
    return method_not_allowed(["GET", "POST"])


@_tx.non_atomic_requests
def _chat_message_dispatch(request, conv_id, msg_id):
    if request.method == "PUT":
        return edit_message(request, conv_id, msg_id)
    if request.method == "DELETE":
        return delete_message(request, conv_id, msg_id)
    return method_not_allowed(["PUT", "DELETE"])


@_tx.non_atomic_requests
def _chat_react_dispatch(request, conv_id, msg_id):
    if request.method == "POST":
        return react_to_message(request, conv_id, msg_id)
    if request.method == "DELETE":
        return remove_reaction(request, conv_id, msg_id)
    return method_not_allowed(["POST", "DELETE"])


urlpatterns += [
    path("api/chat/conversations", _chat_conversations_dispatch),               # GET the tab / POST create-or-reuse
    path("api/chat/conversations/<str:conv_id>", leave_conversation),           # DELETE — leave (purge when last)
    path("api/chat/conversations/<str:conv_id>/messages", _chat_messages_dispatch),  # GET a page / POST a send
    path("api/chat/conversations/<str:conv_id>/changes", get_changes),          # GET — edits/unsends since a cursor
    path("api/chat/conversations/<str:conv_id>/messages/search", search_messages),  # GET — in-room search
    path("api/chat/conversations/<str:conv_id>/messages/<str:msg_id>", _chat_message_dispatch),  # PUT edit / DELETE unsend
    path("api/chat/conversations/<str:conv_id>/messages/<str:msg_id>/pin", pin_message),  # PUT pin / DELETE unpin
    path("api/chat/conversations/<str:conv_id>/messages/<str:msg_id>/react", _chat_react_dispatch),  # POST set / DELETE clear
    path("api/chat/conversations/<str:conv_id>/pins", get_pins),                # GET — the pinned banner
    path("api/chat/conversations/<str:conv_id>/ttl", set_message_ttl),          # PUT — disappearing messages
    path("api/chat/conversations/<str:conv_id>/pin", toggle_pin),               # PUT — the caller's own list pin
    path("api/chat/conversations/<str:conv_id>/read", mark_read),               # PUT — both read stores + receipts
    path("api/chat/unread-count", total_unread_count),                          # GET — the tab badge total
    path("api/chat/online-status", online_status),                              # POST — presence, relationship-gated
    path("api/chat/users/search", search_users),                                # GET — the people picker
]








############################################################
# Wayfind — the indoor map
############################################################
#
# The published building graph is public (the app routes
# without login); drafts, ops, publishes, uploads and the
# guided-capture intake are editor-only (@require_role in
# the views). The stored panoramas/plans are content-
# addressed and served immutable.
############################################################

from knfapp.wayfind.api.views import (
    create_building,
    get_draft,
    get_graph,
    list_buildings,
    list_versions,
    post_ops,
    publish_building,
    serve_panorama,
    serve_plan,
    upload_panorama,
    upload_plan,
)
from knfapp.wayfind.api.captures import (
    create_capture,
    finish_capture,
    get_capture,
    upload_capture_frame,
)


def _wayfind_buildings_dispatch(request):
    if request.method == "POST":
        return create_building(request)
    if request.method in ("GET", "HEAD"):
        return list_buildings(request)
    return method_not_allowed(["GET", "POST"])


urlpatterns += [
    path("api/wayfind/buildings", _wayfind_buildings_dispatch),                     # GET the list / POST create (admin)
    path("api/wayfind/buildings/<str:building_id>/graph", get_graph),               # GET — the published document, ETag
    path("api/wayfind/buildings/<str:building_id>/draft", get_draft),               # GET — whole draft or ?since delta
    path("api/wayfind/buildings/<str:building_id>/ops", post_ops),                  # POST — one op batch
    path("api/wayfind/buildings/<str:building_id>/publish", publish_building),      # POST — validate + snapshot
    path("api/wayfind/buildings/<str:building_id>/versions", list_versions),        # GET — the publish history
    path("api/wayfind/buildings/<str:building_id>/panoramas", upload_panorama),     # POST — content-addressed store
    path("api/wayfind/buildings/<str:building_id>/plans", upload_plan),             # POST — sanitised SVG store
    path("api/wayfind/panoramas/<str:name>", serve_panorama),                       # GET — immutable, public
    path("api/wayfind/plans/<str:name>", serve_plan),                               # GET — immutable, public
    path("api/wayfind/buildings/<str:building_id>/captures", create_capture),       # POST — open a capture session
    path("api/wayfind/captures/<str:capture_id>/frames/<str:target_id>", upload_capture_frame),  # PUT — one frame + pose
    path("api/wayfind/captures/<str:capture_id>/finish", finish_capture),           # POST — queue the stitch
    path("api/wayfind/captures/<str:capture_id>", get_capture),                     # GET — status / report / pano
]








############################################################
# Ops — the readiness probe
############################################################
#
# The one unauthenticated operational route: database
# answering, uploads writable. Described in swagger and
# meant for a manual curl after a deploy (nothing polls it).
#
# View lives in knfapp/ops/api/.
############################################################

from knfapp.ops.api.views import health

urlpatterns += [
    path("api/health", health),                            # GET — 200 ok / 503 with the first reason
]








############################################################
# Assistant — the AI container's internal door
############################################################
#
# ISOLATED-NETWORK ONLY: Caddy proxies /api/* and never
# /internal/*, and every view additionally demands the
# compose-injected X-Internal-Secret header. The assistant
# container is the sole caller — retrieval for its
# searchHandbook tool, the stored conversations, and the
# per-turn telemetry. Not part of the public wire, so not
# in swagger.
#
# Views live in knfapp/assistant/api/.
############################################################

from knfapp.assistant.api.admin_views import (
    activate_prompt, assistant_overview, create_curated, create_prompt, deactivate_prompt,
    delete_curated, knowledge_search, list_curated, list_prompts, reindex_knowledge,
    review_thread, review_threads, update_curated,
)
from knfapp.assistant.api.internal_views import (
    active_prompt, assistant_search, message_feedback, thread_delete,
    thread_messages, threads_claim, threads_create, threads_list,
    threads_lookup, turn_log,
)


def _admin_prompts_dispatch(request):
    # One path, two verbs — POST saves a new version, GET lists
    if request.method == "POST":
        return create_prompt(request)
    if request.method in ("GET", "HEAD"):
        return list_prompts(request)
    return method_not_allowed(["GET", "POST"])


def _admin_curated_dispatch(request):
    # Same shape for the curated answers — POST adds, GET lists
    if request.method == "POST":
        return create_curated(request)
    if request.method in ("GET", "HEAD"):
        return list_curated(request)
    return method_not_allowed(["GET", "POST"])

urlpatterns += [
    path("internal/assistant/search", assistant_search),                            # POST — pgvector top-k for searchHandbook
    path("internal/assistant/prompt", active_prompt),                               # GET — the active prompt appendix
    path("internal/assistant/threads", threads_create),                             # POST — mint a thread (uuid is the guest credential)
    path("internal/assistant/threads/list", threads_list),                          # GET — the signed-in list, newest first
    path("internal/assistant/threads/lookup", threads_lookup),                      # POST — guest bulk fetch by device-held ids
    path("internal/assistant/threads/claim", threads_claim),                        # POST — login adopts the device's guest threads
    path("internal/assistant/threads/<uuid:thread_id>/messages", thread_messages),  # GET transcript / POST turn upsert
    path("internal/assistant/threads/<uuid:thread_id>/feedback", message_feedback),  # POST — thumbs verdict on one message
    path("internal/assistant/threads/<uuid:thread_id>/delete", thread_delete),      # POST — soft delete
    path("internal/assistant/turn-log", turn_log),                                  # POST — one telemetry row per turn
    path("api/admin/assistant/prompts", _admin_prompts_dispatch),                   # GET history / POST a new version (admin)
    path("api/admin/assistant/prompts/deactivate", deactivate_prompt),              # POST — core prompt alone (admin)
    path("api/admin/assistant/prompts/<int:version>/activate", activate_prompt),    # POST — switch the live version (admin)
    path("api/admin/assistant/overview", assistant_overview),                       # GET — KB, turns, ratings, active prompt (admin)
    path("api/admin/assistant/knowledge/reindex", reindex_knowledge),               # POST — cron's sync on demand (admin, audited)
    path("api/admin/assistant/knowledge/search", knowledge_search),                 # POST — retrieval test box (admin)
    path("api/admin/assistant/knowledge/curated", _admin_curated_dispatch),         # GET list / POST add a hand-written answer (admin)
    path("api/admin/assistant/knowledge/curated/<uuid:entry_id>/update", update_curated),  # POST — re-embed one answer (admin)
    path("api/admin/assistant/knowledge/curated/<uuid:entry_id>/delete", delete_curated),  # POST — drop one answer (admin)
    path("api/admin/assistant/threads", review_threads),                            # GET — review list, ?rating=down (admin)
    path("api/admin/assistant/threads/<uuid:thread_id>", review_thread),            # GET — one transcript (admin, audited)
]








############################################################
# Error handlers — the envelope for what no view answered
############################################################
#
# Django's four module-level names. An unknown path, a
# PermissionDenied, a request Django itself refuses (the
# oversized body among them, answered 413 by handler400)
# and an exception no view caught all answer {"error",
# "code"} like every view does, never an HTML page.
# DEBUG=True still shows the technical pages (see the
# views' banner). Named AFTER urlpatterns so the table
# above stays the table of contents.
#
# Views live in knfapp/common/http.py.
############################################################

handler400 = "knfapp.common.http.handler400"
handler403 = "knfapp.common.http.handler403"
handler404 = "knfapp.common.http.handler404"
handler500 = "knfapp.common.http.handler500"
