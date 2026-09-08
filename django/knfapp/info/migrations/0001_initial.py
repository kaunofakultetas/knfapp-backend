############################################################
#  [*] info 0001 — the handbook overlay rows
#
#  Generated from models.py and committed; the suite's
#  `makemigrations --check` fails on drift, never a deploy.
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
                ('data_json', models.TextField()),
                ('scraped_at', models.TextField()),
            ],
            options={
                'db_table': 'faculty_info',
                'constraints': [models.UniqueConstraint(fields=('lang', 'section'), name='faculty_info_lang_section')],
            },
        ),
    ]
