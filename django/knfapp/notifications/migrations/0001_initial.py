############################################################
#  [*] notifications 0001 — the push token registry
#
#  Hand-written to match models.py; drift fails the suite's
#  `makemigrations --check`, never a deploy.
############################################################


from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [("users", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="PushToken",
            fields=[
                ("id", models.TextField(primary_key=True, serialize=False)),
                ("user", models.ForeignKey(db_column="user_id", on_delete=django.db.models.deletion.CASCADE, related_name="push_tokens", to="users.user")),
                ("token", models.TextField(unique=True)),
                ("platform", models.TextField(default="unknown")),
                ("active", models.IntegerField(default=1)),
                ("created_at", models.TextField()),
                ("updated_at", models.TextField()),
            ],
            options={"db_table": "push_tokens"},
        ),
        migrations.AddIndex(
            model_name="pushtoken",
            index=models.Index(fields=["user"], name="idx_push_tokens_user"),
        ),
    ]
