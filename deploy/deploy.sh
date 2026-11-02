#!/usr/bin/env bash
# Build the app image (arm64), push it to ECR, and restart the server on it.
#   deploy/deploy.sh               build + push + restart
#   deploy/deploy.sh --push-only   build + push
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REPO="$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/atya"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}" >/dev/null
TAG=$(git rev-parse --short HEAD 2>/dev/null || date +%s)
docker buildx build --platform linux/arm64 -t "$REPO:latest" -t "$REPO:$TAG" --push .
echo "pushed $REPO:$TAG"
[ "${1:-}" = "--push-only" ] && exit 0
deploy/run.sh "/opt/atya/update.sh && cd /opt/atya && docker compose ps"
