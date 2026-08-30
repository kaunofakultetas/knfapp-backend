# -----------------------------------------------------------
#  [*] Tests — infrastructure smoke
#
#  Proves the harness itself works before any suite relies on
#  it: the app boots on a throwaway database, migrations ran,
#  the seeded admin can authenticate, and a guest can read the
#  feed (the app must work without login).
# -----------------------------------------------------------


def test_app_boots_and_health_answers(client):
    response = client.get("/api/health")
    assert response.status_code == 200


def test_migrations_all_applied(db):
    versions = [r[0] for r in db.execute("SELECT version FROM _migrations ORDER BY version")]
    assert versions, "no migrations recorded on a fresh database"
    assert versions == sorted(versions)


def test_seeded_admin_can_log_in(admin):
    user, headers = admin
    assert headers["Authorization"].startswith("Bearer ")


def test_guest_can_read_the_feed(client):
    response = client.get("/api/news?page=1")
    assert response.status_code == 200


def test_each_test_gets_its_own_database(app, db):
    db.execute("INSERT INTO users (id, username, email, display_name, password_hash, role)"
               " VALUES ('iso-1', 'isolation', 'iso@knf.vu.lt', 'Iso', 'x', 'student')")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM users WHERE id = 'iso-1'").fetchone()[0] == 1


def test_previous_test_left_nothing_behind(db):
    assert db.execute("SELECT COUNT(*) FROM users WHERE id = 'iso-1'").fetchone()[0] == 0


# -----------------------------------------------------------
# Migration guards
# -----------------------------------------------------------
#
# init_db() runs migrations on a PLAIN sqlite3 connection with
# no row_factory, so query results are tuples. A migration that
# subscripts a row by column name raises TypeError and takes
# the whole boot down with it — which is exactly how v47
# crash-looped before release. These guards are structural, so
# they catch the next one at authoring time.
# -----------------------------------------------------------

import ast
import os
import re


def _database_module_source():
    here = os.path.dirname(os.path.abspath(__file__))
    return open(os.path.join(here, "..", "database", "__init__.py")).read()


def test_no_migration_subscripts_a_row_by_column_name():
    source = _database_module_source()
    offenders = []

    for match in re.finditer(r"def (_migration_v\d+\w*)\(conn\):", source):
        name = match.group(1)
        nxt = source.find("\ndef ", match.end())
        body = source[match.end(): nxt if nxt > 0 else len(source)]
        if re.search(r"\w+\[[\"']\w+[\"']\]", body):
            offenders.append(name)

    assert offenders == [], (
        "migrations run without a row_factory, so rows are tuples — "
        f"unpack positionally instead of by name in: {offenders}"
    )


def test_every_registered_migration_has_a_function():
    source = _database_module_source()
    tree = ast.parse(source)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name.startswith("_migration_v")}
    registered = re.findall(r"^\s+(\d+): \(\s*$|^\s+(\d+): \(", source, re.M)
    versions = sorted({int(a or b) for a, b in registered})

    assert versions, "no migrations registered"
    for version in versions:
        assert any(fn.startswith(f"_migration_v{version}_") for fn in defined), \
            f"migration v{version} is registered but has no function"


def test_migration_versions_are_unique_and_ordered(db):
    versions = [r[0] for r in db.execute("SELECT version FROM _migrations ORDER BY version")]
    assert len(versions) == len(set(versions)), "a migration version ran twice"
