############################################################
#  [*] notifications 0003 — the fan-out index
#
#  push_tokens(active, user_id) — every push fan-out scans
#  the active tokens joined against the channel opt-outs;
#  filter column first. Matches the production database's
#  index set.
############################################################

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0002_pushtoken_language_notificationchannel'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='pushtoken',
            index=models.Index(fields=['active', 'user'], name='idx_push_tokens_active'),
        ),
    ]
