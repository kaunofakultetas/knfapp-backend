############################################################
#  [*] scraper 0001 — run bookkeeping
#
#  One table with the per-source status/started indexes the
#  run lock and the status endpoint lean on.
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
            name='ScraperRun',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('source', models.TextField()),
                ('status', models.TextField(default='running')),
                ('articles_found', models.IntegerField(default=0)),
                ('articles_new', models.IntegerField(default=0)),
                ('error_message', models.TextField(blank=True, null=True)),
                ('started_at', models.DateTimeField()),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'scraper_runs',
                'indexes': [models.Index(fields=['-started_at'], name='idx_scraper_runs_started'), models.Index(fields=['source', '-started_at'], name='idx_scraper_runs_src_started')],
                'constraints': [models.CheckConstraint(condition=models.Q(('status__in', ('running', 'completed', 'failed'))), name='scraper_runs_status_check')],
            },
        ),
    ]
