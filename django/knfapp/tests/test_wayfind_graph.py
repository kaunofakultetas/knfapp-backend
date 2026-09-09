############################################################
#  [*] Regression tests — wayfind graph, ops, publish
#
#  What the map's edit-and-publish contract promises: op ids
#  are idempotent PER BUILDING (a duplicate answers what the
#  original did — `of`, reason, revision — and a fixed seed
#  id can still bootstrap a second building), a stale edit
#  is a per-op conflict carrying the entity as it stands
#  while the rest of the batch lands, a batch of rejections
#  bumps no revision, deletions are tombstones a ?since
#  delta carries, the validator refuses a publish with the
#  engine's own error codes, an unchanged draft cannot be
#  re-published, and the published document round-trips
#  byte-for-byte under its ETag (304 on If-None-Match).
############################################################


import json


from django.test import Client, TestCase


from knfapp.common import ratelimit
from knfapp.users import auth
from knfapp.wayfind.graph import compile_document, document_text, validate_document
from .utils import bearer, create_user


def _op(op_id, entity_kind, entity_id, data, base=None, op_type="upsert"):
    op = {"id": op_id, "type": op_type, "kind": entity_kind, "entityId": entity_id, "data": data}
    if base is not None:
        op["baseRevision"] = base
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
        self._ops([_op("op-2", "level", "l1", dict(LEVEL, label="Naujas"))])

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
