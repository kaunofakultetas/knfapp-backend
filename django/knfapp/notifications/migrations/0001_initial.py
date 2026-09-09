############################################################
#  [*] notifications 0001 — push tokens and channel opt-outs
#
#  Both tables in one initial migration: the token UNIQUE,
#  the platform CHECK, the (active, user) index and the
#  composite-PK channel opt-out rows.
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
            name='NotificationChannel',
            fields=[
                ('pk', models.CompositePrimaryKey('user_id', 'channel', blank=True, editable=False, primary_key=True, serialize=False)),
                ('channel', models.TextField()),
                ('enabled', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField()),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='notification_channels', to='users.user')),
            ],
            options={
                'db_table': 'notification_channels',
                'constraints': [models.CheckConstraint(condition=models.Q(('channel__in', ('news', 'chat', 'schedule', 'admin'))), name='notification_channels_channel_check')],
            },
        ),
        migrations.CreateModel(
            name='PushToken',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('token', models.TextField(unique=True)),
                ('platform', models.TextField(default='unknown')),
                ('language', models.TextField(default='lt')),
                ('active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField()),
                ('updated_at', models.DateTimeField()),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='push_tokens', to='users.user')),
            ],
            options={
                'db_table': 'push_tokens',
                'indexes': [models.Index(fields=['user'], name='idx_push_tokens_user'), models.Index(fields=['active', 'user'], name='idx_push_tokens_active')],
                'constraints': [models.CheckConstraint(condition=models.Q(('platform__in', ('ios', 'android', 'web', 'unknown'))), name='push_tokens_platform_check')],
            },
        ),
    ]
