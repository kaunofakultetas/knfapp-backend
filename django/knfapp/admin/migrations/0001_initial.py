############################################################
#  [*] admin 0001 — the privileged-action audit trail
#
#  Matches models.py exactly (the suite runs
#  `makemigrations --check`, so drift fails the tests, not
#  a deploy). Table/column/index names match the
#  production database so the data cutover is a row copy.
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
                ('payload', models.TextField(blank=True, null=True)),
                ('created_at', models.TextField()),
                ('actor', models.ForeignKey(blank=True, db_column='actor_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='admin_actions', to='users.user')),
            ],
            options={
                'db_table': 'admin_audit',
                'indexes': [models.Index(fields=['-created_at'], name='idx_admin_audit_created'), models.Index(fields=['actor'], name='idx_admin_audit_actor')],
            },
        ),
    ]
