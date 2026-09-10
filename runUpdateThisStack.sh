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



# STEP 3: Run the stack
# =====================
sudo docker compose down
sudo docker compose up -d --build
