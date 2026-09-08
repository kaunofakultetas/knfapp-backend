# -----------------------------------------------------------
#  [*] Tests — wayfind op batches (app/wayfind/routes.py)
#
#  What the op log's idempotency and serialisation promise,
#  proved end to end through the real routes:
#
#    - a replayed op whose answer was lost tells the phone
#      what the original did: duplicate + of='applied' with
#      the revision it applied at, or of='rejected' with the
#      reason — never a bare 'duplicate' the outbox would
#      have to guess about.
#    - an op id is idempotent PER BUILDING (the log key is
#      "<building>:<id>"), so the client's fixed seed ids can
#      bootstrap a second building instead of answering 25
#      duplicates that apply nothing.
#    - two overlapping batches serialise on the building
#      row's write lock: exactly one edit of a contested
#      entity lands, the other is a conflict carrying the
#      winner's row — never a silent overwrite where both are
#      told 'applied' under one revision.
# -----------------------------------------------------------

import threading
import time




# -----------------------------------------------------------
# _seed_building
# -----------------------------------------------------------
#
# A building with one level and one node, applied through the
# real ops route so every row carries a true revision (1).
# Answers the ids it used.
#
# Used by:
#   - every test below
# -----------------------------------------------------------

LEVEL = {"label": "L1", "viewBox": [0, 0, 100, 100], "metersPerPixel": 0.1, "ordinal": 0}


def _seed_building(client, headers, building_id="b1"):
    response = client.post("/api/wayfind/buildings", json={"id": building_id, "name": "B"}, headers=headers)
    assert response.status_code == 201, response.get_json()
    response = client.post(f"/api/wayfind/buildings/{building_id}/ops", headers=headers, json={"ops": [
        {"id": "s-level", "type": "upsert", "kind": "level", "entityId": "L1", "data": LEVEL},
        {"id": "s-node", "type": "upsert", "kind": "node", "entityId": "n1", "data": {"level": "L1", "x": 0, "y": 0, "kind": "corridor"}},
    ]})
    body = response.get_json()
    assert [r["status"] for r in body["results"]] == ["applied", "applied"], body
    assert body["revision"] == 1
    return building_id




# -----------------------------------------------------------
# replayed ops — the duplicate answer says what happened
# -----------------------------------------------------------

def test_duplicate_of_applied_carries_of_and_revision(client, admin):
    _, headers = admin
    _seed_building(client, headers)

    op = {"id": "b1-edit", "type": "upsert", "kind": "node", "entityId": "n1",
          "data": {"level": "L1", "x": 5, "y": 5, "kind": "door"}, "baseRevision": 1}
    first = client.post("/api/wayfind/buildings/b1/ops", headers=headers, json={"ops": [op]}).get_json()
    assert first["results"][0]["status"] == "applied"
    assert first["revision"] == 2

    # The answer was lost; the phone re-sends the same batch.
    # The duplicate must carry of='applied' and the revision
    # the op applied at, so the editor can re-stamp the entity
    replay = client.post("/api/wayfind/buildings/b1/ops", headers=headers, json={"ops": [op]}).get_json()
    result = replay["results"][0]
    assert result["status"] == "duplicate"
    assert result["of"] == "applied"
    assert result["revision"] == 2
    assert result["reason"] is None
    # ... and nothing was re-applied
    assert replay["revision"] == 2


def test_duplicate_of_rejected_carries_the_reason(client, admin):
    _, headers = admin
    _seed_building(client, headers)

    # Another editor moved n1 (revision 2), so a base-1 edit
    # is a conflict — logged as rejected
    other = {"id": "other", "type": "upsert", "kind": "node", "entityId": "n1",
             "data": {"level": "L1", "x": 9, "y": 9, "kind": "corridor"}, "baseRevision": 1}
    assert client.post("/api/wayfind/buildings/b1/ops", headers=headers, json={"ops": [other]}).get_json()["results"][0]["status"] == "applied"

    stale = {"id": "stale", "type": "upsert", "kind": "node", "entityId": "n1",
             "data": {"level": "L1", "x": 1, "y": 1, "kind": "door"}, "baseRevision": 1}
    first = client.post("/api/wayfind/buildings/b1/ops", headers=headers, json={"ops": [stale]}).get_json()["results"][0]
    assert first["status"] == "rejected" and first["reason"] == "conflict"

    # The replay must NOT read as success: of='rejected' with
    # the original reason, revision null (nothing applied)
    replay = client.post("/api/wayfind/buildings/b1/ops", headers=headers, json={"ops": [stale]}).get_json()["results"][0]
    assert replay["status"] == "duplicate"
    assert replay["of"] == "rejected"
    assert replay["reason"] == "conflict"
    assert replay["revision"] is None




# -----------------------------------------------------------
# op ids are building-scoped
# -----------------------------------------------------------

def test_same_op_id_bootstraps_a_second_building(client, admin):
    _, headers = admin
    _seed_building(client, headers, "first")

    # The client's bootstrap uses fixed ids (seed-N); a second
    # building seeded with the same ids must apply, not answer
    # duplicates that leave it empty
    assert client.post("/api/wayfind/buildings", json={"id": "second", "name": "S"}, headers=headers).status_code == 201
    seed = {"id": "s-level", "type": "upsert", "kind": "level", "entityId": "L1", "data": LEVEL}
    body = client.post("/api/wayfind/buildings/second/ops", headers=headers, json={"ops": [seed]}).get_json()
    assert body["results"][0] == {"id": "s-level", "status": "applied"}
    assert body["revision"] == 1

    draft = client.get("/api/wayfind/buildings/second/draft", headers=headers).get_json()
    assert [level["id"] for level in draft["document"]["levels"]] == ["L1"]
    assert draft["revisions"] == {"level:L1": 1}

    # ... while the id stays idempotent within its own building
    again = client.post("/api/wayfind/buildings/second/ops", headers=headers, json={"ops": [seed]}).get_json()
    assert again["results"][0]["status"] == "duplicate"
    assert again["results"][0]["of"] == "applied"




# -----------------------------------------------------------
# overlapping batches serialise
# -----------------------------------------------------------

def test_overlapping_batches_cannot_both_edit_one_entity(app, client, admin):
    _, headers = admin
    _seed_building(client, headers)

    # A long batch whose FIRST op edits n1, so its transaction
    # is open while the rival's single-op batch arrives
    long_batch = [{"id": "race-A-0", "type": "upsert", "kind": "node", "entityId": "n1",
                   "data": {"level": "L1", "x": 100, "y": 0, "kind": "corridor"}, "baseRevision": 1}]
    long_batch += [{"id": f"race-A-{index}", "type": "upsert", "kind": "node", "entityId": f"pad{index}",
                    "data": {"level": "L1", "x": index, "y": 0, "kind": "corridor"}} for index in range(1, 500)]
    rival = [{"id": "race-B-0", "type": "upsert", "kind": "node", "entityId": "n1",
              "data": {"level": "L1", "x": 200, "y": 0, "kind": "corridor"}, "baseRevision": 1}]

    answers = {}

    def post(name, ops, delay):
        if delay:
            time.sleep(delay)
        # Each thread gets its own client — the WSGI test
        # client is not thread-safe to share
        answers[name] = app.test_client().post("/api/wayfind/buildings/b1/ops", headers=headers, json={"ops": ops}).get_json()

    threads = [threading.Thread(target=post, args=("A", long_batch, 0)),
               threading.Thread(target=post, args=("B", rival, 0.02))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one of the two n1 edits landed; the loser was
    # told 'conflict' and handed the winner's row — never two
    # 'applied' answers hiding a silent overwrite
    a_result = answers["A"]["results"][0]
    b_result = answers["B"]["results"][0]
    statuses = sorted([a_result["status"], b_result["status"]])
    assert statuses == ["applied", "rejected"], (a_result, b_result)
    loser = a_result if a_result["status"] == "rejected" else b_result
    assert loser["reason"] == "conflict"
    assert loser["current"]["data"]["x"] in (100, 200)

    winner_x = 100 if a_result["status"] == "applied" else 200
    draft = client.get("/api/wayfind/buildings/b1/draft", headers=headers).get_json()
    node = next(node for node in draft["document"]["nodes"] if node["id"] == "n1")
    assert node["x"] == winner_x
