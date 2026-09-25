############################################################
#  [*] Regression tests — wayfind graph, ops, publish
#
#  What the map's edit-and-publish contract promises: op ids
#  are idempotent PER BUILDING (a duplicate answers what the
#  original did — `of`, reason, revision — and a fixed seed
#  id can still bootstrap a second building), a stale edit
#  is a per-op conflict carrying the entity as it stands
#  while the rest of the batch lands, an upsert saying
#  neither baseRevision nor fresh refuses the whole batch
#  (400 no_base) before anything lands while a fresh create
#  and a base-0 seed edit take the normal path (the seed
#  edit conflicting against any row the server holds), a
#  batch of rejections bumps no revision, deletions are
#  tombstones a ?since delta carries, the validator refuses
#  a publish with the engine's own error codes, an unchanged
#  draft cannot be re-published, and the published document
#  round-trips byte-for-byte under its ETag (304 on
#  If-None-Match). Shapes are typed at the door (KNF-023 /
#  KNF-024): an id field that is an object, a coordinate that
#  is a string, NaN or a bool, a polygon or an alias list of
#  the wrong shape is rejected per op with its reason, and a
#  row that predates the checks degrades to validator issues
#  (bad_coordinate among them) instead of a 500 on GET /draft
#  and on publish. A baseRevision that is not an integer
#  rejects its op (KNF-159), a fresh create over a live
#  entity is a conflict, and a building's northDeg must be a
#  finite number.
############################################################


import json


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.common.timestamps import utc_now
from knfapp.users import auth
from knfapp.wayfind.graph import compile_document, document_text, validate_document
from knfapp.wayfind.models import WfBuilding, WfEntity
from .utils import bearer, create_user


def _op(op_id, entity_kind, entity_id, data, base=None, op_type="upsert", fresh=None):
    op = {"id": op_id, "type": op_type, "kind": entity_kind, "entityId": entity_id, "data": data}
    if base is not None:
        op["baseRevision"] = base
    # The phone's shape: an upsert with no base is a create and says
    # so — the server refuses one that says neither
    if fresh is None:
        fresh = op_type == "upsert" and base is None
    if fresh:
        op["fresh"] = True
    return op


LEVEL = {"label": "1 aukštas", "viewBox": [0, 0, 100, 60], "metersPerPixel": 0.1, "ordinal": 0}
NODE_A = {"level": "l1", "x": 10, "y": 10, "kind": "corridor"}
NODE_B = {"level": "l1", "x": 20, "y": 10, "kind": "entrance"}


class WayfindTestCase(TestCase):

    def setUp(self):
        ratelimit.reset()
        self.admin = create_user(username="vadovas", role="admin")
        self.token = auth.mint_session(self.admin.id)
        self.client = Client()
        self._post_json("/api/wayfind/buildings", {"id": "b1", "name": "Fakultetas"}, expect=201)

    def _post_json(self, path, body, token=None, expect=None):
        response = bearer(self.client.post, path, token or self.token,
                          data=json.dumps(body), content_type="application/json")
        if expect is not None:
            self.assertEqual(response.status_code, expect, response.content)
        return response

    def _ops(self, ops, expect=200):
        response = self._post_json("/api/wayfind/buildings/b1/ops", {"ops": ops})
        self.assertEqual(response.status_code, expect, response.content)
        return json.loads(response.content)


class OpLogTests(WayfindTestCase):

    def test_a_duplicate_of_an_applied_op_carries_of_and_revision(self):
        first = self._ops([_op("op-1", "level", "l1", LEVEL)])
        self.assertEqual(first["results"][0]["status"], "applied")

        replay = self._ops([_op("op-1", "level", "l1", LEVEL)])
        result = replay["results"][0]
        self.assertEqual((result["status"], result["of"]), ("duplicate", "applied"))
        self.assertEqual(result["revision"], first["revision"])
        # Nothing re-applied — the revision stands where it was
        self.assertEqual(replay["revision"], first["revision"])

    def test_a_duplicate_of_a_rejected_op_carries_the_reason(self):
        self._ops([_op("op-bad", "node", "n1", {"x": 1})])  # missing fields → rejected
        replay = self._ops([_op("op-bad", "node", "n1", {"x": 1})])
        result = replay["results"][0]
        self.assertEqual((result["status"], result["of"]), ("duplicate", "rejected"))
        self.assertIn("missing", result["reason"])
        self.assertIsNone(result["revision"])

    def test_the_same_op_id_bootstraps_a_second_building(self):
        self._post_json("/api/wayfind/buildings", {"id": "b2", "name": "Antras"}, expect=201)
        self._ops([_op("seed-1", "level", "l1", LEVEL)])
        response = self._post_json("/api/wayfind/buildings/b2/ops",
                                   {"ops": [_op("seed-1", "level", "l1", LEVEL)]})
        self.assertEqual(json.loads(response.content)["results"][0]["status"], "applied")

    def test_a_stale_edit_is_a_conflict_carrying_the_current_entity(self):
        first = self._ops([_op("op-1", "level", "l1", LEVEL)])
        base = first["revision"]
        # The other editor's edit, stamped the way the phone stamps
        # one (a bare `fresh` create over the live row would itself
        # be a conflict now)
        self._ops([_op("op-2", "level", "l1", dict(LEVEL, label="Naujas"), base=base)])

        # An edit from the old copy loses; a fresh op in the same
        # batch still lands
        batch = self._ops([
            _op("op-3", "level", "l1", dict(LEVEL, label="Pasenęs"), base=base),
            _op("op-4", "node", "n1", NODE_A),
        ])
        conflict, applied = batch["results"]
        self.assertEqual((conflict["status"], conflict["reason"]), ("rejected", "conflict"))
        self.assertEqual(conflict["current"]["data"]["label"], "Naujas")
        self.assertEqual(applied["status"], "applied")

    def test_a_bare_upsert_refuses_the_whole_batch_before_anything_lands(self):
        first = self._ops([_op("op-1", "level", "l1", LEVEL)])
        bare = {"id": "op-2", "type": "upsert", "kind": "node", "entityId": "n1", "data": NODE_A}
        response = self._post_json("/api/wayfind/buildings/b1/ops",
                                   {"ops": [_op("op-3", "node", "n2", NODE_B), bare]})
        self.assertEqual(response.status_code, 400, response.content)
        answer = json.loads(response.content)
        self.assertEqual((answer["code"], answer["opId"]), ("no_base", "op-2"))

        # Nothing in the batch landed or was logged: the revision
        # stands, the row is absent, and the good op is no duplicate
        draft = json.loads(bearer(self.client.get, "/api/wayfind/buildings/b1/draft", self.token).content)
        self.assertEqual(draft["revision"], first["revision"])
        self.assertEqual(sorted(draft["revisions"]), ["level:l1"])
        self.assertEqual(self._ops([_op("op-3", "node", "n2", NODE_B)])["results"][0]["status"], "applied")

        # A bool is not a base revision either
        response = self._post_json("/api/wayfind/buildings/b1/ops",
                                   {"ops": [dict(bare, id="op-4", baseRevision=True)]})
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(json.loads(response.content)["code"], "no_base")

    def test_a_fresh_create_lands_without_a_base(self):
        # What the phone sends for a NEW entity: fresh: true, no base
        batch = self._ops([
            {"id": "op-1", "type": "upsert", "kind": "level", "entityId": "l1", "data": LEVEL, "fresh": True},
            {"id": "op-2", "type": "upsert", "kind": "node", "entityId": "n1", "data": NODE_A, "fresh": True},
        ])
        self.assertEqual([r["status"] for r in batch["results"]], ["applied", "applied"])
        # A tombstone, deleted without a base, revives under a fresh
        # create too — deletes are not the guard's business
        self._ops([{"id": "op-3", "type": "delete", "kind": "node", "entityId": "n1"}])
        revived = self._ops([{"id": "op-4", "type": "upsert", "kind": "node", "entityId": "n1", "data": NODE_B, "fresh": True}])
        self.assertEqual(revived["results"][0]["status"], "applied")

    def test_a_seed_edit_stamped_base_0_conflicts_against_any_row_the_server_holds(self):
        # The offline seed: the phone knows no revision, so every edit
        # on a seed entity carries base 0 — the server's copy, at any
        # revision, wins the check and comes back as current
        self._ops([_op("op-1", "level", "l1", LEVEL)])
        batch = self._ops([_op("op-2", "level", "l1", dict(LEVEL, label="Pradinis"), base=0)])
        result = batch["results"][0]
        self.assertEqual((result["status"], result["reason"]), ("rejected", "conflict"))
        self.assertEqual(result["current"]["data"]["label"], LEVEL["label"])
        self.assertFalse(result["current"]["deleted"])
        # A seed entity the server never had is simply created
        created = self._ops([_op("op-3", "node", "n1", NODE_A, base=0)])
        self.assertEqual(created["results"][0]["status"], "applied")

    def test_a_batch_of_rejections_bumps_no_revision(self):
        before = self._ops([_op("op-1", "level", "l1", LEVEL)])["revision"]
        after = self._ops([_op("op-x", "node", "n9", {"x": 1})])["revision"]
        self.assertEqual(after, before)

    def test_a_delete_is_a_tombstone_the_since_delta_carries(self):
        self._ops([_op("op-1", "level", "l1", LEVEL)])
        marker = self._ops([_op("op-2", "node", "n1", NODE_A)])["revision"]
        self._ops([{"id": "op-3", "type": "delete", "kind": "node", "entityId": "n1"}])

        response = bearer(self.client.get, f"/api/wayfind/buildings/b1/draft?since={marker}", self.token)
        delta = json.loads(response.content)
        self.assertEqual([(e["id"], e["deleted"], e["data"]) for e in delta["entities"]],
                         [("n1", True, None)])


class ShapeTypingTests(WayfindTestCase):

    def test_a_malformed_entity_is_rejected_at_the_door_with_its_reason(self):
        self._ops([_op("ok-1", "level", "l1", LEVEL), _op("ok-2", "node", "n1", NODE_A), _op("ok-3", "node", "n2", NODE_B)])
        bad = [
            ("node", "b1", dict(NODE_A, level={"oops": 1}), "level must be a level id"),
            ("node", "b2", dict(NODE_A, x="not-a-number"), "x and y must be finite numbers"),
            ("node", "b3", dict(NODE_A, y=None), "x and y must be finite numbers"),
            ("node", "b4", dict(NODE_A, x=True), "x and y must be finite numbers"),
            ("node", "b5", dict(NODE_A, panoYaw="90"), "panoYaw must be a number or null"),
            ("node", "b6", dict(NODE_A, panoGeometry={"hfovDeg": "wide"}), "panoGeometry"),
            ("node", "b7", dict(NODE_A, pano=["x"]), "pano must be a string or null"),
            ("node", "b7a", dict(NODE_A, panoLinks=5), "panoLinks"),
            ("node", "b7b", dict(NODE_A, panoLinks=[{"yaw": 1}]), "panoLinks"),
            ("node", "b7c", dict(NODE_A, panoHeading="aligned"), "panoHeading must be an object or null"),
            ("edge", "b8", {"a": ["n1"], "b": "n2", "kind": "hallway"}, "a and b must be node ids"),
            ("level", "b9", dict(LEVEL, ordinal="1"), "ordinal must be a number"),
            ("level", "b10", dict(LEVEL, viewBox=[0, 0, 0, 60]), "viewBox"),
            ("level", "b11", dict(LEVEL, metersPerPixel=True), "metersPerPixel"),
            ("level", "b12", dict(LEVEL, label=""), "label must be a non-empty string"),
            ("room", "b13", {"name": "A", "level": "l1", "nodeId": {"n": 1}}, "nodeId must be a node id"),
            ("room", "b14", {"name": "A", "level": "l1", "nodeId": "n1", "polygon": "0,0 1,1"}, "polygon"),
            ("room", "b15", {"name": "A", "level": "l1", "nodeId": "n1", "aliases": "A1"}, "aliases"),
            ("room", "b16", {"name": 7, "level": "l1", "nodeId": "n1"}, "name must be a non-empty string"),
        ]
        batch = self._ops([_op(f"bad-{i}", kind, entity, data) for i, (kind, entity, data, _) in enumerate(bad)])
        for result, (_, entity, _, reason) in zip(batch["results"], bad):
            self.assertEqual(result["status"], "rejected", (entity, result))
            self.assertIn(reason, result["reason"], entity)

        # None of it landed, so the draft still opens and publishes
        draft = bearer(self.client.get, "/api/wayfind/buildings/b1/draft", self.token)
        self.assertEqual(draft.status_code, 200, draft.content)
        self.assertEqual(sorted(json.loads(draft.content)["revisions"]), ["level:l1", "node:n1", "node:n2"])

        # A room unlinked from its force-deleted node ("" node) is
        # a shape the editor writes — accepted, flagged at publish
        unlinked = self._ops([_op("ok-4", "room", "r1", {"name": "A", "level": "l1", "nodeId": "", "polygon": [[0, 0], [5, 0], [5, 5]]})])
        self.assertEqual(unlinked["results"][0]["status"], "applied")

    def test_rows_that_predate_the_checks_degrade_to_issues_not_a_500(self):
        # Written straight to the table, the way a row stored
        # before the checks existed looks
        self._ops([_op("ok-1", "level", "l1", LEVEL), _op("ok-2", "node", "n1", NODE_A)])
        now = utc_now()
        for kind, entity, data in (
            ("node", "n-obj", dict(NODE_A, level={"oops": 1})),
            ("node", "n-nan", dict(NODE_A, x="not-a-number", y=None)),
            ("edge", "e-list", {"a": ["n1"], "b": "n1", "kind": "hallway"}),
            ("room", "r-list", {"name": "A", "level": ["l1"], "nodeId": {"n": 1}}),
        ):
            WfEntity.objects.create(building_id="b1", kind=kind, id=entity, data=data, revision=9,
                                    updated_at=now, updated_by=None, deleted=False)

        draft = bearer(self.client.get, "/api/wayfind/buildings/b1/draft", self.token)
        self.assertEqual(draft.status_code, 200, draft.content)
        codes = {(issue["code"], issue["ref"]) for issue in json.loads(draft.content)["issues"]}
        self.assertIn(("unknown_level", "n-obj"), codes)
        self.assertIn(("bad_coordinate", "n-nan"), codes)
        self.assertIn(("dangling_edge", "['n1']-n1"), codes)
        self.assertIn(("room_without_node", "r-list"), codes)
        self.assertIn(("unknown_level", "r-list"), codes)

        response = self._post_json("/api/wayfind/buildings/b1/publish", {})
        self.assertEqual(response.status_code, 422, response.content)
        self.assertIn("bad_coordinate", [issue["code"] for issue in json.loads(response.content)["issues"]])

    def test_validate_document_never_raises_on_an_unhashable_value(self):
        document = {
            "levels": [{"id": "l1"}],
            "nodes": [{"id": "n1", "level": {"x": 1}, "x": 1, "y": 1}],
            "edges": [{"a": {"x": 1}, "b": ["n1"], "kind": "hallway"}],
            "rooms": [{"id": "r1", "nodeId": ["n1"], "level": {"l": 1}}],
            "entranceNodeId": ["n1"],
        }
        codes = [issue["code"] for issue in validate_document(document)]
        self.assertEqual(sorted(set(codes)), ["dangling_edge", "missing_entrance", "room_without_node", "unknown_level"])


class RevisionStampTests(WayfindTestCase):

    def test_a_malformed_base_revision_rejects_its_op_instead_of_overwriting(self):
        self._ops([_op("op-1", "level", "l1", LEVEL), _op("op-2", "node", "n1", NODE_A)])
        self._ops([_op("op-3", "node", "n1", dict(NODE_A, x=9), base=0)])  # a conflict — the row is past 0
        moved = self._ops([_op("op-4", "node", "n1", dict(NODE_A, x=9), base=10**6)])
        self.assertEqual(moved["results"][0]["status"], "applied")

        for op_id, base, op_type, fresh in (("s-1", "1", "upsert", True), ("s-2", 1.0, "upsert", True),
                                            ("s-3", "1", "delete", False), ("s-4", 1.0, "delete", False),
                                            ("s-5", True, "delete", False)):
            op = {"id": op_id, "type": op_type, "kind": "node", "entityId": "n1", "baseRevision": base}
            if op_type == "upsert":
                op["data"] = dict(NODE_A, x=1)
            if fresh:
                op["fresh"] = True
            result = self._ops([op])["results"][0]
            self.assertEqual((result["status"], result["reason"]), ("rejected", "baseRevision must be an integer revision"), op_id)

        # The stale writes never landed: the node stands where the
        # one well-stamped edit put it
        draft = json.loads(bearer(self.client.get, "/api/wayfind/buildings/b1/draft", self.token).content)
        self.assertEqual([n["x"] for n in draft["document"]["nodes"]], [9])

        # And an upsert whose ONLY licence is a malformed stamp is
        # still the whole-batch no_base refusal
        bare = {"id": "s-6", "type": "upsert", "kind": "node", "entityId": "n1", "data": NODE_A, "baseRevision": "1"}
        response = self._post_json("/api/wayfind/buildings/b1/ops", {"ops": [bare]})
        self.assertEqual((response.status_code, json.loads(response.content)["code"]), (400, "no_base"))

    def test_a_fresh_create_over_a_live_entity_is_a_conflict(self):
        self._ops([_op("op-1", "level", "l1", LEVEL), _op("op-2", "node", "n1", NODE_A)])
        clash = self._ops([_op("op-3", "node", "n1", NODE_B)])["results"][0]
        self.assertEqual((clash["status"], clash["reason"]), ("rejected", "conflict"))
        self.assertEqual(clash["current"]["data"]["kind"], NODE_A["kind"])
        self.assertFalse(clash["current"]["deleted"])

        # Keep-mine, the phone's way: the retry carries the revision
        # the conflict showed and overwrites exactly that copy
        kept = self._ops([_op("op-4", "node", "n1", NODE_B, base=clash["current"]["revision"])])
        self.assertEqual(kept["results"][0]["status"], "applied")

    def test_a_building_north_must_be_a_finite_number(self):
        # An Infinity token is not JSON at all: common/http's
        # get_json_object refuses the WHOLE body (for every API),
        # so it never reaches the northDeg check — still a 400,
        # and nothing is created
        response = bearer(self.client.post, "/api/wayfind/buildings", self.token,
                          data='{"id": "b2", "name": "Antras", "northDeg": Infinity}', content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(WfBuilding.objects.filter(id="b2").exists())
        # A non-finite-number northDeg that IS valid JSON still
        # answers the view's own code
        response = bearer(self.client.post, "/api/wayfind/buildings", self.token,
                          data='{"id": "b3", "name": "Trecias", "northDeg": true}', content_type="application/json")
        self.assertEqual((response.status_code, json.loads(response.content)["code"]), (400, "bad_north"))
        self._post_json("/api/wayfind/buildings", {"id": "b4", "name": "Ketvirtas", "entranceNodeId": {"x": 1}}, expect=400)

        rejected = self._ops([{"id": "op-n", "type": "building", "data": {"northDeg": True}}])["results"][0]
        self.assertEqual((rejected["status"], rejected["reason"]), ("rejected", "northDeg must be a number or null"))


class PublishTests(WayfindTestCase):

    def _seed_valid_draft(self):
        self._ops([
            _op("s-1", "level", "l1", LEVEL),
            _op("s-2", "node", "n1", NODE_A),
            _op("s-3", "node", "n2", NODE_B),
            _op("s-4", "edge", "e1", {"a": "n1", "b": "n2", "kind": "hallway"}),
            {"id": "s-5", "type": "building", "data": {"entranceNodeId": "n2"}},
        ])

    def test_the_validator_refuses_and_names_the_engine_codes(self):
        self._ops([
            _op("s-1", "level", "l1", LEVEL),
            _op("s-2", "node", "n1", NODE_A),
            _op("s-3", "edge", "e1", {"a": "n1", "b": "nera", "kind": "hallway"}),
        ])
        response = self._post_json("/api/wayfind/buildings/b1/publish", {})
        self.assertEqual(response.status_code, 422)
        body = json.loads(response.content)
        self.assertEqual(body["code"], "invalid")
        self.assertIn("dangling_edge", [issue["code"] for issue in body["issues"]])

    def test_publish_then_304_then_409_on_the_unchanged_draft(self):
        self._seed_valid_draft()
        published = json.loads(self._post_json("/api/wayfind/buildings/b1/publish",
                                               {"note": "pirmas"}, expect=200).content)

        # The document round-trips byte-for-byte under its ETag
        response = self.client.get("/api/wayfind/buildings/b1/graph")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["ETag"], f'"{published["etag"]}"')
        document = json.loads(response.content)
        self.assertEqual(document["entranceNodeId"], "n2")
        self.assertEqual(document["revision"], published["revision"])

        # A holder of the current bytes pays a 304 and no body
        again = self.client.get("/api/wayfind/buildings/b1/graph",
                                HTTP_IF_NONE_MATCH=response["ETag"])
        self.assertEqual(again.status_code, 304)

        # Nothing changed since — there is nothing new to hand out
        self._post_json("/api/wayfind/buildings/b1/publish", {}, expect=409)

        # …and the history names the publish
        versions = json.loads(bearer(self.client.get, "/api/wayfind/buildings/b1/versions",
                                     self.token).content)
        self.assertEqual([v["note"] for v in versions["versions"]], ["pirmas"])

    def test_an_unpublished_building_answers_404_not_an_empty_graph(self):
        response = self.client.get("/api/wayfind/buildings/b1/graph")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.content)["code"], "not_published")


class BuildingGateTests(WayfindTestCase):

    def test_the_slug_and_duplicate_gates(self):
        self._post_json("/api/wayfind/buildings", {"id": "Blogas!", "name": "X"}, expect=400)
        self._post_json("/api/wayfind/buildings", {"id": "b1", "name": "Dar kartą"}, expect=409)

    def test_the_listing_is_public_and_carries_the_publish_state(self):
        response = self.client.get("/api/wayfind/buildings")
        self.assertEqual(response.status_code, 200)
        row = json.loads(response.content)["buildings"][0]
        self.assertEqual((row["id"], row["publishedRevision"], row["etag"]), ("b1", None, None))

    def test_the_editor_surface_needs_the_role(self):
        student = create_user(username="studentas")
        student_token = auth.mint_session(student.id)
        self.assertEqual(bearer(self.client.get, "/api/wayfind/buildings/b1/draft",
                                student_token).status_code, 403)
        self.assertEqual(self._post_json("/api/wayfind/buildings/b1/ops",
                                         {"ops": [_op("x-1", "level", "l1", LEVEL)]},
                                         token=student_token).status_code, 403)


class CompileDeterminismTests(TestCase):

    def test_two_compiles_of_one_draft_are_byte_identical(self):
        building = {"id": "b1", "entrance_node_id": None, "north_deg": 12.5}
        # data rides as dicts, the shape the JSON column hands the
        # compiler; copies, because the compiler stamps ids in
        rows = [
            {"kind": "node", "id": "n2", "data": dict(NODE_B), "deleted": 0},
            {"kind": "level", "id": "l1", "data": dict(LEVEL), "deleted": 0},
            {"kind": "node", "id": "n1", "data": dict(NODE_A), "deleted": 0},
            {"kind": "node", "id": "gone", "data": dict(NODE_A), "deleted": 1},
        ]
        first = document_text(compile_document(building, rows))
        second = document_text(compile_document(building, list(reversed(rows))))
        self.assertEqual(first, second)
        # The tombstoned row never reaches the document
        self.assertNotIn("gone", first)
        # …and a clean document validates clean
        self.assertEqual(validate_document(json.loads(first)), [])
