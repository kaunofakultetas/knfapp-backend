############################################################
#  [*] Wayfind — the building graph: compile and validate
#
#  The draft lives as one row per entity (wf_entities: level,
#  node, edge, room) so an admin's edits touch one row each;
#  what the mobile app routes over is ONE document — the
#  BuildingGraph shape the engine package reads (version 1,
#  levels / nodes / edges / rooms, entranceNodeId, northDeg).
#  compile_document assembles that document from the rows,
#  and validate_document is the server-side twin of the
#  engine's validateGraph ERROR codes, so a publish can never
#  hand students a graph the router chokes on. Warnings
#  (unreachable nodes, lengths under their chord, panorama
#  facts) stay on the client — they are advice to an editor,
#  not a reason to refuse a publish.
#
#  Edges carry an "id" the engine does not know about — the
#  editor needs one to address an edge, and the engine ignores
#  fields it never reads.
############################################################


import hashlib
import json
import math
import re


CONNECTOR_KINDS = ("stairs", "elevator", "ramp")
EDGE_KINDS = ("hallway", "door") + CONNECTOR_KINDS
NODE_KINDS = ("corridor", "door", "stairs", "elevator", "ramp", "entrance", "room")
ENTITY_KINDS = ("level", "node", "edge", "room")

# The plural the document keys an entity kind under
COLLECTION = {"level": "levels", "node": "nodes", "edge": "edges", "room": "rooms"}

# The id grammar every entity id — and every field that
# names one (a node's level, an edge's ends, a room's node) —
# must follow; api/views.py re-exports it for the routes
ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

# Optional fields the app reads as text, per kind: absent,
# null or a string — anything else is refused at write time,
# because the phone would crash or misdraw on it (a room's
# name folded for search, a pano reference resolved as a url)
OPTIONAL_TEXT = {
    "level": ("plan",),
    "node": ("roomId", "pano", "qr", "landmark"),
    "edge": (),
    "room": ("nameKey", "nameEn", "category", "hours", "access"),
}

# Optional fields the router or the stage does ARITHMETIC on,
# per kind: absent, null or a finite number
OPTIONAL_NUMBER = {
    "level": ("northDeg",),
    "node": ("panoYaw",),
    "edge": ("closedUntil", "delaySeconds"),
    "room": (),
}








############################################################
# compile_document
############################################################
#
# The BuildingGraph document for one building from its live
# (undeleted) entity rows plus the building row's own fields.
# Levels come out in ordinal order and everything else in id
# order, so two compiles of the same draft are byte-identical
# — the published ETag is the sha256 of exactly this text.
#
# Used by:
#   - api/views.py get_draft / publish_building
############################################################

def compile_document(building, rows):

    document = {
        "version": 1,
        "building": building["id"],
        "levels": [],
        "nodes": [],
        "edges": [],
        "rooms": [],
        "entranceNodeId": building["entrance_node_id"],
        "northDeg": building["north_deg"],
    }


    for row in rows:
        if row["deleted"]:
            continue
        data = row["data"]
        if not isinstance(data, dict):
            continue
        data["id"] = row["id"]
        document[COLLECTION[row["kind"]]].append(data)


    document["levels"].sort(key=lambda level: (_number(level.get("ordinal")), level["id"]))
    for key in ("nodes", "edges", "rooms"):
        document[key].sort(key=lambda entity: entity["id"])

    return document








############################################################
# document_text / document_etag
############################################################
#
# The one serialisation everything hashes and serves: sorted
# keys, no whitespace, so the ETag a client sends back is the
# ETag of the bytes it holds.
#
# Used by:
#   - api/views.py get_graph / publish_building
############################################################

def document_text(document) -> str:
    # allow_nan=False: a NaN / Infinity that slipped past the
    # write checks raises here instead of publishing text no
    # JSON parser on a phone will read
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def document_etag(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()








############################################################
# validate_document
############################################################
#
# The engine's validateGraph ERROR codes, in Python, over a
# compiled document. Same codes, same refs, so the app can
# show a server refusal with the very messages its own
# validator uses: duplicate_id, unknown_level, bad_coordinate
# (a node whose x / y is not a finite number — the router's
# edge lengths would be NaN and every route 'no_path'),
# dangling_edge, unknown_kind (edges only — a node kind never
# reaches the router's arithmetic), bad_length,
# cross_level_hallway, connector_without_length,
# room_without_node, missing_entrance. Answers the list of
# issues; empty means publishable.
#
# It never raises on a malformed row: every value used as a
# set or dict key is checked to be a string first, so a row
# written before entity_shape_error learned types (a level
# that is a JSON object, an edge end that is a list) degrades
# to its issue instead of taking GET /draft and the publish
# down with a TypeError.
#
# Used by:
#   - api/views.py publish_building — refuses with the list
#   - api/views.py get_draft — advice beside the draft
############################################################

def validate_document(document):

    issues = []
    levels = set()
    nodes = {}


    # STEP 1: levels and nodes — ids once, every node on a
    # level that exists
    # ===================================================
    for level in document.get("levels", []):
        if level["id"] in levels:
            _issue(issues, "duplicate_id", level["id"], f"level '{level['id']}' is defined twice")
        levels.add(level["id"])

    for node in document.get("nodes", []):
        if node["id"] in nodes:
            _issue(issues, "duplicate_id", node["id"], f"node '{node['id']}' is defined twice")
        nodes[node["id"]] = node
        if not _known(node.get("level"), levels):
            _issue(issues, "unknown_level", node["id"], f"node '{node['id']}' sits on unknown level '{node.get('level')}'")
        if not (_finite(node.get("x")) and _finite(node.get("y"))):
            _issue(issues, "bad_coordinate", node["id"], f"node '{node['id']}' has coordinates ({node.get('x')}, {node.get('y')})")


    # STEP 2: edges — both ends exist, a known kind, a sane
    # length, a level change only by a connector that carries
    # its length
    # ======================================================
    for edge in document.get("edges", []):
        ref = f"{edge.get('a')}-{edge.get('b')}"
        a = nodes.get(edge.get("a")) if _known(edge.get("a"), nodes) else None
        b = nodes.get(edge.get("b")) if _known(edge.get("b"), nodes) else None
        if a is None or b is None:
            _issue(issues, "dangling_edge", ref, f"edge {ref} references a missing node")
            continue
        if edge.get("kind") not in EDGE_KINDS:
            _issue(issues, "unknown_kind", ref, f"edge {ref} has unknown kind '{edge.get('kind')}'")
        length = edge.get("lengthM")
        has_length = length is not None
        if has_length and not (isinstance(length, (int, float)) and not isinstance(length, bool) and length == length and length not in (float("inf"), float("-inf")) and length >= 0):
            _issue(issues, "bad_length", ref, f"edge {ref} has lengthM {length}")
            has_length = False
        if a.get("level") != b.get("level"):
            if edge.get("kind") not in CONNECTOR_KINDS:
                _issue(issues, "cross_level_hallway", ref, f"edge {ref} changes level but is a '{edge.get('kind')}'")
            elif not has_length:
                _issue(issues, "connector_without_length", ref, f"edge {ref} changes level but carries no lengthM")


    # STEP 3: rooms and the entrance — every room on a node
    # and a level that exist
    # =====================================================
    rooms = set()
    for room in document.get("rooms", []):
        if room["id"] in rooms:
            _issue(issues, "duplicate_id", room["id"], f"room '{room['id']}' is defined twice")
        rooms.add(room["id"])
        if not _known(room.get("nodeId"), nodes):
            _issue(issues, "room_without_node", room["id"], f"room '{room['id']}' points at missing node '{room.get('nodeId')}'")
        if not _known(room.get("level"), levels):
            _issue(issues, "unknown_level", room["id"], f"room '{room['id']}' sits on unknown level '{room.get('level')}'")

    entrance = document.get("entranceNodeId")
    if entrance and not _known(entrance, nodes):
        _issue(issues, "missing_entrance", str(entrance), f"entranceNodeId '{entrance}' is not a node")

    return issues








############################################################
# entity_shape_error
############################################################
#
# The one shape check an op's payload gets before it is
# stored: a JSON object with the fields the engine cannot do
# without for that kind, each of the TYPE the engine reads
# it as — ids are id strings, coordinates / ordinals / scales
# finite numbers (never a bool, never NaN or Infinity, which
# the body parser accepts and PostgreSQL's jsonb refuses), a
# polygon three or more [x, y] points, the optional text
# fields text and the optional numbers numbers. A room's
# nodeId may be "" — the editor unlinks a room that way when
# its node is force-deleted, and the validator then names it
# room_without_node. Everything finer (a level that does not
# exist yet, a dangling edge, a length) is the validator's
# business at publish time — an admin mid-edit is allowed a
# graph that does not yet hang together. Answers None or the
# reason.
#
# Used by:
#   - api/views.py post_ops
############################################################

def entity_shape_error(kind, data):

    if not isinstance(data, dict):
        return "data must be an object"

    required = {
        "level": ("label", "viewBox", "metersPerPixel", "ordinal"),
        "node": ("level", "x", "y", "kind"),
        "edge": ("a", "b", "kind"),
        "room": ("name", "level", "nodeId"),
    }[kind]
    missing = [field for field in required if field not in data]
    if missing:
        return f"missing {', '.join(missing)}"

    if kind == "level":
        if not (isinstance(data["label"], str) and data["label"].strip()):
            return "label must be a non-empty string"
        box = data["viewBox"]
        if not (isinstance(box, list) and len(box) == 4 and all(_finite(v) for v in box) and box[2] > 0 and box[3] > 0):
            return "viewBox must be [minX, minY, width, height] with a positive size"
        if not (_finite(data["metersPerPixel"]) and data["metersPerPixel"] > 0):
            return "metersPerPixel must be a positive number"
        if not _finite(data["ordinal"]):
            return "ordinal must be a number"
    if kind == "node":
        if not _is_id(data["level"]):
            return "level must be a level id"
        if not (_finite(data["x"]) and _finite(data["y"])):
            return "x and y must be finite numbers"
        if data["kind"] not in NODE_KINDS:
            return f"unknown node kind '{data['kind']}'"
        geometry = data.get("panoGeometry")
        if geometry is not None and not _pano_geometry(geometry):
            return "panoGeometry must carry numeric hfovDeg and vfovDeg"
        if data.get("panoHeading") is not None and not isinstance(data["panoHeading"], dict):
            return "panoHeading must be an object or null"
        links = data.get("panoLinks")
        if links is not None and not (isinstance(links, list) and all(isinstance(link, dict) and isinstance(link.get("targetNodeId"), str) for link in links)):
            return "panoLinks must list objects naming a targetNodeId"
    if kind == "edge":
        if not (_is_id(data["a"]) and _is_id(data["b"])):
            return "a and b must be node ids"
        if data["kind"] not in EDGE_KINDS:
            return f"unknown edge kind '{data['kind']}'"
        if data.get("oneWay") is not None and not isinstance(data["oneWay"], bool):
            return "oneWay must be true, false or null"
    if kind == "room":
        if not (isinstance(data["name"], str) and data["name"].strip()):
            return "name must be a non-empty string"
        if not _is_id(data["level"]):
            return "level must be a level id"
        if not (data["nodeId"] == "" or _is_id(data["nodeId"])):
            return "nodeId must be a node id"
        polygon = data.get("polygon")
        if polygon is not None and not (isinstance(polygon, list) and len(polygon) >= 3 and all(
                isinstance(point, list) and len(point) == 2 and _finite(point[0]) and _finite(point[1]) for point in polygon)):
            return "polygon must list at least three [x, y] points"
        for field in ("aliases", "photos"):
            value = data.get(field)
            if value is not None and not (isinstance(value, list) and all(isinstance(item, str) for item in value)):
                return f"{field} must be a list of strings or null"


    # The optional fields every kind shares the rule for
    for field in OPTIONAL_TEXT[kind]:
        if data.get(field) is not None and not isinstance(data[field], str):
            return f"{field} must be a string or null"
    for field in OPTIONAL_NUMBER[kind]:
        if data.get(field) is not None and not _finite(data[field]):
            return f"{field} must be a number or null"

    return None








############################################################
# _issue / _number / _finite / _is_id / _known /
# _pano_geometry
############################################################
#
# One issue row in the engine's own shape; a sort key that
# tolerates a missing or non-numeric ordinal; whether a value
# is a finite JSON number (a bool is not one, and neither is
# NaN or Infinity); whether a value is an entity id; whether
# a value is a STRING key of the given set / dict — the
# membership test that cannot raise on an unhashable value;
# and whether a panorama geometry is an object whose sizes
# are numbers (its centre / offset numbers or null).
#
# Used by:
#   - validate_document / compile_document /
#     entity_shape_error (above)
#   - api/views.py — _finite for the building's northDeg
############################################################

def _issue(issues, code, ref, message):
    issues.append({"severity": "error", "code": code, "ref": ref, "message": message})


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_id(value):
    return isinstance(value, str) and ENTITY_ID_RE.match(value) is not None


def _known(value, keys):
    return isinstance(value, str) and value in keys


def _pano_geometry(geometry):
    if not isinstance(geometry, dict):
        return False
    if not (_finite(geometry.get("hfovDeg")) and _finite(geometry.get("vfovDeg"))):
        return False
    return all(geometry.get(field) is None or _finite(geometry.get(field)) for field in ("centreYawDeg", "vOffsetDeg"))
