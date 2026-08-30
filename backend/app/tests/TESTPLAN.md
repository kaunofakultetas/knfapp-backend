# Backend test suite — plan and conventions

Goal: exercise every line of `backend/app` (21,887 lines, 32 modules) — happy paths,
unhappy paths, edge cases, limits, regressions, and the wire contracts the production
mobile app depends on.

## How to run

```sh
cd backend && ./runTests.sh                 # whole suite + coverage report
./runTests.sh app/tests/test_auth_login.py  # one file
./runTests.sh -k invitation -x              # any pytest args
COVERAGE_MIN=90 ./runTests.sh               # fail below a threshold
```

Everything runs inside the `knfapp-backend-tests` container: same pinned interpreter and
wheels as production, `--network none`, tmpfs `/tmp`, no `_DATA` mount. The live database
is unreachable by construction.

## Rules every test file follows

1. **One owned file per author.** Never edit another agent's `test_*.py`. `conftest.py` is
   SHARED — additive fixtures only, tight anchors, never reformat.
2. **Use the shared fixtures** (`app`, `client`, `db`, `make_user`, `actor`, `admin`,
   `auth_headers`, `seeded_code`). Do not hand-build tokens or re-implement login.
3. **No network, ever.** Scraper tests drive HTML through `responses`; a test that reaches
   knf.vu.lt fails by design (the container has no network).
4. **No sleeping on wall-clock.** Use `time_machine` for expiry, retention, rate-limit and
   cooldown windows.
5. **Assert behaviour, not implementation.** Prefer a route's status + JSON body over
   poking internals — except where the point IS persistence, then assert with `db`.
6. **Contract tests carry `@pytest.mark.contract`** and assert exact field names/shapes the
   mobile app consumes. The mobile client is the source of truth:
   `mobile/app/services/api/*.ts`. A contract test failing means a production app breaks.
7. **House comment style** applies: file header banner explaining what the module proves,
   and banner comments over non-obvious helpers. No docstrings.
8. **Name tests as sentences**: `test_expired_invitation_is_refused`, not `test_invite_2`.
9. Each test is independent — the `app` fixture gives every test a fresh database.
10. **`json=` bodies are escaped by the app's own JSON provider.** Flask's test client
    serialises a `json=` kwarg through `app.json.dumps`, and this app html-escapes every
    string on the way out — so `client.post(..., json={"content": "I <3 you"})` puts an
    ALREADY-ESCAPED string on the wire, which no real client sends. It is harmless for
    plain text, but it silently falsifies any assertion about markup, entities or
    round-tripped content. When what is on the wire matters, post raw bytes:

    ```python
    client.post(path, data=json.dumps(payload),
                headers={**headers, "Content-Type": "application/json"})
    ```

    `test_contracts_mobile.py` has a `post_raw()` helper doing exactly this.

## Coverage ownership

Each agent owns the test file(s) below and drives its target module(s) toward 100% line
coverage. Check your own number with:

```sh
./runTests.sh app/tests/<your_file>.py --cov=app.<your.module> --cov-report=term-missing
```

Lines that genuinely cannot execute in tests (a `__main__` guard, a defensive branch that
needs a corrupted filesystem) get a `# pragma: no cover` WITH a one-line reason — never as
a way to dodge a testable path.
