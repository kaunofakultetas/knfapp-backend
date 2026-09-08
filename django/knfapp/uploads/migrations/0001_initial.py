############################################################
#  [*] uploads 0001 — the ownership ledger
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
            name="Upload",
            fields=[
                ("id", models.TextField(primary_key=True, serialize=False)),
                ("filename", models.TextField(unique=True)),
                ("user", models.ForeignKey(blank=True, db_column="user_id", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploads", to="users.user")),
                ("byte_size", models.IntegerField(default=0)),
                ("created_at", models.TextField()),
            ],
            options={"db_table": "uploads"},
        ),
        migrations.AddIndex(
            model_name="upload",
            index=models.Index(fields=["user"], name="idx_uploads_user"),
        ),
    ]
