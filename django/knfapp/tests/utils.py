############################################################
#  [*] Test helpers — accounts, codes and signed-in clients
#
#  The few builders every suite shares. Passwords default
#  to a known value so login flows can exercise the real
#  bcrypt path; tests that only need a row skip the hash
#  cost with an unusable marker.
############################################################


import uuid
from datetime import datetime, timedelta, timezone


import bcrypt


from knfapp.common.timestamps import utc_now_iso
from knfapp.users.models import InvitationCode, User


PASSWORD = "labai-slapta-2026"

# One bcrypt hash for every test account — hashing once keeps the
# suite fast, and the password path stays real where it matters
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


def create_user(username="tomas", email=None, role="student", active=1, password_hash=None):
    now = utc_now_iso()
    return User.objects.create(
        id=str(uuid.uuid4()),
        username=username,
        email=email or f"{username.lower()}@knf.vu.lt",
        display_name=username.capitalize(),
        password_hash=PASSWORD_HASH if password_hash is None else password_hash,
        role=role,
        active=active,
        created_at=now,
        updated_at=now,
    )


def create_invite(code="KVIETIMAS1", role="teacher", max_uses=1, use_count=0, expires_in_days=7,
                  expires_at=None, created_by=None):
    return InvitationCode.objects.create(
        id=str(uuid.uuid4()),
        code=code,
        role=role,
        max_uses=max_uses,
        use_count=use_count,
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat(),
        created_at=utc_now_iso(),
        created_by=created_by,
    )


def bearer(client_method, path, token, **kwargs):
    return client_method(path, HTTP_AUTHORIZATION=f"Bearer {token}", **kwargs)


def create_post(author=None, source=None, is_public=1, post_type=None, title="Naujiena", **overrides):
    from knfapp.news.models import NewsPost
    now = utc_now_iso()
    fields = dict(
        id=str(uuid.uuid4()),
        title=title,
        content=overrides.pop("content", "Turinys apie fakultetą."),
        summary="Turinys apie fakultetą."[:200],
        author_id=author.id if author else None,
        author_name=author.display_name if author else None,
        source=source or ("user" if author else "knf.vu.lt"),
        post_type=post_type or ("social" if author else "article"),
        is_public=is_public,
        published_at=overrides.pop("published_at", now),
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    return NewsPost.objects.create(**fields)


def befriend(a, b):
    from knfapp.social.models import Friendship
    # Both directions, as social's accept writes them
    Friendship.objects.create(user=a, friend=b, created_at=utc_now_iso())
    Friendship.objects.create(user=b, friend=a, created_at=utc_now_iso())


def naive_now(minutes_ago=0):
    # Chat stamps are naive-UTC datetimes — the exact kind the
    # views bind and the encoder turns into the naive wire shape
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(tzinfo=None)


def create_room(members, conv_type="direct", title=None, message_ttl_seconds=None):
    from knfapp.chat.models import Conversation, ConversationParticipant
    now = naive_now()
    conv = Conversation.objects.create(
        id=str(uuid.uuid4()), type=conv_type, title=title,
        message_ttl_seconds=message_ttl_seconds,
        created_by=members[0], created_at=now, updated_at=now,
    )
    for member in members:
        ConversationParticipant.objects.create(
            conversation=conv, user=member, pinned=0, last_read_at=None, joined_at=now,
        )
    return conv


def create_message(conv, sender, text="Labas", minutes_ago=0, **overrides):
    from knfapp.chat.models import Message
    fields = dict(
        id=str(uuid.uuid4()), conversation=conv, sender=sender, text=text,
        kind="text", forwarded=0, created_at=naive_now(minutes_ago),
    )
    fields.update(overrides)
    return Message.objects.create(**fields)
