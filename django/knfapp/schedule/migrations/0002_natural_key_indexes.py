############################################################
#  [*] schedule 0002 — the natural-key indexes
#
#  idx_schedule_lessons_natural is the 8-column UNIQUE index
#  the schedule scraper's INSERT OR IGNORE dedups on;
#  idx_schedule_lessons_filter is the read index the views
#  ride. Matches models.py exactly
#  (makemigrations --check guards the drift).
############################################################


from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('schedule', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='schedulelesson',
            index=models.Index(fields=['semester', 'group_name', 'day_of_week'], name='idx_schedule_lessons_filter'),
        ),
        migrations.AddConstraint(
            model_name='schedulelesson',
            constraint=models.UniqueConstraint(fields=('semester', 'group_name', 'day_of_week', 'time_start', 'time_end', 'title', 'teacher', 'room'), name='idx_schedule_lessons_natural'),
        ),
    ]
