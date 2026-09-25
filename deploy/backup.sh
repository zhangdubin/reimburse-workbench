#!/usr/bin/env bash
# 数据库备份：导出 PostgreSQL 全量 SQL 并压缩
# 用法：./deploy/backup.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a && source .env && set +a
fi

DB_NAME="${DB_NAME:-workbench}"
DB_USER="${DB_USER:-workbench}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="backups"
OUT_FILE="${OUT_DIR}/reimburse_${STAMP}.sql"

mkdir -p "${OUT_DIR}"

echo "→ 正在导出 ${DB_NAME} ..."
docker compose exec -T db pg_dump -U "${DB_USER}" -d "${DB_NAME}" --no-owner > "${OUT_FILE}"

gzip -f "${OUT_FILE}"
echo "✔ 备份完成：${OUT_FILE}.gz  ($(du -h "${OUT_FILE}.gz" | cut -f1))"

# 只保留最近 30 份
ls -1t "${OUT_DIR}"/reimburse_*.sql.gz 2>/dev/null | tail -n +31 | xargs -r rm -f
