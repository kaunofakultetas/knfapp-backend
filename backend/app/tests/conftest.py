# -----------------------------------------------------------
#  [*] Tests — shared fixtures
#
#  Every test in this suite builds on the fixtures here. The
#  rules they enforce:
#
#    - NOTHING touches the production database. Each test gets
#      a brand-new SQLite file under the container's tmpfs,
#      created by the real init_db() so migrations run exactly
#      as they do in production.
#    - NOTHING reaches the network. The container runs with
#      --network none; scrapers must be driven through the
#      `responses` fake, never live HTTP.
#    - The app is built by the real create_app(), so the
#      middleware, error handlers, CORS and blueprints under
#      test are the ones production runs.
#
#  Fixture map (most tests need only `client` and one actor):
#
#    app          — a configured Flask app on a fresh DB
#    client       — its test client
#    db           — a direct sqlite3 connection to that DB
#    make_user    — factory: insert a user of any role
#    actor        — a registered student + its bearer headers
#    admin        — the seeded admin + its bearer headers
#    auth_headers — bearer headers for any user id
#    seeded_code  — the bootstrap invitation code
# -----------------------------------------------------------

import os
import sqlite3
import sys
import uuid

import bcrypt
import pytest


# The suite imports the app package from backend/, which is the
# working directory inside the test container; adding it here
# too keeps a host-side `pytest` run working the same way
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))




# -----------------------------------------------------------
# app
# -----------------------------------------------------------
#
# A real create_app() on a throwaway database and upload dir.
# The env vars are set BEFORE the import so create_app reads
# them, and restored afterwards so tests cannot leak config
# into one another.
#
# Used by:
#   - every test module, directly or through `client`
# -----------------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):

    db_path = tmp_path / "knfapp-test.db"
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()

    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("SCRAPER_ENABLED", "0")
    # Deterministic origins so CORS assertions do not depend on
    # whatever the deploy happens to set
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8081")

    from app import create_app

    application = create_app()
    application.config["TESTING"] = True

    yield application




# -----------------------------------------------------------
# _clean_rate_limits
# -----------------------------------------------------------
#
# Both limiters live in ONE module-level OrderedDict keyed by
# client address, and every test in a worker process shares the
# same address (127.0.0.1) — so the login limiter's ~10
# attempts per 5 minutes is a budget for the WHOLE worker, not
# per test. Once spent, `auth_headers` starts answering
# rate_limited and every later test in that worker dies at
# setup with a login failure that has nothing to do with what
# it was testing.
#
# Clearing on both sides of every test makes each one start
# with a full budget, which is what a real client gets. The
# limiter's own behaviour is tested deliberately, by tests that
# drive _check_rate_limit directly or build their own app.
#
# Used by:
#   - every test (autouse)
# -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_rate_limits():
    from app.auth import routes as auth_routes

    auth_routes._rate_limit_store.clear()
    yield
    auth_routes._rate_limit_store.clear()




# -----------------------------------------------------------
# client
# -----------------------------------------------------------
#
# Used by:
#   - every route test
# -----------------------------------------------------------

@pytest.fixture
def client(app):
    return app.test_client()




# -----------------------------------------------------------
# db
# -----------------------------------------------------------
#
# A direct connection to the test database, row factory set the
# way the app sets it, for arranging state a route cannot
# create and for asserting what a route actually wrote.
#
# Used by:
#   - tests that seed rows or verify persistence
# -----------------------------------------------------------

@pytest.fixture
def db(app):
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()




# -----------------------------------------------------------
# make_user
# -----------------------------------------------------------
#
#   user = make_user(role="teacher")
#   user = make_user(username="ona", active=0)
#
# Inserts a user directly (bcrypt-hashing the password) and
# returns a dict with its id, username and plaintext password
# so the caller can log in as them.
#
# Used by:
#   - tests needing an actor of a specific role or state
# -----------------------------------------------------------

@pytest.fixture
def make_user(app):

    def _make(username=None, password="slaptazodis123", role="student", active=1, display_name=None):
        username = username or f"user_{uuid.uuid4().hex[:8]}"
        user_id = str(uuid.uuid4())
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

        conn = sqlite3.connect(app.config["DB_PATH"])
        try:
            conn.execute(
                "INSERT INTO users (id, username, email, display_name, password_hash, role, active, invited)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (user_id, username, f"{username}@knf.vu.lt", display_name or username.title(),
                 pw_hash, role, active),
            )
            conn.commit()
        finally:
            conn.close()

        return {"id": user_id, "username": username, "password": password,
                "role": role, "email": f"{username}@knf.vu.lt"}

    return _make




# -----------------------------------------------------------
# auth_headers
# -----------------------------------------------------------
#
#   headers = auth_headers(user)      — logs in, returns the
#                                       Authorization header
#
# Goes through POST /api/auth/login so the token is minted and
# stored exactly as production does it (hashed at rest since
# migration v13) — never hand-built.
#
# Used by:
#   - every authenticated route test
# -----------------------------------------------------------

@pytest.fixture
def auth_headers(client):

    def _headers(user):
        response = client.post("/api/auth/login", json={
            "username": user["username"],
            "password": user["password"],
        })
        assert response.status_code == 200, f"login failed for fixture user: {response.get_json()}"
        token = response.get_json()["token"]
        return {"Authorization": f"Bearer {token}"}

    return _headers




# -----------------------------------------------------------
# actor
# -----------------------------------------------------------
#
# The common case: an ordinary signed-in student. Returns
# (user_dict, headers).
#
# Used by:
#   - tests that just need "some authenticated user"
# -----------------------------------------------------------

@pytest.fixture
def actor(make_user, auth_headers):
    user = make_user()
    return user, auth_headers(user)




# -----------------------------------------------------------
# admin
# -----------------------------------------------------------
#
# The seeded administrator, whose password the `app` fixture
# pins through ADMIN_PASSWORD. Returns (user_dict, headers).
#
# Used by:
#   - admin route tests, role-gate tests
# -----------------------------------------------------------

@pytest.fixture
def admin(app, auth_headers, db):
    row = db.execute("SELECT id, username, email FROM users WHERE username = 'admin'").fetchone()
    user = {"id": row["id"], "username": row["username"], "email": row["email"],
            "password": "test-admin-password", "role": "admin"}
    return user, auth_headers(user)




# -----------------------------------------------------------
# seeded_code
# -----------------------------------------------------------
#
# The bootstrap invitation code planted by _seed_defaults —
# the only way to register on a fresh database.
#
# Used by:
#   - registration tests
# -----------------------------------------------------------

@pytest.fixture
def seeded_code(db):
    row = db.execute("SELECT code FROM invitation_codes ORDER BY created_at LIMIT 1").fetchone()
    return row["code"]
