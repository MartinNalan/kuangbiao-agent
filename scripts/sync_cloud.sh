#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLOUD_CONFIG="${CLOUD_CONFIG:-${PROJECT_ROOT}/.cloud.env}"

if [[ ! -f "${CLOUD_CONFIG}" ]]; then
  echo "Missing ${CLOUD_CONFIG}. Copy .cloud.env.example and fill the SSH password." >&2
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
: "${CLOUD_AGENTMAIL_PROXY_URL:=}"
: "${SYNC_KB_DB:=true}"

if [[ "${CLOUD_APP_DIR}" != "/opt/kuangbiao-agent" ]]; then
  echo "Current systemd templates require CLOUD_APP_DIR=/opt/kuangbiao-agent." >&2
  exit 1
fi

LOCAL_KB_DB="${PROJECT_ROOT}/data/knowledge_base/db/knowledge_base.sqlite"
LOCAL_ANN_INDEX="${PROJECT_ROOT}/data/knowledge_base/indexes/dense.usearch"
LOCAL_ANN_MANIFEST="${PROJECT_ROOT}/data/knowledge_base/indexes/dense_manifest.json"
if [[ "${SYNC_KB_DB}" == "true" && ! -f "${LOCAL_KB_DB}" ]]; then
  echo "Missing private knowledge database: ${LOCAL_KB_DB}" >&2
  exit 1
fi
if [[ "${SYNC_KB_DB}" == "true" && ( ! -f "${LOCAL_ANN_INDEX}" || ! -f "${LOCAL_ANN_MANIFEST}" ) ]]; then
  echo "Missing private ANN index or manifest. Run scripts/build_ann_index.py first." >&2
  exit 1
fi

if [[ ! -f "${PROJECT_ROOT}/.env" ]]; then
  echo "Missing local .env runtime configuration." >&2
  exit 1
fi

export SSHPASS="${CLOUD_SSH_PASSWORD}"
CONTROL_DIR="$(mktemp -d)"
CONTROL_PATH="${CONTROL_DIR}/ssh-control"
SSH=(
  sshpass -e ssh
  -p "${CLOUD_SSH_PORT}"
  -o StrictHostKeyChecking=accept-new
  -o ControlMaster=auto
  -o ControlPersist=10m
  -o "ControlPath=${CONTROL_PATH}"
)
RSYNC_SSH="ssh -p ${CLOUD_SSH_PORT} -o StrictHostKeyChecking=accept-new -o ControlMaster=auto -o ControlPersist=10m -o ControlPath=${CONTROL_PATH}"
REMOTE="${CLOUD_USER}@${CLOUD_HOST}"

cleanup() {
  ssh -p "${CLOUD_SSH_PORT}" -o "ControlPath=${CONTROL_PATH}" -O exit "${REMOTE}" >/dev/null 2>&1 || true
  rm -rf "${CONTROL_DIR}"
}
trap cleanup EXIT

"${SSH[@]}" "${REMOTE}" "mkdir -p \
  '${CLOUD_APP_DIR}/data/knowledge_base/db' \
  '${CLOUD_APP_DIR}/data/knowledge_base/indexes' \
  '${CLOUD_APP_DIR}/data/app' \
  '${CLOUD_APP_DIR}/data/backups' && \
  stamp=\$(date -u +%Y%m%dT%H%M%SZ); \
  if [ -f '${CLOUD_APP_DIR}/data/knowledge_base/db/knowledge_base.sqlite' ]; then \
    cp '${CLOUD_APP_DIR}/data/knowledge_base/db/knowledge_base.sqlite' \
      '${CLOUD_APP_DIR}/data/backups/knowledge_base-'\"\${stamp}\"'.sqlite'; \
  fi; \
  if [ -f '${CLOUD_APP_DIR}/data/app/application.sqlite' ]; then \
    cp '${CLOUD_APP_DIR}/data/app/application.sqlite' \
      '${CLOUD_APP_DIR}/data/backups/application-'\"\${stamp}\"'.sqlite'; \
  fi; \
  ls -1t '${CLOUD_APP_DIR}'/data/backups/knowledge_base-*.sqlite 2>/dev/null | tail -n +4 | xargs -r rm -f; \
  ls -1t '${CLOUD_APP_DIR}'/data/backups/application-*.sqlite 2>/dev/null | tail -n +4 | xargs -r rm -f"

sshpass -e rsync -az --info=progress2 \
  -e "${RSYNC_SSH}" \
  --exclude .git/ \
  --exclude .venv/ \
  --exclude data/ \
  --exclude .env \
  --exclude .cloud.env \
  --exclude __pycache__/ \
  --exclude '*.pyc' \
  "${PROJECT_ROOT}/" "${REMOTE}:${CLOUD_APP_DIR}/"

if [[ "${SYNC_KB_DB}" == "true" ]]; then
  sshpass -e rsync -az --info=progress2 -e "${RSYNC_SSH}" \
    "${LOCAL_KB_DB}" "${REMOTE}:${CLOUD_APP_DIR}/data/knowledge_base/db/knowledge_base.sqlite"
  sshpass -e rsync -az --partial-dir=.rsync-partial --info=progress2 -e "${RSYNC_SSH}" \
    "${LOCAL_ANN_INDEX}" "${REMOTE}:${CLOUD_APP_DIR}/data/knowledge_base/indexes/dense.usearch"
  sshpass -e rsync -az --partial-dir=.rsync-partial --info=progress2 -e "${RSYNC_SSH}" \
    "${LOCAL_ANN_MANIFEST}" "${REMOTE}:${CLOUD_APP_DIR}/data/knowledge_base/indexes/dense_manifest.json"
else
  echo "Skipping private knowledge database synchronization (SYNC_KB_DB=${SYNC_KB_DB})."
fi

sshpass -e scp -P "${CLOUD_SSH_PORT}" -o StrictHostKeyChecking=accept-new \
  -o ControlMaster=auto -o ControlPersist=10m -o "ControlPath=${CONTROL_PATH}" \
  "${PROJECT_ROOT}/.env" "${REMOTE}:${CLOUD_APP_DIR}/.env"

"${SSH[@]}" "${REMOTE}" "sed -i \
  -e 's|^KNOWLEDGE_BASE_URL=.*|KNOWLEDGE_BASE_URL=http://127.0.0.1:18081|' \
  -e 's|^APP_DB_PATH=.*|APP_DB_PATH=${CLOUD_APP_DIR}/data/app/application.sqlite|' \
  -e 's|^ANN_INDEX_PATH=.*|ANN_INDEX_PATH=${CLOUD_APP_DIR}/data/knowledge_base/indexes/dense.usearch|' \
  -e 's|^ANN_MANIFEST_PATH=.*|ANN_MANIFEST_PATH=${CLOUD_APP_DIR}/data/knowledge_base/indexes/dense_manifest.json|' \
  -e 's|^RETRIEVAL_TRACE_PATH=.*|RETRIEVAL_TRACE_PATH=${CLOUD_APP_DIR}/data/app/retrieval_traces.jsonl|' \
  -e 's|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=http://${CLOUD_HOST}|' \
  -e 's|^SESSION_COOKIE_SECURE=.*|SESSION_COOKIE_SECURE=false|' \
  -e 's|^AGENTMAIL_PROXY_URL=.*|AGENTMAIL_PROXY_URL=${CLOUD_AGENTMAIL_PROXY_URL}|' \
  '${CLOUD_APP_DIR}/.env' && \
  grep -q '^KNOWLEDGE_DB_PATH=' '${CLOUD_APP_DIR}/.env' || echo 'KNOWLEDGE_DB_PATH=${CLOUD_APP_DIR}/data/knowledge_base/db/knowledge_base.sqlite' >> '${CLOUD_APP_DIR}/.env'; \
  grep -q '^ANN_INDEX_PATH=' '${CLOUD_APP_DIR}/.env' || echo 'ANN_INDEX_PATH=${CLOUD_APP_DIR}/data/knowledge_base/indexes/dense.usearch' >> '${CLOUD_APP_DIR}/.env'; \
  grep -q '^ANN_MANIFEST_PATH=' '${CLOUD_APP_DIR}/.env' || echo 'ANN_MANIFEST_PATH=${CLOUD_APP_DIR}/data/knowledge_base/indexes/dense_manifest.json' >> '${CLOUD_APP_DIR}/.env'; \
  grep -q '^RETRIEVAL_TRACE_PATH=' '${CLOUD_APP_DIR}/.env' || echo 'RETRIEVAL_TRACE_PATH=${CLOUD_APP_DIR}/data/app/retrieval_traces.jsonl' >> '${CLOUD_APP_DIR}/.env'; \
  grep -q '^AGENTMAIL_PROXY_URL=' '${CLOUD_APP_DIR}/.env' || echo 'AGENTMAIL_PROXY_URL=${CLOUD_AGENTMAIL_PROXY_URL}' >> '${CLOUD_APP_DIR}/.env'"

"${SSH[@]}" "${REMOTE}" "bash '${CLOUD_APP_DIR}/deploy/bootstrap_server.sh' '${CLOUD_APP_DIR}'"

"${SSH[@]}" "${REMOTE}" "if sudo -u kuangbiao env PYTHONPATH='${CLOUD_APP_DIR}/src' \
  '${CLOUD_APP_DIR}/.venv/bin/python' '${CLOUD_APP_DIR}/scripts/setup_agentmail.py' \
  --env '${CLOUD_APP_DIR}/.env'; then \
    sed -i 's|^REGISTRATION_ENABLED=.*|REGISTRATION_ENABLED=true|' '${CLOUD_APP_DIR}/.env'; \
    echo 'AgentMail registration email is enabled.'; \
  else \
    sed -i 's|^REGISTRATION_ENABLED=.*|REGISTRATION_ENABLED=false|' '${CLOUD_APP_DIR}/.env'; \
    echo 'WARNING: AgentMail is unreachable; public registration was disabled.' >&2; \
  fi; \
  chmod 600 '${CLOUD_APP_DIR}/.env'; \
  systemctl restart kuangbiao-api.service; \
  for attempt in \$(seq 1 20); do \
    if curl --fail --silent --max-time 3 http://127.0.0.1:18080/health >/dev/null; then exit 0; fi; \
    sleep 1; \
  done; \
  echo 'Timed out waiting for geowiki API after restart.' >&2; \
  exit 1"

echo "Deployment completed: http://${CLOUD_HOST}/"
