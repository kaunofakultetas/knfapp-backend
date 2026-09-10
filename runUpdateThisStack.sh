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



# STEP 3: Generate DBGate credentials if they don't exist
# =======================================================
if [ ! -f .env ] || ! grep -q "^DBGATE_PASSWORD=" .env; then
    echo "Generating DBGATE credentials..."
    DBGATE_PASSWORD="$(openssl rand -hex 32)"
    DBGATE_AUTH_HEADER="$(echo -n "dbgate:$DBGATE_PASSWORD" | base64 -w 0)"

    # Only add newline if file doesn't end with one
    [ -f .env ] && [ -n "$(tail -c1 .env 2>/dev/null)" ] && echo "" >> .env
    echo "DBGATE_PASSWORD=$DBGATE_PASSWORD" >> .env
    echo "DBGATE_AUTH_HEADER=$DBGATE_AUTH_HEADER" >> .env
    echo "DBGATE credentials added to .env"
fi



# STEP 4: Run the stack
# =====================
sudo docker compose down
sudo docker compose up -d --build
