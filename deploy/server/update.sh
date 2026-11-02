#!/bin/bash
# Pull the latest image and (re)start. Run by deploy/deploy.sh from the Mac.
set -euo pipefail
cd /opt/atya
./fetch-env.sh
IMAGE=$(grep '^ECR_IMAGE=' .env | cut -d= -f2-)
aws ecr get-login-password --region "$(./region.sh)" | docker login --username AWS --password-stdin "${IMAGE%%/*}"
PROFILE=()
grep -q '^WEB_DOMAIN=.' .env && PROFILE=(--profile web)
docker compose "${PROFILE[@]}" pull app
docker compose "${PROFILE[@]}" up -d
docker image prune -f >/dev/null
