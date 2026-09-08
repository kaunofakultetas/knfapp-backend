############################################################
#  [*] wayfind — the indoor-map tables
#
#  Eight tables matching the production database
#  byte-for-byte. Two id schemes worth knowing before the
#  banners below: panoramas and plans are content-addressed
#  (the row id IS the sha256 of the stored bytes), while
#  ops and captures carry client-minted ids scoped
#  '<building>:<client id>' so idempotency holds per
#  building. Shape policy as in users/models.py.
#
#  Models:
#    - WfBuilding     — one building, draft + published revs
#    - WfEntity       — the draft, one row per map entity
#    - WfOp           — the edit-batch idempotency log
#    - WfVersion      — published documents, verbatim
#    - WfPanorama     — content-addressed 360° images
#    - WfPlan         — content-addressed floor-plan SVGs
#    - WfCapture      — phone capture sessions
#    - WfCaptureFrame — one frame per capture target
#
#  Changes to these models require running:
#    python3 manage.py makemigrations
#    python3 manage.py migrate
############################################################


from django.db import models


ENTITY_KINDS = ("level", "node", "edge", "room")
CAPTURE_MODES = ("full", "walls")
CAPTURE_STATUSES = ("uploading", "queued", "stitching", "done", "failed")








# -----------------------------------------------------------
# WfBuilding
# -----------------------------------------------------------
#
# The root row everything else hangs off: the permanent
# slug id, the display name, the plan's rotation against
# true north, and the two revision counters — the draft's
# (bumped by every applied op batch) and the published
# one (set by a publish).
#
# Table: wf_buildings
# -----------------------------------------------------------

class WfBuilding(models.Model):
    # Columns
    id = models.TextField(primary_key=True)
    name = models.TextField()
    north_deg = models.FloatField(null=True, blank=True)
    entrance_node_id = models.TextField(null=True, blank=True)
    draft_revision = models.IntegerField(default=0)
    published_revision = models.IntegerField(null=True, blank=True)
    created_at = models.TextField()
    updated_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "wf_buildings"








# -----------------------------------------------------------
# WfEntity
# -----------------------------------------------------------
#
# The draft: one row per map entity (level/node/edge/room),
# composite PK over building/kind/id, the entity's JSON in
# `data`. `deleted` is a tombstone flag so a `since` delta
# can carry deletions; `revision` stamps the draft revision
# that last touched the row.
#
# Table: wf_entities
# -----------------------------------------------------------

class WfEntity(models.Model):
    # Columns
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

    # Table metadata
    class Meta:
        db_table = "wf_entities"
        constraints = [
            models.CheckConstraint(condition=models.Q(kind__in=ENTITY_KINDS),
                                   name="wf_entities_kind_check"),
        ]
        indexes = [
            models.Index(fields=["building", "revision"], name="idx_wf_entities_revision"),
        ]








# -----------------------------------------------------------
# WfOp
# -----------------------------------------------------------
#
# The edit-batch idempotency log: rows keyed
# '<building>:<op id>' so a retried batch answers its
# recorded verdicts (applied/rejected + reason) instead of
# re-applying. The op's JSON rides in `op` verbatim.
#
# Table: wf_ops
# -----------------------------------------------------------

class WfOp(models.Model):
    # Columns
    id = models.TextField(primary_key=True)
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="ops")
    revision = models.IntegerField(null=True, blank=True)
    op = models.TextField()
    author_id = models.TextField(null=True, blank=True)
    created_at = models.TextField()
    status = models.TextField()
    reason = models.TextField(null=True, blank=True)

    # Table metadata
    class Meta:
        db_table = "wf_ops"
        constraints = [
            models.CheckConstraint(condition=models.Q(status__in=("applied", "rejected")),
                                   name="wf_ops_status_check"),
        ]
        indexes = [
            models.Index(fields=["building", "created_at"], name="idx_wf_ops_building"),
        ]








# -----------------------------------------------------------
# WfVersion
# -----------------------------------------------------------
#
# One row per publish, composite PK over building/revision.
# `document` holds the published map verbatim — its TEXT is
# byte-for-byte what the graph endpoint serves and what the
# ETag hashes, which is why it is stored, never re-derived.
#
# Table: wf_versions
# -----------------------------------------------------------

class WfVersion(models.Model):
    # Columns
    pk = models.CompositePrimaryKey("building_id", "revision")
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="versions")
    revision = models.IntegerField()
    document = models.TextField()
    etag = models.TextField()
    note = models.TextField(null=True, blank=True)
    published_by = models.TextField(null=True, blank=True)
    published_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "wf_versions"








# -----------------------------------------------------------
# WfPanorama
# -----------------------------------------------------------
#
# One row per stored 360° image, content-addressed: the id
# IS the sha256 of the JPEG bytes, so a re-upload of the
# same picture lands on the same row and the serving URL is
# immutable forever. The geometry columns carry what the
# viewer needs (fov, raw heading and where it came from).
#
# Table: wf_panoramas
# -----------------------------------------------------------

class WfPanorama(models.Model):
    # Columns
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

    # Table metadata
    class Meta:
        db_table = "wf_panoramas"








# -----------------------------------------------------------
# WfPlan
# -----------------------------------------------------------
#
# One row per stored floor-plan SVG, content-addressed like
# the panoramas — the id is the sha256 of the SANITISED
# bytes (scripts and handlers are stripped before hashing).
#
# Table: wf_plans
# -----------------------------------------------------------

class WfPlan(models.Model):
    # Columns
    id = models.TextField(primary_key=True)
    building = models.ForeignKey(WfBuilding, on_delete=models.CASCADE,
                                 db_column="building_id", related_name="plans")
    level_id = models.TextField(null=True, blank=True)
    bytes = models.IntegerField()
    uploaded_by = models.TextField(null=True, blank=True)
    created_at = models.TextField()

    # Table metadata
    class Meta:
        db_table = "wf_plans"








# -----------------------------------------------------------
# WfCapture
# -----------------------------------------------------------
#
# A phone capture session: ids scoped '<building>:<client
# id>' like the ops, the shot list as JSON in `targets`,
# and the uploading → queued → stitching → done/failed
# lifecycle the stitch worker walks. `pano_id` points at
# the finished panorama once stitching succeeds — loose on
# purpose, the capture record outlives a purged panorama.
#
# Table: wf_captures
# -----------------------------------------------------------

class WfCapture(models.Model):
    # Columns
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

    # Table metadata
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








# -----------------------------------------------------------
# WfCaptureFrame
# -----------------------------------------------------------
#
# One frame per capture target — named by ROLE (the target
# id from the capture's shot list), composite PK, so a
# re-shoot of the same target REPLACES the row instead of
# accumulating takes. Pose angles ride with the frame; the
# JPEG bytes live on disk next to the capture.
#
# Table: wf_capture_frames
# -----------------------------------------------------------

class WfCaptureFrame(models.Model):
    # Columns
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

    # Table metadata
    class Meta:
        db_table = "wf_capture_frames"
