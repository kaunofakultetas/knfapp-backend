############################################################
#  [*] Wayfind — the building graph, its drafts, panoramas
#      and plans
#
#  Where the indoor map lives on the server. Students read
#  ONE published document per building (the engine's
#  BuildingGraph JSON) through an ETag, so a phone that holds
#  the current one pays a 304 and nothing more; admins and
#  curators edit a DRAFT one entity at a time through an op
#  log (client-generated op ids, so a replayed batch after a
#  dropped connection applies once), and an admin publishes
#  the draft once the server-side validator finds no error.
#  Publishing snapshots the document into wf_versions; the
#  draft keeps its own revision counter, bumped once per
#  accepted batch, and every entity remembers the revision it
#  last changed in — that is what a client's `since` delta and
#  an op's `baseRevision` conflict check read.
#
#  Panoramas and plans are content-addressed: the stored file
#  is named by the sha256 of the bytes actually written (the
#  re-encoded JPEG, the sanitised SVG), served immutable, and
#  a second upload of the same picture answers the same id.
#  Panoramas do NOT go through uploads/routes.py — that route
#  downsizes to 2048 px and a panorama needs its width — but
#  they inherit the app-wide 6 MB body cap (MAX_CONTENT_LENGTH
#  and the ingress's /api/* limit); a larger ingress limit for
#  /api/wayfind/panoramas* is a deploy-side change.
#
#  Files live under UPLOAD_DIR/wayfind/{panoramas,plans}/.
#  Tables: wf_buildings, wf_entities, wf_ops, wf_versions,
#  wf_panoramas, wf_plans (migration v64). Every route is in
#  swagger/swagger.yaml.
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
import uuid

from flask import Blueprint, Response, jsonify, request, send_from_directory
from PIL import Image, ImageOps

from app.auth.routes import rate_limit, require_role
from app.database import get_db, utc_now_iso
from app.uploads.routes import MAX_IMAGE_PIXELS as BOMB_GUARD_PIXELS
# The store primitives moved to store.py so stitch.py can
# write through the exact same content-hash path; the aliases
# keep every call site below unchanged
from app.wayfind.store import store_dir as _store_dir, write_once as _write_once
from app.wayfind.graph import (
    ENTITY_KINDS,
    compile_document,
    document_etag,
    document_text,
    entity_shape_error,
    validate_document,
)


logger = logging.getLogger(__name__)

wayfind_bp = Blueprint("wayfind", __name__)

EDITOR_ROLES = ("admin", "curator")

BUILDING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
STORED_NAME_RE = re.compile(r"^[0-9a-f]{64}\.(?:jpg|svg)\Z")

# One batch is one transaction; a phone replaying a long
# offline session sends several
MAX_OPS_PER_BATCH = 500

# A panorama keeps its width up to this; the sphere stage
# reads 4096-wide textures on older GPUs and the server may
# derive smaller renditions later
PANO_MAX_EDGE = 8192

# The real decode ceiling: Pillow refuses to open anything
# past TWICE the 30 MP bomb guard uploads/routes.py installs
# process-wide (between 1x and 2x it only warns), so 60 MP is
# the largest panorama this route can ever decode and the
# DecompressionBombError catch in upload_panorama is what
# actually delivers the 413. Derived, not copied, so the
# constant cannot drift from the guard; the explicit
# width*height comparison stays as the belt for the day the
# guard moves.
PANO_MAX_PIXELS = 2 * BOMB_GUARD_PIXELS
PLAN_MAX_BYTES = 2 * 1024 * 1024

# An SVG plan is drawn, never run: scripts, event handlers
# and script URLs are cut before the bytes are hashed
SVG_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>|<script\b[^>]*/>", re.IGNORECASE | re.DOTALL)
SVG_HANDLER_RE = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*')", re.IGNORECASE)
SVG_JS_HREF_RE = re.compile(r"(\s(?:xlink:)?href\s*=\s*)([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)









############################################################
# _load_building
############################################################
#
# The wf_buildings row for a validated id, or None — the id
# regex runs first so a stray path segment never reaches SQL.
#
# Used by:
#   - every /buildings/<id>/… route below
############################################################

def _load_building(building_id: str):
    if not BUILDING_ID_RE.match(building_id or ""):
        return None
    return get_db().execute("SELECT * FROM wf_buildings WHERE id = ?", (building_id,)).fetchone()









############################################################
# list_buildings
############################################################
#
# GET /api/wayfind/buildings
#
# Every building with what a student can fetch: the published
# revision and its ETag (null until the first publish). Public
# — the app works without login.
#
# Used by:
#   - mobile services/api/wayfind.ts fetchBuildings
############################################################

@wayfind_bp.route("/buildings", methods=["GET"])
def list_buildings():

    rows = get_db().execute(
        """
        SELECT b.id, b.name, b.north_deg, b.published_revision, b.draft_revision,
               v.etag, v.published_at
        FROM wf_buildings b
        LEFT JOIN wf_versions v ON v.building_id = b.id AND v.revision = b.published_revision
        ORDER BY b.name
        """
    ).fetchall()

    return jsonify({
        "buildings": [
            {
                "id": row["id"],
                "name": row["name"],
                "northDeg": row["north_deg"],
                "publishedRevision": row["published_revision"],
                "draftRevision": row["draft_revision"],
                "etag": row["etag"],
                "publishedAt": row["published_at"],
            }
            for row in rows
        ]
    })









############################################################
# create_building
############################################################
#
# POST /api/wayfind/buildings
#
# {"id": "knf", "name": "…", "northDeg"?: 12, "entranceNodeId"?: "…"}
# — a new empty draft. The id is the slug the app and every
# URL carry; a taken id answers 409.
#
# Used by:
#   - the admin's first-run setup in the app (later); the seed
#     script that imports the bundled graph
############################################################

@wayfind_bp.route("/buildings", methods=["POST"])
@require_role("admin")
def create_building():

    body = request.get_json(silent=True) or {}
    building_id = body.get("id")
    name = body.get("name")
    if not isinstance(building_id, str) or not BUILDING_ID_RE.match(building_id):
        return jsonify({"error": "id must be a lowercase slug (a-z, 0-9, -)", "code": "bad_id"}), 400
    if not isinstance(name, str) or not name.strip():
        return jsonify({"error": "name is required", "code": "bad_name"}), 400
    north = body.get("northDeg")
    if north is not None and not isinstance(north, (int, float)):
        return jsonify({"error": "northDeg must be a number", "code": "bad_north"}), 400

    db = get_db()
    if db.execute("SELECT 1 FROM wf_buildings WHERE id = ?", (building_id,)).fetchone():
        return jsonify({"error": "A building with that id exists", "code": "exists"}), 409

    now = utc_now_iso()
    db.execute(
        "INSERT INTO wf_buildings (id, name, north_deg, entrance_node_id, draft_revision, published_revision, created_at, updated_at) VALUES (?, ?, ?, ?, 0, NULL, ?, ?)",
        (building_id, name.strip(), north, body.get("entranceNodeId"), now, now),
    )
    db.commit()

    return jsonify({"id": building_id, "name": name.strip(), "draftRevision": 0, "publishedRevision": None}), 201









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

@wayfind_bp.route("/buildings/<building_id>/graph", methods=["GET"])
def get_graph(building_id):

    building = _load_building(building_id)
    if building is None or building["published_revision"] is None:
        return jsonify({"error": "No published map for this building", "code": "not_published"}), 404

    version = get_db().execute(
        "SELECT document, etag FROM wf_versions WHERE building_id = ? AND revision = ?",
        (building_id, building["published_revision"]),
    ).fetchone()
    if version is None:
        return jsonify({"error": "No published map for this building", "code": "not_published"}), 404

    etag = f'"{version["etag"]}"'
    if etag in [tag.strip() for tag in (request.headers.get("If-None-Match") or "").split(",")]:
        response = Response(status=304)
    else:
        response = Response(version["document"], status=200, mimetype="application/json")
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
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
#   - the admin editing screens in the app (wayfindsync,
#     later) — on open and on every restore
############################################################

@wayfind_bp.route("/buildings/<building_id>/draft", methods=["GET"])
@require_role(*EDITOR_ROLES)
def get_draft(building_id):

    building = _load_building(building_id)
    if building is None:
        return jsonify({"error": "Unknown building", "code": "not_found"}), 404
    db = get_db()


    since = request.args.get("since")
    if since is not None:
        try:
            since_revision = int(since)
        except ValueError:
            return jsonify({"error": "since must be an integer revision", "code": "bad_since"}), 400
        rows = db.execute(
            "SELECT kind, id, data, revision, deleted, updated_at, updated_by FROM wf_entities WHERE building_id = ? AND revision > ? ORDER BY revision, kind, id",
            (building_id, since_revision),
        ).fetchall()
        return jsonify({
            "revision": building["draft_revision"],
            "since": since_revision,
            "building": _building_fields(building),
            "entities": [
                {
                    "kind": row["kind"],
                    "id": row["id"],
                    "data": None if row["deleted"] else json.loads(row["data"]),
                    "revision": row["revision"],
                    "deleted": bool(row["deleted"]),
                    "updatedAt": row["updated_at"],
                    "updatedBy": row["updated_by"],
                }
                for row in rows
            ],
        })


    rows = db.execute("SELECT kind, id, data, revision, deleted FROM wf_entities WHERE building_id = ?", (building_id,)).fetchall()
    document = compile_document(building, rows)
    # Per-entity revisions ride along so an editor can stamp its
    # ops' baseRevision without a second round trip
    revisions = {f"{row['kind']}:{row['id']}": row["revision"] for row in rows if not row["deleted"]}
    return jsonify({
        "revision": building["draft_revision"],
        "publishedRevision": building["published_revision"],
        "building": _building_fields(building),
        "document": document,
        "revisions": revisions,
        "issues": validate_document(document),
    })









############################################################
# post_ops
############################################################
#
# POST /api/wayfind/buildings/<building_id>/ops
#
# {"ops": [{"id": "<uuid>", "type": "upsert"|"delete"|"building",
#           "kind": "node", "entityId": "n1", "data": {…},
#           "baseRevision"?: 12}, …]}
#
# One batch, one transaction, one draft revision bump — and
# ONE answer per op, so a batch with a stale edit in it still
# lands the rest: applied, rejected (with the reason and, on
# a conflict, the entity as it stands), or duplicate (this op
# id was seen before IN THIS BUILDING; nothing re-applied,
# and the answer says what the original did: of is its
# status, reason its reason, revision the revision it
# applied at — null for a rejected one). A conflict is an
# entity whose revision is past the op's baseRevision — the
# phone edited a copy someone else had already changed. An
# op without baseRevision is a plain overwrite. "building"
# ops patch the building row (name, northDeg, entranceNodeId).
# A delete is a tombstone: the row stays, marked, so a
# `since` delta can carry it. Batches serialise on the
# building row's write lock, so two overlapping batches can
# never both pass the conflict check on one entity.
#
# Used by:
#   - the admin editing screens in the app (wayfindsync,
#     later) — every drain of the offline op log
############################################################

@wayfind_bp.route("/buildings/<building_id>/ops", methods=["POST"])
@require_role(*EDITOR_ROLES)
@rate_limit("wayfind_ops", max_attempts=300)
def post_ops(building_id):

    building = _load_building(building_id)
    if building is None:
        return jsonify({"error": "Unknown building", "code": "not_found"}), 404

    body = request.get_json(silent=True) or {}
    ops = body.get("ops")
    if not isinstance(ops, list) or not ops:
        return jsonify({"error": "ops must be a non-empty list", "code": "bad_batch"}), 400
    if len(ops) > MAX_OPS_PER_BATCH:
        return jsonify({"error": f"At most {MAX_OPS_PER_BATCH} ops per batch", "code": "batch_too_large"}), 400


    # STEP 1: take SQLite's write lock BEFORE reading any
    # entity row — a no-op touch of the building row serialises
    # overlapping batches, so the conflict check below only
    # ever sees committed state. draft_revision is re-read
    # under that lock: the _load_building read above may
    # predate a batch that committed while this one queued
    # ========================================================
    db = get_db()
    author = request.user["id"]
    now = utc_now_iso()
    results = []
    applied_any = False

    try:
        db.execute("UPDATE wf_buildings SET updated_at = updated_at WHERE id = ?", (building_id,))
        building = db.execute("SELECT draft_revision FROM wf_buildings WHERE id = ?", (building_id,)).fetchone()
        revision = building["draft_revision"] + 1


        # STEP 2: the whole batch inside the transaction — the
        # revision every applied op gets is the next draft
        # revision
        # ======================================================
        for op in ops:
            result = _apply_op(db, building_id, op, revision, author, now)
            results.append(result)
            applied_any = applied_any or result["status"] == "applied"


        # STEP 3: the bump only when something landed — a batch of
        # duplicates or rejections leaves the revision alone
        # ========================================================
        if applied_any:
            db.execute("UPDATE wf_buildings SET draft_revision = ?, updated_at = ? WHERE id = ?", (revision, now, building_id))
        db.commit()
    except Exception:
        db.rollback()
        logger.error("Wayfind op batch failed for %s", building_id, exc_info=True)
        return jsonify({"error": "The batch could not be applied", "code": "batch_failed"}), 500

    return jsonify({"revision": revision if applied_any else building["draft_revision"], "results": results})









############################################################
# _apply_op
############################################################
#
# One op against the draft rows, answering its result row.
# Order of refusals: shape, duplicate id, conflict; only a
# clean op writes. The wf_ops row is written for applied and
# rejected ops alike — the log is the audit trail — but a
# duplicate id is answered from the log and writes nothing.
# The log key is "<building>:<op id>", so an op id is
# idempotent PER BUILDING (a fixed seed id can bootstrap a
# second building) while the answer echoes the bare id the
# phone sent; the duplicate answer carries of (the logged
# status), reason and revision so a phone that never saw the
# original answer still learns what happened.
#
# Used by:
#   - post_ops (above)
############################################################

def _apply_op(db, building_id, op, revision, author, now):

    if not isinstance(op, dict) or not isinstance(op.get("id"), str) or not op["id"]:
        return {"id": None, "status": "rejected", "reason": "op needs a string id"}
    op_id = op["id"]
    log_id = f"{building_id}:{op_id}"
    seen = db.execute("SELECT status, reason, revision FROM wf_ops WHERE id = ?", (log_id,)).fetchone()
    if seen is not None:
        # A replay after a lost answer: say what the original
        # did — of/reason, and for an applied op the revision,
        # so the editor can still re-stamp the entity
        return {"id": op_id, "status": "duplicate", "of": seen["status"], "reason": seen["reason"], "revision": seen["revision"]}

    kind = op.get("type")
    rejection = None
    current = None


    # STEP 1: a building patch — no entity, no conflict check
    # ========================================================
    if kind == "building":
        data = op.get("data")
        if not isinstance(data, dict):
            rejection = "data must be an object"
        else:
            fields = []
            values = []
            if "name" in data:
                if not isinstance(data["name"], str) or not data["name"].strip():
                    rejection = "name must be a non-empty string"
                else:
                    fields.append("name = ?")
                    values.append(data["name"].strip())
            if "northDeg" in data and rejection is None:
                if data["northDeg"] is not None and not isinstance(data["northDeg"], (int, float)):
                    rejection = "northDeg must be a number or null"
                else:
                    fields.append("north_deg = ?")
                    values.append(data["northDeg"])
            if "entranceNodeId" in data and rejection is None:
                if data["entranceNodeId"] is not None and not (isinstance(data["entranceNodeId"], str) and ENTITY_ID_RE.match(data["entranceNodeId"])):
                    rejection = "entranceNodeId must be an id or null"
                else:
                    fields.append("entrance_node_id = ?")
                    values.append(data["entranceNodeId"])
            if rejection is None and not fields:
                rejection = "nothing to change"
            if rejection is None:
                db.execute(f"UPDATE wf_buildings SET {', '.join(fields)}, updated_at = ? WHERE id = ?", (*values, now, building_id))


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
        else:
            row = db.execute(
                "SELECT data, revision, deleted FROM wf_entities WHERE building_id = ? AND kind = ? AND id = ?",
                (building_id, entity_kind, entity_id),
            ).fetchone()
            base = op.get("baseRevision")
            if row is not None and isinstance(base, int) and row["revision"] > base:
                rejection = "conflict"
                current = {"data": None if row["deleted"] else json.loads(row["data"]), "revision": row["revision"], "deleted": bool(row["deleted"])}
            elif kind == "delete":
                if row is None:
                    rejection = "no such entity"
                else:
                    db.execute(
                        "UPDATE wf_entities SET deleted = 1, revision = ?, updated_at = ?, updated_by = ? WHERE building_id = ? AND kind = ? AND id = ?",
                        (revision, now, author, building_id, entity_kind, entity_id),
                    )
            else:
                data = op.get("data")
                shape = entity_shape_error(entity_kind, data)
                if shape is not None:
                    rejection = shape
                else:
                    stored = dict(data)
                    stored.pop("id", None)
                    db.execute(
                        """
                        INSERT INTO wf_entities (building_id, kind, id, data, revision, updated_at, updated_by, deleted)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                        ON CONFLICT(building_id, kind, id) DO UPDATE SET
                            data = excluded.data, revision = excluded.revision,
                            updated_at = excluded.updated_at, updated_by = excluded.updated_by, deleted = 0
                        """,
                        (building_id, entity_kind, entity_id, json.dumps(stored, ensure_ascii=False), revision, now, author),
                    )
    else:
        rejection = "type must be upsert, delete or building"


    # STEP 3: the log row — the audit trail for applied and
    # rejected alike
    # =====================================================
    status = "rejected" if rejection else "applied"
    db.execute(
        "INSERT INTO wf_ops (id, building_id, revision, op, author_id, created_at, status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (log_id, building_id, revision if status == "applied" else None, json.dumps(op, ensure_ascii=False), author, now, status, rejection),
    )
    result = {"id": op_id, "status": status}
    if rejection:
        result["reason"] = rejection
    if current is not None:
        result["current"] = current
    return result









############################################################
# publish_building
############################################################
#
# POST /api/wayfind/buildings/<building_id>/publish
#
# {"note"?: "…"} — compiles the draft, runs the validator and
# refuses (422, the issues) on any error; otherwise snapshots
# the document as revision = the draft revision, stamped with
# revision and publishedAt, and points the building at it.
# Publishing a draft that has not changed since the last
# publish answers 409 — there is nothing new to hand out.
#
# Used by:
#   - the admin "Publish" action in the app (later)
############################################################

@wayfind_bp.route("/buildings/<building_id>/publish", methods=["POST"])
@require_role("admin")
def publish_building(building_id):

    building = _load_building(building_id)
    if building is None:
        return jsonify({"error": "Unknown building", "code": "not_found"}), 404
    if building["published_revision"] == building["draft_revision"]:
        return jsonify({"error": "Nothing changed since the last publish", "code": "unchanged"}), 409
    body = request.get_json(silent=True) or {}
    note = body.get("note") if isinstance(body.get("note"), str) else None


    # STEP 1: compile and validate — an error is a refusal
    # ====================================================
    db = get_db()
    rows = db.execute("SELECT kind, id, data, revision, deleted FROM wf_entities WHERE building_id = ?", (building_id,)).fetchall()
    document = compile_document(building, rows)
    issues = validate_document(document)
    if issues:
        return jsonify({"error": "The draft has errors", "code": "invalid", "issues": issues}), 422


    # STEP 2: the snapshot, stamped, hashed and pointed at
    # ====================================================
    revision = building["draft_revision"]
    now = utc_now_iso()
    document["revision"] = revision
    document["publishedAt"] = now
    text = document_text(document)
    etag = document_etag(text)
    db.execute(
        "INSERT OR REPLACE INTO wf_versions (building_id, revision, document, etag, note, published_by, published_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (building_id, revision, text, etag, note, request.user["id"], now),
    )
    db.execute("UPDATE wf_buildings SET published_revision = ?, updated_at = ? WHERE id = ?", (revision, now, building_id))
    db.commit()

    return jsonify({"revision": revision, "etag": etag, "publishedAt": now, "bytes": len(text.encode("utf-8"))})









############################################################
# list_versions
############################################################
#
# GET /api/wayfind/buildings/<building_id>/versions
#
# The publish history, newest first: revision, etag, note,
# who and when, and the document's size. The documents
# themselves stay on the server.
#
# Used by:
#   - the admin's publish sheet in the app (later)
############################################################

@wayfind_bp.route("/buildings/<building_id>/versions", methods=["GET"])
@require_role(*EDITOR_ROLES)
def list_versions(building_id):

    building = _load_building(building_id)
    if building is None:
        return jsonify({"error": "Unknown building", "code": "not_found"}), 404

    rows = get_db().execute(
        "SELECT revision, etag, note, published_by, published_at, LENGTH(document) AS bytes FROM wf_versions WHERE building_id = ? ORDER BY revision DESC",
        (building_id,),
    ).fetchall()
    return jsonify({
        "publishedRevision": building["published_revision"],
        "versions": [
            {"revision": row["revision"], "etag": row["etag"], "note": row["note"], "publishedBy": row["published_by"], "publishedAt": row["published_at"], "bytes": row["bytes"]}
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
# — so the stored file
# describes itself and the sha256 of THOSE bytes is its id.
# The same picture uploaded twice answers the same id. The
# coverage defaults to a full turn with the vertical band the
# aspect gives (a 2:1 photo is a whole sphere). Answers the
# id, the relative url a node's `pano` field stores, the
# pixel size and the coverage.
#
# Used by:
#   - the admin capture / import screens in the app (later)
############################################################

@wayfind_bp.route("/buildings/<building_id>/panoramas", methods=["POST"])
@require_role(*EDITOR_ROLES)
@rate_limit("wayfind_upload", max_attempts=120)
def upload_panorama(building_id):

    building = _load_building(building_id)
    if building is None:
        return jsonify({"error": "Unknown building", "code": "not_found"}), 404
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "file is required", "code": "no_file"}), 400


    # STEP 1: decode, verify and re-encode — the re-encode is
    # the gate, exactly as in uploads/routes.py, only without
    # the 2048 px cap a panorama cannot live with. Pillow
    # raises DecompressionBombError inside open() for anything
    # past PANO_MAX_PIXELS, so the oversize refusal is the
    # catch, not the comparison
    # ======================================================
    raw = upload.read()
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > PANO_MAX_PIXELS:
            return jsonify({"error": f"Image too large. Max {PANO_MAX_PIXELS // (1000 * 1000)} megapixels", "code": "too_large"}), 413
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
        return jsonify({"error": f"Image too large. Max {PANO_MAX_PIXELS // (1000 * 1000)} megapixels", "code": "too_large"}), 413
    except Exception:
        return jsonify({"error": "Not a readable image", "code": "bad_image"}), 400


    # STEP 2: the coverage — the author's word, else the aspect
    # ========================================================
    hfov = _float_field("hfovDeg", 360.0)
    vfov = _float_field("vfovDeg", min(180.0, 360.0 * height / width))
    heading_raw = _float_field("headingRawDeg", None)
    heading_source = request.form.get("headingSource")
    if hfov is False or vfov is False or heading_raw is False:
        return jsonify({"error": "hfovDeg, vfovDeg and headingRawDeg must be numbers", "code": "bad_geometry"}), 400
    hfov = min(360.0, max(1.0, hfov))
    vfov = min(180.0, max(1.0, vfov))
    node_id = request.form.get("nodeId")
    if node_id is not None and not ENTITY_ID_RE.match(node_id):
        return jsonify({"error": "nodeId must be a short id", "code": "bad_node"}), 400


    # STEP 3: store by content hash and record it once
    # =================================================
    digest = hashlib.sha256(blob).hexdigest()
    name = f"{digest}.jpg"
    if not _write_once(_store_dir("panoramas"), name, blob):
        return jsonify({"error": "The panorama could not be stored", "code": "write_failed"}), 500
    db = get_db()
    db.execute(
        """
        INSERT OR IGNORE INTO wf_panoramas (id, building_id, node_id, width, height, bytes, hfov_deg, vfov_deg, heading_raw_deg, heading_source, uploaded_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (digest, building_id, node_id, width, height, len(blob), hfov, vfov, heading_raw, heading_source, request.user["id"], utc_now_iso()),
    )
    db.commit()

    return jsonify({
        "id": digest,
        "url": f"/api/wayfind/panoramas/{name}",
        "width": width,
        "height": height,
        "bytes": len(blob),
        "hfovDeg": hfov,
        "vfovDeg": vfov,
    }), 201









############################################################
# serve_panorama / serve_plan
############################################################
#
# GET /api/wayfind/panoramas/<name>
# GET /api/wayfind/plans/<name>
#
# A stored file by its hashed name, public and immutable: the
# name IS the content, so a year of caching is safe and a
# changed picture is a different url. Anything but a 64-hex
# name with the right extension is a 404 before the disk is
# touched. A plan is served under the app-wide security
# headers (add_security_headers in app/__init__.py forbids
# inline scripts), on top of the sanitising it got on upload.
#
# Used by:
#   - the app's panorama stage and floor plan, through the
#     kit's resolveImageUrl / the plan loader
############################################################

@wayfind_bp.route("/panoramas/<name>", methods=["GET"])
def serve_panorama(name):
    if not STORED_NAME_RE.match(name or "") or not name.endswith(".jpg"):
        return jsonify({"error": "Not found", "code": "not_found"}), 404
    response = send_from_directory(_store_dir("panoramas"), name, mimetype="image/jpeg", conditional=True)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@wayfind_bp.route("/plans/<name>", methods=["GET"])
def serve_plan(name):
    if not STORED_NAME_RE.match(name or "") or not name.endswith(".svg"):
        return jsonify({"error": "Not found", "code": "not_found"}), 404
    response = send_from_directory(_store_dir("plans"), name, mimetype="image/svg+xml", conditional=True)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response









############################################################
# upload_plan
############################################################
#
# POST /api/wayfind/buildings/<building_id>/plans
#
# Multipart: file (an SVG drawing, 2 MB), optional levelId.
# The text must carry an <svg> root; scripts, event-handler
# attributes and script hrefs are cut, and the sha256 of the
# sanitised text names the file. Answers the id and the
# relative url a level's `plan` field stores.
#
# Used by:
#   - the admin level sheet in the app (later); the seed
#     script that imports the bundled plans
############################################################

@wayfind_bp.route("/buildings/<building_id>/plans", methods=["POST"])
@require_role(*EDITOR_ROLES)
@rate_limit("wayfind_upload", max_attempts=120)
def upload_plan(building_id):

    building = _load_building(building_id)
    if building is None:
        return jsonify({"error": "Unknown building", "code": "not_found"}), 404
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "file is required", "code": "no_file"}), 400
    raw = upload.read()
    if len(raw) > PLAN_MAX_BYTES:
        return jsonify({"error": "Plan too large. Max 2 MB", "code": "too_large"}), 413
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "Plan must be UTF-8 SVG text", "code": "bad_plan"}), 400
    if "<svg" not in text[:4096].lower():
        return jsonify({"error": "Plan must be an SVG drawing", "code": "bad_plan"}), 400
    level_id = request.form.get("levelId")
    if level_id is not None and not ENTITY_ID_RE.match(level_id):
        return jsonify({"error": "levelId must be a short id", "code": "bad_level"}), 400

    clean = SVG_SCRIPT_RE.sub("", text)
    clean = SVG_HANDLER_RE.sub("", clean)
    clean = SVG_JS_HREF_RE.sub(r'\1\2\2', clean)
    blob = clean.encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    name = f"{digest}.svg"
    if not _write_once(_store_dir("plans"), name, blob):
        return jsonify({"error": "The plan could not be stored", "code": "write_failed"}), 500
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO wf_plans (id, building_id, level_id, bytes, uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (digest, building_id, level_id, len(blob), request.user["id"], utc_now_iso()),
    )
    db.commit()

    return jsonify({"id": digest, "url": f"/api/wayfind/plans/{name}", "bytes": len(blob)}), 201









############################################################
# _building_fields / _float_field
############################################################
#
# The building row as the app spells it, and a form field as
# a float: the default when absent, False when present but
# not a number (so a caller can refuse rather than guess).
#
# Used by:
#   - get_draft / upload_panorama (above)
############################################################

def _building_fields(building):
    return {"id": building["id"], "name": building["name"], "northDeg": building["north_deg"], "entranceNodeId": building["entrance_node_id"]}


def _float_field(name, default):
    value = request.form.get(name)
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except ValueError:
        return False
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else False









############################################################
# capture routes
############################################################
#
# The guided-capture endpoints (create / frame / finish /
# status) live in captures.py and are hung on this blueprint
# HERE, at the very bottom: captures.py imports this module's
# helpers lazily, so registering after every definition keeps
# the import order acyclic.
#
# Used by:
#   - app/wayfind/captures.py — the four routes it defines
############################################################

from app.wayfind.captures import register_capture_routes

register_capture_routes(wayfind_bp)
