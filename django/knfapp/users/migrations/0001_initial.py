############################################################
#  [*] users 0001 — accounts, invitation codes, sessions
#
#  The whole users schema in one initial migration: the
#  case-insensitive UNIQUE(lower(username/email)) pair, the
#  role CHECK, and the session/invite indexes ride along.
#
#  Head of its app's chain; the suite runs `makemigrations
#  --check`, so drift against models.py fails the tests.
############################################################



import django.db.models.deletion
import django.db.models.functions.text
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('username', models.TextField(unique=True)),
                ('email', models.TextField(unique=True)),
                ('display_name', models.TextField()),
                ('password_hash', models.TextField()),
                ('role', models.TextField(default='student')),
                ('invited', models.BooleanField(default=False)),
                ('avatar_url', models.TextField(blank=True, null=True)),
                ('student_number', models.TextField(blank=True, null=True)),
                ('study_group', models.TextField(blank=True, null=True)),
                ('study_program', models.TextField(blank=True, null=True)),
                ('active', models.BooleanField(default=True)),
                ('chat_push_preview', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField()),
                ('updated_at', models.DateTimeField()),
            ],
            options={
                'db_table': 'users',
                'constraints': [models.CheckConstraint(condition=models.Q(('role__in', ('student', 'teacher', 'admin', 'curator'))), name='users_role_check'), models.UniqueConstraint(django.db.models.functions.text.Lower('username'), name='users_username_ci'), models.UniqueConstraint(django.db.models.functions.text.Lower('email'), name='users_email_ci')],
            },
        ),
        migrations.CreateModel(
            name='Session',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('token', models.TextField(unique=True)),
                ('created_at', models.DateTimeField()),
                ('expires_at', models.DateTimeField()),
                ('user', models.ForeignKey(db_column='user_id', on_delete=django.db.models.deletion.CASCADE, related_name='sessions', to='users.user')),
            ],
            options={
                'db_table': 'sessions',
            },
        ),
        migrations.CreateModel(
            name='InvitationCode',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('code', models.TextField(unique=True)),
                ('role', models.TextField(default='student')),
                ('max_uses', models.IntegerField(default=1)),
                ('use_count', models.IntegerField(default=0)),
                ('expires_at', models.DateTimeField()),
                ('created_at', models.DateTimeField()),
                ('created_by', models.ForeignKey(blank=True, db_column='created_by', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_invitation_codes', to='users.user')),
            ],
            options={
                'db_table': 'invitation_codes',
                'constraints': [models.CheckConstraint(condition=models.Q(('role__in', ('student', 'teacher', 'admin', 'curator'))), name='invitation_codes_role_check')],
            },
        ),
    ]
