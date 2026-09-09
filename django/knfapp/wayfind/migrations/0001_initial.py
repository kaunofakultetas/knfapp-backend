############################################################
#  [*] wayfind 0001 — the indoor-map schema
#
#  All eight tables in one initial migration: composite PKs
#  on entities/versions/frames, the scoped-id CHECK enums,
#  and the op-log/version indexes.
#
#  Head of its app's chain; the suite runs `makemigrations
#  --check`, so drift against models.py fails the tests.
############################################################



import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='WfBuilding',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('name', models.TextField()),
                ('north_deg', models.FloatField(blank=True, null=True)),
                ('entrance_node_id', models.TextField(blank=True, null=True)),
                ('draft_revision', models.IntegerField(default=0)),
                ('published_revision', models.IntegerField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('updated_at', models.DateTimeField()),
            ],
            options={
                'db_table': 'wf_buildings',
            },
        ),
        migrations.CreateModel(
            name='WfCapture',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('node_id', models.TextField(blank=True, null=True)),
                ('mode', models.TextField()),
                ('frame_hfov_deg', models.FloatField()),
                ('targets', models.JSONField()),
                ('expected', models.IntegerField()),
                ('status', models.TextField()),
                ('progress_pct', models.IntegerField(default=0)),
                ('report', models.JSONField(blank=True, null=True)),
                ('pano_id', models.TextField(blank=True, null=True)),
                ('created_by', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('updated_at', models.DateTimeField()),
                ('building', models.ForeignKey(db_column='building_id', on_delete=django.db.models.deletion.CASCADE, related_name='captures', to='wayfind.wfbuilding')),
            ],
            options={
                'db_table': 'wf_captures',
            },
        ),
        migrations.CreateModel(
            name='WfCaptureFrame',
            fields=[
                ('pk', models.CompositePrimaryKey('capture_id', 'target_id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('target_id', models.TextField()),
                ('yaw_deg', models.FloatField()),
                ('pitch_deg', models.FloatField()),
                ('roll_deg', models.FloatField()),
                ('bytes', models.IntegerField()),
                ('width', models.IntegerField()),
                ('height', models.IntegerField()),
                ('updated_at', models.DateTimeField()),
                ('capture', models.ForeignKey(db_column='capture_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='frames', to='wayfind.wfcapture')),
            ],
            options={
                'db_table': 'wf_capture_frames',
            },
        ),
        migrations.CreateModel(
            name='WfEntity',
            fields=[
                ('pk', models.CompositePrimaryKey('building_id', 'kind', 'id', blank=True, editable=False, primary_key=True, serialize=False)),
                ('kind', models.TextField()),
                ('id', models.TextField()),
                ('data', models.JSONField()),
                ('revision', models.IntegerField()),
                ('updated_at', models.DateTimeField()),
                ('updated_by', models.TextField(blank=True, null=True)),
                ('deleted', models.BooleanField(default=False)),
                ('building', models.ForeignKey(db_column='building_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='entities', to='wayfind.wfbuilding')),
            ],
            options={
                'db_table': 'wf_entities',
            },
        ),
        migrations.CreateModel(
            name='WfOp',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('revision', models.IntegerField(blank=True, null=True)),
                ('op', models.TextField()),
                ('author_id', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('status', models.TextField()),
                ('reason', models.TextField(blank=True, null=True)),
                ('building', models.ForeignKey(db_column='building_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='ops', to='wayfind.wfbuilding')),
            ],
            options={
                'db_table': 'wf_ops',
            },
        ),
        migrations.CreateModel(
            name='WfPanorama',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('node_id', models.TextField(blank=True, null=True)),
                ('width', models.IntegerField()),
                ('height', models.IntegerField()),
                ('bytes', models.IntegerField()),
                ('hfov_deg', models.FloatField(blank=True, null=True)),
                ('vfov_deg', models.FloatField(blank=True, null=True)),
                ('heading_raw_deg', models.FloatField(blank=True, null=True)),
                ('heading_source', models.TextField(blank=True, null=True)),
                ('uploaded_by', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('building', models.ForeignKey(db_column='building_id', on_delete=django.db.models.deletion.CASCADE, related_name='panoramas', to='wayfind.wfbuilding')),
            ],
            options={
                'db_table': 'wf_panoramas',
            },
        ),
        migrations.CreateModel(
            name='WfPlan',
            fields=[
                ('id', models.TextField(primary_key=True, serialize=False)),
                ('level_id', models.TextField(blank=True, null=True)),
                ('bytes', models.IntegerField()),
                ('uploaded_by', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField()),
                ('building', models.ForeignKey(db_column='building_id', on_delete=django.db.models.deletion.CASCADE, related_name='plans', to='wayfind.wfbuilding')),
            ],
            options={
                'db_table': 'wf_plans',
            },
        ),
        migrations.CreateModel(
            name='WfVersion',
            fields=[
                ('pk', models.CompositePrimaryKey('building_id', 'revision', blank=True, editable=False, primary_key=True, serialize=False)),
                ('revision', models.IntegerField()),
                ('document', models.TextField()),
                ('etag', models.TextField()),
                ('note', models.TextField(blank=True, null=True)),
                ('published_by', models.TextField(blank=True, null=True)),
                ('published_at', models.DateTimeField()),
                ('building', models.ForeignKey(db_column='building_id', db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='wayfind.wfbuilding')),
            ],
            options={
                'db_table': 'wf_versions',
            },
        ),
        migrations.AddIndex(
            model_name='wfcapture',
            index=models.Index(fields=['status', 'updated_at'], name='idx_wf_captures_status'),
        ),
        migrations.AddConstraint(
            model_name='wfcapture',
            constraint=models.CheckConstraint(condition=models.Q(('mode__in', ('full', 'walls'))), name='wf_captures_mode_check'),
        ),
        migrations.AddConstraint(
            model_name='wfcapture',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ('uploading', 'queued', 'stitching', 'done', 'failed'))), name='wf_captures_status_check'),
        ),
        migrations.AddIndex(
            model_name='wfentity',
            index=models.Index(fields=['building', 'revision'], name='idx_wf_entities_revision'),
        ),
        migrations.AddConstraint(
            model_name='wfentity',
            constraint=models.CheckConstraint(condition=models.Q(('kind__in', ('level', 'node', 'edge', 'room'))), name='wf_entities_kind_check'),
        ),
        migrations.AddIndex(
            model_name='wfop',
            index=models.Index(fields=['building', 'created_at'], name='idx_wf_ops_building'),
        ),
        migrations.AddConstraint(
            model_name='wfop',
            constraint=models.CheckConstraint(condition=models.Q(('status__in', ('applied', 'rejected'))), name='wf_ops_status_check'),
        ),
    ]
