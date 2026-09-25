#!/bin/bash
############################################################
#  [*] Run the assistant sidecar's tests
#
#  node --test over app/tests in a throwaway --rm container
#  of the compose-built dev image, the app tree mounted
#  READ-ONLY — the running knfapp-assistant is never
#  touched and nothing survives the run. Every suite fakes
#  both externals (a scripted stub Django, a mock model),
#  so the container runs with no network at all.
#  node_modules comes from the bind-mounted tree (the dev
#  container's npm install put it there; a fresh checkout
#  runs `npm install` in app/ once first). The image is the
#  dev one compose builds (bare node — nothing baked), so
#  there is nothing to rebuild here.
############################################################

set -e
cd "$(dirname "$0")"

sudo docker run --rm --network none \
    -v "$PWD/app:/app:ro" \
    knfapp-assistant \
    sh -c "cd /app && node --test"
