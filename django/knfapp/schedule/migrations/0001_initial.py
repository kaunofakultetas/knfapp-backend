############################################################
#  [*] schedule 0001 — scraped lessons
#
#  One table with the 8-column natural-key UNIQUE (the row
#  IS the identity; teacher/room store '' so the key stays
#  NOT NULL) and the filter index.
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
            name='ScheduleLesson',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('title', models.TextField()),
                ('teacher', models.TextField(blank=True, default='')),
                ('room', models.TextField(blank=True, default='')),
                ('time_start', models.TextField()),
                ('time_end', models.TextField()),
                ('day_of_week', models.IntegerField()),
                ('group_name', models.TextField(blank=True, default='')),
                ('semester', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField()),
            ],
            options={
                'db_table': 'schedule_lessons',
                'indexes': [models.Index(fields=['semester', 'group_name', 'day_of_week'], name='idx_schedule_lessons_filter')],
                'constraints': [models.CheckConstraint(condition=models.Q(('day_of_week__gte', 0), ('day_of_week__lte', 6)), name='schedule_lessons_day_check'), models.UniqueConstraint(fields=('semester', 'group_name', 'day_of_week', 'time_start', 'time_end', 'title', 'teacher', 'room'), name='idx_schedule_lessons_natural')],
            },
        ),
    ]
