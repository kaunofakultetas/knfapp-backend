############################################################
#  [*] info 0001 — the scraped faculty handbook overlay
#
#  One table: a JSON blob per (lang, section), UNIQUE on the
#  pair.
#
#  Head of its app's chain; the suite runs `makemigrations
#  --check`, so drift against models.py fails the tests.
############################################################



from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='FacultyInfo',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('lang', models.TextField(default='lt')),
                ('section', models.TextField()),
                ('data_json', models.JSONField()),
                ('scraped_at', models.DateTimeField()),
            ],
            options={
                'db_table': 'faculty_info',
                'constraints': [models.UniqueConstraint(fields=('lang', 'section'), name='faculty_info_lang_section')],
            },
        ),
    ]
