############################################################
#  [*] social 0001 — friendships, requests, blocks, reports
#
#  The whole social schema in one initial migration: the
#  pending-request partial UNIQUE, the not-self CHECKs, the
#  composite-PK block pairs and the activity partial UNIQUE
#  over NULL-subject rows.
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
            name='Activity',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('kind', models.TextField()),
                ('subject_id', models.TextField(blank=True, null=True)),
                ('subject_preview', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('read', models.BooleanField(default=False)),
                ('actor', models.ForeignKey(db_column='actor_id', on_delete=django.db.models.deletion.CASCADE, related_name='acted_activity', to='users.user')),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='activity', to='users.user')),
            ],
            options={
                'db_table': 'activity',
                'indexes': [models.Index(fields=['user', '-created_at', '-id'], name='idx_activity_user'), models.Index(fields=['user', 'read'], name='idx_activity_unread')],
                'constraints': [models.CheckConstraint(condition=models.Q(('kind__in', ('like', 'comment', 'connect_request', 'connect_accept'))), name='activity_kind_check'), models.UniqueConstraint(fields=('user', 'kind', 'actor', 'subject_id'), name='activity_unique_row'), models.UniqueConstraint(condition=models.Q(('subject_id__isnull', True)), fields=('user', 'kind', 'actor'), name='activity_unique_null_subject')],
            },
        ),
        migrations.CreateModel(
            name='FriendRequest',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('status', models.TextField(default='pending')),
                ('created_at', models.DateTimeField()),
                ('updated_at', models.DateTimeField()),
                ('from_user', models.ForeignKey(db_column='from_user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='sent_friend_requests', to='users.user')),
                ('to_user', models.ForeignKey(db_column='to_user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='received_friend_requests', to='users.user')),
            ],
            options={
                'db_table': 'friend_requests',
                'indexes': [models.Index(fields=['to_user', 'status'], name='idx_friend_requests_to'), models.Index(fields=['from_user', 'status'], name='idx_friend_requests_from')],
                'constraints': [models.CheckConstraint(condition=models.Q(('status__in', ('pending', 'accepted', 'rejected'))), name='friend_requests_status_check'), models.CheckConstraint(condition=models.Q(('from_user', models.F('to_user')), _negated=True), name='friend_requests_not_self_check'), models.UniqueConstraint(condition=models.Q(('status', 'pending')), fields=('from_user', 'to_user'), name='idx_friend_requests_pending')],
            },
        ),
        migrations.CreateModel(
            name='Friendship',
            fields=[
                ('pk', models.CompositePrimaryKey('user_id', 'friend_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField()),
                ('friend', models.ForeignKey(db_column='friend_id', on_delete=django.db.models.deletion.CASCADE, related_name='friend_of', to='users.user')),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='friendships', to='users.user')),
            ],
            options={
                'db_table': 'friendships',
                'constraints': [models.CheckConstraint(condition=models.Q(('user', models.F('friend')), _negated=True), name='friendships_not_self_check')],
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
                ('created_at', models.DateTimeField()),
                ('reporter', models.ForeignKey(db_column='reporter_id', on_delete=django.db.models.deletion.CASCADE, related_name='reports', to='users.user')),
            ],
            options={
                'db_table': 'reports',
                'indexes': [models.Index(fields=['status', '-created_at'], name='idx_reports_status')],
                'constraints': [models.CheckConstraint(condition=models.Q(('target_type__in', ('user', 'post', 'message'))), name='reports_target_type_check'), models.CheckConstraint(condition=models.Q(('status__in', ('open', 'resolved'))), name='reports_status_check')],
            },
        ),
        migrations.CreateModel(
            name='UserBlock',
            fields=[
                ('pk', models.CompositePrimaryKey('blocker_id', 'blocked_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField()),
                ('blocked', models.ForeignKey(db_column='blocked_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='blocks_received', to='users.user')),
                ('blocker', models.ForeignKey(db_column='blocker_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='blocks_made', to='users.user')),
            ],
            options={
                'db_table': 'user_blocks',
                'indexes': [models.Index(fields=['blocked'], name='idx_user_blocks_blocked')],
                'constraints': [models.CheckConstraint(condition=models.Q(('blocker', models.F('blocked')), _negated=True), name='user_blocks_not_self_check')],
            },
        ),
    ]
