############################################################
#  [*] social 0003 — the badge and queue indexes
#
#  activity(user_id, read) carries the unread-badge count
#  the app asks for on every focus; reports(status,
#  created_at DESC) carries the admin complaint queue.
#  Matches the production database's index set.
############################################################

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('social', '0002_friendrequest_report_userblock'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='activity',
            index=models.Index(fields=['user', 'read'], name='idx_activity_unread'),
        ),
        migrations.AddIndex(
            model_name='report',
            index=models.Index(fields=['status', '-created_at'], name='idx_reports_status'),
        ),
    ]
