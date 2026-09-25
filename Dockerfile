# syntax=docker/dockerfile:1
#
# 构建参数（可在 docker compose build 时覆盖）：
#   PYTHON_IMAGE  基础镜像，默认 python:3.11-slim
#   PIP_INDEX     包索引，默认国内镜像（内网/国内构建快很多）
#   INSTALL_OCR   1 = 装本地 OCR（默认，约 300MB，图片型发票必需）
#                 0 = 不装，镜像小很多，但拍照件/扫描件只能人工补录
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG INSTALL_OCR=1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_PROGRESS_BAR=off

# 时区：不装 tzdata，也不跑 apt。
# 上面的基础镜像（python:3.*-slim）本身已带 /usr/share/zoneinfo，
# 配合 ENV TZ=Asia/Shanghai 即可让 date / datetime.now() 走北京时间（已验证输出 CST）。
# 另外 apt 在部分受限构建环境里会因为 docker-clean 钩子的 rm 返回非 0 而整条失败（exit 100），
# 所以这里刻意不引入 apt，README 的“已知边界”一节也记了这一点。
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 依赖单独一层，改业务代码不会触发重装。
COPY backend/requirements.txt /app/requirements.txt
COPY backend/requirements-ocr.txt /app/requirements-ocr.txt

# Data Matrix（v2.7.7+）需要 libdmtx C 库。
# apt-get install 在受限 Docker 环境会因 dpkg Post-Invoke 的 rm exit 100 整条回滚，
# 解 lzma 又会撞 Docker build 内存不够（"Cannot allocate memory"）。
# 兜底：在 macOS 主机上用 `ar x` 解 .deb 拿到 .so + dmtx.h，按架构放进 vendor 再 COPY 进镜像。
# 包来自 https://deb.debian.org/debian/pool/main/libd/libdmtx/
# （Debian trixie libdmtx 0.7.7-1.2+b1；amd64 与 arm64 取同一版本，两份 .so 同源）
#
# ⚠️ 架构（v2.9.3 修）：这里以前写死 x86_64-linux-gnu。在 arm64 机器上现场构建时，
#    本段会「成功」地把一个 x86-64 的 .so 装进 /usr/local/lib/x86_64-linux-gnu：
#    COPY 不校验架构、arm64 的 ldconfig 配置（aarch64-linux-gnu.conf）也扫不到这个目录，
#    所以既不报错、ls 还能看到文件，直到 `import pylibdmtx` 才抛
#    "Unable to find dmtx shared library" —— 而 print_qr 是启动期导入的，
#    结果就是**整个服务起不来**（只有打印条码这一个功能其实是坏的）。
#    修法：
#      ① 按 platform.machine() 选对应架构的 vendor 目录，缺了直接构建失败（绝不静默出坏镜像）；
#      ② 装到 /usr/local/lib —— Debian 的 /etc/ld.so.conf.d/libc.conf 在任何架构上都收录该目录，
#         不再依赖按架构命名的子目录；
#      ③ ldconfig 后反查 ldconfig -p 确认已登记；真正的 dlopen 校验在下面 pip 那一层（装完 pylibdmtx 才测得出）。
COPY backend/vendor/libdmtx /tmp/libdmtx
RUN set -e; \
    ARCH="$(python -c 'import platform; print(platform.machine())')"; \
    case "${ARCH}" in \
      armv7l|armv6l|arm) SRC_ARCH="arm-linux-gnueabihf" ;; \
      *)                  SRC_ARCH="${ARCH}-linux-gnu" ;; \
    esac; \
    SRC="/tmp/libdmtx/lib/${SRC_ARCH}"; \
    if [ ! -d "${SRC}" ]; then \
        echo "[build] 构建失败：vendor 里没有 ${ARCH} 架构的 libdmtx（找的是 ${SRC_ARCH}）"; \
        echo "[build] 现有：$(ls -1 /tmp/libdmtx/lib 2>/dev/null | tr '\n' ' ')"; \
        echo "[build] 补法：从 https://deb.debian.org/debian/pool/main/libd/libdmtx/ 取"; \
        echo "[build]   libdmtx0t64_<版本>_${ARCH}.deb（运行时 .so）与 libdmtx-dev_<版本>_${ARCH}.deb（.a/.h/pkgconfig），"; \
        echo "[build]   ar x 解包后放进 backend/vendor/libdmtx/lib/${SRC_ARCH}/ 再重新构建"; \
        exit 1; \
    fi; \
    mkdir -p /usr/local/lib; \
    cp -a "${SRC}"/libdmtx.so* /usr/local/lib/; \
    cp -a /tmp/libdmtx/include/. /usr/local/include/; \
    rm -rf /tmp/libdmtx; \
    ldconfig; \
    ldconfig -p | grep -q 'libdmtx\.so\.0' || { echo "[build] 构建失败：libdmtx 没被 ldconfig 登记（架构不符或路径不对）"; exit 1; }; \
    ls -l /usr/local/lib/libdmtx*; \
    echo "[build] libdmtx 离线安装完成（${ARCH} → ${SRC_ARCH}）"

# --progress-bar off 是必须的：pip 的 rich 进度条会另起一个刷新线程，
# 在受限的构建环境里线程创建可能被拒（RuntimeError: can't start new thread），
# 直接把整个安装搞挂。关掉进度条后纯单线程下载安装，稳定。
#
# OCR 那一段为什么要 uninstall 再装 headless：
# rapidocr-onnxruntime 的依赖声明里写的是 opencv-python，而 opencv-python
# 会链接 libGL（GUI 库），slim 镜像里没有，import cv2 直接报
# ImportError: libGL.so.1: cannot open shared object file。
# 本项目不跑 apt（见上文），装不了 libgl1，所以换成 headless 版——
# 接口完全一致，只是不链接 GUI，RapidOCR 用不到 GUI 那部分。
#
# 自检为什么带 *_NUM_THREADS=1：
# OpenBLAS 在 import numpy 时会按 CPU 核数创建线程池；在受限的容器环境里
# （老引擎 seccomp 白名单没有 clone3，见 README「Docker 引擎兼容性」），
# 线程创建失败会让 import 直接被 SIGINT 打断（表现为 KeyboardInterrupt、exit 130）。
# 线程数设为 1 跳过线程池创建，import 即可通过；对单页发票识别的耗时影响可忽略。
RUN set -e; \
    pip install --no-cache-dir --progress-bar off -i ${PIP_INDEX} -r /app/requirements.txt; \
    if [ "$INSTALL_OCR" = "1" ]; then \
        pip install --no-cache-dir --progress-bar off -i ${PIP_INDEX} -r /app/requirements-ocr.txt; \
        pip uninstall -y opencv-python >/dev/null 2>&1 || true; \
        pip install --no-cache-dir --progress-bar off -i ${PIP_INDEX} opencv-python-headless; \
        OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
            python -c "import cv2, numpy, onnxruntime, pypdfium2, rapidocr_onnxruntime; print('[build] OCR 依赖自检通过')"; \
    else \
        echo "[build] INSTALL_OCR=0：跳过本地 OCR，图片型发票将退回人工补录"; \
    fi

# libdmtx 的最终自检：**真 dlopen 一次 + 真 encode 一次**，不能用 `ls` 糊弄。
# v2.9.3 的教训：架构不对时 .so 文件确实在、ldconfig 也不报错，
# 只有真正 import + encode 才会暴露——老版本就是这么把坏镜像发出去了。
# 单独成层是为了不动上面 pip 那层的缓存（改动即重建，代价约 1 秒）。
RUN python -c "from pylibdmtx.pylibdmtx import encode; e = encode(b'selfcheck'); print('[build] pylibdmtx dlopen 自检通过:', e.width, 'x', e.height)"

COPY backend /app/backend
COPY frontend /app/frontend

# ---------------------------------------------------------------------------
# 非 root 运行
#
# 刻意**不调用 useradd/adduser**：Debian slim 里 passwd 包不保证存在，
# 而本项目已经因为 apt 在受限环境里 exit 100 踩过坑（见上）。
# Docker 允许直接用数字 UID/GID 指定运行身份，chown 也不要求该用户存在，
# 所以这里全程用 10001:10001，零额外依赖、零额外失败面。
# ---------------------------------------------------------------------------
ENV APP_UID=10001 \
    APP_GID=10001

# 科学计算库的线程池上限。默认 1：受限引擎（seccomp 无 clone3）里线程创建失败
# 会让 import numpy/cv2 直接被 SIGINT 打断，宁可单线程也别起不来；
# 引擎较新、想让 OCR 多线程加速时，在 compose/.env 里覆盖成核数即可。
ENV OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

# 影像（发票附件）落盘目录。单独一层挂在卷上，容器重建不丢文件。
ENV UPLOAD_DIR=/app/data/uploads
RUN mkdir -p ${UPLOAD_DIR} && chown -R ${APP_UID}:${APP_GID} /app/data

ENV FRONTEND_DIR=/app/frontend \
    PYTHONPATH=/app/backend

EXPOSE 8000

# 用 Python 自检，省掉 curl 依赖
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

USER ${APP_UID}:${APP_GID}

# 说明：刻意只用 1 个 worker。
# 应用启动时会执行 Alembic 迁移 + 管理员初始化，多 worker 会并发跑这段逻辑，
# 可能撞唯一约束。局域网团队规模下 1 worker + FastAPI 同步端点的线程池已经足够；
# 需要横向扩展时请把初始化拆成独立的一次性任务
# （docker compose run --rm app python -m app.seed --purge）再把 workers 调大。
CMD ["uvicorn", "app.main:app", "--app-dir", "/app/backend", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
