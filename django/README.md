# knfapp — Django backend

The faculty app's API service: **no admin site, no contrib.auth, no
cookies**. The API authenticates with opaque bearer tokens against
its own tables, all configuration arrives through env variables,
and the whole URL surface reads as one table of contents in
`knfapp/urls.py`. The wire contract is `swagger/swagger.yaml` — the
mobile app translates machine `code` slugs and never parses the
English `error` prose.

Not wired into docker-compose yet: this image (plus the stack-level
`cron/` container) joins the compose file at cutover.

## Layout

```
manage.py                 Django entry point
knfapp/settings.py        the whole configuration (env-driven)
knfapp/urls.py            every /api/* route, grouped by app
knfapp/wsgi.py            gunicorn entry: socket.io wrapping Django,
                          and the wayfind stitch worker's start
knfapp/common/            http/json helpers, timestamps, rate limits,
                          raw-SQL dict-row helpers
knfapp/users/             accounts, invitation codes, bearer sessions,
                          the GDPR erasure and export
knfapp/uploads/           stored files: byte gates, atomic store, serve
knfapp/news/              the ranked feed, posts, likes, comments, polls
knfapp/social/            profiles, friendships, walls, blocks, reports,
                          activity
knfapp/notifications/     push-token registry, channel switches, the
                          Expo sender (push.py)
knfapp/chat/              conversations/messages REST + the socket.io
                          layer (socket.py, events.py), link unfurling
knfapp/schedule/          the scraped lecture timetable (read side)
knfapp/info/              the bilingual faculty handbook + scraped
                          overlay
knfapp/memes/             the shared meme library
knfapp/admin/             invitation codes, user management, stats,
                          broadcast, reports — plus the audit trail
knfapp/scraper/           the four site scrapers, their run ledger and
                          admin trigger routes, management commands
knfapp/wayfind/           the indoor map: building graph drafts and
                          publishes, panoramas/plans, guided captures,
                          the stitch worker
knfapp/tests/             the regression suite (see below)
schema.dbml               the database diagram (paste into dbdiagram.io)
```

## Authentication

Opaque bearer sessions (`knfapp/users/auth.py`): register/login mint
a uuid4 token, the DB stores its **sha256**, expiry is 30 days with
a lazy purge that takes the owner's push tokens with it, deactivated
accounts are locked out on live sessions, and login is
timing-equalized with a dummy bcrypt check. `@require_auth` /
`@require_role` attach the resolved user to `request.user`.

## Serving

One gunicorn **gthread worker with many threads** — not a worker
pool: chat presence, the socket.io rooms and the per-event socket
rate limiter are in-process state (`chat/socket.py`). The socket
transport is polling only; `/socket.io/*` and `/api/*` are served by
the same container behind Caddy. A `DJANGO_DEBUG` runserver serves
REST only (no socket layer, no stitch worker — both start from
`knfapp/wsgi.py`).

Scheduled work lives in the stack-level `cron/` container (busybox
crond + docker CLI) exec-ing the management commands:
`scrape_news` (20 min), `scrape_schedule` (6 h), `scrape_info`
(daily) and `maintenance` (daily: expired sessions, orphaned push
tokens, abandoned scraper runs, the disappearing-messages backstop
sweep). Its compose block:

```yaml
  knfapp-cron:
    container_name: knfapp-cron
    image: knfapp-cron
    build: ./cron
    user: root
    read_only: true
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - /var/run/docker.sock:/var/run/docker.sock
    network_mode: none
    restart: unless-stopped
```

## Tests

Smart regression pins, not input-permutation sweeps: every case
protects one decision a rewrite must not lose (the sessions table's
contents, the invitation-code rejection order, the feed's two score
formulas, the chat paging cursor's stamp tie-break, the wayfind op
log's idempotency, the stitcher's centre-column contract, …). Run
them with `./runTests.sh` — it builds the image and runs the suite
in a throwaway container on in-memory SQLite, with
`makemigrations --check` guarding the migrations against model
drift (the suite itself repeats that check as a test, so the
everyday `docker exec knfapp-django python3 manage.py test
knfapp.tests --settings=knfapp.tests.settings` loop runs it too).

### The PostgreSQL pass

Production runs on PostgreSQL and SQLite is not it: foreign keys
there are checked at COMMIT (an `except IntegrityError` around an
insert never sees one), the text type refuses NUL bytes, `LIKE`
folds case by collation, and the vendor-split SQL in
`common/expressions.py` takes its other branch. The same suite
runs on PostgreSQL with `TEST_DATABASE_URL` set — the test
settings read it with the parser `settings.py` uses for
`DATABASE_URL`, and Django creates and drops `test_<db>` beside
the database it names. It must name a THROWAWAY cluster: the
stack's own `knfapp-postgres` image (it carries pgvector, which
the assistant models need) started once, disposably:

```
sudo docker run -d --rm --name knfapp-pgtest \
    -e POSTGRES_DB=knfapp_test -e POSTGRES_USER=knfapp -e POSTGRES_PASSWORD=knfapp \
    --tmpfs /var/lib/postgresql/data --tmpfs /var/run/postgresql knfapp-postgres
until sudo docker exec knfapp-pgtest pg_isready -U knfapp -d knfapp_test; do sleep 1; done

TEST_DOCKER_NETWORK=container:knfapp-pgtest \
TEST_DATABASE_URL=postgres://knfapp:knfapp@127.0.0.1:5432/knfapp_test \
    ./runTests.sh

sudo docker stop knfapp-pgtest
```

Never point `TEST_DATABASE_URL` at the live cluster: `settings.py`
refuses a test run whose target is the production database name
(`KNFAPP_ALLOW_TEST_ON_PROD_DB=1` is the operator override) —
that guard exists because runs without `--settings` once left
eighteen orphaned `test_knfapp_*` databases behind.

## Data

The production database starts EMPTY by decision — nothing is
imported from any earlier system. First boot is:

```
python3 manage.py migrate --noinput
python3 manage.py grant_role <username> admin
```

(register the first account through the app as usual, then grant
it the admin role with the bootstrap command — registration alone
can only mint students, and every admin surface requires an
existing admin, so the command is the one sanctioned way in).

SQLite serves with WAL, a 5 s busy timeout and NORMAL synchronous
(set per connection from settings).

The ENGINE is a `DATABASE_URL` choice, not a commitment: the image
carries the postgres driver, row traffic (upserts included, via
the ORM's ON CONFLICT rendering) rides the ORM, the raw SQL that
remains is deliberate and portable (the scraper's one-statement
run lock, the chat last-message seek and unread aggregates,
no-cascade purges), the feed scoring rides one vendor-split
expression module (common/expressions.py — the only place
dialects are allowed to differ), and the one SQLite-only
mechanism degrades by design: the chat write-lock helper issues
`BEGIN IMMEDIATE` only where SQLite needs it. A
postgres deployment is the URL flip plus a database service in
compose; the suite itself runs on SQLite.
