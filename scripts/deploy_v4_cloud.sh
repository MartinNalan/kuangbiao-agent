#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLOUD_CONFIG="${CLOUD_CONFIG:-${PROJECT_ROOT}/.cloud.env}"
MODE="${1:-deploy}"
ROLLBACK_ID="${2:-}"
V4_MANIFEST_REL="data/knowledge_base_v4/runtime_private/hybrid_fixed20_v4/runtime_manifest.json"
V4_MANIFEST="${PROJECT_ROOT}/${V4_MANIFEST_REL}"

V4_ASSETS=(
  "data/knowledge_base_v4/db/corpus.sqlite"
  "data/knowledge_base_v4/retrieval_preprocessing_v1/retrieval_units_v1.jsonl"
  "data/knowledge_base_v4/runtime_private/hybrid_fixed20_v4/fts.sqlite"
  "${V4_MANIFEST_REL}"
  "data/knowledge_base_v4/embedding_artifacts_private/qwen37_text_embedding_1024_t027_v1/manifest.json"
  "data/knowledge_base_v4/embedding_artifacts_private/qwen37_text_embedding_1024_t027_v1/document_embeddings.npy"
  "data/knowledge_base_v4/embedding_artifacts_private/qwen37_text_embedding_1024_t027_v1/row_mapping.jsonl"
  "schemas/v4_governed_concept_families_v1.json"
)

usage() {
  echo "Usage: bash scripts/deploy_v4_cloud.sh [deploy|rollback BACKUP_ID]" >&2
}

if [[ "${MODE}" != "deploy" && "${MODE}" != "rollback" ]]; then
  usage
  exit 2
fi
if [[ "${MODE}" == "rollback" && -z "${ROLLBACK_ID}" ]]; then
  usage
  exit 2
fi
if [[ ! -f "${CLOUD_CONFIG}" ]]; then
  echo "Missing cloud configuration: ${CLOUD_CONFIG}" >&2
  exit 1
fi

set -a
source "${CLOUD_CONFIG}"
set +a

: "${CLOUD_HOST:?CLOUD_HOST is required}"
: "${CLOUD_USER:?CLOUD_USER is required}"
: "${CLOUD_SSH_PASSWORD:?CLOUD_SSH_PASSWORD is required}"
: "${CLOUD_SSH_PORT:=22}"
: "${CLOUD_APP_DIR:=/opt/kuangbiao-agent}"

if [[ "${CLOUD_APP_DIR}" != "/opt/kuangbiao-agent" ]]; then
  echo "Current systemd units require CLOUD_APP_DIR=/opt/kuangbiao-agent." >&2
  exit 1
fi

export SSHPASS="${CLOUD_SSH_PASSWORD}"
CONTROL_DIR="$(mktemp -d)"
CONTROL_PATH="${CONTROL_DIR}/ssh-control"
SSH=(
  sshpass -e ssh
  -p "${CLOUD_SSH_PORT}"
  -o LogLevel=ERROR
  -o StrictHostKeyChecking=accept-new
  -o ControlMaster=auto
  -o ControlPersist=10m
  -o "ControlPath=${CONTROL_PATH}"
)
RSYNC_SSH="ssh -p ${CLOUD_SSH_PORT} -o LogLevel=ERROR -o StrictHostKeyChecking=accept-new -o ControlMaster=auto -o ControlPersist=10m -o ControlPath=${CONTROL_PATH}"
REMOTE="${CLOUD_USER}@${CLOUD_HOST}"

cleanup() {
  ssh -p "${CLOUD_SSH_PORT}" -o LogLevel=ERROR -o "ControlPath=${CONTROL_PATH}" \
    -O exit "${REMOTE}" >/dev/null 2>&1 || true
  rm -rf "${CONTROL_DIR}"
}
trap cleanup EXIT

remote_rollback() {
  local backup_id="$1"
  "${SSH[@]}" "${REMOTE}" bash -s -- "${CLOUD_APP_DIR}" "${backup_id}" <<'REMOTE'
set -Eeuo pipefail
app="$1"
backup_id="$2"
backup="${app}/data/backups/v4-cutover-${backup_id}"
if [[ ! -d "${backup}" || ! -f "${backup}/application-code.tar.gz" || ! -f "${backup}/runtime.env" ]]; then
  echo "Rollback point is incomplete: ${backup_id}" >&2
  exit 1
fi
if [[ "$(id -u)" -eq 0 ]]; then sudo_cmd=(); else sudo_cmd=(sudo); fi
"${sudo_cmd[@]}" systemctl stop kuangbiao-api.service kuangbiao-kb.service
tar -xzf "${backup}/application-code.tar.gz" -C "${app}"
install -m 0600 "${backup}/runtime.env" "${app}/.env"
if [[ -f "${backup}/kuangbiao-api.service" ]]; then
  install -m 0644 "${backup}/kuangbiao-api.service" /etc/systemd/system/kuangbiao-api.service
fi
if [[ -f "${backup}/kuangbiao-kb.service" ]]; then
  install -m 0644 "${backup}/kuangbiao-kb.service" /etc/systemd/system/kuangbiao-kb.service
fi
chown kuangbiao:kuangbiao "${app}/.env"
"${sudo_cmd[@]}" systemctl daemon-reload
"${sudo_cmd[@]}" systemctl restart kuangbiao-kb.service
for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:18081/knowledge/health >/dev/null; then break; fi
  sleep 2
done
curl --fail --silent --max-time 5 http://127.0.0.1:18081/knowledge/health >/dev/null
"${sudo_cmd[@]}" systemctl restart kuangbiao-api.service
for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:18080/health >/dev/null; then break; fi
  sleep 2
done
curl --fail --silent --max-time 5 http://127.0.0.1:18080/health >/dev/null
echo "rollback_status=complete"
echo "rollback_id=${backup_id}"
REMOTE
}

if [[ "${MODE}" == "rollback" ]]; then
  remote_rollback "${ROLLBACK_ID}"
  exit 0
fi

for relative_path in "${V4_ASSETS[@]}"; do
  if [[ ! -f "${PROJECT_ROOT}/${relative_path}" ]]; then
    echo "Missing v4 runtime asset: ${relative_path}" >&2
    exit 1
  fi
done

PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Missing local project Python: ${PYTHON_BIN}" >&2
  exit 1
fi

"${PYTHON_BIN}" - "${PROJECT_ROOT}" "${V4_MANIFEST}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("runtime_id") != "v4-hybrid-fixed20-p1fix-v4":
    raise SystemExit("Unexpected v4 runtime_id")
for label, item in manifest["artifacts"].items():
    path = Path(item["path"])
    if not path.is_absolute():
        path = root / path
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != item["sha256"]:
        raise SystemExit(f"Local v4 hash mismatch: {label}")
print("local_v4_assets=verified")
PY

DEPLOY_ID="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_CREATED=false
DEPLOY_COMPLETE=false

rollback_on_error() {
  local rc=$?
  trap - ERR
  if [[ "${BACKUP_CREATED}" == "true" && "${DEPLOY_COMPLETE}" != "true" ]]; then
    echo "deployment_status=failed_rolling_back" >&2
    remote_rollback "${DEPLOY_ID}" || echo "automatic_rollback_status=failed" >&2
  fi
  exit "${rc}"
}
trap rollback_on_error ERR

"${SSH[@]}" "${REMOTE}" bash -s -- "${CLOUD_APP_DIR}" "${DEPLOY_ID}" <<'REMOTE'
set -Eeuo pipefail
app="$1"
deploy_id="$2"
backup="${app}/data/backups/v4-cutover-${deploy_id}"
if [[ "$(id -u)" -eq 0 ]]; then sudo_cmd=(); else sudo_cmd=(sudo); fi
mkdir -p "${backup}"
install -m 0600 "${app}/.env" "${backup}/runtime.env"
if [[ -f "${app}/data/app/application.sqlite" ]]; then
  cp -a --reflink=auto "${app}/data/app/application.sqlite" "${backup}/application.sqlite"
fi
for unit in kuangbiao-api.service kuangbiao-kb.service; do
  if [[ -f "/etc/systemd/system/${unit}" ]]; then
    cp -a "/etc/systemd/system/${unit}" "${backup}/${unit}"
  fi
done
if [[ -f /etc/nginx/sites-available/kuangbiao.conf ]]; then
  cp -a /etc/nginx/sites-available/kuangbiao.conf "${backup}/nginx-kuangbiao.conf"
fi
paths=()
for candidate in src scripts deploy web schemas requirements.txt pyproject.toml; do
  [[ -e "${app}/${candidate}" ]] && paths+=("${candidate}")
done
tar -czf "${backup}/application-code.tar.gz" -C "${app}" "${paths[@]}"
{
  [[ -f "${app}/data/app/application.sqlite" ]] && sha256sum "${app}/data/app/application.sqlite"
  [[ -f "${app}/data/knowledge_base/db/knowledge_base.sqlite" ]] && sha256sum "${app}/data/knowledge_base/db/knowledge_base.sqlite"
  [[ -f "${app}/data/knowledge_base/indexes/dense.usearch" ]] && sha256sum "${app}/data/knowledge_base/indexes/dense.usearch"
  [[ -f "${app}/data/knowledge_base/indexes/dense_manifest.json" ]] && sha256sum "${app}/data/knowledge_base/indexes/dense_manifest.json"
} > "${backup}/pre_cutover_hashes.sha256"
chmod 700 "${backup}"
chmod 600 "${backup}"/*
echo "backup_status=complete"
echo "backup_id=${deploy_id}"
REMOTE
BACKUP_CREATED=true

for directory in src scripts deploy web schemas; do
  if [[ -d "${PROJECT_ROOT}/${directory}" ]]; then
    sshpass -e rsync -az --info=stats2 -e "${RSYNC_SSH}" \
      --exclude __pycache__/ --exclude '*.pyc' \
      "${PROJECT_ROOT}/${directory}/" "${REMOTE}:${CLOUD_APP_DIR}/${directory}/"
  fi
done
for file in requirements.txt pyproject.toml; do
  if [[ -f "${PROJECT_ROOT}/${file}" ]]; then
    sshpass -e rsync -az --info=stats2 -e "${RSYNC_SSH}" \
      "${PROJECT_ROOT}/${file}" "${REMOTE}:${CLOUD_APP_DIR}/${file}"
  fi
done

(
  cd "${PROJECT_ROOT}"
  relative_assets=()
  for item in "${V4_ASSETS[@]}"; do relative_assets+=("./${item}"); done
  sshpass -e rsync -azR --partial-dir=.rsync-partial --info=progress2 \
    -e "${RSYNC_SSH}" "${relative_assets[@]}" "${REMOTE}:${CLOUD_APP_DIR}/"
)

"${SSH[@]}" "${REMOTE}" bash -s -- "${CLOUD_APP_DIR}" "${V4_MANIFEST_REL}" <<'REMOTE'
set -Eeuo pipefail
app="$1"
manifest_rel="$2"
if [[ "$(id -u)" -eq 0 ]]; then sudo_cmd=(); else sudo_cmd=(sudo); fi
"${app}/.venv/bin/pip" install --disable-pip-version-check --timeout 60 --retries 5 -r "${app}/requirements.txt" >/dev/null
chown -R kuangbiao:kuangbiao "${app}/src" "${app}/scripts" "${app}/deploy" "${app}/web" "${app}/schemas" "${app}/data/knowledge_base_v4"
find "${app}/data/knowledge_base_v4" -type d -exec chmod 700 {} +
find "${app}/data/knowledge_base_v4" -type f -exec chmod 600 {} +
python3 - "${app}" "${manifest_rel}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / sys.argv[2]).read_text(encoding="utf-8"))
for label, item in manifest["artifacts"].items():
    path = Path(item["path"])
    if not path.is_absolute():
        path = root / path
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != item["sha256"]:
        raise SystemExit(f"Remote v4 hash mismatch: {label}")
print("remote_v4_assets=verified")
PY
sudo -u kuangbiao env \
  PYTHONPATH="${app}/src" \
  KNOWLEDGE_RUNTIME_VERSION=v4 \
  V4_RUNTIME_MANIFEST="${app}/${manifest_rel}" \
  V4_CANDIDATE_DB_PATH="${app}/data/knowledge_base_v4/runtime_private/candidates.sqlite" \
  "${app}/.venv/bin/python" - <<'PY'
from mining_qa import knowledge_service

health = knowledge_service.store.health()
expected = {
    "runtime_version": "v4",
    "runtime_id": "v4-hybrid-fixed20-p1fix-v4",
    "document_count": 156,
    "retrieval_leaf_count": 23250,
    "vector_count": 23250,
    "ann_available": False,
    "kg_entity_count": 0,
    "kg_relation_count": 0,
}
for key, value in expected.items():
    if health.get(key) != value:
        raise SystemExit(f"v4 preflight mismatch: {key}")
if not health.get("query_embedding_ready"):
    raise SystemExit("v4 query embedding is not configured")
print("remote_v4_store_preflight=passed")
PY
REMOTE

"${SSH[@]}" "${REMOTE}" bash -s -- "${CLOUD_APP_DIR}" "${V4_MANIFEST_REL}" <<'REMOTE'
set -Eeuo pipefail
app="$1"
manifest_rel="$2"
if [[ "$(id -u)" -eq 0 ]]; then sudo_cmd=(); else sudo_cmd=(sudo); fi
python3 - "${app}/.env" "${app}/${manifest_rel}" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
updates = {
    "KNOWLEDGE_RUNTIME_VERSION": "v4",
    "V4_RUNTIME_MANIFEST": sys.argv[2],
    "V4_CANDIDATE_DB_PATH": str(Path(sys.argv[2]).parent.parent / "candidates.sqlite"),
    "V4_EMBEDDING_MODEL": "qwen3.7-text-embedding",
    "V4_EMBEDDING_DIMENSIONS": "1024",
    "V4_EMBEDDING_TIMEOUT_SECONDS": "3",
    "V4_EMBEDDING_MAX_RETRIES": "1",
    "V4_QUERY_EMBEDDING_CACHE_SIZE": "256",
    "FAST_PATH_SHADOW_ENABLED": "true",
    "FAST_PATH_SHADOW_SAMPLE_RATE": "0.2",
    "FAST_PATH_SHADOW_LOG_PATH": str(Path(sys.argv[2]).parents[3] / "app" / "fast_path_shadow.jsonl"),
    "FAST_PATH_SHADOW_MAX_BYTES": "2097152",
    "FAST_PATH_SHADOW_BACKUP_COUNT": "2",
    "FAST_PATH_SHADOW_DEDUP_TTL_SECONDS": "86400",
    "FAST_PATH_SHADOW_DEDUP_MAX_ENTRIES": "4096",
    "FAST_PATH_SHADOW_MAX_CONCURRENCY": "1",
}
obsolete = {"KNOWLEDGE_LEGACY_DB_PATH"}
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
result = []
for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in obsolete:
            continue
        if key in updates:
            if key not in seen:
                result.append(f"{key}={updates[key]}")
                seen.add(key)
            continue
    result.append(line)
for key, value in updates.items():
    if key not in seen:
        result.append(f"{key}={value}")
temporary = path.with_name(".env.v4-cutover.tmp")
temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
temporary.replace(path)
PY
chown kuangbiao:kuangbiao "${app}/.env"
install -m 0644 "${app}/deploy/systemd/kuangbiao-kb.service" /etc/systemd/system/kuangbiao-kb.service
install -m 0644 "${app}/deploy/systemd/kuangbiao-api.service" /etc/systemd/system/kuangbiao-api.service
"${sudo_cmd[@]}" systemctl daemon-reload
"${sudo_cmd[@]}" systemctl restart kuangbiao-kb.service
for _ in $(seq 1 45); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:18081/knowledge/health >/dev/null; then break; fi
  sleep 2
done
python3 - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:18081/knowledge/health", timeout=5) as response:
    health = json.load(response)
expected = {
    "status": "ok",
    "runtime_version": "v4",
    "runtime_id": "v4-hybrid-fixed20-p1fix-v4",
    "document_count": 156,
    "retrieval_leaf_count": 23250,
    "vector_count": 23250,
    "ann_available": False,
    "ann_count": 0,
    "kg_entity_count": 0,
    "kg_relation_count": 0,
}
for key, value in expected.items():
    if health.get(key) != value:
        raise SystemExit(f"v4 KB health mismatch: {key}")
if not health.get("query_embedding_ready"):
    raise SystemExit("v4 query embedding is not ready")
print("v4_kb_health=passed")
PY
"${sudo_cmd[@]}" systemctl restart kuangbiao-api.service
for _ in $(seq 1 45); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:18080/health >/dev/null; then break; fi
  sleep 2
done
python3 - "${app}" <<'PY'
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

app = Path(sys.argv[1])
with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=5) as response:
    health = json.load(response)
if not health.get("ok") or not health.get("knowledge_base_enabled"):
    raise SystemExit("public API health failed")
if not health.get("auth_required") or not health.get("registration_enabled"):
    raise SystemExit("account or registration architecture changed")
if not health.get("email_verification_ready"):
    raise SystemExit("registration email architecture is not ready")
db = app / "data/app/application.sqlite"
connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
integrity = connection.execute("pragma integrity_check").fetchone()[0]
users = connection.execute("select count(*) from users").fetchone()[0]
connection.close()
if integrity != "ok":
    raise SystemExit("application database integrity failed")
if users < 1:
    raise SystemExit("application user records disappeared")
print("platform_health=passed")
print(f"registered_users={users}")
PY
public_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1/knowledge/health)"
if [[ "${public_code}" != "404" ]]; then
  echo "Private KB route exposure changed: ${public_code}" >&2
  exit 1
fi
echo "public_private_kb_boundary=passed"
REMOTE

DEPLOY_COMPLETE=true
trap - ERR
echo "deployment_status=complete"
echo "backup_id=${DEPLOY_ID}"
echo "runtime_id=v4-hybrid-fixed20-p1fix-v4"
