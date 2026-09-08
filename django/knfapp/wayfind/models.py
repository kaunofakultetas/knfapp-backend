############################################################
#  [*] wayfind — the indoor-map tables
#
#  Eight tables matching the production database
#  byte-for-byte. The draft is one row per entity
#  (wf_entities, composite PK over building/kind/id) with a
#  tombstone flag so a `since` delta can carry deletions;
#  wf_ops is the idempotency log (rows keyed
#  '<building>:<op id>'); wf_versions holds each published
#  document verbatim — its text IS what the ETag hashes.
#  Panoramas and plans are content-addressed (the row id is
#  the sha256 of the stored bytes). Captures live scoped
#  like ops ('<building>:<client id>'), their frames named
#  by ROLE (one row per target, replaced on re-shoot).
#  Shape policy as in users/models.py.
############################################################


from django.db import models


ENTITY_KINDS = ("level", "node", "edge", "room")
CAPTURE_MODES = ("full", "walls")
CAPTURE_STATUSES = ("uploading", "queued", "stitching", "done", "failed")


class WfBuilding(models.Model):
    id = models.TextField(primary_key=True)
    name = models.TextField()
    north_deg = models.FloatField(null=True, blank=True)
    entrance_node_id = models.TextField(null=True, blank=True)
    draft_revision = models.IntegerField(default=0)
    published_revision = models.IntegerField(null=True, blank=True)
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "wf_buildings"


class WfEntity(models.Model):
    pk = models.CompositePrimaryKey("building_id", "kind", "id")
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="entities")
    kind = models.TextField()
    id = models.TextField()
    data = models.TextField()
    revision = models.IntegerField()
    updated_at = models.TextField()
    updated_by = models.TextField(null=True, blank=True)
    deleted = models.IntegerField(default=0)

    class Meta:
        db_table = "wf_entities"
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=ENTITY_KINDS),
                                   name="wf_entities_kind_check"),
        ]
        indexes = [
            models.Index(fields=["building", "revision"], name="idx_wf_entities_revision"),
        ]


class WfOp(models.Model):
    id = models.TextField(primary_key=True)
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="ops")
    revision = models.IntegerField(null=True, blank=True)
    op = models.TextField()
    author_id = models.TextField(null=True, blank=True)
    created_at = models.TextField()
    status = models.TextField()
    reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "wf_ops"
        constraints = [
            models.CheckConstraint(condition=models.Q(status__in=("applied", "rejected")),
                                   name="wf_ops_status_check"),
        ]
        indexes = [
            models.Index(fields=["building", "created_at"], name="idx_wf_ops_building"),
        ]


class WfVersion(models.Model):
    pk = models.CompositePrimaryKey("building_id", "revision")
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="versions")
    revision = models.IntegerField()
    document = models.TextField()
    etag = models.TextField()
    note = models.TextField(null=True, blank=True)
    published_by = models.TextField(null=True, blank=True)
    published_at = models.TextField()

    class Meta:
        db_table = "wf_versions"


class WfPanorama(models.Model):
    id = models.TextField(primary_key=True)
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="panoramas")
    node_id = models.TextField(null=True, blank=True)
    width = models.IntegerField()
    height = models.IntegerField()
    bytes = models.IntegerField()
    hfov_deg = models.FloatField(null=True, blank=True)
    vfov_deg = models.FloatField(null=True, blank=True)
    heading_raw_deg = models.FloatField(null=True, blank=True)
    heading_source = models.TextField(null=True, blank=True)
    uploaded_by = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "wf_panoramas"


class WfPlan(models.Model):
    id = models.TextField(primary_key=True)
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="plans")
    level_id = models.TextField(null=True, blank=True)
    bytes = models.IntegerField()
    uploaded_by = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    class Meta:
        db_table = "wf_plans"


class WfCapture(models.Model):
    id = models.TextField(primary_key=True)
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="captures")
    node_id = models.TextField(null=True, blank=True)
    mode = models.TextField()
    frame_hfov_deg = models.FloatField()
    targets = models.TextField()
    expected = models.IntegerField()
    status = models.TextField()
    progress_pct = models.IntegerField(default=0)
    report = models.TextField(null=True, blank=True)
    pano_id = models.TextField(null=True, blank=True)
    created_by = models.TextField(null=True, blank=True)
    created_at = models.TextField()
    updated_at = models.TextField()

    class Meta:
        db_table = "wf_captures"
        constraints = [
            models.CheckConstraint(condition=models.Q(mode__in=CAPTURE_MODES),
                                   name="wf_captures_mode_check"),
            models.CheckConstraint(condition=models.Q(status__in=CAPTURE_STATUSES),
                                   name="wf_captures_status_check"),
        ]
        indexes = [
            models.Index(fields=["status", "updated_at"], name="idx_wf_captures_status"),
        ]


class WfCaptureFrame(models.Model):
    pk = models.CompositePrimaryKey("capture_id", "target_id")
    capture = models.ForeignKey(WfCapture, on_delete=models.CASCADE,
                                db_column="capture_id", related_name="frames")
    target_id = models.TextField()
    yaw_deg = models.FloatField()
    pitch_deg = models.FloatField()
    roll_deg = models.FloatField()
    bytes = models.IntegerField()
    width = models.IntegerField()
    height = models.IntegerField()
    updated_at = models.TextField()

    class Meta:
        db_table = "wf_capture_frames"
