############################################################
#  [*] Wayfind API — the building graph, its drafts,
#      panoramas and plans
#
#  Where the indoor map lives on the server. Students read
#  ONE published document per building (the mobile engine's
#  BuildingGraph JSON) through an ETag, so a phone that holds
#  the current one pays a 304 and nothing more; admins and
#  curators edit a DRAFT one entity at a time through an op
#  log (client-generated op ids, so a replayed batch after a
#  dropped connection applies once), and an admin publishes
#  the draft once the server-side validator finds no error.
#  Publishing snapshots the document into wf_versions; the
#  draft keeps its own revision counter, bumped once per
#  accepted batch, and every entity remembers the revision it
#  last changed in — that is what a client's `since` delta
#  and an op's `baseRevision` conflict check read.
#
#  Panoramas and plans are content-addressed: the stored file
#  is named by the sha256 of the bytes actually written (the
#  re-encoded JPEG, the sanitised SVG), served immutable, and
#  a second upload of the same picture answers the same id.
#  Panoramas do NOT go through the uploads app — that path
#  downsizes to 2048 px and a panorama needs its width.
#  Files live under UPLOAD_DIR/wayfind/{panoramas,plans}/.
#
#  Transactions: ATOMIC_REQUESTS carries every handler here
#  — each handler is one commit at request end. post_ops'
#  no-op touch of the building row takes SQLite's write
#  lock at that point, so overlapping batches serialise;
#  its batch rides an inner atomic() savepoint so
#  the catch-all 500 can answer JSON over a clean rollback.
#
#    GET    /api/wayfind/buildings                       — the buildings and what is published
#    POST   /api/wayfind/buildings                       — create one (admin)
#    GET    /api/wayfind/buildings/<id>/graph            — the published document (public, ETag)
#    GET    /api/wayfind/buildings/<id>/draft            — the draft, whole or since a revision (admin/curator)
#    POST   /api/wayfind/buildings/<id>/ops              — apply an op batch to the draft (admin/curator)
#    POST   /api/wayfind/buildings/<id>/publish          — validate and publish the draft (admin)
#    GET    /api/wayfind/buildings/<id>/versions         — the publish history (admin/curator)
#    POST   /api/wayfind/buildings/<id>/panoramas        — store a panorama (admin/curator)
#    GET    /api/wayfind/panoramas/<name>                — serve one (public, immutable)
#    POST   /api/wayfind/buildings/<id>/plans            — store a level's SVG plan (admin/curator)
#    GET    /api/wayfind/plans/<name>                    — serve one (public, immutable)
############################################################


import hashlib
import io
import json
import logging
import os
import re

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Length
from django.http import FileResponse, HttpResponse
from PIL import Image, ImageOps

from knfapp.common import ratelimit
from knfapp.common.db import q
from knfapp.common.http import get_json_object, json_error, json_response, require_methods
from knfapp.common.timestamps import as_aware, utc_now
from knfapp.uploads.gates import MAX_IMAGE_PIXELS as BOMB_GUARD_PIXELS
from knfapp.users.auth import require_role
from knfapp.wayfind.graph import (
    ENTITY_ID_RE,
    ENTITY_KINDS,
    _finite,
    compile_document,
    document_etag,
    document_text,
    entity_shape_error,
    validate_document,
)
from knfapp.wayfind.models import WfBuilding, WfEntity, WfOp, WfPanorama, WfPlan, WfVersion
from knfapp.wayfind.store import store_dir as _store_dir, write_once as _write_once
from knfapp.wayfind.svg import PlanRefused, sanitize_plan


logger = logging.getLogger(__name__)

EDITOR_ROLES = ("admin", "curator")

BUILDING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
STORED_NAME_RE = re.compile(r"^[0-9a-f]{64}\.(?:jpg|svg)\Z")

# One batch is one transaction; a phone replaying a long
# offline session sends several
MAX_OPS_PER_BATCH = 500

# A panorama keeps its width up to this; the sphere stage
# reads 4096-wide textures on older GPUs and the server may
# derive smaller renditions later
PANO_MAX_EDGE = 8192

# The real decode ceiling: Pillow refuses to open anything
# past TWICE the 30 MP bomb guard uploads/gates.py installs
# process-wide (between 1x and 2x it only warns), so 60 MP is
# the largest panorama this route can ever decode and the
# DecompressionBombError catch in upload_panorama is what
# actually delivers the 413. Derived, not copied, so the
# constant cannot drift from the guard; the explicit
# width*height comparison is the belt beside it.
PANO_MAX_PIXELS = 2 * BOMB_GUARD_PIXELS
PLAN_MAX_BYTES = 2 * 1024 * 1024

# What a served plan may do once a browser opens it directly:
# nothing — no script, no load of any kind, no framing (the
# ingress sets the same policy; this is the belt for a
# deployment without it). Inline style stays, so the drawing
# still draws
PLAN_CSP = "default-src 'none'; style-src 'unsafe-inline'; sandbox; frame-ancestors 'none'"








############################################################
# _load_building / _multipart
############################################################
#
# The wf_buildings row as a dict, or None — the id regex
# runs first so a stray path segment never reaches a query.
# _multipart parses a request's form + files whatever
# the verb: Django only populates request.POST/FILES for
# POST, and the capture frame upload is a multipart PUT.
############################################################

def _load_building(building_id):
    if not BUILDING_ID_RE.match(building_id or ""):
        return None
    return WfBuilding.objects.filter(id=building_id).values().first()


def _multipart(request):
    if request.method == "POST":
        return request.POST, request.FILES
    if request.content_type and request.content_type.startswith("multipart/"):
        try:
            return request.parse_file_upload(request.META, request)
        except Exception:
            logger.warning("Unparseable multipart body", exc_info=True)
    return {}, {}


# The Content-Length gate the upload routes run BEFORE the
# body is parsed: past DATA_UPLOAD_MAX_MEMORY_SIZE (the
# ingress carve-out for these routes matches it) the phone
# gets a JSON 413 it can render instead of the proxy's
# empty one. A missing or garbage header reads as 0 — the
# proxy keeps the last word on the real byte count
def _body_too_large(request):
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared <= settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
        return None
    return json_error(f"Upload too large. Max {settings.DATA_UPLOAD_MAX_MEMORY_SIZE // (1024 * 1024)} MB",
                      413, code="too_large")








############################################################
# list_buildings / create_building
############################################################
#
# The listing is public — the app works without login: every
# building with what a student can fetch, the published
# revision and its ETag (null until the first publish). The
# create is admin-only: {"id": "knf", "name": "…",
# "northDeg"?, "entranceNodeId"?} — a new empty draft; the
# id is the slug the app and every URL carry, and a taken id
# answers 409.
#
# Used by:
#   - GET: the Vite admin panel — Wayfind BuildingsTable
#     (the phone reads one building's graph, never the list)
#   - POST: the admin panel's NewBuilding dialog and the
#     phone map editor's first-run setup (services/
#     wayfindTransport.ts createBuilding, the seed import)
############################################################

@require_methods("GET")
def list_buildings(request):
    rows = q(
        """
        SELECT b.id, b.name, b.north_deg, b.published_revision, b.draft_revision,
               v.etag, v.published_at
        FROM wf_buildings b
        LEFT JOIN wf_versions v ON v.building_id = b.id AND v.revision = b.published_revision
        ORDER BY b.name
        """
    )

    return json_response({
        "buildings": [
            {
                "id": row["id"],
                "name": row["name"],
                "northDeg": row["north_deg"],
                "publishedRevision": row["published_revision"],
                "draftRevision": row["draft_revision"],
                "etag": row["etag"],
                "publishedAt": as_aware(row["published_at"]),
            }
            for row in rows
        ]
    })


@require_methods("POST")
@require_role("admin")
def create_building(request):
    body = get_json_object(request) or {}
    building_id = body.get("id")
    name = body.get("name")
    if not isinstance(building_id, str) or not BUILDING_ID_RE.match(building_id):
        return json_error("id must be a lowercase slug (a-z, 0-9, -)", 400, code="bad_id")
    if not isinstance(name, str) or not name.strip():
        return json_error("name is required", 400, code="bad_name")
    north = body.get("northDeg")
    # Finite, never a bool: the body parser takes Infinity, the
    # float column stores it, and the published document could
    # then not be written as JSON at all
    if north is not None and not _finite(north):
        return json_error("northDeg must be a number", 400, code="bad_north")
    entrance = body.get("entranceNodeId")
    if entrance is not None and not (isinstance(entrance, str) and ENTITY_ID_RE.match(entrance)):
        return json_error("entranceNodeId must be an id", 400, code="bad_entrance")

    if WfBuilding.objects.filter(id=building_id).exists():
        return json_error("A building with that id exists", 409, code="exists")

    now = utc_now()
    WfBuilding.objects.create(
        id=building_id, name=name.strip(), north_deg=north, entrance_node_id=entrance,
        draft_revision=0, published_revision=None, created_at=now, updated_at=now,
    )

    return json_response({"id": building_id, "name": name.strip(), "draftRevision": 0, "publishedRevision": None}, status=201)








############################################################
# get_graph
############################################################
#
# GET /api/wayfind/buildings/<building_id>/graph
#
# The published document, byte for byte as it was hashed at
# publish time, under its ETag. If-None-Match with that ETag
# answers 304 and no body; Cache-Control is no-cache so every
# client revalidates but never re-downloads what it holds.
# 404 until the building has been published once.
#
# Used by:
#   - mobile services/api/wayfind.ts fetchBuildingGraph — on
#     boot, on network restore and on foreground
############################################################

@require_methods("GET")
def get_graph(request, building_id):
    building = _load_building(building_id)
    if building is None or building["published_revision"] is None:
        return json_error("No published map for this building", 404, code="not_published")

    version = (WfVersion.objects.filter(building_id=building_id, revision=building["published_revision"])
               .values("document", "etag").first())
    if version is None:
        return json_error("No published map for this building", 404, code="not_published")

    etag = f'"{version["etag"]}"'
    if etag in [tag.strip() for tag in (request.headers.get("If-None-Match") or "").split(",")]:
        response = HttpResponse(status=304)
    else:
        response = HttpResponse(version["document"], status=200, content_type="application/json")
    response["ETag"] = etag
    response["Cache-Control"] = "no-cache"
    return response








############################################################
# get_draft
############################################################
#
# GET /api/wayfind/buildings/<building_id>/draft
# GET /api/wayfind/buildings/<building_id>/draft?since=<revision>
#
# Whole: the compiled draft document, its revision and the
# validator's issues (what a publish would refuse on). Since:
# only the entity rows that changed after that revision,
# deleted ones included, so a phone re-syncs a long edit
# session in one small answer.
#
# Used by:
#   - the admin editing screens in the app — on open and on
#     every restore
############################################################

@require_methods("GET")
@require_role(*EDITOR_ROLES)
def get_draft(request, building_id):
    building = _load_building(building_id)
    if building is None:
        return json_error("Unknown building", 404, code="not_found")

    since = request.GET.get("since")
    if since is not None:
        try:
            since_revision = int(since)
        except ValueError:
            return json_error("since must be an integer revision", 400, code="bad_since")
        rows = (WfEntity.objects.filter(building_id=building_id, revision__gt=since_revision)
                .order_by("revision", "kind", "id")
                .values("kind", "id", "data", "revision", "deleted", "updated_at", "updated_by"))
        return json_response({
            "revision": building["draft_revision"],
            "since": since_revision,
            "building": _building_fields(building),
            "entities": [
                {
                    "kind": row["kind"],
                    "id": row["id"],
                    "data": None if row["deleted"] else row["data"],
                    "revision": row["revision"],
                    "deleted": bool(row["deleted"]),
                    "updatedAt": as_aware(row["updated_at"]),
                    "updatedBy": row["updated_by"],
                }
                for row in rows
            ],
        })

    rows = list(WfEntity.objects.filter(building_id=building_id).values("kind", "id", "data", "revision", "deleted"))
    document = compile_document(building, rows)
    # Per-entity revisions ride along so an editor can stamp its
    # ops' baseRevision without a second round trip
    revisions = {f"{row['kind']}:{row['id']}": row["revision"] for row in rows if not row["deleted"]}
    return json_response({
        "revision": building["draft_revision"],
        "publishedRevision": building["published_revision"],
        "building": _building_fields(building),
        "document": document,
        "revisions": revisions,
        "issues": validate_document(document),
    })








############################################################
# post_ops / _apply_op
############################################################
#
# {"ops": [{"id": "<uuid>", "type": "upsert"|"delete"|
# "building", "kind": "node", "entityId": "n1", "data": {…},
# "baseRevision"?: 12}, …]}
#
# One batch, one transaction, one draft revision bump — and
# ONE answer per op, so a batch with a stale edit in it still
# lands the rest: applied, rejected (with the reason and, on
# a conflict, the entity as it stands), or duplicate (this op
# id was seen before IN THIS BUILDING; the answer says what
# the original did: `of` is its status, reason its reason,
# revision the revision it applied at). A conflict is an
# entity whose revision is past the op's baseRevision, or a
# `fresh` create that finds the entity alive (a tombstone it
# revives). An upsert must say what the phone's copy was —
# baseRevision, or `fresh: true` for a create the server
# never heard of: one that says neither is a blind overwrite
# in the making and refuses the WHOLE batch with 400 no_base
# before anything is applied, so a client that forgot the
# stamp learns at once. A baseRevision that is present but
# not an integer ("1", 1.0, true) rejects its op rather than
# reading as absent. A delete without baseRevision is a plain
# overwrite. "building" ops patch the building row. A
# delete is a tombstone: the row stays, marked, so a `since`
# delta can carry it. Batches serialise on the building
# row's write lock (the no-op touch), so two overlapping
# batches can never both pass the conflict check on one
# entity.
#
# The op log key is "<building>:<op id>", so an op id is
# idempotent PER BUILDING (a fixed seed id can bootstrap a
# second building) while the answer echoes the bare id.
#
# Used by:
#   - the admin editing screens — every drain of the offline
#     op log
############################################################

# A revision number as the wire must carry it: an integer,
# and not a bool (True would read as 1)
def _is_revision(value):
    return isinstance(value, int) and not isinstance(value, bool)


# An upsert's licence to write: a real integer baseRevision
# for the conflict check to anchor on, or the fresh mark of a
# create
def _stamped(op):
    return _is_revision(op.get("baseRevision")) or op.get("fresh") is True


@require_methods("POST")
@require_role(*EDITOR_ROLES)
@ratelimit.per_user("wayfind_ops", max_attempts=300)
def post_ops(request, building_id):
    building = _load_building(building_id)
    if building is None:
        return json_error("Unknown building", 404, code="not_found")

    body = get_json_object(request) or {}
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        return json_error("ops must be a non-empty list", 400, code="bad_batch")
    if len(ops) > MAX_OPS_PER_BATCH:
        return json_error(f"At most {MAX_OPS_PER_BATCH} ops per batch", 400, code="batch_too_large")


    # STEP 1: no blind write — an upsert must carry baseRevision
    # (the conflict check's anchor) or fresh (a create the
    # server never heard of). One bare op refuses the whole
    # batch before anything is applied or logged, naming the
    # op, so a client that forgot the stamp learns at once
    # instead of quietly replacing rows
    # ========================================================
    for op in ops:
        if isinstance(op, dict) and op.get("type") == "upsert" and not _stamped(op):
            return json_response({"error": "upsert needs baseRevision or fresh", "code": "no_base", "opId": op.get("id")},
                                 status=400)


    # STEP 2: take SQLite's write lock BEFORE reading any
    # entity row — the F() self-assignment is a no-op touch
    # of the building row that serialises overlapping
    # batches, so the conflict check below only ever sees
    # committed state. draft_revision is re-read under that
    # lock: the _load_building read above may predate a
    # batch that committed while this one queued
    # =====================================================
    author = request.user["id"]
    now = utc_now()
    results = []
    applied_any = False

    try:
        with transaction.atomic():
            WfBuilding.objects.filter(id=building_id).update(updated_at=F("updated_at"))
            building = WfBuilding.objects.filter(id=building_id).values("draft_revision").first()
            revision = building["draft_revision"] + 1


            # STEP 3: the whole batch inside the transaction — the
            # revision every applied op gets is the next draft
            # revision
            # ====================================================
            for op in ops:
                result = _apply_op(building_id, op, revision, author, now)
                results.append(result)
                applied_any = applied_any or result["status"] == "applied"


            # STEP 4: the bump only when something landed — a batch
            # of duplicates or rejections leaves the revision alone
            # =====================================================
            if applied_any:
                WfBuilding.objects.filter(id=building_id).update(draft_revision=revision, updated_at=now)
    except Exception:
        logger.error("Wayfind op batch failed for %s", building_id, exc_info=True)
        return json_error("The batch could not be applied", 500, code="batch_failed")

    return json_response({"revision": revision if applied_any else building["draft_revision"], "results": results})


def _apply_op(building_id, op, revision, author, now):
    # Order of refusals: shape, duplicate id, conflict; only a
    # clean op writes. The wf_ops row is written for applied
    # and rejected ops alike — the log is the audit trail —
    # but a duplicate id is answered from the log and writes
    # nothing
    if not isinstance(op, dict) or not isinstance(op.get("id"), str) or not op["id"]:
        return {"id": None, "status": "rejected", "reason": "op needs a string id"}
    op_id = op["id"]
    log_id = f"{building_id}:{op_id}"
    seen = WfOp.objects.filter(id=log_id).values("status", "reason", "revision").first()
    if seen is not None:
        # A replay after a lost answer: say what the original
        # did — of/reason, and for an applied op the revision,
        # so the editor can still re-stamp the entity
        return {"id": op_id, "status": "duplicate", "of": seen["status"], "reason": seen["reason"], "revision": seen["revision"]}

    kind = op.get("type")
    rejection = None
    current = None


    # STEP 1: a building patch — no entity, no conflict check
    # =======================================================
    if kind == "building":
        data = op.get("data")
        if not isinstance(data, dict):
            rejection = "data must be an object"
        else:
            changes = {}
            if "name" in data:
                if not isinstance(data["name"], str) or not data["name"].strip():
                    rejection = "name must be a non-empty string"
                else:
                    changes["name"] = data["name"].strip()
            if "northDeg" in data and rejection is None:
                if data["northDeg"] is not None and not _finite(data["northDeg"]):
                    rejection = "northDeg must be a number or null"
                else:
                    changes["north_deg"] = data["northDeg"]
            if "entranceNodeId" in data and rejection is None:
                if data["entranceNodeId"] is not None and not (isinstance(data["entranceNodeId"], str) and ENTITY_ID_RE.match(data["entranceNodeId"])):
                    rejection = "entranceNodeId must be an id or null"
                else:
                    changes["entrance_node_id"] = data["entranceNodeId"]
            if rejection is None and not changes:
                rejection = "nothing to change"
            if rejection is None:
                WfBuilding.objects.filter(id=building_id).update(**changes, updated_at=now)


    # STEP 2: an entity upsert or delete — shape, then the
    # conflict check against the row's revision
    # =====================================================
    elif kind in ("upsert", "delete"):
        entity_kind = op.get("kind")
        entity_id = op.get("entityId")
        if entity_kind not in ENTITY_KINDS:
            rejection = "kind must be level, node, edge or room"
        elif not isinstance(entity_id, str) or not ENTITY_ID_RE.match(entity_id):
            rejection = "entityId must be a short id (letters, digits, . _ : -)"
        elif "baseRevision" in op and not _is_revision(op["baseRevision"]):
            # A malformed stamp ("1", 1.0, true) must not quietly
            # read as "no stamp" — that is a blind overwrite
            # answered 'applied' (KNF-159)
            rejection = "baseRevision must be an integer revision"
        else:
            row = (WfEntity.objects.filter(building_id=building_id, kind=entity_kind, id=entity_id)
                   .values("data", "revision", "deleted").first())
            base = op.get("baseRevision")
            # A conflict: the row moved past the phone's copy — or
            # a `fresh` create finds a LIVE row, i.e. the phone's
            # "the server never heard of this" is wrong and the
            # create would clobber someone's entity (a tombstone
            # is fair game: the create revives it)
            stale = row is not None and base is not None and row["revision"] > base
            clobber = row is not None and base is None and op.get("fresh") is True and not row["deleted"]
            if stale or clobber:
                rejection = "conflict"
                current = {"data": None if row["deleted"] else row["data"], "revision": row["revision"], "deleted": bool(row["deleted"])}
            elif kind == "delete":
                if row is None:
                    rejection = "no such entity"
                else:
                    WfEntity.objects.filter(building_id=building_id, kind=entity_kind, id=entity_id).update(
                        deleted=1, revision=revision, updated_at=now, updated_by=author)
            else:
                data = op.get("data")
                shape = entity_shape_error(entity_kind, data)
                if shape is not None:
                    rejection = shape
                else:
                    stored = dict(data)
                    stored.pop("id", None)
                    # A one-row bulk_create for its update_conflicts:
                    # insert or replace on the composite PK — and the
                    # row always lands deleted False, so an upsert
                    # revives a tombstoned entity
                    WfEntity.objects.bulk_create(
                        [WfEntity(building_id=building_id, kind=entity_kind, id=entity_id,
                                  data=stored, revision=revision,
                                  updated_at=now, updated_by=author, deleted=False)],
                        update_conflicts=True,
                        unique_fields=["building_id", "kind", "id"],
                        update_fields=["data", "revision", "updated_at", "updated_by", "deleted"],
                    )
    else:
        rejection = "type must be upsert, delete or building"


    # STEP 3: the log row — the audit trail for applied and
    # rejected alike
    # =====================================================
    status = "rejected" if rejection else "applied"
    WfOp.objects.create(
        id=log_id, building_id=building_id, revision=revision if status == "applied" else None,
        op=json.dumps(op, ensure_ascii=False), author_id=author, created_at=now, status=status, reason=rejection,
    )
    result = {"id": op_id, "status": status}
    if rejection:
        result["reason"] = rejection
    if current is not None:
        result["current"] = current
    return result








############################################################
# publish_building / list_versions
############################################################
#
# The publish compiles the draft, runs the validator and
# refuses (422, the issues) on any error; otherwise
# snapshots the document as revision = the draft revision,
# stamped with revision and publishedAt, and points the
# building at it. Publishing a draft that has not changed
# since the last publish answers 409 — there is nothing new
# to hand out. The history lists newest first: revision,
# etag, note, who and when, and the document's size (the
# documents themselves stay on the server).
#
# Used by:
#   - the admin "Publish" action / publish sheet in the app
############################################################

@require_methods("POST")
@require_role("admin")
def publish_building(request, building_id):
    building = _load_building(building_id)
    if building is None:
        return json_error("Unknown building", 404, code="not_found")
    if building["published_revision"] == building["draft_revision"]:
        return json_error("Nothing changed since the last publish", 409, code="unchanged")
    body = get_json_object(request) or {}
    note = body.get("note") if isinstance(body.get("note"), str) else None


    # STEP 1: compile and validate — an error is a refusal
    # ====================================================
    rows = list(WfEntity.objects.filter(building_id=building_id).values("kind", "id", "data", "revision", "deleted"))
    document = compile_document(building, rows)
    issues = validate_document(document)
    if issues:
        return json_response({"error": "The draft has errors", "code": "invalid", "issues": issues}, status=422)


    # STEP 2: the snapshot, stamped, hashed and pointed at
    # ====================================================
    revision = building["draft_revision"]
    now = utc_now()
    document["revision"] = revision
    # The document is a TEXT artifact — its publishedAt is the
    # aware wire string, byte-for-byte what the ETag hashes
    document["publishedAt"] = now.isoformat()
    text = document_text(document)
    etag = document_etag(text)
    # A one-row bulk_create for its update_conflicts: should
    # this revision number ever be published again, the new
    # snapshot replaces the old row on the composite PK
    # instead of erroring
    WfVersion.objects.bulk_create(
        [WfVersion(building_id=building_id, revision=revision, document=text, etag=etag,
                   note=note, published_by=request.user["id"], published_at=now)],
        update_conflicts=True,
        unique_fields=["building_id", "revision"],
        update_fields=["document", "etag", "note", "published_by", "published_at"],
    )
    WfBuilding.objects.filter(id=building_id).update(published_revision=revision, updated_at=now)

    return json_response({"revision": revision, "etag": etag, "publishedAt": now, "bytes": len(text.encode("utf-8"))})


@require_methods("GET")
@require_role(*EDITOR_ROLES)
def list_versions(request, building_id):
    building = _load_building(building_id)
    if building is None:
        return json_error("Unknown building", 404, code="not_found")

    rows = (WfVersion.objects.filter(building_id=building_id).order_by("-revision")
            .annotate(bytes=Length("document"))
            .values("revision", "etag", "note", "published_by", "published_at", "bytes"))
    return json_response({
        "publishedRevision": building["published_revision"],
        "versions": [
            {"revision": row["revision"], "etag": row["etag"], "note": row["note"],
             "publishedBy": row["published_by"], "publishedAt": as_aware(row["published_at"]), "bytes": row["bytes"]}
            for row in rows
        ],
    })








############################################################
# upload_panorama
############################################################
#
# POST /api/wayfind/buildings/<building_id>/panoramas
#
# Multipart: file (jpg / png / webp), and optional fields
# nodeId, hfovDeg, vfovDeg, headingRawDeg, headingSource.
# The picture is verified, turned upright by its orientation
# tag, refused past PANO_MAX_PIXELS (60 MP — Pillow's hard
# bomb stop, a 413 too_large), capped at 8192 px on its long
# edge and re-encoded as a progressive JPEG with no metadata
# — so the stored file describes itself and the sha256 of
# THOSE bytes is its id. The same picture uploaded twice
# answers the same id. The coverage defaults to a full turn
# with the vertical band the aspect gives (a 2:1 photo is a
# whole sphere). A body declared past
# DATA_UPLOAD_MAX_MEMORY_SIZE (52 MB, the ingress carve-out)
# is refused as a JSON 413 too_large before it is read.
#
# Used by:
#   - the admin capture / import screens in the app
############################################################

@require_methods("POST")
@require_role(*EDITOR_ROLES)
@ratelimit.per_user("wayfind_upload", max_attempts=120)
def upload_panorama(request, building_id):
    building = _load_building(building_id)
    if building is None:
        return json_error("Unknown building", 404, code="not_found")
    too_large = _body_too_large(request)
    if too_large:
        return too_large
    form, files = _multipart(request)
    upload = files.get("file")
    if upload is None:
        return json_error("file is required", 400, code="no_file")


    # STEP 1: decode, verify and re-encode — the re-encode is
    # the gate, exactly as in the uploads app, only without
    # the 2048 px cap a panorama cannot live with. Pillow
    # raises DecompressionBombError inside open() for anything
    # past PANO_MAX_PIXELS, so the oversize refusal is the
    # catch, not the comparison
    # =======================================================
    raw = upload.read()
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > PANO_MAX_PIXELS:
            return json_error(f"Image too large. Max {PANO_MAX_PIXELS // (1000 * 1000)} megapixels", 413, code="too_large")
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
        if max(image.width, image.height) > PANO_MAX_EDGE:
            scale = PANO_MAX_EDGE / max(image.width, image.height)
            image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=85, optimize=True, progressive=True)
        blob = out.getvalue()
        width, height = image.width, image.height
    except Image.DecompressionBombError:
        return json_error(f"Image too large. Max {PANO_MAX_PIXELS // (1000 * 1000)} megapixels", 413, code="too_large")
    except Exception:
        return json_error("Not a readable image", 400, code="bad_image")


    # STEP 2: the coverage — the author's word, else the aspect
    # =========================================================
    hfov = _float_field(form, "hfovDeg", 360.0)
    vfov = _float_field(form, "vfovDeg", min(180.0, 360.0 * height / width))
    heading_raw = _float_field(form, "headingRawDeg", None)
    heading_source = form.get("headingSource")
    if hfov is False or vfov is False or heading_raw is False:
        return json_error("hfovDeg, vfovDeg and headingRawDeg must be numbers", 400, code="bad_geometry")
    hfov = min(360.0, max(1.0, hfov))
    vfov = min(180.0, max(1.0, vfov))
    node_id = form.get("nodeId")
    if node_id is not None and not ENTITY_ID_RE.match(node_id):
        return json_error("nodeId must be a short id", 400, code="bad_node")


    # STEP 3: store by content hash and record it once
    # ================================================
    digest = hashlib.sha256(blob).hexdigest()
    name = f"{digest}.jpg"
    if not _write_once(_store_dir("panoramas"), name, blob):
        return json_error("The panorama could not be stored", 500, code="write_failed")
    # A one-row bulk_create for its ignore_conflicts: the row
    # is content-addressed, so a re-upload of the same picture
    # must keep the first record untouched
    WfPanorama.objects.bulk_create([WfPanorama(
        id=digest, building_id=building_id, node_id=node_id, width=width, height=height, bytes=len(blob),
        hfov_deg=hfov, vfov_deg=vfov, heading_raw_deg=heading_raw, heading_source=heading_source,
        uploaded_by=request.user["id"], created_at=utc_now(),
    )], ignore_conflicts=True)

    return json_response({
        "id": digest,
        "url": f"/api/wayfind/panoramas/{name}",
        "width": width,
        "height": height,
        "bytes": len(blob),
        "hfovDeg": hfov,
        "vfovDeg": vfov,
    }, status=201)








############################################################
# serve_panorama / serve_plan
############################################################
#
# A stored file by its hashed name, public and immutable: the
# name IS the content, so a year of caching is safe and a
# changed picture is a different url. Anything but a 64-hex
# name with the right extension is a 404 before the disk is
# touched. Both answer nosniff; a plan also carries PLAN_CSP,
# so even a drawing opened straight in a browser runs and
# loads nothing. (FileResponse serves no Range requests — the
# app's image loaders never send one for a jpg/svg, and the
# immutable cache header makes revalidation moot.) Neither
# touches the database, so neither runs inside the
# ATOMIC_REQUESTS transaction (KNF-135): a picture GET opens
# no connection for an empty BEGIN/COMMIT.
#
# Used by:
#   - the app's panorama stage and floor plan
############################################################

@transaction.non_atomic_requests
@require_methods("GET")
def serve_panorama(request, name):
    if not STORED_NAME_RE.match(name or "") or not name.endswith(".jpg"):
        return json_error("Not found", 404, code="not_found")
    path = os.path.join(_store_dir("panoramas"), name)
    if not os.path.isfile(path):
        return json_error("Not found", 404, code="not_found")
    response = FileResponse(open(path, "rb"), content_type="image/jpeg")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@transaction.non_atomic_requests
@require_methods("GET")
def serve_plan(request, name):
    if not STORED_NAME_RE.match(name or "") or not name.endswith(".svg"):
        return json_error("Not found", 404, code="not_found")
    path = os.path.join(_store_dir("plans"), name)
    if not os.path.isfile(path):
        return json_error("Not found", 404, code="not_found")
    response = FileResponse(open(path, "rb"), content_type="image/svg+xml")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = PLAN_CSP
    return response








############################################################
# upload_plan
############################################################
#
# POST /api/wayfind/buildings/<building_id>/plans
#
# Multipart: file (an SVG drawing, 2 MB), optional levelId.
# The bytes must be UTF-8 and parse as an <svg> document;
# svg.py's sanitize_plan then REBUILDS the drawing from an
# element / attribute allowlist (no script, no handler, no
# foreign object, no animation, no external reference) and
# the sha256 of that rebuilt text names the file. A drawing
# that will not parse, is not SVG or declares entities is
# refused 400 bad_plan with the reason — never repaired.
# Answers the id and the relative url a level's `plan` field
# stores.
#
# Used by:
#   - the admin level sheet in the app; the seed import
############################################################

@require_methods("POST")
@require_role(*EDITOR_ROLES)
@ratelimit.per_user("wayfind_upload", max_attempts=120)
def upload_plan(request, building_id):
    building = _load_building(building_id)
    if building is None:
        return json_error("Unknown building", 404, code="not_found")
    form, files = _multipart(request)
    upload = files.get("file")
    if upload is None:
        return json_error("file is required", 400, code="no_file")
    raw = upload.read()
    if len(raw) > PLAN_MAX_BYTES:
        return json_error("Plan too large. Max 2 MB", 413, code="too_large")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return json_error("Plan must be UTF-8 SVG text", 400, code="bad_plan")
    level_id = form.get("levelId")
    if level_id is not None and not ENTITY_ID_RE.match(level_id):
        return json_error("levelId must be a short id", 400, code="bad_level")

    # Parse and rebuild — the stored drawing is the allowlist's
    # copy, never the upload
    try:
        clean = sanitize_plan(raw)
    except PlanRefused as refusal:
        return json_error(str(refusal), 400, code="bad_plan")
    blob = clean.encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    name = f"{digest}.svg"
    if not _write_once(_store_dir("plans"), name, blob):
        return json_error("The plan could not be stored", 500, code="write_failed")
    # A one-row bulk_create for its ignore_conflicts: the row
    # is content-addressed, so a re-upload of the same drawing
    # must keep the first record untouched
    WfPlan.objects.bulk_create([WfPlan(
        id=digest, building_id=building_id, level_id=level_id, bytes=len(blob),
        uploaded_by=request.user["id"], created_at=utc_now(),
    )], ignore_conflicts=True)

    return json_response({"id": digest, "url": f"/api/wayfind/plans/{name}", "bytes": len(blob)}, status=201)








############################################################
# _building_fields / _float_field
############################################################
#
# The building row as the app spells it, and a form field as
# a float: the default when absent, False when present but
# not a number (so a caller can refuse rather than guess).
############################################################

def _building_fields(building):
    return {"id": building["id"], "name": building["name"], "northDeg": building["north_deg"], "entranceNodeId": building["entrance_node_id"]}


def _float_field(form, name, default):
    value = form.get(name)
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError:
        return False
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else False
