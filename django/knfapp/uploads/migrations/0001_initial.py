############################################################
#  [*] uploads 0001 — stored file rows
#
#  One table keyed by the uuid4-hex filename's row id, with
#  the owner index.
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
            name='Upload',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('filename', models.TextField(unique=True)),
                ('byte_size', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField()),
                ('user', models.ForeignKey(blank=True, db_column='user_id', db_index=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploads', to='users.user')),
            ],
            options={
                'db_table': 'uploads',
                'indexes': [models.Index(fields=['user'], name='idx_uploads_user'), models.Index(fields=['-created_at'], name='idx_uploads_created')],
            },
        ),
    ]
