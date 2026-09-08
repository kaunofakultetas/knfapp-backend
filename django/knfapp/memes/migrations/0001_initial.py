############################################################
#  [*] memes 0001 — the shared library's ledger
#
#  Generated from models.py and committed; the suite's
#  `makemigrations --check` fails on drift, never a deploy.
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
                ('created_at', models.TextField()),
                ('added_by', models.ForeignKey(blank=True, db_column='added_by', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='memes', to='users.user')),
            ],
            options={
                'db_table': 'memes',
            },
        ),
    ]
