############################################################
#  [*] chat 0001 — the five messaging tables
#
#  The whole chat schema in one initial migration: the
#  composite PKs, the idempotency UNIQUE, the kind CHECK,
#  the loose reply_to reference (db_constraint=False — the
#  TTL sweep deletes quoted rows under live replies and the
#  read path shapes the dangling ref as a ghost quote), the
#  direct_key UNIQUE that settles a racing DM double-create,
#  and the paging/expiry indexes.
#
#  Head of its app's chain; the suite runs `makemigrations
#  --check`, so drift against models.py fails the tests.
############################################################



import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Conversation',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('type', models.TextField(default='direct')),
                ('title', models.TextField(blank=True, null=True)),
                ('avatar_emoji', models.TextField(blank=True, null=True)),
                ('message_ttl_seconds', models.IntegerField(blank=True, null=True)),
                ('direct_key', models.TextField(blank=True, null=True, unique=True)),
                ('created_at', models.DateTimeField()),
                ('updated_at', models.DateTimeField()),
                ('created_by', models.ForeignKey(blank=True, db_column='created_by', db_index=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='created_conversations', to='users.user')),
            ],
            options={
                'db_table': 'conversations',
            },
        ),
        migrations.CreateModel(
            name='ConversationParticipant',
            fields=[
                ('pk', models.CompositePrimaryKey('conversation_id', 'user_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('pinned', models.IntegerField(default=0)),
                ('last_read_at', models.DateTimeField(blank=True, null=True)),
                ('joined_at', models.DateTimeField()),
                ('conversation', models.ForeignKey(db_column='conversation_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='participants', to='chat.conversation')),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='conversation_memberships', to='users.user')),
            ],
            options={
                'db_table': 'conversation_participants',
            },
        ),
        migrations.CreateModel(
            name='Message',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('text', models.TextField(default='')),
                ('image_url', models.TextField(blank=True, null=True)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('client_msg_id', models.TextField(blank=True, null=True)),
                ('kind', models.TextField(default='text')),
                ('edited_at', models.DateTimeField(blank=True, null=True)),
                ('attachment_url', models.TextField(blank=True, null=True)),
                ('attachment_name', models.TextField(blank=True, null=True)),
                ('attachment_size', models.IntegerField(blank=True, null=True)),
                ('attachment_mime', models.TextField(blank=True, null=True)),
                ('attachment_meta', models.JSONField(blank=True, null=True)),
                ('link_preview', models.JSONField(blank=True, null=True)),
                ('gallery', models.JSONField(blank=True, null=True)),
                ('pinned_at', models.DateTimeField(blank=True, null=True)),
                ('pinned_by', models.TextField(blank=True, null=True)),
                ('forwarded', models.BooleanField(default=False)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('conversation', models.ForeignKey(db_column='conversation_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='chat.conversation')),
                ('reply_to', models.ForeignKey(blank=True, db_column='reply_to_id', db_constraint=False, db_index=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='replies', to='chat.message')),
                ('sender', models.ForeignKey(db_column='sender_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='sent_messages', to='users.user')),
            ],
            options={
                'db_table': 'messages',
            },
        ),
        migrations.CreateModel(
            name='MessageReaction',
            fields=[
                ('pk', models.CompositePrimaryKey('message_id', 'user_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('emoji', models.TextField()),
                ('created_at', models.DateTimeField()),
                ('message', models.ForeignKey(db_column='message_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='reactions', to='chat.message')),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='message_reactions', to='users.user')),
            ],
            options={
                'db_table': 'message_reactions',
            },
        ),
        migrations.CreateModel(
            name='MessageRead',
            fields=[
                ('pk', models.CompositePrimaryKey('message_id', 'user_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('read_at', models.DateTimeField()),
                ('message', models.ForeignKey(db_column='message_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='reads', to='chat.message')),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='message_reads', to='users.user')),
            ],
            options={
                'db_table': 'message_reads',
            },
        ),
        migrations.AddIndex(
            model_name='conversation',
            index=models.Index(fields=['created_by'], name='idx_conversations_created_by'),
        ),
        migrations.AddConstraint(
            model_name='conversation',
            constraint=models.CheckConstraint(condition=models.Q(('type__in', ('direct', 'group'))), name='conversations_type_check'),
        ),
        migrations.AddIndex(
            model_name='conversationparticipant',
            index=models.Index(fields=['user'], name='idx_conv_participants_user'),
        ),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(fields=['conversation', '-created_at', '-id'], name='idx_messages_conversation'),
        ),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(fields=['sender'], name='idx_messages_sender'),
        ),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(fields=['reply_to'], name='idx_messages_reply_to'),
        ),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(condition=models.Q(('expires_at__isnull', False)), fields=['conversation', 'expires_at'], name='idx_messages_expires'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.UniqueConstraint(fields=('conversation', 'sender', 'client_msg_id'), name='idx_messages_client_msg'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.CheckConstraint(condition=models.Q(('kind__in', ('text', 'image', 'file', 'video', 'audio', 'system'))), name='messages_kind_check'),
        ),
        migrations.AddIndex(
            model_name='messagereaction',
            index=models.Index(fields=['user'], name='idx_message_reactions_user'),
        ),
        migrations.AddIndex(
            model_name='messageread',
            index=models.Index(fields=['user'], name='idx_message_reads_user'),
        ),
    ]
