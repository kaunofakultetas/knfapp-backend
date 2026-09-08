############################################################
#  [*] Account erasure — the one GDPR routine
#
#  Shared by the self-service DELETE /api/auth/me and the
#  admin console's DELETE /api/admin/users/<id>: the
#  person's uploads leave the disk, the counters their
#  engagement fed are decremented while the rows still
#  exist, everything that is theirs alone is hard-deleted,
#  their authored snapshots are tombstoned, and the users
#  row survives ANONYMISED (uuid-embedding placeholders, so
#  the UNIQUE constraints cannot collide; an unreachable
#  bcrypt hash; active = 0). Runs inside the caller's
#  transaction (ATOMIC_REQUESTS).
#
#  The chat-side steps (messages' image references, read/
#  reaction rows, conversation memberships) run as guarded
#  raw SQL — a database without the chat tables (a
#  stripped-down deployment) skips them with one log line
#  instead of failing the whole erasure.
############################################################


import logging
import uuid


import bcrypt
from django.db import connection
from django.db.models import Q


from knfapp.common.timestamps import utc_now_iso
from knfapp.news.models import NewsLike, NewsPost, PollOption, PollVote
from knfapp.notifications.models import NotificationChannel, PushToken
from knfapp.social.models import FriendRequest, Friendship, UserBlock
from knfapp.uploads.models import Upload
from knfapp.uploads.storage import delete_upload
from knfapp.users.models import Session, User


logger = logging.getLogger(__name__)

ERASED_USER_MARKER = "Ištrintas naudotojas"

# The chat-side statements, applied best-effort — see the
# banner
_CHAT_SQL = (
    ("UPDATE messages SET image_url = NULL"
     " WHERE sender_id = %s AND image_url LIKE '%%/api/uploads/%%'"),
    "DELETE FROM message_reads WHERE user_id = %s",
    "DELETE FROM message_reactions WHERE user_id = %s",
    "DELETE FROM conversation_participants WHERE user_id = %s",
)


def _chat_side_erasure(user_id):
    for sql in _CHAT_SQL:
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, (user_id,))
        except Exception:
            logger.info("Erasure: chat-side statement skipped (table absent)")
            return


def erase_user_account(user_id):
    # STEP 1: files first — the uploads off the disk, own
    # references nulled, the ownership rows dropped
    # ===================================================
    for filename in Upload.objects.filter(user_id=user_id).values_list("filename", flat=True):
        try:
            delete_upload(f"/api/uploads/{filename}")
        except Exception:
            logger.exception("Erasure: upload %s not deleted", filename)

    NewsPost.objects.filter(author_id=user_id, image_url__contains="/api/uploads/").update(image_url=None)
    _chat_side_erasure(user_id)
    Upload.objects.filter(user_id=user_id).delete()


    # STEP 2: the denormalised counters their engagement fed,
    # decremented while the rows still exist to be counted
    # =======================================================
    with connection.cursor() as cursor:
        cursor.execute(
            """UPDATE news_posts SET likes_count = MAX(0, likes_count - 1)
               WHERE id IN (SELECT post_id FROM news_likes WHERE user_id = %s)""",
            (user_id,),
        )
        cursor.execute(
            """UPDATE poll_options SET votes = MAX(0, votes - 1)
               WHERE id IN (SELECT option_id FROM poll_votes WHERE user_id = %s)""",
            (user_id,),
        )


    # STEP 3: everything that is theirs alone, hard-deleted
    # =====================================================
    Session.objects.filter(user_id=user_id).delete()
    PushToken.objects.filter(user_id=user_id).delete()
    NotificationChannel.objects.filter(user_id=user_id).delete()
    NewsLike.objects.filter(user_id=user_id).delete()
    PollVote.objects.filter(user_id=user_id).delete()
    Friendship.objects.filter(Q(user_id=user_id) | Q(friend_id=user_id)).delete()
    FriendRequest.objects.filter(Q(from_user_id=user_id) | Q(to_user_id=user_id)).delete()
    UserBlock.objects.filter(Q(blocker_id=user_id) | Q(blocked_id=user_id)).delete()


    # STEP 4: tombstone the authored snapshots, anonymise the
    # row — the placeholders embed the uuid, so UNIQUE cannot
    # collide, and the fresh random hash is unreachable
    # =======================================================
    NewsPost.objects.filter(author_id=user_id).update(author_name=ERASED_USER_MARKER)
    unreachable_hash = bcrypt.hashpw(uuid.uuid4().hex.encode(), bcrypt.gensalt()).decode()
    User.objects.filter(id=user_id).update(
        username=f"deleted-{user_id}",
        email=f"deleted-{user_id}@deleted.invalid",
        display_name=ERASED_USER_MARKER,
        password_hash=unreachable_hash,
        avatar_url=None, student_number=None, study_group=None, study_program=None,
        active=0, updated_at=utc_now_iso(),
    )
