############################################################
#  [*] chat 0001 — the five messaging tables
#
#  Matches models.py exactly (the suite runs
#  `makemigrations --check`, so drift fails the tests, not
#  a deploy). Table/column/index names match the
#  production database so the data cutover is a row copy; the
#  idempotency index and the composite PKs ride along.
#  The FTS5 shadow table is 0002.
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
                ('created_at', models.TextField()),
                ('updated_at', models.TextField()),
                ('created_by', models.ForeignKey(blank=True, db_column='created_by', null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='created_conversations', to='users.user')),
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
                ('last_read_at', models.TextField(blank=True, null=True)),
                ('joined_at', models.TextField()),
                ('conversation', models.ForeignKey(db_column='conversation_id', on_delete=django.db.models.deletion.CASCADE, related_name='participants', to='chat.conversation')),
                ('user', models.ForeignKey(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='conversation_memberships', to='users.user')),
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
                ('deleted_at', models.TextField(blank=True, null=True)),
                ('client_msg_id', models.TextField(blank=True, null=True)),
                ('kind', models.TextField(default='text')),
                ('edited_at', models.TextField(blank=True, null=True)),
                ('attachment_url', models.TextField(blank=True, null=True)),
                ('attachment_name', models.TextField(blank=True, null=True)),
                ('attachment_size', models.IntegerField(blank=True, null=True)),
                ('attachment_mime', models.TextField(blank=True, null=True)),
                ('attachment_meta', models.TextField(blank=True, null=True)),
                ('link_preview', models.TextField(blank=True, null=True)),
                ('gallery', models.TextField(blank=True, null=True)),
                ('pinned_at', models.TextField(blank=True, null=True)),
                ('pinned_by', models.TextField(blank=True, null=True)),
                ('forwarded', models.IntegerField(default=0)),
                ('expires_at', models.TextField(blank=True, null=True)),
                ('created_at', models.TextField()),
                ('conversation', models.ForeignKey(db_column='conversation_id', on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='chat.conversation')),
                ('reply_to', models.ForeignKey(blank=True, db_column='reply_to_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='replies', to='chat.message')),
                ('sender', models.ForeignKey(db_column='sender_id', on_delete=django.db.models.deletion.CASCADE, related_name='sent_messages', to='users.user')),
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
                ('created_at', models.TextField()),
                ('message', models.ForeignKey(db_column='message_id', on_delete=django.db.models.deletion.CASCADE, related_name='reactions', to='chat.message')),
                ('user', models.ForeignKey(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='message_reactions', to='users.user')),
            ],
            options={
                'db_table': 'message_reactions',
            },
        ),
        migrations.CreateModel(
            name='MessageRead',
            fields=[
                ('pk', models.CompositePrimaryKey('message_id', 'user_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('read_at', models.TextField()),
                ('message', models.ForeignKey(db_column='message_id', on_delete=django.db.models.deletion.CASCADE, related_name='reads', to='chat.message')),
                ('user', models.ForeignKey(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='message_reads', to='users.user')),
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
            index=models.Index(fields=['conversation', '-created_at'], name='idx_messages_conversation'),
        ),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(fields=['sender'], name='idx_messages_sender'),
        ),
        migrations.AddIndex(
            model_name='message',
            index=models.Index(fields=['reply_to'], name='idx_messages_reply_to'),
        ),
        migrations.AddConstraint(
            model_name='message',
            constraint=models.UniqueConstraint(fields=('conversation', 'sender', 'client_msg_id'), name='idx_messages_client_msg'),
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
