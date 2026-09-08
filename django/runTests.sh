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
############################################################

set -e
cd "$(dirname "$0")"

sudo docker build -t knfapp-django-test .
sudo docker run --rm knfapp-django-test \
    sh -c "python3 manage.py makemigrations --check --dry-run --settings=knfapp.tests.settings \
        && python3 manage.py test knfapp.tests --settings=knfapp.tests.settings -v 2"
