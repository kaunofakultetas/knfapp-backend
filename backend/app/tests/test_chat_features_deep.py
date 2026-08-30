############################################################
# test_chat_features_deep.py
############################################################
#
# The message features that arrived with migration v57 — a
# sender editing their own text, the system rows a group
# narrates itself with, and document attachments (a file the
# uploads route stores as sent, then a message that carries
# it). Everything runs through the HTTP surface with the
# socket fan-out swallowed.
#
# Covers:
#   - PUT /api/chat/conversations/<id>/messages/<mid>
#   - POST /api/chat/conversations (group opening line)
#   - DELETE /api/chat/conversations/<id> (leaver line)
#   - POST /api/uploads with kind=file
#   - POST /api/chat/conversations/<id>/messages {attachment}
#   - DELETE …/messages/<mid> — the attachment file goes too
############################################################

import io
import os
import uuid

import pytest

from app.auth.routes import _rate_limit_store
from app.uploads import routes as uploads_routes


CONVERSATIONS = "/api/chat/conversations"
UPLOADS = "/api/uploads"


@pytest.fixture(autouse=True)
def _isolate_module_state():
    uploads_routes._upload_dir = None
    _rate_limit_store.clear()
    yield
    uploads_routes._upload_dir = None
    _rate_limit_store.clear()


@pytest.fixture
def chat_routes(app):
    from app.chat import routes
    return routes


@pytest.fixture
def no_push(chat_routes, monkeypatch):
    calls = []
    monkeypatch.setattr(chat_routes._get_socketio(), "start_background_task",
                        lambda func, *args, **kwargs: calls.append((func, args, kwargs)))
    return calls


@pytest.fixture
def edits(monkeypatch):
    from app.chat import events
    calls = []
    monkeypatch.setattr(events, "emit_message_edited",
                        lambda socketio, conv_id, msg_id, text, edited_at:
                        calls.append((conv_id, msg_id, text, edited_at)))
    return calls


def _plant_conversation(db, member_ids, conv_type="group", title="Kursiokai"):
    conv_id = f"conv-{uuid.uuid4().hex[:8]}"
    now = "2026-01-01T10:00:00"
    db.execute(
        "INSERT INTO conversations (id, type, title, avatar_emoji, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conv_id, conv_type, None if conv_type == "direct" else title, None,
         member_ids[0], now, now),
    )
    for uid in member_ids:
        db.execute(
            "INSERT INTO conversation_participants (conversation_id, user_id, last_read_at)"
            " VALUES (?, ?, NULL)",
            (conv_id, uid),
        )
    db.commit()
    return conv_id


@pytest.fixture
def room(db, actor, make_user, auth_headers):
    user, headers = actor
    other = make_user(display_name="Ona Onaitė")
    conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="direct")
    return conv_id, user, headers, other, auth_headers(other)


class _Sent:
    # The send route wraps its payload as {"message": {...}};
    # this unwraps it while keeping the status for the 400 tests
    def __init__(self, response):
        self.status_code = response.status_code
        self._body = response.get_json()

    def get_json(self):
        if isinstance(self._body, dict) and isinstance(self._body.get("message"), dict):
            return self._body["message"]
        return self._body


def _send(client, headers, conv_id, **body):
    return _Sent(client.post(f"{CONVERSATIONS}/{conv_id}/messages", json=body, headers=headers))


def _leave(client, headers, conv_id):
    return client.delete(f"{CONVERSATIONS}/{conv_id}", headers=headers)


def _messages(client, headers, conv_id):
    response = client.get(f"{CONVERSATIONS}/{conv_id}/messages", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["messages"]


def _edit(client, headers, conv_id, msg_id, text):
    return client.put(f"{CONVERSATIONS}/{conv_id}/messages/{msg_id}", json={"text": text},
                      headers=headers)


def _upload(client, headers, name, content, kind="file"):
    data = {"file": (io.BytesIO(content), name)}
    if kind:
        data["kind"] = kind
    return client.post(UPLOADS, data=data, headers=headers, content_type="multipart/form-data")


PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer\n%%EOF\n"
DOCX = b"PK\x03\x04" + b"\x00" * 40 + b"word/document.xml"




############################################################
# TestEditMessage
############################################################

class TestEditMessage:

    def test_the_sender_rewrites_their_own_text_and_the_room_hears_it(self, client, room, no_push, edits):
        conv_id, user, headers, other, other_headers = room
        sent = _send(client, headers, conv_id, text="Labas").get_json()
        assert sent["editedAt"] is None and sent["kind"] == "text"

        response = _edit(client, headers, conv_id, sent["id"], "  Labas rytas  ")
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert set(body) == {"id", "text", "editedAt"}
        assert body["text"] == "Labas rytas" and body["editedAt"]

        row = [m for m in _messages(client, other_headers, conv_id) if m["id"] == sent["id"]][0]
        assert row["text"] == "Labas rytas"
        assert row["editedAt"] == body["editedAt"]
        assert edits == [(conv_id, sent["id"], "Labas rytas", body["editedAt"])]

    def test_a_reply_quote_follows_the_edit(self, client, room, no_push, edits):
        conv_id, user, headers, other, other_headers = room
        original = _send(client, headers, conv_id, text="Pirmas").get_json()
        _send(client, other_headers, conv_id, text="Atsakymas", replyToId=original["id"])
        assert _edit(client, headers, conv_id, original["id"], "Pirmas (pataisyta)").status_code == 200
        reply = [m for m in _messages(client, headers, conv_id) if m["replyTo"]][0]
        assert reply["replyTo"]["text"] == "Pirmas (pataisyta)"

    def test_an_outsider_is_refused(self, client, room, no_push, make_user, auth_headers):
        conv_id, user, headers, *_ = room
        sent = _send(client, headers, conv_id, text="Labas").get_json()
        stranger = auth_headers(make_user())
        assert _edit(client, stranger, conv_id, sent["id"], "x").status_code == 403

    def test_somebody_elses_message_is_refused(self, client, room, no_push):
        conv_id, user, headers, other, other_headers = room
        sent = _send(client, headers, conv_id, text="Labas").get_json()
        assert _edit(client, other_headers, conv_id, sent["id"], "x").status_code == 403
        assert _messages(client, headers, conv_id)[0]["text"] == "Labas"

    @pytest.mark.parametrize("text", ["", "   ", "x" * 5001, 42, None])
    def test_a_bad_body_is_refused(self, client, room, no_push, text):
        conv_id, user, headers, *_ = room
        sent = _send(client, headers, conv_id, text="Labas").get_json()
        assert _edit(client, headers, conv_id, sent["id"], text).status_code == 400

    def test_an_unknown_message_is_404(self, client, room):
        conv_id, user, headers, *_ = room
        assert _edit(client, headers, conv_id, "nope", "x").status_code == 404

    def test_an_unsent_message_is_409(self, client, room, no_push):
        conv_id, user, headers, *_ = room
        sent = _send(client, headers, conv_id, text="Labas").get_json()
        assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{sent['id']}",
                             headers=headers).status_code == 200
        assert _edit(client, headers, conv_id, sent["id"], "x").status_code == 409

    def test_a_file_message_cannot_be_edited(self, client, room, no_push):
        conv_id, user, headers, *_ = room
        up = _upload(client, headers, "planas.pdf", PDF).get_json()
        sent = _send(client, headers, conv_id, text="",
                     attachment={"url": up["url"], "name": up["name"],
                                 "size": up["size"], "mime": up["mime"]}).get_json()
        assert sent["kind"] == "file"
        assert _edit(client, headers, conv_id, sent["id"], "x").status_code == 400




############################################################
# TestSystemMessages
############################################################

class TestSystemMessages:

    def test_a_new_group_opens_with_its_own_first_line(self, client, actor, make_user, no_push):
        user, headers = actor
        other = make_user()
        response = client.post(CONVERSATIONS, json={
            "participantIds": [other["id"]], "type": "group", "title": "Kursiokai 2026",
        }, headers=headers)
        assert response.status_code == 201, response.get_json()
        conv_id = response.get_json()["conversationId"]
        rows = _messages(client, headers, conv_id)
        assert len(rows) == 1
        first = rows[0]
        assert first["kind"] == "system"
        assert "sukūrė grupę" in first["text"] and "Kursiokai 2026" in first["text"]
        assert first["senderId"] == user["id"]
        # The conversation list previews it too
        listed = client.get(CONVERSATIONS, headers=headers).get_json()["conversations"]
        mine = [c for c in listed if c["id"] == conv_id][0]
        assert mine["lastMessage"]["kind"] == "system"

    def test_a_direct_chat_opens_blank(self, client, actor, make_user, no_push):
        user, headers = actor
        other = make_user()
        response = client.post(CONVERSATIONS, json={
            "participantIds": [other["id"]], "type": "direct",
        }, headers=headers)
        assert response.status_code == 201
        assert _messages(client, headers, response.get_json()["conversationId"]) == []

    def test_leaving_a_group_narrates_it_to_those_who_stay(self, client, db, actor, make_user, auth_headers, no_push):
        user, headers = actor
        a, b = make_user(display_name="Ona"), make_user()
        conv_id = _plant_conversation(db, [user["id"], a["id"], b["id"]])
        response = _leave(client, auth_headers(a), conv_id)
        assert response.status_code == 200, response.get_json()
        rows = _messages(client, headers, conv_id)
        assert [r["kind"] for r in rows] == ["system"]
        assert rows[0]["text"] == "Ona paliko pokalbį"
        assert rows[0]["senderId"] == a["id"]

    def test_leaving_a_direct_chat_stays_silent(self, client, db, actor, make_user, auth_headers, no_push):
        user, headers = actor
        other = make_user()
        conv_id = _plant_conversation(db, [user["id"], other["id"]], conv_type="direct")
        assert _leave(client, auth_headers(other), conv_id).status_code == 200
        assert _messages(client, headers, conv_id) == []

    def test_the_last_member_leaving_drops_the_room_without_a_line(self, client, db, actor, no_push):
        user, headers = actor
        conv_id = _plant_conversation(db, [user["id"]])
        assert _leave(client, headers, conv_id).status_code == 200
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (conv_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0] == 0




############################################################
# TestDocuments
############################################################

class TestDocuments:

    @pytest.mark.parametrize("name,content,mime", [
        ("planas.pdf", PDF, "application/pdf"),
        ("Rašto darbas.docx", DOCX,
         "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("pastabos.txt", "Labas, pasaulis — ąčęėįšųūž\n".encode(), "text/plain"),
        ("archyvas.zip", DOCX, "application/zip"),
    ])
    def test_a_document_is_stored_as_sent(self, client, app, actor, name, content, mime):
        user, headers = actor
        response = _upload(client, headers, name, content)
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert set(body) == {"url", "filename", "name", "size", "mime"}
        assert body["size"] == len(content) and body["mime"] == mime
        assert body["name"] and not body["name"].startswith(".")
        with open(os.path.join(app.config["UPLOAD_DIR"], body["filename"]), "rb") as handle:
            assert handle.read() == content

    def test_a_photo_upload_still_reports_name_size_mime(self, client, actor):
        from PIL import Image
        user, headers = actor
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (123, 0, 63)).save(buffer, format="PNG")
        response = _upload(client, headers, "nuotrauka.png", buffer.getvalue(), kind=None)
        assert response.status_code == 201, response.get_json()
        body = response.get_json()
        assert body["mime"].startswith("image/") and body["size"] > 0 and body["name"] == "nuotrauka.png"

    @pytest.mark.parametrize("name,content,code", [
        ("virus.exe", b"MZ\x90\x00", "bad_file_type"),
        ("planas.pdf", b"not a pdf at all", "bad_file_content"),
        ("darbas.docx", b"plain bytes", "bad_file_content"),
        ("pastabos.txt", b"MZ\x00\x00binary", "bad_file_content"),
        ("nuotrauka.png", b"\x89PNG\r\n\x1a\n", "bad_file_type"),
    ])
    def test_a_document_that_lies_is_refused(self, client, app, actor, name, content, code):
        user, headers = actor
        response = _upload(client, headers, name, content)
        assert response.status_code == 400
        assert response.get_json()["code"] == code
        assert os.listdir(app.config["UPLOAD_DIR"]) == []

    def test_a_document_is_served_and_deletable(self, client, actor):
        user, headers = actor
        body = _upload(client, headers, "planas.pdf", PDF).get_json()
        served = client.get(body["url"])
        assert served.status_code == 200 and served.data == PDF
        assert client.delete(body["url"], headers=headers).status_code == 200
        assert client.get(body["url"]).status_code == 404

    def test_a_file_message_round_trips_with_a_file_preview(self, client, room, no_push):
        conv_id, user, headers, other, other_headers = room
        up = _upload(client, headers, "planas.pdf", PDF).get_json()
        attachment = {"url": up["url"], "name": up["name"], "size": up["size"], "mime": up["mime"]}
        response = _send(client, headers, conv_id, text="", attachment=attachment)
        assert response.status_code == 201, response.get_json()
        sent = response.get_json()
        assert sent["kind"] == "file" and sent["attachment"] == attachment and sent["text"] == ""

        seen = [m for m in _messages(client, other_headers, conv_id) if m["id"] == sent["id"]][0]
        assert seen["attachment"] == attachment and seen["kind"] == "file"

        listed = client.get(CONVERSATIONS, headers=other_headers).get_json()["conversations"]
        assert [c for c in listed if c["id"] == conv_id][0]["lastMessage"]["kind"] == "file"

        assert no_push, "the other member is pushed"
        func, args, kwargs = no_push[-1]
        pushed = [a for a in args if isinstance(a, dict)]
        assert any(d.get("preview") == "file" for d in pushed) or "Failas" in repr(args)

    def test_unsending_a_file_message_removes_the_file(self, client, app, room, no_push):
        conv_id, user, headers, *_ = room
        up = _upload(client, headers, "planas.pdf", PDF).get_json()
        sent = _send(client, headers, conv_id, text="", attachment={
            "url": up["url"], "name": up["name"], "size": up["size"], "mime": up["mime"],
        }).get_json()
        assert client.delete(f"{CONVERSATIONS}/{conv_id}/messages/{sent['id']}",
                             headers=headers).status_code == 200
        row = [m for m in _messages(client, headers, conv_id) if m["id"] == sent["id"]][0]
        assert row["deleted"] is True and row["attachment"] is None
        assert client.get(up["url"]).status_code == 404
        assert os.listdir(app.config["UPLOAD_DIR"]) == []

    @pytest.mark.parametrize("attachment", [
        "not a dict",
        {"url": "https://evil.example/x.pdf", "name": "x.pdf", "size": 1, "mime": "application/pdf"},
        {"url": "/api/uploads/" + "a" * 32 + ".pdf", "name": "", "size": 1, "mime": "application/pdf"},
        {"url": "/api/uploads/" + "a" * 32 + ".pdf", "name": "x.pdf", "size": -1, "mime": "application/pdf"},
        {"url": "/api/uploads/" + "a" * 32 + ".pdf", "name": "x.pdf", "size": "big", "mime": "application/pdf"},
        {"url": "/api/uploads/" + "a" * 32 + ".pdf", "name": "n" * 201, "size": 1, "mime": "application/pdf"},
        {"url": "/api/uploads/" + "a" * 32 + ".pdf", "name": "x.pdf", "size": 1, "mime": "m" * 101},
    ])
    def test_a_bad_attachment_is_refused(self, client, room, no_push, attachment):
        conv_id, user, headers, *_ = room
        response = _send(client, headers, conv_id, text="", attachment=attachment)
        assert response.status_code == 400, response.get_json()
        assert _messages(client, headers, conv_id) == []
