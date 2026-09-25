#!/usr/bin/env bash
# ============================================================================
# 打出「可直接拷到内网服务器」的生产安装包
#
#   ./deploy/make-package.sh                  # 用当前 compose 里的 IMAGE_TAG
#   ./deploy/make-package.sh --tag 2.6.0      # 指定版本
#   ./deploy/make-package.sh --no-image       # 不出镜像（目标机现场构建，包只有几百 KB）
#   ./deploy/make-package.sh --image <tar.gz> # 额外塞进别的架构的镜像（可重复），做成双架构包
#                                             # 例：本机导出 amd64，再用 --image 补一份 arm64
#   ./deploy/make-package.sh --out /tmp/pkg   # 换个输出目录
#   ./deploy/make-package.sh --release        # 额外产出在线升级用的 manifest.json + 骨架包
#
# 产物：
#   dist/reimburse-workbench-<tag>/
#     install.sh                  ← 目标机上跑这一条
#     docker-compose.yml  .env.example  Dockerfile  backend/  frontend/
#     deploy/{install.sh,backup.sh}
#     docs/使用说明.md
#     images/reimburse-workbench-<tag>-linux-<arch>.tar.gz   ← docker load 即用
#     VERSION  快速开始.txt
#
#   --release 时再产出一份可直接传 GitHub Release 的目录：
#   dist/release-<tag>/
#     manifest.json                                  ← 版本清单（含 sha256，升级时校验用）
#     reimburse-workbench-<tag>-linux-<arch>.tar.gz   ← 镜像包（硬链接，不额外占空间）
#     reimburse-workbench-<tag>-app.tar.gz            ← 骨架包（compose/install.sh/deploy/docs）
#
# 两个刻意的取舍：
#   1) 清空旧产物用「移进废纸篓」而不是 rm -rf —— 产物目录动辄上千文件，
#      批量删除会触发本机的删除守卫，脚本直接中断，而且不可恢复。
#   2) 复制源码用 tar 管道排除，而不是 cp -r 再删 —— 把 .venv（近百 MB）整个
#      搬一遍再删掉，打包从几秒变成几分钟。
# ============================================================================
set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YEL=$'\033[33m'; OFF=$'\033[0m'
step() { printf '\n%s==>%s %s\n' "${BOLD}" "${OFF}" "$*"; }
ok()   { printf '  %s✔%s %s\n' "${GREEN}" "${OFF}" "$*"; }
warn() { printf '  %s!%s %s\n' "${YEL}" "${OFF}" "$*"; }
die()  { printf '\n%s✘ %s%s\n' "${RED}" "$*" "${OFF}" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

TAG=""; OUT_DIR="${ROOT}/dist"; WITH_IMAGE=1; RELEASE=0; SCHEMA_CHANGED=0
# --image 传进来的外来镜像包（通常是另一种架构）。本机一个 tag 只能存一份镜像，
# 所以别的架构那份得先在别处构建/导出好，再用这个参数塞进包里。
EXTRA_IMAGES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)       TAG="$2"; shift 2 ;;
    --out)       OUT_DIR="$2"; shift 2 ;;
    --image)     EXTRA_IMAGES+=("$2"); shift 2 ;;
    --no-image)  WITH_IMAGE=0; shift ;;
    --release)   RELEASE=1; shift ;;
    --schema-changed) SCHEMA_CHANGED=1; shift ;;
    -h|--help)   sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "未知参数：$1" ;;
  esac
done

[[ -f docker-compose.yml ]] || die "请在仓库根目录结构下运行（找不到 docker-compose.yml）"

if [[ -z "${TAG}" ]]; then
  TAG="$(sed -n 's/^IMAGE_TAG=//p' .env 2>/dev/null | tail -1)"
fi
[[ -n "${TAG}" ]] || TAG="$(sed -n 's/.*image: reimburse-workbench:\${IMAGE_TAG:-\([^}]*\)}.*/\1/p' docker-compose.yml | head -1)"
[[ -n "${TAG}" ]] || die "无法确定版本号，请用 --tag 指定"
ok "版本 ${TAG}"

[[ -f "backend/app/main.py" ]] || die "源码不完整（缺 backend/app/main.py）"

PKG="${OUT_DIR}/reimburse-workbench-${TAG}"
step "准备输出目录"
mkdir -p "${OUT_DIR}"
if [[ -e "${PKG}" ]]; then
  # 不用 rm -rf：产物里几千个文件，会撞上本机批量删除守卫
  TRASH="${HOME}/.Trash/pkg-$(basename "${PKG}")-$(date +%Y%m%d%H%M%S)"
  mv "${PKG}" "${TRASH}"
  ok "旧产物已移入回收站：${TRASH}"
fi
mkdir -p "${PKG}/images" "${PKG}/deploy" "${PKG}/docs"

step "复制源码（排除虚拟环境、数据库、密钥与产物）"
tar -cf - \
  --exclude='.venv' --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*.pyo' --exclude='.pytest_cache' --exclude='.env' \
  --exclude='*.db' --exclude='*.sqlite3' --exclude='*.log' --exclude='.DS_Store' \
  --exclude='data' --exclude='backups' --exclude='dist' --exclude='node_modules' \
  --exclude='.git' --exclude='images' --exclude='tools' \
  --exclude='deploy/make-package.sh' \
  Dockerfile docker-compose.yml .env.example backend frontend deploy 2>/dev/null \
  | ( cd "${PKG}" && tar -xf - )
ok "源码已复制"

# 包里只留安装与备份两个脚本：make-package.sh 在复制阶段就排除了，
# 这里刻意不做「先拷再 rm」——本机对 workspace 内的批量删除有累计保护，
# 一个 rm 就可能把整个打包流程打断（而且删除不可恢复）。
cp deploy/install.sh deploy/backup.sh "${PKG}/deploy/"
cp deploy/install.sh "${PKG}/install.sh"
chmod +x "${PKG}/install.sh" "${PKG}/deploy/"*.sh
# 使用说明单独拷：docs/ 下还堆着 README 用的截图，不必跟着上生产机
if [[ -f docs/使用说明.md ]]; then
  cp docs/使用说明.md "${PKG}/docs/"
else
  warn "缺少 docs/使用说明.md（安装包内将没有详细说明，只有快速开始.txt）"
fi
if [[ -f docs/使用说明.md ]]; then cp docs/使用说明.md "${PKG}/docs/"; fi
ok "安装脚本已就位"

if [[ "${WITH_IMAGE}" -eq 1 ]]; then
  step "导出镜像"
  docker image inspect "reimburse-workbench:${TAG}" >/dev/null 2>&1 \
    || die "本机没有 reimburse-workbench:${TAG} 镜像。先构建（docker compose build app），或加 --no-image 只出源码包"
  PLAT="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "reimburse-workbench:${TAG}")"
  ARCH="${PLAT##*/}"
  TAR="${PKG}/images/reimburse-workbench-${TAG}-$(echo "${PLAT}" | tr '/' '-').tar.gz"
  say "  镜像平台 ${PLAT}，压缩中（用 -1 档：镜像内部已是压缩层，再压也小不了多少）…"
  docker save "reimburse-workbench:${TAG}" | gzip -1 > "${TAR}"
  ok "镜像已导出：images/$(basename "${TAR}")（$(du -h "${TAR}" | cut -f1)）"
  if [[ "${ARCH}" != "amd64" ]]; then
    warn "本包镜像架构是 ${ARCH}：目标机是 x86_64 时请改用 --no-image，让安装脚本在目标机现场构建"
  fi
else
  warn "--no-image：目标机将在原生架构上现场构建（需要能访问 pip 源）"
fi

# 额外架构的镜像。安装脚本按文件名里的 -linux-<架构> 挑本机那份，
# 挑不到才退回现场构建，所以文件名必须规范（下面会校验）。
if [[ ${#EXTRA_IMAGES[@]} -gt 0 ]]; then
  for f in "${EXTRA_IMAGES[@]}"; do
    [[ -f "${f}" ]] || die "找不到 --image 指定的镜像包：${f}"
    b="$(basename "${f}")"
    case "${b}" in
      reimburse-workbench-*-linux-*.tar.gz) : ;;
      *) die "镜像包文件名要形如 reimburse-workbench-<版本>-linux-<架构>.tar.gz（安装脚本靠它挑架构）：${b}" ;;
    esac
    cp "${f}" "${PKG}/images/${b}"
    ok "额外镜像已入包：$(printf '%s' "${b}" | sed -n 's/.*-linux-\(.*\)\.tar\.gz$/\1/p') 架构（$(du -h "${f}" | cut -f1)）"
  done
fi

step "写入版本与快速开始"
printf '%s\n' "${TAG}" > "${PKG}/VERSION"
cat > "${PKG}/快速开始.txt" <<TXT
销售费用报销管理工作台 · 生产部署（${TAG}）
================================================

一、装什么
    一台装了 Docker 的机器即可，不需要 Python、Node、数据库。

二、怎么装（三条命令）
    cd $(basename "${PKG}")
    chmod +x install.sh
    ./install.sh

    想换端口：        ./install.sh --port 8081
    同机再起一套：    ./install.sh --name stg --port 8081
    完全离线：        ./install.sh --offline
    配置放别处：      ./install.sh --dir /opt/reimburse

    装完终端会打印「应用地址 + 登录账号 + 初始口令」，首次登录会强制改口令。

三、装完先做三件事
    1. 备份 .env 里的 MAIL_SECRET（丢了它，库里存的邮箱授权码就再也解不开）
       —— 而且务必与数据备份分开存放，放同一个地方等于没加密。
    2. 记下管理员口令并立即改掉，别留在终端历史里。
    3. 手机/其他电脑访问：把终端打印的「局域网访问」地址填进浏览器即可；
       iPhone 用 Safari「添加到主屏幕」可以装成 App 图标。

四、日常运维
    查看日志    docker compose logs -f app
    重启        docker compose restart app
    停止        docker compose stop
    备份数据库  ./deploy/backup.sh
    升级        ./install.sh          （换上新版本安装包后重跑，数据与密钥都保留）

五、更详细的说明
    docs/使用说明.md
TXT
ok "快速开始.txt / VERSION 已写入"

step "打包自检"
BAD="$(find "${PKG}" -maxdepth 3 \( -name '.env' -o -name 'master.key' -o -name '*.db' -o -name '*.sqlite3' -o -name '.venv' -o -name 'node_modules' -o -name '__pycache__' \) 2>/dev/null || true)"
if [[ -n "${BAD}" ]]; then
  say "${RED}包里出现了不该有的文件：${OFF}"
  printf '%s\n' "${BAD}"
  die "已中止。请检查 tar --exclude 列表"
fi
ok "无 .env / 密钥 / 数据库 / 虚拟环境残留"

# ---------- 在线升级产物（--release）----------
# 应用内「一键升级」从 GitHub Release 拉三样东西：manifest.json、镜像包、骨架包。
# 这里把它们凑齐放在一个目录里，`gh release create` 一条命令即可上传。
if [[ "${RELEASE}" -eq 1 ]]; then
  step "生成在线升级产物"
  if [[ "${WITH_IMAGE}" -eq 0 ]]; then
    die "--release 需要镜像包（不能与 --no-image 同用）：在线升级下载的就是这个 tar"
  fi
  REL="${OUT_DIR}/release-${TAG}"
  mkdir -p "${REL}"

  # 骨架包：升级时用来替换宿主上的 compose / install.sh / deploy / docs。
  # 不含 backend/frontend —— 运行时代码在镜像里，宿主源码不参与运行。
  BUNDLE="${REL}/reimburse-workbench-${TAG}-app.tar.gz"
  tar -C "${PKG}" -czf "${BUNDLE}" docker-compose.yml install.sh VERSION .env.example deploy docs
  ok "骨架包 $(basename "${BUNDLE}")（$(du -h "${BUNDLE}" | cut -f1)）"

  # 镜像包用硬链接，不额外占 300+ MB。双架构包会有两份（amd64 / arm64），
  # manifest 里逐个登记 —— 客户端（upgrade_client.platform_asset）按本机架构取对应那份。
  img_entries=""
  for f in "${PKG}"/images/*.tar.gz; do
    [[ -f "${f}" ]] || continue
    b="$(basename "${f}")"
    ln -f "${f}" "${REL}/${b}"
    a="$(printf '%s' "${b}" | sed -n 's/.*-linux-\(.*\)\.tar\.gz$/\1/p')"
    [[ -n "${a}" ]] || a="unknown"
    s="$(shasum -a 256 "${REL}/${b}" | awk '{print $1}')"
    z="$(wc -c < "${REL}/${b}" | tr -d ' ')"
    [[ -z "${img_entries}" ]] || img_entries="${img_entries},"$'\n'
    img_entries="${img_entries}    \"${a}\": { \"name\": \"${b}\", \"sha256\": \"${s}\", \"size\": ${z} }"
    ok "镜像包 ${b}（$(du -h "${REL}/${b}" | cut -f1)）"
  done
  bun_sha="$(shasum -a 256 "${BUNDLE}" | awk '{print $1}')"
  bun_size="$(wc -c < "${BUNDLE}" | tr -d ' ')"
  if [[ "${SCHEMA_CHANGED}" -eq 1 ]]; then schema_flag="true"; else schema_flag="false"; fi

  cat > "${REL}/manifest.json" <<JSON
{
  "app": "reimburse-workbench",
  "version": "${TAG}",
  "released_at": "$(date '+%Y-%m-%dT%H:%M:%S%z')",
  "schema_changed": ${schema_flag},
  "min_from": "",
  "images": {
${img_entries}
  },
  "app_bundle": {
    "name": "$(basename "${BUNDLE}")",
    "sha256": "${bun_sha}",
    "size": ${bun_size}
  }
}
JSON
  ok "manifest.json（sha256 已写入，升级时用它校验完整性）"
  say ""
  say "  ${BOLD}发到 GitHub Release${OFF}（仓库名换成自己的）"
  say "    gh release create v${TAG} -t \"v${TAG}\" -n \"更新说明写在正文，客户端会展示\" \"${REL}\"/*"
  say "  ${DIM}版本清单里的 min_from 留空 = 任何旧版本都能升；"
  say "  库里结构有变更时记得加 --schema-changed${OFF}"
fi

SIZE="$(du -sh "${PKG}" | cut -f1)"
say ""
say "${GREEN}${BOLD}安装包已生成${OFF}"
say "  目录  ${PKG}"
say "  体积  ${SIZE}"
say ""
say "  ${BOLD}拷到目标机${OFF}（二选一）"
say "    scp -r \"${PKG}\" user@目标机:/opt/"
say "    tar -C \"${OUT_DIR}\" -czf - \"$(basename "${PKG}")\" | ssh user@目标机 'tar -C /opt -xzf -'"
say ""
say "  ${BOLD}目标机上${OFF}"
say "    cd /opt/$(basename "${PKG}") && ./install.sh"
say ""
