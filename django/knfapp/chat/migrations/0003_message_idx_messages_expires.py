############################################################
#  [*] chat 0003 — the disappearing-messages sweep index
#
#  Partial index over messages.expires_at (non-null rows
#  only): the sweeps ask for overdue rows across ALL
#  conversations, and without this that is a full scan of
#  the messages table. Partial, so only the expiring
#  minority is indexed and ordinary sends pay nothing.
############################################################

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0002_messages_fts'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='message',
            index=models.Index(condition=models.Q(('expires_at__isnull', False)), fields=['expires_at'], name='idx_messages_expires'),
        ),
    ]
