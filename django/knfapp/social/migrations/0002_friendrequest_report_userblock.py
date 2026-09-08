############################################################
#  [*] social 0002 — the handshake, blocks and the ledger
#
#  Generated from models.py and committed; the suite's
#  `makemigrations --check` fails on drift, never a deploy.
#  Carries the partial unique index on the
#  pending pair.
############################################################


import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('social', '0001_initial'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='FriendRequest',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('status', models.TextField(default='pending')),
                ('created_at', models.TextField()),
                ('updated_at', models.TextField()),
                ('from_user', models.ForeignKey(db_column='from_user_id', on_delete=django.db.models.deletion.CASCADE, related_name='sent_friend_requests', to='users.user')),
                ('to_user', models.ForeignKey(db_column='to_user_id', on_delete=django.db.models.deletion.CASCADE, related_name='received_friend_requests', to='users.user')),
            ],
            options={
                'db_table': 'friend_requests',
                'indexes': [models.Index(fields=['to_user', 'status'], name='idx_friend_requests_to'), models.Index(fields=['from_user', 'status'], name='idx_friend_requests_from')],
                'constraints': [models.CheckConstraint(condition=models.Q(('status__in', ('pending', 'accepted', 'rejected'))), name='friend_requests_status_check'), models.UniqueConstraint(condition=models.Q(('status', 'pending')), fields=('from_user', 'to_user'), name='idx_friend_requests_pending')],
            },
        ),
        migrations.CreateModel(
            name='Report',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('target_type', models.TextField()),
                ('target_id', models.TextField()),
                ('reason', models.TextField()),
                ('status', models.TextField(default='open')),
                ('created_at', models.TextField()),
                ('reporter', models.ForeignKey(db_column='reporter_id', on_delete=django.db.models.deletion.CASCADE, related_name='reports', to='users.user')),
            ],
            options={
                'db_table': 'reports',
                'constraints': [models.CheckConstraint(condition=models.Q(('target_type__in', ('user', 'post', 'message'))), name='reports_target_type_check'), models.CheckConstraint(condition=models.Q(('status__in', ('open', 'resolved'))), name='reports_status_check')],
            },
        ),
        migrations.CreateModel(
            name='UserBlock',
            fields=[
                ('pk', models.CompositePrimaryKey('blocker_id', 'blocked_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.TextField()),
                ('blocked', models.ForeignKey(db_column='blocked_id', on_delete=django.db.models.deletion.CASCADE, related_name='blocks_received', to='users.user')),
                ('blocker', models.ForeignKey(db_column='blocker_id', on_delete=django.db.models.deletion.CASCADE, related_name='blocks_made', to='users.user')),
            ],
            options={
                'db_table': 'user_blocks',
                'indexes': [models.Index(fields=['blocked'], name='idx_user_blocks_blocked')],
            },
        ),
    ]
