#!/bin/bash

mkdir -p ./_DATA/backend ./_DATA/backups


sudo docker-compose down
sudo docker-compose up -d --build