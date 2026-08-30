#!/bin/bash
############################################################
#  [*] runUpdateThisStack.sh — deploy/refresh the knfapp stack
#
#  Run from the repo root on the deploy host. In order:
#
#    1. create the data tree and hand it to uid 1000 (every
#       container runs 1000:1000 — a tree owned by root makes
#       the backend crash-loop on its first write)
#    2. tighten modes: .env and the database are secrets, not
#       world-readable files
#    3. take a WAL-safe snapshot BEFORE the new image runs its
#       migrations, so a bad migration is recoverable
#    4. `docker compose up -d --build` — v2, and no `down`
#       first: compose recreates only what actually changed,
#       so unchanged services never leave the air
#
#  Steps 1 and 2 run on every deploy on purpose. Permissions
#  drift (a manual sqlite3 session, an editor writing .env,
#  a restored backup) and this is the one place that fixes it
#  back.
############################################################

set -euo pipefail

cd "$(dirname "$0")"

DATA_DIR="./_DATA"
BACKUP_DIR="$DATA_DIR/backups"
DB_PATH="$DATA_DIR/backend/knfapp.db"


# STEP 1: the data tree, owned by the uid every container runs as
# ===============================================================
sudo mkdir -p "$DATA_DIR/backend/uploads" "$BACKUP_DIR"
sudo chown -R 1000:1000 "$DATA_DIR"


# STEP 2: secrets stay secrets — .env holds SECRET_KEY/JWT_SECRET/
# DBGATE_PASSWORD, and _DATA holds the one copy of production data
# ================================================================
if [ -f ./.env ]; then
    sudo chmod 600 ./.env
fi

sudo chmod 700 "$DATA_DIR" "$DATA_DIR/backend" "$BACKUP_DIR"
sudo find "$DATA_DIR" -type f \
    \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' -o -name '*.gz' \) \
    -exec chmod 600 {} +


# STEP 3: pre-migration snapshot. sqlite3 (not cp): the database
# runs in WAL mode, so a raw copy without its -wal is torn. This
# is the ONLY snapshot the stack takes — there is no backup
# sidecar by design — so no sqlite3 on the host is a warning
# that the deploy proceeds unprotected, not a stop
# ==============================================================
if [ -f "$DB_PATH" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
        SNAPSHOT="$BACKUP_DIR/pre-deploy-$(date -u '+%Y%m%d-%H%M%S').db"
        sudo sqlite3 "$DB_PATH" ".backup '$SNAPSHOT'"
        sudo gzip -f "$SNAPSHOT"
        sudo chown 1000:1000 "$SNAPSHOT.gz"
        sudo chmod 600 "$SNAPSHOT.gz"
        echo "Pre-deploy snapshot: $SNAPSHOT.gz"
    else
        echo "WARNING: sqlite3 not installed — skipping the pre-deploy snapshot"
    fi
fi


# STEP 4: build and roll. `docker compose` v2; no `down`, so only
# the services whose image or config changed are recreated
# ===============================================================
sudo docker compose up -d --build
