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


CONNECTOR_KINDS = ("stairs", "elevator", "ramp")
EDGE_KINDS = ("hallway", "door") + CONNECTOR_KINDS
NODE_KINDS = ("corridor", "door", "stairs", "elevator", "ramp", "entrance", "room")
ENTITY_KINDS = ("level", "node", "edge", "room")

# The plural the document keys an entity kind under
COLLECTION = {"level": "levels", "node": "nodes", "edge": "edges", "room": "rooms"}








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
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def document_etag(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()








############################################################
# validate_document
############################################################
#
# The engine's validateGraph ERROR codes, in Python, over a
# compiled document. Same codes, same refs, so the app can
# show a server refusal with the very messages its own
# validator uses: duplicate_id, unknown_level, dangling_edge,
# unknown_kind (edges only — a node kind never reaches the
# router's arithmetic), bad_length, cross_level_hallway,
# connector_without_length, room_without_node,
# missing_entrance. Answers the list of issues; empty means
# publishable.
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
        if node.get("level") not in levels:
            _issue(issues, "unknown_level", node["id"], f"node '{node['id']}' sits on unknown level '{node.get('level')}'")


    # STEP 2: edges — both ends exist, a known kind, a sane
    # length, a level change only by a connector that carries
    # its length
    # ======================================================
    for edge in document.get("edges", []):
        ref = f"{edge.get('a')}-{edge.get('b')}"
        a = nodes.get(edge.get("a"))
        b = nodes.get(edge.get("b"))
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
        if room.get("nodeId") not in nodes:
            _issue(issues, "room_without_node", room["id"], f"room '{room['id']}' points at missing node '{room.get('nodeId')}'")
        if room.get("level") not in levels:
            _issue(issues, "unknown_level", room["id"], f"room '{room['id']}' sits on unknown level '{room.get('level')}'")

    entrance = document.get("entranceNodeId")
    if entrance and entrance not in nodes:
        _issue(issues, "missing_entrance", entrance, f"entranceNodeId '{entrance}' is not a node")

    return issues








############################################################
# entity_shape_error
############################################################
#
# The one shape check an op's payload gets before it is
# stored: a JSON object with the fields the engine cannot do
# without for that kind. Everything finer (a level that does
# not exist yet, a dangling edge) is the validator's business
# at publish time — an admin mid-edit is allowed a graph that
# does not yet hang together. Answers None or the reason.
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

    if kind == "node" and data["kind"] not in NODE_KINDS:
        return f"unknown node kind '{data['kind']}'"
    if kind == "edge" and data["kind"] not in EDGE_KINDS:
        return f"unknown edge kind '{data['kind']}'"
    if kind == "level":
        box = data["viewBox"]
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
            return "viewBox must be [minX, minY, width, height]"
        if not (isinstance(data["metersPerPixel"], (int, float)) and data["metersPerPixel"] > 0):
            return "metersPerPixel must be a positive number"

    return None








############################################################
# _issue / _number
############################################################
#
# One issue row in the engine's own shape, and a sort key
# that tolerates a missing or non-numeric ordinal.
#
# Used by:
#   - validate_document / compile_document (above)
############################################################

def _issue(issues, code, ref, message):
    issues.append({"severity": "error", "code": code, "ref": ref, "message": message})


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
