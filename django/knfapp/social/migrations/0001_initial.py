############################################################
#  [*] social 0001 — friendships and the activity list
#
#  Generated from models.py and committed; the suite's
#  `makemigrations --check` fails on drift, never a deploy.
#  Table/column names match the production database so the
#  data cutover is a row copy.
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
            name='Friendship',
            fields=[
                ('pk', models.CompositePrimaryKey('user_id', 'friend_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.TextField()),
                ('friend', models.ForeignKey(db_column='friend_id', on_delete=django.db.models.deletion.CASCADE, related_name='friend_of', to='users.user')),
                ('user', models.ForeignKey(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='friendships', to='users.user')),
            ],
            options={
                'db_table': 'friendships',
            },
        ),
        migrations.CreateModel(
            name='Activity',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('kind', models.TextField()),
                ('subject_id', models.TextField(blank=True, null=True)),
                ('subject_preview', models.TextField(blank=True, null=True)),
                ('created_at', models.TextField()),
                ('read', models.IntegerField(default=0)),
                ('actor', models.ForeignKey(db_column='actor_id', on_delete=django.db.models.deletion.CASCADE, related_name='acted_activity', to='users.user')),
                ('user', models.ForeignKey(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='activity', to='users.user')),
            ],
            options={
                'db_table': 'activity',
                'indexes': [models.Index(fields=['user', '-created_at', '-id'], name='idx_activity_user')],
                'constraints': [models.CheckConstraint(condition=models.Q(('kind__in', ('like', 'comment', 'connect_request', 'connect_accept'))), name='activity_kind_check'), models.UniqueConstraint(fields=('user', 'kind', 'actor', 'subject_id'), name='activity_unique_row')],
            },
        ),
    ]
