#!/usr/bin/env bash
set -e

cd "$(dirname "$0")/.."

docker compose \
  -f generated-projects/default-project/edge-router/docker-compose.yml \
  down 2>/dev/null || true

docker compose \
  -p pingora-blue \
  -f generated-projects/default-project/blue/generated-pingora-proxy/docker-compose.bluegreen.yml \
  down --remove-orphans 2>/dev/null || true

docker compose \
  -p pingora-green \
  -f generated-projects/default-project/green/generated-pingora-proxy/docker-compose.bluegreen.yml \
  down --remove-orphans 2>/dev/null || true

docker rm -f pingora-edge-router pingora-proxy-blue pingora-proxy-green 2>/dev/null || true

echo "✅ Stopped Pingora blue/green stack"
