############################################################
#  [*] notifications 0002 — the language column and the topic switches
#
#  Generated from models.py and committed; the suite's
#  `makemigrations --check` fails on drift, never a deploy.
############################################################


import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0001_initial'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='pushtoken',
            name='language',
            field=models.TextField(default='lt'),
        ),
        migrations.CreateModel(
            name='NotificationChannel',
            fields=[
                ('pk', models.CompositePrimaryKey('user_id', 'channel', blank=True, editable=False, primary_key=True, serialize=False)),
                ('channel', models.TextField()),
                ('enabled', models.IntegerField(default=1)),
                ('updated_at', models.TextField()),
                ('user', models.ForeignKey(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='notification_channels', to='users.user')),
            ],
            options={
                'db_table': 'notification_channels',
                'constraints': [models.CheckConstraint(condition=models.Q(('channel__in', ('news', 'chat', 'schedule', 'admin'))), name='notification_channels_channel_check')],
            },
        ),
    ]
