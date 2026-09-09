############################################################
#  [*] news 0001 — posts, likes, comments, polls, tombstones
#
#  The whole news schema in one initial migration: the
#  composite-PK likes/votes, the one-poll-per-post UNIQUE,
#  the source/post_type CHECKs and the feed indexes.
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
            name='DeletedSourceUrl',
            fields=[
                ('source_url', models.TextField(primary_key=True, serialize=False)),
                ('deleted_at', models.DateTimeField()),
                ('deleted_by', models.ForeignKey(blank=True, db_column='deleted_by', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='deleted_source_urls', to='users.user')),
            ],
            options={
                'db_table': 'deleted_source_urls',
            },
        ),
        migrations.CreateModel(
            name='NewsPost',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('title', models.TextField()),
                ('content', models.TextField()),
                ('summary', models.TextField(blank=True, null=True)),
                ('image_url', models.TextField(blank=True, null=True)),
                ('author_name', models.TextField(blank=True, null=True)),
                ('source', models.TextField(default='app')),
                ('source_url', models.TextField(blank=True, null=True, unique=True)),
                ('post_type', models.TextField(default='article')),
                ('is_public', models.BooleanField(default=True)),
                ('likes_count', models.IntegerField(default=0)),
                ('comments_count', models.IntegerField(default=0)),
                ('shares_count', models.IntegerField(default=0)),
                ('published_at', models.DateTimeField()),
                ('created_at', models.DateTimeField()),
                ('updated_at', models.DateTimeField()),
                ('author', models.ForeignKey(blank=True, db_column='author_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='news_posts', to='users.user')),
            ],
            options={
                'db_table': 'news_posts',
            },
        ),
        migrations.CreateModel(
            name='NewsLike',
            fields=[
                ('pk', models.CompositePrimaryKey('user_id', 'post_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField()),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='news_likes', to='users.user')),
                ('post', models.ForeignKey(db_column='post_id', on_delete=django.db.models.deletion.CASCADE, related_name='likes', to='news.newspost')),
            ],
            options={
                'db_table': 'news_likes',
            },
        ),
        migrations.CreateModel(
            name='NewsComment',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('text', models.TextField()),
                ('created_at', models.DateTimeField()),
                ('user', models.ForeignKey(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='news_comments', to='users.user')),
                ('post', models.ForeignKey(db_column='post_id', on_delete=django.db.models.deletion.CASCADE, related_name='comments', to='news.newspost')),
            ],
            options={
                'db_table': 'news_comments',
            },
        ),
        migrations.CreateModel(
            name='Poll',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('title', models.TextField()),
                ('end_date', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('post', models.OneToOneField(db_column='post_id', on_delete=django.db.models.deletion.CASCADE, related_name='poll', to='news.newspost')),
            ],
            options={
                'db_table': 'polls',
            },
        ),
        migrations.CreateModel(
            name='PollOption',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('text', models.TextField()),
                ('votes', models.IntegerField(default=0)),
                ('position', models.IntegerField(default=0)),
                ('poll', models.ForeignKey(db_column='poll_id', on_delete=django.db.models.deletion.CASCADE, related_name='options', to='news.poll')),
            ],
            options={
                'db_table': 'poll_options',
                'ordering': ['position', 'id'],
            },
        ),
        migrations.CreateModel(
            name='PollVote',
            fields=[
                ('pk', models.CompositePrimaryKey('user_id', 'poll_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField()),
                ('option', models.ForeignKey(db_column='option_id', on_delete=django.db.models.deletion.CASCADE, related_name='option_votes', to='news.polloption')),
                ('poll', models.ForeignKey(db_column='poll_id', on_delete=django.db.models.deletion.CASCADE, related_name='votes', to='news.poll')),
                ('user', models.ForeignKey(db_column='user_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='poll_votes', to='users.user')),
            ],
            options={
                'db_table': 'poll_votes',
            },
        ),
        migrations.AddIndex(
            model_name='newspost',
            index=models.Index(fields=['-published_at'], name='idx_news_posts_published'),
        ),
        migrations.AddIndex(
            model_name='newspost',
            index=models.Index(fields=['source'], name='idx_news_posts_source'),
        ),
        migrations.AddConstraint(
            model_name='newspost',
            constraint=models.CheckConstraint(condition=models.Q(('source__in', ('app', 'knf.vu.lt', 'vu.lt', 'faculty', 'user'))), name='news_posts_source_check'),
        ),
        migrations.AddConstraint(
            model_name='newspost',
            constraint=models.CheckConstraint(condition=models.Q(('post_type__in', ('article', 'social', 'announcement', 'poll', 'link'))), name='news_posts_post_type_check'),
        ),
    ]
