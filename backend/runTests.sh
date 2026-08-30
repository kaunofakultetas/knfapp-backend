#!/bin/sh
############################################################
#  [*] backend/runTests.sh — run the backend suite in Docker
#
#  Builds the test image (Dockerfile.test) and runs pytest
#  inside it, so the suite executes against the same pinned
#  interpreter and wheels as production and needs nothing
#  installed on the host but Docker.
#
#  The container is disposable and never touches the live
#  stack: no ports, no _DATA mount, its own network namespace,
#  every test writing to its own temp database under /tmp.
#  Only the repo's backend/ directory is mounted, read-write,
#  so coverage output lands back on the host.
#
#  The whole-suite run goes through pytest-xdist (-n auto):
#  every test builds its own temp database under tmp_path and
#  module-level state (the rate-limit buckets) is per worker
#  process, so the suite is parallel-safe — and 6x faster.
#  Passing any argument runs pytest exactly as asked, single
#  process, which is what you want for one file or a -k filter.
#
#  Usage:
#    ./runTests.sh                     — whole suite + coverage
#    ./runTests.sh app/tests/test_auth.py
#    ./runTests.sh -k invitation -x    — any pytest arguments
#    ./runTests.sh app/tests -n auto   — parallel, no coverage
#    COVERAGE_MIN=90 ./runTests.sh     — fail under a threshold
#    NO_BUILD=1 ./runTests.sh          — skip the image rebuild
#
#  Exit status is pytest's, so CI and the shell can branch on
#  it directly.
############################################################

set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
IMAGE=knfapp-backend-tests
COVERAGE_MIN=${COVERAGE_MIN:-0}


# STEP 1: build the test image unless the caller opted out
# =======================================================
if [ "${NO_BUILD:-0}" != "1" ]; then
    echo "==> Building $IMAGE"
    docker build -q -f "$SCRIPT_DIR/Dockerfile.test" -t "$IMAGE" "$SCRIPT_DIR" >/dev/null
fi


# STEP 2: arguments. With none, run the whole suite under
# coverage; with any, pass them through untouched so a single
# file or a -k filter behaves like a plain pytest call
# ===========================================================
if [ "$#" -eq 0 ]; then
    # A bounded worker count, not `auto`: `auto` takes every core
    # (32 here), and each worker builds its own app and database
    # per test, which starved them into collection errors. 8 is
    # the measured sweet spot and leaves the box usable
    set -- app/tests \
        -n "${TEST_WORKERS:-8}" \
        --cov=app \
        --cov-report=term-missing:skip-covered \
        --cov-report=html:app/tests/.coverage-html \
        --cov-report=xml:app/tests/.coverage.xml \
        "--cov-fail-under=$COVERAGE_MIN" \
        -q
fi


# STEP 3: run. --rm so nothing accumulates, no network so a
# forgotten requests call fails loudly instead of reaching
# knf.vu.lt, and the backend directory mounted for coverage
# output. tmpfs /tmp keeps every test database off the host
# =========================================================
echo "==> Running tests"
exec docker run --rm \
    --network none \
    --tmpfs /tmp:exec,mode=1777 \
    -v "$SCRIPT_DIR:/app" \
    -w /app \
    -e PYTHONDONTWRITEBYTECODE=1 \
    "$IMAGE" "$@"
