#!/bin/bash
############################################################
#  [*] Run the Django regression tests
#
#  Builds the production image and runs the suite inside a
#  throwaway --rm container against in-memory SQLite —
#  nothing running is touched, nothing survives the run.
#  `makemigrations --check` goes first: the migrations are
#  kept in step with models.py by hand, so drift must fail
#  HERE, never at a deploy.
#
#  The PostgreSQL pass — the engine production runs on —
#  is the same command with two variables set (README
#  "Tests" has the whole recipe, throwaway cluster
#  included):
#
#    TEST_DATABASE_URL  postgres://user:pass@host:5432/db —
#                       knfapp.tests.settings swaps the
#                       in-memory SQLite for it; Django
#                       creates and drops test_<db> beside
#                       it, so it must name a THROWAWAY
#                       database (settings.py refuses the
#                       production name at import)
#    TEST_DOCKER_NETWORK  the docker network the throwaway
#                       cluster is reachable on, e.g.
#                       container:knfapp-pgtest to share its
#                       loopback; unset = no network at all
############################################################

set -e
cd "$(dirname "$0")"

sudo docker build -t knfapp-django-test .
sudo docker run --rm \
    --network "${TEST_DOCKER_NETWORK:-none}" \
    ${TEST_DATABASE_URL:+-e TEST_DATABASE_URL="$TEST_DATABASE_URL"} \
    knfapp-django-test \
    sh -c "python3 manage.py makemigrations --check --dry-run --settings=knfapp.tests.settings \
        && python3 manage.py test knfapp.tests --settings=knfapp.tests.settings -v 2"
