#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ ! -d frontend/node_modules ]]; then
  npm --prefix frontend ci
fi
npm --prefix frontend run build
docker compose up --build -d
docker compose ps
