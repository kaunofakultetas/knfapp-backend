############################################################
#  [*] news 0002 — the feed and source indexes
#
#  news_posts(published_at DESC) carries the feed's window
#  and ordering; news_posts(source) carries the source
#  filter and the admin stats' grouped pass. Matches the
#  production database's index set.
############################################################

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('news', '0001_initial'),
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='newspost',
            index=models.Index(fields=['-published_at'], name='idx_news_posts_published'),
        ),
        migrations.AddIndex(
            model_name='newspost',
            index=models.Index(fields=['source'], name='idx_news_posts_source'),
        ),
    ]
