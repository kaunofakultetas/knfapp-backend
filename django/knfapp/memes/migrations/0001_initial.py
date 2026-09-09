############################################################
#  [*] memes 0001 — the shared picture library
#
#  One table with the folded-search column and the
#  created_at index.
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
            name='Meme',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('filename', models.TextField(unique=True)),
                ('title', models.TextField()),
                ('tags', models.TextField(blank=True, null=True)),
                ('byte_size', models.IntegerField(default=0)),
                ('width', models.IntegerField(blank=True, null=True)),
                ('height', models.IntegerField(blank=True, null=True)),
                ('preview', models.TextField(blank=True, null=True)),
                ('search', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('added_by', models.ForeignKey(blank=True, db_column='added_by', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='memes', to='users.user')),
            ],
            options={
                'db_table': 'memes',
                'indexes': [models.Index(fields=['-created_at'], name='idx_memes_created')],
            },
        ),
    ]
