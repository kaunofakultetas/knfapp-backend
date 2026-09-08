############################################################
#  [*] news — posts, likes, comments, polls
#
#  One table serves BOTH feeds: scraped articles, faculty
#  announcements and members' wall posts (source 'user') all
#  live in news_posts, which is why the visibility predicate
#  (core.can_view_post) and the ranking care about source.
#  The engagement counters are denormalised on the row and
#  RECOMPUTED from the child tables on every write — never
#  nudged ±1 — so a drifted counter heals instead of
#  compounding. deleted_source_urls is the tombstone that
#  keeps the scrapers from resurrecting an admin-deleted
#  article on their next tick.
#
#  news_likes and poll_votes keep their composite primary
#  keys (Django 5.2 CompositePrimaryKey — the live tables
#  have no surrogate id). Shape policy as in users/models.py.
############################################################


from django.db import models


from knfapp.users.models import User


POST_TYPES = ("article", "social", "announcement", "poll", "link")
SOURCES = ("app", "knf.vu.lt", "vu.lt", "faculty", "user")
SCRAPED_SOURCES = ("knf.vu.lt", "vu.lt")


class NewsPost(models.Model):
    id = models.TextField(primary_key=True)
    title = models.TextField()
    content = models.TextField()
    summary = models.TextField(null=True, blank=True)
    image_url = models.TextField(null=True, blank=True)
    author = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                               db_column="author_id", related_name="news_posts")
    author_name = models.TextField(null=True, blank=True)
    source = models.TextField(default="app")
    source_url = models.TextField(null=True, blank=True, unique=True)
    post_type = models.TextField(default="article")
    is_public = models.IntegerField(default=1)
    likes_count = models.IntegerField(default=0)
    comments_count = models.IntegerField(default=0)
    shares_count = models.IntegerField(default=0)
    published_at = models.TextField()
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "news_posts"
        indexes = [
            # The feed's window and ordering ride published_at;
            # the source filter and the admin stats' grouped
            # pass ride source
            models.Index(fields=["-published_at"], name="idx_news_posts_published"),
            models.Index(fields=["source"], name="idx_news_posts_source"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(source__in=SOURCES), name="news_posts_source_check"),
            models.CheckConstraint(condition=models.Q(post_type__in=POST_TYPES), name="news_posts_post_type_check"),
        ]


class NewsLike(models.Model):
    pk = models.CompositePrimaryKey("user_id", "post_id")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id", related_name="news_likes")
    post = models.ForeignKey(NewsPost, on_delete=models.CASCADE, db_column="post_id", related_name="likes")
    created_at = models.TextField()

    class Meta:
        db_table = "news_likes"


class NewsComment(models.Model):
    id = models.TextField(primary_key=True)
    post = models.ForeignKey(NewsPost, on_delete=models.CASCADE, db_column="post_id", related_name="comments")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id", related_name="news_comments")
    text = models.TextField()
    created_at = models.TextField()

    class Meta:
        db_table = "news_comments"


class Poll(models.Model):
    id = models.TextField(primary_key=True)
    # OneToOne puts the unique index on post_id — one poll per
    # post, a racing twin's IntegrityError answering the same 409
    post = models.OneToOneField(NewsPost, on_delete=models.CASCADE, db_column="post_id", related_name="poll")
    title = models.TextField()
    end_date = models.TextField(null=True, blank=True)
    total_votes = models.IntegerField(default=0)
    created_at = models.TextField()

    class Meta:
        db_table = "polls"


class PollOption(models.Model):
    id = models.TextField(primary_key=True)
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, db_column="poll_id", related_name="options")
    text = models.TextField()
    votes = models.IntegerField(default=0)
    # The live table orders options by rowid — the order the
    # creator sent them. An explicit column keeps that order
    # portable off SQLite; the data migration backfills it
    position = models.IntegerField(default=0)

    class Meta:
        db_table = "poll_options"
        ordering = ["position", "id"]


class PollVote(models.Model):
    pk = models.CompositePrimaryKey("user_id", "poll_id")
    user = models.ForeignKey(User, on_delete=models.CASCADE, db_column="user_id", related_name="poll_votes")
    poll = models.ForeignKey(Poll, on_delete=models.CASCADE, db_column="poll_id", related_name="votes")
    option = models.ForeignKey(PollOption, on_delete=models.CASCADE, db_column="option_id", related_name="option_votes")
    created_at = models.TextField()

    class Meta:
        db_table = "poll_votes"


class DeletedSourceUrl(models.Model):
    source_url = models.TextField(primary_key=True)
    deleted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL,
                                   db_column="deleted_by", related_name="deleted_source_urls")
    deleted_at = models.TextField()

    class Meta:
        db_table = "deleted_source_urls"
