############################################################
#  [*] Profile patch — the one PUT-profile routine
#
#  PUT /api/auth/me and PUT /api/social/profile are the
#  same operation reached from two screens. This module is
#  the single copy both views call, so the rules cannot
#  drift again (they had: one deferred the avatar cleanup
#  to the commit, one unlinked inline; one was rate-limited,
#  one was not; the avatar check in both took any
#  /api/uploads/ path — somebody else's included).
#
#    apply_profile_patch(request, data)
#        → (patch, None) | (None, error response)
#      Validates the body and collects the columns to write.
#    commit_profile_patch(request, patch)
#        → (row, None) | (None, error response)
#      Writes them, propagates the rename, arms the replaced
#      avatar's cleanup and re-reads the row.
#
#  patch is a plain dict:
#    {"updates":         {column: value},
#     "display_name":    str | None  — set when renamed,
#     "replaced_avatar": path | None — the old own-upload
#                        avatar the write drops}
#
#  Both views answer the row through users/auth.py
#  serialize_user — the same key set from either path.
############################################################


import logging


from django.db import transaction


from knfapp.common.http import json_error
from knfapp.common.timestamps import utc_now
from knfapp.uploads.storage import delete_upload, owns_upload
from knfapp.users.models import User


logger = logging.getLogger(__name__)

# The three student-card fields: (camelCase key, snake key, column)
_STUDENT_FIELDS = (
    ("studentNumber", "student_number", "student_number"),
    ("studyGroup", "study_group", "study_group"),
    ("studyProgram", "study_program", "study_program"),
)








############################################################
# apply_profile_patch
############################################################
#
# The validation half. camelCase keys win when both
# spellings arrive; a key that is absent is skipped, so a
# partial body only touches what it names.
#
#   - display name: present-but-blank 400, over 100 chars
#     400; the stripped value goes in and is remembered for
#     the author_name propagation
#   - avatar: null and "" both clear. A value must be a
#     relative /api/uploads/ path (a foreign host would
#     beacon every avatar render to whoever the user picked)
#     AND either one of the caller's own registered uploads
#     (storage.owns_upload) or the avatar they already have,
#     sent back unchanged — a client that PUTs its whole
#     profile back must not 400 on a no-op, and Upload.user
#     is SET_NULL, so an old avatar may have no row left to
#     own. Anything else is 400 upload_not_owned: filenames
#     are public, and the commit below would otherwise ask
#     the sink for somebody else's file. The avatar being
#     dropped is remembered for cleanup only when it is an
#     /api/uploads/ path
#   - student-card fields: strings ≤50 after strip; explicit
#     null and a blank string both store NULL; the 400 names
#     the key the client sent
#
# No field at all is a 400. Answers (patch, None) or
# (None, response).
#
# Used by:
#   - api/auth_views.py update_me — PUT /api/auth/me
#   - social/api/views.py update_profile — PUT
#     /api/social/profile
############################################################

def apply_profile_patch(request, data):
    # STEP 1: display name
    # ====================
    updates = {}
    new_display_name = None
    replaced_avatar = None

    dn_key = "displayName" if "displayName" in data else "display_name"
    if dn_key in data:
        if not isinstance(data[dn_key], str):
            return None, json_error("display_name must be a string", 400)
        display_name = data[dn_key].strip()
        if not display_name:
            return None, json_error("Display name cannot be empty", 400)
        if len(display_name) > 100:
            return None, json_error("Display name must be at most 100 characters", 400)
        updates["display_name"] = display_name
        new_display_name = display_name


    # STEP 2: avatar — the path shape, then its owner
    # ===============================================
    av_key = "avatarUrl" if "avatarUrl" in data else "avatar_url"
    if av_key in data:
        av = data[av_key]
        if av not in (None, "") and (not isinstance(av, str) or not av.startswith("/api/uploads/")):
            return None, json_error("avatar_url must be a relative /api/uploads/ path", 400)
        old_avatar = request.user.get("avatar_url")
        if av and av != old_avatar and not owns_upload(request.user["id"], av):
            return None, json_error("avatar_url must be one of your own uploads", 400, code="upload_not_owned")
        updates["avatar_url"] = av
        if old_avatar and old_avatar != av and old_avatar.startswith("/api/uploads/"):
            replaced_avatar = old_avatar


    # STEP 3: the student-card fields
    # ===============================
    for camel, snake, column in _STUDENT_FIELDS:
        field = camel if camel in data else snake
        if field in data:
            val = data[field]
            if val is not None:
                if not isinstance(val, str):
                    return None, json_error(f"{field} must be a string", 400)
                val = val.strip()
                if len(val) > 50:
                    return None, json_error(f"{field} must be at most 50 characters", 400)
                if not val:
                    val = None
            updates[column] = val

    if not updates:
        return None, json_error("No fields to update", 400)

    return {"updates": updates, "display_name": new_display_name, "replaced_avatar": replaced_avatar}, None








############################################################
# commit_profile_patch
############################################################
#
# The write half: one UPDATE from the whitelisted columns
# with updated_at stamped, the author_name snapshots on
# the user's posts rewritten in the same transaction (posts
# never show a half-renamed author — the news app owns
# that table, so the import is guarded), the replaced
# avatar handed to the uploads sink ON THE COMMIT as the
# profile owner's (the sink refuses a file they never owned
# and one another record still shows — best-effort, never
# an error here), then the re-read row. The cleanup is
# registered BEFORE the re-read's 401 exit: the commit
# drops the file's last reference either way. The 401 is
# the row vanishing between the auth check and the re-read
# — the session-dead answer the client already handles.
# Answers (row, None) or (None, response); the row is the
# .values() dict serialize_user takes.
#
# Used by:
#   - api/auth_views.py update_me — PUT /api/auth/me
#   - social/api/views.py update_profile — PUT
#     /api/social/profile
############################################################

def commit_profile_patch(request, patch):
    user_id = request.user["id"]

    updates = dict(patch["updates"], updated_at=utc_now())
    User.objects.filter(id=user_id).update(**updates)
    if patch["display_name"]:
        _propagate_display_name(user_id, patch["display_name"])

    replaced_avatar = patch["replaced_avatar"]
    if replaced_avatar:
        transaction.on_commit(lambda: delete_upload(replaced_avatar, user_id))

    row = User.objects.filter(id=user_id).values().first()
    if row is None:
        return None, json_error("Authentication required", 401)
    return row, None


def _propagate_display_name(user_id, display_name):
    try:
        from knfapp.news.models import NewsPost
    except ImportError:
        return
    NewsPost.objects.filter(author_id=user_id).update(author_name=display_name)
