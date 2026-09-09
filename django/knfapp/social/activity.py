############################################################
#  [*] Activity helpers — one row per gesture
#
#  record_activity upserts the "X liked / commented on your
#  post" row (self-gestures and authorless scraped posts
#  are dropped here, so callers never branch); the POST id
#  keys the row, so repeated gestures refresh one row to
#  the top carrying the newest excerpt. drop_activity is
#  the undo — an unlike takes its row back. Neither
#  commits: both ride the request's transaction
#  (ATOMIC_REQUESTS), so a failing route takes its
#  activity rows down with it.
#
#  Used by:
#    - news/api/views.py — toggle_like, add_comment
#    - social/api/views.py — the wall and handshake routes
############################################################


import uuid


from knfapp.common.timestamps import utc_now
from knfapp.social.models import Activity


def record_activity(user_id, kind, actor_id, subject_id=None, subject_preview=None):
    if not user_id or user_id == actor_id:
        return
    # The id rides create_defaults ONLY — a refresh must renew
    # the excerpt and the stamp, never re-mint the row's key
    # (the partial unique over NULL-subject rows would refuse
    # the phantom twin a re-keyed save produces)
    Activity.objects.update_or_create(
        user_id=user_id, kind=kind, actor_id=actor_id, subject_id=subject_id,
        create_defaults={
            "id": str(uuid.uuid4()),
            "subject_preview": subject_preview,
            "created_at": utc_now(),
            "read": 0,
        },
        defaults={
            "subject_preview": subject_preview,
            "created_at": utc_now(),
            "read": 0,
        },
    )


def drop_activity(user_id, kind, actor_id, subject_id=None):
    if not user_id:
        return
    Activity.objects.filter(user_id=user_id, kind=kind, actor_id=actor_id, subject_id=subject_id).delete()
