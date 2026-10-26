#!/usr/bin/env bash
# ============================================================================
# 从 GitHub Release 直接升级（宿主机上跑，绕开应用内的升级执行器）
#
#   bash deploy/upgrade-from-release.sh                      # 升到最新版
#   bash deploy/upgrade-from-release.sh --tag 2.9.31         # 指定版本
#   bash deploy/upgrade-from-release.sh --check              # 只体检 + 看目标版本，不动手
#   bash deploy/upgrade-from-release.sh --image /tmp/x.tar.gz --tag 2.9.31   # 用现成的镜像包
#
# 什么时候用：
#   应用内「一键升级」卡在「找不到自身容器」「执行器起不来」这类环境问题时，
#   这条路只依赖 docker / curl / sha256sum，不依赖应用自己的容器识别逻辑。
#
# 它做的事（每步失败即停，旧容器保持原样）：
#   1. 定位安装目录、app 容器、db 容器（也可以 --container/--dir 手工指定）
#   2. 按 manifest.json 的 sha256 下载镜像包（已存在则跳过）
#   3. pg_dump 备份数据库到 backups/
#   4. docker load 新镜像，并核对架构
#   5. 备份并覆盖 compose / install.sh / deploy / docs（来自骨架包）
#   6. 改 .env 的 IMAGE_TAG，docker compose up -d --no-deps app
#   7. 轮询 /api/health 直到新版本就绪；失败时打印回滚命令
#
# 兼容 bash 3.2（macOS）与 Linux；不依赖 jq。
# ============================================================================
set -uo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YEL=$'\033[33m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "${BOLD}" "${OFF}" "$*"; }
ok()   { printf '  %s✔%s %s\n' "${GREEN}" "${OFF}" "$*"; }
warn() { printf '  %s!%s %s\n' "${YEL}" "${OFF}" "$*"; }
die()  { printf '\n%s✘ %s%s\n' "${RED}" "$*" "${OFF}" >&2; exit 1; }
hint() { printf '  %s%s%s\n' "${DIM}" "$*" "${OFF}"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 脚本经常是从 /tmp 或骨架包临时目录里跑的，那里没有 compose 文件；
# 那就把 PROJ_DIR 留空，等下面用容器的 compose 标签反推（别瞎猜成 "/"）
if [[ -f "${HERE}/docker-compose.yml" ]]; then
  PROJ_DIR="${HERE}"
elif [[ -f "$(dirname "${HERE}")/docker-compose.yml" ]]; then
  PROJ_DIR="$(dirname "${HERE}")"
else
  PROJ_DIR=""
fi

REPO="zhangdubin/reimburse-workbench"
TAG=""; IMAGE_TAR=""; BUNDLE_TAR=""; APP_CONTAINER=""; CHECK_ONLY=0; NO_BUNDLE=0; DIR_GIVEN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)      REPO="$2"; shift 2 ;;
    --tag)       TAG="$2"; shift 2 ;;
    --dir)       PROJ_DIR="$2"; DIR_GIVEN=1; shift 2 ;;
    --container) APP_CONTAINER="$2"; shift 2 ;;
    --image)     IMAGE_TAR="$2"; shift 2 ;;
    --bundle)    BUNDLE_TAR="$2"; shift 2 ;;
    --no-bundle) NO_BUNDLE=1; shift ;;
    --check)     CHECK_ONLY=1; shift ;;
    -h|--help)   sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"; }
need docker
need curl
SHA=$(command -v sha256sum || command -v shasum || true)
[[ -n "${SHA}" ]] || die "缺少 sha256sum / shasum"

DOCKER="docker"
if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "找不到 docker compose（v2）或 docker-compose（v1）"
fi

ARCH="$(uname -m)"
case "${ARCH}" in
  x86_64|amd64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  armv7l) ARCH=armv7 ;;
esac

sha_of() { # 统一 sha256 输出格式
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}

# ---------- 1. 环境与目标 ----------
step "环境检查"

# 找 app 容器：优先进安装目录的 .env，其次按镜像名匹配
if [[ -z "${APP_CONTAINER}" && -n "${PROJ_DIR}" && -f "${PROJ_DIR}/.env" ]]; then
  APP_CONTAINER=$(awk -F= '/^APP_CONTAINER=/{print $2}' "${PROJ_DIR}/.env" | tail -1 | tr -d '"'"'"' \r')
fi
if [[ -z "${APP_CONTAINER}" ]]; then
  APP_CONTAINER=$(docker ps --format '{{.Names}}\t{{.Image}}' \
    | awk -F'\t' '$2 ~ /reimburse-workbench/ {print $1; exit}')
fi
[[ -n "${APP_CONTAINER}" ]] || die "找不到 app 容器，请用 --container 指定（docker ps 里能看到）"
docker inspect "${APP_CONTAINER}" >/dev/null 2>&1 || die "容器不存在：${APP_CONTAINER}"

CUR_TAG=$(docker inspect -f '{{.Config.Image}}' "${APP_CONTAINER}" | sed 's/.*://')
CUR_HOSTNAME=$(docker inspect -f '{{.Config.Hostname}}' "${APP_CONTAINER}")
COMPOSE_PROJ=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "${APP_CONTAINER}" 2>/dev/null || true)
[[ -n "${COMPOSE_PROJ}" && "${COMPOSE_PROJ}" != "<no value>" ]] || COMPOSE_PROJ=""
COMPOSE_FILE=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "${APP_CONTAINER}" 2>/dev/null || true)
[[ -n "${COMPOSE_FILE}" && "${COMPOSE_FILE}" != "<no value>" ]] || COMPOSE_FILE=""
COMPOSE_WD=$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "${APP_CONTAINER}" 2>/dev/null || true)
[[ -n "${COMPOSE_WD}" && "${COMPOSE_WD}" != "<no value>" ]] || COMPOSE_WD=""

# 安装目录三级定位：--dir / 脚本所在 → 容器 compose 配置路径 → compose 工作目录
# （有些 NAS 面板导入 compose 会把 working_dir 标签丢掉，只有 config_files 还在）
if [[ -z "${PROJ_DIR}" && -n "${COMPOSE_FILE}" ]]; then
  CF1="${COMPOSE_FILE%%,*}"          # 可能是逗号分隔的多个文件
  CF1="${CF1#\"}"; CF1="${CF1%\"}"   # 去掉两边引号
  [[ -f "${CF1}" ]] && PROJ_DIR="$(cd "$(dirname "${CF1}")" && pwd)"
fi
if [[ -z "${PROJ_DIR}" && -n "${COMPOSE_WD}" && -d "${COMPOSE_WD}" ]]; then
  PROJ_DIR="${COMPOSE_WD}"
fi
[[ -n "${COMPOSE_FILE}" ]] || COMPOSE_FILE="${PROJ_DIR:+${PROJ_DIR}/docker-compose.yml}"

[[ -n "${PROJ_DIR}" ]] || die "定位不到安装目录：这个容器没有 compose 标签（docker run / 面板手建的？）。
  请显式指定安装目录（有 .env 和 docker-compose.yml 的那个），例如：
    bash $0 --dir /opt/reimburse-workbench --container ${APP_CONTAINER} $*"
[[ -d "${PROJ_DIR}" ]] || die "安装目录不存在：${PROJ_DIR}"
ok "安装目录 ${PROJ_DIR}"
ok "app 容器 ${APP_CONTAINER}（当前镜像 ${CUR_TAG}，容器内 HOSTNAME=${CUR_HOSTNAME}）"
hint "这一行就是历史上「找不到自身容器」的关键：HOSTNAME 不等于容器名时，老版本升级器会认不出自己"

DB_CONTAINER=$(docker ps --format '{{.Names}}\t{{.Image}}' \
  | awk -F'\t' '$2 ~ /postgres/ {print $1; exit}')
PORT=$(awk -F= '/^APP_PORT=/{print $2}' "${PROJ_DIR}/.env" 2>/dev/null | tail -1 | tr -d '"'"'"' \r')
PORT="${PORT:-8080}"

[[ -n "${DB_CONTAINER}" ]] && ok "db 容器 ${DB_CONTAINER}" || warn "没找到 postgres 容器，将跳过数据库备份"
[[ -n "${COMPOSE_PROJ}" ]] && ok "compose 项目 ${COMPOSE_PROJ}" || warn "容器不是 compose 建的？将用目录名当项目名"
ok "compose 文件 ${COMPOSE_FILE}"

# ---------- 2. 目标版本 ----------
WORK="${PROJ_DIR}/.upgrade-tmp"
mkdir -p "${WORK}"

fetch_json() { # $1=url  $2=输出文件
  curl -fsSL --retry 2 --connect-timeout 20 -H "Accept: application/vnd.github+json" \
       -o "$2" "$1" 2>/dev/null || return 1
}

if [[ -z "${TAG}" ]]; then
  step "查询最新版本"
  if fetch_json "https://api.github.com/repos/${REPO}/releases/latest" "${WORK}/latest.json"; then
    TAG=$(tr -d '\n' < "${WORK}/latest.json" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  fi
  if [[ -z "${TAG}" ]]; then
    # API 有匿名限流（403），退回看 releases/latest 的跳转地址，同样能拿到 tag
    HINT_URL=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
      "https://github.com/${REPO}/releases/latest" 2>/dev/null || true)
    TAG=$(printf '%s' "${HINT_URL}" | sed -n 's#.*/tag/v\{0,1\}\([^/?]*\).*#\1#p')
  fi
  [[ -n "${TAG}" ]] || die "读不到最新版本（服务器出不了网？可加 --image 用手上的镜像包，或 --tag 指定版本）"
fi
TAG="${TAG#v}"
ok "目标版本 ${TAG}"

if [[ "${CHECK_ONLY}" == "1" ]]; then
  step "仅检查（--check）"
  say "  当前 ${CUR_TAG} → 目标 ${TAG}"
  say "  真升级请去掉 --check 再跑一次"
  exit 0
fi
[[ "${TAG}" == "${CUR_TAG}" ]] && { ok "已经是 ${TAG}，无需升级"; exit 0; }

# ---------- 3. 取镜像包 ----------
REL_BASE="https://github.com/${REPO}/releases/download/v${TAG}"
IMG_NAME="reimburse-workbench-${TAG}-linux-${ARCH}.tar.gz"
BND_NAME="reimburse-workbench-${TAG}-app.tar.gz"

if [[ -z "${IMAGE_TAR}" ]]; then
  step "下载镜像包（${IMG_NAME}，约 380MB，1-5 分钟）"
  fetch_json "https://api.github.com/repos/${REPO}/releases/tags/v${TAG}" "${WORK}/release.json" \
    || warn "读 Release 详情失败，直接用固定下载地址"
  # manifest 里的 sha256 才是准的；拿不到就跳过校验
  if fetch_json "${REL_BASE}/manifest.json" "${WORK}/manifest.json"; then
    WANT_SHA=$(tr -d '\n' < "${WORK}/manifest.json" \
      | sed -n "s/.*\"${ARCH}\"[^{}]*\"sha256\"[[:space:]]*:[[:space:]]*\"\([a-f0-9]*\)\".*/\1/p")
    ok "manifest 已取到"
  fi
  IMAGE_TAR="${WORK}/${IMG_NAME}"
  if [[ -s "${IMAGE_TAR}" && -n "${WANT_SHA:-}" && "$(sha_of "${IMAGE_TAR}")" == "${WANT_SHA}" ]]; then
    ok "镜像包已存在且校验通过，跳过下载"
  else
    curl -fL --retry 3 --connect-timeout 20 -o "${IMAGE_TAR}.part" "${REL_BASE}/${IMG_NAME}" \
      || die "下载失败：${REL_BASE}/${IMG_NAME}"
    mv "${IMAGE_TAR}.part" "${IMAGE_TAR}"
    ok "下载完成 $(du -h "${IMAGE_TAR}" | awk '{print $1}')"
  fi
  if [[ -n "${WANT_SHA:-}" ]]; then
    [[ "$(sha_of "${IMAGE_TAR}")" == "${WANT_SHA}" ]] \
      || die "镜像包 sha256 与 manifest 不一致（下载损坏？删掉 ${IMAGE_TAR} 重跑）"
    ok "sha256 校验通过"
  else
    warn "没拿到 manifest，跳过 sha256 校验"
  fi
else
  [[ -s "${IMAGE_TAR}" ]] || die "镜像包不存在：${IMAGE_TAR}"
  ok "使用本地镜像包 ${IMAGE_TAR}"
fi

# ---------- 4. 备份数据库 ----------
step "备份数据库"
BK_DIR="${PROJ_DIR}/backups"; mkdir -p "${BK_DIR}"
STAMP=$(date +%Y%m%d_%H%M%S)
if [[ -n "${DB_CONTAINER}" ]]; then
  DB_USER=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${DB_CONTAINER}" \
    | awk -F= '/^POSTGRES_USER=/{print $2}' | tail -1)
  DB_NAME=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${DB_CONTAINER}" \
    | awk -F= '/^POSTGRES_DB=/{print $2}' | tail -1)
  BK_FILE="${BK_DIR}/reimburse_pre-${TAG}_${STAMP}.sql.gz"
  if docker exec "${DB_CONTAINER}" sh -c "pg_dump -U '${DB_USER:-workbench}' -d '${DB_NAME:-workbench}'" \
       | gzip > "${BK_FILE}"; then
    [[ -s "${BK_FILE}" ]] && ok "已备份 $(du -h "${BK_FILE}" | awk '{print $1}') → ${BK_FILE}" \
      || die "备份文件是空的，停止升级"
  else
    die "pg_dump 失败，停止升级（数据库没备份就别动）"
  fi
else
  warn "跳过备份（未识别 db 容器），升级前请自行确认数据安全"
fi

# ---------- 5. 载入镜像 ----------
step "载入新镜像"
docker load -i "${IMAGE_TAR}" | tail -2
docker image inspect "reimburse-workbench:${TAG}" >/dev/null 2>&1 \
  || die "载入后找不到 reimburse-workbench:${TAG}"
GOT_ARCH=$(docker image inspect -f '{{.Architecture}}' "reimburse-workbench:${TAG}")
[[ "${GOT_ARCH}" == "${ARCH}" ]] \
  || die "镜像架构 ${GOT_ARCH} 与本机 ${ARCH} 不一致（能 load 但起不来），请换对应架构的包"
ok "镜像就绪（${GOT_ARCH}）"

# ---------- 6. 更新骨架文件（compose / install.sh / deploy / docs） ----------
if [[ "${NO_BUNDLE}" != "1" ]]; then
  step "更新 compose / 安装脚本等骨架文件"
  if [[ -z "${BUNDLE_TAR}" ]]; then
    BUNDLE_TAR="${WORK}/${BND_NAME}"
    curl -fsSL --retry 3 -o "${BUNDLE_TAR}" "${REL_BASE}/${BND_NAME}" 2>/dev/null || {
      warn "骨架包下载失败，跳过（compose 保持原样，不影响启动新镜像）"
      BUNDLE_TAR=""
    }
  fi
  if [[ -n "${BUNDLE_TAR}" && -s "${BUNDLE_TAR}" ]]; then
    XDIR="${WORK}/bundle"; rm -rf "${XDIR}"; mkdir -p "${XDIR}"
    tar -xzf "${BUNDLE_TAR}" -C "${XDIR}" 2>/dev/null || warn "骨架包解压失败，跳过"
    TSTAMP=$(date +%Y%m%d%H%M%S)
    for rel in docker-compose.yml install.sh VERSION .env.example deploy docs; do
      [[ -e "${XDIR}/${rel}" ]] || continue
      [[ -e "${PROJ_DIR}/${rel}" ]] && cp -R "${PROJ_DIR}/${rel}" "${PROJ_DIR}/${rel}.bak-${TSTAMP}" 2>/dev/null
      rm -rf "${PROJ_DIR:?}/${rel}"
      cp -R "${XDIR}/${rel}" "${PROJ_DIR}/${rel}"
    done
    ok "骨架文件已更新（原文件备份为 *.bak-${TSTAMP}）"
  fi
fi

# ---------- 7. 切版本 ----------
step "切换容器到 ${TAG}"
ENV_FILE="${PROJ_DIR}/.env"
if [[ -f "${ENV_FILE}" ]]; then
  cp "${ENV_FILE}" "${ENV_FILE}.bak-${STAMP}"
  if grep -q '^IMAGE_TAG=' "${ENV_FILE}"; then
    sed "s/^IMAGE_TAG=.*/IMAGE_TAG=${TAG}/" "${ENV_FILE}" > "${ENV_FILE}.new" && mv "${ENV_FILE}.new" "${ENV_FILE}"
  else
    printf '\nIMAGE_TAG=%s\n' "${TAG}" >> "${ENV_FILE}"
  fi
  ok ".env 的 IMAGE_TAG=${TAG}（原文件备份 ${ENV_FILE}.bak-${STAMP}）"
else
  warn "没有 .env，依赖 compose 默认值"
fi

COMPOSE_ARGS=(--env-file "${ENV_FILE}" -f "${COMPOSE_FILE}")
[[ -n "${COMPOSE_PROJ}" ]] && COMPOSE_ARGS+=(-p "${COMPOSE_PROJ}")
${COMPOSE} "${COMPOSE_ARGS[@]}" up -d --no-deps app 2>&1 | tail -5
ok "已提交容器切换"

# ---------- 8. 等健康检查 ----------
step "等待新版本就绪"
HEALTH=""
for i in $(seq 1 60); do
  HEALTH=$(curl -fsS -m 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || true)
  if printf '%s' "${HEALTH}" | grep -q '"status":"ok"'; then break; fi
  sleep 3
done

if printf '%s' "${HEALTH}" | grep -q '"status":"ok"'; then
  GOT_VER=$(printf '%s' "${HEALTH}" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
  ok "服务已就绪，版本 ${GOT_VER}"
  say ""
  say "${BOLD}升级完成：${CUR_TAG} → ${GOT_VER}${OFF}"
  say "  数据库备份：${BK_FILE}"
  say "  打不开页面时看日志：docker logs --tail 80 ${APP_CONTAINER}"
  rm -rf "${WORK}"
  exit 0
fi

say ""
printf '%s升级后健康检查没通过%s\n' "${RED}" "${OFF}" >&2
say "  最近日志："
docker logs --tail 40 "${APP_CONTAINER}" 2>&1 | sed 's/^/    /' >&2
say ""
say "${BOLD}回滚到 ${CUR_TAG}：${OFF}"
say "  sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=${CUR_TAG}/' ${ENV_FILE}"
say "  ${COMPOSE} ${COMPOSE_ARGS[*]} up -d --no-deps app"
say "  数据库如需恢复：gunzip -c ${BK_FILE} | docker exec -i ${DB_CONTAINER:-<db>} psql -U ${DB_USER:-workbench} -d ${DB_NAME:-workbench}"
exit 1
