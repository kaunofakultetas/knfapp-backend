#!/bin/bash



# STEP 1: Create necessary files and directories
# ==============================================
mkdir -p ./_DATA/django/uploads
mkdir -p ./_DATA/postgres
sudo chown -R 1000:1000 ./_DATA



# STEP 2: Generate DJANGO_SECRET_KEY if it doesnt exist
# =====================================================
if [ ! -f .env ] || ! grep -q "^DJANGO_SECRET_KEY=" .env; then
    echo "Generating DJANGO_SECRET_KEY ..."
    DJANGO_SECRET_KEY="$(openssl rand -hex 32)"

    # Only add newline if file doesn't end with one
    [ -f .env ] && [ -n "$(tail -c1 .env 2>/dev/null)" ] && echo "" >> .env
    echo "DJANGO_SECRET_KEY=$DJANGO_SECRET_KEY" >> .env
    echo "DJANGO_SECRET_KEY added to .env"
fi



# STEP 3: Generate DbGate credentials if they don't exist
# =======================================================
#
# One password, two forms: DBGATE_PASSWORD is DbGate's own
# login, DBGATE_BASIC_AUTH_HASH is its bcrypt for the ingress
# gate in endpoint/Caddyfile (HTTP Basic, user "dbgate"). That
# gate ships COMMENTED OUT by the owner's decision, so the hash
# is generated ready but unused until it is uncommented. It is
# single-quoted in .env because a bcrypt is full of $ and
# compose interpolates unquoted values.
if [ ! -f .env ] || ! grep -q "^DBGATE_PASSWORD=" .env; then
    echo "Generating DBGATE_PASSWORD ..."
    DBGATE_PASSWORD="$(openssl rand -hex 32)"

    # Only add newline if file doesn't end with one
    [ -f .env ] && [ -n "$(tail -c1 .env 2>/dev/null)" ] && echo "" >> .env
    echo "DBGATE_PASSWORD=$DBGATE_PASSWORD" >> .env
    echo "DBGATE_PASSWORD added to .env"
fi

if ! grep -q "^DBGATE_BASIC_AUTH_HASH=" .env; then
    echo "Generating DBGATE_BASIC_AUTH_HASH ..."
    DBGATE_PASSWORD="$(grep '^DBGATE_PASSWORD=' .env | cut -d= -f2-)"
    DBGATE_BASIC_AUTH_HASH="$(sudo docker run --rm caddy:2.11-alpine \
        caddy hash-password --plaintext "$DBGATE_PASSWORD")"

    [ -n "$(tail -c1 .env 2>/dev/null)" ] && echo "" >> .env
    echo "DBGATE_BASIC_AUTH_HASH='$DBGATE_BASIC_AUTH_HASH'" >> .env
    echo "DBGATE_BASIC_AUTH_HASH added to .env"
fi



# STEP 4: Run the stack
# =====================
sudo docker compose down
sudo docker compose up -d --build
