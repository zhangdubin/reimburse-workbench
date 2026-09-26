#!/usr/bin/env bash
# ============================================================================
# 销售费用报销管理工作台 —— 一键部署
#
#   ./install.sh                          # 就地部署（默认端口 8080）
#   ./install.sh --port 8081              # 换端口（同机再起一套时用）
#   ./install.sh --dir /opt/reimburse     # 配置与备份放别处，代码仍在本包目录
#   ./install.sh --name stg --port 8081   # 实例后缀：容器名/数据卷自动隔离
#   ./install.sh --offline                # 离线：只认包内镜像，绝不联网构建
#   ./install.sh --build                  # 强制在目标机现场重建镜像
#
# 脚本只做四件事，每一步都可以重复执行：
#   1. 环境自检（docker、compose、端口占用）
#   2. 准备 .env —— 已存在就**原样沿用**，只补缺失项；口令与主密钥只在首次生成
#   3. 选定镜像 —— 本机已有 → 包内 images/*.tar.gz → 现场构建（--offline 时禁止）
#   4. 启动并等健康检查通过，打印访问地址与初始口令
#
# 兼容 bash 3.2（macOS 自带），不依赖 openssl / jq / nc / curl。
# ============================================================================
set -euo pipefail

# ---------- 输出 ----------
# 注意：中文字符紧跟在变量后面时必须写 ${VAR}，否则 bash 3.2 会把多字节字符
# 的首字节吞进变量名，报出一个完全看不出原因的 unbound variable。
BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YEL=$'\033[33m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "${BOLD}" "${OFF}" "$*"; }
ok()   { printf '  %s✔%s %s\n' "${GREEN}" "${OFF}" "$*"; }
warn() { printf '  %s!%s %s\n' "${YEL}" "${OFF}" "$*"; }
die()  { printf '\n%s✘ %s%s\n' "${RED}" "$*" "${OFF}" >&2; exit 1; }
hint() { printf '  %s%s%s\n' "${DIM}" "$*" "${OFF}"; }

# ---------- 参数 ----------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 脚本可能位于安装包根目录，也可能位于仓库的 deploy/ 下，两种位置都认
if [[ -f "${HERE}/docker-compose.yml" ]]; then PKG_DIR="${HERE}"; else PKG_DIR="$(dirname "${HERE}")"; fi

CONF_DIR="${PKG_DIR}"   # .env / 备份 落在这里
PORT=""; SUFFIX=""; OFFLINE=0; FORCE_BUILD=0; IMAGE_TAG_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)      CONF_DIR="$2"; shift 2 ;;
    --port)     PORT="$2"; shift 2 ;;
    --name)     SUFFIX="$2"; shift 2 ;;
    --tag)      IMAGE_TAG_ARG="$2"; shift 2 ;;
    --offline)  OFFLINE=1; shift ;;
    --build)    FORCE_BUILD=1; shift ;;
    -h|--help)  sed -n '3,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "未知参数：$1（用 --help 看用法）" ;;
  esac
done

[[ -f "${PKG_DIR}/docker-compose.yml" ]] || die "没找到 docker-compose.yml（应在 ${PKG_DIR}）——请在安装包根目录下运行本脚本"
mkdir -p "${CONF_DIR}" || die "无法创建目录 ${CONF_DIR}"
CONF_DIR="$(cd "${CONF_DIR}" && pwd)"
cd "${CONF_DIR}"

say ""
say "${BOLD}销售费用报销管理工作台 · 部署${OFF}"
hint "程序目录：${PKG_DIR}"
hint "配置目录：${CONF_DIR}"

# compose 的 .env 显式指定为配置目录里的那份：compose 默认只会去找「compose 文件同目录」
# 的 .env，配置目录换了地方它还照着老地方读，结果是静默用默认值启动（容器名不带后缀、
# 直接撞上生产容器）。这里把 -f / --project-directory / --env-file / -p 全部钉死，
# 让「代码放哪」「配置放哪」「实例叫什么」三件事互不干扰。
#
# -p 固定成 reimburse-workbench[-后缀]：项目名不再随目录名变化，
# 升级时不会因为换了目录而另建一套网络，也让下面「重装 vs 撞名」的判定有据可依。
#
# 空数组在 bash 3.2 下配 `set -u` 展开会报 unbound variable，所以统一写成
# ${ARR[@]+"${ARR[@]}"} —— 数组为空时展开成「什么都没有」，不为空时正常展开。
COMPOSE=(docker compose)
PROJECT="reimburse-workbench"
[[ -n "${SUFFIX}" ]] && PROJECT="${PROJECT}-${SUFFIX}"
COMPOSE_FA=(-p "${PROJECT}" --env-file "${CONF_DIR}/.env")
if [[ "${CONF_DIR}" != "${PKG_DIR}" ]]; then
  COMPOSE_FA=(-f "${PKG_DIR}/docker-compose.yml" --project-directory "${CONF_DIR}" ${COMPOSE_FA[@]+"${COMPOSE_FA[@]}"})
fi
compose_run() { ( cd "${CONF_DIR}" && "${COMPOSE[@]}" ${COMPOSE_FA[@]+"${COMPOSE_FA[@]}"} "$@" ); }

# 汇总里给人看的命令串（--dir 时要把 -f/--project-directory 一起带上，否则用户照抄会找不到 .env）
COMPOSE_SHOW="${COMPOSE[*]}"
[[ "${CONF_DIR}" != "${PKG_DIR}" ]] && COMPOSE_SHOW="${COMPOSE_SHOW} -f \"${PKG_DIR}/docker-compose.yml\" --project-directory \"${CONF_DIR}\""

# ---------- 1. 环境自检 ----------
step "检查运行环境"

command -v docker >/dev/null 2>&1 || die "未找到 docker。请先装 Docker（Linux: apt/yum install docker-ce；macOS: Docker Desktop）"
docker info >/dev/null 2>&1 || die "docker 命令在，但连不上引擎。确认 Docker 已启动、当前用户在 docker 组里（sudo usermod -aG docker \$USER，然后重新登录）"
DOCKER_VER="$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo 0)"
ok "docker ${DOCKER_VER}"

if docker compose version >/dev/null 2>&1; then
  :
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
  warn "用的是 v1 版 docker-compose，建议升级到 docker compose v2"
else
  die "缺少 Docker Compose v2（Linux 安装见 https://docs.docker.com/compose/install/linux/）"
fi
ok "compose $("${COMPOSE[@]}" version --short 2>/dev/null | head -1)"

# HTTP 取数：curl → wget → python3，三选一，都没有就用容器健康状态兜底
http_get() {  # http_get <url>
  if command -v curl >/dev/null 2>&1; then
    curl -s -m 3 "$1" 2>/dev/null || true
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 3 -O - "$1" 2>/dev/null || true
  elif command -v python3 >/dev/null 2>&1; then
    URL="$1" python3 -c 'import os,urllib.request;print(urllib.request.urlopen(os.environ["URL"],timeout=3).read().decode())' 2>/dev/null || true
  fi
}

# ---------- 2. .env ----------
step "准备配置（.env）"

rand_hex() {  # rand_hex <字节数>
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}
rand_pwd() {  # rand_pwd <长度>：去掉 0/O/1/l/I 这类口头传达容易出错的字符
  local n="$1" out="" raw
  while [[ ${#out} -lt ${n} ]]; do
    raw="$(head -c 64 /dev/urandom | LC_ALL=C tr -dc 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789' || true)"
    out="${out}${raw}"
  done
  printf '%s' "${out:0:n}"
}
env_get() {  # 从现有 .env 取值。刻意不 source —— 不执行待安装环境里的文件内容
  [[ -f .env ]] || return 0
  sed -n "s/^$1=//p" .env | tail -1
}
env_has() { [[ -n "$(env_get "$1")" ]]; }
set_env() {  # set_env <键> <值>
  if grep -q "^$1=" .env 2>/dev/null; then
    sed -i.bak "s|^$1=.*|$1=$2|" .env
    rm -f .env.bak
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}
keep_or_fill() {  # 只在值为空时写入 —— 绝不覆盖已有值
  env_has "$1" && return 0
  set_env "$1" "$2"
  ok "生成$3"
}

[[ -f "${PKG_DIR}/.env.example" ]] || die "缺少 .env.example，安装包不完整"
FIRST_INSTALL=0
if [[ ! -f .env ]]; then
  FIRST_INSTALL=1
  cp "${PKG_DIR}/.env.example" .env
  ok "由 .env.example 生成 .env"
else
  ok ".env 已存在 —— 沿用原配置，不重新生成任何口令与主密钥"
fi

if [[ -z "${PORT}" ]]; then PORT="$(env_get APP_PORT)"; fi
[[ -n "${PORT}" ]] || PORT=8080
case "${PORT}" in ''|*[!0-9]*) die "端口必须是数字，收到：${PORT}" ;; esac
if [[ "${PORT}" -lt 1024 ]] && [[ "$(id -u)" -ne 0 ]]; then
  warn "端口 ${PORT} 小于 1024，非 root 用户可能无权绑定"
fi

set_env APP_PORT "${PORT}"

# 数据库口令：默认样例值 workbench123 视为「没配」，一并换掉
DB_PWD="$(env_get DB_PASSWORD)"
if [[ -z "${DB_PWD}" || "${DB_PWD}" == "workbench123" ]]; then
  set_env DB_PASSWORD "$(rand_pwd 24)"
  ok "生成数据库口令（24 位随机）"
fi

# 主密钥：一旦生成就不再变动。换了它，库里已存的邮箱授权码永远解不开
keep_or_fill MAIL_SECRET "$(rand_hex 32)" "邮箱加密主密钥（32 字节）"

ADMIN_PWD="$(env_get ADMIN_PASSWORD)"
if [[ -z "${ADMIN_PWD}" ]]; then
  ADMIN_PWD="$(rand_pwd 12)"
  set_env ADMIN_PASSWORD "${ADMIN_PWD}"
  ok "生成初始管理员口令（12 位随机）"
fi

# 生产硬约束：演示数据自带固定口令账号，混进生产库等于留后门；
# ENV=prod 同时也让后端在缺 MAIL_SECRET 时直接拒绝启动（而不是静默用默认密钥）
set_env SEED_DEMO 0
set_env DOCS_ENABLED 0
set_env ENV prod

# 老引擎的坑：seccomp 白名单没有 clone3，线程创建被拒 → 同步接口全 500、容器反复重启。
# 引擎修了之后就别再放宽隔离，用默认策略。
ver_ge() {
  local a b i; IFS=. read -r -a a <<<"$1"; IFS=. read -r -a b <<<"$2"
  for i in 0 1 2; do
    local x="${a[i]:-0}" y="${b[i]:-0}"
    [[ "${x}" -gt "${y}" ]] && return 0
    [[ "${x}" -lt "${y}" ]] && return 1
  done
  return 0
}
if ! env_has SECCOMP_MODE; then
  if ver_ge "${DOCKER_VER}" "20.10.10"; then
    set_env SECCOMP_MODE default
    ok "seccomp 保持默认策略（引擎 ${DOCKER_VER} 已修复 clone3）"
  else
    set_env SECCOMP_MODE unconfined
    warn "引擎 ${DOCKER_VER} < 20.10.10 → seccomp=unconfined（否则容器起不来，详见 README「Docker 引擎兼容性」）"
  fi
fi

if [[ -n "${SUFFIX}" ]]; then
  set_env APP_CONTAINER "reimburse-app-${SUFFIX}"
  set_env DB_CONTAINER "reimburse-db-${SUFFIX}"
  set_env PGDATA_VOLUME "reimburse-pgdata-${SUFFIX}"
  set_env UPLOADS_VOLUME "reimburse-uploads-${SUFFIX}"
  ok "实例后缀 ${SUFFIX}：容器名与数据卷已隔离"
fi

# ---- 在线升级（v2.9.0+）：容器要能操作宿主 Docker ----
# 项目目录按原路径挂进容器：升级时要用新版本的 compose / install.sh 覆盖宿主文件
set_env HOST_PROJECT_DIR "${PKG_DIR}"

# Docker socket 的位置各平台不同：Linux 多在 /var/run，Docker Desktop 在用户目录下。
# 探测顺序是「当前 context 说的位置 → 几个常见位置」，挂错了只是升级功能用不了。
SOCK_PATH=""
CTX_HOST="$(docker context inspect -f '{{.Endpoints.docker.Host}}' 2>/dev/null | head -1 || true)"
if [[ "${CTX_HOST}" == unix://* ]]; then
  if [[ -S "${CTX_HOST#unix://}" ]]; then SOCK_PATH="${CTX_HOST#unix://}"; fi
fi
if [[ -z "${SOCK_PATH}" ]]; then
  for cand in /var/run/docker.sock "${HOME}/.docker/run/docker.sock" "${HOME}/.orbstack/run/docker.sock"; do
    if [[ -S "${cand}" ]]; then SOCK_PATH="${cand}"; break; fi
  done
fi
if [[ -n "${SOCK_PATH}" ]]; then
  set_env DOCKER_SOCK "${SOCK_PATH}"
  ok "在线升级就绪（Docker socket：${SOCK_PATH}）"

  # 把 socket 的宿主 gid 也写进 .env，compose 会把它加到 app 容器的附属组里。
  # 宿主的 docker.sock 普遍是 root:docker 660 权限；如果应用容器默认是 10001:10001，
  # 没有 docker 组，连不上 socket → 升级页报「容器拿不到宿主 Docker」。
  # 跨主机 docker gid 不一样（Linux 通常 999、但可能 998 或 1001）——只能现场查 stat 取。
  SOCK_GID="$(stat -c '%g' "${SOCK_PATH}" 2>/dev/null || stat -f '%g' "${SOCK_PATH}" 2>/dev/null || true)"
  if [[ -n "${SOCK_GID}" && "${SOCK_GID}" =~ ^[0-9]+$ ]]; then
    set_env DOCKER_GID "${SOCK_GID}"
    ok "检测到 Docker socket gid=${SOCK_GID}（compose 会把容器加到这个组）"
  else
    warn "拿不到 socket 的 gid——compose 会用 fallback ${DOCKER_GID:-999}，可能仍连不上 socket"
  fi
else
  warn "没找到 Docker socket：界面里的「一键升级」会不可用（其余功能不受影响）"
fi

if [[ -n "${SUFFIX}" ]]; then
  set_env UPGRADE_VOLUME "reimburse-upgrade-${SUFFIX}"
fi

SEC_LEN="$(printf '%s' "$(env_get MAIL_SECRET)" | wc -c | tr -d ' ')"
[[ "${SEC_LEN}" -ge 32 ]] || die "MAIL_SECRET 只有 ${SEC_LEN} 个字符（需 ≥32）。改成 openssl rand -hex 32 的输出后重跑——短密钥等于没有加密"

chmod 600 .env
ok "配置就绪（权限 600）"

# ---------- 3. 镜像 ----------
step "准备镜像"

# 镜像来源：由下面三条路径之一赋值（本机复用 / 包内导入 / 现场构建），结尾要打印。
# 必须先声明成空串——`set -u` 下，「本机没有该镜像」这条**首次安装的必经路径**
# 会跳过第一个 if，随后引用 ${IMG_SOURCE} 直接 unbound variable 崩掉，
# 症状就是干净机器上第一次装必失败（本机已有镜像时才侥幸不报）。
IMG_SOURCE=""

IMAGE_TAG="$(env_get IMAGE_TAG)"
[[ -n "${IMAGE_TAG}" ]] || IMAGE_TAG="2.9.17"
if [[ -n "${IMAGE_TAG_ARG}" ]]; then
  IMAGE_TAG="${IMAGE_TAG_ARG}"
  set_env IMAGE_TAG "${IMAGE_TAG}"
fi
APP_IMAGE="reimburse-workbench:${IMAGE_TAG}"

has_image() { docker image inspect "$1" >/dev/null 2>&1; }

# 目标机架构。docker 的架构名与 uname 不同，必须映射后再比对：
# 镜像 tar 是可以跨架构 docker load 的（load 不看架构，只存仓库标签），
# 但**跑起来**会 exec format error。所以「能找到 tar」不等于「这份 tar 能用」。
HOST_ARCH="$(uname -m)"
case "${HOST_ARCH}" in
  x86_64|amd64)   HOST_ARCH="amd64" ;;
  aarch64|arm64)  HOST_ARCH="arm64" ;;
  armv7l|armv7)   HOST_ARCH="armv7" ;;
  *)              HOST_ARCH="" ;;   # 认不出来就别自作聪明过滤
esac

img_arch() { docker image inspect "$1" --format '{{.Architecture}}' 2>/dev/null || true; }

# 本机已有镜像：架构不符时不能直接用（Air/开发机上的 amd64 镜像搬到 ARM 板子就是这种情况）
if [[ "${FORCE_BUILD}" -eq 0 ]] && has_image "${APP_IMAGE}"; then
  LOCAL_ARCH="$(img_arch "${APP_IMAGE}")"
  if [[ -z "${HOST_ARCH}" || -z "${LOCAL_ARCH}" || "${LOCAL_ARCH}" == "${HOST_ARCH}" ]]; then
    IMG_SOURCE="本机已有镜像"
    ok "复用本机镜像 ${APP_IMAGE}"
  else
    warn "本机已有 ${APP_IMAGE} 是 ${LOCAL_ARCH} 架构，装不到 ${HOST_ARCH} 机器上，改看安装包内镜像"
  fi
fi

if [[ -z "${IMG_SOURCE}" && "${FORCE_BUILD}" -eq 0 && -d "${PKG_DIR}/images" ]]; then
  # 选包顺序：本机架构的 → 不带架构后缀的（视为通用）→ 其余（架构不符，只提示不采用）。
  # 这一步必须按架构挑，不能 `ls ... | head -1`：包内同时放 amd64 与 arm64 两份 tar 时，
  # 字母序会让 arm64 机器拿到 amd64 那份，load 成功、启动直接 exec format error。
  SHORTLIST=()
  while IFS= read -r f; do [[ -n "${f}" ]] && SHORTLIST+=("${f}"); done < <(
    ls -1 \
      "${PKG_DIR}"/images/reimburse-workbench-"${IMAGE_TAG}"-*.tar.gz \
      "${PKG_DIR}"/images/reimburse-workbench-"${IMAGE_TAG}".tar.gz \
      "${PKG_DIR}"/images/reimburse-workbench-"${IMAGE_TAG}"-*.tar \
      "${PKG_DIR}"/images/reimburse-workbench-"${IMAGE_TAG}".tar \
      2>/dev/null || true
  )

  TARBALL=""
  OTHER_ARCH_TARS=()
  for f in "${SHORTLIST[@]:-}"; do
    [[ -n "${f}" ]] || continue
    name="$(basename "${f}")"
    case "${name}" in
      *-amd64.tar|*-amd64.tar.gz|*-arm64.tar|*-arm64.tar.gz|*-armv7.tar|*-armv7.tar.gz|*-arm.tar|*-arm.tar.gz)
        # 文件名写了架构：只有与本机一致才采用，别的一律只登记不采用
        if [[ -n "${HOST_ARCH}" && "${name}" == *"-${HOST_ARCH}.tar"* ]]; then
          TARBALL="${f}"
          break
        fi
        OTHER_ARCH_TARS+=("${f}") ;;
      *)
        # 没写架构，当作通用镜像
        [[ -n "${TARBALL}" ]] || TARBALL="${f}" ;;
    esac
  done
  [[ -n "${TARBALL}" ]] || TARBALL="${OTHER_ARCH_TARS[0]:-}"

  if [[ -n "${TARBALL}" ]]; then
    say "  导入镜像 $(printf '%s' "${TARBALL##*/}")（$(du -h "${TARBALL}" | cut -f1)），约 1-3 分钟…"
    if [[ "${TARBALL}" == *.gz ]]; then
      gzip -dc "${TARBALL}" | docker load
    else
      docker load -i "${TARBALL}"
    fi
    has_image "${APP_IMAGE}" || die "导入完成但没看到 ${APP_IMAGE}：检查包内镜像版本与 IMAGE_TAG（当前 ${IMAGE_TAG}）是否一致"

    LOADED_ARCH="$(img_arch "${APP_IMAGE}")"
    if [[ -n "${HOST_ARCH}" && -n "${LOADED_ARCH}" && "${LOADED_ARCH}" != "${HOST_ARCH}" ]]; then
      # 架构不符：镜像已经在本地库里了，但容器起不来（exec format error）。
      # 允许现场构建时继续往下走，别把路堵死；离线模式则必须停下说清楚。
      if [[ "${OFFLINE}" -eq 1 ]]; then
        die "包内镜像是 ${LOADED_ARCH} 架构，本机是 ${HOST_ARCH}：能 load 进去但容器起不来（exec format error）。
    请换用与本机架构一致的安装包（images/ 里选 -linux-${HOST_ARCH}.tar.gz 那份），
    或去掉 --offline 让脚本在目标机现场构建（原生架构，需能访问 pip 源）。"
      fi
      warn "包内镜像是 ${LOADED_ARCH} 架构，与本机 ${HOST_ARCH} 不符，忽略它改为现场构建"
    else
      IMG_SOURCE="安装包内镜像"
      ok "已从安装包导入 ${APP_IMAGE}（${LOADED_ARCH:-未知架构}）"
    fi
  fi
fi

if [[ -z "${IMG_SOURCE}" ]]; then
  if [[ "${OFFLINE}" -eq 1 ]]; then
    die "离线模式下没有可用镜像。
    本机架构：${HOST_ARCH:-未知}
    包内 images/ ：$( [[ -d "${PKG_DIR}/images" ]] && ls -1 "${PKG_DIR}/images" 2>/dev/null | tr '\n' ' ' || echo '目录不存在' )
    → 把 images/ 目录一起拷过来，或确认包内镜像的架构与本机一致（文件名带 -linux-<架构>）。"
  fi
  step "现场构建镜像（需能访问 pip 源）"
  warn "在目标机原生架构上构建，含本地 OCR 时约 5-15 分钟"
  compose_run build app
  has_image "${APP_IMAGE}" || die "构建结束但没看到 ${APP_IMAGE}"
  IMG_SOURCE="现场构建"
  ok "构建完成 ${APP_IMAGE}"
fi

# ---------- 4. 启动 ----------
step "启动服务"

APP_CONTAINER="$(env_get APP_CONTAINER)"; APP_CONTAINER="${APP_CONTAINER:-reimburse-app}"
DB_CONTAINER="$(env_get DB_CONTAINER)"; DB_CONTAINER="${DB_CONTAINER:-reimburse-db}"
DB_NAME="$(env_get DB_NAME)"; DB_NAME="${DB_NAME:-workbench}"
DB_USER="$(env_get DB_USER)"; DB_USER="${DB_USER:-workbench}"

# 本项目是否已经在跑（按 compose 项目标签找）。已经装过就属于「就地更新」，
# 这时候端口必然被自己的容器占着，不能再当成冲突把人拦下来。
RUNNING="$(docker ps --filter "label=com.docker.compose.project=${PROJECT}" --format '{{.Names}}' 2>/dev/null || true)"
if [[ -n "${RUNNING}" ]]; then
  ok "检测到本项目已在运行，按就地更新处理（不会重建数据卷）"
elif command -v lsof >/dev/null 2>&1 && lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  die "端口 ${PORT} 已被其他程序占用。换一个（--port 8081），或先停掉占用者"
fi

# 容器名冲突预检：同名容器若不属于「本项目 + 本配置目录」，说明撞上了另一套正在运行的
# 实例（多半是生产）。必须停手——硬上要么报一句很难懂的 Conflict，要么直接把别人的
# 容器接管过来换成我们的配置，那是最糟的结果。判定要同时看 project 与 working_dir：
# 只看 project 的话，「换个配置目录、但用默认名字」这种情形会被误判成同一次安装。
for cname in "${APP_CONTAINER}" "${DB_CONTAINER}"; do
  if docker inspect "${cname}" >/dev/null 2>&1; then
    owner="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${cname}" 2>/dev/null || true)"
    wdir="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "${cname}" 2>/dev/null || true)"
    if [[ "${owner}" != "${PROJECT}" || "${wdir}" != "${CONF_DIR}" ]]; then
      die "容器名 ${cname} 已被另一套实例占用
     （project=${owner:-未知}，配置目录=${wdir:-未知}）
     本次是 project=${PROJECT}，配置目录=${CONF_DIR}。二选一：
       a) 给这套换个独立身份：./install.sh --name <后缀> --port <另一个端口>
       b) 确认那套确实该退场：docker stop ${cname} && docker rm ${cname}
          （业务数据在数据卷里，删容器不会丢数据）"
    fi
  fi
done

# 提前创建 UPGRADE_VOLUME 并把权限放宽（关键预防）：
# 这卷在不同容器实例之间复用，旧实例若以 root 写过子目录，
# 当前 10001:10001 用户读不到写不进（permission denied），
# 一键升级会卡在「升级目录不可写」。先以本容器 UID/GID 创建并 chmod 777 兜底。
if [[ -n "${UPGRADE_VOLUME:-}" ]]; then
  if ! docker volume inspect "${UPGRADE_VOLUME}" >/dev/null 2>&1; then
    docker volume create "${UPGRADE_VOLUME}" >/dev/null
    ok "创建升级卷 ${UPGRADE_VOLUME}"
  fi
  # 拿一个临时 root 容器把权限放宽到 777——命名卷里通常安全，
  # 对挂载点和已有子目录递归都设
  docker run --rm \
    -v "${UPGRADE_VOLUME}:/upgrade" \
    --user root \
    alpine:latest \
    sh -c 'chmod -R 777 /upgrade 2>/dev/null; mkdir -p /upgrade && chmod 777 /upgrade' \
    >/dev/null 2>&1 || warn "放宽升级卷权限失败（不影响安装；如一键升级报'升级目录不可写'再手动修）"
fi

compose_run up -d

# 接管已有 db 数据卷时把 .env 里的 DB_PASSWORD 同步到存量 workbench 用户
# 场景：换架构重装（amd64 → arm64）、换机器迁移、多次 install 用同一个卷等
# ——Postgres 只在卷首次初始化时根据 POSTGRES_PASSWORD 建用户，env 改了不会动存量
# ——所以 app 容器起不来、循环重启，全是 "password authentication failed"
# 这一段不等 db 启动完（pg_isready 30s 超时即可），失败也不阻塞：
#   - 失败大多是 PGDATA_VOLUME 还没初始化（首次装），本来就不需要改
#   - 真正接管失败也只阻塞登录，不会让服务起不来
if [[ -n "${PGDATA_VOLUME:-}" ]] && docker volume inspect "${PGDATA_VOLUME}" >/dev/null 2>&1; then
  DESIRED_DB_PWD="$(env_get DB_PASSWORD)"
  if [[ -n "${DESIRED_DB_PWD}" && "${DESIRED_DB_PWD}" != "workbench123" ]]; then
    # 等 db 起来（最多 30s）
    if docker exec reimburse-db pg_isready -U workbench -d workbench >/dev/null 2>&1; then
      : # db 已经能用默认密码连 —— 可能在用旧密码
    fi
    # 用 .env 里的密码试连一次；能连就说明密码已对齐，跳过
    if docker exec -e PGPASSWORD="${DESIRED_DB_PWD}" reimburse-db \
         psql -U workbench -d workbench -c "SELECT 1" >/dev/null 2>&1; then
      : # 密码已对齐
    else
      # 用 postgres 维护连接（peer 认证免密）ALTER USER
      if docker exec -u postgres reimburse-db \
           psql -U postgres -d postgres -c "ALTER USER workbench WITH PASSWORD '${DESIRED_DB_PWD}';" >/dev/null 2>&1; then
        ok "同步 .env DB_PASSWORD → db 数据卷里的 workbench 用户"
      else
        warn "同步 DB_PASSWORD 到 db 数据卷失败 —— app 容器可能因密码不匹配反复重启"
        warn "手动修：docker exec -u postgres -e PGPASSWORD=<旧密码> reimburse-db psql -c \"ALTER USER workbench WITH PASSWORD '<.env 里的 DB_PASSWORD>';\""
      fi
    fi
  fi
fi

say "  等待服务就绪（首次启动要跑数据库迁移）…"
HEALTH_URL="http://127.0.0.1:${PORT}/api/health"
DEADLINE=$(( $(date +%s) + 240 ))
HSTATUS=""
while [[ "$(date +%s)" -lt "${DEADLINE}" ]]; do
  HSTATUS="$(http_get "${HEALTH_URL}")"
  [[ "${HSTATUS}" == *'"status":"ok"'* ]] && break
  # 容器已经退出就别再等了，直接把原因打出来
  if [[ "$(docker inspect -f '{{.State.Running}}' "${APP_CONTAINER}" 2>/dev/null || echo false)" != "true" ]]; then
    say ""
    say "${RED}容器已退出。最近 60 行日志：${OFF}"
    docker logs --tail 60 "${APP_CONTAINER}" 2>&1 || true
    die "服务未能启动。常见原因：端口被占、数据库口令与已有数据卷不匹配（换过 DB_PASSWORD 但卷没重建）"
  fi
  sleep 3
done

if [[ "${HSTATUS}" != *'"status":"ok"'* ]]; then
  say ""
  compose_run ps || true
  say ""
  say "${RED}等待超时。最近 60 行日志：${OFF}"
  docker logs --tail 60 "${APP_CONTAINER}" 2>&1 || true
  die "服务没能起来，排查看上面的日志"
fi

# 没装 curl/wget/python3 时拿不到 JSON，改用容器自检状态判断
ok "健康检查通过"

# ---------- 汇总 ----------
APP_VER="$(printf '%s' "${HSTATUS}" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
OCR_ENG="$(printf '%s' "${HSTATUS}" | sed -n 's/.*"ocr_engine":"\([^"]*\)".*/\1/p')"

LAN_IP=""
if command -v ip >/dev/null 2>&1; then
  LAN_IP="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
fi
if [[ -z "${LAN_IP}" ]] && command -v ipconfig >/dev/null 2>&1; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
fi
if [[ -z "${LAN_IP}" ]] && command -v hostname >/dev/null 2>&1; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi

ADMIN_USER="$(env_get ADMIN_USERNAME)"; ADMIN_USER="${ADMIN_USER:-admin}"

say ""
say "${GREEN}${BOLD}部署完成${OFF}"
say ""
say "  应用地址    http://127.0.0.1:${PORT}/"
[[ -n "${LAN_IP}" ]] && say "  局域网访问  http://${LAN_IP}:${PORT}/    ${DIM}手机与其他电脑用这个${OFF}"
say "  版本        ${APP_VER:-未知}${OCR_ENG:+ · OCR ${OCR_ENG}}   ${DIM}镜像来源：${IMG_SOURCE}${OFF}"
say "  配置目录    ${CONF_DIR}"
say ""
say "  ${BOLD}登录账号    ${ADMIN_USER}${OFF}"
say "  ${BOLD}初始口令    ${ADMIN_PWD}${OFF}"
say "  ${DIM}首次登录会强制要求改成你自己的口令；请现在就把上面这行收进密码管理器，别留在终端记录里${OFF}"
say ""
say "  ${BOLD}常用命令${OFF}${DIM}（在 ${CONF_DIR} 下执行）${OFF}"
say "    查看日志    ${COMPOSE_SHOW} logs -f app"
say "    重启        ${COMPOSE_SHOW} restart app"
say "    停止        ${COMPOSE_SHOW} stop          ${DIM}数据保留${OFF}"
say "    备份数据库  ${COMPOSE_SHOW} exec -T db pg_dump -U ${DB_USER} -d ${DB_NAME} | gzip > backup-\$(date +%F).sql.gz"
say "    镜像内备份  ${PKG_DIR}/deploy/backup.sh"
say ""
if [[ "${FIRST_INSTALL}" -eq 1 ]]; then
  say "  ${YEL}务必备份 .env 里的 MAIL_SECRET${OFF}：丢了它，库里存的邮箱授权码就再也解不开。"
  say "  ${DIM}而且密钥备份要与数据备份分开存放——放同一个地方，等于没加密。${OFF}"
  say ""
fi
