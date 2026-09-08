############################################################
#  [*] schedule 0001 — the scraped timetable
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
            name='ScheduleLesson',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('title', models.TextField()),
                ('teacher', models.TextField(blank=True, null=True)),
                ('room', models.TextField(blank=True, null=True)),
                ('time_start', models.TextField()),
                ('time_end', models.TextField()),
                ('day_of_week', models.IntegerField()),
                ('group_name', models.TextField(blank=True, null=True)),
                ('semester', models.TextField(blank=True, null=True)),
                ('created_at', models.TextField()),
            ],
            options={
                'db_table': 'schedule_lessons',
                'constraints': [models.CheckConstraint(condition=models.Q(('day_of_week__gte', 0), ('day_of_week__lte', 6)), name='schedule_lessons_day_check')],
            },
        ),
    ]
