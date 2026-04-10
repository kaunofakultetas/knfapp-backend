#!/bin/bash

mkdir -p ./_DATA/backend


sudo docker-compose down
sudo docker-compose up -d --build