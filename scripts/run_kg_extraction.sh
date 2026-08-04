#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

LIMIT=20
FULL=false
CONCURRENCY=12
TENANT_ID="local"
PROJECT_ID="computer-science"
MODEL="${KG_MODEL:-}"
BASE_URL="${KG_BASE_URL:-}"
CHAR_BUDGET="${KG_CHAR_BUDGET:-20000}"
CHECKPOINT="/data/graph_backfill_manifest.json"
DRY_RUN=false
FORCE=false
SKIP_ERRORS=false
ASSUME_YES=false
DOCUMENT_IDS=()
DOCUMENT_ID_COUNT=0

usage() {
  cat <<'EOF'
Run bounded, resumable OpenAI-compatible knowledge-graph extraction in Docker.

This script always uses exactly 12 concurrent document workers. By default it runs
a 20-document pilot. Extracted entities, relations, and aliases remain pending
candidates; this script never approves facts.

Usage:
  ./scripts/run_kg_extraction.sh [options]

Options:
  --limit N             Process at most N incomplete documents (default: 20).
  --full                Process every incomplete document; asks for confirmation.
  --tenant-id ID        Tenant scope (default: local).
  --project-id ID       Project scope (default: computer-science).
  --document-id UUID    Process one document; may be repeated.
  --model MODEL         Override GRAPH_EXTRACTION_MODEL.
  --base-url URL        Compatible endpoint. localhost is translated for Docker.
  --char-budget N       Representative characters per document (default: 20000).
  --checkpoint PATH     Container checkpoint path.
  --dry-run             List the bounded selection without calling the model.
  --force               Re-extract even if the same revision already completed.
  --skip-errors         Keep failed checkpoints and continue with unseen articles.
  --yes                 Skip confirmation for --full or --force.
  -h, --help            Show this help.

Credentials:
  The script reads MODEL_API_KEY and DOCKER_MODEL_BASE_URL through Docker Compose
  from the shell or repository .env. If MODEL_API_KEY is absent, it prompts once
  with hidden input. The key is never written by this script.

Examples:
  ./scripts/run_kg_extraction.sh --dry-run
  ./scripts/run_kg_extraction.sh --limit 5
  ./scripts/run_kg_extraction.sh --limit 20
  ./scripts/run_kg_extraction.sh --full
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

require_value() {
  local option="$1"
  local value="${2:-}"
  [[ -n "${value}" ]] || die "${option} requires a value"
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

env_file_has_value() {
  local name="$1"
  [[ -f .env ]] && grep -Eq "^[[:space:]]*${name}[[:space:]]*=[[:space:]]*[^[:space:]#]+" .env
}

normalize_docker_url() {
  local value="$1"
  value="${value/http:\/\/localhost/http:\/\/host.docker.internal}"
  value="${value/http:\/\/127.0.0.1/http:\/\/host.docker.internal}"
  printf '%s' "${value}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)
      require_value "$1" "${2:-}"
      LIMIT="$2"
      FULL=false
      shift 2
      ;;
    --full)
      FULL=true
      shift
      ;;
    --tenant-id)
      require_value "$1" "${2:-}"
      TENANT_ID="$2"
      shift 2
      ;;
    --project-id)
      require_value "$1" "${2:-}"
      PROJECT_ID="$2"
      shift 2
      ;;
    --document-id)
      require_value "$1" "${2:-}"
      DOCUMENT_IDS[${DOCUMENT_ID_COUNT}]="$2"
      DOCUMENT_ID_COUNT=$((DOCUMENT_ID_COUNT + 1))
      shift 2
      ;;
    --model)
      require_value "$1" "${2:-}"
      MODEL="$2"
      shift 2
      ;;
    --base-url)
      require_value "$1" "${2:-}"
      BASE_URL="$2"
      shift 2
      ;;
    --char-budget)
      require_value "$1" "${2:-}"
      CHAR_BUDGET="$2"
      shift 2
      ;;
    --checkpoint)
      require_value "$1" "${2:-}"
      CHECKPOINT="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --force)
      FORCE=true
      shift
      ;;
    --skip-errors)
      SKIP_ERRORS=true
      shift
      ;;
    --yes)
      ASSUME_YES=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

is_positive_integer "${LIMIT}" || die "--limit must be a positive integer"
is_positive_integer "${CHAR_BUDGET}" || die "--char-budget must be an integer from 5000 to 100000"
(( CHAR_BUDGET >= 5000 && CHAR_BUDGET <= 100000 )) \
  || die "--char-budget must be an integer from 5000 to 100000"
[[ "${CHECKPOINT}" == /data/* ]] \
  || die "--checkpoint must stay inside the shared /data Docker volume"

command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
docker info >/dev/null 2>&1 || die "Docker Desktop is not running"
docker compose config --quiet

if [[ "${FULL}" == true || "${FORCE}" == true ]]; then
  if [[ "${ASSUME_YES}" != true ]]; then
    if [[ ! -t 0 ]]; then
      die "--full/--force requires an interactive terminal or --yes"
    fi
    printf 'This may consume substantial model tokens. Type RUN to continue: '
    read -r confirmation
    [[ "${confirmation}" == "RUN" ]] || die "cancelled"
  fi
fi

if [[ -z "${MODEL_API_KEY:-}" ]] && ! env_file_has_value MODEL_API_KEY; then
  if [[ ! -t 0 ]]; then
    die "MODEL_API_KEY is missing; export it or add it to .env"
  fi
  printf 'MODEL_API_KEY (hidden): '
  read -r -s MODEL_API_KEY
  printf '\n'
  [[ -n "${MODEL_API_KEY}" ]] || die "MODEL_API_KEY cannot be empty"
  export MODEL_API_KEY
fi

export MODEL_PROVIDER="local-openai-compatible"
export GRAPH_EXTRACTOR_MODE="openai"
export GRAPH_EXTRACTION_INPUT_CHAR_BUDGET="${CHAR_BUDGET}"
export GRAPH_EXTRACTION_PUBLIC_REFERENCE_CHAR_BUDGET="${CHAR_BUDGET}"
export OUTBOX_DISPATCHER_ENABLED="false"
export INGESTION_WORKER_ENABLED="false"
export LEARNING_JOB_WORKER_ENABLED="false"
export VISION_ENABLED="false"

if [[ -n "${MODEL}" ]]; then
  export GRAPH_EXTRACTION_MODEL="${MODEL}"
fi
if [[ -n "${BASE_URL}" ]]; then
  export DOCKER_MODEL_BASE_URL="$(normalize_docker_url "${BASE_URL}")"
elif ! env_file_has_value DOCKER_MODEL_BASE_URL; then
  export DOCKER_MODEL_BASE_URL="http://host.docker.internal:55523/v1"
fi

docker compose up -d postgres qdrant neo4j

CLI_ARGS=(
  python -m app.graph.backfill_cli
  --tenant-id "${TENANT_ID}"
  --project-id "${PROJECT_ID}"
  --concurrency "${CONCURRENCY}"
  --checkpoint "${CHECKPOINT}"
)

if [[ "${FULL}" != true ]]; then
  CLI_ARGS+=(--limit "${LIMIT}")
fi
if (( DOCUMENT_ID_COUNT > 0 )); then
  for document_id in "${DOCUMENT_IDS[@]}"; do
    CLI_ARGS+=(--document-id "${document_id}")
  done
fi
if [[ "${DRY_RUN}" == true ]]; then
  CLI_ARGS+=(--dry-run)
fi
if [[ "${FORCE}" == true ]]; then
  CLI_ARGS+=(--force)
fi
if [[ "${SKIP_ERRORS}" == true ]]; then
  CLI_ARGS+=(--skip-errors)
fi

RUN_ARGS=(run --rm --no-deps --build)

printf '\nKG extraction configuration\n'
printf '  scope:       %s/%s\n' "${TENANT_ID}" "${PROJECT_ID}"
if [[ "${FULL}" == true ]]; then
  printf '  documents:   all incomplete documents\n'
else
  printf '  documents:   at most %s incomplete documents\n' "${LIMIT}"
fi
printf '  concurrency: %s\n' "${CONCURRENCY}"
printf '  char budget: %s per document\n' "${CHAR_BUDGET}"
printf '  checkpoint:  %s\n' "${CHECKPOINT}"
printf '  skip errors: %s\n' "${SKIP_ERRORS}"
printf '  dry run:     %s\n\n' "${DRY_RUN}"

docker compose "${RUN_ARGS[@]}" app "${CLI_ARGS[@]}"

printf '\nDone. Candidates remain pending until they pass the graph review gate.\n'
printf 'Re-run the same command to resume from %s.\n' "${CHECKPOINT}"
