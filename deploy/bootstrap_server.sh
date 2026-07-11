#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/opt/kuangbiao-agent}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

if [[ "${APP_DIR}" != "/opt/kuangbiao-agent" ]]; then
  echo "The current systemd templates expect /opt/kuangbiao-agent." >&2
  exit 1
fi

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "Missing ${APP_DIR}/.env" >&2
  exit 1
fi

if [[ ! -f "${APP_DIR}/data/knowledge_base/db/knowledge_base.sqlite" ]]; then
  echo "Missing private knowledge-base SQLite file." >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip nginx redis-server rsync curl

if ! id -u kuangbiao >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin kuangbiao
fi

mkdir -p "${APP_DIR}/data/app"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --index-url "${PIP_INDEX_URL}" --timeout 60 --retries 5 --upgrade pip
"${APP_DIR}/.venv/bin/pip" install --index-url "${PIP_INDEX_URL}" --timeout 60 --retries 5 -r "${APP_DIR}/requirements.txt"

chown -R kuangbiao:kuangbiao "${APP_DIR}"
chmod 600 "${APP_DIR}/.env"
chmod 700 "${APP_DIR}/data" "${APP_DIR}/data/knowledge_base" "${APP_DIR}/data/knowledge_base/db" "${APP_DIR}/data/app"
chmod 600 "${APP_DIR}/data/knowledge_base/db/knowledge_base.sqlite"
if [[ -f "${APP_DIR}/data/app/application.sqlite" ]]; then
  chmod 600 "${APP_DIR}/data/app/application.sqlite"
fi

install -m 0644 "${APP_DIR}/deploy/systemd/kuangbiao-kb.service" /etc/systemd/system/kuangbiao-kb.service
install -m 0644 "${APP_DIR}/deploy/systemd/kuangbiao-api.service" /etc/systemd/system/kuangbiao-api.service
install -m 0644 "${APP_DIR}/deploy/nginx/kuangbiao.conf" /etc/nginx/sites-available/kuangbiao.conf
ln -sfn /etc/nginx/sites-available/kuangbiao.conf /etc/nginx/sites-enabled/kuangbiao.conf
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl daemon-reload
systemctl enable --now redis-server
systemctl enable kuangbiao-kb.service
systemctl enable kuangbiao-api.service
systemctl enable nginx
systemctl restart kuangbiao-kb.service
systemctl restart kuangbiao-api.service
systemctl restart nginx

wait_for_http() {
  local url="$1"
  local attempts=30
  while (( attempts > 0 )); do
    if curl --fail --silent --max-time 3 "${url}" >/dev/null; then
      return 0
    fi
    attempts=$((attempts - 1))
    sleep 2
  done
  echo "Timed out waiting for ${url}" >&2
  return 1
}

wait_for_http http://127.0.0.1:18081/knowledge/health
wait_for_http http://127.0.0.1:18080/health
echo "geowiki services are running."
