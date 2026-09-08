############################################################
#  [*] users 0001 — accounts, invitation codes, sessions
#
#  Hand-written to match models.py exactly (the suite runs
#  `makemigrations --check`, so drift fails the tests, not
#  a deploy). Table/column names match the production
#  database so the data cutover is a row copy.
############################################################


from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="User",
            fields=[
                ("id", models.TextField(primary_key=True, serialize=False)),
                ("username", models.TextField(unique=True)),
                ("email", models.TextField(unique=True)),
                ("display_name", models.TextField()),
                ("password_hash", models.TextField()),
                ("role", models.TextField(default="student")),
                ("invited", models.IntegerField(default=0)),
                ("avatar_url", models.TextField(blank=True, null=True)),
                ("student_number", models.TextField(blank=True, null=True)),
                ("study_group", models.TextField(blank=True, null=True)),
                ("study_program", models.TextField(blank=True, null=True)),
                ("active", models.IntegerField(default=1)),
                ("chat_push_preview", models.IntegerField(default=1)),
                ("created_at", models.TextField()),
                ("updated_at", models.TextField()),
            ],
            options={"db_table": "users"},
        ),
        migrations.CreateModel(
            name="Session",
            fields=[
                ("id", models.TextField(primary_key=True, serialize=False)),
                ("user", models.ForeignKey(db_column="user_id", on_delete=django.db.models.deletion.CASCADE, related_name="sessions", to="users.user")),
                ("token", models.TextField(unique=True)),
                ("created_at", models.TextField()),
                ("expires_at", models.TextField()),
            ],
            options={"db_table": "sessions"},
        ),
        migrations.CreateModel(
            name="InvitationCode",
            fields=[
                ("id", models.TextField(primary_key=True, serialize=False)),
                ("code", models.TextField(unique=True)),
                ("role", models.TextField(default="student")),
                ("created_by", models.ForeignKey(blank=True, db_column="created_by", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_invitation_codes", to="users.user")),
                ("max_uses", models.IntegerField(default=1)),
                ("use_count", models.IntegerField(default=0)),
                ("expires_at", models.TextField()),
                ("created_at", models.TextField()),
            ],
            options={"db_table": "invitation_codes"},
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(condition=models.Q(("role__in", ("student", "teacher", "admin", "curator"))), name="users_role_check"),
        ),
        migrations.AddConstraint(
            model_name="invitationcode",
            constraint=models.CheckConstraint(condition=models.Q(("role__in", ("student", "teacher", "admin", "curator"))), name="invitation_codes_role_check"),
        ),
    ]
