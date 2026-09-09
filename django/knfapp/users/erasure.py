############################################################
#  [*] Account erasure — the one GDPR routine
#
#  Shared by the self-service DELETE /api/auth/me and the
#  admin console's DELETE /api/admin/users/<id>: the
#  person's uploads leave the disk, the counters their
#  engagement fed are decremented while the rows still
#  exist, everything that is theirs alone is hard-deleted
#  (their sessions, devices, likes, votes, blocks,
#  handshakes AND their activity feed — plus the actor-side
#  activity rows advertising gestures this routine just
#  removed), their authored snapshots are tombstoned, and
#  the users row survives ANONYMISED (uuid-embedding
#  placeholders, so the UNIQUE constraints cannot collide;
#  an unreachable bcrypt hash; active = 0). Runs inside the
#  caller's transaction (ATOMIC_REQUESTS).
#
#  The chat-side steps run as guarded raw SQL — a database
#  without the chat tables (a stripped-down deployment)
#  skips them with one log line instead of failing the
#  whole erasure. They cover: every dead media reference in
#  the person's messages (image, attachment columns,
#  gallery, link card — the files left the disk in STEP 1),
#  the display name frozen into system narrations ("X
#  sukūrė grupę" becomes the tombstone name — names are
#  otherwise always joined live, these strings are the one
#  stored copy), their read/reaction rows and memberships —
#  and finally a purge of any conversation the departure
#  left with ZERO participants, mirroring the last-leaver
#  purge in the chat views: a room nobody can ever open
#  again must not keep its message history.
############################################################


import logging
import uuid


import bcrypt
from django.db import connection
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest


from knfapp.common.timestamps import utc_now
from knfapp.news.models import NewsLike, NewsPost, PollOption, PollVote
from knfapp.notifications.models import NotificationChannel, PushToken
from knfapp.social.models import Activity, FriendRequest, Friendship, UserBlock
from knfapp.uploads.models import Upload
from knfapp.uploads.storage import delete_upload
from knfapp.users.models import Session, User


logger = logging.getLogger(__name__)

ERASED_USER_MARKER = "Ištrintas naudotojas"


# Every conversation nobody is left in — the predicate the
# four orphan-purge statements below share
_ORPHAN_ROOMS = """SELECT c.id FROM conversations c
                   WHERE NOT EXISTS (SELECT 1 FROM conversation_participants p
                                     WHERE p.conversation_id = c.id)"""


def _chat_side_erasure(user_id, display_name):
    # Each statement carries ITS OWN parameters — the media
    # scrub is user-scoped, the orphan purges take none
    statements = [
        # Own upload references — the files left the disk in
        # STEP 1; the LIKE keeps /api/memes/file/ pictures
        (("UPDATE messages SET image_url = NULL"
          " WHERE sender_id = %s AND image_url LIKE '%%/api/uploads/%%'"), (user_id,)),

        # The richer media columns the unsend path clears —
        # attachment_meta rides in the guard too, plain photo
        # messages carry their preview data-URI there alone
        (("UPDATE messages SET attachment_url = NULL, attachment_name = NULL,"
          " attachment_size = NULL, attachment_mime = NULL, attachment_meta = NULL,"
          " gallery = NULL, link_preview = NULL"
          " WHERE sender_id = %s AND (attachment_url IS NOT NULL OR attachment_meta IS NOT NULL"
          " OR gallery IS NOT NULL OR link_preview IS NOT NULL)"), (user_id,)),

        # System narrations open with the actor's display name
        # verbatim ("X sukūrė grupę ...") — swap the frozen
        # prefix for the tombstone. Exact-prefix match on
        # purpose: LIKE would trip over % or _ in a name
        (("UPDATE messages SET text = %s || substr(text, length(%s) + 1)"
          " WHERE sender_id = %s AND kind = 'system'"
          " AND substr(text, 1, length(%s) + 1) = %s || ' '"),
         (ERASED_USER_MARKER, display_name, user_id, display_name, display_name)),

        ("DELETE FROM message_reads WHERE user_id = %s", (user_id,)),
        ("DELETE FROM message_reactions WHERE user_id = %s", (user_id,)),

        # The erased user's direct rooms stop claiming their
        # pair BEFORE the membership rows go (the subquery
        # still needs them) — the counterpart's recreate must
        # insert fresh, never 200 onto the abandoned room
        (("UPDATE conversations SET direct_key = NULL"
          " WHERE id IN (SELECT conversation_id FROM conversation_participants"
          "              WHERE user_id = %s)"), (user_id,)),

        ("DELETE FROM conversation_participants WHERE user_id = %s", (user_id,)),

        # Rooms the departure emptied — children first, the
        # same order the last-leaver purge in the views uses
        (f"DELETE FROM message_reads WHERE message_id IN (SELECT id FROM messages WHERE conversation_id IN ({_ORPHAN_ROOMS}))", ()),
        (f"DELETE FROM message_reactions WHERE message_id IN (SELECT id FROM messages WHERE conversation_id IN ({_ORPHAN_ROOMS}))", ()),
        (f"DELETE FROM messages WHERE conversation_id IN ({_ORPHAN_ROOMS})", ()),
        (f"DELETE FROM conversations WHERE id IN ({_ORPHAN_ROOMS})", ()),
    ]

    for sql, params in statements:
        # A missing display name (cannot happen for a live
        # account, but the routine must never crash) skips the
        # narration rewrite alone
        if display_name is None and ERASED_USER_MARKER in params:
            continue
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
        except Exception:
            logger.info("Erasure: chat-side statement skipped (table absent)")
            return


def erase_user_account(user_id):
    # The display name must be read BEFORE the anonymise —
    # the chat narration rewrite matches on it
    display_name = User.objects.filter(id=user_id).values_list("display_name", flat=True).first()


    # STEP 1: files first — the uploads off the disk, own
    # references nulled, the ownership rows dropped
    # ===================================================
    for filename in Upload.objects.filter(user_id=user_id).values_list("filename", flat=True):
        try:
            delete_upload(f"/api/uploads/{filename}")
        except Exception:
            logger.exception("Erasure: upload %s not deleted", filename)

    NewsPost.objects.filter(author_id=user_id, image_url__contains="/api/uploads/").update(image_url=None)
    _chat_side_erasure(user_id, display_name)
    Upload.objects.filter(user_id=user_id).delete()


    # STEP 2: the denormalised counters their engagement fed,
    # decremented while the rows still exist to be counted —
    # Greatest floors an already-drifted counter at 0
    # =======================================================
    NewsPost.objects.filter(
        id__in=NewsLike.objects.filter(user_id=user_id).values("post_id"),
    ).update(likes_count=Greatest(F("likes_count") - 1, Value(0)))
    PollOption.objects.filter(
        id__in=PollVote.objects.filter(user_id=user_id).values("option_id"),
    ).update(votes=Greatest(F("votes") - 1, Value(0)))


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
    # Their own feed is theirs alone; the actor-side rows
    # advertise gestures the deletes above just removed
    Activity.objects.filter(Q(user_id=user_id) | Q(actor_id=user_id)).delete()


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
        active=0, updated_at=utc_now(),
    )
