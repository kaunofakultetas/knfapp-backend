############################################################
#  [*] memes 0002 — the listing index
#
#  memes(created_at DESC) — the library lists newest first.
#  Matches the production database's index set.
############################################################

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('memes', '0001_initial'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='meme',
            index=models.Index(fields=['-created_at'], name='idx_memes_created'),
        ),
    ]
