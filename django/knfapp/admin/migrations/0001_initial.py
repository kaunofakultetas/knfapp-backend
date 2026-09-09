############################################################
#  [*] admin 0001 — the audit trail
#
#  One table: SET_NULL actor, JSON payload, and the
#  created/actor indexes.
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
            name='AdminAudit',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('action', models.TextField()),
                ('target', models.TextField(blank=True, null=True)),
                ('payload', models.JSONField(null=True)),
                ('created_at', models.DateTimeField()),
                ('actor', models.ForeignKey(blank=True, db_column='actor_id', db_index=False, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='admin_actions', to='users.user')),
            ],
            options={
                'db_table': 'admin_audit',
                'indexes': [models.Index(fields=['-created_at'], name='idx_admin_audit_created'), models.Index(fields=['actor'], name='idx_admin_audit_actor')],
            },
        ),
    ]
