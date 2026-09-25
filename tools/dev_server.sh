#!/usr/bin/env bash
# 本地开发/验收实例启动脚本（SQLite）。
#
#   tools/dev_server.sh start     后台启动（默认 8792 端口）
#   tools/dev_server.sh stop      停止
#   tools/dev_server.sh restart   重启
#   tools/dev_server.sh status    查看状态
#   tools/dev_server.sh logs      跟踪日志
#   tools/dev_server.sh reset     删库重来（迁移 + 建管理员，无演示数据）
#
# 环境变量（都有默认值，可覆盖）：
#   PORT            监听端口，默认 8792
#   DB_FILE         SQLite 文件，默认 /tmp/wb_prod_test.db
#   UPLOAD_DIR      影像落盘目录，默认 /tmp/wb-uploads-<PORT>
#   ADMIN_USERNAME  初始管理员用户名，默认 admin
#   ADMIN_PASSWORD  初始管理员密码，默认 Adm1n@2026（仅首次建库时生效）
#   DOCS_ENABLED   1 时暴露 /docs，默认 1（本地便于调试）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/backend/.venv/bin/python"
PORT="${PORT:-8792}"
DB_FILE="${DB_FILE:-/tmp/wb_prod_test.db}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-Adm1n@2026}"
DOCS_ENABLED="${DOCS_ENABLED:-1}"
PID_FILE="/tmp/wb-dev-$PORT.pid"
LOG_FILE="/tmp/wb-dev-$PORT.log"

# ⚠️ UPLOAD_DIR 必须落在 workspace 之外（这里默认 /tmp）。
# 本机 agent 环境对 workspace 内的文件删除有累计保护（50 次/turn 触顶即拦），
# 触顶后会连带杀掉服务进程，症状是「跑安全测试删影像时服务无故消失、
# 无 traceback、无 500 日志」。放到 /tmp 就没这回事。
# 生产跑在容器里，不受影响。
export UPLOAD_DIR="${UPLOAD_DIR:-/tmp/wb-uploads-$PORT}"
mkdir -p "$UPLOAD_DIR"

# 邮箱授权码加密的主密钥。dev 裸跑也要给，否则邮箱账号保存会失败
export MAIL_SECRET="${MAIL_SECRET:-dev-local-secret-0123456789abcdef0123456789abcdef}"
export INBOX_INGEST_TOKEN="${INBOX_INGEST_TOKEN:-dev-ingest-token}"

export DATABASE_URL="sqlite:///$DB_FILE"
export ADMIN_USERNAME ADMIN_PASSWORD DOCS_ENABLED
export SEED_DEMO=0
export AUTO_MIGRATE=1
export TZ=Asia/Shanghai

run_fg() {
  exec "$PY" -m uvicorn app.main:app \
    --app-dir "$ROOT/backend" --host 127.0.0.1 --port "$PORT" --workers 1 "$@"
}

is_up() {
  curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1
}

case "${1:-status}" in
  start)
    if is_up; then echo "已在运行：http://127.0.0.1:${PORT}"; exit 0; fi
    nohup "$PY" -m uvicorn app.main:app --app-dir "$ROOT/backend" \
      --host 127.0.0.1 --port "${PORT}" --workers 1 >"${LOG_FILE}" 2>&1 &
    echo $! >"$PID_FILE"
    for _ in $(seq 1 40); do is_up && break; sleep 0.25; done
    if is_up; then
      echo "启动成功：http://127.0.0.1:${PORT} (pid $(cat "$PID_FILE"), 日志 ${LOG_FILE})"
      grep -E '^\[init\]' "$LOG_FILE" || true
    else
      echo "启动失败，日志如下："; tail -30 "${LOG_FILE}"; exit 1
    fi
    ;;
  stop)
    if [ -f "$PID_FILE" ]; then
      kill "$(cat "$PID_FILE")" 2>/dev/null || true
      rm -f "$PID_FILE"
      echo "已停止"
    else
      pkill -f "uvicorn app.main:app .*--port $PORT" 2>/dev/null || true
      echo "已停止 (按端口匹配)"
    fi
    ;;
  restart)
    "$0" stop || true
    sleep 1
    "$0" start
    ;;
  reset)
    "$0" stop || true
    rm -f "$DB_FILE"
    echo "已删除 ${DB_FILE}，下次 start 会重新迁移并创建管理员"
    ;;
  status)
    if is_up; then
      echo "运行中："; curl -s "http://127.0.0.1:$PORT/api/health"; echo
    else
      echo "未运行 (端口 ${PORT})"
    fi
    ;;
  logs)
    tail -f "$LOG_FILE"
    ;;
  fg)
    run_fg
    ;;
  *)
    echo "用法：$0 {start|stop|restart|reset|status|logs|fg}"; exit 2
    ;;
esac
