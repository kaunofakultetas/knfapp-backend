############################################################
#  [*] scraper 0001 — the run ledger
#
#  Matches models.py exactly (the suite runs
#  `makemigrations --check`, so drift fails the tests, not
#  a deploy). Table/column names match the production
#  database so the data cutover is a row copy; the source+
#  started_at index carries a shortened name (the live
#  31-char one is over Django's cap — names play no part in
#  the copy).
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
                ('started_at', models.TextField()),
                ('finished_at', models.TextField(blank=True, null=True)),
            ],
            options={
                'db_table': 'scraper_runs',
                'indexes': [models.Index(fields=['-started_at'], name='idx_scraper_runs_started'), models.Index(fields=['source', '-started_at'], name='idx_scraper_runs_src_started')],
                'constraints': [models.CheckConstraint(condition=models.Q(('status__in', ('running', 'completed', 'failed'))), name='scraper_runs_status_check')],
            },
        ),
    ]
