# -----------------------------------------------------------
#  [*] Tests — app/uploads/routes.py, the READ and REMOVE half
#
#  The exhaustive pass over four functions: serve_file,
#  delete_file, delete_upload and sweep_orphan_uploads. The
#  upload half (upload_file, _reencode_image, the magic-byte
#  sniff) is another file's slice and is not touched here.
#
#  What this module proves, beyond the broad suite:
#
#    - the filename gate is TWO arms, not one: secure_filename
#      can empty a name outright ("...", "日本語") and the
#      pattern can refuse what survives — both answer 400, and
#      both answer it before the filesystem or the database is
#      reached at all
#    - secure_filename NORMALISES as well as strips, so
#      "..\<name>", " <name> " and a full-width digit all
#      collapse onto a real stored file. That is safe — the
#      result is still matched against the pattern and joined
#      onto UPLOAD_DIR — but it means the ownership check in
#      delete_file has to run on the NORMALISED name, and it
#      does
#    - the served response is a real static file: ranges,
#      ETags, conditional 304s, inline disposition, byte-exact
#      bodies, and a browser-only 24 h cache
#    - every (row, role) permutation of delete_file — absent
#      row, own row, someone else's row, a row whose owner
#      account is gone — against student, teacher, curator and
#      admin
#    - delete_upload is the helper with NO ownership check and
#      no app context requirement for values it refuses, and
#      its True/False means "a file was unlinked", nothing else
#    - the sweep's boundary is exact (mtime == cutoff is
#      swept), it does not recurse, it is not gated by the
#      stored-name pattern, and it counts FILES not rows
#
#  Everything is driven through planted files and seeded rows
#  rather than the upload route, so nothing here depends on
#  Pillow, the rate limiter or the quota.
# -----------------------------------------------------------

import os
import shutil
import time
import uuid
from pathlib import Path

import pytest
import time_machine

from app.auth.routes import _rate_limit_store
from app.uploads import routes as uploads_routes
from app.uploads.routes import (
    ORPHAN_GRACE_SECONDS,
    delete_upload,
    sweep_orphan_uploads,
)

UPLOADS = "/api/uploads"

# A stored name is a uuid4 hex plus a canonical extension;
# this literal one is used wherever the exact characters
# matter (normalisation, boundaries, near misses)
HEX = "0123456789abcdef0123456789abcdef"

# The five extensions _FILENAME_RE admits. Note bmp/tif/tiff
# are uploadABLE (ALLOWED_EXTENSIONS) yet never servable —
# the re-encode never writes them
SERVABLE = ("jpg", "jpeg", "png", "gif", "webp",
            # documents (kind=file uploads) since v57
            "pdf", "docx", "xlsx", "pptx", "zip", "txt")




# -----------------------------------------------------------
# _isolate_module_state
# -----------------------------------------------------------
#
# uploads/routes.py caches UPLOAD_DIR in a module global for
# the life of the PROCESS, but every test gets its own tmp
# directory — without this reset the second test in the
# session would read and unlink inside the first one's
# directory. The rate-limit store is process-global too, and
# create_app's per-IP budget spends from it on every request
# this client makes from 127.0.0.1.
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_module_state():
    uploads_routes._upload_dir = None
    _rate_limit_store.clear()
    yield
    uploads_routes._upload_dir = None
    _rate_limit_store.clear()




# -----------------------------------------------------------
# _name / _stored / _plant
# -----------------------------------------------------------
#
# serve_file, delete_file and the sweep never decode anything,
# so a "stored file" here is whatever bytes the test needs
# under a name the module could have written. Planting
# directly keeps this file independent of the upload route,
# Pillow, the quota and the rate limiter.
# -----------------------------------------------------------

def _name(ext="png"):
    return f"{uuid.uuid4().hex}.{ext}"


def _stored(app, filename):
    return os.path.join(app.config["UPLOAD_DIR"], filename)


def _plant(app, body=b"paveiksliuko baitai", ext="png", filename=None):
    filename = filename or _name(ext)
    with open(_stored(app, filename), "wb") as handle:
        handle.write(body)
    return filename


def _on_disk(app):
    return set(os.listdir(app.config["UPLOAD_DIR"]))




# -----------------------------------------------------------
# _own / _rows / _used
# -----------------------------------------------------------
#
# The uploads row (migration v43) is the ownership record
# delete_file authorises against and the quota counts, so a
# test that cares about either seeds it by hand. user_id is
# nullable on purpose: ON DELETE SET NULL leaves exactly that
# shape behind when an owner's account goes away.
# -----------------------------------------------------------

def _own(db, user_id, filename, byte_size=0):
    db.execute(
        "INSERT INTO uploads (id, filename, user_id, byte_size, created_at)"
        " VALUES (?, ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (str(uuid.uuid4()), filename, user_id, byte_size),
    )
    db.commit()
    return filename


def _rows(db):
    return {row["filename"] for row in db.execute("SELECT filename FROM uploads")}


def _used(db, user_id):
    return db.execute(
        "SELECT COALESCE(SUM(byte_size), 0) AS total FROM uploads WHERE user_id = ?",
        (user_id,),
    ).fetchone()["total"]




# -----------------------------------------------------------
# _age
# -----------------------------------------------------------
#
# Backdates a file past the sweep's grace period without any
# wall-clock sleeping — the mtime is the only thing the sweep
# reads. follow_symlinks is exposed because a symlink test
# has to age the link, not what it points at.
# -----------------------------------------------------------

def _age(path, seconds=ORPHAN_GRACE_SECONDS + 60, follow_symlinks=True):
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=follow_symlinks)








# -----------------------------------------------------------
# serve_file — the filename gate
# -----------------------------------------------------------

@pytest.mark.parametrize("segment", [
    "...",                                  # secure_filename strips it to ""
    "..",
    ".",
    "_",
    "___",
    "%20",                                  # a lone space
    "%E6%97%A5%E6%9C%AC%E8%AA%9E",          # 日本語 — no ascii survives
    "%CE%B1%CE%B2%CE%B3",                   # αβγ
])
def test_a_name_secure_filename_empties_is_refused_as_a_bad_filename(client, segment):
    # The FIRST arm of the guard — `not safe_name` — which the
    # pattern never gets to see
    response = client.get(f"{UPLOADS}/{segment}")

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid filename", "code": "bad_filename"}


@pytest.mark.parametrize("ext", ["bmp", "tif", "tiff", "svg", "avif", "heic", "ico", "jpe", "jfif", "doc", "exe", "html", "part"])
def test_only_the_stored_name_patterns_extensions_are_ever_servable(client, app, ext):
    # bmp/tif/tiff are accepted by the UPLOAD gate and still
    # unservable: the re-encode never writes one, so a name
    # carrying that extension cannot be ours
    _plant(app, filename=f"{HEX}.{ext}")

    response = client.get(f"{UPLOADS}/{HEX}.{ext}")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_filename"


@pytest.mark.parametrize("ext", SERVABLE)
def test_every_extension_the_stored_name_pattern_admits_is_servable(client, app, ext):
    body = f"turinys-{ext}".encode()
    name = _plant(app, body=body, ext=ext)

    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 200
    assert response.data == body


@pytest.mark.parametrize("stem, expected", [
    (HEX[:31], 400),                        # 31 hex — one short
    (HEX, 200),                             # exactly 32
    (HEX + "0", 400),                       # 33 — one over
    ("g" * 32, 400),                        # 32 chars, not hex
    (HEX[:31] + "g", 400),                  # one non-hex character
    ("f" * 32, 200),                        # the top of the range
    ("0" * 32, 200),                        # the bottom of it
])
def test_the_stem_must_be_exactly_thirty_two_lowercase_hex_characters(client, app, stem, expected):
    _plant(app, filename=f"{stem}.png")

    response = client.get(f"{UPLOADS}/{stem}.png")

    assert response.status_code == expected


def test_an_absurdly_long_name_is_refused_without_reaching_the_filesystem(client):
    # 4000 characters would be ENAMETOOLONG at the syscall; the
    # pattern answers first
    response = client.get(f"{UPLOADS}/{'a' * 4000}.png")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_filename"


def test_a_null_byte_in_the_name_is_refused_rather_than_crashing(client):
    # os.path.isfile() raises ValueError on an embedded NUL —
    # secure_filename strips it and the pattern refuses what is
    # left, so the route never gets there
    response = client.get(f"{UPLOADS}/{HEX}.png%00.txt")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_filename"


def test_a_part_file_from_an_interrupted_write_is_never_served(client, app):
    leftover = _plant(app, body=b"pusiau irasyta", filename=f"{HEX}.png.part")

    response = client.get(f"{UPLOADS}/{leftover}")

    assert response.status_code == 400
    assert os.path.isfile(_stored(app, leftover)), "only the sweep collects a .part file"


def test_the_name_gate_runs_before_the_upload_directory_is_resolved(client, monkeypatch):
    def _never(*args, **kwargs):
        raise AssertionError("a refused name must not touch the filesystem")

    monkeypatch.setattr(uploads_routes, "_get_upload_dir", _never)

    assert client.get(f"{UPLOADS}/labas.png").status_code == 400


def test_a_windows_style_traversal_prefix_collapses_onto_the_plain_name(client, app):
    # secure_filename NORMALISES: a backslash is not a posix
    # separator, so it is stripped and the leading dots go with
    # it. The result is the plain stored name — still matched
    # against the pattern, still joined onto UPLOAD_DIR, so
    # this reads the file it names and nothing else
    body = b"tikras failas"
    name = _plant(app, body=body)

    response = client.get(f"{UPLOADS}/..%5C{name}")

    assert response.status_code == 200
    assert response.data == body


def test_a_whitespace_padded_name_serves_the_same_file(client, app):
    body = b"apkarpytas vardas"
    name = _plant(app, body=body)

    response = client.get(f"{UPLOADS}/%20{name}%20")

    assert response.status_code == 200
    assert response.data == body


def test_a_full_width_digit_normalises_onto_a_real_stored_name(client, app):
    # NFKD folds U+FF10 FULLWIDTH DIGIT ZERO to "0", so a name
    # no client could have been given still resolves
    body = b"placiaraidis"
    _plant(app, body=body, filename=f"{HEX}.png")

    response = client.get(f"{UPLOADS}/%EF%BC%90{HEX[1:]}.png")

    assert response.status_code == 200
    assert response.data == body


def test_a_question_mark_encoded_into_the_name_is_refused(client, app):
    name = _plant(app)

    response = client.get(f"{UPLOADS}/{name}%3Fv=2")

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_filename"


def test_a_real_query_string_does_not_disturb_the_lookup(client, app):
    body = b"su uzklausa"
    name = _plant(app, body=body)

    response = client.get(f"{UPLOADS}/{name}?v=2&cachebust=17")

    assert response.status_code == 200
    assert response.data == body


def test_the_collection_path_has_no_get(client):
    # POST-only; a GET must never be read as "the file called
    # nothing"
    response = client.get(UPLOADS)

    assert response.status_code == 405


def test_a_trailing_slash_is_a_different_path_entirely(client, app):
    name = _plant(app)

    response = client.get(f"{UPLOADS}/{name}/")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Not found"}, "routing answered, not serve_file"








# -----------------------------------------------------------
# serve_file — what actually comes back
# -----------------------------------------------------------

def test_a_stored_file_comes_back_byte_for_byte(client, app):
    # Every byte value, so nothing on the way out re-encodes,
    # escapes or truncates a real image
    body = bytes(range(256)) * 4
    name = _plant(app, body=body)

    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 200
    assert response.data == body
    assert int(response.headers["Content-Length"]) == len(body)


def test_a_zero_byte_stored_file_is_served_as_an_empty_body(client, app):
    name = _plant(app, body=b"")

    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 200
    assert response.data == b""
    assert response.headers["Content-Length"] == "0"


def test_a_head_request_gets_the_headers_and_no_body(client, app):
    body = b"antrastes tik"
    name = _plant(app, body=body)

    response = client.head(f"{UPLOADS}/{name}")

    assert response.status_code == 200
    assert response.data == b""
    assert int(response.headers["Content-Length"]) == len(body)
    assert response.headers["Content-Type"] == "image/png"


def test_a_range_request_gets_exactly_those_bytes(client, app):
    body = bytes(range(64))
    name = _plant(app, body=body)

    response = client.get(f"{UPLOADS}/{name}", headers={"Range": "bytes=8-15"})

    assert response.status_code == 206
    assert response.data == body[8:16]
    assert response.headers["Content-Range"] == f"bytes 8-15/{len(body)}"
    assert response.headers["Accept-Ranges"] == "bytes"


def test_a_range_past_the_end_of_the_file_is_refused(client, app):
    body = b"trumpas"
    name = _plant(app, body=body)

    response = client.get(f"{UPLOADS}/{name}", headers={"Range": "bytes=900-999"})

    assert response.status_code == 416
    assert response.headers["Content-Range"] == f"bytes */{len(body)}"


def test_the_etag_is_stable_while_the_bytes_are(client, app):
    name = _plant(app, body=b"nekintantis")

    first = client.get(f"{UPLOADS}/{name}")
    second = client.get(f"{UPLOADS}/{name}")

    assert first.headers["ETag"] == second.headers["ETag"]


def test_the_etag_changes_when_the_stored_bytes_do(client, app):
    name = _plant(app, body=b"pirma")
    first = client.get(f"{UPLOADS}/{name}").headers["ETag"]

    _plant(app, body=b"antra ir gerokai ilgesne", filename=name)

    assert client.get(f"{UPLOADS}/{name}").headers["ETag"] != first


def test_a_stale_etag_gets_the_bytes_again_not_a_304(client, app):
    body = b"naujausia versija"
    name = _plant(app, body=body)

    response = client.get(f"{UPLOADS}/{name}", headers={"If-None-Match": '"senas-etagas"'})

    assert response.status_code == 200
    assert response.data == body


def test_an_if_none_match_star_gets_a_304(client, app):
    name = _plant(app)

    response = client.get(f"{UPLOADS}/{name}", headers={"If-None-Match": "*"})

    assert response.status_code == 304


def test_an_if_modified_since_at_the_files_own_timestamp_gets_a_304(client, app):
    name = _plant(app)
    last_modified = client.get(f"{UPLOADS}/{name}").headers["Last-Modified"]

    response = client.get(f"{UPLOADS}/{name}", headers={"If-Modified-Since": last_modified})

    assert response.status_code == 304
    assert response.data == b""


def test_the_image_is_served_inline_never_as_a_download(client, app):
    name = _plant(app)

    response = client.get(f"{UPLOADS}/{name}")

    assert response.headers["Content-Disposition"].startswith("inline")


def test_the_answer_is_cached_by_the_browser_and_never_by_a_shared_proxy(client, app):
    name = _plant(app)

    response = client.get(f"{UPLOADS}/{name}")

    cache_control = response.headers["Cache-Control"]
    assert "private" in cache_control and "public" not in cache_control
    assert "max-age=86400" in cache_control
    assert "no-store" not in cache_control
    assert response.headers["Expires"], "max_age also emits an HTTP/1.0 Expires"


def test_the_api_wide_pragma_header_stays_off_a_cacheable_image(client, app):
    # create_app setdefaults Pragma: no-cache on /api/ answers,
    # but only where the effective Cache-Control forbids storing.
    # serve_file ships its own max-age, so the HTTP/1.0 header is
    # withheld rather than telling an old cache the opposite
    name = _plant(app)

    response = client.get(f"{UPLOADS}/{name}")

    assert "Pragma" not in response.headers
    assert "max-age=86400" in response.headers["Cache-Control"]


def test_the_public_route_ignores_an_authorization_header(client, app):
    # No require_auth at all, so a garbage bearer is not even
    # looked at, let alone refused
    body = b"viesas"
    name = _plant(app, body=body)

    response = client.get(f"{UPLOADS}/{name}", headers={"Authorization": "Bearer visiska-nesamone"})

    assert response.status_code == 200
    assert response.data == body


def test_a_file_no_uploads_row_claims_is_still_served(client, app, db):
    # Pre-v43 files have no ownership row; serving is not gated
    # by one
    name = _plant(app)

    assert client.get(f"{UPLOADS}/{name}").status_code == 200
    assert _rows(db) == set()


def test_a_file_someone_else_owns_is_served_to_an_anonymous_viewer(client, app, db, actor):
    user, _ = actor
    name = _own(db, user["id"], _plant(app))

    assert client.get(f"{UPLOADS}/{name}").status_code == 200


def test_a_directory_shaped_like_a_stored_name_is_a_404(client, app):
    name = _name("gif")
    os.makedirs(_stored(app, name))

    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 404
    assert response.get_json() == {"error": "File not found"}


def test_a_file_that_vanishes_after_the_isfile_check_is_still_a_404(client, app, monkeypatch):
    # The TOCTOU window between serve_file's own isfile() and
    # send_from_directory: werkzeug re-checks and raises
    # NotFound, so the app-wide handler answers instead of
    # serve_file's own message. No 500 either way
    name = _plant(app)
    real_send = uploads_routes.send_from_directory

    def _vanishing(directory, path, **kwargs):
        os.unlink(os.path.join(directory, path))
        return real_send(directory, path, **kwargs)

    monkeypatch.setattr(uploads_routes, "send_from_directory", _vanishing)

    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 404
    assert response.get_json() == {"error": "Not found"}


def test_a_symlink_pointing_out_of_the_upload_directory_is_not_served(client, app):
    # The name gate, os.path.isfile() and send_from_directory's
    # safe_join all judge the NAME; only serve_file's realpath
    # check looks at what the link resolves to
    name = _name("jpg")
    os.symlink(app.config["DB_PATH"], _stored(app, name))

    response = client.get(f"{UPLOADS}/{name}")

    assert response.status_code == 404, "the database must not be readable through the image route"
    assert response.get_json() == {"error": "File not found"}
    assert os.path.lexists(_stored(app, name)), "serving refused it, nothing removed it"








# -----------------------------------------------------------
# DELETE /api/uploads/<filename> — every (row, role) pair
# -----------------------------------------------------------

# "none" is a pre-v43 file with no ownership row, "null" is a
# row whose owner account was deleted (ON DELETE SET NULL) —
# both are reachable states, and only an admin gets past them
@pytest.mark.parametrize("owner, role, expected", [
    ("none", "student", 404),
    ("none", "teacher", 404),
    ("none", "curator", 404),
    ("none", "admin", 200),
    ("self", "student", 200),
    ("self", "teacher", 200),
    ("self", "curator", 200),
    ("self", "admin", 200),
    ("other", "student", 403),
    ("other", "teacher", 403),
    ("other", "curator", 403),
    ("other", "admin", 200),
    ("null", "student", 403),
    ("null", "teacher", 403),
    ("null", "curator", 403),
    ("null", "admin", 200),
])
def test_who_may_delete_a_stored_file(client, app, db, make_user, auth_headers, owner, role, expected):
    caller = make_user(role=role)
    name = _plant(app)

    if owner == "self":
        _own(db, caller["id"], name)
    elif owner == "other":
        _own(db, make_user()["id"], name)
    elif owner == "null":
        _own(db, None, name)

    response = client.delete(f"{UPLOADS}/{name}", headers=auth_headers(caller))

    assert response.status_code == expected
    assert os.path.exists(_stored(app, name)) is (expected != 200)


def test_a_refused_delete_leaves_the_ownership_row_alone(client, app, db, make_user, auth_headers):
    owner = make_user()
    stranger = make_user()
    name = _own(db, owner["id"], _plant(app), byte_size=4096)

    assert client.delete(f"{UPLOADS}/{name}", headers=auth_headers(stranger)).status_code == 403
    assert _rows(db) == {name}
    assert _used(db, owner["id"]) == 4096


def test_a_normalised_name_is_still_checked_against_its_owner(client, app, db, make_user, auth_headers):
    # The ownership lookup uses the name AFTER secure_filename,
    # so dressing it up as "..\<name>" cannot slip past it
    owner = make_user()
    stranger = make_user()
    name = _own(db, owner["id"], _plant(app))

    response = client.delete(f"{UPLOADS}/..%5C{name}", headers=auth_headers(stranger))

    assert response.status_code == 403
    assert os.path.isfile(_stored(app, name))


def test_an_owner_can_delete_through_a_normalised_name(client, app, db, actor):
    user, headers = actor
    name = _own(db, user["id"], _plant(app))

    response = client.delete(f"{UPLOADS}/%20{name}%20", headers=headers)

    assert response.status_code == 200
    assert not os.path.exists(_stored(app, name))


def test_the_route_answers_ok_when_the_row_exists_but_the_file_does_not(client, db, actor):
    user, headers = actor
    name = _own(db, user["id"], _name())

    response = client.delete(f"{UPLOADS}/{name}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert _rows(db) == set()


def test_a_file_that_could_not_be_unlinked_is_not_reported_as_deleted(client, app, db, actor, monkeypatch):
    # A read-only volume: the bytes survive and stay readable
    # through the public GET, so ok would be a lie. The row
    # goes either way, which is what makes the survivor an
    # orphan the sweep can still collect
    user, headers = actor
    name = _own(db, user["id"], _plant(app))

    def _boom(path):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(os, "unlink", _boom)

    response = client.delete(f"{UPLOADS}/{name}", headers=headers)

    assert response.status_code == 500
    assert response.get_json()["code"] == "delete_failed"
    assert os.path.isfile(_stored(app, name)), "the answer agrees with the disk"
    assert _rows(db) == set()


def test_a_directory_shaped_like_an_upload_is_not_reported_deleted(client, app, db, actor):
    # The same path with no monkeypatching at all: os.unlink on
    # a directory raises IsADirectoryError, an OSError the
    # helper swallows and the route notices
    user, headers = actor
    name = _name("jpg")
    os.makedirs(_stored(app, name))
    _own(db, user["id"], name)

    response = client.delete(f"{UPLOADS}/{name}", headers=headers)

    assert response.status_code == 500
    assert response.get_json()["code"] == "delete_failed"
    assert os.path.isdir(_stored(app, name))
    assert _rows(db) == set()


def test_deleting_frees_the_owners_quota(client, app, db, actor):
    user, headers = actor
    name = _own(db, user["id"], _plant(app), byte_size=3_000_000)
    _own(db, user["id"], _plant(app), byte_size=1_000_000)
    assert _used(db, user["id"]) == 4_000_000

    client.delete(f"{UPLOADS}/{name}", headers=headers)

    assert _used(db, user["id"]) == 1_000_000


def test_an_admin_delete_frees_the_members_quota(client, app, db, admin, make_user):
    _, admin_headers = admin
    member = make_user()
    name = _own(db, member["id"], _plant(app), byte_size=2_500_000)

    assert client.delete(f"{UPLOADS}/{name}", headers=admin_headers).status_code == 200
    assert _used(db, member["id"]) == 0


def test_an_admin_deleting_the_same_name_twice_stays_ok(client, app, admin):
    _, admin_headers = admin
    name = _plant(app)

    assert client.delete(f"{UPLOADS}/{name}", headers=admin_headers).status_code == 200
    assert client.delete(f"{UPLOADS}/{name}", headers=admin_headers).status_code == 200


def test_deleting_a_pre_v43_jpeg_is_admin_only(client, app, actor, admin):
    # .jpeg is in the pattern although the re-encode never
    # writes one — those files predate the uploads table, so
    # they have no owner
    _, headers = actor
    _, admin_headers = admin
    name = _plant(app, ext="jpeg")

    assert client.delete(f"{UPLOADS}/{name}", headers=headers).status_code == 404
    assert client.delete(f"{UPLOADS}/{name}", headers=admin_headers).status_code == 200
    assert not os.path.exists(_stored(app, name))


def test_deleting_a_file_an_avatar_still_points_at_is_allowed(client, app, db, actor):
    # The moderation and erasure path on purpose: the reference
    # is left to render as a broken image, exactly as a swept
    # orphan would
    user, headers = actor
    name = _own(db, user["id"], _plant(app))
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (f"/api/uploads/{name}", user["id"]))
    db.commit()

    assert client.delete(f"{UPLOADS}/{name}", headers=headers).status_code == 200
    assert not os.path.exists(_stored(app, name))

    still = db.execute("SELECT avatar_url FROM users WHERE id = ?", (user["id"],)).fetchone()
    assert still["avatar_url"] == f"/api/uploads/{name}", "the dangling reference is left in place"


@pytest.mark.parametrize("segment", ["...", "%E6%97%A5%E6%9C%AC%E8%AA%9E", f"{HEX}.bmp", f"{HEX}.png.part"])
def test_a_delete_of_a_name_this_module_could_not_have_written_is_a_400(client, actor, segment):
    _, headers = actor

    response = client.delete(f"{UPLOADS}/{segment}", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid filename", "code": "bad_filename"}


def test_the_name_gate_runs_before_the_database_is_touched(client, actor, monkeypatch):
    _, headers = actor

    def _never():
        raise AssertionError("a refused name must not open the database")

    monkeypatch.setattr(uploads_routes, "get_db", _never)

    assert client.delete(f"{UPLOADS}/labas.png", headers=headers).status_code == 400


def test_a_bad_bearer_token_cannot_delete(client, app):
    name = _plant(app)

    response = client.delete(f"{UPLOADS}/{name}", headers={"Authorization": "Bearer nesamone"})

    assert response.status_code == 401
    assert os.path.isfile(_stored(app, name))


def test_the_collection_path_has_no_delete(client, actor):
    _, headers = actor

    assert client.delete(UPLOADS, headers=headers).status_code == 405








# -----------------------------------------------------------
# delete_upload() — the shared helper, no ownership check
# -----------------------------------------------------------

def test_it_refuses_a_value_it_does_not_own_with_no_app_context_at_all(app):
    # The type and pattern guards return BEFORE _get_upload_dir
    # touches current_app, so a caller in a teardown, a signal
    # handler or a REPL can hand it anything safely
    assert delete_upload(None) is False
    assert delete_upload("https://knf.vu.lt/wp-content/naujiena.jpg") is False


@pytest.mark.parametrize("dress", [
    "{name}",
    "/api/uploads/{name}",
    "//{name}",
    "/a/b/c/{name}",
    "https://kitas.lt/failai/{name}",        # only the last segment is read
    "..%5C{name}".replace("%5C", "\\"),      # a backslash is not a posix separator
    " {name} ",
    "\t{name}\n",
    ".{name}",
    "{name}_",
])
def test_only_the_last_path_segment_matters(app, dress):
    name = _plant(app)

    with app.app_context():
        assert delete_upload(dress.format(name=name)) is True

    assert not os.path.exists(_stored(app, name))


def test_a_cache_busting_query_string_defeats_the_helper(app):
    # "?" and "=" are stripped rather than split off, so the
    # name no longer matches the pattern and the file survives
    name = _plant(app)

    with app.app_context():
        assert delete_upload(f"/api/uploads/{name}?v=2") is False

    assert os.path.isfile(_stored(app, name))


def test_a_string_subclass_is_still_a_string(app):
    class Vardas(str):
        pass

    name = _plant(app)

    with app.app_context():
        assert delete_upload(Vardas(name)) is True


def test_a_path_object_is_not_a_string(app):
    name = _plant(app)

    with app.app_context():
        assert delete_upload(Path(name)) is False

    assert os.path.isfile(_stored(app, name))


@pytest.mark.parametrize("value", [True, False, 0, 1.5, (), set(), object()])
def test_anything_that_is_not_a_string_is_refused(app, value):
    with app.app_context():
        assert delete_upload(value) is False


@pytest.mark.parametrize("value", [
    "0123456789abcdef0123456789abcdef",              # no extension
    "0123456789abcdef0123456789abcdef.png.part",     # an interrupted write
    "0123456789abcdef0123456789abcde.png",           # 31 hex
    "0123456789abcdef0123456789abcdef0.png",         # 33 hex
    "a" * 4000 + ".png",
    "/api/uploads/",
])
def test_a_value_the_pattern_refuses_never_reaches_the_disk(app, value):
    with app.app_context():
        assert delete_upload(value) is False


def test_a_part_file_is_left_for_the_sweep(app):
    leftover = _plant(app, filename=f"{HEX}.jpg.part")

    with app.app_context():
        assert delete_upload(leftover) is False

    assert os.path.isfile(_stored(app, leftover))


@pytest.mark.parametrize("ext", SERVABLE)
def test_every_extension_the_pattern_admits_is_deletable(app, ext):
    name = _plant(app, ext=ext)

    with app.app_context():
        assert delete_upload(name) is True


def test_it_is_idempotent_and_only_the_first_call_reports_true(app, db, actor):
    user, _ = actor
    name = _own(db, user["id"], _plant(app))

    with app.app_context():
        assert delete_upload(name) is True
        assert delete_upload(name) is False

    assert _rows(db) == set()


def test_it_ignores_ownership_entirely(app, db, make_user):
    # No caller check at all — which is exactly why delete_file
    # has to do its own before calling in here
    owner = make_user()
    name = _own(db, owner["id"], _plant(app), byte_size=777)

    with app.app_context():
        assert delete_upload(name) is True

    assert _rows(db) == set()
    assert _used(db, owner["id"]) == 0


def test_it_clears_only_the_named_row(app, db, actor):
    user, _ = actor
    doomed = _own(db, user["id"], _plant(app))
    spared = _own(db, user["id"], _plant(app))

    with app.app_context():
        delete_upload(doomed)

    assert _rows(db) == {spared}
    assert os.path.isfile(_stored(app, spared))


def test_a_directory_shaped_like_an_upload_reports_false_and_still_clears_its_row(app, db, actor):
    user, _ = actor
    name = _name("webp")
    os.makedirs(_stored(app, name))
    _own(db, user["id"], name)

    with app.app_context():
        assert delete_upload(name) is False

    assert os.path.isdir(_stored(app, name))
    assert _rows(db) == set(), "the row goes even when the file cannot"


def test_a_file_no_row_ever_recorded_still_reports_true(app, db):
    name = _plant(app)

    with app.app_context():
        assert delete_upload(name) is True

    assert not os.path.exists(_stored(app, name))
    assert _rows(db) == set()


def test_it_survives_an_upload_directory_that_is_not_there(app):
    shutil.rmtree(app.config["UPLOAD_DIR"])

    with app.app_context():
        assert delete_upload(f"{HEX}.png") is False

    assert os.path.isdir(app.config["UPLOAD_DIR"]), "_get_upload_dir recreates it"


def test_it_does_not_raise_when_the_upload_directory_is_a_file(app, db, actor, tmp_path):
    # A bad mount points UPLOAD_DIR at a regular file, so
    # _get_upload_dir's makedirs raises FileExistsError — an
    # OSError, and it is raised INSIDE the unlink guard, so the
    # caller mid delete-or-replace still gets a plain False
    user, _ = actor
    not_a_directory = tmp_path / "ne-katalogas"
    not_a_directory.write_bytes(b"failas, ne katalogas")
    app.config["UPLOAD_DIR"] = str(not_a_directory)
    name = _own(db, user["id"], f"{HEX}.png")

    with app.app_context():
        assert delete_upload(name) is False

    assert _rows(db) == set(), "the row is still cleared"








# -----------------------------------------------------------
# sweep_orphan_uploads() — the grace boundary and the walk
# -----------------------------------------------------------

# The clock is frozen so time.time() inside the sweep is the
# same value the test computed the mtime from — the boundary
# is then exact rather than "a second either way"
FROZEN = 1_800_000_000.0


@pytest.mark.parametrize("offset, swept", [
    (-1.0, False),                          # a second inside the grace period
    (0.0, True),                            # exactly at the cutoff — st_mtime > cutoff is False
    (1.0, True),                            # a second past it
])
def test_the_grace_period_boundary_is_exact(app, offset, swept):
    name = _plant(app)

    with time_machine.travel(FROZEN, tick=False):
        stamp = time.time() - ORPHAN_GRACE_SECONDS - offset
        os.utime(_stored(app, name), (stamp, stamp))

        with app.app_context():
            assert sweep_orphan_uploads() == (1 if swept else 0)

    assert os.path.exists(_stored(app, name)) is not swept


def test_it_does_not_recurse_into_subdirectories(app):
    nested = os.path.join(app.config["UPLOAD_DIR"], "archyvas")
    os.makedirs(nested)
    buried = os.path.join(nested, _name())
    with open(buried, "wb") as handle:
        handle.write(b"giliai")
    _age(buried)
    _age(nested)

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert os.path.isfile(buried)
    assert os.path.isdir(nested)


@pytest.mark.parametrize("filename", ["uzrasai.txt", ".slaptas-failas", "knfapp.db", "README"])
def test_it_takes_any_old_unreferenced_file_not_only_stored_names(app, filename):
    # The sweep is NOT gated by _FILENAME_RE — that is what
    # lets it collect a .part leftover, and it means UPLOAD_DIR
    # must never be shared with anything else
    _plant(app, filename=filename)
    _age(_stored(app, filename))

    with app.app_context():
        assert sweep_orphan_uploads() == 1

    assert not os.path.exists(_stored(app, filename))


def test_it_keeps_the_row_of_a_file_it_spared(app, db, actor):
    user, _ = actor
    name = _own(db, user["id"], _plant(app), byte_size=1234)

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert _rows(db) == {name}
    assert _used(db, user["id"]) == 1234


def test_one_pass_sorts_a_mixed_directory(app, db, make_user):
    user = make_user()

    referenced = _own(db, user["id"], _plant(app, ext="jpg"))
    fresh = _plant(app)
    orphan = _own(db, user["id"], _plant(app))
    leftover = _plant(app, filename=f"{uuid.uuid4().hex}.png.part")
    stray = _plant(app, filename="uzrasai.txt")

    nested = os.path.join(app.config["UPLOAD_DIR"], "archyvas")
    os.makedirs(nested)
    buried = os.path.join(nested, _name())
    with open(buried, "wb") as handle:
        handle.write(b"giliai")

    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?",
               (f"/api/uploads/{referenced}", user["id"]))
    db.commit()

    for path in (_stored(app, referenced), _stored(app, orphan), _stored(app, leftover),
                 _stored(app, stray), nested, buried):
        _age(path)

    with app.app_context():
        assert sweep_orphan_uploads() == 3

    assert _on_disk(app) == {referenced, fresh, "archyvas"}
    assert os.path.isfile(buried)
    assert _rows(db) == {referenced}


def test_a_reference_is_matched_by_its_last_path_segment_only(app, db, actor):
    # An avatar_url pointing at another host still spares a
    # local file whose basename happens to match
    user, _ = actor
    name = _plant(app)
    _age(_stored(app, name))
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?",
               (f"https://gravatar.com/avatars/{name}", user["id"]))
    db.commit()

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert os.path.isfile(_stored(app, name))


def test_a_reference_stored_as_a_blob_does_not_protect_its_file(app, db, actor):
    # users.avatar_url has TEXT affinity, which does not
    # convert a BLOB — str() then renders it as "b'/api/...'"
    # and the basename no longer matches anything on disk
    user, _ = actor
    name = _plant(app)
    _age(_stored(app, name))
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?",
               (f"/api/uploads/{name}".encode(), user["id"]))
    db.commit()

    with app.app_context():
        assert sweep_orphan_uploads() == 1

    assert not os.path.exists(_stored(app, name))


def test_an_empty_avatar_url_does_not_disturb_the_sweep(app, db, actor):
    user, _ = actor
    db.execute("UPDATE users SET avatar_url = '' WHERE id = ?", (user["id"],))
    db.commit()
    name = _plant(app)
    _age(_stored(app, name))

    with app.app_context():
        assert sweep_orphan_uploads() == 1


def test_a_reference_that_was_nulled_stops_protecting_its_file(app, db, actor):
    user, _ = actor
    name = _own(db, user["id"], _plant(app))
    _age(_stored(app, name))
    db.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (f"/api/uploads/{name}", user["id"]))
    db.commit()

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    db.execute("UPDATE users SET avatar_url = NULL WHERE id = ?", (user["id"],))
    db.commit()

    with app.app_context():
        assert sweep_orphan_uploads() == 1

    assert _rows(db) == set()


def test_it_counts_files_not_rows(app):
    # A .part leftover has no uploads row, so the DELETE
    # matches nothing and the counter still moves
    leftover = _plant(app, filename=f"{uuid.uuid4().hex}.jpg.part")
    _age(_stored(app, leftover))

    with app.app_context():
        assert sweep_orphan_uploads() == 1


def test_it_returns_the_number_it_removed(app):
    for _ in range(25):
        _age(_stored(app, _plant(app)))

    with app.app_context():
        assert sweep_orphan_uploads() == 25

    assert _on_disk(app) == set()


def test_a_second_pass_finds_nothing_left(app):
    for _ in range(3):
        _age(_stored(app, _plant(app)))

    with app.app_context():
        assert sweep_orphan_uploads() == 3
        assert sweep_orphan_uploads() == 0


def test_it_leaves_the_row_of_a_file_it_could_not_unlink(app, db, actor, monkeypatch):
    user, _ = actor
    name = _own(db, user["id"], _plant(app), byte_size=555)
    _age(_stored(app, name))

    def _boom(path):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(os, "unlink", _boom)

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert _rows(db) == {name}
    assert _used(db, user["id"]) == 555


def test_a_file_that_disappears_mid_pass_is_not_counted(app, db, actor, monkeypatch):
    # The race with a concurrent delete_upload: the unlink
    # raises FileNotFoundError, which is an OSError, so the
    # entry is skipped and its row is left for whoever won
    user, _ = actor
    vanishing = _own(db, user["id"], _plant(app))
    survivor = _plant(app)
    _age(_stored(app, vanishing))
    _age(_stored(app, survivor))

    real_unlink = os.unlink
    doomed = _stored(app, vanishing)

    def _racing_unlink(path):
        if path == doomed:
            real_unlink(path)
            raise FileNotFoundError(path)
        return real_unlink(path)

    monkeypatch.setattr(os, "unlink", _racing_unlink)

    with app.app_context():
        assert sweep_orphan_uploads() == 1

    assert _on_disk(app) == set()
    assert _rows(db) == {vanishing}, "the losing sweep leaves the row behind"


def test_it_removes_a_symlink_without_touching_its_target(app, tmp_path):
    target = tmp_path / "svarbus-failas.txt"
    target.write_bytes(b"ne uploads kataloge")
    link = _stored(app, _name("jpg"))
    os.symlink(target, link)
    _age(str(target))

    with app.app_context():
        assert sweep_orphan_uploads() == 1

    assert not os.path.lexists(link)
    assert target.exists(), "os.unlink drops the link, never what it points at"


def test_it_removes_an_orphan_row_whose_file_is_already_gone(app, db, actor):
    # The mirror image of an orphan file: a row whose file was
    # removed outside the app would otherwise count against its
    # owner's quota forever. The return value stays a count of
    # FILES, so it does not move
    user, _ = actor
    _own(db, user["id"], _name(), byte_size=9_000_000)

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert _rows(db) == set()
    assert _used(db, user["id"]) == 0


def test_a_broken_symlink_is_left_behind(app, tmp_path):
    # entry.is_file() follows the link and answers False for a
    # dangling one, so the sweep walks past it forever — the
    # one kind of litter this pass cannot collect
    link = _stored(app, _name("jpg"))
    os.symlink(tmp_path / "niekada-neegzistavo.txt", link)

    with app.app_context():
        assert sweep_orphan_uploads() == 0

    assert os.path.lexists(link)


def test_it_needs_an_app_context(app):
    # UPLOAD_DIR is read off current_app, and it is the very
    # first thing the sweep does
    with pytest.raises(RuntimeError):
        sweep_orphan_uploads()


def test_it_surfaces_an_upload_directory_that_is_a_file(app, tmp_path):
    # Unlike delete_upload, the sweep resolves UPLOAD_DIR
    # OUTSIDE any guard, so a bad mount fails the maintenance
    # job loudly instead of reporting a quiet zero
    not_a_directory = tmp_path / "ne-katalogas"
    not_a_directory.write_bytes(b"failas, ne katalogas")
    app.config["UPLOAD_DIR"] = str(not_a_directory)

    with app.app_context():
        with pytest.raises(OSError):
            sweep_orphan_uploads()
